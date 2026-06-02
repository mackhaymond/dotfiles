#!/bin/bash

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

CACHE_FILE="${YABAI_WORKSPACE_CACHE:-${TMPDIR:-/tmp}/yabai_workspace_cache.env}"

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

if ! load_cache; then
  "$SCRIPT_DIR/yabai_workspace_refresh.sh" >/dev/null 2>&1 || true
  load_cache || exit 0
fi

query_focused_display_index() {
  yabai -m query --displays --display 2>/dev/null |
    jq -r '.index // empty' |
    head -n 1
}

query_space_display_index() {
  local label="$1"

  yabai -m query --spaces --space "$label" 2>/dev/null |
    jq -r '.display // empty' |
    head -n 1
}

move_space_to_display() {
  local label="$1"
  local target_index="$2"
  local current_index

  current_index=$(query_space_display_index "$label")
  if [ -z "$current_index" ] || [ "$current_index" = "$target_index" ]; then
    return 0
  fi

  yabai -m space "$label" --display "$target_index" >/dev/null 2>&1 || true
}

focus_space() {
  yabai -m space --focus "$LABEL" >/dev/null 2>&1 || true
}

if [ "$DISPLAY_COUNT" -le 1 ]; then
  focus_space
  exit 0
fi

case "$MODE" in
  normal|external-first)
    FOCUSED_INDEX=$(query_focused_display_index)
    SPACE_DISPLAY_INDEX=$(query_space_display_index "$LABEL")

    if [ -z "$FOCUSED_INDEX" ] || [ -z "$SPACE_DISPLAY_INDEX" ]; then
      exit 0
    fi

    if [ "$FOCUSED_INDEX" != "$MASTER_DISPLAY_INDEX" ] && [ "$SPACE_DISPLAY_INDEX" = "$MASTER_DISPLAY_INDEX" ]; then
      move_space_to_display "$LABEL" "$FOCUSED_INDEX"
    fi

    focus_space
    exit 0
    ;;
  master)
    TARGET_INDEX="$MASTER_DISPLAY_INDEX"
    ;;
  *)
    exit 64
    ;;
esac

if [ -z "${TARGET_INDEX:-}" ]; then
  exit 0
fi

move_space_to_display "$LABEL" "$TARGET_INDEX"
yabai -m display --focus "$TARGET_INDEX" >/dev/null 2>&1 || true
focus_space
