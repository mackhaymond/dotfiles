#!/usr/bin/env bash
# tmux-resurrect save guard: decides whether a new save may become `last`.
#
# WHY THIS EXISTS
# resurrect's save.sh repoints the `last` symlink (scripts/save.sh:248)
# UNCONDITIONALLY, and only afterwards runs the post-save-all hook (:259). So a
# save taken against a near-empty server destroys the pointer to the last good
# state, and nothing in resurrect or continuum checks for that.
#
# Observed 2026-08-22: 74s after a reboot, a `prefix + C-s` (resurrect's save
# key, adjacent to its restore key `C-r`) repointed `last` at a 1-pane,
# 118-byte file. The restore two seconds later read that file and brought back
# nothing — 58 panes and 6 assistant sessions looked gone. continuum was NOT
# involved: its @continuum-save-last-timestamp never moved. Any save path can
# do this, which is why the guard sits in the save itself rather than in
# continuum.
#
# Two resurrect files are also NOT timestamped and are rewritten in place —
# assistant-sessions.json and pane_contents.tar.gz — so that same bad save
# destroyed the captured scrollback and the assistant session map outright.
#
# TWO ENTRY POINTS
#   resurrect-guard.sh check <file>   BEFORE `last` moves. resurrect-save.sh
#       (our replacement for the plugin's save.sh) asks this for every new
#       snapshot that differs from `last`. Exit 0: accept, link it. Exit 2:
#       HOLD — stdout is "<good_file>\x1f<reason>"; the save keeps the file as
#       evidence but never links `last` to it and runs neither the pane capture
#       nor the post-save-all hooks (they rewrite the two sidecars in place).
#       A hold updates the streak state here; an accept writes nothing.
#       (Review 2026-10-15: deciding AFTER linking left `last` on the rejected
#       snapshot for ~1.4s of every minute during a hold — a C-r or a power
#       loss in that window restored the tiny layout.)
#   resurrect-guard.sh                AFTER the hooks: bookkeeping for whatever
#       `last` names now. An accepted save gets its sidecars archived and
#       becomes the known-good save. Idempotent, so it is safe in the
#       post-save-all chain too (older tmux.conf) as well as run by the save.
#       Still reverts `last` itself when it finds a save no check ever saw.
#
# THE RULES  (cur = the new save, good = the known-good save in .guard-state)
#   * Not a shrink (cur >= good/SHRINK_RATIO, or good < MIN_PANES): accept.
#   * A shrink on an ordinary server: hold until it has been seen
#     CONFIRM_STREAK times in a row AND for CONFIRM_MINUTES since it was first
#     held — someone genuinely closing windows, not a bad save. (Streak alone
#     confirmed 87 -> 19 panes in 20s on 2026-10-06; at minute cadence two
#     shrunk saves land seconds apart.)
#   * A REPLACEMENT server — started (#{start_time}) after `good` was captured
#     (the stamp in its name), so it never had that layout: a crash or kill
#     replacement. Unless a restore completed ON THIS SERVER
#     (@resurrect_restored, set at the end of tmux.conf's post-restore-all
#     chain), its small layout is not Mack closing tabs, and wall-clock time
#     says nothing about it — neither `held` nor uptime: a crash at 18:00, a
#     skipped restore and a closed lid read as 14h of both at 08:00, and a
#     server pty-mcp starts headless burns hours before Mack ever attaches.
#     So "Mack genuinely living on a fresh server" is measured in saves that
#     actually RAN: a shrink there is held until TRUST_RUN consecutive holds
#     (TRUST_FRESH_HOURS * 4 — the held fingerprint re-judges an unchanged
#     layout every ~15 minutes) each at most TRUST_GAP seconds after the one
#     before — a longer gap (sleep, a stopped clock) restarts that count — and
#     counted at most once per TRUST_EVERY seconds (minute-cadence holds while
#     tabs come and go must not race it), AND uptime >= TRUST_FRESH_HOURS,
#     AND the ordinary streak and 10 minutes.
#     A restored replacement server follows the ordinary rule.
#   * `final` (RESURRECT_SAVE_MODE=final: kill-guard's save right before a
#     deliberate kill) is accepted — the live layout is real, and reverting
#     it would restore an older one — EXCEPT on a replacement server that
#     never restored, or while a restore is in progress (@resurrect_restoring
#     under 15 minutes old): then the snapshot is held, so the kill is safe
#     and the next restore brings the real layout back.
#   * A save identical to `good` (resurrect-save.sh kept `last`) ends a hold:
#     the streak and its clock reset.
#
# SERVER FACTS come from resurrect-save.sh's environment
# (RESURRECT_SERVER_START, RESURRECT_RESTORED, RESURRECT_RESTORING; set but
# empty = unknown, which never makes a server a replacement). Unset, the guard
# asks tmux once.
#
# .guard-state is shell assignments: good_file, streak, first_reject (the
# epoch of the first held save of the current streak). A state file without
# first_reject starts that clock at the next hold — only ever more cautious.
# While a hold lasts it also carries trust_run, trust_at (the last counted
# hold) and last_hold, the replacement-server escape's count; an accept or a
# cleared hold writes the three-line form again.

