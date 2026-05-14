# Stream Event schema (AC-S15)

> One-page spec for the real-time agent-observability stream. Every agent
> execution at every level (L4-L8) emits Stream Events through this
> contract. AC-S11 (per-level token telemetry) consumes the persisted log
> rather than re-instrumenting drivers.

## Event envelope

Every event is a JSON object with the following fields. Persisted form is
one JSON object per line (JSONL); over-the-wire SSE form is `data: <json>\n\n`.

| field                | type           | required | notes                                                                       |
| -------------------- | -------------- | -------- | --------------------------------------------------------------------------- |
| `seq`                | int            | yes      | Monotonically increasing per session, starting at 0. Replay-stable.         |
| `timestamp`          | float          | yes      | Unix seconds (UTC). Sub-second precision is preserved.                      |
| `session_id`         | string         | yes      | Stable id for this agent invocation. Filename = `<session_id>.jsonl`.       |
| `parent_session_id`  | string \| null | yes      | Set when this run was spawned by another agent. Null for top-level.         |
| `agent_role`         | string \| null | yes      | Role id from `templates/settings.json` (e.g. `grader`, `coder`).            |
| `level`              | string \| null | yes      | One of `L4`..`L8`. Null only when the recorder runs out-of-band.            |
| `kind`               | string         | yes      | See "Event kinds" below.                                                    |
| `data`               | object         | yes      | Kind-specific payload. May be empty `{}`.                                   |

## Event kinds

Each session produces exactly one `start` event (seq=0) and exactly one
`end` event (highest seq). Everything else is interleaved between them.

### `start`
Context received at the start of the run.
```json
{
  "data": {
    "prompt": "<full role prompt, verbatim>",
    "context": "<full upstream context, verbatim>",
    "engine": "<engine id e.g. claude-sonnet>",
    "extras": { ... }
  }
}
```

### `tool_call`
A tool invocation initiated by the agent.
```json
{
  "data": {
    "call_id": "<engine-supplied id, used to pair with tool_result>",
    "name": "<tool name>",
    "arguments": { ... }
  }
}
```

### `tool_result`
The result returned to the agent. `call_id` MUST match a prior `tool_call`.
```json
{
  "data": {
    "call_id": "<matches tool_call>",
    "ok": true,
    "result": "<string or object>",
    "error": null
  }
}
```

### `reasoning_delta`
Engine-internal scratchpad surfaced by the engine (Claude thinking blocks,
Ollama `<think>` tags, etc.). Appended in order.
```json
{ "data": { "text": "<delta>" } }
```

### `output_delta`
A streamed chunk of the agent's final response. Token-level for streaming
engines, chunk-level otherwise. Concatenating every `output_delta.text` in
seq order reconstructs the final response.
```json
{ "data": { "text": "<delta>" } }
```

### `end`
Terminal event. Always last.
```json
{
  "data": {
    "status": "ok" | "error" | "cancelled",
    "source": "human" | "ai" | "mixed",
    "attribution": { "human": <bytes>, "ai": <bytes> },
    "error": null | "<message>"
  }
}
```

## Persistence

* File per session: `data/observability/<session_id>.jsonl` (configurable
  via `OBSERVABILITY_ROOT` env / constructor arg).
* Append-only. The file is opened with line-buffered writes so live
  consumers see events the moment they're emitted.
* Retention matches the playtest evidence rule (research ef520e40
  amendment): keep last N sessions; older sessions roll off.

## API

* `GET /api/observability/replay/<session_id>` — returns the full event
  log as a JSON array (`Content-Type: application/json`). 404 if the
  session is unknown.
* `GET /api/observability/stream?session_id=<id>` — Server-Sent Events.
  Replays existing events first, then tails live events until the
  session emits `end` (or the client disconnects). Each event is sent
  as `data: <json>\n\n`. The terminal `end` event closes the stream.

## Invariants tested by the unit suite

1. **Schema conformance** — every emitted event matches the envelope; the
   kind is one of the six above; `seq` is monotonic.
2. **Replay round-trip** — `recorder.emit(...)` events written to disk and
   re-read via `ObservabilityStore.replay(session_id)` are byte-for-byte
   equal to the originals.
3. **Claude driver coverage** — the Claude driver emits every event kind
   during a representative streaming run (start, tool_call, tool_result,
   reasoning_delta, output_delta, end).

## Out of scope (AC-S15)

* Dream UI tab (separate task tied to Dream).
* Per-level metrics aggregation — AC-S11 consumes this log.
* Cross-agent dependency graphs — possible follow-on.
