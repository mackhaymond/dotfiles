#!/usr/bin/env bash
#
# SPACES (docs/spaces.md): a space is a tmux session, an agent is a window
# whose @agent_state is non-empty, every other window is a shell. This script
# holds the logic behind the spaces keys that a tmux format cannot do on its
# own (counting, wrapping, validating a remembered window). The keys and the
# two status lines live in spaces.conf; the hidden-session list lives in
# tmux.conf (@space_hidden_sessions).
#
#   sync                      mark hidden sessions (@space_hidden 1) and number
#                             the visible ones (@space_index: main = 1, then
#                             by session id, i.e. creation order). Run by the
#                             session-created/closed/renamed hooks and once at
#                             config load.
#   (agent/next/prev/toggle take an optional trailing CLIENT, for messages;
#    all four refuse a hidden session)
#   agent N SID               go to agent N of session SID (1-based ordinal
#                             among windows with @agent_state, by window_index;
#                             the agent sidebar numbers agents the same way)
#   next|prev SID             next / previous agent in SID, wrapping, skipping
#                             shells
#   toggle SID                agent <-> last-visited shell of this space (a
#                             new shell at the space folder if there is none);
#                             from a shell, back to the last-visited agent
#   new-space CLIENT          folder picker (run inside display-popup), then
#                             create or reuse a session for that folder and
#                             switch CLIENT to it
#
# Cost: every key is one bash fork plus one list call and one action call to
# tmux. Callers pass '#{session_id}' so nothing has to be asked twice; a
# script calling new-window must pass -c itself (a CLI new-window would use
# this script's cwd, not the session's path).

set -uo pipefail

TMUX_BIN="${SPACE_TMUX_BIN:-tmux}"
t() { "$TMUX_BIN" "$@"; }

# Messages go to the client whose key ran us (CLIENT, passed by the binding as
# '#{client_name}'); without one tmux picks its "best" client, which may be a
# different terminal.
CLIENT=""
msg() {
  if [[ -n "$CLIENT" ]]; then t display-message -c "$CLIENT" "space: $*" 2>/dev/null
  else t display-message "space: $*" 2>/dev/null; fi
}

