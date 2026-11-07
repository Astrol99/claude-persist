#!/usr/bin/env bash
# The checks are eval'd strings, so to shellcheck their variables look unused and their $(...) unexpanded:
# shellcheck disable=SC2016,SC2034
# End-to-end test without a reboot. Uses a SEPARATE tmux server (-L cptest) and two throwaway conversations created
# with two tiny `claude -p --model haiku` calls in scratch folders (deleted afterwards). Never touches real sessions.
# Covers: snapshot -> kill server ("reboot") -> restore; same session ids; window names/indices/cwd; two-pane layout
# geometry; automatic-rename kept; Claude-as-pane-command restored as pane command; folder-trust prompt answered;
# idempotent re-run; partial loss; /exit (prompt_input_exit) dropped vs kill ("other") kept.
set -uo pipefail
CP="$(cd "$(dirname "$0")/.." && pwd)/claude-persist"
W=$(mktemp -d /tmp/cp-e2e.XXXXXX); S="tmux -L cptest"
export CLAUDE_PERSIST_SOCKET=cptest CLAUDE_PERSIST_STATE="$W/state" CLAUDE_PERSIST_NOTIFY=0 CLAUDE_PERSIST_VERIFY_WAIT=60
pass=0; fail=0
ok()  { echo "  PASS $*"; pass=$((pass+1)); }
bad() { echo "  FAIL $*"; fail=$((fail+1)); }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
cleanup() {
  $S kill-server 2>/dev/null
  for _ in $(seq 1 20); do pgrep -u "$USER" -f -- "--resume (${A:-x}|${B:-x})" >/dev/null || break; sleep 0.5; done   # let dying Claudes finish
  for sid in ${A:-} ${B:-}; do for t in ~/.claude/projects/*/"$sid".jsonl; do case "$t" in *cp-e2e*) rm -rf "$(dirname "$t")";; esac; done; done
  rm -rf "$W"
}
trap cleanup EXIT
sid_of() { "$CP" _pane-session "$1" | cut -d" " -f1; }       # session id the snapshot would record for a pane
src_of() { "$CP" _pane-session "$1" | cut -d" " -f2; }
wait_claude() {  # wait until the pane's Claude reached SessionStart (past any trust prompt)
  for _ in $(seq 1 30); do
    "$CP" _answer-trust "$1" >/dev/null
    [ "$(src_of "$1")" = hook ] && return 0; sleep 1
  done; return 1
}
mkdir -p "$W/alpha" "$W/beta" "$W/state/pids"
A=$(python3 -c 'import uuid;print(uuid.uuid4())'); B=$(python3 -c 'import uuid;print(uuid.uuid4())')
(cd "$W/alpha" && env -u TMUX -u TMUX_PANE claude -p --model haiku --session-id "$A" "Reply with exactly: ok" </dev/null >/dev/null)
(cd "$W/beta"  && env -u TMUX -u TMUX_PANE claude -p --model haiku --session-id "$B" "Reply with exactly: ok" </dev/null >/dev/null)
$S kill-server 2>/dev/null
$S new-session -d -s t1 -n alpha-win -x 200 -y 50 -c "$W/alpha"
$S send-keys -t t1:0 "claude --dangerously-skip-permissions --model haiku --resume $A" Enter
$S new-window -d -t t1:3 -n twopanes -c /tmp; $S split-window -d -h -t t1:3 -c "$W"; $S select-layout -t t1:3 main-vertical
$S new-session -d -s cc-beta -x 200 -y 50 -c "$W/beta" "claude --model haiku --resume $B"
P_A=$($S display-message -p -t t1:0 '#{pane_id}'); P_B=$($S display-message -p -t cc-beta:0 '#{pane_id}')
check "both test sessions running (trust prompts answered)" 'wait_claude "$P_A" && wait_claude "$P_B"'
check "hook recorded the right session per process" '[ "$("$CP" _pane-session "$P_A")" = "$A hook" ] && [ "$("$CP" _pane-session "$P_B")" = "$B hook" ]'
"$CP" snapshot
layout_before=$(python3 -c "import json;d=json.load(open('$W/state/snapshot.json'));print([w for s in d['sessions'] for w in s['windows'] if w['index']==3][0]['layout'])")
$S kill-server; sleep 4
check "no claude left after the 'reboot'" '! pgrep -u "$USER" -f -- "--resume ($A|$B)" >/dev/null'
out=$("$CP" restore); echo "  restore: $out"
check "restore resumed 2/2" '[[ "$out" == *"Claude resumed 2/2"* ]]'
check "alpha resumed with id A in its folder" '[ "$(sid_of t1:0)" = "$A" ] && [ "$($S display-message -p -t t1:0 "#{pane_current_path}")" = "$W/alpha" ]'
check "beta resumed as the pane command" '[ "$(sid_of cc-beta:0)" = "$B" ] && [ "$($S display-message -p -t cc-beta:0 "#{pane_current_command}")" = "claude" ]'
"$CP" snapshot
check "pane-command style survives a second reboot (recorded again)" 'python3 -c "import json,sys;d=json.load(open(\"$W/state/snapshot.json\"));p=[p for s in d[\"sessions\"] if s[\"name\"]==\"cc-beta\" for w in s[\"windows\"] for p in w[\"panes\"]][0];sys.exit(0 if p[\"claude\"][\"pane_command\"] else 1)"'
P_A2=$($S display-message -p -t t1:0 '#{pane_id}')
check "a nested claude -p claiming the same pane does not change the recorded session" '$S send-keys -t t1:3.0 "cd $W/alpha && TMUX_PANE=$P_A2 claude -p --model haiku \"Reply with exactly: ok\" >/dev/null; tmux -L cptest wait-for -S nested-done" Enter; timeout 90 $S wait-for nested-done; [ "$(sid_of t1:0)" = "$A" ] && [ "$(src_of t1:0)" = hook ]'
check "window indices and names kept" '[ "$($S list-windows -t t1 -F "#{window_index}:#{window_name}" | tr "\n" " ")" = "0:alpha-win 3:twopanes " ]'
strip() { echo "$1" | sed -E 's/^[0-9a-f]{4},//; s/,[0-9]+([},]|$)/,#\1/g'; }
check "two-pane layout geometry kept" '[ "$(strip "$layout_before")" = "$(strip "$($S display-message -p -t t1:3 "#{window_layout}")")" ]'
check "automatic-rename kept (off stays off, automatic stays automatic)" '[ "$($S display-message -p -t t1:0 "#{automatic-rename}")" = 0 ] && [ "$($S display-message -p -t cc-beta:0 "#{automatic-rename}")" = 1 ]'
check "re-run is idempotent" '[[ "$("$CP" restore --dry-run)" == *"skip session t1: already exists"* ]]'
$S kill-session -t t1; sleep 3
out=$("$CP" restore)
check "partial loss: only the missing session comes back" '[[ "$out" == *"restored 1 tmux session(s); Claude resumed 1/1"* ]]'
"$CP" snapshot
$S send-keys -t t1:0 "/exit" Enter; sleep 6
"$CP" snapshot
check "/exit drops the conversation from the snapshot" '"$CP" status | grep -q "t1:0.0 .* shell"'
check "SessionEnd reasons logged (other on kill, prompt_input_exit on /exit)" 'grep -q "end other" "$W/state/hooks.log" && grep -q "end prompt_input_exit" "$W/state/hooks.log"'
echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
