#!/usr/bin/env bash
# review — the "review old agents" popup (prefix O; docs/review.md). The
# popup itself is `agent-ui review` (the Rust TUI): one stale agent per
# screen, c close / p park / s snooze / k keep / v go. This script opens it,
# counts for the nudge timer, and runs the popup's "close the space" once
# the tabs it took out of that space are gone.
#
#   review.sh open [<client>]      the popup on that client (default: the
#                                  most recently active one), 76x22 or the
#                                  whole client when it is smaller
#   review.sh count [--min-idle SECS]
#                                  how many agents are up for review
#                                  (`agent-ui review --count`)
#   review.sh close-space <sid> <client> [<win>…]
#                                  the popup's y: `@space_sh close <sid>
#                                  <client>` once none of <win> is in the
#                                  session any more and no other agent is
#                                  there (else a warn toast, nothing closes;
#                                  see do_close_space)
#
# The binary is the server's @space_agent_ui (spaces.conf), else
# ~/.local/bin/agent-ui; space.sh is @space_sh, else the installed one.
# Everything talks to the server in $TMUX (run-shell sets it), and the
# popup is told that server's socket explicitly.
set -uo pipefail

case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
    *[Uu][Tt][Ff]-8*|*[Uu][Tt][Ff]8*) ;;
    *) export LANG=en_US.UTF-8 ;;
esac

SCRIPTS="$HOME/.config/tmux/scripts"
# Most of the client, never under MIN (or the whole client when it is smaller).
POPUP_PCT_W=90
POPUP_PCT_H=88
POPUP_MIN_W=108
POPUP_MIN_H=32
# Centred on the whole WezTerm row while a sidebar sits beside the client
# (tmux.conf.tmpl's @popup_x); tmux's own centre on a server without it.
POPUP_X='#{?#{@popup_x},#{E:@popup_x},#{popup_centre_x}}'

opt() { tmux show -gv "$1" 2>/dev/null; }

agent_ui() {
    local b; b=$(opt @space_agent_ui)
    [ -n "$b" ] || b="$HOME/.local/bin/agent-ui"
    printf '%s' "$b"
}

notify() {
    local n; n=$(opt @notify_sh); [ -n "$n" ] || n="$SCRIPTS/notify.sh"
    [ -x "$n" ] && "$n" "$@"
    return 0
}

# The nudge's toast (review-nudge.sh, titled "Review agents") asks for the
# popup that is opening: take it down in every sidebar.
clear_nudge() {
    local raw kept
    raw=$(opt @toast)
    case "$raw" in *$'\tReview agents\t'*) ;; *) return 0 ;; esac
    kept=$(printf '%s' "$raw" | awk -v RS=$'\x1e' -v sep=$'\x1e' -F'\t' '$7 != "Review agents" { printf "%s%s", s, $0; s = sep }')
    # (An argument ending in `;` is a tmux command separator.)
    case "$kept" in *';') kept="${kept%;}\\;" ;; esac
    tmux set -gq @toast "$kept"
    [ -n "${AGENT_FOCUS_NOTICE-x}" ] || return 0
    notifyutil -p "${AGENT_FOCUS_NOTICE-com.mackhaymond.tmux.focus}" </dev/null >/dev/null 2>&1 &
}

# sq <word>: single-quoted for sh.
sq() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

do_open() {
    local client="${1:-}" b sock cw ch w h
    [ -n "$client" ] || client=$(tmux display-message -p '#{client_name}' 2>/dev/null)
    [ -n "$client" ] || { echo "review: no client" >&2; return 1; }
    b=$(agent_ui)
    if [ ! -x "$b" ]; then
        notify -k warn -c "$client" -- "review: agent-ui is not installed"
        return 0
    fi
    clear_nudge
    sock=$(tmux display-message -p '#{socket_path}')
    IFS=' ' read -r cw ch <<<"$(tmux display-message -p -c "$client" '#{client_width} #{client_height}' 2>/dev/null)"
    w=$(( ${cw:-0} * POPUP_PCT_W / 100 )); h=$(( ${ch:-0} * POPUP_PCT_H / 100 ))
    [ "$w" -lt "$POPUP_MIN_W" ] && w=$POPUP_MIN_W
    [ "$h" -lt "$POPUP_MIN_H" ] && h=$POPUP_MIN_H
    [ -n "${cw:-}" ] && [ "$cw" -lt "$w" ] && w=$cw
    [ -n "${ch:-}" ] && [ "$ch" -lt "$h" ] && h=$ch
    tmux display-popup -c "$client" -E -B -w "$w" -h "$h" -x "$POPUP_X" \
        "$(sq "$b") --socket $(sq "$sock") review --client $(sq "$client")"
}

do_count() {
    local b; b=$(agent_ui)
    [ -x "$b" ] || { echo "review: agent-ui is not installed" >&2; return 1; }
    "$b" review --count "$@"
}

# still_in <list> <win…>: the first <win> that is a line of <list>, if any.
still_in() {
    local list="$1" w; shift
    for w in "$@"; do
        case $'\n'"$list"$'\n' in *$'\n'"$w"$'\n'*) printf '%s' "$w"; return 0 ;; esac
    done
    return 1
}

# close-space <sid> <client> <win…>: the popup's y. Closes the space with
# prefix Q's command (@space_sh close, run as closed-tabs.sh's ask-space-yes
# runs it) only when
#   1. every <win> (the tabs this review just closed / parked / snoozed) has
#      left the session within CLOSE_WAIT ticks: one still there was refused
#      by its script (stash.sh keeps a draft, a busy agent, …), so the space
#      is NOT closed (that tab would go into the backlog with it);
#   2. no other window there has @agent_state: an agent started, reopened
#      or unparked into it while the question was up.
# Either refusal is a warn toast on <client>; nothing closes. A space gone
# meanwhile: nothing.
CLOSE_WAIT=${REVIEW_CLOSE_WAIT:-50}     # 0.2 s ticks: 10 s
do_close_space() {
    local sid="${1:-}" client="${2:-}" i rows ids w name space_sh
    [ -n "$sid" ] || { echo "review: close-space needs a session id" >&2; return 1; }
    shift 2 2>/dev/null || shift $#
    name=$(tmux display-message -p -t "$sid" '#{session_name}' 2>/dev/null) || return 0   # gone already
    i=0
    while :; do
        ids=$(tmux list-windows -t "$sid" -F '#{window_id}' 2>/dev/null) || return 0
        w=$(still_in "$ids" "$@") || break
        i=$((i + 1))
        if [ "$i" -ge "$CLOSE_WAIT" ]; then
            notify -k warn ${client:+-c "$client"} -- "review: a tab stayed in $name ($w) · space not closed"
            return 0
        fi
        sleep 0.2
    done
    rows=$(tmux list-windows -t "$sid" -F '#{window_id} #{?#{@agent_state},agent,}' 2>/dev/null) || return 0
    if printf '%s\n' "$rows" | grep -q ' agent$'; then
        notify -k warn ${client:+-c "$client"} -- "review: $name has an agent again · space not closed"
        return 0
    fi
    space_sh=$(opt @space_sh); [ -n "$space_sh" ] || space_sh="$SCRIPTS/space.sh"
    sh -c "$space_sh close \"\$1\" \"\$2\"" _ "$sid" "$client"
}

case "${1:-}" in
    open)        shift; do_open "$@" ;;
    count)       shift; do_count "$@" ;;
    close-space) shift; do_close_space "$@" ;;
    *) sed -n '2,25p' "$0" >&2; exit 2 ;;
esac
