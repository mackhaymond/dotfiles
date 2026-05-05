#!/bin/bash

mail=(
    script="$PLUGIN_DIR/mail.sh"
    update_freq=2
    background.color=$BACKGROUND_1
    background.border_color=$BACKGROUND_2
    padding_left=0
    padding_right=3
    label.padding_right=10
    icon.padding_left=8
    label.font="$FONT:Regular:14.0"
) 

sketchybar --add item mail center \
    --set mail "${mail[@]}" \
    --subscribe mail mouse.clicked
