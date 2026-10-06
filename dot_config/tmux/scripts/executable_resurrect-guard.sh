#!/usr/bin/env bash
# tmux-resurrect post-save-all guard.
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
# do this, which is why the guard lives in the hook rather than in continuum.
#
# Two resurrect files are also NOT timestamped and are rewritten in place —
# assistant-sessions.json and pane_contents.tar.gz — so that same bad save
# destroyed the captured scrollback and the assistant session map outright.
# Timestamped .txt saves survived only because they are never pruned.
#
# WHAT IT DOES  (runs after `last` has already moved)
#   1. Archives the two in-place sidecar files beside every accepted save, so
#      a later bad save can no longer destroy them.
#   2. Reverts `last` — and the sidecars — when a save loses most of its panes.
#
# A shrink repeated CONFIRM_STREAK times in a row is ACCEPTED: that is someone
# genuinely closing windows, not a bad save. So the guard costs at most one
# save cycle of staleness and can never wedge the pointer permanently.

set -uo pipefail

# RESURRECT_DIR may be pre-set in the environment; that is how the test
# harness points the guard at a scratch dir instead of the live one.
RESURRECT_DIR="${RESURRECT_DIR:-$(tmux show-option -gqv @resurrect-dir 2>/dev/null || true)}"
RESURRECT_DIR="${RESURRECT_DIR:-$HOME/.tmux/resurrect}"
RESURRECT_DIR="${RESURRECT_DIR/#\~/$HOME}"

LAST="$RESURRECT_DIR/last"
STATE="$RESURRECT_DIR/.guard-state"
LOG="$RESURRECT_DIR/guard.log"
ARCHIVE="$RESURRECT_DIR/sidecar"
SIDECARS=(assistant-sessions.json pane_contents.tar.gz)

# Revert when the new save has less than 1/SHRINK_RATIO of the known-good pane
# count. Only applies once the known-good save is at least MIN_PANES, so small
# everyday setups are never second-guessed.
SHRINK_RATIO=2
MIN_PANES=4
CONFIRM_STREAK=2
KEEP_ARCHIVES=30

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >>"$LOG"; }

# Sessions resurrect-save.sh never writes do not count, in either file: an
# older save holding 70 `agents` panes is not 70 panes better, and comparing
# raw counts made the first save after the exclusion look like a shrink.
EXCLUDE="${RESURRECT_EXCLUDE:-$(tmux show-option -gqv @resurrect-exclude-sessions 2>/dev/null || true)}"
EXCLUDE="${EXCLUDE:-agents tasks}"
pane_count() {
	awk -F'\t' -v ex=" $EXCLUDE " '$1 == "pane" && index(ex, " " $2 " ") == 0 { n++ } END { print n + 0 }' "$1" 2>/dev/null || echo 0
}

write_state() {
	printf 'good_file=%s\nstreak=%s\n' "$1" "$2" >"$STATE"
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
	n="$(ls -1 "$ARCHIVE" 2>/dev/null | wc -l | tr -d ' ')"
	[ "${n:-0}" -gt "$KEEP_ARCHIVES" ] || return 0
	ls -1t "$ARCHIVE" 2>/dev/null | tail -n +$((KEEP_ARCHIVES + 1)) | while read -r old; do
		[ -n "$old" ] && rm -rf "${ARCHIVE:?}/$old"
	done
}

accept() {
	archive_sidecars "$1"
	write_state "$1" 0
	prune_archives
}

main() {
	[ -L "$LAST" ] || exit 0
	local cur_file cur_panes good_file streak good_panes
	cur_file="$(readlink "$LAST")"
	cur_file="${cur_file##*/}"
	[ -f "$RESURRECT_DIR/$cur_file" ] || exit 0
	cur_panes="$(pane_count "$RESURRECT_DIR/$cur_file")"

	good_file=""
	streak=0
	# shellcheck disable=SC1090
	[ -f "$STATE" ] && . "$STATE" 2>/dev/null

	# First run, or the known-good save was deleted: adopt whatever we have.
	if [ -z "$good_file" ] || [ ! -f "$RESURRECT_DIR/$good_file" ]; then
		accept "$cur_file"
		exit 0
	fi

	# Pointer did not move (files_differ said the save was identical).
	[ "$good_file" = "$cur_file" ] && exit 0

	good_panes="$(pane_count "$RESURRECT_DIR/$good_file")"

	if [ "$cur_panes" -ge "$good_panes" ] ||
		[ "$good_panes" -lt "$MIN_PANES" ] ||
		[ $((cur_panes * SHRINK_RATIO)) -ge "$good_panes" ]; then
		accept "$cur_file"
		exit 0
	fi

	streak=$((streak + 1))
	if [ "$streak" -ge "$CONFIRM_STREAK" ]; then
		log "shrink confirmed ${streak}x (${good_panes} -> ${cur_panes} panes); accepting $cur_file"
		accept "$cur_file"
		exit 0
	fi

	ln -sfn "$good_file" "$LAST"
	log "REVERTED last -> $good_file"
	log "  rejected $cur_file: $cur_panes pane(s) vs $good_panes in the known-good save (streak ${streak}/${CONFIRM_STREAK})"
	restore_sidecars "$good_file"
	write_state "$good_file" "$streak"
}

main "$@"
