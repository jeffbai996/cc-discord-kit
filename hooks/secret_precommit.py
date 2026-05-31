#!/usr/bin/env python3
"""Pre-commit secret + PII scanner. Blocks commits that leak credentials
or personal data.

Reads the staged diff, scans for known leak patterns, prints any matches with
file:line, and exits nonzero if anything matches. Install it as a git
pre-commit hook in any repo you care about:

    ln -s "$(pwd)/hooks/secret_precommit.py" .git/hooks/pre-commit

Two pattern classes with DIFFERENT allowlist semantics:

  SECRET_PATTERNS — API keys, tokens, credentials. These are scanned in
  EVERY repo, including allowlisted ones. A secret in a "private" repo is
  still a live credential sitting in version control, replicated to every
  box that clones it, one accidental publish (or fork, or visibility flip)
  away from exposure. Private is not a reason to commit a secret. The
  allowlist does NOT exempt these.

  PII_PATTERNS — identity PII (your name, addresses, internal hostnames,
  private codenames, etc.). These are about keeping YOUR identity out of
  PUBLIC repos. A private repo legitimately references internal infra, so
  the allowlist DOES exempt these. This kit ships an EMPTY PII default set
  plus a commented example — populate it with your own patterns via
  patterns.json (see below), or leave it empty to scan secrets only.

Allowlist:
  Repos can opt out of the PII check by name in
  ~/.config/cc-discord-kit/allow.json:
    { "allowed_repos": ["my-private-repo", "another-one"] }
  Allowed repos skip PII_PATTERNS but are STILL scanned for SECRET_PATTERNS.

Override (single commit):
  CCDK_PII_OVERRIDE=1 git commit ...     bypasses the check entirely
  with a stderr warning. Use sparingly; the rule exists for a reason.

Patterns are loaded from ~/.config/cc-discord-kit/patterns.json if present:
    {
      "patterns": [
        {"label": "my_real_name", "regex": "\\bJane\\s+Doe\\b"},
        {"label": "home_address", "regex": "\\b123\\s+Main\\s+St\\b"}
      ],
      "secret_patterns": [
        {"label": "my_internal_token", "regex": "INT-[0-9A-Z]{20}"}
      ]
    }
  - `patterns`        REPLACES the (empty) built-in PII defaults.
  - `secret_patterns` EXTENDS the built-in secret patterns (can't disable them).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "cc-discord-kit"
PATTERNS_FILE = CONFIG_DIR / "patterns.json"
ALLOW_FILE = CONFIG_DIR / "allow.json"

# Secret / credential patterns. UNLIKE the PII patterns below, these are
# scanned in EVERY repo regardless of the allowlist — a live credential in
# any git history is a defect, public or private. Catching the *provider*
# prefix is the reliable signal; the trailing generic catch-all handles
# `api_key = "..."` / `secret: ...` assignments that don't carry a prefix.
SECRET_PATTERNS: list[tuple[str, str]] = [
    # Provider-prefixed keys. Trailing negative-lookahead (not another key
    # char) instead of \b so a real fixed-length key matches even when the
    # surrounding token has separators.
    ("google_api_key",   r"AIza[0-9A-Za-z_\-]{35}(?![0-9A-Za-z_\-])"),
    ("google_oauth",     r"GOCSPX-[0-9A-Za-z_\-]{20,}"),
    ("openai_key",       r"\bsk-(?:proj-)?[0-9A-Za-z_\-]{20,}"),
    ("anthropic_key",    r"\bsk-ant-[0-9A-Za-z_\-]{20,}"),
    ("github_token",     r"\b(?:ghp|gho|ghu|ghs|ghr)_[0-9A-Za-z]{36}\b"),
    ("github_pat_fine",  r"\bgithub_pat_[0-9A-Za-z_]{22,}\b"),
    ("slack_token",      r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b"),
    ("aws_access_key",   r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("discord_bot_token", r"\b[MN][\w\-]{23}\.[\w\-]{6}\.[\w\-]{27,}\b"),
    ("private_key_block", r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    # Generic "<...>key/secret/token = <long value>" assignment. The name
    # may carry a prefix (GEMINI_API_KEY, DISCORD_BOT_TOKEN), so the key
    # word is preceded by a non-alnum boundary OR a word char — i.e. just
    # require it ends a name token. Needs a real-looking value (16+ chars).
    # Skips obvious placeholders so runbooks using <your-key> don't trip.
    ("generic_secret_assignment",
     r"(?i)(?:api[_-]?key|secret|token|password|passwd|client[_-]?secret)"
     r"['\"]?\s*[:=]\s*"
     r"['\"]?(?!your[_-]|<|\.\.\.|placeholder|example|xxx|changeme|redacted|none\b|null\b|true\b|false\b)"
     r"[0-9A-Za-z_\-./+]{16,}"),
]


# Identity-PII patterns. This kit ships an EMPTY default set — the secret
# scanner above runs everywhere, but PII is necessarily personal, so you
# supply your own. Add patterns by writing ~/.config/cc-discord-kit/patterns.json
# with a `patterns` key (see the module docstring), which REPLACES this list.
#
# Example of what you might put there (commented out — do NOT hardcode your
# own identity into a public repo's source; load it from patterns.json):
#
#   PII_PATTERNS = [
#       ("real_name",      r"\bJane\s+Doe\b"),
#       ("email",          r"jane\.doe@example\.com"),
#       ("home_address",   r"\b123\s+Main\s+St\b"),
#       ("internal_host",  r"\bmy-private-host\b"),
#       ("project_codename", r"\bprivate-project-name\b"),
#   ]
PII_PATTERNS: list[tuple[str, str]] = []


def _compile(raw: list[tuple[str, str]]) -> list[tuple[str, re.Pattern]]:
    return [(label, re.compile(rx)) for label, rx in raw]


def load_secret_patterns() -> list[tuple[str, re.Pattern]]:
    """Secret patterns. Optionally extended (not replaced) by patterns.json's
    `secret_patterns` key, so a custom file can't accidentally disable the
    built-in credential coverage."""
    raw = list(SECRET_PATTERNS)
    if PATTERNS_FILE.exists():
        try:
            data = json.loads(PATTERNS_FILE.read_text())
            raw += [(item["label"], item["regex"]) for item in data.get("secret_patterns", [])]
        except (json.JSONDecodeError, KeyError, OSError) as e:
            print(f"cc-discord-kit: failed to load {PATTERNS_FILE}: {e}", file=sys.stderr)
    return _compile(raw)


def load_patterns() -> list[tuple[str, re.Pattern]]:
    """Identity-PII patterns. A patterns.json `patterns` key REPLACES the
    (empty) defaults."""
    raw = PII_PATTERNS
    if PATTERNS_FILE.exists():
        try:
            data = json.loads(PATTERNS_FILE.read_text())
            if data.get("patterns"):
                raw = [(item["label"], item["regex"]) for item in data["patterns"]]
        except (json.JSONDecodeError, KeyError, OSError) as e:
            print(f"cc-discord-kit: failed to load {PATTERNS_FILE}: {e}", file=sys.stderr)
            print("cc-discord-kit: using built-in defaults", file=sys.stderr)
    return _compile(raw)


def is_allowed_repo(repo_name: str) -> bool:
    if not ALLOW_FILE.exists():
        return False
    try:
        data = json.loads(ALLOW_FILE.read_text())
        return repo_name in (data.get("allowed_repos") or [])
    except (json.JSONDecodeError, OSError):
        return False


def get_repo_name() -> str:
    """Return basename of git toplevel."""
    try:
        toplevel = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return os.path.basename(toplevel)
    except subprocess.CalledProcessError:
        return ""


def get_staged_diff() -> str:
    """Return only the *added* lines from the staged diff.

    We scan only added content — modifying a file that already had a leak
    shouldn't fail the new commit (separate cleanup PR for that).
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "-U0", "--no-color"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        print(f"cc-discord-kit: git diff failed: {e.stderr}", file=sys.stderr)
        return ""


