#!/bin/bash

set -u

# Reconcile the canonical labeled spaces and refresh the display cache.
#
# Jobs (all idempotent; 1-4 are non-destructive, 5 destroys empty spaces):
#   1. Resolve display topology -> DISPLAY_COUNT / MASTER / EXTERNAL indices.
#   2. Ensure every canonical label exists (heals a missing label on the laptop).
#   3. Re-pin each label onto the space where its app actually lives (laptop only).
#   4. Write the shared cache consumed by the workspace/display/move scripts.
#   5. Only with --sweep (yabai_heal.sh): destroy surplus EMPTY UNLABELED spaces,
#      keeping one per display (yabai_sweep_empty_spaces in yabai_common.sh).
#
# It NO LONGER moves spaces between displays. Plug/unplug reconciliation lives in
# yabai_displays.sh; this script never resets a docked layout back to the laptop.

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

# shellcheck source=/dev/null
. "$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)/yabai_common.sh"
CACHE_FILE="${YABAI_WORKSPACE_CACHE:-${HOME}/.cache/yabai/workspace_cache.env}"

#   yabai_workspace_refresh.sh [--sweep]
# --sweep (yabai_heal.sh): after reconciling, trim surplus empty unlabeled spaces.
SWEEP=0
[ "${1:-}" = --sweep ] && SWEEP=1

# Single-flight, SERIALIZED rather than coalesced. Refresh is called from heal, the
# hotplug handler, startup and half the keybinds; two at once (undock fires
# display_removed AND space_destroyed) each saw the same missing label and each
# created a space for it, or stripped/re-applied labels over one another.
# When the lock is held we WAIT for the holder (bounded, ~5s) and then run ourselves:
#   - not "exit at once": callers (yabai_displays.sh, yabai_workspace.sh on a cache
#     miss) read the cache/labels the moment we return, so a refresh must have
#     finished by then;
#   - not "wait, then return without running": the holder's snapshot may predate
#     what our caller just did (the pull-home before yabai_displays.sh's final
#     refresh), so its result is not ours. A refresh is idempotent and ~0.3s.
# If the holder is still going after the wait (a bare-"[" episode makes every
# re-query retry), we run WITHOUT the lock -- exactly the pre-lock behaviour --
# rather than hang or leave the caller with no refresh; the release below is
# ownership-aware, so it cannot remove the holder's lock. No deadlock with callers:
# they hold DIFFERENT locks (heal.lock, yabai_displays.lock), and nothing this
# script runs calls back into it. A holder that died is detected by pid and broken
# at once (yabai_lock_acquire), so a restart mid-refresh costs no wait.
REFRESH_LOCK="$YABAI_STATE_DIR/refresh.lock"
LOCK_HELD=0
yabai_lock_acquire_wait "$REFRESH_LOCK" 50 && LOCK_HELD=1
trap 'yabai_lock_release "$REFRESH_LOCK"' EXIT
[ "$LOCK_HELD" = 1 ] || yabai_log refresh "lock busy after ~5s; running unlocked"
# Accepted, not closed: an UNLOCKED run can still be between `space --create` and
# labeling the result (label_space_if_missing) while a locked `--sweep` refresh runs.
# It takes a holder stuck >5s, a missing label with no unlabeled space to reuse, and
# a heal landing in that ~0.1s stretch; the worst outcome is the label landing on the
# other of the two empties and the laptop left with no spare (the next refresh that
# needs a space creates one). An unlocked run never sweeps (bottom of this file).

# A VALIDATED bulk query, or nothing. `yabai -m query --spaces` intermittently answers a
# bare "[" with exit 0 (space_manager_query_spaces_for_displays skips the closing "]"
# when display_space_list() is NULL). Every helper below then sees zero spaces, decides
# all ten labels are missing and finds no unlabeled space to reuse -- so the old
# exit-code-only check ran `space --create` once per label, twice per run (20 spurious
# spaces). Not yabai_spaces_json(): its label-by-label fallback drops UNLABELED spaces,
# which this script needs to see to reuse them. Retry briefly, else give up this run.
query_json_array() {
  local out _try
  for _try in 1 2 3; do
    out=$(yabai -m query "$@" 2>/dev/null)
    if jq -e 'type == "array" and length > 0' >/dev/null 2>&1 <<<"$out"; then
      printf '%s' "$out"
      return 0
    fi
    sleep 0.2
  done
  return 1
}

