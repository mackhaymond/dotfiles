#!/usr/bin/env bash
# Reopen closed tabs, browser-style: a stack of recently closed panes, each
# remembered with its cwd, its place in the tab bar, and — if a claude / codex /
# opencode session was running in it — enough to resume that conversation.
#
#   closed-tabs.sh close <pane_id>             snapshot, push, then kill the pane
#   closed-tabs.sh reopen <session_id> <tty>   pop the newest and recreate it
#   closed-tabs.sh list                        print the stack, newest last
#
# Bound in tmux.conf: prefix x (CMD+W) closes through here, prefix X (CMD+Z)
# reopens. Only closes that go through `close` are remembered — a tab that ends
# because its shell exited never passes through a key binding, and by the time
# any tmux hook fires its process tree is gone.
#
# Agent detection and session-id resolution are NOT reimplemented here: they
# come from tmux-assistant-resurrect's save script, which is written to be
# sourced (its main() is guarded) and already knows every tool's quirks —
# claude's SessionStart state file, codex's session-tags.jsonl and thread DB,
# opencode's plugin file and SQLite fallback, and stripping resume flags out of
# the original command line. That is the same code that resumes these sessions
# after a reboot, so a reopened tab comes back exactly the way a resurrected
# one does.
#
# A reopened tab is a normal login zsh with the agent started through
# ZSH_AUTOSTART (see the end of .zshrc), same as a CMD+R tab: quitting the
# agent leaves a prompt instead of closing the tab.

set -uo pipefail

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/tmux-closed-tabs"
STACK="$STATE_DIR/stack.jsonl"
LOCK="$STATE_DIR/lock"
LOGFILE="$STATE_DIR/log"
MAX_ENTRIES=50

RESURRECT_SAVE="$HOME/.config/tmux/plugins/tmux-assistant-resurrect/scripts/save-assistant-sessions.sh"

mkdir -p "$STATE_DIR"

msg() { tmux display-message "$*" 2>/dev/null; }
log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOGFILE" 2>/dev/null; }

# mkdir is the atomic lock (macOS has no flock(1)). A lock older than 10s is a
# crashed holder; it is broken with rename(2), not rm+mkdir, so two waiters
# can't both win.
lock_acquire() {
    local i=0
    until mkdir "$LOCK" 2>/dev/null; do
        if [ $((i % 20)) -eq 19 ]; then
            local age=$(( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || date +%s) ))
            if [ "$age" -gt 10 ] && mv "$LOCK" "$LOCK.stale.$$" 2>/dev/null; then
                rm -rf "$LOCK.stale.$$"
                continue
            fi
        fi
        i=$((i + 1))
        [ "$i" -ge 200 ] && return 1
        sleep 0.05
    done
}
lock_release() { rmdir "$LOCK" 2>/dev/null; }

