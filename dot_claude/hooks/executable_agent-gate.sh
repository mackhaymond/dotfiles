#!/bin/sh
# Claude Code gate against unattended usage burn. Usage: agent-gate.sh pre-tool|prompt
#
# 1. DarkWake hold (pre-tool only). With the lid closed macOS DarkWakes every
#    few minutes for ~9 s; agents mid-task re-sent their whole ~450k-token
#    context on each one (2026-10-11: 29% of a week in 15 awake minutes).
#    While the system lacks the Graphics capability (DarkWake), wait here
#    instead: the agent sleeps with the Mac and resumes on a real wake.
#    Awake with the display off (lid closed under Awake) keeps Graphics.
# 2. Weekly reserve. Stop work on the main account at RESERVE_PCT weekly so the
#    rest stays usable from the phone. `touch ~/.claude/reserve-override`
#    bypasses it until the next weekly reset.
#
# Fails OPEN: any unreadable input lets Claude run. Never SIGSTOPs anything.
cat >/dev/null 2>&1

MAIN=mackhaymond@gmail.com
RESERVE_PCT=90
LIVE=$HOME/.cache/codexbar-tmux/claude-live.json
OVERRIDE=$HOME/.claude/reserve-override

dark_wake() {
	caps=$(pmset -g systemstate 2>/dev/null) || return 1
	case $caps in *"System Capabilities"*) ;; *) return 1 ;; esac
	case $caps in *Graphics*) return 1 ;; esac
	return 0
}

if [ "${1:-}" = pre-tool ]; then
	while dark_wake; do sleep 5; done
fi

acct=$(plutil -extract oauthAccount.emailAddress raw -o - "${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json" 2>/dev/null) || exit 0
[ "$acct" = "$MAIN" ] || exit 0
live_acct=$(plutil -extract account raw -o - "$LIVE" 2>/dev/null) || exit 0
[ "${live_acct%%/*}" = "$MAIN" ] || exit 0
used=$(plutil -extract seven_day.used raw -o - "$LIVE" 2>/dev/null) || exit 0
resets=$(plutil -extract seven_day.resets_at raw -o - "$LIVE" 2>/dev/null) || exit 0
used=${used%%.*}
case $used$resets in '' | *[!0-9]*) exit 0 ;; esac
[ "$(date +%s)" -lt "$resets" ] || exit 0
[ "$used" -ge "$RESERVE_PCT" ] || exit 0
if [ -f "$OVERRIDE" ] && [ "$(stat -f %m "$OVERRIDE")" -ge $((resets - 604800)) ]; then
	exit 0
fi

# 3. An agent-spawn CHILD stopped by the reserve (2026-11-25): nobody is
#    watching its tab, so say why on the tab itself - `failed` with the
#    reason, which agent-spawn wait reports and the parent's ask surfaces
#    (docs/agent-tab-indicator.md §8). Only a child: its window has the raw
#    link @agent_parent, or its @agent_spawn_id is this env's AGENT_SPAWN_ID
#    (an orphaned child keeps its spawn id). Fail-open: no tmux, no pane, any
#    error - the block below still stands.
#    The indicator's own hook for this same UserPromptSubmit / PreToolUse
#    runs in PARALLEL and writes `running` (and the blocked prompt's
#    @agent_turn_start), which can land after the mark. So the mark is
#    re-asserted once, 2 s later, by a detached tmux job (it outlives this
#    hook): only if the tab still reads `running` and its @agent_turn_start
#    is no later than this block (+1 s), i.e. no real turn has started since.
mark_child_failed() {
	[ -n "${TMUX_PANE:-}" ] || return 0
	command -v tmux >/dev/null 2>&1 || return 0
	row=$(tmux display-message -p -t "$TMUX_PANE" '#{window_id}	#{@agent_parent}	#{@agent_spawn_id}	#{socket_path}' 2>/dev/null) || return 0
	win=${row%%	*}
	rest=${row#*	}
	parent=${rest%%	*}
	rest=${rest#*	}
	spawn=${rest%%	*}
	sock=${rest#*	}
	case $win in @[0-9]*) ;; *) return 0 ;; esac
	if [ -z "$parent" ]; then
		[ -n "${AGENT_SPAWN_ID:-}" ] && [ "$spawn" = "$AGENT_SPAWN_ID" ] || return 0
	fi
	now=$(date +%s)
	tmux set-option -w -t "$win" @agent_state failed \; \
		set-option -w -t "$win" @agent_detail 'weekly reserve' \; \
		set-option -w -t "$win" @agent_since "$now failed" 2>/dev/null || true
	# The re-assert. No '#' in it (run-shell expands formats) and an explicit
	# socket (a job may not inherit $TMUX); a quote in the path: skip it.
	case $sock in /*) ;; *) return 0 ;; esac
	case $sock in *"'"*) return 0 ;; esac
	tm="tmux -S '$sock'"
	tmux run-shell -b -t "$TMUX_PANE" "sleep 2; s=\$($tm show-options -wqv -t $win @agent_state); t=\$($tm show-options -wqv -t $win @agent_turn_start); t=\${t%%[!0-9]*}; [ \"\$s\" = running ] && [ \"\${t:-0}\" -le $((now + 1)) ] && $tm set-option -w -t $win @agent_state failed \\; set-option -w -t $win @agent_detail 'weekly reserve' \\; set-option -w -t $win @agent_since '$now failed'; true" 2>/dev/null || true
}
mark_child_failed

printf '{"continue":false,"stopReason":"Weekly reserve: %s is at %s%% (reserve starts at %s%%). touch ~/.claude/reserve-override to keep going until the reset."}\n' \
	"$MAIN" "$used" "$RESERVE_PCT"
