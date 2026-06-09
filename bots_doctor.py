"""bots doctor — validate every agent in agents.yaml against reality.

For each agent: access.json resolves, every persona file resolves (locally or
via the agent's transport), home_channel looks like a snowflake, the transport
wrapper exists, and the derived registries have no orphans (an agent in the
registry not in the YAML). Automates audits that otherwise get done by hand
(e.g. a persona-path typo, a missing home-channel).

Scope: the Claude Code agents declared in agents.yaml. Service-style agents
(node daemons, etc.) get a lighter check — their persona file plus a live
systemd --user unit.

Config: the agent registry is read from `~/.config/cc-discord-kit/agents.yaml`
(or the path in `CCDK_AGENTS_FILE`), the same file `personas.py` uses. The
doctor needs a few fields beyond the persona slots `personas.py` surfaces
(transport, access_json, settings_source, config_dir, host, home_channel,
kind, service), so it parses the YAML directly. See `agents.example.yaml` in
the repo root for the schema; the doctor-specific keys are optional and any
agent missing them simply skips the checks that need them.
"""
import json
import os
import re
import subprocess

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - guidance shown if missing
    yaml = None  # type: ignore

_SNOWFLAKE = re.compile(r"^\d{17,20}$")


# ─────────────────────────── agents.yaml loader ───────────────────────────
# A thin reader for the doctor's needs. `personas.py` is the SSOT for the
# persona-slot side of agents.yaml; the doctor needs the richer per-agent
# fields too, so it reads the raw mapping here. Imports cleanly with no
# config file present (returns {}).

_AGENTS_CACHE: dict | None = None


def _agents_path() -> str:
    explicit = os.environ.get("CCDK_AGENTS_FILE")
    if explicit:
        return os.path.expanduser(explicit)
    return os.path.expanduser("~/.config/cc-discord-kit/agents.yaml")


def load_bots() -> dict:
    """Return the raw {agent_name: attrs} mapping from agents.yaml (cached).

    Empty dict if the file is absent so the module imports and runs without a
    config present. Each agent's attrs is the YAML mapping under `agents:`,
    expected to carry doctor fields (transport, access_json, settings_source,
    config_dir, host, home_channel, kind, service) plus a `personas` list of
    {slot, path} dicts. Missing fields just skip the checks that need them.
    """
    global _AGENTS_CACHE
    if _AGENTS_CACHE is not None:
        return _AGENTS_CACHE
    path = _agents_path()
    if not os.path.exists(path):
        _AGENTS_CACHE = {}
        return _AGENTS_CACHE
    if yaml is None:
        raise RuntimeError(
            "PyYAML is required to parse agents.yaml; pip install pyyaml"
        )
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw = data.get("agents") or {}
    # Tolerate the simple `personas.py` schema {name: [slot, ...]} as well as
    # the richer doctor schema {name: {..., personas: [...]}}: wrap a bare slot
    # list into a dict so every check can read fields uniformly. A flat-schema
    # agent carries no doctor fields, so its field-dependent checks just skip.
    _AGENTS_CACHE = {
        name: ({"personas": val} if isinstance(val, list) else (val or {}))
        for name, val in raw.items()
    }
    return _AGENTS_CACHE


def reset_cache() -> None:
    """Test hook + manual reload after editing agents.yaml."""
    global _AGENTS_CACHE
    _AGENTS_CACHE = None


def bot_registry() -> dict:
    """Derive the access-json registry: {agent: {path, schema, remote?}}.
    Only Claude agents with a non-null access_json."""
    out = {}
    for name, b in load_bots().items():
        if b.get("kind", "claude") != "claude":
            continue
        if not b.get("access_json"):
            continue
        entry = {"path": b["access_json"], "schema": b.get("schema")}
        if b.get("transport"):
            entry["remote"] = b["transport"]
        out[name] = entry
    return out


def persona_bots() -> dict:
    """Derive the persona registry: {agent: [(slot, path, transport), ...]}.
    Every agent with a `personas:` block, regardless of kind."""
    out = {}
    for name, b in load_bots().items():
        slots = b.get("personas") or []
        if not slots:
            continue
        transport = b.get("transport")
        out[name] = [(s["slot"], s["path"], transport) for s in slots]
    return out


# The narrate / tool-trace / presence / echo-guard hooks that EVERY Claude
# --channels agent must have wired into its settings.json for Discord
# surfacing to work. (event, identifier) pairs; the identifier is matched as a
# SUBSTRING of the hook command — so a host-specific path or extra flags don't
# cause a false "missing". This is the required CORE only; agents may carry
# extra optional hooks, which the doctor doesn't ding.
REQUIRED_HOOKS = [
    ("SessionStart", "cc-presence-hint"),
    ("PreToolUse", "cc-askuser-guard"),     # blocks AskUserQuestion, which wedges a --channels agent.
    ("PreToolUse", "cc-narrate-prereply"),
    ("PostToolUse", "cc-narrate-watcher"),
    ("PostToolUse", "cc-tool-watcher"),
    ("Stop", "cc-discord-echo-guard"),
    ("Stop", "cc-narrate-finalize"),
    ("PostToolUseFailure", "cc-tool-watcher"),
]


