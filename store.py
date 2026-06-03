"""Shared memory + journal store across multiple agents.

Single source of truth that any number of agents (Claude Code instances,
Discord bots, helper scripts, web UI) can read and write to. Writes are
atomic per-call (last-writer-wins, no locking — collisions are rare
given typical message cadence).

Two stores:
  memories.json  — durable facts (cap 200), always loaded into prompt
  journal.json   — pinned moments (cap 1000), recent slice loaded into prompt

A single freeform markdown rules doc (SHARED.md) of always-true rules every
agent boots with sits alongside them; it's injected full-text by the
SessionStart hook and edited via the web UI. Distinct from memories: memories
are atomic facts; SHARED.md is the coherent rulebook.

Data location is parametrized via the CCDK_DATA_DIR env var.
Default is `~/.local/share/cc-discord-kit/`.

Schema (memory entry):
  id, ts, type, name, text, tags, about?, bot?
    about: list[str]  — subject(s) the memory is about
    bot:   list[str] | null  — whitelist of agents that see this memory.
           For default-allow agents: unset = shared with all, set = scope to listed agents.
           For RESTRICTED_BOTS (default-deny): the agent must be explicitly in this
           list, otherwise the memory is hidden. Use `cc-discord-kit memory share <id> --with <bot>`.
    visibility: reserved for v2 — do not claim this name for anything else
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


def _resolve_data_dir() -> str:
    """Return the directory housing memories.json + journal.json."""
    explicit = os.environ.get("CCDK_DATA_DIR", "")
    if explicit:
        return os.path.expanduser(explicit)
    return os.path.expanduser("~/.local/share/cc-discord-kit")


DATA_DIR = _resolve_data_dir()
MEMORIES_FILE = os.path.join(DATA_DIR, "memories.json")
JOURNAL_FILE = os.path.join(DATA_DIR, "journal.json")
# SHARED.md — a single freeform markdown doc of always-true rules every agent
# boots with. Injected full-text by the SessionStart hook (same channel as
# feedback memories), edited via the web UI. Distinct from memories: memories
# are atomic facts; SHARED.md is the coherent rulebook.
SHARED_DOC_FILE = os.path.join(DATA_DIR, "SHARED.md")

MEMORIES_CAP = 200
JOURNAL_CAP = 1000

VALID_TYPES = {"user", "feedback", "project", "reference"}

# Agents that operate outside the trusted set. They default-DENY: they only
# see memories whose `bot` whitelist explicitly includes them. Trusted agents
# (everything not listed here) default-ALLOW: they see all shared memories plus
# any whose `bot` whitelist includes them. Empty by default — the machinery is
# kept for any future untrusted agent: add its name here.
RESTRICTED_BOTS: set[str] = set()

# Strip leading rendered-header lines that agents sometimes paste back when
# round-tripping content through `cc-discord-kit memory show`. This keeps
# metadata displays from compounding into the stored body across edits.
_RENDERED_HEADER_RE = re.compile(
    r'^(About: [^\n]*\n+Saved: [^\n]*\n+)+', re.MULTILINE
)


def _strip_rendered_header(text: str) -> str:
    return _RENDERED_HEADER_RE.sub('', text.lstrip())


class JsonStore:
    """Persistent list-of-dicts store with auto-incrementing IDs.

    Cache uses mtime-based invalidation so external writes (e.g. backfill
    scripts, another process) are picked up on the next load. Without this,
    a long-running Flask server holds stale data forever.
    """

    def __init__(self, file_path: str, max_entries: int) -> None:
        self._file_path = file_path
        self.max_entries = max_entries
        self._cache: list[dict] | None = None
        self._cache_mtime: float = 0.0

    def _invalidate(self) -> None:
        self._cache = None
        self._cache_mtime = 0.0

    def _file_mtime(self) -> float:
        try:
            return os.path.getmtime(self._file_path)
        except OSError:
            return 0.0

    def _load_raw(self) -> list[dict]:
        """Load all entries including soft-deleted tombstones. Used for ID allocation."""
        current_mtime = self._file_mtime()
        if self._cache is not None and current_mtime == self._cache_mtime:
            return list(self._cache)
        if not os.path.exists(self._file_path):
            return []
        try:
            with open(self._file_path, "r") as f:
                data = json.load(f)
            result = data if isinstance(data, list) else []
            self._cache = result
            self._cache_mtime = current_mtime
            return list(result)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to load %s: %s", self._file_path, e)
            return []

    def load(self) -> list[dict]:
        """Load live entries — tombstones (deleted=True) filtered out."""
        return [e for e in self._load_raw() if not e.get("deleted")]

    def save(self, entries: list[dict]) -> None:
        os.makedirs(os.path.dirname(self._file_path), exist_ok=True)
        try:
            tmp = self._file_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(entries, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self._file_path)
            self._cache = entries
            self._cache_mtime = self._file_mtime()
        except OSError as e:
            log.warning("Failed to save %s: %s", self._file_path, e)
            self._invalidate()

    @property
    def _maxid_path(self) -> str:
        """Sidecar that persists the all-time-high ID independently of the
        entries. Survives a tombstone purge ('clear trash'), so IDs never get
        reused even if every dead record is hard-deleted from the file."""
        return self._file_path + ".maxid"

    def _read_watermark(self) -> int:
        try:
            with open(self._maxid_path) as f:
                return int(f.read().strip() or 0)
        except (OSError, ValueError):
            return 0

    def _bump_watermark(self, value: int) -> None:
        """Persist a new high-water mark (monotonic — never lowered)."""
        if value <= self._read_watermark():
            return
        try:
            tmp = self._maxid_path + ".tmp"
            with open(tmp, "w") as f:
                f.write(str(value))
            os.replace(tmp, self._maxid_path)
        except OSError as e:
            log.warning("Failed to write watermark %s: %s", self._maxid_path, e)

    def next_id(self, entries: list[dict] | None = None) -> int:
        """Monotonic ID allocation: 1 + max over (a) all entries incl. tombstones
        and (b) the persisted watermark sidecar.

        Ignores any `entries` arg passed by older callers — always reads raw to
        guarantee monotonicity. Deleted IDs are never reused, and the watermark
        keeps that guarantee even after tombstones are purged from the file.
        Pre-fix installs have no sidecar (reads as 0) and fall back cleanly to
        max-over-entries; the sidecar is (re)created on the next add().
        """
        raw = self._load_raw()
        max_entry = max((e.get("id", 0) for e in raw), default=0)
        return max(max_entry, self._read_watermark()) + 1

    def add(self, extra_fields: dict) -> dict:
        raw = self._load_raw()
        new_id = self.next_id()
        entry = {
            "id": new_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            **extra_fields,
        }
        raw.append(entry)
        # Persist the high-water mark so the ID is never reused even if this
        # entry (or every tombstone) is later purged from the file.
        self._bump_watermark(new_id)
        # Cap counts only live entries; tombstones don't count toward the cap.
        live_count = sum(1 for e in raw if not e.get("deleted"))
        if live_count > self.max_entries:
            # Drop oldest live entries until we're under the cap, but keep
            # tombstones (they're cheap and preserve ID monotonicity).
            excess = live_count - self.max_entries
            kept = []
            dropped = 0
            for e in raw:
                if dropped < excess and not e.get("deleted"):
                    dropped += 1
                    continue
                kept.append(e)
            raw = kept
        self.save(raw)
        return entry

    def update(self, entry_id: int, fields: dict) -> bool:
        raw = self._load_raw()
        for e in raw:
            if e.get("id") == entry_id and not e.get("deleted"):
                e.update(fields)
                self.save(raw)
                return True
        return False

    def remove(self, entry_id: int) -> bool:
        """Soft-delete: mark tombstone, keep ID reserved forever."""
        raw = self._load_raw()
        for e in raw:
            if e.get("id") == entry_id and not e.get("deleted"):
                e["deleted"] = True
                e["deleted_ts"] = datetime.now(timezone.utc).isoformat()
                self.save(raw)
                return True
        return False

    def clear(self) -> int:
        """Soft-delete everything live. Tombstones remain to preserve ID monotonicity."""
        raw = self._load_raw()
        count = 0
        ts = datetime.now(timezone.utc).isoformat()
        for e in raw:
            if not e.get("deleted"):
                e["deleted"] = True
                e["deleted_ts"] = ts
                count += 1
        self.save(raw)
        return count


# ─────────────────────────── memories ───────────────────────────

_memories = JsonStore(MEMORIES_FILE, MEMORIES_CAP)


def load_memories() -> list[dict]:
    return _memories.load()


def load_memories_raw() -> list[dict]:
    """Return all memories including tombstones (entries with deleted=True).

    Use only for debugging — most callers want load_memories(), which filters
    tombstones the same way the rest of the read path does.
    """
    return _memories._load_raw()


def save_memory(text: str, *, type: str = "feedback", name: str = "",
                tags: list[str] | None = None,
                about: list[str] | None = None,
                bot: list[str] | None = None) -> dict:
    """Save a durable memory. Type: user|feedback|project|reference.

    about: free-form subject labels. Defaults to [].
    bot:   if set, only matching bots include this in default views. Default null.
    """
    if type not in VALID_TYPES:
        type = "feedback"
    fields: dict = {
        "type": type,
        "name": name.strip(),
        "tags": list(tags) if tags else [],
        "text": _strip_rendered_header(text.strip()),
        "about": list(about) if about else [],
    }
    if bot is not None:
        fields["bot"] = list(bot)
    return _memories.add(fields)


def edit_memory(memory_id: int, text: str | None = None, *,
                name: str | None = None, type: str | None = None,
                tags: list[str] | None = None,
                about: list[str] | None = None,
                bot: list[str] | None = None,
                pinned: bool | None = None) -> bool:
    fields: dict = {}
    if text is not None:
        fields["text"] = _strip_rendered_header(text.strip())
    if name is not None:
        fields["name"] = name.strip()
    if type is not None and type in VALID_TYPES:
        fields["type"] = type
    if tags is not None:
        fields["tags"] = list(tags)
    if about is not None:
        fields["about"] = list(about)
    if bot is not None:
        fields["bot"] = list(bot)
    if pinned is not None:
        fields["pinned"] = bool(pinned)
    if not fields:
        return False
    return _memories.update(memory_id, fields)


def remove_memory(memory_id: int) -> bool:
    return _memories.remove(memory_id)


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (chars/4). No tokenizer dep — keeps the Zone-B
    preload budget in the right order of magnitude, not exact."""
    return len(text) // 4


