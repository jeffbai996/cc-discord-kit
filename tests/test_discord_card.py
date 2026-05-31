"""Tests for discord_card rendering — natural-width (wrap-not-trim) + diffs.

Cards no longer hard-wrap to a 32-char mobile width — they emit full-length
lines and rely on Discord's own wrapping. Edit cards render the meta + a
line-numbered diff inside a SINGLE ```diff fenced block (green additions /
red removals), zoomed on the changed window.
"""
from __future__ import annotations

import re

from discord_card import (
    CARD_BODY_LIMIT,
    _diff_body,
    _render_card_block,
    _sanitize_fence,
    format_card,
)


def _block_lines(card: str) -> list[str]:
    """Strip the bold header + fence and return the lines inside the first
    plain code block."""
    m = re.search(r"```\n(.*?)\n```", card, flags=re.DOTALL)
    assert m, f"no code block in card: {card!r}"
    return m.group(1).splitlines()


def _diff_block(card: str) -> str | None:
    """Return the body of the ```diff fenced block, if present."""
    m = re.search(r"```diff\n(.*?)\n```", card, flags=re.DOTALL)
    return m.group(1) if m else None


# ─── card block rendering: no hard mobile-width trim ───

def test_render_card_block_does_not_hard_wrap_long_value():
    long_name = "a long memory name that used to be chopped at thirty-two chars"
    meta = [("type", "project"), ("name", long_name)]
    block = "\n".join(_block_lines("X\n" + _render_card_block(meta, body=None)))
    assert long_name in block  # whole value intact, not split


def test_render_card_block_preserves_body_paragraphs():
    meta = [("type", "project"), ("name", "x")]
    body = "first paragraph words.\n\nsecond paragraph words."
    lines = _block_lines("X\n" + _render_card_block(meta, body))
    assert "" in lines
    text = "\n".join(lines)
    assert "first paragraph words." in text and "second paragraph words." in text


def test_render_card_block_body_not_truncated_under_limit():
    meta = [("type", "user"), ("name", "x")]
    body = "a normal short body that is well under the limit"
    block = "\n".join(_block_lines("X\n" + _render_card_block(meta, body)))
    assert body in block and "…" not in block


# ─── format_card: headers, names, fences ───

def test_memory_saved_name_in_bold_header_not_in_block():
    action = {
        "kind": "memory_saved",
        "entry": {"id": 99, "type": "feedback", "name": "valid name",
                  "tags": ["one", "two"], "about": [], "text": "body text"},
    }
    card = format_card(action)
    assert card is not None
    header = card.splitlines()[0]
    assert "valid name" in header
    assert header.startswith("💾 **Memory #99 saved**")
    assert not any(line.lower().startswith("name:") for line in _block_lines(card))


def test_memory_saved_no_name_omits_dash():
    action = {"kind": "memory_saved",
              "entry": {"id": 1, "type": "user", "tags": [], "about": [], "text": "x"}}
    card = format_card(action)
    assert card is not None
    assert card.splitlines()[0] == "💾 **Memory #1 saved**"


def test_memory_edited_name_in_header():
    action = {"kind": "memory_edited", "id": 42,
              "before": {"name": "old name", "type": "feedback"},
              "after": {"name": "new name", "type": "feedback", "text": "updated body"}}
    card = format_card(action)
    assert card is not None
    assert "new name" in card.splitlines()[0]


def test_memory_deleted_header_is_plain():
    action = {"kind": "memory_deleted",
              "before": {"id": 7, "name": "doomed name", "type": "user",
                         "text": "the body of the doomed memory"}}
    card = format_card(action)
    assert card is not None
    assert card.splitlines()[0] == "🗑️ **Memory #7 deleted**"
    assert any("doomed name" in line for line in _block_lines(card))


def test_format_card_all_kinds_render_without_crash():
    cases = [
        {"kind": "memory_saved", "entry": {"id": 1, "type": "user", "name": "x", "text": "y"}},
        {"kind": "memory_edited", "id": 1, "before": {"name": "a"}, "after": {"name": "b"}},
        {"kind": "memory_deleted", "before": {"id": 1, "type": "user", "name": "n"}},
        {"kind": "journal_added", "entry": {"id": 2, "tags": ["t"], "actor": "a", "text": "t"}},
        {"kind": "journal_edited", "id": 2, "before": {"text": "a"}, "after": {"text": "b"}},
        {"kind": "journal_deleted", "before": {"id": 2}},
    ]
    for c in cases:
        assert isinstance(format_card(c), str)


# ─── fence sanitization ───