set -uo pipefail

# RESURRECT_DIR may be pre-set in the environment; that is how the save and
# the test harness point the guard at the right directory.
RESURRECT_DIR="${RESURRECT_DIR:-$(tmux show-option -gqv @resurrect-dir 2>/dev/null || true)}"
RESURRECT_DIR="${RESURRECT_DIR:-$HOME/.tmux/resurrect}"
RESURRECT_DIR="${RESURRECT_DIR/#\~/$HOME}"

LAST="$RESURRECT_DIR/last"
STATE="$RESURRECT_DIR/.guard-state"
LOG="$RESURRECT_DIR/guard.log"
ARCHIVE="$RESURRECT_DIR/sidecar"
SIDECARS=(assistant-sessions.json pane_contents.tar.gz)

# A shrink: the new save has less than 1/SHRINK_RATIO of the known-good pane
# count. Only applies once the known-good save is at least MIN_PANES, so small
# everyday setups are never second-guessed.
SHRINK_RATIO=2
MIN_PANES=4
CONFIRM_STREAK=2
CONFIRM_MINUTES=10
TRUST_FRESH_HOURS=4
TRUST_RUN=$((TRUST_FRESH_HOURS * 4)) # consecutive holds, ~one per 15-minute re-judge
TRUST_GAP=1200                       # seconds; a longer gap between holds restarts the count
TRUST_EVERY=720                      # seconds; holds closer together than this count once
RESTORE_MAX_SECONDS=900 # resurrect-save.sh's bound on @resurrect_restoring
# Sidecar archives are only ever restored for the CURRENT known-good save (the
# newest accepted one), so 30 is ample even at minute cadence; older ones are
# for a human digging by hand (~16KB each, ~0.5MB total).
KEEP_ARCHIVES=30

NOW="${EPOCHSECONDS:-$(date +%s)}"
MODE="${RESURRECT_SAVE_MODE:-}"

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >>"$LOG"; }

# Sessions resurrect-save.sh never writes do not count, in either file: an
# older save holding 70 `agents` panes is not 70 panes better. Entries are
# shell `case` patterns (`ptymcp-*`), the same rule as the save and repair.
EXCLUDE="${RESURRECT_EXCLUDE:-$(tmux show-option -gqv @resurrect-exclude-sessions 2>/dev/null || true)}"
EXCLUDE="${EXCLUDE:-agents tasks}"
is_excluded() {
	local p IFS=$' \t\n'
	set -f
	for p in $EXCLUDE; do
		# shellcheck disable=SC2254 # a pattern on purpose
		case "$1" in $p) set +f; return 0 ;; esac
	done
	set +f
	return 1
}
pane_count() {
	local n=0 line rest s
	while IFS= read -r line; do
		case "$line" in pane$'\t'*) ;; *) continue ;; esac
		rest="${line#pane$'\t'}"
		s="${rest%%$'\t'*}"
		is_excluded "$s" || n=$((n + 1))
	done 2>/dev/null <"$1"
	echo "$n"
}

