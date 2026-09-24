#!/usr/bin/env bash
# Double-tap hyper or right shift in WezTerm → flip to the previous tmux session or
# window, the way the same double-tap flips Arc tabs (BTT sends Ctrl+Tab there).
# Karabiner runs this (.chezmoitemplates/karabiner-base.json), so there is no $TMUX:
# the target is the focused client. Which flip is `@double_tap_target` (tmux.conf):
#   session  the most recently attached other session — the top row of prefix+a
#   window   last-window in the focused client's session
# Switch live: tmux set -g @double_tap_target window

set -uo pipefail

# The focused client; failing that (no focus event seen yet), the one typed in last.
# Never fall back to tmux's own no-$TMUX defaults: display-message resolves those to
# the most recently *active session* (often `agents`), not the client switch-client
# would move, so the flip became a no-op or hit the agents session's windows.
client="$(tmux list-clients -F '#{client_activity} #{client_flags} #{client_name}' 2>/dev/null |
  awk '$2 !~ /(^|,)control-mode(,|$)/ { print ($2 ~ /(^|,)focused(,|$)/), $1, $3 }' |
  sort -k1,1nr -k2,2nr | head -1 | cut -d' ' -f3-)"
[[ -n "$client" ]] || exit 0

# display-message: -t <client> resolves formats against that client's session,
# -c <client> shows the message on that client.
case "$(tmux show -gv @double_tap_target 2>/dev/null)" in
  window)
    session="$(tmux display-message -t "$client" -p '#{session_id}')"
    tmux last-window -t "$session" 2>/dev/null ||
      tmux display-message -c "$client" "No previous window"
    ;;
  *)
    current="$(tmux display-message -t "$client" -p '#S')"
    # Same list and order as mru-session-switch.sh, minus the picker. Space-separated:
    # in Karabiner's bare env (no LANG) tmux prints a tab as `_`. The name is the
    # rest of the line, so a name with spaces still compares whole.
    target="$(tmux list-sessions -F '#{session_last_attached} #{session_name}' |
      awk -v cur="$current" '{ n = substr($0, index($0, " ") + 1) }
        n != cur && n != "scratch" && n != "agents" && n != "stash"' |
      sort -k1,1nr | head -1 | cut -d' ' -f2-)"
    if [[ -n "$target" ]]; then
      tmux switch-client -c "$client" -t "=$target"
    else
      tmux display-message -c "$client" "No other session"
    fi
    ;;
esac
