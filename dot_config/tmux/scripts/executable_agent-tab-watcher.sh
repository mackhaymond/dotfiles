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
#   agent present + no @agent_kind     → its comm's kind (codex|claude|opencode)
#   no agent     + any state OR summary → unset @agent_state/@agent_summary (GC)
#   no agent     + fresh @agent_born    → left alone (BIRTH GRACE: a claude tab
#                                         marked an agent at creation, before
#                                         claude itself is running)
#   agent        + @agent_born          → unset @agent_born (it arrived)
# Hook-set states (running/needs-input/done) are never overridden while the
# agent lives, except by the agent's own record of the turn: a `running` its
# session/rollout says is over, and a codex `needs-input` or `running` whose
# rollout ended the turn after the state was stamped (see the reconcile).
# When such an ending closes a turn another agent started, the window gets
# back its saved prior state instead (AGENT-ORIGINATED TURNS); while the pause
# key's @agent_paused is set, every such ending writes `paused` (and nothing
# here ever moves a `paused` window: docs/agent-pause.md, "The paused state").
# A codex turn that ended on a usage limit is marked rate-limited (paused +
# @agent_limit), and agent-pause.py's limit-tick is kicked when a limit may
# have lifted (RATE-LIMITED AGENTS, below).
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
# don't match). OpenCode is one native process, comm "opencode", and counts
# only as its interactive TUI (opencode_tui_args: `opencode serve` is not an
# agent). Candidates come from `pgrep` (cheap), and each one's tty+comm
# from a single-pid ps, cached per pid — see PROCESS DISCOVERY in the loop.
#
# TICK TRACE (live measurement, no restart needed). If the file
#   ${TMPDIR:-/tmp}/agent-tab-watcher.$UID.trace
# EXISTS at the start of a tick, the tick appends one line to it:
#   <epoch> <tick_ms> <windows> <agents>
# epoch = integer seconds at tick start, tick_ms = wall time from the top of
# the tick to just before its sleep, windows = distinct windows reconciled
# (a window linked into two sessions counts once; idle fast-path windows
# count), agents = tty-owning agent processes seen. `: > that-file` to start, `rm` it to stop
# (it is never truncated or rotated by the watcher). Ticks that bail out on
# the failure path write no line. Needs $EPOCHREALTIME (bash 5); with tracing
# off the cost is one builtin file test per tick, no fork.
#
# AGENT EVENT LOG (the sidebar's "log" section reads it). Every time a tick
# reads a window's @agent_state as different from what the previous tick
# read, one line is appended to
#   ${TMPDIR:-/tmp}/agent-events.$UID.log     (a trailing / on TMPDIR is dropped)
# as US (\x1f) separated fields, newline-terminated:
#   epoch US window_id US session US window_index US state US prev_state US
#   detail_kind US title US detail
# epoch = integer seconds of the tick, state = the new @agent_state,
# prev_state = the last one read ("" for a window first seen with a state),
# title = @agent_summary, else the window name (the roster's fallback),
# detail_kind/detail = @agent_detail_kind/@agent_detail as read with it, both
# left EMPTY on an idle line (the discharge, the stuck-running reconcile and a
# bare SessionStart change only the state, so the detail still describes the
# event before). US, \n and \r are stripped from every value. Only non-empty
# states are logged: a window whose state is unset (GC) or that disappears
# writes nothing. It compares what tmux HOLDS, read once a tick, so a change
# the watcher makes itself (the idle seed, the seen-it discharge) is logged
# once, a tick later, like any other.
# RESTARTS (`prefix r`, ensure_watcher's reap) must not lose a transition
# that landed while no watcher ran: the first tick compares each window with
# the state of its NEWEST log line instead of logging nothing - but only
# while that line's session+index still match the window id in the read
# (ids restart from @0 with the tmux server). A window without such a line is
# a silent baseline.
# Capped: past EV_MAX (2000) lines the file is rewritten to its newest
# EV_KEEP (1000) via a temp file + mv in the same directory (stray temps are
# swept at startup). Only a regular file (or none) is ever written: a FIFO
# there would block the daemon, a symlink would redirect it. The watcher is
# the only writer; appends are builtin printf, no fork.
#
# AGENT SLOTS (@agent_slot, the number labels agent-ui's Option-W menu and
# sidebar show). Every ELIGIBLE window carries the window option @agent_slot,
# a decimal integer >= 1. Eligible = in the ACTIVE set (agent-jump's queue
# plus working, the menu's Active tab): @agent_state running, failed,
# needs-input or done, OR @agent_state is idle AND @agent_pending is an
# epoch no older than 600 s (an answered prompt: its turn resumes, the
# number stays; an older or junk stamp is unset by the tick), OR
# @agent_workflow set, OR @agent_cua set (so a done with a gear counts, as
# working) - AND at least one of the window's links in a session outside
# SLOT_EXCLUDE (agent-jump.sh's EXCLUDE, byte for byte: agents tasks stash
# scratch btop-popup). A seen done (idle, no pending), no state, or no agent
# at all is not active. A window linked into several sessions has one window
# option, so it holds one slot. The rules live in the SLOT CORE below, which
# agent-tab-indicator.sh carries byte for byte: the hook STAMPS a window the
# moment it sets an active state (under the slot lock), and this tick is the
# backstop - it stamps what became active without a hook (a gear, a robot),
# releases, and repairs. Each tick, after the per-window reconcile (so its
# own writes - the stuck-running reconcile, the seen-it discharge, the GC
# - count at once):
#   - a slot on a window that is not eligible is unset (its agent went idle
#     or exited, it was parked into stash, ...). A closed window simply
#     leaves the read; its number is free from that tick on. A MISSING agent
#     pane releases nothing early: tmux-thumbs swaps the pane out for
#     seconds, so the state AND the @agent_workflow / @agent_cua flags are
#     held until the GC streak (GC_TICKS) expires, and only then dropped.
#   - eligible windows keep a VALID slot (^[1-9][0-9]{0,8}$: canonical
#     decimal, no sign, no leading zero) that no other eligible window
#     claims; on a duplicate the lower window id (numeric: @12 < @100) keeps
#     it.
#   - every other eligible window (none, junk, a duplicate's loser) gets the
#     LOWEST positive integer no eligible window holds, filled in order of
#     session name in BYTE order (LC_ALL=C: "Zeta" < "alpha"), then window
#     index, then window id. A linked window sorts by its byte-smallest
#     visible session and its index there.
# No other window's slot ever changes, so a closed agent leaves a gap and the
# ones above stay put; the next new agent fills the lowest gap. Stateless:
# whether anything needs doing is decided from the tick's one list-panes read
# (the slot and @agent_pending ride in it), fork-free, so the idle fast path
# needs nothing extra and a restart re-reads the slots tmux holds instead of
# renumbering. Only a tick that has something to write takes the slot lock,
# re-reads (one list-windows -a: a hook may have stamped since), decides
# again from that and writes in one tmux call; refresh-client -S stays its
# own call after it (a set on a window closed since the read fails, and a
# failure ends the rest of a tmux command list).
#
# Singleton + lifecycle follow coffee-watcher.sh: PID-file guard, exits when
# the tmux server goes away, writes only on change then refresh-client -S.
# Spawned from tmux.conf via `run-shell -b`. set -u/-e relaxed: a daemon
# must survive transient tmux command failures mid-loop.

# 1s: doubles as the blink interval for the running-glyph animation.
POLL_SECONDS=1
# Field separator for multi-field list-windows reads (see the states read).
US=$'\x1f'
# Tests only: exit after this many ticks (unset = run forever).
MAX_TICKS="${AGENT_TAB_WATCHER_MAX_TICKS:-}"

command -v tmux >/dev/null 2>&1 || exit 0
# Bash 4+ required (associative arrays here and in agent-session-lib.sh).
# Under macOS /bin/bash 3.2 `declare -A` fails and the loop would die
# mid-tick; leave before touching the pidfile or reaping anything.
[ "${BASH_VERSINFO[0]:-0}" -ge 4 ] || exit 0

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
# The FULL path, not the basename: a server under a private TMUX_TMPDIR is also
# called "default" (how agents isolate tests), and it must not reap the real one.
# Same rule as kill-guard.sh / resurrect-save.sh.
_sock="${TMUX%%,*}"
if [ -n "$_sock" ]; then
    case "$_sock" in
        "/private/tmp/tmux-${UID:-$(id -u)}/default"|"/tmp/tmux-${UID:-$(id -u)}/default") ;;
        *) exit 0 ;;
    esac
fi

# Singleton: every tmux.conf reload (`prefix r`) re-runs the spawn line, so a
# plain check-then-write leaks daemons (a transiently-exiting watcher with an
# unconditional trap can delete a live sibling's pidfile, then the next reload
# finds none and starts another). Reap any prior instance, then claim the
# pidfile, and only clean it up on exit if it's still ours.
PIDFILE="${TMPDIR:-/tmp}/agent-tab-watcher.${UID:-$(id -u)}.pid"
SELF="$HOME/.config/tmux/scripts/agent-tab-watcher.sh"
# Per-tick mtime reference files (see CHANGE STAMPS below). Named with our
# own pid so cleanup() only ever removes this instance's set.
STAMPS="${TMPDIR:-/tmp}/agent-tab-watcher.${UID:-$(id -u)}.$$.stamp"
# The agent event log (see AGENT EVENT LOG); its rotation temps are
# "$EVLOG.<pid>.tmp", swept with the stamps below.
EVLOG="${TMPDIR:-/tmp}"
EVLOG="${EVLOG%/}/agent-events.${UID:-$(id -u)}.log"

# STATE NOTICE (see agent-tab-indicator.sh, which posts the same name): this
# tick changed some window's @agent_state, so CuaNotch should re-read tmux.
notify_state() {
    local name="${AGENT_STATE_NOTICE-com.mackhaymond.tmux.agent-state}"
    [ -n "$name" ] || return 0
    notifyutil -p "$name" >/dev/null 2>&1 || true
}

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
# ATOMIC WRITES ONLY. `echo $$ > "$PIDFILE"` is truncate-THEN-write, and in
# that window a reader sees an empty file: measured ~22% empty reads in a
# tight writer/reader loop. The pulse child reads this file every second and
# used to treat an empty read as "I've been superseded" and exit, which froze
# the pulse for good (~1-2 child deaths a day). A CLAIM (startup, or a tick
# that finds the file empty/missing) writes a sibling temp file and renames it
# over the pidfile: rename(2) within one directory is atomic, so a reader sees
# the old complete inode or the new complete one, never a half-written one.
# The per-tick restamp does not use this (see restamp_pidfile): an mv is a
# fork, and the loop runs 86400 times a day.
write_pidfile() {
    printf '%s\n' "$$" > "$PIDFILE.$$" 2>/dev/null \
        && mv -f "$PIDFILE.$$" "$PIDFILE" 2>/dev/null \
        || rm -f "$PIDFILE.$$" 2>/dev/null
}
write_pidfile
# Stamp sets left by instances that never ran cleanup() (SIGKILL). Every
# other instance has been reaped above, and a stamp that vanishes under a
# live reader only ever reads as "changed" (see changed_since). Startup only.
# Same for an event-log rotation temp a killed instance left mid-rewrite.
rm -f "${TMPDIR:-/tmp}/agent-tab-watcher.${UID:-$(id -u)}".*.stamp.* "$EVLOG".*.tmp 2>/dev/null
# Heartbeat restamp, builtin only. Called only when the file already reads
# exactly "$$" (ours), so this rewrites the IDENTICAL bytes in place: `1<>`
# opens read-write WITHOUT truncating, so a concurrent reader sees the same
# complete content before, during and after, and the write() still advances
# the mtime that ensure_watcher and the roster treat as the liveness clock.
restamp_pidfile() {
    printf '%s\n' "$$" 2>/dev/null 1<>"$PIDFILE" || write_pidfile
}
# Is $BLINK_PID still OUR running pulse child? Asked of bash's own job table,
# never of the pid alone: once the child dies and bash reaps it, that number
# is free for reuse (the pid space wraps in ~15 minutes here), and `kill -0`
# would happily answer for a stranger. `jobs -r` lists only async children
# this shell forked and has NOT yet reaped as dead - and an unreaped child's
# pid cannot be reused - so membership is proof of identity. (A comsub fork,
# so only where it is rare: cleanup, not the 1 Hz loop.)
blink_is_ours() {
    [ -n "${BLINK_PID:-}" ] || return 1
    case " $(jobs -rp 2>/dev/null | tr '\n' ' ') " in
        *" $BLINK_PID "*) return 0 ;;
    esac
    return 1
}
# Remove the pidfile on exit only if it's still ours. The signal traps must
# EXIT (a bare cleanup trap on TERM/INT/HUP would run the handler and then
# RESUME the loop — the daemon would survive `kill`, which is exactly how the
# old version leaked); routing signals through `exit 0` fires the EXIT trap.
# The pulse child is killed only after blink_is_ours re-verifies it: this used
# to `kill "$BLINK_PID"` unconditionally, and a child that had died earlier
# left a number that could, by now, be any unrelated process.
cleanup() {
    if blink_is_ours; then
        kill "$BLINK_PID" 2>/dev/null
    fi
    BLINK_PID=""
    [ "$(cat "$PIDFILE" 2>/dev/null)" = "$$" ] && rm -f "$PIDFILE"
    [ -e "$PIDFILE.$$" ] && rm -f "$PIDFILE.$$"
    # A TERM landing mid-section must not leave the slot lock to time out.
    declare -F slot_unlock >/dev/null && slot_unlock
    # Guarded: an empty $STAMPS would make this `rm -f .*` in the cwd.
    [ -n "${STAMPS:-}" ] && rm -f "$STAMPS".* 2>/dev/null
}
trap cleanup EXIT
trap 'exit 0' INT TERM HUP

is_agent_comm() {
    # ${1##*/}, not basename: this used to run for every tty-owning process
    # on every 1s tick (~60 fork+exec per tick, ~5M/day) — measured
    # 109ms/tick vs 9.5ms for the builtin. (Now it runs once per newly seen
    # pgrep candidate, but there is still no reason to fork for it.) ps
    # `comm` is never "/" or a trailing-slash path,
    # so the expansion is exactly equivalent here (and it doesn't choke on
    # comm values like "-zsh", which basename parses as an option).
    local base="${1##*/}"
    case "$base" in
        claude|codex|opencode) return 0 ;;
    esac
    # Claude's binary is version-named (e.g. "2.1.170") in case its
    # process-title rename ever stops applying. Anchored regex, digits-only
    # segments — a case glob like [0-9]*.[0-9]*.[0-9]* would also match
    # IP-like or suffixed names ("10.0.0.1", "1.2.3-beta").
    [[ "$base" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

# Claude Code's own view of whether the session is mid-turn, stored in the
# caller's `st` as "busy" | "idle" | "" (unknown/not a claude session).
# Assigned, not echoed: `st=$(session_status ...)` was a comsub, i.e. a fork
# per running claude window per tick, around a function that is otherwise
# all builtins.
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
    st=""
    [ -n "$1" ] || return 0
    sf="$HOME/.claude/sessions/$1.json"
    [ -f "$sf" ] || return 0
    raw="$(<"$sf")"
    [[ $raw =~ \"status\":\"([^\"]+)\" ]] && st="${BASH_REMATCH[1]}"
    return 0
}

# OpenCode's answer, from the status file its tmux-agent-state plugin keeps:
#   ${XDG_STATE_HOME:-$HOME/.local/state}/tmux-agent/opencode/<pid>.status
# one line, "busy" or "idle", written atomically (tmp + rename) on every
# session status change and removed (best effort) when the process exits.
# Into the caller's `st` like session_status: "busy" | "idle" | "" (no file,
# or anything else - left alone, the same conservative failure). Builtin read,
# no fork; the file is a few bytes.
OPENCODE_STATUS_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/tmux-agent/opencode"
opencode_status() {
    local sf raw
    st=""
    [ -n "$1" ] || return 0
    sf="$OPENCODE_STATUS_DIR/$1.status"
    [ -f "$sf" ] || return 0
    raw="$(<"$sf")"
    case "$raw" in busy|idle) st="$raw" ;; esac
    return 0
}

# Is this opencode command line ($1, `ps -o args=`) the interactive TUI? Only
# that is an agent. `opencode` with no subcommand, a project dir
# (`opencode ~/code/x`), -c/--continue, -s/--session <id>, --mini, `opencode
# pr <n>` (checks the PR out, then runs the TUI): yes. Every other subcommand
# - serve, web, acp, mcp ..., debug ..., run (one-shot), attach <url> (a TUI,
# but on a remote server whose plugin cannot report this pane, so it would
# sit `idle` forever), export, stats, models, ... - is a shell running a
# command, not an agent tab. The first bare word decides; options that take
# a value skip it (`opencode -m openai/gpt-5 serve` is still serve). ps
# joins argv with spaces, so a --prompt with spaces in it can shift that by
# a word - harmless unless its 2nd word is a subcommand name. Builtins only.
opencode_tui_args() {
    local -a a
    local i=1 w
    read -r -a a <<<"$1"
    while [ "$i" -lt "${#a[@]}" ]; do
        w="${a[i]}"
        case "$w" in
            --log-level|--port|--hostname|--mdns-domain|--cors|-m|--model|-s|--session|--prompt|--agent|--replay-limit)
                i=$((i + 2)); continue ;;
            --) return 0 ;;
            -*) i=$((i + 1)); continue ;;
            completion|acp|mcp|attach|run|debug|providers|auth|agent|upgrade|uninstall|serve|web|models|stats|export|import|github|session|plugin|plug|db)
                return 1 ;;
            *) return 0 ;;
        esac
    done
    return 0
}

