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

displays=$(yabai_displays_json) || exit 0

master=$(yabai_master_index "$displays")

# Reordering must never change the focused space; a burst of `space --move` can make
# macOS yank the ACTIVE desktop to some other space (a yabai/macOS quirk that surfaces
# under rapid moves + concurrent queries), so the user who just pressed hyper+<space>
# would see the view jump off their target. We snap back at the end if that happened.
#
# Only for a change seen ACROSS one of our own --move calls (focus read right before and
# right after it, by STABLE id -- indices renumber as spaces move). Restoring to whatever
# was focused on entry also undid the user's own switch to another space mid-run. If
# focus moves between our calls instead, that was the user: forget the earlier yank.
# Nothing is read when nothing moves.
focused_id() {
  yabai -m query --spaces --space 2>/dev/null | jq -r '.id // empty' 2>/dev/null
}
drift_from=""   # focused before the first yank
drift_to=""     # where the latest yank left it
move_space() {
  local before after rc
  before=$(focused_id)
  [ -n "$before" ] && [ -n "$drift_to" ] && [ "$before" != "$drift_to" ] &&
    { drift_from=""; drift_to=""; }
  yabai -m space "$1" --move "$2" >/dev/null 2>&1; rc=$?
  after=$(focused_id)
  if [ -n "$before" ] && [ -n "$after" ] && [ "$before" != "$after" ]; then
    drift_from=${drift_from:-$before}
    drift_to=$after
  fi
  return "$rc"
}

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
        move_space "$label" "$pos" && moved=1
      fi
      pos=$((pos + 1))
    done
  done
done

# Restore focus if a move of ours drifted the active desktop, and the user has not gone
# elsewhere since. No-op when nothing moved (the common already-ordered case stays a
# cheap query-only pass) or focus held. Matched by id, not index, because the moves
# renumbered the spaces.
if [ -n "$drift_to" ] && [ "$drift_to" != "$drift_from" ]; then
  if [ "$(focused_id)" = "$drift_to" ]; then
    tgt=$(yabai -m query --spaces 2>/dev/null |
      jq -r --argjson id "$drift_from" '([.[] | select(.id == $id) | .index][0]) // empty' 2>/dev/null)
    [ -n "$tgt" ] && yabai -m space --focus "$tgt" >/dev/null 2>&1 || true
  fi
fi
