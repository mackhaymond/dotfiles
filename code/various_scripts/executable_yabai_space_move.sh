#!/bin/bash

# Explicit cross-display space moves (the only keys that relocate a space).
#
#   yabai_space_move.sh push       -> send the FOCUSED space to the other display, follow
#   yabai_space_move.sh home-all   -> pull every canonical label home to the laptop
#
# Both are no-ops with a single display. Moves are native whole-space operations
# (yabai -m space ... --display ...), so all of a space's windows travel with it.

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

CACHE_FILE="${YABAI_WORKSPACE_CACHE:-${HOME}/.cache/yabai/workspace_cache.env}"
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"

ACTION="${1:-}"
[ -z "$ACTION" ] && exit 64

LABELS="terminal main school todo schedule mail calendar messages chatgpt codex"

load_cache() {
  if [ -r "$CACHE_FILE" ]; then
    # shellcheck disable=SC1090
    . "$CACHE_FILE"
  fi
  case "${DISPLAY_COUNT:-}" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ -n "${MASTER_DISPLAY_INDEX:-}" ] || return 1
  return 0
}

if ! load_cache; then
  "$SCRIPT_DIR/yabai_workspace_refresh.sh" >/dev/null 2>&1 || true
  load_cache || exit 0
fi

# Single display: nothing to move anywhere.
if [ "${DISPLAY_COUNT:-1}" -le 1 ]; then
  exit 0
fi

case "$ACTION" in
  push)
    # Capture the focused space's STABLE internal id before the move. Labels can
    # be empty (external scratch spaces) and mission-control indices renumber
    # across displays, so neither is a reliable post-move handle; the id is.
    sid=$(yabai -m query --spaces --space 2>/dev/null | jq -r '.id // empty' 2>/dev/null)
    # Send the current space to the other display (next, else prev). No-ops if
    # there is no other display to receive it.
    if yabai -m space --display next >/dev/null 2>&1 ||
       yabai -m space --display prev >/dev/null 2>&1; then
      # Follow the moved space across displays: resolve its NEW index by id.
      if [ -n "$sid" ]; then
        nidx=$(yabai -m query --spaces 2>/dev/null |
          jq -r --argjson sid "$sid" '.[] | select(.id == $sid) | .index' 2>/dev/null |
          head -n 1)
        [ -n "$nidx" ] && yabai -m space --focus "$nidx" >/dev/null 2>&1 || true
      fi
    fi
    exit 0
    ;;
  home-all)
    master="${MASTER_DISPLAY_INDEX:-1}"
    for label in $LABELS; do
      disp=$(yabai -m query --spaces --space "$label" 2>/dev/null | jq -r '.display // empty' 2>/dev/null)
      [ -n "$disp" ] && [ "$disp" != "$master" ] &&
        yabai -m space "$label" --display "$master" >/dev/null 2>&1 || true
    done
    yabai -m display --focus "$master" >/dev/null 2>&1 || true
    yabai -m rule --apply >/dev/null 2>&1 || true
    exit 0
    ;;
  *)
    exit 64
    ;;
esac
