#!/usr/bin/env bash
# stash — park a tmux window out of the tab bar without stopping it.
#
# tmux has no hide/minimize (still an open request, tmux/tmux#3047), but it does
# not need one: a window's session membership is just a pointer. `move-window`
# to a detached holding session takes the tab off the bar while the window, its
# panes, its processes and its scrollback carry on untouched — verified by round
# trip, including that custom window options survive the move. That last part is
# why there is no state file here: where a window came from is recorded ON the
# window, so it cannot go stale, cannot be orphaned by a tmux restart, and is
# garbage-collected by the window closing.
#
# SUSPENDING AGENTS. Parking a tab is an explicit "not now", which makes it a
# good moment to stop paying for a Claude session that is sitting there: an idle
# one holds 640 MB - 1.4 GB and ~0.5-0.9% of a core (measured 2026-08-22). So a
# parked window holding exactly one agent is SIGTERMed — which exits cleanly and
# reaps its MCP children — and `claude --resume` puts it back, full context
# intact, when the tab is unparked. Never SIGSTOP: it frees no memory at all and
# leaves the TUI permanently unable to read input.
#
# Everything needed to resume is stored ON the window, like @stash_origin, so
# there is still no state file to go stale or be orphaned.
#
# NOTHING IN THE STASH IS RUNNING (2026-11-11; supersedes the two paragraphs
# above for every NEW park — their machinery stays only for windows suspended
# before then, until they are migrated). A park now classifies each tab:
#   - refused, with the reason: the home tab, a working agent, unsent text in
#     an agent's composer, background workflow / subagent / cua work, ssh /
#     mosh / et in a pane's foreground, a split tab with an agent in it, a
#     pane whose process runs with no shell under it;
#   - a one-pane agent tab (claude, abby, codex, opencode, grok, copilot, agy)
#     is not parked at all: `closed-tabs.sh close --pin` closes it into a
#     PINNED history entry that resumes it (the ⌘Z picker's Stashed section);
#   - anything else moves into the holding session and every pane's
#     foreground job is FROZEN (⌃Z, SIGSTOP as the fallback), recorded in
#     @stash_frozen with its exact argv. Unparking types `fg` (warm); after a
#     tmux restart the process is gone and the recorded command is re-run in
#     its directory instead (cold, @stash_cold).
#
#   stash.sh stash   [<window>]   park it (default: current)
#   stash.sh unstash [<window>] [<client>]
#                                 bring one back (no <window>: the merged
#                                 picker, `closed-tabs.sh pick-popup --stashed`),
#                                 and take <client> to it if it went home to
#                                 another session; a frozen job is thawed
#   stash.sh stash-many <w>...    park a group in one transaction
#   stash.sh unstash-many [-c <client>] <w>...
#                                 bring a group back in one transaction
#   stash.sh kill-many <w>...     discard parked windows: each is closed through
#                                 `closed-tabs.sh close`, so it lands in the ⌘Z
#                                 backlog; discarded session ids land in the
#                                 log with the command that resumes them by hand
#   stash.sh rows                 the parked windows, one \x1f-separated row
#                                 each: window id, origin, label, park time,
#                                 frozen|cold|idle|suspended|pending, cwd,
#                                 wake epoch (the merged picker's)
#   stash.sh snooze <epoch> [<w>] `stash` plus an alarm (agent tab: a pinned
#                                 entry with wake_at; else @stash_wake);
#                                 prints p:<entry id> / w:<window id> per
#                                 thing snoozed
#   stash.sh set-wake <epoch|""> <w>...  set / clear the alarm on parked windows
#   stash.sh wake <w>...          the alarm went off: back to its origin at the
#                                 end of the bar, no client switch, thawed;
#                                 prints "<label>\t<session>" per window
#   stash.sh publish-count        @stash_count = parked windows + pinned entries
#   stash.sh thaw <w> [<client>]  bring a frozen window's jobs back (unstash
#                                 hands off to this)
#   stash.sh sel-start here|left|right  start a range at the current tab
#   stash.sh sel-move  left|right grow or shrink it
#   stash.sh sel-cancel           drop the selection
#   stash.sh sel-commit           park everything selected
#   stash.sh sel-send             move everything selected to another (or a
#                                 new) session, picked in a popup
#   stash.sh sel-kill <w> <client>  close everything selected (homes skipped)
#                                 after a popup only ⇧Y confirms, through
#                                 closed-tabs.sh
#   stash.sh sel-kill-prompt <w>  print that menu's question (tests)
#   stash.sh count                how many are parked, pinned entries included
#                                 (for the status line)
#   stash.sh list                 what is parked, and where each came from
#   stash.sh restore-state        re-apply parked state after a resurrect restore
set -uo pipefail

# A UTF-8 locale, ALWAYS. A tmux client whose locale is not UTF-8 prints every
# control character of a -F format as "_" — and every record this script reads
# from tmux is split on \x1f (SEP, below). Without it a publish parses no rows
# at all; before save_state's server tests (2026-10-15) that orphaned every
# suspended session and deleted the sidecar. Callers outside tmux (launchd,
# the kill guard, `env -i`) need not have one, so it is fixed here, the way
# tmux itself decides: the first non-empty of LC_ALL, LC_CTYPE, LANG.
case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
    *[Uu][Tt][Ff]-8*|*[Uu][Tt][Ff]8*) ;;
    *)
        if [ -n "${LC_ALL:-}" ]; then export LC_ALL=en_US.UTF-8
        elif [ -n "${LC_CTYPE:-}" ]; then export LC_CTYPE=en_US.UTF-8
        else export LANG=en_US.UTF-8
        fi ;;
esac

HOLD=stash          # the detached holding session

# Absolute, because this script re-enters itself inside a popup and
# `display-popup` does NOT expand #{...} formats in its command the way
# run-shell does — a `#{HOME}/...` path reaches the popup's shell literally,
# fails to exec, and the popup vanishes instantly with no error anywhere.
#
# Overridable so a copy of this script can be exercised against a scratch tmux
# server without the re-entrant calls silently landing back in the installed
# one — which would test the old code and report that the new code passed.
SELF="${STASH_SELF:-$HOME/.config/tmux/scripts/stash.sh}"

# The ⌘Z history: a parked agent tab is closed into it as a pinned entry, a
# discarded parked window is closed into it, and the unpark picker is its.
# CLOSED_TABS_SH names another (tests stub it); missing = those paths degrade
# (an agent tab is refused, a discard is a plain kill-window).
CLOSED_TABS_SH="${CLOSED_TABS_SH:-$HOME/.config/tmux/scripts/closed-tabs.sh}"
# Its history file, for the pinned count (publish-count). Where closed-tabs.sh
# keeps it: $XDG_STATE_HOME/tmux-closed-tabs.
CLOSED_HISTORY="${XDG_STATE_HOME:-$HOME/.local/state}/tmux-closed-tabs/history.jsonl"

SESS_DIR="$HOME/.claude/sessions"
TERM_WAIT=12          # seconds to allow for a graceful exit
RESUME_WAIT=360       # 0.25s ticks => 90s; a big transcript takes a while to replay
ACTIVE_SECS=15        # a transcript written this recently means a turn is in flight

# The picker popup's geometry — and, derived from it, the size a suspended
# agent's screen is captured at for the picker's preview pane. One pair of
# numbers feeding both sides, so the snapshot is taken at the width the
# preview will actually render it at. Snapshots are keyed by session id,
# which survives a tmux restart; window ids do not.
# Same popup as closed-tabs.sh's picker, so the two read alike: list on top,
# preview UNDER it at (nearly) the full width.
POPUP_W_PCT=94
POPUP_H_PCT=85
# Centred on the whole WezTerm row while a sidebar sits beside the client
# (tmux.conf.tmpl's @popup_x); tmux's own centre on a server without it.
POPUP_X='#{?#{@popup_x},#{E:@popup_x},#{popup_centre_x}}'
PREVIEW_PCT=72
PREVIEW_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/tmux-stash/previews"

# Records are delimited with the unit separator, NOT a tab: bash treats tab as
# IFS whitespace and collapses runs of it, so a session that has not reported a
# `status` yet loses that empty field and every later field shifts left.
SEP=$'\x1f'

# Serialise the part that reads tmux state and then acts on it. Both binds are
# run-shell, so two presses genuinely run at once, and every check-then-move in
# here was racy: two parks of a two-window session both saw "2 windows", both
# moved, and the second emptied the session — which with detach-on-destroy on
# drops the attached client to a shell.
#
# mkdir is the atomic primitive (there is no flock(1) on macOS). The lock is
# held only across the state-changing section — never across the up-to-12s
# suspend or the up-to-90s resume, which would make one park block the next.
LOCKDIR="${TMPDIR:-/tmp}/tmux-stash.${UID:-$(id -u)}.lock"
LOCK_STALE=30

# The sidecar has its OWN mutex, separate from the one above, because save_state
# is reached from paths that deliberately run unlocked and concurrently (see the
# header on save_state) AND from inside this lock's critical section via
# publish() — one mutex for both would deadlock. There is no lock-ordering cycle
# to worry about: save_state never takes the main lock, so the order is always
# main -> sidecar and never the reverse.
SAVE_LOCKDIR="${TMPDIR:-/tmp}/tmux-stash-save.${UID:-$(id -u)}.lock"

# `lock_acquire <dir> wait` never gives up while the lock is merely HELD: a
# holder either releases it or, past LOCK_STALE, has it broken below, so the
# wait is bounded by that. It still fails if the lock cannot be created at all
# (100 tries with no directory there), since waiting cannot fix that.
lock_acquire() {
    local dir="${1:-$LOCKDIR}" mode="${2:-}"
    local i=0 owner age
    while :; do
        if mkdir "$dir" 2>/dev/null; then
            # Stamping ownership is part of ACQUIRING, not a formality after it.
            # If the directory is gone by now — a concurrent holder's `rm -rf`
            # racing our mkdir, seen under 10-way contention — we do not hold
            # the lock, and returning anyway would be worse than failing: with
            # no pid file, lock_release refuses to free it and the lock sticks
            # until the stale sweep. Go round again instead.
            # Braces around the redirection: a failing `>` is reported by the
            # SHELL, not by printf, so `printf ... 2>/dev/null` does not silence
            # it and the retry printed a scary path error on every race.
            { printf '%s' "$$" > "$dir/pid"; } 2>/dev/null && return 0
        fi
        i=$((i + 1))
        # In wait mode only CONSECUTIVE failures with no directory there
        # count: a lock released between our mkdir and this test is just a
        # retry, but 100 in a row means it cannot be created.
        if [ "$mode" = wait ]; then
            if [ -d "$dir" ]; then i=0; elif [ "$i" -gt 100 ]; then
                log "could not create the lock (${dir##*/})"; return 1
            fi
        elif [ "$i" -gt 100 ]; then
            log "could not take the lock (${dir##*/})"; return 1
        fi
        owner=$(cat "$dir/pid" 2>/dev/null)
        age=$(( $(date +%s) - $(stat -f %m "$dir" 2>/dev/null || date +%s) ))
        if { [ -n "$owner" ] && ! kill -0 "$owner" 2>/dev/null; } || [ "$age" -ge "$LOCK_STALE" ]; then
            # Breaking must itself be atomic. `rm -rf` then `mkdir` is not a
            # compare-and-swap: two processes can both judge the lock stale,
            # both remove it, and both then create it — and the second `rm`
            # deletes the FIRST one's live lock, so both proceed into the
            # critical section holding "the lock". rename(2) has exactly one
            # winner, so only the process that succeeds in moving the stale
            # directory aside is allowed to clear it.
            if mv "$dir" "${dir}.stale.$$" 2>/dev/null; then
                log "broke stale lock (${dir##*/}, owner ${owner:-?}, age ${age}s)"
                rm -rf "${dir}.stale.$$" 2>/dev/null
            fi
        fi
        sleep 0.1
    done
}

# Only ever release a lock we still own. Without the ownership test, a process
# whose lock was broken out from under it would delete whoever holds it now.
lock_release() {
    local dir="${1:-$LOCKDIR}"
    [ "$(cat "$dir/pid" 2>/dev/null)" = "$$" ] || return 0
    rm -rf "$dir" 2>/dev/null
}

# A holder doing a long series of steps (a range park: a pin or a freeze per
# window) renews the lock's age, so LOCK_STALE measures time since its last
# step, never the whole series — a range must not be broken into as stale.
lock_touch() {
    local dir="${1:-$LOCKDIR}"
    [ "$(cat "$dir/pid" 2>/dev/null)" = "$$" ] && touch "$dir" 2>/dev/null
    return 0
}

hold_exists() { tmux has-session -t "=$HOLD" 2>/dev/null; }

# Every session that is not a place for a tab, one name per line: tmux.conf's
# @space_hidden_sessions, any session spaces.conf flagged @space_hidden, and the
# built-in list as a floor (a server running without the spaces config still
# has an `agents` and a holding session). Hard-coded subsets of it were the bug
# (2026-10-22): the send picker offered `pending` and `btop-popup`, and an
# unpark whose origin was gone could land in `agents` — tmux-pty-mcp's session,
# whose sweep deletes an untagged window — and switch the client there.
hidden_sessions() {
    local s
    printf '%s\n' "$HOLD" agents tasks scratch btop-popup pending
    # read -a, not an unquoted expansion: no pathname expansion of the names.
    local -a cfg=()
    read -r -a cfg <<< "$(tmux show -gqv @space_hidden_sessions 2>/dev/null)"
    for s in ${cfg[@]+"${cfg[@]}"}; do printf '%s\n' "$s"; done
    tmux list-sessions -F '#{?#{@space_hidden},#{session_name},}' 2>/dev/null | grep -v '^$'
    return 0
}
# A here-string, not `hidden_sessions | grep -q`: grep -q quits on the first
# match, the rest of hidden_sessions then dies of SIGPIPE, and pipefail turns
# that 141 into "not hidden" — for every built-in name once tmux.conf sets
# @space_hidden_sessions to the same list (2026-10-22 review).
is_hidden_session() { [ -n "${1:-}" ] && grep -qxF -- "$1" <<< "$(hidden_sessions)"; }

# Close the gap a park leaves behind. `renumber-windows on` only fires when a
# window is CLOSED — moving one to another session is not a close, so parking
# tab 2 of 1,2,3 left 1,3 with a hole in it. `move-window -r` renumbers a
# session sequentially.
#
# Ordering matters: this MUST run before save_state, because the sidecar is
# keyed by window index and renumbering changes it. Callers renumber, then
# publish.
renumber() {
    local sess
    for sess in "$@"; do
        [ -n "$sess" ] || continue
        tmux has-session -t "=$sess" 2>/dev/null || continue
        tmux move-window -r -t "=$sess" 2>/dev/null
    done
}
# The `&&`/`||` form printed TWO lines when tmux failed: wc still printed 0 and
# pipefail then propagated tmux's failure, firing the `|| echo 0` as well. A
# two-line @stash_count broke both the statusline test and `[ "$(count)" -gt 1 ]`.
count_windows() {
    local n
    hold_exists || { printf '0'; return 0; }
    n=$(tmux list-windows -t "=$HOLD" -F '#{window_id}' 2>/dev/null | wc -l | tr -d ' ')
    case "$n" in ''|*[!0-9]*) n=0 ;; esac
    printf '%s' "$n"
}
# Pinned history entries (agent tabs parked through `close --pin`): the other
# half of what the stash holds. One jq over the file, no lock — a count read
# mid-append is off by one until the next publish. Lines that do not parse
# are skipped (fromjson?), so one torn line cannot zero the count.
pinned_count() {
    local n=0
    if [ -s "$CLOSED_HISTORY" ] && command -v jq >/dev/null 2>&1; then
        n=$(jq -R 'fromjson? | select(type == "object" and .pinned == true) | 1' "$CLOSED_HISTORY" 2>/dev/null | wc -l | tr -d ' ')
        case "$n" in ''|*[!0-9]*) n=0 ;; esac
    fi
    printf '%s' "$n"
}
# Everything stashed: parked windows plus pinned entries.
count() { printf '%s' "$(( $(count_windows) + $(pinned_count) ))"; }
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
# msg [-k KIND] TEXT: "stash: TEXT", to the client tmux resolves.
msg() {
    local k=info
    [ "${1:-}" = -k ] && { k="$2"; shift 2; }
    notify_user -k "$k" -- "stash: $*"
}

# Notes about what suspension decided go to a file, not over the tab bar. They
# explain a non-event ("parked, but left the agent running because…") which is
# worth being able to look up and not worth interrupting for.
LOGFILE="$HOME/Library/Logs/tmux-stash.log"
log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOGFILE" 2>/dev/null; }

# The status line reads @stash_count, which is written here on every change
# rather than polled with a #() shell call. A #() in the status format re-forks
# on every redraw forever; this forks only when you actually park or restore
# something, which on an idle machine is never.
publish() { publish_count; save_state; }
# The count alone: no sidecar, no lock — closed-tabs.sh calls it after every
# pin change, sometimes while a park holds this script's lock.
publish_count() { tmux set-option -g @stash_count "$(count)" 2>/dev/null; }

# SNOOZE. A snoozed window is a parked one with an alarm: @stash_wake, the
# epoch it comes back at (`snooze`, `set-wake`, `wake` below). snooze.sh's
# timer reads the earliest alarm from a file of its own, which `snooze.sh
# reschedule` recomputes — run after every change to @stash_wake, detached
# (fire-and-forget, like closed-tabs.sh's publish-count), so a park never
# waits on it. SNOOZE_SH names another (tests); missing = nothing to tell.
SNOOZE_SH="${SNOOZE_SH:-$HOME/.config/tmux/scripts/snooze.sh}"
reschedule_snooze() {
    [ -f "$SNOOZE_SH" ] || return 0
    ( bash "$SNOOZE_SH" reschedule </dev/null >/dev/null 2>&1 & )
    return 0
}

# --- surviving a tmux restart -------------------------------------------------
#
# tmux-resurrect already saves the holding session verbatim — its panes and
# windows appear in the save file as `stash` like any other session — so parked
# windows come back parked with no help from us. What it does NOT save is
# WINDOW OPTIONS, and that is where everything this script knows lives:
# @stash_origin (where to put a window back), @stash_label (its name once the
# agent that supplied it is gone) and above all @stash_session, which after a
# suspend is the ONLY remaining pointer to that conversation — claude deletes
# its own ~/.claude/sessions/<pid>.json on the way out.
#
# So the options are mirrored to a sidecar and re-applied by a post-restore
# hook. Written on every state change rather than from resurrect's save hook:
# this set only changes when this script runs, so a save hook would add a
# second writer, an ordering dependency on a hook chain two other things
# already share, and nothing else.
# Follows @resurrect-dir rather than hardcoding a path: the mirror belongs
# beside the save it corresponds to, and a second tmux server on another socket
# (which is how this gets tested) points that option somewhere else precisely so
# it cannot touch the real one.
#
# Expanded the way tmux-resurrect and resurrect-save.sh expand it ($HOME and a
# leading ~), so `last` below is looked up where the save actually wrote it.
resurrect_dir() {
    local d; d=$(tmux show -gqv @resurrect-dir 2>/dev/null)
    d="${d:-$HOME/.tmux/resurrect}"
    d="${d//\$HOME/$HOME}"
    printf '%s' "${d/#\~/$HOME}"
}

# When one resurrect snapshot's layout was captured (epoch seconds), or
# nothing. <file> is `last` or a tmux_resurrect_*.txt.
#
# `last` is a symlink (resurrect-save.sh `ln -sfn`, like upstream) to a
# BASENAME, and only its target describes a layout: an unchanged save deletes
# its new file and leaves `last` on the older one, and resurrect-guard.sh can
# repoint `last` BACK to an older good file, which gives the link itself a
# fresh mtime describing nothing.
#
# The capture time is the stamp in the target's NAME — resurrect-save.sh sets
# TS (`date +%Y%m%dT%H%M%S`, local time, the same format as upstream's
# resurrect_file_path) before its list-panes, so it is a lower bound on when
# the layout was read. The file's mtime is not: the post-save hook
# (resurrect-save-repair.py) os.replace()s it seconds after the capture.
# `date -j -f` reads the stamp in the local zone, as it was written; in the
# repeated hour of a DST fall-back it may resolve an hour late, so the result
# is the EARLIER of stamp and mtime — the mtime is never before the capture,
# so that is still an upper bound, and erring early only refuses more.
# A name without a stamp falls back to the mtime.
snapshot_file_time() {
    local f="$1" m t base
    m=$(stat -L -f %m "$f" 2>/dev/null)
    case "$m" in ''|*[!0-9]*) return 0 ;; esac
    base=$(readlink "$f" 2>/dev/null) || base="$f"
    base="${base##*/}"
    if [[ $base =~ _([0-9]{8}T[0-9]{6})\.txt$ ]]; then
        t=$(date -j -f '%Y%m%dT%H%M%S' "${BASH_REMATCH[1]}" '+%s' 2>/dev/null)
        case "$t" in ''|*[!0-9]*) ;; *) [ "$t" -lt "$m" ] && m="$t" ;; esac
    fi
    printf '%s' "$m"
}

# When the resurrect snapshot this server's parked windows were rebuilt from
# was taken (epoch seconds), or nothing if that cannot be known.
#
# restore-state records the one it was run for, as the server option
# @stash_snapshot_ts (see do_restore_state); that is the answer whenever it
# exists. Without it — the restore hook did not run — it has to be inferred,
# because `last` keeps moving after the restore: every continuum save on this
# server repoints it at a snapshot OF this server. The one that was restored
# predates the server, so when `last` is newer than #{start_time}, the
# snapshot is taken to be the newest save that is not. That inference is
# fooled by a save resurrect-guard.sh REJECTED just before the restart (it is
# never deleted, and is newer than the good one `last` was reverted to),
# which is why the recorded value is preferred. A manual restore into a
# running server also lands in the inference and picks an older snapshot than
# it used — which only refuses more.
restored_snapshot_time() {
    local t
    t=$(tmux show -gqv @stash_snapshot_ts 2>/dev/null)
    case "$t" in ''|*[!0-9]*) inferred_snapshot_time ;; *) printf '%s' "$t" ;; esac
}
# The inference. Both of its tests ("is `last` of this server?" and "which
# file is the newest before the start?") go by the CAPTURE time (the name stamp, see snapshot_file_time),
# never by the raw mtime: resurrect-save.sh re-dates `last`'s target on an
# unchanged save (touch -c, the stale-save chip's clock), so the snapshot that
# was restored can carry an mtime after #{start_time} while its name still says
# it was captured before. By mtime that read as "a save of this server", and
# with nothing older on disk the inference came back empty — which turns the
# park-time filter off (F3). The fallback picks the newest file STAMPED at or
# before the start: stamps are fixed-width local time, so they compare as
# strings and no file needs a stat or a date(1) of its own.
inferred_snapshot_time() {
    local dir start t f g cut best=""
    dir=$(resurrect_dir)
    t=$(snapshot_file_time "$dir/last")
    case "$t" in ''|*[!0-9]*) return 0 ;; esac
    start=$(tmux display-message -p '#{start_time}' 2>/dev/null)
    case "$start" in ''|*[!0-9]*) start="" ;; esac
    if [ -n "$start" ] && [ "$t" -gt "$start" ]; then
        cut=$(date -r "$start" '+%Y%m%dT%H%M%S' 2>/dev/null)
        f=""
        if [ -n "$cut" ]; then
            for g in "$dir"/tmux_resurrect_*.txt; do
                [[ ${g##*/} =~ ^tmux_resurrect_([0-9]{8}T[0-9]{6})\.txt$ ]] || continue
                [[ ${BASH_REMATCH[1]} > $cut ]] && continue
                if [ -z "$best" ] || [[ ${BASH_REMATCH[1]} > $best ]]; then
                    best="${BASH_REMATCH[1]}"; f="$g"
                fi
            done
        fi
        t=""; [ -n "$f" ] && t=$(snapshot_file_time "$f")
    fi
    printf '%s' "$t"
}
STATE_FILE=""   # resolved per-call; the server this talks to decides it
state_file() { [ -n "$STATE_FILE" ] || STATE_FILE="$(resurrect_dir)/stash-state.tsv"; printf '%s' "$STATE_FILE"; }

# Does the window at <session>:<index> still look like the one a row describes?
# ONE definition, used by both the save-time merge and the restore-time match,
# so the two can never drift apart. Every recorded attribute must agree —
# index alone is not identity (renumber-windows reshuffles it), and neither
# name nor cwd alone discriminates (every claude window is named for the
# version string; most sessions sit in $HOME).
# Echoes the window id on a match; returns 1 otherwise.
#
# <icwd> (field 10) is the window's pane directory at save time, and stands in
# for <cwd> when there is no @stash_cwd — i.e. for every window parked WITHOUT
# suspending its agent (busy, unsent input, a workflow...), plus plain shells.
# Those rows used to have NO discriminator but the name, and the name of an
# auto-renamed window is not identity (see below), so they could not match.
#
# The index must be CORROBORATED by at least one of those: a directory, or a
# name somebody chose. A row with neither (written before field 10 existed,
# for a window whose name is auto-derived) does not match at all — that would
# be the bare index, and a renumber puts a different window in the slot.
#
# This is necessary, not sufficient: two parked windows in the same directory
# are indistinguishable here. Rows WITHOUT a session id get the stricter
# resolve_parked_rows on top, which refuses that ambiguity outright.
window_for_row() {
    local sess="$1" idx="$2" name="$3" cwd="$4" pidx="${5:-}" icwd="${6:-}" win pcwd proved=0
    if [ -z "$cwd" ] && [ -n "$icwd" ]; then
        # An identity-only path is not the agent's pane: accept any pane there.
        cwd="$icwd"; pidx=""
    fi
    tmux has-session -t "=$sess" 2>/dev/null || return 1
    win=$(tmux list-windows -t "=$sess" -F '#{window_index} #{window_id}' 2>/dev/null \
          | awk -v i="$idx" '$1==i{print $2}')
    [ -n "$win" ] || return 1
    if [ -n "$cwd" ]; then
        # Compare against the AGENT'S pane, not the window's active one.
        # `display-message -t <window-id>` resolves to whichever pane is active,
        # so a multi-pane window whose active pane was an editor elsewhere in
        # the tree failed its own cwd check and got orphaned on every single
        # restart — even though @stash_pane_idx, recorded precisely because the
        # agent may not be pane 0, was sitting right there unused.
        pcwd=""
        if [ -n "$pidx" ]; then
            pcwd=$(tmux list-panes -t "$win" -F '#{pane_index} #{pane_current_path}' 2>/dev/null \
                   | awk -v i="$pidx" '$1==i{ $1=""; sub(/^ /,""); print }')
        fi
        if [ -n "$pcwd" ]; then
            [ "$pcwd" = "$cwd" ] || return 1
        else
            # No recorded pane (or it is gone): accept if ANY pane sits there.
            tmux list-panes -t "$win" -F '#{pane_current_path}' 2>/dev/null | grep -qxF "$cwd" || return 1
        fi
        proved=1
    fi
    # The name counts only when somebody CHOSE it. With automatic-rename on
    # (the default here, format #{pane_current_command}) it is whatever the
    # foreground process is called at this instant — for a live agent the
    # claude VERSION STRING. That is not identity: it changes on every claude
    # update, and at restore time the pane is still a shell, or a claude the
    # assistant-restore pass started seconds ago that has not retitled itself
    # yet. Comparing it is how stash:4 (a parked agent that was never
    # suspended, so no cwd to fall back on) failed "no matching window" on
    # every restore from 2026-10-01 on, and lost its origin, label and park
    # time on the first one.
    if [ -n "$name" ]; then
        local ar_name ar
        ar_name=$(tmux display-message -p -t "$win" "#{automatic-rename}${SEP}#{window_name}" 2>/dev/null)
        ar="${ar_name%%"$SEP"*}"
        case "$ar" in
            1|on) : ;;
            *) [ "${ar_name#*"$SEP"}" = "$name" ] || return 1; proved=1 ;;
        esac
    fi
    [ "$proved" = 1 ] || return 1
    printf '%s' "$win"
}

# Line numbers of the session-id rows in <file> that a LATER park superseded.
#
# A slot can be named by two rows: a session id kept for a restore that had
# not happened (save_state keeps those until a server adopts the file), and a
# tab parked later that took the same slot in the same directory. Both match
# the restored window on index and cwd, and stamping the old id there would
# resume a dead conversation in the wrong tab and cost that tab its own origin
# and label. The slot belongs to the most recent park: a session row whose
# park time is older than another row's for the same slot AND directory (so
# the other row would match the very same window) is superseded. A newer row
# in another directory cannot match that window, so it does not count; a row
# with no park time is never superseded and never supersedes.
superseded_sid_rows() {
    awk -F"$SEP" '{ k[NR] = $1 FS $2 FS (($8 != "") ? $8 : $10); t[NR] = $9; s[NR] = $7
                    if ($9 ~ /^[0-9]+$/ && (!(k[NR] in m) || $9 + 0 > m[k[NR]] + 0)) m[k[NR]] = $9 }
                  END { for (i = 1; i <= NR; i++)
                            if (s[i] != "" && t[i] ~ /^[0-9]+$/ && m[k[i]] + 0 > t[i] + 0) print i }' "$1" 2>/dev/null
}

# Which window, if any, each parked-state row WITHOUT a session id belongs to.
# Prints "<line number>SEP<window id>" per row it can place; silence otherwise.
#
# These rows (origin, label, park time) get a stricter test than window_for_row
# alone, because nothing that survives a restart tells two parked windows in
# the same directory apart — window and pane ids, pids and activity times are
# all new on the restored server, and the auto-renamed name is the process
# title. Index + directory therefore identifies a window only while the slots
# have not moved, and nothing can prove that: an unpark or a kill renumbers the
# holding session (A at 1 goes, B slides from 2 into 1). If A and B both sat in
# $HOME, "stash:1 in $HOME" is B now, and A's origin and label would be
# stamped on it. Mislabelling a tab is worse than losing its label (a parked
# tab without an origin still comes back, to the attached session), so a row
# is placed only when it is UNAMBIGUOUS:
#   - window_for_row matches (same index, and a directory or chosen name agrees);
#   - no other id-less row in the file has the same key (directory, else name);
#   - the window carries no stash state yet (else something already owns it);
#   - it is not the window a session-id row identifies;
#   - no OTHER unclaimed, state-less parked window shares that key — if
#     another one could equally be "the window in $HOME", neither is placed;
#   - the row's park time PREDATES the resurrect snapshot the parked windows
#     were rebuilt from (restored_snapshot_time).
# That last one is not covered by the uniqueness tests, because a STALE
# snapshot can leave exactly one candidate. Continuum saves every 15 minutes;
# the sidecar is current to the second. Snapshot at T0 has X parked at stash:1
# in $HOME; then X is unparked and A (also in $HOME) parked, renumber puts A at
# stash:1, and the sidecar's slot-1 row is A's. Restart, T0 comes back:
# stash:1 holds X, with no state; A's key is unique and X is the only pool
# window — so without this test A's origin, label and park time land on X.
# A window parked after the snapshot cannot be in its stash at all.
# The time can only be compared when there is one: a row with no (or a
# non-numeric) park time — sidecars from before field 9, and windows restored
# from those — is NOT placed while a snapshot time is known, since nothing
# shows it predates the snapshot. With no `last` at all, nothing was restored
# from a snapshot and there is nothing to compare against; the other tests
# still apply.
# Rows WITH a session id keep window_for_row alone, as before: refusing those
# would orphan a conversation, and they carry a recorded cwd and pane.
#
# COST. This runs on every publish — N+1 times per range park, under the main
# lock — and the per-row work is ~4 tmux calls plus forks for EVERY sid row.
# Yet it can only ever place a row on a parked window with no stash state, and
# on a server that was not just restored there is none: park_one stamps
# @stash_origin and @stash_ts before every move. So that list is built first,
# in one tmux call, and an empty one ends it there.
resolve_parked_rows() {
    local f="$1" n=0 claimed=" " w key
    local sess idx name pidx origin label sid cwd ts icwd extra
    [ -f "$f" ] || return 0

    # The parked windows with no stash state — the only ones a row can land
    # on. A missing holding session fails the call and leaves this empty.
    local w_id w_state w_ar w_name bare=""
    while IFS="$SEP" read -r w_id w_state w_ar w_name; do
        [ -n "$w_id" ] && [ -z "$w_state" ] || continue
        bare="${bare}${w_id}${SEP}${w_ar}${SEP}${w_name}"$'\n'
    done < <(tmux list-windows -t "=$HOLD" -F "#{window_id}${SEP}#{@stash_origin}#{@stash_session}#{@stash_ts}${SEP}#{automatic-rename}${SEP}#{window_name}" 2>/dev/null)
    [ -n "$bare" ] || return 0

    # Windows the session-id rows lay claim to — except rows a later park
    # superseded, which restore-state will not place either, so their old
    # slot is free for the row that took it.
    local stale ln=0
    stale=$'\n'$(superseded_sid_rows "$f")$'\n'
    while IFS="$SEP" read -r sess idx name pidx origin label sid cwd ts icwd extra; do
        ln=$((ln + 1))
        [ -n "$sid" ] || continue
        case "$stale" in *$'\n'"$ln"$'\n'*) continue ;; esac
        w=$(window_for_row "$sess" "$idx" "$name" "$cwd" "$pidx" "$icwd") && claimed="$claimed$w "
    done < "$f"

    # Keys of the id-less parked rows, to spot two rows describing lookalikes.
    local keys
    keys=$(awk -F"$SEP" -v hold="$HOLD" '$1==hold && $7=="" && ($5!="" || $6!="" || $9!="") {
               k = ($8 != "") ? "d:" $8 : (($10 != "") ? "d:" $10 : "n:" $3); print k }' "$f")

    # The parked windows nothing owns yet, with every key each could answer to.
    local pool="" p
    while IFS="$SEP" read -r w_id w_ar w_name; do
        [ -n "$w_id" ] || continue
        case "$claimed" in *" $w_id "*) continue ;; esac
        case "$w_ar" in 1|on) ;; *) pool="${pool}${w_id}${SEP}n:${w_name}"$'\n' ;; esac
        while IFS= read -r p; do
            pool="${pool}${w_id}${SEP}d:${p}"$'\n'
        done < <(tmux list-panes -t "$w_id" -F '#{pane_current_path}' 2>/dev/null)
    done <<< "$bare"

    local snap; snap=$(restored_snapshot_time)

    while IFS="$SEP" read -r sess idx name pidx origin label sid cwd ts icwd extra; do
        n=$((n + 1))
        [ "$sess" = "$HOLD" ] && [ -z "$sid" ] || continue
        [ -n "${origin}${label}${ts}" ] || continue
        if [ -n "$snap" ]; then
            # Strictly before: a park in the snapshot's own second may or may
            # not have made it in. (The capture runs a second or two before the
            # file's mtime; a park inside that gap is the residual.)
            case "$ts" in ''|*[!0-9]*) continue ;; esac
            [ "$ts" -lt "$snap" ] || continue
        fi
        if [ -n "$cwd" ]; then key="d:$cwd"; elif [ -n "$icwd" ]; then key="d:$icwd"; else key="n:$name"; fi
        [ "$(printf '%s\n' "$keys" | grep -cxF -- "$key")" -eq 1 ] || continue
        w=$(window_for_row "$sess" "$idx" "$name" "$cwd" "$pidx" "$icwd") || continue
        case "$claimed" in *" $w "*) continue ;; esac
        # In the pool means unclaimed AND state-less; anyone else there with
        # this key is a lookalike.
        printf '%s' "$pool" | awk -F"$SEP" -v w="$w" '$1==w{f=1} END{exit !f}' || continue
        printf '%s' "$pool" | awk -F"$SEP" -v w="$w" -v k="$key" '$1!=w && substr($0, length($1)+2)==k{f=1} END{exit f}' || continue
        printf '%s%s%s\n' "$n" "$SEP" "$w"
    done < "$f"
}

