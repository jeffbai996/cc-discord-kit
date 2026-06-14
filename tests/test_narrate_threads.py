"""Tests for thread->parent resolution and per-channel config inheritance.

A Discord thread is a channel with its own id; tools/narrate config is
keyed by the parent. These cover the cached resolver and the inheritance
fallback in _channel_tools_mode / channel_mode. Discord API + token are
monkeypatched throughout -- no network."""
import json
import os
import sys

sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")),
)
import narrate  # noqa: E402


def _patch_resolver(monkeypatch, tmp_path, api_responses):
    monkeypatch.setattr(narrate, "_THREAD_CACHE_PATH",
                        str(tmp_path / "thread_parents.json"))
    monkeypatch.setattr(narrate, "read_bot_token", lambda sd: "tok")
    monkeypatch.setattr(narrate, "detect_discord_state_dir", lambda: "/x")
    calls = []

    def fake_request(method, url, token, body=None, _retry=True):
        calls.append(url)
        return api_responses[len(calls) - 1]

    monkeypatch.setattr(narrate, "_discord_request", fake_request)
    return calls


def test_parent_id_thread_resolves_and_caches(tmp_path, monkeypatch):
    calls = _patch_resolver(monkeypatch, tmp_path,
                            [{"type": 11, "parent_id": "PARENT"}])
    assert narrate.channel_parent_id("THREAD") == "PARENT"
    assert narrate.channel_parent_id("THREAD") == "PARENT"
    assert len(calls) == 1


def test_parent_id_plain_channel_returns_none_and_caches(tmp_path, monkeypatch):
    calls = _patch_resolver(monkeypatch, tmp_path, [{"type": 0}])
    assert narrate.channel_parent_id("CHAN") is None
    assert narrate.channel_parent_id("CHAN") is None
    assert len(calls) == 1


def test_parent_id_no_token_returns_none_uncached(tmp_path, monkeypatch):
    _patch_resolver(monkeypatch, tmp_path, [{"type": 11, "parent_id": "P"}])
    monkeypatch.setattr(narrate, "read_bot_token", lambda sd: None)
    assert narrate.channel_parent_id("THREAD") is None
    assert not os.path.exists(str(tmp_path / "thread_parents.json"))


def test_parent_id_api_failure_not_cached(tmp_path, monkeypatch):
    calls = _patch_resolver(monkeypatch, tmp_path,
                            [None, {"type": 11, "parent_id": "P"}])
    assert narrate.channel_parent_id("THREAD") is None
    assert narrate.channel_parent_id("THREAD") == "P"
    assert len(calls) == 2


def _tools_dir(tmp_path, data):
    sd = tmp_path / "state"
    sd.mkdir()
    (sd / "tools.json").write_text(json.dumps(data))
    return str(sd)


def test_tools_thread_inherits_parent(tmp_path, monkeypatch):
    monkeypatch.setattr(narrate, "channel_parent_id", lambda c: "PARENT")
    sd = _tools_dir(tmp_path, {"PARENT": "full"})
    assert narrate._channel_tools_mode(sd, "THREAD") == "full"


def test_tools_thread_own_entry_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(narrate, "channel_parent_id",
                        lambda c: (_ for _ in ()).throw(AssertionError("called")))
    sd = _tools_dir(tmp_path, {"PARENT": "full", "THREAD": "collapse"})
    assert narrate._channel_tools_mode(sd, "THREAD") == "collapse"


def test_tools_plain_channel_off_default(tmp_path, monkeypatch):
    monkeypatch.setattr(narrate, "channel_parent_id", lambda c: None)
    sd = _tools_dir(tmp_path, {"OTHER": "full"})
    assert narrate._channel_tools_mode(sd, "CHAN") == "off"


def test_tools_thread_parent_unset_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(narrate, "channel_parent_id", lambda c: "PARENT")
    sd = _tools_dir(tmp_path, {"OTHER": "full"})
    assert narrate._channel_tools_mode(sd, "THREAD") == "off"


def _narrate_cfg(tmp_path, monkeypatch, data):
    p = tmp_path / "narrate.json"
    p.write_text(json.dumps(data))
    monkeypatch.setattr(narrate, "_narrate_config_path", lambda: str(p))


def test_narrate_thread_inherits_parent(tmp_path, monkeypatch):
    _narrate_cfg(tmp_path, monkeypatch, {"PARENT": "always"})
    monkeypatch.setattr(narrate, "channel_parent_id", lambda c: "PARENT")
    assert narrate.channel_mode("THREAD") == "always"


def test_narrate_thread_own_entry_wins(tmp_path, monkeypatch):
    _narrate_cfg(tmp_path, monkeypatch, {"PARENT": "always", "THREAD": "collapse"})
    monkeypatch.setattr(narrate, "channel_parent_id",
                        lambda c: (_ for _ in ()).throw(AssertionError("called")))
    assert narrate.channel_mode("THREAD") == "collapse"


def test_narrate_plain_channel_default_never(tmp_path, monkeypatch):
    _narrate_cfg(tmp_path, monkeypatch, {"OTHER": "always"})
    monkeypatch.setattr(narrate, "channel_parent_id", lambda c: None)
    assert narrate.channel_mode("CHAN") == "never"
