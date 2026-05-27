"""Digest module tests — env parsing, channel config, narrate/tool filtering, stats.

Network is fully mocked; no real Discord/Gemini calls. The narrate/tool-trace
filtering predicate is exercised through fetch_window with _fetch_channel
patched to return canned raw messages.
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


@pytest.fixture
def digest_mod(monkeypatch, tmp_path):
    """Reload digest with a fake env file path and clean module state."""
    # Point the env-file lookup at a tmp file that doesn't exist by default.
    fake_env = tmp_path / "env"
    if "digest" in sys.modules:
        del sys.modules["digest"]
    mod = importlib.import_module("digest")
    monkeypatch.setattr(mod, "ENV_FILE", str(fake_env))
    return mod


# ─────────────────────────── _read_env_var ───────────────────────────


def test_read_env_var_prefers_os_environ(digest_mod, monkeypatch):
    monkeypatch.setenv("CCDK_TEST_VAR", "from-environ")
    assert digest_mod._read_env_var("CCDK_TEST_VAR") == "from-environ"


def test_read_env_var_missing_returns_none(digest_mod):
    assert digest_mod._read_env_var("DEFINITELY_NOT_SET_XYZ") is None


def test_read_env_var_reads_from_file(digest_mod, tmp_path, monkeypatch):
    env_file = tmp_path / "env2"
    env_file.write_text('CCDK_DISCORD_TOKEN="abc123"\nOTHER=value\n')
    monkeypatch.setattr(digest_mod, "ENV_FILE", str(env_file))
    monkeypatch.delenv("CCDK_DISCORD_TOKEN", raising=False)
    assert digest_mod._read_env_var("CCDK_DISCORD_TOKEN") == "abc123"


def test_read_env_var_strips_single_quotes(digest_mod, tmp_path, monkeypatch):
    env_file = tmp_path / "env3"
    env_file.write_text("MYKEY='single-quoted'\n")
    monkeypatch.setattr(digest_mod, "ENV_FILE", str(env_file))
    monkeypatch.delenv("MYKEY", raising=False)
    assert digest_mod._read_env_var("MYKEY") == "single-quoted"


# ─────────────────────────── _load_digest_channels ───────────────────────────


def test_load_digest_channels_parses_pairs(digest_mod, monkeypatch):
    monkeypatch.setenv("CCDK_DIGEST_CHANNELS", "general:111,dev:222")
    out = digest_mod._load_digest_channels()
    assert out == {"general": "111", "dev": "222"}


def test_load_digest_channels_empty(digest_mod, monkeypatch):
    monkeypatch.delenv("CCDK_DIGEST_CHANNELS", raising=False)
    assert digest_mod._load_digest_channels() == {}


def test_load_digest_channels_skips_malformed(digest_mod, monkeypatch):
    # Entry with no colon is skipped; valid ones survive.
    monkeypatch.setenv("CCDK_DIGEST_CHANNELS", "good:123, ,bad-no-colon,other:456")
    out = digest_mod._load_digest_channels()
    assert out == {"good": "123", "other": "456"}


def test_load_digest_channels_trims_whitespace(digest_mod, monkeypatch):
    monkeypatch.setenv("CCDK_DIGEST_CHANNELS", "  spaced  :  789  ")
    out = digest_mod._load_digest_channels()
    assert out == {"spaced": "789"}


# ─────────────────────────── stats ───────────────────────────


def _msg(channel, author, is_bot=False):
    return {
        "channel": channel, "channel_id": "0", "id": "1",
        "author": author, "author_id": "a", "is_bot": is_bot,
        "content": "x", "ts": "2026-01-01T00:00:00",
    }


def test_stats_counts_by_channel_and_author(digest_mod):
    msgs = [
        _msg("general", "jeff"),
        _msg("general", "jeff"),
        _msg("dev", "agent-1", is_bot=True),
    ]
    s = digest_mod.stats(msgs)
    assert s["total"] == 3
    assert s["by_channel"] == {"general": 2, "dev": 1}
    assert s["bot_count"] == 1
    assert s["human_count"] == 2
    # by_author sorted descending by count.
    assert list(s["by_author"].items())[0] == ("jeff", 2)


def test_stats_empty(digest_mod):
    s = digest_mod.stats([])
    assert s["total"] == 0
    assert s["bot_count"] == 0
    assert s["human_count"] == 0


# ─────────────────────────── narrate / tool-trace filtering ───────────────────────────


def test_fetch_window_drops_narrate_and_tool_messages(digest_mod, monkeypatch):
    """🧠 narrate placeholders and 🔧 tool-traces must be filtered out."""
    digest_mod.DIGEST_CHANNELS = {"general": "111"}
    digest_mod._FETCH_CACHE.clear()
    monkeypatch.setattr(digest_mod, "_load_token", lambda: "fake-token")

    raw_batch = [
        {"id": "1", "content": "🧠 thinking about something",
         "author": {"username": "bot", "id": "b", "bot": True},
         "timestamp": "2026-01-01T00:00:01"},
        {"id": "2", "content": "🔧 ran a tool",
         "author": {"username": "bot", "id": "b", "bot": True},
         "timestamp": "2026-01-01T00:00:02"},
        {"id": "3", "content": "actual human message",
         "author": {"username": "jeff", "id": "j", "bot": False},
         "timestamp": "2026-01-01T00:00:03"},
    ]
    monkeypatch.setattr(digest_mod, "_fetch_channel",
                        lambda cid, after_ms, token: raw_batch)

    msgs = digest_mod.fetch_window(hours=6)
    contents = [m["content"] for m in msgs]
    assert contents == ["actual human message"]
    assert msgs[0]["author"] == "jeff"
    assert msgs[0]["is_bot"] is False
    assert msgs[0]["channel"] == "general"


def test_fetch_window_no_token_returns_empty(digest_mod, monkeypatch):
    digest_mod._FETCH_CACHE.clear()
    monkeypatch.setattr(digest_mod, "_load_token", lambda: None)
    assert digest_mod.fetch_window(hours=6) == []


def test_fetch_window_skips_failed_channel(digest_mod, monkeypatch):
    """A channel that raises RuntimeError is skipped, not fatal."""
    digest_mod.DIGEST_CHANNELS = {"broken": "111", "ok": "222"}
    digest_mod._FETCH_CACHE.clear()
    monkeypatch.setattr(digest_mod, "_load_token", lambda: "fake-token")

    def fake_fetch(cid, after_ms, token):
        if cid == "111":
            raise RuntimeError("Discord API 403")
        return [{"id": "9", "content": "from ok channel",
                 "author": {"username": "u", "id": "u", "bot": False},
                 "timestamp": "2026-01-01T00:00:00"}]

    monkeypatch.setattr(digest_mod, "_fetch_channel", fake_fetch)
    msgs = digest_mod.fetch_window(hours=6)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "from ok channel"


def test_fetch_window_sorts_oldest_first(digest_mod, monkeypatch):
    digest_mod.DIGEST_CHANNELS = {"general": "111"}
    digest_mod._FETCH_CACHE.clear()
    monkeypatch.setattr(digest_mod, "_load_token", lambda: "fake-token")
    raw = [
        {"id": "2", "content": "second", "author": {"username": "u", "id": "u"},
         "timestamp": "2026-01-01T00:00:05"},
        {"id": "1", "content": "first", "author": {"username": "u", "id": "u"},
         "timestamp": "2026-01-01T00:00:01"},
    ]
    monkeypatch.setattr(digest_mod, "_fetch_channel", lambda c, a, t: raw)
    msgs = digest_mod.fetch_window(hours=6)
    assert [m["content"] for m in msgs] == ["first", "second"]


# ─────────────────────────── summarize_messages guardrails ───────────────────────────


def test_summarize_raises_without_api_key(digest_mod, monkeypatch):
    monkeypatch.setattr(digest_mod, "_load_gemini_key", lambda: None)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        digest_mod.summarize_messages([_msg("g", "u")])


def test_summarize_raises_on_empty_messages(digest_mod, monkeypatch):
    monkeypatch.setattr(digest_mod, "_load_gemini_key", lambda: "fake-key")
    with pytest.raises(RuntimeError, match="no messages"):
        digest_mod.summarize_messages([])
