"""Tests for agent_view.py's hook recorder (handle_pre / handle_post)."""
import os
import sys

sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")),
)
import agent_view as av  # noqa: E402


def _payload(tool="Agent", desc="bear case", prompt="Research the bear case",
             session="sess1", resp=None):
    p = {"session_id": session, "transcript_path": "/tmp/x/sess1.jsonl",
         "tool_name": tool,
         "tool_input": {"description": desc, "prompt": prompt,
                        "subagent_type": "Explore"}}
    if resp is not None:
        p["tool_response"] = resp
    return p


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CCDK_AGENT_VIEW_STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(av, "_resolve_chat_id", lambda payload: "111")
    monkeypatch.setattr(av, "_ensure_updater", lambda session: None)
    monkeypatch.setattr(av, "_bot_key", lambda: "testbot")


def test_pre_registers_running_agent(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    av.handle_pre(_payload())
    sess = av._load_av_state()["testbot:sess1"]
    (key, agent), = sess["agents"].items()
    assert agent["label"] == "bear case"
    assert agent["status"] == "running"
    assert agent["_prompt"] == "Research the bear case"
    assert sess["chat_id"] == "111"
    assert sess["transcript_path"] == "/tmp/x/sess1.jsonl"


def test_pre_ignores_other_tools(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    av.handle_pre(_payload(tool="Bash"))
    assert av._load_av_state() == {}


def test_parallel_identical_calls_get_suffixed_keys(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    av.handle_pre(_payload())
    av.handle_pre(_payload())
    keys = list(av._load_av_state()["testbot:sess1"]["agents"])
    assert len(keys) == 2
    assert keys[1] == keys[0] + ":2"


def test_post_marks_done_and_links_agent_id(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    av.handle_pre(_payload())
    av.handle_post(_payload(resp={"agentId": "abc123", "status": "completed"}),
                   failed=False)
    (agent,) = av._load_av_state()["testbot:sess1"]["agents"].values()
    assert agent["status"] == "done"
    assert agent["agent_id"] == "abc123"
    assert agent["ended_at"] is not None


def test_post_failure_marks_failed(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    av.handle_pre(_payload())
    av.handle_post(_payload(resp={}), failed=True)
    (agent,) = av._load_av_state()["testbot:sess1"]["agents"].values()
    assert agent["status"] == "failed"


def test_post_settles_earliest_matching_running_agent(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    av.handle_pre(_payload())
    av.handle_pre(_payload())  # identical parallel call
    av.handle_post(_payload(resp={"agentId": "a1"}), failed=False)
    agents = list(av._load_av_state()["testbot:sess1"]["agents"].values())
    assert [a["status"] for a in agents] == ["done", "running"]


def test_post_without_pre_is_noop(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    av.handle_post(_payload(resp={}), failed=False)
    assert av._load_av_state() == {}


def test_pre_records_explicit_model(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    p = _payload()
    p["tool_input"]["model"] = "haiku"
    av.handle_pre(p)
    (agent,) = av._load_av_state()["testbot:sess1"]["agents"].values()
    assert agent["model"] == "haiku"


def test_post_overrides_provisional_lost(tmp_path, monkeypatch):
    """The harness completion event is ground truth — it settles an
    agent the updater provisionally marked lost."""
    _isolate(tmp_path, monkeypatch)
    av.handle_pre(_payload())
    state = av._load_av_state()
    (agent,) = state["testbot:sess1"]["agents"].values()
    agent["status"] = "lost"
    av._save_av_state(state)
    av.handle_post(_payload(resp={"agentId": "a1"}), failed=False)
    (agent,) = av._load_av_state()["testbot:sess1"]["agents"].values()
    assert agent["status"] == "done"


def test_post_async_launch_keeps_running(tmp_path, monkeypatch):
    """run_in_background: PostToolUse means 'launched', not 'finished' —
    the agent stays running (2026-06-11: a bot's background agents were
    marked done after 2s and the panel froze)."""
    _isolate(tmp_path, monkeypatch)
    av.handle_pre(_payload())
    av.handle_post(_payload(resp={"agentId": "bg1", "isAsync": True,
                                  "status": "async_launched"}),
                   failed=False)
    (agent,) = av._load_av_state()["testbot:sess1"]["agents"].values()
    assert agent["status"] == "running"
    assert agent["async"] is True
    assert agent["agent_id"] == "bg1"
