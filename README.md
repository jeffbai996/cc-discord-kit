# cc-discord-kit

> **⚠️ Archived.** This repo is a snapshot of an early self-hosted multi-agent system — shared memory + Claude Code sessions surfaced into Discord. Active development has moved to **[claude-code-discord](https://github.com/jeffbai996/claude-code-discord)**, which extracts and sharpens the best part of this kit — the live Claude-Code-into-Discord observability layer (narration, tool-trace diffs, turn-state, the subagent panel) — into a small, one-command tool without the household-specific plumbing.
>
> This repo stays up as a **design + demo record**: the walkthrough below shows what the observability looks like in a real channel, and the architecture notes document how the whole system fit together. The runnable source was removed because it had drifted from the private repo it mirrored; it remains in this repo's git history.

**Give your Claude Code agents a shared memory — and watch them work from Discord.**

Two things in one system:

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

**Or open a live terminal pane** — send a bare `!` and one pinned message becomes a real scrollback, PATCHed in place every command instead of a new reply each time. Close it with `!exit`; an idle pane auto-expires after 30 minutes.

All of it is **opt-in per channel** and **off by default**. Pick how much you want to see — silent, just status emoji, narration, or full diffs.

---

## The two halves

### 1. Shared context store
A JSON-backed store with three tiers — **memories** (durable atomic facts), a **journal** (pinned moments, with optional titles), and **files** (shared documents/references/datasets/images the agents can read) — plus a freeform rules doc and per-agent **persona** files. Every agent uses it through one CLI — directly on a shared filesystem, or over HTTP against a Flask server when the agent's on another machine. Optional semantic search hooks out to an external [vecgrep](https://github.com/jeffbai996/vecgrep) service (no embedding model ships here). Last-writer-wins + plain JSON is deliberate: you can read and fix the store with a text editor, and a process dying mid-write can't corrupt it.

### 2. Claude Code, surfaced into Discord
Three independent hooks make a Claude Code turn **legible from a phone** — opt in per agent, per channel:

| Hook | What it surfaces |
| --- | --- |
| **narration** | the agent's between-tool prose, live as a `🧠` blockquote |
| **tool-trace** | the actual tool calls — one-line ticker → full diffs → command output |
| **emoji-state** | one reaction on your message tracking the turn: `👀` got it → `🔧` working → `✅` done |

Plus a **command path back**: a pass-through hook lets you type `!ls` or a registered `/deploy` in Discord and have it run on the host, reply inline, and never cost a token. (Permission prompts are mirrored read-only — you still approve in the terminal.)

**Discord isn't load-bearing for the store.** The store / CLI / server / inventory layers don't know Discord exists — they're transport-agnostic. Only the observability + command hooks are Discord-native (reactions, `>>>` blockquotes, a 2000-char pagination guard). Swap substrates → reimplement the hook layer; the store underneath is unchanged.

---

## Architecture

**The store** (Discord-agnostic — works on its own)
- A JSON memory + journal store with atomic, last-writer-wins writes. Memories carry an optional `bot` whitelist (share/unshare per agent); journal entries carry an optional title; a freeform rules doc sits alongside, injected at session start.
- A **files** tier: shared documents, references, datasets, images, PDFs the whole set of agents can read — inline text or on-disk blobs, size-capped, sha256'd, mime-typed.
- A **local CLI** (`memory`/`journal`/`persona`/`files` × `list|show|add|edit|delete|search`, plus a semantic `recall` — see [docs/retrieval-controller.md](docs/retrieval-controller.md)), with an HTTP client so remote agents use the same commands transparently.
- A **Flask web UI + JSON API**: ⌘K palette, editors, markdown, pinning/trash/history/merge, a file browser with inline preview, optional semantic search.

**The Discord layer**
- Three observability lanes (narration / tool-trace / emoji-state) that surface a live Claude Code turn into a channel.
- A pass-through hook to run `!cmd` / `/cmd` from Discord on the host, reply inline, zero token spend.
- A permission-prompt mirror (read-only), and an optional slash-command bot (`/mem`, `/journal`, `/persona`, `/bot`, `/squad`).

**Fleet management** (for running several agents off one registry)
- A single-source-of-truth config resolving which agent a given Claude Code session is (by config dir), with per-agent fields (kind, host, home channel, access policy).
- Per-channel flag toggles (requireMention / narrate / tool-watcher / allowed), a doctor that validates every agent against reality (persona files, hooks wired, unit alive), a self-reported **capability matrix** to spot drift, and a scaffolder for standing up a new agent.

**Ops**
- Live read of hooks, crontab, systemd units, launchd agents across hosts (cached, never writes); a screenshot helper for verifying the web UI; a channel-history digest with optional summarization.

See [docs/](docs/) for design writeups — including the [retrieval controller](docs/retrieval-controller.md) and the [agent-view panel design](docs/specs/2026-06-11-agent-view-design.md).

---

## Operator — a Computer-Using Agent on top of the kit

[**Operator**](https://github.com/jeffbai996/operator) is a companion project built on this system: a live browser/computer-use cockpit where you watch an agent drive a real browser in real time, steer it yourself, or hand it the wheel. The agents are the same subscription-backed Claude/GPT sessions this system already manages — Operator just adds a watch-and-steer surface in front of them.

![Operator driving a browser](docs/img/operator-geoguessr.jpeg)
<sub><i>Operator's GPT agent reasoning through a live GeoGuessr round — left: the interleaved thinking + action trace (Browsing / Reading / Clicking) with a live status card; right: the actual browser it's driving, streamed frame-by-frame.</i></sub>

---

## License

MIT — see [LICENSE](LICENSE).
