"""Multi-engine driver layer for agent-controller.

This module provides the engine abstraction used by every role in the
L4-L8 hierarchy. AC-S10 is the umbrella task; AC-S14 (this change)
adds the ``human`` driver and the handoff machinery on top of a
minimal driver contract so the human-engine path is testable
end-to-end before the AI adapters land.

Contract (per AC-S10 / AC-S14):

    driver = Dispatcher.resolve(engine_id)
    response = driver.run(EngineRequest(prompt=..., context=...))
    # response.text  -- the role's response
    # response.source -- "human" | "ai" | "mixed"
    # response.attribution -- byte-counts per source, for AC-S11

The ``HumanDriver``:

* Surfaces the EXACT prompt + context the AI would have received --
  byte-for-byte, no summarisation -- via the configured
  ``surface`` callable (default: stdout).
* Collects the user's response via the configured ``input_provider``
  (default: a multi-line ``stdin`` read).
* If ``handoff_to`` is configured AND ``handoff_trigger`` fires while
  the user is mid-response, the partial response is forwarded as
  additional context to the named AI driver; the AI driver's output
  is appended to the user's partial and the merged response is
  returned with ``source="mixed"``.

Telemetry attribution (AC-S11 hook): each ``EngineResponse`` carries
a ``source`` field and an ``attribution`` dict counting bytes by
source. The cost-tracker hook can roll this up per-level.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

# ---------------------------------------------------------------------------
# Public data contracts
# ---------------------------------------------------------------------------

# Sentinel returned by an input_provider when the human triggers
# mid-flight handoff via a keyword. The payload is the user's partial
# response captured up to the trigger.
HANDOFF_KEYWORD_SENTINEL = "__HANDOFF_KEYWORD__"

# Sentinel for an input_provider that timed out mid-response.
HANDOFF_TIMEOUT_SENTINEL = "__HANDOFF_TIMEOUT__"


@dataclass
class EngineRequest:
    """Inputs to a driver call.

    ``prompt`` is the role's system / instruction text. ``context`` is
    whatever upstream context the orchestrator gathered (file
    excerpts, prior swarm messages, etc.). The human-engine path
    MUST surface both verbatim -- no truncation, no summarisation.
    """

    prompt: str
    context: str = ""
    role: Optional[str] = None
    level: Optional[str] = None
    extras: Mapping[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        """Render prompt + context as a single byte-for-byte payload.

        The format is deterministic so a human reading it sees the
        same bytes the AI would have received downstream.
        """
        parts: list[str] = []
        if self.role or self.level:
            header = f"[role={self.role or '?'} level={self.level or '?'}]"
            parts.append(header)
        parts.append("=== PROMPT ===")
        parts.append(self.prompt)
        if self.context:
            parts.append("=== CONTEXT ===")
            parts.append(self.context)
        return "\n".join(parts)


@dataclass
class EngineResponse:
    """Outputs from a driver call.

    ``source`` distinguishes human-bytes from AI-bytes for AC-S11's
    per-level token-spend stream:

    * ``human`` -- response came entirely from a human-engine role.
    * ``ai``    -- response came entirely from an AI driver.
    * ``mixed`` -- partial human response was handed off mid-flight
                   to an AI driver and the final response is a
                   concatenation of both.

    ``attribution`` is a byte-count map keyed by source; the
    cost-tracker hook rolls this up per-level.
    """

    text: str
    source: str = "ai"
    attribution: dict[str, int] = field(default_factory=dict)
    handoff_to: Optional[str] = None
    handoff_trigger: Optional[str] = None  # "keyword" | "timer" | None


# ---------------------------------------------------------------------------
# Driver contract
# ---------------------------------------------------------------------------


class BaseDriver:
    """Abstract base for every engine driver."""

    engine_id: str = "base"

    def run(self, request: EngineRequest) -> EngineResponse:  # pragma: no cover - abstract
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Dispatcher / registry
# ---------------------------------------------------------------------------


class Dispatcher:
    """Resolve an engine id to a concrete driver instance.

    Driver instances are registered with ``register(engine_id, driver)``.
    AC-S14 only needs the human driver + a stub AI driver for the
    handoff path; the real claude / ollama / openai / google drivers
    land in AC-S10.
    """

    def __init__(self) -> None:
        self._drivers: dict[str, BaseDriver] = {}

    def register(self, engine_id: str, driver: BaseDriver) -> None:
        self._drivers[engine_id] = driver

    def resolve(self, engine_id: str) -> BaseDriver:
        try:
            return self._drivers[engine_id]
        except KeyError as exc:
            known = ", ".join(sorted(self._drivers)) or "<none registered>"
            raise UnknownEngineError(
                f"engine {engine_id!r} not registered (known: {known})"
            ) from exc

    def known(self) -> list[str]:
        return sorted(self._drivers)


class UnknownEngineError(LookupError):
    """Raised when an engine id has no registered driver."""


# ---------------------------------------------------------------------------
# Human driver
# ---------------------------------------------------------------------------


# Input provider contract:
#   provider(rendered_payload: str, *,
#            handoff_trigger: Optional[dict],
#            clock: Callable[[], float]) -> tuple[str, Optional[str]]
#
# Returns (partial_or_full_text, trigger_kind) where trigger_kind is:
#   - None       -> user finished without triggering handoff
#   - "keyword"  -> on_keyword fired
#   - "timer"    -> on_timer fired
InputProvider = Callable[..., "tuple[str, Optional[str]]"]


class HumanDriver(BaseDriver):
    """Pause-and-prompt driver with optional mid-flight AI handoff.

    Parameters
    ----------
    handoff_to:
        Engine id (registered on the same dispatcher) to hand off to
        when the trigger fires. ``None`` disables handoff.
    handoff_trigger:
        ``{"on_keyword": "/continue"}`` or ``{"on_timer": 600}``.
        Honoured by the configured ``input_provider``.
    surface:
        Callable used to surface the prompt + context to the user.
        Default writes to stdout. The Dream tab UI (research
        0fa2b8ad) plugs in here.
    input_provider:
        Callable returning the user's response (see contract above).
        Default reads multi-line stdin until EOF (Ctrl-D / Ctrl-Z).
    dispatcher:
        The dispatcher used to resolve ``handoff_to``. Required when
        ``handoff_to`` is set.
    clock:
        Time source, injectable for tests.
    """

    engine_id = "human"

    def __init__(
        self,
        *,
        handoff_to: Optional[str] = None,
        handoff_trigger: Optional[Mapping[str, Any]] = None,
        surface: Optional[Callable[[str], None]] = None,
        input_provider: Optional[InputProvider] = None,
        dispatcher: Optional[Dispatcher] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if handoff_to and dispatcher is None:
            raise ValueError(
                "handoff_to is set but no dispatcher was provided; "
                "HumanDriver needs a Dispatcher to resolve the handoff target"
            )
        self.handoff_to = handoff_to
        self.handoff_trigger = dict(handoff_trigger) if handoff_trigger else None
        self.surface = surface or _stdout_surface
        self.input_provider = input_provider or _stdin_input_provider
        self.dispatcher = dispatcher
        self.clock = clock

    # ------------------------------------------------------------------
    # Core contract
    # ------------------------------------------------------------------

    def run(self, request: EngineRequest) -> EngineResponse:
        # Surface the EXACT prompt + context (byte-for-byte).
        rendered = request.render()
        self.surface(rendered)

        # Collect the user's response. The input_provider is
        # responsible for honouring the handoff_trigger.
        partial, trigger_kind = self.input_provider(
            rendered,
            handoff_trigger=self.handoff_trigger,
            clock=self.clock,
        )

        # No handoff requested / available -> pure human response.
        if not trigger_kind or not self.handoff_to:
            return EngineResponse(
                text=partial,
                source="human",
                attribution={"human": len(partial.encode("utf-8"))},
            )

        # Mid-flight handoff: route the user's partial response as
        # ADDITIONAL CONTEXT to the configured AI driver, then merge.
        assert self.dispatcher is not None  # enforced in __init__
        ai_driver = self.dispatcher.resolve(self.handoff_to)
        handoff_context = _build_handoff_context(request.context, partial, trigger_kind)
        ai_request = EngineRequest(
            prompt=request.prompt,
            context=handoff_context,
            role=request.role,
            level=request.level,
            extras={**dict(request.extras), "handoff_from": "human"},
        )
        ai_response = ai_driver.run(ai_request)

        merged_text = _merge_partial_with_ai(partial, ai_response.text)
        human_bytes = len(partial.encode("utf-8"))
        ai_bytes = len(ai_response.text.encode("utf-8"))
        return EngineResponse(
            text=merged_text,
            source="mixed",
            attribution={
                "human": human_bytes,
                "ai": ai_bytes,
                **{k: v for k, v in ai_response.attribution.items() if k != "ai"},
            },
            handoff_to=self.handoff_to,
            handoff_trigger=trigger_kind,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stdout_surface(rendered: str) -> None:  # pragma: no cover - I/O glue
    sys.stdout.write("\n--- HUMAN ENGINE PAUSE ---\n")
    sys.stdout.write(rendered)
    sys.stdout.write("\n--- END PAYLOAD ---\n")
    sys.stdout.flush()


def _stdin_input_provider(  # pragma: no cover - I/O glue
    _rendered: str,
    *,
    handoff_trigger: Optional[Mapping[str, Any]],
    clock: Callable[[], float],
) -> tuple[str, Optional[str]]:
    """Default input provider: multi-line stdin until EOF.

    Honours ``on_keyword`` triggers; ``on_timer`` is best-effort here
    (a richer UI -- the Dream tab -- provides true async timing).
    """
    keyword = (handoff_trigger or {}).get("on_keyword")
    lines: list[str] = []
    sys.stdout.write("(end with Ctrl-D / Ctrl-Z, or type the handoff keyword)\n")
    sys.stdout.flush()
    try:
        for line in sys.stdin:
            if keyword and line.strip() == keyword:
                return "".join(lines), "keyword"
            lines.append(line)
    except KeyboardInterrupt:
        return "".join(lines), None
    return "".join(lines), None


def _build_handoff_context(
    upstream_context: str, partial: str, trigger_kind: str
) -> str:
    """Compose the context handed off to the AI driver.

    The AI sees the SAME upstream context the human saw, plus the
    human's partial response framed so the AI knows it's continuing
    a human-started task, not starting fresh.
    """
    parts: list[str] = []
    if upstream_context:
        parts.append(upstream_context)
    parts.append("=== HUMAN HANDOFF ===")
    parts.append(f"(trigger: {trigger_kind})")
    parts.append("The human engine produced this PARTIAL response.")
    parts.append("Continue from where they left off; do not restart.")
    parts.append("=== HUMAN PARTIAL ===")
    parts.append(partial)
    return "\n".join(parts)


def _merge_partial_with_ai(partial: str, ai_text: str) -> str:
    """Concatenate the human's partial with the AI's continuation.

    A single newline boundary is inserted iff the partial does not
    already end in whitespace. The AI's text is appended verbatim.
    """
    if not partial:
        return ai_text
    if partial.endswith(("\n", " ", "\t")):
        return partial + ai_text
    return partial + "\n" + ai_text


# ---------------------------------------------------------------------------
# Test helpers (also useful for the future Dream-tab integration)
# ---------------------------------------------------------------------------


def scripted_input_provider(
    response_text: str,
    *,
    trigger_kind: Optional[str] = None,
) -> InputProvider:
    """Return an input provider that yields a fixed response.

    Used by tests (AC-S14 DONE WHEN: unit test covers pause/resume +
    handoff path) and by the Dream tab when replaying recorded
    sessions.
    """

    def _provider(
        _rendered: str,
        *,
        handoff_trigger: Optional[Mapping[str, Any]] = None,  # noqa: ARG001
        clock: Callable[[], float] = time.monotonic,  # noqa: ARG001
    ) -> tuple[str, Optional[str]]:
        return response_text, trigger_kind

    return _provider


def keyword_aware_input_provider(
    lines: list[str],
) -> InputProvider:
    """Input provider that scans ``lines`` for the configured keyword.

    Emits the partial captured up to (but not including) the keyword
    line and reports trigger_kind="keyword". If no keyword fires,
    returns the concatenated lines with trigger_kind=None.
    """

    def _provider(
        _rendered: str,
        *,
        handoff_trigger: Optional[Mapping[str, Any]] = None,
        clock: Callable[[], float] = time.monotonic,  # noqa: ARG001
    ) -> tuple[str, Optional[str]]:
        keyword = (handoff_trigger or {}).get("on_keyword")
        collected: list[str] = []
        for line in lines:
            if keyword is not None and line.strip() == keyword:
                return "".join(collected), "keyword"
            collected.append(line)
        return "".join(collected), None

    return _provider


def timed_input_provider(
    *,
    text_before_timeout: str,
    work_time: float,
) -> InputProvider:
    """Input provider that simulates the timer firing mid-response.

    ``work_time`` is how long the user "worked" (as reported by the
    injected clock) before the timer trips. If it meets or exceeds
    ``handoff_trigger['on_timer']`` the provider returns
    ``(text_before_timeout, "timer")``.
    """

    def _provider(
        _rendered: str,
        *,
        handoff_trigger: Optional[Mapping[str, Any]] = None,
        clock: Callable[[], float] = time.monotonic,  # noqa: ARG001
    ) -> tuple[str, Optional[str]]:
        timeout = (handoff_trigger or {}).get("on_timer")
        if timeout is not None and work_time >= float(timeout):
            return text_before_timeout, "timer"
        return text_before_timeout, None

    return _provider
