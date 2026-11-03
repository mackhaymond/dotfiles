#!/usr/bin/env bash
# Post-save hook: tmux-assistant-resurrect's save-assistant-sessions.sh, minus
# its two costs and plus @resurrect-exclude-sessions.
#
# The upstream script is SOURCED, not copied — its main() is guarded for
# exactly that — so detection and session-id resolution stay upstream's. Only
# caching is layered on top:
#
#   * extract_cli_args() runs `flags=$(_discover_session_flags ...)`. The cache
#     that function writes lives in that $( ) subshell and dies with it, so the
#     "cached per tool" lookup re-ran `claude --help` and a 3-greps-per-line
#     parse of it for EVERY claude pane: ~0.4s each, 4.6s of a 7.6s save at 11
#     agents (measured 2026-10-06). Priming the variable in THIS shell makes
#     every subshell inherit it.
#   * it also runs `codex --help` once per codex pane (~0.17s). A function
#     shadowing the binary answers --help from a cache instead.
#   Both caches are keyed on the resolved binary path + mtime, so an upgrade
#   invalidates them.
#
# Sessions in @resurrect-exclude-sessions (agents, tasks) are dropped from the
# sidecar: resurrect-save.sh no longer writes their panes, so a row there could
# only resume an agent into a window that does not exist.

PLUGIN_DIR="${TMUX_ASSISTANT_RESURRECT_PLUGIN:-$HOME/.config/tmux/plugins/tmux-assistant-resurrect}"
# shellcheck source=/dev/null
source "$PLUGIN_DIR/scripts/save-assistant-sessions.sh" || exit 1
# (the sourced file has set -euo pipefail)

CACHE_DIR="$STATE_DIR/help-cache"
mkdir -p "$CACHE_DIR"

binary_key() {
	local bin real
	bin="$(command -v "$1" 2>/dev/null)" || return 1
	real="$(realpath "$bin" 2>/dev/null || printf '%s' "$bin")"
	printf '%s %s' "$real" "$(stat -f %m "$real" 2>/dev/null || echo 0)"
}

# cached <name> <key> <command...>: print the command's output, from cache when
# the key matches. The first line of a cache file is its key. Empty output is
# never cached — a failed --help must be retried, not remembered.
cached() {
	local name="$1" key="$2" file="$CACHE_DIR/$1"
	shift 2
	if [ -f "$file" ] && [ "$(head -n 1 "$file")" = "$key" ]; then
		tail -n +2 "$file"
		return 0
	fi
	local out
	out="$("$@" 2>/dev/null)" || true
	if [ -n "$out" ]; then
		{ printf '%s\n' "$key"; printf '%s\n' "$out"; } >"$file.$$" && mv -f "$file.$$" "$file"
	fi
	printf '%s\n' "$out"
}

for tool in claude codex opencode; do
	key="$(binary_key "$tool")" || continue
	help="$(cached "help-$tool" "$key" command "$tool" --help)"
	[ -n "$help" ] || continue
	printf -v "_HELP_$tool" '%s' "$help"
	# `"$tool" --help` inside the upstream script now hits this function.
	eval "$tool() {
		if [ \"\$#\" -eq 1 ] && [ \"\$1\" = --help ] && [ -n \"\${_HELP_$tool}\" ]; then
			printf '%s\n' \"\${_HELP_$tool}\"
		else
			command $tool \"\$@\"
		fi
	}"
	pattern_var="SESSION_FLAG_PATTERN_${tool}"
	if [ -n "${!pattern_var:-}" ] && declare -F _discover_session_flags >/dev/null; then
		# The parse itself is ~0.4s of grep forks for claude, so its result is
		# cached too (same key plus the pattern). The variable is set in THIS
		# shell, so the $( ) subshells upstream calls it from inherit it.
		flags_file="$CACHE_DIR/flags-$tool"
		flags_key="$key ${!pattern_var}"
		if [ -f "$flags_file" ] && [ "$(head -n 1 "$flags_file")" = "$flags_key" ]; then
			printf -v "_SESSION_FLAGS_$tool" '%s' "$(tail -n +2 "$flags_file")"
		else
			_discover_session_flags "$tool" "${!pattern_var}" >/dev/null || true
			flags_var="_SESSION_FLAGS_$tool"
			if [ -n "${!flags_var:-}" ] && [ "${!flags_var}" != "-" ]; then
				{ printf '%s\n' "$flags_key"; printf '%s\n' "${!flags_var}"; } >"$flags_file.$$" && mv -f "$flags_file.$$" "$flags_file"
			fi
		fi
	fi
