#!/usr/bin/env bash
# snooze — park a tab with an alarm: it comes back on its own at a set time.
#
# A snooze is a STASH with a wake time; nothing here moves a tab itself. The
# two kinds of stashed thing each carry their own alarm:
#   - a pinned history entry (closed-tabs.sh; a one-pane agent tab is stashed
#     that way) gets "wake_at": <epoch>  — snoozed ⇔ pinned && wake_at;
#   - a window in the `stash` session (stash.sh) gets @stash_wake <epoch>.
# Unsnoozing drops the alarm only: the thing stays stashed. This script parses
# when, dispatches to those two scripts, and runs the timer.
#
#   snooze.sh parse <when…>        the epoch on stdout, or exit 1 + why on
#                                  stderr. 30m 2h 1d 1w 2h30m "2h 30m" ·
#                                  tonight / evening (18:00) · tomorrow (9:00;
#                                  before 05:00 it is the day just begun)
#                                  · mon..sun (9:00, the next such day; today
#                                  only while it is still before 9:00) · 9am
#                                  9:30am 14:30 2pm noon midnight (the next
#                                  occurrence) · "fri 9am" · "tomorrow 2pm" ·
#                                  "today 5pm". Local time (date -j); always in
#                                  the future.
#   snooze.sh menu <client_tty> <target…>
#                                  the snooze menu on that client (display-menu
#                                  -c): 1 hour, 3 hours, This evening 18:00,
#                                  Tomorrow 9:00, Monday 9:00, Custom… (the
#                                  time box, below), and Unsnooze when any
#                                  target is snoozed. Each item re-enters
#                                  here as apply-text, so the time is read
#                                  when the item is picked, not when the menu
#                                  opened. A tab not parked yet that stash.sh
#                                  would refuse (`stash.sh check --snooze`,
#                                  exit 3) is not offered: its refusal text
#                                  ("… not snoozing it") is the toast, and no
#                                  menu opens when nothing is left.
#   snooze.sh apply [-c <client>] <epoch|unsnooze> <target…>
#                                  dispatch. p:<id> / b:<id> → closed-tabs.sh
#                                  snooze <epoch> / unsnooze; w:@N (or a bare
#                                  @N) already in `stash` → stash.sh set-wake;
#                                  w:@N elsewhere → stash.sh snooze (parks it;
#                                  unsnooze skips it); r:@N (the picker's
#                                  pending rows) is ignored. One toast:
#                                  "⏰ snoozed <what> — back <when>" (when:
#                                  "in 45m" under 50 minutes, else "at 17:45",
#                                  "tomorrow 9:00", "Fri 9:00").
#   snooze.sh apply-text [-c <client>] <text> <target…>
#                                  parse, then apply; unreadable text → a
#                                  warning toast and nothing changes (exit 0:
#                                  every verb a menu or popup runs exits 0
#                                  once it has said what happened)
#   snooze.sh custom -c <client> <target…>
#                                  the Custom… time box: a small popup on the
#                                  client running prompt-popup
#   snooze.sh prompt-popup -c <client> <target…>
#                                  inside it: fzf as an input box over a few
#                                  suggestions, each with when it lands; the
#                                  typed text wins when it reads as a time,
#                                  else the highlighted suggestion; Esc
#                                  cancels. Only the epoch leaves the popup
#                                  (to `apply`, after it closed); the text
#                                  is never put on a command line.
#   snooze.sh wake-due             the timer tick (LaunchAgent
#                                  com.mackhaymond.tmux-snooze, every 60 s)
#   snooze.sh reschedule           recompute snooze-next: the earliest wake
#                                  over pinned entries' wake_at and stashed
#                                  windows' @stash_wake (`stash.sh rows` field
#                                  7); no alarm at all removes the file
#   snooze.sh next                 print snooze-next (empty: no alarm)
#
# THE TIMER. launchd runs `wake-due` every minute, so its common case has to
# cost nothing: it reads ONE small file, snooze-next (the earliest wake
# epoch), with bash builtins and exits while that is in the future or the file
# is missing — no tmux, no jq. Only past it (or with a malformed file) does the
# slow path run: under a non-blocking lock dir (an overlapping tick skips), no
# tmux server → exit 0 (the file stays, so a wake missed while the server was
# down — or the laptop asleep or off — fires at the first tick after it is
# up); the due entries (jq over history.jsonl) go to `closed-tabs.sh wake`,
# the due windows (`stash.sh rows`) to `stash.sh wake`; their
# "<label>\t<session>" lines make ONE toast ("⏰ back: X in main", or
# "⏰ back: 3 snoozed tabs — X, Y, Z"); then reschedule.
# The scripts that change an alarm run `reschedule` themselves (detached);
# `apply` runs it too, so a missed one costs at most the next change.
#
# FAILED WAKES. Whatever is still due after its wake (refused, or failed) is
# a failure: `failures` holds "<key>\t<count>\t<last try>" per target (key
# p:<id> or w:@N). A failed target is due again only RETRY_SECS after its
# last try — reschedule counts it at that time, so it never holds back the
# other alarms — and after GIVEUP_AFTER tries it is given up: one toast
# ("⏰ couldn't bring back X — it's still in Snoozed (⌘Z)"), no more
# automatic tries, and reschedule leaves it out. It stays in Snoozed, due,
# for a hand restore. Every reschedule drops the record of a target that is
# no longer due (woken, unsnoozed, snoozed again or gone), so a hand restore
# or a new snooze starts it from zero.
#
# State: ${XDG_STATE_HOME:-~/.local/state}/tmux-snooze/ — snooze-next,
# failures, log, launchd.log (the LaunchAgent's stdout/stderr), wake.lock,
# resched.lock.
#
# Environment (tests): SNOOZE_NOW (a fixed "now" epoch), STASH_SH,
# CLOSED_TABS_SH, NOTIFY_SH (scripts to call instead of the installed ones),
# XDG_STATE_HOME. All but SNOOZE_NOW are carried into the menu's re-entry:
# commands tmux runs get the SERVER's environment, not this process's.
#
# Bash 3.2 (launchd runs /bin/bash): no associative arrays, no ${x,,}, and an
# empty array is expanded as ${a[@]+"${a[@]}"} under set -u.
set -uo pipefail

STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"
STATE_DIR="$STATE_HOME/tmux-snooze"
NEXT_FILE="$STATE_DIR/snooze-next"
RETRY_SECS=300

# --- the fast path: before anything else, builtins only ---------------------
if [ "${1:-}" = wake-due ]; then
    [ -f "$NEXT_FILE" ] || exit 0
    next=""
    read -r next <"$NEXT_FILE" 2>/dev/null
    case "$next" in
        ''|*[!0-9]*) ;;                         # malformed: let the slow path rewrite it
        *)
            if [ -n "${SNOOZE_NOW:-}" ]; then now="$SNOOZE_NOW"; else now=$(date +%s); fi
            [ "$next" -gt "$now" ] && exit 0
            ;;
    esac
fi

# A UTF-8 locale, ALWAYS, and exported, as stash.sh does it: a tmux client
# whose locale is not UTF-8 prints every control character and non-ASCII byte
# of its output as "_". launchd starts this with no locale at all, and then
# closed-tabs.sh read the session "main" as "main__", every wake of a snoozed
# entry failed, and notify.sh's \x1f split broke (the end-to-end probe,
# 2026-11-13). Exported, so closed-tabs.sh, stash.sh and notify.sh inherit it.
# The first non-empty of LC_ALL, LC_CTYPE, LANG decides, as in tmux itself.
case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
    *[Uu][Tt][Ff]-8*|*[Uu][Tt][Ff]8*) ;;
    *)
        if [ -n "${LC_ALL:-}" ]; then export LC_ALL=en_US.UTF-8
        elif [ -n "${LC_CTYPE:-}" ]; then export LC_CTYPE=en_US.UTF-8
        else export LANG=en_US.UTF-8
        fi ;;
esac

# --- everything else --------------------------------------------------------
HOLD=stash
SCRIPTS="$HOME/.config/tmux/scripts"
STASH_SH="${STASH_SH:-$SCRIPTS/stash.sh}"
CLOSED_TABS_SH="${CLOSED_TABS_SH:-$SCRIPTS/closed-tabs.sh}"
HISTORY="$STATE_HOME/tmux-closed-tabs/history.jsonl"
LOGFILE="$STATE_DIR/log"
US=$'\x1f'
TAB=$'\t'

mkdir -p "$STATE_DIR" 2>/dev/null

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOGFILE" 2>/dev/null; }

now_epoch() {
    if [ -n "${SNOOZE_NOW:-}" ]; then printf '%s\n' "$SNOOZE_NOW"; else date +%s; fi
}

# Messages go through notify.sh (a sidebar toast, else display-message), as
# closed-tabs.sh's notify_user: NOTIFY_SH, else the sibling notify.sh. It
# expands tmux formats in the text, and ours never mean one: a tab named
# "a#{client_tty}" must read that way, so every # after `--` is doubled.
notify_user() {
    local n="${NOTIFY_SH:-}" d="${BASH_SOURCE[0]%/*}" c="" a seen="" args=()
    for a in "$@"; do
        if [ -n "$seen" ]; then args+=("${a//#/##}")
        else args+=("$a"); [ "$a" = -- ] && seen=1
        fi
    done
    set -- "${args[@]}"
    [ "$d" = "${BASH_SOURCE[0]}" ] && d=.
    if [ -z "$n" ]; then
        for n in "$d/notify.sh" "$d/executable_notify.sh" "$SCRIPTS/notify.sh"; do
            [ -f "$n" ] && break
        done
    fi
    if [ -x "$n" ]; then "$n" "$@"
    elif [ -f "$n" ]; then zsh -f "$n" "$@"
    else
        while [ $# -gt 0 ]; do
            case "$1" in
                -c|-k|-t|-T) [ "$1" = -c ] && c="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
                --) shift; break ;;
                *) break ;;
            esac
        done
        tmux display-message ${c:+-c "$c"} -- "$*" 2>/dev/null
    fi
    return 0
}

stash_sh()  { [ -f "$STASH_SH" ] || return 1; bash "$STASH_SH" "$@"; }
closed_sh() { [ -f "$CLOSED_TABS_SH" ] || return 1; bash "$CLOSED_TABS_SH" "$@"; }

lower() { printf '%s' "$*" | tr '[:upper:]' '[:lower:]'; }

# --- parse ------------------------------------------------------------------

# at_day <now> <days ahead> <H> <M>: the epoch of that local wall-clock time.
at_day() {
    local ymd
    ymd=$(date -j -r "$1" -v+"$2"d '+%Y-%m-%d') || return 1
    date -j -f '%Y-%m-%d %H:%M:%S' "$ymd $(printf '%02d:%02d' "$3" "$4"):00" '+%s'
}

