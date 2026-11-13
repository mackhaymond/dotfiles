#!/bin/bash

# Debounced self-heal. Coalesce a burst of space_destroyed / mission_control_exit
# signals into ONE yabai_workspace_refresh: a Mission Control session that churns
# several spaces (or a fullscreen collapse that merges an adjacent space and drops
# its label) should heal exactly once, after it settles -- not N times mid-churn.
#
# Single-flight via an mkdir lock: the first event takes the lock, waits a short
# settle for the burst to finish, then refreshes; concurrent events drop (the
# in-flight refresh captures the settled end state). refresh is idempotent (~0.34s),
# and the only thing this path destroys is surplus EMPTY UNLABELED spaces (`--sweep`
# below, one kept per display), so an occasional redundant run is harmless.
#
#   yabai_heal.sh            (bound to space_destroyed / mission_control_exit)

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=/dev/null  # required sibling: the shared state dir
. "$SCRIPT_DIR/yabai_common.sh"
# In the shared state dir (not ${TMPDIR:-/tmp}, see yabai_common.sh) so the float
# sweep in yabai_startup_reconcile.sh can SEE a heal in progress: a refresh/reorder
# focuses spaces transiently, and an un-float landing during one must take the
# view-rebuild route rather than trust the focused space.
mkdir -p "$YABAI_STATE_DIR" 2>/dev/null
LOCK="$YABAI_STATE_DIR/heal.lock"
SETTLE="${YABAI_HEAL_SETTLE:-0.4}"

# Single-flight. A lock orphaned by a crashed holder (dead owner, or older than 30s)
# is recovered, and the release is ownership-aware: a run that stalled past 30s and
# was taken over must not remove its successor's lock on exit (yabai_lock_* in
# yabai_common.sh).
yabai_lock_acquire "$LOCK" 30 || exit 0
trap 'yabai_lock_release "$LOCK"' EXIT

# Let the burst (several destroys, or a Mission Control exit) settle before we read
# state, so we reconcile the final topology exactly once.
sleep "$SETTLE"

# --sweep: also trim surplus empty unlabeled spaces (one kept per display) once the
# labels are settled. Asked for here, not on every refresh: this is the path that
# runs after spaces were destroyed or rearranged, and it is debounced; a refresh
# fired from a keybind mid-operation should not be destroying spaces.
"$SCRIPT_DIR/yabai_workspace_refresh.sh" --sweep >/dev/null 2>&1 || true
