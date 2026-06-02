#!/bin/bash
set -euo pipefail

AEROSPACE="${AEROSPACE:-/opt/homebrew/bin/aerospace}"
DIRECTION="${1:-next}"

case "$DIRECTION" in
  next|prev) ;;
  *) exit 64 ;;
esac

focused_id=""
focused_id="$("$AEROSPACE" list-windows --focused --format '%{window-id}' 2>/dev/null || true)"

workspace=""
workspace="$("$AEROSPACE" list-workspaces --focused 2>/dev/null || true)"
if [ -z "$workspace" ]; then
  workspace="$("$AEROSPACE" list-workspaces --monitor all --visible --format '%{workspace}' 2>/dev/null | head -n 1 || true)"
fi

if [ -z "$workspace" ]; then
  exit 0
fi

window_ids=()
target_app_names=()
target_window_titles=()
while IFS=$'\t' read -r window_id window_layout app_name window_title; do
  if [ -n "$window_id" ] && [ "$window_layout" != "floating" ]; then
    window_ids+=("$window_id")
    target_app_names+=("$app_name")
    target_window_titles+=("$window_title")
  fi
done < <("$AEROSPACE" list-windows --workspace "$workspace" --format '%{window-id}%{tab}%{window-layout}%{tab}%{app-name}%{tab}%{window-title}' 2>/dev/null)

window_count="${#window_ids[@]}"
if [ "$window_count" -eq 0 ]; then
  exit 0
fi

current_index=-1
for i in "${!window_ids[@]}"; do
  if [ "${window_ids[$i]}" = "$focused_id" ]; then
    current_index="$i"
    break
  fi
done

if [ "$current_index" -lt 0 ]; then
  if [ "$DIRECTION" = "next" ]; then
    target_index=0
  else
    target_index=$((window_count - 1))
  fi
elif [ "$DIRECTION" = "next" ]; then
  target_index=$(((current_index + 1) % window_count))
else
  target_index=$(((current_index + window_count - 1) % window_count))
fi

target_window_id="${window_ids[$target_index]}"
target_app_name="${target_app_names[$target_index]}"
target_window_title="${target_window_titles[$target_index]}"

"$AEROSPACE" focus --window-id "$target_window_id"

TARGET_APP_NAME="$target_app_name" TARGET_WINDOW_TITLE="$target_window_title" osascript >/dev/null <<'APPLESCRIPT' || true
set targetAppName to system attribute "TARGET_APP_NAME"
set targetWindowTitle to system attribute "TARGET_WINDOW_TITLE"

tell application "System Events"
  set matchingProcesses to every application process whose name is targetAppName
  if matchingProcesses is {} then return

  set targetProcess to item 1 of matchingProcesses
  set frontmost of targetProcess to true

  if targetWindowTitle is not "" then
    repeat with candidateWindow in windows of targetProcess
      try
        if name of candidateWindow is targetWindowTitle then
          perform action "AXRaise" of candidateWindow
          return
        end if
      end try
    end repeat
  end if

  try
    perform action "AXRaise" of window 1 of targetProcess
  end try
end tell
APPLESCRIPT