def search_memories(term: str) -> list[dict]:
    term_lower = term.lower()
    return [
        m for m in load_memories()
        if term_lower in m.get("text", "").lower()
        or term_lower in m.get("name", "").lower()
        or any(term_lower in t.lower() for t in m.get("tags", []))
        or any(term_lower in a.lower() for a in m.get("about", []))
    ]


def filter_memories(entries: list[dict] | None = None, *,
                    type: str | None = None,
                    about: list[str] | None = None,
                    bot: str | None = None,
                    show_all: bool = False) -> list[dict]:
    """Filter memories by type / about / bot.

    about: list of labels; entry matches if ANY of its `about` labels are in
           the filter list (OR semantics). Empty filter = no about filtering.
    bot:   the calling bot's name. Default view hides entries with `bot` set
           UNLESS the calling bot is in that list. show_all=True bypasses.
    """
    if entries is None:
        entries = load_memories()
    out = []
    for m in entries:
        if type and m.get("type") != type:
            continue
        if about:
            entry_about = m.get("about", []) or []
            if not any(label in entry_about for label in about):
                continue
        if not show_all:
            entry_bot = m.get("bot")
            if bot in RESTRICTED_BOTS:
                # Restricted bot: default-deny. Must be explicitly whitelisted.
                if not entry_bot or bot not in entry_bot:
                    continue
            elif entry_bot:
                # Trusted bot: hidden only if a whitelist exists and excludes us.
                if not bot or bot not in entry_bot:
                    continue
        out.append(m)
    return out


