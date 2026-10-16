#!/usr/bin/env bash
# Drop-in replacement for tmux-resurrect's scripts/save.sh.
#
# Same save file format, same `last` symlink, same pane_contents.tar.gz, same
# @resurrect-hook-post-save-layout / -post-save-all hooks — the plugin's
# restore.sh, continuum, the assistant hook, the repair and the guard all read
# its output unchanged. tmux.conf points @resurrect-save-script-path (what
# continuum runs) and prefix C-s here.
#
# WHY IT EXISTS (measured 2026-10-06, 21 panes; 87 panes before agents were
# excluded):
#   * upstream forks a full `ps -ao ppid,args | sed | grep | cut` PER PANE to
#     find the pane's command: ~100ms each, 2.2s at 21 panes, 8s+ at 87. Here
#     it is one ps snapshot for the whole save. That grep was also a PREFIX
#     match (`grep "^2995"` matches ppid 29951), so a pane could be saved with
#     another pane's command — or with two lines, corrupting the file.
#   * upstream's `IFS=$'\t' read` collapses an empty field, so a blank pane
#     title shifted every later column (what resurrect-save-repair.py's
#     "malformed pane row" branch exists to fix). awk splits on every tab.
#   * pane capture was 3-4 tmux forks per pane; here every capture is one
#     chained tmux command (capture-pane -b / save-buffer / delete-buffer).
#   * the `agents` session (tmux-pty-mcp shells — up to 173 panes) was saved
#     only to be restored as unownable idle zsh windows and immediately killed
#     by agent-restore-prune.sh. Sessions matching @resurrect-exclude-sessions
#     (shell `case` patterns, e.g. `ptymcp-*`) are never written (default:
#     agents tasks).
#   * concurrent saves (continuum + prefix C-s) shared one scratch dir; now
#     serialised by a lock.
#   * hook output went to the active pane via run-shell; now to save.log.
#   * DEFAULT SERVER ONLY (see the guard below): a `tmux -L <name>` test server
#     that sources tmux.conf gets continuum's hook and prefix C-s too, and used
#     to write a snapshot of ITSELF into ~/.tmux/resurrect and move `last`.
#   * `last` moved BEFORE anything judged the save; resurrect-guard.sh now
#     rules first (`check`), and a held save never touches `last` or the two
#     in-place sidecar files (see "verdict" below).
#
# MODES (the first argument; docs/resurrect-cadence.md has the whole story):
#   (none)     prefix C-s. Always a full save; refused while a restore runs.
#   quiet      continuum's autosave, now every minute. Gated and fingerprinted.
#   auto       resurrect-save-soon.sh after a structural tmux event. Same as quiet.
#   auto-full  resurrect-save-soon.sh after the last client detached (quitting
#              the terminal): gated like auto, but always a full save, so a
#              quit leaves everything saved and not just the layout.
#   final      kill-guard, synchronously, right before it kills the server. A
#              full save that waits for the lock, ignores every gate, and whose
#              exit status is the kill guard's go/no-go (table below).
# "Gated" means skipped, silently, while the server is under
# @resurrect-startup-grace-seconds old (default 120: a crashed server's fresh
# 1-pane replacement, before continuum's restore has run, must never be what
# gets saved — 2026-10-15 09:40 saved exactly that), while a restore is marked
# in progress (@resurrect_restoring, an epoch, set by tmux.conf's
# pre-restore-all hook and honoured for at most 15 minutes), and while
# @kill_guard_shutdown holds an epoch under 120s old (the server is about to be
# killed: teardown-time saves would capture a dying server). Every such epoch
# must also not be in the future; one that is counts as unset.
# "Fingerprinted": a full save costs ~1.4s of CPU (mostly the post-save-all
# hooks), far too much to run every minute. So quiet/auto compare a fingerprint
# of the layout — read in the same tmux call as the options — with the one
# recorded at the last full save, and while it matches and that save is under
# @resurrect-full-every-minutes old (default 15) they only re-date `last` and
# exit: a few ms, no snapshot, no hooks, no log line.
#
# `final` EXIT STATUS:
#   0  `last` names this save's snapshot (new, or the identical one, re-dated)
#      — OR the guard held it (a replacement server that never restored, or a
#      restore in progress): the snapshot is kept as evidence, `last` stays on
#      the known-good save and is re-dated, and the log line says
#      "saved N panes in Xs (kept last on <good> (<reason>); evidence <file>)".
#      A kill is safe then: the next restore brings the real layout back.
#   1  no panes / nothing to save        2  refused: not the default server
#   3  the save lock stayed held          4  `last` ended up elsewhere
#   5  the snapshot could not be written  6  a post-save-all hook failed
#      (assistant-save / repair: the sidecar may not hold every agent)

set -uo pipefail

# A tmux CLIENT without a UTF-8 locale prints every control character in -F
# output as `_` — the \x1f and tab separators below included (measured
# 2026-10-15: LANG=C or unset gives "a_b_c" for a\x1fb\tc). Under tmux's own
# run-shell/#() the server's LANG is inherited, but kill-guard, launchd or a
# bare `env -i` caller may have none, and then every row would parse wrong.
# Exported, so the hooks (assistant-save, the repair's own tab-separated
# list-panes, the guard) get it too. LC_ALL/LC_CTYPE outrank LANG, so a
# non-UTF-8 value there is dropped rather than left to win.
case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
*[Uu][Tt][Ff]-8* | *[Uu][Tt][Ff]8*) ;;
*)
	unset LC_ALL LC_CTYPE
	export LANG=en_US.UTF-8
	;;
