"""One-shot migration: parse Markdown memory files into memories.json + journal.json.

Walks a directory of frontmatter-prefixed `.md` files and imports each into the
cc-discord-kit store: memory-style files become durable memories, journal-style
files become pinned journal moments.

The already-run guard refuses to write over a non-empty store, so it can sit
dormant safely and be re-run on a fresh machine without clobbering data.

Frontmatter format expected:
  ---
  name: ...
  description: ...
  type: feedback|project|user|reference
  ---
  body...

Source directories are supplied on the command line (there are no defaults):
  --memory-dir   directory of memory `.md` files  -> memories.json
  --context-dir  directory of journal `.md` files -> journal.json

Storage location follows the CCDK_DATA_DIR env var (default
`~/.local/share/cc-discord-kit/`), inherited from store.py.

Usage:
  python3 migrate.py --memory-dir ~/notes/memories --dry-run   # preview
  python3 migrate.py --memory-dir ~/notes/memories             # write memories
  python3 migrate.py --context-dir ~/notes/journal             # write journal
  python3 migrate.py --memory-dir ... --context-dir ...        # both
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Returns (frontmatter dict, body)."""
    m = FRONTMATTER_RE.match(content)
    if not m:
        return {}, content
    fm_text, body = m.group(1), m.group(2)
    fm: dict = {}
    for line in fm_text.split("\n"):
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        fm[key.strip()] = val.strip().strip('"')
    return fm, body.strip()


def derive_tags(filename: str, fm: dict, body: str) -> list[str]:
    """Best-effort tags from filename + content."""
    tags = []
    stem = filename.replace(".md", "")
    for prefix in ("feedback_", "project_", "user_", "reference_"):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break
    parts = [p for p in stem.split("_") if p and len(p) > 2]
    tags.extend(parts[:4])
    return tags


def import_memory_dir(dir_path: str, *, dry_run: bool) -> int:
    if not os.path.isdir(dir_path):
        print(f"[skip] {dir_path} does not exist")
        return 0
    files = sorted(f for f in os.listdir(dir_path)
                   if f.endswith(".md") and f != "MEMORY.md")
    count = 0
    for fname in files:
        path = os.path.join(dir_path, fname)
        with open(path, "r") as f:
            content = f.read()
        fm, body = parse_frontmatter(content)
        if not body:
            print(f"[warn] {fname}: no body, skipping")
            continue
        type_ = fm.get("type", "feedback")
        if type_ not in store.VALID_TYPES:
            type_ = "feedback"
        name = fm.get("name", fname.replace(".md", ""))
        tags = derive_tags(fname, fm, body)
        text_parts = []
        if fm.get("description"):
            text_parts.append(f"_{fm['description']}_")
        text_parts.append(body)
        text = "\n\n".join(text_parts)

        if dry_run:
            print(f"[would import] type={type_} name={name!r} tags={tags} "
                  f"len={len(text)}")
        else:
            entry = store.save_memory(text, type=type_, name=name, tags=tags)
            print(f"[saved #{entry['id']}] {type_}: {name}")
        count += 1
    return count


def import_journal_dir(dir_path: str, *, dry_run: bool) -> int:
    if not os.path.isdir(dir_path):
        print(f"[skip] {dir_path} does not exist")
        return 0
    files = sorted(f for f in os.listdir(dir_path) if f.endswith(".md"))
    count = 0
    for fname in files:
        path = os.path.join(dir_path, fname)
        with open(path, "r") as f:
            content = f.read()
        fm, body = parse_frontmatter(content)
        if not body:
            body = content.strip()
        text = body
        if fm.get("name"):
            text = f"**{fm['name']}**\n\n{text}"
        tags = derive_tags(fname, fm, body)

        if dry_run:
            print(f"[would import journal] tags={tags} len={len(text)} from {fname}")
        else:
            entry = store.add_journal(text, source=f"migrate:{fname}",
                                      actor="migrate", tags=tags)
            print(f"[saved journal #{entry['id']}] {fname}")
        count += 1
    return count


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import Markdown memory/journal files into the "
                    "cc-discord-kit store.")
    parser.add_argument(
        "--memory-dir",
        help="Directory of memory .md files to import into memories.json.")
    parser.add_argument(
        "--context-dir",
        help="Directory of journal .md files to import into journal.json.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be imported without writing.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    dry_run = args.dry_run

    if not args.memory_dir and not args.context_dir:
        print("ERROR: supply at least one of --memory-dir / --context-dir.")
        sys.exit(2)

    if not dry_run:
        if args.memory_dir and store.load_memories():
            print("ERROR: memories.json is not empty. "
                  "Refusing to migrate over existing data.")
            sys.exit(1)
        if args.context_dir and store.load_journal():
            print("ERROR: journal.json is not empty. "
                  "Refusing to migrate over existing data.")
            sys.exit(1)

    n_mem = 0
    n_jou = 0
    mode = "DRY RUN" if dry_run else "LIVE"

    if args.memory_dir:
        print(f"=== Migrating from {args.memory_dir} -> memories.json ({mode}) ===")
        n_mem = import_memory_dir(args.memory_dir, dry_run=dry_run)

    if args.context_dir:
        print(f"\n=== Migrating from {args.context_dir} -> journal.json ({mode}) ===")
        n_jou = import_journal_dir(args.context_dir, dry_run=dry_run)

    print(f"\n=== Done ===")
    print(f"Memories: {n_mem}")
    print(f"Journal:  {n_jou}")
    if not dry_run:
        print(f"\nFiles written:")
        if args.memory_dir:
            print(f"  {store.MEMORIES_FILE}")
        if args.context_dir:
            print(f"  {store.JOURNAL_FILE}")


if __name__ == "__main__":
    main()