def share_memory(memory_id: int, with_bot: str) -> bool:
    """Add a bot to a memory's `bot` whitelist. Idempotent. No-op if already present."""
    target = next((m for m in load_memories() if m.get("id") == memory_id), None)
    if not target:
        return False
    current = list(target.get("bot") or [])
    if with_bot in current:
        return True
    current.append(with_bot)
    return _memories.update(memory_id, {"bot": current})


def unshare_memory(memory_id: int, with_bot: str) -> bool:
    """Remove a bot from a memory's `bot` whitelist. Idempotent.

    If the resulting whitelist is empty, the `bot` field is cleared entirely
    so the memory returns to shared-with-all visibility for trusted bots (and
    stays invisible to restricted bots, which is the safe default)."""
    target = next((m for m in load_memories() if m.get("id") == memory_id), None)
    if not target:
        return False
    current = list(target.get("bot") or [])
    if with_bot not in current:
        return True
    current = [b for b in current if b != with_bot]
    if current:
        return _memories.update(memory_id, {"bot": current})
    # Empty whitelist: pop the field entirely so the entry returns to the
    # "no bot field" state (shared with all for trusted bots, hidden for restricted).
    raw = _memories._load_raw()
    for e in raw:
        if e.get("id") == memory_id and not e.get("deleted"):
            e.pop("bot", None)
            _memories.save(raw)
            return True
    return False