# Codex's answer to the same question, from its rollout stream, into the
# caller's cx_mark = "started" | "complete" | "aborted" | "limit" (a
# task_complete whose error is usage_limit_exceeded: RATE-LIMITED AGENTS) |
# "" (unknown) and
# cx_ts = that event's own "timestamp", UTC to the second
# ("2026-10-04T21:53:53"; "" for a start, or a line whose head the tail cut).
# Codex has no ~/.claude/sessions equivalent and no pid→thread mapping we could
# follow, so agent-tab-indicator.sh stashes the thread's rollout_path in
# @agent_rollout (it already queries that row on every codex hook) and this
# reads the tail of it.
#
# The stream records turn boundaries explicitly: event_msg task_started opens a
# turn, task_complete closes it, and an INTERRUPT - Esc mid-turn, or Esc on an
# approval prompt - writes turn_aborted (reason "interrupted"). So the turn is
# live iff the most recent of the three is task_started — real state, not an
# inference from how recently the file was touched. Matched as the exact line
# shape codex 0.160 writes, checked against every rollout on disk (1661
# starts, 1468 completes, 138 aborts, all of this form):
#   {"timestamp":"2026-10-04T21:53:53.001Z","ordinal":594,"type":"event_msg",
#    "payload":{"type":"turn_aborted","turn_id":"…","reason":"interrupted",…}}
# "ordinal" is absent in older files; the key run from "type":"event_msg" on is
# the same everywhere. "timestamp" is always the line's first key, and it is
# used rather than payload.completed_at because 6 of the aborts (some as recent
# as 2026-10-05) and 21 of the completes carry no completed_at. There is no
# error event to match: none appears in any rollout here, so a failed turn is
# not inferred from this stream.
#
# Bounded tail, never the whole file: rollouts run to 27MB here (p90 791KB).
# 64KB holds the end of a turn with room to spare: across the 389 rollouts here
# that end between turns, the last turn end starts at most 29KB from EOF
# (measured 2026-10-08). If no marker falls in the
# tail the answer is "" and the caller does nothing, which is the same
# conservative failure as not looking at all.
#
# awk, NOT bash string ops. The obvious `${chunk##*"$marker"}` trick to find a
# last occurrence is O(n^2) on a 256KB string — it hung this function outright
# on the first large rollout it met. One linear pass instead; records are
# JSONL, one event per line, so the last line carrying any of the three
# markers decides. A truncated first line from the byte-oriented tail is
# harmless: at worst it is a marker with no timestamp, which never counts as
# "after" anything (cx_after).
CODEX_TAIL_BYTES=65536
codex_tail() {
    local rp="$1" out
    cx_mark=""; cx_ts=""
    [ -n "$rp" ] && [ -f "$rp" ] || return 0
    out=$(tail -c "$CODEX_TAIL_BYTES" "$rp" 2>/dev/null | awk '
        function ts(  t) {
            if (substr($0, 1, 14) != "{\"timestamp\":\"") return ""
            t = substr($0, 15, 19)
            return (t ~ /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]$/) ? t : ""
        }
        index($0, "\"type\":\"event_msg\",\"payload\":{\"type\":\"task_started\"")  { m = "started";  t = "" }
        index($0, "\"type\":\"event_msg\",\"payload\":{\"type\":\"task_complete\"") {
            m = (index($0, "\"codex_error_info\":\"usage_limit_exceeded\"") ? "limit" : "complete"); t = ts() }
        index($0, "\"type\":\"event_msg\",\"payload\":{\"type\":\"turn_aborted\"")  { m = "aborted";  t = ts() }
        END { printf "%s %s", m, t }
    ')
    cx_mark="${out%% *}"; cx_ts="${out#* }"
    return 0
}
# codex_tail for window $1 (rollout $2), re-read only when it can have changed.
# A codex window sitting on an open prompt used to pay tail+awk (3 forks)
# every second for as long as it waited, on a file nobody was writing. The
# answer is a function of the file's bytes alone, so it is kept per window
# with the path and the tick it was last vouched for, and reused while the
# CHANGE STAMPS (below) say the file has not moved since: builtin -nt, and
# every doubt - an expired or missing stamp, a tie - reads as changed, i.e. a
# fresh read. A hit re-vouches at this tick: unchanged since stamp T means
# unchanged since this tick's later stamp too, and a write racing this look is
# newer than that stamp, so the next tick sees it. cx_after is NOT cached (the
# window's own stamp moves independently). An entry a tick does not touch -
# the window left running/needs-input, lost its agent, or is gone - is dropped
# at the end of that tick (CX_USED).
declare -A CX_PATH=() CX_TICK=() CX_MARK=() CX_TS=() CX_USED=()
codex_tail_cached() {
    local w="$1" rp="$2"
    CX_USED[$w]=1
    if [ "${CX_PATH[$w]:-}" = "$rp" ] && [ -f "$rp" ] \
       && ! changed_since "${CX_TICK[$w]:-}" "$rp"; then
        cx_mark="${CX_MARK[$w]}"; cx_ts="${CX_TS[$w]}"
    else
        codex_tail "$rp"
        CX_PATH[$w]="$rp"; CX_MARK[$w]="$cx_mark"; CX_TS[$w]="$cx_ts"
    fi
    CX_TICK[$w]=$tick_no
}
# Did the turn end codex_tail found (cx_ts) land strictly AFTER this window's
# current state was stamped? @agent_since must describe the state as read
# ("<epoch> <state>"); a missing or mismatched stamp is "don't know" (the
# backstop restamps it this tick, so the next tick can answer). The epoch is
# rendered as the same UTC "YYYY-MM-DDTHH:MM:SS" and compared as a string,
# builtin only. Strictly after, to the second: an event in the stamp's own
# second could be the previous turn's end racing a new prompt (a queued
# message is submitted the instant a turn completes), and missing a real one
# only leaves the tab as it would have been without this check.
cx_after() {
    local e iso
    [ -n "$cx_ts" ] && [ "${since#* }" = "$state" ] || return 1
    e="${since%% *}"
    case "$e" in ''|*[!0-9]*) return 1 ;; esac
    TZ=UTC0 printf -v iso '%(%Y-%m-%dT%H:%M:%S)T' "$e" 2>/dev/null || return 1
    [[ "$cx_ts" > "$iso" ]]
}

# ------------------------------------------------------ rate-limited agents
#
# RATE-LIMITED AGENTS (agent-tab-indicator.sh and the doc, same heading). The
# indicator marks a Claude usage limit from its StopFailure; this daemon does
# two things for the feature:
#   1. CODEX. Codex has no failure hook: a usage limit ends the turn with a
#      rollout event_msg task_complete whose error.codex_error_info is
#      usage_limit_exceeded (codex_tail's "limit" mark). Spotted where the
#      CODEX TURN ENDS reconcile looks (a running/needs-input window, the
#      event after the state's stamp), and also on a done/idle window whose
#      stamp is under LIMIT_FRESH_STAMP s old and the event not older than it
#      (cx_recent: codex may still run its Stop hook for the failed turn,
#      which paints done in the same second; a settled window reads nothing),
#      it is marked exactly as the indicator marks Claude (codex_limit_mark):
#      type codex, the reset parsed from the message's "try again at 6:32 PM."
#      (today, or tomorrow once past) or "try again at Oct 22nd, 2026 2:55
#      AM." (local time), else the last token_count's rate_limits reset (the
#      exhausted window's, else the later), else 0. Then agent-pause.py
#      limit-add W in the background.
#   2. THE RESUME TRIGGER. agent-pause.py keeps the global @agent_limit_due
#      (the earliest moment a limited agent could resume; unset when none
#      can), which rides in the tick's one list-panes read for free. While it
#      is set, `agent-pause.py limit-tick` is spawned in the background, at
#      most once per LIMIT_TICK_EVERY s, when: due <= now; or cswap's
#      sequence.json is newer than our mark (an account switch can lift a
#      limit early; builtin -nt, the mark is rewritten when it fires); or the
#      tick gap says the machine just slept (> LIMIT_WAKE_GAP s between
#      ticks; remembered until it fires). limit-tick takes its own lock.
# AGENT_LIMIT_PAUSE_PY / AGENT_LIMIT_SEQ_FILE override the paths, and
# AGENT_LIMIT_WAKE_GAP the wake gap, AGENT_LIMIT_TICK_EVERY the rate limit
# (tests).
LIMIT_PY="${AGENT_LIMIT_PAUSE_PY:-$HOME/.config/tmux/scripts/agent-pause.py}"
LIMIT_SEQ="${AGENT_LIMIT_SEQ_FILE:-$HOME/.claude-swap-backup/sequence.json}"
LIMIT_SEQ_MARK="${TMPDIR:-/tmp}"
LIMIT_SEQ_MARK="${LIMIT_SEQ_MARK%/}/agent-limit-seq.${UID:-$(id -u)}.mark"
LIMIT_TICK_EVERY="${AGENT_LIMIT_TICK_EVERY:-20}"   # (override: tests)
case "$LIMIT_TICK_EVERY" in ''|*[!0-9]*) LIMIT_TICK_EVERY=20 ;; esac
LIMIT_FRESH_STAMP=30   # a done/idle codex stamp younger than this is looked at (cx_recent)
LIMIT_WAKE_GAP="${AGENT_LIMIT_WAKE_GAP:-30}"   # (override: tests)
case "$LIMIT_WAKE_GAP" in ''|*[!0-9]*) LIMIT_WAKE_GAP=30 ;; esac
lim_last_spawn=0     # epoch of the last limit-tick spawn
lim_prev_tick=0      # the previous tick's tick_now (wake detection)
lim_wake=0           # a wake gap seen and not yet acted on
lim_due=""           # this tick's @agent_limit_due (the global, read with the rows)

# >>> LIMIT CORE >>> (byte-identical in agent-tab-watcher.sh and
# agent-tab-indicator.sh; tests/test_agent_limit.py fails if they drift)
# limit_text R TYPE NOW: into LIM_TXT the sidebar line for a limit, e.g.
# "weekly limit · resets Tue 1:00 AM". The time follows the ONE date rule
# every surface shares (agent-pause.py fmt_local, agent-ui text::when), by
# calendar days in local time: the same day "5:31 PM", one to six days ahead
# "Tue 5:31 PM", anything else (a week or more on, or past) "Nov 2 5:31 PM".
# R 0 (unknown) is the label alone. Fixed words, digits, ':' and '·' only:
# safe in a tmux command string. Builtin clock, no fork; C locale names.
limit_text() {
    local r="$1" ty="$2" now="$3" lab when d0 d1 dk h m s k noon
    local LC_ALL=C
    case "$ty" in
        five_hour) lab="session limit" ;;
        seven_day) lab="weekly limit" ;;
        seven_day_opus) lab="Opus weekly limit" ;;
        seven_day_sonnet) lab="Sonnet weekly limit" ;;
        overage) lab="overage limit" ;;
        codex) lab="Codex limit" ;;
        *) lab="usage limit" ;;
    esac
    if ! [[ $r =~ ^[1-9][0-9]{0,11}$ ]] || ! [[ $now =~ ^[0-9]{1,12}$ ]]; then
        LIM_TXT="$lab"
        return 0
    fi
    printf -v d0 '%(%Y%m%d)T' "$now"
    printf -v d1 '%(%Y%m%d)T' "$r"
    printf -v when '%(%l:%M %p)T' "$r"
    when="${when# }"
    if [ "$d0" = "$d1" ]; then
        LIM_TXT="$lab · resets $when"
        return 0
    fi
    # One to six calendar days ahead: step a day at a time from NOW's local
    # noon (a DST hour can never carry noon across a date).
    printf -v h '%(%H)T' "$now"; printf -v m '%(%M)T' "$now"; printf -v s '%(%S)T' "$now"
    noon=$((now - 10#$h * 3600 - 10#$m * 60 - 10#$s + 43200))
    for k in 1 2 3 4 5 6; do
        printf -v dk '%(%Y%m%d)T' "$((noon + k * 86400))"
        if [ "$dk" = "$d1" ]; then
            printf -v dk '%(%a)T' "$r"
            LIM_TXT="$lab · resets $dk $when"
            return 0
        fi
    done
    printf -v dk '%(%b %e)T' "$r"
    while [[ $dk == *"  "* ]]; do dk="${dk//  / }"; done
    LIM_TXT="$lab · resets $dk $when"
}
# codex_reset_epoch TEXT NOW: into CL_R the epoch of codex's "try again at"
# text, local time: "6:32 PM" (today, tomorrow if already past) or "Oct 22nd,
# 2026 2:55 AM". 0 when it does not parse. BSD date, on the marking path only.
codex_reset_epoch() {
    local t="$1" now="$2" d="" day mon ap
    local re1='^([0-9]{1,2}):([0-9]{2}) ?([AaPp][Mm])$'
    local re2='^([A-Z][a-z]{2})[a-z]* ([0-9]{1,2})(st|nd|rd|th)?,? ([0-9]{4}),? ([0-9]{1,2}):([0-9]{2}) ?([AaPp][Mm])$'
    CL_R=0
    t="${t%.}"
    if [[ $t =~ $re1 ]]; then
        printf -v day '%(%Y-%m-%d)T' "$now"
        t="$day ${BASH_REMATCH[1]}:${BASH_REMATCH[2]}:00 ${BASH_REMATCH[3]^^}"
        d=$(LC_ALL=C date -j -f '%Y-%m-%d %I:%M:%S %p' "$t" +%s 2>/dev/null) || d=""
        case "$d" in ''|*[!0-9]*) d="" ;; esac
        if [ -n "$d" ] && [ "$d" -le "$now" ]; then
            d=$(LC_ALL=C date -j -v+1d -f '%Y-%m-%d %I:%M:%S %p' "$t" +%s 2>/dev/null) || d=""
        fi
    elif [[ $t =~ $re2 ]]; then
        mon="${BASH_REMATCH[1]}"; day="${BASH_REMATCH[2]}"; ap="${BASH_REMATCH[7]^^}"
        d=$(LC_ALL=C date -j -f '%b %d %Y %I:%M:%S %p' \
            "$mon $day ${BASH_REMATCH[4]} ${BASH_REMATCH[5]}:${BASH_REMATCH[6]}:00 $ap" +%s 2>/dev/null) || d=""
    fi
    if [[ $d =~ ^[1-9][0-9]{0,11}$ ]]; then CL_R="$d"; fi
    return 0
}
# codex_limit_parse ROLLOUT NOW: the LAST task_complete in the rollout's tail
# (256 KB, never the whole file). CL_HIT 1 when it is a usage limit
# (error.codex_error_info usage_limit_exceeded) - tested on the WHOLE line,
# as codex_tail tests it, so a long final message can never make the two
# disagree; only the pieces read below are cut. CL_TID its turn_id ("-" when
# none), CL_R its reset: the "try again at" text (codex_reset_epoch, "today"
# meaning the day of the task_complete's own timestamp, else NOW's), else the
# last token_count's rate_limits reset - the window at used_percent >= 100,
# else the later - when still ahead of NOW, else 0. Safe under set -eu.
codex_limit_parse() {
    local rp="$1" now="$2" out="" hit="" ts="" tid="" err="" tk="" msg="" ref o obj p pr re ru=0 rmax=0
    local re_tid='^"turn_id":"([A-Za-z0-9_.:-]{1,128})"' re_try='try again at ([A-Za-z0-9 ,:]{1,60})'
    local re_at='"resets_at":([0-9]{1,12})' re_pct='"used_percent":([0-9]+)'
    local re_ts='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}$'
    CL_HIT=0; CL_TID="-"; CL_R=0
    [ -n "$rp" ] && [ -f "$rp" ] || return 0
    out=$(tail -c 262144 "$rp" 2>/dev/null | awk '
        index($0, "\"type\":\"event_msg\",\"payload\":{\"type\":\"task_complete\"") { tc = $0 }
        index($0, "\"type\":\"event_msg\",\"payload\":{\"type\":\"token_count\"") && index($0, "\"resets_at\":") { tk = $0 }
        END {
            print (index(tc, "\"codex_error_info\":\"usage_limit_exceeded\"") ? 1 : 0)
            print ((substr(tc, 1, 14) == "{\"timestamp\":\"") ? substr(tc, 15, 19) : "")
            p = index(tc, "\"turn_id\":\""); print (p ? substr(tc, p, 200) : "")
            p = index(tc, "\"error\":{"); print (p ? substr(tc, p, 4096) : "")
            print substr(tk, 1, 8192)
        }' 2>/dev/null) || out=""
    { IFS= read -r hit; IFS= read -r ts; IFS= read -r tid; IFS= read -r err; IFS= read -r tk; } <<<"$out" || true
    [ "$hit" = 1 ] || return 0
    CL_HIT=1
    if [[ $tid =~ $re_tid ]]; then CL_TID="${BASH_REMATCH[1]}"; fi
    if [[ $err =~ $re_try ]]; then msg="${BASH_REMATCH[1]% }"; fi
    ref="$now"
    if [[ $ts =~ $re_ts ]]; then
        o=$(TZ=UTC0 date -j -f '%Y-%m-%dT%H:%M:%S' "$ts" +%s 2>/dev/null) || o=""
        if [[ $o =~ ^[1-9][0-9]{0,11}$ ]]; then ref="$o"; fi
    fi
    codex_reset_epoch "$msg" "$ref"
    [ "$CL_R" = 0 ] || return 0
    for p in primary secondary; do
        re="\"$p\":\\{([^}]*)\\}"
        [[ $tk =~ $re ]] || continue
        obj="${BASH_REMATCH[1]}"
        [[ $obj =~ $re_at ]] || continue
        o="${BASH_REMATCH[1]}"
        if [ "$o" -gt "$rmax" ]; then rmax="$o"; fi
        pr=0
        if [[ $obj =~ $re_pct ]]; then pr="${BASH_REMATCH[1]}"; fi
        if [ "$pr" -ge 100 ] && [ "$o" -gt "$ru" ]; then ru="$o"; fi
    done
    if [ "$ru" -gt 0 ]; then CL_R="$ru"; else CL_R="$rmax"; fi
    if [ "$CL_R" -le "$now" ]; then CL_R=0; fi
    return 0
}
# <<< LIMIT CORE <<<

# cx_recent: the codex turn end codex_tail found (cx_ts) is no older than
# this window's state stamp less 5 s - for a done/idle window, whose stamp a
# Stop hook for the very same turn may have written in the same second. A
# missing or mismatched stamp is "don't know" (no mark), as in cx_after.
cx_recent() {
    local e iso
    [ -n "$cx_ts" ] && [ "${since#* }" = "$state" ] || return 1
    e="${since%% *}"
    case "$e" in ''|*[!0-9]*) return 1 ;; esac
    TZ=UTC0 printf -v iso '%(%Y-%m-%dT%H:%M:%S)T' "$((e - 5))" 2>/dev/null || return 1
    [[ ! "$cx_ts" < "$iso" ]]
}

