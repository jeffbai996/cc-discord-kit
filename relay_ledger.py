"""Append-only ledger of Discord tap/relay events — the audit trail for the
one-tap control surface (veto / choice / todo / retry / vecgrep).

Every tap the relay helper resolves logs one JSON line here, so a tap that
didn't land is debuggable after the fact and the relay has a replayable history.
Best-effort throughout: a ledger failure must NEVER break the tap it's recording
— every public call swallows its own errors.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from jsonlock import rmw_lock

LEDGER_PATH = Path(
    os.environ.get("CCDK_RELAY_LEDGER",
                   os.path.expanduser("~/.local/state/cc-discord-kit/relay-ledger.jsonl")))
# Keep the file bounded — taps are human-paced, so this is months of history.
MAX_LINES = int(os.environ.get("CCDK_RELAY_LEDGER_MAX", "5000"))
# Only pay the read-and-trim cost once the file is plausibly over the cap
# (~300 bytes/line), so the common append stays O(1).
_TRIM_SIZE_HINT = MAX_LINES * 300


def log_event(kind: str, *, chat_id: str = "", message_id: str = "",
              actor: str | int = "", entry_id=None, detail: str = "",
              ok: bool = True, ts: float | None = None) -> None:
    """Append one event line. `kind` is e.g. veto_approve / choice_pick /
    todo_done / retry. `actor` is the tapping user id, `detail` a short human
    label, `ok` whether the action succeeded."""
    try:
        rec = {
            "ts": ts if ts is not None else time.time(),
            "kind": kind,
            "chat_id": str(chat_id),
            "message_id": str(message_id),
            "actor": str(actor),
            "detail": detail,
            "ok": bool(ok),
        }
        if entry_id is not None:
            rec["entry_id"] = entry_id
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with rmw_lock(LEDGER_PATH):
            with open(LEDGER_PATH, "a") as f:
                f.write(json.dumps(rec) + "\n")
            _trim_locked()
    except Exception:
        pass


def _trim_locked() -> None:
    """Rewrite the file keeping only the last MAX_LINES — called under the lock,
    and only when the file is large enough to plausibly need it."""
    try:
        if LEDGER_PATH.stat().st_size < _TRIM_SIZE_HINT:
            return
        lines = LEDGER_PATH.read_text().splitlines()
        if len(lines) <= MAX_LINES:
            return
        keep = lines[-MAX_LINES:]
        tmp = LEDGER_PATH.with_suffix(LEDGER_PATH.suffix + ".tmp")
        tmp.write_text("\n".join(keep) + "\n")
        os.replace(tmp, LEDGER_PATH)
    except Exception:
        pass


def tail(n: int = 20) -> list[dict]:
    """The last `n` events, oldest-first (file order). Bad lines are skipped."""
    try:
        lines = LEDGER_PATH.read_text().splitlines()
    except Exception:
        return []
    out: list[dict] = []
    for ln in lines[-max(0, n):]:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def count() -> int:
    """Total events currently in the ledger."""
    try:
        return sum(1 for _ in LEDGER_PATH.read_text().splitlines() if _.strip())
    except Exception:
        return 0
