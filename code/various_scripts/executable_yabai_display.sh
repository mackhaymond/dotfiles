#!/bin/bash

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

# shellcheck disable=SC2034  # consumed by yabai_load_cache (sourced from yabai_common.sh)
CACHE_FILE="${YABAI_WORKSPACE_CACHE:-${HOME}/.cache/yabai/workspace_cache.env}"
TARGET="${1:-}"

if [ -z "$TARGET" ]; then
  exit 64
fi

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/yabai_common.sh"

refresh_cache() {
  "$SCRIPT_DIR/yabai_workspace_refresh.sh" >/dev/null 2>&1 || true
  yabai_load_cache
}

focused_display_index() {
  yabai -m query --displays --display 2>/dev/null |
    jq -r '.index // empty' |
    head -n 1
}

if ! yabai_load_cache; then
  refresh_cache || exit 0
fi

case "$TARGET" in
  master|primary|laptop)
    TARGET_INDEX="${MASTER_DISPLAY_INDEX:-}"
    ;;
  external)
    if [ "${DISPLAY_COUNT:-0}" -le 1 ]; then
      exit 0
    fi
    TARGET_INDEX="${EXTERNAL_DISPLAY_INDEX:-}"
    ;;
  *)
    exit 64
    ;;
esac

if [ -z "${TARGET_INDEX:-}" ]; then
  exit 0
fi

# Bring the cursor along. display_changed -> mouse_follow covers a real focus
# change, but when focus is ALREADY on the target (e.g. the mouse was left on the
# other screen) no signal fires, so the key looked dead. mouse_follow is a no-op
# when the cursor is already on the focused display.
follow_mouse() {
  "$SCRIPT_DIR/yabai_mouse_follow.sh" >/dev/null 2>&1 || true
}

if [ "$(focused_display_index)" = "$TARGET_INDEX" ]; then
  follow_mouse
  exit 0
fi
trap follow_mouse EXIT

if ! yabai -m display --focus "$TARGET_INDEX" >/dev/null 2>&1; then
  refresh_cache || exit 0
  case "$TARGET" in
    master|primary|laptop)
      TARGET_INDEX="${MASTER_DISPLAY_INDEX:-}"
      ;;
    external)
      [ "${DISPLAY_COUNT:-0}" -le 1 ] && exit 0
      TARGET_INDEX="${EXTERNAL_DISPLAY_INDEX:-}"
      ;;
  esac

  if [ -n "${TARGET_INDEX:-}" ] && [ "$(focused_display_index)" != "$TARGET_INDEX" ]; then
    yabai -m display --focus "$TARGET_INDEX" >/dev/null 2>&1 || true
  fi
fi
