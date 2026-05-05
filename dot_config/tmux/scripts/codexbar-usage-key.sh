#!/usr/bin/env bash
set -euo pipefail

get_tmux_opt() {
  local opt_name="${1:?}"
  local default_value="${2-}"

  local value
  value="$(tmux show-option -gqv "$opt_name" 2>/dev/null || true)"
  if [[ -z "$value" ]]; then
    value="$default_value"
  fi
  printf '%s' "$value"
}

parse_reset_seconds() {
  local raw
  raw="$(get_tmux_opt "@codexbar_reset_view_seconds" "3")"

  if [[ ! "$raw" =~ ^-?[0-9]+$ ]]; then
    raw="3"
  fi

  local n
  n="$raw"

  if (( n < 0 )); then
    n=0
  fi

  if (( n > 3600 )); then
    n=3600
  fi

  printf '%s' "$n"
}

gen_nonce() {
  if command -v uuidgen >/dev/null 2>&1; then
    uuidgen
    return 0
  fi

  local candidate
  candidate="$(date +%s%N 2>/dev/null || true)"
  if [[ -n "$candidate" && "$candidate" != *N* && "$candidate" =~ ^[0-9]+$ ]]; then
    printf '%s' "$candidate"
    return 0
  fi

  date +%s
}

main() {
  local seconds script_dir status_script
  seconds="$(parse_reset_seconds)"
  script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
  status_script="$script_dir/codexbar-usage-status.sh"

  if (( seconds == 0 )); then
    local current
    current="$(get_tmux_opt "@codexbar_reset_preview_until" "0")"

    if [[ "$current" == "-1" ]]; then
      tmux set-option -gq "@codexbar_reset_preview_until" "0" >/dev/null 2>&1 || true
    else
      tmux set-option -gq "@codexbar_reset_preview_until" "-1" >/dev/null 2>&1 || true
    fi

    bash "$status_script" --publish >/dev/null 2>&1 || true
    tmux refresh-client -S >/dev/null 2>&1 || true
    return 0
  fi

  local now until nonce
  now="$(date +%s)"
  until=$(( now + seconds ))
  nonce="$(gen_nonce)"

  tmux set-option -gq "@codexbar_reset_preview_until" "$until" >/dev/null 2>&1 || true
  tmux set-option -gq "@codexbar_reset_preview_nonce" "$nonce" >/dev/null 2>&1 || true

  bash "$status_script" --publish >/dev/null 2>&1 || true
  tmux refresh-client -S >/dev/null 2>&1 || true

  tmux run-shell -b "sleep $seconds; n=\$(tmux show-option -gqv @codexbar_reset_preview_nonce 2>/dev/null); if [ \"\$n\" = \"$nonce\" ]; then bash \"$status_script\" --publish >/dev/null 2>&1 || true; tmux refresh-client -S >/dev/null 2>&1; fi; exit 0" >/dev/null 2>&1 || true

}

main "$@"
