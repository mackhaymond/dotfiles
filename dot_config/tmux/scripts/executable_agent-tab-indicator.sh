#!/usr/bin/env bash
# Agent tab indicator v2 — drive per-window tmux options from AI-agent
# lifecycle hooks so the Catppuccin tab bar reflects agent state.
#
# Options written (window scope; unset = no agent in window):
#   @agent_state    idle | running | needs-input | failed | done
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
#   @agent_kind     claude | codex — which agent's hooks drive this window;
#                   written with any state/detail write a hook makes
#   @agent_detail_kind  perm | ask | fail | done | run — what the last event
#                   was about (see DETAIL_JQ); @agent_detail = one sanitized
#                   line for it (≤80 chars). Both written in set_state's
#                   command list, unset by SessionStart and clear_state, never
#                   touched by the heartbeat. Not rendered in the tab bar —
#                   they feed the sidebar (`list-windows -F '#{@agent_detail}'`).
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
#   hooks may run in a shared daemon.
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
#   running      UserPromptSubmit    → turn started; refresh @agent_summary
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
#                                      not tinted if a client is watching it
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
# The sidebar detail the next set_state writes (see set_state/take_detail).
dkind=""
dtext=""

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
    local win="$1" new="$2" dk="$dkind" dt="$dtext" cur ts
    local -a cmd=()
    dkind=""; dtext=""
    owns_window "$win" || return 0
    cur=$(window_state "$win")
    if [ "$cur" != "$new" ]; then
        printf -v ts '%(%s)T' -1 2>/dev/null || ts=$(date +%s)
        cmd=(set-option -w -t "$win" @agent_state "$new" \;
             set-option -w -t "$win" @agent_since "$ts $new")
    fi
    if [ "$dk" = - ]; then
        cmd+=(${cmd[0]+\;} set-option -uw -t "$win" @agent_detail_kind \;
              set-option -uw -t "$win" @agent_detail)
    elif [ -n "$dk" ]; then
        cmd+=(${cmd[0]+\;} set-option -w -t "$win" @agent_detail_kind "$dk" \;
              set-option -w -t "$win" @agent_detail "$dt")
    fi
    [ "${#cmd[@]}" -gt 0 ] || return 0
    case "$agent" in
        claude|codex) cmd+=(\; set-option -w -t "$win" @agent_kind "$agent") ;;
    esac
    tmux "${cmd[@]}" 2>/dev/null || true
    [ "$cur" = "$new" ] || tmux refresh-client -S 2>/dev/null || true
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
    # The sidebar's per-window fields (see set_state), one call for all three.
    tmux set-option -uw -t "$win" @agent_kind \; \
         set-option -uw -t "$win" @agent_detail_kind \; \
         set-option -uw -t "$win" @agent_detail 2>/dev/null || true
    if [ -n "$cur" ]; then
        tmux refresh-client -S 2>/dev/null || true
    fi
}

clear_pending() {
    tmux set-option -uw -t "$1" @agent_pending 2>/dev/null || true
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
    # $2 is extract_summary's tagged "<src>\t<title>" (bare text = final).
    local win="$1" tagged="$2" payload="$3" proj short src raw
    [ -n "$tagged" ] || return 0
    case "$tagged" in
        *$'\t'*) src="${tagged%%$'\t'*}"; raw="${tagged#*$'\t'}" ;;
        *)       src="final"; raw="$tagged" ;;
    esac
    [ -n "$raw" ] || return 0
    proj=$(project_name "$payload")
    short=$(cached_short "$raw")
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
    if [ "$(tmux show-options -wqv -t "$win" @agent_summary_cond 2>/dev/null || true)" != 1 ]; then
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
        bash "$0" condense "$win" "$proj" "$raw"
    launch+=' >/dev/null 2>&1'
    tmux run-shell -b "$launch" >/dev/null 2>&1 || true
}

# Conversation title, best source first. Emits "<src>\t<title>", where <src>
# distinguishes the agent's own conversation title (`final`) from the turn's
# prompt standing in until that title exists (`interim`) — compose_summary
# spends a model call only on `final`. Empty output = no title at all.
# A tab is a safe delimiter: sanitize_summary maps tabs to spaces.
#   claude: latest ai-title entry near the transcript tail (cheap: last 64KB),
#           else session_title, else the prompt that started the turn.
#   codex:  threads.name from ~/.codex/state_5.sqlite keyed by session_id
#           (0.148 moved thread metadata off session_index.jsonl), else
#           threads.title (first user message), else the prompt.
extract_summary() {
    local payload="$1" title="" src="final"
    [ -n "$JQ" ] || return 0
    [ -n "$payload" ] || return 0

    case "$agent" in
        claude)
            local transcript
            transcript=$("$JQ" -r '.transcript_path // empty' <<<"$payload" 2>/dev/null || true)
            if [ -n "$transcript" ] && [ -f "$transcript" ]; then
                title=$(tail -c 65536 "$transcript" 2>/dev/null \
                    | grep '"type":"ai-title"' | tail -1 \
                    | "$JQ" -r '.aiTitle // empty' 2>/dev/null || true)
            fi
            if [ -z "$title" ]; then
                title=$("$JQ" -r '.session_title // empty' <<<"$payload" 2>/dev/null || true)
            fi
            if [ -z "$title" ]; then
                title=$("$JQ" -r '.prompt // empty' <<<"$payload" 2>/dev/null || true)
                src="interim"
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
    esac

    # An agent-written title is already short and is displayed as-is if the
    # condense fails; a prompt is a whole sentence whose first 60 chars can cut
    # off the identifying words the condenser exists to find, so give it room.
    # Display is unaffected — compose_summary trims to 24 chars either way.
    if [ "$src" = "interim" ]; then
        title=$(printf '%s' "$title" | sanitize_summary 200)
    else
        title=$(printf '%s' "$title" | sanitize_summary)
    fi
    [ -n "$title" ] || return 0
    printf '%s\t%s' "$src" "$title"
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
    case "$(window_state "$win")" in
        needs-*|failed)
            # The user came to answer the prompt. Discharge the tint, but
            # stamp @agent_pending so the next heartbeat can restore
            # `running` once the answered turn resumes — the discharge
            # happens BEFORE any post-answer heartbeat, so heartbeat's own
            # needs-input re-arm can never fire in this flow.
            set_state "$win" idle
            tmux set-option -w -t "$win" @agent_pending "$(now_epoch)" 2>/dev/null || true
            ;;
        done) set_state "$win" idle ;;
    esac
    exit 0
