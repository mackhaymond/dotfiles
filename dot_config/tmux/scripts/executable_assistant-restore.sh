#!/usr/bin/env bash
# Post-restore hook: resume every saved assistant session. Replaces
# tmux-assistant-resurrect's restore-assistant-sessions.sh (same sidecar, same
# resume commands, same two guards, same log) because its pacing made a
# restore take minutes:
#
#   * `sleep 2` up front, then PER PANE a wait of up to 5s for a client to
#     attach to that pane's tmux session, `sleep 0.3` after clearing, and
#     `sleep 1` after launching. At login nothing is attached to most sessions
#     (often not even `main` yet), so every agent paid the full 5s: ~7s each,
#     serially — 86s for 11 agents on 2026-10-06, per its own log.
#   * That client wait cannot help a session nobody is looking at: tmux 3.7
#     answers a pane's OSC 10/11 colour query only through a client attached
#     to a session containing that window (window_get_bg_client()). So here it
#     is ONE bounded wait for the first client, shared by the whole restore,
#     and sessions with a client go first.
#
# Readiness is polled instead of slept on: a pane is ready once it runs a shell
# AND that shell has drawn its prompt (cursor off the origin) — keys sent
# earlier could be eaten by rc-file init.

set -uo pipefail

PLUGIN_DIR="${TMUX_ASSISTANT_RESURRECT_PLUGIN:-$HOME/.config/tmux/plugins/tmux-assistant-resurrect}"
# shellcheck source=/dev/null
source "$PLUGIN_DIR/scripts/lib-detect.sh" || exit 1 # detect_tool, pane_has_assistant, posix_quote

RESURRECT_DIR="$(tmux show-option -gqv @resurrect-dir 2>/dev/null)"
RESURRECT_DIR="${RESURRECT_DIR:-$HOME/.tmux/resurrect}"
RESURRECT_DIR="${RESURRECT_DIR/#\~/$HOME}"
INPUT_FILE="$RESURRECT_DIR/assistant-sessions.json"
LOG_FILE="$RESURRECT_DIR/assistant-restore.log"
EXCLUDE="$(tmux show-option -gqv @resurrect-exclude-sessions 2>/dev/null)"
EXCLUDE="${EXCLUDE:-agents tasks}"
CLIENT_WAIT="$(tmux show-option -gqv @assistant-resurrect-client-wait 2>/dev/null)"
CLIENT_WAIT="${CLIENT_WAIT:-3}" # seconds, once per restore
SHELL_WAIT=20                   # seconds, once per restore
US=$'\x1f'

if [ -f "$LOG_FILE" ]; then
	tail -n 500 "$LOG_FILE" >"$LOG_FILE.tmp" 2>/dev/null && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi
log() {
	local msg="[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
	echo "$msg" >&2
	echo "$msg" >>"$LOG_FILE"
}
elapsed() { awk -v s="$START" -v e="$EPOCHREALTIME" 'BEGIN { printf "%.1f", e - s }'; }

[ -f "$INPUT_FILE" ] || { log "no saved sessions found at $INPUT_FILE"; exit 0; }
START=$EPOCHREALTIME

