#!/bin/bash
set -euo pipefail

AEROSPACE="${AEROSPACE:-/opt/homebrew/bin/aerospace}"

monitor_rows="$("$AEROSPACE" list-monitors --format "%{monitor-id}\t%{monitor-is-main}")"
main_monitor="$(awk -F '\t' '$2 == "true" { print $1; exit }' <<<"$monitor_rows")"
secondary_monitor="$(awk -F '\t' '$2 == "false" { print $1; exit }' <<<"$monitor_rows")"

if [ -z "${main_monitor:-}" ] || [ -z "${secondary_monitor:-}" ]; then
  exit 0
fi

main_workspace="$("$AEROSPACE" list-workspaces --monitor "$main_monitor" --visible)"
secondary_workspace="$("$AEROSPACE" list-workspaces --monitor "$secondary_monitor" --visible)"

if [ -z "${main_workspace:-}" ] || [ -z "${secondary_workspace:-}" ] || [ "$main_workspace" = "$secondary_workspace" ]; then
  exit 0
fi

focused_monitor="$("$AEROSPACE" list-monitors --focused --format "%{monitor-id}")"

"$AEROSPACE" move-workspace-to-monitor --workspace "$main_workspace" "$secondary_monitor"
"$AEROSPACE" move-workspace-to-monitor --workspace "$secondary_workspace" "$main_monitor"

case "$focused_monitor" in
  "$main_monitor") "$AEROSPACE" focus-monitor "$main_monitor" ;;
  "$secondary_monitor") "$AEROSPACE" focus-monitor "$secondary_monitor" ;;
esac
