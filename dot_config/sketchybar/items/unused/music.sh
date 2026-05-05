#!/bin/bash

sketchybar -m --add event song_update com.apple.iTunes.playerInfo

music=(
    script="$PLUGIN_DIR/music.sh"
    label.padding_right=10
    icon.padding_left=10
    background.color=$BACKGROUND_1
    background.border_color=$BACKGROUND_2
)

sketchybar --add item music center \
    --set music "${music[@]}" \
    --subscribe music song_update
