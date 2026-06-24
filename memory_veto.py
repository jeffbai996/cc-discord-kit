"""Veto window for memory / journal saves — the ❌-to-reject pattern.

Unlike vecgrep (default-DENY: a proposal is inert until a human ✅ confirms),
memory saves are default-ALLOW: the memory is written immediately (bots
are trusted, not injection vectors), but its confirmation card carries a
❌ revoke option for a short window. Tap ❌ within the window → the memory is
deleted. Silence → it stays (it's already saved). So the tap REJECTS junk; no
tap KEEPS. That matches "default to allow if no response in N minutes".

This reuses the identity gate + card-map + Discord primitives from
vecgrep_confirm — the second instance of the propose→card→tap pattern, and the
seam toward a generic helper once a third shows up.

The window is enforced at REACT time (no background timer needed): we stamp the
save time on the veto record; a ❌ after VETO_WINDOW_SECONDS is a no-op ("already
kept"). Silence costs nothing — the memory was saved up front.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import vecgrep_confirm as _vc  # shared owner gate + Discord REST primitives
from jsonlock import rmw_lock  # inter-process lock for the shared card map

REJECT_EMOJI = "❌"
KEEP_EMOJI = "✅"

# How long ❌ can still revoke (and free the id of) a freshly-saved memory before
# it auto-keeps. 6h is safe BECAUSE the card is sticky — it bumps itself to the
# channel bottom on every message, so it stays visible and won't auto-keep unseen.
# The window label everywhere (card hint, /pending) is derived from this constant
# via _fmt_window, so changing it here is the single source of truth — no hardcoded
# "2h"/"6h" strings to chase. Override with CCDK_VETO_WINDOW_SECONDS.
VETO_WINDOW_SECONDS = int(os.environ.get("CCDK_VETO_WINDOW_SECONDS", str(6 * 60 * 60)))

# Min seconds between sticky-card bumps, so a burst of channel chatter doesn't
# thrash the delete+repost cycle. Now that bumps fire on bot messages too (not
# just human chatter), 8s keeps the card glued near the bottom without churning
# reactions every second. Reposts are silent (no ping), so a tighter cadence is
# cheap. Override with CCDK_VETO_BUMP_COOLDOWN.
BUMP_COOLDOWN_SECONDS = int(os.environ.get("CCDK_VETO_BUMP_COOLDOWN", "8"))

# message_id -> {kind, entry_id, saved_at} so a ❌ on a save-card maps back to
# the memory/journal entry to delete, and we can check the window.
VETO_MAP_PATH = Path(
    os.environ.get(
        "CCDK_VETO_MAP",
        os.path.expanduser("~/.local/state/cc-discord-kit/veto-map.json"),
    )
)

# Which card kinds get a veto window + what ❌ does for each:
#   *_saved / *_added → ❌ REJECTS (deletes the just-written entry, frees the id)
#   *_edited          → ❌ UNDOES the edit (reverts to the before-snapshot)
#   *_deleted         → ❌ UNDOES the delete (restores the entry)
# ✅ always = keep the change (forget the veto). Saves are "is this worth
# keeping?"; edits/deletes are "did the bot just clobber something?" — same
# one-tap safety net, the action just differs.
SAVE_KINDS = {"memory_saved", "journal_added"}
EDIT_KINDS = {"memory_edited", "journal_edited"}
DELETE_KINDS = {"memory_deleted", "journal_deleted"}
# Deep-tier (.md reference FILE) kinds. File-backed, not store rows: the
# "entry_id" is the filename and the before-snapshot is the file content, so
# the undo rewrites the file. Saves aren't vetoed (a new deep file is low-risk);
# edits/deletes are — those can clobber long-form reference content.
DEEP_EDIT_KINDS = {"deep_edited"}
DEEP_DELETE_KINDS = {"deep_deleted"}
DEEP_KINDS = DEEP_EDIT_KINDS | DEEP_DELETE_KINDS
VETOABLE_KINDS = SAVE_KINDS | EDIT_KINDS | DELETE_KINDS | DEEP_KINDS


def _history_kind(action_kind: str) -> str:
	"""Map a card kind ('memory_edited') to a history kind ('memory')."""
	return "journal" if action_kind.startswith("journal") else "memory"


def _veto_target(action: dict) -> tuple[int | None, dict | None]:
	"""Pull (entry_id, before_snapshot) out of an action, across all kinds.
	save → ('entry'.id, None); edit → ('id', 'before'); delete → (before.id, 'before')."""
	kind = action.get("kind")
	if kind in SAVE_KINDS:
		return ((action.get("entry") or {}).get("id"), None)
	if kind in EDIT_KINDS:
		return (action.get("id"), action.get("before"))
	if kind in DELETE_KINDS:
		before = action.get("before") or {}
		return (before.get("id"), before)
	if kind in DEEP_KINDS:
		# fname is the key; before carries {fname, content, title, category}.
		return (action.get("fname"), action.get("before"))
	return (None, None)


def _load() -> dict:
	try:
		return json.loads(VETO_MAP_PATH.read_text())
	except Exception:
		return {}


def _save(m: dict) -> None:
	# Atomic write (tmp + os.replace): the sticky-bump fires on every channel
	# message now, so the discord client writes this far more often and can race
	# other writers. os.replace is atomic on POSIX, so a concurrent
	# reader/writer never sees a half-written file.
	VETO_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
	tmp = VETO_MAP_PATH.with_suffix(VETO_MAP_PATH.suffix + ".tmp")
	tmp.write_text(json.dumps(m, indent=2))
	os.replace(tmp, VETO_MAP_PATH)


def _entry_of(action: dict) -> dict:
	return action.get("entry") or {}


def _fmt_window(s: int) -> str:
	"""Human-friendly duration: '2h', '30m', '12h'."""
	if s % 3600 == 0:
		return f"{s // 3600}h"
	return f"{max(1, s // 60)}m"


def _window_label() -> str:
	return _fmt_window(VETO_WINDOW_SECONDS)


def attach(action: dict, chat_id: str, token: str | None, now: float,
		   message_id: str | None = None) -> bool:
	"""After a save-card was posted, seed ✅/❌ on it and record the veto window.

	Call this right after the existing card post. `token` MUST be the token the
	CARD was posted with — Discord only lets a bot edit/react-cleanly on its own
	message, so the editing bot must equal the posting bot (a cross-bot edit is
	a 403). `now` is the save timestamp (the store layer owns the clock).
	Returns True if a veto was armed. Best-effort: a failure just means no
	✅/❌ appeared; the memory is already saved either way.
	"""
	if action.get("kind") not in VETOABLE_KINDS:
		return False
	entry_id, before = _veto_target(action)
	if entry_id is None:
		return False
	# The card poster's credential. Prefer the helper token: the card is now
	# posted with it (so the central handler can edit the card in place on a tap),
	# and seeding ✅/❌ + decorating with the SAME token keeps poster == editor.
	# Falls back to the caller's token, then the local default.
	import discord_card
	card_tok = discord_card.read_helper_token() or token or discord_card.read_bot_token()
	if not card_tok:
		return False
	# Use ONLY the EXACT id of the card we just posted (passed in by the caller
	# from the POST response). Do NOT fall back to a "latest message in channel"
	# guess — on a busy bot channel a narrate/tool-trace post lands between the
	# post and the fetch, mis-binding the ❌ to a different message so a tap could
	# delete the WRONG memory. No exact id → don't arm.
	mid = message_id
	if not mid:
		return False
	with rmw_lock(VETO_MAP_PATH):
		m = _load()
		m[str(mid)] = {
			"kind": action["kind"],
			"entry_id": entry_id,
			# before-snapshot for edit/delete undo (None for saves).
			"before": before,
			"chat_id": str(chat_id),
			"saved_at": now,
			"bumped_at": now,
		}
		_save(m)
	_decorate_card(card_tok, str(chat_id), mid, action)  # network — outside the lock
	return True


def _decorate_card(card_tok: str, chat_id: str, mid: str, action: dict) -> None:
	"""Seed ✅/❌ on a freshly-posted card and edit the window hint into its code
	block. Shared by the first post (attach) and every sticky re-post (bump)."""
	import time as _time
	# Seed ✅ first so it reads left-to-right. Discord rate-limits back-to-back
	# reaction adds — a small gap keeps the 2nd from being silently dropped.
	_vc._add_reaction(card_tok, chat_id, mid, KEEP_EMOJI)
	_time.sleep(0.35)
	if not _vc._add_reaction(card_tok, chat_id, mid, REJECT_EMOJI):
		_time.sleep(0.6)
		_vc._add_reaction(card_tok, chat_id, mid, REJECT_EMOJI)
	# Hint as the last row INSIDE the code block (not a floating reply). ❌
	# means "reject" for a save but "undo" for an edit/delete — say the right one.
	k = action.get("kind", "")
	x_verb = "undo" if (k in EDIT_KINDS or k in DELETE_KINDS) else "reject"
	keeps = "auto-keeps" if k in SAVE_KINDS else "auto-keeps the change"
	hint = f"✅ approve · ❌ {x_verb} — {keeps} in {_window_label()}"
	try:
		import discord_card
		rendered = discord_card.format_card(action)
		if rendered:
			_vc.edit_message(card_tok, chat_id, mid, _hint_into_block(rendered, hint))
	except Exception:
		pass


def _action_for_record(rec: dict) -> dict | None:
	"""Reconstruct the card `action` for a veto record so a bump can re-render
	the same card. Returns None if the underlying entry is gone.

	Handles ALL vetoable kinds — saves, EDITS, and deletes. Edit/delete were
	previously unhandled, so their cards returned None and never got the
	sticky-bump: an edit card just scrolled away instead of staying pinned at
	the channel bottom. Edits re-render from the record's
	frozen `before` snapshot + the CURRENT stored `after`; deletes re-render
	from `before` alone (the entry is gone from the store)."""
	try:
		import store
		kind = rec.get("kind")
		eid = rec.get("entry_id")
		if kind == "memory_saved":
			e = next((x for x in store.load_memories() if x.get("id") == eid), None)
			return {"kind": "memory_saved", "entry": e} if e else None
		if kind == "journal_added":
			e = next((x for x in store.load_journal() if x.get("id") == eid), None)
			return {"kind": "journal_added", "entry": e} if e else None
		if kind == "memory_edited":
			after = next((x for x in store.load_memories() if x.get("id") == eid), None)
			return ({"kind": "memory_edited", "id": eid,
					 "before": rec.get("before"), "after": after}
					if after else None)
		if kind == "journal_edited":
			after = next((x for x in store.load_journal() if x.get("id") == eid), None)
			return ({"kind": "journal_edited", "id": eid,
					 "before": rec.get("before"), "after": after}
					if after else None)
		if kind == "memory_deleted":
			return {"kind": "memory_deleted", "before": rec.get("before")}
		if kind == "journal_deleted":
			return {"kind": "journal_deleted", "before": rec.get("before")}
	except Exception:
		return None
	return None


def bump_pending_cards(chat_id: str, now: float, token: str | None = None) -> int:
	"""Sticky-card behavior: re-post each in-window veto card for `chat_id` at
	the BOTTOM of the channel (post new, then delete old), so it stays visible
	instead of scrolling away — same idea as a standings board. A
	per-card cooldown stops channel chatter from thrashing the cycle. Returns
	how many cards were bumped.

	`token`: post with this bot token. The helper passes its OWN token so
	reposts go out as the identity that's actually a member of every channel it
	manages. Without it we fell back to the primary bot's token, which 403s in
	channels the primary bot isn't in — so the card never bumped.

	Best-effort throughout: a failed repost leaves the old card in place (never
	a zero-card gap), a failed delete just leaves a stale duplicate that the
	next tap/forget cleans up.
	"""
	m = _load()  # snapshot for iteration only; the real write re-reads under lock
	bumped = 0
	deltas: list[tuple[str, str, dict]] = []  # (old_mid, new_mid, new_rec)
	for old_mid, rec in list(m.items()):
		if str(rec.get("chat_id")) != str(chat_id):
			continue
		if not within_window(rec, now):
			continue
		if now - float(rec.get("bumped_at", rec.get("saved_at", 0))) < BUMP_COOLDOWN_SECONDS:
			continue
		action = _action_for_record(rec)
		if not action:
			continue
		import discord_card
		card_tok = token or discord_card.read_bot_token()
		if not card_tok:
			continue
		rendered = discord_card.format_card(action)
		if not rendered:
			continue
		# Post the replacement FIRST (never a zero-card window). silent=True:
		# a bump is a re-post of an already-seen card, so it must not ping.
		id_box: list = []
		ok, _ = _vc.post_message(card_tok, str(chat_id), rendered, silent=True, id_out=id_box)
		if not ok:
			continue
		new_mid = id_box[0] if id_box else None
		if not new_mid or new_mid == old_mid:
			continue
		_decorate_card(card_tok, str(chat_id), new_mid, action)
		deltas.append((old_mid, new_mid, {**rec, "bumped_at": now}))
		bumped += 1
		_vc.delete_message(card_tok, str(chat_id), old_mid)
	# Apply the id moves under a SHORT lock that RE-READS fresh state — the
	# snapshot read at the top is stale after seconds of network I/O, and a blind
	# _save(m) would clobber any attach()/forget() that landed meanwhile. Only
	# move cards still present in fresh, so a card forgotten (tapped) mid-bump
	# isn't resurrected.
	if deltas:
		with rmw_lock(VETO_MAP_PATH):
			fresh = _load()
			for old_mid, new_mid, new_rec in deltas:
				if old_mid in fresh:
					del fresh[old_mid]
					fresh[new_mid] = new_rec
			_save(fresh)
	return bumped


def finalize_expired(now: float, token: str | None = None) -> int:
	"""Settle cards whose veto window has elapsed: strip the ✅/❌ buttons (no
	longer tappable) and drop a SILENT 'auto-saved' note under the card, so it
	visibly closes instead of lingering with dead reactions.
	Forgets each settled card. Returns how many it closed.

	`token`: post/clean as this bot (the helper's own token), so it works
	in channels the primary bot can't reach.

	Driven by a ~minute background sweep in the discord client, so a card in a
	quiet channel still settles right at the window mark — not only when the
	next message happens to land."""
	m = _load()  # snapshot for iteration; the removal re-reads under the lock
	closed = 0
	settled: list[str] = []
	import discord_card
	card_tok = token or discord_card.read_bot_token()
	for mid, rec in list(m.items()):
		if within_window(rec, now):
			continue
		chat_id = str(rec.get("chat_id") or "")
		if card_tok and chat_id:
			# Clear ✅ AND ❌ in ONE call — removing them separately rate-limited
			# and left the ❌ behind on auto-approve.
			_vc.remove_all_reactions(card_tok, chat_id, str(mid))
			_vc.post_message(
				card_tok, chat_id,
				"🕐  auto-saved — the veto window elapsed, so this is locked in.",
				reply_to=str(mid), silent=True)
		settled.append(mid)
		closed += 1
	# Drop the settled cards under a SHORT lock that re-reads fresh state, so the
	# seconds of network I/O above can't make us clobber a concurrent attach()/
	# forget().
	if settled:
		with rmw_lock(VETO_MAP_PATH):
			fresh = _load()
			for mid in settled:
				fresh.pop(mid, None)
			_save(fresh)
	return closed


def _hint_into_block(rendered: str, hint: str) -> str:
	"""Insert `hint` as the last line INSIDE the card's trailing ``` code block.

	The card ends with `...body\\n```. We splice the hint (preceded by a rule)
	just before that closing fence so it reads as the final row of the block.
	Falls back to appending below the card if the shape isn't what we expect.
	"""
	fence = "```"
	idx = rendered.rstrip().rfind(fence)
	if idx <= 0 or not rendered.rstrip().endswith(fence):
		return rendered + "\n" + hint
	body = rendered.rstrip()[:idx].rstrip("\n")
	rule = "─" * 32
	return f"{body}\n{rule}\n{hint}\n{fence}"


def _collapse_hint(content: str, outcome: str) -> str:
	"""Replace the card's trailing hint row (the '✅ approve · ❌ reject …' line
	inside the code block) with the resolution `outcome` — collapses the prompt
	into the result IN PLACE on the card, so no separate
	'approved' reply is needed. Best-effort: returns the content unchanged if the
	shape isn't what we expect."""
	fence = "```"
	s = content.rstrip()

	def _swap(lines: list[str]) -> list[str]:
		# Replace a recognizable hint/outcome row, else append. Matching the
		# prior outcome too (✅/❌/🔒/⚠/🚫 …) lets a re-settle overwrite cleanly.
		if lines and ("approve" in lines[-1] or " · " in lines[-1]
					  or lines[-1].lstrip()[:1] in ("✅", "❌", "🔒", "⚠", "🚫", "🗑")):
			lines[-1] = outcome
		else:
			lines.append(outcome)
		return lines

	if s.endswith(fence):
		idx = s.rfind(fence)            # closing fence
		before = s[:idx].rstrip("\n")   # everything up to (not incl.) the fence
		return "\n".join(_swap(before.split("\n"))) + "\n" + fence
	# No trailing code block (e.g. one-line card): the hint, if present, is the
	# last plain line (appended by _hint_into_block's no-fence fallback). Collapse
	# it in place so the handler still edits inline instead of posting a separate
	# reply.
	return "\n".join(_swap(s.split("\n")))


