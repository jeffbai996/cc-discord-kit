"""Tap-to-complete to-do cards — ✅ done / 🚫 cancel / ⭐ flag.

A posted `todo_added` card gets actionable reactions seeded; the owner taps one
and the helper applies it to the store (set_todo_status / set_todo_flag)
then settles the card. Mirrors choice_card / memory_veto: a message_id → todo_id
map persisted under a jsonlock so the helper can resolve a tap back to the todo.

✅ / 🚫 RESOLVE the card (status → done/cancelled, clear the reactions, drop a
confirmation reply). ⭐ TOGGLES the flag (Apple's star) and leaves the card
actionable so you can still finish or cancel it after.
"""
from __future__ import annotations

import json
import os
import time as _time
from pathlib import Path

import vecgrep_confirm as _vc  # shared owner gate + Discord REST primitives
from jsonlock import rmw_lock

DONE_EMOJI = "✅"  # green check
CANCEL_EMOJI = "🚫"
FLAG_EMOJI = "⭐"
ACTION_EMOJI = (DONE_EMOJI, FLAG_EMOJI, CANCEL_EMOJI)  # seed order, left→right

# message_id -> {todo_id, chat_id}
TODO_MAP_PATH = Path(
    os.environ.get("CCDK_TODO_MAP",
                   os.path.expanduser("~/.local/state/cc-discord-kit/todo_cards.json")))


def _load() -> dict:
    try:
        return json.loads(TODO_MAP_PATH.read_text())
    except Exception:
        return {}


def _save(m: dict) -> None:
    TODO_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TODO_MAP_PATH.with_suffix(TODO_MAP_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(m, indent=2))
    os.replace(tmp, TODO_MAP_PATH)


def _norm(e: str) -> str:
    # Discord sends keycaps/symbols with or without the U+FE0F variation
    # selector; compare without it (same fix choice_card needed).
    return (e or "").replace("️", "")


def emoji_action(emoji: str) -> str | None:
    """Map a reaction emoji to a todo action ('keep'|'cancel'|'flag'), or None.

    ✅ means KEEP (acknowledge, leave the todo active), NOT complete-via-tap.
    🚫 cancels, ⭐ flags. (DONE_EMOJI keeps its name for the seed order; only
    its meaning changed.)"""
    e = _norm(emoji)
    if e == _norm(DONE_EMOJI):
        return "keep"
    if e == _norm(CANCEL_EMOJI):
        return "cancel"
    if e == _norm(FLAG_EMOJI):
        return "flag"
    return None


def attach(todo_id: int, chat_id: str, token: str | None,
           message_id: str | None) -> bool:
    """Seed ☑️/⭐/🚫 on a freshly-posted todo card + track message_id → todo_id.

    `token` MUST be the token the card was posted with — Discord only lets a bot
    react cleanly on its OWN message. `message_id` is the exact id of the posted
    card (from the POST response). Best-effort: a failure just means no taps.
    """
    if not message_id:
        return False
    import discord_card
    # Helper token so the central handler can edit the card on a tap (cross-bot
    # edits 403); caller/local token are fallbacks.
    tok = discord_card.read_helper_token() or token or discord_card.read_bot_token()
    if not tok:
        return False
    with rmw_lock(TODO_MAP_PATH):
        m = _load()
        m[str(message_id)] = {"todo_id": int(todo_id), "chat_id": str(chat_id)}
        _save(m)
    # Seed with a small gap so Discord's reaction rate-limit doesn't drop later
    # ones (same pacing the veto/choice seeders use).
    for e in ACTION_EMOJI:
        _vc._add_reaction(tok, str(chat_id), str(message_id), e)
        _time.sleep(0.34)
    return True


def lookup(message_id: str) -> dict | None:
    return _load().get(str(message_id))


def forget(message_id: str) -> None:
    with rmw_lock(TODO_MAP_PATH):
        m = _load()
        if str(message_id) in m:
            del m[str(message_id)]
            _save(m)
