#!/usr/bin/env bash
# agent-jump.sh — the ONE answer to "which tab wants me next?". prefix g /
# prefix G use it today; the agent roster (and its WezTerm strip) are meant to
# call the same modes rather than grow a second ordering.
#
#   agent-jump.sh next <client_tty>          go to the next tab that needs you
#   agent-jump.sh back <client_tty>          return to where the jumps started
#   agent-jump.sh goto <client_tty> <win>    go to one window (roster ⏎, clicks)
#   agent-jump.sh list                       the needs-you queue, in order:
#                                            win<TAB>session<TAB>index<TAB>state<TAB>since<TAB>label
#
# ORDER. failed (red) > needs-input (yellow) > done (green), then the oldest
# @agent_since first, then session name and window index so equal stamps
# (the watcher stamps at 1 s resolution) still sort the same way every time.
# `done` while @agent_workflow is set is NOT in the queue: the tab renders
# untinted because a fleet is still out (see tmux.conf.tmpl), so it doesn't
# want you yet. A window with no stamp sorts last within its tier.
#
# NEVER INTO: agents (pty-MCP shells), tasks (CuaNotch's broker), stash (the
# parked-tab holding session — switching there would land you inside the
# park), scratch, btop-popup (toggle-btop-popup.sh's session). `list` drops
# them; go_to refuses them too, because `back`'s origin (or a goto target) can
# be parked AFTER the chain was recorded.
#
# SELECT FIRST, THEN SWITCH. The tint is discharged by after-select-window[0]
# → `agent-tab-indicator.sh clear-current`, and a bare `switch-client` fires
# no select hook — a cross-session jump would land on a tab still painted, and
# `next` would keep cycling back to it. So every move is
#   select-window -t =<session>:<win> ; switch-client -c <tty> -t =<session>
# The session is named explicitly because a window can be LINKED into several
# sessions, and `display-message -t <win>` resolves it to whichever had the
# latest activity (often stash). `next` uses the session from its list row;
# back/goto prefer the client's own session, then the first linked session
# that is not excluded. The =<session>:<win> target keeps select and switch
# in the same session. The hook's #{window_id} is the command's target, so
# it discharges the right window even before the client arrives, and client-session-changed[1] then
# runs cua-notch-visit for the notch. The client is checked BEFORE the select:
# a select whose switch then fails would discharge a tint nobody saw.
#
# THE CHAIN. `back` undoes a run of `next`s. tmux has no client-scoped user
# options, so the chain lives in a global keyed by the client's tty:
#   @agent_jump_<tty> = "<origin window> <window we last landed on>"
# It resets lazily, with no hook of its own: `next` keeps the origin only
# while the client is still on the window it last landed on (any other
# navigation in between starts a fresh chain from where you are), and `back`
# acts only from that landing, and only if the origin still exists.
#
# Messages are worded to stay info chips: message-format paints anything
# starting "no …"/"not …"/"can't …" as an error.
#
# display-message takes its text as a FORMAT, so say() doubles every `#`:
# a summary, window name or session name holding #{…}/#[…] is shown, not
# expanded or styled. Nothing here uses formats in its own messages.

set -uo pipefail

US=$'\x1f'
EXCLUDE=" agents tasks stash scratch btop-popup "

mode="${1:-}"
tty="${2:-}"

say() {
    local m="${1//\#/##}"
    if [ -n "$tty" ]; then
        tmux display-message -c "$tty" "$m" 2>/dev/null || tmux display-message "$m" 2>/dev/null
    else
        tmux display-message "$m" 2>/dev/null
    fi
}

list_needs() {
    tmux list-windows -a -F "#{window_id}${US}#{session_name}${US}#{window_index}${US}#{@agent_state}${US}#{@agent_workflow}${US}#{@agent_since}${US}#{?#{n:#{@agent_summary}},#{@agent_summary},#{window_name}}" 2>/dev/null |
    awk -F "$US" -v ex="$EXCLUDE" '
        index(ex, " " $2 " ") { next }
        {
            if ($4 == "failed") p = 0
            else if ($4 == "needs-input") p = 1
            else if ($4 == "done" && $5 == "") p = 2
            else next
            split($6, a, " ")
            t = (a[1] ~ /^[0-9]+$/) ? a[1] : 9999999999
            label = $7; gsub(/\t/, " ", label)
            printf "%d\t%s\t%s\t%09d\t%s\t%s\t%s\t%s\t%s\t%s\n", p, t, $2, $3, $1, $2, $3, $4, t, label
        }' |
    sort -t "$(printf '\t')" -k1,1n -k2,2n -k3,3 -k4,4 |
    # `list-windows -a` prints a linked window once per session it is linked
    # into; keep its first (sorted) row so it is queued and counted once.
    # Excluded sessions were dropped above, so that row is never one of them.
    awk -F '\t' '!seen[$5]++' |
    cut -f5-
}

