#!/usr/bin/env python3
"""cc-discord-kit CLI. Manages memories.json + journal.json from the shell.

Usage:
  cc-discord-kit memory list [--type TYPE] [--about LABEL]... [--bot NAME] [--all]
  cc-discord-kit memory show <id>
  cc-discord-kit memory add <text> [--type TYPE] [--name NAME] [--tags a,b,c]
                                [--about a,b] [--bot a,b]
  cc-discord-kit memory edit <id> <text>
  cc-discord-kit memory delete <id>
  cc-discord-kit memory search <term> [--about LABEL]... [--bot NAME] [--all]

  cc-discord-kit journal list [--days N]
  cc-discord-kit journal show <id>
  cc-discord-kit journal add <text> [--source SRC] [--actor A] [--tags a,b,c]
                                    [--title T]
  cc-discord-kit journal edit <id> [<text>] [--actor A] [--source SRC]
                                    [--tags a,b,c] [--title T]
  cc-discord-kit journal delete <id>
  cc-discord-kit journal search <term>

  cc-discord-kit files list
  cc-discord-kit files show <id> [--body-only]
  cc-discord-kit files add [name] [--from-file PATH] [--tags a,b] [--about a,b]
                                  [--bot a,b] [--actor A]
  cc-discord-kit files edit <id> [--name N] [--tags a,b] [--about a,b]
                                  [--from-file PATH]
  cc-discord-kit files delete <id>
  cc-discord-kit files search <term>

  cc-discord-kit persona list
  cc-discord-kit persona show <bot> <slot>
  cc-discord-kit persona edit <bot> <slot>           # opens $EDITOR
  cc-discord-kit persona write <bot> <slot> <text>   # write directly

When CCDK_URL is set in env, this CLI shells out to client.py and
talks to the cc-discord-kit HTTP server at that URL instead of touching local
files. Output format is identical either way.

Run with no args for help.
"""

from __future__ import annotations

import argparse
import os
import sys
import unicodedata


def _maybe_proxy() -> None:
    """If CCDK_URL is set, hand off to client.py and exit."""
    url = os.environ.get("CCDK_URL", "").strip()
    if not url:
        return
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    try:
        import client  # type: ignore
    except ImportError:
        # client.py missing — fall through to local mode.
        return
    sys.exit(client.main(sys.argv[1:], base_url=url))


_maybe_proxy()

# Local mode: import store after the proxy check.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
import history  # noqa: E402
import personas  # noqa: E402
import files_store  # noqa: E402
import facts  # noqa: E402
import capabilities  # noqa: E402


# ─────────────────────────── display helpers ───────────────────────────


def _char_width(ch: str) -> int:
    # East Asian Wide and Fullwidth chars render in 2 terminal cells.
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def _disp_width(text: str) -> int:
    return sum(_char_width(c) for c in text)


def _pad_disp(text: str, width: int) -> str:
    """Truncate text to fit `width` display cells, then pad to exactly width."""
    text = text.replace("\n", " ").strip()
    if _disp_width(text) <= width:
        return text + " " * (width - _disp_width(text))
    # Truncate with ellipsis. Reserve 3 cells for "...".
    out = []
    used = 0
    for ch in text:
        cw = _char_width(ch)
        if used + cw > width - 3:
            break
        out.append(ch)
        used += cw
    return "".join(out) + "..." + " " * (width - used - 3)


def _print_memory_list(entries: list[dict]) -> None:
    if not entries:
        print("(no memories)")
        return
    print(f"{_pad_disp('ID', 5)}{_pad_disp('TYPE', 11)}"
          f"{_pad_disp('NAME', 38)}{_pad_disp('ABOUT', 12)}"
          f"{_pad_disp('DATE', 12)}")
    print("-" * 78)
    for m in entries:
        ts = m.get("ts", "")[:10]
        about = ",".join(m.get("about", []) or [])
        bot = m.get("bot")
        name = m.get("name", "")
        if bot:
            name = f"{name} [bot:{','.join(bot)}]"
        if m.get("deleted"):
            name = f"[DEL] {name}"
        print(f"{_pad_disp('#' + str(m['id']), 5)}"
              f"{_pad_disp(m.get('type', ''), 11)}"
              f"{_pad_disp(name, 38)}"
              f"{_pad_disp(about, 12)}"
              f"{_pad_disp(ts, 12)}")


def _print_journal_list(entries: list[dict]) -> None:
    if not entries:
        print("(no journal entries)")
        return
    print(f"{_pad_disp('ID', 5)}{_pad_disp('DATE', 12)}"
          f"{_pad_disp('ACTOR', 12)}{_pad_disp('TEXT', 50)}")
    print("-" * 79)
    for e in entries:
        ts = e.get("ts", "")[:10]
        actor = e.get("actor", "") or "-"
        text = e.get("text", "")
        if e.get("deleted"):
            text = f"[DEL] {text}"
        print(f"{_pad_disp('#' + str(e['id']), 5)}"
              f"{_pad_disp(ts, 12)}"
              f"{_pad_disp(actor, 12)}"
              f"{_pad_disp(text, 50)}")


def _print_memory_full(m: dict) -> None:
    print(f"=== Memory #{m['id']} ===")
    print(f"Type:  {m.get('type', '')}")
    print(f"Name:  {m.get('name', '')}")
    print(f"Tags:  {', '.join(m.get('tags', []))}")
    if m.get("about"):
        print(f"About: {', '.join(m.get('about', []))}")
    if m.get("bot"):
        print(f"Bot:   {', '.join(m.get('bot', []))}")
    print(f"Saved: {m.get('ts', '')}")
    print()
    print(m.get("text", ""))