# parse_time <word>: sets PT_H PT_M, or fails. 9am 9:30am 9a 2pm 14:30 noon
# midnight. A bare number ("9") is refused: am or pm is a guess.
PT_H=0 PT_M=0
parse_time() {
    local w="$1" re='^([0-9]{1,2})(:([0-9]{2}))?(am|pm|a|p)?$' h m ap
    case "$w" in
        noon) PT_H=12 PT_M=0; return 0 ;;
        midnight) PT_H=0 PT_M=0; return 0 ;;
    esac
    [[ "$w" =~ $re ]] || return 1
    h=$((10#${BASH_REMATCH[1]})); m="${BASH_REMATCH[3]}"; ap="${BASH_REMATCH[4]}"
    [ -n "$m" ] && m=$((10#$m)) || m=0
    if [ -n "$ap" ]; then
        [ "$h" -ge 1 ] && [ "$h" -le 12 ] || return 1
        [ "$h" -eq 12 ] && h=0
        case "$ap" in p*) h=$((h + 12)) ;; esac
    else
        [ -n "${BASH_REMATCH[2]}" ] || return 1
        [ "$h" -le 23 ] || return 1
    fi
    [ "$m" -le 59 ] || return 1
    PT_H=$h PT_M=$m
}

# weekday <word>: 1 (mon) .. 7 (sun), or fails.
weekday() {
    case "$1" in
        mon|monday) echo 1 ;; tue|tues|tuesday) echo 2 ;; wed|weds|wednesday) echo 3 ;;
        thu|thur|thurs|thursday) echo 4 ;; fri|friday) echo 5 ;;
        sat|saturday) echo 6 ;; sun|sunday) echo 7 ;;
        *) return 1 ;;
    esac
}

# duration <word>: seconds, or fails. 30m 2h 1d 1w 2h30m 90min 1hr
duration() {
    local w="$1" re='^([0-9]+)(minutes|minute|mins|min|m|hours|hour|hrs|hr|h|days|day|d|weeks|week|wks|wk|w)(.*)$' n u s=0
    [ -n "$w" ] || return 1
    while [ -n "$w" ]; do
        [[ "$w" =~ $re ]] || return 1
        n=$((10#${BASH_REMATCH[1]})); u="${BASH_REMATCH[2]}"; w="${BASH_REMATCH[3]}"
        case "$u" in
            m|min*) s=$((s + n * 60)) ;;
            h*) s=$((s + n * 3600)) ;;
            d|day|days) s=$((s + n * 86400)) ;;
            *) s=$((s + n * 604800)) ;;
        esac
    done
    echo "$s"
}

# do_parse <when…> → epoch, or exit status 1 with the reason on stderr.
do_parse() {
    local text now words=() w day="" tod="" secs=0 alldur=1 d
    text=$(lower "$*")
    now=$(now_epoch)
    # Filler words go; a bare number joins the word after it ("45 min").
    local num=""
    set -f
    for w in $text; do
        case "$w" in in|at|until|this|on|next|for) continue ;; esac
        case "$w" in ''|*[!0-9]*) ;; *) [ -z "$num" ] && { num="$w"; continue; } ;; esac
        words+=("$num$w"); num=""
    done
    set +f
    [ -n "$num" ] && words+=("$num")
    if [ ${#words[@]} -eq 0 ]; then
        echo "snooze: when? (try 45m, 2h, tonight, tomorrow, fri 9am, 14:30)" >&2; return 1
    fi
    for w in "${words[@]}"; do
        d=$(duration "$w") || { alldur=0; break; }
        secs=$((secs + d))
    done
    if [ "$alldur" = 1 ]; then
        [ "$secs" -gt 0 ] || { echo "snooze: '$text' is no time at all" >&2; return 1; }
        echo $((now + secs)); return 0
    fi
    # Otherwise at most one day word and at most one time of day.
    for w in "${words[@]}"; do
        case "$w" in
            tonight|evening) [ -z "$tod" ] && tod=evening ;;     # "tonight 9pm": the 9pm
            today|tomorrow|tmrw|tmr|tom) [ -z "$day" ] || { bad "$text"; return 1; }; day="$w" ;;
            *)
                if weekday "$w" >/dev/null; then
                    [ -z "$day" ] || { bad "$text"; return 1; }; day="$w"
                elif parse_time "$w"; then
                    [ -z "$tod" ] || [ "$tod" = evening ] || { bad "$text"; return 1; }
                    tod="$PT_H:$PT_M"
                else
                    bad "$text"; return 1
                fi ;;
        esac
    done
    local h m nd t off target
    case "$tod" in
        evening) h=18 m=0 ;;
        '') h=9 m=0 ;;
        *) h="${tod%%:*}" m="${tod##*:}" ;;
    esac
    # Time words alone ("9am", "tonight"): the next occurrence.
    [ -z "$day" ] && day="next_occurrence"
    nd=$(date -j -r "$now" '+%u')
    case "$day" in
        next_occurrence)
            target=$(at_day "$now" 0 "$h" "$m") || return 1
            [ "$target" -gt "$now" ] || target=$(at_day "$now" 1 "$h" "$m") || return 1 ;;
        today)
            target=$(at_day "$now" 0 "$h" "$m") || return 1 ;;
        tomorrow|tmrw|tmr|tom)
            # Before 05:00 "tomorrow" is the day that has just begun (Mack
            # is up late): 01:30 "tomorrow" = 9:00 today. Still past → the
            # day after.
            off=1
            [ "$((10#$(date -j -r "$now" '+%H')))" -lt "$LATE_NIGHT_END" ] && off=0
            target=$(at_day "$now" "$off" "$h" "$m") || return 1
            [ "$target" -gt "$now" ] || target=$(at_day "$now" $((off + 1)) "$h" "$m") || return 1 ;;
        *)
            t=$(weekday "$day")
            off=$(( (t - nd + 7) % 7 ))
            target=$(at_day "$now" "$off" "$h" "$m") || return 1
            [ "$target" -gt "$now" ] || target=$(at_day "$now" $((off + 7)) "$h" "$m") || return 1 ;;
    esac
    if [ "$target" -le "$now" ]; then
        echo "snooze: '$text' has already passed" >&2; return 1
    fi
    echo "$target"
}
LATE_NIGHT_END=5
bad() { echo "snooze: can't read '$1' (try 45m, 2h, tonight, tomorrow, fri 9am, 14:30)" >&2; }

