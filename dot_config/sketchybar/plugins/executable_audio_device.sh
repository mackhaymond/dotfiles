#!/bin/bash


update() {
DEVICE=$(SwitchAudioSource -c)

# if Airpods Pro is found in the string, set icon to that
# if Speakers is found, set to that

if [[ $DEVICE == *"AirPods Pro"* ]]; then
    ICON="󰋋"
elif [[ $DEVICE == *"Speakers"* ]]; then
    ICON="󰽟"
else
    ICON="󰓃"
fi

sketchybar --set audio_device icon="$ICON" label="$DEVICE"
}

click() {
  CURRENT_WIDTH="$(sketchybar --query $NAME | jq -r .label.width)"

  WIDTH=0
  if [ "$CURRENT_WIDTH" -eq "0" ]; then
    WIDTH=dynamic
  fi

  WIFI_WIDTH=$(sketchybar --query wifi | jq '.label.width')

  if [ "$WIFI_WIDTH" -ne "0" ]; then
    sketchybar --animate sin 20 --set wifi label.width=0
  fi

  sketchybar --animate sin 20 --set $NAME label.width="$WIDTH"
}

case "$SENDER" in
  "mouse.clicked") click
  ;;
*) update
    ;;
esac
