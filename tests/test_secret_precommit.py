"""Tests for hooks/secret_precommit.py — the secret/PII diff scanner.

Focus on scan_diff (the pure logic) and the secret-pattern coverage. The PII
default set ships EMPTY in the OSS kit, so we assert that — and that secret
patterns still fire regardless.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")),
)
import secret_precommit as sp  # noqa: E402


def _diff(path: str, *added_lines: str) -> str:
    """Build a minimal staged-diff string with the given added lines."""
    body = "\n".join("+" + ln for ln in added_lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(added_lines)} @@\n"
        f"{body}\n"
    )


def test_pii_defaults_are_empty():
    # OSS kit must NOT ship hardcoded identity patterns.
    assert sp.PII_PATTERNS == []


def test_secret_patterns_present():
    labels = {label for label, _ in sp.SECRET_PATTERNS}
    assert "anthropic_key" in labels
    assert "github_token" in labels
    assert "generic_secret_assignment" in labels


def test_scan_detects_anthropic_key():
    diff = _diff("config.py", 'API_KEY = "sk-ant-' + "a" * 30 + '"')
    findings = sp.scan_diff(diff, sp.load_secret_patterns())
    labels = {f["label"] for f in findings}
    assert "anthropic_key" in labels


def test_scan_detects_aws_key():
    diff = _diff("creds.txt", "AKIA" + "A" * 16)
    findings = sp.scan_diff(diff, sp.load_secret_patterns())
    assert any(f["label"] == "aws_access_key" for f in findings)


def test_scan_detects_private_key_block():
    diff = _diff("id_rsa", "-----BEGIN OPENSSH PRIVATE KEY-----")
    findings = sp.scan_diff(diff, sp.load_secret_patterns())
    assert any(f["label"] == "private_key_block" for f in findings)


def test_generic_secret_assignment_skips_placeholders():
    diff = _diff("README.md", 'token = "<your-token-here>"')
    findings = sp.scan_diff(diff, sp.load_secret_patterns())
    assert findings == []


def test_clean_diff_has_no_findings():
    diff = _diff("app.py", "def hello():", "    return 'world'")
    findings = sp.scan_diff(diff, sp.load_secret_patterns())
    assert findings == []


def test_scan_tracks_file_and_line():
    diff = _diff("a.py", "harmless", 'key = "sk-ant-' + "z" * 30 + '"')
    findings = sp.scan_diff(diff, sp.load_secret_patterns())
    assert findings
    f = findings[0]
    assert f["file"] == "a.py"
    assert f["line"] == 2  # second added line


def test_removed_lines_not_scanned():
    # A '-' line carrying a secret should be ignored (only added content scanned).
    diff = (
        "diff --git a/x b/x\n--- a/x\n+++ b/x\n"
        "@@ -1,1 +0,0 @@\n"
        '-API_KEY = "sk-ant-' + "q" * 30 + '"\n'
    )
    findings = sp.scan_diff(diff, sp.load_secret_patterns())
    assert findings == []


def test_load_patterns_replaces_defaults_from_file(tmp_path, monkeypatch):
    pf = tmp_path / "patterns.json"
    pf.write_text('{"patterns": [{"label": "custom", "regex": "SECRET-NAME"}]}')
    monkeypatch.setattr(sp, "PATTERNS_FILE", pf)
    pats = sp.load_patterns()
    labels = {label for label, _ in pats}
    assert labels == {"custom"}


def test_load_secret_patterns_extends_not_replaces(tmp_path, monkeypatch):
    pf = tmp_path / "patterns.json"
    pf.write_text('{"secret_patterns": [{"label": "extra", "regex": "EXTRA-KEY"}]}')
    monkeypatch.setattr(sp, "PATTERNS_FILE", pf)
    labels = {label for label, _ in sp.load_secret_patterns()}
    assert "extra" in labels
    assert "anthropic_key" in labels  # builtins still present
