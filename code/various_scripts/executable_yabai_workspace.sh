#!/bin/bash

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

MASTER_DISPLAY_UUID="${YABAI_MASTER_DISPLAY_UUID:-37D8832A-2D66-02CA-B9F7-8F30A301B230}"
CACHE_FILE="${YABAI_WORKSPACE_CACHE:-${TMPDIR:-/tmp}/yabai_workspace_cache.env}"

MODE="${1:-}"
LABEL="${2:-}"

if [ -z "$MODE" ] || [ -z "$LABEL" ]; then
  exit 64
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

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

if [ "$DISPLAY_COUNT" -le 1 ]; then
  yabai -m space --focus "$LABEL" >/dev/null 2>&1 || true
  exit 0
fi

case "$MODE" in
  external-first)
    FOCUSED_INDEX=$(
      yabai -m query --displays --display 2>/dev/null |
        sed -n 's/^[[:space:]]*"index":[[:space:]]*\([0-9][0-9]*\),.*/\1/p' |
        head -n 1
    )
    if [ "$FOCUSED_INDEX" = "$MASTER_DISPLAY_INDEX" ]; then
      TARGET_INDEX="$EXTERNAL_DISPLAY_INDEX"
    else
      TARGET_INDEX="$FOCUSED_INDEX"
    fi
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

yabai -m space "$LABEL" --display "$TARGET_INDEX" >/dev/null 2>&1 || true
yabai -m space --focus "$LABEL" >/dev/null 2>&1 || true
