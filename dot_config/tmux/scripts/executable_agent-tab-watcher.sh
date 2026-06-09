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
#   agent present + no state           → idle    (seed presence)
#   no agent     + any state OR summary → unset @agent_state/@agent_summary (GC)
# Hook-set states (running/needs-input/done) are never overridden while the
# agent lives.
#
# It also drives the running-state animation: while ANY window is in state
# running, the global @agent_blink option toggles each tick and the window
# formats alternate the agent glyph between two colors off it. No running
# windows → no toggling, no redraws.
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

# 1s: doubles as the blink interval for the running-glyph animation.
POLL_SECONDS=1

command -v tmux >/dev/null 2>&1 || exit 0

# Singleton: every tmux.conf reload (`prefix r`) re-runs the spawn line, so a
# plain check-then-write leaks daemons (a transiently-exiting watcher with an
# unconditional trap can delete a live sibling's pidfile, then the next reload
# finds none and starts another). Reap any prior instance, then claim the
# pidfile, and only clean it up on exit if it's still ours.
PIDFILE="${TMPDIR:-/tmp}/agent-tab-watcher.$(id -u).pid"
prev=$(cat "$PIDFILE" 2>/dev/null || true)
if [ -n "$prev" ] && [ "$prev" != "$$" ] && kill -0 "$prev" 2>/dev/null; then
    kill "$prev" 2>/dev/null || true
fi
echo $$ > "$PIDFILE"
# Remove the pidfile on exit only if it's still ours. The signal traps must
# EXIT (a bare cleanup trap on TERM/INT/HUP would run the handler and then
# RESUME the loop — the daemon would survive `kill`, which is exactly how the
# old version leaked); routing signals through `exit 0` fires the EXIT trap.
cleanup() { [ "$(cat "$PIDFILE" 2>/dev/null)" = "$$" ] && rm -f "$PIDFILE"; }
trap cleanup EXIT
trap 'exit 0' INT TERM HUP

is_agent_comm() {
    local base
    base="$(basename "$1")"
    case "$base" in
        claude|codex) return 0 ;;
    esac
    # Claude's binary is version-named (e.g. "2.1.170") in case its
    # process-title rename ever stops applying. Anchored regex, digits-only
    # segments — a case glob like [0-9]*.[0-9]*.[0-9]* would also match
    # IP-like or suffixed names ("10.0.0.1", "1.2.3-beta").
    [[ "$base" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
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

    # Windows with a non-empty @agent_summary (read separately — a summary can
    # contain spaces, and it can outlive @agent_state: a detached condenser may
    # write a summary after the agent died and the watcher GC'd its state).
    with_summary=" "
    while IFS=' ' read -r win rest; do
        [ -n "$win" ] && [ -n "$rest" ] && with_summary="${with_summary}${win} "
    done <<EOF
$(tmux list-windows -a -F '#{window_id} #{@agent_summary}' 2>/dev/null)
EOF

    changed=0
    while IFS=' ' read -r win state; do
        [ -n "$win" ] || continue
        case "$present" in
            *" ${win} "*) has_agent=1 ;;
            *) has_agent=0 ;;
        esac
        case "$with_summary" in
            *" ${win} "*) has_summary=1 ;;
            *) has_summary=0 ;;
        esac
        if [ "$has_agent" = 1 ] && [ -z "$state" ]; then
            tmux set-option -w -t "$win" @agent_state idle 2>/dev/null && changed=1
        elif [ "$has_agent" = 0 ] && { [ -n "$state" ] || [ "$has_summary" = 1 ]; }; then
            tmux set-option -uw -t "$win" @agent_state 2>/dev/null
            tmux set-option -uw -t "$win" @agent_summary 2>/dev/null
            changed=1
        fi
    done <<EOF
$states
EOF

    # Blink driver: toggle while anything is running; redraw covers both
    # the toggle and any reconcile changes above.
    case "$states" in
        *" running"*)
            if [ "$(tmux show-options -gqv @agent_blink 2>/dev/null)" = "1" ]; then
                tmux set-option -g @agent_blink 0 2>/dev/null
            else
                tmux set-option -g @agent_blink 1 2>/dev/null
            fi
            changed=1
            ;;
    esac

    if [ "$changed" = 1 ]; then
        tmux refresh-client -S 2>/dev/null
    fi

    sleep "$POLL_SECONDS"
done
