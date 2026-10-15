#!/usr/bin/env bash
# Debounced, event-driven tmux-resurrect save: tmux.conf's hooks run this as
#   run-shell -b "#{HOME}/.config/tmux/scripts/resurrect-save-soon.sh [full]"
# on structural events (session created/closed/renamed, window linked/unlinked,
# split, pane exited/killed) and, with `full`, on client-detached.
#
# WHY. continuum's minute poll bounds layout loss to about a minute; this
# brings a structural change (an agent's new window, a closed tab, a park)
# down to ~DELAY seconds. Events come in bursts — a range park unlinks N
# windows, a restore creates every session — so callers coalesce: the first
# becomes the OWNER (mkdir lock), sleeps DELAY, then runs ONE
# `resurrect-save.sh auto`; every caller that arrives meanwhile just drops a
# `pending` marker into the lock dir and exits, and the owner runs one more
# save if a marker arrived after its last one began (at most MAX_ROUNDS).
# resurrect-save.sh does the real work and the real gating (startup grace,
# restore in progress, @kill_guard_shutdown, fingerprint fast path), so a save
# whose layout did not change is a few ms.
#
# `full` (client-detached: quitting the terminal only detaches its client)
# asks for `resurrect-save.sh auto-full` — a full save even when the layout
# fingerprint matches, so a quit leaves the assistant sessions and pane
# contents saved too, not just the layout. Any coalesced caller asking for
# full makes the owner's next save full — but only if, after the debounce, NO
# client is attached any more: closing a popup (floax, btop) detaches a nested
# client too, and must not cost a full save each time.
#
# COST. A caller that finds a live owner forks the one tmux call below and
# one `stat` of the lock's age; the owner's pid check (kill -0) and the marker
# write are bash builtins.
#
# DEFAULT SERVER ONLY, the rule continuum-ensure.sh and resurrect-save.sh use:
# run-shell jobs inherit TMUX="<socket>,<pid>,<session>", and a `tmux -L` test
# server that sources tmux.conf must never save into ~/.tmux/resurrect.
# RESURRECT_SAVE_ALLOW_SOCKET=<exact socket path> is the test harness's opt-in
# (resurrect-save.sh honours the same variable).
#
# NO STALE LOCKS. The lock dir holds the owner's pid; a caller that finds the
# lock but not a living owner (SIGKILL, a crash) breaks it and takes over. An
# owner removes its lock on exit. A lock dir with no pid yet is an owner in
# its first instant and is honoured — but only until STALE_AFTER: any lock
# that old is broken whatever its pid file says.
#
# Lost-event window, accepted: a marker written in the microseconds between
# the owner's last check and its rm is lost; the next minute poll saves it.

set -uo pipefail
exec >/dev/null 2>&1 # run-shell would show any output in the active pane

WANT_FULL=0
[ "${1:-}" = full ] && WANT_FULL=1

# The real default socket by full path, not any socket named `default` (agents'
# private servers live at $TMUX_TMPDIR/tmux-$UID/default). An empty TMUX goes
# on to resurrect-save.sh, which checks the socket the CLI actually reaches.
sock="${TMUX:-}"
sock="${sock%%,*}"
case "$sock" in
'' | "/private/tmp/tmux-$UID/default" | "/tmp/tmux-$UID/default") ;;
*) [ -n "${RESURRECT_SAVE_ALLOW_SOCKET:-}" ] && [ "$sock" = "$RESURRECT_SAVE_ALLOW_SOCKET" ] || exit 0 ;;
esac

# The server is about to be killed (kill-guard): teardown-time hooks must
# never save the dying server. resurrect-save.sh checks again after the delay.
# The flag is the epoch kill-guard set it at and counts only while under 120s
# old (resurrect-save.sh's rule): a guard that died mid-kill must not stop
# every event save forever. Non-numeric or stale counts as unset.
# A future epoch counts as unset too.
shutdown="$(tmux display-message -p '#{@kill_guard_shutdown}' 2>/dev/null)" || exit 0
case "$shutdown" in
'' | *[!0-9]*) ;;
*)
	age=$((${EPOCHSECONDS:-$(date +%s)} - shutdown))
	[ "$age" -ge 0 ] && [ "$age" -lt 120 ] && exit 0
	;;
esac

DELAY="${RESURRECT_SAVE_SOON_DELAY:-5}"
MAX_ROUNDS=4
SAVE="${RESURRECT_SAVE_SCRIPT:-${BASH_SOURCE[0]%/*}/resurrect-save.sh}"
key="${sock:-default}"
LOCK="${TMPDIR:-/tmp}"
LOCK="${LOCK%/}/resurrect-save-soon.${UID}.${key//\//_}"

mark() { # Hand the work to the living owner. Builtins only.
	: >"$LOCK/pending" || return 1
	[ "$WANT_FULL" = 1 ] && { : >"$LOCK/full" || return 1; }
	return 0
}

owner_alive() {
	local pid=""
	[ -f "$LOCK/pid" ] || return 0 # an owner between mkdir and writing its pid
	read -r pid <"$LOCK/pid" 2>/dev/null
	case "$pid" in '' | *[!0-9]*) return 0 ;; esac
	kill -0 "$pid" 2>/dev/null
}

# A lock older than any owner can live (MAX_ROUNDS debounces plus a minute for
# the saves; an owner re-dates the lock dir every round) is broken WHATEVER
# its pid says: a lock with no pid file, or whose pid was reused by an
# unrelated live process, would otherwise be honoured forever and every event
# save would stop silently. Same idea as resurrect-save.sh's stale-lock rule.
delay_s="${DELAY%%.*}"
case "$delay_s" in '' | *[!0-9]*) delay_s=0 ;; esac
STALE_AFTER=$(((delay_s + 1) * MAX_ROUNDS + 60))
lock_stale() {
	local m
	m="$(stat -f %m "$LOCK" 2>/dev/null || stat -c %Y "$LOCK" 2>/dev/null)" || return 1
	case "$m" in '' | *[!0-9]*) return 1 ;; esac
	[ $((${EPOCHSECONDS:-$(date +%s)} - m)) -ge "$STALE_AFTER" ]
}

if [ -d "$LOCK" ]; then
	if lock_stale; then
		rm -rf "$LOCK"
	elif owner_alive; then
		mark && exit 0
		# The owner released its lock between our test and our write: fall
		# through and try to become the owner ourselves.
	else
		rm -rf "$LOCK"
	fi
fi
if ! mkdir "$LOCK" 2>/dev/null; then
	# Lost the race to another caller that just became the owner.
	mark
	exit 0
fi
printf '%s' "$$" >"$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

rounds=0
while :; do
	touch "$LOCK" 2>/dev/null # alive: see STALE_AFTER
	sleep "$DELAY"
	mode=auto
	if [ "$WANT_FULL" = 1 ] || [ -e "$LOCK/full" ]; then
		WANT_FULL=0
		# client-detached also fires when a POPUP closes (floax, btop: each
		# attaches a nested client), and a full save is ~1.4s of CPU. Quitting
		# the terminal leaves no client at all; anything else is not a quit.
		[ -z "$(tmux list-clients -F x 2>/dev/null)" ] && mode=auto-full
	fi
	# Cleared BEFORE the save: an event during the save marks again and earns
	# one more round, since the save may have read the layout before it.
	rm -f "$LOCK/pending" "$LOCK/full"
	bash "$SAVE" "$mode"
	rounds=$((rounds + 1))
	[ -e "$LOCK/pending" ] && [ "$rounds" -lt "$MAX_ROUNDS" ] || break
done
exit 0