# Resolve the agent running in a pane, if any, into a resume command.
# Prints "<tool>\x1f<session_id>\x1f<pid>\x1f<cmd>" or nothing.
agent_resume() {
    local pane="$1" pane_pid="$2" cwd="$3"
    [ -r "$RESURRECT_SAVE" ] || return 0
    (
        # The save script turns on errexit/nounset when sourced; its functions
        # are written for that, and this subshell keeps it from leaking out.
        # shellcheck source=/dev/null
        source "$RESURRECT_SAVE" 2>/dev/null || exit 0
        set +e
        # Seed its per-process flag cache with its own fallback list. Otherwise
        # stripping the resume flags runs `claude --help` on every close, which
        # is ~300ms of the key feeling slow.
        _SESSION_FLAGS_claude="$SESSION_FLAGS_FALLBACK_claude"
        _SESSION_FLAGS_opencode="$SESSION_FLAGS_FALLBACK_opencode"
        local apid args tool
        apid=$(pane_has_assistant "$pane_pid") || exit 0
        args=$(ps -o args= -p "$apid" 2>/dev/null)
        tool=$(detect_tool "$args")
        [ -n "$tool" ] || exit 0

        PARTS_FILE=$(mktemp)
        emit_session "$pane" "$tool" "$apid" "$args" "$cwd" 1 0 >/dev/null 2>&1
        local sid="" cli_args="" model="" env_json="{}"
        IFS=$'\x1f' read -r sid cli_args model env_json < <(jq -r \
            '[.session_id // "", .cli_args // "", .model // "", (.env // {} | tojson)] | join("\u001f")' \
            "$PARTS_FILE" 2>/dev/null)
        rm -f "$PARTS_FILE"
        if [ -z "$sid" ]; then
            printf '%s\x1f\x1f%s\x1f\n' "$tool" "$apid"
            exit 0
        fi

        # Same command shape restore-assistant-sessions.sh builds.
        local q_args="" a
        set -f
        for a in $cli_args; do q_args+=" $(posix_quote "$a")"; done
        set +f
        local env_prefix="" var val
        for var in $(tmux show-option -gqv @assistant-resurrect-capture-env 2>/dev/null); do
            [[ "$var" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
            val=$(jq -r --arg k "$var" '.[$k] // empty' <<<"$env_json" 2>/dev/null)
            [ -n "$val" ] && env_prefix+="$var=$(posix_quote "$val") "
        done
        local cmd
        case "$tool" in
        claude)
            case "$cli_args" in *--model*) ;; *) [ -n "$model" ] && q_args+=" --model $(posix_quote "$model")" ;; esac
            cmd="command claude${q_args} --resume $(posix_quote "$sid")" ;;
        opencode) cmd="command opencode${q_args} -s $(posix_quote "$sid")" ;;
        codex)    cmd="command codex${q_args} resume $(posix_quote "$sid")" ;;
        esac
        printf '%s\x1f%s\x1f%s\x1f%s\n' "$tool" "$sid" "$apid" "${env_prefix}${cmd}"
    )
}

do_close() {
    local pane="${1:-}"
    # An empty -t resolves to the CURRENT pane, not to nothing.
    [ -n "$pane" ] || { msg "closed-tabs: no pane given"; return 1; }

    local info
    info=$(tmux display-message -p -t "$pane" \
        '#{pane_id}|#{pane_pid}|#{session_name}|#{window_index}|#{pane_current_path}|#{?#{n:#{@agent_summary}},#{@agent_summary},#{window_name}}' 2>/dev/null)
    # display-message against a dead target can succeed with an empty
    # expansion — test the expansion, not the exit status.
    local pane_id pane_pid sess widx cwd label
    IFS='|' read -r pane_id pane_pid sess widx cwd label <<<"$info"
    [ -n "$pane_id" ] || return 1

    local tool="" sid="" apid="" cmd=""
    local resolved; resolved=$(agent_resume "$pane_id" "$pane_pid" "$cwd")
    [ -n "$resolved" ] && IFS=$'\x1f' read -r tool sid apid cmd <<<"$resolved"

    local rec
    rec=$(jq -cn --arg ts "$(date +%s)" --arg sess "$sess" --arg widx "$widx" \
        --arg cwd "$cwd" \
        --arg label "$label" --arg tool "$tool" --arg sid "$sid" --arg cmd "$cmd" \
        '{ts: ($ts|tonumber), session: $sess, index: ($widx|tonumber), cwd: $cwd,
          label: $label, tool: $tool, session_id: $sid, cmd: $cmd}')

    # Pushed BEFORE anything dies: once the agent exits, claude deletes the
    # state files the session id was read from.
    if lock_acquire; then
        printf '%s\n' "$rec" >>"$STACK"
        tail -n "$MAX_ENTRIES" "$STACK" >"$STACK.tmp" && mv "$STACK.tmp" "$STACK"
        lock_release
    else
        # Refuse rather than close something we could not remember.
        msg "closed-tabs: stack busy — tab left open"
        return 1
    fi
    log "close $sess:$widx cwd=$cwd tool=${tool:--} sid=${sid:--}"
    if [ -n "$tool" ] && [ -z "$sid" ]; then
        msg "closed-tabs: couldn't find the $tool session id — reopening will give a plain shell"
    fi

    # SIGTERM first so the agent starts its own shutdown, then take the pane at
    # once. Waiting for it to exit is ~2.2s (claude reaps every MCP child
    # first) and buys nothing measurable: with and without the SIGTERM, all of
    # a claude's descendants were gone within 6s of the kill-pane.
    if [[ "$apid" =~ ^[0-9]+$ ]] && [ "$apid" -gt 1 ]; then
        kill -TERM "$apid" 2>/dev/null
    fi
    tmux kill-pane -t "$pane_id" 2>/dev/null
    return 0
}

