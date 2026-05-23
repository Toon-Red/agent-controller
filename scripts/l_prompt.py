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

ALL_VALIDATORS = {
    "prompt_is_self_contained": prompt_is_self_contained,
    "prompt_has_done_when": prompt_has_done_when,
    "prompt_has_tool_allowlist": prompt_has_tool_allowlist,
}


def prompt_humanly_executable(prompt: LPrompt) -> tuple[bool, list[str]]:
    """Composite: runs all three named validators. ALL must pass for
    the prompt to be "humanly executable" -- a Preston stand-in
    could pick it up cold and produce the expected output.

    Each per-validator failure is tagged ``[validator_name] reason``
    in the aggregate so the operator log + Discord ping name the
    failing predicate.
    """
    failed: list[str] = []
    for name, fn in ALL_VALIDATORS.items():
        ok, reasons = fn(prompt)
        if not ok:
            for r in reasons:
                failed.append(f"[{name}] {r}")
    return (not failed, failed)


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
