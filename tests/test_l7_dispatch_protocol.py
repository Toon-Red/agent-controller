"""AC-S16g tests for the L7 Dispatch protocol."""
from __future__ import annotations

import json
import sys
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import l7_dispatch_protocol as proto


# ---------------------------------------------------------------------------
# Fake opener helpers
# ---------------------------------------------------------------------------

class _OK:
    def __init__(self, body: bytes):
        self._body = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._body


def _ok_opener(body_dict):
    def _open(req, timeout=None):
        return _OK(json.dumps(body_dict).encode("utf-8"))
    return _open


def _http_error(code: int):
    def _open(req, timeout=None):
        raise urllib.error.HTTPError("http://x", code, "fail", {}, BytesIO(b""))
    return _open


# ---------------------------------------------------------------------------
# L7DispatchRequest -- shape + round-trip
# ---------------------------------------------------------------------------

class TestDispatchRequest:
    def test_minimal_fields_round_trip(self):
        r = proto.L7DispatchRequest(
            iteration_id="iter-1",
            pd_project_id="agent-controller",
            pd_task_id="20edaba7",
        )
        wire = r.to_wire()
        assert wire["iteration_id"] == "iter-1"
        assert wire["kind"] == "task_handoff"
        roundtrip = proto.L7DispatchRequest.from_wire(wire)
        assert roundtrip == r

    def test_full_fields_round_trip(self):
        r = proto.L7DispatchRequest(
            iteration_id="iter-2",
            pd_project_id="dream",
            pd_task_id="abc",
            claim_token="toon-red-dispatch-audit",
            engines={"L4": "ollama-local", "L8": "claude-opus"},
            goal_template_text="Process task abc",
            deadline_iso="2026-05-14T23:59:59",
            metadata={"caller": "L8"},
        )
        assert proto.L7DispatchRequest.from_wire(r.to_wire()) == r

    def test_missing_required_fields_rejected(self):
        with pytest.raises(proto.ProtocolError, match="missing required"):
            proto.L7DispatchRequest.from_wire({"iteration_id": "x"})

    def test_unknown_kind_rejected(self):
        with pytest.raises(proto.ProtocolError, match="kind"):
            proto.L7DispatchRequest.from_wire({
                "iteration_id": "x", "pd_project_id": "p",
                "pd_task_id": "t", "kind": "nuclear",
            })

    def test_non_mapping_payload_rejected(self):
        with pytest.raises(proto.ProtocolError, match="mapping"):
            proto.L7DispatchRequest.from_wire("not a dict")  # type: ignore

    def test_non_mapping_engines_rejected(self):
        with pytest.raises(proto.ProtocolError, match="engines"):
            proto.L7DispatchRequest.from_wire({
                "iteration_id": "x", "pd_project_id": "p",
                "pd_task_id": "t", "engines": "L4=ollama",
            })


# ---------------------------------------------------------------------------
# L7DispatchResponse -- shape + round-trip
# ---------------------------------------------------------------------------

class TestDispatchResponse:
    def test_accepted_round_trip(self):
        r = proto.L7DispatchResponse(
            iteration_id="iter-1", status="accepted",
            started_at="2026-05-14T18:00:00",
        )
        assert proto.L7DispatchResponse.from_wire(r.to_wire()) == r

    def test_rejected_with_message(self):
        r = proto.L7DispatchResponse(
            iteration_id="iter-1", status="rejected",
            message="L7 at capacity",
        )
        wire = r.to_wire()
        assert wire["message"] == "L7 at capacity"
        assert proto.L7DispatchResponse.from_wire(wire) == r

    def test_queued_status_accepted(self):
        r = proto.L7DispatchResponse(iteration_id="iter-1", status="queued")
        assert proto.L7DispatchResponse.from_wire(r.to_wire()).status == "queued"

    def test_unknown_status_rejected(self):
        with pytest.raises(proto.ProtocolError, match="status"):
            proto.L7DispatchResponse.from_wire({
                "iteration_id": "x", "status": "exploding",
            })

    def test_missing_iteration_id_rejected(self):
        with pytest.raises(proto.ProtocolError, match="iteration_id"):
            proto.L7DispatchResponse.from_wire({"status": "accepted"})


# ---------------------------------------------------------------------------
# L7DispatchClient -- happy + error paths
# ---------------------------------------------------------------------------

