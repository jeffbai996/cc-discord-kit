"""Memory attribution: author (immutable creator) + last_editor (mutable).

author      — set once in save_memory, NEVER overwritten by an edit.
last_editor — stamped on every edit_memory that passes editor=... non-empty.
A memory only ever touched by its creator -> author == last_editor.
Render functions surface the author so the boot dump shows who wrote each fact.

Generic agent names only (public repo: no personal data in tests).
"""

from __future__ import annotations


def test_save_memory_stores_author(fresh_store):
    store, _ = fresh_store
    mem = store.save_memory("body", name="m", author="agent-1")
    assert mem["author"] == "agent-1"


def test_save_memory_author_defaults_empty(fresh_store):
    store, _ = fresh_store
    mem = store.save_memory("body", name="m")
    assert mem.get("author", "") == ""


def test_edit_memory_sets_last_editor_without_changing_author(fresh_store):
    store, _ = fresh_store
    mem = store.save_memory("v1", name="m", author="agent-1")
    assert store.edit_memory(mem["id"], text="v2", editor="agent-2") is True
    after = next(m for m in store.load_memories() if m["id"] == mem["id"])
    assert after["author"] == "agent-1"     # immutable
    assert after["last_editor"] == "agent-2"  # mutable


def test_edit_memory_no_editor_leaves_last_editor_absent(fresh_store):
    store, _ = fresh_store
    mem = store.save_memory("v1", name="m", author="agent-1")
    assert store.edit_memory(mem["id"], text="v2") is True
    after = next(m for m in store.load_memories() if m["id"] == mem["id"])
    assert "last_editor" not in after


def test_render_full_dump_includes_author(fresh_store):
    store, _ = fresh_store
    store.save_memory("body text", name="m", type="user", author="agent-1")
    out = store.format_memories_for_prompt()
    assert "agent-1" in out


def test_render_index_includes_author(fresh_store):
    store, _ = fresh_store
    store.save_memory("body text", name="m", type="user", author="agent-1")
    out = store.format_memories_index()
    assert "agent-1" in out


def test_render_preload_shows_editor_when_differs(fresh_store):
    store, _ = fresh_store
    mem = store.save_memory("body text", name="m", type="user", author="agent-1")
    store.edit_memory(mem["id"], text="body text edited", editor="agent-2")
    out, _ids = store.format_memories_full_preload(exclude_types=())
    assert "agent-1" in out
    assert "agent-2" in out
