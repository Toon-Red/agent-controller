"""Level-promotion gate predicates (3f83494b).

Pure functions that decide whether a unit of work may advance UP the
L-level hierarchy. Each predicate cites the relevant L-level doc in
its docstring; the CI lint at `pipeline-dashboard/scripts/check_l_level_docs.py`
verifies every gate_id in this module appears in exactly one doc.

Signature contract: each predicate takes the relevant input record(s)
and returns ``(passed: bool, failed_predicates: list[str])``. An empty
``failed_predicates`` list MUST imply ``passed=True`` and vice versa --
the list is the operator-facing diagnostic of why the gate refused.

Imports + integration:

  * L7 calls ``gate_l7_to_l8__green_ci`` in
    ``scripts.l7_dispatch_protocol`` before dispatching to L8 oversight.
  * L8 calls ``gate_l8_to_preston__decision_needed`` in
    ``scripts.l8_project_manager`` before sending a Discord ping or
    DREAM_TAB decision payload.
  * L4/L5/L6 gates are pure -- no live integration yet (L4/L5 currently
    run inside claude-flow swarms; we never modify upstream). When the
    agent-controller side gains an L4/L5/L6 dispatch surface, those
    surfaces import the matching gate here.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

GateResult = tuple[bool, list[str]]


# ---------------------------------------------------------------------------
# L4 -> L5: tests must have been run
# ---------------------------------------------------------------------------

def gate_l4_to_l5__tests_run(task: Mapping[str, Any]) -> GateResult:
    """Per docs/l-levels/L4.md ``Gates to Promote``.

    L4 may not request L5 review until ``task.test_results`` is a
    non-empty mapping with at least one of ``passed_count`` or
    ``failed_count`` set. Anything less means qa never ran on this
    task -- L5 review on un-tested work is theatre.
    """
    failures: list[str] = []
    if not isinstance(task, Mapping):
        return False, ["task is not a mapping"]
    results = task.get("test_results")
    if not isinstance(results, Mapping) or not results:
        failures.append("task.test_results is empty or not a mapping")
    else:
        if results.get("passed_count") is None and results.get("failed_count") is None:
            failures.append(
                "task.test_results has neither passed_count nor failed_count")
    return (not failures), failures


# ---------------------------------------------------------------------------
# L5 -> L6: quality review stamped
# ---------------------------------------------------------------------------

def gate_l5_to_l6__quality_review(task: Mapping[str, Any]) -> GateResult:
    """Per docs/l-levels/L5.md ``Gates to Promote``.

    L5 may not request L6 queen escalation until ``task.quality_review_at``
    is a parseable ISO 8601 string AND ``task.quality_review_by`` is
    non-empty.
    """
    failures: list[str] = []
    if not isinstance(task, Mapping):
        return False, ["task is not a mapping"]
    at = task.get("quality_review_at")
    by = task.get("quality_review_by")
    if not isinstance(at, str) or not at:
        failures.append("task.quality_review_at missing or not a string")
    else:
        candidate = at.replace("Z", "+00:00") if at.endswith("Z") else at
        try:
            datetime.fromisoformat(candidate)
        except ValueError:
            failures.append(
                f"task.quality_review_at not a valid ISO 8601 timestamp ({at!r})")
    if not isinstance(by, str) or not by:
        failures.append("task.quality_review_by missing or empty")
    return (not failures), failures


# ---------------------------------------------------------------------------
# L6 -> L7: every swarm member terminal
# ---------------------------------------------------------------------------

_TERMINAL_MEMBER_STATUSES = frozenset({"success", "failed", "cancelled"})


def gate_l6_to_l7__swarm_complete(swarm: Mapping[str, Any]) -> GateResult:
    """Per docs/l-levels/L6.md ``Gates to Promote``.

    L6 may not surface a swarm result to L7 until every member reports
    a terminal status (success / failed / cancelled). ``running`` or
    ``unknown`` means the swarm is still in flight; surfacing now would
    half-bake the result.
    """
    failures: list[str] = []
    if not isinstance(swarm, Mapping):
        return False, ["swarm is not a mapping"]
    members = swarm.get("members") or ()
    if not isinstance(members, Iterable) or isinstance(members, (str, bytes)):
        return False, ["swarm.members is not an iterable"]
    non_terminal: list[str] = []
    saw_member = False
    for m in members:
        if not isinstance(m, Mapping):
            continue
        saw_member = True
        status = str(m.get("status") or "").lower()
        if status not in _TERMINAL_MEMBER_STATUSES:
            mid = str(m.get("id") or m.get("name") or "?")
            non_terminal.append(f"{mid}={status or 'unknown'!s}")
    if not saw_member:
        failures.append("swarm has no member records")
    if non_terminal:
        failures.append(
            f"members not in terminal status: {', '.join(non_terminal)}")
    return (not failures), failures


# ---------------------------------------------------------------------------
# L7 -> L8: qa run was green for the target project
# ---------------------------------------------------------------------------

def gate_l7_to_l8__green_ci(
    project_id: str,
    *,
    qa_run_path: Optional[Path] = None,
) -> GateResult:
    """Per docs/l-levels/L7.md ``Gates to Promote``.

    L7 may not surface a project to L8 oversight unless today's EOD
    orchestrator qa step (``01_qa.json``) shows the project finished
    with ``failed == 0``, ``passed > 0``, and per-project status ``ok``.

    ``qa_run_path`` is injectable for tests; defaults to
    ``pipeline-dashboard/orchestration/eod-runs/<today>/01_qa.json``
    resolved via PD_REPO_PATH env var (matches AR-S3b cron handler's
    resolution pattern) or a sibling-of-this-repo default.
    """
    failures: list[str] = []
    if not isinstance(project_id, str) or not project_id:
        return False, ["project_id must be a non-empty string"]
    if qa_run_path is None:
        pd_root_env = os.environ.get("PD_REPO_PATH")
        if pd_root_env:
            pd_root = Path(pd_root_env)
        else:
            pd_root = Path(__file__).resolve().parent.parent.parent / "pipeline-dashboard"
        today_iso = datetime.now().date().isoformat()
        qa_run_path = pd_root / "orchestration" / "eod-runs" / today_iso / "01_qa.json"
    qa_run_path = Path(qa_run_path)
    if not qa_run_path.is_file():
        return False, [f"qa run not found at {qa_run_path}"]
    try:
        data = json.loads(qa_run_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, [f"qa run unreadable: {type(exc).__name__}: {exc}"]
    per_project = data.get("per_project") or []
    match = None
    for entry in per_project:
        if isinstance(entry, Mapping) and entry.get("project_id") == project_id:
            match = entry
            break
    if match is None:
        return False, [f"project {project_id!r} absent from qa run"]
    status = match.get("status")
    passed = match.get("passed", 0)
    failed = match.get("failed", 0)
    errors = match.get("errors", 0)
    if status != "ok":
        failures.append(f"project status is {status!r}, not 'ok'")
    if failed or errors:
        failures.append(f"project has {failed} failed + {errors} error(s)")
    if not passed:
        failures.append("project has 0 passed tests (qa never produced a green)")
    return (not failures), failures


# ---------------------------------------------------------------------------
# L8 -> Preston: structured decision payload required
# ---------------------------------------------------------------------------

_REQUIRED_DECISION_FIELDS = ("options", "rationale", "data",
                              "recommendation", "decision_needed_by")


def gate_l8_to_preston__decision_needed(
    payload: Mapping[str, Any],
) -> GateResult:
    """Per docs/l-levels/L8.md ``Gates to Promote``.

    L8 may not ping Preston for a decision unless the payload carries:

      * ``options`` -- non-empty list, each entry a mapping with a
        non-empty ``label`` (>= 2 options; one-option "decisions" are
        announcements, not decisions).
      * ``rationale`` -- non-empty string.
      * ``data`` -- mapping (may be empty; presence is the contract).
      * ``recommendation`` -- non-empty string.
      * ``decision_needed_by`` -- ISO 8601 string OR ``None`` (None ==
        no deadline; absent key fails).

    "what do you think" pings are forbidden; this is the schema enforcer.
    """
    failures: list[str] = []
    if not isinstance(payload, Mapping):
        return False, ["payload is not a mapping"]
    for field in _REQUIRED_DECISION_FIELDS:
        if field not in payload:
            failures.append(f"missing required field: {field!r}")
    if failures:
        return False, failures

    options = payload.get("options")
    if not isinstance(options, list) or len(options) < 2:
        failures.append("options must be a list of at least 2 entries")
    else:
        for i, opt in enumerate(options):
            if not isinstance(opt, Mapping):
                failures.append(f"options[{i}] is not a mapping")
            elif not isinstance(opt.get("label"), str) or not opt.get("label"):
                failures.append(f"options[{i}].label missing or empty")

    rationale = payload.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        failures.append("rationale missing or empty")

    data = payload.get("data")
    if not isinstance(data, Mapping):
        failures.append("data must be a mapping (may be empty)")

    recommendation = payload.get("recommendation")
    if not isinstance(recommendation, str) or not recommendation.strip():
        failures.append("recommendation missing or empty")

    decision_by = payload.get("decision_needed_by")
    if decision_by is not None:
        if not isinstance(decision_by, str) or not decision_by:
            failures.append("decision_needed_by must be ISO 8601 string or None")
        else:
            candidate = decision_by.replace("Z", "+00:00") if decision_by.endswith("Z") else decision_by
            try:
                datetime.fromisoformat(candidate)
            except ValueError:
                failures.append(
                    f"decision_needed_by not a valid ISO 8601 timestamp ({decision_by!r})")

    return (not failures), failures


# ---------------------------------------------------------------------------
# Registry -- used by the CI lint to verify every gate_id has a doc
# ---------------------------------------------------------------------------

ALL_GATES: dict[str, str] = {
    "gate_l4_to_l5__tests_run": "L4.md",
    "gate_l5_to_l6__quality_review": "L5.md",
    "gate_l6_to_l7__swarm_complete": "L6.md",
    "gate_l7_to_l8__green_ci": "L7.md",
    "gate_l8_to_preston__decision_needed": "L8.md",
}
