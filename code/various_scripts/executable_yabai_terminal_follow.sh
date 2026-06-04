#!/bin/bash

# Keep the "terminal" label pinned to WezTerm wherever it lives -- including when
# it roams into (or out of) a macOS NATIVE-FULLSCREEN Space, which is a brand-new,
# unlabeled Space that WezTerm moves to while the old "terminal" space is left
# behind empty. Without this, `focus terminal` (hyper+`) would land on that empty
# husk instead of the fullscreen WezTerm.
#
# Designed to be cheap enough to run on every space_changed: it does two queries
# and, on the common case (label already on WezTerm's space), exits immediately.
# It only relabels + reorders when WezTerm has actually changed Spaces. When it
# does, it also sweeps the empty "husk" desktops that fullscreen enter/exit leaves
# behind (keeping one landing pad) so they don't pile up.

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

MASTER_DISPLAY_UUID="${YABAI_MASTER_DISPLAY_UUID:-37D8832A-2D66-02CA-B9F7-8F30A301B230}"

# WezTerm's current space (it carries the window into its fullscreen Space).
wspace=$(yabai -m query --windows 2>/dev/null \
  | jq -r 'first(.[] | select(.app | test("^(wezterm-gui|WezTerm)$")) | .space) // empty' 2>/dev/null)

# WezTerm not open -> nothing to anchor; leave the label where it is.
[ -z "$wspace" ] && exit 0

# Where the "terminal" label sits right now.
tspace=$(yabai -m query --spaces --space terminal 2>/dev/null | jq -r '.index // empty' 2>/dev/null)

# Fast path: already correct (every ordinary space switch hits this) -> no-op.
[ "$wspace" = "$tspace" ] && exit 0

# Re-pin "terminal" onto WezTerm's current space. A label is unique, so free the
# old holder (the empty husk, or wherever it drifted) before moving it.
if [ -n "$tspace" ]; then
  yabai -m space "$tspace" --label >/dev/null 2>&1 || true
fi
yabai -m space "$wspace" --label terminal >/dev/null 2>&1 || true

# Slot the (possibly fullscreen) terminal space back into canonical order.
"$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)/yabai_reorder_spaces.sh" >/dev/null 2>&1 || true

# Sweep husk spaces. Entering/exiting native fullscreen leaves the Space WezTerm
# vacated behind as an empty desktop; over time these would pile up. Destroy the
# surplus empty, UNLABELED, NON-fullscreen spaces on the master display, keeping
# ONE as a safe landing pad for when WezTerm exits fullscreen. Never touches a
# labeled or fullscreen Space, and is a no-op when there is <=1 empty.
master=$(yabai -m query --displays 2>/dev/null | jq -r --arg u "$MASTER_DISPLAY_UUID" '
  ([.[] | select(.uuid == $u) | .index][0]) // (min_by(.frame.w * .frame.h).index) // empty' 2>/dev/null)
if [ -n "$master" ]; then
  surplus=$(yabai -m query --spaces 2>/dev/null | jq -r --argjson d "$master" '
    [ .[]
      | select(.display == $d and .label == "" and (.windows | length == 0) and (.["is-native-fullscreen"] == false))
      | .index
    ] | sort | reverse | .[1:] | .[]' 2>/dev/null)
  for sidx in $surplus; do
    yabai -m space "$sidx" --destroy >/dev/null 2>&1 || true
  done
fi
