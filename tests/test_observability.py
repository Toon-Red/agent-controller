"""Unit tests for AC-S15 -- real-time agent observability stream.

Coverage (matches the DONE-WHEN in the AC-S15 brief):

* Schema conformance: every event the recorder emits passes
  ``validate_event_dict`` and uses one of the six canonical kinds.
* Replay round-trip: events written via StreamRecorder.emit() and
  re-read via ObservabilityStore.replay() are byte-for-byte equal.
* Claude driver emits the full event set during a representative
  streaming run.
* /api/observability/replay/<id> returns the full log as JSON.
* /api/observability/stream back-fills history then tails live events
  and stops on `end`.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from scripts.claude_stream_driver import (
    ClaudeStreamDriver,
    ClaudeStreamEvent,
    scripted_claude_source,
)
from scripts.engine_driver import EngineRequest
from scripts.observability import (
    KIND_END,
    KIND_OUTPUT_DELTA,
    KIND_REASONING_DELTA,
    KIND_START,
    KIND_TOOL_CALL,
    KIND_TOOL_RESULT,
    KINDS,
    ObservabilityHub,
    ObservabilityStore,
    SchemaError,
    StreamEvent,
    StreamRecorder,
    new_session_id,
    render_sse,
    validate_event_dict,
)
from scripts.observability_api import (
    REPLAY_PREFIX,
    iter_stream_frames,
    route_replay,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_clock_factory(start: float = 1_700_000_000.0, step: float = 0.001):
    """Return a deterministic clock for replay-stable timestamps."""
    state = {"t": start}

    def _now() -> float:
        t = state["t"]
        state["t"] += step
        return t

    return _now


# ---------------------------------------------------------------------------
# Schema conformance
# ---------------------------------------------------------------------------


def test_kinds_set_is_exactly_six_canonical():
    assert KINDS == {
        KIND_START,
        KIND_TOOL_CALL,
        KIND_TOOL_RESULT,
        KIND_REASONING_DELTA,
        KIND_OUTPUT_DELTA,
        KIND_END,
    }


def test_validate_event_accepts_well_formed_event():
    raw = {
        "seq": 0,
        "timestamp": 1.0,
        "session_id": "s",
        "parent_session_id": None,
        "agent_role": "grader",
        "level": "L5",
        "kind": KIND_START,
        "data": {"prompt": "p"},
    }
    validate_event_dict(raw)  # no raise


@pytest.mark.parametrize(
    "mutation, message",
    [
        ({"kind": "bogus"}, "not one of"),
        ({"seq": "0"}, "expected"),
        ({"data": "not-a-dict"}, "must be an object"),
        ({"data": {}}, "start event missing data.prompt"),
    ],
)
def test_validate_event_rejects_malformed(mutation, message):
    raw = {
        "seq": 0,
        "timestamp": 1.0,
        "session_id": "s",
        "parent_session_id": None,
        "agent_role": None,
        "level": None,
        "kind": KIND_START,
        "data": {"prompt": "p"},
    }
    raw.update(mutation)
    with pytest.raises(SchemaError, match=message):
        validate_event_dict(raw)


def test_validate_event_rejects_missing_required_fields():
    with pytest.raises(SchemaError, match="missing required field 'seq'"):
        validate_event_dict(
            {
                "timestamp": 1.0,
                "session_id": "s",
                "parent_session_id": None,
                "agent_role": None,
                "level": None,
                "kind": KIND_START,
                "data": {"prompt": "p"},
            }
        )


def test_validate_event_rejects_tool_call_without_call_id():
    with pytest.raises(SchemaError, match="tool_call event missing data.call_id"):
        validate_event_dict(
            {
                "seq": 1,
                "timestamp": 1.0,
                "session_id": "s",
                "parent_session_id": None,
                "agent_role": None,
                "level": None,
                "kind": KIND_TOOL_CALL,
                "data": {"name": "grep"},
            }
        )


def test_validate_event_rejects_unknown_kind():
    with pytest.raises(SchemaError):
        validate_event_dict(
            {
                "seq": 0,
                "timestamp": 1.0,
                "session_id": "s",
                "parent_session_id": None,
                "agent_role": None,
                "level": None,
                "kind": "made_up_kind",
                "data": {},
            }
        )


# ---------------------------------------------------------------------------
# Recorder behaviour
# ---------------------------------------------------------------------------


def test_recorder_assigns_monotonic_seq_and_persists_jsonl(tmp_path):
    session_id = "session-A"
    with StreamRecorder(
        session_id=session_id,
        agent_role="grader",
        level="L5",
        root=tmp_path,
        clock=_fake_clock_factory(),
    ) as rec:
        rec.start(prompt="Grade this PR.", context="diff bytes")
        rec.tool_call(call_id="t1", name="grep", arguments={"q": "TODO"})
        rec.tool_result(call_id="t1", ok=True, result="3 hits")
        rec.reasoning_delta("thinking...")
        rec.output_delta("Score: ")
        rec.output_delta("8/10")
        rec.end(status="ok", source="ai", attribution={"ai": 8})

    log = (tmp_path / f"{session_id}.jsonl").read_text(encoding="utf-8")
    lines = [json.loads(ln) for ln in log.strip().splitlines()]
    assert [e["seq"] for e in lines] == list(range(len(lines)))
    assert [e["kind"] for e in lines] == [
        KIND_START,
        KIND_TOOL_CALL,
        KIND_TOOL_RESULT,
        KIND_REASONING_DELTA,
        KIND_OUTPUT_DELTA,
        KIND_OUTPUT_DELTA,
        KIND_END,
    ]
    # Every persisted line passes the schema validator.
    for raw in lines:
        validate_event_dict(raw)


def test_recorder_replay_roundtrip_byte_for_byte(tmp_path):
    """DONE-WHEN: a unit test covers schema-conformance + replay round-trip."""
    session_id = "roundtrip"
    rec = StreamRecorder(
        session_id=session_id,
        agent_role="coder",
        level="L4",
        parent_session_id="parent-x",
        root=tmp_path,
        clock=_fake_clock_factory(),
    )
    emitted: list[StreamEvent] = []
    try:
        emitted.append(rec.start(prompt="p", context="c", engine="claude-sonnet"))
        emitted.append(rec.tool_call(call_id="c1", name="ls", arguments={"d": "/tmp"}))
        emitted.append(rec.tool_result(call_id="c1", ok=True, result=["a", "b"]))
        emitted.append(rec.reasoning_delta("hmm"))
        emitted.append(rec.output_delta("hello "))
        emitted.append(rec.output_delta("world"))
        emitted.append(rec.end(status="ok", source="ai", attribution={"ai": 11}))
    finally:
        rec.close()

    store = ObservabilityStore(root=tmp_path)
    replayed = store.replay(session_id)

    assert len(replayed) == len(emitted)
    for original, again in zip(emitted, replayed):
        assert original == again, (original, again)
    # And the JSON form matches too -- the disk really is the
    # authoritative record.
    for original, again in zip(emitted, replayed):
        assert original.to_json() == again.to_json()


def test_recorder_rejects_emission_after_end(tmp_path):
    rec = StreamRecorder(session_id="x", root=tmp_path)
    rec.start(prompt="p")
    rec.end(status="ok")
    with pytest.raises(RuntimeError, match="already closed"):
        rec.output_delta("late")
    rec.close()


def test_recorder_context_manager_emits_end_on_exception(tmp_path):
    session_id = "boom"
    with pytest.raises(ValueError):
        with StreamRecorder(session_id=session_id, root=tmp_path) as rec:
            rec.start(prompt="p")
            raise ValueError("boom")

    events = ObservabilityStore(root=tmp_path).replay(session_id)
    assert events[-1].kind == KIND_END
    assert events[-1].data["status"] == "error"
    assert "ValueError" in (events[-1].data.get("error") or "")


def test_recorder_context_manager_emits_end_on_clean_exit_if_missing(tmp_path):
    session_id = "forgot"
    with StreamRecorder(session_id=session_id, root=tmp_path) as rec:
        rec.start(prompt="p")
        rec.output_delta("x")
        # NB: deliberately no rec.end(...)
    events = ObservabilityStore(root=tmp_path).replay(session_id)
    assert events[-1].kind == KIND_END
    assert events[-1].data["status"] == "ok"


def test_unknown_kind_rejected_at_emit(tmp_path):
    rec = StreamRecorder(session_id="x", root=tmp_path)
    with pytest.raises(SchemaError):
        rec.emit("invented_kind")
    rec.close()


# ---------------------------------------------------------------------------
# Hub pub/sub
# ---------------------------------------------------------------------------


def test_hub_publishes_only_to_matching_session(tmp_path):
    hub = ObservabilityHub()
    received_a: list[StreamEvent] = []
    received_b: list[StreamEvent] = []
    hub.subscribe("A", received_a.append)
    hub.subscribe("B", received_b.append)

    with StreamRecorder(session_id="A", root=tmp_path, hub=hub) as rec_a:
        rec_a.start(prompt="pA")
        rec_a.output_delta("ax")
        rec_a.end(status="ok")
    with StreamRecorder(session_id="B", root=tmp_path, hub=hub) as rec_b:
        rec_b.start(prompt="pB")
        rec_b.end(status="ok")

    assert all(ev.session_id == "A" for ev in received_a)
    assert all(ev.session_id == "B" for ev in received_b)
    assert len(received_a) >= 3
    assert len(received_b) >= 2


def test_hub_tolerates_buggy_subscriber(tmp_path):
    hub = ObservabilityHub()
    good: list[StreamEvent] = []

    def _boom(_ev: StreamEvent) -> None:
        raise RuntimeError("subscriber crashed")

    hub.subscribe("S", _boom)
    hub.subscribe("S", good.append)

    with StreamRecorder(session_id="S", root=tmp_path, hub=hub) as rec:
        rec.start(prompt="p")
        rec.end(status="ok")

    # The healthy subscriber still got both events.
    assert len(good) == 2


def test_hub_unsubscribe_removes_callback(tmp_path):
    hub = ObservabilityHub()
    received: list[StreamEvent] = []
    cb = received.append
    hub.subscribe("S", cb)
    hub.unsubscribe("S", cb)
    with StreamRecorder(session_id="S", root=tmp_path, hub=hub) as rec:
        rec.start(prompt="p")
        rec.end(status="ok")
    assert received == []


# ---------------------------------------------------------------------------
# Claude driver emits the full event set
# ---------------------------------------------------------------------------


def test_claude_driver_emits_full_event_set(tmp_path):
    """DONE-WHEN: one engine driver (Claude) emits the full event set
    during a real agent run."""
    transcript = [
        ClaudeStreamEvent(kind="thinking_delta", text="reading diff..."),
        ClaudeStreamEvent(
            kind="tool_use",
            call_id="call-1",
            tool_name="grep",
            tool_arguments={"q": "TODO"},
        ),
        ClaudeStreamEvent(
            kind="tool_result",
            call_id="call-1",
            tool_ok=True,
            tool_result="found 3 TODOs",
        ),
        ClaudeStreamEvent(kind="thinking_delta", text="scoring..."),
        ClaudeStreamEvent(kind="text_delta", text="Score: "),
        ClaudeStreamEvent(kind="text_delta", text="8/10"),
        ClaudeStreamEvent(kind="message_stop"),
    ]
    hub = ObservabilityHub()
    received: list[StreamEvent] = []
    session_id = "claude-run-1"
    hub.subscribe(session_id, received.append)

    driver = ClaudeStreamDriver(
        scripted_claude_source(transcript),
        hub=hub,
        root=tmp_path,
    )
    response = driver.run(
        EngineRequest(
            prompt="Grade this PR.",
            context="diff: +foo",
            role="grader",
            level="L5",
            extras={"session_id": session_id},
        )
    )

    # Final response text was reconstructed from output_deltas.
    assert response.text == "Score: 8/10"
    assert response.source == "ai"
    assert response.attribution == {"ai": len("Score: 8/10".encode("utf-8"))}

    # All six event kinds were emitted at least once.
    emitted_kinds = {ev.kind for ev in received}
    assert emitted_kinds == KINDS, (
        f"Claude driver must emit every canonical kind; "
        f"missing: {KINDS - emitted_kinds}"
    )

    # Disk log matches the live stream.
    persisted = ObservabilityStore(root=tmp_path).replay(session_id)
    assert [e.to_dict() for e in persisted] == [e.to_dict() for e in received]
    # Exactly one terminal event, and it is last.
    end_events = [e for e in persisted if e.kind == KIND_END]
    assert len(end_events) == 1
    assert persisted[-1].kind == KIND_END

    # Tool-call <-> tool-result pairing by call_id.
    tcs = [e for e in persisted if e.kind == KIND_TOOL_CALL]
    trs = [e for e in persisted if e.kind == KIND_TOOL_RESULT]
    assert {e.data["call_id"] for e in tcs} == {e.data["call_id"] for e in trs}


def test_claude_driver_records_error_on_source_exception(tmp_path):
    def _bad_source(_req):
        yield ClaudeStreamEvent(kind="text_delta", text="partial...")
        raise RuntimeError("upstream collapsed")

    session_id = "claude-error"
    driver = ClaudeStreamDriver(_bad_source, root=tmp_path)
    with pytest.raises(RuntimeError, match="upstream collapsed"):
        driver.run(
            EngineRequest(
                prompt="p",
                role="coder",
                level="L4",
                extras={"session_id": session_id},
            )
        )

    events = ObservabilityStore(root=tmp_path).replay(session_id)
    assert events[-1].kind == KIND_END
    assert events[-1].data["status"] == "error"
    assert "RuntimeError" in events[-1].data["error"]


# ---------------------------------------------------------------------------
# HTTP surface (no socket required)
# ---------------------------------------------------------------------------


def test_route_replay_returns_full_event_list(tmp_path):
    session_id = "replay-1"
    with StreamRecorder(session_id=session_id, root=tmp_path) as rec:
        rec.start(prompt="p", context="c")
        rec.output_delta("o")
        rec.end(status="ok", source="ai", attribution={"ai": 1})

    store = ObservabilityStore(root=tmp_path)
    status, headers, body = route_replay(store, REPLAY_PREFIX + session_id)

    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    payload = json.loads(body.decode("utf-8"))
    assert isinstance(payload, list)
    assert [e["kind"] for e in payload] == [KIND_START, KIND_OUTPUT_DELTA, KIND_END]


def test_route_replay_404_for_unknown_session(tmp_path):
    store = ObservabilityStore(root=tmp_path)
    status, _, body = route_replay(store, REPLAY_PREFIX + "does-not-exist")
    assert status == 404
    assert b"no session" in body


def test_route_replay_400_on_path_traversal(tmp_path):
    store = ObservabilityStore(root=tmp_path)
    status, _, _ = route_replay(store, REPLAY_PREFIX + "a/b")
    assert status == 400


def test_iter_stream_frames_replays_history_then_stops_on_end(tmp_path):
    session_id = "stream-history"
    with StreamRecorder(session_id=session_id, root=tmp_path) as rec:
        rec.start(prompt="p")
        rec.output_delta("hi")
        rec.end(status="ok")

    store = ObservabilityStore(root=tmp_path)
    hub = ObservabilityHub()

    frames = list(iter_stream_frames(store, hub, session_id, timeout=0.5))
    assert len(frames) == 3
    parsed = [
        json.loads(frame.removeprefix(b"data: ").removesuffix(b"\n\n"))
        for frame in frames
    ]
    assert [p["kind"] for p in parsed] == [KIND_START, KIND_OUTPUT_DELTA, KIND_END]


def test_iter_stream_frames_back_fills_and_tails_live(tmp_path):
    """Late subscriber sees history first, then catches new live events."""
    session_id = "stream-live"
    hub = ObservabilityHub()
    rec = StreamRecorder(session_id=session_id, root=tmp_path, hub=hub)
    rec.start(prompt="p")  # already persisted before consumer subscribes

    store = ObservabilityStore(root=tmp_path)
    collected: list[bytes] = []

    def _drive() -> None:
        for frame in iter_stream_frames(store, hub, session_id, timeout=2.0):
            collected.append(frame)

    t = threading.Thread(target=_drive)
    t.start()

    # Wait briefly to let the subscriber attach + drain history.
    import time as _t

    _t.sleep(0.1)

    rec.output_delta("late-1")
    rec.output_delta("late-2")
    rec.end(status="ok")

    t.join(timeout=3.0)
    rec.close()

    assert not t.is_alive(), "stream generator should stop on end event"
    parsed = [
        json.loads(f.removeprefix(b"data: ").removesuffix(b"\n\n"))
        for f in collected
    ]
    kinds = [p["kind"] for p in parsed]
    # We expect: start (history) + output_delta x2 + end.
    assert kinds == [KIND_START, KIND_OUTPUT_DELTA, KIND_OUTPUT_DELTA, KIND_END]
    # And the seq numbers are unique / monotonic across the merged
    # history+live stream.
    seqs = [p["seq"] for p in parsed]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def test_render_sse_format():
    ev = StreamEvent(
        seq=0,
        timestamp=1.0,
        session_id="s",
        parent_session_id=None,
        agent_role=None,
        level=None,
        kind=KIND_START,
        data={"prompt": "p"},
    )
    out = render_sse(ev)
    assert out.startswith(b"data: ")
    assert out.endswith(b"\n\n")
    inner = out[len(b"data: "):-2]
    parsed = json.loads(inner)
    assert parsed["kind"] == KIND_START


def test_new_session_id_unique():
    ids = {new_session_id() for _ in range(100)}
    assert len(ids) == 100


# ---------------------------------------------------------------------------
# Spec doc sanity
# ---------------------------------------------------------------------------


def test_spec_doc_lists_every_kind():
    spec = (
        Path(__file__).resolve().parent.parent
        / "docs"
        / "observability-stream-spec.md"
    ).read_text(encoding="utf-8")
    for kind in KINDS:
        assert f"`{kind}`" in spec, f"spec must document the {kind!r} kind"