# limit_end_cmd WIN: into lim_cmd the tmux commands (" ; ..."-joined, ids and
# fixed words only) a watcher-written turn end adds to its own write: unset
# @agent_limit / @agent_limit_lifted when the tick read a limit, and
# @agent_paused while it still holds the limit's own E - then W_PAUSED is
# cleared, so this turn end is not ruled `paused` (the indicator's
# limit_end, for the watcher's un-stick, bg release and codex turn ends).
limit_end_cmd() {
    local w="$1" e=""
    lim_cmd=""
    [ -n "${W_LIMIT[$w]:-}" ] || return 0
    lim_cmd=" ; set-option -uw -t $w @agent_limit ; set-option -uw -t $w @agent_limit_lifted"
    read -r _ _ e _ <<<"${W_LIMIT[$w]}" || true
    if [ -n "${W_PAUSED[$w]:-}" ] && [[ $e =~ ^[0-9]{1,12}$ ]] && [ "${W_PAUSED[$w]}" = "$e" ]; then
        lim_cmd+=" ; set-option -uw -t $w @agent_paused"
        W_PAUSED[$w]=""
    fi
    return 0
}

# limit_spawn ARGS...: agent-pause.py ARGS, detached (double fork: never in
# this daemon's job table), fire-and-forget; a missing script is no error.
limit_spawn() {
    [ -x "$LIMIT_PY" ] || return 0
    ( "$LIMIT_PY" "$@" </dev/null >/dev/null 2>&1 & ) 2>/dev/null
    return 0
}

# codex_limit_mark WIN WAS ROLLOUT: mark a codex window rate-limited (the
# indicator's limit_mark, same list and order) from its rollout's last
# task_complete and token_count, over the state WAS the tick decided from
# (CONDITIONAL WRITES). Sets the caller's state etc. through cas_write.
# Returns 1 when nothing was written, 2 when the rollout did not parse as a
# usage limit after all (the caller then ends the turn as a plain complete).
codex_limit_mark() {
    local win="$1" was="${2//[^a-z-]/}" rp="$3" r tid e c o
    codex_limit_parse "$rp" "$tick_now"
    [ "$CL_HIT" = 1 ] || return 2
    r="$CL_R"; tid="$CL_TID"
    limit_text "$r" codex "$tick_now"
    e="$tick_now"
    c=""
    for o in @agent_pending @agent_held_detail @agent_quiet @agent_limit_lifted @agent_ask_from @agent_ask_bg \
             @agent_prior_state @agent_prior_since @agent_prior_detail_kind @agent_prior_detail; do
        c+="set-option -uw -t $win $o ; "
    done
    c+="set-option -w -t $win @agent_paused $e ; set-option -w -t $win @agent_limit '$r codex $e'"
    c+=" ; set-option -w -t $win @agent_detail_kind limit ; set-option -w -t $win @agent_detail '$LIM_TXT'"
    c+=" ; set-option -w -t $win @agent_turn_result '$tid limited $r'"
    c+=" ; set-option -w -t $win @agent_since '$e paused' ; set-option -w -t $win @agent_state paused"
    cas_write "$win" "#{==:#{@agent_state},$was}" "$c" || return 1
    [ "$state" = paused ] || return 1
    limit_spawn limit-add "$win"
    return 0
}

# limit_trigger: the RESUME TRIGGER (item 2 above), once per tick after the
# window loop. Builtins only unless it fires.
limit_trigger() {
    local why=""
    [ "$lim_prev_tick" -gt 0 ] && [ $((tick_now - lim_prev_tick)) -gt "$LIMIT_WAKE_GAP" ] && lim_wake=1
    lim_prev_tick="$tick_now"
    [[ $lim_due =~ ^[0-9]{1,12}$ ]] || { lim_wake=0; return 0; }
    [ $((tick_now - lim_last_spawn)) -ge "$LIMIT_TICK_EVERY" ] || return 0
    if [ "$((10#$lim_due))" -le "$tick_now" ]; then
        why=due
    elif [ "$lim_wake" = 1 ]; then
        why=wake
    elif [ -e "$LIMIT_SEQ" ] && { [ ! -e "$LIMIT_SEQ_MARK" ] || [[ $LIMIT_SEQ -nt $LIMIT_SEQ_MARK ]]; }; then
        why=swap
    fi
    [ -n "$why" ] || return 0
    lim_last_spawn="$tick_now"
    lim_wake=0
    [ -e "$LIMIT_SEQ" ] && : > "$LIMIT_SEQ_MARK" 2>/dev/null
    limit_spawn limit-tick
    return 0
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
    resolve_session_bases() { return 1; }   # subagent_running's gate
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
# The interpreter half: the live pid set straight from the file. Only ever
# called through refresh_cua_pids' gates below. Exit status is part of the
# answer: 0 = the set (possibly empty, " "), non-zero = NO answer (file
# unreadable/unparseable, or the interpreter itself failed - EAGAIN, jetsam
# under load), which the caller treats as empty for this tick only and never
# caches as "nobody" (see refresh_cua_pids).
live_cua_pids() {
    /usr/bin/python3 - "$ACTIVITY" "$CUA_LIVE" <<'PY' 2>/dev/null
import json, sys, time
try:
    with open(sys.argv[1]) as f:
        sessions = json.load(f).get("sessions", {}) or {}
except Exception:
    raise SystemExit(1)
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

# CHANGE STAMPS: "has this file changed since tick T?" without a fork.
# Bash cannot read an mtime without forking `stat` (~2-8 ms a call under
# this machine's load, and the session checks paid one per claude session
# per tick), but `[ a -nt b ]` compares two mtimes as a builtin. So every
# tick begins by writing an empty stamp file (builtin redirect), and "F
# changed since tick T began" is "F is NOT older than T's stamp". A ring of
# STAMP_RING files bounds the set; a stamp is valid for STAMP_RING - 1 ticks
# after its own, after which callers must recompute from scratch.
#
# Every doubt reads as CHANGED, i.e. as "do the full check, exactly as
# before": a missing stamp (unwritable TMPDIR, swept by a successor), an
# expired one, or a file whose mtime TIES the stamp (`! ref -nt f`, so any
# granularity - bash 5.1+ compares nanoseconds, older bash whole seconds -
# can only over-report). A file that does not exist is skipped: its parent
# directory's mtime is what records it appearing or disappearing.
#
# This assumes IN-PLACE writes (appends, truncate+write), which is how the
# transcripts the subagent gate watches are written. A file REPLACED by
# rename keeps its temp file's mtime, set at close - which can fall just
# before a stamp while the rename lands just after the look; activity.json
# is written exactly that way, so refresh_cua_pids adds a directory check.
STAMP_RING=10
tick_no=0
# changed_since T FILE...: true (0) if any existing FILE changed since tick
# T's stamp, or the stamp can't vouch for it.
changed_since() {
    local t="$1" ref f
    shift
    [ -n "$t" ] && [ $((tick_no - t)) -lt "$STAMP_RING" ] || return 0
    ref="$STAMPS.$((t % STAMP_RING))"
    [ -e "$ref" ] || return 0
    for f; do
        [ -e "$f" ] || continue
        [ "$ref" -nt "$f" ] || return 0
    done
    return 1
}

# Computer-use pids for this tick, into cua_pids (" 123 456 ", or a single
# space when none). Called once per tick, no comsub (a $(...) is a fork).
#
# Two gates before the interpreter (~22 ms at rest, far more under load):
#  1. The shim rewrites activity.json on every driver call, so every session
#     ts is <= its mtime: a file untouched for CUA_LIVE seconds cannot hold a
#     live session. This used to `stat` the file every tick; now it is only
#     stat'ed when no stamp can vouch (first tick, after a gap) or when the
#     file or its directory moved (see RENAMES), and cua_seen is advanced to
#     the first tick that sees a change - never earlier than the real mtime,
#     so the gate can only stay open a tick longer than the stat version,
#     never close early.
#  2. A live set only SHRINKS while the file is untouched (entries age out),
#     so once python has CLEANLY answered "nobody" for the current contents
#     (exit 0, " "), the answer stays "nobody" until the file changes again -
#     which spares the python run every second of the CUA_LIVE tail after
#     each drive. A failed run is never cached: it is " " for this tick and
#     python is asked again next tick, so a transient interpreter failure
#     costs one tick of glyph, not the rest of the 60 s window.
#
# RENAMES. Every writer of activity.json (cua-mcp-shim, cua_state_watch,
# arc-cdp-client) replaces it by rename, and the new file carries its temp
# file's mtime - set at close, which can land just before a tick's stamp
# while the rename lands just after that tick looked: the -nt test alone
# would then call the new file "unchanged" for good. A rename always bumps
# the DIRECTORY's mtime, so the file OR its directory moving gets one exact
# look: stat the file's inode and ns mtime and compare with the last ones
# seen (a rename is a new inode; an in-place write a new mtime). Not the
# directory alone as the change signal: CuaNotch renames agents.json and
# friends into it every 1-6 s (measured 2026-10-07, activity.json idle for
# 40 s meanwhile), which would hold the gate open and run python every
# tick. The stat is a fork, but only on a tick where something there moved,
# and python still runs only when activity.json itself is new.
cua_seen=0       # epoch at/after activity.json's last change
cua_tick=""      # tick that last looked at it
cua_none=0       # 1 = python cleanly said "nobody" and the file hasn't changed since
cua_id=""        # "<inode>.<ns mtime>" from the last exact look ("" = unknown)
refresh_cua_pids() {
    cua_pids=" "
    [ -f "$ACTIVITY" ] || return 0
    local st_out amt id
    if [ -n "$cua_tick" ] && [ $((tick_no - cua_tick)) -lt "$STAMP_RING" ] \
       && [ -e "$STAMPS.$((cua_tick % STAMP_RING))" ]; then
        if changed_since "$cua_tick" "$ACTIVITY" "${ACTIVITY%/*}"; then
            st_out=$(stat -f '%m %i.%Fm' "$ACTIVITY" 2>/dev/null)
            id="${st_out#* }"
            if [ -z "$st_out" ] || [ "$id" != "$cua_id" ]; then
                cua_seen=$tick_now; cua_none=0; cua_id="$id"
            fi
        fi
    else
        st_out=$(stat -f '%m %i.%Fm' "$ACTIVITY" 2>/dev/null)
        amt="${st_out%% *}"
        case "$amt" in ''|*[!0-9]*) amt=0 ;; esac   # digits only before $(( ))
        cua_seen=$amt; cua_none=0; cua_id="${st_out#* }"
    fi
    cua_tick=$tick_no
    [ $((tick_now - cua_seen)) -lt "$CUA_LIVE" ] || return 0
    [ "$cua_none" = 1 ] && return 0
    if ! cua_pids=$(live_cua_pids); then
        cua_pids=" "                       # no answer: empty now, ask again next tick
    elif [ "$cua_pids" = " " ]; then
        cua_none=1                         # a clean "nobody": holds until the file changes
    fi
    [ -n "$cua_pids" ] || cua_pids=" "
    return 0
}

# session_has_running_subagent, skipped when nothing it reads has changed.
# Its verdict is a function of the session's parent transcript, its
# subagents/ dir and every agent-*.jsonl in it (mtime, size, contents) -
# plus the clock, which can only move a subagent OUT of the age window
# (running -> finished), never into it. So an unchanged input set keeps its
# verdict, and the STAMP_RING-tick expiry bounds how late an age-out is seen
# (seconds, on a one-hour backstop). Keyed by pid AND the resolved session
# path, so /clear or /resume (a new sessionId behind the same pid) is a
# fresh look. The rules themselves stay in agent-session-lib.sh; this only
# decides whether to ask. One stat fork per session per tick (~6 ms each
# here at load 200, 23 ms for a 312-transcript session) becomes a builtin
# -nt walk (~2.5 ms for those 312) while nothing moves.
declare -A SUB_KEY=() SUB_TICK=() SUB_V=()
subagent_running() {
    local pid="$1" key b v
    local -a watch=()
    resolve_session_bases "$pid" || return 1
    key="$SESSION_PROJ/${SESSION_BASES[*]}"
    if [ -n "${SUB_TICK[$pid]:-}" ] && [ "${SUB_KEY[$pid]:-}" = "$key" ]; then
        for b in "${SESSION_BASES[@]}"; do
            watch+=("$SESSION_PROJ/$b.jsonl" "$SESSION_PROJ/$b/subagents" \
                    "$SESSION_PROJ/$b/subagents"/agent-*.jsonl)
        done
        if ! changed_since "${SUB_TICK[$pid]}" "${watch[@]}"; then
            return "${SUB_V[$pid]}"
        fi
    fi
    session_has_running_subagent "$pid"
    v=$?
    SUB_KEY[$pid]="$key"; SUB_TICK[$pid]=$tick_no; SUB_V[$pid]=$v
    return "$v"
}

# BACKGROUND TASKS HOLD A TAB RUNNING (Mack, 2026-10-14: "an agent ending
# generation ... but still waiting on something, e.g. a background test/build
# run ... treated the same as ... still having subagents"). A turn that ends
# with a background task it launched still out (a pty/run moved to the
# background, a Bash run_in_background) has not finished. The Stop hook
# already asks agent-bg-pending and leaves such a tab running, but Claude's
# own status then reads "shell" (a pending SUBAGENT keeps it "busy", which is
# why those always held), and the un-stick below idled the tab 3 s later.
# So a running claude window whose status reads idle/shell asks the same
# program - one algorithm for both surfaces, CuaNotch's - and a yes holds the
# tab running and lights the gear, as a subagent does. Only on that path, and
# cached: asked again when the parent transcript moves (the task's
# notification lands there) or once STAMP_RING ticks pass (its age backstop).
BG_PENDING="$HOME/.local/bin/agent-bg-pending"
declare -A BG_KEY=() BG_TICK=() BG_V=()
bg_task_pending() {
    local pid="$1" tp raw cwd="" v
    [ -x "$BG_PENDING" ] || return 1
    resolve_session_bases "$pid" || return 1
    tp="$SESSION_PROJ/${SESSION_BASES[0]}.jsonl"
    if [ -n "${BG_TICK[$pid]:-}" ] && [ "${BG_KEY[$pid]:-}" = "$tp" ] \
       && ! changed_since "${BG_TICK[$pid]}" "$tp"; then
        return "${BG_V[$pid]}"
    fi
    raw="$(<"$HOME/.claude/sessions/$pid.json")"
    [[ $raw =~ \"cwd\":\"([^\"]+)\" ]] && cwd="${BASH_REMATCH[1]}"
    "$BG_PENDING" "$tp" "${SESSION_BASES[0]}" "$cwd" >/dev/null 2>&1
    v=$?
    [ "$v" = 0 ] || v=1
    BG_KEY[$pid]="$tp"; BG_TICK[$pid]=$tick_no; BG_V[$pid]=$v
    return "$v"
}

# STALE-SAVE CHIP. tmux-continuum once stopped autosaving for ~38 h without a
# sound (a stray `tmux -L` test server tripped its another-server check; see
# continuum-ensure.sh). So: when the newest snapshot - the mtime of the
# `last` target under @resurrect-dir, which resurrect-save.sh refreshes even
# on an unchanged save - is older than @resurrect-stale-minutes (default 60,
# four continuum intervals; 0 = off), the global @resurrect_stale holds its
# age ("75m", "3h", "2d") and stash-statusline.sh renders a red chip from it.
# Unset otherwise. resurrect-save.sh clears it the moment a save lands; this
# re-evaluates at most every STALE_EVERY seconds. It is DEFAULT-SERVER-ONLY by
# construction: a watcher on any other server exits at the top.
#
# Fork-free while healthy. The options are read ONCE (the first look, a full
# period after startup so tmux.conf has finished loading; `prefix r` respawns
# this daemon, so a changed option is picked up). After that, stale_lb is a
# LOWER bound on last's mtime: each look first asks the builtin `[ last -nt
# STAMP ]` against a stamp written at the previous look (epoch stale_x), and a
# yes moves the bound up to stale_x - a save every 15 min keeps the bound
# within one period of the truth with no fork at all. Only when the bound
# alone would say "stale" is there an exact `stat` (one fork a minute, and
# only while actually stale or on the first look). The stamp is written BEFORE
# that stat, so a save racing the two is seen by one or the other; every doubt
# (missing stamp, a tie) falls through to the stat, never to a false chip.
STALE_EVERY="${AGENT_TAB_WATCHER_STALE_EVERY:-60}"
STALE_STAMP="$STAMPS.save"     # under $STAMPS.*: cleanup() and the startup sweep take it
stale_due=""     # epoch of the next look ("" = not scheduled yet)
stale_opts=0     # 1 = options read
stale_dir=""     # resolved @resurrect-dir
stale_secs=3600  # threshold, seconds (0 = off)
stale_lb=0       # lower bound on last's mtime, epoch (0 = unknown)
stale_x=""       # epoch the stamp was written at ("" = no stamp)
stale_val="?"    # what @resurrect_stale holds as far as we know ("?" = unknown)
check_stale_save() {
    [ -n "$stale_due" ] || stale_due=$((tick_now + STALE_EVERY))
    # BACKWARD CLOCK JUMPS (NTP step, a manual set). A due time more than one
    # period ahead can only come from the old clock, and waiting for the new
    # one to catch up would mute the chip for the size of the jump - so look
    # now. Likewise a bound or stamp from the future: with stale_lb ahead of
    # the clock every age reads negative ("fresh") until the clock passes it,
    # and a save stamped by the new clock is never -nt a future stamp. Both
    # are dropped, so this look takes the exact stat and re-learns from it.
    # (Not exercised by the tests: tick_now is the builtin %(%s)T clock, which
    # the fake harness cannot move. A forward jump needs nothing: ages only
    # grow, and every "stale" verdict is confirmed by a stat first.)
    [ "$tick_now" -ge "$stale_due" ] || [ $((stale_due - tick_now)) -gt "$STALE_EVERY" ] || return 0
    stale_due=$((tick_now + STALE_EVERY))
    if [ "$stale_lb" -gt "$tick_now" ] || { [ -n "$stale_x" ] && [ "$stale_x" -gt "$tick_now" ]; }; then
        stale_lb=0; stale_x=""
    fi
    local opts mins last label="" mt age
    if [ "$stale_opts" = 0 ]; then
        opts=$(tmux display-message -p "#{@resurrect-dir}${US}#{@resurrect-stale-minutes}" 2>/dev/null) || return 0
        IFS="$US" read -r stale_dir mins <<<"$opts"
        stale_dir="${stale_dir:-$HOME/.tmux/resurrect}"
        stale_dir="${stale_dir//\$HOME/$HOME}"
        stale_dir="${stale_dir/#\~/$HOME}"
        case "$mins" in ''|*[!0-9]*) mins=60 ;; esac
        stale_secs=$((10#$mins * 60))
        stale_opts=1
    fi
    last="$stale_dir/last"
    # Off, or nothing to measure (never saved, or `last` dangles): no chip.
    if [ "$stale_secs" -gt 0 ] && [ -e "$last" ]; then
        if [ -n "$stale_x" ] && [ -e "$STALE_STAMP" ] && [ "$last" -nt "$STALE_STAMP" ]; then
            [ "$stale_x" -gt "$stale_lb" ] && stale_lb=$stale_x
            : > "$STALE_STAMP" 2>/dev/null && stale_x=$tick_now || stale_x=""
        fi
        age=$((tick_now - stale_lb))
        if [ "$age" -ge "$stale_secs" ]; then
            : > "$STALE_STAMP" 2>/dev/null && stale_x=$tick_now || stale_x=""
            mt=$(stat -L -f %m "$last" 2>/dev/null)
            case "$mt" in
                ''|*[!0-9]*) age=0 ;;                 # vanished mid-look: no verdict
                *) stale_lb=$mt; age=$((tick_now - mt)) ;;
            esac
        fi
        if [ "$age" -ge "$stale_secs" ]; then
            if [ "$age" -lt 7200 ]; then label="$((age / 60))m"
            elif [ "$age" -lt 172800 ]; then label="$((age / 3600))h"
            else label="$((age / 86400))d"; fi
        fi
    fi
    [ "$label" = "$stale_val" ] && return 0
    if [ -n "$label" ]; then
        tmux set-option -g @resurrect_stale "$label" 2>/dev/null || return 0
    else
        tmux set-option -gu @resurrect_stale 2>/dev/null || return 0
    fi
    stale_val="$label"
    changed=1
}

# A failed tmux command is NOT proof the server died — it can also be a
# transient hiccup (server mid-reload, EINTR, fd pressure). Exiting on the
# first one is how the daemon silently disappears after days of uptime, taking
# the blink, the workflow gear and the state GC with it and leaving no trace.
# Tolerate a short streak, and only quit once the server is confirmed gone.
FAIL_LIMIT=5
fail_streak=0
server_gone() { ! tmux list-sessions >/dev/null 2>&1; }

# Consecutive-idle counters for the stuck-`running` reconcile (and the codex
# turn-end reconcile beside it), carried across ticks as " @win=N @win=N "
# (see the hysteresis note in the loop).
idle_streak=" "

# Is a window with this #{window_active_clients} ($1) in front of the user:
# active for a client AND WezTerm frontmost? The seen-it discharge's test, and
# the indicator's viewing_now. The frontmost lookup forks twice, so it runs at
# most once a tick (wez_front, reset per tick) and only when a watched window
# asks.
viewed_now() {
    case "$1" in ''|0|*[!0-9]*) return 1 ;; esac
    if [ -z "$wez_front" ]; then
        wez_front=no
        [ "$(lsappinfo info -only bundleid "$(lsappinfo front 2>/dev/null)" 2>/dev/null)" = \
          '"CFBundleIdentifier"="com.github.wez.wezterm"' ] && wez_front=yes
    fi
    [ "$wez_front" = yes ]
}

# CONDITIONAL WRITES (2026-10-22). Every state this loop writes is decided
# from the tick's ONE READ, and that read can be seconds old when the write
# lands: refresh_cua_pids, the windows before this one, this window's own
# viewed_now forks. A hook that moved the window in between was overwritten
# with no trace - Mack Cmd-Tabs back to a `done` tab as a prompt (typed, a
# message_agent, a task wake-up) sets `running`, and the seen-it discharge
# wrote idle over it with no @agent_pending, which the heartbeat never
# re-arms: the whole turn read idle, no pulse, its slot released. So each
# write is guarded IN tmux by the value it was decided from (if-shell -F runs
# no shell), and the same call reads the window back. The paused turn end is
# guarded on its state like the rest; its resume-flag re-check is a second
# if-shell INSIDE that guarded list, not a guard of its own.
# cas_write WIN COND CMDS: CMDS (a tmux command string, parsed by tmux: window
# ids, epochs and fixed words only - never user text, which goes through
# set-option -F copies) runs only while the format COND is true. Sets state,
# pending, since and W_PRIOR[WIN] to what the window holds AFTER the call,
# whichever writer won. Returns 1 only when tmux failed (nothing is known).
cas_write() {
    local out p
    out=$(tmux if-shell -F -t "$1" "$2" "$3" \; \
          display-message -p -t "$1" "x${US}#{@agent_state}${US}#{@agent_pending}${US}#{@agent_since}${US}#{@agent_prior_state}" 2>/dev/null) || return 1
    case "$out" in "x$US"*) ;; *) return 1 ;; esac
    IFS="$US" read -r _ state pending since p <<<"$out" || true
    W_PRIOR[$1]="$p"
    return 0
}

