"""L8 End-of-Day (EOD) output generator (AC-S16d).

Mirror of :mod:`scripts.l8_sod_generator`. Produces the **EOD Review**
L8 fires at the end of every day, replacing the hand-crafted Discord
embed in ``dream/orchestrator.py:run_eod`` with a *state-aware*
generator: same structured sections every evening, but the contents
are derived from a fresh :class:`scripts.l8_project_manager.StateSnapshot`
(AC-S16b) plus the day's completion deltas.

Two layers (same shape as SOD):

1. **Deterministic section extraction** -- :func:`extract_eod_sections`
   turns a snapshot + the SOD's reference timestamp into an
   :class:`EODSections` dataclass. Pure / side-effect free.
2. **Prompt rendering** -- :class:`L8EODGenerator.render` is the
   :class:`scripts.l8_project_manager.EODGenerator` implementation
   the L8 PM consumes.

Surface payloads:
  * :func:`build_discord_embed` -- Discord embed for the EOD post.
  * :func:`build_dream_markdown` -- Dream-UI-friendly markdown.

A small workflow-state helper (:func:`mark_eod_complete`) updates
``automation-registry/data/workflow_state.json``'s ``last_eod_date``
after a successful EOD; the state-machine reads it to gate tomorrow's
SOD (SOD always before EOD within a day).

Design parity with SOD:
  * Generated > templated.
  * Read-only on the snapshot.
  * Deterministic ordering: ``(priority_rank, id)``.
  * No engine SDK imports.
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

PRIORITY_RANK: Mapping[str, int] = {
    "critical": 0,
    "high": 1,
    "normal": 2,
    "low": 3,
}
_UNKNOWN_PRIORITY_RANK = 10

DEFAULT_DONE_CAP = 8
DEFAULT_CARRY_CAP = 5
DEFAULT_AT_RISK_CAP = 5
DEFAULT_TOMORROW_CAP = 5
DEFAULT_CALENDAR_TOMORROW_CAP = 5


@dataclass(frozen=True)
class EODSections:
    """Structured per-section content extracted from a snapshot.

    Mirrors :class:`scripts.l8_sod_generator.SODSections` for shape
    consistency.

    Fields
    ------
    today_iso:
        ISO date for the review header. Derived from
        ``snapshot.captured_at``.
    done_today:
        Tasks closed (``status == "done"``) with ``completed_at``
        falling on ``today_iso``.
    carry_over:
        Open tasks (todo / in_progress / blocked) that should have
        moved today but didn't -- includes the operational reason
        (status itself, plus a derived ``reason`` field where
        ``blocked`` carries ``blocked_by`` ids).
    at_risk_goals:
        Open research / projects flagged ``failing`` or
        ``rollback_needed`` -- the L8 PM's "trending behind" surface.
    tomorrow_priority:
        Open tasks (priority + status order) the L8 expects L7 to
        spawn tomorrow morning.
    tomorrow_calendar:
        Tomorrow's calendar events (verbatim from the snapshot's
        upcoming feed).
    trajectory_delta:
        Free-text bullets describing what shifted since the previous
        SOD/EOD reference window.
    previous_eod_date:
        Previous EOD's date, or None on first run.
    completion_counts:
        ``{"done_today": int, "carry_over": int, "blocked": int}``
        for a one-line ecosystem-health snapshot.
    """

    today_iso: str = ""
    done_today: tuple[Mapping[str, Any], ...] = ()
    carry_over: tuple[Mapping[str, Any], ...] = ()
    at_risk_goals: tuple[Mapping[str, Any], ...] = ()
    tomorrow_priority: tuple[Mapping[str, Any], ...] = ()
    tomorrow_calendar: tuple[Mapping[str, Any], ...] = ()
    trajectory_delta: tuple[str, ...] = ()
    previous_eod_date: Optional[str] = None
    completion_counts: Mapping[str, int] = field(default_factory=dict)
    audit_open_flagged: Mapping[str, int] = field(default_factory=dict)
    previous_audit_open_flagged: Mapping[str, int] = field(default_factory=dict)
    audit_delta: Mapping[str, int] = field(default_factory=dict)

    def is_quiet(self) -> bool:
        """True when nothing material happened or is queued.

        A quiet EOD still goes out (daily ritual), but the engine can
        lean on the shorter prompt path.
        """
        return not any((
            self.done_today,
            self.carry_over,
            self.at_risk_goals,
            self.tomorrow_priority,
            self.trajectory_delta,
        ))


# ---------------------------------------------------------------------------
# Section extraction (pure)
# ---------------------------------------------------------------------------

def extract_eod_sections(
    snapshot: StateSnapshot,
    *,
    done_cap: int = DEFAULT_DONE_CAP,
    carry_cap: int = DEFAULT_CARRY_CAP,
    at_risk_cap: int = DEFAULT_AT_RISK_CAP,
    tomorrow_cap: int = DEFAULT_TOMORROW_CAP,
    calendar_cap: int = DEFAULT_CALENDAR_TOMORROW_CAP,
    now: Optional[float] = None,
) -> EODSections:
    """Derive the EOD section payload from a snapshot.

    Pure function. Two calls with the same snapshot return byte-equal
    sections so the downstream prompt is deterministic.
    """
    ref_ts = float(now) if now is not None else float(snapshot.captured_at)
    today_iso = _iso_date(ref_ts)

    done = _select_done_today(snapshot.pd_tasks, today_iso=today_iso)[:done_cap]
    carry = _select_carry_over(snapshot.pd_tasks)[:carry_cap]
    at_risk = _select_at_risk_goals(
        projects=snapshot.pd_projects,
        research=snapshot.pd_research,
    )[:at_risk_cap]
    tomorrow = _select_tomorrow_priority(snapshot.pd_tasks)[:tomorrow_cap]
    cal_tomorrow = tuple(
        _as_event(ev) for ev in _calendar_tomorrow_or_upcoming(snapshot)
    )[:calendar_cap]
    trajectory = _build_trajectory_delta(snapshot)
    counts = _completion_counts(snapshot.pd_tasks, today_iso=today_iso)

    ws = snapshot.workflow_state or {}
    prev_eod = ws.get("last_eod_date")

    audit_current = _normalise_audit_counts(
        getattr(snapshot, "audit_open_flagged", None)
    )
    audit_previous = _normalise_audit_counts(
        ws.get("last_audit_open_flagged")
    )
    audit_delta = _compute_audit_delta(audit_current, audit_previous)

    return EODSections(
        today_iso=today_iso,
        done_today=done,
        carry_over=carry,
        at_risk_goals=at_risk,
        tomorrow_priority=tomorrow,
        tomorrow_calendar=cal_tomorrow,
        trajectory_delta=trajectory,
        previous_eod_date=prev_eod if isinstance(prev_eod, str) else None,
        completion_counts=counts,
        audit_open_flagged=audit_current,
        previous_audit_open_flagged=audit_previous,
        audit_delta=audit_delta,
    )


def _calendar_tomorrow_or_upcoming(snap: StateSnapshot
                                     ) -> Sequence[Mapping[str, Any]]:
    """Prefer an explicit ``calendar_tomorrow`` if the richer snapshot
    carries one; fall back to the full ``calendar_events`` list."""
    tomorrow = getattr(snap, "calendar_tomorrow", None)
    if tomorrow:
        return tuple(tomorrow)
    return tuple(getattr(snap, "calendar_upcoming", None)
                  or snap.calendar_events)


def _select_done_today(tasks: Sequence[Mapping[str, Any]], *,
                        today_iso: str) -> tuple[Mapping[str, Any], ...]:
    """Tasks with status=done AND completed_at on today_iso."""
    out: list[tuple[tuple[int, str], Mapping[str, Any]]] = []
    for task in tasks:
        if str(task.get("status") or "").lower() != "done":
            continue
        completed = task.get("completed_at") or task.get("done_at")
        if not isinstance(completed, str):
            continue
        # ISO datetime starts with YYYY-MM-DD; compare prefix.
        if not completed.startswith(today_iso):
            continue
        out.append((_priority_sort_key(task), dict(task)))
    out.sort(key=lambda pair: pair[0])
    return tuple(task for _, task in out)


def _select_carry_over(tasks: Sequence[Mapping[str, Any]]
                        ) -> tuple[Mapping[str, Any], ...]:
    """Open tasks (todo / in_progress / blocked) flagged for carry-over.

    Each entry gets a derived ``reason`` field so the EOD prose can
    say WHY it didn't move:
      * status=blocked       -> "blocked: <blocked_by>"
      * status=in_progress   -> "in_progress"
      * status=todo          -> "not picked up"
    """
    out: list[tuple[tuple[int, str], Mapping[str, Any]]] = []
    for task in tasks:
        status = str(task.get("status") or "").lower()
        if status not in {"todo", "in_progress", "blocked"}:
            continue
        entry = dict(task)
        if status == "blocked":
            blocked_by = task.get("blocked_by") or []
            if blocked_by:
                entry["reason"] = f"blocked: {', '.join(str(b) for b in blocked_by)}"
            else:
                entry["reason"] = "blocked"
        elif status == "in_progress":
            entry["reason"] = "in_progress"
        else:
            entry["reason"] = "not picked up"
        out.append((_priority_sort_key(task), entry))
    out.sort(key=lambda pair: pair[0])
    return tuple(task for _, task in out)


def _select_at_risk_goals(*,
                            projects: Mapping[str, Mapping[str, Any]],
                            research: Sequence[Mapping[str, Any]],
                            ) -> tuple[Mapping[str, Any], ...]:
    """Projects flagged failing/rollback + research with at_risk=True.
    Returns a unified list with a ``source`` tag."""
    out: list[Mapping[str, Any]] = []
    for pid, p in projects.items():
        flags = p or {}
        if flags.get("failing") or flags.get("rollback_needed"):
            out.append({
                "id": pid,
                "title": flags.get("name") or pid,
                "source": "project",
                "reason": "failing" if flags.get("failing") else "rollback_needed",
                "priority": "high",
            })
    for r in research:
        if (r or {}).get("at_risk"):
            entry = dict(r)
            entry.setdefault("source", "research")
            entry.setdefault("reason", "at_risk")
            out.append(entry)
    out.sort(key=_priority_sort_key)
    return tuple(out)


def _select_tomorrow_priority(tasks: Sequence[Mapping[str, Any]]
                                ) -> tuple[Mapping[str, Any], ...]:
    """Top open tasks L8 expects L7 to spawn tomorrow morning."""
    out: list[tuple[tuple[int, str], Mapping[str, Any]]] = []
    for task in tasks:
        status = str(task.get("status") or "").lower()
        if status not in {"todo", "in_progress"}:
            continue
        out.append((_priority_sort_key(task), dict(task)))
    out.sort(key=lambda pair: pair[0])
    return tuple(task for _, task in out)


def _build_trajectory_delta(snapshot: StateSnapshot) -> tuple[str, ...]:
    """Bullets describing material shifts during the day.

    Same source as the SOD trajectory delta -- the loop runtime
    accumulates these into workflow_state between SOD/EOD ticks.
    """
    ws = snapshot.workflow_state or {}
    raw = ws.get("trajectory_delta") or ws.get("trajectory_deltas") or ()
    if isinstance(raw, str):
        raw = [line for line in raw.split("\n") if line.strip()]
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in raw if str(item).strip())


def _completion_counts(tasks: Sequence[Mapping[str, Any]],
                        *, today_iso: str) -> dict[str, int]:
    counts = {"done_today": 0, "carry_over": 0, "blocked": 0}
    for task in tasks:
        status = str(task.get("status") or "").lower()
        completed = task.get("completed_at") or task.get("done_at")
        if (status == "done" and isinstance(completed, str)
                and completed.startswith(today_iso)):
            counts["done_today"] += 1
        elif status in {"todo", "in_progress", "blocked"}:
            counts["carry_over"] += 1
            if status == "blocked":
                counts["blocked"] += 1
    return counts


def _normalise_audit_counts(value: Any) -> dict[str, int]:
    """Coerce the audit per-project payload into ``{project_id: int}``.

    Accepts ``None``, a mapping ``{pid: int}``, or a mapping where the
    value is itself a dict with an ``open_flagged`` int (the shape the
    state reader might pre-roll). Anything else returns ``{}`` so the
    audit_delta line stays well-defined even with sad-path inputs.
    """
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, int] = {}
    for pid, raw in value.items():
        if pid is None:
            continue
        if isinstance(raw, bool):  # bool is an int subclass; reject explicitly.
            continue
        if isinstance(raw, int):
            out[str(pid)] = int(raw)
        elif isinstance(raw, Mapping):
            inner = raw.get("open_flagged")
            if isinstance(inner, int) and not isinstance(inner, bool):
                out[str(pid)] = int(inner)
    return out


def _compute_audit_delta(
    current: Mapping[str, int],
    previous: Mapping[str, int],
) -> dict[str, int]:
    """Per-project signed delta ``current - previous``, non-zero only.

    Keys are sorted to keep the rendered prompt deterministic so two
    EOD runs against the same snapshot produce byte-identical bytes.
    """
    keys = sorted(set(current) | set(previous))
    deltas: dict[str, int] = {}
    for k in keys:
        d = int(current.get(k, 0)) - int(previous.get(k, 0))
        if d != 0:
            deltas[k] = d
    return deltas


def _format_audit_delta_line(deltas: Mapping[str, int]) -> str:
    """Return the single ``audit_delta: ...`` line for the EOD prompt.

    Format mirrors the AC-L8DOC1 proposal: comma-separated
    ``<project> <+/-N>`` pairs when there is movement, or the literal
    sentinel ``audit_delta: stable`` when everything is flat.
    """
    if not deltas:
        return "audit_delta: stable"
    parts = [
        f"{pid} {delta:+d}" for pid, delta in deltas.items()
    ]
    return "audit_delta: " + ", ".join(parts)


def _priority_sort_key(item: Mapping[str, Any]) -> tuple[int, str]:
    priority = str(item.get("priority") or "").lower()
    rank = PRIORITY_RANK.get(priority, _UNKNOWN_PRIORITY_RANK)
    pid = str(item.get("id") or item.get("title") or "")
    return (rank, pid)


def _as_event(event: Mapping[str, Any]) -> Mapping[str, Any]:
    return dict(event)


# ---------------------------------------------------------------------------
# Prompt rendering (EODGenerator protocol implementation)
# ---------------------------------------------------------------------------

_PROMPT_PREFIX = (
    "You are L8 -- the PM oversight layer of the agent-controller "
    "hierarchy. Generate today's END-OF-DAY REVIEW for Preston.\n"
    "\n"
    "Tone: PM closing the day for the operator. Short sentences. Items, "
    "not paragraphs. Honest about what didn't move (no spin).\n"
    "\n"
    "Output sections, in this exact order. Use the section headings "
    "verbatim. Skip a section ONLY if the data block below it is "
    "literally empty -- never invent items, never duplicate items "
    "across sections, never drop a section heading the data supports.\n"
    "\n"
    "  1. Headline      -- one sentence: state of the day.\n"
    "  2. Done today    -- bullets, one per completed task.\n"
    "  3. Carry over    -- open work with its REASON (blocked, "
    "in_progress, not picked up).\n"
    "  4. At-risk goals -- projects/research trending behind.\n"
    "  5. Tomorrow      -- what L7 Dispatch should spawn first.\n"
    "  6. Calendar tomorrow -- events on tomorrow's calendar (skip if "
    "none).\n"
    "  7. Trajectory delta -- what shifted today (skip if none).\n"
    "\n"
    "End with a single line: 'Decision needed:' OR 'No action needed.'"
)


class L8EODGenerator:
    """Concrete EODGenerator for the L8 PM.

    Construction takes the per-section caps so callers can tighten
    the budget when the downstream surface is space-constrained.
    """

    PROMPT_PREFIX = _PROMPT_PREFIX

    def __init__(
        self,
        *,
        done_cap: int = DEFAULT_DONE_CAP,
        carry_cap: int = DEFAULT_CARRY_CAP,
        at_risk_cap: int = DEFAULT_AT_RISK_CAP,
        tomorrow_cap: int = DEFAULT_TOMORROW_CAP,
        calendar_cap: int = DEFAULT_CALENDAR_TOMORROW_CAP,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        self._done_cap = done_cap
        self._carry_cap = carry_cap
        self._at_risk_cap = at_risk_cap
        self._tomorrow_cap = tomorrow_cap
        self._calendar_cap = calendar_cap
        self._now = now

    # ------------------------------------------------------------------
    # EODGenerator protocol
    # ------------------------------------------------------------------

    def render(self, snapshot: StateSnapshot) -> str:
        sections = self.extract(snapshot)
        return self._format_prompt(sections)

    # ------------------------------------------------------------------
    # Reusable helpers
    # ------------------------------------------------------------------

    def extract(self, snapshot: StateSnapshot) -> EODSections:
        now = self._now() if callable(self._now) else None
        return extract_eod_sections(
            snapshot,
            done_cap=self._done_cap,
            carry_cap=self._carry_cap,
            at_risk_cap=self._at_risk_cap,
            tomorrow_cap=self._tomorrow_cap,
            calendar_cap=self._calendar_cap,
            now=now,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _format_prompt(self, sections: EODSections) -> str:
        blocks: list[str] = [self.PROMPT_PREFIX, ""]
        blocks.append(f"=== TODAY ===\n{sections.today_iso}")
        if sections.previous_eod_date:
            blocks.append(
                f"=== PREVIOUS EOD ===\n{sections.previous_eod_date}"
            )
        blocks.append(
            "=== COMPLETION COUNTS ===\n"
            + json.dumps(dict(sections.completion_counts), sort_keys=True)
        )
        # AC-S16d-AUDITLINE: single relative-change line summarising
        # per-project (open+flagged) audit movement vs the previous EOD.
        blocks.append(
            "=== AUDIT DELTA ===\n"
            + _format_audit_delta_line(sections.audit_delta)
        )
        blocks.append(_format_block(
            "DONE TODAY", _format_task_lines(sections.done_today),
        ))
        blocks.append(_format_block(
            "CARRY OVER", _format_carry_lines(sections.carry_over),
        ))
        blocks.append(_format_block(
            "AT-RISK GOALS", _format_at_risk_lines(sections.at_risk_goals),
        ))
        blocks.append(_format_block(
            "TOMORROW PRIORITY", _format_task_lines(sections.tomorrow_priority),
        ))
        blocks.append(_format_block(
            "CALENDAR TOMORROW", _format_calendar_lines(sections.tomorrow_calendar),
        ))
        blocks.append(_format_block(
            "TRAJECTORY DELTA",
            tuple(sections.trajectory_delta) or ("(none)",),
        ))
        return "\n\n".join(blocks).rstrip() + "\n"


def _format_block(heading: str, lines: Sequence[str]) -> str:
    body = "\n".join(lines) if lines else "(none)"
    return f"=== {heading} ===\n{body}"


def _format_task_lines(tasks: Sequence[Mapping[str, Any]]
                        ) -> tuple[str, ...]:
    if not tasks:
        return ("(none)",)
    return tuple(_format_task_line(task) for task in tasks)


def _format_task_line(task: Mapping[str, Any]) -> str:
    tid = task.get("id") or "?"
    title = (task.get("title") or "").strip() or "(untitled)"
    project = task.get("project") or task.get("project_id") or "?"
    priority = task.get("priority") or "?"
    status = task.get("status") or "?"
    parts = [f"- [{tid}]", f"({project})", f"[{priority}/{status}]", title]
    return " ".join(parts)


def _format_carry_lines(tasks: Sequence[Mapping[str, Any]]
                         ) -> tuple[str, ...]:
    if not tasks:
        return ("(none)",)
    out: list[str] = []
    for task in tasks:
        tid = task.get("id") or "?"
        title = (task.get("title") or "").strip() or "(untitled)"
        project = task.get("project") or task.get("project_id") or "?"
        reason = task.get("reason") or task.get("status") or "?"
        out.append(f"- [{tid}] ({project}) [{reason}] {title}")
    return tuple(out)


def _format_at_risk_lines(items: Sequence[Mapping[str, Any]]
                            ) -> tuple[str, ...]:
    if not items:
        return ("(none)",)
    out: list[str] = []
    for item in items:
        iid = item.get("id") or "?"
        title = (item.get("title") or "").strip() or "(untitled)"
        source = item.get("source") or "?"
        reason = item.get("reason") or "?"
        out.append(f"- [{source}:{iid}] [{reason}] {title}")
    return tuple(out)


def _format_calendar_lines(events: Sequence[Mapping[str, Any]]
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

_COLOR_GREEN = 0x57F287
_COLOR_AMBER = 0xFEE75C
_COLOR_RED = 0xED4245


def build_discord_embed(
    body: str,
    sections: EODSections,
    *,
    field_value_cap: int = 1000,
) -> dict[str, Any]:
    """Return the Discord embed payload for the EOD post."""
    color = _select_embed_color(sections)
    title = (f"EOD Review -- {sections.today_iso}"
             if sections.today_iso else "EOD Review")
    description = _truncate(body.strip(), 2000)

    fields: list[dict[str, Any]] = []
    if sections.done_today:
        fields.append({
            "name": f"Done today ({len(sections.done_today)})",
            "value": _truncate(
                "\n".join(_format_task_lines(sections.done_today)),
                field_value_cap,
            ),
            "inline": False,
        })
    if sections.carry_over:
        fields.append({
            "name": f"Carry over ({len(sections.carry_over)})",
            "value": _truncate(
                "\n".join(_format_carry_lines(sections.carry_over)),
                field_value_cap,
            ),
            "inline": False,
        })
    if sections.at_risk_goals:
        fields.append({
            "name": f"At-risk goals ({len(sections.at_risk_goals)})",
            "value": _truncate(
                "\n".join(_format_at_risk_lines(sections.at_risk_goals)),
                field_value_cap,
            ),
            "inline": False,
        })
    if sections.tomorrow_priority:
        fields.append({
            "name": f"Tomorrow ({len(sections.tomorrow_priority)})",
            "value": _truncate(
                "\n".join(_format_task_lines(sections.tomorrow_priority)),
                field_value_cap,
            ),
            "inline": False,
        })
    if sections.tomorrow_calendar:
        fields.append({
            "name": f"Calendar tomorrow ({len(sections.tomorrow_calendar)})",
            "value": _truncate(
                "\n".join(_format_calendar_lines(sections.tomorrow_calendar)),
                field_value_cap,
            ),
            "inline": False,
        })
    if sections.trajectory_delta:
        fields.append({
            "name": "Trajectory delta",
            "value": _truncate(
                "\n".join(f"- {line}" for line in sections.trajectory_delta),
                field_value_cap,
            ),
            "inline": False,
        })

    return {
        "title": title,
        "description": description or "(no review body)",
        "color": color,
        "fields": fields,
    }


def build_dream_markdown(body: str, sections: EODSections) -> str:
    """Return a Dream-UI-friendly markdown rendering of the EOD review."""
    header = (f"# EOD Review -- {sections.today_iso}"
              if sections.today_iso else "# EOD Review")
    parts: list[str] = [header, "", body.strip(), ""]

    def _section(heading: str, lines: Sequence[str]) -> None:
        if not lines:
            return
        parts.append(f"## {heading}")
        for line in lines:
            parts.append(line if line.startswith("-") else f"- {line}")
        parts.append("")

    _section(
        f"Done today ({len(sections.done_today)})",
        _format_task_lines(sections.done_today) if sections.done_today else (),
    )
    _section(
        f"Carry over ({len(sections.carry_over)})",
        _format_carry_lines(sections.carry_over) if sections.carry_over else (),
    )
    _section(
        f"At-risk goals ({len(sections.at_risk_goals)})",
        _format_at_risk_lines(sections.at_risk_goals)
        if sections.at_risk_goals else (),
    )
    _section(
        f"Tomorrow ({len(sections.tomorrow_priority)})",
        _format_task_lines(sections.tomorrow_priority)
        if sections.tomorrow_priority else (),
    )
    if sections.tomorrow_calendar:
        parts.append("## Calendar tomorrow")
        for line in _format_calendar_lines(sections.tomorrow_calendar):
            parts.append(line)
        parts.append("")
    _section(
        "Trajectory delta",
        tuple(f"- {line}" for line in sections.trajectory_delta),
    )
    return "\n".join(parts).rstrip() + "\n"


def _select_embed_color(sections: EODSections) -> int:
    if sections.at_risk_goals:
        return _COLOR_RED
    if sections.carry_over or sections.trajectory_delta:
        return _COLOR_AMBER
    return _COLOR_GREEN


def _truncate(text: str, cap: int) -> str:
    if cap <= 0 or len(text) <= cap:
        return text
    return text[: max(0, cap - 3)] + "..."


# ---------------------------------------------------------------------------
# Workflow-state helpers (write side -- updates last_eod_date)
# ---------------------------------------------------------------------------

def mark_eod_complete(
    workflow_state_path: Path | str,
    *,
    today_iso: Optional[str] = None,
    now: Optional[Callable[[], float]] = None,
    audit_open_flagged: Optional[Mapping[str, int]] = None,
) -> dict[str, Any]:
    """Update ``automation-registry/data/workflow_state.json`` after EOD.

    Mirrors :func:`scripts.l8_sod_generator.mark_sod_complete`: reads,
    sets ``last_eod_date``, writes atomically (tmp + replace).

    When ``audit_open_flagged`` is provided, the per-project
    (open + flagged) counts captured at this EOD are persisted under
    ``last_audit_open_flagged`` so the next EOD can render an
    ``audit_delta`` line relative to today (AC-S16d-AUDITLINE).
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
        existing = {}
    existing["last_eod_date"] = today_iso
    if audit_open_flagged is not None:
        existing["last_audit_open_flagged"] = dict(
            _normalise_audit_counts(audit_open_flagged)
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2, sort_keys=True),
                    encoding="utf-8")
    tmp.replace(path)
    return existing


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def _iso_date(ts: float) -> str:
    if not ts:
        ts = time.time()
    return time.strftime("%Y-%m-%d", time.localtime(ts))


__all__ = [
    "DEFAULT_AT_RISK_CAP",
    "DEFAULT_CALENDAR_TOMORROW_CAP",
    "DEFAULT_CARRY_CAP",
    "DEFAULT_DONE_CAP",
    "DEFAULT_TOMORROW_CAP",
    "EODSections",
    "L8EODGenerator",
    "PRIORITY_RANK",
    "build_discord_embed",
    "build_dream_markdown",
    "extract_eod_sections",
    "mark_eod_complete",
]