def _print_journal_full(e: dict) -> None:
    print(f"=== Journal #{e['id']} ===")
    print(f"Source: {e.get('source', '')}")
    print(f"Actor:  {e.get('actor', '')}")
    print(f"Tags:   {', '.join(e.get('tags', []))}")
    print(f"Saved:  {e.get('ts', '')}")
    print()
    print(e.get("text", ""))


# ─────────────────────────── command handlers ───────────────────────────


def _parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _store_supports(func, *names: str) -> bool:
    """True iff `func` accepts every keyword in `names`.

    The optional journal --title flag is wired through to
    store.add_journal / store.edit_journal only when that store build
    actually declares those parameters. Older store modules that predate
    the fields keep working unchanged — we simply drop the extra kwargs
    instead of raising TypeError on an unknown keyword.
    """
    try:
        import inspect
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True
    return all(n in params for n in names)


def _detect_calling_bot() -> str | None:
    """Best-effort agent name. Override with CCDK_BOT in env.

    Fallback: derive from CLAUDE_CONFIG_DIR last path segment if set
    (e.g. ~/.claude-alt → "claude-alt"). Works for any naming scheme.
    """
    explicit = os.environ.get("CCDK_BOT", "").strip()
    if explicit:
        return explicit
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if cfg:
        return os.path.basename(cfg.rstrip("/")) or None
    return None


