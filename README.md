# cc-discord-kit

**Give your Claude Code agents a shared memory — and watch them work from Discord.**

Two things in one kit:

1. **Shared context** — a memory + journal + persona store that any number of Claude Code agents (across any number of machines) read and write through one CLI. Plain JSON, auditable by hand.
2. **Claude Code → Discord** — a set of hooks that surface a running Claude Code session into a Discord channel: its narration, its tool calls, its turn status — and let you fire commands back at the host from your phone.

So you can step away from the terminal and still see what your agent is doing, what it remembered, and nudge it — all from Discord. Runs on your own box, over LAN or a private tunnel (tailscale). Never the public internet.

---

## Contents

- [What it looks like](#what-it-looks-like)
- [The two halves](#the-two-halves)
- [What's in here](#whats-in-here)
- [Install](#install)
- [Configuration](#configuration)
- [CLI](#cli)
- [Web UI](#web-ui)
- [Memory schema](#memory-schema)
- [Saving from agents](#saving-from-agents)
- [Hooks (Claude Code agents)](#hooks-claude-code-agents)
- [Discord bot](#discord-bot)
- [Spoken voice (TTS / STT)](#spoken-voice-tts--stt)
- [Tests](#tests)
- [Inventory probes](#inventory-probes)
- [License](#license)

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

**It shows its subagents** — when the agent fans work out, a live panel rides the bottom of the tool trace, edited in place every few seconds (top-left spinner pulses per update, each running dot blinks ○⇄◉, circles fill solid as agents land, tokens and elapsed from the real transcripts):

```
◓ agents · mybot · 2 running · 1 done · 16.5k tok

  ◉  research bear case   sonnet  1m12s  12.3k
  ◉  audit api handlers   sonnet  1m12s   3.9k
  ●  verify margin math   haiku     31s   4.2k
```

**And you can talk back** — type a command in the channel, it runs on the host:

> **you:** `!git log --oneline -3`
> **bot:**
> ```
> 5b54edd docs(README): reframe positioning
> 8e98be7 feat(hooks): port tool-trace rework
> 3b62f9f feat: CF Worker + KV backend
> ```

`!agents` works the same way — it replies with a snapshot of the panel above, in any mode, even when live surfacing is off.

**Or open a live terminal pane** — send a bare `!` and one pinned message becomes a real scrollback, PATCHed in place every command instead of a new reply each time:

> **you:** `!`
> **bot:**
> ```
> $ ▏
> ```
> **you:** `!ls`
> **bot:** *(same message, edited)*
> ```
> $ ls
> README.md  cli.py  store.py  hooks/
> $ ▏
> ```

Close it with `!exit` (or `!q`); an idle pane auto-expires after 30 minutes back to one-shot mode.

All of it is **opt-in per channel** and **off by default**. Pick how much you want to see — silent, just status emoji, narration, or full diffs.

See [Tool-trace modes](#tool-trace-by-example) for the full mode list, and [The two halves](#the-two-halves) for how it's built.

---

## The two halves

### 1. Shared context store
A JSON-backed store with three tiers — **memories** (durable atomic facts, cap 200), a **journal** (pinned moments, cap 1000, with optional titles), and **files** (shared documents/references/datasets/images the agents can read) — plus a freeform `SHARED.md` rules doc and per-agent **persona** files. Every agent uses it through one CLI — directly on a shared filesystem, or over HTTP against the bundled Flask server when the agent's on another machine. Optional semantic search hooks out to an external [vecgrep](#) service (no embedding model ships here). Last-writer-wins + plain JSON is deliberate: you can read and fix the store with a text editor, and a process dying mid-write can't corrupt it.

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
- `store.py` — the JSON memory + journal store. Atomic writes, last-writer-wins. Memories carry an optional `bot` whitelist (share/unshare per agent); journal entries carry an optional `title`. A freeform `SHARED.md` rules doc sits alongside, injected at session start.
- `files_store.py` — a third tier: shared **files** (documents, references, datasets, images, PDFs) the whole set of agents can read. Inline text or on-disk blobs, size-capped, sha256'd, mime-typed.
- `cli.py` — local CLI: `memory`/`journal`/`persona`/`files` × `list|show|add|edit|delete|search`, plus `recall` (semantic retrieve over the store; see [docs/retrieval-controller.md](docs/retrieval-controller.md)).
- `client.py` — same CLI, but over HTTP to the server (set `CCDK_URL`) so remote agents use it transparently.
- `server.py` — Flask web UI + JSON API. ⌘K palette, editors, markdown, pinning/trash/history/merge, a file browser with inline preview, optional semantic search.
- `personas.py` — where each agent keeps its persona files (configured in `agents.yaml`); auto-commits if they live in a git repo.
- `migrate.py` — one-shot importer: turn a directory of frontmatter markdown notes into memories/journal entries (`--memory-dir` / `--context-dir`).

**The Discord layer**
- `hooks/narrate.py`, `hooks/tool_watcher.py`, `hooks/react_hook.py` — the three observability lanes (narration / tool-trace / emoji-state).
- `hooks/discord_passthrough.py` — run `!cmd` / `/cmd` from Discord on the host, reply inline, zero token spend. See `commands/README.md`.
- `hooks/notify_hook.py` — mirror Claude Code permission prompts to Discord (read-only).
- `discord_handler.py` — optional slash-command bot: `/mem`, `/journal`, `/persona`, `/bot`, `/squad`, `/vecgrep`.

**Fleet management** (optional — for running several agents off one `agents.yaml`)
- `bot_config.py` — single source of truth: reads `agents.yaml` and resolves which agent a given Claude Code session is (by config dir), with per-agent fields (kind, host, home channel, access.json path).
- `bot_admin.py` — toggle per-channel Discord flags (requireMention / narrate / tool-watcher / allowed) for any agent in the registry; backs the `/bot` slash surface.
- `bots_doctor.py` — validate every agent in `agents.yaml` against reality (persona files present, hooks wired, unit alive) and report problems.
- `capabilities.py` — a capability matrix each agent self-reports into, so you can spot drift (one agent missing a hook — or an MCP-delivered capability like `browser` (the Playwright computer-use MCP) — the others have). Note: the matrix *tracks* which agents have a capability; it doesn't ship the implementation (e.g. the browser tooling itself lives outside this kit).
- `new_bot.py` — scaffold a new agent: emits its `settings.json` (the kit's hook set), an `agents.yaml` entry, a launcher, and a presence file.
- `facts.py` — a tiny key→value store for reusable literals (ports, IDs, paths) agents should look up rather than hallucinate.

**Ops**
- `inventory.py` — live read of hooks, crontab, systemd units, launchd agents across hosts (cached 30s, never writes).
- `shot.py` — Playwright screenshot helper for visually verifying the web UI (`CCDK_HOST`/`CCDK_PORT`).
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
| `CCDK_PRELOAD_BUDGET` | `hooks/session_start_hook.py` | token budget for the full-body memory preload at session start. `0` (default) = load every memory body; a positive value caps it (newest-first), the rest stay index-only and are reached via `recall`. See [docs/retrieval-controller.md](docs/retrieval-controller.md). |
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
| `CCDK_RELAY_HELPER_ID` | `choice_card.py` | Discord user_id of the central helper bot that handles card taps. **Required for tap-to-act** — fail-closed if unset (no helper ⇒ relayed taps aren't trusted). |
| `CCDK_HELPER_DISCORD_TOKEN` | `discord_card.py` | the helper bot's Discord token — cards are posted with it so the helper can edit them in place (cross-bot edits 403). Env or in `~/.config/cc-discord-kit/env`. |
| `CCDK_ALLOWED_TAPPERS` | `vecgrep_confirm.py` | extra Discord user_ids (comma-separated) allowed to tap choice/veto/todo cards, beyond the owner. Vecgrep write-confirms stay owner-only. |
| `CCDK_RELAY_CONFIG` / `CCDK_RELAY_SECRET` | `choice_card.py` | relay config + HMAC secret paths. Defaults `~/.config/cc-discord-kit/relay.json` + `relay_secret` (secret auto-generated on first use). |
| `CCDK_VECGREP_CONFIRM_CHANNEL` | `vecgrep_confirm.py` | the only channel a vecgrep write-confirm tap is honored in (owner's private channel). Fail-closed if unset. |
| `CCDK_PLUGIN_DIRS` | `hooks/discord_plugin_patch.py` | extra config dirs (comma-separated, relative to `$HOME`) whose plugin copy to patch, for multi-agent hosts. Default: `CLAUDE_CONFIG_DIR` + `~/.claude`. |
| `CCDK_THINK_SHOW_SEC` | `hooks/narrate.py` | ceiling (seconds) before the thinking indicator shows on a silent turn whose first output hasn't flushed. Default 6. |

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
cc-discord-kit journal add "..." --actor agent-1 --tags a,b --title "X"
cc-discord-kit journal edit 17 "updated body" --tags a,b --title "X"

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

# Fleet management (needs agents.yaml + PyYAML)
cc-discord-kit bots list                            # agents in the registry
cc-discord-kit bots doctor                          # validate each against reality
cc-discord-kit capabilities report                  # this agent self-reports its hooks/features
cc-discord-kit capabilities show [agent]            # the capability matrix
cc-discord-kit capabilities drift                   # who's missing what
cc-discord-kit fact set <key> <value> [--note ...]  # reusable literal store
cc-discord-kit fact get <key>
cc-discord-kit fact list
cc-discord-kit fact search <term>
cc-discord-kit fact delete <key>
```


Set `CCDK_URL=https://your-host:8443/` to run the same commands against a remote server.

## Web UI

`python3 server.py` then open `http://127.0.0.1:5005`. Pages:

| Path | What |
| --- | --- |
| `/` | memories index — search, optional semantic search, filter by type/about/bot, pin/trash |
| `/journal` | journal entries timeline with literal or optional semantic search; optional title |
| `/files` | shared file browser — colored type pills + hover legend, grid/list views, inline preview (images, syntax-highlighted code, markdown, JSON, CSV tables, PDF, audio/video), and an edit/preview toggle for text files |
| `/context` | edit `SHARED.md` (the global rules doc injected at session start) + per-agent brain-file (CLAUDE.md) cards |
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

Journal entries carry `id, ts, source, actor, text, tags, pinned` plus an optional `title` (short heading).

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

- **`discord_passthrough.py`** (UserPromptSubmit) — intercepts Discord-origin `!cmd` (raw shell) and `/cmd` (registered slash) messages from the configured owner, runs them on the host, replies directly to Discord, blocks the prompt from reaching the model (zero token spend). See `commands/README.md` for the dispatch contract. Owner check: `CCDK_OWNER_DISCORD_USER_ID` or `~/.config/cc-discord-kit/owner_id`. `!help` prints the command reference; `!agents help` prints the agent-view reference.

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

  **Threads inherit the parent.** A Discord thread is a channel with its own id, but `narrate.json` / `tools.json` are keyed by the parent channel — so a thread with no entry of its own inherits the parent's mode (set it once on the channel, every thread under it follows). Set a mode on a thread's own id to override. The thread→parent lookup is one cached Discord API call per channel id, ever.

- **`agent_view.py`** (PreToolUse / PostToolUse / PostToolUseFailure / Stop) — the **subagent panel**. PreToolUse on `Agent|Task` registers the spawn and forks a detached poller that tails each subagent's transcript (`<session>/subagents/agent-*.jsonl`) for tokens, model, and liveness, live-editing the panel every `CCDK_AGENT_VIEW_TICK` seconds (default 5) until every agent lands. Fully deterministic — hooks and a poller, no model in the display loop.

  The panel renders as a **footer on the live tool-trace message**: Discord edits don't reorder messages, so it stays pinned at the visual bottom and migrates automatically when the trace rotates to a fresh segment. No trace to ride (quiet channel)? It posts standalone and reposts itself below anything that displaces it. Lifecycle follows the channel's `tools` mode — `off` no panel, `collapse` deleted with the trace at Stop, `ticker`/`diffs`/`full` frozen in place as the final summary.

  Registry lives in `~/.local/state/cc-discord-kit/agent_view_state.json`; a silent running agent is marked lost after 15 min; the poller self-destructs after 2 h with a `stale` marker. `!agents` / `!agent` in any channel replies with a one-shot snapshot (reserved command — never hits the shell, works even in `off` mode). Subcommands: `!agents clear` drops finished rows and keeps runners, `!agents clear all` wipes the lot, `!agents help` prints the reference. Clear is view-only — it stops *tracking* agents, it never kills a running subagent.

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

<a name="agent-panel-by-example"></a>
#### Agent panel, by example

When the session fans work out to subagents, the panel appears as a footer
on the live tool-trace message and re-renders every ~5 seconds.

**Mid-burst** — the top-left spinner advances one frame per update
(◐ ◓ ◑ ◒) and each running task's dot pulses ○⇄◉ in step, so you can tell
at a glance the panel is alive, not frozen. Two consecutive frames:

```diff
+ ● Agent({"description":"research bear case","prompt":"Research the…)
+ ● Agent({"description":"audit api handlers","prompt":"Read every…)
+ ● Agent({"description":"verify margin math","prompt":"Re-derive…)
```
```
◑ agents · mybot · 2 running · 1 done · 16.5k tok

  ○  research bear case   sonnet  1m12s  12.3k
  ○  audit api handlers   sonnet  1m12s   3.9k
  ●  verify margin math   haiku     31s   4.2k
```
```
◒ agents · mybot · 2 running · 1 done · 16.7k tok

  ◉  research bear case   sonnet  1m14s  12.4k
  ◉  audit api handlers   sonnet  1m14s   3.9k
  ●  verify margin math   haiku     31s   4.2k
```

**Burst complete** — the spinner settles to a solid ●, every dot fills,
the final summary freezes in place (`ticker`/`diffs`/`full` modes) or
vanishes with the trace (`collapse`):

```
● agents · mybot · 0 running · 3 done · 31.8k tok

  ●  research bear case   sonnet  4m07s  21.4k
  ●  audit api handlers   sonnet  3m44s   6.2k
  ●  verify margin math   haiku     31s   4.2k
```

**Something went wrong** — a failed call renders `✗`, and an agent whose
transcript goes silent for 15 minutes is marked lost rather than spinning
forever:

```
◒ agents · mybot · 1 running · 2 done · 9.1k tok

  ◉  research bear case   sonnet  2m02s   7.7k
  ✗  audit api handlers   sonnet    44s   1.4k
  ✗  verify margin math   haiku    15m0s     0
```

**On demand** — `!agents` replies with a snapshot of the same panel, in
any channel, any mode. A snapshot can't animate, so the status dot sits
static: ◉ while anything's live, ● once everything's done:

> **you:** `!agents`
> **bot:**
> ```
> ◉ agents · mybot · 1 running · 2 done · 28.7k tok
>
>   ◉  research bear case   sonnet  3m21s  18.3k
>   ●  audit api handlers   sonnet  3m44s   6.2k
>   ●  verify margin math   haiku     31s   4.2k
> ```

**Reset** — `!agents clear` drops the finished rows once a burst has
landed (`clear all` wipes runners too). View-only: the subagents keep
running, they just leave the panel.

> **you:** `!agents clear`
> **bot:** cleared — 2 finished dropped, 1 running kept

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

### Tap-to-act: one-tap controls from Discord

Cards aren't just read-only confirmations — the owner can **tap** them to drive the agent. Built + verified 2026-06-20.

- **The trusted relay (load-bearing).** The Claude Code Discord plugin drops every bot-authored message, so a reaction the helper bot hears can't reach the asking agent's `--channels` session. The HMAC-signed plugin patch (`hooks/discord_plugin_patch.py`) opens the filter only for the helper's signed `⟦vc-relay:<hex>⟧` messages → delivered to the session as a normal prompt. **Without this, every tap dead-ends.** The helper signs via `choice_card.deliver_pick()` against a shared secret (`~/.config/cc-discord-kit/relay_secret`, auto-generated). The helper bot id must be set in `CCDK_RELAY_HELPER_ID` — fail-closed if unset (no helper ⇒ no relayed taps are trusted).
- **Inline-collapse (every card).** A tap **collapses its outcome into the card in place** — the prompt/hint row inside the code block is replaced with the result (e.g. `✅ approved`, `↩️ backed out`) — instead of posting a separate reply. Shared `_settle_card_inline` drives this across choice, veto, todo, and vecgrep cards. One card, one final state.
- **Veto cards** — every save/edit/delete card carries ✅ keep / ❌. ❌ **rejects** a save (hard-delete + free the id), **undoes** an edit (revert to the before-snapshot), or **undoes** a delete (restore). 1h window; the card sticky-bumps to the channel bottom so it stays visible; the decision collapses inline. So bots can write liberally — a bad write is one tap from reversal. (`memory_veto.py`)
- **Choice cards** — `cc-discord-kit choice ask "<q>" "<opt1>" "<opt2>" …` posts a numbered tap card; the owner taps a number and the pick is relayed back into the session. Two escape taps: ✏️ *type something* (free-text your own answer) and ❌ *back out*. The channel-safe replacement for `AskUserQuestion` (which the `askuser_guard` PreToolUse hook hard-blocks in bot sessions, handing back the exact `choice ask` command).
- **Todo cards** — `cc-discord-kit todo add "<text>"` posts an actionable card: ✅ keep (acknowledge, stays active), 🚫 cancel (resolve), ⭐ flag (toggle starred). (`todo_card.py`)
- **Who can tap.** Cards are owner-only by default (`CCDK_OWNER_DISCORD_USER_ID`). Add more tappers for choice/veto/todo cards via `CCDK_ALLOWED_TAPPERS` (comma-separated user IDs). The vecgrep write-confirm card stays owner-only regardless — it's gated by its own confirm-channel wall, so the allowlist never widens write-confirms.
- **Interrupt + retry** — send a lone `❌` message to stop the bot's current turn (the patched plugin writes the stop flag the stop-check hook reads); tap 🔁 on the "🛑 Stopped" reply to relay a retry.
- **Plugin-mod durability** — `hooks/discord_plugin_patch.py` keeps every local plugin edit (the relay, presence, etc.) alive across plugin updates: idempotent anchor-based fixes re-applied each `SessionStart`, with a self-test that screams (stderr + a sentinel JSON) if a marker goes missing. Targets the agent's `CLAUDE_CONFIG_DIR` + `~/.claude` plugin copy by default; multi-agent hosts can list extra dirs via `CCDK_PLUGIN_DIRS`.

### Thinking indicator + tool surfacing

Beyond the cards, the kit surfaces a running session live (`hooks/narrate.py`, `hooks/react_hook.py`, `hooks/tool_watcher.py`):

- **Working react** — one emoji on the inbound message tracks the turn: 👀 received → 🔧/🌐/🤖 working → ✅ replied.
- **Narration** — the agent's between-tool prose, surfaced as a `🧠 Narrating…` block (per-channel `narrate` mode: `off`/`collapse`/`always`).
- **Tool trace** — tool calls as a one-line ticker up to full diffs (per-channel `tools` mode).
- **Thinking indicator** — a standalone `🧠 ✻ Thinking…` message (animated, escalating) that settles to `🧠 ✓ Thought for Ns`. Shows whenever a turn does real work or reasoning (a thinking block **or** a real tool call) — decoupled from the extended-thinking toggle. The "Thought for Ns" is real think-only time (excludes tool execution). Anchored above the tool trace + reply via the first tool's PreToolUse. Spawned by `react_hook` when extended thinking is engaged; lives in `narrate.py`'s `run_think_updater`.

## Discord bot

`discord_handler.py` is an optional standalone bot exposing these slash
command groups:

- **`/mem`** — `list`/`show`/`add`/`search`/`edit`/`pin`/`delete`/`retag`/`reabout`/`dupes`, plus `trash`/`restore` (delete is a recoverable soft-delete).
- **`/journal`** — `list`/`show`/`add`/`search`/`pin`/`delete`.
- **`/persona`** — `list`/`show`/`edit` agent persona files.
- **`/bot`** — `list`/`info`/`set`/`toggle`/`narrate`/`tools`/`doctor`: manage per-channel flags for the agents in `agents.yaml`. Admin-gated via `CCDK_ADMIN_ID`.
- **`/squad`** — `status`/`services`/`restart`/`logs`/`presence`: systemd ops over the units in `CCDK_SERVICES` (a `unit:label` CSV; empty by default).
- **`/vecgrep`** — semantic search over your corpora (needs `vecgrep_client.py` wired; extra corpora via `CCDK_VECGREP_CORPORA`).

To set up:

1. Create a Discord application + bot at <https://discord.com/developers/applications>.
2. Under **OAuth2 → URL Generator**, select scopes `bot` and `applications.commands`. The bot only needs the **default** intents — no Message Content Intent required.
3. Invite the bot to your server with the generated URL.
4. Set `CCDK_DISCORD_TOKEN=<token>` in `~/.config/cc-discord-kit/env`.
5. Optionally set `CCDK_GUILD_IDS=<csv of guild IDs>` for instant slash-command sync (otherwise it's ~1hr global propagation). For the fleet commands, also set `CCDK_ADMIN_ID` (your Discord user id) and populate `agents.yaml`.
6. Run `python3 discord_handler.py` (or enable the systemd unit installed by `install.sh`).

## Spoken voice (TTS / STT)

Audible voice — the agent *speaking* short replies into a Discord voice channel, and/or transcribing what you say back — is doable, but deliberately **not bundled here**. It's a different shape from everything above: it needs a voice-capable Discord client (the Claude Code plugin is text-only), a TTS provider (typically a paid API), and ffmpeg/opus for audio. That's a heavy, mostly-paid dependency most users of a text-bot kit won't want, so it lives outside the kit.

The working pattern, if you want it, is a small **standalone companion process** (its own repo) that:

1. joins a Discord voice channel and exposes a one-shot CLI — `say <text-channel-id> "<short reply>"` streams TTS audio into the paired voice channel (~3s to first audio with a streaming model);
2. is gated by an explicit *voice-mode* toggle — a small JSON config pairing a text channel ↔ a voice channel, with an allowlist and a master on/off switch. No always-on presence polling (a per-reply "is anyone in the VC" gateway check is more annoying than it's worth);
3. is driven by a line in your `SHARED.md` rules doc, **not a hook**: *"while voice mode is on, run `say` with a 1–3 sentence version of your reply, then echo that same short text to the channel; on error or empty playback, fall back to a normal text reply."*

So the integration is a **rules-doc protocol plus an external CLI call** — which is exactly why there's no `hooks/voice.py`. It isn't hook-shaped, and forcing it into the hook layer would only drag a paid API and a second Discord client into a text/observability kit. Keep the voice helper as its own repo; the two compose cleanly without coupling.

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
