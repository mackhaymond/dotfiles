#!/usr/bin/env bash
# Agent tab indicator v2 — drive per-window tmux options from AI-agent
# lifecycle hooks so the Catppuccin tab bar reflects agent state.
#
# Options written (window scope; unset = no agent in window):
#   @agent_state    idle | running | needs-input | failed | done | paused
#                   (paused: stopped by the pause key while @agent_paused is
#                   set - see AGENT-ORIGINATED TURNS and docs/agent-pause.md)
#                   REPALETTED 2026-08-21 (Mack's call): yellow means
#                   SOMETHING IS WAITING ON YOU — approvals and questions
#                   alike, one state — and red means SOMETHING BROKE. Red used
#                   to mean "wants your consent", which fired many times an
#                   hour; a color that constant is wallpaper, not an alarm.
#                   Failures are rare, so red is worth something again.
#                   It also deleted the hardest thing here: approvals and
#                   questions come through the same events and only one of the
#                   two events describing a gate can name which it is, which
#                   painted a question red twice in two days. Nothing left to
#                   tell apart. `needs-approval` is retired as a STATE (it
#                   survives only as the mode name settings.json passes).
#                   Both tinted states want dark text — match them together,
#                   never by listing one.
#   @agent_summary  "<project>/<ultra-short title>" shown as the tab name;
#                   project = basename of the agent's cwd, title = the
#                   conversation title condensed to its 2-4 identifying
#                   words by a cached background copilot/haiku call
#                   (interim: the raw title until the condensation lands)
#   @agent_summary_cond  1 while @agent_summary holds a condensed label, unset
#                   while it holds raw stand-in text — see compose_summary
#   @agent_summary_key  the condense key the label should come from; a
#                   detached worker paints only while it is still its own —
#                   see compose_summary's SUMMARY KEY
#   @agent_pending  epoch stamp: a needs-input window the user focused (went
#                   to answer its prompt). The next heartbeat consumes it to
#                   restore `running` when the answered turn resumes; every
#                   other lifecycle mode clears it so it can't go stale.
#   @agent_ask_from WHO the window's needs-input is waiting for: the
#                   PermissionRequest payload's agent_id (a subagent's gate)
#                   or "main". Written with needs-input by the
#                   PermissionRequest (never its Notification twin), unset
#                   in the same command list by any set_state that leaves
#                   needs-input (and by clear_state / the watcher's
#                   discharge and GC). Read by the heartbeat - a tool call
#                   by anyone else must not re-arm `running` over a gate
#                   still on screen - and by Stop, which leaves a subagent's
#                   gate standing (ASKER GATE, see heartbeat and done).
#                   Unset while needs-input = no asker: the old rule, any
#                   tool call re-arms.
#   @agent_ask_bg   "1": this needs-input is a turn that ENDED asking Mack
#                   while its background work is still out (ASKS WHILE WORK
#                   IS OUT, in done). Written with that needs-input (asker
#                   "main", detail kind ask), unset by every write that
#                   leaves needs-input (set_state, clear_state, the
#                   watcher's discharge and GC). A look while the work is
#                   still out discharges it to running, not idle.
#   @agent_kind    claude | codex | opencode — which agent's hooks drive this
#                   window; written with any state/detail write a hook makes
#   @agent_detail_kind  perm | ask | fail | done | run — what the last event
#                   was about (see DETAIL_JQ); @agent_detail = one sanitized
#                   line for it (≤80 chars). Both written in set_state's
#                   command list, unset by SessionStart and clear_state, never
#                   touched by the heartbeat. Not rendered in the tab bar —
#                   they feed the sidebar (`list-windows -F '#{@agent_detail}'`).
#   @agent_born     epoch: set (with idle, kind, since and a "New Session"
#                   summary) by agent-birth.conf in the new-window that opens
#                   a claude tab, so the tab is an agent before claude runs.
#                   The watcher honours it as a grace and drops it once the
#                   agent shows up; clear_state unsets it.
#   @agent_limit    "<resets_at> <type> <since>": the window is RATE-LIMITED
#                   (a paused agent with a reason; RATE-LIMITED AGENTS below).
#   @agent_limit_lifted  "<epoch>": that limit has passed, the agent waits
#                   for Mack (written by agent-pause.py's limit-tick).
#   @agent_slot     the window's sticky agent number (agent-ui's number keys).
#                   Stamped by set_state, under the slot lock and in the
#                   state's own tmux call, when a window ENTERS the Active
#                   set without a valid one; released and repaired only by
#                   agent-tab-watcher.sh. Rules: SLOT CORE (shared verbatim
#                   with the watcher) and docs/agent-tab-indicator.md.
#
# Rendering happens entirely in tmux.conf: the Catppuccin window formats
# read these options via #{?…} conditionals (background tint per state,
# glyph, summary-with-#W-fallback). Catppuccin bakes its window formats
# ONCE at load from GLOBAL options, so per-window @catppuccin_* overrides
# can't work — per-window state must flow through user options like these.
#
# Callers:
#   Claude Code hooks (~/.claude/settings.json) and Codex hooks
#   (~/.codex/hooks.json) invoke:  agent-tab-indicator.sh <mode> <agent>
#   with the hook's JSON payload on stdin. Claude uses inherited terminal
#   identity; Codex resolves a verified frontend/thread binding because its
#   hooks may run in a shared daemon. OpenCode has no hooks: its plugin
#   (~/.config/opencode/plugins/tmux-agent-state.js) runs inside the TUI's
#   own process and calls this with `<mode> opencode` and a Claude-shaped
#   payload, using the same inherited TMUX_PANE identity as Claude.
#
#   tmux after-select-window hook invokes:  agent-tab-indicator.sh clear-current
#   (no stdin, no TMUX_PANE → operates on the now-active window).
#
#   agent-tab-watcher.sh (companion daemon) seeds `idle` for hook-less
#   agents and garbage-collects state when the agent process dies — this
#   script never has to handle crashed/killed agents.
#
# Modes:
#   idle         SessionStart        → mark present (skipped for compact
#                                      restarts); a fresh session with no title
#                                      yet shows "<project>/New Session"
#   running      UserPromptSubmit    → turn started; claude: topic-drift check
#                                      (agent-topic-drift.awk); refresh
#                                      @agent_summary
#   heartbeat    PostToolUse         → re-arm running mid-turn: from failed
#                                      only for a main-agent tool call, from
#                                      needs-input only when the tool call is
#                                      the asker's own (@agent_ask_from), or
#                                      from idle when
#                                      @agent_pending marks an answered
#                                      permission prompt — never from bare
#                                      idle/done, so a late tool call can't
#                                      resurrect a finished tab; never parses
#                                      stdin — payloads can be huge (only
#                                      needs-input and failed peek at its
#                                      first 8 KB)
#   needs-approval  PermissionRequest, and Notification(permission_prompt)
#                → sets needs-input (YELLOW). Both events describe the SAME
#                                      gate: PermissionRequest is structural
#                                      (always fires, carries the tool name),
#                                      the Notification is the "look over
#                                      here" that Claude suppresses while the
#                                      terminal is focused, and it names
#                                      nothing. That asymmetry is why sorting
#                                      approvals from questions kept failing,
#                                      and why they no longer are: one state,
#                                      and the tab name says which session.
#   subagent-stop  SubagentStop        → the asking subagent's end ends its
#                                      gate (ASKER GATE); else inert
#   failed       StopFailure         → the TURN ITSELF died (529, overloaded).
#                                      Red. Nothing is waiting on an answer;
#                                      something broke. Asserted like the
#                                      other attention state (focus ≠ fixed)
#                                      and discharged by the focus hook.
#                                      EXCEPT error == rate_limit (a usage
#                                      limit): `paused` + @agent_limit, see
#                                      RATE-LIMITED AGENTS.
#   done         Stop                → turn finished; refresh @agent_summary;
#                                      not tinted if a client is watching it;
#                                      a turn ANOTHER AGENT started finishes
#                                      quiet (AGENT-ORIGINATED TURNS below);
#                                      a SUBAGENT's open gate stands through
#                                      it (ASKER GATE)
#   clear        SessionEnd          → remove state (skipped for clear/resume,
#                                      which are followed by a new SessionStart)
#   clear-current  focus hook        → attention states → idle once seen;
#                                      needs-input also stamps @agent_pending
#                                      so the answered turn resumes as running
#
# "Seen-it" semantics: a tinted tab discharges to idle when you focus it
# (clear-current); `done` additionally isn't tinted if its window is already
# being watched. needs-input and failed always tint, so neither a prompt nor
# a dead turn is ever lost.
# Answering a permission prompt usually REQUIRES focusing the window, and the
# focus discharge destroys needs-input before any heartbeat can re-arm from
# it — so heartbeat's needs-input path alone left the tab idle for the rest
# of the turn (user-reported desync). The @agent_pending stamp bridges the
# gap: focus-discharge of needs-input stamps it, the next heartbeat consumes
# it and restores running.

set -euo pipefail

command -v tmux >/dev/null 2>&1 || exit 0

mode="${1:-}"
agent="${2:-}"
# UserPromptSubmit's stdout is CONTEXT for the model (Claude adds plain text
# to the prompt; both Claude and Codex read hookSpecificOutput JSON). The only
# thing this hook may ever print there is the agent-origin additionalContext
# (see AGENT-ORIGINATED TURNS), written explicitly to fd 3. Everything else -
# a stray tmux message, a helper's echo - goes to /dev/null.
if [ "$mode" = running ]; then
    exec 3>&1 1>/dev/null
fi
# The sidebar detail the next set_state writes (see set_state/take_detail).
dkind=""
dtext=""
# set_state's @agent_pending op ("set" | "unset" | "") and its "wrote" flag.
pend_op=""
ss_wrote=0
# set_state's discharge flag: 1 stamps @agent_seen_at (see set_state).
seen_op=""
# set_state's @agent_since override ("<epoch> <state>", used only when the
# state changes; a quiet finish restores the stamp the window had) and extra
# tmux commands to run in the SAME command list (ss_add). Both consumed.
ss_since=""
ss_extra=()
# owner_current's answer for this process ("" = not asked yet, 1, 0). See
# owner_current.
OWNER_MEMO=""

JQ="$(command -v jq || true)"
# Codex hooks run in a shared app-server. Its inherited TMUX_PANE belongs to
# whoever originally started the daemon, not necessarily this conversation.
# Resolve the exact client/thread binding before any tmux reads or mutations.
payload=""
if [ "$agent" = codex ]; then
    [ -n "$JQ" ] || exit 0
    payload=$(cat 2>/dev/null || true)
    [ -z "$("$JQ" -r '.agent_id // empty' <<<"$payload" 2>/dev/null || true)" ] || exit 0
    owner_sid=$("$JQ" -r '.session_id // empty' <<<"$payload" 2>/dev/null || true)
    owner=$("$HOME/.local/bin/codex-terminal-owner" resolve "$owner_sid" 2>/dev/null || true)
    [ "$("$JQ" -r '.status // empty' <<<"$owner" 2>/dev/null || true)" = bound ] || exit 0
    export AGENT_TAB_SOCKET=$("$JQ" -r '.tmux_socket' <<<"$owner")
    export TMUX_PANE=$("$JQ" -r '.pane' <<<"$owner")
    export TMUX="$AGENT_TAB_SOCKET,0,0"
    export AGENT_TAB_OWNER_SESSION="$owner_sid"
    export AGENT_TAB_OWNER_TOKEN=$("$JQ" -r '.token' <<<"$owner")
    export AGENT_TAB_OWNER_BINDING=$("$JQ" -r '.binding_id' <<<"$owner")
    # The resolved record itself, for the detached condenser (owner_current).
    export AGENT_TAB_OWNER_RECORD=$("$JQ" -c . <<<"$owner")
    reconcile_binding=$("$JQ" -r '.terminal_binding_id // empty' <<<"$payload" 2>/dev/null || true)
    [ -z "$reconcile_binding" ] || [ "$reconcile_binding" = "$AGENT_TAB_OWNER_BINDING" ] || exit 0
    # This resolve IS owner_current's answer for this hook: the binding and
    # token it would compare were just copied out of this same bound record.
    # Re-resolving cost ~150 ms each (python + a ps ancestor walk + tmux), and
    # a no-op heartbeat paid it three times, on every codex tool call, which
    # codex blocks on (2026-10-22).
    OWNER_MEMO=1
fi
# A FOREIGN AGENT'S TOOL SHELL (2026-10-22). A claude (or opencode) started by
# another agent's tool call - codex's shell running `claude -p "..."` -
# inherits that agent's TMUX_PANE, and its hooks then drive the PARENT's tab:
# SessionStart repainted codex's running window as an idle claude "New
# Session" and dropped its owner binding, SessionEnd's clear_state wiped it,
# and codex's next hook (sid mismatch) cleared it again - the codex turn read
# idle until its Stop. Codex marks its tool shells with these two; the
# OpenCode plugin's PARENT_AGENT_ENV skips on the same pair (not on
# AGENT_PTY: a pty-MCP shell owns its own pane, and a headless claude there
# paints it). Claude-in-Claude is caught after the pane lookup (nested_agent).
case "$agent" in
    claude|opencode) [ -z "${CODEX_THREAD_ID:-}${CODEX_SANDBOX:-}" ] || exit 0 ;;
esac
[ -n "${TMUX:-}" ] || exit 0

# Detached condensers inherit the explicit socket too; a pane/window id by
# itself is not unique across tmux servers.
tmux() {
    if [ -n "${AGENT_TAB_SOCKET:-}" ]; then
        command tmux -S "$AGENT_TAB_SOCKET" "$@"
    else
        command tmux "$@"
    fi
}

# STATE NOTICE. A write that changes a window's @agent_state posts the Darwin
# notification com.mackhaymond.tmux.agent-state (no payload; it means
# "re-read"). CuaNotch listens and re-checks its tmux rows against tmux, so a
# tab cleared here (a visit, an ⌥S landing, the watcher's discharge) clears
# in the notch at the same moment: for tmux rows, tmux's verdict wins. The
# watcher posts the same name. AGENT_STATE_NOTICE overrides the name; set it
# empty to post nothing (tests).
notify_state() {
    local name="${AGENT_STATE_NOTICE-com.mackhaymond.tmux.agent-state}"
    [ -n "$name" ] || return 0
    notifyutil -p "$name" >/dev/null 2>&1 || true
}

# owns_window, answered once per window per process: compose_summary asks
# before it writes anything (the SUMMARY KEY rides in tmux calls that bypass
# set_summary), and set_summary's own check then costs a codex hook nothing
# more. A process is one hook event, or one worker whose first ask is at
# paint time, so the answer can't go stale in between.
OWN_MEMO=""
owns_window_once() {
    case "$OWN_MEMO" in
        "$1:1") return 0 ;;
        "$1:0") return 1 ;;
    esac
    if owns_window "$1"; then OWN_MEMO="$1:1"; return 0; fi
    OWN_MEMO="$1:0"
    return 1
}

owns_window() {
    [ -z "${AGENT_TAB_OWNER_SESSION:-}" ] && return 0
    [ "$(tmux show-options -wqv -t "$1" @agent_session_id 2>/dev/null)" = "$AGENT_TAB_OWNER_SESSION" ] &&
        [ "$(tmux show-options -wqv -t "$1" @agent_owner_token 2>/dev/null)" = "$AGENT_TAB_OWNER_TOKEN" ] &&
        owner_current
}

# Memoized per process like owns_window_once (OWNER_MEMO; the codex hook seeds
# it from its own resolve): one hook event is one answer, and the detached
# condenser's first ask is at paint time.
owner_current() {
    [ -z "${AGENT_TAB_OWNER_SESSION:-}" ] && return 0
    case "$OWNER_MEMO" in
        1) return 0 ;;
        0) return 1 ;;
    esac
    if owner_resolve; then OWNER_MEMO=1; return 0; fi
    OWNER_MEMO=0
    return 1
}

owner_resolve() {
    local current status
    current=$("$HOME/.local/bin/codex-terminal-owner" resolve "$AGENT_TAB_OWNER_SESSION" 2>/dev/null || true)
    status=$("$JQ" -r '.status // empty' <<<"$current" 2>/dev/null || true)
    if [ "$status" = bound ]; then
        [ "$("$JQ" -r '.binding_id // empty' <<<"$current" 2>/dev/null)" = "$AGENT_TAB_OWNER_BINDING" ] &&
            [ "$("$JQ" -r '.token // empty' <<<"$current" 2>/dev/null)" = "$AGENT_TAB_OWNER_TOKEN" ]
        return
    fi
    # A direct binding (direct_owner) is found by walking up from the hook to
    # its Codex frontend. The detached condenser is a tmux-server child with
    # no such ancestor, so resolve says unbound there and every condensed
    # title was dropped. It carries the record the hook resolved instead:
    # `valid` re-derives the token from that frontend's pid+start, socket and
    # pane and checks the process is still alive on that pane's tty, so a
    # stale condenser cannot vouch for a window another session now owns.
    case "$mode" in condense-locked) ;; *) return 1 ;; esac
    [ -n "${AGENT_TAB_OWNER_RECORD:-}" ] || return 1
    "$HOME/.local/bin/codex-terminal-owner" valid "$AGENT_TAB_OWNER_SESSION" "$AGENT_TAB_OWNER_TOKEN" \
        <<<"$AGENT_TAB_OWNER_RECORD" >/dev/null 2>&1
}
# Shared with CuaNotch's cua-notch-agent-hook: the one implementation of "is
# background work from this session still out?". See its header.
BG_PENDING="$HOME/.local/bin/agent-bg-pending"
# ...and of "does the turn that just ended ask Mack for something?" (cua-notch
# 547f0e7; its header has the contract). See ASKS WHILE WORK IS OUT in done.
TURN_ASKS="$HOME/.local/bin/agent-turn-asks"
# The Stop hook's synchronous --marker-only call: SIGTERMed past this (s).
# The ONLY call: there is no detached full (model) run any more (EXPLICIT
# ONLY, in ASKS WHILE WORK IS OUT).
ASK_SYNC_BUDGET=0.3

# Watchdog for the companion daemon. agent-tab-watcher.sh is spawned exactly
# once, from tmux.conf, so if it ever dies mid-session (stray pkill, OOM, a
# tmux hiccup) everything it drives silently stops — no blink (the glyph
# freezes on the @agent_blink=unset color), no workflow gear, no presence
# seeding, no GC — until the next `prefix r`, and nothing surfaces the failure.
# Hooks fire constantly, so re-assert it here. Respawn goes through the tmux
# server so the daemon outlives this short-lived hook process. PIDFILE path
# must match the watcher's.
#
# NO FORKS WHEN HEALTHY (2026-10-22). The checks below cost a ps, a date and a
# stat on every hook - every tool call - where this header used to promise
# none. Only a TURNING watcher restamps its pidfile (each tick, and never over
# a successor's), so "the pidfile changed after a moment we know is under 30 s
# ago" proves the whole chain - alive, ours, not wedged - with builtins:
# $SEEN holds the epoch it was last written at (by the slow path below, once
# it has proved a beat), and `-nt` compares the two mtimes. Past 30 s, or
# with no tick since, the slow path runs exactly as before and rewrites the
# mark. So a busy turn pays the forks about once per 30 s, not per tool call.
ensure_watcher() {
    local pidfile seen_mark seen="" pid="" now beat
    pidfile="${TMPDIR:-/tmp}/agent-tab-watcher.${UID:-$(id -u)}.pid"
    seen_mark="${pidfile%.pid}.hookseen"
    printf -v now '%(%s)T' -1 2>/dev/null || now=$(date +%s 2>/dev/null || echo 0)
    read -r seen < "$seen_mark" 2>/dev/null || seen=""
    case "$seen" in ''|*[!0-9]*) seen=0 ;; esac
    if [ $((now - seen)) -lt 30 ] && [ "$pidfile" -nt "$seen_mark" ]; then
        return 0
    fi
    if [ -f "$pidfile" ]; then
        read -r pid < "$pidfile" 2>/dev/null || pid=""
    fi
    # `kill -0` alone answers "some process exists", not "the watcher is
    # alive": the daemon only unlinks its pidfile on a clean EXIT, and pids
    # here wrap in ~15 min, so a stale file usually points at an unrelated
    # live process. That reads as HEALTHY and the watchdog never fires —
    # the one failure it exists to catch, silently. Confirm identity before
    # believing it (one ps, on the slow path only - see NO FORKS WHEN HEALTHY).
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        case "$(ps -o command= -p "$pid" 2>/dev/null)" in
            *"/agent-tab-watcher.sh")
                # ...and that it is still TURNING, not merely resident. The
                # daemon restamps this file every tick, so a stale mtime means
                # a wedged loop: alive to every liveness test we have, but no
                # longer reconciling, and invisible because a frozen blink now
                # renders as plain blue (= idle). 30s is 30 ticks of grace.
                # Kill it so the respawn below isn't refused by the singleton.
                beat=$(stat -f %m "$pidfile" 2>/dev/null || echo 0)
                if [ $((now - beat)) -lt 30 ]; then
                    # The mark for the fast path: written now, so a pidfile
                    # newer than it is a beat inside the next 30 s.
                    printf '%s\n' "$now" 2>/dev/null > "$seen_mark" || true
                    return 0
                fi
                # TERM then CONT: a wedge that is STOPPED rather than blocked
                # leaves the TERM merely pending, so it would sit there
                # holding the singleton while the respawn piles a second
                # daemon on top — two of them toggle @agent_blink per tick
                # and cancel out, which is the exact symptom this file exists
                # to avoid. CONT wakes it to process the pending signal.
                kill "$pid" 2>/dev/null || true
                kill -CONT "$pid" 2>/dev/null || true
                ;;
        esac
    fi
    tmux run-shell -b "bash $HOME/.config/tmux/scripts/agent-tab-watcher.sh" 2>/dev/null || true
}
ensure_watcher

