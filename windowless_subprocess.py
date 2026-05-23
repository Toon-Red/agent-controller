"""Windowless subprocess helper (d48d4ddb).

Wraps ``subprocess.run`` / ``subprocess.Popen`` so background
orchestration ticks (calendar scheduler, EOD qa_phase, Dream Auto
cycles, etc.) don't flash a visible Python console window on every
invocation on Windows.

``CREATE_NO_WINDOW`` is the Windows process-creation flag that
suppresses the console window; on POSIX it's a no-op. We also pick
``pythonw.exe`` when available so even shell=True invocations stay
silent on Windows.

WINDOWLESS != silent. stdout/stderr are still captured; exit codes
still propagate; run records still get full output. The only thing
suppressed is the visible cmd.exe popup.

Test-helpers that mock ``subprocess.run`` directly are unaffected --
the wrapper just calls through to subprocess after merging kwargs.

Pythonw fallback: some venv setups (especially conda + pyenv) don't
ship ``pythonw.exe`` next to ``python.exe``. When ``pythonw.exe`` is
absent the helper falls back to ``sys.executable``; ``CREATE_NO_WINDOW``
still suppresses the popup so the user experience is identical.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

# Windows process-creation flag: do not allocate a console window.
# Documented at:
#   https://docs.microsoft.com/en-us/windows/win32/procthread/process-creation-flags
CREATE_NO_WINDOW = 0x08000000


def _is_windows() -> bool:
    return os.name == "nt"


def windowless_kwargs(kwargs: Optional[dict] = None) -> dict:
    """Return ``kwargs`` with Windows-windowless flags merged in.

    On Windows: OR-merges ``creationflags`` with ``CREATE_NO_WINDOW``.
    On POSIX: no-op (returns ``kwargs`` unchanged).

    Preserves any caller-supplied creationflags; the bitwise-OR means
    a caller that already set ``DETACHED_PROCESS`` keeps it AND gets
    windowless behavior.
    """
    out = dict(kwargs or {})
    if _is_windows():
        existing = int(out.get("creationflags", 0) or 0)
        out["creationflags"] = existing | CREATE_NO_WINDOW
    return out


def run(*args, **kwargs) -> subprocess.CompletedProcess:
    """Drop-in replacement for ``subprocess.run`` with windowless flags."""
    return subprocess.run(*args, **windowless_kwargs(kwargs))


def Popen(*args, **kwargs) -> subprocess.Popen:
    """Drop-in replacement for ``subprocess.Popen`` with windowless flags."""
    return subprocess.Popen(*args, **windowless_kwargs(kwargs))


def python_exe() -> str:
    """Return ``pythonw.exe`` on Windows when it exists, else
    ``sys.executable``.

    ``pythonw.exe`` is the windowless variant shipped with CPython on
    Windows; using it as the interpreter for scripts spawned via
    ``shell=True`` (where ``CREATE_NO_WINDOW`` isn't enough) keeps the
    invocation silent. The fallback is safe because the helper's
    ``run``/``Popen`` already pass ``CREATE_NO_WINDOW`` -- popups are
    suppressed either way.
    """
    if not _is_windows():
        return sys.executable
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    if candidate.is_file():
        return str(candidate)
    return sys.executable
