"""History purge / kwargs-extraction tests — gaps not covered by test_history.py.

Covers purge_history_entry, purge_all_deletes, the _record_to_edit_kwargs_*
extractors, and restore_deleted edge cases. Uses the fresh_store fixture.
"""

from __future__ import annotations


# ─────────────────────────── _record_to_edit_kwargs_* ───────────────────────────


def test_record_to_edit_kwargs_memory_full(fresh_store):
    _, history = fresh_store
    rec = {"id": 1, "text": "body", "name": "n", "type": "user",
           "tags": ["a"], "about": ["AAPL"], "bot": ["agent-1"]}
    kwargs = history._record_to_edit_kwargs_memory(rec)
    assert kwargs["text"] == "body"
    assert kwargs["name"] == "n"
    assert kwargs["type"] == "user"
    assert kwargs["tags"] == ["a"]
    assert kwargs["about"] == ["AAPL"]
    assert kwargs["bot"] == ["agent-1"]


def test_record_to_edit_kwargs_memory_no_bot_key(fresh_store):
    _, history = fresh_store
    rec = {"id": 1, "text": "body", "name": "n", "type": "feedback"}
    kwargs = history._record_to_edit_kwargs_memory(rec)
    # bot only included if present in the record.
    assert "bot" not in kwargs
    assert kwargs["tags"] == []
    assert kwargs["about"] == []


def test_record_to_edit_kwargs_journal(fresh_store):
    _, history = fresh_store
    rec = {"id": 1, "text": "moment", "actor": "agent-1",
           "source": "cli", "tags": ["t"]}
    kwargs = history._record_to_edit_kwargs_journal(rec)
    assert kwargs == {"text": "moment", "actor": "agent-1",
                      "source": "cli", "tags": ["t"]}


# ─────────────────────────── purge_history_entry ───────────────────────────


def test_purge_history_entry_removes_matching_line(fresh_store):
    store, history = fresh_store
    mem = store.save_memory("v1")
    history.edit_memory_with_history(mem["id"], text="v2")
    hist = history.load_history("memory", mem["id"])
    assert len(hist) == 1

    assert history.purge_history_entry(hist[0]) is True
    # Gone for good.
    assert history.load_history("memory", mem["id"]) == []


def test_purge_history_entry_no_match_returns_false(fresh_store):
    store, history = fresh_store
    mem = store.save_memory("v1")
    history.edit_memory_with_history(mem["id"], text="v2")
    fake = {"kind": "memory", "id": mem["id"], "ts": "2099-01-01T00:00:00+00:00"}
    assert history.purge_history_entry(fake) is False


def test_purge_history_entry_invalid_input_returns_false(fresh_store):
    _, history = fresh_store
    assert history.purge_history_entry({"kind": "bogus", "id": 1, "ts": "x"}) is False
    assert history.purge_history_entry({"kind": "memory", "id": "notint", "ts": "x"}) is False
    assert history.purge_history_entry({"kind": "memory", "id": 1}) is False  # no ts


def test_purge_history_entry_no_file_returns_false(fresh_store):
    _, history = fresh_store
    # No edits.jsonl exists yet.
    fake = {"kind": "memory", "id": 1, "ts": "2026-01-01T00:00:00+00:00"}
    assert history.purge_history_entry(fake) is False


def test_purge_keeps_other_entries(fresh_store):
    store, history = fresh_store
    m1 = store.save_memory("a")
    m2 = store.save_memory("b")
    history.edit_memory_with_history(m1["id"], text="a2")
    history.edit_memory_with_history(m2["id"], text="b2")

    h1 = history.load_history("memory", m1["id"])[0]
    assert history.purge_history_entry(h1) is True
    # m1's edit gone, m2's edit untouched.
    assert history.load_history("memory", m1["id"]) == []
    assert len(history.load_history("memory", m2["id"])) == 1


# ─────────────────────────── purge_all_deletes ───────────────────────────


def test_purge_all_deletes_removes_only_delete_lines(fresh_store):
    store, history = fresh_store
    m1 = store.save_memory("edit-me")
    m2 = store.save_memory("delete-me")
    history.edit_memory_with_history(m1["id"], text="edited")  # edit line
    history.remove_memory_with_history(m2["id"])               # delete line

    removed = history.purge_all_deletes()
    assert removed == 1
    # Delete history gone...
    assert history.load_recent_deletes() == []
    # ...but the edit history survives.
    assert len(history.load_history("memory", m1["id"])) == 1


def test_purge_all_deletes_empty_returns_zero(fresh_store):
    store, history = fresh_store
    mem = store.save_memory("x")
    history.edit_memory_with_history(mem["id"], text="y")  # only an edit, no delete
    assert history.purge_all_deletes() == 0


def test_purge_all_deletes_no_file_returns_zero(fresh_store):
    _, history = fresh_store
    assert history.purge_all_deletes() == 0


# ─────────────────────────── restore_deleted edge cases ───────────────────────────


def test_restore_deleted_non_delete_entry_returns_none(fresh_store):
    store, history = fresh_store
    mem = store.save_memory("v1")
    history.edit_memory_with_history(mem["id"], text="v2")
    edit_entry = history.load_history("memory", mem["id"])[0]
    # Passing an edit (not a delete) entry → None.
    assert history.restore_deleted(edit_entry) is None


def test_restore_deleted_journal(fresh_store):
    store, history = fresh_store
    j = store.add_journal("pin me", actor="agent-1", source="cli")
    jid = j["id"]
    history.remove_journal_with_history(jid)
    assert all(e["id"] != jid for e in store.load_journal())

    deletes = history.load_recent_deletes()
    restored = history.restore_deleted(deletes[0])
    assert restored is not None
    assert restored["text"] == "pin me"
    assert restored["actor"] == "agent-1"