def test_sanitize_fence_replaces_triple_backticks():
    bt = "`" * 3
    out = _sanitize_fence(f"arch:\n{bt}\nA->B\n{bt}\nend")
    assert bt not in out
    assert out == "arch:\n′′′\nA->B\n′′′\nend"


def test_sanitize_fence_idempotent_on_clean_text():
    text = "no fences here, just words"
    assert _sanitize_fence(text) == text


def test_memory_saved_with_backtick_body_does_not_break_outer_fence():
    bt = "`" * 3
    action = {
        "kind": "memory_saved",
        "entry": {"id": 5, "type": "reference", "name": "arch-note",
                  "tags": ["x"], "about": ["y"],
                  "text": f"## Arch\n{bt}\nclient -> server\n{bt}\nend"},
    }
    card = format_card(action)
    assert card is not None
    assert card.count(bt) == 2  # outer fence only


# ─── memory_edited diff: markers, line numbers, single ```diff block ───

def test_diff_body_identical_returns_none():
    assert _diff_body("same\ntext", "same\ntext") is None


def test_diff_body_highlights_changed_line():
    out = _diff_body("alpha\nbeta\ngamma", "alpha\nBETA\ngamma")
    assert out is not None
    assert any(l.startswith("-") and "beta" in l for l in out.splitlines())
    assert any(l.startswith("+") and "BETA" in l for l in out.splitlines())
    assert any(l.startswith(" ") and ("alpha" in l or "gamma" in l)
               for l in out.splitlines())


def test_diff_body_has_line_numbers():
    out = _diff_body("alpha\nbeta\ngamma", "alpha\nBETA\ngamma")
    assert out is not None
    changed = [ln for ln in out.splitlines() if "BETA" in ln or "beta" in ln]
    assert changed and all(re.search(r"^[+\- ]\s*2\b", ln) for ln in changed)


def test_diff_body_omits_hunk_header():
    out = _diff_body("alpha\nbeta\ngamma", "alpha\nBETA\ngamma")
    assert out is not None and "@@" not in out


def test_diff_body_marker_then_space_then_gutter():
    """A space sits between the +/- marker and the line number so a double-digit
    gutter (e.g. '+10') doesn't jam against the marker."""
    before = "\n".join(f"line {i}" for i in range(1, 13))
    after = before.replace("line 10", "LINE 10")
    out = _diff_body(before, after)
    assert out is not None
    for line in out.splitlines():
        if line and line[0] in "+-":
            assert line[1] == " ", f"expected space after marker: {line!r}"


def test_diff_body_truncates_at_limit():
    before = "\n".join(f"line {i}" for i in range(500))
    after = "\n".join(f"LINE {i}" for i in range(500))
    out = _diff_body(before, after)
    assert out is not None and len(out) <= CARD_BODY_LIMIT + 1


def test_memory_edited_is_a_single_fenced_block():
    """The whole edit card (meta + rule + diff) lives in ONE ```diff fence —
    two stacked fenced blocks is broken on Discord (dropped diff / ugly gap)."""
    action = {"kind": "memory_edited", "id": 7,
              "before": {"id": 7, "name": "x", "type": "user", "tags": ["a"],
                         "about": [], "text": "first\nold\nlast"},
              "after": {"id": 7, "name": "x", "type": "user", "tags": ["a"],
                        "about": [], "text": "first\nnew\nlast"}}
    card = format_card(action)
    assert card is not None
    assert card.count("```") == 2  # exactly one fenced block
    diff = _diff_block(card)
    assert diff is not None and "type:" in diff and "tags:" in diff


def test_memory_edited_shows_diff_markers():
    action = {"kind": "memory_edited", "id": 1,
              "before": {"id": 1, "name": "cfg", "type": "reference",
                         "tags": ["c"], "about": ["d"],
                         "text": "intro unchanged.\nold port 8080\ntrailing line"},
              "after": {"id": 1, "name": "cfg", "type": "reference",
                        "tags": ["c"], "about": ["d"],
                        "text": "intro unchanged.\nnew port 8765\ntrailing line"}}
    card = format_card(action)
    assert card is not None
    diff = _diff_block(card)
    assert diff is not None
    assert any(l.startswith("-") and "old port 8080" in l for l in diff.splitlines())
    assert any(l.startswith("+") and "new port 8765" in l for l in diff.splitlines())


def test_memory_edited_falls_back_when_body_unchanged():
    action = {"kind": "memory_edited", "id": 50,
              "before": {"id": 50, "name": "thing", "type": "user",
                         "tags": ["old"], "about": [], "text": "the body text"},
              "after": {"id": 50, "name": "thing", "type": "user",
                        "tags": ["new"], "about": [], "text": "the body text"}}
    card = format_card(action)
    assert card is not None
    assert "the body text" in card
    assert _diff_block(card) is None
