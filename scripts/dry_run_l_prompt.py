"""Dry-run LPrompt builder CLI (09ca6f69).

Used by Preston to user-test prompt reasonableness BEFORE turning
the LLM loose. NO engine invocation -- just build, validate, print.

Usage::

    python scripts/dry_run_l_prompt.py --task <id> --level L4 --role coder
    python scripts/dry_run_l_prompt.py --task <id> --level L7 --role dispatch --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _fetch_task(task_id: str) -> dict:
    """Best-effort task fetch via PD HTTP. Returns the minimal stub
    when PD unreachable so the dry-run can still demonstrate the
    prompt shape against a synthetic task."""
    import os
    import urllib.request
    base = os.environ.get("PD_API_BASE", "http://127.0.0.1:5100")
    try:
        # The PD API doesn't expose a task-by-id without project_id.
        # Walk projects until we find the task.
        with urllib.request.urlopen(f"{base}/api/tasks/all", timeout=3) as r:
            data = json.loads(r.read())
        for t in (data.get("tasks") or []):
            if t.get("id") == task_id:
                return dict(t)
    except Exception:
        pass
    # Synthetic stub so --task <fake-id> still produces output.
    return {
        "id": task_id, "project_id": "pipeline-dashboard",
        "title": f"(synthetic stub for {task_id})",
        "status": "todo", "priority": "normal", "category": "feature",
        "description": "Stub task body; PD was unreachable or task absent.",
        "tests": [], "test_results": {}, "files_changed": [],
        "deliverables": [], "decisions": [],
        "acceptance_criteria": "Acceptance criteria placeholder.",
    }


def _fetch_project(project_id: str) -> dict:
    return {
        "project_id": project_id,
        "lifecycle": "alpha",
        "open_tasks": [],
        "recent_decisions": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task", required=True, help="PD task id")
    parser.add_argument("--level", required=True,
                         choices=["L4", "L5", "L6", "L7"])
    parser.add_argument("--role", required=True,
                         help="agent role (coder/qa/manager/dispatch/...)")
    parser.add_argument("--json", action="store_true",
                         help="emit the prompt as JSON instead of human text")
    args = parser.parse_args(argv)

    from scripts.prompt_emitter import build_prompt
    from scripts.l_prompt import (
        prompt_humanly_executable, PromptValidationError,
    )

    task = _fetch_task(args.task)
    project = _fetch_project(task.get("project_id", ""))
    try:
        prompt = build_prompt(task, project, args.level, args.role)
        valid = True
        validator_msg = "OK: prompt passes all 3 validators."
    except PromptValidationError as exc:
        # Run the composite validator manually so we still print
        # the would-be prompt + the per-validator failures.
        prompt = None
        valid = False
        validator_msg = f"FAIL: {exc.failed_predicates}"
    except KeyError as exc:
        print(f"FATAL: no composer for "
              f"({args.level!r}, {args.role!r}): {exc}",
              file=sys.stderr)
        return 2

    if args.json:
        out = {
            "valid": valid,
            "validator_message": validator_msg,
            "prompt": prompt,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0 if valid else 1

    print(f"=== LPrompt dry-run ({args.level} {args.role}, task={args.task}) ===")
    print(validator_msg)
    print()
    if prompt is None:
        print("(prompt not built; see validator failures above)")
        return 1
    for block in ("context_block", "task_block", "tools_block",
                   "examples_block"):
        print(f"--- {block} ---")
        print(json.dumps(prompt.get(block), indent=2, ensure_ascii=False,
                          default=str))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
