#!/usr/bin/env bash
# Agent tab indicator v2 — drive per-window tmux options from AI-agent
# lifecycle hooks so the Catppuccin tab bar reflects agent state.
#
# Options written (window scope; unset = no agent in window):
#   @agent_state    idle | running | needs-input | done
#   @agent_summary  "<project>/<ultra-short title>" shown as the tab name;
#                   project = basename of the agent's cwd, title = the
#                   conversation title condensed to its 2-4 identifying
#                   words by a cached background copilot/haiku call
#                   (interim: the raw title until the condensation lands)
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
#                                      permission prompt), but ONLY from
#                                      running/needs-input so a late tool call
#                                      can't resurrect a finished tab; skips
#                                      stdin entirely — payloads can be huge
#   needs-input  PermissionRequest / Notification(permission_prompt|idle_prompt)
#                / StopFailure       → attention; always asserted (focus ≠
#                                      answer), discharged by the focus hook
#   done         Stop                → turn finished; refresh @agent_summary;
#                                      not tinted if a client is watching it
#   clear        SessionEnd          → remove state (skipped for clear/resume,
#                                      which are followed by a new SessionStart)
#   clear-current  focus hook        → attention states → idle once seen
#
# "Seen-it" semantics: a tinted tab discharges to idle when you focus it
# (clear-current); `done` additionally isn't tinted if its window is already
# being watched. needs-input always tints so a prompt is never lost.

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
    # must not churn the status line. Writes are best-effort: the window can
    # close between the read and the write, and a failed write must not make
    # the hook exit nonzero (codex treats hook exit status as a gate).
    local win="$1" new="$2" cur
    cur=$(window_state "$win")
    [ "$cur" = "$new" ] && return 0
    tmux set-option -w -t "$win" @agent_state "$new" 2>/dev/null || true
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
    # lands in window-status-format via #{@agent_summary}; '#' starts a
    # format/style token and '%' is a strftime metacharacter under #{T:…}, so
    # strip both defensively even though the current render path is bare.
    tr '\n\t' '  ' | tr -d '#"%' | sed -e 's/  */ /g' -e 's/^ //' -e 's/ $//' | cut -c1-60
}

set_summary() {
    local win="$1" summary="$2" cur
    [ -n "$summary" ] || return 0
    cur=$(tmux show-options -wqv -t "$win" @agent_summary 2>/dev/null || true)
    [ "$cur" = "$summary" ] && return 0
    tmux set-option -w -t "$win" @agent_summary "$summary" 2>/dev/null || true
    tmux refresh-client -S 2>/dev/null || true
}

# --- summary composition: @agent_summary = "<project>/<ultra-short title>" -

# Cache rows are TAB-separated: <key> <short> <epoch>. A row with an empty
# <short> is a NEGATIVE entry (a failed/garbage condense) used to back off
# retries for NEG_TTL seconds instead of re-calling the model every turn.
CACHE="$HOME/.cache/agent-tab/titles.tsv"
NEG_TTL=600

now_epoch() { date +%s 2>/dev/null || echo 0; }

title_key() {
    printf '%s' "$1" | /usr/bin/shasum -a 256 | cut -c1-16
}

# Last non-empty short for a title (skips negative rows; last writer wins).
cached_short() {
    [ -f "$CACHE" ] || return 0
    awk -F'\t' -v k="$(title_key "$1")" \
        '$1==k && $2!=""{v=$2} END{if(v!="")print v}' "$CACHE" 2>/dev/null || true
}

# True if the most recent row for this key is a negative within NEG_TTL —
# i.e. we failed to condense recently and shouldn't retry the model yet.
negative_fresh() {
    [ -f "$CACHE" ] || return 1
    awk -F'\t' -v k="$1" -v now="$(now_epoch)" -v ttl="$NEG_TTL" \
        '$1==k{s=$2;ts=$3} END{exit !(s=="" && ts!="" && (now-ts)<ttl)}' \
        "$CACHE" 2>/dev/null
}

cache_put() {
    mkdir -p "$(dirname "$CACHE")" 2>/dev/null || true
    printf '%s\t%s\t%s\n' "$1" "$2" "$(now_epoch)" >> "$CACHE" 2>/dev/null || true
}

