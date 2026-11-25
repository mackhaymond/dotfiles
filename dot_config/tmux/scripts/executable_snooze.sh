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
#                                  stderr. The reading is snooze-when.py's
#                                  (its header lists what it takes): 2h30m ·
#                                  an hour and a half · tonight · tomorrow
#                                  morning · fri 9am · next monday · a week
#                                  from friday 3pm · dec 3 at 2pm · end of
#                                  the month. Local time; always in the future.
#   snooze.sh menu <client_tty> <target…>
#                                  the snooze menu on that client (display-menu
#                                  -c): 1 hour, 3 hours, This evening 18:00,
#                                  Tomorrow 9:00, Monday 9:00, Custom… (the
#                                  time box, below), and Unsnooze when any
#                                  target is snoozed. Each item re-enters
#                                  here as apply-text, so the time is read
#                                  when the item is picked, not when the menu
#                                  opened; all but Unsnooze with --ask-space
#                                  (see apply). A tab not parked yet that
#                                  stash.sh would refuse (`stash.sh check
#                                  --snooze`, exit 3) is not offered: its
#                                  refusal text ("… not snoozing it") is the
#                                  toast, and no menu opens when nothing is
#                                  left.
#   snooze.sh apply [-c <client>] [--ask-space] <epoch|unsnooze> <target…>
#                                  dispatch. p:<id> / b:<id> → closed-tabs.sh
#                                  snooze <epoch> / unsnooze; w:@N (or a bare
#                                  @N) already in `stash` → stash.sh set-wake;
#                                  w:@N elsewhere → stash.sh snooze (parks it;
#                                  unsnooze skips it); r:@N (the picker's
#                                  pending rows) is ignored. One toast:
#                                  "⏰ snoozed <what> — back <when>" (when:
#                                  "in 45m" under 50 minutes, else "at 17:45",
#                                  "tomorrow 9:00", "Fri 9:00"). With
#                                  --ask-space (either side of -c), a space
#                                  this park left with only its home gets
#                                  "last tab snoozed · close space NAME until
#                                  it wakes?" on that client (closed-tabs.sh
#                                  ask-space --snoozed), last of all: it holds
#                                  until the box is answered. Without it,
#                                  never (agent-ui's review asks itself).
#   snooze.sh apply-text [-c <client>] [--ask-space] <text> <target…>
#                                  parse, then apply; unreadable text → a
#                                  warning toast and nothing changes (exit 0:
#                                  every verb a menu or popup runs exits 0
#                                  once it has said what happened)
#   snooze.sh custom -c <client> [--ask-space] <target…>
#                                  the Custom… time box: a small popup on the
#                                  client running prompt-popup
#   snooze.sh prompt-popup -c <client> [--ask-space] <target…>
#                                  inside it: fzf as an input box over a few
#                                  suggestions, each with when it lands; a
#                                  line under the prompt says, as you type,
#                                  what Enter would take. The typed text wins
#                                  when it reads as a time, else the
#                                  highlighted suggestion it is the start of;
#                                  unreadable text keeps the box open and says
#                                  why; Esc cancels. Only the epoch leaves the
#                                  popup (to `apply`, after it closed); the
#                                  text is never put on a command line.
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
# up), and likewise while a restart's restore is pending
# (@space_restore_pending, docs/spaces.md: a wake then would race resurrect);
# the due entries (jq over history.jsonl) go to `closed-tabs.sh wake`, the
# due windows (`stash.sh rows`) to `stash.sh wake`; their
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
# CLOSED_TABS_SH, NOTIFY_SH, SNOOZE_WHEN (scripts to call instead of the
# installed ones; SNOOZE_WHEN is the time reader, else the snooze-when.py
# beside this script), XDG_STATE_HOME. All but SNOOZE_NOW are carried into the menu's re-entry:
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

# --- parse ------------------------------------------------------------------