# A numeric epoch that is neither in the future nor older than <max seconds>.
fresh() { # <value> <max seconds>
	case "$1" in '' | *[!0-9]*) return 1 ;; esac
	[ $((NOW - $1)) -ge 0 ] && [ $((NOW - $1)) -lt "$2" ]
}

server_facts() {
	if [ -z "${RESURRECT_SERVER_START+set}" ]; then
		local US=$'\x1f' out
		out="$(tmux display-message -p "#{start_time}${US}#{@resurrect_restored}${US}#{@resurrect_restoring}" 2>/dev/null)"
		IFS="$US" read -r RESURRECT_SERVER_START RESURRECT_RESTORED RESURRECT_RESTORING <<<"$out"
	fi
	SERVER_START="${RESURRECT_SERVER_START:-}"
	RESTORED="${RESURRECT_RESTORED:-}"
	RESTORING="${RESURRECT_RESTORING:-}"
}

# The capture time in a snapshot's name (tmux_resurrect_YYYYmmddTHHMMSS.txt,
# local time). Empty when it does not parse.
capture_epoch() {
	local stamp="${1#tmux_resurrect_}"
	stamp="${stamp%.txt}"
	date -j -f '%Y%m%dT%H%M%S' "$stamp" +%s 2>/dev/null ||
		date -d "${stamp:0:8} ${stamp:9:2}:${stamp:11:2}:${stamp:13:2}" +%s 2>/dev/null
}

# Sets REPLACEMENT=1 when this server started after <good> was captured and
# has not restored since. Unknown facts never make a replacement.
replacement_check() { # <good_file>
	REPLACEMENT=0
	case "$SERVER_START" in '' | *[!0-9]*) return 0 ;; esac
	case "$RESTORED" in '' | *[!0-9]*) ;; *) return 0 ;; esac # restored here: trusted
	local captured
	captured="$(capture_epoch "$1")"
	case "$captured" in '' | *[!0-9]*) return 0 ;; esac
	[ "$SERVER_START" -gt "$captured" ] && REPLACEMENT=1
	return 0
}

write_state() { # <good_file> <streak> [first_reject [trust_run trust_at last_hold]]
	{
		printf 'good_file=%s\nstreak=%s\nfirst_reject=%s\n' "$1" "$2" "${3:-}"
		if [ -n "${6:-}" ]; then
			printf 'trust_run=%s\ntrust_at=%s\nlast_hold=%s\n' "${4:-0}" "${5:-}" "$6"
		fi
	} >"$STATE"
}

archive_sidecars() {
	local dest="$ARCHIVE/$1" f
	mkdir -p "$dest" 2>/dev/null || return 0
	for f in "${SIDECARS[@]}"; do
		[ -f "$RESURRECT_DIR/$f" ] && cp -p "$RESURRECT_DIR/$f" "$dest/$f" 2>/dev/null
	done
	return 0
}

restore_sidecars() {
	local src="$ARCHIVE/$1" f n=0
	[ -d "$src" ] || { log "  no archived sidecars for $1 — leaving current ones in place"; return 0; }
	for f in "${SIDECARS[@]}"; do
		if [ -f "$src/$f" ]; then
			cp -p "$src/$f" "$RESURRECT_DIR/$f" 2>/dev/null && n=$((n + 1))
		fi
	done
	log "  restored $n sidecar file(s) from $1"
}

prune_archives() {
	local n
	n="$(find "$ARCHIVE" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l | tr -d ' ')"
	[ "${n:-0}" -gt "$KEEP_ARCHIVES" ] || return 0
	# shellcheck disable=SC2012 # names are our own snapshot names
	ls -1t "$ARCHIVE" 2>/dev/null | tail -n +$((KEEP_ARCHIVES + 1)) | while read -r old; do
		[ -n "$old" ] && rm -rf "${ARCHIVE:?}/$old"
	done
}

accept() {
	archive_sidecars "$1"
	write_state "$1" 0
	prune_archives
}

