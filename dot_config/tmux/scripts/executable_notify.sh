#!/bin/zsh -f
# notify — tell the user something without bringing back the tmux status line.
#
# While the agent-ui sidebar is open the status line is off (`status off`),
# and a plain `tmux display-message` then draws on the client's bottom line,
# which reads as the status line coming back. So messages go through here:
# when a sidebar is open for the client, the message becomes a TOAST in the
# sidebar (drawn in its usage card's bottom border, see agent-ui
# src/sidebar/layout.rs "Toasts"); otherwise it is exactly the old
# `tmux display-message [-c CLIENT] -- TEXT`, so with no sidebar nothing
# changes.
#
#   notify.sh [-c CLIENT] [-k info|ok|warn|error] [-t SECONDS] [--] TEXT...
#
#   -c CLIENT  a client as `display-message -c` takes it. Without it: the
#              calling context's client as tmux resolves it (`#{client_name}`;
#              in a binding's run-shell, the client that pressed it), else
#              none, which toasts in every open sidebar.
#   -k KIND    info (default), ok, warn, error: the toast's glyph and colour.
#   -t SECONDS how long the toast shows (default 4; error 8).
#   TEXT       the words, joined by spaces; tabs and newlines become spaces.
#              tmux formats in it are expanded as display-message would.
#              Empty: nothing is shown.
#
# Always exits 0 and prints nothing.
#
# The toast transport (GLOBAL tmux options, set in ONE call):
#   @toast_seq  an integer, one more per toast (read, then written: two
#               racing notifies can share one; a toast is (seq, epoch_ms))
#   @toast      seq TAB epoch_ms TAB kind TAB ttl_s TAB client TAB text
#               (client "" = every sidebar)
# then the sidebar's focus notice is posted (notifyutil, in the background)
# so open sidebars redraw at once. Which clients have a sidebar is
# `@sidebar_clients` (kept by the sidebar word for word beside
# `@sidebar_pids`); a listed sidebar counts only while its pid lives. Every
# message is also logged: epoch TAB kind TAB client TAB text, in
# ~/.local/state/tmux-notify/log, trimmed to its last 500 lines past 1000.
#
# Cost: two tmux calls (one read: client, sidebars, seq and the expanded
# text together; one write or the display-message).
#
# Environment (tests):
#   NOTIFY_TMUX          the tmux command words (default `tmux`), e.g.
#                        "tmux -L test -f /dev/null" to target a private server
#   NOTIFY_LOG           the log file
#   AGENT_FOCUS_NOTICE   the notice name (the sidebar honours it too); set
#                        but empty: post nothing

zmodload zsh/datetime 2>/dev/null

client= kind=info ttl=
while (( $# )); do
    case $1 in
        -c|-k|-t)
            if (( $# < 2 )); then shift; break; fi
            case $1 in
                -c) client=$2 ;;
                -k) kind=$2 ;;
                -t) ttl=$2 ;;
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
if [[ ! $ttl =~ '^[0-9]+([.][0-9]+)?$' ]] || (( ttl <= 0 )); then
    if [[ $kind == error ]]; then ttl=8; else ttl=4; fi
fi

# One line: tabs, CRs and newlines → spaces.
text="$*"
text=${text//[$'\t\r\n']/ }
[[ -z ${text//[[:space:]]/} ]] && exit 0

tmux_cmd=(${(z)${NOTIFY_TMUX:-tmux}})
copt=() topt=()
if [[ -n $client ]]; then
    copt=(-c "$client")
    # -t too: display -p's formats follow -t's session (with only -c they
    # name the best client of the current session). A client tmux does not
    # know still exits 0, with every field empty: no name, so no toast.
    topt=(-c "$client" -t "$client")
fi

# The one read: the client's name as tmux knows it, the open sidebars' pids
# and their clients (word for word), the last seq, and the text expanded as
# display-message would expand it, unit-separated (the text last, so it may
# hold anything).
US=$'\x1f'
out=$("${tmux_cmd[@]}" display-message -p "${topt[@]}" -- \
    "#{client_name}$US#{@sidebar_pids}$US#{@sidebar_clients}$US#{@toast_seq}$US$text" 2>/dev/null)
read_ok=$?

target= pids= clients= seq=0 shown=$text
if (( read_ok == 0 )) && [[ $out == *$US*$US*$US*$US* ]]; then
    target=${out%%$US*}; out=${out#*$US}
    pids=${out%%$US*}; out=${out#*$US}
    clients=${out%%$US*}; out=${out#*$US}
    seq=${out%%$US*}; shown=${out#*$US}
    shown=${shown//[$'\t\r\n']/ }
    [[ $seq == <-> ]] || seq=0
    # Formats that expand to nothing leave nothing to say.
    [[ -z ${shown//[[:space:]]/} ]] && exit 0
fi

# Only a LIVE sidebar counts: a SIGKILLed one stays listed until something
# repairs the registry, and a toast for it would be swallowed. Word i of
# @sidebar_clients belongs to pid i of @sidebar_pids; `kill -0` is a builtin.
toast=0
if (( read_ok == 0 )); then
    pid_list=(${=pids}) client_list=(${=clients})
    for (( i = 1; i <= $#pid_list; i++ )); do
        p=${pid_list[i]}
        [[ $p == <1-> ]] && kill -0 "$p" 2>/dev/null || continue
        if [[ -n $target ]]; then
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
    payload="$seq"$'\t'"$ms"$'\t'"$kind"$'\t'"$ttl"$'\t'"$target"$'\t'"$shown"
    # tmux reads an argument ending in `;` as a command separator (the `;`
    # dropped) and one ending in `\;` as a literal `;`: keep the text's own.
    [[ $payload == *';' ]] && payload="${payload%;}\\;"
    "${tmux_cmd[@]}" set-option -gq @toast_seq "$seq" \; \
        set-option -gq @toast "$payload" >/dev/null 2>&1
    notice=${AGENT_FOCUS_NOTICE-com.mackhaymond.tmux.focus}
    if [[ -n $notice ]]; then
        notifyutil -p "$notice" </dev/null >/dev/null 2>&1 &!
    fi
else
    # No sidebar for it: tmux's own message, exactly as before.
    "${tmux_cmd[@]}" display-message "${copt[@]}" -- "$text" </dev/null >/dev/null 2>&1
fi

# The log, bounded.
log=${NOTIFY_LOG:-${XDG_STATE_HOME:-$HOME/.local/state}/tmux-notify/log}
{
    [[ -d ${log:h} ]] || mkdir -p -- "${log:h}"
    print -r -- "${EPOCHSECONDS:-$(date +%s)}"$'\t'"$kind"$'\t'"$target"$'\t'"$shown" >>"$log"
    integer n=$(wc -l <"$log")
    if (( n > 1000 )); then
        tail -n 500 -- "$log" >"$log.$$" && mv -f -- "$log.$$" "$log"
    fi
} 2>/dev/null
exit 0
