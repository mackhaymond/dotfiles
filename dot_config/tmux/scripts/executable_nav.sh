#!/usr/bin/env bash
# nav.sh: browser-style back/forward through the tabs a client has been on.
# Option-X goes back, Option-C goes forward (docs/nav.md; tests/test_nav.py).
#
#   nav.sh record client <client_name>    a hook: this client may have moved
#   nav.sh record session <session_id>    a hook: every client on this session
#   nav.sh attach <client_name>           fresh history (a tty can be reused)
#   nav.sh forget <client_name>           the client detached: drop its history
#   nav.sh back|forward <client_name>     the keys
#   nav.sh show <client_name>             debugging: the history, one per line
#
# HISTORY, per client. tmux has no client-scoped user options, so it lives in
# two globals keyed by the client name (non-alphanumerics -> _):
#   @nav_<key>     = "<cur>|<back...>|<forward...>"
#   @nav_seq_<key> = a counter bumped by every write
# An entry is "<session_id>:<window_id>" ($3:@12). <back> is oldest first and
# pops from its end; so does <forward>. A navigation pushes <cur> onto <back>
# (unless back's last entry is the same window: consecutive duplicates
# collapse), caps <back> at NAV_CAP (50), clears <forward>, and becomes <cur>.
#
# RECORDING. The hooks (session-window-changed, client-session-changed) fire
# for every focus change, whatever caused it: keys, clicks, the sidebar,
# agent-jump, menus, switch-client from a script. They pass only WHO may have
# moved; the script reads where the client is NOW, so a stale or reordered
# hook (run-shell -b gives no ordering) can only ever record the truth, and a
# burst of moves records where it ended.
#
# BACK/FORWARD ARE NOT NAVIGATIONS. back writes the new history (cur = the
# target) in the SAME tmux command list as the move, so when the move's own
# hooks run the record step finds the client already on <cur> and does
# nothing. That is the per-client marker: <cur> itself, set atomically with
# the move (a separate marker option written before or after the move would
# race the hook, as agent-jump.sh's select-switch-then-record did).
#
# NO LOCK, A COMPARE-AND-SET. Every write is
#   if -F "#{==:#{@nav_seq_<key>},<seq we read>}" { [move ;] write ; seq+1 }
# so a writer that read stale history (a hook racing a key) changes nothing
# and retries from a fresh read. tmux runs one command list at a time, so
# check-and-write is atomic; nothing can be left locked by a dead process.
#
# back/forward first catch up with a move whose hook has not run yet (moved,
# then pressed Option-X at once), then pop. They skip, and drop, entries whose
# window is gone or now only lives in a hidden session (@space_hidden 1, or
# named in @space_hidden_sessions: agents tasks stash scratch btop-popup; e.g.
# a tab parked into stash). A window moved to another visible session is
# followed there. Never into a hidden session; across spaces is fine
# (select-window, then switch-client: the order agent-jump.sh uses so the
# after-select-window tint discharge fires). A client sitting in a hidden
# session (a popup client, say) records nothing; back from there returns to
# <cur>. The sidebar's focus notification is already posted by the
# session-window-changed[50] / client-session-changed[50] hooks the move fires.

set -o pipefail

# A UTF-8 locale, always (see agent-jump.sh): otherwise a tmux client run
# outside tmux rewrites the US separator below as `_`.
case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
    *[Uu][Tt][Ff]-8* | *[Uu][Tt][Ff]8*) ;;
    *) unset LC_ALL; export LC_CTYPE=en_US.UTF-8 ;;
esac

US=$'\x1f'
NL=$'\n'
CAP="${NAV_CAP:-50}"
HIDDEN='#{?#{||:#{@space_hidden},#{m:* #{session_name} *, #{@space_hidden_sessions} }},1,0}'
TRIES=5

mode="${1:-}"

usage() {
    echo "usage: nav.sh record client <client> | record session <session_id> | attach|forget|back|forward|show <client>" >&2
    exit 2
}

