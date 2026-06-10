"""Standalone Discord bot exposing cc-discord-kit as slash commands.

Zero Claude tokens — this bot owns its own connection, calls store.py
directly, and replies in <500ms. Runs as a systemd user service.

Slash commands:
  /mem list  [q] [type] [about] [bot] [show_all]
  /mem show  <id>
  /mem add   <text> [type] [name] [tags] [about] [bot]
  /mem edit  <id> <text>
  /mem retag <id> <tags>     /mem reabout <id> <about>
  /mem pin   <id>
  /mem delete <id>           /mem trash [limit]   /mem restore <id>
  /mem dupes <id> [limit]
  /mem search <term>
  /journal list [days]       /journal show <id>
  /journal add  <text> [actor] [tags]
  /journal pin <id>          /journal search <term>   /journal delete <id>
  /bot list|info|set|toggle|narrate|tools|doctor
  /persona list|show|edit
  /squad status|services|restart|logs|presence
  /vecgrep <query> [limit] [corpus] [kind]
  /help

Env vars (loaded from $HOME/.config/cc-discord-kit/env):
  CCDK_DISCORD_TOKEN       — bot token
  CCDK_GUILD_IDS           — optional CSV of guild IDs for instant per-server
                             command sync. Without this, slash commands sync
                             globally (~1hr propagation).
  CCDK_DATA_DIR            — passed through to store.py (data files location)
  CCDK_ADMIN_ID            — Discord user id allowed to run admin-gated
                             commands (/bot, /persona edit, /squad restart,
                             /squad presence). Unset → those commands no-op
                             with a permission-denied message. Keeps any user
                             ID out of source.
  CCDK_TOGGLE_ALLOWED_IDS  — optional CSV of extra user ids allowed to run the
                             benign /bot toggle|narrate|tools subcommands
                             without full admin rights.
  CCDK_SERVICES            — optional CSV of `unit:label` pairs the /squad
                             services|status|restart|logs commands may operate
                             on. Empty default → those commands report "no
                             services configured" and stay safe to import.
  CCDK_VECGREP_CORPORA     — optional CSV of `value:description` pairs for the
                             /vecgrep corpus picker. Empty default → a single
                             corpus matching vecgrep_client.VECGREP_CORPUS_MEMORIES.
"""

from __future__ import annotations

import logging
import os
import sys

import discord
from discord import app_commands

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
import bot_admin  # noqa: E402
import bots_doctor  # noqa: E402
import history  # noqa: E402
import personas  # noqa: E402
import vecgrep_client  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ccdk-discord")

# ─────────────────────────── env loading ───────────────────────────
#
# Same lazy env-file convention as digest.py: read from os.environ first,
# then fall back to ~/.config/cc-discord-kit/env. The module must import
# cleanly with no config file present, so every lookup has a safe default.

ENV_FILE = os.path.expanduser("~/.config/cc-discord-kit/env")


def _read_env_var(name: str) -> str | None:
    """Look up `name` in os.environ, then in the env file."""
    if v := os.environ.get(name):
        return v
    if not os.path.exists(ENV_FILE):
        return None
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{name}="):
                    val = line.split("=", 1)[1].strip()
                    return val.strip('"').strip("'")
    except OSError:
        return None
    return None


TOKEN = _read_env_var("CCDK_DISCORD_TOKEN") or ""
GUILD_IDS_RAW = _read_env_var("CCDK_GUILD_IDS") or ""
GUILD_IDS = [int(g.strip()) for g in GUILD_IDS_RAW.split(",") if g.strip().isdigit()]

# Admin gate — /bot, /persona edit, /squad restart|presence are admin-only.
# Without CCDK_ADMIN_ID set, those commands no-op with a permission-denied
# message. Avoids hardcoding any user ID in source.
ADMIN_ID_RAW = (_read_env_var("CCDK_ADMIN_ID") or "").strip()
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.isdigit() else None

# Per-command allowlists. Comma-separated user IDs. These users can run the
# benign toggle-style subcommands (flip a bool, set a mode) without being the
# full admin — useful for delegating low-risk flags to trusted users.
TOGGLE_ALLOWED_RAW = _read_env_var("CCDK_TOGGLE_ALLOWED_IDS") or ""
TOGGLE_ALLOWED_IDS: set[int] = {
    int(x.strip()) for x in TOGGLE_ALLOWED_RAW.split(",") if x.strip().isdigit()
}


def _load_services() -> list[tuple[str, str]]:
    """Parse `CCDK_SERVICES=unit1:label1,unit2:label2` into (unit, label)
    pairs. Empty / unset → empty list, so /squad ships without baked-in
    systemd unit names and stays safe to import."""
    raw = _read_env_var("CCDK_SERVICES")
    if not raw:
        return []
    out: list[tuple[str, str]] = []
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" in pair:
            unit, label = pair.split(":", 1)
            unit, label = unit.strip(), label.strip()
        else:
            unit, label = pair, pair
        if unit:
            out.append((unit, label or unit))
    return out


# Whitelisted systemd units `/squad` may operate on. Listed via env so slash
# users can't poke at unrelated user units, and so source carries no service
# names. Lazy-loaded at import; empty when CCDK_SERVICES is unset.
_SERVICES: list[tuple[str, str]] = _load_services()
_KNOWN_UNITS: list[str] = [u for u, _ in _SERVICES]


# ─────────────────────────── helpers ───────────────────────────


def _parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _truncate(text: str, limit: int = 1900) -> str:
    """Discord messages cap at 2000. Code-block wrap costs 8 chars."""
    if len(text) <= limit:
        return text
    return text[: limit - 16] + "\n…(truncated)"


def _fmt_memory_list(entries: list[dict]) -> str:
    if not entries:
        return "(no memories)"
    lines = []
    for m in entries:
        ts = (m.get("ts") or "")[:10]
        about = ",".join(m.get("about") or [])
        bot = m.get("bot")
        tag_str = f"about:{about}" if about else ""
        if bot:
            tag_str = f"{tag_str} bot:{','.join(bot)}".strip()
        type_ = m.get("type", "")
        name = m.get("name") or m.get("text", "")[:60]
        if tag_str:
            lines.append(f"#{m['id']:>3} {type_:<9} {name}  ({tag_str})  {ts}")
        else:
            lines.append(f"#{m['id']:>3} {type_:<9} {name}  {ts}")
    return "\n".join(lines)


def _fmt_journal_list(entries: list[dict]) -> str:
    if not entries:
        return "(no journal entries)"
    lines = []
    for e in entries:
        ts = (e.get("ts") or "")[:10]
        actor = e.get("actor") or "-"
        text = (e.get("text") or "").replace("\n", " ")[:80]
        lines.append(f"#{e['id']:>3} {ts} {actor:<12} {text}")
    return "\n".join(lines)


def _fmt_memory_full(m: dict) -> str:
    parts = [
        f"=== Memory #{m['id']} ===",
        f"Type:  {m.get('type', '')}",
        f"Name:  {m.get('name', '')}",
        f"Tags:  {', '.join(m.get('tags', []))}",
    ]
    if m.get("about"):
        parts.append(f"About: {', '.join(m['about'])}")
    if m.get("bot"):
        parts.append(f"Bot:   {', '.join(m['bot'])}")
    parts.append(f"Saved: {m.get('ts', '')}")
    parts.append("")
    parts.append(m.get("text", ""))
    return "\n".join(parts)


