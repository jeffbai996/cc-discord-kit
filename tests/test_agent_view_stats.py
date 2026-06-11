"""Tests for agent_view.py's subagent-transcript stats + matching."""
import json
import os
import sys

sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")),
)
import agent_view as av  # noqa: E402


def _write_subagent(dirpath, agent_id, prompt, models_tokens):
    """models_tokens: list of (model, output_tokens) assistant records."""
    p = dirpath / f"agent-{agent_id}.jsonl"
    recs = [{"agentId": agent_id, "type": "user",
             "timestamp": "2026-06-11T20:00:00.000Z",
             "message": {"role": "user", "content": prompt}}]
    for i, (model, tok) in enumerate(models_tokens):
        recs.append({"agentId": agent_id, "type": "assistant",
                     "timestamp": f"2026-06-11T20:00:{10 + i:02d}.000Z",
                     "message": {"role": "assistant", "model": model,
                                 "usage": {"output_tokens": tok}}})
    p.write_text("\n".join(json.dumps(r) for r in recs))
    return p


def test_read_stats_sums_output_tokens_and_takes_model(tmp_path):
    f = _write_subagent(tmp_path, "abc", "do the thing",
                        [("claude-haiku-4-5-20251001", 100),
                         ("claude-haiku-4-5-20251001", 250)])
    stats = av.read_transcript_stats(str(f))
    assert stats["tokens"] == 350
    assert stats["model"] == "claude-haiku-4-5-20251001"
    assert stats["last_ts"] > 0


def test_read_stats_tolerates_partial_tail_line(tmp_path):
    f = _write_subagent(tmp_path, "abc", "p", [("m", 10)])
    with open(f, "a") as fh:
        fh.write('\n{"agentId": "abc", "type": "assist')  # mid-write tail
    stats = av.read_transcript_stats(str(f))
    assert stats["tokens"] == 10


def test_read_stats_missing_file():
    stats = av.read_transcript_stats("/nonexistent/agent-x.jsonl")
    assert stats == {"tokens": 0, "model": None, "last_ts": 0.0}


def _reg(desc, prompt):
    return {"prompt_sha": av._agent_key({"description": desc,
                                         "prompt": prompt}),
            "agent_id": None, "transcript": None, "_prompt": prompt[:200],
            "status": "running"}


def test_match_transcripts_links_pending_agents_by_prompt(tmp_path):
    _write_subagent(tmp_path, "a1", "PROMPT-ONE rest of it", [("m", 1)])
    _write_subagent(tmp_path, "a2", "PROMPT-TWO rest of it", [("m", 2)])
    agents = {"k1": _reg("d1", "PROMPT-ONE rest of it"),
              "k2": _reg("d2", "PROMPT-TWO rest of it")}
    av.match_transcripts(str(tmp_path), agents)
    assert agents["k1"]["agent_id"] == "a1"
    assert agents["k1"]["transcript"].endswith("agent-a1.jsonl")
    assert agents["k2"]["agent_id"] == "a2"


def test_match_transcripts_agent_id_wins_over_prompt(tmp_path):
    _write_subagent(tmp_path, "a1", "SAME PROMPT", [("m", 1)])
    _write_subagent(tmp_path, "a2", "SAME PROMPT", [("m", 2)])
    a = _reg("d", "SAME PROMPT")
    a["agent_id"] = "a2"  # already linked by PostToolUse
    agents = {"k": a}
    av.match_transcripts(str(tmp_path), agents)
    assert agents["k"]["transcript"].endswith("agent-a2.jsonl")


def test_match_transcripts_does_not_double_claim(tmp_path):
    _write_subagent(tmp_path, "a1", "SAME PROMPT", [("m", 1)])
    # two identical registrations, one file: only one gets linked
    agents = {"k1": _reg("d", "SAME PROMPT"), "k2": _reg("d", "SAME PROMPT")}
    av.match_transcripts(str(tmp_path), agents)
    linked = [k for k, a in agents.items() if a["transcript"]]
    assert len(linked) == 1


def test_match_transcripts_no_dir():
    agents = {"k": _reg("d", "p")}
    av.match_transcripts("/nonexistent/dir", agents)  # no raise
    assert agents["k"]["transcript"] is None
