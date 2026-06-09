#!/bin/bash

# Toggle the focused window between floating and tiled/stacked. `--toggle float`
# is layout-agnostic, so this works in BOTH a stack space (pops the window out of
# / back into the stack) and a bsp space (out of / back into the tree) -- no
# per-layout branching needed.
#
# REFUSES on pinned apps: a `space=` app sitting on its home space, or an Arc main
# window on main/school, is left alone (no toggle) so a curated pinned layout can't
# be knocked loose. This mirrors the guard in yabai_send_window.sh verbatim.
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

info=$(yabai -m query --windows --window 2>/dev/null) || exit 0
app=$(printf '%s' "$info" | jq -r '.app // ""' 2>/dev/null)
cur=$(printf '%s' "$info" | jq -r '.space // empty' 2>/dev/null)
[ -z "$cur" ] && exit 0

# Apps pinned to a home space (mirrors the `space=` rules in yabairc). If the
# focused window is one of these AND it is already on its home space, it is bound
# to that space -- leave it. (Unpinned windows -- browsers, Finder, etc. -- have no
# home and are always toggleable.)
home=""
case "$app" in
  wezterm-gui|WezTerm) home=terminal ;;
  Todoist)             home=todo ;;
  Granola)             home=schedule ;;
  "Spark Mail")        home=mail ;;
  "Notion Calendar")   home=calendar ;;
  Messages)            home=messages ;;
  ChatGPT|Claude)      home=ai ;;
  Codex)               home=codex ;;
esac

if [ -n "$home" ]; then
  cur_label=$(yabai -m query --spaces --space "$cur" 2>/dev/null | jq -r '.label // ""' 2>/dev/null)
  [ "$cur_label" = "$home" ] && exit 0
fi

# Arc's two MAIN browser windows are pinned to main/school, so protect an Arc
# window on main or school -- the same home-space guard the other pinned apps get
# (pure yabai, no AXIdentifier; also shields a rare Little Arc on main/school).
if [ "$app" = "Arc" ]; then
  cur_label=$(yabai -m query --spaces --space "$cur" 2>/dev/null | jq -r '.label // ""' 2>/dev/null)
  case "$cur_label" in
    main|school) exit 0 ;;
  esac
fi

yabai -m window --toggle float >/dev/null 2>&1 || exit 0

# A float just flipped, but --toggle float emits no window_created/destroyed signal,
# so reconcile the floating-window borders here (no-op if borders isn't installed).
"$HOME/code/various_scripts/yabai_float_borders.sh" sync >/dev/null 2>&1 || true
