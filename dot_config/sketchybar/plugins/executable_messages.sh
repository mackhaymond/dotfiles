#!/usr/bin/env bash

TEXT=$(sqlite3 ~/Library/Messages/chat.db "SELECT text FROM message WHERE is_read=0 AND is_from_me=0 AND text!='' AND date_read=0" | wc -l | awk '{$1=$1};1')

source "$CONFIG_DIR/colors.sh"

if [ $TEXT = 0 ]; then
  sketchybar -m --set $NAME label="$TEXT" icon.color=$GREEN icon="󰍡"
else
  sketchybar -m --set $NAME label="$TEXT" \
                            icon.color=$RED \
                            icon="󱥁"
  echo $TEXT
fi
