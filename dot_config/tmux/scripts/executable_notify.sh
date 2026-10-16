#!/bin/zsh -f
# notify — tell the user something without bringing back the tmux status line.
#
# While the agent-ui sidebar is open the status line is off (`status off`),
# and a plain `tmux display-message` then draws on the client's bottom line,
# which reads as the status line coming back. So messages go through here:
# when a sidebar is open for the client, the message becomes a TOAST in the
# sidebar (a card stacked over its top, nvim-notify style, see agent-ui
# src/sidebar/layout.rs "Toasts"); otherwise it is exactly the old
# `tmux display-message [-c CLIENT] -- TEXT`, so with no sidebar nothing
# changes.
#
#   notify.sh [-c CLIENT] [-k info|ok|warn|error] [-t SECONDS] [-T TITLE] [--] TEXT...
#
#   -c CLIENT  a client as `display-message -c` takes it. Without it: the
#              calling context's client as tmux resolves it (`#{client_name}`;
#              in a binding's run-shell, the client that pressed it), else
#              none, which toasts in every open sidebar. `-c '*'`: EVERY
#              client — one toast in every live sidebar, and tmux's own
#              message on each client without one (one toast for all, so a
#              per-client loop would not post the same card n times).
#   -k KIND    info (default), ok, warn, error: the card's icon, colour and
#              default title (Info, Done, Warning, Error).
#   -t SECONDS how long the toast shows (default 4; error 8). Given (and
#              valid), the display-message fallback shows that long too
#              (`-d`); without it, display-time as before.
#   -T TITLE   the card's title instead of the kind's (taken literally: no
#              tmux formats). The display-message fallback shows the text
#              alone, as before.
#   TEXT       the words, joined by spaces; tabs, newlines and the ASCII
#              separators RS / US become spaces. tmux formats in it are
#              expanded as display-message would. Empty: nothing is shown.
#
# Always exits 0 and prints nothing.
#
# The toast transport (GLOBAL tmux options, set in ONE call):
#   @toast_seq  an integer, one more per toast (read, then written: two
#               racing notifies can share one; a toast is (seq, epoch_ms))
#   @toast      the recent toasts, oldest first, separated by RS (U+001E):
#               at most 4 records, each
#                 seq TAB epoch_ms TAB kind TAB ttl_s TAB client TAB text TAB title
#               (client "" = every sidebar; title "" = the kind's; ttl_s
#               capped at 3600, as the sidebar caps it). Each post drops the
#               expired records (epoch_ms + ttl_s*1000 <= now, ttl capped the
#               same way), those stamped more than 60 s in the future (a
#               clock jump) and malformed ones, and keeps the newest 3 beside
#               the new one; the sidebar shows the live ones, at most 3 at
#               once. (Records without the title field, from before it,
#               still parse.) Two notifies racing can lose one toast: both
#               rewrite @toast from the same read, and the later write wins.
#               Where it is drawn: cards over the top of a sidebar of 16+
#               columns and 4+ free rows; smaller, the newest as one compact
#               row, in any sidebar of 2+ rows (the free rows are those under
#               the key-table marks and the selection hint, which stay on
#               top).
# then the sidebar's focus notice is posted (notifyutil, in the background)
# so open sidebars redraw at once. Which clients have a sidebar is
# `@sidebar_clients` (kept by the sidebar word for word beside
# `@sidebar_pids`); a listed sidebar counts only while its pid lives. Every
# message is also logged: epoch TAB kind TAB client TAB text (TAB title when
# -T gave one), in ~/.local/state/tmux-notify/log, trimmed to its last 500
# lines past 1000.
#
# Cost: two tmux calls (one read: client, sidebars, seq, the current toasts
# and the expanded text together; one write or the display-message).
#
# Environment (tests):
#   NOTIFY_TMUX          the tmux command words (default `tmux`), e.g.
#                        "tmux -L test -f /dev/null" to target a private server
#   NOTIFY_LOG           the log file
#   AGENT_FOCUS_NOTICE   the notice name (the sidebar honours it too); set
#                        but empty: post nothing

