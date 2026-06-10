"""Bot capability / infra matrix — anti-drift.

Each bot self-reports the hooks + features its environment ACTUALLY has wired
up (read from its own settings.json). Those reports accumulate in a matrix
keyed by bot name. A human-maintained INTENDED declaration says what each bot
is SUPPOSED to have. drift_report() diffs the two so silent drift — a feature
that landed on one bot but was never wired up on the others, with nothing
tracking it — becomes a visible diff.

Scope is deliberately LOCAL: a bot can only inspect ITS OWN environment. We
never reach out to other hosts. The matrix is the union of self-reports
gathered over time as each bot runs `capabilities report` on its own host.

Storage: capabilities.json in the data dir (store.DATA_DIR), a flat dict
    {bot_name: {bot, machine, config_dir, hooks, detected_features, reported_ts}}
Plain JSON (not a JsonStore) — it's a small keyed map, last-write-wins per bot,
no IDs/tombstones/caps needed.

The INTENDED declaration is not hardcoded: it is read from the env file
(CCDK_INTENDED_CAPABILITIES, a comma-separated list of "bot:feat1+feat2"
entries) so this module ships without baked-in bot names. The module imports
cleanly with no config file present (empty intended map → every bot "unknown").
"""

from __future__ import annotations

import json
import logging
import os
import socket
from datetime import datetime, timezone

import store  # DATA_DIR lives here

log = logging.getLogger("cc_discord_kit.capabilities")

ENV_FILE = os.path.expanduser("~/.config/cc-discord-kit/env")
INTENDED_VAR = "CCDK_INTENDED_CAPABILITIES"


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


def _detect_bot() -> str | None:
    """Best-effort agent name. Override with CCDK_BOT in env.

    Fallback: derive from CLAUDE_CONFIG_DIR's last path segment if set
    (e.g. ~/.claude-alt → "claude-alt"). Works for any naming scheme.
    Returns None when nothing identifies the bot — callers default it.
    """
    explicit = os.environ.get("CCDK_BOT", "").strip()
    if explicit:
        return explicit
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if cfg:
        return os.path.basename(cfg.rstrip("/")) or None
    return None


def _matrix_path() -> str:
    """Resolve lazily off store.DATA_DIR so test reloads (which repoint
    CCDK_DATA_DIR then re-import store) land in the tmp dir, not prod."""
    return os.path.join(store.DATA_DIR, "capabilities.json")


# Substring → feature name. A hook command's basename is scanned for each
# substring; every match contributes its feature. Underscores in commands are
# normalized to hyphens before matching so `tool_watcher` and `tool-watcher`
# both resolve. This is the recognizer for what a wired-up hook MEANS — kept
# small and obvious so adding a new tracked capability is a one-line edit.
FEATURE_SIGNATURES: dict[str, str] = {
    "passthrough": "passthrough",
    "narrate": "narrate",
    "tool-watcher": "tool-watcher",
    "compact": "compaction-recovery",
    "mention-resolver": "mention-resolver",
}


def features_for(command: str) -> set[str]:
    """The set of features implied by a single hook command (matched against
    its basename, underscores normalized to hyphens). Empty set if none match."""
    base = os.path.basename((command or "").strip().split()[0] if command and command.strip() else "")
    norm = base.replace("_", "-").lower()
    return {feat for sub, feat in FEATURE_SIGNATURES.items() if sub in norm}


def _settings_path() -> str:
    """The current bot's settings.json. $CLAUDE_CONFIG_DIR/settings.json, or the
    default ~/.claude/settings.json when CLAUDE_CONFIG_DIR is unset."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    base = cfg if cfg else os.path.expanduser("~/.claude")
    return os.path.join(base, "settings.json")


def _hook_command_basenames(hooks_block: dict) -> dict[str, list[str]]:
    """Extract {event: [command-basename, ...]} from a settings.json `hooks`
    block. The Claude Code shape is:
        {event: [{matcher, hooks: [{type, command}, ...]}, ...]}
    We flatten to the basenames of each command (the leaf script name, args
    stripped) so a host-specific absolute path doesn't obscure WHAT runs."""
    out: dict[str, list[str]] = {}
    if not isinstance(hooks_block, dict):
        return out
    for event, groups in hooks_block.items():
        names: list[str] = []
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks", []) or []:
                if not isinstance(hook, dict):
                    continue
                cmd = (hook.get("command") or "").strip()
                if not cmd:
                    continue
                # Strip args: first whitespace-delimited token is the program.
                prog = cmd.split()[0]
                names.append(os.path.basename(prog))
        if names:
            out[event] = names
    return out