def lookup(message_id: str) -> dict | None:
	return _load().get(str(message_id))


def forget(message_id: str) -> None:
	with rmw_lock(VETO_MAP_PATH):
		m = _load()
		if str(message_id) in m:
			del m[str(message_id)]
			_save(m)


def within_window(record: dict, now: float) -> bool:
	return (now - float(record.get("saved_at", 0))) <= VETO_WINDOW_SECONDS


def list_pending(now: float) -> list[dict]:
	"""Memory/journal saves still inside their veto window — a STANDING surface
	so a card that scrolled out of view isn't silently auto-kept unseen. Fed to
	the web /pending pane + nav badge. Newest first; closed-window entries are
	omitted (they're locked in) and lazily forgotten."""
	out: list[dict] = []
	m = _load()
	stale = []
	for mid, rec in m.items():
		if within_window(rec, now):
			secs_left = int(VETO_WINDOW_SECONDS - (now - float(rec.get("saved_at", 0))))
			kind = rec.get("kind")
			out.append({
				"message_id": mid,
				"kind": kind,
				"entry_id": rec.get("entry_id"),
				"chat_id": rec.get("chat_id"),
				"saved_at": rec.get("saved_at"),
				"secs_left": max(0, secs_left),
				"preview": _entry_preview(kind, rec.get("entry_id")),
				# Full current body (expand) + before-image (edit diff) for /pending.
				"full": _entry_full(kind, rec.get("entry_id")),
				"is_edit": kind in EDIT_KINDS,
				"before_full": _snapshot_text(rec.get("before")) if kind in EDIT_KINDS else "",
			})
		else:
			stale.append(mid)
	# Prune closed-window entries so the map doesn't grow unbounded — under a
	# short lock that re-reads fresh, so this read-path prune can't clobber a
	# concurrent attach()/forget().
	if stale:
		with rmw_lock(VETO_MAP_PATH):
			fresh = _load()
			for mid in stale:
				fresh.pop(mid, None)
			_save(fresh)
	out.sort(key=lambda x: x.get("saved_at", 0), reverse=True)
	return out


