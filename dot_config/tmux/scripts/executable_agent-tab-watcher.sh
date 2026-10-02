#!/usr/bin/env bash
# Agent tab watcher — presence daemon backing agent-tab-indicator.sh.
#
# Hooks give instant state transitions but can't cover two cases:
#   1. presence with no events yet (agent just launched, or hooks untrusted —
#      codex requires interactive trust approval for new hook entries), and
#   2. cleanup when the agent dies without firing SessionEnd (SIGKILL,
#      kill-pane SIGHUP, crash) — SessionEnd is best-effort for both agents
#      (codex additionally clamps its SessionEnd hook timeout to 3s).
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
# (Workflows are a Claude feature; a codex pid has no ~/.claude/sessions
# file, so codex windows fall through the lookup naturally.)
#
# It also flags COMPUTER USE. cua-mcp-shim stamps every driver tool call into
# ~/Library/Application Support/CuaNotch/activity.json with the owning agent's pid, which is
# the same pane→pid map the workflow lookup already builds — so a window whose
# agent has driven an app within CUA_LIVE seconds gets a per-window @agent_cua
# flag, rendered as a blue robot. This is the tab-bar twin of CuaNotch's blue
# glow segment: same source of truth, same meaning, same hue.
#
# It also drives the running-state animation: while ANY window is in state
# running OR has @agent_workflow / @agent_cua set, the global @agent_blink
# option toggles each tick and the window formats alternate the glyph between
# its color and a dimmed copy of THE SAME hue (a brightness pulse, matching
# CuaNotch's breathing glow — never a hue swap, which would make a state look
# like a different state). Nothing running → no toggling, no redraws.
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

# DEFAULT SERVER ONLY. The singleton below is per-USER (one pidfile, a pgrep
# sweep over every matching command line), but tmux.conf is loaded by every
# server — and agents here routinely start scratch servers with `tmux -L
# <name>` (notchlab, stashtest, ...) that source it. On 2026-10-01 a
# `tmux -L notchlab` test server's run-shell spawned a watcher that reaped the
# real one and then drove @agent_blink on the scratch server, so every working
# tab on the user's server sat frozen on plain blue (= idle). run-shell and the
# agent hooks both inherit TMUX="<socket>,<pid>,<session>"; anything not on the
# default socket leaves BEFORE touching the pidfile or killing anything. Empty
# TMUX means the tmux CLI targets the default server anyway, so that's allowed.
_sock="${TMUX%%,*}"
[ -z "$_sock" ] || [ "${_sock##*/}" = default ] || exit 0

# Singleton: every tmux.conf reload (`prefix r`) re-runs the spawn line, so a
# plain check-then-write leaks daemons (a transiently-exiting watcher with an
# unconditional trap can delete a live sibling's pidfile, then the next reload
# finds none and starts another). Reap any prior instance, then claim the
# pidfile, and only clean it up on exit if it's still ours.
PIDFILE="${TMPDIR:-/tmp}/agent-tab-watcher.${UID:-$(id -u)}.pid"
SELF="$HOME/.config/tmux/scripts/agent-tab-watcher.sh"

# A PID IS NOT AN IDENTITY. cleanup() only unlinks the pidfile on a normal
# EXIT, so a SIGKILLed watcher leaves a live-looking pid behind — and pid
# churn on this machine is ~1000/10s, so the space wraps in ~15 minutes. By
# the next `prefix r` that number is very likely some unrelated process, and
# `kill -0` says only "a process exists", not "it is mine". This used to
# SIGTERM whatever answered. Exactly the hazard the -fx sweep below documents
# ("killing a bystander is not [harmless]") — the pidfile path just had no
# equivalent guard. One ps, at startup only; the loop never forks for this.
is_watcher_pid() {
    local cmd
    cmd="$(ps -o command= -p "$1" 2>/dev/null)"
    [ "$cmd" = "bash $SELF" ] || [ "$cmd" = "/bin/bash $SELF" ] || [ "$cmd" = "$SELF" ]
}

prev=$(cat "$PIDFILE" 2>/dev/null || true)
if [ -n "$prev" ] && [ "$prev" != "$$" ] && kill -0 "$prev" 2>/dev/null \
   && is_watcher_pid "$prev"; then
    kill "$prev" 2>/dev/null || true