# when_label <epoch>: "in 45m" (under SOON_SECS), else the clock: "at 18:00",
# "tomorrow 9:00", "Mon 9:00", "Nov 30 9:00". Never "in 60m".
SOON_SECS=3000
when_label() {
    local t="$1" now d
    now=$(now_epoch); d=$((t - now))
    if [ "$d" -lt "$SOON_SECS" ]; then
        d=$(( (d + 59) / 60 )); [ "$d" -lt 1 ] && d=1
        echo "in ${d}m"; return 0
    fi
    clock_label "$t" at
}
# clock_label <epoch> [at]: "18:00" (today; "at 18:00" with `at`),
# "tomorrow 9:00", "Mon 9:00", "Nov 30 9:00".
clock_label() {
    local t="$1" now d ymd hh mm wd today tmrw
    now=$(now_epoch); d=$((t - now))
    read -r ymd hh mm wd <<EOF
$(date -j -r "$t" '+%Y-%m-%d %H %M %a')
EOF
    today=$(date -j -r "$now" '+%Y-%m-%d')
    tmrw=$(date -j -r "$now" -v+1d '+%Y-%m-%d')
    hh="${hh#0}"
    if [ "$ymd" = "$today" ]; then echo "${2:+$2 }$hh:$mm"
    elif [ "$ymd" = "$tmrw" ]; then echo "tomorrow $hh:$mm"
    elif [ "$d" -lt $((6 * 86400)) ]; then echo "$wd $hh:$mm"
    else echo "$(date -j -r "$t" '+%b %e' | tr -s ' ') $hh:$mm"
    fi
}

# --- targets ----------------------------------------------------------------

# split_targets <target…>: T_IDS (history entry ids) and T_WINS (window ids).
T_IDS=() T_WINS=()
split_targets() {
    local t
    T_IDS=() T_WINS=()
    for t in "$@"; do
        case "$t" in
            p:?*|b:?*) t="${t#?:}"; case "$t" in *[!A-Za-z0-9._-]*) continue ;; esac; T_IDS+=("$t") ;;
            w:@*|@*) t="${t#w:}"; case "${t#@}" in ''|*[!0-9]*) continue ;; esac; T_WINS+=("$t") ;;
            *) ;;   # r:@N (pending) and anything else
        esac
    done
}

