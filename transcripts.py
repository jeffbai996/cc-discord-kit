"""Discord channel transcript archiver — incremental polling + rolling retention.

Polls all configured digest channels every N minutes via the same Discord REST
path the digest UI uses. Appends new messages to per-channel-per-day JSONL
files, and writes a markdown shadow alongside that external search/indexing
tools can pick up automatically.

Layout:
    ~/.local/share/cc-discord-kit/transcripts/
        state.json                          # last_seen_message_id per channel
        <channel_name>/
            <YYYY-MM-DD>.jsonl              # append-only structured archive
            <YYYY-MM-DD>.md                 # search-friendly shadow

State file is the source of truth for "where did we leave off." We poll with
Discord's `after=<snowflake>` cursor, which guarantees no gaps even if the
poller misses a window.

Retention: prune any file (jsonl or md) whose date is older than RETENTION_DAYS.
Auto-prune runs on every poll cycle — cheap (just stat + unlink).

Idempotency: if the same message appears across two polls (rare, mostly an
edge case at the exact polling boundary), the JSONL append dedupes by message
id; the markdown shadow regenerates from the JSONL.

Usage:
    python -m transcripts                   # run one poll cycle, exit
    python -m transcripts --watch           # poll forever every POLL_INTERVAL
    python -m transcripts --reindex         # rebuild markdown shadows from jsonl

Env:
    CCDK_TRANSCRIPTS_DIR             override storage dir
    CCDK_TRANSCRIPTS_POLL_INTERVAL   seconds between poll cycles in --watch mode
    CCDK_TRANSCRIPTS_RETENTION_DAYS  prune older than this (default 365)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

# Reuse digest's token loader + per-channel fetcher — same Discord auth path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from digest import (  # noqa: E402
    DIGEST_CHANNELS,
    HARD_MESSAGE_CAP,
    _fetch_channel,
    _load_token,
)

log = logging.getLogger(__name__)

TRANSCRIPTS_DIR = Path(os.environ.get(
    "CCDK_TRANSCRIPTS_DIR",
    os.path.expanduser("~/.local/share/cc-discord-kit/transcripts"),
))
STATE_FILE = TRANSCRIPTS_DIR / "state.json"
POLL_INTERVAL_SEC = int(os.environ.get("CCDK_TRANSCRIPTS_POLL_INTERVAL", "300"))
RETENTION_DAYS = int(os.environ.get("CCDK_TRANSCRIPTS_RETENTION_DAYS", "365"))

# Discord epoch (2015-01-01 UTC) — used to convert ms timestamps to snowflakes.
DISCORD_EPOCH_MS = 1420070400000

_DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.(jsonl|md)$")


# ─────────────────────────── state ───────────────────────────


def _load_state() -> dict[str, str]:
    """Return last_seen_message_id per channel. Empty dict if no state yet."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        log.warning("state file unreadable, starting fresh")
        return {}


def _save_state(state: dict[str, str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)


def _initial_cursor_ms() -> int:
    """When no state exists, only fetch the last 7 days to bootstrap."""
    return int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp() * 1000)


# ─────────────────────────── per-day file paths ───────────────────────────


def _channel_dir(channel_name: str) -> Path:
    return TRANSCRIPTS_DIR / channel_name


def _jsonl_path(channel_name: str, day: str) -> Path:
    return _channel_dir(channel_name) / f"{day}.jsonl"


def _md_path(channel_name: str, day: str) -> Path:
    return _channel_dir(channel_name) / f"{day}.md"


def _ts_to_day(iso_ts: str) -> str:
    """ISO 8601 → 'YYYY-MM-DD' in UTC."""
    return iso_ts[:10]


# ─────────────────────────── append + write ───────────────────────────


def _append_messages(channel_name: str, msgs: list[dict]) -> set[str]:
    """Write each message to its day's jsonl file. Returns set of touched days."""
    touched_days: set[str] = set()
    if not msgs:
        return touched_days

    # Group by day so we open each file once
    by_day: dict[str, list[dict]] = {}
    for m in msgs:
        by_day.setdefault(_ts_to_day(m["ts"]), []).append(m)

    _channel_dir(channel_name).mkdir(parents=True, exist_ok=True)
    for day, day_msgs in by_day.items():
        jsonl = _jsonl_path(channel_name, day)
        # Dedup by id against existing file content (rare but possible)
        existing_ids: set[str] = set()
        if jsonl.exists():
            for line in jsonl.read_text().splitlines():
                try:
                    existing_ids.add(json.loads(line)["id"])
                except (json.JSONDecodeError, KeyError):
                    pass

        new = [m for m in day_msgs if m.get("id") not in existing_ids]
        if not new:
            continue
        with jsonl.open("a") as f:
            for m in new:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        touched_days.add(day)
    return touched_days


