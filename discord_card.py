"""Shared Discord card rendering + posting for cc-discord-kit actions.

Both the Stop hook (when a bot emits a [MEMORY:]/[JOURNAL:] tag) and the
CLI (`cc-discord-kit memory add|edit|delete` with --discord-* flags) post
the same rendered card to Discord. Sharing the format here keeps them
byte-for-byte consistent — drift between hook and CLI was the original
bug that motivated this module.

Format conventions:
  - Bold header in prose (emoji + verb + ID).
  - Single fenced code block below the header containing aligned meta
    key:value pairs and the body, separated by a horizontal rule. The
    code-block surface renders consistently on Discord mobile; markdown
    tables are unreliable on the same surface.

Failure modes for the poster: no token resolved → silent skip; HTTP
error → log + skip. The action already landed in the store; missing
visible confirmation is the worst case, never a corrupted write.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import urllib.error
import urllib.request

CARD_BODY_LIMIT = 600

# Width of the horizontal rule between meta and body. Cards no longer
# hard-wrap their content to a mobile width (that trimmed long values and
# chopped diffs); they emit full-length lines and let Discord wrap. The rule
# is the one fixed-width element — kept short so it doesn't itself force a
# horizontal scroll on a phone.
RULE_WIDTH = 32


def _sanitize_fence(text: str) -> str:
    """Replace triple-backticks in body text so they don't close the outer
    fenced code block. A bare ``` inside a fenced block ends the block early,
    leaving everything after it rendered as plain text. Swap to a visually
    similar U+2032 prime so the body still reads as code-ish but doesn't
    break the fence."""
    return text.replace("```", "′′′")


def _truncate_body(text: str, lim: int = CARD_BODY_LIMIT) -> str:
    text = _sanitize_fence(text)
    if len(text) <= lim:
        return text
    cut = text[: lim - 1]
    # Drop a trailing partial word so we don't slice mid-token. Skip only if
    # that would discard a large tail — a pathological space-less run.
    sp = cut.rsplit(" ", 1)
    if len(sp) == 2 and len(sp[0]) >= len(cut) * 0.6:
        cut = sp[0]
    return cut.rstrip() + "…"


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _diff_body(before_text: str, after_text: str,
               lim: int = CARD_BODY_LIMIT) -> str | None:
    """Produce a compact +/- diff focused on what changed, with line numbers.

    Strategy: unified_diff with 1 line of context (so it "zooms" on the
    changed window), then render each line as `<marker> <lineno> <content>`.
    The marker (+/-/space) stays at column 0 so Discord's ```diff colorizer
    fires (green additions, red removals), with one cell of whitespace after
    it so the marker reads cleanly separated from the gutter even at
    double-digit line numbers. The line number is the source line — `after`
    line for additions/context, `before` line for removals — so the reader
    can see WHICH lines changed.

    Returns None if nothing materially changed (only tags/about edited, body
    identical) so the caller can fall back to a plain body preview.
    """
    before_text = _sanitize_fence(before_text or "")
    after_text = _sanitize_fence(after_text or "")
    if before_text == after_text:
        return None
    # A line-diff is meaningless for single-paragraph bodies: the whole entry
    # is one "line", so unified_diff renders it as one giant +/- pair that
    # truncates into an unreadable wall. Fall back to a clean body preview
    # when neither side is multi-line.
    if before_text.count("\n") <= 1 and after_text.count("\n") <= 1:
        return None
    diff_lines = list(difflib.unified_diff(
        before_text.splitlines(),
        after_text.splitlines(),
        n=1,
        lineterm="",
    ))

    out: list[str] = []
    old_ln = new_ln = 0
    # Gutter width: enough digits for the larger file (min 2 for tidy alignment).
    width = max(2, len(str(max(
        before_text.count("\n") + 1, after_text.count("\n") + 1))))
    for ln in diff_lines:
        if ln.startswith("---") or ln.startswith("+++"):
            continue  # file headers — noise
        m = _HUNK_RE.match(ln)
        if m:
            # Parse the hunk header for line numbers but DON'T render it — the
            # `@@ -a,b +c,d @@` machinery isn't human-parseable. A jump in the
            # gutter line numbers between sections signals the gap on its own.
            old_ln, new_ln = int(m.group(1)), int(m.group(2))
            continue
        if ln.startswith("+"):
            out.append(f"+ {str(new_ln).rjust(width)} {ln[1:]}")
            new_ln += 1
        elif ln.startswith("-"):
            out.append(f"- {str(old_ln).rjust(width)} {ln[1:]}")
            old_ln += 1
        else:  # context line (leading space)
            content = ln[1:] if ln.startswith(" ") else ln
            out.append(f"  {str(new_ln).rjust(width)} {content}")
            old_ln += 1
            new_ln += 1

    if not out:
        return None
    rendered = "\n".join(out)
    if len(rendered) <= lim:
        return rendered
    return rendered[: lim - 1].rstrip() + "…"


