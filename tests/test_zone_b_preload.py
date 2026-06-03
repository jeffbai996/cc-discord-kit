"""Zone-B full-body preload tests. Generic fixtures only (fresh_store)."""
from __future__ import annotations


def test_estimate_tokens_roughly_quarter_of_chars(fresh_store):
    store, _ = fresh_store
    assert store.estimate_tokens("x" * 400) == 100


def test_estimate_tokens_empty_is_zero(fresh_store):
    store, _ = fresh_store
    assert store.estimate_tokens("") == 0


def test_preload_unbounded_loads_all(fresh_store):
    store, _ = fresh_store
    store.save_memory("alpha facts", name="alpha", type="project")
    store.save_memory("beta facts", name="beta", type="reference")
    block, loaded = store.format_memories_full_preload(budget_tokens=0)
    assert len(loaded) == 2
    assert "2 of 2" in block
    assert "alpha facts" in block and "beta facts" in block


def test_preload_excludes_feedback_by_default(fresh_store):
    store, _ = fresh_store
    store.save_memory("a rule", name="rule", type="feedback")
    store.save_memory("project body", name="proj", type="project")
    block, loaded = store.format_memories_full_preload(budget_tokens=0)
    # feedback excluded; only the project memory loads
    assert "a rule" not in block
    assert "project body" in block


def test_preload_budget_drops_and_reports(fresh_store):
    store, _ = fresh_store
    # 3 project memories; budget small enough that the oldest drops.
    # body template is "- #N name\n  TEXT"; size via estimate_tokens.
    m1 = store.save_memory("xxxx", name="one", type="project")
    m2 = store.save_memory("yyyy", name="two", type="project")
    m3 = store.save_memory("zzzz", name="three", type="project")
    # newest-first => m3, m2, m1. Compute a budget that fits exactly 2.
    body = lambda m: f"- #{m['id']} {m['name']}\n  {m['text']}"
    budget = store.estimate_tokens(body(m3)) + store.estimate_tokens(body(m2))
    block, loaded = store.format_memories_full_preload(budget_tokens=budget)
    assert loaded == [m3["id"], m2["id"]]
    assert m1["id"] not in loaded
    assert "2 of 3" in block


def test_preload_budget_below_newest_loads_zero(fresh_store):
    store, _ = fresh_store
    # A budget smaller than the single newest entry must load ZERO full bodies
    # (strict cap) — not inject the oversized memory anyway. Everything falls
    # to index-only.
    m = store.save_memory("a fairly long body that exceeds the tiny budget",
                          name="big", type="project")
    body = f"- #{m['id']} {m['name']}\n  {m['text']}"
    tiny = store.estimate_tokens(body) - 1  # one token under the only entry
    block, loaded = store.format_memories_full_preload(budget_tokens=tiny)
    assert loaded == []
    assert "0 of 1" in block


def test_preload_empty_store(fresh_store):
    store, _ = fresh_store
    block, loaded = store.format_memories_full_preload(budget_tokens=0)
    assert block == "" and loaded == []
