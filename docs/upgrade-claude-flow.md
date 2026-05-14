# Upgrade playbook -- claude-flow

`claude-flow` is the npm package that ships the `ruflo` CLI + the
hive-mind primitives we treat as L6 (per research `c1779970`).
Upstream releases happen on `npmjs.com/package/claude-flow`; this
doc is how WE update our pin without breaking anything.

## Cadence

  - Check for upstream release at least weekly OR when an upstream
    bug fix / feature is needed.
  - **Do not bump silently** -- always run the compat tests below.
  - **Major versions** (e.g. 3.x -> 4.x) get their own PR with a
    deliberate compat audit. Minor + patch bumps can ride a regular
    PR.

## Workflow

1. **Read the upstream release notes.**

   ```bash
   npm view claude-flow versions      # what versions exist
   npm view claude-flow@<new>         # the metadata for that version
   ```

   Skim the release notes / changelog if the upstream provides one.
   Pay attention to: `hive-mind spawn` CLI flag changes, MCP tool
   additions/renames (we wire several `mcp__claude-flow__*` tools by
   name), config-file schema shifts.

2. **Bump the pin.**

   ```bash
   npm install claude-flow@<new>       # updates package.json + lockfile
   ```

   `package.json`'s `dependencies.claude-flow` field gets the new
   version. Commit on a branch -- NOT directly to master.

3. **Run the compat tests.** (See "Tests we run for every bump"
   below.)

4. **Smoke test the templates.** Spawn one queen with our existing
   templates against a no-op task:

   ```bash
   npx claude-flow hive-mind spawn --agent templates/agents/queen-coordinator.md --task "noop check"
   ```

   Expected: the queen starts, hits a defined stopping point, exits
   cleanly. If it errors, the upgrade is gated -- don't merge.

5. **Smoke test the hooks.** Our `hooks/` (cost-tracker,
   dream-progress, etc.) hook into claude-flow lifecycle events. If
   upstream renames or removes an event, our hooks silently no-op
   until we update them. Verify each hook fires at least once during
   the smoke spawn (instrument with a temporary log line if needed).

6. **Update `templates/mcp.json`** if upstream changed the MCP
   server's invocation. Today it's
   `{ "command": "npx", "args": ["claude-flow", "mcp", "start"] }`.
   If upstream switches the entry point, mirror it here.

7. **Commit + open a PR titled** `chore(deps): bump claude-flow
   3.x.y -> 3.x.z`. Include in the PR body:

   - upstream release notes link
   - smoke-test output
   - any template / hook changes

8. **Merge** once CI is green + Preston approves.

## Tests we run for every bump

  - `npm install` succeeds.
  - `npx claude-flow --version` returns the new version.
  - `npx claude-flow mcp start` starts the MCP server (kill after
    confirming it's listening; don't leave it running in CI).
  - `pytest` -- the Python tests in this repo pass (cover our
    wrapper code, not upstream behaviour).
  - Smoke spawn (step 4 above) succeeds.

If ANY of these fail, the bump is blocked; investigate the
breaking change.

## Rollback

`npm install claude-flow@<old>` and commit. Lockfile reverts the
exact tree. Upgrade attempts are reversible.

## Major-version audit checklist (3.x -> 4.x etc.)

Beyond the per-bump tests, a major-version upgrade walks through:

  - [ ] Read the upstream MIGRATION.md / changelog top-to-bottom.
  - [ ] Diff every template's `mcp__claude-flow__*` tool usage
        against the new tool list (`npx claude-flow mcp list-tools`
        or equivalent).
  - [ ] Re-run all our hook tests; major versions sometimes rename
        the hook event taxonomy.
  - [ ] Verify the cost-tracker still observes the right output
        format (the upstream may change session-finalisation
        events).
  - [ ] Update this doc if the workflow itself shifts.

## When to PIN vs FLOAT

We **pin** an exact version (e.g. `"claude-flow": "3.7.0-alpha.34"`)
rather than a range. Reasons:

  - Upstream is on alpha tags -- ranges (`^3.7.0`) would auto-
    upgrade into incompatible alphas.
  - Reproducibility: every checkout gets the exact tree we tested.

When upstream graduates to stable (no `-alpha` / `-beta`), revisit
the pin policy.

## References

- Upstream: https://www.npmjs.com/package/claude-flow
- GitHub: https://github.com/ruvnet/claude-flow (per the wiki/Billing Audit.md trace in the ruflow customisation layer)
- Local customisations (templates/hooks): `~/Desktop/code/ruflow/`
  (will migrate into this repo as part of subsequent AC-Sn tasks).