# ─────────────────────────── checks ───────────────────────────


def _file_exists(path: str, transport: str | None) -> bool:
    """Does `path` exist — locally if transport is None, else via the wrapper."""
    if not transport:
        return os.path.exists(path)
    try:
        r = subprocess.run([transport, f"test -f {path} && echo OK || echo NO"],
                           capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
        return r.stdout.strip() == "OK"
    except Exception:
        return False


def _read_file(path: str, transport: str | None) -> str | None:
    """Return the contents of `path` (local or via the transport wrapper), or
    None if it can't be read. Used to inspect an agent's settings.json wherever
    it physically lives."""
    if not transport:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None
    try:
        r = subprocess.run([transport, f"cat {path}"], capture_output=True,
                           text=True, timeout=15, stdin=subprocess.DEVNULL)
        return r.stdout if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        return None


def _transport_exists(transport: str) -> bool:
    """Is the transport wrapper on PATH?"""
    from shutil import which
    return which(transport) is not None


def _service_active(service: str) -> bool:
    """Is a systemd --user unit active? (best-effort; for service-style agents)"""
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", service],
                           capture_output=True, text=True, timeout=8, stdin=subprocess.DEVNULL)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def _missing_hooks(settings: dict) -> list[tuple[str, str]]:
    """Pure: given a parsed settings.json dict, return the REQUIRED_HOOKS that
    are NOT wired into it, as (event, identifier) pairs. A hook counts as
    present if its identifier appears as a substring of any command in that
    event (so host-specific paths / extra flags still match)."""
    hooks = settings.get("hooks", {}) if isinstance(settings, dict) else {}

    def present(event: str, ident: str) -> bool:
        for group in hooks.get(event, []):
            for h in group.get("hooks", []):
                if ident in h.get("command", ""):
                    return True
        return False

    return [(ev, ident) for ev, ident in REQUIRED_HOOKS
            if not present(ev, ident)]


def _check_hooks(name: str, b: dict) -> list[str]:
    """For a Claude --channels agent, read its settings.json (through its
    transport) and flag any missing REQUIRED_HOOKS. An agent with a stripped
    hook set is exactly the 'narrate/tool-trace silently stopped' failure —
    this catches it.

    Where the file lives: `settings_source` if declared (for agents whose
    launcher sets no CLAUDE_CONFIG_DIR and thus ride the default ~/.claude —
    one agent can share another's config), else {config_dir}/settings.json.
    Agents with neither (shouldn't happen for claude) are skipped rather than
    crashing."""
    base = b.get("settings_source") or b.get("config_dir")
    if not base:
        return [f"{name}: no config_dir/settings_source in agents.yaml (can't check hooks)"]
    path = base.rstrip("/")
    if not path.endswith("settings.json"):
        path += "/settings.json"
    raw = _read_file(path, b.get("transport"))
    if raw is None:
        return [f"{name}: settings.json unreadable → {path}"]
    try:
        settings = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [f"{name}: settings.json is not valid JSON → {path}"]
    return [f"{name}: missing hook {ev}:{ident}" for ev, ident in _missing_hooks(settings)]


def _check_discord_plugin(name: str, b: dict) -> list[str]:
    """Verify the discord plugin the agent RUNS has the presence patch
    (set_presence). A common trap: the agent runs server.ts from its plugins
    dir, but a patch script only touched a cache copy — so presence silently
    never loaded. We check EVERY discord server.ts under the agent's plugins
    root; if ANY copy is unpatched we flag it, because we can't be sure which
    one the runtime --cwd's into. Agents that share another agent's config dir
    (settings_source set) ride that agent's plugin and are skipped here to
    avoid double-reporting."""
    if b.get("settings_source"):
        return []
    cfg = b.get("config_dir")
    if not cfg:
        return []
    # plugins root is ~/.claude/plugins; derive from config_dir's home.
    # config_dir is like /home/<user>/.claude or /home/<user>/agents/<agent>/.claude
    plugins_root = cfg.rstrip("/")
    if plugins_root.endswith("/.claude"):
        plugins_root = plugins_root + "/plugins"
    else:
        # agent layout (.../<agent>/.claude) — plugins still live under that .claude
        plugins_root = plugins_root + "/plugins"
    transport = b.get("transport")
    # count discord server.ts files and how many contain set_presence
    # -L: follow symlinks. An agent's <agent>/.claude/plugins is often a
    # symlink to ~/.claude/plugins, and find won't descend a symlinked start
    # point without -L — that would falsely report "no plugin found".
    cmd = (f"total=0; patched=0; "
           f"for f in $(find -L {plugins_root} -type f -name server.ts -path '*discord*' "
           f"! -path '*/node_modules/*' 2>/dev/null); do "
           f"total=$((total+1)); grep -q \"name: 'set_presence'\" \"$f\" && patched=$((patched+1)); "
           f"done; echo \"$patched/$total\"")
    try:
        if transport:
            r = subprocess.run([transport, cmd], capture_output=True, text=True,
                               timeout=20, stdin=subprocess.DEVNULL)
        else:
            r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                               timeout=20, stdin=subprocess.DEVNULL)
        out = (r.stdout or "").strip()
    except Exception:
        return [f"{name}: discord plugin check failed (transport error)"]
    if "/" not in out:
        return [f"{name}: discord plugin check returned no result ({out!r})"]
    patched, total = out.split("/", 1)
    if total == "0":
        return [f"{name}: no discord plugin server.ts found under {plugins_root}"]
    if patched != total:
        return [f"{name}: discord plugin NOT fully patched ({patched}/{total} copies have set_presence) "
                f"— run the presence patch on {b.get('host', '?')}"]
    return []


