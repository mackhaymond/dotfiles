#!/bin/bash

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

MASTER_DISPLAY_UUID="${YABAI_MASTER_DISPLAY_UUID:-37D8832A-2D66-02CA-B9F7-8F30A301B230}"

MODE="${1:-}"
LABEL="${2:-}"

if [ -z "$MODE" ] || [ -z "$LABEL" ]; then
  exit 64
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
"$SCRIPT_DIR/yabai_workspace_refresh.sh" labels-only >/dev/null 2>&1 || true

display_count() {
  jq -r 'length' <<<"$DISPLAYS_JSON"
}

focused_display_id() {
  jq -r '.[] | select(."has-focus" == true) | .id' <<<"$DISPLAYS_JSON" | head -n 1
}

master_display_id() {
  local count
  count="$(display_count)"

  if [ "$count" -le 1 ]; then
    jq -r '.[0].id // empty' <<<"$DISPLAYS_JSON"
    return
  fi

  local by_uuid
  by_uuid="$(jq -r --arg uuid "$MASTER_DISPLAY_UUID" '.[] | select(.uuid == $uuid) | .id' <<<"$DISPLAYS_JSON" | head -n 1)"
  if [ -n "$by_uuid" ]; then
    printf "%s\n" "$by_uuid"
    return
  fi

  jq -r 'min_by(.frame.w * .frame.h).id // .[0].id // empty' <<<"$DISPLAYS_JSON"
}

first_external_display_id() {
  local master_id="$1"
  jq -r --argjson master "$master_id" '.[] | select(.id != $master) | .id' <<<"$DISPLAYS_JSON" | head -n 1
}

space_index_for_label() {
  case "$1" in
    terminal) printf "1\n" ;;
    main) printf "2\n" ;;
    school) printf "3\n" ;;
    todo) printf "4\n" ;;
    schedule) printf "5\n" ;;
    mail) printf "6\n" ;;
    calendar) printf "7\n" ;;
    messages) printf "8\n" ;;
    chatgpt) printf "9\n" ;;
    codex) printf "10\n" ;;
    video) printf "11\n" ;;
  esac
}

refresh_spaces() {
  SPACES_JSON="$(yabai -m query --spaces 2>/dev/null)" || exit 0
}

ensure_space_label() {
  if jq -e --arg label "$LABEL" 'any(.[]; .label == $label)' >/dev/null 2>&1 <<<"$SPACES_JSON"; then
    return 0
  fi

  local index
  index="$(space_index_for_label "$LABEL")"
  if [ -z "$index" ]; then
    return 1
  fi

  if jq -e --argjson index "$index" 'any(.[]; .index == $index)' >/dev/null 2>&1 <<<"$SPACES_JSON"; then
    yabai -m space "$index" --label "$LABEL" >/dev/null 2>&1 || true
    refresh_spaces
  fi

  jq -e --arg label "$LABEL" 'any(.[]; .label == $label)' >/dev/null 2>&1 <<<"$SPACES_JSON"
}

space_display_id() {
  jq -r --arg label "$LABEL" '.[] | select(.label == $label) | .display' <<<"$SPACES_JSON" | head -n 1
}

focus_label_on_display() {
  local target_display="$1"
  local current_display

  current_display="$(space_display_id)"
  if [ -z "$current_display" ]; then
    exit 0
  fi

  if [ "$current_display" != "$target_display" ]; then
    yabai -m space "$LABEL" --display "$target_display" >/dev/null 2>&1 || true
    sleep 0.05
    refresh_spaces
  fi

  yabai -m space --focus "$LABEL" >/dev/null 2>&1 || true
}

DISPLAYS_JSON="$(yabai -m query --displays 2>/dev/null)" || exit 0
refresh_spaces
ensure_space_label || exit 0

MASTER_ID="$(master_display_id)"
FOCUSED_ID="$(focused_display_id)"

if [ -z "$MASTER_ID" ]; then
  exit 0
fi

if [ "$(display_count)" -le 1 ]; then
  focus_label_on_display "$MASTER_ID"
  exit 0
fi

case "$MODE" in
  external-first)
    if [ "$FOCUSED_ID" = "$MASTER_ID" ]; then
      TARGET_ID="$(first_external_display_id "$MASTER_ID")"
    else
      TARGET_ID="$FOCUSED_ID"
    fi
    ;;
  master)
    TARGET_ID="$MASTER_ID"
    ;;
  *)
    exit 64
    ;;
esac

if [ -z "${TARGET_ID:-}" ]; then
  exit 0
fi

focus_label_on_display "$TARGET_ID"
