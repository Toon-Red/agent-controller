"""LPrompt typed shape + validator predicates (09ca6f69 AC-HUMAN-ENGINE1).

Every L4/L5/L6/L7 agent invocation goes through this fixed 5-block
shape so an LLM doesn't have to remember scope, and a human stand-in
could replace any L-step and produce equivalent output from the same
prompt. The 4 validator predicates enforce "humanly executable":
self-contained, machine-checkable done-when, tool allowlist,
composite check.

Preston rule (2026-05-23): every L invocation has a hard-coded scope
so an LLM doesn't need to remember. The HumanEngine (sibling
``scripts/human_engine.py``) is the VALIDATION harness, not a
production engine -- it proves the L assignment is reasonable by
proving a human could execute it.

Validator signature contract (mirrors level_gates predicates from
``3f83494b``)::

    def validator(prompt: LPrompt) -> tuple[bool, list[str]]

``PromptValidationError`` inherits from ``LevelGateViolation`` so it
flows through the same exception machinery + Discord ping path.
"""
from __future__ import annotations

import re
from typing import Any, Literal, Mapping, TypedDict

from scripts.level_gates import LevelGateViolation

LEVEL = Literal["L4", "L5", "L6", "L7"]


class LPrompt(TypedDict, total=False):
    """The 5-block prompt shape. ``total=False`` so partial dicts can
    flow through validators that report missing fields explicitly
    rather than crashing on KeyError."""
    level: str          # L4 | L5 | L6 | L7
    agent_role: str     # coder | qa | playtester | grader | manager | queen | dispatch
    context_block: dict[str, Any]   # all data the agent needs
    task_block: dict[str, Any]      # inputs -> outputs -> done_when
    tools_block: list[str]          # tool names the agent may call
    examples_block: list[dict]      # 1-2 golden outputs for shape