def pending_count(now: float) -> int:
	return len(list_pending(now))


def _entry_preview(kind: str | None, entry_id) -> str:
	"""A short label for a pending veto item so the web pane shows WHAT would be
	rejected, not just an id. Best-effort; '' if the entry's already gone."""
	try:
		import store
		if kind in DEEP_KINDS:
			return f"deep: {entry_id}"
		if kind == "memory_saved":
			e = next((x for x in store.load_memories() if x.get("id") == entry_id), None)
		elif kind == "journal_added":
			e = next((x for x in store.load_journal() if x.get("id") == entry_id), None)
		else:
			e = None
		if not e:
			return ""
		name = e.get("name") or ""
		text = (e.get("text") or "")[:160]
		return f"{name}: {text}" if name else text
	except Exception:
		return ""


def _entry_full(kind: str | None, entry_id) -> str:
	"""Full (untruncated) current body of a pending entry — any kind, so the web
	pane can expand the whole memory/journal text instead of the 160-char teaser."""
	try:
		import store
		label = "journal" if (kind or "").startswith("journal") else "memory"
		rows = store.load_journal() if label == "journal" else store.load_memories()
		e = next((x for x in rows if x.get("id") == entry_id), None)
		return _snapshot_text(e) if e else ""
	except Exception:
		return ""


