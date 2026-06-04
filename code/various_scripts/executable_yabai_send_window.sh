#!/bin/bash

# Send the focused window to a labeled space -- UNLESS it is a pinned app that is
# already sitting on its home space (those are "bound to their current space" and
# left alone). Free / unpinned windows move. Focus stays where you are.
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
  ChatGPT)             home=chatgpt ;;
  Codex)               home=codex ;;
esac

if [ -n "$home" ]; then
  cur_label=$(yabai -m query --spaces --space "$cur" 2>/dev/null | jq -r '.label // ""' 2>/dev/null)
  [ "$cur_label" = "$home" ] && exit 0
fi

# Nothing to do if it is already on the target space.
tidx=$(yabai -m query --spaces --space "$TARGET" 2>/dev/null | jq -r '.index // empty' 2>/dev/null)
[ -z "$tidx" ] && exit 0
[ "$cur" = "$tidx" ] && exit 0

yabai -m window --space "$TARGET" >/dev/null 2>&1 || true
