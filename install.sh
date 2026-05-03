#!/usr/bin/env bash
# multiagent-tools installer. Idempotent — safe to re-run.
#
# Linux — by default:
#   - symlinks ~/.local/bin/multiagent-tools -> module's cli.py
#   - patches ~/.claude/settings.json hooks to point at the module
#   - installs systemd user units for server.py + discord_handler.py
#     (use --no-services to skip the systemd step)
#
# macOS — by default:
#   - symlinks ~/.local/bin/multiagent-tools -> module's cli.py
#   - prompts for MULTIAGENT_URL and writes ~/.config/multiagent-tools/env
#     (CLI runs in HTTP mode against the configured remote server)
#
# Re-runnable. Detects existing patches via marker comments and skips them.

set -eo pipefail

MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
OS="$(uname -s)"
WITH_SERVICES=1
MARKER="# multiagent-tools-module"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--no-services]

Options:
  --no-services   skip installing systemd user units (Linux only)
  -h, --help      this message
EOF
}

for arg in "$@"; do
  case "$arg" in
    --no-services) WITH_SERVICES=0 ;;
    -h|--help)     usage; exit 0 ;;
    *)             echo "unknown arg: $arg" >&2; usage; exit 2 ;;
  esac
done

echo "==> multiagent-tools module install"
echo "    module dir: $MODULE_DIR"
echo "    OS:         $OS"
echo

# 1. CLI symlink (both platforms)
mkdir -p "$HOME/.local/bin"
LINK="$HOME/.local/bin/multiagent-tools"
if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$MODULE_DIR/cli.py" ]; then
  echo "  ok $LINK (already linked)"
else
  if [ -e "$LINK" ] || [ -L "$LINK" ]; then
    rm "$LINK"
  fi
  ln -s "$MODULE_DIR/cli.py" "$LINK"
  echo "  -> $LINK -> $MODULE_DIR/cli.py"
fi
chmod +x "$MODULE_DIR/cli.py"

# 2. Platform-specific
case "$OS" in
  Linux)
    SETTINGS="$HOME/.claude/settings.json"
    if [ ! -f "$SETTINGS" ]; then
      echo "  WARN: $SETTINGS does not exist — skipping hook patch"
    else
      # Detect prior patch via marker
      if grep -q "$MARKER" "$SETTINGS" 2>/dev/null; then
        echo "  ok $SETTINGS (hooks already patched)"
      else
        echo "  >> patching $SETTINGS hooks"
        # Use python so we can edit JSON safely
        python3 - "$SETTINGS" "$MODULE_DIR" "$MARKER" <<'PYEOF'
import json, os, sys, shutil

settings_path = sys.argv[1]
module_dir = sys.argv[2]
marker = sys.argv[3]

with open(settings_path) as f:
    raw = f.read()
data = json.loads(raw)

# Backup once
backup = settings_path + ".pre-multiagent-tools"
if not os.path.exists(backup):
    shutil.copy2(settings_path, backup)

new_dir = module_dir + "/hooks"

def patched(cmd: str) -> str:
    """Rewrite known hook scripts to point at this module's hooks/ dir."""
    for name in ("session_start_hook.py", "user_prompt_hook.py", "stop_hook.py", "precompact_hook.py"):
        if name in cmd:
            return f"python3 {new_dir}/{name}  {marker}"
    return cmd

hooks = data.get("hooks", {})
changed = False
for event, blocks in hooks.items():
    for block in blocks:
        for entry in block.get("hooks", []):
            cmd = entry.get("command", "")
            new_cmd = patched(cmd)
            if new_cmd != cmd:
                entry["command"] = new_cmd
                changed = True

if changed:
    tmp = settings_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, settings_path)
    print(f"     patched (backup at {backup})")
else:
    print("     no legacy paths found — nothing to patch")
