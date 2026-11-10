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
# Upstream's detect_tool knows claude/codex/opencode only; guard 2 below must
# also see a grok already running in a pane (argv0 basename grok or
# grok-<os>-<arch>, any mode: a pane busy with `grok -p` is not free either),
# and a copilot or agy (argv0 basename, any mode too).
eval "_upstream_detect_tool() $(declare -f detect_tool | tail -n +2)"
detect_tool() {
	local w="${1%%[[:space:]]*}"
	if [[ "${w##*/}" =~ ^grok(-(macos|linux|windows)-[a-z0-9_]+)?$ ]]; then
		echo grok
	elif [ "${w##*/}" = copilot ] || [ "${w##*/}" = agy ]; then
		echo "${w##*/}"
	else
		_upstream_detect_tool "$1"
	fi
}

RESURRECT_DIR="$(tmux show-option -gqv @resurrect-dir 2>/dev/null)"
RESURRECT_DIR="${RESURRECT_DIR:-$HOME/.tmux/resurrect}"
RESURRECT_DIR="${RESURRECT_DIR/#\~/$HOME}"
INPUT_FILE="$RESURRECT_DIR/assistant-sessions.json"
LOG_FILE="$RESURRECT_DIR/assistant-restore.log"

# Only ever restore into THE default tmux server. Any server that loads
# tmux.conf runs this as its post-restore hook: on 2026-10-08 and again on
# 2026-11-02 a subagent's test server started without `-f /dev/null`,
# continuum restored the real snapshot into it, and this resumed every live
# Claude session a second time inside the test server. #{socket_path} is the
# server this script was invoked for (the hook's $TMUX; by hand, whatever the
# CLI reaches). "Default" is /private/tmp/tmux-$UID/default by resolved path
# (/tmp is a link to it). Deliberately NOT ${TMUX_TMPDIR}/tmux-$UID/default:
# agents' private servers live at exactly that under their own TMUX_TMPDIR,
# and their hooks inherit it, so honouring it would admit the very servers
# this guard is for. Same rule as resurrect-save.sh's socket_ok, and the same
# opt-in for test harnesses: RESURRECT_SAVE_ALLOW_SOCKET=<that exact socket>.
resolved_path() { # <path>: its directory with symlinks resolved, + basename
	local d
	d="$(cd -P "${1%/*}" 2>/dev/null && pwd -P)" || return 1
	printf '%s/%s' "$d" "${1##*/}"
}
SOCKET="$(tmux display-message -p '#{socket_path}' 2>/dev/null)"
sock_real="$(resolved_path "$SOCKET")" || sock_real=""
default_real="$(resolved_path "/private/tmp/tmux-$UID/default")" || default_real="/private/tmp/tmux-$UID/default"
allow_real=""
[ -n "${RESURRECT_SAVE_ALLOW_SOCKET:-}" ] && allow_real="$(resolved_path "$RESURRECT_SAVE_ALLOW_SOCKET")"
if [ -z "$sock_real" ] || { [ "$sock_real" != "$default_real" ] && [ "$sock_real" != "$allow_real" ]; }; then
	echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] not the default server: ${SOCKET:-(tmux unreachable)}, restore skipped" \
		| tee -a "$LOG_FILE" >&2 2>/dev/null
	exit 0
fi

EXCLUDE="$(tmux show-option -gqv @resurrect-exclude-sessions 2>/dev/null)"
EXCLUDE="${EXCLUDE:-agents tasks}"
# Entries are shell `case` patterns (`ptymcp-*`), as in resurrect-save.sh; an
# exact name is its own pattern.
is_excluded() {
	local p IFS=$' \t\n'
	set -f
	for p in $EXCLUDE; do
		# shellcheck disable=SC2254 # a pattern on purpose
		case "$1" in $p) set +f; return 0 ;; esac
	done
	set +f
	return 1
}
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