# ------------------------------------------------------------ agent slots
# set_state STAMPS a window's @agent_slot the moment it puts the window in
# the Active set (see SLOT CORE and slot_stamp), so a new agent has its
# number before the watcher's next tick. The core needs bash 4+: under an
# older bash the hook leaves all of it to the watcher's tick.
SLOT_OK=0
slot_locked=0
if [ "${BASH_VERSINFO[0]:-0}" -ge 4 ]; then
SLOT_OK=1
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
fi

# ---------------------------------------------------------------- helpers

window_state() {
    tmux show-options -wqv -t "$1" @agent_state 2>/dev/null || true
}

# Is the user actually LOOKING at the agent's window? $watched is its
# #{window_active_clients}, read by the caller (the hook body). A watched window in a WezTerm that is behind Slack is not being
# seen, and until 2026-08-25 `done` skipped its tint on the watched test
# alone - the one case where a finish could be missed with no colour ever
# shown. Now both: the window is active for a client AND WezTerm is
# frontmost. Two lsappinfo forks, ~11ms, and only on the events that would
# tint. Mack's ask: a yellow or green that lands while he is sitting on that
# tab with WezTerm focused clears at once.
watched=0
viewing_now() {
    [ "$watched" -gt 0 ] || return 1
    [ "$(lsappinfo info -only bundleid "$(lsappinfo front 2>/dev/null)" 2>/dev/null)" = \
      '"CFBundleIdentifier"="com.github.wez.wezterm"' ]
}


set_state() {
    # Write + redraw only on change; heartbeat fires on every tool call and
    # must not churn the status line. Writes are best-effort: the window can
    # close between the read and the write, and a failed write must not make
    # the hook exit nonzero (codex treats hook exit status as a gate).
    #
    # @agent_since ("<epoch> <state>", the roster's elapsed column and the
    # jump order) is stamped HERE, at the transition, not only by the
    # watcher: the watcher compares once per tick, and a tick can take 4-6s,
    # so done->running->done inside one tick kept the OLD stamp. The watcher
    # still stamps as the backstop for writers that bypass this function; a
    # stamp written here already matches the live state, so it leaves it be.
    # Change-only like the state write, so the no-op heartbeat stays fork-free.
    # ONE tmux invocation for both: the server runs a command list as a unit,
    # so the watcher's snapshot can't land between them and restamp the new
    # state against a stale stamp (and it saves a fork).
    #
    # The globals dkind/dtext (set by take_detail, or dkind=- to unset) are
    # the @agent_detail_kind / @agent_detail for the event that caused this
    # transition; empty dkind leaves them. CONSUMED here (reset on entry), so
    # a detail is written by exactly the one set_state its mode meant it for.
    # Globals rather than arguments so every call site keeps the plain
    # `set_state "$win" <state>` shape — cua-notch's dev/check-invariants pins
    # `        set_state "$win" failed` verbatim (its failure-state agree).
    # They ride in the SAME command list, unconditionally (an unchanged state
    # can still carry a new detail: a second gate, a new question), so a
    # reader never pairs a new state with the previous event's detail.
    # @agent_kind rides along on any write made by an agent hook. The
    # heartbeat sets no detail, so its no-change call stays fork-free.
    # The detail is never rendered by the status line: no refresh for it.
    #
    # pend_op (global, consumed like dkind): "set" stamps @agent_pending,
    # "unset" removes it, IN this command list. idle + pending holds the
    # window's agent slot, so the pair must never be seen half-written: a
    # stamp under the slot lock in between would read bare idle and give the
    # number away. ss_wrote=1 says this call reached the write (callers fall
    # back to their own pending write when it did not, as before).
    #
    # A change INTO the Active set stamps @agent_slot here (slot_stamp), in
    # the same command list, when the window has no valid one.
    #
    # seen_op (global, consumed like dkind): 1 = this write DISCHARGES the
    # window (the user saw it: viewing_now, clear-current), so it stamps
    # @agent_seen_at "<epoch>" in the same command list and posts the STATE
    # NOTICE even when the state itself was already idle (the hooks race).
    # CuaNotch's contract: never unset, always the latest discharge; it acks
    # a done/input row whose stamp is no earlier than 1 s before the row's.
    #
    # ss_since / ss_extra (globals, consumed like dkind): the AGENT-ORIGINATED
    # TURNS bookkeeping (@agent_turn_origin, @agent_turn_result, the
    # @agent_prior_* save/restore, @agent_inbound) rides in this same command
    # list, so the notch never reads a turn result ahead of the state it
    # describes, and a restored state never pairs with a fresh stamp.
    local win="$1" new="$2" dk="$dkind" dt="$dtext" po="$pend_op" so="$seen_op" cur ts
    local sv="$ss_since"
    local -a cmd=() ex=(${ss_extra[@]+"${ss_extra[@]}"})
    dkind=""; dtext=""; pend_op=""; seen_op=""; ss_wrote=0; ss_since=""; ss_extra=()
    cur=$(window_state "$win")
    # A call that would write nothing returns BEFORE the ownership check:
    # owns_window is two show-options plus (codex) an owner resolve, and an
    # unchanged running->running heartbeat must not pay for a no-op.
    [ "$cur" != "$new" ] || [ -n "$dk$po$so" ] || [ "${#ex[@]}" -gt 0 ] || return 0
    owns_window "$win" || return 0
    if [ "$cur" != "$new" ]; then
        printf -v ts '%(%s)T' -1 2>/dev/null || ts=$(date +%s)
        cmd=(set-option -w -t "$win" @agent_state "$new" \;
             set-option -w -t "$win" @agent_since "${sv:-$ts $new}")
        # Leaving needs-input answers its gate: who asked goes with it, in
        # the same command (ASKER GATE, heartbeat). One place, so every way
        # out (a prompt, a discharge, Stop, a failure, a restore) drops it.
        # So does @agent_ask_bg (ASKS WHILE WORK IS OUT): it only ever
        # describes the needs-input it was raised with.
        [ "$cur" != needs-input ] || cmd+=(\; set-option -uw -t "$win" @agent_ask_from \;
                                          set-option -uw -t "$win" @agent_ask_bg)
    fi
    if [ "$dk" = - ]; then
        cmd+=(${cmd[0]+\;} set-option -uw -t "$win" @agent_detail_kind \;
              set-option -uw -t "$win" @agent_detail)
    elif [ -n "$dk" ]; then
        cmd+=(${cmd[0]+\;} set-option -w -t "$win" @agent_detail_kind "$dk" \;
              set-option -w -t "$win" @agent_detail "$dt")
    fi
    local pts=""   # the pending stamp this call writes (slot_active reads it)
    case "$po" in
        set) printf -v pts '%(%s)T' -1 2>/dev/null || pts=$(date +%s)
             cmd+=(${cmd[0]+\;} set-option -w -t "$win" @agent_pending "$pts") ;;
        unset) cmd+=(${cmd[0]+\;} set-option -uw -t "$win" @agent_pending) ;;
    esac
    if [ "$so" = 1 ]; then
        [ -n "${ts:-}" ] || { printf -v ts '%(%s)T' -1 2>/dev/null || ts=$(date +%s); }
        cmd+=(${cmd[0]+\;} set-option -w -t "$win" @agent_seen_at "$ts")
    fi
    [ "${#ex[@]}" -gt 0 ] && cmd+=(${cmd[0]+\;} "${ex[@]}")
    [ "${#cmd[@]}" -gt 0 ] || return 0
    case "$agent" in
        claude|codex|opencode) cmd+=(\; set-option -w -t "$win" @agent_kind "$agent") ;;
    esac
    slot_locked=0
    if [ "$SLOT_OK" = 1 ] && { [ "$cur" != "$new" ] || [ "$po" = set ]; } \
       && slot_active "$new" "$pts" "" ""; then
        slot_stamp "$win"
    fi
    tmux "${cmd[@]}" 2>/dev/null || true
    if [ "$slot_locked" = 1 ]; then
        slot_unlock
        slot_locked=0
        trap - INT TERM HUP
    fi
    ss_wrote=1
    [ "$cur" = "$new" ] || tmux refresh-client -S 2>/dev/null || true
    { [ "$cur" != "$new" ] || [ "$so" = 1 ]; } && notify_state
    return 0
}

# slot_stamp WIN, from set_state: WIN is entering the Active set. Appends
# `@agent_slot N` (plus the unsets that move N off any window still carrying
# it stale) to set_state's cmd, so the number lands in the SAME tmux call as
# the state. A cheap look first, no lock, no list: a window that already has a
# valid slot keeps it (that is what makes it sticky), and one whose session is
# hidden is left to the watcher (which finds a visible link if it has one).
# Otherwise the slot lock (SLOT CORE) is taken and held until set_state has
# written: one list-windows -a, the lowest number no other active window
# holds. A lock not won in ~1 s skips the stamp; the watcher's tick does it.
slot_stamp() {
    local w="$1" pre s sess rows
    pre=$(tmux display-message -p -t "$w" "#{@agent_slot}${SLOT_US}#{session_name}" 2>/dev/null || true)
    s="${pre%%"$SLOT_US"*}"
    slot_valid "$s" && return 0
    sess=""
    case "$pre" in *"$SLOT_US"*) sess="${pre#*"$SLOT_US"}" ;; esac
    case "$SLOT_EXCLUDE" in *" $sess "*) return 0 ;; esac
    slot_lock || return 0
    slot_locked=1
    # A signal mid-section exits through the unlock (set_state clears this).
    trap 'slot_unlock; exit 0' INT TERM HUP
    rows=$(tmux list-windows -a -F "$SLOT_FMT" 2>/dev/null || true)
    slot_fold "$rows"
    [ -n "${S_VIS[$w]:-}" ] || return 0           # gone, or hidden everywhere
    slot_valid "${S_SLOT[$w]:-}" && return 0      # stamped since the look
    slot_take "$w"
    cmd+=(\; set-option -w -t "$w" @agent_slot "$slot_n" "${slot_cmd[@]}")
}

clear_state() {
    local win="$1"
    local cur
    cur=$(window_state "$win")
    tmux set-option -uw -t "$win" @agent_state 2>/dev/null || true
    tmux set-option -uw -t "$win" @agent_summary 2>/dev/null || true
    tmux set-option -uw -t "$win" @agent_summary_cond \; \
         set-option -uw -t "$win" @agent_summary_key 2>/dev/null || true
    tmux set-option -uw -t "$win" @agent_pending 2>/dev/null || true
    tmux set-option -uw -t "$win" @agent_rollout 2>/dev/null || true
    tmux set-option -uw -t "$win" @agent_session_id 2>/dev/null || true
    tmux set-option -uw -t "$win" @agent_owner_token 2>/dev/null || true
    tmux set-option -uw -t "$win" @agent_since 2>/dev/null || true
    # The sidebar's per-window fields (see set_state), one call for all three,
    # plus the birth mark (agent-birth.conf): an agent that came and went
    # before the watcher ever saw it must not leave the window held.
    tmux set-option -uw -t "$win" @agent_kind \; \
         set-option -uw -t "$win" @agent_detail_kind \; \
         set-option -uw -t "$win" @agent_detail \; \
         set-option -uw -t "$win" @agent_born \; \
         set-option -uw -t "$win" @agent_ask_from \; \
         set-option -uw -t "$win" @agent_ask_bg \; \
         set-option -uw -t "$win" @agent_ask_seen \; \
         set-option -uw -t "$win" @agent_held_detail 2>/dev/null || true
    # AGENT-ORIGINATED TURNS: the session is gone, and so is every turn,
    # message and flag that belonged to it (one call).
    local -a ocmd=()
    local o
    for o in "${ORIGIN_OPTS[@]}"; do
        ocmd+=(${ocmd[0]+\;} set-option -uw -t "$win" "$o")
    done
    tmux "${ocmd[@]}" 2>/dev/null || true
    if [ -n "$cur" ]; then
        # A discharge too (see seen_op in set_state): @agent_seen_at is the
        # one mark that survives the unsets, and is never unset itself.
        tmux set-option -w -t "$win" @agent_seen_at "$(now_epoch)" 2>/dev/null || true
        tmux refresh-client -S 2>/dev/null || true
        notify_state
    fi
}

clear_pending() {
    tmux set-option -uw -t "$1" @agent_pending 2>/dev/null || true
}

# HELD DETAIL (ASKER GATE, see done): a main-agent Stop that a subagent's
# open gate holds keeps the gate's perm/ask detail on screen, and stashes
# the detail it would have written - the reply's first line, or an agent
# turn's notify_user reason - as "<kind>\t<text>" in @agent_held_detail
# (sanitized text never holds a tab). The heartbeat that ends that gate
# (the asker's re-arm, or consuming the seen gate's @agent_pending) takes
# it back into dkind/dtext for its set_state, so the window lands exactly
# where the held Stop would have left it. Unset by the next prompt, Stop,
# failure, SessionStart and clear_state; one show-options, on those two
# rare heartbeat paths only.
held_detail_take() {
    local hd
    hd=$(tmux show-options -wqv -t "$1" @agent_held_detail 2>/dev/null || true)
    [ -n "$hd" ] || return 0
    dkind="${hd%%$'\t'*}"; dtext=""
    case "$hd" in *$'\t'*) dtext="${hd#*$'\t'}" ;; esac
    ss_add set-option -uw -t "$1" @agent_held_detail
}

# ------------------------------------------------- agent-originated turns
#
# AGENT-ORIGINATED TURNS (docs/agent-tab-indicator.md, same heading). When a
# turn was started by another agent (tmux-mcp's message_agent) rather than by
# Mack, its finish is QUIET: the window goes back to exactly the attention
# state it had before that turn (an unseen green stays green, idle stays
# idle), no done tint. Loud instead when the agent flagged Mack
# (@agent_for_user, notify_user), when the turn failed, or when the turn's
# origin is unknown. Options, all window-scope:
#   @agent_inbound      written by tmux-mcp at delivery: "<epoch> <sha1> <from>
#                       <for>" entries joined by "|", newest last. A prompt is
#                       AGENT-VERIFIED iff an entry no older than 180 s has
#                       sha1(trimmed prompt) or the prompt opens with
#                       "[message from @<from>]"; the match is removed.
#                       for=user (a reply to a message Mack's own turn sent)
#                       counts as Mack's.
#   @agent_turn_origin  "<user|agent> <turn_id> <from|->", per turn.
#   @agent_turn_result  "<turn_id> loud" | "<turn_id> quiet <restored_state>"
#                       at Stop (CuaNotch's hook reads it).
#   @agent_prior_state / _since / _detail_kind / _detail   the attention state
#                       saved when an agent turn began; restored on a quiet
#                       finish, by this hook or (bg-held, Esc, codex turn end)
#                       by the watcher. Set only while an agent-origin turn's
#                       finish is pending.
#   @agent_inbound_seen verified messages that arrived MID-turn, same format,
#                       for Claude's split case (an unseen prompt_id at Stop).
#   @agent_for_user     "<epoch> <reason>", set by notify_user /
#                       agent-notify-user.sh; cleared when an agent turn starts.
#   @agent_awaiting     tmux-mcp's own (on a sender's window), unset with the
#                       session.
#   @agent_quiet        "<from|->": one-shot mark set with a quiet restore;
#                       the watcher logs that transition as "↩ replied to
#                       @from" (detail_kind `quiet`) and unsets it.
#   @agent_bg_owner     user | agent: whose turn's background work is still
#                       out. Set by a Stop HELD running for it (loud turn ->
#                       user, quiet -> agent; sticky toward user), unset by
#                       any Stop that is not held and by the watcher's
#                       release when the work drains. A wake-up (the work
#                       reporting back) inherits THIS origin, not whatever
#                       turn ran in between.
#   @agent_paused       "<epoch>": agent-pause.py's flag (docs/agent-pause.md,
#                       "The paused state"), set before its PAUSE goes out and
#                       unset before the RESUME. While it is set, a turn end
#                       that would write done or idle, or a quiet restore,
#                       writes @agent_state `paused` and @agent_turn_result
#                       "<id> paused". Dropped with the session.
#   @agent_turn_start   "<epoch>": when the last REAL prompt (not a harness
#                       wake-up, TURN_JQ's "sys") started a turn. The --since
#                       of ASKS WHILE WORK IS OUT: an agent-needs-you marker
#                       older than it belongs to an earlier turn, and one
#                       written in a woken turn after it still counts.
#   @agent_limit / @agent_limit_lifted   RATE-LIMITED AGENTS (below): dropped
#                       wherever @agent_paused is, plus by a successful Stop.
ORIGIN_OPTS=(@agent_turn_origin @agent_turn_result @agent_prior_state @agent_prior_since
             @agent_prior_detail_kind @agent_prior_detail @agent_inbound @agent_inbound_seen
             @agent_for_user @agent_awaiting @agent_quiet @agent_bg_owner @agent_paused
             @agent_turn_start @agent_limit @agent_limit_lifted)
PRIOR_OPTS=(@agent_prior_state @agent_prior_since @agent_prior_detail_kind @agent_prior_detail)
INBOUND_TTL=180
US=$'\x1f'

# ss_add CMD...: one more tmux command for the next set_state's command list.
ss_add() {
    [ "${#ss_extra[@]}" -eq 0 ] || ss_extra+=(\;)
    ss_extra+=("$@")
}
prior_unset() {
    local o
    for o in "${PRIOR_OPTS[@]}"; do ss_add set-option -uw -t "$1" "$o"; done
}

# origin_read WIN: everything the origin logic needs in ONE tmux call. The
# leading "x" is a sentinel: a read that did not come back from a format
# expansion (a failed call) leaves every field empty, which classifies as an
# unknown origin, i.e. LOUD. Free text rides last (details are sanitized to
# one control-free line; the for_user reason, written by tmux-mcp, last of
# all so a stray separator in it can only shorten itself).
origin_read() {
    local out x
    O_ORIGIN=""; O_STATE=""; O_SINCE=""; O_DK=""; O_PSTATE=""; O_PSINCE=""; O_PDK=""
    O_INB=""; O_SEEN=""; O_DT=""; O_PDT=""; O_FORUSER=""; O_RESULT=""; O_BGOWN=""; O_PAUSED=""
    O_OKIND=""; O_OTID=""; O_OFROM=""
    # ASKS WHILE WORK IS OUT's two ride along: @agent_turn_start (digits)
    # with the bare tokens, @agent_ask_seen (a sanitized line) with the
    # details.
    O_TSTART=""; O_ASKSEEN=""
    # RATE-LIMITED AGENTS: @agent_limit ("R T E", bare tokens) and the lifted
    # stamp ride with the other bare fields.
    O_LIMIT=""; O_LIFTED=""
    out=$(tmux display-message -p -t "$1" "x${US}#{@agent_turn_origin}${US}#{@agent_state}${US}#{@agent_since}${US}#{@agent_detail_kind}${US}#{@agent_prior_state}${US}#{@agent_prior_since}${US}#{@agent_prior_detail_kind}${US}#{@agent_turn_result}${US}#{@agent_bg_owner}${US}#{@agent_paused}${US}#{@agent_turn_start}${US}#{@agent_limit}${US}#{@agent_limit_lifted}${US}#{@agent_inbound}${US}#{@agent_inbound_seen}${US}#{@agent_detail}${US}#{@agent_prior_detail}${US}#{@agent_ask_seen}${US}#{@agent_for_user}" 2>/dev/null || true)
    case "$out" in "x$US"*) ;; *) return 0 ;; esac
    IFS="$US" read -r x O_ORIGIN O_STATE O_SINCE O_DK O_PSTATE O_PSINCE O_PDK O_RESULT O_BGOWN O_PAUSED O_TSTART O_LIMIT O_LIFTED O_INB O_SEEN O_DT O_PDT O_ASKSEEN O_FORUSER <<<"$out" || true
    case "$O_TSTART" in *[!0-9]*) O_TSTART="" ;; esac
    case "$O_BGOWN" in user|agent) ;; *) O_BGOWN="" ;; esac
    O_OKIND=""; O_OTID=""; O_OFROM=""
    read -r O_OKIND O_OTID O_OFROM _ <<<"$O_ORIGIN" || true
    case "$O_OKIND" in user|agent) ;; *) O_OKIND=""; O_OTID=""; O_OFROM="" ;; esac
    O_OFROM="${O_OFROM//[^A-Za-z0-9_.-]/}"
}

# The one jq for a turn event: line 1 the turn id (Claude prompt_id, Codex
# turn_id, "-" when absent or not a plain token), line 2 the sender handle when
# the prompt opens with "[message from @h]" (after leading whitespace), line 3
# "sys" when the prompt is harness-injected - a <task-notification> (a
# background task/agent reporting back; it fires UserPromptSubmit with a new
# prompt_id) or a subagent hand-back - which neither starts a turn of its own
# origin nor upgrades one: it inherits. Bounded slice before the regex.
# Line 4: the payload's `source` (Claude 2.1.311: user | sdk | system |
# loop_wakeup | schedule_wakeup | poll_event; absent on older CLIs), held to
# [a-z_]. Read ONLY by RATE-LIMITED AGENTS' hold (a loop/schedule wake-up or
# a poll event is not Mack); the origin rules above still classify by the
# prompt alone, exactly as before. `system` is never a wake-up by itself:
# native auto-continuation arrives as `system`.
TURN_JQ='
(.prompt_id // .turn_id // "" | tostring | .[0:128]
 | if test("^[A-Za-z0-9_.:-]+$") then . else "-" end),
