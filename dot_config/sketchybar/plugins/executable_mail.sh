#!/usr/bin/env bash

STATUS_LABEL=$(lsappinfo info -only StatusLabel "Spark Mail")
ICON=""

source "$CONFIG_DIR/colors.sh"

mouse_clicked() {
  if [ "$BUTTON" = "left" ]; then
      yabai -m space --focus 6 2>/dev/null
  fi
}

update() {
if [[ $STATUS_LABEL =~ \"label\"=\"([^\"]*)\" ]]; then
    LABEL="${BASH_REMATCH[1]}"

    if [[ $LABEL == "" ]]; then
        LABEL="0"
        ICON_COLOR=$GREEN
        LABEL_PADDING=10
    elif [[ $LABEL == "•" ]]; then
        ICON_COLOR=$RED
        LABEL_PADDING=10
    elif [[ $LABEL =~ ^[0-9]+$ ]]; then
        ICON_COLOR=$RED
        LABEL_PADDING=10
    else
        exit 0
    fi
else
  exit 0
fi

sketchybar --set $NAME icon=$ICON \
    label="${LABEL}" \
    icon.color=${ICON_COLOR} \
    label.padding_right=${LABEL_PADDING}
}

case "$SENDER" in
  "mouse.clicked") mouse_clicked
  ;;
  *) update
  ;;
esac
