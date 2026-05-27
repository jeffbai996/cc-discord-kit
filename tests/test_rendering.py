"""Rendering module tests — ref linkification, extraction, backlinks.

These are pure functions (no I/O), so no fixture is needed. We import
rendering directly; ensure the project root is importable.
"""

from __future__ import annotations

import os
import sys

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

rendering = pytest.importorskip("rendering")  # skip if bleach/markdown absent


# ─────────────────────────── extract_refs ───────────────────────────


def test_extract_refs_memory_ids():
    mems, jnls = rendering.extract_refs("see #42 and also #100")
    assert mems == {42, 100}
    assert jnls == set()


def test_extract_refs_journal_ids():
    mems, jnls = rendering.extract_refs("logged in j#7 and j#9")
    assert jnls == {7, 9}
    assert mems == set()


def test_extract_refs_long_form():
    mems, jnls = rendering.extract_refs("[[memory:5]] and [[journal:6]]")
    assert mems == {5}
    assert jnls == {6}


def test_extract_refs_empty_text():
    assert rendering.extract_refs("") == (set(), set())
    assert rendering.extract_refs(None) == (set(), set())


def test_extract_refs_ignores_hash_color_and_fragments():
    # `#fff` (color) and `#fragment-id` (slug) must NOT be treated as memory ids.
    mems, jnls = rendering.extract_refs("color #fff and #section-name but #42 is real")
    assert mems == {42}


def test_extract_refs_mixed():
    mems, jnls = rendering.extract_refs("#1 j#2 [[memory:3]] [[journal:4]]")
    assert mems == {1, 3}
    assert jnls == {2, 4}


# ─────────────────────────── _linkify_refs ───────────────────────────


def test_linkify_memory_ref_produces_markdown_link():
    out = rendering._linkify_refs("ref #42 here", "/squad")
    assert "[#42](/squad/memory/42)" in out


def test_linkify_journal_ref():
    out = rendering._linkify_refs("ref j#7 here", "/squad")
    assert "[j#7](/squad/journal/7)" in out


def test_linkify_long_form_both_kinds():
    out = rendering._linkify_refs("[[memory:1]] [[journal:2]]", "/x")
    assert "[#1](/x/memory/1)" in out
    assert "[j#2](/x/journal/2)" in out


def test_linkify_journal_not_double_linked_as_memory():
    # j#7 should become a journal link, NOT also a memory link off the bare 7.
    out = rendering._linkify_refs("j#7", "/p")
    assert "/journal/7" in out
    assert "/memory/7" not in out


# ─────────────────────────── render_body ───────────────────────────


def test_render_body_empty():
    assert str(rendering.render_body("")) == ""


def test_render_body_basic_markdown():
    out = str(rendering.render_body("**bold** and *italic*"))
    assert "<strong>bold</strong>" in out
    assert "<em>italic</em>" in out


def test_render_body_strips_script():
    out = str(rendering.render_body("<script>alert(1)</script>safe"))
    assert "<script>" not in out
    assert "alert" in out or "safe" in out  # text survives, tag stripped


def test_render_body_linkifies_refs():
    out = str(rendering.render_body("see #42", url_prefix="/squad/"))
    assert "/squad/memory/42" in out
    assert "<a" in out


# ─────────────────────────── find_backlinks ───────────────────────────


def test_find_backlinks_memory_to_memory():
    memories = [
        {"id": 1, "text": "this references #5", "name": "A", "type": "user"},
        {"id": 2, "text": "no refs here", "name": "B", "type": "feedback"},
        {"id": 5, "text": "the target", "name": "T", "type": "project"},
    ]
    out = rendering.find_backlinks(5, "memory", memories, [])
    assert len(out["memories"]) == 1
    assert out["memories"][0]["id"] == 1
    assert out["memories"][0]["name"] == "A"
    assert out["journal"] == []


def test_find_backlinks_excludes_self_reference():
    memories = [{"id": 5, "text": "I mention myself #5", "name": "self", "type": "user"}]
    out = rendering.find_backlinks(5, "memory", memories, [])
    # A memory referencing its own id shouldn't list itself as a backlink.
    assert out["memories"] == []


def test_find_backlinks_journal_target():
    journal = [
        {"id": 10, "text": "see j#3", "ts": "2026-01-01", "actor": "agent-1"},
        {"id": 11, "text": "nothing", "ts": "2026-01-02", "actor": "agent-2"},
    ]
    memories = [{"id": 1, "text": "also j#3", "name": "M", "type": "feedback"}]
    out = rendering.find_backlinks(3, "journal", memories, journal)
    assert len(out["journal"]) == 1
    assert out["journal"][0]["id"] == 10
    assert len(out["memories"]) == 1
    assert out["memories"][0]["id"] == 1


def test_find_backlinks_none_found():
    out = rendering.find_backlinks(999, "memory", [{"id": 1, "text": "no refs"}], [])
    assert out == {"memories": [], "journal": []}
