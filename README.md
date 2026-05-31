# cc-discord-kit

**Give your Claude Code agents a shared memory — and watch them work from Discord.**

Two things in one kit:

1. **Shared context** — a memory + journal + persona store that any number of Claude Code agents (across any number of machines) read and write through one CLI. Plain JSON, auditable by hand.
2. **Claude Code → Discord** — a set of hooks that surface a running Claude Code session into a Discord channel: its narration, its tool calls, its turn status — and let you fire commands back at the host from your phone.

So you can step away from the terminal and still see what your agent is doing, what it remembered, and nudge it — all from Discord. Runs on your own box, over LAN or a private tunnel (tailscale). Never the public internet.

---

## What it looks like

Your agent is running a task in the terminal. Here's the same turn, in your Discord channel:

**It reacts to your message as it works** — one emoji tracks the whole turn:

> 👀 → 🔧 → ✅  *(got it → editing → done)*

**It narrates** — the prose it'd normally only print to the terminal shows up live:

> 🧠 ***Narrating…***
> \> Looking at the config now. The timeout's hardcoded — I'll pull it into an env var and drop the debug flag while I'm here.

**It shows its tool calls** — from a one-line ticker up to full diffs:

```diff
+ ● Edit(src/config.py)
  ⎿ [+3, -1]
- DEBUG = True
+ DEBUG = False
+ TIMEOUT = int(os.environ.get("TIMEOUT", 30))
+ ● Bash(npm test)
- ● Bash(npm run deploy) FAILED
```

**And you can talk back** — type a command in the channel, it runs on the host:

> **you:** `!git log --oneline -3`
> **bot:**
> ```
> 5b54edd docs(README): reframe positioning
> 8e98be7 feat(hooks): port tool-trace rework
> 3b62f9f feat: CF Worker + KV backend
> ```

All of it is **opt-in per channel** and **off by default**. Pick how much you want to see — silent, just status emoji, narration, or full diffs.