# display-message takes a format: double every # so nothing expands.
say() { tmux display-message -c "$client" "${1//\#/##}" 2>/dev/null; }

win_of() { printf '%s' "${1#*:}"; }

# load [windows]: ONE tmux call. Sets act (the client's "<session>:<window>"
# now), act_hidden, seq, state, cur, back[], fwd[]; with `windows`, also
# ROWS: "\n<session>:<window> <hidden>\n..." for every linked window.
# Fails if the client is not attached.
load() {
    local args out tag a b c
    args=(list-clients -F "C${US}#{client_name}${US}#{session_id}:#{window_id}${US}${HIDDEN}"
          \; display-message -p "S${US}#{@nav_seq_${K}}${US}#{@nav_${K}}")
    [ "${1:-}" = windows ] &&
        args+=(\; list-windows -a -F "W${US}#{session_id}:#{window_id} ${HIDDEN}")
    out=$(tmux "${args[@]}" 2>/dev/null) || return 1
    act= act_hidden=1 seq= state= ROWS=$NL
    while IFS="$US" read -r tag a b c; do
        case "$tag" in
            C) [ "$a" = "$client" ] && { act=$b; act_hidden=$c; } ;;
            S) seq=$a; state=$b ;;
            W) ROWS+="$a$NL" ;;
        esac
    done <<<"$out"
    [ -n "$act" ] || return 1
    local bs fs
    IFS='|' read -r cur bs fs <<<"$state"
    back=() fwd=()
    read -ra back <<<"$bs"
    read -ra fwd <<<"$fs"
    return 0
}

# commit [moves]: compare-and-set the history, running <moves> (a tmux command
# list ending in " ; ") first, in the same list. 0 written, 1 lost the race or
# the move failed (re-read and try again).
commit() {
    local IFS=' ' new next r
    new="$cur|${back[*]}|${fwd[*]}"
    case "$seq" in '' | *[!0-9]*) next=1 ;; *) next=$((seq + 1)) ;; esac
    r=$(tmux if-shell -F "#{==:#{@nav_seq_${K}},${seq}}" \
        "${1:-}set-option -g @nav_${K} '${new}' ; set-option -g @nav_seq_${K} ${next}" \
        "display-message -p NAV-CAS-FAIL" 2>&1) || return 1
    case "$r" in *NAV-CAS-FAIL*) return 1 ;; esac
    return 0
}

