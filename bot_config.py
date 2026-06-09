"""Single source of truth loader for agents. Reads agents.yaml and derives
the shapes the various registries expect, so /bot, the /context bot-file
cards, and card routing all come from ONE declaration. Leaf module — imports
nothing from the admin/personas/cli/client modules (no circular import).

Reads the same registry file as `personas.py`:
`~/.config/cc-discord-kit/agents.yaml` (or the path in `CCDK_AGENTS_FILE`).
See `agents.example.yaml` in the repo root for the base schema; this module
reads a few OPTIONAL per-agent keys on top of it, all of which default
sensibly when absent so a minimal registry still works:

  agents:
    agent-1:
      kind: claude              # default "claude"; non-claude agents (gemini/
                                # gpt daemons) are skipped by claude-only views
      config_dir: ~/.claude     # absolute config dir, for detect_bot()
      schema: claude            # registry schema tag
      transport: ssh-host       # null/absent = local; else a remote wrapper id
      home_channel: "<id>"      # Discord channel id, for card routing
      access_json: ~/.../access.json   # null/absent = agent not in /bot
      personas:                 # per-agent brain files
        - { slot: CLAUDE.md, path: ~/.../CLAUDE.md, mode: plain }

The module imports cleanly with NO config file present (empty registry).
"""
import os

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - guidance shown if missing
    yaml = None  # type: ignore


def _config_path() -> str:
    """Registry file: env override, then XDG-style default (matches personas.py)."""
    explicit = os.environ.get("CCDK_AGENTS_FILE")
    if explicit:
        return os.path.expanduser(explicit)
    return os.path.expanduser("~/.config/cc-discord-kit/agents.yaml")


_cache = None

# The narrate / tool-trace / presence / echo-guard hooks that EVERY Claude
# --channels agent must have wired into its settings.json for Discord surfacing
# to work. (event, identifier) pairs; the identifier is matched as a SUBSTRING
# of the hook command — so a host-specific path or extra flags don't cause a
# false "missing" (e.g. a hook installed under a different absolute path on
# another machine still matches). This is the required CORE only; agents may
# carry extra optional hooks (passthrough, crosscheck, …) — the doctor doesn't
# ding those.
REQUIRED_HOOKS = [
    ("SessionStart", "cc-presence-hint"),
    ("PreToolUse", "cc-askuser-guard"),     # blocks AskUserQuestion, which wedges a --channels agent
    ("PreToolUse", "cc-narrate-prereply"),
    ("PostToolUse", "cc-narrate-watcher"),
    ("PostToolUse", "cc-tool-watcher"),
    ("Stop", "cc-discord-echo-guard"),
    ("Stop", "cc-narrate-finalize"),
    ("PostToolUseFailure", "cc-tool-watcher"),
]


def load_bots() -> dict:
    """Return the raw {agent_name: attrs} dict from agents.yaml (cached).

    Empty dict if the registry file is absent or has no `agents:` block, so
    callers degrade to an empty registry rather than raising.
    """
    global _cache
    if _cache is not None:
        return _cache

    path = _config_path()
    if not os.path.exists(path):
        _cache = {}
        return _cache
    if yaml is None:
        raise RuntimeError(
            "PyYAML is required to parse agents.yaml; pip install pyyaml"
        )
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw = data.get("agents") or {}
    # Tolerate both registry shapes. The simple `personas.py` schema is
    # {name: [slot, ...]} (a bare list of persona slots). The richer schema
    # the fleet tooling needs is {name: {kind, config_dir, ..., personas: [...]}}.
    # Normalize the former into the latter so every caller can treat an agent's
    # value as a dict; flat-schema agents just have no extra fields (and the
    # rich-field getters below degrade to defaults).
    _cache = {
        name: ({"personas": val} if isinstance(val, list) else (val or {}))
        for name, val in raw.items()
    }
    return _cache


def reset_cache() -> None:
    """Test hook + manual reload after editing agents.yaml."""
    global _cache
    _cache = None


def bot_registry() -> dict:
    """Derive the admin registry: {agent: {path, schema, remote?}}.
    Only agents with a non-null access_json (i.e. present in /bot)."""
    out = {}
    for name, b in load_bots().items():
        if b.get("kind", "claude") != "claude":
            continue
        if not b.get("access_json"):
            continue
        entry = {"path": b["access_json"], "schema": b.get("schema", "claude")}
        if b.get("transport"):
            entry["remote"] = b["transport"]
        out[name] = entry
    return out


def persona_bots() -> dict:
    """Derive personas.BOTS: {agent: [(slot, path, mode, remote_or_None), ...]}.

    Every entry with a `personas:` block, regardless of kind — the bot-file
    editor on /context edits each agent's brain file(s) (CLAUDE.md for Claude
    agents, persona.md for other runtimes, a shared global CLAUDE.md for a
    `global` pseudo-entry). This is intentionally broader than bot_registry()/
    home_channels(), which stay Claude-only for /bot and card routing."""
    out = {}
    for name, b in load_bots().items():
        slots = b.get("personas") or []
        if not slots:
            continue
        transport = b.get("transport")
        out[name] = [(s["slot"], s["path"], s["mode"], transport) for s in slots]
    return out


def home_channels() -> dict:
    """Derive the {agent: channel_id} card-routing map. Excludes null home_channel."""
    return {n: b["home_channel"] for n, b in load_bots().items()
            if b.get("home_channel") and b.get("kind", "claude") == "claude"}


def _norm(p: str) -> str:
    """Normalize a filesystem path for comparison (strip trailing slash, resolve
    '..', no symlink resolution since these are config-declared paths)."""
    return os.path.normpath(p) if p else ""


def detect_bot(*, config_dir: str = "", discord_state_dir: str = "",
               bot_override: str = "") -> str | None:
    """Identify which agent is invoking, from its environment. Single
    SSOT-backed implementation shared by cli.py + client.py.

    Resolution order:
      1. bot_override explicit override (trimmed) — always wins. Callers pass
         the value of the CCDK_BOT env var here.
      2. CLAUDE_CONFIG_DIR matched against each agent's `config_dir` in
         agents.yaml. Distinguishes co-located agents whose default
         '~/.claude' resolves to different absolute paths per box.
      3. DISCORD_STATE_DIR matched against the agent's access.json parent dir —
         for agents that ride a shared CLAUDE_CONFIG_DIR and set only the state
         dir. As long as each such agent has a DISTINCT state dir, each
         resolves unambiguously to its own agent.
      4. None — unknown. Caller decides the default.
    """
    if bot_override and bot_override.strip():
        return bot_override.strip()

    bots = load_bots()

    cfg = _norm(config_dir)
    if cfg:
        for name, b in bots.items():
            if b.get("kind", "claude") != "claude":
                continue
            if b.get("config_dir") and _norm(os.path.expanduser(b["config_dir"])) == cfg:
                return name

    sd = _norm(discord_state_dir)
    if sd:
        for name, b in bots.items():
            if b.get("kind", "claude") != "claude":
                continue
            aj = b.get("access_json")
            if aj and _norm(os.path.dirname(os.path.expanduser(aj))) == sd:
                return name

    return None
