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
#   @agent_pending  epoch stamp: a needs-input window the user focused (went
#                   to answer its prompt). The next heartbeat consumes it to
#                   restore `running` when the answered turn resumes; every
#                   other lifecycle mode clears it so it can't go stale.
#   @agent_kind     claude | codex | opencode — which agent's hooks drive this
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
#   heartbeat    PostToolUse         → re-arm running mid-turn: from
#                                      running/needs-input, or from idle when
#                                      @agent_pending marks an answered
#                                      permission prompt — never from bare
#                                      idle/done, so a late tool call can't
#                                      resurrect a finished tab; skips stdin
#                                      entirely — payloads can be huge
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
#   failed       StopFailure         → the TURN ITSELF died (529, overloaded).
#                                      Red. Nothing is waiting on an answer;
#                                      something broke. Asserted like the
#                                      other attention state (focus ≠ fixed)
#                                      and discharged by the focus hook.
#   done         Stop                → turn finished; refresh @agent_summary;
#                                      not tinted if a client is watching it;
#                                      a turn ANOTHER AGENT started finishes
#                                      quiet (AGENT-ORIGINATED TURNS below)
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
fi
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

owns_window() {
    [ -z "${AGENT_TAB_OWNER_SESSION:-}" ] && return 0
    [ "$(tmux show-options -wqv -t "$1" @agent_session_id 2>/dev/null)" = "$AGENT_TAB_OWNER_SESSION" ] &&
        [ "$(tmux show-options -wqv -t "$1" @agent_owner_token 2>/dev/null)" = "$AGENT_TAB_OWNER_TOKEN" ] &&
        owner_current
}

owner_current() {
    [ -z "${AGENT_TAB_OWNER_SESSION:-}" ] && return 0
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
    [ "$mode" = condense-locked ] && [ -n "${AGENT_TAB_OWNER_RECORD:-}" ] || return 1
    "$HOME/.local/bin/codex-terminal-owner" valid "$AGENT_TAB_OWNER_SESSION" "$AGENT_TAB_OWNER_TOKEN" \
        <<<"$AGENT_TAB_OWNER_RECORD" >/dev/null 2>&1
}
# Shared with CuaNotch's cua-notch-agent-hook: the one implementation of "is
# background work from this session still out?". See its header.
BG_PENDING="$HOME/.local/bin/agent-bg-pending"