# SERIALISED, and the tmux snapshot is taken INSIDE the lock.
#
# This is a read-merge-write cycle — snapshot tmux, read the old file, decide
# which old rows to keep, rename a new file over the top — i.e. a textbook
# lost update, and it is reached from several genuinely concurrent unlocked
# callers: the `window-unlinked` hook fires one backgrounded `publish` per
# window MOVED (so a range park fires N+1 of them), and do_unstash_many fires
# one `resume` per window restored, each mirroring whenever its agent registers,
# up to 90s later.
#
# The specific loss, which a reviewer demonstrated and this comment previously
# denied: suspend_agent deliberately records the sessionId and mirrors it BEFORE
# killing the agent, so for that instant the id is on disk, in a window option,
# AND still listed by live_sessions. A concurrent writer whose tmux snapshot
# predates the option therefore reaches "still a live agent? then it needs no
# record" below, drops the row, and renames its copy over the top — moments
# before the kill makes that row the only pointer to the conversation. Nothing
# re-mirrors until the next park or unpark, which may be days.
#
# Taking the lock FIRST and snapshotting after makes each cycle atomic, so a
# writer either sees the option or predates the whole thing; either way the id
# survives. Failing to get the lock is safe to skip: whoever holds it is about
# to write, and their snapshot is newer than ours.
#
# NEVER JUDGE A LOSS ON A SERVER THAT HAS NOT ADOPTED THE SIDECAR.
#
# 2026-10-15 09:40:37 Mack's tmux server was killed. Between 09:40:38 and :42
# backgrounded `publish`es (the window-unlinked hook) merged the sidecar
# against a server with no `stash` session and no live claude for any of the
# four suspended sessions, logged "has no window any more — moved to
# stash-orphans.tsv" for all four, found nothing left to write and DELETED
# stash-state.tsv. At 09:41:06 the post-restore restore-state found no file,
# and all four stash windows came back "no recorded origin".
#
# The obvious story — the dying server answered with half its windows torn
# down — is not what tmux does, and was tested (tmux 3.7c, a -L test server
# with this hook and attached clients): on kill-server or SIGTERM it destroys
# every session in one go, runs NONE of the queued window-unlinked hooks, and
# SIGTERMs run-shell jobs still in flight (the "broke stale lock (owner dead,
# age 0s)" lines at 09:40:38 are those victims). A job that outlives it gets
# "no server running", which the snapshot below already treats as "keep what
# is on disk". So the publishes that did the damage were answered by a
# DIFFERENT, healthy server: the pty-mcp shells of the killed server all exit
# at 09:40:37 and a second batch with fresh pane ids (%1, %2, %5, %15) at
# 09:40:51 — a server that lived in between, started with this config and so
# with this hook, never restored. To it the parked windows really did not
# exist. Nothing was half-read; the server was the wrong one.
#
# ADOPTION. The sidecar describes the server that last owned it, and only a
# server that has ADOPTED it may call one of its rows lost — move a session id
# to the orphans file, or drop a parked row's origin/label/park time because
# no window answers to it. A server has adopted the file when:
#   a. restore-state ran on it (it sets @stash_adopted to #{start_time}): the
#      rows were reconciled against this server's windows; or
#   b. the file had no rows when this server came to write it: there was
#      nothing on disk to misjudge; or
#   c. stash-state.tsv.owner names this server (#{pid}/#{start_time}). Every
#      save of an adopted server writes it; or
#   d. there is no .owner at all — a sidecar from before adoption existed —
#      and the file was last written after this server started. An
#      unadopted server that writes the file first creates a .owner of
#      "none", so its own writes can never pass this test later.
# The first to apply sets @stash_adopted, which rides in the list-windows call
# save_state already makes: an adopted server pays nothing per publish.
#
# Before adoption every old row the merge cannot account for is KEPT and the
# live rows are mirrored on top: the server can only add. That covers the
# incident's server, a server mid-restore (resurrect builds the windows before
# restore-state puts the options back), and the same damage by another route
# — a tab parked on a restarted server before anyone restored, after which an
# "is anything parked here?" test would have judged every old row against
# windows that were never brought back. `stash.sh list` shows what is kept as
# awaiting a restore. After adoption losses orphan as they always did: a
# suspended window closed by hand, or a resumed agent that came back under a
# new session id, leaves no stale id behind to be stamped on another tab.
#
# Also, after adoption:
#   - $TMUX names a server pid that is not the one answering: this process
#     was started by a server that has since died, and is talking to its
#     replacement. Nothing is judged; the disk stays as it is.
#   - @kill_guard_shutdown holds a fresh epoch stamp (the kill guard sets it
#     to `date +%s` just before a deliberate kill-server, then runs its own
#     synchronous `publish`; stale or garbage stamps are ignored): the merge
#     keeps rows as if unadopted — that publish is the final mirror — except
#     that a parked row whose slot or key a live parked window now holds is
#     superseded and goes; and the file is never deleted.
#   - Otherwise an orphaning is believed only if it survives SAVE_SETTLE
#     seconds and a re-read of the SAME server (#{pid} and #{start_time}
#     both unchanged): a teardown in progress, a server swap or a tmux that
#     stops answering in that window all abort with the disk untouched.
#     That covers kills which bypass the guard. Dropped parked rows without
#     a session id do not pay it: an unpark drops one every time.
# A tmux that cannot report #{start_time} cannot tell one server from the next
# at all, and gets the merge as it was before adoption existed (in practice:
# the unit tests' fake tmux).
SAVE_SETTLE=1.5
GUARD_FRESH=120       # seconds a @kill_guard_shutdown stamp stays in force

# --- only the DEFAULT server writes the sidecar and the orphans file ---------
# Every server that sources tmux.conf gets the window-unlinked publish hook and
# reads the real @resurrect-dir, so a spare `tmux -L agents` (up since Oct 13,
# real config loaded) mirrored ITS windows over Mack's sidecar on every window
# close: the incident's damage, from a server that is not even the one that
# gets restored. Both checks required, each against the same test:
#   * TMUX="<socket>,<pid>,<session>": jobs inherit it from the server that
#     ran them. Empty means the CLI targets the default server;
#   * #{socket_path}: whatever server the tmux CLI really talks to.
# "The default server" is the EXACT path /tmp/tmux-$UID/default (as tmux
# reports it, through realpath: /private/tmp/...), never "any socket named
# default": every agent's private server under its own TMUX_TMPDIR is
# <dir>/tmux-$UID/default too, and one that loads tmux.conf would otherwise
# pass and write the real sidecar. A string compare — nothing to stat.
# STASH_ALLOW_SOCKET=<exact socket path> admits one other server: the test
# harness's opt-in for its private servers. A server that merely sources
# tmux.conf can never match it by accident.
# Any other server may still park and unpark (window options live on the
# server), but writes neither file and logs nothing; that is not news.
# A #{socket_path} that comes back EMPTY from a call that SUCCEEDED is a tmux
# that cannot render it (only the unit tests' fake does that) and is let
# through, like a missing #{start_time} in save_state; a call that failed
# refuses.
socket_ok() {
    case "$1" in "/private/tmp/tmux-${UID}/default"|"/tmp/tmux-${UID}/default") return 0 ;; esac
    [ -n "${STASH_ALLOW_SOCKET:-}" ] && [ "$1" = "$STASH_ALLOW_SOCKET" ]
}
tmux_env_socket_ok() { local t="${TMUX:-}"; t="${t%%,*}"; [ -z "$t" ] || socket_ok "$t"; }
sidecar_socket_ok() { tmux_env_socket_ok && { [ -z "$1" ] || socket_ok "$1"; }; }
# For a writer with no list-windows call of its own to ride on: one call.
# list-windows, not `display-message -p`, which with no current client or
# target needs a session to resolve; every window reports the same socket.
server_writes_sidecar() {
    tmux_env_socket_ok || return 1
    local sp; sp=$(tmux list-windows -a -F '#{socket_path}' 2>/dev/null) || return 1
    sidecar_socket_ok "${sp%%$'\n'*}"
}

# The tail of a sidecar row from field 10 on — icwd, frozen jobs, wake time —
# written only as far as the last field that is set (older readers see the
# 10- or 11-field rows they always did). In ROW_TAIL, not on stdout: this
# runs per row inside the merge, and $(…) would fork for each.
ROW_TAIL=""
row_tail() {   # <icwd> <frozen> <wake>
    if [ -n "${3:-}" ]; then ROW_TAIL="${SEP}${1}${SEP}${2}${SEP}${3}"
    elif [ -n "${2:-}" ]; then ROW_TAIL="${SEP}${1}${SEP}${2}"
    else ROW_TAIL="${1:+${SEP}${1}}"; fi
}

save_state() {
    local sf tmp rows live raw
    # Not this server's file to write (see socket_ok): out before the lock, so
    # a foreign server's publishes do not even contend for it. The
    # #{socket_path} half rides on the snapshot below.
    tmux_env_socket_ok || return 0
    sf=$(state_file)

    lock_acquire "$SAVE_LOCKDIR" || { log "sidecar busy — skipped this mirror"; return 0; }

    local of; of=$(orphan_file)
    local pass=1 first_ident="" s_pid s_start s_adopt s_sock guard adopted keep fresh_sids fresh_keys
    local notes orphan_rows n_orph n_kept lost_idless line key own m row
    local o_sess o_idx o_name o_pidx o_origin o_label o_sid o_cwd o_ts o_icwd o_frozen o_wake o_extra o_tail
    while :; do
        # Every window that matters, not just the parked ones. A window can
        # leave the holding session still carrying @stash_session —
        # resume_agent declines when the pane is busy and tells you to try
        # again — and the old version only mirrored windows inside HOLD, so
        # that row was dropped at exactly the moment the window option became
        # the sole surviving pointer.
        #
        # Field 9, @stash_ts (when it was parked), is APPENDED rather than
        # slotted in beside the others: a sidecar written before it existed has
        # 8 fields, and every reader below reads it into its own trailing
        # variable, so an old row simply yields an empty ts and restores
        # everything else as before. Without it a restore brought parked tabs
        # back with no park time, and the roster showed a blank age for every
        # one of them.
        #
        # Field 10, the pane directory, is appended the same way and for the
        # same reason, for EVERY window. It is what corroborates the index when
        # there is no @stash_cwd — a window parked without suspending its
        # agent, or a plain shell — because window_for_row no longer accepts an
        # auto-renamed name as identity (see there), and resurrect can restore
        # a hand-named window as auto-renamed. It is identity only, never
        # re-applied as an option: @stash_cwd means "a suspended agent resumes
        # here", and stamping it on a window whose agent was never suspended
        # would lie to every reader of it.
        #
        # Field 11, @stash_frozen (the parked window's frozen jobs: a JSON
        # array, so it holds no \x1f or newline), is appended the same way,
        # and only on rows that have one: a row without it is written with 10
        # fields exactly as before. restore-state re-applies it and marks the
        # window @stash_cold — after a restart the processes are gone, and
        # the unpark re-runs the recorded commands instead of typing `fg`.
        #
        # Field 12, @stash_wake (a snoozed window's wake epoch), likewise:
        # written only when set, with fields 10 and 11 in place (even empty)
        # in front of it; restore-state puts it back, so a restart keeps the
        # alarm (row_tail builds that tail for rows carried forward).
        #
        # The five leading fields are not row data: the answering server's
        # #{pid} and #{start_time} (its identity, for adoption, the settle
        # and the $TMUX test in the header), @kill_guard_shutdown,
        # @stash_adopted and #{socket_path} (default server only). Carried on
        # every line of the call this already made, so they cost no tmux
        # call; the filter below strips them before anything is written.
        raw=$(tmux list-windows -a -F \
            "#{pid}${SEP}#{start_time}${SEP}#{@kill_guard_shutdown}${SEP}#{@stash_adopted}${SEP}#{socket_path}${SEP}#{session_name}${SEP}#{window_index}${SEP}#{window_name}${SEP}#{@stash_pane_idx}${SEP}#{@stash_origin}${SEP}#{@stash_label}${SEP}#{@stash_session}${SEP}#{@stash_cwd}${SEP}#{@stash_ts}${SEP}#{pane_current_path}${SEP}#{@stash_frozen}${SEP}#{@stash_wake}" \
            2>/dev/null) || { lock_release "$SAVE_LOCKDIR"; return 0; }   # tmux unreachable: keep what is on disk
        s_pid=""; s_start=""; s_adopt=""; s_sock=""
        IFS="$SEP" read -r s_pid s_start _ s_adopt s_sock _ <<< "$raw"
        # Not the default server (socket_ok), or no window to say which server
        # this is: nothing is written and nothing logged.
        if [ -z "$raw" ] || ! sidecar_socket_ok "$s_sock"; then
            lock_release "$SAVE_LOCKDIR"; return 0
        fi

        # Adopted? (see the header). The flag answers for free; the rest is
        # paid once per server, by the save that establishes it — a .owner
        # read, at most one stat and the one set-option that records it.
        # An answer with no windows at all is never trusted to be the owner.
        adopted=""
        if [ -z "$raw" ]; then
            :
        else
            case "$s_start" in
                ''|*[!0-9]*) adopted=1 ;;          # cannot tell servers apart
                *) [ "$s_adopt" = "$s_start" ] && adopted=1 ;;
            esac
            if [ -z "$adopted" ]; then
                if [ ! -s "$sf" ]; then
                    adopted=1
                elif [ -f "$sf.owner" ]; then
                    own=""; IFS= read -r own < "$sf.owner"
                    [ "$own" = "${s_pid}/${s_start}" ] && adopted=1
                else
                    m=$(stat -f %m "$sf" 2>/dev/null)
                    # Strictly AFTER: a file stamped in the server's own first
                    # second may be the predecessor's last write.
                    case "$m" in ''|*[!0-9]*) ;; *) [ "$m" -gt "$s_start" ] && adopted=1 ;; esac
                fi
                [ -n "$adopted" ] && tmux set-option -g @stash_adopted "$s_start" 2>/dev/null
            fi
        fi
        # Only windows that carry something to re-apply. A parked window with
        # NO stash options used to be mirrored anyway, as a row of a bare index
        # and name: re-applying it can restore nothing, and since the name it
        # carried was an auto-renamed version string it never matched either —
        # so it logged "skipped stash:N — no matching window" on every restore,
        # forever, reporting a loss that had already happened. (pidx alone is
        # meaningless without the session id it locates.)
        # The same awk reports the guard flag through its exit status (3), so
        # it costs no fork either; any other awk failure also reads as "set",
        # which only makes this save more careful.
        #
        # The flag is the EPOCH the guard set it at (`set -g
        # @kill_guard_shutdown "$(date +%s)"`), and it counts only while it is
        # numeric and under GUARD_FRESH seconds old. A guard that dies between
        # setting it and the kill leaves it behind on a server that lives on,
        # and a bare "1" would then switch off orphaning on that server for
        # good. A few seconds in the future is tolerated (same clock, but the
        # set and this read race); garbage or anything older is "not set".
        # $EPOCHSECONDS is a bash 5 builtin — no fork; date(1) only on an
        # older bash.
        guard=0
        rows=$(printf '%s\n' "$raw" | awk -F"$SEP" -v hold="$HOLD" -v now="${EPOCHSECONDS:-$(date +%s)}" -v fresh="$GUARD_FRESH" '
            $3 ~ /^[0-9]+$/ && now - $3 < fresh && now - $3 >= -5 { g = 1 }
            { $0 = substr($0, length($1) + length($2) + length($3) + length($4) + length($5) + 6) }
            $7!="" || ($1==hold && ($5!="" || $6!="" || $8!="" || $9!="")) {
                # No field 11: the 10-field row older readers wrote.
                # Fields 11 and 12 only as far as the last one set.
                last = ($12 != "") ? 12 : (($11 != "") ? 11 : 10)
                s = $1; for (i = 2; i <= last; i++) s = s FS $i; print s }
            END { exit g ? 3 : 0 }') || guard=1
        # Rows the merge cannot account for are kept, not judged (header).
        keep=""; { [ -z "$adopted" ] || [ "$guard" = 1 ]; } && keep=1

        # MERGE, never a blind rebuild.
        #
        # This used to regenerate the file purely from live state, so any sid
        # the current server does not know about was simply not written back —
        # and an empty result deleted the file outright. That erased every
        # suspended conversation on the first park after a restart, because
        # window options do not survive a restart and the post-restore hook had
        # not repopulated them yet. Worse, on this machine the hook usually
        # never runs at all: continuum skips auto-restore whenever a second
        # tmux server exists, and there are always agent sockets around. So
        # "restore did not run" is the normal case and the first prefix+H was
        # destroying the file permanently.
        #
        # A sid in the old file that live state cannot account for is
        # therefore kept, not dropped: in the sidecar if its window still
        # plausibly exists and is simply awaiting restore, otherwise in the
        # orphans file where `stash.sh list` prints the command to resume it
        # by hand — but only by a server that has adopted the file (header);
        # before that it stays in the sidecar. Even then the merge only
        # COLLECTS what it would orphan; nothing is appended, logged or written
        # before the decision below.
        #
        # Session ids some window carries right now — from THIS snapshot,
        # before the merge below adds carried-forward rows that no window holds
        # yet.
        fresh_sids=$(printf '%s\n' "$rows" | awk -F"$SEP" '$7!=""{print $7}')
        notes=""; orphan_rows=""; n_orph=0; n_kept=0; lost_idless=0
        if [ -f "$sf" ]; then
            # ONCE, not per row: live_sessions reads every session file, and
            # this ran inside the loop — O(rows) interpreter startups while
            # holding a lock, which after a restart (many rows, no options yet)
            # is the slowest thing in the file and was itself widening the race
            # above.
            live=$(live_sessions)
            # Id-less parked rows whose window is unambiguously still here.
            local placed; placed=$(resolve_parked_rows "$sf")
            # What the live rows already say, as newline-framed records for a
            # fork-free `case` lookup: K = origin/label/park time (a parked
            # window still mirrored under its own row), L = the slot of every
            # window that carries stash state of its own, and for parked
            # windows D = their identity key, the same key resolve_parked_rows
            # uses (directory, else name).
            fresh_keys=$'\n'$(printf '%s\n' "$rows" | awk -F"$SEP" -v hold="$HOLD" '$1 != "" {
                print "K" FS $5 FS $6 FS $9
                print "L" FS $1 FS $2
                if ($1 == hold)
                    print "D" FS (($8 != "") ? "d:" $8 : (($10 != "") ? "d:" $10 : "n:" $3))
                }')$'\n'
            local o_n=0
            # o_extra soaks up any field a later format appends, so o_icwd
            # (and, for older rows, o_ts or o_cwd) never carries a stray
            # separator.
            while IFS="$SEP" read -r o_sess o_idx o_name o_pidx o_origin o_label o_sid o_cwd o_ts o_icwd o_frozen o_wake o_extra; do
                o_n=$((o_n + 1))
                # Fields 10-12 as a row carried forward writes them (row_tail).
                row_tail "$o_icwd" "$o_frozen" "$o_wake"; o_tail="$ROW_TAIL"
                if [ -z "$o_sid" ]; then
                    # A parked window's origin, label and park time. Not
                    # irreplaceable like a session id, but the same "options do
                    # not survive a restart" problem applies: a park after a
                    # restart and before restore-state rebuilt this file from
                    # live windows that had no options yet, and silently wiped
                    # every one of these rows — the tab then sat in the stash
                    # with no origin for good. Carry it while
                    # resolve_parked_rows can say WHICH window it is
                    # (state-less, unclaimed, no lookalike).
                    if printf '%s\n' "$placed" | awk -F"$SEP" -v n="$o_n" '$1==n{f=1} END{exit !f}'; then
                        rows="${rows}"$'\n'"${o_sess}${SEP}${o_idx}${SEP}${o_name}${SEP}${o_pidx}${SEP}${o_origin}${SEP}${o_label}${SEP}${SEP}${o_cwd}${SEP}${o_ts}${o_tail}"
                        continue
                    fi
                    # Otherwise it is dropped, which is NOT a loss when it
                    # carried nothing, or when its window is still parked and
                    # mirrored by its own live row.
                    [ -n "${o_origin}${o_label}${o_ts}" ] || continue
                    case "$fresh_keys" in *$'\n'"K${SEP}${o_origin}${SEP}${o_label}${SEP}${o_ts}"$'\n'*) continue ;; esac
                    # Not judged here (unadopted, or the guard is up): it stays
                    # for restore-state, whose resolve_parked_rows refuses any
                    # ambiguity — except when a live parked window WITH state
                    # now holds its slot or its key (directory, else name).
                    # Then this row is a stale copy: on an unadopted server
                    # typically a tab unparked (or killed) since, whose row
                    # nothing on this server will ever account for, and a tab
                    # parked later from the same directory. Kept, the two rows
                    # share the key d:<dir>, resolve_parked_rows refuses both
                    # at the next restore, and the LIVE tab loses its origin
                    # and label for good.
                    #
                    # Dropping the stale row, rather than teaching the restore
                    # to prefer the newest park among duplicate keys: the
                    # newest row is not always the live one (park X, park Y,
                    # unpark Y — Y's stale row is the newest, and X's slot can
                    # be Y's old one after a renumber), so "newest wins" can
                    # put one tab's label on another, and this file's rule is
                    # that mislabelling a tab is worse than losing a label.
                    # Here the live window proves which row is current. The
                    # cost: a row from an earlier server, for a tab not yet
                    # restored, goes if a tab parked here from the same
                    # directory takes its key — a label, never a session id.
                    if [ -n "$keep" ]; then
                        if [ -n "$o_cwd" ]; then key="d:$o_cwd"; elif [ -n "$o_icwd" ]; then key="d:$o_icwd"; else key="n:$o_name"; fi
                        case "$fresh_keys" in
                            *$'\n'"L${SEP}${o_sess}${SEP}${o_idx}"$'\n'*|*$'\n'"D${SEP}${key}"$'\n'*) continue ;;
                        esac
                        rows="${rows}"$'\n'"${o_sess}${SEP}${o_idx}${SEP}${o_name}${SEP}${o_pidx}${SEP}${o_origin}${SEP}${o_label}${SEP}${SEP}${o_cwd}${SEP}${o_ts}${o_tail}"
                        n_kept=$((n_kept + 1))
                        continue
                    fi
                    # An unpark looks exactly like this and is not a loss, and
                    # a guess could put this tab's label on another — so it is
                    # dropped silently (counted only for the $TMUX test).
                    lost_idless=$((lost_idless + 1))
                    continue
                fi
                printf '%s\n' "$fresh_sids" | grep -qx "$o_sid" && continue   # already represented
                # Still a live agent? Then it is not suspended and needs no
                # record. Dropped even under the guard: the conversation is
                # running in a pane that resurrect saves and assistant-restore
                # resumes, and a row kept for it would let restore-state stamp
                # the same id on a parked window too — one conversation resumed
                # in two places.
                printf '%s\n' "$live" | grep -q "${SEP}${o_sid}${SEP}" && continue
                row="${o_sess}${SEP}${o_idx}${SEP}${o_name}${SEP}${o_pidx}${SEP}${o_origin}${SEP}${o_label}${SEP}${o_sid}${SEP}${o_cwd}${SEP}${o_ts}${o_tail}"
                # Identity, not "some window has that index": after a close,
                # renumber-windows slides a DIFFERENT window into the vacated
                # slot, and carrying the row forward there is exactly how a
                # suspended session's id ends up stamped on someone else. And
                # only onto a window with no stash state of its own (the slot
                # is not held by a live row): a tab parked busy in the same
                # directory, landing in a dead session's old slot, matches on
                # index and cwd alike — carried there, restore-state would
                # stamp the dead id on it and it would lose its own origin.
                # Nor onto a window with a live agent in it: that is the very
                # tab an unpark resumed, whose agent came back under a NEW id
                # (a compaction) — S1 is dead, and carrying it would re-stamp
                # it there on the next restore, forever. A window awaiting
                # restore-state holds a shell, not an agent. (That check
                # forks, so it runs only for a row that got this far.)
                local c_win=""
                if c_win=$(window_for_row "$o_sess" "$o_idx" "$o_name" "$o_cwd" "$o_pidx" "$o_icwd") \
                   && case "$fresh_keys" in *$'\n'"L${SEP}${o_sess}${SEP}${o_idx}"$'\n'*) false ;; *) true ;; esac \
                   && [ -z "$(agent_pid_in_window "$c_win")" ]; then
                    rows="${rows}"$'\n'"${row}"
                    notes="${notes}carried forward suspended session ${o_sid%%-*} ($o_sess:$o_idx) — window exists but has no options yet"$'\n'
                elif [ -n "$keep" ]; then
                    # Kept where it is, for restore-state to place (or orphan,
                    # by its own rules) once a server has adopted the file.
                    rows="${rows}"$'\n'"${row}"
                    n_kept=$((n_kept + 1))
                    [ -n "$adopted" ] && notes="${notes}kept suspended session ${o_sid%%-*} ($o_sess:$o_idx) in $(basename "$sf") although no window has it — @kill_guard_shutdown is set, so this server is going down and cannot judge it lost"$'\n'
                else
                    orphan_rows="${orphan_rows}${row}"$'\n'
                    n_orph=$((n_orph + 1))
                fi
            done < "$sf"
        fi

        # The header's after-adoption tests. Only a save that would LOSE
        # something gets past the first `if` (an unadopted or guarded one
        # never does); the common publish falls straight through.
        if [ "$n_orph" -gt 0 ] || [ "$lost_idless" -gt 0 ]; then
            local t_pid="${TMUX:-}"
            t_pid="${t_pid#*,}"; t_pid="${t_pid%%,*}"
            case "$t_pid" in
                ''|*[!0-9]*) ;;
                *)
                    if [ -n "$s_pid" ] && [ "$t_pid" != "$s_pid" ]; then
                        log "started by tmux server pid $t_pid but answered by pid $s_pid — that server is gone; not judging ${n_orph} suspended session(s) and ${lost_idless} parked row(s) lost, $(basename "$sf") left as it is"
                        lock_release "$SAVE_LOCKDIR"; return 0
                    fi ;;
            esac
        fi
        if [ "$pass" = 2 ] && [ "${s_pid}/${s_start}" != "$first_ident" ]; then
            log "tmux server changed while confirming (${first_ident} -> ${s_pid}/${s_start}); $(basename "$sf") left as it is"
            lock_release "$SAVE_LOCKDIR"; return 0
        fi
        if [ "$n_orph" -gt 0 ] && [ "$pass" = 1 ]; then
            # Test 4. Still holding the sidecar lock: the decision and the
            # write must be one cycle (see above).
            first_ident="${s_pid}/${s_start}"
            pass=2
            sleep "$SAVE_SETTLE"
            continue
        fi
        break
    done

    # Decided. Orphans first, sidecar second, so a failed write never leaves a
    # session in neither file: a row the orphans file would not take goes
    # back into the sidecar instead.
    if [ -n "$orphan_rows" ]; then
        mkdir -p "$(dirname "$of")" 2>/dev/null
        while IFS= read -r line; do
            [ -n "$line" ] || continue
            IFS="$SEP" read -r _ _ _ _ _ _ o_sid _ <<< "$line"
            # Append only if this sid is not already parked here. The file is
            # a hand-recovery list, and re-appending the same session on
            # every save turned one lost conversation into eight identical
            # rows — noise that makes `stash.sh list` look like a disaster.
            grep -q "${SEP}${o_sid}${SEP}" "$of" 2>/dev/null && continue
            # Braces: a failing `>>` is reported by the shell, not printf.
            if { printf '%s\n' "$line" >> "$of"; } 2>/dev/null; then
                log "suspended session ${o_sid%%-*} has no window any more — moved to $(basename "$of")"
            else
                rows="${rows}"$'\n'"${line}"
                log "suspended session ${o_sid%%-*} has no window any more, but $(basename "$of") could not be written — kept it in $(basename "$sf")"
            fi
        done <<< "$orphan_rows"
    fi
    while IFS= read -r line; do
        [ -n "$line" ] && log "$line"
    done <<< "$notes"
    # Once per server, not per publish: an unrestored server keeps these on
    # every save until restore-state runs, and each window close is a save.
    if [ -z "$adopted" ] && [ "$n_kept" -gt 0 ]; then
        own=""; [ -f "$sf.noted" ] && IFS= read -r own < "$sf.noted"
        if [ "$own" != "${s_pid}/${s_start}" ]; then
            log "this tmux server (pid $s_pid) has not adopted $(basename "$sf") — restore-state has not run on it — so $n_kept row(s) with no window here are kept for the restore, not judged lost; \`stash.sh list\` shows them"
            { printf '%s\n' "${s_pid}/${s_start}" > "$sf.noted"; } 2>/dev/null
        fi
    fi
    # The live rows went in first; with none, the kept ones start with a
    # newline that would become an empty first line on disk.
    while [[ $rows == $'\n'* ]]; do rows=${rows#$'\n'}; done

    if [ -z "$rows" ]; then
        # Never while the guard is up: a server about to die is the one
        # place an empty answer means nothing.
        if [ "$guard" = 1 ]; then
            lock_release "$SAVE_LOCKDIR"
            return 0
        fi
        rm -f "$sf" 2>/dev/null
        # Nothing on any window: this only folds repeats and merges pending rows.
        orphans_reconcile "$of"
        lock_release "$SAVE_LOCKDIR"
        return 0
    fi
    mkdir -p "$(dirname "$sf")" 2>/dev/null
    # Write-then-rename. `> "$sf"` truncates before the command runs, so a
    # single failed list-windows used to leave an EMPTY sidecar and silently
    # drop every suspended conversation's off-server copy at once.
    tmp="${sf}.$$"
    if printf '%s\n' "$rows" > "$tmp" 2>/dev/null && mv -f "$tmp" "$sf" 2>/dev/null; then
        # A session mirrored from a live window is not lost, whatever the
        # orphans file says from an earlier restore. Only after the mirror is
        # on disk, so a failed write never leaves a session in neither file.
        # Unquoted on purpose: one uuid a word.
        # shellcheck disable=SC2086
        orphans_reconcile "$of" $fresh_sids
        # Who wrote it (header, c and d): this server if it has adopted the
        # file; otherwise "none" the first time, so that a later stat cannot
        # mistake an unadopted server's own write for adoption. Rewritten only
        # when it changes. Not with a tmux that has no #{start_time}.
        case "$s_start" in
            ''|*[!0-9]*) ;;
            *)
                own=""; [ -f "$sf.owner" ] && IFS= read -r own < "$sf.owner"
                if [ -n "$adopted" ]; then m="${s_pid}/${s_start}"; else m="${own:-none}"; fi
                if [ "$own" != "$m" ]; then
                    { printf '%s\n' "$m" > "$sf.owner.$$"; } 2>/dev/null && mv -f "$sf.owner.$$" "$sf.owner" 2>/dev/null
                    rm -f "$sf.owner.$$" 2>/dev/null
                fi ;;
        esac
    fi
    rm -f "$tmp" 2>/dev/null
    lock_release "$SAVE_LOCKDIR"
}

