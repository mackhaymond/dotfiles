#!/usr/bin/env bash
# review-nudge — every so often, ask Mack to review his old agents.
#
#   review-nudge.sh check            evaluate, and maybe toast (the timer tick:
#                                    LaunchAgent com.mackhaymond.tmux-review-nudge,
#                                    every 300 s)
#   review-nudge.sh check --dry-run  print the decision; post nothing, write
#   review-nudge.sh --dry-run        nothing (the state file and log untouched)
#
# THE RULE. A nudge goes out when
#     (old agents >= 3  OR  letters ran out)
#     AND no nudge in the last 4 hours
#     AND the tmux server runs with at least one client attached
#     AND Mack is at it: some client had input in the last 5 minutes
#         (#{client_activity}). Nobody there: nothing, and the 4 hours do not
#         restart, so it waits for him instead of firing at 3am.
#   old agents     idle through 8h+ OF USE, as the review popup counts them:
#                  `review.sh count` (an integer; anything else counts as 0).
#                  Only time Mack was at tmux counts, so a night with the
#                  laptop shut ages nobody. This script keeps the record:
#                  every tick with him there appends its epoch to the use log,
#                  and review.sh turns 8h of it into a cutoff.
#   letters ran out  all 26 single letters a-z are held RIGHT NOW: each is the
#                  @agent_key of some window of a VISIBLE session (not
#                  @space_hidden, not named in @space_hidden_sessions) that is
#                  not a home (@space_home unset), i.e. an agent or a shell.
#                  Not a child tab either (@agent_child, @agent_parent or
#                  @agent_spawn_id): subagents get no letter.
#                  An overflow key (`;a`, ...) alone is no trigger: a window
#                  keeps its key for life, so one can outlast the shortage.
# Within the 4 hours a tick is one tmux call (for the use log) and exits.
#
# THE NUDGE. One info toast in every sidebar (`notify.sh -c '*'`; a client
# with no sidebar gets tmux's own message, the text alone, for 30 s), up for
# 5 minutes or until the review popup opens (review.sh open takes it down),
# titled "Review agents" (the popup's key is prefix O, ⌃S O). With its click
# action the sidebar draws it as a solid card:
#     "9 agents idle 8h+ of use · ⌃S O or click to review"
#     "9 agents idle 8h+ of use · letters ran out · ⌃S O or click to review"
#     "letters ran out (2 on ; keys) · ⌃S O or click to review"
#     "letters ran out · ⌃S O or click to review"   (no window on a ; key yet)
# with a click action (`notify.sh -a`): `review.sh open {client}`, the review
# popup on the clicking client (the sidebar substitutes {client}). Then
# last-nudge is written, so the next one waits 4 hours whether or not
# anyone clicked.
#
# State: ${XDG_STATE_HOME:-~/.local/state}/agent-review/ — last-nudge (an
# epoch), in-use (the use log: one epoch per tick that found Mack at tmux, each
# worth the 300 s before it, bounded; review.sh reads it), log (one line per tick that got past the cooldown, bounded),
# launchd.log (the LaunchAgent's stdout/stderr).
#
# Environment (tests):
#   REVIEW_NUDGE_NOW    a fixed "now" epoch
#   REVIEW_NUDGE_TMUX   the tmux command words (default `tmux`)
#   REVIEW_COUNT_CMD    the old-agent count command, run by bash -c (default
#                       `'<review.sh>' count`)
#   REVIEW_SH           the review.sh path the toast's action runs (default the
#                       sibling review.sh)
#   REVIEW_NUDGE_ACTIVE how recent a client's input must be (seconds)
#   NOTIFY_SH           the notify.sh to post with (default the sibling)
#   XDG_STATE_HOME
#
# Always exits 0. Bash 3.2 (launchd runs /bin/bash): no associative arrays,
# and an empty array is expanded as ${a[@]+"${a[@]}"} under set -u.
set -uo pipefail

EVERY=14400     # at most one nudge per 4 hours
MIN_OLD=3       # this many agents idle 8h+ is worth a nudge
TTL=300         # seconds the toast shows (review.sh open takes it down)
MSG_TTL=30      # seconds tmux's own message shows, on a client with no sidebar
ACTIVE="${REVIEW_NUDGE_ACTIVE:-300}"   # a client with input this recent: he is there

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/agent-review"
LAST_FILE="$STATE_DIR/last-nudge"
USE_FILE="$STATE_DIR/in-use"
LOGFILE="$STATE_DIR/log"

dry=0 verb=""
for a in "$@"; do
    case "$a" in
        --dry-run|-n) dry=1 ;;
        check) verb=check ;;
        -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "review-nudge.sh: unknown argument: $a" >&2; exit 0 ;;
    esac
