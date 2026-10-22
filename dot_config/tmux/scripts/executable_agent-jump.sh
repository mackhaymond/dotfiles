#!/usr/bin/env bash
# agent-jump.sh — the ONE answer to "which tab wants me next?". prefix d /
# prefix D, ⌥S / ⌥X (tmux's ⌥S being the fallback when CuaNotch owns ⌥S), the
# agent roster, agent-ui and CuaNotch all call these modes rather than grow a
# second ordering.
#
#   agent-jump.sh next <client_tty>          go to the next tab that needs you
#   agent-jump.sh back <client_tty>          return to where the jumps started
#   agent-jump.sh goto <client_tty> <win> [<session>]
#                                            go to one window (roster ⏎, clicks);
#                                            <session>: the one the row listed it
#                                            under (a linked window has several)
#   agent-jump.sh jump <client_tty> <win> <session>
#                                            one `next` step onto a window the
#                                            CALLER picked (CuaNotch's ⌥S); exit
#                                            code says what happened (below)
#   agent-jump.sh list                       the needs-you queue, in order
#
# LIST FORMAT v1: win_id<TAB>session<TAB>index<TAB>state<TAB>since_epoch<TAB>label (columns only ever appended)
#   one row per window, best first; since_epoch is 9999999999 when unstamped;
#   label is @agent_summary, else the window name, with tabs turned to spaces.
#
# JUMP EXIT CODES (only `jump` uses them; next/back/goto always exit 0
# because tmux's run-shell turns a nonzero exit into a "returned N" view-mode
# pane over the user's window, and the human gets the chip instead):
#   0  landed, or the client was already on <win> (its tint is then
#      discharged as the focus hooks would); silent
#   2  usage (missing <win> or <session>)
#   3  gone: <win> no longer exists, or <session> no longer holds it
#   4  refused: <session> is an EXCLUDE session, or <win> was parked
#   5  client not attached: no client with that tty, or it detached during
#      the move; silent
#
# CUANOTCH CONTRACT. When CuaNotch's keyJumpToNeedy is opt+s (this machine's
# setting), CuaNotch owns ⌥S globally and tmux's `bind -n M-s` is the
# fallback. CuaNotch merges this queue with its own rows (Codex/Claude
# desktop):
#   - queue:   `agent-jump.sh list`, parsed by position (LIST FORMAT v1). The
#              list INCLUDES the client's own window; CuaNotch decides: if
#              WezTerm is frontmost, it drops rows whose window is the chosen
#              client's current window (`tmux list-clients -F '#{client_tty}
#              #{window_id}'`); otherwise it `jump`s there too, which returns
#              0 and clears that window's tint.
#   - landing: `agent-jump.sh jump <tty> <win_id> <session>` with <tty> exactly
#              as `tmux list-clients -F '#{client_tty}'` prints it and <win_id>
#              / <session> from the chosen list row; then it activates WezTerm.
#              On 3 it re-lists once and tries the next row; on 4 it skips the
#              row; 3 and 4 also show the usual chip on that client (never on
#              another one).
#   - back:    ⌥X (`back`) still works after CuaNotch's jumps, because `jump`
#              records THE CHAIN exactly as `next` does, two jumps racing
#              each other included.
#
# ORDER. failed (red) > needs-input (yellow) > done (green), then the oldest
# @agent_since first, then session name and window index so equal stamps
# (the watcher stamps at 1 s resolution) still sort the same way every time.
# `done` while @agent_workflow is set is NOT in the queue: the tab renders
# untinted because a fleet is still out (see tmux.conf.tmpl), so it doesn't
# want you yet. Nor is a paused agent: `paused` is in no tier, and `done`
# with @agent_paused set (the pause key's flag, before the tab hook writes
# `paused`) is left out the same way (docs/agent-pause.md "The paused
# state"; agent-ui and agent-roster.py agree). A window with no stamp sorts
# last within its tier.
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
# latest activity (often stash). `next` uses the session from its list row,
# and so does `goto` when the caller names one (the roster passes the session
# of the row that was clicked); back and a bare goto prefer the client's own
# session, then the first linked session that is not excluded. A named session
# must still hold the window, and is refused like any other if it is an
# EXCLUDE session. The =<session>:<win> target keeps select and switch
# in the same session. When the select CHANGES the session's window, the
# hook's #{window_id} is the command's target, so it discharges the right
# window even before the client arrives. When the target is already that
# session's current window the select is a no-op and the hook expands
# against the client's old session instead; client-session-changed[0] (the
# same clear-current) covers that case, and client-session-changed[1] then
# runs cua-notch-visit for the notch. The client is checked BEFORE the select:
# a select whose switch then fails would discharge a tint nobody saw.
#
# THE CHAIN. `back` undoes a run of `next`s (and `jump`s: both are one
# step(), the only code that writes the chain). tmux has no client-scoped user
# options, so the chain lives in a global keyed by the client's tty:
#   @agent_jump_<tty> = "<origin window> <window we last landed on>"
# It resets lazily, with no hook of its own: a step keeps the origin only
# while the client is still on the window it last landed on (any other
# navigation in between starts a fresh chain from where you are), and `back`
# acts only from that landing, and only if the origin still exists. A `jump`
# onto the client's current window is no step at all (as `next` never picks
# that window): it leaves the chain alone.
# The chain is written by the move's OWN tmux command list (select ; switch ;
# set-option), which the server runs without interleaving another client's
# commands. Two jumps fired at once (⌥S twice, CuaNotch and a key) both start
# from the same origin, and whichever list runs last sets both where the
# client is and the landing the chain names, so ⌥X still goes home. A
# select-switch-then-record in separate calls raced (13 of 40); a `wait-for
# -L` lock would wedge every later jump if its holder died.
#
# SILENT SUCCESS. A `next`, `jump` or `back` that lands says nothing. tmux stops
# redrawing the status line while a display-message is up (display-time,
# 4 s here), so a "→ main:5 …" chip kept the tab you landed on painted
# green that long even though clear-current had already discharged it. The
# landing is the feedback; the sidebar shows what is still queued. Only the
# misses below (nothing needs you, gone, refused) speak.
#
# Messages are worded to stay info chips: message-format paints anything
# starting "no …"/"not …"/"can't …" as an error.
#
# display-message takes its text as a FORMAT, so say() doubles every `#`:
# a summary, window name or session name holding #{…}/#[…] is shown, not
# expanded or styled. Nothing here uses formats in its own messages.