# Rows that could not be safely matched are parked HERE rather than dropped.
# A row carrying a sessionId is the only surviving pointer to a conversation,
# and the previous version's "a mismatch just skips the row" was not the safe
# choice it claimed: the next save_state rebuilds the sidecar from live tmux
# state, so a skipped row was silently erased on the following park.
ORPHAN_FILE=""
orphan_file() { [ -n "$ORPHAN_FILE" ] || ORPHAN_FILE="$(resurrect_dir)/stash-orphans.tsv"; printf '%s' "$ORPHAN_FILE"; }

# Take sessions that are on a window again OUT of the orphans file, fold any
# repeated session id down to its first row, and merge in rows that were
# parked in pending files (orphans_append's no-lock path). Caller holds
# SAVE_LOCKDIR.
#
# Nothing used to remove an orphan once it was placed again — a restore that
# matched it, or a save that found it mirrored from a live window — so
# `stash.sh list` went on calling conversations "lost" that were sitting in
# the stash with their ids on them (aa3db67d, bcf8d86e and 4664ef27, three
# rows each, while @83/@84/@85 carried them).
#
# The arguments must be the ids carried in @stash_session by some window RIGHT
# NOW, read from tmux — never what the sidecar says or what a match intended.
# An orphan row is the last pointer to its conversation; it may only go when
# another pointer demonstrably exists. Rows with no session id are left alone.
#
# A session the user resumed by hand (`claude --resume <sid>` in some pane) is
# deliberately NOT a reason to drop its row, although `list` shows it as
# running again (running_claude_sids). That process's sessions file is a
# pointer only while it runs — claude deletes it on exit — and it carries no
# origin or label; quit that claude without parking it and the row is once
# more the only record tying the conversation to where it came from. Parking
# that tab puts the id in @stash_session, and THAT retires the row here.
#
# The rewrite is read -> temp -> rename, and nothing else may be lost to it.
# The lock should make it the only writer, but a lock can be broken out from
# under a holder that outlives LOCK_STALE, and then an append landing between
# the read and the rename would go to the inode being replaced. So:
#   - the file is hard-linked aside and its inode+size recorded BEFORE the
#     read, and read through that link;
#   - if the file is no longer that inode at that size when the temp is ready,
#     the rewrite is abandoned (the next save does it) — nothing is lost;
#   - after the rename, anything appended to the old inode in the instant
#     between that check and the rename is copied across from the link.
# Only when something changes; an emptied file stays, empty (`list` tests -s).
orphans_reconcile() {   # <orphans file> <sid>...
    local of="$1" tmp prev sig size gone s p rc pend=(); shift
    for p in "$of".pending.*; do
        [[ $p =~ \.pending\.[0-9]+$ ]] && [ -f "$p" ] && pend+=("$p")
    done
    if [ "${#pend[@]}" -eq 0 ]; then
        # The common case — the same real orphans, nothing to do — costs one
        # read and no rewrite machinery.
        [ -s "$of" ] || return 0
        awk -F"$SEP" -v live=" $* " '$7 != "" && (index(live, " " $7 " ") || ($7 in seen)) { f = 1; exit }
                                    { seen[$7] = 1 } END { exit !f }' "$of" 2>/dev/null || return 0
    fi
    [ -e "$of" ] || : >> "$of"
    tmp="${of}.reconcile.$$"; prev="${of}.prev.$$"
    ln -f "$of" "$prev" 2>/dev/null || return 0
    sig=$(stat -f '%i %z' "$prev" 2>/dev/null)
    : > "$tmp"
    gone=$(awk -F"$SEP" -v live=" $* " -v out="$tmp" '
            $7 != "" && index(live, " " $7 " ") { if (!($7 in g)) print $7; g[$7] = 1; d = 1; next }
            $7 != "" && ($7 in seen)            { d = 1; next }
            { if ($7 != "") seen[$7] = 1; print > out }
            END { close(out); exit !d }' "$prev" ${pend[@]+"${pend[@]}"} 2>/dev/null)
    rc=$?
    # awk: 0 = rows dropped, 1 = nothing dropped (worth committing only to
    # merge pending files), anything else = it FAILED (a read or write error
    # - e.g. a full disk, the very condition that makes pending files exist)
    # and $tmp may be truncated: never rename that over the file, and never
    # delete pending rows it did not merge.
    if { [ "$rc" = 0 ] || { [ "$rc" = 1 ] && [ "${#pend[@]}" -gt 0 ]; }; } && [ -n "$sig" ] \
       && [ "$(stat -f '%i %z' "$of" 2>/dev/null)" = "$sig" ] \
       && mv -f "$tmp" "$of" 2>/dev/null; then
        size=${sig#* }
        if [ "$(stat -f %z "$prev" 2>/dev/null || echo "$size")" -gt "$size" ]; then
            tail -c +"$((size + 1))" "$prev" >> "$of" 2>/dev/null
            log "rows appended during the orphans rewrite were carried over"
        fi
        [ "${#pend[@]}" -gt 0 ] && rm -f "${pend[@]}" 2>/dev/null
        for s in $gone; do log "suspended session ${s%%-*} is on a window again — removed from ${of##*/}"; done
    fi
    rm -f "$tmp" "$prev" 2>/dev/null
    return 0
}

# Append orphan rows, skipping any session id already recorded (or earlier in
# this batch): the file is a hand-recovery list, and a repeat of the same
# session reads in `stash.sh list` like another loss. <rows> is
# newline-separated sidecar rows; <skip> optional space-separated ids to leave
# out as well.
#
# With <locked> = 1 the caller holds SAVE_LOCKDIR and the rows go straight
# into the file. Without it they must NOT: a concurrent orphans_reconcile
# replaces the file by rename, and an append racing that is how a row ends up
# in an unlinked inode. They go, complete, into a pending file of their own —
# written aside, then renamed into view — which `list` reads and the next
# locked reconcile merges.
orphans_append() {   # <orphans file> <rows> [<skip>] [<locked>]
    local of="$1" rows="$2" skip="${3:-}" locked="${4:-1}" line sid have dest out=""
    [ -n "$rows" ] || return 0
    mkdir -p "$(dirname "$of")" 2>/dev/null
    have=" $skip $(cat "$of" "$of".pending.* 2>/dev/null | awk -F"$SEP" '$7 != "" { printf "%s ", $7 }')"
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        IFS="$SEP" read -r _ _ _ _ _ _ sid _ <<< "$line"
        if [ -n "$sid" ]; then
            case "$have" in *" $sid "*) continue ;; esac
            have="$have$sid "
        fi
        out="$out$line"$'\n'
    done <<< "$rows"
    [ -n "$out" ] || return 0
    if [ "$locked" = 1 ]; then
        printf '%s' "$out" >> "$of" 2>/dev/null
    else
        dest="${of}.pending.$$"
        { [ -f "$dest" ] && cat "$dest"; printf '%s' "$out"; } > "${dest}-w" 2>/dev/null \
            && mv -f "${dest}-w" "$dest" 2>/dev/null
        log "orphans file lock unavailable — $(printf '%s' "$out" | wc -l | tr -d ' ') row(s) kept in ${dest##*/} until the next save merges them"
    fi
}

# Drop specific session ids from the sidecar. The kill path only: everywhere
# else a sid that leaves live state must be PRESERVED — that is save_state's
# whole merge — but a kill is the user explicitly discarding the
# conversation, and a preserved row would resurface in the orphans file as
# clutter that outranks the user's decision. Rows with an empty sid are
# untouched: " $7 " for those is two spaces, and the drop list, built from
# single-space-joined uuids, never contains two in a row.
forget_sids() {
    local sf tmp
    sf=$(state_file)
    [ -f "$sf" ] && [ "$#" -gt 0 ] || return 0
    server_writes_sidecar || return 0      # the default server's file only
    lock_acquire "$SAVE_LOCKDIR" || { log "sidecar busy — could not forget discarded sids"; return 0; }
    tmp="${sf}.forget.$$"
    awk -F"$SEP" -v drop=" $* " 'index(drop, " " $7 " ") == 0' "$sf" > "$tmp" 2>/dev/null \
        && mv -f "$tmp" "$sf" 2>/dev/null
    rm -f "$tmp" 2>/dev/null
    lock_release "$SAVE_LOCKDIR"
}

# Re-attach the options to the windows resurrect just rebuilt.
#
# The window NAME is useless as a discriminator here, which the previous
# version's comment got wrong. automatic-rename is on with format
# #{pane_current_command}, and claude reports its own VERSION STRING as that —
# so every claude window on the machine is called the same thing (verified:
# both tabs read `2.1.241`). Keying on session+index+name therefore degraded to
# session+index for exactly the windows that matter, which is what the fix was
# supposed to stop.
#
# The recorded cwd is the real discriminator: resurrect restores a pane's
# working directory, and a suspended window's shell keeps the agent's. Identity
# has to survive ALL of: the index exists, the cwd matches, and the window is
# not already holding some other session id. Anything less unique is treated as
# unidentifiable and preserved rather than guessed at.
#
# A window parked WITHOUT a suspend (its agent was busy, so it is still running)
# has no recorded cwd; its pane directory (field 10) corroborates the index
# instead, and window_for_row no longer accepts an auto-renamed name. Rows
# without a session id are placed in a SECOND pass, through
# resolve_parked_rows, once the session-id rows have claimed their windows —
# a lookalike in the same directory makes such a row unplaceable rather than
# a guess.
apply_row() {   # <win> <origin> <label> <sid> <cwd> <pidx> <ts> [<frozen>] [<wake>]
    local win="$1"
    # Frozen jobs recorded before a restart: the processes died with the old
    # server and the pane is a plain shell now (resurrect-save-repair.py saves
    # stash panes with no command), so the unpark re-runs them — cold.
    if [ -n "${8:-}" ]; then
        tmux set-option -w -t "$win" @stash_frozen "$8" 2>/dev/null
        tmux set-option -w -t "$win" @stash_cold 1 2>/dev/null
    fi
    [ -n "$2" ] && tmux set-option -w -t "$win" @stash_origin   "$2" 2>/dev/null
    [ -n "$3" ] && tmux set-option -w -t "$win" @stash_label    "$3" 2>/dev/null
    [ -n "$4" ] && tmux set-option -w -t "$win" @stash_session  "$4" 2>/dev/null
    [ -n "$5" ] && tmux set-option -w -t "$win" @stash_cwd      "$5" 2>/dev/null
    [ -n "$6" ] && tmux set-option -w -t "$win" @stash_pane_idx "$6" 2>/dev/null
    # Epoch seconds or nothing: the roster and the picker do arithmetic on
    # it, and a hand-edited or torn row must not plant garbage there.
    case "$7" in ''|*[!0-9]*) ;; *) tmux set-option -w -t "$win" @stash_ts "$7" 2>/dev/null ;; esac
    # A snooze's alarm (field 12): an epoch, or nothing. RESTORED_WAKE tells
    # the caller to reschedule once it is done.
    case "${9:-}" in ''|*[!0-9]*) ;; *) tmux set-option -w -t "$win" @stash_wake "$9" 2>/dev/null; RESTORED_WAKE=1 ;; esac
    return 0
}
RESTORED_WAKE=""

do_restore_state() {
    # The sidecar and the orphans file are the DEFAULT server's (socket_ok):
    # another server that sourced tmux.conf runs this hook too, and would
    # stamp the real sidecar's ids on its own windows and write the real
    # orphans file. It gets nothing, silently.
    server_writes_sidecar || return 0
    local sf of; sf=$(state_file); of=$(orphan_file)
    local sess idx name pidx origin label sid cwd ts icwd frozen wake extra win existing kept="" tail

    # Which snapshot was just restored, for resolve_parked_rows (see
    # restored_snapshot_time). As resurrect's post-restore hook this runs with
    # `last` on exactly the file restore.sh read — the time inferred_snapshot_time
    # gives whenever `last` predates the server, which a restart's restore
    # always does. Recorded now, before a continuum save moves `last` on and
    # the inference has to guess (and can guess a guard-rejected save). If
    # `last` is already newer than the server — a manual restore into a
    # running one, or this run by hand later — the inference's older answer
    # stands, and so does any EARLIER value already recorded: an earlier time
    # only refuses more.
    local snap old
    snap=$(inferred_snapshot_time)
    old=$(tmux show -gqv @stash_snapshot_ts 2>/dev/null)
    case "$old" in ''|*[!0-9]*) ;; *) { [ -z "$snap" ] || [ "$old" -lt "$snap" ]; } && snap="$old" ;; esac
    [ -n "$snap" ] && tmux set-option -g @stash_snapshot_ts "$snap" 2>/dev/null

    # ts is field 9 and icwd field 10, each absent from sidecars written before
    # it was mirrored; such a row reads them as "" and restores the rest.
    if [ -f "$sf" ]; then
        # Pass 1: rows carrying a session id. Not one superseded by a later
        # park (superseded_sid_rows), and not onto a window that is already
        # somebody's (below).
        local stale n1=0
        stale=$'\n'$(superseded_sid_rows "$sf")$'\n'
        while IFS="$SEP" read -r sess idx name pidx origin label sid cwd ts icwd frozen wake extra; do
            n1=$((n1 + 1))
            [ -n "$sess" ] && [ -n "$idx" ] && [ -n "$sid" ] || continue

            local ok=1
            case "$stale" in *$'\n'"$n1"$'\n'*) ok=0 ;; esac
            [ "$ok" = 1 ] && { win=$(window_for_row "$sess" "$idx" "$name" "$cwd" "$pidx" "$icwd") || ok=0; }
            [ "$ok" = 1 ] && { [ -n "$win" ] || ok=0; }
            if [ "$ok" = 1 ]; then
                # Not onto a window that already carries stash state of its
                # own — another session id, or the origin/park time of a tab
                # parked on this server (a restore-state run by hand) — nor
                # one with a live agent in it: a restored window that awaits
                # its options holds a shell; one running an agent is that
                # agent's — UNLESS the agent is this very session:
                # assistant-restore runs first and resumes `claude --resume S`
                # wherever the snapshot caught S before its SIGTERM finished,
                # and that tab is exactly where S's row belongs. window_agent
                # is "pid SEP sid SEP cwd", and empty unless exactly one agent
                # is verified under the window's panes.
                existing=$(tmux display-message -p -t "$win" "#{@stash_session}${SEP}#{@stash_origin}#{@stash_ts}" 2>/dev/null)
                if [ -n "${existing%%"$SEP"*}" ]; then
                    [ "${existing%%"$SEP"*}" = "$sid" ] || ok=0
                elif [ -n "${existing#*"$SEP"}" ]; then
                    ok=0
                else
                    local w_agent; w_agent=$(window_agent "$win")
                    if [ -n "$w_agent" ]; then
                        w_agent="${w_agent#*"$SEP"}"
                        [ "${w_agent%%"$SEP"*}" = "$sid" ] || ok=0
                    fi
                fi
            fi
            if [ "$ok" != 1 ]; then
                # Keep the pointer somewhere durable, and say so loudly.
                row_tail "$icwd" "$frozen" "$wake"; tail="$ROW_TAIL"
                kept="${kept}${sess}${SEP}${idx}${SEP}${name}${SEP}${pidx}${SEP}${origin}${SEP}${label}${SEP}${sid}${SEP}${cwd}${SEP}${ts}${tail}"$'\n'
                log "could not place suspended session ${sid%%-*} ($sess:$idx) — kept in $(basename "$of"); \`stash.sh list\` shows how to resume it"
                continue
            fi
            apply_row "$win" "$origin" "$label" "$sid" "$cwd" "$pidx" "$ts" "$frozen" "$wake"
            log "restored $sess:$idx — suspended session ${sid%%-*}"
        done < "$sf"

        # Pass 2: parked state without a session id. Resolved all at once,
        # BEFORE any of it is applied, so one placement cannot change what
        # the next row sees.
        local placed n=0; placed=$(resolve_parked_rows "$sf")
        while IFS="$SEP" read -r sess idx name pidx origin label sid cwd ts icwd frozen wake extra; do
            n=$((n + 1))
            [ -n "$sess" ] && [ -n "$idx" ] && [ -z "$sid" ] || continue
            # Nothing to re-apply (a bare index and name, as older versions
            # wrote for every parked window): matching it can restore nothing,
            # and failing to match it is not a loss. Ignore it rather than log a
            # "skipped" on every restore for a row that never carried anything.
            [ -n "${origin}${label}${ts}" ] || continue
            win=$(printf '%s\n' "$placed" | awk -F"$SEP" -v n="$n" '$1==n{print $2; exit}')
            if [ -z "$win" ]; then
                # Already on its window (restore-state run with nothing lost,
                # or a tab parked since the restart) — nothing to report.
                win=$(window_for_row "$sess" "$idx" "$name" "$cwd" "$pidx" "$icwd") &&
                    [ "$(tmux display-message -p -t "$win" "#{@stash_origin}${SEP}#{@stash_label}${SEP}#{@stash_ts}" 2>/dev/null)" \
                      = "${origin}${SEP}${label}${SEP}${ts}" ] && continue
                # Say WHAT is lost: the next save drops a row it cannot place,
                # so this line is the last record of it.
                log "could not re-apply parked state to $sess:$idx (from ${origin:-?}${label:+, \"$label\"}) — no window can be told apart as its own${icwd:+ (in $icwd)}, or it was parked after the restored snapshot; not re-applied"
                continue
            fi
            apply_row "$win" "$origin" "$label" "" "$cwd" "$pidx" "$ts" "$frozen" "$wake"
            log "restored $sess:$idx${frozen:+ — its frozen jobs re-run on unpark (cold)}"
        done < "$sf"
    fi

    # The orphans file, under the sidecar's lock like every other writer of it
    # (save_state appends to it while holding that lock, and this used to
    # append unlocked and undeduplicated — a restore run twice doubled every
    # row). First drop whatever this restore put back on a window: the ids are
    # read back from tmux, not taken from what apply_row was asked to do, so an
    # orphan only goes when a window demonstrably carries it. Then append what
    # could not be placed, minus ids already recorded (and minus ids on a
    # window — a second sidecar row for a session another row just placed is
    # not a loss).
    #
    # The lock is WAITED for, with no cap: these rows may be the only pointer
    # to their conversations, and a held lock always ends (released, or broken
    # once stale). Only a lock that cannot be created at all gets past that,
    # and then the rows go to a pending file of their own rather than into
    # the orphans file unlocked — an unlocked append can land in the inode a
    # concurrent rewrite is replacing, and vanish (see orphans_append).
    if [ -n "$kept" ] || [ -s "$of" ]; then
        local locked=0 on_windows
        lock_acquire "$SAVE_LOCKDIR" wait && locked=1
        on_windows=$(tmux list-windows -a -F '#{@stash_session}' 2>/dev/null | grep -v '^$' | tr '\n' ' ')
        # shellcheck disable=SC2086
        [ "$locked" = 1 ] && orphans_reconcile "$of" $on_windows
        orphans_append "$of" "$kept" "$on_windows" "$locked"
        [ "$locked" = 1 ] && lock_release "$SAVE_LOCKDIR"
    fi

    # Every parked window should now have an origin: park_one stamps it before
    # the move. One without is either state lost to an earlier restore or a
    # window moved in by something other than this script. It still works
    # (unstash falls back to the attached session) but `list` shows from=?,
    # and nothing ever said so — the bare-row "skipped" above was a misleading
    # symptom of exactly this. Name it, once per restore.
    local w_idx w_id w_name
    while IFS="$SEP" read -r w_idx w_id w_name; do
        [ -n "$w_id" ] || continue
        [ -n "$(tmux show -wqv -t "$w_id" @stash_origin 2>/dev/null)" ] && continue
        log "parked window $HOLD:$w_idx ($w_id, ${w_name}) has no recorded origin — its parked state was lost or it was moved in by hand; prefix+h will take it to the attached session"
    done < <(hold_exists && tmux list-windows -t "=$HOLD" -F "#{window_index}${SEP}#{window_id}${SEP}#{window_name}" 2>/dev/null)

    # This server has now reconciled the sidecar against its own windows, so
    # it may judge losses from here on (save_state's ADOPTION). Keyed to
    # #{start_time} like everything else that must not outlive the server.
    local st; st=$(tmux display-message -p '#{start_time}' 2>/dev/null)
    case "$st" in ''|*[!0-9]*) ;; *) tmux set-option -g @stash_adopted "$st" 2>/dev/null ;; esac

    tmux set-option -g @stash_count "$(count)" 2>/dev/null
    # Alarms put back on restored windows: the snooze timer's next wake.
    [ -n "$RESTORED_WAKE" ] && reschedule_snooze
    return 0
}

# --- agent suspend / resume ---------------------------------------------------

# Live claude sessions, one per line, $SEP-delimited:
#   pid · sessionId · status · window · pane · cwd
# The session file's own "tmux" field is "session:@win.%pane", so the window id
# is authoritative — and it does not change when a window moves between
# sessions, which is exactly what parking does.
live_sessions() {
    # Bash builtins, NOT python. This is on the path between prefix+H and the
    # tab disappearing, and python3 startup alone is ~130ms on this machine
    # (measured; the parse itself is nothing). It is also forked by every
    # save_state and by the resume poll every 250ms for up to 90s. The regex
    # version reads the same records in ~1ms with byte-identical output.
    #
    # The session file is one flat JSON object per file, so a first-match
    # regex per field is exact. A missing field is empty, as before, and a
    # malformed file yields an empty record rather than aborting the loop —
    # the bug the python version once had was dropping every LATER file on
    # one bad one. Known limit: a cwd containing an escaped quote would be
    # truncated at it (none exist; JSON-escaping only matters for that).
    local f raw pid sid status t win pane cwd
    for f in "$SESS_DIR"/*.json; do
        [ -f "$f" ] || continue
        raw=$(<"$f") || continue
        pid=""; sid=""; status=""; t=""; cwd=""; win=""; pane=""
        [[ $raw =~ \"pid\":([0-9]+) ]] && pid="${BASH_REMATCH[1]}"
        [[ $raw =~ \"sessionId\":\"([^\"]*)\" ]] && sid="${BASH_REMATCH[1]}"
        [[ $raw =~ \"status\":\"([^\"]*)\" ]] && status="${BASH_REMATCH[1]}"
        [[ $raw =~ \"tmux\":\"([^\"]*)\" ]] && t="${BASH_REMATCH[1]}"
        [[ $raw =~ \"cwd\":\"([^\"]*)\" ]] && cwd="${BASH_REMATCH[1]}"
        # "main:@8.%8" -> win=@8 pane=%8; anything without both separators is
        # an unplaced session and reads as empty, exactly as the python did.
        if [[ $t == *:*.* ]]; then win="${t#*:}"; pane="${win#*.}"; win="${win%%.*}"; fi
        printf '%s\n' "${pid}${SEP}${sid}${SEP}${status}${SEP}${win}${SEP}${pane}${SEP}${cwd}"
    done
}

# Which of <sid>... a RUNNING claude process holds right now — an orphan the
# user resumed by hand (`claude --resume <sid>` typed in some pane) carries no
# @stash_session anywhere, so only its sessions file says it is back. One line
# per hit: sid · pid · "session:index" of the local window it runs in, or empty.
#
# Verified, not trusted: a crash leaves its sessions file behind, so the pid
# must be alive and — when the file records procStart — be the SAME process
# (ps lstart; claude writes it in UTC here, local time is accepted too), or a
# reused pid would pass a lost conversation off as running. A mismatch reads as
# "not running", the safe direction: the row then just stays listed as lost.
# The "tmux" field's window id is honoured only if that pid really is under a
# local pane (other servers number windows from @0 too; see pid_in_window).
# Bash builtins over the files, ps only for sessions that match — this runs
# for `stash.sh list`, never on the park path.
running_claude_sids() {   # <sid>...
    [ "$#" -gt 0 ] || return 0
    local want=" $* " f raw pid sid t start win where a b
    for f in "$SESS_DIR"/*.json; do
        [ -f "$f" ] || continue
        raw=$(<"$f") || continue
        pid=""; sid=""; t=""; start=""; where=""
        [[ $raw =~ \"pid\":([0-9]+) ]] && pid="${BASH_REMATCH[1]}"
        [[ $raw =~ \"sessionId\":\"([^\"]*)\" ]] && sid="${BASH_REMATCH[1]}"
        [ -n "$pid" ] && [ -n "$sid" ] || continue
        case "$want" in *" $sid "*) ;; *) continue ;; esac
        kill -0 "$pid" 2>/dev/null || continue
        if [[ $raw =~ \"procStart\":\"([^\"]*)\" ]]; then
            read -ra start <<< "${BASH_REMATCH[1]}"
            read -ra a <<< "$(LC_ALL=C TZ=UTC0 ps -o lstart= -p "$pid" 2>/dev/null)"
            read -ra b <<< "$(LC_ALL=C ps -o lstart= -p "$pid" 2>/dev/null)"
            [ "${a[*]-}" = "${start[*]-}" ] || [ "${b[*]-}" = "${start[*]-}" ] || continue
        fi
        [[ $raw =~ \"tmux\":\"([^\"]*)\" ]] && t="${BASH_REMATCH[1]}"
        if [[ $t == *:@*.* ]]; then
            win="${t#*:}"; win="${win%%.*}"
            pid_in_window "$pid" "$win" \
                && where=$(tmux display-message -p -t "$win" '#{session_name}:#{window_index}' 2>/dev/null)
        fi
        printf '%s\n' "${sid}${SEP}${pid}${SEP}${where}"
    done
}

# Is <pid> a descendant of any pane in <window>? This is the server-local,
# definitive answer to "does this session record belong to THIS window".
#
# ~/.claude/sessions/*.json is machine-global but records only a bare window id,
# and every tmux server numbers windows from @0 — so on this machine, where
# tmux-pty-mcp keeps 7-15 extra sockets alive, a record from another server
# routinely collides with a local window id. The visible symptom today is
# silent: the collision inflates the match count, suspend_agent says "window
# holds 2 agents - left running", and parking simply stops suspending anything
# with no hint that another socket is the reason.
# It also subsumes the stale-record and pid-reuse cases that `kill -0` plus
# `ps -o comm=` would wave through.
# The registered agent living in a window: "pid SEP sid SEP cwd", or nothing.
# Prints only when exactly ONE agent is there — two is ambiguous, and every
# caller treats ambiguity as "leave it alone".
window_agent() {
    local win="$1" pid sid status w pane cwd n=0 out=""
    while IFS="$SEP" read -r pid sid status w pane cwd; do
        [ "$w" = "$win" ] || continue
        pid_in_window "$pid" "$win" || continue
        n=$((n + 1)); out="${pid}${SEP}${sid}${SEP}${cwd}"
    done < <(live_sessions)
    [ "$n" -eq 1 ] && printf '%s' "$out"
    return 0
}
agent_pid_in_window() { local r; r=$(window_agent "$1"); printf '%s' "${r%%"$SEP"*}"; }

pid_in_window() {
    local pid="$1" win="$2" root p guard
    [ -n "$pid" ] && [ -n "$win" ] || return 1
    for root in $(tmux list-panes -t "$win" -F '#{pane_pid}' 2>/dev/null); do
        p="$pid"; guard=0
        while [ -n "$p" ] && [ "$p" != "0" ] && [ "$p" != "1" ] && [ "$guard" -lt 30 ]; do
            [ "$p" = "$root" ] && return 0
            p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
            guard=$((guard + 1))
        done
    done
    return 1
}

# The pid currently registered for a sessionId, or empty. Identity, not mere
# existence — see the poll in resume_agent.
sid_pid() {
    local sid="$1" pid rest
    [ -n "$sid" ] || return 0
    while IFS="$SEP" read -r pid rest; do
        case "$rest" in "$sid$SEP"*) printf '%s' "$pid"; return 0 ;; esac
    done < <(live_sessions)
    return 0
}

transcript_mtime() {
    local f
    f=$(find "$HOME/.claude/projects" "$HOME/.claude-abby/projects" -name "$1.jsonl" -maxdepth 2 2>/dev/null | head -1)
    [ -n "$f" ] && stat -f %m "$f" 2>/dev/null || echo 0
}

# Is a turn in flight? `status` is authoritative while fresh; the transcript is
# appended per message, so a very recent write means something is happening now
# whatever the field says. Stale-BUSY only over-refuses (safe); stale-IDLE is
# the dangerous direction, which the mtime check covers.
is_working() {
    local status="$1" sid="$2" pid="$3" win="${4:-}" mt tab
    [ "$status" = "busy" ] && return 0
    # claude holds a `caffeinate -i -t 300` child while it works. Its 300s timer
    # means it lingers ~5min past a turn, which made it a bad signal for an
    # idle-timer — but here it is exactly right: parking is explicit, and the
    # only cost of a stale caffeinate is leaving an agent running, the safe
    # direction. It is also the ONLY guard that catches a long silent tool call
    # (a build, a test suite, a subagent), which writes no transcript for
    # minutes and has no `status` field on a session that never reported one.
    if [ -n "$pid" ] && ps -eo ppid=,command= 2>/dev/null \
         | awk -v r="$pid" '$1==r && $2 ~ /(^|\/)caffeinate$/{f=1} END{exit !f}'; then
        return 0
    fi
    # The TAB'S OWN STATE settles it when it has an opinion, and the transcript
    # mtime below is only the fallback for when it does not.
    #
    # The mtime check is a proxy for "a turn is in flight", and it is a bad one
    # at the exact moment parking happens: a transcript write 2s before the
    # press reads identical whether the turn is starting or has just finished.
    # Measured — resuming a session and hiding it 19s later left it hidden AND
    # still running, because startup wrote the transcript 2s before the press
    # while `status` said idle and no caffeinate child existed. Hidden-but-alive
    # is the one outcome parking exists to prevent.
    #
    # Trusting the tab here is safe in a way that trusting it for the KILL
    # decision alone would not be, because it is not consulted alone: `busy`
    # above and the caffeinate child are both still checked first, and
    # caffeinate is the strong one — claude holds it for the whole turn and it
    # lingers ~300s after, so a genuinely in-flight turn is caught there even if
    # the watcher has died and frozen this option at a stale `idle`.
    if [ -n "$win" ]; then
        tab=$(tmux show -wqv -t "$win" @agent_state 2>/dev/null)
        case "$tab" in
            running)                    return 0 ;;   # definitely working
            idle|done|paused|needs-*|failed) return 1 ;;   # definitely not; skip the proxy
        esac
    fi

    mt=$(transcript_mtime "$sid")
    case "$mt" in ''|*[!0-9]*) mt=0 ;; esac
    [ "$mt" -gt 0 ] && [ $(( $(date +%s) - mt )) -lt "$ACTIVE_SECS" ]
}

# Background subagents and workflows — the "looks finished and is not" class.
# A backgrounded Workflow outlives the turn that started it; an Agent-tool
# subagent is in-process with no child pid, no caffeinate of its own, an
# ended parent turn and an empty composer, so every OTHER guard in this file
# reads "safe" while a twenty-minute review is still running (the guard that
# was missing on 2026-08-25). Both die with the process, so the kill path
# must see them even when the watcher is dead.
#
# The detection — resolve_session_bases (the sessions file's own id; the
# compaction-chain walk was removed 2026-10-07, see the lib's header),
# session_has_running_subagent (finished = the parent was TOLD the result)
# and session_has_running_workflow (live = runtime dir without its
# completion file) — is SHARED with agent-tab-watcher.sh via this lib, so
# there is exactly one implementation of those rules on the machine. This
# file used to carry its own private copies; they drifted (a one-level chain
# walk blind to a double compaction or a quiet middle link, and a
# transcript-shape finished-rule that a 101-transcript corpus later refuted
# — the text→tool_use gap runs to 86s, past the 30s the shape rule waited),
# which is why they are gone rather than kept as a second opinion.
#
# If the lib is missing, fail toward "still running": suspension quietly
# stops working, which costs memory — never a turn. The watcher's stub for
# the same situation fails the opposite way (no gear), which is ITS safe
# direction; the asymmetry is deliberate.
if ! . "$HOME/.config/tmux/scripts/agent-session-lib.sh" 2>/dev/null; then
    session_has_running_workflow() { log "agent-session-lib.sh missing — treating a possible workflow as live"; return 0; }
    session_has_running_subagent() { log "agent-session-lib.sh missing — treating a possible subagent as live"; return 0; }
fi

# Is this agent driving an app through cua-driver right now? Same reasoning as
# above: read the shim's own activity file rather than trusting @agent_cua,
# which the watcher may not be alive to set.
CUA_ACTIVITY="$HOME/Library/Application Support/CuaNotch/activity.json"
CUA_LIVE=60
has_live_cua() {
    local pid="$1"
    [ -n "$pid" ] && [ -f "$CUA_ACTIVITY" ] || return 1
    # Exit codes are three-valued: 0 driving, 1 not driving, 2 UNREADABLE.
    # The shim rewrites this file on every driver call, so a torn or malformed
    # read is likeliest exactly while the agent IS driving — and here the cost
    # of guessing "not driving" is SIGTERM on an agent mid-run, not a missing
    # glyph as it is in the watcher this was ported from.
    python3 - "$CUA_ACTIVITY" "$pid" "$CUA_LIVE" <<'PY' 2>/dev/null
import json, sys, time
try:
    sessions = json.load(open(sys.argv[1])).get("sessions", {}) or {}
except Exception:
    raise SystemExit(2)
try:
    pid, live, now = int(sys.argv[2]), float(sys.argv[3]), time.time()
    for d in sessions.values():
        if (isinstance(d, dict) and d.get("agent_pid")
                and int(d["agent_pid"]) == pid
                and now - float(d.get("ts") or 0) < live):
            raise SystemExit(0)
except SystemExit:
    raise
except Exception:
    raise SystemExit(2)
raise SystemExit(1)
PY
    local rc=$?
    if [ "$rc" = 2 ]; then
        log "cua activity file unreadable - assuming the agent is driving"
        return 0
    fi
    return "$rc"
}

# Text typed but not submitted lives only in the TUI's buffer and dies with the
# process. The composer is the band between the LAST TWO horizontal rules — not
# the text after the last one, which is the model/context status line and is
# never empty.
has_unsent_input() {
    # An empty target is NOT a no-op in tmux: `-t ''` resolves to the CURRENT
    # pane, so this would have inspected whatever the user was looking at
    # instead of the agent being parked. A session file whose "tmux" field
    # lacks the pane part produces exactly that.
    [ -n "${1:-}" ] || return 0
    local cap
    cap=$(tmux capture-pane -p -t "$1" 2>/dev/null) || return 0
    [ -n "$cap" ] || return 0
    # Fewer than two rules means the composer is not fully on screen (a tall
    # draft pushes the opening rule off, and dialogs replace the band entirely).
    # That is UNKNOWN, and unknown must mean "leave it running" — the previous
    # `exit 1` read it as "no draft" and killed the agent, so the longer the
    # unsent message the likelier it was destroyed.
    printf '%s\n' "$cap" | awk '
        { line[NR] = $0; if ($0 ~ /^─────/) { prev = last; last = NR } }
        END {
            if (!prev) exit 0        # composer not fully visible -> assume a draft
            for (i = prev + 1; i < last; i++) {
                s = line[i]
                gsub(/^[[:space:]]*❯[[:space:]]*/, "", s)
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", s)
                if (s != "") exit 0
            }
            exit 1
        }'
}

