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
    # 97346fa1: env-predicates require context_block.ui with real
    # content payloads + tools_block must satisfy coder-L4 spec
    # (read_file + run_tests + at least one of edit_file/write_file).
    base: LPrompt = {
        "level": "L4",
        "agent_role": "coder",
        "context_block": {
            "foo": [1, 2], "bar": "x",
            "ui": {
                "file_content": "def foo():\n    return 1\n",
                "test_log": "FAIL test_foo: expected 2, got 1",
            },
        },
        "task_block": {
            "inputs": "${foo}, ${bar}",
            "outputs": ["modified file"],
            "done_when": "test:test_foo passes (file `foo.py`)",
        },
        "tools_block": ["read_file", "edit_file", "run_tests"],
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


# ===========================================================================
# 97346fa1 -- environment-sufficiency predicates
# ===========================================================================

from scripts.l_prompt import (  # noqa: E402
    prompt_has_ui_context_block,
    prompt_tool_allowlist_covers_task,
    prompt_scope_is_self_contained_no_window_escape,
    check_env_only, check_shape_only,
    _clear_tool_requirements_cache, _record_finding, _finding_hash,
    FINDINGS_DIR,
)


# --- prompt_has_ui_context_block ------------------------------------------

def test_ui_block_passes_when_present_with_content():
    p = _ok_prompt()
    assert prompt_has_ui_context_block(p) == (True, [])


def test_ui_block_fails_when_missing():
    p = _ok_prompt()
    del p["context_block"]["ui"]
    ok, fails = prompt_has_ui_context_block(p)
    assert not ok and any("ui" in f and "missing" in f for f in fails)


def test_ui_block_fails_when_paths_only():
    """All values are bare short strings (paths/IDs) -> heuristic fail."""
    p = _ok_prompt(context_block={
        "foo": "x", "bar": "y",
        "ui": {"file_path": "foo.py", "task_id": "abc12345"},
    })
    ok, fails = prompt_has_ui_context_block(p)
    assert not ok and any("paths/IDs" in f for f in fails)


def test_ui_block_fails_when_empty_dict():
    p = _ok_prompt()
    p["context_block"]["ui"] = {}
    ok, fails = prompt_has_ui_context_block(p)
    assert not ok and any("empty" in f for f in fails)


# --- prompt_tool_allowlist_covers_task ------------------------------------

def test_tool_coverage_passes_for_coder_l4():
    p = _ok_prompt()
    p["task_block"]["category"] = "bug"
    assert prompt_tool_allowlist_covers_task(p) == (True, [])


def test_tool_coverage_fails_when_edit_missing_for_bug_fix():
    p = _ok_prompt(tools_block=["read_file", "run_tests"])  # no edit/write
    p["task_block"]["category"] = "bug"
    ok, fails = prompt_tool_allowlist_covers_task(p)
    assert not ok
    assert any("must_have_at_least_one_of" in f and "bug" in f.lower() for f in fails)


def test_tool_coverage_fails_when_no_spec_for_role(tmp_path, monkeypatch):
    """Fail-closed when no rule exists for the (role, level, category)."""
    _clear_tool_requirements_cache()
    monkeypatch.setenv("LEVEL_GATES_YAML", str(tmp_path / "empty.yaml"))
    (tmp_path / "empty.yaml").write_text("tool_requirements: {}\n", encoding="utf-8")
    p = _ok_prompt()
    ok, fails = prompt_tool_allowlist_covers_task(p)
    assert not ok and any("no tool_requirements spec" in f for f in fails)
    _clear_tool_requirements_cache()


def test_tool_coverage_falls_back_to_role_level_when_no_category():
    """coder-L4 generic rule applies when no category-specific match."""
    p = _ok_prompt()
    p["task_block"]["category"] = "obscure-category-not-in-yaml"
    # Should fall through to coder-L4 generic rule which is also satisfied.
    assert prompt_tool_allowlist_covers_task(p) == (True, [])


# --- prompt_scope_is_self_contained_no_window_escape ----------------------

def test_no_escape_passes_on_self_contained_done_when():
    p = _ok_prompt()
    assert prompt_scope_is_self_contained_no_window_escape(p) == (True, [])


def test_no_escape_fails_on_user_clicks_pattern():
    p = _ok_prompt(task_block={
        "inputs": "${foo}",
        "outputs": ["page rendered"],
        "done_when": "user clicks the button and confirms",
    })
    ok, fails = prompt_scope_is_self_contained_no_window_escape(p)
    assert not ok
    assert any("'user'" in f for f in fails)


def test_no_escape_fails_on_wait_for_pattern():
    p = _ok_prompt(task_block={
        "inputs": "${foo}", "outputs": ["x"],
        "done_when": "wait for the deploy to finish (test:smoke_passes file `deploy.py`)",
    })
    ok, fails = prompt_scope_is_self_contained_no_window_escape(p)
    assert not ok and any("wait" in f.lower() for f in fails)


def test_no_escape_passes_when_observer_tool_present():
    p = _ok_prompt(
        tools_block=["read_file", "edit_file", "run_tests", "wait_for_event"],
        task_block={
            "inputs": "${foo}", "outputs": ["x"],
            "done_when": "wait for the regression test to fire test:smoke (file `x.py`)",
        },
    )
    assert prompt_scope_is_self_contained_no_window_escape(p) == (True, [])


# --- composite ordering / aggregation -------------------------------------

def test_composite_runs_all_6_validators_and_aggregates():
    p = _ok_prompt(
        context_block={"foo": "x"},   # no ui
        task_block={"inputs": "${missing}", "outputs": ["x"],
                    "done_when": "looks reasonable"},
        tools_block=[],
    )
    ok, fails = prompt_humanly_executable(p)
    assert not ok
    tags = {f.split("]", 1)[0] for f in fails if f.startswith("[")}
    # At minimum: tool_allowlist (empty) + self_contained (missing ref)
    # + has_done_when (vibes) + ui_block (missing) all surface.
    assert "[prompt_has_tool_allowlist" in tags
    assert "[prompt_is_self_contained" in tags
    assert "[prompt_has_done_when" in tags
    assert "[prompt_has_ui_context_block" in tags


def test_check_env_only_returns_only_env_results():
    p = _ok_prompt()
    del p["context_block"]["ui"]   # env-failure
    p["task_block"]["done_when"] = "looks reasonable"   # shape-failure
    ok, fails = check_env_only(p)
    assert not ok
    # Only env predicates surface.
    tags = {f.split("]", 1)[0] for f in fails if f.startswith("[")}
    assert "[prompt_has_ui_context_block" in tags
    assert "[prompt_has_done_when" not in tags
    assert "[prompt_is_self_contained" not in tags


def test_check_shape_only_returns_only_shape_results():
    p = _ok_prompt()
    p["task_block"]["done_when"] = "looks reasonable"   # shape-failure
    del p["context_block"]["ui"]                          # env-failure
    ok, fails = check_shape_only(p)
    assert not ok
    tags = {f.split("]", 1)[0] for f in fails if f.startswith("[")}
    assert "[prompt_has_done_when" in tags
    assert "[prompt_has_ui_context_block" not in tags


# --- auto-file finding dedup ---------------------------------------------

def test_record_finding_writes_one_file_per_hash(tmp_path):
    p = _ok_prompt()
    failed = ["[prompt_has_ui_context_block] ui missing"]
    _record_finding(p, failed, base_dir=tmp_path)
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1


def test_record_finding_dedup_bumps_seen_count(tmp_path):
    p = _ok_prompt()
    failed = ["[prompt_has_ui_context_block] ui missing"]
    _record_finding(p, failed, base_dir=tmp_path)
    _record_finding(p, failed, base_dir=tmp_path)
    _record_finding(p, failed, base_dir=tmp_path)
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    import json as _j
    rec = _j.loads(files[0].read_text(encoding="utf-8"))
    assert rec["seen_count"] == 3


def test_finding_hash_is_deterministic_and_distinct():
    h1 = _finding_hash("coder", "L4", "p", "r1")
    h2 = _finding_hash("coder", "L4", "p", "r1")
    h3 = _finding_hash("coder", "L4", "p", "r2")
    assert h1 == h2 and h1 != h3 and len(h1) == 16


def test_composite_failure_records_finding(tmp_path, monkeypatch):
    """A composite failure auto-files via _record_finding."""
    monkeypatch.setenv("PROMPT_FINDINGS_DIR", str(tmp_path))
    p = _ok_prompt()
    del p["context_block"]["ui"]   # one env failure
    ok, fails = prompt_humanly_executable(p)
    assert not ok
    files = list(tmp_path.glob("*.json"))
    assert len(files) >= 1
