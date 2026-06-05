#!/bin/bash

# Flash a border around the focused display when the ACTIVE DISPLAY changes TO a
# non-master (external) display. Wired to a second yabai display_changed signal,
# alongside the mouse-follow one.
#
# Fires ONLY for the external display, never the laptop:
#   * display_changed cannot fire with a single display -> no-op solo.
#   * if the now-focused display is the master (laptop) index, exit without a flash.
#
# Tunables via env (defaults in []):
#   YABAI_FLASH_BORDER [8]  width px      YABAI_FLASH_RADIUS [13] corner px
#   YABAI_FLASH_R/G/B [1.0/0.6/0.0]       sRGB 0..1 (default = orange)
#   YABAI_FLASH_HOLD [0.18] s solid       YABAI_FLASH_FADE [0.22] s fade-out
# To DISABLE: remove the flash_external_display signal from yabairc (or this file).

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

CACHE_FILE="${YABAI_WORKSPACE_CACHE:-${HOME}/.cache/yabai/workspace_cache.env}"
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/yabai_common.sh"
JS="$SCRIPT_DIR/yabai_screen_flash.js"

FLASH_BORDER="${YABAI_FLASH_BORDER:-8}"
FLASH_R="${YABAI_FLASH_R:-1.0}"
FLASH_G="${YABAI_FLASH_G:-0.6}"
FLASH_B="${YABAI_FLASH_B:-0.0}"
FLASH_HOLD="${YABAI_FLASH_HOLD:-0.18}"
FLASH_FADE="${YABAI_FLASH_FADE:-0.22}"
FLASH_RADIUS="${YABAI_FLASH_RADIUS:-13}"

[ -r "$JS" ] || exit 0

fdisp=$(yabai -m query --displays --display 2>/dev/null) || exit 0
fidx=$(printf '%s' "$fdisp" | jq -r '.index // empty' 2>/dev/null)
fid=$(printf '%s' "$fdisp" | jq -r '.id // empty' 2>/dev/null)
fw=$(printf '%s' "$fdisp" | jq -r '.frame.w // empty' 2>/dev/null)
fh=$(printf '%s' "$fdisp" | jq -r '.frame.h // empty' 2>/dev/null)
{ [ -z "$fidx" ] || [ -z "$fid" ]; } && exit 0

# Resolve the master (laptop) display index: cache first, then live UUID.
# shellcheck disable=SC1090
master=$( . "$CACHE_FILE" 2>/dev/null; printf '%s' "${MASTER_DISPLAY_INDEX:-}" )
case "$master" in
  ''|*[!0-9]*) master=$(yabai_master_index) ;;
esac

# Flash only on a NON-master display; if master is unknown, stay conservative.
[ -z "$master" ] && exit 0
[ "$fidx" = "$master" ] && exit 0

# Single-instance guard: each flash blocks its runloop for ~hold+fade (~0.4s). A
# burst of display_changed events would otherwise stack overlapping overlays. flock
# is absent on this machine, so skip if a flash JXA is already in flight (the .js
# distinguishes it from this .sh, so pgrep can't match self).
pgrep -f "yabai_screen_flash.js" >/dev/null 2>&1 && exit 0

osascript -l JavaScript "$JS" \
  "$fid" "$FLASH_BORDER" "$FLASH_R" "$FLASH_G" "$FLASH_B" "$FLASH_HOLD" "$FLASH_FADE" "$FLASH_RADIUS" "$fw" "$fh" \
  >/dev/null 2>&1 || true

exit 0