set -uo pipefail

# A UTF-8 locale, always. Outside tmux ($TMUX unset: CuaNotch, launchd, cron)
# a tmux client whose locale is not UTF-8 rewrites control bytes in format
# output as `_` (the US separator included), so `list` would print nothing,
# read as "nothing needs you", and the client lookup would never match
# (`jump` → 5). agent-ui's Tmux::command passes -u for the same reason;
# exporting the locale also covers the awk/sort below.
case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
    *[Uu][Tt][Ff]-8* | *[Uu][Tt][Ff]8*) ;;
    *) unset LC_ALL; export LC_CTYPE=en_US.UTF-8 ;;
esac

US=$'\x1f'
EXCLUDE=" agents tasks stash scratch btop-popup "

mode="${1:-}"
tty="${2:-}"

# Messages go through notify.sh: a toast in the agent sidebar when one is open
# for the client, else tmux's own display-message (its header has the rest).
# NOTIFY_SH names another (tests); beside this script it is notify.sh
# (executable_notify.sh in the chezmoi source); with none, display-message.
#   notify_user [-c CLIENT] [-k info|ok|warn|error] [-t SECONDS] [--] TEXT
notify_user() {
    local n="${NOTIFY_SH:-}" d="${BASH_SOURCE[0]%/*}" c=
    [ "$d" = "${BASH_SOURCE[0]}" ] && d=.
    if [ -z "$n" ]; then
        for n in "$d/notify.sh" "$d/executable_notify.sh" "$HOME/.config/tmux/scripts/notify.sh"; do
            [ -f "$n" ] && break
        done
    fi
    if [ -x "$n" ]; then "$n" "$@"
    elif [ -f "$n" ]; then zsh -f "$n" "$@"
    else
        while [ $# -gt 0 ]; do
            case "$1" in
                -c|-k|-t) [ "$1" = -c ] && c="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
                --) shift; break ;;
                *) break ;;
            esac
        done
        ${NOTIFY_TMUX:-tmux} display-message ${c:+-c "$c"} -- "$*" 2>/dev/null
    fi
    return 0
}

