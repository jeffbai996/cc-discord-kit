"""Todo lifecycle — journal entries with kind=='todo'. Generic data."""
from __future__ import annotations


def test_add_todo_is_open_and_listed(fresh_store):
    store, _ = fresh_store
    t = store.add_todo("water the plants", owner="dan", due="2026-07-01")
    assert t["kind"] == "todo" and t["status"] == "open"
    assert t["owner"] == "dan" and t["due"] == "2026-07-01"
    assert [x["id"] for x in store.list_todos()] == [t["id"]]


def test_todos_excluded_from_moments_view(fresh_store):
    store, _ = fresh_store
    store.add_todo("a todo")
    store.add_journal("a moment")
    block = store.format_journal_for_prompt(days=30)
    assert "a moment" in block
    assert "a todo" not in block


def test_done_moves_out_of_open(fresh_store):
    store, _ = fresh_store
    t = store.add_todo("ship it")
    assert store.set_todo_status(t["id"], "done", editor="fraggy") is True
    assert store.list_todos(status="open") == []
    done = store.list_todos(status="done")
    assert [x["id"] for x in done] == [t["id"]]
    assert done[0].get("closed_ts")           # stamped on close


def test_reopen_clears_closed(fresh_store):
    store, _ = fresh_store
    t = store.add_todo("revisit")
    store.set_todo_status(t["id"], "done")
    store.set_todo_status(t["id"], "open")
    reopened = store.list_todos(status="open")
    assert [x["id"] for x in reopened] == [t["id"]]
    assert not reopened[0].get("closed_ts")    # cleared on reopen


def test_invalid_status_rejected(fresh_store):
    store, _ = fresh_store
    t = store.add_todo("x")
    assert store.set_todo_status(t["id"], "bogus") is False


def test_owner_filter_includes_unassigned(fresh_store):
    store, _ = fresh_store
    mine = store.add_todo("mine", owner="fraggy")
    shared = store.add_todo("shared")            # unassigned
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
    assert f"#{b['id']}" not in block            # done -> not surfaced


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
