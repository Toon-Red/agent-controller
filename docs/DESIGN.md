# Agent Controller -- Design Reference

This document collects design surfaces in the agent-controller package
that span multiple modules. Each section is paired with code under
`scripts/` and with a lint or test suite that asserts the doc stays in
sync with the code.

---

## Level Gate Enforcement

Level-gate enforcement (47c52660) makes the five `level_gates`
predicates shipped in `3f83494b` ENFORCING, not advisory. Predicates
that don't refuse are policy; refusal-via-exception is enforcement
(Preston rule 2026-05-23).

### Components

| module | exports |
|---|---|
| `scripts/level_gates.py` | 5 pure predicates + `LevelGateViolation(BaseException)` exception class + `ALL_GATES` registry. Frozen since 3f83494b. |
| `scripts/level_gate_enforcer.py` | `enforce(transition, record, **kwargs)`, `enforce_transition(transition, record, gate_fn, ...)`, `TRANSITION_TO_GATE` canonical mapping. |
| `scripts/show_violations.py` | CLI for inspecting persisted violations (`data/level-gate-violations/*.json`); `--since YYYY-MM-DD`, `--json` flags. |

### Canonical transition mapping

```python
TRANSITION_TO_GATE = {
    "L4->L5":      gate_l4_to_l5__tests_run,
    "L5->L6":      gate_l5_to_l6__quality_review,
    "L6->L7":      gate_l6_to_l7__swarm_complete,
    "L7->L8":      gate_l7_to_l8__green_ci,
    "L8->Preston": gate_l8_to_preston__decision_needed,
}
```

### Violation persistence

Refusals land in `data/level-gate-violations/<ts>-<gate_id>.json`:

```json
{
  "ts": "2026-05-23T14:30:00+00:00",
  "gate_id": "gate_l4_to_l5__tests_run",
  "transition": "L4->L5",
  "failing_record_id": "task-abc",
  "failed_predicates": ["task.test_results is empty or not a mapping"]
}
```

`scripts.discord_ping.send_escalation` posts a `warn`-level message on
the configured Discord channel (skipped when `DREAM_DISCORD_WEBHOOK`
is unset). Both persistence and ping are best-effort; the
`LevelGateViolation` itself ALWAYS propagates -- the contract is the
caller cannot work around the refusal.

### Wired call sites

| site | file:line | gate | activation |
|---|---|---|---|
| L7 dispatch | `scripts/l7_dispatch_protocol.py:276-294` | `L4->L5` | `L7DispatchClient.dispatch(req, source_task=task)` -- gate fires only when caller passes the hydrated task dict; backward-compatible (no source_task -> no gate) for old call sites pending hydration |
| L7->L8 progress observer | `scripts/l8_project_manager.py:515-540` | `L7->L8` | `L8ProjectManager.observe_l7_progress(msg, enforce_gate=True)` -- off by default during rollout so existing tests + the trajectory observer suite stay green; the production loop runtime flips it on at boot once green-qa observability lands |
| L8 -> Preston decision | (no production call site yet) | `L8->Preston` | `enforce("L8->Preston", payload)` helper available for direct invocation. The `L8Output` dataclass has no `decision_payload` field today -- when a future dispatch adds a `kind="decision"` path through `_dispatch`, wire `enforce` there. Honest-stop: tests cover the gate via direct `enforce()` calls |

### Pairs with AC-PRINCIPLES1 (d15de2b4)

The upcoming `d15de2b4` AC-PRINCIPLES1 dispatch will ship per-role
principles as a sibling registry; the principles run through
`enforce_transition` with the same exception + persistence shape so
operator surfaces (`show_violations` + Discord) stay uniform. No
infrastructure change required when the principles ship -- they
register as additional callables in a parallel `PRINCIPLE_TO_GATE`
mapping.

### Principles (d15de2b4 AC-PRINCIPLES1)

Six Amazon-LP-aligned principle predicates run alongside the five
`gate_*` predicates on every L-transition. Stacked semantics: ALL
must pass. Wiring lives in `level_gates.yaml` (per-transition list of
gates + principles); `PRINCIPLE_REGISTRY` in
`scripts/level_gate_enforcer.py` resolves names to callables.

