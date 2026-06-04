#!/bin/bash

# Keep the canonical labeled spaces in a STABLE order on each display, so they
# always appear in the same sequence no matter which display they're on or which
# ones have been pushed elsewhere ("the order they'd be even if some were
# removed"). Out-of-order labels are slid into place with `space --move`; when
# everything is already ordered this is a cheap query-only no-op.
#
# On the master (laptop) display, labels start at the first space. On a non-master
# (external) display the first space is left as a throwaway "scratch" (macOS
# destroys a display's first space on disconnect, so a label must never sit
# there), and labels are ordered from the second space on.

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

MASTER_DISPLAY_UUID="${YABAI_MASTER_DISPLAY_UUID:-37D8832A-2D66-02CA-B9F7-8F30A301B230}"

LABELS="terminal main school todo schedule mail calendar messages chatgpt codex"

displays=$(yabai -m query --displays 2>/dev/null) || exit 0

master=$(printf '%s' "$displays" | jq -r --arg u "$MASTER_DISPLAY_UUID" '
  ([.[] | select(.uuid == $u) | .index][0]) // (min_by(.frame.w * .frame.h).index) // empty
' 2>/dev/null)

for disp in $(printf '%s' "$displays" | jq -r '.[].index' 2>/dev/null); do
  lo=$(yabai -m query --spaces 2>/dev/null |
    jq -r --argjson d "$disp" '[.[] | select(.display == $d) | .index] | min // empty' 2>/dev/null)
  case "$lo" in ''|*[!0-9]*) continue ;; esac

  # Reserve the first space of a non-master display as scratch.
  if [ -n "$master" ] && [ "$disp" != "$master" ]; then
    pos=$((lo + 1))
  else
    pos=$lo
  fi

  for label in $LABELS; do
    info=$(yabai -m query --spaces --space "$label" 2>/dev/null) || continue
    d=$(printf '%s' "$info" | jq -r '.display // empty' 2>/dev/null)
    [ "$d" = "$disp" ] || continue
    cur=$(printf '%s' "$info" | jq -r '.index // empty' 2>/dev/null)
    case "$cur" in ''|*[!0-9]*) pos=$((pos + 1)); continue ;; esac
    [ "$cur" != "$pos" ] && yabai -m space "$label" --move "$pos" >/dev/null 2>&1
    pos=$((pos + 1))
  done
done