def scan_diff(diff: str, patterns: list[tuple[str, re.Pattern]]) -> list[dict]:
    """Walk staged diff hunks; return matches as {file, line, label, snippet}.

    Diff lines starting with '+' are new content. We track current file via
    `+++ b/<path>` markers and current line number via `@@ ... +<line>,n @@`.
    """
    findings: list[dict] = []
    current_file = ""
    current_lineno = 0

    file_re = re.compile(r"^\+\+\+ b/(.+)$")
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    for line in diff.splitlines():
        m = file_re.match(line)
        if m:
            current_file = m.group(1)
            current_lineno = 0
            continue
        m = hunk_re.match(line)
        if m:
            current_lineno = int(m.group(1)) - 1
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            current_lineno += 1
            content = line[1:]
            for label, pat in patterns:
                if pat.search(content):
                    findings.append({
                        "file": current_file,
                        "line": current_lineno,
                        "label": label,
                        "snippet": content.strip()[:120],
                    })
        elif line.startswith(" "):
            current_lineno += 1
        # '-' lines and meta lines don't advance new-file lineno
    return findings


def main() -> int:
    if os.environ.get("CCDK_PII_OVERRIDE") == "1":
        print("cc-discord-kit: bypassed via CCDK_PII_OVERRIDE=1", file=sys.stderr)
        return 0

    diff = get_staged_diff()
    if not diff:
        return 0

    repo = get_repo_name()
    allowed = is_allowed_repo(repo)

    # Secrets are scanned in EVERY repo — the allowlist never exempts a
    # credential. PII is scanned only when the repo isn't allowlisted.
    secret_findings = scan_diff(diff, load_secret_patterns())
    pii_findings = [] if allowed else scan_diff(diff, load_patterns())
    if allowed and not secret_findings:
        print(f"cc-discord-kit: PII skipped (allowed repo: {repo}); no secrets found",
              file=sys.stderr)
        return 0

    findings = secret_findings + pii_findings
    if not findings:
        return 0

    has_secret = bool(secret_findings)
    kind = "SECRETS" if has_secret and not pii_findings else (
        "SECRETS + personal data" if has_secret else "personal data")
    print("=" * 70, file=sys.stderr)
    print(f"cc-discord-kit: BLOCKED — staged diff contains {kind}", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    for f in secret_findings:
        print(f"  [SECRET] {f['file']}:{f['line']}  [{f['label']}]", file=sys.stderr)
        print(f"    > {f['snippet']}", file=sys.stderr)
    for f in pii_findings:
        print(f"  [PII]    {f['file']}:{f['line']}  [{f['label']}]", file=sys.stderr)
        print(f"    > {f['snippet']}", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"  {len(findings)} match(es) across "
          f"{len({f['file'] for f in findings})} file(s)", file=sys.stderr)
    print("", file=sys.stderr)
    if has_secret:
        print("  A credential must NEVER be committed — not even to a private repo.",
              file=sys.stderr)
        print("  Move it to a gitignored .env / secret manager and reference it by name.",
              file=sys.stderr)
        print("", file=sys.stderr)
    print("To bypass for this single commit (use sparingly):", file=sys.stderr)
    print("  CCDK_PII_OVERRIDE=1 git commit ...", file=sys.stderr)
    if not has_secret:
        print("", file=sys.stderr)
        print("To allowlist this repo for PII permanently, add its name to:", file=sys.stderr)
        print(f"  {ALLOW_FILE}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