| principle | predicate | enforced on | why it bites |
|---|---|---|---|
| Customer Obsession | `principle_customer_cited` | L7->L8, L8->Preston | record must declare `customer_impact` AND body cites `request:<hex8>` or `user_story:<hex8>` -- no anonymous escalations |
| Ownership | `principle_owner_declared` | L4->L5, L5->L6, L6->L7, L7->L8 | frontmatter `owner` non-empty AND in `wiki/owners.yaml` active list -- nobody handed off to "the team" |
| Dive Deep | `principle_data_cited` | L4->L5, L8->Preston | every `Decision:` line cites research/test/commit -- vibes-only decisions blocked |
| Insist on the Highest Standards | `principle_gates_passed` | L5->L6, L6->L7, L7->L8 | PD's close-gates evaluator returns 0 failures for the task (HTTP shim) -- can't promote work that won't close cleanly |
| Frugality | `principle_cost_evaluated` | L7->L8, L8->Preston | frontmatter has `complexity` in `S/M/L/XL` AND `value_score` 1..10 -- no unmeasured escalations |
| Bias for Action | `principle_no_indefinite_stall` | L4->L5, L5->L6 | `updated_at` within 7 days OR `status=blocked` with `blocked_reason` -- nothing sits forever |

Violations: `LevelGateViolation` gains a `failed_principles` field
separate from `failed_predicates`. The Discord ping + persisted JSON
both name the failing principle by predicate name (operators learn
the Amazon-LP mapping via `ALL_PRINCIPLES` constant + DESIGN.md
table).

Honest-stop: `principle_gates_passed` is a thin HTTP shim against PD
since the close-gate evaluator only fires on a real status flip. The
shim uses a heuristic surrogate (task's `quality.missing` includes
`tests` AND status is in-flight) and fails-closed when PD is
unreachable. A proper PD endpoint exposing the dry-run close-gate
evaluator is filed as a follow-on.

---

## LPrompt + HumanEngine (09ca6f69 AC-HUMAN-ENGINE1)

Every L4/L5/L6/L7 agent invocation goes through a fixed 5-block
prompt shape (`scripts/l_prompt.LPrompt`):

```python
class LPrompt(TypedDict, total=False):
    level: str          # L4 | L5 | L6 | L7
    agent_role: str     # coder | qa | manager | queen | dispatch | ...
    context_block: dict[str, Any]   # all data the agent needs
    task_block: dict[str, Any]      # inputs -> outputs -> done_when
    tools_block: list[str]          # hard-coded tool allowlist
    examples_block: list[dict]      # 1-2 golden output shapes
```

### Validator predicates

| validator | what it bites |
|---|---|
| `prompt_is_self_contained` | every `${ref}` / `{{ref}}` in task_block resolves to a key in context_block; no "go look elsewhere" |
| `prompt_has_done_when` | task_block.done_when references a test name / file path / record id / callable / count assertion / exit code; pure prose ("looks reasonable") rejected |
| `prompt_has_tool_allowlist` | tools_block is a non-empty list of strings; empty list = unbounded scope, refused |
| `prompt_humanly_executable` | composite: all 3 above must pass |

`validate_or_raise(prompt, level, role)` raises
`PromptValidationError(LevelGateViolation)` -- propagates as
`BaseException`, can't be swallowed by `except Exception`.

### HumanEngine (NOT a production engine)

`scripts/human_engine.HumanEngine` is a VALIDATION harness.
`run(prompt, *, timeout_sec=600)`:
  1. Validates via `validate_or_raise` (broken prompt never reaches disk).
  2. Writes prompt to `data/human-engine/<ts>/<level>-<role>.md`.
  3. Best-effort Discord ping with the file path.
  4. Polls every `poll_interval_sec` for the sibling
     `<level>-<role>.response.md` to appear.
  5. Parses the response's YAML frontmatter `outputs:` block.
  6. Returns the dict OR raises `HumanEngineTimeout` (BaseException).

