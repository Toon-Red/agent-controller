# claude-integration -- portable Claude Code artifacts

Slash commands + hooks that run inside an operator's Claude Code session. Lives here (tracked) because per-project `.claude/` directories are typically gitignored (runtime state, secrets, session JSON).

## Contents

### commands/sync-quota.md

`/sync-quota` -- daily corrective re-sync of the automation-registry quota state from `claude.ai/settings/usage` via Chrome MCP. Part of AC-L8QUOTA1.

**Architecture:** Chrome MCP tools (`mcp__Claude_in_Chrome__*`) are MCP-protocol tools exposed by the browser extension and only callable from inside an active Claude Code session. This slash command is the integration shape -- not a Python subprocess (which can't reach the MCP server).

**Workflow:**
- AR-S3k phase A (operator-anchored quota state) is the primary source.
- This command runs daily as a corrective re-sync so the registry never drifts more than 24h from reality.

### hooks/quota_sync_check.py

SessionStart hook. Reads `data/last_quota_sync.json`; prints a stderr nudge if the last successful re-sync is >12h old. Silent otherwise.

## Install (per operator machine)

Each operator copies these into their Claude Code workspace's `.claude/` directory. For Preston's Dream workspace:

```sh
mkdir -p ~/Desktop/code/dream/.claude/commands ~/Desktop/code/dream/.claude/hooks
cp scripts/claude-integration/commands/sync-quota.md \
   ~/Desktop/code/dream/.claude/commands/sync-quota.md
cp scripts/claude-integration/hooks/quota_sync_check.py \
   ~/Desktop/code/dream/.claude/hooks/quota_sync_check.py
```

Then wire the SessionStart hook into `.claude/settings.json`:

```jsonc
"SessionStart": [
  {
    "hooks": [
      { "type": "command",
        "command": "python \"<absolute-path>/.claude/hooks/quota_sync_check.py\"",
        "timeout": 3000 }
    ]
  }
]
```

## Why these files live here

`agent-controller` is the L4-L8 hierarchy controller; integrations between the agent layer (L4-L8) and the operator-facing Claude Code session belong here as the canonical artifact, then get installed into each operator's `.claude/` directory.

If a different operator picks up the ecosystem, they install from this directory rather than reverse-engineering Preston's local `.claude/`.