esac

MODE="${1:-}"
AUTO=0 FINAL=0 FORCE_FULL=1
case "$MODE" in
quiet | auto) AUTO=1 FORCE_FULL=0 ;;
auto-full) AUTO=1 ;;
final) FINAL=1 ;;
esac
QUIET=0
{ [ "$AUTO" = 1 ] || [ "$FINAL" = 1 ]; } && QUIET=1
d=$'\t'
# Bash `read` gets \x1f-separated input throughout: with a whitespace IFS such
# as tab, `read` collapses an empty field (a blank title, an unset option) and
# shifts every later one — the very bug upstream has. awk keeps tabs.
US=$'\x1f'

HERE="${BASH_SOURCE[0]%/*}"
[ "$HERE" = "${BASH_SOURCE[0]}" ] && HERE=.
GUARD="${RESURRECT_GUARD_SCRIPT:-$HERE/resurrect-guard.sh}"
[ -f "$GUARD" ] || GUARD="$HERE/executable_resurrect-guard.sh" # the chezmoi source tree (tests)

# ONE tmux call for every option this needs (user options read as formats)
# AND the live layout the fingerprint is built from: a `display-message` line
# first, then one line per pane. #{socket_path} rides along: it is the server
# the tmux CLI ACTUALLY reaches, which the guard below checks. @resurrect_stale
# is the watcher's stale-save chip, cleared once this save lands.
#
# The fingerprint holds what a restore rebuilds and what says WHICH agent a tab
# is: session, window index and name, pane index and pid, foreground command,
# cwd, and the pane title minus its leading status glyph. Claude Code's window
# name is its version (2.1.300) in every tab, so the title ("✳ BAI info session
# slide deck") is the only cheap tab identity; the glyph in front of it spins
# while the agent works and is stripped below. Deliberately NOT in it, because
# they change all day without changing what a restore would rebuild: the
# active window/pane and client session (every window switch), history_size
# and cursor (every keystroke of output), window_layout (any terminal resize
# re-sizes every window it visits; a resized split restores with its old
# proportions until the next 15-minute full save, which is the whole loss).
FP_FORMAT="#{session_name}${US}#{window_index}${US}#{window_name}${US}#{pane_index}${US}#{pane_pid}${US}#{pane_current_command}${US}#{pane_current_path}${US}#{pane_title}"
SOCKET="" STALE="" START_TIME="" GRACE="" FULL_EVERY="" RESTORING="" SHUTDOWN="" RESTORED="" NOTICED=""
OUT="$(tmux display-message -p "#{socket_path}${US}#{@resurrect-dir}${US}#{@resurrect-capture-pane-contents}${US}#{@resurrect-pane-contents-area}${US}#{@resurrect-delete-backup-after}${US}#{@resurrect_stale}${US}#{@resurrect-exclude-sessions}${US}#{start_time}${US}#{@resurrect-startup-grace-seconds}${US}#{@resurrect-full-every-minutes}${US}#{@resurrect_restoring}${US}#{@kill_guard_shutdown}${US}#{@resurrect_restored}${US}#{@resurrect_replacement_noticed}" ";" \
	list-panes -a -F "$FP_FORMAT" 2>/dev/null)" || true
LIVE=""
case "$OUT" in *$'\n'*) LIVE="${OUT#*$'\n'}" ;; esac
IFS="$US" read -r SOCKET RESURRECT_DIR CAPTURE CONTENTS_AREA DELETE_AFTER STALE EXCLUDE START_TIME GRACE FULL_EVERY RESTORING SHUTDOWN RESTORED NOTICED <<<"${OUT%%$'\n'*}" || true
RESURRECT_DIR="${RESURRECT_DIR:-$HOME/.tmux/resurrect}"
RESURRECT_DIR="${RESURRECT_DIR//\$HOME/$HOME}"
RESURRECT_DIR="${RESURRECT_DIR/#\~/$HOME}"
CONTENTS_AREA="${CONTENTS_AREA:-full}"
DELETE_AFTER="${DELETE_AFTER:-30}"
# Unset means the default list; set-but-empty (`set -g @resurrect-exclude-sessions ''`)
# cannot be told apart from unset through a format, so use a placeholder like
# `none` to save everything.
EXCLUDE="${EXCLUDE:-agents tasks}"

case "$GRACE" in '' | *[!0-9]*) GRACE=120 ;; esac
case "$FULL_EVERY" in '' | *[!0-9]*) FULL_EVERY=15 ;; esac
# A restore marker older than this is a restore that died without clearing it;
# honouring it forever would stop autosave as silently as the 38 h gap did.
RESTORE_MAX_SECONDS=900
# @kill_guard_shutdown counts only this long: a guard that died mid-kill
# (C-c, SIGKILL) must not leave a flag that silently stops every autosave.
SHUTDOWN_MAX_SECONDS=120
# How long `final` waits for a running save to finish. A save takes ~2s.
FINAL_LOCK_WAIT="${RESURRECT_FINAL_LOCK_WAIT:-20}"

