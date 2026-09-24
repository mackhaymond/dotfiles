#!/usr/bin/env bash
# Double-tap hyper or right shift in WezTerm → flip to the previous tmux session or
# window, the way the same double-tap flips Arc tabs (BTT sends Ctrl+Tab there).
# Karabiner runs this (.chezmoitemplates/karabiner-base.json), so there is no $TMUX:
# the target is the focused client. Which flip is `@double_tap_target` (tmux.conf):
#   session  the most recently attached other session — the top row of prefix+a
#   window   last-window in the focused client's session
# Switch live: tmux set -g @double_tap_target window

set -uo pipefail

client="$(tmux list-clients -F '#{client_flags} #{client_name}' 2>/dev/null |
  awk '$1 ~ /(^|,)focused(,|$)/ { print $2; exit }')"
cflag=(); [[ -n "$client" ]] && cflag=(-c "$client")
# `-t <client>` makes display-message resolve formats against that client.
tflag=(); [[ -n "$client" ]] && tflag=(-t "$client")

case "$(tmux show -gv @double_tap_target 2>/dev/null)" in
  window)
    session="$(tmux display-message "${tflag[@]}" -p '#{session_id}')"
    tmux last-window -t "$session" 2>/dev/null ||
      tmux display-message "${tflag[@]}" "No previous window"
    ;;
  *)
    current="$(tmux display-message "${tflag[@]}" -p '#S')"
    # Same list and order as mru-session-switch.sh, minus the picker. Space-separated:
    # in Karabiner's bare env (no LANG) tmux prints a tab as `_`.
    target="$(tmux list-sessions -F '#{session_last_attached} #{session_name}' |
      awk -v cur="$current" '$2 != cur && $2 != "scratch" && $2 != "agents" && $2 != "stash"' |
      sort -k1,1nr | head -1 | cut -d' ' -f2-)"
    if [[ -n "$target" ]]; then
      tmux switch-client "${cflag[@]}" -t "=$target"
    else
      tmux display-message "${tflag[@]}" "No other session"
    fi
    ;;
esac
