#!/bin/bash

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

# hyper+x. STACK space: focus the previous stack layer (wrap to last). BSP space: the
# stack is meaningless, so repurpose the otherwise-dead key to MIRROR the tree
# VERTICALLY (--mirror y-axis). hyper+z is the horizontal counterpart.
SPACE_TYPE=$(yabai -m query --spaces --space 2>/dev/null | jq -r '.type // ""' 2>/dev/null)
if [ "$SPACE_TYPE" = "bsp" ]; then
  yabai -m space --mirror y-axis >/dev/null 2>&1 || true
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

if [ "$CURRENT_LAYER" -gt 1 ]; then
  yabai -m window --focus stack.prev >/dev/null 2>&1 || true
else
  yabai -m window --focus stack.last >/dev/null 2>&1 || true
fi