LAST="$RESURRECT_DIR/last"
LOG="$RESURRECT_DIR/save.log"
LOCK="$RESURRECT_DIR/.save.lock"
FPFILE="$RESURRECT_DIR/.save-fingerprint"
SAVE_DIR="$RESURRECT_DIR/save/pane_contents"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*" >>"$LOG"; }

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
		if [ "$c" = '*' ]; then
			# Every client (notify.sh's -c '*').
			local cl
			while IFS= read -r cl; do
				[ -n "$cl" ] && ${NOTIFY_TMUX:-tmux} display-message -c "$cl" -- "$*" 2>/dev/null
			done < <(${NOTIFY_TMUX:-tmux} list-clients -F '#{client_name}' 2>/dev/null)
		else
			${NOTIFY_TMUX:-tmux} display-message ${c:+-c "$c"} -- "$*" 2>/dev/null
		fi
	fi
	return 0
}

# message [-k KIND] TEXT: 5 s, unless QUIET.
message() {
	local k=info
	[ "${1:-}" = -k ] && { k="$2"; shift 2; }
	[ "$QUIET" = 1 ] && return 0
	notify_user -k "$k" -t 5 -- "$1"
}

# `final`'s exit status is the kill guard's go/no-go; every other mode exits 0
# on the paths that always did (continuum ignores the status anyway).
fail() { # <status for final> <log line>
	log "$2"
	[ "$FINAL" = 1 ] && exit "$1"
	exit 0
}

# @resurrect-exclude-sessions entries are shell `case` patterns (`ptymcp-*`),
# matched against whole session names — the same rule as resurrect-guard.sh
# and resurrect-save-repair.py. Cached per session name: the fingerprint asks
# once per pane.
declare -A EXCL_CACHE=()
is_excluded() {
	local v="${EXCL_CACHE["$1"]-}" p IFS=$' \t\n'
	if [ -z "$v" ]; then
		v=n
		set -f
		for p in $EXCLUDE; do
			# shellcheck disable=SC2254 # a pattern on purpose
			case "$1" in $p)
				v=y
				break
				;;
			esac
		done
		set +f
		EXCL_CACHE["$1"]=$v
	fi
	[ "$v" = y ]
}

# A numeric epoch neither in the future nor <max> seconds old or more.
fresh() { # <epoch> <max seconds>
	case "$1" in '' | *[!0-9]*) return 1 ;; esac
	[ $((NOW - $1)) -ge 0 ] && [ $((NOW - $1)) -lt "$2" ]
}

# --- default server only ------------------------------------------------------
# Every server that sources tmux.conf gets continuum's #(continuum_save.sh)
# hook and the prefix C-s binding, and both run THIS script with that server's
# environment: a `tmux -L <name>` test server would save a snapshot of itself
# into the real @resurrect-dir and move `last` onto it (and its post-save hooks
# would then "repair" and guard that snapshot). Two checks, both required:
#   * TMUX="<socket>,<pid>,<session>" — run-shell and #() jobs inherit it from
#     the server that launched them; empty means no server named one, and the
#     next check decides. Its socket must be THE default by exact path, the
#     same rule as agent-tab-watcher.sh, continuum-ensure.sh and
#     resurrect-save-soon.sh;
#   * #{socket_path} from the read above — whatever server the tmux CLI really
#     talks to (TMUX could be empty while something else steers the CLI). Empty
#     means tmux was unreachable, which proves nothing, so that refuses too.
# Nothing below this — lock, snapshot, `last`, hooks, prune — runs on a refusal.
# "Default" means THE real default socket, by full path — /private/tmp/tmux-
# $UID/default (what tmux reports on macOS) or /tmp/tmux-$UID/default — not
# any socket named `default`: every agent's private server now lives at
# $TMUX_TMPDIR/tmux-$UID/default, and one that loaded tmux.conf would
# otherwise autosave ITSELF into ~/.tmux/resurrect. A string compare, no stat.
# RESURRECT_SAVE_ALLOW_SOCKET=<exact socket path> admits one other server: the
# test harness's opt-in for its throwaway servers. A server that merely
# sources tmux.conf can never match it by accident.
socket_ok() {
	case "$1" in "/private/tmp/tmux-$UID/default" | "/tmp/tmux-$UID/default") return 0 ;; esac
	[ -n "${RESURRECT_SAVE_ALLOW_SOCKET:-}" ] && [ "$1" = "$RESURRECT_SAVE_ALLOW_SOCKET" ]
}
tmux_sock="${TMUX:-}"
tmux_sock="${tmux_sock%%,*}"
refused=""
if [ -n "$tmux_sock" ] && ! socket_ok "$tmux_sock"; then
	refused="TMUX names socket $tmux_sock"
elif [ -z "$SOCKET" ]; then
	refused="tmux unreachable (no #{socket_path})"
elif ! socket_ok "$SOCKET"; then
	refused="the tmux CLI reaches $SOCKET"
