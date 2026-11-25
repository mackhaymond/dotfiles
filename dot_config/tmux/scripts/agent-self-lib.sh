# agent-self-lib.sh: "which agent window am I running for?", the ONE
# implementation. Sourced by agent-notify-user.sh and run by agent-spawn
# (~/.local/bin; spawn's parent, report's own window). bash 3.2+.
#
#   agent_self_window
#       Sets AGENT_SELF_WIN (window id), AGENT_SELF_PANE (pane id) and
#       AGENT_SELF_SOCKET (the tmux socket path, empty = tmux's default, i.e.
#       $TMUX) and returns 0; returns 1 with AGENT_SELF_ERR (one line, no
#       trailing period) when no agent window can be found.
#   agent_self_tm ARGS...   tmux on AGENT_SELF_SOCKET (or the default one)
#
# The pane starts as $TMUX_PANE (an agent's tool shell inherits it). A Codex
# tool shell under a shared app-server can inherit a stale one, so with
# $CODEX_THREAD_ID set the verified thread->pane binding (codex-terminal-owner)
# wins when it resolves, socket and all.
#
# The answer is the AGENT's window, the one with @agent_state. On this machine
# an agent's shell usually is not in it: native Bash is deny-listed and agents
# run commands through the pty MCP server, whose shells live in the hidden
# `agents` session - $TMUX_PANE is the pty's pane there. So:
#   1. $TMUX_PANE's window, if it is an agent window;
#   2. else the pty window's @pty_claude_pid (the pty server tags each window
#      with the agent that owns it) -> that pid's tty -> the pane on it;
#   3. else walk up from our parent ($PPID): the first ancestor whose tty is
#      an agent window's pane (native Bash, a Codex tool shell).
# In 2 and 3 the pane is the agent's own pane on that tty.

agent_self_tm() {
    if [ -n "${AGENT_SELF_SOCKET:-}" ]; then command tmux -S "$AGENT_SELF_SOCKET" "$@"; else command tmux "$@"; fi
}

# _agent_self_for_tty TTY ROWS: "<window> <pane>" of the agent window with a
# pane on /dev/TTY, from ROWS (list-panes: tty US window US state US pane).
_agent_self_for_tty() {
    local t w s p
    while IFS=$'\x1f' read -r t w s p; do
        [ "$t" = "/dev/$1" ] && [ -n "$s" ] && { printf '%s %s' "$w" "$p"; return 0; }
    done <<<"$2"
    return 1
}

agent_self_window() {
    AGENT_SELF_WIN="" AGENT_SELF_PANE="" AGENT_SELF_SOCKET="" AGENT_SELF_ERR=""
    local pane="${TMUX_PANE:-}" owner p s
    if [ -n "${CODEX_THREAD_ID:-}" ] && [ -x "$HOME/.local/bin/codex-terminal-owner" ] \
       && command -v jq >/dev/null 2>&1; then
        owner=$("$HOME/.local/bin/codex-terminal-owner" resolve "$CODEX_THREAD_ID" 2>/dev/null || true)
        if [ "$(jq -r '.status // empty' <<<"$owner" 2>/dev/null)" = bound ]; then
            p=$(jq -r '.pane // empty' <<<"$owner" 2>/dev/null)
            s=$(jq -r '.tmux_socket // empty' <<<"$owner" 2>/dev/null)
            if [ -n "$p" ] && [ -n "$s" ]; then pane="$p"; AGENT_SELF_SOCKET="$s"; fi
        fi
    fi
    if [ -z "$pane" ] || { [ -z "$AGENT_SELF_SOCKET" ] && [ -z "${TMUX:-}" ]; }; then
        AGENT_SELF_ERR="no tmux pane for this agent (TMUX_PANE unset)"
        return 1
    fi
    local US=$'\x1f' win pstate ppid_tag rows found="" tty pp
    read -r win pstate ppid_tag < <(agent_self_tm display-message -p -t "$pane" \
        "#{window_id} #{?#{@agent_state},1,0} #{@pty_claude_pid}" 2>/dev/null || true)
    case "$win" in @*) ;; *) AGENT_SELF_ERR="pane $pane has no tmux window"; return 1 ;; esac
    if [ "$pstate" != 1 ]; then
        rows=$(agent_self_tm list-panes -a -F "#{pane_tty}${US}#{window_id}${US}#{@agent_state}${US}#{pane_id}" 2>/dev/null || true)
        case "$ppid_tag" in
            ''|*[!0-9]*) ;;
            *) tty=$(ps -o tty= -p "$ppid_tag" 2>/dev/null | tr -d ' ')
               case "$tty" in ''|'??') ;; *) found=$(_agent_self_for_tty "$tty" "$rows") ;; esac ;;
        esac
        if [ -z "$found" ]; then
            p=$PPID
            for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16; do
                case "$p" in ''|0|1|*[!0-9]*) break ;; esac
                read -r pp tty < <(ps -o ppid=,tty= -p "$p" 2>/dev/null || true)
                case "$tty" in ''|'??') ;; *) found=$(_agent_self_for_tty "$tty" "$rows") && break ;; esac
                p=$pp
            done
        fi
        if [ -z "$found" ]; then
            AGENT_SELF_ERR="pane $pane is not in an agent window (no @agent_state), and no agent owns it"
            return 1
        fi
        win="${found%% *}" pane="${found#* }"
    fi
    AGENT_SELF_WIN="$win" AGENT_SELF_PANE="$pane"
    return 0
}