# ── sync ────────────────────────────────────────────────────────────────────
cmd_sync() {
  local hidden rows
  hidden="$(t show -gv @space_hidden_sessions 2>/dev/null)"
  rows="$(t list-sessions -F '#{session_id}|#{session_name}|#{@space_hidden}|#{@space_index}')" || return 0
  local clients
  clients="$(t list-clients -F '#{client_name}' 2>/dev/null)"
  # main first, then by numeric session id (= creation order; S: loops use the
  # same order, so the status line and these numbers agree).
  # Every change goes into ONE tmux call, commands joined by ';' arguments.
  local -a args=()
  add() { ((${#args[@]})) && args+=(";"); args+=("$@"); }
  local n=1 id name was idx sorted
  sorted="$(printf '%s\n' "$rows" | awk -F'|' 'NF { k = ($2 == "main") ? -1 : substr($1, 2) + 0; print k "|" $0 }' \
    | sort -t'|' -k1,1n | cut -d'|' -f2-)"
  while IFS='|' read -r id name was idx; do
    [[ -n "$id" ]] || continue
    if [[ " $hidden " == *" $name "* ]]; then
      [[ "$was" == "1" ]] || add set-option -t "$id" @space_hidden 1
      [[ -z "$idx" ]] || add set-option -u -t "$id" @space_index
      continue
    fi
    [[ -z "$was" ]] || add set-option -u -t "$id" @space_hidden
    [[ "$idx" == "$n" ]] || add set-option -t "$id" @space_index "$n"
    n=$((n + 1))
  done <<<"$sorted"
  # Redraw EVERY client's tabs (a bare refresh-client reaches only one), in the
  # same call, and only when a number or marker actually changed.
  if ((${#args[@]})); then
    local c
    while IFS= read -r c; do [[ -n "$c" ]] && add refresh-client -S -t "$c"; done <<<"$clients"
    t "${args[@]}" >/dev/null 2>&1
  fi
  return 0
}

# ── agents of one session ──────────────────────────────────────────────────
# Fills AGENTS (window ids, by window_index), SHELLS likewise, CUR (active
# window id), CUR_KIND (a|s), LAST_A / LAST_S (the session's remembered ids)
# and SPATH, all from ONE list-windows call.
load_session() {
  local sid="$1" id active kind la ls sp hid
  AGENTS=() SHELLS=() CUR="" CUR_KIND="" LAST_A="" LAST_S="" SPATH=""
  local out
  out="$(t list-windows -t "$sid" -F '#{window_id}|#{window_active}|#{?#{@agent_state},a,s}|#{@space_last_agent}|#{@space_last_shell}|#{session_path}|#{@space_hidden}')" || return 1
  # The bindings already refuse hidden sessions without forking; this is the
  # backstop for any other caller. Never select or create windows in agents,
  # stash, tasks, ... (status 2 = refused).
  if [[ "${out##*|}" == "1" ]]; then
    msg "not a space (hidden session)"
    return 2
  fi
  while IFS='|' read -r id active kind la ls sp hid; do
    [[ -n "$id" ]] || continue
    LAST_A="$la" LAST_S="$ls" SPATH="$sp"
    if [[ "$kind" == "a" ]]; then AGENTS+=("$id"); else SHELLS+=("$id"); fi
    if [[ "$active" == "1" ]]; then CUR="$id" CUR_KIND="$kind"; fi
  done <<<"$out"
}

contains() {
  local needle="$1"; shift
  local x
  for x in "$@"; do [[ "$x" == "$needle" ]] && return 0; done
  return 1
}

cmd_agent() {
  local n="$1" sid="$2"
  [[ "$n" =~ ^[1-9][0-9]*$ ]] || { msg "bad agent number '$n'"; return 1; }
  load_session "$sid" || return 1
  if ((n > ${#AGENTS[@]})); then
    msg "no agent $n here (${#AGENTS[@]} in this space)"
    return 0
  fi
  t select-window -t "${AGENTS[n - 1]}"
}

cmd_step() {
  local dir="$1" sid="$2"
  load_session "$sid" || return 1
  local count=${#AGENTS[@]}
  ((count)) || { msg "no agents in this space"; return 0; }
  local i target=""
  if [[ "$CUR_KIND" == "a" ]]; then
    for i in "${!AGENTS[@]}"; do
      [[ "${AGENTS[i]}" == "$CUR" ]] || continue
      if [[ "$dir" == next ]]; then target="${AGENTS[(i + 1) % count]}"
      else target="${AGENTS[(i - 1 + count) % count]}"; fi
      break
    done
  else
    # On a shell: next = the first agent after it in tab order, prev = the
    # last one before it, wrapping. Needs window order, which AGENTS/SHELLS
    # alone lose, so ask for indexes (still one call).
    local out id idx active kind curidx="" first="" last="" before="" after=""
    out="$(t list-windows -t "$sid" -F '#{window_id}|#{window_index}|#{window_active}|#{?#{@agent_state},a,s}')" || return 1
    while IFS='|' read -r id idx active kind; do
      [[ "$active" == "1" ]] && curidx="$idx"
    done <<<"$out"
    while IFS='|' read -r id idx active kind; do
      [[ "$kind" == "a" ]] || continue
      [[ -n "$first" ]] || first="$id"
      last="$id"
      if ((idx < curidx)); then before="$id"; fi
      if ((idx > curidx)) && [[ -z "$after" ]]; then after="$id"; fi
    done <<<"$out"
    if [[ "$dir" == next ]]; then target="${after:-$first}"; else target="${before:-$last}"; fi
  fi
  [[ -n "$target" && "$target" != "$CUR" ]] && t select-window -t "$target"
  return 0
}

cmd_toggle() {
  local sid="$1"
  load_session "$sid" || return 1
  if [[ "$CUR_KIND" == "a" ]]; then
    if [[ -n "$LAST_S" ]] && contains "$LAST_S" "${SHELLS[@]}"; then
      t select-window -t "$LAST_S"
    elif ((${#SHELLS[@]})); then
      t select-window -t "${SHELLS[0]}"
    else
      t new-window -a -t "$sid:{end}" -c "${SPATH:-$HOME}"
    fi
  else
    if [[ -n "$LAST_A" ]] && contains "$LAST_A" "${AGENTS[@]}"; then
      t select-window -t "$LAST_A"
    elif ((${#AGENTS[@]})); then
      t select-window -t "${AGENTS[0]}"
    else
      msg "no agents in this space"
    fi
  fi
}

# ── new space ──────────────────────────────────────────────────────────────
# tmux refuses '.' and ':' in session names (it rewrites them to '_'); spaces
# and other oddities read badly in the tab, so keep [A-Za-z0-9_-] only.
sanitize() {
  local s="$1"
  s="${s//[^A-Za-z0-9_-]/-}"
  s="${s##-}"
  printf '%s' "${s:-space}"
}

# Create-or-reuse for DIR, print the session id to switch to. Pure tmux, no
# picker, so tests can drive it: `space.sh open-dir DIR`.
# DIR must already be absolute (cmd_open_dir makes it so).
open_dir() {
  local dir="${1%/}" rows id name path base
  [[ -n "$dir" ]] || dir="/"
  [[ "$dir" == "$HOME" ]] && { t list-sessions -F '#{session_id}|#{session_name}' | awk -F'|' '$2=="main"{print $1; exit}'; return 0; }
  rows="$(t list-sessions -F '#{session_id}|#{session_name}|#{session_path}|#{@space_hidden}')" || return 1
  local hid hidden
  while IFS='|' read -r id name path hid; do
    [[ "$hid" == "1" ]] && continue
    if [[ "${path%/}" == "$dir" ]]; then printf '%s\n' "$id"; return 0; fi
  done <<<"$rows"
  # Every hidden name counts as taken even with no such session yet: a folder
  # called stash/tasks/agents/... must become stash-2, never a hidden session.
  hidden=" $(t show -gqv @space_hidden_sessions 2>/dev/null) "
  base="$(sanitize "$(basename "$dir")")"
  name="$base"
  local k=2
  while [[ "$hidden" == *" $name "* ]] \
    || printf '%s\n' "$rows" | awk -F'|' -v n="$name" '$2==n{f=1} END{exit !f}'; do
    name="$base-$k"; k=$((k + 1))
  done
  t new-session -d -s "$name" -c "$dir" -P -F '#{session_id}'
}

cmd_open_dir() {
  local dir="$1" client="${2:-}" sid
  [[ -d "$dir" ]] || { msg "not a directory: $dir"; return 1; }
  # Absolute before the reuse match and before it becomes a session_path: a
  # typed "../foo" or "." is relative to the popup's cwd, not to tmux's.
  dir="$(cd -- "$dir" 2>/dev/null && pwd)" || { msg "cannot enter: $1"; return 1; }
  sid="$(open_dir "$dir")" || { msg "could not open a space for $dir"; return 1; }
  [[ -n "$sid" ]] || { msg "could not open a space for $dir"; return 1; }
  if [[ -n "$client" ]]; then t switch-client -c "$client" -t "$sid"; else t switch-client -t "$sid"; fi
}

cmd_new_space() {
  local client="${1:-}"
  CLIENT="$client"
  command -v fzf >/dev/null 2>&1 || { msg "fzf not found in PATH"; return 1; }
  local pane_path list out status query selection target
  if [[ -n "$client" ]]; then pane_path="$(t display-message -c "$client" -p '#{pane_current_path}')"
  else pane_path="$(t display-message -p '#{pane_current_path}')"; fi
  [[ -d "$pane_path" ]] || pane_path="$HOME"
  # Same candidate list as claude-dir-picker.sh: this tab's cwd, then zoxide
  # by frecency — or, without zoxide, ~ and the folders under ~/code/projects
  # and ~/code — deduped, dead dirs dropped, ~-shortened. A typed path that is
  # not listed still works (--print-query).
  list="$(
    {
      printf '%s\n' "$pane_path"
      if command -v zoxide >/dev/null 2>&1; then
        zoxide query -l 2>/dev/null || true
      else
        printf '%s\n' "$HOME"
        for d in "$HOME"/code/projects/*/ "$HOME"/code/*/; do [[ -d "$d" ]] && printf '%s\n' "${d%/}"; done
      fi
    } | awk 'NF && !seen[$0]++' \
      | while IFS= read -r d; do [[ -d "$d" ]] && printf '%s\n' "$d"; done \
      | sed "s|^$HOME\$|~|; s|^$HOME/|~/|"
  )"
  out="$(printf '%s\n' "$list" | fzf \
    --reverse --info=inline --print-query --tiebreak=index \
    --prompt='new space in: ' \
    --header='enter: open (or switch to) a space for this folder · esc: cancel' \
    --preview "bash '${BASH_SOURCE[0]}' preview {}" \
    --preview-window=right:50%:nowrap:noinfo)"
  status=$?
  [[ $status -eq 0 || $status -eq 1 ]] || return 0
  query="$(printf '%s\n' "$out" | sed -n '1p')"
  selection="$(printf '%s\n' "$out" | sed -n '2p')"
  target="${selection:-$query}"
  [[ -n "$target" ]] || return 0
  target="${target/#\~/$HOME}"
  cmd_open_dir "$target" "$client"
}

main() {
  local cmd="${1:-}"
  shift || true
  case "$cmd" in
    sync) cmd_sync ;;
    agent) CLIENT="${3:-}"; cmd_agent "${1:-}" "${2:-}" ;;
    next | prev) CLIENT="${2:-}"; cmd_step "$cmd" "${1:-}" ;;
    toggle) CLIENT="${2:-}"; cmd_toggle "${1:-}" ;;
    new-space) cmd_new_space "${1:-}" ;;
    open-dir) CLIENT="${2:-}"; cmd_open_dir "${1:-}" "${2:-}" ;;
    preview)
      local d="${1:-}"
      d="${d/#\~/$HOME}"
      ls -1p -- "$d" 2>/dev/null | head -200
      ;;
    *)
      echo "usage: space.sh sync | agent N SID [CLIENT] | next|prev SID [CLIENT] | toggle SID [CLIENT] | new-space CLIENT | open-dir DIR [CLIENT]" >&2
      return 2
      ;;
  esac
}

main "$@"
