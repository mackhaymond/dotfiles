#!/bin/bash

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

MASTER_DISPLAY_UUID="${YABAI_MASTER_DISPLAY_UUID:-37D8832A-2D66-02CA-B9F7-8F30A301B230}"
CACHE_FILE="${YABAI_WORKSPACE_CACHE:-${TMPDIR:-/tmp}/yabai_workspace_cache.env}"

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

DISPLAYS_JSON=$(yabai -m query --displays 2>/dev/null) || exit 0
DISPLAY_COUNT=$(jq -r 'length' <<<"$DISPLAYS_JSON")
MASTER_DISPLAY_ID=$(
  jq -r --arg uuid "$MASTER_DISPLAY_UUID" '
    ([.[] | select(.uuid == $uuid) | .id][0]) //
    (min_by(.frame.w * .frame.h).id) //
    empty
  ' <<<"$DISPLAYS_JSON" | head -n 1
)
EXTERNAL_DISPLAY_ID=$(
  jq -r --argjson master "${MASTER_DISPLAY_ID:-0}" '
    .[] | select(.id != $master) | .id
  ' <<<"$DISPLAYS_JSON" | head -n 1
)

{
  printf 'DISPLAY_COUNT=%s\n' "${DISPLAY_COUNT:-0}"
  printf 'MASTER_DISPLAY_ID=%s\n' "${MASTER_DISPLAY_ID:-}"
  printf 'EXTERNAL_DISPLAY_ID=%s\n' "${EXTERNAL_DISPLAY_ID:-}"
  printf 'MASTER_DISPLAY_UUID=%s\n' "$MASTER_DISPLAY_UUID"
} >"${CACHE_FILE}.$$" && mv "${CACHE_FILE}.$$" "$CACHE_FILE"

yabai -m rule --apply >/dev/null 2>&1 || true