fi
if [ -n "$refused" ]; then
	# An event-driven save whose server has gone (it fired during teardown and
	# outlived the server by its debounce) has nothing to save and nobody to
	# warn. continuum's `quiet` poll is launched BY the server, so for it an
	# unreachable server stays as loud as any other refusal.
	case "$MODE" in auto | auto-full) [ -z "$SOCKET" ] && exit 0 ;; esac
	# Logged only where a save log already lives: a refused save creates nothing.
	[ -d "$RESURRECT_DIR" ] && log "REFUSED: not the default tmux server — $refused; nothing saved, \`last\` untouched, no hooks run"
	# Visible even when quiet (continuum's path, where message() is silent): a
	# FALSE refusal would stop autosave as silently as the 38 h gap did. The
	# stale-save chip renders this value as "save refused"; on a genuine test
	# server it lands on that server and harms nothing. One tmux call, and only
	# on a refusal; the next successful save clears it like any stale value.
	tmux set-option -g @resurrect_stale refused \; refresh-client -S >/dev/null 2>&1 || true
	message -k warn "Tmux save refused: not the default server ($refused)"
	[ "$FINAL" = 1 ] && exit 2
	exit 0
fi

# --- gates ----------------------------------------------------------------------
# quiet / auto / auto-full: silent — these run every minute, and a skip is the
# normal case at startup. Manual C-s: refused out loud while a restore runs
# (it would save the half-built layout and rewrite the sidecars from it).
NOW="${EPOCHSECONDS:-$(date +%s)}"
if [ "$AUTO" = 1 ]; then
	fresh "$SHUTDOWN" "$SHUTDOWN_MAX_SECONDS" && exit 0
	fresh "$START_TIME" "$GRACE" && exit 0
	fresh "$RESTORING" "$RESTORE_MAX_SECONDS" && exit 0
elif [ "$FINAL" = 0 ] && fresh "$RESTORING" "$RESTORE_MAX_SECONDS"; then
	message -k warn "Tmux save refused: a restore is in progress"
	exit 0
fi
[ -d "$RESURRECT_DIR" ] || mkdir -p "$RESURRECT_DIR"

# --- lock ---------------------------------------------------------------------
# mkdir is atomic. A lock older than two minutes belongs to a save that died
# (a save is a second or two), so it is broken rather than honoured forever.
take_lock() {
	mkdir "$LOCK" 2>/dev/null && return 0
	[ -n "$(find "$LOCK" -maxdepth 0 -mmin +2 2>/dev/null)" ] || return 1
	rm -rf "$LOCK" && mkdir "$LOCK" 2>/dev/null
}
if ! take_lock; then
	if [ "$FINAL" = 1 ]; then
		# The kill is about to happen: a save already running would capture
		# the same live server, but its outcome is not ours to report. Wait
		# for it, then take a save of our own.
		deadline=$((NOW + FINAL_LOCK_WAIT))
		until take_lock; do
			[ "${EPOCHSECONDS:-$(date +%s)}" -ge "$deadline" ] && fail 3 "final save: the save lock was still held after ${FINAL_LOCK_WAIT}s; nothing saved"
			sleep 0.2
		done
		# The layout read before the wait is up to FINAL_LOCK_WAIT old: read it
		# again now that this save owns the lock, or the fingerprint recorded
		# below could disagree with the snapshot taken after it.
		LIVE="$(tmux list-panes -a -F "$FP_FORMAT" 2>/dev/null)"
		NOW="${EPOCHSECONDS:-$(date +%s)}"
	else
		message -k warn "Tmux save already running"
		exit 0
	fi
fi
# A trap on a script's own EXIT is fine: this is a short-lived process, not the
# persistent shell.
trap 'rm -rf "$LOCK"' EXIT

# --- fingerprint fast path ----------------------------------------------------------
# Built with parameter expansion only (no fork, no pipe): ~20 panes cost well
# under a millisecond. Excluded sessions are left out, or every tmux-pty-mcp
# shell an agent opens in `agents` would force a full save. The layout was read
# before the snapshot below, so a recorded fingerprint is never NEWER than its
# snapshot: a change in between only costs one more full save.
#
# .save-fingerprint: "<epoch>[ held]\n<fingerprint>". `held` marks one taken by
# a save the guard held: unchanged since then, there is nothing new to judge
# for FULL_EVERY minutes, but `last` is not re-dated either — it does not
# describe this server, and the stale chip may say so.
GLYPH_RE='^[^[:alnum:][:space:]]+[[:space:]]+(.*)$'
fingerprint() {
	local IFS=$'\n' line s t out=""
	set -f
	for line in $LIVE; do
		s="${line%%"$US"*}"
		is_excluded "$s" && continue
		t="${line##*"$US"}"
		[[ $t =~ $GLYPH_RE ]] && t="${BASH_REMATCH[1]}"
		out+="${line%"$US"*}$US$t"$'\n'
	done
	set +f
	FP="$out"
}
FP=""
fingerprint
record_fp() { # [held]
	printf '%s%s\n%s' "$NOW" "${1:+ $1}" "$FP" >"$FPFILE" 2>/dev/null
}
if [ "$AUTO" = 1 ] && [ "$FORCE_FULL" = 0 ] && [ -f "$FPFILE" ]; then
	fp_head="" fp_saved=""
	{ IFS= read -r fp_head; IFS= read -r -d '' fp_saved; } <"$FPFILE"
	fp_when="${fp_head%% *}"
	if [ "$fp_saved" = "$FP" ] && fresh "$fp_when" $((FULL_EVERY * 60)); then
		# Unchanged: the snapshot `last` names still describes this server.
		# Re-date it, as an identical full save does: its mtime is the
		# watcher's "last save" clock (@resurrect_stale). -c never creates.
		# Not for an empty fingerprint (only excluded sessions: nothing was
		# saved) nor a held one.
		if [ -n "$FP" ] && [ "$fp_head" = "$fp_when" ] && [ -L "$LAST" ]; then
			touch -c "$LAST" 2>/dev/null
			[ -n "$STALE" ] && tmux set-option -gu @resurrect_stale \; refresh-client -S >/dev/null 2>&1
		fi
		exit 0
	fi