# when_py: sets W_PY (the interpreter) and W_FILE (snooze-when.py), or fails.
# Never `python3` off PATH: the pyenv shim costs 130 ms, and this runs on every
# keystroke of the time box. Both interpreters run the same file.
W_PY="" W_FILE=""
when_py() {
    [ -n "$W_PY" ] && return 0
    local p d="${BASH_SOURCE[0]%/*}" f=""
    [ "$d" = "${BASH_SOURCE[0]}" ] && d=.
    for f in "${SNOOZE_WHEN:-}" "$d/snooze-when.py" "$d/executable_snooze-when.py" "$SCRIPTS/snooze-when.py"; do
        [ -n "$f" ] && { [ -f "$f" ] || [ "$f" = "${SNOOZE_WHEN:-}" ]; } && break
    done
    [ -f "$f" ] || return 1
    for p in /opt/homebrew/bin/python3 /usr/bin/python3; do
        [ -x "$p" ] && { W_PY="$p" W_FILE="$f"; return 0; }
    done
    return 1
}
# The text is data: after --, nothing in it is an option or a verb.
run_when() { when_py && "$W_PY" -I -S "$W_FILE" "$@"; }

# do_parse <when…> → epoch, or exit status 1 with the reason on stderr. What
# snooze-when.py says is believed only in its two shapes, digits with exit 0
# or a "snooze: …" line with exit 1: a crash or a missing reader is said as
# such, never taken for a time.
BROKEN="snooze: the time reader isn't working"
do_parse() {
    local out
    when_py
    if out=$(run_when parse -- "$*" 2>/dev/null); then
        case "$out" in
            ''|*[!0-9]*) ;;
            *) printf '%s\n' "$out"; return 0 ;;
        esac
    else
        out=$(run_when parse -- "$*" 2>&1 >/dev/null)
        case "$out" in
            *$'\n'*) ;;
            'snooze: '?*) printf '%s\n' "$out" >&2; return 1 ;;
        esac
    fi
    log "parse: no answer from '${W_FILE:-snooze-when.py}' (${W_PY:-no python3})"
    echo "$BROKEN" >&2
    return 1
}

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

# opts <arg…>: the options in front of a verb's arguments, in any order:
# O_CLIENT (-c <client>), O_ASK (--ask-space, "" without it) and O_N, how many
# arguments they took (the caller shifts them).
O_CLIENT="" O_ASK="" O_N=0
opts() {
    O_CLIENT="" O_ASK="" O_N=0
    while [ $# -gt 0 ]; do
        case "$1" in
            -c) O_CLIENT="${2:-}"; O_N=$((O_N + 1)); shift
                [ $# -gt 0 ] && { O_N=$((O_N + 1)); shift; } ;;
            --ask-space) O_ASK="$1"; O_N=$((O_N + 1)); shift ;;
            *) return 0 ;;
        esac
    done
    return 0
}

