#!/usr/bin/env bash
# Agent tab indicator v2 — drive per-window tmux options from AI-agent
# lifecycle hooks so the Catppuccin tab bar reflects agent state.
#
# Options written (window scope; unset = no agent in window):
#   @agent_state    idle | running | needs-input | done
#   @agent_summary  short conversation title shown as the tab name
#
# Rendering happens entirely in tmux.conf: the Catppuccin window formats
# read these options via #{?…} conditionals (background tint per state,
# glyph, summary-with-#W-fallback). Catppuccin bakes its window formats
# ONCE at load from GLOBAL options, so per-window @catppuccin_* overrides
# can't work — per-window state must flow through user options like these.
#
# Callers:
#   Claude Code hooks (~/.claude/settings.json) and Codex hooks
#   (~/.codex/hooks.json) invoke:  agent-tab-indicator.sh <mode> <agent>
#   with the hook's JSON payload on stdin. Hook processes are children of
#   the agent process, so TMUX/TMUX_PANE identify the agent's pane.
#
#   tmux after-select-window hook invokes:  agent-tab-indicator.sh clear-current
#   (no stdin, no TMUX_PANE → operates on the now-active window).
#
#   agent-tab-watcher.sh (companion daemon) seeds `idle` for hook-less
#   agents and garbage-collects state when the agent process dies — this
#   script never has to handle crashed/killed agents.
#
# Modes:
#   idle         SessionStart        → mark present (skipped for compact restarts)
#   running      UserPromptSubmit    → turn started; refresh @agent_summary
#   heartbeat    PostToolUse         → re-arm running mid-turn (after an answered
#                                      permission prompt); skips stdin entirely —
#                                      tool_response payloads can be huge
#   needs-input  PermissionRequest / Notification(permission_prompt|idle_prompt)
#                / StopFailure       → attention; downgraded to idle on the
#                                      active window (you're already looking)
#   done         Stop                → turn finished; refresh @agent_summary;
#                                      downgraded to idle on the active window
#   clear        SessionEnd          → remove state (skipped for clear/resume,
#                                      which are followed by a new SessionStart)
#   clear-current  focus hook        → attention states → idle once seen
#
# "Seen-it" semantics: needs-input/done only ever tint BACKGROUND tabs;
# focusing a window discharges its attention state to idle.

set -euo pipefail

command -v tmux >/dev/null 2>&1 || exit 0
[ -n "${TMUX:-}" ] || exit 0

mode="${1:-}"
agent="${2:-}"

JQ="$(command -v jq || true)"

# ---------------------------------------------------------------- helpers

window_state() {
    tmux show-options -wqv -t "$1" @agent_state 2>/dev/null || true
}

set_state() {
    # Write + redraw only on change; heartbeat fires on every tool call and
    # must not churn the status line.
    local win="$1" new="$2" cur
    cur=$(window_state "$win")
    [ "$cur" = "$new" ] && return 0
    tmux set-option -w -t "$win" @agent_state "$new"
    tmux refresh-client -S 2>/dev/null || true
}

clear_state() {
    local win="$1"
    local cur
    cur=$(window_state "$win")
    tmux set-option -uw -t "$win" @agent_state 2>/dev/null || true
    tmux set-option -uw -t "$win" @agent_summary 2>/dev/null || true
    if [ -n "$cur" ]; then
        tmux refresh-client -S 2>/dev/null || true
    fi
}

sanitize_summary() {
    # One line, no format-significant characters, bounded length. The value
    # lands inside window-status-format via #{@agent_summary}; '#' would be
    # parsed as a format/style token there.
    tr '\n\t' '  ' | tr -d '#"' | sed -e 's/  */ /g' -e 's/^ //' -e 's/ $//' | cut -c1-60
}

set_summary() {
    local win="$1" summary="$2" cur
    [ -n "$summary" ] || return 0
    cur=$(tmux show-options -wqv -t "$win" @agent_summary 2>/dev/null || true)
    [ "$cur" = "$summary" ] && return 0
    tmux set-option -w -t "$win" @agent_summary "$summary"
    tmux refresh-client -S 2>/dev/null || true
}

