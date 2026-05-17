# AC-L8QUOTA1 scoping -- 2026-05-17

**Verdict: BLOCKED at PHASE A.** The premise (L8 reads Claude Usage page DOM via Chrome MCP) depends on the `claude-in-chrome` MCP extension being installed + connected, and it currently isn't on this machine.

## What was checked

`mcp__Claude_in_Chrome__list_connected_browsers` returned `[]` (zero connected browser instances). Per the MCP server's own guidance: "If the Chrome extension isn't connected, ask the user to install it rather than falling through to computer use." Computer-use fallback is also gated -- Chrome / Safari / Firefox are tier "read", meaning screenshots work but clicks/typing don't, so even with computer-use access we couldn't navigate the Usage UI.

## What this blocks

- Reading the Pro/Max 5h session reset timer
- Reading the weekly reset timer
- Auto-populating registry quota state for AR-S3k

AR-S3k's three options surfaced in Q-MAIN findings:
- (a) operator-anchored timer
- (b) scrape Claude Code Desktop UI via Chrome MCP (THIS task's approach)
- (c) empirical test of whether org headers reflect the Pro/Max session

This task scopes (b). It's the architecturally cleanest if Chrome MCP is available; with no extension, it's a non-starter and Preston needs to either install the extension or pick (a) / (c) instead.

## DOM mechanics (unverified, would land in PHASE B)

Best guess at URL: `https://claude.ai/settings/usage`. Preston's prior Usage UI screenshot (referenced in `AR-S3-Q-MAIN-headers-findings.md` lines 107-115) shows two timer values:
- Current session: "Resets in X hr Y min" (relative)
- Weekly limits: "Resets <weekday> <time>" (absolute)

Stable CSS selectors versus text-matching is unknown without DOM inspection -- which is the verification PHASE A was supposed to do.

## Unblock paths

1. **Install the claude-in-chrome browser extension** + retry this scoping task.
2. **Switch to AR-S3k option (a)**: operator-anchored timer. First Claude call's timestamp + 5h is the session reset; weekly is a static cron (Tue 9pm PT or whatever Preston's billing day is). Manual configuration in `data/quota_state.json`. Smaller surface, no MCP dependency.
3. **Switch to AR-S3k option (c)**: empirical test of whether `anthropic-ratelimit-tokens-reset` actually reflects the Pro/Max session (zero-cost check; if true, the original AR-S3k probe works without UI scraping).

Recommended: try (c) first since it's cheapest and may eliminate the need for either (a) or (b).
