#!/usr/bin/env bash
# Reopen closed tabs. Every close is kept for a while (RETAIN_DAYS, at most
# MAX_ENTRIES) with its cwd, its place in the tab bar, a colour snapshot of the
# screen as it was, and — if a claude / codex / opencode session was running in
# it — enough to resume that conversation.
#
#   closed-tabs.sh close <pane_id>                 snapshot, record, kill the pane
#   closed-tabs.sh pick-popup <session_id> <tty>   fzf picker over the history
#   closed-tabs.sh reopen <session_id> <tty> [id…] reopen these (default: newest)
#   closed-tabs.sh forget <id…>                    drop entries from the history
#   closed-tabs.sh list                            print the history, newest first
#
# Bound in tmux.conf: prefix x (CMD+W, asks y/n) and prefix C-l (CMD+SHIFT+W,
# doesn't) close through here; prefix X (CMD+Z) opens the picker. Only closes
# that go through `close` are remembered — a tab that ends because its shell
# exited never passes through a key binding, and by the time any tmux hook
# fires its process tree is gone.
#
# Storage, all under ~/.local/state/tmux-closed-tabs/:
#   history.jsonl        one JSON object per closed tab, oldest first
#   previews/<id>.ansi   `capture-pane -e` of the screen at close time
# Pruned on every close and every picker open. A snapshot is the visible screen
# only (tens of KB), so a full history is a few MB at most.
#
# Agent detection and session-id resolution are NOT reimplemented here: they
# come from tmux-assistant-resurrect's save script, which is written to be
# sourced (its main() is guarded) and already knows every tool's quirks —
# claude's SessionStart state file, codex's session-tags.jsonl and thread DB,
# opencode's plugin file and SQLite fallback, and stripping resume flags out of
# the original command line. That is the same code that resumes these sessions
# after a reboot, so a reopened tab comes back exactly the way a resurrected
# one does.
#
# A reopened tab is a normal login zsh with the agent started through
# ZSH_AUTOSTART (see the end of .zshrc), same as a CMD+R tab: quitting the
# agent leaves a prompt instead of closing the tab.

set -uo pipefail

# Re-entry (popup, preview, hand-offs through `tmux run-shell`) goes through
# SELF with STATE_HOME carried along explicitly: commands tmux runs get the
# SERVER's environment, not this process's. With the installed path and an
# inherited XDG_STATE_HOME, a scratch copy pointed at a test history handed its
# reopen to the installed script, which reopened the REAL history — ten tabs
# on 2026-09-24. Whatever copy and history started a flow now finish it.
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"
STATE_DIR="$STATE_HOME/tmux-closed-tabs"
HISTORY="$STATE_DIR/history.jsonl"
PREVIEWS="$STATE_DIR/previews"
LOCK="$STATE_DIR/lock"
LOGFILE="$STATE_DIR/log"
RETAIN_DAYS=7
MAX_ENTRIES=100
# The preview sits UNDER the list so it gets (nearly) the full client width: a
# snapshot is as wide as the tab was, and anything wider than the preview is
# clipped on the right.
POPUP_W_PCT=94
POPUP_H_PCT=85

RESURRECT_SAVE="$HOME/.config/tmux/plugins/tmux-assistant-resurrect/scripts/save-assistant-sessions.sh"

mkdir -p "$STATE_DIR" "$PREVIEWS"

msg() { tmux display-message "$*" 2>/dev/null; }
log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOGFILE" 2>/dev/null; }

# mkdir is the atomic lock (macOS has no flock(1)). A lock older than 10s is a
# crashed holder; it is broken with rename(2), not rm+mkdir. The age is checked
# AGAIN on what the rename actually took: two waiters can both judge the same
# dead lock stale, and the slower one's rename would otherwise carry off the
# fresh lock the faster one had just made. A fresh catch is put back.
lock_age() { echo $(( $(date +%s) - $(stat -f %m "$1" 2>/dev/null || date +%s) )); }
lock_acquire() {
    local i=0
    until mkdir "$LOCK" 2>/dev/null; do
        if [ $((i % 20)) -eq 19 ] && [ "$(lock_age "$LOCK")" -gt 10 ] \
           && mv "$LOCK" "$LOCK.stale.$$" 2>/dev/null; then
            if [ "$(lock_age "$LOCK.stale.$$")" -gt 10 ]; then
                rm -rf "$LOCK.stale.$$"
                continue
            fi
            # Guarded: mv onto an existing directory moves INTO it.
            [ -e "$LOCK" ] || mv "$LOCK.stale.$$" "$LOCK" 2>/dev/null
            rm -rf "$LOCK.stale.$$"
        fi
        i=$((i + 1))
        [ "$i" -ge 200 ] && return 1
        sleep 0.05
    done
}
lock_release() { rmdir "$LOCK" 2>/dev/null; }

