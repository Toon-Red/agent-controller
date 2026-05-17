"""L8 Pro/Max quota timer reader via Chrome MCP (AC-L8QUOTA1).

Cross-ref: AR-S3k (`59d69499`) was blocked because the Pro/Max
subscription's two billing timers -- the 5h rolling *session* window
and the absolute *weekly* reset -- are UI-only. The Anthropic API
exposes ``anthropic-ratelimit-*-reset`` headers but those reflect
ORG-LEVEL minute-scale enforcement, not the Pro/Max subscription
windows that actually bind Preston's usage. See
``automation-registry/docs/proposals/AR-S3-Q-MAIN-headers-findings.md``
lines 107-174 for the three-option call (operator-anchored timer /
DOM scrape / empirical test).

Preston's decision scoped this task to *option (b)* and assigned it
to L8 as a recurring read:

  1. L8 drives the Chrome MCP (``mcp__claude-in-chrome__*``) to the
     Claude Desktop Usage page once per iteration.
  2. The page's two visible timer strings -- ``Resets in X hr Y min``
     (session) and ``Resets <weekday> <H:MM AM/PM>`` (weekly) -- are
     extracted and parsed.
  3. The parsed absolute timestamps are written to
     ``data/quota_state.json`` in the format AR-S3k's smart-resume
     scheduler already consumes.
  4. AR-S3k (and any other quota-aware consumer) reads from that
     JSON instead of probing the API.

Design notes:

* **Brittleness is acknowledged.** DOM scraping is the last-resort
  option from the Q-MAIN findings; this module is a known surface
  for re-verification on every Claude Desktop update. The DOM
  contract is isolated behind the :class:`ChromeUsageScraper`
  Protocol so a UI refactor only touches the adapter, not the
  parser / writer / sanity-checker.
* **Pure parsing.** :func:`parse_session_timer` and
  :func:`parse_weekly_timer` are side-effect-free; they take the raw
  UI text + a ``now`` and return ``datetime`` results. Tests pin
  every wording variant the Usage page is known to emit.
* **Sanity-checked output.** A parsed value that fails the
  ``session within 5h, weekly within 7d`` check is rejected with
  :class:`QuotaSanityError`; the reader either retries (caller's
  decision) or leaves the existing ``data/quota_state.json``
  untouched. This stops a DOM drift from silently corrupting the
  resume scheduler.
* **Schema-compatible.** :class:`QuotaSnapshot.to_quota_state_dict`
  emits the same keys as
  ``automation-registry/quota_probe.py::QuotaState`` -- plus the new
  ``session_reset_at`` / ``weekly_reset_at`` fields the two-timer
  model requires. Existing AR-S3k consumers see a familiar shape
  with ``source="scraped"``.
* **Injectable everything.** Chrome client, clock, timezone, and
  output path are all constructor knobs. Tests never touch the real
  Chrome MCP transport.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Where the Chrome MCP is pointed for the per-iteration read. Kept as a
#: module constant (not a default arg) so a debugger / test can monkey-patch
#: it without going through the constructor.
USAGE_PAGE_URL = "https://claude.ai/settings/usage"

#: Default output path -- ``<repo-root>/data/quota_state.json``. The
#: smart-resume scheduler in ``automation-registry`` reads its own copy
#: from its own ``data/`` dir; the L8 reader writes the L8-canonical
#: copy and the operator wires the two together (symlink, copy-on-write
#: in the loop runtime, or a configured path override).
DEFAULT_QUOTA_STATE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "quota_state.json"
)

#: Sanity bounds for the two timers. Slight slack is added on top of
#: the documented Pro/Max windows so a UI that rounds *up* (e.g.
#: displays "4 hr 59 min" as "5 hr") doesn't trip the check.
SESSION_WINDOW_MAX = timedelta(hours=5, minutes=10)
WEEKLY_WINDOW_MAX = timedelta(days=7, hours=1)

#: Source tag persisted into the QuotaState file. Matches the (b)
#: source enum from ``quota_probe.py`` so AR-S3k's existing logging
#: doesn't change.
SOURCE_SCRAPED = "scraped"


class QuotaParseError(ValueError):
    """The Usage page returned text that doesn't match the documented
    timer wording. Most often a DOM drift after a Claude Desktop UI
    update. Caller should keep the previous ``quota_state.json`` and
    surface the failure to the operator."""


class QuotaSanityError(ValueError):
    """The parsed values are syntactically OK but lie outside the
    documented Pro/Max windows (session > 5h, weekly > 7d, or either
    in the past). Almost certainly a parser bug or a stale snapshot
    -- never write this to ``quota_state.json``."""


# ---------------------------------------------------------------------------
# Chrome scraper Protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsagePageContent:
    """The two raw timer strings ripped from the Usage page DOM.

    Field naming mirrors the Q-MAIN findings doc so a reader can map
    each field to its UI element without consulting two files.
    """
    session_timer_text: str   # e.g. "Resets in 3 hr 21 min"
    weekly_timer_text: str    # e.g. "Resets Tue 9:00 PM"


@runtime_checkable
class ChromeUsageScraper(Protocol):
    """Adapter contract for the Chrome MCP-driven Usage page read.

    The reader needs ONE method: navigate to the Usage page (if not
    already there) and return the two timer strings. Production
    wires a :class:`ChromeMCPUsageScraper` which composes the
    relevant ``mcp__claude-in-chrome__*`` tool calls; tests pass an
    in-memory fake.

    Implementations MAY cache (the L8 loop only calls this once per
    iteration) but MUST NOT retry-internally -- the reader's sanity
    check is the canonical retry policy.
    """

    def fetch_usage_content(self) -> UsagePageContent: ...


# ---------------------------------------------------------------------------
# Chrome MCP client wiring (production adapter)
# ---------------------------------------------------------------------------


@runtime_checkable
class ChromeMCPClient(Protocol):
    """Minimal Chrome MCP transport contract.

    Same shape as :class:`scripts.l8_state_reader.MCPClient` -- one
    ``call_tool(name, **params)`` entry point. The names live as
    constants below so a typo / upstream rename is greppable.
    """
    def call_tool(self, name: str, /, **params: Any) -> Any: ...


# Tool names for the ``mcp__claude-in-chrome__*`` server. Pinned as
# constants so the adapter is greppable when Anthropic ships a Chrome
# MCP rename.
CHROME_TOOL_NAVIGATE = "mcp__claude-in-chrome__navigate"
CHROME_TOOL_SNAPSHOT = "mcp__claude-in-chrome__snapshot"
CHROME_TOOL_EVALUATE = "mcp__claude-in-chrome__evaluate"


# Regexes used by the adapter to find the timer strings in the
# page text. Kept here next to the tool names so a DOM drift can be
# patched in one place. The session string is "Resets in X hr Y min"
# (one or both numbers may be present); the weekly string is
# "Resets <weekday> <H:MM AM/PM>".
_SESSION_TEXT_RE = re.compile(
    r"Resets\s+in\s+(?:\d+\s*hr)?(?:\s+\d+\s*min)?",
    flags=re.IGNORECASE,
)
_WEEKLY_TEXT_RE = re.compile(
    r"Resets\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*"
    r"\s+\d{1,2}:\d{2}\s*(?:AM|PM)",
    flags=re.IGNORECASE,
)


class ChromeMCPUsageScraper:
    """Production :class:`ChromeUsageScraper` driving the Chrome MCP.

    The sequence is:

      1. ``navigate(url=USAGE_PAGE_URL)``
      2. ``snapshot()`` (returns the page-text snapshot)
      3. Regex the two timer strings out of the snapshot text.

    DOM drift surfaces as :class:`QuotaParseError` -- caller decides
    whether to retry or fall back to the previous snapshot.
    """

    def __init__(
        self,
        *,
        client: ChromeMCPClient,
        url: str = USAGE_PAGE_URL,
    ) -> None:
        self._client = client
        self._url = url

    def fetch_usage_content(self) -> UsagePageContent:
        self._client.call_tool(CHROME_TOOL_NAVIGATE, url=self._url)
        snapshot = self._client.call_tool(CHROME_TOOL_SNAPSHOT)
        text = _coerce_snapshot_text(snapshot)
        session_match = _SESSION_TEXT_RE.search(text)
        weekly_match = _WEEKLY_TEXT_RE.search(text)
        if not session_match or not weekly_match:
            raise QuotaParseError(
                "Usage page snapshot did not contain both timer strings; "
                "DOM drift suspected. Re-verify against the live page."
            )
        return UsagePageContent(
            session_timer_text=session_match.group(0),
            weekly_timer_text=weekly_match.group(0),
        )


def _coerce_snapshot_text(snapshot: Any) -> str:
    """Best-effort text extraction from a Chrome MCP snapshot payload.

    The Chrome MCP server returns a dict-shaped accessibility tree
    in steady state. We don't depend on the exact shape -- if a key
    named ``text`` / ``content`` / ``snapshot`` is present at the
    top level we use it; otherwise we ``json.dumps`` the whole thing.
    The regexes operate on the resulting flat string so even a JSON
    serialization of the a11y tree still finds the timer strings.
    """
    if snapshot is None:
        return ""
    if isinstance(snapshot, str):
        return snapshot
    if isinstance(snapshot, Mapping):
        for key in ("text", "content", "snapshot", "body"):
            value = snapshot.get(key)
            if isinstance(value, str):
                return value
        return json.dumps(snapshot, default=str)
    return json.dumps(snapshot, default=str)


# ---------------------------------------------------------------------------
# Pure parsing
# ---------------------------------------------------------------------------


_SESSION_PARSE_RE = re.compile(
    r"Resets\s+in"
    r"(?:\s+(?P<hours>\d+)\s*hr)?"
    r"(?:\s+(?P<mins>\d+)\s*min)?",
    flags=re.IGNORECASE,
)

_WEEKDAY_TO_INDEX = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3,
    "fri": 4, "sat": 5, "sun": 6,
}

_WEEKLY_PARSE_RE = re.compile(
    r"Resets\s+"
    r"(?P<weekday>Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*"
    r"\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>AM|PM)",
    flags=re.IGNORECASE,
)


def parse_session_timer(text: str, *, now: datetime) -> datetime:
    """Parse a "Resets in X hr Y min" string into an absolute timestamp.

    Either "X hr" or "Y min" may be omitted -- the UI drops zero-valued
    components (e.g. "Resets in 4 hr" or "Resets in 47 min"). Both
    omitted is a parse error.

    Parameters
    ----------
    text:
        The raw string scraped from the Usage page.
    now:
        Wall-clock anchor. Must be timezone-aware -- the function
        refuses naive datetimes to keep the call site honest.

    Returns
    -------
    The absolute timestamp at which the session resets.

    Raises
    ------
    QuotaParseError:
        Text doesn't match the documented "Resets in ..." wording or
        carries neither hours nor minutes.
    ValueError:
        ``now`` is timezone-naive.
    """
    _ensure_tz_aware(now)
    match = _SESSION_PARSE_RE.search(text)
    if not match:
        raise QuotaParseError(
            f"session timer text did not match 'Resets in X hr Y min': {text!r}"
        )
    hours = int(match.group("hours") or 0)
    mins = int(match.group("mins") or 0)
    if hours == 0 and mins == 0:
        raise QuotaParseError(
            f"session timer parsed to zero duration (text={text!r})"
        )
    return now + timedelta(hours=hours, minutes=mins)


def parse_weekly_timer(
    text: str, *, now: datetime, tz: Optional[Any] = None,
) -> datetime:
    """Parse a "Resets <weekday> <H:MM AM/PM>" string.

    The Usage page renders the weekly reset in the user's local
    timezone. The function honours ``now``'s timezone by default
    (``tz=None``); pass an explicit ``tz`` (e.g. ``ZoneInfo("America/Los_Angeles")``)
    if the local DOM is being rendered with a different offset.

    The reset is interpreted as "the next occurrence of this weekday
    at this local time, ahead of ``now``". If today is the target
    weekday but the wall-clock time has already passed, we advance
    by 7 days (the Usage page rolls to next week the moment the
    reset fires).

    Raises
    ------
    QuotaParseError:
        Text doesn't match the documented wording.
    ValueError:
        ``now`` is timezone-naive.
    """
    _ensure_tz_aware(now)
    match = _WEEKLY_PARSE_RE.search(text)
    if not match:
        raise QuotaParseError(
            f"weekly timer text did not match 'Resets <weekday> <H:MM AM/PM>': {text!r}"
        )
    weekday_idx = _WEEKDAY_TO_INDEX[match.group("weekday").lower()[:3]]
    hour_12 = int(match.group("hour"))
    minute = int(match.group("minute"))
    ampm = match.group("ampm").upper()

    if not (1 <= hour_12 <= 12):
        raise QuotaParseError(
            f"weekly timer hour out of range: {match.group(0)!r}"
        )
    if not (0 <= minute <= 59):
        raise QuotaParseError(
            f"weekly timer minute out of range: {match.group(0)!r}"
        )
    # 12 AM -> 0, 12 PM -> 12, 1..11 PM -> 13..23.
    hour_24 = hour_12 % 12
    if ampm == "PM":
        hour_24 += 12

    anchor_tz = tz if tz is not None else now.tzinfo
    local_now = now.astimezone(anchor_tz)
    days_ahead = (weekday_idx - local_now.weekday()) % 7
    candidate = local_now.replace(
        hour=hour_24, minute=minute, second=0, microsecond=0,
    ) + timedelta(days=days_ahead)
    if candidate <= local_now:
        candidate += timedelta(days=7)
    # Return in the same tz as ``now`` so the caller's downstream
    # arithmetic stays in one frame.
    return candidate.astimezone(now.tzinfo)


def _ensure_tz_aware(dt: datetime) -> None:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(
            "now must be timezone-aware (use datetime.now(timezone.utc))"
        )


# ---------------------------------------------------------------------------
# Snapshot dataclass + sanity check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuotaSnapshot:
    """The two parsed timer values + provenance.

    ``next_reset_at`` is the EARLIER of the two timestamps -- AR-S3k's
    smart-resume scheduler fires off that field, so we pre-compute it
    here instead of relying on every consumer to do the comparison.
    """
    session_reset_at: datetime
    weekly_reset_at: datetime
    checked_at: datetime
    raw_session_text: str
    raw_weekly_text: str
    notes: tuple[str, ...] = ()

    @property
    def next_reset_at(self) -> datetime:
        return min(self.session_reset_at, self.weekly_reset_at)

    def to_quota_state_dict(self) -> dict[str, Any]:
        """Render to the on-disk JSON shape consumed by AR-S3k.

        The base keys mirror ``quota_probe.QuotaState`` so the
        smart-resume scheduler doesn't need a schema branch for the
        scraped vs. probed cases. The new ``session_reset_at`` /
        ``weekly_reset_at`` / ``raw_*`` fields are additive -- old
        consumers ignore them.
        """
        def _iso(dt: datetime) -> str:
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds")

        return {
            # AR-S3k canonical fields ---------------------------------
            "next_reset_ts": _iso(self.next_reset_at),
            "source": SOURCE_SCRAPED,
            "tokens_remaining": None,
            "requests_remaining": None,
            "api_tokens_reset_ts": None,
            "api_requests_reset_ts": None,
            "operator_reset_ts": None,
            "checked_at": _iso(self.checked_at),
            "notes": list(self.notes),
            # Two-timer-model additions -------------------------------
            "session_reset_at": _iso(self.session_reset_at),
            "weekly_reset_at": _iso(self.weekly_reset_at),
            "raw_session_text": self.raw_session_text,
            "raw_weekly_text": self.raw_weekly_text,
        }


def sanity_check(snapshot: QuotaSnapshot, *, now: datetime) -> None:
    """Reject a snapshot whose timers fall outside the documented Pro/Max
    windows.

    The session window is rolling-5h, anchored on first use; the
    weekly window is an absolute 7-day grant. We allow a small slack
    (:data:`SESSION_WINDOW_MAX` / :data:`WEEKLY_WINDOW_MAX`) on top
    of the nominal max to absorb UI rounding.

    Raises
    ------
    QuotaSanityError:
        Either timer lies in the past or beyond its documented window.
    """
    _ensure_tz_aware(now)
    if snapshot.session_reset_at <= now:
        raise QuotaSanityError(
            f"session_reset_at is in the past: {snapshot.session_reset_at!s} <= {now!s}"
        )
    if snapshot.weekly_reset_at <= now:
        raise QuotaSanityError(
            f"weekly_reset_at is in the past: {snapshot.weekly_reset_at!s} <= {now!s}"
        )
    if snapshot.session_reset_at - now > SESSION_WINDOW_MAX:
        raise QuotaSanityError(
            f"session_reset_at exceeds 5h window: "
            f"{snapshot.session_reset_at!s} > {now!s} + {SESSION_WINDOW_MAX}"
        )
    if snapshot.weekly_reset_at - now > WEEKLY_WINDOW_MAX:
        raise QuotaSanityError(
            f"weekly_reset_at exceeds 7d window: "
            f"{snapshot.weekly_reset_at!s} > {now!s} + {WEEKLY_WINDOW_MAX}"
        )


# ---------------------------------------------------------------------------
# Reader (orchestration)
# ---------------------------------------------------------------------------


class L8QuotaReader:
    """L8's per-iteration Pro/Max quota timer reader.

    Composes:

      * a :class:`ChromeUsageScraper` (production: :class:`ChromeMCPUsageScraper`)
      * the pure parsers above
      * :func:`sanity_check`
      * persistence to ``data/quota_state.json``

    Typical wiring from the L8 PM:

        reader = L8QuotaReader(
            scraper=ChromeMCPUsageScraper(client=chrome_mcp),
            output_path=DEFAULT_QUOTA_STATE_PATH,
        )
        snapshot = reader.read_and_persist()  # once per iteration

    The reader does not retry. If the scraper raises
    :class:`QuotaParseError` or the snapshot fails
    :func:`sanity_check`, the caller (the L8 loop runtime) is
    responsible for deciding whether to leave the previous
    ``quota_state.json`` in place or re-attempt.
    """

    def __init__(
        self,
        *,
        scraper: ChromeUsageScraper,
        output_path: Path | str = DEFAULT_QUOTA_STATE_PATH,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        tz: Optional[Any] = None,
    ) -> None:
        self._scraper = scraper
        self._output_path = Path(output_path)
        self._clock = clock
        self._tz = tz

    # ------------------------------------------------------------------
    # Public entrypoints
    # ------------------------------------------------------------------

    def read(self) -> QuotaSnapshot:
        """Scrape + parse + sanity-check. Does NOT write to disk.

        Useful for tests and for callers that want to compare against
        the previous on-disk snapshot before persisting.
        """
        now = self._clock()
        _ensure_tz_aware(now)
        page = self._scraper.fetch_usage_content()
        session_at = parse_session_timer(page.session_timer_text, now=now)
        weekly_at = parse_weekly_timer(
            page.weekly_timer_text, now=now, tz=self._tz,
        )
        snapshot = QuotaSnapshot(
            session_reset_at=session_at,
            weekly_reset_at=weekly_at,
            checked_at=now,
            raw_session_text=page.session_timer_text,
            raw_weekly_text=page.weekly_timer_text,
        )
        sanity_check(snapshot, now=now)
        return snapshot

    def read_and_persist(self) -> QuotaSnapshot:
        """:meth:`read` + write the result to :attr:`output_path`."""
        snapshot = self.read()
        write_quota_state(snapshot, path=self._output_path)
        return snapshot

    @property
    def output_path(self) -> Path:
        return self._output_path


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def write_quota_state(
    snapshot: QuotaSnapshot,
    *,
    path: Path | str = DEFAULT_QUOTA_STATE_PATH,
) -> Path:
    """Write the snapshot to ``path`` as JSON.

    Atomic-ish: writes to a sibling temp file then renames. The
    parent directory is created if missing -- the agent-controller
    repo doesn't ship a ``data/`` dir in version control.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(snapshot.to_quota_state_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(p)
    return p


def read_quota_state(
    path: Path | str = DEFAULT_QUOTA_STATE_PATH,
) -> Optional[dict[str, Any]]:
    """Read the persisted snapshot back. Returns ``None`` if missing
    or unreadable -- mirrors :func:`quota_probe.read_state`."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


__all__ = [
    "CHROME_TOOL_EVALUATE",
    "CHROME_TOOL_NAVIGATE",
    "CHROME_TOOL_SNAPSHOT",
    "ChromeMCPClient",
    "ChromeMCPUsageScraper",
    "ChromeUsageScraper",
    "DEFAULT_QUOTA_STATE_PATH",
    "L8QuotaReader",
    "QuotaParseError",
    "QuotaSanityError",
    "QuotaSnapshot",
    "SESSION_WINDOW_MAX",
    "SOURCE_SCRAPED",
    "USAGE_PAGE_URL",
    "UsagePageContent",
    "WEEKLY_WINDOW_MAX",
    "parse_session_timer",
    "parse_weekly_timer",
    "read_quota_state",
    "sanity_check",
    "write_quota_state",
]
