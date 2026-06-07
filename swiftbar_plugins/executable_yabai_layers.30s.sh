#!/bin/bash

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

# <swiftbar.runInBash>true</swiftbar.runInBash>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>

# Print the menu-bar title with an EXPLICIT color so SwiftBar never falls back to
# its dynamic NSColor.controlTextColor default. That default re-resolves at draw
# time and -- because SwiftBar never sets the status button's appearance -- briefly
# paints BLACK (light/Aqua context) before re-resolving white on the dark menu bar,
# which is the black-then-white flash on every refresh. A fixed color (parsed once
# into a static RGB NSColor) has no draw-time re-resolution, so no flash. The pair is
# light,dark: black in Light mode, white on the dark menu bar -- correct in both.
emit() { printf '%s | color=black,white\n' "$1"; }

SPACE_JSON=$(yabai -m query --spaces --space 2>/dev/null) || { emit "yabai"; exit 0; }
MODE=$(jq -r '.type // ""' <<<"$SPACE_JSON" 2>/dev/null)

WINDOW_JSON=$(yabai -m query --windows --window 2>/dev/null) || { emit "—"; exit 0; }
IS_FLOATING=$(jq -r '."is-floating" // false' <<<"$WINDOW_JSON" 2>/dev/null)

if [ "$IS_FLOATING" = "true" ]; then
  emit "FLOAT"
  exit 0
fi

if [ "$MODE" = "bsp" ]; then
  emit "BSP"
  exit 0
fi

CURRENT_LAYER=$(jq -r '."stack-index" // 0' <<<"$WINDOW_JSON" 2>/dev/null)

SPACE_WINDOWS_JSON=$(yabai -m query --windows --space 2>/dev/null) || { emit "—"; exit 0; }
TOTAL_LAYERS=$(jq -r 'map(select(."stack-index" > 0) | ."stack-index") | max // 0' <<<"$SPACE_WINDOWS_JSON" 2>/dev/null)

if [ "${TOTAL_LAYERS:-0}" -le 1 ] || [ "${CURRENT_LAYER:-0}" -le 0 ]; then
  CURRENT_LAYER=1
  TOTAL_LAYERS=1
fi

emit "$CURRENT_LAYER / $TOTAL_LAYERS"