# Rewrite the history through jq, atomically. Caller holds the lock.
history_filter() {
    [ -s "$HISTORY" ] || return 0
    jq -c "$@" "$HISTORY" >"$HISTORY.tmp" 2>/dev/null && mv "$HISTORY.tmp" "$HISTORY"
}

# ids as a JSON array, for jq --argjson.
ids_json() { printf '%s\n' "$@" | jq -R . | jq -sc .; }

# Age out old entries, cap the count, and delete snapshots nothing points at
# (including any left by a close that died halfway). Caller holds the lock.
prune() {
    local cutoff=$(( $(date +%s) - RETAIN_DAYS * 86400 ))
    history_filter --argjson cut "$cutoff" 'select(.ts >= $cut)'
    # grep -c prints "0" AND exits 1 on an empty file, so `|| echo 0` would
    # yield "0\n0"; wc -l always prints exactly one number.
    local n; n=$(wc -l <"$HISTORY" 2>/dev/null | tr -d ' '); n=${n:-0}
    if [ "$n" -gt "$MAX_ENTRIES" ]; then
        tail -n "$MAX_ENTRIES" "$HISTORY" >"$HISTORY.tmp" && mv "$HISTORY.tmp" "$HISTORY"
    fi
    # One jq for the live ids, then in-shell matching: a grep per snapshot
    # was a fork per history entry on every close and every CMD+Z.
    local keep f id
    keep=$'\n'$(jq -r '.id' "$HISTORY" 2>/dev/null)$'\n'
    for f in "$PREVIEWS"/*.ansi; do
        [ -e "$f" ] || continue
        id=${f##*/}; id=${id%.ansi}
        [[ "$keep" == *$'\n'"$id"$'\n'* ]] || rm -f "$f"
    done
}

# Where the plugin's own hooks keep per-pid state (claude-<pid>.json from its
# SessionStart hook, opencode-<pid>.json from its plugin). Same expression as
# save-assistant-sessions.sh.
ASSISTANT_STATE_DIR="${TMUX_ASSISTANT_RESURRECT_DIR:-${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/tmux-assistant-resurrect}"

# The part of the agent lookup that needs the agent ALIVE, kept as small as
# possible because the tab stays on screen until it's done: find the agent
# process and copy its state file somewhere safe (claude deletes its own as it
# exits). Resolving the session id from that copy happens after the kill.
# Prints "<tool>\x1f<pid>\x1f<args>\x1f<state copy dir>" or nothing.
agent_snapshot() {
    local pane_pid="$1" pane_tty="$2"
    local lib; lib="$(dirname "$RESURRECT_SAVE")/lib-detect.sh"
    [ -r "$lib" ] || return 0
    (
        # shellcheck source=/dev/null
        source "$lib" 2>/dev/null || exit 0
        # Only the processes on this pane's terminal: 2-3ms, against ~50ms for
        # the whole-machine `ps -eo` the plugin takes by default. The agent and
        # everything under it share the pane's tty, so the tree walk sees all
        # it needs.
        local snap; snap=$(ps -t "${pane_tty#/dev/}" -o pid=,ppid=,args= 2>/dev/null)
        [ -n "$snap" ] || exit 0
        local apid args tool
        apid=$(pane_has_assistant "$pane_pid" "$snap") || exit 0
        args=$(awk -v p="$apid" '$1 == p { sub(/^ *[0-9]+ +[0-9]+ /, ""); print; exit }' <<<"$snap")
        tool=$(detect_tool "$args")
        [ -n "$tool" ] || exit 0
        local copy; copy=$(mktemp -d)
        cp "$ASSISTANT_STATE_DIR/$tool-$apid.json" "$copy/" 2>/dev/null
        printf '%s\x1f%s\x1f%s\x1f%s\n' "$tool" "$apid" "$args" "$copy"
    )
}

