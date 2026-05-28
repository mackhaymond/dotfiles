#!/usr/bin/env bash

set -euo pipefail

current_path="${1:-$HOME}"
if [ "$#" -gt 0 ]; then
  shift
fi

if [ ! -d "$current_path" ]; then
  current_path="$HOME"
fi

popup_cmd="$(printf '%q ' command yazi "$@")"

tmux display-popup \
  -E \
  -d "$current_path" \
  -w 92% \
  -h 90% \
  -T " yazi " \
  "$popup_cmd"
