"""Reusable-string fact store — anti-confabulation lookup table.

A fourth tier alongside memories (atomic durable facts), journal (moments),
and files (documents): a flat key→value table of *volatile literals* agents
should LOOK UP rather than regenerate from memory. Commit hashes, Discord
message_ids, ports, account IDs, absolute paths — the exact strings a model
will happily hallucinate if asked to recall them. This directly attacks a
real failure mode (models fabricate Discord message_ids and git commit hashes
when asked to recall them from memory): make the source of truth a cheap lookup.

Why a flat dict, not the append-list JsonStore the other tiers use: a fact is
a *single current truth*, overwritten on update. There's no history, no IDs,
no tombstones — `deploy-port` is whatever it is right now. A dict keyed by the
fact-name is the natural shape; the latest write wins and the old value is
gone (that's the point — stale literals are the enemy here).

Record shape (value of the dict, keyed by the slug):
  {key, value, note, updated_ts, updated_by}

Storage: facts.json in the shared DATA_DIR (same dir as memories/journal/files).
Every read hits disk fresh (no cache) — this is a low-write lookup table, and a
fresh read means external edits and other processes are always seen. Writes are
atomic (tmp + os.replace) and a missing/corrupt file reads as empty so a
garbled file can never crash an agent mid-turn.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

from store import DATA_DIR  # reuse the same shared data dir

log = logging.getLogger("ccdk.facts")

FACTS_FILE = os.path.join(DATA_DIR, "facts.json")

# Keys are slugs: lowercase alnum start, then alnum/dot/dash/underscore. This
# bans spaces, uppercase, and — critically — `/` and `..`, so a key can never
# be a path-traversal vector or look pathy (facts are flat, not nested).
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _valid_key(key: str) -> bool:
    return isinstance(key, str) and bool(_KEY_RE.match(key))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    """Read the whole fact table. Missing/corrupt/non-dict file → {} (never raises).

    Tolerance is deliberate: this file is read on hot paths, and a single bad
    write (or a half-flushed file from a crash) must degrade to "no facts",
    not take down the caller.
    """
    if not os.path.exists(FACTS_FILE):
        return {}
    try:
        with open(FACTS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log.warning("facts: failed to load %s: %s", FACTS_FILE, e)
        return {}
    return data if isinstance(data, dict) else {}


def _save(table: dict) -> None:
    """Atomically write the whole table: write tmp, fsync, os.replace.

    os.replace is atomic on POSIX, so a reader never sees a half-written file
    and a crash leaves either the old file or the new one — never a partial.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = FACTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(table, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, FACTS_FILE)


def set_fact(key: str, value: str, note: str = "", by: str = "") -> dict:
    """Upsert a fact. Validates the key slug, stamps updated_ts, returns the record.

    Raises ValueError on a bad key (spaces, uppercase, path-ish). The latest
    write wins — the prior value is overwritten, no history kept.
    """
    if not _valid_key(key):
        raise ValueError(
            f"invalid fact key {key!r}: must match [a-z0-9][a-z0-9._-]* "
            "(lowercase, no spaces, no slashes)"
        )
    table = _load()
    rec = {
        "key": key,
        "value": "" if value is None else str(value),
        "note": note or "",
        "updated_ts": _now(),
        "updated_by": by or "",
    }
    table[key] = rec
    _save(table)
    return rec


def get_fact(key: str) -> dict | None:
    """Return the record for `key`, or None if absent."""
    rec = _load().get(key)
    return rec if isinstance(rec, dict) else None


def list_facts() -> list[dict]:
    """All facts, sorted by key."""
    table = _load()
    return [table[k] for k in sorted(table) if isinstance(table[k], dict)]


def delete_fact(key: str) -> bool:
    """Delete a fact. True if it existed, False otherwise."""
    table = _load()
    if key not in table:
        return False
    del table[key]
    _save(table)
    return True


def search_facts(term: str) -> list[dict]:
    """Substring (case-insensitive) match over key + value + note. Sorted by key."""
    t = (term or "").lower()
    out = []
    for rec in list_facts():
        if (
            t in rec.get("key", "").lower()
            or t in rec.get("value", "").lower()
            or t in rec.get("note", "").lower()
        ):
            out.append(rec)
    return out