# Conversation title, best source first.
#   claude: latest ai-title entry near the transcript tail (cheap: last 64KB),
#           else the prompt that started the turn.
#   codex:  thread_name from ~/.codex/session_index.jsonl keyed by session_id,
#           else the prompt.
extract_summary() {
    local payload="$1" title=""
    [ -n "$JQ" ] || return 0
    [ -n "$payload" ] || return 0

    case "$agent" in
        claude)
            local transcript
            transcript=$("$JQ" -r '.transcript_path // empty' <<<"$payload" 2>/dev/null || true)
            if [ -n "$transcript" ] && [ -f "$transcript" ]; then
                title=$(tail -c 65536 "$transcript" 2>/dev/null \
                    | grep '"type":"ai-title"' | tail -1 \
                    | "$JQ" -r '.aiTitle // empty' 2>/dev/null || true)
            fi
            if [ -z "$title" ]; then
                title=$("$JQ" -r '.session_title // .prompt // empty' <<<"$payload" 2>/dev/null || true)
            fi
            ;;
        codex)
            local session_id index="$HOME/.codex/session_index.jsonl"
            session_id=$("$JQ" -r '.session_id // empty' <<<"$payload" 2>/dev/null || true)
            if [ -n "$session_id" ] && [ -f "$index" ]; then
                title=$(grep -F "\"$session_id\"" "$index" | tail -1 \
                    | "$JQ" -r '.thread_name // empty' 2>/dev/null || true)
            fi
            if [ -z "$title" ]; then
                title=$("$JQ" -r '.prompt // empty' <<<"$payload" 2>/dev/null || true)
            fi
            ;;
    esac

    printf '%s' "$title" | sanitize_summary
}

# ------------------------------------------------------------ entry modes

# Focus hook: attention discharged for the window the user just selected.
if [ "$mode" = "clear-current" ]; then
    win=$(tmux display-message -p '#{window_id}' 2>/dev/null || true)
    [ -n "$win" ] || exit 0
    case "$(window_state "$win")" in
        needs-input|done) set_state "$win" idle ;;
    esac
    exit 0
fi

# Everything below is an agent hook: resolve the agent's window from the
# pane the hook inherited.
pane="${TMUX_PANE:-}"
if [ -n "$pane" ]; then
    win=$(tmux display-message -p -t "$pane" '#{window_id}' 2>/dev/null || true)
else
    win=$(tmux display-message -p '#{window_id}' 2>/dev/null || true)
fi
[ -n "$win" ] || exit 0

# Heartbeat is the hot path (every tool call): no stdin read — the payload
# includes tool_response, which can be megabytes.
if [ "$mode" = "heartbeat" ]; then
    set_state "$win" running
    exit 0
fi

payload=$(cat 2>/dev/null || true)

# Hooks also fire inside subagent contexts (payload carries agent_id); a
# subagent's Stop/PermissionRequest must not flip the main agent's tab.
if [ -n "$JQ" ] && [ -n "$payload" ]; then
    if [ -n "$("$JQ" -r '.agent_id // empty' <<<"$payload" 2>/dev/null || true)" ]; then
        exit 0
    fi
fi

active_win=$(tmux display-message -p '#{window_id}' 2>/dev/null || true)

case "$mode" in
    idle)
        # SessionStart with source=compact fires mid-turn after auto-compaction;
        # don't downgrade a running turn.
        if [ -n "$JQ" ] && [ -n "$payload" ]; then
            src=$("$JQ" -r '.source // empty' <<<"$payload" 2>/dev/null || true)
            [ "$src" = "compact" ] && exit 0
        fi
        set_state "$win" idle
        set_summary "$win" "$(extract_summary "$payload")"
        ;;
    running)
        set_state "$win" running
        set_summary "$win" "$(extract_summary "$payload")"
        ;;
    needs-input)
        if [ "$win" = "$active_win" ]; then
            set_state "$win" idle
        else
            set_state "$win" needs-input
        fi
        ;;
    done)
        if [ "$win" = "$active_win" ]; then
            set_state "$win" idle
        else
            set_state "$win" done
        fi
        set_summary "$win" "$(extract_summary "$payload")"
        ;;
    clear)
        # SessionEnd reasons clear/resume are immediately followed by a new
        # SessionStart in the same pane — clearing would just flicker.
        if [ -n "$JQ" ] && [ -n "$payload" ]; then
            reason=$("$JQ" -r '.reason // empty' <<<"$payload" 2>/dev/null || true)
            case "$reason" in clear|resume) exit 0 ;; esac
        fi
        clear_state "$win"
        ;;
    *)
        echo "Usage: agent-tab-indicator.sh <idle|running|heartbeat|needs-input|done|clear|clear-current> [claude|codex]" >&2
        exit 1
        ;;
esac
