#!/bin/bash

MODE=$(yabai -m query --spaces --space | jq ".type")

if [ "$MODE" == "\"bsp\"" ]; then
    yabai -m config --space mouse layout stack
else
    yabai -m config --space mouse layout bsp
fi

