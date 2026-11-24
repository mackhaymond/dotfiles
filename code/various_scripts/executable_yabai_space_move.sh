#!/bin/bash

# Explicit cross-display space moves (the only keys that relocate a space).
#
#   yabai_space_move.sh push       -> send the FOCUSED space to the other display, follow
#   yabai_space_move.sh home-all   -> pull every canonical label home to the laptop
#
# Both are no-ops with a single display. Moves are native whole-space operations
# (yabai -m space ... --display ...), so all of a space's windows travel with it. They
# go through yabai_space_to_display (yabai_common.sh), which knows which moves the Dock
# survives.

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

# shellcheck disable=SC2034  # consumed by yabai_load_cache (sourced from yabai_common.sh)
CACHE_FILE="${YABAI_WORKSPACE_CACHE:-${HOME}/.cache/yabai/workspace_cache.env}"
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/yabai_common.sh"

ACTION="${1:-}"
[ -z "$ACTION" ] && exit 64

# Live, not cached: display indices swap when the external changes sides.
if ! yabai_load_topology; then
  "$SCRIPT_DIR/yabai_workspace_refresh.sh" >/dev/null 2>&1 || true
  yabai_load_topology || exit 0
fi

# Single display: nothing to move anywhere.
if [ "${DISPLAY_COUNT:-1}" -le 1 ]; then
  exit 0
fi

# Both actions run under refresh.lock, the lock of everything that creates, labels or
# sweeps spaces: a refresh or heal sweep in the middle of a move saw half-moved labels.
# Bounded wait, then go ahead unlocked (a keypress must not hang), and the release only
# drops a lock this process took. Nothing called while it is held may take it:
# yabai_workspace_refresh.sh does (so it is only run above, before this), reorder and the
# yabai_common.sh helpers used here do not.
REFRESH_LOCK="$YABAI_STATE_DIR/refresh.lock"
take_lock() {
  trap 'yabai_lock_release "$REFRESH_LOCK"' EXIT
  yabai_lock_acquire_wait "$REFRESH_LOCK" ||
    yabai_log space_move "$ACTION: refresh.lock busy; going ahead unlocked"
}

case "$ACTION" in
  push)
    # Snapshot the focused space explicitly (id + index + display) up front, so
    # a focus drift between a preceding focus keypress and this one can't make us
    # move the wrong space. id is the stable post-move handle (indices renumber).
    info=$(yabai -m query --spaces --space 2>/dev/null) || exit 0
    sid=$(printf '%s' "$info" | jq -r '.id // empty' 2>/dev/null)
    sidx=$(printf '%s' "$info" | jq -r '.index // empty' 2>/dev/null)
    sdisp=$(printf '%s' "$info" | jq -r '.display // empty' 2>/dev/null)
    [ -z "$sidx" ] && exit 0

    # If the focused space is the on-demand `ext` scratch-work space, "pushing it
    # home" DISSOLVES it into main (windows moved there, label dropped) and lands
    # you on main -- rather than relocating the ext space itself. (yabai_common.sh.)
    slabel=$(printf '%s' "$info" | jq -r '.label // ""' 2>/dev/null)
    if [ "$slabel" = "ext" ]; then
      take_lock
      yabai_dissolve_ext
      yabai -m space --focus "$YABAI_EXT_TARGET" >/dev/null 2>&1 || true
      exit 0
    fi

    # The other display, by role: which index that is can change while we wait for the
    # lock or retry (yabai_space_to_display re-resolves it).
    target=""
    role=""
    if [ -n "$sdisp" ] && [ -n "${MASTER_DISPLAY_INDEX:-}" ] && [ -n "${EXTERNAL_DISPLAY_INDEX:-}" ]; then
      if [ "$sdisp" = "$MASTER_DISPLAY_INDEX" ]; then
        target="$EXTERNAL_DISPLAY_INDEX"
        role=external
      else
        target="$MASTER_DISPLAY_INDEX"
        role=master
      fi
    fi

    # The unlabeled first space of a non-master display is that display's reserved
    # scratch (the slot macOS destroys on disconnect): pushing it home would put a label
    # there instead. "First" = nothing before it, or what is before it is on another
    # display; asked by index, which works while the bulk query is answering "[".
    if [ "$role" = master ] && [ -z "$slabel" ] &&
       [ "$(printf '%s' "$info" | jq -r '."is-native-fullscreen"' 2>/dev/null)" != true ]; then
      pdisp=""
      [ "$sidx" -gt 1 ] &&
        pdisp=$(yabai -m query --spaces --space "$((sidx - 1))" 2>/dev/null | jq -r '.display // empty' 2>/dev/null)
      [ "$pdisp" = "$sdisp" ] || exit 0
    fi

    take_lock
    # Move THAT space to the explicit other display, retrying that one target while
    # yabai refuses (animating, Mission Control). Never next/prev when the target is
    # known: those count ignored displays too, so a single refusal sent the space to
    # agent-chrome's invisible virtual screen. They remain for an unknown target only.
    if [ -n "$target" ]; then
      yabai_space_to_display "$sid" "$target" "$role" || exit 0
    else
      yabai -m space "$sidx" --display next >/dev/null 2>&1 ||
        yabai -m space "$sidx" --display prev >/dev/null 2>&1 || exit 0
    fi

    # Follow the moved space, resolving its NEW index by id, retrying until focus
    # actually lands on it (rides out yabai's cross-display focus race).
    [ -z "$sid" ] && exit 0
    # Right after a cross-display move yabai can refuse the focus or drop it, so
    # pause between tries.
    for _try in 1 2 3 4 5 6; do
      nidx=$(yabai_space_index_for_id "$sid")
      [ -n "$nidx" ] && yabai -m space --focus "$nidx" >/dev/null 2>&1
      cur=$(yabai -m query --spaces --space 2>/dev/null | jq -r '.id // empty' 2>/dev/null)
      [ "$cur" = "$sid" ] && break
      sleep 0.15
    done
    "$SCRIPT_DIR/yabai_reorder_spaces.sh" >/dev/null 2>&1 || true
    exit 0
    ;;
  home-all)
    # Resolve master from LIVE topology (UUID first, smallest-area fallback) so a
    # stale/wrong cached index can't aim pull-home at the wrong display; fall back
    # to the cache, then to 1 (and every move re-resolves it again). Mirrors yabai_displays.sh's removed branch.
    master=$(yabai_master_index)
    case "$master" in ''|*[!0-9]*) master="${MASTER_DISPLAY_INDEX:-}" ;; esac
    case "$master" in ''|*[!0-9]*) master=1 ;; esac
    take_lock
    yabai_pull_labels_home "$master"
    # Also pull any native-fullscreen Space stranded on the external -- those are
    # unlabeled (outside the labeled model), so the loop above misses them and a
    # fullscreened window would otherwise be left behind on the external.
    yabai_pull_fullscreen_home "$master"
    # Dissolve the on-demand `ext` scratch-work space into main (windows moved, label
    # dropped; the empty space stays on the external). main was pulled home above, so
    # the windows arrive on the laptop. No-op if ext doesn't exist.
    yabai_dissolve_ext
    # Re-resolved: the moves above can take long enough for the external to change sides.
    master=$(_yabai_role_display master "$master")
    yabai -m display --focus "$master" >/dev/null 2>&1 || true
    yabai -m rule --apply >/dev/null 2>&1 || true
    "$SCRIPT_DIR/yabai_reorder_spaces.sh" >/dev/null 2>&1 || true
    exit 0
    ;;
  *)
    exit 64
    ;;
esac