def _meta_pad(meta: list[tuple[str, str]]) -> int:
    """Column width to align meta values: longest key + `: `."""
    return max((len(k) for k, _ in meta), default=0) + 2


def _render_card_block(meta: list[tuple[str, str]], body: str | None) -> str:
    """Render meta-pairs + optional body inside a fenced code block.

    Lines are emitted at their natural length — no hard mobile-width wrap.
    Discord wraps long lines on its own; trimming them to 32 chars chopped
    long names and identifiers, which read worse than a soft wrap. The one
    fixed-width element is the rule between meta and body (RULE_WIDTH).
    """
    if not meta and not body:
        return ""
    pad = _meta_pad(meta)
    lines: list[str] = []
    for key, val in meta:
        lines.append((key + ":").ljust(pad) + val)
    if body:
        if lines:
            lines.append("─" * RULE_WIDTH)
        lines.append(body)
    return "```\n" + "\n".join(lines) + "\n```"


def _render_diff_block(meta: list[tuple[str, str]], diff_body: str | None,
                       fallback_body: str | None) -> str:
    """Single-block edit-card rendering.

    Everything — meta key:value lines, a rule, and the diff — lives in ONE
    ```diff fenced block. Discord colorizes only lines that begin with +/-
    (the diff lines); the meta lines start with letters (`type:` / `tags:` /
    `about:`), so they render as plain default text. The line numbers in the
    diff gutter come after the marker, so they don't trigger coloring either.

    Why one block, not two: stacking a plain ``` block above a ```diff block
    is broken on Discord — when the closing fence sits directly above the next
    opening fence Discord drops the second block entirely (the diff renders as
    nothing), and even with a blank line between them it leaves an ugly gap.
    A single fence sidesteps both: no dropped diff, no gap.

    Falls back to a plain block (no diff highlight) when nothing materially
    changed in the prose — e.g. tag-only edits where _diff_body returned None.
    """
    if diff_body:
        lines = [(k + ":").ljust(_meta_pad(meta)) + v for k, v in meta]
        if lines:
            lines.append("─" * RULE_WIDTH)
        lines.append(diff_body)
        return "```diff\n" + "\n".join(lines) + "\n```"
    return _render_card_block(meta, fallback_body)


def format_card(action: dict) -> str | None:
    """Render one action as a Discord-friendly card.

    `action` shape:
      {kind: 'memory_saved', entry: {...}}
      {kind: 'memory_edited', id: int, before: dict|None, after: dict|None}
      {kind: 'memory_deleted', before: dict|None}
      {kind: 'journal_added', entry: {...}}
      {kind: 'journal_edited', id: int, before: dict|None, after: dict|None}
      {kind: 'journal_deleted', before: dict|None}

    Returns None if the action has nothing renderable (e.g. delete of a
    missing id where `before` is None).
    """
    kind = action.get("kind")
    if kind == "memory_saved":
        e = action.get("entry") or {}
        if not e.get("id"):
            return None
        title = e.get("name") or ""
        # Name moves into the bold prose header — easier to spot on phone
        # without expanding the code block. Keep type/tags/about inside.
        meta = [
            ("type", e.get("type", "?")),
            ("tags", ", ".join(e.get("tags") or []) or "—"),
            ("about", ", ".join(e.get("about") or []) or "—"),
        ]
        body = _truncate_body(e.get("text", "")) or None
        head = f"💾 **Memory #{e['id']} saved**"
        if title:
            head += f" — {title}"
        return head + "\n" + _render_card_block(meta, body)
    if kind == "memory_edited":
        before = action.get("before") or {}
        after = action.get("after") or {}
        mid = action.get("id")
        title = after.get("name") or before.get("name") or ""
        meta = [
            ("type", after.get("type", before.get("type", "?"))),
            ("tags", ", ".join(after.get("tags") or before.get("tags") or []) or "—"),
            ("about", ", ".join(after.get("about") or before.get("about") or []) or "—"),
        ]
        diff = _diff_body(before.get("text", ""), after.get("text", ""))
        fallback = _truncate_body(after.get("text", "")) or None
        head = f"✏️ **Memory #{mid} edited**"
        if title:
            head += f" — {title}"
        return head + "\n" + _render_diff_block(meta, diff, fallback)
    if kind == "memory_deleted":
        before = action.get("before") or {}
        if not before:
            return None
        # Header stays plain (no name suffix) — when something's gone, the
        # bold line should announce the loss, not eulogize it. Name + a short
        # excerpt of the body live inside the block so the user can still
        # recognize what was removed.
        meta: list[tuple[str, str]] = [("type", before.get("type", "?"))]
        title = before.get("name") or ""
        if title:
            meta.append(("name", title))
        body = _truncate_body(before.get("text", "")) or None
        head = f"🗑️ **Memory #{before.get('id', '?')} deleted**"
        return head + "\n" + _render_card_block(meta, body)
    if kind == "journal_added":
        e = action.get("entry") or {}
        if not e.get("id"):
            return None
        meta = [
            ("tags", ", ".join(e.get("tags") or []) or "—"),
            ("actor", e.get("actor", "") or "—"),
        ]
        body = _truncate_body(e.get("text", "")) or None
        return f"📓 **Journal #{e['id']} added**\n" + _render_card_block(meta, body)
    if kind == "journal_edited":
        before = action.get("before") or {}
        after = action.get("after") or {}
        jid = action.get("id")
        meta = [
            ("tags", ", ".join(after.get("tags") or before.get("tags") or []) or "—"),
            ("actor", after.get("actor", before.get("actor", "")) or "—"),
        ]
        diff = _diff_body(before.get("text", ""), after.get("text", ""))
        fallback = _truncate_body(after.get("text") or before.get("text", "")) or None
        return f"✏️ **Journal #{jid} edited**\n" + _render_diff_block(meta, diff, fallback)
    if kind == "journal_deleted":
        before = action.get("before") or {}
        if not before:
            return None
        return f"🗑️ **Journal #{before.get('id','?')} deleted**"
    if kind == "todo_added":
        # Todos are id-less by design — the card shows the checklist item, not
        # a #number. owner/due ride as meta, mirroring the memory-card shape.
        e = action.get("entry") or {}
        text = e.get("text", "")
        if not text:
            return None
        meta: list[tuple[str, str]] = []
        if e.get("owner"):
            meta.append(("owner", f"@{e['owner']}"))
        if e.get("due"):
            meta.append(("due", e["due"]))
        return "🆕 **To-do added**\n" + _render_card_block(meta, _truncate_body(text))
    if kind == "todo_status":
        status = action.get("status", "")
        text = action.get("text", "")
        head = {
            "done": "✅ **To-do done**",
            "cancelled": "🚫 **To-do cancelled**",
            "open": "↩️ **To-do reopened**",
        }.get(status, "**To-do updated**")
        if not text:
            return head
        return head + "\n" + _render_card_block([], _truncate_body(text))
    return None