do_apply() {
    opts "$@"; shift "$O_N"
    local client="$O_CLIENT" ask="$O_ASK"
    local when="${1:-}"; [ $# -gt 0 ] && shift
    case "$when" in
        unsnooze) ;;
        ''|*[!0-9]*) echo "snooze: apply needs an epoch or unsnooze, got '$when'" >&2; return 1 ;;
    esac
    describe "$@"
    local ids=(${T_IDS[@]+"${T_IDS[@]}"}) wins=(${T_WINS[@]+"${T_WINS[@]}"})
    local wsess=(${D_WSESS[@]+"${D_WSESS[@]}"})
    local done_n=0 i w s seen out set_wins=() park_wins=() park_sess=() left=()
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
            *) [ "$when" = unsnooze ] || { park_wins+=("$w"); park_sess+=("${wsess[$i]}"); } ;;
        esac
        i=$((i + 1))
    done
    if [ ${#set_wins[@]} -gt 0 ]; then
        if [ "$when" = unsnooze ]; then stash_sh set-wake "" "${set_wins[@]}" >/dev/null
        else stash_sh set-wake "$when" "${set_wins[@]}" >/dev/null
        fi && done_n=$((done_n + ${#set_wins[@]}))
    fi
    # stash.sh snooze parks one window and prints what got the alarm
    # (p:<id> / w:<win>); nothing printed = refused (it has said why). Each
    # session one of them left is noted once, for --ask-space. A parent goes
    # with its subagents, one alarm for the family (stash.sh FAMILIES): one
    # line each, counted apart (kids_n) so the toast still names the parent.
    local kids_n=0
    i=0
    for w in ${park_wins[@]+"${park_wins[@]}"}; do
        out=$(stash_sh snooze "$when" "$w" 2>/dev/null) || out=""
        if [ -n "$out" ]; then
            done_n=$((done_n + 1))
            kids_n=$((kids_n + $(grep -c . <<<"$out") - 1))
            seen=0
            for s in ${left[@]+"${left[@]}"}; do [ "$s" = "${park_sess[$i]}" ] && seen=1; done
            [ "$seen" = 1 ] || left+=("${park_sess[$i]}")
        fi
        i=$((i + 1))
    done
    log "apply $when ${*:-} → $done_n"
    if [ "$done_n" -gt 0 ]; then
        local w_label
        if [ "$done_n" -eq 1 ] && [ ${#D_LABELS[@]} -eq 1 ]; then w_label=$(what "${D_LABELS[0]}")
        else w_label="$done_n tabs"; [ "$done_n" -eq 1 ] && w_label="1 tab"
        fi
        if [ "$kids_n" -gt 0 ]; then
            w_label="$w_label + $kids_n subagent"; [ "$kids_n" -gt 1 ] && w_label="${w_label}s"
        fi
        if [ "$when" = unsnooze ]; then
            notify_user ${client:+-c "$client"} -k ok -- "⏰ unsnoozed $w_label — still stashed"
        else
            notify_user ${client:+-c "$client"} -k ok -- "⏰ snoozed $w_label — back $(when_label "$when")"
        fi
    fi
    do_reschedule
    # Last: it holds until the box is answered. closed-tabs.sh decides whether
    # there is one (a visible space left with only its home, never main).
    if [ -n "$ask" ] && [ -n "$client" ]; then
        for s in ${left[@]+"${left[@]}"}; do
            closed_sh ask-space --snoozed "$client" "=$s:" </dev/null >/dev/null 2>&1
        done
    fi
    return 0
}

do_apply_text() {
    opts "$@"; shift "$O_N"
    local client="$O_CLIENT" ask="$O_ASK"
    local text="${1:-}" epoch err
    [ $# -gt 0 ] && shift
    if ! epoch=$(do_parse "$text" 2>/dev/null); then
        err=$(do_parse "$text" 2>&1 >/dev/null)
        [ -n "$err" ] || err="snooze: can't read '$text'"
        notify_user ${client:+-c "$client"} -k warn -- "$err"
        return 0        # told: a run-shell must not report a status too
    fi
    do_apply ${client:+-c "$client"} ${ask:+"$ask"} "$epoch" "$@"
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
    [ -n "${SNOOZE_WHEN:-}" ] && prefix="$prefix SNOOZE_WHEN=$(sq "$SNOOZE_WHEN")"
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
    # --ask-space on every item that can park a space's last tab.
    item() { local v="$1"; shift; printf 'run-shell -b %s' "$(tq "$prefix $v -c $tq_tty${*:+ $*}$tgt || true")"; }
    at_item() { item apply-text --ask-space "$(sq "$1")"; }

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
        "Custom…" c "$(item custom --ask-space)"
    )
    [ "$D_SNOOZED" = 1 ] && items+=("" "Unsnooze" u "$(item apply unsnooze)")
    tmux display-menu -c "$tty" -b rounded -s 'bg=#1e1e2e,fg=#cdd6f4' -S 'fg=#f9e2af' \
        -H 'bg=#45475a,fg=#cdd6f4,bold' -T "$title" \
        -x "$POPUP_X" -y C "${items[@]}"
    return 0
}

# custom -c <tty> [--ask-space] <target…>: the Custom… box, a small popup on
# that client running prompt-popup. A menu item runs this after its menu has
# closed; a popup that will not open at once (an overlay still going) is
# retried.
do_custom() {
    opts "$@"; shift "$O_N"
    local tty="$O_CLIENT" ask="$O_ASK"
    [ -n "$tty" ] || return 0
    local cmd i w=58 h cw="" ch=""
    cmd="$(reenter) prompt-popup -c $(sq "$tty")${ask:+ $ask}$(targets_q "$@")"
    # Every suggestion shows: one row each, plus the prompt, its rule, the
    # live line and the keys, and the border's two. tmux refuses a popup bigger
    # than its client ("width too large"), so a small client gets less.
    h=$(( ${#SUGGEST[@]} + 6 ))
    IFS=' ' read -r cw ch <<EOF
$(tmux display-message -p -c "$tty" '#{client_width} #{client_height}' 2>/dev/null)
EOF
    case "$cw" in ''|*[!0-9]*) ;; *) [ "$cw" -gt 0 ] && [ "$cw" -lt "$w" ] && w=$cw ;; esac
    case "$ch" in ''|*[!0-9]*) ;; *) [ "$ch" -gt 0 ] && [ "$ch" -lt "$h" ] && h=$ch ;; esac
    for i in 1 2 3 4 5 6 7 8 9 10; do
        tmux display-popup -c "$tty" -E -w "$w" -h "$h" -x "$POPUP_X" -T " snooze until " "$cmd" 2>/dev/null && return 0
        sleep 0.1
    done
    log "custom: no popup on $tty"
    notify_user -c "$tty" -k warn -- "snooze: couldn't open the time box"
    return 0
}

# The Custom… box's suggestions: what is typed most, shown with when it lands.
SUGGEST=(30m 1h 2h 3h tonight tomorrow "tomorrow morning" "tomorrow 2pm" mon "fri 9am" "next monday" 1w)

# prompt-popup -c <tty> [--ask-space] <target…>: inside the popup. fzf as an
# input box; what Enter takes is snooze-when.py's `box` / `enter` (one rule for
# the live line and for Enter, its header has it). Unreadable text keeps the
# box open with the reason, Esc cancels. fzf hands both to the reader in its own
# quoting ({q} also makes it run with no row left, {1} then being empty); what
# leaves fzf (become) is the epoch alone.
do_prompt_popup() {
    opts "$@"; shift "$O_N"
    local tty="$O_CLIENT" ask="$O_ASK"
    local s e at lines="" epoch live
    when_py                                 # here, not in a subshell: exported below
    while IFS="$TAB" read -r s e at; do
        case "$e" in ''|*[!0-9]*) continue ;; esac
        lines="$lines$s$TAB$s — $at"$'\n'
    done <<EOF
$(printf '%s\n' "${SUGGEST[@]}" | run_when batch 2>/dev/null)
EOF
    # No suggestion read: the reader is broken, and no Enter would do anything.
    if [ -z "$lines" ]; then
        notify_user ${tty:+-c "$tty"} -k warn -- "$BROKEN"
        return 0
    fi
    export W_PY W_FILE
    [ -n "${SNOOZE_NOW:-}" ] && export SNOOZE_NOW
    # bg-: typing never waits for it, and a newer line cancels an older one.
    live='bg-cancel+bg-transform-header:exec "$W_PY" -I -S "$W_FILE" box -- {q} {1}'
    epoch=$(printf '%s' "$lines" | fzf --with-shell '/bin/sh -c' \
            --prompt 'until: ' --delimiter "$TAB" --with-nth 2 \
            --reverse --no-multi --no-sort --info=hidden --header 'type a time, or pick one' \
            --footer '⏎ snooze · esc cancel' --footer-border=none \
            --bind "result:$live" --bind "focus:$live" \
            --bind 'enter:transform:exec "$W_PY" -I -S "$W_FILE" enter -- {q} {1}')
    case "$epoch" in ''|*[!0-9]*) return 0 ;; esac      # Esc / ⌃c: nothing
    # Applied after the popup has closed: a park takes a moment.
    tmux run-shell -b "$(reenter) apply ${tty:+-c $(sq "$tty") }${ask:+$ask }$epoch$(targets_q "$@") || true"
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
    # A restart's restore still running (space.sh's @space_restore_pending:
    # an epoch under 15 min old, not `done`, the rule space.sh's sync uses):
    # resurrect is rebuilding sessions, and a wake now would land in main or
    # make a space resurrect is about to make. Nothing this tick — no wakes,
    # no failure counted — and the alarms stay due, so the next tick after
    # the restore takes them.
    local pend
    pend=$(tmux show -gqv @space_restore_pending 2>/dev/null)
    case "$pend" in
        ''|*[!0-9]*) ;;
        *) if [ $((now - pend)) -lt 900 ]; then log "restore pending — wakes deferred"; return 0; fi ;;
    esac
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
