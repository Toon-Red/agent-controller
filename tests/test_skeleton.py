"""Skeleton tests for agent-controller (AC-S13 bootstrap)."""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_package_json_declares_claude_flow():
    data = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    deps = data.get("dependencies") or {}
    assert "claude-flow" in deps, "claude-flow must be declared as a dep"
    # Pinned exact version (no leading ^ or ~), per upgrade policy.
    version = deps["claude-flow"]
    assert not version.startswith(("^", "~")), (
        "claude-flow must be pinned to an exact version "
        f"(got {version!r}); see docs/upgrade-claude-flow.md"
    )


def test_pyproject_metadata_present():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "agent-controller"' in text
    assert 'requires-python = ">=3.10"' in text


def test_upgrade_doc_exists():
    assert (ROOT / "docs" / "upgrade-claude-flow.md").is_file()


def test_health_check_hook_exists():
    assert (ROOT / ".claude" / "hooks" / "health_check.py").is_file()


def test_readme_states_scope():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "L4-L8 agent hierarchy" in text
    assert "claude-flow" in text
    assert "do NOT fork or modify upstream" in text