SPACES_JSON=$(query_json_array --spaces) || exit 0
WINDOWS_JSON=$(yabai -m query --windows 2>/dev/null)
jq -e 'type == "array"' >/dev/null 2>&1 <<<"$WINDOWS_JSON" || WINDOWS_JSON='[]'
DISPLAYS_JSON=$(query_json_array --displays) || exit 0
# Ignored displays (agent-chrome's virtual screen) don't exist for the cache: no
# DISPLAY_COUNT bump, never EXTERNAL_DISPLAY_INDEX.
REAL_DISPLAYS_JSON=$(yabai_filter_displays <<<"$DISPLAYS_JSON") || exit 0
# ...unless nothing else is attached (lid closed, only the virtual screen left): then
# the ignored display is the only one there is and holds every space. Filtering it out
# wrote DISPLAY_COUNT=0 with an empty MASTER_DISPLAY_INDEX, yabai_load_cache rejected
# that for every consumer, and each hyper+<label> ran a full refresh and focused
# nothing. Keep the unfiltered list in that one case (yabai_master_index does the same).
[ "$REAL_DISPLAYS_JSON" = "[]" ] || DISPLAYS_JSON=$REAL_DISPLAYS_JSON
DISPLAY_COUNT=$(jq -r 'length' <<<"$DISPLAYS_JSON")
MASTER_DISPLAY_INDEX=$(yabai_master_index "$DISPLAYS_JSON")
# Only meaningful once master is known; computing it against a placeholder master
# (the old `${MASTER_DISPLAY_INDEX:-0}`) conflated "master unknown" with "master is
# display 0" and could write an inconsistent master(empty)/external(set) pair.
if [ -n "${MASTER_DISPLAY_INDEX:-}" ]; then
  EXTERNAL_DISPLAY_INDEX=$(
    jq -r --argjson master "$MASTER_DISPLAY_INDEX" '
      .[] | select(.index != $master) | .index
    ' <<<"$DISPLAYS_JSON" | head -n 1
  )
else
  EXTERNAL_DISPLAY_INDEX=""
fi

# Mid-run re-query. NOT fatal, unlike the initial one: by now labels have been moved,
# and exiting on a bare-"[" answer here skipped the cache write and yabai_pin_rules_add
# (and, before the reorder in assign_label_to_space, left a label stripped for good).
# On a bad answer the previous snapshot is kept and 1 returned.
refresh_spaces_json() {
  local fresh
  fresh=$(query_json_array --spaces) || return 1
  SPACES_JSON=$fresh
}

# Backstop for the create path below: if a `space --create` does not yield a new
# unlabeled master space (the scripting addition rejected it -- on yabai 7.1.25 /
# macOS 26.6 create returns 0 and does nothing -- or the query lied), stop creating
# for the rest of this run instead of retrying once per missing label.
CREATE_OK=1

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

# Native-fullscreen Spaces never carry a canonical label (except `terminal`, which
# yabai_terminal_follow.sh deliberately moves onto a fullscreen WezTerm). They sit in
# the master's space list like any other, so counting them shifted every label after
# one by a position, and the first-unlabeled fallback could hand one a label.
first_unlabeled_space_index_on_master() {
  [ -z "${MASTER_DISPLAY_INDEX:-}" ] && return 0

  jq -r --argjson master "$MASTER_DISPLAY_INDEX" '
    ([.[] | select(.display == $master and .label == "" and (."is-native-fullscreen" | not))
      | .index][0]) // empty
  ' <<<"$SPACES_JSON"
}

