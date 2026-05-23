"""Tests for the 6 principle predicates (d15de2b4)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.level_gates import (  # noqa: E402
    ALL_PRINCIPLES,
    LevelGateViolation,
    principle_customer_cited,
    principle_owner_declared,
    principle_data_cited,
    principle_gates_passed,
    principle_cost_evaluated,
    principle_no_indefinite_stall,
)
from scripts import level_gate_enforcer as enf  # noqa: E402


def _recent_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stale_iso(days: int = 10) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# principle_customer_cited
# ---------------------------------------------------------------------------

def test_customer_cited_passes_with_impact_and_request_ref():
    rec = {"customer_impact": "Operators were blocked.",
           "body": "Tracks request:a1b2c3d4 for the regression."}
    assert principle_customer_cited(rec) == (True, [])


def test_customer_cited_fails_when_no_impact_and_no_ref():
    passed, fails = principle_customer_cited({"body": "no link"})
    assert not passed
    assert any("customer_impact" in f for f in fails)
    assert any("user_story" in f or "request:" in f for f in fails)


# ---------------------------------------------------------------------------
# principle_owner_declared
# ---------------------------------------------------------------------------

def test_owner_declared_passes_with_active_owner(tmp_path):
    owners_yaml = tmp_path / "owners.yaml"
    owners_yaml.write_text(
        "project: x\nowners:\n  - id: preston\n    name: Preston\n    active: true\n",
        encoding="utf-8")
    rec = {"owner": "preston", "_owners_yaml": owners_yaml}
    assert principle_owner_declared(rec) == (True, [])


def test_owner_declared_fails_without_owner_field():
    passed, fails = principle_owner_declared({})
    assert not passed
    assert any("owner" in f for f in fails)


def test_owner_declared_fails_when_owner_not_in_active_list(tmp_path):
    owners_yaml = tmp_path / "owners.yaml"
    owners_yaml.write_text(
        "project: x\nowners:\n  - id: preston\n    name: Preston\n    active: true\n",
        encoding="utf-8")
    rec = {"owner": "ghost", "_owners_yaml": owners_yaml}
    passed, fails = principle_owner_declared(rec)
    assert not passed
    assert any("ghost" in f for f in fails)


# ---------------------------------------------------------------------------
# principle_data_cited
# ---------------------------------------------------------------------------

def test_data_cited_empty_body_passes():
    assert principle_data_cited({"body": ""}) == (True, [])


def test_data_cited_passes_with_research_ref():
    rec = {"body": "## Notes\nDecision: chose A. See research:abc12345 for tradeoffs."}
    assert principle_data_cited(rec) == (True, [])


def test_data_cited_passes_with_commit_sha():
    rec = {"body": "Decision: rolled forward after a1b2c3d landed cleanly."}
    assert principle_data_cited(rec) == (True, [])


def test_data_cited_fails_on_unsourced_decision():
    rec = {"body": "Decision: ship it because vibes."}
    passed, fails = principle_data_cited(rec)
    assert not passed
    assert any("Decision line" in f for f in fails)


# ---------------------------------------------------------------------------
# principle_gates_passed (HTTP shim; PD unreachable in tests)
# ---------------------------------------------------------------------------

def test_gates_passed_fails_when_no_task_id():
    passed, fails = principle_gates_passed({})
    assert not passed
    assert any("task_id" in f for f in fails)


def test_gates_passed_fails_when_no_project_id():
    passed, fails = principle_gates_passed({"task_id": "t1"})
    assert not passed
    assert any("project_id" in f for f in fails)


def test_gates_passed_fails_when_pd_unreachable(monkeypatch):
    # urlopen raises -> the principle treats it as failed (fail-closed).
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: (_ for _ in ()).throw(OSError("down")))
    passed, fails = principle_gates_passed(
        {"task_id": "t1", "project_id": "p1"})
    assert not passed
    assert any("could not verify close gates" in f for f in fails)


# ---------------------------------------------------------------------------
# principle_cost_evaluated
# ---------------------------------------------------------------------------

def test_cost_evaluated_passes_with_complexity_and_value():
    assert principle_cost_evaluated(
        {"complexity": "M", "value_score": 7}) == (True, [])


def test_cost_evaluated_fails_on_bad_complexity():
    passed, fails = principle_cost_evaluated(
        {"complexity": "huge", "value_score": 5})
    assert not passed
    assert any("complexity" in f for f in fails)


def test_cost_evaluated_fails_on_out_of_range_value():
    passed, fails = principle_cost_evaluated(
        {"complexity": "S", "value_score": 99})
    assert not passed
    assert any("value_score" in f for f in fails)


def test_cost_evaluated_fails_on_non_int_value():
    passed, fails = principle_cost_evaluated(
        {"complexity": "L", "value_score": "tons"})
    assert not passed
    assert any("not an integer" in f for f in fails)


# ---------------------------------------------------------------------------
# principle_no_indefinite_stall
# ---------------------------------------------------------------------------

def test_no_stall_passes_with_recent_updated_at():
    assert principle_no_indefinite_stall(
        {"updated_at": _recent_iso(), "status": "in_progress"}) == (True, [])


def test_no_stall_passes_when_blocked_with_reason():
    assert principle_no_indefinite_stall(
        {"status": "blocked", "blocked_reason": "Waiting on upstream API."}
    ) == (True, [])


def test_no_stall_fails_on_stale_unblocked_record():
    passed, fails = principle_no_indefinite_stall(
        {"updated_at": _stale_iso(15), "status": "in_progress"})
    assert not passed
    assert any("limit: 7d" in f for f in fails)


def test_no_stall_fails_when_blocked_without_reason():
    passed, fails = principle_no_indefinite_stall(
        {"status": "blocked", "blocked_reason": ""})
    assert not passed
    assert any("blocked_reason" in f for f in fails)


# ---------------------------------------------------------------------------
# PRINCIPLE_REGISTRY pin
# ---------------------------------------------------------------------------

def test_principle_registry_has_all_6():
    assert set(enf.PRINCIPLE_REGISTRY) == set(ALL_PRINCIPLES)
    for name, fn in enf.PRINCIPLE_REGISTRY.items():
        assert callable(fn), f"{name} not callable"


def test_principle_registry_yaml_names_resolve(tmp_path):
    """Every principle named in level_gates.yaml resolves to a function
    in PRINCIPLE_REGISTRY -- catches typos in the YAML."""
    yaml_map = enf._load_transitions_yaml()
    if not yaml_map:
        pytest.skip("level_gates.yaml not present in this checkout")
    for transition, entry in yaml_map.items():
        for pname in entry.get("principles", []):
            assert pname in enf.PRINCIPLE_REGISTRY, \
                f"{transition}: principle {pname!r} not in registry"


# ---------------------------------------------------------------------------
# End-to-end enforce() with principle stack
# ---------------------------------------------------------------------------

def test_enforce_l4_to_l5_passes_with_full_record(tmp_path, monkeypatch):
    """L4->L5 with valid gate + 3 principles satisfied."""
    yaml_path = tmp_path / "lg.yaml"
    yaml_path.write_text(
        "transitions:\n"
        "  L4_to_L5:\n"
        "    gates: [gate_l4_to_l5__tests_run]\n"
        "    principles: [principle_owner_declared, principle_data_cited, "
        "principle_no_indefinite_stall]\n",
        encoding="utf-8")
    # Patch the owners.yaml load to a tmp file that has 'preston'.
    owners_yaml = tmp_path / "owners.yaml"
    owners_yaml.write_text(
        "project: x\nowners:\n  - id: preston\n    name: P\n    active: true\n",
        encoding="utf-8")
    record = {
        "id": "t-1",
        "test_results": {"passed_count": 5},
        "owner": "preston",
        "_owners_yaml": owners_yaml,
        "body": "",
        "updated_at": _recent_iso(),
        "status": "in_progress",
    }
    enf.enforce("L4->L5", record, ping=False, violations_dir=tmp_path,
                yaml_path=yaml_path)


def test_enforce_raises_with_failed_principles_list(tmp_path):
    """A failing principle surfaces by name in violation.failed_principles."""
    yaml_path = tmp_path / "lg.yaml"
    yaml_path.write_text(
        "transitions:\n"
        "  L4_to_L5:\n"
        "    gates: [gate_l4_to_l5__tests_run]\n"
        "    principles: [principle_owner_declared]\n",
        encoding="utf-8")
    record = {
        "id": "t-2", "test_results": {"passed_count": 1},
        # owner missing -> principle_owner_declared fails
    }
    with pytest.raises(LevelGateViolation) as exc:
        enf.enforce("L4->L5", record, ping=False,
                    violations_dir=tmp_path, yaml_path=yaml_path)
    assert "principle_owner_declared" in exc.value.failed_principles


def test_enforce_aggregates_gate_and_principle_failures(tmp_path):
    """Gate failure AND principle failure surface together."""
    yaml_path = tmp_path / "lg.yaml"
    yaml_path.write_text(
        "transitions:\n"
        "  L4_to_L5:\n"
        "    gates: [gate_l4_to_l5__tests_run]\n"
        "    principles: [principle_owner_declared]\n",
        encoding="utf-8")
    with pytest.raises(LevelGateViolation) as exc:
        enf.enforce("L4->L5", {"id": "t-3"}, ping=False,
                    violations_dir=tmp_path, yaml_path=yaml_path)
    # Gate failure surfaces in failed_predicates as well.
    assert any("test_results" in f for f in exc.value.failed_predicates)
    assert "principle_owner_declared" in exc.value.failed_principles


def test_enforce_missing_yaml_falls_back_to_gates_only(tmp_path):
    """When level_gates.yaml is absent, enforce() uses the hardcoded
    TRANSITION_TO_GATE mapping (gates only, no principles)."""
    with pytest.raises(LevelGateViolation) as exc:
        enf.enforce("L4->L5", {"id": "x"}, ping=False,
                    violations_dir=tmp_path,
                    yaml_path=tmp_path / "does-not-exist.yaml")
    assert exc.value.gate_id == "gate_l4_to_l5__tests_run"
    assert exc.value.failed_principles == []


def test_enforce_persists_violation_with_principle_tag(tmp_path):
    """Failed-principle output lands in the violation log."""
    yaml_path = tmp_path / "lg.yaml"
    yaml_path.write_text(
        "transitions:\n"
        "  L4_to_L5:\n"
        "    gates: []\n"
        "    principles: [principle_owner_declared]\n",
        encoding="utf-8")
    vio_dir = tmp_path / "vio"
    with pytest.raises(LevelGateViolation):
        enf.enforce("L4->L5", {"id": "t-vio"}, ping=False,
                    violations_dir=vio_dir, yaml_path=yaml_path)
    files = list(vio_dir.glob("*.json"))
    assert len(files) == 1
    import json
    rec = json.loads(files[0].read_text(encoding="utf-8"))
    # The failed_predicates list carries the [principle_*]-tagged
    # entries from the per-principle reason lines.
    assert any("[principle_owner_declared]" in f
               for f in rec["failed_predicates"])