done
[ -z "$verb" ] && [ "$dry" = 1 ] && verb=check
if [ "$verb" != check ]; then
    echo "usage: review-nudge.sh check [--dry-run]" >&2
    exit 0
fi

if [ -n "${REVIEW_NUDGE_NOW:-}" ]; then now="$REVIEW_NUDGE_NOW"; else now=$(date +%s); fi

say() {   # the decision: printed on a dry run, logged otherwise
    if [ "$dry" = 1 ]; then printf '%s\n' "$*"; return 0; fi
    mkdir -p "$STATE_DIR" 2>/dev/null
    printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOGFILE" 2>/dev/null
    local n
    n=$(wc -l <"$LOGFILE" 2>/dev/null | tr -d ' ')
    if [ "${n:-0}" -gt 1000 ] 2>/dev/null; then
        tail -n 500 "$LOGFILE" >"$LOGFILE.$$" 2>/dev/null && mv -f "$LOGFILE.$$" "$LOGFILE"
    fi
    return 0
}

# A UTF-8 locale, always (as snooze.sh): launchd gives none, and a tmux
# client without one prints \x1f, tabs and non-ASCII as "_". Exported, so
# notify.sh and review.sh inherit it.
case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
    *[Uu][Tt][Ff]-8*|*[Uu][Tt][Ff]8*) ;;
    *)
        if [ -n "${LC_ALL:-}" ]; then export LC_ALL=en_US.UTF-8
        elif [ -n "${LC_CTYPE:-}" ]; then export LC_CTYPE=en_US.UTF-8
        else export LANG=en_US.UTF-8
        fi ;;
esac

# shellcheck disable=SC2206  # word-split on purpose, as notify.sh's NOTIFY_TMUX
tmux_cmd=(${REVIEW_NUDGE_TMUX:-tmux})
t() { "${tmux_cmd[@]}" "$@"; }

# --- the server, its clients, and whether he is there ---------------------------
clients="" server=1
clients=$(t list-clients -F '#{client_activity}' 2>/dev/null) || server=0
# The newest input on any client (an epoch). Unreadable: taken as there (but
# not logged as use).
newest=0 guess=0
while IFS= read -r a; do
    case "$a" in ''|*[!0-9]*) guess=1; continue ;; esac
    [ "$a" -gt "$newest" ] && newest=$a
done <<<"$clients"
here_now=0
[ $((now - newest)) -le "$ACTIVE" ] && here_now=1

# The use log: one epoch per tick that found him there, each worth the TICK
# seconds before it. Every tick, the 4 hours or not; never on a dry run.
if [ "$server" = 1 ] && [ "$here_now" = 1 ] && [ "$dry" = 0 ]; then
    mkdir -p "$STATE_DIR" 2>/dev/null
    printf '%s\n' "$now" >>"$USE_FILE" 2>/dev/null
    n=$(wc -l <"$USE_FILE" 2>/dev/null | tr -d ' ')
    if [ "${n:-0}" -gt 12000 ] 2>/dev/null; then
        tail -n 10000 "$USE_FILE" >"$USE_FILE.$$" 2>/dev/null && mv -f "$USE_FILE.$$" "$USE_FILE"
    fi
fi
[ "$guess" = 1 ] && [ "$newest" = 0 ] && here_now=1

# --- the cooldown ------------------------------------------------------------
last=""
[ -f "$LAST_FILE" ] && read -r last <"$LAST_FILE" 2>/dev/null
case "$last" in
    ''|*[!0-9]*) last="" ;;
    *) last=$((10#$last)) ;;   # (a leading 0 is not octal)
esac
# (A stamp in the future, from a clock jump, does not hold the nudge back.)
if [ -n "$last" ] && [ "$last" -le "$now" ] && [ $((now - last)) -lt "$EVERY" ]; then
    [ "$dry" = 1 ] && say "skip: nudged $(( (now - last) / 60 ))m ago (one per $((EVERY / 3600))h)"
    exit 0
fi

if [ "$server" = 0 ]; then
    say "skip: no tmux server"
    exit 0
fi
if [ -z "$clients" ]; then
    say "skip: no client attached"
    exit 0
fi
if [ "$here_now" = 0 ]; then
    say "skip: no input for $(( (now - newest) / 60 ))m"
    exit 0
fi

# --- the triggers --------------------------------------------------------------
# The letters held by the letter-holders (agents and non-home shells of
# visible sessions): `held` distinct single letters a-z, `overflow` windows on
# a 2-char `;x` key. Exhausted only while all 26 singles are held.
US=$'\x1f'
# An external subagent holds no letter (docs/agent-tab-indicator.md §8,
# "Letters"): the allocator skips a window with @agent_child, the raw
# @agent_parent or @agent_spawn_id, and a key one still carries is free. Such
# a window is folded into the first field as if hidden, so it never counts.
held=0 seen="" overflow=0
rows=$(t list-windows -a -F "#{?#{||:#{@space_hidden},#{||:#{m:* #{session_name} *, #{@space_hidden_sessions} },#{||:#{@agent_child},#{||:#{@agent_parent},#{@agent_spawn_id}}}}},1,0}${US}#{@space_home}${US}#{@agent_key}" 2>/dev/null)
while IFS= read -r row; do
    [ -z "$row" ] && continue
    hidden=${row%%"$US"*}
    rest=${row#*"$US"}
    home=${rest%%"$US"*}
    key=${rest#*"$US"}
    [ "$hidden" = 0 ] && [ -z "$home" ] || continue
    case "$key" in
        # (Spelled out: a locale's collation can put capitals inside a-z.)
        [abcdefghijklmnopqrstuvwxyz])
            case "$seen" in
                *"$key"*) ;;
                *) seen="$seen$key"; held=$((held + 1)) ;;
            esac ;;
        ';'?*) overflow=$((overflow + 1)) ;;
    esac