def collect_self_capabilities() -> dict:
    """Inspect the CURRENT bot's environment and return its self-report.

    {bot, machine, config_dir, hooks: {event: [basenames]}, detected_features}

    Robust to a missing/corrupt settings.json (empty hooks, no crash) — a bot
    that hasn't wired anything up should still report, showing zero features
    (which is itself the drift signal)."""
    bot = _detect_bot() or "my-bot-1"  # None = generic default

    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip() \
        or os.path.expanduser("~/.claude")

    hooks: dict[str, list[str]] = {}
    path = _settings_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            hooks = _hook_command_basenames(data.get("hooks") or {})
        except (OSError, json.JSONDecodeError, ValueError) as e:
            log.warning("could not parse settings.json at %s: %s", path, e)

    features: set[str] = set()
    for names in hooks.values():
        for name in names:
            features |= features_for(name)

    return {
        "bot": bot,
        "machine": socket.gethostname(),
        "config_dir": config_dir,
        "hooks": hooks,
        "detected_features": sorted(features),
    }


def load_matrix() -> dict:
    """Load the whole capability matrix {bot: report}. Missing or corrupt file
    → empty dict (never crash — a fresh box has no matrix yet)."""
    path = _matrix_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError) as e:
        log.warning("capabilities matrix unreadable at %s: %s", path, e)
        return {}


def get_bot(name: str) -> dict | None:
    """The recorded self-report for one bot, or None if it never reported."""
    return load_matrix().get(name)


def record_self() -> dict:
    """Collect this bot's capabilities and write its entry into the matrix
    (last-write-wins per bot). Returns the recorded entry (with reported_ts)."""
    rec = collect_self_capabilities()
    rec["reported_ts"] = datetime.now(timezone.utc).isoformat()

    matrix = load_matrix()
    matrix[rec["bot"]] = rec

    path = _matrix_path()
    os.makedirs(store.DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(matrix, f, indent=2, sort_keys=True, ensure_ascii=False)
    os.replace(tmp, path)
    return rec


# ─────────────────────── intended state + drift ───────────────────────


def _load_intended() -> dict[str, set[str]]:
    """Parse the human-maintained INTENDED declaration from the env file.

    Format: CCDK_INTENDED_CAPABILITIES=bot1:featA+featB,bot2:featA+featC
    Each comma-separated entry is "bot:feat1+feat2+..."; features within an
    entry are joined by `+` (commas already separate bots). A bare "bot:"
    with no features means "intended to have nothing tracked" (empty set).

    Lazy/empty default: returns {} when unset so the module imports cleanly
    with no config — drift_report() then has no bots to diff and returns []."""
    raw = _read_env_var(INTENDED_VAR)
    if not raw:
        return {}
    out: dict[str, set[str]] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        bot, feats = entry.split(":", 1)
        bot = bot.strip()
        if not bot:
            continue
        features = {f.strip() for f in feats.split("+") if f.strip()}
        out[bot] = features
    return out


# HUMAN-MAINTAINED SOURCE OF TRUTH: what each bot is SUPPOSED to have. Loaded
# from CCDK_INTENDED_CAPABILITIES (see _load_intended). Set it when a capability
# is meant to roll out to a bot; `capabilities drift` then surfaces any bot that
# hasn't actually wired it up (or wired up something unexpected). A typical
# setup declares the surfacing core (narrate + tool-watcher) plus whatever
# rollout you're tracking (e.g. passthrough) across every --channels bot.
INTENDED: dict[str, set[str]] = _load_intended()


def drift_report() -> list[dict]:
    """Diff intended vs. actual for every bot in INTENDED.

    Per bot:
      - never reported            → status "unknown"
      - reported, intended ⊆ have → status "ok"   (missing=[], maybe extra)
      - reported, missing some    → status "drift"
    `extra` = features the bot actually has that aren't in its intended set
    (not necessarily wrong — could be a new capability not yet declared — but
    worth surfacing). Sorted lists for stable output."""
    matrix = load_matrix()
    report: list[dict] = []
    for bot in sorted(INTENDED):
        intended = INTENDED[bot]
        rec = matrix.get(bot)
        if rec is None:
            report.append({
                "bot": bot,
                "status": "unknown",
                "intended": sorted(intended),
                "actual": None,
                "missing": sorted(intended),
                "extra": [],
                "machine": None,
                "reported_ts": None,
            })
            continue
        actual = set(rec.get("detected_features") or [])
        missing = sorted(intended - actual)
        extra = sorted(actual - intended)
        report.append({
            "bot": bot,
            "status": "drift" if missing else "ok",
            "intended": sorted(intended),
            "actual": sorted(actual),
            "missing": missing,
            "extra": extra,
            "machine": rec.get("machine"),
            "reported_ts": rec.get("reported_ts"),
        })
    return report
