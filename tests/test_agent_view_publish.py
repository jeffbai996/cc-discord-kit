"""Tests for the panel publisher: tool-trace footer composition and
mode policy. Discord I/O is monkeypatched throughout."""
import os
import sys

sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")),
)
import agent_view as av  # noqa: E402
import tool_watcher as tw  # noqa: E402


PANEL = "```\nagents · bot · 1 running · 0 done · 0 tok\n\n  ○  x  ?  1s  0\n```"


def test_tool_message_content_appends_panel_after_fence():
    out = tw._tool_message_content("+ Bash(ls)", panel=PANEL)
    assert out.endswith(PANEL)
    assert out.index("```diff") < out.index("agents ·")


def test_tool_message_content_without_panel_unchanged():
    assert tw._tool_message_content("+ Bash(ls)") == \
        tw._tool_message_content("+ Bash(ls)", panel="")
    # empty buffer + panel still renders the panel
    out = tw._tool_message_content("", panel=PANEL)
    assert PANEL in out


def _sess(chat_id="999"):
    return {"chat_id": chat_id, "transcript_path": "/tmp/none.jsonl",
            "updater_pid": None, "standalone_msg_id": None,
            "agents": {"k": {"label": "x", "model": None,
                             "status": "running", "started_at": 0.0,
                             "ended_at": None, "tokens": 0,
                             "prompt_sha": "p", "agent_id": None,
                             "_prompt": "x", "transcript": None}}}


