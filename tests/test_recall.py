"""recall_memories tests — vecgrep path + substring fallback. Generic data."""
from __future__ import annotations

import vecgrep_client


def test_recall_uses_vecgrep_rank_order(fresh_store, monkeypatch):
    store, _ = fresh_store
    a = store.save_memory("apple stuff", name="apple", type="project")
    b = store.save_memory("banana stuff", name="banana", type="project")
    monkeypatch.setattr(
        vecgrep_client, "search_corpus_to_ids_with_match",
        lambda q, corpus, want_kind=None: [(b["id"], 95.0, frozenset({"vector"})),
                                           (a["id"], 80.0, frozenset({"vector"}))],
    )
    entries, source = store.recall_memories("fruit")
    assert source == "vecgrep"
    assert [m["id"] for m in entries] == [b["id"], a["id"]]


def test_recall_falls_back_to_substring(fresh_store, monkeypatch):
    store, _ = fresh_store
    store.save_memory("the cat is grey", name="pet", type="reference")

    def _boom(*a, **k):
        raise vecgrep_client.VecgrepUnavailable("down")
    monkeypatch.setattr(vecgrep_client, "search_corpus_to_ids_with_match", _boom)

    entries, source = store.recall_memories("cat")
    assert source == "substring"
    assert any("cat" in m["text"] for m in entries)


def test_recall_top_k_caps_results(fresh_store, monkeypatch):
    store, _ = fresh_store
    ids = [store.save_memory(f"body {i}", name=f"m{i}", type="project")["id"]
           for i in range(3)]
    monkeypatch.setattr(
        vecgrep_client, "search_corpus_to_ids_with_match",
        lambda q, corpus, want_kind=None: [(i, 90.0, frozenset()) for i in reversed(ids)],
    )
    entries, _ = store.recall_memories("anything", top_k=2)
    assert len(entries) == 2
