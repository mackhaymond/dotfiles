#!/bin/bash

# Draw a JankyBorders border around FLOATING windows only.
#
# yabai has no native per-window border, and JankyBorders (`borders`) filters by
# APP NAME, not by float state -- it has no idea what yabai considers floating. So
# this computes the set of apps that CURRENTLY have a floating window and drives the
# borders daemon's `whitelist` to exactly that set: it starts the daemon (with the
# style below) when the first floating window appears, live-updates the whitelist as
# the set changes, and kills the daemon when the last floating window goes away (so
# borders runs only while something floats -- not a brew service / always-on).
#
# SCOPE = ALL is-floating windows. yabai reports `is-floating: true` for a window you
# floated (hyper+t) AND for a manage=off app (Finder, System Settings, ...) -- there
# is no per-window "managed" flag to tell them apart -- so manage=off apps are
# bordered whenever open. (Chosen over a drift-prone second copy of the yabairc
# manage=off list.)
#
# CAVEAT (inherent to JankyBorders' app-granularity, NOT a bug): the whitelist is by
# app, so if one app has BOTH a floating and a tiled window (e.g. a floating Little
# Arc popup while the main Arc window is tiled), BOTH windows get the border.
#
#   yabai_float_borders.sh sync   # reconcile borders to the current floating set
#
# Wired to: yabairc window_created / window_destroyed signals + a startup sync, and
# the hyper+t toggle (yabai_toggle_float.sh) -- the events where the float set changes
# (a plain --toggle float emits no window_created/destroyed, so the toggle script
# calls sync itself).

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export LANG="${LANG:-en_US.UTF-8}"   # borders needs a UTF-8 locale (brew caveat)

BORDERS=/opt/homebrew/bin/borders
[ -x "$BORDERS" ] || exit 0          # degrade to a no-op if borders isn't installed

# Border style. active = focused window (white), inactive = unfocused (subtle gray).
# Round, 2.0pt -- the subtle-white look. Style is set once at daemon start; later
# syncs only push the whitelist (the daemon keeps the style).
BORDERS_STYLE=(style=round width=2.0 active_color=0xffffffff inactive_color=0xff5c6370 hidpi=on)

cmd="${1:-sync}"
[ "$cmd" = "sync" ] || exit 64

# Distinct app names that currently have a floating window, comma-joined (empty if
# none). manage=off apps are intentionally included (see header).
apps=$(yabai -m query --windows 2>/dev/null \
  | jq -r '[.[] | select(.["is-floating"] == true) | .app] | unique | join(",")' 2>/dev/null)

if [ -z "$apps" ]; then
  # Nothing floats -> stop the daemon entirely (no borders anywhere).
  pkill -x borders 2>/dev/null
  exit 0
fi

if pgrep -x borders >/dev/null 2>&1; then
  # Daemon already running (ours -- we own its whole lifecycle) -> live-update just
  # the whitelist; the running daemon keeps the style.
  "$BORDERS" "whitelist=$apps" >/dev/null 2>&1 || true
else
  # First floater -> start the daemon with style + whitelist. nohup + disown so it
  # survives this short-lived signal/keybind shell.
  nohup "$BORDERS" "${BORDERS_STYLE[@]}" "whitelist=$apps" >/dev/null 2>&1 &
  disown 2>/dev/null || true
fi
