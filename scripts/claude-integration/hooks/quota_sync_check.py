"""SessionStart hook -- nudge operator to run /sync-quota when stale.

AC-L8QUOTA1 PHASE B. Triggers once per session-start. If the last
successful re-sync recorded in data/last_quota_sync.json is more than
12h old (or absent), prints a one-line reminder telling Preston to
run `/sync-quota`. Otherwise silent.

Honest defaults: no last-sync file -> first-run reminder. Bad JSON ->
silent (the hook must never break session-start).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DREAM_ROOT = Path(__file__).resolve().parent.parent.parent
LAST_SYNC_FILE = DREAM_ROOT / "data" / "last_quota_sync.json"
STALENESS_HOURS = 12


def _last_sync_age_hours() -> float | None:
    """Return hours since last successful re-sync, or None if unknown."""
    if not LAST_SYNC_FILE.exists():
        return None
    try:
        data = json.loads(LAST_SYNC_FILE.read_text(encoding="utf-8"))
        ts = data.get("timestamp")
        if not ts:
            return None
        candidate = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        last = datetime.fromisoformat(candidate)
        if last.tzinfo is None:
            return None
        delta = datetime.now(timezone.utc) - last
        return delta.total_seconds() / 3600.0
    except Exception:
        return None


def main() -> int:
    age = _last_sync_age_hours()
    if age is None:
        print(
            "[quota-sync] No quota re-sync on record. Run `/sync-quota` to "
            "pull the live values from claude.ai/settings/usage.",
            file=sys.stderr,
        )
        return 0
    if age > STALENESS_HOURS:
        print(
            f"[quota-sync] Last re-sync was {age:.1f}h ago "
            f"(>{STALENESS_HOURS}h staleness threshold). Run `/sync-quota` "
            "to correct any drift from the operator-anchored value.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