load_state() {
	good_file=""
	streak=0
	first_reject=""
	trust_run=0 trust_at="" last_hold=""
	# shellcheck disable=SC1090
	[ -f "$STATE" ] && . "$STATE" 2>/dev/null
	case "$streak" in '' | *[!0-9]*) streak=0 ;; esac
	case "$first_reject" in *[!0-9]*) first_reject="" ;; esac
	case "$trust_run" in '' | *[!0-9]*) trust_run=0 ;; esac
	case "$trust_at" in *[!0-9]*) trust_at="" ;; esac
	case "$last_hold" in *[!0-9]*) last_hold="" ;; esac
}

# The replacement-server escape's count of saves that actually ran, as of a
# hold NOW: continued only if the previous hold was 0..TRUST_GAP seconds ago
# (else it restarts), and advanced at most once per TRUST_EVERY seconds. Sets
# TRUST_RUN_NOW and TRUST_AT_NOW.
trust_count() {
	TRUST_RUN_NOW="$trust_run" TRUST_AT_NOW="$trust_at"
	if [ -z "$last_hold" ] || [ $((NOW - last_hold)) -lt 0 ] || [ $((NOW - last_hold)) -gt "$TRUST_GAP" ]; then
		TRUST_RUN_NOW=0 TRUST_AT_NOW=""
	fi
	if [ -z "$TRUST_AT_NOW" ] || [ $((NOW - TRUST_AT_NOW)) -ge "$TRUST_EVERY" ] || [ $((NOW - TRUST_AT_NOW)) -lt 0 ]; then
		TRUST_RUN_NOW=$((TRUST_RUN_NOW + 1)) TRUST_AT_NOW="$NOW"
	fi
}

# The verdict on <cur_file> against the known-good save. Sets VERDICT
# (accept|hold), REASON, and on a hold the new STREAK/FIRST_REJECT (and the
# trust count, TRUST_RUN_NOW/TRUST_AT_NOW) to record. Writes nothing; callers do.
decide() { # <cur_file>
	local cur_file="$1" cur_panes good_panes held uptime
	VERDICT=accept REASON="" STREAK=0 FIRST_REJECT=""
	# HOLD_AT is the last_hold to record: set only on a replacement server, so
	# an ordinary server's state keeps its three-line form.
	TRUST_RUN_NOW="$trust_run" TRUST_AT_NOW="$trust_at" HOLD_AT=""
	cur_panes="$(pane_count "$RESURRECT_DIR/$cur_file")"
	good_panes="$(pane_count "$RESURRECT_DIR/$good_file")"
	server_facts
	replacement_check "$good_file"

	if [ "$MODE" = final ]; then
		if fresh "$RESTORING" "$RESTORE_MAX_SECONDS"; then
			VERDICT=hold REASON="restore in progress"
		elif [ "$REPLACEMENT" = 1 ]; then
			VERDICT=hold REASON="replacement server never restored"
		elif [ "$cur_panes" -lt "$good_panes" ]; then
			REASON="final save before a deliberate kill (${good_panes} -> ${cur_panes} panes)"
		fi
		# A final hold is not a shrink verdict: the streak and the trust count
		# stay as they were.
		STREAK="$streak" FIRST_REJECT="$first_reject" HOLD_AT="$last_hold"
		return 0
	fi

	if [ "$cur_panes" -ge "$good_panes" ] ||
		[ "$good_panes" -lt "$MIN_PANES" ] ||
		[ $((cur_panes * SHRINK_RATIO)) -ge "$good_panes" ]; then
		return 0
	fi

	STREAK=$((streak + 1))
	# The clock starts at the first hold of this streak (or now, for a state
	# file written before first_reject existed).
	FIRST_REJECT="$first_reject"
	[ -z "$FIRST_REJECT" ] || [ "$STREAK" -eq 1 ] || [ "$FIRST_REJECT" -gt "$NOW" ] && FIRST_REJECT="$NOW"
	held=$((NOW - FIRST_REJECT))
	local confirmed=0
	[ "$STREAK" -ge "$CONFIRM_STREAK" ] && [ "$held" -ge $((CONFIRM_MINUTES * 60)) ] && confirmed=1
	trust_count
	if [ "$REPLACEMENT" = 1 ]; then
		uptime=$((NOW - SERVER_START))
		if [ "$confirmed" = 1 ] && [ "$uptime" -ge $((TRUST_FRESH_HOURS * 3600)) ] &&
			[ "$TRUST_RUN_NOW" -ge "$TRUST_RUN" ]; then
			REASON="shrink confirmed ${STREAK}x over ${held}s on a fresh server up ${uptime}s, ${TRUST_RUN_NOW} holds in a row (${good_panes} -> ${cur_panes} panes)"
			return 0
		fi
		VERDICT=hold HOLD_AT="$NOW"
		REASON="replacement server never restored (${cur_panes} pane(s) vs ${good_panes}, streak ${STREAK}, ${TRUST_RUN_NOW}/${TRUST_RUN} holds in a row, up ${uptime}s of $((TRUST_FRESH_HOURS * 3600))s)"
		return 0
	fi
	if [ "$confirmed" = 1 ]; then
		REASON="shrink confirmed ${STREAK}x over ${held}s (${good_panes} -> ${cur_panes} panes)"
		return 0
	fi
	VERDICT=hold
	REASON="$cur_panes pane(s) vs $good_panes in the known-good save (streak ${STREAK}/${CONFIRM_STREAK}, shrunk for ${held}s of $((CONFIRM_MINUTES * 60))s)"
}