# The screen a suspended agent was showing, saved for the picker's preview.
# Captured BEFORE the kill — afterwards the pane holds a dead shell and the
# conversation's rendering is gone. The window is first resized to the size
# the preview pane will have (the popup is POPUP_*_PCT of the client, fzf
# splits it in half, borders come off both layers), so the TUI reflows to
# exactly the width the preview renders at; -e keeps the colours.
# resize-window pins window-size to manual, so it is unset again after the
# capture or the window would come home tiny and stay that way.
snapshot_pane() {
    local win="$1" pane="$2" sid="$3" c="" r="" cols lines
    [ -n "$win" ] && [ -n "$pane" ] && [ -n "$sid" ] || return 0
    read -r c r < <(tmux list-clients -F '#{client_width} #{client_height}' 2>/dev/null | head -1)
    case "$c" in ''|*[!0-9]*) c=0 ;; esac
    case "$r" in ''|*[!0-9]*) r=0 ;; esac
    if [ "$c" -gt 0 ] && [ "$r" -gt 0 ]; then
        # Popup border takes 2 each way; fzf gives the preview PREVIEW_PCT of
        # the height below the list and border-top another row. Undershoot
        # rather than clip: a missing column cuts through the composer's box
        # border, a one-column gap is invisible.
        cols=$(( c * POPUP_W_PCT / 100 - 3 ))
        lines=$(( (r * POPUP_H_PCT / 100 - 2) * PREVIEW_PCT / 100 - 2 ))
        if [ "$cols" -ge 20 ] && [ "$lines" -ge 5 ]; then
            # The redraw is asynchronous — give the TUI a beat to take the
            # SIGWINCH. This runs after the tab is already off the bar, so
            # the wait is out of sight like the rest of the suspend.
            tmux resize-window -t "$win" -x "$cols" -y "$lines" 2>/dev/null && sleep 0.6
        fi
    fi
    mkdir -p "$PREVIEW_DIR" 2>/dev/null
    tmux capture-pane -ep -t "$pane" > "$PREVIEW_DIR/$sid.ansi" 2>/dev/null
    tmux set-option -uw -t "$win" window-size 2>/dev/null
    # Snapshots whose cleanup path never ran (a crash, a kill from outside
    # this script) age out here rather than accumulating forever.
    find "$PREVIEW_DIR" -name '*.ansi' -mtime +30 -delete 2>/dev/null
}

# Best-effort: parking always succeeds, suspending is the bonus. Anything
# uncertain leaves the agent running inside the parked window, which costs
# memory but can never lose work.
suspend_agent() {
    local win="$1" n=0 pid sid status supd w pane cwd
    local a_pid="" a_sid="" a_pane="" a_cwd="" a_status=""
    while IFS="$SEP" read -r pid sid status w pane cwd; do
        [ "$w" = "$win" ] || continue
        # Window id alone is per-server and collides across sockets; require the
        # process to actually live in this window.
        pid_in_window "$pid" "$win" || { log "ignoring session record pid $pid — window id $w matches but the process is not in this window (another tmux server?)"; continue; }
        n=$((n + 1)); a_pid=$pid; a_sid=$sid; a_pane=$pane; a_cwd=$cwd; a_status=$status
    done < <(live_sessions)

    [ "$n" -eq 0 ] && return 0                      # no agent — nothing to do
    # More than one agent in a window is ambiguous to put back, so leave it be
    # rather than guess which pane each resume belongs in.
    [ "$n" -gt 1 ] && { log "window holds $n agents — left running"; return 0; }

    # The pid comes from a file claude wrote; a crash or SIGKILL leaves that
    # file behind, and pids get reused. Confirm it is alive AND still claude
    # before signalling it — otherwise SIGTERM goes to an unrelated process.
    if ! kill -0 "$a_pid" 2>/dev/null; then
        log "session record for pid $a_pid is stale (process gone) — nothing to suspend"; return 0
    fi
    # `*node*` was accepted too, which with a stale session file plus pid reuse
    # meant SIGTERM to an unrelated node process — a dev server, an MCP host.
    # claude reports comm=claude; the bare version-string form is allowed
    # because that is how it presents itself elsewhere in this system.
    case "$(ps -p "$a_pid" -o comm= 2>/dev/null)" in
        *claude*) : ;;
        [0-9]*.[0-9]*.[0-9]*) : ;;
        *) log "pid $a_pid is not claude (pid reuse?) — left alone"; return 0 ;;
    esac

    # Never overwrite a sessionId already recorded here: that value can be the
    # only pointer to a DIFFERENT conversation (a resume that was refused
    # leaves one behind), and replacing it loses that one silently.
    local existing; existing=$(tmux show -wqv -t "$win" @stash_session 2>/dev/null)
    if [ -n "$existing" ] && [ "$existing" != "$a_sid" ]; then
        log "window already holds session ${existing%%-*} — not suspending over it"; return 0
    fi

    is_working "$a_status" "$a_sid" "$a_pid" "$win" && { log "agent is mid-turn — left running"; return 0; }
    has_unsent_input "$a_pane"      && { log "unsent input in the composer — left running"; return 0; }
    # A backgrounded workflow outlives its turn and computer-use spans turns;
    # both would die with the process. Checked TWO ways each — the watcher's
    # flag, which is instant but goes stale if the daemon dies, and the
    # underlying files via the shared lib, which are authoritative but cost a
    # little more. Either saying "busy" is enough to leave the agent alone.
    # The lib takes the PID and reads the session file itself — still present,
    # since the kill has not happened yet — then follows the compaction chain,
    # so a post-compaction session is judged by the dirs its work actually
    # lives in. (@agent_workflow is one gear for workflows AND subagents, so
    # the first line's tab check covers both while the watcher is alive.)
    if [ -n "$(tmux show -wqv -t "$win" @agent_workflow 2>/dev/null)" ] || session_has_running_workflow "$a_pid"; then
        log "background work (workflow, subagent or task) still in flight — left running"; return 0
    fi
    if session_has_running_subagent "$a_pid"; then
        log "background subagent still running — left running"; return 0
    fi
    if [ -n "$(tmux show -wqv -t "$win" @agent_cua 2>/dev/null)" ] || has_live_cua "$a_pid"; then
        log "driving an app through cua — left running"; return 0
    fi

    # Recorded BEFORE the kill: claude deletes its own session file on exit, so
    # afterwards there is nothing left that knows the sessionId.
    tmux set-option -w -t "$win" @stash_session "$a_sid"
    tmux set-option -w -t "$win" @stash_cwd "$a_cwd"
    # Which PANE the agent was in. Without this, resume typed into whichever
    # pane happened to be first, which in a multi-pane window can be an editor
    # or REPL — C-u plus a command line straight into an unsaved buffer.
    tmux set-option -w -t "$win" @stash_pane_idx \
        "$(tmux display-message -p -t "$a_pane" '#{pane_index}' 2>/dev/null)"

    # Mirror immediately, BEFORE the kill wait: a server death during the wait
    # would otherwise lose the id that was just written to a volatile option.
    save_state

    # LAST-MOMENT RE-CHECK. do_stash releases the lock before calling this, so
    # between the guards above and here the user can have pressed prefix+h and
    # brought the window home — measured 0.3-0.6s from park to this point, and
    # the guard chain's `find` over ~/.claude/projects dominates it. Killing
    # then would SIGTERM an agent in a window that is focused and back on the
    # tab bar. Re-read the two facts that can have changed rather than
    # re-taking the lock: is it still parked, and is the composer still empty
    # (the earlier check is ~1s stale by now, and anything typed in that second
    # dies with the process).
    if [ "$(tmux display-message -p -t "$win" '#{session_name}' 2>/dev/null)" != "$HOLD" ]; then
        tmux set-option -uw -t "$win" @stash_session 2>/dev/null
        tmux set-option -uw -t "$win" @stash_cwd 2>/dev/null
        tmux set-option -uw -t "$win" @stash_pane_idx 2>/dev/null
        save_state
        log "window left the holding session before the kill — not suspending"
        return 0
    fi
    if has_unsent_input "$a_pane"; then
        tmux set-option -uw -t "$win" @stash_session 2>/dev/null
        tmux set-option -uw -t "$win" @stash_cwd 2>/dev/null
        tmux set-option -uw -t "$win" @stash_pane_idx 2>/dev/null
        save_state
        log "text appeared in the composer after the first check — left running"
        return 0
    fi
    # And is it STILL not working? The first is_working ran before
    # has_unsent_input, the workflow and subagent walks, the cua fork, three
    # set-options and a save_state — and in a range, before up to TERM_WAIT
    # seconds per window ahead of this one, so ~36s stale on the fourth tab. A
    # turn that started in that gap (a peer's message_agent paste, a queued
    # message dequeuing, a subagent-completion wake) was never re-examined.
    # `status` is the guard that actually holds through a live turn (measured
    # 240/240 samples; caffeinate is a CHAIN of processes with sub-second holes
    # between them), so re-read it fresh rather than trusting the copy.
    local now_status; now_status=$(live_sessions | awk -F"$SEP" -v p="$a_pid" '$1==p{print $3; exit}')
    if is_working "${now_status:-$a_status}" "$a_sid" "$a_pid" "$win"; then
        tmux set-option -uw -t "$win" @stash_session 2>/dev/null
        tmux set-option -uw -t "$win" @stash_cwd 2>/dev/null
        tmux set-option -uw -t "$win" @stash_pane_idx 2>/dev/null
        save_state
        log "a turn started after the first check — left running"
        return 0
    fi

    # The last thing before the signal, so the picker's preview shows the
    # conversation as it looked at the moment it was put away.
    snapshot_pane "$win" "$a_pane" "$a_sid"

    # ...and the snapshot is itself 0.6s+ (the resize settle), plus everything
    # since the re-check above — so that check was stale again by the time the
    # signal went (2026-10-22: prefix+H then prefix+h a second later SIGTERMed
    # claude in the tab just brought back, and its composer with it). The
    # FINAL check and the kill now happen under the main lock: do_unstash_many
    # moves windows only while holding it, so the window cannot leave the
    # holding session between this test and the signal. Only the test and the
    # kill are inside — not the snapshot, not the TERM_WAIT. Lock not to be had
    # = uncertain = left running, like every other doubt in here.
    local why=""
    if ! lock_acquire; then
        why="stash lock busy at the kill — left running"
    elif [ "$(tmux display-message -p -t "$win" '#{session_name}' 2>/dev/null)" != "$HOLD" ]; then
        why="window left the holding session during the snapshot — not suspending"
    elif has_unsent_input "$a_pane"; then
        why="text appeared in the composer during the snapshot — left running"
    fi
    if [ -n "$why" ]; then
        lock_release
        tmux set-option -uw -t "$win" @stash_session 2>/dev/null
        tmux set-option -uw -t "$win" @stash_cwd 2>/dev/null
        tmux set-option -uw -t "$win" @stash_pane_idx 2>/dev/null
        save_state
        rm -f "$PREVIEW_DIR/$a_sid.ansi" 2>/dev/null
        log "$why"
        return 0
    fi
    kill -TERM "$a_pid" 2>/dev/null
    lock_release
    local i=0
    while [ "$i" -lt "$TERM_WAIT" ] && kill -0 "$a_pid" 2>/dev/null; do sleep 1; i=$((i + 1)); done

    if kill -0 "$a_pid" 2>/dev/null; then
        # KEEP the record. The earlier version unset it here, reasoning that a
        # process still alive at the timeout had ignored the signal — but
        # SIGTERM has been delivered and cannot be recalled, and an agent with
        # several MCP children to reap can legitimately need longer than
        # TERM_WAIT. Unsetting meant: agent exits at T+15, deletes its own
        # session file, and the sole pointer to that conversation has already
        # been thrown away by the code whose comment claimed to be protecting
        # it. A window wrongly marked suspended is harmless and self-correcting
        # (resume_agent notices the pane is busy and says so); a killed agent
        # with no session id is not recoverable at all.
        log "agent $a_pid still exiting after ${TERM_WAIT}s — keeping its session id"
    fi
    return 0
}

resume_agent() {
    # pane/pane_pid are INITIALISED, not merely declared: `local pane` leaves it
    # unset, and under `set -u` the `[ -n "$pane" ]` fallback below then aborts
    # the whole resume — which is the path taken whenever @stash_pane_idx is
    # absent, i.e. every window restored from a sidecar written before it
    # existed, and every pending resume.
    local win="$1" sid cwd pane="" pane_pid=""
    sid=$(tmux show -wqv -t "$win" @stash_session 2>/dev/null)
    [ -n "$sid" ] || return 0
    cwd=$(tmux show -wqv -t "$win" @stash_cwd 2>/dev/null)
    local pidx; pidx=$(tmux show -wqv -t "$win" @stash_pane_idx 2>/dev/null)
    if [ -n "$pidx" ]; then
        pane=$(tmux list-panes -t "$win" -F '#{pane_index} #{pane_id}' 2>/dev/null \
               | awk -v i="$pidx" '$1==i{print $2}')
    fi
    [ -n "$pane" ] || pane=$(tmux list-panes -t "$win" -F '#{pane_id}' 2>/dev/null | head -1)
    [ -n "$pane" ] || return 1

    # Only type into a shell sitting at a prompt. The test is "the pane's shell
    # has no child", NOT a name match on #{pane_current_command}: claude sets
    # its process title to its own version string, so that field reads e.g.
    # `2.1.241` and keeps reading it briefly after the process is gone.
    pane_pid=$(tmux display-message -p -t "$pane" '#{pane_pid}' 2>/dev/null)
    if [ -z "$pane_pid" ] || pgrep -P "$pane_pid" >/dev/null 2>&1; then
        msg -k warn "pane is busy — agent not resumed (prefix+h again once it's at a prompt)"
        return 1
    fi

    # No `${cwd:-$HOME}` fallback: `claude --resume <id>` only finds a session
    # under the project dir it was recorded in, so guessing $HOME produces a
    # confusing "no such session" instead of an honest refusal.
    if [ -z "$cwd" ]; then
        msg -k warn "no working directory recorded — resume by hand: claude --resume $sid"
        return 1
    fi
    # Whoever owns this sid right now — normally nobody, but if we are racing a
    # still-exiting claude it is the CORPSE's pid. A SIGTERMed claude keeps its
    # ~/.claude/sessions/<pid>.json for seconds while it reaps MCP children, so
    # "the sid appears in live_sessions" was satisfied by the dying process and
    # the poll below declared success, cleared @stash_session, and mirrored the
    # erasure — then claude exited and deleted its own file, taking the last
    # copy of the pointer with it. A resumed agent is a NEW process, so require
    # the pid to differ.
    local before_pid; before_pid=$(sid_pid "$sid")

    # An `abby` session (Claude Code on abliteration.ai, ~/.local/bin/abby) has
    # to come back through abby: plain `claude --resume` would not find it
    # (abby keeps its conversations under ~/.claude-abby/projects) or would
    # continue it on Anthropic. One recorded before that split still sits in
    # ~/.claude/projects; its replies say "model":"abliterated-…".
    local launcher=claude f
    for f in "$HOME"/.claude-abby/projects/*/"$sid".jsonl; do
        [ -f "$f" ] && { launcher=abby; break; }
    done
    if [ "$launcher" = claude ]; then
        for f in "$HOME"/.claude/projects/*/"$sid".jsonl; do
            [ -f "$f" ] && grep -m1 -q '"model":"abliterated-model' "$f" \
                && { launcher=abby; break; }
        done
    fi
    if [ "$launcher" = abby ] && ! command -v abby >/dev/null 2>&1; then
        msg -k warn "abby session but abby isn't on PATH — not resumed (abby --resume $sid)"
        return 1
    fi

    local cmd
    printf -v cmd 'cd %q && %s --resume %q' "$cwd" "$launcher" "$sid"
    tmux send-keys -t "$pane" C-u
    tmux send-keys -t "$pane" "$cmd" Enter

    # Only drop the record once the session is actually back: it is the sole
    # remaining pointer to that conversation, and a resume can take a while on a
    # large transcript.
    # Check BEFORE sleeping, in short steps: the old form paid a flat 2s even
    # when the agent was already back.
    local waited=0
    while :; do
        # Process substitution, not a pipe: `grep -q` exits at the first match,
        # and with pipefail a SIGPIPE'd python makes the pipeline nonzero even
        # though the session WAS found — reporting failure for a live resume.
        # By WINDOW, not by sid. A resumed process does not reliably register
        # under the id it was resumed with: after a compaction it reports the
        # continued conversation's new id, so `sid_pid "$sid"` never matched, the
        # poll ran out its 90s, and the record stayed on the tab — where the next
        # park read "window already holds a session" and silently refused to
        # suspend it, forever. Any agent that appears in this window and is not
        # the one we killed IS the comeback, whatever id it reports.
        local now_pid; now_pid=$(agent_pid_in_window "$win")
        if [ -n "$now_pid" ] && [ "$now_pid" != "$before_pid" ]; then
            # Do not erase a record that is no longer ours: the window may have
            # been re-parked and re-suspended while we polled (we run unlocked,
            # in the background), in which case @stash_session now belongs to
            # that newer suspend.
            if [ "$(tmux show -wqv -t "$win" @stash_session 2>/dev/null)" != "$sid" ]; then
                log "resume of ${sid%%-*} finished but the window moved on — leaving its record alone"
                return 0
            fi
            tmux set-option -uw -t "$win" @stash_session 2>/dev/null
            tmux set-option -uw -t "$win" @stash_cwd 2>/dev/null
            tmux set-option -uw -t "$win" @stash_pane_idx 2>/dev/null
            save_state
            rm -f "$PREVIEW_DIR/$sid.ansi" 2>/dev/null
            return 0
        fi
        [ "$waited" -ge "$RESUME_WAIT" ] && break
        sleep 0.25; waited=$((waited + 1))
    done
    msg -k error "agent did not come back — its session id is still on the tab (stash.sh list)"
    return 1
}

# A session cannot be created empty, so it is born with a placeholder that is
# killed once the real windows are inside; the session then dies by itself when
# the last window leaves, so nothing idles in the background.
#
# `-P -F` is load-bearing: it reports the id of the window this call actually
# created. Taking "the first window in the stash session" instead meant that if
# new-session FAILED because the session already existed — which two concurrent
# `prefix+H` presses reliably produce, since the bind is run-shell -b and both
# see hold_exists as false — `boot` resolved to somebody else's ALREADY-PARKED
# window, and the kill below destroyed it, panes, scrollback, suspended agent
# and all. Reproduced during review.
#
# Reports the placeholder in BOOT_WIN (empty if it created nothing) so the
# caller can take it back out again if nothing ends up parked.
BOOT_WIN=""
ensure_hold() {
    BOOT_WIN=""
    hold_exists && return 0
    BOOT_WIN=$(tmux new-session -d -s "$HOLD" -n _bootstrap -P -F '#{window_id}' 2>/dev/null) || BOOT_WIN=""
    return 0
}

# Move one window into the holding session. Assumes the lock is held and the
# holding session already exists.
park_one() {
    local win="$1" sess="$2" label
    # Invariant, enforced here rather than trusted from callers: nothing inside
    # the holding session is ever flagged as selected. @stash_sel is a window
    # option, so it survives the move and would repaint the tab mauve when it
    # comes back — with no selection in progress to explain why.
    tmux set-option -uw -t "$win" @stash_sel 2>/dev/null
    # Origin travels with the window (see header). The label is captured too:
    # @agent_summary is maintained by the tab watcher and gets cleared once the
    # agent it describes is gone, so without this the picker would list a
    # suspended session as plain "zsh".
    tmux set-option -w -t "$win" @stash_origin "$sess"
    label=$(tmux show -wqv -t "$win" @agent_summary 2>/dev/null)
    [ -n "$label" ] && tmux set-option -w -t "$win" @stash_label "$label"
    # For the picker: when it was parked, and (written by `describe` shortly
    # after) what it was doing. A description from an earlier park is stale.
    tmux set-option -w -t "$win" @stash_ts "$(date +%s)"
    tmux set-option -uw -t "$win" @stash_desc 2>/dev/null
    # A plain park has no alarm; a snooze sets it once the park has worked.
    tmux set-option -uw -t "$win" @stash_wake 2>/dev/null

    tmux move-window -s "$win" -t "$HOLD": 2>/dev/null && return 0

    # A window still sitting on the tab bar must not be left claiming it was
    # parked: @stash_origin is what `list` and the restore hook read, and a
    # stale one describes a park that never happened.
    tmux set-option -uw -t "$win" @stash_origin 2>/dev/null
    tmux set-option -uw -t "$win" @stash_label 2>/dev/null
    return 1
}

# Is this window's agent working right now, per the TAB'S OWN STATE?
#
# Reads the options the tab watcher maintains — the same ones the tab bar
# renders — rather than re-deriving from session files. That is a deliberate
# exception to the kill path's rule (never trust a watcher option alone, it
# fails open if the watcher dies), and the difference is which way the
# failure points: this gates a REFUSAL, not a kill. A dead watcher frozen at
# `running` merely declines to hide the tab, which costs nothing; frozen at
# `idle` the tab parks and suspend_agent's own guards — status, the caffeinate
# child, the transcript — still stand between it and a SIGTERM. Nothing here can
# end a turn.
#
# `running` is the state; @agent_workflow and @agent_cua are the two "in flight
# but the chip cannot say so" cases from the same family. needs-input, failed
# and done are NOT running — those are precisely the tabs worth tidying away.
# Sets AGENT_RUNNING_WHY so refusals can be logged with a reason — a silent
# "still working" on a tab that looks done is undebuggable from the outside.
AGENT_RUNNING_WHY=""
agent_running() {
    local win="$1" rec pid sid cwd tab flagged=0
    AGENT_RUNNING_WHY=""
    tab=$(tmux show -wqv -t "$win" @agent_state 2>/dev/null)
    case "$tab" in
        ""|idle|done|paused|needs-*|failed) : ;;
        # `running`, and anything this script has never heard of. A state the
        # watcher grows later must read as "working" here — is_working treats
        # the unknown as unknown and falls back to its proxies, but a refusal
        # has nothing to fall back to, so it errs the harmless way.
        *) flagged=1 ;;
    esac
    [ -n "$(tmux show -wqv -t "$win" @agent_workflow 2>/dev/null)" ] && flagged=1
    [ -n "$(tmux show -wqv -t "$win" @agent_cua 2>/dev/null)" ] && flagged=1
    # The one escape hatch a tab-derived refusal needs: the watcher can die
    # (its own header says so) with `running` frozen on a window whose agent
    # has long since exited — and that window would then be unparkable forever,
    # with a message that is a lie. A flag only counts if an agent is actually
    # registered in the window; a bare shell under a stale flag parks normally.
    if [ "$flagged" = "1" ]; then
        if [ -n "$(window_agent "$win")" ]; then
            AGENT_RUNNING_WHY="tab state [${tab:-flag}] with a live agent registered"
            return 0
        fi
        return 1
    fi
    # No file-derived checks here any more — the tab settles it. They were
    # added when the tab was BLIND to background work (the watcher derived a
    # session's runtime dir from the pre-compaction id, so two in-flight
    # reviewers showed no gear and a park SIGTERMed them), and they cost real
    # things: pre-move latency the user sits through, and a lockout window
    # after a subagent finishes that the watcher's own finished-rule does not
    # have (it clears the gear when the parent is TOLD the result — the
    # authoritative signal; watcher commit c554ffa also follows the
    # compaction chain now). With the blindness fixed, duplicating the lookup
    # here buys nothing on the good days and five minutes of "won't hide" on
    # the staggered end of a review fleet.
    #
    # The failure this accepts: watcher dead or one tick behind at the moment
    # of the press → this says "not running". That lands on classify_window's
    # agent_guard, which keeps EVERY deep guard (status, caffeinate, the
    # lineage-aware workflow/subagent walks), and on pin_recheck right before
    # the close — so the outcome is a refusal with a logged reason, never a
    # killed turn.
    return 1
}

# --- classify, freeze, thaw ---------------------------------------------------
#
# "Nothing in the stash is running." Every tab a park reaches is classified
# BEFORE anything moves (classify_window): refused with a reason, closed into
# a pinned ⌘Z entry (a one-pane agent tab), or parked with its jobs frozen
# (freeze_window). The guards suspend_agent used to run after the move — and
# answer with "parked, but left running" — run here, up front, so a refusal
# leaves the tab exactly where it was. The slow part of the classification
# runs before the park takes its lock (do_stash_many); under the lock only
# cheap re-checks and the moves.

SHELL_NAMES=" zsh bash sh fish dash ksh tcsh csh nu "
REMOTE_NAMES=" ssh mosh mosh-client et autossh "
# A word that names an agent harness, as argv0 (or argv1 after an interpreter:
# `node …/codex`, the abby wrapper under bash). claude's process title is its
# version string, caught separately. It routes a tab to `closed-tabs.sh close
# --pin`, which does the real detection and the session lookup.
export STASH_AGENT_RE='^(claude|abby|codex|codex\.js|opencode|grok(-[a-z]+-[a-z0-9_]+)?|copilot|agy)$'
INTERPRETERS=" node bun deno python python3 bash zsh sh perl ruby "
FREEZE_TRIES=10       # 0.05s ticks for ⌃Z, then again for SIGSTOP, to take

# What is running on one pane's terminal, from ONE `ps -t` (the pane's tty:
# 2-3ms, against ~50ms for the whole machine). Lines, tab-separated:
#   gone                      the pane's process is not on its tty
#   root  shell|other  NAME   the pane's own process (a shell, or not)
#   agent NAME                the TAB's agent: the pane's own process, or a
#                             process the shell started for its foreground job
#                             (not an agent some other program runs — `make
#                             review` running `claude -p`, an agent under vim)
#   fg    PGID  PIDS  NAME    the foreground job, when the shell is not in the
#                             foreground: its process group, the pids the shell
#                             started for it (a pipeline's members, comma
#                             separated) and the first one's name
#   remote NAME               ssh / mosh / et among that job's processes
# The foreground is the tty's process group (tpgid), which a shell at its
# prompt holds itself — so "tpgid differs from the shell's pid" is the test.
pane_scan() {   # <pane pid> <pane tty>
    ps -t "${2#/dev/}" -o pid=,ppid=,pgid=,tpgid=,stat=,args= 2>/dev/null |
        awk -v OFS='\t' -v root="$1" -v shells="$SHELL_NAMES" -v remotes="$REMOTE_NAMES" -v interp="$INTERPRETERS" '
            function base(w) { sub(/^-/, "", w); sub(/.*\//, "", w); return w }
            BEGIN { are = ENVIRON["STASH_AGENT_RE"] }
            $1 ~ /^[0-9]+$/ {
                pid = $1; pp[pid] = $2; pg[pid] = $3
                if (pid == root) rtp = $4
                a = $0; sub(/^[ \t]*[0-9]+[ \t]+[0-9]+[ \t]+[0-9]+[ \t]+-?[0-9]+[ \t]+[^ \t]+[ \t]*/, "", a)
                n = split(a, w, /[ \t]+/); w0 = base(w[1]); w1 = (n > 1) ? base(w[2]) : ""
                nm[pid] = w0
                if (w0 ~ are) ag[pid] = w0
                else if (index(interp, " " w0 " ") && w1 ~ are) ag[pid] = w1
                else if (a ~ /^[0-9]+\.[0-9]+\.[0-9]+/) ag[pid] = "claude"
                o[++np] = pid
            }
            END {
                if (!(root in pp)) { print "gone"; exit }
                isshell = index(shells, " " nm[root] " ")
                print "root", (isshell ? "shell" : "other"), nm[root]
                agent = (!isshell && (root in ag)) ? ag[root] : ""
                fg = rtp ""
                if (fg + 0 > 0 && fg != root) {
                    mem = ""; rem = ""
                    for (i = 1; i <= np; i++) { p = o[i]
                        if (pg[p] == fg && pp[p] == root) mem = mem (mem == "" ? "" : ",") p }
                    if (mem == "" && (fg in pp)) mem = fg
                    split(mem, f, ",")
                    for (k in f) if (agent == "" && (f[k] in ag)) agent = ag[f[k]]
                    for (i = 1; i <= np; i++) { p = o[i]
                        if (pg[p] == fg && index(remotes, " " nm[p] " ")) { rem = nm[p]; break } }
                    print "fg", fg, mem, nm[f[1]]
                    if (rem != "") print "remote", rem
                }
                if (agent != "") print "agent", agent
            }'
}

# The deep guards for a window whose agent is a REGISTERED claude (its
# sessions file, verified to live in this window): exactly what
# suspend_agent checked after the move, now before anything happens.
# 0 = refuse (GUARD_WHY says why), 1 = no registered agent, 2 = agent, clear.
GUARD_WHY=""
agent_guard() {
    local win="$1" pid sid status w pane cwd n=0 a_pid="" a_sid="" a_status="" a_pane=""
    GUARD_WHY=""
    while IFS="$SEP" read -r pid sid status w pane cwd; do
        [ "$w" = "$win" ] || continue
        pid_in_window "$pid" "$win" || continue
        n=$((n + 1)); a_pid=$pid; a_sid=$sid; a_status=$status; a_pane=$pane
    done < <(live_sessions)
    [ "$n" -eq 0 ] && return 1
    if [ "$n" -gt 1 ]; then GUARD_WHY="that tab holds $n agents"; return 0; fi
    if is_working "$a_status" "$a_sid" "$a_pid" "$win"; then
        GUARD_WHY="that agent is still working"; return 0
    fi
    if has_unsent_input "$a_pane"; then
        GUARD_WHY="unsent text in the agent's composer"; return 0
    fi
    if [ -n "$(tmux show -wqv -t "$win" @agent_workflow 2>/dev/null)" ] || session_has_running_workflow "$a_pid"; then
        GUARD_WHY="background work (workflow or subagent) still in flight"; return 0
    fi
    if session_has_running_subagent "$a_pid"; then
        GUARD_WHY="a background subagent is still running"; return 0
    fi
    if [ -n "$(tmux show -wqv -t "$win" @agent_cua 2>/dev/null)" ] || has_live_cua "$a_pid"; then
        GUARD_WHY="it's driving an app through cua"; return 0
    fi
    return 2
}

# Any agent's tab, by the watcher's options: working, or a workflow or cua
# flag. For the harnesses agent_guard cannot see (codex, opencode, …), whose
# tab state agent_running ignores because no claude is registered there. Only
# consulted once the tab's agent was found.
tab_busy() {
    case "$(tmux show -wqv -t "$1" @agent_state 2>/dev/null)" in
        ""|idle|done|paused|needs-*|failed) ;;
        *) return 0 ;;
    esac
    [ -n "$(tmux show -wqv -t "$1" @agent_workflow 2>/dev/null)" ] && return 0
    [ -n "$(tmux show -wqv -t "$1" @agent_cua 2>/dev/null)" ] && return 0
    return 1
}

