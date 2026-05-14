"""PD + Calendar state reader for L8 (AC-S16b).

L8 (the PM oversight layer in :mod:`scripts.l8_project_manager`) needs a
fresh, structured view of the ecosystem at the top of every iteration:

* What projects exist; which are failing or pending rollback?
* What tasks are open, and bucketed by status?
* What research items are open (pending decision)?
* What user requests are open (pending triage)?
* What's on the calendar today + the next 7 days?
* What's the workflow-state machine (last_sod_date / last_eod_date)?

The state itself lives behind two MCP surfaces:

* **Pipeline Dashboard** -- ``mcp__pipeline-dashboard__*`` tools:
  ``get_projects``, ``list_tasks``, ``list_research``, ``list_requests``.
* **Calendar** -- the local Calendar MCP exposing today's and
  upcoming events.

This module is the thin client wrapper L8 invokes once per iteration to
collapse those two surfaces into a single immutable :class:`PDCalendarSnapshot`.
The :class:`PDCalendarStateReader` also satisfies the
:class:`scripts.l8_project_manager.StateReader` protocol so the L8
orchestrator can use it without an adapter:

    pm = L8ProjectManager(
        settings=settings,
        dispatcher=dispatcher,
        state_reader=PDCalendarStateReader(mcp=..., calendar=...),
        ...
    )

Design notes (Preston 2026-05-14):

* **Read-only.** This module never mutates PD / Calendar state. Writes
  go through the L8 PM's action-item path, not the reader.
* **Per-iteration cache.** ``snapshot()`` caches its result on a single
  reader instance. The loop runtime calls :meth:`refresh` at the top
  of each pass to invalidate the cache, so each iteration sees one
  consistent snapshot (no torn reads within a single iteration).
* **Injectable clients.** Both the MCP client and the Calendar client
  are duck-typed Protocols. Tests pass in canned in-memory clients;
  production wires the real MCP transport in the loop runtime.
* **No SDK imports.** This module imports neither ``anthropic`` nor
  any other LLM SDK. It's pure transport + shaping; the only Python
  stdlib used is ``dataclasses`` / ``time`` / ``typing``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Protocol, runtime_checkable

from scripts.l8_project_manager import StateSnapshot


# ---------------------------------------------------------------------------
# Client protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class MCPClient(Protocol):
    """Minimal MCP client contract used by the state reader.

    Implementations call PD MCP tools by their canonical name (e.g.
    ``mcp__pipeline-dashboard__list_tasks``) and return the raw result
    payload. The contract is intentionally narrow:

    * ``call_tool(name, **params)`` returns the tool's raw result
      (a list, dict, or scalar -- whatever the tool emits).
    * Implementations MUST NOT mutate server state from this method;
      this is a read-only client.

    The real production client wraps the Claude Code MCP transport;
    test doubles wrap an in-memory dict.
    """

    def call_tool(self, name: str, /, **params: Any) -> Any: ...


@runtime_checkable
class CalendarClient(Protocol):
    """Calendar client contract used by the state reader.

    Two methods so today's events and the 7-day look-ahead can be
    fetched separately (mirrors the L8 template body's read scopes).
    Each method returns an iterable of event dicts; shape is whatever
    the upstream Calendar MCP emits.
    """

    def today_events(self) -> Iterable[Mapping[str, Any]]: ...

    def upcoming_events(self, days: int) -> Iterable[Mapping[str, Any]]: ...


# ---------------------------------------------------------------------------
# Snapshot dataclass (consumed by SOD/EOD generators in AC-S16c / AC-S16d)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PDCalendarSnapshot:
    """Frozen, point-in-time view of PD + Calendar state.

    This is the richer view consumed by the SOD/EOD generators
    (AC-S16c / AC-S16d). It carries the bucketed-by-status task map
    + the today/upcoming calendar split so generators don't have to
    re-shape the raw lists.

    The :class:`scripts.l8_project_manager.StateSnapshot` is derived
    from this via :meth:`to_state_snapshot` -- L8 PM consumes that
    canonical shape so the wiring between the reader and the
    orchestrator stays narrow.

    Fields
    ------
    projects:
        Projects keyed by id; each entry carries (at minimum) the
        original PD project shape (``failing``, ``rollback_needed``,
        ``stage``, ``branch``, ``version`` ...).
    tasks_by_status:
        Open tasks bucketed by ``status``. Keys are the literal PD
        status strings (``"todo"``, ``"in_progress"``, ``"blocked"``,
        ``"done"`` -- and any other status PD emits). Tasks within
        each bucket are in the order PD returned them.
    open_research:
        Research items pending decision.
    open_requests:
        User requests pending triage.
    calendar_today:
        Today's calendar events.
    calendar_upcoming:
        The next-7-days events (exclusive of today; the reader
        de-duplicates by event id when the Calendar MCP includes
        today's events in the look-ahead).
    workflow_state:
        Contents of ``automation-registry/data/workflow_state.json``
        (``last_sod_date``, ``last_eod_date``, plus future fields).
        The reader treats this as opaque -- it's the SOD/EOD state
        machine's source of truth, and L8 PM owns the rule that
        consumes it.
    captured_at:
        Unix timestamp the snapshot was built. Used by generators
        that need to format date-sensitive headers without re-querying
        wall-clock state.
    """

    projects: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    tasks_by_status: Mapping[str, tuple[Mapping[str, Any], ...]] = field(
        default_factory=dict
    )
    open_research: tuple[Mapping[str, Any], ...] = ()
    open_requests: tuple[Mapping[str, Any], ...] = ()
    calendar_today: tuple[Mapping[str, Any], ...] = ()
    calendar_upcoming: tuple[Mapping[str, Any], ...] = ()
    workflow_state: Mapping[str, Any] = field(default_factory=dict)
    captured_at: float = 0.0

    # ------------------------------------------------------------------
    # Derived views
    # ------------------------------------------------------------------

    def all_open_tasks(self) -> tuple[Mapping[str, Any], ...]:
        """Flatten all status buckets except ``done`` into one tuple.

        SOD/EOD generators that don't care about the per-status split
        can read this. ``done`` is excluded because "open" means
        "still in flight"; if a generator wants completed work it
        reads ``tasks_by_status['done']`` directly.
        """
        flat: list[Mapping[str, Any]] = []
        for status, bucket in self.tasks_by_status.items():
            if status == "done":
                continue
            flat.extend(bucket)
        return tuple(flat)

    def failing_project_ids(self) -> tuple[str, ...]:
        """Project ids flagged ``failing=True`` or ``rollback_needed=True``."""
        return tuple(
            sorted(
                pid for pid, p in self.projects.items()
                if p.get("failing") or p.get("rollback_needed")
            )
        )

    def to_state_snapshot(self) -> StateSnapshot:
        """Project this rich view onto the canonical L8 PM shape.

        :class:`StateSnapshot` is what L8 PM and its existing test
        suite consume; we keep it as the canonical wire format
        between the reader and the orchestrator so adding fields to
        ``PDCalendarSnapshot`` doesn't ripple through L8 PM.
        """
        # Flatten every task (including done) -- L8 PM's existing
        # ``_summarise_snapshot`` does its own by-status rollup and
        # we shouldn't drop information on the way through.
        flat_tasks: list[Mapping[str, Any]] = []
        for bucket in self.tasks_by_status.values():
            flat_tasks.extend(bucket)
        return StateSnapshot(
            pd_tasks=tuple(flat_tasks),
            pd_research=self.open_research,
            pd_requests=self.open_requests,
            pd_projects=dict(self.projects),
            calendar_events=self.calendar_today + self.calendar_upcoming,
            workflow_state=dict(self.workflow_state),
            captured_at=self.captured_at,
        )


# ---------------------------------------------------------------------------
# PD MCP tool names (canonical -- pinned for grep-ability)
# ---------------------------------------------------------------------------

PD_TOOL_GET_PROJECTS = "mcp__pipeline-dashboard__get_projects"
PD_TOOL_LIST_TASKS = "mcp__pipeline-dashboard__list_tasks"
PD_TOOL_LIST_RESEARCH = "mcp__pipeline-dashboard__list_research"
PD_TOOL_LIST_REQUESTS = "mcp__pipeline-dashboard__list_requests"


# Default look-ahead for calendar events. The L8 template body says
# "today's events + the next 7 days"; we keep that as the canonical
# horizon and expose it as a constructor knob for tests.
DEFAULT_CALENDAR_LOOKAHEAD_DAYS = 7


# Status values that count as "open" for the open-tasks rollup. The
# reader still buckets every status PD emits; this set just feeds the
# task description's "open_requests / open_research" filtering rule
# for any PD endpoint that ships closed items in the same response.
OPEN_STATUSES: frozenset[str] = frozenset({"todo", "in_progress", "blocked"})


# ---------------------------------------------------------------------------
# Workflow-state loader (read-only)
# ---------------------------------------------------------------------------


WorkflowStateLoader = Any  # callable: () -> Mapping[str, Any]


def _empty_workflow_state() -> Mapping[str, Any]:
    return {}


# ---------------------------------------------------------------------------
# State reader
# ---------------------------------------------------------------------------


class PDCalendarStateReader:
    """MCP-client wrapper L8 invokes at each iteration.

    Parameters
    ----------
    mcp:
        :class:`MCPClient` for PD tools. Required.
    calendar:
        :class:`CalendarClient` for today's + upcoming events. If
        ``None``, calendar buckets stay empty (useful when the
        Calendar MCP isn't running locally yet -- L8 PM still gets
        PD state).
    workflow_state_loader:
        Zero-arg callable returning the parsed
        ``automation-registry/data/workflow_state.json`` content. If
        ``None``, ``workflow_state`` stays an empty dict. The
        automation-registry side owns this file; the reader treats
        it as read-only.
    calendar_lookahead_days:
        Days passed to ``calendar.upcoming_events``. Defaults to 7.
    clock:
        Time source for ``captured_at``. Injectable for tests.

    Behaviour
    ---------
    * :meth:`read` returns the rich :class:`PDCalendarSnapshot`.
    * :meth:`snapshot` returns the L8 PM canonical :class:`StateSnapshot`
      (so this class satisfies :class:`scripts.l8_project_manager.StateReader`).
    * Both go through the SAME underlying fetch. Within a single
      iteration, repeated calls return the cached value; the loop
      runtime calls :meth:`refresh` at the top of each pass.
    """

    def __init__(
        self,
        *,
        mcp: MCPClient,
        calendar: Optional[CalendarClient] = None,
        workflow_state_loader: Optional[Any] = None,
        calendar_lookahead_days: int = DEFAULT_CALENDAR_LOOKAHEAD_DAYS,
        clock: Any = time.time,
    ) -> None:
        if calendar_lookahead_days < 0:
            raise ValueError(
                f"calendar_lookahead_days must be >= 0 (got {calendar_lookahead_days})"
            )
        self._mcp = mcp
        self._calendar = calendar
        self._workflow_state_loader = workflow_state_loader or _empty_workflow_state
        self._calendar_lookahead_days = calendar_lookahead_days
        self._clock = clock
        self._cache: Optional[PDCalendarSnapshot] = None

    # ------------------------------------------------------------------
    # Public entrypoints
    # ------------------------------------------------------------------

    def read(self) -> PDCalendarSnapshot:
        """Return the rich PD + Calendar snapshot (cached per iteration)."""
        if self._cache is None:
            self._cache = self._build_snapshot()
        return self._cache

    def snapshot(self) -> StateSnapshot:
        """Implement :class:`StateReader` -- L8 PM's canonical shape."""
        return self.read().to_state_snapshot()

    def refresh(self) -> PDCalendarSnapshot:
        """Drop the cache and re-fetch. Called at the top of each loop pass."""
        self._cache = None
        return self.read()

    @property
    def cached(self) -> Optional[PDCalendarSnapshot]:
        """The currently-cached snapshot, or ``None`` if not yet read."""
        return self._cache

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_snapshot(self) -> PDCalendarSnapshot:
        projects_raw = self._mcp.call_tool(PD_TOOL_GET_PROJECTS)
        tasks_raw = self._mcp.call_tool(PD_TOOL_LIST_TASKS)
        research_raw = self._mcp.call_tool(PD_TOOL_LIST_RESEARCH)
        requests_raw = self._mcp.call_tool(PD_TOOL_LIST_REQUESTS)

        projects = _index_projects(projects_raw)
        tasks_by_status = _bucket_tasks_by_status(tasks_raw)
        open_research = _filter_open(research_raw)
        open_requests = _filter_open(requests_raw)

        calendar_today, calendar_upcoming = self._fetch_calendar()

        workflow_state = dict(self._workflow_state_loader() or {})

        return PDCalendarSnapshot(
            projects=projects,
            tasks_by_status=tasks_by_status,
            open_research=open_research,
            open_requests=open_requests,
            calendar_today=calendar_today,
            calendar_upcoming=calendar_upcoming,
            workflow_state=workflow_state,
            captured_at=float(self._clock() if callable(self._clock) else self._clock),
        )

    def _fetch_calendar(
        self,
    ) -> tuple[tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]]:
        if self._calendar is None:
            return (), ()
        today = tuple(_as_dicts(self._calendar.today_events()))
        # De-dupe by id when upcoming() includes today's events too.
        today_ids = {ev.get("id") for ev in today if ev.get("id") is not None}
        upcoming_raw = (
            self._calendar.upcoming_events(self._calendar_lookahead_days)
            if self._calendar_lookahead_days > 0
            else ()
        )
        upcoming = tuple(
            ev for ev in _as_dicts(upcoming_raw)
            if ev.get("id") is None or ev.get("id") not in today_ids
        )
        return today, upcoming


# ---------------------------------------------------------------------------
# Shaping helpers (pure functions -- unit-testable in isolation)
# ---------------------------------------------------------------------------


def _as_dicts(value: Any) -> list[Mapping[str, Any]]:
    """Normalise an MCP/Calendar payload into a list of dicts.

    PD tools return a list of dicts in steady state, but a sad-path
    response (server down, empty, scalar) shouldn't crash the reader.
    Anything non-iterable becomes an empty list; non-mapping entries
    are skipped.
    """
    if value is None:
        return []
    if isinstance(value, Mapping):
        # Some MCP tools wrap the list in {"items": [...]} or
        # {"data": [...]} -- support both, but never invent fields.
        for key in ("items", "data", "results"):
            inner = value.get(key)
            if isinstance(inner, list):
                return [item for item in inner if isinstance(item, Mapping)]
        # A single dict payload is treated as a one-item list.
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _index_projects(value: Any) -> dict[str, Mapping[str, Any]]:
    """Index projects by id, dropping entries without one."""
    projects: dict[str, Mapping[str, Any]] = {}
    for entry in _as_dicts(value):
        pid = entry.get("id") or entry.get("project_id") or entry.get("name")
        if not pid:
            continue
        # Freeze the row as a plain dict so callers can't mutate the
        # MCP payload back through the reader.
        projects[str(pid)] = dict(entry)
    return projects


def _bucket_tasks_by_status(
    value: Any,
) -> dict[str, tuple[Mapping[str, Any], ...]]:
    """Bucket tasks by their ``status`` field. Order-preserving."""
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for task in _as_dicts(value):
        status = str(task.get("status") or "unknown")
        buckets.setdefault(status, []).append(dict(task))
    return {status: tuple(items) for status, items in buckets.items()}


def _filter_open(value: Any) -> tuple[Mapping[str, Any], ...]:
    """Return only items whose ``status`` is open, or whose status is missing.

    PD's ``list_research`` / ``list_requests`` endpoints may already
    pre-filter, but the reader applies the rule defensively so a
    schema drift on the server side doesn't silently leak closed
    items into L8's open-items rollups.
    """
    items: list[Mapping[str, Any]] = []
    for entry in _as_dicts(value):
        status = entry.get("status")
        if status is None:
            items.append(dict(entry))
            continue
        if str(status).lower() in OPEN_STATUSES:
            items.append(dict(entry))
            continue
        # Some PD endpoints use a boolean "open" flag instead of status.
        if entry.get("open") is True:
            items.append(dict(entry))
    return tuple(items)


__all__ = [
    "CalendarClient",
    "DEFAULT_CALENDAR_LOOKAHEAD_DAYS",
    "MCPClient",
    "OPEN_STATUSES",
    "PD_TOOL_GET_PROJECTS",
    "PD_TOOL_LIST_REQUESTS",
    "PD_TOOL_LIST_RESEARCH",
    "PD_TOOL_LIST_TASKS",
    "PDCalendarSnapshot",
    "PDCalendarStateReader",
]