def _fmt_journal_full(e: dict) -> str:
    parts = [
        f"=== Journal #{e['id']} ===",
        f"Source: {e.get('source', '')}",
        f"Actor:  {e.get('actor', '')}",
        f"Tags:   {', '.join(e.get('tags', []))}",
        f"Saved:  {e.get('ts', '')}",
        "",
        e.get("text", ""),
    ]
    return "\n".join(parts)


def _wrap(text: str) -> str:
    return f"```\n{_truncate(text)}\n```"


# ─────────────────────────── client + tree ───────────────────────────


class CCDKClient(discord.Client):
    def __init__(self) -> None:
        # Slash commands don't require Message Content Intent — we only need
        # the default intents to receive interactions.
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        if GUILD_IDS:
            # Avoid the duplicate-commands gotcha: a prior boot synced
            # globally, then GUILD_IDS was added and we synced per-guild —
            # Discord then shows both copies because global registrations
            # don't auto-expire. Snapshot the commands, wipe global, restore
            # them to the tree, then sync per-guild. End state: global is
            # empty, each guild has the full set, no duplicates.
            global_cmds = list(self.tree.get_commands(guild=None))
            self.tree.clear_commands(guild=None)
            await self.tree.sync()  # tells Discord to drop globals
            log.info("cleared any stale global slash command registrations")
            for cmd in global_cmds:
                self.tree.add_command(cmd, guild=None)
            for gid in GUILD_IDS:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info("synced commands to guild %s", gid)
        else:
            await self.tree.sync()
            log.info("synced commands globally (~1hr propagation)")


client = CCDKClient()


# ─────────────────────────── /mem command group ───────────────────────────

mem_group = app_commands.Group(name="mem", description="cc-discord-kit memories")


@mem_group.command(name="list", description="list memories with optional filters + search")
@app_commands.describe(
    q="substring to search in body / name / tags / about (case-insensitive)",
    type="filter by type",
    about="comma-separated subject labels (OR semantics)",
    bot="filter to entries scoped to a single bot",
    show_all="include bot-scoped entries from other bots",
)
@app_commands.choices(type=[
    app_commands.Choice(name="user", value="user"),
    app_commands.Choice(name="feedback", value="feedback"),
    app_commands.Choice(name="project", value="project"),
    app_commands.Choice(name="reference", value="reference"),
])
async def mem_list(
    interaction: discord.Interaction,
    q: str | None = None,
    type: app_commands.Choice[str] | None = None,
    about: str | None = None,
    bot: str | None = None,
    show_all: bool = False,
) -> None:
    base = store.search_memories(q) if q else None
    entries = store.filter_memories(
        base,
        type=type.value if type else None,
        about=_parse_csv(about) or None,
        bot=bot,
        show_all=show_all,
    )
    await interaction.response.send_message(_wrap(_fmt_memory_list(entries)))


@mem_group.command(name="show", description="show a memory by id")
async def mem_show(interaction: discord.Interaction, id: int) -> None:
    m = next((x for x in store.load_memories() if x.get("id") == id), None)
    if not m:
        await interaction.response.send_message(f"Memory #{id} not found", ephemeral=True)
        return
    await interaction.response.send_message(_wrap(_fmt_memory_full(m)))


@mem_group.command(name="add", description="save a new memory")
@app_commands.describe(
    text="memory body",
    type="entry type (default feedback)",
    name="short title",
    tags="comma-separated tags",
    about="comma-separated subject labels (e.g. user,project)",
    bot="comma-separated bot scope (leave blank for shared)",
)
@app_commands.choices(type=[
    app_commands.Choice(name="user", value="user"),
    app_commands.Choice(name="feedback", value="feedback"),
    app_commands.Choice(name="project", value="project"),
    app_commands.Choice(name="reference", value="reference"),
])
async def mem_add(
    interaction: discord.Interaction,
    text: str,
    type: app_commands.Choice[str] | None = None,
    name: str | None = None,
    tags: str | None = None,
    about: str | None = None,
    bot: str | None = None,
) -> None:
    bot_list = _parse_csv(bot) if bot else None
    m = store.save_memory(
        text,
        type=type.value if type else "feedback",
        name=name or "",
        tags=_parse_csv(tags),
        about=_parse_csv(about),
        bot=bot_list,
    )
    await interaction.response.send_message(
        f"Saved #{m['id']} ({m.get('type')}): {m.get('name', '')}"
    )


@mem_group.command(name="search", description="search memories by term")
async def mem_search(interaction: discord.Interaction, term: str) -> None:
    results = store.search_memories(term)
    await interaction.response.send_message(_wrap(_fmt_memory_list(results)))


@mem_group.command(name="edit", description="overwrite the body of a memory by id")
@app_commands.describe(id="memory id", text="new body text (replaces existing)")
async def mem_edit(
    interaction: discord.Interaction,
    id: int,
    text: str,
) -> None:
    ok = store.edit_memory(id, text=text)
    if ok:
        await interaction.response.send_message(f"✅ Edited #{id}")
    else:
        await interaction.response.send_message(
            f"Memory #{id} not found", ephemeral=True
        )


@mem_group.command(name="pin", description="toggle a memory's pinned state by id")
async def mem_pin(interaction: discord.Interaction, id: int) -> None:
    m = next((x for x in store.load_memories() if x.get("id") == id), None)
    if not m:
        await interaction.response.send_message(
            f"Memory #{id} not found", ephemeral=True,
        )
        return
    new_state = not m.get("pinned", False)
    store.edit_memory(id, pinned=new_state)
    state_str = "📌 pinned" if new_state else "unpinned"
    await interaction.response.send_message(f"#{id}: {state_str}")


@mem_group.command(name="delete", description="delete a memory by id (recoverable via /mem trash)")
async def mem_delete(interaction: discord.Interaction, id: int) -> None:
    # Soft-delete through the history layer so /mem trash + /mem restore can
    # round-trip. (Plain store.remove_memory would drop it with no trail.)
    ok = history.remove_memory_with_history(id, actor="discord")
    if ok:
        await interaction.response.send_message(f"Deleted #{id} (restore with /mem restore id:{id})")
    else:
        await interaction.response.send_message(f"Memory #{id} not found", ephemeral=True)


@mem_group.command(name="trash", description="show recently deleted memories")
@app_commands.describe(limit="how many recent deletes to show (default 20)")
async def mem_trash(interaction: discord.Interaction, limit: int = 20) -> None:
    deletes = history.load_recent_deletes(limit=limit)
    mem_deletes = [d for d in deletes if d.get("kind") == "memory"]
    if not mem_deletes:
        await interaction.response.send_message("(trash is empty)", ephemeral=True)
        return
    lines = ["**recently deleted memories:**"]
    for d in mem_deletes:
        before = d.get("before") or {}
        eid = before.get("id", "?")
        name = before.get("name") or (before.get("text") or "")[:60]
        ts = (d.get("ts") or "")[:10]
        lines.append(f"  • #`{eid}` `{ts}` — {name}")
    lines.append("\nRestore with `/mem restore id:<n>`")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@mem_group.command(name="restore", description="restore a deleted memory by its original id")