# Index of the Nth (1-based) regular -- non-fullscreen -- space on the master display.
nth_regular_space_index_on_master() {
  [ -z "${MASTER_DISPLAY_INDEX:-}" ] && return 0

  jq -r --argjson master "$MASTER_DISPLAY_INDEX" --argjson n "$1" '
    ([.[] | select(.display == $master and (."is-native-fullscreen" | not))]
      | sort_by(.index) | .[$n - 1].index) // empty
  ' <<<"$SPACES_JSON"
}

space_is_fullscreen() {
  jq -e --argjson index "$1" 'any(.[]; .index == $index and ."is-native-fullscreen")' \
    >/dev/null 2>&1 <<<"$SPACES_JSON"
}

# Pinned app -> home label (YABAI_PINNED_HOMES + the agent apps), for "whose space is it".
HOME_MAP=$(yabai_home_map_json)
[ -n "$HOME_MAP" ] || HOME_MAP='{}'

# Does space $1 hold a window of an app matching regex $2?
space_hosts_app() {
  jq -e --argjson index "$1" --arg re "$2" 'any(.[]; .space == $index and (.app | test($re)))' \
    >/dev/null 2>&1 <<<"$WINDOWS_JSON"
}

# Does space $1 hold a window of an app whose home is label $2? Arc counts for
# main/school (arcSync pins its main windows there; it has no entry in the map).
space_hosts_label_app() {
  jq -e --argjson index "$1" --arg label "$2" --argjson home "$HOME_MAP" '
    any(.[]; .space == $index
             and ($home[.app] == $label
                  or (.app == "Arc" and ($label == "main" or $label == "school"))))' \
    >/dev/null 2>&1 <<<"$WINDOWS_JSON"
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

  # Strip and re-apply back to back, and only then re-query: a re-query between the
  # two that hit a bare-"[" episode used to end the run with the label stripped from
  # its old space and never applied to the new one.
  if [ -n "$current_index" ]; then
    yabai -m space "$current_index" --label >/dev/null 2>&1 || true
  fi

  yabai -m space "$index" --label "$label" >/dev/null 2>&1 || true
  if ! refresh_spaces_json; then
    # No fresh snapshot: record the move in the old one, or the next label would
    # still see this space as unlabeled and be handed it too.
    local patched
    patched=$(jq -c --arg label "$label" --argjson index "$index" '
      map(if .index == $index then .label = $label
          elif .label == $label then .label = ""
          else . end)' 2>/dev/null <<<"$SPACES_JSON") && [ -n "$patched" ] && SPACES_JSON=$patched
  fi
}

# Label-follows-app: move <label> onto the space where its pinned app lives. This is
# the repair for labels handed out by POSITION (after a yabai restart, or a dropped
# label re-created on a fresh space). It used to follow ANY window of the app, which
# let a stray one drag the label along: Claude stranded on `todo` at a restart took
# `ai` there and `todo` went to ChatGPT's space; Claude in native fullscreen pulled
# `ai` onto the fullscreen Space; ChatGPT+Claude on different spaces flipped `ai` to
# whichever was checked last. Now, conservatively:
#   1. the label stays where it is if that space already hosts one of its apps;
#   2. it never takes a space whose OWN label's app lives there (that window is the
#      stray, not the label);
#   3. it never lands on a native-fullscreen Space (terminal excepted, see above).
assign_label_to_pinned_app_space() {
  local label="$1"
  local app_pattern="$2"
  local index current held
  local display_index

  current=$(space_index_for_label "$label")
  [ -n "$current" ] && space_hosts_app "$current" "$app_pattern" && return 0

  index=$(space_for_app "$app_pattern")
  [ -z "$index" ] && return 0
  display_index=$(space_display_for_index "$index")
  [ -n "${MASTER_DISPLAY_INDEX:-}" ] && [ "$display_index" != "$MASTER_DISPLAY_INDEX" ] && return 0
  [ "$label" != terminal ] && space_is_fullscreen "$index" && return 0
  held=$(space_label_for_index "$index")
  [ -n "$held" ] && [ "$held" != "$label" ] && space_hosts_label_app "$index" "$held" && return 0
  assign_label_to_space "$label" "$index"
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

  # Canonical position N = the Nth REGULAR space on the master (fullscreen Spaces don't count).
  index=$(nth_regular_space_index_on_master "$index")
  if [ -z "$index" ] || [ -n "$(space_label_for_index "$index")" ]; then
    index=$(first_unlabeled_space_index_on_master)
  fi

  if [ -z "$index" ]; then
    index=$(first_unlabeled_space_index_on_master)
  fi

  if [ -z "$index" ] && [ -n "${MASTER_DISPLAY_INDEX:-}" ] && [ "$CREATE_OK" = 1 ]; then
    yabai -m space --create "$MASTER_DISPLAY_INDEX" >/dev/null 2>&1 || true
    refresh_spaces_json
    index=$(first_unlabeled_space_index_on_master)
    [ -n "$index" ] || CREATE_OK=0
    # A new master space renumbers every space after it (the external's), so the
    # window->space indices captured at the top are stale now.
    WINDOWS_JSON=$(yabai -m query --windows 2>/dev/null)
    jq -e 'type == "array"' >/dev/null 2>&1 <<<"$WINDOWS_JSON" || WINDOWS_JSON='[]'
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
  label_space_if_missing 9 ai
  label_space_if_missing 10 agent
}

label_missing_workspace_labels

assign_label_to_pinned_app_space terminal '^(wezterm-gui|WezTerm)$'
# Arc main/school pinning is no longer title-based (titles change as you browse,
# and Little Arc popups are byte-identical to main windows in every yabai field).
# It now lives in Hammerspoon's arcSync() (see dot_hammerspoon/init.lua), which
# classifies main vs Little Arc windows via AXIdentifier -- something yabai cannot
# read -- and pins only the main windows. (Little Arc is left fully managed, not
# floated.) There is no yabai_arc_pin.sh script.
assign_label_to_pinned_app_space todo '^Todoist$'
assign_label_to_pinned_app_space schedule '^Granola$'
assign_label_to_pinned_app_space mail '^Spark Mail$'
assign_label_to_pinned_app_space calendar '^Notion Calendar$'
assign_label_to_pinned_app_space messages '^Messages$'
# ChatGPT and Claude share the `ai` home space: ONE call with both, so rule 1 keeps
# `ai` wherever either already is (two calls flipped it to whichever came second).
assign_label_to_pinned_app_space ai '^(ChatGPT|Claude)$'
# The coding-agent apps share the `agent` home space; whichever is running labels
# it (one regex over the whole set -- see YABAI_AGENT_APPS in yabai_common.sh).
assign_label_to_pinned_app_space agent "$YABAI_AGENT_APPS_RE"

label_missing_workspace_labels

mkdir -p "$(dirname "$CACHE_FILE")" 2>/dev/null || true
{
  printf 'DISPLAY_COUNT=%s\n' "${DISPLAY_COUNT:-0}"
  printf 'MASTER_DISPLAY_INDEX=%s\n' "${MASTER_DISPLAY_INDEX:-}"
  printf 'EXTERNAL_DISPLAY_INDEX=%s\n' "${EXTERNAL_DISPLAY_INDEX:-}"
  printf 'MASTER_DISPLAY_UUID=%s\n' "$YABAI_MASTER_DISPLAY_UUID"
} >"${CACHE_FILE}.$$" && mv "${CACHE_FILE}.$$" "$CACHE_FILE"

# Re-bind the pinning rules to the labels as they stand NOW (space= resolves to a space
# id at rule-add time; see yabai_pin_rules_add), then apply them.
yabai_pin_rules_add
yabai -m rule --apply >/dev/null 2>&1 || true

# Keep the labeled spaces in their canonical order on each display.
"$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)/yabai_reorder_spaces.sh" >/dev/null 2>&1 || true

# Last, and only under the lock: with every label settled, what is still empty and
# unlabeled is a leaked spare. Unlocked (see above) another refresh may be between
# `space --create` and labeling the result, and the sweep must not race that.
if [ "$SWEEP" = 1 ] && [ "$LOCK_HELD" = 1 ]; then
  yabai_sweep_empty_spaces
fi
