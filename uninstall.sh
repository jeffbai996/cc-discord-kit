#!/usr/bin/env bash
# cc-discord-kit module uninstaller. Reverses install.sh.
#
# Removes the CLI symlink and (Linux) systemd units. Restores settings.json
# from the .pre-cc-discord-kit backup if present. Data files are NEVER touched.

set -eo pipefail

OS="$(uname -s)"

echo "==> cc-discord-kit module uninstall"
echo

# 1. CLI symlink
LINK="$HOME/.local/bin/cc-discord-kit"
if [ -L "$LINK" ]; then
  rm "$LINK"
  echo "  -- removed $LINK"
fi

case "$OS" in
  Linux)
    # 2. Stop + disable + remove systemd units
    for unit in cc-discord-kit-server.service cc-discord-kit-discord.service; do
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
    BACKUP="$SETTINGS.pre-cc-discord-kit"
    if [ -f "$BACKUP" ]; then
      cp "$BACKUP" "$SETTINGS"
      echo "  -- restored $SETTINGS from $BACKUP"
      echo "     (backup left in place; remove manually with: rm $BACKUP)"
    else
      echo "  -- no settings.json backup found (was install ever run?)"
    fi
    ;;
  Darwin)
    echo "  -- $HOME/.config/cc-discord-kit/env left in place (delete manually if desired)"
    ;;
esac

echo
DATA_DIR="${CCDK_DATA_DIR:-$HOME/.local/share/cc-discord-kit}"
echo "Done. Data files at $DATA_DIR were not touched."
echo "If you want to also remove them: rm -rf \"$DATA_DIR\""