def _resolve_discord_origin_from_transcript() -> tuple[str, str] | None:
    """Auto-detect Discord chat/message IDs by reading the active Claude
    Code transcript.

    Used as a fallback when explicit --discord-chat-id / --discord-message-id
    flags weren't passed but we're running inside a Claude Code session that
    originated from a Discord-routed user turn.

    Looks at the most-recently-modified .jsonl under
    ~/.claude/projects/<cwd-encoded>/, tails it from the end, and pulls
    the latest <channel source="plugin:discord:discord" ...> tag. The
    "latest real user prompt" is the message the user actually meant when
    they asked the assistant to do something.

    Returns None outside a Claude Code session, when no transcript is
    found, or when the latest user content has no Discord channel tag.
    """
    if os.environ.get("CLAUDECODE") != "1":
        return None
    # Scan all project dirs. Each agent may use its own config dir, so
    # transcripts live under $CLAUDE_CONFIG_DIR/projects when that's set,
    # falling back to ~/.claude/projects otherwise. Most-recently-modified
    # .jsonl across those roots = the active session.
    roots: list[str] = []
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if cfg:
        roots.append(os.path.join(cfg, "projects"))
    default_root = os.path.expanduser("~/.claude/projects")
    if default_root not in roots:
        roots.append(default_root)
    candidates: list[tuple[str, float]] = []
    for projects_root in roots:
        if not os.path.isdir(projects_root):
            continue
        try:
            for proj in os.listdir(projects_root):
                sub = os.path.join(projects_root, proj)
                if not os.path.isdir(sub):
                    continue
                for name in os.listdir(sub):
                    if name.endswith(".jsonl"):
                        p = os.path.join(sub, name)
                        try:
                            candidates.append((p, os.path.getmtime(p)))
                        except OSError:
                            continue
        except OSError:
            continue
    if not candidates:
        return None
    # Don't trust transcripts that haven't been touched in a long while —
    # otherwise a stale transcript from a closed session could resurface
    # an old Discord chat_id and post the card to the wrong channel.
    import time as _time
    transcript, mtime = max(candidates, key=lambda x: x[1])
    if _time.time() - mtime > 600:  # 10min staleness window
        return None

    import json
    import re
    tag_re = re.compile(
        r'<channel\s+source=["\'](?:plugin:discord:discord|discord)["\']'
        r'[^>]*?chat_id=["\']([^"\']+)["\']'
        r'[^>]*?message_id=["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    try:
        with open(transcript, "r") as f:
            lines = f.readlines()
    except OSError:
        return None

    # Walk backward to find the latest *real* user prompt (skipping
    # tool_result entries — type:user but tool output, not user words).
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "user":
            continue
        msg = obj.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text_parts: list[str] = []
            is_real_prompt = False
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_result":
                    continue
                if c.get("type") == "text" and isinstance(c.get("text"), str):
                    text_parts.append(c["text"])
                    is_real_prompt = True
            if not is_real_prompt:
                continue
            text = "\n".join(text_parts)
        else:
            continue
        if not text:
            continue
        matches = list(tag_re.finditer(text))
        if matches:
            m = matches[-1]
            return m.group(1), m.group(2)
        # Latest real prompt had no Discord tag — terminal-turn signal.
        # Don't walk back into older Discord history.
        return None
    return None


def _post_card_if_discord(action: dict, args: argparse.Namespace) -> None:
    """Post a confirmation card to Discord if Discord context is available.

    Resolves the target channel in priority order:
      1. Explicit --discord-chat-id / --discord-message-id flags
      2. (auto) Latest <channel> tag in the active Claude Code transcript
         when running inside a Claude Code session (CLAUDECODE=1)
      3. (fallback) Calling agent's `discord_home_channel` from agents.yaml
         when CLAUDECODE=1 and no transcript tag matched — covers self-
         initiated mutations that aren't tied to a specific Discord turn

    With no Discord context resolvable, this is a no-op — the CLI's own
    `Saved #N` print is the terminal-only confirmation. The CLAUDECODE
    gate is what keeps human terminal use silent.
    """
    chat_id = getattr(args, "discord_chat_id", None) or ""
    msg_id = getattr(args, "discord_message_id", None) or None

    if not chat_id:
        auto = _resolve_discord_origin_from_transcript()
        if auto is not None:
            chat_id, msg_id = auto
        elif os.environ.get("CLAUDECODE") == "1":
            # Agent invoking the CLI but the current turn isn't Discord-
            # routed (self-initiated cleanup, scheduled task, etc). Post
            # to that agent's home channel if agents.yaml declares one.
            # Flag it self-initiated so the renderer badges the card 🧪 TEST —
            # makes a smoke-test/cleanup write read as such at a glance instead
            # of masquerading as a real conversation save.
            bot = _detect_calling_bot()
            meta = personas.get_agent_meta(bot)
            chat_id = str(meta.get("discord_home_channel") or "")
            msg_id = None
            if chat_id:
                action = {**action, "_self_initiated": True}

    if not chat_id:
        return

    try:
        import discord_card
        ok, err = discord_card.post_action_card(
            action, chat_id, reply_to=msg_id,
            user_agent="multiagent-cli (1.0)",
        )
        if not ok and err:
            print(f"[card post failed] {err}", file=sys.stderr)
    except Exception as e:
        print(f"[card post crashed] {type(e).__name__}: {e}", file=sys.stderr)


def _find_memory(mid: int) -> dict | None:
    return next((x for x in store.load_memories() if x.get("id") == mid), None)


def _find_journal(jid: int) -> dict | None:
    return next((x for x in store.load_journal() if x.get("id") == jid), None)


def cmd_memory(args: argparse.Namespace) -> int:
    sub = args.sub
    if sub == "list":
        base = store.load_memories_raw() if getattr(args, "include_deleted", False) \
            else store.load_memories()
        entries = store.filter_memories(
            entries=base,
            type=args.type,
            about=args.about or None,
            bot=_detect_calling_bot(),
            show_all=bool(args.all),
        )
        _print_memory_list(entries)
        return 0
    if sub == "show":
        m = next((x for x in store.load_memories() if x["id"] == args.id), None)
        if not m:
            print(f"Memory #{args.id} not found", file=sys.stderr)
            return 1
        if getattr(args, "body_only", False):
            sys.stdout.write(m.get("text", ""))
            if not m.get("text", "").endswith("\n"):
                sys.stdout.write("\n")
        else:
            _print_memory_full(m)
        return 0
    if sub == "add":
        tags = _parse_csv(args.tags)
        about = _parse_csv(args.about)
        bot_list = _parse_csv(args.bot) if args.bot else None
        m = store.save_memory(args.text, type=args.type or "feedback",
                              name=args.name or "", tags=tags,
                              about=about, bot=bot_list,
                              author=_detect_calling_bot() or "unknown")
        print(f"Saved #{m['id']}: {m.get('name', '')}")
        _post_card_if_discord({"kind": "memory_saved", "entry": m}, args)
        return 0
    if sub == "edit":
        before = _find_memory(args.id)
        ok = store.edit_memory(args.id, args.text,
                               editor=_detect_calling_bot() or "unknown")
        if not ok:
            print(f"Memory #{args.id} not found", file=sys.stderr)
            return 1
        print(f"Updated #{args.id}")
        _post_card_if_discord(
            {"kind": "memory_edited", "id": args.id,
             "before": before, "after": _find_memory(args.id)},
            args,
        )
        return 0
    if sub == "delete":
        before = _find_memory(args.id)
        ok = history.remove_memory_with_history(args.id, actor="cli")
        if not ok:
            print(f"Memory #{args.id} not found", file=sys.stderr)
            return 1
        print(f"Deleted #{args.id}")
        _post_card_if_discord({"kind": "memory_deleted", "before": before}, args)
        return 0
    if sub == "search":
        results = store.search_memories(args.term)
        # Apply about/bot filters to search results too.
        results = store.filter_memories(
            results,
            about=args.about or None,
            bot=_detect_calling_bot(),
            show_all=bool(args.all),
        )
        _print_memory_list(results)
        return 0
    return 2


def cmd_recall(args: argparse.Namespace) -> int:
    entries, source = store.recall_memories(
        args.query, bot=_detect_calling_bot(), top_k=args.top_k,
    )
    if not entries:
        print(f"(no recall hits for {args.query!r}; source={source})")
        return 0
    print(f"RECALL — {len(entries)} hit(s) for {args.query!r} (via {source}):")
    print("-" * 78)
    for m in entries:
        head = f"#{m['id']} [{m.get('type', '')}] {m.get('name', '')}"
        tags = m.get("tags", [])
        if tags:
            head += f" ({','.join(tags)})"
        print(head)
        print(f"  {m.get('text', '')}")
        print()
    return 0


def cmd_journal(args: argparse.Namespace) -> int:
    sub = args.sub
    if sub == "list":
        if getattr(args, "include_deleted", False):
            entries = store.load_journal_raw()
            if args.days:
                # Manual day-window filter on raw set; journal_recent() already filters.
                from datetime import datetime, timedelta, timezone
                cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
                entries = [
                    e for e in entries
                    if datetime.fromisoformat(e.get("ts", "1970-01-01T00:00:00+00:00")) >= cutoff
                ]
        else:
            entries = (store.journal_recent(args.days)
                       if args.days else store.load_journal())
        # Moments view excludes todos (use `todo list` for those).
        entries = [e for e in entries if e.get("kind") != "todo"]
        _print_journal_list(entries)
        return 0
    if sub == "show":
        e = next((x for x in store.load_journal() if x["id"] == args.id), None)
        if not e:
            print(f"Journal #{args.id} not found", file=sys.stderr)
            return 1
        if getattr(args, "body_only", False):
            sys.stdout.write(e.get("text", ""))
            if not e.get("text", "").endswith("\n"):
                sys.stdout.write("\n")
        else:
            _print_journal_full(e)
        return 0
    if sub == "add":
        tags = _parse_csv(args.tags)
        kwargs = dict(source=args.source or "cli",
                      actor=args.actor or "", tags=tags)
        # Optional heading — only forwarded if the store build declares it.
        if _store_supports(store.add_journal, "title"):
            kwargs["title"] = getattr(args, "title", "") or ""
        e = store.add_journal(args.text, **kwargs)
        print(f"Pinned #{e['id']}")
        _post_card_if_discord({"kind": "journal_added", "entry": e}, args)
        return 0
    if sub == "edit":
        before = _find_journal(args.id)
        if not before:
            print(f"Journal #{args.id} not found", file=sys.stderr)
            return 1
        kwargs = {}
        if args.text is not None:
            kwargs["text"] = args.text
        if args.actor is not None:
            kwargs["actor"] = args.actor
        if args.source is not None:
            kwargs["source"] = args.source
        if args.tags is not None:
            kwargs["tags"] = _parse_csv(args.tags)
        if getattr(args, "title", None) is not None \
                and _store_supports(store.edit_journal, "title"):
            kwargs["title"] = args.title
        if not kwargs:
            print("nothing to edit (pass text or --actor/--source/--tags/"
                  "--title)", file=sys.stderr)
            return 2
        ok = store.edit_journal(args.id, **kwargs)
        if not ok:
            print(f"Journal #{args.id} edit failed", file=sys.stderr)
            return 1
        after = _find_journal(args.id)
        print(f"Edited #{args.id}")
        _post_card_if_discord(
            {"kind": "journal_edited", "id": args.id,
             "before": before, "after": after},
            args,
        )
        return 0
    if sub == "delete":
        before = _find_journal(args.id)
        ok = history.remove_journal_with_history(args.id, actor="cli")
        if not ok:
            print(f"Journal #{args.id} not found", file=sys.stderr)
            return 1
        print(f"Deleted #{args.id}")
        _post_card_if_discord({"kind": "journal_deleted", "before": before}, args)
        return 0
    if sub == "search":
        results = store.search_journal(args.term)
        _print_journal_list(results)
        return 0
    return 2


def cmd_persona(args: argparse.Namespace) -> int:
    sub = args.sub
    if sub == "list":
        for bot in personas.list_bots():
            print(bot)
            for s in personas.get_files(bot):
                print(f"  {s['slot']:<12} [{s['mode']}]  {s['path']}")
        return 0
    if sub == "show":
        try:
            data = personas.read_slot(args.bot, args.slot)
        except KeyError:
            print(f"unknown bot/slot: {args.bot}/{args.slot}", file=sys.stderr)
            return 1
        sys.stdout.write(data["text"])
        return 0
    if sub == "write":
        try:
            result = personas.write_slot(args.bot, args.slot, args.text)
        except KeyError:
            print(f"unknown bot/slot: {args.bot}/{args.slot}", file=sys.stderr)
            return 1
        msg = f"wrote {result['path']}"
        if result["mode"] == "git":
            if result["committed"]:
                msg += f" (committed {result['sha']})"
            elif result["error"]:
                msg += f" (commit failed: {result['error']})"
        print(msg)
        return 0
    if sub == "edit":
        import subprocess, tempfile
        try:
            data = personas.read_slot(args.bot, args.slot)
        except KeyError:
            print(f"unknown bot/slot: {args.bot}/{args.slot}", file=sys.stderr)
            return 1
        editor = os.environ.get("EDITOR", "vim")
        suffix = "." + args.slot.rsplit(".", 1)[-1] if "." in args.slot else ".md"
        with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8") as tmp:
            tmp.write(data["text"])
            tmp_path = tmp.name
        try:
            subprocess.run([editor, tmp_path], check=True)
            with open(tmp_path, "r", encoding="utf-8") as f:
                new_text = f.read()
        finally:
            os.unlink(tmp_path)
        if new_text == data["text"]:
            print("no changes")
            return 0
        result = personas.write_slot(args.bot, args.slot, new_text)
        msg = f"wrote {result['path']}"
        if result["mode"] == "git":
            if result["committed"]:
                msg += f" (committed {result['sha']})"
            elif result["error"]:
                msg += f" (commit failed: {result['error']})"
        print(msg)
        return 0
    return 2


def _fmt_size(n: int) -> str:
    n = float(int(n or 0))
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.0f}B"