# say [-k KIND] TEXT: on <tty> while it is attached (tty_ok, from
# client_info). A <tty> that is not attached gets the untargeted fallback
# instead, unless say_strict (set by `jump`): that fallback would land on
# whichever client tmux picks, which for an outside caller (CuaNotch) is
# nobody in particular; a client that is gone hears nothing. (notify.sh never
# fails, so whether <tty> is attached is asked up front, not read off a
# failed display-message as before.)
say_strict=
tty_ok=
say() {
    local k=info
    [ "${1:-}" = -k ] && { k="$2"; shift 2; }
    local m="${1//\#/##}"
    if [ -n "$tty" ]; then
        [ -n "$tty_ok" ] || client_info || :
        if [ "$tty_ok" = 1 ]; then
            notify_user -c "$tty" -k "$k" -- "$m"
        elif [ -z "$say_strict" ]; then
            notify_user -k "$k" -- "$m"
        fi
    else
        notify_user -k "$k" -- "$m"
    fi
}

list_needs() {
    tmux list-windows -a -F "#{window_id}${US}#{session_name}${US}#{window_index}${US}#{@agent_state}${US}#{@agent_workflow}${US}#{@agent_since}${US}#{@agent_paused}${US}#{?#{n:#{@agent_summary}},#{@agent_summary},#{window_name}}" 2>/dev/null |
    awk -F "$US" -v ex="$EXCLUDE" '
        index(ex, " " $2 " ") { next }
        {
            if ($4 == "failed") p = 0
            else if ($4 == "needs-input") p = 1
            else if ($4 == "done" && $5 == "" && $7 == "") p = 2
            else next
            split($6, a, " ")
            t = (a[1] ~ /^[0-9]+$/) ? a[1] : 9999999999
            label = $8; gsub(/\t/, " ", label)
            printf "%d\t%s\t%s\t%09d\t%s\t%s\t%s\t%s\t%s\t%s\n", p, t, $2, $3, $1, $2, $3, $4, t, label
        }' |
    sort -t "$(printf '\t')" -k1,1n -k2,2n -k3,3 -k4,4 |
    # `list-windows -a` prints a linked window once per session it is linked
    # into; keep its first (sorted) row so it is queued and counted once.
    # Excluded sessions were dropped above, so that row is never one of them.
    awk -F '\t' '!seen[$5]++' |
    cut -f5-
}

# Sets cur_sess / cur_win for $tty, and tty_ok (1 attached, else 0). Fails if
# that client is not attached.
client_info() {
    local line
    tty_ok=0
    [ -n "$tty" ] || return 1
    line=$(tmux list-clients -F "#{client_tty}${US}#{session_name}${US}#{window_id}" 2>/dev/null |
           awk -F "$US" -v t="$tty" '$1 == t { print; exit }')
    [ -n "$line" ] || return 1
    tty_ok=1
    IFS="$US" read -r _ cur_sess cur_win <<<"$line"
}

