#!/usr/bin/env bash
#
# CMD+SHIFT+R (wezterm) → prefix+K → this popup: fuzzy-pick a directory from
# zoxide's frecency list, then open a new claude tab at the end of the tab bar
# in it. Escape / ^C picks nothing and creates no tab.
#
# The tab is created from INSIDE the popup rather than by the key binding,
# because display-popup -E cannot hand a value back to tmux -- it can only
# report the command's exit status. tmux commands work fine in a popup, so the
# script does the new-window itself, targeting the session passed in as $1 (the
# popup is not "in" a window, so a bare {end} would be ambiguous).

set -euo pipefail

SELF="${BASH_SOURCE[0]}"

# Self-dispatched by fzf for the preview pane: the list carries ~-shortened
# paths for readability, so expand before listing.
if [[ "${1:-}" == "--preview" ]]; then
  dir="${2/#\~/$HOME}"
  if command -v eza >/dev/null 2>&1; then
    eza -1 --icons=always --color=always --group-directories-first -- "$dir" 2>/dev/null | head -200
  else
    ls -1p -- "$dir" 2>/dev/null | head -200
  fi
  exit 0
fi

SESSION="${1:-}"
PANE_PATH="${2:-$HOME}"

main() {
  for tool in fzf zoxide; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      tmux display-message "claude-dir-picker: $tool not found in PATH"
      exit 1
    fi
  done

  # Candidates, best first: this tab's cwd, $HOME (the old unconditional
  # destination of this key), then zoxide in frecency order. awk dedups keeping
  # the first — i.e. the highest-priority — copy, and the -d test drops zoxide
  # rows whose directory has since been deleted.
  local list
  list="$(
    {
      printf '%s\n' "$PANE_PATH" "$HOME"
      zoxide query -l 2>/dev/null || true
    } | awk 'NF && !seen[$0]++' \
      | while IFS= read -r d; do [[ -d "$d" ]] && printf '%s\n' "$d"; done \
      | sed "s|^$HOME\$|~|; s|^$HOME/|~/|"
  )"

  # --tiebreak=index keeps zoxide's frecency order among equally good matches;
  # fzf's default (length) would surface short irrelevant paths over the dir
  # actually lived in. --print-query lets an unlisted path be typed in full.
  local out status
  set +e
  out="$(printf '%s\n' "$list" | fzf \
    --reverse \
    --info=inline \
    --print-query \
    --tiebreak=index \
    --prompt='claude in: ' \
    --header='enter: new claude tab at end · esc: cancel' \
    --preview "'$SELF' --preview {}" \
    --preview-window=right:50%:nowrap:noinfo)"
  status=$?
  set -e

  # 0 = picked a row, 1 = no match but a query was typed. Anything else
  # (130 esc/^C) is a cancel: no tab.
  if [[ $status -ne 0 && $status -ne 1 ]]; then
    exit 0
  fi

  local query selection target
  query="$(printf '%s\n' "$out" | sed -n '1p')"
  selection="$(printf '%s\n' "$out" | sed -n '2p')"
  target="${selection:-$query}"
  [[ -n "$target" ]] || exit 0

  target="${target/#\~/$HOME}"
  if [[ ! -d "$target" ]]; then
    tmux display-message "claude-dir-picker: not a directory: $target"
    exit 0
  fi

  # Mirrors `bind K`'s old body. claude is NOT the window command (quitting it
  # would close the tab); ZSH_AUTOSTART tells .zshrc to launch it as a child of
  # the login shell, and -e scopes the variable to this window's first process.
  tmux new-window -a -t "${SESSION}:{end}" -c "$target" -e ZSH_AUTOSTART=claude
}

main "$@"