class PromptValidationError(LevelGateViolation):
    """Raised when a prompt fails ``prompt_humanly_executable``.

    Inherits from ``LevelGateViolation`` (-> ``BaseException``) so it
    cannot be swallowed by ``except Exception`` -- matches the
    Preston rule that enforcement-class refusals must propagate.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Reference tokens inside task_block strings. We look for "${key}" and
# "{{key}}" placeholders which is the convention prompt_emitter uses.
_REF_RE = re.compile(r"(?:\$\{|\{\{)([a-zA-Z_][a-zA-Z0-9_.]*)(?:\}\}|\})")

# "Vibes" phrasing in done_when that we refuse: pure prose with no
# machine-checkable hook.
_VIBES_RE = re.compile(
    r"\b(looks (?:reasonable|good|ok|fine)|seems (?:right|reasonable|fine)|"
    r"(?:use|trust) (?:your |my )?judgment|gut check)\b",
    re.IGNORECASE,
)

# Done-when must reference at least one of these machine-checkable shapes.
_MACHINE_CHECKABLE_RE = re.compile(
    r"\btest:[\w./-]+|"                       # test:foo::bar or test:path/to.py
    r"\b[a-zA-Z_][\w./-]*\.(?:py|md|json|yaml|yml|ts|js|tsx|jsx)\b|"  # file path
    r"\b(?:task|request|research):[a-f0-9-]{4,}|"  # record id
    r"`[A-Za-z_][\w.]*\(`?|"                   # callable like `foo(`
    r"\bcount\s*[<>=]+\s*\d+|"                  # numeric assertion
    r"\b(?:returns?|exits?\s+with)\s+\d+"      # exit code / return code
)


def _all_text_from(node: Any) -> str:
    """Flatten nested dict/list/str values into one big string so
    placeholder + machine-checkable scans see everything in task_block."""
    if isinstance(node, str):
        return node
    if isinstance(node, Mapping):
        return " ".join(_all_text_from(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return " ".join(_all_text_from(v) for v in node)
    return ""


# ---------------------------------------------------------------------------
# Validator 1: self-contained
# ---------------------------------------------------------------------------

def prompt_is_self_contained(prompt: LPrompt) -> tuple[bool, list[str]]:
    """Every ``${key}`` / ``{{key}}`` reference in task_block resolves
    to a key present in context_block. The LLM (or human stand-in)
    never needs to "go look elsewhere"; explicit data only.

    Resolution rule: ``${foo}`` resolves if ``foo`` (or its first dot-
    segment for ``${foo.bar}``) exists as a key in
    ``context_block``. Missing context_block fails the check
    outright -- the prompt isn't self-contained if it has no context.
    """
    fails: list[str] = []
    ctx = prompt.get("context_block")
    task = prompt.get("task_block")
    if not isinstance(ctx, Mapping):
        fails.append("context_block missing or not a mapping")
    if not isinstance(task, Mapping):
        fails.append("task_block missing or not a mapping")
        return (False, fails)
    text = _all_text_from(task)
    refs = _REF_RE.findall(text)
    ctx_keys = set(ctx.keys()) if isinstance(ctx, Mapping) else set()
    for ref in sorted(set(refs)):
        head = ref.split(".", 1)[0]
        if head not in ctx_keys:
            fails.append(
                f"task_block references {ref!r} but context_block has no "
                f"key {head!r} (available: {sorted(ctx_keys)})"
            )
    return (not fails, fails)


# ---------------------------------------------------------------------------
# Validator 2: machine-checkable done_when
# ---------------------------------------------------------------------------

def prompt_has_done_when(prompt: LPrompt) -> tuple[bool, list[str]]:
    """``task_block.done_when`` is non-empty AND references at least
    one machine-checkable hook: test name, file path, record id,
    callable, count assertion, or exit code. Pure prose ("looks
    reasonable") fails."""
    task = prompt.get("task_block")
    if not isinstance(task, Mapping):
        return (False, ["task_block missing"])
    dw = task.get("done_when")
    if not dw:
        return (False, ["task_block.done_when missing or empty"])
    text = dw if isinstance(dw, str) else _all_text_from(dw)
    if _VIBES_RE.search(text):
        return (False, [
            f"done_when contains vibes-only phrasing: {text[:120]!r}"
        ])
    if not _MACHINE_CHECKABLE_RE.search(text):
        return (False, [
            f"done_when has no machine-checkable hook "
            f"(test:/file path/record id/callable/count/exit code): "
            f"{text[:120]!r}"
        ])
    return (True, [])


# ---------------------------------------------------------------------------
# Validator 3: tool allowlist
# ---------------------------------------------------------------------------

def prompt_has_tool_allowlist(prompt: LPrompt) -> tuple[bool, list[str]]:
    """``tools_block`` is a non-empty list of strings. An empty tools
    list means the agent is implicitly allowed everything -- which
    breaks the Preston rule's "hard-coded scope" constraint."""
    tools = prompt.get("tools_block")
    if not isinstance(tools, list):
        return (False, ["tools_block missing or not a list"])
    if not tools:
        return (False, [
            "tools_block is empty -- prompt has no tool allowlist; agent "
            "would have unbounded tool access. Per Preston rule, scope "
            "must be hard-coded."
        ])
    bad = [str(t) for t in tools if not isinstance(t, str) or not t.strip()]
    if bad:
        return (False, [f"tools_block has non-string / empty entries: {bad}"])
    return (True, [])


# ---------------------------------------------------------------------------
# Validator 4: composite (the "humanly executable" check)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Validators 4-6: environment sufficiency (97346fa1 AC-AGENT-ENV1)
# ---------------------------------------------------------------------------
#
# Preston rule (2026-05-23, verbatim): "It's more than just prompt. If
# the UI and such is not good, or tools available don't help with
# solving the tasks, or the user has to go outside the window to make
# progress, then it's not fully thought out yet, and it needs to be
# better prepared for."
#
# These predicates check the AGENT'S ENVIRONMENT is sufficient -- not
# just the prompt shape but whether the agent has the surfaces and
# tools it needs to actually finish the task without escaping the
# window.

_WINDOW_ESCAPE_RE = re.compile(
    r"\b(user|preston|manual(?:ly)?|external|wait\s+(?:for|until)|"
    r"someone\s+(?:has\s+to|must|needs?\s+to))\b",
    re.IGNORECASE,
)

# Tool requirements YAML loader. Cached per yaml path so repeated
# validation in a tight loop doesn't re-read the file.
_TOOL_REQUIREMENTS_CACHE: dict[str, dict] = {}


def _load_tool_requirements(yaml_path: Optional[str] = None) -> dict[str, dict]:
    """Read ``level_gates.yaml > tool_requirements:`` block."""
    import os
    from pathlib import Path
    if yaml_path is None:
        yaml_path = os.environ.get("LEVEL_GATES_YAML")
        if not yaml_path:
            yaml_path = str(Path(__file__).resolve().parent.parent / "level_gates.yaml")
    if yaml_path in _TOOL_REQUIREMENTS_CACHE:
        return _TOOL_REQUIREMENTS_CACHE[yaml_path]
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
        out = data.get("tool_requirements") or {}
        if not isinstance(out, Mapping):
            out = {}
        out = dict(out)
    except Exception:
        out = {}
    _TOOL_REQUIREMENTS_CACHE[yaml_path] = out
    return out


def _clear_tool_requirements_cache() -> None:
    """Test helper -- force re-read on next call."""
    _TOOL_REQUIREMENTS_CACHE.clear()


def prompt_has_ui_context_block(prompt: LPrompt) -> tuple[bool, list[str]]:
    """``context_block.ui`` exists and contains actual content payloads
    (not just paths/IDs). L4 coder needs file content, not just paths;
    L5 manager needs diff text, not just a PR link; L7 dispatch needs
    task records, not just task IDs."""
    fails: list[str] = []
    ctx = prompt.get("context_block")
    if not isinstance(ctx, Mapping):
        return (False, ["context_block missing or not a mapping"])
    ui = ctx.get("ui")
    if ui is None:
        return (False, [
            "context_block.ui is missing -- the agent needs an explicit "
            "ui block enumerating the surfaces it operates on "
            "(file contents, diff text, task records, etc.), not just "
            "pointers to them"
        ])
    if not isinstance(ui, Mapping):
        return (False, [f"context_block.ui must be a mapping, got {type(ui).__name__}"])
    if not ui:
        return (False, ["context_block.ui is empty"])
    # Detect path-only / id-only payloads (heuristic): values that are
    # bare strings with no whitespace and < 200 chars are likely paths
    # or IDs, not content. Real content has whitespace.
    suspect_paths: list[str] = []
    for key, val in ui.items():
        if isinstance(val, str) and val.strip():
            if len(val) < 200 and "\n" not in val and " " not in val:
                suspect_paths.append(f"{key}={val!r}")
    if suspect_paths and len(suspect_paths) == len(ui):
        return (False, [
            f"context_block.ui contains only short bare strings (looks "
            f"like paths/IDs, not content payloads): {suspect_paths}"
        ])
    return (True, [])


def _resolve_tool_requirement_key(role: str, level: str,
                                  category: str) -> Optional[str]:
    """Resolution order: <role>-<level>-<category>, <role>-<level>."""
    candidates = [
        f"{role}-{level}-{category}".lower(),
        f"{role}-{level}".lower(),
    ]
    reqs = _load_tool_requirements()
    # Normalize the YAML keys to lowercase for case-insensitive match.
    reqs_lower = {k.lower(): v for k, v in reqs.items()}
    for key in candidates:
        if key in reqs_lower:
            return key
    return None


def prompt_tool_allowlist_covers_task(prompt: LPrompt) -> tuple[bool, list[str]]:
    """``tools_block`` satisfies the role's declared requirements from
    ``level_gates.yaml > tool_requirements``. Fail-closed when no
    requirement spec exists for the (role, level, category) -- never
    silently pass an unverified role."""
    role = str(prompt.get("agent_role") or "")
    level = str(prompt.get("level") or "")
    task = prompt.get("task_block")
    category = ""
    if isinstance(task, Mapping):
        category = str(task.get("category") or "")
    if not role or not level:
        return (False, [
            f"prompt missing agent_role or level (role={role!r}, level={level!r})"
        ])
    key = _resolve_tool_requirement_key(role, level, category)
    if key is None:
        return (False, [
            f"no tool_requirements spec for (role={role!r}, level={level!r}, "
            f"category={category!r}) in level_gates.yaml -- fail-closed; "
            f"add a rule before this role+task-kind can be enforced"
        ])
    reqs = _load_tool_requirements()
    reqs_lower = {k.lower(): v for k, v in reqs.items()}
    spec = reqs_lower[key]
    if not isinstance(spec, Mapping):
        return (False, [f"tool_requirements[{key!r}] is not a mapping"])
    tools = prompt.get("tools_block") or []
    if not isinstance(tools, list):
        return (False, ["tools_block missing or not a list"])
    tools_set = {str(t).strip() for t in tools if isinstance(t, str)}
    fails: list[str] = []
    must_all = spec.get("must_have_all_of") or []
    if isinstance(must_all, list):
        missing = [t for t in must_all if t not in tools_set]
        if missing:
            fails.append(
                f"missing required tools (must_have_all_of) for rule "
                f"{key!r}: {missing}"
            )
    must_one = spec.get("must_have_at_least_one_of") or []
    if isinstance(must_one, list) and must_one:
        if not any(t in tools_set for t in must_one):
            fails.append(
                f"tools_block has none of the alternatives "
                f"(must_have_at_least_one_of) for rule {key!r}: "
                f"need one of {list(must_one)}, got {sorted(tools_set)}"
            )
    return (not fails, fails)


def prompt_scope_is_self_contained_no_window_escape(
        prompt: LPrompt) -> tuple[bool, list[str]]:
    """``task_block.done_when`` and ``task_block.outputs`` reference
    only entities reachable from context_block + tools_block. Window-
    escape verbs (user, preston, manual, external, wait for, someone)
    are refused unless the tools_block carries something that could
    observe the externally-driven event."""
    task = prompt.get("task_block")
    if not isinstance(task, Mapping):
        return (False, ["task_block missing"])
    fragments = []
    if "done_when" in task and task["done_when"]:
        fragments.append(("done_when",
                          task["done_when"] if isinstance(task["done_when"], str)
                          else _all_text_from(task["done_when"])))
    if "outputs" in task and task["outputs"]:
        fragments.append(("outputs", _all_text_from(task["outputs"])))
    tools = prompt.get("tools_block") or []
    tools_set = {str(t).lower() for t in tools if isinstance(t, str)}
    has_observer = any(
        t in tools_set
        for t in ("wait_for_event", "observe", "poll", "subscribe",
                  "browser_screenshot", "screenshot")
    )
    fails: list[str] = []
    for field_name, text in fragments:
        for m in _WINDOW_ESCAPE_RE.finditer(text):
            verb = m.group(0)
            if has_observer:
                continue
            fails.append(
                f"{field_name} contains window-escape verb {verb!r} "
                f"with no observer tool in tools_block (need one of: "
                f"wait_for_event / observe / poll / subscribe / "
                f"browser_screenshot)"
            )
    return (not fails, fails)


ALL_VALIDATORS = {
    "prompt_is_self_contained": prompt_is_self_contained,
    "prompt_has_done_when": prompt_has_done_when,
    "prompt_has_tool_allowlist": prompt_has_tool_allowlist,
    # 97346fa1 environment-sufficiency predicates
    "prompt_has_ui_context_block": prompt_has_ui_context_block,
    "prompt_tool_allowlist_covers_task": prompt_tool_allowlist_covers_task,
    "prompt_scope_is_self_contained_no_window_escape":
        prompt_scope_is_self_contained_no_window_escape,
}

# Subsets so the CLI can run env-only or shape-only checks.
SHAPE_VALIDATORS = {
    "prompt_is_self_contained": prompt_is_self_contained,
    "prompt_has_done_when": prompt_has_done_when,
    "prompt_has_tool_allowlist": prompt_has_tool_allowlist,
}
ENV_VALIDATORS = {
    "prompt_has_ui_context_block": prompt_has_ui_context_block,
    "prompt_tool_allowlist_covers_task": prompt_tool_allowlist_covers_task,
    "prompt_scope_is_self_contained_no_window_escape":
        prompt_scope_is_self_contained_no_window_escape,
}


def _run_subset(prompt: LPrompt, subset: dict) -> tuple[bool, list[str]]:
    failed: list[str] = []
    for name, fn in subset.items():
        ok, reasons = fn(prompt)
        if not ok:
            for r in reasons:
                failed.append(f"[{name}] {r}")
    return (not failed, failed)


def prompt_humanly_executable(prompt: LPrompt) -> tuple[bool, list[str]]:
    """Composite: runs all six named validators (3 shape + 3 env).
    ALL must pass for the prompt to be 'humanly executable' AND have
    sufficient environment -- a Preston stand-in could pick it up
    cold, AND would have the surfaces + tools to actually finish.

    Order: cheap pre-checks first (has_tool_allowlist), then expensive
    (regex scans, YAML loader). Each per-validator failure tagged
    ``[validator_name] reason`` in the aggregate so the operator log
    + Discord ping name the failing predicate.
    """
    # Cheap checks first.
    cheap = ("prompt_has_tool_allowlist", "prompt_is_self_contained",
             "prompt_has_done_when", "prompt_has_ui_context_block")
    expensive = ("prompt_tool_allowlist_covers_task",
                  "prompt_scope_is_self_contained_no_window_escape")
    ordered = {k: ALL_VALIDATORS[k] for k in cheap + expensive}
    passed, failed = _run_subset(prompt, ordered)
    if not passed:
        try:
            _record_finding(prompt, failed)
        except Exception:
            pass   # finding-record failure never swallows the violation
    return (passed, failed)


def check_env_only(prompt: LPrompt) -> tuple[bool, list[str]]:
    """Run ONLY the 3 environment-sufficiency predicates. Used by the
    --check-env-only CLI flag for fast operator spot-checks."""
    return _run_subset(prompt, ENV_VALIDATORS)


def check_shape_only(prompt: LPrompt) -> tuple[bool, list[str]]:
    """Run ONLY the 3 prompt-shape predicates from 09ca6f69."""
    return _run_subset(prompt, SHAPE_VALIDATORS)


# ---------------------------------------------------------------------------
# Auto-file findings with dedup-by-content-hash (97346fa1)
# ---------------------------------------------------------------------------
#
# Same mechanism as 5214e749's wiki_health auto-file: each finding
# gets a stable sha256 hash of (role, level, predicate, reason). The
# hash is the filename + the content. Repeat of the same finding
# bumps a ``seen_count`` rather than re-filing.

import hashlib as _hashlib
import json as _json
from pathlib import Path as _Path

FINDINGS_DIR = _Path(__file__).resolve().parent.parent / "data" / "prompt-validation-findings"


def _finding_hash(role: str, level: str, predicate: str, reason: str) -> str:
    h = _hashlib.sha256()
    h.update(role.encode("utf-8"))
    h.update(b"\0")
    h.update(level.encode("utf-8"))
    h.update(b"\0")
    h.update(predicate.encode("utf-8"))
    h.update(b"\0")
    h.update(reason.encode("utf-8"))
    return h.hexdigest()[:16]


def _record_finding(prompt: LPrompt, failed: list[str],
                    base_dir: Optional[_Path] = None) -> None:
    """Write one .json per failing predicate with dedup-by-hash."""
    import os as _os
    target_dir = base_dir
    if target_dir is None:
        env_override = _os.environ.get("PROMPT_FINDINGS_DIR")
        target_dir = _Path(env_override) if env_override else FINDINGS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    role = str(prompt.get("agent_role") or "?")
    level = str(prompt.get("level") or "?")
    task = prompt.get("task_block") or {}
    task_id = ""
    ctx = prompt.get("context_block") or {}
    if isinstance(ctx, Mapping):
        task_id = str(ctx.get("task_id") or ctx.get("id") or "")
    for entry in failed:
        # entry looks like "[predicate_name] reason text..."
        if entry.startswith("[") and "] " in entry:
            pred, reason = entry[1:].split("] ", 1)
        else:
            pred, reason = "?", entry
        h = _finding_hash(role, level, pred, reason)
        path = target_dir / f"{h}.json"
        if path.is_file():
            # Bump seen_count atomically (best-effort).
            try:
                existing = _json.loads(path.read_text(encoding="utf-8"))
                existing["seen_count"] = int(existing.get("seen_count", 1)) + 1
                existing["last_seen_at"] = _utc_iso()
                path.write_text(_json.dumps(existing, indent=2,
                                              ensure_ascii=False),
                                  encoding="utf-8")
            except Exception:
                pass
            continue
        from datetime import datetime as _dt, timezone as _tz
        rec = {
            "hash": h,
            "role": role,
            "level": level,
            "task_id": task_id,
            "failing_predicate": pred,
            "reason": reason,
            "evidence": {
                "tools_block": prompt.get("tools_block"),
                "task_block_keys": list(task.keys()) if isinstance(task, Mapping) else [],
                "context_block_keys": list(ctx.keys()) if isinstance(ctx, Mapping) else [],
            },
            "first_seen_at": _utc_iso(),
            "last_seen_at": _utc_iso(),
            "seen_count": 1,
        }
        path.write_text(_json.dumps(rec, indent=2, ensure_ascii=False),
                          encoding="utf-8")


def _utc_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def validate_or_raise(prompt: LPrompt, *, level: str = "?",
                       role: str = "?") -> None:
    """Convenience wrapper: run composite validator + raise
    ``PromptValidationError`` on refusal so callers can rely on
    BaseException propagation rather than checking return values."""
    ok, fails = prompt_humanly_executable(prompt)
    if ok:
        return
    raise PromptValidationError(
        gate_id="prompt_humanly_executable",
        failed_predicates=fails,
        failing_record_id=f"{level}::{role}",
        transition=f"build_prompt({level}, {role})",
    )
