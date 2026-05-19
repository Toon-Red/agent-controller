---
name: sync-quota
description: Re-sync the automation-registry quota state from claude.ai/settings/usage via Chrome MCP (AC-L8QUOTA1). Reads the live session + weekly reset countdowns, parses them to ISO 8601, POSTs to localhost:5050.
---

# /sync-quota — re-sync quota state from claude.ai Usage page

Operator-anchored quota state (AR-S3k option A) is the **primary** source.
This command runs option B as a **daily corrective re-sync** so the registry
value never drifts more than 24h from reality.

## Procedure

1. **Connect to Chrome MCP.** Call `mcp__Claude_in_Chrome__list_connected_browsers`. If empty, surface: "Chrome MCP extension not connected — operator-anchored state remains authoritative; no re-sync this run." Exit.

2. **Open / reuse a tab.** Call `mcp__Claude_in_Chrome__tabs_context_mcp` with `createIfEmpty=true`. Pick a tabId from the returned group.

3. **Navigate.** `mcp__Claude_in_Chrome__navigate` with `url: "https://claude.ai/settings/usage"` and the tabId from step 2.

4. **Wait for render.** The Usage panel renders skeleton-first. Wait ~3s before reading, then call `mcp__Claude_in_Chrome__get_page_text` (or `read_page`) for the tabId.

5. **Parse the two timer values.** Anchor on labels, not positional indices:

   - **Session window:** find the line beginning with "Current session" (case-insensitive). The same paragraph contains "Resets in X hr Y min" (or "Resets in X min"). Compute `session_reset_at = now_utc + timedelta(hours=X, minutes=Y)`. Format as `YYYY-MM-DDTHH:MM:SSZ`.

   - **Weekly window:** find the line containing "Weekly limits" or "All models". Within that block the reset text appears in ONE of two shapes -- the Anthropic UI flips between them without a deterministic preference (both observed 2026-05-17 within a few hours on the same page). Handle both:

     - **Absolute shape:** "Resets `<Weekday>` `<H>`:`<MM>` `<AM|PM>`" (e.g. "Resets Tue 6:00 PM"). Compute the next occurrence of that weekday + time in the operator's local timezone, then format as ISO 8601 with the local UTC offset suffix (e.g. `2026-05-19T18:00:00-04:00`). Use the offset, NOT `Z`, since this anchors to local-clock wall time.

     - **Relative shape:** "Resets in `<X>` hr `<Y>` min" (e.g. "Resets in 13 hr 11 min"). Same pattern as the session window. Compute `weekly_reset_at = now_utc + timedelta(hours=X, minutes=Y)`. Format as `YYYY-MM-DDTHH:MM:SSZ` (UTC Z-suffix).

     If the weekly block exists but matches NEITHER pattern: fail loud. Print exactly:
     ```
     [WEEKLY-PARSE-ERROR] Block found but matches no known pattern.
       Known patterns:
         - "Resets <Weekday> <H>:<MM> <AM|PM>"
         - "Resets in <X> hr <Y> min"
       Raw text: <verbatim block content>
       File a follow-up to AR-S3k-PARSER (2b05e937) with the new format.
     ```
     Skip the weekly POST. Session POST may still proceed. The verbatim raw text is the only way to add a third parser; don't try to guess.

   If either label (Current session / Weekly limits) is absent from the page entirely, log "[ABSENT]" for that window and skip its POST — partial re-sync is fine, don't fabricate.

6. **POST to the registry, one call per kind.** For each parsed value:

   ```sh
   curl -X POST http://127.0.0.1:5050/api/registry/quota \
        -H 'Content-Type: application/json' \
        -d '{"reset_at": "<ISO>", "window_kind": "session" or "weekly"}'
   ```

   Expect 200 + the full state dict in response. On non-200, log the body + status and continue (don't abort on one bad POST).

7. **Record the last-sync timestamp.** Append a line to `data/last_quota_sync.json` (in Dream's data dir) with `{"timestamp": now_iso, "session": <iso or null>, "weekly": <iso or null>}`. The SessionStart hook reads this to decide whether to re-prompt today.

8. **Summarize.** Print a one-line summary: `synced session=<iso> weekly=<iso>` (or "session=ABSENT" etc.). Show drift if session reset differs from the operator-anchored value by >5 minutes (`old vs new` line).

## Drift handling

If the Chrome read disagrees with the operator value by >5 min: that's a
re-sync correction — the Chrome value wins (it's the live UI). Surface the
delta in the summary so the operator knows their manual entry was slightly
off (clock drift between operator's wall watch and Anthropic's server).

## Failure modes

- Chrome MCP not connected -> log + exit cleanly. Operator value stays
  authoritative.
- claude.ai requires re-auth -> the page will redirect to login. Detect
  via `page.url.includes("/login")` and surface "operator must log in" + exit.
- Both timer labels absent -> page format changed; log + exit cleanly.
  File a follow-up task on agent-controller.
- Registry POST 4xx/5xx -> log body + continue to the other kind.

## When to run

- Daily via the SessionStart hook (auto, when last sync >12h old).
- On-demand: Preston runs `/sync-quota` directly when they suspect drift.