fi

START=$EPOCHREALTIME
TS="$(date +%Y%m%dT%H%M%S)"
FILE="$RESURRECT_DIR/tmux_resurrect_${TS}.txt"
# Names have one-second resolution, and two saves can now land in the same
# second (an event save straight after a `final`, or hooks that are fast). The
# `mv` below must never overwrite an existing snapshot: it may be the one
# `last` names, or the guard's known-good save, which a shrunk save would then
# replace in place where no revert can reach it. Wait for the next second.
while [ -e "$FILE" ]; do
	sleep 0.2
	TS="$(date +%Y%m%dT%H%M%S)"
	FILE="$RESURRECT_DIR/tmux_resurrect_${TS}.txt"
done
TMP="$FILE.tmp.$$"

PANES="$(mktemp)"
WINDOWS="$(mktemp)"
SESSIONS="$(mktemp)"
PS_SNAP="$(mktemp)"
AUTORENAME="$(mktemp)"
cleanup_tmp() { rm -f "$PANES" "$WINDOWS" "$SESSIONS" "$PS_SNAP" "$AUTORENAME" "$TMP"; }

# --- snapshot -----------------------------------------------------------------
# NOTE every field that can be empty sits between tabs, and awk (unlike `read`)
# keeps empty fields, so nothing shifts.
tmux list-panes -a -F "#{session_name}${d}#{window_index}${d}#{window_active}${d}#{window_flags}${d}#{pane_index}${d}#{pane_title}${d}#{pane_current_path}${d}#{pane_active}${d}#{pane_current_command}${d}#{pane_pid}${d}#{history_size}${d}#{cursor_y}${d}#{pane_id}" >"$PANES" 2>/dev/null
tmux list-windows -a -F "#{session_name}${d}#{window_index}${d}#{window_name}${d}#{window_active}${d}#{window_flags}${d}#{window_layout}${d}#{window_id}" >"$WINDOWS" 2>/dev/null
tmux list-sessions -F "#{session_grouped}${d}#{session_group}${d}#{session_id}${d}#{session_name}" >"$SESSIONS" 2>/dev/null
ps -axo ppid=,pid=,args= >"$PS_SNAP" 2>/dev/null

if [ ! -s "$PANES" ]; then
	log "no panes listed — tmux unreachable? nothing saved"
	cleanup_tmp
	message -k error "Tmux save failed: no panes"
	exit 1
fi

# The excluded sessions of THIS snapshot, by exact name (\x1f-delimited, so
# names with spaces work), for awk. Matched here, once, against the patterns.
EXCL_NAMES="$US"
while IFS="$US" read -r _g _gr _id sname; do
	is_excluded "$sname" && EXCL_NAMES+="$sname$US"
done < <(tr '\t' '\037' <"$SESSIONS")

# automatic-rename is saved as the window's LOCAL value (":" when unset) so a
# restore does not pin the global default onto every window. One tmux call for
# all windows: a marker line per window, then the value if one is set locally.
args=()
while IFS="$US" read -r _s _i _n _a _f _l wid; do
	[ -n "$wid" ] || continue
	[ "${#args[@]}" -gt 0 ] && args+=(";")
	args+=(display-message -p -t "$wid" "W${d}${wid}" ";" show-options -wqv -t "$wid" automatic-rename)
done < <(tr '\t' '\037' <"$WINDOWS")
# A window closed since the snapshot fails its display-message, which aborts
# the rest of the chain; those windows then save as ":" (no local value).
[ "${#args[@]}" -gt 0 ] && tmux "${args[@]}" >"$AUTORENAME" 2>/dev/null

STATE="$(tmux display-message -p "state${d}#{client_session}${d}#{client_last_session}" 2>/dev/null || true)"

