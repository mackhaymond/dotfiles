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
# It also makes tabs aware of background Claude WORKFLOWS. A backgrounded
# Workflow keeps running after the main turn's Stop fires (so the tab would
# otherwise read done/idle). There's no hook for it, but the workflow runtime
# writes a live dir subagents/workflows/wf_<id>/ and only writes the terminal
# state file workflows/wf_<id>.json at completion — so a workflow is in-flight
# iff its runtime dir exists without that completion file. Per claude window we
# map pane→pid→session (~/.claude/sessions/<pid>.json) and set a per-window
# @agent_workflow flag the formats render as a distinct blinking gear.
# (Workflows are a Claude feature; codex windows are never checked.)
#
# It also drives the running-state animation: while ANY window is in state
# running OR has @agent_workflow set, the global @agent_blink option toggles
# each tick and the window formats alternate the glyph between two colors off
# it. Nothing running → no toggling, no redraws.
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

# True if the claude session owning PID has a background Workflow in flight.
# Runtime dir without its completion file = running (see header). Scoped to
# the session's own project/<sid> dir so other panes' workflows don't leak in.
session_has_running_workflow() {
    local pid="$1" sf sid cwd proj base d wfid mt now
    [ -n "$pid" ] || return 1
    sf="$HOME/.claude/sessions/$pid.json"
    [ -f "$sf" ] || return 1
    sid=$(grep -o '"sessionId":"[^"]*"' "$sf" 2>/dev/null | head -1 | cut -d'"' -f4)
    cwd=$(grep -o '"cwd":"[^"]*"' "$sf" 2>/dev/null | head -1 | cut -d'"' -f4)
    { [ -n "$sid" ] && [ -n "$cwd" ]; } || return 1
    # Claude munges the project dir name from cwd: '/' and '.' both become '-'.
    proj="${cwd//\//-}"; proj="${proj//./-}"
    base="$HOME/.claude/projects/$proj/$sid"
    [ -d "$base/subagents/workflows" ] || return 1
    now=$(date +%s 2>/dev/null || echo 0)
    for d in "$base"/subagents/workflows/wf_*/; do
        [ -d "$d" ] || continue
        wfid=$(basename "$d")
        [ -f "$base/workflows/$wfid.json" ] && continue   # completion file → done
        # Backstop against a crashed/stale runtime dir: the agent transcripts
        # stream continuously while running, so a recent mtime means live.
        mt=$(stat -f %m "$d"/agent-*.jsonl "$d/journal.jsonl" 2>/dev/null | sort -rn | head -1)
        [ -n "$mt" ] && [ $((now - mt)) -lt 600 ] && return 0
    done
    return 1
}

while :; do
    # window_id<space>pane_tty for every pane; failure = server gone.
    panes=$(tmux list-panes -a -F '#{window_id} #{pane_tty}' 2>/dev/null) || exit 0

    # One ps for all TTYs: agent TTYs, plus tty=pid for claude panes (workflow
    # lookup needs the pid; codex panes are skipped — no workflows there).
    agent_ttys=" "
    tty_pid=" "
    while IFS=' ' read -r tty pid comm; do
        [ -n "$tty" ] && [ "$tty" != "??" ] || continue
        if is_agent_comm "$comm"; then
            agent_ttys="${agent_ttys}${tty} "
            case "$(basename "$comm")" in
                claude|[0-9]*) tty_pid="${tty_pid}${tty}=${pid} " ;;
            esac
        fi
    done <<EOF
$(ps -ax -o tty=,pid=,comm= 2>/dev/null)
EOF

    # Current per-window state in one call (formats resolve window options).
    states=$(tmux list-windows -a -F '#{window_id} #{@agent_state}' 2>/dev/null) || exit 0

    # Windows containing at least one agent pane, and the claude pid per window
    # (first agent pane wins) for workflow lookup.
    present=" "
    win_pid=" "
    while IFS=' ' read -r win tty; do
        [ -n "$win" ] || continue
        short_tty="${tty#/dev/}"
        case "$agent_ttys" in
            *" ${short_tty} "*)
                present="${present}${win} "
                case "$win_pid" in
                    *" ${win}="*) : ;;   # already mapped
                    *)
                        for kv in $tty_pid; do
                            case "$kv" in "${short_tty}="*) win_pid="${win_pid}${win}=${kv#*=} "; break ;; esac
                        done
                        ;;
                esac
                ;;
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

    # Windows that currently carry @agent_workflow (to reconcile against).
    wf_now=" "
    while IFS=' ' read -r win rest; do
        [ -n "$win" ] && [ -n "$rest" ] && wf_now="${wf_now}${win} "
    done <<EOF
$(tmux list-windows -a -F '#{window_id} #{@agent_workflow}' 2>/dev/null)
EOF

    changed=0
    any_workflow=0
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
        case "$wf_now" in
            *" ${win} "*) had_wf=1 ;;
            *) had_wf=0 ;;
        esac

        # Background-workflow detection (claude only; needs a live agent).
        wf=0
        if [ "$has_agent" = 1 ]; then
            pid=""
            for kv in $win_pid; do
                case "$kv" in "${win}="*) pid="${kv#*=}"; break ;; esac
            done
            if [ -n "$pid" ] && session_has_running_workflow "$pid"; then
                wf=1; any_workflow=1
            fi
        fi
        if [ "$wf" = 1 ] && [ "$had_wf" = 0 ]; then
            tmux set-option -w -t "$win" @agent_workflow 1 2>/dev/null && changed=1
        elif [ "$wf" = 0 ] && [ "$had_wf" = 1 ]; then
            tmux set-option -uw -t "$win" @agent_workflow 2>/dev/null && changed=1
        fi

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

    # Blink driver: toggle while anything is running or has a workflow in
    # flight; redraw covers both the toggle and any reconcile changes above.
    blink_active=0
    case "$states" in *" running"*) blink_active=1 ;; esac
    [ "$any_workflow" = 1 ] && blink_active=1
    if [ "$blink_active" = 1 ]; then
        if [ "$(tmux show-options -gqv @agent_blink 2>/dev/null)" = "1" ]; then
            tmux set-option -g @agent_blink 0 2>/dev/null
        else
            tmux set-option -g @agent_blink 1 2>/dev/null
        fi
        changed=1
    fi

    if [ "$changed" = 1 ]; then
        tmux refresh-client -S 2>/dev/null
    fi

    sleep "$POLL_SECONDS"
done