# go_to <win> [<session>] [<chain>]. Sets go_sess to the session it moves in:
# the one given, else the client's own session if <win> is linked there, else
# the first linked session not in EXCLUDE, else (all excluded) the first one.
# Returns 1 if the window is gone (or the move failed), 2 if go_sess is an
# EXCLUDE session (e.g. the origin was parked into stash mid-chain): never
# switch the client in there — refused() explains instead. <chain>, when
# given, is stored as the client's chain IN THE SAME COMMAND LIST as the move
# (see THE CHAIN: why that, not a lock).
go_to() {
    local win="$1" chain="${3:-}"
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
    # A pending claude (space.sh's pending spaces: a claude a picker's
    # sentence started, waiting in the hidden session `pending`) is listed and
    # jumped to like any agent, but never by switching the client into
    # `pending`: its popup opens instead (run-shell -b, as display-popup waits
    # for the popup to close). No chain: nothing to come back from.
    if [ "$go_sess" = pending ]; then
        tmux run-shell -b "#{@space_sh} pending-open $win '$tty'" 2>/dev/null
        return
    fi
    if [ -n "$chain" ]; then
        tmux select-window -t "=$go_sess:$win" \; switch-client -c "$tty" -t "=$go_sess" \
            \; set-option -g "$(chain_key)" "$chain" 2>/dev/null
    else
        tmux select-window -t "=$go_sess:$win" \; switch-client -c "$tty" -t "=$go_sess" 2>/dev/null
    fi
}

refused() {
    if [ "$go_sess" = stash ]; then
        say -k warn "that tab was parked · prefix h brings it back"
    else
        say -k warn "that tab is in $go_sess · jumps skip that session"
    fi
}

chain_key() { printf '@agent_jump_%s' "${tty//[^A-Za-z0-9]/_}"; }

# step <win> <session> <where>: ONE jump onto <win> in <session> — what `next`
# does with its pick and `jump` with the caller's. client_info must have run
# (cur_win is the window the client is on). Keeps the chain's origin while
# the client is still on the chain's last landing, else starts a fresh one
# from cur_win; records "<origin> <win>" in the move's own command list, so
# only a move that landed records. <where> names the window in the "gone"
# chip. Returns 0 landed (silent), 3 gone, 4 refused, 5 the client detached
# during the move (silent: nobody to tell).
step() {
    local win="$1" sess="$2" where="$3" chain origin rc
    chain=$(tmux show-options -gqv "$(chain_key)" 2>/dev/null)
    origin="${chain%% *}"
    if [ -z "$chain" ] || [ "${chain##* }" != "$cur_win" ]; then
        origin="$cur_win"
    fi
    go_to "$win" "$sess" "$origin $win"; rc=$?
    [ "$rc" -eq 2 ] && { refused; return 4; }
    if [ "$rc" -ne 0 ]; then
        client_info || return 5                # the switch failed because the client left
        say -k warn "agent-jump: $where is gone"
        return 3
    fi
    return 0                                   # landed: says nothing (SILENT SUCCESS)
}

