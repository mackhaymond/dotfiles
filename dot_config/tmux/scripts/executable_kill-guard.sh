#!/bin/bash
# kill-guard.sh: Touch ID gate (plus a verified save) in front of anything that
# would kill Mack's REAL default tmux server or one of its protected sessions
# (default: main). Docs: ~/.config/tmux/docs/kill-guard.md
#
# Entry points:
#
#   kill-guard.sh server SOCKET
#       Run by the server-side alias in tmux.conf (command-alias[90]:
#       kill-server=run-shell "... kill-guard.sh server #{q:socket_path}").
#       Any socket that is not protected is killed at once, so test servers that
#       happen to load tmux.conf behave normally. A protected socket asks for
#       Touch ID, saves everything (see save_first), then kills; anything else
#       refuses and exits 1, which run-shell turns into a non-zero exit for the
#       CLI caller.
#
#   kill-guard.sh cli TMUX-ARGS...
#       Exec'd by the ~/.local/bin/tmux shim (kill-guard/tmux-shim.c) only when
#       an argument contains "kill-se". Works out which socket the call would
#       reach; anything but a protected socket goes straight to the real tmux.
#       On a protected socket, kill-session of a protected / the last session,
#       kill-session -a, an ABBREVIATED kill-server (kill-ser .. kill-serve; the
#       alias only matches the full word), or a full kill-server on a server
#       WITHOUT the alias -> one Touch ID prompt for the whole invocation, the
#       same save, then the real tmux with the original arguments.
#
#   kill-guard.sh auth-source SOCKET | save-source SOCKET
#       Read-only: which authority / which save+publish commands a protected
#       kill on SOCKET would use. Used by the tests.
#
# The guard's own kills use the ABBREVIATION `kill-ser` through the ABSOLUTE
# real tmux path, so they never re-enter the alias or the shim.
#
# Environment:
#   KILL_GUARD_AUTH=approve|deny  test override. `approve` is IGNORED for the
#       real default socket (an env var must never stand in for Touch ID there);
#       `deny` is honoured everywhere (refusing is always safe).
#   KILL_GUARD_SAVE_CMD / KILL_GUARD_PUBLISH_CMD  test stubs for the save steps,
#       IGNORED for the real default socket.
#   KILL_GUARD_PROTECT_SOCKET=PATH  also treat PATH as protected (tests point it
#       at a -L test server). It can only ADD protection.
#   KILL_GUARD_DRYRUN=1  print the verdict and exit 0: no prompt, no save, no
#       tmux exec, and no liveness probes for kill-server abbreviations.
#   KILL_GUARD_LOG=PATH  decision log (default ~/.local/state/tmux-kill-guard/log).
#
# /bin/bash on macOS is 3.2: no associative arrays, no ${x,,}, no mapfile.

REAL_TMUX=/opt/homebrew/bin/tmux
HELPER="$HOME/.local/bin/tmux-touchid"
LOG="${KILL_GUARD_LOG:-$HOME/.local/state/tmux-kill-guard/log}"
TAB=$'\t'
STAT=/usr/bin/stat
# A failed `exec` must return here (and unset the shutdown flag), not end the
# script silently.
shopt -s execfail

# ---------------------------------------------------------------- helpers

