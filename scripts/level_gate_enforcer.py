"""Level-gate enforcer -- makes the 3f83494b predicates ENFORCING (47c52660).

The five predicates shipped by `scripts.level_gates` have been
pure functions with 34 tests since this morning, but until now no
production code called them on a real transition. This module
closes that gap.

Use::

    from scripts.level_gate_enforcer import enforce

    enforce("L4->L5", task)             # raises LevelGateViolation on refusal
    enforce("L7->L8", project_state)
    enforce("L8->Preston", decision_payload)

Refusals are persisted to ``data/level-gate-violations/<ts>-<gate_id>.json``
and (when ``ping=True``) posted to Discord via ``scripts.discord_ping.
send_escalation``. The exception is ``LevelGateViolation`` from
``scripts.level_gates`` -- it inherits from ``BaseException`` so it
cannot be swallowed by ``except Exception``, by design.

The orchestrator is responsible for surfacing + waiting for the
failing predicate to clear; the enforcer never auto-fixes the
source record. (Preston rule 2026-05-23: enforcement, not policy.)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from scripts.level_gates import (
    LevelGateViolation,
    gate_l4_to_l5__tests_run,
    gate_l5_to_l6__quality_review,
    gate_l6_to_l7__swarm_complete,
    gate_l7_to_l8__green_ci,
    gate_l8_to_preston__decision_needed,
)

log = logging.getLogger("level_gate_enforcer")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VIOLATIONS_DIR = REPO_ROOT / "data" / "level-gate-violations"

# Canonical wiring of L-level transitions to gate predicates. The
# transition string uses the Unicode arrow because the gate IDs use
# ASCII `__to__`; the arrow keeps the operator-facing surface
# readable. Tests pin the mapping so a future rewire is intentional.
TRANSITION_TO_GATE: dict[str, Callable] = {
    "L4->L5": gate_l4_to_l5__tests_run,
    "L5->L6": gate_l5_to_l6__quality_review,
    "L6->L7": gate_l6_to_l7__swarm_complete,
    "L7->L8": gate_l7_to_l8__green_ci,
    "L8->Preston": gate_l8_to_preston__decision_needed,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_id(record: Any) -> str:
    """Best-effort id extraction for the violation log."""
    if isinstance(record, Mapping):
        for key in ("id", "task_id", "project_id", "iteration_id"):
            v = record.get(key)
            if isinstance(v, str) and v:
                return v
    if isinstance(record, str):
        return record
    return "?"


def _persist_violation(*, gate_id: str, failed: list, record_id: str,
                       transition: str, base: Optional[Path] = None) -> Path:
    target_dir = base if base is not None else DEFAULT_VIOLATIONS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = _now_iso().replace(":", "-").replace("+", "_")
    path = target_dir / f"{stamp}-{gate_id}.json"
    payload = {
        "ts": _now_iso(),
        "gate_id": gate_id,
        "transition": transition,
        "failing_record_id": record_id,
        "failed_predicates": list(failed),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return path


def _send_ping(*, gate_id: str, failed: list, record_id: str,
               transition: str) -> None:
    """Discord escalation via scripts.discord_ping.send_escalation.

    Fail-open: if the webhook is unset / send raises, we log a warning
    and continue. The violation itself ALREADY raised; the ping is
    operator-courtesy, not the contract.
    """
    try:
        from scripts.discord_ping import send_escalation
        send_escalation(
            title=f"Level-gate violation: {transition} blocked by {gate_id}",
            body=(
                f"Record `{record_id}` failed predicates: "
                f"{', '.join(failed) or '(unspecified)'}\n\n"
                f"Source: scripts.level_gate_enforcer.enforce()\n"
                f"Resolve by fixing the source record; never bypass the gate."
            ),
            level="warn",
        )
    except Exception as exc:
        log.warning("discord ping for violation failed: %s", exc)


def enforce_transition(transition: str, record: Any, gate_fn: Callable,
                       *, ping: bool = True, log_to_disk: bool = True,
                       violations_dir: Optional[Path] = None) -> None:
    """Run ``gate_fn(record)`` and raise ``LevelGateViolation`` on refuse.

    The gate function is expected to return ``(passed: bool,
    failed_predicates: list[str])`` per the 3f83494b contract.
    ``ping`` + ``log_to_disk`` are kwargs so tests can disable either
    independently. ``violations_dir`` is injectable for tests.
    """
    passed, failed = gate_fn(record)
    if passed:
        return
    gate_id = getattr(gate_fn, "__name__", "?")
    record_id = _record_id(record)
    if log_to_disk:
        try:
            _persist_violation(
                gate_id=gate_id, failed=failed, record_id=record_id,
                transition=transition, base=violations_dir,
            )
        except Exception as exc:
            # Persisting is best-effort; never let it swallow the
            # actual violation.
            log.warning("could not persist violation: %s", exc)
    if ping:
        _send_ping(gate_id=gate_id, failed=failed,
                   record_id=record_id, transition=transition)
    raise LevelGateViolation(
        gate_id=gate_id, failed_predicates=failed,
        failing_record_id=record_id, transition=transition,
    )


def enforce(transition: str, record: Any, **kwargs) -> None:
    """Look up the gate for ``transition`` in ``TRANSITION_TO_GATE``
    and call :func:`enforce_transition`. Convenience wrapper for the
    canonical wiring used by L7/L8 call sites."""
    gate_fn = TRANSITION_TO_GATE.get(transition)
    if gate_fn is None:
        raise KeyError(
            f"unknown transition {transition!r}; known: "
            f"{sorted(TRANSITION_TO_GATE)}"
        )
    enforce_transition(transition, record, gate_fn, **kwargs)
