#!/bin/bash

battery=(
    update_freq=1
    icon.font="MesloLGS NF:Regular:14.0"
    icon.padding_right=3
    icon.padding_left=10
    label.padding_right=8
    label.font="$FONT:Regular:14.0"
    script="$PLUGIN_DIR/battery.sh"

    background.color=$BACKGROUND_1
background.border_color=$BACKGROUND_2
background.height=28
)

sketchybar --add item battery right \
                --set battery "${battery[@]}"
