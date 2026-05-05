#!/bin/bash

messages=(
script="$PLUGIN_DIR/messages.sh"
update_freq=1
background.color=$BACKGROUND_1
background.border_color=$BACKGROUND_2
padding_left=0
padding_right=3
icon="󰍡"
icon.padding_left=10
icon.padding_right=4
label.padding_right=10
)

sketchybar --add item messages center \
    --set messages "${messages[@]}"
