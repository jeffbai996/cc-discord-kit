"""Stop hook: surface CLI API errors (529/429/4xx) to the inbound Discord channel.

WHY THIS EXISTS — the gap notify_hook can't fill:
A permission prompt fires a Claude Code `Notification` event, so notify_hook can
mirror it. A 529 (Anthropic overload) / 429 (rate limit) / 4xx is an HTTP error
on the wire between the CLI and the API — it fires NO Notification event. The CLI
retries internally and, if it gives up, the turn just dies. From Discord's side
the bot goes silent mid-thought and looks dead.

But the CLI DOES write a structured entry into the session transcript JSONL:
  {"type":"assistant", "isApiErrorMessage":true, "apiErrorStatus":529,
   "message":{"role":"assistant","content":[{"type":"text","text":"API Error: 529 ..."}]}}
(schema confirmed against real bot transcripts: statuses seen = 429/529/404/400.)

So on Stop we scan the transcript tail for an UNREPORTED api-error entry (dedup by
its uuid via a per-session state file), classify the status, and post a one-line
heads-up to the EXACT inbound channel (reusing notify_hook's channel-tag resolver).
Stop fires on normal turn-end; if the CLI hard-crashes Stop may not fire — that's
the known residual gap, acceptable since the common 529/429 case ends the turn
cleanly with the entry written.

Registered as an additional `Stop` hook (Claude Code runs all hooks for an event),
sitting alongside the existing stop_hook — it does not replace it.
"""
from __future__ import annotations

import json
import os
import sys
import traceback

# Reuse notify_hook's battle-tested I/O helpers rather than duplicating them:
# token read, state-dir detection, channel-tag resolution, Discord POST, logging.
_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)
import notify_hook as nh  # noqa: E402

_STATE_DIR = os.path.expanduser("~/.local/state/cc-discord-kit")
try:
    os.makedirs(_STATE_DIR, exist_ok=True)
except OSError:
    pass
LOG_PATH = os.environ.get(
    "CCDK_API_ERROR_HOOK_LOG", os.path.join(_STATE_DIR, "api_error_hook.log")
)

# How many transcript lines from the tail to scan. An api-error entry lands at the
# end of the failed turn, so the tail is where it is; bound the read for big files.
_TAIL_SCAN = 400


def log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{msg}\n")
    except OSError:
        pass


def classify_status(status: int) -> tuple[str, str, bool]:
    """Map an HTTP status to (emoji, label, transient).

    transient=True means "will self-heal on retry" (overload/rate-limit) — the
    message can reassure. transient=False means a human likely has to act (a 4xx
    config/request error won't fix itself by waiting).
    """
    if status == 529:
        return "⚠️", "API overloaded (529)", True
    if status == 429:
        return "⏳", "rate-limited (429)", True
    if status == 503:
        return "⚠️", "service unavailable (503)", True
    if 500 <= status < 600:
        return "⚠️", f"server error ({status})", True
    if 400 <= status < 500:
        return "🛑", f"API request error ({status})", False
    return "🛑", f"API error ({status or 'unknown'})", False


def _entry_text(entry: dict) -> str:
    """Pull the human-readable 'API Error: ...' text out of the transcript entry."""
    content = entry.get("message", {}).get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                return c["text"]
    return ""


def format_api_error_message(entry: dict) -> str:
    """Build the Discord heads-up line for an api-error transcript entry."""
    status = entry.get("apiErrorStatus") or 0
    emoji, label, transient = classify_status(int(status) if status else 0)
    detail = _entry_text(entry).strip()
    # Strip a redundant leading "API Error: " — our label already carries the code.
    for prefix in ("API Error:", "API error:"):
        if detail.startswith(prefix):
            detail = detail[len(prefix):].strip()
    if len(detail) > 300:
        detail = detail[:297] + "…"
    tail = " — retrying automatically." if transient else " — this one needs attention (won't self-heal)."
    parts = [f"{emoji} **{label}**"]
    if detail:
        parts.append(f"\n> {detail}")
    parts.append(tail)
    return "".join(parts)


def find_unreported_api_error(lines: list[str], last_seen_uuid: str | None) -> dict | None:
    """Return the most recent api-error transcript entry not yet reported, or None.

    Scans newest-first. An entry is an api-error iff isApiErrorMessage is truthy.
    Dedups by uuid against last_seen_uuid so the same error isn't re-announced on
    a subsequent Stop.
    """
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not obj.get("isApiErrorMessage"):
            continue
        if last_seen_uuid is not None and obj.get("uuid") == last_seen_uuid:
            # Reached the one we already reported — nothing newer is unreported.
            return None
        return obj
    return None


def _state_path(session_id: str) -> str:
    safe = "".join(c for c in (session_id or "default") if c.isalnum() or c in "-_")
    return os.path.join(_STATE_DIR, f"api_error_seen_{safe or 'default'}")


def _read_last_seen(session_id: str) -> str | None:
    try:
        with open(_state_path(session_id)) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _write_last_seen(session_id: str, uuid: str) -> None:
    try:
        with open(_state_path(session_id), "w") as f:
            f.write(uuid)
    except OSError:
        pass


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload: dict = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        log(f"bad JSON input: {raw[:200]!r}")
        return 0

    transcript_path = payload.get("transcript_path", "")
    session_id = payload.get("session_id", "") or os.path.basename(transcript_path)
    if not transcript_path or not os.path.exists(transcript_path):
        return 0

    try:
        with open(transcript_path) as f:
            lines = f.readlines()[-_TAIL_SCAN:]
    except OSError:
        return 0

    last_seen = _read_last_seen(session_id)
    entry = find_unreported_api_error(lines, last_seen)
    if entry is None:
        return 0

    uuid = entry.get("uuid", "")
    # Record FIRST, so a Discord-post failure doesn't cause an infinite re-announce
    # loop on every subsequent Stop. One missed surfacing beats a spam loop.
    if uuid:
        _write_last_seen(session_id, uuid)

    state_dir = nh._detect_state_dir()
    token = nh._read_token(state_dir)
    if not token:
        log(f"no token at {state_dir} — skipping (status={entry.get('apiErrorStatus')})")
        return 0

    channel_id = os.environ.get("NOTIFY_CHANNEL_ID", "").strip()
    if not channel_id:
        origin = nh._origin_from_transcript(transcript_path)
        if origin:
            channel_id = origin[0]
    if not channel_id:
        log(f"no channel target (status={entry.get('apiErrorStatus')}) — skipping")
        return 0

    try:
        msg = format_api_error_message(entry)
    except Exception:
        log(f"format crash:\n{traceback.format_exc()}")
        return 0

    if os.environ.get("NOTIFY_DRY_RUN") == "1":
        log(f"DRY status={entry.get('apiErrorStatus')} channel={channel_id} msg={msg!r}")
        return 0

    ok = nh._discord_post(token, channel_id, msg)
    log(f"status={entry.get('apiErrorStatus')} channel={channel_id} posted={ok}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log(f"main crash:\n{traceback.format_exc()}")
        sys.exit(0)  # never block the Stop event
