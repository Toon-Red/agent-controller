"""L8 Project Manager component (AC-S16).

L8 is the PM-style layer that talks WITH Preston (oversight) AND with
L7 Dispatch (operational view). It does not write code, run tests, or
dispatch tasks; it observes, narrates, and surfaces decisions.

This module is the WIRING for L8. The pieces it depends on land in
sibling sub-tasks:

* PD + Calendar state reader -- ``StateReader`` protocol, concrete
  implementation in **AC-S16b**.
* SOD output generator -- ``SODGenerator`` protocol, concrete
  implementation in **AC-S16c**.
* EOD output generator -- ``EODGenerator`` protocol, concrete
  implementation in **AC-S16d**.
* Discord async surface -- ``DiscordSurface`` protocol, concrete
  implementation in **AC-S16e**.
* Dream tab surface -- ``DreamTabSurface`` protocol, concrete
  implementation in **AC-S16f**.

The orchestrator below is intentionally engine-agnostic: it consumes
``EngineRequest`` / ``EngineResponse`` from :mod:`scripts.engine_driver`
and resolves the configured engine via :mod:`scripts.role_config`.
That keeps the L8 component plugged into the same per-role driver
plumbing every other layer uses (AC-S2 + AC-S9 + AC-S10).

The component is invoked one of three ways:

* ``L8ProjectManager.run_sod(snapshot)`` -- start-of-day standup.
* ``L8ProjectManager.run_eod(snapshot)`` -- end-of-day review.
* ``L8ProjectManager.run_interactive(question, snapshot)`` -- Preston
  asks a mid-day question via the Dream tab.

Each path returns an ``L8Output`` envelope with the text + the
selected surface + the engine attribution; the caller (the
``claude_loop_continuous`` runtime per AR-S3h) is responsible for
firing the output through the matching surface adapter.

Conversation surface selection (Preston 2026-05-13 decision):

* SOD/EOD              -> Discord async (push, time-anchored).
* Mid-day interactive  -> Dream tab (sustained dialog).
* Escalations          -> Discord async (push, when Preston is away).

The surface selector is owned here, not by callers, so the decision
rule stays canonical and testable.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from scripts.engine_driver import (
    Dispatcher,
    EngineRequest,
    EngineResponse,
)
from scripts.role_config import Settings


# ---------------------------------------------------------------------------
# Public surface enum
# ---------------------------------------------------------------------------


class Surface(str, Enum):
    """The two L8 conversation surfaces per the 2026-05-13 decision."""

    DREAM_TAB = "dream_tab"
    DISCORD_ASYNC = "discord_async"


# ---------------------------------------------------------------------------
# State snapshot (consumed by L8; produced by AC-S16b state reader)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StateSnapshot:
    """Frozen, point-in-time view of everything L8 needs.

    AC-S16b owns the ``StateReader`` that builds this. L8 only consumes
    it. Keeping the shape narrow + immutable makes the L8 path testable
    without standing up the PD / Calendar MCP servers.

    Fields
    ------
    pd_tasks:
        Open tasks, in the PD shape: list of dicts with at minimum
        ``id``, ``title``, ``status``, ``project`` -- exact schema is
        whatever ``mcp__pipeline-dashboard__list_tasks`` returns.
    pd_research:
        Open research items pending decision.
    pd_requests:
        Open user requests pending triage.
    pd_projects:
        Projects keyed by id; each entry carries ``failing`` /
        ``rollback_needed`` flags so SOD/EOD can highlight broken
        projects.
    calendar_events:
        Today + next-7-days events.
    workflow_state:
        Contents of ``automation-registry/data/workflow_state.json``
        (``last_sod_date``, ``last_eod_date``, plus future fields).
    captured_at:
        Unix timestamp the snapshot was built. Used so generators can
        format dates without re-querying wall-clock state.
    """

    pd_tasks: tuple[Mapping[str, Any], ...] = ()
    pd_research: tuple[Mapping[str, Any], ...] = ()
    pd_requests: tuple[Mapping[str, Any], ...] = ()
    pd_projects: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    calendar_events: tuple[Mapping[str, Any], ...] = ()
    workflow_state: Mapping[str, Any] = field(default_factory=dict)
    # AC-L8DOC1a: documentation-audit payload from GET /api/tasks/audit.
    # Shape: {"by_project": {pid: {total, documented, missing_tests,
    # missing_wiki, missing_both, project_name}}, "total", "documented",
    # "coverage_pct"}. Empty dict when the endpoint is unreachable
    # (fail-open). EOD generator (AC-L8DOC1b) is the canonical consumer.
    documentation_audit: Mapping[str, Any] = field(default_factory=dict)
    captured_at: float = 0.0


# ---------------------------------------------------------------------------
# L7 -> L8 progress message (AC-S16g protocol; consumed by trajectory tracking)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class L7ProgressMessage:
    """One iteration's progress report from L7 Dispatch up to L8.

    Mirrors the JSON shape declared in the L8 template body so the
    wire format and the Python contract stay in lock-step.
    """

    iteration_id: str
    current_task: Mapping[str, Any] = field(default_factory=dict)
    status: str = "running"           # "running" | "blocked" | "done"
    blockers: tuple[str, ...] = ()
    completed_this_iteration: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# L8 output envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class L8Output:
    """What the L8 orchestrator hands back to the loop runtime.

    * ``kind`` -- ``"sod" | "eod" | "interactive" | "escalation"``
    * ``surface`` -- which surface the caller should dispatch through.
    * ``text`` -- the PM-style narrative.
    * ``engine`` -- the resolved engine id (for AC-S11 telemetry).
    * ``attribution`` -- per-source byte-count (mirrors EngineResponse).
    * ``trajectory_delta`` -- material trajectory changes detected
      while processing the L7 progress feed. Empty unless the caller
      passed ``progress_feed=...``.
    """

    kind: str
    surface: Surface
    text: str
    engine: str
    attribution: Mapping[str, int] = field(default_factory=dict)
    trajectory_delta: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Protocols (sub-tasks plug in here)
# ---------------------------------------------------------------------------


@runtime_checkable
class StateReader(Protocol):
    """Builds a ``StateSnapshot``. Concrete impl in AC-S16b."""

    def snapshot(self) -> StateSnapshot: ...


@runtime_checkable
class SODGenerator(Protocol):
    """Renders the SOD prompt fed to the L8 engine. AC-S16c."""

    def render(self, snapshot: StateSnapshot) -> str: ...


@runtime_checkable
class EODGenerator(Protocol):
    """Renders the EOD prompt fed to the L8 engine. AC-S16d."""

    def render(self, snapshot: StateSnapshot) -> str: ...


@runtime_checkable
class InteractiveGenerator(Protocol):
    """Renders the mid-day-question prompt. Owned here, no sub-task."""

    def render(self, question: str, snapshot: StateSnapshot) -> str: ...


@runtime_checkable
class DreamTabSurface(Protocol):
    """Push an L8Output to the Dream tab. AC-S16f."""

    def post(self, output: L8Output) -> None: ...


@runtime_checkable
class DiscordSurface(Protocol):
    """Push an L8Output to Discord async. AC-S16e."""

    def post(self, output: L8Output) -> None: ...


# ---------------------------------------------------------------------------
# Built-in interactive generator (sub-tasks own the SOD/EOD variants)
# ---------------------------------------------------------------------------


class DefaultInteractiveGenerator:
    """Minimal interactive renderer.

    The SOD/EOD generators live in their own sub-tasks because their
    output replaces hand-crafted templates in ``dream/orchestrator.py``
    and the format needs to be PR-reviewable in isolation. The
    interactive renderer has no such constraint; it is the L8 body
    prompt + the user's question + a snapshot summary, period.
    """

    PROMPT_PREFIX = (
        "You are L8 -- the PM oversight layer. Preston has asked a "
        "question via the Dream tab. Answer in 2-3 paragraphs maximum, "
        "lead with the headline, back it with 2-4 concrete data points "
        "from the snapshot, end with a decision needed (if any) or "
        "'no action needed.'"
    )

    def render(self, question: str, snapshot: StateSnapshot) -> str:
        snapshot_summary = _summarise_snapshot(snapshot)
        return (
            f"{self.PROMPT_PREFIX}\n\n"
            f"=== Preston's question ===\n{question.strip()}\n\n"
            f"=== Snapshot ===\n{snapshot_summary}"
        )


def _summarise_snapshot(snapshot: StateSnapshot) -> str:
    """Compact, deterministic snapshot summary for prompt construction.

    Deliberately terse: the engine reads structured data better than
    prose. We trade readability of the prompt for determinism (so the
    same snapshot always produces the same bytes -> caching + replay).
    """
    failing = sorted(
        pid for pid, p in snapshot.pd_projects.items()
        if p.get("failing") or p.get("rollback_needed")
    )
    by_status: dict[str, int] = {}
    for task in snapshot.pd_tasks:
        status = str(task.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
    parts = [
        f"projects.failing={failing}",
        f"tasks.by_status={dict(sorted(by_status.items()))}",
        f"research.open={len(snapshot.pd_research)}",
        f"requests.open={len(snapshot.pd_requests)}",
        f"calendar.events_today_plus_7={len(snapshot.calendar_events)}",
        f"workflow.last_sod_date={snapshot.workflow_state.get('last_sod_date')!r}",
        f"workflow.last_eod_date={snapshot.workflow_state.get('last_eod_date')!r}",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Trajectory tracker -- consumes L7 progress feed
# ---------------------------------------------------------------------------


class TrajectoryTracker:
    """Detect material trajectory shifts in the L7 progress feed.

    Definition of "material" (from the L8 template body):

    * A previously-on-track goal moves to ``blocked``.
    * A blocker becomes critical (string starts with ``"critical:"``).
    * A previously-running task slips its deadline (``status="done"``
      with the iteration's ``current_task.deadline_slipped=True``).

    Stateful: tracks per-task status across messages so we surface
    *transitions*, not steady state.
    """

    CRITICAL_PREFIX = "critical:"

    def __init__(self) -> None:
        self._task_state: dict[str, str] = {}

    def observe(self, message: L7ProgressMessage) -> tuple[str, ...]:
        """Return zero-or-more material-shift descriptions for ``message``."""
        deltas: list[str] = []
        task = message.current_task or {}
        task_id = str(task.get("id") or "")
        prev = self._task_state.get(task_id)
        if task_id:
            self._task_state[task_id] = message.status

        # Transition: was running, now blocked.
        if prev == "running" and message.status == "blocked":
            title = task.get("title") or task_id
            deltas.append(f"{task_id} ({title}): on-track -> blocked")

        # Critical blockers (regardless of prior state).
        for blocker in message.blockers:
            if blocker.lower().startswith(self.CRITICAL_PREFIX):
                deltas.append(f"{task_id}: critical blocker: {blocker}")

        # Deadline slip flagged on the current_task payload.
        if task.get("deadline_slipped"):
            deltas.append(
                f"{task_id} ({task.get('title') or task_id}): deadline slipped"
            )

        return tuple(deltas)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


PROJECT_MANAGER_ROLE = "project-manager"


class L8ProjectManager:
    """The L8 Project Manager component.

    Construction
    ------------

    * ``settings`` -- :class:`Settings` from ``templates/settings.json``.
      Used to resolve the configured engine for ``project-manager``.
    * ``dispatcher`` -- :class:`Dispatcher` carrying every engine driver
      L8 might be configured to use. Caller wires this up in the
      loop runtime (AR-S3h) before invoking L8.
    * ``state_reader`` -- AC-S16b implementation. L8 will call
      ``state_reader.snapshot()`` at the top of every public method
      that does not already receive an explicit snapshot.
    * ``sod_generator`` / ``eod_generator`` -- AC-S16c / AC-S16d.
    * ``dream_tab`` / ``discord`` -- AC-S16f / AC-S16e adapters.
    * ``interactive_generator`` -- defaults to
      :class:`DefaultInteractiveGenerator`.
    * ``role`` -- role-id used for engine resolution and telemetry.
      Defaults to ``"project-manager"`` (matches ``templates/settings.json``).

    Behaviour
    ---------

    Every ``run_*`` method:

    1. Builds (or reuses) a snapshot.
    2. Renders the prompt via the appropriate generator.
    3. Resolves the configured engine (defaults to ``claude-opus``).
    4. Runs the engine with role-tagged ``EngineRequest`` so the AC-S15
       observability stream picks up ``agent_role="project-manager"``,
       ``level="L8"``.
    5. Wraps the response in an :class:`L8Output` with the surface
       picked per the 2026-05-13 routing rule.
    6. If ``dispatch=True`` (the default), pushes through the matching
       surface adapter as well as returning the envelope.

    The orchestrator never imports ``anthropic`` / Ollama / OpenAI /
    Google SDKs directly -- that is what :mod:`scripts.engine_driver`
    + AC-S10's driver bundle is for.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        dispatcher: Dispatcher,
        state_reader: Optional[StateReader] = None,
        sod_generator: Optional[SODGenerator] = None,
        eod_generator: Optional[EODGenerator] = None,
        dream_tab: Optional[DreamTabSurface] = None,
        discord: Optional[DiscordSurface] = None,
        interactive_generator: Optional[InteractiveGenerator] = None,
        role: str = PROJECT_MANAGER_ROLE,
        clock: Any = time.time,
    ) -> None:
        self._settings = settings
        self._dispatcher = dispatcher
        self._state_reader = state_reader
        self._sod_generator = sod_generator
        self._eod_generator = eod_generator
        self._dream_tab = dream_tab
        self._discord = discord
        self._interactive_generator = (
            interactive_generator or DefaultInteractiveGenerator()
        )
        self._role = role
        self._clock = clock
        self._trajectory = TrajectoryTracker()

        # Fail-fast: confirm the role is configured at construction
        # time so an out-of-the-box install doesn't blow up on the
        # first SOD when Preston is asleep.
        if role not in settings.roles:
            raise ValueError(
                f"role {role!r} not in templates/settings.json; "
                f"known roles: {sorted(settings.roles)}"
            )
        cfg = settings.roles[role]
        if cfg.level != "L8":
            raise ValueError(
                f"role {role!r} is level {cfg.level!r}, expected 'L8'"
            )

    # ------------------------------------------------------------------
    # Public entrypoints
    # ------------------------------------------------------------------

    def run_sod(
        self,
        snapshot: Optional[StateSnapshot] = None,
        *,
        dispatch: bool = True,
    ) -> L8Output:
        """Generate the Start-of-Day standup post.

        Routed to Discord async (push). The Dream tab subscriber will
        see it via Discord's webhook fan-out per AC-S16e.
        """
        if self._sod_generator is None:
            raise RuntimeError(
                "run_sod called but no SOD generator is configured "
                "(land AC-S16c and wire it in)"
            )
        snap = snapshot or self._require_snapshot()
        prompt = self._sod_generator.render(snap)
        response = self._invoke_engine(prompt=prompt, label="sod")
        output = L8Output(
            kind="sod",
            surface=Surface.DISCORD_ASYNC,
            text=response.text,
            engine=self._resolved_engine(),
            attribution=dict(response.attribution),
        )
        if dispatch:
            self._dispatch(output)
        return output

    def run_eod(
        self,
        snapshot: Optional[StateSnapshot] = None,
        *,
        dispatch: bool = True,
    ) -> L8Output:
        """Generate the End-of-Day review post.

        Routed to Discord async (push). Mirrors SOD routing.
        """
        if self._eod_generator is None:
            raise RuntimeError(
                "run_eod called but no EOD generator is configured "
                "(land AC-S16d and wire it in)"
            )
        snap = snapshot or self._require_snapshot()
        prompt = self._eod_generator.render(snap)
        response = self._invoke_engine(prompt=prompt, label="eod")
        output = L8Output(
            kind="eod",
            surface=Surface.DISCORD_ASYNC,
            text=response.text,
            engine=self._resolved_engine(),
            attribution=dict(response.attribution),
        )
        if dispatch:
            self._dispatch(output)
        return output

    def run_interactive(
        self,
        question: str,
        snapshot: Optional[StateSnapshot] = None,
        *,
        dispatch: bool = True,
    ) -> L8Output:
        """Answer a mid-day interactive question.

        Routed to Dream tab (sustained dialog). Discord is not used
        for these -- they would spam the channel.
        """
        if not question or not question.strip():
            raise ValueError("interactive question must be non-empty")
        snap = snapshot or self._require_snapshot()
        prompt = self._interactive_generator.render(question, snap)
        response = self._invoke_engine(prompt=prompt, label="interactive")
        output = L8Output(
            kind="interactive",
            surface=Surface.DREAM_TAB,
            text=response.text,
            engine=self._resolved_engine(),
            attribution=dict(response.attribution),
        )
        if dispatch:
            self._dispatch(output)
        return output

    def observe_l7_progress(
        self,
        message: L7ProgressMessage,
        *,
        snapshot: Optional[StateSnapshot] = None,
        dispatch: bool = True,
        enforce_gate: bool = False,
    ) -> Optional[L8Output]:
        """Consume one L7 -> L8 progress message; escalate if material.

        Returns ``None`` when nothing material happened (the common
        case; L8 only speaks up when there's something to say). Returns
        an :class:`L8Output` with ``kind="escalation"`` and ``surface=
        DISCORD_ASYNC`` when a material trajectory shift was detected.

        47c52660: when ``enforce_gate=True``, the L7->L8 gate runs
        BEFORE the trajectory observer fires -- if the project's qa
        run isn't green for today, ``LevelGateViolation`` propagates
        and no escalation is constructed. Off by default during the
        AC-LEVEL-WIRE1 rollout so existing test fixtures + the
        progress-observer suite stay green; production loop runtime
        flips it on at boot once green-qa observability is in
        place.
        """
        if enforce_gate:
            from scripts.level_gate_enforcer import enforce
            pid = ""
            ct = message.current_task or {}
            if isinstance(ct, Mapping):
                pid = str(ct.get("project_id") or ct.get("pd_project_id") or "")
            if pid:
                enforce("L7->L8", pid)
        deltas = self._trajectory.observe(message)
        if not deltas:
            return None
        snap = snapshot or self._maybe_snapshot()
        prompt = _render_escalation_prompt(message, deltas, snap)
        response = self._invoke_engine(prompt=prompt, label="escalation")
        output = L8Output(
            kind="escalation",
            surface=Surface.DISCORD_ASYNC,
            text=response.text,
            engine=self._resolved_engine(),
            attribution=dict(response.attribution),
            trajectory_delta=deltas,
        )
        if dispatch:
            self._dispatch(output)
        return output

    # ------------------------------------------------------------------
    # Hooks for the loop runtime (AR-S3h)
    # ------------------------------------------------------------------

    def should_run_sod(self, snapshot: Optional[StateSnapshot] = None,
                       today_iso: Optional[str] = None) -> bool:
        """State-aware SOD trigger.

        Mirrors the rule in the L8 template body: SOD fires when
        ``last_sod_date != today``. The ``today_iso`` parameter is
        injectable for tests; production uses :func:`_today_iso`.
        """
        snap = snapshot or self._maybe_snapshot()
        today = today_iso or _today_iso(self._clock)
        last = (snap.workflow_state or {}).get("last_sod_date")
        return last != today

    def should_run_eod(self, snapshot: Optional[StateSnapshot] = None,
                       *, today_iso: Optional[str] = None,
                       hour_of_day: Optional[int] = None) -> bool:
        """State-aware EOD trigger.

        Per the L8 template: ``SOD ran today AND hour >= 17 AND
        last_eod_date != today``. ``hour_of_day`` is injectable for
        tests; production reads ``time.localtime`` via the clock.
        """
        snap = snapshot or self._maybe_snapshot()
        today = today_iso or _today_iso(self._clock)
        hour = hour_of_day if hour_of_day is not None else _hour_of_day(self._clock)
        ws = snap.workflow_state or {}
        return (
            ws.get("last_sod_date") == today
            and hour >= 17
            and ws.get("last_eod_date") != today
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_snapshot(self) -> StateSnapshot:
        snap = self._maybe_snapshot()
        if snap is None:
            raise RuntimeError(
                "no StateReader configured and no snapshot was passed; "
                "wire AC-S16b's StateReader or hand a snapshot in"
            )
        return snap

    def _maybe_snapshot(self) -> Optional[StateSnapshot]:
        if self._state_reader is None:
            return None
        return self._state_reader.snapshot()

    def _resolved_engine(self) -> str:
        return self._settings.resolve_engine(self._role)

    def _invoke_engine(self, *, prompt: str, label: str) -> EngineResponse:
        engine_id = self._resolved_engine()
        driver = self._dispatcher.resolve(engine_id)
        request = EngineRequest(
            prompt=prompt,
            context="",
            role=self._role,
            level="L8",
            extras={"l8_invocation": label},
        )
        return driver.run(request)

    def _dispatch(self, output: L8Output) -> None:
        if output.surface is Surface.DREAM_TAB:
            if self._dream_tab is None:
                # The loop runtime is free to skip dispatch (dispatch=
                # False) when the Dream tab is not yet running. If it
                # asked us to dispatch but we have no adapter, that's
                # a wiring bug -- fail loudly.
                raise RuntimeError(
                    "L8Output routed to Dream tab but no adapter is "
                    "configured; pass dispatch=False or wire AC-S16f"
                )
            self._dream_tab.post(output)
        elif output.surface is Surface.DISCORD_ASYNC:
            if self._discord is None:
                raise RuntimeError(
                    "L8Output routed to Discord async but no adapter "
                    "is configured; pass dispatch=False or wire AC-S16e"
                )
            self._discord.post(output)
        else:  # pragma: no cover - exhaustive enum
            raise AssertionError(f"unknown surface {output.surface!r}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_escalation_prompt(
    message: L7ProgressMessage,
    deltas: tuple[str, ...],
    snapshot: Optional[StateSnapshot],
) -> str:
    delta_lines = "\n".join(f"- {d}" for d in deltas)
    snap_summary = _summarise_snapshot(snapshot) if snapshot else "<no snapshot>"
    return (
        "You are L8 -- the PM oversight layer. L7 Dispatch has surfaced a "
        "material trajectory shift. Generate a short Discord escalation "
        "post for Preston: lead with the headline, list the shifts in "
        "bullets, end with the decision Preston needs to make. No prose "
        "padding.\n\n"
        f"=== L7 progress ===\n"
        f"iteration_id={message.iteration_id}\n"
        f"current_task={dict(message.current_task)}\n"
        f"status={message.status}\n"
        f"blockers={list(message.blockers)}\n"
        f"completed_this_iteration={list(message.completed_this_iteration)}\n\n"
        f"=== Material shifts ===\n{delta_lines}\n\n"
        f"=== Snapshot ===\n{snap_summary}"
    )


def _today_iso(clock: Any) -> str:
    """ISO date for the local-time today. Injectable clock for tests."""
    t = clock() if callable(clock) else clock
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _hour_of_day(clock: Any) -> int:
    t = clock() if callable(clock) else clock
    return time.localtime(t).tm_hour


__all__ = [
    "DefaultInteractiveGenerator",
    "DiscordSurface",
    "DreamTabSurface",
    "EODGenerator",
    "InteractiveGenerator",
    "L7ProgressMessage",
    "L8Output",
    "L8ProjectManager",
    "PROJECT_MANAGER_ROLE",
    "SODGenerator",
    "StateReader",
    "StateSnapshot",
    "Surface",
    "TrajectoryTracker",
]