# canon PATH: physical path of PATH's directory + basename. The socket itself
# may not exist (no server), so resolve the directory only; /tmp ->
# /private/tmp is the case that matters on macOS. A symlinked socket FILE is
# caught by same_socket's device:inode compare instead.
canon() {
    local p="$1" d b r
    case "$p" in */*) d="${p%/*}"; b="${p##*/}" ;; *) d="."; b="$p" ;; esac
    [ -z "$d" ] && d="/"
    if r="$(cd -P "$d" 2>/dev/null && pwd -P)"; then
        [ "$r" = "/" ] && r=""
        printf '%s/%s\n' "$r" "$b"
    else
        case "$p" in /tmp/*) p="/private$p" ;; esac
        printf '%s\n' "$p"
    fi
}

# same_socket A B: same canonical path, or (both exist) same device:inode, so
# `-S ~/link-to-default` is the real default too.
same_socket() {
    local a b
    [ "$(canon "$1")" = "$(canon "$2")" ] && return 0
    a="$($STAT -L -f %d:%i "$1" 2>/dev/null)" || return 1
    b="$($STAT -L -f %d:%i "$2" 2>/dev/null)" || return 1
    [ -n "$a" ] && [ "$a" = "$b" ]
}

# is_real_default SOCKET: Mack's real server, which is exactly
# /tmp/tmux-$UID/default (= /private/tmp/...), by canonical path or by
# device:inode. Mack never sets TMUX_TMPDIR. A TMUX_TMPDIR elsewhere means a bare
# `tmux` reaches a DIFFERENT server (agents' private test servers, which load
# the real tmux.conf and so this alias), and that is by definition not the real
# one: spare servers must die freely. TMUX_TMPDIR still matters for RESOLVING
# which socket a cli call reaches (mode_cli), just not for what is protected.
is_real_default() {
    same_socket "$1" "/tmp/tmux-$UID/default"
}

is_protected() {
    is_real_default "$1" && return 0
    [ -n "${KILL_GUARD_PROTECT_SOCKET:-}" ] &&
        same_socket "$1" "$KILL_GUARD_PROTECT_SOCKET" && return 0
    return 1
}

log() {
    local dir="${LOG%/*}" size
    /bin/mkdir -p "$dir" 2>/dev/null || return 0
    size="$($STAT -f %z "$LOG" 2>/dev/null || echo 0)"
    [ "${size:-0}" -gt 1048576 ] && /bin/mv -f "$LOG" "$LOG.1" 2>/dev/null
    printf '%s pid=%s %s\n' "$(/bin/date '+%Y-%m-%dT%H:%M:%S')" "$$" "$*" >>"$LOG" 2>/dev/null
}

# proc_chain START-PID | proc_chain - NEEDLE
# "cmd [pid] <- parent [pid] <- ..." (5 levels max). With "-", start from the
# first tmux client process whose argv contains NEEDLE (the CLI client that
# sent kill-server to the alias; #{client_pid} is empty for such clients).
proc_chain() {
    /bin/ps -ax -o pid=,ppid=,command= 2>/dev/null | /usr/bin/awk -v start="$1" -v want="${2:-}" -v self="$$" '
        { pid = $1; pp[pid] = $2; $1 = ""; $2 = ""; sub(/^ +/, ""); cmd[pid] = $0 }
        END {
            p = start
            if (start == "-") {
                p = ""
                for (q in cmd) {
                    split(cmd[q], w, " "); n = split(w[1], parts, "/")
                    if (parts[n] == "tmux" && index(cmd[q], want) && q != self) { p = q; break }
                }
            }
            out = ""
            for (i = 0; i < 5 && p != "" && p + 0 > 1 && (p in cmd); i++) {
                c = cmd[p]; if (length(c) > 60) c = substr(c, 1, 57) "..."
                out = out (out == "" ? "" : " <- ") c " [" p "]"
                p = pp[p]
            }
            print out
        }'
}

# auth_source SOCKET: which authority decides.
auth_source() {
    case "${KILL_GUARD_AUTH:-}" in
        deny) echo "override:deny"; return ;;
        approve)
            if is_real_default "$1"; then echo "touchid"; else echo "override:approve"; fi
            return ;;
    esac
    echo "touchid"
}

# authorize SOCKET REASON -> 0 approved, 1 denied, 2 unavailable
authorize() {
    local src rc
    src="$(auth_source "$1")"
    case "$src" in
        override:approve) return 0 ;;
        override:deny) return 1 ;;
    esac
    if [ "${KILL_GUARD_AUTH:-}" = approve ]; then
        log "auth-override-ignored socket=$1 (real default server)"
    fi
    if [ ! -x "$HELPER" ]; then
        log "touchid-helper-missing helper=$HELPER"
        return 2
    fi
    "$HELPER" "$2" </dev/null
    rc=$?
    case "$rc" in 0) return 0 ;; 2) return 2 ;; *) return 1 ;; esac
}

describe_rc() {
    case "$1" in
        0) echo approved ;;
        2) echo "Touch ID unavailable (no GUI session, helper missing, or no auth configured)" ;;
        *) echo "denied or timed out" ;;
    esac
}

BYPASS_HINT="emergency bypass: /opt/homebrew/bin/tmux kill-ser (see ~/.config/tmux/docs/kill-guard.md)"

server_alive() {
    [ "$("$REAL_TMUX" -S "$1" display -p x 2>/dev/null)" = x ]
}

# tell SOCKET MESSAGE: a status-line message on the server (best effort).
tell() {
    "$REAL_TMUX" -S "$1" display-message -d 10000 "$2" 2>/dev/null
}

# ---------------------------------------------------------------- save first
# Mack: "tmux kill server [must] literally not be allowed without saving
# everything first" on the primary server. After Touch ID and BEFORE any
# protected kill:
#   flag    set -g @kill_guard_shutdown <epoch>  (stash.sh / the savers skip
#           their work while it is set AND younger than 120 s, so teardown hooks
#           can't wipe state, and a flag orphaned by a crash expires by itself)
#   publish stash.sh publish               (mirror the parked-tab sidecar)
#   save    resurrect-save.sh final        (full, synchronous, waits for the lock)
#   verify  rc 0 + `last`'s target stamped at/after the save started + a new
#           "saved N panes" line in save.log
# Steps publish..verify share a SAVE_BUDGET-second cap. Any failure unsets the
# flag and the kill is refused. INT/TERM/HUP between flag and kill unset it too.
SCRIPTS="$HOME/.config/tmux/scripts"
SAVE_BUDGET=30
SAVE_WHY=""
FLAG_SOCK=""      # socket whose @kill_guard_shutdown this process has set
CAPPED_PID=""     # running capped supervisor, for the signal trap

# The supervisor behind `capped`: the command runs in its OWN process group;
# on the deadline (or a TERM/INT/HUP to the supervisor) the whole group gets
# TERM then KILL, so post-save hooks don't linger. Exit: the command's status,
# 142 on timeout, 143 when stopped by a signal.
CAPPED_PERL='
my $t = shift;
my $pid = fork;
defined $pid or exit 127;
if (!$pid) { setpgrp(0, 0); exec { $ARGV[0] } @ARGV; exit 127; }
eval { setpgrp($pid, $pid) };
my $stop = sub {
    kill "TERM", -$pid;
    select(undef, undef, undef, 0.3);
    kill "KILL", -$pid;
    exit $_[0];
};
$SIG{ALRM} = sub { $stop->(142) };
$SIG{TERM} = $SIG{INT} = $SIG{HUP} = sub { $stop->(143) };
alarm $t;
waitpid($pid, 0);
my $st = $?;
alarm 0;
exit($st & 127 ? 128 + ($st & 127) : $st >> 8);
'

# capped SECS CMD...: run CMD under the supervisor. It runs in the background
# and is waited for, so a trapped signal interrupts the wait at once (bash
# defers traps until a FOREGROUND child exits). Output goes to the guard log
# so nothing leaks into run-shell's output or the caller's pipe.
capped() {
    local secs="$1" rc
    shift
    /bin/mkdir -p "${LOG%/*}" 2>/dev/null
    /usr/bin/perl -e "$CAPPED_PERL" "$secs" "$@" </dev/null >>"$LOG" 2>&1 &
    CAPPED_PID=$!
    wait "$CAPPED_PID"
    rc=$?
    CAPPED_PID=""
    return "$rc"
}

on_signal() {
    trap - INT TERM HUP
    [ -n "$CAPPED_PID" ] && kill -TERM "$CAPPED_PID" 2>/dev/null
    if [ -n "$FLAG_SOCK" ]; then
        unflag "$FLAG_SOCK"
        log "aborted by signal; shutdown flag cleared, nothing killed by the guard socket=$FLAG_SOCK"
    fi
    exit 1
}

set_flag() {
    "$REAL_TMUX" -S "$1" set -g @kill_guard_shutdown "$(/bin/date +%s)" 2>/dev/null || return 1
    FLAG_SOCK="$1"
    trap on_signal INT TERM HUP
    log "step=flag-set socket=$1"
}

unflag() {
    "$REAL_TMUX" -S "$1" set -gu @kill_guard_shutdown 2>/dev/null
    log "step=flag-unset socket=$1"
}

# fail_save WHY: unset the flag, disarm the trap, record why.
fail_save() {
    SAVE_WHY="$1"
    trap - INT TERM HUP
    unflag "$FLAG_SOCK"
    FLAG_SOCK=""
}

# Children parse \x1f-separated tmux -F output; a client without a UTF-8
# locale prints control characters as "_". Make sure they get one.
ensure_utf8() {
    local cur="${LC_ALL:-${LC_CTYPE:-${LANG:-}}}"
    case "$cur" in
        *[Uu][Tt][Ff]-8*|*[Uu][Tt][Ff]8*) return 0 ;;
    esac
    export LANG=en_US.UTF-8 LC_CTYPE=en_US.UTF-8
    [ -n "${LC_ALL:-}" ] && export LC_ALL=en_US.UTF-8
}

script_cmd() {
    if [ -x "$SCRIPTS/$1" ]; then echo "$SCRIPTS/$1"; else echo ""; fi
}

# bash4: a bash >= 4 to run stash.sh / resurrect-save.sh with (they need
# declare -A, EPOCHREALTIME...). Their `#!/usr/bin/env bash` shebang would pick
# macOS's 3.2 for a caller whose PATH lacks Homebrew. Homebrew's bash first,
# then the first bash on PATH that reports BASH_VERSINFO >= 4.
bash4() {
    local d IFS=:
    for d in /opt/homebrew/bin $PATH; do
        [ -n "$d" ] && [ -x "$d/bash" ] || continue
        if "$d/bash" -c '[ "${BASH_VERSINFO[0]}" -ge 4 ]' 2>/dev/null; then
            echo "$d/bash"
            return 0
        fi
    done
    return 1
}

# child_socket SOCKET: the path the save/publish children get as $TMUX. The
# scripts check for the real default by PATH, so a symlink (-S ~/link) that
# same_socket matched is replaced by the canonical path it stands for.
child_socket() {
    if is_real_default "$1"; then
        canon "/tmp/tmux-$UID/default"
    elif [ -n "${KILL_GUARD_PROTECT_SOCKET:-}" ] && same_socket "$1" "$KILL_GUARD_PROTECT_SOCKET"; then
        canon "$KILL_GUARD_PROTECT_SOCKET"
    else
        printf '%s\n' "$1"
    fi
}

# save_first SOCKET -> 0 when state is safely on disk (flag left SET, trap
# armed), 1 otherwise (flag unset again, SAVE_WHY says why).
save_first() {
    local sock="$1" publish_cmd="" save_cmd="" resdir start size now left rc mt tgt csock bash
    # Test stubs: honoured only off the real default server, like KILL_GUARD_AUTH.
    if ! is_real_default "$sock"; then
        publish_cmd="${KILL_GUARD_PUBLISH_CMD:-}"
        save_cmd="${KILL_GUARD_SAVE_CMD:-}"
    fi
    if ! set_flag "$sock"; then
        SAVE_WHY="could not set @kill_guard_shutdown (server not reachable?)"
        return 1
    fi
    resdir="$("$REAL_TMUX" -S "$sock" show -gqv @resurrect-dir 2>/dev/null)"
    resdir="${resdir:-$HOME/.tmux/resurrect}"
    resdir="${resdir//\$HOME/$HOME}"
    resdir="${resdir/#\~/$HOME}"
    # Children talk to THIS server whatever the caller's environment said, by
    # its canonical path (see child_socket).
    csock="$(child_socket "$sock")"
    export TMUX="$csock" KILL_GUARD_SOCKET="$csock" KILL_GUARD_LOG="$LOG"
    unset TMUX_PANE
    ensure_utf8
    if [ -z "$publish_cmd" ] || [ -z "$save_cmd" ]; then
        if ! bash="$(bash4)"; then
            fail_save "no bash >= 4 found (/opt/homebrew/bin/bash or on PATH) to run the save scripts"
            return 1
        fi
    fi

    start="$(/bin/date +%s)"
    if [ -n "$publish_cmd" ]; then
        capped 10 /bin/bash -c "$publish_cmd"
    elif [ -n "$(script_cmd stash.sh)" ]; then
        capped 10 "$bash" "$SCRIPTS/stash.sh" publish
    else
        fail_save "$SCRIPTS/stash.sh is not installed"; return 1
    fi
    rc=$?
    if [ "$rc" -ne 0 ]; then
        if [ "$rc" -eq 142 ]; then fail_save "stash publish timed out after 10s"
        else fail_save "stash publish failed (rc=$rc)"; fi
        return 1
    fi
    log "step=publish-ok socket=$sock"

    now="$(/bin/date +%s)"
    left=$((SAVE_BUDGET - (now - start)))
    [ "$left" -lt 5 ] && left=5
    start="$(/bin/date +%s)"
    size="$($STAT -f %z "$resdir/save.log" 2>/dev/null || echo 0)"
    if [ -n "$save_cmd" ]; then
        capped "$left" /bin/bash -c "$save_cmd"
    elif [ -n "$(script_cmd resurrect-save.sh)" ]; then
        capped "$left" "$bash" "$SCRIPTS/resurrect-save.sh" final
    else
        fail_save "$SCRIPTS/resurrect-save.sh is not installed"; return 1
    fi
    rc=$?
    if [ "$rc" -ne 0 ]; then
        if [ "$rc" -eq 142 ]; then fail_save "resurrect save timed out after ${left}s"
        else fail_save "resurrect save failed (rc=$rc)"; fi
        return 1
    fi
    tgt="$resdir/last"
    mt="$($STAT -L -f %m "$tgt" 2>/dev/null)"
    if [ -z "$mt" ] || [ "$mt" -lt "$start" ]; then
        fail_save "save reported success but $tgt is missing or older than the save"
        return 1
    fi
    if [ "$($STAT -f %z "$resdir/save.log" 2>/dev/null || echo 0)" -lt "$size" ]; then size=0; fi
    if ! /usr/bin/tail -c +$((size + 1)) "$resdir/save.log" 2>/dev/null |
            /usr/bin/grep -q 'saved [0-9][0-9]* panes'; then
        fail_save "save reported success but $resdir/save.log has no new \"saved N panes\" line"
        return 1
    fi
    log "step=save-verified socket=$sock resurrect_dir=$resdir"
    return 0
}

# ---------------------------------------------------------------- server mode

mode_server() {
    local sock requester reason rc label
    sock="$(canon "$1")"
    if ! is_protected "$sock"; then
        log "mode=server socket=$sock decision=unprotected-kill"
        [ -n "${KILL_GUARD_DRYRUN:-}" ] && { echo "kill-guard: verdict=passthrough socket=$sock"; exit 0; }
        exec "$REAL_TMUX" -S "$sock" kill-ser
        exit 1
    fi
    # The process chain first: run-shell's #{client_name} falls back to the most
    # recently active ATTACHED client (Mack's own tty), which would blame his
    # terminal for an agent's kill.
    requester="$(proc_chain - kill-server)"
    [ -n "$requester" ] ||
        requester="no tmux CLI process found (key binding, command prompt, hook or config file)"
    label="${sock##*/}"
    reason="kill the tmux server \"$label\" and every session in it. Requested by: $requester"
    if [ -n "${KILL_GUARD_DRYRUN:-}" ]; then
        echo "kill-guard: verdict=protect socket=$sock real_default=$(is_real_default "$sock" && echo 1 || echo 0) auth=$(auth_source "$sock") reason=$reason"
        exit 0
    fi
    authorize "$sock" "$reason"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log "mode=server socket=$sock decision=refused rc=$rc requester=$requester"
        # run-shell shows stdout (stderr is dropped) and turns exit 1 into an error.
        echo "kill-guard: refused to kill tmux server \"$label\": $(describe_rc "$rc"). $BYPASS_HINT"
        exit 1
    fi
    log "step=auth-approved mode=server socket=$sock requester=$requester"
    if ! save_first "$sock"; then
        log "mode=server socket=$sock decision=refused-save-failed why=[$SAVE_WHY] requester=$requester"
        tell "$sock" "kill-guard: kill-server refused, state not saved: $SAVE_WHY"
        echo "kill-guard: refused to kill tmux server \"$label\": could not save first: $SAVE_WHY. $BYPASS_HINT"
        echo "kill-guard: refused to kill tmux server \"$label\": could not save first: $SAVE_WHY" >&2
        exit 1
    fi
    log "mode=server socket=$sock decision=approved-kill requester=$requester"
    exec "$REAL_TMUX" -S "$sock" kill-ser
    # Only reached when exec failed (execfail).
    unflag "$sock"
    log "mode=server socket=$sock decision=exec-failed"
    echo "kill-guard: could not run $REAL_TMUX; server left running"
    exit 1
}

