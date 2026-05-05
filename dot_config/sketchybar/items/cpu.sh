#!/bin/bash

source "$CONFIG_DIR/colors.sh" # Loads all defined colors

cpu=(
script="$PLUGIN_DIR/cpu.sh"
update_freq=3
background.color=$BACKGROUND_1
background.border_color=$BACKGROUND_2
background.padding_right=0
background.padding_left=-13
background.width=100
icon=""
icon.color=$BLUE
icon.font="$FONT:Regular:14.0"
icon.padding_left=8
icon.padding_right=2
label.font="$FONT:Regular:14.0"
label.padding_right=8
)

sketchybar --add item cpu right \
    --set cpu "${cpu[@]}"