# Consecutive agent-LESS ticks per window, same " @win=N " shape, for the GC
# below. A window is only garbage-collected after GC_TICKS ticks in a row
# without an agent pane. One tick is not enough: tmux-thumbs (prefix+Space)
# `swap-pane`s the active pane into a throwaway "[thumbs]" window for the
# seconds the picker is up, and a single-tick GC read that as "agent gone",
# wiped @agent_summary/@agent_state, and the tab label stayed blank until the
# next hook refreshed it. A real exit is still collected, just GC_TICKS later.
GC_TICKS=5
gc_streak=" "

# BIRTH GRACE. A claude tab is marked an agent the instant it is created
# (agent-birth.conf: @agent_state idle + @agent_born "<epoch>", in the
# new-window's own command list), seconds before claude itself is running in
# it - zsh has to start first. Without this the GC above would collect that
# window as an agent that died, and the tab would flicker shell -> agent. So a
# window holding a FRESH birth mark (an epoch less than BIRTH_GRACE seconds
# from now) and no agent pane yet is left alone: no GC streak at all. The first
# tick that sees its agent drops the mark (from then on it is an ordinary
# agent window), and a mark past the grace - claude never started, or exited
# before any tick saw it - is just another leftover: the GC streak starts and
# collects it with everything else, mark included. So a birth that never
# becomes an agent is a shell again after BIRTH_GRACE + GC_TICKS seconds.
BIRTH_GRACE=30
# birth_fresh EPOCH: digits only, within BIRTH_GRACE of tick_now either way (a
# mark from the future - a clock step - is not trusted beyond the grace).
birth_fresh() {
    [[ $1 =~ ^[0-9]{1,18}$ ]] || return 1
    local age=$((tick_now - 10#$1))
    [ "$age" -lt "$BIRTH_GRACE" ] && [ "$age" -gt "-$BIRTH_GRACE" ]
}

# Process discovery (see PROCESS DISCOVERY in the loop): the pgrep pattern
# (is_agent_comm's names, -x anchors it), the per-pid tty cache, and how
# often that cache is thrown away wholesale.
#
# ASSOCIATIVE ARRAYS for every per-tick lookup (bash 4+, which the lib's
# caches already require). These used to be space-joined strings probed
# with `case "$s" in *" ${key} "*`, a linear scan per probe; with ~165
# windows (most of them pty-MCP shells in `agents`) and four such probes
# per window that was quadratic, ~20 ms a tick at load 200.
AGENT_PAT='claude|codex|opencode|[0-9]+\.[0-9]+\.[0-9]+'
declare -A PID_TTY=() PID_NEXT=() TTY_PID=()   # pid->tty|-, its rebuild, tty->lowest pid
# pid -> codex|claude|opencode, for agent pids only: written by the same single-pid ps
# that fills PID_TTY (no extra fork), dropped with it. A pid that leaves the
# pgrep set and comes back is ps'd again, so this is overwritten with it.
declare -A PID_KIND=()
pid_cache_age=0
PID_CACHE_TICKS=60
# Per-window fields of the tick's one tmux read (see the ONE READ note).
declare -A W_SEEN=() W_PID=() W_STATE=() W_WAC=() W_SINCE=() W_WF=() W_CUA=() W_ROLL=() W_SUM=() W_DET=()
declare -A W_DK=() W_DTXT=() W_SESS=() W_IDX=() W_NAME=()   # event-log fields
declare -A W_AK=()   # @agent_kind alone (W_DET is kind+detail kind, for the GC)
declare -A W_PEND=() # @agent_pending (agent slots: idle + pending is active)
declare -A W_BORN=() # @agent_born (BIRTH GRACE)
declare -A W_PRIOR=() # @agent_prior_state (AGENT-ORIGINATED TURNS)
declare -A W_QUIET=() # @agent_quiet: a quiet restore's one-shot log mark
declare -A W_PAUSED=() # @agent_paused: agent-pause.py's flag (docs/agent-pause.md)
declare -A W_ASKBG=()  # @agent_ask_bg: the indicator's ASKS WHILE WORK IS OUT raise
declare -A W_LIMIT=()  # @agent_limit: RATE-LIMITED AGENTS
wins=()

# AGENT-ORIGINATED TURNS (agent-tab-indicator.sh, same heading; the doc's
# section of that name). A turn another agent started finishes QUIET: the
# window goes back to the attention state it had before that turn, saved by
# the indicator in @agent_prior_state/_since/_detail_kind/_detail, which are
# set ONLY while such a turn's finish is pending. The indicator's Stop does
# the restore itself, except when this watcher is the one ending the turn:
#   - a Stop held `running` by background work (BACKGROUND TASKS HOLD A TAB
#     RUNNING), released here when the work drains;
#   - a stuck `running` (Esc, a missed Stop) un-stuck from the session file /
#     OpenCode status / rollout;
#   - the CODEX TURN ENDS reconcile (task_complete would paint done).
# Each of those writes prior_restore's state instead of idle/done when the
# window holds a prior. Seen-it rules apply to what comes back, as the Stop
# path's prior_restore applies them (one of two places - change both): a
# done/needs-input restored while viewed lands as idle, stamped seen; red is
# never discharged by being watched; the saved stamp comes back when it still
# describes the restored state.
ORIGIN_OPTS=(@agent_turn_origin @agent_turn_result @agent_prior_state @agent_prior_since
             @agent_prior_detail_kind @agent_prior_detail @agent_inbound @agent_inbound_seen
             @agent_for_user @agent_awaiting @agent_quiet @agent_bg_owner @agent_paused
             @agent_turn_start @agent_limit @agent_limit_lifted)
# (@agent_limit / @agent_limit_lifted: RATE-LIMITED AGENTS, dropped with the
# session wherever @agent_paused is.)
# prior_restore WIN WAC: one read of the saved fields, one write. Sets
# r_state (the state written) and r_since; returns 1 when nothing was
# written: the write failed, or the prior is gone since the tick's read (the
# indicator's quiet Stop restored it in between - the window is already
# right, and writing idle over it would undo that).
# The write also sets @agent_quiet "<from>", the one-shot mark that makes
# the next tick's event-log line read "↩ replied to @from" (see log_event).
#   The write is a cas_write (CONDITIONAL WRITES): it lands only while the
# window still holds the tick's state STATE and a prior. The detail is
# user text, so it is copied tmux-side (set-option -F), never spliced into
# the command string; it goes before the unsets it reads from. Whatever
# the window holds after the call - written here, or by the hook that won -
# comes back in r_state/r_since (and the caller's state/pending/since).
prior_restore() {
    local win="$1" wac="$2" was="${3//[^a-z-]/}" out x ps psince pdk pdt origin ofrom seen=0 c
    out=$(tmux display-message -p -t "$win" "x${US}#{@agent_prior_state}${US}#{@agent_prior_since}${US}#{@agent_prior_detail_kind}${US}#{@agent_turn_origin}${US}#{@agent_prior_detail}" 2>/dev/null || true)
    ps=""; psince=""; pdk=""; pdt=""; origin=""
    case "$out" in "x$US"*) IFS="$US" read -r x ps psince pdk origin pdt <<<"$out" || true ;; esac
    [ -n "$ps" ] || return 1
    read -r _ _ ofrom _ <<<"$origin" || true
    ofrom="${ofrom//[^A-Za-z0-9_.-]/}"
    case "$ps" in idle|done|needs-input|failed) ;; *) ps=idle ;; esac
    case "$ps" in done|needs-input) viewed_now "$wac" && { ps=idle; seen=1; } ;; esac
    r_since="$tick_now $ps"
    if [[ $psince =~ ^[0-9]{1,18}\ (.+)$ ]] && [ "${BASH_REMATCH[1]}" = "$ps" ]; then
        r_since="$psince"
    fi
    if [ -n "$pdk" ]; then
        c="set-option -w -t $win -F @agent_detail_kind '#{@agent_prior_detail_kind}' ; set-option -w -t $win -F @agent_detail '#{@agent_prior_detail}'"
    else
        c="set-option -uw -t $win @agent_detail_kind ; set-option -uw -t $win @agent_detail"
    fi
    c+=" ; set-option -w -t $win @agent_state $ps ; set-option -w -t $win @agent_since '$r_since'"
    c+=" ; set-option -uw -t $win @agent_pending ; set-option -uw -t $win @agent_prior_state"
    c+=" ; set-option -uw -t $win @agent_prior_since ; set-option -uw -t $win @agent_prior_detail_kind"
    c+=" ; set-option -uw -t $win @agent_prior_detail ; set-option -uw -t $win @agent_bg_owner"
    c+=" ; set-option -uw -t $win @agent_ask_bg"   # it only ever describes a needs-input
    [ "$seen" = 1 ] && c+=" ; set-option -w -t $win @agent_seen_at $tick_now"
    c+=" ; set-option -w -t $win @agent_quiet ${ofrom:--}"
    c+="${lim_cmd:-}"   # RATE-LIMITED AGENTS: a turn end ends the limit (limit_end_cmd)
    cas_write "$win" "#{&&:#{==:#{@agent_state},$was},#{!=:#{@agent_prior_state},}}" "$c" || return 1
    # Still a prior: the guard was false (a hook moved the state), nothing
    # written. Gone: restored - here, or by the indicator's quiet Stop.
    [ -z "${W_PRIOR[$win]:-}" ] || return 1
    r_state="$state"; r_since="$since"
}

