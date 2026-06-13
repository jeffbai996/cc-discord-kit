"""Tests for discord_card rendering — natural-width (wrap-not-trim) + diffs.

As of 2026-05-30 the cards NO LONGER hard-wrap to a 32-char mobile width —
they emit full-length lines and rely on Discord's own wrapping. The diff body
on edit cards carries a line-number gutter and renders inside a ```diff fence
(green additions / red removals).
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


# ─── card block rendering (no more hard mobile-width trim) ───

def test_render_card_block_does_not_hard_wrap_long_value():
    """A long value is NOT split to fit 32 chars anymore — it renders whole
    and Discord wraps it. The full string survives on one logical line."""
    long_name = "Discord pass-through mode — shell/Claude Code commands"
    meta = [("type", "project"), ("name", long_name)]
    block = "\n".join(_block_lines("X\n" + _render_card_block(meta, body=None)))
    # The whole value appears intact somewhere in the block (not chopped).
    assert long_name in block


def test_render_card_block_preserves_body_paragraphs():
    meta = [("type", "project"), ("name", "x")]
    body = "first paragraph words.\n\nsecond paragraph words."
    lines = _block_lines("X\n" + _render_card_block(meta, body))
    assert "" in lines  # blank line between paragraphs survives
    text = "\n".join(lines)
    assert "first paragraph words." in text
    assert "second paragraph words." in text


def test_render_card_block_body_not_truncated_under_limit():
    meta = [("type", "user"), ("name", "x")]
    body = "a normal short body that is well under the limit"
    block = "\n".join(_block_lines("X\n" + _render_card_block(meta, body)))
    assert body in block
    assert "…" not in block


# ─── format_card: names, headers, fences ───

def test_format_card_memory_saved_full_value_survives():
    """The long #98 name renders intact (no mid-string chop)."""
    action = {
        "kind": "memory_saved",
        "entry": {
            "id": 98,
            "type": "project",
            "name": "Discord pass-through mode — shell/Claude Code commands",
            "tags": ["discord", "passthrough", "bash", "claude-code"],
            "about": ["jeff"],
            "text": (
                "Jeff wants a 'pass-through mode' where bash commands or "
                "Claude Code commands sent via Discord are executed directly."
            ),
        },
    }
    card = format_card(action)
    assert card is not None
    block = "\n".join(_block_lines(card))
    assert "discord, passthrough, bash, claude-code" in block


def test_memory_saved_name_in_bold_header_not_in_block():
    action = {
        "kind": "memory_saved",
        "entry": {
            "id": 99, "type": "feedback", "name": "valid name",
            "tags": ["one", "two", "three"], "about": [], "text": "body text",
        },
    }
    card = format_card(action)
    assert card is not None
    header = card.splitlines()[0]
    assert "valid name" in header
    assert header.startswith("💾 **Memory #99 saved**")
    block = _block_lines(card)
    assert not any(line.lower().startswith("name:") for line in block)


def test_memory_saved_no_name_omits_dash():
    action = {
        "kind": "memory_saved",
        "entry": {"id": 1, "type": "user", "tags": [], "about": [], "text": "x"},
    }
    card = format_card(action)
    assert card is not None
    assert card.splitlines()[0] == "💾 **Memory #1 saved**"


def test_memory_edited_name_in_header():
    action = {
        "kind": "memory_edited", "id": 42,
        "before": {"name": "old name", "type": "feedback"},
        "after": {"name": "new name", "type": "feedback", "text": "updated body"},
    }
    card = format_card(action)
    assert card is not None
    assert "new name" in card.splitlines()[0]


def test_memory_deleted_header_is_plain():
    action = {
        "kind": "memory_deleted",
        "before": {"id": 7, "name": "doomed name", "type": "user",
                   "text": "the body of the doomed memory"},
    }
    card = format_card(action)
    assert card is not None
    assert card.splitlines()[0] == "🗑️ **Memory #7 deleted**"
    assert any("doomed name" in line for line in _block_lines(card))