done
unset tool key help pattern_var flags_file flags_key flags_var

main "$@"

# Grok rows. Upstream detects only claude/codex/opencode, so this adds a row
# per pane running a Grok TUI, in upstream's shape: pid from the pane's process
# tree (argv0 basename grok / grok-<os>-<arch>, never the `agent` symlink, and
# the TUI filter below so `grok -p`, `grok agent stdio`, `grok sessions` … are
# not agents), session id from ~/.grok/active_sessions.json by pid (Grok
# rewrites it on /new and /resume), else the grok-<pid>.json record
# grok-session-track.sh keeps, else a UUID given to -r/--resume or
# -s/--session-id. cwd is the session's own (a -w worktree session lives there,
# and its resume is looked up there), else the pane's. cli_args is argv minus
# argv0; assistant-restore.sh drops the resume flags and any launch prompt.
#
# ps flattens argv, so a TUI launched with a prompt (`grok "help me fix X"`)
# reads as the `help` subcommand. A subcommand-word verdict therefore stands
# only for a pid with no session behind it: a grok-<pid>.json record (only a
# TUI fires SessionStart) or an active_sessions.json entry makes it a TUI. The
# headless flags (-p, --prompt-file, …) are final.
#
# Costs nothing when Grok is not in use: with no live entry in
# active_sessions.json and no live grok-*.json record there is no ps at all,
# and no fork either. A record whose pid is gone (a grok that was killed
# before its SessionEnd ran) is deleted here, so it cannot hold that fast path
# off for good.
#
# A grok row takes a pane from an upstream row only when that row's process is
# the grok itself (upstream's pattern can match grok's own argv, e.g.
# `--add-dir ~/code/claude`) or the grok's descendant (a `claude -p` one of its
# tools runs); any other agent already saved for the pane keeps it.
grok_rows() {
	local grok_home="${GROK_HOME:-$HOME/.grok}" active="" recs=() f p a="" recpids=" "
	active="$grok_home/active_sessions.json"
	for f in "$STATE_DIR"/grok-*.json; do
		[ -f "$f" ] || continue
		p="${f##*/grok-}"
		p="${p%.json}"
		if [[ "$p" =~ ^[0-9]+$ ]] && ! kill -0 "$p" 2>/dev/null; then
			rm -f "$f"
			continue
		fi
		recs+=("$f")
		recpids+="$p "
	done
	[ -s "$active" ] && { read -r -d '' a <"$active" || true; }
	# pids of the active sessions, without jq ("pid": N; not "pid_start").
	local rest="$a" re='"pid"[[:space:]]*:[[:space:]]*([0-9]+)(.*)'
	while [[ "$rest" =~ $re ]]; do
		p="${BASH_REMATCH[1]}"
		rest="${BASH_REMATCH[2]}"
		kill -0 "$p" 2>/dev/null && recpids+="$p "
	done
	[ "$recpids" != " " ] || return 0
	[ -s "$OUTPUT_FILE" ] || return 0

	local US=$'\x1f' matches
	# Pane list first (US-joined), then the ps snapshot; the same tree walk as
	# upstream's, keeping the first grok TUI per pane (BFS, so the outermost).
	# tui(): 1 = TUI, 0 = headless (final), 2 = subcommand word (a TUI only
	# with a session behind its pid, see above). Line for line the watcher's
	# grok_tui_args: the scan goes on past the first bare word (a headless flag
	# later still counts) and stops at `--` (words after it are prompt text).
	matches="$(awk -v us="$US" -v recpids="$recpids" '
		function tui(line,    n, w, i, a, first, sub_) {
			n = split(line, w, /[ \t]+/)
			first = 1; sub_ = 0
			for (i = 2; i <= n; ) {
				a = w[i]
				if (a == "--") break
				# -phi / -p=hi (a glued value) and -h… / -v… are headless too.
				if (a ~ /^-[phv]/ || a ~ /^--(single|prompt-file|prompt-json|help|version)(=|$)/) return 0
				if (a in VALUE) { i += 2; continue }
				if (a in OPTVAL) {
					i++
					if (i <= n && w[i] != "" && w[i] !~ /^-/ && !(w[i] in SUBCMD)) i++
					continue
				}
				if (a ~ /^-/) { i++; continue }
				if (first) { first = 0; if (a in SUBCMD) sub_ = 1 }
				i++
			}
			return sub_ ? 2 : 1
		}
		BEGIN {
			split("agent leader worker clone completions doctor du disk-usage export help inspect login logout mcp memory models plugin sessions setup trace update usage version v worktree wrap", t, " ")
			for (k in t) SUBCMD[t[k]] = 1
			split("--add-dir --agent --agents --allow --cwd --debug-file --deny --disallowed-tools --json-schema --leader-socket -m --model --max-turns --output-format --permission-mode --reasoning-effort --effort --rules -s --session-id --sandbox --system-prompt-override --system-prompt --tools --worktree-ref --ref", t, " ")
			for (k in t) VALUE[t[k]] = 1
			split("-r --resume -w --worktree", t, " ")
			for (k in t) OPTVAL[t[k]] = 1
		}
		NR == FNR {
			split($0, p, us)
			target[p[2]] = p[1]; pcwd[p[2]] = p[3]; panes[++np] = p[2]
			next
		}
		{
			pid = $1 + 0; ppid = $2 + 0
			line = $0
			sub(/^[ \t]*[0-9]+[ \t]+[0-9]+[ \t]*/, "", line)
			args[pid] = line
			kids[ppid] = (ppid in kids) ? kids[ppid] " " pid : pid ""
			split(line, w, /[ \t]+/)
			base = w[1]; sub(/.*\//, "", base)
			if (base ~ /^grok(-(macos|linux|windows)-[a-z0-9_]+)?$/) {
				verdict = tui(line)
				if (verdict == 1 || (verdict == 2 && index(recpids, " " pid " "))) isgrok[pid] = 1
			}
		}
		function desc(root,    q, qs, qe, out, cur, n, c, j) {
			qs = 1; qe = 0; out = ""
			if (root in kids) { n = split(kids[root], c, " "); for (j = 1; j <= n; j++) q[++qe] = c[j] }
			while (qs <= qe) {
				cur = q[qs++]; out = out " " cur
				if (cur in kids) { n = split(kids[cur], c, " "); for (j = 1; j <= n; j++) q[++qe] = c[j] }
			}
			return out " "
		}
		END {
			for (i = 1; i <= np; i++) {
				root = panes[i] + 0
				delete q; qs = 1; qe = 0; q[++qe] = root; found = ""
				while (qs <= qe && found == "") {
					cur = q[qs++] + 0
					if (cur in isgrok) { found = cur; break }
					if (cur in kids) { n = split(kids[cur], c, " "); for (j = 1; j <= n; j++) q[++qe] = c[j] }
				}
				if (found == "") continue
				a = args[found]; sub(/^[^ \t]+[ \t]*/, "", a)
				print target[panes[i]] us found us a us pcwd[panes[i]] us desc(found)
			}
		}
	' <(tmux list-panes -a -F "#{session_name}:#{window_index}.#{pane_index}${US}#{pane_pid}${US}#{pane_current_path}" 2>/dev/null) \
		<(ps -eo pid=,ppid=,args= 2>/dev/null))" || return 0
	[ -n "$matches" ] || return 0

	local out
	# The records are one JSON line each (grok-session-track.sh writes them
	# with -c); `awk 1` ends every file with a newline, so a torn one costs
	# only its own line. A torn active_sessions.json reads as [].
	out="$(jq --arg m "$matches" --arg actraw "$a" \
		--slurpfile rec <(awk 1 "${recs[@]+"${recs[@]}"}" /dev/null 2>/dev/null | jq -cR 'fromjson? | select(type == "object")' 2>/dev/null) '
		def uuid: test("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$");
		def argsid: [splits("[ \t]+")] as $w
		  | [range(0; $w | length) as $i
		     | if ($w[$i] | test("^(-r|--resume|-s|--session-id)$")) then ($w[$i + 1] // "")
		       elif ($w[$i] | test("^(--resume|--session-id)=")) then ($w[$i] | sub("^[^=]*="; ""))
		       else empty end
		     | select(uuid)] | last // "";
		((try ($actraw | fromjson) catch []) | if type == "array" then . else [] end) as $a
		| [$m | split("\n")[] | select(length > 0) | split("\u001f")
		   | {pane: .[0], pid: .[1], args: .[2], pcwd: .[3], desc: (.[4] // "")} as $x
		   | ([$a[] | select((.pid | tostring) == $x.pid)] | last // {}) as $as
		   | ([$rec[] | select((.ppid | tostring) == $x.pid)] | last // {}) as $r
		   | ([$as.session_id, $r.session_id, ($x.args | argsid)] | map(select(type == "string" and . != "")) | first // "") as $sid
		   | select($sid != "")
		   | {pane: $x.pane, tool: "grok", session_id: $sid,
		      cwd: ([$as.cwd, $r.cwd, $x.pcwd] | map(select(type == "string" and . != "")) | first // ""),
		      pid: $x.pid, model: "", cli_args: $x.args, env: ($r.env // null), desc: $x.desc}] as $g
		| (.sessions // []) as $s
		| ($g | map(. as $row | select(all($s[] | select(.pane == $row.pane);
		                                    ((.pid // "") | tostring) as $p
		                                    | $p == $row.pid or ($row.desc | contains(" " + $p + " ")))))) as $g
		| .sessions = ($s | map(. as $u | select($g | any(.pane == $u.pane) | not))) + ($g | map(del(.desc)))
		| ($g | length) as $n | {n: $n, doc: .}
	' "$OUTPUT_FILE" 2>/dev/null)" || { log "grok: could not read the grok sessions"; return 0; }
	local n
	n="$(jq -r '.n' <<<"$out")"
	[ "${n:-0}" -gt 0 ] || return 0
	jq '.doc' <<<"$out" >"$OUTPUT_FILE.$$" && mv -f "$OUTPUT_FILE.$$" "$OUTPUT_FILE"
	log "saved $n grok session(s)"
	# Upstream strips assistant panes' text from the resurrect archive only for
	# the rows it wrote; a resumed TUI redraws anyway.
	if declare -F strip_assistant_pane_contents >/dev/null; then strip_assistant_pane_contents; fi
}
grok_rows

EXCLUDE="$(tmux show-option -gqv @resurrect-exclude-sessions 2>/dev/null || true)"
EXCLUDE="${EXCLUDE:-agents tasks}"
# Entries are shell `case` patterns (`ptymcp-*`), as in resurrect-save.sh,
# resurrect-guard.sh and the repair; an exact name is its own pattern. The
# sessions they match are handed to jq by exact name, newline-delimited.
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
if [ -s "$OUTPUT_FILE" ]; then
	ex=$'\n'
	while IFS= read -r s; do
		if is_excluded "$s"; then ex+="$s"$'\n'; fi
	done < <(jq -r '.sessions[]? | (.pane // "") | split(":")[0]' "$OUTPUT_FILE" 2>/dev/null || true)
	filtered="$(jq --arg ex "$ex" '
		.sessions |= map(select(((.pane // "") | split(":")[0]) as $s | ($ex | contains("\n" + $s + "\n")) | not))
	' "$OUTPUT_FILE")" || exit 0
	if [ -n "$filtered" ] && [ "$filtered" != "$(jq . "$OUTPUT_FILE")" ]; then
		printf '%s\n' "$filtered" >"$OUTPUT_FILE.$$" && mv -f "$OUTPUT_FILE.$$" "$OUTPUT_FILE"
		log "dropped excluded-session rows ($EXCLUDE)"
	fi
fi
