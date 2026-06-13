"""semantic.format_recall tests — the per-turn recall block. Generic data."""
from __future__ import annotations

import importlib

import semantic
import vecgrep_client


def _reload_semantic():
    """fresh_store reimports `store` into a new module object; rebind semantic's
    `store`/`vg` references to match (a test-isolation concern only — in
    production the hook is one process with CCDK_DATA_DIR set at launch)."""
    return importlib.reload(semantic)


def _mock_hits(monkeypatch, pairs):
    """pairs: list of (entry_id, pct) -> mock vecgrep ranked output."""
    monkeypatch.setattr(
        vecgrep_client, "search_corpus_to_ids_with_match",
        lambda q, corpus, top_k=None, want_kind=None: [
            (eid, pct, frozenset({"vector"})) for eid, pct in pairs],
    )


def test_block_renders_top_hits(fresh_store, monkeypatch):
    store, _ = fresh_store
    semantic = _reload_semantic()
    a = store.save_memory("apple body text", name="apple", type="project")
    b = store.save_memory("banana body text", name="banana", type="project")
    _mock_hits(monkeypatch, [(b["id"], 95.0), (a["id"], 80.0)])

    block = semantic.format_recall("which fruit did we pick")
    assert "RELEVANT TO YOUR MESSAGE" in block
    # vecgrep rank order preserved; both rendered with id, pct, name
    lines = [l for l in block.splitlines() if l.strip().startswith("•")]
    assert f"#{b['id']}" in lines[0] and "[95%]" in lines[0] and "banana" in lines[0]
    assert f"#{a['id']}" in lines[1] and "apple" in lines[1]


def test_floor_filters_low_scores(fresh_store, monkeypatch):
    store, _ = fresh_store
    semantic = _reload_semantic()
    keep = store.save_memory("keep me", name="keep", type="project")
    drop = store.save_memory("too soft", name="drop", type="project")
    _mock_hits(monkeypatch, [(keep["id"], 88.0), (drop["id"], 12.0)])

    block = semantic.format_recall("a sufficiently long query here")
    assert f"#{keep['id']}" in block
    assert f"#{drop['id']}" not in block


def test_caps_to_top_n(fresh_store, monkeypatch):
    store, _ = fresh_store
    semantic = _reload_semantic()
    ids = [store.save_memory(f"body {i}", name=f"m{i}", type="project")["id"]
           for i in range(6)]
    _mock_hits(monkeypatch, [(i, 90.0) for i in ids])

    block = semantic.format_recall("a sufficiently long query here")
    bullets = [l for l in block.splitlines() if l.strip().startswith("•")]
    assert len(bullets) == semantic.TOP_N


def test_silent_when_vecgrep_down(fresh_store, monkeypatch):
    store, _ = fresh_store
    store.save_memory("something", name="x", type="project")

    def _boom(*a, **k):
        raise vecgrep_client.VecgrepUnavailable("down")
    monkeypatch.setattr(vecgrep_client, "search_corpus_to_ids_with_match", _boom)

    assert semantic.format_recall("a sufficiently long query here") == ""


def test_skips_short_prompt(fresh_store, monkeypatch):
    _mock_hits(monkeypatch, [(1, 99.0)])
    assert semantic.format_recall("go") == ""


def test_toggle_off(fresh_store, monkeypatch):
    monkeypatch.setenv("CCDK_SEMANTIC_RECALL", "0")
    _mock_hits(monkeypatch, [(1, 99.0)])
    assert semantic.format_recall("a sufficiently long query here") == ""


def test_files_recall_renders_and_is_fail_silent(fresh_store, monkeypatch):
    _reload_semantic()
    import semantic, vecgrep_client
    # mock the files-corpus search to return one hit above the floor
    monkeypatch.setattr(vecgrep_client, "_post_search",
                        lambda q, corpus, top_k=None: [
                            {"source_id": "/x/file-7.md", "similarity_pct": 88.0,
                             "chunk": "the rotation signal is capex deceleration"}])
    block = semantic.format_files_recall("what signals the rotation")
    assert "RELEVANT FILES" in block
    assert "file #7" in block and "capex deceleration" in block

    # vecgrep down -> silent ""
    def _boom(*a, **k):
        raise vecgrep_client.VecgrepUnavailable("down")
    monkeypatch.setattr(vecgrep_client, "_post_search", _boom)
    assert semantic.format_files_recall("what signals the rotation") == ""