# Only user-chosen env vars are replayed (never tmux_pane/shell); names are
# validated here so nothing from the sidecar reaches the command line unquoted.
capture_env=()
for var in $(tmux show-option -gqv @assistant-resurrect-capture-env 2>/dev/null); do
	if [[ "$var" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
		capture_env+=("$var")
	else
		log "skipping invalid env var name: $var"
	fi
done
capture_json="$(printf '%s\n' "${capture_env[@]+"${capture_env[@]}"}" | jq -Rsc 'split("\n") | map(select(length > 0))')"

# One jq pass builds every resume command — the strings upstream built with ~8
# jq forks per entry. q is posix_quote(). cli_args are whitespace-split and
# quoted per token, so a glob like `claude-opus-5-5[1m]` stays literal. Fields
# are \x1f-joined so an empty one survives `read`.
#
# Codex rows drop every saved `-c model_context_window=…` (ctxdrop): the wrapper
# prepends a fresh one, and a saved copy later in argv would win and pile up
# one more per save/restore. Same spellings as context_override() in
# resurrect-save-repair.py; value-taking flags keep their operand (VALUE_FLAGS
# there, also mirrored in closed-tabs.sh build_cmd) and nothing after `--` is
# touched.
read -r -d '' JQ_PROG <<'JQ'
def q: "'" + gsub("'"; "'\"'\"'") + "'";
def vflag: . as $w | any(("-c", "--config", "--enable", "--disable", "-C", "--cd", "-m", "--model",
  "-p", "--profile", "-s", "--sandbox", "-a", "--ask-for-approval", "-i", "--image", "--add-dir",
  "--local-provider", "--remote"); . == $w);
def mcw: split("=")[0] == "model_context_window";
def ctxdrop:
  reduce .[] as $w ({out: [], pend: null, lit: false};
    if .lit then .out += [$w]
    elif .pend != null then
      (if (.pend == "-c" or .pend == "--config") and ($w | mcw) then .out |= .[:-1] else .out += [$w] end)
      | .pend = null
    elif $w == "--" then .out += [$w] | .lit = true
    elif ($w | vflag) then .out += [$w] | .pend = $w
    elif ($w | test("^(-c|--config)=")) and ($w | sub("^[^=]*="; "") | mcw) then .
    elif ($w | test("^-c[^-=]")) and ($w[2:] | mcw) then .
    else .out += [$w] end)
  | .out;
(.sessions // [])[]
| . as $e
| ((.cli_args // "") | [splits("[ \t\n]+")] | map(select(length > 0))
   | if $e.tool == "codex" then ctxdrop else . end | map(" " + q) | join("")) as $args
| (if (.model // "") != "" and .tool == "claude" and ((.cli_args // "") | contains("--model") | not)
   then " --model " + (.model | q) else "" end) as $model
| ([$cap[] as $v | (($e.env // {})[$v] // "") | tostring
    | select(. != "" and . != "null") | $v + "=" + q + " "] | join("")) as $envp
| (if .tool == "claude" then "command claude" + $args + $model + " --resume " + (.session_id | q)
   elif .tool == "opencode" then "command opencode" + $args + " -s " + (.session_id | q)
   elif .tool == "codex" then $codex + $args + " resume " + (.session_id | q)
   else "" end) as $cmd
| [.pane, .tool, .session_id, (.cwd // ""), (if $cmd == "" then "" else $envp + $cmd end)]
| join("\u001f")
JQ
# Codex resumes through the ~/.local/bin wrapper by path: it adds --no-daemon,
# which the tab/notch binding relies on, and a restored pane's PATH can put
# ~/.bun/bin's raw CLI first.
codex_cmd="command codex"
[ -x "$HOME/.local/bin/codex" ] && codex_cmd="$(posix_quote "$HOME/.local/bin/codex")"
rows="$(jq -r --argjson cap "$capture_json" --arg codex "$codex_cmd" "$JQ_PROG" "$INPUT_FILE")" || { log "could not parse $INPUT_FILE"; exit 0; }

count="$(printf '%s\n' "$rows" | grep -c . || true)"
[ "${count:-0}" -gt 0 ] || { log "no assistant sessions to restore"; exit 0; }
log "restoring $count assistant session(s)..."

is_shell() {
	case "${1#-}" in bash | zsh | fish | sh | dash | ksh | tcsh | csh | nu) return 0 ;; esac
	return 1
}

# --- one bounded wait for the first client -----------------------------------
waited=0
while [ -z "$(tmux list-clients -F x 2>/dev/null)" ] && [ "$waited" -lt $((CLIENT_WAIT * 10)) ]; do
	sleep 0.1
	waited=$((waited + 1))
done
[ "$waited" -ge $((CLIENT_WAIT * 10)) ] && log "no client attached after ${CLIENT_WAIT}s; resuming anyway (TUI colour queries may go unanswered)"
attached=" $(tmux list-clients -F '#{client_session}' 2>/dev/null | tr '\n' ' ') "

# --- pick the panes, attached sessions first ------------------------------------
first="" rest=""
while IFS="$US" read -r pane tool sid cwd cmd; do
	[ -n "$pane" ] || continue
	sess="${pane%%:*}"
	case " $EXCLUDE " in *" $sess "*) log "session '$sess' is excluded, skipping $pane"; continue ;; esac
	if [ -z "$cmd" ]; then
		log "unknown tool '$tool' for pane $pane, skipping"
		continue
	fi
	line="${pane}${US}${tool}${US}${sid}${US}${cwd}${US}${cmd}"$'\n'
	case "$attached" in *" $sess "*) first+="$line" ;; *) rest+="$line" ;; esac
done <<<"$rows"
todo="${first}${rest}"

# --- wait (once, bounded) until those panes sit at a shell prompt ------------------
pane_row() { printf '%s\n' "$live" | awk -F"$US" -v p="$1" '$1 == p { print; exit }'; }
waited=0
while :; do
	live="$(tmux list-panes -a -F "#{session_name}:#{window_index}.#{pane_index}${US}#{pane_current_command}${US}#{pane_pid}${US}#{cursor_x}${US}#{cursor_y}" 2>/dev/null)"
	pending=0
	while IFS="$US" read -r pane _rest; do
		[ -n "$pane" ] || continue
		IFS="$US" read -r _p pcmd _pid cx cy <<<"$(pane_row "$pane")"
		[ -n "$pcmd" ] || continue # gone; reported below
		# Still replaying old contents (resurrect starts panes as
		# `cat <contents>; exec zsh`), or a shell yet to print its prompt.
		# Anything else is settled: guard 1 below skips a non-shell.
		if [ "$pcmd" = cat ] || { is_shell "$pcmd" && [ "${cx:-0}" -eq 0 ] && [ "${cy:-0}" -eq 0 ]; }; then
			pending=$((pending + 1))
		fi
	done <<<"$todo"
	[ "$pending" -eq 0 ] && break
	if [ "$waited" -ge $((SHELL_WAIT * 10)) ]; then
		log "$pending pane(s) still not at a prompt after ${SHELL_WAIT}s; resuming anyway"
		break
	fi
	sleep 0.1
	waited=$((waited + 1))
done
snapshot="$(ps -eo pid=,ppid=,args= 2>/dev/null)"
log "panes ready after $(elapsed)s"

# --- resume ------------------------------------------------------------------------
restored=0
while IFS="$US" read -r pane tool sid cwd cmd; do
	[ -n "$pane" ] || continue
	sess="${pane%%:*}"
	if ! tmux has-session -t "=$sess" 2>/dev/null; then
		log "session '$sess' does not exist, skipping pane $pane"
		continue
	fi
	row="$(pane_row "$pane")"
	if [ -z "$row" ]; then
		log "pane $pane does not exist, skipping"
		continue
	fi
	IFS="$US" read -r _p pane_cmd pane_pid _cx _cy <<<"$row"

	# Guard 1: only ever type into a shell.
	if ! is_shell "$pane_cmd"; then
		log "pane $pane is running '$pane_cmd' (not a shell), skipping"
		continue
	fi
	# Guard 2: never start a second assistant in a pane that has one.
	if existing="$(pane_has_assistant "$pane_pid" "$snapshot")" && [ -n "$existing" ]; then
		log "pane $pane already has a running assistant (pid $existing), skipping"
		continue
	fi

	line="$cmd"
	[ -n "$cwd" ] && [ "$cwd" != "null" ] && line="cd $(posix_quote "$cwd") 2>/dev/null; $cmd"
	log "restoring $tool in $pane (session: $sid, cmd: $cmd)"
	# resurrect may have replayed old pane text; wipe it so the TUI starts clean.
	tmux clear-history -t "$pane" 2>/dev/null
	tmux send-keys -t "$pane" -l "clear; $line" 2>/dev/null && tmux send-keys -t "$pane" Enter 2>/dev/null
	restored=$((restored + 1))
	# A tenth of a second apart: enough that a dozen node TUIs do not all cold
	# start in the same instant, nowhere near upstream's 1.3s each.
	sleep 0.1
done <<<"$todo"

log "restored $restored of $count assistant session(s) in $(elapsed)s"