def format_memories_for_prompt(*, bot: str | None = None,
                               types: list[str] | None = None,
                               exclude_types: list[str] | None = None) -> str:
    """Full memory dump — for SessionStart hooks (cached, paid once per session).

    If `bot` is provided, hides entries with a `bot` field that doesn't include
    the caller. Shared entries (no `bot` field) are always shown.
    `types` restricts to only those types; `exclude_types` drops those types.
    """
    entries = filter_memories(bot=bot)
    if types is not None:
        entries = [m for m in entries if m.get("type", "feedback") in types]
    if exclude_types is not None:
        entries = [m for m in entries if m.get("type", "feedback") not in exclude_types]
    if not entries:
        return ""
    lines = ["MEMORIES (durable facts shared across agents):"]
    by_type: dict[str, list[dict]] = {}
    for m in entries:
        by_type.setdefault(m.get("type", "feedback"), []).append(m)
    for t in ("user", "project", "feedback", "reference"):
        if t not in by_type:
            continue
        lines.append(f"\n[{t.upper()}]")
        for m in by_type[t]:
            header = f"#{m['id']}"
            if m.get("name"):
                header += f" {m['name']}"
            extras = []
            if m.get("tags"):
                extras.append(', '.join(m['tags']))
            if m.get("about"):
                extras.append("about: " + ', '.join(m['about']))
            if extras:
                header += f" ({' | '.join(extras)})"
            lines.append(f"- {header}")
            lines.append(f"  {m['text']}")
    return "\n".join(lines)


def format_memories_index(*, bot: str | None = None,
                          types: list[str] | None = None,
                          exclude_types: list[str] | None = None) -> str:
    """Compact index — name + type + tags only, no body. ~800 tokens for 60 entries.
    For UserPromptSubmit hooks where the full dump is too expensive every turn.
    Bot can `cc-discord-kit memory show <id>` to read any specific entry in full.
    `types` restricts to only those types; `exclude_types` drops those types.
    """
    entries = filter_memories(bot=bot)
    if types is not None:
        entries = [m for m in entries if m.get("type", "feedback") in types]
    if exclude_types is not None:
        entries = [m for m in entries if m.get("type", "feedback") not in exclude_types]
    if not entries:
        return ""
    lines = [
        "MEMORIES INDEX — names + tags only.",
        "Run `cc-discord-kit memory show <id>` to read full text of any entry.",
    ]
    by_type: dict[str, list[dict]] = {}
    for m in entries:
        by_type.setdefault(m.get("type", "feedback"), []).append(m)
    for t in ("user", "project", "feedback", "reference"):
        if t not in by_type:
            continue
        lines.append(f"\n[{t.upper()}]")
        for m in by_type[t]:
            head = f"#{m['id']} {m.get('name', '')}"
            tags = m.get("tags", [])
            if tags:
                head += f" ({','.join(tags[:3])})"
            about = m.get("about", [])
            if about:
                head += f" [{','.join(about)}]"
            lines.append(f"  {head}")
    return "\n".join(lines)