PYEOF
      fi
    fi

    # 3. systemd user units (default on, skip with --no-services)
    if [ "$WITH_SERVICES" = "1" ]; then
      UNIT_DIR="$HOME/.config/systemd/user"
      mkdir -p "$UNIT_DIR"
      ENV_FILE="$HOME/.config/multiagent-tools/env"
      mkdir -p "$(dirname "$ENV_FILE")"
      [ ! -f "$ENV_FILE" ] && touch "$ENV_FILE" && chmod 600 "$ENV_FILE"

      # Resolve which python to invoke. Prefer the module's venv (built by
      # this script if missing), fall back to system python3.
      VENV_PY="$MODULE_DIR/venv/bin/python3"
      if [ ! -x "$VENV_PY" ]; then
        echo "  >> creating venv at $MODULE_DIR/venv"
        python3 -m venv "$MODULE_DIR/venv" >/dev/null
        "$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null
        "$VENV_PY" -m pip install --quiet -r "$MODULE_DIR/requirements.txt" \
          | sed 's/^/     /'
      fi
      echo "  ok venv python: $VENV_PY"

      echo "  >> installing systemd units"

      cat > "$UNIT_DIR/multiagent-tools-server.service" <<UNIT
[Unit]
Description=multiagent-tools Flask web UI + HTTP API
After=network.target

[Service]
Type=simple
EnvironmentFile=-$ENV_FILE
Environment=MULTIAGENT_HOST=127.0.0.1
Environment=MULTIAGENT_PORT=5005
ExecStart=$VENV_PY $MODULE_DIR/server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT
      echo "     wrote $UNIT_DIR/multiagent-tools-server.service"

      cat > "$UNIT_DIR/multiagent-tools-discord.service" <<UNIT
[Unit]
Description=multiagent-tools Discord /mem and /journal slash command handler
After=network.target

[Service]
Type=simple
EnvironmentFile=-$ENV_FILE
ExecStart=$VENV_PY $MODULE_DIR/discord_handler.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
UNIT
      echo "     wrote $UNIT_DIR/multiagent-tools-discord.service"

      systemctl --user daemon-reload
      systemctl --user enable --now multiagent-tools-server.service 2>&1 \
        | sed 's/^/     /'
      # Discord handler enable depends on token presence — only enable if env file has it
      if grep -q "MULTIAGENT_DISCORD_TOKEN=" "$ENV_FILE" 2>/dev/null; then
        systemctl --user enable --now multiagent-tools-discord.service 2>&1 \
          | sed 's/^/     /'
      else
        echo "     SKIP multiagent-tools-discord.service (set MULTIAGENT_DISCORD_TOKEN in $ENV_FILE then enable)"
      fi
    else
      echo "  -- skipping systemd units (--no-services)"
    fi

    echo
    echo "Linux install done."
    echo "  - CLI:        multiagent-tools memory list"
    echo "  - Web UI:     http://127.0.0.1:5005  (expose via 'tailscale serve')"
    echo "  - Hooks:      restart Claude Code to load patched settings.json"
    ;;
  Darwin)
    ENV_FILE="$HOME/.config/multiagent-tools/env"
    mkdir -p "$(dirname "$ENV_FILE")"
    [ ! -f "$ENV_FILE" ] && touch "$ENV_FILE" && chmod 600 "$ENV_FILE"

    if grep -q "^MULTIAGENT_URL=" "$ENV_FILE" 2>/dev/null; then
      current=$(grep "^MULTIAGENT_URL=" "$ENV_FILE" | head -1 | cut -d= -f2-)
      echo "  ok MULTIAGENT_URL already set: $current"
    else
      printf "  >> MULTIAGENT_URL not set. Enter the multiagent-tools URL\n"
      printf "     (e.g. http://your-server:5005 or https://your-server.example):\n  > "
      read -r url || url=""
      if [ -n "$url" ]; then
        echo "MULTIAGENT_URL=$url" >> "$ENV_FILE"
        echo "     wrote MULTIAGENT_URL=$url to $ENV_FILE"
      else
        echo "     no URL entered — CLI will run in local-fs mode (no remote data)"
      fi
    fi

    echo
    echo "macOS install done."
    echo "  - To enable HTTP mode, add this to your shell rc:"
    echo "      [ -f \$HOME/.config/multiagent-tools/env ] && set -a && source \$HOME/.config/multiagent-tools/env && set +a"
    echo "  - Then: multiagent-tools memory list"
    ;;
  *)
    echo "unsupported OS: $OS" >&2
    exit 1
    ;;
esac
