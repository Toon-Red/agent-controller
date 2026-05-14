"""AC-S16a: tests for the L8 PM agent template skeleton."""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "agents" / "L8-project-manager.md"


def _read():
    return TEMPLATE.read_text(encoding="utf-8")


def _parse_frontmatter():
    text = _read()
    assert text.startswith("---\n"), "template must start with YAML frontmatter"
    end = text.find("\n---\n", 4)
    assert end > 0, "frontmatter must end with closing ---"
    import yaml
    return yaml.safe_load(text[4:end]), text[end + 5:]


# ---------------------------------------------------------------------------
# Template presence + frontmatter shape
# ---------------------------------------------------------------------------

def test_template_file_exists():
    assert TEMPLATE.is_file()


def test_frontmatter_required_fields():
    fm, _ = _parse_frontmatter()
    for field in ("name", "level", "engine", "description"):
        assert field in fm, f"frontmatter missing required {field!r}"
    assert fm["name"] == "L8-project-manager"
    assert fm["level"] == "L8"
    assert fm["engine"] == "claude-opus"


def test_read_scopes_present():
    fm, _ = _parse_frontmatter()
    scopes = fm.get("read_scopes") or []
    expected = {"pipeline-dashboard:tasks", "pipeline-dashboard:research",
                "pipeline-dashboard:requests", "pipeline-dashboard:projects",
                "calendar:events"}
    assert expected.issubset(set(scopes)), (
        f"missing read scopes: {expected - set(scopes)}"
    )


def test_both_conversation_surfaces_declared():
    fm, _ = _parse_frontmatter()
    surfaces = set(fm.get("conversation_surfaces") or [])
    assert {"dream_tab", "discord_async"} == surfaces, (
        f"conversation_surfaces must be exactly dream_tab + discord_async "
        f"per AC-S16 2026-05-13 decision; got {surfaces!r}"
    )


def test_template_variables_present():
    fm, _ = _parse_frontmatter()
    vars_ = fm.get("template_variables") or {}
    required = {"engine", "pd_endpoint", "calendar_endpoint",
                "discord_webhook", "dream_tab_id"}
    assert required.issubset(set(vars_.keys())), (
        f"template_variables missing: {required - set(vars_.keys())}"
    )


# ---------------------------------------------------------------------------
# Body content invariants
# ---------------------------------------------------------------------------

def test_body_documents_two_preston_interfaces():
    _, body = _parse_frontmatter()
    assert "Preston <-> L8" in body
    assert "Preston <-> L7 Dispatch" in body


def test_body_documents_hierarchy_with_corrections():
    _, body = _parse_frontmatter()
    # Grader is L4 high-compute per the 2026-05-13 correction --
    # NOT L5 like the original draft.
    assert "grader" in body.lower()
    assert "L4" in body
    # L5 framed as "managers + guides", NOT "grading".
    assert "managers + guides" in body


def test_body_references_claude_flow_not_ruflow():
    """Naming clarity sweep: body refers to claude-flow (upstream)
    by its canonical name; doesn't conflate with our agent-controller
    repo or the legacy customisation workspace."""
    _, body = _parse_frontmatter()
    assert "claude-flow" in body, (
        "L6 description should reference claude-flow (upstream)"
    )
    # "ruflow" alone (the ambiguous old name) should NOT appear in
    # the body. The legacy customisation workspace can be referenced
    # only by its full path.
    standalone_ruflow_uses = [
        line for line in body.lower().split("\n")
        if "ruflow" in line
        and "~/desktop/code/ruflow" not in line
        and "legacy" not in line
        and "naming" not in line
    ]
    assert standalone_ruflow_uses == [], (
        f"standalone 'ruflow' uses leaked into body: "
        f"{standalone_ruflow_uses}"
    )


def test_body_documents_what_l8_does_not_do():
    _, body = _parse_frontmatter()
    assert "do NOT do" in body or "DO NOT do" in body or "you do not" in body.lower()


def test_engine_default_is_opus():
    """Per Preston 2026-05-13 sign-off."""
    fm, body = _parse_frontmatter()
    assert fm["engine"] == "claude-opus"
    assert "claude-opus" in body