async def mem_restore(interaction: discord.Interaction, id: int) -> None:
    deletes = history.load_recent_deletes(limit=200)
    target = next(
        (d for d in deletes
         if d.get("kind") == "memory" and (d.get("before") or {}).get("id") == id),
        None,
    )
    if target is None:
        await interaction.response.send_message(
            f"No deleted memory #{id} in trash (last 200 deletes)", ephemeral=True,
        )
        return
    restored = history.restore_deleted(target)
    if restored:
        new_id = restored.get("id", id)
        suffix = "" if new_id == id else f" (new id #{new_id} — original slot was reused)"
        await interaction.response.send_message(f"♻️ Restored memory #{id}{suffix}")
    else:
        await interaction.response.send_message(
            f"Restore failed for #{id}", ephemeral=True,
        )


@mem_group.command(name="retag", description="replace a memory's tags (no body change)")
@app_commands.describe(id="memory id", tags="comma-separated new tag list (replaces all)")
async def mem_retag(interaction: discord.Interaction, id: int, tags: str) -> None:
    new_tags = _parse_csv(tags)
    ok = store.edit_memory(id, tags=new_tags)
    if ok:
        await interaction.response.send_message(
            f"#{id} tags → `{', '.join(new_tags) or '(none)'}`",
        )
    else:
        await interaction.response.send_message(f"Memory #{id} not found", ephemeral=True)


@mem_group.command(name="reabout", description="replace a memory's about labels (no body change)")
@app_commands.describe(id="memory id", about="comma-separated new about list (replaces all)")
async def mem_reabout(interaction: discord.Interaction, id: int, about: str) -> None:
    new_about = _parse_csv(about)
    ok = store.edit_memory(id, about=new_about)
    if ok:
        await interaction.response.send_message(
            f"#{id} about → `{', '.join(new_about) or '(none)'}`",
        )
    else:
        await interaction.response.send_message(f"Memory #{id} not found", ephemeral=True)


@mem_group.command(name="dupes", description="find memories semantically similar to a given one")
@app_commands.describe(
    id="memory id to find duplicates of",
    limit="how many similar entries to show (default 8)",
)
async def mem_dupes(interaction: discord.Interaction, id: int, limit: int = 8) -> None:
    m = next((x for x in store.load_memories() if x.get("id") == id), None)
    if not m:
        await interaction.response.send_message(
            f"Memory #{id} not found", ephemeral=True,
        )
        return
    await interaction.response.defer(thinking=True)
    query = (m.get("name") or "") + " " + (m.get("text") or "")[:400]
    try:
        pairs = vecgrep_client.search_corpus_to_ids(
            query.strip(),
            vecgrep_client.VECGREP_CORPUS_MEMORIES,
            top_k=max(limit + 5, 10),
            want_kind="memory",
        )
    except vecgrep_client.VecgrepUnavailable as e:
        await interaction.followup.send(f"⚠️ vecgrep unavailable: {e}", ephemeral=True)
        return
    pairs = [(eid, pct) for eid, pct in pairs if eid != id][:limit]
    if not pairs:
        await interaction.followup.send(f"No similar memories found to #{id}.")
        return
    by_id = {x.get("id"): x for x in store.load_memories()}
    lines = [f"**similar to #{id} ({m.get('name') or '(untitled)'}):**"]
    for eid, pct in pairs:
        e = by_id.get(eid)
        if not e:
            continue
        name = e.get("name") or (e.get("text") or "")[:60]
        lines.append(f"  • `{pct*100:.1f}%` #`{eid}` — {name}")
    await interaction.followup.send("\n".join(lines))


# ─────────────────────────── /journal command group ───────────────────────────

jou_group = app_commands.Group(name="journal", description="cc-discord-kit journal")


@jou_group.command(name="list", description="list journal entries")
@app_commands.describe(days="filter to last N days (0 = all)")
async def jou_list(interaction: discord.Interaction, days: int = 0) -> None:
    entries = store.journal_recent(days) if days else store.load_journal()
    await interaction.response.send_message(_wrap(_fmt_journal_list(entries)))


@jou_group.command(name="show", description="show a journal entry by id")
async def jou_show(interaction: discord.Interaction, id: int) -> None:
    e = next((x for x in store.load_journal() if x.get("id") == id), None)
    if not e:
        await interaction.response.send_message(f"Journal #{id} not found", ephemeral=True)
        return
    await interaction.response.send_message(_wrap(_fmt_journal_full(e)))


@jou_group.command(name="add", description="pin a journal entry")
@app_commands.describe(
    text="entry body",
    actor="who/what created it (default: discord-handler)",
    tags="comma-separated tags",
)
async def jou_add(
    interaction: discord.Interaction,
    text: str,
    actor: str | None = None,
    tags: str | None = None,
) -> None:
    e = store.add_journal(
        text,
        source="discord:slash",
        actor=actor or "discord-handler",
        tags=_parse_csv(tags),
    )
    await interaction.response.send_message(f"Pinned #{e['id']}")


@jou_group.command(name="search", description="search journal by term")
async def jou_search(interaction: discord.Interaction, term: str) -> None:
    results = store.search_journal(term)
    await interaction.response.send_message(_wrap(_fmt_journal_list(results)))


@jou_group.command(name="delete", description="delete a journal entry by id")
async def jou_delete(interaction: discord.Interaction, id: int) -> None:
    ok = store.remove_journal(id)
    if ok:
        await interaction.response.send_message(f"Deleted #{id}")
    else:
        await interaction.response.send_message(f"Journal #{id} not found", ephemeral=True)


@jou_group.command(name="pin", description="toggle a journal entry's pinned state")
async def jou_pin(interaction: discord.Interaction, id: int) -> None:
    e = next((x for x in store.load_journal() if x.get("id") == id), None)
    if not e:
        await interaction.response.send_message(
            f"Journal #{id} not found", ephemeral=True,
        )
        return
    new_state = not e.get("pinned", False)
    store.edit_journal(id, pinned=new_state)
    state_str = "📌 pinned" if new_state else "unpinned"
    await interaction.response.send_message(f"journal #{id}: {state_str}")


# ─────────────────────────── /bot command group ───────────────────────────
#
# Per-bot access.json admin from Discord. Reads + writes each bot's
# access.json via bot_admin.py, which knows the schema differences and the
# per-bot path layout (local vs ssh-wrapped remotes). Admin-gated: CCDK_ADMIN_ID
# env must equal interaction.user.id, otherwise the command no-ops with a
# friendly message.

bot_group = app_commands.Group(name="bot", description="per-bot access.json admin")


def _admin_or_deny(interaction: discord.Interaction) -> bool:
    if ADMIN_ID is None:
        return False
    return interaction.user.id == ADMIN_ID


def _admin_or_toggle_or_deny(interaction: discord.Interaction) -> bool:
    """Looser gate for /bot toggle — admin OR anyone in the toggle allowlist.
    Toggle is benign relative to set/info (only flips bools, no schema choice),
    so it's the right surface to delegate to trusted non-admin users.
    """
    if ADMIN_ID is not None and interaction.user.id == ADMIN_ID:
        return True
    return interaction.user.id in TOGGLE_ALLOWED_IDS


