#!/bin/bash

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

# <swiftbar.runInBash>true</swiftbar.runInBash>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>

SPACE_JSON=$(yabai -m query --spaces --space 2>/dev/null) || { echo "yabai"; exit 0; }
MODE=$(jq -r '.type // ""' <<<"$SPACE_JSON" 2>/dev/null)

WINDOW_JSON=$(yabai -m query --windows --window 2>/dev/null) || { echo "—"; exit 0; }
IS_FLOATING=$(jq -r '."is-floating" // false' <<<"$WINDOW_JSON" 2>/dev/null)

if [ "$IS_FLOATING" = "true" ]; then
  echo "FLOAT"
  exit 0
fi

if [ "$MODE" = "bsp" ]; then
  echo "BSP"
  exit 0
fi

CURRENT_LAYER=$(jq -r '."stack-index" // 0' <<<"$WINDOW_JSON" 2>/dev/null)

SPACE_WINDOWS_JSON=$(yabai -m query --windows --space 2>/dev/null) || { echo "—"; exit 0; }
TOTAL_LAYERS=$(jq -r 'map(select(."stack-index" > 0) | ."stack-index") | max // 0' <<<"$SPACE_WINDOWS_JSON" 2>/dev/null)

if [ "${TOTAL_LAYERS:-0}" -le 1 ] || [ "${CURRENT_LAYER:-0}" -le 0 ]; then
  CURRENT_LAYER=1
  TOTAL_LAYERS=1
fi

printf '%s\n' "$CURRENT_LAYER / $TOTAL_LAYERS"
