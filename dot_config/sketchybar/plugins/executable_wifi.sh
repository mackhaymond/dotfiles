#!/bin/bash

update() {
  source "$CONFIG_DIR/icons.sh"
  INFO="$(networksetup -getairportnetwork en0 | awk -F': ' '{print $2}')"
  LABEL="$INFO ($(ipconfig getifaddr en0))"
  ICON="$([ -n "$INFO" ] && echo "$WIFI_CONNECTED" || echo "$WIFI_DISCONNECTED")"

  echo $LABEL >> ~/test.txt

  sketchybar --set $NAME icon="$ICON" label="$LABEL"
}

click() {
  CURRENT_WIDTH="$(sketchybar --query $NAME | jq -r .label.width)"

  WIDTH=0
  if [ "$CURRENT_WIDTH" -eq "0" ]; then
    WIDTH=dynamic
  fi
  
  AUDIO_WIDTH=$(sketchybar --query audio_device | jq '.label.width')

  if [ "$AUDIO_WIDTH" -ne "0" ]; then
    sketchybar --animate sin 20 --set audio_device label.width=0
  fi


  sketchybar --animate sin 20 --set $NAME label.width="$WIDTH"
}

case "$SENDER" in
  "wifi_change") update
  ;;
  "mouse.clicked") click
  ;;
*) update
    ;;
esac