async def _deny(interaction: discord.Interaction) -> None:
    if ADMIN_ID is None:
        msg = "/bot is admin-only and `CCDK_ADMIN_ID` isn't set"
    else:
        msg = "/bot is admin-only — you're not the admin"
    await interaction.response.send_message(msg, ephemeral=True)


def _bot_choices() -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=b, value=b) for b in bot_admin.list_bots()]


def _fmt_flags(flags: dict, schema: str) -> str:
    """One-line per-channel flag summary for /bot list output."""
    if not flags:
        return "*(no per-channel config)*"
    parts = []
    for k, v in flags.items():
        if isinstance(v, list):
            if not v:
                continue
            parts.append(f"{k}={len(v)}")
        elif isinstance(v, bool):
            parts.append(f"{k}={'on' if v else 'off'}")
        else:
            parts.append(f"{k}={v}")
    return " · ".join(parts) if parts else "*(empty)*"


@bot_group.command(name="list", description="show every bot's flags for THIS channel")
async def bot_list(interaction: discord.Interaction) -> None:
    if not _admin_or_deny(interaction):
        return await _deny(interaction)
    rows = bot_admin.all_bot_flags_for_channel(str(interaction.channel_id))
    lines = [f"**access state in <#{interaction.channel_id}>:**"]
    for r in rows:
        if "error" in r:
            lines.append(f"  • `{r['bot']}` ⚠️ {r['error']}")
            continue
        lines.append(f"  • `{r['bot']}` ({r['schema']}): {_fmt_flags(r['flags'], r['schema'])}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot_group.command(name="info", description="full per-channel state for one bot")
@app_commands.describe(bot="which bot to inspect")
@app_commands.choices(bot=_bot_choices())
async def bot_info(interaction: discord.Interaction, bot: app_commands.Choice[str]) -> None:
    if not _admin_or_deny(interaction):
        return await _deny(interaction)
    try:
        flags = bot_admin.channel_flags(bot.value, str(interaction.channel_id))
    except Exception as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
        return
    schema = bot_admin.schema_for(bot.value)
    schema_flags = bot_admin.flags_for(bot.value)
    lines = [f"**{bot.value}** ({schema}) in <#{interaction.channel_id}>"]
    if not flags:
        lines.append("  *(channel not in bot's access.json yet — using defaults)*")
    for fname, spec in schema_flags.items():
        cur = flags.get(fname, "—")
        kind = spec["type"]
        if kind == "enum":
            kind_hint = "/".join(spec["values"])
        else:
            kind_hint = kind
        lines.append(f"  • `{fname}` ({kind_hint}): **{cur}**")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def _bot_set_flag_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Suggest valid flag names for the chosen bot. Reads the bot from the
    partially-filled-in interaction so the dropdown narrows by schema."""
    bot_val = None
    for opt in (interaction.namespace.__dict__ or {}).values():
        if isinstance(opt, str) and opt in bot_admin.list_bots():
            bot_val = opt
            break
    if bot_val is None:
        return []
    flags = bot_admin.flags_for(bot_val)
    cur = (current or "").lower()
    return [
        app_commands.Choice(name=f, value=f)
        for f in flags.keys() if cur in f.lower()
    ][:25]


async def _bot_set_value_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Suggest valid values once both bot + flag are known."""
    ns = interaction.namespace
    bot_val = getattr(ns, "bot", None)
    flag_val = getattr(ns, "flag", None)
    if not bot_val or not flag_val or bot_val not in bot_admin.list_bots():
        return []
    flags = bot_admin.flags_for(bot_val)
    spec = flags.get(flag_val)
    if not spec:
        return []
    cur = (current or "").lower()
    if spec["type"] == "bool":
        opts = ["true", "false"]
    elif spec["type"] == "enum":
        opts = list(spec["values"])
    else:
        return []
    return [
        app_commands.Choice(name=v, value=v) for v in opts if cur in v.lower()
    ][:25]


@bot_group.command(name="set", description="set a flag on a bot for THIS channel")
@app_commands.describe(
    bot="which bot to modify",
    flag="flag name (autocomplete shows what's valid for this bot's schema)",
    value="new value (autocomplete shows valid options for the chosen flag)",
)
@app_commands.choices(bot=_bot_choices())
@app_commands.autocomplete(flag=_bot_set_flag_autocomplete, value=_bot_set_value_autocomplete)
async def bot_set(
    interaction: discord.Interaction,
    bot: app_commands.Choice[str],
    flag: str,
    value: str,
) -> None:
    if not _admin_or_deny(interaction):
        return await _deny(interaction)
    schema = bot_admin.schema_for(bot.value)
    try:
        coerced = bot_admin.coerce_value(flag, value, schema)
    except ValueError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
        return
    try:
        new_flags = bot_admin.set_flag(
            bot.value, str(interaction.channel_id), flag, coerced,
        )
    except Exception as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
        return
    await interaction.response.send_message(
        f"✅ `{bot.value}` in <#{interaction.channel_id}>: "
        f"`{flag}` → **{coerced}** (now: {_fmt_flags(new_flags, schema)})",
        ephemeral=True,
    )


async def _bot_toggle_flag_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Same as set's flag autocomplete, but filtered to boolean-typed flags
    only since /bot toggle can't flip enums."""
    bot_val = getattr(interaction.namespace, "bot", None)
    if not bot_val or bot_val not in bot_admin.list_bots():
        return []
    flags = bot_admin.flags_for(bot_val)
    cur = (current or "").lower()
    return [
        app_commands.Choice(name=f, value=f)
        for f, spec in flags.items()
        if spec["type"] == "bool" and cur in f.lower()
    ][:25]


@bot_group.command(name="toggle", description="flip a boolean flag on a bot for THIS channel")
@app_commands.describe(
    bot="which bot to modify",
    flag="boolean flag name (autocomplete filters to bools only)",
)
@app_commands.choices(bot=_bot_choices())
@app_commands.autocomplete(flag=_bot_toggle_flag_autocomplete)
async def bot_toggle(
    interaction: discord.Interaction,
    bot: app_commands.Choice[str],
    flag: str,
) -> None:
    if not _admin_or_toggle_or_deny(interaction):
        return await _deny(interaction)
    schema = bot_admin.schema_for(bot.value)
    spec = bot_admin.flags_for(bot.value).get(flag)
    if spec is None:
        await interaction.response.send_message(
            f"⚠️ unknown flag `{flag}` for `{bot.value}` ({schema})", ephemeral=True,
        )
        return
    if spec["type"] != "bool":
        await interaction.response.send_message(
            f"⚠️ `{flag}` is `{spec['type']}`, not bool — use `/bot set` instead",
            ephemeral=True,
        )
        return
    cur = bot_admin.channel_flags(bot.value, str(interaction.channel_id)).get(flag, False)
    new_val = not bool(cur)
    try:
        new_flags = bot_admin.set_flag(
            bot.value, str(interaction.channel_id), flag, new_val,
        )
    except Exception as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
        return
    await interaction.response.send_message(
        f"✅ `{bot.value}` `{flag}`: {cur} → **{new_val}** "
        f"(now: {_fmt_flags(new_flags, schema)})",
        ephemeral=True,
    )


@bot_group.command(
    name="narrate",
    description="set the narrate mode (collapse/always/never) for a bot in THIS channel",
)
@app_commands.describe(
    bot="which bot to configure",
    mode="collapse (live, vanishes at end) / always (live, kept as 🧠 Narration prefix) / never (off)",
)
@app_commands.choices(
    bot=_bot_choices(),
    mode=[
        app_commands.Choice(name="collapse — live placeholder, deleted when reply lands", value="collapse"),
        app_commands.Choice(name="always — placeholder kept as 🧠 Narration prefix above reply", value="always"),
        app_commands.Choice(name="never — no narration in this channel", value="never"),
    ],
)
async def bot_narrate(
    interaction: discord.Interaction,
    bot: app_commands.Choice[str],
    mode: app_commands.Choice[str],
) -> None:
    """Dedicated /bot narrate so users don't have to remember /bot set's flag
    name. Both routes write through the same set_flag path so behavior stays
    consistent; this is sugar."""
    if not _admin_or_toggle_or_deny(interaction):
        return await _deny(interaction)
    schema = bot_admin.schema_for(bot.value)
    # narrate is only defined for the claude schema today; refuse early if
    # someone targets a bot whose schema doesn't expose it.
    if "narrate" not in bot_admin.flags_for(bot.value):
        await interaction.response.send_message(
            f"⚠️ `{bot.value}` ({schema}) has no narrate flag", ephemeral=True,
        )
        return
    try:
        bot_admin.set_flag(
            bot.value, str(interaction.channel_id), "narrate", mode.value,
        )
    except Exception as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
        return
    # Skip the trailing `(now: ...)` summary that the other /bot subcommands
    # use — surfacing every other per-channel flag here is noise when the
    # user just set ONE thing. /bot info exists if they want the full state.
    await interaction.response.send_message(
        f"✅ `{bot.value}` in <#{interaction.channel_id}>: narrate → **{mode.value}**",
        ephemeral=True,
    )


@bot_group.command(
    name="tools",
    description="set the tool-trace mode (off/collapse/ticker/diffs/full) for a bot in THIS channel",
)
@app_commands.describe(
    bot="which bot to configure",
    mode="off / collapse (live ticker+diffs, cleared after reply) / ticker (kept) / diffs (kept) / full (+ Bash stdout)",
)
@app_commands.choices(
    bot=_bot_choices(),
    mode=[
        app_commands.Choice(name="off — nothing extra surfaced", value="off"),
        app_commands.Choice(name="collapse — live ticker + edit diffs while bot works, cleared once the reply lands", value="collapse"),
        app_commands.Choice(name="ticker — one-line names+args of every tool call (kept)", value="ticker"),
        app_commands.Choice(name="diffs — ticker + diffs for any text-file edit", value="diffs"),
        app_commands.Choice(name="full — diffs + Bash stdout (secret-stripped, firehose)", value="full"),
    ],
)
async def bot_tools(
    interaction: discord.Interaction,
    bot: app_commands.Choice[str],
    mode: app_commands.Choice[str],
) -> None:
    """Dedicated /bot tools so users don't have to remember /bot set's flag
    name. Same sugar pattern as /bot narrate — set_flag is the real path."""
    if not _admin_or_toggle_or_deny(interaction):
        return await _deny(interaction)
    schema = bot_admin.schema_for(bot.value)
    if "tools" not in bot_admin.flags_for(bot.value):
        await interaction.response.send_message(
            f"⚠️ `{bot.value}` ({schema}) has no tools flag", ephemeral=True,
        )
        return
    try:
        bot_admin.set_flag(
            bot.value, str(interaction.channel_id), "tools", mode.value,
        )
    except Exception as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
        return
    await interaction.response.send_message(
        f"✅ `{bot.value}` in <#{interaction.channel_id}>: tools → **{mode.value}**",
        ephemeral=True,
    )


@bot_group.command(
    name="doctor",
    description="health-check every bot: hooks, access.json, personas, transports, service",
)
async def bot_doctor(interaction: discord.Interaction) -> None:
    """Run bots_doctor across the whole fleet and post the report. Reads each
    bot's settings.json through its transport (ssh-wrapped for remotes), so a
    full run does several SSH round-trips and can blow Discord's 3s reply
    deadline — hence defer + followup. Same core the CLI's `bots doctor`
    command calls; this is just the Discord surface."""
    if not _admin_or_deny(interaction):
        return await _deny(interaction)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        report = bots_doctor.format_report(bots_doctor.run())
    except Exception as e:
        logging.exception("/bot doctor failed")
        await interaction.followup.send(f"⚠️ doctor failed: {type(e).__name__}: {e}", ephemeral=True)
        return
    await interaction.followup.send(_wrap(report), ephemeral=True)


# ─────────────────────────── /help and /squad ───────────────────────────


HELP_TEXT = """**cc-discord-kit commands**

**`/mem`** — durable memories (cap 200). All public, no admin gate.
  • `list [q] [type] [about] [bot] [show_all]` — browse + search in one
  • `show <id>` — full body of a memory
  • `add <text> [type] [name] [tags] [about] [bot]` — save new
  • `edit <id> <text>` — overwrite body
  • `retag <id> <tags>` / `reabout <id> <about>` — replace tags or about labels (no body change)
  • `pin <id>` — toggle pinned (pinned entries show first)
  • `delete <id>` — delete a memory
  • `trash [limit]` — list recently deleted memories
  • `restore <id>` — bring a deleted memory back
  • `dupes <id>` — find memories semantically similar to a given one (vecgrep)
  • `search <term>` — substring search

**`/journal`** — pinned moments (cap 1000), recent slice loaded to bot prompts.
  • `list [days]` · `show <id>` · `add <text> [actor] [tags]`
  • `pin <id>` — toggle pinned
  • `search <term>` · `delete <id>`

**`/bot`** — per-bot access.json admin (admin-gated). Per-channel flags vary by schema:
  • `list` — all bots' flags for THIS channel
  • `info <bot>` — full per-channel state for one bot
  • `set <bot> <flag> <value>` — autocomplete fills valid flags + values
  • `toggle <bot> <flag>` — flip a boolean flag (autocomplete bools only)
  • `narrate <bot> <mode>` — shortcut for setting narrate mode
  • `tools <bot> <mode>` — shortcut for setting tool-trace mode
  • `doctor` — health-check the whole fleet (hooks, access.json, personas, transports)

**`/squad`** — ops + diagnostics (services come from CCDK_SERVICES)
  • `status` — services health
  • `services` — list whitelisted systemd units + their state
  • `restart <unit>` — restart a whitelisted unit (admin only)
  • `logs <unit> [lines]` — tail recent journalctl output (default 30, max 100)
  • `presence <text> [type]` — set this bot's own status (admin only). type: custom/playing/watching/listening/competing

**`/persona`** — per-bot brain-file registry across all configured bots
  • `list` — all bots + their slot names
  • `show <bot> <slot>` — display a bot's brain file (autocompletes slots per-bot)
  • `edit <bot> <slot> <text>` — overwrite a slot (admin only). git-tracked slots auto-commit.

**`/vecgrep <query>`** — semantic search across the configured corpora. Use natural language.
  • `kind:` — restrict to memory or journal (default: both)
  • `corpus:` — which index to search

**`/help`** — this card"""


@client.tree.command(name="help", description="show what every cc-discord-kit command does")
async def help_cmd(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(HELP_TEXT, ephemeral=True)


squad_group = app_commands.Group(name="squad", description="ops + diagnostics")


def _systemd_active(unit: str) -> tuple[bool, str]:
    """Return (active, status_word) for a user systemd unit. status_word
    is the raw `is-active` reply (active|inactive|failed|...) so we can
    show the precise state in the card."""
    import subprocess
    try:
        p = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True, text=True, timeout=5,
        )
        word = (p.stdout or p.stderr).strip() or "unknown"
        return (word == "active", word)
    except Exception as e:
        return (False, f"err: {type(e).__name__}")


@squad_group.command(name="status", description="health check: configured services")
async def squad_status(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    if not _SERVICES:
        await interaction.followup.send(
            "No services configured. Set `CCDK_SERVICES=unit:label,...` in the "
            "env file to enable /squad.",
            ephemeral=True,
        )
        return
    lines = ["**status**"]
    for unit, label in _SERVICES:
        ok, state = _systemd_active(unit)
        marker = "🟢" if ok else "🔴"
        lines.append(f"  {marker} `{unit}` — {label} ({state})")
    # Note: presence/online state of other bots requires Members + Presence
    # intents which we don't request here (slash-only bot). systemd state
    # above is the source of truth — if the unit is active, the bot is up.
    await interaction.followup.send("\n".join(lines), ephemeral=True)


def _unit_choices() -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=u, value=u) for u in _KNOWN_UNITS]


