"""Todos — memories of type 'todo', auto-injected, hidden from the durable index."""
from __future__ import annotations


def test_add_todo_is_a_type_todo_memory(fresh_store):
    store, _ = fresh_store
    t = store.add_todo("water the plants", owner="dan", due="2026-07-01")
    assert t["type"] == "todo" and t["status"] == "open"
    assert t["owner"] == "dan" and t["due"] == "2026-07-01"
    # It IS a memory — shows up in the memory store (and thus the web page).
    assert any(m["id"] == t["id"] for m in store.load_memories())
    assert [x["id"] for x in store.list_todos()] == [t["id"]]


def test_todos_hidden_from_durable_index_but_facts_shown(fresh_store):
    store, _ = fresh_store
    store.add_todo("a todo")
    store.save_memory("a durable fact", type="project", name="fact")
    idx = store.format_memories_index()
    assert "fact" in idx
    assert "a todo" not in idx          # type:todo not rendered in the index


def test_todo_type_is_valid(fresh_store):
    store, _ = fresh_store
    assert "todo" in store.VALID_TYPES
    # save_memory(type='todo') is accepted, not coerced to feedback
    m = store.save_memory("x", type="todo")
    assert m["type"] == "todo"


def test_done_moves_out_of_open_but_stays_a_memory(fresh_store):
    store, _ = fresh_store
    t = store.add_todo("ship it")
    assert store.set_todo_status(t["id"], "done", editor="fraggy") is True
    assert store.list_todos(status="open") == []
    assert [x["id"] for x in store.list_todos(status="done")] == [t["id"]]
    # Completed todo persists as a memory (history), not deleted.
    done = next(m for m in store.load_memories() if m["id"] == t["id"])
    assert done["status"] == "done" and done.get("closed_ts")


def test_reopen_clears_closed(fresh_store):
    store, _ = fresh_store
    t = store.add_todo("revisit")
    store.set_todo_status(t["id"], "done")
    store.set_todo_status(t["id"], "open")
    reopened = store.list_todos(status="open")
    assert [x["id"] for x in reopened] == [t["id"]]
    assert not reopened[0].get("closed_ts")


def test_set_status_rejects_non_todo_memory(fresh_store):
    store, _ = fresh_store
    m = store.save_memory("a fact", type="project")
    # status flips only apply to todos, never to a real memory
    assert store.set_todo_status(m["id"], "done") is False


def test_invalid_status_rejected(fresh_store):
    store, _ = fresh_store
    t = store.add_todo("x")
    assert store.set_todo_status(t["id"], "bogus") is False


def test_owner_filter_includes_unassigned(fresh_store):
    store, _ = fresh_store
    mine = store.add_todo("mine", owner="fraggy")
    shared = store.add_todo("shared")
    other = store.add_todo("theirs", owner="bricky")
    ids = {x["id"] for x in store.list_todos(owner="fraggy")}
    assert mine["id"] in ids and shared["id"] in ids
    assert other["id"] not in ids


def test_prompt_block_renders_open_only(fresh_store):
    store, _ = fresh_store
    a = store.add_todo("alpha", owner="jeff", due="2026-06-12")
    b = store.add_todo("beta")
    store.set_todo_status(b["id"], "done")
    block = store.format_todos_for_prompt()
    assert "OPEN TODOS" in block
    assert f"#{a['id']}" in block and "@jeff" in block and "due 2026-06-12" in block
    assert f"#{b['id']}" not in block


def test_prompt_block_empty_when_no_open(fresh_store):
    store, _ = fresh_store
    t = store.add_todo("only one")
    store.set_todo_status(t["id"], "cancelled")
    assert store.format_todos_for_prompt() == ""


def test_prompt_block_caps_and_counts_overflow(fresh_store):
    store, _ = fresh_store
    for i in range(13):
        store.add_todo(f"task {i}")
    block = store.format_todos_for_prompt(max_items=10)
    bullets = [l for l in block.splitlines() if l.strip().startswith("☐")]
    assert len(bullets) == 10
    assert "+3 more open" in block
