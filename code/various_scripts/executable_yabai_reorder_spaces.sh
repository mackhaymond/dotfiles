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

# shellcheck source=/dev/null
. "$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)/yabai_common.sh"

displays=$(yabai -m query --displays 2>/dev/null) || exit 0

master=$(yabai_master_index "$displays")

# Each `space --move` renumbers indices on its display, so a single pass that
# derives the target `pos` from a pre-move snapshot can leave a genuinely scrambled
# display short of canonical order. Re-run the whole placement pass, re-deriving the
# per-display base index each time, until a full pass issues NO move (converged) or
# a small attempt cap is hit. When everything is already ordered the first pass
# moves nothing and the loop exits immediately -- the common case stays a cheap
# query-only no-op, byte-identical to before.
attempt=0
moved=1
while [ "$moved" = "1" ] && [ "$attempt" -lt 4 ]; do
  moved=0
  attempt=$((attempt + 1))

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

    for label in $YABAI_LABELS; do
      info=$(yabai -m query --spaces --space "$label" 2>/dev/null) || continue
      d=$(printf '%s' "$info" | jq -r '.display // empty' 2>/dev/null)
      [ "$d" = "$disp" ] || continue
      cur=$(printf '%s' "$info" | jq -r '.index // empty' 2>/dev/null)
      case "$cur" in ''|*[!0-9]*) pos=$((pos + 1)); continue ;; esac
      if [ "$cur" != "$pos" ]; then
        yabai -m space "$label" --move "$pos" >/dev/null 2>&1 && moved=1
      fi
      pos=$((pos + 1))
    done
  done
done