def cmd_todo(args: argparse.Namespace) -> int:
    sub = args.sub
    editor = getattr(args, "actor", "") or ""
    if sub == "add":
        e = store.add_todo(args.text, owner=args.owner, due=args.due,
                           actor=editor, source=args.source or "cli",
                           tags=_parse_csv(args.tags),
                           note=getattr(args, "note", "") or "",
                           priority=getattr(args, "priority", "none"),
                           flag=getattr(args, "flag", False))
        print(f"Added to-do #{e['id']}")
        _post_card_if_discord({"kind": "todo_added", "entry": e}, args)
        return 0
    if sub == "list":
        status = None if args.status == "all" else args.status
        todos = store.list_todos(status=status, owner=args.owner)
        if not todos:
            print("(no to-dos)")
            return 0
        for t in todos:
            mark = {"open": "☐", "done": "☑", "cancelled": "✗"}.get(
                t.get("status", "open"), "☐")
            pri = store._TODO_PRI_MARK.get(t.get("priority", "none"), "")
            flag = "⚑" if t.get("flag") else ""
            badge = (" " + " ".join(b for b in (pri, flag) if b)) if (pri or flag) else ""
            owner = f" @{t['owner']}" if t.get("owner") else ""
            due = f" (due {t['due']})" if t.get("due") else ""
            first = (t.get("text", "").splitlines() or [""])[0][:100]
            note = " 📝" if t.get("note") else ""
            print(f"{mark} #{t['id']}{badge}{owner}{due}  {first}{note}")
        return 0
    if sub == "show":
        t = next((x for x in store.load_todos() if x["id"] == args.id), None)
        if not t:
            print(f"To-do #{args.id} not found", file=sys.stderr)
            return 1
        print(f"=== To-do #{t['id']} ===")
        print(f"Status:   {t.get('status', 'open')}")
        print(f"Priority: {t.get('priority', 'none')}")
        print(f"Flag:     {'⚑ flagged' if t.get('flag') else '—'}")
        if t.get("owner"):
            print(f"Owner:    @{t['owner']}")
        if t.get("due"):
            print(f"Due:      {t['due']}")
        if t.get("tags"):
            print(f"Tags:     {', '.join(t['tags'])}")
        print()
        print(t.get("text", ""))
        if t.get("note"):
            print()
            print(f"Note: {t['note']}")
        return 0
    if sub in ("done", "cancel", "reopen"):
        target = {"done": "done", "cancel": "cancelled", "reopen": "open"}[sub]
        e = next((x for x in store.load_todos() if x["id"] == args.id), None)
        if not e:
            print(f"To-do #{args.id} not found", file=sys.stderr)
            return 1
        if not store.set_todo_status(args.id, target, editor=editor):
            print(f"To-do #{args.id} update failed", file=sys.stderr)
            return 1
        print(f"To-do #{args.id} → {target}")
        _post_card_if_discord(
            {"kind": "todo_status", "id": args.id, "status": target,
             "text": e.get("text", "")}, args)
        return 0
    if sub == "note":
        ok = store.set_todo_note(args.id, args.text, editor=editor)
    elif sub == "priority":
        ok = store.set_todo_priority(args.id, args.level, editor=editor)
        if not ok:
            print(f"Invalid priority {args.level!r} (choose: "
                  f"{', '.join(store.TODO_PRIORITIES)}) or unknown to-do",
                  file=sys.stderr)
            return 1
    elif sub == "flag":
        ok = store.set_todo_flag(args.id, True, editor=editor)
    elif sub == "unflag":
        ok = store.set_todo_flag(args.id, False, editor=editor)
    elif sub == "due":
        ok = store.set_todo_due(args.id, "" if args.date == "clear" else args.date,
                                editor=editor)
    elif sub == "edit":
        ok = store.set_todo_text(args.id, args.text, editor=editor)
    else:
        return 2
    if not ok:
        print(f"To-do #{args.id} not found", file=sys.stderr)
        return 1
    print(f"To-do #{args.id} updated ({sub})")
    return 0