The Preston-on-the-keyboard test: if Preston can read the prompt
cold and produce the expected output, the prompt is good enough
for the LLM. The HumanEngine doesn't ship with production loops --
it ships as the proof harness.

### Environment Sufficiency (97346fa1 AC-AGENT-ENV1)

Preston rule 2026-05-23 (verbatim): "It's more than just prompt. If
the UI and such is not good, or tools available don't help with
solving the tasks, or the user has to go outside the window to make
progress, then it's not fully thought out yet, and it needs to be
better prepared for."

Three additional predicates run alongside the 3 shape predicates
from 09ca6f69. The composite `prompt_humanly_executable` now runs
all six.

| predicate | what it bites |
|---|---|
| `prompt_has_ui_context_block` | `context_block.ui` is a non-empty dict carrying actual content payloads (file content, diff text, task records) -- not just paths/IDs. Heuristic: bare strings < 200 chars with no whitespace look like paths and fail the check when they're the entire ui payload. |
| `prompt_tool_allowlist_covers_task` | `tools_block` satisfies `level_gates.yaml > tool_requirements[<role>-<level>[-<category>]]` (resolution: exact first, then role+level fallback, then fail-closed). Each rule supports `must_have_all_of` + `must_have_at_least_one_of`. |
| `prompt_scope_is_self_contained_no_window_escape` | `task_block.done_when` + `outputs` don't contain window-escape verbs (user, preston, manual, external, wait for, someone) UNLESS the tools_block carries an observer (wait_for_event / observe / poll / subscribe / screenshot). |

### Auto-file findings (dedup-by-content-hash)

Every composite failure auto-files to
`data/prompt-validation-findings/<sha>.json` with sha256 of
(role + level + predicate + reason). Repeat failures bump
`seen_count` rather than re-filing. Same shape as the wiki_health
audit's auto-file mechanism (`5214e749`). Findings surface in
operator dashboards; no auto-conversion to PD tasks yet --
Discord ping volume is observed before that layer is added.

### Subset helpers + CLI

`check_env_only(prompt)` / `check_shape_only(prompt)` run only
the 3 env / 3 shape predicates respectively. The dry-run CLI
gains `--check-env-only` for fast Preston spot-checks of just the
env layer (UI block, tool coverage, no-window-escape).

### Sibling registry (honest-stop)

Per the dispatch's `4ae126d2`-coupling guardrail, this dispatch
ships a NEW `AGENT_ENGINE_REGISTRY: dict[str, Callable]` in
`scripts/human_engine.py` rather than retrofitting the existing
planning/scheduling/learning engine registry from `4ae126d2`
(those are EOD-step engines, different concept). Agent engines
implement `run(prompt) -> dict`; the registry pattern mirrors
`engines.registry` but operates on LPrompts.

### Per-(level, role) composers prevent context bleed

`scripts/prompt_emitter.py::build_prompt(task, project, level, role)`
dispatches to one of `_COMPOSERS[(level, role)]`. L4 coder sees
file paths + tests + acceptance; NOT project_state. L7 dispatch
sees project_state + open_tasks; NOT raw file diffs. Cross-level
bleed is structurally impossible because each (level, role) has
its own composer function -- the L4 composer doesn't know how to
read L7 strategic context. Missing (level, role) raises KeyError
so the operator sees the gap immediately.

### Dry-run CLI

`python scripts/dry_run_l_prompt.py --task <id> --level L4 --role coder`
builds + validates + prints the prompt WITHOUT engine invocation.
Operator dry-run for prompt reasonableness before turning the LLM
loose. `--json` emits the structured payload + validator report.

---

### Why `BaseException` (cross-cutting)

`LevelGateViolation` inherits from `BaseException` (not `Exception`)
so a future `except Exception` block in the L7/L8 dispatch path
cannot accidentally swallow a refusal. Test
`test_violation_not_caught_by_except_exception` pins this. Only an
explicit `except LevelGateViolation` (or `except BaseException`) in
the orchestrator runtime can catch the exception, and it should
NEVER do so to bypass the gate -- catch only to log + re-raise +
trigger the human-resolution path.
