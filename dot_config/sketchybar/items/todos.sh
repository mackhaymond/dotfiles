#!/bin/bash

todos=(
    script="$PLUGIN_DIR/todos.sh"
    update_freq=10
    background.color=$BACKGROUND_1
    background.border_color=$BACKGROUND_2
    padding_left=0
    padding_right=3
    icon=""
    label.font="$FONT:Regular:14.0"
    icon.padding_left=10
    label.padding_right=10
)

sketchybar --add item todos center \
                 --set todos "${todos[@]}" \
                 --subscribe todos mouse.clicked
