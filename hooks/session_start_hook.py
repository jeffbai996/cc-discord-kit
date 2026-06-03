"""SessionStart hook — inject shared memories once per session.

Split dump strategy:
- feedback type: full body (behavioral rules need to be pre-loaded)
- all other types: index only (titles + tags; look up body on demand)
- journal: last 10 days

Paid once per session boot, then cached for the remainder of the session.
UserPromptSubmit hook handles per-turn refresh with compact index + 1d journal.
"""

from __future__ import annotations

import os
import sys
import traceback

_MODULE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _MODULE_DIR)
import store  # noqa: E402

LOG_PATH = os.path.join(store.DATA_DIR, "boot_hook.log")


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


def main() -> int:
    try:
        bot = _detect_bot()
        mem_full = store.format_memories_for_prompt(bot=bot, types=["feedback"])
        budget = int(os.environ.get("CCDK_PRELOAD_BUDGET", "0") or "0")
        mem_preload, loaded_ids = store.format_memories_full_preload(
            bot=bot, budget_tokens=budget, exclude_types=("feedback",),
        )
        mem_idx = store.format_memories_index(
            bot=bot, exclude_types=["feedback"], exclude_ids=loaded_ids,
        )
        if mem_full:
            print(mem_full)
            print()
        if mem_preload:
            print(mem_preload)
            print()
        if mem_idx:
            print(mem_idx)
            print()
        jou = store.format_journal_for_prompt(days=10)
        if jou:
            print(jou)
            print()
        # journal older tail: breadcrumbs past the 10-day full dump.
        jou_old = store.format_journal_older_index(after_days=10, before_days=90)
        if jou_old:
            print(jou_old)
            print()
        # files manifest: the pull signal so agents know what's in the file store.
        files_idx = store.format_files_index(bot=bot)
        if files_idx:
            print(files_idx)
        log(
            f"session_start bot={bot}: injected {len(mem_full)} full + "
            f"{len(mem_preload)} preload ({len(loaded_ids)} bodies) + "
            f"{len(mem_idx)} idx + {len(jou)} jou + {len(jou_old)} jou_old + "
            f"{len(files_idx)} files chars"
        )
    except Exception:
        log(f"session_start crashed:\n{traceback.format_exc()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
