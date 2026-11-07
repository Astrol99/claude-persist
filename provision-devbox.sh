#!/usr/bin/env bash
# Example: install claude-persist into Incus dev containers named dev-<name> (user <name> inside). Run from the host.
#   ./provision-devbox.sh <name> [<name>...]      or      ./provision-devbox.sh --all
# Copies the script to /usr/local/bin and runs `claude-persist install --global <name>` inside the container.
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
INCUS="incus"; incus info >/dev/null 2>&1 || INCUS="sudo incus"
if [ "${1:-}" = "--all" ]; then mapfile -t NAMES < <($INCUS list -c n --format csv | sed -n 's/^dev-//p'); else NAMES=("$@"); fi
[ "${#NAMES[@]}" -gt 0 ] || { echo "usage: $0 <name>... | --all" >&2; exit 1; }
for NAME in "${NAMES[@]}"; do
  printf '%s' "$NAME" | grep -Eq '^[a-z][a-z0-9-]{0,30}$' || { echo "ERROR: bad name '$NAME'" >&2; exit 1; }
  CT="dev-$NAME"
  info=$($INCUS info "$CT" 2>/dev/null) || { echo "ERROR: no container $CT" >&2; exit 1; }
  grep -qi 'status:.*running' <<<"$info" || { echo "ERROR: $CT is not running" >&2; exit 1; }
  echo "== $CT (user $NAME) =="
  $INCUS file push "$HERE/claude-persist" "$CT/usr/local/bin/claude-persist" --uid 0 --gid 0 --mode 0755 >/dev/null
  $INCUS exec "$CT" -- /usr/local/bin/claude-persist install --global "$NAME" 2>&1 | sed 's/^/  /'
done
