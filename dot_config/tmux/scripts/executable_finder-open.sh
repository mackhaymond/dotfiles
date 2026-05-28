#!/usr/bin/env bash

set -euo pipefail

root="${1:-$PWD}"
if [ "$#" -gt 0 ]; then
  shift
fi

if [ ! -d "$root" ]; then
  root="$HOME"
fi

if ! command -v fd >/dev/null 2>&1; then
  tmux display-message "finder-open: fd not found"
  exit 1
fi

if ! command -v fzf >/dev/null 2>&1; then
  tmux display-message "finder-open: fzf not found"
  exit 1
fi

preview='if [ -d {} ]; then eza --icons --group-directories-first --tree --level=2 --color=always {}; elif command -v bat >/dev/null 2>&1; then bat --style=numbers --color=always --line-range=:200 {}; else file {}; fi'

selection="$({
  command fd \
    --hidden \
    --follow \
    --exclude .git \
    --exclude node_modules \
    --exclude .DS_Store \
    . "$root"
} | command fzf \
  --exit-0 \
  --select-1 \
  --multi \
  --reverse \
  --prompt='open ❯ ' \
  --preview "$preview" \
  --preview-window=right:60%:wrap)" || exit 0

[ -n "$selection" ] || exit 0

while IFS= read -r path; do
  [ -n "$path" ] || continue
  command open "$path"
done <<< "$selection"
