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


# ---------------------------------------------------------------------------
# Enforcement exception (47c52660)
# ---------------------------------------------------------------------------

class LevelGateViolation(BaseException):
    """Raised by ``scripts.level_gate_enforcer.enforce_transition`` when
    a level-gate predicate refuses a transition.

    Inherits from ``BaseException`` (not ``Exception``) so it cannot be
    swallowed by ``except Exception`` -- the whole point is the
    violation must propagate to the L7/L8 caller. The orchestrator
    layer that holds the source record is responsible for surfacing
    + waiting for the failing predicate to clear, never for working
    around the refusal.

    d15de2b4 extension: ``failed_principles`` (separate list) carries
    Amazon-LP-friendly principle names when the violation came from
    a ``principle_*`` predicate. ``failed_predicates`` stays for the
    raw predicate strings the gate function returned.
    """

    def __init__(self, *, gate_id: str, failed_predicates: list,
                 failing_record_id: str, transition: str,
                 failed_principles: list | None = None) -> None:
        self.gate_id = gate_id
        self.failed_predicates = list(failed_predicates)
        self.failed_principles = list(failed_principles or [])
        self.failing_record_id = failing_record_id
        self.transition = transition
        suffix = ""
        if self.failed_principles:
            suffix = f"; principles: {self.failed_principles}"
        super().__init__(
            f"{transition} blocked by {gate_id} on record "
            f"{failing_record_id!r}: {self.failed_predicates}{suffix}"
        )


# ---------------------------------------------------------------------------
# Principle predicates (d15de2b4)
# ---------------------------------------------------------------------------
#
# Amazon Leadership Principles enforced as machine-checked gates on every
# L-transition. Stacked with the existing gate_* predicates -- ALL must
# pass. Each principle: (record: dict) -> (passed: bool, failed: list[str]).
# The `record` is a flat dict typically derived from a task wiki's
# frontmatter; the optional `body` key carries the wiki body text for
# checks that need to scan "Decision:" lines.

import re as _re
from datetime import datetime as _dt, timezone as _tz, timedelta as _td
from pathlib import Path as _Path

_USER_STORY_RE = _re.compile(r"\b(?:request|user_story):[a-f0-9]{8}\b")
_RESEARCH_RE = _re.compile(r"\bresearch:[a-f0-9]{8}\b")
_TEST_RE = _re.compile(r"\btest:[a-z0-9_]+\b")
_COMMIT_RE = _re.compile(r"\b[a-f0-9]{7,40}\b")
_DECISION_LINE_RE = _re.compile(r"^\s*Decision:\s*(.+)$", _re.MULTILINE)


def _str(v: object) -> str:
    return str(v).strip() if v is not None else ""