(.prompt // "" | tostring | .[0:600]
 | ((capture("^[ \\t\\r\\n]*\\[message from @(?<h>[^\\]\\s|]{1,64})\\]") // {h: ""}).h),
   (if test("^[ \\t\\r\\n]*(<task-notification>|Another Claude session sent a message:\\s*<agent-message )")
    then "sys" else "" end)),
(.source // "" | tostring | .[0:32] | gsub("[^a-z_]"; ""))'

# Trim leading/trailing space, tab, CR, LF (the set tmux-mcp trims before it
# hashes) in LINEAR time: explode + two early-exit scans. A regex rtrim is
# quadratic on long whitespace runs (see DETAIL_JQ's note on bounding).
TRIM_JQ='
def ws: . == 32 or . == 9 or . == 13 or . == 10;
def trimws: tostring | explode as $c | ($c | length) as $n
  | (first(range(0; $n) | select($c[.] | ws | not)) // $n) as $a
  | (first(range($n - 1; $a - 1; -1) | select($c[.] | ws | not)) // ($a - 1)) as $b
  | $c[$a:$b + 1] | implode;
'

# sha1 hex of stdin, or nothing. openssl is a C binary (~5 ms); shasum is
# perl (~40 ms), the fallback.
sha1_stdin() {
    local out
    if command -v openssl >/dev/null 2>&1; then
        out=$(openssl dgst -sha1 -r 2>/dev/null) || out=""
    else
        out=$(shasum -a 1 2>/dev/null) || out=""
    fi
    out="${out%% *}"
    [[ $out =~ ^[0-9a-f]{40}$ ]] && printf '%s' "$out"
    return 0
}

# prompt_sha1 raw|lf: sha1 of the payload's FULL prompt, trimmed (tmux-mcp
# hashes the full trimmed UTF-8 body it delivered). jq -j writes the exact
# trimmed string, no newline added, straight into the hash: no $(...) ever
# holds the text, so nothing is stripped. `lf` first turns CRLF and CR into
# LF (split/join: linear, unlike gsub). Only computed when a fresh inbound
# entry exists, so an ordinary prompt never pays for it.
prompt_sha1() {
    local s f='.prompt // "" | tostring'
    [ "$1" = lf ] && f="$f"' | split("\r\n") | join("\n") | split("\r") | join("\n")'
    s=$("$JQ" -j "$TRIM_JQ$f"' | trimws' <<<"$payload" 2>/dev/null | sha1_stdin) || s=""
    printf '%s' "$s"
}

# seen_for SHA: the <for> of an @agent_inbound_seen entry with that hash.
seen_for() {
    local e ep sh fr fo
    local -a ents=()
    IFS='|' read -ra ents <<<"$O_SEEN" || true
    for e in ${ents[@]+"${ents[@]}"}; do
        read -r ep sh fr fo _ <<<"$e" || true
        [ "$sh" = "$1" ] && { printf '%s' "${fo:-agent}"; return 0; }
    done
    return 0
}

# split_classify ID TRANSCRIPT: the origin of a Claude turn whose Stop carries
# a prompt_id no UserPromptSubmit announced. Two ways that happens:
#   * the SPLIT case: a prompt queued while the model wrote its final text
#     runs as its own turn with a new id and no UserPromptSubmit;
#   * a harness wake-up (a background task reporting back) that skipped the
#     hook.
# The transcript's `user` row carrying that promptId says which: Claude
# records origin.kind ("human" for typed/queued prompts, "task-notification"
# for injected ones; promptSource typed|queued|system, isMeta). A human row is
# classified by @agent_inbound_seen's hash (its <for>) or the
# "[message from @" opener; anything else INHERITS the current origin. Only
# the last 1 MB is read (a grep for the id first, so jq sees only that turn's
# rows). Prints agent | user | inherit; nothing found prints user (LOUD).
split_classify() {
    local id="$1" tp="$2" out kind pre txt h fo
    out=$(tail -c 1048576 "$tp" 2>/dev/null | grep -F "\"promptId\":\"$id\"" \
        | "$JQ" -Rrn --arg id "$id" "$TRIM_JQ"'
            first(inputs | fromjson? | objects
              | select(.type == "user" and .promptId == $id)
              | . as $r | ($r.message.content) as $c
              | (if ($c | type) == "string" then $c
                 elif ($c | type) == "array" then
                   ([$c[] | select(type == "object" and .type == "text") | .text] | if length > 0 then join("\n") else empty end)
                 else empty end) as $t
              | (if ($r.origin.kind // "") == "human" or ($r.promptSource // "") == "typed"
                    or ($r.promptSource // "") == "queued" then "human" else "sys" end)
                + "\t" + ($t | trimws | if startswith("[message from @") then "P" else "-" end)
                + "\t" + ($t | trimws))' 2>/dev/null || true)
    if [ -z "$out" ]; then printf user; return 0; fi
    kind="${out%%$'\t'*}"; out="${out#*$'\t'}"
    pre="${out%%$'\t'*}"; txt="${out#*$'\t'}"
    if [ "$kind" != human ]; then printf inherit; return 0; fi
    if [ -n "$O_SEEN" ]; then
        # As typed, then with CR/CRLF line ends as LF (see prompt_sha1).
        local t2="${txt//$'\r\n'/$'\n'}"
        t2="${t2//$'\r'/$'\n'}"
        for t2 in "$txt" "$t2"; do
            h=$(printf '%s' "$t2" | sha1_stdin)
            [ -n "$h" ] || continue
            fo=$(seen_for "$h")
            case "$fo" in user) printf user; return 0 ;; agent) printf agent; return 0 ;; esac
        done
    fi
    if [ "$pre" = P ]; then printf agent; else printf user; fi
}

# prior_save WIN: queue (ss_add) the save of WIN's current attention state as
# the prior an agent-originated turn's quiet finish restores. A window still
# `running` (a Stop held by background work) is not a state to come back to:
#   - a prior an earlier agent turn saved (and maybe Mack has since seen and
#     clear-current downgraded) stays as it is;
#   - else Mack's own turn that already ENDED loud and is only held for its
#     background work: a green he has not seen yet, so the prior is `done`
#     (that turn's done detail if the window still shows it);
#   - else idle.
prior_save() {
    local win="$1"
    # ACROSS A PAUSE. A paused Stop keeps the prior an agent turn saved (an
    # unseen green from before the pause), and agent-pause.py turns `paused`
    # back to idle at the resume. So the turn after it - the RESUME, or any
    # ping while still paused - must not save `paused` or that idle over it:
    # keep the existing prior, so the RESUME's quiet finish brings the green
    # back. `paused` itself is never a state to come back to (it would outlive
    # the resume).
    if [ "$O_STATE" = paused ] || { [ "$O_STATE" = idle ] && [[ $O_RESULT == *" paused" ]]; }; then
        if [ -z "$O_PSTATE" ]; then
            ss_add set-option -w -t "$win" @agent_prior_state idle
            ss_add set-option -uw -t "$win" @agent_prior_since
            ss_add set-option -uw -t "$win" @agent_prior_detail_kind
            ss_add set-option -uw -t "$win" @agent_prior_detail
        fi
        return 0
    fi
    case "$O_STATE" in
        running|"")
            # Held for Mack's background work: @agent_bg_owner says so. The
            # verdict test is the fallback for a window marked before that
            # option existed; it matches any `loud` (a Claude split turn's
            # loud Stop carries a newer id than the origin's), which is safe
            # because a new agent turn cannot start while a USER turn is
            # still in flight - a ping then joins that turn instead.
            if [ -n "$O_PSTATE" ]; then
                :
            elif [ "$O_STATE" = running ] && { [ "$O_BGOWN" = user ] || \
               { [ -z "$O_BGOWN" ] && [ "$O_OKIND" = user ] && [[ $O_RESULT == *" loud" ]]; }; }; then
                local now
                printf -v now '%(%s)T' -1 2>/dev/null || now=$(now_epoch)
                ss_add set-option -w -t "$win" @agent_prior_state done
                ss_add set-option -w -t "$win" @agent_prior_since "$now done"
                if [ "$O_DK" = done ]; then
                    ss_add set-option -w -t "$win" @agent_prior_detail_kind done
                    ss_add set-option -w -t "$win" @agent_prior_detail "$O_DT"
                else
                    ss_add set-option -uw -t "$win" @agent_prior_detail_kind
                    ss_add set-option -uw -t "$win" @agent_prior_detail
                fi
            else
                ss_add set-option -w -t "$win" @agent_prior_state idle
            fi ;;
        *)
            ss_add set-option -w -t "$win" @agent_prior_state "$O_STATE"
            if [ -n "$O_SINCE" ]; then ss_add set-option -w -t "$win" @agent_prior_since "$O_SINCE"
            else ss_add set-option -uw -t "$win" @agent_prior_since; fi
            if [ -n "$O_DK" ]; then
                ss_add set-option -w -t "$win" @agent_prior_detail_kind "$O_DK"
                ss_add set-option -w -t "$win" @agent_prior_detail "$O_DT"
            else
                ss_add set-option -uw -t "$win" @agent_prior_detail_kind
                ss_add set-option -uw -t "$win" @agent_prior_detail
            fi ;;
    esac
}

# prior_restore WIN: set up the next set_state to put WIN back to the saved
# prior attention state (quiet finish). Seen-it rules apply to what comes
# back: a done or needs-input restored while Mack is looking (viewing_now)
# lands as idle, stamped seen, exactly as a watched Stop would; red is never
# discharged by being watched. The stamp the window had comes back with it
# when it still describes that state. Sets R_STATE.
prior_restore() {
    local win="$1" ps="${O_PSTATE:-idle}"
    case "$ps" in idle|done|needs-input|failed) ;; *) ps=idle ;; esac
    case "$ps" in
        done|needs-input) if viewing_now; then ps=idle; seen_op=1; fi ;;
    esac
    ss_since=""
    if [[ $O_PSINCE =~ ^[0-9]{1,18}\ (.+)$ ]] && [ "${BASH_REMATCH[1]}" = "$ps" ]; then
        ss_since="$O_PSINCE"
    fi
    if [ -n "$O_PDK" ]; then dkind="$O_PDK"; dtext="$O_PDT"; else dkind=-; dtext=""; fi
    pend_op=unset
    prior_unset "$win"
    R_STATE="$ps"
}

# stop_fields: S_ID (the turn id, as TURN_JQ's line 1) and S_TP (Claude's
# transcript_path) from the payload, one jq.
stop_fields() {
    local out=""
    s_id=-; s_tp=""
    if [ -n "$JQ" ] && [ -n "$payload" ]; then
        out=$("$JQ" -r '(.prompt_id // .turn_id // "" | tostring | .[0:128]
                         | if test("^[A-Za-z0-9_.:-]+$") then . else "-" end),
                        (.transcript_path // "" | tostring | gsub("[\\r\\n]"; " "))' \
              <<<"$payload" 2>/dev/null || true)
    fi
    { IFS= read -r s_id; IFS= read -r s_tp; } <<<"$out" || true
    [ -n "$s_id" ] || s_id=-
}

# A for_user reason as a sidebar detail: DETAIL_JQ's own sanitizer.
reason_detail() {
    local r="$1"
    case "$r" in [0-9]*" "*) r="${r#* }" ;; esac
    printf '%s' "$r" | "$JQ" -Rr "$DETAIL_JQ"'clean' 2>/dev/null | head -n 1 || true
}

# ------------------------------------------------- asks while work is out
#
# ASKS WHILE WORK IS OUT (2026-10-22; docs/agent-tab-indicator.md, same
# heading). A Stop with background work still out stays `running` (see done),
# so a turn that ended ASKING Mack something hid behind the spinner. At such
# a Stop the one decider both surfaces share, agent-turn-asks, is asked
# synchronously, `--marker-only --since @agent_turn_start`, SIGTERMed at
# ASK_SYNC_BUDGET (300 ms): the agent's own `agent-needs-you` marker, which
# it must write right before ending its turn.
#   EXPLICIT ONLY (2026-10-26, Mack's call): that marker is the ONLY way in.
# The detached full run (agent-turn-asks' copilot MODEL BACKSTOP, guessing
# from the last message) is gone: it raised needs-input far too often. No
# marker = the Stop stays `running`, nothing ask-related written.
# A yes RAISES: needs-input, @agent_detail_kind ask, @agent_detail the line,
# @agent_ask_from main (so only the main agent's own next tool call re-arms
# it, never a background subagent's), @agent_ask_bg 1. Unless Mack is
# looking (the Stop stays running, stamped seen) or the line is the one he
# already saw (@agent_ask_seen, below): then nothing ask-related is written.
#   A LOOK (clear-current, the watcher's viewed-discharge, agent-ui's answer
# discharge, which is clear-current) at an @agent_ask_bg window copies the
# shown line into @agent_ask_seen (while its kind is still `ask`: a
# permission gate landing on a raised ask replaces it, ask_bg and all, in
# needs-approval) and, while @agent_workflow says the work is
# still out, goes back to `running` in ONE command list: @agent_seen_at and
# @agent_since "<same epoch> running", @agent_detail_kind unset, @agent_ask_bg
# unset. With nothing out any more it is the ordinary idle + pending
# discharge. Only a later Stop raises again. @agent_ask_seen is unset by a
# real prompt (Mack replied), a SessionStart, clear_state and the GC.

# ask_detail LINE: the decider's line as a sidebar detail (DETAIL_JQ clean).
ask_detail() {
    local d=""
    [ -z "$1" ] || d=$(printf '%s' "$1" | "$JQ" -Rr "$DETAIL_JQ"'clean' 2>/dev/null | head -n 1 || true)
    printf '%s' "${d:-needs your input}"
}

# ask_run BUDGET ARGS... < STDIN: agent-turn-asks ARGS with this stdin,
# SIGTERMed after BUDGET seconds (never KILL). Sets ask_rc (its exit status,
# 2 when it is missing, 143 when stopped) and ask_line (its first line).
# The watchdog writes nowhere a hook's reader waits on, and its orphaned
# sleep (when the decider ends first) holds no pipe.
ask_run() {
    local budget="$1" out pid wd
    shift
    ask_rc=2; ask_line=""
    [ -x "$TURN_ASKS" ] || return 0
    out=$(mktemp "${TMPDIR:-/tmp}/agent-tab-ask.XXXXXX" 2>/dev/null) || return 0
    # `<&0`: an async command's stdin is /dev/null unless it names one.
    "$TURN_ASKS" "$@" <&0 >"$out" 2>/dev/null &
    pid=$!
    ( sleep "$budget"; kill -TERM "$pid" ) </dev/null >/dev/null 2>&1 &
    wd=$!
    ask_rc=0
    wait "$pid" 2>/dev/null || ask_rc=$?
    kill "$wd" 2>/dev/null || true
    IFS= read -r ask_line < "$out" 2>/dev/null || true
    rm -f "$out" 2>/dev/null || true
}

# ask_seen_same CLEAN: CLEAN (an ask_detail line) is the ask Mack already
# saw on the window: origin_read's @agent_ask_seen (written only as a cleaned
# line, so no second sanitizer fork).
ask_seen_same() {
    [ -n "$O_ASKSEEN" ] && [ "$O_ASKSEEN" = "$1" ]
}

# ask_raise WIN CLEAN: queue the raise for set_state (the caller writes
# needs-input next).
ask_raise() {
    dkind=ask; dtext="$2"
    ss_add set-option -w -t "$1" @agent_ask_from main
    ss_add set-option -w -t "$1" @agent_ask_bg 1
}

# ------------------------------------------------------ rate-limited agents
#
# RATE-LIMITED AGENTS (2026-10-26; docs/agent-tab-indicator.md, same heading).
# A usage limit ends a Claude turn with StopFailure error == "rate_limit", not
# with Stop. That used to land as `failed` (red, top of NEEDS YOU) although
# nothing broke: the agent only has to wait. Now it is a PAUSED agent with a
# reason, not a second state machine:
#   @agent_paused E, @agent_limit "R T E", @agent_detail_kind limit,
#   @agent_detail "<text>", @agent_turn_result "<id> limited R",
#   @agent_since "E paused", @agent_state paused (last)
# in ONE tmux command list, in that order (CuaNotch reads @agent_limit and the
# state in one format), then the STATE NOTICE, then agent-pause.py limit-add W
# in the background (its limited.json resume engine; fire-and-forget, a
# missing or failing script costs nothing here). R = resets_at (epoch s, 0 =
# unknown: a model limit, never auto-resumed), T = five_hour | seven_day |
# seven_day_opus | seven_day_sonnet | overage | model | codex, E = when it was
# marked. Idempotent: queued task-notifications each hit the limit again and
# re-mark, which only refreshes E.
#   A prompt while @agent_limit is set (running): a wake-up, task-notification
# or peer message while @agent_paused is still set and now < R is HELD - the
# hook writes nothing, so the tab stays paused without a running flicker.
# Anything else runs, LOUD (the resumed work is Mack's own, even when our
# RESUME comes through tmux-mcp), with the ordinary `run` detail, and unsets
# @agent_paused in the same command list (Mack's prompt, a native
# auto-continuation after R that limit-tick never beat, our RESUME) - except
# a pause-key stamp set on the window while it was working. @agent_limit stays
# until the turn's outcome: ANY outcome but a rate limit drops it (limit_end:
# a Stop, another failure, an interrupt; the watcher's turn ends likewise),
# a rate_limit StopFailure re-marks. A held prompt is one the harness sent
# (task-notification, peer, or a `source` of loop_wakeup / schedule_wakeup /
# poll_event). Codex has no failure hook: its Stop reads the rollout here,
# and the watcher is the backstop (agent-tab-watcher.sh, same heading).
# AGENT_LIMIT_PAUSE_PY overrides agent-pause.py's path (tests).
LIMIT_PY="${AGENT_LIMIT_PAUSE_PY:-$HOME/.config/tmux/scripts/agent-pause.py}"
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

# limit_type RAW R E: into LIM_TY the @agent_limit type for a transcript's
# rateLimitType. seven_day_overage_included (and any other seven_day_*) is
# weekly; an unknown one is guessed from how far out the reset is.
limit_type() {
    case "$1" in
        five_hour|seven_day|seven_day_opus|seven_day_sonnet|overage|model|codex) LIM_TY="$1" ;;
        seven_day_*) LIM_TY=seven_day ;;
        *) if [ $(($2 - $3)) -gt 21600 ]; then LIM_TY=seven_day; else LIM_TY=five_hour; fi ;;
    esac
}

# limit_add WIN: agent-pause.py limit-add WIN, owned by the tmux server (a
# hook's process group may be reaped once it returns, as spawn_condense notes).
limit_add() {
    local launch
    [ -x "$LIMIT_PY" ] || return 0
    printf -v launch '%q ' env "HOME=$HOME" "PATH=$PATH" "TMPDIR=${TMPDIR:-/tmp}" \
        "$LIMIT_PY" limit-add "$1"
    launch+=' >/dev/null 2>&1'
    tmux run-shell -b "$launch" >/dev/null 2>&1 || true
}

# limit_mark WIN R TYPE TEXT TURN_ID: the marking (see the section header).
# The marking list is one tmux call, preceded in that same call by the
# housekeeping any turn end does: a stale @agent_pending, a held Stop's stash,
# a quiet-restore mark and a lifted stamp go, as does the prior an agent
# turn saved (the turn after a limit is Mack's own, so nothing is restored),
# and the asker of a needs-input this leaves.
limit_mark() {
    local win="$1" r="$2" ty="$3" txt="$4" tid="$5" e cur o
    local -a cmd=()
    owns_window "$win" || return 0
    printf -v e '%(%s)T' -1 2>/dev/null || e=$(now_epoch)
    cur=$(window_state "$win")
    for o in @agent_pending @agent_held_detail @agent_quiet @agent_limit_lifted "${PRIOR_OPTS[@]}"; do
        cmd+=(${cmd[0]+\;} set-option -uw -t "$win" "$o")
    done
    [ "$cur" != needs-input ] || cmd+=(\; set-option -uw -t "$win" @agent_ask_from \;
                                       set-option -uw -t "$win" @agent_ask_bg)
    case "$agent" in
        claude|codex|opencode) cmd+=(\; set-option -w -t "$win" @agent_kind "$agent") ;;
    esac
    cmd+=(\; set-option -w -t "$win" @agent_paused "$e"
          \; set-option -w -t "$win" @agent_limit "$r $ty $e"
          \; set-option -w -t "$win" @agent_detail_kind limit
          \; set-option -w -t "$win" @agent_detail "$txt"
          \; set-option -w -t "$win" @agent_turn_result "$tid limited $r"
          \; set-option -w -t "$win" @agent_since "$e paused"
          \; set-option -w -t "$win" @agent_state paused)
    tmux "${cmd[@]}" 2>/dev/null || true
    [ "$cur" = paused ] || tmux refresh-client -S 2>/dev/null || true
    notify_state
    limit_add "$win"
}

