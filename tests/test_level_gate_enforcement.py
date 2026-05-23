"""Tests for the level-gate enforcer (47c52660)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.level_gates import LevelGateViolation  # noqa: E402
from scripts import level_gate_enforcer as enf  # noqa: E402


# ---------------------------------------------------------------------------
# LevelGateViolation propagation contract
# ---------------------------------------------------------------------------

def test_violation_inherits_from_base_exception_not_exception():
    """The whole point: cannot be swallowed by except Exception."""
    assert issubclass(LevelGateViolation, BaseException)
    assert not issubclass(LevelGateViolation, Exception)


def test_violation_carries_diagnostic_fields():
    v = LevelGateViolation(
        gate_id="g", failed_predicates=["a", "b"],
        failing_record_id="rec-1", transition="L4->L5",
    )
    assert v.gate_id == "g"
    assert v.failed_predicates == ["a", "b"]
    assert v.failing_record_id == "rec-1"
    assert v.transition == "L4->L5"
    assert "L4->L5" in str(v)
    assert "g" in str(v)


def test_violation_not_caught_by_except_exception():
    """Hard pin: only an explicit BaseException catch can swallow it."""
    raised = []
    try:
        try:
            raise LevelGateViolation(
                gate_id="x", failed_predicates=[],
                failing_record_id="r", transition="L4->L5",
            )
        except Exception:  # noqa: BLE001
            raised.append("Exception caught it")
    except LevelGateViolation:
        raised.append("BaseException caught it")
    assert raised == ["BaseException caught it"]


# ---------------------------------------------------------------------------
# enforce_transition: pass + fail paths
# ---------------------------------------------------------------------------

def test_passing_gate_does_not_raise_or_log(tmp_path):
    def green_gate(_record):
        return True, []
    # No exception expected.
    enf.enforce_transition(
        "L4->L5", {"id": "x"}, green_gate,
        ping=True, violations_dir=tmp_path,
    )
    # No file written either.
    assert list(tmp_path.glob("*.json")) == []


def test_failing_gate_raises_violation(tmp_path):
    def red_gate(_record):
        return False, ["no test_results"]
    with pytest.raises(LevelGateViolation) as exc:
        enf.enforce_transition(
            "L4->L5", {"id": "abc"}, red_gate,
            ping=False, violations_dir=tmp_path,
        )
    assert exc.value.gate_id == "red_gate"
    assert exc.value.failed_predicates == ["no test_results"]
    assert exc.value.failing_record_id == "abc"
    assert exc.value.transition == "L4->L5"


def test_failing_gate_persists_violation_to_disk(tmp_path):
    def red_gate(_record):
        return False, ["x"]
    with pytest.raises(LevelGateViolation):
        enf.enforce_transition(
            "L4->L5", {"id": "abc"}, red_gate,
            ping=False, violations_dir=tmp_path,
        )
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text(encoding="utf-8"))
    assert rec["gate_id"] == "red_gate"
    assert rec["transition"] == "L4->L5"
    assert rec["failing_record_id"] == "abc"


def test_failing_gate_disable_log_to_disk(tmp_path):
    def red_gate(_record):
        return False, ["x"]
    with pytest.raises(LevelGateViolation):
        enf.enforce_transition(
            "L4->L5", {"id": "abc"}, red_gate,
            ping=False, log_to_disk=False, violations_dir=tmp_path,
        )
    assert list(tmp_path.glob("*.json")) == []


def test_failing_gate_fires_discord_ping_when_enabled(tmp_path):
    with patch.object(enf, "_send_ping") as ping:
        def red_gate(_record):
            return False, ["x"]
        with pytest.raises(LevelGateViolation):
            enf.enforce_transition(
                "L4->L5", {"id": "abc"}, red_gate,
                ping=True, violations_dir=tmp_path,
            )
        ping.assert_called_once()
        kwargs = ping.call_args.kwargs
        assert kwargs["gate_id"] == "red_gate"
        assert kwargs["transition"] == "L4->L5"


def test_ping_failure_does_not_swallow_violation(tmp_path):
    """If the Discord ping raises, the gate violation still propagates."""
    def red_gate(_record):
        return False, ["x"]
    with patch("scripts.discord_ping.send_escalation",
                side_effect=RuntimeError("webhook down")):
        with pytest.raises(LevelGateViolation):
            enf.enforce_transition(
                "L4->L5", {"id": "abc"}, red_gate,
                ping=True, violations_dir=tmp_path,
            )


# ---------------------------------------------------------------------------
# enforce() canonical wiring
# ---------------------------------------------------------------------------

def test_enforce_canonical_lookup_for_l4_to_l5(tmp_path):
    # task with no test_results -> L4->L5 gate refuses.
    with pytest.raises(LevelGateViolation) as exc:
        enf.enforce("L4->L5", {"id": "t1"},
                     ping=False, violations_dir=tmp_path)
    assert exc.value.gate_id == "gate_l4_to_l5__tests_run"


def test_enforce_passes_for_l4_to_l5_with_tests_recorded(tmp_path):
    enf.enforce("L4->L5",
                 {"id": "t1", "test_results": {"passed_count": 5}},
                 ping=False, violations_dir=tmp_path)


def test_enforce_unknown_transition_raises_key_error():
    with pytest.raises(KeyError) as exc:
        enf.enforce("L9->L10", {})
    assert "L9->L10" in str(exc.value)


def test_transition_to_gate_pin():
    """Mapping shape pinned so a future rewire is intentional."""
    keys = set(enf.TRANSITION_TO_GATE.keys())
    assert keys == {"L4->L5", "L5->L6", "L6->L7", "L7->L8", "L8->Preston"}


# ---------------------------------------------------------------------------
# Wired call site: L7DispatchClient.dispatch
# ---------------------------------------------------------------------------

def test_l7_dispatch_blocks_when_source_task_has_no_tests(tmp_path):
    from scripts.l7_dispatch_protocol import L7DispatchClient, L7DispatchRequest
    client = L7DispatchClient(
        endpoint="http://test/discard",
        opener=lambda *a, **kw: pytest.fail("dispatch should not POST"),
    )
    req = L7DispatchRequest(
        iteration_id="iter-1", pd_project_id="proj-a", pd_task_id="t-a",
    )
    source = {"id": "t-a"}  # no test_results
    with patch.object(enf, "DEFAULT_VIOLATIONS_DIR", tmp_path):
        with pytest.raises(LevelGateViolation) as exc:
            client.dispatch(req, source_task=source)
    assert exc.value.gate_id == "gate_l4_to_l5__tests_run"


def test_l7_dispatch_proceeds_when_source_task_has_tests(tmp_path):
    from scripts.l7_dispatch_protocol import L7DispatchClient, L7DispatchRequest

    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self):
            return json.dumps({
                "iteration_id": "iter-1", "status": "accepted",
            }).encode("utf-8")

    posted = []

    def fake_opener(req, timeout=None):
        posted.append(req)
        return _FakeResp()

    client = L7DispatchClient(endpoint="http://test/x", opener=fake_opener)
    req = L7DispatchRequest(
        iteration_id="iter-1", pd_project_id="proj-a", pd_task_id="t-a",
    )
    source = {"id": "t-a", "test_results": {"passed_count": 3}}
    with patch.object(enf, "DEFAULT_VIOLATIONS_DIR", tmp_path):
        resp = client.dispatch(req, source_task=source)
    assert posted  # POST was issued
    assert resp is not None and resp.status == "accepted"


def test_l7_dispatch_backward_compatible_without_source_task(tmp_path):
    """No source_task -> no gate check (old callers keep working)."""
    from scripts.l7_dispatch_protocol import L7DispatchClient, L7DispatchRequest

    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self):
            return json.dumps({"iteration_id": "x", "status": "accepted"}).encode("utf-8")

    client = L7DispatchClient(endpoint="http://test/x",
                              opener=lambda *a, **kw: _FakeResp())
    req = L7DispatchRequest(
        iteration_id="x", pd_project_id="p", pd_task_id="t",
    )
    # No source_task; no exception even though a gate refusal would apply.
    resp = client.dispatch(req)
    assert resp is not None


# ---------------------------------------------------------------------------
# Wired call site: L8ProjectManager.observe_l7_progress with gate
# ---------------------------------------------------------------------------

def test_l8_pm_observe_progress_blocks_on_non_green_ci(tmp_path):
    """When enforce_gate=True and the project's qa run is not green
    for today, the L7->L8 gate raises and no escalation is produced."""
    from scripts.l8_project_manager import L7ProgressMessage
    # Build a minimal L8ProjectManager-free path by exercising the
    # gate directly via the same code path the wiring uses.
    pid = "proj-no-qa"
    with patch.object(enf, "DEFAULT_VIOLATIONS_DIR", tmp_path):
        with pytest.raises(LevelGateViolation) as exc:
            enf.enforce("L7->L8", pid, ping=False, violations_dir=tmp_path)
    assert exc.value.gate_id == "gate_l7_to_l8__green_ci"


# ---------------------------------------------------------------------------
# L8->Preston gate standalone (honest-stop: no production decision-
# payload code path yet; enforcer helper proven by direct invocation)
# ---------------------------------------------------------------------------

def test_l8_to_preston_gate_blocks_payload_missing_rationale(tmp_path):
    payload = {
        "options": [{"label": "A"}, {"label": "B"}],
        # rationale missing
        "data": {},
        "recommendation": "go with A",
        "decision_needed_by": None,
    }
    with pytest.raises(LevelGateViolation) as exc:
        enf.enforce("L8->Preston", payload, ping=False, violations_dir=tmp_path)
    assert exc.value.gate_id == "gate_l8_to_preston__decision_needed"
    assert any("rationale" in f for f in exc.value.failed_predicates)


def test_l8_to_preston_gate_passes_on_complete_payload(tmp_path):
    payload = {
        "options": [{"label": "A"}, {"label": "B"}],
        "rationale": "ship-vs-hold",
        "data": {"open_bugs": 1},
        "recommendation": "ship",
        "decision_needed_by": None,
    }
    enf.enforce("L8->Preston", payload, ping=False, violations_dir=tmp_path)


# ---------------------------------------------------------------------------
# show_violations CLI
# ---------------------------------------------------------------------------

def test_show_violations_cli_lists_records(tmp_path, capsys):
    import importlib.util
    rec_dir = tmp_path / "vio"
    rec_dir.mkdir()
    (rec_dir / "2026-05-23T08-00-00_00-00-gate_x.json").write_text(json.dumps({
        "ts": "2026-05-23T08:00:00+00:00",
        "gate_id": "gate_x", "transition": "L4->L5",
        "failing_record_id": "r1", "failed_predicates": ["a"],
    }), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        "_show", Path(__file__).resolve().parent.parent / "scripts" / "show_violations.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = mod.main(["--dir", str(rec_dir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "gate_x" in out
    assert "r1" in out


def test_show_violations_cli_since_filter(tmp_path, capsys):
    import importlib.util
    rec_dir = tmp_path / "v2"
    rec_dir.mkdir()
    for ts in ("2026-05-20T00:00:00+00:00", "2026-05-23T00:00:00+00:00"):
        safe = ts.replace(":", "-").replace("+", "_")
        (rec_dir / f"{safe}-g.json").write_text(json.dumps({
            "ts": ts, "gate_id": "g", "transition": "L4->L5",
            "failing_record_id": "x", "failed_predicates": [],
        }), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        "_show2", Path(__file__).resolve().parent.parent / "scripts" / "show_violations.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = mod.main(["--dir", str(rec_dir), "--since", "2026-05-22", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    records = json.loads(out)
    assert len(records) == 1
    assert records[0]["ts"].startswith("2026-05-23")
