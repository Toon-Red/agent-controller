"""Build LPrompt from (task, project, level, role) (09ca6f69).

Composes the 5 blocks per the level's scope. L4 sees raw file
slices + test records. L7 sees project state + open tasks. Cross-
level bleed is structurally prevented because each (level, role)
has its OWN composer function -- the L4 composer doesn't know how
to read L7 strategic context, and vice versa.

Calls ``scripts.l_prompt.validate_or_raise`` before returning so
the LLM (or HumanEngine) never sees a broken prompt.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from scripts.l_prompt import LPrompt, validate_or_raise


# ---------------------------------------------------------------------------
# Role -> tool allowlist. Hard-coded here; AC-AGENT-ENV1 (97346fa1)
# will move this to a per-role YAML for declarative config.
# ---------------------------------------------------------------------------

ROLE_TOOL_ALLOWLIST: dict[str, list[str]] = {
    # L4 grunts
    "coder":       ["read_file", "edit_file", "write_file", "run_tests"],
    "qa":          ["read_file", "run_tests", "list_tests"],
    "playtester":  ["read_file", "browser_open", "browser_click",
                    "browser_screenshot"],
    "grader":      ["read_file", "list_files"],
    # L5 managers
    "manager":     ["read_pd_task", "list_pd_tasks", "update_pd_task",
                    "list_files"],
    # L6 queen
    "queen":       ["spawn_agent", "list_agents", "agent_status",
                    "kill_agent"],
    # L7 dispatch
    "dispatch":    ["read_pd_task", "list_pd_tasks", "update_pd_task",
                    "read_pd_project", "list_pd_projects",
                    "send_l7_dispatch", "observe_l7_progress"],
}


# ---------------------------------------------------------------------------
# Per-(level, role) composers
# ---------------------------------------------------------------------------

def _compose_l4_coder(task: Mapping[str, Any],
                      project: Mapping[str, Any]) -> LPrompt:
    """L4 coder sees: assigned file paths + test record + acceptance
    criteria. NOT project history, NOT other tasks' context."""
    files_changed = list(task.get("files_changed") or [])
    tests = list(task.get("tests") or [])
    test_results = dict(task.get("test_results") or {})
    acceptance = task.get("acceptance_criteria") or task.get("description") or ""
    inputs_refs = (
        "${files_changed}, ${tests}, ${test_results}, "
        "${acceptance_criteria}"
    )
    done_when = (
        f"All linked tests pass: {tests!r}. Each test ref must appear in "
        f"task.test_results with passed=true. Verify by running "
        f"`pytest {tests[0] if tests else '<no-tests>'}` (test:"
        f"{tests[0] if tests else 'none'})."
    )
    prompt: LPrompt = {
        "level": "L4",
        "agent_role": "coder",
        "context_block": {
            "files_changed": files_changed,
            "tests": tests,
            "test_results": test_results,
            "acceptance_criteria": acceptance,
            "task_id": task.get("id"),
            # 97346fa1: ui block carries the actual surfaces the agent
            # operates on (file content, test logs), not just paths.
            # Production composers would hydrate these from the live
            # files; the synthetic stub here surfaces the names so the
            # env validator can confirm the shape.
            "ui": {
                "files_to_read_first": files_changed,
                "tests_to_run_first": tests,
                "summary": "L4 coder operates on the file content + test logs above.",
            },
        },
        "task_block": {
            "inputs": inputs_refs,
            "outputs": ["modified file contents", "test_results updates"],
            "done_when": done_when,
        },
        "tools_block": list(ROLE_TOOL_ALLOWLIST["coder"]),
        "examples_block": [
            {"task": "fix off-by-one in `pagination.py:42`",
             "output": {"file": "pagination.py",
                        "diff": "- if i > end:\n+ if i >= end:",
                        "test_run": "test:test_pagination::test_last_page"}}
        ],
    }
    return prompt