# limit_end WIN LIMIT LIFTED PAUSED: ANY turn outcome but a rate limit ends
# the limit (a Stop, a failure, an interrupt; the watcher's un-stick too).
# Queues (ss_add, the outcome's own command list) the unset of @agent_limit
# and @agent_limit_lifted when either is set, and of @agent_paused while it
# still holds the limit's own E (a pause-key stamp is left for the pause
# key). LIM_UNPAUSED=1 when it dropped @agent_paused, so the caller treats
# the window as not paused.
limit_end() {
    local win="$1" lim="$2" lifted="$3" paused="$4" r="" t="" e=""
    LIM_UNPAUSED=0
    [ -n "$lim$lifted" ] || return 0
    ss_add set-option -uw -t "$win" @agent_limit
    ss_add set-option -uw -t "$win" @agent_limit_lifted
    read -r r t e _ <<<"$lim" || true
    if [ -n "$paused" ] && [ -n "$e" ] && [ "$paused" = "$e" ]; then
        ss_add set-option -uw -t "$win" @agent_paused
        LIM_UNPAUSED=1
    fi
    return 0
}

# LIMIT_JQ: the last main-thread rate_limit record of a transcript tail
# (lines already narrowed by grep): resetsAt (0 = none), rateLimitType,
# apiError, and its text, sanitized (DETAIL_JQ's clean). One line each.
LIMIT_JQ='
  [inputs | fromjson? | objects
   | select((.isSidechain // false) != true and .isApiErrorMessage == true and .error == "rate_limit")]
  | (last // {}) as $r
  | ($r.quotaLimits.resetsAt // $r.apiErrorParams.rate_limit_info.resetsAt // 0
     | if type == "number" then floor elif type == "string" then (tonumber? // 0) else 0 end),
    ($r.quotaLimits.rateLimitType // $r.apiErrorParams.rate_limit_info.rateLimitType // ""
     | tostring | gsub("[^a-z_]"; "") | .[0:40]),
    ($r.apiError // "" | tostring | gsub("[^A-Za-z0-9_]"; "") | .[0:60]),
    ($r.message.content // ""
     | if type == "string" then . elif type == "array" then
         [.[] | select(type == "object" and .type == "text") | .text] | join(" ")
       else "" end | clean)'




sanitize_summary() {
    # One line, no format-significant characters, bounded length. The value
    # lands in window-status-format via #{@agent_summary}; '#' starts a
    # format/style token and '%' is a strftime metacharacter under #{T:…}, so
    # strip both defensively even though the current render path is bare.
    # '/' becomes a space so the only slash in a tab name is the deliberate
    # "<project>/" separator — a model title or project basename can't add
    # its own (it would read as a second path segment).
    # $1 overrides the 60-char bound: a title is display text, but a prompt is
    # the condenser's *input* and wants more of the sentence (extract_summary).
    local max="${1:-60}"
    tr '\n\t/' '   ' | tr -d '#"%' | sed -e 's/  */ /g' -e 's/^ //' -e 's/ $//' | cut -c1-"$max"
}

# $3=1 marks <summary> as a condensed (model-written) label; 0/absent marks it
# as raw stand-in text. Recorded in @agent_summary_cond so compose_summary can
# refuse to downgrade a finished label back to raw text on a later turn. The
# flag is written before the no-change short-circuit: re-asserting the same
# string must still correct the flag.
# $4: the condense key this label answers (@agent_summary_key, see
# compose_summary's SUMMARY KEY), "-" to unset it, empty to leave it. It
# rides in the flag's tmux call: no extra fork.
set_summary() {
    local win="$1" summary="$2" cond="${3:-0}" key="${4:-}" cur
    local -a kc=()
    owns_window_once "$win" || return 0
    [ -n "$summary" ] || return 0
    case "$key" in
        "") ;;
        -) kc=(\; set-option -uw -t "$win" @agent_summary_key) ;;
        *) kc=(\; set-option -w -t "$win" @agent_summary_key "$key") ;;
    esac
    if [ "$cond" = 1 ]; then
        tmux set-option -w -t "$win" @agent_summary_cond 1 ${kc[@]+"${kc[@]}"} 2>/dev/null || true
    else
        tmux set-option -uw -t "$win" @agent_summary_cond ${kc[@]+"${kc[@]}"} 2>/dev/null || true
    fi
    cur=$(tmux show-options -wqv -t "$win" @agent_summary 2>/dev/null || true)
    [ "$cur" = "$summary" ] && return 0
    tmux set-option -w -t "$win" @agent_summary "$summary" 2>/dev/null || true
    tmux refresh-client -S 2>/dev/null || true
}

# --- summary composition: @agent_summary = "<project>/<ultra-short title>" -

# Cache rows are TAB-separated: <key> <short> <epoch>. A row with an empty
# <short> is a NEGATIVE entry (a failed/garbage condense) used to back off
# retries for NEG_TTL seconds instead of re-calling the model every turn.
CACHE="$HOME/.cache/agent-tab/titles.tsv"
NEG_TTL=600

# Condenser backends. Every condense is one ai-exec call (the fallback door
# closed-tabs.sh and chezmoi-commit use too): copilot first, then codex, then
# claude haiku, each only when the one before fails or is usage-limited
# (ai-exec remembers codex's reset time and skips it until then). 2026-10-28:
# copilot ran out of its monthly quota and, with copilot as the only backend,
# every condense failed and tabs sat on raw prompt words for the rest of the
# month. Copilot's `--model` takes only `auto` on this account (CLI 1.0.102
# refused every named id), so that is the default; AGENT_TAB_CONDENSE_MODEL
# overrides it.
CONDENSE_ORDER="${AGENT_TAB_CONDENSE_ORDER:-copilot,codex,claude}"
CONDENSE_MODEL="${AGENT_TAB_CONDENSE_MODEL:-auto}"
CONDENSE_CODEX_MODEL=gpt-6-luna
CONDENSE_CLAUDE_MODEL=haiku
# The whole chain, not each leg: a quota-dead copilot answers in ~3 s.
CONDENSE_TIMEOUT=120

ai_exec_bin() {
    command -v ai-exec 2>/dev/null || { [ -x "$HOME/.local/bin/ai-exec" ] && echo "$HOME/.local/bin/ai-exec"; }
}

now_epoch() { date +%s 2>/dev/null || echo 0; }

title_key() {
    printf '%s' "$1" | /usr/bin/shasum -a 256 | cut -c1-16
}

# Last non-empty short for a title (skips negative rows; last writer wins).
cached_short() {
    [ -f "$CACHE" ] || return 0
    cached_short_key "$(title_key "$1")"
}

# The same, by a title_key already computed.
cached_short_key() {
    [ -f "$CACHE" ] || return 0
    awk -F'\t' -v k="$1" \
        '$1==k && $2!=""{v=$2} END{if(v!="")print v}' "$CACHE" 2>/dev/null || true
}

# True if the most recent row for this key is a negative within NEG_TTL —
# i.e. we failed to condense recently and shouldn't retry the model yet.
negative_fresh() {
    [ -f "$CACHE" ] || return 1
    awk -F'\t' -v k="$1" -v now="$(now_epoch)" -v ttl="$NEG_TTL" \
        '$1==k{s=$2;ts=$3} END{exit !(s=="" && ts!="" && (now-ts)<ttl)}' \
        "$CACHE" 2>/dev/null
}

cache_put() {
    mkdir -p "$(dirname "$CACHE")" 2>/dev/null || true
    printf '%s\t%s\t%s\n' "$1" "$2" "$(now_epoch)" >> "$CACHE" 2>/dev/null || true
}

# Trim to <=N chars on whole-word boundaries (never mid-word unless the first
# word alone is longer than N).
fit_words() {
    local s="$1" n="${2:-24}"
    while [ "${#s}" -gt "$n" ] && [ "${s% *}" != "$s" ]; do s="${s% *}"; done
    printf '%s' "$s" | cut -c1-"$n"
}

# TITLE BUDGET: the cells a label gets in the agent-ui sidebar, which shows
# @agent_summary minus its `project/` (layout.rs short_title) and clips it to
# what its card row leaves (layout.rs agent_rows). Derived from that code, for
# the worst common card — two lines, working, a ⚙/◎ flag, the client's own:
#   pane  = |@sidebar_width| - 1     the value includes WezTerm's divider and
#                                    is negative for a left sidebar
#                                    (status.rs width_value)
#   inner = pane - 2                 a 1-cell margin each side (inner/margin)
#   fixed = 6 + 2 + 10 + 1 + 1       lead: rail, pad, the 3-cell key-cap
#                                    (BADGE_W), pad (Badge::lead); the flag
#                                    " ⚙"/" ◎"; "◐ working" + its pad
#                                    (right_of: "working" is the longest state
#                                    word); the 1 in `fixed`; `marked` (the
#                                    client's own card's edge)
#   room  = @sidebar_width - 23      22 at today's 45 columns
# Clamped to 12..24 (24: the tab bar's own cap; 12: below that a label is
# useless, the sidebar clips). Unset or non-numeric width → 24.
# AGENT_TAB_TITLE_BUDGET overrides (tests). Read ONLY in the detached
# condense worker: never a tmux call on the hook's hot path. A width change
# later does not re-condense cached labels — the sidebar clips with an
# ellipsis. If agent_rows' chrome changes, change TITLE_CHROME with it.
TITLE_CHROME=23
TITLE_BUDGET_MIN=12
TITLE_BUDGET_MAX=24
title_budget() {
    local w="${AGENT_TAB_TITLE_BUDGET:-}"
    if [[ $w =~ ^[0-9]+$ ]] && [ "$w" -ge 1 ]; then
        printf '%s' "$((10#$w))"
        return 0
    fi
    w=$(tmux show-options -gqv @sidebar_width 2>/dev/null || true)
    w="${w#-}"
    if ! [[ $w =~ ^[0-9]{1,4}$ ]]; then
        printf '%s' "$TITLE_BUDGET_MAX"
        return 0
    fi
    w=$((10#$w - TITLE_CHROME))
    [ "$w" -ge "$TITLE_BUDGET_MIN" ] || w=$TITLE_BUDGET_MIN
    [ "$w" -le "$TITLE_BUDGET_MAX" ] || w=$TITLE_BUDGET_MAX
    printf '%s' "$w"
}

# A /rename title as words: Claude's generated names are kebab slugs, so a
# space-free one with -/_ reads as words; anything with a space is shown as
# typed.
custom_words() {
    local s="$1"
    case "$s" in
        *" "*) ;;
        *[-_]*)
            s="${s//[-_]/ }"
            while [[ $s == *"  "* ]]; do s="${s//  / }"; done
            s="${s# }"; s="${s% }"
            ;;
    esac
    printf '%s' "${s:-$1}"
}

# Gate model output before caching: accept only title-like text (1-4 words,
# <= $2 chars — the title budget, default 24 — has a letter, no colon /
# sentence-final punctuation, not an apology / refusal / auth-error /
# first-person reply). This is what stops copilot error strings ("Credit
# balance is too…") from poisoning the cache.
valid_short() {
    local s="$1" max="${2:-24}" lc wc
    [ -n "$s" ] || return 1
    [ "${#s}" -le "$max" ] || return 1
    case "$s" in *:*|*.|*!|*\?) return 1 ;; esac
    case "$s" in *[A-Za-z]*) ;; *) return 1 ;; esac
    wc=$(printf '%s' "$s" | wc -w | tr -d ' ')
    [ "$wc" -ge 1 ] && [ "$wc" -le 4 ] || return 1
    # Refusal/error preambles START with these — anchor to the front so a
    # legit title that merely CONTAINS a word like "login" or "error"
    # ("Add login flow", "Error boundary component") isn't rejected. Errors
    # with a colon ("Error: …") are already caught by the *:* rule above.
    lc=$(printf '%s' "$s" | tr '[:upper:]' '[:lower:]')
    case "$lc" in
        "i "*|"i'"*|sorry*|apolog*|please*|unable*|"sure,"*|"here "* \
        |"credit balance"*|"not authenticated"*) return 1 ;;
    esac
    return 0
}

project_name() {
    # Basename of the hook payload's cwd (HOME → "~"), falling back to the
    # agent pane's current path.
    local payload="$1" cwd=""
    if [ -n "$JQ" ] && [ -n "$payload" ]; then
        cwd=$("$JQ" -r '.cwd // empty' <<<"$payload" 2>/dev/null || true)
    fi
    if [ -z "$cwd" ] && [ -n "${pane:-}" ]; then
        cwd=$(tmux display-message -p -t "$pane" '#{pane_current_path}' 2>/dev/null || true)
    fi
    [ -n "$cwd" ] || return 0
    if [ "$cwd" = "$HOME" ]; then
        printf '~'
    else
        basename "$cwd" | sanitize_summary | cut -c1-20
    fi
}

compose_summary() {
    # Set "<project>/<short>" immediately from cache when we've condensed
    # this title before; otherwise show "<project>/<raw>" as an interim and
    # condense in a detached child (an LLM call must never block a hook).
    # $2 is extract_summary's tagged "<src>\t<title>" (bare text = final;
    # retitle is "retitle\t<label>\t<prompts>").
    local win="$1" tagged="$2" payload="$3" proj short src raw prev="" kin
    [ -n "$tagged" ] || return 0
    case "$tagged" in
        *$'\t'*) src="${tagged%%$'\t'*}"; raw="${tagged#*$'\t'}" ;;
        *)       src="final"; raw="$tagged" ;;
    esac
    # retitle carries "<label on screen when it fired>\t<drifted prompts>".
    if [ "$src" = retitle ]; then
        case "$raw" in
            *$'\t'*) prev="${raw%%$'\t'*}"; raw="${raw#*$'\t'}" ;;
            *)       src="interim" ;;
        esac
        [ -n "$prev" ] || src="interim"
    fi
    [ -n "$raw" ] || return 0
    # The condense input, which is also the cache key: a retitle is judged
    # against the label it may keep, so the same prompts under another label
    # are a different question.
    kin="$raw"
    [ -z "$prev" ] || kin="retitle"$'\n'"$prev"$'\n'"$raw"
    [ "$src" != custom ] || kin="custom"$'\n'"$raw"
    # SUMMARY KEY. The window remembers which condense its label should come
    # from (@agent_summary_key = this kin's title_key), written with every
    # final paint (set_summary's 4th argument, in the flag's tmux call) and
    # with every worker spawn (in the run-shell's own call): no extra fork. A
    # worker paints only while the key is still its own, so a slow worker for
    # an older title (a long /rename, then a short one before copilot
    # answered) can't repaint the newer one for a turn.
    local key
    # Nothing below may touch a window this hook can't prove it owns (the key
    # writes ride outside set_summary). Memoized: set_summary reuses it.
    owns_window_once "$win" || return 0
    key=$(title_key "$kin")
    proj=$(project_name "$payload")
    # A custom title (/rename) is the user's own naming, but Claude's
    # generated names run long ("topic-drift-detector-hook-for-tab-titles"),
    # and the sidebar clips whatever doesn't fit its card. So it is shown as
    # written when it fits the title budget, else shortened by the condenser
    # to fit — keyed apart from every other source ("custom\n<title>"), so a
    # rename never shares a cache row with the same text as a prompt. Until
    # the worker answers (or if it fails), the words trimmed to 24 stand in.
    # Marked condensed (1) either way, so no raw stand-in repaints over it.
    if [ "$src" = custom ]; then
        short=$(cached_short_key "$key")
        if [ -n "$short" ]; then
            set_summary "$win" "${proj:+$proj/}$short" 1 "$key"
            return 0
        fi
        set_summary "$win" "${proj:+$proj/}$(fit_words "$(custom_words "$raw")" 24)" 1
        spawn_condense "$win" "$proj" "$raw" "" custom "$key"
        return 0
    fi
    short=$(cached_short_key "$key")
    if [ -n "$short" ]; then
        set_summary "$win" "${proj:+$proj/}$short" 1 "$key"
        return 0
    fi
    # Raw text is only ever a stand-in for the label the condenser is about to
    # write, so don't paint it over a label already condensed for this window.
    # Both sources get condensed (see $src below), so the second one to arrive
    # would otherwise flash the untrimmed title for a second or two mid-turn —
    # which reads as the tab breaking, not refining. With no condenser (or a
    # failing one) the flag never gets set and raw text still shows through.
    # A retitle never paints raw at all: the judge's likeliest answer is "keep
    # the label", which is what is on screen.
    if [ "$src" != retitle ] &&
       [ "$(tmux show-options -wqv -t "$win" @agent_summary_cond 2>/dev/null || true)" != 1 ]; then
        set_summary "$win" "${proj:+$proj/}$(fit_words "$raw" 24)"
    fi
    # $src is condensed either way. The agent writes its own title only at the
    # END of the first turn, so gating on `final` meant a whole turn of raw
    # prompt text in the tab; condensing the `interim` prompt names the tab
    # from the moment the first message is sent, for one extra call per
    # session (the real title hashes to a different key). The cache read above
    # keeps repeats — and every later turn — free.
    spawn_condense "$win" "$proj" "$raw" "$prev" "" "$key"
}

# spawn_condense WIN PROJ RAW PREV KIND KEY: the detached condense worker (see
# the condense mode; KIND is "" or custom). KEY (the SUMMARY KEY) is stored
# on the window in the same tmux call that launches the worker.
spawn_condense() {
    [ -n "$(ai_exec_bin)" ] || return 0
    local -a pre=()
    [ -z "${6:-}" ] || pre=(set-option -w -t "$1" @agent_summary_key "$6" \;)
    # Hooks may have their whole process group reaped after returning. Ask
    # the tmux server to own this background job instead, carrying the exact
    # socket/session identity rather than the server's launch environment.
    # The title budget's override rides along (the worker reads the budget).
    local launch
    printf -v launch '%q ' env "HOME=$HOME" "PATH=$PATH" "TMPDIR=${TMPDIR:-/tmp}" \
        "TMUX=${TMUX:-}" "TMUX_PANE=${TMUX_PANE:-}" \
        "AGENT_TAB_SOCKET=${AGENT_TAB_SOCKET:-}" \
        "AGENT_TAB_OWNER_SESSION=${AGENT_TAB_OWNER_SESSION:-}" \
        "AGENT_TAB_OWNER_TOKEN=${AGENT_TAB_OWNER_TOKEN:-}" \
        "AGENT_TAB_OWNER_BINDING=${AGENT_TAB_OWNER_BINDING:-}" \
        "AGENT_TAB_OWNER_RECORD=${AGENT_TAB_OWNER_RECORD:-}" \
        "AGENT_TAB_CONDENSE_MODEL=$CONDENSE_MODEL" \
        "AGENT_TAB_CONDENSE_ORDER=$CONDENSE_ORDER" \
        "AGENT_TAB_TITLE_BUDGET=${AGENT_TAB_TITLE_BUDGET:-}" \
        bash "$0" condense "$1" "$2" "$3" "$4" "$5"
    launch+=' >/dev/null 2>&1'
    tmux ${pre[@]+"${pre[@]}"} run-shell -b "$launch" >/dev/null 2>&1 || true
}

# --- claude title sources + topic drift --------------------------------------
#
# Claude writes {"type":"ai-title"} once, at the end of the first turn, and
# re-appends the SAME line every turn: it practically never retitles (1 of
# ~300 sessions ever did). So a tab named on turn one keeps that name while
# the conversation moves on. Two ways out, both read per event:
#   * /rename appends {"type":"custom-title","customTitle":…} (with no args,
#     Claude generates a fresh kebab-case name). The user's own naming.
#   * TOPIC DRIFT: on every UserPromptSubmit, agent-topic-drift.awk compares
#     the prompt's content words with a bag of recent on-topic prompts (pure
#     lexical, no model call). That is only a PRE-FILTER — same-task
#     follow-ups often share no words — so when it fires it records the
#     drifted prompts as `retitle_raw` in ~/.cache/agent-tab/drift/<sid>, the
#     hook records the label then on screen as `retitle_prev`, and the
#     condenser JUDGES: keep that label, or name the new task (one cached
#     call per trigger, see the condense mode).
# The detector is its own file so an offline calibration replay runs the
# exact same code; its header documents the model and the state format.

