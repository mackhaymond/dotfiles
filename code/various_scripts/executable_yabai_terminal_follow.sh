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

# WezTerm's current space (it carries the window into its fullscreen Space).
wspace=$(yabai -m query --windows 2>/dev/null \
  | jq -r 'first(.[] | select(.app | test("^(wezterm-gui|WezTerm)$")) | .space) // empty' 2>/dev/null)

# WezTerm not open -> nothing to anchor; leave the label where it is.
[ -z "$wspace" ] && exit 0

# Where the "terminal" label sits right now.
tspace=$(yabai -m query --spaces --space terminal 2>/dev/null | jq -r '.index // empty' 2>/dev/null)

# Fast path: already correct (every ordinary space switch hits this) -> no-op.
[ "$wspace" = "$tspace" ] && exit 0

# Stand down while a display hotplug is being reconciled: yabai_displays.sh holds
# this lock and is itself moving/relabeling/reordering spaces, and its pull-home
# moves fire space_changed, which re-entrantly invokes us. Let it finish -- its
# final workspace_refresh leaves canonical order, and a later space_changed re-runs
# us cleanly. Same lock dir as yabai_displays.sh; never present on a stable session.
[ -d "${TMPDIR:-/tmp}/yabai_displays.lock" ] && exit 0

# Re-pin "terminal" onto WezTerm's current space. A label is unique, so free the
# old holder (the empty husk, or wherever it drifted) before moving it.
if [ -n "$tspace" ]; then
  yabai -m space "$tspace" --label >/dev/null 2>&1 || true
fi
yabai -m space "$wspace" --label terminal >/dev/null 2>&1 || true

# Slot the (possibly fullscreen) terminal space back into canonical order.
"$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)/yabai_reorder_spaces.sh" >/dev/null 2>&1 || true

# Sweep husk spaces. Entering/exiting native fullscreen leaves the vacated Space
# behind as an empty desktop; over time these pile up. Destroy the surplus empty,
# UNLABELED, NON-fullscreen spaces, keeping exactly ONE empty per display -- enough
# that the sweep is idempotent and doesn't thrash (macOS recreates a space on
# fullscreen-exit if none is suitable). group_by(.display) so a fullscreen toggle
# on the EXTERNAL display is cleaned too, each display keeping its own landing pad.
# Never touches a labeled or fullscreen Space; a no-op when every display has <=1
# empty. Indices are destroyed high-to-low so yabai's index compaction on --destroy
# can't stale a later target.
surplus=$(yabai -m query --spaces 2>/dev/null | jq -r '
  [ .[] | select(.label == "" and (.windows | length == 0) and (.["is-native-fullscreen"] == false)) ]
  | group_by(.display)
  | map(sort_by(.index) | reverse | .[1:])
  | flatten | map(.index) | sort | reverse | .[]' 2>/dev/null)
for sidx in $surplus; do
  yabai -m space "$sidx" --destroy >/dev/null 2>&1 || true
done
