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

WORKSPACE_LABELS='terminal
main
school
todo
schedule
mail
calendar
messages
chatgpt
codex
video'

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
MASTER_DISPLAY_INDEX=$(
  jq -r --arg uuid "$MASTER_DISPLAY_UUID" '
    ([.[] | select(.uuid == $uuid) | .index][0]) //
    (min_by(.frame.w * .frame.h).index) //
    empty
  ' <<<"$DISPLAYS_JSON" | head -n 1
)
EXTERNAL_DISPLAY_INDEX=$(
  jq -r --argjson master "${MASTER_DISPLAY_INDEX:-0}" '
    .[] | select(.index != $master) | .index
  ' <<<"$DISPLAYS_JSON" | head -n 1
)

if [ "${DISPLAY_COUNT:-0}" -le 1 ] && [ -n "${MASTER_DISPLAY_INDEX:-}" ]; then
  while IFS= read -r label; do
    [ -z "$label" ] && continue
    SPACE_DISPLAY_INDEX=$(
      jq -r --arg label "$label" '
        ([.[] | select(.label == $label) | .display][0]) // empty
      ' <<<"$SPACES_JSON"
    )
    if [ -n "$SPACE_DISPLAY_INDEX" ] && [ "$SPACE_DISPLAY_INDEX" != "$MASTER_DISPLAY_INDEX" ]; then
      yabai -m space "$label" --display "$MASTER_DISPLAY_INDEX" >/dev/null 2>&1 || true
    fi
  done <<<"$WORKSPACE_LABELS"
fi

{
  printf 'DISPLAY_COUNT=%s\n' "${DISPLAY_COUNT:-0}"
  printf 'MASTER_DISPLAY_INDEX=%s\n' "${MASTER_DISPLAY_INDEX:-}"
  printf 'EXTERNAL_DISPLAY_INDEX=%s\n' "${EXTERNAL_DISPLAY_INDEX:-}"
  printf 'MASTER_DISPLAY_UUID=%s\n' "$MASTER_DISPLAY_UUID"
} >"${CACHE_FILE}.$$" && mv "${CACHE_FILE}.$$" "$CACHE_FILE"

yabai -m rule --apply >/dev/null 2>&1 || true
