#!/usr/bin/env python3
"""SessionStart hook: agent-controller health probe.

Reports whether the npm dep (claude-flow) is installed and
discoverable. Cross-platform stdlib only. Times out fast (~1.5s).
Exits 0 unconditionally -- informational, not a gate.
"""
from __future__ import annotations

import shutil
import subprocess
import sys


def main() -> int:
    if not shutil.which("npx"):
        print("[agent-controller] npx not on PATH -- claude-flow "
              "invocations will fail.")
        return 0
    try:
        proc = subprocess.run(
            ["npx", "claude-flow", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            version = (proc.stdout or "").strip().splitlines()[-1]
            print(f"[agent-controller] UP -- claude-flow {version}")
        else:
            print("[agent-controller] DOWN -- "
                  f"npx claude-flow --version exit {proc.returncode}; "
                  "run `npm install` in the repo root.")
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[agent-controller] DOWN -- {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
