#!/usr/bin/env bash

set -euo pipefail

if [ "${1:-}" = "--popup-child" ]; then
  cwd_file="${2:?missing cwd file}"
  target_pane="${3:-}"
  if [ "$#" -ge 3 ]; then
    shift 3
  else
    set --
  fi
  cwd=""
  yazi_status=0

  trap 'command rm -f -- "$cwd_file"' EXIT

  command yazi "$@" --cwd-file="$cwd_file" || yazi_status=$?

  if IFS= read -r cwd < "$cwd_file" && [ -n "$cwd" ] && [ -d "$cwd" ] && [ -n "$target_pane" ]; then
    tmux send-keys -t "$target_pane" "builtin cd -- $(printf '%q' "$cwd")" Enter
  fi

  exit "$yazi_status"
fi

target_client="${1:-}"
current_path="${2:-$HOME}"
target_pane="${3:-}"
if [ "$#" -ge 3 ]; then
  shift 3
else
  set --
fi
script_path="${BASH_SOURCE[0]}"
cwd_file="$(mktemp -t "yazi-cwd.XXXXXX")"

if [ ! -d "$current_path" ]; then
  current_path="$HOME"
fi

popup_cmd="$(printf '%q ' "$script_path" --popup-child "$cwd_file" "$target_pane" "$@")"

popup_width="92%"
popup_height="90%"
popup_title=" yazi "

if [ -n "$target_client" ]; then
  tmux display-popup -t "$target_client" -E -d "$current_path" -w "$popup_width" -h "$popup_height" -T "$popup_title" "$popup_cmd"
else
  tmux display-popup -E -d "$current_path" -w "$popup_width" -h "$popup_height" -T "$popup_title" "$popup_cmd"
fi
