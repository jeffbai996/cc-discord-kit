"""UserPromptSubmit hook — refresh compact memory index + last-day journal.

Cheap context refresh on every turn. Total injection ~1k tokens:
  - Memories index (name/type/tags only, ~700 tokens for 60 entries)
  - Journal entries from last 1 day only (~300 tokens typical)

The full memory dump and 30-day journal are loaded once via SessionStart
hook (cached for the rest of the session). This per-turn hook keeps the
index fresh in case memories were added/edited mid-session by another bot
or via the CLI, and surfaces only the most recent journal entries
to avoid burning ~4k tokens every single turn on history that's already in
the SessionStart cache.
"""

from __future__ import annotations

import json
import os
import sys
import traceback

_MODULE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _MODULE_DIR)
import store  # noqa: E402
import semantic  # noqa: E402

LOG_PATH = os.path.join(store.DATA_DIR, "user_prompt_hook.log")


def _detect_bot() -> str | None:
    """Override with CCDK_BOT, else derive from CLAUDE_CONFIG_DIR."""
    explicit = os.environ.get("CCDK_BOT", "").strip()
    if explicit:
        return explicit
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if cfg:
        return os.path.basename(cfg.rstrip("/")) or None
    return None


def log(msg: str) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(f"{msg}\n")
    except OSError:
        pass


def _read_prompt() -> str:
    """UserPromptSubmit passes {"prompt": ...} as JSON on stdin."""
    try:
        raw = sys.stdin.read()
        return (json.loads(raw).get("prompt", "") if raw.strip() else "") or ""
    except Exception:
        return ""


def main() -> int:
    prompt = _read_prompt()
    try:
        # Semantic recall first — the targeted, query-relevant content goes
        # at the top where it gets attention; the index is the catalog below.
        recall = semantic.format_recall(prompt)
        if recall:
            print(recall)
            print()
        idx = store.format_memories_index(bot=_detect_bot())
        if idx:
            print(idx)
            print()
        todos = store.format_todos_for_prompt()
        if todos:
            print(todos)
            print()
        jou = store.format_journal_for_prompt(days=1)
        if jou:
            print(jou)
    except Exception:
        log(f"user_prompt crashed:\n{traceback.format_exc()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
