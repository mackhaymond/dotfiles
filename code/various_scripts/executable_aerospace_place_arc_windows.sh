#!/bin/bash
set -euo pipefail

AEROSPACE="${AEROSPACE:-/opt/homebrew/bin/aerospace}"
ARC_BUNDLE_ID="company.thebrowser.Browser"

arc_windows=()
while IFS= read -r window_id; do
  if [ -n "$window_id" ]; then
    arc_windows+=("$window_id")
  fi
done < <("$AEROSPACE" list-windows --monitor all --app-bundle-id "$ARC_BUNDLE_ID" --format '%{window-id}' 2>/dev/null | sort -n)

if [ "${#arc_windows[@]}" -eq 0 ]; then
  exit 0
fi

"$AEROSPACE" move-node-to-workspace --window-id "${arc_windows[0]}" 2 >/dev/null 2>&1 || true

if [ "${#arc_windows[@]}" -ge 2 ]; then
  "$AEROSPACE" move-node-to-workspace --window-id "${arc_windows[1]}" 3 >/dev/null 2>&1 || true
fi
