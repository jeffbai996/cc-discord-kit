"""Tests for clear_agents() and the agents help page. State file and
Discord I/O are monkeypatched throughout."""
import os
import sys

sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")),
)
import agent_view as av  # noqa: E402


def _agent(status="done", started=100.0, ended=131.0, tokens=10):
    return {"label": "x", "model": None, "status": status,
            "started_at": started, "ended_at": ended, "tokens": tokens,
            "prompt_sha": "p", "agent_id": None, "_prompt": "x",
            "transcript": None}


def _seed(monkeypatch, tmp_path, agents, **sess_extra):
    monkeypatch.setenv("CCDK_AGENT_VIEW_STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(av, "_bot_key", lambda: "bot")
    sess = {"chat_id": "999", "transcript_path": "/tmp/none.jsonl",
            "updater_pid": None, "standalone_msg_id": None,
            "panel_cards": [], "agents": agents}
    sess.update(sess_extra)
    av._save_av_state({"bot:sess1": sess})


def test_clear_finished_keeps_running(tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path, {
        "a": _agent(status="done"),
        "b": _agent(status="failed"),
        "c": _agent(status="running", ended=None),
    })
    res = av.clear_agents(scope="finished")
    assert res["found"] and res["cleared"] == 2 and res["kept"] == 1
    assert res["emptied"] is False
    assert set(av._load_av_state()["bot:sess1"]["agents"]) == {"c"}


def test_clear_all_empties(tmp_path, monkeypatch):
    monkeypatch.setattr(av, "_bot_token", lambda: None)
    _seed(monkeypatch, tmp_path, {
        "a": _agent(status="done"),
        "c": _agent(status="running", ended=None),
    })
    res = av.clear_agents(scope="all")
    assert res["cleared"] == 2 and res["kept"] == 0 and res["emptied"] is True
    assert av._load_av_state()["bot:sess1"]["agents"] == {}


def test_clear_empties_deletes_standalone_card(tmp_path, monkeypatch):
    monkeypatch.setattr(av, "_bot_token", lambda: "tok")
    deleted = []
    monkeypatch.setattr(av, "_discord_delete",
                        lambda t, c, mid: deleted.append(mid) or True)
    _seed(monkeypatch, tmp_path,
          {"a": _agent(status="done")},
          standalone_msg_id="card1", panel_cards=["card0", "card1"])
    res = av.clear_agents(scope="finished")
    assert res["emptied"] is True
    assert set(deleted) == {"card0", "card1"}
    sess = av._load_av_state()["bot:sess1"]
    assert sess["standalone_msg_id"] is None and sess["panel_cards"] == []


def test_clear_no_session_returns_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("CCDK_AGENT_VIEW_STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(av, "_bot_key", lambda: "bot")
    av._save_av_state({})
    res = av.clear_agents(scope="finished")
    assert res["found"] is False and res["cleared"] == 0


def test_clear_targets_most_recent_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CCDK_AGENT_VIEW_STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(av, "_bot_key", lambda: "bot")
    monkeypatch.setattr(av, "_bot_token", lambda: None)
    old = {"chat_id": "1", "agents": {"a": _agent(started=100.0)},
           "standalone_msg_id": None, "panel_cards": []}
    new = {"chat_id": "2", "agents": {"b": _agent(started=999.0)},
           "standalone_msg_id": None, "panel_cards": []}
    av._save_av_state({"bot:old": old, "bot:new": new})
    res = av.clear_agents(scope="all")
    assert res["cleared"] == 1
    state = av._load_av_state()
    assert state["bot:new"]["agents"] == {}    # newest cleared
    assert state["bot:old"]["agents"] != {}    # older untouched


def test_agents_help_mentions_subcommands():
    h = av.agents_help()
    for needle in ("!agents clear", "clear all", "!agents help", "view-only"):
        assert needle in h
