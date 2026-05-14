"""AC-S16b: tests for the PD + Calendar state reader.

The reader is a thin MCP-client wrapper. These tests use an in-memory
fake MCP client + fake Calendar client to exercise:

* Snapshot shape -- the dataclass carries every field the task
  description (AC-S16b body) calls out.
* PD tool fan-out -- exactly four PD MCP tools are called, by their
  canonical names.
* Bucketing -- tasks are grouped by status; order within each bucket
  is preserved.
* Calendar split -- today vs upcoming(7 days), with de-dupe by id
  when the Calendar MCP returns today's events in both.
* Per-iteration cache -- repeated reads in one iteration return the
  same object; ``refresh()`` invalidates and re-fetches.
* StateReader protocol -- the reader satisfies the L8 PM
  ``StateReader`` contract and the projected ``StateSnapshot`` is
  well-formed.
* Read-only -- frozen dataclass; helper accessors don't leak
  mutability of the underlying MCP payload back into the reader.
* Defensive shaping -- malformed / empty payloads degrade gracefully.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

import pytest

from scripts.l8_project_manager import StateReader, StateSnapshot
from scripts.l8_state_reader import (
    DEFAULT_CALENDAR_LOOKAHEAD_DAYS,
    OPEN_STATUSES,
    PD_TOOL_GET_PROJECTS,
    PD_TOOL_LIST_REQUESTS,
    PD_TOOL_LIST_RESEARCH,
    PD_TOOL_LIST_TASKS,
    PDCalendarSnapshot,
    PDCalendarStateReader,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeMCP:
    """Records every call_tool invocation; returns canned payloads."""

    def __init__(self, responses: Mapping[str, Any]) -> None:
        self._responses = dict(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, /, **params: Any) -> Any:
        self.calls.append((name, dict(params)))
        if name not in self._responses:
            raise AssertionError(f"unexpected MCP call: {name}")
        return self._responses[name]


class _FakeCalendar:
    def __init__(
        self,
        *,
        today: Iterable[Mapping[str, Any]] = (),
        upcoming: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        self._today = list(today)
        self._upcoming = list(upcoming)
        self.today_calls = 0
        self.upcoming_days_called: list[int] = []

    def today_events(self) -> list[Mapping[str, Any]]:
        self.today_calls += 1
        return list(self._today)

    def upcoming_events(self, days: int) -> list[Mapping[str, Any]]:
        self.upcoming_days_called.append(days)
        return list(self._upcoming)


def _canned_pd() -> dict[str, Any]:
    return {
        PD_TOOL_GET_PROJECTS: [
            {"id": "agent-controller", "stage": "dev", "failing": False},
            {"id": "ruflow-legacy", "stage": "frozen", "rollback_needed": True},
            {"id": "dream", "stage": "prod", "failing": True},
        ],
        PD_TOOL_LIST_TASKS: [
            {"id": "AC-S16a", "status": "done", "title": "L8 template"},
            {"id": "AC-S16b", "status": "in_progress", "title": "state reader"},
            {"id": "AC-S16c", "status": "todo", "title": "SOD generator"},
            {"id": "AC-S16d", "status": "todo", "title": "EOD generator"},
            {"id": "AC-S99",  "status": "blocked", "title": "stuck"},
        ],
        PD_TOOL_LIST_RESEARCH: [
            {"id": "c1779970", "status": "todo", "title": "naming clarity"},
            {"id": "stale-decision", "status": "done", "title": "already filed"},
        ],
        PD_TOOL_LIST_REQUESTS: [
            {"id": "req-1", "status": "todo", "title": "ship L8"},
            # No status field -- defensive default keeps it.
            {"id": "req-2", "title": "no-status"},
            # Closed-by-flag form -- should be dropped.
            {"id": "req-old", "status": "done", "title": "old"},
        ],
    }


def _make_reader(
    *,
    pd_responses: Mapping[str, Any] | None = None,
    calendar: _FakeCalendar | None = None,
    workflow_state_loader: Any | None = None,
    calendar_lookahead_days: int = DEFAULT_CALENDAR_LOOKAHEAD_DAYS,
    clock: Any = lambda: 1_700_000_000.0,
) -> tuple[PDCalendarStateReader, _FakeMCP]:
    mcp = _FakeMCP(pd_responses if pd_responses is not None else _canned_pd())
    reader = PDCalendarStateReader(
        mcp=mcp,
        calendar=calendar,
        workflow_state_loader=workflow_state_loader,
        calendar_lookahead_days=calendar_lookahead_days,
        clock=clock,
    )
    return reader, mcp


# ---------------------------------------------------------------------------
# Snapshot shape
# ---------------------------------------------------------------------------


def test_read_returns_pdcalendar_snapshot_with_all_fields():
    cal = _FakeCalendar(
        today=[{"id": "ev-today-1", "title": "AC sync"}],
        upcoming=[{"id": "ev-up-1", "title": "Pipelines review"}],
    )
    reader, _ = _make_reader(calendar=cal, workflow_state_loader=lambda: {
        "last_sod_date": "2026-05-14",
        "last_eod_date": "2026-05-13",
    })
    snap = reader.read()
    assert isinstance(snap, PDCalendarSnapshot)
    # Every field from the task description (AC-S16b body) is present.
    assert "agent-controller" in snap.projects
    assert "in_progress" in snap.tasks_by_status
    assert snap.open_research and snap.open_research[0]["id"] == "c1779970"
    # req-1 is open by status; req-2 has no status (defensive default).
    open_request_ids = {r["id"] for r in snap.open_requests}
    assert open_request_ids == {"req-1", "req-2"}
    assert snap.calendar_today and snap.calendar_today[0]["id"] == "ev-today-1"
    assert snap.calendar_upcoming and snap.calendar_upcoming[0]["id"] == "ev-up-1"
    assert snap.workflow_state["last_sod_date"] == "2026-05-14"
    assert snap.captured_at == 1_700_000_000.0


def test_snapshot_is_frozen_and_immutable():
    reader, _ = _make_reader()
    snap = reader.read()
    with pytest.raises((AttributeError, Exception)):
        snap.projects = {}  # type: ignore[misc]
    # Even if the caller mutates a returned bucket, the snapshot's
    # tuples can't be re-assigned and our internal copies are dicts.
    bucket = snap.tasks_by_status["todo"]
    assert isinstance(bucket, tuple)


# ---------------------------------------------------------------------------
# PD MCP fan-out
# ---------------------------------------------------------------------------


def test_one_call_per_pd_tool_in_one_pass():
    reader, mcp = _make_reader()
    reader.read()
    called_names = [name for name, _ in mcp.calls]
    assert called_names == [
        PD_TOOL_GET_PROJECTS,
        PD_TOOL_LIST_TASKS,
        PD_TOOL_LIST_RESEARCH,
        PD_TOOL_LIST_REQUESTS,
    ]


def test_pd_tool_names_match_canonical_pipeline_dashboard_namespace():
    """Catches a typo where a tool name drifts from the upstream MCP."""
    assert PD_TOOL_GET_PROJECTS == "mcp__pipeline-dashboard__get_projects"
    assert PD_TOOL_LIST_TASKS == "mcp__pipeline-dashboard__list_tasks"
    assert PD_TOOL_LIST_RESEARCH == "mcp__pipeline-dashboard__list_research"
    assert PD_TOOL_LIST_REQUESTS == "mcp__pipeline-dashboard__list_requests"


# ---------------------------------------------------------------------------
# Bucketing + filtering
# ---------------------------------------------------------------------------


def test_tasks_bucketed_by_status_preserving_input_order():
    reader, _ = _make_reader()
    snap = reader.read()
    assert [t["id"] for t in snap.tasks_by_status["todo"]] == ["AC-S16c", "AC-S16d"]
    assert [t["id"] for t in snap.tasks_by_status["in_progress"]] == ["AC-S16b"]
    assert [t["id"] for t in snap.tasks_by_status["blocked"]] == ["AC-S99"]
    assert [t["id"] for t in snap.tasks_by_status["done"]] == ["AC-S16a"]


def test_missing_status_falls_into_unknown_bucket():
    pd = _canned_pd()
    pd[PD_TOOL_LIST_TASKS] = [{"id": "weird"}]  # no status
    reader, _ = _make_reader(pd_responses=pd)
    snap = reader.read()
    assert "unknown" in snap.tasks_by_status
    assert snap.tasks_by_status["unknown"][0]["id"] == "weird"


def test_all_open_tasks_excludes_done():
    reader, _ = _make_reader()
    snap = reader.read()
    ids = {t["id"] for t in snap.all_open_tasks()}
    assert "AC-S16a" not in ids  # done -- excluded
    assert {"AC-S16b", "AC-S16c", "AC-S16d", "AC-S99"} <= ids


def test_open_research_filters_done_items():
    reader, _ = _make_reader()
    snap = reader.read()
    ids = {r["id"] for r in snap.open_research}
    assert "stale-decision" not in ids
    assert "c1779970" in ids


def test_open_statuses_constant_matches_pd_vocabulary():
    assert OPEN_STATUSES == frozenset({"todo", "in_progress", "blocked"})


def test_failing_project_ids_includes_rollback_needed():
    reader, _ = _make_reader()
    snap = reader.read()
    failing = snap.failing_project_ids()
    assert "dream" in failing  # failing=True
    assert "ruflow-legacy" in failing  # rollback_needed=True
    assert "agent-controller" not in failing


# ---------------------------------------------------------------------------
# Projects indexing
# ---------------------------------------------------------------------------


def test_projects_indexed_by_id():
    reader, _ = _make_reader()
    snap = reader.read()
    assert set(snap.projects) == {"agent-controller", "ruflow-legacy", "dream"}
    assert snap.projects["dream"]["failing"] is True


def test_projects_without_id_are_dropped():
    pd = _canned_pd()
    pd[PD_TOOL_GET_PROJECTS] = [
        {"id": "p1", "stage": "dev"},
        {"stage": "dev"},  # no id -- dropped
    ]
    reader, _ = _make_reader(pd_responses=pd)
    snap = reader.read()
    assert set(snap.projects) == {"p1"}


def test_projects_payload_wrapped_in_items_dict_is_unwrapped():
    pd = _canned_pd()
    pd[PD_TOOL_GET_PROJECTS] = {"items": [{"id": "wrapped", "stage": "dev"}]}
    reader, _ = _make_reader(pd_responses=pd)
    snap = reader.read()
    assert "wrapped" in snap.projects


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def test_calendar_passes_lookahead_days_to_upcoming():
    cal = _FakeCalendar(today=[], upcoming=[])
    reader, _ = _make_reader(calendar=cal, calendar_lookahead_days=14)
    reader.read()
    assert cal.upcoming_days_called == [14]


def test_calendar_default_lookahead_is_7():
    assert DEFAULT_CALENDAR_LOOKAHEAD_DAYS == 7


def test_calendar_today_and_upcoming_split_by_id():
    cal = _FakeCalendar(
        today=[{"id": "a", "title": "today"}],
        # Upstream included today's "a" in upcoming -- de-dupe wins.
        upcoming=[{"id": "a", "title": "today (dup)"},
                  {"id": "b", "title": "tomorrow"}],
    )
    reader, _ = _make_reader(calendar=cal)
    snap = reader.read()
    assert [ev["id"] for ev in snap.calendar_today] == ["a"]
    assert [ev["id"] for ev in snap.calendar_upcoming] == ["b"]


def test_no_calendar_client_leaves_calendar_buckets_empty():
    reader, _ = _make_reader(calendar=None)
    snap = reader.read()
    assert snap.calendar_today == ()
    assert snap.calendar_upcoming == ()


def test_zero_lookahead_skips_upcoming_fetch():
    cal = _FakeCalendar(today=[{"id": "a"}], upcoming=[{"id": "b"}])
    reader, _ = _make_reader(calendar=cal, calendar_lookahead_days=0)
    snap = reader.read()
    assert cal.upcoming_days_called == []
    assert snap.calendar_today and snap.calendar_today[0]["id"] == "a"
    assert snap.calendar_upcoming == ()


def test_negative_lookahead_rejected():
    with pytest.raises(ValueError):
        PDCalendarStateReader(mcp=_FakeMCP({}), calendar_lookahead_days=-1)


# ---------------------------------------------------------------------------
# Caching + refresh
# ---------------------------------------------------------------------------


def test_repeated_read_returns_same_cached_snapshot():
    reader, mcp = _make_reader()
    snap_a = reader.read()
    snap_b = reader.read()
    assert snap_a is snap_b
    # Only one round of PD calls.
    assert len(mcp.calls) == 4


def test_refresh_invalidates_and_re_fetches():
    reader, mcp = _make_reader()
    snap_a = reader.read()
    snap_b = reader.refresh()
    assert snap_a is not snap_b
    # Two rounds of PD calls (4 + 4).
    assert len(mcp.calls) == 8


def test_cached_property_reflects_state():
    reader, _ = _make_reader()
    assert reader.cached is None
    snap = reader.read()
    assert reader.cached is snap
    reader.refresh()
    assert reader.cached is not snap


# ---------------------------------------------------------------------------
# Workflow state
# ---------------------------------------------------------------------------


def test_workflow_state_loader_consulted_per_fetch():
    calls: list[int] = []

    def loader() -> Mapping[str, Any]:
        calls.append(1)
        return {"last_sod_date": "2026-05-14"}

    reader, _ = _make_reader(workflow_state_loader=loader)
    reader.read()
    reader.refresh()
    # Once per fetch -- not per cache hit.
    assert sum(calls) == 2


def test_workflow_state_defaults_to_empty_when_loader_absent():
    reader, _ = _make_reader()
    snap = reader.read()
    assert snap.workflow_state == {}


def test_workflow_state_none_loader_result_becomes_empty_dict():
    reader, _ = _make_reader(workflow_state_loader=lambda: None)
    snap = reader.read()
    assert snap.workflow_state == {}


# ---------------------------------------------------------------------------
# StateReader protocol + projection to StateSnapshot
# ---------------------------------------------------------------------------


def test_reader_satisfies_state_reader_protocol():
    reader, _ = _make_reader()
    assert isinstance(reader, StateReader)


def test_snapshot_method_returns_l8_pm_state_snapshot():
    cal = _FakeCalendar(
        today=[{"id": "ev-today", "title": "x"}],
        upcoming=[{"id": "ev-up", "title": "y"}],
    )
    reader, _ = _make_reader(calendar=cal, workflow_state_loader=lambda: {
        "last_sod_date": "2026-05-14",
    })
    state = reader.snapshot()
    assert isinstance(state, StateSnapshot)
    # All tasks (including done) flow through.
    assert len(state.pd_tasks) == 5
    # Calendar events are concatenated today + upcoming.
    assert len(state.calendar_events) == 2
    assert state.pd_projects["dream"]["failing"] is True
    assert state.workflow_state["last_sod_date"] == "2026-05-14"
    assert state.captured_at == 1_700_000_000.0


def test_snapshot_method_uses_same_cache_as_read():
    reader, mcp = _make_reader()
    reader.read()
    reader.snapshot()
    reader.snapshot()
    # Still only one round of PD calls.
    assert len([c for c in mcp.calls if c[0] == PD_TOOL_LIST_TASKS]) == 1


# ---------------------------------------------------------------------------
# Defensive shaping
# ---------------------------------------------------------------------------


def test_none_payload_does_not_crash():
    pd = {
        PD_TOOL_GET_PROJECTS: None,
        PD_TOOL_LIST_TASKS: None,
        PD_TOOL_LIST_RESEARCH: None,
        PD_TOOL_LIST_REQUESTS: None,
    }
    reader, _ = _make_reader(pd_responses=pd)
    snap = reader.read()
    assert snap.projects == {}
    assert snap.tasks_by_status == {}
    assert snap.open_research == ()
    assert snap.open_requests == ()


def test_non_mapping_entries_are_skipped():
    pd = {
        PD_TOOL_GET_PROJECTS: [{"id": "ok"}, "garbage", 42],
        PD_TOOL_LIST_TASKS: [{"id": "t", "status": "todo"}, None],
        PD_TOOL_LIST_RESEARCH: [{"id": "r"}],
        PD_TOOL_LIST_REQUESTS: [{"id": "rq"}],
    }
    reader, _ = _make_reader(pd_responses=pd)
    snap = reader.read()
    assert set(snap.projects) == {"ok"}
    assert len(snap.tasks_by_status["todo"]) == 1


def test_payload_dict_wrapped_in_data_unwraps():
    pd = _canned_pd()
    pd[PD_TOOL_LIST_TASKS] = {"data": [{"id": "t1", "status": "todo"}]}
    reader, _ = _make_reader(pd_responses=pd)
    snap = reader.read()
    assert snap.tasks_by_status["todo"][0]["id"] == "t1"


def test_reader_does_not_mutate_input_payload():
    """The MCP payload is the upstream's source of truth; our reader
    must not write back through the object graph."""
    pd = _canned_pd()
    original_projects = [dict(p) for p in pd[PD_TOOL_GET_PROJECTS]]
    reader, _ = _make_reader(pd_responses=pd)
    snap = reader.read()
    # Mutate the snapshot's copy of a project -- but it's a dict copy,
    # so the original payload list should be unchanged.
    snap.projects["dream"]  # access -- no exception
    assert pd[PD_TOOL_GET_PROJECTS] == original_projects
