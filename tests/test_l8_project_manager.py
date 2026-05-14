"""AC-S16: tests for the L8 Project Manager orchestrator.

The L8 PM component is the wiring between AC-S16b (state reader),
AC-S16c/d (SOD/EOD generators), and AC-S16e/f (Discord + Dream tab
surfaces). Those sub-tasks own their own concrete implementations;
this suite locks down the wiring shape and the surface-selection rule
from Preston's 2026-05-13 decision.

What we cover here:

* Construction validates the configured role.
* ``run_sod`` / ``run_eod`` route to Discord async per the
  2026-05-13 routing rule.
* ``run_interactive`` routes to the Dream tab.
* Engine resolution honours per-role overrides via the AC-S9 resolver.
* The engine receives a properly tagged ``EngineRequest``
  (``role="project-manager"`` / ``level="L8"``) so the AC-S15
  observability stream picks it up.
* Snapshot is read from the configured state reader when no explicit
  snapshot is passed.
* ``should_run_sod`` / ``should_run_eod`` implement the state-machine
  rule from the template body.
* ``observe_l7_progress`` escalates only on material shifts, never on
  steady state.
* Surface adapters are called with the matching :class:`L8Output`.
"""
from __future__ import annotations

import time
from typing import Any, Mapping

import pytest

from scripts.engine_driver import (
    BaseDriver,
    Dispatcher,
    EngineRequest,
    EngineResponse,
)
from scripts.l8_project_manager import (
    DefaultInteractiveGenerator,
    L7ProgressMessage,
    L8Output,
    L8ProjectManager,
    PROJECT_MANAGER_ROLE,
    StateSnapshot,
    Surface,
    TrajectoryTracker,
)
from scripts.role_config import parse_settings


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


class _RecordingDriver(BaseDriver):
    """Captures every request it gets; returns a canned response."""

    def __init__(self, text: str = "ok", engine_id: str = "claude-opus") -> None:
        self.engine_id = engine_id
        self.requests: list[EngineRequest] = []
        self._text = text

    def run(self, request: EngineRequest) -> EngineResponse:
        self.requests.append(request)
        return EngineResponse(
            text=self._text,
            source="ai",
            attribution={"ai": len(self._text.encode("utf-8"))},
        )


class _StaticStateReader:
    def __init__(self, snapshot: StateSnapshot) -> None:
        self._snapshot = snapshot
        self.calls = 0

    def snapshot(self) -> StateSnapshot:
        self.calls += 1
        return self._snapshot


