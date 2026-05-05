#!/bin/bash

layers=(
script="$PLUGIN_DIR/yabai_layers.sh"
update_freq=1
background.color=$BACKGROUND_1
background.border_color=$BACKGROUND_2
icon.drawing=off
label.padding_right=10
label.padding_left=10

)

sketchybar --add item layers left \
    --set layers "${layers[@]}" \
    --subscribe layers front_app_switched space_change
