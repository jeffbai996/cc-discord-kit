"""Deferred-choice cards — the CHOOSE shape of the propose->card->tap pattern.

A bot that's genuinely unsure posts a numbered question card and ENDS its turn.
The owner taps 1️⃣/2️⃣/3️⃣… whenever; the tap posts the chosen option back into
the channel as a normal message, which re-enters the conversation as a fresh
prompt so the asking bot continues with the pick.

This is deliberately NOT a blocking AskUserQuestion replacement — nothing ever
suspends a turn, so there's no wedge risk (the reason AskUserQuestion is banned
in --channels sessions). If the owner never taps, the card just sits and expires
like the veto card; nothing hangs. For cases where a turn MUST block on the
answer, AskUserQuestion stays banned — this doesn't pretend to cover that.

Reuses vecgrep_confirm's owner gate + Discord primitives (3rd instance of the
kit, after vecgrep_confirm and memory_veto).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import vecgrep_confirm as _vc
from jsonlock import rmw_lock  # inter-process lock for the shared choice map

# 1-based number emojis. Discord renders the keycap sequence; we cap at 9 (a
# choice with >9 options is a smell — narrow it before asking).
NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣",
                "5️⃣", "6️⃣", "7️⃣", "8️⃣",
                "9️⃣"]
MAX_OPTIONS = len(NUMBER_EMOJI)

# How long an UNANSWERED choice card is kept tracked (so a late tap still finds
# it and the web surface lists it). This no longer gates the tap itself — a pick
# is honored whenever you make it (see _handle_choice). It only bounds how long
# an ignored card lingers before auto-pruning. 24h so a tap the next day still
# lands; override with CCDK_CHOICE_WINDOW_SECONDS.
CHOICE_WINDOW_SECONDS = int(os.environ.get("CCDK_CHOICE_WINDOW_SECONDS", str(24 * 60 * 60)))
BUMP_COOLDOWN_SECONDS = int(os.environ.get("CCDK_CHOICE_BUMP_COOLDOWN", "8"))

# message_id -> {question, options[], asked_by, asked_at, bumped_at, chat_id}
CHOICE_MAP_PATH = Path(
    os.environ.get(
        "CCDK_CHOICE_MAP",
        os.path.expanduser("~/.local/state/cc-discord-kit/cc-choice-cards.json"),
    )
)


def _bot_credential() -> str | None:
    """The posting bot's credential (Discord lets a bot react/edit only on its
    own message). Factored out so callers don't repeat the import."""
    import discord_card
    return discord_card.read_bot_token()


# ─── trusted relay: deliver a tap result back to the asking agent session ───
# A choice answer is useless if it dead-ends at the helper bot — the agent that
# ASKED needs it. The Claude Code Discord plugin drops all bot messages, so we
# can't just post the pick. The discord_plugin_patch adds a verified-relay path:
# a message from the helper id carrying ⟦vc-relay:<hmac>⟧ is delivered to the
# agent's session as a normal prompt. Here we write the shared config (helper id
# + secret) the patch reads, and sign relay messages with it.

# Where the patched plugin reads relay config. Each bot's in-process plugin reads
# relay.json from its OWN state dir, so in a MULTI-BOT setup the config must
# physically exist in every asking bot's dir — the helper that posts the signed
# relay runs in its own env and can't know which bot will ask. Single-bot setups
# just use CCDK_RELAY_CONFIG (one path); multi-bot setups set CCDK_RELAY_CONFIGS
# to an os.pathsep-separated list of relay.json paths (one per bot state dir).
# Writing only one path (the old behavior) left every OTHER bot's plugin with no
# config → a ⟦vc-relay⟧ tap leaked as visible text and never reached the session.
RELAY_CONFIG_PATH = os.path.expanduser(
    os.environ.get("CCDK_RELAY_CONFIG",
                   "~/.config/cc-discord-kit/relay.json"))


def _relay_config_paths() -> list[str]:
    """All relay.json paths to write. CCDK_RELAY_CONFIGS (os.pathsep-separated)
    fans out to every bot state dir in a multi-bot setup; otherwise the single
    CCDK_RELAY_CONFIG path."""
    multi = os.environ.get("CCDK_RELAY_CONFIGS", "").strip()
    if multi:
        return [os.path.expanduser(p) for p in multi.split(os.pathsep) if p.strip()]
    return [RELAY_CONFIG_PATH]
# Persistent secret the helper signs with (must match what the patch reads).
RELAY_SECRET_PATH = os.path.expanduser(
    os.environ.get("CCDK_RELAY_SECRET",
                   "~/.config/cc-discord-kit/relay_secret"))