def _preload_log(msg: str) -> None:
    """Best-effort append to the boot log (same path/convention as the
    session-start hook's log()). Swallows OSError — logging must never
    break a session boot."""
    path = os.path.join(DATA_DIR, "boot_hook.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(f"{msg}\n")
    except OSError:
        pass


def format_memories_full_preload(*, bot: str | None = None, budget_tokens: int = 0,
                                 exclude_types=("feedback",)) -> tuple[str, list[int]]:
    """Zone-B: full memory bodies, newest-first, up to a token budget.
    budget_tokens=0 => unbounded. Greedily includes newest-first; drops the
    rest (logged) and returns (block_text, loaded_ids) so the caller can
    index-only the remainder. feedback excluded by default (already loaded
    full in Zone A)."""
    entries = filter_memories(bot=bot)
    entries = [m for m in entries
               if m.get("type", "feedback") not in exclude_types]
    if not entries:
        return "", []
    entries = sorted(entries, key=lambda m: -m.get("id", 0))
    total = len(entries)
    loaded, dropped, used = [], [], 0
    for m in entries:
        body = f"- #{m['id']} {m.get('name', '')}\n  {m.get('text', '')}"
        cost = estimate_tokens(body)
        if budget_tokens and used + cost > budget_tokens and loaded:
            dropped.append(m)
            continue
        loaded.append(m)
        used += cost
    if dropped:
        ids = ", ".join(f"#{m['id']}" for m in dropped)
        _preload_log(f"Zone-B budget {budget_tokens}t: dropped "
                     f"{len(dropped)} to index-only: {ids}")
    header = (f"MEMORIES (full text, {len(loaded)} of {total} loaded — "
              f"the rest are in the index above):")
    lines = [header]
    for m in loaded:
        head = f"- #{m['id']} {m.get('name', '')}"
        tags = m.get("tags", [])
        if tags:
            head += f" ({','.join(tags[:3])})"
        lines.append(head)
        lines.append(f"  {m.get('text', '')}")
    return "\n".join(lines), [m["id"] for m in loaded]


def _file_visible_to(rec: dict, bot: str | None) -> bool:
    """Mirror filter_memories' visibility rule for a file record.

    Restricted agents default-deny (must be explicitly whitelisted in `bot`);
    trusted agents see everything unless a whitelist exists that excludes them.
    bot=None (terminal/generic session) is treated as trusted.
    """
    entry_bot = rec.get("bot")
    if bot in RESTRICTED_BOTS:
        return bool(entry_bot) and bot in entry_bot
    if entry_bot:
        return bool(bot) and bot in entry_bot
    return True


def format_files_index(*, bot: str | None = None) -> str:
    """Compact manifest of the file store — name + tags + about, no body.

    The session-start digest indexes memories so agents know they exist; files
    got nothing, so they were a trove that never got pinged. This is the missing
    pull signal: one body-less line per file. An agent scans it and fetches any
    file it needs by id. Respects the same visibility model as memories.
    Returns "" when no files are visible — no stray header.
    """
    import files_store  # lazy: avoid a circular import at module load
    files = [f for f in files_store.load_files() if _file_visible_to(f, bot)]
    if not files:
        return ""
    lines = [
        "FILES INDEX — names + tags only.",
        "Fetch a file by id via the files API to read its contents.",
    ]
    for f in files:
        head = f"#{f['id']} {f.get('name', '')}"
        tags = f.get("tags", [])
        if tags:
            head += f" ({','.join(tags[:3])})"
        about = f.get("about", [])
        if about:
            head += f" [{','.join(about)}]"
        size = f.get("size")
        if size:
            head += f" · {size}B"
        lines.append(f"  {head}")
    return "\n".join(lines)


# ─────────────────────────── journal ───────────────────────────

_journal = JsonStore(JOURNAL_FILE, JOURNAL_CAP)


def load_journal() -> list[dict]:
    return _journal.load()


def load_journal_raw() -> list[dict]:
    """Return all journal entries including tombstones. Debugging only."""
    return _journal._load_raw()


def add_journal(text: str, *, source: str = "", actor: str = "",
                tags: list[str] | None = None,
                title: str = "") -> dict:
    """Pin a moment. source = 'discord:my-channel' / 'cli' / etc.
    actor = agent or user name.

    title — optional short heading, rendered like a memory name.
    """
    return _journal.add({
        "source": source,
        "actor": actor,
        "tags": list(tags) if tags else [],
        "title": title.strip(),
        "text": _strip_rendered_header(text.strip()),
    })


def remove_journal(entry_id: int) -> bool:
    return _journal.remove(entry_id)


def edit_journal(entry_id: int, text: str | None = None, *,
                 actor: str | None = None,
                 source: str | None = None,
                 tags: list[str] | None = None,
                 pinned: bool | None = None,
                 title: str | None = None) -> bool:
    fields: dict = {}
    if text is not None:
        fields["text"] = _strip_rendered_header(text.strip())
    if actor is not None:
        fields["actor"] = actor.strip()
    if source is not None:
        fields["source"] = source.strip()
    if tags is not None:
        fields["tags"] = list(tags)
    if pinned is not None:
        fields["pinned"] = bool(pinned)
    if title is not None:
        fields["title"] = title.strip()
    if not fields:
        return False
    return _journal.update(entry_id, fields)


def search_journal(term: str) -> list[dict]:
    term_lower = term.lower()
    return [
        e for e in load_journal()
        if term_lower in e.get("text", "").lower()
        or any(term_lower in t.lower() for t in e.get("tags", []))
    ]


def journal_recent(days: int = 7) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for e in load_journal():
        try:
            ts = datetime.fromisoformat(e.get("ts", "").replace("Z", "+00:00"))
            if ts >= cutoff:
                out.append(e)
        except (ValueError, TypeError):
            continue
    return out


def format_journal_for_prompt(days: int = 7) -> str:
    entries = journal_recent(days)
    if not entries:
        return ""
    lines = [f"JOURNAL (pinned moments, last {days} days):"]
    for e in entries:
        ts = e.get("ts", "")[:10]
        actor = e.get("actor", "")
        src = e.get("source", "")
        head = f"#{e['id']} [{ts}"
        if actor:
            head += f" by {actor}"
        if src:
            head += f" via {src}"
        head += "]"
        lines.append(f"- {head}")
        lines.append(f"  {e['text']}")
    return "\n".join(lines)


def _journal_between(after_days: int, before_days: int) -> list[dict]:
    """Journal entries with ts in [now-before_days, now-after_days) — the band
    OLDER than the full-text window but newer than the outer cutoff."""
    now = datetime.now(timezone.utc)
    lo = now - timedelta(days=before_days)
    hi = now - timedelta(days=after_days)
    out = []
    for e in load_journal():
        try:
            ts = datetime.fromisoformat(e.get("ts", "").replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if lo <= ts < hi:
            out.append(e)
    return out


def format_journal_older_index(*, after_days: int = 10,
                               before_days: int = 90) -> str:
    """Compact breadcrumb index of journal entries older than the full-text dump.

    The digest dumps the last `after_days` of journal in full; older entries
    (up to the cap) are invisible at session start, so a weeks-long thread
    silently falls off. This surfaces the day-(after..before) tail as one
    truncated line each — date + a title/first-60-chars snippet — an agent can
    fetch in full if relevant. Returns "" when the window is empty.
    """
    entries = _journal_between(after_days, before_days)
    if not entries:
        return ""
    lines = [
        f"JOURNAL OLDER INDEX — days {after_days}–{before_days}, breadcrumbs only.",
        "Fetch any entry by id to read it in full.",
    ]
    for e in entries:
        ts = e.get("ts", "")[:10]
        title = (e.get("title") or "").strip()
        snippet = title or " ".join((e.get("text") or "").split())[:60]
        if not title and len(e.get("text") or "") > 60:
            snippet += "…"
        lines.append(f"  #{e['id']} [{ts}] {snippet}")
    return "\n".join(lines)


# ── SHARED.md — global rules doc ──────────────────────────────────────────────

def read_shared_doc() -> str:
    """Return the SHARED.md body, or "" if it doesn't exist / can't be read.

    Never raises — a missing or unreadable rules doc must not break callers
    (especially the SessionStart hook, where any exception would degrade boot).
    """
    try:
        with open(SHARED_DOC_FILE, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


def write_shared_doc(text: str) -> None:
    """Overwrite SHARED.md with `text`. Creates DATA_DIR if needed."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SHARED_DOC_FILE, "w", encoding="utf-8") as f:
        f.write(text)


def format_shared_doc_for_prompt(*, bot: str | None = None) -> str:
    """Wrap SHARED.md for SessionStart injection. "" if empty or restricted bot.

    Gated to trusted agents: any restricted bot (in RESTRICTED_BOTS) never
    receives the shared rulebook, mirroring how feedback memories are withheld
    from them. bot=None (terminal / generic session) is treated as trusted.
    """
    if bot in RESTRICTED_BOTS:
        return ""
    body = read_shared_doc()
    if not body.strip():
        return ""
    return (
        "SHARED RULES (SHARED.md — always-true rules for every agent; "
        "follow these like your own instructions):\n"
        f"{body.strip()}"
    )