# The session behind an agent, through the plugin's own resolver (emit_session).
# With a state dir, reads that copy instead of the live one — which is what
# lets this run after the agent is gone.
# Prints "<session_id>\x1f<cli_args>\x1f<model>\x1f<env json>" or nothing.
agent_session() {
    local tool="$1" apid="$2" args="$3" cwd="$4" state="${5:-}"
    [ -r "$RESURRECT_SAVE" ] || return 0
    (
        # The save script turns on errexit/nounset when sourced; its functions
        # are written for that, and this subshell keeps it from leaking out.
        # shellcheck source=/dev/null
        source "$RESURRECT_SAVE" 2>/dev/null || exit 0
        set +e
        [ -n "$state" ] && STATE_DIR="$state"
        # Seed its per-process flag cache with its own fallback list. Otherwise
        # stripping the resume flags runs `claude --help` on every close, which
        # is ~300ms of the key feeling slow.
        _SESSION_FLAGS_claude="${SESSION_FLAGS_FALLBACK_claude:-}"
        _SESSION_FLAGS_opencode="${SESSION_FLAGS_FALLBACK_opencode:-}"
        PARTS_FILE=$(mktemp)
        emit_session "-" "$tool" "$apid" "$args" "$cwd" 1 0 >/dev/null 2>&1
        jq -r '[.session_id // "", .cli_args // "", .model // "", (.env // {} | tojson)] | join("\u001f")' \
            "$PARTS_FILE" 2>/dev/null
        rm -f "$PARTS_FILE"
    )
}

# The same resume command restore-assistant-sessions.sh builds.
build_cmd() {
    local tool="$1" sid="$2" cli_args="$3" model="$4" env_json="$5"
    [ -n "$sid" ] || return 0
    # shellcheck source=/dev/null
    source "$(dirname "$RESURRECT_SAVE")/lib-detect.sh" 2>/dev/null || return 0
    local q_args="" a
    set -f
    for a in $cli_args; do q_args+=" $(posix_quote "$a")"; done
    set +f
    # Only the variables @assistant-resurrect-capture-env lists, as a prefix.
    [ -n "$env_json" ] || env_json='{}'
    local env_prefix="" var val
    while IFS=$'\x1f' read -r var val; do
        [[ "$var" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && [ -n "$val" ] || continue
        env_prefix+="$var=$(posix_quote "$val") "
    done < <(jq -r --arg vars "$(tmux show-option -gqv @assistant-resurrect-capture-env 2>/dev/null)" \
        '($vars | split(" ")) as $v | to_entries[] | select(.key as $k | $v | index($k))
         | "\(.key)\u001f\(.value)"' <<<"$env_json" 2>/dev/null)
    case "$tool" in
    claude)
        case "$cli_args" in *--model*) ;; *) [ -n "$model" ] && q_args+=" --model $(posix_quote "$model")" ;; esac
        echo "${env_prefix}command claude${q_args} --resume $(posix_quote "$sid")" ;;
    opencode) echo "${env_prefix}command opencode${q_args} -s $(posix_quote "$sid")" ;;
    codex)    echo "${env_prefix}command codex${q_args} resume $(posix_quote "$sid")" ;;
    esac
}

