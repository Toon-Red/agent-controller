"""AC-S16c: tests for the L8 SOD output generator.

The SOD generator turns a mocked :class:`StateSnapshot` into:

* a ``SODSections`` payload (deterministic, side-effect-free),
* a PM-style prompt the L8 PM hands to the engine,
* a Discord embed payload + Dream-UI markdown so callers don't
  have to re-shape the structured view.

These tests exercise every section the AC-S16c task description
calls out -- overdue, decisions needed, trajectory delta, agents
being assigned -- plus the workflow-state write helper.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from scripts.l8_project_manager import (
    Dispatcher,
    L8ProjectManager,
    StateSnapshot,
    Surface,
)
from scripts.engine_driver import BaseDriver, EngineRequest, EngineResponse
from scripts.l8_sod_generator import (
    DEFAULT_OVERDUE_CAP,
    L8SODGenerator,
    SODSections,
    build_discord_embed,
    build_dream_markdown,
    extract_sod_sections,
    mark_sod_complete,
)
from scripts.role_config import parse_settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# Local-time noon on 2026-05-14 -- pinned so deadline comparisons
# don't depend on the runner's wall clock. Built via mktime so the
# resulting timestamp is in the runner's local timezone (the SOD
# generator formats today_iso via localtime, so we must match).
import time as _time_mod
_TODAY_TS = _time_mod.mktime((2026, 5, 14, 12, 0, 0, 0, 0, -1))
_TODAY_ISO = "2026-05-14"


def _snapshot(**overrides: Any) -> StateSnapshot:
    defaults: dict[str, Any] = dict(
        pd_tasks=(),
        pd_research=(),
        pd_requests=(),
        pd_projects={},
        calendar_events=(),
        workflow_state={},
        captured_at=_TODAY_TS,
    )
    defaults.update(overrides)
    return StateSnapshot(**defaults)


def _task(**kw: Any) -> Mapping[str, Any]:
    base = {
        "id": kw.get("id", "T1"),
        "title": kw.get("title", "Task"),
        "status": kw.get("status", "todo"),
        "priority": kw.get("priority", "normal"),
        "project": kw.get("project", "agent-controller"),
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Section extraction
# ---------------------------------------------------------------------------


class TestExtractSODSections:
    def test_overdue_includes_past_deadline_and_blocked(self):
        snap = _snapshot(
            pd_tasks=(
                _task(id="T1", title="ship it", deadline="2026-04-30",
                      status="in_progress", priority="high"),
                _task(id="T2", title="future", deadline="2027-01-01",
                      status="todo", priority="high"),
                _task(id="T3", title="stuck", status="blocked",
                      priority="critical"),
                _task(id="T4", title="done thing", status="done",
                      deadline="1999-01-01"),
            ),
        )
        sections = extract_sod_sections(snap, now=_TODAY_TS)
        ids = [t["id"] for t in sections.overdue]
        # Blocked (critical) sorts first; then past-deadline high.
        assert ids == ["T3", "T1"]
        # Future deadline is not overdue; done is excluded.
        assert "T2" not in ids
        assert "T4" not in ids

    def test_decisions_combines_research_and_requests_with_source(self):
        snap = _snapshot(
            pd_research=({"id": "R1", "title": "should we?", "priority": "high"},),
            pd_requests=({"id": "U1", "title": "please add X"},),
        )
        sections = extract_sod_sections(snap, now=_TODAY_TS)
        sources = {d["source"] for d in sections.decisions_needed}
        assert sources == {"research", "request"}
        # High priority research sorts before unmarked request.
        assert sections.decisions_needed[0]["id"] == "R1"

    def test_trajectory_delta_from_workflow_state_list(self):
        snap = _snapshot(workflow_state={
            "last_sod_date": "2026-05-13",
            "trajectory_delta": ["AC-S16 moved to in_progress", "AC-S3 slipped"],
        })
        sections = extract_sod_sections(snap, now=_TODAY_TS)
        assert sections.trajectory_delta == (
            "AC-S16 moved to in_progress",
            "AC-S3 slipped",
        )
        assert sections.previous_sod_date == "2026-05-13"

    def test_trajectory_delta_accepts_newline_string(self):
        snap = _snapshot(workflow_state={
            "trajectory_delta": "line A\nline B\n",
        })
        sections = extract_sod_sections(snap, now=_TODAY_TS)
        assert sections.trajectory_delta == ("line A", "line B")

    def test_trajectory_delta_empty_when_no_data(self):
        sections = extract_sod_sections(_snapshot(), now=_TODAY_TS)
        assert sections.trajectory_delta == ()
        assert sections.previous_sod_date is None

    def test_agents_being_assigned_is_priority_sorted_open_tasks(self):
        snap = _snapshot(pd_tasks=(
            _task(id="A", priority="low", status="todo"),
            _task(id="B", priority="critical", status="in_progress"),
            _task(id="C", priority="high", status="todo"),
            _task(id="D", priority="high", status="done"),    # excluded
            _task(id="E", priority="high", status="blocked"),  # excluded
        ))
        sections = extract_sod_sections(snap, now=_TODAY_TS)
        ids = [t["id"] for t in sections.agents_being_assigned]
        assert ids == ["B", "C", "A"]

    def test_failing_projects_sorted_and_deduped(self):
        snap = _snapshot(pd_projects={
            "zebra": {"failing": True},
            "alpha": {"rollback_needed": True},
            "ok": {"failing": False},
        })
        sections = extract_sod_sections(snap, now=_TODAY_TS)
        assert sections.failing_projects == ("alpha", "zebra")

    def test_open_task_counts_by_status_excludes_done(self):
        snap = _snapshot(pd_tasks=(
            _task(id="1", status="todo"),
            _task(id="2", status="todo"),
            _task(id="3", status="blocked"),
            _task(id="4", status="done"),
        ))
        sections = extract_sod_sections(snap, now=_TODAY_TS)
        assert sections.open_task_counts == {"blocked": 1, "todo": 2}

    def test_today_iso_derived_from_snapshot_when_no_override(self):
        sections = extract_sod_sections(_snapshot(), now=None)
        assert sections.today_iso == _TODAY_ISO

    def test_caps_respected(self):
        many = tuple(
            _task(id=f"T{i}", status="blocked", priority="high")
            for i in range(20)
        )
        sections = extract_sod_sections(_snapshot(pd_tasks=many),
                                        overdue_cap=3, now=_TODAY_TS)
        assert len(sections.overdue) == 3

    def test_is_quiet_when_no_material_data(self):
        sections = extract_sod_sections(_snapshot(), now=_TODAY_TS)
        assert sections.is_quiet() is True

    def test_is_not_quiet_when_overdue_present(self):
        snap = _snapshot(pd_tasks=(_task(status="blocked"),))
        sections = extract_sod_sections(snap, now=_TODAY_TS)
        assert sections.is_quiet() is False

    def test_extraction_is_deterministic(self):
        snap = _snapshot(
            pd_tasks=(
                _task(id="T1", status="blocked", priority="high"),
                _task(id="T2", priority="high", status="todo"),
            ),
            pd_research=({"id": "R1", "title": "x"},),
        )
        a = extract_sod_sections(snap, now=_TODAY_TS)
        b = extract_sod_sections(snap, now=_TODAY_TS)
        assert a == b


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


class TestSODPromptRender:
    def test_prompt_contains_all_section_headings(self):
        gen = L8SODGenerator()
        snap = _snapshot(
            pd_tasks=(
                _task(id="T1", status="blocked", priority="critical",
                      title="stuck pipe"),
                _task(id="T2", status="todo", priority="high",
                      title="ship feature"),
            ),
            pd_research=({"id": "R1", "title": "decide engine?"},),
            pd_projects={"proj-a": {"failing": True}},
            calendar_events=({"start": "10:00", "title": "Standup"},),
            workflow_state={
                "last_sod_date": "2026-05-13",
                "trajectory_delta": ["AC-S16b landed"],
            },
        )
        prompt = gen.render(snap)
        for heading in (
            "TODAY",
            "PREVIOUS SOD",
            "OPEN TASK COUNTS",
            "OVERDUE / BLOCKED",
            "DECISIONS NEEDED",
            "TRAJECTORY DELTA",
            "AGENTS BEING ASSIGNED",
            "FAILING PROJECTS",
            "CALENDAR TODAY",
        ):
            assert f"=== {heading} ===" in prompt, f"missing section: {heading}"

    def test_prompt_carries_data_from_snapshot(self):
        gen = L8SODGenerator()
        snap = _snapshot(
            pd_tasks=(
                _task(id="STUCK-1", status="blocked", priority="critical",
                      title="rotate keys"),
                _task(id="TODO-1", status="todo", priority="high",
                      title="land sod gen"),
            ),
            pd_research=({"id": "R-99", "title": "approve plugin?"},),
            pd_projects={"broken-proj": {"failing": True}},
            workflow_state={"trajectory_delta": ["AC-S16b shipped"]},
        )
        prompt = gen.render(snap)
        # Overdue/blocked task surfaces.
        assert "STUCK-1" in prompt
        assert "rotate keys" in prompt
        # Decision item surfaces with its id + source.
        assert "R-99" in prompt
        assert "research:" in prompt or "[research" in prompt
        # Agents-today section carries the open task.
        assert "TODO-1" in prompt
        assert "land sod gen" in prompt
        # Failing project surfaces.
        assert "broken-proj" in prompt
        # Trajectory delta surfaces.
        assert "AC-S16b shipped" in prompt

    def test_prompt_marks_empty_sections_with_none(self):
        gen = L8SODGenerator()
        prompt = gen.render(_snapshot())
        # No overdue items, decisions, etc -- but the heading still
        # appears with an explicit (none) marker so the engine
        # doesn't hallucinate items.
        assert "=== OVERDUE / BLOCKED ===\n(none)" in prompt
        assert "=== DECISIONS NEEDED ===\n(none)" in prompt
        assert "=== TRAJECTORY DELTA ===\n(none)" in prompt
        assert "=== AGENTS BEING ASSIGNED ===\n(none)" in prompt
        assert "=== FAILING PROJECTS ===\n(none)" in prompt
        assert "=== CALENDAR TODAY ===\n(none)" in prompt

    def test_prompt_prefix_documents_pm_tone(self):
        prompt = L8SODGenerator().render(_snapshot())
        # PM-tone rule from the L8 template body should land in the prefix.
        assert "MORNING STANDUP" in prompt
        assert "Short sentences" in prompt
        # The decision-or-no-action footer rule is part of the prefix.
        assert "Decision needed" in prompt or "No action needed" in prompt


# ---------------------------------------------------------------------------
# Discord embed builder
# ---------------------------------------------------------------------------


class TestBuildDiscordEmbed:
    def test_title_and_description_set(self):
        sections = extract_sod_sections(_snapshot(), now=_TODAY_TS)
        embed = build_discord_embed("hello standup", sections)
        assert embed["title"] == "Morning Standup -- 2026-05-14"
        assert embed["description"] == "hello standup"
        # Empty snapshot -> no fields (green light).
        assert embed["fields"] == []
        assert embed["color"] == 0x57F287  # green

    def test_color_red_when_failing_projects(self):
        snap = _snapshot(pd_projects={"x": {"failing": True}})
        sections = extract_sod_sections(snap, now=_TODAY_TS)
        embed = build_discord_embed("body", sections)
        assert embed["color"] == 0xED4245

    def test_color_amber_when_overdue_or_decisions(self):
        snap = _snapshot(pd_tasks=(_task(status="blocked"),))
        sections = extract_sod_sections(snap, now=_TODAY_TS)
        embed = build_discord_embed("body", sections)
        assert embed["color"] == 0xFEE75C

    def test_fields_added_per_nonempty_section(self):
        snap = _snapshot(
            pd_tasks=(
                _task(id="A", status="blocked", priority="high", title="x"),
                _task(id="B", status="todo", priority="high", title="y"),
            ),
            pd_research=({"id": "R", "title": "?"},),
            pd_projects={"p1": {"failing": True}},
            calendar_events=({"start": "9:00", "title": "Sync"},),
            workflow_state={"trajectory_delta": ["a delta"]},
        )
        sections = extract_sod_sections(snap, now=_TODAY_TS)
        embed = build_discord_embed("body", sections)
        names = [f["name"] for f in embed["fields"]]
        assert any("Overdue" in n for n in names)
        assert any("Decisions" in n for n in names)
        assert any("Trajectory" in n for n in names)
        assert any("Agents" in n for n in names)
        assert any("Failing" in n for n in names)
        assert any("Calendar" in n for n in names)

    def test_field_value_capped(self):
        long_title = "x" * 5000
        snap = _snapshot(pd_tasks=(
            _task(id="LONG", status="blocked", title=long_title),
        ))
        sections = extract_sod_sections(snap, now=_TODAY_TS)
        embed = build_discord_embed("body", sections, field_value_cap=100)
        overdue_field = next(f for f in embed["fields"] if "Overdue" in f["name"])
        assert len(overdue_field["value"]) <= 100
        assert overdue_field["value"].endswith("...")


# ---------------------------------------------------------------------------
# Dream markdown builder
# ---------------------------------------------------------------------------


class TestBuildDreamMarkdown:
    def test_markdown_contains_header_and_body(self):
        sections = extract_sod_sections(_snapshot(), now=_TODAY_TS)
        md = build_dream_markdown("standup body here", sections)
        assert md.startswith("# Morning Standup -- 2026-05-14")
        assert "standup body here" in md

    def test_markdown_skips_empty_sections(self):
        sections = extract_sod_sections(_snapshot(), now=_TODAY_TS)
        md = build_dream_markdown("body", sections)
        # An empty snapshot should not produce ANY section H2s.
        assert "## " not in md

    def test_markdown_emits_each_nonempty_section(self):
        snap = _snapshot(
            pd_tasks=(_task(id="A", status="blocked", priority="high"),),
            pd_research=({"id": "R", "title": "?"},),
            pd_projects={"p1": {"failing": True}},
            workflow_state={"trajectory_delta": ["x"]},
        )
        sections = extract_sod_sections(snap, now=_TODAY_TS)
        md = build_dream_markdown("body", sections)
        assert "## Overdue" in md
        assert "## Decisions needed" in md
        assert "## Trajectory delta" in md
        assert "## Failing projects" in md


# ---------------------------------------------------------------------------
# Workflow-state writer
# ---------------------------------------------------------------------------


class TestMarkSODComplete:
    def test_creates_file_when_missing(self, tmp_path: Path):
        path = tmp_path / "workflow_state.json"
        state = mark_sod_complete(path, today_iso="2026-05-14")
        assert state == {"last_sod_date": "2026-05-14"}
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["last_sod_date"] == "2026-05-14"

    def test_preserves_existing_fields(self, tmp_path: Path):
        path = tmp_path / "workflow_state.json"
        path.write_text(
            json.dumps({"last_eod_date": "2026-05-13", "custom": 42}),
            encoding="utf-8",
        )
        state = mark_sod_complete(path, today_iso="2026-05-14")
        assert state["last_eod_date"] == "2026-05-13"
        assert state["custom"] == 42
        assert state["last_sod_date"] == "2026-05-14"

    def test_overwrites_previous_sod_date(self, tmp_path: Path):
        path = tmp_path / "workflow_state.json"
        path.write_text(
            json.dumps({"last_sod_date": "2026-05-13"}),
            encoding="utf-8",
        )
        state = mark_sod_complete(path, today_iso="2026-05-14")
        assert state["last_sod_date"] == "2026-05-14"

    def test_recovers_from_malformed_existing_state(self, tmp_path: Path):
        path = tmp_path / "workflow_state.json"
        path.write_text("not json at all", encoding="utf-8")
        state = mark_sod_complete(path, today_iso="2026-05-14")
        assert state == {"last_sod_date": "2026-05-14"}


# ---------------------------------------------------------------------------
# L8 PM integration -- run_sod with the real SOD generator
# ---------------------------------------------------------------------------


class _RecordingDriver(BaseDriver):
    engine_id = "claude-opus"

    def __init__(self) -> None:
        self.requests: list[EngineRequest] = []

    def run(self, request: EngineRequest) -> EngineResponse:
        self.requests.append(request)
        return EngineResponse(
            text="Headline: ecosystem nominal.\n\nNo action needed.",
            source="ai",
            attribution={"ai": 50},
        )


class _RecordingSurface:
    def __init__(self) -> None:
        self.posts: list[Any] = []

    def post(self, output: Any) -> None:
        self.posts.append(output)


def test_l8_pm_run_sod_uses_real_generator():
    settings = parse_settings({
        "engines": {"L8": "claude-opus"},
        "roles": {"project-manager": {"level": "L8"}},
    })
    dispatcher = Dispatcher()
    driver = _RecordingDriver()
    dispatcher.register("claude-opus", driver)
    discord = _RecordingSurface()
    pm = L8ProjectManager(
        settings=settings,
        dispatcher=dispatcher,
        sod_generator=L8SODGenerator(),
        discord=discord,
    )
    snap = _snapshot(pd_tasks=(_task(id="T1", status="blocked"),))
    output = pm.run_sod(snapshot=snap)

    # Engine got the SOD prompt with the section headings.
    assert len(driver.requests) == 1
    prompt = driver.requests[0].prompt
    assert "MORNING STANDUP" in prompt
    assert "T1" in prompt

    # Output routed to Discord with the engine's body text.
    assert output.surface is Surface.DISCORD_ASYNC
    assert "ecosystem nominal" in output.text
    assert discord.posts == [output]