def read_bot_token() -> str | None:
    """Read DISCORD_BOT_TOKEN.

    Resolution order:
      1. $CCDK_DISCORD_TOKEN — explicit token override
      2. $DISCORD_STATE_DIR/.env — multi-agent setups where each bot has
         its own state dir but shares CLAUDE_CONFIG_DIR. Priority over
         CLAUDE_CONFIG_DIR so per-bot overrides actually apply.
      3. $CLAUDE_PLUGIN_STATE_DIR/.env
      4. $CLAUDE_CONFIG_DIR/channels/discord/.env
      5. ~/.claude/channels/discord/.env (default agent)
    """
    explicit = os.environ.get("CCDK_DISCORD_TOKEN", "").strip()
    if explicit:
        return explicit

    env_path: str | None = None
    state_dir = os.environ.get("DISCORD_STATE_DIR", "")
    if state_dir:
        env_path = os.path.join(state_dir, ".env")
    else:
        plugin_dir = os.environ.get("CLAUDE_PLUGIN_STATE_DIR", "")
        if plugin_dir:
            env_path = os.path.join(plugin_dir, ".env")
        elif os.environ.get("CLAUDE_CONFIG_DIR"):
            env_path = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "channels", "discord", ".env")
        else:
            env_path = os.path.expanduser("~/.claude/channels/discord/.env")
    if not env_path or not os.path.exists(env_path):
        return None
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("DISCORD_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def post_message(token: str, channel_id: str, content: str,
                 reply_to: str | None = None,
                 user_agent: str = "cc-discord-kit-card (1.0)") -> tuple[bool, str]:
    """POST /channels/<id>/messages. Returns (ok, error_message_if_failed).

    Best-effort, single-shot, no retry — if the network's flaky, the user
    sees no card; the action already landed in the store.
    """
    body: dict = {
        "content": content,
        "allowed_mentions": {"parse": []},
    }
    if reply_to:
        body["message_reference"] = {
            "message_id": reply_to,
            "fail_if_not_exists": False,
        }
    data = json.dumps(body).encode("utf-8")
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": user_agent,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            return (200 <= resp.status < 300, "")
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read()[:200].decode("utf-8", "replace")
        except Exception:
            err_body = ""
        return (False, f"HTTP {e.code}: {err_body!r}")
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}")


def post_action_card(action: dict, chat_id: str,
                     reply_to: str | None = None,
                     user_agent: str = "cc-discord-kit-card (1.0)") -> tuple[bool, str]:
    """Render and post a card for one action. Returns (ok, error).

    `ok=False, error=""` means the action wasn't renderable (e.g. delete
    of a missing id) — not actually a failure.
    """
    card = format_card(action)
    if not card:
        return (False, "")
    token = read_bot_token()
    if not token:
        return (False, "no DISCORD_BOT_TOKEN found")
    return post_message(token, chat_id, card, reply_to=reply_to, user_agent=user_agent)