# Ordered for latency: everything that has to be read from the live pane (its
# screen, its agent's session) comes first, then the pane dies, and only then
# the bookkeeping — building the resume command, writing and pruning the
# history. The tab is off the bar before any of that starts.
do_close() {
    local pane="${1:-}"
    # An empty -t resolves to the CURRENT pane, not to nothing.
    [ -n "$pane" ] || { msg "closed-tabs: no pane given"; return 1; }

    local info
    info=$(tmux display-message -p -t "$pane" \
        '#{pane_id}|#{pane_pid}|#{pane_tty}|#{session_name}|#{session_id}|#{session_created}|#{window_index}|#{pane_current_path}|#{?#{n:#{@agent_summary}},#{@agent_summary},#{window_name}}' 2>/dev/null)
    # display-message against a dead target can succeed with an empty
    # expansion — test the expansion, not the exit status.
    local pane_id pane_pid pane_tty sess tsess tcreated widx cwd label
    IFS='|' read -r pane_id pane_pid pane_tty sess tsess tcreated widx cwd label <<<"$info"
    [ -n "$pane_id" ] || return 1

    local ts id
    ts=$(date +%s); id="$ts-$$"

    # The screen as it is right now, for the picker, captured alongside the
    # agent lookup. Trailing blank rows are dropped so the preview (which
    # follows the bottom) lands on the last line of real output rather than on
    # empty space under a shell prompt.
    tmux capture-pane -ep -t "$pane_id" 2>/dev/null | perl -e '
        my @l = <STDIN>;
        while (@l && $l[-1] =~ /^(?:\e\[[0-9;:]*[A-Za-z]|\s)*$/) { pop @l }
        print @l;' >"$PREVIEWS/$id.ansi" &
    local cap_pid=$!

    local tool="" apid="" args="" copy="" snap
    snap=$(agent_snapshot "$pane_pid" "$pane_tty")
    [ -n "$snap" ] && IFS=$'\x1f' read -r tool apid args copy <<<"$snap"

    # codex is the exception that resolves BEFORE the kill: its fallbacks match
    # threads against the live process's start time, which a dead pid no
    # longer has.
    local sid="" cli_args="" model="" env_json="" sess_info=""
    [ "$tool" = codex ] && sess_info=$(agent_session "$tool" "$apid" "$args" "$cwd" "$copy")
    wait "$cap_pid"

    # SIGTERM first so the agent starts its own shutdown, then take the pane at
    # once. Waiting for it to exit is ~2.2s (claude reaps every MCP child
    # first) and buys nothing measurable: with and without the SIGTERM, all of
    # a claude's descendants were gone within 6s of the kill-pane.
    if [[ "$apid" =~ ^[0-9]+$ ]] && [ "$apid" -gt 1 ]; then
        kill -TERM "$apid" 2>/dev/null
    fi
    tmux kill-pane -t "$pane_id" 2>/dev/null

    if [ -n "$tool" ] && [ "$tool" != codex ]; then
        sess_info=$(agent_session "$tool" "$apid" "$args" "$cwd" "$copy")
    fi
    [ -n "$copy" ] && rm -rf "$copy"
    [ -n "$sess_info" ] && IFS=$'\x1f' read -r sid cli_args model env_json <<<"$sess_info"

    local cmd; cmd=$(build_cmd "$tool" "$sid" "$cli_args" "$model" "$env_json")
    local rec
    rec=$(jq -cn --arg id "$id" --arg ts "$ts" --arg sess "$sess" --arg widx "$widx" \
        --arg tsess "$tsess" --arg tcreated "$tcreated" \
        --arg cwd "$cwd" --arg label "$label" --arg tool "$tool" --arg sid "$sid" --arg cmd "$cmd" \
        '{id: $id, ts: ($ts|tonumber), session: $sess, tmux_session: $tsess,
          session_created: $tcreated, index: ($widx|tonumber), cwd: $cwd,
          label: $label, tool: $tool, session_id: $sid, cmd: $cmd}')

    if lock_acquire; then
        printf '%s\n' "$rec" >>"$HISTORY"
        prune
        lock_release
    else
        # The tab is already gone, so write it anyway: a lone O_APPEND line is
        # safe next to other appends, and only a prune racing it could drop it.
        printf '%s\n' "$rec" >>"$HISTORY"
        log "history lock busy — appended unlocked"
    fi
    log "close $sess:$widx cwd=$cwd tool=${tool:--} sid=${sid:--}"
    if [ -n "$tool" ] && [ -z "$sid" ]; then
        msg "closed-tabs: couldn't find the $tool session id — reopening will give a plain shell"
    fi
    return 0
}

# Recreate one recorded tab. Prints the new window id.
reopen_one() {
    local rec="$1" client_sess="$2"
    local sess tsess tcreated idx cwd cmd
    sess=$(jq -r '.session' <<<"$rec"); idx=$(jq -r '.index' <<<"$rec")
    cwd=$(jq -r '.cwd' <<<"$rec");      cmd=$(jq -r '.cmd // empty' <<<"$rec")
    tsess=$(jq -r '.tmux_session // empty' <<<"$rec")
    tcreated=$(jq -r '.session_created // empty' <<<"$rec")

    # Back in its own session, found by identity first: the tmux id survives a
    # rename, but ids are handed out again after a server restart, so the
    # session's creation time has to match too. Then by name (a session
    # resurrect recreated after a restart), then wherever you are.
    local now_created=""
    [ -n "$tsess" ] && now_created=$(tmux display-message -p -t "$tsess" '#{session_created}' 2>/dev/null)
    if [ -n "$tcreated" ] && [ "$now_created" = "$tcreated" ]; then
        sess=$(tmux display-message -p -t "$tsess" '#{session_name}' 2>/dev/null)
    elif ! tmux has-session -t "=$sess" 2>/dev/null; then
        sess=$(tmux display-message -p -t "$client_sess" '#{session_name}' 2>/dev/null)
        [ -n "$sess" ] || return 1
        msg "closed-tabs: its session is gone — reopened in $sess"
    fi
    [ -d "$cwd" ] || cwd="$HOME"

    # Same slot it left: insert before whatever slid into its index (with
    # renumber-windows on, its right-hand neighbour), or at the end if the bar
    # is now shorter than that.
    local -a where
    if tmux list-windows -t "=$sess" -F '#{window_index}' 2>/dev/null | grep -qx "$idx"; then
        where=(-b -t "=$sess:$idx")
    else
        where=(-t "=$sess:")
    fi
    local -a autostart=()
    [ -n "$cmd" ] && autostart=(-e "ZSH_AUTOSTART=$cmd")
    tmux new-window -P -F '#{window_id}' "${where[@]}" -c "$cwd" "${autostart[@]}" 2>/dev/null
}

# Reopen the given entries (default: the newest). Several at once go back
# newest-closed first: each one's recorded index was taken AFTER the ones closed
# before it had left, so undoing in reverse order puts every tab back in its
# original slot.
do_reopen() {
    local client_sess="${1:-}" client_tty="${2:-}"
    shift 2 2>/dev/null
    local recs
    lock_acquire || { msg "closed-tabs: history busy"; return 1; }
    if [ "$#" -gt 0 ]; then
        recs=$(jq -c --argjson ids "$(ids_json "$@")" 'select(.id as $i | $ids | index($i))' "$HISTORY" 2>/dev/null)
    else
        recs=$(tail -n 1 "$HISTORY" 2>/dev/null)
    fi
    if [ -z "$recs" ]; then
        lock_release
        msg "closed-tabs: nothing to reopen"
        return 0
    fi
    local ids; ids=$(jq -r '.id' <<<"$recs")
    # shellcheck disable=SC2086
    history_filter --argjson ids "$(ids_json $ids)" 'select(.id as $i | $ids | index($i) | not)'
    lock_release

    local rec win first="" n=0 failed=0 lost=0
    while IFS= read -r rec; do
        [ -n "$rec" ] || continue
        if win=$(reopen_one "$rec" "$client_sess") && [ -n "$win" ]; then
            [ -n "$first" ] || first="$win"
            n=$((n + 1))
            rm -f "$PREVIEWS/$(jq -r '.id' <<<"$rec").ansi"
            log "reopen $(jq -r '"\(.session) cwd=\(.cwd) tool=\(.tool) cmd=\(.cmd)"' <<<"$rec")"
        else
            # Put it back rather than lose it.
            failed=$((failed + 1))
            if lock_acquire; then
                printf '%s\n' "$rec" >>"$HISTORY"; lock_release
            else
                lost=$((lost + 1))
                log "reopen failed and history busy, dropped: $rec"
            fi
        fi
    # Newest close first. Ties on .ts (two closes in the same second) fall
    # back to file order, reversed — sort_by is stable, so -.ts alone would
    # replay same-second closes oldest first and swap their slots.
    done < <(jq -sc 'to_entries | sort_by(-.value.ts, -.key) | .[].value' <<<"$recs")

    [ -n "$client_tty" ] && [ -n "$first" ] && tmux switch-client -c "$client_tty" -t "$first" 2>/dev/null
    if [ "$lost" -gt 0 ]; then
        msg "closed-tabs: reopened $n, couldn't recreate $failed ($lost lost: history busy, see log)"
    elif [ "$failed" -gt 0 ]; then
        msg "closed-tabs: reopened $n, couldn't recreate $failed (kept in the history)"
    fi
}

do_forget() {
    [ "$#" -gt 0 ] || return 0
    lock_acquire || return 1
    history_filter --argjson ids "$(ids_json "$@")" 'select(.id as $i | $ids | index($i) | not)'
    local id; for id in "$@"; do rm -f "$PREVIEWS/$id.ansi"; done
    lock_release
    msg "closed-tabs: forgot $# tab(s)"
}

# fzf needs a terminal, so the picker runs inside a popup that re-enters this
# script as `pick`. Called from a FOREGROUND run-shell: a backgrounded one has
# no client to raise a popup on.
do_pick_popup() {
    local client_sess="${1:-}" client_tty="${2:-}"
    if lock_acquire; then prune; lock_release; fi
    if [ ! -s "$HISTORY" ]; then
        msg "closed-tabs: nothing to reopen"
        return 0
    fi
    local -a client=()
    [ -n "$client_tty" ] && client=(-c "$client_tty")
    tmux display-popup "${client[@]}" -E -w "${POPUP_W_PCT}%" -h "${POPUP_H_PCT}%" \
        -T ' closed tabs ' "XDG_STATE_HOME='$STATE_HOME' '$SELF' pick '$client_sess' '$client_tty'"
}

# Sets REPLY rather than printing: $(ago …) would fork once per entry.
ago() {
    local s=$(( $2 - $1 ))
    if   [ "$s" -lt 60 ];    then REPLY="just now"
    elif [ "$s" -lt 3600 ];  then REPLY="$((s / 60))m ago"
    elif [ "$s" -lt 86400 ]; then REPLY="$((s / 3600))h ago"
    else                          REPLY="$((s / 86400))d ago"
    fi
}

# Runs inside the popup. Newest first, so CMD+Z then ⏎ is "reopen the last
# thing I closed". ⌃x forgets instead of reopening, via --expect (which puts
# the accepting key on the first output line, empty for a plain ⏎) — the same
# gesture as the stash picker's kill.
do_pick() {
    local client_sess="${1:-}" client_tty="${2:-}"
    local id ts tool label cwd
    local dim=$'\e[2m' mauve=$'\e[38;2;203;166;247m' teal=$'\e[38;2;148;226;213m' off=$'\e[0m'
    local out now; now=$(date +%s)
    out=$(jq -r '[.id, .ts, .tool, .label, .cwd] | join("\u001f")' "$HISTORY" 2>/dev/null \
        | tail -r \
        | while IFS=$'\x1f' read -r id ts tool label cwd; do
            [ -n "$id" ] || continue
            local c="$teal"; [ "$tool" = claude ] && c="$mauve"
            ago "$ts" "$now"
            printf '%s\t%s%-9s%s %s%-9s%s %s\t%s%s%s\n' "$id" \
                "$dim" "$REPLY" "$off" "$c" "${tool:-shell}" "$off" "$label" \
                "$dim" "${cwd/#"$HOME"/\~}" "$off"
          done \
        | fzf --ansi --delimiter='\t' --with-nth=2.. --reverse --multi \
              --prompt='reopen > ' \
              --expect=ctrl-x \
              --bind 'shift-down:toggle+down,shift-up:toggle+up,ctrl-a:select-all,ctrl-d:deselect-all' \
              --preview "XDG_STATE_HOME='$STATE_HOME' '$SELF' preview {1}" \
              --preview-window 'down,72%,border-top,follow' \
              --header '⏎ reopen · Tab or ⇧↑/⇧↓ to pick several · ⌃x forget')
    local key=${out%%$'\n'*}
    local ids; ids=$(printf '%s\n' "$out" | tail -n +2 | cut -f1 | tr '\n' ' ')
    [ -n "${ids// /}" ] || return 0
    # Hand off so the popup closes the moment you pick. The ids are
    # <epoch>-<pid>, nothing a shell would interpret, so unquoted is fine.
    if [ "$key" = "ctrl-x" ]; then
        tmux run-shell -b "XDG_STATE_HOME='$STATE_HOME' '$SELF' forget $ids"
    else
        tmux run-shell -b "XDG_STATE_HOME='$STATE_HOME' '$SELF' reopen '$client_sess' '$client_tty' $ids"
    fi
}

do_preview() {
    local f="$PREVIEWS/${1:-}.ansi"
    if [ -s "$f" ]; then cat "$f"; else echo "(no snapshot)"; fi
}

do_list() {
    [ -s "$HISTORY" ] || { echo "(empty)"; return 0; }
    jq -r '"\(.ts | strflocaltime("%m-%d %H:%M"))  \(.session):\(.index)  \(if .tool == "" then "-" else .tool end)\t\(.label)\t\(.cwd)"' "$HISTORY" | tail -r
}

case "${1:-}" in
close)      shift; do_close "$@" ;;
pick-popup) shift; do_pick_popup "$@" ;;
pick)       shift; do_pick "$@" ;;
preview)    shift; do_preview "$@" ;;
reopen)     shift; do_reopen "$@" ;;
forget)     shift; do_forget "$@" ;;
list)       do_list ;;
*) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
esac
