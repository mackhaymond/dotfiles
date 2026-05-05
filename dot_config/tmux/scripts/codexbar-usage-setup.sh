#!/usr/bin/env bash
set -euo pipefail

get_tmux_opt() {
  local opt_name="${1:?}"
  local default_value="${2-}"

  if tmux show-option -gq "$opt_name" >/dev/null 2>&1; then
    tmux show-option -gqv "$opt_name" 2>/dev/null || true
    return 0
  fi

  printf '%s' "$default_value"
}

main() {
  local home
  home="$(tmux display-message -p "#{HOME}" 2>/dev/null || true)"
  if [[ -z "$home" ]]; then
    home="${HOME:-}"
  fi

  local desired_key
  desired_key="$(get_tmux_opt "@codexbar_reset_key" "u")"

  local prev_key
  prev_key="$(get_tmux_opt "@codexbar__bound_reset_key" "")"

  if [[ -z "$desired_key" ]]; then
    if [[ -n "$prev_key" ]]; then
      tmux unbind-key -T prefix "$prev_key" 2>/dev/null || true
    fi

    tmux set-option -gq "@codexbar__bound_reset_key" "" >/dev/null 2>&1 || true
    return 0
  fi

  if [[ -n "$prev_key" && "$prev_key" != "$desired_key" ]]; then
    tmux unbind-key -T prefix "$prev_key" 2>/dev/null || true
  fi

  tmux bind-key -T prefix "$desired_key" run-shell "bash \"$home/.config/tmux/scripts/codexbar-usage-key.sh\"" >/dev/null 2>&1 || true
  tmux set-option -gq "@codexbar__bound_reset_key" "$desired_key" >/dev/null 2>&1 || true
}

main "$@"
