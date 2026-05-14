"""Real-time agent-observability stream (AC-S15).

Every agent execution at every level (L4-L8) emits Stream Events
through this module. AC-S11 (per-level token telemetry) consumes the
persisted log rather than re-instrumenting drivers.

Public surface
--------------

* ``StreamEvent`` -- the wire/disk format (dataclass + JSON helpers).
* ``KIND_*``      -- canonical kind constants. ``KINDS`` is the set.
* ``StreamRecorder`` -- per-session emitter. Allocates ``seq``, stamps
                        ``timestamp``, fans out to live subscribers,
                        appends one JSON line to disk per event.
* ``ObservabilityHub``  -- pub/sub broker. Lets the SSE endpoint
                        subscribe to a session without coupling to
                        the recorder.
* ``ObservabilityStore`` -- replays a finished (or in-progress)
                        session from its on-disk JSONL log.

See ``docs/observability-stream-spec.md`` for the full schema spec.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

# ---------------------------------------------------------------------------
# Schema -- kinds + envelope
# ---------------------------------------------------------------------------

KIND_START = "start"
KIND_TOOL_CALL = "tool_call"
KIND_TOOL_RESULT = "tool_result"
KIND_REASONING_DELTA = "reasoning_delta"
KIND_OUTPUT_DELTA = "output_delta"
KIND_END = "end"

KINDS: frozenset[str] = frozenset(
    {
        KIND_START,
        KIND_TOOL_CALL,
        KIND_TOOL_RESULT,
        KIND_REASONING_DELTA,
        KIND_OUTPUT_DELTA,
        KIND_END,
    }
)

DEFAULT_OBSERVABILITY_ROOT = Path("data/observability")


class SchemaError(ValueError):
    """Raised when an event payload violates the schema."""


@dataclass(frozen=True)
class StreamEvent:
    """One observability event on the wire / on disk.

    See ``docs/observability-stream-spec.md`` for the field-by-field
    contract. The dataclass is frozen so events cannot be mutated
    after they've been broadcast to a subscriber -- live consumers
    and the replay log MUST observe the same bytes.
    """

    seq: int
    timestamp: float
    session_id: str
    parent_session_id: Optional[str]
    agent_role: Optional[str]
    level: Optional[str]
    kind: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        # Deterministic key order so the on-disk log is diff-friendly.
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StreamEvent":
        return cls(
            seq=int(raw["seq"]),
            timestamp=float(raw["timestamp"]),
            session_id=str(raw["session_id"]),
            parent_session_id=(
                None if raw.get("parent_session_id") is None else str(raw["parent_session_id"])
            ),
            agent_role=(None if raw.get("agent_role") is None else str(raw["agent_role"])),
            level=(None if raw.get("level") is None else str(raw["level"])),
            kind=str(raw["kind"]),
            data=dict(raw.get("data") or {}),
        )

    @classmethod
    def from_json(cls, line: str) -> "StreamEvent":
        return cls.from_dict(json.loads(line))


def validate_event_dict(raw: Any) -> None:
    """Validate a raw event dict against the schema.

    Raises ``SchemaError`` with a precise message on mismatch. Used by
    the unit suite to lock in schema-conformance.
    """
    if not isinstance(raw, dict):
        raise SchemaError(f"event must be a dict, got {type(raw).__name__}")
    required = {
        "seq": int,
        "timestamp": (int, float),
        "session_id": str,
        "kind": str,
    }
    for key, typ in required.items():
        if key not in raw:
            raise SchemaError(f"missing required field {key!r}")
        if not isinstance(raw[key], typ) or isinstance(raw[key], bool):
            raise SchemaError(
                f"field {key!r}: expected {typ}, got {type(raw[key]).__name__}"
            )
    for key in ("parent_session_id", "agent_role", "level"):
        if key not in raw:
            raise SchemaError(f"missing required (nullable) field {key!r}")
        if raw[key] is not None and not isinstance(raw[key], str):
            raise SchemaError(f"field {key!r}: expected str|null")
    if raw["kind"] not in KINDS:
        raise SchemaError(
            f"kind {raw['kind']!r} not one of {sorted(KINDS)}"
        )
    if "data" not in raw:
        raise SchemaError("missing required field 'data'")
    if not isinstance(raw["data"], dict):
        raise SchemaError("field 'data' must be an object")
    # Kind-specific minimum-fields guards. These are the contracts
    # downstream consumers rely on; keep them tight.
    data = raw["data"]
    kind = raw["kind"]
    if kind == KIND_START:
        for required_key in ("prompt",):
            if required_key not in data:
                raise SchemaError(f"start event missing data.{required_key}")
    elif kind == KIND_TOOL_CALL:
        for required_key in ("call_id", "name"):
            if required_key not in data:
                raise SchemaError(f"tool_call event missing data.{required_key}")
    elif kind == KIND_TOOL_RESULT:
        for required_key in ("call_id",):
            if required_key not in data:
                raise SchemaError(f"tool_result event missing data.{required_key}")
    elif kind in (KIND_REASONING_DELTA, KIND_OUTPUT_DELTA):
        if "text" not in data:
            raise SchemaError(f"{kind} event missing data.text")
    elif kind == KIND_END:
        if "status" not in data:
            raise SchemaError("end event missing data.status")


# ---------------------------------------------------------------------------
# Hub -- live pub/sub broker
# ---------------------------------------------------------------------------


Subscriber = Callable[[StreamEvent], None]


class ObservabilityHub:
    """Process-local pub/sub broker for live Stream Events.

    Each agent run gets one ``StreamRecorder``; the recorder publishes
    every event to the hub keyed by ``session_id``. The SSE endpoint
    (and any other live consumer) subscribes by session.

    Thread-safe: ``publish`` / ``subscribe`` / ``unsubscribe`` may be
    called concurrently. Subscribers MUST be fast -- the recorder
    blocks on them. A real UI subscriber should drop events into its
    own queue rather than processing inline.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subs: dict[str, list[Subscriber]] = {}

    def subscribe(self, session_id: str, callback: Subscriber) -> None:
        with self._lock:
            self._subs.setdefault(session_id, []).append(callback)

    def unsubscribe(self, session_id: str, callback: Subscriber) -> None:
        with self._lock:
            subs = self._subs.get(session_id)
            if not subs:
                return
            try:
                subs.remove(callback)
            except ValueError:
                pass
            if not subs:
                self._subs.pop(session_id, None)

    def publish(self, event: StreamEvent) -> None:
        with self._lock:
            subs = list(self._subs.get(event.session_id, ()))
        for cb in subs:
            try:
                cb(event)
            except Exception:  # noqa: BLE001
                # A buggy subscriber must not break the recorder.
                # In production we'd log; in tests this stays silent.
                continue

    def has_subscribers(self, session_id: str) -> bool:
        with self._lock:
            return bool(self._subs.get(session_id))


