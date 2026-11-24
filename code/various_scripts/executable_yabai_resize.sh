#!/bin/bash

# Resize the focused window by 60px (hyper + [ ] ; ').
#
#   yabai_resize.sh narrower|wider|shorter|taller
#
# TILED (bsp): yabai moves the split on the side the handle names, and FAILS when there
# is none (right/bottom on the rightmost/bottom window, left/top on the leftmost/top
# one). So try the near handle (left/top), then the far one (right/bottom). Keep that
# order: yabai's right/bottom check compares rounded edges, so being 1px off can make it
# pick a split on the window's other side and resize it the wrong way. The left/top
# check compares exact origins.
#
# FLOATING (or the query failed): only the far handle, so the window keeps its top-left
# corner. A floating window accepts any handle, so the near one would always win there.

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

case "${1:-}" in
  narrower) near=left:60:0  far=right:-60:0 ;;
  wider)    near=left:-60:0 far=right:60:0 ;;
  shorter)  near=top:0:60   far=bottom:0:-60 ;;
  taller)   near=top:0:-60  far=bottom:0:60 ;;
  *) echo "usage: ${0##*/} narrower|wider|shorter|taller" >&2; exit 2 ;;
esac

# Exactly "false" = tiled; anything else (true, or empty after a failed query) = far only.
floating=$(yabai -m query --windows --window 2>/dev/null | jq -r '."is-floating"' 2>/dev/null)

if [ "$floating" = "false" ]; then
  yabai -m window --resize "$near" >/dev/null 2>&1 && exit 0
fi
yabai -m window --resize "$far" >/dev/null 2>&1
exit 0
