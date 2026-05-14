"""HTTP surface for the AC-S15 observability stream.

Two endpoints, served by a stdlib ``http.server.ThreadingHTTPServer``
so we don't pull a web framework in for two routes:

* ``GET /api/observability/replay/<session_id>``
    -> ``application/json`` array of the session's events. 404 on
       unknown session.

* ``GET /api/observability/stream?session_id=<id>``
    -> ``text/event-stream``. Back-fills any persisted history, then
       tails the hub for new events until the session emits ``end``
       (or the client disconnects). One ``data: <json>\\n\\n`` frame
       per event.

Tests exercise the routing/handler functions directly without going
through a TCP socket; the same handler class is also runnable from
``serve()`` for a developer to point a browser at.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from scripts.observability import (
    KIND_END,
    ObservabilityHub,
    ObservabilityStore,
    StreamEvent,
    render_sse,
)

REPLAY_PREFIX = "/api/observability/replay/"
STREAM_PATH = "/api/observability/stream"


# ---------------------------------------------------------------------------
# Pure logic (testable without a socket)
# ---------------------------------------------------------------------------


def route_replay(
    store: ObservabilityStore, path: str
) -> Tuple[int, dict[str, str], bytes]:
    """Resolve a replay request to (status, headers, body).

    Pure function -- the HTTP handler is a thin shim on top of this.
    """
    if not path.startswith(REPLAY_PREFIX):
        return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"not found"
    session_id = path[len(REPLAY_PREFIX):]
    if not session_id or "/" in session_id:
        return (
            400,
            {"Content-Type": "text/plain; charset=utf-8"},
            b"bad session id",
        )
    if not store.exists(session_id):
        return (
            404,
            {"Content-Type": "text/plain; charset=utf-8"},
            f"no session {session_id}".encode("utf-8"),
        )
    events = [ev.to_dict() for ev in store.replay(session_id)]
    body = json.dumps(events, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return (
        200,
        {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store",
        },
        body,
    )


def iter_stream_frames(
    store: ObservabilityStore,
    hub: ObservabilityHub,
    session_id: str,
    *,
    timeout: Optional[float] = None,
    clock: Any = time.monotonic,
):
    """Yield raw SSE frames for a session.

    Behaviour:

    1. Replay every persisted event for ``session_id`` first. This
       lets a late subscriber see the full history without racing
       the recorder.
    2. Subscribe to the hub for new events emitted after we read the
       log. Yield each as an SSE frame.
    3. Stop when an ``end`` event is observed (live or replayed).
    4. ``timeout`` (seconds) caps how long we wait for new events
       when the session is mid-flight. ``None`` means wait forever
       (the default; the HTTP handler closes the socket when the
       client disconnects).

    This generator is the unit-testable core of the SSE endpoint.
    """
    # Subscribe FIRST so we don't miss any event that lands while we
    # back-fill the on-disk history. We de-dupe by `seq` below.
    q: "queue.Queue[StreamEvent]" = queue.Queue()
    seen_seq: set[int] = set()

    def _cb(ev: StreamEvent) -> None:
        q.put(ev)

    hub.subscribe(session_id, _cb)
    try:
        # Back-fill from disk.
        history: list[StreamEvent] = []
        if store.exists(session_id):
            history = store.replay(session_id)
        terminal_seen = False
        for ev in history:
            if ev.seq in seen_seq:
                continue
            seen_seq.add(ev.seq)
            yield render_sse(ev)
            if ev.kind == KIND_END:
                terminal_seen = True
        if terminal_seen:
            return

        # Tail live events.
        deadline: Optional[float]
        if timeout is None:
            deadline = None
        else:
            deadline = clock() + timeout
        while True:
            wait_for: Optional[float]
            if deadline is None:
                wait_for = None
            else:
                wait_for = max(0.0, deadline - clock())
                if wait_for == 0.0:
                    return
            try:
                ev = q.get(timeout=wait_for)
            except queue.Empty:
                return
            if ev.seq in seen_seq:
                continue
            seen_seq.add(ev.seq)
            yield render_sse(ev)
            if ev.kind == KIND_END:
                return
    finally:
        hub.unsubscribe(session_id, _cb)


# ---------------------------------------------------------------------------
# stdlib http.server glue
# ---------------------------------------------------------------------------


def make_handler(
    store: ObservabilityStore,
    hub: ObservabilityHub,
    *,
    stream_timeout: Optional[float] = None,
) -> type:
    """Build a BaseHTTPRequestHandler subclass bound to a given store + hub.

    Returned class is plug-compatible with ``ThreadingHTTPServer``.
    """

    class _Handler(BaseHTTPRequestHandler):
        # Silence stderr access-log noise in tests / dev.
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
            return

        def do_GET(self) -> None:  # noqa: N802 (stdlib API)
            split = urlsplit(self.path)
            if split.path.startswith(REPLAY_PREFIX):
                self._handle_replay(split.path)
                return
            if split.path == STREAM_PATH:
                params = parse_qs(split.query)
                session_ids = params.get("session_id") or []
                if not session_ids or not session_ids[0]:
                    self._send_text(400, "missing session_id")
                    return
                self._handle_stream(session_ids[0])
                return
            self._send_text(404, "not found")

        # ----- handlers -----
        def _handle_replay(self, path: str) -> None:
            status, headers, body = route_replay(store, path)
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_stream(self, session_id: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                for frame in iter_stream_frames(
                    store, hub, session_id, timeout=stream_timeout
                ):
                    try:
                        self.wfile.write(frame)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
            except Exception:  # noqa: BLE001
                # Best-effort; nothing more we can do over a broken stream.
                return

        # ----- helpers -----
        def _send_text(self, status: int, msg: str) -> None:
            body = msg.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _Handler


def serve(
    host: str,
    port: int,
    *,
    store: ObservabilityStore,
    hub: ObservabilityHub,
    stream_timeout: Optional[float] = None,
) -> ThreadingHTTPServer:
    """Start a ThreadingHTTPServer on (host, port) and return it.

    Caller is responsible for ``server.serve_forever()`` (typically
    on its own thread) and ``server.shutdown()`` on teardown.
    """
    handler = make_handler(store, hub, stream_timeout=stream_timeout)
    return ThreadingHTTPServer((host, port), handler)


__all__ = [
    "REPLAY_PREFIX",
    "STREAM_PATH",
    "iter_stream_frames",
    "make_handler",
    "route_replay",
    "serve",
]