def _snapshot_text(snap) -> str:
	"""name + text of an entry snapshot (the stored before-image, or a live row),
	so an edit can be shown as a before→after diff in the web pane."""
	if not isinstance(snap, dict):
		return ""
	name = (snap.get("name") or "").strip()
	text = (snap.get("text") or "").strip()
	return f"{name}\n\n{text}".strip() if name else text


def revoke(record: dict) -> tuple[bool, str]:
	"""Apply the ❌ for a veto record. Returns (ok, msg). Per kind:
	  save   → reject (hard-delete + free the id; it should look like it never happened)
	  edit   → undo the edit (revert to the before-snapshot)
	  delete → undo the delete (restore the entry)
	"""
	import store
	kind = record.get("kind")
	eid = record.get("entry_id")
	label = "memory" if (kind or "").startswith("memory") else "journal"
	try:
		if kind in SAVE_KINDS:
			ok = (store.reject_memory if label == "memory" else store.reject_journal)(int(eid))
			return (ok, f"{label} #{eid} rejected (id freed)" if ok else f"{label} #{eid} not found")
		if kind in EDIT_KINDS:
			import history
			ok = history.revert_edit({"kind": _history_kind(kind), "id": int(eid),
									  "before": record.get("before") or {}})
			return (ok, f"{label} #{eid} edit reverted" if ok else f"{label} #{eid} revert failed")
		if kind in DELETE_KINDS:
			import history
			res = history.restore_deleted({"kind": _history_kind(kind), "deleted": True,
										   "before": record.get("before") or {}})
			return (bool(res), f"{label} #{eid} restored" if res else f"{label} #{eid} restore failed")
		if kind in DEEP_KINDS:
			# File-backed undo: rewrite the deep .md from its before-snapshot.
			# eid is the filename; _deep_restore (in server) owns path safety,
			# the file write, index-line restore, and git-commit.
			import server
			before = record.get("before") or {}
			ok, msg = server._deep_restore(str(eid), before, deleted=(kind in DEEP_DELETE_KINDS))
			return (ok, msg)
	except Exception as e:
		return (False, f"{type(e).__name__}: {e}")
	return (False, f"unknown kind {kind}")
