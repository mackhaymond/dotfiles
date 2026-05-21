#!/bin/bash

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

WINDOW_JSON=$(yabai -m query --windows --window 2>/dev/null) || exit 0
CURRENT_LAYER=$(jq -r '."stack-index" // 0' <<<"$WINDOW_JSON" 2>/dev/null) || exit 0

if [ "${CURRENT_LAYER:-0}" -le 0 ]; then
  exit 0
fi

SPACE_JSON=$(yabai -m query --windows --space 2>/dev/null) || exit 0
TOTAL_LAYERS=$(jq -r 'map(select(."stack-index" > 0) | ."stack-index") | max // 0' <<<"$SPACE_JSON" 2>/dev/null) || exit 0

if [ "${TOTAL_LAYERS:-0}" -le 1 ]; then
  exit 0
fi

if [ "$CURRENT_LAYER" -gt 1 ]; then
  yabai -m window --focus stack.prev >/dev/null 2>&1 || true
else
  yabai -m window --focus stack.last >/dev/null 2>&1 || true
fi
