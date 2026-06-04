#!/bin/bash

# Pin the (up to two) MAIN Arc browser windows to the `main` and `school` spaces,
# one each -- title-independent and stable. It does NOT matter which window goes
# where; only that each space ends up with one.
#
# A "main" Arc window is: app Arc + subrole AXStandardWindow + NOT floating + NOT
# native-fullscreen. Little Arc popups are indistinguishable from main windows in
# every yabai field EXCEPT AXIdentifier (which yabai can't read), so Hammerspoon
# floats them first; that `is-floating` flag is what lets us exclude them here.
#
# Stable: a window already sitting on main or school is never disturbed; only a
# window that has drifted off both (or a surplus second one on a target) is moved
# to fill an empty target. Fullscreen windows are left entirely alone.

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

WINS=$(yabai -m query --windows 2>/dev/null) || exit 0

m_idx=$(yabai -m query --spaces --space main   2>/dev/null | jq -r '.index // empty')
s_idx=$(yabai -m query --spaces --space school 2>/dev/null | jq -r '.index // empty')
[ -z "$m_idx" ] && exit 0
[ -z "$s_idx" ] && exit 0

# "<id> <space-index>" for each main Arc window.
real=$(printf '%s' "$WINS" | jq -r '
  .[]
  | select(.app == "Arc"
      and .subrole == "AXStandardWindow"
      and .["is-floating"] == false
      and .["is-native-fullscreen"] == false)
  | "\(.id) \(.space)"')

on_main=() on_school=() pool=()
while read -r id sp; do
  [ -z "$id" ] && continue
  if   [ "$sp" = "$m_idx" ]; then on_main+=("$id")
  elif [ "$sp" = "$s_idx" ]; then on_school+=("$id")
  else pool+=("$id")
  fi
done <<EOF
$real
EOF

# A second+ window already on a target is surplus -> movable.
for extra in "${on_main[@]:1}";   do pool+=("$extra"); done
for extra in "${on_school[@]:1}"; do pool+=("$extra"); done

# Fill an empty target from the movable pool (never disturb a filled one).
if [ "${#on_main[@]}" -eq 0 ] && [ "${#pool[@]}" -gt 0 ]; then
  yabai -m window "${pool[0]}" --space main >/dev/null 2>&1 || true
  pool=("${pool[@]:1}")
fi
if [ "${#on_school[@]}" -eq 0 ] && [ "${#pool[@]}" -gt 0 ]; then
  yabai -m window "${pool[0]}" --space school >/dev/null 2>&1 || true
fi