do_reopen() {
    local client_sess="${1:-}" client_tty="${2:-}"
    local rec
    lock_acquire || { msg "closed-tabs: stack busy"; return 1; }
    rec=$(tail -n 1 "$STACK" 2>/dev/null)
    if [ -z "$rec" ]; then
        lock_release
        msg "closed-tabs: nothing to reopen"
        return 0
    fi
    sed -i '' '$d' "$STACK"
    lock_release

    local sess idx cwd cmd tool label
    sess=$(jq -r '.session' <<<"$rec"); idx=$(jq -r '.index' <<<"$rec")
    cwd=$(jq -r '.cwd' <<<"$rec");      cmd=$(jq -r '.cmd // empty' <<<"$rec")
    tool=$(jq -r '.tool // empty' <<<"$rec"); label=$(jq -r '.label // empty' <<<"$rec")

    # Back in its own session if that still exists, else wherever you are.
    if ! tmux has-session -t "=$sess" 2>/dev/null; then
        sess=$(tmux display-message -p -t "$client_sess" '#{session_name}' 2>/dev/null)
        [ -n "$sess" ] || { msg "closed-tabs: no session to reopen into"; return 1; }
    fi
    [ -d "$cwd" ] || cwd="$HOME"

    # Same slot it left: insert before whatever slid into its index (with
    # renumber-windows on, its right-hand neighbour), or at the end if the bar
    # is now shorter than that.
    local -a where
    if tmux list-windows -t "=$sess" -F '#{window_index}' 2>/dev/null | grep -qx "$idx"; then
        where=(-b -t "=$sess:$idx")
    else
        where=(-t "=$sess:")
    fi
    local -a autostart=()
    [ -n "$cmd" ] && autostart=(-e "ZSH_AUTOSTART=$cmd")

    local win
    win=$(tmux new-window -P -F '#{window_id}' "${where[@]}" -c "$cwd" "${autostart[@]}" 2>/dev/null) || {
        # Put it back rather than lose it.
        lock_acquire && { printf '%s\n' "$rec" >>"$STACK"; lock_release; }
        msg "closed-tabs: couldn't create the tab — kept it on the stack"
        return 1
    }
    [ -n "$client_tty" ] && tmux switch-client -c "$client_tty" -t "$win" 2>/dev/null
    log "reopen $sess cwd=$cwd tool=${tool:--} cmd=${cmd:--}"
    local left; left=$(grep -c . "$STACK" 2>/dev/null || echo 0)
    msg "reopened ${label:-tab}${tool:+ ($tool resumed)} · $left more"
}

do_list() {
    [ -s "$STACK" ] || { echo "(empty)"; return 0; }
    jq -r '"\(.ts | strflocaltime("%m-%d %H:%M"))  \(.session):\(.index)  \(.tool // "" | if . == "" then "-" else . end)\t\(.label)\t\(.cwd)"' "$STACK"
}

case "${1:-}" in
close)  shift; do_close "$@" ;;
reopen) shift; do_reopen "$@" ;;
list)   do_list ;;
*) echo "usage: $0 close <pane_id> | reopen <session_id> <client_tty> | list" >&2; exit 2 ;;
esac