# --- write the layout -----------------------------------------------------------
# The exclusion list goes through ENVIRON, not -v: awk would interpret
# backslashes in a session name.
EXCL_NAMES="$EXCL_NAMES" awk -F"$d" -v OFS="$d" -v us="$US" '
	function excluded(s) { return index(ENVIRON["EXCL_NAMES"], us s us) > 0 }
	FILENAME == ARGV[1] {   # sessions: grouped bookkeeping
		if ($1 == "1") grouped_rows[++ng] = $2 OFS $3 OFS $4
		next
	}
	FILENAME == ARGV[2] {   # ps snapshot: ppid pid args
		ppid = $0; sub(/^[ \t]+/, "", ppid)
		split(ppid, f, /[ \t]+/)
		line = ppid; sub(/^[0-9]+[ \t]+[0-9]+[ \t]?/, "", line)
		kids[f[1]] = (f[1] in kids) ? kids[f[1]] SUBSEP line : line
		next
	}
	FILENAME == ARGV[3] {   # automatic-rename probe output
		if ($1 == "W") { cur = $2; autoren[cur] = ":" } else if (cur != "") autoren[cur] = $0
		next
	}
	FILENAME == ARGV[4] {   # windows
		win[++nw] = $0
		flags[$1 SUBSEP $2] = $5
		next
	}
	{ pane[++np] = $0 }     # panes
	END {
		# Grouped sessions: the first of each group (sorted like upstream) is
		# the original; the rest are written as pointers and their panes and
		# windows are not saved.
		n = 0
		for (i = 1; i <= ng; i++) sorted[++n] = grouped_rows[i]
		for (i = 2; i <= n; i++) { v = sorted[i]; j = i - 1
			while (j > 0 && sorted[j] > v) { sorted[j + 1] = sorted[j]; j-- }
			sorted[j + 1] = v }
		lastgroup = ""
		for (i = 1; i <= n; i++) {
			split(sorted[i], g, OFS)
			if (g[1] != lastgroup) { original = g[3]; lastgroup = g[1]; continue }
			is_grouped[g[3]] = 1
			act = ""; alt = ""
			for (w = 1; w <= nw; w++) { split(win[w], x, OFS)
				if (x[1] != g[3]) continue
				if (x[5] ~ /\*/) act = x[2]
				if (x[5] ~ /-/) alt = x[2] }
			if (!excluded(g[3])) print "grouped_session", g[3], original, ":" alt, ":" act
		}
		for (i = 1; i <= np; i++) {
			split(pane[i], p, OFS)
			s = p[1]
			if ((s in is_grouped) || excluded(s)) continue
			# The pane command: a child of the pane shell, preferring the one
			# tmux reports as the foreground command when there are several.
			cmd = ""
			if (p[10] in kids) {
				nk = split(kids[p[10]], k, SUBSEP)
				cmd = k[1]
				for (j = 1; j <= nk; j++) {
					a0 = k[j]; sub(/[ \t].*/, "", a0); sub(/.*\//, "", a0)
					if (a0 == p[9]) { cmd = k[j]; break }
				}
			}
			dir = p[7]; gsub(/ /, "\\ ", dir)
			title = (p[6] == "") ? ":" : p[6]
			print "pane", s, p[2], p[3], ":" p[4], p[5], title, ":" dir, p[8], p[9], ":" cmd
		}
		for (w = 1; w <= nw; w++) {
			split(win[w], x, OFS)
			if ((x[1] in is_grouped) || excluded(x[1])) continue
			ar = (x[7] in autoren) ? autoren[x[7]] : ":"
			if (ar == "") ar = ":"
			print "window", x[1], x[2], ":" x[3], x[4], ":" x[5], x[6], ar
		}
	}
' "$SESSIONS" "$PS_SNAP" "$AUTORENAME" "$WINDOWS" "$PANES" >"$TMP"
printf '%s\n' "$STATE" >>"$TMP"

if ! grep -q '^pane' "$TMP"; then
	cleanup_tmp
	# Only excluded sessions: record that empty layout, so the next minutes
	# fast-path instead of re-trying (and re-logging) every minute.
	[ -z "$FP" ] && record_fp
	if [ "$FINAL" = 1 ]; then
		# A server holding only excluded sessions (after a crash, pty-mcp can
		# bring up a default server with just `agents`) has nothing to lose,
		# and `last` already holds the last real layout: re-date it and say
		# so, in the "saved N panes in" form the kill guard verifies, or such
		# a server could never be killed through the guard.
		touch -c "$LAST" 2>/dev/null
		log "saved 0 panes in $(awk -v s="$START" -v e="$EPOCHREALTIME" 'BEGIN { printf "%.2f", e - s }')s (nothing to save; kept last)"
		exit 0
	fi
	message -k warn "Tmux save skipped: nothing to save"
	fail 1 "save produced no pane rows (all sessions excluded?) — keeping last"
fi
if ! mv -f "$TMP" "$FILE"; then
	cleanup_tmp
	message -k error "Tmux save failed: could not write the snapshot"
	fail 5 "could not write $FILE — nothing linked, \`last\` untouched, no hooks run"
fi

# --- hooks ----------------------------------------------------------------------
# Upstream evals hooks with stdout on the active pane; keep them, minus the noise.
# (The name is kept before `shift`: logging "$1" after it, under `set -u`,
# killed the whole save with status 1 whenever post-save-all failed — before
# the fingerprint, the log line and the stale-chip clear.)
run_hook() {
	local name="$1" hook rc
	hook="$(tmux show-option -gqv "@resurrect-hook-$name" 2>/dev/null)"
	[ -n "$hook" ] || return 0
	shift
	local hook_args=""
	[ "$#" -gt 0 ] && printf -v hook_args '%q ' "$@"
	eval "$hook $hook_args" >>"$LOG" 2>&1
	rc=$?
	[ "$rc" -eq 0 ] || log "hook $name exited $rc"
	return "$rc"
}

# What the guard (here and in the hook chain) and the repair read instead of
# asking tmux again. RESURRECT_SERVER_START & co. let the guard tell a crash
# or kill REPLACEMENT server (started after the known-good save was captured,
# never restored) from Mack closing tabs.
export RESURRECT_EXCLUDE="$EXCLUDE" RESURRECT_SAVE_MODE="${MODE:-manual}" \
	RESURRECT_SERVER_START="$START_TIME" RESURRECT_RESTORED="$RESTORED" RESURRECT_RESTORING="$RESTORING"

run_hook post-save-layout "$FILE"

# --- verdict: may this snapshot become `last`? -------------------------------------
# Asked BEFORE `last` moves (resurrect-guard.sh check). Linking first and
# reverting in the post-save-all hook left `last` on a rejected 1-pane save for
# ~1.4s of every minute during a hold — a C-r or power loss then restored it.
# A HELD save: the snapshot stays on disk as evidence; `last`, the pane capture
# and the post-save-all hooks are all skipped, because assistant-sessions.json
# and pane_contents.tar.gz are rewritten IN PLACE and would then describe the
# held layout beside the good `last`. A guard that cannot be run (missing,
# crashed) does not block saving: logged, and the save links as it always did.
held="" held_on=""
if [ -L "$LAST" ] && cmp -s "$FILE" "$LAST"; then
	rm -f "$FILE"
	FILE="$(readlink "$LAST")"
	# The kept snapshot is re-confirmed as of now: its mtime is the watcher's
	# "last save" clock (@resurrect_stale), so an unchanged layout must not
	# read as a stale save. Follows the symlink; -c never creates.
	touch -c "$LAST" 2>/dev/null
	changed=0
else
	verdict="$(RESURRECT_DIR="$RESURRECT_DIR" bash "$GUARD" check "${FILE##*/}" 2>>"$LOG")"
	vrc=$?
	if [ "$vrc" -eq 2 ]; then
		held_on="${verdict%%"$US"*}"
		held="${verdict#*"$US"}"
		held="${held:-held by the guard}"
	elif [ "$vrc" -ne 0 ]; then
		log "guard check exited $vrc ($GUARD); linking without a verdict"
	fi
	if [ -z "$held" ]; then
		ln -sfn "${FILE##*/}" "$LAST"
		changed=1
	fi
fi

if [ -n "$held" ]; then
	# `final` re-dates the kept good save: the kill guard checks that `last`'s
	# target is no older than this save, and the good layout IS what a restore
	# after the kill should bring back.
	[ "$FINAL" = 1 ] && touch -c "$LAST" 2>/dev/null
	[ -n "$FP" ] && record_fp held
	cleanup_tmp
	elapsed="$(awk -v s="$START" -v e="$EPOCHREALTIME" 'BEGIN { printf "%.2f", e - s }')"
	log "saved $(grep -c '^pane' "$FILE" 2>/dev/null || echo '?') panes in ${elapsed}s (kept last on ${held_on} (${held}); evidence ${FILE##*/})"
	message -k warn "Tmux save held back: kept the last good save (${held})"
	# Tell Mack ONCE per server (server options die with it) that autosave is
	# holding the pre-crash layout for him: otherwise the only sign is a
	# stale-save chip an hour later. The chip cannot say it — any value but
	# `refused` renders as "last save <value> ago", and agent-tab-watcher.sh
	# rewrites it from `last`'s age — so a 10s message on every attached client.
	case "$held" in "replacement server never restored"*)
		if [ -z "$NOTICED" ]; then
			notice="tmux: this server started after the last good save and was never restored; autosave keeps that layout. prefix C-r restores it."
			tmux set-option -g @resurrect_replacement_noticed "$NOW" >/dev/null 2>&1
			# One call for every client (`-c '*'`): one toast exists at a
			# time, so a toast per client would leave only the last one.
			told=$(tmux list-clients -F x 2>/dev/null | grep -c x)
			notify_user -c '*' -k warn -t 10 -- "$notice" </dev/null
			log "notice: replacement server never restored; told $told client(s)"
		fi
		;;
	esac
	exit 0
fi

# --- pane contents --------------------------------------------------------------
if [ "$CAPTURE" = "on" ]; then
	rm -rf "$SAVE_DIR"
	mkdir -p "$SAVE_DIR"
	# One chained tmux command captures every pane straight into a file.
	buf="_resurrect_save_$$"
	capture_args() { # <pane-id> <history-size> <file>
		local start="-${2:-0}"
		[ "$CONTENTS_AREA" = "visible" ] && start=0
		args+=(capture-pane -e -J -S "$start" -t "$1" -b "$buf" ";"
			save-buffer -b "$buf" "$3" ";" delete-buffer -b "$buf")
	}
	args=()
	while IFS="$US" read -r s widx _wa _wf pidx _t _p _pa _c _pid hist _cy pid; do
		is_excluded "$s" && continue
		[ -n "$pid" ] || continue
		[ "${#args[@]}" -gt 0 ] && args+=(";")
		capture_args "$pid" "$hist" "$SAVE_DIR/pane-${s}:${widx}.${pidx}"
	done < <(tr '\t' '\037' <"$PANES")
	[ "${#args[@]}" -gt 0 ] && tmux "${args[@]}" >/dev/null 2>&1
	# A pane that closed mid-save fails its capture and aborts the rest of the
	# chain, so anything still missing is retried one pane at a time.
	while IFS="$US" read -r s widx _wa _wf pidx _t _p _pa _c _pid hist _cy pid; do
		is_excluded "$s" && continue
		f="$SAVE_DIR/pane-${s}:${widx}.${pidx}"
		[ -n "$pid" ] && [ ! -f "$f" ] || continue
		args=()
		capture_args "$pid" "$hist" "$f"
		tmux "${args[@]}" >/dev/null 2>&1
	done < <(tr '\t' '\037' <"$PANES")
	tmux delete-buffer -b "$buf" >/dev/null 2>&1
	# Upstream's printf hack: drop trailing blank lines. And upstream's
	# pane_has_any_content: no history, cursor on the first row and at most one
	# non-blank line means there is nothing worth restoring.
	while IFS="$US" read -r s widx _wa _wf pidx _t _p _pa _c _pid hist cy _id; do
		f="$SAVE_DIR/pane-${s}:${widx}.${pidx}"
		[ -f "$f" ] || continue
		if [ "${hist:-0}" -eq 0 ] && [ "${cy:-0}" -eq 0 ] && [ "$(grep -c '[^[:space:]]' "$f")" -le 1 ]; then
			rm -f "$f"
			continue
		fi
		awk '{ l[NR] = $0 } NF { last = NR } END { for (i = 1; i <= last; i++) print l[i] }' "$f" >"$f.t" && mv -f "$f.t" "$f"
	done < <(tr '\t' '\037' <"$PANES")
	archive="$RESURRECT_DIR/pane_contents.tar.gz"
	if tar cf - -C "$RESURRECT_DIR/save" ./pane_contents/ 2>/dev/null | gzip >"$archive.tmp.$$"; then
		mv -f "$archive.tmp.$$" "$archive"
	else
		rm -f "$archive.tmp.$$"
		log "could not write $archive"
	fi
	rm -rf "$SAVE_DIR"
fi

# --- prune ------------------------------------------------------------------------
# Upstream's rule: files older than @resurrect-delete-backup-after days, but
# always keep the newest five. One find for all of them: a month of 15-minute
# saves is ~3000 files, and a find per file cost seconds. With event-driven
# saves a month can hold several thousand more, so nothing here passes every
# file name to an exec any more (`ls -t <glob>` would hit ARG_MAX): the glob is
# expanded inside bash, and its names sort chronologically, so the newest five
# are its last five, excluded by name.
snaps=("$RESURRECT_DIR"/tmux_resurrect_*.txt)
if [ "${#snaps[@]}" -gt 5 ]; then
	keep=()
	for f in "${snaps[@]: -5}"; do keep+=(! -name "${f##*/}"); done
	find "$RESURRECT_DIR" -maxdepth 1 -type f -name 'tmux_resurrect_*.txt' "${keep[@]}" -mtime "+${DELETE_AFTER}" -delete 2>/dev/null
fi

# post-save-all is the sidecar chain (assistant-save && repair); its status is
# `final`'s exit 6. The guard's bookkeeping then ALWAYS runs, whatever the
# hooks did: it archives the fresh sidecars beside the accepted save and makes
# it the known-good one. It is idempotent, so a tmux.conf whose chain still
# ends in `; resurrect-guard.sh` merely runs it twice — but then the chain's
# status is the guard's, and a failed assistant-save no longer reaches exit 6.
run_hook post-save-all
hook_rc=$?
RESURRECT_DIR="$RESURRECT_DIR" bash "$GUARD" >>"$LOG" 2>&1 || log "guard bookkeeping exited $? ($GUARD)"

cleanup_tmp

# Record the fingerprint this save was taken from — only if `last` still
# points at its snapshot (a hook may have moved it). Recorded even when a hook
# failed: the snapshot itself is fine, and a hook that fails every time must
# not turn into a 1.4s save every minute.
now_last="$(readlink "$LAST" 2>/dev/null)"
if [ "${now_last##*/}" = "${FILE##*/}" ]; then
	record_fp
else
	rm -f "$FPFILE"
fi
END=$EPOCHREALTIME
elapsed="$(awk -v s="$START" -v e="$END" 'BEGIN { printf "%.2f", e - s }')"
log "saved $(grep -c '^pane' "$RESURRECT_DIR/${FILE##*/}" 2>/dev/null || echo '?') panes in ${elapsed}s ($([ "$changed" = 1 ] && echo new || echo unchanged) ${FILE##*/})"

# A save landed: drop the stale-save chip now rather than at the watcher's
# next once-a-minute look (which would clear it too).
if [ -n "$STALE" ]; then
	tmux set-option -gu @resurrect_stale \; refresh-client -S >/dev/null 2>&1 || true
fi

# Keep the log short; this runs forever. A full save writes ~4-6 lines (hooks
# included) and event-driven saves run a few times an hour while windows come
# and go, so the cap keeps roughly a day of history (~300KB at most).
if [ "$(wc -l <"$LOG")" -gt 3000 ]; then
	tail -n 1500 "$LOG" >"$LOG.tmp" && mv -f "$LOG.tmp" "$LOG"
fi

if [ "$FINAL" = 1 ]; then
	if [ "${now_last##*/}" != "${FILE##*/}" ]; then
		# The guard accepted it, so a hook moved `last` off this save. The
		# kill guard must not proceed.
		log "final save: \`last\` points at ${now_last:-nothing}, not ${FILE##*/}"
		exit 4
	fi
	if [ "$hook_rc" -ne 0 ]; then
		log "final save: post-save-all exited $hook_rc — the sidecars may not hold every agent"
		exit 6
	fi
fi

if [ "$hook_rc" -ne 0 ]; then
	message -k error "Tmux saved, but a post-save hook failed (see $LOG)"
else
	message -k ok "Tmux environment saved! (${elapsed}s)"
fi
exit 0