zmodload zsh/datetime 2>/dev/null

client= kind=info ttl= title=
while (( $# )); do
    case $1 in
        -c|-k|-t|-T)
            if (( $# < 2 )); then shift; break; fi
            case $1 in
                -c) client=$2 ;;
                -k) kind=$2 ;;
                -t) ttl=$2 ;;
                -T) title=$2 ;;
            esac
            shift 2 ;;
        --) shift; break ;;
        *) break ;;
    esac
done

case $kind in
    info|ok|warn|error) ;;
    *) kind=info ;;
esac
dopt=()
if [[ ! $ttl =~ '^[0-9]{1,9}([.][0-9]{1,6})?$' ]] || (( ttl <= 0 )); then
    if [[ $kind == error ]]; then ttl=8; else ttl=4; fi
else
    # An explicit duration holds for tmux's own message too (milliseconds).
    integer dms=$(( ttl * 1000 ))
    (( dms > 0 )) && dopt=(-d $dms)
fi

# One line: tabs, CRs, newlines and the record/field separators → spaces.
RS=$'\x1e' US=$'\x1f'
text="$*"
text=${text//[$'\t\r\n'$RS$US]/ }
[[ -z ${text//[[:space:]]/} ]] && exit 0
title=${title//[$'\t\r\n'$RS$US]/ }
[[ -z ${title//[[:space:]]/} ]] && title=

# Split like a shell would (a quoted or escaped socket path keeps its spaces).
tmux_cmd=(${(Q)${(z)${NOTIFY_TMUX:-tmux}}})
copt=() topt=() all=0
if [[ $client == '*' ]]; then
    all=1 client=
elif [[ -n $client ]]; then
    copt=(-c "$client")
    # -t too: display -p's formats follow -t's session (with only -c they
    # name the best client of the current session). A client tmux does not
    # know still exits 0, with every field empty: no name, so no toast.
    topt=(-c "$client" -t "$client")
fi

# The one read: the client's name as tmux knows it, the open sidebars' pids
# and their clients (word for word), the last seq, the current toasts (any
# US in them made a space, so the fields stay apart) and the text expanded
# as display-message would expand it, unit-separated (the text last, so it
# may hold anything).
out=$("${tmux_cmd[@]}" display-message -p "${topt[@]}" -- \
    "#{client_name}$US#{@sidebar_pids}$US#{@sidebar_clients}$US#{@toast_seq}$US#{s/$US/ /:@toast}$US$text" 2>/dev/null)
read_ok=$?

target= pids= clients= seq=0 old= shown=$text
if (( read_ok == 0 )) && [[ $out == *$US*$US*$US*$US*$US* ]]; then
    target=${out%%$US*}; out=${out#*$US}
    pids=${out%%$US*}; out=${out#*$US}
    clients=${out%%$US*}; out=${out#*$US}
    seq=${out%%$US*}; out=${out#*$US}
    old=${out%%$US*}; shown=${out#*$US}
    # (A format can expand to any of the separators: one line, one field.)
    shown=${shown//[$'\t\r\n'$RS$US]/ }
    (( all )) && target=

    [[ $seq == <-> ]] || seq=0
    # Formats that expand to nothing leave nothing to say.
    [[ -z ${shown//[[:space:]]/} ]] && exit 0
fi

# Only a LIVE sidebar counts: a SIGKILLed one stays listed until something
# repairs the registry, and a toast for it would be swallowed. Word i of
# @sidebar_clients belongs to pid i of @sidebar_pids; `kill -0` is a builtin.
toast=0 live=()
if (( read_ok == 0 )); then
    pid_list=(${=pids}) client_list=(${=clients})
    for (( i = 1; i <= $#pid_list; i++ )); do
        p=${pid_list[i]}
        [[ $p == <1-> ]] && kill -0 "$p" 2>/dev/null || continue
        if (( all )); then
            # Every client: note each live sidebar's client, toast once.
            toast=1; live+=("${client_list[i]-}")
        elif [[ -n $target ]]; then
            # A sidebar for exactly this client.
            [[ ${client_list[i]-} == "$target" ]] && { toast=1; break; }
        elif [[ -z $client ]]; then
            # No client to name: every open sidebar, if any lives. (A -c
            # client tmux does not know resolves to no name too, and never
            # toasts.)
            toast=1; break
        fi
    done
fi

if (( toast )); then
    seq=$(( seq + 1 ))
    integer ms=$(( EPOCHREALTIME * 1000 ))
    # The stack: the records still live (well-formed ones only, ttl capped
    # at 3600 as the sidebar caps it, none stamped over 60 s ahead), the
    # newest 3 of them, then this one (its ttl capped the same way; tmux's
    # own message, with no sidebar, still takes -t as given).
    recs=()
    for r in "${(@ps:$RS:)old}"; do
        f=("${(@ps:\t:)r}")
        (( ${#f} >= 6 )) && [[ ${f[1]} == <-> && ${f[2]} == <-> ]] || continue
        # Bounded digits: zsh arithmetic warns on stderr past 19.
        (( ${#f[1]} <= 18 && ${#f[2]} <= 16 )) || continue
        (( f[2] > ms + 60000 )) && continue
        t=${f[4]}
        [[ $t =~ '^[0-9]{1,9}([.][0-9]{1,6})?$' ]] && (( t > 0 )) || t=4
        (( t > 3600 )) && t=3600
        (( f[2] + t * 1000 > ms )) && recs+=("$r")
    done
    (( ${#recs} > 3 )) && recs=("${(@)recs[-3,-1]}")
    tttl=$ttl
    (( tttl > 3600 )) && tttl=3600
    recs+=("$seq"$'\t'"$ms"$'\t'"$kind"$'\t'"$tttl"$'\t'"$target"$'\t'"$shown"$'\t'"$title")
    payload=${(pj:$RS:)recs}
    # tmux reads an argument ending in `;` as a command separator (the `;`
    # dropped) and one ending in `\;` as a literal `;`: keep the value's own
    # (only its very end matters, the newest record's title or text).
    [[ $payload == *';' ]] && payload="${payload%;}\\;"
    "${tmux_cmd[@]}" set-option -gq @toast_seq "$seq" \; \
        set-option -gq @toast "$payload" >/dev/null 2>&1
    notice=${AGENT_FOCUS_NOTICE-com.mackhaymond.tmux.focus}
    if [[ -n $notice ]]; then
        notifyutil -p "$notice" </dev/null >/dev/null 2>&1 &!
    fi
fi
if (( all )); then
    # Every client without a live sidebar: tmux's own message, on it.
    for c in ${(f)"$("${tmux_cmd[@]}" list-clients -F '#{client_name}' 2>/dev/null)"}; do
        (( ${live[(Ie)$c]} )) && continue
        "${tmux_cmd[@]}" display-message -c "$c" "${dopt[@]}" -- "$text" </dev/null >/dev/null 2>&1
    done
elif (( ! toast )); then
    # No sidebar for it: tmux's own message, exactly as before (with -d when
    # the caller gave -t, as its old display-message -d did).
    "${tmux_cmd[@]}" display-message "${copt[@]}" "${dopt[@]}" -- "$text" </dev/null >/dev/null 2>&1
fi

# The log, bounded.
log=${NOTIFY_LOG:-${XDG_STATE_HOME:-$HOME/.local/state}/tmux-notify/log}
{
    [[ -d ${log:h} ]] || mkdir -p -- "${log:h}"
    tab=$'\t'
    print -r -- "${EPOCHSECONDS:-$(date +%s)}$tab$kind$tab$target$tab$shown${title:+$tab$title}" >>"$log"
    integer n=$(wc -l <"$log")
    if (( n > 1000 )); then
        tail -n 500 -- "$log" >"$log.$$" && mv -f -- "$log.$$" "$log"
    fi
} 2>/dev/null
exit 0