# Undo the save's blank-title placeholder. resurrect-save.sh writes an empty
# #{pane_title} as ':' so the plugin's whitespace-IFS read keeps its columns,
# but restore.sh applies the title as-is (`select-pane -T ":"`): restored
# shells came back titled ':' (three stash shells on 2026-10-18), and the next
# save kept it, for good. This hook is the first of post-restore-all, so it
# blanks every pane titled exactly ':' in one tmux call, before anything reads
# them. Runs whatever the sidecar holds.
untitle=()
while IFS="$US" read -r t_pane t_title; do
	[ "$t_title" = ":" ] || continue
	[ "${#untitle[@]}" -gt 0 ] && untitle+=(";")
	untitle+=(select-pane -t "$t_pane" -T "")
done < <(tmux list-panes -a -F "#{pane_id}${US}#{pane_title}" 2>/dev/null)
[ "${#untitle[@]}" -gt 0 ] && tmux "${untitle[@]}" 2>/dev/null

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
def nq: if type != "string" or contains("'###") then error("unquotable") else "r###'" + . + "'###" end;
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
# Claude rows keep only their leading options (claudecut). ps prints argv
# space-joined and upstream strips only the binary and the session flags, so a
# launch prompt (`claude "fix the login bug"`, an Ask Claude space's sentence)
# was saved as cli_args and replayed word by word on every restore: extra
# positionals, a new turn, or a subcommand (`update …`) run instead of the
# resume. So a value-taking flag keeps its one operand, and the first bare word
# or `--` ends the options: it and everything after are dropped. Same list as
# build_cmd in closed-tabs.sh.
def cvflag: . as $w | any(("--model", "--permission-mode", "--add-dir", "--allowedTools", "--allowed-tools",
  "--disallowedTools", "--disallowed-tools", "--tools", "--append-system-prompt", "--system-prompt",
  "--append-system-prompt-file", "--system-prompt-file", "--settings", "--setting-sources", "--mcp-config",
  "--agents", "--agent", "--fallback-model", "--session-id", "--output-format", "--input-format",
  "--max-turns", "--betas", "--plugin-dir"); . == $w);
def claudecut:
  reduce .[] as $w ({out: [], pend: false, stop: false};
    if .stop then .
    elif .pend then .out += [$w] | .pend = false
    elif $w == "--" or ($w | startswith("-") | not) then .stop = true
    elif ($w | cvflag) then .out += [$w] | .pend = true
    else .out += [$w] end)
  | .out;
# Grok rows (assistant-save.sh's post-pass) keep their leading options too, and
# lose whatever would fight the `-r <sid>` appended here: -r/--resume and
# -w/--worktree with their optional value (a next word that is no option),
# -c/--continue, -s/--session-id, --worktree-ref/--ref and --cwd with theirs,
# and the one-shot --fork-session (a new id on every restore) and
# --restore-code. The line cd's into the session's cwd first, so --cwd (and a
# relative --add-dir, which is dropped too) would resolve against the wrong
# directory; an absolute --add-dir stays. Same rules as build_cmd in
# closed-tabs.sh; value-taking flags = the TUI filter's list.
def gvflag: . as $w | any(("--agent", "--agents", "--allow", "--debug-file", "--deny",
  "--disallowed-tools", "--json-schema", "--leader-socket", "-m", "--model", "--max-turns", "--output-format",
  "--permission-mode", "--reasoning-effort", "--effort", "--rules", "--sandbox", "--system-prompt-override",
  "--system-prompt", "--tools"); . == $w);
def grokcut:
  reduce .[] as $w ({out: [], pend: false, skip: false, opt: false, add: false, stop: false};
    if .stop then .
    elif .pend then .out += [$w] | .pend = false
    elif .skip then .skip = false
    elif .add then (if $w | startswith("/") then .out += ["--add-dir", $w] else . end) | .add = false
    elif .opt and ($w | startswith("-") | not) then .opt = false
    elif $w == "--" or ($w | startswith("-") | not) then .stop = true
    elif any(("-r", "--resume", "-w", "--worktree"); . == $w) then .opt = true
    elif any(("-s", "--session-id", "--worktree-ref", "--ref", "--cwd"); . == $w) then .skip = true | .opt = false
    elif $w == "--add-dir" then .add = true | .opt = false
    elif any(("-c", "--continue", "--fork-session", "--restore-code"); . == $w) then .opt = false
    elif $w | test("^--add-dir=/") then .out += [$w] | .opt = false
    elif $w | test("^--(resume|worktree|session-id|worktree-ref|ref|cwd|add-dir)=") then .opt = false
    elif ($w | gvflag) then .out += [$w] | .pend = true | .opt = false
    else .out += [$w] | .opt = false end)
  | .out;
