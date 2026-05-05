#!/usr/bin/env bash

set -euo pipefail

target_client="${1:-}"
popup_session="btop-popup"
current_session="$(tmux display-message -p '#{session_name}')"

if [ "$current_session" = "$popup_session" ]; then
  tmux kill-session -t "$popup_session"
  exit 0
fi

if ! tmux has-session -t "$popup_session" 2>/dev/null; then
  tmux new-session -d -s "$popup_session" btop
fi
tmux set-option -t "$popup_session" status off
popup_cmd="tmux attach-session -t $popup_session"

popup_width="92%"
popup_height_pct=90
popup_height="${popup_height_pct}%"
popup_top_offset_pct=6
popup_bottom_pct=$(( popup_top_offset_pct + popup_height_pct ))

if [ -n "$target_client" ]; then
  client_height=$(tmux display-message -p -t "$target_client" '#{client_height}')
else
  client_height=$(tmux display-message -p '#{client_height}')
fi
popup_y=$(( client_height * popup_bottom_pct / 100 ))

if [ -n "$target_client" ]; then
  tmux display-popup -t "$target_client" -E -w "$popup_width" -h "$popup_height" -y "$popup_y" "$popup_cmd"
else
  tmux display-popup -E -w "$popup_width" -h "$popup_height" -y "$popup_y" "$popup_cmd"
fi
