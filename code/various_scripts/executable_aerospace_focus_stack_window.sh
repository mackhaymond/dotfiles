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

window_ids=()
while IFS=$'\t' read -r window_id window_layout; do
  if [ -n "$window_id" ] && [ "$window_layout" != "floating" ]; then
    window_ids+=("$window_id")
  fi
done < <("$AEROSPACE" list-windows --workspace focused --format '%{window-id}%{tab}%{window-layout}' 2>/dev/null)

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

"$AEROSPACE" focus --window-id "${window_ids[$target_index]}"
