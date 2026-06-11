# Agent View — subagent panel surfaced into Discord

**Date:** 2026-06-11
**Status:** approved (design reviewed in cl-7)
**Repo:** cc-discord-kit

## Problem

When a Claude Code bot runs multi-subagent work, Discord shows nothing: no
indication that agents spawned, which model they run, how long they've been
going, or what they've consumed. The terminal's bottom-row agent indicators
have no Discord equivalent, so multi-agent jobs look like silence.

## Goal

A live, deterministically-updated panel in the originating Discord channel
that mirrors the Claude Code bottom-row agent view: per-agent status, name,
model, elapsed time, tokens consumed. Plus an on-demand `!agents` snapshot
via the existing passthrough.

No LLM in the update loop — display is driven entirely by hooks and a
poller. (A prior attempt at model-driven agent controls failed on exactly
this; determinism is a hard requirement.)

## Render

Fenced code block (Discord pipe tables don't render; fixed-width alignment
requires monospace):

```
agents · claudsson · 2 running · 1 done · 25.6k tok

  ○  bear-case:capex      sonnet     1m12s    12.3k
  ○  bear-case:hbm        sonnet     1m12s     9.1k
  ●  verify:margin        haiku        31s     4.2k
```

- Header: `agents · <bot name> · <n> running · <n> done · <total> tok`,
  blank line after.
- One row per agent, fixed-width columns: status glyph, label, model
  (short alias), elapsed, tokens.
- Glyphs: `○` running, `●` done, `✗` failed.
- Labels derive from the Agent tool call's `description` (slugified);
  model from the spawned transcript's `message.model` (or tool input
  override); collision-suffix duplicate labels (`:2`).

## Architecture

Four pieces, following the narrate/tool_watcher house pattern.

### 1. Recorder — `hooks/agent_view.py` (hook entry)

- **PreToolUse, matcher `Agent|Task`:** register the spawn in the state
  file: label, agent type, model (if specified), start timestamp, session
  transcript dir, chat_id. On the first registration of a burst, ensure a
  panel exists (see Placement) and spawn the updater if not running.
- **PostToolUse, same matcher:** mark the agent done (or failed, via
  PostToolUseFailure), record final token count.
- chat_id resolution: same per-turn inbound-channel detection narrate
  uses — the panel lands in whichever channel's message triggered the
  turn. Multiple channels driving one bot: the spawning turn owns the
  panel.

### 2. Updater — `agent_view.py --updater` (detached process)

Why a process: hooks fire only on events, and while the main loop is
blocked on a long Agent call nothing fires. The updater is spawned
detached (double-fork / setsid) by the first registration and owns all
mid-flight rendering:

- Every ~5s: tail each registered agent's `subagents/agent-<id>.jsonl`
  for cumulative output tokens and last-activity timestamp; compute
  elapsed; re-render; PATCH the host message. (Discord edits don't
  notify; 5s is far inside edit rate limits.)
- Singleton per session via a pidfile in the state dir; registrations
  from later bursts in the same session reuse the live updater.
- Exit when all registered agents are terminal → apply mode policy
  (below) after a final render.
- Safety: hard timeout (default 2h) after which the panel freezes with a
  `stale` marker and the updater exits; an agent whose transcript stops
  growing for >15m with no PostToolUse is marked `✗ (lost)`.

### 3. Placement & lifecycle

**Primary: panel as a footer section of the live tool-trace message.**
The trace grows by edits, and edits don't change message order — so a
footer composed into every render of the trace placeholder is always at
the visual bottom. When tool_watcher seals an overflowing segment and
rotates to a fresh message, the footer migrates to the new segment;
bottom-following comes free from existing rotation machinery. The shared
narrate state (under `_state_lock`) gains a `agent_panel` field; all
three writers (narrate watch, tool_watcher, updater) compose
`prose + ticker + panel` on every edit.

**Fallback: standalone panel message** when no trace message exists for
the turn (narrate and tools both produce nothing). The updater then also
checks whether anything was posted below the panel and, if displaced,
deletes + reposts it at the bottom (only on displacement, not per tick).

**End-of-burst policy — slaved to the existing per-channel `tools` mode
(`tools.json`), no new config surface:**

| tools mode | live panel | at burst end |
|---|---|---|
| off | none | — |
| collapse | yes | deleted with the trace |
| ticker / diffs / full | yes | freezes into final summary, persists |

Char budget: the footer counts against the host message's 2000-char
limit; the composer truncates panel rows (oldest-done first) before
letting the segment rotate early.

### 4. `!agents` passthrough command

Reserved command in `discord_passthrough.py` (alias `!agent`): reads the
same state file, replies once with the current render in a code block.
Works in every tools mode including `off` — an explicit ask beats
channel policy. No live editing; it's a snapshot.

## State

`~/.local/state/cc-discord-kit/agent_view_state.json`, keyed
`{bot_id}:{session_id}`:

```json
{
  "chat_id": "...",
  "host_msg_id": "...",
  "host_kind": "trace|standalone",
  "updater_pid": 12345,
  "agents": {
    "<agent_id|toolcall_key>": {
      "label": "bear-case:capex",
      "model": "sonnet",
      "started_at": 1760000000.0,
      "status": "running|done|failed",
      "tokens": 12345,
      "transcript": "/path/to/subagents/agent-x.jsonl"
    }
  }
}
```

Written under the same lock discipline as narrate state. Plain JSON,
last-writer-wins, hand-fixable — house style.

## Open verification items (implementation-time)

- Exact PreToolUse payload for the Agent tool (field names for
  description/model/agentType) — verify against a live spawn before
  freezing the recorder's parser.
- Mapping a PreToolUse registration to its `agent-<id>.jsonl` file (id
  isn't known at spawn): match by mtime-window + prompt prefix; confirm
  reliability with parallel bursts.
- Whether SubagentStart/SubagentStop hook events carry enough to replace
  the PreToolUse/PostToolUse pair (they exist in the settings schema);
  use them if richer, but don't block on it.

## Scope

**v1:** Agent-tool subagents, including parallel bursts, on all family
bots once merged (kit module — rolls out with cc-context per the
co-evolution rule). **v2 (explicitly out):** Workflow fleets, background
tasks (TaskCreate), cross-bot aggregate view, controls (pause/kill) —
read-only first.

## Testing

- pytest: renderer (column alignment, truncation, glyph transitions),
  state transitions (register → running → done/failed/lost), mode-policy
  matrix, footer composition + segment-rotation migration.
- Live: spawn 2-3 cheap Haiku subagents from a test session, watch the
  panel through a full burst in each tools mode; kill an updater
  mid-burst and confirm pidfile recovery.
