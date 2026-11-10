#!/usr/bin/env bash
# Reopen closed tabs. Every close is kept for a while (RETAIN_DAYS, at most
# MAX_ENTRIES) with its cwd, its place in the tab bar, a colour snapshot of the
# screen as it was, and — if a claude / codex / opencode / grok session was
# running in it — enough to resume that conversation.
#
#   closed-tabs.sh close <pane_id>                 snapshot, record, kill the pane
#   closed-tabs.sh close --ask-space <client> <pane_id>   the same, then ask-space
#   closed-tabs.sh ask-space <client> <session>    only home left? ask to close the space
#   closed-tabs.sh ask-space-yes <client> <sid>    that box's y: still only home? close it
#   closed-tabs.sh pick-popup <session_id> <tty>   fzf picker over the history
#   closed-tabs.sh reopen <session_id> <tty> [id…] reopen these (default: newest)
#   closed-tabs.sh forget <id…>                    drop entries from the history
#   closed-tabs.sh list                            print the history, newest first
#   closed-tabs.sh describe <id>                   label an entry (close runs this)
#   closed-tabs.sh summarize <tool> <sid> <cwd> <label> <screen>   print one
#
# After a close, a detached `describe` has a small model read the tab's
# transcript — or its screen, for a plain shell — and stores a one-line
# summary on the entry, shown on its line in the picker. The call goes through
# ai-exec: gpt-6-luna (codex exec) first, then claude haiku, then copilot, each
# only when the one before fails or is usage-limited. stash.sh labels parked
# tabs through `summarize` the same way.
#
# Bound in tmux.conf: prefix x (CMD+W, asks y/n) and prefix C-l (CMD+SHIFT+W,
# doesn't) close through here; prefix X (CMD+Z) opens the picker. Only closes
# that go through `close` are remembered — a tab that ends because its shell
# exited never passes through a key binding, and by the time any tmux hook
# fires its process tree is gone. The closes made by hand (prefix x / C-l,
# the agent-ui menu's x, prefix v's range close) also ask whether to close the
# space when only its home is left (ask_space below); no other close does.
#
# Storage, all under ~/.local/state/tmux-closed-tabs/:
#   history.jsonl        one JSON object per closed tab, oldest first
#   previews/<id>.ansi   `capture-pane -e` of the screen at close time
# Pruned on every close and every picker open. A snapshot is the visible screen
# only (tens of KB), so a full history is a few MB at most.
#
# Agent detection and session-id resolution are NOT reimplemented here: they
# come from tmux-assistant-resurrect's save script, which is written to be
# sourced (its main() is guarded) and already knows every tool's quirks —
# claude's SessionStart state file, codex's session-tags.jsonl and thread DB,
# opencode's plugin file and SQLite fallback, and stripping resume flags out of
# the original command line. That is the same code that resumes these sessions
# after a reboot, so a reopened tab comes back exactly the way a resurrected
# one does. Grok, which the plugin does not know, is handled here the way
# assistant-save.sh's grok post-pass handles it (grok_* below).
#
# A reopened tab is a normal login zsh with the agent started through
# ZSH_AUTOSTART (see the end of .zshrc), same as a CMD+R tab: quitting the
# agent leaves a prompt instead of closing the tab.

set -uo pipefail

# Re-entry (popup, preview, hand-offs through `tmux run-shell`) goes through
# SELF with STATE_HOME carried along explicitly: commands tmux runs get the
# SERVER's environment, not this process's. With the installed path and an
# inherited XDG_STATE_HOME, a scratch copy pointed at a test history handed its
# reopen to the installed script, which reopened the REAL history — ten tabs
# on 2026-09-24. Whatever copy and history started a flow now finish it.
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"
STATE_DIR="$STATE_HOME/tmux-closed-tabs"
HISTORY="$STATE_DIR/history.jsonl"
PREVIEWS="$STATE_DIR/previews"
LOCK="$STATE_DIR/lock"
LOGFILE="$STATE_DIR/log"
RETAIN_DAYS=7
MAX_ENTRIES=100
PREVIEW_GRACE=120     # seconds an unreferenced snapshot is left for its close to finish (prune)
# The preview sits UNDER the list so it gets (nearly) the full client width: a
# snapshot is as wide as the tab was, and anything wider than the preview is
# clipped on the right.
POPUP_W_PCT=94
POPUP_H_PCT=85
# Centred on the whole WezTerm row while a sidebar sits beside the client
# (tmux.conf.tmpl's @popup_x); tmux's own centre on a server without it.
POPUP_X='#{?#{@popup_x},#{E:@popup_x},#{popup_centre_x}}'

RESURRECT_SAVE="$HOME/.config/tmux/plugins/tmux-assistant-resurrect/scripts/save-assistant-sessions.sh"

mkdir -p "$STATE_DIR" "$PREVIEWS"

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
# msg [-k KIND] TEXT: to the client tmux resolves.
msg() {
    local k=info
    [ "${1:-}" = -k ] && { k="$2"; shift 2; }
    notify_user -k "$k" -- "$*"
}
log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOGFILE" 2>/dev/null; }

# mkdir is the atomic lock (macOS has no flock(1)). A lock older than 10s is a
# crashed holder; it is broken with rename(2), not rm+mkdir. The age is checked
# AGAIN on what the rename actually took: two waiters can both judge the same
# dead lock stale, and the slower one's rename would otherwise carry off the
# fresh lock the faster one had just made. A fresh catch is put back.
lock_age() { echo $(( $(date +%s) - $(stat -f %m "$1" 2>/dev/null || date +%s) )); }
lock_acquire() {
    local i=0
    until mkdir "$LOCK" 2>/dev/null; do
        if [ $((i % 20)) -eq 19 ] && [ "$(lock_age "$LOCK")" -gt 10 ] \
           && mv "$LOCK" "$LOCK.stale.$$" 2>/dev/null; then
            if [ "$(lock_age "$LOCK.stale.$$")" -gt 10 ]; then
                rm -rf "$LOCK.stale.$$"
                continue
            fi
            # Guarded: mv onto an existing directory moves INTO it.
            [ -e "$LOCK" ] || mv "$LOCK.stale.$$" "$LOCK" 2>/dev/null
            rm -rf "$LOCK.stale.$$"
        fi
        i=$((i + 1))
        [ "$i" -ge 200 ] && return 1
        sleep 0.05
    done
}
lock_release() { rmdir "$LOCK" 2>/dev/null; }

# Rewrite the history through jq, atomically. Caller holds the lock.
history_filter() {
    [ -s "$HISTORY" ] || return 0
    jq -c "$@" "$HISTORY" >"$HISTORY.tmp" 2>/dev/null && mv "$HISTORY.tmp" "$HISTORY"
}

# ids as a JSON array, for jq --argjson.
ids_json() { printf '%s\n' "$@" | jq -R . | jq -sc .; }

