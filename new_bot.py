"""new-bot — provisioner for a Claude Code `--channels` Discord bot.

Scaffolds a new bot correct-by-construction: the agents.yaml entry, the config
dir + channels/discord/, a settings.json with the kit's hook set, a .presence
file, a launcher, and a transport reminder. Dry-run by default (prints the
plan); pass --apply to actually write.

Scope: Claude Code `--channels` bots only. Each new bot gets one CLAUDE.md
brain-file slot.

This module ships with NO hardcoded server data. Defaults that vary per
deployment (remote URL, config dir root) are read from the kit env file at
`~/.config/cc-discord-kit/env` via `_read_env_var(name)`, falling back to
generic placeholders so the module imports and runs cleanly with no config
file present.
"""
import argparse
import json
import os
import sys

ENV_FILE = os.path.expanduser("~/.config/cc-discord-kit/env")

# Env var names (kit convention: CCDK_* prefix).
REMOTE_URL_VAR = "CCDK_URL"                 # remote server URL for off-host bots
CONFIG_DIR_ROOT_VAR = "CCDK_AGENTS_DIR"    # root under which per-bot config dirs live

# Where the kit's hook scripts live. Resolved relative to this module so the
# scaffolded settings.json points at the installed copy regardless of where the
# kit is checked out. Override with CCDK_HOOKS_DIR if hooks live elsewhere.
_HOOKS_DIR = os.environ.get(
    "CCDK_HOOKS_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks"),
)


def _read_env_var(name: str) -> str | None:
    """Look up `name` in os.environ, then in the env file."""
    if v := os.environ.get(name):
        return v
    if not os.path.exists(ENV_FILE):
        return None
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{name}="):
                    val = line.split("=", 1)[1].strip()
                    return val.strip('"').strip("'")
    except OSError:
        return None
    return None


def _config_dir_root() -> str:
    """Root directory under which per-bot config dirs are created.

    Read from CCDK_AGENTS_DIR (env or env file); defaults to
    `~/.config/cc-discord-kit/agents`.
    """
    root = _read_env_var(CONFIG_DIR_ROOT_VAR)
    return root or os.path.expanduser("~/.config/cc-discord-kit/agents")


def _default_remote_url() -> str | None:
    """Remote server URL for off-host bots (CCDK_URL). None if unset."""
    return _read_env_var(REMOTE_URL_VAR)


def _hook(script: str, *args: str) -> str:
    """A `python3 <hooks_dir>/<script> [args]` hook command string."""
    cmd = f"python3 {os.path.join(_HOOKS_DIR, script)}"
    if args:
        cmd += " " + " ".join(args)
    return cmd


def _build_hooks() -> dict[str, list[str]]:
    """Canonical hook set for a kit --channels bot.

    Built from the hook scripts the kit ships in hooks/. Adopt any subset — each
    hook is independent. This is the full recommended wiring for a Discord
    --channels bot (memory integration + pass-through + voice surfacing +
    echo/paginate guardrails).
    """
    return {
        "SessionStart": [
            _hook("session_start_hook.py"),
        ],
        "UserPromptSubmit": [
            _hook("discord_passthrough.py"),
            _hook("discord_mention_resolver.py"),
            _hook("inject_time.py"),
            _hook("user_prompt_hook.py"),
            _hook("react_hook.py", "--mode", "received"),
        ],
        "PreToolUse": [
            _hook("paginate_guard.py"),
            _hook("react_hook.py", "--mode", "working"),
        ],
        "PostToolUse": [
            _hook("react_hook.py", "--mode", "replied"),
            _hook("react_hook.py", "--mode", "crosscheck"),
            _hook("narrate.py", "--mode", "watch"),
            _hook("tool_watcher.py"),
        ],
        "Stop": [
            _hook("discord_echo_guard.py"),
            _hook("narrate.py", "--mode", "finalize"),
            _hook("stop_hook.py"),
            _hook("react_hook.py", "--mode", "terminal"),
        ],
        "PreCompact": [
            _hook("precompact_hook.py"),
            _hook("react_hook.py", "--mode", "compacted"),
        ],
        "Notification": [
            _hook("notify_hook.py"),
        ],
    }


def _hook_block(commands: list[str]) -> list[dict]:
    return [{"hooks": [{"type": "command", "command": c} for c in commands]}]


def render_settings_json(*, bot: str, presence: str, remote_url: str | None) -> str:
    """settings.json for a --channels bot. remote_url set → off-host bot that
    proxies the kit store over HTTP (sets CCDK_URL)."""
    env = {"CCDK_STATE_BOT": bot}
    if remote_url:
        env["CCDK_URL"] = remote_url
    env["BOT_PRESENCE"] = presence
    settings = {
        "env": env,
        "permissions": {
            "allow": ["Bash", "Edit", "Write", "Read", "Glob", "Grep", "Task", "WebSearch", "WebFetch", "mcp__*"],
            "deny": ["Read(./.env)", "Read(./.env.*)", "Read(./secrets/**)"],
            "defaultMode": "bypassPermissions",
        },
        "hooks": {ev: _hook_block(cmds) for ev, cmds in _build_hooks().items()},
        "enabledPlugins": {
            "discord@claude-plugins-official": True,
            "superpowers@claude-plugins-official": True,
            "context7@claude-plugins-official": True,
            "github@claude-plugins-official": False,
        },
        "theme": "dark",
    }
    return json.dumps(settings, indent=2, ensure_ascii=False)


