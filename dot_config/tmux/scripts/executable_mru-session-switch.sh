#!/usr/bin/env bash

set -euo pipefail

CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

is_valid_session_name() {
  [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]]
}

main() {
  if ! command -v fzf >/dev/null 2>&1; then
    tmux display-message "mru-session-switch: fzf not found in PATH"
    exit 1
  fi

  local current_session
  current_session="$(tmux display-message -p '#S')"

  local fzf_command=(fzf --exit-0 --print-query --reverse)
  local preview_script="$CURRENT_DIR/preview_session.sh"
  if [[ "${PREVIEW_ENABLED:-0}" == "1" && -x "$preview_script" ]]; then
    fzf_command+=(--preview "$preview_script {}" --preview-window=right:60%)
  fi

  local list
  list="$({
    tmux list-sessions -F $'#{session_last_attached}\t#{session_name}' 2>/dev/null \
      | awk -F $'\t' -v cur="$current_session" '$2 != cur && $2 != "scratch"' \
      | sort -t $'\t' -k1,1nr \
      | cut -f2-
  } || true)"

  local out status
  set +e
  out="$(printf '%s\n' "$list" | "${fzf_command[@]}")"
  status=$?
  set -e

  if [[ $status -ne 0 && $status -ne 1 ]]; then
    exit 0
  fi

  local query selection target
  query="$(printf '%s\n' "$out" | sed -n '1p' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  selection="$(printf '%s\n' "$out" | sed -n '2p')"

  if [[ -n "${selection:-}" ]]; then
    target="$selection"
  else
    target="${query:-}"
  fi

  [[ -n "${target:-}" ]] || exit 0

  if [[ "${target:-}" == "scratch" ]]; then
    exit 0
  fi

  if ! is_valid_session_name "$target"; then
    tmux display-message "Invalid session name (allowed: A-Z a-z 0-9 . _ -): $target"
    exit 0
  fi

  if tmux has-session -t "$target" 2>/dev/null; then
    tmux switch-client -t "$target"
    exit 0
  fi

  tmux command-prompt -b -k -p "Create and go to [$target] session? [Y/n]" \
    "if-shell -F '#{m/r:^(Enter|C-m|y|Y)$,%1}' { new-session -d -s $target -c ~ ; switch-client -t $target } { display-message Cancelled }"
}

main "$@"
