#!/bin/bash

# Focus the Nth native-fullscreen app (1-based), in Mission Control order
# (display first, then space index). These are apps you've put into macOS native
# fullscreen -- non-pinned apps or the browser -- which live in their own Spaces
# OUTSIDE the labeled-workspace model, so the hyper+<label> focus keys can't reach
# them. hyper+3 -> 1st fullscreen app, hyper+4 -> 2nd, ... No-op if fewer exist.
#
# WezTerm is EXCLUDED: although a fullscreen WezTerm is also a fullscreen Space, it
# is the "terminal" workspace and is reached with hyper+` (the terminal label
# follows it), so it must never consume one of these ordinals.
#
#   yabai_fullscreen_focus.sh <ordinal>

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

N="${1:-}"
case "$N" in ''|*[!0-9]*) exit 64 ;; esac
[ "$N" -lt 1 ] && exit 64

# Query WINDOWS (not spaces) so we can drop WezTerm by app name: a native-fullscreen
# window reports is-native-fullscreen=true and carries its Space in .space. Order
# deterministically so the Nth key always maps to the same on-screen position: by
# display, then by space index (left-to-right).
idx=$(yabai -m query --windows 2>/dev/null \
  | jq -r --argjson n "$N" '
      [ .[]
        | select(.["is-native-fullscreen"] == true)
        | select((.app | test("^(wezterm-gui|WezTerm)$")) | not)
      ]
      | sort_by(.display, .space)
      | (.[$n - 1].space) // empty
    ' 2>/dev/null)

# Fewer than N (non-WezTerm) fullscreen apps open -> nothing to focus.
[ -z "$idx" ] && exit 0

yabai -m space --focus "$idx" >/dev/null 2>&1 || true