# Watchdog for the companion daemon. agent-tab-watcher.sh is spawned exactly
# once, from tmux.conf, so if it ever dies mid-session (stray pkill, OOM, a
# tmux hiccup) everything it drives silently stops — no blink (the glyph
# freezes on the @agent_blink=unset color), no workflow gear, no presence
# seeding, no GC — until the next `prefix r`, and nothing surfaces the failure.
# Hooks fire constantly, so re-assert it here: in the healthy case this is a
# file test plus a kill -0, no forks. Respawn goes through the tmux server so
# the daemon outlives this short-lived hook process. PIDFILE path must match
# the watcher's.
ensure_watcher() {
    local pidfile pid="" now beat
    pidfile="${TMPDIR:-/tmp}/agent-tab-watcher.${UID:-$(id -u)}.pid"
    if [ -f "$pidfile" ]; then
        read -r pid < "$pidfile" 2>/dev/null || pid=""
    fi
    # `kill -0` alone answers "some process exists", not "the watcher is
    # alive": the daemon only unlinks its pidfile on a clean EXIT, and pids
    # here wrap in ~15 min, so a stale file usually points at an unrelated
    # live process. That reads as HEALTHY and the watchdog never fires —
    # the one failure it exists to catch, silently. Confirm identity before
    # believing it. This costs one ps against the "no forks in the healthy
    # case" goal, but hooks fire a few times per turn, not 86400 times a day
    # like the watcher's own tick, so it is the cheap place to pay.
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        case "$(ps -o command= -p "$pid" 2>/dev/null)" in
            *"/agent-tab-watcher.sh")
                # ...and that it is still TURNING, not merely resident. The
                # daemon restamps this file every tick, so a stale mtime means
                # a wedged loop: alive to every liveness test we have, but no
                # longer reconciling, and invisible because a frozen blink now
                # renders as plain blue (= idle). 30s is 30 ticks of grace.
                # Kill it so the respawn below isn't refused by the singleton.
                now=$(date +%s 2>/dev/null || echo 0)
                beat=$(stat -f %m "$pidfile" 2>/dev/null || echo 0)
                if [ $((now - beat)) -lt 30 ]; then
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
    owns_window "$win" || return 0
    cur=$(window_state "$win")
    if [ "$cur" != "$new" ]; then
        printf -v ts '%(%s)T' -1 2>/dev/null || ts=$(date +%s)
        cmd=(set-option -w -t "$win" @agent_state "$new" \;
             set-option -w -t "$win" @agent_since "${sv:-$ts $new}")
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
    tmux set-option -uw -t "$win" @agent_summary_cond 2>/dev/null || true
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
         set-option -uw -t "$win" @agent_born 2>/dev/null || true
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
ORIGIN_OPTS=(@agent_turn_origin @agent_turn_result @agent_prior_state @agent_prior_since
             @agent_prior_detail_kind @agent_prior_detail @agent_inbound @agent_inbound_seen
             @agent_for_user @agent_awaiting @agent_quiet @agent_bg_owner @agent_paused)
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
    out=$(tmux display-message -p -t "$1" "x${US}#{@agent_turn_origin}${US}#{@agent_state}${US}#{@agent_since}${US}#{@agent_detail_kind}${US}#{@agent_prior_state}${US}#{@agent_prior_since}${US}#{@agent_prior_detail_kind}${US}#{@agent_turn_result}${US}#{@agent_bg_owner}${US}#{@agent_paused}${US}#{@agent_inbound}${US}#{@agent_inbound_seen}${US}#{@agent_detail}${US}#{@agent_prior_detail}${US}#{@agent_for_user}" 2>/dev/null || true)
    case "$out" in "x$US"*) ;; *) return 0 ;; esac
    IFS="$US" read -r x O_ORIGIN O_STATE O_SINCE O_DK O_PSTATE O_PSINCE O_PDK O_RESULT O_BGOWN O_PAUSED O_INB O_SEEN O_DT O_PDT O_FORUSER <<<"$out" || true
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
TURN_JQ='
(.prompt_id // .turn_id // "" | tostring | .[0:128]
 | if test("^[A-Za-z0-9_.:-]+$") then . else "-" end),
