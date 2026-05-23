"""Inspect persisted level-gate violations (47c52660).

Usage::

    python scripts/show_violations.py
    python scripts/show_violations.py --since 2026-05-23
    python scripts/show_violations.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = REPO_ROOT / "data" / "level-gate-violations"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", default=None,
                        help="ISO date (YYYY-MM-DD); only show on/after this date.")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON array instead of human output.")
    parser.add_argument("--dir", default=str(DEFAULT_DIR))
    args = parser.parse_args(argv)
    target = Path(args.dir)
    if not target.is_dir():
        print(f"no violations directory at {target} (nothing recorded yet)",
              file=sys.stderr)
        return 0
    since_date: date | None = None
    if args.since:
        try:
            since_date = date.fromisoformat(args.since)
        except ValueError:
            print(f"FATAL: --since {args.since!r} not ISO YYYY-MM-DD",
                  file=sys.stderr)
            return 2
    records: list[dict] = []
    for p in sorted(target.glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        ts = rec.get("ts", "")
        if since_date:
            try:
                rec_date = date.fromisoformat(ts[:10])
            except ValueError:
                continue
            if rec_date < since_date:
                continue
        records.append(rec)
    records.sort(key=lambda r: r.get("ts", ""))
    if args.json:
        print(json.dumps(records, indent=2, ensure_ascii=False))
        return 0
    if not records:
        print("(no violations found)")
        return 0
    print(f"Level-gate violations under {target} "
          f"({len(records)} record(s)):")
    for r in records:
        print(f"  {r.get('ts', '?')}  {r.get('transition', '?'):14s}"
              f" {r.get('gate_id', '?'):40s} record={r.get('failing_record_id', '?')}")
        for p in r.get("failed_predicates", []):
            print(f"    - {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