# AGENT SLOTS (see the header). In the tick, the fold fills S_SLOT/S_VIS
# from the list-panes rows and the per-window loop S_ELIG (from the state
# and flags as the tick LEAVES them); reconcile_slots then says whether
# anything needs writing at all.
# >>> SLOT CORE >>> (byte-identical in agent-tab-watcher.sh and
# agent-tab-indicator.sh; tests/test_agent_slots.py fails if they drift)
# AGENT SLOTS: the sticky numbers agent-ui shows (docs/agent-tab-indicator.md,
# "Agent slots"). A window holds @agent_slot while it is ACTIVE and VISIBLE:
#   active  = @agent_state is running, failed, needs-input or done; OR
#             @agent_state is idle AND @agent_pending is an epoch no older
#             than 600 s (SLOT_PENDING_TTL: an answered prompt whose turn is
#             about to resume - bounded, because a "No"/Esc answer fires no
#             resume hook and the stamp would otherwise hold the number
#             forever; a junk or empty stamp never counts); OR
#             @agent_workflow is set; OR @agent_cua is set.
#   visible = linked into at least one session outside SLOT_EXCLUDE (which is
#             agent-jump.sh's EXCLUDE byte for byte).
# Numbers are taken lowest first and never move while held. Bash 4+.
SLOT_EXCLUDE=" agents tasks stash scratch btop-popup "
SLOT_US=$'\x1f'
# One `list-windows -a` row per window link, for slot_fold.
SLOT_FMT="#{window_id}${SLOT_US}#{session_name}${SLOT_US}#{window_index}${SLOT_US}#{@agent_slot}${SLOT_US}#{@agent_state}${SLOT_US}#{@agent_pending}${SLOT_US}#{@agent_workflow}${SLOT_US}#{@agent_cua}"
# S_SLOT = @agent_slot as read (set only), S_VIS = "session US index" of the
# byte-smallest visible link (set = visible), S_ELIG = active.
declare -A S_SLOT=() S_VIS=() S_ELIG=()
slot_cmd=()
slot_n=""
SLOT_PENDING_TTL=600
# slot_pending_fresh STAMP: an epoch (digits only, at most 18 of them) no
# older than SLOT_PENDING_TTL. Builtin clock, no fork.
slot_pending_fresh() {
    local now
    [[ $1 =~ ^[0-9]{1,18}$ ]] || return 1
    printf -v now '%(%s)T' -1
    [ $((now - 10#$1)) -le "$SLOT_PENDING_TTL" ]
}
# slot_active STATE PENDING WORKFLOW CUA
slot_active() {
    case "$1" in
        running|failed|needs-input|done) return 0 ;;
        idle) slot_pending_fresh "$2" && return 0 ;;
    esac
    [ -n "$3$4" ]
}
# A valid slot: canonical decimal 1..999999999 (no sign, no leading zero).
slot_valid() { [[ $1 =~ ^[1-9][0-9]{0,8}$ ]]; }
# slot_vis WIN SESSION INDEX: one link of WIN. Keeps the byte-order smallest
# visible session (`[ \< ]` is strcmp whatever the locale; `[[ < ]]` would
# collate) and the lower index on a tie.
slot_vis() {
    local v
    case "$SLOT_EXCLUDE" in *" $2 "*) return 0 ;; esac
    v="${S_VIS[$1]:-}"
    if [ -z "$v" ] || [ "$2" \< "${v%%"$SLOT_US"*}" ] \
       || { [ "$2" = "${v%%"$SLOT_US"*}" ] && [ "$3" -lt "${v#*"$SLOT_US"}" ] 2>/dev/null; }; then
        S_VIS[$1]="$2$SLOT_US$3"
    fi
}
# slot_fold ROWS: a `list-windows -a -F "$SLOT_FMT"` read into S_*.
slot_fold() {
    local w sess idx slot st pend wf cua
    S_SLOT=(); S_VIS=(); S_ELIG=()
    while IFS="$SLOT_US" read -r w sess idx slot st pend wf cua; do
        case "$w" in @*) ;; *) continue ;; esac
        [ -n "$slot" ] && S_SLOT[$w]="$slot"
        slot_active "$st" "$pend" "$wf" "$cua" && S_ELIG[$w]=1
        slot_vis "$w" "$sess" "$idx"
    done <<<"$1"
}
# slot_after A B: does A fill after B? Session (bytes), index, window id.
slot_after() {
    local a="${S_VIS[$1]}" b="${S_VIS[$2]}" sa sb ia ib
    sa="${a%%"$SLOT_US"*}"; ia="${a#*"$SLOT_US"}"; sb="${b%%"$SLOT_US"*}"; ib="${b#*"$SLOT_US"}"
    [ "$sa" != "$sb" ] && { [ "$sa" \> "$sb" ]; return; }
    case "$ia$ib" in
        ''|*[!0-9]*) [ "$ia" != "$ib" ] && { [ "$ia" \> "$ib" ]; return; } ;;
        *) [ "$ia" != "$ib" ] && { [ "$ia" -gt "$ib" ]; return; } ;;
    esac
    [ "${1#@}" -gt "${2#@}" ] 2>/dev/null
}
# reconcile_slots: into slot_cmd, the tmux command list (empty = nothing to
# do) that makes every @agent_slot in S_* follow the rules: release a slot on
# a window that is not active+visible; keep a valid one no other claims (on a
# duplicate the lower window id, numerically, keeps it); give every other
# active+visible window the lowest number nobody holds, in slot_after order.
reconcile_slots() {
    local w v keep i j
    local -a need=()
    local -A held=()
    slot_cmd=()
    for w in "${!S_SLOT[@]}"; do
        [ -n "${S_ELIG[$w]:-}" ] && [ -n "${S_VIS[$w]:-}" ] && continue
        slot_cmd+=(${slot_cmd[0]+\;} set-option -uw -t "$w" @agent_slot)
    done
    for w in "${!S_ELIG[@]}"; do
        [ -n "${S_VIS[$w]:-}" ] || continue
        v="${S_SLOT[$w]:-}"
        if slot_valid "$v"; then
            keep="${held[$v]:-}"
            if [ -z "$keep" ]; then
                held[$v]=$w
            elif [ "${w#@}" -lt "${keep#@}" ] 2>/dev/null; then
                held[$v]=$w; need+=("$keep")
            else
                need+=("$w")
            fi
        else
            need+=("$w")
        fi
    done
    [ "${#need[@]}" -gt 0 ] || return 0
    for ((i = 1; i < ${#need[@]}; i++)); do
        w="${need[i]}"; j=$((i - 1))
        while [ "$j" -ge 0 ] && slot_after "${need[j]}" "$w"; do
            need[j+1]="${need[j]}"; j=$((j - 1))
        done
        need[j+1]="$w"
    done
    v=1
    for w in "${need[@]}"; do
        while [ -n "${held[$v]:-}" ]; do v=$((v + 1)); done
        held[$v]=$w
        slot_cmd+=(${slot_cmd[0]+\;} set-option -w -t "$w" @agent_slot "$v")
    done
}
# slot_take WIN: WIN is becoming active - into slot_n the lowest number no
# OTHER active+visible window holds, and into slot_cmd the unsets that free it
# on any window still carrying it stale (inactive or hidden, not yet released
# by a tick), so the number moves in the same command as the stamp.
slot_take() {
    local w v
    local -A held=()
    slot_cmd=()
    for w in "${!S_ELIG[@]}"; do
        [ "$w" != "$1" ] && [ -n "${S_VIS[$w]:-}" ] || continue
        v="${S_SLOT[$w]:-}"
        slot_valid "$v" && held[$v]=1
    done
    slot_n=1
    while [ -n "${held[$slot_n]:-}" ]; do slot_n=$((slot_n + 1)); done
    for w in "${!S_SLOT[@]}"; do
        [ "$w" != "$1" ] && [ "${S_SLOT[$w]}" = "$slot_n" ] || continue
        slot_cmd+=(\; set-option -uw -t "$w" @agent_slot)
    done
}
# THE SLOT LOCK: every read-decide-write of @agent_slot (a hook stamping its
# window, a watcher tick repairing) runs under it, so two writers can never
# hand out one number. A file created O_EXCL (noclobber, builtin) holding
# "<pid> <epoch>". NOT `tmux wait-for -L`: measured on tmux 3.7c, a waiter
# that is killed stays queued as a ghost that swallows a later -U, and a
# holder killed mid-section leaves the channel locked for good - every later
# hook and tick would block forever. Here a dead holder (kill -0) or a hold
# past SLOT_STALE seconds is broken by the next waiter. A LIVE holder is not
# waited out: a section takes milliseconds, so a waiter that sees the same
# live pid on SLOT_GIVEUP consecutive polls (~50-75 ms) gives up - the hook
# skips its stamp (the watcher's next tick stamps it), the watcher retries
# next tick. If a break ever races, the worst case is a duplicate, which the
# tick repairs.
SLOT_LOCK="${TMPDIR:-/tmp}"
SLOT_LOCK="${SLOT_LOCK%/}/agent-slot.${UID:-$(id -u)}.lock"
SLOT_STALE=2
SLOT_GIVEUP=3
slot_lock() {
    local i hp ht now last="" seen=0
    for ((i = 0; i < 40; i++)); do
        printf -v now '%(%s)T' -1
        if { set -C; printf '%s %s\n' "$$" "$now" > "$SLOT_LOCK"; } 2>/dev/null; then
            set +C
            return 0
        fi
        set +C
        hp=""; ht=""
        # stderr first: a lock released since the create is a missing file
        # (no message), and `|| true` keeps a set -e caller alive.
        read -r hp ht 2>/dev/null < "$SLOT_LOCK" || true
        case "$hp" in *[!0-9]*) hp="" ;; esac
        case "$ht" in ''|*[!0-9]*) ht="$now" ;; esac
        if { [ -n "$hp" ] && ! kill -0 "$hp" 2>/dev/null; } \
           || { [ -z "$hp" ] && [ "$i" -ge 8 ]; } \
           || [ $((now - 10#$ht)) -ge "$SLOT_STALE" ]; then
            rm -f "$SLOT_LOCK" 2>/dev/null
            last=""; seen=0
            continue
        fi
        if [ -n "$hp" ]; then
            if [ "$hp" = "$last" ]; then seen=$((seen + 1)); else last=$hp; seen=1; fi
            [ "$seen" -ge "$SLOT_GIVEUP" ] && return 1
        fi
        sleep 0.025 2>/dev/null
    done
    return 1
}
# Release only our own hold (a breaker may have taken it from a stalled us).
slot_unlock() {
    local hp=""
    read -r hp _ 2>/dev/null < "$SLOT_LOCK" || true
    [ "$hp" != "$$" ] || rm -f "$SLOT_LOCK" 2>/dev/null
    return 0
}
# <<< SLOT CORE <<<

# Tick trace (see TICK TRACE in the header). Tested once per tick, builtin.
TRACE="${TMPDIR:-/tmp}/agent-tab-watcher.${UID:-$(id -u)}.trace"

# Agent event log (see AGENT EVENT LOG in the header; EVLOG is defined with
# the pidfile). EV_PREV holds last tick's read state per window that had one;
# ev_base=0 until the first tick has recorded it. ev_lines counts the file's
# lines (we are its only writer). EV_SEED/EV_SEED_AT: each window's newest
# logged state and "session US index", consulted by the first tick only.
EV_MAX="${AGENT_TAB_WATCHER_EVENTS_MAX:-2000}"     # test hooks, like STALE_EVERY
EV_KEEP="${AGENT_TAB_WATCHER_EVENTS_KEEP:-1000}"
declare -A EV_PREV=() EV_NEXT=() EV_SEED=() EV_SEED_AT=()
ev_base=0
ev_lines=0
EV_STRIP=$'\x1f\n\r'
# A regular file, or nothing yet: never a FIFO (an open would block the
# daemon), a symlink (writes redirected) or anything else.
ev_safe() { [ ! -L "$EVLOG" ] && { [ -f "$EVLOG" ] || [ ! -e "$EVLOG" ]; }; }
# Startup, once: the line count and the restart seed, from one builtin
# mapfile (<= EV_MAX lines). Only well-formed lines (8 separators) count
# toward the seed; a later line for the same window overwrites an earlier one.
if ev_safe && [ -f "$EVLOG" ]; then
    ev_all=()
    { mapfile -t ev_all < "$EVLOG"; } 2>/dev/null
    ev_lines=${#ev_all[@]}
    for ev_ln in "${ev_all[@]}"; do
        ev_f="${ev_ln//[!$US]/}"
        [ "${#ev_f}" = 8 ] || continue
        ev_r="${ev_ln#*$US}"
        ev_w="${ev_r%%$US*}"; ev_r="${ev_r#*$US}"
        case "$ev_w" in @*) ;; *) continue ;; esac
        ev_s="${ev_r%%$US*}"; ev_r="${ev_r#*$US}"
        ev_i="${ev_r%%$US*}"; ev_r="${ev_r#*$US}"
        EV_SEED[$ev_w]="${ev_r%%$US*}"; EV_SEED_AT[$ev_w]="$ev_s$US$ev_i"
    done
    unset ev_all ev_ln ev_f ev_r ev_w ev_s ev_i
fi
# log_event WIN STATE PREV: one line, fields from this tick's read. Builtins
# only except the rare cap rewrite (one mv per EV_MAX - EV_KEEP events).
log_event() {
    local f line="$tick_now" dk="${W_DK[$1]:-}" dt="${W_DTXT[$1]:-}" ev_all
    # Idle carries no event of its own (see the header): no stale detail.
    [ "$2" = idle ] && { dk=""; dt=""; }
    # A QUIET RESTORE (AGENT-ORIGINATED TURNS: @agent_quiet, set with it)
    # is logged as what it was, a reply to another agent, whatever state it
    # restored (idle included): detail_kind `quiet`, detail "↩ replied to
    # @from". The window's own restored detail is not this line's. A restore
    # never writes `running`: a running line with the mark means a new turn
    # began before the tick looked, and is logged as that turn.
    if [ -n "${W_QUIET[$1]:-}" ] && [ "$2" != running ]; then
        dk=quiet
        case "${W_QUIET[$1]}" in
            -) dt="↩ replied to another agent" ;;
            *) dt="↩ replied to @${W_QUIET[$1]}" ;;
        esac
    fi
    for f in "$1" "${W_SESS[$1]:-}" "${W_IDX[$1]:-}" "$2" "$3" \
             "$dk" "${W_SUM[$1]:-${W_NAME[$1]:-}}" "$dt"; do
        line+="$US${f//[$EV_STRIP]/}"
    done
    ev_safe || return 0
    { printf '%s\n' "$line" >> "$EVLOG"; } 2>/dev/null || return 0
    ev_lines=$((ev_lines + 1))
    [ "$ev_lines" -gt "$EV_MAX" ] || return 0
    ev_all=()
    { mapfile -t ev_all < "$EVLOG"; } 2>/dev/null || return 0
    ev_lines=${#ev_all[@]}
    [ "$ev_lines" -gt "$EV_KEEP" ] || return 0
    # rm first: a planted symlink at the temp path is unlinked, not followed.
    rm -f "$EVLOG.$$.tmp" 2>/dev/null
    if { printf '%s\n' "${ev_all[@]: -$EV_KEEP}" > "$EVLOG.$$.tmp"; } 2>/dev/null \
       && ev_safe && mv -f "$EVLOG.$$.tmp" "$EVLOG" 2>/dev/null; then
        ev_lines=$EV_KEEP
    else
        rm -f "$EVLOG.$$.tmp" 2>/dev/null
    fi
}

# THE PULSE HAS ITS OWN CLOCK. @agent_blink used to be toggled once per loop
# tick, so a pulse phase lasted POLL_SECONDS *plus the whole tick's work* -
# and that work was not small at the time: `ps -ax` alone was ~250ms
# whatever flags it got, the background-subagent scan ~450ms over 9 claude
# windows, and a cold compaction-lineage walk ~1.5s PER SESSION every 60s.
# Measured 2026-10-07: phases of 1.7s steady, with 4-6s spikes
# ("inconsistent and pretty long" - Mack). Those costs have since been cut,
# but no amount of trimming makes a reconcile loop that forks tmux and reads
# files a metronome, so the toggling moved into this child, which
# does nothing else: the loop only raises or lowers BLINK_FLAG (builtin
# redirect to raise, one rm on the falling edge), and the child flips the
# option every POLL_SECONDS while the flag exists.
#
# One actor still owns @agent_blink (rule 1 in the doc): the parent never
# toggles it any more, and the child retires itself the moment the parent is
# gone OR a different pid owns the pidfile - so a respawn can never leave two
# togglers cancelling each other out. The parent re-forks it on the next tick
# if it ever dies anyway (see the heartbeat), because nothing else would: a
# dead child is a pulse frozen at whatever phase it stopped on, until the next
# `prefix r`. It is a forked subshell, so it shares
# the parent's argv: the startup pgrep sweep in a successor kills it like
# any other straggler, and a `pgrep -f agent-tab-watcher` now legitimately
# shows TWO matches (the second with the first as its ppid).
BLINK_FLAG="${TMPDIR:-/tmp}/agent-tab-blink.${UID:-$(id -u)}"
rm -f "$BLINK_FLAG"
WATCHER_PID=$$
blink_loop() {
    # Belt-and-braces only: bash already resets caught traps in an `&`
    # subshell, so this child never inherits cleanup() (if it did, $$ - still
    # the PARENT's pid in a subshell - would read as "the pidfile is mine").
    # Explicit so the child's exit can never depend on that rule.
    trap - EXIT
    # The nap is `sleep & wait`, not a foreground sleep: bash defers a trapped
    # signal until the foreground command returns, so cleanup()'s TERM used to
    # sit pending for up to POLL_SECONDS while a successor's child was already
    # toggling. `wait` is interrupted by the trap at once (same one sleep fork
    # per beat); the trap reaps the orphaned nap with the kill builtin.
    local owner nap=""
    trap '[ -n "$nap" ] && kill "$nap" 2>/dev/null; exit 0' INT TERM HUP
    while :; do
        sleep "$POLL_SECONDS" & nap=$!
        wait "$nap" || exit 0
        nap=""
        kill -0 "$WATCHER_PID" 2>/dev/null || exit 0
        # Retire ONLY on positive evidence of a successor: a non-empty owner
        # that is not our parent. An empty or unreadable pidfile is not
        # evidence of anything (a tmp cleaner, a writer mid-swap) - it used to
        # be read as "superseded", and that one misread killed the pulse
        # permanently. It doesn't skip the beat either: the parent is alive
        # (kill -0 above), so keep pulsing; it re-claims the file next tick.
        owner=""
        read -r owner < "$PIDFILE" 2>/dev/null
        if [ -n "$owner" ] && [ "$owner" != "$WATCHER_PID" ]; then
            exit 0
        fi
        [ -e "$BLINK_FLAG" ] || continue
        if [ "$(tmux show-options -gqv @agent_blink 2>/dev/null)" = "1" ]; then
            tmux set-option -g @agent_blink 0 \; refresh-client -S 2>/dev/null
        else
            tmux set-option -g @agent_blink 1 \; refresh-client -S 2>/dev/null
        fi
    done
}
blink_loop &
BLINK_PID=$!

while :; do
    # Tick trace: armed only if the file exists now (one stat, no fork) and
    # this bash has EPOCHREALTIME (bash 5; a bash 4 just never traces - 3.2
    # never gets this far, see the version check at the top).
    trace_t0=""
    if [ -e "$TRACE" ] && [ -n "${EPOCHREALTIME:-}" ]; then
        trace_t0=$EPOCHREALTIME
    fi
    n_windows=0
    # This tick's change stamp (see CHANGE STAMPS), written before anything
    # is read so a change racing the reads below is "since" it. Builtin.
    tick_no=$((tick_no + 1))
    : > "$STAMPS.$((tick_no % STAMP_RING))" 2>/dev/null

    # PROCESS DISCOVERY: agent TTYs, plus tty=pid for every agent pane —
    # claude pids feed the workflow lookup (codex pids simply miss in
    # ~/.claude/sessions and fall through), and both kinds feed the
    # computer-use pid match (@agent_cua), which is agent-agnostic.
    #
    # NOT `ps -ax`. That was one call per tick, but macOS ps gathers task
    # info for EVERY process whatever -o asks for: ~250 ms wall on a quiet
    # machine, 0.7-2.5 s under load, ~570 ms of CPU per call (measured
    # 2026-10-07, ~1700 processes). And `ps -p a,b,c` is no way out: with
    # more than one pid it takes the same all-process path (13 pids: ~1.3 s,
    # vs ~6 ms for one). What is cheap is pgrep (~40 ms wall, ~7 ms CPU: a
    # sysctl walk, no task info) and SINGLE-pid ps. So: pgrep for candidates,
    # then a single-pid ps per pid NOT SEEN BEFORE, all in parallel (13 new
    # pids: ~30 ms), and the answer cached per pid. Steady state is one pgrep
    # per tick.
    #
    # pgrep matches the basename of the same argv[0]-derived name ps prints
    # as comm (checked against every process on this machine: claude
    # matches as "claude" though its p_comm is "2.1.291", vendor/.../codex as
    # "codex", a path argv[0] by its basename) - but it is only a candidate
    # filter: each new pid's comm is re-checked with is_agent_comm, exactly
    # as before, and its tty must not be empty/"??". -a: pgrep otherwise
    # skips its own ancestors, and ps never did. -x + the alternation is
    # anchored as a whole (^(...)$), same shape as is_agent_comm.
    #
    # THE CACHE (PID_TTY[pid] = tty, "-" = not an agent / no tty) is rebuilt
    # from each tick's pgrep, so a pid drops out the tick it stops matching;
    # a stale entry would need an agent to die AND its pid to be reused by
    # another agent-named process within one tick (the pid space wraps in
    # ~15 min). A process's controlling tty and comm don't change under it
    # short of exec/setsid; as belt and braces the whole cache is dropped
    # every PID_CACHE_TICKS ticks (one cold refresh a minute, ~30 ms).
    #
    # FAILURE HANDLING. A failed or empty listing used to make has_agent=0 for
    # every window, the GC unset every @agent_* option, and the next good
    # tick reseeded them all to `idle` - which is TERMINAL for a mid-turn
    # agent, because the heartbeat only re-arms `running` from idle when
    # @agent_pending is set, and the GC had just cleared it. So a real error
    # skips the tick (fail_streak), like the tmux reads around it: pgrep
    # exit >= 2 (bad pattern / fatal; 127 = missing), or new candidates of
    # which ps could describe NONE (all exiting in the ~ms between the two is
    # possible but one skipped tick is cheap; a broken ps is not). pgrep exit
    # 1 is NOT a failure: it means zero agent processes, a normal state (the
    # GC_TICKS hysteresis still applies before any window is collected).
    # LC_ALL=C: under the UTF-8 locale tmux hands us, macOS pgrep exits 3
    # ("illegal byte sequence") if ANY process on the machine has a
    # non-UTF-8 argv[0] - agent or not - and every tick would then fail
    # until that process exits (reconcile and heartbeat both stop, and
    # ensure_watcher reaps and respawns into the same wall). The pattern is
    # ASCII, so the C locale matches exactly the same names.
    cand=$(LC_ALL=C pgrep -ax "$AGENT_PAT" 2>/dev/null)
    rc=$?
    if [ "$rc" -ge 2 ]; then
        fail_streak=$((fail_streak + 1))
        { [ "$fail_streak" -ge "$FAIL_LIMIT" ] && server_gone; } && exit 0
        sleep "$POLL_SECONDS"
        continue
    fi
    [ "$rc" = 0 ] || cand=""
    pid_cache_age=$((pid_cache_age + 1))
    if [ "$pid_cache_age" -ge "$PID_CACHE_TICKS" ]; then
        PID_TTY=(); PID_KIND=()
        # The subagent verdict cache rides the same clock: dead pids drop
        # out, and every session gets one full look a minute regardless.
        SUB_KEY=(); SUB_TICK=(); SUB_V=()
        BG_KEY=(); BG_TICK=(); BG_V=()     # the background-task verdicts too
        pid_cache_age=0
    fi
    new_pids=""
    for pid in $cand; do
        [ -n "${PID_TTY[$pid]+x}" ] || new_pids="${new_pids} ${pid}"
    done
    if [ -n "$new_pids" ]; then
        # One write per ps (a ~30-byte line, under PIPE_BUF), so parallel
        # output never interleaves mid-line. The `&` jobs belong to this
        # comsub's subshell, never to the watcher's own job table.
        #
        # NO `wait` in here. The comsub already reads its pipe to EOF, and
        # every ps holds the write end until it exits, so all output is in
        # before $(...) returns (the subshell exits at once; its ps children
        # are reparented and reaped by launchd). And a bare `wait` here is a
        # trap: bash 5.3.9 makes the comsub inherit the parent's job table,
        # so `wait` tries the pulse child, gets "pid N is not a child of this
        # shell", and spins on that error forever - a wedged tick.
        ps_out=$(for pid in $new_pids; do ps -o tty=,pid=,comm= -p "$pid" 2>/dev/null & done)
        if [ -z "$ps_out" ]; then
            fail_streak=$((fail_streak + 1))
            { [ "$fail_streak" -ge "$FAIL_LIMIT" ] && server_gone; } && exit 0
            sleep "$POLL_SECONDS"
            continue
        fi
        while IFS=' ' read -r tty pid comm; do
            [ -n "$pid" ] || continue
            # An opencode pid is an agent only as the interactive TUI
            # (opencode_tui_args): `opencode serve` / `web` / `mcp ...` in a
            # shell tab is a command, not an agent. That needs its argv, so
            # one more single-pid ps - for a NEW opencode pid only (the
            # verdict is cached in PID_TTY like everything else here).
            # An EMPTY answer is no verdict: it is not cached (a cached "-"
            # would hide a live TUI for up to PID_CACHE_TICKS, long enough for
            # the GC to collect its tab), so the pid counts as exited this
            # tick and is looked at again next tick; one that really exited
            # leaves the pgrep set anyway.
            agent_ok=0
            if [ -n "$tty" ] && [ "$tty" != "??" ] && is_agent_comm "$comm"; then
                agent_ok=1
                if [ "${comm##*/}" = opencode ]; then
                    oc_args=$(ps -ww -o args= -p "$pid" 2>/dev/null)
                    [ -n "$oc_args" ] || continue
                    opencode_tui_args "$oc_args" || agent_ok=0
                fi
            fi
            if [ "$agent_ok" = 1 ]; then
                PID_TTY[$pid]="$tty"
                # is_agent_comm's names: codex, opencode, else claude (incl. N.N.N).
                case "${comm##*/}" in
                    codex) PID_KIND[$pid]=codex ;;
                    opencode) PID_KIND[$pid]=opencode ;;
                    *) PID_KIND[$pid]=claude ;;
                esac
            else
                PID_TTY[$pid]=-
                unset 'PID_KIND[$pid]'
            fi
        done <<EOF