def test_publish_off_mode_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("CCDK_AGENT_VIEW_STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(av, "_tools_mode_for", lambda chat_id: "off")
    calls = []
    monkeypatch.setattr(av, "_discord_edit",
                        lambda *a: calls.append(("edit", a)))
    monkeypatch.setattr(av, "_discord_send",
                        lambda *a: calls.append(("send", a)) or "1")
    av.publish_panel("sess1", _sess(), final=False, stale=False)
    assert calls == []


def test_publish_standalone_sends_then_edits(tmp_path, monkeypatch):
    monkeypatch.setenv("CCDK_AGENT_VIEW_STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(av, "_tools_mode_for", lambda chat_id: "ticker")
    monkeypatch.setattr(av, "_bot_token", lambda: "tok")
    monkeypatch.setattr(av, "_narrate_turn_for", lambda sess: (None, None))
    monkeypatch.setattr(av, "_reply_count", lambda path: 0)
    sent, edited = [], []
    monkeypatch.setattr(av, "_discord_send",
                        lambda t, c, m: sent.append(m) or "msg1")
    monkeypatch.setattr(av, "_discord_edit",
                        lambda t, c, mid, m: edited.append((mid, m)) or True)
    sess = _sess()
    state = {"sess1": sess}
    av._save_av_state(state)
    av.publish_panel("sess1", sess, final=False, stale=False)
    assert len(sent) == 1 and "agents ·" in sent[0]
    assert sess["standalone_msg_id"] == "msg1"
    av.publish_panel("sess1", sess, final=False, stale=False)
    assert len(sent) == 1  # no re-send
    assert edited and edited[0][0] == "msg1"


def test_publish_standalone_reposts_on_displacement(tmp_path, monkeypatch):
    monkeypatch.setenv("CCDK_AGENT_VIEW_STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(av, "_tools_mode_for", lambda chat_id: "ticker")
    monkeypatch.setattr(av, "_bot_token", lambda: "tok")
    monkeypatch.setattr(av, "_narrate_turn_for", lambda sess: (None, None))
    sent, deleted = [], []
    monkeypatch.setattr(av, "_discord_send",
                        lambda t, c, m: sent.append(m) or f"m{len(sent)}")
    monkeypatch.setattr(av, "_discord_edit", lambda *a: True)
    monkeypatch.setattr(av, "_discord_delete",
                        lambda t, c, mid: deleted.append(mid) or True)
    sess = _sess()
    av._save_av_state({"sess1": sess})
    monkeypatch.setattr(av, "_reply_count", lambda path: 0)
    av.publish_panel("sess1", sess, final=False, stale=False)
    assert sess["standalone_msg_id"] == "m1"
    # a reply landed below the panel -> repost (cooldown expired)
    monkeypatch.setattr(av, "_reply_count", lambda path: 1)
    sess["standalone_reposted_at"] = 0
    av.publish_panel("sess1", sess, final=False, stale=False)
    assert deleted == ["m1"]
    assert sess["standalone_msg_id"] == "m2"


def test_publish_footer_writes_panel_into_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("CCDK_AGENT_VIEW_STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(av, "_tools_mode_for", lambda chat_id: "diffs")
    monkeypatch.setattr(av, "_bot_token", lambda: "tok")
    turn = {"chat_id": "999", "tool_msg_id": "tm1",
            "tool_buffer": "+ Bash(ls)", "finalized": False}
    monkeypatch.setattr(av, "_narrate_turn_for", lambda sess: ("tk", turn))
    saved = []
    monkeypatch.setattr(av, "_save_narrate_turn",
                        lambda key, t: saved.append((key, dict(t))))
    edited = []
    monkeypatch.setattr(av, "_discord_edit",
                        lambda t, c, mid, m: edited.append((mid, m)) or True)
    sess = _sess()
    av._save_av_state({"sess1": sess})
    av.publish_panel("sess1", sess, final=False, stale=False)
    assert turn["agent_panel"].startswith("```")
    assert saved and saved[0][0] == "tk"
    assert edited and edited[0][0] == "tm1"
    assert edited[0][1].endswith(turn["agent_panel"])


def test_failed_delete_is_retried_not_orphaned(tmp_path, monkeypatch):
    """Displacement repost with a failing delete keeps the old card on
    the sweep list and retries next tick (2026-06-11: failed deletes
    left frozen duplicate cards in the channel)."""
    monkeypatch.setenv("CCDK_AGENT_VIEW_STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(av, "_tools_mode_for", lambda chat_id: "ticker")
    monkeypatch.setattr(av, "_bot_token", lambda: "tok")
    monkeypatch.setattr(av, "_narrate_turn_for", lambda sess: (None, None))
    sent, deleted = [], []
    monkeypatch.setattr(av, "_discord_send",
                        lambda t, c, m: sent.append(m) or f"m{len(sent)}")
    monkeypatch.setattr(av, "_discord_edit", lambda *a: True)
    delete_ok = {"v": False}
    monkeypatch.setattr(av, "_discord_delete",
                        lambda t, c, mid: deleted.append(mid) or delete_ok["v"])
    sess = _sess()
    av._save_av_state({"sess1": sess})
    monkeypatch.setattr(av, "_reply_count", lambda path: 0)
    av.publish_panel("sess1", sess, final=False, stale=False)
    assert sess["standalone_msg_id"] == "m1"
    # displacement; delete FAILS -> m1 stays tracked
    monkeypatch.setattr(av, "_reply_count", lambda path: 1)
    sess["standalone_reposted_at"] = 0
    av.publish_panel("sess1", sess, final=False, stale=False)
    assert sess["standalone_msg_id"] == "m2"
    assert "m1" in sess["panel_cards"]
    # next tick: delete works -> orphan swept
    delete_ok["v"] = True
    av.publish_panel("sess1", sess, final=False, stale=False)
    assert sess["panel_cards"] == ["m2"]


def test_repost_cooldown_limits_churn(tmp_path, monkeypatch):
    monkeypatch.setenv("CCDK_AGENT_VIEW_STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(av, "_tools_mode_for", lambda chat_id: "ticker")
    monkeypatch.setattr(av, "_bot_token", lambda: "tok")
    monkeypatch.setattr(av, "_narrate_turn_for", lambda sess: (None, None))
    sent = []
    monkeypatch.setattr(av, "_discord_send",
                        lambda t, c, m: sent.append(m) or f"m{len(sent)}")
    edited = []
    monkeypatch.setattr(av, "_discord_edit",
                        lambda t, c, mid, m: edited.append(mid) or True)
    monkeypatch.setattr(av, "_discord_delete", lambda *a: True)
    sess = _sess()
    av._save_av_state({"sess1": sess})
    monkeypatch.setattr(av, "_reply_count", lambda path: 0)
    av.publish_panel("sess1", sess, final=False, stale=False)
    assert len(sent) == 1
    # replies climb immediately after creation: cooldown blocks -> EDIT
    monkeypatch.setattr(av, "_reply_count", lambda path: 5)
    av.publish_panel("sess1", sess, final=False, stale=False)
    assert len(sent) == 1
    assert edited
    # cooldown expired -> the displaced card reposts
    sess["standalone_reposted_at"] = 0
    av.publish_panel("sess1", sess, final=False, stale=False)
    assert len(sent) == 2