(.prompt // "" | tostring | .[0:600]
 | ((capture("^[ \\t\\r\\n]*\\[message from @(?<h>[^\\]\\s|]{1,64})\\]") // {h: ""}).h),
   (if test("^[ \\t\\r\\n]*(<task-notification>|Another Claude session sent a message:\\s*<agent-message )")
    then "sys" else "" end))'

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
set_summary() {
    local win="$1" summary="$2" cond="${3:-0}" cur
    owns_window "$win" || return 0
    [ -n "$summary" ] || return 0
    if [ "$cond" = 1 ]; then
        tmux set-option -w -t "$win" @agent_summary_cond 1 2>/dev/null || true
    else
        tmux set-option -uw -t "$win" @agent_summary_cond 2>/dev/null || true
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

# Condenser model. copilot's --model whitelist tracks the CLI version and the
# account's entitlements, and an id that vanishes fails EVERY condense: CLI
# 1.0.75 rejected claude-haiku-4.5 (and every other explicit id) with
# `Model "…" is not available`, so tabs silently kept their raw interim titles
# and the cache filled with negative entries — a broken renamer that looks
# exactly like a slow one. The pin is therefore best-effort: on rejection the
# condenser falls back to copilot's default model and drops MODEL_SKIP so
# later runs skip the doomed call, re-probing the pin once the marker ages out
# (models come back). Override the pin with AGENT_TAB_CONDENSE_MODEL; set it
# empty to always use copilot's default.
CONDENSE_MODEL="${AGENT_TAB_CONDENSE_MODEL-claude-haiku-4.5}"
MODEL_SKIP="${TMPDIR:-/tmp}/agent-tab-model-unavailable.${UID:-$(id -u)}"
MODEL_SKIP_TTL=86400

now_epoch() { date +%s 2>/dev/null || echo 0; }

title_key() {
    printf '%s' "$1" | /usr/bin/shasum -a 256 | cut -c1-16
}

# Last non-empty short for a title (skips negative rows; last writer wins).
cached_short() {
    [ -f "$CACHE" ] || return 0
    awk -F'\t' -v k="$(title_key "$1")" \
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

# Gate model output before caching: accept only title-like text (1-4 words,
# <=24 chars, has a letter, no colon / sentence-final punctuation, not an
# apology / refusal / auth-error / first-person reply). This is what stops
# copilot error strings ("Credit balance is too…") from poisoning the cache.
valid_short() {
    local s="$1" lc wc
    [ -n "$s" ] || return 1
    [ "${#s}" -le 24 ] || return 1
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
    proj=$(project_name "$payload")
    # A custom title (/rename) is the user's own naming: shown as written,
    # never condensed. Claude's generated names are kebab slugs, so a
    # space-free one with -/_ reads as words. Marked condensed (1) so no raw
    # stand-in ever repaints over it.
    if [ "$src" = custom ]; then
        short="$raw"
        case "$short" in
            *" "*) ;;
            *[-_]*)
                short="${short//[-_]/ }"
                while [[ $short == *"  "* ]]; do short="${short//  / }"; done
                short="${short# }"; short="${short% }"
                ;;
        esac
        [ -n "$short" ] || short="$raw"
        set_summary "$win" "${proj:+$proj/}$(fit_words "$short" 24)" 1
        return 0
    fi
    short=$(cached_short "$kin")
    if [ -n "$short" ]; then
        set_summary "$win" "${proj:+$proj/}$short" 1
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
    command -v copilot >/dev/null 2>&1 || return 0
    # Hooks may have their whole process group reaped after returning. Ask
    # the tmux server to own this background job instead, carrying the exact
    # socket/session identity rather than the server's launch environment.
    local launch
    printf -v launch '%q ' env "HOME=$HOME" "PATH=$PATH" "TMPDIR=${TMPDIR:-/tmp}" \
        "TMUX=${TMUX:-}" "TMUX_PANE=${TMUX_PANE:-}" \
        "AGENT_TAB_SOCKET=${AGENT_TAB_SOCKET:-}" \
        "AGENT_TAB_OWNER_SESSION=${AGENT_TAB_OWNER_SESSION:-}" \
        "AGENT_TAB_OWNER_TOKEN=${AGENT_TAB_OWNER_TOKEN:-}" \
        "AGENT_TAB_OWNER_BINDING=${AGENT_TAB_OWNER_BINDING:-}" \
        "AGENT_TAB_OWNER_RECORD=${AGENT_TAB_OWNER_RECORD:-}" \
        "AGENT_TAB_CONDENSE_MODEL=$CONDENSE_MODEL" \
        bash "$0" condense "$win" "$proj" "$raw" "$prev"
    launch+=' >/dev/null 2>&1'
    tmux run-shell -b "$launch" >/dev/null 2>&1 || true
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
# compose_summary shows as-is, and a drift retitle, emitted as
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
# Cached under the composed input, so it is one call per trigger. Never
# invoked by hooks directly, so a slow/failed model call only delays the
# title swap.
if [ "$mode" = "condense" ] || [ "$mode" = "condense-locked" ]; then
    win="${2:-}"; proj="${3:-}"; raw="${4:-}"; prev="${5:-}"
    { [ -n "$win" ] && [ -n "$raw" ]; } || exit 0
    kin="$raw"
    [ -z "$prev" ] || kin="retitle"$'\n'"$prev"$'\n'"$raw"   # = compose_summary's
    key=$(title_key "$kin")
    if [ "$mode" = "condense" ]; then
        # A mkdir lock survives SIGKILL and then rejects this title forever.
        # Keep the advisory-lock inode permanently; the kernel releases its
        # lock when the worker exits, even if no shell trap can run. The new
        # suffix also bypasses orphaned legacy .lock directories safely.
        exec python3 - "${TMPDIR:-/tmp}/agent-tab-condense.$key.flock" "$0" "$win" "$proj" "$raw" "$prev" <<'PYLOCK'
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
    ERRF=$(mktemp "${TMPDIR:-/tmp}/agent-tab-condense.$key.err.XXXXXX")
    trap 'rm -f "$ERRF" 2>/dev/null' EXIT
    trap 'exit 0' INT TERM

    short=$(cached_short "$kin")
    if [ -z "$short" ]; then
        # Back off if a recent attempt for this title failed, so a persistent
        # copilot error (not logged in, out of credit) isn't re-run every turn.
        negative_fresh "$key" && exit 0

        # gtimeout is coreutils' name when /usr/bin/timeout is absent (macOS).
        TO=$(command -v timeout || command -v gtimeout || true)
        if [ -n "$prev" ]; then
            PROMPT="Current tab label: $prev
The user's latest messages: $raw

If these messages are still about the same work as the label (follow-ups, details, sub-steps, feedback, logistics), output the label EXACTLY as given. Only if the user has clearly moved on to a different task or subject, output a new 2-4 word label for the new task, built from the MOST SPECIFIC, distinctive words: the concrete subject, target, feature, file, tech, or action unique to it. Keep proper nouns and real names. DROP generic filler (project, code, app, task, session, various, thing, stuff, update, changes, improve, and a bare fix/add/refactor when what it acts on is unnamed). Output ONLY the label - no punctuation, quotes, or explanation."
        else
        PROMPT="From the coding-session title or first message below, output a 2-4 word tab label built from the MOST SPECIFIC, distinctive words in it: the concrete subject, target, feature, file, tech, or action unique to THIS task. Keep proper nouns and real names. DROP generic filler (project, code, app, task, session, various, deep, inspection, thing, stuff, update, changes, improve, and a bare fix/add/refactor when what it acts on is unnamed). Someone reading the label should be able to guess the task back. Prefer the unusual identifying word over the category word. Output ONLY the label - no punctuation, quotes, or explanation.

Examples:
- 'deep inspection of this project docs, I wanna open source it, make sure nothing reads like AI or oddly specific to me' => Open-source doc cleanup
- 'refactor the auth middleware to use JWTs instead of session cookies' => JWT auth refactor
- 'figure out why the websocket reconnect drops messages under load' => Websocket reconnect bug

Title: $raw"
        fi
        # Copilot prints the answer on stdout, stats on stderr; a pure text
        # prompt grants no tool permissions. Capture the exit code explicitly
        # — `|| true` would mask a failed call whose stdout is an error string.
        # Empty $1 = no --model flag, i.e. whatever copilot picks by default.
        copilot_condense() {
            ${TO:+"$TO" 90} copilot -p "$PROMPT" ${1:+--model "$1"} \
                --no-color </dev/null 2>"$ERRF"
        }

        model="$CONDENSE_MODEL"
        # A standing rejection marker means the pin was refused recently —
        # go straight to the default instead of burning a doomed call.
        if [ -n "$model" ] && [ -f "$MODEL_SKIP" ]; then
            mts=$(stat -f %m "$MODEL_SKIP" 2>/dev/null || echo 0)
            [ "$(( $(now_epoch) - mts ))" -lt "$MODEL_SKIP_TTL" ] && model=""
        fi

        ok=0
        if out=$(copilot_condense "$model"); then
            ok=1
        elif [ -n "$model" ] && grep -qiE 'not available|unknown model|invalid model' "$ERRF" 2>/dev/null; then
            # The pin is gone from this CLI/account. Remember it (so the next
            # condense skips straight to the default) and retry right now on
            # the default model — a renamed model must not cost a whole
            # session of raw tab titles.
            : > "$MODEL_SKIP" 2>/dev/null || true
            out=$(copilot_condense "") && ok=1
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
                short=$(fit_words "$short" 24)
                valid_short "$short" || short=""
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
    set_summary "$win" "${proj:+$proj/}$short" 1
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
    owner_current || exit 0
    previous_sid=$(tmux show-options -wqv -t "$win" @agent_session_id 2>/dev/null || true)
    if [ "$previous_sid" != "$AGENT_TAB_OWNER_SESSION" ]; then
        clear_state "$win"
    fi
    tmux set-option -w -t "$win" @agent_session_id "$AGENT_TAB_OWNER_SESSION" 2>/dev/null || exit 0
    tmux set-option -w -t "$win" @agent_owner_token "$AGENT_TAB_OWNER_TOKEN" 2>/dev/null || exit 0
fi

# Claude heartbeat is the hot path (every tool call): don't wait on stdin — the
# payload includes tool_response, which can be megabytes. A backgrounded
# drain consumes the pipe so the writer never sees EPIPE (codex's tolerance
# for a hook that abandons its stdin is undocumented), without blocking us.
# Only re-arm running from running/needs-input — a late PostToolUse landing
# after Stop's `done` must NOT resurrect a finished tab. Codex reads its
# payload earlier to validate session ownership and reject subagents.
if [ "$mode" = "heartbeat" ]; then
    [ "$agent" = codex ] || ( cat >/dev/null 2>&1 & ) 2>/dev/null
    case "$(window_state "$win")" in
        running|needs-*|failed) set_state "$win" running ;;
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

# ...and is the user actually LOOKING? A watched window in a WezTerm that is
# behind Slack is not being seen, and until 2026-08-25 `done` skipped its
# tint on the watched test alone - the one case where a finish could be
# missed with no colour ever shown. Now both: the window is active for a
# client AND WezTerm is frontmost. Two lsappinfo forks, ~11ms, and only on
# the two events that would tint. Mack's ask: a yellow or green that lands
# while he is sitting on that tab with WezTerm focused clears at once.
viewing_now() {
    [ "$watched" -gt 0 ] || return 1
    [ "$(lsappinfo info -only bundleid "$(lsappinfo front 2>/dev/null)" 2>/dev/null)" = \
      '"CFBundleIdentifier"="com.github.wez.wezterm"' ]
}

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
        case "$src" in
            clear|startup) ss_add set-option -uw -t "$win" @agent_paused ;;
        esac
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
                    set_summary "$win" "${proj:+$proj/}New Session"
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
        { IFS= read -r t_id; IFS= read -r t_pre; IFS= read -r t_sys; } <<<"$tj" || true
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
        # New turn or the running one? By id; an agent without turn ids
        # (OpenCode, "-") starts a new turn whenever it is not mid-turn.
        t_new=0
        if [ -z "$O_OKIND" ] || [ "$O_OTID" != "$t_id" ]; then
            t_new=1
        elif [ "$t_id" = - ]; then
            case "$O_STATE" in running|needs-input) ;; *) t_new=1 ;; esac
        fi
        if [ "$t_new" = 1 ]; then
            if [ "$t_sys" = sys ] && [ -n "${O_BGOWN:-$O_OKIND}" ]; then
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
            if [ "$agent_msg" = 0 ] && [ "$t_sys" != sys ] && [ "$O_OKIND" = agent ]; then
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
        take_detail needs-approval perm
        if [ "$dkind" = note ]; then
            case "$(tmux show-options -wqv -t "$win" @agent_detail_kind 2>/dev/null || true)" in
                perm|ask) dkind="" ;;
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
        ss_add set-option -w -t "$win" @agent_turn_result "$s_id loud"
        prior_unset "$win"
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
        # The detail is the Stop's either way: even a turn held `running` by
        # background work has a final message worth showing.
        take_detail done done "turn finished"
        # AGENT-ORIGINATED TURNS: loud or quiet? The Stop carries the id of
        # the turn's UserPromptSubmit; an id nobody announced is Claude's
        # split case or a harness wake-up, settled from the transcript row.
        origin_read "$win"
        stop_fields
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
                set_state "$win" running
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
                ss_add set-option -w -t "$win" @agent_turn_result "$s_id quiet running"
                set_state "$win" running
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
            set_state "$win" running
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
        echo "Usage: agent-tab-indicator.sh <idle|running|heartbeat|needs-approval|failed|done|interrupt|reconcile|clear|clear-current> [claude|codex|opencode]" >&2
        # exit 0, not 1: codex treats a hook's exit status as a GATE (see the
        # header), so a typo'd mode in hooks.json would block every codex
        # event rather than just failing to paint a tab. A silently inert
        # indicator is a far better failure than a wedged agent; the usage
        # line still goes to stderr for anyone running this by hand.
        exit 0
        ;;
esac