DRIFT_DIR="$HOME/.cache/agent-tab/drift"
case "${BASH_SOURCE[0]}" in
    */*) DRIFT_AWK="${BASH_SOURCE[0]%/*}/agent-topic-drift.awk" ;;
    *)   DRIFT_AWK="./agent-topic-drift.awk" ;;
esac
DRIFT_AWK="${AGENT_TAB_DRIFT_AWK:-$DRIFT_AWK}"

# claude_fields PAYLOAD: one jq for every payload field the claude title path
# needs. Sets P_SID (validated [0-9a-fA-F-], else empty), P_TP (transcript
# path), P_STITLE (session_title), P_PROMPT (first 1200 chars, one line).
# Memoized: the running hook fills these before extract_summary's subshell.
P_DONE=0; P_SID=""; P_TP=""; P_STITLE=""; P_PROMPT=""
claude_fields() {
    [ "$P_DONE" = 1 ] && return 0
    P_DONE=1
    local out
    out=$("$JQ" -r 'def one: tostring | gsub("[\\r\\n\\t]"; " ");
        (.session_id // "" | one), (.transcript_path // "" | one),
        (.session_title // "" | one), (.prompt // "" | tostring | .[0:1200] | one)' \
        <<<"$1" 2>/dev/null || true)
    { IFS= read -r P_SID; IFS= read -r P_TP; IFS= read -r P_STITLE; IFS= read -r P_PROMPT; } <<<"$out" || true
    case "$P_SID" in *[!0-9a-fA-F-]*) P_SID="" ;; esac
}

# claude_titles: CT_AI / CT_CUSTOM = the LAST ai-title and custom-title in the
# transcript's last 64KB, from one tail + one grep + one jq (the ai-title-only
# read it replaces was four forks). Both lines are re-appended over time, but
# NOT reliably within 64KB of the end: measured, 41 prompts in 10 of 59
# sessions had their last title line further back (up to 1.5MB). So an empty
# answer here is "not in the tail", not "none" — extract_summary falls back to
# the drift state's seen_custom / seen_ai. Memoized like claude_fields (needs
# it first).
CT_DONE=0; CT_AI=""; CT_CUSTOM=""
claude_titles() {
    [ "$CT_DONE" = 1 ] && return 0
    CT_DONE=1
    { [ -n "$P_TP" ] && [ -f "$P_TP" ]; } || return 0
    local out
    out=$(tail -c 65536 "$P_TP" 2>/dev/null | grep -E '"type":"(ai|custom)-title"' \
        | "$JQ" -Rrn 'def one: tostring | gsub("[\\r\\n\\t]"; " ");
            reduce (inputs | fromjson? | objects) as $o ({a: "", c: ""};
              if $o.type == "ai-title" then .a = ($o.aiTitle // "" | one)
              elif $o.type == "custom-title" then .c = ($o.customTitle // "" | one)
              else . end) | .a, .c' 2>/dev/null || true)
    { IFS= read -r CT_AI; IFS= read -r CT_CUSTOM; } <<<"$out" || true
}

# drift_read: this session's drift state (format: agent-topic-drift.awk's
# header) into DR_OK (1 = a state exists), DR_RAW / DR_CUSTOM / DR_PREV
# (retitle_raw / _custom / _prev), DR_LAUNCH (launch_custom), DR_SEEN_CUSTOM,
# DR_SEEN_AI. Builtins only — it runs on every claude event.
drift_read() {
    DR_OK=0; DR_RAW=""; DR_CUSTOM=""; DR_PREV=""; DR_LAUNCH=""; DR_SEEN_CUSTOM=""; DR_SEEN_AI=""
    { [ -n "$P_SID" ] && [ -f "$DRIFT_DIR/$P_SID" ]; } || return 0
    local k v
    while IFS=$'\t' read -r k v || [ -n "$k" ]; do
        case "$k" in
            v) [ "$v" != 1 ] || DR_OK=1 ;;
            retitle_raw) DR_RAW="$v" ;;
            retitle_custom) DR_CUSTOM="$v" ;;
            retitle_prev) DR_PREV="$v" ;;
            launch_custom) DR_LAUNCH="$v" ;;
            seen_custom) DR_SEEN_CUSTOM="$v" ;;
            seen_ai) DR_SEEN_AI="$v" ;;
        esac
    done < "$DRIFT_DIR/$P_SID" 2>/dev/null || true
}

# drift_update (running, claude): feed this prompt to the detector and replace
# the session's state atomically (tmp + mv). Cost on top of the hook: the awk
# and the mv (claude_fields' jq replaces reads extract_summary made anyway);
# once per session, when the state is created, a head|grep|jq for the first
# custom-title; and only on a prompt that fires a retitle, one tmux call plus
# a sanitize: the condensed label on screen then is what the condenser judges
# against (retitle_prev, appended to the new state here — the awk carries it
# through later runs).
# LC_ALL=C: macOS awk byte-slices with substr but decodes UTF-8 in tolower/
# match, and exits 2 on a cut that split a character (the prompt was then
# silently dropped). Best-effort throughout: the caller adds `|| true`, and
# any failure leaves the old state (or none) in place — never the hook's
# state write.
drift_update() {
    { [ "$agent" = claude ] && [ -n "$JQ" ] && [ -n "$payload" ] && [ -r "$DRIFT_AWK" ]; } || return 0
    claude_fields "$payload"
    [ -n "$P_SID" ] || return 0
    claude_titles
    local f="$DRIFT_DIR/$P_SID" in=/dev/null tmp now verdict="" prev launch="" ab=/usr/bin/awk
    [ -x "$ab" ] || ab="awk"
    [ -d "$DRIFT_DIR" ] || mkdir -p "$DRIFT_DIR" 2>/dev/null || return 0
    tmp="$f.tmp.$$"
    if [ -f "$f" ]; then
        in="$f"
    elif [ -n "$P_TP" ] && [ -f "$P_TP" ]; then
        # Creating the state: launch_custom must be the custom-title the
        # session STARTED with (a Tasks launcher slug sits at line 2), and in a
        # long session it is far outside the tail by now. Once per session.
        launch=$(head -c 8192 "$P_TP" 2>/dev/null | grep -m1 -F '"type":"custom-title"' \
            | "$JQ" -Rr 'fromjson? | .customTitle // empty | tostring | gsub("[\\r\\n\\t]"; " ")' \
            2>/dev/null || true)
    fi
    printf -v now '%(%s)T' -1 2>/dev/null || now=$(now_epoch)
    if verdict=$(LC_ALL=C DRIFT_PROMPT="$P_PROMPT" DRIFT_TITLE="${CT_CUSTOM:-${CT_AI:-$P_STITLE}}" \
            DRIFT_CUSTOM="$CT_CUSTOM" DRIFT_AI="$CT_AI" DRIFT_LAUNCH="$launch" \
            "$ab" -v now="$now" -v state_out="$tmp" -f "$DRIFT_AWK" "$in" 2>/dev/null) \
        && [ -s "$tmp" ]; then
        case "$verdict" in
            *RETITLE*)
                # Only a CONDENSED label is worth judging against: a raw
                # stand-in (@agent_summary_cond unset) handed to "keep it if
                # it's the same work" would be locked in. One tmux call, two
                # reads; -q prints nothing for an unset option, so exactly
                # "1\n<label>" means condensed and set.
                prev=$(tmux show-options -wqv -t "$win" @agent_summary_cond \; \
                            show-options -wqv -t "$win" @agent_summary 2>/dev/null || true)
                case "$prev" in
                    1$'\n'?*) prev="${prev#1$'\n'}" ;;
                    *) prev="" ;;
                esac
                case "$prev" in */*) prev="${prev#*/}" ;; esac
                # The birth/SessionStart placeholder names nothing to keep.
                [ "$prev" != "New Session" ] || prev=""
                # Sanitized once here; extract_summary uses it as stored.
                [ -z "$prev" ] || prev=$(printf '%s' "$prev" | sanitize_summary || true)
                [ -z "$prev" ] || printf 'retitle_prev\t%s\n' "$prev" >> "$tmp" 2>/dev/null || true
                ;;
        esac
        mv -f "$tmp" "$f" 2>/dev/null || rm -f "$tmp" 2>/dev/null || true
    else
        rm -f "$tmp" 2>/dev/null || true
    fi
}

# Conversation title, best source first. Emits "<src>\t<title>", where <src>
# distinguishes the agent's own conversation title (`final`) from the turn's
# prompt standing in until that title exists (`interim`) — both condense, the
# prompt at a longer bound — a user-blessed /rename title (`custom`), which
# compose_summary shows as written when it fits the title budget and has the
# condenser shorten when it doesn't, and a drift retitle, emitted as
# "retitle\t<label on screen when it fired>\t<drifted prompts>" for the
# condenser to judge. Empty output = no title at all.
# A tab is a safe delimiter: sanitize_summary maps tabs to spaces.
#   claude: the custom-title, if it is a user rename, against a drift retitle
#           by recency: the rename wins unless a retitle fired after it (the
#           retitle records the custom-title in force when it fired; a
#           different one now means the user renamed since). Otherwise the
#           retitle if there is one, else the latest ai-title, else
#           session_title, else the turn's prompt. Title lines missing from
#           the 64KB tail fall back to the drift state's seen_custom/seen_ai.
#           A custom-title is a user rename only if it differs from
#           launch_custom, the one the session already had when its drift
#           state was created: CuaNotch's Tasks launcher writes a 24-char
#           slug as custom-title at line 2 (no ai-title), which is not the
#           user's naming. With no state yet (a SessionStart before the
#           first prompt) it can't be told apart and is not trusted; a real
#           /rename comes after a prompt, so its state exists.
#   codex:  threads.name from ~/.codex/state_5.sqlite keyed by session_id
#           (0.148 moved thread metadata off session_index.jsonl), else
#           threads.title (first user message), else the prompt.
#   opencode: the payload's session_title unless it is OpenCode's
#           "New session - <date>" placeholder, else the prompt.
extract_summary() {
    local payload="$1" title="" src="final" prev=""
    [ -n "$JQ" ] || return 0
    [ -n "$payload" ] || return 0

    case "$agent" in
        claude)
            claude_fields "$payload"
            claude_titles
            drift_read
            local cust="${CT_CUSTOM:-$DR_SEEN_CUSTOM}" ai="${CT_AI:-$DR_SEEN_AI}" blessed=0
            if [ "$DR_OK" = 1 ] && [ -n "$cust" ] && [ "$cust" != "$DR_LAUNCH" ]; then
                blessed=1
            fi
            if [ "$blessed" = 1 ] && { [ -z "$DR_RAW" ] || [ "$cust" != "$DR_CUSTOM" ]; }; then
                title="$cust"; src="custom"
            elif [ -n "$DR_RAW" ]; then
                title="$DR_RAW"; src="interim"
                # retitle_prev was sanitized when drift_update stored it; a tab
                # (only a corrupt file could hold one) would break the format.
                prev="${DR_PREV//$'\t'/ }"
                [ -z "$prev" ] || src="retitle"
            elif [ -n "$ai" ]; then
                title="$ai"
            elif [ -n "$P_STITLE" ]; then
                title="$P_STITLE"
            else
                title="$P_PROMPT"; src="interim"
            fi
            ;;
        codex)
            # codex ≥0.148 keeps thread metadata in sqlite (session_index.jsonl
            # is no longer written): threads.name is the model-written thread
            # title (lands after the first turn), threads.title is the first
            # user message — a prompt-grade stand-in, so it condenses like one.
            local session_id db="$HOME/.codex/state_5.sqlite"
            session_id=$("$JQ" -r '.session_id // empty' <<<"$payload" 2>/dev/null || true)
            case "$session_id" in *[!0-9a-fA-F-]*) session_id="" ;; esac
            if [ -n "$session_id" ] && [ -f "$db" ] && command -v sqlite3 >/dev/null 2>&1; then
                title=$(sqlite3 -readonly "$db" \
                    "select coalesce(nullif(name,''),'') from threads where id='$session_id'" \
                    2>/dev/null || true)
                if [ -z "$title" ]; then
                    title=$(sqlite3 -readonly "$db" \
                        "select title from threads where id='$session_id'" \
                        2>/dev/null || true)
                    [ -n "$title" ] && src="interim"
                fi
            fi
            if [ -z "$title" ]; then
                title=$("$JQ" -r '.prompt // empty' <<<"$payload" 2>/dev/null || true)
                src="interim"
            fi
            ;;
        opencode)
            # The plugin sends the session's title (OpenCode generates it
            # after the first turn) and the turn's prompt. Until then the
            # title is OpenCode's placeholder "New session - <ISO date>"
            # ("Child session - …" for subagents), which names nothing: skip
            # it (the plugin already does; this is the backstop).
            title=$("$JQ" -r '.session_title // empty | tostring
                | if test("^(New|Child) session - [0-9]{4}-") then "" else . end' \
                <<<"$payload" 2>/dev/null || true)
            if [ -z "$title" ]; then
                title=$("$JQ" -r '.prompt // empty' <<<"$payload" 2>/dev/null || true)
                src="interim"
            fi
            ;;
    esac

    # An agent-written title is already short and is displayed as-is if the
    # condense fails; a prompt is a whole sentence whose first 60 chars can cut
    # off the identifying words the condenser exists to find, so give it room.
    # Display is unaffected — compose_summary trims to 24 chars either way.
    if [ "$src" = "interim" ] || [ "$src" = "retitle" ]; then
        title=$(printf '%s' "$title" | sanitize_summary 200 || true)
    else
        title=$(printf '%s' "$title" | sanitize_summary || true)
    fi
    [ -n "$title" ] || return 0
    if [ "$src" = "retitle" ]; then
        printf '%s\t%s\t%s' "$src" "$prev" "$title"
    else
        printf '%s\t%s' "$src" "$title"
    fi
}

# --- sidebar detail: @agent_detail_kind + @agent_detail ---------------------
#
# What the event that set the state was ABOUT, for a sidebar to show next to
# it. Kinds: perm (a permission gate: tool + the identifying part of its
# input), ask (AskUserQuestion: the first question), fail (StopFailure: the
# error), done (Stop: first line of the final assistant message), run
# (UserPromptSubmit: first line of the prompt). Written with the state in one
# tmux command list by set_state; unset by SessionStart and clear_state.
#
# The text is untrusted (a prompt, a model reply, a tool input), and it only
# ever reaches tmux as a set-option ARGV element: set-option stores it
# verbatim and `#{@agent_detail}` expands it without re-parsing formats inside
# it (verified on a scratch server, tmux 3.7c). The sanitizer still matches
# @agent_summary's — one line, no '#', '"', '%', no control characters,
# whitespace collapsed — plus one tmux-argv rule: an argument ENDING in ';'
# terminates the command list (tmux strips it, and turns a trailing '\;' into
# ';'), so trailing semicolons are dropped. Invisible/bidi controls (zero-
# widths, LRE..RLO, LRI..PDI, BOM, ALM) become spaces, so a payload can't
# reorder or hide sidebar text. Bounded to 80 characters with a trailing '…'.
# Done in jq, which the payload already needs: it slices by code point (a
# byte cut can split a UTF-8 sequence) and the whole extraction is ONE fork
# per event.
# EVERY value is sliced BEFORE any per-character work: str caps at 4000 code
# points, clean at 1000. Unbounded, explode|map|implode|gsub was quadratic in
# the input — a 152 KB heredoc Bash command took 16 s, 1 MB over five
# minutes — and PermissionRequest/UserPromptSubmit block the user's dialog /
# prompt (codex kills a hook at 10 s, losing the state write with it). Bounded,
# the cost is jq's linear parse of the payload, which this hook already pays
# in its other jq reads; a jq failure yields no output and take_detail falls
# back to the bare kind, so a detail problem can never cost the state write.
# Output: "<kind>\t<text>" — or "note\t<text>" for a Notification that names
# no tool (see needs-approval).
DETAIL_JQ='
def invisible: (. >= 8203 and . <= 8207) or (. >= 8234 and . <= 8238)
  or (. >= 8288 and . <= 8297) or . == 65279 or . == 1564 or . == 8232 or . == 8233;
def clean:
  tostring | .[0:1000] | explode
  | map(if . < 32 or (. >= 127 and . < 160) or invisible then 32
        elif . == 34 or . == 35 or . == 37 then empty else . end)
  | implode | gsub("\\s+"; " ") | sub("^[ ;]+"; "") | sub("[ ;]+$"; "")
  | if length > 80 then (.[0:79] | sub("[ ;]+$"; "")) + "…" else . end;
def str: (if type == "string" then . elif type == "array" then .[0:200] | map(tostring) | join(" ")
          elif . == null then "" else tostring end) | .[0:4000];
