#!/usr/bin/env bash
# GitHub Copilot CLI / Antigravity CLI (agy) session hook: their twin of
# grok-session-track.sh (and of tmux-assistant-resurrect's claude-session-
# track.sh). Keeps $STATE_DIR/<kind>-<pid>.json, the shape every reader of
# those records knows: {tool, session_id, cwd, ppid, timestamp, env:
# {tmux_pane, shell, ...@assistant-resurrect-capture-env}}. Read by
# assistant-save.sh's copilot/agy rows, closed-tabs.sh, agent-pause.py and
# tmux-mcp (message_agent).
#
#   copilot-agy-session-track.sh <copilot|agy> <start|end>    hook JSON on stdin
#
# copilot: sessionStart -> start (source startup, resume, or new = /new and
#   /clear, which follow a sessionEnd), sessionEnd -> end. Payload (native
#   camelCase): {source, sessionId, timestamp, cwd}.
# agy: SessionStart and Stop -> start. agy has no session-end event, and
#   SessionStart is documented only for a NEW conversation, so the end of every
#   turn (Stop) re-files the conversation it ran (a `--conversation <id>` resume
#   or a switch inside the TUI is then known after its first turn). Payload:
#   {conversationId, cwd, workspacePaths, parentConversationId, ...}; a
#   subagent's (parentConversationId set) is ignored. A record whose pid is
#   gone is deleted by the next save.
#
# start (re)writes the record (only when the session or pane changed, so agy's
# per-turn Stop costs one read); end removes it, only when it names the session
# that ended, renamed aside first so a racing start is never deleted.
#
# pid = the CLI's own process, the OUTERMOST of its comm above the hook: the
# Homebrew copilot is a restart supervisor with the CLI as its child, both
# comm `copilot`, and the supervisor is what the pane's shell started (the
# save, closed-tabs and the pause key find that one). Hooks run under
# `bash -c` (copilot) or `sh -c` (agy), so the walk goes up at most six
# parents. Nothing is written for a CLI outside tmux (no TMUX_PANE), a
# headless one (no tty, or -p/--prompt/--print in its argv: the same TUI rule
# as the watcher's copilot_tui_args / agy_tui_args), CUA_NOTCH_IGNORE=1 (every
# ai-exec leg), or a hook with no such process above it.

set -uo pipefail

KIND="${1:-}"
EVENT="${2:-start}"
case "$KIND" in copilot | agy) ;; *) exit 0 ;; esac
case "$EVENT" in start | end) ;; *) exit 0 ;; esac
[ "${CUA_NOTCH_IGNORE:-}" = 1 ] && exit 0
[ -n "${TMUX_PANE:-}" ] || exit 0

STATE_DIR="${TMUX_ASSISTANT_RESURRECT_DIR:-${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/tmux-assistant-resurrect}"
INPUT=$(cat)

# The watcher's copilot_tui_args / agy_tui_args, verbatim: 0 = the TUI.
copilot_tui_args() {
	local -a a
	local i=1 w
	read -r -a a <<<"$1"
	while [ "$i" -lt "${#a[@]}" ]; do
		w="${a[i]}"
		case "$w" in
		--) return 0 ;;
		-i | -i=* | --interactive | --interactive=*) return 0 ;;
		-p* | --prompt | --prompt=* | -h | --help | -v | --version) return 1 ;;
		--model | --reasoning-effort | --context | --auto-tier | --agent | -n | --name | --session-id | -C | --log-dir | --extension-sdk-path | --log-level | --stream | --output-format | --add-dir | --allow-tool | --deny-tool | --available-tools | --excluded-tools | --additional-mcp-config | --config-dir | --mode)
			i=$((i + 2))
			continue
			;;
		-*)
			i=$((i + 1))
			continue
			;;
		app | login | help | init | config | update | version | workflow | sessions | memories | plugin | mcp | skill | instruction | lsp | sandbox | completion)
			return 1
			;;
		*) return 0 ;;
		esac
	done
	return 0
}
agy_tui_args() {
	local -a a
	local i=1 w
	read -r -a a <<<"$1"
	while [ "$i" -lt "${#a[@]}" ]; do
		w="${a[i]}"
		case "$w" in
		--) return 0 ;;
		-i | -i=* | -prompt-interactive | -prompt-interactive=* | --prompt-interactive | --prompt-interactive=*)
			return 0
			;;
		-p | -p=* | -print | -print=* | --print | --print=* | -prompt | -prompt=* | --prompt | --prompt=* | -h | -help | --help | -version | --version)
			return 1
			;;
		-add-dir | --add-dir | -agent | --agent | -conversation | --conversation | -effort | --effort | -input-format | --input-format | -json-schema | --json-schema | -log-file | --log-file | -mode | --mode | -model | --model | -output-format | --output-format | -print-timeout | --print-timeout | -project | --project | -settings | --settings)
			i=$((i + 2))
			continue
			;;
		-*)
			i=$((i + 1))
			continue
			;;
		acp | agent | agents | changelog | help | install | mcp | mic-serve | models | plugin | plugins | remote-control | update)
			return 1
			;;
		*) return 0 ;;
		esac
	done
	return 0
}