# The cheap guards, again, right before `close --pin` kills the agent: the
# classification ran before the lock (and before every earlier tab of a
# range). Every harness: its tab state. claude and abby (one TUI, so
# has_unsent_input reads both screens): the composer. A registered claude:
# its sessions file's `status`, read fresh. 0 = refuse (RECHECK_WHY).
RECHECK_WHY=""
pin_recheck() {   # <win> <pane> <agent>
    RECHECK_WHY=""
    if tab_busy "$1"; then RECHECK_WHY="that agent is still working"; return 0; fi
    case "$3" in
        claude|abby)
            if has_unsent_input "$2"; then RECHECK_WHY="unsent text in the agent's composer"; return 0; fi ;;
    esac
    if live_sessions | awk -F"$SEP" -v w="$1" '$4 == w && $3 == "busy" { f = 1 } END { exit !f }'; then
        RECHECK_WHY="that agent is still working"; return 0
    fi
    return 1
}

# What a park does with <window>. Sets CL_KIND:
#   refuse   leave it; CL_WHY says why
#   pin      a one-pane agent tab: `closed-tabs.sh close --pin` CL_PANE;
#            CL_AGENT names the harness
#   park     park it; freeze whatever runs under each pane's shell
# Agents count only as the tab's agent (pane_scan's `agent`). Anything else
# in the foreground is parked and frozen, never offered to `close --pin`: a
# close that found an agent somewhere under the pane would kill the pane.
CL_KIND=""
CL_WHY=""
CL_PANE=""
CL_AGENT=""
classify_window() {
    local win="$1" pane idx ppid tty dead n=0 k a b c
    local agent="" why="" noshell="" rkind rname members=" " g
    CL_KIND=refuse; CL_WHY=""; CL_PANE=""; CL_AGENT=""
    while IFS="$SEP" read -r pane idx ppid tty dead; do
        [ -n "$pane" ] || continue
        n=$((n + 1)); CL_PANE="$pane"
        [ "$dead" = 1 ] && continue
        case "$ppid" in ''|*[!0-9]*) continue ;; esac
        [ -n "$tty" ] || continue
        rkind=""; rname=""
        while IFS=$'\t' read -r k a b c; do
            case "$k" in
                root)   rkind="$a"; rname="$b"; [ "$a" = other ] && members="$members$ppid " ;;
                agent)  [ -n "$agent" ] || agent="$a" ;;
                fg)     members="$members${b//,/ } " ;;
                remote) [ -n "$why" ] || why="$a in the foreground" ;;
            esac
        done < <(pane_scan "$ppid" "$tty")
        [ "$rkind" = other ] && [ -z "$noshell" ] && noshell="${rname:-its program} runs with no shell — can't freeze it"
    done < <(tmux list-panes -t "$win" -F "#{pane_id}${SEP}#{pane_index}${SEP}#{pane_pid}${SEP}#{pane_tty}${SEP}#{pane_dead}" 2>/dev/null)
    if [ "$n" -eq 0 ]; then CL_WHY="it's gone"; return 0; fi
    # A registered claude counts only as the tab's own agent (its process is
    # the foreground job's), for the same reason.
    if [ -z "$agent" ]; then
        g=$(window_agent "$win"); g="${g%%"$SEP"*}"
        [ -n "$g" ] && case "$members" in *" $g "*) agent=claude ;; esac
    fi

    if [ -n "$agent" ]; then
        if [ "$n" -gt 1 ]; then CL_WHY="split tab with an agent — can't park it yet"; return 0; fi
        agent_guard "$win"
        [ "$?" = 0 ] && { CL_WHY="$GUARD_WHY"; return 0; }
        tab_busy "$win" && { CL_WHY="that agent is still working"; return 0; }
        case "$agent" in
            claude|abby) has_unsent_input "$CL_PANE" && { CL_WHY="unsent text in the agent's composer"; return 0; } ;;
        esac
        CL_KIND=pin; CL_AGENT="$agent"
        return 0
    fi
    [ -n "$why" ] && { CL_WHY="$why"; return 0; }
    [ -n "$noshell" ] && { CL_WHY="$noshell"; return 0; }
    CL_KIND=park
    return 0
}

# Close a one-pane agent tab into a pinned ⌘Z entry. 0 = closed and pinned;
# 3 = refused, nothing killed (PIN_WHY: closed-tabs.sh's own reason); 1 =
# something else went wrong (PIN_WHY). With a snooze (SNOOZE_WAKE) the
# entry is written with its alarm (`--wake`); PIN_ID is the new entry's id,
# which closed-tabs.sh prints on success.
PIN_WHY=""
PIN_ID=""
pin_window() {
    local pane="$1" err rc errf
    PIN_WHY=""; PIN_ID=""
    [ -f "$CLOSED_TABS_SH" ] || { PIN_WHY="closed-tabs.sh is missing"; return 3; }
    errf=$(mktemp "${TMPDIR:-/tmp}/stash-pin.XXXXXX") || errf=/dev/null
    PIN_ID=$(bash "$CLOSED_TABS_SH" close --pin ${SNOOZE_WAKE:+--wake "$SNOOZE_WAKE"} "$pane" 2>"$errf" </dev/null)
    rc=$?
    PIN_ID="${PIN_ID##*$'\n'}"
    err=$(cat "$errf" 2>/dev/null)
    [ "$errf" = /dev/null ] || rm -f "$errf"
    case "$rc" in
        0) return 0 ;;
        3) PIN_WHY=$(printf '%s\n' "$err" | sed -n 's/^closed-tabs: not stashed — //p' | tail -n 1)
           PIN_WHY="${PIN_WHY:-closed-tabs.sh refused it}"
           return 3 ;;
        *) PIN_WHY="closed-tabs.sh close --pin failed ($rc)"; return 1 ;;
    esac
}

# 0 when every process of <pgid> on <tty> is stopped (T, or a zombie), 2 when
# there is none left, 1 when one still runs after FREEZE_TRIES ticks.
pg_wait_stopped() {   # <tty> <pgid>
    local i=0 r
    while :; do
        r=$(ps -t "${1#/dev/}" -o pgid=,stat= 2>/dev/null |
            awk -v g="$2" '$1 == g { n++; if ($2 !~ /^[TZ]/) run++ } END { print (n == 0) ? 2 : (run ? 1 : 0) }')
        [ "$r" = 1 ] || return "${r:-1}"
        i=$((i + 1)); [ "$i" -ge "$FREEZE_TRIES" ] && return 1
        sleep 0.05
    done
}

# Every OTHER process group still running under the pane's shell — a
# background job (`server &`), or a group the foreground job started of its
# own (a dev server's workers that setpgid), which ⌃Z does not reach. Each is
# SIGSTOPped and printed, comma-separated, for the thaw to SIGCONT.
pg_stop_rest() {   # <pane pid> <tty> [<fg pgid>]
    local g out=""
    for g in $(ps -t "${2#/dev/}" -o pid=,ppid=,pgid=,stat= 2>/dev/null | awk -v root="$1" -v fg="${3:-}" '
            $1 ~ /^[0-9]+$/ { pp[$1] = $2; pg[$1] = $3; st[$1] = $4; o[++n] = $1 }
            END {
                m[root] = 1
                do { ch = 0
                     for (i = 1; i <= n; i++) { p = o[i]; if (!(p in m) && (pp[p] in m)) { m[p] = 1; ch = 1 } }
                } while (ch)
                for (i = 1; i <= n; i++) { p = o[i]
                    if ((p in m) && p != root && pg[p] != fg && pg[p] != pg[root] && st[p] !~ /^[TZ]/ && !(pg[p] in seen)) {
                        seen[pg[p]] = 1; print pg[p] } }
            }'); do
        kill -STOP -- "-$g" 2>/dev/null && out="${out:+$out,}$g"
    done
    printf '%s' "$out"
}

# Is the terminal generating signals from keys (termios ISIG)? A raw-mode
# program (vim — in every mode —, a TUI) turns it off and reads ⌃Z as a byte:
# typed, it lands in the buffer. Unknown (stty could not read it) = no.
isig_on() {   # <tty>
    local s
    s=$(stty -f "$1" -a 2>/dev/null) || return 1
    s=" ${s//$'\n'/ } "
    case "$s" in *" -isig "*) return 1 ;; *" isig "*) return 0 ;; esac
    return 1
}

# The shell's own working directory: the one its job was typed in, and so the
# one to re-run it from (the job's leader may have cd'd — `./dev.sh` going
# into sub/). It cannot change while the job holds the terminal.
shell_cwd() {   # <shell pid>
    lsof -a -d cwd -p "$1" -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1
}

# @stash_frozen's value from "pane_index US pgid US cwd US pids US groups"
# lines on stdin (pgid 0: nothing in the foreground, only stopped background
# groups): a JSON array, one object per pane —
#   {"pane": N, "pgid": N, "cwd": "...", "cmd": "...", "xpg": [N, ...]}
# cmd is what a cold unpark types: the job's EXACT argv, read with
# KERN_PROCARGS2 (ps flattens it: `a "b c"` and `a b c` print alike) and
# quoted for the shell (\$'…' for control characters, so a tab cannot reach
# the line editor as a completion key). Empty — "command unknown" — when it
# cannot be trusted: a pipeline (the members' order is not knowable from the
# process table), an unreadable argv, one a program rewrote (node's
# process.title: ["npm run dev", "", ""]), a forked subshell. Only cmd is
# stored, not argv beside it: tmux refuses a command over ~16 KB.
# JSON keeps the value free of \x1f and newlines, so it rides in the sidecar.
FROZEN_PY='
import ctypes, ctypes.util, json, os, re, struct, sys
libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
def sysctl(mib, size):
    arr = (ctypes.c_int * len(mib))(*mib)
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(size)
    if libc.sysctl(arr, len(mib), buf, ctypes.byref(n), None, ctypes.c_size_t(0)) != 0:
        return None
    return buf.raw[:n.value]
try:
    ARGMAX = struct.unpack("i", sysctl([1, 8], 4))[0]
except Exception:
    ARGMAX = 1 << 20
def argv(pid):
    try:
        raw = sysctl([1, 49, int(pid)], ARGMAX)
        if not raw or len(raw) < 4:
            return None
        argc = struct.unpack("i", raw[:4])[0]
        i = raw.find(b"\0", 4)
        if i < 0:
            return None
        while i < len(raw) and raw[i] == 0:
            i += 1
        out = []
        for _ in range(argc):
            j = raw.find(b"\0", i)
            if j < 0:
                return None
            out.append(raw[i:j].decode("utf-8", "replace"))
            i = j + 1
        return out
    except Exception:
        return None
SHELLS = set("zsh bash sh fish dash ksh tcsh csh nu".split())
def trusted(a):
    if not a or not a[0] or "�" in "".join(a):
        return False
    if len(a) == 1 and a[0].lstrip("-").rsplit("/", 1)[-1] in SHELLS:
        return False
    if any(c.isspace() for c in a[0]) and not os.path.exists(a[0]):
        return False
    # A title written over the argv area: libuv zeroes the rest (["grunt",
    # "", ""]); perl pads it with spaces, and the parse then runs on into the
    # environment (["grunt", "      ", "HOME=..."]).
    if len(a) > 1 and not any(a[1:]):
        return False
    if any(w and not w.strip() for w in a[1:]):
        return False
    return True
SAFE = re.compile(r"[A-Za-z0-9_@%+:,./-]+")
def q(w):
    if w and SAFE.fullmatch(w):
        return w
    if all(" " <= c != "\x7f" for c in w):
        return "\x27" + w.replace("\x27", "\x27\\\x27\x27") + "\x27"
    out = []
    for c in w:
        if c == "\\": out.append("\\\\")
        elif c == "\x27": out.append("\\\x27")
        elif ord(c) < 32 or ord(c) == 127: out.append("\\x%02x" % ord(c))
        else: out.append(c)
    return "$\x27" + "".join(out) + "\x27"
res = []
for line in sys.stdin.read().split("\n"):
    f = line.split("\x1f")
    if len(f) < 5 or not f[0].isdigit() or not f[1].isdigit():
        continue
    pids = [x for x in f[3].split(",") if x.isdigit()]
    cmd = ""
    if int(f[1]) and len(pids) == 1:
        a = argv(pids[0])
        if trusted(a):
            cmd = " ".join(q(w) for w in a)
    res.append({"pane": int(f[0]), "pgid": int(f[1]), "cwd": f[2], "cmd": cmd,
                "xpg": [int(x) for x in f[4].split(",") if x.isdigit()]})
print(json.dumps(res))
'
frozen_json() {
    local in out
    in=$(cat)
    out=$(printf '%s\n' "$in" | python3 -I -c "$FROZEN_PY" 2>/dev/null)
    [ -n "$out" ] && { printf '%s' "$out"; return 0; }
    # No command lines without python: the warm `fg` still works, a cold
    # unpark cannot re-run anything and says so.
    log "could not read the frozen jobs' argv (python3) — recorded without their commands"
    printf '%s\n' "$in" | frozen_json_bare
}
# The record with no command lines: from frozen_json's input lines, or (with
# --strip) from an existing record.
frozen_json_bare() {
    if [ "${1:-}" = --strip ]; then
        jq -c 'map(.cmd = "" | del(.cwd))' 2>/dev/null
    else
        jq -Rnc '[inputs | split("\u001f") | select(length >= 5)
                  | {pane: (.[0] | tonumber), pgid: (.[1] | tonumber), cwd: .[2], cmd: "",
                     xpg: (.[4] | split(",") | map(select(. != "") | tonumber))}]' 2>/dev/null
    fi
}
# Store a record. tmux refuses a command over ~16 KB ("command too long") —
# a job with a huge argument list — and then it goes again without the
# command lines, so the warm `fg` still finds its job and a cold unpark says
# "command unknown". 1 only when even that cannot be stored.
store_frozen() {   # <win> <json>
    local win="$1" json="$2" bare
    tmux set-option -w -t "$win" @stash_frozen "$json" 2>/dev/null && return 0
    bare=$(printf '%s' "$json" | frozen_json_bare --strip)
    if [ -n "$bare" ] && tmux set-option -w -t "$win" @stash_frozen "$bare" 2>/dev/null; then
        log "frozen jobs of $win recorded without their commands (too long for tmux)"
        return 0
    fi
    log "could not record the frozen jobs of $win"
    return 1
}

# Keys typed into a window must reach one pane: synchronize-panes would send
# them to every pane. sync_off <win> remembers the window's own value in
# SYNC_WAS ("-" = not set locally) and turns it off; sync_restore puts it back.
SYNC_WAS="-"
sync_off() {
    SYNC_WAS="-"
    [ "$(tmux display-message -p -t "$1" '#{synchronize-panes}' 2>/dev/null)" = 1 ] || return 0
    SYNC_WAS=$(tmux show -wqv -t "$1" synchronize-panes 2>/dev/null)
    SYNC_WAS="${SYNC_WAS:-unset}"
    tmux set-option -w -t "$1" synchronize-panes off 2>/dev/null
}
sync_restore() {
    case "$SYNC_WAS" in
        -) ;;
        unset) tmux set-option -uw -t "$1" synchronize-panes 2>/dev/null ;;
        *) tmux set-option -w -t "$1" synchronize-panes "$SYNC_WAS" 2>/dev/null ;;
    esac
    SYNC_WAS="-"
}

# Freeze everything that runs under each pane's shell in <window>:
#   - the foreground job: ⌃Z typed into the pane when the terminal turns keys
#     into signals (ISIG) — the job's own handler runs, a TUI puts the
#     terminal back, the shell files it as a stopped job, so `fg` brings it
#     back; SIGSTOP on its group when the terminal is raw (the ⌃Z would be a
#     byte in vim's buffer) or when something in the group still runs after
#     FREEZE_TRIES ticks. SIGSTOP cannot be caught, and the shell still sees
#     a stopped job (zsh keeps the job's terminal modes for its `fg`);
#   - every other group still running under the shell (pg_stop_rest) —
#     FIRST: a shell makes the job stopped last its current job (%+), and the
#     thaw's `fg` must resume the foreground one, not a background job
#     SIGSTOPped after it.
# Recorded in @stash_frozen (frozen_json). 1 = something could not be stopped
# or recorded (FREEZE_WHY); what WAS frozen is still recorded, so the
# caller's thaw puts it back. A pane whose job will not stop is continued
# again, groups and all, before giving up.
FREEZE_WHY=""
freeze_window() {
    local win="$1" pane idx ppid tty dead k a b c fgpg mem job rc lines="" xpg scwd g
    FREEZE_WHY=""
    # A record left by an earlier park (a thaw that kept a busy pane's entry,
    # a cold mark) describes jobs that are not this park's: re-used, it would
    # re-run a command whose job is still stopped, or show an idle tab as
    # frozen. This park's record is written from scratch.
    tmux set-option -uw -t "$win" @stash_frozen 2>/dev/null
    tmux set-option -uw -t "$win" @stash_cold 2>/dev/null
    sync_off "$win"
    while IFS="$SEP" read -r pane idx ppid tty dead; do
        [ -n "$pane" ] && [ "$dead" != 1 ] && [ -n "$tty" ] || continue
        case "$ppid" in ''|*[!0-9]*) continue ;; esac
        fgpg=""; mem=""; job=""
        while IFS=$'\t' read -r k a b c; do
            [ "$k" = fg ] && { fgpg="$a"; mem="$b"; job="$c"; }
        done < <(pane_scan "$ppid" "$tty")
        xpg=$(pg_stop_rest "$ppid" "$tty" "$fgpg")
        if [ -n "$fgpg" ]; then
            rc=1
            if isig_on "$tty"; then
                # A pane in copy-mode (or any mode) would take the ⌃Z itself.
                tmux copy-mode -q -t "$pane" 2>/dev/null
                tmux send-keys -t "$pane" C-z 2>/dev/null
                pg_wait_stopped "$tty" "$fgpg"; rc=$?
                [ "$rc" = 1 ] && log "⌃Z did not stop $job (pgid $fgpg) in $win.$idx — SIGSTOP"
            else
                log "$job (pgid $fgpg) in $win.$idx has a raw terminal — SIGSTOP, not ⌃Z"
            fi
            if [ "$rc" = 1 ]; then
                kill -STOP -- "-$fgpg" 2>/dev/null
                pg_wait_stopped "$tty" "$fgpg"; rc=$?
            fi
            [ "$rc" = 2 ] && fgpg=""                  # it exited meanwhile
            if [ "$rc" = 1 ]; then
                # Not frozen, not recorded: leave nothing of this pane stopped.
                kill -CONT -- "-$fgpg" 2>/dev/null
                for g in ${xpg//,/ }; do kill -CONT -- "-$g" 2>/dev/null; done
                FREEZE_WHY="couldn't stop what runs in pane $idx"
                break
            fi
        fi
        [ -n "$fgpg$xpg" ] || continue
        scwd=$(shell_cwd "$ppid")
        [ -n "$scwd" ] || scwd=$(tmux display-message -p -t "$pane" '#{pane_current_path}' 2>/dev/null)
        lines="${lines}${idx}${SEP}${fgpg:-0}${SEP}${scwd}${SEP}${mem}${SEP}${xpg}"$'\n'
    done < <(tmux list-panes -t "$win" -F "#{pane_id}${SEP}#{pane_index}${SEP}#{pane_pid}${SEP}#{pane_tty}${SEP}#{pane_dead}" 2>/dev/null)
    sync_restore "$win"
    if [ -n "$lines" ]; then
        local json; json=$(frozen_json <<< "$lines")
        if [ -z "$json" ] || ! store_frozen "$win" "$json"; then
            [ -n "$FREEZE_WHY" ] || FREEZE_WHY="couldn't record its stopped jobs"
        fi
    fi
    [ -z "$FREEZE_WHY" ]
}

# stopped | running | gone: the processes of <pgid> on <tty>.
pg_state() {   # <tty> <pgid>
    ps -t "${1#/dev/}" -o pgid=,stat= 2>/dev/null |
        awk -v g="$2" '$1 == g { n++; if ($2 !~ /^[TZ]/) run++ }
                       END { print (n == 0) ? "gone" : (run ? "running" : "stopped") }'
}
# The shell owns its terminal again: at a prompt, not running a job.
shell_at_prompt() {   # <pane pid>
    [ "$(ps -o tpgid= -p "$1" 2>/dev/null | tr -d ' ')" = "$1" ]
}

# Type <text> + Enter into <pane> of <win>, but only if the window is still
# home: under the park lock, so a re-park cannot take it between the test
# and the keys. 1 = not typed (lock busy, or parked again).
type_home() {   # <win> <pane> <text>
    # Already ours (a park's failed freeze thaws what it froze, under its
    # lock): no second acquire, which would wait on itself.
    local mine=""
    [ "$(cat "$LOCKDIR/pid" 2>/dev/null)" = "$$" ] && mine=1
    [ -n "$mine" ] || lock_acquire || return 1
    if [ "$(tmux display-message -p -t "$1" '#{session_name}' 2>/dev/null)" = "$HOLD" ]; then
        [ -n "$mine" ] || lock_release
        return 1
    fi
    tmux copy-mode -q -t "$2" 2>/dev/null
    # ⌃C first, only while the shell itself holds the terminal (so the
    # SIGINT reaches nothing but the shell): a half-typed continuation (an
    # open quote, a trailing \) is abandoned for a fresh prompt, where the
    # line would otherwise have joined it. The shell is given a moment to
    # redraw before the line arrives.
    local ppid; ppid=$(tmux display-message -p -t "$2" '#{pane_pid}' 2>/dev/null)
    if [ -n "$ppid" ] && shell_at_prompt "$ppid"; then
        tmux send-keys -t "$2" C-c 2>/dev/null
        sleep 0.15
    fi
    tmux send-keys -t "$2" C-u 2>/dev/null
    tmux send-keys -t "$2" -l "$3" 2>/dev/null
    tmux send-keys -t "$2" Enter 2>/dev/null
    [ -n "$mine" ] || lock_release
    return 0
}