def test_memory_deleted_includes_content_excerpt():
    action = {
        "kind": "memory_deleted",
        "before": {"id": 8, "name": "ice cream flavors", "type": "user",
                   "text": "Tillamook butter pecan. Oreo ice cream sandwiches."},
    }
    card = format_card(action)
    assert card is not None
    block_text = "\n".join(_block_lines(card))
    assert "Tillamook" in block_text or "butter pecan" in block_text


def test_memory_deleted_no_name_no_dangling_dash():
    action = {
        "kind": "memory_deleted",
        "before": {"id": 9, "type": "user", "text": "body"},
    }
    card = format_card(action)
    assert card is not None
    assert not any(line.lower().startswith("name:") for line in _block_lines(card))


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


# ─── fence sanitization (overflow bug from #103 vecgrep edit) ───

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
        "entry": {
            "id": 103, "type": "reference", "name": "vecgrep",
            "tags": ["vecgrep"], "about": ["fragserv"],
            "text": f"## Arch\n{bt}\nclaude -> vecgrep\n{bt}\nend",
        },
    }
    card = format_card(action)
    assert card is not None
    assert card.count(bt) == 2, f"expected 2 fences (outer only), got {card.count(bt)}"


# ─── memory_edited diff: +/- markers, line numbers, ```diff fence ───

def test_diff_body_identical_returns_none():
    assert _diff_body("same\ntext", "same\ntext") is None


def test_diff_body_highlights_changed_line():
    out = _diff_body("alpha\nbeta\ngamma", "alpha\nBETA\ngamma")
    assert out is not None
    # marker char stays at column 0 (Discord's diff highlighter needs it there)
    assert any(line.startswith("-") and "beta" in line for line in out.splitlines())
    assert any(line.startswith("+") and "BETA" in line for line in out.splitlines())
    # at least one context line (unchanged neighbor) prefixed with a space
    assert any(line.startswith(" ") and ("alpha" in line or "gamma" in line)
               for line in out.splitlines())


def test_diff_body_has_line_numbers():
    """Each diff line carries a source line number in a gutter after the
    marker, so the reader knows WHICH lines changed."""
    out = _diff_body("alpha\nbeta\ngamma", "alpha\nBETA\ngamma")
    assert out is not None
    # 'beta'/'BETA' are on line 2 — the gutter must show 2 next to them.
    changed = [ln for ln in out.splitlines() if "BETA" in ln or "beta" in ln]
    assert changed and all(re.search(r"^[+\- ]\s*2\b", ln) for ln in changed), \
        f"expected line-number 2 in gutter: {changed!r}"


def test_diff_body_marker_at_column_zero():
    """The +/-/space marker must be the first char of every diff line so
    Discord's ```diff colorizer fires."""
    out = _diff_body("a\nb\nc", "a\nB\nc")
    assert out is not None
    for line in out.splitlines():
        if line:  # non-empty lines start with a diff marker
            assert line[0] in "+- ", f"line does not start with a marker: {line!r}"


def test_diff_body_omits_hunk_header():
    """The `@@ -a,b +c,d @@` hunk header is NOT rendered — it's not
    human-parseable. Only +/-/context lines (with gutters) appear."""
    out = _diff_body("alpha\nbeta\ngamma", "alpha\nBETA\ngamma")
    assert out is not None
    assert "@@" not in out


def test_diff_body_space_after_marker():
    """One cell of whitespace between the +/- marker and the line-number
    gutter, so the marker is visually separated from the content even when
    the gutter is double-digit (e.g. '+10' jammed → '+ 10'). Jeff 2026-05-31.
    Marker stays at column 0 (colorizer still fires); the space is char[1]."""
    # 12 lines forces a 2-digit gutter; change line 10 so we exercise a
    # double-digit number where the old code jammed marker against digits.
    before = "\n".join(f"line {i}" for i in range(1, 13))
    after = before.replace("line 10", "LINE 10")
    out = _diff_body(before, after)
    assert out is not None
    for line in out.splitlines():
        if line and line[0] in "+-":
            assert line[1] == " ", \
                f"expected a space between marker and gutter: {line!r}"