# Age out old entries, cap the count, and delete snapshots nothing points at
# (including any left by a close that died halfway). Caller holds the lock.
prune() {
    local cutoff=$(( $(date +%s) - RETAIN_DAYS * 86400 ))
    history_filter --argjson cut "$cutoff" 'select(.ts >= $cut)'
    # grep -c prints "0" AND exits 1 on an empty file, so `|| echo 0` would
    # yield "0\n0"; wc -l always prints exactly one number.
    local n; n=$(wc -l <"$HISTORY" 2>/dev/null | tr -d ' '); n=${n:-0}
    if [ "$n" -gt "$MAX_ENTRIES" ]; then
        tail -n "$MAX_ENTRIES" "$HISTORY" >"$HISTORY.tmp" && mv "$HISTORY.tmp" "$HISTORY"
    fi
    # One jq for the live ids, then in-shell matching: a grep per snapshot
    # was a fork per history entry on every close and every CMD+Z.
    #
    # An unreferenced snapshot younger than PREVIEW_GRACE is NOT orphaned: it
    # belongs to a close still in flight. do_close writes the preview first
    # and its history row only after the agent lookup, the kill and
    # agent_session — hundreds of ms for a claude tab — and closing a space
    # (prefix Q) runs every pane's close at once, so the first one to reach
    # this deleted the screens of all the others (2026-10-22). The stat is
    # paid only for unreferenced files, so the steady state forks nothing.
    local keep f id now; now=$(date +%s)
    keep=$'\n'$(jq -r '.id' "$HISTORY" 2>/dev/null)$'\n'
    for f in "$PREVIEWS"/*.ansi; do
        [ -e "$f" ] || continue
        id=${f##*/}; id=${id%.ansi}
        [[ "$keep" == *$'\n'"$id"$'\n'* ]] && continue
        [ $(( now - $(stat -f %m "$f" 2>/dev/null || echo "$now") )) -lt "$PREVIEW_GRACE" ] && continue
        rm -f "$f"
    done
}

# Where the plugin's own hooks keep per-pid state (claude-<pid>.json from its
# SessionStart hook, opencode-<pid>.json from its plugin). Same expression as
# save-assistant-sessions.sh.
ASSISTANT_STATE_DIR="${TMUX_ASSISTANT_RESURRECT_DIR:-${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/tmux-assistant-resurrect}"
GROK_HOME="${GROK_HOME:-$HOME/.grok}"

# Grok's TUI, from argv (words, argv0 first): the interactive session is an
# agent; -p/--single/--prompt-file/--prompt-json runs, help/version and the
# utility and daemon subcommands are not. Same rules as the watcher's
# grok_tui_args and assistant-save.sh's awk; -r/--resume and -w/--worktree take
# an optional value (a next word that is neither an option nor a subcommand).
# Returns 0 for a TUI, 1 for headless (final), 2 for a subcommand first word:
# ps flattens argv, so `grok "help me fix X"` reads as `grok help me fix X`,
# and is_grok settles that one by whether the pid has a session. The scan goes
# on past the first bare word (a headless flag later still counts) and stops
# at `--` (words after it are prompt text). Line for line the watcher's
# grok_tui_args.
GROK_SUBCMDS=" agent leader worker clone completions doctor du disk-usage export help inspect login logout mcp memory models plugin sessions setup trace update usage version v worktree wrap "
grok_tui() {
    local a i=1 n=$# first=1 sub=0
    local -a w=("$@")
    while [ "$i" -lt "$n" ]; do
        a=${w[i]}
        case "$a" in
        --) break ;;
        # -phi / -p=hi (a glued value) and -h… / -v… are headless too.
        -p*|--single|--single=*|--prompt-file|--prompt-file=*|--prompt-json|--prompt-json=*|-h*|--help|--help=*|-v*|--version|--version=*)
            return 1 ;;
        --add-dir|--agent|--agents|--allow|--cwd|--debug-file|--deny|--disallowed-tools|--json-schema|--leader-socket|-m|--model|--max-turns|--output-format|--permission-mode|--reasoning-effort|--effort|--rules|-s|--session-id|--sandbox|--system-prompt-override|--system-prompt|--tools|--worktree-ref|--ref)
            i=$((i + 2)); continue ;;
        -r|--resume|-w|--worktree)
            i=$((i + 1))
            a=${w[i]:-}
            if [ -n "$a" ] && [[ "$a" != -* ]] && [[ "$GROK_SUBCMDS" != *" $a "* ]]; then
                i=$((i + 1))
            fi
            continue ;;
        -*) i=$((i + 1)); continue ;;
        esac
        if [ "$first" = 1 ]; then
            first=0
            [[ "$GROK_SUBCMDS" == *" $a "* ]] && sub=1
        fi
        i=$((i + 1))
    done
    [ "$sub" = 1 ] && return 2
    return 0
}
# grok_has_session <pid>: a grok-<pid>.json record (only a TUI fires
# SessionStart) or an active_sessions.json entry. No fork.
grok_has_session() {
    [ -f "$ASSISTANT_STATE_DIR/grok-$1.json" ] && return 0
    local a="" re="\"pid\"[[:space:]]*:[[:space:]]*$1([^0-9]|\$)"
    [ -s "$GROK_HOME/active_sessions.json" ] || return 1
    read -r -d '' a <"$GROK_HOME/active_sessions.json"
    [[ "$a" =~ $re ]]
}
# is_grok <args> [pid]: a Grok TUI's command line (argv0 basename grok or
# grok-<os>-<arch>; never the `agent` symlink to the same binary).
is_grok() {
    local -a w
    set -f
    w=($1)
    set +f
    [ "${#w[@]}" -gt 0 ] || return 1
    [[ "${w[0]##*/}" =~ ^grok(-(macos|linux|windows)-[a-z0-9_]+)?$ ]] || return 1
    grok_tui "${w[@]}"
    case $? in
    0) return 0 ;;
    2) [ -n "${2:-}" ] && grok_has_session "$2" ;;
    *) return 1 ;;
    esac
}

