#!/usr/bin/env python3
"""Surface the bot's between-tool prose ("narration") to Discord.

This is NOT the LLM's extended-thinking trace — it's the plaintext
assistant text blocks that appear between tool calls in the session
transcript. the user sees them in the Claude Code terminal; the Discord
sender doesn't, which makes the bot look silent during long tasks.

This module is invoked in two phases of the hook lifecycle:

  PostToolUse  (--mode watch)     — after each tool call, pick up any
                                    new assistant text blocks that
                                    appeared in the transcript since
                                    the last firing and post / edit a
                                    narrate placeholder in Discord.
  Stop         (--mode finalize)  — at turn end, either delete the
                                    placeholder (collapse mode: real reply
                                    already landed, narration just
                                    collapses away) or convert it to a
                                    persistent quoted prefix (always
                                    mode).

Per-channel mode lives in <bot_root>/channels/discord/narrate.json:
  { "1234567890": "collapse" | "always" | "never" }
(Legacy "auto" is accepted as an alias for "collapse" and migrated on
read.) Default is "never" for every channel — opt-in.

Per-turn state lives in ~/.local/state/cc-discord-kit/
narrate_state.json keyed by "{bot_id}:{inbound_message_id}":
  { chat_id, placeholder_msg_id, last_transcript_line,
    buffer, mode, finalized }

The key is the Discord message_id of the inbound message this turn is
responding to — NOT the user-entry timestamp. message_id is the stable,
globally-unique turn identity. Timestamp keying broke when one bot was
hit in two channels at once: both turns share one session/transcript, so
"latest user message" (which timestamp keying derived from) flip-flopped
between the two channels mid-flight. That caused (1) narration landing in
the wrong channel — watch-mode resolved the target channel from the
latest user msg, so chat A's prose chased a chat B message into B's
channel — and (2) placeholders that never collapsed — at Stop, the
timestamp key resolved to whichever turn was newest, finalizing only it
and orphaning the other channel's placeholder forever. Keying by
message_id (paired with its chat_id from the SAME <channel> tag) makes
each inbound message its own turn, and finalize sweeps ALL unfinalized
turns at Stop so nothing is orphaned.

Hook input contract (stdin JSON, same shape Anthropic gives all hooks):
  { transcript_path, stop_hook_active, ... }

We reuse parsers from react_hook.py — same file watches the same
transcript so the two hooks stay in lockstep.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Any

# Reuse the existing transcript / Discord-tag parsers.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from react_hook import (# type: ignore[import-not-found]
    _bot_id,
    detect_discord_state_dir,
    read_bot_token,
    parse_discord_origins,
    _last_user_entry,
    assistant_called_discord_reply,
    count_discord_replies,
    _do_react,
    discord_unreact,
    EMOJI,
    _has_applied,
)

# Discord 2000-char per-message hard limit; leave headroom for our prefix.
DISCORD_LIMIT = 2000
# Bold+italic for the live "narrating…" marker so it's visually distinct
# from the body even at a glance. Always-mode marker stays plain bold —
# it's a header, not a status.
NARRATE_PREFIX_AUTO = "🧠 ***Narrating…***\n"
NARRATE_PREFIX_ALWAYS = "🧠 **Narration**\n"


_NBSP = " "  # non-breaking space — Discord preserves these at line start
_LIST_RE = re.compile(r"^(\s*)([\-*+]|\d+\.)\s")
_FENCE_SAFE = "`​`​`"  # ``` with zero-width spaces spliced between


def _blockquote(text: str) -> str:
    """Wrap text in Discord's `>>>` blockquote: neutralize fences, indent lists.

    SINGLE rendering boundary for narration — runs on the full concatenated
    buffer (not per-segment), so it sees fences and lists exactly as Discord
    will. Doing fence-neutralization here (rather than per text block in
    _clean_narrate_text) is what fixes the "salad" leak: a ``` spanning two
    watcher fires used to slip through because each half was neutralized
    separately but the concatenation re-formed a raw run. On the whole buffer
    there are no half-fences.

    Transforms, in order:

    1. Neutralize ``` runs → `​`​` (zero-width spaces between backticks). A
       real ``` inside `>>>` always wins and opens a fenced code block,
       turning narration into a salad of quoted + unquoted fragments. The
       ZWSP splice keeps the visual ``` at normal zoom but stops Discord's
       fence regex from firing. Since all fences die here, there's no
       in-code-fence state to track — everything downstream is prose.

    2. Indent list lines. List rendering inside `>>>` is flat — "1. foo" /
       "2. bar" sit flush with the quote bar as a wall of text. Discord
       collapses leading ASCII spaces but preserves U+00A0, so prefix every
       list line (-/*/+ bullets and N. numbers) with two NBSPs base indent
       plus one NBSP per original leading-whitespace char (nesting depth).

    3. Strip trailing whitespace so the block doesn't end on padded blanks.

    Fence-neutralization + list-marker handling used to ALSO live in
    _clean_narrate_text, which collided with this function (it rewrote
    "- foo" → "• foo" before this regex could match). That logic now lives
    ONLY here so there's one coherent owner.
    """
    text = text.replace("```", _FENCE_SAFE)
    out_lines: list[str] = []
    for raw in text.rstrip().splitlines():
        m = _LIST_RE.match(raw)
        if m:
            leading_ws = m.group(1)
            rest = raw[len(leading_ws):]
            out_lines.append(_NBSP * 2 + (_NBSP * len(leading_ws)) + rest)
            continue
        out_lines.append(raw)
    return ">>> " + "\n".join(out_lines)



def _fit_split(buf: str, prefix: str) -> tuple[str, str]:
    """Split buf into (head, tail) so prefix + _blockquote(head) fits the cap.

    The split lands on a natural boundary — paragraph, then line, then word —
    because the tail becomes its own blockquote segment and a mid-sentence cut
    reads as truncation (the exact bug this exists to fix). Rendering can
    LENGTHEN text (NBSP list indents, ZWSP fence splices), so the char budget
    is only a first guess; the loop re-verifies against the rendered form.
    Returns ("", buf) only if even a single word can't fit, which can't happen
    at Discord's 2000-char cap.
    """
    if len(prefix + _blockquote(buf)) <= DISCORD_LIMIT:
        return buf, ""
    budget = DISCORD_LIMIT - len(prefix) - 8
    head = buf[:budget]
    for sep in ("\n\n", "\n", " "):
        cut = head.rfind(sep)
        if cut > budget // 2:
            head = head[:cut]
            break
    while head and len(prefix + _blockquote(head)) > DISCORD_LIMIT:
        head = head[: int(len(head) * 0.9)].rstrip()
    return head, buf[len(head):].lstrip()


STATE_DIR = os.path.expanduser("~/.local/state/cc-discord-kit")
try:
    os.makedirs(STATE_DIR, exist_ok=True)
except OSError:
    pass
STATE_PATH = os.path.join(STATE_DIR, "narrate_state.json")
LOG_PATH = os.environ.get("CCDK_NARRATE_LOG", os.path.join(STATE_DIR, "narrate.log")
)


def log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Per-channel mode config
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Thread → parent resolution
# ---------------------------------------------------------------------------
#
# A Discord thread is a channel with its own id. Per-channel config
# (tools.json, narrate.json) is keyed by the *parent* channel id, so a
# thread inherits nothing of its parent's settings unless we map it back.
# The thread→parent mapping is immutable (a thread's parent never
# changes), so we cache it on disk: one Discord API GET per channel id
# *ever*, then free. We also cache plain channels (parent = "") so a
# non-thread is never re-queried.

_THREAD_CACHE_PATH = os.path.expanduser("~/.local/state/cc-discord-kit/thread_parents.json")
# Discord channel types that are threads: 10 announcement, 11 public, 12
# private. Everything else is a normal channel with no parent to inherit.
_THREAD_TYPES = (10, 11, 12)