def firstline: str | split("\n") | map(select(test("\\S"))) | (first // "");
def rel($cwd): (env.HOME // "") as $h
  | if $cwd != "" and startswith($cwd + "/") then .[($cwd | length) + 1:]
    elif $h != "" and startswith($h + "/") then "~/" + .[($h | length) + 1:]
    else . end;
def toolarg($cwd):
  if type != "object" then str
  elif (.command // .cmd) != null then (.command // .cmd) | str
  elif .file_path != null then .file_path | str | rel($cwd)
  elif .notebook_path != null then .notebook_path | str | rel($cwd)
  elif .url != null then .url | str | sub("^[A-Za-z][A-Za-z0-9+.-]*://"; "") | sub("^www\\."; "")
  elif .pattern != null then .pattern | str
  elif .path != null then .path | str | rel($cwd)
  elif .query != null then .query | str
  elif .description != null then .description | str
  elif .skill != null then .skill | str
  elif .prompt != null then .prompt | firstline
  else [.[] | select(type == "string" and . != "")] | (first // "") end;
def detail($mode):
  (.cwd // "" | str) as $cwd
  | if $mode == "running" then "run\t" + (.prompt | firstline | clean)
    elif $mode == "needs-approval" then
      (.tool_name // "" | str) as $tool
      | if $tool == "AskUserQuestion" then
          "ask\t" + (((try .tool_input.questions[0].question catch null)
                      // (try .tool_input.question catch null) // "") | firstline | clean)
        elif $tool != "" then
          "perm\t" + ([$tool, (.tool_input | toolarg($cwd))] | map(select(. != "")) | join(" ") | clean)
        else
          "note\t" + (.message // "" | str
                      | (capture("permission to use (?<t>.+)$").t // .) | clean)
        end
    elif $mode == "failed" then
      "fail\t" + ([(.error | if type == "object" then (.message // .type // "") else str end),
                   (.error_details | str)]
                  | map(select(. != "")) | join(": ") | firstline | clean)
    elif $mode == "done" then "done\t" + (.last_assistant_message | firstline | clean)
    else empty end;
'

# Emits "<kind>\t<text>" for $1 from the global $payload (empty on no jq/
# payload; the caller then falls back to the mode's bare kind). Claude's Stop
# payload carries last_assistant_message (captured from 2.1.294; codex sends
# the same field); an older Claude without it gets the transcript tail that
# extract_summary already reads — bounded to 64KB, only on that fallback.
detail_for() {
    local mode="$1" out="" tp
    if [ -n "$JQ" ] && [ -n "$payload" ]; then
        out=$("$JQ" -r --arg mode "$mode" "$DETAIL_JQ"'detail($mode)' <<<"$payload" 2>/dev/null || true)
    fi
    if [ "$mode" = done ] && [ "$agent" = claude ] && [ -n "$JQ" ] && [ -z "${out#*$'\t'}" ]; then
        tp=$("$JQ" -r '.transcript_path // empty' <<<"$payload" 2>/dev/null || true)
        if [ -n "$tp" ] && [ -f "$tp" ]; then
            out=$(tail -c 65536 "$tp" 2>/dev/null | grep -F '"type":"assistant"' \
                | "$JQ" -Rrn "$DETAIL_JQ"'[inputs | fromjson?
                    | [.message.content[]? | select(.type? == "text") | .text]
                    | select(length > 0) | join("\n")]
                  | {last_assistant_message: (last // "")} | detail("done")' 2>/dev/null || true)
        fi
    fi
    printf '%s' "$out"
}

# Sets globals dkind/dtext for set_state: detail_for's answer, else $2 (the
# mode's kind) with empty text; empty text falls back to $3.
take_detail() {
    local out
    out=$(detail_for "$1")
    if [ -n "$out" ]; then
        dkind="${out%%$'\t'*}"; dtext="${out#*$'\t'}"
    else
        dkind="$2"; dtext=""
    fi
    [ -n "$dtext" ] || dtext="${3:-}"
}

# ------------------------------------------------------------ entry modes

# Focus hook: attention discharged for the window the user just selected.
# The after-select-window hook passes the selected #{window_id} as $2 —
# an untargeted display-message resolves to the most-recently-active
# CLIENT, which can be a different one when several clients are attached.
if [ "$mode" = "clear-current" ]; then
    win="${2:-}"
    if [ -z "$win" ]; then
        win=$(tmux display-message -p '#{window_id}' 2>/dev/null || true)
    fi
    [ -n "$win" ] || exit 0
    cc_state=$(window_state "$win")
    # AGENT-ORIGINATED TURNS: while an agent-started turn runs, the window
    # holds the attention state it had before (@agent_prior_state) for a quiet
    # finish to restore. Mack has now looked at the window, so that state is
    # seen: downgrade a saved done/needs-input/failed to idle (stamped seen),
    # or the quiet restore would re-tint something he already looked at. One
    # extra read, only for a window that is mid-turn or idle (prior is never
    # set otherwise).
    # A `paused` window can hold one too (kept across the pause for after the
    # resume); `paused` itself is never discharged by a look.
    case "$cc_state" in
        running|idle|paused|needs-*)
            case "$(tmux show-options -wqv -t "$win" @agent_prior_state 2>/dev/null || true)" in
                done|needs-input|failed)
                    tmux set-option -w -t "$win" @agent_prior_state idle \; \
                         set-option -w -t "$win" @agent_seen_at "$(now_epoch)" 2>/dev/null || true
                    ;;
            esac ;;
    esac
    # ASKS WHILE WORK IS OUT: a raised ask (@agent_ask_bg) is seen now. Its
    # line becomes @agent_ask_seen (copied tmux-side, in the discharge's own
    # command list); with the work still out (@agent_workflow, the
    # watcher's) the window goes back to the `running` it was held at, the
    # detail kind no longer `ask`. One read, needs-input only. The line is
    # kept as seen only while the detail still IS the ask (kind `ask`).
    # @agent_ask_bg counts when non-empty (`1`, or a counted `1 N`); a read
    # without both separators is no answer at all.
    cc_ab=""; cc_wf=""; cc_dk=""; cc_rest=""
    case "$cc_state" in
        needs-*) cc_rest=$(tmux display-message -p -t "$win" "#{@agent_ask_bg}${US}#{@agent_workflow}${US}#{@agent_detail_kind}" 2>/dev/null || true) ;;
    esac
    if [[ $cc_rest == *"$US"*"$US"* ]]; then
        IFS="$US" read -r cc_ab cc_wf cc_dk <<<"$cc_rest" || true
    fi
    if [ -n "$cc_ab" ]; then
        [ "$cc_dk" != ask ] || ss_add set-option -w -t "$win" -F @agent_ask_seen '#{@agent_detail}'
        if [ -n "$cc_wf" ]; then
            seen_op=1
            ss_add set-option -uw -t "$win" @agent_detail_kind
            set_state "$win" running
            exit 0
        fi
    fi
    case "$cc_state" in
        needs-*)
            # The user came to answer the prompt. Discharge the tint, but
            # stamp @agent_pending so the next heartbeat can restore
            # `running` once the answered turn resumes — the discharge
            # happens BEFORE any post-answer heartbeat, so heartbeat's own
            # needs-input re-arm can never fire in this flow. The stamp rides
            # in set_state's own command (pend_op): idle + pending keeps the
            # window's agent slot (for up to 600 s, see SLOT CORE), and must
            # never be seen half-written.
            pend_op=set; seen_op=1
            set_state "$win" idle
            [ "$ss_wrote" = 1 ] || tmux set-option -w -t "$win" @agent_pending "$(now_epoch)" 2>/dev/null || true
            ;;
        # A seen FAILED gets no pending: a dead turn never resumes (no
        # heartbeat follows it; a new prompt sets running by itself), and the
        # stamp would only hold the window's agent slot for nothing.
        failed|done) seen_op=1; set_state "$win" idle ;;
    esac
    exit 0
fi

# Detached condenser (spawned by compose_summary): distill the raw title to
# its 2-4 identifying words with a one-shot copilot/haiku call, cache it,
# update the tab. argv: condense <window_id> <project> <raw-title> [<prev>].
# With <prev> (a topic-drift retitle: <raw-title> is the drifted prompts,
# <prev> the label on screen when it fired) the model JUDGES instead: the
# label back unchanged if the prompts are still the same work, a new label
# only for a clearly different task. The lexical detector fires on plenty of
# same-task follow-ups that share no words; this call is what rejects them.
# Cached under the composed input, so it is one call per trigger.
# <kind> custom (argv 6): a /rename title, shown verbatim when it fits the
# title budget (no model call), else shortened to fit it. Every new label is
# held to the budget (title_budget: the sidebar card's room), in the prompt
# and in valid_short. Never invoked by hooks directly, so a slow/failed model
# call only delays the title swap.
if [ "$mode" = "title-budget" ]; then
    title_budget; echo
    exit 0
fi

if [ "$mode" = "condense" ] || [ "$mode" = "condense-locked" ]; then
    win="${2:-}"; proj="${3:-}"; raw="${4:-}"; prev="${5:-}"; kind="${6:-}"
    { [ -n "$win" ] && [ -n "$raw" ]; } || exit 0
    [ "$kind" != custom ] || prev=""
    kin="$raw"   # = compose_summary's
    [ -z "$prev" ] || kin="retitle"$'\n'"$prev"$'\n'"$raw"
    [ "$kind" != custom ] || kin="custom"$'\n'"$raw"
    key=$(title_key "$kin")
    if [ "$mode" = "condense" ]; then
        # A mkdir lock survives SIGKILL and then rejects this title forever.
        # Keep the advisory-lock inode permanently; the kernel releases its
        # lock when the worker exits, even if no shell trap can run. The new
        # suffix also bypasses orphaned legacy .lock directories safely.
        exec python3 - "${TMPDIR:-/tmp}/agent-tab-condense.$key.flock" "$0" "$win" "$proj" "$raw" "$prev" "$kind" <<'PYLOCK'
import fcntl
import os
import sys

fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(0)
os.set_inheritable(fd, True)
os.execvp("bash", ["bash", sys.argv[2], "condense-locked"] + sys.argv[3:])
PYLOCK
    fi
    # Each worker owns its stderr file. A killed worker may leave diagnostics,
    # but no stale directory can block the next attempt.
    WORK=$(mktemp -d "${TMPDIR:-/tmp}/agent-tab-condense.$key.XXXXXX") || exit 0
    trap 'rm -rf "$WORK" 2>/dev/null' EXIT
    trap 'exit 0' INT TERM
    # SUMMARY KEY (compose_summary): paint only while the window still wants
    # this condense. One show-options, in the detached worker only.
    paint() {
        [ "$(tmux show-options -wqv -t "$win" @agent_summary_key 2>/dev/null || true)" = "$key" ] || return 0
        set_summary "$win" "${proj:+$proj/}$1" 1
    }

    short=$(cached_short_key "$key")
    if [ -z "$short" ]; then
        # Back off if a recent attempt for this title failed, so a persistent
        # copilot error (not logged in, out of credit) isn't re-run every turn.
        negative_fresh "$key" && exit 0
        budget=$(title_budget)

        if [ "$kind" = custom ]; then
            disp=$(custom_words "$raw")
            if [ "${#disp}" -le "$budget" ]; then
                # It fits: the user's own words, no model call.
                cache_put "$key" "$disp"
                paint "$disp"
                exit 0
            fi
        fi

        # gtimeout is coreutils' name when /usr/bin/timeout is absent (macOS).
        TO=$(command -v timeout || command -v gtimeout || true)
        if [ "$kind" = custom ]; then
            PROMPT="The user named this coding session: $disp
Shorten it to a tab label of at most $budget characters (1-4 words) that keeps its meaning and its most distinctive words. Output ONLY the label - no punctuation, quotes, or explanation."
        elif [ -n "$prev" ]; then
            PROMPT="Current tab label: $prev
The user's latest messages: $raw

If these messages are still about the same work as the label (follow-ups, details, sub-steps, feedback, logistics), output the label EXACTLY as given. Only if the user has clearly moved on to a different task or subject, output a new 2-4 word label of at most $budget characters for the new task, built from the MOST SPECIFIC, distinctive words: the concrete subject, target, feature, file, tech, or action unique to it. Keep proper nouns and real names. DROP generic filler (project, code, app, task, session, various, thing, stuff, update, changes, improve, and a bare fix/add/refactor when what it acts on is unnamed). Output ONLY the label - no punctuation, quotes, or explanation."
        else
        PROMPT="From the coding-session title or first message below, output a 2-4 word tab label of at most $budget characters, built from the MOST SPECIFIC, distinctive words in it: the concrete subject, target, feature, file, tech, or action unique to THIS task. Keep proper nouns and real names. DROP generic filler (project, code, app, task, session, various, deep, inspection, thing, stuff, update, changes, improve, and a bare fix/add/refactor when what it acts on is unnamed). Someone reading the label should be able to guess the task back. Prefer the unusual identifying word over the category word. Output ONLY the label - no punctuation, quotes, or explanation.

Examples:
- 'deep inspection of this project docs, I wanna open source it, make sure nothing reads like AI or oddly specific to me' => Open-source doc cleanup
- 'refactor the auth middleware to use JWTs instead of session cookies' => JWT auth refactor
- 'figure out why the websocket reconnect drops messages under load' => Websocket reconnect bug

Title: $raw"
        fi
        # ai-exec runs copilot -> codex -> claude (CONDENSE_ORDER) and exits 0
        # if any of them answered; the answer is in -o. The codex flags pass
        # through verbatim (as closed-tabs.sh's): --ephemeral keeps it out of
        # codex's thread history, --ignore-user-config skips MCP servers and
        # notify, hooks off so the call never shows up as an agent in a tab or
        # the notch. ai-exec runs the claude/copilot legs with no user settings
        # and CUA_NOTCH_IGNORE=1 for the same reason. Every leg runs in the
        # empty $WORK, read-only; the prompt goes in on stdin. Capture the exit
        # code explicitly: `|| true` would accept a failed chain.
        ok=0
        aiexec=$(ai_exec_bin)
        if [ -n "$aiexec" ] && printf '%s' "$PROMPT" | (
                cd "$WORK" || exit 1
                unset TMUX TMUX_PANE
                AI_EXEC_ORDER="$CONDENSE_ORDER" exec ${TO:+"$TO" "$CONDENSE_TIMEOUT"} \
                    "$aiexec" exec -m "$CONDENSE_CODEX_MODEL" -c model_reasoning_effort=low \
                    --ephemeral --skip-git-repo-check --ignore-user-config --ignore-rules \
                    --disable hooks -s read-only -o "$WORK/out" \
                    --claude-model "$CONDENSE_CLAUDE_MODEL" --copilot-model "$CONDENSE_MODEL" -
            ) >/dev/null 2>&1 && [ -s "$WORK/out" ]; then
            out=$(cat "$WORK/out")
            ok=1
        fi

        if [ "$ok" = 1 ]; then
            # `|| true`: with set -e + pipefail, a completion that is empty or
            # newlines-only makes grep -m1 exit 1 and kills the whole script
            # here — before the negative cache entry below is written, so
            # nothing throttles the next attempt and the condenser respawns
            # in a loop (silently: the child is detached to /dev/null).
            line=$(printf '%s' "$out" | grep -m1 . | sanitize_summary || true)
            if [ -n "$prev" ] && { [ "${line%.}" = "$prev" ] ||
                [ "$(printf '%s' "${line%.}" | tr '[:upper:]' '[:lower:]')" = \
                  "$(printf '%s' "$prev" | tr '[:upper:]' '[:lower:]')" ]; }; then
                # "Same work": keep the label exactly, even one valid_short
                # would refuse as a new answer (a 5-word raw stand-in) — the
                # judge kept it, and blanking it would be worse.
                short="$prev"
            else
                short=$(printf '%s' "$line" | cut -d' ' -f1-4 || true)
                short=$(fit_words "$short" "$budget")
                valid_short "$short" "$budget" || short=""
            fi
        else
            short=""
        fi

        if [ -n "$short" ]; then
            cache_put "$key" "$short"
        else
            cache_put "$key" ""   # negative entry → NEG_TTL backoff
            exit 0
        fi
    fi
    [ -n "$short" ] || exit 0
    paint "$short"
    exit 0
fi

# Everything below is an agent hook: resolve the agent's window from the
# pane the hook inherited. No TMUX_PANE = the hook has no pane identity
# (a ChatGPT-app codex thread, a background worker with a scrubbed env).
# The old fallback resolved an untargeted display-message to the *currently
# active* window, which painted a foreign agent's state and title onto
# whatever tab the user happened to be looking at. No pane, no tab.
pane="${TMUX_PANE:-}"
[ -n "$pane" ] || exit 0
win=$(tmux display-message -p -t "$pane" '#{window_id}' 2>/dev/null || true)
[ -n "$win" ] || exit 0

if [ "$agent" = codex ]; then
    # Ownership may have changed since resolve (or while a detached title
    # condenser was working). Never let an old hook reclaim the window.
    # owner_current is this hook's own resolve (OWNER_MEMO), not a second one.
    owner_current || exit 0
    previous_sid=$(tmux show-options -wqv -t "$win" @agent_session_id 2>/dev/null || true)
    if [ "$previous_sid" != "$AGENT_TAB_OWNER_SESSION" ]; then
        clear_state "$win"
    fi
    tmux set-option -w -t "$win" @agent_session_id "$AGENT_TAB_OWNER_SESSION" \; \
         set-option -w -t "$win" @agent_owner_token "$AGENT_TAB_OWNER_TOKEN" 2>/dev/null || exit 0
fi

# Claude-in-Claude (see A FOREIGN AGENT'S TOOL SHELL above): a `claude -p`
# run from another Claude's Bash tool carries no codex marker, so ask the
# process tree. nested_agent: walking up from this hook, is there an agent
# (claude/codex/opencode, or a native claude's version-number comm, as the
# watcher's AGENT_PAT) ABOVE a non-agent that is itself above the hook's own
# agent? That is agent -> tool shell -> agent: this hook's claude was started
# by another agent, whose tab TMUX_PANE names. An agent's direct agent parent
# (no shell between) is not a nesting. One ps, on the lifecycle events only:
# the heartbeat (every tool call) skips it - from a nested claude it is inert
# anyway, since the parent's window is running while its tool runs. Any ps
# failure reads as not nested (the old behavior). Bash 4+ (associative).
nested_agent() {
    [ "${BASH_VERSINFO[0]:-0}" -ge 4 ] || return 1
    local rows pid ppid comm p seen=0 gap=0 n=0
    local -A PP=() PC=()
    rows=$(ps -axo pid=,ppid=,comm= 2>/dev/null) || return 1
    while read -r pid ppid comm; do
        PP[$pid]="$ppid"; PC[$pid]="${comm##*/}"
    done <<<"$rows"
    p="$PPID"
    while [ -n "$p" ] && [ "$p" -gt 1 ] 2>/dev/null && [ "$n" -lt 64 ]; do
        n=$((n + 1))
        if [[ ${PC[$p]:-} =~ ^(claude|codex|opencode|[0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
            [ "$gap" = 1 ] && return 0
            seen=1
        elif [ "$seen" = 1 ]; then
            gap=1
        fi
        p="${PP[$p]:-}"
    done
    return 1
}
if [ "$agent" = claude ] && [ "$mode" != heartbeat ] && nested_agent; then
    exit 0
fi

# Claude heartbeat is the hot path (every tool call): don't wait on stdin — the
# payload includes tool_response, which can be megabytes. A backgrounded
# drain consumes the pipe so the writer never sees EPIPE (codex's tolerance
# for a hook that abandons its stdin is undocumented), without blocking us.
# Only re-arm running from running/needs-input — a late PostToolUse landing
# after Stop's `done` must NOT resurrect a finished tab. Codex reads its
# payload earlier to validate session ownership and reject subagents.
# The drain's `<&0` is load-bearing: without job control, bash points an
# async command's stdin at /dev/null unless it names one itself, so the bare
# `( cat & )` that stood here until 2026-10-21 drained nothing at all (the
# writer got its EPIPE after all; Claude shrugs it off).
#
# ASKER GATE (2026-10-21). PostToolUse fires for EVERY agent in the session,
# subagents included, and this branch exits before the subagent guard below
# ever sees the payload. So a background subagent's tool calls re-armed
# `running` over the main agent's open AskUserQuestion (live: window @15
# went needs-input/ask at 09:37, `running` at 09:37:55 with @agent_workflow
# set and the question still on screen; the tab and sidebar said "working" -
# CuaNotch too, but via its own hook, which this does not reach). The fix
# is bounded to needs-input, the rare state: there the
# heartbeat peeks at the payload and re-arms only for the agent that asked
# (@agent_ask_from, written by needs-approval: the PermissionRequest's
# agent_id, or "main"). The asker's own next tool call is the gate
# answering - an approved tool's PostToolUse, or AskUserQuestion's own.
# A subagent's gate is NOT re-armed by the main agent either: a background
# subagent can sit on its prompt while main keeps working (nor ended by
# main's Stop: see done). A gate with NO recorded asker (raised by the
# Notification alone, or yellow since before this) keeps the old rule - any
# tool call re-arms - since a gate nobody can end would stand yellow until
# the next prompt; CuaNotch's hook (0083845) rules the same way.
#   WHO: Claude Code builds every hook payload as {...base, hook_event_name,
# tool_name, tool_input, tool_response, ...} (2.1.306's wu(): session_id,
# transcript_path, cwd, scratchpad_dir, prompt_id, permission_mode,
# agent_id, agent_type, effort), and JSON.stringify keeps that insertion
# order, so a subagent's top-level agent_id sits in the first few hundred
# bytes, BEFORE "hook_event_name" and long before the megabytes of
# tool_response. jq can't parse a truncated document, so: `head -c 8192`,
# keep what precedes the first "hook_event_name", and match the key there.
# Cutting at hook_event_name is what makes the match the top-level key: a
# tool_input or tool_response OBJECT with its own "agent_id" key (an MCP
# tool's argument, say) comes after it; inside a JSON string every quote
# is escaped (\"agent_id\"), which the pattern can't match anyway. No
# agent_id = the main agent. Codex rejects subagent payloads up top and
# OpenCode sends none, so both are always "main" and behave as before.
# The payload is read only when the window is needs-input with an asker, or
# failed. Ending a held gate, and consuming a fresh @agent_pending, add one
# show-options (HELD DETAIL).
#   FAILED IS MAIN'S (2026-10-22). A StopFailure (529) kills the MAIN turn;
# background subagents keep working, and their PostToolUse (and
# PostToolUseFailure, wired to heartbeat) re-armed `running` over the red
# within seconds, so the failure was never seen - the same clobbering the
# asker gate stops for needs-input. So `failed` re-arms only for a main-agent
# tool call (same 8 KB peek); a new prompt or the wake-up turn clears it as
# before.
#   A running window is left alone without a set_state: nothing would be
# written, and the hot path skips even the call's state re-read.
hb_who() {
    hb_from=main
    [ "$agent" != codex ] || return 0
    hb_head=$(head -c 8192 2>/dev/null || true)
    hb_cut='"hook_event_name"'
    hb_head=${hb_head%%"$hb_cut"*}
    hb_re='"agent_id"[[:space:]]*:[[:space:]]*"([^"]*)"'
    if [[ $hb_head =~ $hb_re ]]; then
        hb_from="${BASH_REMATCH[1]//[^A-Za-z0-9_.-]/}"
        hb_from="${hb_from:-main}"
    fi
}
if [ "$mode" = "heartbeat" ]; then
    hb_state=$(window_state "$win")
    hb_ok=1
    case "$hb_state" in
        needs-*)
            hb_ask=$(tmux show-options -wqv -t "$win" @agent_ask_from 2>/dev/null || true)
            if [ -n "$hb_ask" ]; then
                hb_who
                [ "$hb_from" = "$hb_ask" ] || hb_ok=0
            fi
            ;;
        failed)
            hb_who
            [ "$hb_from" = main ] || hb_ok=0
            ;;
    esac
    [ "$agent" = codex ] || ( cat <&0 >/dev/null 2>&1 & ) 2>/dev/null
    case "$hb_state" in
        running) ;;
        failed) if [ "$hb_ok" = 1 ]; then set_state "$win" running; fi ;;
        needs-*)
            if [ "$hb_ok" = 1 ]; then
                # A subagent's gate the main agent's Stop held (see done):
                # its answer lands the window where that Stop would have,
                # running with the Stop's own detail.
                case "${hb_ask:-main}" in main) ;; *) held_detail_take "$win" ;; esac
                set_state "$win" running
            fi ;;
        idle)
            # idle + @agent_pending = an answered permission prompt's turn
            # resuming (see clear-current). Single-use and age-gated, so a
            # stray late tool call can't resurrect a tab that merely sat
            # idle; bare idle (no stamp) stays inert as before.
            # The consume rides in set_state's command (pend_op): running and
            # the unset land together, so the window's agent slot (held by
            # idle + pending) is never seen free in between.
            pend=$(tmux show-options -wqv -t "$win" @agent_pending 2>/dev/null || true)
            if [ -n "$pend" ]; then
                case "$pend" in *[!0-9]*) pend=0 ;; esac
                if [ "$(( $(now_epoch) - pend ))" -lt 3600 ]; then
                    pend_op=unset
                    held_detail_take "$win"   # a held Stop's, if its gate was seen
                    set_state "$win" running
                    [ "$ss_wrote" = 1 ] || clear_pending "$win"
                else
                    clear_pending "$win"
                fi
            fi
            ;;
    esac
    exit 0
fi

[ "$agent" = codex ] || payload=$(cat 2>/dev/null || true)

# subagent-stop (ASKER GATE): the asking subagent ENDING ends its gate, the
# way its own next tool call would - for the case that fires no heartbeat:
# it was denied and simply finished (an approved tool that FAILED fires
# PostToolUseFailure, which is wired to heartbeat). Mode for SubagentStop,
# wired in ~/.claude/settings.json (see the doc's Asker gate). Only a
# needs-input whose recorded
# asker is this agent_id moves, to `running` - the state a held Stop stands
# on, or main's turn still going - with a held Stop's stash (HELD DETAIL).
# Never a heartbeat: from failed or idle + pending it would claim a turn.
if [ "$mode" = subagent-stop ]; then
    [ "$agent" = claude ] && [ -n "$JQ" ] && [ -n "$payload" ] || exit 0
    sa_id=$("$JQ" -r '.agent_id // empty' <<<"$payload" 2>/dev/null || true)
    sa_id="${sa_id//[^A-Za-z0-9_.-]/}"
    [ -n "$sa_id" ] && [ "$(window_state "$win")" = needs-input ] || exit 0
    [ "$(tmux show-options -wqv -t "$win" @agent_ask_from 2>/dev/null || true)" = "$sa_id" ] || exit 0
    held_detail_take "$win"
    set_state "$win" running
    exit 0
fi

# Hooks also fire inside subagent contexts (payload carries agent_id); a
# subagent's Stop/heartbeat/turn events must not flip the main agent's tab.
# EXCEPT its PermissionRequest: a subagent's approval prompt is shown in the
# main session and blocks on the user exactly like the main agent's own, so
# it is the tab's needs-input. Dropping it left the tab on the Notification
# half of the same gate, which Claude sends seconds later (CuaNotch, which
# takes the PermissionRequest, went yellow ~5 s before the tab and
# Option-S, 2026-10-09).
if [ -n "$JQ" ] && [ -n "$payload" ] && [ "$mode" != needs-approval ]; then
    if [ -n "$("$JQ" -r '.agent_id // empty' <<<"$payload" 2>/dev/null || true)" ]; then
        exit 0
    fi
fi

if [ "$agent" != codex ] && owns_window "$win"; then
    # A new Claude/OpenCode session in the same window invalidates detached
    # Codex title writers even if the previous frontend has not exited yet.
    # owns_window: a hook carrying a Codex owner binding it cannot prove (a
    # leaked condenser environment) must leave the window alone entirely.
    tmux set-option -uw -t "$win" @agent_session_id 2>/dev/null || true
    tmux set-option -uw -t "$win" @agent_owner_token 2>/dev/null || true
fi

# Codex background threads (subagents, review/guardian workers, the Memory
# Writing Agent) fire the same hooks as the interactive thread, from the same
# process — same TMUX_PANE — under their own session_id. Under codex 0.147 the
# memory writer's UserPromptSubmit repainted the user's tab title ("Memory
# Writing"); 0.148 stopped hooking memory workers, but subagent threads remain.
# Two guards: the thread registry marks non-user threads (thread_source), and
# the memory writer's prompt opener is recognizable even before a row exists.
# A session with no row yet is presumed interactive (rows land within the
# first turn, and a wrong "interactive" guess only refreshes the tab early).
if [ "$agent" = codex ] && [ -n "$JQ" ] && [ -n "$payload" ]; then
    # Also by its cwd: codex 0.162's memory agent runs in ~/.codex/memories
    # and fires UserPromptSubmit too (seen in the agent-origin spike); it
    # must never touch the tab or the agent-origin bookkeeping. Same one jq.
    cx_head=$("$JQ" -r '(.cwd // "" | tostring | gsub("[\\r\\n]"; " ")),
                        (.prompt // "" | tostring | .[0:40] | gsub("[\\r\\n]"; " "))' \
              <<<"$payload" 2>/dev/null || true)
    case "${cx_head%%$'\n'*}" in
        "$HOME/.codex/memories"|"$HOME/.codex/memories/"*) exit 0 ;;
    esac
    case "${cx_head#*$'\n'}" in
        "You are a Memory Writing Agent"*) exit 0 ;;
    esac
    csid=$("$JQ" -r '.session_id // empty' <<<"$payload" 2>/dev/null || true)
    case "$csid" in *[!0-9a-fA-F-]*) csid="" ;; esac
    cdb="$HOME/.codex/state_5.sqlite"
    if [ -n "$csid" ] && [ -f "$cdb" ] && command -v sqlite3 >/dev/null 2>&1; then
        # Same one query also yields rollout_path, which the WATCHER needs to
        # tell a live codex turn from an interrupted one (codex fires no hook
        # on abort either, so `running` sticks exactly as it did for claude).
        # Claude publishes a status field in ~/.claude/sessions/<pid>.json;
        # codex has no such file and no pid→thread mapping the watcher could
        # follow, so the hook — which knows session_id — stashes the path here
        # for it. Tab-separated: rollout paths contain no tabs.
        crow=$(sqlite3 -readonly -separator "$(printf '\t')" "$cdb" \
            "select coalesce(thread_source,''),coalesce(rollout_path,'') from threads where id='$csid'" \
            2>/dev/null || true)
        tsrc="${crow%%	*}"
        croll="${crow#*	}"
        case "$tsrc" in ''|user) : ;; *) exit 0 ;; esac
        if [ -n "$croll" ] && [ "$croll" != "$crow" ]; then
            tmux set-option -w -t "$win" @agent_rollout "$croll" 2>/dev/null || true
        fi
    fi
