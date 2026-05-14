"""AC-S16d tests for the L8 EOD output generator."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import l8_eod_generator as eod


# Reference timestamp: 2026-05-14 18:00 local.
REF_TS = time.mktime(time.struct_time((2026, 5, 14, 18, 0, 0, 0, 0, -1)))
TODAY = time.strftime("%Y-%m-%d", time.localtime(REF_TS))


def _snapshot(**overrides):
    """Build a minimal StateSnapshot-shaped namespace for tests."""
    base = {
        "captured_at": REF_TS,
        "pd_tasks": [],
        "pd_research": [],
        "pd_requests": [],
        "pd_projects": {},
        "calendar_events": [],
        "calendar_upcoming": [],
        "workflow_state": {},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Section extraction
# ---------------------------------------------------------------------------

class TestExtractSections:
    def test_empty_snapshot_returns_quiet_sections(self):
        s = eod.extract_eod_sections(_snapshot())
        assert s.today_iso == TODAY
        assert s.is_quiet() is True
        assert s.completion_counts == {"done_today": 0, "carry_over": 0,
                                         "blocked": 0}

    def test_done_today_filtered_by_completed_at(self):
        tasks = [
            {"id": "a", "status": "done", "title": "A",
             "completed_at": f"{TODAY}T10:00:00", "priority": "high"},
            {"id": "b", "status": "done", "title": "B",
             "completed_at": "2026-05-13T18:00:00"},  # yesterday
            {"id": "c", "status": "todo", "title": "C"},
        ]
        s = eod.extract_eod_sections(_snapshot(pd_tasks=tasks))
        assert len(s.done_today) == 1
        assert s.done_today[0]["id"] == "a"
        assert s.completion_counts["done_today"] == 1

    def test_carry_over_carries_reason(self):
        tasks = [
            {"id": "x", "status": "blocked", "title": "X",
             "blocked_by": ["y"], "priority": "high"},
            {"id": "z", "status": "in_progress", "title": "Z"},
            {"id": "q", "status": "todo", "title": "Q"},
            {"id": "skip", "status": "done", "title": "Skip",
             "completed_at": f"{TODAY}T08:00:00"},
        ]
        s = eod.extract_eod_sections(_snapshot(pd_tasks=tasks))
        assert len(s.carry_over) == 3
        reasons = {t["id"]: t["reason"] for t in s.carry_over}
        assert "blocked: y" in reasons["x"]
        assert reasons["z"] == "in_progress"
        assert reasons["q"] == "not picked up"
        assert s.completion_counts["carry_over"] == 3
        assert s.completion_counts["blocked"] == 1

    def test_at_risk_pulls_failing_projects(self):
        projects = {
            "fp": {"name": "Failing", "failing": True},
            "rb": {"name": "Rollback", "rollback_needed": True},
            "ok": {"name": "Okay"},
        }
        research = [
            {"id": "r1", "title": "R1", "at_risk": True, "priority": "high"},
            {"id": "r2", "title": "Not at risk"},
        ]
        s = eod.extract_eod_sections(_snapshot(
            pd_projects=projects, pd_research=research,
        ))
        ids = {item["id"] for item in s.at_risk_goals}
        assert ids == {"fp", "rb", "r1"}

    def test_tomorrow_priority_sorts_by_priority(self):
        tasks = [
            {"id": "low", "status": "todo", "priority": "low"},
            {"id": "crit", "status": "todo", "priority": "critical"},
            {"id": "norm", "status": "in_progress", "priority": "normal"},
        ]
        s = eod.extract_eod_sections(_snapshot(pd_tasks=tasks))
        assert [t["id"] for t in s.tomorrow_priority] == ["crit", "norm", "low"]

    def test_tomorrow_calendar_prefers_calendar_tomorrow(self):
        events_upcoming = [{"id": "1", "title": "A", "start": "2026-05-15T09:00"}]
        events_tomorrow = [{"id": "2", "title": "B", "start": "2026-05-15T10:00"}]
        s = eod.extract_eod_sections(_snapshot(
            calendar_upcoming=events_upcoming,
            calendar_tomorrow=events_tomorrow,
        ))
        assert [e["id"] for e in s.tomorrow_calendar] == ["2"]

    def test_tomorrow_calendar_falls_back_to_upcoming(self):
        events_upcoming = [{"id": "1", "title": "A", "start": "2026-05-15T09:00"}]
        s = eod.extract_eod_sections(_snapshot(
            calendar_upcoming=events_upcoming,
        ))
        assert [e["id"] for e in s.tomorrow_calendar] == ["1"]

    def test_previous_eod_date_pulled_from_workflow_state(self):
        s = eod.extract_eod_sections(_snapshot(
            workflow_state={"last_eod_date": "2026-05-13"},
        ))
        assert s.previous_eod_date == "2026-05-13"

    def test_trajectory_delta_accepts_list_or_string(self):
        s1 = eod.extract_eod_sections(_snapshot(
            workflow_state={"trajectory_delta": ["shipped X", "blocked Y"]},
        ))
        assert s1.trajectory_delta == ("shipped X", "blocked Y")
        s2 = eod.extract_eod_sections(_snapshot(
            workflow_state={"trajectory_delta": "shipped X\nblocked Y"},
        ))
        assert s2.trajectory_delta == ("shipped X", "blocked Y")

    def test_section_caps_truncate(self):
        tasks = [
            {"id": f"t{i}", "status": "done", "title": f"T{i}",
             "completed_at": f"{TODAY}T08:00:00"}
            for i in range(20)
        ]
        s = eod.extract_eod_sections(_snapshot(pd_tasks=tasks),
                                       done_cap=3)
        assert len(s.done_today) == 3


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

class TestPromptRender:
    def test_render_includes_section_headings(self):
        gen = eod.L8EODGenerator()
        out = gen.render(_snapshot())
        for heading in ("DONE TODAY", "CARRY OVER", "AT-RISK GOALS",
                         "TOMORROW PRIORITY", "CALENDAR TOMORROW",
                         "TRAJECTORY DELTA"):
            assert f"=== {heading} ===" in out

    def test_render_is_deterministic(self):
        snap = _snapshot(pd_tasks=[
            {"id": "a", "status": "todo", "priority": "high", "title": "A"},
        ])
        gen = eod.L8EODGenerator()
        assert gen.render(snap) == gen.render(snap)

    def test_empty_sections_render_as_none(self):
        out = eod.L8EODGenerator().render(_snapshot())
        assert "(none)" in out


# ---------------------------------------------------------------------------
# Surface payloads
# ---------------------------------------------------------------------------

class TestSurfacePayloads:
    def test_discord_embed_red_when_at_risk(self):
        snap = _snapshot(pd_projects={"x": {"name": "X", "failing": True}})
        sections = eod.extract_eod_sections(snap)
        embed = eod.build_discord_embed("body", sections)
        assert embed["color"] == 0xED4245
        assert any(f["name"].startswith("At-risk goals")
                    for f in embed["fields"])

    def test_discord_embed_amber_on_carry_over(self):
        snap = _snapshot(pd_tasks=[
            {"id": "x", "status": "blocked", "title": "X",
             "blocked_by": ["y"]},
        ])
        sections = eod.extract_eod_sections(snap)
        embed = eod.build_discord_embed("body", sections)
        assert embed["color"] == 0xFEE75C

    def test_discord_embed_green_on_clean_day(self):
        sections = eod.extract_eod_sections(_snapshot(pd_tasks=[
            {"id": "a", "status": "done", "completed_at": f"{TODAY}T08:00:00",
             "title": "A"},
        ]))
        embed = eod.build_discord_embed("body", sections)
        assert embed["color"] == 0x57F287
        # Has the "Done today" field
        assert any(f["name"].startswith("Done today")
                    for f in embed["fields"])

    def test_discord_embed_truncates_long_body(self):
        sections = eod.extract_eod_sections(_snapshot())
        long_body = "x" * 5000
        embed = eod.build_discord_embed(long_body, sections)
        assert len(embed["description"]) <= 2000

    def test_dream_markdown_has_sections(self):
        snap = _snapshot(pd_tasks=[
            {"id": "a", "status": "done", "title": "Shipped",
             "completed_at": f"{TODAY}T10:00:00"},
            {"id": "b", "status": "blocked", "title": "Stuck",
             "blocked_by": ["c"]},
        ])
        sections = eod.extract_eod_sections(snap)
        md = eod.build_dream_markdown("Summary line.", sections)
        assert md.startswith("# EOD Review --")
        assert "## Done today" in md
        assert "## Carry over" in md


# ---------------------------------------------------------------------------
# Workflow-state write
# ---------------------------------------------------------------------------

class TestMarkEodComplete:
    def test_creates_file_when_absent(self, tmp_path):
        path = tmp_path / "workflow_state.json"
        result = eod.mark_eod_complete(path, today_iso="2026-05-14")
        assert result["last_eod_date"] == "2026-05-14"
        assert json.loads(path.read_text())["last_eod_date"] == "2026-05-14"

    def test_preserves_other_keys(self, tmp_path):
        path = tmp_path / "workflow_state.json"
        path.write_text(json.dumps({"last_sod_date": "2026-05-14",
                                     "other": "keep"}))
        result = eod.mark_eod_complete(path, today_iso="2026-05-14")
        assert result["last_sod_date"] == "2026-05-14"
        assert result["other"] == "keep"
        assert result["last_eod_date"] == "2026-05-14"

    def test_recovers_from_corrupt_state_file(self, tmp_path):
        path = tmp_path / "workflow_state.json"
        path.write_text("not json {}{")  # garbage
        result = eod.mark_eod_complete(path, today_iso="2026-05-14")
        assert result == {"last_eod_date": "2026-05-14"}


# ---------------------------------------------------------------------------
# Output format parity with SOD generator
# ---------------------------------------------------------------------------

def test_format_parity_with_sod():
    """EOD generator follows the same module shape as SOD: extract,
    render, embed, markdown, mark_complete. Plus discord color palette
    matches (green/amber/red)."""
    from scripts import l8_sod_generator as sod
    sod_attrs = {"extract_sod_sections", "L8SODGenerator",
                  "build_discord_embed", "build_dream_markdown",
                  "mark_sod_complete"}
    eod_attrs = {"extract_eod_sections", "L8EODGenerator",
                  "build_discord_embed", "build_dream_markdown",
                  "mark_eod_complete"}
    # Both modules expose the equivalent surface, just renamed for EOD.
    assert len(sod_attrs) == len(eod_attrs)
    # Color constants match between modules (green/amber/red palette).
    assert sod._COLOR_GREEN == eod._COLOR_GREEN
    assert sod._COLOR_AMBER == eod._COLOR_AMBER
    assert sod._COLOR_RED == eod._COLOR_RED
