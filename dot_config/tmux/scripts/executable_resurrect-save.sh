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
#     by agent-restore-prune.sh. Sessions in @resurrect-exclude-sessions are
#     never written (default: agents tasks).
#   * concurrent saves (continuum + prefix C-s) shared one scratch dir; now
#     serialised by a lock.
#   * hook output went to the active pane via run-shell; now to save.log.
#   * DEFAULT SERVER ONLY (see the guard below): a `tmux -L <name>` test server
#     that sources tmux.conf gets continuum's hook and prefix C-s too, and used
#     to write a snapshot of ITSELF into ~/.tmux/resurrect and move `last`.

set -uo pipefail

SCRIPT_OUTPUT="${1:-}"
d=$'\t'
# Bash `read` gets \x1f-separated input throughout: with a whitespace IFS such
# as tab, `read` collapses an empty field (a blank title, an unset option) and
# shifts every later one — the very bug upstream has. awk keeps tabs.
US=$'\x1f'

# One round-trip for every option this needs; user options read as formats.
# #{socket_path} rides along: it is the server the tmux CLI ACTUALLY reaches,
# which the guard below checks. @resurrect_stale is the watcher's stale-save
# chip, cleared once this save lands.
SOCKET="" STALE=""
IFS="$US" read -r SOCKET RESURRECT_DIR CAPTURE CONTENTS_AREA DELETE_AFTER STALE EXCLUDE < <(
	tmux display-message -p "#{socket_path}${US}#{@resurrect-dir}${US}#{@resurrect-capture-pane-contents}${US}#{@resurrect-pane-contents-area}${US}#{@resurrect-delete-backup-after}${US}#{@resurrect_stale}${US}#{@resurrect-exclude-sessions}" 2>/dev/null
) || true
RESURRECT_DIR="${RESURRECT_DIR:-$HOME/.tmux/resurrect}"
RESURRECT_DIR="${RESURRECT_DIR//\$HOME/$HOME}"
RESURRECT_DIR="${RESURRECT_DIR/#\~/$HOME}"
CONTENTS_AREA="${CONTENTS_AREA:-full}"
DELETE_AFTER="${DELETE_AFTER:-30}"
# Unset means the default list; set-but-empty (`set -g @resurrect-exclude-sessions ''`)
# cannot be told apart from unset through a format, so use a placeholder like
# `none` to save everything.
EXCLUDE="${EXCLUDE:-agents tasks}"

LAST="$RESURRECT_DIR/last"
LOG="$RESURRECT_DIR/save.log"
LOCK="$RESURRECT_DIR/.save.lock"
SAVE_DIR="$RESURRECT_DIR/save/pane_contents"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*" >>"$LOG"; }

message() {
	[ "$SCRIPT_OUTPUT" = "quiet" ] && return 0
	tmux display-message -d 5000 "$1" 2>/dev/null || true
}

# --- default server only ------------------------------------------------------
# Every server that sources tmux.conf gets continuum's #(continuum_save.sh)
# hook and the prefix C-s binding, and both run THIS script with that server's
# environment: a `tmux -L <name>` test server would save a snapshot of itself
# into the real @resurrect-dir and move `last` onto it (and its post-save hooks
# would then "repair" and guard that snapshot). Two checks, both required:
#   * TMUX="<socket>,<pid>,<session>" — run-shell and #() jobs inherit it from
#     the server that launched them (same rule as agent-tab-watcher.sh and
#     continuum-ensure.sh; empty means the CLI targets the default server);
#   * #{socket_path} from the read above — whatever server the tmux CLI really
#     talks to (TMUX could be empty while something else steers the CLI). Empty
#     means tmux was unreachable, which proves nothing, so that refuses too.
# Nothing below this — lock, snapshot, `last`, hooks, prune — runs on a refusal.
# RESURRECT_SAVE_ALLOW_SOCKET=<exact socket path> admits one other server: the
# test harness's opt-in for its throwaway servers. A server that merely
# sources tmux.conf can never match it by accident.
socket_ok() {
	[ "${1##*/}" = default ] || { [ -n "${RESURRECT_SAVE_ALLOW_SOCKET:-}" ] && [ "$1" = "$RESURRECT_SAVE_ALLOW_SOCKET" ]; }
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
	# Logged only where a save log already lives: a refused save creates nothing.
	[ -d "$RESURRECT_DIR" ] && log "REFUSED: not the default tmux server — $refused; nothing saved, \`last\` untouched, no hooks run"
	# Visible even when quiet (continuum's path, where message() is silent): a
	# FALSE refusal would stop autosave as silently as the 38 h gap did. The
	# stale-save chip renders this value as "save refused"; on a genuine test
	# server it lands on that server and harms nothing. One tmux call, and only
	# on a refusal; the next successful save clears it like any stale value.
	tmux set-option -g @resurrect_stale refused \; refresh-client -S >/dev/null 2>&1 || true
	message "Tmux save refused: not the default server ($refused)"
	exit 0
fi
mkdir -p "$RESURRECT_DIR"

# --- lock ---------------------------------------------------------------------
# mkdir is atomic. A lock older than two minutes belongs to a save that died
# (a save is a second or two), so it is broken rather than honoured forever.
if ! mkdir "$LOCK" 2>/dev/null; then
	if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +2 2>/dev/null)" ]; then
		rm -rf "$LOCK" && mkdir "$LOCK" 2>/dev/null || { message "Tmux save already running"; exit 0; }
	else
		message "Tmux save already running"
		exit 0
	fi
