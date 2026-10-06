#!/bin/bash

# Focus a labeled yabai space, wherever it lives (never moves it).
#
#   yabai_workspace.sh focus <label>   -> focus the label wherever it lives
#
# With <=1 display this is a plain focus, byte-equivalent to the original
# single-laptop behavior. (Cross-display pull-home lives in yabai_space_move.sh:
# `home-all` pulls every label back to the laptop, bound to hyper+0.)

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

# shellcheck disable=SC2034  # consumed by yabai_load_cache (sourced from yabai_common.sh)
CACHE_FILE="${YABAI_WORKSPACE_CACHE:-${HOME}/.cache/yabai/workspace_cache.env}"

MODE="${1:-}"
LABEL="${2:-}"

if [ -z "$MODE" ] || [ -z "$LABEL" ]; then
  exit 64
fi

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/yabai_common.sh"

# Cache miss -> heal labels + rewrite the cache WITHOUT any reset, then retry.
if ! yabai_load_cache; then
  "$SCRIPT_DIR/yabai_workspace_refresh.sh" >/dev/null 2>&1 || true
  yabai_load_cache || exit 0
fi

query_space_index() {
  local label="$1"

  yabai -m query --spaces --space "$label" 2>/dev/null |
    jq -r '.index // empty' |
    head -n 1
}

# Ids of the windows you'd actually SEE on space index $1 (empty output = the space
# looks empty). Hidden and minimized windows don't count: a hidden Notes window left on
# `agent` made hyper+esc jump to what looked like an empty space.
space_window_ids() {
  yabai -m query --windows --space "$1" 2>/dev/null |
    jq -r '.[] | select((."is-hidden" | not) and (."is-minimized" | not)) | .id' 2>/dev/null
}

focus_space() {
  local space_index

  space_index=$(query_space_index "$LABEL")
  [ -z "$space_index" ] && return 0

  # `agent` (hyper+esc) is CONDITIONAL: it is a generic coding-agent view rather
  # than a fixed app's home, so with nothing on it -- no Conductor et al. running,
  # nothing flung there with hyper+fn+esc -- the key is a no-op instead of a trip
  # to an empty space. Every other label focuses unconditionally, as before.
  if [ "$LABEL" = "${YABAI_AGENT_LABEL:-agent}" ] && [ -z "$(space_window_ids "$space_index")" ]; then
    return 0
  fi

  yabai -m space --focus "$space_index" >/dev/null 2>&1 || true
}

# Prime single-laptop fast path: with one display, every mode is a plain focus.
if [ "${DISPLAY_COUNT:-1}" -le 1 ]; then
  focus_space
  exit 0
fi

case "$MODE" in
  focus)
    # Pure focus: never relocate a space. Only reached with 2+ displays.
    focus_space
    exit 0
    ;;
  *)
    exit 64
    ;;
esac