# entry_info <id…>: "id<TAB>label<TAB>wake_at" per history entry found.
entry_info() {
    [ $# -gt 0 ] && [ -s "$HISTORY" ] || return 0
    jq -rR --arg ids " $* " '
        fromjson? | select(type == "object" and (.id | type) == "string")
        | select(.id as $i | $ids | contains(" " + $i + " "))
        | [.id, (.label // "" | tostring), (if (.wake_at | type) == "number" then .wake_at | tostring else "" end)]
        | @tsv' "$HISTORY" 2>/dev/null
}

# A window's label: @stash_label, else @agent_summary, else its name, each
# read as a plain value (tmux never expands a value's own #{…}) and chosen
# here, so a tab named "a#{client_tty}" stays exactly that.
WIN_FIELDS_FMT="#{session_name}${US}#{@stash_wake}${US}#{@stash_label}${US}#{@agent_summary}${US}#{window_name}"

# describe <target…>: sets D_LABELS (one per live target, entries first),
# D_SNOOZED (1 if any target has an alarm) and D_WSESS (each window's
# session, beside T_WINS; "" = gone).
D_LABELS=() D_SNOOZED=0 D_WSESS=()
describe() {
    local id label wake w line sess slabel summary name
    split_targets "$@"
    D_LABELS=() D_SNOOZED=0 D_WSESS=()
    while IFS="$TAB" read -r id label wake; do
        [ -n "$id" ] || continue
        D_LABELS+=("${label:-$id}")
        [ -n "$wake" ] && D_SNOOZED=1
    done <<EOF
$(entry_info ${T_IDS[@]+"${T_IDS[@]}"})
EOF
    for w in ${T_WINS[@]+"${T_WINS[@]}"}; do
        line=$(tmux display-message -p -t "$w" "$WIN_FIELDS_FMT" 2>/dev/null) || line=""
        if [ -z "$line" ]; then D_WSESS+=(""); continue; fi
        IFS="$US" read -r sess wake slabel summary name <<EOF
$line
EOF
        D_WSESS+=("$sess")
        [ -n "$wake" ] && D_SNOOZED=1
        label="${slabel:-${summary:-$name}}"
        D_LABELS+=("${label:-$w}")
    done
}

# what <label…>: one label as it is (shortened), several as "N tabs".
what() {
    if [ $# -eq 1 ]; then
        local l="$1"
        [ ${#l} -gt 40 ] && l="${l:0:39}…"
        printf '%s' "$l"
    else
        printf '%s tabs' "$#"
    fi
}

# --- apply ------------------------------------------------------------------

do_apply() {
    local client=""
    if [ "${1:-}" = -c ]; then client="${2:-}"; shift 2 || shift $#; fi
    local when="${1:-}"; [ $# -gt 0 ] && shift
    case "$when" in
        unsnooze) ;;
        ''|*[!0-9]*) echo "snooze: apply needs an epoch or unsnooze, got '$when'" >&2; return 1 ;;
    esac
    describe "$@"
    local ids=(${T_IDS[@]+"${T_IDS[@]}"}) wins=(${T_WINS[@]+"${T_WINS[@]}"})
    local wsess=(${D_WSESS[@]+"${D_WSESS[@]}"})
    local done_n=0 i w out set_wins=() park_wins=()
    if [ ${#ids[@]} -gt 0 ]; then
        if [ "$when" = unsnooze ]; then closed_sh unsnooze "${ids[@]}" >/dev/null
        else closed_sh snooze "$when" "${ids[@]}" >/dev/null
        fi && done_n=$((done_n + ${#ids[@]}))
    fi
    i=0
    for w in ${wins[@]+"${wins[@]}"}; do
        case "${wsess[$i]:-}" in
            '') ;;
            "$HOLD") set_wins+=("$w") ;;
            *) [ "$when" = unsnooze ] || park_wins+=("$w") ;;
        esac
        i=$((i + 1))
    done
    if [ ${#set_wins[@]} -gt 0 ]; then
        if [ "$when" = unsnooze ]; then stash_sh set-wake "" "${set_wins[@]}" >/dev/null
        else stash_sh set-wake "$when" "${set_wins[@]}" >/dev/null
        fi && done_n=$((done_n + ${#set_wins[@]}))
    fi
    # stash.sh snooze parks one window and prints what got the alarm
    # (p:<id> / w:<win>); nothing printed = refused (it has said why).
    for w in ${park_wins[@]+"${park_wins[@]}"}; do
        out=$(stash_sh snooze "$when" "$w" 2>/dev/null) || out=""
        [ -n "$out" ] && done_n=$((done_n + 1))
    done
    log "apply $when ${*:-} → $done_n"
    if [ "$done_n" -gt 0 ]; then
        local w_label
        if [ "$done_n" -eq 1 ] && [ ${#D_LABELS[@]} -eq 1 ]; then w_label=$(what "${D_LABELS[0]}")
        else w_label="$done_n tabs"; [ "$done_n" -eq 1 ] && w_label="1 tab"
        fi
        if [ "$when" = unsnooze ]; then
            notify_user ${client:+-c "$client"} -k ok -- "⏰ unsnoozed $w_label — still stashed"
        else
            notify_user ${client:+-c "$client"} -k ok -- "⏰ snoozed $w_label — back $(when_label "$when")"
        fi
    fi
    do_reschedule
    return 0
}

do_apply_text() {
    local client=""
    if [ "${1:-}" = -c ]; then client="${2:-}"; shift 2 || shift $#; fi
    local text="${1:-}" epoch err
    [ $# -gt 0 ] && shift
    if ! epoch=$(do_parse "$text" 2>/dev/null); then
        err=$(do_parse "$text" 2>&1 >/dev/null)
        [ -n "$err" ] || err="snooze: can't read '$text'"
        notify_user ${client:+-c "$client"} -k warn -- "$err"
        return 0        # told: a run-shell must not report a status too
    fi
    do_apply ${client:+-c "$client"} "$epoch" "$@"
}

# --- menu and the Custom… box -----------------------------------------------

# sq <word>: single-quoted for sh.  tq <string>: double-quoted for tmux's
# command parser (\, " and $ escaped).
sq() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
tq() {
    local s="$1"
    s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; s="${s//\$/\\\$}"
    printf '"%s"' "$s"
}

# reenter: the command line that runs this copy again with its overrides
# (commands tmux runs get the SERVER's environment, not this process's).
reenter() {
    local self d prefix
    d=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
    self="$d/$(basename "${BASH_SOURCE[0]}")"
    prefix="env XDG_STATE_HOME=$(sq "$STATE_HOME")"
    [ -n "${STASH_SH:-}" ] && prefix="$prefix STASH_SH=$(sq "$STASH_SH")"
    [ -n "${CLOSED_TABS_SH:-}" ] && prefix="$prefix CLOSED_TABS_SH=$(sq "$CLOSED_TABS_SH")"
    [ -n "${NOTIFY_SH:-}" ] && prefix="$prefix NOTIFY_SH=$(sq "$NOTIFY_SH")"
    [ -n "${SNOOZE_NOW:-}" ] && prefix="$prefix SNOOZE_NOW=$(sq "$SNOOZE_NOW")"
    printf '%s bash %s' "$prefix" "$(sq "$self")"
}

# targets_q <target…>: the targets, each sh-quoted, each after a space.
targets_q() { local t; for t in "$@"; do printf ' %s' "$(sq "$t")"; done; }

POPUP_X='#{?#{@popup_x},#{E:@popup_x},#{popup_centre_x}}'

do_menu() {
    local tty="${1:-}"; [ $# -gt 0 ] && shift
    [ -n "$tty" ] || { echo "snooze: menu needs a client tty" >&2; return 0; }
    describe "$@"
    local targets=() t j=0 reason rc refused=()
    for t in ${T_IDS[@]+"${T_IDS[@]}"}; do targets+=("p:$t"); done
    # A tab not parked yet: would stash.sh refuse it? (`stash.sh check
    # --snooze`: exit 3 and the refusal, already worded "… not snoozing it";
    # nothing changed.) Then it is not offered at all.
    for t in ${T_WINS[@]+"${T_WINS[@]}"}; do
        case "${D_WSESS[$j]:-}" in
            '') ;;                                  # gone
            "$HOLD") targets+=("w:$t") ;;
            *)
                reason=$(stash_sh check --snooze "$t" 2>/dev/null); rc=$?
                [ -n "$reason" ] || reason="not snoozing it"
                if [ "$rc" = 3 ]; then refused+=("$reason")
                else targets+=("w:$t")
                fi ;;
        esac
        j=$((j + 1))
    done
    if [ ${#refused[@]} -gt 0 ]; then
        reason="${refused[0]}"
        [ ${#refused[@]} -gt 1 ] && reason="$reason (${#refused[@]} tabs refused)"
        notify_user -c "$tty" -k warn -- "$reason"
        # The rest, described again: labels and alarms of what is offered.
        [ ${#targets[@]} -gt 0 ] && describe "${targets[@]}"
    fi
    if [ ${#targets[@]} -eq 0 ] || [ ${#D_LABELS[@]} -eq 0 ]; then
        [ ${#refused[@]} -gt 0 ] || notify_user -c "$tty" -k warn -- "snooze: nothing to snooze here"
        return 0
    fi
    local prefix tgt tq_tty
    prefix=$(reenter)
    tgt=$(targets_q "${targets[@]}")
    tq_tty=$(sq "$tty")
    # item <verb> [arg…]: the run-shell that runs `<verb> -c <tty> [arg…]
    # <targets>`. `|| true`: the script tells its story in toasts; a failing
    # run-shell would also put its status into the pane, in view mode.
    item() { local v="$1"; shift; printf 'run-shell -b %s' "$(tq "$prefix $v -c $tq_tty${*:+ $*}$tgt || true")"; }
    at_item() { item apply-text "$(sq "$1")"; }

    local hour evening_label evening_text title what_s
    hour=$(date -j -r "$(now_epoch)" '+%H'); hour=$((10#$hour))
    if [ "$hour" -lt 18 ]; then evening_label="This evening 18:00" evening_text="tonight"
    else evening_label="Tomorrow evening 18:00" evening_text="tomorrow 18:00"
    fi
    what_s=$(what "${D_LABELS[@]}")
    title="#[align=centre] snooze ${what_s//#/##} "

    local items=(
        "1 hour" 1 "$(at_item 1h)"
        "3 hours" 3 "$(at_item 3h)"
        "$evening_label" e "$(at_item "$evening_text")"
        "Tomorrow 9:00" t "$(at_item tomorrow)"
        "Monday 9:00" m "$(at_item mon)"
        ""
        "Custom…" c "$(item custom)"
    )
    [ "$D_SNOOZED" = 1 ] && items+=("" "Unsnooze" u "$(item apply unsnooze)")
    tmux display-menu -c "$tty" -b rounded -s 'bg=#1e1e2e,fg=#cdd6f4' -S 'fg=#f9e2af' \
        -H 'bg=#45475a,fg=#cdd6f4,bold' -T "$title" \
        -x "$POPUP_X" -y C "${items[@]}"
    return 0
}

# custom -c <tty> <target…>: the Custom… box, a small popup on that client
# running prompt-popup. A menu item runs this after its menu has closed; a
# popup that will not open at once (an overlay still going) is retried.
do_custom() {
    local tty=""
    if [ "${1:-}" = -c ]; then tty="${2:-}"; shift 2 || shift $#; fi
    [ -n "$tty" ] || return 0
    local cmd i
    cmd="$(reenter) prompt-popup -c $(sq "$tty")$(targets_q "$@")"
    for i in 1 2 3 4 5 6 7 8 9 10; do
        tmux display-popup -c "$tty" -E -w 44 -h 12 -x "$POPUP_X" -T " snooze until " "$cmd" 2>/dev/null && return 0
        sleep 0.1
    done
    log "custom: no popup on $tty"
    notify_user -c "$tty" -k warn -- "snooze: couldn't open the time box"
    return 0
}

# The Custom… box's suggestions: what is typed most, shown with when it lands.
SUGGEST=(30m 1h 2h 3h tonight tomorrow "tomorrow 2pm" mon "fri 9am" 1w)

# prompt-popup -c <tty> <target…>: inside the popup. fzf as an input box:
# the typed query wins when it reads as a time, else the highlighted
# suggestion; Esc cancels. The text never leaves this process: what is handed
# on (after the popup has closed) is the epoch.
do_prompt_popup() {
    local tty=""
    if [ "${1:-}" = -c ]; then tty="${2:-}"; shift 2 || shift $#; fi
    local s e lines="" out rc query sel epoch="" err today at ymd wd hm
    today=$(date -j -r "$(now_epoch)" '+%Y-%m-%d')
    for s in "${SUGGEST[@]}"; do
        e=$(do_parse "$s" 2>/dev/null) || continue
        # "tonight — 18:00", "fri 9am — Fri 9:00": the clock, with the day
        # when it is not today.
        read -r ymd wd hm <<EOF
$(date -j -r "$e" '+%Y-%m-%d %a %H:%M')
EOF
        hm="${hm#0}"
        if [ "$ymd" = "$today" ]; then at="$hm"; else at="$wd $hm"; fi
        lines="$lines$s$TAB$s — $at"$'\n'
    done
    out=$(printf '%s' "$lines" | fzf --print-query --prompt 'until: ' --delimiter "$TAB" --with-nth 2 \
            --reverse --no-multi --no-sort --info=hidden --header '⏎ snooze · esc cancel' \
            --bind 'enter:accept-or-print-query')
    rc=$?
    case "$rc" in 0|1) ;; *) return 0 ;; esac          # Esc / ⌃c: nothing
    query="${out%%$'\n'*}"
    sel=""
    case "$out" in *$'\n'*) sel=$(printf '%s\n' "${out#*$'\n'}" | head -1 | cut -f1) ;; esac
    if [ -n "$query" ] && epoch=$(do_parse "$query" 2>/dev/null); then :
    elif [ -n "$sel" ] && epoch=$(do_parse "$sel" 2>/dev/null); then :
    elif [ -z "$query" ]; then return 0
    else
        err=$(do_parse "$query" 2>&1 >/dev/null)
        [ -n "$err" ] || err="snooze: no time in that"
        notify_user ${tty:+-c "$tty"} -k warn -- "$err"
        return 0
    fi
    # Applied after the popup has closed: a park takes a moment.
    tmux run-shell -b "$(reenter) apply ${tty:+-c $(sq "$tty") }$epoch$(targets_q "$@") || true"
    return 0
}

# --- the timer --------------------------------------------------------------

# A mkdir lock older than <age> seconds is a crashed holder: broken by rename.
lock_try() {   # <dir> <stale age>
    mkdir "$1" 2>/dev/null && return 0
    local m now
    m=$(stat -f %m "$1" 2>/dev/null) || return 1
    now=$(date +%s)
    [ $((now - m)) -gt "$2" ] || return 1
    mv "$1" "$1.stale.$$" 2>/dev/null && rm -rf "$1.stale.$$"
    mkdir "$1" 2>/dev/null
}

# write_atomic <file> <content>: temp + rename; empty content removes it.
write_atomic() {
    if [ -n "$2" ]; then
        printf '%s' "$2" >"$1.tmp.$$" && mv -f "$1.tmp.$$" "$1"
    else
        rm -f "$1"
    fi
}

# alarms: every alarm, one "key<TAB>wake<TAB>title" line each: pinned entries
# with wake_at (key p:<id>) and stashed windows with a wake (key w:@N).
alarms() {
    if [ -s "$HISTORY" ]; then
        jq -rR 'fromjson? | select(type == "object" and .pinned == true and (.id | type) == "string" and (.wake_at | type) == "number")
            | ["p:" + .id, (.wake_at | floor | tostring),
               ((.label // "") | tostring | if . == "" then ((.desc // .id) | tostring) else . end)]
            | @tsv' "$HISTORY" 2>/dev/null
    fi
    local w l wake
    while IFS="$US" read -r w _o l _ts _st _cwd wake _rest; do
        case "$wake" in ''|*[!0-9]*) continue ;; esac
        printf 'w:%s\t%s\t%s\n' "$w" "$wake" "$l"
    done <<EOF
$(stash_sh rows 2>/dev/null)
EOF
}

# The failed-wake records (FAILED WAKES above): FAILS is the file's text.
FAIL_FILE="$STATE_DIR/failures"
GIVEUP_AFTER=3
FAILS="" F_COUNT=0 F_LAST=0 EFF=0
fails_load() { FAILS=""; [ -f "$FAIL_FILE" ] && FAILS=$(cat "$FAIL_FILE" 2>/dev/null); return 0; }
# fail_info <key>: F_COUNT, F_LAST (0 0: no failure).
fail_info() {
    local k c l
    F_COUNT=0 F_LAST=0
    while IFS="$TAB" read -r k c l; do
        [ "$k" = "$1" ] || continue
        case "$c" in ''|*[!0-9]*) c=0 ;; esac
        case "$l" in ''|*[!0-9]*) l=0 ;; esac
        F_COUNT=$c F_LAST=$l
        return 0
    done <<EOF
$FAILS
EOF
    return 0
}
# due_at <key> <wake>: EFF = when it is next to be tried (a failed one
# RETRY_SECS after its last try); status 1 = given up.
due_at() {
    fail_info "$1"
    [ "$F_COUNT" -ge "$GIVEUP_AFTER" ] && return 1
    EFF=$2
    if [ "$F_COUNT" -gt 0 ] && [ $((F_LAST + RETRY_SECS)) -gt "$EFF" ]; then EFF=$((F_LAST + RETRY_SECS)); fi
    return 0
}

RLOCK="$STATE_DIR/resched.lock"
resched_lock() {
    local i=0
    until lock_try "$RLOCK" 30; do
        i=$((i + 1)); [ "$i" -ge 100 ] && return 1
        sleep 0.05
    done
}
resched_unlock() { rmdir "$RLOCK" 2>/dev/null; return 0; }

# Caller holds RLOCK. snooze-next = the earliest time anything is to be tried;
# the failure records of targets no longer due are dropped.
reschedule_locked() {
    local now min="" key wake _title keep=" " k c l newf=""
    now=$(now_epoch)
    fails_load
    while IFS="$TAB" read -r key wake _title; do
        case "$wake" in ''|*[!0-9]*) continue ;; esac
        EFF="$wake"
        if [ "$wake" -le "$now" ]; then    # due: its failure record counts
            keep="$keep$key "
            due_at "$key" "$wake" || continue
        fi
        if [ -z "$min" ] || [ "$EFF" -lt "$min" ]; then min="$EFF"; fi
    done <<EOF
$(alarms)
EOF
    while IFS="$TAB" read -r k c l; do
        [ -n "$k" ] || continue
        case "$keep" in *" $k "*) newf="$newf$k$TAB$c$TAB$l"$'\n' ;; esac
    done <<EOF
$FAILS
EOF
    [ "$newf" = "$FAILS"$'\n' ] || write_atomic "$FAIL_FILE" "$newf"
    write_atomic "$NEXT_FILE" "${min:+$min$'\n'}"
}

do_reschedule() {
    resched_lock || { log "reschedule: lock busy, skipped"; return 0; }
    reschedule_locked
    resched_unlock
}

# toast_list <label…>: "A, B, C" (at most 4, then …).
toast_list() {
    local list="" n=0 lbl
    for lbl in "$@"; do
        n=$((n + 1))
        [ "$n" -gt 4 ] && { list="$list, …"; break; }
        list="${list:+$list, }$(what "$lbl")"
    done
    printf '%s' "$list"
}

do_wake_due() {
    local lock="$STATE_DIR/wake.lock" now ids=() wins=() tries=() titles=() key wake title out="" line
    lock_try "$lock" 600 || return 0                # another tick is at it
    trap 'rmdir "'"$lock"'" 2>/dev/null' EXIT
    tmux list-sessions >/dev/null 2>&1 || return 0  # no server: the next tick
    now=$(now_epoch)
    # Due, not given up, and not waiting out a failed try's retry.
    fails_load
    while IFS="$TAB" read -r key wake title; do
        case "$wake" in ''|*[!0-9]*) continue ;; esac
        [ "$wake" -le "$now" ] || continue
        due_at "$key" "$wake" || continue
        [ "$EFF" -le "$now" ] || continue
        tries+=("$key"); titles+=("${title:-$key}")
        case "$key" in p:*) ids+=("${key#p:}") ;; w:*) wins+=("${key#w:}") ;; esac
    done <<EOF
$(alarms)
EOF
    if [ ${#tries[@]} -eq 0 ]; then do_reschedule; return 0; fi
    if [ ${#ids[@]} -gt 0 ]; then
        log "wake entries ${ids[*]}"
        out=$(closed_sh wake "${ids[@]}" 2>>"$LOGFILE")
    fi
    if [ ${#wins[@]} -gt 0 ]; then
        log "wake windows ${wins[*]}"
        line=$(stash_sh wake "${wins[@]}" 2>>"$LOGFILE")
        [ -n "$line" ] && out="${out:+$out$'\n'}$line"
    fi
    # One toast for everything that came back.
    local labels=() sessions=() lbl sess
    while IFS="$TAB" read -r lbl sess; do
        [ -n "$lbl$sess" ] || continue
        labels+=("${lbl:-a tab}"); sessions+=("$sess")
    done <<EOF
$out
EOF
    if [ ${#labels[@]} -eq 1 ]; then
        notify_user -c '*' -k info -t 8 -- "⏰ back: $(what "${labels[0]}") in ${sessions[0]:-?}"
    elif [ ${#labels[@]} -gt 1 ]; then
        notify_user -c '*' -k info -t 8 -- "⏰ back: ${#labels[@]} snoozed tabs — $(toast_list "${labels[@]}")"
    fi
    log "woke ${#labels[@]} of ${#tries[@]}"

    # What is still due failed (refused, or the wake went wrong): count it,
    # and give it up after GIVEUP_AFTER tries.
    local still=" " gave=() k c l newf="" i
    while IFS="$TAB" read -r key wake title; do
        case "$wake" in ''|*[!0-9]*) continue ;; esac
        [ "$wake" -le "$now" ] && still="$still$key "
    done <<EOF
$(alarms)
EOF
    if ! resched_lock; then log "wake-due: reschedule lock busy"; return 0; fi
    fails_load
    while IFS="$TAB" read -r k c l; do         # every other target's record as it was
        [ -n "$k" ] || continue
        case " ${tries[*]} " in *" $k "*) continue ;; esac
        newf="$newf$k$TAB$c$TAB$l"$'\n'
    done <<EOF
$FAILS
EOF
    i=0
    for key in "${tries[@]}"; do
        case "$still" in
            *" $key "*)
                fail_info "$key"
                c=$((F_COUNT + 1))
                newf="$newf$key$TAB$c$TAB$now"$'\n'
                log "wake failed: $key (try $c of $GIVEUP_AFTER)"
                [ "$c" -ge "$GIVEUP_AFTER" ] && gave+=("${titles[$i]}") ;;
        esac
        i=$((i + 1))
    done
    write_atomic "$FAIL_FILE" "$newf"
    reschedule_locked
    resched_unlock
    if [ ${#gave[@]} -eq 1 ]; then
        notify_user -c '*' -k warn -t 12 -- "⏰ couldn't bring back $(what "${gave[0]}") — it's still in Snoozed (⌘Z)"
    elif [ ${#gave[@]} -gt 1 ]; then
        notify_user -c '*' -k warn -t 12 -- "⏰ couldn't bring back ${#gave[@]} snoozed tabs — $(toast_list "${gave[@]}") — still in Snoozed (⌘Z)"
    fi
    return 0
}

do_next() {
    local next=""
    [ -f "$NEXT_FILE" ] && read -r next <"$NEXT_FILE"
    [ -n "$next" ] && printf '%s\n' "$next"
    return 0
}

case "${1:-}" in
    parse)        shift; do_parse "$@" ;;
    menu)         shift; do_menu "$@" ;;
    apply)        shift; do_apply "$@" ;;
    apply-text)   shift; do_apply_text "$@" ;;
    custom)       shift; do_custom "$@" ;;
    prompt-popup) shift; do_prompt_popup "$@" ;;
    wake-due)     do_wake_due ;;
    reschedule)   do_reschedule ;;
    next)         do_next ;;
    *)            sed -n '/^#   snooze.sh/,/^# THE TIMER/p' "$0" | sed 's/^# \{0,1\}//;$d' >&2; exit 2 ;;
esac
