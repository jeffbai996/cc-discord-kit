"""Tests for the !agents reserved passthrough command."""
import os
import sys

sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")),
)
import discord_passthrough as dp  # noqa: E402


def test_parse_recognizes_agents_as_bash_mode(monkeypatch):
    monkeypatch.setattr(dp, "get_owner_id", lambda: "42")
    prompt = ('<channel source="plugin:discord:discord" chat_id="1" '
              'message_id="2" user="jeff" user_id="42">!agents</channel>')
    parsed = dp.parse_passthrough(prompt)
    assert parsed is not None
    assert parsed["mode"] == "bash"
    assert parsed["cmd"].strip() == "agents"


def test_reserved_detector():
    assert dp.is_reserved_agents_cmd("agents")
    assert dp.is_reserved_agents_cmd("agent")
    assert dp.is_reserved_agents_cmd("AGENTS")
    assert dp.is_reserved_agents_cmd("agents sess-id")
    assert not dp.is_reserved_agents_cmd("agentsmith --help")
    assert not dp.is_reserved_agents_cmd("ls agents")


def test_agents_command_bypasses_shell_empty_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CCDK_AGENT_VIEW_STATE", str(tmp_path / "empty.json"))
    out, code = dp.run_reserved_agents("")
    assert code == 0
    assert "no agents" in out.lower()
    assert out.startswith("```")  # always fenced for Discord


def test_agents_command_renders_live_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CCDK_AGENT_VIEW_STATE", str(tmp_path / "s.json"))
    import agent_view as av
    monkeypatch.setattr(av, "_bot_key", lambda: "testbot")
    av._save_av_state({"testbot:sess1": {
        "chat_id": "1", "transcript_path": "/t", "updater_pid": None,
        "standalone_msg_id": None,
        "agents": {"k": {"label": "research", "model": "claude-haiku-4-5",
                         "status": "running", "started_at": 0.0,
                         "ended_at": None, "tokens": 1500,
                         "prompt_sha": "p", "agent_id": None,
                         "_prompt": "x", "transcript": None}}}})
    out, code = dp.run_reserved_agents("")
    assert code == 0
    assert "research" in out
    assert "haiku" in out
    assert "1.5k" in out


def test_agents_snapshot_does_not_bleed_across_bots(tmp_path, monkeypatch):
    """Regression: fragserv bots share the state file — one bot's !agents
    must never serve another bot's registry (2026-06-11, Fraggy showed
    another bot's demo agents)."""
    monkeypatch.setenv("CCDK_AGENT_VIEW_STATE", str(tmp_path / "s.json"))
    import agent_view as av
    other = {"chat_id": "1", "transcript_path": "/t", "updater_pid": None,
             "standalone_msg_id": None,
             "agents": {"k": {"label": "other-bots-secret-job",
                              "model": "m", "status": "running",
                              "started_at": 0.0, "ended_at": None,
                              "tokens": 5, "prompt_sha": "p",
                              "agent_id": None, "_prompt": "x",
                              "transcript": None}}}
    av._save_av_state({"otherbot:sess9": other})
    monkeypatch.setattr(av, "_bot_key", lambda: "testbot")
    out, code = dp.run_reserved_agents("")
    assert code == 0
    assert "other-bots-secret-job" not in out
    assert "no agents" in out.lower()