# Trim to <=N chars on whole-word boundaries (never mid-word unless the first
# word alone is longer than N).
fit_words() {
    local s="$1" n="${2:-24}"
    while [ "${#s}" -gt "$n" ] && [ "${s% *}" != "$s" ]; do s="${s% *}"; done
    printf '%s' "$s" | cut -c1-"$n"
}

# Gate model output before caching: accept only title-like text (1-4 words,
# <=24 chars, has a letter, no colon / sentence-final punctuation, not an
# apology / refusal / auth-error / first-person reply). This is what stops
# copilot error strings ("Credit balance is too…") from poisoning the cache.
valid_short() {
    local s="$1" lc wc
    [ -n "$s" ] || return 1
    [ "${#s}" -le 24 ] || return 1
    case "$s" in *:*|*.|*!|*\?) return 1 ;; esac
    case "$s" in *[A-Za-z]*) ;; *) return 1 ;; esac
    wc=$(printf '%s' "$s" | wc -w | tr -d ' ')
    [ "$wc" -ge 1 ] && [ "$wc" -le 4 ] || return 1
    lc=$(printf '%s' "$s" | tr '[:upper:]' '[:lower:]')
    case "$lc" in
        "i "*|"i'"*|*sorry*|*"credit balance"*|*"i cannot"*|*"i can't"* \
        |*"not authenticated"*|*"please "*|*login*|*error*|*unable*|*apolog*) return 1 ;;
    esac
    return 0
}

project_name() {
    # Basename of the hook payload's cwd (HOME → "~"), falling back to the
    # agent pane's current path.
    local payload="$1" cwd=""
    if [ -n "$JQ" ] && [ -n "$payload" ]; then
        cwd=$("$JQ" -r '.cwd // empty' <<<"$payload" 2>/dev/null || true)
    fi
    if [ -z "$cwd" ] && [ -n "${pane:-}" ]; then
        cwd=$(tmux display-message -p -t "$pane" '#{pane_current_path}' 2>/dev/null || true)
    fi
    [ -n "$cwd" ] || return 0
    if [ "$cwd" = "$HOME" ]; then
        printf '~'
    else
        basename "$cwd" | sanitize_summary | cut -c1-20
    fi
}

compose_summary() {
    # Set "<project>/<short>" immediately from cache when we've condensed
    # this title before; otherwise show "<project>/<raw>" as an interim and
    # condense in a detached child (an LLM call must never block a hook).
    local win="$1" raw="$2" payload="$3" proj short
    [ -n "$raw" ] || return 0
    proj=$(project_name "$payload")
    short=$(cached_short "$raw")
    if [ -n "$short" ]; then
        set_summary "$win" "${proj:+$proj/}$short"
        return 0
    fi
    set_summary "$win" "${proj:+$proj/}$(fit_words "$raw" 24)"
    command -v copilot >/dev/null 2>&1 || return 0
    ( nohup bash "$0" condense "$win" "$proj" "$raw" >/dev/null 2>&1 & ) 2>/dev/null || true
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
# The after-select-window hook passes the selected #{window_id} as $2 —
# an untargeted display-message resolves to the most-recently-active
# CLIENT, which can be a different one when several clients are attached.
if [ "$mode" = "clear-current" ]; then
    win="${2:-}"
    if [ -z "$win" ]; then
        win=$(tmux display-message -p '#{window_id}' 2>/dev/null || true)
    fi
    [ -n "$win" ] || exit 0
    case "$(window_state "$win")" in
        needs-input|done) set_state "$win" idle ;;
    esac
    exit 0
fi

