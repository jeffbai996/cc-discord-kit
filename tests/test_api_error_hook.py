"""Tests for api_error_hook — surface CLI API errors (529/429/4xx) to Discord.

A 529/overload never fires a Notification event, so notify_hook can't catch it;
the bot just goes silent mid-turn. But the CLI DOES write an `isApiErrorMessage`
entry into the session transcript with an `apiErrorStatus`. This Stop hook scans
the transcript tail for an unreported api-error entry and posts a classified
line to the exact inbound Discord channel.

These tests pin the pure logic: detection + dedup, status classification, and
message formatting. The I/O main() is a thin wrapper around them.
"""
import json

import pytest

from hooks import api_error_hook as h


def _err_entry(uuid, status, text, ts="2026-06-17T16:00:00Z"):
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "isApiErrorMessage": True,
        "apiErrorStatus": status,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _normal_entry(uuid, role="assistant", text="hello"):
    return {
        "type": role,
        "uuid": uuid,
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def _lines(*entries):
    return [json.dumps(e) for e in entries]


# --- classify_status ---

class TestClassifyStatus:
    def test_529_is_transient_overload(self):
        emoji, label, transient = h.classify_status(529)
        assert transient is True
        assert "overload" in label.lower()

    def test_429_is_transient_rate_limit(self):
        emoji, label, transient = h.classify_status(429)
        assert transient is True
        assert "rate" in label.lower()

    def test_400_is_non_transient(self):
        # A 4xx config error won't self-heal — it needs a human, not a retry.
        emoji, label, transient = h.classify_status(400)
        assert transient is False

    def test_404_is_non_transient(self):
        _e, _l, transient = h.classify_status(404)
        assert transient is False

    def test_unknown_status_defaults_non_transient(self):
        _e, _l, transient = h.classify_status(0)
        assert transient is False


# --- find_unreported_api_error ---

class TestFindUnreportedApiError:
    def test_finds_the_api_error_entry(self):
        lines = _lines(_normal_entry("u1"), _err_entry("e1", 529, "API Error: 529"))
        entry = h.find_unreported_api_error(lines, last_seen_uuid=None)
        assert entry is not None and entry["uuid"] == "e1"

    def test_returns_none_when_no_api_error(self):
        lines = _lines(_normal_entry("u1"), _normal_entry("u2"))
        assert h.find_unreported_api_error(lines, last_seen_uuid=None) is None

    def test_dedups_already_reported(self):
        # Same error entry on a later Stop must not be re-reported.
        lines = _lines(_normal_entry("u1"), _err_entry("e1", 529, "API Error: 529"))
        assert h.find_unreported_api_error(lines, last_seen_uuid="e1") is None

    def test_returns_most_recent_when_multiple(self):
        lines = _lines(
            _err_entry("e1", 429, "API Error: 429"),
            _normal_entry("u2"),
            _err_entry("e2", 529, "API Error: 529"),
        )
        entry = h.find_unreported_api_error(lines, last_seen_uuid=None)
        assert entry["uuid"] == "e2"

    def test_ignores_malformed_lines(self):
        lines = ["not json{", json.dumps(_err_entry("e1", 529, "API Error: 529"))]
        entry = h.find_unreported_api_error(lines, last_seen_uuid=None)
        assert entry["uuid"] == "e1"

    def test_false_flag_is_ignored(self):
        # isApiErrorMessage absent/false → not an error entry.
        e = _normal_entry("u1")
        e["isApiErrorMessage"] = False
        assert h.find_unreported_api_error(_lines(e), last_seen_uuid=None) is None


# --- format_api_error_message ---

class TestFormatMessage:
    def test_transient_message_says_retrying(self):
        entry = _err_entry("e1", 529, "API Error: 529 Overloaded")
        msg = h.format_api_error_message(entry)
        assert "529" in msg
        assert "retry" in msg.lower() or "overload" in msg.lower()

    def test_non_transient_message_flags_attention(self):
        entry = _err_entry("e1", 400, 'API Error: 400 "thinking.type.disabled" ...')
        msg = h.format_api_error_message(entry)
        assert "400" in msg
        # A non-transient error should NOT promise a retry/self-heal.
        assert "retrying" not in msg.lower()

    def test_includes_human_readable_text(self):
        entry = _err_entry("e1", 529, "API Error: 529 service overloaded")
        msg = h.format_api_error_message(entry)
        assert "overloaded" in msg.lower()

    def test_truncates_long_error_text(self):
        entry = _err_entry("e1", 400, "API Error: 400 " + "x" * 1000)
        msg = h.format_api_error_message(entry)
        assert len(msg) < 600  # stays well under Discord's 2000 limit