def cmd_files(args: argparse.Namespace) -> int:
    sub = args.sub
    if sub == "list":
        entries = files_store.load_files()
        if not entries:
            print("(no files)")
            return 0
        for f in entries:
            tags = ",".join(f.get("tags") or [])
            print(f"#{f['id']:<4} {f.get('type','?'):<6} {_fmt_size(f.get('size',0)):>8}  "
                  f"{f.get('name','')}{('  ['+tags+']') if tags else ''}")
        return 0
    if sub == "show":
        f = files_store.get_file(args.id)
        if not f:
            print(f"File #{args.id} not found", file=sys.stderr)
            return 1
        if getattr(args, "body_only", False):
            data = files_store.read_file_content(args.id) or b""
            sys.stdout.buffer.write(data)
            return 0
        print(f"#{f['id']} {f.get('name','')}")
        print(f"  type={f.get('type')}  mime={f.get('mime')}  "
              f"size={_fmt_size(f.get('size',0))}  storage={f.get('storage')}")
        if f.get("tags"):
            print(f"  tags: {', '.join(f['tags'])}")
        if f.get("about"):
            print(f"  about: {', '.join(f['about'])}")
        print(f"  sha256: {f.get('sha256','')[:16]}…")
        if f.get("storage") == "inline":
            print("  ---")
            print(f.get("content", ""))
        return 0
    if sub == "add":
        # Read content from --from-file (path), stdin, or empty.
        content = None
        blob_bytes = None
        name = args.name
        if getattr(args, "from_file", None):
            path = os.path.expanduser(args.from_file)
            if not name:
                name = os.path.basename(path)
            if files_store._is_text_ext(path):
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            else:
                with open(path, "rb") as fh:
                    blob_bytes = fh.read()
        elif not sys.stdin.isatty():
            content = sys.stdin.read()
        try:
            rec = files_store.add_file(
                name or "untitled", content=content, blob_bytes=blob_bytes,
                tags=_parse_csv(args.tags), about=_parse_csv(args.about),
                bot=_parse_csv(args.bot) if args.bot else None,
                actor=args.actor or "cli",
            )
        except (files_store.FileTooLarge, files_store.StoreFull) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"Added #{rec['id']}: {rec['name']} ({_fmt_size(rec['size'])}, {rec['storage']})")
        _post_card_if_discord({"kind": "file_added", "entry": rec}, args)
        return 0
    if sub == "edit":
        kwargs = {}
        if args.name is not None:
            kwargs["name"] = args.name
        if args.tags is not None:
            kwargs["tags"] = _parse_csv(args.tags)
        if args.about is not None:
            kwargs["about"] = _parse_csv(args.about)
        if getattr(args, "from_file", None):
            with open(os.path.expanduser(args.from_file), "r",
                      encoding="utf-8", errors="replace") as fh:
                kwargs["content"] = fh.read()
        if not kwargs:
            print("nothing to edit (pass --name/--tags/--about/--from-file)",
                  file=sys.stderr)
            return 2
        try:
            ok = files_store.edit_file(args.id, **kwargs)
        except files_store.FileTooLarge as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if not ok:
            print(f"File #{args.id} not found", file=sys.stderr)
            return 1
        print(f"Edited #{args.id}")
        return 0
    if sub == "delete":
        ok = files_store.remove_file(args.id)
        if not ok:
            print(f"File #{args.id} not found", file=sys.stderr)
            return 1
        print(f"Deleted #{args.id}")
        return 0
    if sub == "search":
        for f in files_store.search_files(args.term):
            print(f"#{f['id']:<4} {f.get('name','')}  ({_fmt_size(f.get('size',0))})")
        return 0
    print(f"unknown files subcommand: {sub}", file=sys.stderr)
    return 2


