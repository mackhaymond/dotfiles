#!/bin/bash

VOLUME=$(osascript -e 'get volume settings' | awk -F'[ :,]' '{print $3}')

sketchybar --set volume_slider slider.percentage=$VOLUME
