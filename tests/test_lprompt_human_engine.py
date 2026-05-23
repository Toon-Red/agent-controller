"""Tests for the LPrompt HumanEngine + dry_run_l_prompt CLI (09ca6f69).

Distinct from the older AC-S14 ``test_human_engine.py`` which tests
a different ``HumanDriver`` for mid-flight AI handoff. This file
tests the L-prompt validation-harness HumanEngine.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.human_engine import (  # noqa: E402
    HumanEngine, HumanEngineTimeout, AGENT_ENGINE_REGISTRY, get_agent_engine,
)
from scripts.l_prompt import LPrompt, PromptValidationError  # noqa: E402


def _good_prompt() -> LPrompt:
    return {
        "level": "L4", "agent_role": "coder",
        "context_block": {
            "foo": "x",
            "ui": {"file_content": "def foo(): return 1\n"},
        },
        "task_block": {
            "inputs": "${foo}", "outputs": ["modified file"],
            "done_when": "test:test_foo passes (file `foo.py`)",
        },
        "tools_block": ["read_file", "edit_file", "run_tests"],
        "examples_block": [{"task": "x", "output": {"y": 1}}],
    }


def test_human_engine_registers_via_get_agent_engine():
    eng = get_agent_engine("HumanEngine")
    assert isinstance(eng, HumanEngine)


def test_get_agent_engine_unknown_raises_key_error():
    with pytest.raises(KeyError):
        get_agent_engine("NopeEngine")


def test_human_engine_writes_prompt_file_and_returns_response(tmp_path):
    eng = HumanEngine(base_dir=tmp_path, poll_interval_sec=0.01)
    def drop_response():
        time.sleep(0.05)
        run_dirs = list(tmp_path.iterdir())
        if run_dirs:
            (run_dirs[0] / "L4-coder.response.md").write_text(
                "---\noutputs:\n  modified_file: foo.py\n---\nDone.\n",
                encoding="utf-8",
            )
    threading.Thread(target=drop_response, daemon=True).start()
    out = eng.run(_good_prompt(), timeout_sec=5)
    assert out.get("modified_file") == "foo.py"


def test_human_engine_renders_all_5_blocks_in_prompt_file(tmp_path):
    eng = HumanEngine(base_dir=tmp_path, poll_interval_sec=0.01)
    def drop_response():
        time.sleep(0.02)
        run_dirs = list(tmp_path.iterdir())
        if run_dirs:
            (run_dirs[0] / "L4-coder.response.md").write_text(
                "---\noutputs:\n  ok: true\n---\n", encoding="utf-8")
    threading.Thread(target=drop_response, daemon=True).start()
    eng.run(_good_prompt(), timeout_sec=5)
    run_dirs = list(tmp_path.iterdir())
    body = (run_dirs[0] / "L4-coder.md").read_text(encoding="utf-8")
    for block in ("## context_block", "## task_block", "## tools_block",
                   "## examples_block"):
        assert block in body


def test_human_engine_times_out_when_no_response(tmp_path):
    eng = HumanEngine(base_dir=tmp_path, poll_interval_sec=0.01)
    with pytest.raises(HumanEngineTimeout):
        eng.run(_good_prompt(), timeout_sec=0)


def test_human_engine_timeout_not_caught_by_except_exception(tmp_path):
    raised = []
    eng = HumanEngine(base_dir=tmp_path, poll_interval_sec=0.01)
    try:
        try:
            eng.run(_good_prompt(), timeout_sec=0)
        except Exception:  # noqa: BLE001
            raised.append("Exception")
    except HumanEngineTimeout:
        raised.append("HumanEngineTimeout")
    assert raised == ["HumanEngineTimeout"]


def test_human_engine_validates_prompt_before_writing(tmp_path):
    """Broken prompt never reaches disk; PromptValidationError propagates."""
    eng = HumanEngine(base_dir=tmp_path)
    bad: LPrompt = {"level": "L4", "agent_role": "coder",
                     "context_block": {}, "task_block": {"done_when": ""},
                     "tools_block": [], "examples_block": []}
    with pytest.raises(PromptValidationError):
        eng.run(bad, timeout_sec=1)
    assert list(tmp_path.iterdir()) == []


def test_human_engine_fires_ping_when_provided(tmp_path):
    ping = MagicMock()
    eng = HumanEngine(base_dir=tmp_path, ping=ping, poll_interval_sec=0.01)
    def drop_response():
        time.sleep(0.02)
        run_dirs = list(tmp_path.iterdir())
        if run_dirs:
            (run_dirs[0] / "L4-coder.response.md").write_text(
                "---\noutputs:\n  ok: true\n---\n", encoding="utf-8")
    threading.Thread(target=drop_response, daemon=True).start()
    eng.run(_good_prompt(), timeout_sec=5)
    ping.assert_called_once()


def test_human_engine_returns_raw_body_when_no_frontmatter(tmp_path):
    eng = HumanEngine(base_dir=tmp_path, poll_interval_sec=0.01)
    def drop_response():
        time.sleep(0.02)
        run_dirs = list(tmp_path.iterdir())
        if run_dirs:
            (run_dirs[0] / "L4-coder.response.md").write_text(
                "plain markdown, no frontmatter\n", encoding="utf-8")
    threading.Thread(target=drop_response, daemon=True).start()
    out = eng.run(_good_prompt(), timeout_sec=5)
    assert "raw_body" in out


# ---------------------------------------------------------------------------
# dry-run CLI
# ---------------------------------------------------------------------------

def _load_cli_module():
    import importlib.util
    p = Path(__file__).resolve().parent.parent / "scripts" / "dry_run_l_prompt.py"
    spec = importlib.util.spec_from_file_location("_dryrun_lp", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dry_run_cli_human_output_includes_validator_message(capsys):
    mod = _load_cli_module()
    rc = mod.main(["--task", "fake-id-1234", "--level", "L4", "--role", "coder"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "LPrompt dry-run" in out
    assert "OK: prompt passes" in out
    assert "context_block" in out


def test_dry_run_cli_json_mode_emits_parseable_payload(capsys):
    mod = _load_cli_module()
    rc = mod.main(["--task", "fake-id-2", "--level", "L7", "--role", "dispatch",
                    "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["valid"] is True
    assert payload["prompt"]["level"] == "L7"


def test_dry_run_cli_unknown_role_returns_exit_2(capsys):
    mod = _load_cli_module()
    rc = mod.main(["--task", "x", "--level", "L4", "--role", "queen"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no composer" in err


def test_dry_run_cli_check_env_only_flag(capsys):
    mod = _load_cli_module()
    rc = mod.main(["--task", "fake-env-id", "--level", "L4", "--role", "coder",
                    "--check-env-only"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "env" in out.lower()