fi
# A trap on a script's own EXIT is fine: this is a short-lived process, not the
# persistent shell.
trap 'rm -rf "$LOCK"' EXIT

START=$EPOCHREALTIME
TS="$(date +%Y%m%dT%H%M%S)"
FILE="$RESURRECT_DIR/tmux_resurrect_${TS}.txt"
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
	message "Tmux save failed: no panes"
	exit 1
fi

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
awk -F"$d" -v OFS="$d" -v exclude=" $EXCLUDE " '
	function excluded(s) { return index(exclude, " " s " ") > 0 }
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
	log "save produced no pane rows (all sessions excluded?) — keeping last"
	cleanup_tmp
	message "Tmux save skipped: nothing to save"
	exit 0
fi
mv -f "$TMP" "$FILE"

# --- hooks ----------------------------------------------------------------------
# Upstream evals hooks with stdout on the active pane; keep them, minus the noise.
run_hook() {
	local hook rc
	hook="$(tmux show-option -gqv "@resurrect-hook-$1" 2>/dev/null)"
	[ -n "$hook" ] || return 0
	shift
	local args=""
	[ "$#" -gt 0 ] && printf -v args '%q ' "$@"
	eval "$hook $args" >>"$LOG" 2>&1
	rc=$?
	[ "$rc" -eq 0 ] || log "hook $1 exited $rc"
	return "$rc"
}

run_hook post-save-layout "$FILE"

if [ -L "$LAST" ] && cmp -s "$FILE" "$LAST"; then
	rm -f "$FILE"
	FILE="$(readlink "$LAST")"
	# The kept snapshot is re-confirmed as of now: its mtime is the watcher's
	# "last save" clock (@resurrect_stale), so an unchanged layout must not
	# read as a stale save. Follows the symlink; -c never creates.
	touch -c "$LAST" 2>/dev/null
	changed=0
else
	ln -sfn "$(basename "$FILE")" "$LAST"
	changed=1
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
		case " $EXCLUDE " in *" $s "*) continue ;; esac
		[ -n "$pid" ] || continue
		[ "${#args[@]}" -gt 0 ] && args+=(";")
		capture_args "$pid" "$hist" "$SAVE_DIR/pane-${s}:${widx}.${pidx}"
	done < <(tr '\t' '\037' <"$PANES")
	[ "${#args[@]}" -gt 0 ] && tmux "${args[@]}" >/dev/null 2>&1
	# A pane that closed mid-save fails its capture and aborts the rest of the
	# chain, so anything still missing is retried one pane at a time.
	while IFS="$US" read -r s widx _wa _wf pidx _t _p _pa _c _pid hist _cy pid; do
		case " $EXCLUDE " in *" $s "*) continue ;; esac
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
# saves is ~3000 files, and a find per file cost seconds.
old=()
while IFS= read -r f; do old+=("$f"); done < <(ls -t "$RESURRECT_DIR"/tmux_resurrect_*.txt 2>/dev/null | tail -n +6)
[ "${#old[@]}" -gt 0 ] && find "${old[@]}" -maxdepth 0 -type f -mtime "+${DELETE_AFTER}" -delete 2>/dev/null

run_hook post-save-all
hook_rc=$?

cleanup_tmp
END=$EPOCHREALTIME
elapsed="$(awk -v s="$START" -v e="$END" 'BEGIN { printf "%.2f", e - s }')"
log "saved $(grep -c '^pane' "$RESURRECT_DIR/$(basename "$FILE")" 2>/dev/null || echo '?') panes in ${elapsed}s ($([ "$changed" = 1 ] && echo new || echo unchanged) $(basename "$FILE"))"

# A save landed: drop the stale-save chip now rather than at the watcher's
# next once-a-minute look (which would clear it too).
if [ -n "$STALE" ]; then
	tmux set-option -gu @resurrect_stale \; refresh-client -S >/dev/null 2>&1 || true
fi

# Keep the log short; this runs every 15 minutes forever.
if [ "$(wc -l <"$LOG")" -gt 1000 ]; then
	tail -n 500 "$LOG" >"$LOG.tmp" && mv -f "$LOG.tmp" "$LOG"
fi

if [ "$hook_rc" -ne 0 ]; then
	message "Tmux saved, but a post-save hook failed (see $LOG)"
else
	message "Tmux environment saved! (${elapsed}s)"
fi
exit 0
