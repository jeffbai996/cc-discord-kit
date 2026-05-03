#!/usr/bin/env bash
# multiagent-tools module uninstaller. Reverses install.sh.
#
# Removes the CLI symlink and (Linux) systemd units. Restores settings.json
# from the .pre-multiagent-tools backup if present. Data files are NEVER touched.

set -eo pipefail

OS="$(uname -s)"

echo "==> multiagent-tools module uninstall"
echo

# 1. CLI symlink
LINK="$HOME/.local/bin/multiagent-tools"
if [ -L "$LINK" ]; then
  rm "$LINK"
  echo "  -- removed $LINK"
fi

case "$OS" in
  Linux)
    # 2. Stop + disable + remove systemd units
    for unit in multiagent-tools-server.service multiagent-tools-discord.service; do
      if systemctl --user is-enabled "$unit" >/dev/null 2>&1; then
        systemctl --user disable --now "$unit" 2>&1 | sed 's/^/  /'
      fi
      f="$HOME/.config/systemd/user/$unit"
      if [ -f "$f" ]; then
        rm "$f"
        echo "  -- removed $f"
      fi
    done
    systemctl --user daemon-reload 2>/dev/null || true

    # 3. Restore settings.json
    SETTINGS="$HOME/.claude/settings.json"
    BACKUP="$SETTINGS.pre-multiagent-tools"
    if [ -f "$BACKUP" ]; then
      cp "$BACKUP" "$SETTINGS"
      echo "  -- restored $SETTINGS from $BACKUP"
      echo "     (backup left in place; remove manually with: rm $BACKUP)"
    else
      echo "  -- no settings.json backup found (was install ever run?)"
    fi
    ;;
  Darwin)
    echo "  -- $HOME/.config/multiagent-tools/env left in place (delete manually if desired)"
    ;;
esac

echo
DATA_DIR="${MULTIAGENT_DATA_DIR:-$HOME/.local/share/multiagent-tools}"
echo "Done. Data files at $DATA_DIR were not touched."
echo "If you want to also remove them: rm -rf \"$DATA_DIR\""
