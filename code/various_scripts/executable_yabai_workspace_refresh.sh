#!/bin/bash

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

case "${1:-}" in
  display-changed)
    sleep 1
    ;;
esac

SPACES_JSON=$(yabai -m query --spaces 2>/dev/null) || exit 0

label_space_if_missing() {
  local index="$1"
  local label="$2"

  if jq -e --arg label "$label" 'any(.[]; .label == $label)' >/dev/null 2>&1 <<<"$SPACES_JSON"; then
    return 0
  fi

  if jq -e --argjson index "$index" 'any(.[]; .index == $index)' >/dev/null 2>&1 <<<"$SPACES_JSON"; then
    yabai -m space "$index" --label "$label" >/dev/null 2>&1 || true
  fi
}

label_space_if_missing 1 terminal
label_space_if_missing 2 main
label_space_if_missing 3 school
label_space_if_missing 4 todo
label_space_if_missing 5 schedule
label_space_if_missing 6 mail
label_space_if_missing 7 calendar
label_space_if_missing 8 messages
label_space_if_missing 9 chatgpt
label_space_if_missing 10 codex
label_space_if_missing 11 video

case "${1:-}" in
  labels-only)
    exit 0
    ;;
esac

yabai -m rule --apply >/dev/null 2>&1 || true