# ---------------------------------------------------------------- cli mode

# Globals filled by the parser.
SOCK=""           # canonical socket path the call would reach
PROTECT_WHY=""    # "; "-joined descriptions of protected kills
NEED_AUTH=0
SESS_LEFT=""      # sessions still alive as the chain proceeds (lazy)

# passthrough ARGS: the ONLY place cli mode hands the call to the real tmux
# unchanged; under KILL_GUARD_DRYRUN it prints the verdict instead.
passthrough() {
    if [ -n "${KILL_GUARD_DRYRUN:-}" ]; then
        echo "kill-guard: verdict=passthrough socket=${SOCK:-?}"
        exit 0
    fi
    exec "$REAL_TMUX" "$@"
    echo "tmux shim: cannot exec $REAL_TMUX" >&2
    exit 127
}

add_protect() {
    NEED_AUTH=1
    if [ -z "$PROTECT_WHY" ]; then PROTECT_WHY="$1"; else PROTECT_WHY="$PROTECT_WHY; $1"; fi
}

# in_list WORD LIST: WORD is one of LIST's space-separated words (no globbing).
in_list() {
    local w found=1
    set -f
    for w in $2; do
        [ "$w" = "$1" ] && { found=0; break; }
    done
    set +f
    return "$found"
}

# eval_command WORD...: one command of the parsed list.
eval_command() {
    local name="$1" a=0 C=0 t="" have_t=0 arg i ch out sname count plist
    shift
    [ -z "$name" ] && return 0
    case "$name" in
        kill-server)
            # The server alias owns the full word. Prompt here only when the
            # server lacks it (started without tmux.conf), so never twice.
            if ! "$REAL_TMUX" -S "$SOCK" show -sv command-alias 2>/dev/null |
                    /usr/bin/grep -q '^"\{0,1\}kill-server='; then
                server_alive "$SOCK" &&
                    add_protect "kill the whole tmux server (no kill-guard alias loaded)"
            fi
            return 0 ;;
    esac
    # Abbreviated kill-server: kill-ser .. kill-serve (tmux accepts unique
    # prefixes; the alias matches only the full word). No server, no prompt.
    if [ ${#name} -ge 8 ] && [ "${name}" = "${name%%[!a-z-]*}" ]; then
        case "kill-server" in
            "$name"*)
                if [ -n "${KILL_GUARD_DRYRUN:-}" ] || server_alive "$SOCK"; then
                    add_protect "kill the whole tmux server ($name)"
                fi
                return 0 ;;
        esac
    fi
    # kill-session or a unique prefix of it (kill-ses ..).
    [ ${#name} -ge 8 ] || return 0
    case "kill-session" in "$name"*) ;; *) return 0 ;; esac
    # getopt "aCt:"
    while [ $# -gt 0 ]; do
        arg="$1"
        case "$arg" in
            --) shift; break ;;
            -) break ;;
            -*)
                i=1
                while [ $i -lt ${#arg} ]; do
                    ch="${arg:$i:1}"
                    case "$ch" in
                        a) a=1 ;;
                        C) C=1 ;;
                        t)
                            have_t=1
                            if [ $((i + 1)) -lt ${#arg} ]; then
                                t="${arg:$((i + 1))}"
                            else
                                shift
                                [ $# -gt 0 ] || return 0   # -t without value: tmux errors
                                t="$1"
                            fi
                            break ;;
                        *) return 0 ;;                       # unknown flag: tmux errors
                    esac
                    i=$((i + 1))
                done
                shift ;;
            *) break ;;
        esac
    done
    [ $# -gt 0 ] && return 0          # stray positional args: tmux errors out
    [ "$C" -eq 1 ] && return 0        # -C only clears alerts, kills nothing
    if [ "$a" -eq 1 ]; then
        add_protect "kill every session except ${t:-the current one}"
        return 0
    fi
    # Resolve the target the way kill-session does: list-windows -t is a
    # SESSION-type lookup (=name, name:, $id, prefixes), and with no -t it is
    # the current session. display -t would be a PANE lookup, where "main" can
    # match a window called main in the caller's session and "=main" errors.
    if [ "$have_t" -eq 1 ]; then
        out="$("$REAL_TMUX" -S "$SOCK" list-windows -t "$t" -F "#{session_name}${TAB}#{server_sessions}${TAB}#{@kill-guard-protect}" 2>/dev/null)" || return 0
    else
        out="$("$REAL_TMUX" -S "$SOCK" list-windows -F "#{session_name}${TAB}#{server_sessions}${TAB}#{@kill-guard-protect}" 2>/dev/null)" || return 0
    fi
    out="${out%%$'\n'*}"
    [ -n "$out" ] || return 0
    sname="${out%%${TAB}*}"; out="${out#*${TAB}}"
    count="${out%%${TAB}*}"; plist="${out#*${TAB}}"
    [ -n "$plist" ] || plist="main"
    [ -n "$SESS_LEFT" ] || SESS_LEFT="$count"
    if in_list "$sname" "$plist"; then
        add_protect "kill protected session \"$sname\""
        return 0
    fi
    case "$SESS_LEFT" in ''|*[!0-9]*) return 0 ;; esac
    if [ "$SESS_LEFT" -le 1 ]; then
        add_protect "kill \"$sname\", the last session (the server would exit)"
        return 0
    fi
    SESS_LEFT=$((SESS_LEFT - 1))
    return 0
}