def _rebuild_md(channel_name: str, day: str) -> None:
    """Regenerate the markdown shadow for one day from its jsonl source.

    Markdown format mirrors what search indexers ingest well: YAML front-matter
    with channel + date, then one blockquote-style block per message with author
    and timestamp. We deliberately don't truncate — indexers chunk anyway.
    """
    jsonl = _jsonl_path(channel_name, day)
    md = _md_path(channel_name, day)
    if not jsonl.exists():
        # Source gone; remove the shadow too
        md.unlink(missing_ok=True)
        return

    msgs: list[dict] = []
    for line in jsonl.read_text().splitlines():
        try:
            msgs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    msgs.sort(key=lambda m: m.get("ts", ""))

    # Skip narration placeholders and tool-trace messages — both are
    # bot-internal surfacing (stream-of-consciousness narration; tool
    # call ticker / diffs) and have ~zero standalone retrieval value
    # for a search index. Including them roughly 2-3x's the corpus
    # size and drowns substantive replies in noise.
    def _is_narrate_or_tool(content: str) -> bool:
        c = (content or "").lstrip()
        return c.startswith("🧠 ") or c.startswith("🔧 ")

    indexable = [m for m in msgs if not _is_narrate_or_tool(m.get("content", ""))]

    parts: list[str] = [
        "---",
        f'channel: "{channel_name}"',
        f"date: {day}",
        f"messages: {len(indexable)}",
        "---",
        "",
        f"# #{channel_name} - {day}",
        "",
    ]
    for m in indexable:
        time_part = (m.get("ts", "")[11:16] or "??:??")
        author = m.get("author", "?")
        bot_marker = " [bot]" if m.get("is_bot") else ""
        content = m.get("content", "") or "(empty)"
        parts.append(f"**{author}{bot_marker}** . {time_part}")
        # Indent body under a blockquote so paragraphs stay attributed to the
        # speaker after the indexer's chunker.
        for line in content.splitlines() or [""]:
            parts.append(f"> {line}")
        parts.append("")
    md.write_text("\n".join(parts))


# ─────────────────────────── retention ───────────────────────────


