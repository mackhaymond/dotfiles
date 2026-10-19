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

socket=""
pane="${TMUX_PANE:-}"
if [ -n "${CODEX_THREAD_ID:-}" ] && [ -x "$HOME/.local/bin/codex-terminal-owner" ] \
   && command -v jq >/dev/null 2>&1; then
    owner=$("$HOME/.local/bin/codex-terminal-owner" resolve "$CODEX_THREAD_ID" 2>/dev/null || true)
    if [ "$(jq -r '.status // empty' <<<"$owner" 2>/dev/null)" = bound ]; then
        p=$(jq -r '.pane // empty' <<<"$owner" 2>/dev/null)
        s=$(jq -r '.tmux_socket // empty' <<<"$owner" 2>/dev/null)
        if [ -n "$p" ] && [ -n "$s" ]; then pane="$p"; socket="$s"; fi
    fi
fi
tm() {
    if [ -n "$socket" ]; then command tmux -S "$socket" "$@"; else command tmux "$@"; fi
}
fail() {
    echo "agent-notify-user.sh: $*; Mack was NOT notified" >&2
    exit 1
}
if [ -z "$pane" ] || { [ -z "$socket" ] && [ -z "${TMUX:-}" ]; }; then
    fail "no tmux pane for this agent (TMUX_PANE unset)"
fi

# The flag belongs on the AGENT's window, the one with @agent_state. On this
# machine an agent's shell usually is not in it: native Bash is deny-listed
# and agents run commands through the pty MCP server, whose shells live in
# the hidden `agents` session - $TMUX_PANE is the pty's pane there. So:
#   1. $TMUX_PANE's window, if it is an agent window;
#   2. else the pty window's @pty_claude_pid (the pty server tags each window
#      with the agent that owns it) -> that pid's tty -> the pane on it;
#   3. else walk up from our parent: the first ancestor whose tty is an agent
#      window's pane (native Bash, a Codex tool shell).
# Anything else: exit 1, nothing set.
US=$'\x1f'
read -r win pstate ppid_tag < <(tm display-message -p -t "$pane" \
    "#{window_id} #{?#{@agent_state},1,0} #{@pty_claude_pid}" 2>/dev/null || true)
case "$win" in @*) ;; *) fail "pane $pane has no tmux window" ;; esac
if [ "$pstate" != 1 ]; then
    rows=$(tm list-panes -a -F "#{pane_tty}${US}#{window_id}${US}#{@agent_state}" 2>/dev/null || true)
    # agent_win_for_tty TTY: the agent window with a pane on /dev/TTY.
    agent_win_for_tty() {
        local t w s
        while IFS="$US" read -r t w s; do
            [ "$t" = "/dev/$1" ] && [ -n "$s" ] && { printf '%s' "$w"; return 0; }
        done <<<"$rows"
        return 1
    }
    found=""
    case "$ppid_tag" in
        ''|*[!0-9]*) ;;
        *) tty=$(ps -o tty= -p "$ppid_tag" 2>/dev/null | tr -d ' ')
           case "$tty" in ''|'??') ;; *) found=$(agent_win_for_tty "$tty") ;; esac ;;
    esac
    if [ -z "$found" ]; then
        p=$PPID
        for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16; do
            case "$p" in ''|0|1|*[!0-9]*) break ;; esac
            read -r pp tty < <(ps -o ppid=,tty= -p "$p" 2>/dev/null || true)
            case "$tty" in ''|'??') ;; *) found=$(agent_win_for_tty "$tty") && break ;; esac
            p=$pp
        done
    fi
    [ -n "$found" ] || fail "pane $pane is not in an agent window (no @agent_state), and no agent owns it"
    win=$found
fi

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
if ! tm set-option -w -t "$win" @agent_for_user "$now $r" 2>/dev/null; then
    echo "agent-notify-user.sh: could not set @agent_for_user on $win; Mack was NOT notified" >&2
    exit 1
fi
echo "Flagged for Mack: your finish will notify him ($r)"
