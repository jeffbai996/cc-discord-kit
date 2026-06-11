"""Tests for agent_view.py's updater tick logic (pure parts only —
the daemon loop is exercised live)."""
import os
import sys

sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")),
)
import agent_view as av  # noqa: E402


def _running(transcript="/t"):
    return {"label": "x", "model": None, "status": "running",
            "started_at": 0.0, "ended_at": None, "tokens": 0,
            "prompt_sha": "p", "agent_id": "a", "_prompt": "x",
            "transcript": transcript}


def test_tick_updates_tokens_and_model(monkeypatch):
    agents = {"k": _running()}
    monkeypatch.setattr(av, "match_transcripts", lambda d, a: None)
    monkeypatch.setattr(av, "read_transcript_stats",
                        lambda p: {"tokens": 777,
                                   "model": "claude-haiku-4-5",
                                   "last_ts": 50.0})
    av.tick_agents(agents, subagents_dir="/tmp/none", now=60.0)
    assert agents["k"]["tokens"] == 777
    assert agents["k"]["model"] == "claude-haiku-4-5"
    assert agents["k"]["status"] == "running"


def test_tick_marks_silent_running_agent_lost(monkeypatch):
    agents = {"k": _running()}
    monkeypatch.setattr(av, "match_transcripts", lambda d, a: None)
    monkeypatch.setattr(av, "read_transcript_stats",
                        lambda p: {"tokens": 5, "model": "m",
                                   "last_ts": 100.0})
    av.tick_agents(agents, subagents_dir="/tmp/none",
                   now=100.0 + av._LOST_AFTER + 60)
    assert agents["k"]["status"] == "lost"
    assert agents["k"]["ended_at"] == 100.0


def test_tick_does_not_resurrect_done_agent(monkeypatch):
    agents = {"k": dict(_running(), status="done", ended_at=10.0)}
    monkeypatch.setattr(av, "match_transcripts", lambda d, a: None)
    monkeypatch.setattr(av, "read_transcript_stats",
                        lambda p: {"tokens": 5, "model": "m",
                                   "last_ts": 100.0})
    av.tick_agents(agents, subagents_dir="/tmp/none", now=10_000.0)
    assert agents["k"]["status"] == "done"


def test_tick_skips_unlinked_agents(monkeypatch):
    agents = {"k": _running(transcript=None)}
    monkeypatch.setattr(av, "match_transcripts", lambda d, a: None)
    called = []
    monkeypatch.setattr(av, "read_transcript_stats",
                        lambda p: called.append(p))
    av.tick_agents(agents, subagents_dir="/tmp/none", now=1.0)
    assert called == []


def test_all_terminal():
    assert av.all_terminal({"a": {"status": "done"},
                            "b": {"status": "failed"},
                            "c": {"status": "lost"}})
    assert not av.all_terminal({"a": {"status": "running"}})
    assert av.all_terminal({})


def test_subagents_dir_from_transcript_path():
    assert av._subagents_dir("/x/proj/sess-1.jsonl") == \
        "/x/proj/sess-1/subagents"


def test_pid_alive():
    assert av._pid_alive(os.getpid())
    assert not av._pid_alive(None)
    assert not av._pid_alive("not-a-pid")


def test_tick_never_loses_agent_on_stale_transcript(monkeypatch):
    """A linked transcript whose last activity PREDATES the registration
    is a mis-link, not a silent agent — must stay running."""
    agents = {"k": _running()}
    agents["k"]["started_at"] = 10_000.0
    monkeypatch.setattr(av, "match_transcripts", lambda d, a: None)
    monkeypatch.setattr(av, "read_transcript_stats",
                        lambda p: {"tokens": 5, "model": "m",
                                   "last_ts": 100.0})  # long before start
    av.tick_agents(agents, subagents_dir="/tmp/none",
                   now=10_000.0 + av._LOST_AFTER + 60)
    assert agents["k"]["status"] == "running"
