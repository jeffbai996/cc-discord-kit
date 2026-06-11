"""Tests for agent_view.py's panel renderer and formatting helpers."""
import os
import sys

sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")),
)
import agent_view as av  # noqa: E402


def _agent(label, model="claude-sonnet-4-6", status="running", started=1000.0,
           ended=None, tokens=0):
    return {"label": label, "model": model, "status": status,
            "started_at": started, "ended_at": ended, "tokens": tokens}


def test_model_alias_shortens_full_ids():
    assert av.model_alias("claude-sonnet-4-6") == "sonnet"
    assert av.model_alias("claude-haiku-4-5-20251001") == "haiku"
    assert av.model_alias("claude-opus-4-8") == "opus"
    assert av.model_alias("claude-fable-5") == "fable"
    assert av.model_alias(None) == "?"
    # bare aliases (PreToolUse tool_input.model) pass through
    assert av.model_alias("sonnet") == "sonnet"


def test_fmt_elapsed():
    assert av.fmt_elapsed(31) == "31s"
    assert av.fmt_elapsed(72) == "1m12s"
    assert av.fmt_elapsed(120) == "2m"
    assert av.fmt_elapsed(3725) == "1h2m"


def test_fmt_tokens():
    assert av.fmt_tokens(0) == "0"
    assert av.fmt_tokens(950) == "950"
    assert av.fmt_tokens(12345) == "12.3k"
    assert av.fmt_tokens(1_200_000) == "1.2M"


def test_render_panel_header_and_rows():
    agents = [
        _agent("bear-case:capex", tokens=12345),
        _agent("verify:margin", model="claude-haiku-4-5-20251001",
               status="done", started=1000.0, ended=1031.0, tokens=4200),
    ]
    out = av.render_panel("mybot", agents, now=1072.0)
    lines = out.splitlines()
    assert lines[0] == "```"
    assert lines[1] == "agents · mybot · 1 running · 1 done · 16.5k tok"
    assert lines[2] == ""  # blank spacer row
    assert lines[3].lstrip().startswith("○")
    assert "bear-case:capex" in lines[3]
    assert "sonnet" in lines[3] and "1m12s" in lines[3] and "12.3k" in lines[3]
    assert lines[4].lstrip().startswith("●")
    assert "31s" in lines[4] and "4.2k" in lines[4]
    assert lines[-1] == "```"


def test_render_panel_failed_glyph_and_alignment():
    agents = [
        _agent("a", status="failed", ended=1010.0, tokens=10),
        _agent("longer-label-here", status="done", ended=1020.0, tokens=20),
    ]
    out = av.render_panel("bot", agents, now=1030.0)
    rows = out.splitlines()[3:-1]
    assert rows[0].lstrip().startswith("✗")
    # fixed-width: the model column starts at the same index in every row
    idx = [r.index("sonnet") for r in rows]
    assert len(set(idx)) == 1


def test_render_panel_running_uses_now_for_elapsed():
    out = av.render_panel("bot", [_agent("x", started=100.0)], now=172.0)
    assert "1m12s" in out


def test_render_panel_truncates_to_char_budget():
    agents = [_agent(f"agent-{i:02d}", status="done", ended=1010.0)
              for i in range(40)]
    out = av.render_panel("bot", agents, now=1030.0, max_chars=400)
    assert len(out) <= 400
    assert "more" in out  # dropped done-rows are summarized


def test_render_panel_empty_agents():
    out = av.render_panel("bot", [], now=0.0)
    assert "0 running" in out
    assert "(none)" in out


def test_render_panel_spinner_frames_cycle():
    agents = [_agent("x")]
    h0 = av.render_panel("bot", agents, now=1.0, spinner_frame=0).splitlines()[1]
    h1 = av.render_panel("bot", agents, now=1.0, spinner_frame=1).splitlines()[1]
    h4 = av.render_panel("bot", agents, now=1.0, spinner_frame=4).splitlines()[1]
    assert h0 != h1          # frame advances -> header visibly changes
    assert h0 == h4          # cycle of 4
    assert h0.split()[1] == "agents"  # glyph sits left of "agents"


def test_render_panel_no_spinner_when_frame_none():
    out = av.render_panel("bot", [_agent("x", status="done", ended=2.0)],
                          now=3.0)
    assert out.splitlines()[1].startswith("agents ·")