See [Tool-trace modes](#tool-trace-by-example) for the full mode list, and [The two halves](#the-two-halves) for how it's built.

---

## The two halves

### 1. Shared context store
A JSON-backed store with three tiers — **memories** (durable atomic facts, cap 200), a **journal** (pinned moments, cap 1000, with titles + colored categories), and **files** (shared documents/references/datasets/images the agents can read) — plus a freeform `SHARED.md` rules doc and per-agent **persona** files. Every agent uses it through one CLI — directly on a shared filesystem, or over HTTP against the bundled Flask server when the agent's on another machine. Optional semantic search hooks out to an external [vecgrep](#) service (no embedding model ships here). Last-writer-wins + plain JSON is deliberate: you can read and fix the store with a text editor, and a process dying mid-write can't corrupt it.

### 2. Claude Code, surfaced into Discord
Three independent hooks make a Claude Code turn **legible from a phone** — opt in per agent, per channel:

| Hook | What it surfaces |
| --- | --- |
| **narration** (`narrate.py`) | the agent's between-tool prose, live as a `🧠` blockquote |
| **tool-trace** (`tool_watcher.py`) | the actual tool calls — one-line ticker → full diffs → command output |
| **emoji-state** (`react_hook.py`) | one reaction on your message tracking the turn: `👀` got it → `🔧` working → `✅` done |

Plus a **command path back**: `discord_passthrough.py` lets you type `!ls` or a registered `/deploy` in Discord and have it run on the host, reply inline, and never cost a token. (Permission prompts are mirrored read-only — you still approve in the terminal.)

See [Tool-trace, by example](#tool-trace-by-example) below for what these actually look like in a channel.

---

**Discord isn't load-bearing for the store.** The store/CLI/server/inventory layers don't know Discord exists — they're transport-agnostic. Only the observability + command hooks are Discord-native (reactions, `>>>` blockquotes, the 2000-char pagination guard). Swap substrates → reimplement the hook layer; the store underneath is unchanged.

The store/server layer originated here; the Claude Code hooks were developed alongside it and genericized for this kit.

## What's in here

**The store** (Discord-agnostic — works on its own)
- `store.py` — the JSON memory + journal store. Atomic writes, last-writer-wins. Memories carry an optional `bot` whitelist (share/unshare per agent); journal entries carry an optional `title` + `category` (colored pills). A freeform `SHARED.md` rules doc sits alongside, injected at session start.
- `files_store.py` — a third tier: shared **files** (documents, references, datasets, images, PDFs) the whole set of agents can read. Inline text or on-disk blobs, size-capped, sha256'd, mime-typed.
- `cli.py` — local CLI: `memory`/`journal`/`persona`/`files` × `list|show|add|edit|delete|search`.
- `client.py` — same CLI, but over HTTP to the server (set `CCDK_URL`) so remote agents use it transparently.
- `server.py` — Flask web UI + JSON API. ⌘K palette, editors, markdown, pinning/trash/history/merge, a file browser with inline preview, optional semantic search.
- `personas.py` — where each agent keeps its persona files (configured in `agents.yaml`); auto-commits if they live in a git repo.
- `migrate.py` — one-shot importer: turn a directory of frontmatter markdown notes into memories/journal entries (`--memory-dir` / `--context-dir`).

**The Discord layer**
- `hooks/narrate.py`, `hooks/tool_watcher.py`, `hooks/react_hook.py` — the three observability lanes (narration / tool-trace / emoji-state).
- `hooks/discord_passthrough.py` — run `!cmd` / `/cmd` from Discord on the host, reply inline, zero token spend. See `commands/README.md`.
- `hooks/notify_hook.py` — mirror Claude Code permission prompts to Discord (read-only).
- `discord_handler.py` — optional `/mem` + `/journal` slash-command bot.

**Ops**
- `inventory.py` — live read of hooks, crontab, systemd units, launchd agents across hosts (cached 30s, never writes).
- `digest.py` — pull recent channel history for review; optional Gemini summarize.

## Install

```bash
git clone https://github.com/<you>/cc-discord-kit.git
cd cc-discord-kit
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp agents.example.yaml ~/.config/cc-discord-kit/agents.yaml
# Edit agents.yaml to point at your real persona-file paths.
```

Then either run the server:

```bash
python3 server.py
# Open http://127.0.0.1:5005
```

…or the CLI:

```bash
./cli.py memory list
./cli.py memory add "Use direct, no glazing" --type=feedback --name="comm style"
```

## Configuration

All env vars optional unless noted.

| Env var | Read by | Purpose |
| --- | --- | --- |
| `CCDK_DATA_DIR` | `store.py` | dir holding `memories.json` + `journal.json`. Default `~/.local/share/cc-discord-kit/`. |
| `CCDK_AGENTS_FILE` | `personas.py` | path to `agents.yaml`. Default `~/.config/cc-discord-kit/agents.yaml`. |
| `CCDK_URL` | `client.py` | when set, CLI runs in HTTP mode against this base URL instead of touching JSON files locally. |
| `CCDK_HOST` / `CCDK_PORT` | `server.py` | Flask bind. Default `127.0.0.1:5005`. **Don't bind to 0.0.0.0** — this is a personal store, not a public service. |
| `CCDK_URL_PREFIX` | `server.py` | for hosting under a path (e.g. `/cc-discord-kit` behind a reverse proxy). |
| `CCDK_BOT` | `cli.py`, hooks | explicit agent identity. Otherwise auto-detected from `CLAUDE_CONFIG_DIR` last segment, then hostname. |
| `CCDK_DISCORD_TOKEN` | `digest.py`, `discord_handler.py`, `hooks/stop_hook.py` | bot token for the discord side. `stop_hook` uses it to post save/edit/delete confirmation cards back to the originating channel. |
| `CCDK_GUILD_IDS` | `discord_handler.py` | optional CSV of Discord guild IDs for instant per-server slash command sync. Without this, slash commands sync globally (~1hr propagation). |
| `CCDK_DIGEST_CHANNELS` | `digest.py` | comma-separated `name:id` pairs for digest pull. |
| `CCDK_SETTINGS_PATHS` | `inventory.py` | optional CSV of extra Claude Code `settings.json` paths to probe for hook chains. |
| `GEMINI_API_KEY` | `digest.py` | enables the optional auto-summarize button on the digest page. |
| `VECGREP_URL` | `vecgrep_client.py` | optional vecgrep endpoint for semantic search. Default `http://127.0.0.1:8765`. |
| `VECGREP_CORPUS_MEMORIES` / `VECGREP_CORPUS_JOURNAL` | `vecgrep_client.py` | optional corpus names. Default `cc-discord-kit`. |
| `CCDK_OWNER_DISCORD_USER_ID` | `hooks/discord_passthrough.py` | the Discord `user_id` allowed to run `!cmd` and `/cmd` pass-through. Required for the hook to activate (fails closed). Alternatively place the same value in `~/.config/cc-discord-kit/owner_id`. |
| `CCDK_OWNER_ID_FILE` | `hooks/discord_passthrough.py` | override the owner_id file path. Default `~/.config/cc-discord-kit/owner_id`. |
| `CCDK_COMMANDS_DIR` | `hooks/discord_passthrough.py` | where to find `/cmd` registry scripts. Default `<repo>/commands/`. |
| `CCDK_PASSTHROUGH_LOG` | `hooks/discord_passthrough.py` | log file path. Default `~/.local/state/cc-discord-kit/passthrough.log`. |
| `CCDK_SESSION_STATE_FILE` | `hooks/discord_passthrough.py` | live-terminal session state (open pane's screen message + scrollback, per channel). Default `~/.cache/cc-discord-kit/passthrough_term.json`. |

The env file at `~/.config/cc-discord-kit/env` is checked as a fallback for any of the above. Shell-style:

```
CCDK_DISCORD_TOKEN=...
CCDK_DIGEST_CHANNELS=general:111111111111111111,help:222222222222222222
GEMINI_API_KEY=...
```

## CLI

```bash
cc-discord-kit memory list                          # all entries
cc-discord-kit memory list --about user             # filter by subject
cc-discord-kit memory list --type feedback          # filter by type
cc-discord-kit memory show 42
cc-discord-kit memory show 42 --body-only
cc-discord-kit memory add "..." --type project --name "X" --tags a,b --about user

cc-discord-kit journal list
cc-discord-kit journal show 17
cc-discord-kit journal add "..." --actor agent-1 --tags a,b --title "X" --category note
cc-discord-kit journal edit 17 "updated body" --tags a,b --category milestone

cc-discord-kit files list                           # shared files
cc-discord-kit files show 3
cc-discord-kit files add ./notes.md --tags a,b      # text inline, binaries → blob
cc-discord-kit files edit 3 --tags a,b
cc-discord-kit files delete 3
cc-discord-kit files search "term"

cc-discord-kit memory share 42 --with agent-1       # scope a memory to an agent
cc-discord-kit memory unshare 42 --with agent-1

cc-discord-kit persona show agent-1 persona.md      # print file contents
cc-discord-kit persona edit agent-1 persona.md      # opens $EDITOR; saves on exit
cc-discord-kit persona write agent-1 persona.md "<text>"  # write directly
```

Journal `--category` is one of `decision`, `incident`, `milestone`, `moment`, `note`, `general` (the catch-all). Unknown values fall back to `general`.

Set `CCDK_URL=https://your-host:8443/` to run the same commands against a remote server.

## Web UI

`python3 server.py` then open `http://127.0.0.1:5005`. Pages:

| Path | What |
| --- | --- |
| `/` | memories index — search, optional semantic search, filter by type/about/bot, pin/trash |
| `/journal` | journal entries timeline with literal or optional semantic search; title + colored category pills |
| `/files` | shared file browser — colored type pills + hover legend, grid/list views, inline preview (images, syntax-highlighted code, markdown, JSON, CSV tables, PDF, audio/video), and an edit/preview toggle for text files |
| `/personas` | per-agent persona file editor |
| `/digest` | recent Discord channel review (if configured) |
| `/inventory` | live hooks/crons/services across configured hosts |
| `/trash` | soft-deleted records, restore-able |

`⌘K` (mac) / `ctrl+K` (everywhere else) opens the command palette. Filter type-ahead, ↑↓ to navigate, ↵ to fire, esc to close.

## Memory schema

```json
{
  "id": 42,
  "type": "feedback",
  "name": "concise replies",
  "text": "...",
  "tags": ["communication"],
  "about": ["user"],
  "bot": null,
  "ts": "2026-05-01T20:00:00Z"
}
```

- `type` — one of `user`, `feedback`, `project`, `reference`. Used for color coding + filter.
- `name` — short title.
- `text` — the body. Markdown rendered in the web UI.
- `tags` — free-form labels.
- `about` — subjects the entry concerns (e.g. `["user"]`, `["domain-x"]`). Filterable.
- `bot` — if set (e.g. `["agent-1"]`), only that agent includes the entry in default views; others must pass `--all` to see it. Default null = visible to all agents. Manage with `memory share`/`memory unshare`.

Journal entries carry `id, ts, source, actor, text, tags, pinned` plus an optional `title` (short heading) and `category` (one of `decision|incident|milestone|moment|note|general`, rendered as a colored pill).

File records carry `id, ts, name, slug, type, mime, size, sha256, storage, content?/blob_path?, tags, about, bot?, actor`. `storage` is `inline` (text in the JSON) or `blob` (bytes on disk under `<CCDK_DATA_DIR>/files/`). Caps: 100 MB/file, 5 GB total. The web UI serves only provably-inert types inline (raster images, PDF, audio, video) — active types (SVG, HTML) are always forced to download.

## Saving From Agents

Use explicit CLI commands for real writes, especially when the request came from Discord:

```bash
cc-discord-kit memory add \
  --type feedback \
  --name "short title" \
  --tags "tag1,tag2" \
  --about "subject1,subject2" \
  --discord-chat-id "<chat_id>" \
  --discord-message-id "<message_id>" \
  "body text"
```

The Discord flags are optional. When present, the CLI or HTTP API posts a confirmation card back to the originating channel. For terminal-only saves, omit them.

## Hooks (Claude Code agents)

The `hooks/` directory has a full set of Claude Code hooks. Wire any subset into your `settings.json`. Each is independent — adopt only what you need.

### Memory / journal integration (the original set)

- **`session_start_hook.py`** (SessionStart) — injects full feedback memories, an index of other memories, and recent journal entries into context on session boot.
- **`user_prompt_hook.py`** (UserPromptSubmit) — refreshes a compact memory index on each user prompt.
- **`precompact_hook.py`** (PreCompact) — writes a "what was the last conversation about" snapshot before context compaction. Routes through `CCDK_URL` if set, else direct import.
- **`stop_hook.py`** (Stop) — legacy tag-parser save path. **Use CLI commands as the recommended write path** (`cc-discord-kit memory add ...`). The Stop hook is retained for back-compat; see [Legacy save-intent gate](#legacy-save-intent-gate) for the syntax. See `SAVES.md` for the rationale and Discord card flow.

### Discord pass-through + slash dispatch

- **`discord_passthrough.py`** (UserPromptSubmit) — intercepts Discord-origin `!cmd` (raw shell) and `/cmd` (registered slash) messages from the configured owner, runs them on the host, replies directly to Discord, blocks the prompt from reaching the model (zero token spend). See `commands/README.md` for the dispatch contract. Owner check: `CCDK_OWNER_DISCORD_USER_ID` or `~/.config/cc-discord-kit/owner_id`.

  **Live-terminal mode.** Send a bare `!` to open a *terminal screen* — a single pinned Discord message that's PATCHed in place as you run commands, instead of a new reply per command. While the pane is open, each `!cmd` appends to a rolling scrollback (last 25 lines) rendered into that one message, so a channel reads like a real terminal. Close it with `!exit` (or `!q`) — the screen gets a final `Goodbye! 👋` frame. A pane left idle for 30 minutes auto-expires back to one-shot mode (so a forgotten session doesn't keep editing a message scrolled out of view). Session state lives in `CCDK_SESSION_STATE_FILE` (default `~/.cache/cc-discord-kit/passthrough_term.json`).

### Voice surfacing — narrate + tool-watcher

- **`narrate.py`** (PostToolUse `--mode watch` + Stop `--mode finalize`) — surfaces the agent's between-tool prose to Discord. Watcher tails the transcript for new `type:assistant` text blocks and posts/edits a `🧠 *Narrating…*` placeholder in the originating channel. Finalize fires on Stop — mode determines what happens to the placeholder.

  Per-channel mode lives in `<bot_root>/channels/discord/narrate.json`:

  ```json
  { "<chat_id>": "collapse" | "always" | "never" }
  ```

  - **`collapse`** (alias: `auto`) — placeholder posted live, **deleted at Stop** after the real reply lands. Best for fast turns. (Legacy "auto" is migrated on read.)
  - **`always`** — placeholder converted at Stop into a `🧠 **Narration**` quoted block kept **above** the real reply. Persistent, reviewable.
  - **`never`** — no narration. Default.

  Live placeholder uses Discord's `>>>` multi-line blockquote. Triple-backticks in prose are neutralized so they don't break the outer fence. The watcher rotates segments on mid-turn reply landings.

- **`tool_watcher.py`** (PostToolUse) — surfaces tool calls themselves into the same per-turn segment that narrate.py owns. Per-channel mode in `<bot_root>/channels/discord/tools.json`:

  ```json
  { "<chat_id>": "off" | "collapse" | "ticker" | "diffs" | "full" }
  ```

  - **`ticker`** — one-line header per call: `+ ● ToolName(short args)`. The `●` dot marks it as a tool invocation (vs a file-edit `+`/`-` line, which carry no dot). Errored calls render `- ● ToolName(...) FAILED` (red). Color is via a ` ```diff ` fence: Discord renders `+` lines green, `-` lines red — cross-platform. Persists past Stop.
  - **`diffs`** — ticker + a ` ```diff ` unified diff for Edit/Write/MultiEdit, plus a grey summary line under the header: `  ⎿ [+N, -M]` (lines added/removed) for edits, `  ⎿ [N lines]` for Read.
  - **`collapse`** — same as `diffs` while live (ticker + diffs + summaries), then the whole tool message is deleted at Stop. Symmetric with narrate's `collapse` — pair them for full visibility during the turn, clean channel after.
  - **`full`** — diffs + ` ``` ` fenced Bash stdout (secret-stripped).
  - **`off`** — disabled (default).

<a name="tool-trace-by-example"></a>
#### Tool-trace, by example

What actually shows up in the channel as the agent works. Everything renders inside a ` ```diff ` fence so the `+`/`-` coloring works on desktop *and* mobile.

**`ticker`** — one line per tool call, headers only:

```diff
+ ● Read(src/server.py)
+ ● Edit(src/server.py)
+ ● Bash(npm test)
- ● Bash(npm run deploy) FAILED
```

**`diffs`** — same headers, plus a grey summary line and the actual edit diff:

```diff
+ ● Edit(src/config.py)
  ⎿ [+3, -1]
- DEBUG = True
+ DEBUG = False
+ LOG_LEVEL = "info"
+ TIMEOUT = 30
+ ● Read(README.md)
  ⎿ [127 lines]
```

The `●` dot marks a **tool invocation**; bare `+`/`-` lines (no dot) are the **file diff** itself — so a green `+ DEBUG` edit line never gets confused with the green `+ ●` header above it. `collapse` renders identically while the turn runs, then deletes the whole block at Stop for a clean channel.

**`full`** adds the command's stdout below the header (secrets stripped):

```diff
+ ● Bash(git status)
```
```
On branch main
nothing to commit, working tree clean
```

### Discord echo + guardrails

- **`react_hook.py`** — emoji reaction signaller. Called with `--mode received|working|replied|terminal|memorized|compacted|crosscheck|notified` from various Claude Code hook events. State partitioned per-agent so multiple agents sharing a host don't clobber each other. Emoji map:

  | Mode       | Emoji | When                                         |
  |---         |---    |---                                            |
  | received   | 👀    | UserPromptSubmit — agent has the message     |
  | working    | varies | PreToolUse — type of tool (🤔 think, 🔨 edit, 🔍 research, …) |
  | replied    | ✅    | PostToolUse on Discord reply tool            |
  | terminal   | 🖥️    | Stop — Discord-origin turn with no reply / no content react |
  | memorized  | 💾    | Stop — turn wrote a memory/journal entry     |
  | compacted  | 📝    | PreCompact — context was compacted           |
  | crosscheck | 🔀    | PostToolUse on reply tool — chat_id doesn't match any inbound origin (cross-channel leak warning) |
  | notified   | 🔔    | External — `notify_hook` mirrored a system notification |

  Terminal-mode keeps one 🖥️ per channel (sliding-forward). Suppresses 🖥️ when an explicit content react was made (the react IS the response).

- **`discord_echo_guard.py`** (Stop) — blocks turn end (exit 2) when a Discord-origin user message was responded to only in terminal — no reply / react. Forces the model to actually echo to Discord. Passes through when `stop_hook_active=true` so retries don't loop. Cooperates with react_hook's terminal mode to avoid premature 🖥️ stamps.

- **`paginate_guard.py`** (PreToolUse) — rejects Discord `reply` calls whose `text` would auto-paginate a fenced code block. Discord chunks at 2000 chars by character boundary, butchering backticks. The guard tells the model to write the body to `/tmp/<name>.md` and attach instead.

- **`scrub_tags.py`** (PreToolUse) — mutator on `mcp__plugin_discord_discord__reply`. Strips `[MEMORY:...]`, `[MEMORY_EDIT:…]`, `[MEMORY_DELETE:…]`, `[JOURNAL:…]`, `[JOURNAL_DELETE:…]` tags from outbound `text` so they don't leak visibly into Discord. Stop hook still captures the tags from the transcript.

- **`discord_mention_resolver.py`** (UserPromptSubmit) — resolves `<@USER_ID>` mentions in inbound Discord messages to human-readable names. Roster loaded from `~/.config/cc-discord-kit/discord_roster.json` (or `CCDK_DISCORD_ROSTER`). The running agent's own ID comes from `CCDK_BOT_DISCORD_USER_ID`. Injects a `Discord mentions resolved:` block; adds an explicit warning when this agent was addressed.

### Lifecycle + system

- **`inject_time.py`** (UserPromptSubmit) — injects a one-line wall-clock stamp on every prompt. Compensates for stale `currentDate` in long-running sessions.
- **`notify_hook.py`** (Notification) — mirrors Claude Code system notifications (permission prompts, elicitation dialogs) to Discord. Target channel via `NOTIFY_CHANNEL_ID` env, else the most recent Discord-origin chat. Best-effort drops a 🔔 reaction via `react_hook --mode notified`.

### Env vars (per-hook overrides)

All log + state paths default under `~/.local/state/cc-discord-kit/`. Override individually:

| Var | Hook | What |
|---|---|---|
| `CCDK_REACT_HOOK_LOG` / `CCDK_REACT_HOOK_STATE` | react_hook | log + state paths |
| `CCDK_NARRATE_LOG` / `CCDK_NARRATE_STATE` | narrate | log + state paths |
| `CCDK_TOOL_WATCHER_LOG` | tool_watcher | log path |
| `CCDK_ECHO_GUARD_LOG` | discord_echo_guard | log path |
| `CCDK_PAGINATE_GUARD_LOG` / `CCDK_PAGINATE_GUARD_LIMIT` | paginate_guard | log path + char limit (default 1900) |
| `CCDK_SCRUB_TAGS_LOG` | scrub_tags | log path |
| `CCDK_NOTIFY_HOOK_LOG` | notify_hook | log path |
| `CCDK_STOP_HOOK_LOG` | react_hook (memorized mode) | stop-hook log path to scan for 💾 trigger |
| `CCDK_REACT_HOOK_BIN` | notify_hook | path to react_hook entrypoint for `--mode notified` |
| `CCDK_DISCORD_ROSTER` | discord_mention_resolver | path to user_id → name JSON |
| `CCDK_BOT_DISCORD_USER_ID` | discord_mention_resolver | running agent's own Discord user_id |
| `DISCORD_STATE_DIR` | several | per-agent Discord plugin state dir override |

### Legacy save-intent gate

The Stop hook only fires tag handlers when one of the user's last 5 messages contains a save-intent verb (`remember`, `save`, `memory`, `forget`, `delete`, `remove`, `nuke`, `edit`, `note`, `remind`, `journal`, `pin`, `stash`, `memo`). The 5-message window catches multi-turn save flows — e.g. user says "save our address" in turn N, replies with the actual address in turn N+1, assistant emits `[MEMORY:]` in response to N+1 — without it, the gate would scan only the address-only message and silently block.

This prevents meta-discussion of the tag syntax from triggering real writes. To talk *about* the tags without firing them, use the `[MEMORY-EXAMPLE: ...]` / `[JOURNAL-EXAMPLE: ...]` form — those get stripped before scanning.

### Discord cards

When an explicit CLI/API save includes Discord IDs, the app posts a rendered confirmation card to the same channel as a reply:

```
💾 Memory #42 saved
type: feedback · name: Communication style · tags: comm, voice · about: user

Body text in italics, truncated past 600 chars.
Multi-paragraph bodies render naturally with blank lines between.
```

Cards cover save (`💾`), edit (`✏️`), and delete (`🗑️`) for both memory and journal. The hook reads `DISCORD_BOT_TOKEN` from `CCDK_DISCORD_TOKEN` first, then falls back to `$CLAUDE_PLUGIN_STATE_DIR/.env` and `~/.claude/channels/discord/.env` so the same setup as the rest of your Discord integration works without extra config.

If no Discord origin is in the user message (e.g. the save happened in a terminal session), no card is posted — the CLI's own `Saved #N` output is the confirmation in that case.

## Discord bot

`discord_handler.py` is an optional standalone bot exposing `/mem` and
`/journal` slash commands. To set up:

1. Create a Discord application + bot at <https://discord.com/developers/applications>.
2. Under **OAuth2 → URL Generator**, select scopes `bot` and `applications.commands`. The bot only needs the **default** intents — no Message Content Intent required.
3. Invite the bot to your server with the generated URL.
4. Set `CCDK_DISCORD_TOKEN=<token>` in `~/.config/cc-discord-kit/env`.
5. Optionally set `CCDK_GUILD_IDS=<csv of guild IDs>` for instant slash-command sync (otherwise it's ~1hr global propagation).
6. Run `python3 discord_handler.py` (or enable the systemd unit installed by `install.sh`).

## Tests

```bash
pip install pytest
pytest tests/
```

Tests are fully isolated from your real data dir (`CCDK_DATA_DIR` is
set to a `tmp_path` in `conftest.py`) and do not touch the network.

## Inventory probes

The `/inventory` page uses a transport abstraction to read hook chains, crontab, and service lists from each host. Out of the box:

- `LocalTransport` — runs commands directly on the same host as the server.
- Custom transports — drop a class with `run(cmd, timeout) → (rc, stdout, stderr)` into `inventory.py` to reach other hosts. Common patterns: SSH-with-restricted-`command=` wrapper, `kubectl exec`, `docker exec`.

Source of truth (`settings.json`, `crontab`, `systemd` units) stays in its canonical location. This module just reads.

## License

MIT
