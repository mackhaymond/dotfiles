#!/bin/bash

# Debounced display hotplug handler. Wired to yabai's display_added /
# display_removed / display_moved / display_resized signals.
#
#   yabai_displays.sh added     -> a display was connected
#   yabai_displays.sh removed   -> a display was disconnected
#   yabai_displays.sh moved     -> a display was rearranged or changed resolution:
#               yabai's indices follow x, so the external crossing to the laptop's
#               other side swaps them. Same handling as `added` (refresh the cache,
#               move nothing). Log: ~/Library/Logs/yabai/displays.log.
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
  added|removed|moved) ;;
  *) exit 64 ;;
esac

# --- single-flight lock (coalesce duplicate hotplug signals) ----------------
# A stale lock (dead owner, or older than ~30s) left by a crashed run is broken.
# 30s is well above a normal handler's runtime, so a slow-but-live run is not
# normally pre-empted by a concurrent one -- and if one is, the ownership-aware
# release keeps its late exit from removing the lock of the run that took over
# (yabai_lock_* in yabai_common.sh; age is in seconds via BSD stat there, since
# BSD find rejects a fractional `-mmin +0.5`).
# `removed` WAITS for a live holder instead of dropping: it carries the pull-home, and a
# disconnect right after a resolution change would otherwise lose it to a `moved` run.
if [ "$ACTION" = removed ]; then
  yabai_lock_acquire_wait "$LOCKDIR" 100 || exit 0
else
  yabai_lock_acquire "$LOCKDIR" 30 || exit 0
fi
# refresh.lock too: the pull-home below holds it (the release is a no-op when we do not).
REFRESH_LOCK="$YABAI_STATE_DIR/refresh.lock"
trap 'yabai_lock_release "$REFRESH_LOCK"; yabai_lock_release "$LOCKDIR"' EXIT

# --- settle: wait until the display count is stable across two reads --------
settle_count() {
  local prev="" cur="" i=0
  while [ "$i" -lt 20 ]; do
    cur=$(yabai_displays_json | jq -r 'length' 2>/dev/null)
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
    cur=$(yabai_displays_json | jq -r 'length' 2>/dev/null)
    case "$cur" in ''|*[!0-9]*) cur="" ;; esac
  fi
  printf '%s' "${cur:-}"
}

# The cache's topology as `count:master:external` -- everything it records about the
# displays -- to compare with yabai_live_topology (yabai_common.sh).
cached_topology() {
  [ -r "$CACHE_FILE" ] || { printf ''; return; }
  ( . "$CACHE_FILE" 2>/dev/null
    printf '%s:%s:%s' "${DISPLAY_COUNT:-}" "${MASTER_DISPLAY_INDEX:-}" "${EXTERNAL_DISPLAY_INDEX:-}" )
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
# `added`/`moved` only. On undock macOS destroys the external's spaces, which fires
# space_destroyed -> yabai_heal.sh -> a refresh that can rewrite the cache to
# DISPLAY_COUNT=1 BEFORE this handler reads it; the guard then skipped the pull-home
# safety net and the `ext` dissolve entirely. Both are idempotent no-ops when there is
# nothing to do, and the lock above already coalesces duplicate fires.
# Compares the master/external INDICES too, not just the count: when an ignored display
# (agent-chrome's virtual screen) attaches, the real-display count is unchanged but
# yabai can renumber the displays, and a count-only guard left the cached indices
# pointing at the wrong display. Any unresolved live value fails the match and refreshes.
#
# Re-checked after the refresh, a bounded number of times: a `moved` that fires while we
# hold the lock is dropped, and OpenDisplay hops sides within ~2s of connecting.
if [ "$ACTION" != removed ]; then
  for _pass in 1 2 3; do
    live=$(yabai_live_topology)
    [ -n "$live" ] && [ "$live" = "$(cached_topology)" ] && break
    yabai_log displays "$ACTION pass=$_pass live=$live cached=$(cached_topology)"
    "$SCRIPT_DIR/yabai_workspace_refresh.sh" >/dev/null 2>&1 || true
    sleep 0.5
  done
  exit 0
fi

# Refresh first so the cache + labels reflect the new topology (master/external
# indices, DISPLAY_COUNT). This never moves spaces between displays.
yabai_log displays "removed count=$count cached=$(cached_topology)"
"$SCRIPT_DIR/yabai_workspace_refresh.sh" >/dev/null 2>&1 || true

case "$ACTION" in
  removed)
    # A real external is still attached, so what left was an ignored display
    # (agent-chrome's virtual screen) -- never dissolve `ext` or pull home for that.
    # (`count` is the FILTERED count: ignored displays are never in it.)
    [ "$count" -gt 1 ] && exit 0
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
    # Under refresh.lock (bounded wait, then unlocked), so a heal refresh cannot relabel
    # or sweep between these moves.
    yabai_lock_acquire_wait "$REFRESH_LOCK" ||
      yabai_log displays "removed: refresh.lock busy; pulling home unlocked"
    # Re-read right before acting: settle, the refresh and the lock wait above take
    # seconds, and a replug in that time (macOS has put the external's spaces back by
    # now) must not have its layout pulled home. Its own `added` was dropped, because
    # this run holds the single-flight lock, so refresh for it here.
    now=$(yabai_displays_json | jq -r 'length' 2>/dev/null)
    case "$now" in ''|*[!0-9]*) now="$count" ;; esac
    if [ "$now" -gt 1 ]; then
      yabai_log displays "removed: $now displays again; not pulling home"
    else
      yabai_pull_labels_home "$master"
      # Clean up the on-demand `ext` scratch-work space exactly as home-all does: move
      # its windows to main. If macOS already reparented `ext` onto the laptop on
      # disconnect it is destroyed here; if macOS destroyed it outright this is a no-op.
      yabai_dissolve_ext
      yabai -m rule --apply >/dev/null 2>&1 || true
    fi
    # Before the refresh, which takes this same lock.
    yabai_lock_release "$REFRESH_LOCK"
    # Final refresh so labels/cache settle after the pull-home.
    "$SCRIPT_DIR/yabai_workspace_refresh.sh" >/dev/null 2>&1 || true
    ;;
esac

exit 0