done <<<"$rows"
out=0
[ "$held" -ge 26 ] && out=1

# The scripts beside this one (installed: review.sh; in the chezmoi source:
# executable_review.sh).
here=${BASH_SOURCE[0]%/*}
[ "$here" = "${BASH_SOURCE[0]}" ] && here=.
here=$(cd "$here" 2>/dev/null && pwd) || here="$HOME/.config/tmux/scripts"
sibling() {
    local n
    for n in "$here/$1" "$here/executable_$1" "$HOME/.config/tmux/scripts/$1"; do
        [ -f "$n" ] && { printf '%s\n' "$n"; return 0; }
    done
    printf '%s\n' "$here/$1"
}
REVIEW_SH="${REVIEW_SH:-$(sibling review.sh)}"
NOTIFY_SH="${NOTIFY_SH:-$(sibling notify.sh)}"

squote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

# Old agents, as the review popup counts them.
count_cmd="${REVIEW_COUNT_CMD:-$(squote "$REVIEW_SH") count}"
old=$(bash -c "$count_cmd" </dev/null 2>/dev/null | tr -d '[:space:]')
case "$old" in
    ''|*[!0-9]*) old=0 ;;
esac
old=$((10#$old))

if [ "$old" -lt "$MIN_OLD" ] && [ "$out" = 0 ]; then
    say "skip: $old idle 8h+ (needs $MIN_OLD), $held/26 letters held"
    exit 0
fi

# --- the nudge -----------------------------------------------------------------
agents="$old agents idle 8h+ of use"
[ "$old" = 1 ] && agents="1 agent idle 8h+ of use"
if [ "$old" -ge "$MIN_OLD" ] && [ "$out" = 1 ]; then
    text="$agents · letters ran out · ⌃S O or click to review"
elif [ "$old" -ge "$MIN_OLD" ]; then
    text="$agents · ⌃S O or click to review"
elif [ "$overflow" -gt 0 ]; then
    text="letters ran out ($overflow on ; keys) · ⌃S O or click to review"
else
    text="letters ran out · ⌃S O or click to review"
fi
# The action runs through run-shell, which expands tmux formats: a `#` in the
# path is doubled. {client} is the sidebar's to fill in.
hash="#"
action="$(squote "${REVIEW_SH//$hash/$hash$hash}") open {client}"

if [ "$dry" = 1 ]; then
    say "nudge: $old idle 8h+, $held/26 letters held, $overflow overflow keys → would toast: $text"
    say "action: $action"
    exit 0
fi

# notify.sh expands formats in the text; ours never means one.
if [ -x "$NOTIFY_SH" ]; then
    "$NOTIFY_SH" -c '*' -k info -t "$TTL" -m "$MSG_TTL" -T "Review agents" -a "$action" -- "${text//$hash/$hash$hash}"
elif [ -f "$NOTIFY_SH" ]; then
    zsh -f "$NOTIFY_SH" -c '*' -k info -t "$TTL" -m "$MSG_TTL" -T "Review agents" -a "$action" -- "${text//$hash/$hash$hash}"
else
    say "error: no notify.sh at $NOTIFY_SH"
    exit 0
fi
mkdir -p "$STATE_DIR" 2>/dev/null
printf '%s\n' "$now" >"$LAST_FILE.$$" 2>/dev/null && mv -f "$LAST_FILE.$$" "$LAST_FILE"
say "nudged: $old idle 8h+, $held/26 letters held, $overflow overflow keys"
exit 0
