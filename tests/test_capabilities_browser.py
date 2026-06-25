"""Tests for browser/computer-use capability detection in the capability matrix.

The `browser` feature is MCP-based (a `playwright` server in .claude.json), not
hook-based like the others — so it has its own probe. These cover: detected when
the MCP is present, absent when it isn't, and no crash when .claude.json is
missing or unreadable. settings.json / .claude.json are written into a tmp config
dir and CLAUDE_CONFIG_DIR is pointed at it."""
import json
import os
import sys

sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
)
import capabilities as cap  # noqa: E402


def _cfg(tmp_path, *, mcp=None, hooks=None):
    """Write a config dir with optional .claude.json mcpServers + settings.json
    hooks, point CLAUDE_CONFIG_DIR at it, return the path."""
    d = tmp_path / "cfg"
    d.mkdir()
    if mcp is not None:
        (d / ".claude.json").write_text(json.dumps({"mcpServers": mcp}))
    if hooks is not None:
        (d / "settings.json").write_text(json.dumps({"hooks": hooks}))
    return str(d)


def test_browser_feature_detected_from_playwright_mcp(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, mcp={"playwright": {"command": "bash", "args": ["-c", "x"]}})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", cfg)
    rec = cap.collect_self_capabilities()
    assert "browser" in rec["detected_features"]


def test_no_browser_feature_without_playwright_mcp(tmp_path, monkeypatch):
    # An .claude.json with other MCPs but no playwright → no browser feature.
    cfg = _cfg(tmp_path, mcp={"vecgrep": {"url": "http://x"}})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", cfg)
    rec = cap.collect_self_capabilities()
    assert "browser" not in rec["detected_features"]


def test_browser_detection_survives_missing_claude_json(tmp_path, monkeypatch):
    # No .claude.json at all → no crash, no browser feature.
    cfg = _cfg(tmp_path, hooks={})  # settings.json only, no .claude.json
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", cfg)
    rec = cap.collect_self_capabilities()
    assert "browser" not in rec["detected_features"]


def test_browser_detection_survives_corrupt_claude_json(tmp_path, monkeypatch):
    d = tmp_path / "cfg"
    d.mkdir()
    (d / ".claude.json").write_text("{not valid json")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    rec = cap.collect_self_capabilities()  # must not raise
    assert "browser" not in rec["detected_features"]
