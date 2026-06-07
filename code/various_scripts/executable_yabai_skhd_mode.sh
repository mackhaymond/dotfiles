#!/bin/bash

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

SPACE_JSON=$(yabai -m query --spaces --space 2>/dev/null) || exit 0
SPACE_INDEX=$(jq -r '.index // empty' <<<"$SPACE_JSON" 2>/dev/null) || exit 0
MODE=$(jq -r '.type // ""' <<<"$SPACE_JSON" 2>/dev/null) || exit 0

if [ -z "$SPACE_INDEX" ]; then
  exit 0
fi

if [ "$MODE" = "bsp" ]; then
  yabai -m config --space "$SPACE_INDEX" layout stack >/dev/null 2>&1 || true
else
  yabai -m config --space "$SPACE_INDEX" layout bsp >/dev/null 2>&1 || true
fi

# No yabai signal fires for a layout change (config --space ... layout pushes none), so
# poke the SwiftBar layers indicator directly -- this flips it BSP <-> N/M and there is
# no other event to catch it. -g never steals focus; single-quote so `?` isn't globbed.
/usr/bin/open -g 'swiftbar://refreshplugin?name=yabai_layers' >/dev/null 2>&1 || true
