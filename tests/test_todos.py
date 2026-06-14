"""Todos — journal entries with kind='todo'. Share journal.json with moments;
the /journal page toggles between the two views."""
from __future__ import annotations


def test_add_todo_is_kind_todo_in_journal(fresh_store):
    store, _ = fresh_store
    t = store.add_todo("water the plants", owner="dan", due="2026-07-01")
    assert t["kind"] == "todo" and t["status"] == "open"
    assert t["owner"] == "dan" and t["due"] == "2026-07-01"
    assert any(e["id"] == t["id"] for e in store.load_journal())
    assert [x["id"] for x in store.list_todos()] == [t["id"]]


def test_todos_excluded_from_moments_view(fresh_store):
    store, _ = fresh_store
    store.add_todo("a todo")
    store.add_journal("a moment")
    block = store.format_journal_for_prompt(days=30)
    assert "a moment" in block
    assert "a todo" not in block          # toggle: moments timeline hides todos


def test_done_moves_out_of_open_but_stays_in_journal(fresh_store):
    store, _ = fresh_store
    t = store.add_todo("ship it")
    assert store.set_todo_status(t["id"], "done", editor="web") is True
    assert store.list_todos(status="open") == []
    assert [x["id"] for x in store.list_todos(status="done")] == [t["id"]]
    done = next(e for e in store.load_journal() if e["id"] == t["id"])
    assert done["status"] == "done" and done.get("closed_ts")


def test_reopen_clears_closed(fresh_store):
    store, _ = fresh_store
    t = store.add_todo("revisit")
    store.set_todo_status(t["id"], "done")
    store.set_todo_status(t["id"], "open")
    reopened = store.list_todos(status="open")
    assert [x["id"] for x in reopened] == [t["id"]]
    assert not reopened[0].get("closed_ts")


def test_set_status_rejects_non_todo_entry(fresh_store):
    store, _ = fresh_store
    m = store.add_journal("just a moment")
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


def test_prompt_block_is_idless_checklist_open_only(fresh_store):
    store, _ = fresh_store
    a = store.add_todo("alpha", owner="jeff", due="2026-06-12")
    b = store.add_todo("beta")
    store.set_todo_status(b["id"], "done")
    block = store.format_todos_for_prompt()
    assert "OPEN TODOS" in block
    assert "alpha" in block and "@jeff" in block and "due 2026-06-12" in block
    assert "beta" not in block
    # "no IDs": the checklist must not surface a #<id> token
    assert f"#{a['id']}" not in block and "☐" in block


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
    assert "+3 more" in block


# ── todos own a SEPARATE id space from journal moments ──────────────────────

def test_todos_do_not_advance_journal_counter(fresh_store):
    """Adding todos must NOT inflate the journal moment id."""
    store, _ = fresh_store
    m1 = store.add_journal("first moment")
    store.add_todo("t1"); store.add_todo("t2"); store.add_todo("t3")
    m2 = store.add_journal("second moment")
    assert m2["id"] == m1["id"] + 1


def test_todo_ids_are_their_own_sequence(fresh_store):
    store, _ = fresh_store
    store.add_journal("moment a"); store.add_journal("moment b")
    t1 = store.add_todo("todo one")
    t2 = store.add_todo("todo two")
    assert t1["id"] == 1 and t2["id"] == 2


def test_todo_id_overlaps_moment_id_without_collision_on_status(fresh_store):
    store, _ = fresh_store
    m = store.add_journal("moment with id 1")
    t = store.add_todo("todo with id 1")
    assert m["id"] == t["id"] == 1
    assert store.set_todo_status(t["id"], "done")
    moment = next(e for e in store.load_journal()
                  if e["id"] == 1 and e.get("kind") != "todo")
    assert moment.get("status") in (None, "")
    todo = next(e for e in store.load_journal()
                if e["id"] == 1 and e.get("kind") == "todo")
    assert todo["status"] == "done"


def test_set_status_on_nonexistent_todo_is_false(fresh_store):
    store, _ = fresh_store
    store.add_journal("just a moment")
    assert store.set_todo_status(1, "done") is False