fi

# Is the agent's own window currently being viewed by ≥1 client? window
# scope, so it's robust to multiple attached clients AND to detached sessions
# (an untargeted display-message would resolve to some other client's window;
# #{window_active} is 1 even for zero-client sessions — neither is correct).
watched=$(tmux display-message -p -t "$win" '#{window_active_clients}' 2>/dev/null || true)
case "$watched" in ''|*[!0-9]*) watched=0 ;; esac
# ...and is the user actually LOOKING? See viewing_now (with the helpers).

case "$mode" in
    interrupt)
        # The terminal bridge observes turn/completed with interrupted status;
        # the OpenCode plugin sees the turn end in MessageAbortedError (Esc).
        # Keep the conversation label, but discharge working/attention state.
        case "$agent" in codex|opencode) ;; *) exit 0 ;; esac
        # An interrupted agent-originated turn ends like a quiet one: back to
        # the attention state the window had before it (AGENT-ORIGINATED
        # TURNS), seen-it rules included.
        origin_read "$win"
        # RATE-LIMITED AGENTS: an interrupted turn is an outcome; it ends the
        # limit (and the limit's own pause flag) like a Stop does.
        limit_end "$win" "$O_LIMIT" "$O_LIFTED" "$O_PAUSED"
        [ "$LIM_UNPAUSED" = 0 ] || O_PAUSED=""
        if [ -n "$O_PAUSED" ]; then
            # Paused beats idle and the restore; flag re-checked in the same
            # command list, as the Stop does.
            ss_add if-shell -F -t "$win" '#{?#{@agent_paused},0,1}' \
                "set-option -w -t $win @agent_state idle"
            set_state "$win" paused
        elif [ -n "$O_PSTATE" ]; then
            prior_restore "$win"
            set_state "$win" "$R_STATE"
        else
            set_state "$win" idle
        fi
        clear_pending "$win"
        ;;
    reconcile)
        # A new thread's id reaches the terminal bridge after its first hook.
        # Recover the title/presence without downgrading a newer event's state.
        if [ -z "$(window_state "$win")" ]; then
            if [ "$("$JQ" -r '.hook_event_name // empty' <<<"$payload")" = UserPromptSubmit ]; then
                set_state "$win" running
            else
                set_state "$win" idle
            fi
        fi
        compose_summary "$win" "$(extract_summary "$payload")" "$payload"
        ;;
    idle)
        # SessionStart with source=compact fires mid-turn after auto-compaction;
        # don't downgrade a running turn.
        src=""
        if [ -n "$JQ" ] && [ -n "$payload" ]; then
            src=$("$JQ" -r '.source // empty' <<<"$payload" 2>/dev/null || true)
            [ "$src" = "compact" ] && exit 0
        fi
        dkind=-   # SessionStart: a new conversation has no detail yet
        prior_unset "$win"   # no turn is in flight in a fresh/resumed session
        # A fresh conversation (/clear, a new start) is not the paused one:
        # agent-pause.py would find that target gone at the resume and never
        # clear its flag, and every later Stop here would end `paused`. Drop
        # it. Not on `resume` (a tmux restore resumes the paused session
        # itself) and not on `compact` (skipped above).
        # A fresh conversation is not the rate-limited one either (RATE-LIMITED
        # AGENTS): its limit goes with the flag.
        case "$src" in
            clear|startup) ss_add set-option -uw -t "$win" @agent_paused
                           ss_add set-option -uw -t "$win" @agent_limit
                           ss_add set-option -uw -t "$win" @agent_limit_lifted ;;
        esac
        ss_add set-option -uw -t "$win" @agent_held_detail   # HELD DETAIL
        # ASKS WHILE WORK IS OUT: a new or resumed session (compact exited
        # above) has seen no ask of its own yet.
        ss_add set-option -uw -t "$win" @agent_ask_seen
        set_state "$win" idle
        clear_pending "$win"
        # Drift state is per session and only ever read for the live one:
        # drop what no session has touched in 30 days (best-effort).
        if [ "$agent" = claude ] && [ -d "$DRIFT_DIR" ]; then
            find "$DRIFT_DIR" -type f -mtime +30 -delete 2>/dev/null || true
        fi
        summary=$(extract_summary "$payload")
        if [ -n "$summary" ]; then
            compose_summary "$win" "$summary" "$payload"
        else
            # A fresh conversation (/clear, new startup) has no title yet —
            # show "<project>/New Session" as a placeholder until the first
            # turn generates a real title (set directly, not condensed). resume
            # keeps whatever title it had: that's still the right conversation.
            case "$src" in
                clear|startup)
                    proj=$(project_name "$payload")
                    # "-": no condense owns this label, so no worker still out
                    # for the previous conversation may paint over it.
                    set_summary "$win" "${proj:+$proj/}New Session" 0 -
                    ;;
            esac
        fi
        ;;
    running)
        # AGENT-ORIGINATED TURNS: who started this turn? See the section's
        # header for the options and docs/agent-tab-indicator.md for the
        # rules. One tmux read + one jq always; the prompt hash (jq|openssl)
        # only when a fresh @agent_inbound entry exists to match it against.
        origin_read "$win"
        ctx_from=""
        if [ -n "$JQ" ] && [ -n "$payload" ]; then
            tj=$("$JQ" -r "$TURN_JQ" <<<"$payload" 2>/dev/null || true)
        else
            tj=""
        fi
        t_src=""
        { IFS= read -r t_id; IFS= read -r t_pre; IFS= read -r t_sys; IFS= read -r t_src; } <<<"$tj" || true
        [ -n "$t_id" ] || t_id=-
        printf -v t_now '%(%s)T' -1 2>/dev/null || t_now=$(now_epoch)
        # Match the prompt against the pending inbound messages.
        v_ok=0; v_for=""; v_from=""; v_sha=""
        inb_keep=""; inb_changed=0
        ents=()
        IFS='|' read -ra ents <<<"$O_INB" || true
        fresh=()
        for i in ${ents[@]+"${!ents[@]}"}; do
            read -r ep sh fr fo _ <<<"${ents[i]}" || true
            if [[ $ep =~ ^[0-9]{1,18}$ ]] && [[ $sh =~ ^[0-9a-f]{40}$ ]] \
               && [ $((t_now - 10#$ep)) -le "$INBOUND_TTL" ] && [ $((10#$ep - t_now)) -le 60 ]; then
                fresh+=("$i")
            fi
        done
        m=-1
        if [ "${#fresh[@]}" -gt 0 ] && [ "$t_sys" != sys ]; then
            p_sha=$(prompt_sha1 raw)
            for i in "${fresh[@]}"; do
                read -r ep sh fr fo _ <<<"${ents[i]}" || true
                if [ -n "$p_sha" ] && [ "$sh" = "$p_sha" ]; then m=$i; break; fi
            done
            # tmux's paste-buffer (without -r) turns LF into CR, so a
            # multi-line message can reach the prompt with CR or CRLF line
            # ends while tmux-mcp hashed it with LF: try that form too, only
            # when the raw hash missed.
            if [ "$m" -lt 0 ]; then
                p_sha2=$(prompt_sha1 lf)
                if [ -n "$p_sha2" ] && [ "$p_sha2" != "$p_sha" ]; then
                    for i in "${fresh[@]}"; do
                        read -r ep sh fr fo _ <<<"${ents[i]}" || true
                        if [ "$sh" = "$p_sha2" ]; then m=$i; p_sha=$p_sha2; break; fi
                    done
                fi
            fi
            if [ "$m" -lt 0 ] && [ -n "$t_pre" ]; then
                for i in "${fresh[@]}"; do
                    read -r ep sh fr fo _ <<<"${ents[i]}" || true
                    if [ "$fr" = "$t_pre" ]; then m=$i; break; fi
                done
            fi
            if [ "$m" -ge 0 ]; then
                read -r ep sh fr fo _ <<<"${ents[m]}" || true
                # The handle reaches tmux argv (and the event log) only as
                # [A-Za-z0-9_.-]: tmux-mcp writes it, but a trailing `;` in
                # an argv element would end the command list.
                v_from="${fr//[^A-Za-z0-9_.-]/}"
                v_ok=1; v_from="${v_from:--}"; v_for="${fo:-agent}"; v_sha="${p_sha:-$sh}"
                [ "$v_for" = user ] || v_for=agent
            fi
        fi
        # Keep every other FRESH entry; the match and anything stale go.
        if [ "${#ents[@]}" -gt 0 ]; then
            for i in "${!ents[@]}"; do
                [ "$i" = "$m" ] && continue
                case " ${fresh[*]-} " in *" $i "*) inb_keep="${inb_keep:+$inb_keep|}${ents[i]}" ;; esac
            done
            [ "$inb_keep" = "$O_INB" ] || inb_changed=1
        fi
        agent_msg=0
        [ "$v_ok" = 1 ] && [ "$v_for" = agent ] && agent_msg=1
        # RATE-LIMITED AGENTS (see its section): a prompt into a limited
        # window. Held (nothing written: the tab stays paused, no running
        # flicker) while the limit's pause still stands and the reset is
        # ahead - a wake-up, a task-notification or a peer's ping, each of
        # which hits the limit again and re-marks. Mack's own prompt is never
        # held. Otherwise this turn RESUMES the limited work (lim_resume):
        # Mack's, so loud below whoever sent it, and @agent_paused goes (see
        # below for the one stamp kept). @agent_limit itself stays until
        # the outcome (Stop clears, a rate_limit StopFailure re-marks).
        lim_resume=0
        if [ -n "$O_LIMIT" ]; then
            l_r=""; l_t=""; l_e=""
            read -r l_r l_t l_e _ <<<"$O_LIMIT" || true
            [[ $l_r =~ ^[0-9]{1,12}$ ]] || l_r=0
            l_mack=0
            [ "$agent_msg" = 0 ] && [ "$t_sys" != sys ] && l_mack=1
            # A /loop or scheduled wake-up, or a poll event, is the harness
            # (TURN_JQ's source line), never Mack - held like a
            # task-notification. Not `system` as a whole: native
            # auto-continuation is `system` and must never be held by it.
            case "$t_src" in loop_wakeup|schedule_wakeup|poll_event) l_mack=0 ;; esac
            if [ "$l_mack" = 0 ] && [ -n "$O_PAUSED" ] && [ "$((10#$l_r))" -gt 0 ] \
               && [ "$t_now" -lt "$((10#$l_r))" ]; then
                exit 0
            fi
            lim_resume=1
            # Not held: @agent_paused goes in the same command list as the
            # running write, or the clean Stop that follows would read it and
            # end the turn `paused` instead of loud. The one stamp kept: the
            # pause key's own on a window that was WORKING when it was set
            # (newer than the limit's E, state not paused) - its PAUSE can
            # arrive as this very prompt.
            if [ -n "$O_PAUSED" ]; then
                if [ "$l_mack" = 1 ] || [ "$O_STATE" = paused ] || [ "$O_PAUSED" = "$l_e" ] \
                   || ! [[ $O_PAUSED =~ ^[0-9]{1,12}$ && $l_e =~ ^[0-9]{1,12}$ ]] \
                   || [ "$((10#$O_PAUSED))" -le "$((10#$l_e))" ]; then
                    ss_add set-option -uw -t "$win" @agent_paused
                fi
            fi
        fi
        # New turn or the running one? By id; an agent without turn ids
        # (OpenCode, "-") starts a new turn whenever it is not mid-turn.
        t_new=0
        if [ -z "$O_OKIND" ] || [ "$O_OTID" != "$t_id" ]; then
            t_new=1
        elif [ "$t_id" = - ]; then
            case "$O_STATE" in running|needs-input) ;; *) t_new=1 ;; esac
        fi
        if [ "$t_new" = 1 ]; then
            if [ "$lim_resume" = 1 ]; then
                # The turn after a limit picks up MACK's work: loud, prior
                # dropped, no "started by another agent" context - even for
                # agent-pause.py's RESUME, which tmux-mcp stamps for=agent
                # (the manual pause's RESUME precedent, next arm but one).
                ss_add set-option -w -t "$win" @agent_turn_origin "user $t_id -"
                prior_unset "$win"
            elif [ "$t_sys" = sys ] && [ -n "${O_BGOWN:-$O_OKIND}" ]; then
                # A harness wake-up (a background task or subagent reporting
                # back) continues the work of the turn that launched it:
                # INHERIT its origin. That is @agent_bg_owner when a held Stop
                # recorded whose work is out - an agent turn may have run in
                # between, and Mack's own background result must still be
                # loud - else the current origin. A quiet turn's background
                # work stays quiet; Mack's stays loud.
                t_inh="${O_BGOWN:-$O_OKIND}"
                if [ "$t_inh" = user ]; then
                    ss_add set-option -w -t "$win" @agent_turn_origin "user $t_id -"
                    prior_unset "$win"
                else
                    t_ifrom=-
                    [ "$O_OKIND" = agent ] && t_ifrom="${O_OFROM:--}"
                    ss_add set-option -w -t "$win" @agent_turn_origin "agent $t_id $t_ifrom"
                    # An agent turn whose quiet finish already restored
                    # (prior consumed) is waking up for its background work:
                    # the window's state now is again the one to come back to.
                    if [ -z "$O_PSTATE" ]; then
                        case "$O_STATE" in running|"") ;; *) prior_save "$win" ;; esac
                    fi
                fi
            elif [ "$agent_msg" = 1 ] && [[ $O_RESULT == *" paused" ]] && [ "$O_OKIND" = user ]; then
                # The RESUME (or a ping) after a pause that stopped MACK's
                # own turn: tmux-mcp stamps agent-pause.py's messages
                # for=agent, but the work this turn picks up is his, and he
                # must hear when it finishes. Loud, prior dropped. (A pause
                # that stopped an agent-started turn keeps that turn's quiet
                # finish and its prior: the next arm.)
                ss_add set-option -w -t "$win" @agent_turn_origin "user $t_id -"
                prior_unset "$win"
            elif [ "$agent_msg" = 1 ]; then
                ss_add set-option -w -t "$win" @agent_turn_origin "agent $t_id $v_from"
                prior_save "$win"   # the attention state to come back to
                ss_add set-option -uw -t "$win" @agent_for_user
                ctx_from="$v_from"
            else
                ss_add set-option -w -t "$win" @agent_turn_origin "user $t_id -"
                prior_unset "$win"
            fi
        else
            # Same turn. Mack typing into an agent's turn makes it his
            # (upgrade only, never back); a harness wake-up changes nothing.
            # So does a turn resuming limited work (RATE-LIMITED AGENTS).
            if [ "$O_OKIND" = agent ] && { [ "$lim_resume" = 1 ] || \
               { [ "$agent_msg" = 0 ] && [ "$t_sys" != sys ]; }; }; then
                ss_add set-option -w -t "$win" @agent_turn_origin "user $O_OTID -"
                prior_unset "$win"
            fi
            # A verified message mid-turn may yet run as its own turn (Claude's
            # split case): remember it, with its <for>, for that Stop.
            if [ "$v_ok" = 1 ]; then
                # Oldest first, at most 8 with this one.
                ents=()
                IFS='|' read -ra ents <<<"$O_SEEN" || true
                n=${#ents[@]}
                [ "$n" -gt 7 ] && ents=("${ents[@]:$((n - 7))}")
                seen_new=""
                for e in ${ents[@]+"${ents[@]}"}; do seen_new="${seen_new:+$seen_new|}$e"; done
                seen_new="${seen_new:+$seen_new|}$t_now $v_sha $v_from $v_for"
                ss_add set-option -w -t "$win" @agent_inbound_seen "$seen_new"
            fi
        fi
        if [ "$inb_changed" = 1 ]; then
            if [ -n "$inb_keep" ]; then ss_add set-option -w -t "$win" @agent_inbound "$inb_keep"
            else ss_add set-option -uw -t "$win" @agent_inbound; fi
        fi
        # A quiet restore's log mark never outlives it into a new turn (no
        # watcher tick in between, or a deploy window): free, same call.
        ss_add set-option -uw -t "$win" @agent_quiet
        # Mack has spoken: a held Stop's stash (HELD DETAIL) is history.
        ss_add set-option -uw -t "$win" @agent_held_detail
        # ASKS WHILE WORK IS OUT: a REAL prompt (not a harness wake-up, the
        # same "sys" test as the origin above) starts the turn an ask is
        # scoped to (agent-turn-asks --since: a peer's message counts, as
        # its transcript rule does). Mack's own prompt also answers whatever
        # ask he saw: the next one raises even if it reads the same. A
        # verified agent message (agent_msg) is not Mack replying.
        if [ "$t_sys" != sys ]; then
            ss_add set-option -w -t "$win" @agent_turn_start "$t_now"
            [ "$agent_msg" = 1 ] || ss_add set-option -uw -t "$win" @agent_ask_seen
        fi
        take_detail running run
        set_state "$win" running
        if [ -n "$ctx_from" ] && [ "$ss_wrote" = 1 ]; then
            # The model is told why nobody will be watching for its finish,
            # and how to flag Mack when it should. A handle reaches the JSON
            # only as [A-Za-z0-9_.-].
            ctx_h="${ctx_from//[^A-Za-z0-9_.-]/}"
            if [ -n "$ctx_h" ] && [ "$ctx_h" != - ]; then ctx_who="@$ctx_h"; else ctx_who="another agent"; fi
            printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"%s"}}\n' \
                "This turn was started by a message from $ctx_who, not by Mack. Mack will not be notified when you finish — his tab stays as it was. If your result needs Mack's own attention or a decision from him, call the tmux MCP tool \`notify_user\` with a one-line reason before you finish (fallback: run \`~/.config/tmux/scripts/agent-notify-user.sh \\\"<reason>\\\"\`). Don't use it for routine replies to $ctx_who." >&3 2>/dev/null || true
        fi
        clear_pending "$win"
        # After the state write (it is what the user is waiting to see),
        # before the summary (which reads what the detector just decided).
        drift_update || true
        compose_summary "$win" "$(extract_summary "$payload")" "$payload"
        ;;
    needs-approval)
        # MODE NAME IS HISTORICAL — it is the argument ~/.claude/settings.json
        # passes, and it now means "blocked on the user", approval or question
        # alike. Always assert: focusing a window is not answering its prompt.
        # The focus hook (clear-current) discharges attention states → idle
        # once seen, so a prompt is never silently lost by switching away.
        # Approvals and questions are ONE yellow state since 2026-08-21.
        # They arrive through the same events, and of the two events that
        # describe a single gate only one can name which kind it is — so
        # telling them apart painted a question red twice in two days. There
        # is nothing left to get wrong: both mean "waiting on you", and the
        # tab name still says which session.
        #
        # EXCEPT when the prompt lands in front of the user's eyes: the
        # window active in a client and WezTerm frontmost. Then it is
        # discharged exactly as clear-current would a moment later - idle,
        # with @agent_pending stamped so the heartbeat re-arms `running`
        # once the answered turn resumes. Switching away first still gets
        # the yellow, so a prompt is never silently lost.
        #
        # Detail: PermissionRequest names the tool (perm, or ask for
        # AskUserQuestion); the Notification half of the same gate names
        # nothing ("note"), so it must not overwrite the richer detail its
        # twin already wrote — one extra read, only on that event. If it lands
        # first, its "needs your permission to use <Tool>" stands in until the
        # PermissionRequest replaces it.
        #
        # ASKER GATE (see heartbeat): record whose gate this is, in the
        # state's own command list - the PermissionRequest's agent_id (a
        # subagent's prompt, let through the subagent guard above), else
        # "main". ONLY the PermissionRequest writes it. The Notification twin
        # (the "note" detail) never names an agent - Claude builds it without
        # one, whoever asked - so it leaves @agent_ask_from exactly as it is:
        # after its PermissionRequest, that one's asker stands; landing first,
        # the gate has no asker (the old any-tool-call rule) until the
        # PermissionRequest arrives and records it. Hooks here are never
        # dropped for arriving late, so the twin that loses the ordering race
        # still lands its write.
        take_detail needs-approval perm
        ask_from=""
        if [ "$dkind" != note ]; then
            ask_from=main
            if [ -n "$JQ" ] && [ -n "$payload" ]; then
                ask_from=$("$JQ" -r '.agent_id // empty' <<<"$payload" 2>/dev/null || true)
                # Same charset the heartbeat compares in; also keeps a
                # trailing ';' out of tmux argv.
                ask_from="${ask_from//[^A-Za-z0-9_.-]/}"
                ask_from="${ask_from:-main}"
            fi
        fi
        if [ "$dkind" = note ]; then
            # (A raised ask's `ask` - ASKS WHILE WORK IS OUT - is no twin of
            # this gate: the gate replaces it, detail and asker both; the
            # asker stays unknown until its PermissionRequest lands.)
            case "$(tmux show-options -wqv -t "$win" @agent_detail_kind 2>/dev/null || true)" in
                perm) dkind="" ;;
                ask)
                    dkind=""
                    if [ -n "$(tmux show-options -wqv -t "$win" @agent_ask_bg 2>/dev/null || true)" ]; then
                        dkind=perm
                        ss_add set-option -uw -t "$win" @agent_ask_from
                    fi ;;
                *) dkind=perm ;;
            esac
        fi
        if viewing_now; then
            pend_op=set; seen_op=1   # in the state's own command, as clear-current
            # Mack is looking at the window: a done/needs-input saved for an
            # agent turn's quiet restore is seen now too (AGENT-ORIGINATED
            # TURNS; red is not discharged by being watched).
            case "$(tmux show-options -wqv -t "$win" @agent_prior_state 2>/dev/null || true)" in
                done|needs-input) ss_add set-option -w -t "$win" @agent_prior_state idle ;;
            esac
            set_state "$win" idle
            [ "$ss_wrote" = 1 ] || tmux set-option -w -t "$win" @agent_pending "$(now_epoch)" 2>/dev/null || true
        else
            # (A discharged prompt needs no asker: idle + @agent_pending is
            # consumed by whichever heartbeat comes first, as before.)
            [ -z "$ask_from" ] || ss_add set-option -w -t "$win" @agent_ask_from "$ask_from"
            # A gate replaces a raised ask (ASKS WHILE WORK IS OUT) standing
            # on this needs-input: it is no longer that ask.
            ss_add set-option -uw -t "$win" @agent_ask_bg
            set_state "$win" needs-input
        fi
        ;;
    failed)
        # The turn itself died (529, overloaded). RED, and red now means
        # exactly this — nothing is waiting on an answer, something broke.
        # Loud in any turn, agent-originated included: a failure is never
        # quiet (AGENT-ORIGINATED TURNS). The turn result and the dropped
        # prior ride in the state's own command list.
        stop_fields
        # RATE-LIMITED AGENTS: a usage limit is not a failure. The payload
        # names the error but carries no reset time; the transcript's last
        # main-thread rate_limit record does (tail -n 200, grep-narrowed:
        # transcripts run to hundreds of MB). A model limit (Fable's
        # model_requires_usage_credits) or a record with no resetsAt is type
        # `model`, R 0: only Mack can fix it, in his own words.
        if [ -n "$JQ" ] && [ -n "$payload" ]; then
            lm_head=$("$JQ" -r '(.error | if type == "object" then (.type // .error // "") else (. // "") end
                                 | tostring | .[0:40] | gsub("[\\r\\n]"; " ")),
                                (.last_assistant_message // "" | tostring | .[0:600] | gsub("[\\r\\n]"; " "))' \
                      <<<"$payload" 2>/dev/null || true)
            lm_err="${lm_head%%$'\n'*}"
            if [ "$lm_err" = rate_limit ]; then
                lm_lam=""
                case "$lm_head" in *$'\n'*) lm_lam="${lm_head#*$'\n'}" ;; esac
                lm_r=0; lm_raw=""; lm_api=""; lm_text=""
                if [ -n "$s_tp" ] && [ -f "$s_tp" ]; then
                    lm_out=$(tail -n 200 "$s_tp" 2>/dev/null | grep -F '"rate_limit"' \
                        | "$JQ" -Rrn "$DETAIL_JQ$LIMIT_JQ" 2>/dev/null || true)
                    { IFS= read -r lm_r; IFS= read -r lm_raw; IFS= read -r lm_api; IFS= read -r lm_text; } <<<"$lm_out" || true
                fi
                [ -n "$lm_text" ] || lm_text=$(printf '%s' "$lm_lam" | "$JQ" -Rr "$DETAIL_JQ"'clean' 2>/dev/null | head -n 1 || true)
                printf -v lm_now '%(%s)T' -1 2>/dev/null || lm_now=$(now_epoch)
                if [ "$lm_api" = model_requires_usage_credits ] || ! [[ $lm_r =~ ^[1-9][0-9]{0,11}$ ]]; then
                    lm_r=0; LIM_TY=model
                    # "You've reached your Fable limit. Run /usage-credits to
                    # continue or switch models with /model." -> "Fable limit
                    # · /model to switch"; any other wording stands as written.
                    lm_re='reached your ([A-Za-z0-9][A-Za-z0-9 ._-]{0,30}) limit'
                    if [[ $lm_text =~ $lm_re ]]; then
                        LIM_TXT="${BASH_REMATCH[1]} limit · /model to switch"
                    elif [ "$lm_api" = model_requires_usage_credits ]; then
                        LIM_TXT="model limit · /model to switch"
                    else
                        LIM_TXT="${lm_text:-usage limit}"
                    fi
                else
                    limit_type "$lm_raw" "$lm_r" "$lm_now"
                    limit_text "$lm_r" "$LIM_TY" "$lm_now"
                fi
                limit_mark "$win" "$lm_r" "$LIM_TY" "$LIM_TXT" "$s_id"
                exit 0
            fi
        fi
        # Any other failure is an outcome: it ends a limit the window still
        # carries (a resumed turn that died, or a failure while still
        # limited), with the limit's own pause flag. One read, failures only.
        lf=$(tmux display-message -p -t "$win" "x#{@agent_limit}${US}#{@agent_limit_lifted}${US}#{@agent_paused}" 2>/dev/null || true)
        case "$lf" in
            "x"*"$US"*"$US"*)
                lf="${lf#x}"
                lf_lim="${lf%%"$US"*}"; lf="${lf#*"$US"}"
                limit_end "$win" "$lf_lim" "${lf%%"$US"*}" "${lf#*"$US"}" ;;
        esac
        ss_add set-option -w -t "$win" @agent_turn_result "$s_id loud"
        prior_unset "$win"
        ss_add set-option -uw -t "$win" @agent_held_detail   # HELD DETAIL
        take_detail failed fail "turn failed"
        set_state "$win" failed
        ;;
    done)
        # A turn that ended WAITING on work it launched has not finished, and
        # this tab must not say it has. Mack's call, 2026-08-21, after the tab
        # went green while CuaNotch went amber for the same session at the same
        # moment: a pending background agent or a live workflow keeps the tab
        # running. The rule lives in agent-bg-pending, which the notch's hook
        # calls too — a duplicated ALGORITHM is not something check-invariants
        # can compare the way it compares duplicated constants, so there is
        # exactly one copy of it and two callers.
        bg_pending=0
        if [ -n "$JQ" ] && [ -n "$payload" ] && [ -x "$BG_PENDING" ]; then
            bg_tp=$("$JQ" -r '.transcript_path // empty' <<<"$payload" 2>/dev/null || true)
            bg_sid=$("$JQ" -r '.session_id // empty' <<<"$payload" 2>/dev/null || true)
            bg_cwd=$("$JQ" -r '.cwd // empty' <<<"$payload" 2>/dev/null || true)
            if [ -n "$bg_tp" ] && "$BG_PENDING" "$bg_tp" "$bg_sid" "$bg_cwd"; then
                bg_pending=1
            fi
        fi
        # ASKER GATE (see heartbeat), 2026-10-21: the MAIN agent's reply
        # ending says nothing about a SUBAGENT's gate - a background one can
        # still be waiting on Mack after it - so a needs-input whose asker is
        # a subagent id stands through this Stop (CuaNotch 0083845 keeps its
        # row `input` the same way). It is held exactly like background work,
        # because that is what it is: an asking subagent is work still out.
        # So the Stop takes the bg_pending branches below (turn result,
        # @agent_bg_owner, a quiet turn's prior kept) with the state that
        # holds being the gate (sd_hold) instead of `running`, and the gate's
        # perm/ask detail kept instead of the reply's. When the subagent's
        # gate is answered its own heartbeat re-arms `running`, which is the
        # state this Stop would have held anyway, and the existing drain
        # takes it from there: the watcher's release when the work is gone,
        # or the wake-up turn's own Stop. Two reads, on Stop only.
        sd_hold=running
        if [ "$(window_state "$win")" = needs-input ]; then
            case "$(tmux show-options -wqv -t "$win" @agent_ask_from 2>/dev/null || true)" in
                ''|main) ;;
                *) sd_hold=needs-input; bg_pending=1 ;;
            esac
        fi
        # Any stash of an earlier held Stop is this Stop's to replace.
        ss_add set-option -uw -t "$win" @agent_held_detail
        # set_state for a Stop held by background work: `running`, or the
        # standing subagent gate with its own detail - this Stop's detail
        # (its reply, or a notify_user reason) stashed for when the gate
        # ends (HELD DETAIL, see held_detail_take).
        set_held() {
            if [ "$sd_hold" != running ]; then
                [ -z "$dkind" ] || ss_add set-option -w -t "$win" @agent_held_detail "$dkind"$'\t'"$dtext"
                dkind=""; dtext=""
            fi
            set_state "$win" "$sd_hold"
        }
        # The detail is the Stop's either way: even a turn held `running` by
        # background work has a final message worth showing.
        take_detail done done "turn finished"
        # AGENT-ORIGINATED TURNS: loud or quiet? The Stop carries the id of
        # the turn's UserPromptSubmit; an id nobody announced is Claude's
        # split case or a harness wake-up, settled from the transcript row.
        origin_read "$win"
        stop_fields
        # RATE-LIMITED AGENTS: a turn that finished is past its limit. The
        # limit and its lifted stamp go in this Stop's command list; so does
        # @agent_paused while it is still the limit's own (a held wake-up
        # whose turn got through after all - a cswap switch lifted the limit
        # early): that pause was the limit's, so this finish is an ordinary
        # one, not `paused`. A manual pause stamped since stands.
        # A Stop for the very turn the limit was marked from (codex may run
        # its Stop hook for the failed turn after the watcher marked it) is
        # that limit, not a finish: left alone.
        if [ -n "$O_LIMIT" ] && [ "$s_id" != - ] && [[ $O_RESULT == "$s_id limited "* ]]; then
            exit 0
        fi
        # CODEX has no failure hook, but runs its Stop for a turn that ended
        # on a usage limit: the rollout's last task_complete, for THIS turn,
        # says so (usage_limit_exceeded). Marked here, so the verdict lands
        # with the Stop (CuaNotch reads @agent_turn_result within 2 s); the
        # watcher's rollout reconcile stays the backstop for a task_complete
        # written after this hook ran. One show-options and a bounded tail
        # (codex_limit_parse), on codex Stops only.
        if [ "$agent" = codex ] && [ "$s_id" != - ]; then
            cx_roll=$(tmux show-options -wqv -t "$win" @agent_rollout 2>/dev/null || true)
            if [ -n "$cx_roll" ]; then
                printf -v lm_now '%(%s)T' -1 2>/dev/null || lm_now=$(now_epoch)
                codex_limit_parse "$cx_roll" "$lm_now"
                if [ "$CL_HIT" = 1 ] && [ "$CL_TID" = "$s_id" ]; then
                    limit_text "$CL_R" codex "$lm_now"
                    limit_mark "$win" "$CL_R" codex "$LIM_TXT" "$s_id"
                    exit 0
                fi
            fi
        fi
        limit_end "$win" "$O_LIMIT" "$O_LIFTED" "$O_PAUSED"
        [ "$LIM_UNPAUSED" = 0 ] || O_PAUSED=""
        s_cls=user; s_unseen=0
        if [ -n "$O_OKIND" ] && [ "$s_id" = "$O_OTID" ]; then
            s_cls="$O_OKIND"
        else
            s_unseen=1
            if [ "$agent" = claude ] && [ "$s_id" != - ] && [ -n "$s_tp" ] && [ -f "$s_tp" ] && [ -n "$JQ" ]; then
                s_cls=$(split_classify "$s_id" "$s_tp")
                # A wake-up continues the current turn's origin (unknown:
                # loud). Record it, so the next event of this turn agrees.
                if [ "$s_cls" = inherit ]; then
                    # From whoever's background work is out, as in running.
                    s_cls="${O_BGOWN:-${O_OKIND:-user}}"
                    if [ -n "$O_BGOWN$O_OKIND" ]; then
                        s_ifrom=-
                        [ "$s_cls" = agent ] && [ "$O_OKIND" = agent ] && s_ifrom="${O_OFROM:--}"
                        ss_add set-option -w -t "$win" @agent_turn_origin "$s_cls $s_id $s_ifrom"
                    fi
                fi
            fi
        fi
        s_quiet=0
        if [ "$s_cls" = agent ]; then
            if [ -n "$O_FORUSER" ]; then
                # The agent flagged Mack (notify_user): a normal loud finish,
                # with its reason as the detail.
                r=$(reason_detail "$O_FORUSER")
                [ -z "$r" ] || { dkind=done; dtext="$r"; }
            else
                s_quiet=1
            fi
        fi
        # ASKS WHILE WORK IS OUT (see its section): a Stop held `running` by
        # background work asks the decider whether the turn asked Mack. Its
        # marker is the agent saying so outright, so it makes even an
        # agent-started turn loud (as notify_user does). EXPLICIT ONLY: no
        # marker, no ask - nothing guesses from the reply any more. A
        # subagent's standing gate (sd_hold) or a pause already decides what
        # the window shows: not asked there.
        ask_hit=0; ask_line=""; ask_clean=""
        if [ "$bg_pending" -eq 1 ] && [ "$sd_hold" = running ] && [ -z "$O_PAUSED" ] && [ -n "$JQ" ]; then
            ask_since="$O_TSTART"
            ask_run "$ASK_SYNC_BUDGET" --marker-only ${ask_since:+--since "$ask_since"} <<<"$payload"
            if [ "$ask_rc" = 0 ]; then
                # Sanitized once; every use below takes this.
                ask_clean=$(ask_detail "$ask_line")
                # The question Mack already saw (@agent_ask_seen; a marker
                # stands through the agent's woken turns): no re-ping, and
                # nothing ask-related written - this Stop is as if unasked.
                if ! ask_seen_same "$ask_clean"; then
                    ask_hit=1; s_quiet=0
                fi
            fi
        fi
        # @agent_bg_owner: a Stop HELD for background work records whose work
        # it is (quiet -> agent, loud -> user; never user -> agent, so Mack's
        # work out under an agent turn keeps its claim); a Stop that is not
        # held clears it - nothing of this turn is out any more.
        if [ "$bg_pending" -eq 1 ]; then
            s_own=user
            [ "$s_quiet" = 1 ] && [ "$O_BGOWN" != user ] && s_own=agent
            ss_add set-option -w -t "$win" @agent_bg_owner "$s_own"
        else
            ss_add set-option -uw -t "$win" @agent_bg_owner
        fi
        # PAUSED (docs/agent-pause.md, "The paused state"): agent-pause.py
        # flagged this window before its PAUSE went out, so this turn end -
        # loud, quiet or flagged for Mack alike - is `paused`: not done, no
        # green, no restore. The PAUSE itself usually arrives as an agent
        # message (often queued mid-turn), and paused beats the quiet finish.
        # Held by background work: still running, and the watcher writes
        # paused when it drains. The prior an agent turn saved is KEPT for
        # after the resume (see prior_save); the log mark never applies.
        if [ -n "$O_PAUSED" ]; then
            ss_add set-option -w -t "$win" @agent_turn_result "$s_id paused"
            ss_add set-option -uw -t "$win" @agent_quiet
            if [ "$bg_pending" -eq 1 ]; then
                set_held
            else
                pend_op=unset
                # The flag was read above; agent-pause.py may have cleared it
                # for a resume since (it unsets the flag and turns `paused`
                # into idle in one call). Re-checked INSIDE this command
                # list, which tmux runs as a unit: flag gone -> idle, exactly
                # what the resume would have made of it. Only a window id
                # and fixed words go through if-shell's re-parse.
                ss_add if-shell -F -t "$win" '#{?#{@agent_paused},0,1}' \
                    "set-option -w -t $win @agent_state idle"
                set_state "$win" paused
            fi
            compose_summary "$win" "$(extract_summary "$payload")" "$payload"
            exit 0
        fi
        if [ "$s_quiet" = 1 ]; then
            if [ "$bg_pending" -eq 1 ]; then
                # Held running for its background work, prior kept: the
                # watcher restores it when the work drains (or a wake-up
                # turn's own quiet Stop does).
                ss_add set-option -w -t "$win" @agent_turn_result "$s_id quiet $sd_hold"
                set_held
            elif [ "$s_unseen" = 1 ] && [ -z "$O_PSTATE" ]; then
                # A split-case turn that never went `running`: there is
                # nothing to restore - leave the window exactly as it is.
                dkind=""; dtext=""
                cur_st=$(window_state "$win")
                ss_add set-option -w -t "$win" @agent_turn_result "$s_id quiet ${cur_st:-idle}"
                set_state "$win" "${cur_st:-idle}"
            else
                prior_restore "$win"
                ss_add set-option -w -t "$win" @agent_turn_result "$s_id quiet $R_STATE"
                # The watcher's event log describes this transition as the
                # reply it was ("↩ replied to @from"), not as a fresh finish
                # carrying the restored detail; it consumes the mark.
                ss_add set-option -w -t "$win" @agent_quiet "${O_OFROM:--}"
                set_state "$win" "$R_STATE"
            fi
            compose_summary "$win" "$(extract_summary "$payload")" "$payload"
            exit 0
        fi
        ss_add set-option -w -t "$win" @agent_turn_result "$s_id loud"
        ss_add set-option -uw -t "$win" @agent_quiet   # never logged as a reply
        prior_unset "$win"
        if [ "$bg_pending" -eq 1 ]; then
            if [ "$ask_hit" = 1 ] && viewing_now; then
                # In front of his eyes: seen as it lands (still running).
                seen_op=1
                ss_add set-option -w -t "$win" @agent_ask_seen "$ask_clean"
                set_held
            elif [ "$ask_hit" = 1 ]; then
                ask_raise "$win" "$ask_clean"
                set_state "$win" needs-input
            else
                set_held
            fi
            compose_summary "$win" "$(extract_summary "$payload")" "$payload"
            exit 0
        fi
        # Don't tint a window the user is looking at — they saw it finish.
        # Watched AND WezTerm frontmost (see viewing_now): a watched window
        # in a backgrounded terminal was the one way to miss a finish.
        if viewing_now; then
            seen_op=1
            set_state "$win" idle
        else
            set_state "$win" "done"
        fi
        clear_pending "$win"
        compose_summary "$win" "$(extract_summary "$payload")" "$payload"
        ;;
    clear)
        # SessionEnd reasons clear/resume are immediately followed by a new
        # SessionStart in the same pane — clearing would just flicker.
        if [ -n "$JQ" ] && [ -n "$payload" ]; then
            reason=$("$JQ" -r '.reason // empty' <<<"$payload" 2>/dev/null || true)
            case "$reason" in clear|resume) exit 0 ;; esac
        fi
        clear_state "$win"
        ;;
    *)
        echo "Usage: agent-tab-indicator.sh <idle|running|heartbeat|needs-approval|subagent-stop|failed|done|interrupt|reconcile|clear|clear-current> [claude|codex|opencode]" >&2
        # exit 0, not 1: codex treats a hook's exit status as a GATE (see the
        # header), so a typo'd mode in hooks.json would block every codex
        # event rather than just failing to paint a tab. A silently inert
        # indicator is a far better failure than a wedged agent; the usage
        # line still goes to stderr for anyone running this by hand.
        exit 0
        ;;
esac