$ps_out
EOF
    fi
    # In pgrep's order: ascending pid. ps -ax sorted by tty, then pid, and
    # the tty->pid map below only ever keeps the FIRST pid seen on a tty, so
    # "first agent pane wins" still picks the lowest pid on it, as it did.
    TTY_PID=()
    PID_NEXT=()
    n_agents=0
    for pid in $cand; do
        [ -n "${PID_TTY[$pid]+x}" ] || continue   # exited between pgrep and its ps
        tty="${PID_TTY[$pid]}"
        PID_NEXT[$pid]="$tty"
        [ "$tty" = - ] && continue
        [ -n "${TTY_PID[$tty]+x}" ] || TTY_PID[$tty]="$pid"
        n_agents=$((n_agents + 1))
    done
    PID_TTY=()
    for pid in "${!PID_NEXT[@]}"; do PID_TTY[$pid]="${PID_NEXT[$pid]}"; done

    # ONE READ. Every pane, carrying its window's fields, in a single tmux
    # call. This used to be six: list-panes for the ttys, then five
    # list-windows -a (state/clients/since, summary, workflow, cua, rollout),
    # each a fork+exec of tmux plus a full walk of the server - ~15 ms apiece
    # at load ~200 with 165 windows, i.e. ~75 ms of every tick for data one
    # call returns. User options resolve pane -> window -> global in a pane
    # format, and nothing sets @agent_* per pane, so these are exactly the
    # window values list-windows printed (diffed against the live server,
    # 2026-10-07: identical). Read AFTER discovery, as the states read always
    # was, so the gap between reading a state and acting on it stays short.
    #
    # Fields are split on US (\x1f), NOT on spaces. `read` collapses runs of
    # IFS whitespace, and \t counts as whitespace too, so with a space here an
    # EMPTY @agent_state vanished and the client count slid into `state`
    # (measured 2026-10-07: 104 of 115 windows, every tick). Both halves of
    # the watcher then misfired: an agent window with no state never got its
    # idle seed (state was "0"/"1", not empty), and every non-agent window read
    # as a stale agent and ran the 7-unset GC every GC_TICKS ticks, forever.
    # US is not whitespace, so `read` keeps empty fields, middle ones
    # included. Same idiom as stash.sh.
    #
    # The summary rides LAST: it is free text (spaces are fine; it never
    # holds a US), and as the final variable it would absorb any stray
    # separator rather than shift every field after it. A summary can
    # outlive @agent_state (a detached condenser may write one after the
    # agent died and the watcher GC'd its state), which is why the GC also
    # keys on it. @agent_since rides along: "<epoch> <state>", see the stamp
    # below. Rollout paths (see codex_tail) live under ~/.codex/sessions.
    # @agent_kind / @agent_detail_kind (the sidebar's fields, written by the
    # indicator) are bare tokens, read only so a window holding nothing else
    # still leaves the fast path and gets collected (@agent_detail always
    # travels with its kind, so it never decides that). Session, index, name
    # and @agent_detail ride along for the AGENT EVENT LOG only: free text,
    # but the indicator strips controls from the detail, and session/window
    # names hold no US in practice; the summary stays last regardless.
    # @agent_slot and @agent_pending (AGENT SLOTS) ride before them all:
    # digits only, and ahead of the free text a stray US in a detail can
    # never shift into them. So does @agent_born (BIRTH GRACE), digits too.
    #
    # A row only counts if it starts with a window id and carries a /dev/
    # tty, so a line that is not a pane row (a summary with an embedded
    # newline continues on its own line) can never be taken for a window.
    # @agent_prior_state (AGENT-ORIGINATED TURNS) is a bare state token, so it
    # rides with the digit fields, ahead of all free text.
    # So do @agent_limit ("R T E", bare tokens) and @agent_limit_due (RATE-
    # LIMITED AGENTS): a GLOBAL, which a pane format resolves through the
    # window to the global value - the resume trigger's input at no extra
    # fork, the same on every row.
    if ! rows=$(tmux list-panes -a -F "#{window_id}${US}#{pane_tty}${US}#{@agent_state}${US}#{window_active_clients}${US}#{@agent_since}${US}#{@agent_workflow}${US}#{@agent_cua}${US}#{@agent_rollout}${US}#{@agent_kind}${US}#{@agent_detail_kind}${US}#{@agent_slot}${US}#{@agent_pending}${US}#{@agent_born}${US}#{@agent_prior_state}${US}#{@agent_quiet}${US}#{@agent_paused}${US}#{@agent_ask_bg}${US}#{@agent_limit}${US}#{@agent_limit_due}${US}#{session_name}${US}#{window_index}${US}#{window_name}${US}#{@agent_detail}${US}#{@agent_summary}" 2>/dev/null); then
        fail_streak=$((fail_streak + 1))
        { [ "$fail_streak" -ge "$FAIL_LIMIT" ] && server_gone; } && exit 0
        sleep "$POLL_SECONDS"
        continue
    fi
    fail_streak=0

    # Fold pane rows into windows: the first row of a window records its
    # fields (a window's panes all carry the same ones; a window linked into
    # several sessions is listed once per link but reconciled ONCE - it used
    # to run twice, writes and all), and any agent pane maps the window to
    # its pid (first agent pane wins) for the workflow/cua/status lookups.
    #
    # THE IDLE FAST PATH. Most windows here are pty-MCP shells in `agents`
    # (146 of 165 at the last count) with no agent and no @agent_* option at
    # all. Such a window has nothing to seed, collect, stamp or clear, so it
    # is recorded as SEEN and nothing else, and the loop below skips it on
    # sight. This is decided by content, never by session name: an `agents`
    # pane that runs a headless `claude -p` has an agent pane (W_PID) and
    # gets the full path - seeded, stamped, and garbage-collected once it
    # exits, exactly like any other window.
    W_SEEN=(); W_PID=(); W_STATE=(); W_WAC=(); W_SINCE=(); W_WF=(); W_CUA=(); W_ROLL=(); W_SUM=(); W_DET=()
    W_DK=(); W_DTXT=(); W_SESS=(); W_IDX=(); W_NAME=(); W_AK=()
    W_PEND=(); W_BORN=(); W_PRIOR=(); W_QUIET=(); W_PAUSED=(); W_ASKBG=(); W_LIMIT=(); S_SLOT=(); S_VIS=(); S_ELIG=()
    wins=()
    lim_due=""
    while IFS="$US" read -r win tty state wac since wf_opt cua_opt roll akind dkind slot pend born prior quiet paused askbg limit ldue sess widx wname dtxt summary; do
        case "$win" in @*) ;; *) continue ;; esac
        case "$tty" in /dev/?*) ;; *) continue ;; esac
        [ -n "$lim_due" ] || lim_due="$ldue"
        if [ -z "${W_SEEN[$win]+x}" ]; then
            W_SEEN[$win]=1
            wins+=("$win")
            if [ -n "${state}${since}${wf_opt}${cua_opt}${roll}${akind}${dkind}${born}${prior}${paused}${limit}${summary}" ]; then
                W_PRIOR[$win]="$prior"; W_QUIET[$win]="$quiet"; W_PAUSED[$win]="$paused"
                W_ASKBG[$win]="$askbg"; W_LIMIT[$win]="$limit"
                W_STATE[$win]="$state"; W_WAC[$win]="$wac"; W_SINCE[$win]="$since"
                W_WF[$win]="$wf_opt"; W_CUA[$win]="$cua_opt"; W_ROLL[$win]="$roll"
                W_SUM[$win]="$summary"; W_DET[$win]="${akind}${dkind}"; W_AK[$win]="$akind"
                W_DK[$win]="$dkind"; W_DTXT[$win]="$dtxt"
                W_SESS[$win]="$sess"; W_IDX[$win]="$widx"; W_NAME[$win]="$wname"
                W_PEND[$win]="$pend"; W_BORN[$win]="$born"
            fi
            # Not part of the fast-path test above: a window holding only a
            # slot (or only a pending stamp) has nothing for the loop to do,
            # and the slot pass after it releases the slot from here.
            [ -n "$slot" ] && S_SLOT[$win]="$slot"
        fi
        # Slot visibility (slot_vis): every row is one link of the window.
        # Hidden rows - most of them, the `agents` shells - skip the call.
        case "$SLOT_EXCLUDE" in
            *" $sess "*) ;;
            *) slot_vis "$win" "$sess" "$widx" ;;
        esac
        tty="${tty#/dev/}"
        if [ -n "${TTY_PID[$tty]+x}" ] && [ -z "${W_PID[$win]+x}" ]; then
            W_PID[$win]="${TTY_PID[$tty]}"
        fi
    done <<EOF
