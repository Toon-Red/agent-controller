"""L7 Dispatch protocol -- L8 <-> L7 wire contract (AC-S16g).

The canonical home for the bidirectional protocol between L8 (Project
Manager, oversight) and L7 (Dispatch, operational). Both directions
have a structured message shape so wire format and Python contract
stay in lock-step.

Directions:

  * **L8 -> L7 (dispatch request)**: L8 hands a unit of work to L7
    for execution. Carries the PD task pointer, the claim status,
    and engine/dispatch hints. L7 responds synchronously with an
    accept/reject envelope; the actual work runs async, surfaced
    back via the L7 -> L8 progress feed.

  * **L7 -> L8 (progress feed)**: L7 reports iteration status up.
    Re-exported from :mod:`scripts.l8_project_manager` as the
    canonical :class:`L7ProgressMessage` (already shipped via the
    AC-S16 orchestrator commit).

Transport: HTTP. L8 POSTs to L7's dispatch endpoint; L7 POSTs
progress messages to L8's observability endpoint OR drops them in a
shared sqlite table (deferred to AR-S3h's loop_continuous runtime
choice). This module owns the JSON shape; the transport seam is
configurable so a future routing layer (Dream UI, MCP, queue) can
plug in without rewriting the contract.

"Dispatch" in our ecosystem today is :mod:`dream.orchestrator` --
specifically ``run_work_cycle``. That code is the operational L7 in
practice; it picks tasks, claims them (PD lock from c44d761), and
dispatches to swarm / agent backends. AC-S16g formalises what L8
needs to hand it (without requiring an orchestrator rewrite -- the
endpoint stub lives in this module's documentation; the actual
endpoint wiring on the L7 side is a follow-on if/when Dream's
orchestrator grows a programmatic dispatch surface).

Design parity with SOD/EOD modules:

  * Frozen dataclasses for messages.
  * JSON round-trip via :func:`to_wire` / :func:`from_wire`.
  * Pure validation; transport-agnostic.
  * Defensive on missing fields -- never raises on optional data.

Per Preston 2026-05-13 hierarchy spec: L7 is Preston's OPERATIONAL
interface; L8 is the OVERSIGHT interface. Both are first-class.
This protocol describes how the two layers coordinate -- it does
NOT subordinate L7 to L8. Preston can talk to either directly.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping, Optional


# Re-export the already-shipped L7 -> L8 progress shape so callers
# import "one protocol module" instead of having to know it lives in
# l8_project_manager.
from scripts.l8_project_manager import L7ProgressMessage  # noqa: F401


log = logging.getLogger("agent-controller.l7_dispatch_protocol")

DEFAULT_DISPATCH_TIMEOUT = 5
DEFAULT_L7_ENDPOINT = "http://127.0.0.1:5005/api/orchestrator/dispatch"


# Valid status / kind enum values. Validation rejects anything else
# so the wire shape stays narrow.
VALID_DISPATCH_STATUS = frozenset({"accepted", "rejected", "queued"})
VALID_DISPATCH_KIND = frozenset({"task_handoff", "cancellation", "ping"})


# ---------------------------------------------------------------------------
# Message shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class L7DispatchRequest:
    """L8 -> L7 work handoff.

    Fields
    ------
    iteration_id:
        Stable id for the L8-L4 cycle this request belongs to.
        Echoed back on the L7ProgressMessage feed for correlation.
    kind:
        ``task_handoff`` (default) | ``cancellation`` | ``ping``.
    pd_project_id / pd_task_id:
        Pointer to the PD task. L7 reads PD for full task details;
        we don't duplicate them on the wire.
    claim_token:
        The actor id that holds the PD per-task claim. L7 inherits
        this claim (uses the same claimer string for its heartbeats);
        the PD lock primitive's stale-takeover handles crash recovery.
    engines:
        Optional per-layer engine override map for this dispatch.
        L7 forwards to L6 (claude-flow hive-mind); empty means the
        L4-L8 stack uses templates/settings.json defaults.
    goal_template_text:
        Optional pre-rendered ``/goal`` text (per AR-S3j). When
        present, L7 fires this verbatim at the layer the loop
        configuration designates.
    deadline_iso:
        Optional ISO timestamp; L7 abandons + reports back via
        L7ProgressMessage{status=blocked, blockers=[deadline_missed]}
        if work isn't done by then.
    metadata:
        Free-form dict for protocol extensions. Carried verbatim.
    """

    iteration_id: str
    pd_project_id: str
    pd_task_id: str
    kind: str = "task_handoff"
    claim_token: Optional[str] = None
    engines: Mapping[str, str] = field(default_factory=dict)
    goal_template_text: Optional[str] = None
    deadline_iso: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        """JSON-serialisable dict. Empty mappings collapse to ``{}``."""
        return {
            "iteration_id": self.iteration_id,
            "kind": self.kind,
            "pd_project_id": self.pd_project_id,
            "pd_task_id": self.pd_task_id,
            "claim_token": self.claim_token,
            "engines": dict(self.engines),
            "goal_template_text": self.goal_template_text,
            "deadline_iso": self.deadline_iso,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> "L7DispatchRequest":
        """Parse a wire dict back into the dataclass. Raises
        :class:`ProtocolError` on missing required fields or invalid
        kind."""
        if not isinstance(payload, Mapping):
            raise ProtocolError(
                f"L7DispatchRequest payload must be a mapping, got "
                f"{type(payload).__name__}"
            )
        missing = [f for f in ("iteration_id", "pd_project_id", "pd_task_id")
                    if not payload.get(f)]
        if missing:
            raise ProtocolError(
                f"L7DispatchRequest missing required field(s): "
                f"{', '.join(missing)}"
            )
        kind = payload.get("kind", "task_handoff")
        if kind not in VALID_DISPATCH_KIND:
            raise ProtocolError(
                f"L7DispatchRequest.kind: {kind!r} not in "
                f"{sorted(VALID_DISPATCH_KIND)}"
            )
        engines = payload.get("engines") or {}
        metadata = payload.get("metadata") or {}
        if not isinstance(engines, Mapping):
            raise ProtocolError("L7DispatchRequest.engines must be a mapping")
        if not isinstance(metadata, Mapping):
            raise ProtocolError("L7DispatchRequest.metadata must be a mapping")
        return cls(
            iteration_id=str(payload["iteration_id"]),
            pd_project_id=str(payload["pd_project_id"]),
            pd_task_id=str(payload["pd_task_id"]),
            kind=kind,
            claim_token=payload.get("claim_token"),
            engines=dict(engines),
            goal_template_text=payload.get("goal_template_text"),
            deadline_iso=payload.get("deadline_iso"),
            metadata=dict(metadata),
        )


@dataclass(frozen=True)
class L7DispatchResponse:
    """L7 -> L8 immediate response to a dispatch request.

    Synchronous accept/reject envelope. The actual work outcome
    surfaces later through :class:`L7ProgressMessage`.

    Fields
    ------
    iteration_id:
        Echoed from the request.
    status:
        ``accepted`` (L7 took the work) | ``rejected`` (L7 declined)
        | ``queued`` (L7 took it but hasn't started yet).
    message:
        Human-readable reason. Required when status == ``rejected``.
    started_at:
        ISO timestamp for when L7 actually starts processing
        (populated by L7; None for queued / rejected).
    """

    iteration_id: str
    status: str
    message: str = ""
    started_at: Optional[str] = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "iteration_id": self.iteration_id,
            "status": self.status,
            "message": self.message,
            "started_at": self.started_at,
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> "L7DispatchResponse":
        if not isinstance(payload, Mapping):
            raise ProtocolError(
                f"L7DispatchResponse payload must be a mapping, got "
                f"{type(payload).__name__}"
            )
        if not payload.get("iteration_id"):
            raise ProtocolError(
                "L7DispatchResponse missing required iteration_id"
            )
        status = payload.get("status")
        if status not in VALID_DISPATCH_STATUS:
            raise ProtocolError(
                f"L7DispatchResponse.status: {status!r} not in "
                f"{sorted(VALID_DISPATCH_STATUS)}"
            )
        return cls(
            iteration_id=str(payload["iteration_id"]),
            status=status,
            message=str(payload.get("message") or ""),
            started_at=payload.get("started_at"),
        )


class ProtocolError(ValueError):
    """Raised on malformed L8 <-> L7 wire payloads."""


# ---------------------------------------------------------------------------
# Transport: L7DispatchClient
# ---------------------------------------------------------------------------

class L7DispatchClient:
    """L8-side client for issuing dispatch requests to L7.

    Default transport: HTTP POST to ``DEFAULT_L7_ENDPOINT`` (Dream's
    orchestrator port -- Dream's run_work_cycle IS our operational
    L7 today). The opener / endpoint URL are injectable so tests
    don't hit the network and a future routing layer can swap the
    transport.

    Error semantics (mirrors discord_ping / dream orchestrator's
    fail-open pattern):

      * Happy path -> returns :class:`L7DispatchResponse`.
      * L7 unreachable / 5xx / malformed payload -> returns ``None``
        and logs a warning. L8 callers decide whether to retry,
        downgrade to direct PD-task work, or surface to Preston via
        :class:`scripts.discord_ping.send_escalation`.
    """

    def __init__(self, *,
                  endpoint: str = DEFAULT_L7_ENDPOINT,
                  timeout: int = DEFAULT_DISPATCH_TIMEOUT,
                  opener: Callable[..., Any] = urllib.request.urlopen,
                  ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self._opener = opener

    def dispatch(self, request: L7DispatchRequest
                  ) -> Optional[L7DispatchResponse]:
        """Send a dispatch request to L7. Returns the response on
        success; None on transport / protocol failure."""
        body = json.dumps(request.to_wire()).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as exc:
            log.warning("L7 dispatch HTTP %s: %s", exc.code, exc.reason)
            return None
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            log.warning("L7 unreachable at %s: %s", self.endpoint, exc)
            return None

        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            log.warning("L7 returned non-JSON: %s", exc)
            return None
        try:
            return L7DispatchResponse.from_wire(payload)
        except ProtocolError as exc:
            log.warning("L7 returned malformed response: %s", exc)
            return None


# ---------------------------------------------------------------------------
# L7-side helpers (for future Dispatch implementation)
# ---------------------------------------------------------------------------

def build_progress_message(
    iteration_id: str,
    current_task: Mapping[str, Any],
    *,
    status: str = "running",
    blockers: tuple[str, ...] = (),
    completed_this_iteration: tuple[str, ...] = (),
) -> L7ProgressMessage:
    """Convenience constructor for the L7 side of the protocol.

    The L7 implementation (currently Dream's orchestrator; future
    standalone Dispatch module) uses this to emit progress messages
    consumable by L8's observe_l7_progress (already shipped).
    """
    if status not in {"running", "blocked", "done"}:
        raise ProtocolError(
            f"L7ProgressMessage.status: {status!r} must be running / "
            "blocked / done"
        )
    return L7ProgressMessage(
        iteration_id=iteration_id,
        current_task=dict(current_task),
        status=status,
        blockers=tuple(blockers),
        completed_this_iteration=tuple(completed_this_iteration),
    )


__all__ = [
    "DEFAULT_DISPATCH_TIMEOUT",
    "DEFAULT_L7_ENDPOINT",
    "L7DispatchClient",
    "L7DispatchRequest",
    "L7DispatchResponse",
    "L7ProgressMessage",
    "ProtocolError",
    "VALID_DISPATCH_KIND",
    "VALID_DISPATCH_STATUS",
    "build_progress_message",
]