# Bring a window's frozen jobs back; unstash hands off to this (`thaw`), in
# the background, once the window is home. Per frozen pane:
#   cold — the processes died with the tmux server (@stash_cold, set by
#          restore-state; or the server is younger than the park). Checked
#          FIRST: after a reboot a recorded pgid can name something else. The
#          recorded command is re-run in the shell's directory, and the
#          client is told; with none, "couldn't re-run: command unknown";
#   warm — the job is there, stopped: `fg` is typed at the shell's prompt
#          (only when the shell holds the terminal, i.e. is at one), the
#          other stopped groups get SIGCONT, and a SIGWINCH a moment later
#          makes a TUI repaint what a ⌃Z or SIGSTOP left behind;
#   gone — it ended while parked on a live server: nothing is re-run.
# A pane that is busy, or whose job did not resume, keeps its entry (and its
# other groups stay stopped): @stash_frozen goes only once every entry is
# settled, and @stash_cold with it.
do_thaw() {
    local win="${1:-}" client="${2:-}" json cold ts start
    [ -n "$win" ] || return 0
    json=$(tmux show -wqv -t "$win" @stash_frozen 2>/dev/null)
    [ -n "$json" ] || return 0
    # Parked again since the unpark that handed this off: not ours to thaw.
    [ "$(tmux display-message -p -t "$win" '#{session_name}' 2>/dev/null)" = "$HOLD" ] && return 0
    cold=$(tmux show -wqv -t "$win" @stash_cold 2>/dev/null)
    if [ -z "$cold" ]; then
        ts=$(tmux show -wqv -t "$win" @stash_ts 2>/dev/null)
        start=$(tmux display-message -p -t "$win" '#{start_time}' 2>/dev/null)
        # Both numeric, each on its own: a move-back after a failed freeze
        # has unset @stash_ts, and "" is no number to compare.
        case "$ts" in ''|*[!0-9]*) ;; *)
            case "$start" in ''|*[!0-9]*) ;; *) [ "$start" -gt "$ts" ] && cold=1 ;; esac ;;
        esac
    fi
    local pidx pg cwd cmd xpg obj pane ppid tty state g i keep=() said=() kind=info
    sync_off "$win"
    while IFS="$SEP" read -r pidx pg cwd cmd xpg obj; do
        [ -n "$pidx" ] || continue
        pane=""; ppid=""; tty=""
        IFS="$SEP" read -r pane ppid tty < <(tmux list-panes -t "$win" \
            -F "#{pane_index}${SEP}#{pane_id}${SEP}#{pane_pid}${SEP}#{pane_tty}" 2>/dev/null |
            awk -F"$SEP" -v OFS="$SEP" -v i="$pidx" '$1 == i { print $2, $3, $4; exit }')
        [ -n "$pane" ] || { log "thaw $win: pane $pidx is gone"; continue; }
        if [ -n "$cold" ]; then
            [ "$pg" = 0 ] && continue                 # background groups only: not re-run
            if [ -z "$cmd" ]; then
                said+=("couldn't re-run: command unknown (pane $pidx)"); kind=warn
            elif ! shell_at_prompt "$ppid"; then
                said+=("pane $pidx is busy — not restarted yet: $cmd"); kind=warn
                keep+=("$obj")
            else
                local line; printf -v line 'cd %q && %s' "${cwd:-$HOME}" "$cmd"
                if type_home "$win" "$pane" "$line"; then
                    said+=("restarted after a tmux restart: $cmd")
                    log "thaw $win.$pidx: cold — re-ran $line"
                else
                    said+=("pane $pidx: busy — not restarted yet: $cmd"); kind=warn
                    keep+=("$obj")
                fi
            fi
            continue
        fi
        state=gone
        [ "$pg" = 0 ] || state=$(pg_state "$tty" "$pg")
        case "$state" in
            stopped)
                if ! shell_at_prompt "$ppid"; then
                    said+=("pane $pidx is busy — type fg there to resume it"); kind=warn
                    keep+=("$obj"); continue
                fi
                if ! type_home "$win" "$pane" fg; then
                    said+=("pane $pidx: busy — type fg there"); kind=warn
                    keep+=("$obj"); continue
                fi
                i=0
                while [ "$i" -lt 40 ] && [ "$(pg_state "$tty" "$pg")" = stopped ]; do sleep 0.05; i=$((i + 1)); done
                if [ "$(pg_state "$tty" "$pg")" = stopped ]; then
                    log "thaw $win.$pidx: fg typed but pgid $pg is still stopped — kept its record"
                    said+=("pane $pidx did not resume — type fg there"); kind=warn
                    keep+=("$obj"); continue
                fi ;;
            gone)
                if [ "$pg" != 0 ]; then
                    log "thaw $win.$pidx: pgid $pg ended while parked — not re-run"
                    said+=("${cmd:-its job} ended while parked — not restarted"); kind=warn
                fi ;;
        esac
        for g in ${xpg//,/ }; do kill -CONT -- "-$g" 2>/dev/null; done
        [ "$state" = gone ] || ( sleep 0.3; kill -WINCH -- "-$pg" ) </dev/null >/dev/null 2>&1 &
    done < <(jq -r '.[] | [(.pane | tostring), (.pgid | tostring), (.cwd // ""), (.cmd // ""),
                         ((.xpg // []) | map(tostring) | join(",")), tojson] | join("\u001f")' <<< "$json" 2>/dev/null)
    sync_restore "$win"
    if [ "${#keep[@]}" -eq 0 ]; then
        tmux set-option -uw -t "$win" @stash_frozen 2>/dev/null
        tmux set-option -uw -t "$win" @stash_cold 2>/dev/null
    else
        local rest="[" o
        for o in "${keep[@]}"; do rest="${rest}${o},"; done
        tmux set-option -w -t "$win" @stash_frozen "${rest%,}]" 2>/dev/null
    fi
    if [ "${#said[@]}" -gt 0 ]; then
        local text="${said[0]}" s
        for s in "${said[@]:1}"; do text="$text · $s"; done
        notify_user ${client:+-c "$client"} -k "$kind" -- "stash: $text"
    fi
    return 0
}

# The stash for the merged picker (closed-tabs.sh), one per line,
# \x1f-separated: window id, origin, label, park time, state, cwd, wake epoch
# (a snoozed window's @stash_wake; "" for none, and for pending rows). State is
# frozen | cold | idle for a parked window — `suspended` for one parked
# before 2026-11-11 whose agent was suspended (@stash_session) — and
# `pending` for a window that is
# home already but whose suspended agent was never resumed (a resume refused
# because the pane was busy; @stash_session still on it): `unstash` on it
# runs the resume. Its "origin" is the session it sits in.
do_rows() {
    if hold_exists; then
        tmux list-windows -t "=$HOLD" -F "#{window_id}${SEP}#{@stash_origin}${SEP}#{?#{@stash_label},#{@stash_label},#{?#{@agent_summary},#{@agent_summary},#{window_name}}}${SEP}#{@stash_ts}${SEP}#{?#{@stash_session},suspended,#{?#{@stash_cold},cold,#{?#{@stash_frozen},frozen,idle}}}${SEP}#{?#{@stash_cwd},#{@stash_cwd},#{pane_current_path}}${SEP}#{@stash_wake}" 2>/dev/null
    fi
    local p
    for p in $(pending_windows); do
        tmux display-message -p -t "$p" "#{window_id}${SEP}#{session_name}${SEP}#{?#{@stash_label},#{@stash_label},#{?#{@agent_summary},#{@agent_summary},#{window_name}}}${SEP}#{@stash_ts}${SEP}pending${SEP}#{?#{@stash_cwd},#{@stash_cwd},#{pane_current_path}}${SEP}" 2>/dev/null
    done
    return 0
}

# The words a refusal uses: a park's ("not parking it", "nothing hidden"), or
# a snooze's ("not snoozing it", "nothing snoozed") while SNOOZE_WORDS is set.
# refuse TEXT: the park's toast — or, under `check` (STASH_DRY), the same
# text on stdout and a verdict of 3, with no toast. slog: the park's log,
# which `check` does not write (nothing happened).
STASH_DRY=""
SNOOZE_WORDS=""
DRY_RC=0
ING=parking
HIDING=hiding
DONE=hidden
stash_words() {
    if [ -n "$SNOOZE_WORDS" ]; then ING=snoozing; HIDING=snoozing; DONE=snoozed
    else ING=parking; HIDING=hiding; DONE=hidden; fi
}
refuse() {
    if [ -n "$STASH_DRY" ]; then printf '%s\n' "$*"; DRY_RC=3
    else msg -k warn "$*"; fi
}
slog() { [ -n "$STASH_DRY" ] || log "$@"; }

# check [--snooze] [<win>]: would `stash` (or, with --snooze, `snooze`) take
# this tab? The same classification and refusals — the deep guards included —
# with no side effect: nothing moves, freezes or closes, no toast, no log
# line. A refusal prints its reason (the toast's text, no "stash: ") and
# exits 3; parkable is silent, 0.
do_check() {
    [ "${1:-}" = --snooze ] && { SNOOZE_WORDS=1; shift; }
    STASH_DRY=1; DRY_RC=0
    do_stash_many "$(sel_win "${1:-}")"
    STASH_DRY=""
    return "$DRY_RC"
}

# Park one or more windows as a single transaction: one lock, one renumber, one
# publish. Looping the single-window path instead would take and drop the lock
# per window, renumber per window, and let another park interleave halfway
# through a group the user selected as one unit.
do_stash_many() {
    local w s sess="" wins=() total busy=0 homes=0 seen="" note=""
    stash_words
    # Per accepted window, beside `wins`: what classify_window decided, and
    # its pane for a pin. Refusals with a reason go to `kept_why`.
    local kinds=() cpanes=() cagents=() kept_why=() pre="" line

    # The slow part of the classification FIRST, outside the lock: the deep
    # guards (session files, the transcript, the workflow and subagent walks,
    # the cua fork) and a ps per pane. Under the lock only the cheap re-checks
    # (pin_recheck) and the moves run, so a range park holds the lock for
    # moves and freezes, not for guards. One line per window:
    # "win SEP kind SEP pane SEP agent SEP why".
    for w in "$@"; do
        [ -n "$w" ] || continue
        case "$pre" in *"$w$SEP"*) continue ;; esac
        s=$(tmux display-message -p -t "$w" '#{session_name}' 2>/dev/null) || continue
        [ -n "$s" ] && [ "$s" != "$HOLD" ] || continue
        [ "$(tmux show -wqv -t "$w" @space_home 2>/dev/null)" = "1" ] && continue
        classify_window "$w"
        pre="${pre}${w}${SEP}${CL_KIND}${SEP}${CL_PANE}${SEP}${CL_AGENT}${SEP}${CL_WHY}"$'\n'
    done

    # Everything from here to the moves reads tmux state and then acts on it, so
    # it runs under the lock — moves, freezes and pins, each window refreshing
    # the lock's age (lock_touch) so a long range never reads as stale.
    [ -n "$STASH_DRY" ] || lock_acquire || { msg -k warn "busy — try again"; return 0; }   # 0: see the note on the refusal below

    # Resolve membership UNDER the lock. These ids come from a keypress that may
    # be seconds old by now, so in between a window can have been closed, parked
    # by another press, or moved to another session.
    for w in "$@"; do
        [ -n "$w" ] || continue
        s=$(tmux display-message -p -t "$w" '#{session_name}' 2>/dev/null) || continue
        [ -n "$s" ] || continue
        case "$s" in "$HOLD") continue ;; esac                # already parked
        [ -n "$sess" ] || sess="$s"
        [ "$s" = "$sess" ] || continue                        # a range is one session by construction
        # De-dupe against everything SEEN, not just what was accepted: a busy
        # window is never appended to wins, so testing wins let `@5 @5` count
        # one working agent twice and report "all 2 of those agents".
        case " $seen " in *" $w "*) continue ;; esac
        seen="$seen $w"
        # A space's HOME never parks (2026-10-22). M-m and x/C-l already refuse
        # it, but H did not — and a range always reaches it, since sel_order
        # walks the home first. Parked, the space grew a new home; brought back,
        # space.sh's sync kept the lower one, stripped the returning window's
        # markers and killed its dashboard pane.
        if [ "$(tmux show -wqv -t "$w" @space_home 2>/dev/null)" = "1" ]; then
            slog "refused to hide $w — it is its space's home"
            homes=$((homes + 1)); continue
        fi
        # Refuse to hide a working agent at all, rather than hiding it and then
        # declining to suspend it. That combination is the worst of both: the
        # tab is gone from the bar AND still holding its ~1GB and its share of a
        # core, with nothing on screen to say so. Parking is an explicit "not
        # now", and "not now" is not a thing to say to a turn in flight.
        if agent_running "$w"; then
            slog "refused to hide $w — $AGENT_RUNNING_WHY"
            busy=$((busy + 1)); continue
        fi
        # Everything else that would leave something running in the stash,
        # or lose work, was settled above — before anything moves (the old
        # "parked, but left running" came from guards that ran after it).
        line=$(printf '%s' "$pre" | awk -F"$SEP" -v w="$w" '$1 == w { print; exit }')
        if [ -n "$line" ]; then
            IFS="$SEP" read -r _ CL_KIND CL_PANE CL_AGENT CL_WHY <<< "$line"
        else
            classify_window "$w"
        fi
        if [ "$CL_KIND" = refuse ]; then
            if [ "$CL_WHY" = "that agent is still working" ]; then
                busy=$((busy + 1))
            else
                kept_why+=("$CL_WHY")
            fi
            slog "refused to hide $w — $CL_WHY"
            continue
        fi
        wins+=("$w"); kinds+=("$CL_KIND"); cpanes+=("$CL_PANE"); cagents+=("$CL_AGENT")
    done

    if [ "${#wins[@]}" -eq 0 ]; then
        lock_release
        local nk="${#kept_why[@]}"
        if [ "$nk" -gt 0 ] && [ "$busy" -eq 0 ] && [ "$homes" -eq 0 ]; then
            refuse "$(why_list "${kept_why[@]}") — not $ING $([ "$nk" -eq 1 ] && echo it || echo them)"
        elif [ "$nk" -gt 0 ]; then
            local parts; parts=$(why_list "${kept_why[@]}")
            [ "$busy" -gt 0 ]  && parts="$parts; $busy still working"
            [ "$homes" -gt 0 ] && parts="$parts; the home tab stays"
            refuse "nothing $DONE — $parts"
        elif [ "$busy" -eq 1 ]; then
            refuse "that agent is still working — not $HIDING it"
        elif [ "$busy" -gt 1 ]; then
            refuse "all $busy of those agents are still working — not $HIDING them"
        elif [ "$homes" -gt 0 ]; then
            refuse "that's the space's home tab — it stays"
        else
            refuse "already parked"
        fi
        return 0
    fi

    # Never strand a client: taking a session's last window destroys it, and
    # detach-on-destroy is on here, so that would drop the attached client to
    # the shell. Counted INSIDE the lock — two concurrent parks both saw "2
    # windows" and both moved, and the second one emptied the session.
    total=$(tmux list-windows -t "=$sess" -F '#{window_id}' 2>/dev/null | wc -l | tr -d ' ')
    case "$total" in ''|*[!0-9]*) total=0 ;; esac
    if [ "$total" -le "${#wins[@]}" ]; then
        lock_release
        if [ "${#wins[@]}" -eq 1 ]; then
            refuse "that's the only tab in this session — not $ING it"
        else
            # Refused rather than quietly parking all but one: a partial result
            # nobody asked for is worse than a clear no.
            refuse "that's every tab in this session — leave one out and try again"
        fi
        # 0, NOT 1. `run-shell` displays the exit status of a failed command IN
        # THE PANE — which puts it into view-mode, so every subsequent keystroke
        # goes to copy-mode's key table instead of whatever is running there
        # until the user presses q. `-b` does not exempt it. A refusal the user
        # has already been told about via msg() is not a script failure, and
        # hijacking the focused pane to report it is far worse than the refusal.
        return 0
    fi

    # `check`: everything above is the whole verdict — nothing is acted on.
    [ -n "$STASH_DRY" ] && return 0

    local parked=() pinned=0 failed=0 left i kind rc hold_made=""
    BOOT_WIN=""
    for i in "${!wins[@]}"; do
        w="${wins[$i]}"; kind="${kinds[$i]}"
        # Re-check per move, not just once up front. The count above cannot be
        # fooled by duplicates or foreign ids, but nothing stops a window
        # CLOSING on its own between the count and the last move — a shell
        # exiting is not a stash.sh actor and the lock cannot hold it back. With
        # a range that is "N of N+1", so one exit is enough to leave the session
        # empty, and destroying it drops the attached client to a shell. A pin
        # closes the window, so it counts the same as a move.
        left=$(tmux list-windows -t "=$sess" -F '#{window_id}' 2>/dev/null | wc -l | tr -d ' ')
        case "$left" in ''|*[!0-9]*) left=0 ;; esac
        if [ "$left" -le 1 ]; then
            # Noted, not shown yet: display-message has no queue, each call
            # overwrites the last, so every partial result is composed into ONE
            # message at the end or only the final fragment is ever seen.
            note="stopped — the rest is all that's left in this session"
            break
        fi

        lock_touch

        # A one-pane agent tab: not parked, closed into a pinned ⌘Z entry
        # that resumes it. The cheap guards are read once more right before
        # (pin_recheck): the classify ran before the lock, and a draft dies
        # with the close.
        if [ "$kind" = pin ]; then
            if pin_recheck "$w" "${cpanes[$i]}" "${cagents[$i]}"; then
                if [ "$RECHECK_WHY" = "that agent is still working" ]; then busy=$((busy + 1)); else kept_why+=("$RECHECK_WHY"); fi
                slog "refused to hide $w — $RECHECK_WHY (re-check)"
                continue
            fi
            pin_window "${cpanes[$i]}"; rc=$?
            if [ "$rc" = 0 ]; then
                pinned=$((pinned + 1)); log "stashed $w as a pinned ⌘Z entry${SNOOZE_WAKE:+ ($PIN_ID), waking at $SNOOZE_WAKE}"
                [ -n "$SNOOZE_WAKE" ] && printf 'p:%s\n' "$PIN_ID"
            elif [ -z "$(tmux display-message -p -t "${cpanes[$i]}" '#{pane_id}' 2>/dev/null)" ]; then
                # Gone even so (a close that failed late).
                failed=$((failed + 1)); log "close --pin of $w failed ($PIN_WHY) and the tab is gone"
            else
                if [ "$rc" = 3 ]; then kept_why+=("$PIN_WHY"); else failed=$((failed + 1)); fi
                slog "refused to hide $w — $PIN_WHY"
            fi
            continue
        fi

        # Where it was, for a move back that puts it in its own slot.
        local o_idx o_act
        IFS="$SEP" read -r o_idx o_act < <(tmux display-message -p -t "$w" "#{window_index}${SEP}#{window_active}" 2>/dev/null)
        if [ -z "$hold_made" ]; then ensure_hold; hold_made=1; fi
        if ! park_one "$w" "$sess"; then failed=$((failed + 1)); continue; fi
        if freeze_window "$w"; then
            parked+=("$w")
            if [ -n "$SNOOZE_WAKE" ]; then
                tmux set-option -w -t "$w" @stash_wake "$SNOOZE_WAKE" 2>/dev/null
                printf 'w:%s\n' "$w"
                log "snoozed $w until $SNOOZE_WAKE"
            fi
            continue
        fi
        # Never leave it running in the stash: back in its own slot, the part
        # that did freeze thawed, and the reason in the report. A move out of
        # a session leaves its index free (renumber runs only at the end), so
        # the slot is normally there; taken, it goes in before whoever holds it
        # (closed-tabs.sh's reopen does the same).
        log "could not freeze $w — $FREEZE_WHY; moved it back to $sess"
        if tmux move-window -d -s "$w" -t "=$sess:$o_idx" 2>/dev/null \
           || tmux move-window -d -b -s "$w" -t "=$sess:$o_idx" 2>/dev/null \
           || tmux move-window -d -s "$w" -t "=$sess:" 2>/dev/null; then
            [ "$o_act" = 1 ] && tmux select-window -t "$w" 2>/dev/null
            tmux set-option -uw -t "$w" @stash_origin 2>/dev/null
            tmux set-option -uw -t "$w" @stash_label 2>/dev/null
            tmux set-option -uw -t "$w" @stash_ts 2>/dev/null
            do_thaw "$w"
            kept_why+=("$FREEZE_WHY")
        else
            failed=$((failed + 1))
            log "could not move $w back to $sess — it is parked, partly running"
            parked+=("$w")
        fi
    done

    if [ "${#parked[@]}" -eq 0 ]; then
        # If this call created the holding session and then failed to put
        # anything in it, take the placeholder back out rather than leaving a
        # session whose only window is a stray shell that `count` reports as
        # parked and the next prefix+h would hand you.
        [ -n "$BOOT_WIN" ] && tmux kill-window -t "$BOOT_WIN" 2>/dev/null
        [ "$pinned" -gt 0 ] && renumber "$sess"
        lock_release
        [ "$pinned" -gt 0 ] && publish_count
        # Say WHY nothing was parked. "could not park it" on a batch where two
        # tabs were deliberately left working and the third refused to move is
        # both incomplete and, in the stopped case, simply wrong.
        if [ "$pinned" -gt 0 ]; then
            stash_report "$pinned" 0 "$busy" "$homes" "$failed" "$note" ${kept_why[@]+"${kept_why[@]}"}
        elif [ -n "$note" ]; then
            msg -k warn "nothing $DONE: $note"
        elif [ "${#kept_why[@]}" -gt 0 ] && [ "$failed" -eq 0 ]; then
            msg -k warn "$(why_list "${kept_why[@]}") — not $ING $([ "${#kept_why[@]}" -eq 1 ] && echo it || echo them)"
        elif [ "$busy" -gt 0 ]; then
            msg -k error "could not park it — and left $busy still working"
        else
            msg -k error "could not park it"
        fi
        return 0
    fi
    # Only ever kill a window this invocation created, and never one just parked.
    if [ -n "$BOOT_WIN" ]; then
        case " ${parked[*]} " in
            *" $BOOT_WIN "*) : ;;
            *) tmux kill-window -t "$BOOT_WIN" 2>/dev/null ;;
        esac
    fi
    renumber "$sess" "$HOLD"
    publish
    lock_release
    [ -n "$SNOOZE_WAKE" ] && reschedule_snooze
    stash_report "$pinned" "${#parked[@]}" "$busy" "$homes" "$failed" "$note" ${kept_why[@]+"${kept_why[@]}"}

    # Detached: a one-line account of each parked window for the picker.
    for w in "${parked[@]}"; do
        ( "$SELF" describe "$w" </dev/null >/dev/null 2>&1 & )
    done
    # No suspend_agent any more: an agent tab is pinned, never parked, and
    # everything parked is frozen. (suspend_agent / resume_agent stay for the
    # windows suspended before 2026-11-11 until they are migrated.)
}

# The reasons tabs were kept, de-duplicated, joined for one message.
why_list() {
    local out="" w
    for w in "$@"; do
        case "; $out; " in *"; $w; "*) continue ;; esac
        out="${out:+$out; }$w"
    done
    printf '%s' "$out"
}

# The one message after a park that did something. Silent when it all went
# (a clean park says nothing, as before); otherwise what happened to each part.
stash_report() {   # <pinned> <parked> <busy> <homes> <failed> <note> [<why>...]
    local pinned="$1" n_parked="$2" busy="$3" homes="$4" failed="$5" note="$6"; shift 6
    [ "$busy" -gt 0 ] || [ "$homes" -gt 0 ] || [ "$failed" -gt 0 ] || [ -n "$note" ] || [ "$#" -gt 0 ] || return 0
    local report="" s
    [ "$n_parked" -gt 0 ] && report="hid $n_parked"
    if [ "$pinned" -gt 0 ]; then
        s=s; [ "$pinned" -eq 1 ] && s=""
        report="${report:+$report, }stashed $pinned agent$s"
    fi
    [ "$#" -gt 0 ]      && report="$report — kept $#: $(why_list "$@")"
    [ "$busy" -gt 0 ]   && report="$report — left $busy still working"
    [ "$homes" -gt 0 ]  && report="$report — the home tab stays"
    [ "$failed" -gt 0 ] && report="$report — could not park $failed"
    [ -n "$note" ]      && report="$report — $note"
    if [ "$failed" -gt 0 ]; then msg -k error "$report"; else msg -k warn "$report"; fi
}

# The single-window entry point. Resolves its target through sel_win for the
# reason spelled out there — a bare `display-message -p` answers by
# most-recently-active rules, so `stash.sh stash` typed at a shell (where no
# window id is passed) could park a window in a different session than the one
# you are looking at, or no-op with a misleading "already parked".
#
# It also clears any range selection first. tmux hands the prefix key back to
# the prefix table WITHOUT consulting the `stash` table's bindings, so prefix
# mid-selection leaves the mode — prefix+v cancels it (a toggle since
# 2026-10-21), and the muscle-memory follow-up prefix+H lands here. Clearing
# here means that sequence parks the current window and tidies the tint,
# instead of parking one window that then travels into the holding session
# still flagged and comes back mauve.
do_stash() {
    do_sel_cancel
    do_stash_many "$(sel_win "${1:-}")"
}

# --- snooze -----------------------------------------------------------------
#
# snooze <epoch> [<win>]: exactly `stash`, plus the alarm — a one-pane agent
# tab goes to `closed-tabs.sh close --pin --wake <epoch>`, anything else is
# parked (and frozen) with @stash_wake. Same classification, refusals and
# messages as `stash`. Prints what got an alarm, one line each: `p:<entry
# id>` or `w:<window id>` — nothing means nothing was snoozed (refused, or a
# failure the usual message has already reported). Exit 0 either way, as
# stash: a run-shell would put a nonzero status into the pane.
SNOOZE_WAKE=""
do_snooze() {
    local when="${1:-}"
    case "$when" in
        ''|*[!0-9]*) msg -k error "snooze: no wake time"; return 0 ;;
    esac
    SNOOZE_WAKE="$when"; SNOOZE_WORDS=1
    do_stash "${2:-}"
    SNOOZE_WAKE=""; SNOOZE_WORDS=""
}

# set-wake <epoch|""> <win…>: set or clear the alarm on windows already
# parked; anything not in the holding session is skipped (`snooze` parks).
# Under the park lock, since `wake` reads the same option under it.
do_set_wake() {
    local when="${1:-}" w n=0; shift
    case "$when" in ''|*[!0-9]*) [ -z "$when" ] || { msg -k error "snooze: bad wake time $when"; return 0; } ;; esac
    lock_acquire || { msg -k warn "busy — try again"; return 0; }
    for w in "$@"; do
        [ "$(tmux display-message -p -t "$w" '#{session_name}' 2>/dev/null)" = "$HOLD" ] || continue
        if [ -n "$when" ]; then
            tmux set-option -w -t "$w" @stash_wake "$when" 2>/dev/null
        else
            tmux set-option -uw -t "$w" @stash_wake 2>/dev/null
        fi
        n=$((n + 1))
    done
    [ "$n" -gt 0 ] && save_state
    lock_release
    [ "$n" -gt 0 ] && reschedule_snooze
    return 0
}

# Where a woken window goes: its origin — unless that is gone or a hidden
# session — else main, else a visible session somebody is attached to, else
# any visible one. Nothing (empty) when there is no visible session at all;
# the window then stays parked.
wake_target() {   # <origin>
    local o="${1:-}" hidden
    if [ -n "$o" ] && [ "$o" != "$HOLD" ] && ! is_hidden_session "$o" && tmux has-session -t "=$o" 2>/dev/null; then
        printf '%s' "$o"; return 0
    fi
    if tmux has-session -t "=main" 2>/dev/null && ! is_hidden_session main; then printf 'main'; return 0; fi
    hidden=$(hidden_sessions)
    o=$(tmux list-sessions -F '#{?session_attached,#{session_name},}' 2>/dev/null |
        grep -v '^$' | grep -vxF -f <(printf '%s\n' "$hidden") | head -1)
    [ -n "$o" ] || o=$(tmux list-sessions -F '#{session_name}' 2>/dev/null |
                       grep -vxF -f <(printf '%s\n' "$hidden") | head -1)
    printf '%s' "$o"
}

# wake <win…>: the alarm went off (snooze.sh wake-due). Each window still
# parked goes back to its origin (wake_target) at the END of its bar, without
# becoming that session's current tab and without switching any client; its
# frozen jobs are thawed (warm `fg`, or the command re-run after a restart)
# and a suspended agent from before 2026-11-11 is resumed, as an unpark
# would. Prints "<label>\t<session>" per window woken, for snooze.sh's one
# toast; wake itself says nothing.
do_wake() {
    local w win origin dest label woken=() dests=() lines="" o seen
    hold_exists || return 0
    lock_acquire || { log "wake: stash lock busy — skipped $*"; return 0; }
    local wanted=" $* " ordered=()
    while read -r w; do
        [ -n "$w" ] || continue
        case "$wanted" in *" $w "*) ordered+=("$w") ;; esac
    done < <(tmux list-windows -t "=$HOLD" -F '#{window_id}' 2>/dev/null)
    for win in ${ordered[@]+"${ordered[@]}"}; do
        IFS="$SEP" read -r origin label < <(tmux display-message -p -t "$win" \
            "#{@stash_origin}${SEP}#{?#{@stash_label},#{@stash_label},#{?#{@agent_summary},#{@agent_summary},#{window_name}}}" 2>/dev/null)
        dest=$(wake_target "$origin")
        if [ -z "$dest" ]; then
            log "wake: no session to bring $win back to — left parked"
            continue
        fi
        if ! tmux move-window -d -a -s "$win" -t "=$dest:{end}" 2>/dev/null \
           && ! tmux move-window -d -s "$win" -t "=$dest:" 2>/dev/null; then
            log "wake: could not move $win to $dest"
            continue
        fi
        tmux set-option -uw -t "$win" @stash_origin 2>/dev/null
        tmux set-option -uw -t "$win" @stash_label 2>/dev/null
        tmux set-option -uw -t "$win" @stash_wake 2>/dev/null
        woken+=("$win")
        lines="${lines}${label}"$'\t'"${dest}"$'\n'
        seen=0
        for o in ${dests[@]+"${dests[@]}"}; do [ "$o" = "$dest" ] && seen=1; done
        [ "$seen" = 1 ] || dests+=("$dest")
        log "woke $win ($label) into $dest"
    done
    if [ "${#woken[@]}" -gt 0 ]; then
        renumber "$HOLD" "${dests[@]}"
        publish
    fi
    lock_release
    [ "${#woken[@]}" -gt 0 ] || return 0
    reschedule_snooze
    # Thawed here, in order (wake-due runs from the timer, not a key, so
    # nothing waits on it); a suspended agent's resume polls for up to 90s,
    # so that one is handed off as an unpark does.
    for win in "${woken[@]}"; do
        do_thaw "$win"
        [ -n "$(tmux show -wqv -t "$win" @stash_session 2>/dev/null)" ] \
            && tmux run-shell -b "'$SELF' resume '$win'"
    done
    printf '%s' "$lines"
    return 0
}

# --- selecting a range of tabs ------------------------------------------------
#
# prefix+S-Left / prefix+S-Right start a selection at the current tab and extend
# it one tab; the client is then left in the `stash` key table where bare
# S-Left/S-Right keep extending and H parks the lot.
#
# Modelled the way every list widget models shift-arrow: an ANCHOR that stays
# put and a CURSOR that moves, with everything between them selected. That is
# what makes shrinking fall out for free — walking the cursor back toward the
# anchor and out the other side reverses the direction with no special case.
# The range is contiguous because a contiguous range is the only kind the tab
# bar can show unambiguously.
#
# Anchor and cursor are window IDs, never indexes: parking renumbers the session
# (see renumber()), so an index captured beforehand names a different window
# afterwards.
#
# The bindings that move the cursor are FOREGROUND run-shell. Backgrounded, two
# quick taps both read the same cursor and both wrote cursor+1, so the selection
# stopped growing while the keys kept registering. tmux runs foreground items
# through the client's command queue in order, which serialises them for free —
# and the freeze that makes a foreground run-shell dangerous elsewhere needs a
# job that BLOCKS (the 90s resume poll); these are a handful of tmux calls.

# The window the key was pressed in, and the session holding it.
#
# A bare `tmux display-message -p '#{session_name}'` does NOT mean "the session
# the client is looking at". With no -t, tmux resolves the target by its own
# most-recently-active rules, and the holding session is the newest thing on the
# server the moment anything is parked — so with a client sitting in `main` and
# three tabs parked, it answered `stash`, every selection landed on the HOLD
# guard, and the keys silently did nothing (measured in the lab).
#
# So the bindings pass '#{window_id}', which tmux expands against the client's
# current window, exactly as the prefix+H binding already did. TMUX_PANE (set by
# run-shell) is the fallback; a bare display-message is the last resort and is
# only ever right when there is a single session.
sel_win() {
    local w="${1:-}"
    [ -n "$w" ] && { printf '%s' "$w"; return 0; }
    if [ -n "${TMUX_PANE:-}" ]; then
        tmux display-message -p -t "$TMUX_PANE" '#{window_id}' 2>/dev/null
    else
        tmux display-message -p '#{window_id}' 2>/dev/null
    fi
}
sel_sess_of() { [ -n "${1:-}" ] && tmux display-message -p -t "$1" '#{session_name}' 2>/dev/null; }

# The mode's keys in the ROOT table, bound only while a selection exists
# (tmux.conf.tmpl's stash section has why: bound for good, a prefix miss —
# C-s Esc — reached the pane). sel-start sources the file; sel_clear unbinds
# exactly these keys. The file sits beside this script, wherever it runs from
# (~/.config/tmux/scripts, or the chezmoi source in the tests);
# STASH_ROOT_KEYS_CONF names another.
STASH_ROOT_KEYS="h l j k H Enter m a x Escape q C-c Space"
stash_root_keys_conf() {
    local d="${BASH_SOURCE[0]%/*}"
    [ "$d" = "${BASH_SOURCE[0]}" ] && d=.
    printf '%s' "${STASH_ROOT_KEYS_CONF:-$d/stash-root-keys.conf}"
}
SEL_ROOT_BOUND=""     # what the last sel_clear found in @stash_root_bound

# Clear the flag wherever it is, not merely where we believe it is. A selection
# that leaked would tint tabs with no way to reach the mode that clears them.
#
# Also THE place the root keys come off — every ending runs through here
# (sel-cancel, so the park, sel-send and prefix+H too, and sel-start's wipe).
# @stash_root_bound rides in the list-windows call this makes anyway, so with
# nothing bound (a plain prefix+H) it costs no tmux call; with them bound it is
# one call: an `unbind-key -q -n` per key and the flag, as a single command list.
# `keep` (sel-start, which binds them again at once) leaves them be.
sel_clear() {
    local line w bound="" ids="" k cmd=()
    while IFS= read -r line; do
        [ -n "$bound" ] || bound="${line%%"$SEP"*}"
        w="${line#*"$SEP"}"
        [ -n "$w" ] && ids="$ids $w"
    done < <(tmux list-windows -a -F "#{@stash_root_bound}${SEP}#{?#{@stash_sel},#{window_id},}" 2>/dev/null)
    for w in $ids; do
        tmux set-option -uw -t "$w" @stash_sel 2>/dev/null
    done
    SEL_ROOT_BOUND="$bound"
    if [ -n "$bound" ] && [ "${1:-}" != keep ]; then
        for k in $STASH_ROOT_KEYS; do cmd+=(unbind-key -q -n "$k" ';'); done
        tmux "${cmd[@]}" set-option -gu @stash_root_bound 2>/dev/null
        SEL_ROOT_BOUND=""
    fi
}

# Bind the root keys (one call), unless the sel_clear just before found them
# bound and kept them.
sel_bind_root_keys() {
    [ -n "$SEL_ROOT_BOUND" ] && return 0
    tmux source-file "$(stash_root_keys_conf)" ';' set-option -g @stash_root_bound 1 2>/dev/null \
        || log "could not bind the selection's root keys ($(stash_root_keys_conf))"
}

# THE order a selection walks session $1 in: the agent sidebar's, not the tab
# bar's. agent-ui draws a space as its HOME (`@space_home` 1: the space's
# header), then its AGENTS (an `@agent_state`) by window index, then its
# SHELLS (the rest) by window index — view.rs `ViewModel::build` partitions
# the index-sorted windows, sidebar.rs `hide_homes` takes the home out of
# the agents whatever it runs, `Space::shells` sorts the rest; and while a
# selection is live PINNED is not drawn, so a pinned agent keeps its place
# among the agents. So with tabs `shell agent shell agent`, l from the first
# agent goes to the second, then to the shells, as the rows read on screen.
# (The idle fold and the shells' pill line only squeeze rows that are
# already in this order.) agent-ui/tests/sidebar.rs checks the two agree
# against a rendered frame. One list-windows, already in index order.
# `sel_order SESS rows` prints the whole row (id, @space_home, @agent_state,
# @agent_workflow, @agent_cua).
sel_order() {
    tmux list-windows -t "=$1" -F "#{window_id}${SEP}#{@space_home}${SEP}#{@agent_state}${SEP}#{@agent_workflow}${SEP}#{@agent_cua}" 2>/dev/null |
        awk -F "$SEP" -v rows="${2:-}" '
            { r = (rows != "" ? $0 : $1) }
            $2 == "1" { home[++nh] = r; next }
            $3 != ""  { agent[++na] = r; next }
                      { shell[++ns] = r }
            END {
                for (k = 1; k <= nh; k++) print home[k]
                for (k = 1; k <= na; k++) print agent[k]
                for (k = 1; k <= ns; k++) print shell[k]
            }'
}

# The ids currently selected, in sel_order — what a park parks and a move
# moves, in the order the sidebar shows them.
sel_ids() {
    local sel w
    sel=" $(tmux list-windows -t "=$1" -F '#{?#{@stash_sel},#{window_id},}' 2>/dev/null | tr '\n' ' ') "
    sel_order "$1" | while IFS= read -r w; do
        case "$sel" in *" $w "*) printf '%s\n' "$w" ;; esac
    done
}

# The window one step left/right of $2 in session $1, in sel_order — or $2
# itself at either end, so holding the key down parks the selection against
# the edge instead of silently wrapping around to the far side.
sel_neighbour() {
    sel_order "$1" |
        awk -v w="$2" -v d="$3" '
            { id[NR] = $0; if ($0 == w) i = NR }
            END {
                if (!i) { print w; exit }
                j = (d == "left" ? i - 1 : i + 1)
                if (j < 1 || j > NR) j = i
                print id[j]
            }'
}

# Every window between anchor and cursor inclusive, in sel_order (contiguous
# in the sidebar, not necessarily in the tab bar), whichever way round the
# two are. Empty (and nonzero) if either has since gone.
sel_range() {
    sel_order "$1" |
        awk -v a="$2" -v c="$3" '
            { id[NR] = $0; if ($0 == a) ai = NR; if ($0 == c) ci = NR }
            END {
                if (!ai || !ci) exit 1
                lo = (ai < ci ? ai : ci); hi = (ai < ci ? ci : ai)
                for (k = lo; k <= hi; k++) print id[k]
            }'
}

# Flag exactly $2.. and clear the flag everywhere else in session $1. Only
# windows whose flag actually changes are touched, so holding an arrow down does
# not fork a set-option per tab per keypress.
sel_apply() {
    local sess="$1"; shift
    local want=" $* " wid flag
    while IFS="$SEP" read -r wid flag; do
        [ -n "$wid" ] || continue
        case "$want" in
            # Space-delimited on BOTH sides. A bare substring test matches @1
            # inside @12 and would drag an unselected tab into the range — the
            # same trap that made every window id ending in 1 a false positive
            # in pending_windows().
            *" $wid "*) [ -n "$flag" ] || tmux set-option -w -t "$wid" @stash_sel 1 2>/dev/null ;;
            *)          [ -z "$flag" ] || tmux set-option -uw -t "$wid" @stash_sel 2>/dev/null ;;
        esac
    done < <(tmux list-windows -t "=$sess" -F "#{window_id}${SEP}#{@stash_sel}" 2>/dev/null)
    # status-interval is 5s, so without this the tab bar keeps showing the
    # previous selection until the next tick and the arrow key feels dead.
    tmux refresh-client -S 2>/dev/null
    sel_notice
}

# The agent-ui sidebar (CMD+B) shows the selection while the status line is
# hidden; the focus notice makes it redraw at once instead of on its 5 s net.
# AGENT_FOCUS_NOTICE renames it (tests); set but empty turns it off.
sel_notice() {
    local name="${AGENT_FOCUS_NOTICE-com.mackhaymond.tmux.focus}"
    [ -n "$name" ] && command -v notifyutil >/dev/null 2>&1 && notifyutil -p "$name" >/dev/null 2>&1
    return 0
}