class TestDispatchClient:
    def _request(self):
        return proto.L7DispatchRequest(
            iteration_id="iter-1",
            pd_project_id="agent-controller",
            pd_task_id="20edaba7",
        )

    def test_dispatch_happy_path(self):
        response_body = {"iteration_id": "iter-1", "status": "accepted"}
        captured: list = []
        def opener(req, timeout=None):
            captured.append({
                "url": req.full_url,
                "data": json.loads(req.data),
            })
            return _OK(json.dumps(response_body).encode("utf-8"))
        client = proto.L7DispatchClient(opener=opener)
        out = client.dispatch(self._request())
        assert isinstance(out, proto.L7DispatchResponse)
        assert out.status == "accepted"
        # Request body matches the wire shape.
        assert captured[0]["data"]["pd_task_id"] == "20edaba7"
        assert captured[0]["url"] == proto.DEFAULT_L7_ENDPOINT

    def test_dispatch_custom_endpoint(self):
        captured: list = []
        def opener(req, timeout=None):
            captured.append(req.full_url)
            return _OK(json.dumps({"iteration_id": "iter-1",
                                     "status": "accepted"}).encode())
        client = proto.L7DispatchClient(
            endpoint="http://dispatch.local/api/dispatch", opener=opener,
        )
        client.dispatch(self._request())
        assert captured[0] == "http://dispatch.local/api/dispatch"

    def test_dispatch_http_error_returns_none(self):
        client = proto.L7DispatchClient(opener=_http_error(503))
        out = client.dispatch(self._request())
        assert out is None  # graceful degradation

    def test_dispatch_network_error_returns_none(self):
        def boom(req, timeout=None):
            raise ConnectionError("L7 dead")
        client = proto.L7DispatchClient(opener=boom)
        out = client.dispatch(self._request())
        assert out is None

    def test_dispatch_non_json_response_returns_none(self):
        def opener(req, timeout=None):
            return _OK(b"<!doctype html><body>oops</body>")
        client = proto.L7DispatchClient(opener=opener)
        out = client.dispatch(self._request())
        assert out is None

    def test_dispatch_malformed_response_returns_none(self):
        # JSON valid but missing required fields -> ProtocolError ->
        # client returns None.
        def opener(req, timeout=None):
            return _OK(json.dumps({"status": "accepted"}).encode())  # no iteration_id
        client = proto.L7DispatchClient(opener=opener)
        out = client.dispatch(self._request())
        assert out is None


# ---------------------------------------------------------------------------
# build_progress_message convenience
# ---------------------------------------------------------------------------

class TestBuildProgressMessage:
    def test_running_default(self):
        msg = proto.build_progress_message(
            iteration_id="iter-1",
            current_task={"id": "t1", "title": "Do X"},
        )
        assert msg.status == "running"
        assert msg.current_task["id"] == "t1"
        assert msg.blockers == ()
        assert msg.completed_this_iteration == ()

    def test_blocked_with_blockers(self):
        msg = proto.build_progress_message(
            iteration_id="iter-1", current_task={"id": "t1"},
            status="blocked", blockers=("waiting on Preston",),
        )
        assert msg.status == "blocked"
        assert msg.blockers == ("waiting on Preston",)

    def test_done_with_completed(self):
        msg = proto.build_progress_message(
            iteration_id="iter-1", current_task={"id": "t1"},
            status="done", completed_this_iteration=("t1",),
        )
        assert msg.status == "done"
        assert msg.completed_this_iteration == ("t1",)

    def test_unknown_status_rejected(self):
        with pytest.raises(proto.ProtocolError, match="status"):
            proto.build_progress_message(
                iteration_id="iter-1", current_task={"id": "t1"},
                status="exploding",
            )


# ---------------------------------------------------------------------------
# Re-export check: L7ProgressMessage importable from this module
# ---------------------------------------------------------------------------

def test_l7_progress_message_reexported():
    from scripts.l7_dispatch_protocol import L7ProgressMessage as ReExport
    from scripts.l8_project_manager import L7ProgressMessage as Original
    assert ReExport is Original


# ---------------------------------------------------------------------------
# JSON wire stability: snapshot a known good payload
# ---------------------------------------------------------------------------

def test_wire_shape_stable():
    """Pin the wire shape so a future refactor that drops / renames a
    field surfaces in CI rather than silently breaking L7."""
    r = proto.L7DispatchRequest(
        iteration_id="iter-1",
        pd_project_id="agent-controller",
        pd_task_id="20edaba7",
    )
    wire = r.to_wire()
    assert set(wire.keys()) == {
        "iteration_id", "kind", "pd_project_id", "pd_task_id",
        "claim_token", "engines", "goal_template_text",
        "deadline_iso", "metadata",
    }