def _compose_l5_manager(task: Mapping[str, Any],
                        project: Mapping[str, Any]) -> LPrompt:
    """L5 manager sees: L4 output records + role rubric + prior decisions."""
    deliverables = list(task.get("deliverables") or [])
    decisions = list(task.get("decisions") or [])
    prompt: LPrompt = {
        "level": "L5",
        "agent_role": "manager",
        "context_block": {
            "task_id": task.get("id"),
            "status": task.get("status"),
            "deliverables": deliverables,
            "decisions": decisions,
            "test_results": task.get("test_results") or {},
            "rubric": (
                "Coder rubric: all tests pass, files_changed is non-empty, "
                "regression_info is null."
            ),
            "ui": {
                "task_record_summary": (
                    f"task_id={task.get('id')!r} status={task.get('status')!r} "
                    f"with {len(deliverables)} deliverable(s) "
                    f"and {len(decisions)} prior decision(s)"
                ),
                "rubric_text": "Apply the coder rubric above to the L4 output.",
            },
        },
        "task_block": {
            "inputs": "${deliverables}, ${decisions}, ${test_results}, ${rubric}",
            "outputs": [
                "task.quality_review_at (ISO 8601 UTC)",
                "task.quality_review_by (manager id)",
                "task.decisions[] append with rubric outcome",
            ],
            "done_when": (
                "task.quality_review_at is non-empty AND test_results "
                "shows every linked test passed=true (test:rubric_pass). "
                "Verify via record id task:${task_id}."
            ),
        },
        "tools_block": list(ROLE_TOOL_ALLOWLIST["manager"]),
        "examples_block": [
            {"task": "review coder output for task:abc12345",
             "output": {"quality_review_at": "2026-05-23T15:00:00+00:00",
                        "quality_review_by": "qa-manager"}}
        ],
    }
    return prompt


def _compose_l7_dispatch(task: Mapping[str, Any],
                         project: Mapping[str, Any]) -> LPrompt:
    """L7 dispatch sees: project state + open task list + recent
    decisions. NOT raw file diffs (that's L4)."""
    pid = task.get("project_id") or project.get("project_id") or ""
    prompt: LPrompt = {
        "level": "L7",
        "agent_role": "dispatch",
        "context_block": {
            "project_id": pid,
            "project_state": project.get("lifecycle") or "unknown",
            "open_tasks": project.get("open_tasks") or [],
            "recent_decisions": project.get("recent_decisions") or [],
            "task_id": task.get("id"),
            "task_title": task.get("title"),
            "task_priority": task.get("priority", "normal"),
            "ui": {
                "project_summary": (
                    f"Project {pid!r} in lifecycle "
                    f"{project.get('lifecycle')!r} with "
                    f"{len(project.get('open_tasks') or [])} open task(s)."
                ),
                "task_summary": (
                    f"task={task.get('id')!r} title={task.get('title')!r} "
                    f"priority={task.get('priority', 'normal')!r}"
                ),
            },
        },
        "task_block": {
            "inputs": (
                "${project_id}, ${project_state}, ${open_tasks}, "
                "${recent_decisions}, ${task_id}, ${task_title}"
            ),
            "outputs": [
                "L7DispatchRequest with iteration_id + claim_token",
                "L7ProgressMessage stream (status: running|blocked|done)",
            ],
            "done_when": (
                "Dispatch completes with claim released. Verify via "
                "record id task:${task_id} status flip + return code 0 "
                "(exits with 0). File reference: "
                "scripts/l7_dispatch_protocol.py."
            ),
        },
        "tools_block": list(ROLE_TOOL_ALLOWLIST["dispatch"]),
        "examples_block": [
            {"task": "dispatch task:abc12345 to L6 swarm",
             "output": {"iteration_id": "iter-2026-05-23-001",
                        "claim_token": "toon-red-dispatch-audit"}}
        ],
    }
    return prompt


_COMPOSERS = {
    ("L4", "coder"): _compose_l4_coder,
    ("L5", "manager"): _compose_l5_manager,
    ("L7", "dispatch"): _compose_l7_dispatch,
}


def build_prompt(task: Mapping[str, Any], project: Mapping[str, Any],
                  level: str, role: str) -> LPrompt:
    """Compose + validate an LPrompt for the given (task, project,
    level, role). Raises ``PromptValidationError`` when validation
    fails -- the LLM never sees a broken prompt.

    Composers are registered by (level, role) so cross-level bleed
    is structurally impossible. Missing pairs raise KeyError so the
    operator sees the gap immediately instead of getting a fallback
    that silently widens scope.
    """
    key = (level, role)
    composer = _COMPOSERS.get(key)
    if composer is None:
        raise KeyError(
            f"no LPrompt composer for {key!r}; known: {sorted(_COMPOSERS)}"
        )
    prompt = composer(task, project)
    validate_or_raise(prompt, level=level, role=role)
    return prompt
