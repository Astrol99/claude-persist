# claude-persist

Bring tmux sessions and the Claude Code conversations running in them back after a reboot.

After a reboot, `tmux attach` shows the same sessions and windows, and each Claude window has already reopened its
conversation: same session id, same folder, same launch flags. Sessions ended deliberately with `/exit` stay closed.

## Requirements

- Linux with systemd (user services, plus `loginctl enable-linger` so they run at boot without a login).
- tmux 3.x (developed on 3.4) and Python 3.8+ (standard library only).
- Claude Code with hooks support. The pid-file fallback reads `~/.claude/sessions/<pid>.json`, which is undocumented and
  may change between Claude Code versions; the hook path does not depend on it.

## How it works

- **Which conversation is in which pane:** a Claude Code `SessionStart` hook records, per Claude *process* (pid plus
  start time, so a reused pid never matches), which session it runs. It fires on startup, resume, clear, compact and fork,
  so it follows `/clear` and `/resume`, and a nested `claude -p` only records itself. The snapshot finds each pane's Claude
  through the terminal's foreground process. For processes started before the hook was installed it falls back to Claude's
  own `~/.claude/sessions/<pid>.json`. Nothing is guessed: a pane whose session is unknown, already running elsewhere, or
  has no saved conversation is reported, not resumed.
- **Snapshots** come from a systemd user timer every 2 minutes. Nothing is saved at shutdown on purpose: the user manager
  gets 5s to stop and tmux 3.4 stops panes in parallel, so a shutdown-time save can find Claude already gone.
- **Restore** runs once at boot (systemd user service; needs `loginctl enable-linger`). It creates only tmux sessions that
  do not exist yet and starts each pane with `$SHELL -lic 'cd <dir> && claude <flags> --resume <id>; exec $SHELL -l'`, so
  nothing is typed into a shell (a pane that *was* Claude closes on a normal exit, as before). A new tmux server gets its
  own systemd scope, not the restore service. Claude's folder-trust prompt (default "No, exit"; always shown for `$HOME`,
  whose trust Claude never keeps) is answered only before that pane's Claude reaches SessionStart, so keys never go into a
  running conversation. Every pane is then checked against the expected session, and a summary can go to a Discord webhook.
- **Flags:** an allow-list of launch flags is carried over (including multi-value ones such as `--add-dir a b`);
  anything else is dropped and shown by `claude-persist status`.
- A snapshot never replaces a good one when the tmux server is missing or empty (the gap between boot and restore).

## Use

```
claude-persist status               what a restore would bring back
claude-persist restore --dry-run    the plan, without doing anything
claude-persist snapshot             take a snapshot now (the timer does this every 2 minutes)
```

State and logs: `~/.local/state/claude-persist/` (`snapshot.json`, `snapshot.prev.json`, `pids/` hook records, `claude-persist.log`, `hooks.log`).
`CLAUDE_CONFIG_DIR` is honoured.

## Install

- **This machine, current user:** `./install-host.sh` (PATH link, hooks in `~/.claude/settings.json`, user units; the
  units and hook entries come from `claude-persist _write-units` / `_merge-hooks`).
- **Incus dev containers (example for one particular setup):** `./provision-devbox.sh <name>...` or `--all` assumes
  containers named `dev-<name>` with a user `<name>` inside; (binary in `/usr/local/bin`, units in
  `/etc/systemd/user`, hooks in `/etc/claude-code/managed-settings.json`, lingering for the dev user).
- Optional Discord summary after each restore: put a webhook URL in `~/.config/claude-persist/discord-webhook`
  (mode 600), or point `CLAUDE_PERSIST_WEBHOOK_FILE` at one.

## Test

`tests/e2e.sh` runs a full snapshot, kill and restore cycle on a separate tmux server with two throwaway conversations
(two tiny `claude -p --model haiku` calls). It never touches real sessions.

## Limits

- **Tested by simulation, not yet across many real reboots.** `tests/e2e.sh` kills a tmux server and restores it, and the
  boot path was exercised through a transient systemd service. Treat the first real reboot as a test: check
  `claude-persist status` before it and the restore summary (or `~/.local/state/claude-persist/claude-persist.log`) after.
- **The folder-trust answer reads Claude's screen.** It looks for the dialog's wording ("Yes, I trust this folder",
  "Enter to confirm"). If a Claude Code release changes that text, those panes are reported as not resumed instead.
  It only ever comes up for `$HOME` or for folders where Claude never ran interactively.
- Anything mid-flight is lost: a running tool call, dev servers, test runs, background shells.
- The first message after a resume re-reads the whole conversation (cold prompt cache).
- Only tmux panes are covered. Sessions in the VS Code extension, Zellij, or plain terminals are not.
- Sessions Claude Code itself deletes (transcripts older than `cleanupPeriodDays`, 30 days by default) cannot be resumed.

## License

MIT, see [LICENSE](LICENSE).
