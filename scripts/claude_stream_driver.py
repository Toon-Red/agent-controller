"""Claude engine driver wired to the AC-S15 observability stream.

Claude is the most mature of the engine options we support, so per
AC-S15's DONE-WHEN it is the first driver to emit the full Stream
Event set during a real agent run.

This module is intentionally decoupled from the Anthropic SDK so we
can:

1. Land AC-S15 (the schema + persistence + transport) before
   AC-S10 ships the production HTTP/SSE client to Anthropic's API.
2. Unit-test the event emission deterministically by injecting a
   scripted stream source.

Production wiring
-----------------

The Anthropic Messages API streams events of the form
``message_start``, ``content_block_start``, ``content_block_delta``
(with delta types ``text_delta`` and ``thinking_delta``),
``tool_use`` blocks, ``message_delta``, ``message_stop``. A real
adapter constructs ``ClaudeStreamEvent`` instances from those API
events and feeds them to ``ClaudeStreamDriver`` -- the driver
translates them into the canonical AC-S15 Stream Event vocabulary
that downstream consumers (Dream UI, AC-S11 metrics, replay) speak.

The contract is *one ClaudeStreamEvent in, one or more StreamEvents
out*. The driver allocates ``seq`` + ``timestamp`` via the recorder
so live consumers and on-disk replay see identical events.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Optional

from scripts.engine_driver import BaseDriver, EngineRequest, EngineResponse
from scripts.observability import (
    ObservabilityHub,
    StreamRecorder,
    new_session_id,
)

# ---------------------------------------------------------------------------
# Internal protocol -- the shape the driver consumes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaudeStreamEvent:
    """One event from an upstream Claude streaming response.

    A real Anthropic-SDK adapter constructs these from
    ``message_start`` / ``content_block_*`` / ``message_stop`` API
    events. Tests construct them inline.

    ``kind`` is the upstream Claude vocabulary (not the AC-S15
    vocabulary):

    * ``thinking_delta``   -> reasoning_delta
    * ``text_delta``       -> output_delta
    * ``tool_use``         -> tool_call
    * ``tool_result``      -> tool_result
    * ``message_stop``     -> end (driver also emits this on the
                                   final natural flush)
    """

    kind: str
    text: Optional[str] = None
    call_id: Optional[str] = None
    tool_name: Optional[str] = None
    tool_arguments: Optional[dict[str, Any]] = None
    tool_ok: bool = True
    tool_result: Any = None
    tool_error: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)


# A stream source is any iterable of ClaudeStreamEvents. In production
# this is fed by the Anthropic SDK's streaming response; in tests, by
# a hand-rolled list.
ClaudeStreamSource = Callable[[EngineRequest], Iterable[ClaudeStreamEvent]]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class ClaudeStreamDriver(BaseDriver):
    """Claude engine driver that emits the full AC-S15 event set.

    Parameters
    ----------
    stream_source:
        Callable invoked with the EngineRequest, returning an iterable
        of ClaudeStreamEvents. The production adapter wraps the
        Anthropic SDK; tests pass a scripted list.
    hub:
        Optional ObservabilityHub. Live subscribers (the SSE
        endpoint, Dream tab) attach here. Persistence happens
        unconditionally via the StreamRecorder.
    root:
        Override the on-disk observability root. Mostly for tests.
    engine_id:
        Reported in the ``start`` event's ``engine`` field. Defaults
        to ``claude-sonnet``; AC-S10 will let callers configure
        per-role model selection.
    """

    engine_id = "claude"

    def __init__(
        self,
        stream_source: ClaudeStreamSource,
        *,
        hub: Optional[ObservabilityHub] = None,
        root: Optional[Any] = None,
        engine_id: str = "claude-sonnet",
    ) -> None:
        self._stream_source = stream_source
        self._hub = hub
        self._root = root
        self._configured_engine = engine_id

    # ------------------------------------------------------------------
    # Driver contract
    # ------------------------------------------------------------------

    def run(self, request: EngineRequest) -> EngineResponse:
        session_id = request.extras.get("session_id") or new_session_id()
        parent_session_id = request.extras.get("parent_session_id")

        output_parts: list[str] = []
        ai_byte_total = 0

        with StreamRecorder(
            session_id=session_id,
            agent_role=request.role,
            level=request.level,
            parent_session_id=parent_session_id,
            hub=self._hub,
            root=self._root,
        ) as rec:
            rec.start(
                prompt=request.prompt,
                context=request.context,
                engine=self._configured_engine,
                extras={k: v for k, v in dict(request.extras).items()
                        if k not in {"session_id", "parent_session_id"}},
            )

            try:
                for ev in self._stream_source(request):
                    self._translate(rec, ev, output_parts)
            except Exception as exc:  # noqa: BLE001
                # __exit__ would emit `end` with status=error, but
                # we want the error message attached too. Emit here
                # and re-raise so the caller sees the failure.
                rec.end(
                    status="error",
                    source="ai",
                    attribution={"ai": _utf8_len("".join(output_parts))},
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise

            final_text = "".join(output_parts)
            ai_byte_total = _utf8_len(final_text)
            # Idempotency guard: if the stream_source already emitted
            # a message_stop and the driver produced the `end` event,
            # don't double-emit.
            if not rec.closed:
                rec.end(
                    status="ok",
                    source="ai",
                    attribution={"ai": ai_byte_total},
                )

        return EngineResponse(
            text=final_text,
            source="ai",
            attribution={"ai": ai_byte_total},
        )

    # ------------------------------------------------------------------
    # Translation
    # ------------------------------------------------------------------

    def _translate(
        self,
        rec: StreamRecorder,
        ev: ClaudeStreamEvent,
        output_parts: list[str],
    ) -> None:
        if ev.kind == "thinking_delta":
            rec.reasoning_delta(text=ev.text or "")
        elif ev.kind == "text_delta":
            text = ev.text or ""
            output_parts.append(text)
            rec.output_delta(text=text)
        elif ev.kind == "tool_use":
            rec.tool_call(
                call_id=ev.call_id or "",
                name=ev.tool_name or "",
                arguments=ev.tool_arguments or {},
            )
        elif ev.kind == "tool_result":
            rec.tool_result(
                call_id=ev.call_id or "",
                ok=ev.tool_ok,
                result=ev.tool_result,
                error=ev.tool_error,
            )
        elif ev.kind == "message_stop":
            final_text = "".join(output_parts)
            rec.end(
                status="ok",
                source="ai",
                attribution={"ai": _utf8_len(final_text)},
            )
        else:
            # Unknown upstream event kinds are tolerated -- the
            # Anthropic API may add new event types and we don't want
            # an old driver to refuse to run. We attach them as
            # reasoning_delta with a sentinel prefix so debugging
            # consumers can see them.
            rec.reasoning_delta(text=f"[unknown claude event: {ev.kind}]")


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def scripted_claude_source(
    events: Iterable[ClaudeStreamEvent],
) -> ClaudeStreamSource:
    """Return a stream source that yields a fixed list of events.

    Used by ``tests/test_observability.py`` to drive the Claude
    driver deterministically.
    """

    materialised = list(events)

    def _source(_request: EngineRequest) -> Iterator[ClaudeStreamEvent]:
        yield from materialised

    return _source


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


__all__ = [
    "ClaudeStreamDriver",
    "ClaudeStreamEvent",
    "ClaudeStreamSource",
    "scripted_claude_source",
]