case "$mode" in
    list)
        list_needs
        ;;

    next)
        client_info || { say -k warn "agent-jump: client $tty is not attached"; exit 0; }
        queue=$(list_needs | awk -F '\t' -v c="$cur_win" '$1 != c')
        if [ -z "$queue" ]; then
            say "nothing needs you"
            exit 0
        fi
        IFS=$'\t' read -r twin tsess tidx _ <<<"${queue%%$'\n'*}"
        step "$twin" "$tsess" "$tsess:$tidx"
        exit 0                                 # run-shell: never a nonzero exit
        ;;

    jump)
        # CuaNotch's landing (see CUANOTCH CONTRACT): exit codes, not just chips.
        win="${3:-}"
        sess="${4:-}"
        if [ -z "$tty" ] || [ -z "$win" ] || [ -z "$sess" ]; then
            echo "usage: agent-jump.sh jump <client_tty> <window_id> <session>" >&2
            exit 2
        fi
        say_strict=1
        client_info || exit 5                  # no client: nobody to tell
        if [ "$win" = "$cur_win" ]; then
            # Already there (CuaNotch picked the client's own window because
            # WezTerm wasn't frontmost): landed, chain untouched. No select
            # runs, so no focus hook fires: discharge the tint the way the
            # after-select-window[0] / client-session-changed[0] hooks do.
            tmux run-shell -b "#{HOME}/.config/tmux/scripts/agent-tab-indicator.sh clear-current $win" 2>/dev/null
            exit 0
        fi
        # Where is <win> now? "held <index>" if <session> still holds it, else
        # "parked <excluded session>" when every session holding it is an
        # EXCLUDE one (parked into stash since the caller listed it), else
        # "left" (it lives elsewhere now) or nothing (gone).
        where=$(tmux list-windows -a -F "#{window_id}${US}#{session_name}${US}#{window_index}" 2>/dev/null |
            JUMP_SESS="$sess" awk -F "$US" -v w="$win" -v ex="$EXCLUDE" '
                $1 != w { next }
                $2 == ENVIRON["JUMP_SESS"] { held = $3 }
                index(ex, " " $2 " ") { if (exs == "" || $2 == "stash") exs = $2; next }
                { other = 1 }
                END {
                    if (held != "") print "held " held
                    else if (other) print "left"
                    else if (exs != "") print "parked " exs
                }')
        case "$where" in
            held\ *)
                step "$win" "$sess" "$sess:${where#held }"
                exit $?                        # 0 landed, 3 gone, 4 refused, 5 detached
                ;;
            parked\ *)
                go_sess="${where#parked }"
                refused
                exit 4
                ;;
            left)
                say -k warn "agent-jump: $win has left $sess · pick it again"
                exit 3
                ;;
            *)
                say -k warn "agent-jump: window $win is gone"
                exit 3
                ;;
        esac
        ;;

    back)
        client_info || { say -k warn "agent-jump: client $tty is not attached"; exit 0; }
        key=$(chain_key)
        chain=$(tmux show-options -gqv "$key" 2>/dev/null)
        if [ -z "$chain" ] || [ "${chain##* }" != "$cur_win" ]; then
            say -k warn "nothing to go back to"
            exit 0
        fi
        origin="${chain%% *}"
        tmux set-option -gu "$key" 2>/dev/null          # chain is spent either way
        go_to "$origin"; rc=$?
        [ "$rc" -eq 2 ] && { refused; exit 0; }
        [ "$rc" -eq 0 ] || { say -k warn "the window you jumped from is gone"; exit 0; }
        ;;

    goto)
        # The roster sends parked windows through `stash.sh unstash`, not
        # here; this guard only keeps a stray goto out of EXCLUDE sessions.
        win="${3:-}"
        sess="${4:-}"
        [ -n "$win" ] || { say -k error "agent-jump: goto needs a window"; exit 0; }
        client_info || { say -k warn "agent-jump: client $tty is not attached"; exit 0; }
        if [ -n "$sess" ] && ! tmux list-windows -a -F "#{window_id}${US}#{session_name}" 2>/dev/null |
                GOTO_SESS="$sess" awk -F "$US" -v w="$win" '$1 == w && $2 == ENVIRON["GOTO_SESS"] { f = 1 } END { exit !f }'; then
            say -k warn "agent-jump: $win has left $sess · pick it again"
            exit 0
        fi
        go_to "$win" "$sess"; rc=$?
        if [ "$rc" -eq 2 ]; then refused
        elif [ "$rc" -ne 0 ]; then say -k warn "agent-jump: window $win is gone"
        fi
        ;;

    *)
        echo "usage: agent-jump.sh next|back <client_tty> | goto <client_tty> <window> [<session>] | jump <client_tty> <window_id> <session> | list" >&2
        exit 2
        ;;
esac
