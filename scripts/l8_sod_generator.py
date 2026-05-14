"""L8 Start-of-Day (SOD) output generator (AC-S16c).

This module produces the **Morning Standup** that L8 fires at the top
of every day. It replaces the hand-crafted Discord embed currently
living in ``dream/orchestrator.py:run_morning`` (see PD task
AC-S16c) with a *state-aware* generator: same five sections every
morning, but their contents are derived from a fresh
:class:`scripts.l8_project_manager.StateSnapshot` (AC-S16b) instead
of a fixed template.

The generator has two layers:

1. **Deterministic section extraction** -- :func:`extract_sod_sections`
   turns a snapshot into a :class:`SODSections` dataclass. This is
   pure / side-effect-free, so SOD tests can assert that a mocked
   snapshot produces the expected per-section content without
   touching an LLM.
2. **Prompt rendering** -- :class:`L8SODGenerator.render` is the
   :class:`scripts.l8_project_manager.SODGenerator` implementation
   the L8 PM consumes. It embeds the extracted sections into a
   PM-style prompt that asks the configured engine for a short,
   PM-tone standup body.

The engine's response text lands in :class:`L8Output.text`. To turn
that text into the two surfaces Preston actually reads -- Discord
embed + Dream-UI markdown -- callers use:

* :func:`build_discord_embed` -- returns the embed payload dict
  ``dream/orchestrator.py`` ships through ``_discord_notify``.
* :func:`build_dream_markdown` -- returns a Dream-tab-friendly
  markdown string.

A small workflow-state helper (:func:`mark_sod_complete`) updates
``automation-registry/data/workflow_state.json``'s ``last_sod_date``
after a successful SOD; the field is what the L8 state-machine reads
on the next iteration to decide whether SOD has already fired today.

Design notes (Preston 2026-05-14):

* **Generated > templated.** The prompt encodes the data; the
  engine composes prose. Section *headings* are fixed (so the
  reader's eye finds the same shape every day) but *body* is fresh.
* **Read-only on the snapshot.** Everything here treats the
  ``StateSnapshot`` as immutable. We never mutate caller payloads.
* **Deterministic ordering.** Section content is sorted by
  ``(priority_rank, id)`` so two runs against the same snapshot
  produce byte-identical prompts (caches, replay).
* **No engine SDK imports.** This module is pure shaping; the
  engine call goes through :class:`L8ProjectManager`, which in turn
  goes through :mod:`scripts.engine_driver`.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from scripts.l8_project_manager import StateSnapshot


# ---------------------------------------------------------------------------
# Section model
# ---------------------------------------------------------------------------


# Priority ranking mirrors what Pipeline Dashboard emits today
# (critical > high > normal > low). Unknown priorities sort after
# the canonical four.
PRIORITY_RANK: Mapping[str, int] = {
    "critical": 0,
    "high": 1,
    "normal": 2,
    "low": 3,
}
_UNKNOWN_PRIORITY_RANK = 10


# Section caps. Discord embeds have a 1024-char field limit; we cap
# the number of rows up-front so the embed stays comfortably under
# that even with long titles. Sections summarise the cap in the
# "+N more..." suffix when truncated.
DEFAULT_OVERDUE_CAP = 5
DEFAULT_DECISIONS_CAP = 5
DEFAULT_AGENTS_CAP = 5
DEFAULT_FAILING_PROJECTS_CAP = 5
DEFAULT_CALENDAR_CAP = 5


@dataclass(frozen=True)
class SODSections:
    """Structured per-section content extracted from a snapshot.

    The fields mirror the five sections the L8 template body calls
    out (overdue, decisions needed, trajectory delta, agents being
    assigned, plus the calendar / failing-projects context).

    Each task / item is a plain dict (the original PD shape) so the
    Discord embed builder can pick whichever fields it wants without
    a re-shape pass.

    Fields
    ------
    today_iso:
        ISO date string for the standup header. Derived from
        ``snapshot.captured_at`` so two runs at the same instant
        produce the same date.
    overdue:
        Tasks whose ``deadline``/``due_date`` is in the past or
        whose ``status`` is ``"blocked"`` (the operational proxy
        for "this should already be done").
    decisions_needed:
        Open research items + open user requests pending Preston's
        triage. (Both surfaces ask for a yes/no/route call.)
    trajectory_delta:
        Free-text bullets describing what shifted since yesterday.
        Empty when ``last_sod_date`` is missing or no shift data
        was carried in ``snapshot.workflow_state``.
    agents_being_assigned:
        Top N tasks (priority + status order) that L8 expects L7
        Dispatch to spawn agents for today.
    failing_projects:
        Project ids flagged ``failing=true`` or
        ``rollback_needed=true`` in PD.
    calendar_today:
        Today's calendar events (verbatim from the snapshot).
    previous_sod_date:
        The previous run's ``last_sod_date``, or ``None`` if SOD has
        never fired. Used for the trajectory header ("delta since
        2026-05-13").
    open_task_counts:
        Per-status open-task counts, useful when the engine wants
        a one-line ecosystem health snapshot.
    """

    today_iso: str = ""
    overdue: tuple[Mapping[str, Any], ...] = ()
    decisions_needed: tuple[Mapping[str, Any], ...] = ()
    trajectory_delta: tuple[str, ...] = ()
    agents_being_assigned: tuple[Mapping[str, Any], ...] = ()
    failing_projects: tuple[str, ...] = ()
    calendar_today: tuple[Mapping[str, Any], ...] = ()
    previous_sod_date: Optional[str] = None
    open_task_counts: Mapping[str, int] = field(default_factory=dict)

    def is_quiet(self) -> bool:
        """True when nothing material is in flight.

        A "quiet" SOD still goes out -- the daily ritual matters --
        but the engine can lean on the shorter prompt path.
        """
        return not any((
            self.overdue,
            self.decisions_needed,
            self.trajectory_delta,
            self.agents_being_assigned,
            self.failing_projects,
        ))


# ---------------------------------------------------------------------------
# Section extraction (pure)
# ---------------------------------------------------------------------------


def extract_sod_sections(
    snapshot: StateSnapshot,
    *,
    overdue_cap: int = DEFAULT_OVERDUE_CAP,
    decisions_cap: int = DEFAULT_DECISIONS_CAP,
    agents_cap: int = DEFAULT_AGENTS_CAP,
    failing_cap: int = DEFAULT_FAILING_PROJECTS_CAP,
    calendar_cap: int = DEFAULT_CALENDAR_CAP,
    now: Optional[float] = None,
) -> SODSections:
    """Derive the SOD section payload from a snapshot.

    Pure function. Two calls with the same snapshot return byte-equal
    ``SODSections`` so the downstream prompt is deterministic.

    Parameters
    ----------
    snapshot:
        State at the top of the iteration. Must already be populated;
        this function does not call out to MCP.
    overdue_cap / decisions_cap / agents_cap / failing_cap / calendar_cap:
        Maximum rows kept in each list. Values past the cap are
        dropped (the standup body uses "+N more" suffixes).
    now:
        Override for the "today" reference. Falls back to
        ``snapshot.captured_at`` so SOD bodies stay stable when the
        same snapshot is re-rendered (cache, replay).
    """
    today_iso = _iso_date(now if now is not None else snapshot.captured_at)
    today_ts = float(now) if now is not None else float(snapshot.captured_at)

    overdue = _select_overdue(snapshot.pd_tasks, today_ts=today_ts)[:overdue_cap]
    decisions = _select_decisions(
        research=snapshot.pd_research,
        requests=snapshot.pd_requests,
    )[:decisions_cap]
    agents = _select_agents_being_assigned(snapshot.pd_tasks)[:agents_cap]
    failing = _select_failing_projects(snapshot.pd_projects)[:failing_cap]
    cal_today = tuple(_as_event(ev) for ev in _calendar_today_or_events(snapshot))[:calendar_cap]

    trajectory = _build_trajectory_delta(snapshot)
    open_counts = _open_task_counts(snapshot.pd_tasks)

    prev_sod = snapshot.workflow_state.get("last_sod_date") if snapshot.workflow_state else None

    return SODSections(
        today_iso=today_iso,
        overdue=overdue,
        decisions_needed=decisions,
        trajectory_delta=trajectory,
        agents_being_assigned=agents,
        failing_projects=failing,
        calendar_today=cal_today,
        previous_sod_date=prev_sod if isinstance(prev_sod, str) else None,
        open_task_counts=open_counts,
    )


# ``StateSnapshot`` (L8 PM canonical shape) only carries
# ``calendar_events`` -- the today/upcoming split lives on the richer
# ``PDCalendarSnapshot``. Provide a thin shim so the extractor can
# accept either by attribute-probing.
def _calendar_today_or_events(snap: StateSnapshot) -> Sequence[Mapping[str, Any]]:
    today = getattr(snap, "calendar_today", None)
    if today:
        return tuple(today)
    return tuple(snap.calendar_events)


def _select_overdue(
    tasks: Sequence[Mapping[str, Any]],
    *,
    today_ts: float,
) -> tuple[Mapping[str, Any], ...]:
    """Tasks whose deadline is past OR whose status is 'blocked'.

    Blocked counts as overdue for the standup because the PM cares
    about "what isn't moving" more than the literal due date.
    """
    items: list[tuple[tuple[int, str], Mapping[str, Any]]] = []
    for task in tasks:
        status = str(task.get("status") or "").lower()
        if status == "done":
            continue
        is_overdue = False
        deadline = task.get("deadline") or task.get("due_date") or task.get("due")
        if deadline is not None:
            ddl_ts = _parse_iso_to_ts(deadline)
            if ddl_ts is not None and ddl_ts < today_ts:
                is_overdue = True
        if not is_overdue and status == "blocked":
            is_overdue = True
        if not is_overdue:
            continue
        sort_key = _priority_sort_key(task)
        items.append((sort_key, dict(task)))
    items.sort(key=lambda pair: pair[0])
    return tuple(task for _, task in items)


def _select_decisions(
    *,
    research: Sequence[Mapping[str, Any]],
    requests: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    """Open research + open user requests, tagged with their source."""
    out: list[Mapping[str, Any]] = []
    for r in research:
        entry = dict(r)
        entry.setdefault("source", "research")
        out.append(entry)
    for r in requests:
        entry = dict(r)
        entry.setdefault("source", "request")
        out.append(entry)
    out.sort(key=_priority_sort_key)
    return tuple(out)


def _select_agents_being_assigned(
    tasks: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    """Top tasks (priority + todo/in_progress) L8 expects L7 to spawn.

    The actual assignment lives in L7 Dispatch; this section is L8's
    PM-style "here's what should land today" pitch, not the
    operational dispatch order.
    """
    items: list[tuple[tuple[int, str], Mapping[str, Any]]] = []
    for task in tasks:
        status = str(task.get("status") or "").lower()
        if status not in {"todo", "in_progress"}:
            continue
        items.append((_priority_sort_key(task), dict(task)))
    items.sort(key=lambda pair: pair[0])
    return tuple(task for _, task in items)


def _select_failing_projects(
    projects: Mapping[str, Mapping[str, Any]],
) -> tuple[str, ...]:
    """Project ids flagged failing=true OR rollback_needed=true."""
    return tuple(
        sorted(
            pid for pid, p in projects.items()
            if (p or {}).get("failing") or (p or {}).get("rollback_needed")
        )
    )


def _build_trajectory_delta(snapshot: StateSnapshot) -> tuple[str, ...]:
    """Bullets describing material shifts since the previous SOD.

    The snapshot may carry a ``trajectory_delta`` field on
    ``workflow_state`` populated by the loop runtime (L7 progress
    feed accumulates here between SODs). If absent, returns ().
    The L8 prompt then leaves that section as "no material shifts".
    """
    ws = snapshot.workflow_state or {}
    raw = ws.get("trajectory_delta") or ws.get("trajectory_deltas") or ()
    if isinstance(raw, str):
        # Allow a single newline-joined string for convenience.
        raw = [line for line in raw.split("\n") if line.strip()]
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in raw if str(item).strip())


def _open_task_counts(tasks: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        status = str(task.get("status") or "unknown").lower()
        if status == "done":
            continue
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _priority_sort_key(item: Mapping[str, Any]) -> tuple[int, str]:
    priority = str(item.get("priority") or "").lower()
    rank = PRIORITY_RANK.get(priority, _UNKNOWN_PRIORITY_RANK)
    pid = str(item.get("id") or item.get("title") or "")
    return (rank, pid)


def _as_event(event: Mapping[str, Any]) -> Mapping[str, Any]:
    # Defensive freeze: callers should never see a mutable view of
    # the snapshot's payload through the sections.
    return dict(event)


# ---------------------------------------------------------------------------
# Prompt rendering (the SODGenerator protocol implementation)
# ---------------------------------------------------------------------------


_PROMPT_PREFIX = (
    "You are L8 -- the PM oversight layer of the agent-controller "
    "hierarchy. Generate today's MORNING STANDUP for Preston.\n"
    "\n"
    "Tone: PM briefing the operator. Short sentences. Items, not "
    "paragraphs. No filler ('exciting day ahead' etc).\n"
    "\n"
    "Output sections, in this exact order. Use the section headings "
    "verbatim. Skip a section ONLY if the data block below it is "
    "literally empty -- never invent items, never duplicate items "
    "across sections, never drop a section heading the data supports.\n"
    "\n"
    "  1. Headline       -- one sentence: state of the world.\n"
    "  2. Overdue        -- bullets, one per overdue/blocked task.\n"
    "  3. Decisions needed -- bullets, one per open research/request.\n"
    "  4. Trajectory delta -- what changed since the previous SOD.\n"
    "  5. Agents today   -- what L7 Dispatch should spawn today.\n"
    "  6. Calendar       -- today's events (skip if none).\n"
    "\n"
    "End with a single line: 'Decision needed:' OR 'No action needed.'"
)


class L8SODGenerator:
    """Concrete :class:`SODGenerator` for the L8 PM.

    Construction takes the per-section caps so callers can tighten
    the budget when the downstream surface is space-constrained
    (Discord embed fields cap at 1024 chars).

    Behaviour
    ---------
    :meth:`render` returns the prompt string the L8 PM hands to the
    configured engine. :meth:`extract` returns the underlying
    :class:`SODSections` so callers that already have the structured
    view (e.g., the Discord embed builder) can skip the second pass.
    """

    PROMPT_PREFIX = _PROMPT_PREFIX

    def __init__(
        self,
        *,
        overdue_cap: int = DEFAULT_OVERDUE_CAP,
        decisions_cap: int = DEFAULT_DECISIONS_CAP,
        agents_cap: int = DEFAULT_AGENTS_CAP,
        failing_cap: int = DEFAULT_FAILING_PROJECTS_CAP,
        calendar_cap: int = DEFAULT_CALENDAR_CAP,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        self._overdue_cap = overdue_cap
        self._decisions_cap = decisions_cap
        self._agents_cap = agents_cap
        self._failing_cap = failing_cap
        self._calendar_cap = calendar_cap
        self._now = now

    # ------------------------------------------------------------------
    # SODGenerator protocol
    # ------------------------------------------------------------------

    def render(self, snapshot: StateSnapshot) -> str:
        sections = self.extract(snapshot)
        return self._format_prompt(sections)

    # ------------------------------------------------------------------
    # Helpers callers can reuse (Discord embed builder, tests)
    # ------------------------------------------------------------------

    def extract(self, snapshot: StateSnapshot) -> SODSections:
        now = self._now() if callable(self._now) else None
        return extract_sod_sections(
            snapshot,
            overdue_cap=self._overdue_cap,
            decisions_cap=self._decisions_cap,
            agents_cap=self._agents_cap,
            failing_cap=self._failing_cap,
            calendar_cap=self._calendar_cap,
            now=now,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _format_prompt(self, sections: SODSections) -> str:
        # Each "=== <SECTION DATA> ===" block carries the structured
        # rows; the engine consumes the structured form and prose-
        # writes the standup. Empty lists are still surfaced (with
        # the literal "(none)" marker) so the engine never has to
        # guess whether a section is empty or omitted.
        blocks: list[str] = [self.PROMPT_PREFIX, ""]
        blocks.append(f"=== TODAY ===\n{sections.today_iso}")
        if sections.previous_sod_date:
            blocks.append(
                f"=== PREVIOUS SOD ===\n{sections.previous_sod_date}"
            )
        blocks.append(
            "=== OPEN TASK COUNTS ===\n"
            + json.dumps(dict(sections.open_task_counts), sort_keys=True)
        )
        blocks.append(_format_block("OVERDUE / BLOCKED", _format_task_lines(sections.overdue)))
        blocks.append(
            _format_block("DECISIONS NEEDED", _format_decision_lines(sections.decisions_needed))
        )
        blocks.append(
            _format_block(
                "TRAJECTORY DELTA",
                tuple(sections.trajectory_delta) or ("(none)",),
            )
        )
        blocks.append(
            _format_block(
                "AGENTS BEING ASSIGNED",
                _format_task_lines(sections.agents_being_assigned),
            )
        )
        blocks.append(
            _format_block(
                "FAILING PROJECTS",
                tuple(f"- {pid}" for pid in sections.failing_projects) or ("(none)",),
            )
        )
        blocks.append(
            _format_block("CALENDAR TODAY", _format_calendar_lines(sections.calendar_today))
        )
        return "\n\n".join(blocks).rstrip() + "\n"


def _format_block(heading: str, lines: Sequence[str]) -> str:
    body = "\n".join(lines) if lines else "(none)"
    return f"=== {heading} ===\n{body}"


def _format_task_lines(tasks: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    if not tasks:
        return ("(none)",)
    return tuple(_format_task_line(task) for task in tasks)


def _format_task_line(task: Mapping[str, Any]) -> str:
    tid = task.get("id") or "?"
    title = (task.get("title") or "").strip() or "(untitled)"
    project = task.get("project") or task.get("project_id") or "?"
    priority = task.get("priority") or "?"
    status = task.get("status") or "?"
    deadline = task.get("deadline") or task.get("due_date") or task.get("due") or ""
    parts = [
        f"- [{tid}]",
        f"({project})",
        f"[{priority}/{status}]",
        title,
    ]
    if deadline:
        parts.append(f"-- due {deadline}")
    return " ".join(parts)


def _format_decision_lines(items: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    if not items:
        return ("(none)",)
    out: list[str] = []
    for item in items:
        iid = item.get("id") or "?"
        title = (item.get("title") or item.get("question") or "").strip() or "(untitled)"
        source = item.get("source") or "?"
        priority = item.get("priority") or "?"
        out.append(f"- [{source}:{iid}] [{priority}] {title}")
    return tuple(out)


def _format_calendar_lines(
    events: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    if not events:
        return ("(none)",)
    out: list[str] = []
    for ev in events:
        when = ev.get("start") or ev.get("time") or ev.get("when") or "?"
        title = (ev.get("title") or ev.get("summary") or "").strip() or "(untitled)"
        out.append(f"- {when}: {title}")
    return tuple(out)


# ---------------------------------------------------------------------------
# Surface payloads (Discord embed + Dream-UI markdown)
# ---------------------------------------------------------------------------


# Discord brand-ish colours. Picked to match the existing
# ``dream/orchestrator.py:_discord_notify`` palette so the new
# embed doesn't visually clash when both code paths coexist.
_COLOR_GREEN = 0x57F287   # nominal day
_COLOR_AMBER = 0xFEE75C   # decisions pending / overdue items
_COLOR_RED = 0xED4245     # failing projects


def build_discord_embed(
    body: str,
    sections: SODSections,
    *,
    field_value_cap: int = 1000,
) -> dict[str, Any]:
    """Return the Discord embed payload for the SOD post.

    The embed has:

    * ``title``  -- "Morning Standup -- <date>"
    * ``description`` -- the engine's standup body (capped).
    * ``color`` -- green / amber / red, chosen from the section
      severities (failing projects -> red, overdue / decisions ->
      amber, otherwise green).
    * ``fields`` -- one per non-empty structured section, so a
      reader skimming on mobile can find the rollup without parsing
      the body prose.

    ``field_value_cap`` defaults to 1000 (below the Discord 1024
    field limit, leaving headroom for the section's "+N more" suffix).
    """
    color = _select_embed_color(sections)
    title = f"Morning Standup -- {sections.today_iso}" if sections.today_iso else "Morning Standup"
    description = _truncate(body.strip(), 2000)

    fields: list[dict[str, Any]] = []
    if sections.overdue:
        fields.append({
            "name": f"Overdue / blocked ({len(sections.overdue)})",
            "value": _truncate("\n".join(_format_task_lines(sections.overdue)), field_value_cap),
            "inline": False,
        })
    if sections.decisions_needed:
        fields.append({
            "name": f"Decisions needed ({len(sections.decisions_needed)})",
            "value": _truncate("\n".join(_format_decision_lines(sections.decisions_needed)), field_value_cap),
            "inline": False,
        })
    if sections.trajectory_delta:
        fields.append({
            "name": "Trajectory delta",
            "value": _truncate("\n".join(f"- {line}" for line in sections.trajectory_delta), field_value_cap),
            "inline": False,
        })
    if sections.agents_being_assigned:
        fields.append({
            "name": f"Agents today ({len(sections.agents_being_assigned)})",
            "value": _truncate("\n".join(_format_task_lines(sections.agents_being_assigned)), field_value_cap),
            "inline": False,
        })
    if sections.failing_projects:
        fields.append({
            "name": f"Failing projects ({len(sections.failing_projects)})",
            "value": _truncate(", ".join(sections.failing_projects), field_value_cap),
            "inline": False,
        })
    if sections.calendar_today:
        fields.append({
            "name": f"Calendar today ({len(sections.calendar_today)})",
            "value": _truncate("\n".join(_format_calendar_lines(sections.calendar_today)), field_value_cap),
            "inline": False,
        })

    return {
        "title": title,
        "description": description or "(no standup body)",
        "color": color,
        "fields": fields,
    }


def build_dream_markdown(body: str, sections: SODSections) -> str:
    """Return a Dream-UI-friendly markdown rendering of the standup."""
    header = f"# Morning Standup -- {sections.today_iso}" if sections.today_iso else "# Morning Standup"
    parts: list[str] = [header, "", body.strip(), ""]

    def _section(heading: str, lines: Sequence[str]) -> None:
        if not lines:
            return
        parts.append(f"## {heading}")
        for line in lines:
            parts.append(line if line.startswith("-") else f"- {line}")
        parts.append("")

    _section(
        f"Overdue / blocked ({len(sections.overdue)})",
        _format_task_lines(sections.overdue) if sections.overdue else (),
    )
    _section(
        f"Decisions needed ({len(sections.decisions_needed)})",
        _format_decision_lines(sections.decisions_needed) if sections.decisions_needed else (),
    )
    _section(
        "Trajectory delta",
        tuple(f"- {line}" for line in sections.trajectory_delta),
    )
    _section(
        f"Agents today ({len(sections.agents_being_assigned)})",
        _format_task_lines(sections.agents_being_assigned)
        if sections.agents_being_assigned else (),
    )
    if sections.failing_projects:
        parts.append("## Failing projects")
        for pid in sections.failing_projects:
            parts.append(f"- {pid}")
        parts.append("")
    if sections.calendar_today:
        parts.append("## Calendar today")
        for line in _format_calendar_lines(sections.calendar_today):
            parts.append(line)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _select_embed_color(sections: SODSections) -> int:
    if sections.failing_projects:
        return _COLOR_RED
    if sections.overdue or sections.decisions_needed:
        return _COLOR_AMBER
    return _COLOR_GREEN


def _truncate(text: str, cap: int) -> str:
    if cap <= 0 or len(text) <= cap:
        return text
    # Leave room for the ellipsis suffix.
    return text[: max(0, cap - 3)] + "..."


# ---------------------------------------------------------------------------
# Workflow-state helpers (write side -- updates last_sod_date)
# ---------------------------------------------------------------------------


def mark_sod_complete(
    workflow_state_path: Path | str,
    *,
    today_iso: Optional[str] = None,
    now: Optional[Callable[[], float]] = None,
) -> dict[str, Any]:
    """Update ``automation-registry/data/workflow_state.json`` after a SOD.

    Reads the current file (or treats it as an empty dict if absent),
    sets ``last_sod_date`` to ``today_iso`` (or today per ``now``),
    writes atomically (tmp + replace) so a concurrent reader never
    sees a half-written file.

    Returns the updated state dict.
    """
    path = Path(workflow_state_path)
    if today_iso is None:
        today_iso = _iso_date(now() if callable(now) else time.time())
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            existing = {}
    except FileNotFoundError:
        existing = {}
    except (OSError, ValueError):
        # Treat unreadable / malformed state as empty: SOD must still
        # succeed; the next iteration will re-stamp.
        existing = {}
    existing["last_sod_date"] = today_iso
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return existing


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _iso_date(ts: float) -> str:
    if not ts:
        ts = time.time()
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _parse_iso_to_ts(value: Any) -> Optional[float]:
    """Best-effort ISO-8601 -> unix-ts parser.

    Accepts ``YYYY-MM-DD``, ``YYYY-MM-DDTHH:MM:SS``, with or without
    a trailing ``Z`` / offset. Returns ``None`` on any failure so
    the overdue check degrades to "no deadline" rather than
    crashing the standup.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # Trim trailing 'Z' (UTC marker) so fromisoformat accepts it on
    # Python < 3.11. Drop any offset for simplicity; the standup
    # only needs day-granularity comparisons.
    if text.endswith("Z"):
        text = text[:-1]
    if "T" not in text and len(text) == 10:
        text = text + "T00:00:00"
    try:
        from datetime import datetime as _dt
        dt = _dt.fromisoformat(text)
        return dt.timestamp()
    except ValueError:
        return None


__all__ = [
    "DEFAULT_AGENTS_CAP",
    "DEFAULT_CALENDAR_CAP",
    "DEFAULT_DECISIONS_CAP",
    "DEFAULT_FAILING_PROJECTS_CAP",
    "DEFAULT_OVERDUE_CAP",
    "L8SODGenerator",
    "PRIORITY_RANK",
    "SODSections",
    "build_discord_embed",
    "build_dream_markdown",
    "extract_sod_sections",
    "mark_sod_complete",
]