do_sel_start() {
    local dir="${1:-right}" cur sess nxt
    cur=$(sel_win "${2:-}"); [ -n "$cur" ] || return 0
    sess=$(sel_sess_of "$cur"); [ -n "$sess" ] || return 0
    # Selecting inside the holding session would offer to park what is already
    # parked, and the tab bar it tints is not on screen to begin with.
    case "$sess" in "$HOLD") return 0 ;; esac
    sel_clear keep
    # `here` selects only the current tab — vim's `v` — and h/l (j/k) grow it from
    # there. left/right start already extended by one (the shift-arrow shape).
    if [ "$dir" = "here" ]; then nxt="$cur"; else nxt=$(sel_neighbour "$sess" "$cur" "$dir"); fi
    # Global, not session-scoped: set-option's -t is a PANE target, so naming a
    # session with it ("=main") fails to resolve outright. A stale anchor left by
    # another session is harmless — sel_range looks the anchor up among THIS
    # session's windows, does not find it, and the selection simply restarts.
    tmux set-option -g @stash_anchor "$cur" 2>/dev/null
    tmux set-option -g @stash_cursor "$nxt" 2>/dev/null
    # Before the paint, so the keys are there by the time the tint is.
    sel_bind_root_keys
    sel_apply "$sess" $(sel_range "$sess" "$cur" "$nxt")
}

do_sel_move() {
    local dir="${1:-right}" cur sess anchor cursor nxt
    cur=$(sel_win "${2:-}"); [ -n "$cur" ] || return 0
    sess=$(sel_sess_of "$cur"); [ -n "$sess" ] || return 0
    case "$sess" in "$HOLD") return 0 ;; esac
    anchor=$(tmux show -gqv @stash_anchor 2>/dev/null)
    cursor=$(tmux show -gqv @stash_cursor 2>/dev/null)
    # No live selection, or one whose windows have since gone: start a fresh one
    # rather than doing nothing, so a key in the table is never a dead end.
    if [ -z "$anchor" ] || [ -z "$cursor" ] || [ -z "$(sel_range "$sess" "$anchor" "$cursor")" ]; then
        do_sel_start "$dir" "$cur"
        return
    fi
    nxt=$(sel_neighbour "$sess" "$cursor" "$dir")
    tmux set-option -g @stash_cursor "$nxt" 2>/dev/null
    sel_apply "$sess" $(sel_range "$sess" "$anchor" "$nxt")
}

do_sel_cancel() {
    sel_clear
    tmux set-option -gu @stash_anchor 2>/dev/null
    tmux set-option -gu @stash_cursor 2>/dev/null
    tmux refresh-client -S 2>/dev/null
    sel_notice
}

do_sel_commit() {
    local cur sess ids
    cur=$(sel_win "${1:-}"); [ -n "$cur" ] || return 0
    sess=$(sel_sess_of "$cur"); [ -n "$sess" ] || return 0
    case "$sess" in "$HOLD") return 0 ;; esac
    # Read the selection before clearing it, and clear it before parking: the
    # flag is a window option, so it rides along into the holding session and
    # would come back tinted on the next unstash.
    ids=$(sel_ids "$sess")
    do_sel_cancel
    [ -n "$ids" ] || return 0
    do_stash_many $ids
    sel_notice
}

# --- closing a selection ------------------------------------------------------
#
# x: close every selected tab, after a y/n. Each pane goes through closed-tabs.sh
# `close`, the very path prefix x / CMD+W takes (snapshot, agent session id, a
# history row CMD+Z reopens from), one after the other so each row's recorded
# index is the slot that tab really left: reopened newest-first, they land back
# where they were. A space's HOME is never closed (prefix x refuses it too);
# it is skipped and the question says so.
#
# sel-kill runs in the key's foreground run-shell and only asks, in a small
# popup (status-line prompts are hidden behind the sidebar) that reads ONE key:
# ⇧Y closes, and every other key, y included, cancels and is swallowed there,
# never reaching the pane. Not prefix x's display-menu, and not a plain y: x
# is also the root twin's, live whenever a selection is, so ordinary typing
# can open the box. "proxy" / "oxygen" at speed answered a y/n menu with y,
# and a menu's own keys walk its highlight (k wraps onto the last item, G,
# Tab, the arrows), so "xkcd⏎" picked Close (both measured, review
# 2026-10-25). Capital Y, and nothing navigable, is the one rule.
# The binding re-arms the `stash` table before calling this, so a "no" leaves
# the selection exactly as it was, keys and all. Y hands sel-kill-go the ids
# the question described, in the background. (CLOSED_TABS_SH: at the top.)

# What x would do to the selection in session $1, as globals: KILL_IDS (the
# non-home selected windows, sel_order), KILL_N, KILL_AGENTS, KILL_RUN
# (running), KILL_ASK (needs-input), KILL_HOMES (selected homes, skipped) and
# KILL_ALL (1: they are every window of the session, so closing them would end
# it). With $2.. given, those ids instead of the tint (sel-kill-go: the ones the
# question named, re-checked).
sel_kill_survey() {
    local sess="$1"; shift
    local want wid home state flow cua
    KILL_IDS=""; KILL_N=0; KILL_AGENTS=0; KILL_RUN=0; KILL_ASK=0; KILL_HOMES=0; KILL_ALL=0
    if [ "$#" -gt 0 ]; then want=" $* "; else want=" $(sel_ids "$sess" | tr '\n' ' ') "; fi
    local total=0
    while IFS="$SEP" read -r wid home state flow cua; do
        [ -n "$wid" ] || continue
        total=$((total + 1))
        case "$want" in *" $wid "*) ;; *) continue ;; esac
        if [ "$home" = 1 ]; then KILL_HOMES=$((KILL_HOMES + 1)); continue; fi
        KILL_IDS="$KILL_IDS $wid"; KILL_N=$((KILL_N + 1))
        [ -n "$state" ] || continue
        KILL_AGENTS=$((KILL_AGENTS + 1))
        # Waiting on you first (the sidebar's rank puts attention before
        # work), then working exactly as the sidebar counts it
        # (agent-ui model::in_flight): running, or a workflow or cua flag
        # whatever the state. One count per agent, never both.
        if [ "$state" = needs-input ]; then
            KILL_ASK=$((KILL_ASK + 1))
        elif [ "$state" = running ] || [ -n "$flow" ] || [ -n "$cua" ]; then
            KILL_RUN=$((KILL_RUN + 1))
        fi
    done < <(sel_order_rows "$sess")
    KILL_IDS="${KILL_IDS# }"
    [ "$KILL_N" -gt 0 ] && [ "$KILL_N" -eq "$total" ] && KILL_ALL=1
    return 0
}

# sel_order's walk with each window's @space_home, @agent_state,
# @agent_workflow and @agent_cua beside it.
sel_order_rows() { sel_order "$1" rows; }

# The question, from the survey: "close 3 tabs (2 agents, 1 working, 1 needs
# input)? · home stays". Running and needs-input agents are named because
# those are the ones a close interrupts.
sel_kill_prompt() {
    local p d s=""
    [ "$KILL_N" -eq 1 ] || s=s
    p="close $KILL_N tab$s"
    if [ "$KILL_AGENTS" -gt 0 ]; then
        d="$KILL_AGENTS agent"; [ "$KILL_AGENTS" -eq 1 ] || d="${d}s"
        [ "$KILL_RUN" -gt 0 ] && d="$d, $KILL_RUN working"
        if [ "$KILL_ASK" -gt 0 ]; then
            d="$d, $KILL_ASK need"; [ "$KILL_ASK" -eq 1 ] && d="${d}s"; d="$d input"
        fi
        p="$p ($d)"
    fi
    p="$p?"
    [ "$KILL_HOMES" -gt 0 ] && p="$p · home stays"
    printf '%s' "$p"
}

do_sel_kill_prompt() {
    local cur sess
    cur=$(sel_win "${1:-}"); [ -n "$cur" ] || return 0
    sess=$(sel_sess_of "$cur"); [ -n "$sess" ] || return 0
    sel_kill_survey "$sess"
    sel_kill_prompt; echo
}

do_sel_kill() {
    local cur sess client="${2:-}" s
    cur=$(sel_win "${1:-}"); [ -n "$cur" ] || return 0
    sess=$(sel_sess_of "$cur"); [ -n "$sess" ] || return 0
    # The binding armed the table; with nothing to ask about, put the client
    # back in root, or its next l would start a new selection.
    unarm() { [ -n "$client" ] && tmux switch-client -c "$client" -T root 2>/dev/null; return 0; }
    case "$sess" in
        "$HOLD") unarm; return 0 ;;
        agents) do_sel_cancel; unarm; msg -k warn "agents windows belong to tmux-pty-mcp — not closing them"; return 0 ;;
        tasks) do_sel_cancel; unarm; msg -k warn "tasks windows belong to CuaNotch — not closing them"; return 0 ;;
    esac
    sel_kill_survey "$sess"
    if [ "$KILL_N" -eq 0 ]; then
        if [ "$KILL_HOMES" -gt 0 ]; then
            # Only the home is selected: the selection stays, as after a "no".
            notify_user ${client:+-c "$client"} -k warn -- "home is never closed · prefix Q closes the space"
        else
            unarm
        fi
        return 0
    fi
    if [ "$KILL_ALL" = 1 ]; then
        notify_user ${client:+-c "$client"} -k warn -- "closing every tab would end $sess · leave one out"
        return 0
    fi
    # Sized to its two lines (sel_kill_ask prints them), prefix x's colours.
    # Ids go in unquoted: @ plus digits, nothing a shell would touch.
    local q y w
    q=" $(sel_kill_prompt) "; y=" $(sel_kill_yes) "
    w=${#q}; [ "${#y}" -gt "$w" ] && w=${#y}
    tmux display-popup ${client:+-c "$client"} -E -b rounded -s 'bg=#1e1e2e,fg=#cdd6f4' -S 'fg=#f38ba8' \
        -T "#[align=centre] close tabs " -w $((w + 4)) -h 4 -x "$POPUP_X" -y C \
        "'$SELF' sel-kill-ask '$client' $KILL_HOMES $KILL_IDS" 2>/dev/null
    return 0
}

# The popup's second line: its one yes, and that nothing else is.
sel_kill_yes() {
    local s=s; [ "$KILL_N" -eq 1 ] && s=""
    printf '%s' "⇧Y close $KILL_N tab$s · any other key cancels"
}

# Inside the popup: the question, then one key. Re-surveyed from the ids, so
# the counts are the ones sel-kill-go will act on. Anything but Y (⏎, Esc and
# the rest of an escape sequence, a stray letter) ends the popup and goes no
# further: what is left unread dies with the popup's terminal.
do_sel_kill_ask() {
    local client="${1:-}" homes="${2:-0}"; shift 2
    local sess="" w k
    for w in "$@"; do
        sess=$(tmux display-message -p -t "$w" '#{session_name}' 2>/dev/null) && [ -n "$sess" ] && break
    done
    [ -n "$sess" ] || return 0
    sel_kill_survey "$sess" "$@"
    KILL_HOMES="$homes"
    printf ' %s\n \033[1;31m%s\033[0m' "$(sel_kill_prompt)" "$(sel_kill_yes)"
    IFS= read -rsn1 k
    [ "$k" = Y ] || return 0
    tmux run-shell -b "'$SELF' sel-kill-go '$client' $homes $*"
}

# y: the closes themselves, in a backgrounded run-shell (the menu is gone the
# moment you answer). $2 is how many homes the question said stay, $3.. the
# ids it named; anything that has since closed, moved session or become a home
# is dropped.
do_sel_kill_go() {
    local client="${1:-}" homes="${2:-0}"; shift 2
    local sess="" w s land="" act wid lastf p
    for w in "$@"; do
        s=$(tmux display-message -p -t "$w" '#{session_name}' 2>/dev/null) || continue
        [ -n "$s" ] && { sess="$s"; break; }
    done
    # The selection ends here, like every other way out of it, and the client
    # leaves the `stash` table the binding re-armed.
    do_sel_cancel
    [ -n "$client" ] && tmux switch-client -c "$client" -T root 2>/dev/null
    [ -n "$sess" ] || return 0
    case "$sess" in "$HOLD"|agents|tasks) return 0 ;; esac
    sel_kill_survey "$sess" "$@"
    [ "$KILL_N" -gt 0 ] || return 0
    [ "$KILL_ALL" = 1 ] && { notify_user ${client:+-c "$client"} -k warn -- "closing every tab would end $sess · leave one out"; return 0; }

    # Where the session lands, picked once instead of letting tmux hop through
    # the ones about to close: what a single close lands on (the last-visited
    # window) if it stays, else the window just before the first closed one in
    # sel_order (the home, in a space), else any that stays.
    while IFS="$SEP" read -r wid lastf; do
        case " $KILL_IDS " in *" $wid "*) continue ;; esac
        [ "$lastf" = 1 ] && land="$wid"
    done < <(tmux list-windows -t "=$sess" -F "#{window_id}${SEP}#{window_last_flag}" 2>/dev/null)
    if [ -z "$land" ]; then
        land=$(sel_order "$sess" | awk -v ids=" $KILL_IDS " '
            { if (index(ids, " " $0 " ")) { if (prev != "") { print prev; exit } ; hit = 1; next }
              prev = $0; if (hit) { print; exit } }')
    fi
    act=$(tmux display-message -p -t "=$sess:" '#{window_id}' 2>/dev/null)
    case " $KILL_IDS " in *" $act "*) [ -n "$land" ] && tmux select-window -t "$land" 2>/dev/null ;; esac

    local n=0
    for w in $KILL_IDS; do
        # Every pane of the tab, as prefix x on each would; a home's panes can
        # not be among them (surveyed out), and a dashboard is never a tab.
        while IFS= read -r p; do
            [ -n "$p" ] || continue
            bash "$CLOSED_TABS_SH" close "$p" </dev/null >/dev/null 2>&1
        done < <(tmux list-panes -t "$w" -F '#{?#{||:#{@space_home_pane},#{@space_dash_pane}},,#{pane_id}}' 2>/dev/null)
        # Counted only once the window is really gone: a close that failed
        # (or a pane the filter kept) leaves it standing.
        [ -n "$(tmux display-message -p -t "$w" '#{window_id}' 2>/dev/null)" ] || n=$((n + 1))
    done
    sel_notice
    local kind=ok what
    s=s; [ "$KILL_N" -eq 1 ] && s=""
    if [ "$n" -eq "$KILL_N" ]; then what="closed $n tab$s"
    elif [ "$n" -eq 0 ]; then what="couldn't close $([ "$KILL_N" -eq 1 ] && echo the tab || echo "the $KILL_N tabs")"; kind=warn
    else what="closed $n of $KILL_N tab$s"; kind=warn
    fi
    [ $((homes + KILL_HOMES)) -gt 0 ] && what="$what · home stays"
    [ "$n" -gt 0 ] && what="$what · ⌘Z reopens"
    notify_user ${client:+-c "$client"} -k "$kind" -- "$what"
    # Left the space with only its home: ask ONCE whether the space goes too
    # (the closes above are plain `close`, which never ask). A selection is
    # one session's, so this is the one space it can have emptied;
    # closed-tabs.sh decides (a visible space, never main) and asks on this
    # client, holding this backgrounded job until the box is answered.
    [ "$n" -gt 0 ] && [ -n "$client" ] && bash "$CLOSED_TABS_SH" ask-space "$client" "=$sess:" </dev/null >/dev/null 2>&1
    return 0
}

# --- sending a selection to another session -----------------------------------
#
# The selection's other exit: m (or a, the prefix+a muscle memory) raises a
# session picker shaped like prefix+a's, and the selected tabs MOVE there —
# an existing session, or a new one named by typing it. ⏎ follows them over,
# Tab sends them off and stays put.
#
# Three steps, each where it has to be. sel-send runs in the key binding's
# foreground run-shell (a popup needs a client to raise on) and does nothing
# but read the selection and open the popup — the tint stays up while you
# pick, so you can see what is about to go. send-pick runs INSIDE the popup,
# where fzf has a terminal. send-many does the moves from a backgrounded
# run-shell, for the same reason do_pick hands off: the popup closes the moment
# you pick instead of lingering over the switch. A backgrounded run-shell has
# no client of its own, so the client name is carried through all three and
# every switch-client / display-message names it explicitly.

do_sel_send() {
    local cur sess ids client="${2:-}" n
    cur=$(sel_win "${1:-}"); [ -n "$cur" ] || return 0
    sess=$(sel_sess_of "$cur"); [ -n "$sess" ] || return 0
    case "$sess" in "$HOLD") return 0 ;; esac
    # Its windows belong to tmux-pty-mcp, which tracks them by session.
    case "$sess" in
        agents) do_sel_cancel; msg -k warn "agents windows belong to tmux-pty-mcp — not moving them"; return 0 ;;
        tasks) do_sel_cancel; msg -k warn "tasks windows belong to CuaNotch — not moving them"; return 0 ;;
    esac
    ids=$(sel_ids "$sess")
    [ -n "$ids" ] || ids="$cur"
    n=$(printf '%s\n' "$ids" | wc -l | tr -d ' ')
    # Unquoted $ids: tmux window ids (@ plus digits), nothing for a shell to
    # interpret. The session is NOT passed — send-pick re-derives it from the
    # ids, so a user-chosen name never has to survive a round of quoting.
    tmux display-popup ${client:+-c "$client"} -E -w 60% -h 75% -x "$POPUP_X" \
        -T " move $n tab$([ "$n" -eq 1 ] || echo s) to… " \
        "'$SELF' send-pick '$client' $(printf '%s ' $ids)"
}

do_send_pick() {
    local client="${1:-}"; shift
    local src out status query key selection target mode=follow
    src=$(sel_sess_of "${1:-}")
    [ -n "$src" ] || { do_sel_cancel; return 0; }

    command -v fzf >/dev/null 2>&1 || { do_sel_cancel; notify_user ${client:+-c "$client"} -k error -- "stash: fzf not found"; return 0; }

    # Same list and order as prefix+a (mru-session-switch.sh): most recently
    # attached first, minus the session the tabs are already in and the ones
    # that are not places to put things.
    # The not-places are every hidden session (hidden_sessions), not a
    # hard-coded subset — `pending` and `btop-popup` were offered until
    # 2026-10-22.
    local list
    list=$(tmux list-sessions -F $'#{session_last_attached}\t#{session_name}' 2>/dev/null \
        | awk -F '\t' -v cur="$src" '$2 != cur' \
        | sort -t $'\t' -k1,1nr | cut -f2- \
        | grep -vxF -f <(hidden_sessions))

    # Tab for "stay", not ⌥⏎ or ⌃s: WezTerm's default bindings take Alt+Enter
    # for ToggleFullScreen before fzf ever sees it, and C-s is the tmux prefix.
    local fzf_cmd=(fzf --print-query --expect=tab --reverse --ansi --info=hidden
                   --prompt 'move to > '
                   --header '⏎ move & go there · Tab move & stay here · no match = new session')
    local preview="$HOME/.config/tmux/scripts/preview_session.sh"
    [ -x "$preview" ] && fzf_cmd+=(--preview "'$preview' {}" --preview-window=down:70%:nowrap:noinfo)

    out=$({ [ -n "$list" ] && printf '%s\n' "$list"; } | "${fzf_cmd[@]}")
    status=$?
    # 0 = picked a row, 1 = no row matched (the typed name is the answer);
    # anything else is Esc / ⌃c.
    if [ "$status" -ne 0 ] && [ "$status" -ne 1 ]; then do_sel_cancel; return 0; fi

    query=$(printf '%s\n' "$out" | sed -n '1p' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    key=$(printf '%s\n' "$out" | sed -n '2p')
    selection=$(printf '%s\n' "$out" | sed -n '3p')
    target="${selection:-$query}"
    [ "$key" = "tab" ] && mode=stay

    do_sel_cancel
    [ -n "$target" ] || return 0
    # Validated before it goes anywhere near a command string. No `.`: tmux
    # silently turns it into `_` in a new session's name, and reads `=a.b` as
    # session a, pane b — so the session got created as a_b, every move into
    # "a.b" then failed, and the stray session was left behind.
    if ! [[ "$target" =~ ^[A-Za-z0-9_-]+$ ]]; then
        notify_user ${client:+-c "$client"} -k warn -- "Invalid session name (allowed: A-Z a-z 0-9 _ -): $target"
        return 0
    fi
    tmux run-shell -b "'$SELF' send-many '$client' $mode '$target' $*"
}

do_send_many() {
    local client="$1" mode="$2" target="$3"; shift 3
    local w s src="" wins=() seen="" created=0 boot="" total homes=0
    # say [-k KIND] [-t SECONDS] TEXT: to $client (a backgrounded run-shell
    # has no client of its own), as is (no "stash: ").
    say() {
        local k=info t=
        [ "${1:-}" = -k ] && { k="$2"; shift 2; }
        [ "${1:-}" = -t ] && { t="$2"; shift 2; }
        notify_user ${client:+-c "$client"} -k "$k" ${t:+-t "$t"} -- "$*"
    }

    # The picker hides these, but a typed name bypasses the list. `agents` is
    # tmux-pty-mcp's: it owns windows there by @pty_* tags, and an untagged
    # window is exactly what its sweep and agent-restore-prune.sh delete.
    # Every hidden session, not a hard-coded few: `pending` (space.sh's
    # space-building claudes) and `btop-popup` got through, and ⏎ then switched
    # the client into a session that is on no bar.
    case "$target" in
        "$HOLD")  say -k warn "that's the parking session — use H to hide tabs"; return 0 ;;
    esac
    if is_hidden_session "$target"; then
        say -k warn "$target isn't a place to put tabs"; return 0
    fi

    lock_acquire || { say -k warn "stash: busy — try again"; return 0; }

    # Re-resolve under the lock: the picker may have sat open a while, and in
    # the meantime a tab can have closed, been parked, or moved elsewhere.
    for w in "$@"; do
        [ -n "$w" ] || continue
        s=$(tmux display-message -p -t "$w" '#{session_name}' 2>/dev/null) || continue
        [ -n "$s" ] && [ "$s" != "$HOLD" ] && [ "$s" != "tasks" ] || continue
        [ -n "$src" ] || src="$s"
        [ "$s" = "$src" ] || continue
        case " $seen " in *" $w "*) continue ;; esac
        seen="$seen $w"
        # A space's home stays in its space — M-m's rule, now here too (see
        # do_stash_many: a range always reaches the home first).
        if [ "$(tmux show -wqv -t "$w" @space_home 2>/dev/null)" = "1" ]; then
            homes=$((homes + 1)); continue
        fi
        wins+=("$w")
    done

    if [ "${#wins[@]}" -eq 0 ]; then
        lock_release
        if [ "$homes" -gt 0 ]; then say -k warn "that's the space's home tab — it stays"; else say -k warn "nothing to move"; fi
        return 0
    fi
    if [ "$src" = "$target" ]; then lock_release; say -k warn "already in $target"; return 0; fi

    total=$(tmux list-windows -t "=$src" -F '#{window_id}' 2>/dev/null | wc -l | tr -d ' ')
    case "$total" in ''|*[!0-9]*) total=0 ;; esac
    # Taking every tab destroys the source session, and detach-on-destroy is on:
    # fine when the client is following them (it is switched away first), but
    # staying behind would mean staying in a session that no longer exists.
    if [ "$mode" = stay ] && [ "$total" -le "${#wins[@]}" ]; then
        lock_release
        say -k warn "that's every tab in $src — leave one behind, or ⏎ to follow them"
        return 0
    fi
    # ...and following only saves THIS client. Another terminal on the same
    # session would be dropped to a shell when it is destroyed.
    if [ "$total" -le "${#wins[@]}" ] &&
       [ "$(tmux display-message -p -t "=$src:" '#{session_attached}' 2>/dev/null)" -gt 1 ] 2>/dev/null; then
        lock_release
        say -k warn "$src is open in another terminal — leave one tab behind"
        return 0
    fi

    # A session cannot be born empty: create it with a placeholder and kill
    # that once the real tabs are in (the ensure_hold pattern, -P -F for the
    # same reason — only ever kill the window THIS call created).
    if ! tmux has-session -t "=$target" 2>/dev/null; then
        boot=$(tmux new-session -d -s "$target" -c "$HOME" -P -F '#{window_id}' 2>/dev/null) || boot=""
        [ -n "$boot" ] || { lock_release; say -k error "could not create session $target"; return 0; }
        created=1
    fi

    # The client switches AFTER the tabs have landed and the placeholder is
    # gone, so the first frame of the target session is the finished result.
    # Switching first (the original order) painted the target mid-move — for a
    # new session, its placeholder shell — and then repainted it tab by tab.
    #
    # The one exception: when the selection is every tab in the source, the
    # last move destroys that session, and with detach-on-destroy on the client
    # must already be elsewhere by then. So that last tab is held back until
    # after the switch.
    local moved=() failed=0 last="" note="" left switched=0
    if [ "$mode" = follow ] && [ "$total" -le "${#wins[@]}" ]; then
        last="${wins[${#wins[@]}-1]}"
        unset 'wins[${#wins[@]}-1]'
    fi
    move_to_target() {
        if tmux move-window -d -a -s "$1" -t "=$target:{end}" 2>/dev/null; then moved+=("$1"); else failed=$((failed + 1)); fi
    }
    # A placeholder may only go once something real is in the session.
    drop_boot() {
        [ -n "$boot" ] && [ "${#moved[@]}" -gt 0 ] || return 0
        tmux kill-window -t "$boot" 2>/dev/null; boot=""
    }
    for w in ${wins[@]+"${wins[@]}"}; do
        # Re-count per move unless emptying the source is the plan: an
        # unselected tab's shell can exit mid-batch (the lock cannot hold it
        # back), and the next move would then empty the session under an
        # attached client — do_stash_many's "N of N+1" case.
        if [ -z "$last" ]; then
            left=$(tmux list-windows -t "=$src" -F '#{window_id}' 2>/dev/null | wc -l | tr -d ' ')
            case "$left" in ''|*[!0-9]*) left=0 ;; esac
            if [ "$left" -le 1 ]; then note="stopped — the rest is all that's left in $src"; break; fi
        fi
        move_to_target "$w"
    done
    drop_boot

    if [ "$mode" = follow ] && { [ "${#moved[@]}" -gt 0 ] || [ -n "$last" ]; }; then
        [ "${#moved[@]}" -gt 0 ] && tmux select-window -t "${moved[0]}" 2>/dev/null
        tmux switch-client ${client:+-c "$client"} -t "=$target" 2>/dev/null && switched=1
    fi
    if [ -n "$last" ]; then
        move_to_target "$last"
        drop_boot
        [ "${#moved[@]}" -gt 0 ] && tmux select-window -t "${moved[0]}" 2>/dev/null
    fi

    if [ "${#moved[@]}" -eq 0 ]; then
        # Nothing moved, so the source still exists: go back there BEFORE
        # removing a session this call created, or the client would be
        # destroyed along with it.
        [ "$switched" = 1 ] && tmux switch-client ${client:+-c "$client"} -t "=$src" 2>/dev/null
        [ "$created" = 1 ] && tmux kill-session -t "=$target" 2>/dev/null
        lock_release
        say -k error "could not move them"
        return 0
    fi
    # Callers renumber, then publish (see renumber()): windows carrying a
    # pending @stash_session are mirrored by session:index, and the
    # window-unlinked publishes fired during the moves saw the old indexes.
    renumber "$src" "$target"
    publish
    lock_release
    tmux refresh-client ${client:+-t "$client"} 2>/dev/null

    # No message on a clean follow: you are looking at the result, and a
    # display-message replaces the whole tab bar for display-time (4s here) —
    # which read as the screen being broken right when you want to see where
    # the tabs landed. Staying behind has nothing else to show, so it gets a
    # SHORT one; failures always get one.
    local n="${#moved[@]}" report
    report="moved $n tab$([ "$n" -eq 1 ] || echo s) to $target$([ "$created" = 1 ] && echo ' (new)')"
    if [ "$failed" -gt 0 ] || [ "$homes" -gt 0 ] || [ -n "$note" ]; then
        [ "$failed" -gt 0 ] && report="$report — could not move $failed"
        [ "$homes" -gt 0 ]  && report="$report — the home tab stays"
        [ -n "$note" ]      && report="$report — $note"
        if [ "$failed" -gt 0 ]; then say -k error "$report"; else say -k warn "$report"; fi
    elif [ "$mode" = stay ]; then
        say -k ok -t 1.5 "$report"
    fi
}

# Windows that are NOT parked but still carry a suspended session — a resume
# that was declined because the pane was busy. Before this existed the advice
# "prefix+h again once it's at a prompt" was impossible to follow: do_unstash
# only ever looked inside the holding session, and the window had already gone
# home, so the agent was dead with no key that could reach it.
#
# Fields are $SEP-delimited. They were concatenated with no separator at first,
# and "the last character is the flag" is wrong the moment a window id ends in
# 1: `agents@71` with NO suspended session parsed as flag=1 and yielded the
# truncated id `@7`. Every window whose id ended in 1 was a false positive.
pending_windows() {
    local sess wid flag
    while IFS="$SEP" read -r sess wid flag; do
        [ "$flag" = "1" ] || continue
        [ "$sess" = "$HOLD" ] && continue
        [ -n "$wid" ] && printf '%s\n' "$wid"
    done < <(tmux list-windows -a -F "#{session_name}${SEP}#{window_id}${SEP}#{?#{@stash_session},1,}" 2>/dev/null)
}

do_unstash() {
    local win="${1:-}" client="${2:-}"
    # No window named: the merged picker, scoped to what is stashed (pinned
    # ⌘Z entries and the parked windows; `closed-tabs.sh pick-popup
    # --stashed`). It raises its own popup on the client's terminal. Without
    # closed-tabs.sh, the old picker below.
    if [ -z "$win" ] && [ -f "$CLOSED_TABS_SH" ]; then
        local c_sess c_tty
        IFS="$SEP" read -r c_sess c_tty < <(tmux display-message -p ${client:+-c "$client"} "#{session_id}${SEP}#{client_tty}" 2>/dev/null)
        bash "$CLOSED_TABS_SH" pick-popup --stashed "${c_sess:-}" "${c_tty:-$client}" </dev/null
        return 0
    fi
    if [ -z "$win" ] && ! hold_exists; then
        # Nothing parked, but a pending resume may still be waiting.
        local p; p=$(pending_windows | head -1)
        if [ -n "$p" ]; then
            # Backgrounded: see the note on the other hand-off below.
            tmux run-shell -b "'$SELF' resume '$p'"
            return 0
        fi
        msg -k warn "nothing is parked"
        return 0
    fi

    if [ -z "$win" ]; then
        # The picker lists pending resumes too (do_pick), so they stay
        # reachable while anything else is parked.
        if [ "$(count_windows)" -ge 1 ]; then
            # Even for a single parked window: the popup's preview shows what is
            # about to come back, and ⌃x kill is only reachable from here.
            # fzf needs a terminal, so the choosing happens inside a popup that
            # re-enters this script as `pick`. Deciding here rather than in an
            # if-shell in tmux.conf keeps the branch in one place and avoids a
            # second layer of shell quoting inside a tmux command string.
            # Deliberately outside the lock: the popup waits on a human.
            tmux display-popup ${client:+-c "$client"} -E -w "${POPUP_W_PCT}%" -h "${POPUP_H_PCT}%" -x "$POPUP_X" -T ' stashed tabs ' "'$SELF' pick '$client'"
            return 0
        fi
    fi

    do_unstash_many ${client:+-c "$client"} "$win"
}

