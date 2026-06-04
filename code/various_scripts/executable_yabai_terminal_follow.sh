#!/bin/bash

# Keep the "terminal" label pinned to WezTerm wherever it lives -- including when
# it roams into (or out of) a macOS NATIVE-FULLSCREEN Space, which is a brand-new,
# unlabeled Space that WezTerm moves to while the old "terminal" space is left
# behind empty. Without this, `focus terminal` (hyper+`) would land on that empty
# husk instead of the fullscreen WezTerm.
#
# Designed to be cheap enough to run on every space_changed: it does two queries
# and, on the common case (label already on WezTerm's space), exits immediately.
# It only relabels + reorders when WezTerm has actually changed Spaces.

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

# Re-pin "terminal" onto WezTerm's current space. A label is unique, so free the
# old holder (the empty husk, or wherever it drifted) before moving it.
if [ -n "$tspace" ]; then
  yabai -m space "$tspace" --label >/dev/null 2>&1 || true
fi
yabai -m space "$wspace" --label terminal >/dev/null 2>&1 || true

# Slot the (possibly fullscreen) terminal space back into canonical order.
"$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)/yabai_reorder_spaces.sh" >/dev/null 2>&1 || true