@squad_group.command(name="services", description="list whitelisted systemd units + their state")
async def squad_services(interaction: discord.Interaction) -> None:
    if not _SERVICES:
        await interaction.response.send_message(
            "No services configured. Set `CCDK_SERVICES=unit:label,...` in the "
            "env file to enable /squad.",
            ephemeral=True,
        )
        return
    lines = ["**known services:**"]
    for u, _label in _SERVICES:
        ok, state = _systemd_active(u)
        marker = "🟢" if ok else "🔴"
        lines.append(f"  {marker} `{u}` ({state})")
    lines.append("\nRestart with `/squad restart unit:<name>` (admin only).")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@squad_group.command(name="restart", description="restart a whitelisted systemd unit (admin only)")
@app_commands.describe(unit="which unit to restart")
@app_commands.choices(unit=_unit_choices())
async def squad_restart(
    interaction: discord.Interaction,
    unit: app_commands.Choice[str],
) -> None:
    if not _admin_or_deny(interaction):
        return await _deny(interaction)
    import subprocess
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        p = subprocess.run(
            ["systemctl", "--user", "restart", unit.value],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        await interaction.followup.send(
            f"⚠️ restart failed: {type(e).__name__}: {e}", ephemeral=True,
        )
        return
    if p.returncode != 0:
        await interaction.followup.send(
            f"⚠️ restart `{unit.value}` returned {p.returncode}: ```{p.stderr.strip()[:1500]}```",
            ephemeral=True,
        )
        return
    ok, state = _systemd_active(unit.value)
    marker = "🟢" if ok else "🔴"
    await interaction.followup.send(
        f"{marker} restarted `{unit.value}` → {state}", ephemeral=True,
    )


@squad_group.command(name="logs", description="tail recent journalctl output for a unit")
@app_commands.describe(
    unit="which unit to tail",
    lines="number of recent lines to show (default 30, max 100)",
)
@app_commands.choices(unit=_unit_choices())
async def squad_logs(
    interaction: discord.Interaction,
    unit: app_commands.Choice[str],
    lines: int = 30,
) -> None:
    import subprocess
    n = max(1, min(int(lines), 100))
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        p = subprocess.run(
            ["journalctl", "--user", "-u", unit.value, "-n", str(n),
             "--no-pager", "--output=cat"],
            capture_output=True, text=True, timeout=8,
        )
    except Exception as e:
        await interaction.followup.send(
            f"⚠️ logs failed: {type(e).__name__}: {e}", ephemeral=True,
        )
        return
    out = p.stdout or "(empty)"
    await interaction.followup.send(
        f"**last {n} lines of `{unit.value}`:**\n{_wrap(out)}",
        ephemeral=True,
    )


@squad_group.command(name="presence", description="set this bot's own Discord status")
@app_commands.describe(
    text="status text (or empty string to clear)",
    type="presence type — playing/watching/listening/competing/custom (default: custom)",
)
@app_commands.choices(type=[
    app_commands.Choice(name="custom",     value="custom"),
    app_commands.Choice(name="playing",    value="playing"),
    app_commands.Choice(name="watching",   value="watching"),
    app_commands.Choice(name="listening",  value="listening"),
    app_commands.Choice(name="competing",  value="competing"),
])
async def squad_presence(
    interaction: discord.Interaction,
    text: str,
    type: app_commands.Choice[str] | None = None,
) -> None:
    if not _admin_or_deny(interaction):
        return await _deny(interaction)
    type_str = type.value if type else "custom"
    type_map = {
        "playing":   discord.ActivityType.playing,
        "watching":  discord.ActivityType.watching,
        "listening": discord.ActivityType.listening,
        "competing": discord.ActivityType.competing,
        "custom":    discord.ActivityType.custom,
    }
    activity_type = type_map[type_str]
    text = (text or "").strip()
    try:
        if not text:
            # Clear presence
            await client.change_presence(activity=None)
            await interaction.response.send_message(
                "✅ cleared presence", ephemeral=True,
            )
            return
        if activity_type == discord.ActivityType.custom:
            # Custom activity uses `state`, not `name`, for the visible bit.
            activity = discord.CustomActivity(name=text, state=text)
        else:
            activity = discord.Activity(type=activity_type, name=text)
        await client.change_presence(activity=activity)
        await interaction.response.send_message(
            f"✅ presence → `{type_str}` {text!r}", ephemeral=True,
        )
    except Exception as e:
        await interaction.response.send_message(
            f"⚠️ failed: {type(e).__name__}: {e}", ephemeral=True,
        )


# ─────────────────────────── /persona command group ───────────────────────────

persona_group = app_commands.Group(
    name="persona", description="view and edit per-bot brain files",
)


def _persona_bot_choices() -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=b, value=b) for b in personas.list_bots()]