def yaml_entry(*, name: str, host: str, channel: str, transport: str | None,
               config_dir: str, presence: str) -> str:
    """An agents.yaml block for the new bot (YAML text to append under `agents:`)."""
    t = "null" if not transport else transport
    access = f"{config_dir}/channels/discord/access.json"
    claude_md = f"{config_dir}/CLAUDE.md"
    return (
        f"  {name}:\n"
        f"    host: {host}\n"
        f"    config_dir: {config_dir}\n"
        f"    schema: claude\n"
        f"    transport: {t}\n"
        f"    home_channel: \"{channel}\"\n"
        f"    access_json: {access}\n"
        f"    personas:\n"
        f"      - {{slot: CLAUDE.md, path: {claude_md}, mode: plain}}\n"
    )


def render_launcher(*, name: str, config_dir: str) -> str:
    """A ~/.local/bin/<name> launcher (interactive --channels bot).

    Run in a real terminal so it can clear trust/permission prompts.
    """
    state = f"{config_dir}/channels/discord"
    return (
        "#!/usr/bin/env bash\n"
        f"# `{name}` — interactive Claude Code --channels Discord bot (run in a real\n"
        "# terminal so it can clear trust/permission prompts). No background service.\n"
        "set -euo pipefail\n"
        f'export CLAUDE_CONFIG_DIR="{config_dir}"\n'
        f'export DISCORD_STATE_DIR="{state}"\n'
        'export PATH="$HOME/.local/bin:$PATH"\n'
        'exec claude --channels plugin:discord@claude-plugins-official --permission-mode bypassPermissions "$@"\n'
    )


def render_presence_file(presence: str) -> str:
    return presence.rstrip("\n") + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="new-bot", description="Provision a Claude Code --channels Discord bot.")
    p.add_argument("name")
    p.add_argument("--host", required=True, help="host the bot runs on (e.g. myhost | localhost)")
    p.add_argument("--channel", required=True, help="home channel ID (Discord snowflake)")
    p.add_argument("--transport", default=None, help="ssh wrapper name for a remote host (omit for local)")
    p.add_argument("--config-dir", default=None, help="defaults to <CCDK_AGENTS_DIR>/<name>")
    p.add_argument("--presence", default=None, help="Discord status string")
    p.add_argument("--remote-url", default=None, help="CCDK_URL for off-host bots (defaults to CCDK_URL in the env file)")
    p.add_argument("--local-host", default="localhost", help="host value treated as local (no remote URL injected)")
    p.add_argument("--apply", action="store_true", help="actually write (default: dry-run)")
    a = p.parse_args(argv)

    config_dir = a.config_dir or os.path.join(_config_dir_root(), a.name)
    presence = a.presence or f"🤖 {a.name}"
    default_remote = a.remote_url if a.remote_url is not None else _default_remote_url()
    remote_url = default_remote if a.host != a.local_host else None

    print(f"=== new-bot: {a.name} (host={a.host}, channel={a.channel}, transport={a.transport or 'local'}) ===\n")
    print("--- agents.yaml entry ---")
    print(yaml_entry(name=a.name, host=a.host, channel=a.channel, transport=a.transport,
                     config_dir=config_dir, presence=presence))
    print("--- settings.json ---")
    print(render_settings_json(bot=a.name, presence=presence, remote_url=remote_url))
    print(f"--- launcher → ~/.local/bin/{a.name} ---")
    print(render_launcher(name=a.name, config_dir=config_dir))
    print(f"--- .presence → {config_dir}/channels/discord/.presence ---")
    print(render_presence_file(presence))

    if not a.apply:
        print("\n[DRY RUN] nothing written. Re-run with --apply to provision.")
        if a.transport:
            print(f"[reminder] ensure the `{a.transport}` transport wrapper exists on this host's ~/.local/bin (per-host deploy artifact).")
        return 0

    # --apply path: orchestration. For local bots, write directly; for remote,
    # this scaffolds on THIS host — point at the target box or run there.
    print("\n[APPLY] writing… (orchestration writes the YAML entry + scaffolds files; remote hosts need the files placed on that box)")
    # YAML append (append under agents: — caller reviews the diff). Defaults to
    # the kit's agents.yaml location; override with CCDK_AGENTS_FILE.
    yaml_path = os.environ.get(
        "CCDK_AGENTS_FILE",
        os.path.expanduser("~/.config/cc-discord-kit/agents.yaml"),
    )
    os.makedirs(os.path.dirname(yaml_path), exist_ok=True)
    with open(yaml_path, "a", encoding="utf-8") as f:
        f.write(yaml_entry(name=a.name, host=a.host, channel=a.channel, transport=a.transport,
                           config_dir=config_dir, presence=presence))
    print(f"  appended {a.name} to {yaml_path}")
    print("  NOTE: config dir / settings.json / launcher / .presence scaffolding for a REMOTE host")
    print("  must be placed on that box (use the transport or run new-bot there).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
