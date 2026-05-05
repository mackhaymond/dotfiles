#!/bin/bash

BLUETOOTH_EVENT="com.apple.bluetooth.status"
sketchybar --add event bluetooth_update $BLUETOOTH_EVENT

device=(
  padding_right=7
  label.width=0
  label.font="$FONT:Regular:13.0"
  label.padding_right=7
  icon.padding_left=7
  icon.padding_right=7
  script="$PLUGIN_DIR/audio_device.sh"

background.color=$BACKGROUND_1
background.border_color=$BACKGROUND_2
background.height=28
background.padding_right=0
)


sketchybar --add item audio_device right \
    --set audio_device "${device[@]}" \
    --subscribe audio_device mouse.clicked bluetooth_update
