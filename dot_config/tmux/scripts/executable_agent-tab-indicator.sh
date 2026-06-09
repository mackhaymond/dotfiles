#!/usr/bin/env bash
# Tint the tmux tab (window) of the calling pane based on AI agent state.
#
# Catppuccin builds window-status-format with explicit inline #[fg=,bg=] for
# every tab segment, which overrides window-status-style — so the usual
# style-based tinting (e.g. tmux-agent-indicator) is invisible here. Instead we
# stash the state in a per-window user option @agent_state that Catppuccin's
# window-text format reads (see @catppuccin_window_*_text in tmux.conf) and
# recolors the tab accordingly.
#
# Invoked from Claude Code hooks (UserPromptSubmit/PermissionRequest/Stop). The
# hook process is a child of `claude`, so it inherits TMUX/TMUX_PANE from the
# pane Claude runs in.
#
# Usage: agent-tab-indicator.sh <running|needs-input|done|off|clear-current>
#   running/off  -> clear the tab tint
#   needs-input  -> yellow tab (Claude is waiting on you)
#   done         -> green tab  (Claude finished)
#   clear-current-> clear the tab the user just focused (after-select-window hook)

set -euo pipefail

command -v tmux >/dev/null 2>&1 || exit 0
[ -n "${TMUX:-}" ] || exit 0

state="${1:-}"

# Focus hook: the user navigated to a tab, so they've seen it — clear its tint.
if [ "$state" = "clear-current" ]; then
    win=$(tmux display-message -p '#{window_id}' 2>/dev/null || true)
    [ -n "$win" ] && tmux set-option -uw -t "$win" @agent_state 2>/dev/null || true
    exit 0
fi

# Resolve the window of the pane that triggered the hook.
pane="${TMUX_PANE:-}"
if [ -n "$pane" ]; then
    win=$(tmux display-message -p -t "$pane" '#{window_id}' 2>/dev/null || true)
else
    win=$(tmux display-message -p '#{window_id}' 2>/dev/null || true)
fi
[ -n "$win" ] || exit 0

case "$state" in
    needs-input|done)
        # Only alert on background tabs; if the user is already looking at this
        # window there's nothing to flag.
        active_win=$(tmux display-message -p '#{window_id}' 2>/dev/null || true)
        if [ "$win" = "$active_win" ]; then
            tmux set-option -uw -t "$win" @agent_state 2>/dev/null || true
        else
            tmux set-option -w -t "$win" @agent_state "$state"
        fi
        ;;
    *)
        # running / off / anything else -> clear.
        tmux set-option -uw -t "$win" @agent_state 2>/dev/null || true
        ;;
esac

tmux refresh-client -S 2>/dev/null || true
