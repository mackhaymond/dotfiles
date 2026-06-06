#!/bin/bash

# Debounced self-heal. Coalesce a burst of space_destroyed / mission_control_exit
# signals into ONE yabai_workspace_refresh: a Mission Control session that churns
# several spaces (or a fullscreen collapse that merges an adjacent space and drops
# its label) should heal exactly once, after it settles -- not N times mid-churn.
#
# Single-flight via an mkdir lock: the first event takes the lock, waits a short
# settle for the burst to finish, then refreshes; concurrent events drop (the
# in-flight refresh captures the settled end state). refresh is idempotent and
# non-destructive (~0.34s), so an occasional redundant run is harmless.
#
#   yabai_heal.sh            (bound to space_destroyed / mission_control_exit)

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
LOCK="${TMPDIR:-/tmp}/yabai_heal.lock"
SETTLE="${YABAI_HEAL_SETTLE:-0.4}"

# Single-flight. Recover a lock orphaned by a crashed holder (older than 30s) using
# BSD stat -- matching yabai_displays.sh (GNU `find -mmin` is not portable here).
if ! mkdir "$LOCK" 2>/dev/null; then
  now=$(date +%s)
  mtime=$(stat -f %m "$LOCK" 2>/dev/null || printf '%s' "$now")
  if [ "$((now - mtime))" -ge 30 ]; then
    rmdir "$LOCK" 2>/dev/null || true
    mkdir "$LOCK" 2>/dev/null || exit 0
  else
    exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

# Let the burst (several destroys, or a Mission Control exit) settle before we read
# state, so we reconcile the final topology exactly once.
sleep "$SETTLE"

"$SCRIPT_DIR/yabai_workspace_refresh.sh" >/dev/null 2>&1 || true
