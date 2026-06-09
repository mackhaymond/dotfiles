#!/bin/bash

# Send the focused window to a labeled space and FOLLOW focus to it -- you land on
# the target space alongside the window. UNLESS it is a pinned app already sitting
# on its home space (those are "bound to their current space" and left alone, with
# focus unchanged). Free / unpinned windows move and focus follows; a window that
# does not move (pinned-on-home, or already on the target) leaves focus put.
#
#   yabai_send_window.sh <target-label>

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

TARGET="${1:-}"
[ -z "$TARGET" ] && exit 64

info=$(yabai -m query --windows --window 2>/dev/null) || exit 0
app=$(printf '%s' "$info" | jq -r '.app // ""' 2>/dev/null)
cur=$(printf '%s' "$info" | jq -r '.space // empty' 2>/dev/null)
wid=$(printf '%s' "$info" | jq -r '.id // empty' 2>/dev/null)
[ -z "$cur" ] && exit 0

# Apps pinned to a home space (mirrors the `space=` rules in yabairc). If the
# focused window is one of these AND it is already on its home space, it is bound
# to its current space, so leave it. (Unpinned windows -- browsers, Finder, etc.
# -- have no home and always move.)
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
# window sitting on main or school from being force-moved off it -- the same home-
# space guard the other pinned apps get. Pure yabai (no AXIdentifier needed),
# which keeps it reliable. This also shields a Little Arc that happens to be on
# main/school (rare); a main-only guard would need Hammerspoon's arcFocusedKind.
if [ "$app" = "Arc" ]; then
  cur_label=$(yabai -m query --spaces --space "$cur" 2>/dev/null | jq -r '.label // ""' 2>/dev/null)
  case "$cur_label" in
    main|school) exit 0 ;;
  esac
fi

# Nothing to do if it is already on the target space.
tidx=$(yabai -m query --spaces --space "$TARGET" 2>/dev/null | jq -r '.index // empty' 2>/dev/null)
[ -z "$tidx" ] && exit 0
[ "$cur" = "$tidx" ] && exit 0

# Move the window, then FOLLOW focus to it so you land on the target space with
# the window. Focusing the moved window by id reliably brings its space forward
# (works across displays too); a bare space --focus is the fallback.
yabai -m window --space "$TARGET" >/dev/null 2>&1 || exit 0
if [ -n "$wid" ]; then
  yabai -m window "$wid" --focus >/dev/null 2>&1 || true
fi
# Verify focus actually landed on the target space; if the id-focus failed or raced
# (stale id after the move, or a cross-display focus race), fall back to a bare
# space --focus. Compared by LABEL so a reorder renumbering indices can't fool it.
# On a single display the id-focus always brings the space forward, so this is a
# no-op there (the fallback never fires).
cur_now=$(yabai -m query --spaces --space 2>/dev/null | jq -r '.label // empty' 2>/dev/null)
[ "$cur_now" = "$TARGET" ] || yabai -m space --focus "$TARGET" >/dev/null 2>&1 || true