# Bring one or more parked windows back, in a single transaction for the same
# reasons do_stash_many is one.
#
# `-c <client>` names the client to take to them. A window goes home to its
# ORIGIN session, which is often not the one you are looking at when you pick
# it — and select-window alone only changes that session's current window, so
# the tab came back somewhere off-screen and you had to go and find it. The
# client is passed in explicitly because this runs from a backgrounded
# run-shell (see do_pick), which has no client of its own; same arrangement as
# do_send_many.
do_unstash_many() {
    local client=""
    if [ "${1:-}" = "-c" ]; then client="${2:-}"; shift 2; fi
    local w win origin origins=() ordered=() wanted=" $* " unsnoozed=""

    # Ids NAMED here that are pending resumes rather than parked windows: home
    # already, agent dead, @stash_session still set (a resume refused because
    # the pane was busy). The picker lists them, and filtering them out against
    # $HOLD below — as this did until 2026-10-22 — left no key that could
    # resume one while anything else was parked. They get the resume, nothing
    # else: there is nothing to move.
    local pend=()
    if [ "$wanted" != "  " ]; then
        while read -r w; do
            case "$wanted" in *" $w "*) pend+=("$w") ;; esac
        done < <(pending_windows)
    fi

    if ! hold_exists; then
        [ "${#pend[@]}" -gt 0 ] && { resume_pending "$client" "${pend[@]}"; return 0; }
        msg -k warn "nothing is parked"; return 0
    fi

    lock_acquire || { msg -k warn "busy — try again"; return 0; }   # 0: see the note on the refusal below

    # Re-resolve under the lock. Picking "the only parked window" before taking
    # it meant two unstashes could select the same window, and the second
    # move-window then acted on one that had already gone home.
    #
    # Walking the holding session rather than the argument list does three jobs
    # at once: it drops ids that are no longer parked, de-dupes, and puts the
    # group back in the order it sits in — so tabs parked together come home in
    # the same relative order rather than the order fzf happened to report them.
    while read -r w; do
        [ -n "$w" ] || continue
        case "$wanted" in
            "  ") ordered+=("$w"); break ;;      # no ids given: the first parked one
            *" $w "*) ordered+=("$w") ;;
        esac
    done < <(tmux list-windows -t "=$HOLD" -F '#{window_id}' 2>/dev/null)

    if [ "${#ordered[@]}" -eq 0 ]; then
        lock_release
        [ "${#pend[@]}" -gt 0 ] && { resume_pending "$client" "${pend[@]}"; return 0; }
        msg -k warn "nothing is parked"
        return 0
    fi

    local restored=() failed=0
    for win in "${ordered[@]}"; do
        # Home if it still exists, otherwise wherever we are now — a parked
        # window must never become unreachable because its origin session was
        # closed. Resolved per window: a batch can span origins.
        origin=$(tmux show -wqv -t "$win" @stash_origin 2>/dev/null)
        if [ -z "$origin" ] || [ "$origin" = "$HOLD" ] || ! tmux has-session -t "=$origin" 2>/dev/null; then
            # The session the CLIENT is looking at first, when there is one —
            # that is "wherever we are now". The bare display-message below is
            # only right inside tmux: from the sidebar (run-shell sent from
            # outside tmux) it answered tmux's best session, which was `agents`
            # whenever tmux-pty-mcp had just opened a window there.
            origin=""
            [ -n "$client" ] && origin=$(tmux display-message -p -c "$client" '#{client_session}' 2>/dev/null)
            [ -n "$origin" ] || origin=$(tmux display-message -p '#{session_name}' 2>/dev/null)
            # ...but the bare form resolves by tmux's most-recently-active
            # rules, not "the session the client is looking at", and the
            # holding session is the newest thing on the server whenever
            # anything is parked — so it can answer `stash`, and moving a parked
            # window to `stash` succeeds while leaving it exactly where it was:
            # parked, with its origin now cleared, and no way left to tell it
            # had ever been anywhere else. It can equally answer `agents`,
            # `tasks` or `pending` — a hidden session, where the tab is off
            # every bar and the client is then switched INTO it (2026-10-22:
            # landed in tmux-pty-mcp's `agents` from the sidebar). Every hidden
            # session is refused, not only $HOLD.
            #
            # Then prefer a session someone is actually ATTACHED to.
            # `list-sessions` is sorted by name, so taking its head is
            # "alphabetically first", which on this machine is `agents` — the
            # window would land somewhere the user is not looking, and the
            # select-window below would then quietly change THAT session's
            # current window instead. -F to match literally: a session name is
            # user-chosen and may contain regex metacharacters. With no visible
            # session left at all the window stays parked (counted as failed):
            # still reachable, unlike one moved into a hidden session.
            if [ -z "$origin" ] || is_hidden_session "$origin"; then
                local hidden; hidden=$(hidden_sessions)
                origin=$(tmux list-sessions -F '#{?session_attached,#{session_name},}' 2>/dev/null |
                         grep -v '^$' | grep -vxF -f <(printf '%s\n' "$hidden") | head -1)
                [ -n "$origin" ] || origin=$(tmux list-sessions -F '#{session_name}' 2>/dev/null |
                                             grep -vxF -f <(printf '%s\n' "$hidden") | head -1)
            fi
        fi
        if [ -z "$origin" ]; then
            failed=$((failed + 1))
            log "no session left to bring $win back to"
            continue
        fi
        if ! tmux move-window -s "$win" -t "$origin": 2>/dev/null; then
            failed=$((failed + 1))
            continue
        fi
        tmux set-option -uw -t "$win" @stash_origin 2>/dev/null
        tmux set-option -uw -t "$win" @stash_label 2>/dev/null
        # Brought back before its alarm: the snooze is over too.
        if [ -n "$(tmux show -wqv -t "$win" @stash_wake 2>/dev/null)" ]; then
            tmux set-option -uw -t "$win" @stash_wake 2>/dev/null
            unsnoozed=1
        fi
        # An ARRAY, not a space-joined string. Session names are user-chosen and
        # may contain spaces (or glob metacharacters), and the string form both
        # word-split `my project` into two bogus targets — silently skipping the
        # renumber, so the origin kept its index hole forever — and exposed the
        # name to pathname expansion.
        local seen=0 o
        for o in ${origins[@]+"${origins[@]}"}; do
            [ "$o" = "$origin" ] && { seen=1; break; }
        done
        [ "$seen" = "1" ] || origins+=("$origin")
        restored+=("$win")
    done

    if [ "${#restored[@]}" -eq 0 ]; then
        lock_release
        [ "${#pend[@]}" -gt 0 ] && { resume_pending "$client" "${pend[@]}"; return 0; }
        msg -k error "could not bring it back"; return 0
    fi
    # Land on the first of the group, so a multi-tab restore leaves you at the
    # left end of what just came back rather than on whichever one moved last.
    tmux select-window -t "${restored[0]}" 2>/dev/null
    # ...and take the client there if that is another session. Skipped when it
    # is already there, so a restore into the current session does not
    # overwrite last-session (prefix+L) with itself.
    if [ -n "$client" ]; then
        local dest; dest=$(tmux display-message -p -t "${restored[0]}" '#{session_name}' 2>/dev/null)
        if [ -n "$dest" ] && [ "$dest" != "$(tmux display-message -p -c "$client" '#{client_session}' 2>/dev/null)" ]; then
            tmux switch-client -c "$client" -t "=$dest" 2>/dev/null
        fi
    fi
    renumber "$HOLD" ${origins[@]+"${origins[@]}"}
    publish
    lock_release
    [ -n "$unsnoozed" ] && reschedule_snooze
    [ "$failed" -gt 0 ] && msg -k error "brought back ${#restored[@]}, could not bring back $failed"

    # Handed to a BACKGROUNDED run-shell, never called inline. prefix+h is a
    # foreground run-shell (it has to be — the picker needs a client to raise a
    # popup on), and tmux dispatches key events through the same client command
    # queue that a blocking run-shell item occupies. So polling inline froze the
    # client's keyboard for the whole resume: measured 96.9s on a session that
    # never registered, with everything dead — typing, prefix chords, even
    # prefix+d — and keys typed during a 90s block discarded outright rather
    # than replayed.
    #
    # One per window, and deliberately concurrent: each polls for up to 90s for
    # its OWN agent to register, so running them in series would make the last
    # tab of a group wait out every tab before it. They touch different windows
    # and the sidecar write they each trigger is a rename, so the worst case is
    # a redundant mirror, not a lost id.
    for win in "${restored[@]}"; do
        tmux run-shell -b "'$SELF' resume '$win'"
    done
    # Frozen jobs come back the same way, backgrounded (do_thaw waits up to
    # 2s per pane for the job to run again): `fg`, or a re-run after a restart.
    for win in "${restored[@]}"; do
        [ -n "$(tmux show -wqv -t "$win" @stash_frozen 2>/dev/null)" ] || continue
        tmux run-shell -b "'$SELF' thaw '$win' '$client'"
    done
    # Pending resumes picked alongside: resumed in place, and the client stays
    # on the first window that MOVED (that is what came back on screen).
    for win in ${pend[@]+"${pend[@]}"}; do
        tmux run-shell -b "'$SELF' resume '$win'"
    done
}

# Resume windows that are home already (see pending_windows) and take the
# client to the first, as an unpark would. Same backgrounded hand-off as
# do_unstash_many's, for the same reason.
resume_pending() {
    local client="$1" win dest; shift
    [ "$#" -gt 0 ] || return 0
    tmux select-window -t "$1" 2>/dev/null
    if [ -n "$client" ]; then
        dest=$(tmux display-message -p -t "$1" '#{session_name}' 2>/dev/null)
        if [ -n "$dest" ] && [ "$dest" != "$(tmux display-message -p -c "$client" '#{client_session}' 2>/dev/null)" ]; then
            tmux switch-client -c "$client" -t "=$dest" 2>/dev/null
        fi
    fi
    for win in "$@"; do
        tmux run-shell -b "'$SELF' resume '$win'"
    done
}

# Destroy parked windows outright — the picker's other exit. Killing is the
# one stash verb that cannot be undone, so it never guesses:
#  - it acts only on windows still parked at the moment it runs (the picker
#    may have sat open a while), re-resolved under the lock;
#  - every discarded session id goes to the LOG first, with the exact command
#    that resumes it by hand — the picker showed a label, not a conversation,
#    and a mispick must stay recoverable;
#  - a still-live agent (a park whose suspend was refused) gets SIGTERM and a
#    short grace before the window goes, so it reaps its MCP children and
#    deletes its own session file — a bare kill-window is a SIGHUP, the crash
#    path that fires no SessionEnd;
#  - the sid leaves the SIDECAR before the window dies. kill-window fires the
#    window-unlinked hook, whose backgrounded publish merges the old sidecar
#    against a world where the window is gone and the sid is not live — which
#    is exactly save_state's cue to move the row to the orphans file,
#    resurrecting as clutter the conversation the user just discarded. The
#    options are unset first for the same reason: a concurrent snapshot that
#    catches the window still alive would re-mirror them. If the kill then
#    FAILS, the options and mirror are restored — until then the log line
#    already holds the pointer, so no window exists where it is nowhere.
#
# Discarding is not destroying any more (2026-11-11): each window is closed
# through `closed-tabs.sh close`, pane by pane, so it lands in the ⌘Z backlog
# like any closed tab (undoable for a month). It is moved back to its origin
# first (`-d`: the session's current tab does not change), so the entry
# remembers the session it belongs to rather than the hidden holding one, and
# a reopen does not land in `stash`. Without closed-tabs.sh, or if it leaves
# the window standing, a plain kill-window as before.
close_parked_window() {   # <win>
    local win="$1" origin p
    if [ -f "$CLOSED_TABS_SH" ]; then
        origin=$(tmux show -wqv -t "$win" @stash_origin 2>/dev/null)
        if [ -n "$origin" ] && [ "$origin" != "$HOLD" ] && ! is_hidden_session "$origin" \
           && tmux has-session -t "=$origin" 2>/dev/null; then
            tmux move-window -d -s "$win" -t "=$origin:" 2>/dev/null \
                || log "could not move $win to $origin before closing it — its ⌘Z entry says $HOLD"
        fi
        # A frozen job dies with its shell's SIGHUP (the shell continues and
        # hangs up its stopped jobs); nothing to thaw first.
        while IFS= read -r p; do
            [ -n "$p" ] || continue
            bash "$CLOSED_TABS_SH" close "$p" </dev/null >/dev/null 2>&1
        done < <(tmux list-panes -t "$win" -F '#{pane_id}' 2>/dev/null)
        [ -z "$(tmux display-message -p -t "$win" '#{window_id}' 2>/dev/null)" ] && return 0
        log "closed-tabs.sh left $win standing — killing it"
    fi
    tmux kill-window -t "$win" 2>/dev/null
}

do_kill_many() {
    local w win sid cwd lbl pidx apid i killed=0 nsids=0
    hold_exists || { msg -k warn "nothing is parked"; return 0; }
    lock_acquire || { msg -k warn "busy — try again"; return 0; }

    local ordered=() wanted=" $* "
    while read -r w; do
        [ -n "$w" ] || continue
        case "$wanted" in *" $w "*) ordered+=("$w") ;; esac
    done < <(tmux list-windows -t "=$HOLD" -F '#{window_id}' 2>/dev/null)
    if [ "${#ordered[@]}" -eq 0 ]; then
        lock_release; msg -k warn "nothing to kill — not parked any more"; return 0
    fi

    local had_wake=""
    for win in "${ordered[@]}"; do
        # A snoozed window discarded: its alarm goes with it (rescheduled
        # below, whether or not the close works — a re-read is harmless).
        [ -n "$(tmux show -wqv -t "$win" @stash_wake 2>/dev/null)" ] && had_wake=1
        sid=$(tmux show -wqv -t "$win" @stash_session 2>/dev/null)
        cwd=$(tmux show -wqv -t "$win" @stash_cwd 2>/dev/null)
        pidx=$(tmux show -wqv -t "$win" @stash_pane_idx 2>/dev/null)
        lbl=$(tmux show -wqv -t "$win" @stash_label 2>/dev/null)
        [ -n "$lbl" ] || lbl=$(tmux display-message -p -t "$win" '#{window_name}' 2>/dev/null)

        if [ -n "$sid" ]; then
            log "killing parked window $win ($lbl) — discarding suspended session ${sid}; resume by hand: cd ${cwd:-?} && claude --resume $sid"
            tmux set-option -uw -t "$win" @stash_session 2>/dev/null
            tmux set-option -uw -t "$win" @stash_cwd 2>/dev/null
            tmux set-option -uw -t "$win" @stash_pane_idx 2>/dev/null
            forget_sids "$sid"
            nsids=$((nsids + 1))
        fi

        # A still-live agent (a window parked "left running" before
        # 2026-11-11). With closed-tabs.sh the close does the SIGTERM itself,
        # AFTER copying the agent's state, so its ⌘Z entry resumes the
        # conversation; a SIGTERM here first would leave it no session id.
        # A window with a suspended session (@stash_session: dead agent, id on
        # the window) keeps the old path end to end — the id to the log, a
        # plain kill-window — until its migration.
        apid=$(agent_pid_in_window "$win")
        local via_closed=""
        [ -z "$sid" ] && [ -f "$CLOSED_TABS_SH" ] && via_closed=1
        if [ -n "$apid" ] && [ -z "$via_closed" ]; then
            log "killing parked window $win ($lbl) — its agent (pid $apid) is still live; SIGTERM first"
            kill -TERM "$apid" 2>/dev/null
            i=0
            while [ "$i" -lt 12 ] && kill -0 "$apid" 2>/dev/null; do sleep 0.25; i=$((i + 1)); done
        elif [ -n "$apid" ]; then
            log "closing parked window $win ($lbl) through closed-tabs.sh — its live agent (pid $apid) goes to the ⌘Z backlog with its session"
        fi

        local gone=1
        if [ -n "$via_closed" ]; then close_parked_window "$win" || gone=""
        else tmux kill-window -t "$win" 2>/dev/null || gone=""
        fi
        if [ -n "$gone" ]; then
            killed=$((killed + 1))
            # After the kill, not beside forget_sids: a failed kill restores
            # the record, and its snapshot should still be there to back it.
            [ -n "$sid" ] && rm -f "$PREVIEW_DIR/$sid.ansi" 2>/dev/null
            [ -z "$sid" ] && [ -z "$apid" ] && log "killed parked window $win ($lbl)"
        else
            if [ -n "$sid" ]; then
                tmux set-option -w -t "$win" @stash_session "$sid" 2>/dev/null
                [ -n "$cwd" ]  && tmux set-option -w -t "$win" @stash_cwd "$cwd" 2>/dev/null
                [ -n "$pidx" ] && tmux set-option -w -t "$win" @stash_pane_idx "$pidx" 2>/dev/null
                save_state
                log "could not kill $win — restored its suspended-session record"
            else
                log "could not kill $win"
            fi
        fi
    done

    renumber "$HOLD"
    publish
    lock_release
    [ -n "$had_wake" ] && reschedule_snooze
    if [ "$killed" -gt 0 ]; then
        if [ "$nsids" -gt 0 ]; then
            msg -k ok "killed $killed — $nsids suspended session(s) discarded, resume commands in $(basename "$LOGFILE")"
        else
            msg -k ok "closed $killed · ⌘Z reopens"
        fi
    else
        msg -k error "could not kill it"
    fi
}

# Runs inside the popup, where there is a real terminal for fzf.
#
# --multi so a group parked together can come back together. Shift-Up/Shift-Down
# are bound alongside fzf's own Tab/Shift-Tab because shift+arrow is the gesture
# this pairs with on the tab-bar side, and having the two halves of the feature
# answer to the same key is most of what makes it memorable.
#
# What the preview pane shows for one parked window: the snapshot taken at
# suspend time if the agent was suspended (the live pane is a dead shell by
# then), otherwise the pane as it looks right now — an agent left running, or
# a plain shell, still has a real screen to show. The live capture is NOT
# resized to fit: fzf clips the right edge, and reflowing a running agent's
# window once per cursor movement in the picker is worse than a clipped edge.
do_preview() {
    local win="${1:-}" sid pidx pane=""
    [ -n "$win" ] || return 0
    sid=$(tmux show -wqv -t "$win" @stash_session 2>/dev/null)
    if [ -n "$sid" ] && [ -f "$PREVIEW_DIR/$sid.ansi" ]; then
        cat "$PREVIEW_DIR/$sid.ansi"
        return 0
    fi
    # The agent's pane if one was recorded, the active pane otherwise — the
    # same preference resume_agent has, for the same reason.
    pidx=$(tmux show -wqv -t "$win" @stash_pane_idx 2>/dev/null)
    if [ -n "$pidx" ]; then
        pane=$(tmux list-panes -t "$win" -F '#{pane_index} #{pane_id}' 2>/dev/null \
               | awk -v i="$pidx" '$1==i{print $2}')
    fi
    [ -n "$pane" ] || pane=$(tmux list-panes -t "$win" -F '#{?pane_active,#{pane_id},}' 2>/dev/null \
                             | grep -v '^$' | head -1)
    # An empty target is the CURRENT pane, not a no-op — never capture -t ''.
    [ -n "$pane" ] || return 0
    tmux capture-pane -ep -t "$pane" 2>/dev/null
}

# ⌃x kills instead of restoring — same selection semantics as ⏎, via fzf's
# --expect, which prefixes the output with the key that accepted it (empty
# line for a plain Enter). ⌃x because fzf already means something by most
# mnemonic keys (⌃k is line-up, ⌃d is deselect-all here) and ⌃x is unbound.
#
# Laid out like closed-tabs.sh's picker — "when · what · label — description ·
# where", newest first, preview underneath at the size snapshot_pane captures
# at — so the two popups read the same. The description is @stash_desc,
# written by `describe` a few seconds after the park.
ago() {
    local s=$(( $2 - $1 ))
    if   [ "$s" -lt 60 ];    then REPLY="just now"
    elif [ "$s" -lt 3600 ];  then REPLY="$((s / 60))m ago"
    elif [ "$s" -lt 86400 ]; then REPLY="$((s / 3600))h ago"
    else                          REPLY="$((s / 86400))d ago"
    fi
}
do_pick() {
    local client="${1:-}" out key wins now; now=$(date +%s)
    out=$(pick_rows "$now" \
          | fzf --ansi --with-nth=2.. --delimiter='\t' --reverse --prompt='bring back > ' \
                --multi \
                --expect=ctrl-x \
                --bind 'shift-down:toggle+down,shift-up:toggle+up,ctrl-a:select-all,ctrl-d:deselect-all' \
                --preview "'$SELF' preview {1}" \
                --preview-window "down,${PREVIEW_PCT}%,border-top,follow" \
                --header '⏎ bring back · Tab or ⇧↑/⇧↓ to pick several · ⌃a all · ⌃x kill')
    key=${out%%$'\n'*}
    wins=$(printf '%s\n' "$out" | tail -n +2 | cut -f1 | tr '\n' ' ')
    # Hand off rather than doing the work here. the resume polls for up to
    # RESUME_WAIT, and display-popup -E keeps the popup on screen — holding the
    # keyboard and covering the windows it just restored — until its command
    # exits. Backgrounding lets the popup close the moment you pick.
    #
    # Unquoted on purpose: this is a list of ids for the command line to split.
    # They are tmux window ids (@ plus digits), so there is nothing in them for
    # a shell to interpret.
    [ -n "${wins// /}" ] || return 0
    if [ "$key" = "ctrl-x" ]; then
        tmux run-shell -b "'$SELF' kill-many $wins"
    else
        tmux run-shell -b "'$SELF' unstash-many ${client:+-c '$client' }$wins"
    fi
}

# The picker's rows: every parked window, newest first, then every pending
# resume (pending_windows) — marked, since those are on the tab bar already
# and ⏎ only resumes their agent. Without them a refused resume could not be
# retried from the keyboard while anything else was parked. ⌃x on one is a
# no-op: kill-many acts on parked windows only.
pick_rows() {
    local now="$1" p
    local dim=$'\e[2m' mauve=$'\e[38;2;203;166;247m' teal=$'\e[38;2;148;226;213m' off=$'\e[0m'
    local sub=$'\e[38;2;166;173;200m' peach=$'\e[38;2;250;179;135m'
    local id ts sid cmd label cwd desc tool c where
    tmux list-windows -t "=$HOLD" \
            -F "#{window_id}${SEP}#{@stash_ts}${SEP}#{@stash_session}${SEP}#{pane_current_command}${SEP}#{?#{@stash_label},#{@stash_label},#{?#{@agent_summary},#{@agent_summary},#{window_name}}}${SEP}#{?#{@stash_cwd},#{@stash_cwd},#{pane_current_path}}${SEP}#{@stash_desc}" \
            2>/dev/null \
          | sort -t "$SEP" -k2,2nr \
          | while IFS="$SEP" read -r id ts sid cmd label cwd desc; do
              [ -n "$id" ] || continue
              # claude's process title is its version string.
              case "$cmd" in
                  [0-9]*.[0-9]*) tool=claude ;;
                  zsh|bash|sh|fish|-zsh) tool=shell ;;
                  *) tool=$cmd ;;
              esac
              [ -n "$sid" ] && tool=claude
              c="$teal"; [ "$tool" = claude ] && c="$mauve"
              if [ -n "$ts" ]; then ago "$ts" "$now"; else REPLY="?"; fi
              [ -n "$desc" ] && desc="  ${sub}— ${desc}${off}"
              printf '%s\t%s%-9s%s %s%-9s%s %s%s\t%s%s%s\n' "$id" \
                  "$dim" "$REPLY" "$off" "$c" "$tool" "$off" "$label" "$desc" \
                  "$dim" "${cwd/#"$HOME"/\~}" "$off"
            done
    for p in $(pending_windows); do
        label="" cwd="" where=""
        IFS="$SEP" read -r label cwd where < <(tmux display-message -p -t "$p" \
            "#{?#{@stash_label},#{@stash_label},#{?#{@agent_summary},#{@agent_summary},#{window_name}}}${SEP}#{?#{@stash_cwd},#{@stash_cwd},#{pane_current_path}}${SEP}#{session_name}" 2>/dev/null)
        printf '%s\t%s%-9s%s %s%-9s%s %s  %s— resume pending, in %s%s\t%s%s%s\n' "$p" \
            "$peach" "pending" "$off" "$mauve" "claude" "$off" "${label:-$p}" "$sub" "${where:-?}" "$off" \
            "$dim" "${cwd/#"$HOME"/\~}" "$off"
    done
    return 0
}

# Detached, one per parked window (do_stash_many starts it): a one-line
# account of what the window was doing, from closed-tabs.sh's `summarize`
# (codex -> claude -> copilot through ai-exec), stored as @stash_desc for the
# picker. Reads the claude
# transcript when the window holds one, plus the screen either way.
do_describe() {
    local win="${1:-}" agent sid="" cwd label ts shot desc
    [ -n "$win" ] || return 0
    local summarize="$HOME/.config/tmux/scripts/closed-tabs.sh"
    [ -x "$summarize" ] || return 0
    ts=$(tmux show -wqv -t "$win" @stash_ts 2>/dev/null)
    agent=$(window_agent "$win")
    if [ -n "$agent" ]; then
        sid=${agent#*"$SEP"}; sid=${sid%%"$SEP"*}
    else
        sid=$(tmux show -wqv -t "$win" @stash_session 2>/dev/null)
    fi
    label=$(tmux show -wqv -t "$win" @stash_label 2>/dev/null)
    [ -n "$label" ] || label=$(tmux display-message -p -t "$win" '#{window_name}' 2>/dev/null)
    cwd=$(tmux display-message -p -t "$win" '#{pane_current_path}' 2>/dev/null)
    shot=$(mktemp)
    # The window's active pane. An empty target is the CURRENT pane — never
    # capture -t ''.
    local pane; pane=$(tmux display-message -p -t "$win" '#{pane_id}' 2>/dev/null)
    [ -n "$pane" ] && tmux capture-pane -ep -t "$pane" >"$shot" 2>/dev/null
    desc=$("$summarize" summarize "${sid:+claude}" "$sid" "$cwd" "$label" "$shot")
    rm -f "$shot"
    [ -n "$desc" ] || return 0
    # Only if it's still the same park: a window brought back (or re-parked,
    # which re-describes) in the meantime shouldn't get this one.
    [ "$(tmux display-message -p -t "$win" '#{session_name}' 2>/dev/null)" = "$HOLD" ] || return 0
    [ "$(tmux show -wqv -t "$win" @stash_ts 2>/dev/null)" = "$ts" ] || return 0
    tmux set-option -w -t "$win" @stash_desc "$desc" 2>/dev/null
    log "described $win: $desc"
}

do_list() {
    if hold_exists; then
        tmux list-windows -t "=$HOLD" \
            -F '  #{window_id}  from=#{?#{@stash_origin},#{@stash_origin},?}  #{?#{@stash_session},[suspended] ,}#{?#{@stash_cold},[cold] ,#{?#{@stash_frozen},[frozen] ,}}#{?#{@stash_label},#{@stash_label},#{?#{@agent_summary},#{@agent_summary},#{window_name}}}#{?#{@stash_desc},  — #{@stash_desc},}'
    else
        echo "  nothing parked"
    fi

    # Windows that came home but whose agent has not been resumed yet — the
    # "pane was busy" case. prefix+h reaches these, but they are invisible
    # otherwise, so say so.
    local p sid lbl
    for p in $(pending_windows); do
        sid=$(tmux show -wqv -t "$p" @stash_session 2>/dev/null)
        lbl=$(tmux show -wqv -t "$p" @stash_label 2>/dev/null)
        printf '  %s  [resume pending] %s  (prefix+h, or: claude --resume %s)\n' \
            "$p" "${lbl:-$p}" "$sid"
    done

    # Suspended sessions whose window could not be identified after a restart.
    # These are the ones with no tmux state left at all, so print the command
    # that gets them back — this listing is the only place the id still exists.
    local of; of=$(orphan_file)
    # Plus rows still waiting in pending files (orphans_append's no-lock path)
    # for the next save to merge.
    local pend=() p
    for p in "$of".pending.*; do
        [[ $p =~ \.pending\.[0-9]+$ ]] && [ -s "$p" ] && pend+=("$p")
    done
    if [ -s "$of" ] || [ "${#pend[@]}" -gt 0 ]; then
        local orows; orows=$(cat "$of" ${pend[@]+"${pend[@]}"} 2>/dev/null)
        # A row whose session the user already resumed by hand is not lost —
        # it is running again, with no @stash_session on any window, so the
        # reconcile cannot see it. It is listed apart, not hidden: its row is
        # still in the file (and must stay — see orphans_reconcile), and the
        # moment that claude exits it is lost again and goes back up there.
        local running; running=$(running_claude_sids $(printf '%s\n' "$orows" | awk -F"$SEP" '$7 != "" { print $7 }'))
        # Trailing _ts/_icwd/_extra keep cwd clean on 9/10-field rows (see
        # save_state). The pane directory is the next-best guess when no cwd
        # was recorded — better than the `cd ?` that cannot be pasted.
        local _ts _icwd _extra seen=" " hit r_pid r_where lost="" back=""
        while IFS="$SEP" read -r sess idx name pidx origin label sid cwd _ts _icwd _extra; do
            [ -n "$sid" ] || continue
            case "$seen" in *" $sid "*) continue ;; esac
            seen="$seen$sid "
            hit=$(printf '%s\n' "$running" | awk -F"$SEP" -v s="$sid" '$1 == s { print; exit }')
            if [ -n "$hit" ]; then
                IFS="$SEP" read -r _ r_pid r_where <<< "$hit"
                printf -v hit '    %-28s running again in %s  (session %s)\n' "${label:-$name}" \
                    "${r_where:-pid $r_pid}" "$sid"
                back="$back$hit"
            else
                printf -v hit '    %-28s cd %s && claude --resume %s\n' "${label:-$name}" "${cwd:-${_icwd:-?}}" "$sid"
                lost="$lost$hit"
            fi
        done <<< "$orows"
        if [ -n "$lost" ]; then
            echo
            echo "  Suspended sessions that lost their window (resume by hand):"
            printf '%s' "$lost"
        fi
        if [ -n "$back" ]; then
            echo
            echo "  Lost sessions resumed by hand, running again:"
            printf '%s' "$back"
        fi
        echo "  (delete $of only once none of these is lost or running by hand)"
    fi

    # Suspended sessions the sidecar is holding for a restore: save_state
    # keeps a row no window carries until a server has ADOPTED the file
    # (restore-state ran on it), rather than calling it lost. Until then they
    # are in neither list above, so name them here with the same hand-resume
    # command. Ids on a window or already in the orphans file are not
    # repeated.
    local sf; sf=$(state_file)
    if [ -s "$sf" ]; then
        local known waiting="" w_seen=" "
        known=" $(tmux list-windows -a -F '#{@stash_session}' 2>/dev/null | tr '\n' ' ') $(cat "$of" "$of".pending.* 2>/dev/null | awk -F"$SEP" '$7 != "" { printf "%s ", $7 }') "
        while IFS="$SEP" read -r sess idx name pidx origin label sid cwd _ts _icwd _extra; do
            [ -n "$sid" ] || continue
            case "$known$w_seen" in *" $sid "*) continue ;; esac
            w_seen="$w_seen$sid "
            printf -v hit '    %-28s cd %s && claude --resume %s\n' "${label:-$name}" "${cwd:-${_icwd:-?}}" "$sid"
            waiting="$waiting$hit"
        done < "$sf"
        if [ -n "$waiting" ]; then
            echo
            echo "  Suspended sessions awaiting a restore (a resurrect restore brings their tabs back; or by hand):"
            printf '%s' "$waiting"
        fi
    fi
}

case "${1:-}" in
    stash)   shift; do_stash "${1:-}" ;;
    snooze)  shift; do_snooze "${1:-}" "${2:-}" ;;
    check)   shift; do_check "$@"; exit $? ;;
    set-wake) shift; do_set_wake "$@" ;;
    wake)    shift; do_wake "$@" ;;
    unstash) shift; do_unstash "${1:-}" "${2:-}" ;;
    stash-many)   shift; do_stash_many "$@" ;;
    unstash-many) shift; do_unstash_many "$@" ;;
    kill-many)    shift; do_kill_many "$@" ;;
    sel-start)  shift; do_sel_start "${1:-right}" "${2:-}" ;;
    sel-move)   shift; do_sel_move "${1:-right}" "${2:-}" ;;
    sel-cancel) do_sel_cancel ;;
    sel-commit) shift; do_sel_commit "${1:-}" ;;
    sel-send)   shift; do_sel_send "${1:-}" "${2:-}" ;;
    sel-kill)   shift; do_sel_kill "${1:-}" "${2:-}" ;;
    sel-kill-prompt) shift; do_sel_kill_prompt "${1:-}" ;;
    sel-kill-ask) shift; do_sel_kill_ask "$@" ;;
    sel-kill-go) shift; do_sel_kill_go "$@" ;;
    send-pick)  shift; do_send_pick "$@" ;;
    send-many)  shift; do_send_many "$@" ;;
    count)   count ;;
    publish) publish ;;
    publish-count) publish_count ;;
    rows)    do_rows ;;
    thaw)    shift; do_thaw "${1:-}" "${2:-}" ;;
    pick)    shift; do_pick "${1:-}" ;;
    preview) shift; do_preview "${1:-}" ;;
    describe) shift; do_describe "${1:-}" ;;
    resume)  shift; resume_agent "${1:-}" ;;
    restore-state) do_restore_state ;;
    list)    do_list ;;
    *)       sed -n '/^#   stash.sh/,/^set -uo/p' "$0" | sed 's/^# \{0,1\}//;$d' ;;
esac
