"""Persona registry tests.

We point CCDK_AGENTS_FILE at a tmp YAML so personas.py never reads
the user's real ~/.config/cc-discord-kit/agents.yaml.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest


def _import_personas():
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    if "personas" in sys.modules:
        del sys.modules["personas"]
    import personas  # noqa: E402
    return personas


@pytest.fixture
def fresh_personas(tmp_path, monkeypatch):
    plain = tmp_path / "plain.md"
    agents_file = tmp_path / "agents.yaml"
    agents_file.write_text(textwrap.dedent(f"""
        agents:
          testbot:
            - {{ slot: plain.md, path: {plain}, mode: plain }}
    """).strip() + "\n")
    monkeypatch.setenv("CCDK_AGENTS_FILE", str(agents_file))
    p = _import_personas()
    p.reset_cache()
    return p, plain


@pytest.fixture
def git_personas(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    tracked = repo / "tracked.md"
    tracked.write_text("seed\n")
    subprocess.run(["git", "add", "tracked.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)

    agents_file = tmp_path / "agents.yaml"
    agents_file.write_text(textwrap.dedent(f"""
        git_repo: {repo}
        agents:
          gitbot:
            - {{ slot: tracked.md, path: {tracked}, mode: git }}
    """).strip() + "\n")
    monkeypatch.setenv("CCDK_AGENTS_FILE", str(agents_file))
    p = _import_personas()
    p.reset_cache()
    return p, repo, tracked


def test_list_bots_and_get_files(fresh_personas):
    personas, _ = fresh_personas
    assert personas.list_bots() == ["testbot"]
    files = personas.get_files("testbot")
    assert len(files) == 1
    assert files[0]["slot"] == "plain.md"
    assert files[0]["mode"] == "plain"


def test_read_missing_file_returns_empty(fresh_personas):
    personas, plain = fresh_personas
    assert not plain.exists()
    data = personas.read_slot("testbot", "plain.md")
    assert data["text"] == ""
    assert data["mtime"] is None
    assert data["mode"] == "plain"


def test_write_then_read_roundtrip(fresh_personas):
    personas, plain = fresh_personas
    result = personas.write_slot("testbot", "plain.md", "hello world\n")
    assert result["ok"] is True
    assert result["committed"] is False
    assert plain.read_text() == "hello world\n"

    data = personas.read_slot("testbot", "plain.md")
    assert data["text"] == "hello world\n"
    assert data["mtime"] is not None


def test_write_atomic_on_existing_file(fresh_personas):
    personas, plain = fresh_personas
    plain.write_text("original\n")
    personas.write_slot("testbot", "plain.md", "replaced\n")
    assert plain.read_text() == "replaced\n"
    # No leftover .tmp.
    assert not (plain.parent / "plain.md.tmp").exists()


def test_unknown_bot_raises(fresh_personas):
    personas, _ = fresh_personas
    with pytest.raises(KeyError):
        personas.read_slot("nope", "plain.md")
    with pytest.raises(KeyError):
        personas.read_slot("testbot", "nope.md")


def test_git_write_commits(git_personas):
    personas, repo, tracked = git_personas
    result = personas.write_slot("gitbot", "tracked.md", "edited\n")
    assert result["ok"] is True
    assert result["committed"] is True
    assert result["sha"]
    assert result["error"] is None

    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    assert log.stdout.strip() == "personas: update gitbot tracked.md"


def test_git_write_idempotent(git_personas):
    """Writing identical content shouldn't create an empty commit."""
    personas, repo, tracked = git_personas
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    result = personas.write_slot("gitbot", "tracked.md", "seed\n")
    assert result["committed"] is True  # path was clean → returns existing sha
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert before == after


# ─────────────────────────── gap coverage ───────────────────────────


def test_resolve_returns_path_and_mode(fresh_personas):
    personas, plain = fresh_personas
    path, mode = personas._resolve("testbot", "plain.md")
    assert path == str(plain)
    assert mode == "plain"


def test_resolve_unknown_bot_and_slot_raise(fresh_personas):
    personas, _ = fresh_personas
    with pytest.raises(KeyError):
        personas._resolve("ghost", "plain.md")
    with pytest.raises(KeyError):
        personas._resolve("testbot", "missing-slot.md")


def test_get_agent_meta_none_and_unknown_return_empty(fresh_personas):
    personas, _ = fresh_personas
    assert personas.get_agent_meta(None) == {}
    assert personas.get_agent_meta("testbot") == {}  # no agent_meta block in fixture


def test_get_agent_meta_reads_block(tmp_path, monkeypatch):
    plain = tmp_path / "plain.md"
    agents_file = tmp_path / "agents.yaml"
    agents_file.write_text(textwrap.dedent(f"""
        agents:
          metabot:
            - {{ slot: plain.md, path: {plain}, mode: plain }}
        agent_meta:
          metabot:
            discord_home_channel: "999000"
    """).strip() + "\n")
    monkeypatch.setenv("CCDK_AGENTS_FILE", str(agents_file))
    personas = _import_personas()
    personas.reset_cache()
    meta = personas.get_agent_meta("metabot")
    assert meta["discord_home_channel"] == "999000"


def test_empty_config_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CCDK_AGENTS_FILE", str(tmp_path / "nope.yaml"))
    personas = _import_personas()
    personas.reset_cache()
    assert personas.list_bots() == []


def test_git_write_no_repo_configured(fresh_personas, monkeypatch):
    """mode=git slot but git_repo unset → file written, committed False, error set."""
    personas, _ = fresh_personas
    # Patch _resolve to report git mode for the plain slot, and force empty repo.
    orig_resolve = personas._resolve
    monkeypatch.setattr(
        personas, "_resolve",
        lambda bot, slot: (orig_resolve(bot, slot)[0], "git"),
    )
    monkeypatch.setattr(personas, "_git_repo", lambda: "")
    result = personas.write_slot("testbot", "plain.md", "content")
    assert result["ok"] is True
    assert result["committed"] is False
    assert "no git_repo" in result["error"]


def test_git_write_commit_failure_still_writes_file(git_personas, monkeypatch):
    personas, repo, tracked = git_personas

    def boom(repo_, path, msg):
        raise subprocess.CalledProcessError(1, "git", stderr="commit failed")

    monkeypatch.setattr(personas, "_git_commit", boom)
    result = personas.write_slot("gitbot", "tracked.md", "new body\n")
    # File write succeeds independently of the commit failure.
    assert tracked.read_text() == "new body\n"
    assert result["committed"] is False
    assert "commit failed" in result["error"]
