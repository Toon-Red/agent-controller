"""Tests for scripts/level_gates.py (3f83494b).

One block per gate predicate covering pass + fail with injected inputs;
no live PD / git / network. Each block exercises:
  - happy path (passed=True, failed_predicates=[])
  - at least one targeted failure mode that returns a useful diagnostic
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATES_PATH = REPO_ROOT / "scripts" / "level_gates.py"


def _load_gates_module():
    spec = importlib.util.spec_from_file_location("level_gates", GATES_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["level_gates"] = mod
    spec.loader.exec_module(mod)
    return mod


gates = _load_gates_module()


# ---------------------------------------------------------------------------
# gate_l4_to_l5__tests_run
# ---------------------------------------------------------------------------

def test_l4_to_l5_passes_with_passed_count():
    ok, failures = gates.gate_l4_to_l5__tests_run({"test_results": {"passed_count": 12}})
    assert ok and failures == []


def test_l4_to_l5_passes_with_failed_count():
    ok, failures = gates.gate_l4_to_l5__tests_run({"test_results": {"failed_count": 3}})
    assert ok and failures == []


def test_l4_to_l5_fails_when_test_results_missing():
    ok, failures = gates.gate_l4_to_l5__tests_run({})
    assert not ok
    assert any("test_results" in f for f in failures)


def test_l4_to_l5_fails_when_test_results_empty():
    ok, failures = gates.gate_l4_to_l5__tests_run({"test_results": {}})
    assert not ok
    assert any("empty" in f for f in failures)


def test_l4_to_l5_fails_when_no_counts():
    ok, failures = gates.gate_l4_to_l5__tests_run({"test_results": {"duration_s": 1.2}})
    assert not ok
    assert any("passed_count" in f and "failed_count" in f for f in failures)


def test_l4_to_l5_fails_when_task_not_a_mapping():
    ok, failures = gates.gate_l4_to_l5__tests_run("not a mapping")  # type: ignore[arg-type]
    assert not ok and failures


# ---------------------------------------------------------------------------
# gate_l5_to_l6__quality_review
# ---------------------------------------------------------------------------

def test_l5_to_l6_passes_with_iso_and_by():
    ok, failures = gates.gate_l5_to_l6__quality_review(
        {"quality_review_at": "2026-05-23T12:00:00+00:00", "quality_review_by": "qa-manager"}
    )
    assert ok and failures == []


def test_l5_to_l6_accepts_zulu_suffix():
    ok, failures = gates.gate_l5_to_l6__quality_review(
        {"quality_review_at": "2026-05-23T12:00:00Z", "quality_review_by": "qa-manager"}
    )
    assert ok and failures == []


def test_l5_to_l6_fails_on_garbage_timestamp():
    ok, failures = gates.gate_l5_to_l6__quality_review(
        {"quality_review_at": "yesterday afternoon", "quality_review_by": "qa-manager"}
    )
    assert not ok
    assert any("ISO 8601" in f for f in failures)


def test_l5_to_l6_fails_when_by_missing():
    ok, failures = gates.gate_l5_to_l6__quality_review(
        {"quality_review_at": "2026-05-23T12:00:00Z", "quality_review_by": ""}
    )
    assert not ok
    assert any("quality_review_by" in f for f in failures)


def test_l5_to_l6_fails_when_both_missing():
    ok, failures = gates.gate_l5_to_l6__quality_review({})
    assert not ok
    assert len(failures) >= 2


# ---------------------------------------------------------------------------
# gate_l6_to_l7__swarm_complete
# ---------------------------------------------------------------------------

def test_l6_to_l7_passes_when_all_members_terminal():
    swarm = {
        "members": [
            {"id": "a", "status": "success"},
            {"id": "b", "status": "failed"},
            {"id": "c", "status": "cancelled"},
        ]
    }
    ok, failures = gates.gate_l6_to_l7__swarm_complete(swarm)
    assert ok and failures == []


def test_l6_to_l7_fails_when_a_member_still_running():
    swarm = {
        "members": [
            {"id": "a", "status": "success"},
            {"id": "b", "status": "running"},
        ]
    }
    ok, failures = gates.gate_l6_to_l7__swarm_complete(swarm)
    assert not ok
    assert any("not in terminal status" in f and "b=running" in f for f in failures)


def test_l6_to_l7_fails_when_no_members():
    ok, failures = gates.gate_l6_to_l7__swarm_complete({"members": []})
    assert not ok
    assert any("no member records" in f for f in failures)


def test_l6_to_l7_fails_when_status_blank():
    swarm = {"members": [{"id": "a", "status": ""}]}
    ok, failures = gates.gate_l6_to_l7__swarm_complete(swarm)
    assert not ok
    assert any("a=unknown" in f for f in failures)


def test_l6_to_l7_fails_when_members_not_iterable():
    ok, failures = gates.gate_l6_to_l7__swarm_complete({"members": "abc"})
    assert not ok
    assert any("not an iterable" in f for f in failures)


# ---------------------------------------------------------------------------
# gate_l7_to_l8__green_ci
# ---------------------------------------------------------------------------

def _write_qa(path: Path, *, project_id: str, passed: int, failed: int, errors: int, status: str = "ok") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "per_project": [
                    {
                        "project_id": project_id,
                        "passed": passed,
                        "failed": failed,
                        "errors": errors,
                        "status": status,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_l7_to_l8_passes_for_clean_project(tmp_path):
    qa = tmp_path / "01_qa.json"
    _write_qa(qa, project_id="pd", passed=42, failed=0, errors=0)
    ok, failures = gates.gate_l7_to_l8__green_ci("pd", qa_run_path=qa)
    assert ok and failures == []


def test_l7_to_l8_fails_when_failed_count_nonzero(tmp_path):
    qa = tmp_path / "01_qa.json"
    _write_qa(qa, project_id="pd", passed=10, failed=2, errors=0)
    ok, failures = gates.gate_l7_to_l8__green_ci("pd", qa_run_path=qa)
    assert not ok
    assert any("2 failed" in f for f in failures)


def test_l7_to_l8_fails_when_zero_passed(tmp_path):
    qa = tmp_path / "01_qa.json"
    _write_qa(qa, project_id="pd", passed=0, failed=0, errors=0)
    ok, failures = gates.gate_l7_to_l8__green_ci("pd", qa_run_path=qa)
    assert not ok
    assert any("0 passed" in f for f in failures)


def test_l7_to_l8_fails_when_status_not_ok(tmp_path):
    qa = tmp_path / "01_qa.json"
    _write_qa(qa, project_id="pd", passed=10, failed=0, errors=0, status="skipped")
    ok, failures = gates.gate_l7_to_l8__green_ci("pd", qa_run_path=qa)
    assert not ok
    assert any("status is 'skipped'" in f for f in failures)


def test_l7_to_l8_fails_when_project_absent(tmp_path):
    qa = tmp_path / "01_qa.json"
    _write_qa(qa, project_id="other", passed=10, failed=0, errors=0)
    ok, failures = gates.gate_l7_to_l8__green_ci("pd", qa_run_path=qa)
    assert not ok
    assert any("'pd' absent" in f for f in failures)


def test_l7_to_l8_fails_when_qa_file_missing(tmp_path):
    qa = tmp_path / "missing.json"
    ok, failures = gates.gate_l7_to_l8__green_ci("pd", qa_run_path=qa)
    assert not ok
    assert any("qa run not found" in f for f in failures)


def test_l7_to_l8_fails_on_empty_project_id():
    ok, failures = gates.gate_l7_to_l8__green_ci("")
    assert not ok
    assert any("non-empty string" in f for f in failures)


# ---------------------------------------------------------------------------
# gate_l8_to_preston__decision_needed
# ---------------------------------------------------------------------------

def _good_payload(**overrides) -> dict:
    payload = {
        "options": [{"label": "Ship it"}, {"label": "Hold"}],
        "rationale": "Risk vs schedule",
        "data": {"open_bugs": 3},
        "recommendation": "Ship it -- bugs are non-blocking",
        "decision_needed_by": "2026-05-24T17:00:00Z",
    }
    payload.update(overrides)
    return payload


def test_l8_to_preston_passes_on_good_payload():
    ok, failures = gates.gate_l8_to_preston__decision_needed(_good_payload())
    assert ok and failures == []


def test_l8_to_preston_passes_when_deadline_none():
    ok, failures = gates.gate_l8_to_preston__decision_needed(_good_payload(decision_needed_by=None))
    assert ok and failures == []


def test_l8_to_preston_fails_when_one_option():
    ok, failures = gates.gate_l8_to_preston__decision_needed(
        _good_payload(options=[{"label": "Only choice"}])
    )
    assert not ok
    assert any("at least 2" in f for f in failures)


def test_l8_to_preston_fails_when_option_label_empty():
    ok, failures = gates.gate_l8_to_preston__decision_needed(
        _good_payload(options=[{"label": "a"}, {"label": ""}])
    )
    assert not ok
    assert any("options[1].label" in f for f in failures)


def test_l8_to_preston_fails_when_rationale_blank():
    ok, failures = gates.gate_l8_to_preston__decision_needed(_good_payload(rationale="   "))
    assert not ok
    assert any("rationale" in f for f in failures)


def test_l8_to_preston_fails_when_data_not_mapping():
    ok, failures = gates.gate_l8_to_preston__decision_needed(_good_payload(data=["x"]))
    assert not ok
    assert any("data must be a mapping" in f for f in failures)


def test_l8_to_preston_fails_when_recommendation_missing():
    ok, failures = gates.gate_l8_to_preston__decision_needed(_good_payload(recommendation=""))
    assert not ok
    assert any("recommendation" in f for f in failures)


def test_l8_to_preston_fails_when_deadline_garbage():
    ok, failures = gates.gate_l8_to_preston__decision_needed(_good_payload(decision_needed_by="next Tuesdayish"))
    assert not ok
    assert any("ISO 8601" in f for f in failures)


def test_l8_to_preston_fails_when_required_field_absent():
    payload = _good_payload()
    del payload["options"]
    ok, failures = gates.gate_l8_to_preston__decision_needed(payload)
    assert not ok
    assert any("missing required field" in f and "options" in f for f in failures)


# ---------------------------------------------------------------------------
# ALL_GATES registry
# ---------------------------------------------------------------------------

def test_all_gates_registry_matches_module_attributes():
    for gate_id in gates.ALL_GATES:
        assert hasattr(gates, gate_id), f"registry references {gate_id!r}, which is not a module-level function"
        assert callable(getattr(gates, gate_id)), f"{gate_id} is not callable"


def test_all_gates_registry_has_expected_levels():
    assert set(gates.ALL_GATES.values()) == {"L4.md", "L5.md", "L6.md", "L7.md", "L8.md"}
