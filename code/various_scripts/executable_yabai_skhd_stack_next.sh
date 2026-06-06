#!/bin/bash

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

# hyper+z. STACK space: focus the next stack layer (wrap to first). BSP space: the
# stack is meaningless, so repurpose the otherwise-dead key to MIRROR the tree
# HORIZONTALLY (--mirror x-axis). hyper+x is the vertical counterpart.
SPACE_TYPE=$(yabai -m query --spaces --space 2>/dev/null | jq -r '.type // ""' 2>/dev/null)
if [ "$SPACE_TYPE" = "bsp" ]; then
  yabai -m space --mirror x-axis >/dev/null 2>&1 || true
  exit 0
fi

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

if [ "$CURRENT_LAYER" -lt "$TOTAL_LAYERS" ]; then
  yabai -m window --focus stack.next >/dev/null 2>&1 || true
else
  yabai -m window --focus stack.first >/dev/null 2>&1 || true
fi
