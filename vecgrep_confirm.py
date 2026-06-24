"""Discord reaction-confirm glue for vecgrep write proposals.

When claude.ai proposes a write to vecgrep (via the propose_write / propose_edit
MCP tools), the proposal is inert until a HUMAN confirms it — that's the
anti-poisoning wall. This module moves that confirm gesture off the CLI and onto
a one-tap Discord reaction:

  1. vecgrep fires VECGREP_PROPOSE_HOOK on each new proposal -> propose_hook.py
     posts a card to the owner's private channel and records message_id ->
     proposal_id here.
  2. The owner taps ✅ (confirm) / ❌ (discard) on the card.
  3. discord_handler's on_raw_reaction_add verifies the reactor IS the owner
     (Discord user_id gate — same identity proof the "approved" override uses),
     looks the message up here, and runs confirm()/discard().

The wall is preserved exactly: a tap only counts when it comes from the owner's
verified user_id in the owner's own private channel. Bots can propose but never
confirm. Protected-tier docs (identity / credentials / medical) need MORE than a
tap — a reaction is too weak — so confirm_protected_needs_reply() flags them and
the handler asks for a typed reply that re-states the doc id instead.

vecgrep itself stays OSS-clean: it knows nothing about Discord. All the
channel/owner/PII-bearing logic lives in the deployment config (env vars /
owner_id file), not in this module.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from discord_card import post_message
from discord_card import read_bot_token as _read_primary_token
from discord_card import read_helper_token

# --- config (no PII here — owner/channel binding comes from env/file) --------

# The owner's Discord user_id. A reaction only confirms when it comes from this
# id — identity is provable from Discord itself, the same gate the kit's
# "approved"/"authorized" override uses.
#
# Resolution order (mirrors discord_passthrough.py's get_owner_id()):
#   1. CCDK_OWNER_DISCORD_USER_ID env var (preferred — works everywhere)
#   2. ~/.config/cc-discord-kit/owner_id file (single line, just the user_id)
#
# Fails closed when neither is set — any other behavior would let arbitrary
# Discord users confirm vecgrep writes.
_OWNER_ID_FILE = Path(
    os.environ.get("CCDK_OWNER_ID_FILE")
    or Path.home() / ".config" / "cc-discord-kit" / "owner_id"
)


def _get_owner_id() -> str:
    """Return the configured owner Discord user_id, or empty string if unset."""
    env = os.environ.get("CCDK_OWNER_DISCORD_USER_ID", "").strip()
    if env:
        return env
    try:
        if _OWNER_ID_FILE.is_file():
            return _OWNER_ID_FILE.read_text().strip().splitlines()[0].strip()
    except OSError:
        pass
    return ""


# The ONLY channel a confirm reaction is honored in — the owner's configured
# private channel. A card posted anywhere else (or a reaction in any other
# channel) is ignored, so a leaked card in a shared channel can't be tapped by
# someone else.
#
# Must be set via CCDK_VECGREP_CONFIRM_CHANNEL — no hardcoded default.
# If unset, post_proposal_card() returns an error and is_confirm_channel()
# returns False for every channel (fail-closed).
CONFIRM_CHANNEL_ID = os.environ.get("CCDK_VECGREP_CONFIRM_CHANNEL", "")

CONFIRM_EMOJI = "✅"
DISCARD_EMOJI = "❌"

# Path to the vecgrep CLI (the confirm/discard authority). Resolved once.
VECGREP_BIN = os.environ.get("VECGREP_BIN", os.path.expanduser("~/.local/bin/vecgrep"))

# Where vecgrep keeps its pending proposals (read-only, for the web pane + the
# protected-tier check). Mirrors vecgrep's get_settings().home / "write".
VECGREP_HOME = Path(os.environ.get("VECGREP_HOME", os.path.expanduser("~/.vecgrep")))
PENDING_DIR = VECGREP_HOME / "write" / "_pending"

# message_id -> {proposal_id, doc_id, corpus, is_edit, tier} so a reaction on a
# card can be mapped back to the proposal it represents. Persisted so it
# survives a handler restart between propose and tap.
CARD_MAP_PATH = Path(
    os.environ.get(
        "CCDK_VECGREP_CARD_MAP",
        os.path.expanduser("~/.local/state/cc-discord-kit/vecgrep-cards.json"),
    )
)


# --- token (the card MUST be posted by the bot that LISTENS for reactions) ---

def read_bot_token() -> str | None:
    """Resolve the helper-bot token — the bot that runs discord_handler's
    reaction listener. The card and the listener MUST be the same Discord
    identity so (a) the seeded ✅/❌ are correctly skipped as the bot's own
    reactions and (b) the bot can read reactions on its own message.

    Order: CCDK_HELPER_DISCORD_TOKEN env → ~/.config/cc-discord-kit/env →
    primary bot token as a last resort (a different bot, but still in the
    channel — the listener's own-reaction check still works, just less clean).
    """
    t = os.environ.get("CCDK_HELPER_DISCORD_TOKEN", "").strip()
    if t:
        return t
    env_path = os.path.expanduser("~/.config/cc-discord-kit/env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("CCDK_HELPER_DISCORD_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return _read_primary_token()


# --- card map (message_id <-> proposal) -------------------------------------

def _load_map() -> dict:
    try:
        return json.loads(CARD_MAP_PATH.read_text())
    except Exception:
        return {}


def _save_map(m: dict) -> None:
    CARD_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    CARD_MAP_PATH.write_text(json.dumps(m, indent=2))


def record_card(message_id: str, proposal: dict) -> None:
    """Remember that `message_id` is the card for this proposal."""
    m = _load_map()
    m[str(message_id)] = {
        "proposal_id": proposal["proposal_id"],
        "doc_id": proposal.get("doc_id"),
        "corpus": proposal.get("corpus"),
        "is_edit": proposal.get("is_edit", False),
        "tier": (proposal.get("meta") or {}).get("tier", "normal"),
    }
    _save_map(m)


def lookup_card(message_id: str) -> dict | None:
    return _load_map().get(str(message_id))


def forget_card(message_id: str) -> None:
    m = _load_map()
    if str(message_id) in m:
        del m[str(message_id)]
        _save_map(m)


# --- card rendering ----------------------------------------------------------

def render_proposal_card(proposal: dict) -> str:
    """A Discord card for a pending proposal. Tap ✅ to confirm, ❌ to discard.

    Tabular bits live in a fenced code block (mobile-friendly in Discord).
    """
    pid = proposal["proposal_id"]
    doc_id = proposal.get("doc_id", "?")
    corpus = proposal.get("corpus", "?")
    is_edit = proposal.get("is_edit", False)
    tier = (proposal.get("meta") or {}).get("tier", "normal")
    verb = "edit" if is_edit else "new entry"

    head = f"🧠 **vecgrep proposal — {verb}** `{doc_id}`"
    if tier == "protected":
        head += "  ·  🔒 PROTECTED"

    preview = (proposal.get("preview") or "").strip()
    if len(preview) > 700:
        preview = preview[:700] + " …"

    meta_lines = [
        f"corpus    : {corpus}",
        f"doc id    : {doc_id}",
        f"proposal  : {pid}",
        f"tier      : {tier}",
        "─" * 32,
        preview or "(no preview)",
    ]
    block = "```\n" + "\n".join(meta_lines) + "\n```"
    if tier == "protected":
        foot = (f"🔒 Protected — run `/proposal confirm {doc_id}` to write it "
                f"(a tap isn't enough). {DISCARD_EMOJI} still discards.")
    else:
        foot = f"Tap {CONFIRM_EMOJI} to confirm · {DISCARD_EMOJI} to discard"
    return f"{head}\n{block}\n{foot}"


# --- posting (called from the propose hook) ---------------------------------

def _post_message_with_id(
    token: str, channel_id: str, content: str
) -> tuple[bool, str, str | None]:
    """POST a message and return (ok, error, message_id).

    The kit's post_message() returns (ok, err) but not the message_id. We need
    the exact id from the POST response body to arm the card — fetching the
    "latest message in channel" afterwards races on a busy channel and risks
    arming the card against a different message (hardening note: a ❌ tap could
    then hit the wrong entry). One atomic POST+parse avoids the race entirely.
    """
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    body = json.dumps({
        "content": content,
        "allowed_mentions": {"parse": []},
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "vecgrep-confirm (1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            ok = 200 <= resp.status < 300
            if not ok:
                return (False, f"HTTP {resp.status}", None)
            data = json.loads(resp.read())
            mid = data.get("id")
            return (True, "", str(mid) if mid else None)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read()[:200].decode("utf-8", "replace")
        except Exception:
            err_body = ""
        return (False, f"HTTP {e.code}: {err_body!r}", None)
    except Exception as exc:
        return (False, f"{type(exc).__name__}: {exc}", None)


def post_proposal_card(proposal: dict, token: str | None = None) -> tuple[bool, str]:
    """Post the card to the owner's private channel and seed reaction options.

    Returns (ok, error). On success the message_id is recorded so a later
    reaction can be mapped back to this proposal.
    """
    if not CONFIRM_CHANNEL_ID:
        return (False, "CCDK_VECGREP_CONFIRM_CHANNEL not set")
    # Helper token so the central handler can edit this card in place on a
    # ✅/❌ tap (cross-bot edits 403 — posting with the same bot that holds the
    # reaction listener is the clean path).
    token = read_helper_token() or token or read_bot_token()
    if not token:
        return (False, "no DISCORD_BOT_TOKEN")
    content = render_proposal_card(proposal)
    ok, err, mid = _post_message_with_id(token, CONFIRM_CHANNEL_ID, content)
    if not ok:
        return (False, err)
    if mid:
        record_card(mid, proposal)
        tier = (proposal.get("meta") or {}).get("tier", "normal")
        # Protected cards don't get a tappable ✅ (tap is too weak) — only the
        # discard option; confirm is via typed reply.
        if tier != "protected":
            _add_reaction(token, CONFIRM_CHANNEL_ID, mid, CONFIRM_EMOJI)
        _add_reaction(token, CONFIRM_CHANNEL_ID, mid, DISCARD_EMOJI)
    return (True, "")


def _latest_own_message_id(token: str, channel_id: str) -> str | None:
    """DEPRECATED — do not use to bind a card to its tap-handlers.

    Returns the channel's most recent message, NOT necessarily this bot's just-
    posted card: on a busy channel a narrate/tool-trace/other-bot post can land
    between the POST and this fetch, so the card gets armed against the wrong
    message (a ❌ tap could then hit a different proposal). All card-arming paths
    now use _post_message_with_id() and take the exact returned id instead.
    Kept only so any stray caller fails safe rather than ImportError-ing."""
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit=1"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bot {token}",
                      "User-Agent": "vecgrep-confirm (1.0)"})
    try:
        import urllib.error
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read())
        return data[0]["id"] if data else None
    except Exception:
        return None


def edit_message(token: str, channel_id: str, message_id: str, content: str) -> bool:
    """PATCH a message's content (used to append a hint onto a just-posted card
    rather than spawning a separate, messier reply). Best-effort."""
    import json as _json
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
    data = _json.dumps({"content": content,
                        "allowed_mentions": {"parse": []}}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="PATCH",
        headers={"Authorization": f"Bot {token}",
                 "Content-Type": "application/json",
                 "User-Agent": "vecgrep-confirm (1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def delete_message(token: str, channel_id: str, message_id: str) -> bool:
    """DELETE a message (used to bump a sticky card to the bottom: post the new
    one, then delete the old). Best-effort."""
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
    req = urllib.request.Request(
        url, method="DELETE",
        headers={"Authorization": f"Bot {token}", "User-Agent": "vecgrep-confirm (1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _add_reaction(token: str, channel_id: str, message_id: str, emoji: str) -> bool:
    import urllib.parse
    e = urllib.parse.quote(emoji)
    url = (f"https://discord.com/api/v10/channels/{channel_id}/messages/"
           f"{message_id}/reactions/{e}/@me")
    req = urllib.request.Request(
        url, method="PUT",
        headers={"Authorization": f"Bot {token}",
                 "User-Agent": "vecgrep-confirm (1.0)", "Content-Length": "0"})
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def remove_reaction(token: str, channel_id: str, message_id: str, emoji: str) -> bool:
    """Remove ALL of one emoji from a message (the bot's seed + any user taps).
    DELETE .../reactions/{emoji}. Used to retire a card's no-longer-tappable
    options: the un-chosen ✅/❌ once the owner picks one, and both once the veto
    window elapses. Best-effort."""
    import urllib.parse
    e = urllib.parse.quote(emoji)
    url = (f"https://discord.com/api/v10/channels/{channel_id}/messages/"
           f"{message_id}/reactions/{e}")
    req = urllib.request.Request(
        url, method="DELETE",
        headers={"Authorization": f"Bot {token}",
                 "User-Agent": "vecgrep-confirm (1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def remove_all_reactions(token: str, channel_id: str, message_id: str) -> bool:
    """Remove EVERY reaction from a message in ONE call (needs MANAGE_MESSAGES).
    DELETE .../reactions. Used to clear a resolved choice card's number reactions
    at once — the per-emoji loop got rate-limited and left some behind. Best-
    effort."""
    url = (f"https://discord.com/api/v10/channels/{channel_id}/messages/"
           f"{message_id}/reactions")
    req = urllib.request.Request(
        url, method="DELETE",
        headers={"Authorization": f"Bot {token}",
                 "User-Agent": "vecgrep-confirm (1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


# --- identity gate + actions -------------------------------------------------

def _allowed_tappers() -> frozenset[str]:
    """Owner plus any CCDK_ALLOWED_TAPPERS members (comma-separated user IDs).

    These extra users may tap CHOICE / VETO / TODO cards. The vecgrep-confirm
    card stays owner-only regardless — it's gated by is_confirm_channel() (a
    channel-level wall), so loosening is_owner() here does NOT widen who can
    confirm a write proposal. Computed fresh each call so an env change applies
    on the next tap without a restart."""
    owner = _get_owner_id()
    extra = os.environ.get("CCDK_ALLOWED_TAPPERS", "")
    ids = ([owner] if owner else []) + [u.strip() for u in extra.split(",") if u.strip()]
    return frozenset(ids)


def is_owner(user_id) -> bool:
    """Owner OR any CCDK_ALLOWED_TAPPERS member can tap cards.

    Fail-closed: with no owner configured and no allowlist, returns False. The
    vecgrep-confirm wall is preserved by the separate is_confirm_channel() gate
    (the confirm card is only honored in the owner's private channel), so the
    allowlist only ever widens choice/veto/todo taps, never write-confirms."""
    allowed = _allowed_tappers()
    if not allowed:
        return False  # fail-closed — no config means no taps
    return str(user_id) in allowed


def is_confirm_channel(channel_id) -> bool:
    if not CONFIRM_CHANNEL_ID:
        return False  # fail-closed — no configured channel means no confirms
    return str(channel_id) == str(CONFIRM_CHANNEL_ID)


def confirm(proposal_id: str, ack: str | None = None) -> tuple[bool, str]:
    """Run `vecgrep confirm` for a proposal. Returns (ok, message).

    The human-authorization the wall requires is satisfied by the verified-owner
    reaction that got us here. `ack` re-states the doc id for protected tier.
    """
    cmd = [VECGREP_BIN, "confirm", proposal_id]
    if ack:
        cmd += ["--ack", ack]
    return _run_vecgrep(cmd)


def discard(proposal_id: str) -> tuple[bool, str]:
    """Run `vecgrep discard` for a proposal. Returns (ok, message)."""
    return _run_vecgrep([VECGREP_BIN, "discard", proposal_id])


def _run_vecgrep(cmd: list[str]) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}")
    out = (p.stdout or "").strip() or (p.stderr or "").strip()
    return (p.returncode == 0, out)


# --- pending listing (for the web pane) -------------------------------------

def list_pending() -> list[dict]:
    """Read vecgrep's pending proposals straight off disk (read-only). Powers
    the web 'pending' pane + the nav badge count. Newest first."""
    items: list[dict] = []
    if not PENDING_DIR.exists():
        return items
    for f in sorted(PENDING_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        items.append({
            "proposal_id": d.get("proposal_id"),
            "doc_id": d.get("doc_id"),
            "corpus": d.get("corpus"),
            "is_edit": d.get("is_edit", False),
            "tier": (d.get("meta") or {}).get("tier", "normal"),
            "origin": (d.get("meta") or {}).get("origin", "?"),
            "preview": (d.get("rendered") or "")[:800],
            "mtime": f.stat().st_mtime,
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def pending_count() -> int:
    if not PENDING_DIR.exists():
        return 0
    return sum(1 for _ in PENDING_DIR.glob("*.json"))
