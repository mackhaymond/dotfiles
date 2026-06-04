#!/bin/bash

# Reconcile the canonical labeled spaces and refresh the display cache.
#
# Jobs (all idempotent, all non-destructive):
#   1. Resolve display topology -> DISPLAY_COUNT / MASTER / EXTERNAL indices.
#   2. Ensure every canonical label exists (heals a missing label on the laptop).
#   3. Re-pin each label onto the space where its app actually lives (laptop only).
#   4. Write the shared cache consumed by the workspace/display/move scripts.
#
# It NO LONGER moves spaces between displays. Plug/unplug reconciliation lives in
# yabai_displays.sh; this script never resets a docked layout back to the laptop.

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

MASTER_DISPLAY_UUID="${YABAI_MASTER_DISPLAY_UUID:-37D8832A-2D66-02CA-B9F7-8F30A301B230}"
CACHE_FILE="${YABAI_WORKSPACE_CACHE:-${HOME}/.cache/yabai/workspace_cache.env}"

SPACES_JSON=$(yabai -m query --spaces 2>/dev/null) || exit 0
WINDOWS_JSON=$(yabai -m query --windows 2>/dev/null || printf '[]')
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

refresh_spaces_json() {
  SPACES_JSON=$(yabai -m query --spaces 2>/dev/null) || exit 0
}

space_index_for_label() {
  local label="$1"

  jq -r --arg label "$label" '
    ([.[] | select(.label == $label) | .index][0]) // empty
  ' <<<"$SPACES_JSON"
}

space_label_for_index() {
  local index="$1"

  jq -r --argjson index "$index" '
    ([.[] | select(.index == $index) | .label][0]) // empty
  ' <<<"$SPACES_JSON"
}

space_display_for_index() {
  local index="$1"

  jq -r --argjson index "$index" '
    ([.[] | select(.index == $index) | .display][0]) // empty
  ' <<<"$SPACES_JSON"
}

first_unlabeled_space_index_on_master() {
  [ -z "${MASTER_DISPLAY_INDEX:-}" ] && return 0

  jq -r --argjson master "$MASTER_DISPLAY_INDEX" '
    ([.[] | select(.display == $master and .label == "") | .index][0]) // empty
  ' <<<"$SPACES_JSON"
}

space_for_app() {
  local app_pattern="$1"

  jq -r --arg app_pattern "$app_pattern" '
    ([.[] |
      select(.app | test($app_pattern)) |
      select(."is-sticky" == false) |
      select(."is-floating" == false) |
      select(.subrole == "AXStandardWindow" or .role == "") |
      select(.space as $space | $space != null) |
      .space
    ][0]) // empty
  ' <<<"$WINDOWS_JSON"
}

space_for_app_title() {
  local app_pattern="$1"
  local title_pattern="$2"

  jq -r --arg app_pattern "$app_pattern" --arg title_pattern "$title_pattern" '
    ([.[] |
      select((.app | test($app_pattern)) and (.title | test($title_pattern))) |
      select(."is-sticky" == false) |
      select(."is-floating" == false) |
      select(.subrole == "AXStandardWindow" or .role == "") |
      select(.space as $space | $space != null) |
      .space
    ][0]) // empty
  ' <<<"$WINDOWS_JSON"
}

assign_label_to_space() {
  local label="$1"
  local index="$2"
  local current_index

  [ -z "$index" ] && return 0

  current_index=$(space_index_for_label "$label")
  if [ "$current_index" = "$index" ]; then
    return 0
  fi

  if [ -n "$current_index" ]; then
    yabai -m space "$current_index" --label >/dev/null 2>&1 || true
    refresh_spaces_json
  fi

  yabai -m space "$index" --label "$label" >/dev/null 2>&1 || true
  refresh_spaces_json
}

assign_label_to_pinned_app_space() {
  local label="$1"
  local app_pattern="$2"
  local index
  local display_index

  index=$(space_for_app "$app_pattern")
  [ -z "$index" ] && return 0
  display_index=$(space_display_for_index "$index")
  [ -n "${MASTER_DISPLAY_INDEX:-}" ] && [ "$display_index" != "$MASTER_DISPLAY_INDEX" ] && return 0
  [ -n "$index" ] && assign_label_to_space "$label" "$index"
}

assign_label_to_pinned_window_space() {
  local label="$1"
  local app_pattern="$2"
  local title_pattern="$3"
  local index
  local display_index

  index=$(space_for_app_title "$app_pattern" "$title_pattern")
  [ -z "$index" ] && return 0
  display_index=$(space_display_for_index "$index")
  [ -n "${MASTER_DISPLAY_INDEX:-}" ] && [ "$display_index" != "$MASTER_DISPLAY_INDEX" ] && return 0
  [ -n "$index" ] && assign_label_to_space "$label" "$index"
}

label_space_if_missing() {
  local index="$1"
  local label="$2"

  if jq -e --arg label "$label" 'any(.[]; .label == $label)' >/dev/null 2>&1 <<<"$SPACES_JSON"; then
    return 0
  fi

  if [ -n "$(space_label_for_index "$index")" ] || [ "$(space_display_for_index "$index")" != "${MASTER_DISPLAY_INDEX:-}" ]; then
    index=$(first_unlabeled_space_index_on_master)
  fi

  if [ -z "$index" ]; then
    index=$(first_unlabeled_space_index_on_master)
  fi

  if [ -z "$index" ] && [ -n "${MASTER_DISPLAY_INDEX:-}" ]; then
    yabai -m space --create "$MASTER_DISPLAY_INDEX" >/dev/null 2>&1 || true
    refresh_spaces_json
    index=$(first_unlabeled_space_index_on_master)
  fi

  [ -n "$index" ] && assign_label_to_space "$label" "$index"
}

label_missing_workspace_labels() {
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
}

label_missing_workspace_labels

assign_label_to_pinned_app_space terminal '^(wezterm-gui|WezTerm)$'
assign_label_to_pinned_window_space main '^Arc$' '^(Main|codex the model)'
assign_label_to_pinned_window_space school '^Arc$' '^(ECON 102|Physics)'
assign_label_to_pinned_app_space todo '^Todoist$'
assign_label_to_pinned_app_space schedule '^Granola$'
assign_label_to_pinned_app_space mail '^Spark Mail$'
assign_label_to_pinned_app_space calendar '^Notion Calendar$'
assign_label_to_pinned_app_space messages '^Messages$'
assign_label_to_pinned_app_space chatgpt '^ChatGPT$'
assign_label_to_pinned_app_space codex '^Codex$'

label_missing_workspace_labels

mkdir -p "$(dirname "$CACHE_FILE")" 2>/dev/null || true
{
  printf 'DISPLAY_COUNT=%s\n' "${DISPLAY_COUNT:-0}"
  printf 'MASTER_DISPLAY_INDEX=%s\n' "${MASTER_DISPLAY_INDEX:-}"
  printf 'EXTERNAL_DISPLAY_INDEX=%s\n' "${EXTERNAL_DISPLAY_INDEX:-}"
  printf 'MASTER_DISPLAY_UUID=%s\n' "$MASTER_DISPLAY_UUID"
} >"${CACHE_FILE}.$$" && mv "${CACHE_FILE}.$$" "$CACHE_FILE"

yabai -m rule --apply >/dev/null 2>&1 || true