# The part of the agent lookup that needs the agent ALIVE, kept as small as
# possible because the tab stays on screen until it's done: find the agent
# process and copy its state file somewhere safe (claude deletes its own as it
# exits). Resolving the session id from that copy happens after the kill.
# Prints "<tool>\x1f<pid>\x1f<args>\x1f<state copy dir>" or nothing.
agent_snapshot() {
    local pane_pid="$1" pane_tty="$2"
    local lib; lib="$(dirname "$RESURRECT_SAVE")/lib-detect.sh"
    [ -r "$lib" ] || return 0
    (
        # shellcheck source=/dev/null
        source "$lib" 2>/dev/null || exit 0
        # Only the processes on this pane's terminal: 2-3ms, against ~50ms for
        # the whole-machine `ps -eo` the plugin takes by default. The agent and
        # everything under it share the pane's tty, so the tree walk sees all
        # it needs.
        local snap; snap=$(ps -t "${pane_tty#/dev/}" -o pid=,ppid=,args= 2>/dev/null)
        [ -n "$snap" ] || exit 0
        # pane_has_assistant's walk (the pane's own pid, then its descendants)
        # with the plugin's detect_tool, plus a Grok TUI. Done here rather than
        # through a detect_tool override because the grok check needs the pid
        # (is_grok's session test).
        local apid="" args="" tool="" p a
        while IFS=$'\x1f' read -r p a; do
            if is_grok "$a" "$p"; then tool=grok
            else tool=$(detect_tool "$a")
            fi
            if [ -n "$tool" ]; then apid=$p args=$a; break; fi
        done < <(awk -v root="$pane_pid" '
            { pid = $1; ppid = $2; line = $0; sub(/^ *[0-9]+ +[0-9]+ /, "", line)
              args[pid] = line; kids[ppid] = kids[ppid] " " pid }
            END {
                q[++qe] = root
                for (qs = 1; qs <= qe; qs++) {
                    cur = q[qs]
                    if (cur in args) print cur "\037" args[cur]
                    n = split(kids[cur], c, " ")
                    for (j = 1; j <= n; j++) q[++qe] = c[j]
                }
            }' <<<"$snap")
        [ -n "$tool" ] || exit 0
        local copy; copy=$(mktemp -d)
        cp "$ASSISTANT_STATE_DIR/$tool-$apid.json" "$copy/" 2>/dev/null
        # Grok drops its active_sessions.json entry as it exits, and that
        # entry is where its current session id lives (/new, /resume).
        [ "$tool" = grok ] && cp "$GROK_HOME/active_sessions.json" "$copy/grok-active.json" 2>/dev/null
        printf '%s\x1f%s\x1f%s\x1f%s\n' "$tool" "$apid" "$args" "$copy"
    )
}

# The session behind an agent, through the plugin's own resolver (emit_session).
# With a state dir, reads that copy instead of the live one — which is what
# lets this run after the agent is gone.
# Prints "<session_id>\x1f<cli_args>\x1f<model>\x1f<env json>" or nothing;
# grok adds a fifth field, the session's own cwd.
agent_session() {
    local tool="$1" apid="$2" args="$3" cwd="$4" state="${5:-}"
    if [ "$tool" = grok ]; then
        grok_session "$apid" "$args" "${state:-}"
        return 0
    fi
    [ -r "$RESURRECT_SAVE" ] || return 0
    (
        # The save script turns on errexit/nounset when sourced; its functions
        # are written for that, and this subshell keeps it from leaking out.
        # shellcheck source=/dev/null
        source "$RESURRECT_SAVE" 2>/dev/null || exit 0
        set +e
        [ -n "$state" ] && STATE_DIR="$state"
        # Seed its per-process flag cache with its own fallback list. Otherwise
        # stripping the resume flags runs `claude --help` on every close, which
        # is ~300ms of the key feeling slow.
        _SESSION_FLAGS_claude="${SESSION_FLAGS_FALLBACK_claude:-}"
        _SESSION_FLAGS_opencode="${SESSION_FLAGS_FALLBACK_opencode:-}"
        PARTS_FILE=$(mktemp)
        emit_session "-" "$tool" "$apid" "$args" "$cwd" 1 0 >/dev/null 2>&1
        jq -r '[.session_id // "", .cli_args // "", .model // "", (.env // {} | tojson)] | join("\u001f")' \
            "$PARTS_FILE" 2>/dev/null
        rm -f "$PARTS_FILE"
    )
}

# A Grok session, the way assistant-save.sh's grok post-pass resolves it: the
# active_sessions.json entry for the pid (rewritten on /new and /resume), else
# the grok-<pid>.json record grok-session-track.sh keeps, else a UUID given to
# -r/--resume or -s/--session-id. From the copies agent_snapshot took when the
# state dir is given, the live files otherwise. cli_args is argv minus argv0
# (build_cmd trims it); no model (a -m in argv stays there); env from the record.
grok_session() {
    local apid="$1" args="$2" state="${3:-}" act rec
    if [ -n "$state" ]; then
        act="$state/grok-active.json" rec="$state/grok-$apid.json"
    else
        act="$GROK_HOME/active_sessions.json" rec="$ASSISTANT_STATE_DIR/grok-$apid.json"
    fi
    [ -s "$act" ] || act=/dev/null
    [ -s "$rec" ] || rec=/dev/null
    jq -rn --arg pid "$apid" --arg args "$args" --slurpfile act "$act" --slurpfile rec "$rec" '
        def uuid: test("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$");
        ($args | [splits("[ \t]+")] | map(select(length > 0))) as $w
        | ([range(0; $w | length) as $i
            | if ($w[$i] | test("^(-r|--resume|-s|--session-id)$")) then ($w[$i + 1] // "")
              elif ($w[$i] | test("^(--resume|--session-id)=")) then ($w[$i] | sub("^[^=]*="; ""))
              else empty end
            | select(uuid)] | last // "") as $argsid
        | ((($act[0] // []) | if type == "array" then . else [] end)
           | map(select((.pid | tostring) == $pid)) | last // {}) as $a
        | ($rec[0] // {} | if type == "object" then . else {} end) as $r
        | ([$a.session_id, $r.session_id, $argsid] | map(select(type == "string" and . != "")) | first // "") as $sid
        | select($sid != "")
        | [$sid, ($w[1:] | join(" ")), "", ($r.env // {} | tojson),
           ([$a.cwd, $r.cwd] | map(select(type == "string" and . != "")) | first // "")]
        | join("\u001f")' 2>/dev/null
}

# grok_cmd: the grok to resume with. The installer links ~/.grok/bin/grok and
# puts nothing on PATH, so that link wins when it exists.
grok_cmd() {
    if [ -x "$GROK_HOME/bin/grok" ]; then posix_quote "$GROK_HOME/bin/grok"
    else printf 'command grok'; fi
}

# The same resume command restore-assistant-sessions.sh builds.
build_cmd() {
    local tool="$1" sid="$2" cli_args="$3" model="$4" env_json="$5"
    [ -n "$sid" ] || return 0
    # shellcheck source=/dev/null
    source "$(dirname "$RESURRECT_SAVE")/lib-detect.sh" 2>/dev/null || return 0
    local q_args="" a v i=0 n literal=0 has_model=0 words=()
    set -f
    words=($cli_args)
    set +f
    n=${#words[@]}
    while [ "$i" -lt "$n" ]; do
        a=${words[i]}
        i=$((i + 1))
        # Codex: drop every saved `-c model_context_window=…` (the spellings
        # resurrect-save-repair.py's context_override() drops). The wrapper
        # prepends a fresh one; a saved copy sits later, wins, and each
        # close/reopen used to add another, pinning the old catalog maximum.
        # A value-taking flag keeps its operand whatever it looks like, and
        # nothing after `--` is touched. Flag list = VALUE_FLAGS in
        # resurrect-save-repair.py (also in assistant-restore.sh's JQ_PROG).
        if [ "$tool" = codex ] && [ "$literal" = 0 ]; then
            case "$a" in
            --) literal=1 ;;
            -c|--config)
                if [ "$i" -lt "$n" ] && [ "${words[i]%%=*}" = model_context_window ]; then
                    i=$((i + 1))
                    continue
                fi
                if [ "$i" -lt "$n" ]; then
                    q_args+=" $(posix_quote "$a") $(posix_quote "${words[i]}")"
                    i=$((i + 1))
                    continue
                fi ;;
            --enable|--disable|-C|--cd|-m|--model|-p|--profile|-s|--sandbox|-a|--ask-for-approval|-i|--image|--add-dir|--local-provider|--remote)
                if [ "$i" -lt "$n" ]; then
                    q_args+=" $(posix_quote "$a") $(posix_quote "${words[i]}")"
                    i=$((i + 1))
                    continue
                fi ;;
            -c=*|--config=*) v=${a#*=}; [ "${v%%=*}" = model_context_window ] && continue ;;
            -c[!-=]*) v=${a#-c}; [ "${v%%=*}" = model_context_window ] && continue ;;
            esac
        fi
        # Claude: only the leading options. A launch prompt (`claude "fix
        # it"`, an Ask Claude space's sentence) reaches cli_args word by word,
        # and replayed it became extra positionals, a new turn or a subcommand
        # (`update …`) run instead of the resume. A value-taking flag keeps its
        # one operand; the first bare word or `--` ends it all. Flag list =
        # cvflag in assistant-restore.sh's JQ_PROG.
        if [ "$tool" = claude ]; then
            case "$a" in
            --|[!-]*) break ;;
            --model|--permission-mode|--add-dir|--allowedTools|--allowed-tools|--disallowedTools|--disallowed-tools|--tools|--append-system-prompt|--system-prompt|--append-system-prompt-file|--system-prompt-file|--settings|--setting-sources|--mcp-config|--agents|--agent|--fallback-model|--session-id|--output-format|--input-format|--max-turns|--betas|--plugin-dir)
                [ "$a" = --model ] && has_model=1
                if [ "$i" -lt "$n" ]; then
                    q_args+=" $(posix_quote "$a") $(posix_quote "${words[i]}")"
                    i=$((i + 1))
                    continue
                fi ;;
            --model=*) has_model=1 ;;
            esac
        fi
        # Grok: only the leading options too, minus what would fight the
        # `-r <sid>` added below: -r/--resume and -w/--worktree with their
        # optional value (a next word that is no option), -c/--continue,
        # -s/--session-id, --worktree-ref/--ref and --cwd with theirs, and the
        # one-shot --fork-session and --restore-code. The tab reopens in the
        # session's cwd, so --cwd (and a relative --add-dir, dropped too)
        # would resolve against the wrong directory; an absolute --add-dir
        # stays. Same rules as grokcut in assistant-restore.sh.
        if [ "$tool" = grok ]; then
            case "$a" in
            -r|--resume|-w|--worktree)
                [ "$i" -lt "$n" ] && [[ "${words[i]}" != -* ]] && i=$((i + 1))
                continue ;;
            -s|--session-id|--worktree-ref|--ref|--cwd) i=$((i + 1)); continue ;;
            --add-dir)
                if [ "$i" -lt "$n" ]; then
                    [[ "${words[i]}" == /* ]] && q_args+=" $(posix_quote "$a") $(posix_quote "${words[i]}")"
                    i=$((i + 1))
                fi
                continue ;;
            --add-dir=/*) ;;
            -c|--continue|--fork-session|--restore-code|--resume=*|--worktree=*|--session-id=*|--worktree-ref=*|--ref=*|--cwd=*|--add-dir=*) continue ;;
            --|[!-]*) break ;;
            --agent|--agents|--allow|--debug-file|--deny|--disallowed-tools|--json-schema|--leader-socket|-m|--model|--max-turns|--output-format|--permission-mode|--reasoning-effort|--effort|--rules|--sandbox|--system-prompt-override|--system-prompt|--tools)
                if [ "$i" -lt "$n" ]; then
                    q_args+=" $(posix_quote "$a") $(posix_quote "${words[i]}")"
                    i=$((i + 1))
                    continue
                fi ;;
            esac
        fi
        q_args+=" $(posix_quote "$a")"
    done
    # Only the variables @assistant-resurrect-capture-env lists, as a prefix.
    [ -n "$env_json" ] || env_json='{}'
    local env_prefix="" var val
    while IFS=$'\x1f' read -r var val; do
        [[ "$var" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && [ -n "$val" ] || continue
        env_prefix+="$var=$(posix_quote "$val") "
    done < <(jq -r --arg vars "$(tmux show-option -gqv @assistant-resurrect-capture-env 2>/dev/null)" \
        '($vars | split(" ")) as $v | to_entries[] | select(.key as $k | $v | index($k))
         | "\(.key)\u001f\(.value)"' <<<"$env_json" 2>/dev/null)
    case "$tool" in
    claude)
        [ "$has_model" = 0 ] && [ -n "$model" ] && q_args+=" --model $(posix_quote "$model")"
        echo "${env_prefix}command claude${q_args} --resume $(posix_quote "$sid")" ;;
    opencode) echo "${env_prefix}command opencode${q_args} -s $(posix_quote "$sid")" ;;
    codex)
        # The wrapper adds --no-daemon, which the tab/notch binding relies
        # on; name it so a pane PATH with ~/.bun/bin first can't skip it.
        local codex="command codex"
        [ -x "$HOME/.local/bin/codex" ] && codex=$(posix_quote "$HOME/.local/bin/codex")
        echo "${env_prefix}${codex}${q_args} resume $(posix_quote "$sid")" ;;
    grok) echo "${env_prefix}$(grok_cmd)${q_args} -r $(posix_quote "$sid")" ;;
    esac
}

# Ordered for latency: everything that has to be read from the live pane (its
# screen, its agent's session) comes first, then the pane dies, and only then
# the bookkeeping — building the resume command, writing and pruning the
# history. The tab is off the bar before any of that starts.
do_close() {
    local pane="${1:-}"
    # An empty -t resolves to the CURRENT pane, not to nothing.
    [ -n "$pane" ] || { msg -k error "closed-tabs: no pane given"; return 1; }

    local info
    info=$(tmux display-message -p -t "$pane" \
        '#{pane_id}|#{pane_pid}|#{pane_tty}|#{session_name}|#{session_id}|#{session_created}|#{window_index}|#{pane_current_path}|#{?#{n:#{@agent_summary}},#{@agent_summary},#{window_name}}' 2>/dev/null)
    # display-message against a dead target can succeed with an empty
    # expansion — test the expansion, not the exit status.
    local pane_id pane_pid pane_tty sess tsess tcreated widx cwd label
    IFS='|' read -r pane_id pane_pid pane_tty sess tsess tcreated widx cwd label <<<"$info"
    [ -n "$pane_id" ] || return 1

    local ts id
    ts=$(date +%s); id="$ts-$$"

    # The screen as it is right now, for the picker, captured alongside the
    # agent lookup. Trailing blank rows are dropped so the preview (which
    # follows the bottom) lands on the last line of real output rather than on
    # empty space under a shell prompt.
    tmux capture-pane -ep -t "$pane_id" 2>/dev/null | perl -e '
        my @l = <STDIN>;
        while (@l && $l[-1] =~ /^(?:\e\[[0-9;:]*[A-Za-z]|\s)*$/) { pop @l }
        print @l;' >"$PREVIEWS/$id.ansi" &
    local cap_pid=$!

    local tool="" apid="" args="" copy="" snap
    snap=$(agent_snapshot "$pane_pid" "$pane_tty")
    [ -n "$snap" ] && IFS=$'\x1f' read -r tool apid args copy <<<"$snap"

    # codex is the exception that resolves BEFORE the kill: its fallbacks match
    # threads against the live process's start time, which a dead pid no
    # longer has.
    local sid="" cli_args="" model="" env_json="" sess_info=""
    [ "$tool" = codex ] && sess_info=$(agent_session "$tool" "$apid" "$args" "$cwd" "$copy")
    wait "$cap_pid"

    # SIGTERM first so the agent starts its own shutdown, then take the pane at
    # once. Waiting for it to exit is ~2.2s (claude reaps every MCP child
    # first) and buys nothing measurable: with and without the SIGTERM, all of
    # a claude's descendants were gone within 6s of the kill-pane.
    if [[ "$apid" =~ ^[0-9]+$ ]] && [ "$apid" -gt 1 ]; then
        kill -TERM "$apid" 2>/dev/null
    fi
    tmux kill-pane -t "$pane_id" 2>/dev/null

    if [ -n "$tool" ] && [ "$tool" != codex ]; then
        sess_info=$(agent_session "$tool" "$apid" "$args" "$cwd" "$copy")
    fi
    [ -n "$copy" ] && rm -rf "$copy"
    local scwd=""
    [ -n "$sess_info" ] && IFS=$'\x1f' read -r sid cli_args model env_json scwd <<<"$sess_info"
    # A grok session is looked up under its own cwd (a -w worktree one too).
    [ -n "$scwd" ] && [ -d "$scwd" ] && cwd="$scwd"

    local cmd; cmd=$(build_cmd "$tool" "$sid" "$cli_args" "$model" "$env_json")
    local rec
    rec=$(jq -cn --arg id "$id" --arg ts "$ts" --arg sess "$sess" --arg widx "$widx" \
        --arg tsess "$tsess" --arg tcreated "$tcreated" \
        --arg cwd "$cwd" --arg label "$label" --arg tool "$tool" --arg sid "$sid" --arg cmd "$cmd" \
        '{id: $id, ts: ($ts|tonumber), session: $sess, tmux_session: $tsess,
          session_created: $tcreated, index: ($widx|tonumber), cwd: $cwd,
          label: $label, tool: $tool, session_id: $sid, cmd: $cmd}')

    if lock_acquire; then
        printf '%s\n' "$rec" >>"$HISTORY"
        prune
        lock_release
    else
        # The tab is already gone, so write it anyway: a lone O_APPEND line is
        # safe next to other appends, and only a prune racing it could drop it.
        printf '%s\n' "$rec" >>"$HISTORY"
        log "history lock busy — appended unlocked"
    fi
    log "close $sess:$widx cwd=$cwd tool=${tool:--} sid=${sid:--}"
    if [ -n "$tool" ] && [ -z "$sid" ]; then
        msg -k warn "closed-tabs: couldn't find the $tool session id — reopening will give a plain shell"
    fi
    # Fully detached: the model call takes seconds and the tab is already gone.
    # Output to /dev/null so tmux isn't left holding the job's pipe open.
    ( XDG_STATE_HOME="$STATE_HOME" "$SELF" describe "$id" </dev/null >/dev/null 2>&1 & )
    return 0
}

# --- the last tab of a space ---------------------------------------------------
#
# "Closing the last tab in a space prompts to close the space. If you say no,
# it goes to the home of that space" (Mack, 2026-11-03). Opt-in: only the
# closes made by hand ask — prefix x / C-l (`close --ask-space`), the agent-ui
# menu's x (the same) and prefix v's range close (stash.sh, one `ask-space`
# after all its closes). A plain `close` never does, so space.sh close's own
# closer, the prune / pause / limit code and a tab whose process exits by
# itself never prompt.
#
# The client lands on the space's home FIRST (switch-client, even from another
# space: the menu can close any space's tab), then the box opens there. A
# dismissed menu runs no command, so Cancel, Esc and a click outside all leave
# it on home with nothing more to do; only y closes, through `ask-space-yes`:
# it checks the space is still nothing but its home (a tab opened while the
# box was up would go with it) and runs prefix Q's own command (space.sh
# close: every pane through this script, ⌘Z reopens). main
# is never closed: no box, and a client already in main lands on its home.
# Hidden sessions, a space already closing and spaces off: nothing.
#
# tmux refuses a menu, silently and at once, while the client has another
# overlay up (the agent-ui menu stays open after its x), and a menu that IS up
# holds the `tmux display-menu` call until it is answered. So a call still
# running ASK_PROBE ms past one tmux round trip (timed just before, so a slow
# or stalled server stretches it alike) was shown; one back sooner met an
# overlay and is retried every ASK_RETRY seconds, up to ASK_TRIES times (a
# minute), re-checking first that the space still has only its home and the
# client is still on it. The probe is short because an answer inside it reads
# as a refusal and would bring the box back once; nothing a hand does lands
# inside 0.15 s of the box appearing (keys typed before it go to the pane).
ASK_PROBE=150
ASK_RETRY=0.2
ASK_TRIES=120
US=$'\x1f'

# space_home_only <target>: "<session_id> <name> <home pane>" (US-separated)
# when the target's session is a space of nothing but its home; else status 1.
space_home_only() {
    local info sid name hid closing on nwin hidden_list
    info=$(tmux display-message -p -t "$1" \
        "#{session_id}${US}#{session_name}${US}#{@space_hidden}${US}#{@space_closing}${US}#{@spaces}${US}#{session_windows}${US}#{@space_hidden_sessions}" 2>/dev/null)
    IFS="$US" read -r sid name hid closing on nwin hidden_list <<<"$info"
    [ -n "$sid" ] && [ "$on" = on ] && [ "$hid" != 1 ] && [ -z "$closing" ] && [ "$nwin" = 1 ] || return 1
    [[ " $hidden_list " == *" $name "* ]] && return 1
    local win home pane
    IFS="$US" read -r win home <<<"$(tmux list-windows -t "$sid" -F "#{window_id}${US}#{@space_home}" 2>/dev/null)"
    [ "$home" = 1 ] || return 1
    pane=$(tmux list-panes -t "$win" -F '#{?#{@space_home_pane},#{pane_id},}' 2>/dev/null | grep -m 1 .)
    [ -n "$pane" ] || return 1
    printf '%s%s%s%s%s\n' "$sid" "$US" "$name" "$US" "$pane"
}

# The session client $1 is on; status 1 when it is not attached.
client_session() {
    tmux list-clients -F "#{client_name}${US}#{session_id}" 2>/dev/null \
        | awk -F "$US" -v c="$1" '$1 == c { print $2; f = 1; exit } END { exit !f }'
}

# ask_space <client> <target>: the target's session after a close (a pane
# target: a session id, `=name:`, or any window or pane of it; a bare `=name`
# resolves to nothing). Returns once the box is answered or given up on.
ask_space() {
    local client="${1:-}" row sid name pane on
    [ -n "$client" ] && [ -n "${2:-}" ] || return 0
    row=$(space_home_only "$2") || return 0
    IFS="$US" read -r sid name pane <<<"$row"
    on=$(client_session "$client") || return 0
    if [ "$name" = main ]; then
        [ "$on" = "$sid" ] && tmux select-pane -t "$pane" 2>/dev/null
        return 0
    fi
    tmux select-pane -t "$pane" \; switch-client -c "$client" -t "$sid" 2>/dev/null || return 0
    # One box per space at a time: two closes racing to empty it both get
    # here, and the second would retry behind the first box and ask again
    # once it is answered. mkdir is the atomic test; the holder's pid inside
    # says whether it still lives (its box can stay up as long as it likes).
    # A dead holder's lock is taken over; a day-old one too, should its pid
    # have been reused. Released only by its holder.
    local held="$STATE_DIR/ask.${sid#\$}" owner=""
    if ! mkdir "$held" 2>/dev/null; then
        owner=$(cat "$held/pid" 2>/dev/null)
        # Not written yet: the holder is between its mkdir and its write.
        [ -z "$owner" ] && [ "$(lock_age "$held")" -lt 5 ] && return 0
        if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null && [ "$(lock_age "$held")" -lt 86400 ]; then
            return 0
        fi
        mv "$held" "$held.stale.$$" 2>/dev/null && rm -rf "$held.stale.$$"
        mkdir "$held" 2>/dev/null || return 0
    fi
    echo "$$" >"$held/pid"
    ask_box "$client" "$sid" "$name" "$pane"
    [ "$(cat "$held/pid" 2>/dev/null)" = "$$" ] && rm -rf "$held"
    return 0
}

# Milliseconds since the epoch, in REPLY: bash 5's EPOCHREALTIME, else perl
# (macOS's /bin/bash is 3.2).
now_ms() {
    if [ -n "${EPOCHREALTIME:-}" ]; then
        local t=${EPOCHREALTIME/[.,]/}
        REPLY=$(( 10#$t / 1000 ))
    else
        REPLY=$(perl -MTime::HiRes=time -e 'printf "%d", time() * 1000')
    fi
}

# The box itself, retried while another overlay holds the client.
ask_box() {
    local client="$1" sid="$2" name="$3" pane="$4" i=0 mp t0 wait_ms
    while [ "$i" -lt "$ASK_TRIES" ]; do
        # One round trip, as the refused menu's would be.
        now_ms; t0=$REPLY
        tmux display-message -p '' >/dev/null 2>&1
        now_ms; wait_ms=$(( REPLY - t0 + ASK_PROBE ))
        # prefix Q's box (spaces.conf), on this client, formats expanded in
        # the home pane: #{session_id} is the space, #{client_name} the
        # client. y re-checks before it closes (ask-space-yes).
        tmux display-menu -c "$client" -t "$pane" -b rounded -s 'bg=#1e1e2e,fg=#cdd6f4' -S 'fg=#f38ba8' \
            -H 'bg=#45475a,fg=#cdd6f4,bold' -T "#[align=centre] last tab closed · close space #{session_name}? " \
            -x "$POPUP_X" -y C "Cancel" n "" \
            "Close space" y "run-shell -b \"XDG_STATE_HOME='$STATE_HOME' bash '$SELF' ask-space-yes '#{client_name}' '#{session_id}'\"" \
            </dev/null >/dev/null 2>&1 &
        mp=$!
        sleep "$(printf '%d.%03d' $((wait_ms / 1000)) $((wait_ms % 1000)))"
        if kill -0 "$mp" 2>/dev/null; then
            log "ask-space $name on $client"
            wait "$mp"
            return 0
        fi
        wait "$mp"
        i=$((i + 1))
        sleep "$ASK_RETRY"
        space_home_only "$sid" >/dev/null || return 0
        [ "$(client_session "$client")" = "$sid" ] || return 0
    done
    log "ask-space $name: $client never took the box"
}

# close --ask-space <client> <pane>: the close, then ask_space for the pane's
# session, both read before the pane goes. Only a close that took its whole
# WINDOW (a tab) asks: closing a split of the home window in a space that
# already had nothing but its home leaves the very state that asks, and must
# not. kill-pane is synchronous, so by the time do_close returns the window
# is gone or not (a split, a failed kill: then nothing asks).
do_close_ask() {
    local client="${1:-}" pane="${2:-}" info="" sid="" win=""
    [ -n "$pane" ] && info=$(tmux display-message -p -t "$pane" "#{session_id}${US}#{window_id}" 2>/dev/null)
    IFS="$US" read -r sid win <<<"$info"
    do_close "$pane" || return
    [ -n "$sid" ] && [ -n "$win" ] || return 0
    [ -z "$(tmux display-message -p -t "$win" '#{window_id}' 2>/dev/null)" ] || return 0
    ask_space "$client" "$sid"
    return 0
}

# ask-space-yes <client> <session_id>: the box's y. Closes the space with
# prefix Q's command (@space_sh close, as run-shell would run it) only while
# it is still nothing but its home: a tab opened while the box was up says so
# and nothing closes. A space gone or closing meanwhile: nothing.
do_ask_space_yes() {
    local client="${1:-}" sid="${2:-}" n space_sh
    [ -n "$sid" ] || return 0
    if ! space_home_only "$sid" >/dev/null; then
        n=$(tmux display-message -p -t "$sid" '#{?#{@space_closing},,#{session_windows}}' 2>/dev/null)
        [ -n "$n" ] && [ "$n" -gt 1 ] \
            && notify_user ${client:+-c "$client"} -k warn -- "space has tabs again · not closed"
        return 0
    fi
    space_sh=$(tmux show-option -gqv @space_sh 2>/dev/null)
    [ -n "$space_sh" ] || { notify_user ${client:+-c "$client"} -k error -- "space: @space_sh is not set"; return 0; }
    sh -c "$space_sh close \"\$1\" \"\$2\"" _ "$sid" "$client"
}

# The conversation behind a closed agent tab as plain "ROLE: text" lines —
# messages and tool calls, no thinking, tool output or injected context.
transcript_digest() {
    local tool="$1" sid="$2" f
    [ -n "$sid" ] || return 0
    case "$tool" in
    claude)
        # ~/.claude-abby/projects: abby (Claude Code on abliteration.ai) keeps
        # its conversations apart from Claude's.
        f=$(ls "$HOME"/.claude/projects/*/"$sid".jsonl "$HOME"/.claude-abby/projects/*/"$sid".jsonl 2>/dev/null | head -n 1)
        [ -r "$f" ] || return 0
        jq -r 'select((.type == "user" or .type == "assistant") and .isMeta != true and .isSidechain != true)
            | .type as $t
            | (.message.content | if type == "string" then [{type: "text", text: .}] else . end)[]
            | if .type == "text" then "\($t | ascii_upcase): \(.text[0:800])"
              elif .type == "tool_use" then "  [\(.name)] \(.input | (.description // .command // .file_path // .pattern // .prompt // "") | tostring | .[0:160])"
              else empty end' "$f" 2>/dev/null ;;
    codex)
        f=$(find "$HOME/.codex/sessions" "$HOME/.codex/archived_sessions" -name "rollout-*$sid.jsonl" 2>/dev/null | head -n 1)
        [ -r "$f" ] || return 0
        jq -r 'select(.type == "response_item") | .payload
            | if .type == "message" and (.role == "user" or .role == "assistant") then
                .role as $r | .content[]? | select(.type == "input_text" or .type == "output_text")
                | select(.text | test("^\\s*<(environment_context|user_instructions|permissions)|^# AGENTS\\.md") | not)
                | "\($r | ascii_upcase): \(.text[0:800])"
              elif .type == "function_call" or .type == "custom_tool_call" then
                "  [\(.name)] \((.arguments // .input // "") | tostring | .[0:160])"
              else empty end' "$f" 2>/dev/null ;;
    opencode)
        opencode export "$sid" 2>/dev/null | jq -r '.messages[] | .info.role as $r | .parts[]
            | if .type == "text" then "\($r | ascii_upcase): \(.text[0:800])"
              elif .type == "tool" then "  [\(.tool)]"
              else empty end' 2>/dev/null ;;
    grok)
        # `grok export` reads the session off disk and prints Markdown
        # (~0.2s; no model, no leader): "## User" / "## Assistant" paragraphs
        # and "## Tools" bullets. Without it, the session's own title.
        local bin="$GROK_HOME/bin/grok" out=""
        [ -x "$bin" ] || bin=$(command -v grok 2>/dev/null)
        [ -n "$bin" ] && out=$(cd / && "$bin" export "$sid" </dev/null 2>/dev/null | awk '
            /^## User$/ { r = "USER"; next }
            /^## Assistant$/ { r = "ASSISTANT"; next }
            /^## / { r = (/^## Tools$/) ? "TOOLS" : ""; next }
            r == "TOOLS" && /^- / { print "  [" substr($0, 3, 160) "]"; next }
            r != "" && r != "TOOLS" && NF { print r ": " substr($0, 1, 800) }')
        if [ -n "$out" ]; then
            printf '%s\n' "$out"
        else
            f=$(ls "$GROK_HOME"/sessions/*/"$sid"/summary.json 2>/dev/null | head -n 1)
            [ -r "$f" ] && jq -r '(.generated_title // .session_summary // empty) | "TITLE: \(.)"' "$f" 2>/dev/null
        fi ;;
    esac
}

# Runs detached after a close: ask a small model for a one-line account of
# what the tab was doing and store it on the entry as .desc, which the picker
# shows on that entry's line. Agent tabs are read from their transcript, plain
# shells from the screen snapshot. Any failure just leaves the entry without
# one. The model call itself is `summarize`, which stash.sh uses too.
# ai-exec tries codex on DESCRIBE_MODEL, then claude on DESCRIBE_CLAUDE_MODEL,
# then copilot (auto); DESCRIBE_TIMEOUT covers the whole chain, not each leg.
DESCRIBE_MODEL=gpt-6-luna
DESCRIBE_CLAUDE_MODEL=haiku
DESCRIBE_TIMEOUT=180
# The whole digest goes in, so the label reflects the thread's theme rather
# than its last few turns. ~400k chars is ~120k tokens, inside both luna's
# 272k window and haiku's 200k; past it, tool-call lines go first, then the
# middle.
DESCRIBE_MAX_CHARS=400000
do_describe() {
    local id="${1:-}" rec
    [ -n "$id" ] || return 0
    rec=$(jq -c --arg id "$id" 'select(.id == $id)' "$HISTORY" 2>/dev/null)
    [ -n "$rec" ] || return 0
    local tool sid cwd label desc
    tool=$(jq -r '.tool' <<<"$rec"); sid=$(jq -r '.session_id' <<<"$rec")
    cwd=$(jq -r '.cwd' <<<"$rec");   label=$(jq -r '.label' <<<"$rec")

    desc=$(do_summarize "$tool" "$sid" "$cwd" "$label" "$PREVIEWS/$id.ansi")
    [ -n "$desc" ] || return 0
    if lock_acquire; then
        history_filter --arg id "$id" --arg d "$desc" 'if .id == $id then .desc = $d else . end'
        lock_release
        log "describe $id: $desc"
    else
        log "describe $id: history busy, dropped"
    fi
}

# One line on what a tab was doing, from its agent transcript (if any) and a
# screen capture. Prints nothing when there's nothing worth describing or the
# call fails.
#   summarize <tool> <session_id> <cwd> <label> <screen.ansi>
do_summarize() {
    local tool="${1:-}" sid="${2:-}" cwd="${3:-}" label="${4:-}" screen_file="${5:-}"
    local aiexec; aiexec=$(command -v ai-exec || echo "$HOME/.local/bin/ai-exec")
    [ -x "$aiexec" ] || { log "summarize: no ai-exec"; return 0; }

    local convo screen=""
    convo=$(transcript_digest "$tool" "$sid")
    if [ "${#convo}" -gt "$DESCRIBE_MAX_CHARS" ]; then
        convo=$(grep -v '^  \[' <<<"$convo")
    fi
    if [ "${#convo}" -gt "$DESCRIBE_MAX_CHARS" ]; then
        local head=$((DESCRIBE_MAX_CHARS / 2)) tail=$((DESCRIBE_MAX_CHARS / 2))
        convo="${convo:0:$head}"$'\n[…]\n'"${convo: -$tail}"
    fi
    # The screen only stands in for a missing transcript: next to one it's just
    # the last turn again, which is what pulled labels toward the latest step.
    [ -z "$convo" ] && [ -s "$screen_file" ] && screen=$(perl -pe 's/\e\[[0-9;:?]*[A-Za-z]//g' "$screen_file" | tail -n 60)
    # A bare prompt isn't worth a call.
    if [ -z "$convo" ] && [ "$(grep -c '[^[:space:]]' <<<"$screen")" -lt 3 ]; then
        return 0
    fi

    local work; work=$(mktemp -d)
    {
        cat <<'EOF'
You label terminal tabs the user has put away, so they can pick the right one
to bring back later. Below is what was happening in one tab. Write ONE line of
at most 70 characters naming what the session as a whole was about — the main
goal or theme running through the entire conversation, not just its last few
turns. If it wandered across topics, name the one it mostly served. Be
specific (name the feature, project, file or bug). No preamble, no quotes, no
trailing period. Do not run any commands; answer from this text alone.

EOF
        printf 'Tab title: %s\nDirectory: %s\nProgram: %s\n' "$label" "$cwd" "${tool:-shell}"
        [ -n "$convo" ] && printf '\n<transcript>\n%s\n</transcript>\n' "$convo"
        [ -n "$screen" ] && printf '\n<screen>\n%s\n</screen>\n' "$screen"
    } >"$work/prompt"

    # The codex flags pass through ai-exec verbatim: --ephemeral keeps it out
    # of codex's thread history and resume list; --ignore-user-config skips
    # MCP servers and notify; hooks off so it doesn't show up in the tab
    # indicator or notch as a running agent. ai-exec runs the claude/copilot
    # legs with CUA_NOTCH_IGNORE=1 and no user settings, for the same reason.
    (
        cd "$work" || exit 1
        unset TMUX TMUX_PANE
        exec "$aiexec" exec -m "$DESCRIBE_MODEL" -c model_reasoning_effort=low \
            --ephemeral --skip-git-repo-check --ignore-user-config --ignore-rules \
            --disable hooks -s read-only -o "$work/out" \
            --claude-model "$DESCRIBE_CLAUDE_MODEL" - <"$work/prompt" >"$work/log" 2>&1
    ) &
    local cpid=$!
    # The subshell exec'd ai-exec, so the kill lands on it: it takes its running
    # backend's process group down with it and tries no further backend.
    ( sleep "$DESCRIBE_TIMEOUT"; kill "$cpid" ) 2>/dev/null &
    local wd=$!
    wait "$cpid"; local rc=$?
    pkill -P "$wd" sleep 2>/dev/null; kill "$wd" 2>/dev/null

    # First line, tabs and separators out (they delimit picker fields), and a
    # word-boundary cut if the model ran long.
    local desc=""
    [ -s "$work/out" ] && desc=$(grep -m 1 '[^[:space:]]' "$work/out" \
        | tr '\t\037' '  ' | sed -E 's/^[[:space:]"'\''`]+//; s/[[:space:]"'\''`.]+$//')
    if [ "${#desc}" -gt 90 ]; then
        desc="${desc:0:88}"; desc="${desc% *}…"
    fi
    [ -n "$desc" ] || log "summarize ${tool:-shell} ${sid:--}: failed (rc=$rc) $(tail -n 3 "$work/log" 2>/dev/null | tr '\n' ' ')"
    rm -rf "$work"
    printf '%s' "$desc"
}

# Recreate one recorded tab. Prints the new window id.
reopen_one() {
    local rec="$1" client_sess="$2"
    local sess tsess tcreated idx cwd cmd tool label
    sess=$(jq -r '.session' <<<"$rec"); idx=$(jq -r '.index' <<<"$rec")
    cwd=$(jq -r '.cwd' <<<"$rec");      cmd=$(jq -r '.cmd // empty' <<<"$rec")
    tool=$(jq -r '.tool // empty' <<<"$rec"); label=$(jq -r '.label // empty' <<<"$rec")
    tsess=$(jq -r '.tmux_session // empty' <<<"$rec")
    tcreated=$(jq -r '.session_created // empty' <<<"$rec")

    # Back in its own session, found by identity first: the tmux id survives a
    # rename, but ids are handed out again after a server restart, so the
    # session's creation time has to match too. Then by name (a session
    # resurrect recreated after a restart), then wherever you are.
    local now_created=""
    [ -n "$tsess" ] && now_created=$(tmux display-message -p -t "$tsess" '#{session_created}' 2>/dev/null)
    if [ -n "$tcreated" ] && [ "$now_created" = "$tcreated" ]; then
        sess=$(tmux display-message -p -t "$tsess" '#{session_name}' 2>/dev/null)
    elif ! tmux has-session -t "=$sess" 2>/dev/null; then
        sess=$(tmux display-message -p -t "$client_sess" '#{session_name}' 2>/dev/null)
        [ -n "$sess" ] || return 1
        msg "closed-tabs: its session is gone — reopened in $sess"
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
    local -a autostart=() birth=()
    [ -n "$cmd" ] && autostart=(-e "ZSH_AUTOSTART=$cmd")
    # A claude/codex/opencode/grok tab comes back AS an agent, in the new-window's own
    # command list (agent-birth.conf; guarded, so a server without
    # @agent_birth still reopens it), wearing the name it had when it closed
    # until the resumed session's hooks retitle it. The name is a set-option
    # argument: one ending in ';' would end the command list, and one starting
    # with '-' would read as a flag, so those are trimmed / skipped.
    case "$tool" in
    claude|codex|opencode|grok)
        if [ -n "$cmd" ]; then
            birth=(\; run-shell -C "#{?@agent_birth,#{@agent_birth} $tool,}")
            label="${label%"${label##*[!; ]}"}"
            case "$label" in
            ''|-*) ;;
            *) birth+=(\; set-option -w @agent_summary "$label") ;;
            esac
        fi ;;
    esac
    tmux new-window -P -F '#{window_id}' "${where[@]}" -c "$cwd" "${autostart[@]}" "${birth[@]}" 2>/dev/null
}

# Reopen the given entries (default: the newest). Several at once go back
# newest-closed first: each one's recorded index was taken AFTER the ones closed
# before it had left, so undoing in reverse order puts every tab back in its
# original slot.
do_reopen() {
    local client_sess="${1:-}" client_tty="${2:-}"
    shift 2 2>/dev/null
    local recs
    lock_acquire || { msg -k warn "closed-tabs: history busy"; return 1; }
    if [ "$#" -gt 0 ]; then
        recs=$(jq -c --argjson ids "$(ids_json "$@")" 'select(.id as $i | $ids | index($i))' "$HISTORY" 2>/dev/null)
    else
        recs=$(tail -n 1 "$HISTORY" 2>/dev/null)
    fi
    if [ -z "$recs" ]; then
        lock_release
        msg -k warn "closed-tabs: nothing to reopen"
        return 0
    fi
    local ids; ids=$(jq -r '.id' <<<"$recs")
    # shellcheck disable=SC2086
    history_filter --argjson ids "$(ids_json $ids)" 'select(.id as $i | $ids | index($i) | not)'
    lock_release

    local rec win first="" n=0 failed=0 lost=0
    while IFS= read -r rec; do
        [ -n "$rec" ] || continue
        if win=$(reopen_one "$rec" "$client_sess") && [ -n "$win" ]; then
            [ -n "$first" ] || first="$win"
            n=$((n + 1))
            rm -f "$PREVIEWS/$(jq -r '.id' <<<"$rec").ansi"
            log "reopen $(jq -r '"\(.session) cwd=\(.cwd) tool=\(.tool) cmd=\(.cmd)"' <<<"$rec")"
        else
            # Put it back rather than lose it.
            failed=$((failed + 1))
            if lock_acquire; then
                printf '%s\n' "$rec" >>"$HISTORY"; lock_release
            else
                lost=$((lost + 1))
                log "reopen failed and history busy, dropped: $rec"
            fi
        fi
    # Newest close first. Ties on .ts (two closes in the same second) fall
    # back to file order, reversed — sort_by is stable, so -.ts alone would
    # replay same-second closes oldest first and swap their slots.
    done < <(jq -sc 'to_entries | sort_by(-.value.ts, -.key) | .[].value' <<<"$recs")

    [ -n "$client_tty" ] && [ -n "$first" ] && tmux switch-client -c "$client_tty" -t "$first" 2>/dev/null
    if [ "$lost" -gt 0 ]; then
        msg -k error "closed-tabs: reopened $n, couldn't recreate $failed ($lost lost: history busy, see log)"
    elif [ "$failed" -gt 0 ]; then
        msg -k error "closed-tabs: reopened $n, couldn't recreate $failed (kept in the history)"
    fi
}

do_forget() {
    [ "$#" -gt 0 ] || return 0
    lock_acquire || return 1
    history_filter --argjson ids "$(ids_json "$@")" 'select(.id as $i | $ids | index($i) | not)'
    local id; for id in "$@"; do rm -f "$PREVIEWS/$id.ansi"; done
    lock_release
    msg -k ok "closed-tabs: forgot $# tab(s)"
}

# fzf needs a terminal, so the picker runs inside a popup that re-enters this
# script as `pick`. Called from a FOREGROUND run-shell: a backgrounded one has
# no client to raise a popup on.
do_pick_popup() {
    local client_sess="${1:-}" client_tty="${2:-}"
    if lock_acquire; then prune; lock_release; fi
    if [ ! -s "$HISTORY" ]; then
        msg -k warn "closed-tabs: nothing to reopen"
        return 0
    fi
    local -a client=()
    [ -n "$client_tty" ] && client=(-c "$client_tty")
    tmux display-popup "${client[@]}" -E -w "${POPUP_W_PCT}%" -h "${POPUP_H_PCT}%" -x "$POPUP_X" \
        -T ' closed tabs ' "XDG_STATE_HOME='$STATE_HOME' '$SELF' pick '$client_sess' '$client_tty'"
}

# Sets REPLY rather than printing: $(ago …) would fork once per entry.
ago() {
    local s=$(( $2 - $1 ))
    if   [ "$s" -lt 60 ];    then REPLY="just now"
    elif [ "$s" -lt 3600 ];  then REPLY="$((s / 60))m ago"
    elif [ "$s" -lt 86400 ]; then REPLY="$((s / 3600))h ago"
    else                          REPLY="$((s / 86400))d ago"
    fi
}

# Runs inside the popup. Newest first, so CMD+Z then ⏎ is "reopen the last
# thing I closed". ⌃x forgets instead of reopening, via --expect (which puts
# the accepting key on the first output line, empty for a plain ⏎) — the same
# gesture as the stash picker's kill.
do_pick() {
    local client_sess="${1:-}" client_tty="${2:-}"
    local id ts tool label cwd desc
    local dim=$'\e[2m' mauve=$'\e[38;2;203;166;247m' teal=$'\e[38;2;148;226;213m' off=$'\e[0m'
    local sub=$'\e[38;2;166;173;200m'
    local out now; now=$(date +%s)
    # desc is the model's one-liner from do_describe; missing for a close that
    # was just made (still being written) or where the call failed.
    out=$(jq -r '[.id, .ts, .tool, .label, .cwd, (.desc // "")] | join("\u001f")' "$HISTORY" 2>/dev/null \
        | tail -r \
        | while IFS=$'\x1f' read -r id ts tool label cwd desc; do
            [ -n "$id" ] || continue
            local c="$teal"; [ "$tool" = claude ] && c="$mauve"
            ago "$ts" "$now"
            [ -n "$desc" ] && desc="  ${sub}— ${desc}${off}"
            printf '%s\t%s%-9s%s %s%-9s%s %s%s\t%s%s%s\n' "$id" \
                "$dim" "$REPLY" "$off" "$c" "${tool:-shell}" "$off" "$label" "$desc" \
                "$dim" "${cwd/#"$HOME"/\~}" "$off"
          done \
        | fzf --ansi --delimiter='\t' --with-nth=2.. --reverse --multi \
              --prompt='reopen > ' \
              --expect=ctrl-x \
              --bind 'shift-down:toggle+down,shift-up:toggle+up,ctrl-a:select-all,ctrl-d:deselect-all' \
              --preview "XDG_STATE_HOME='$STATE_HOME' '$SELF' preview {1}" \
              --preview-window 'down,72%,border-top,follow' \
              --header '⏎ reopen · Tab or ⇧↑/⇧↓ to pick several · ⌃x forget')
    local key=${out%%$'\n'*}
    local ids; ids=$(printf '%s\n' "$out" | tail -n +2 | cut -f1 | tr '\n' ' ')
    [ -n "${ids// /}" ] || return 0
    # Hand off so the popup closes the moment you pick. The ids are
    # <epoch>-<pid>, nothing a shell would interpret, so unquoted is fine.
    if [ "$key" = "ctrl-x" ]; then
        tmux run-shell -b "XDG_STATE_HOME='$STATE_HOME' '$SELF' forget $ids"
    else
        tmux run-shell -b "XDG_STATE_HOME='$STATE_HOME' '$SELF' reopen '$client_sess' '$client_tty' $ids"
    fi
}

do_preview() {
    local f="$PREVIEWS/${1:-}.ansi"
    if [ -s "$f" ]; then cat "$f"; else echo "(no snapshot)"; fi
}

do_list() {
    [ -s "$HISTORY" ] || { echo "(empty)"; return 0; }
    jq -r '"\(.ts | strflocaltime("%m-%d %H:%M"))  \(.session):\(.index)  \(if .tool == "" then "-" else .tool end)\t\(.label)\t\(.cwd)\(if .desc then "\n    \(.desc)" else "" end)"' "$HISTORY" | tail -r
}

case "${1:-}" in
close)      shift
            if [ "${1:-}" = --ask-space ]; then shift; do_close_ask "$@"; else do_close "$@"; fi ;;
ask-space)  shift; ask_space "$@" ;;
ask-space-yes) shift; do_ask_space_yes "$@" ;;
pick-popup) shift; do_pick_popup "$@" ;;
pick)       shift; do_pick "$@" ;;
preview)    shift; do_preview "$@" ;;
reopen)     shift; do_reopen "$@" ;;
forget)     shift; do_forget "$@" ;;
describe)   shift; do_describe "$@" ;;
summarize)  shift; do_summarize "$@" ;;
list)       do_list ;;
*) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
esac