async def _persona_slot_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Suggest valid slot names for the chosen bot."""
    bot_val = getattr(interaction.namespace, "bot", None)
    if not bot_val or bot_val not in personas.list_bots():
        return []
    try:
        slots = personas.get_files(bot_val)
    except KeyError:
        return []
    cur = (current or "").lower()
    return [
        app_commands.Choice(name=s["slot"], value=s["slot"])
        for s in slots if cur in s["slot"].lower()
    ][:25]


@persona_group.command(name="list", description="list bots and their brain-file slots")
async def persona_list(interaction: discord.Interaction) -> None:
    bots = personas.list_bots()
    lines = ["**bot brain-file registry:**"]
    for b in bots:
        try:
            slots = personas.get_files(b)
        except KeyError:
            continue
        slot_list = ", ".join(f"`{s['slot']}`" for s in slots)
        lines.append(f"  • **{b}**: {slot_list}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@persona_group.command(name="show", description="show a bot's brain file (truncated)")
@app_commands.describe(bot="which bot", slot="which file slot")
@app_commands.choices(bot=_persona_bot_choices())
@app_commands.autocomplete(slot=_persona_slot_autocomplete)
async def persona_show(
    interaction: discord.Interaction,
    bot: app_commands.Choice[str],
    slot: str,
) -> None:
    try:
        info = personas.read_slot(bot.value, slot)
    except KeyError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
        return
    text = info.get("text", "") or "(empty file)"
    header = f"**{bot.value} / {slot}** (`{info.get('path')}`, mode=`{info.get('mode')}`)"
    await interaction.response.send_message(
        f"{header}\n{_wrap(text)}", ephemeral=True,
    )


@persona_group.command(name="edit", description="overwrite a bot's persona slot (admin only)")
@app_commands.describe(
    bot="which bot",
    slot="which file slot",
    text="full new contents (replaces file)",
)
@app_commands.choices(bot=_persona_bot_choices())
@app_commands.autocomplete(slot=_persona_slot_autocomplete)
async def persona_edit(
    interaction: discord.Interaction,
    bot: app_commands.Choice[str],
    slot: str,
    text: str,
) -> None:
    if not _admin_or_deny(interaction):
        return await _deny(interaction)
    try:
        result = personas.write_slot(bot.value, slot, text)
    except KeyError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
        return
    except Exception as e:
        await interaction.response.send_message(
            f"⚠️ {type(e).__name__}: {e}", ephemeral=True,
        )
        return
    if not result.get("ok"):
        await interaction.response.send_message(
            f"⚠️ write failed: {result.get('error', 'unknown')}", ephemeral=True,
        )
        return
    sha = result.get("sha")
    sha_str = f" (committed `{sha[:8]}`)" if sha else ""
    await interaction.response.send_message(
        f"✅ wrote `{bot.value}/{slot}`{sha_str}", ephemeral=True,
    )


# ─────────────────────────── /vecgrep ───────────────────────────


# Confidence tiers for /vecgrep card. Tier drives ANSI color in the
# ```ansi``` Discord code block.
#
# Pct alone is unreliable because nomic-embed-text floors around 70-75% for
# any English query (vector noise band) and RRF compresses BM25-only hits
# down into single-digit percentages. So we combine pct with `matched_by`:
# a BM25 keyword hit at 1.6% is a *real* exact-keyword match, not noise —
# promote its color tier even when pct says iffy.
#
# ANSI codes that render in Discord ```ansi``` blocks: 32=green, 33=yellow.
# We deliberately avoid 30 (bright-black) — invisible on dark mode. Below-
# threshold rows go uncolored (default) instead of gray.
_VECGREP_HI_PCT = 75.0
_VECGREP_MED_PCT = 45.0


def _ansi_for_hit(pct: float, matched_by: frozenset[str]) -> tuple[str, str]:
    """Return (open, close) ANSI codes based on confidence tier.

    Aligned with vecgrep's canonical sigmoid-calibrated tiers:

    GREEN:  pct >= 75, or V+K (both retrievers agreed)
    YELLOW: pct >= 45
    none:   below 45 — noise band
    """
    has_bm25 = "bm25" in matched_by
    has_vector = "vector" in matched_by
    has_both = has_bm25 and has_vector
    if pct >= _VECGREP_HI_PCT or has_both:
        return ("\x1b[0;32m", "\x1b[0m")  # green
    if pct >= _VECGREP_MED_PCT:
        return ("\x1b[0;33m", "\x1b[0m")  # yellow
    return ("", "")


def _match_badge(matched_by: frozenset[str]) -> str:
    """2-char badge — V vector, K keyword/bm25, VK both."""
    has_v = "vector" in matched_by
    has_k = "bm25" in matched_by
    if has_v and has_k:
        return "VK"
    if has_v:
        return "V "
    if has_k:
        return " K"
    return "  "


# The unified store corpus (memories + journal) lives in
# vecgrep_client.VECGREP_CORPUS_MEMORIES. Any extra corpora the user wants in
# the /vecgrep picker (source code, transcripts, etc.) are declared via
# CCDK_VECGREP_CORPORA=value:description,... — externalized so source carries
# no author-specific corpus names. The store corpus always appears.
_CORPUS_STORE = vecgrep_client.VECGREP_CORPUS_MEMORIES


def _load_extra_corpora() -> dict[str, str]:
    """Parse `CCDK_VECGREP_CORPORA=value:description,...`. These are
    path-keyed corpora (file paths, not memory/journal ids) — rendered with
    the file-path renderer. Empty / unset → just the unified store corpus."""
    raw = _read_env_var("CCDK_VECGREP_CORPORA")
    if not raw:
        return {}
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" in pair:
            value, desc = pair.split(":", 1)
            value, desc = value.strip(), desc.strip()
        else:
            value, desc = pair, pair
        if value and value != _CORPUS_STORE:
            out[value] = desc or value
    return out


_EXTRA_CORPORA: dict[str, str] = _load_extra_corpora()

_CORPUS_DESCRIPTIONS: dict[str, str] = {
    _CORPUS_STORE: "memories + journal",
    **_EXTRA_CORPORA,
}


def _corpus_choices() -> list[app_commands.Choice[str]]:
    choices = [app_commands.Choice(
        name=f"{_CORPUS_STORE} (memories + journal)", value=_CORPUS_STORE,
    )]
    for value, desc in _EXTRA_CORPORA.items():
        choices.append(app_commands.Choice(name=f"{value} ({desc})", value=value))
    return choices[:25]


async def _vecgrep_corpus_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Suggest configured corpus values. The store corpus always appears;
    extras come from CCDK_VECGREP_CORPORA. Corpus is a free-text string option
    (not a fixed Choice list) so the set can grow via config without a code
    change — autocomplete keeps the picker usable."""
    cur = (current or "").lower()
    out = []
    for choice in _corpus_choices():
        if cur in choice.value.lower() or cur in choice.name.lower():
            out.append(choice)
    return out[:25]