# The outermost $KIND process above this hook, and only a TUI one with a tty.
find_pid() {
	local pid="$PPID" n=6 line ppid tty comm found="" found_tty=""
	while [ "$n" -gt 0 ] && [ "${pid:-0}" -gt 1 ]; do
		line="$(ps -o ppid=,tty=,comm= -p "$pid" 2>/dev/null)" || break
		read -r ppid tty comm <<<"$line"
		if [ "${comm##*/}" = "$KIND" ]; then
			found="$pid" found_tty="$tty"
		elif [ -n "$found" ]; then
			break
		fi
		pid="$ppid"
		n=$((n - 1))
	done
	[ -n "$found" ] || return 1
	case "$found_tty" in '' | '??' | '?') return 1 ;; esac
	local args
	args="$(ps -ww -o args= -p "$found" 2>/dev/null)" || return 1
	if [ "$KIND" = copilot ]; then copilot_tui_args "$args" || return 1; else agy_tui_args "$args" || return 1; fi
	printf '%s' "$found"
}

# Session id, cwd and "sub" (a subagent's event) out of the payload, one jq.
# copilot: sessionId (session_id too); agentId marks a subagent. agy:
# conversationId; parentConversationId marks a subagent; cwd, else its first
# workspace path.
parsed="$(printf '%s' "$INPUT" | jq -r --arg kind "$KIND" '
	if type != "object" then empty
	elif $kind == "copilot" then
	  [(.sessionId // .session_id // ""), (.cwd // ""), (if (.agentId // .agent_id // "") != "" then "sub" else "" end)]
	else
	  [(.conversationId // .conversation_id // ""), (.cwd // (.workspacePaths // [])[0] // ""),
	   (if (.parentConversationId // .parent_conversation_id // "") != "" then "sub" else "" end)]
	end
	| map(tostring | gsub("[\u0000-\u001f]"; "")) | join("\u001f")' 2>/dev/null)" || exit 0
IFS=$'\x1f' read -r SID SCWD SUB <<<"$parsed"
[ -n "$SID" ] && [ -z "$SUB" ] || exit 0

PID="$(find_pid)" || exit 0
STATE_FILE="$STATE_DIR/$KIND-$PID.json"

if [ "$EVENT" = end ]; then
	[ -f "$STATE_FILE" ] || exit 0
	aside="$STATE_FILE.end.$$"
	mv -f "$STATE_FILE" "$aside" 2>/dev/null || exit 0
	verdict="$(jq -r --arg s "$SID" 'if (.session_id // "") == $s then "rm" else "keep" end' "$aside" 2>/dev/null)"
	[ "$verdict" = keep ] && mv -n "$aside" "$STATE_FILE" 2>/dev/null
	rm -f "$aside"
	exit 0
fi

# Unchanged (agy's Stop on every turn): nothing to write. A plain read, no jq.
if [ -f "$STATE_FILE" ]; then
	rec=""
	read -r -d '' rec <"$STATE_FILE" 2>/dev/null
	case "$rec" in *"\"session_id\":\"$SID\""*"\"tmux_pane\":\"$TMUX_PANE\""*) exit 0 ;; esac
fi

mkdir -p -m 0700 "$STATE_DIR" 2>/dev/null
envargs=()
for var in $(tmux show-option -gqv @assistant-resurrect-capture-env 2>/dev/null); do
	[[ "$var" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && envargs+=(--arg "env_$var" "${!var:-}")
done
tmp="$STATE_FILE.$$"
jq -cn \
	--arg tool "$KIND" --arg sid "$SID" --arg cwd "${SCWD:-$PWD}" \
	--argjson ppid "$PID" \
	--arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
	--arg tmux_pane "$TMUX_PANE" --arg shell "${SHELL:-}" \
	"${envargs[@]+"${envargs[@]}"}" '
	{tool: $tool, session_id: $sid, cwd: $cwd, ppid: $ppid, timestamp: $timestamp,
	 env: ({tmux_pane: $tmux_pane, shell: $shell}
	       + ($ARGS.named | with_entries(select(.key | startswith("env_")) | .key |= .[4:])))}' >"$tmp" 2>/dev/null
if [ -s "$tmp" ]; then
	mv -f "$tmp" "$STATE_FILE"
else
	rm -f "$tmp"
fi
exit 0
