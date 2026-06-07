#!/bin/bash
# yabai_borders.sh -- gate JankyBorders (the `borders` daemon) to the BSP layout,
# FOLLOWING THE FOCUSED SPACE. Borders are visible ONLY while the focused space is
# in `bsp` layout; in `stack` (the global default) they are hidden. There is no
# global "bsp mode" in this config -- bsp is toggled per-space via yabai_skhd_mode.sh
# -- so a single source of truth (the focused space's `.type`) drives borders.
#
# Callers:
#   1. yabai_skhd_mode.sh   -- after the hyper+fn+b layout toggle (bsp <-> stack).
#   2. yabairc space_changed -- when focus moves between spaces (so borders follow
#      whichever space you land on, including across displays).
#   3. yabairc startup       -- one-shot `sync` so state is correct from t=0.
#
# Lifecycle is idempotent and flicker-free: starting an already-running borders is a
# no-op (so a bsp -> bsp space switch does NOT restart it / flash the borders), and
# stopping an already-stopped one is a cheap no-op. Borders is NOT a brew service
# (that would run it always-on at login, defeating the bsp gating); this script owns
# its lifecycle. If `borders` is not installed, every path degrades to a no-op.
#
# Usage: yabai_borders.sh [sync|on|off]   (default: sync)

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export LANG="${LANG:-en_US.UTF-8}"   # borders requires a UTF-8 locale (see brew caveat)

# --- style tunables -------------------------------------------------------------
# active = white, inactive = muted slate gray, ~2px, rounded corners ("subtle").
# Colors are 0xAARRGGBB. Tweak freely -- a running borders is restarted on the next
# bsp re-entry, so style edits take effect after one stack<->bsp round-trip.
# NOTE: a border reaches `width` px OUTWARD from each window edge (inner edge flush
# with the frame), so two adjacent bsp tiles' borders just touch at a gap of 2*width.
# If you change `width` here, also set yabairc's `window_gap = 2 * width` to keep the
# borders just-not-overlapping.
BORDERS_ARGS=(
  active_color=0xffffffff
  inactive_color=0xff5c6370
  width=2.0
  style=round
)

borders_on() {
  command -v borders >/dev/null 2>&1 || return 0   # not installed -> no-op
  pgrep -x borders >/dev/null 2>&1 && return 0      # already running -> no flicker
  nohup borders "${BORDERS_ARGS[@]}" >/dev/null 2>&1 &
  disown 2>/dev/null || true                        # survive this script's exit
}

borders_off() {
  pkill -x borders >/dev/null 2>&1 || true          # no-op if not running
}

case "${1:-sync}" in
  on)  borders_on ;;
  off) borders_off ;;
  sync|*)
    # Desired state = the FOCUSED space's layout. `--space` with no index targets the
    # currently focused space; .type is "bsp", "stack", or "float".
    layout=$(yabai -m query --spaces --space 2>/dev/null | jq -r '.type // ""' 2>/dev/null)
    if [ "$layout" = "bsp" ]; then borders_on; else borders_off; fi
    ;;
esac
exit 0
