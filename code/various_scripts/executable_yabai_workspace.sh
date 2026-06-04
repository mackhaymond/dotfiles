#!/bin/bash

# Focus (and optionally home) a labeled yabai space.
#
#   yabai_workspace.sh focus  <label>   -> focus the label wherever it lives (never moves)
#   yabai_workspace.sh master <label>   -> bring the label home to the laptop, then focus
#
# "normal"/"external-first" are accepted as aliases of "focus" for backward
# compatibility (older skhd binds). With <=1 display every mode collapses to a
# plain focus, byte-equivalent to the original single-laptop behavior.

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

CACHE_FILE="${YABAI_WORKSPACE_CACHE:-${HOME}/.cache/yabai/workspace_cache.env}"

MODE="${1:-}"
LABEL="${2:-}"

if [ -z "$MODE" ] || [ -z "$LABEL" ]; then
  exit 64
fi

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"

load_cache() {
  if [ -r "$CACHE_FILE" ]; then
    # shellcheck disable=SC1090
    . "$CACHE_FILE"
  fi

  case "${DISPLAY_COUNT:-}" in
    ''|*[!0-9]*)
      return 1
      ;;
  esac

  if [ "$DISPLAY_COUNT" -le 1 ] && [ -n "${MASTER_DISPLAY_INDEX:-}" ]; then
    return 0
  fi

  if [ "$DISPLAY_COUNT" -gt 1 ] && [ -n "${MASTER_DISPLAY_INDEX:-}" ] && [ -n "${EXTERNAL_DISPLAY_INDEX:-}" ]; then
    return 0
  fi

  return 1
}

# Cache miss -> heal labels + rewrite the cache WITHOUT any reset, then retry.
if ! load_cache; then
  "$SCRIPT_DIR/yabai_workspace_refresh.sh" >/dev/null 2>&1 || true
  load_cache || exit 0
fi

query_space_index() {
  local label="$1"

  yabai -m query --spaces --space "$label" 2>/dev/null |
    jq -r '.index // empty' |
    head -n 1
}

query_space_display_index() {
  local label="$1"

  yabai -m query --spaces --space "$label" 2>/dev/null |
    jq -r '.display // empty' |
    head -n 1
}

ensure_space_label() {
  local label="$1"

  if [ -n "$(query_space_display_index "$label")" ]; then
    return 0
  fi

  "$SCRIPT_DIR/yabai_workspace_refresh.sh" >/dev/null 2>&1 || true
  [ -n "$(query_space_display_index "$label")" ]
}

focus_space() {
  local space_index

  space_index=$(query_space_index "$LABEL")
  [ -z "$space_index" ] && return 0

  yabai -m space --focus "$space_index" >/dev/null 2>&1 || true
}

# Prime single-laptop fast path: with one display, every mode is a plain focus.
if [ "${DISPLAY_COUNT:-1}" -le 1 ]; then
  focus_space
  exit 0
fi

case "$MODE" in
  focus|normal|external-first)
    # Pure focus: never relocate a space. Only reached with 2+ displays.
    focus_space
    exit 0
    ;;
  master)
    # Bring the labeled space home to the laptop via a native whole-space
    # move (carries all its windows), then focus it.
    ensure_space_label "$LABEL" || exit 0

    if [ -n "${MASTER_DISPLAY_INDEX:-}" ] &&
       [ "$(query_space_display_index "$LABEL")" != "$MASTER_DISPLAY_INDEX" ]; then
      yabai -m space "$LABEL" --display "$MASTER_DISPLAY_INDEX" >/dev/null 2>&1 || true
    fi

    yabai -m display --focus "${MASTER_DISPLAY_INDEX:-1}" >/dev/null 2>&1 || true
    focus_space
    "$SCRIPT_DIR/yabai_reorder_spaces.sh" >/dev/null 2>&1 || true
    exit 0
    ;;
  *)
    exit 64
    ;;
esac