# Detached condenser (spawned by compose_summary): distill the raw title to
# its 2-4 identifying words with a one-shot copilot/haiku call, cache it,
# update the tab. argv: condense <window_id> <project> <raw-title>. Never
# invoked by hooks directly, so a slow/failed model call only delays the
# title swap.
if [ "$mode" = "condense" ]; then
    win="${2:-}"; proj="${3:-}"; raw="${4:-}"
    { [ -n "$win" ] && [ -n "$raw" ]; } || exit 0
    key=$(title_key "$raw")
    lock="${TMPDIR:-/tmp}/agent-tab-condense.$key.lock"
    mkdir "$lock" 2>/dev/null || exit 0   # another condenser owns this title
    trap 'rmdir "$lock" 2>/dev/null' EXIT INT TERM

    short=$(cached_short "$raw")
    if [ -z "$short" ]; then
        # Back off if a recent attempt for this title failed, so a persistent
        # copilot error (not logged in, out of credit) isn't re-run every turn.
        negative_fresh "$key" && exit 0

        # gtimeout is coreutils' name when /usr/bin/timeout is absent (macOS).
        TO=$(command -v timeout || command -v gtimeout || true)
        # Copilot prints the answer on stdout, stats on stderr; a pure text
        # prompt grants no tool permissions. Capture the exit code explicitly
        # — `|| true` would mask a failed call whose stdout is an error string.
        if out=$(${TO:+"$TO" 90} copilot -p \
            "Condense this coding-session title into the 2-4 words that best identify it. Output only those words - no punctuation, quotes, or explanation.

Title: $raw" --model claude-haiku-4.5 --no-color </dev/null 2>/dev/null); then
            short=$(printf '%s' "$out" | grep -m1 . | sanitize_summary | cut -d' ' -f1-4)
            short=$(fit_words "$short" 24)
            valid_short "$short" || short=""
        else
            short=""
        fi

        if [ -n "$short" ]; then
            cache_put "$key" "$short"
        else
            cache_put "$key" ""   # negative entry → NEG_TTL backoff
            exit 0
        fi
    fi
    [ -n "$short" ] || exit 0
    set_summary "$win" "${proj:+$proj/}$short"
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

# Heartbeat is the hot path (every tool call): don't wait on stdin — the
# payload includes tool_response, which can be megabytes. A backgrounded
# drain consumes the pipe so the writer never sees EPIPE (codex's tolerance
# for a hook that abandons its stdin is undocumented), without blocking us.
# Only re-arm running from running/needs-input — a late PostToolUse landing
# after Stop's `done` (or a subagent tool firing past the turn boundary, which
# bypasses the agent_id guard below) must NOT resurrect a finished tab.
if [ "$mode" = "heartbeat" ]; then
    ( cat >/dev/null 2>&1 & ) 2>/dev/null
    case "$(window_state "$win")" in
        running|needs-input) set_state "$win" running ;;
    esac
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

# Is the agent's own window currently being viewed by ≥1 client? window
# scope, so it's robust to multiple attached clients AND to detached sessions
# (an untargeted display-message would resolve to some other client's window;
# #{window_active} is 1 even for zero-client sessions — neither is correct).
watched=$(tmux display-message -p -t "$win" '#{window_active_clients}' 2>/dev/null || true)
case "$watched" in ''|*[!0-9]*) watched=0 ;; esac

case "$mode" in
    idle)
        # SessionStart with source=compact fires mid-turn after auto-compaction;
        # don't downgrade a running turn.
        src=""
        if [ -n "$JQ" ] && [ -n "$payload" ]; then
            src=$("$JQ" -r '.source // empty' <<<"$payload" 2>/dev/null || true)
            [ "$src" = "compact" ] && exit 0
        fi
        set_state "$win" idle
        summary=$(extract_summary "$payload")
        if [ -n "$summary" ]; then
            compose_summary "$win" "$summary" "$payload"
        else
            # A fresh conversation (/clear, new startup) has no title yet —
            # drop the previous conversation's stale title rather than keep
            # rendering it on the new session's tab. resume keeps it: the
            # old title is still the right one for a resumed conversation.
            case "$src" in
                clear|startup) tmux set-option -uw -t "$win" @agent_summary 2>/dev/null || true ;;
            esac
        fi
        ;;
    running)
        set_state "$win" running
        compose_summary "$win" "$(extract_summary "$payload")" "$payload"
        ;;
    needs-input)
        # Always assert — focusing a window is not answering its prompt. The
        # focus hook (clear-current) discharges needs-input→idle once seen, so
        # a prompt is never silently lost by switching away.
        set_state "$win" needs-input
        ;;
    done)
        # Don't tint a window someone is actively watching — they saw it finish.
        if [ "$watched" -gt 0 ]; then
            set_state "$win" idle
        else
            set_state "$win" "done"
        fi
        compose_summary "$win" "$(extract_summary "$payload")" "$payload"
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
