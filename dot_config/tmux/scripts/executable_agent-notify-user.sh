#!/usr/bin/env bash
# agent-notify-user.sh "<reason>" - flag Mack from inside an agent's turn.
#
# The CLI twin of tmux-mcp's `notify_user` tool (AGENT-ORIGINATED TURNS in
# docs/agent-tab-indicator.md). A turn started by another agent finishes
# QUIET - no green tab, no notch glow - unless the agent flags that its
# result needs Mack himself. This sets, on the calling agent's own window:
#   @agent_for_user "<epoch_s> <reason>"
# and agent-tab-indicator.sh's Stop then paints a normal loud `done` with the
# reason as the sidebar detail. The flag is cleared when the next
# agent-originated turn starts.
#
# The window is the one holding $TMUX_PANE (an agent's tool shell inherits
# it). A Codex tool shell under a shared app-server can inherit a stale one,
# so with $CODEX_THREAD_ID set the verified thread->pane binding
# (codex-terminal-owner) wins when it resolves.
#
# Reason: one line (CR/LF/tab -> space), no control characters, no `|` or
# `#`, whitespace collapsed, trailing `;` dropped (an argv element ending in
# `;` ends a tmux command list), at most 200 characters.
# Exit 0 on success; 2 = usage; 1 = the caller's window could not be found.
set -uo pipefail

reason="$*"
if [ -z "${reason//[[:space:]]/}" ]; then
    echo "usage: agent-notify-user.sh \"<one-line reason Mack must look at this>\"" >&2
    exit 2
fi

# Character-wise slicing below wants a UTF-8 locale (bash counts bytes in C).
export LC_ALL="${AGENT_NOTIFY_LOCALE:-en_US.UTF-8}"

fail() {
    echo "agent-notify-user.sh: $*; Mack was NOT notified" >&2
    exit 1
}

# The flag belongs on the AGENT's window, the one with @agent_state, which an
# agent's shell usually is not in (a pty-MCP shell, a Codex tool shell):
# agent_self_window (agent-self-lib.sh) finds it - $TMUX_PANE's own window,
# the pty window's @pty_claude_pid, or the parent-process walk, with the Codex
# thread binding's pane and socket first. Anything else: exit 1, nothing set.
# shellcheck source=agent-self-lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/agent-self-lib.sh" 2>/dev/null \
    || fail "agent-self-lib.sh is missing next to this script"
agent_self_window || fail "$AGENT_SELF_ERR"
win="$AGENT_SELF_WIN"

r="${reason//[$'\r\n\t']/ }"
r=$(printf '%s' "$r" | LC_ALL=C tr -d '\000-\037\177|#')
while [[ $r == *"  "* ]]; do r="${r//  / }"; done
r="${r# }"
r="${r:0:200}"
while :; do
    case "$r" in *" "|*";") r="${r%?}" ;; *) break ;; esac
done
[ -n "$r" ] || r="needs your attention"

printf -v now '%(%s)T' -1
if ! agent_self_tm set-option -w -t "$win" @agent_for_user "$now $r" 2>/dev/null; then
    echo "agent-notify-user.sh: could not set @agent_for_user on $win; Mack was NOT notified" >&2
    exit 1
fi
echo "Flagged for Mack: your finish will notify him ($r)"