def cmd_fact(args) -> int:
    """Reusable-string fact store: look up volatile literals instead of
    regenerating (and fabricating) them. Mirrors cmd_files in shape."""
    sub = args.sub
    if sub == "list":
        rows = facts.list_facts()
        if not rows:
            print("(no facts)")
            return 0
        for r in rows:
            note = f"  # {r['note']}" if r.get("note") else ""
            print(f"{_pad_disp(r['key'], 24)} {r.get('value', '')}{note}")
        return 0
    if sub == "get":
        r = facts.get_fact(args.key)
        if not r:
            print(f"fact {args.key!r} not found", file=sys.stderr)
            return 1
        # Bare value to stdout so it's pipe-friendly; metadata to stderr.
        print(r.get("value", ""))
        meta = []
        if r.get("note"):
            meta.append(f"note: {r['note']}")
        if r.get("updated_by"):
            meta.append(f"by: {r['updated_by']}")
        if r.get("updated_ts"):
            meta.append(f"updated: {r['updated_ts']}")
        if meta:
            print("  (" + "; ".join(meta) + ")", file=sys.stderr)
        return 0
    if sub == "set":
        try:
            rec = facts.set_fact(
                args.key, args.value,
                note=args.note or "",
                by=_detect_calling_bot() or "",
            )
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"Set {rec['key']} = {rec['value']}")
        return 0
    if sub == "delete":
        if facts.delete_fact(args.key):
            print(f"Deleted {args.key}")
            return 0
        print(f"fact {args.key!r} not found", file=sys.stderr)
        return 1
    if sub == "search":
        rows = facts.search_facts(args.term)
        if not rows:
            print("(no matches)")
            return 0
        for r in rows:
            note = f"  # {r['note']}" if r.get("note") else ""
            print(f"{_pad_disp(r['key'], 24)} {r.get('value', '')}{note}")
        return 0
    print(f"unknown fact subcommand: {sub}", file=sys.stderr)
    return 2


def _print_capability_report(rec: dict) -> None:
    print(f"agent:    {rec.get('bot')}")
    print(f"machine:  {rec.get('machine')}")
    print(f"config:   {rec.get('config_dir')}")
    if rec.get("reported_ts"):
        print(f"reported: {rec['reported_ts']}")
    feats = rec.get("detected_features") or []
    print(f"features: {', '.join(feats) if feats else '(none detected)'}")
    hooks = rec.get("hooks") or {}
    if hooks:
        print("hooks:")
        for event in sorted(hooks):
            print(f"  {event}: {', '.join(hooks[event])}")
    else:
        print("hooks:    (none)")


