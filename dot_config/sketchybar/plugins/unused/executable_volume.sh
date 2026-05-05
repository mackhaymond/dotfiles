#!/bin/bash

volume_change() {
    VOLUME=$(osascript -e 'get volume settings' | awk -F'[ :,]' '{print $3}')
  source "$CONFIG_DIR/icons.sh"
  case $VOLUME in
    [6-9][0-9]|100) ICON=$VOLUME_100
    ;;
    [3-5][0-9]) ICON=$VOLUME_66
    ;;
    [1-2][0-9]) ICON=$VOLUME_33
    ;;
    [1-9]) ICON=$VOLUME_10
    ;;
    0) ICON=$VOLUME_0
    ;;
    *) ICON=$VOLUME_100
  esac

  sketchybar --set volume_icon icon=$ICON
}

mouse_clicked() {
    VOLUME=$(osascript -e 'get volume settings' | awk -F'[ :,]' '{print $3}')
    case $VOLUME in
        0)
            osascript -e "set volume output volume $LAST_VOLUME"
        ;;
        *)
        export LAST_VOLUME=$VOLUME
        osascript -e "set volume output volume 0"
        ;;
    esac
}

case "$SENDER" in
  "volume_change") volume_change
  ;;
  "mouse.clicked") mouse_clicked
  ;;
esac