def test_diff_body_truncates_at_limit():
    before = "\n".join(f"line {i}" for i in range(500))
    after = "\n".join(f"LINE {i}" for i in range(500))
    out = _diff_body(before, after)
    assert out is not None
    assert len(out) <= CARD_BODY_LIMIT + 1  # +1 for the trailing ellipsis


def test_memory_edited_shows_diff_in_diff_fence():
    action = {
        "kind": "memory_edited", "id": 103,
        "before": {"id": 103, "name": "vecgrep", "type": "reference",
                   "tags": ["vecgrep"], "about": ["fragserv"],
                   "text": "intro paragraph that is unchanged.\nold port 8080\ntrailing line"},
        "after": {"id": 103, "name": "vecgrep", "type": "reference",
                  "tags": ["vecgrep"], "about": ["fragserv"],
                  "text": "intro paragraph that is unchanged.\nnew port 8765\ntrailing line"},
    }
    card = format_card(action)
    assert card is not None
    diff = _diff_block(card)
    assert diff is not None, "edit card must contain a ```diff block"
    assert any(l.startswith("-") and "old port 8080" in l for l in diff.splitlines())
    assert any(l.startswith("+") and "new port 8765" in l for l in diff.splitlines())


def test_memory_edited_diff_colors_meta_outside_fence():
    """The diff lives ALONE in one ```diff fence so Discord's colorizer fires
    (folding the meta lines into the fence suppressed coloring — 2026-05-31).
    Meta renders as bold prose ABOVE the fence, not inside it. Exactly one
    fenced block, so no 'two adjacent fences → dropped diff' problem either."""
    action = {
        "kind": "memory_edited", "id": 7,
        "before": {"id": 7, "name": "x", "type": "user", "tags": ["a"],
                   "about": [], "text": "first\nold\nlast"},
        "after": {"id": 7, "name": "x", "type": "user", "tags": ["a"],
                  "about": [], "text": "first\nnew\nlast"},
    }
    card = format_card(action)
    assert card is not None
    # Exactly one fenced block (the diff): one ```diff opener + one ``` closer.
    assert card.count("```") == 2, f"expected a single fenced block, got {card.count('```')//2}"
    diff = _diff_block(card)
    assert diff is not None
    # Meta is NOT inside the diff fence (that's what broke coloring)...
    assert "type:" not in diff and "tags:" not in diff
    # ...it's bold prose above the fence instead.
    assert "**type:**" in card and "**tags:**" in card
    # The diff lines still carry their +/- markers for coloring.
    assert any(l.startswith("+") for l in diff.splitlines())
    assert any(l.startswith("-") for l in diff.splitlines())


def test_memory_edited_falls_back_when_body_unchanged():
    action = {
        "kind": "memory_edited", "id": 50,
        "before": {"id": 50, "name": "thing", "type": "user",
                   "tags": ["old"], "about": [], "text": "the body text"},
        "after": {"id": 50, "name": "thing", "type": "user",
                  "tags": ["new"], "about": [], "text": "the body text"},
    }
    card = format_card(action)
    assert card is not None
    assert "the body text" in card
    assert _diff_block(card) is None  # no diff block when body is unchanged


def test_todo_added_card_is_idless():
    import discord_card as dc
    out = dc.format_card({"kind": "todo_added",
                          "entry": {"id": 7, "text": "ship the thing",
                                    "owner": "jeff", "due": "2026-07-01"}})
    assert "To-do added" in out
    assert "ship the thing" in out and "@jeff" in out and "2026-07-01" in out
    assert "#7" not in out            # todos are id-less on the card


def test_todo_status_cards():
    import discord_card as dc
    done = dc.format_card({"kind": "todo_status", "id": 7, "status": "done",
                           "text": "ship the thing"})
    assert "To-do done" in done and "ship the thing" in done and "#7" not in done
    reopened = dc.format_card({"kind": "todo_status", "id": 7, "status": "open",
                               "text": "ship the thing"})
    assert "reopened" in reopened.lower()