check() { # <file name in RESURRECT_DIR>
	local cur_file="${1##*/}"
	[ -f "$RESURRECT_DIR/$cur_file" ] || exit 0
	load_state
	# First run, or the known-good save was deleted: nothing to compare with.
	if [ -z "$good_file" ] || [ ! -f "$RESURRECT_DIR/$good_file" ] || [ "$good_file" = "$cur_file" ]; then
		exit 0
	fi
	decide "$cur_file"
	[ "$VERDICT" = accept ] && exit 0
	log "HELD $cur_file; \`last\` stays on $good_file: $REASON"
	write_state "$good_file" "$STREAK" "$FIRST_REJECT" "$TRUST_RUN_NOW" "$TRUST_AT_NOW" "$HOLD_AT"
	printf '%s\x1f%s\n' "$good_file" "$REASON"
	exit 2
}

main() {
	[ -L "$LAST" ] || exit 0
	local cur_file
	cur_file="$(readlink "$LAST")"
	cur_file="${cur_file##*/}"
	[ -f "$RESURRECT_DIR/$cur_file" ] || exit 0
	load_state

	# First run, or the known-good save was deleted: adopt whatever we have.
	if [ -z "$good_file" ] || [ ! -f "$RESURRECT_DIR/$good_file" ]; then
		accept "$cur_file"
		exit 0
	fi

	# `last` still names the known-good save: the new save was identical to
	# it, so whatever shrink was being held has gone away.
	if [ "$good_file" = "$cur_file" ]; then
		if [ "$streak" != 0 ] || [ -n "$first_reject" ] || [ -n "$last_hold" ]; then
			log "layout matches the known-good save $good_file again; hold cleared (streak was $streak)"
			write_state "$good_file" 0
		fi
		exit 0
	fi

	decide "$cur_file"
	if [ "$VERDICT" = accept ]; then
		[ -n "$REASON" ] && log "$REASON; accepting $cur_file"
		accept "$cur_file"
		exit 0
	fi

	# Only reached when nothing checked this save before `last` moved (an
	# older resurrect-save.sh, or a save from elsewhere): undo it.
	ln -sfn "$good_file" "$LAST"
	log "REVERTED last -> $good_file"
	log "  rejected $cur_file: $REASON"
	restore_sidecars "$good_file"
	write_state "$good_file" "$STREAK" "$FIRST_REJECT" "$TRUST_RUN_NOW" "$TRUST_AT_NOW" "$HOLD_AT"
}

case "${1:-}" in
check) check "${2:-}" ;;
*) main ;;
esac
