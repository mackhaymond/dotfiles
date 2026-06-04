#!/bin/bash

# Focus the Nth native-fullscreen space (1-based), in Mission Control order
# (display first, then space index). These hold apps you've put into macOS native
# fullscreen -- non-pinned apps or the browser -- which live in their own Spaces
# OUTSIDE the labeled-workspace model, so the hyper+<label> focus keys can't reach
# them. hyper+3 -> 1st fullscreen app, hyper+4 -> 2nd, ... No-op if fewer exist.
#
#   yabai_fullscreen_focus.sh <ordinal>

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

N="${1:-}"
case "$N" in ''|*[!0-9]*) exit 64 ;; esac
[ "$N" -lt 1 ] && exit 64

# A native-fullscreen Space reports is-native-fullscreen=true (see yabai query).
# Order them deterministically so the Nth key always maps to the same on-screen
# position: by display, then by mission-control index (left-to-right).
idx=$(yabai -m query --spaces 2>/dev/null \
  | jq -r --argjson n "$N" '
      [ .[] | select(.["is-native-fullscreen"] == true) ]
      | sort_by(.display, .index)
      | (.[$n - 1].index) // empty
    ' 2>/dev/null)

# Fewer than N fullscreen apps open -> nothing to focus.
[ -z "$idx" ] && exit 0

yabai -m space --focus "$idx" >/dev/null 2>&1 || true