def _row_extra_tags(entry: dict, max_len: int = 36) -> str:
    """Compact tag string for inline display in a result row."""
    tags = entry.get("tags") or ""
    if isinstance(tags, list):
        tags = ",".join(t for t in tags if t)
    elif not isinstance(tags, str):
        tags = str(tags)
    tags = tags.strip()
    if not tags:
        return ""
    if len(tags) > max_len:
        tags = tags[: max_len - 1] + "…"
    return tags


def _vecgrep_row(
    rank: int,
    pct: float,
    matched_by: frozenset[str],
    label: str,
    body: str,
) -> str:
    """One fixed-width result row for the /vecgrep ```ansi``` card.

    Column layout (every field a fixed width so columns line up regardless of
    ANSI color escapes, which are zero-width and don't affect alignment):

        ▸rr  62.1% VK  #132    semantic-search relevance · tags

    - rank: 2-char right-aligned ('#1' .. '#12'). Order is meaningful (it's the
      reranked ranking), so we number it — the displayed % is a *separate*
      confidence signal and may not descend monotonically under rerank.
    - pct:  5-char ('100.0', ' 62.1', '  8.0') + '%'
    - badge: 2-char (V / K / VK / blank) from _match_badge
    - label: 7-char left ('#84', 'j#48', '#1234')
    - body: free text (name + optional · tags), caller pre-trims.
    """
    on, off = _ansi_for_hit(pct, matched_by)
    rank_s = f"#{rank}"
    return f"{on}{rank_s:>3}  {pct:5.1f}% {_match_badge(matched_by)} {label:<7} {body}{off}"


