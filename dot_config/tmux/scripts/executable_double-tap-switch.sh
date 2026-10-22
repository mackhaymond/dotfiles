#!/usr/bin/env bash
# Double-tap hyper or right shift in WezTerm → flip to the previous tmux tab,
# session or window, the way the same double-tap flips Arc tabs (Karabiner
# sends Ctrl+Tab there). Karabiner runs this (.chezmoitemplates/karabiner-base.json),
# so there is no $TMUX: the target is the focused client. Which flip is
# `@double_tap_target` (tmux.conf; unset = global):
#   global   the tab this client was on before this one, in ANY space: agent,
#            shell or home, never a hidden session. Pressed again, back: a true
#            A<->B flip. It is `nav.sh last`, Option-X's history (docs/nav.md),
#            which skips closed and parked tabs.
#   session  the most recently attached other session — the top row of prefix+a
#   window   last-window in the focused client's session (same space only)
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

# Messages go through notify.sh: a toast in the agent sidebar when one is open
# for the client, else tmux's own display-message (its header has the rest).
# NOTIFY_SH names another (tests); beside this script it is notify.sh
# (executable_notify.sh in the chezmoi source); with none, display-message.
#   notify_user [-c CLIENT] [-k info|ok|warn|error] [-t SECONDS] [--] TEXT
notify_user() {
  local n="${NOTIFY_SH:-}" d="${BASH_SOURCE[0]%/*}" c=
  [ "$d" = "${BASH_SOURCE[0]}" ] && d=.
  if [ -z "$n" ]; then
    for n in "$d/notify.sh" "$d/executable_notify.sh" "$HOME/.config/tmux/scripts/notify.sh"; do
      [ -f "$n" ] && break
    done
  fi
  if [ -x "$n" ]; then "$n" "$@"
  elif [ -f "$n" ]; then zsh -f "$n" "$@"
  else
    while [ $# -gt 0 ]; do
      case "$1" in
        -c|-k|-t) [ "$1" = -c ] && c="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --) shift; break ;;
        *) break ;;
      esac
    done
    ${NOTIFY_TMUX:-tmux} display-message ${c:+-c "$c"} -- "$*" 2>/dev/null
  fi
  return 0
}
# A UTF-8 locale for it: in Karabiner's bare env tmux would print notify.sh's
# separator byte as `_` and it could never find the client's sidebar.
say() { LC_ALL= LC_CTYPE=en_US.UTF-8 notify_user -c "$client" -k warn -- "$1"; }

# display-message: -t <client> resolves formats against that client's session;
# say's -c <client> shows the message on that client.
case "$(tmux show -gv @double_tap_target 2>/dev/null)" in
  window)
    session="$(tmux display-message -t "$client" -p '#{session_id}')"
    tmux last-window -t "$session" 2>/dev/null ||
      say "No previous window"
    ;;
  session)
    current="$(tmux display-message -t "$client" -p '#S')"
    # Same list and order as mru-session-switch.sh, minus the picker. Space-separated:
    # in Karabiner's bare env (no LANG) tmux prints a tab as `_`. The name is the
    # rest of the line, so a name with spaces still compares whole.
    target="$(tmux list-sessions -F '#{session_last_attached} #{session_name}' |
      awk -v cur="$current" '{ n = substr($0, index($0, " ") + 1) }
        n != cur && n != "scratch" && n != "agents" && n != "tasks" && n != "stash"' |
      sort -k1,1nr | head -1 | cut -d' ' -f2-)"
    if [[ -n "$target" ]]; then
      tmux switch-client -c "$client" -t "=$target"
    else
      say "No other session"
    fi
    ;;
  *)
    # nav.sh beside this script (executable_nav.sh in the chezmoi source). It
    # keys the history by client name, which is what $client is; it sets its
    # own UTF-8 locale and says "no previous tab" itself. $BASH, the bash
    # running this: Karabiner's PATH may not hold the one tmux's would.
    d="${BASH_SOURCE[0]%/*}"
    [ "$d" = "${BASH_SOURCE[0]}" ] && d=.
    for nav in "$d/nav.sh" "$d/executable_nav.sh" "$HOME/.config/tmux/scripts/nav.sh"; do
      [ -f "$nav" ] && break
    done
    if [ -f "$nav" ]; then
      "${BASH:-bash}" "$nav" last "$client"
    else
      say "double-tap: nav.sh not found"
    fi
    ;;
esac