def _prune_old(retention_days: int = RETENTION_DAYS) -> int:
    """Remove jsonl + md files older than retention_days. Returns count."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).date()
    removed = 0
    if not TRANSCRIPTS_DIR.exists():
        return 0
    for channel_dir in TRANSCRIPTS_DIR.iterdir():
        if not channel_dir.is_dir():
            continue
        for f in channel_dir.iterdir():
            m = _DAY_RE.match(f.name)
            if not m:
                continue
            try:
                day = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                f.unlink(missing_ok=True)
                removed += 1
    return removed


# ─────────────────────────── poll loop ───────────────────────────


def _poll_once(state: dict[str, str], token: str) -> dict[str, int]:
    """One poll cycle across all channels. Mutates state in-place.

    Returns {channel_name: messages_added} for visibility.
    """
    counts: dict[str, int] = {}
    for channel_name, channel_id in DIGEST_CHANNELS.items():
        last_id = state.get(channel_id)
        if last_id:
            # `after` snowflake is derived from the literal last_id we saved
            after_ms = ((int(last_id) >> 22) + DISCORD_EPOCH_MS)
        else:
            after_ms = _initial_cursor_ms()

        try:
            raw = _fetch_channel(channel_id, after_ms, token)
        except RuntimeError as e:
            log.warning("channel %s fetch failed: %s", channel_name, e)
            counts[channel_name] = 0
            continue

        msgs = []
        for m in raw:
            author = m.get("author", {}) or {}
            msgs.append({
                "channel": channel_name,
                "channel_id": channel_id,
                "id": m.get("id", ""),
                "author": author.get("username", "?"),
                "author_id": author.get("id", ""),
                "is_bot": bool(author.get("bot", False)),
                "content": m.get("content", "") or "",
                "ts": m.get("timestamp", ""),
            })

        if msgs:
            touched = _append_messages(channel_name, msgs)
            for day in touched:
                _rebuild_md(channel_name, day)
            # Update state to the largest id we just wrote
            state[channel_id] = max(m["id"] for m in msgs)
        counts[channel_name] = len(msgs)
    return counts


def read_window(start_date: str, end_date: str,
                channel_filter: list[str] | None = None) -> list[dict]:
    """Read all archived messages between start_date and end_date inclusive.

    Both dates are 'YYYY-MM-DD'. Returns oldest → newest, channel-interleaved
    (sorted by ts after the read).

    `channel_filter` takes display names — if set, restrict to those channels
    only. Default = all configured channels.

    Returns [] if the archive doesn't exist yet or no files match.
    """
    if not TRANSCRIPTS_DIR.exists():
        return []
    try:
        sd = datetime.strptime(start_date, "%Y-%m-%d").date()
        ed = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return []
    if sd > ed:
        sd, ed = ed, sd

    target_channels = channel_filter or list(DIGEST_CHANNELS.keys())
    out: list[dict] = []
    for channel_name in target_channels:
        channel_dir = _channel_dir(channel_name)
        if not channel_dir.exists():
            continue
        for f in sorted(channel_dir.glob("*.jsonl")):
            m = _DAY_RE.match(f.name)
            if not m:
                continue
            try:
                day = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < sd or day > ed:
                continue
            for line in f.read_text().splitlines():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    out.sort(key=lambda m: m.get("ts", ""))
    return out


def archive_stats() -> dict:
    """Quick summary of what's in the archive: counts per channel, date range."""
    stats: dict = {
        "channels": {},
        "earliest_day": None,
        "latest_day": None,
        "total_messages": 0,
    }
    if not TRANSCRIPTS_DIR.exists():
        return stats
    all_days: set[str] = set()
    for channel_name in DIGEST_CHANNELS:
        channel_dir = _channel_dir(channel_name)
        if not channel_dir.exists():
            stats["channels"][channel_name] = {"days": 0, "messages": 0}
            continue
        days = 0
        msgs = 0
        for f in channel_dir.glob("*.jsonl"):
            m = _DAY_RE.match(f.name)
            if not m:
                continue
            days += 1
            all_days.add(m.group(1))
            # Count lines without parsing each
            try:
                msgs += sum(1 for _ in f.open())
            except OSError:
                pass
        stats["channels"][channel_name] = {"days": days, "messages": msgs}
        stats["total_messages"] += msgs
    if all_days:
        sorted_days = sorted(all_days)
        stats["earliest_day"] = sorted_days[0]
        stats["latest_day"] = sorted_days[-1]
    return stats


def _reindex_all() -> int:
    """Rebuild every md shadow from its jsonl. Returns file count."""
    count = 0
    if not TRANSCRIPTS_DIR.exists():
        return 0
    for channel_dir in TRANSCRIPTS_DIR.iterdir():
        if not channel_dir.is_dir():
            continue
        for f in channel_dir.glob("*.jsonl"):
            _rebuild_md(channel_dir.name, f.stem)
            count += 1
    return count


# ─────────────────────────── entry points ───────────────────────────


def run_once() -> dict[str, int]:
    token = _load_token()
    if not token:
        log.error("no Discord token; cannot poll")
        return {}
    state = _load_state()
    counts = _poll_once(state, token)
    _save_state(state)
    pruned = _prune_old()
    if pruned:
        log.info("pruned %d transcript files older than %d days", pruned, RETENTION_DAYS)
    return counts


def run_forever(interval_sec: int = POLL_INTERVAL_SEC) -> None:
    log.info("transcript poller starting (interval=%ds)", interval_sec)
    while True:
        try:
            counts = run_once()
            total = sum(counts.values())
            if total:
                log.info("poll added %d messages: %s",
                         total, {k: v for k, v in counts.items() if v})
        except Exception:
            log.exception("poll cycle crashed")
        time.sleep(interval_sec)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="Discord transcript archiver")
    p.add_argument("--watch", action="store_true",
                   help="run forever, polling every POLL_INTERVAL_SEC")
    p.add_argument("--reindex", action="store_true",
                   help="regenerate all markdown shadows from jsonl, exit")
    p.add_argument("--prune-only", action="store_true",
                   help="just delete files older than retention, exit")
    args = p.parse_args(argv)

    if args.reindex:
        print(f"rebuilt {_reindex_all()} markdown shadows")
        return 0
    if args.prune_only:
        print(f"pruned {_prune_old()} files older than {RETENTION_DAYS} days")
        return 0
    if args.watch:
        run_forever()
        return 0
    print(json.dumps(run_once(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
