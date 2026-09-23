#!/usr/bin/env bash
#
# CMD+SHIFT+R (wezterm) → prefix+K → this popup: fuzzy-pick a directory from
# zoxide's frecency list, then open a new claude tab at the end of the tab bar
# in it. Escape / ^C picks nothing and creates no tab.
#
# The tab is created from INSIDE the popup rather than by the key binding,
# because display-popup -E cannot hand a value back to tmux -- it can only
# report the command's exit status.
#
# Which session/tab to aim at is discovered here, NOT passed in by the binding:
# display-popup format-expands its own arguments (-d, -T ...) but NOT the
# shell-command, so a '#{session_name}' written into the command string arrives
# at the script as those 14 literal characters and every target built from it
# silently fails. A bare `display-message -p` inside the popup resolves against
# the invoking client, which is exactly the session and pane we want. (The
# popup is not itself a pane -- $TMUX_PANE is empty in here -- so there is
# nothing to pass to -t either.)

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

# stderr is thrown away with the popup, so anything worth seeing goes to the
# status line of the session underneath it.
die() {
  tmux display-message "claude-dir-picker: $*"
  exit 1
}

main() {
  for tool in fzf zoxide; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool not found in PATH"
  done

  local session pane_path
  session="$(tmux display-message -p '#{session_name}')" || die "not inside tmux"
  [[ -n "$session" ]] || die "could not resolve the session name"
  pane_path="$(tmux display-message -p '#{pane_current_path}')"
  [[ -d "$pane_path" ]] || pane_path="$HOME"

  # Candidates, best first: $HOME (always the top row, so enter alone keeps
  # the old unconditional destination of this key), this tab's cwd, then
  # zoxide in frecency order. awk dedups keeping the first — i.e. the
  # highest-priority — copy, and the -d test drops zoxide rows whose directory
  # has since been deleted.
  local list
  list="$(
    {
      printf '%s\n' "$HOME" "$pane_path"
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
  [[ -d "$target" ]] || die "not a directory: $target"

  # Mirrors `bind K`'s old body. claude is NOT the window command (quitting it
  # would close the tab); ZSH_AUTOSTART tells .zshrc to launch it as a child of
  # the login shell, and -e scopes the variable to this window's first process.
  tmux new-window -a -t "${session}:{end}" -c "$target" -e ZSH_AUTOSTART=claude \
    || die "could not open a tab in $target"
}

main "$@"