# push_back <entry>: onto back's end, collapsing a repeat of its last window,
# capped at CAP (oldest dropped). Same for push_fwd.
push_back() {
    [ -n "$1" ] || return 0
    local n=${#back[@]}
    if [ "$n" -gt 0 ] && [ "$(win_of "${back[n-1]}")" = "$(win_of "$1")" ]; then return 0; fi
    back+=("$1")
    n=${#back[@]}
    [ "$n" -gt "$CAP" ] && back=("${back[@]:n-CAP}")
    return 0
}
push_fwd() {
    [ -n "$1" ] || return 0
    local n=${#fwd[@]}
    if [ "$n" -gt 0 ] && [ "$(win_of "${fwd[n-1]}")" = "$(win_of "$1")" ]; then return 0; fi
    fwd+=("$1")
    n=${#fwd[@]}
    [ "$n" -gt "$CAP" ] && fwd=("${fwd[@]:n-CAP}")
    return 0
}

# catch_up: fold where the client is now into the history. 0 = changed.
catch_up() {
    [ "$act_hidden" = 0 ] || return 1
    [ "$act" = "$cur" ] && return 1
    if [ "$(win_of "$act")" != "$(win_of "$cur")" ]; then
        push_back "$cur"
        fwd=()
    fi
    cur=$act                           # same window seen via another session: no step
    return 0
}

# resolve <entry>: sets go to where <entry> can be reached now: its own
# session if that still holds the window and is not hidden, else the
# client's session if it holds it, else the first visible session that does.
# Fails if the window is gone or only lives in hidden sessions. Needs ROWS.
resolve() {
    local w s row
    w=$(win_of "$1")
    case "$ROWS" in *"$NL$1 0$NL"*) go=$1; return 0 ;; esac
    s=${act%%:*}
    case "$ROWS" in *"$NL$s:$w 0$NL"*) go="$s:$w"; return 0 ;; esac
    while IFS= read -r row; do
        case "$row" in *":$w 0") go=${row% 0}; return 0 ;; esac
    done <<<"$ROWS"
    return 1
}

record_one() {
    local i
    for ((i = 0; i < TRIES; i++)); do
        load || return 0
        catch_up || return 0
        commit && return 0
    done
}

move() {                               # move back|forward
    local dir=$1 i e n found
    for ((i = 0; i < TRIES; i++)); do
        load windows || return 0
        if [ "$act_hidden" = 1 ]; then
            # In a hidden session: back (or forward) means "out of here".
            if [ -n "$cur" ] && resolve "$cur"; then
                cur=$go
                commit "select-window -t '$go' ; switch-client -c '$client' -t '${go%%:*}' ; " && return 0
                continue
            fi
        else
            catch_up
        fi
        found=
        while :; do
            if [ "$dir" = back ]; then n=${#back[@]}; else n=${#fwd[@]}; fi
            [ "$n" -gt 0 ] || break
            if [ "$dir" = back ]; then
                e=${back[n-1]}; back=("${back[@]:0:n-1}")
            else
                e=${fwd[n-1]}; fwd=("${fwd[@]:0:n-1}")
            fi
            [ "$(win_of "$e")" = "$(win_of "$cur")" ] && continue
            resolve "$e" && { found=1; break; }
        done
        if [ -z "$found" ]; then
            commit                     # keep the catch-up and the dropped entries
            if [ "$dir" = back ]; then say "nothing to go back to"; else say "nothing to go forward to"; fi
            return 0
        fi
        if [ "$dir" = back ]; then push_fwd "$cur"; else push_back "$cur"; fi
        cur=$go
        commit "select-window -t '$go' ; switch-client -c '$client' -t '${go%%:*}' ; " && return 0
    done
}

case "$mode" in
    record)
        kind="${2:-}" target="${3:-}"
        [ -n "$target" ] || usage
        case "$kind" in
            client)
                client=$target K=${client//[^A-Za-z0-9]/_}
                record_one
                ;;
            session)
                tmux list-clients -F "#{client_name}${US}#{session_id}" 2>/dev/null |
                while IFS="$US" read -r c s; do
                    [ "$s" = "$target" ] || continue
                    client=$c K=${client//[^A-Za-z0-9]/_}
                    record_one
                done
                ;;
            *) usage ;;
        esac
        ;;
    attach)
        client="${2:-}"; [ -n "$client" ] || usage
        K=${client//[^A-Za-z0-9]/_}
        for ((i = 0; i < TRIES; i++)); do
            load || break
            back=() fwd=() cur=
            [ "$act_hidden" = 0 ] && cur=$act
            commit && break
        done
        ;;
    forget)
        client="${2:-}"; [ -n "$client" ] || usage
        K=${client//[^A-Za-z0-9]/_}
        tmux set-option -gu "@nav_${K}" \; set-option -gu "@nav_seq_${K}" 2>/dev/null
        ;;
    back | forward)
        client="${2:-}"; [ -n "$client" ] || usage
        K=${client//[^A-Za-z0-9]/_}
        move "$mode"
        ;;
    show)
        client="${2:-}"; [ -n "$client" ] || usage
        K=${client//[^A-Za-z0-9]/_}
        load || { echo "nav: $client is not attached" >&2; exit 1; }
        printf 'cur %s\n' "$cur"
        for e in "${back[@]}"; do printf 'back %s\n' "$e"; done
        for e in "${fwd[@]}"; do printf 'fwd %s\n' "$e"; done
        ;;
    *) usage ;;
esac
exit 0
