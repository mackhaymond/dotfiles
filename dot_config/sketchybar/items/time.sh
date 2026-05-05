#!/bin/bash

time=(
    script="$PLUGIN_DIR/time.sh"
    update_freq=1
    icon.drawing=off
    label.padding_right=10
    label.padding_left=10
    label.font="$FONT:Regular:14.0"
    background.color=$BACKGROUND_1
    background.border_color=$BACKGROUND_2
    background.height=28
    background.padding_left=0
    background.padding_right=0
)

sketchybar --add item time right \
                 --set time "${time[@]}"
