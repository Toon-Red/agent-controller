"""AC-L8QUOTA1: tests for the Chrome MCP-driven Pro/Max quota reader.

Covers:

* Pure parsers -- ``parse_session_timer`` / ``parse_weekly_timer``
  against the wording variants the Usage page is documented to emit.
* :class:`ChromeMCPUsageScraper` -- the production adapter calls
  the canonical Chrome MCP tool names, in order, and regexes the
  two timer strings out of the snapshot payload.
* :class:`L8QuotaReader` -- orchestration honours the injected
  scraper + clock, refuses to write to disk on parse / sanity
  failures, and produces a QuotaState JSON shape compatible with
  ``automation-registry/quota_probe.py``.
* :func:`sanity_check` -- rejects past timestamps + values beyond
  the documented Pro/Max windows.
* Round-trip persistence -- ``write_quota_state`` /
  ``read_quota_state`` are inverses of each other.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts.l8_quota_reader import (
    CHROME_TOOL_NAVIGATE,
    CHROME_TOOL_SNAPSHOT,
    ChromeMCPUsageScraper,
    ChromeUsageScraper,
    DEFAULT_QUOTA_STATE_PATH,
    L8QuotaReader,
    QuotaParseError,
    QuotaSanityError,
    QuotaSnapshot,
    SOURCE_SCRAPED,
    USAGE_PAGE_URL,
    UsagePageContent,
    parse_session_timer,
    parse_weekly_timer,
    read_quota_state,
    sanity_check,
    write_quota_state,
)


UTC = timezone.utc


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeChromeMCP:
    """Records every call_tool invocation; returns canned payloads."""

    def __init__(self, snapshot_payload: Any) -> None:
        self._snapshot_payload = snapshot_payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, /, **params: Any) -> Any:
        self.calls.append((name, dict(params)))
        if name == CHROME_TOOL_NAVIGATE:
            return None
        if name == CHROME_TOOL_SNAPSHOT:
            return self._snapshot_payload
        raise AssertionError(f"unexpected Chrome MCP call: {name}")


class _FakeScraper:
    """In-memory ChromeUsageScraper used by reader-level tests."""

    def __init__(
        self,
        *,
        session: str = "Resets in 3 hr 21 min",
        weekly: str = "Resets Tue 9:00 PM",
        raises: Exception | None = None,
    ) -> None:
        self._session = session
        self._weekly = weekly
        self._raises = raises
        self.calls = 0

    def fetch_usage_content(self) -> UsagePageContent:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return UsagePageContent(
            session_timer_text=self._session,
            weekly_timer_text=self._weekly,
        )


# ---------------------------------------------------------------------------
# parse_session_timer
# ---------------------------------------------------------------------------


def test_session_parser_extracts_hours_and_minutes():
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    out = parse_session_timer("Resets in 3 hr 21 min", now=now)
    assert out == now + timedelta(hours=3, minutes=21)


def test_session_parser_handles_minutes_only():
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    out = parse_session_timer("Resets in 47 min", now=now)
    assert out == now + timedelta(minutes=47)


def test_session_parser_handles_hours_only():
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    out = parse_session_timer("Resets in 4 hr", now=now)
    assert out == now + timedelta(hours=4)


def test_session_parser_is_case_insensitive_and_whitespace_tolerant():
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    out = parse_session_timer("  RESETS  IN   2 hr  5 min  ", now=now)
    assert out == now + timedelta(hours=2, minutes=5)


def test_session_parser_rejects_non_matching_text():
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    with pytest.raises(QuotaParseError):
        parse_session_timer("usage information unavailable", now=now)


def test_session_parser_rejects_zero_duration_text():
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    # The regex pattern requires "Resets in" but if no hr/min follow we
    # treat that as undefined; "Resets in" alone is rejected because
    # both groups are zero.
    with pytest.raises(QuotaParseError):
        parse_session_timer("Resets in", now=now)


def test_session_parser_refuses_naive_now():
    with pytest.raises(ValueError):
        parse_session_timer("Resets in 1 hr", now=datetime(2026, 5, 14, 17, 0))


# ---------------------------------------------------------------------------
# parse_weekly_timer
# ---------------------------------------------------------------------------


def test_weekly_parser_returns_next_weekday_at_local_time():
    # Thu 2026-05-14 17:00 UTC -- next Tuesday 9:00 PM is 2026-05-19 21:00 UTC.
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    out = parse_weekly_timer("Resets Tue 9:00 PM", now=now)
    assert out == datetime(2026, 5, 19, 21, 0, tzinfo=UTC)


def test_weekly_parser_rolls_forward_when_today_already_past_target_time():
    # Tue 2026-05-12 22:00 UTC -- target "Tue 9:00 PM" already passed
    # today, so the reset is next Tuesday.
    now = datetime(2026, 5, 12, 22, 0, tzinfo=UTC)
    out = parse_weekly_timer("Resets Tue 9:00 PM", now=now)
    assert out == datetime(2026, 5, 19, 21, 0, tzinfo=UTC)


def test_weekly_parser_returns_today_if_target_time_still_ahead():
    # Tue 2026-05-12 13:00 UTC -- "Tue 9:00 PM" is later today.
    now = datetime(2026, 5, 12, 13, 0, tzinfo=UTC)
    out = parse_weekly_timer("Resets Tue 9:00 PM", now=now)
    assert out == datetime(2026, 5, 12, 21, 0, tzinfo=UTC)


def test_weekly_parser_handles_full_weekday_names():
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    out = parse_weekly_timer("Resets Tuesday 9:00 PM", now=now)
    assert out == datetime(2026, 5, 19, 21, 0, tzinfo=UTC)


def test_weekly_parser_handles_am_pm_correctly():
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    midnight = parse_weekly_timer("Resets Sat 12:00 AM", now=now)
    noon = parse_weekly_timer("Resets Sat 12:00 PM", now=now)
    assert midnight.hour == 0
    assert noon.hour == 12


def test_weekly_parser_rejects_non_matching_text():
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    with pytest.raises(QuotaParseError):
        parse_weekly_timer("Resets next week", now=now)


def test_weekly_parser_rejects_out_of_range_hour():
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    with pytest.raises(QuotaParseError):
        parse_weekly_timer("Resets Tue 13:00 PM", now=now)


def test_weekly_parser_refuses_naive_now():
    with pytest.raises(ValueError):
        parse_weekly_timer("Resets Tue 9:00 PM", now=datetime(2026, 5, 14, 17, 0))


# ---------------------------------------------------------------------------
# ChromeMCPUsageScraper (production adapter)
# ---------------------------------------------------------------------------


def test_scraper_calls_navigate_then_snapshot_in_order():
    mcp = _FakeChromeMCP({
        "text": "...Resets in 3 hr 21 min...Weekly limits...Resets Tue 9:00 PM..."
    })
    scraper = ChromeMCPUsageScraper(client=mcp)
    content = scraper.fetch_usage_content()
    assert content.session_timer_text.lower().startswith("resets in")
    assert content.weekly_timer_text.lower().startswith("resets tue")
    assert [name for name, _ in mcp.calls] == [
        CHROME_TOOL_NAVIGATE, CHROME_TOOL_SNAPSHOT,
    ]
    # navigate gets the usage URL.
    assert mcp.calls[0][1] == {"url": USAGE_PAGE_URL}


def test_scraper_handles_json_snapshot_payload():
    """Chrome MCP may emit an a11y tree instead of a flat text string."""
    payload = {
        "role": "document",
        "children": [
            {"role": "text", "name": "Resets in 4 hr"},
            {"role": "text", "name": "Resets Fri 11:00 AM"},
        ],
    }
    mcp = _FakeChromeMCP(payload)
    scraper = ChromeMCPUsageScraper(client=mcp)
    content = scraper.fetch_usage_content()
    assert "4 hr" in content.session_timer_text
    assert "Fri" in content.weekly_timer_text


def test_scraper_raises_on_dom_drift():
    mcp = _FakeChromeMCP({"text": "no timers here"})
    scraper = ChromeMCPUsageScraper(client=mcp)
    with pytest.raises(QuotaParseError):
        scraper.fetch_usage_content()


def test_scraper_satisfies_protocol():
    mcp = _FakeChromeMCP({"text": "Resets in 1 hr ... Resets Mon 1:00 AM"})
    scraper = ChromeMCPUsageScraper(client=mcp)
    assert isinstance(scraper, ChromeUsageScraper)


# ---------------------------------------------------------------------------
# QuotaSnapshot + to_quota_state_dict
# ---------------------------------------------------------------------------


def test_snapshot_next_reset_is_min_of_two_timers():
    session_at = datetime(2026, 5, 14, 20, 0, tzinfo=UTC)
    weekly_at = datetime(2026, 5, 19, 21, 0, tzinfo=UTC)
    checked_at = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    snap = QuotaSnapshot(
        session_reset_at=session_at,
        weekly_reset_at=weekly_at,
        checked_at=checked_at,
        raw_session_text="Resets in 3 hr",
        raw_weekly_text="Resets Tue 9:00 PM",
    )
    assert snap.next_reset_at == session_at


def test_quota_state_dict_carries_ar_s3k_canonical_keys():
    snap = QuotaSnapshot(
        session_reset_at=datetime(2026, 5, 14, 20, 0, tzinfo=UTC),
        weekly_reset_at=datetime(2026, 5, 19, 21, 0, tzinfo=UTC),
        checked_at=datetime(2026, 5, 14, 17, 0, tzinfo=UTC),
        raw_session_text="Resets in 3 hr",
        raw_weekly_text="Resets Tue 9:00 PM",
    )
    d = snap.to_quota_state_dict()
    # AR-S3k contract.
    assert d["source"] == SOURCE_SCRAPED
    assert d["next_reset_ts"] == "2026-05-14T20:00:00+00:00"
    assert d["tokens_remaining"] is None
    assert d["requests_remaining"] is None
    assert d["api_tokens_reset_ts"] is None
    assert d["api_requests_reset_ts"] is None
    assert d["operator_reset_ts"] is None
    assert d["checked_at"] == "2026-05-14T17:00:00+00:00"
    assert d["notes"] == []
    # Two-timer-model additions.
    assert d["session_reset_at"] == "2026-05-14T20:00:00+00:00"
    assert d["weekly_reset_at"] == "2026-05-19T21:00:00+00:00"
    assert d["raw_session_text"] == "Resets in 3 hr"
    assert d["raw_weekly_text"] == "Resets Tue 9:00 PM"


# ---------------------------------------------------------------------------
# sanity_check
# ---------------------------------------------------------------------------


def _good_snapshot(now: datetime) -> QuotaSnapshot:
    return QuotaSnapshot(
        session_reset_at=now + timedelta(hours=3),
        weekly_reset_at=now + timedelta(days=4),
        checked_at=now,
        raw_session_text="Resets in 3 hr",
        raw_weekly_text="Resets Tue 9:00 PM",
    )


def test_sanity_check_passes_for_well_formed_snapshot():
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    sanity_check(_good_snapshot(now), now=now)


def test_sanity_check_rejects_past_session():
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    snap = QuotaSnapshot(
        session_reset_at=now - timedelta(minutes=1),
        weekly_reset_at=now + timedelta(days=4),
        checked_at=now,
        raw_session_text="x", raw_weekly_text="y",
    )
    with pytest.raises(QuotaSanityError):
        sanity_check(snap, now=now)


def test_sanity_check_rejects_past_weekly():
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    snap = QuotaSnapshot(
        session_reset_at=now + timedelta(hours=3),
        weekly_reset_at=now - timedelta(seconds=1),
        checked_at=now,
        raw_session_text="x", raw_weekly_text="y",
    )
    with pytest.raises(QuotaSanityError):
        sanity_check(snap, now=now)


def test_sanity_check_rejects_session_beyond_5h_window():
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    snap = QuotaSnapshot(
        session_reset_at=now + timedelta(hours=6),  # exceeds 5h10m slack
        weekly_reset_at=now + timedelta(days=4),
        checked_at=now,
        raw_session_text="x", raw_weekly_text="y",
    )
    with pytest.raises(QuotaSanityError):
        sanity_check(snap, now=now)


def test_sanity_check_rejects_weekly_beyond_7d_window():
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    snap = QuotaSnapshot(
        session_reset_at=now + timedelta(hours=3),
        weekly_reset_at=now + timedelta(days=8),
        checked_at=now,
        raw_session_text="x", raw_weekly_text="y",
    )
    with pytest.raises(QuotaSanityError):
        sanity_check(snap, now=now)


def test_sanity_check_accepts_5h_exact():
    """A scrape captured the instant the session window started -- 5h
    exact should pass (slack covers UI rounding)."""
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    snap = QuotaSnapshot(
        session_reset_at=now + timedelta(hours=5),
        weekly_reset_at=now + timedelta(days=4),
        checked_at=now,
        raw_session_text="x", raw_weekly_text="y",
    )
    sanity_check(snap, now=now)  # no raise


# ---------------------------------------------------------------------------
# L8QuotaReader orchestration
# ---------------------------------------------------------------------------


def _fixed_clock(ts: datetime):
    return lambda: ts


def test_reader_read_returns_snapshot_passing_sanity_check(tmp_path: Path):
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    reader = L8QuotaReader(
        scraper=_FakeScraper(
            session="Resets in 3 hr 21 min",
            weekly="Resets Tue 9:00 PM",
        ),
        output_path=tmp_path / "quota_state.json",
        clock=_fixed_clock(now),
    )
    snap = reader.read()
    assert snap.session_reset_at == now + timedelta(hours=3, minutes=21)
    assert snap.weekly_reset_at == datetime(2026, 5, 19, 21, 0, tzinfo=UTC)
    # No file written by read() alone.
    assert not (tmp_path / "quota_state.json").exists()


def test_reader_read_and_persist_writes_file(tmp_path: Path):
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    out = tmp_path / "quota_state.json"
    reader = L8QuotaReader(
        scraper=_FakeScraper(),
        output_path=out,
        clock=_fixed_clock(now),
    )
    snap = reader.read_and_persist()
    assert out.is_file()
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert on_disk["source"] == SOURCE_SCRAPED
    assert on_disk["session_reset_at"].startswith("2026-05-14")
    assert on_disk["next_reset_ts"] == on_disk["session_reset_at"]  # session < weekly
    assert on_disk == snap.to_quota_state_dict()


def test_reader_does_not_write_on_parse_failure(tmp_path: Path):
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    out = tmp_path / "quota_state.json"
    reader = L8QuotaReader(
        scraper=_FakeScraper(session="totally broken text"),
        output_path=out,
        clock=_fixed_clock(now),
    )
    with pytest.raises(QuotaParseError):
        reader.read_and_persist()
    assert not out.exists()


def test_reader_does_not_write_on_sanity_failure(tmp_path: Path):
    # A scraper that returns a session of 99 hr fails the 5h sanity gate.
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    out = tmp_path / "quota_state.json"
    reader = L8QuotaReader(
        scraper=_FakeScraper(session="Resets in 99 hr"),
        output_path=out,
        clock=_fixed_clock(now),
    )
    with pytest.raises(QuotaSanityError):
        reader.read_and_persist()
    assert not out.exists()


def test_reader_refuses_to_run_with_naive_clock(tmp_path: Path):
    reader = L8QuotaReader(
        scraper=_FakeScraper(),
        output_path=tmp_path / "quota_state.json",
        clock=lambda: datetime(2026, 5, 14, 17, 0),  # no tzinfo
    )
    with pytest.raises(ValueError):
        reader.read()


def test_reader_propagates_scraper_errors(tmp_path: Path):
    boom = QuotaParseError("DOM drift")
    reader = L8QuotaReader(
        scraper=_FakeScraper(raises=boom),
        output_path=tmp_path / "quota_state.json",
        clock=_fixed_clock(datetime(2026, 5, 14, 17, 0, tzinfo=UTC)),
    )
    with pytest.raises(QuotaParseError):
        reader.read()


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------


def test_write_then_read_round_trip(tmp_path: Path):
    now = datetime(2026, 5, 14, 17, 0, tzinfo=UTC)
    snap = _good_snapshot(now)
    out = tmp_path / "data" / "quota_state.json"  # tests parent-mkdir path
    write_quota_state(snap, path=out)
    loaded = read_quota_state(out)
    assert loaded is not None
    assert loaded["next_reset_ts"] == loaded["session_reset_at"]
    assert loaded["source"] == SOURCE_SCRAPED


def test_read_quota_state_returns_none_for_missing_file(tmp_path: Path):
    assert read_quota_state(tmp_path / "does-not-exist.json") is None


def test_read_quota_state_returns_none_for_corrupt_file(tmp_path: Path):
    p = tmp_path / "broken.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert read_quota_state(p) is None


# ---------------------------------------------------------------------------
# Module-level pinning (catches accidental rename)
# ---------------------------------------------------------------------------


def test_chrome_tool_names_are_canonical():
    assert CHROME_TOOL_NAVIGATE == "mcp__claude-in-chrome__navigate"
    assert CHROME_TOOL_SNAPSHOT == "mcp__claude-in-chrome__snapshot"


def test_usage_page_url_is_claude_dot_ai_settings_usage():
    assert USAGE_PAGE_URL == "https://claude.ai/settings/usage"


def test_default_quota_state_path_lives_under_repo_data_dir():
    # The repo root is the parent of `scripts/`.
    assert DEFAULT_QUOTA_STATE_PATH.name == "quota_state.json"
    assert DEFAULT_QUOTA_STATE_PATH.parent.name == "data"