def _check_common(name: str, b: dict) -> list[str]:
    """Checks that apply to every agent kind: transport wrapper, brain file(s),
    home_channel plausibility."""
    problems: list[str] = []
    transport = b.get("transport")
    if transport and not _transport_exists(transport):
        problems.append(f"{name}: transport wrapper '{transport}' not found on PATH")
    for slot in (b.get("personas") or []):
        if not _file_exists(slot["path"], transport):
            problems.append(f"{name}: brain file {slot['slot']} missing → {slot['path']}")
    hc = b.get("home_channel")
    if hc and not _SNOWFLAKE.match(str(hc)):
        problems.append(f"{name}: home_channel '{hc}' is not a valid snowflake")
    return problems


def check_bot(name: str) -> list[str]:
    """Return a list of problem strings for one agent ([] = healthy).
    Dispatches on `kind` — claude agents are checked for access.json +
    personas; service-style agents for their persona + a live service."""
    bots = load_bots()
    if name not in bots:
        return [f"{name}: not in agents.yaml"]
    b = bots[name]
    kind = b.get("kind", "claude")
    problems = _check_common(name, b)

    if kind == "claude":
        aj = b.get("access_json")
        if aj and not _file_exists(aj, b.get("transport")):
            problems.append(f"{name}: access.json missing → {aj}")
        problems += _check_hooks(name, b)
        problems += _check_discord_plugin(name, b)
    elif kind in ("gemini", "gpt", "service"):
        # service-style agents (e.g. a Node daemon) — no access.json/persona
        # admin; check the systemd unit instead.
        svc = b.get("service")
        if svc and not _service_active(svc):
            problems.append(f"{name}: service '{svc}' not active")
        elif not svc:
            problems.append(f"{name}: kind={kind} but no 'service' declared")
    elif kind == "shared":
        # not an agent — a shared context layer (e.g. a global CLAUDE.md). The
        # common brain-file check above is all that applies.
        pass
    else:
        problems.append(f"{name}: unknown kind '{kind}'")

    return problems


def check_orphans() -> list[str]:
    """Registries derive from YAML, so orphans shouldn't happen — but verify the
    derived registries only contain YAML agents (guards against manual drift)."""
    yaml_bots = set(load_bots())
    problems = []
    for n in bot_registry():
        if n not in yaml_bots:
            problems.append(f"orphan in access-json registry (not in agents.yaml): {n}")
    for n in persona_bots():
        if n not in yaml_bots:
            problems.append(f"orphan in persona registry (not in agents.yaml): {n}")
    return problems


def run() -> dict:
    """Run all checks. Returns {agent: [problems]} + '__orphans__'."""
    result = {}
    for name in load_bots():
        result[name] = check_bot(name)
    orph = check_orphans()
    if orph:
        result["__orphans__"] = orph
    return result


def format_report(result: dict) -> str:
    lines = []
    healthy = True
    for name, probs in result.items():
        if name == "__orphans__":
            continue
        mark = "✅" if not probs else "❌"
        if probs:
            healthy = False
        lines.append(f"  {mark} {name}")
        for p in probs:
            lines.append(f"       ↳ {p}")
    if result.get("__orphans__"):
        healthy = False
        lines.append("  ❌ ORPHANS:")
        for p in result["__orphans__"]:
            lines.append(f"       ↳ {p}")
    header = "🩺 bots doctor — " + ("all healthy ✅" if healthy else "PROBLEMS found ❌")
    return header + "\n" + "\n".join(lines)


if __name__ == "__main__":
    print(format_report(run()))