# The helper bot's own user id (it posts the signed relay). Must be configured
# via CCDK_RELAY_HELPER_ID — no hardcoded default. Fails closed when unset:
# relay-trust checks reject any sender whose id is empty or doesn't match.
RELAY_HELPER_ID = os.environ.get("CCDK_RELAY_HELPER_ID", "")


def _relay_secret() -> str:
    """Load (or first-time generate) the HMAC secret shared with the patch."""
    import secrets
    try:
        s = open(RELAY_SECRET_PATH).read().strip()
        if s:
            return s
    except OSError:
        pass
    s = secrets.token_hex(32)
    os.makedirs(os.path.dirname(RELAY_SECRET_PATH), exist_ok=True)
    fd = os.open(RELAY_SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(s)
    return s


def _self_id_for_path(path: str) -> str:
    """Which bot's state dir does this relay.json path live in? Matched
    against bot_registry()'s access_json parent dir (relay.json lives in the
    same STATE_DIR). Empty string if no match — that bot's relay.json stays in
    broadcast mode (accepts any target) until it's in the agents registry."""
    import bot_config
    target_dir = os.path.normpath(os.path.dirname(os.path.expanduser(path)))
    for name, entry in bot_config.bot_registry().items():
        aj = entry.get("path")
        if aj and os.path.normpath(os.path.dirname(os.path.expanduser(aj))) == target_dir:
            return name
    return ""


def ensure_relay_config() -> None:
    """Write relay.json (helper id + secret + the identity the relayed prompt
    should appear from, plus THIS bot's own self_id) where the patched plugin
    reads it. Idempotent.

    self_id is what lets the verifier drop a relay addressed to a DIFFERENT
    bot instead of delivering it to every bot listening in the channel (the
    choice-tap over-broadcast fix, 2026-07-01) — derived per-path from
    bot_registry(), so a bot not yet in the registry falls back to self_id=""
    (old broadcast-to-all behavior, unchanged).

    Fails closed when CCDK_RELAY_HELPER_ID is unset — the config is written with
    an empty helper_id, which the patch treats as no trusted relay source."""
    secret = _relay_secret()
    for path in _relay_config_paths():
        cfg = {
            "helper_id": RELAY_HELPER_ID,
            "secret": secret,
            "relay_user": "choice-tap",
            "relay_user_id": _vc._get_owner_id(),  # the pick is the owner's decision
            "self_id": _self_id_for_path(path),
        }
        blob = json.dumps(cfg)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(blob)
        except OSError:
            # One unwritable dir must not stop the rest of the fan-out.
            continue


def sign_relay(chat_id: str, target_bot: str, payload: str) -> str:
    """Build the signed relay message the patched plugin will deliver. The HMAC
    signs chat_id+target_bot+payload — binds the marker to a channel AND (new,
    2026-07-01) the specific bot session it's meant for, so a marker can't be
    replayed into another channel, and isn't honored by every OTHER bot
    co-present in the same channel (the over-broadcast fix). target_bot=""
    preserves the old broadcast-to-all behavior for callers that don't know
    the asker."""
    import hashlib
    import hmac
    tgt = (target_bot or "").replace(":", "").replace("⟧", "")
    mac = hmac.new(_relay_secret().encode(), f"{chat_id}\n{tgt}\n{payload}".encode(),
                   hashlib.sha256).hexdigest()
    return f"⟦vc-relay:{tgt}:{mac}⟧ {payload}"


def deliver_pick(chat_id: str, payload: str, token: str | None = None,
                  target_bot: str = "") -> tuple[bool, str]:
    """Post the signed relay so the asking agent's session receives `payload`
    as a prompt. `target_bot` (the asker's bot name, e.g. a choice-card
    record's `asked_by`) is embedded + verified so other bots co-present in
    the channel don't also fire on the same tap. Returns (ok, error)."""
    ensure_relay_config()
    token = token or _bot_credential()
    if not token:
        return (False, "no bot token")
    return _vc.post_message(token, str(chat_id), sign_relay(str(chat_id), target_bot, payload))


def _load() -> dict:
    try:
        return json.loads(CHOICE_MAP_PATH.read_text())
    except Exception:
        return {}


def _save(m: dict) -> None:
    # Atomic write (tmp + os.replace) — bumps now fire on every channel message,
    # so this writes often enough to race other writers; os.replace is atomic.
    CHOICE_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHOICE_MAP_PATH.with_suffix(CHOICE_MAP_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(m, indent=2))
    os.replace(tmp, CHOICE_MAP_PATH)


def _norm_emoji(e: str) -> str:
    """Strip the VS16 variation selector (U+FE0F). Discord's gateway is
    inconsistent about whether a keycap reaction carries it (`1️⃣` vs `1⃣`),
    so we compare without it on both sides — otherwise a tap silently no-ops."""
    return (e or "").replace("️", "")


_NUMBER_NORM = [_norm_emoji(e) for e in NUMBER_EMOJI]


def emoji_to_index(emoji: str) -> int | None:
    """Map a number-keycap reaction to a 0-based option index, or None.
    VS16-insensitive (see _norm_emoji)."""
    try:
        return _NUMBER_NORM.index(_norm_emoji(emoji))
    except ValueError:
        return None


# Escape hatches every input card carries (input cards MUST always offer
# "type something" or "back out"). ✏️ = free-text your own answer,
# ❌ = back out (relays a cancel so the asking session doesn't hang).
TYPE_EMOJI = "✏️"
CANCEL_EMOJI = "❌"


def is_type(emoji: str) -> bool:
    return _norm_emoji(emoji) == _norm_emoji(TYPE_EMOJI)


def is_cancel(emoji: str) -> bool:
    return _norm_emoji(emoji) == _norm_emoji(CANCEL_EMOJI)


# Discord hard-caps a message at 2000 chars. Lengthy questions/options are
# fine (a code block has room) — we only trim if a card would actually overflow,
# so it degrades gracefully instead of silently failing to post.
DISCORD_LIMIT = 2000
_SAFE_LIMIT = 1900


def render_card(question: str, options: list[str], asked_by: str = "") -> str:
    """A numbered question card. Tabular bits in a code block (Discord
    monospace rule — raw pipe tables render as garbled text without it).
    No truncation unless the whole card would exceed Discord's 2000-char cap;
    then the longest options are trimmed just enough to fit."""
    head = "🗳️ **Input required**"
    if asked_by:
        head += f" — {asked_by}"

    def build(q: str, opts: list[str]) -> str:
        lines = [q.strip(), "─" * 32]
        for i, opt in enumerate(opts):
            lines.append(f"{i + 1}. {opt}")
        lines.append("─" * 32)
        lines.append("tap a number to choose")
        lines.append("✏️ type something  ·  ❌ back out")
        return f"{head}\n```\n" + "\n".join(lines) + "\n```"

    card = build(question, options)
    if len(card) <= _SAFE_LIMIT:
        return card
    # Overflow: trim the longest options first (the question stays intact —
    # it's what you're answering). Iterate a few times; cheap + bounded.
    opts = list(options)
    for _ in range(50):
        card = build(question, opts)
        if len(card) <= _SAFE_LIMIT:
            break
        longest = max(range(len(opts)), key=lambda i: len(opts[i]))
        if len(opts[longest]) <= 12:
            break  # can't usefully shrink further; let it post (may clip)
        opts[longest] = opts[longest][:len(opts[longest]) - 20].rstrip() + " …"
    return build(question, opts)[:DISCORD_LIMIT]


def ask(question: str, options: list[str], chat_id: str,
        asked_by: str = "", token: str | None = None,
        now: float = 0.0) -> tuple[bool, str]:
    """Post a numbered choice card + seed number reactions, record it, return.

    Best-effort and non-blocking by design. Returns (ok, message_id_or_error).
    `token` MUST be the posting bot's credential (Discord lets a bot react/edit
    only on its own message). `now` is the caller's timestamp.
    """
    options = [str(o).strip() for o in options if str(o).strip()]
    if not (2 <= len(options) <= MAX_OPTIONS):
        return (False, f"need 2-{MAX_OPTIONS} options, got {len(options)}")
    import discord_card
    # Post (and seed) with the helper bot's token so the central handler can
    # edit the card in place on a tap — cross-bot edits 403.
    token = discord_card.read_helper_token() or token or _bot_credential()
    if not token:
        return (False, "no bot token")
    id_box: list = []
    ok, err = _vc.post_message(token, str(chat_id), render_card(question, options, asked_by), id_out=id_box)
    if not ok:
        return (False, err)
    # Exact id from the POST response — never a racy latest-message re-fetch that
    # could bind the tap-card to a different message.
    mid = id_box[0] if id_box else None
    if not mid:
        return (False, "could not resolve posted message id")
    with rmw_lock(CHOICE_MAP_PATH):
        m = _load()
        m[str(mid)] = {
            "question": question, "options": options, "asked_by": asked_by,
            "asked_at": now, "bumped_at": now, "chat_id": str(chat_id),
        }
        _save(m)
    _seed_numbers(token, str(chat_id), mid, len(options))  # network — outside lock
    _seed_escapes(token, str(chat_id), mid)
    return (True, str(mid))


def _seed_numbers(token: str, chat_id: str, mid: str, n: int) -> None:
    """Seed 1..n number reactions, with a small gap so Discord's reaction
    rate-limit doesn't drop later ones."""
    import time as _time
    for i in range(min(n, MAX_OPTIONS)):
        if not _vc._add_reaction(token, chat_id, mid, NUMBER_EMOJI[i]):
            _time.sleep(0.5)
            _vc._add_reaction(token, chat_id, mid, NUMBER_EMOJI[i])
        _time.sleep(0.3)


def _seed_escapes(token: str, chat_id: str, mid: str) -> None:
    """Seed the two escape hatches (✏️ type / ❌ back out) AFTER the numbers, so
    every input card always offers them. Same paced retry as the numbers."""
    import time as _time
    for e in (TYPE_EMOJI, CANCEL_EMOJI):
        if not _vc._add_reaction(token, chat_id, mid, e):
            _time.sleep(0.5)
            _vc._add_reaction(token, chat_id, mid, e)
        _time.sleep(0.3)


def lookup(message_id: str) -> dict | None:
    return _load().get(str(message_id))


def forget(message_id: str) -> None:
    with rmw_lock(CHOICE_MAP_PATH):
        m = _load()
        if str(message_id) in m:
            del m[str(message_id)]
            _save(m)


def within_window(record: dict, now: float) -> bool:
    return (now - float(record.get("asked_at", 0))) <= CHOICE_WINDOW_SECONDS


def resolve(record: dict, index: int) -> str | None:
    """The chosen option text for a 0-based index, or None if out of range."""
    opts = record.get("options") or []
    return opts[index] if 0 <= index < len(opts) else None


def list_pending(now: float) -> list[dict]:
    """In-window choice cards — fed to the web /pending pane + nav badge so an
    unanswered question isn't lost if its card scrolls away. Prunes expired."""
    m = _load()
    out, stale = [], []
    for mid, rec in m.items():
        if within_window(rec, now):
            out.append({
                "message_id": mid, "question": rec.get("question"),
                "options": rec.get("options") or [], "asked_by": rec.get("asked_by"),
                "chat_id": rec.get("chat_id"),
                "secs_left": max(0, int(CHOICE_WINDOW_SECONDS - (now - float(rec.get("asked_at", 0))))),
            })
        else:
            stale.append(mid)
    if stale:
        with rmw_lock(CHOICE_MAP_PATH):
            fresh = _load()
            for mid in stale:
                fresh.pop(mid, None)
            _save(fresh)
    out.sort(key=lambda x: x.get("message_id", ""), reverse=True)
    return out


def bump_pending_cards(chat_id: str, now: float, token: str | None = None) -> int:
    """Sticky behavior: re-post in-window choice cards at the channel bottom so
    they don't scroll away. Same delete+repost-with-cooldown as memory_veto.

    `token`: the helper bot's own token, so reposts reach channels the primary
    bot isn't a member of."""
    m = _load()  # snapshot for iteration only; the write re-reads under the lock
    bumped = 0
    deltas: list[tuple[str, str, dict]] = []  # (old_mid, new_mid, new_rec)
    token = token or _bot_credential()
    if not token:
        return 0
    for old_mid, rec in list(m.items()):
        if str(rec.get("chat_id")) != str(chat_id):
            continue
        if not within_window(rec, now):
            continue
        if now - float(rec.get("bumped_at", rec.get("asked_at", 0))) < BUMP_COOLDOWN_SECONDS:
            continue
        card = render_card(rec.get("question", ""), rec.get("options") or [], rec.get("asked_by", ""))
        # silent=True: a bump re-posts an already-seen card; it must not ping.
        id_box: list = []
        ok, _ = _vc.post_message(token, str(chat_id), card, silent=True, id_out=id_box)
        if not ok:
            continue
        new_mid = id_box[0] if id_box else None
        if not new_mid or new_mid == old_mid:
            continue
        _seed_numbers(token, str(chat_id), new_mid, len(rec.get("options") or []))
        _seed_escapes(token, str(chat_id), new_mid)
        deltas.append((old_mid, new_mid, {**rec, "bumped_at": now}))
        bumped += 1
        _vc.delete_message(token, str(chat_id), old_mid)
    # Apply the id moves under a SHORT lock that re-reads fresh state, so the
    # network I/O above can't clobber a concurrent ask()/forget() (bump-clobber).
    if deltas:
        with rmw_lock(CHOICE_MAP_PATH):
            fresh = _load()
            for old_mid, new_mid, new_rec in deltas:
                if old_mid in fresh:
                    del fresh[old_mid]
                    fresh[new_mid] = new_rec
            _save(fresh)
    return bumped
