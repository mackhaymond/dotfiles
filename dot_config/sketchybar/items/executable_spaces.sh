#!/bin/bash

SPACE_ICONS=("" "" "󰑴" "" "󰃰" "" "󰸗" "󰍡" "󱜸")

# Destroy space on right click, focus space on left click.
# New space by left clicking separator (>)

sid=0
spaces=()
for i in "${!SPACE_ICONS[@]}"
do
  sid=$(($i+1))

  space=(
    space=$sid
    icon="${SPACE_ICONS[i]}"
    icon.padding_left=10
    icon.padding_right=7
    padding_left=2
    padding_right=2
    label.padding_right=0
    icon.highlight_color=$ORANGE
    icon.color=$WHITE
    script="$PLUGIN_DIR/space.sh"
  )

  sketchybar --add space space.$sid left    \
             --set space.$sid "${space[@]}" \
             --subscribe space.$sid mouse.clicked
done

sketchybar --add bracket spaces space.1 space.2 space.3 space.4 space.5 space.6 space.7 space.8 space.9 \
    --set spaces background.color=$BACKGROUND_1 \
    background.border_color=$BACKGROUND_2 \
    background.height=28
