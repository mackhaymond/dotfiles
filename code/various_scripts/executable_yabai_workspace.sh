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

refresh_state() {
  "$SCRIPT_DIR/yabai_workspace_refresh.sh" >/dev/null 2>&1 || true
  load_cache || true
}

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

query_space_index() {
  local label="$1"

  yabai -m query --spaces --space "$label" 2>/dev/null |
    jq -r '.index // empty' |
    head -n 1
}

query_space_windows() {
  local label="$1"

  yabai -m query --spaces --space "$label" 2>/dev/null |
    jq -r '.windows[]?'
}

first_unlabeled_space_on_display() {
  local display_index="$1"

  yabai -m query --spaces 2>/dev/null |
    jq -r --argjson display_index "$display_index" '
      ([.[] | select(.display == $display_index and .label == "") | .index][0]) // empty
    ' |
    head -n 1
}

create_space_on_display() {
  local display_index="$1"
  local index

  yabai -m space --create "$display_index" >/dev/null 2>&1 || true
  index=$(first_unlabeled_space_on_display "$display_index")
  [ -n "$index" ] && printf '%s\n' "$index"
}

ensure_space_label() {
  local label="$1"

  if [ -n "$(query_space_display_index "$label")" ]; then
    return 0
  fi

  refresh_state
  [ -n "$(query_space_display_index "$label")" ]
}

move_space_to_display() {
  local label="$1"
  local target_index="$2"
  local current_display_index
  local source_space_index
  local target_space_index
  local window_ids

  current_display_index=$(query_space_display_index "$label")
  if [ -z "$current_display_index" ] || [ "$current_display_index" = "$target_index" ]; then
    return 0
  fi

  source_space_index=$(query_space_index "$label")
  [ -z "$source_space_index" ] && return 0
  window_ids=$(query_space_windows "$label")

  target_space_index=$(first_unlabeled_space_on_display "$target_index")
  if [ -z "$target_space_index" ]; then
    target_space_index=$(create_space_on_display "$target_index")
  fi
  [ -z "$target_space_index" ] && return 0

  yabai -m space "$source_space_index" --label >/dev/null 2>&1 || true
  yabai -m space "$target_space_index" --label "$label" >/dev/null 2>&1 || true
  yabai -m display --focus "$target_index" >/dev/null 2>&1 || true
  yabai -m space --focus "$label" >/dev/null 2>&1 || true

  while IFS= read -r window_id; do
    [ -z "$window_id" ] && continue
    yabai -m window "$window_id" --display "$target_index" >/dev/null 2>&1 || true
  done <<<"$window_ids"
}

focus_space() {
  local space_index

  space_index=$(query_space_index "$LABEL")
  [ -z "$space_index" ] && return 0

  yabai -m space --focus "$space_index" >/dev/null 2>&1 || true
}

if [ "$DISPLAY_COUNT" -le 1 ]; then
  focus_space
  exit 0
fi

case "$MODE" in
  normal|external-first)
    ensure_space_label "$LABEL" || exit 0

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
    ensure_space_label "$LABEL" || exit 0

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