@client.tree.command(name="vecgrep", description="semantic search across configured corpora via vecgrep")
@app_commands.describe(
    query="natural-language query — finds semantic matches, not just substrings",
    limit="how many hits to show (default 8)",
    corpus="which index to search (default: the unified store corpus)",
    kind="restrict to memory or journal (store corpus only; default: both)",
)
@app_commands.choices(kind=[
    app_commands.Choice(name="memory",  value="memory"),
    app_commands.Choice(name="journal", value="journal"),
    app_commands.Choice(name="both",    value="both"),
])
@app_commands.autocomplete(corpus=_vecgrep_corpus_autocomplete)
async def vecgrep_cmd(
    interaction: discord.Interaction,
    query: str,
    limit: int = 8,
    corpus: str | None = None,
    kind: app_commands.Choice[str] | None = None,
) -> None:
    corpus_val = corpus or _CORPUS_STORE
    kind_val = (kind.value if kind else "both")
    # `kind` filter only meaningful for the unified store corpus
    want_kind = None if (kind_val == "both" or corpus_val != _CORPUS_STORE) else kind_val
    await interaction.response.defer(thinking=True)

    # Path-keyed corpora (source_id is a file path, not a memory/journal id)
    # use the file-path renderer — store ID parsing doesn't apply.
    if corpus_val != _CORPUS_STORE:
        await _vecgrep_render_paths(interaction, query, limit, corpus_val)
        return

    try:
        triples = vecgrep_client.search_corpus_to_ids_with_match(
            query.strip(),
            corpus_val,
            top_k=max(limit, 10),
            want_kind=want_kind,
        )
    except vecgrep_client.VecgrepUnavailable as e:
        await interaction.followup.send(f"⚠️ vecgrep unavailable: {e}", ephemeral=True)
        return
    triples = triples[:limit]
    if not triples:
        await interaction.followup.send(_vecgrep_header(query, corpus_val, kind_val, 0) + "\n_(no hits)_")
        return

    by_id_mem = {x.get("id"): x for x in store.load_memories()}
    by_id_jou = {x.get("id"): x for x in store.load_journal()}
    rows: list[str] = []
    rank = 0
    for eid, pct, matched_by in triples:
        is_journal = (
            want_kind == "journal"
            or (want_kind is None and eid in by_id_jou and eid not in by_id_mem)
        )
        if is_journal:
            e = by_id_jou.get(eid)
            if not e:
                continue
            label = f"j#{eid}"
            name = (e.get("text") or "").splitlines()[0][:55]
        else:
            e = by_id_mem.get(eid)
            if not e:
                continue
            label = f"#{eid}"
            name = e.get("name") or (e.get("text") or "").splitlines()[0][:55]
        rank += 1
        tags = _row_extra_tags(e, max_len=30)
        body_txt = f"{name}  · {tags}" if tags else name
        rows.append(_vecgrep_row(rank, pct, matched_by, label, body_txt))

    body = "```ansi\n" + "\n".join(rows) + "\n```"
    await interaction.followup.send(_vecgrep_header(query, corpus_val, kind_val, len(rows)) + "\n" + body)


def _vecgrep_header(query: str, corpus_val: str, kind_val: str, n: int) -> str:
    """Multi-line header — bold name on its own line, then field rows.

    Footer carries the V/K legend so users can see at a glance whether a hit
    is keyword-driven (BM25) or semantic (vector) or both — critical because
    pct alone is misleading for short queries.
    """
    desc = _CORPUS_DESCRIPTIONS.get(corpus_val, corpus_val)
    if corpus_val == _CORPUS_STORE and kind_val != "both":
        scope = f"{desc} (filtered to {kind_val} only)"
    else:
        scope = desc
    plural = "s" if n != 1 else ""
    return (
        f"**vecgrep**\n"
        f"query: `{query}`\n"
        f"scope: {scope}\n"
        f"**{n}** result{plural}  · _#=rank, %=confidence; "
        f"V=semantic K=keyword VK=both_"
    )


async def _vecgrep_render_paths(
    interaction: discord.Interaction,
    query: str,
    limit: int,
    corpus_val: str,
) -> None:
    """Path-keyed corpora have file-path source_ids, not memory/journal IDs.
    Render parent-dir/filename + similarity pct, color-coded by tier.
    """
    try:
        hits = vecgrep_client._post_search(
            query.strip(), corpus_val, top_k=max(limit, 10),
        )
    except vecgrep_client.VecgrepUnavailable as e:
        await interaction.followup.send(f"⚠️ vecgrep unavailable: {e}", ephemeral=True)
        return
    if not hits:
        await interaction.followup.send(_vecgrep_header(query, corpus_val, "both", 0) + "\n_(no hits)_")
        return

    # Dedupe by source_id, keep highest pct + union matched_by, preserve order.
    seen_pct: dict[str, float] = {}
    seen_by: dict[str, set[str]] = {}
    order: list[str] = []
    for h in hits:
        sid = h.get("source_id") or ""
        pct = float(h.get("similarity_pct", 0.0))
        by = set(h.get("matched_by") or [])
        if not sid:
            continue
        if sid not in seen_pct:
            seen_pct[sid] = pct
            seen_by[sid] = by
            order.append(sid)
        else:
            seen_pct[sid] = max(seen_pct[sid], pct)
            seen_by[sid] |= by

    rows: list[str] = []
    rank = 0
    for sid in order[:limit]:
        pct = seen_pct[sid]
        matched_by = frozenset(seen_by[sid])
        # "<parent_dir>/<filename>" — full paths blow up the row width. The
        # path goes in the label slot (it's the identifier for file corpora).
        parent = os.path.basename(os.path.dirname(sid)) or "/"
        base = os.path.basename(sid)
        rank += 1
        rows.append(_vecgrep_row(rank, pct, matched_by, f"{parent}/{base}", ""))

    body = "```ansi\n" + "\n".join(rows) + "\n```"
    await interaction.followup.send(_vecgrep_header(query, corpus_val, "both", len(rows)) + "\n" + body)


client.tree.add_command(mem_group)
client.tree.add_command(jou_group)
client.tree.add_command(bot_group)
client.tree.add_command(squad_group)
client.tree.add_command(persona_group)


@client.event
async def on_ready() -> None:
    log.info("logged in as %s (id %s)", client.user, client.user.id if client.user else "?")


# ─────────────────────────── main ───────────────────────────


def main() -> int:
    if not TOKEN:
        log.error("CCDK_DISCORD_TOKEN not set in env")
        return 1
    client.run(TOKEN, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