fi
# Belt to the pidfile's braces: also sweep for stragglers whose pidfile we
# can't see (a respawn under a different TMPDIR, a cleaned tmp dir). Two live
# daemons would both toggle @agent_blink each second and the glyph would sit
# on one color — the exact symptom this daemon exists to produce. One pgrep at
# startup only; the loop below never forks for this.
#
# -fx (whole command line must match EXACTLY) is load-bearing: a substring
# `pgrep -f agent-tab-watcher` also matches any shell, editor or grep whose
# argv merely mentions this path, and we kill what we match. Missing a stray
# spawned some other way is harmless (the pidfile still covers the normal
# case); killing a bystander is not. ($SELF is defined with is_watcher_pid
# above, which applies the same exactness to the pidfile path.)
if command -v pgrep >/dev/null 2>&1; then
    for stray in $(pgrep -fx "bash $SELF" 2>/dev/null; pgrep -fx "/bin/bash $SELF" 2>/dev/null; pgrep -fx "$SELF" 2>/dev/null); do
        if [ "$stray" != "$$" ] && [ "$stray" != "$PPID" ]; then
            kill "$stray" 2>/dev/null || true
        fi
    done
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
    # ${1##*/}, not basename: this runs for every tty-owning process on every
    # 1s tick (~60 fork+exec per tick, ~5M/day) — measured 109ms/tick vs
    # 9.5ms for the builtin. ps `comm` is never "/" or a trailing-slash path,
    # so the expansion is exactly equivalent here (and it doesn't choke on
    # comm values like "-zsh", which basename parses as an option).
    local base="${1##*/}"
    case "$base" in
        claude|codex) return 0 ;;
    esac
    # Claude's binary is version-named (e.g. "2.1.170") in case its
    # process-title rename ever stops applying. Anchored regex, digits-only
    # segments — a case glob like [0-9]*.[0-9]*.[0-9]* would also match
    # IP-like or suffixed names ("10.0.0.1", "1.2.3-beta").
    [[ "$base" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

# Claude Code's own view of whether the session is mid-turn, echoed as
# "busy" | "idle" | "" (unknown/not a claude session).
#
# ~/.claude/sessions/<pid>.json carries a "status" field that Claude maintains
# itself, and it is the only GROUND TRUTH here — every other signal we have is
# an inference from hooks, and hooks only tell us about transitions they
# actually fire. Interrupting a turn with Esc fires NOTHING: no Stop, no
# StopFailure (there is no interrupt/abort event in the wired set at all), so
# @agent_state sat at `running` and the tab pulsed for a session doing nothing
# — reported from the field, reproduced with tab main:3 showing running while
# its session file read idle for 156s. Same stuck state arrives from a missed
# Stop, a hook that failed to run, or the deliberate SessionStart(compact)
# skip, so reconciling against status fixes the whole class rather than one
# cause. Builtin read, no fork; unknown values are left alone deliberately.
session_status() {
    local sf raw
    [ -n "$1" ] || return 0
    sf="$HOME/.claude/sessions/$1.json"
    [ -f "$sf" ] || return 0
    raw="$(<"$sf")"
    [[ $raw =~ \"status\":\"([^\"]+)\" ]] && printf '%s' "${BASH_REMATCH[1]}"
}

# Codex's answer to the same question, from its rollout stream: "busy" | "idle"
# | "" (unknown). Codex has no ~/.claude/sessions equivalent and no pid→thread
# mapping we could follow, so agent-tab-indicator.sh stashes the thread's
# rollout_path in @agent_rollout (it already queries that row on every codex
# hook) and this reads the tail of it.
#
# The stream records turn boundaries explicitly: event_msg task_started opens a
# turn, task_complete closes it, and an INTERRUPT writes turn_aborted (verified
# against the on-disk corpus: 51 starts, 41 completes, 8 aborts). So the turn is
# live iff the most recent of the three is task_started — real state, not an
# inference from how recently the file was touched.
#
# Bounded tail, never the whole file: rollouts run to 27MB here (p90 791KB).
# If no marker falls in the tail the answer is "" and the caller does nothing,
# which is the same conservative failure as not looking at all.
#
# awk, NOT bash string ops. The obvious `${chunk##*"$marker"}` trick to find a
# last occurrence is O(n^2) on a 256KB string — it hung this function outright
# on the first large rollout it met. One linear pass instead; records are
# JSONL, one event per line, so the last line carrying any of the three
# markers decides. A truncated first line from the byte-oriented tail is
# harmless.
CODEX_TAIL_BYTES=262144
codex_status() {
    local rp="$1"
    [ -n "$rp" ] && [ -f "$rp" ] || return 0
    tail -c "$CODEX_TAIL_BYTES" "$rp" 2>/dev/null | awk '
        /"type":"task_started"/                        { last = "busy" }
        /"type":"task_complete"/ || /"type":"turn_aborted"/ { last = "idle" }
        END { printf "%s", last }
    '
}

# Background-work detection — resolve_session_bases (the compaction-chain
# walk), notified_ids, session_has_running_subagent and
# session_has_running_workflow — lives in agent-session-lib.sh, SHARED with
# stash.sh so the tab bar's gear and the park/kill guard run the same rules
# and can never drift apart again (stash.sh once carried its own older
# copies; that drift is how a park killed two live reviewers on 2026-08-25).
# cua-notch's check-invariants pins those rules against the lib file. If the
# lib is missing, fail toward "no gear" — the tab loses its workflow glyph,
# an honest cosmetic loss; stash.sh's stub for the same functions fails the
# OPPOSITE way (toward "still running"), so a missing lib can never become
# a kill.
if ! . "$HOME/.config/tmux/scripts/agent-session-lib.sh" 2>/dev/null; then
    session_has_running_workflow() { return 1; }
    session_has_running_subagent() { return 1; }
fi

# Agent pids that have driven an app within CUA_LIVE seconds, space-delimited
# (" 123 456 "). Mirrors CuaNotch's own liveWindow so the tab and the notch
# light up and go dark together. Missing/garbage file → empty (never fatal).
ACTIVITY="$HOME/Library/Application Support/CuaNotch/activity.json"
# 60s, matching CuaNotch's liveWindow AND the shim's LOCK_TTL: a drive is in
# progress for as long as the shim holds its window lock, and an agent that
# thinks for 25s between two clicks is still driving. At 15 the tab glyph
# dropped out and came back on every model turn while the notch stayed lit —
# the two surfaces must light up and go dark together. Change both or neither.
CUA_LIVE=60
live_cua_pids() {
    [ -f "$ACTIVITY" ] || { printf ' '; return 0; }
    # Cheap gate before starting an interpreter (~22ms, every second,
    # forever): the shim rewrites this file on every driver call, so every
    # session ts is <= its mtime — an untouched file cannot hold a live
    # session, and the answer is the empty set without any python at all.
    local _now _amt
    printf -v _now '%(%s)T' -1
    _amt=$(stat -f %m "$ACTIVITY" 2>/dev/null || echo 0)
    [ $((_now - _amt)) -lt "$CUA_LIVE" ] || { printf ' '; return 0; }
    /usr/bin/python3 - "$ACTIVITY" "$CUA_LIVE" <<'PY' 2>/dev/null || printf ' '
import json, sys, time
try:
    with open(sys.argv[1]) as f:
        sessions = json.load(f).get("sessions", {}) or {}
except Exception:
    print(" "); raise SystemExit(0)
now, live = time.time(), float(sys.argv[2])
pids = {int(d["agent_pid"]) for d in sessions.values()
        if isinstance(d, dict) and d.get("agent_pid")
        and now - float(d.get("ts") or 0) < live}
# Emit a SINGLE space when the set is empty, matching the early-return paths.
# " " + "" + " " gave two, and the consumer's `*" ${pid} "*` test matches that
# with an empty pid — so an unset pid would have read as "driving an app" if
# its [ -n "$pid" ] guard were ever dropped. Don't leave a guard load-bearing
# in a caller when the producer can just not lie.
print((" " + " ".join(str(p) for p in sorted(pids)) + " ") if pids else " ")
PY
}

# A failed tmux command is NOT proof the server died — it can also be a
# transient hiccup (server mid-reload, EINTR, fd pressure). Exiting on the
# first one is how the daemon silently disappears after days of uptime, taking
# the blink, the workflow gear and the state GC with it and leaving no trace.
# Tolerate a short streak, and only quit once the server is confirmed gone.
FAIL_LIMIT=5
fail_streak=0
server_gone() { ! tmux list-sessions >/dev/null 2>&1; }

# Consecutive-idle counters for the stuck-`running` reconcile, carried across
# ticks as " @win=N @win=N " (see the hysteresis note in the loop).
idle_streak=" "

# Consecutive agent-LESS ticks per window, same " @win=N " shape, for the GC
# below. A window is only garbage-collected after GC_TICKS ticks in a row
# without an agent pane. One tick is not enough: tmux-thumbs (prefix+Space)
# `swap-pane`s the active pane into a throwaway "[thumbs]" window for the
# seconds the picker is up, and a single-tick GC read that as "agent gone",
# wiped @agent_summary/@agent_state, and the tab label stayed blank until the
# next hook refreshed it. A real exit is still collected, just GC_TICKS later.
GC_TICKS=5
gc_streak=" "

while :; do
    # window_id<space>pane_tty for every pane.
    if ! panes=$(tmux list-panes -a -F '#{window_id} #{pane_tty}' 2>/dev/null); then
        fail_streak=$((fail_streak + 1))
        { [ "$fail_streak" -ge "$FAIL_LIMIT" ] && server_gone; } && exit 0
        sleep "$POLL_SECONDS"
        continue
    fi

    # One ps for all TTYs: agent TTYs, plus tty=pid for every agent pane —
    # claude pids feed the workflow lookup (codex pids simply miss in
    # ~/.claude/sessions and fall through), and both kinds feed the
    # computer-use pid match (@agent_cua), which is agent-agnostic.
    # GUARDED like the two tmux reads around it. This was the one data source
    # in the loop that was not: a single empty/failed ps tick made has_agent=0
    # for every window, the GC below unset every @agent_* option, and the next
    # good tick reseeded them all to `idle` - which is TERMINAL for a mid-turn
    # agent, because the heartbeat only re-arms `running` from idle when
    # @agent_pending is set, and the GC had just cleared it. An empty listing
    # is never real (this very shell is in it), so it is treated as a failure.
    if ! ps_out=$(ps -ax -o tty=,pid=,comm= 2>/dev/null) || [ -z "$ps_out" ]; then
        fail_streak=$((fail_streak + 1))
        { [ "$fail_streak" -ge "$FAIL_LIMIT" ] && server_gone; } && exit 0
        sleep "$POLL_SECONDS"
        continue
    fi
    agent_ttys=" "
    tty_pid=" "
    while IFS=' ' read -r tty pid comm; do
        [ -n "$tty" ] && [ "$tty" != "??" ] || continue
        if is_agent_comm "$comm"; then
            agent_ttys="${agent_ttys}${tty} "
            case "${comm##*/}" in
                claude|codex|[0-9]*) tty_pid="${tty_pid}${tty}=${pid} " ;;
            esac
        fi
    done <<EOF
$ps_out
EOF

    # Current per-window state in one call (formats resolve window options).
    if ! states=$(tmux list-windows -a -F '#{window_id} #{@agent_state} #{window_active_clients}' 2>/dev/null); then
        fail_streak=$((fail_streak + 1))
        { [ "$fail_streak" -ge "$FAIL_LIMIT" ] && server_gone; } && exit 0
        sleep "$POLL_SECONDS"
        continue
    fi
    fail_streak=0

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

    # Same, for @agent_cua, plus this tick's live driver pids (one read).
    cua_now=" "
    while IFS=' ' read -r win rest; do
        [ -n "$win" ] && [ -n "$rest" ] && cua_now="${cua_now}${win} "
    done <<EOF
$(tmux list-windows -a -F '#{window_id} #{@agent_cua}' 2>/dev/null)
EOF
    cua_pids=$(live_cua_pids)

    # win=<rollout path> for codex windows (see codex_status). Paths live under
    # ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl and contain no
    # spaces, so the flat "win=value" encoding used elsewhere holds.
    roll_map=" "
    while IFS=' ' read -r win rest; do
        [ -n "$win" ] && [ -n "$rest" ] && roll_map="${roll_map}${win}=${rest} "
    done <<EOF
$(tmux list-windows -a -F '#{window_id} #{@agent_rollout}' 2>/dev/null)
EOF

    changed=0
    any_workflow=0
    any_cua=0
    # Rebuilt each tick; a window that stops reading idle drops out, so the
    # streak only ever counts CONSECUTIVE observations.
    idle_streak_next=" "
    gc_streak_next=" "
    wez_front=""   # per tick, computed at most once, only if a tinted tab is watched
    while IFS=' ' read -r win state wac; do
        # SEEN-IT, CONTINUOUSLY. The hook discharges a yellow/green that lands
        # while the user is sitting on the tab with WezTerm focused; this is
        # the other order - the tint landed while WezTerm was behind something,
        # and the user then Cmd-Tabbed back to it without changing tabs, so no
        # select-window hook ever fires. Same test (active for a client AND
        # WezTerm frontmost), same discharge as clear-current, once a second.
        # Red is left alone: a dead turn is not answered by being looked at.
        # The frontmost lookup forks twice, so it runs only when there is a
        # tinted, watched window to ask about - almost never.
        case "$state" in
            done|needs-input)
                case "$wac" in ''|0|*[!0-9]*) : ;; *)
                    if [ -z "$wez_front" ]; then
                        wez_front=no
                        [ "$(lsappinfo info -only bundleid "$(lsappinfo front 2>/dev/null)" 2>/dev/null)" = \
                          '"CFBundleIdentifier"="com.github.wez.wezterm"' ] && wez_front=yes
                    fi
                    if [ "$wez_front" = yes ]; then
                        tmux set-option -w -t "$win" @agent_state idle 2>/dev/null && changed=1
                        [ "$state" = needs-input ] && \
                            tmux set-option -w -t "$win" @agent_pending "$(printf '%(%s)T' -1)" 2>/dev/null
                        state=idle
                    fi ;;
                esac ;;
        esac
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
        case "$cua_now" in
            *" ${win} "*) had_cua=1 ;;
            *) had_cua=0 ;;
        esac

        # Background-workflow + computer-use detection (both need a live agent
        # and share the one pane→pid lookup; workflows are claude-only).
        wf=0
        cua=0
        if [ "$has_agent" = 1 ]; then
            pid=""
            for kv in $win_pid; do
                case "$kv" in "${win}="*) pid="${kv#*=}"; break ;; esac
            done
            # A workflow or a background subagent: one gear for both.
            if [ -n "$pid" ] && { session_has_running_workflow "$pid" \
                                  || session_has_running_subagent "$pid"; }; then
                wf=1; any_workflow=1
            fi
            case "$cua_pids" in
                *" ${pid} "*) [ -n "$pid" ] && { cua=1; any_cua=1; } ;;
            esac
        fi
        if [ "$cua" = 1 ] && [ "$had_cua" = 0 ]; then
            tmux set-option -w -t "$win" @agent_cua 1 2>/dev/null && changed=1
        elif [ "$cua" = 0 ] && [ "$had_cua" = 1 ]; then
            tmux set-option -uw -t "$win" @agent_cua 2>/dev/null && changed=1
        fi
        if [ "$wf" = 1 ] && [ "$had_wf" = 0 ]; then
            tmux set-option -w -t "$win" @agent_workflow 1 2>/dev/null && changed=1
        elif [ "$wf" = 0 ] && [ "$had_wf" = 1 ]; then
            tmux set-option -uw -t "$win" @agent_workflow 2>/dev/null && changed=1
        fi

        # Un-stick a `running` tab whose session says it is idle (see
        # session_status). ONLY `running` is reconciled: the attention states
        # are "always asserted, discharged by focus" on purpose, and a session
        # sitting on an open permission gate also reads idle — clearing those
        # from here would silently drop live prompts, the one thing this
        # indicator must never do.
        #
        # HYSTERESIS, not a single reading. At turn start the hook and Claude's
        # own status write race, so one tick can legitimately see
        # state=running with a stale idle status. Acting on that would clear
        # the tab for the WHOLE turn — heartbeat re-arms running only from
        # running/needs-input, never from bare idle, so nothing would put it
        # back. Three consecutive idle ticks costs 3s of latency on a fix for
        # a tab that was previously stuck for the rest of the session, and
        # makes the race require three impossible coincidences in a row.
        agent_idle=0
        if [ "$state" = "running" ] && [ "$has_agent" = 1 ]; then
            st=""
            [ -n "$pid" ] && st=$(session_status "$pid")        # claude
            if [ -z "$st" ]; then                               # codex
                rp=""
                for kv in $roll_map; do
                    case "$kv" in "${win}="*) rp="${kv#*=}"; break ;; esac
                done
                # Only reached for a codex window ALREADY showing running, so
                # the tail read is bounded to that case; a long live turn pays
                # one read per tick until it ends, which is the price of having
                # no status file to poll.
                [ -n "$rp" ] && st=$(codex_status "$rp")
            fi
            # "shell" is Claude Code's at-the-prompt status now (seen on every
            # idle session, 2.1.278-2.1.284, 2026-09-29) - checking for "idle"
            # alone left a tab that missed its Stop pinned at running.
            # "waiting" (a gate on screen) and "busy" still never qualify.
            case "$st" in idle|shell) agent_idle=1 ;; esac
        fi
        if [ "$agent_idle" = 1 ]; then
            n=0
            for kv in $idle_streak; do
                case "$kv" in "${win}="*) n="${kv#*=}"; break ;; esac
            done
            n=$((n + 1))
            if [ "$n" -ge 3 ]; then
                tmux set-option -w -t "$win" @agent_state idle 2>/dev/null && changed=1
                state=idle
            else
                idle_streak_next="${idle_streak_next}${win}=${n} "
            fi
        fi

        if [ "$has_agent" = 1 ] && [ -z "$state" ]; then
            tmux set-option -w -t "$win" @agent_state idle 2>/dev/null && changed=1
        elif [ "$has_agent" = 0 ] && { [ -n "$state" ] || [ "$has_summary" = 1 ]; }; then
            n=0
            for kv in $gc_streak; do
                case "$kv" in "${win}="*) n="${kv#*=}"; break ;; esac
            done
            n=$((n + 1))
            if [ "$n" -ge "$GC_TICKS" ]; then
                tmux set-option -uw -t "$win" @agent_state 2>/dev/null
                tmux set-option -uw -t "$win" @agent_summary 2>/dev/null
                tmux set-option -uw -t "$win" @agent_summary_cond 2>/dev/null
                tmux set-option -uw -t "$win" @agent_pending 2>/dev/null
                tmux set-option -uw -t "$win" @agent_rollout 2>/dev/null
                tmux set-option -uw -t "$win" @agent_session_id 2>/dev/null
                tmux set-option -uw -t "$win" @agent_owner_token 2>/dev/null
                changed=1
            else
                gc_streak_next="${gc_streak_next}${win}=${n} "
            fi
        fi
    done <<EOF
