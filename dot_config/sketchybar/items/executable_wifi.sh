#!/bin/bash

source "$CONFIG_DIR/icons.sh"

wifi=(
  padding_right=7
  label.width=0
  label.font="$FONT:Regular:13.0"
  label.padding_right=7
  icon="$WIFI_DISCONNECTED"
  icon.padding_left=7
  icon.padding_right=7
  script="$PLUGIN_DIR/wifi.sh"

background.color=$BACKGROUND_1
background.border_color=$BACKGROUND_2
background.height=28
background.padding_right=0
)

sketchybar --add item wifi right \
           --set wifi "${wifi[@]}" \
           --subscribe wifi wifi_change mouse.clicked
