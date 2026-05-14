# agent-controller

L4-L8 agent hierarchy that wraps `claude-flow` (the npm package
shipping the `ruflo` CLI) into a configurable, level-aware
orchestration layer.

> **Upstream**: [claude-flow on npm](https://www.npmjs.com/package/claude-flow)
> (current: 3.7.0-alpha.34). We consume it as a dependency and
> compose its primitives -- we do NOT fork or modify upstream.

## Hierarchy (canonical per research `c1779970`)

- **L4** -- gruntwork: `coder`, `qa`, `playtester`, AND `grader`
  (high-compute L4 using Claude; standard L4s can run on local Ollama).
- **L5** -- managers + guides. Every L4 has an L5 manager (including
  the grader-L4).
- **L6** -- queen. `claude-flow`'s hive-mind primitives ARE L6.
- **L7** -- Dispatch (Preston's operational interface).
- **L8** -- Project Manager (Preston's oversight interface).

Engine choice is **per-role within a layer**, not uniform per-layer
(per Preston 2026-05-13 + 2026-05-13 correction).

## Why this exists (Preston verbatim 2026-05-13)

> "ruflow is a tool that other people use. We got it from github. We
> are wanting it to be utilized, but it's maintained externally. The
> plan is to utilize it in a format that we can keep updating it
> when they update it, and it not break our things."

This repo (`agent-controller`, Toon-Red) is the wrapper we
maintain. `claude-flow` upstream is the dependency. We never modify
upstream; we pin a known-good version, test compatibility, ship.

## Integration pattern

`claude-flow` is invoked through `npx`:

```json
// package.json
{
  "dependencies": {
    "claude-flow": "3.7.0-alpha.34"
  }
}
```

Local install: `npm install`. Invocation: `npx claude-flow hive-mind spawn ...`.

Why this pattern over a git submodule:

1. **claude-flow is published on npm** with semver-pinned releases.
   No need to vendor source.
2. **Upgrade workflow** is `npm install claude-flow@<new>` + run
   our compat tests. Submodules are heavier and don't carry the
   same release-channel guarantees.
3. **`npx`** handles the CLI invocation without a global install,
   matching the pattern already in `templates/mcp.json`.

See [docs/upgrade-claude-flow.md](docs/upgrade-claude-flow.md) for
the upgrade playbook.

## Scope

**In scope:**
- L4-L8 agent template definitions (markdown files in `templates/agents/`).
- Per-role engine config (claude-opus / sonnet / haiku / ollama-local; resolver per AC-S9).
- Multi-engine driver (AC-S10).
- L8 PM component (AC-S16) -- generates SOD/EOD output, talks to Preston.
- Cost tracking + dream-progress + pd-reporter hooks.

**Out of scope:**
- The claude-flow core codebase (upstream, ruvnet/claude-flow).
- Work-item selection (Dream + Calendar do that).
- Work-tracking persistence (Pipeline Dashboard does that).
- Schedule management (automation-registry does that).

## Status

**Bootstrap.** AC-S13 (`7315deb6`) shipped the initial repo
scaffolding + npm dependency declaration + upgrade docs.

Pending sub-tasks land via the AC roadmap on Pipeline Dashboard
(research umbrella `c1779970`).

## References

- Research: PD `c1779970` (L4-L8 hierarchy + 2026-05-13 amendments).
- Schema v2: `automation-registry/docs/proposals/automation-registry-schema-v2.md`.
- Upgrade playbook: [docs/upgrade-claude-flow.md](docs/upgrade-claude-flow.md).
