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

# WezTerm's real windows, as a JSON array of window ids in window order. Only
# non-minimized standard root windows count: taking the first WezTerm window of ANY kind
# let a minimized one, or a non-standard helper window parked elsewhere, drag the label
# off the real terminal.
wids=$(yabai -m query --windows 2>/dev/null \
  | jq -c '[.[] | select(.app | test("^(wezterm-gui|WezTerm)$"))
                | select(.subrole == "AXStandardWindow" and ."root-window" and (."is-minimized" | not))
                | .id]' 2>/dev/null)

# WezTerm not open -> nothing to anchor; leave the label where it is.
case "$wids" in ''|'[]') exit 0 ;; esac

# Which space each is on AND where the `terminal` label sits, both from ONE spaces
# snapshot and by space id: "<terminal's id or none> <space id>...". Two queries compared
# by index went wrong when the spaces renumbered in between (a space created, a display
# swapping sides): WezTerm then looked as if it had left `terminal`, and the label was
# put on some other space. A bad answer (a bare "[") emits nothing, and we leave.
snap=$(yabai -m query --spaces 2>/dev/null | jq -r --argjson wids "$wids" '
  . as $s
  | ([.[] | select(.label == "terminal") | .id][0] // "none") as $t
  | [$wids[] as $w | [$s[] | select(.windows | index($w)) | .id][0] // empty]
  | [$t] + . | map(tostring) | join(" ")' 2>/dev/null)
wsids=${snap#* }
[ -n "$snap" ] && [ "$wsids" != "$snap" ] || exit 0
tsid=${snap%% *}

# Fast path: already correct (every ordinary space switch hits this) -> no-op. ANY
# WezTerm window on the terminal space counts, so a second window opened elsewhere
# does not pull the label away from the first.
case " $wsids " in *" $tsid "*) exit 0 ;; esac
wsid=${wsids%% *}

# Stand down while a display hotplug is being reconciled: yabai_displays.sh holds
# this lock and is itself moving/relabeling/reordering spaces, and its pull-home
# moves fire space_changed, which re-entrantly invokes us. Let it finish -- its
# final workspace_refresh leaves canonical order, and a later space_changed re-runs
# us cleanly. Same lock dir as yabai_displays.sh; never present on a stable session.
# yabai_lock_live, not a bare `-d`: a lock whose holder died (a yabai restart mid-
# hotplug) used to keep this hook off until something else reaped the directory.
# (yabai_common.sh is sourced only here, past the fast path: sourcing it forks, and
# this runs on every space switch.)
# shellcheck source=/dev/null
. "$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)/yabai_common.sh"
yabai_lock_live "${TMPDIR:-/tmp}/yabai_displays.lock" && exit 0

# Never take a space that already carries ANOTHER canonical label. A WezTerm window
# dragged onto `main` in Mission Control (or caught there by the launch race) used to
# get `main` overwritten with `terminal`, leaving the real terminal space unlabeled.
# That window is the stray, not the label: leave everything as it is and let the pin
# rule / window_created nudge send WezTerm home.
# Re-resolved by id from a fresh snapshot: the index and label are what we act on next.
winfo=$(yabai -m query --spaces 2>/dev/null | jq -r --argjson id "$wsid" '
  .[] | select(.id == $id) | "\(.index) \(.label // "")"' 2>/dev/null | head -n 1)
wspace=${winfo%% *}
wlabel=${winfo#* }
case "$wspace" in ''|*[!0-9]*) exit 0 ;; esac
if [ -n "$wlabel" ] && [ "$wlabel" != terminal ]; then
  case " $YABAI_LABELS " in *" $wlabel "*) exit 0 ;; esac
fi

# Re-pin "terminal" onto WezTerm's current space. A label is unique, so free the
# old holder (the empty husk, or wherever it drifted) before moving it.
# By name, not index: whatever holds it now is what must give it up.
[ "$tsid" = none ] || yabai -m space terminal --label >/dev/null 2>&1 || true
yabai -m space "$wspace" --label terminal >/dev/null 2>&1 || true

# Slot the (possibly fullscreen) terminal space back into canonical order.
"$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)/yabai_reorder_spaces.sh" >/dev/null 2>&1 || true

# Sweep husk spaces. Entering/exiting native fullscreen leaves the vacated Space
# behind as an empty desktop; over time these pile up. Keeps exactly ONE empty,
# unlabeled space on the laptop (policy + guards: yabai_sweep_empty_spaces in
# yabai_common.sh, shared with the heal path's `yabai_workspace_refresh.sh --sweep`).
# Under refresh.lock like that one: a space a refresh or yabai_ensure_ext has created
# and not yet labeled looks like a husk. If the lock stays busy (~2s) skip the sweep
# rather than run it unguarded; the next heal or relabel sweeps.
if yabai_lock_acquire_wait "$YABAI_STATE_DIR/refresh.lock" 20; then
  yabai_sweep_empty_spaces
  yabai_lock_release "$YABAI_STATE_DIR/refresh.lock"
fi
