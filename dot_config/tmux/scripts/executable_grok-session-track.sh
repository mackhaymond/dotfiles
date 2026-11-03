#!/usr/bin/env bash
# Grok SessionStart / SessionEnd hook: the grok twin of tmux-assistant-resurrect's
# claude-session-track.sh / claude-session-cleanup.sh. Keeps
# $STATE_DIR/grok-<pid>.json, same shape as the plugin's claude-<pid>.json
# (the hook payload merged with tool, ppid, timestamp and env.tmux_pane), for
# everything that reads those records: assistant-save.sh's grok rows,
# closed-tabs.sh, tmux-mcp and agent-pause.
#
#   grok-session-track.sh [start|end]    hook JSON on stdin
#
# Without an argument the event comes from GROK_HOOK_EVENT (set for every Grok
# hook), then the payload's hook_event_name / hookEventName.
#
# SessionStart (re)writes the record: /new and /resume start a new session id in
# the same process, and the record follows it. SessionEnd removes it, but only
# when the session it ends is the one recorded, so a late end of the old
# session cannot delete the new one's record. Child (subagent) sessions carry
# subagentType and are ignored.
#
# pid = the grok TUI: hooks are its direct children (started directly, or
# through `sh -c` for shell text), so this walks up at most three parents to a
# process whose comm is grok / grok-<os>-<arch>. Anything else (a hook run by
# hand, Claude's compat runner) writes nothing.
#
# Runs on every session start, so it stays small: one jq, one ps per level,
# one tmux call only when @assistant-resurrect-capture-env can be read.

set -uo pipefail

STATE_DIR="${TMUX_ASSISTANT_RESURRECT_DIR:-${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/tmux-assistant-resurrect}"
GROK_RE='^grok(-(macos|linux|windows)-[a-z0-9_]+)?$'

INPUT=$(cat)

event="${1:-${GROK_HOOK_EVENT:-}}"
if [ -z "$event" ]; then
	# Rare (no argument and no runner variable): pull the name out of the
	# payload without another jq.
	[[ "$INPUT" =~ \"(hook_event_name|hookEventName)\"[[:space:]]*:[[:space:]]*\"([A-Za-z_]+)\" ]] && event="${BASH_REMATCH[2]}"
fi
# PascalCase (Claude's spelling), camelCase or snake_case.
case "${event//_/}" in
start | [Ss]ession[Ss]tart) event=start ;;
end | [Ss]ession[Ee]nd) event=end ;;
*) exit 0 ;;
esac

find_grok_pid() {
	local pid="$PPID" n=3 line ppid comm
	while [ "$n" -gt 0 ] && [ "${pid:-0}" -gt 1 ]; do
		line="$(ps -o ppid=,comm= -p "$pid" 2>/dev/null)" || return 1
		read -r ppid comm <<<"$line"
		if [[ "${comm##*/}" =~ $GROK_RE ]]; then
			printf '%s' "$pid"
			return 0
		fi
		pid="$ppid"
		n=$((n - 1))
	done
	return 1
}
GROK_PID="$(find_grok_pid)" || exit 0
STATE_FILE="$STATE_DIR/grok-$GROK_PID.json"

if [ "$event" = end ]; then
	[ -f "$STATE_FILE" ] || exit 0
	# A child session's end never touches the record.
	[[ "$INPUT" =~ \"(subagentType|subagent_type)\"[[:space:]]*:[[:space:]]*\"[^\"] ]] && exit 0
	# Atomic against a SessionStart racing this end (/new): the record is
	# renamed aside first and judged there. It is only ever put back with
	# mv -n, so a record the new session wrote meanwhile is neither deleted
	# nor overwritten.
	aside="$STATE_FILE.end.$$"
	mv -f "$STATE_FILE" "$aside" 2>/dev/null || exit 0
	verdict="$(printf '%s' "$INPUT" | jq -r --slurpfile rec "$aside" '
		(.session_id // .sessionId // "") as $s
		| if (.subagentType // .subagent_type // "") != "" then "keep"
		  elif $s == "" or (($rec[0] // {}).session_id // "") == $s then "rm"
		  else "keep" end' 2>/dev/null)"
	# A record jq cannot read is garbage either way.
	[ "$verdict" = keep ] && mv -n "$aside" "$STATE_FILE" 2>/dev/null
	rm -f "$aside"
	exit 0
fi

mkdir -p -m 0700 "$STATE_DIR" 2>/dev/null
# Same env object as the claude records: TMUX_PANE and SHELL always, plus the
# variables @assistant-resurrect-capture-env names (assistant-restore.sh
# replays those).
envargs=()
if [ -n "${TMUX:-}" ]; then
	for var in $(tmux show-option -gqv @assistant-resurrect-capture-env 2>/dev/null); do
		[[ "$var" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && envargs+=(--arg "env_$var" "${!var:-}")
	done
fi
tmp="$STATE_FILE.$$"
printf '%s' "$INPUT" | jq -c \
	--argjson ppid "$GROK_PID" \
	--arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
	--arg tmux_pane "${TMUX_PANE:-}" --arg shell "${SHELL:-}" --arg pwd "$PWD" \
	"${envargs[@]+"${envargs[@]}"}" '
	(.session_id // .sessionId // "") as $sid
	| if $sid == "" or (.subagentType // .subagent_type // "") != "" then empty
	  else . + {session_id: $sid, cwd: (.cwd // $pwd), tool: "grok", ppid: $ppid, timestamp: $timestamp,
	            env: ({tmux_pane: $tmux_pane, shell: $shell}
	                  + ($ARGS.named | with_entries(select(.key | startswith("env_")) | .key |= .[4:])))}
	  end' >"$tmp" 2>/dev/null
if [ -s "$tmp" ]; then
	mv -f "$tmp" "$STATE_FILE"
else
	rm -f "$tmp"
fi
exit 0
