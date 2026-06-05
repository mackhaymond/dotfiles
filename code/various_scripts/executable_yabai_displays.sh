#!/bin/bash

# Debounced display hotplug handler. Wired to yabai's display_added /
# display_removed signals.
#
#   yabai_displays.sh added     -> a display was connected
#   yabai_displays.sh removed   -> a display was disconnected
#
# Phase 1 policy:
#   * added   = NON-DESTRUCTIVE. Refresh the cache/labels so the new display is
#               known, but move NOTHING. The external comes up empty-and-ready;
#               the user pushes spaces over manually (hyper+\). No auto-reset to
#               the laptop (this is the key fix over the old refresh path).
#   * removed = pull every label home to the remaining (laptop) display as a
#               safety net (macOS usually reparents already), then refresh.
#
# Hardening: a single-flight mkdir lock coalesces the 2-3 duplicate signals macOS
# emits per real transition (resolution/HDR/clamshell renegotiation); a settle
# poll replaces the old blind `sleep 1`; a transition guard makes redundant fires
# cheap no-ops. flock is absent on this machine, hence mkdir.

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

CACHE_FILE="${YABAI_WORKSPACE_CACHE:-${HOME}/.cache/yabai/workspace_cache.env}"
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/yabai_common.sh"
LOCKDIR="${TMPDIR:-/tmp}/yabai_displays.lock"

ACTION="${1:-}"
case "$ACTION" in
  added|removed) ;;
  *) exit 64 ;;
esac

# --- single-flight lock (coalesce duplicate hotplug signals) ----------------
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  # Break a stale lock (older than ~30s) left by a crashed run, then try once
  # more. 30s is well above a normal handler's runtime, so a slow-but-live run is
  # never pre-empted by a concurrent one. Age is computed in seconds via stat:
  # BSD find rejects a fractional `-mmin +0.5`, so the old form silently never
  # fired and a crash-orphaned lock would wedge all hotplug handling forever.
  now=$(date +%s 2>/dev/null)
  lock_mtime=$(stat -f %m "$LOCKDIR" 2>/dev/null)
  if [ -n "$now" ] && [ -n "$lock_mtime" ] && [ "$((now - lock_mtime))" -gt 30 ]; then
    rmdir "$LOCKDIR" 2>/dev/null || true
  fi
  mkdir "$LOCKDIR" 2>/dev/null || exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

# --- settle: wait until the display count is stable across two reads --------
settle_count() {
  local prev="" cur="" i=0
  while [ "$i" -lt 20 ]; do
    cur=$(yabai -m query --displays 2>/dev/null | jq -r 'length' 2>/dev/null)
    case "$cur" in ''|*[!0-9]*) cur="" ;; esac
    if [ -n "$cur" ] && [ "$cur" = "$prev" ]; then
      printf '%s' "$cur"
      return 0
    fi
    prev="$cur"
    sleep 0.15
    i=$((i + 1))
  done
  # Timed out (~3s) without two stable reads (very slow system); use one final
  # live query rather than aborting on a possibly-empty value.
  if [ -z "$cur" ]; then
    cur=$(yabai -m query --displays 2>/dev/null | jq -r 'length' 2>/dev/null)
    case "$cur" in ''|*[!0-9]*) cur="" ;; esac
  fi
  printf '%s' "${cur:-}"
}

cached_count() {
  [ -r "$CACHE_FILE" ] || { printf ''; return; }
  ( . "$CACHE_FILE" 2>/dev/null; printf '%s' "${DISPLAY_COUNT:-}" )
}

cached_master() {
  [ -r "$CACHE_FILE" ] || { printf ''; return; }
  ( . "$CACHE_FILE" 2>/dev/null; printf '%s' "${MASTER_DISPLAY_INDEX:-}" )
}

count=$(settle_count)
case "$count" in
  ''|*[!0-9]*) exit 0 ;;
esac

# --- transition guard: ignore redundant fires that change nothing -----------
if [ "$count" = "$(cached_count)" ]; then
  exit 0
fi

# Refresh first so the cache + labels reflect the new topology (master/external
# indices, DISPLAY_COUNT). This never moves spaces between displays.
"$SCRIPT_DIR/yabai_workspace_refresh.sh" >/dev/null 2>&1 || true

case "$ACTION" in
  added)
    # Non-destructive: nothing to move. External is empty-and-ready.
    :
    ;;
  removed)
    # Safety net: ensure every label is on the remaining (master) display.
    # macOS normally reparents on disconnect, so this is usually a no-op; the
    # native move carries any stragglers (and their windows) home.
    # Resolve master from LIVE topology (UUID first, smallest-area fallback) so a
    # failed cache write can't point pull-home at the just-removed display. Falls
    # back to the cached index, then to 1. (LIVE-first is deliberate here -- do not
    # switch to a cache-first resolve.)
    master=$(yabai_master_index)
    case "$master" in ''|*[!0-9]*) master=$(cached_master) ;; esac
    case "$master" in ''|*[!0-9]*) master=1 ;; esac
    for label in $YABAI_LABELS; do
      disp=$(yabai -m query --spaces --space "$label" 2>/dev/null | jq -r '.display // empty' 2>/dev/null)
      [ -n "$disp" ] && [ "$disp" != "$master" ] &&
        yabai -m space "$label" --display "$master" >/dev/null 2>&1 || true
    done
    yabai -m rule --apply >/dev/null 2>&1 || true
    # Final refresh so labels/cache settle after the pull-home.
    "$SCRIPT_DIR/yabai_workspace_refresh.sh" >/dev/null 2>&1 || true
    ;;
esac

exit 0