# Copilot rows (assistant-save.sh's ca_rows) keep their options, minus what
# would fight the `--resume <sid>` appended here: -r/--resume and --connect
# with their optional value, --session-id, -n/--name and -C with theirs (the
# line cd's into the session's cwd), --continue, --fleet and --attachment;
# -i/--interactive (a launch prompt, its words unquoted by ps) and `--` end it
# all. Copilot takes no positional argument, so a bare word is always the
# value of the option before it: kept or dropped with that option (several,
# for --allow-tool a b). ps loses argv's quoting, so a value that held a
# space comes back split (`--allow-tool 'shell(git push)'` reads `shell(git`
# `push)`): an option any of whose words has unbalanced parentheses is dropped
# whole rather than replayed as different tools. (A split value without
# parentheses cannot be told apart and is replayed as separate words.) Same
# rules as build_cmd in closed-tabs.sh.
def unbal: ([scan("\\(")] | length) != ([scan("\\)")] | length);
def copilotcut:
  (reduce .[] as $w ({gs: [], stop: false};
    if .stop then .
    elif ($w | startswith("-") | not) then
      (if (.gs | length) > 0 then .gs[-1].vals += [$w] else .gs += [{opt: null, vals: [$w], drop: false}] end)
    elif $w == "--" or ($w | test("^(-i|--interactive)(=|$)")) then .stop = true
    elif ($w | test("^(-r|--resume|--connect|--session-id|-n|--name|-C|--continue|--fleet|--attachment)$|^(--resume|--connect|--session-id|--name|--attachment)=|^-C."))
      then .gs += [{opt: $w, vals: [], drop: true}]
    else .gs += [{opt: $w, vals: [], drop: false}] end) | .gs)
  | map(select((.drop | not) and (any(([.opt // empty] + .vals)[]; unbal) | not)))
  | map([.opt // empty] + .vals) | add // [];
# agy rows keep their leading options (Go flags: one or two dashes, a value
# as the next word or after `=`), minus --conversation and its value, -c/
# --continue and --new-project; -i/--prompt-interactive (a launch prompt),
# `--` and the first bare word (a positional prompt) end it all. Same rules as
# build_cmd in closed-tabs.sh.
def agycut:
  reduce .[] as $w ({out: [], pend: "", stop: false};
    if .stop then .
    elif .pend == "keep" then .out += [$w] | .pend = ""
    elif .pend == "drop" then .pend = ""
    elif $w == "--" or ($w | startswith("-") | not) then .stop = true
    elif ($w | test("^--?(i|prompt-interactive)(=|$)")) then .stop = true
    elif ($w | test("^--?conversation$")) then .pend = "drop"
    elif ($w | test("^--?conversation=|^--?(c|continue|new-project)(=.*)?$")) then .
    elif ($w | test("^--?(add-dir|agent|effort|input-format|json-schema|log-file|mode|model|output-format|print-timeout|project|settings)$")) then .out += [$w] | .pend = "keep"
    else .out += [$w] end)
  | .out;
(.sessions // [])[]
| . as $e
| ((.cli_args // "") | [splits("[ \t\n]+")] | map(select(length > 0))
   | if $e.tool == "codex" then ctxdrop elif $e.tool == "claude" then claudecut
     elif $e.tool == "grok" then grokcut elif $e.tool == "copilot" then copilotcut
     elif $e.tool == "agy" then agycut else . end) as $words
| ($words | map(" " + q) | join("")) as $args
| (if (.model // "") != "" and .tool == "claude"
     and ($words | any(. == "--model" or startswith("--model=")) | not)
   then " --model " + (.model | q) else "" end) as $model
| ([$cap[] as $v | (($e.env // {})[$v] // "") | tostring
    | select(. != "" and . != "null") | $v + "=" + q + " "] | join("")) as $envp
| (if .tool == "claude" then "command claude" + $args + $model + " --resume " + (.session_id | q)
   elif .tool == "opencode" then "command opencode" + $args + " -s " + (.session_id | q)
   elif .tool == "codex" then $codex + $args + " resume " + (.session_id | q)
   elif .tool == "grok" then $grok + $args + " -r " + (.session_id | q)
   elif .tool == "copilot" then "command copilot" + $args + " --resume " + (.session_id | q)
   elif .tool == "agy" then "command agy" + $args + " --conversation " + (.session_id | q)
   else "" end) as $cmd
# The same line for a nu pane (field 6), whole: nu cannot parse the POSIX one
# (`2>/dev/null`, `command`, a `VAR=x` prefix), so a nu shell typed it and the
# agent stayed down. Words are nu raw strings, which take any text but their
# own terminator; a value holding that (or a non-string env value) leaves the
# field empty, and the pane is skipped with a log line.
| (try (
     ($words | map(" " + nq) | join("")) as $nargs
     | (if $model == "" then "" else " --model " + (.model | nq) end) as $nmodel
     | (if .tool == "claude" then "^claude" + $nargs + $nmodel + " --resume " + (.session_id | nq)
        elif .tool == "opencode" then "^opencode" + $nargs + " -s " + (.session_id | nq)
        elif .tool == "codex" then $codexnu + $nargs + " resume " + (.session_id | nq)
        elif .tool == "grok" then $groknu + $nargs + " -r " + (.session_id | nq)
        elif .tool == "copilot" then "^copilot" + $nargs + " --resume " + (.session_id | nq)
        elif .tool == "agy" then "^agy" + $nargs + " --conversation " + (.session_id | nq)
        else "" end) as $ncmd
     | ([$cap[] as $v | (($e.env // {})[$v] // "") | tostring
         | select(. != "" and . != "null") | $v + ": " + nq] | join(", ")) as $nenv
     | (if $nenv == "" then $ncmd else "with-env {" + $nenv + "} { " + $ncmd + " }" end) as $nline
     | if $ncmd == "" then ""
       elif (.cwd // "") == "" then $nline
       else (.cwd | nq) as $d | "if (" + $d + " | path exists) { cd " + $d + " }; " + $nline end
   ) catch "") as $nucmd
| [.pane, .tool, .session_id, (.cwd // ""), (if $cmd == "" then "" else $envp + $cmd end), $nucmd]
| join("\u001f")
JQ
# Codex resumes through the ~/.local/bin wrapper by path: it adds --no-daemon,
# which the tab/notch binding relies on, and a restored pane's PATH can put
# ~/.bun/bin's raw CLI first. codex_nu is the same for a nu pane (^ = external).
codex_cmd="command codex" codex_nu="^codex"
[ -x "$HOME/.local/bin/codex" ] && codex_cmd="$(posix_quote "$HOME/.local/bin/codex")" && codex_nu="^r###'$HOME/.local/bin/codex'###"
# Grok's installer links ~/.grok/bin/grok but puts nothing on PATH, so a grok
# row resumes through that link when it exists.
grok_cmd="command grok" grok_nu="^grok" grok_bin="${GROK_HOME:-$HOME/.grok}/bin/grok"
[ -x "$grok_bin" ] && grok_cmd="$(posix_quote "$grok_bin")" && grok_nu="^r###'$grok_bin'###"
# One line: tests/test_codex_launchers.py lifts this statement (and the
# codex_cmd / grok_cmd lines above) out with grep and runs it.
rows="$(jq -r --argjson cap "$capture_json" --arg codex "$codex_cmd" --arg codexnu "$codex_nu" --arg grok "$grok_cmd" --arg groknu "$grok_nu" "$JQ_PROG" "$INPUT_FILE")" || { log "could not parse $INPUT_FILE"; exit 0; }

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
while IFS="$US" read -r pane tool sid cwd cmd nucmd; do
	[ -n "$pane" ] || continue
	sess="${pane%%:*}"
	if is_excluded "$sess"; then log "session '$sess' is excluded, skipping $pane"; continue; fi
	if [ -z "$cmd" ]; then
		log "unknown tool '$tool' for pane $pane, skipping"
		continue
	fi
	line="${pane}${US}${tool}${US}${sid}${US}${cwd}${US}${cmd}${US}${nucmd}"$'\n'
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
log "panes ready after $(elapsed)s"

# --- wait (once, bounded) for the local MCP servers -----------------------------
# Claude connects to its HTTP MCP servers once, at startup; a refused connection
# marks the server failed for the life of the process (no retry, only /mcp by
# hand). At login launchd starts the LaunchAgents behind them (tmux-pty-mcp on
# 7997, tmux-mcp on 7998) AFTER WezTerm has brought tmux up: on 2026-10-19 the
# resumes went out at 12:32:53 and the servers listened at 12:32:59, so all 14
# restored agents came up without pty or tmux tools. So the resumes wait here
# until every loopback http server in ~/.claude.json answers (any HTTP status,
# 401 included, means it is listening). Remote ones are not ours to wait on.
# @assistant-resurrect-mcp-urls overrides the list; @assistant-resurrect-mcp-wait
# (seconds) bounds the wait, after which the agents are resumed anyway.
MCP_WAIT="$(tmux show-option -gqv @assistant-resurrect-mcp-wait 2>/dev/null)"
MCP_WAIT="${MCP_WAIT:-90}"
mcp_urls="$(tmux show-option -gqv @assistant-resurrect-mcp-urls 2>/dev/null)"
if [ -z "$mcp_urls" ] && [ -f "$HOME/.claude.json" ]; then
	mcp_urls="$(jq -r '(.mcpServers // {})[] | select(.type == "http" or .type == "sse") | .url // empty
		| select(test("^https?://(127\\.0\\.0\\.1|localhost|\\[::1\\])[:/]"))' "$HOME/.claude.json" 2>/dev/null | tr '\n' ' ')"
fi
mcp_down() {
	local u down=""
	for u in $mcp_urls; do
		[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 1 "$u" 2>/dev/null)" = 000 ] && down+=" $u"
	done
	printf '%s' "${down# }"
}
if [ -n "$mcp_urls" ]; then
	mcp_start=$EPOCHREALTIME
	down="$(mcp_down)"
	[ -n "$down" ] && log "waiting up to ${MCP_WAIT}s for MCP server(s): $down"
	while [ -n "$down" ]; do
		if awk -v s="$mcp_start" -v e="$EPOCHREALTIME" -v w="$MCP_WAIT" 'BEGIN { exit !(e - s >= w) }'; then
			log "MCP server(s) still down after ${MCP_WAIT}s, resuming anyway (those agents will need /mcp → Reconnect): $down"
			"$HOME/.config/tmux/scripts/notify.sh" -k warn -- "Agents resumed without MCP: $down down. /mcp → Reconnect in each, or re-resume them." 2>/dev/null
			break
		fi
		sleep 0.5
		down="$(mcp_down)"
		[ -z "$down" ] && log "MCP servers up after $(elapsed)s"
	done
	# Guard 1 below reads $live: refresh it if the wait gave panes time to change.
	live="$(tmux list-panes -a -F "#{session_name}:#{window_index}.#{pane_index}${US}#{pane_current_command}${US}#{pane_pid}${US}#{cursor_x}${US}#{cursor_y}" 2>/dev/null)"
fi
snapshot="$(ps -eo pid=,ppid=,args= 2>/dev/null)"

# --- resume ------------------------------------------------------------------------
restored=0
while IFS="$US" read -r pane tool sid cwd cmd nucmd; do
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

	if [ "${pane_cmd#-}" = nu ]; then
		# nu needs its own line (JQ_PROG's field 6, cd included).
		if [ -z "$nucmd" ]; then
			log "pane $pane is a nu shell and its resume command cannot be quoted for nu, skipping"
			continue
		fi
		line="$nucmd" cmd="$nucmd"
	else
		line="$cmd"
		[ -n "$cwd" ] && [ "$cwd" != "null" ] && line="cd $(posix_quote "$cwd") 2>/dev/null; $cmd"
	fi
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