# ---------------------------------------------------------------------------
# Recorder -- per-session emitter
# ---------------------------------------------------------------------------


class StreamRecorder:
    """Allocates ``seq``, stamps ``timestamp``, persists to JSONL, fans
    out to the hub. One recorder per agent run.

    Use as a context manager so the underlying file handle is closed
    even if the agent raises:

        with StreamRecorder.open(session_id, agent_role="grader",
                                 level="L5", hub=hub) as rec:
            rec.start(prompt=..., context=...)
            rec.tool_call(call_id="t1", name="grep", arguments={...})
            rec.tool_result(call_id="t1", ok=True, result="...")
            rec.output_delta(text="...")
            rec.end(status="ok", source="ai", attribution={"ai": 42})
    """

    def __init__(
        self,
        session_id: str,
        *,
        agent_role: Optional[str] = None,
        level: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        root: Optional[Path] = None,
        hub: Optional[ObservabilityHub] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.session_id = session_id
        self.agent_role = agent_role
        self.level = level
        self.parent_session_id = parent_session_id
        self.hub = hub
        self._clock = clock
        self._seq = 0
        self._lock = threading.Lock()
        self._closed = False
        self.root = Path(root) if root is not None else _default_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / f"{session_id}.jsonl"
        # Line-buffered so live tailers see events as they're written.
        self._fp = self.path.open("a", encoding="utf-8", buffering=1)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        session_id: Optional[str] = None,
        **kwargs: Any,
    ) -> "StreamRecorder":
        return cls(session_id or new_session_id(), **kwargs)

    def __enter__(self) -> "StreamRecorder":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # If the caller errored without emitting `end`, surface the
        # error in the log so consumers don't deadlock waiting for a
        # terminal event that never arrives.
        if not self._closed:
            try:
                if exc is not None:
                    self.end(status="error", error=f"{exc_type.__name__}: {exc}")
                else:
                    # Caller forgot to emit end. Emit an "ok" terminal
                    # so consumers can tear down cleanly.
                    self.end(status="ok")
            finally:
                self.close()
        else:
            self.close()

    def close(self) -> None:
        with self._lock:
            if self._fp and not self._fp.closed:
                self._fp.flush()
                self._fp.close()

    # ------------------------------------------------------------------
    # Emission helpers (one per kind)
    # ------------------------------------------------------------------

    def emit(self, kind: str, **data: Any) -> StreamEvent:
        if kind not in KINDS:
            raise SchemaError(f"unknown kind {kind!r}")
        with self._lock:
            if self._closed:
                raise RuntimeError(
                    f"recorder for session {self.session_id!r} already closed"
                )
            event = StreamEvent(
                seq=self._seq,
                timestamp=self._clock(),
                session_id=self.session_id,
                parent_session_id=self.parent_session_id,
                agent_role=self.agent_role,
                level=self.level,
                kind=kind,
                data=dict(data),
            )
            # Validate before we persist or broadcast -- a bad payload
            # should fail fast at emission, not on replay.
            validate_event_dict(event.to_dict())
            self._fp.write(event.to_json() + "\n")
            self._fp.flush()
            self._seq += 1
            if kind == KIND_END:
                self._closed = True
        if self.hub is not None:
            self.hub.publish(event)
        return event

    def start(
        self,
        *,
        prompt: str,
        context: str = "",
        engine: Optional[str] = None,
        extras: Optional[dict[str, Any]] = None,
    ) -> StreamEvent:
        return self.emit(
            KIND_START,
            prompt=prompt,
            context=context,
            engine=engine,
            extras=dict(extras or {}),
        )

    def tool_call(
        self, *, call_id: str, name: str, arguments: Optional[dict[str, Any]] = None
    ) -> StreamEvent:
        return self.emit(
            KIND_TOOL_CALL,
            call_id=call_id,
            name=name,
            arguments=dict(arguments or {}),
        )

    def tool_result(
        self,
        *,
        call_id: str,
        ok: bool = True,
        result: Any = None,
        error: Optional[str] = None,
    ) -> StreamEvent:
        return self.emit(
            KIND_TOOL_RESULT,
            call_id=call_id,
            ok=ok,
            result=result,
            error=error,
        )

    def reasoning_delta(self, text: str) -> StreamEvent:
        return self.emit(KIND_REASONING_DELTA, text=text)

    def output_delta(self, text: str) -> StreamEvent:
        return self.emit(KIND_OUTPUT_DELTA, text=text)

    def end(
        self,
        *,
        status: str = "ok",
        source: str = "ai",
        attribution: Optional[dict[str, int]] = None,
        error: Optional[str] = None,
    ) -> StreamEvent:
        return self.emit(
            KIND_END,
            status=status,
            source=source,
            attribution=dict(attribution or {}),
            error=error,
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def seq(self) -> int:
        return self._seq


# ---------------------------------------------------------------------------
# Store -- replay from disk
# ---------------------------------------------------------------------------


class ObservabilityStore:
    """Read-only view of persisted session logs.

    Used by the ``/api/observability/replay/<session_id>`` endpoint
    and by the SSE endpoint to back-fill history before it starts
    tailing live events.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root is not None else _default_root()

    def path_for(self, session_id: str) -> Path:
        return self.root / f"{session_id}.jsonl"

    def exists(self, session_id: str) -> bool:
        return self.path_for(session_id).is_file()

    def replay(self, session_id: str) -> list[StreamEvent]:
        path = self.path_for(session_id)
        if not path.is_file():
            raise FileNotFoundError(f"no session log at {path}")
        events: list[StreamEvent] = []
        with path.open("r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                events.append(StreamEvent.from_json(line))
        return events

    def iter_sessions(self) -> Iterator[str]:
        if not self.root.is_dir():
            return iter(())
        return (p.stem for p in sorted(self.root.glob("*.jsonl")))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def new_session_id() -> str:
    """Generate a fresh session id. UUID4 hex, 32 chars, URL-safe."""
    return uuid.uuid4().hex


def _default_root() -> Path:
    """Resolve the on-disk root for session logs.

    Honour the ``OBSERVABILITY_ROOT`` env var so tests / Dream / a
    sandboxed daemon can redirect writes without touching the repo
    tree. Falls back to ``data/observability/`` under cwd.
    """
    env = os.environ.get("OBSERVABILITY_ROOT")
    if env:
        return Path(env)
    return Path.cwd() / DEFAULT_OBSERVABILITY_ROOT


def render_sse(event: StreamEvent) -> bytes:
    """Format an event for Server-Sent Events transport.

    Each frame is ``data: <json>\\n\\n`` per the SSE spec. The
    consumer (Dream tab) parses each frame back into a StreamEvent.
    """
    payload = event.to_json()
    return f"data: {payload}\n\n".encode("utf-8")


__all__ = [
    "KIND_START",
    "KIND_TOOL_CALL",
    "KIND_TOOL_RESULT",
    "KIND_REASONING_DELTA",
    "KIND_OUTPUT_DELTA",
    "KIND_END",
    "KINDS",
    "DEFAULT_OBSERVABILITY_ROOT",
    "ObservabilityHub",
    "ObservabilityStore",
    "SchemaError",
    "StreamEvent",
    "StreamRecorder",
    "new_session_id",
    "render_sse",
    "validate_event_dict",
]
