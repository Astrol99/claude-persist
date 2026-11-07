#!/usr/bin/env bash
# Install claude-persist into Incus dev containers (container dev-<name>, user <name>). Idempotent; run from the host.
#   ./provision-devbox.sh <name> [<name>...]
#   ./provision-devbox.sh --all            every dev-* container
# Per box: /usr/local/bin/claude-persist, systemd user units in /etc/systemd/user (enabled with --global), the
# SessionStart/SessionEnd hooks in /etc/claude-code/managed-settings.json (the dev's own settings are not touched),
# lingering for the dev user so the units run at boot, the snapshot timer started, and a first snapshot.
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
INCUS="incus"; incus info >/dev/null 2>&1 || INCUS="sudo incus"
if [ "${1:-}" = "--all" ]; then
  mapfile -t NAMES < <($INCUS list -c n --format csv | sed -n 's/^dev-//p')
else NAMES=("$@"); fi
[ "${#NAMES[@]}" -gt 0 ] || { echo "usage: $0 <name>... | --all" >&2; exit 1; }
for NAME in "${NAMES[@]}"; do
  printf '%s' "$NAME" | grep -Eq '^[a-z][a-z0-9-]{0,30}$' || { echo "ERROR: bad name '$NAME'" >&2; exit 1; }
  CT="dev-$NAME"
  info=$($INCUS info "$CT" 2>/dev/null) || { echo "ERROR: no container $CT" >&2; exit 1; }
  grep -qi 'status:.*running' <<<"$info" || { echo "ERROR: $CT is not running" >&2; exit 1; }
  echo "== $CT (user $NAME) =="
  $INCUS file push "$HERE/claude-persist" "$CT/usr/local/bin/claude-persist" --uid 0 --gid 0 --mode 0755 >/dev/null
  $INCUS exec "$CT" --env U="$NAME" -- bash -c '
    set -e
    /usr/local/bin/claude-persist _write-units /etc/systemd/user /usr/local/bin/claude-persist >/dev/null
    systemctl --global enable claude-persist-restore.service claude-persist-snapshot.timer 2>/dev/null
    install -d -m 0755 /etc/claude-code
    /usr/local/bin/claude-persist _merge-hooks /etc/claude-code/managed-settings.json /usr/local/bin/claude-persist
    chmod 0644 /etc/claude-code/managed-settings.json
    loginctl enable-linger "$U"
    for i in $(seq 1 20); do systemctl --user -M "$U@" is-system-running >/dev/null 2>&1 && break; sleep 0.5; done
    systemctl --user -M "$U@" daemon-reload
    systemctl --user -M "$U@" start claude-persist-snapshot.timer
    echo "  linger=$(loginctl show-user "$U" -p Linger --value)  timer=$(systemctl --user -M "$U@" is-active claude-persist-snapshot.timer)  restore-at-boot=$(systemctl --user -M "$U@" is-enabled claude-persist-restore.service)"
    su - "$U" -c "claude-persist snapshot; claude-persist status | head -1" | sed "s/^/  /"'
done