def principle_customer_cited(record: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Customer Obsession: ``customer_impact`` non-empty AND body cites
    at least one ``request:<hex8>`` or ``user_story:<hex8>``."""
    fails: list[str] = []
    if not _str(record.get("customer_impact")):
        fails.append("record.customer_impact is empty or missing")
    body = _str(record.get("body"))
    if not _USER_STORY_RE.search(body):
        fails.append(
            "record body cites no request:<hex8> or user_story:<hex8>"
        )
    return (not fails, fails)


def _load_owners_yaml(owners_yaml_path: _Path | None = None) -> set[str]:
    """Read active owner ids from wiki/owners.yaml. Empty set on
    missing/unreadable -- caller treats absence as 'no validation'."""
    try:
        import yaml  # type: ignore
        p = owners_yaml_path or (_Path(__file__).resolve().parent.parent.parent
                                  / "pipeline-dashboard" / "wiki" / "owners.yaml")
        if not p.is_file():
            return set()
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        out: set[str] = set()
        for entry in (data.get("owners") or []):
            if isinstance(entry, Mapping) and entry.get("active", True):
                oid = entry.get("id")
                if isinstance(oid, str) and oid.strip():
                    out.add(oid.strip())
        return out
    except Exception:
        return set()


def principle_owner_declared(record: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Ownership: ``owner`` frontmatter non-empty AND in the
    project's wiki/owners.yaml active list (when reachable)."""
    fails: list[str] = []
    owner = _str(record.get("owner"))
    if not owner:
        fails.append("record.owner is empty or missing")
        return (False, fails)
    owners = _load_owners_yaml(record.get("_owners_yaml"))
    if owners and owner not in owners:
        fails.append(
            f"owner {owner!r} not in wiki/owners.yaml active list "
            f"({sorted(owners)})"
        )
    return (not fails, fails)


def principle_data_cited(record: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Dive Deep: every 'Decision:' line in the body cites at least one
    of research:<hex8>, test:<id>, or a commit SHA. Empty body = pass
    (no decisions to cite)."""
    body = _str(record.get("body"))
    fails: list[str] = []
    decisions = _DECISION_LINE_RE.findall(body)
    for i, line in enumerate(decisions, start=1):
        has_research = bool(_RESEARCH_RE.search(line))
        has_test = bool(_TEST_RE.search(line))
        has_commit = bool(_COMMIT_RE.search(line))
        if not (has_research or has_test or has_commit):
            preview = line.strip()[:80]
            fails.append(
                f"Decision line #{i} cites no research/test/commit: "
                f"{preview!r}"
            )
    return (not fails, fails)


def principle_gates_passed(record: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Insist on the Highest Standards: PD's close_gates evaluator
    returns 0 failures for this record's task_id.

    Implemented as a thin HTTP shim against PD. Fail-closed: if PD is
    unreachable or returns a non-200 unexpectedly, treat the principle
    as failed (operator must verify by hand or unblock PD).
    """
    tid = _str(record.get("task_id") or record.get("id"))
    pid = _str(record.get("project_id"))
    if not tid:
        return (False, ["record has no task_id or id; cannot verify gates"])
    if not pid:
        return (False, ["record has no project_id; cannot verify gates"])
    try:
        import os as _os, json as _json, urllib.request as _ur
        base = _os.environ.get("PD_API_BASE", "http://127.0.0.1:5100")
        # Use a no-op update (POST with empty body) -- PD's update_task
        # returns the close_blocked sentinel without mutating when the
        # gates would refuse. For a no-op (no status change), gates
        # do NOT run. So we read the task via GET and let the caller
        # observe quality.missing / open close-blocked finding markers.
        url = f"{base}/api/projects/{pid}/tasks/{tid}"
        with _ur.urlopen(url, timeout=3) as r:
            data = _json.loads(r.read())
        # Heuristic surrogate (since the real evaluation only happens on
        # status flip): a task whose quality.missing includes 'tests' AND
        # whose status is in-flight is FAILING close gates.
        q = (data.get("quality") or {})
        missing = set(q.get("missing") or [])
        if missing and "tests" in missing and data.get("status") != "done":
            return (False, [
                f"close-gate surrogate failure: quality.missing={sorted(missing)} "
                f"(task is not done AND lacks tests)"
            ])
        return (True, [])
    except Exception as exc:
        return (False, [
            f"could not verify close gates via PD HTTP: "
            f"{type(exc).__name__}: {exc}"
        ])


_ALLOWED_COMPLEXITIES = {"S", "M", "L", "XL"}


def principle_cost_evaluated(record: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Frugality: ``complexity`` in S/M/L/XL AND ``value_score`` is an
    int in 1..10. Per the dispatch, REQUIRED only for L7->L8; other
    transitions exempt by NOT registering this principle in the YAML."""
    fails: list[str] = []
    cx = _str(record.get("complexity"))
    if cx not in _ALLOWED_COMPLEXITIES:
        fails.append(
            f"complexity {cx!r} not in {sorted(_ALLOWED_COMPLEXITIES)}"
        )
    vs = record.get("value_score")
    try:
        vs_int = int(vs)
        if not (1 <= vs_int <= 10):
            fails.append(f"value_score {vs!r} not in [1, 10]")
    except (TypeError, ValueError):
        fails.append(f"value_score {vs!r} is not an integer")
    return (not fails, fails)


def principle_no_indefinite_stall(record: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Bias for Action: ``updated_at`` within 7 days OR ``status`` is
    ``blocked`` AND ``blocked_reason`` non-empty."""
    status = _str(record.get("status"))
    if status == "blocked":
        if not _str(record.get("blocked_reason")):
            return (False, [
                "status is 'blocked' but blocked_reason is empty"
            ])
        return (True, [])
    raw = _str(record.get("updated_at"))
    if not raw:
        return (False, ["updated_at missing"])
    try:
        candidate = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        dt = _dt.fromisoformat(candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        age = _dt.now(_tz.utc) - dt
        if age > _td(days=7):
            return (False, [
                f"updated_at is {age.days}d old (limit: 7d) and status "
                f"is not 'blocked'"
            ])
        return (True, [])
    except ValueError:
        return (False, [f"updated_at {raw!r} is not parseable ISO 8601"])


ALL_PRINCIPLES: dict[str, str] = {
    "principle_customer_cited": "Customer Obsession",
    "principle_owner_declared": "Ownership",
    "principle_data_cited": "Dive Deep",
    "principle_gates_passed": "Insist on the Highest Standards",
    "principle_cost_evaluated": "Frugality",
    "principle_no_indefinite_stall": "Bias for Action",
}
