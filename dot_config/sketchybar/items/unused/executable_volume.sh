#!/bin/bash

volume_icon=(
  script="$PLUGIN_DIR/volume.sh"
  update_freq=0.1
  background.padding_left=3
  background.padding_right=0
  icon=$VOLUME_100
  icon.color=$WHITE
  icon.font="$FONT:Regular:17.0"
)


volume_slider=(
      script="$PLUGIN_DIR/volume_slider.sh"
      update_freq=0.1
  updates=on
  label.drawing=off
  icon.drawing=off
  slider.highlight_color=$BLUE
  slider.background.height=5
  slider.background.corner_radius=3
  slider.background.color=$BACKGROUND_2
background.padding_left=10
  slider.width=100
)

sketchybar --add item volume_icon right         \
           --set volume_icon "${volume_icon[@]}" \
           --subscribe volume_icon volume_change mouse.clicked

sketchybar --add slider volume_slider right \
    --set volume_slider "${volume_slider[@]}" \
    --subscribe volume_slider volume_change

sketchybar --add bracket volume_group volume_slider volume_icon \
           --set volume_group background.drawing=on \
           background.color=$BACKGROUND_1 \
           background.border_color=$BACKGROUND_2 \
           background.corner_radius=4 \
           background.height=20 \
        background.padding_left=10

status_bracket=(
  background.color=$BACKGROUND_1
  background.border_color=$BACKGROUND_2
)

sketchybar --add bracket status wifi volume_group \
           --set status "${status_bracket[@]}"
