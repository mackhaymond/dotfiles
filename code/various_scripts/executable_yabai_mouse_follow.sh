#!/bin/bash

# Warp the mouse onto the focused display when the ACTIVE DISPLAY changes.
# Wired to yabai's display_changed signal, so it covers every command that moves
# focus across displays (F13/F14, push, home-all, master, a focus bind whose
# label lives on the other screen) without touching those scripts.
#
# Cross-display ONLY:
#   * display_changed cannot fire with a single display, so single-laptop use is
#     completely unaffected (the signal simply never runs).
#   * if the cursor already sits on the focused display, it does nothing — so a
#     manual click onto the other display never yanks the pointer to a center.
#
# Uses CGWarpMouseCursorPosition via the osascript JXA ObjC bridge (no external
# dependency). yabai frame coordinates are already top-left/global, the exact
# space CGWarpMouseCursorPosition expects, including negative origins for a
# display positioned left of / above the laptop.

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

fdisp=$(yabai -m query --displays --display 2>/dev/null) || exit 0
fidx=$(printf '%s' "$fdisp" | jq -r '.index // empty' 2>/dev/null)
[ -z "$fidx" ] && exit 0

# Cursor already on the focused display -> leave it alone.
midx=$(yabai -m query --displays --display mouse 2>/dev/null | jq -r '.index // empty' 2>/dev/null)
[ -n "$midx" ] && [ "$midx" = "$fidx" ] && exit 0

# Prefer the focused window's center; fall back to the focused display's center.
target=$(yabai -m query --windows --window 2>/dev/null |
  jq -r --argjson fidx "$fidx" '
    select(.display == $fidx and .frame != null)
    | "\(.frame.x + .frame.w / 2) \(.frame.y + .frame.h / 2)"
  ' 2>/dev/null | head -n 1)
[ -z "$target" ] && target=$(printf '%s' "$fdisp" |
  jq -r '"\(.frame.x + .frame.w / 2) \(.frame.y + .frame.h / 2)"' 2>/dev/null)
[ -z "$target" ] && exit 0

tx=${target%% *}
ty=${target##* }
case "$tx" in ''|*[!0-9.-]*) exit 0 ;; esac
case "$ty" in ''|*[!0-9.-]*) exit 0 ;; esac

osascript -l JavaScript \
  -e 'function run(a){ObjC.import("CoreGraphics");$.CGWarpMouseCursorPosition({x:parseFloat(a[0]),y:parseFloat(a[1])});}' \
  "$tx" "$ty" >/dev/null 2>&1 || true

exit 0
