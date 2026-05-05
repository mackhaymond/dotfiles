#!/bin/bash

MODE=$(yabai -m query --spaces --space | jq ".type")

if [ "$MODE" == "\"bsp\"" ]; then
    sketchybar --set layers label="BSP"
    exit 0
fi


TOTAL_LAYERS=$(yabai -m query --windows --space | jq length)

if [[ $(jq 'map(select(.app == "Messages")) | length > 0' <<< $(yabai -m query --windows --space)) == true ]]; then
    TOTAL_LAYERS=$((TOTAL_LAYERS-1))
fi

if [ "$TOTAL_LAYERS" -eq 1 ]; then
  CURRENT_LAYER=1
else
    CURRENT_LAYER=$(yabai -m query --windows --window | jq '.["stack-index"]')
fi


LABEL="$CURRENT_LAYER / $TOTAL_LAYERS"

sketchybar --set layers label="$LABEL"