def cmd_capabilities(args: argparse.Namespace) -> int:
    sub = args.sub
    if sub == "report":
        rec = capabilities.record_self()
        print(f"Recorded capabilities for '{rec['bot']}':")
        _print_capability_report(rec)
        return 0
    if sub == "show":
        matrix = capabilities.load_matrix()
        if not matrix:
            print("(no capabilities recorded yet — run `capabilities report` "
                  "on each agent)")
            return 0
        target = getattr(args, "bot", None)
        if target:
            rec = matrix.get(target)
            if not rec:
                print(f"agent '{target}' has never reported", file=sys.stderr)
                return 1
            _print_capability_report(rec)
            return 0
        for name in sorted(matrix):
            rec = matrix[name]
            feats = ", ".join(rec.get("detected_features") or []) or "(none)"
            print(f"{name:14} {rec.get('machine','?'):20} {feats}")
        return 0
    if sub == "drift":
        report = capabilities.drift_report()
        if not report:
            print("(no intended capabilities declared)")
            return 0
        for r in report:
            mark = {"ok": "ok   ", "drift": "DRIFT", "unknown": "?    "}.get(
                r["status"], r["status"])
            print(f"[{mark}] {r['bot']}")
            if r["status"] == "unknown":
                print(f"          never reported — intended: "
                      f"{', '.join(r['intended'])}")
                continue
            if r["missing"]:
                print(f"          missing: {', '.join(r['missing'])}")
            if r["extra"]:
                print(f"          extra:   {', '.join(r['extra'])}")
            if not r["missing"] and not r["extra"]:
                print(f"          all intended features present "
                      f"({', '.join(r['intended'])})")
        return 0
    print(f"unknown capabilities subcommand: {sub}", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cc-discord-kit",
                                description="Shared memory + journal for multi-agent setups")
    top = p.add_subparsers(dest="cmd", required=True)

    # memory
    mem = top.add_parser("memory", help="manage durable memories")
    msub = mem.add_subparsers(dest="sub", required=True)

    m_list = msub.add_parser("list")
    m_list.add_argument("--type", choices=sorted(store.VALID_TYPES))
    m_list.add_argument("--about", action="append", default=[],
                        help="filter by subject label (repeatable, OR semantics)")
    m_list.add_argument("--all", action="store_true",
                        help="include bot-scoped entries from other bots")
    m_list.add_argument("--include-deleted", action="store_true",
                        help="include tombstoned entries (debugging)")

    m_show = msub.add_parser("show")
    m_show.add_argument("id", type=int)
    m_show.add_argument("--body-only", action="store_true",
                        help="print only the body text, no rendered metadata")

    m_add = msub.add_parser("add")
    m_add.add_argument("text")
    m_add.add_argument("--type", choices=sorted(store.VALID_TYPES))
    m_add.add_argument("--name", default="")
    m_add.add_argument("--tags", default="", help="comma-separated")
    m_add.add_argument("--about", default="", help="comma-separated subject labels")
    m_add.add_argument("--bot", default="",
                       help="comma-separated bot names; default unset = shared across all agents")
    _add_discord_flags(m_add)

    m_edit = msub.add_parser("edit")
    m_edit.add_argument("id", type=int)
    m_edit.add_argument("text")
    _add_discord_flags(m_edit)

    m_del = msub.add_parser("delete")
    m_del.add_argument("id", type=int)
    _add_discord_flags(m_del)

    m_search = msub.add_parser("search")
    m_search.add_argument("term")
    m_search.add_argument("--about", action="append", default=[])
    m_search.add_argument("--all", action="store_true")

    # journal
    jou = top.add_parser("journal", help="manage pinned moments")
    jsub = jou.add_subparsers(dest="sub", required=True)

    j_list = jsub.add_parser("list")
    j_list.add_argument("--days", type=int, default=0,
                        help="filter to last N days (0 = all)")
    j_list.add_argument("--include-deleted", action="store_true",
                        help="include tombstoned entries (debugging)")

    j_show = jsub.add_parser("show")
    j_show.add_argument("id", type=int)
    j_show.add_argument("--body-only", action="store_true",
                        help="print only the body text, no rendered metadata")

    j_add = jsub.add_parser("add")
    j_add.add_argument("text")
    j_add.add_argument("--source", default="cli")
    j_add.add_argument("--actor", default="")
    j_add.add_argument("--tags", default="")
    j_add.add_argument("--title", default="", help="optional short heading")
    _add_discord_flags(j_add)

    j_edit = jsub.add_parser("edit")
    j_edit.add_argument("id", type=int)
    j_edit.add_argument("text", nargs="?", default=None,
                        help="new body text (omit to keep current text)")
    j_edit.add_argument("--actor", default=None,
                        help="overwrite the entry's actor field")
    j_edit.add_argument("--source", default=None,
                        help="overwrite the entry's source field")
    j_edit.add_argument("--tags", default=None,
                        help="comma-separated tags (overwrites)")
    j_edit.add_argument("--title", default=None,
                        help="overwrite the entry's title")
    _add_discord_flags(j_edit)

    j_del = jsub.add_parser("delete")
    j_del.add_argument("id", type=int)
    _add_discord_flags(j_del)

    j_search = jsub.add_parser("search")
    j_search.add_argument("term")

    # files — shared "project files" any agent can read
    tdo = top.add_parser("todo", help="manage auto-injected to-dos (own primitive)")
    tsub = tdo.add_subparsers(dest="sub", required=True)
    t_add = tsub.add_parser("add")
    t_add.add_argument("text")
    t_add.add_argument("--owner", default="", help="who owns it — an agent or person")
    t_add.add_argument("--due", default="", help="due date, free-form (e.g. 2026-07-01)")
    t_add.add_argument("--source", default="cli")
    t_add.add_argument("--actor", default="")
    t_add.add_argument("--tags", default="")
    t_add.add_argument("--note", default="",
                       help=f"short clarifying note (capped {store.TODO_NOTE_MAX} chars)")
    t_add.add_argument("--priority", default="none", choices=list(store.TODO_PRIORITIES),
                       help="priority (sorts high→none)")
    t_add.add_argument("--flag", action="store_true", help="flag (star) the to-do")
    _add_discord_flags(t_add)
    t_list = tsub.add_parser("list", help="list to-dos (open by default)")
    t_list.add_argument("--status", default="open",
                        choices=["open", "done", "cancelled", "all"])
    t_list.add_argument("--owner", default=None,
                        help="filter to an owner (incl. unassigned)")
    t_show = tsub.add_parser("show", help="full detail of one to-do (incl. note)")
    t_show.add_argument("id", type=int)
    for _tn, _th in (("done", "mark a to-do done"),
                     ("cancel", "cancel a to-do"),
                     ("reopen", "reopen a to-do")):
        _tp = tsub.add_parser(_tn, help=_th)
        _tp.add_argument("id", type=int)
        _add_discord_flags(_tp)
    t_note = tsub.add_parser("note", help="set/replace a to-do's clarifying note")
    t_note.add_argument("id", type=int)
    t_note.add_argument("text", help=f"note text (capped {store.TODO_NOTE_MAX} chars)")
    t_pri = tsub.add_parser("priority", help="set a to-do's priority")
    t_pri.add_argument("id", type=int)
    t_pri.add_argument("level", choices=list(store.TODO_PRIORITIES))
    t_flag = tsub.add_parser("flag", help="flag (star) a to-do")
    t_flag.add_argument("id", type=int)
    t_unflag = tsub.add_parser("unflag", help="remove a to-do's flag")
    t_unflag.add_argument("id", type=int)
    t_due = tsub.add_parser("due", help="set or clear a to-do's due date")
    t_due.add_argument("id", type=int)
    t_due.add_argument("date", help="due date, or 'clear' to remove it")
    t_edit = tsub.add_parser("edit", help="edit a to-do's text")
    t_edit.add_argument("id", type=int)
    t_edit.add_argument("text")

    fil = top.add_parser("files", help="manage shared files (documents)")
    fsub = fil.add_subparsers(dest="sub", required=True)

    fsub.add_parser("list")

    f_show = fsub.add_parser("show")
    f_show.add_argument("id", type=int)
    f_show.add_argument("--body-only", action="store_true",
                        help="print raw file content only (bytes to stdout)")

    f_add = fsub.add_parser("add")
    f_add.add_argument("name", nargs="?", default="",
                       help="file name (inferred from --from-file if omitted)")
    f_add.add_argument("--from-file", default=None,
                       help="read content from this local path")
    f_add.add_argument("--tags", default="")
    f_add.add_argument("--about", default="")
    f_add.add_argument("--bot", default="",
                       help="comma-separated bot whitelist (scopes visibility)")
    f_add.add_argument("--actor", default="")
    _add_discord_flags(f_add)

    f_edit = fsub.add_parser("edit")
    f_edit.add_argument("id", type=int)
    f_edit.add_argument("--name", default=None)
    f_edit.add_argument("--tags", default=None)
    f_edit.add_argument("--about", default=None)
    f_edit.add_argument("--from-file", default=None,
                        help="replace inline content from this local path")

    f_del = fsub.add_parser("delete")
    f_del.add_argument("id", type=int)

    f_search = fsub.add_parser("search")
    f_search.add_argument("term")

    # persona
    per = top.add_parser("persona", help="manage per-bot persona files")
    psub = per.add_subparsers(dest="sub", required=True)

    psub.add_parser("list")

    p_show = psub.add_parser("show")
    p_show.add_argument("bot")
    p_show.add_argument("slot")

    p_edit = psub.add_parser("edit")
    p_edit.add_argument("bot")
    p_edit.add_argument("slot")

    p_write = psub.add_parser("write")
    p_write.add_argument("bot")
    p_write.add_argument("slot")
    p_write.add_argument("text")

    # recall
    rec = top.add_parser("recall", help="semantic recall of memories")
    rec.add_argument("query")
    rec.add_argument("--top-k", type=int, default=8)

    # fact — reusable-string store for volatile literals
    fct = top.add_parser(
        "fact",
        help="reusable-string facts: look up volatile literals "
             "(commit hashes, message_ids, ports, IDs, paths) not from memory")
    ctsub = fct.add_subparsers(dest="sub", required=True)

    ctsub.add_parser("list")

    ct_get = ctsub.add_parser("get")
    ct_get.add_argument("key")

    ct_set = ctsub.add_parser("set")
    ct_set.add_argument("key", help="slug: [a-z0-9][a-z0-9._-]*")
    ct_set.add_argument("value")
    ct_set.add_argument("--note", default="", help="why/what this literal is")

    ct_del = ctsub.add_parser("delete")
    ct_del.add_argument("key")

    ct_search = ctsub.add_parser("search")
    ct_search.add_argument("term")

    # capabilities — per-agent hook/feature capability matrix (anti-drift)
    caps = top.add_parser("capabilities", aliases=["caps"],
                          help="agent hook/feature capability matrix (anti-drift)")
    csub = caps.add_subparsers(dest="sub", required=True)
    csub.add_parser("report",
                    help="record THIS agent's actual installed hooks/features")
    c_show = csub.add_parser("show", help="print the matrix (or one agent)")
    c_show.add_argument("bot", nargs="?", help="limit to one agent")
    csub.add_parser("drift",
                    help="diff intended vs actual features per agent")

    # bots — fleet management (doctor validates agents.yaml against reality)
    botsp = top.add_parser("bots",
                           help="agent fleet management (registry: agents.yaml)")
    bsub = botsp.add_subparsers(dest="sub", required=True)
    bsub.add_parser("doctor",
                    help="validate every agent in agents.yaml against reality")
    bsub.add_parser("list",
                    help="list all agents in agents.yaml with kind + host")

    return p


