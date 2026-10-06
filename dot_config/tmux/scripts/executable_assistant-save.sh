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

EXCLUDE="$(tmux show-option -gqv @resurrect-exclude-sessions 2>/dev/null || true)"
EXCLUDE="${EXCLUDE:-agents tasks}"
if [ -s "$OUTPUT_FILE" ]; then
	filtered="$(jq --arg ex " $EXCLUDE " '
		.sessions |= map(select(((.pane // "") | split(":")[0]) as $s | ($ex | contains(" " + $s + " ")) | not))
	' "$OUTPUT_FILE")" || exit 0
	if [ -n "$filtered" ] && [ "$filtered" != "$(jq . "$OUTPUT_FILE")" ]; then
		printf '%s\n' "$filtered" >"$OUTPUT_FILE.$$" && mv -f "$OUTPUT_FILE.$$" "$OUTPUT_FILE"
		log "dropped excluded-session rows ($EXCLUDE)"
	fi
fi
