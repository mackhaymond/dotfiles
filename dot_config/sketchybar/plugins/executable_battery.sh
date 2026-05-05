#!/bin/bash

source "$CONFIG_DIR/colors.sh" # Loads all defined colors

BATT_PERCENT=$(pmset -g batt | grep -Eo "\d+%" | cut -d% -f1)
CHARGING=$(pmset -g batt | grep 'AC Power')

case ${BATT_PERCENT} in
    100) ICON="" COLOR=${GREEN};;
    9[0-9]) ICON="" COLOR=${GREEN};;
    8[0-9]) ICON="" COLOR=${GREEN};;
    7[0-9]) ICON="" COLOR=${GREEN};;
    6[0-9]) ICON="" COLOR=${YELLOW};;
    5[0-9]) ICON="" COLOR=${YELLOW};;
    4[0-9]) ICON="" COLOR=${ORANGE};;
    3[0-9]) ICON="" COLOR=${ORANGE};;
    2[0-9]) ICON="" COLOR=${RED};;
    1[0-9]) ICON="" COLOR=${RED};;
    *) ICON="" COLOR=${RED};;
esac

if [[ $CHARGING != "" ]]; then
    ICON=""
fi

sketchybar --set battery \
  icon.color=$COLOR \
  icon=$ICON \
  label=$(printf "${BATT_PERCENT}%%")