mode_cli() {
    local S="" L="" i j ch arg n rest sock requester rc
    local -a cmd args
    n=$#
    args=("$@")
    # --- global flags, getopt "2c:CDdf:hlL:NqS:T:uUvV"
    i=0
    while [ $i -lt $n ]; do
        arg="${args[$i]}"
        case "$arg" in
            --) i=$((i + 1)); break ;;
            -) break ;;
            -*) ;;
            *) break ;;
        esac
        j=1
        while [ $j -lt ${#arg} ]; do
            ch="${arg:$j:1}"
            case "$ch" in
                c|f|L|S|T)
                    if [ $((j + 1)) -lt ${#arg} ]; then
                        rest="${arg:$((j + 1))}"
                    else
                        i=$((i + 1))
                        [ $i -lt $n ] || passthrough "$@"
                        rest="${args[$i]}"
                    fi
                    case "$ch" in
                        c) passthrough "$@" ;;          # shell-command mode
                        L) L="$rest" ;;
                        S) S="$rest" ;;
                    esac
                    break ;;
                2|C|D|d|h|l|N|q|u|U|v|V) ;;
                *) passthrough "$@" ;;
            esac
            j=$((j + 1))
        done
        i=$((i + 1))
    done
    # --- which socket (tmux's own order: -S, -L, $TMUX, default). tmux ignores
    # a TMUX value that starts with "," (empty socket part).
    if [ -n "$S" ]; then
        sock="$S"
    elif [ -n "$L" ]; then
        sock="${TMUX_TMPDIR:-/tmp}/tmux-$UID/$L"
    elif [ -n "${TMUX:-}" ] && [ "${TMUX#,}" = "$TMUX" ]; then
        sock="${TMUX%%,*}"
    else
        sock="${TMUX_TMPDIR:-/tmp}/tmux-$UID/default"
    fi
    SOCK="$(canon "$sock")"
    is_protected "$SOCK" || passthrough "$@"
    # --- command list: standalone ";" or a trailing unescaped ";" ends a command
    cmd=()
    while [ $i -lt $n ]; do
        arg="${args[$i]}"
        i=$((i + 1))
        if [ "$arg" = ";" ]; then
            eval_command "${cmd[@]}"; cmd=(); continue
        fi
        case "$arg" in
            *\\\;) cmd[${#cmd[@]}]="${arg%??};" ;;
            *\;) cmd[${#cmd[@]}]="${arg%?}"; eval_command "${cmd[@]}"; cmd=() ;;
            *) cmd[${#cmd[@]}]="$arg" ;;
        esac
    done
    [ ${#cmd[@]} -gt 0 ] && eval_command "${cmd[@]}"

    if [ "$NEED_AUTH" -eq 0 ]; then
        if [ -n "${KILL_GUARD_DRYRUN:-}" ]; then
            # Dry-run never executes anything (passthrough checks too).
            echo "kill-guard: verdict=passthrough socket=$SOCK"
            exit 0
        fi
        log "mode=cli socket=$SOCK decision=pass argv=$*"
        passthrough "$@"
    fi
    requester="$(proc_chain "$PPID")"
    [ -n "$requester" ] || requester="pid $PPID"
    if [ -n "${KILL_GUARD_DRYRUN:-}" ]; then
        echo "kill-guard: verdict=protect socket=$SOCK real_default=$(is_real_default "$SOCK" && echo 1 || echo 0) auth=$(auth_source "$SOCK") reason=$PROTECT_WHY"
        exit 0
    fi
    authorize "$SOCK" "$PROTECT_WHY on tmux server \"${SOCK##*/}\". Requested by: $requester"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        log "mode=cli socket=$SOCK decision=refused rc=$rc what=[$PROTECT_WHY] requester=$requester argv=$*"
        echo "kill-guard: refused to $PROTECT_WHY: $(describe_rc "$rc"). $BYPASS_HINT" >&2
        exit 1
    fi
    log "step=auth-approved mode=cli socket=$SOCK what=[$PROTECT_WHY] requester=$requester"
    if ! save_first "$SOCK"; then
        log "mode=cli socket=$SOCK decision=refused-save-failed why=[$SAVE_WHY] what=[$PROTECT_WHY] argv=$*"
        tell "$SOCK" "kill-guard: refused ($PROTECT_WHY), state not saved: $SAVE_WHY"
        echo "kill-guard: refused to $PROTECT_WHY: could not save first: $SAVE_WHY. $BYPASS_HINT" >&2
        exit 1
    fi
    log "mode=cli socket=$SOCK decision=approved what=[$PROTECT_WHY] requester=$requester argv=$*"
    # Not exec: the shutdown flag comes off again afterwards unless the server
    # went away with the kill (the INT/TERM/HUP trap stays armed meanwhile).
    # save_first exported TMUX=<this socket>; the caller's own value goes back.
    if [ -n "$CALLER_TMUX_SET" ]; then export TMUX="$CALLER_TMUX"; else unset TMUX; fi
    if [ -n "$CALLER_PANE_SET" ]; then export TMUX_PANE="$CALLER_PANE"; fi
    "$REAL_TMUX" "$@"
    rc=$?
    trap - INT TERM HUP
    server_alive "$SOCK" && unflag "$SOCK"
    exit "$rc"
}

# ---------------------------------------------------------------- main

CALLER_TMUX="${TMUX-}"; CALLER_TMUX_SET="${TMUX+1}"
CALLER_PANE="${TMUX_PANE-}"; CALLER_PANE_SET="${TMUX_PANE+1}"
mode="$1"
shift
case "$mode" in
    server) mode_server "$@" ;;
    cli) mode_cli "$@" ;;
    auth-source) auth_source "$1" ;;
    save-source)
        # Which save/publish commands a protected kill on SOCKET would run.
        if ! is_real_default "$1" && [ -n "${KILL_GUARD_SAVE_CMD:-}" ]; then echo "save=stub"; else echo "save=scripts"; fi
        if ! is_real_default "$1" && [ -n "${KILL_GUARD_PUBLISH_CMD:-}" ]; then echo "publish=stub"; else echo "publish=scripts"; fi ;;
    *) echo "usage: kill-guard.sh server SOCKET | cli TMUX-ARGS... | auth-source SOCKET | save-source SOCKET" >&2; exit 64 ;;
esac