def _add_discord_flags(parser: argparse.ArgumentParser) -> None:
    """Add --discord-chat-id / --discord-message-id to a subcommand parser.

    When --discord-chat-id is set, after a successful op the CLI posts a
    rendered card to that channel (replying to --discord-message-id when
    provided). Without these flags the CLI is silent on Discord and just
    prints `Saved #N` to stdout — backwards-compatible with terminal use.

    Bots passing these from a Discord-originated request resolve them from
    the inbound `<channel>` tag's `chat_id` and `message_id` attributes.
    """
    parser.add_argument("--discord-chat-id", default="",
                        help="post a confirmation card to this Discord channel after the op")
    parser.add_argument("--discord-message-id", default="",
                        help="reply-to message ID for the card (requires --discord-chat-id)")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "memory":
        return cmd_memory(args)
    if args.cmd == "journal":
        return cmd_journal(args)
    if args.cmd == "todo":
        return cmd_todo(args)
    if args.cmd == "files":
        return cmd_files(args)
    if args.cmd == "persona":
        return cmd_persona(args)
    if args.cmd == "recall":
        return cmd_recall(args)
    if args.cmd == "fact":
        return cmd_fact(args)
    if args.cmd in ("capabilities", "caps"):
        return cmd_capabilities(args)
    if args.cmd == "bots":
        import bots_doctor, bot_config
        if args.sub == "doctor":
            print(bots_doctor.format_report(bots_doctor.run()))
            return 0
        if args.sub == "list":
            for name, b in bot_config.load_bots().items():
                kind = b.get("kind", "claude")
                print(f"  {name:14} kind={kind:8} host={b.get('host','?')}")
            return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