fi

# Detached condenser (spawned by compose_summary): distill the raw title to
# its 2-4 identifying words with a one-shot copilot/haiku call, cache it,
# update the tab. argv: condense <window_id> <project> <raw-title>. Never
# invoked by hooks directly, so a slow/failed model call only delays the
# title swap.
if [ "$mode" = "condense" ] || [ "$mode" = "condense-locked" ]; then
    win="${2:-}"; proj="${3:-}"; raw="${4:-}"
    { [ -n "$win" ] && [ -n "$raw" ]; } || exit 0
    key=$(title_key "$raw")
    if [ "$mode" = "condense" ]; then
        # A mkdir lock survives SIGKILL and then rejects this title forever.
        # Keep the advisory-lock inode permanently; the kernel releases its
        # lock when the worker exits, even if no shell trap can run. The new
        # suffix also bypasses orphaned legacy .lock directories safely.
        exec python3 - "${TMPDIR:-/tmp}/agent-tab-condense.$key.flock" "$0" "$win" "$proj" "$raw" <<'PYLOCK'
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

    short=$(cached_short "$raw")
    if [ -z "$short" ]; then
        # Back off if a recent attempt for this title failed, so a persistent
        # copilot error (not logged in, out of credit) isn't re-run every turn.
        negative_fresh "$key" && exit 0

        # gtimeout is coreutils' name when /usr/bin/timeout is absent (macOS).
        TO=$(command -v timeout || command -v gtimeout || true)
        PROMPT="From the coding-session title or first message below, output a 2-4 word tab label built from the MOST SPECIFIC, distinctive words in it: the concrete subject, target, feature, file, tech, or action unique to THIS task. Keep proper nouns and real names. DROP generic filler (project, code, app, task, session, various, deep, inspection, thing, stuff, update, changes, improve, and a bare fix/add/refactor when what it acts on is unnamed). Someone reading the label should be able to guess the task back. Prefer the unusual identifying word over the category word. Output ONLY the label - no punctuation, quotes, or explanation.

Examples:
- 'deep inspection of this project docs, I wanna open source it, make sure nothing reads like AI or oddly specific to me' => Open-source doc cleanup
- 'refactor the auth middleware to use JWTs instead of session cookies' => JWT auth refactor
- 'figure out why the websocket reconnect drops messages under load' => Websocket reconnect bug

Title: $raw"
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
            short=$(printf '%s' "$out" | grep -m1 . | sanitize_summary | cut -d' ' -f1-4 || true)
            short=$(fit_words "$short" 24)
            valid_short "$short" || short=""
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
            pend=$(tmux show-options -wqv -t "$win" @agent_pending 2>/dev/null || true)
            if [ -n "$pend" ]; then
                clear_pending "$win"
                case "$pend" in *[!0-9]*) pend=0 ;; esac
                if [ "$(( $(now_epoch) - pend ))" -lt 3600 ]; then
                    set_state "$win" running
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

if [ "$agent" = claude ]; then
    # A new Claude session in the same window invalidates detached Codex
    # title writers even if the previous frontend has not exited yet.
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
    case "$("$JQ" -r '.prompt // empty' <<<"$payload" 2>/dev/null | head -c 40)" in
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
        # The terminal bridge observes turn/completed with interrupted status.
        # Keep the conversation label, but discharge working/attention state.
        [ "$agent" = codex ] || exit 0
        set_state "$win" idle
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
        set_state "$win" idle
        clear_pending "$win"
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
        take_detail running run
        set_state "$win" running
        clear_pending "$win"
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
            set_state "$win" idle
            tmux set-option -w -t "$win" @agent_pending "$(now_epoch)" 2>/dev/null || true
        else
            set_state "$win" needs-input
        fi
        ;;
    failed)
        # The turn itself died (529, overloaded). RED, and red now means
        # exactly this — nothing is waiting on an answer, something broke.
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
        if [ "$bg_pending" -eq 1 ]; then
            set_state "$win" running
            compose_summary "$win" "$(extract_summary "$payload")" "$payload"
            exit 0
        fi
        # Don't tint a window the user is looking at — they saw it finish.
        # Watched AND WezTerm frontmost (see viewing_now): a watched window
        # in a backgrounded terminal was the one way to miss a finish.
        if viewing_now; then
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
        echo "Usage: agent-tab-indicator.sh <idle|running|heartbeat|needs-approval|failed|done|interrupt|clear|clear-current> [claude|codex]" >&2
        # exit 0, not 1: codex treats a hook's exit status as a GATE (see the
        # header), so a typo'd mode in hooks.json would block every codex
        # event rather than just failing to paint a tab. A silently inert
        # indicator is a far better failure than a wedged agent; the usage
        # line still goes to stderr for anyone running this by hand.
        exit 0
        ;;
esac
