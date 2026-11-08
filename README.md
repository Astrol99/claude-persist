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
  running conversation. It sends one key per check and reads the screen in between: Down while the cursor is on "No",
  Enter only once the cursor is drawn on "Yes". Claude draws the dialog before it reads keys, and keys sent together then
  arrive in one batch where Enter still sees the old cursor and picks "No, exit". Every pane is then checked against the expected session, and a summary can go to a Discord webhook.
- **Flags:** an allow-list of launch flags is carried over (including multi-value ones such as `--add-dir a b`);
  anything else is dropped and shown by `claude-persist status`.
- **Between boot and restore nothing is overwritten.** Restore writes a marker in `$XDG_RUNTIME_DIR` (emptied when the
  user manager stops); snapshots only run once it exists. After that a snapshot records whatever is there, so closing
  all your tmux sessions on purpose is remembered and a reboot does not bring them back.

## Use

```
claude-persist status               what a restore would bring back
claude-persist restore --dry-run    the plan, without doing anything
claude-persist snapshot             take a snapshot now (the timer does this every 2 minutes)
```

State and logs: `~/.local/state/claude-persist/`: `snapshot.json`, `snapshot.prev.json` (the previous distinct state),
`snapshot.pre-restore.json` (what the last restore started from), `pids/` (hook records), `claude-persist.log`, `hooks.log`.
`CLAUDE_CONFIG_DIR` is honoured.

## Install

```
./claude-persist install                 # this user: PATH link, hooks in ~/.claude/settings.json, systemd user units
sudo claude-persist install --global me  # a machine/container: units for every user, hooks in managed settings, lingering for "me"
```

- Boot-time restore needs lingering (`sudo loginctl enable-linger $USER`); `install` says so if it is off, and the
  restore unit does not run without it (a restored tmux would otherwise die at logout).
- `install --global` copies the script to a root-owned `/usr/local/bin/claude-persist` first, because every user's
  hooks and units run that file.
- **Incus dev containers (example for one particular setup):** `./provision-devbox.sh <name>...` or `--all` copies the script
  into containers named `dev-<name>` and runs `install --global <name>` inside each.
- Optional Discord summary after each restore: put a webhook URL in `~/.config/claude-persist/discord-webhook`
  (mode 600), or point `CLAUDE_PERSIST_WEBHOOK_FILE` at one.

## Test

- `python3 -m unittest discover -s tests` — fast (about 6s), no network and no real Claude: flag parsing, Claude
  detection, skip decisions, the hook's records (with a stand-in Claude process), settings-file merging, a
  snapshot/restore round trip on a separate tmux server, the trust-prompt answer against a stand-in dialog that reads
  keys late, and the restore's wait surviving a wall-clock jump.
- `tests/e2e.sh` — the real thing on a separate tmux server with two throwaway conversations (two or three tiny
  `claude -p --model haiku` calls, about 40s): snapshot, kill, restore, trust prompt, `/exit`, nested `claude -p`.
  It never touches real sessions.

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