$rows
EOF

    # Not `now`: agent-session-lib.sh assigns a global of that name.
    printf -v tick_now '%(%s)T' -1
    # This tick's live driver pids, into cua_pids.
    refresh_cua_pids

    changed=0
    state_moved=0   # an @agent_state write this tick: post the STATE NOTICE
    any_workflow=0
    any_cua=0
    # Decided per window inside the loop, from the PARSED state. It used to be
    # a substring test on the raw list-windows output (`*" running"*`), which
    # silently depended on the separator being a space; a US separator would
    # have frozen every pulse at whatever phase it happened to be in.
    blink_active=0
    # Rebuilt each tick; a window that stops reading idle drops out, so the
    # streak only ever counts CONSECUTIVE observations. (Plain strings, not
    # arrays: they only ever hold the few windows mid-hysteresis.)
    idle_streak_next=" "
    gc_streak_next=" "
    wez_front=""   # per tick, computed at most once, only if a tinted tab is watched
    EV_NEXT=()
    for win in "${wins[@]}"; do
        n_windows=$((n_windows + 1))
        pid="${W_PID[$win]:-}"
        # The idle fast path (see the fold above): no agent pane and no
        # @agent_* option means every branch below is a no-op. No heartbeat
        # either - a window that does nothing cannot be the slow one.
        [ -n "$pid" ] || [ -n "${W_STATE[$win]+x}" ] || continue
        state="${W_STATE[$win]:-}"
        # Event log: the state as READ, before this tick touches it (see
        # AGENT EVENT LOG). A fast-path window has no state to log. The first
        # tick compares with the window's newest log line instead, and only
        # while that line's session+index still match this id (RESTARTS).
        if [ -n "$state" ]; then
            EV_NEXT[$win]="$state"
            if [ "$ev_base" = 1 ]; then
                [ "${EV_PREV[$win]:-}" != "$state" ] \
                    && log_event "$win" "$state" "${EV_PREV[$win]:-}"
            elif [ -n "${EV_SEED[$win]:-}" ] && [ "${EV_SEED[$win]}" != "$state" ] \
                 && [ "${EV_SEED_AT[$win]}" = "${W_SESS[$win]:-}$US${W_IDX[$win]:-}" ]; then
                log_event "$win" "$state" "${EV_SEED[$win]}"
            fi
        fi
        # The quiet-restore mark is one-shot: consumed by the read that saw
        # it (logged above if the state moved, which it does with it).
        if [ -n "${W_QUIET[$win]:-}" ]; then
            tmux set-option -uw -t "$win" @agent_quiet 2>/dev/null
        fi
        wac="${W_WAC[$win]:-}"
        since="${W_SINCE[$win]:-}"
        # MID-TICK HEARTBEAT. The heartbeat means "the loop is turning", and a
        # slow tick turns slowly but does turn. A slow per-window call makes
        # the tick's length scale with the window count: it happened with a
        # compaction-lineage walk (since removed) that grepped every
        # transcript in the project dir (643 MB, ~7 s per session) with a
        # cache that lived in this process, so the first tick after any
        # (re)start paid it for every claude window. Measured 2026-10-07:
        # first ticks past 35 s, so ensure_watcher's 30 s grace read a working
        # daemon as wedged, reaped it, and the respawn started cold again - a
        # restart every ~35 s with no reconcile ever finishing. Stamping once
        # per window that does any work (the idle fast path above skips this
        # too) keeps any such slow tick alive while a truly blocked call
        # (one window stuck > 30 s) still goes stale. Builtins only (read +
        # in-place printf): no fork. Only while we still own the file - never
        # stamp over a successor.
        read -r _owner < "$PIDFILE" 2>/dev/null || _owner=""
        [ "$_owner" = "$$" ] && restamp_pidfile
        # SEEN-IT, CONTINUOUSLY. The hook discharges a yellow/green that lands
        # while the user is sitting on the tab with WezTerm focused; this is
        # the other order - the tint landed while WezTerm was behind something,
        # and the user then Cmd-Tabbed back to it without changing tabs, so no
        # select-window hook ever fires. Same test (active for a client AND
        # WezTerm frontmost), same discharge as clear-current, once a second.
        # Red is left alone: a dead turn is not answered by being looked at.
        # The frontmost lookup forks twice, so it runs only when there is a
        # tinted, watched window to ask about - almost never.
        # A needs-input discharge stamps @agent_pending in the SAME tmux call
        # as the idle: idle + pending holds the window's slot (AGENT SLOTS),
        # and a hook stamping under the slot lock between two calls would
        # read bare idle and hand that number to someone else. Every discharge
        # also stamps @agent_seen_at in that call (CuaNotch's ack, see
        # seen_op in agent-tab-indicator.sh's set_state).
        # Each discharge is a cas_write (CONDITIONAL WRITES): it lands only
        # while the window still holds the state the tick read.
        pending="${W_PEND[$win]:-}"
        case "$state" in
            done|needs-input)
                if viewed_now "$wac"; then
                    # A raised ask's shown line is kept as seen (copied
                    # tmux-side) only while the detail still IS the ask
                    # (kind `ask`, as the tick read it).
                    aseen=""
                    [ -n "${W_ASKBG[$win]:-}" ] && [ "${W_DK[$win]:-}" = ask ] \
                        && aseen="set-option -w -t $win -F @agent_ask_seen '#{@agent_detail}' ; "
                    if [ "$state" = needs-input ] && [ -n "${W_ASKBG[$win]:-}" ] && [ -n "${W_WF[$win]:-}" ]; then
                        # ASKS WHILE WORK IS OUT (the indicator's section;
                        # clear-current's twin): a raised ask, seen while
                        # its work is still out (@agent_workflow), goes back
                        # to the `running` it was held at - seen_at and
                        # since on one epoch, the detail kind no longer
                        # `ask`, the shown line kept as @agent_ask_seen
                        # (copied tmux-side, before anything else moves).
                        cas_write "$win" "#{&&:#{==:#{@agent_state},needs-input},#{!=:#{@agent_ask_bg},}}" \
                            "${aseen}set-option -w -t $win @agent_state running ; set-option -w -t $win @agent_since '$tick_now running' ; set-option -w -t $win @agent_seen_at $tick_now ; set-option -uw -t $win @agent_detail_kind ; set-option -uw -t $win @agent_ask_bg ; set-option -uw -t $win @agent_ask_from" \
                            && [ "$state" = running ] && { changed=1; state_moved=1; }
                    elif [ "$state" = needs-input ]; then
                        # @agent_ask_from (the indicator's ASKER GATE) goes
                        # with the needs-input it described, as in set_state;
                        # so does @agent_ask_bg, whose line (if it was one)
                        # is seen now (aseen, above).
                        cas_write "$win" "#{==:#{@agent_state},needs-input}" \
                            "${aseen}set-option -w -t $win @agent_state idle ; set-option -w -t $win @agent_pending $tick_now ; set-option -w -t $win @agent_seen_at $tick_now ; set-option -uw -t $win @agent_ask_from ; set-option -uw -t $win @agent_ask_bg" \
                            && [ "$state" = idle ] && { changed=1; state_moved=1; }
                    else
                        cas_write "$win" "#{==:#{@agent_state},done}" \
                            "set-option -w -t $win @agent_state idle ; set-option -w -t $win @agent_seen_at $tick_now" \
                            && [ "$state" = idle ] && { changed=1; state_moved=1; }
                    fi
                fi ;;
        esac
        # ...and the attention state an agent-originated turn is holding for
        # its quiet restore (AGENT-ORIGINATED TURNS): seen the same way, so
        # the restore can't re-tint what Mack already looked at. Red is left
        # alone here too. Asks the front app only for a watched window.
        case "${W_PRIOR[$win]:-}" in
            done|needs-input)
                if viewed_now "$wac"; then
                    cas_write "$win" "#{==:#{@agent_prior_state},${W_PRIOR[$win]}}" \
                        "set-option -w -t $win @agent_prior_state idle ; set-option -w -t $win @agent_seen_at $tick_now" || true
                fi ;;
        esac
        has_agent=0; [ -n "$pid" ] && has_agent=1
        # BIRTH GRACE (see its note): a fresh mark with no agent yet holds the
        # window off the GC; the agent arriving retires the mark.
        born="${W_BORN[$win]:-}"
        birth_hold=0
        if [ -n "$born" ]; then
            if [ "$has_agent" = 1 ]; then
                tmux set-option -uw -t "$win" @agent_born 2>/dev/null
                born=""
            elif birth_fresh "$born"; then
                birth_hold=1
            fi
        fi
        # A leftover sidebar kind/detail is collectable exactly like a summary.
        # So is a leftover @agent_prior_state (AGENT-ORIGINATED TURNS).
        has_summary=0; [ -n "${W_SUM[$win]:-}${W_DET[$win]:-}${W_PRIOR[$win]:-}${W_PAUSED[$win]:-}${W_LIMIT[$win]:-}" ] && has_summary=1
        had_wf=0; [ -n "${W_WF[$win]:-}" ] && had_wf=1
        had_cua=0; [ -n "${W_CUA[$win]:-}" ] && had_cua=1

        # Background-workflow + computer-use detection (both need a live agent
        # and share the one pane→pid lookup; workflows are claude-only).
        wf=0
        cua=0
        bg_held=0
        if [ "$has_agent" = 1 ]; then
            # A workflow or a background subagent: one gear for both. The
            # subagent half goes through subagent_running's change gate.
            if [ -n "$pid" ] && { session_has_running_workflow "$pid" \
                                  || subagent_running "$pid"; }; then
                wf=1; any_workflow=1
            fi
            # A background task the ended turn is waiting on: gear on, and
            # bg_held keeps the un-stick below from idling the tab (see
            # BACKGROUND TASKS HOLD A TAB RUNNING).
            # Asked whatever wf says: a stale gear (the lib's verdict after a
            # crash and resume) must not stop a real task from holding.
            # Also asked for a raised ask (ASKS WHILE WORK IS OUT: needs-input
            # + @agent_ask_bg), so its gear - which a look reads as "the work
            # is still out" - stays true for a background task too.
            if { [ "$state" = running ] || { [ "$state" = needs-input ] && [ -n "${W_ASKBG[$win]:-}" ]; }; } \
               && [ -n "$pid" ] \
               && [ "${PID_KIND[$pid]:-}" != codex ] \
               && [ "${PID_KIND[$pid]:-}" != opencode ]; then
                st=""
                session_status "$pid"
                case "$st" in
                    idle|shell)
                        bg_task_pending "$pid" && { bg_held=1; wf=1; any_workflow=1; } ;;
                esac
            fi
            case "$cua_pids" in
                *" ${pid} "*) [ -n "$pid" ] && { cua=1; any_cua=1; } ;;
            esac
        else
            # NO AGENT PANE: hold both flags as they are until the GC below
            # collects the window (GC_TICKS agent-less ticks in a row), like
            # the state itself. tmux-thumbs (prefix+Space) swaps the agent
            # pane out for the seconds its picker is up; dropping the gear or
            # robot at once flickered it and, with the state idle, freed the
            # window's slot (AGENT SLOTS) - it came back with a new number.
            # Held flags keep pulsing (the blink child would otherwise freeze
            # them on one phase).
            wf=$had_wf; cua=$had_cua
            [ "$wf" = 1 ] && any_workflow=1
            [ "$cua" = 1 ] && any_cua=1
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
        #
        # CODEX TURN ENDS, including needs-input. Esc on a codex approval
        # prompt, or Esc mid-turn, fires no hook either, so a codex tab sat
        # yellow (or pulsing) after the turn was gone. Its rollout says so: a
        # turn_aborted or task_complete whose own timestamp is AFTER the
        # window's stamp (cx_after) ended the turn this state belongs to. That
        # is the one ground truth that may clear needs-input: an open prompt is
        # still inside its turn, so it has no turn end after its stamp, and an
        # old turn end (before the stamp) is ignored. A turn end after the
        # stamp is written the way the indicator would have: task_complete is
        # its Stop (done, or idle while viewed_now; detail "done turn
        # finished", the Stop's own fallback text, since the reply itself would
        # need the indicator's jq sanitizer), turn_aborted its interrupt mode
        # (idle); both unset @agent_pending, and an abort unsets the detail as
        # SessionStart/clear_state do. One tmux call carries all of it plus the
        # @agent_since stamp. Codex agents only (PID_KIND, from the agent's
        # own comm, in both states), and the rollout is looked at only for a
        # window ALREADY running or needs-input - one bounded read when it
        # changes (codex_tail_cached), builtin -nt tests otherwise - and every
        # other window none. A running tab whose last turn end predates its
        # stamp, or whose agent is not codex, keeps the older rule (idle after
        # the streak, state only).
        #
        # OPENCODE: its plugin's status file (opencode_status) is the ground
        # truth, read the way Claude's session file is - `idle` for three
        # ticks in a row clears a stuck `running` to idle, state only (Esc
        # mid-turn, a missed session.idle, a plugin spawn that failed). Only
        # `running`: like Claude's, it is not trusted to clear needs-input.
        # An opencode pid never falls through to the Claude session file or a
        # (stale, earlier-codex) rollout.
        fin=""        # the state this agent's own record says, if it disagrees
        cx_full=0     # 1 = a codex turn end after the stamp: the full write
        cx_lim=0      # 1 = that turn end was a usage limit (RATE-LIMITED AGENTS)
        if [ "$has_agent" = 1 ]; then
            case "$state" in
                running)
                    st=""
                    if [ "${PID_KIND[$pid]:-}" = opencode ]; then
                        opencode_status "$pid"                      # opencode
                    else
                        [ -n "$pid" ] && session_status "$pid"      # claude
                    fi
                    if [ -z "$st" ] && [ -n "${W_ROLL[$win]:-}" ] \
                       && [ "${PID_KIND[$pid]:-}" != opencode ]; then   # codex
                        codex_tail_cached "$win" "${W_ROLL[$win]}"
                        # The full turn-end write is for a CODEX agent only: a
                        # claude window with no session file and a stale
                        # rollout path (left by an earlier codex in the pane)
                        # keeps the older rule - idle, state only - never a
                        # codex "done" it did not have.
                        case "$cx_mark" in
                            complete|aborted|limit)
                                if [ "${PID_KIND[$pid]:-}" = codex ] && cx_after; then
                                    cx_full=1
                                    [ "$cx_mark" = limit ] && cx_lim=1
                                else
                                    st=idle
                                fi ;;
                        esac
                    fi
                    # "shell" is Claude Code's at-the-prompt status now (seen
                    # on every idle session, 2.1.278-2.1.284, 2026-09-29) -
                    # checking for "idle" alone left a tab that missed its Stop
                    # pinned at running. "waiting" (a gate on screen) and
                    # "busy" still never qualify.
                    case "$st" in idle|shell) [ "$bg_held" = 1 ] || fin=idle ;; esac
                    ;;
                needs-input)
                    if [ "${PID_KIND[$pid]:-}" = codex ] && [ -n "${W_ROLL[$win]:-}" ]; then
                        codex_tail_cached "$win" "${W_ROLL[$win]}"
                        case "$cx_mark" in
                            complete|aborted) cx_after && cx_full=1 ;;
                            limit) cx_after && cx_lim=1 ;;
                        esac
                    fi
                    ;;
                done|idle)
                    # A usage limit whose turn codex's own Stop hook already
                    # painted (done, or idle while viewed) in the same breath:
                    # see cx_recent. Only a codex window with a rollout whose
                    # stamp describes this state and is under
                    # LIMIT_FRESH_STAMP s old (the Stop that just landed), so
                    # a SETTLED done/idle window reads no rollout at all; a
                    # cached look (builtin -nt) unless the file moved.
                    cx_e="${since%% *}"
                    if [ "${PID_KIND[$pid]:-}" = codex ] && [ -n "${W_ROLL[$win]:-}" ] \
                       && [ "${since#* }" = "$state" ] && [[ $cx_e =~ ^[0-9]{1,12}$ ]] \
                       && [ $((tick_now - 10#$cx_e)) -le "$LIMIT_FRESH_STAMP" ]; then
                        codex_tail_cached "$win" "${W_ROLL[$win]}"
                        [ "$cx_mark" = limit ] && cx_recent && cx_lim=1
                    fi
                    ;;
            esac
            if [ "$cx_lim" = 1 ]; then
                # RATE-LIMITED AGENTS: marked at once (the rollout's verdict is
                # final, no hysteresis), and nothing else ends this turn.
                # If the rollout does not parse as a limit after all (rc 2),
                # it is ended as the plain `complete` it also is, so the turn
                # can never wedge at running.
                cx_full=0
                cx_rc=0
                codex_limit_mark "$win" "$state" "${W_ROLL[$win]}" || cx_rc=$?
                if [ "$cx_rc" = 0 ]; then
                    changed=1; state_moved=1
                elif [ "$cx_rc" = 2 ]; then
                    case "$state" in
                        running|needs-input) cx_full=1; cx_mark=complete; fin=done ;;
                    esac
                fi
            elif [ "$cx_full" = 1 ]; then
                fin=idle
                [ "$cx_mark" = complete ] && fin=done
            fi
        fi
        if [ -n "$fin" ]; then
            n=0
            for kv in $idle_streak; do
                case "$kv" in "${win}="*) n="${kv#*=}"; break ;; esac
            done
            n=$((n + 1))
            if [ "$n" -ge 3 ]; then
                # RATE-LIMITED AGENTS: whatever this turn end writes, it is an
                # outcome, and any outcome but a rate limit ends the limit
                # (and the limit's own pause flag): lim_cmd rides in its write.
                limit_end_cmd "$win"
                if [ -n "${W_PAUSED[$win]:-}" ]; then
                    # PAUSED (docs/agent-pause.md, "The paused state"): this
                    # turn end - its background work drained, Esc, a missed
                    # Stop, a codex turn end - is `paused`, never idle, done
                    # or a restore. A saved prior stays for after the resume
                    # (the indicator's prior_save keeps it); nobody's work is
                    # out any more. One call. The flag was read at the tick's
                    # start; agent-pause.py may have cleared it for a resume
                    # since (flag off + paused -> idle, in one call), so the
                    # SAME command list re-checks it and lands on idle if it
                    # is gone - what that resume would have made of it. And
                    # the whole list lands only over the state this was
                    # decided from (CONDITIONAL WRITES, 2026-10-22): a gate
                    # that just landed keeps its yellow, since and pending.
                    cas_was="$state"
                    cas_write "$win" "#{==:#{@agent_state},$cas_was}" \
                        "set-option -w -t $win @agent_state paused ; set-option -w -t $win @agent_since '$tick_now paused' ; set-option -uw -t $win @agent_pending ; set-option -uw -t $win @agent_bg_owner ; set-option -uw -t $win @agent_ask_bg${lim_cmd} ; if-shell -F -t $win '#{?#{@agent_paused},0,1}' 'set-option -w -t $win @agent_state idle'" \
                        && [ "$state" != "$cas_was" ] && { changed=1; state_moved=1; }
                    fin="$state"
                elif [ -n "${W_PRIOR[$win]:-}" ]; then
                    # An agent-originated turn ended here (its work drained, Esc,
                    # a missed Stop, a codex turn end): restore, never tint.
                    if prior_restore "$win" "$wac" "$state"; then
                        changed=1; state_moved=1; since="$r_since"; fin="$r_state"
                        W_PRIOR[$win]=""
                    else
                        fin="$state"   # nothing written: the window is as read
                    fi
                elif [ "$cx_full" = 1 ]; then
                    # The Stop path's viewing_now: a finish the user is
                    # looking at is not tinted.
                    cx_seen=0
                    [ "$fin" = done ] && viewed_now "$wac" && { fin=idle; cx_seen=1; }
                    cx_cmd="set-option -w -t $win @agent_state $fin ; set-option -w -t $win @agent_since '$tick_now $fin' ; set-option -uw -t $win @agent_pending ; set-option -uw -t $win @agent_ask_bg"
                    [ "$cx_seen" = 1 ] && cx_cmd+=" ; set-option -w -t $win @agent_seen_at $tick_now"
                    if [ "$cx_mark" = complete ]; then
                        cx_cmd+=" ; set-option -w -t $win @agent_detail_kind done ; set-option -w -t $win @agent_detail 'turn finished'"
                    else
                        cx_cmd+=" ; set-option -uw -t $win @agent_detail_kind ; set-option -uw -t $win @agent_detail"
                    fi
                    cx_cmd+="$lim_cmd"
                    # CONDITIONAL WRITES: only over the state this was decided from.
                    cas_was="$state"
                    cas_write "$win" "#{==:#{@agent_state},$cas_was}" "$cx_cmd" \
                        && [ "$state" != "$cas_was" ] && { changed=1; state_moved=1; }
                    fin="$state"
                else
                    # The bg-pending release lands here (and in prior_restore):
                    # no background work is out any more, so nobody owns any
                    # (@agent_bg_owner, AGENT-ORIGINATED TURNS). Same call,
                    # and only over the `running` it was decided from
                    # (CONDITIONAL WRITES) - never over a gate that just landed.
                    cas_write "$win" "#{==:#{@agent_state},running}" \
                        "set-option -w -t $win @agent_state idle ; set-option -uw -t $win @agent_bg_owner$lim_cmd" \
                        && [ "$state" = idle ] && { changed=1; state_moved=1; }
                    fin="$state"
                fi
                state=$fin
            else
                idle_streak_next="${idle_streak_next}${win}=${n} "
            fi
        fi

        if [ "$has_agent" = 1 ] && [ -z "$state" ]; then
            # The seed never lands over a first hook's state (CONDITIONAL
            # WRITES); state is whatever the window holds after it.
            cas_write "$win" "#{==:#{@agent_state},}" "set-option -w -t $win @agent_state idle" \
                && [ "$state" = idle ] && { changed=1; state_moved=1; }
        elif [ "$has_agent" = 0 ] && [ "$birth_hold" = 0 ] \
             && { [ -n "$state" ] || [ "$has_summary" = 1 ] \
                  || [ "$wf$cua" != 00 ] || [ -n "$born" ]; }; then
            # The held @agent_workflow / @agent_cua (see NO AGENT PANE above)
            # ride this streak too and go with the rest, as does an expired
            # birth mark (BIRTH GRACE).
            n=0
            for kv in $gc_streak; do
                case "$kv" in "${win}="*) n="${kv#*=}"; break ;; esac
            done
            n=$((n + 1))
            if [ "$n" -ge "$GC_TICKS" ]; then
                if [ "$wf$cua" != 00 ]; then
                    tmux set-option -uw -t "$win" @agent_workflow \; \
                         set-option -uw -t "$win" @agent_cua 2>/dev/null
                    wf=0; cua=0
                fi
                tmux set-option -uw -t "$win" @agent_state 2>/dev/null
                tmux set-option -uw -t "$win" @agent_summary 2>/dev/null
                tmux set-option -uw -t "$win" @agent_summary_cond 2>/dev/null
                # The condenser's paint() matches its key against this one, as
                # clear_state's unset makes it: left set, a worker still
                # running for the dead agent painted its title on the shell.
                tmux set-option -uw -t "$win" @agent_summary_key 2>/dev/null
                tmux set-option -uw -t "$win" @agent_pending 2>/dev/null
                tmux set-option -uw -t "$win" @agent_rollout 2>/dev/null
                tmux set-option -uw -t "$win" @agent_session_id 2>/dev/null
                tmux set-option -uw -t "$win" @agent_owner_token 2>/dev/null
                tmux set-option -uw -t "$win" @agent_kind \; \
                     set-option -uw -t "$win" @agent_detail_kind \; \
                     set-option -uw -t "$win" @agent_detail \; \
                     set-option -uw -t "$win" @agent_born \; \
                     set-option -uw -t "$win" @agent_ask_from \; \
                     set-option -uw -t "$win" @agent_ask_bg \; \
                     set-option -uw -t "$win" @agent_ask_seen \; \
                     set-option -uw -t "$win" @agent_held_detail 2>/dev/null
                # The agent-origin bookkeeping goes with the session (one
                # call), as clear_state drops it.
                gc_cmd=()
                for o in "${ORIGIN_OPTS[@]}"; do
                    gc_cmd+=(${gc_cmd[0]+\;} set-option -uw -t "$win" "$o")
                done
                tmux "${gc_cmd[@]}" 2>/dev/null
                # A discharge, as clear_state's: the stamp outlives the unsets.
                if [ -n "$state" ]; then
                    tmux set-option -w -t "$win" @agent_seen_at "$tick_now" 2>/dev/null
                    state_moved=1
                fi
                changed=1
                state=""; pending=""
            else
                gc_streak_next="${gc_streak_next}${win}=${n} "
            fi
        fi

        [ "$state" = running ] && blink_active=1

        # @agent_since = "<epoch> <state>": when this window's state last
        # changed (the roster's elapsed column and the jump order read it).
        # The hook's set_state stamps its own transitions at write time (a
        # tick has taken 4-6s, so a done->running->done inside one tick would
        # otherwise keep the old stamp) and clear_state unsets it. This is
        # the BACKSTOP for everyone else: the seen-it discharge, the
        # stuck-running reconcile, the idle seed and the GC all change state,
        # and this one comparison against the stored value sees every one of
        # them within a tick (a hook stamp that already matches the live
        # state is left alone). It is
        # compared with what is STORED, not with last tick's memory, so a
        # `prefix r` or an ensure_watcher respawn neither restamps every
        # window nor loses a change made while no watcher was running. Up to
        # POLL_SECONDS late, which an elapsed column can't show anyway. Not a
        # rendered option, so it never sets `changed`.
        #
        # @agent_kind for a window whose agent has fired no hook yet. Codex's
        # first hook (SessionStart) waits for the first PROMPT, so a freshly
        # launched codex sat seeded `idle` with no kind - no ⬢ glyph in the
        # sidebar or menu - until then. The agent's own comm (PID_KIND) says
        # which it is, so a live agent window with no kind gets it here, riding
        # in the stamp's call: the idle seed always stamps, so a fresh window
        # pays no extra fork; a held state with no kind (rare: a seed from
        # before this existed) pays one call, once. A kind already set - always
        # by a hook - is never overwritten, and none is written without a live
        # agent, so the GC (which unsets it with the state) never races this.
        kind_cmd=()
        if [ -n "$state" ] && [ "$has_agent" = 1 ] && [ -z "${W_AK[$win]:-}" ] \
           && [ -n "${PID_KIND[$pid]:-}" ]; then
            kind_cmd=(set-option -w -t "$win" @agent_kind "${PID_KIND[$pid]}")
        fi
        if [ -z "$state" ]; then
            [ -n "$since" ] && tmux set-option -uw -t "$win" @agent_since 2>/dev/null
        elif [ "${since#* }" != "$state" ]; then
            tmux set-option -w -t "$win" @agent_since "$tick_now $state" \
                ${kind_cmd[0]+\;} "${kind_cmd[@]}" 2>/dev/null
        elif [ "${#kind_cmd[@]}" -gt 0 ]; then
            tmux "${kind_cmd[@]}" 2>/dev/null
        fi
        # EXPIRED PENDING. idle + @agent_pending holds the window's slot only
        # while the stamp is an epoch no older than SLOT_PENDING_TTL (600 s):
        # an approval answered with No/Esc fires no resume hook, so nothing
        # else would ever clear it. Past that (or junk) it is unset here,
        # once - it must not linger for the heartbeat's own 3600 s gate, which
        # it now never reaches. (A junk stamp's unset is not atomic with a
        # hook; an epoch's is, below.)
        #   A TURN STILL GOING (2026-10-22). The stamp also outlives a YES:
        # the approved tool - a 12-minute build - fires its PostToolUse only
        # when it finishes, so the heartbeat that would re-arm finds bare idle
        # and the rest of the turn read idle (no pulse, slot released) until
        # its Stop. So an expiring stamp whose agent's own record says busy
        # (Claude's session file, the OpenCode plugin's status; codex has no
        # such record) becomes `running` instead, the way the heartbeat would
        # have made it; a turn that is over idles it again through the stuck-
        # running reconcile above. Both writes are conditional on the window
        # still holding this idle and this stamp (CONDITIONAL WRITES).
        if [ "$state" = idle ] && [ -n "$pending" ] && ! slot_pending_fresh "$pending"; then
            st=""
            if [ "$has_agent" = 1 ] && [ -n "$pid" ]; then
                case "${PID_KIND[$pid]:-}" in
                    opencode) opencode_status "$pid" ;;
                    codex) ;;
                    *) session_status "$pid" ;;
                esac
            fi
            if [[ $pending =~ ^[0-9]{1,18}$ ]]; then
                cas_c="set-option -uw -t $win @agent_pending"
                [ "$st" = busy ] \
                    && cas_c+=" ; set-option -w -t $win @agent_state running ; set-option -w -t $win @agent_since '$tick_now running'"
                cas_write "$win" "#{&&:#{==:#{@agent_state},idle},#{==:#{@agent_pending},$pending}}" "$cas_c" \
                    && [ "$state" = running ] && { changed=1; state_moved=1; blink_active=1; }
            else
                tmux set-option -uw -t "$win" @agent_pending 2>/dev/null
                pending=""
            fi
        fi
        # Slot eligibility (slot_active), from the window as this tick
        # LEAVES it: state, pending, and wf/cua - exactly what the flags now
        # hold (held while the pane is missing, dropped by the GC).
        slot_active "$state" "$pending" "${wf/0/}" "${cua/0/}" && S_ELIG[$win]=1
    done
    gc_streak="$gc_streak_next"
    # This tick's reads become the next tick's "previous" (a window gone from
    # the read, or now stateless, just drops out). The restart seed is done.
    EV_PREV=()
    for win in "${!EV_NEXT[@]}"; do EV_PREV[$win]="${EV_NEXT[$win]}"; done
    ev_base=1
    EV_SEED=(); EV_SEED_AT=()

    idle_streak="$idle_streak_next"
    # Rollout answers no window asked for this tick are dropped (see
    # codex_tail_cached); a window that comes back reads afresh.
    for win in "${!CX_PATH[@]}"; do
        [ -n "${CX_USED[$win]:-}" ] && continue
        unset 'CX_PATH[$win]' 'CX_TICK[$win]' 'CX_MARK[$win]' 'CX_TS[$win]'
    done
    CX_USED=()

    # The stale-save chip (see STALE-SAVE CHIP): a builtin clock compare on
    # almost every tick, a look at most once per STALE_EVERY seconds.
    check_stale_save

    # Pulse while anything is running (set in the loop), has a workflow in
    # flight, or is driving an app. The toggling itself is blink_loop's job;
    # this only raises/lowers its flag (no fork unless it actually falls).
    [ "$any_workflow" = 1 ] && blink_active=1
    [ "$any_cua" = 1 ] && blink_active=1
    if [ "$blink_active" = 1 ]; then
        [ -e "$BLINK_FLAG" ] || : > "$BLINK_FLAG"
    elif [ -e "$BLINK_FLAG" ]; then
        rm -f "$BLINK_FLAG"
    fi

    # Agent slots (see AGENT SLOTS). Decided from this tick's read, fork-free;
    # only when that says something must change: under the slot lock, one
    # fresh list-windows -a (a hook may have stamped since our read), decide
    # again from it, write in one call, unlock. A lock not won in ~1 s skips
    # this tick's writes (the next tick tries again).
    reconcile_slots
    if [ "${#slot_cmd[@]}" -gt 0 ] && slot_lock; then
        if slot_rows=$(tmux list-windows -a -F "$SLOT_FMT" 2>/dev/null); then
            slot_fold "$slot_rows"
            reconcile_slots
            [ "${#slot_cmd[@]}" -gt 0 ] && tmux "${slot_cmd[@]}" 2>/dev/null
        fi
        slot_unlock
    fi
    # Its own call, after the slots: a set on a window closed since the read
    # fails, and a failure ends the rest of a command list - the redraw for
    # this tick's state changes must not ride behind it. (Nor in front: with
    # no client attached refresh-client itself fails.)
    if [ "$changed" = 1 ]; then
        tmux refresh-client -S 2>/dev/null
    fi
    # After every write of the tick, @agent_since stamps included, so a reader
    # woken by it sees the whole tick (STATE NOTICE, agent-tab-indicator.sh).
    [ "$state_moved" = 1 ] && notify_state
    # RATE-LIMITED AGENTS: the resume trigger (limit_trigger), after this
    # tick's writes so a limit-tick it spawns reads them.
    limit_trigger

    # HEARTBEAT. Being alive is not the same as turning: every check on this
    # daemon (the pidfile, ensure_watcher's kill -0 + ps) proves a PROCESS
    # exists, and none of them prove the LOOP is still going round. A tmux
    # call or the python in live_cua_pids wedging would leave a healthy-
    # looking watcher that has quietly stopped reconciling, and since the
    # working chip went pink↔blue a frozen blink renders as plain blue —
    # i.e. identical to idle, so the surface lies rather than merely going
    # quiet. Restamping the pidfile each tick makes the mtime a liveness
    # clock that ensure_watcher can test. The pulse child reads this file
    # every second, so it is never truncated: already ours → identical bytes
    # rewritten in place (restamp_pidfile, builtin, no fork); empty/missing →
    # re-claimed with the atomic temp+mv (write_pidfile, rare).
    #
    # Reading it back first also settles a race the startup guard can't: if
    # a newer instance has claimed the file, WE are the stale one and should
    # go, rather than both of us toggling @agent_blink and cancelling out.
    # Only a non-empty foreign pid counts as a successor.
    _owner=""
    read -r _owner < "$PIDFILE" 2>/dev/null
    if [ "$_owner" = "$$" ]; then
        restamp_pidfile
    elif [ -z "$_owner" ]; then
        write_pidfile
    else
        exit 0
    fi

    # PULSE CHILD WATCHDOG. Still ours past the check above, so the child has
    # no reason to be gone - but if it is (killed, or it misread the pidfile
    # under an older build), nothing else would ever restart it and the pulse
    # would sit frozen. kill -0 is a builtin; a pid can only be reused after
    # bash reaps it, and a reaped child fails this check within a second, long
    # before a ~15-minute pid wrap could hand that number to a stranger.
    if [ -z "${BLINK_PID:-}" ] || ! kill -0 "$BLINK_PID" 2>/dev/null; then
        BLINK_PID=""
        blink_loop &
        BLINK_PID=$!
    fi

    # Tick trace line: "<epoch> <tick_ms> <windows> <agents>". Digits-only
    # microseconds (EPOCHREALTIME's radix follows the locale: "." or ","),
    # and the file is re-tested so an `rm` mid-tick isn't undone by >>.
    if [ -n "$trace_t0" ] && [ -e "$TRACE" ]; then
        trace_t1=$EPOCHREALTIME
        printf '%s %d %d %d\n' "${trace_t0%%[.,]*}" \
            $(( (${trace_t1//[!0-9]/} - ${trace_t0//[!0-9]/}) / 1000 )) \
            "$n_windows" "$n_agents" >> "$TRACE" 2>/dev/null
    fi

    if [ -n "$MAX_TICKS" ]; then
        MAX_TICKS=$((MAX_TICKS - 1))
        [ "$MAX_TICKS" -gt 0 ] || exit 0
    fi
    sleep "$POLL_SECONDS"
done
