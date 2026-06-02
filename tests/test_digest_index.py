"""Tests for the session-digest manifest indexes.

Two gaps in the SessionStart digest these cover:
- Files were invisible at session start (no manifest) → format_files_index.
- Journal entries older than the last-10-days dump vanished → an older-entries
  breadcrumb index via format_journal_older_index.

Both mirror format_memories_index: compact, body-less, look-up-on-demand lines.
Visibility matches filter_memories (restricted agents default-deny).
"""

from __future__ import annotations

import importlib
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def digest_store(tmp_path, monkeypatch):
    """Reload store + files_store pointed at a clean tmp dir. Returns (store, files_store)."""
    monkeypatch.setenv("CCDK_DATA_DIR", str(tmp_path))
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    for mod in ("store", "files_store", "server"):
        if mod in sys.modules:
            del sys.modules[mod]
    store = importlib.import_module("store")
    files_store = importlib.import_module("files_store")
    return store, files_store


def _backdate_journal(store, entry_id: int, days_ago: float) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    entries = store.load_journal()
    for e in entries:
        if e["id"] == entry_id:
            e["ts"] = ts
    store._journal.save(entries)


# ─────────────────────── format_files_index ───────────────────────

def test_files_index_one_line_per_file(digest_store):
    store, fs = digest_store
    fs.add_file("research/thesis.md", content="body", tags=["thesis"], about=["topic"])
    fs.add_file("notes.txt", content="x", tags=["scratch"])
    out = store.format_files_index()
    assert "thesis.md" in out
    assert "notes.txt" in out
    assert "thesis" in out and "scratch" in out
    assert "topic" in out
    assert "body" not in out


def test_files_index_empty_store_is_empty_string(digest_store):
    store, _ = digest_store
    assert store.format_files_index() == ""


def test_files_index_restricted_agent_sees_only_whitelisted(digest_store):
    store, fs = digest_store
    fs.add_file("public.md", content="a")
    fs.add_file("private.md", content="b", bot=["agent-two"])

    # A trusted agent sees the squad-wide file; an agent-two-scoped file is
    # hidden from a different trusted agent (whitelist excludes them).
    trusted = store.format_files_index(bot="agent-one")
    assert "public.md" in trusted
    assert "private.md" not in trusted

    store.RESTRICTED_BOTS.add("restricted-agent")
    try:
        restricted = store.format_files_index(bot="restricted-agent")
        assert "public.md" not in restricted and "private.md" not in restricted
        files = fs.load_files()
        pub = next(f for f in files if f["name"] == "public.md")
        pub["bot"] = ["restricted-agent"]
        fs._files.save(files)
        restricted2 = store.format_files_index(bot="restricted-agent")
        assert "public.md" in restricted2 and "private.md" not in restricted2
    finally:
        store.RESTRICTED_BOTS.discard("restricted-agent")


# ─────────────────── format_journal_older_index ───────────────────

def test_older_index_only_includes_the_window(digest_store):
    store, _ = digest_store
    recent = store.add_journal("recent moment")
    mid = store.add_journal("a few weeks back")
    ancient = store.add_journal("ancient history")
    _backdate_journal(store, mid["id"], 30)
    _backdate_journal(store, ancient["id"], 200)

    out = store.format_journal_older_index(after_days=10, before_days=90)
    assert "a few weeks back" in out
    assert "recent moment" not in out
    assert "ancient history" not in out


def test_older_index_is_compact_not_full_body(digest_store):
    store, _ = digest_store
    long_text = "L" * 400
    e = store.add_journal(long_text)
    _backdate_journal(store, e["id"], 30)
    out = store.format_journal_older_index(after_days=10, before_days=90)
    assert long_text not in out
    assert f"#{e['id']}" in out


def test_older_index_empty_window_is_empty_string(digest_store):
    store, _ = digest_store
    store.add_journal("only a recent one")
    assert store.format_journal_older_index(after_days=10, before_days=90) == ""