def _load_thread_cache() -> dict:
    try:
        with open(_THREAD_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_thread_cache(cache: dict) -> None:
    # Lock-free: the cache is shared across co-located bots, but a lost
    # update only costs a re-query later, never correctness. Atomic
    # replace keeps the file from ever being half-written.
    try:
        os.makedirs(os.path.dirname(_THREAD_CACHE_PATH), exist_ok=True)
        tmp = f"{_THREAD_CACHE_PATH}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, _THREAD_CACHE_PATH)
    except OSError as e:
        log(f"thread cache write failed: {e}")


def channel_parent_id(chat_id: str) -> str | None:
    """Parent channel id if chat_id is a thread, else None.

    Cached on disk (the mapping is immutable). One Discord API GET per
    channel id ever; cache hits and plain channels are free. Returns None
    on any failure (no token, transient API error) — the caller then
    behaves as if it were a normal channel, so the worst case is "no
    inheritance this turn", never a crash."""
    cache = _load_thread_cache()
    if chat_id in cache:
        return cache[chat_id] or None
    token = read_bot_token(detect_discord_state_dir())
    if not token:
        return None  # can't resolve yet; don't poison the cache
    info = _discord_request("GET", f"https://discord.com/api/v10/channels/{chat_id}", token)
    if info is None:
        return None  # transient failure — leave it uncached so we retry
    parent = ""
    if info.get("type") in _THREAD_TYPES:
        parent = str(info.get("parent_id") or "")
    cache[chat_id] = parent
    _save_thread_cache(cache)
    return parent or None


def _channel_tools_mode(state_dir: str, chat_id: str) -> str:
    """Resolve the channel's `tools` mode from sibling tools.json.

    Used at narrate finalize time to decide whether tool messages
    should be deleted (collapse) or kept (ticker/diffs/full). A thread
    with no explicit entry of its own inherits the parent channel's
    setting. Returns 'off' on any missing-file or parse error."""
    path = os.path.join(state_dir, "tools.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return "off"
    if not isinstance(data, dict):
        return "off"
    if chat_id in data:
        val = data[chat_id]
        return val if isinstance(val, str) else "off"
    parent = channel_parent_id(chat_id)
    if parent and parent in data:
        val = data[parent]
        return val if isinstance(val, str) else "off"
    return "off"


def _narrate_config_path() -> str:
    """<bot_root>/channels/discord/narrate.json — sibling to access.json."""
    state_dir = detect_discord_state_dir()
    return os.path.join(state_dir, "narrate.json")


def channel_mode(chat_id: str) -> str:
    """Resolve the per-channel narrate mode. Default 'never' (opt-in).

    A thread with no explicit entry inherits the parent channel's mode."""
    path = _narrate_config_path()
    if not os.path.exists(path):
        return "never"
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return "never"
    if chat_id in cfg:
        val = cfg[chat_id]
    else:
        parent = channel_parent_id(chat_id)
        val = cfg.get(parent, "never") if parent else "never"
    # 'auto' is the legacy alias for 'collapse' — accept it for backwards
    # compatibility with narrate.json files that pre-date the rename
    if val == "auto":
        val = "collapse"
    if val not in ("never", "collapse", "always"):
        log(f"invalid mode {val!r} for chat {chat_id} — defaulting to never")
        return "never"
    return val


# ---------------------------------------------------------------------------
# Per-turn state
# ---------------------------------------------------------------------------

import fcntl
from contextlib import contextmanager

_LOCK_PATH = STATE_PATH + ".lock"


_lock_depth = 0  # per-process reentrancy counter (each narrate run is 1 process)


@contextmanager
def _state_lock():
    """Exclusive, REENTRANT file lock around read-modify-write of
    narrate_state.json.

    PostToolUse fires after every tool call, so multi-tool turns
    (Bash + Read + Edit clusters) trigger many concurrent invocations
    of this script. Without locking, two concurrent watchers can both
    read state showing 'no placeholder_msg_id', both decide to create
    one, and we end up with duplicate placeholders for the same turn.

    fcntl.flock() blocks the SECOND PROCESS until the first commits, and
    auto-releases on fd close. But flock is per-open-file-description: a
    SAME-process nested acquire opens a new fd whose LOCK_EX blocks on the
    fd already held by this very process — a self-deadlock (observed
    2026-06-20: a think-updater entered the lock, hit an already-finalized
    turn, and called _release_think_claim() which re-locks, wedging itself
    while holding the lock and starving every other bot on this shared lock).
    Reentrancy via a per-process depth counter fixes the whole class: we
    flock only on the outermost acquire and unlock only on the outermost
    release; nested acquires just pass through.
    """
    global _lock_depth
    if _lock_depth > 0:
        _lock_depth += 1
        try:
            yield
        finally:
            _lock_depth -= 1
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    f = open(_LOCK_PATH, "a")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        _lock_depth = 1
        yield
    finally:
        _lock_depth = 0
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


def _load_state() -> dict[str, Any]:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.rename(tmp, STATE_PATH)
    except OSError as e:
        log(f"state save failed: {e}")


def _turn_key_for_message(message_id: str) -> str:
    """Build the state key for an inbound Discord message_id.

    message_id is the stable, globally-unique turn identity. Pairing the
    key to the inbound message (rather than a timestamp that the
    transcript's "latest user entry" lookup resolves) is what keeps two
    interleaved channels from colliding on one key.
    """
    return f"{_bot_id()}:{message_id}"



def _turn_key(transcript_path: str) -> str | None:
    """Legacy turn key: (bot, current user-turn timestamp). KEPT because
    tool_watcher + agent_view import it. The message-id-keyed
    _turn_key_for_message above is preferred for multi-channel correctness;
    this timestamp variant is the older keying, retained for those callers.
    """
    user = _last_user_entry(transcript_path)
    if not user:
        return None
    ts = user.get("timestamp") or user.get("ts") or ""
    if not ts:
        return None
    return f"{_bot_id()}:{ts}"

def _current_origin(transcript_path: str) -> tuple[str, str] | None:
    """Resolve (chat_id, message_id) of the inbound message this turn answers.

    Reads the most recent real user entry and returns the LAST <channel>
    tag in it (document-order last == chronologically newest). chat_id and
    message_id come from the SAME tag, so routing and turn-keying can never
    disagree. Returns None if the latest user entry isn't a Discord-origin
    message (nothing to narrate).
    """
    user = _last_user_entry(transcript_path)
    user_text = _extract_user_text(user)
    origins = parse_discord_origins(user_text)
    if not origins:
        return None
    return origins[-1]


def _get_turn(state: dict[str, Any], key: str) -> dict[str, Any] | None:
    val = state.get(key)
    return val if isinstance(val, dict) else None


def _last_origin_for_channel(transcript_path: str, chat_id: str,
) -> tuple[str, str] | None:
    """Most recent inbound (chat_id, message_id) for a SPECIFIC channel.

    Scans the whole transcript newest→oldest for a user entry whose
    <channel> tag matches chat_id. Needed because under interleave the
    reply-target channel's inbound message may NOT be in the newest user
    entry — it can be several entries back. Returns None if none found.
    """
    if not chat_id or not transcript_path or not os.path.exists(transcript_path):
        return None
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "user":
            continue
        txt = _extract_user_text(obj)
        if not txt:
            continue
        for cid, mid in reversed(parse_discord_origins(txt)):
            if cid == chat_id:
                return (cid, mid)
    return None


def _ts_for_message(transcript_path: str, message_id: str) -> str:
    """Timestamp of the user entry whose <channel> tag carries message_id.

    Used so the byte-offset walker bounds the turn at the CHOSEN origin's
    user message (which may be earlier than the newest entry under interleave),
    not at whatever _last_user_entry returned. Empty string if not found.
    """
    if not message_id or not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "user":
            continue
        txt = _extract_user_text(obj)
        if not txt:
            continue
        for _cid, mid in parse_discord_origins(txt):
            if mid == message_id:
                return obj.get("timestamp") or obj.get("ts") or ""
    return ""


# ---------------------------------------------------------------------------
# Discord HTTP — send / edit / delete a message
# ---------------------------------------------------------------------------

def _discord_request(method: str, url: str, token: str, body: dict | None = None,
    _retry: bool = True,
) -> dict | None:
    data = None
    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": "cc-discord-kit-narrate (0.1)",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif method in ("POST", "PATCH"):
        # POST/PATCH with empty body still needs the length header
        data = b""
        headers["Content-Length"] = "0"

    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if 200 <= resp.status < 300:
                raw = resp.read()
                return json.loads(raw) if raw else {}
            log(f"{method} {url} → HTTP {resp.status}")
            return None
    except urllib.error.HTTPError as e:
        if e.code == 429 and _retry:
            retry_after = e.headers.get("Retry-After", "1")
            try:
                wait = max(0.1, min(3.0, float(retry_after)))
            except ValueError:
                wait = 1.0
            log(f"{method} 429 backoff {wait}s")
            time.sleep(wait)
            return _discord_request(method, url, token, body, _retry=False)
        log(f"{method} {url} HTTP {e.code} body={e.read()[:200]!r}")
        return None
    except Exception as e:
        log(f"{method} {url} failed: {e}")
        return None


def _fence_safe_truncate(content: str, limit: int = DISCORD_LIMIT) -> str:
    """Cap content to Discord's char limit WITHOUT orphaning a ``` fence.

    A naive content[:limit] can slice through (or drop) a block's closing
    ```; Discord then renders the rest of the channel as one runaway code
    block — the "unclosed code block" bug (2026-06-20). When the cut leaves
    an odd number of fences, trim back past any partial backticks and append
    a closing fence so the block always closes within the limit."""
    if len(content) <= limit:
        return content
    cut = content[:limit]
    if cut.count("```") % 2 == 0:
        return cut
    # Odd fence count -> a closing ``` was lost. Reserve room (newline +
    # 3 backticks) and re-close, stripping any partial backticks at the cut.
    trimmed = content[:limit - 4].rstrip("`").rstrip()
    if trimmed.count("```") % 2 == 1:
        trimmed += "\n```"
    return trimmed[:limit]


def discord_send_message(token: str, chat_id: str, content: str) -> str | None:
    """POST /channels/{id}/messages. Returns the new message_id or None."""
    url = f"https://discord.com/api/v10/channels/{chat_id}/messages"
    body = {"content": _fence_safe_truncate(content),
            "allowed_mentions": {"parse": []}}
    resp = _discord_request("POST", url, token, body)
    return resp.get("id") if resp else None


def discord_edit_message(token: str, chat_id: str, msg_id: str, content: str) -> bool:
    url = f"https://discord.com/api/v10/channels/{chat_id}/messages/{msg_id}"
    body = {"content": _fence_safe_truncate(content)}
    return _discord_request("PATCH", url, token, body) is not None


def discord_delete_message(token: str, chat_id: str, msg_id: str) -> bool:
    url = f"https://discord.com/api/v10/channels/{chat_id}/messages/{msg_id}"
    return _discord_request("DELETE", url, token) is not None


def discord_get_message(token: str, chat_id: str, msg_id: str) -> dict | None:
    """GET /channels/{id}/messages/{id}. Returns the message dict or None."""
    url = f"https://discord.com/api/v10/channels/{chat_id}/messages/{msg_id}"
    return _discord_request("GET", url, token)


def discord_latest_message_id(token: str, chat_id: str) -> str | None:
    """GET the channel's most-recent message id (limit=1), or None on failure.
    Lets a pinned-ish panel detect when it's been buried by ANY new message
    (not just the bot's own replies) so it can repost lower to stay visible."""
    url = f"https://discord.com/api/v10/channels/{chat_id}/messages?limit=1"
    resp = _discord_request("GET", url, token)
    if isinstance(resp, list) and resp:
        return resp[0].get("id")
    return None


# ---------------------------------------------------------------------------
# Transcript walker — pick up new assistant text since last_seen
# ---------------------------------------------------------------------------

def _byte_offset_after_current_user_turn(transcript_path: str, turn_ts: str,
) -> int:
    """Return the byte position just after the user-entry line matching turn_ts.

    Critical for first-run turn init: we only want to narrate text blocks
    that come AFTER the inbound message we're responding to. Initializing
    last_byte_offset at 0 would replay the entire session's history.

    Falls back to end-of-file (not 0) if the matching user entry isn't
    found — safer to narrate nothing than to flood with history.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return 0
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return 0
    try:
        offset = 0
        with open(transcript_path) as f:
            while True:
                line = f.readline()
                if not line:
                    break
                # The newline that .readline() included is also the file
                # separator we care about — offset = end-of-this-line
                next_offset = offset + len(line.encode("utf-8"))
                stripped = line.strip()
                if stripped:
                    try:
                        obj = json.loads(stripped)
                    except json.JSONDecodeError:
                        obj = {}
                    if (obj.get("type") == "user"
                            and (obj.get("timestamp") or obj.get("ts")) == turn_ts):
                        return next_offset
                offset = next_offset
    except OSError as e:
        log(f"user-turn search failed: {e}")
        return size
    # Not found — fall back to end-of-file so we narrate nothing
    # rather than flooding history.
    return size


def _new_text_blocks_since(transcript_path: str, last_byte_offset: int,
    *, skip_uuids: list[str] | None = None,
) -> tuple[list[str], list[str], int]:
    """Read the transcript starting at last_byte_offset.

    Return (text_block_contents, assistant_message_uuids, new_byte_offset).
    Text blocks are returned in document order — earliest first. The uuids
    list contains the assistant-message uuid for each message that yielded
    at least one text block (used by the prereply scan-back to dedupe).

    We track by byte offset rather than line index because the file is
    append-only JSONL and offset comparison is trivially monotonic.

    `skip_uuids` is the set of assistant-message uuids we've ALREADY
    incorporated. Their text blocks are skipped even if encountered in
    this read range — necessary because scan-back can absorb blocks
    physically PAST `last_byte_offset` (eof-snapshot race), leaving the
    offset behind blocks already consumed.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return [], [], last_byte_offset
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return [], [], last_byte_offset
    if size <= last_byte_offset:
        return [], [], last_byte_offset

    skip_set = set(skip_uuids or [])
    new_texts: list[str] = []
    new_uuids: list[str] = []
    try:
        with open(transcript_path) as f:
            f.seek(last_byte_offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                uuid = obj.get("uuid")
                if isinstance(uuid, str) and uuid in skip_set:
                    continue
                msg = obj.get("message", {})
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue
                added_any = False
                for c in content:
                    if (isinstance(c, dict)
                            and c.get("type") == "text"
                            and isinstance(c.get("text"), str)):
                        cleaned = _clean_narrate_text(c["text"])
                        if cleaned:
                            new_texts.append(cleaned)
                            added_any = True
                if added_any and isinstance(uuid, str) and uuid:
                    new_uuids.append(uuid)
    except OSError as e:
        log(f"transcript read failed: {e}")
        return [], [], last_byte_offset

    return new_texts, new_uuids, size


def _scan_turn_text_blocks(transcript_path: str, turn_ts: str, seen_uuids: list[str] | None,
) -> tuple[list[str], list[str]]:
    """Robust fallback: scan ALL assistant text blocks for the current turn.

    Used at prereply time as an option-A safety net for the post-tool race:
    `_new_text_blocks_since` reads from a byte offset that the post-bash
    watch tick already advanced past. If the model emitted text AFTER that
    advance but BEFORE the reply tool's prereply hook fires, the byte-
    offset-based read returns []. Result: prereply doesn't see the new
    prose, the placeholder isn't edited, and the next watch tick (after
    the reply lands) creates a fresh placeholder BELOW the reply.

    This scan walks the transcript from the current user turn's timestamp
    forward, collecting every assistant text-block content. It DEDUPES via
    each assistant message's `uuid` (recorded as `seen_uuids` in turn
    state) — any block whose containing message uuid is already in the
    seen set is skipped. Returns (new_text_blocks, updated_seen_uuids).

    Slightly more expensive than the byte-offset path (re-reads the file
    from turn boundary each prereply call) but bounded by turn length.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return [], list(seen_uuids or [])
    seen = set(seen_uuids or [])
    new_texts: list[str] = []
    new_seen = list(seen_uuids or [])
    # Find the user-message that bounds this turn, then collect every
    # assistant entry after it.
    in_turn = False
    try:
        with open(transcript_path) as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                t = obj.get("type")
                if t == "user" and not in_turn:
                    ts = obj.get("timestamp") or obj.get("ts") or ""
                    if ts == turn_ts:
                        in_turn = True
                    continue
                if not in_turn:
                    continue
                if t != "assistant":
                    continue
                uuid = obj.get("uuid") or ""
                if uuid in seen:
                    continue
                msg = obj.get("message", {})
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue
                added = False
                for c in content:
                    if (isinstance(c, dict)
                            and c.get("type") == "text"
                            and isinstance(c.get("text"), str)):
                        cleaned = _clean_narrate_text(c["text"])
                        if cleaned:
                            new_texts.append(cleaned)
                            added = True
                if added and uuid:
                    seen.add(uuid)
                    new_seen.append(uuid)
    except OSError as e:
        log(f"turn scan failed: {e}")
    return new_texts, new_seen


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_narrate_text(text: str) -> str:
    """Strip HTML tags + trim whitespace. Per-text-block sanitation only.

    The model sometimes emits raw HTML in terminal-targeted prose (<br>,
    <span style=...>, etc.) for formatting affordances that exist in its own
    output channel but render as literal tags on Discord. We strip those at
    the narrate boundary.

    Fence-neutralization and list-marker handling deliberately do NOT happen
    here — they live in _blockquote(), which runs on the full concatenated
    buffer. Doing them per-block here caused two bugs:
      - a ``` spanning two watcher fires got each half neutralized
        separately, then the concatenation re-formed a raw run (the "salad"
        leak)
      - the "- foo" → "• foo" rewrite here collided with _blockquote's
        list-indent regex, which then couldn't match the rewritten bullet
    Centralizing both in _blockquote fixes the collision and the leak.

    Returns an empty string if the text is empty after stripping — callers
    should skip empty results.
    """
    if not text:
        return ""
    cleaned = _HTML_TAG_RE.sub("", text)
    cleaned = cleaned.strip()
    return cleaned


# ---------------------------------------------------------------------------
# Phase handlers
# ---------------------------------------------------------------------------

def _resolve_turn_target(payload: dict, transcript_path: str, *, is_prereply: bool,
) -> tuple[str, str, str] | None:
    """Resolve (chat_id, message_id, turn_ts) for the turn this hook is acting on.

    Shared by handle_watch (narrate placeholder) and handle_think_prereply
    (think indicator) so both anchor to the EXACT same conversation + turn key.

    Resolves BOTH the target channel (chat_id) and the turn identity (inbound
    message_id) — paired from the SAME inbound <channel> tag so routing and
    keying never disagree. The whole multi-channel fix hinges on anchoring to
    "the conversation the bot is actually replying to," NOT "the newest message
    in the transcript" — because when two channels interleave, `_last_user_entry`
    flip-flops between them, and routing by it sends channel A's narration into
    channel B (the live leak seen 2026-05-25: channel A's work narrated into channel B
    mid-interleave).

    The ground truth for "which conversation" is the REPLY TARGET:
      prereply → the about-to-fire reply tool's chat_id (payload).
      watch    → the most recent reply tool's chat_id in the transcript
                 (_last_reply_chat_id). A reply already landing in channel X
                 means the bot is conversing in X, so post-tool prose for this
                 turn belongs in X — not in whatever just arrived.
    Only when NO reply target is known (no reply yet this turn) do we fall back
    to the newest inbound origin.

    Returns None when this isn't a Discord-origin turn (nothing to anchor).
    """
    reply_chat_id: str | None = None
    if is_prereply:
        tool_input = payload.get("tool_input") or {}
        candidate = tool_input.get("chat_id")
        if isinstance(candidate, str) and candidate:
            reply_chat_id = candidate
    else:
        # watch mode: anchor to the last reply's channel (the conversation
        # actually in progress), not the latest inbound.
        reply_chat_id = _last_reply_chat_id(transcript_path)

    user = _last_user_entry(transcript_path)
    user_text = _extract_user_text(user)
    origins = parse_discord_origins(user_text)
    if not origins:
        # Not a Discord-origin turn — narration not applicable
        return None

    chosen = None
    if reply_chat_id:
        # Prefer the inbound origin matching the reply target's channel, so
        # the turn key (message_id) belongs to the conversation we're in.
        # First check the latest user entry's origins, then fall back to a
        # whole-transcript scan — under interleave the reply target's inbound
        # message may be several entries back, not in the newest one.
        for cid, mid in reversed(origins):
            if cid == reply_chat_id:
                chosen = (cid, mid)
                break
        if chosen is None:
            chosen = _last_origin_for_channel(transcript_path, reply_chat_id)
    if chosen is None:
        chosen = origins[-1]
    chat_id, message_id = chosen
    # The reply target's channel is authoritative for WHERE the placeholder
    # goes, even if no inbound tag in this user_text matched it (e.g. the
    # inbound message scrolled out, or interleave put a different channel's
    # message newest). This is what stops cross-channel narration leaks.
    if reply_chat_id:
        chat_id = reply_chat_id

    # turn_ts locates the user-turn boundary for the transcript walkers. It
    # must come from the user entry that actually carries the CHOSEN
    # message_id — NOT _last_user_entry — because under interleave the chosen
    # origin (reply target) can be an EARLIER entry than the newest one. Using
    # the newest entry's ts here would point the byte-offset walker past the
    # chosen turn's prose and narrate nothing.
    turn_ts = _ts_for_message(transcript_path, message_id)
    if not turn_ts:
        turn_ts = (user.get("timestamp") or user.get("ts") or "") if user else ""

    return chat_id, message_id, turn_ts


def handle_watch(payload: dict, *, is_prereply: bool = False) -> int:
    """PostToolUse: surface new assistant text blocks for this turn.

    is_prereply=True is set by the `--mode prereply` dispatch (PreToolUse on
    Discord reply tool). In that mode we ALWAYS lock a placeholder above the
    upcoming reply, even when no new prose has accumulated yet — otherwise
    text emitted AFTER the reply tool fires creates a fresh placeholder
    that lands BELOW the reply. Empty placeholders get cleaned up at
    finalize (collapse mode deletes them outright; always-mode skips
    finalizing them).
    """
    transcript_path = payload.get("transcript_path") or ""
    if not transcript_path:
        return 0

    resolved = _resolve_turn_target(payload, transcript_path, is_prereply=is_prereply,
)
    if resolved is None:
        return 0
    chat_id, message_id, turn_ts = resolved

    mode = channel_mode(chat_id)
    if mode == "never":
        return 0

    turn_key = _turn_key_for_message(message_id)

    # Serialize the entire read-modify-write of narrate_state.json so
    # concurrent PostToolUse hooks (one per tool call) can't both create
    # a placeholder for the same turn. flock blocks the second invocation
    # until the first commits its placeholder_msg_id back to state.
    with _state_lock():
        return _handle_watch_locked(payload, transcript_path, chat_id, mode, turn_key, turn_ts,
            is_prereply=is_prereply,
)


def _suppress_post_reply_placeholder(mode: str, has_live_placeholder: bool,
    current_reply_count: int, replies_at_create: int,
) -> bool:
    """Whether to SKIP creating a narrate placeholder this tick.

    In `collapse` mode a placeholder created AFTER a reply has landed this
    turn sits BELOW the output it was meant to narrate — and collapse deletes
    it at finalize anyway, so it's pure visual flicker under the reply. Skip
    it. (The narration text isn't "lost": collapse mode discards it on
    finalize regardless of whether we ever posted the placeholder.)

    Only collapse mode is suppressed. `always` mode deliberately KEEPS the
    below-reply placeholder (it becomes a 🧠 Narration header above its own
    following prose), so its content must stay visible. A live placeholder
    means we're editing one that's already correctly positioned above its
    reply — never suppress that.

    This is the mechanism-level guard behind SQUAD.md's "reply LAST" rule:
    even if a bot emits prose after its reply, no useless block appears.
    """
    return (mode == "collapse"
        and not has_live_placeholder
        and current_reply_count > replies_at_create
)


def _handle_watch_locked(payload: dict, transcript_path: str, chat_id: str, mode: str,
    turn_key: str, turn_ts: str, *, is_prereply: bool = False,
) -> int:
    """Body of handle_watch executed under _state_lock()."""
    state = _load_state()
    turn = _get_turn(state, turn_key)
    if turn is None:
        # First firing for this turn — initialize offset at the byte
        # position just after the inbound user message. NEVER 0 — that
        # would replay the entire session history.
        initial_offset = _byte_offset_after_current_user_turn(transcript_path, turn_ts
)
        turn = {
            "chat_id": chat_id,
            # Current segment's live placeholder. Reset to None each
            # time we detect a mid-turn reply landed (segment rotation).
            "placeholder_msg_id": None,
            "last_byte_offset": initial_offset,
            "buffer": "",
            "mode": mode,
            "finalized": False,
            # How many discord-reply tool calls had landed when the
            # current segment's placeholder was created. When the live
            # count exceeds this, a reply landed mid-segment and we
            # seal the current placeholder and start a new one below.
            "replies_at_create": 0,
            # Trail of sealed placeholders within this turn — collapse
            # mode deletes all of them at Stop, always-mode keeps them as
            # 🧠 Narration headers above their respective replies.
            "sealed_placeholders": [],
            # Assistant-message UUIDs whose text content has already been
            # incorporated into the buffer (or a sealed segment's buffer).
            # Used by prereply's scan-back fallback (option A) to dedupe
            # against text the byte-offset path already captured.
            "seen_text_uuids": [],
        }
    if turn.get("finalized"):
        # Already wrapped up — nothing more to do for this turn
        return 0

    # Pass seen_text_uuids into the byte-offset reader so we skip any
    # already-incorporated message. This handles the race where a prior
    # prereply's scan-back captured a text block that physically lives
    # PAST `last_byte_offset` (because at scan time the EOF was BEFORE
    # the block was written, but the scan walked the whole turn and
    # found it anyway). Without uuid-level dedup the next watch's
    # byte-offset would re-read the same block and double the buffer.
    new_texts, new_uuids, new_offset = _new_text_blocks_since(transcript_path, turn.get("last_byte_offset", 0),
        skip_uuids=turn.get("seen_text_uuids", []),
)

    # Option A: in prereply mode, ALSO scan back through every assistant
    # entry in the current turn and pick up any text blocks the byte-offset
    # path missed (e.g. text emitted between Bash PostToolUse and the
    # reply tool's PreToolUse — the byte offset has already advanced past
    # the bash record, but the assistant's post-bash text record may not
    # have been flushed to disk in time for that earlier read).
    #
    # Dedup via assistant-message uuid: seen_text_uuids tracks every
    # message that's already contributed text to the buffer. Scan returns
    # only NOT-yet-seen text. Slightly more expensive on prereply (re-
    # reads file from turn boundary) but bounded by turn length and only
    # fires once per Discord reply.
    if is_prereply:
        scan_texts, scan_seen = _scan_turn_text_blocks(transcript_path, turn_ts,
            list(turn.get("seen_text_uuids", [])) + new_uuids,
)
        if scan_texts:
            new_texts = new_texts + scan_texts
            log(f"prereply scan-back recovered {len(scan_texts)} text block(s) for turn {turn_key}")
        new_uuids = [u for u in scan_seen
                     if u not in turn.get("seen_text_uuids", [])]

    # Segment rotation: if a reply landed since the current segment's
    # placeholder was created, seal the current placeholder (so it
    # stays positioned above its triggering reply) and reset state for
    # a brand-new segment below the reply.
    current_reply_count = count_discord_replies(transcript_path)
    if (turn.get("placeholder_msg_id")
        and current_reply_count > turn.get("replies_at_create", 0)
):
        _seal_segment(turn)
        log(f"rotated narrate segment for turn {turn_key} (reply count {current_reply_count})")

    # Collapse-mode post-reply guard: once a reply has landed this turn and we
    # have no live placeholder, any new placeholder would post BELOW the reply
    # and get deleted at finalize anyway — pure flicker. Skip creating it.
    # Advance the offset + seen-uuids so we don't re-scan this text next tick,
    # then exit. (Mechanism-level enforcement of SQUAD.md's "reply LAST" rule:
    # holds even when a bot emits prose after its reply.) always-mode is NOT
    # suppressed — it keeps below-reply placeholders as 🧠 Narration headers.
    if _suppress_post_reply_placeholder(mode, bool(turn.get("placeholder_msg_id")),
        current_reply_count, turn.get("replies_at_create", 0),
):
        turn["last_byte_offset"] = new_offset
        if new_uuids:
            seen = turn.get("seen_text_uuids") or []
            for u in new_uuids:
                if u not in seen:
                    seen.append(u)
            turn["seen_text_uuids"] = seen
        state[turn_key] = turn
        _save_state(state)
        return 0

    if not new_texts and turn.get("placeholder_msg_id"):
        # Nothing new to add — keep state coherent and exit.
        turn["last_byte_offset"] = new_offset
        if new_uuids:
            seen = turn.get("seen_text_uuids") or []
            for u in new_uuids:
                if u not in seen:
                    seen.append(u)
            turn["seen_text_uuids"] = seen
        state[turn_key] = turn
        _save_state(state)
        return 0
    if not new_texts and not (is_prereply and mode == "always"):
        # Nothing yet — don't create an empty placeholder.
        #
        # Exception: prereply mode in `always` mode posts the placeholder
        # NOW (above the upcoming Discord reply) so that any prose
        # emitted AFTER the reply still gets edited into THIS placeholder
        # instead of creating a new one below the reply.
        #
        # In `collapse` mode the placeholder gets deleted at finalize
        # regardless of position, so the position-locking is pure visual
        # noise — users see a useless "🧠 Narrating… …" stub mid-turn.
        # Skip the stub creation; if post-reply prose comes in later, the
        # PostToolUse watch tick will create the placeholder normally
        # (below the reply) and finalize will delete it as usual.
        return 0

    # NOTE: race guard removed 2026-05-22. Previously suppressed
    # placeholder creation when a reply had already landed this turn —
    # the worry was that narrate appearing BELOW its reply reads as
    # "the bot is talking to itself." But suppression means valid
    # narration content gets silently dropped, which is worse. With
    # the PreToolUse-on-reply hook (`--mode prereply`), the placeholder
    # is now created BEFORE the reply lands, so the common case
    # ordering is correct. For the edge case (mid-turn rotation, model
    # emits prose AFTER a reply), the placeholder posts below — the user's
    # explicit preference: "narration should never be cut off, always
    # above the output it was meant for, except when steered by a new
    # user message" — i.e. degraded-but-visible beats invisible.

    state_dir = detect_discord_state_dir()
    token = read_bot_token(state_dir)
    if not token:
        log(f"no bot token at {state_dir} — skipping narrate")
        return 0

    # NOTE: the thinking indicator is now a STANDALONE Discord message
    # (turn["think_msg_id"]), wholly owned by the detached think-updater and
    # decoupled from this placeholder's lifecycle. Narrate watch no longer
    # touches any think_* flag here — there is no stub to adopt, no summary to
    # replace in the buffer. The two messages have distinct ids.

    # Append new text to buffer with paragraph breaks between blocks
    incoming = "\n\n".join(new_texts) if new_texts else ""
    candidate_buffer = (turn["buffer"] + "\n\n" + incoming
                        if turn["buffer"] and incoming else (turn["buffer"] or incoming
))
    candidate_content = NARRATE_PREFIX_AUTO + _blockquote(candidate_buffer or "…")

    # Length guard: if appending this batch would push the placeholder
    # over Discord's 2000-char cap, the edit would silently truncate
    # mid-blockquote — looks like raw text leaks past the quote bar.
    # Auto-rotate: seal the current segment (it stays sized as it was
    # before this batch), reset state, and start a fresh placeholder
    # with just the new text. Same mechanism as reply-triggered
    # rotation, just driven by length instead of a reply landing.
    if (turn.get("placeholder_msg_id")
        and len(candidate_content) > DISCORD_LIMIT
):
        _seal_segment(turn)
        log(f"rotated narrate segment for turn {turn_key} (length cap)")
        turn["buffer"] = incoming
    else:
        turn["buffer"] = candidate_buffer
    turn["last_byte_offset"] = new_offset
    # Record assistant-message UUIDs whose text we just incorporated so the
    # prereply scan-back can dedupe against them.
    if new_uuids:
        seen = turn.get("seen_text_uuids") or []
        for u in new_uuids:
            if u not in seen:
                seen.append(u)
        turn["seen_text_uuids"] = seen

    # A single batch can itself exceed the cap — one long uninterrupted
    # narration block, most commonly as the FIRST batch of a turn, which
    # skips the rotation guard above (no placeholder exists yet) and used
    # to get hard-sliced mid-sentence by the [:DISCORD_LIMIT] in the send.
    # Carve the buffer into cap-sized segments at natural boundaries:
    # render each head into the current placeholder (creating one if
    # needed), seal it, carry the remainder forward as the live segment.
    while len(NARRATE_PREFIX_AUTO + _blockquote(turn["buffer"] or "…")) > DISCORD_LIMIT:
        head, tail = _fit_split(turn["buffer"], NARRATE_PREFIX_AUTO)
        if not head or not tail:
            break  # unsplittable — defensive, the send cap still applies
        head_content = NARRATE_PREFIX_AUTO + _blockquote(head)
        if turn.get("placeholder_msg_id"):
            discord_edit_message(token, chat_id, turn["placeholder_msg_id"], head_content)
        else:
            msg_id = discord_send_message(token, chat_id, head_content)
            if not msg_id:
                break
            turn["placeholder_msg_id"] = msg_id
            turn["replies_at_create"] = current_reply_count
        turn["buffer"] = head  # sealed entry snapshots this for finalize
        _seal_segment(turn)
        turn["buffer"] = tail
        log(f"split oversized narrate buffer for turn {turn_key} "
            f"({len(tail)} chars carried to next segment)")

    # Both modes show the bold+italic "Narrating…" status marker while the
    # turn is mid-flight. The transition happens at Stop:
    #   collapse → placeholder is deleted (status marker disappears with it)
    #   always → handle_finalize swaps to the plain bold "Narration" header
    # so the live word reads as "this is still happening" and the settled
    # word reads as "this was narration".
    #
    # Prereply may post BEFORE any prose has accumulated — render a stub
    # "…" so the blockquote isn't visually empty. Subsequent watch ticks
    # will replace it once real text comes through.
    content = NARRATE_PREFIX_AUTO + _blockquote(turn["buffer"] or "…")

    if turn.get("placeholder_msg_id"):
        if discord_edit_message(token, chat_id, turn["placeholder_msg_id"], content):
            log(f"edited narrate placeholder for turn {turn_key}")
    else:
        msg_id = discord_send_message(token, chat_id, content)
        if msg_id:
            turn["placeholder_msg_id"] = msg_id
            turn["replies_at_create"] = current_reply_count
            log(f"created narrate placeholder {msg_id} for turn {turn_key}")
        else:
            log(f"narrate placeholder send failed for {turn_key}")

    state[turn_key] = turn
    _save_state(state)
    return 0


def _seal_segment(turn: dict) -> None:
    """Seal the current segment's placeholder and reset for a new one.

    Called when a mid-turn reply lands. The current placeholder stays
    where it is in the channel (above its triggering reply) and is
    appended to sealed_placeholders for Stop-time finalization. Buffer
    and live id reset so the next narrate text creates a fresh
    placeholder below the reply.
    """
    msg_id = turn.get("placeholder_msg_id")
    if msg_id:
        turn.setdefault("sealed_placeholders", []).append({
            "msg_id": msg_id,
            "buffer": turn.get("buffer", ""),
        })
    turn["placeholder_msg_id"] = None
    turn["buffer"] = ""


def handle_finalize(payload: dict) -> int:
    """Stop: clean up the narrate placeholders for this turn.

    Each turn may now contain multiple segment placeholders if a
    mid-turn reply triggered rotation. Auto-mode deletes them all
    (the placeholder is purely ephemeral). Always-mode edits each
    one to the settled 🧠 Narration header so they read as section
    headers above their respective replies.
    """
    transcript_path = payload.get("transcript_path") or ""
    with _state_lock():
        return _handle_finalize_locked(payload, transcript_path)


def _safety_net_settle_think(turn_key: str, turn: dict, chat_id: str, token: str,
) -> None:
    """Settle an orphaned, still-live think indicator from the Stop hook.

    Runs UNDER the caller's _state_lock() (handle_finalize holds it for the
    whole sweep), so this must NOT re-acquire the lock — fcntl flock isn't
    reentrant across separate fds in one process and would deadlock. We mutate
    the passed `turn` dict in place (the caller writes state back) and do the
    network edit inline, exactly like the rest of _finalize_one_turn.

    Total think time is reconstructed from the snapshot the live updater
    persisted each tick (think_total + the in-flight think phase), so it
    excludes tool-execution time. If no snapshot exists (updater died before
    its first tick), fall back to wall-clock from think_start — an over-
    estimate, but better than leaving a spinner up forever.

    Collapse-delete is best-effort and NEVER sleeps: a settled "Thought for N"
    line lingering is acceptable; an orphaned animating "Thinking…" is not.
    """
    think_msg_id = turn.get("think_msg_id")
    if not think_msg_id:
        return
    now = time.time()
    total = turn.get("think_total")
    if isinstance(total, (int, float)):
        if turn.get("think_phase") == _PHASE_THINK:
            ps = turn.get("think_phase_start")
            if isinstance(ps, (int, float)):
                total = total + max(0.0, now - ps)
    else:
        # No per-tick snapshot — fall back to wall-clock since think_start.
        started = turn.get("think_start")
        total = max(0.0, now - started) if isinstance(started, (int, float)) else 1.0

    summary = _think_summary("", float(total))
    turn["think_settled"] = True
    turn.pop("think_owner", None)
    ok = discord_edit_message(token, chat_id, think_msg_id, summary)
    if ok:
        log(f"think safety-net settled turn={turn_key} -> {summary!r}")
    # Strip our own 🤔 here too. react_hook's _mark_removed locks the SEPARATE
    # react_hook_state.json.lock (this holds narrate_state.json.lock), so no
    # reentrancy/deadlock despite running under the finalize sweep's lock.
    _clear_think_react(chat_id, _msg_id_from_turn_key(turn_key), token)
    # Do NOT delete here. A settled "Thought for Ns" line lingering is harmless
    # (only an animating "Thinking…" orphan is not). The still-alive detached
    # updater owns the readable collapse delay on its next tick — it sees
    # `finalized` and runs _finalize_think_summary — so the line gets the SAME
    # _THINK_COLLAPSE_SEC linger in every mode, including `never`, instead of
    # vanishing the instant a reply-ended turn hits this safety net. `always` keeps the line indefinitely as before.


def _finalize_one_turn(turn_key: str, turn: dict, token: str, state_dir: str,
                       transcript_path: str = "") -> None:
    """Finalize a single turn's placeholders + tool messages in-place.

    Mutates `turn` (sets finalized=True). Network calls use the passed
    token/state_dir so the caller resolves them once per Stop, not per turn.
    transcript_path feeds the tool-trace-below-reply swap.
    """
    chat_id = turn.get("chat_id")

    # ── Stop-hook safety net for the standalone think indicator. The detached
    #    updater can be killed at turn-end before it settles, orphaning a live
    #    animating "🧠 ✻ Thinking…". If this turn still has an un-settled think
    #    message, settle it to "🧠 ✓ Thought for {total}" here. We must keep
    #    Stop FAST — no time.sleep(60); the collapse-delete is best-effort and
    #    skippable (a settled line persisting is fine; an orphaned spinner is
    #    not). Total is reconstructed from the persisted phase snapshot so it
    #    excludes tool-execution time, matching what the live updater would show.
    if chat_id and turn.get("think_msg_id") and not turn.get("think_settled"):
        try:
            _safety_net_settle_think(turn_key, turn, chat_id, token)
        except Exception as e:
            log(f"think safety-net failed (turn {turn_key}): {e}")

    # Resolve mode at finalize time from the CURRENT channel config, not the
    # value snapshotted at turn-start. Otherwise a turn that began under
    # `always` (or before the channel was switched to `collapse`) finalizes
    # under the stale mode and the narration persists instead of collapsing —
    # which is exactly the "collapse doesn't work / channel gets clogged on
    # long agentic turns" bug. The live channel config is the source of truth.
    snapshot_mode = turn.get("mode", "never")
    current_mode = channel_mode(chat_id) if chat_id else snapshot_mode
    mode = current_mode
    if snapshot_mode != current_mode:
        log(f"mode changed mid-turn {snapshot_mode}->{current_mode} "
            f"(turn {turn_key}); finalizing as {current_mode}")

    # Collect everything to finalize: the live segment + all sealed
    # segments from mid-turn rotations.
    segments: list[dict] = list(turn.get("sealed_placeholders", []))
    live_msg_id = turn.get("placeholder_msg_id")
    if live_msg_id:
        segments.append({
            "msg_id": live_msg_id,
            "buffer": turn.get("buffer", ""),
        })

    if (segments or turn.get("tool_msg_id") or turn.get("sealed_tool_messages")) and chat_id:
        for seg in segments:
            seg_msg_id = seg.get("msg_id")
            if not seg_msg_id:
                continue
            buf = (seg.get("buffer") or "").strip()
            if not buf:
                # Empty placeholder — almost always a prereply stub
                # whose reply tool ran without any prose preceding or
                # following it. Nothing useful to settle into, just
                # remove. (Same behavior in both collapse and always.)
                if discord_delete_message(token, chat_id, seg_msg_id):
                    log(f"deleted empty narrate placeholder {seg_msg_id} (turn {turn_key})")
                continue
            if mode == "collapse":
                if discord_delete_message(token, chat_id, seg_msg_id):
                    log(f"deleted narrate placeholder {seg_msg_id} (collapse, turn {turn_key})")
            elif mode == "always":
                content = NARRATE_PREFIX_ALWAYS + _blockquote(buf)
                if discord_edit_message(token, chat_id, seg_msg_id, content):
                    log(f"finalized narrate placeholder {seg_msg_id} (always, turn {turn_key})")

        # Finalize tool-trace messages.
        # Behavior depends on the channel's `tools` mode:
        #   collapse → delete the tool message entirely (symmetric
        #              with narrate collapse mode)
        #   otherwise → swap the live "Tool trace…" prefix to the
        #               settled "Tool trace" version, keep the body
        # Covers both the in-flight tool_msg_id and any
        # sealed_tool_messages from rotations.
        tools_mode = _channel_tools_mode(state_dir, chat_id)
        tool_msgs: list[dict] = list(turn.get("sealed_tool_messages", []))
        if turn.get("tool_msg_id"):
            tool_msgs.append({
                "msg_id": turn["tool_msg_id"],
                "buffer": turn.get("tool_buffer", ""),
                # only the LIVE message carries the agent-view footer;
                # keep it through finalization in persist modes
                "panel": turn.get("agent_panel", ""),
            })
        for tm in tool_msgs:
            tm_id = tm.get("msg_id")
            if not tm_id:
                continue
            if tools_mode == "collapse":
                if discord_delete_message(token, chat_id, tm_id):
                    log(f"deleted tool message {tm_id} (collapse, turn {turn_key})")
                continue
            buf = tm.get("buffer", "")
            if not buf.strip():
                # Empty anchor — a prereply tool-trace stub on a turn that
                # never surfaced a tool (reply-only). Delete rather than leave
                # a bare "🔧 Tool trace" header. Mirrors the empty-narrate-
                # placeholder reap above.
                if discord_delete_message(token, chat_id, tm_id):
                    log(f"deleted empty tool anchor {tm_id} (turn {turn_key})")
                continue
            final_content = "🔧 **Tool trace**\n"
            if buf:
                final_content += "```diff\n" + buf + "\n```"
            if tm.get("panel"):
                final_content += "\n" + tm["panel"]
            if discord_edit_message(token, chat_id, tm_id, final_content):
                log(f"finalized tool message {tm_id} (turn {turn_key})")

        # ── Tool-trace-below-reply SWAP (, "crude but works").
        #    If the live tool-trace message ended up BELOW the last reply — i.e.
        #    an echo-first turn (reply, THEN tool work, violating reply-LAST) —
        #    Discord can't reorder messages, but the bot owns both, so swap their
        #    TEXT contents: the (upper) reply message takes the trace, the
        #    (lower) trace message takes the reply text. Net: the reply reads
        #    last. Snowflake ids are monotonic, so trace_id > reply_id ⇒ the
        #    trace is below. Fully best-effort: any failure leaves both as-is.
        if chat_id and transcript_path and turn.get("tool_msg_id"):
            try:
                _swap_trace_below_reply(chat_id, str(turn["tool_msg_id"]), transcript_path,
                    token, turn_key,
)
            except Exception as e:
                log(f"trace/reply swap failed (turn {turn_key}): {e}")

    turn["finalized"] = True


def _handle_finalize_locked(payload: dict, transcript_path: str) -> int:
    """Body of handle_finalize executed under _state_lock().

    Stop fires once at the END of the whole agent turn, with only
    transcript_path — no message_id to tell us which turn ended. Since the
    session is wrapping up, EVERY not-yet-finalized turn for this bot should
    collapse now. Sweeping all of them (rather than resolving a single key
    from "latest user message") is what stops the multi-channel orphan bug:
    when two channels interleaved, the old single-key finalize cleaned only
    the newest turn and left the other channel's placeholder up forever.
    """
    state = _load_state()
    bot_prefix = f"{_bot_id()}:"
    pending = [
        (k, v) for k, v in state.items()
        if isinstance(v, dict)
        and k.startswith(bot_prefix)
        and not v.get("finalized")
    ]
    if pending:
        state_dir = detect_discord_state_dir()
        token = read_bot_token(state_dir)
        if token:
            for turn_key, turn in pending:
                _finalize_one_turn(turn_key, turn, token, state_dir,
                                   transcript_path)
                state[turn_key] = turn
        else:
            log(f"no bot token at {state_dir} — skipping finalize sweep")

    # Garbage-collect old finalized turns (older than 24h) to keep state bounded.
    now = time.time()
    cutoff = now - 86400
    pruned = {
        k: v for k, v in state.items()
        if not (isinstance(v, dict) and v.get("finalized")
                and isinstance(v.get("ts"), (int, float)) and v["ts"] < cutoff)
    }
    # Stamp finalize time on the turns we just settled so GC can age them out.
    for turn_key, turn in pending:
        if turn_key in pruned:
            pruned[turn_key] = {**turn, "ts": now}
    _save_state(pruned)
    return 0


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _last_reply_chat_id(transcript_path: str) -> str | None:
    """Return the chat_id of the most recent mcp Discord reply tool_use.

    Used by handle_watch (PostToolUse) to anchor narration to the channel
    the bot is actively conversing in — important when mid-turn interrupts
    from other channels cause `_last_user_entry` to point at a different
    chat than the bot is actually replying to. The reply tool's chat_id
    is the ground truth.

    Walks the transcript in reverse, scanning every assistant message's
    content blocks for `mcp__plugin_discord_discord__reply` tool_use
    entries. Returns the chat_id from the most recent one found; None
    if no reply has been issued yet in the file.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return None
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use" and c.get("name") == "mcp__plugin_discord_discord__reply":
                input_ = c.get("input") or {}
                cid = input_.get("chat_id")
                if isinstance(cid, str) and cid:
                    return cid
    return None


_SENT_ID_RE = re.compile(r"sent(?:\s+\d+\s+parts?)?\s*\(ids?:\s*(\d{17,20})")


def _reply_messages_for_turn(transcript_path: str, chat_id: str) -> list[str]:
    """Return the sent message_ids of ALL replies to `chat_id` this turn, in
    document order. >1 means a multi-part reply (the swap skips those — too
    fiddly to disambiguate which part a single trace pairs with)."""
    if not transcript_path or not os.path.exists(transcript_path) or not chat_id:
        return []
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except OSError:
        return []
    # Map reply tool_use_ids (to this chat) → keep order; then resolve their
    # tool_results' sent ids. Bound to the LAST user turn by scanning back to
    # the most recent user entry first.
    last_user_idx = 0
    for i in range(len(lines) - 1, -1, -1):
        try:
            o = json.loads(lines[i])
        except json.JSONDecodeError:
            continue
        if o.get("type") == "user" and _extract_user_text(o):
            # a real user message (not a tool_result-only user entry)
            if any(isinstance(c, dict) and c.get("type") == "text"
                   for c in o.get("message", {}).get("content", []) if isinstance(c, dict)):
                last_user_idx = i
                break
    tuids: list[str] = []
    for line in lines[last_user_idx:]:
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("type") != "assistant":
            continue
        for c in o.get("message", {}).get("content", []):
            if (isinstance(c, dict) and c.get("type") == "tool_use"
                    and c.get("name") == "mcp__plugin_discord_discord__reply"
                    and str((c.get("input") or {}).get("chat_id") or "") == str(chat_id)):
                if c.get("id"):
                    tuids.append(c["id"])
    if not tuids:
        return []
    sent: list[str] = []
    want = set(tuids)
    for line in lines[last_user_idx:]:
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        for c in o.get("message", {}).get("content", []) if isinstance(o.get("message"), dict) else []:
            if (isinstance(c, dict) and c.get("type") == "tool_result"
                    and c.get("tool_use_id") in want):
                txt = c.get("content", "")
                if isinstance(txt, list):
                    txt = " ".join(b.get("text", "") for b in txt
                                   if isinstance(b, dict) and b.get("type") == "text")
                m = _SENT_ID_RE.search(str(txt))
                if m:
                    sent.append(m.group(1))
    return sent


def _swap_trace_below_reply(chat_id: str, trace_msg_id: str, transcript_path: str,
    token: str | None, turn_key: str,
) -> None:
    """If the tool-trace message is BELOW the reply, swap their text in place.

    Crude-but-correct: Discord can't reorder messages, but the
    bot owns both, so we swap CONTENT — the upper (reply) message becomes the
    trace, the lower (trace) message becomes the reply text — leaving the reply
    visually last. Only fires on a SINGLE-part reply (multi-part is skipped) when
    the trace's snowflake id > the reply's (⇒ posted later ⇒ lower). Best-effort;
    any failure leaves both messages untouched.
    """
    if not token:
        return
    replies = _reply_messages_for_turn(transcript_path, chat_id)
    if len(replies) != 1:
        # No reply found (nothing to swap against) or multi-part (ambiguous).
        return
    reply_id = replies[0]
    try:
        if int(trace_msg_id) <= int(reply_id):
            return  # trace is already AT/ABOVE the reply — nothing to do
    except (TypeError, ValueError):
        return
    # Fetch both current contents. The trace we could rebuild, but fetching is
    # simplest and authoritative; the reply we MUST fetch (its text isn't in our
    # state). A failed fetch → bail (don't half-swap).
    trace_msg = discord_get_message(token, chat_id, trace_msg_id)
    reply_msg = discord_get_message(token, chat_id, reply_id)
    if not trace_msg or not reply_msg:
        return
    trace_content = trace_msg.get("content") or ""
    reply_content = reply_msg.get("content") or ""
    if not trace_content or not reply_content:
        return
    # Swap: upper(reply_id) ← trace, lower(trace_id) ← reply text. Order the
    # edits so a mid-swap failure is least confusing: write the reply text into
    # the lower message FIRST (so the reply is already last even if the second
    # edit fails), then move the trace up.
    if not discord_edit_message(token, chat_id, trace_msg_id, reply_content):
        return
    if not discord_edit_message(token, chat_id, reply_id, trace_content):
        # Couldn't move the trace up; the lower message now holds the reply text
        # (still reads as the reply, last) — acceptable degraded state.
        log(f"trace/reply swap half-applied (turn {turn_key}): lower set, upper edit failed")
        return
    log(f"swapped trace↔reply (turn {turn_key}): reply now last "
        f"(trace {trace_msg_id} ↔ reply {reply_id})")


def _extract_user_text(user_entry: dict | None) -> str:
    if not user_entry:
        return ""
    msg = user_entry.get("message", {})
    content = msg.get("content", [])
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                txt = c.get("text", "")
                if isinstance(txt, str):
                    parts.append(txt)
        return "\n".join(parts)
    return ""


# ---------------------------------------------------------------------------
# Thinking-mode updater (Mode A) — detached, spawned from react_hook's
# handle_received when extended thinking is ON. Owns: the ~1s gap → 🤔 stamp,
# and a progressive "🧠 Thinking with {effort} effort…" narrate block that escalates
# over time, until the turn moves on (first tool/text lands) or it times out.
# ---------------------------------------------------------------------------

# Escalation tiers: (elapsed_seconds_threshold, phrase_template). The updater
# edits the message to the LAST template whose threshold has been crossed.
# {g} is the spinner glyph, {e} is the effort suffix (" with xhigh effort" or
# "" when effort is unknown). The verb phrase escalates; {e} = " with <effort>
# effort" trails it ("Thinking with high effort" → "Thinking more with high
# effort" → …). Six tiers, widening cadence for the long-haul framing.
_THINK_TIERS = [
    (0.0, "🧠 {g} **Thinking{e}{d}**"),
    (30.0, "🧠 {g} **Thinking more{e}{d}**"),
    (60.0, "🧠 {g} **Thinking even more{e}{d}**"),
    (90.0, "🧠 {g} **Thinking more still{e}{d}**"),
    (150.0, "🧠 {g} **Thinking for a long time{e}{d}**"),
    (240.0, "🧠 {g} **Thinking forever{e}{d}**"),
]
# Claude-Code-style loading glyphs; the updater rotates through these every
# refresh tick so the 🧠 message animates while the think runs.
_THINK_GLYPHS = ["✻", "✢", "✱", "✶", "✷", "✸"]  # all text-presentation (no emoji variants)
# {d} = the trailing dots, animated alongside the glyph so the ellipsis itself
# pulses (". .. …"). Cycles each refresh tick like the glyph.
_THINK_DOTS = [".", "..", "…"]
_THINK_REFRESH_SEC = 1.5       # edit cadence: advance glyph + update tier
_THINK_HARD_TIMEOUT_SEC = 43200  # absolute lifetime ceiling (12h) — a far backstop against a runaway turn. NOT the normal stand-down: an ACTIVE turn keeps its indicator the whole time, however long (the old 300s cap is why the trace vanished mid-long-task — ; raised 1h→12h same day so even a marathon session is never cut).
_THINK_IDLE_TIMEOUT_SEC = 720   # stand down after this much SILENCE (no new act/output) → the turn likely ended without a terminal react. Resets on any activity, so a long-but-busy turn (many tool calls) never trips it; only a stalled/orphaned one does. Kept > the 600s max Bash runtime so a single long tool call can't false-trip it.
_THINK_GAP_SEC = 1.0           # gap before posting (instant-turn guard window)
# Dwell gate for the STANDALONE indicator. The runtime extended-thinking toggle
# (Option+T / shift+tab) is invisible to hooks, and the model emits brief
# "thoughts" even with extended thinking OFF (verified: transcript thinking
# blocks are redacted/empty, no readable on/off flag). Those brief thoughts
# resolve to output fast; only a GENUINE extended-think phase holds output back
# for seconds. So we post the "🧠 Thinking…" indicator only once the turn has
# stayed output-less this long — dwell time is the sole available proxy for
# "thinking is actually on". The prompt 🤔 react + narrate
# placeholder still carry every turn; this gates ONLY the extra indicator.
# Tunable via env so we can dial the cut without a redeploy.
_THINK_SHOW_SEC = float(os.environ.get("CCDK_THINK_SHOW_SEC", "6"))
_THINK_SHOW_POLL_SEC = 0.5     # how often the dwell gate checks for first output
_THINK_COLLAPSE_SEC = 60       # collapse/never: keep "Thought for Ns" this long so the duration is readable, THEN delete (a deliberate call — readability over the brief linger; 6s was too short). always-mode keeps it indefinitely.
_THINK_HOLDER_STALE_SEC = 30   # cross-turn liveness: a channel's indicator-holder blocks NEW turns from posting only while its updater is alive (heartbeats every _THINK_REFRESH_SEC). If the heartbeat goes this stale the updater died (crash / killed session / Stop never fired) and its orphaned "Thinking…" must NOT keep blacking out the channel. 30s ≈ 20 missed ticks — unambiguous death, no flap. (an orphan once blocked a channel for the full 12h hard-timeout.)

# Final settled line shown once the think ends (first real output / timeout).
# Duration from think-start to think-end, shown as "Ns" under a minute and
# "Xm YYs" at/over 60s. The loading glyph settles to a ✓ checkmark. No effort
# suffix. This message PERSISTS — it is never deleted. The owner uses the
# duration to gauge whether the effort level is set too high.
_THINK_SUMMARY_TEMPLATE = "🧠 ✓ **Thought for {dur}**"

# Settled line when the turn was INTERRUPTED (user hit ESC on the Claude Code
# side). Claude Code fires no hook on interrupt and stamps no terminal react, so
# the detached updater detects the "[Request interrupted by user]" marker in the
# transcript and settles to this instead of spinning "Thinking…" to the idle
# timeout. ✗ mirrors the ✓ of a clean finish.
_INTERRUPTED_SUMMARY = "🧠 ✗ **Interrupted**"

# Reacts that mean a later lifecycle state has taken over from 🤔 — when any
# of these is applied to the inbound message, the turn has visibly moved on.
# (Kept for back-compat / the gap-guard's "did anything happen yet" check.)
_THINK_SUPERSEDING_EMOJI = [
    EMOJI["editing"], EMOJI["researching"], EMOJI["delegating"],
    EMOJI["replied"], EMOJI["terminal"], EMOJI["errored"], EMOJI["denied"],
]

# Reacts that mean the WHOLE TURN has ended (not merely "an action started").
# These settle the think indicator for good. Two kinds of intermediate react are
# deliberately EXCLUDED so the indicator survives and reactivates for mid-task
# thinking:
#   - tool-state emojis (🔧/🌐/🤖 = editing/researching/delegating) — "the model
#     is acting", an ACT phase, not turn-end.
#   - ✅ replied — a Discord reply can be INTERMEDIATE: the bot posts a message
#     between two tasks in the same turn and keeps working. Treating ✅ as
#     turn-end settled the updater on that first reply, so the next task's
#     "Thinking…" never came back. The real turn-end for a
#     reply-ending turn is the Stop hook, which settles the indicator via the
#     _safety_net_settle_think path in handle_finalize.
# So terminal = only the genuine end-of-turn reacts: 🖥️ (terminal — Stop with no
# reply), ❌ (errored), ⚠️ (denied). ✅ stays in _THINK_SUPERSEDING_EMOJI above
# because a reply IS output (for the startup gap-guard) — it just isn't turn-end.
_THINK_TERMINAL_EMOJI = [
    EMOJI["terminal"], EMOJI["errored"], EMOJI["denied"],
]

# Phase labels for the per-turn think/act state machine.
_PHASE_THINK = "think"
_PHASE_ACT = "act"

# A gap of this many seconds with NO new transcript entries AFTER an action
# means the model has gone back to reasoning → reactivate the THINK phase.
# One poll interval is the natural granularity (the loop wakes every
# _THINK_REFRESH_SEC); requiring a full quiet interval avoids flapping on the
# brief lull between a tool_use and its tool_result.
_THINK_REACTIVATE_GAP_SEC = _THINK_REFRESH_SEC

# Compaction guard. A mid-turn context compaction makes the transcript go quiet,
# which the phase machine would misread as a THINK phase and falsely animate
# "Thinking with {effort}" while the model is actually compacting (a bot was
# observed showing xhigh thinking during a compaction). The PreCompact
# react hook writes a per-turn sentinel (keyed by inbound message_id so
# concurrent bots sharing STATE_DIR don't collide); SessionStart(source=compact)
# clears it. While it's present the updater shows a Compacting indicator instead
# and doesn't bank the time as thinking.
_COMPACTING_TTL_SEC = 600  # stale backstop if the clear never fired (killed mid-compaction)


def _compacting_path(msg_id: str) -> str:
    return os.path.join(STATE_DIR, f".compacting.{msg_id}")


def _compacting_active(msg_id: str) -> bool:
    """True while a compaction is in progress for this turn. TTL-bounded so a
    sentinel orphaned by an abnormal exit can't suppress thinking forever."""
    try:
        st = os.stat(_compacting_path(msg_id))
    except OSError:
        return False
    return (time.time() - st.st_mtime) <= _COMPACTING_TTL_SEC


def _think_glyph(tick: int) -> str:
    """Return the spinner glyph for refresh tick `tick`, cycling the set."""
    return _THINK_GLYPHS[tick % len(_THINK_GLYPHS)]


def _think_dots(tick: int) -> str:
    """Return the animated trailing dots for refresh tick `tick` ('.'/'..'/'…')."""
    return _THINK_DOTS[tick % len(_THINK_DOTS)]


def _think_phrase(effort: str, elapsed: float, glyph: str, dots: str = "…") -> str:
    """Render the escalation phrase for the elapsed time + current glyph + dots.

    {g} is the rotating spinner glyph, {e} the effort suffix, {d} the animated
    trailing dots. The updater edits the standalone think message to the LAST
    tier whose threshold has been crossed, advancing the glyph AND the dots each
    refresh so the ellipsis pulses with the spinner.
    """
    suffix = f" with {effort} effort" if effort else ""
    template = _THINK_TIERS[0][1]
    for threshold, tmpl in _THINK_TIERS:
        if elapsed >= threshold:
            template = tmpl
    return template.format(g=glyph, e=suffix, d=dots)


def _fmt_think_duration(elapsed: float) -> str:
    """'Ns' under a minute, 'Xm Ys' at/over 60s — actual seconds, floored at 1s.
    No zero-pad on the seconds ('1m 0s', not '1m 00s')."""
    n = max(1, int(round(elapsed)))
    if n < 60:
        return f"{n}s"
    m, s = divmod(n, 60)
    return f"{m}m {s}s"


def _think_summary(effort: str, elapsed: float, interrupted: bool = False) -> str:
    """Render the settled (persistent) think line.

    Normally '🧠 ✓ Thought for {duration}' (duration floored at 1s; m:s at/over
    60s). When `interrupted` (the user hit ESC mid-turn), '🧠 ✗ Interrupted'
    instead — no duration, since the turn was cut short. No effort suffix; this
    line is never deleted. (`effort` is accepted for call-site compatibility but
    no longer shown.)"""
    if interrupted:
        return _INTERRUPTED_SUMMARY
    return _THINK_SUMMARY_TEMPLATE.format(dur=_fmt_think_duration(elapsed))


def _turn_moved_on(transcript_path: str, turn_ts: str, start_byte: int,
    chat_id: str, msg_id: str,
) -> bool:
    """Has the turn produced visible output since the think started?

    Two independent signals — either is enough to stop the updater so the
    normal watch / tool_watcher / finalize path can adopt the block:

      1. New transcript content past `start_byte` contains an assistant
         text/tool_use block or a tool_result — i.e. the model emitted prose
         or fired a tool. (start_byte is the EOF captured right after the
         inbound message, so anything past it is this turn's first output.)
      2. A superseding emoji (🔧/🌐/🤖/✅/🖥️/❌/⚠️) is already applied to the
         inbound message — a later react_hook state replaced 🤔.

    Fails CLOSED toward "moved on" only on the byte signal (a read error
    returns False so we don't prematurely stop just because the file is
    momentarily unreadable); the emoji signal is best-effort.
    """
    # Signal 2: a later lifecycle react already superseded 🤔.
    try:
        for emoji in _THINK_SUPERSEDING_EMOJI:
            if _has_applied(chat_id, msg_id, emoji):
                return True
    except Exception:
        pass
    # Signal 1: new assistant output appended since the think began.
    if not transcript_path or not os.path.exists(transcript_path):
        return False
    try:
        with open(transcript_path, "rb") as f:
            f.seek(start_byte)
            tail = f.read()
    except OSError:
        return False
    if not tail:
        return False
    for raw in tail.split(b"\n"):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        t = obj.get("type")
        if t == "assistant":
            content = obj.get("message", {}).get("content", [])
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") in ("text", "tool_use"):
                        # Ignore empty redacted thinking-only blocks; require
                        # real text or a tool_use to count as "moved on".
                        if c.get("type") == "tool_use":
                            return True
                        if (c.get("text") or "").strip():
                            return True
        elif t == "user":
            content = obj.get("message", {}).get("content", [])
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        return True
    return False


def _turn_first_output_kind(transcript_path: str, start_byte: int) -> str | None:
    """Classify this turn's output past `start_byte` for the show-gate.

    Returns:
      "tool" — the turn fired at least one tool_use (or a tool_result landed):
               a working/agentic turn → counts as narration, SHOW the indicator.
      "text" — the turn emitted assistant text but NO tool_use: so far a bare
               reply with no narration. The gate waits to see if a tool follows.
      None   — no real output yet (silent / still thinking).

    "tool" wins over "text": a single scan returns "tool" if ANY non-Discord
    tool_use is present, else "text" if any non-empty text block is present, else
    None. The reflexive-reply suppression keys off a turn that ENDS as "text".

    CRITICAL: the Discord reply/react tools (mcp__plugin_discord_discord__*) are
    EXCLUDED — they ARE the reply mechanism, present on every reply turn, so
    counting them would make every turn read as "working" and the reflexive-reply
    suppression would never fire. Narration follows REAL work (Bash/Edit/Read/…),
    not the act of replying.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return None
    try:
        with open(transcript_path, "rb") as f:
            f.seek(start_byte)
            tail = f.read()
    except OSError:
        return None
    if not tail:
        return None
    saw_text = False
    for raw in tail.split(b"\n"):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        t = obj.get("type")
        if t == "assistant":
            content = obj.get("message", {}).get("content", [])
            if isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_use":
                        name = c.get("name") or ""
                        if name.startswith("mcp__plugin_discord_discord__"):
                            continue  # the reply mechanism, not narration/work
                        return "tool"
                    if c.get("type") == "text" and (c.get("text") or "").strip():
                        saw_text = True
        # NOTE: we deliberately do NOT inspect user-entry tool_results here. A
        # real tool always appears as an assistant `tool_use` FIRST (returns
        # "tool" above), so by document order any tool_result we'd reach without
        # having already returned belongs to a Discord reply/react — not work.
        # Counting it would misclassify a reflexive reply (text → reply →
        # reply-result) as a working turn.
    return "text" if saw_text else None


def _turn_has_thinking_block(transcript_path: str, start_byte: int) -> bool:
    """Did the model emit a real thinking block this turn?

    Scans the transcript tail (past start_byte — captured right after the inbound
    prompt) for an assistant entry carrying a `type:thinking` content block. The
    body is redacted (empty + signature), but the BLOCK ITSELF is ground truth
    that the model actually REASONED — present for extended AND regular thinking,
    streamed as its own timestamped entry during the turn (verified 2026-06-24).
    This is what lets the indicator key off real reasoning instead of guessing
    from settings/effort (the on/off toggle is invisible to hooks) or from dwell
    alone (which can't tell thinking from slow latency)."""
    if not transcript_path or not os.path.exists(transcript_path):
        return False
    try:
        with open(transcript_path, "rb") as f:
            f.seek(start_byte)
            tail = f.read()
    except OSError:
        return False
    for raw in tail.split(b"\n"):
        if b'"thinking"' not in raw:   # cheap prefilter before the JSON parse
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if obj.get("type") != "assistant":
            continue
        content = obj.get("message", {}).get("content", [])
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "thinking":
                    return True
    return False


def _turn_ended(chat_id: str, msg_id: str) -> bool:
    """Has the WHOLE turn ended (terminal lifecycle react applied)?

    Distinct from _turn_moved_on: this returns True ONLY when a true turn-end
    emoji (✅/🖥️/❌/⚠️) is on the inbound message — NOT for the intermediate
    tool-state emojis (🔧/🌐/🤖). Those mean "the model is acting", which is an
    ACT phase the indicator survives, not a reason to settle. Best-effort:
    any read error → False (don't settle on a transient hiccup).
    """
    try:
        for emoji in _THINK_TERMINAL_EMOJI:
            if _has_applied(chat_id, msg_id, emoji):
                return True
    except Exception:
        pass
    return False


def _think_standdown_reason(wall: float, idle: float) -> str | None:
    """Should the think-updater stand down this tick (timeout path), and why?

    Pure (no I/O) so it's unit-testable. `wall` is seconds since the think
    started; `idle` is seconds since the last observed activity. IDLE is the
    normal orphan stand-down — it resets on every action, so a long but ACTIVE
    turn never trips it (the fix for the trace vanishing mid-long-task). The wall
    ceiling is only a final backstop against a runaway turn. Returns the settle
    reason ("idle" / "timeout") or None to keep animating.
    """
    if idle >= _THINK_IDLE_TIMEOUT_SEC:
        return "idle"
    if wall >= _THINK_HARD_TIMEOUT_SEC:
        return "timeout"
    return None


def _scan_new_act_entries(transcript_path: str, start_byte: int,
) -> tuple[bool, int, bool]:
    """Detect new ACT-phase entries (and any interrupt) appended past `start_byte`.

    "Act" entries = the model produced visible output or a tool ran:
      - assistant message with a non-empty text block or any tool_use, OR
      - user message carrying a tool_result.

    Returns (has_new_act, new_eof, interrupted):
      - has_new_act: True if at least one act entry was found past start_byte.
      - new_eof: the byte length of the file (the offset to resume scanning
        from next poll). Equals start_byte when nothing new / unreadable, so
        the caller never rewinds.
      - interrupted: True if a "[Request interrupted by user]" marker appeared —
        the only signal Claude Code leaves on ESC (no hook, no react fires).

    This is the engine of the THINK/ACT phase machine: a poll that finds new
    act entries means an action happened since last poll (think→act, or
    act-continues); a poll that finds NONE means a quiet interval (which, when
    we're in ACT, signals the model has gone back to thinking).

    Pure read; never raises. Fails toward "no new act" on any error so a
    momentarily-unreadable file doesn't spuriously flip phases.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return (False, start_byte, False)
    try:
        eof = os.path.getsize(transcript_path)
    except OSError:
        return (False, start_byte, False)
    if eof <= start_byte:
        return (False, start_byte, False)
    try:
        with open(transcript_path, "rb") as f:
            f.seek(start_byte)
            tail = f.read()
    except OSError:
        return (False, start_byte, False)
    if not tail:
        return (False, eof, False)
    has_new = False
    interrupted = False
    for raw in tail.split(b"\n"):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        t = obj.get("type")
        if t == "assistant":
            content = obj.get("message", {}).get("content", [])
            if isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_use":
                        has_new = True
                        break
                    if c.get("type") == "text" and (c.get("text") or "").strip():
                        has_new = True
                        break
        elif t == "user":
            content = obj.get("message", {}).get("content", [])
            blocks = (content if isinstance(content, list)
                      else [{"type": "text", "text": content}]
                      if isinstance(content, str) else [])
            for c in blocks:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_result":
                    has_new = True
                # An ESC interrupt lands as a user TEXT entry "[Request
                # interrupted by user]" / "...for tool use" — the turn's only
                # terminal signal (Claude Code fires no hook / react on it).
                txt = c.get("text")
                if isinstance(txt, str) and "Request interrupted by user" in txt:
                    interrupted = True
        if interrupted:
            break
    return (has_new, eof, interrupted)


def _release_think_claim(turn_key: str) -> None:
    """Drop our single-owner claim on the turn (best-effort, under lock).

    Called when the updater stands down for a reason OTHER than emitting the
    duration summary (fast turn during the gap, or watch adopting the stub
    with real prose). Leaving a stale think_owner is harmless to the active
    turn, but clearing it keeps state tidy and lets a late re-spawn (rare)
    take over if the turn somehow isn't finalized yet.
    """
    try:
        with _state_lock():
            state = _load_state()
            turn = _get_turn(state, turn_key)
            if turn and "think_owner" in turn:
                turn.pop("think_owner", None)
                state[turn_key] = turn
                _save_state(state)
    except Exception:
        pass


def _other_live_think_indicator(state: dict, turn_key: str, chat_id: str,
                                now: float) -> bool:
    """Is some OTHER turn in this same channel already showing a live think
    indicator? We spawn one updater per inbound message (react_hook), so a burst
    of messages/taps arriving ~together spawns several updaters with DISTINCT
    turn_keys — the per-turn `think_owner` claim can't dedup across keys, so each
    used to post its own "Thinking…" message ("you emitted 2").
    At any instant a channel should show at most ONE indicator; if another turn
    already owns one (posted, or mid-post via the "__posting__" reservation),
    stand down. A recency bound (the hard-timeout lifetime) keeps a stale
    un-finalized turn from blocking new indicators forever."""
    for k, t in state.items():
        if k == turn_key or not isinstance(t, dict):
            continue
        if str(t.get("chat_id")) != str(chat_id) or t.get("finalized"):
            continue
        if t.get("think_settled"):
            continue  # already settled to "Thought for Ns" — not animating, won't double up
        if not t.get("think_msg_id"):
            continue
        # Block us ONLY if the holder's updater is still ALIVE. A live updater
        # writes think_heartbeat every tick (~_THINK_REFRESH_SEC). A stale
        # heartbeat ⇒ the updater died (crash / killed session / Stop never
        # fired) and its orphaned "Thinking…" must not blackout the channel.
        # Reusing the 12h hard-timeout here is what let one orphan suppress ALL
        # new thinking in a channel for 12h.
        hb = t.get("think_heartbeat")
        if hb is not None:
            if (now - float(hb)) > _THINK_HOLDER_STALE_SEC:
                continue  # dead holder — don't let it block
        else:
            # Legacy entry (or the sub-tick window before the first heartbeat):
            # fall back to turn age. A heartbeat-less holder older than the idle
            # timeout is almost certainly dead — a live one would have written
            # one well before then.
            start = t.get("think_start")
            if start is not None and (now - float(start)) > _THINK_IDLE_TIMEOUT_SEC:
                continue
        return True
    return False


def _settle_think_message(chat_id: str, turn_key: str, effort: str, total_think: float,
    token: str | None, *, reason: str, interrupted: bool = False,
) -> str | None:
    """Edit the standalone think message to the persistent
    '🧠 ✓ Thought for {total}' line and mark the turn settled.

    `total_think` is the CUMULATIVE think time across all think phases this
    turn (NOT turn wall-clock — tool-execution time is excluded), so the
    settled duration reflects actual reasoning effort.

    Marks turn["think_settled"]=True (idempotent — a second caller, e.g. the
    Stop safety net racing the detached updater, edits to the same line and is
    harmless) and releases the single-owner claim. Does NOT delete/collapse —
    that's a separate step (so the Stop hook can settle without a 60s sleep).
    Does NOT touch placeholder_msg_id/buffer/narrate flags.

    Returns the think_msg_id that was edited (or None if there was nothing to
    settle), so the caller can decide whether to schedule a collapse. Never
    raises.
    """
    if not token:
        _release_think_claim(turn_key)
        return None
    try:
        summary = _think_summary(effort, total_think, interrupted=interrupted)
        think_msg_id: str | None = None
        already_settled = False
        with _state_lock():
            state = _load_state()
            turn = _get_turn(state, turn_key)
            if turn:
                think_msg_id = turn.get("think_msg_id")
                already_settled = bool(turn.get("think_settled"))
                turn["think_settled"] = True
                turn.pop("think_owner", None)
                state[turn_key] = turn
                _save_state(state)
        # Edit OUTSIDE the lock (network call). If no message was ever posted
        # (instant turn that slipped past the guard), there's nothing to settle.
        if think_msg_id and not already_settled:
            ok = discord_edit_message(token, chat_id, think_msg_id, summary)
            if ok:
                log(f"think settle turn={turn_key} ({reason}) -> {summary!r}")
        return think_msg_id
    except Exception as e:
        log(f"think settle failed turn={turn_key}: {e}")
        _release_think_claim(turn_key)
        return None


def _think_should_collapse(chat_id: str) -> bool:
    """Whether the settled "Thought for Ns" line should be DELETED after its
    readable linger, vs kept.

    Rule: the line stays whenever there's persistent context to
    sit alongside — a persisting tool-trace (ticker/diffs/full) OR persisting
    narration (always-mode). It collapses ONLY when both are ephemeral: the
    tool-trace is collapse/off AND narration is collapse/never/auto. Concretely:
    a tool-trace on `diff` keeps the Thought-for even if narrate is `collapse`;
    it's removed only in the fully-ephemeral "collapse + collapse" channel.

    Fails toward KEEP (returns False) on any error — never orphan-delete a line."""
    try:
        narrate_m = channel_mode(chat_id)
        tools_m = _channel_tools_mode(detect_discord_state_dir(), chat_id)
    except Exception:
        return False
    tools_persists = tools_m in ("ticker", "diffs", "full")
    narration_persists = narrate_m == "always"
    return not (tools_persists or narration_persists)


def _collapse_think_message(chat_id: str, think_msg_id: str, token: str | None,
                            *, turn_key: str = "") -> None:
    """Delete the settled think message when the channel is fully ephemeral.

    Keeps the "Thought for {N}" line when a tool-trace persists (ticker/diffs/
    full) or narration is `always`; deletes it only when both collapse — see
    _think_should_collapse. CALLERS own the timing — this does
    NOT sleep. The detached updater sleeps _THINK_COLLAPSE_SEC then calls this.
    """
    if not token or not think_msg_id:
        return
    if not _think_should_collapse(chat_id):
        return
    if discord_delete_message(token, chat_id, think_msg_id):
        log(f"think collapsed turn={turn_key} (mode={channel_mode(chat_id)})")


def _msg_id_from_turn_key(turn_key: str) -> str:
    """Recover the inbound Discord message_id from a turn_key.

    turn_key is f"{_bot_id()}:{message_id}" and _bot_id() is a state-dir
    basename (e.g. ".claude", ".claude-bot2", …) that never contains a colon, so the
    LAST colon-segment is always the message_id.
    """
    return turn_key.rsplit(":", 1)[-1]


def _clear_think_react(chat_id: str, msg_id: str, token: str | None) -> None:
    """Remove the 🤔 this updater stamped on the inbound message.

    The updater stamps 🤔 (line ~2117) but historically relied SOLELY on
    react_hook's Stop-hook cleanup to take it off again. That cleanup can race
    the detached stamp: on a fast turn — especially a content-react one where
    the 😮/🎉 IS the response and Stop suppresses 🖥️ — the bot reacts and Stop
    runs BEFORE this updater wins its start_byte capture, so _turn_moved_on
    sees nothing new and stamps 🤔 AFTER the cleanup already passed. Nothing
    then removes it and it orphans until the 720s idle timeout (
    2026-06-24: "Thinking emoji failed to clear"). Fix: whoever stamped the 🤔
    also removes it at settle. discord_unreact is idempotent + state-aware
    (no-op if not applied), so this is safe to call in every settle path,
    including reply turns where react_hook already flipped 🤔→✅.
    """
    if not token:
        return
    try:
        discord_unreact(token, chat_id, msg_id, EMOJI["thinking"])
    except Exception as e:  # never let cleanup crash a settle
        log(f"think react-clear failed chat={chat_id} msg={msg_id}: {e}")


def _finalize_think_summary(chat_id: str, turn_key: str, effort: str, total_think: float,
    token: str | None, *, reason: str, interrupted: bool = False,
) -> None:
    """Settle + (after a delay) collapse the standalone think message.

    Called from the DETACHED updater when the turn ends (terminal react or
    hard timeout). `total_think` is the cumulative think time. We settle the
    message in place, then — for collapse/never channels — sleep
    _THINK_COLLAPSE_SEC and delete it. We're in the detached updater, so the
    sleep blocks nothing. Best-effort throughout; never raises.
    """
    think_msg_id = _settle_think_message(chat_id, turn_key, effort, total_think, token, reason=reason,
        interrupted=interrupted,
)
    # Always strip our own 🤔 from the inbound at settle — closes the
    # stamp-after-cleanup race (see _clear_think_react). Done regardless of
    # whether an indicator message existed (a sub-1s turn posts no indicator
    # but may still have stamped 🤔 before bailing).
    _clear_think_react(chat_id, _msg_id_from_turn_key(turn_key), token)
    if not think_msg_id or not token:
        return
    if _think_should_collapse(chat_id):
        # Keep the settled "🧠 ✓ Thought for Ns" up for _THINK_COLLAPSE_SEC so
        # the duration is readable, THEN delete it — but ONLY in the fully-
        # ephemeral channel (narrate collapse/never/auto AND tool-trace
        # collapse/off). A persisting tool-trace (diffs/ticker/full) or
        # always-narration keeps the line alongside that context. 60s linger set 2026-06-21 for readability.
        time.sleep(_THINK_COLLAPSE_SEC)
        _collapse_think_message(chat_id, think_msg_id, token, turn_key=turn_key)


def handle_think_prereply(payload: dict, effort: str = "",
                          tool_is_work: bool = False) -> int:
    """Anchor the standalone think indicator ABOVE the upcoming reply.

    tool_is_work=True is set by the dispatch when this fires on a real
    (non-Discord) tool's PreToolUse — the about-to-run tool IS the work signal,
    so we anchor without waiting for its tool_use to appear in the transcript
    (it only lands at PostToolUse, AFTER the trace is created — too late to
    anchor above the trace). This is the path that puts the indicator above the
    🔧 tool trace.

    Fires as PreToolUse on the Discord reply tool — i.e. AFTER the model has
    finished reasoning (so its `type:thinking` block is already in the
    transcript) but BEFORE the reply message ships. On a short think→reply turn
    the detached updater's post races the reply to Discord's API and loses,
    landing the indicator BELOW the reply. The detached path
    is correct on long turns (the block streams early) but inherently racy on
    short ones because it posts on its own clock.

    The fix mirrors the narrate-placeholder + tool-trace prereply anchor: post
    the indicator HERE, synchronously, when a real thinking block exists for
    this turn. That guarantees it's above the reply. The detached updater then
    ADOPTS the message — it sees `think_msg_id` already set (line ~2309,
    `already=True`), skips its own post, and falls straight into the animate/
    settle loop on the message we created.

    Show-guard — matches the detached updater's show rule:
    anchor when the turn has done real WORK or reasoning by anchor-time — a
    `type:thinking` block OR a real (non-Discord) tool_use. A bare reflexive
    reply (only text, no work) anchors nothing. This is what lets the anchor
    fire on a toggle-off working turn (no block, but a tool ran) — the case that
    was landing the indicator below the trace because the old block-only guard
    never fired. `never`-narrate channels are skipped, same as the detached path.

    Best-effort throughout — a failure here must NEVER block the reply/tool.
    Returns 0 always.
    """
    try:
        transcript_path = payload.get("transcript_path") or ""
        if not transcript_path:
            return 0
        resolved = _resolve_turn_target(payload, transcript_path, is_prereply=True,
)
        if resolved is None:
            log("think prereply bail: resolve_turn_target=None")
            return 0
        chat_id, message_id, turn_ts = resolved

        # `never`-narrate channels never show the indicator (matches the
        # detached updater's only suppression).
        if channel_mode(chat_id) == "never":
            log(f"think prereply bail: never-channel chat={chat_id}")
            return 0

        # Turn-start boundary: everything appended after the inbound prompt is
        # this turn. Scanning from 0 would catch PRIOR turns' work/blocks and
        # false-anchor on a reflexive reply.
        start_byte = _byte_offset_after_current_user_turn(transcript_path, turn_ts)
        # Show-rule (same as the detached gate): a real thinking block OR a real
        # tool_use ("work"). A turn that's only emitted text so far → nothing to
        # anchor yet (the reflexive-reply case; if it later does work, that
        # tool's own prereply re-fires this and anchors then).
        # tool_is_work short-circuits the transcript scan: the about-to-run tool
        # IS the work (its tool_use isn't written until PostToolUse, which is too
        # late to land above the trace). For the reply-tool path (tool_is_work
        # False) we still require a block or already-recorded work — a bare
        # reflexive reply anchors nothing.
        if not tool_is_work:
            has_block = _turn_has_thinking_block(transcript_path, start_byte)
            kind = _turn_first_output_kind(transcript_path, start_byte)
            if not has_block and kind != "tool":
                log(f"think prereply bail: no work/block yet (kind={kind}) "
                    f"start_byte={start_byte} chat={chat_id}")
                return 0

        turn_key = _turn_key_for_message(message_id)
        token = read_bot_token(detect_discord_state_dir())
        if not token:
            return 0

        # Reserve the channel's single indicator slot atomically, exactly like
        # the detached updater (line ~2304): bail if this turn already has one,
        # or another live turn in the channel owns the slot, else stamp the
        # "__posting__" sentinel before the network call so a racing detached
        # post stands down. Reuse the same `started` origin the updater would.
        now = time.time()
        reserved = False
        with _state_lock():
            state = _load_state()
            turn = _get_turn(state, turn_key)
            if turn is None:
                # Seed a minimal record so the detached updater (which may not
                # have spawned/claimed yet) and the offset walker agree on the
                # turn boundary. Mirrors run_think_updater's seed.
                turn = {
                    "chat_id": chat_id,
                    "placeholder_msg_id": None,
                    "think_msg_id": None,
                    "last_byte_offset": start_byte,
                    "buffer": "",
                    "mode": channel_mode(chat_id),
                    "finalized": False,
                    "replies_at_create": 0,
                    "sealed_placeholders": [],
                    "seen_text_uuids": [],
                }
            if turn.get("finalized"):
                log(f"think prereply bail: turn finalized turn={turn_key}")
                return 0
            if turn.get("think_msg_id"):
                log(f"think prereply bail: think_msg_id already set "
                    f"({turn.get('think_msg_id')!r}) turn={turn_key}")
                return 0  # already posted/reserved (detached path won, or straggler)
            if _other_live_think_indicator(state, turn_key, chat_id, now):
                log(f"think prereply bail: other live indicator in channel turn={turn_key}")
                return 0  # another live turn owns the channel's single indicator
            # Pin the time origin so the eventual "Thought for Ns" total is
            # computed from one shared start even though the detached updater
            # adopts the message. Don't clobber an existing think_start (the
            # updater may have claimed first and set its own).
            turn.setdefault("think_start", now)
            turn["think_msg_id"] = "__posting__"  # reserve before network
            state[turn_key] = turn
            _save_state(state)
            reserved = True

        if not reserved:
            return 0

        # Post the indicator at tick 0 (same content the detached updater posts
        # first). The detached updater adopts this msg_id and animates/settles
        # it from here.
        content = _think_phrase(effort, 0.0, _think_glyph(0), _think_dots(0))
        try:
            tmid = discord_send_message(token, chat_id, content)
        except Exception:
            tmid = None
        with _state_lock():
            state = _load_state()
            turn = _get_turn(state, turn_key)
            if turn is not None:
                # Resolve the reservation: real id on success, None on failure —
                # never leave the "__posting__" sentinel (a later edit would
                # target a phantom). On failure the detached updater's own gate
                # will retry the post.
                turn["think_msg_id"] = tmid if tmid else None
                state[turn_key] = turn
                _save_state(state)
        if tmid:
            log(f"think prereply anchored indicator {tmid} turn={turn_key}")
    except Exception as e:
        log(f"think prereply anchor failed: {e}")
    return 0


def run_think_updater(chat_id: str, msg_id: str, effort: str, transcript_path: str,
) -> None:
    """Detached Mode-A worker. Stamp 🤔 after a gap, then post + animate a
    STANDALONE thinking-indicator Discord message that persists.

    Behavior (4th-iteration spec):
      - The indicator is its OWN message (turn["think_msg_id"]), distinct from
        the narrate placeholder. It sits ABOVE the narrate block / reply.
      - It is NOT gated by /bot narrate rules — it ALWAYS appears whenever
        extended thinking is on, even in a `never`-narrate channel.
      - It animates: a Claude-Code-style spinner glyph (✻ ✢ ✳ ✶ ✷ ✸) cycles
        every 1.5s, and the escalation text steps up at 30/60/90s.
      - When the think ends it settles to "🧠 ✻ Thought for {N}s{effort}" and
        STOPS. That line PERSISTS — never deleted, fully decoupled from the
        narrate finalize/collapse lifecycle.
      - A sub-1s turn (output during the gap) shows NOTHING — we never post.

    EVERYTHING is wrapped so a crash here can never affect a turn or another
    hook — this runs in its own detached process, but we still swallow all
    errors and log them rather than raising.
    """
    turn_key = _turn_key_for_message(msg_id)
    try:
        # ── 0. Single-owner claim. UserPromptSubmit can fire more than once
        #    for the same inbound message (harness re-fires, queued bursts),
        #    and each fire spawns a fresh detached updater. Without a claim,
        #    N updaters all stamp 🤔 (log spam — Discord dedupes the react
        #    itself, but the calls pile up) and, worse, all drive the SAME
        #    placeholder with independent `started` clocks, so the escalation
        #    line oscillates ("Almost done" → "Thinking some more" → …) as
        #    they fight. Claim the turn atomically under the state lock; only
        #    the first updater proceeds, the rest exit immediately. The claim
        #    also stamps think_start so the "Thought for Ns" summary can be
        #    computed from a single shared origin even if watch finalizes.
        claimed = False
        try:
            with _state_lock():
                state = _load_state()
                turn = _get_turn(state, turn_key)
                if turn is None:
                    # Seed a minimal record now so the offset walker still
                    # bounds the turn correctly when watch first fires.
                    turn_ts = _ts_for_message(transcript_path, msg_id)
                    initial_offset = _byte_offset_after_current_user_turn(transcript_path, turn_ts
)
                    turn = {
                        "chat_id": chat_id,
                        "placeholder_msg_id": None,
                        "think_msg_id": None,
                        "last_byte_offset": initial_offset,
                        "buffer": "",
                        "mode": channel_mode(chat_id),
                        "finalized": False,
                        "replies_at_create": 0,
                        "sealed_placeholders": [],
                        "seen_text_uuids": [],
                    }
                if turn.get("finalized"):
                    # Turn already wrapped — nothing for this updater to do.
                    return
                if turn.get("think_owner"):
                    # Another updater already owns this turn — stand down.
                    log(f"think-updater duplicate spawn, standing down "
                        f"turn={turn_key}")
                    return
                turn["think_owner"] = os.getpid()
                turn["think_start"] = time.time()
                claimed = True
                state[turn_key] = turn
                _save_state(state)
        except Exception as e:
            # If the claim itself failed we can't safely coordinate — bail
            # rather than risk a second uncoordinated updater.
            log(f"think-updater claim failed turn={turn_key}: {e}")
            return
        if not claimed:
            return

        # Capture the transcript EOF at turn-start (BEFORE the gap). Anything
        # appended past this is this turn's first real output. This MUST be
        # taken here: the inbound prompt is the last thing in the transcript
        # right now, so output during/after the gap is the move-on signal.
        # ⚠️ BUG FIX: the gap-check below previously passed start_byte=0, which
        # made _turn_moved_on scan the ENTIRE transcript (always full of prior
        # assistant turns) → it always read "moved on" → the updater bailed
        # after the 1s gap before ever stamping 🤔 or posting the block. That
        # was why the thinking block never appeared on long xhigh turns.
        start_byte = 0
        try:
            if transcript_path and os.path.exists(transcript_path):
                start_byte = os.path.getsize(transcript_path)
        except OSError:
            start_byte = 0

        # ── 1. The gap (instant-turn guard), then 🤔. _do_react flips 👀→🤔
        #    via the predecessor table, exactly like a PreToolUse thinking
        #    emoji would. If the turn already produced output during the gap
        #    (sub-1s turn), show NOTHING — don't stamp 🤔, don't post the
        #    indicator — and release the claim.
        time.sleep(_THINK_GAP_SEC)
        # Don't stamp 🤔 if the turn already ended during the gap. A fast turn
        # can finish AND get finalized by the Stop hook's sweep entirely within
        # _THINK_GAP_SEC. _turn_moved_on misses a turn whose only output (a
        # content react, or a fast reply) landed BEFORE we captured start_byte
        # (the detached spawn can lose that race), so without these extra
        # checks we'd stamp 🤔 AFTER react_hook already cleaned transients at
        # Stop and orphan it. finalized / _turn_ended catch
        # the ended-during-gap case; _turn_moved_on catches the still-running
        # one that produced output.
        ended = False
        try:
            with _state_lock():
                _t = _get_turn(_load_state(), turn_key)
            ended = bool(_t and _t.get("finalized"))
        except Exception:
            ended = False
        if (ended or _turn_ended(chat_id, msg_id)
                or _turn_moved_on(transcript_path, "", start_byte, chat_id, msg_id)):
            _release_think_claim(turn_key)
            return
        try:
            _do_react(chat_id, msg_id, EMOJI["thinking"], "thinking")
        except Exception as e:
            log(f"think-updater 🤔 stamp failed chat={chat_id}: {e}")

        # ── 2. The single shared time origin for the duration summary: prefer
        #    the claim's think_start (set under the lock above) so it survives
        #    even if another component re-reads the turn before we settle.
        token = read_bot_token(detect_discord_state_dir())
        started = time.time()
        try:
            with _state_lock():
                t = _get_turn(_load_state(), turn_key)
            if t and t.get("think_start"):
                started = float(t["think_start"])
        except Exception:
            pass

        # ── 2a. The ONLY suppression: a `never`-narrate channel. 
        #    — always show the indicator whenever the model actually thinks
        #    (accurate think time, regardless of the extended-thinking toggle),
        #    EXCEPT where narration is turned off entirely. (The 🤔 react above
        #    still fires as the lightweight working signal.) All the earlier
        #    "hide it when extended thinking is off" logic is gone — the toggle
        #    isn't the determinant; real reasoning is.
        if channel_mode(chat_id) == "never":
            _release_think_claim(turn_key)
            return

        # ── 2b. Show-gate: the indicator shows unless the turn
        #    emitted ZERO narration AND ZERO thinking blocks. "Narration" here =
        #    a working/agentic turn — the model fired a tool (the narrate trace
        #    follows tool work), so a tool_use is the narration signal. So we
        #    SHOW on: a real `type:thinking` block (→ accurate think-time) OR any
        #    tool_use (a narrating turn). We only SUPPRESS the bare reflexive
        #    reply: a single text block, no tool, no thinking. This decouples the
        #    indicator from the extended-thinking toggle entirely — with the
        #    toggle off the model emits no blocks, but a working turn still
        #    narrates, so it still shows. _THINK_SHOW_SEC is the ceiling fallback
        #    for a silent turn whose first output hasn't flushed.
        while True:
            if _turn_has_thinking_block(transcript_path, start_byte):
                break  # reasoning confirmed → show (accurate think-time)
            kind = _turn_first_output_kind(transcript_path, start_byte)
            if kind == "tool":
                break  # a narrating/working turn → show
            if kind == "text":
                # Bare reply, no tool/thinking yet. Give the rest of the turn a
                # beat to fire a tool (→ narration) before deciding it's purely
                # reflexive. If the whole turn ends with only that text and no
                # tool/block, it's a reflexive reply → suppress.
                if _turn_ended(chat_id, msg_id) or (time.time() - started) >= _THINK_SHOW_SEC:
                    if not _turn_has_thinking_block(transcript_path, start_byte) \
                            and _turn_first_output_kind(transcript_path, start_byte) == "text":
                        _release_think_claim(turn_key)  # reflexive reply → no indicator
                        return
                    break
            if (time.time() - started) >= _THINK_SHOW_SEC:
                break  # silent this long, output not yet flushed → show anyway
            time.sleep(_THINK_SHOW_POLL_SEC)

        # ── 3. Post the STANDALONE think-indicator message — the model is
        #    genuinely reasoning, settling to the real "Thought for Ns". It is
        #    its OWN message (turn["think_msg_id"]), separate from the narrate
        #    placeholder. (Suppressed only in `never` channels — gated above.)
        if not token:
            # No token → nothing to post or settle. We've already stamped 🤔.
            _release_think_claim(turn_key)
            return
        try:
            # Decide-and-reserve ATOMICALLY under one lock: the old code read
            # `already` inside the lock but posted OUTSIDE it, so two updaters
            # (same turn, OR — the real case — different turn_keys from a burst
            # of inbound messages) both saw "no indicator yet" and both posted.
            # We now reserve the slot ("__posting__") before releasing the lock,
            # and stand down if THIS turn already has one or ANOTHER turn in the
            # channel already owns the channel's single indicator.
            already = False
            stand_down = False
            bail_finalized = False
            with _state_lock():
                state = _load_state()
                turn = _get_turn(state, turn_key)
                if not turn or turn.get("finalized"):
                    bail_finalized = True
                elif turn.get("think_msg_id"):
                    already = True  # straggler — this turn already posted one
                elif _other_live_think_indicator(state, turn_key, chat_id, started):
                    # reuse the already-captured `started` (≈now) rather than a
                    # fresh time() — an extra clock read would perturb callers /
                    # tests that count time() calls; the 300s staleness bound
                    # doesn't need sub-second precision.
                    stand_down = True
                else:
                    turn["think_msg_id"] = "__posting__"  # reserve before network
                    state[turn_key] = turn
                    _save_state(state)
            # Both bail paths happen AFTER we stamped 🤔 (line ~2117) but before
            # the loop that would settle + clear it — so strip our 🤔 here too,
            # outside the lock (no HTTP under flock). Idempotent if already gone.
            if bail_finalized:
                _clear_think_react(chat_id, msg_id, token)
                _release_think_claim(turn_key)
                return
            if stand_down:
                log(f"think-updater dup indicator in channel, standing down "
                    f"turn={turn_key}")
                _clear_think_react(chat_id, msg_id, token)
                _release_think_claim(turn_key)
                return
            if not already:
                content = _think_phrase(effort, 0.0, _think_glyph(0), _think_dots(0))
                try:
                    tmid = discord_send_message(token, chat_id, content)
                except Exception:
                    tmid = None
                with _state_lock():
                    state = _load_state()
                    turn = _get_turn(state, turn_key)
                    if turn is not None:
                        # Always RESOLVE the "__posting__" reservation: the real
                        # id on success, None on failure — never leave the phantom
                        # sentinel behind (a later tick would try to edit it).
                        turn["think_msg_id"] = tmid if tmid else None
                        state[turn_key] = turn
                        _save_state(state)
                if tmid:
                    log(f"think-updater posted indicator {tmid} turn={turn_key}")
        except Exception as e:
            log(f"think-updater indicator post failed turn={turn_key}: {e}")

        # ── 4. Phase state machine. ONE message per turn that REACTIVATES
        #    between think and act phases — NEVER a new message per segment
        #    (that was the spam bug of the prior attempt).
        #
        #    THINK phase: animate the spinner + escalation tiers; the tier is
        #      keyed to THIS phase's elapsed, the displayed total is
        #      (think_total + this-phase-elapsed). Each tick edits the ONE
        #      message to "🧠 {spinner} Thinking…".
        #    ACT phase: the model produced output / a tool is running. Edit the
        #      SAME message ONCE to the static "🧠 ✓ Thought for {total}" — no
        #      spinner, no new message, no delete. (This also keeps the think
        #      msg from competing with the agent-view panel during tool runs.)
        #    REACTIVATE: a quiet interval AFTER an action ⇒ the model went back
        #      to thinking → flip to THINK, restart this phase's clock, keep
        #      accumulating into think_total.
        #
        #    Phase detection polls the transcript every _THINK_REFRESH_SEC:
        #    new act-entries past last_seen ⇒ ACT; a full quiet interval after
        #    an action ⇒ a new THINK phase. Turn-start (no output yet) is the
        #    first THINK phase. Turn END = a terminal react (✅/🖥️/❌/⚠️), the
        #    finalized flag, or the hard timeout — NOT an intermediate tool
        #    emoji. discord_edit_message backs off on 429s internally; a failed
        #    edit just skips the frame (never crash).
        phase = _PHASE_THINK
        phase_start = started        # wall-clock origin of the current phase
        think_total = 0.0            # cumulative seconds across ENDED think phases
        last_seen = start_byte       # transcript offset already consumed
        last_act = started           # wall-clock of the last observed activity (output/tool); idle timeout measures from here
        tick = 0
        last_content: str | None = None

        def _total_now(now: float) -> float:
            """Cumulative think seconds including the in-flight think phase."""
            if phase == _PHASE_THINK:
                return think_total + (now - phase_start)
            return think_total

        while True:
            time.sleep(_THINK_REFRESH_SEC)
            tick += 1
            now = time.time()

            # ── Compaction guard (before everything else). While the harness is
            #    compacting THIS turn's context, the transcript is quiet and the
            #    phase machine below would falsely read it as "Thinking". Show a
            #    Compacting indicator instead, treat the time as ACT (not think)
            #    so it's excluded from the "Thought for Ns" total, and keep the
            #    idle clock fresh so the updater doesn't stand down mid-compaction.
            if _compacting_active(msg_id):
                last_act = now
                if phase == _PHASE_THINK:
                    think_total += now - phase_start  # bank the pre-compaction think
                phase = _PHASE_ACT
                phase_start = now
                try:
                    with _state_lock():
                        state = _load_state()
                        turn = _get_turn(state, turn_key)
                        if not turn or turn.get("finalized"):
                            _release_think_claim(turn_key)
                            return
                        tmid = turn.get("think_msg_id")
                        turn["think_phase"] = phase
                        turn["think_total"] = think_total
                        turn["think_phase_start"] = phase_start
                        state[turn_key] = turn
                        _save_state(state)
                    if tmid:
                        compacting = (f"📝 {_think_glyph(tick)} **Compacting context"
                            f"{_think_dots(tick)}**"
)
                        if compacting != last_content:
                            discord_edit_message(token, chat_id, tmid, compacting)
                            last_content = compacting
                except Exception as e:
                    log(f"think-updater compacting render error turn={turn_key}: {e}")
                continue

            # ── Turn END checks (highest priority): terminal react / finalized
            #    / idle / absolute ceiling. Settle to the CUMULATIVE think total
            #    and stop. The IDLE check (not an absolute cap) is what lets a
            #    long-but-active turn keep its indicator: as long as the model is
            #    producing output/tools, `last_act` keeps advancing and we never
            #    time out. Only a turn gone genuinely silent (orphaned — Stop never
            #    fired AND no terminal react landed) idles out. The absolute
            #    ceiling is a final backstop for a runaway turn.
            wall = now - started
            idle = now - last_act
            standdown = _think_standdown_reason(wall, idle)
            if standdown:
                log(f"think-updater {standdown} turn={turn_key} "
                    f"wall={wall:.0f}s idle={idle:.0f}s")
                _finalize_think_summary(chat_id, turn_key, effort, _total_now(now), token,
                    reason=standdown,
)
                return
            if _turn_ended(chat_id, msg_id):
                log(f"think-updater turn-ended turn={turn_key} "
                    f"after {wall:.0f}s think_total={_total_now(now):.0f}s")
                _finalize_think_summary(chat_id, turn_key, effort, _total_now(now), token,
                    reason="turn-ended",
)
                return

            # ── Phase transition detection from the transcript.
            try:
                has_new_act, new_eof, interrupted = _scan_new_act_entries(transcript_path, last_seen,
)
            except Exception as e:
                log(f"think-updater scan error turn={turn_key}: {e}")
                has_new_act, new_eof, interrupted = False, last_seen, False
            last_seen = new_eof

            # ── Interrupt: the user hit ESC on the Claude Code side. CC fires no
            #    hook and stamps no terminal react, so this transcript marker is
            #    the ONLY signal — settle the indicator to "✗ Interrupted" and
            #    stop, instead of animating "Thinking…" to the idle timeout
            #.
            if interrupted:
                log(f"think-updater interrupted turn={turn_key} after {wall:.0f}s")
                _finalize_think_summary(chat_id, turn_key, effort, _total_now(now), token,
                    reason="interrupted", interrupted=True,
)
                return

            if has_new_act:
                # The model acted (text/tool_use/tool_result appeared) — the turn
                # is demonstrably alive, so reset the idle clock.
                last_act = now
                if phase == _PHASE_THINK:
                    # think → act: this think phase ended; bank its duration.
                    think_total += now - phase_start
                    phase = _PHASE_ACT
                    phase_start = now
                else:
                    # act continues: refresh the act-phase clock (so a quiet
                    # interval is measured from the LAST action, not the first).
                    phase_start = now
            else:
                # No new act entries this poll.
                if phase == _PHASE_ACT and (now - phase_start) >= _THINK_REACTIVATE_GAP_SEC:
                    # A quiet interval after the last action ⇒ the model went
                    # back to thinking. Reactivate the SAME message.
                    phase = _PHASE_THINK
                    phase_start = now
                    last_content = None  # force the next render to repaint

            # ── Re-read live think_msg_id; bail if the turn finalized. Also
            #    persist the phase + cumulative total so the Stop-hook safety
            #    net can settle to a meaningful (think-only, act-excluded)
            #    duration if this detached updater is killed before it settles.
            try:
                turn_finalized = False
                with _state_lock():
                    state = _load_state()
                    turn = _get_turn(state, turn_key)
                    if not turn:
                        _release_think_claim(turn_key)
                        return
                    turn_finalized = bool(turn.get("finalized"))
                    if not turn_finalized:
                        tmid = turn.get("think_msg_id")
                        turn["think_phase"] = phase
                        turn["think_total"] = think_total
                        turn["think_phase_start"] = phase_start
                        # Liveness beacon: _other_live_think_indicator trusts a
                        # holder to block new turns only while this stays fresh,
                        # so a dead updater can't blackout the channel.
                        turn["think_heartbeat"] = now
                        state[turn_key] = turn
                        _save_state(state)
                if turn_finalized:
                    # Stop ran. The Stop-hook safety net settled the indicator but
                    # no longer deletes it, so WE own the readable collapse delay:
                    # settle (idempotent — already done) then sleep _THINK_COLLAPSE_SEC
                    # and collapse, exactly like a normal turn-end. This makes the
                    # "Thought for Ns" linger the same in EVERY mode (incl. never),
                    # instead of the reply-ended turn vanishing instantly. Done OUTSIDE the lock — the sleep blocks only us.
                    log(f"think-updater finalized turn={turn_key} -> collapse delay")
                    _finalize_think_summary(chat_id, turn_key, effort, _total_now(now), token,
                        reason="finalized",
)
                    _release_think_claim(turn_key)
                    return
                if not tmid:
                    continue  # post failed earlier; nothing to animate
            except Exception as e:
                log(f"think-updater state read error turn={turn_key}: {e}")
                continue

            # ── Render. ONE settled "Thought for Ns" per turn — posted ONCE at
            #    turn-end by _finalize_think_summary, NOT on every think→act
            #    cycle. Previously the ACT phase repainted the message to
            #    "🧠 ✓ Thought for {total}" each time the model acted, so a
            #    multi-tool high-effort turn flashed "Thought for 5s", "10s",
            #    "15s"… (the noise the user flagged). Now we keep the calm spinner
            #    for the WHOLE turn (think AND act), tiered by cumulative think
            #    time; the single "Thought for Ns" lands only at the end.
            try:
                content = _think_phrase(effort, _total_now(now), _think_glyph(tick),
                                        _think_dots(tick))
                if content != last_content:
                    if discord_edit_message(token, chat_id, tmid, content):
                        last_content = content
            except Exception as e:
                log(f"think-updater render error turn={turn_key}: {e}")
                # keep looping; a transient edit failure shouldn't kill us
    except Exception:
        try:
            log("think-updater crash:\n" + "".join(__import__("traceback").format_exc()))
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode",
        choices=("watch", "prereply", "finalize", "think-updater"),
        required=True,
)
    # think-updater args (detached child spawned by react_hook.handle_received).
    ap.add_argument("--chat-id")
    ap.add_argument("--message-id")
    ap.add_argument("--effort", default="")
    ap.add_argument("--transcript", default="")
    args = ap.parse_args()

    if args.mode == "think-updater":
        # No stdin payload — args carry everything. Never raises (the worker
        # swallows all errors); detached so it outlives the spawning hook.
        run_think_updater(args.chat_id or "", args.message_id or "",
            args.effort or "", args.transcript or "",
)
        return 0

    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log("bad JSON input, passing through")
        return 0

    if args.mode == "watch":
        return handle_watch(payload)
    if args.mode == "prereply":
        # Fires as PreToolUse on the Discord reply tool. Same logic as
        # handle_watch, but the timing guarantees the placeholder lands
        # BEFORE the reply message — narrate always reads as "the bot
        # was thinking THIS, then said THAT". Without prereply, narration
        # often races behind the reply tool because PostToolUse hooks
        # fire after the reply has shipped.
        #
        # is_prereply=True forces placeholder creation even when no prose
        # has accumulated yet, so post-reply prose edits THIS placeholder
        # instead of creating a new one below the reply.
        tool_name = payload.get("tool_name", "")
        is_reply = tool_name == "mcp__plugin_discord_discord__reply"
        is_discord = tool_name.startswith("mcp__plugin_discord_discord__")

        # ── THINK INDICATOR anchor — runs on the FIRST real (non-Discord) tool's
        #    PreToolUse AND on the reply tool's PreToolUse. Anchoring at the first
        #    tool is what puts the indicator ABOVE the tool trace: that tool's
        #    PostToolUse is what lazily creates the 🔧 trace message, and we post
        #    the indicator at its PreToolUse — strictly earlier ⇒ strictly above
        #    (Discord positions by post time). Anchoring at the reply covers the
        #    no-tool think→reply turn. handle_think_prereply is idempotent (it
        #    no-ops once an indicator is up) and show-gated (block OR real work),
        #    so calling it on every non-Discord tool is safe — only the first one
        #    that satisfies the show-rule actually posts. Desired final order:
        #    🧠 indicator  >  🔧 tool trace  >  reply (last).
        if not is_discord or is_reply:
            try:
                # A real (non-Discord) tool firing IS work → anchor without
                # waiting for the tool_use to hit the transcript. The reply tool
                # is not "work" itself, so it anchors only on a block/recorded
                # work (the think→reply case).
                handle_think_prereply(payload, tool_is_work=(not is_discord))
            except Exception as e:
                log(f"think prereply anchor dispatch failed: {e}")

        # The narrate-prose + tool-trace anchors only apply to the REPLY tool
        # (they position relative to the upcoming reply). A non-reply tool's
        # PreToolUse just needed the indicator anchor above; return now.
        if not is_reply:
            return 0

        rc = handle_watch(payload, is_prereply=True)
        # Anchor the TOOL trace above the upcoming reply too — same guarantee
        # the prose placeholder gets. Without this, an echo-first turn (reply,
        # then tool work) creates the trace AFTER the reply has shipped, so it
        # lands below the bot's output. Late import: tool_watcher imports from
        # narrate at module load, so importing it here (not at top) avoids the
        # circular load. Fully isolated — a failure never blocks the reply.
        try:
            import tool_watcher
            tool_watcher.handle_tool_prereply(payload)
        except Exception as e:
            log(f"tool-trace prereply anchor failed: {e}")
        return rc
    return handle_finalize(payload)


if __name__ == "__main__":
    sys.exit(main())
