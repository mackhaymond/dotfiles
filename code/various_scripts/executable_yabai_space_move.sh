#!/bin/bash

# Explicit cross-display space moves (the only keys that relocate a space).
#
#   yabai_space_move.sh push       -> send the FOCUSED space to the other display, follow
#   yabai_space_move.sh home-all   -> pull every canonical label home to the laptop
#
# Both are no-ops with a single display. Moves are native whole-space operations
# (yabai -m space ... --display ...), so all of a space's windows travel with it.

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

if ! yabai_load_cache; then
  "$SCRIPT_DIR/yabai_workspace_refresh.sh" >/dev/null 2>&1 || true
  yabai_load_cache || exit 0
fi

# Single display: nothing to move anywhere.
if [ "${DISPLAY_COUNT:-1}" -le 1 ]; then
  exit 0
fi

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
    # home" DISSOLVES it into main (windows moved there, ext destroyed) and lands
    # you on main -- rather than relocating the ext space itself. (yabai_common.sh.)
    slabel=$(printf '%s' "$info" | jq -r '.label // ""' 2>/dev/null)
    if [ "$slabel" = "ext" ]; then
      yabai_dissolve_ext
      yabai -m space --focus "$YABAI_EXT_TARGET" >/dev/null 2>&1 || true
      exit 0
    fi

    # Move THAT space to the explicit other display (deterministic), falling back
    # to relative next/prev if the cache indices are unavailable.
    target=""
    if [ -n "$sdisp" ] && [ -n "${MASTER_DISPLAY_INDEX:-}" ] && [ -n "${EXTERNAL_DISPLAY_INDEX:-}" ]; then
      if [ "$sdisp" = "$MASTER_DISPLAY_INDEX" ]; then
        target="$EXTERNAL_DISPLAY_INDEX"
      else
        target="$MASTER_DISPLAY_INDEX"
      fi
    fi
    if [ -n "$target" ]; then
      yabai -m space "$sidx" --display "$target" >/dev/null 2>&1 ||
        yabai -m space "$sidx" --display next >/dev/null 2>&1 ||
        yabai -m space "$sidx" --display prev >/dev/null 2>&1 || exit 0
    else
      yabai -m space "$sidx" --display next >/dev/null 2>&1 ||
        yabai -m space "$sidx" --display prev >/dev/null 2>&1 || exit 0
    fi

    # Follow the moved space, resolving its NEW index by id, retrying until focus
    # actually lands on it (rides out yabai's cross-display focus race).
    [ -z "$sid" ] && exit 0
    for _try in 1 2 3; do
      nidx=$(yabai -m query --spaces 2>/dev/null |
        jq -r --argjson sid "$sid" '.[] | select(.id == $sid) | .index' 2>/dev/null | head -n 1)
      [ -n "$nidx" ] && yabai -m space --focus "$nidx" >/dev/null 2>&1
      cur=$(yabai -m query --spaces --space 2>/dev/null | jq -r '.id // empty' 2>/dev/null)
      [ "$cur" = "$sid" ] && break
    done
    "$SCRIPT_DIR/yabai_reorder_spaces.sh" >/dev/null 2>&1 || true
    exit 0
    ;;
  home-all)
    # Resolve master from LIVE topology (UUID first, smallest-area fallback) so a
    # stale/wrong cached index can't aim pull-home at the wrong display; fall back
    # to the cache, then to 1. Mirrors yabai_displays.sh's removed branch.
    master=$(yabai_master_index)
    case "$master" in ''|*[!0-9]*) master="${MASTER_DISPLAY_INDEX:-}" ;; esac
    case "$master" in ''|*[!0-9]*) master=1 ;; esac
    for label in $YABAI_LABELS; do
      disp=$(yabai -m query --spaces --space "$label" 2>/dev/null | jq -r '.display // empty' 2>/dev/null)
      [ -n "$disp" ] && [ "$disp" != "$master" ] &&
        yabai -m space "$label" --display "$master" >/dev/null 2>&1 || true
    done
    # Also pull any native-fullscreen Space stranded on the external -- those are
    # unlabeled (outside the labeled model), so the loop above misses them and a
    # fullscreened window would otherwise be left behind on the external.
    yabai_pull_fullscreen_home "$master"
    # Dissolve the on-demand `ext` scratch-work space into main (windows moved,
    # ext destroyed). main was pulled home above, so the windows arrive on the
    # laptop. No-op if ext doesn't exist.
    yabai_dissolve_ext
    yabai -m display --focus "$master" >/dev/null 2>&1 || true
    yabai -m rule --apply >/dev/null 2>&1 || true
    "$SCRIPT_DIR/yabai_reorder_spaces.sh" >/dev/null 2>&1 || true
    exit 0
    ;;
  *)
    exit 64
    ;;
esac