class _RenderingGenerator:
    """SOD/EOD/interactive generator stub. Records every call."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[StateSnapshot] = []

    def render(self, snapshot: StateSnapshot) -> str:
        self.calls.append(snapshot)
        return f"<{self.label} prompt with {len(snapshot.pd_tasks)} tasks>"


class _RecordingSurface:
    def __init__(self) -> None:
        self.posts: list[L8Output] = []

    def post(self, output: L8Output) -> None:
        self.posts.append(output)


_SETTINGS_DICT = {
    "engines": {
        "L4": "ollama-local",
        "L5": "claude-haiku",
        "L6": "claude-sonnet",
        "L7": "claude-opus",
        "L8": "claude-opus",
    },
    "roles": {
        "project-manager": {"level": "L8"},
    },
}


def _make_pm(
    *,
    driver: BaseDriver | None = None,
    state_reader: Any | None = None,
    sod_generator: Any | None = None,
    eod_generator: Any | None = None,
    dream_tab: Any | None = None,
    discord: Any | None = None,
    settings_dict: dict | None = None,
    clock: Any = time.time,
) -> tuple[L8ProjectManager, _RecordingDriver, Dispatcher]:
    settings = parse_settings(settings_dict or _SETTINGS_DICT)
    dispatcher = Dispatcher()
    rec_driver = driver or _RecordingDriver()
    dispatcher.register(rec_driver.engine_id, rec_driver)
    pm = L8ProjectManager(
        settings=settings,
        dispatcher=dispatcher,
        state_reader=state_reader,
        sod_generator=sod_generator,
        eod_generator=eod_generator,
        dream_tab=dream_tab,
        discord=discord,
        clock=clock,
    )
    return pm, rec_driver, dispatcher


def _snapshot(**overrides: Any) -> StateSnapshot:
    defaults: dict[str, Any] = dict(
        pd_tasks=(),
        pd_research=(),
        pd_requests=(),
        pd_projects={},
        calendar_events=(),
        workflow_state={},
        captured_at=1_700_000_000.0,
    )
    defaults.update(overrides)
    return StateSnapshot(**defaults)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_construct_fails_when_role_missing_from_settings():
    settings = parse_settings({"engines": {"L8": "claude-opus"}, "roles": {}})
    dispatcher = Dispatcher()
    with pytest.raises(ValueError, match="project-manager"):
        L8ProjectManager(settings=settings, dispatcher=dispatcher)


def test_construct_fails_when_role_level_is_not_L8():
    settings = parse_settings({
        "engines": {"L5": "claude-haiku", "L8": "claude-opus"},
        "roles": {"project-manager": {"level": "L5"}},
    })
    dispatcher = Dispatcher()
    with pytest.raises(ValueError, match="L8"):
        L8ProjectManager(settings=settings, dispatcher=dispatcher)


def test_default_role_name_matches_settings_key():
    # Catches a typo where the orchestrator drifts from the schema.
    assert PROJECT_MANAGER_ROLE == "project-manager"


# ---------------------------------------------------------------------------
# Surface-selection rule (Preston 2026-05-13)
# ---------------------------------------------------------------------------


def test_sod_routes_to_discord_async():
    sod = _RenderingGenerator("sod")
    discord = _RecordingSurface()
    pm, driver, _ = _make_pm(sod_generator=sod, discord=discord)

    output = pm.run_sod(snapshot=_snapshot())

    assert output.kind == "sod"
    assert output.surface is Surface.DISCORD_ASYNC
    assert discord.posts == [output]
    assert sod.calls and len(sod.calls) == 1
    # And the engine driver was invoked exactly once.
    assert len(driver.requests) == 1


def test_eod_routes_to_discord_async():
    eod = _RenderingGenerator("eod")
    discord = _RecordingSurface()
    pm, _, _ = _make_pm(eod_generator=eod, discord=discord)

    output = pm.run_eod(snapshot=_snapshot())

    assert output.kind == "eod"
    assert output.surface is Surface.DISCORD_ASYNC
    assert discord.posts == [output]


def test_interactive_routes_to_dream_tab():
    dream = _RecordingSurface()
    pm, _, _ = _make_pm(dream_tab=dream)

    output = pm.run_interactive("what's the trajectory?",
                                snapshot=_snapshot())

    assert output.kind == "interactive"
    assert output.surface is Surface.DREAM_TAB
    assert dream.posts == [output]


def test_interactive_rejects_empty_question():
    pm, _, _ = _make_pm(dream_tab=_RecordingSurface())
    with pytest.raises(ValueError):
        pm.run_interactive("   ", snapshot=_snapshot())


# ---------------------------------------------------------------------------
# Engine resolution + request tagging
# ---------------------------------------------------------------------------


def test_engine_request_carries_role_and_level_tags():
    """AC-S15 needs ``agent_role`` + ``level`` on the EngineRequest so
    the observability stream knows which row to file the event under."""
    pm, driver, _ = _make_pm(
        sod_generator=_RenderingGenerator("sod"),
        discord=_RecordingSurface(),
    )
    pm.run_sod(snapshot=_snapshot())
    req = driver.requests[0]
    assert req.role == PROJECT_MANAGER_ROLE
    assert req.level == "L8"
    assert req.extras.get("l8_invocation") == "sod"


def test_per_role_engine_override_is_honoured():
    """AC-S9 resolver: per-role engine overrides the L8 layer default."""
    custom_settings = {
        "engines": {"L8": "claude-opus"},
        "roles": {
            "project-manager": {"level": "L8", "engine": "claude-sonnet"},
        },
    }
    sonnet = _RecordingDriver(engine_id="claude-sonnet")
    opus = _RecordingDriver(engine_id="claude-opus")
    settings = parse_settings(custom_settings)
    dispatcher = Dispatcher()
    dispatcher.register("claude-sonnet", sonnet)
    dispatcher.register("claude-opus", opus)
    pm = L8ProjectManager(
        settings=settings,
        dispatcher=dispatcher,
        sod_generator=_RenderingGenerator("sod"),
        discord=_RecordingSurface(),
    )
    output = pm.run_sod(snapshot=_snapshot())
    assert output.engine == "claude-sonnet"
    assert len(sonnet.requests) == 1
    assert opus.requests == []


def test_attribution_propagates_from_engine_response():
    pm, driver, _ = _make_pm(
        driver=_RecordingDriver(text="hello world"),
        sod_generator=_RenderingGenerator("sod"),
        discord=_RecordingSurface(),
    )
    output = pm.run_sod(snapshot=_snapshot())
    assert output.attribution == {"ai": len(b"hello world")}


# ---------------------------------------------------------------------------
# Snapshot acquisition
# ---------------------------------------------------------------------------


def test_state_reader_is_consulted_when_no_snapshot_passed():
    snap = _snapshot(pd_tasks=({"id": "t1", "status": "todo"},))
    reader = _StaticStateReader(snap)
    sod = _RenderingGenerator("sod")
    pm, _, _ = _make_pm(state_reader=reader, sod_generator=sod,
                         discord=_RecordingSurface())
    pm.run_sod()  # no snapshot kwarg
    assert reader.calls == 1
    assert sod.calls[0] is snap


def test_missing_state_reader_and_no_snapshot_raises():
    pm, _, _ = _make_pm(
        sod_generator=_RenderingGenerator("sod"),
        discord=_RecordingSurface(),
    )
    with pytest.raises(RuntimeError, match="StateReader"):
        pm.run_sod()


def test_missing_sod_generator_raises_clear_error():
    pm, _, _ = _make_pm(discord=_RecordingSurface())
    with pytest.raises(RuntimeError, match="AC-S16c"):
        pm.run_sod(snapshot=_snapshot())


def test_missing_eod_generator_raises_clear_error():
    pm, _, _ = _make_pm(discord=_RecordingSurface())
    with pytest.raises(RuntimeError, match="AC-S16d"):
        pm.run_eod(snapshot=_snapshot())


# ---------------------------------------------------------------------------
# Dispatch toggle
# ---------------------------------------------------------------------------


def test_dispatch_false_returns_output_without_pushing():
    discord = _RecordingSurface()
    pm, _, _ = _make_pm(sod_generator=_RenderingGenerator("sod"),
                         discord=discord)
    output = pm.run_sod(snapshot=_snapshot(), dispatch=False)
    assert isinstance(output, L8Output)
    assert discord.posts == []


def test_dispatch_to_missing_surface_raises():
    """If the caller asks for dispatch but didn't wire a surface, the
    orchestrator must fail loudly rather than silently swallow the
    output. (A silent drop here would lose escalations.)"""
    pm, _, _ = _make_pm(sod_generator=_RenderingGenerator("sod"))
    with pytest.raises(RuntimeError, match="AC-S16e"):
        pm.run_sod(snapshot=_snapshot())


# ---------------------------------------------------------------------------
# SOD/EOD state machine
# ---------------------------------------------------------------------------


def test_should_run_sod_true_when_last_sod_date_not_today():
    pm, _, _ = _make_pm()
    snap = _snapshot(workflow_state={"last_sod_date": "2026-05-13"})
    assert pm.should_run_sod(snapshot=snap, today_iso="2026-05-14") is True


def test_should_run_sod_false_when_last_sod_date_is_today():
    pm, _, _ = _make_pm()
    snap = _snapshot(workflow_state={"last_sod_date": "2026-05-14"})
    assert pm.should_run_sod(snapshot=snap, today_iso="2026-05-14") is False


def test_should_run_eod_requires_sod_today_and_hour_geq_17():
    pm, _, _ = _make_pm()
    snap = _snapshot(workflow_state={
        "last_sod_date": "2026-05-14",
        "last_eod_date": "2026-05-13",
    })
    assert pm.should_run_eod(snapshot=snap, today_iso="2026-05-14",
                              hour_of_day=17) is True
    # Before 17:00 -- not yet.
    assert pm.should_run_eod(snapshot=snap, today_iso="2026-05-14",
                              hour_of_day=16) is False
    # SOD didn't run today -- skip EOD.
    snap2 = _snapshot(workflow_state={
        "last_sod_date": "2026-05-13",
        "last_eod_date": "2026-05-13",
    })
    assert pm.should_run_eod(snapshot=snap2, today_iso="2026-05-14",
                              hour_of_day=18) is False
    # EOD already fired today -- skip.
    snap3 = _snapshot(workflow_state={
        "last_sod_date": "2026-05-14",
        "last_eod_date": "2026-05-14",
    })
    assert pm.should_run_eod(snapshot=snap3, today_iso="2026-05-14",
                              hour_of_day=20) is False


# ---------------------------------------------------------------------------
# Trajectory tracker + L7 progress feed
# ---------------------------------------------------------------------------


def test_trajectory_tracker_detects_running_to_blocked():
    tracker = TrajectoryTracker()
    # First message: running, no shift.
    deltas = tracker.observe(L7ProgressMessage(
        iteration_id="i1",
        current_task={"id": "AC-S16", "title": "L8"},
        status="running",
    ))
    assert deltas == ()
    # Second message: now blocked -- shift detected.
    deltas = tracker.observe(L7ProgressMessage(
        iteration_id="i2",
        current_task={"id": "AC-S16", "title": "L8"},
        status="blocked",
    ))
    assert any("blocked" in d for d in deltas)


def test_trajectory_tracker_flags_critical_blockers():
    tracker = TrajectoryTracker()
    deltas = tracker.observe(L7ProgressMessage(
        iteration_id="i1",
        current_task={"id": "AC-S16"},
        status="blocked",
        blockers=("critical: ruflow upstream down",),
    ))
    assert any("critical blocker" in d for d in deltas)


def test_trajectory_tracker_flags_deadline_slip():
    tracker = TrajectoryTracker()
    deltas = tracker.observe(L7ProgressMessage(
        iteration_id="i1",
        current_task={
            "id": "AC-S16",
            "title": "L8",
            "deadline_slipped": True,
        },
        status="running",
    ))
    assert any("deadline slipped" in d for d in deltas)


def test_observe_l7_progress_returns_none_on_steady_state():
    pm, _, _ = _make_pm(discord=_RecordingSurface())
    out = pm.observe_l7_progress(L7ProgressMessage(
        iteration_id="i1",
        current_task={"id": "AC-S16"},
        status="running",
    ))
    assert out is None


def test_observe_l7_progress_escalates_to_discord_on_material_shift():
    discord = _RecordingSurface()
    pm, driver, _ = _make_pm(discord=discord)
    # Seed running, then blocked.
    pm.observe_l7_progress(L7ProgressMessage(
        iteration_id="i1",
        current_task={"id": "AC-S16", "title": "L8"},
        status="running",
    ))
    out = pm.observe_l7_progress(L7ProgressMessage(
        iteration_id="i2",
        current_task={"id": "AC-S16", "title": "L8"},
        status="blocked",
    ))
    assert out is not None
    assert out.kind == "escalation"
    assert out.surface is Surface.DISCORD_ASYNC
    assert discord.posts == [out]
    assert any("blocked" in d for d in out.trajectory_delta)
    # The escalation prompt was sent to the engine.
    assert len(driver.requests) == 1


# ---------------------------------------------------------------------------
# Default interactive generator
# ---------------------------------------------------------------------------


def test_default_interactive_generator_includes_question_and_snapshot():
    gen = DefaultInteractiveGenerator()
    snap = _snapshot(
        pd_tasks=({"id": "t1", "status": "todo"},
                  {"id": "t2", "status": "in_progress"}),
        pd_projects={"agent-controller": {"failing": True}},
        workflow_state={"last_sod_date": "2026-05-14",
                        "last_eod_date": "2026-05-13"},
    )
    rendered = gen.render("what's blocked this week?", snap)
    assert "what's blocked this week?" in rendered
    assert "projects.failing=['agent-controller']" in rendered
    assert "tasks.by_status=" in rendered
    assert "last_sod_date" in rendered


def test_default_interactive_generator_is_deterministic_for_same_snapshot():
    """Determinism matters for caching + replay -- same snapshot in,
    same bytes out."""
    gen = DefaultInteractiveGenerator()
    snap = _snapshot(
        pd_tasks=({"id": "t1", "status": "todo"},),
        pd_projects={"p": {"failing": False}},
    )
    a = gen.render("q", snap)
    b = gen.render("q", snap)
    assert a == b