$states
EOF
    gc_streak="$gc_streak_next"

    idle_streak="$idle_streak_next"

    # Blink driver: toggle while anything is running or has a workflow in
    # flight; redraw covers both the toggle and any reconcile changes above.
    blink_active=0
    case "$states" in *" running"*) blink_active=1 ;; esac
    [ "$any_workflow" = 1 ] && blink_active=1
    [ "$any_cua" = 1 ] && blink_active=1
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

    # HEARTBEAT. Being alive is not the same as turning: every check on this
    # daemon (the pidfile, ensure_watcher's kill -0 + ps) proves a PROCESS
    # exists, and none of them prove the LOOP is still going round. A tmux
    # call or the python in live_cua_pids wedging would leave a healthy-
    # looking watcher that has quietly stopped reconciling, and since the
    # working chip went pink↔blue a frozen blink renders as plain blue —
    # i.e. identical to idle, so the surface lies rather than merely going
    # quiet. Restamping the pidfile each tick makes the mtime a liveness
    # clock that ensure_watcher can test. Builtin redirect: no fork.
    #
    # Reading it back first also settles a race the startup guard can't: if
    # a newer instance has claimed the file, WE are the stale one and should
    # go, rather than both of us toggling @agent_blink and cancelling out.
    read -r _owner < "$PIDFILE" 2>/dev/null || _owner="$$"
    [ "$_owner" = "$$" ] || exit 0
    echo $$ > "$PIDFILE"

    sleep "$POLL_SECONDS"
done
