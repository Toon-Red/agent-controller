"""Tests for LPrompt + 4 validator predicates + prompt_emitter (09ca6f69)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.l_prompt import (  # noqa: E402
    LPrompt,
    PromptValidationError,
    prompt_is_self_contained,
    prompt_has_done_when,
    prompt_has_tool_allowlist,
    prompt_humanly_executable,
    validate_or_raise,
)


def _ok_prompt(**overrides) -> LPrompt:
    base: LPrompt = {
        "level": "L4",
        "agent_role": "coder",
        "context_block": {"foo": [1, 2], "bar": "x"},
        "task_block": {
            "inputs": "${foo}, ${bar}",
            "outputs": ["modified file"],
            "done_when": "test:test_foo passes (file `foo.py`)",
        },
        "tools_block": ["read_file", "edit_file"],
        "examples_block": [{"task": "x", "output": {"y": 1}}],
    }
    for k, v in overrides.items():
        base[k] = v
    return base


# ---------------------------------------------------------------------------
# prompt_is_self_contained
# ---------------------------------------------------------------------------

def test_self_contained_passes_when_refs_resolve():
    assert prompt_is_self_contained(_ok_prompt()) == (True, [])


def test_self_contained_passes_with_dotted_ref():
    p = _ok_prompt(task_block={
        "inputs": "${foo.bar.baz}",
        "outputs": ["x"],
        "done_when": "test:t passes (file `f.py`)",
    })
    # foo is in context_block, dotted access resolves to head
    assert prompt_is_self_contained(p) == (True, [])


def test_self_contained_fails_on_unresolved_ref():
    p = _ok_prompt(task_block={
        "inputs": "${missing_thing}",
        "outputs": ["x"],
        "done_when": "test:t passes (file `f.py`)",
    })
    ok, fails = prompt_is_self_contained(p)
    assert not ok
    assert any("missing_thing" in f for f in fails)


def test_self_contained_fails_when_context_block_missing():
    p = _ok_prompt()
    del p["context_block"]
    ok, fails = prompt_is_self_contained(p)
    assert not ok
    assert any("context_block" in f for f in fails)


def test_self_contained_handles_curly_brace_variant():
    p = _ok_prompt(task_block={
        "inputs": "{{foo}}",
        "outputs": ["x"],
        "done_when": "test:t passes (file `f.py`)",
    })
    assert prompt_is_self_contained(p) == (True, [])


# ---------------------------------------------------------------------------
# prompt_has_done_when
# ---------------------------------------------------------------------------

def test_done_when_passes_with_test_ref():
    assert prompt_has_done_when(_ok_prompt())[0] is True


def test_done_when_passes_with_file_path():
    p = _ok_prompt(task_block={
        "inputs": "${foo}", "outputs": ["x"],
        "done_when": "Modify routes/api.py and confirm",
    })
    assert prompt_has_done_when(p)[0] is True


def test_done_when_passes_with_record_id():
    p = _ok_prompt(task_block={
        "inputs": "${foo}", "outputs": ["x"],
        "done_when": "Close task:abc12345 with status=done.",
    })
    assert prompt_has_done_when(p)[0] is True


def test_done_when_fails_on_vibes_phrasing():
    p = _ok_prompt(task_block={
        "inputs": "${foo}", "outputs": ["x"],
        "done_when": "Output looks reasonable to a reader.",
    })
    ok, fails = prompt_has_done_when(p)
    assert not ok
    assert any("vibes" in f for f in fails)


def test_done_when_fails_on_pure_prose():
    p = _ok_prompt(task_block={
        "inputs": "${foo}", "outputs": ["x"],
        "done_when": "Task is complete when the work is done.",
    })
    ok, fails = prompt_has_done_when(p)
    assert not ok
    assert any("machine-checkable" in f for f in fails)


def test_done_when_fails_when_empty():
    p = _ok_prompt(task_block={"inputs": "${foo}", "outputs": ["x"],
                                 "done_when": ""})
    assert prompt_has_done_when(p)[0] is False


# ---------------------------------------------------------------------------
# prompt_has_tool_allowlist
# ---------------------------------------------------------------------------

def test_tool_allowlist_passes_with_non_empty_list():
    assert prompt_has_tool_allowlist(_ok_prompt()) == (True, [])


def test_tool_allowlist_fails_when_empty():
    p = _ok_prompt(tools_block=[])
    ok, fails = prompt_has_tool_allowlist(p)
    assert not ok
    assert any("hard-coded" in f for f in fails)


def test_tool_allowlist_fails_when_missing():
    p = _ok_prompt()
    del p["tools_block"]
    ok, fails = prompt_has_tool_allowlist(p)
    assert not ok


def test_tool_allowlist_fails_on_non_string_entries():
    p = _ok_prompt(tools_block=["read_file", "", 42])
    ok, fails = prompt_has_tool_allowlist(p)
    assert not ok


# ---------------------------------------------------------------------------
# prompt_humanly_executable (composite)
# ---------------------------------------------------------------------------

def test_composite_passes_on_clean_prompt():
    ok, fails = prompt_humanly_executable(_ok_prompt())
    assert ok and fails == []


def test_composite_aggregates_all_failures():
    p = _ok_prompt(
        context_block={},
        task_block={"inputs": "${missing}", "outputs": ["x"],
                    "done_when": "looks reasonable"},
        tools_block=[],
    )
    ok, fails = prompt_humanly_executable(p)
    assert not ok
    # All 3 validators surface; tagged with validator name.
    tags = {f.split("]", 1)[0] for f in fails if f.startswith("[")}
    assert "[prompt_is_self_contained" in tags
    assert "[prompt_has_done_when" in tags
    assert "[prompt_has_tool_allowlist" in tags


# ---------------------------------------------------------------------------
# validate_or_raise + PromptValidationError
# ---------------------------------------------------------------------------

def test_validate_or_raise_clean_returns_silently():
    validate_or_raise(_ok_prompt())   # no exception


def test_validate_or_raise_raises_on_failure():
    p = _ok_prompt(tools_block=[])
    with pytest.raises(PromptValidationError) as exc:
        validate_or_raise(p, level="L4", role="coder")
    assert exc.value.transition == "build_prompt(L4, coder)"


def test_prompt_validation_error_not_caught_by_except_exception():
    raised = []
    try:
        try:
            raise PromptValidationError(
                gate_id="x", failed_predicates=[],
                failing_record_id="r", transition="t",
            )
        except Exception:  # noqa: BLE001
            raised.append("Exception")
    except PromptValidationError:
        raised.append("PromptValidationError")
    assert raised == ["PromptValidationError"]


# ---------------------------------------------------------------------------
# prompt_emitter.build_prompt
# ---------------------------------------------------------------------------

def test_build_prompt_l4_coder():
    from scripts.prompt_emitter import build_prompt
    task = {"id": "t1", "project_id": "pd", "title": "Fix bug",
            "files_changed": ["pagination.py"],
            "tests": ["test_pagination::test_last_page"],
            "test_results": {},
            "acceptance_criteria": "Pagination handles edge."}
    project = {"project_id": "pd", "lifecycle": "alpha"}
    p = build_prompt(task, project, "L4", "coder")
    assert p["level"] == "L4"
    assert p["agent_role"] == "coder"
    assert "files_changed" in p["context_block"]
    # Cross-level bleed prevention: no project_state in L4 context.
    assert "project_state" not in p["context_block"]


def test_build_prompt_l7_dispatch():
    from scripts.prompt_emitter import build_prompt
    task = {"id": "t2", "project_id": "pd", "title": "Promote",
            "priority": "high"}
    project = {"project_id": "pd", "lifecycle": "beta",
               "open_tasks": [{"id": "t2"}]}
    p = build_prompt(task, project, "L7", "dispatch")
    assert p["level"] == "L7"
    assert p["agent_role"] == "dispatch"
    # Cross-level bleed prevention: no files_changed in L7 context.
    assert "files_changed" not in p["context_block"]
    assert "project_state" in p["context_block"]


def test_build_prompt_unknown_pair_raises_key_error():
    from scripts.prompt_emitter import build_prompt
    with pytest.raises(KeyError):
        build_prompt({"id": "x"}, {}, "L4", "queen")


def test_build_prompt_runs_validation_before_returning(monkeypatch):
    """If the composer produces a broken prompt, build_prompt raises
    PromptValidationError (the LLM never sees a bad prompt)."""
    from scripts import prompt_emitter as pe
    def bad_composer(task, project):
        return {"level": "L4", "agent_role": "coder",
                "context_block": {}, "task_block": {"done_when": ""},
                "tools_block": [], "examples_block": []}
    monkeypatch.setitem(pe._COMPOSERS, ("L4", "coder"), bad_composer)
    with pytest.raises(PromptValidationError):
        pe.build_prompt({"id": "x"}, {}, "L4", "coder")
