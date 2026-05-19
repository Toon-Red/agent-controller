# AC-L8DOC1 -- evaluation + decision

**Date:** 2026-05-18
**Status:** REBUILD as L8 EOD sub-section (not as a standalone modal)
**Related:** PD-DOC1 (commit `b8c2ea2`, retired the in-app modal)

## The question

PD-DOC1 retired the documentation-audit modal in pipeline-dashboard -- chip + 7 dead tests removed because the modal logic never existed (forward-reference). Backend `GET /api/tasks/audit` (`pipeline-dashboard/routes/tasks.py:52` + `project_tasks.audit_documentation()`) was preserved.

Should L8 PM rebuild this as part of its oversight role -- surface undocumented tasks to Preston in SOD/EOD reports or a Dream tab section?

## Inputs

- **Backend already exists.** `audit_documentation()` returns per-project breakdown + aggregate `coverage_pct` + `missing_tests` / `missing_wiki` / `missing_both` task ID lists. Zero new endpoint work.
- **L8 already reads PD state.** `l8_state_reader.PDCalendarStateReader.snapshot()` is the canonical surface; doc audit fits as one more derived field on `StateSnapshot`.
- **EOD is the natural delivery channel.** `EODSections` already has `trajectory_delta` (free-text observations) + `at_risk_goals` (data) + `completion_counts` (numeric rollup). A `documentation_hygiene` field follows the same pattern.
- **PD-DOC1 retired the MODAL, not the concept.** A daily "X tasks need descriptions, here are the IDs" line in the EOD digest is fundamentally different from an interactive modal that didn't exist. The modal was friction; the digest line is information.

## Pros of L8-rebuild

- The data is observable, the delivery surface (EOD) is built, the consumer (Preston) reads EOD daily anyway.
- Documentation-as-friction is one of Preston's stated quality bars; L8 surfacing it daily reinforces that without requiring a new UI.
- Implementation cost is small: one snapshot field, one extractor, one render block, ~50 LOC + tests. Smaller than this proposal doc.
- Closes the question once and for all -- the backend stops being orphaned, the data starts being used.

## Cons of L8-rebuild

- Adds noise to the EOD digest (one more section). Mitigation: render only when count > 0, so a fully-documented day is silent.
- Risk of feeling like PD-DOC1 churn (just retired, now coming back). Mitigation: clear framing -- the modal was retired, the data was preserved deliberately, this is the surface that was always implied.

## Third options considered + rejected

- **SOD instead of EOD:** rejected. SOD is forward-looking ("today's calendar"); doc hygiene is retrospective ("yesterday's tasks lack docs"). EOD is the right slot.
- **Standalone Dream tab:** rejected. Modal-level surface is what PD-DOC1 retired for good reason -- nobody opens audit modals. EOD digest gets read.
- **Different app entirely (e.g., agent-commander):** rejected. agent-controller's L8 PM is the explicit "project oversight" role; doc audit IS project oversight.

## Decision

**BUILD, scoped tightly:** add doc-hygiene as a sub-section of L8's EOD output. No modal. No standalone tab. Silent when count is zero.

## Concrete sub-tasks

Two follow-ons filed on agent-controller (small, scoped):

1. **AC-L8DOC1a:** Add `documentation_audit` field to `l8_state_reader.PDCalendarStateReader.snapshot()` -- calls `GET /api/tasks/audit`, maps response into the StateSnapshot. S complexity.
2. **AC-L8DOC1b:** Surface `documentation_audit` in `l8_eod_generator.EODSections` + render block. Renders only when `missing_count > 0`. S complexity.

Total: ~50 LOC across two repos, two test files, one EOD render block.
