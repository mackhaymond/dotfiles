#!/bin/bash

# Toggle the focused window between floating and tiled/stacked. `--toggle float`
# is layout-agnostic, so this works in BOTH a stack space (pops the window out of
# / back into the stack) and a bsp space (out of / back into the tree) -- no
# per-layout branching needed.
#
# REFUSES TO FLOAT a pinned app: a `space=` app sitting on its home space, or an Arc
# main window on main/school, is left tiled (no toggle) so a curated pinned layout
# can't be knocked loose. This is the guard in yabai_send_window.sh, but DIRECTIONAL
# -- it applies only to the tiled -> floating direction. Un-floating a pinned window
# is always allowed, because that direction can only ever repair the layout, and
# refusing it left the one case with no manual way out: yabai can flag a window
# FLOAT on its own at startup (see yabai_startup_reconcile.sh), and a two-way guard
# meant hyper+t could not put it back. Observed 2026-08-21 with Claude on `ai`.
#
# Un-floating from here is also the safe direction for a yabai quirk that bites the
# reconcile script: yabai re-tiles an un-floated window onto the ACTIVE space rather
# than the window's own space. This script only ever acts on the FOCUSED window, and
# a focused window is by definition on the active space, so the two always agree.
#
# NOT guarded: manage=off apps (System Settings, Finder, etc.). yabai exposes no
# per-window "managed" flag -- a manage=off window reports is-floating=true exactly
# like a user-floated one -- so they can't be auto-detected without re-listing the
# yabairc rules here. By design (chosen over a drift-prone second copy of that list)
# the toggle acts on them too; the effect is reversible with another press.
#
#   yabai_toggle_float.sh

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

# shellcheck source=/dev/null  # required sibling: the agent app list + helper
. "$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)/yabai_common.sh"

info=$(yabai -m query --windows --window 2>/dev/null) || exit 0
app=$(printf '%s' "$info" | jq -r '.app // ""' 2>/dev/null)
cur=$(printf '%s' "$info" | jq -r '.space // empty' 2>/dev/null)
[ -z "$cur" ] && exit 0

# Which way is this press going? Only tiled -> floating is guarded below; a window
# that is ALREADY floating is always allowed back into the layout.
floating=$(printf '%s' "$info" | jq -r '."is-floating" // false' 2>/dev/null)

# Apps pinned to a home space (mirrors the `space=` rules in yabairc). If the
# focused window is one of these AND it is already on its home space, it is bound
# to that space -- leave it tiled. (Unpinned windows -- browsers, Finder, etc. --
# have no home and are always toggleable.)
home=""
case "$app" in
  wezterm-gui|WezTerm) home=terminal ;;
  Todoist)             home=todo ;;
  Granola)             home=schedule ;;
  "Spark Mail")        home=mail ;;
  "Notion Calendar")   home=calendar ;;
  Messages)            home=messages ;;
  ChatGPT|Claude|Muse) home=ai ;;
esac
yabai_is_agent_app "$app" && home="$YABAI_AGENT_LABEL"

if [ "$floating" != "true" ] && [ -n "$home" ]; then
  cur_label=$(yabai -m query --spaces --space "$cur" 2>/dev/null | jq -r '.label // ""' 2>/dev/null)
  [ "$cur_label" = "$home" ] && exit 0
fi

# Arc's two MAIN browser windows are pinned to main/school, so protect an Arc
# window on main or school -- the same home-space guard the other pinned apps get
# (pure yabai, no AXIdentifier; also shields a rare Little Arc on main/school).
if [ "$floating" != "true" ] && [ "$app" = "Arc" ]; then
  cur_label=$(yabai -m query --spaces --space "$cur" 2>/dev/null | jq -r '.label // ""' 2>/dev/null)
  case "$cur_label" in
    main|school) exit 0 ;;
  esac
fi

yabai -m window --toggle float >/dev/null 2>&1 || exit 0

# Record a DELIBERATE float of a pinned app so the automated un-float in
# yabai_startup_reconcile.sh (its `float` sweep) never fights it. Reaching here
# with a pinned app that was tiled means it is OFF its home space (the guard above
# exits otherwise); if a later `rule --apply` re-homes it while still floating, the
# sweep would otherwise "repair" the user's own float. The marker is by window id;
# cleared when the same window is un-floated here, and the whole dir is purged by
# the startup poll (a float that survives a yabai restart is never a user choice --
# yabai re-classifies from scratch). See yabai_common.sh for why the dir is fixed.
wid=$(printf '%s' "$info" | jq -r '.id // empty' 2>/dev/null)
if [ -n "$home" ] && [ -n "$wid" ]; then
  keep="$YABAI_STATE_DIR/keep-float"
  if [ "$floating" != "true" ]; then
    mkdir -p "$keep" 2>/dev/null && : >"$keep/$wid" 2>/dev/null
  else
    rm -f "$keep/$wid" 2>/dev/null
  fi
fi

# A float just flipped, but --toggle float emits no window_created/destroyed signal,
# so reconcile the floating-window borders here (no-op if borders isn't installed).
"$HOME/code/various_scripts/yabai_float_borders.sh" sync >/dev/null 2>&1 || true
