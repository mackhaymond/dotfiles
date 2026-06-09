#!/usr/bin/env bash
# Agent tab watcher — presence daemon backing agent-tab-indicator.sh.
#
# Hooks give instant state transitions but can't cover two cases:
#   1. presence with no events yet (agent just launched, or hooks untrusted —
#      codex requires interactive trust approval for new hook entries), and
#   2. cleanup when the agent dies without firing SessionEnd (SIGKILL,
#      kill-pane SIGHUP, crash) — Claude's SessionEnd is best-effort and
#      codex has no SessionEnd event at all.
#
# Every POLL_SECONDS this daemon matches agent processes to tmux windows by
# TTY and reconciles the per-window @agent_state option:
#   agent present + no state  → idle    (seed presence)
#   no agent     + any state  → unset @agent_state/@agent_summary (GC)
# Hook-set states (running/needs-input/done) are never overridden while the
# agent lives.
#
# Process matching is by `ps -o comm` basename — NOT #{pane_current_command}:
# tmux reads the kernel p_comm, which for Claude Code is the version-named
# binary ("2.1.170"), while ps comm reflects argv[0] ("claude"). The bare
# version-string pattern is kept as a fallback in case a Claude build stops
# setting its process title. Codex's npm wrapper spawns the native binary
# vendor/<triple>/bin/codex → comm basename "codex" (plus a "node" parent we
# don't match).
#
# Singleton + lifecycle follow coffee-watcher.sh: PID-file guard, exits when
# the tmux server goes away, writes only on change then refresh-client -S.
# Spawned from tmux.conf via `run-shell -b`. set -u/-e relaxed: a daemon
# must survive transient tmux command failures mid-loop.

POLL_SECONDS=2

command -v tmux >/dev/null 2>&1 || exit 0

PIDFILE="${TMPDIR:-/tmp}/agent-tab-watcher.$(id -u).pid"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
    exit 0
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT INT TERM HUP

is_agent_comm() {
    case "$(basename "$1")" in
        claude|codex) return 0 ;;
        [0-9]*.[0-9]*.[0-9]*) return 0 ;;   # claude's version-named binary
        *) return 1 ;;
    esac
}

while :; do
    # window_id<space>pane_tty for every pane; failure = server gone.
    panes=$(tmux list-panes -a -F '#{window_id} #{pane_tty}' 2>/dev/null) || exit 0

    # One ps for all TTYs: build "ttys001 ttys004 ..." list of agent TTYs.
    agent_ttys=" "
    while IFS=' ' read -r tty comm; do
        [ -n "$tty" ] && [ "$tty" != "??" ] || continue
        if is_agent_comm "$comm"; then
            agent_ttys="${agent_ttys}${tty} "
        fi
    done <<EOF
$(ps -ax -o tty=,comm= 2>/dev/null)
EOF

    # Current per-window state in one call (formats resolve window options).
    states=$(tmux list-windows -a -F '#{window_id} #{@agent_state}' 2>/dev/null) || exit 0

    # Windows containing at least one agent pane.
    present=" "
    while IFS=' ' read -r win tty; do
        [ -n "$win" ] || continue
        short_tty="${tty#/dev/}"
        case "$agent_ttys" in
            *" ${short_tty} "*) present="${present}${win} " ;;
        esac
    done <<EOF
$panes
EOF

    changed=0
    while IFS=' ' read -r win state; do
        [ -n "$win" ] || continue
        case "$present" in
            *" ${win} "*) has_agent=1 ;;
            *) has_agent=0 ;;
        esac
        if [ "$has_agent" = 1 ] && [ -z "$state" ]; then
            tmux set-option -w -t "$win" @agent_state idle 2>/dev/null && changed=1
        elif [ "$has_agent" = 0 ] && [ -n "$state" ]; then
            tmux set-option -uw -t "$win" @agent_state 2>/dev/null
            tmux set-option -uw -t "$win" @agent_summary 2>/dev/null
            changed=1
        fi
    done <<EOF
$states
EOF

    if [ "$changed" = 1 ]; then
        tmux refresh-client -S 2>/dev/null
    fi

    sleep "$POLL_SECONDS"
done
