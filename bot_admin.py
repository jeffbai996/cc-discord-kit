"""Per-bot access.json admin from the Discord helper bot.

Each managed bot owns its own `access.json` with per-channel flags
(requireMention, etc). Editing them by SSH-ing in and touching the file
is fine for setup but tedious mid-day; this module lets the helper slash
bot mutate them safely with validation.

Two access schemas exist in the wild:

  CLAUDE_SCHEMA  — Claude Code `--channels` bots
    { dmPolicy, allowFrom, groups: { <channelId>: {requireMention, allowFrom} } }

  GEMMA_SCHEMA   — a Gemini-style daemon bot
    { users, channels: { <channelId>: {enabled, requireMention,
                                       thinking, showCode, verbose, cache} } }

The registry maps bot names to (path, schema, optional remote transport).
Some bots' access.json lives on another host — for those we shell out to
the configured remote wrapper (an SSH bridge with a deny-list guard).
Anything else is local on the current host.

The registry loads from `~/.config/cc-discord-kit/agents.yaml`
(or the path in `CCDK_AGENTS_FILE`). See `agents.example.yaml` in the
repo root for the schema — bot_admin reads the optional top-level
`bot_admin:` block. The module imports cleanly with no config file
present (lazy/empty default).

Writes are atomic per-bot (tmp-file rename) and pure-data — we never
parse the bot's runtime state, just rewrite the JSON. Bots reload on
SIGHUP (Claude bots) or on their next gate check (the Gemini daemon
reads access on each message).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Any, Literal

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - guidance shown if missing
    yaml = None  # type: ignore


SchemaKind = Literal["claude", "gemma"]


# Default config search path: env var, then XDG-style fallback. Shared
# convention with personas.py so a single agents.yaml drives both.
def _config_path() -> str:
    explicit = os.environ.get("CCDK_AGENTS_FILE")
    if explicit:
        return os.path.expanduser(explicit)
    return os.path.expanduser("~/.config/cc-discord-kit/agents.yaml")


# Cache parsed registry so we don't reread on every call.
_REGISTRY_CACHE: dict[str, dict[str, Any]] | None = None


def _load_registry() -> dict[str, dict[str, Any]]:
    """Load the per-bot access.json registry from agents.yaml.

    Reads the optional top-level `bot_admin:` block:

      bot_admin:
        my-bot-1:
          path: ~/.config/my-bot-1/channels/discord/access.json
          schema: claude
        my-bot-2:
          path: /remote/path/access.json
          schema: claude
          remote: my-ssh-wrapper      # optional; routes ops through it
        my-bot-3:
          path: ~/.config/my-bot-3/access.json
          schema: gemma

    Returns {bot: {path, schema, remote?}}. Empty dict when the config
    file is absent so the module imports cleanly with no setup.
    """
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is not None:
        return _REGISTRY_CACHE

    path = _config_path()
    if not os.path.exists(path):
        _REGISTRY_CACHE = {}
        return _REGISTRY_CACHE

    if yaml is None:
        raise RuntimeError(
            "PyYAML is required to parse agents.yaml; pip install pyyaml"
        )

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    out: dict[str, dict[str, Any]] = {}
    for name, b in (data.get("bot_admin") or {}).items():
        if not isinstance(b, dict) or not b.get("path"):
            continue
        entry: dict[str, Any] = {
            "path": os.path.expanduser(str(b["path"])),
            "schema": str(b.get("schema", "claude")),
        }
        if b.get("remote"):
            entry["remote"] = str(b["remote"])
        out[str(name)] = entry

    _REGISTRY_CACHE = out
    return out


def reset_cache() -> None:
    """Test hook + manual reload after editing agents.yaml."""
    global _REGISTRY_CACHE
    _REGISTRY_CACHE = None


def _registry() -> dict[str, dict[str, Any]]:
    """Per-bot config: {bot: {path, schema, remote?}}. Path is absolute on
    the host; if `remote` is set, ops route through that wrapper (an SSH
    bridge) instead of touching the file locally."""
    return _load_registry()


# Per-channel flag schemas. Each flag knows its type so we can validate
# user input before writing. enum types declare allowed values.
#
# `storage` marks flags that live OUTSIDE access.json. Default storage is
# access.json; "narrate.json" routes the read/write through the sibling
# narrate.json file (used by the narrate watcher/finalize hooks). Keeping
# the schema unified means /bot set + /bot info + /bot list cover narrate
# the same way they cover requireMention — no second command surface for
# the same kind of toggle.
CLAUDE_CHANNEL_FLAGS: dict[str, dict[str, Any]] = {
    # `allowed` is synthetic: it maps to whether the channel is allowlisted
    # at all (presence of the groups[channel] entry for claude). storage
    # "__allowlist__" routes set_flag to the add/remove-entry path.
    "allowed":        {"type": "bool", "storage": "__allowlist__"},
    "requireMention": {"type": "bool"},
    "narrate":        {"type": "enum", "values": ["collapse", "always", "never"],
                       "storage": "narrate.json"},
    "tools":          {"type": "enum", "values": ["off", "collapse", "ticker", "diffs", "full"],
                       "storage": "tools.json"},
}

GEMMA_CHANNEL_FLAGS: dict[str, dict[str, Any]] = {
    # `allowed` mirrors the gemma-style native `enabled` field (its gate honors it).
    "allowed":        {"type": "bool", "storage": "__allowlist__"},
    "enabled":        {"type": "bool"},
    "requireMention": {"type": "bool"},
    "showCode":       {"type": "bool"},
    "verbose":        {"type": "bool"},
    "cache":          {"type": "bool"},
    "thinking":       {"type": "enum", "values": ["always", "auto", "never"]},
}

SCHEMA_FLAGS: dict[SchemaKind, dict[str, dict[str, Any]]] = {
    "claude": CLAUDE_CHANNEL_FLAGS,
    "gemma":  GEMMA_CHANNEL_FLAGS,
}


def list_bots() -> list[str]:
    return list(_registry().keys())


def schema_for(bot: str) -> SchemaKind:
    reg = _registry()
    if bot not in reg:
        raise KeyError(bot)
    return reg[bot]["schema"]


def flags_for(bot: str) -> dict[str, dict[str, Any]]:
    return SCHEMA_FLAGS[schema_for(bot)]


def coerce_value(flag: str, raw: str, schema: SchemaKind) -> Any:
    """Parse a CLI/user-supplied string into the typed value the JSON
    schema wants. Raises ValueError on bad input so the caller can show
    a friendly message."""
    spec = SCHEMA_FLAGS[schema].get(flag)
    if spec is None:
        raise ValueError(f"unknown flag '{flag}' for schema '{schema}'")
    t = spec["type"]
    if t == "bool":
        low = raw.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"flag '{flag}' is bool; got {raw!r}")
    if t == "enum":
        # Legacy alias: narrate's 'auto' was renamed to 'collapse'.
        if flag == "narrate" and raw == "auto":
            raw = "collapse"
        if raw in spec["values"]:
            return raw
        raise ValueError(
            f"flag '{flag}' must be one of {spec['values']}; got {raw!r}"
        )
    raise ValueError(f"unsupported flag type '{t}' for '{flag}'")


def _sibling_path(bot: str, filename: str) -> str:
    """Path of a sibling file alongside the bot's access.json (e.g.
    narrate.json). Same directory, same host (local or remote)."""
    base = _registry()[bot]["path"]
    return os.path.join(os.path.dirname(base), filename)


def _read_text(bot: str, path: str | None = None) -> str:
    """Return raw text contents for `bot`. Defaults to access.json; pass
    an explicit `path` to read a sibling file (narrate.json, etc.).
    Routes through the remote wrapper for remote bots."""
    cfg = _registry()[bot]
    remote = cfg.get("remote")
    if path is None:
        path = cfg["path"]
    if remote:
        p = subprocess.run(
            [remote, f"cat {path}"],
            capture_output=True, text=True, timeout=8,
        )
        if p.returncode != 0:
            raise RuntimeError(
                f"{remote} cat {path} failed: rc={p.returncode} {p.stderr.strip()}"
            )
        return p.stdout
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write_text(bot: str, body: str, path: str | None = None) -> None:
    """Atomic write of `body` to the bot's access.json (default) or a
    sibling file when `path` is given. Local bots: tmp + rename. Remote
    bots: pipe via wrapper to `cat > tmp && mv tmp orig`."""
    cfg = _registry()[bot]
    remote = cfg.get("remote")
    if path is None:
        path = cfg["path"]
    if remote:
        # Remote atomic write: write tmp, then mv. The wrapper allows
        # arbitrary non-destructive commands so this works.
        tmp = path + ".tmp"
        p = subprocess.run(
            [remote, f"cat > {tmp} && mv {tmp} {path}"],
            input=body, capture_output=True, text=True, timeout=12,
        )
        if p.returncode != 0:
            raise RuntimeError(
                f"{remote} write failed: rc={p.returncode} {p.stderr.strip()}"
            )
        return
    # Local atomic write
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".access-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_access(bot: str) -> dict[str, Any]:
    """Parsed access.json for `bot`."""
    return json.loads(_read_text(bot))


def _read_narrate(bot: str) -> dict[str, str]:
    """Parsed narrate.json for `bot`. Returns {} when file is missing.

    Legacy 'auto' values get migrated to 'collapse'."""
    path = _sibling_path(bot, "narrate.json")
    try:
        raw = _read_text(bot, path=path)
    except (FileNotFoundError, RuntimeError):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {k: ("collapse" if v == "auto" else v) for k, v in parsed.items()}


def _write_narrate(bot: str, data: dict[str, str]) -> None:
    """Atomic write of narrate.json. Drops entries whose value is 'never'
    so the file stays minimal (default mode = never)."""
    pruned = {k: v for k, v in data.items() if v != "never"}
    path = _sibling_path(bot, "narrate.json")
    body = json.dumps(pruned, indent=2, ensure_ascii=False) + "\n"
    _write_text(bot, body, path=path)


def _read_tools(bot: str) -> dict[str, str]:
    """Parsed tools.json for `bot`. Returns {} when file is missing."""
    path = _sibling_path(bot, "tools.json")
    try:
        raw = _read_text(bot, path=path)
    except (FileNotFoundError, RuntimeError):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_tools(bot: str, data: dict[str, str]) -> None:
    """Atomic write of tools.json. Drops entries whose value is 'off' so
    the file stays minimal (default mode = off, like narrate's 'never')."""
    pruned = {k: v for k, v in data.items() if v != "off"}
    path = _sibling_path(bot, "tools.json")
    body = json.dumps(pruned, indent=2, ensure_ascii=False) + "\n"
    _write_text(bot, body, path=path)


def channel_flags(bot: str, channel_id: str) -> dict[str, Any]:
    """Return the per-channel flag dict for `bot` in `channel_id`, or
    empty if the channel isn't tracked yet. Merges sibling-file flags
    (e.g. narrate.json) into the same dict so /bot info / /bot list
    show every controllable flag in one place."""
    data = read_access(bot)
    schema = schema_for(bot)
    if schema == "claude":
        flags = dict((data.get("groups") or {}).get(channel_id, {}))
    elif schema == "gemma":
        flags = dict((data.get("channels") or {}).get(channel_id, {}))
    else:
        flags = {}
    # Merge in flags that live in sibling files. Add more here if other
    # flags get out-of-band storage.
    narrate_cfg = _read_narrate(bot)
    if channel_id in narrate_cfg:
        flags["narrate"] = narrate_cfg[channel_id]
    tools_cfg = _read_tools(bot)
    if channel_id in tools_cfg:
        flags["tools"] = tools_cfg[channel_id]
    # Surface the synthetic `allowed` state. claude: allowlisted iff the channel
    # has a groups entry; gemma: the channel's `enabled` field (default False if
    # the channel isn't listed at all).
    if schema == "claude":
        flags["allowed"] = channel_id in ((data.get("groups") or {}))
    elif schema == "gemma":
        gch = (data.get("channels") or {}).get(channel_id)
        flags["allowed"] = bool(gch.get("enabled", True)) if gch else False
    return flags


def set_flag(bot: str, channel_id: str, flag: str, value: Any) -> dict[str, Any]:
    """Set a single flag on `bot` for `channel_id`. Creates the channel
    entry if missing. Returns the *new* per-channel flag dict.

    Routes the write based on the flag spec's `storage` field:
      - default → access.json (`groups[channel_id]` for claude,
        `channels[channel_id]` for gemma)
      - "narrate.json" → sibling narrate.json file
      - "tools.json" → sibling tools.json file

    Caller is responsible for validating `value` against the schema via
    `coerce_value`. We don't re-validate here (decoupled so the Discord
    handler can produce nice error messages closer to the user input).
    """
    schema = schema_for(bot)
    spec = SCHEMA_FLAGS[schema].get(flag, {})
    storage = spec.get("storage", "access.json")

    if storage == "narrate.json":
        narrate_cfg = _read_narrate(bot)
        narrate_cfg[channel_id] = value
        _write_narrate(bot, narrate_cfg)
        # Return a view that mirrors the merged channel_flags shape.
        return channel_flags(bot, channel_id)

    if storage == "tools.json":
        tools_cfg = _read_tools(bot)
        tools_cfg[channel_id] = value
        _write_tools(bot, tools_cfg)
        return channel_flags(bot, channel_id)

    if storage == "__allowlist__":
        # Synthetic allowlist toggle. For claude, un-allowlisting REMOVES the
        # channel's groups entry (and its per-channel requireMention/narrate/
        # tools); re-allowlisting recreates it with defaults. For gemma, it
        # flips the native `enabled` field.
        data = read_access(bot)
        if schema == "claude":
            groups = data.setdefault("groups", {})
            if value:
                groups.setdefault(channel_id, {"requireMention": True, "allowFrom": []})
            else:
                groups.pop(channel_id, None)
        elif schema == "gemma":
            channels = data.setdefault("channels", {})
            ch = channels.setdefault(channel_id, {"enabled": True, "requireMention": True})
            ch["enabled"] = bool(value)
        else:
            raise ValueError(f"unknown schema {schema}")
        body = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        _write_text(bot, body)
        return channel_flags(bot, channel_id)

    # Default — flag stored inline in access.json
    data = read_access(bot)
    if schema == "claude":
        groups = data.setdefault("groups", {})
        ch = groups.setdefault(channel_id, {"requireMention": True, "allowFrom": []})
        ch[flag] = value
    elif schema == "gemma":
        channels = data.setdefault("channels", {})
        ch = channels.setdefault(channel_id, {"enabled": True, "requireMention": True})
        ch[flag] = value
    else:
        raise ValueError(f"unknown schema {schema}")

    body = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _write_text(bot, body)
    return ch


def all_bot_flags_for_channel(channel_id: str) -> list[dict[str, Any]]:
    """Render `[ {bot, schema, flags, error?} ]` for every registered bot
    in `channel_id`. Per-bot read errors don't fail the whole call —
    each entry has an `error` key if the bot was unreachable."""
    out: list[dict[str, Any]] = []
    for bot in list_bots():
        entry: dict[str, Any] = {"bot": bot, "schema": schema_for(bot)}
        try:
            entry["flags"] = channel_flags(bot, channel_id)
        except Exception as e:
            entry["error"] = str(e)
        out.append(entry)
    return out


def _configured_channels(bot: str) -> set[str]:
    """Every channel id that has ANY config for `bot` — from access.json
    (groups/channels) plus the sibling narrate.json / tools.json files."""
    chans: set[str] = set()
    try:
        data = read_access(bot)
        schema = schema_for(bot)
        key = "groups" if schema == "claude" else "channels"
        chans.update((data.get(key) or {}).keys())
    except Exception:
        pass
    try:
        chans.update(_read_narrate(bot).keys())
    except Exception:
        pass
    try:
        chans.update(_read_tools(bot).keys())
    except Exception:
        pass
    return chans


def status_grid() -> list[dict[str, Any]]:
    """Compact cross-bot status: for every registered bot, list each
    channel it is configured in along with that channel's merged flags.
    Shape: [ {bot, schema, channels: [ {channel_id, flags} ], error?} ].
    A bot with no per-channel config shows an empty `channels` list."""
    out: list[dict[str, Any]] = []
    for bot in list_bots():
        entry: dict[str, Any] = {"bot": bot, "schema": schema_for(bot), "channels": []}
        try:
            for cid in sorted(_configured_channels(bot)):
                entry["channels"].append({"channel_id": cid, "flags": channel_flags(bot, cid)})
        except Exception as e:
            entry["error"] = str(e)
        out.append(entry)
    return out
