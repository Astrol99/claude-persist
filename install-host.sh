#!/usr/bin/env bash
# Install claude-persist for the CURRENT user on a host (no sudo): PATH link, Claude hooks, systemd user units.
# Needs lingering so the units run at boot without a login:  loginctl show-user "$USER" -p Linger
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
X="$HOME/.local/bin/claude-persist"
ln -sfn "$HERE/claude-persist" "$X"
"$X" _merge-hooks "$HOME/.claude/settings.json" '$HOME/.local/bin/claude-persist'
"$X" _write-units "$HOME/.config/systemd/user" '%h/.local/bin/claude-persist'
systemctl --user daemon-reload
systemctl --user enable claude-persist-restore.service          # runs at next boot, not now
systemctl --user enable --now claude-persist-snapshot.timer
"$X" snapshot
echo "linger: $(loginctl show-user "$USER" -p Linger --value 2>/dev/null)"
"$X" status