# Sets cur_sess / cur_win for $tty. Fails if that client is not attached.
client_info() {
    local line
    [ -n "$tty" ] || return 1
    line=$(tmux list-clients -F "#{client_tty}${US}#{session_name}${US}#{window_id}" 2>/dev/null |
           awk -F "$US" -v t="$tty" '$1 == t { print; exit }')
    [ -n "$line" ] || return 1
    IFS="$US" read -r _ cur_sess cur_win <<<"$line"
}

# go_to <win> [<session>]. Sets go_sess to the session it moves in: the one
# given, else the client's own session if <win> is linked there, else the
# first linked session not in EXCLUDE, else (all excluded) the first one.
# Returns 1 if the window is gone, 2 if go_sess is an EXCLUDE session (e.g.
# the origin was parked into stash mid-chain): never switch the client in
# there — refused() explains instead.
go_to() {
    local win="$1"
    go_sess="${2:-}"
    if [ -z "$go_sess" ]; then
        go_sess=$(tmux list-windows -a -F "#{window_id}${US}#{session_name}" 2>/dev/null |
            awk -F "$US" -v w="$win" -v cur="${cur_sess:-}" -v ex="$EXCLUDE" '
                $1 != w { next }
                first == "" { first = $2 }
                index(ex, " " $2 " ") { next }
                $2 == cur { mine = $2 }
                ok == "" { ok = $2 }
                END { print (mine != "" ? mine : (ok != "" ? ok : first)) }')
        [ -n "$go_sess" ] || return 1
    fi
    case "$EXCLUDE" in *" $go_sess "*) return 2 ;; esac
    tmux select-window -t "=$go_sess:$win" \; switch-client -c "$tty" -t "=$go_sess" 2>/dev/null
}

refused() {
    if [ "$go_sess" = stash ]; then
        say "that tab was parked · prefix h brings it back"
    else
        say "that tab is in $go_sess · jumps skip that session"
    fi
}

chain_key() { printf '@agent_jump_%s' "${tty//[^A-Za-z0-9]/_}"; }

describe() {
    case "$1" in
        failed) printf 'failed' ;;
        needs-input) printf 'waiting on you' ;;
        done) printf 'done' ;;
        *) printf '%s' "$1" ;;
    esac
}

case "$mode" in
    list)
        list_needs
        ;;

    next)
        client_info || { say "agent-jump: client $tty is not attached"; exit 0; }
        queue=$(list_needs | awk -F '\t' -v c="$cur_win" '$1 != c')
        if [ -z "$queue" ]; then
            say "nothing needs you"
            exit 0
        fi
        IFS=$'\t' read -r twin tsess tidx tstate _ tlabel <<<"${queue%%$'\n'*}"
        more=$(( $(printf '%s\n' "$queue" | wc -l) - 1 ))
        key=$(chain_key)
        chain=$(tmux show-options -gqv "$key" 2>/dev/null)
        origin="${chain%% *}"
        if [ -z "$chain" ] || [ "${chain##* }" != "$cur_win" ]; then
            origin="$cur_win"
        fi
        go_to "$twin" "$tsess"; rc=$?
        [ "$rc" -eq 2 ] && { refused; exit 0; }
        [ "$rc" -eq 0 ] || { say "agent-jump: $tsess:$tidx is gone"; exit 0; }
        tmux set-option -g "$key" "$origin $twin" 2>/dev/null
        msg="→ $tsess:$tidx $tlabel · $(describe "$tstate")"
        [ "$more" -gt 0 ] && msg="$msg · $more more"
        say "$msg"
        ;;

    back)
        client_info || { say "agent-jump: client $tty is not attached"; exit 0; }
        key=$(chain_key)
        chain=$(tmux show-options -gqv "$key" 2>/dev/null)
        if [ -z "$chain" ] || [ "${chain##* }" != "$cur_win" ]; then
            say "nothing to go back to"
            exit 0
        fi
        origin="${chain%% *}"
        tmux set-option -gu "$key" 2>/dev/null          # chain is spent either way
        go_to "$origin"; rc=$?
        [ "$rc" -eq 2 ] && { refused; exit 0; }
        [ "$rc" -eq 0 ] || { say "the window you jumped from is gone"; exit 0; }
        say "← back"
        ;;

    goto)
        # The roster sends parked windows through `stash.sh unstash`, not
        # here; this guard only keeps a stray goto out of EXCLUDE sessions.
        win="${3:-}"
        [ -n "$win" ] || { say "agent-jump: goto needs a window"; exit 0; }
        client_info || { say "agent-jump: client $tty is not attached"; exit 0; }
        go_to "$win"; rc=$?
        if [ "$rc" -eq 2 ]; then refused
        elif [ "$rc" -ne 0 ]; then say "agent-jump: window $win is gone"
        fi
        ;;

    *)
        echo "usage: agent-jump.sh next|back <client_tty> | goto <client_tty> <window> | list" >&2
        exit 2
        ;;
esac
