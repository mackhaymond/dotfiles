#!/usr/bin/env bash
# agent-refresh — restart idle, outdated Claude Code tabs IN PLACE, in the
# background, so they come back on the newest installed version.
#
#   agent-refresh.sh check               the timer tick (LaunchAgent
#                                        com.mackhaymond.tmux-agent-refresh,
#                                        every 600 s)
#   agent-refresh.sh --dry-run           every agent: versions, idle time, and
#                                        the resume line or why it is skipped;
#                                        changes nothing, writes nothing
#   agent-refresh.sh once @W [--force-idle] [--dry-run]
#                                        one window by hand; --force-idle skips
#                                        only the 2 h rule
#
# docs/agent-refresh.md has the rules in prose. In short: a visible, one-pane,
# non-home claude tab (no abby), `idle` (so already seen) for 2 h+ (past the 1 h
# prompt-cache TTL: a resume then costs nothing extra), not on screen, running
# an older version than ~/.local/bin/claude links to, with nothing inside the
# process that a restart would lose, and that `stash.sh check` would park.
# At most 3 per tick, one after another, longest idle first.
#
# A RESTART IS: write a journal record, look again, SIGTERM (never -9), wait for
# the pane's own shell to be at its prompt, type the resume command there (off
# screen only), wait for the NEW process of that conversation on the new version
# and for its SessionStart, then put back the window options
# SessionEnd/SessionStart wiped or restamped: above all @agent_since, which
# keeps its OLD epoch, so the agent's age and the review popup's 8 h clock do
# not reset. If anything moved on meanwhile (Mack used the tab), those options
# are left alone.
#
# NOTHING HERE WAITS FOREVER: every tmux, python, jq, stash.sh, closed-tabs.sh
# and notify call is bounded (`bounded`), a watchdog ends a tick after 8 min,
# and a lock older than 15 min is taken over. A stall between the kill and the
# typing leaves the journal, and the next tick goes on from it.
#
# REFUSALS are not reimplemented here: `stash.sh check` runs the deep guards
# (busy, caffeinate, composer, workflow, subagent, cua), closed-tabs.sh builds
# the resume command (agent_snapshot / agent_session / build_cmd, as
# stash.sh's pin_dry_why does), and agent-bg-pending's detectors answer for
# background tasks. What is added here is what only a restart loses without a trace: an
# armed ScheduleWakeup, a cron, a Monitor, and the model it is on (HELPER scan).
#
# CRASH RECOVERY: the journal is written before the kill and removed once the
# agent is verified back, so a tick that died (logout, a reboot, a kill) is
# finished by the next one: the resume is typed at most ONCE more; after that
# the resume command goes to Mack in a toast. A failed (window, session,
# version) is recorded and never tried again.
#
# State: ${XDG_STATE_HOME:-~/.local/state}/agent-refresh/ — journal/*.json
# (restarts in flight), archive/*.json (given up on), toasts/ (failures Mack may
# not have seen), failed, log (bounded), lock/, launchd.log.
#
# Environment (tests): AGENT_REFRESH_NOW (a fixed "now"; the age rules only),
# AGENT_REFRESH_ALLOW_SOCKET (the one other server it may run on, its exact
# socket path), AGENT_REFRESH_MCP_URLS (set = this list, even empty),
# AGENT_REFRESH_EXIT_TICKS / _BACK_TICKS (0.25 s each), _TMUX_TIMEOUT,
# _TICK_MAX (s), STASH_SH, CLOSED_TABS_SH, NOTIFY_SH, SAVE_SOON_SH,
# AGENT_BG_PENDING, AGENT_PAUSE_STATE_DIR, AGENT_STATE_NOTICE, XDG_STATE_HOME.
#
# Bash 3.2 (launchd runs /bin/bash): no associative arrays. closed-tabs.sh and
# stash.sh run under the `bash` on PATH (the LaunchAgent's has Homebrew's).
# `check` always exits 0; `once`: 0 restarted, 3 refused, 1 failed.
# shellcheck disable=SC2154  # the j_* are j_load's, by eval
set -uo pipefail

# A UTF-8 locale, always (as review-nudge.sh): launchd gives none, and a tmux
# client without one prints \x1f, tabs and non-ASCII as "_".
case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
    *[Uu][Tt][Ff]-8*|*[Uu][Tt][Ff]8*) ;;
    *)
        if [ -n "${LC_ALL:-}" ]; then export LC_ALL=en_US.UTF-8
        elif [ -n "${LC_CTYPE:-}" ]; then export LC_CTYPE=en_US.UTF-8
        else export LANG=en_US.UTF-8
        fi ;;
esac
# The per-process session records (closed-tabs.sh's source) live under TMPDIR.
if [ -z "${TMPDIR:-}" ]; then
    TMPDIR=$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null) && [ -n "$TMPDIR" ] && export TMPDIR || unset TMPDIR
fi

# `_ready TTY PID` (run by tmux inside type_line, right after its C-c): waits for
# PID, a shell, to be reading a fresh prompt: it holds its terminal and its line
# editor has it (-icanon; zsh and readline take it only while they read a line,
# not during precmd). Twice in a row, up to 5 s.
if [ "${1:-}" = _ready ]; then
    i=0 ok=0
    sleep 0.3
    while [ "$i" -lt 50 ]; do
        if [ "$(/bin/ps -o tpgid= -p "$3" 2>/dev/null | tr -d ' ')" = "$3" ] \
            && /bin/stty -f "$2" -a 2>/dev/null | grep -q '[[:space:]]-icanon'; then ok=$((ok + 1)); else ok=0; fi
        [ "$ok" -ge 2 ] && exit 0
        sleep 0.1; i=$((i + 1))
    done
    exit 1
fi

# bounded SECONDS CMD...: CMD, its whole process group killed (124) when it
# takes longer. No stdin. (macOS has no timeout(1).)
bounded() {
    PERL_BADLANG=0 perl -e 'my $t = shift; my $p = fork; exit 127 unless defined $p;
        if (!$p) { setpgrp(0, 0); open STDIN, "<", "/dev/null"; exec @ARGV; exit 127 }
        $SIG{ALRM} = sub { kill "KILL", -$p; waitpid $p, 0; exit 124 };
        alarm $t; waitpid $p, 0; exit($? & 127 ? 128 + ($? & 127) : $? >> 8)' "$@"
}
tmux() { bounded "${AGENT_REFRESH_TMUX_TIMEOUT:-10}" tmux "$@"; }

MIN_IDLE=7200        # s idle before a restart (past the 1 h prompt cache)
MIN_INSTALL_AGE=600  # s since the installed binary was written
MAX_PER_TICK=3
EXIT_TICKS=${AGENT_REFRESH_EXIT_TICKS:-120}   # 0.25 s each: 30 s to exit, at a prompt
BACK_TICKS=${AGENT_REFRESH_BACK_TICKS:-360}   # 90 s to come back
TICK_MAX=${AGENT_REFRESH_TICK_MAX:-480}       # s: the watchdog ends the tick
TICK_START_MAX=200   # s into a tick: no further restart is begun
LOCK_MAX=900         # s: an older lock's holder is hung, and ended
JOURNAL_GIVE_UP=1800 # s after which a resume that still cannot be typed is handed to Mack

US=$'\x1f'
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/agent-refresh"
JOURNAL="$STATE_DIR/journal"
ARCHIVE="$STATE_DIR/archive"
FAILED="$STATE_DIR/failed"
LOGFILE="$STATE_DIR/log"
LOCK="$STATE_DIR/lock"
TOASTS="$STATE_DIR/toasts"

# What a restart may lose from the window: SessionEnd's clear_state unsets
# @agent_since, _summary*, _detail* and _harness (agent-tab-indicator.sh), and
# SessionStart restamps since and the summary. @agent_pinned and @review_kept
# are touched by neither today; they are here in case that changes.
SAVE_OPTS="@agent_since @agent_summary @agent_summary_cond @agent_summary_key @agent_detail @agent_detail_kind @agent_harness @agent_pinned @review_kept"

dry=0 force_idle=0 verb="" only=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run|-n) dry=1 ;;
        --force-idle) force_idle=1 ;;
        check) verb=check ;;
        once) verb=once; only="${2:-}"; [ $# -gt 1 ] && shift ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "agent-refresh.sh: unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done
[ -z "$verb" ] && [ "$dry" = 1 ] && verb=check
if [ -z "$verb" ]; then
    echo "usage: agent-refresh.sh check | --dry-run | once @W [--force-idle] [--dry-run]" >&2
    exit 2
fi
if [ "$verb" = once ]; then
    case "$only" in
        @*[!0-9]*|@|'') echo "agent-refresh.sh: once needs a window id (@12)" >&2; exit 2 ;;
        @*) ;;
        *) echo "agent-refresh.sh: once needs a window id (@12)" >&2; exit 2 ;;
    esac
fi

now() { if [ -n "${AGENT_REFRESH_NOW:-}" ]; then printf '%s' "$AGENT_REFRESH_NOW"; else date +%s; fi; }

# The decision: printed on a dry run (and by `once`), logged otherwise.
say() {
    if [ "$dry" = 1 ]; then printf '%s\n' "$*"; return 0; fi
    [ "$verb" = once ] && printf '%s\n' "$*"
    mkdir -p "$STATE_DIR" 2>/dev/null
    printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOGFILE" 2>/dev/null
    local n
    n=$(wc -l <"$LOGFILE" 2>/dev/null | tr -d ' ')
    if [ "${n:-0}" -gt 1000 ] 2>/dev/null; then
        tail -n 500 "$LOGFILE" >"$LOGFILE.$$" 2>/dev/null && mv -f "$LOGFILE.$$" "$LOGFILE"
    fi
    return 0
}

# The scripts beside this one (installed: stash.sh; in the chezmoi source:
# executable_stash.sh).
here=${BASH_SOURCE[0]%/*}
[ "$here" = "${BASH_SOURCE[0]}" ] && here=.
here=$(cd "$here" 2>/dev/null && pwd) || here="$HOME/.config/tmux/scripts"
sibling() {
    local n
    for n in "$here/$1" "$here/executable_$1" "$HOME/.config/tmux/scripts/$1"; do
        [ -f "$n" ] && { printf '%s\n' "$n"; return 0; }
    done
    printf '%s\n' "$here/$1"
}
STASH_SH="${STASH_SH:-$(sibling stash.sh)}"
CLOSED_TABS_SH="${CLOSED_TABS_SH:-$(sibling closed-tabs.sh)}"
NOTIFY_SH="${NOTIFY_SH:-$(sibling notify.sh)}"
SAVE_SOON_SH="${SAVE_SOON_SH:-$(sibling resurrect-save-soon.sh)}"
BG_PENDING="${AGENT_BG_PENDING:-$HOME/.local/bin/agent-bg-pending}"
export STASH_SH CLOSED_TABS_SH

squote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

# --- HELPER -------------------------------------------------------------------
# The JSON work. `sessions`: one US-separated row per readable
# ~/.claude/sessions file. `scan`: what a restart would lose (below). `tick`: a
# line per reason nothing may restart now. `bg`: agent-bg-pending's own
# detectors, called directly: its exit status cannot say "I failed" (an
# exception exits 1, "nothing is out"), so here one is exit 2. `setmodel CMD
# MODEL`: the resume command with exactly that --model ("" keeps its own). `jset FILE k v...`: write/merge a journal
# record atomically. `jload FILE`: its fields as shell assignments (j_<key>; the
# saved options are in j_opts, "name US value" RS-separated).
#
# scan <transcript> <process start epoch> <now>: exit 0 = nothing armed (stdout:
# the model this process's last main-thread reply was asked of, or nothing), 3 =
# something is (its line on stdout), anything else = could not tell. As the transcripts record it
# (2.1.300-2.1.330):
#   ScheduleWakeup  tool_result's toolUseResult.scheduledFor (ms); {"stop":
#                   true} answers with "stopped": true and cancels them all;
#   CronCreate      toolUseResult.id, "nextFireAt"; gone once a CronDelete of
#                   that id did not fail. Crons outlive a process (--resume
#                   brings unexpired ones back), so one counts whoever made
#                   it, for 8 days (they expire after 7), or while its next
#                   fire is still ahead;
#   Monitor         toolUseResult.taskId, timeoutMs, persistent; over with a
#                   <task-notification> for that id that carries a <status>
#                   (one without is an event), a TaskStop of it, or its
#                   timeout. It dies with its process, so only one started in
#                   this process's lifetime counts.
# THE MODEL is the last assistant record's requestedModel (never a sidechain's
# or a <synthetic> one, nor an earlier process's). A last turn run under a skill
# (attributionSkill) may be on the skill's model, and a /model after the last
# reply shows in no reply yet: both refuse.
# A call whose result is missing or unreadable counts as armed (an error result
# arms nothing). api-request-* records repeat old calls with no timestamp and
# are skipped, as is anything but the harness's own records (text a model or a
# command printed is no notification). Lines are prefiltered as bytes: these
# files reach 108 MB.
helper() {
    bounded 60 python3 -I -c '
import glob, json, os, re, sys, time
HOME = os.path.expanduser("~")
US = "\x1f"

def epoch(ts):
    from datetime import datetime, timezone
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None

def num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

def sessions():
    for f in sorted(glob.glob(os.path.join(HOME, ".claude/sessions/*.json"))):
        try:
            d = json.load(open(f))
            m = re.search(r":(@\d+)\.(%\d+)$", d.get("tmux") or "")
            if not m:
                continue
            sid, cwd = d["sessionId"], d["cwd"]
            if not re.match(r"^[A-Za-z0-9_-]+$", sid):
                continue
            proj = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
            tp = os.path.join(HOME, ".claude/projects", proj, sid + ".jsonl")
            row = [d["pid"], sid, d.get("version") or "", d.get("status") or "", m.group(1), m.group(2), cwd,
                   int(d.get("startedAt") or 0) // 1000, d.get("procStart") or "", tp if os.path.isfile(tp) else ""]
            row = [str(x) for x in row]
            if any(re.search(r"[\x00-\x1f\x7f]", x) for x in row):
                continue
            print(US.join(row))
        except Exception:
            continue

def scan(path, start, now):
    start, now = float(start), float(now)
    KEYS = (b"\"ScheduleWakeup\"", b"\"CronCreate\"", b"\"CronDelete\"", b"\"Monitor\"", b"\"TaskStop\"",
            b"\"KillShell\"")
    NAMES = {"ScheduleWakeup", "CronCreate", "CronDelete", "Monitor", "TaskStop", "KillShell"}
    pending = {}          # tool_use id -> (name, input, epoch)
    pend_b = []           # the same ids, bytes
    wake = 0.0
    model, skill, turn, switched = "", False, False, False
    crons, monitors = {}, {}
    notice = re.compile(r"<task-id>([^<]+)</task-id>")

    def notices(text):
        if not isinstance(text, str) or not text.lstrip().startswith("<task-notification>"):
            return
        for part in text.split("<task-notification>")[1:]:
            part = part.split("<event>", 1)[0]
            m = notice.search(part)
            s = re.search(r"<status>([^<]*)</status>", part)
            if m and s and s.group(1) not in ("running", "pending"):
                monitors.pop(m.group(1), None)

    def use(b, ts):
        i, name = b.get("id"), b.get("name")
        if name in NAMES and isinstance(i, str) and i not in pending:
            pending[i] = (name, b.get("input") if isinstance(b.get("input"), dict) else {}, ts)
            pend_b.append(i.encode())

    def result(b, tur):
        nonlocal wake
        i = b.get("tool_use_id")
        if i not in pending:
            return
        name, inp, ts = pending.pop(i)
        pend_b.remove(i.encode())
        if b.get("is_error"):
            return
        tur = tur if isinstance(tur, dict) else {}
        text = json.dumps(b.get("content"))
        if name == "ScheduleWakeup":
            if tur.get("stopped") or inp.get("stop"):
                wake = 0.0
                return
            at = num(tur.get("scheduledFor"))
            if at:
                wake = max(wake, at / 1000)
            else:
                wake = max(wake, (ts if ts is not None else now) + (num(inp.get("delaySeconds")) or 3600) + 120)
        elif name == "CronCreate":
            cid = tur.get("id")
            if not cid:
                m = re.search(r"(?:job|task) ([0-9a-f]{6,})", text)
                cid = m.group(1) if m else i
            fire = num(tur.get("nextFireAt"))
            crons[cid] = (ts, fire / 1000 if fire else None)
        elif name == "CronDelete":
            crons.pop(tur.get("id") or inp.get("id"), None)
        elif name == "Monitor":
            tid = tur.get("taskId")
            if not tid:
                m = re.search(r"Monitor started \(task ([A-Za-z0-9_-]+)", text)
                tid = m.group(1) if m else i
            t = num(tur.get("timeoutMs"))
            monitors[tid] = (ts, None if tur.get("persistent") or not t else t / 1000)
        elif name in ("TaskStop", "KillShell"):
            for k in ("task_id", "shell_id"):
                monitors.pop(inp.get(k), None)

    with open(path, "rb") as f:
        for line in f:
            if line.startswith(b"{\"type\":\"api-request"):
                continue
            reply = b"\"requestedModel\"" in line or b"\"attributionSkill\"" in line
            prompt = b"\"user\"" in line and b"\"tool_result\"" not in line
            slash = b"<command-name>/model<" in line
            if not (reply or prompt or slash or any(k in line for k in KEYS) or (pend_b and any(p in line for p in pend_b))
                    or (monitors and b"<task-id>" in line)):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                # A torn last line (being written), or a corrupt one: a call in
                # it cannot be read.
                print("unreadable transcript line")
                return 3
            t = d.get("type") if isinstance(d, dict) else None
            ts = epoch(d.get("timestamp")) if t else None
            if slash and t in ("user", "system"):
                switched = True
            if t == "assistant" and reply and not d.get("isSidechain") and (ts or 0) >= start:
                if turn:
                    skill = turn = False
                skill = skill or bool(d.get("attributionSkill"))
                m = d.get("requestedModel")
                if isinstance(m, str) and m != "<synthetic>" and (d.get("message") or {}).get("model") != "<synthetic>":
                    model, switched = m, False
            elif t == "user" and prompt and not d.get("isMeta"):
                turn = True
            if t == "queue-operation":
                notices(d.get("content"))
            elif t == "attachment":
                a = d.get("attachment")
                notices(a.get("prompt") if isinstance(a, dict) else None)
            if t not in ("assistant", "user"):
                continue
            c = (d.get("message") or {}).get("content")
            if isinstance(c, str):
                if t == "user":
                    notices(c)
                continue
            for b in c if isinstance(c, list) else []:
                if not isinstance(b, dict):
                    continue
                if t == "assistant" and b.get("type") == "tool_use":
                    use(b, ts)
                elif t == "user" and b.get("type") == "tool_result":
                    result(b, d.get("toolUseResult"))
                elif t == "user" and b.get("type") == "text":
                    notices(b.get("text"))
    # A call that never got a result.
    for name, inp, ts in pending.values():
        if name == "ScheduleWakeup" and not inp.get("stop"):
            wake = max(wake, (ts if ts is not None else now) + (num(inp.get("delaySeconds")) or 3600) + 120)
        elif name == "CronCreate":
            crons["?%d" % len(crons)] = (ts, None)
        elif name == "Monitor":
            monitors["?%d" % len(monitors)] = (ts, None)
    if wake > now:
        print("a wakeup is scheduled in %dm" % max(1, (wake - now) // 60))
        return 3
    for ts, fire in crons.values():
        if ts is None or now - ts < 8 * 86400 or (fire or 0) > now:
            print("a scheduled job (cron) is set")
            return 3
    for ts, limit in monitors.values():
        if ts is not None and ts < start - 1:
            continue
        if ts is not None and limit is not None and ts + limit + 60 < now:
            continue
        print("a Monitor is still running")
        return 3
    if switched:
        print("/model was used since its last reply")
        return 3
    if skill:
        print("its last turn ran under a skill")
        return 3
    if re.match(r"^claude-[a-z0-9.-]+(\[1m\])?$", model):
        print(model)
    return 0

def setmodel(cmd, model):
    import shlex
    q = lambda w: "\x27" + w.replace("\x27", "\x27\"\x27\"\x27") + "\x27"
    words, out, own, i = shlex.split(cmd), [], "", 0
    at = words.index("command")
    while i < len(words):
        w = words[i]
        if i > at and w == "--model":
            own, i = words[i + 1], i + 2
            continue
        if i > at and w.startswith("--model="):
            own, i = w[8:], i + 1
            continue
        if i < at:
            k, v = w.split("=", 1)
            out.append(k + "=" + q(v))
        elif w in ("command", "claude", "--resume"):
            out.append(w)
        else:
            out.append(q(w))
        i += 1
    if "opusplan" in own:
        print("it is on opusplan")
        return 3
    if not (model or own):
        print("no model known for its resume")
        return 3
    n = out.index("--resume")
    print(" ".join(out[:n] + ["--model", q(model or own)] + out[n:]))

def bg(prog, path, sid, start):
    from importlib.machinery import SourceFileLoader
    import types
    sys.dont_write_bytecode = True
    try:
        if not os.access(prog, os.X_OK):
            return 2
        m = types.ModuleType("agent_bg_pending")
        m.__file__ = prog
        SourceFileLoader("agent_bg_pending", prog).exec_module(m)
        start = float(start)
        proj, chain = m._session_chain(path)
        # Its own detectors read a file or a tmux they cannot reach as "none".
        if not start or chain[:1] != [sid] or m._pty_windows() is None or m._pty_procs(set()) is None:
            return 2
        for c in chain:
            open(os.path.join(proj, c + ".jsonl"), "rb").close()
        out = (m.agents_pending(path, start, True) or m.tasks_pending(path, proj, chain, start, True)
               or m.subagents_pending(proj, chain, start, start) or m.workflow_pending(proj, chain, start, start)
               or m.ptys_pending(set(chain), start, True, proj, chain))
        return 3 if out else 0
    except BaseException:
        return 2

def tick():
    out = []
    base = os.environ.get("AGENT_PAUSE_STATE_DIR") or os.path.join(HOME, ".local/state/agent-pause")
    p = os.path.join(base, "paused.json")
    try:
        if os.path.exists(p) and json.load(open(p)).get("targets"):
            out.append("agents are paused (agent-pause)")
    except Exception:
        out.append("agent-pause state unreadable")
    p = os.path.join(base, "progress.json")
    try:
        d = json.load(open(p))
        if d.get("mode") == "pause" and not d.get("done") and time.time() - os.stat(p).st_mtime < 600:
            out.append("agents are being paused (agent-pause)")
    except Exception:
        pass
    # Without it, resuming a big old session stops at a dialog.
    try:
        ok = json.load(open(os.path.join(HOME, ".claude.json"))).get("resumeReturnDismissed") is True
    except Exception:
        ok = False
    if not ok:
        out.append("Claude Code would ask how to resume (resumeReturnDismissed is not true)")
    try:
        if "opusplan" in str(json.load(open(os.path.join(HOME, ".claude/settings.json"))).get("model")):
            out.append("the default model is opusplan")
    except Exception:
        pass
    print("; ".join(out))

def jset(path, *kv):
    try:
        d = json.load(open(path))
    except Exception:
        d = {}
    for k, v in zip(kv[::2], kv[1::2]):
        d[k] = v
    tmp = "%s.%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def jload(path):
    import shlex
    try:
        d = json.load(open(path))
        opts = "\x1e".join(k[4:] + US + v for k, v in d.items() if k.startswith("opt:"))
        keys = ("window pane pane_pid pid sid cwd cmd line from to title ts server phase typed told").split()
        out = ["j_%s=%s" % (k, shlex.quote(str(d.get(k, "")))) for k in keys]
        print("\n".join(out + ["j_opts=" + shlex.quote(opts)]))
    except Exception:
        return 1

sys.exit({"sessions": sessions, "scan": scan, "tick": tick, "bg": bg, "setmodel": setmodel, "jset": jset, "jload": jload}[sys.argv[1]](*sys.argv[2:]) or 0)
' "$@"
}

# --- the server ----------------------------------------------------------------
# Only ever the default server, by exact path (assistant-restore.sh's rule and
# reasons): a private test server, under any TMUX_TMPDIR, is never it.
resolved_path() {
    local d
    d="$(cd -P "${1%/*}" 2>/dev/null && pwd -P)" || return 1
    printf '%s/%s' "$d" "${1##*/}"
}
server=$(tmux list-windows -a -F "#{socket_path}${US}#{start_time}" 2>/dev/null | head -1)
if [ -z "$server" ]; then
    say "skip: no tmux server"
    [ "$verb" = once ] && exit 3
    exit 0
fi
SOCKET=${server%%"$US"*}
SERVER_START=${server#*"$US"}
sock_real="$(resolved_path "$SOCKET")" || sock_real=""
default_real="$(resolved_path "/private/tmp/tmux-$UID/default")" || default_real="/private/tmp/tmux-$UID/default"
allow_real=""
[ -n "${AGENT_REFRESH_ALLOW_SOCKET:-}" ] && allow_real="$(resolved_path "$AGENT_REFRESH_ALLOW_SOCKET")"
if [ -z "$sock_real" ] || { [ "$sock_real" != "$default_real" ] && [ "$sock_real" != "$allow_real" ]; }; then
    say "skip: not the default tmux server: ${SOCKET:-?}"
    [ "$verb" = once ] && exit 3
    exit 0
fi

# --- windows ---------------------------------------------------------------------
# One row per window (its active pane's fields: a candidate has one pane).
# Free text last.
set_if() { printf '#{?#{!=:#{%s},},%s ,}' "$1" "$2"; }
FLAGS="$(set_if @agent_workflow workflow)$(set_if @agent_cua cua)$(set_if @agent_limit rate-limited)$(set_if @agent_limit_lifted rate-limited)$(set_if @agent_paused paused)$(set_if @agent_pending pending-prompt)$(set_if @agent_child delegated-child)$(set_if @agent_parent delegated-child)$(set_if @agent_kids delegating)$(set_if @agent_kids_hold delegating)"
HIDDEN="#{?#{||:#{@space_hidden},#{m:* #{session_name} *, stash agents tasks scratch btop-popup pending #{@space_hidden_sessions} }},1,0}"
ROW_FMT="#{window_id}${US}#{session_name}${US}${HIDDEN}${US}#{@space_home}${US}#{window_panes}${US}#{window_linked_sessions}${US}#{window_active_clients}${US}#{pane_id}${US}#{pane_pid}${US}#{pane_tty}${US}#{@agent_kind}${US}#{@agent_harness}${US}#{@agent_state}${US}#{@agent_since}${US}#{@agent_key}${US}${FLAGS}${US}#{?#{@agent_summary},#{@agent_summary},#{window_name}}"

all_rows() { tmux list-windows -a -F "$ROW_FMT" 2>/dev/null; }
row_of() { all_rows | awk -F"$US" -v w="$1" '$1 == w { print; exit }'; }
parse_row() {
    IFS="$US" read -r r_win r_sess r_hidden r_home r_panes r_linked r_clients r_pane r_ppid _ r_kind r_harness r_state r_since r_key r_flags r_title <<<"$1"
    r_flags=${r_flags%% *}
    r_epoch=""
    case "$r_since" in
        *' idle') r_epoch=${r_since%% *} ;;
    esac
    case "$r_epoch" in ''|*[!0-9]*) r_epoch="" ;; *) r_epoch=$((10#$r_epoch)) ;; esac
    r_idle=""
    [ -n "$r_epoch" ] && r_idle=$(( $(now) - r_epoch ))
    return 0
}

WHY=""
no() { WHY="$*"; return 1; }

# The cheap rules, on the parsed row.
row_ok() {
    [ "$r_hidden" = 0 ] || { no "in a hidden space ($r_sess)"; return 1; }
    [ -z "$r_home" ] || { no "a home tab"; return 1; }
    [ "$r_panes" = 1 ] || { no "split tab"; return 1; }
    [ "${r_linked:-1}" -le 1 ] 2>/dev/null || { no "linked into several sessions"; return 1; }
    [ "$r_kind" = claude ] || { no "not Claude Code (${r_kind:-no agent})"; return 1; }
    case "$r_harness" in ''|claude) ;; *) no "harness $r_harness"; return 1 ;; esac
    [ "$r_state" = idle ] || { no "state ${r_state:-unset}"; return 1; }
    [ -z "$r_flags" ] || { no "$r_flags"; return 1; }
    [ "${r_clients:-1}" = 0 ] || { no "on screen"; return 1; }
    [ -n "$r_epoch" ] || { no "idle since when is unknown"; return 1; }
    if [ "$force_idle" = 0 ] && [ "$r_idle" -lt "$MIN_IDLE" ]; then
        no "idle under $((MIN_IDLE / 3600))h"; return 1
    fi
    return 0
}

# --- processes -----------------------------------------------------------------
# The same process the sessions file was written by (not a reused pid): alive,
# with that start time (ps lstart; Claude Code writes it in UTC, local time is
# accepted too), and under <pane pid>.
same_proc() {   # <pid> <procStart>
    kill -0 "$1" 2>/dev/null || return 1
    [ -n "$2" ] || return 1
    local a b s
    # shellcheck disable=SC2086  # word-split on purpose: lstart's runs of blanks
    s=$(echo $2)
    # shellcheck disable=SC2046
    a=$(echo $(LC_ALL=C TZ=UTC0 ps -o lstart= -p "$1" 2>/dev/null))
    # shellcheck disable=SC2046
    b=$(echo $(LC_ALL=C ps -o lstart= -p "$1" 2>/dev/null))
    [ "$a" = "$s" ] || [ "$b" = "$s" ]
}
under() {   # <pid> <ancestor>
    local p="$1" n=0
    while [ -n "$p" ] && [ "$p" != 0 ] && [ "$p" != 1 ] && [ "$n" -lt 30 ]; do
        [ "$p" = "$2" ] && return 0
        p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
        n=$((n + 1))
    done
    return 1
}
# The one registered Claude Code of <window>, under <pane pid> → s_*; 1 = none,
# 2 = more than one, 3 = could not ask (never to be read as none).
session_in() {
    local rows pid sid ver status win cwd started pstart tp n=0
    s_pid="" s_sid="" s_ver="" s_status="" s_cwd="" s_started="" s_pstart="" s_tp=""
    rows=$(helper sessions 2>/dev/null) || return 3
    while IFS="$US" read -r pid sid ver status win _ cwd started pstart tp; do
        [ "$win" = "$1" ] || continue
        same_proc "$pid" "$pstart" && under "$pid" "$2" || continue
        n=$((n + 1))
        s_pid=$pid s_sid=$sid s_ver=$ver s_status=$status s_cwd=$cwd s_started=$started s_pstart=$pstart s_tp=$tp
    done <<<"$rows"
    [ "$n" = 0 ] && return 1
    [ "$n" -gt 1 ] && return 2
    return 0
}
# Is <sid> held by any live Claude Code process? 0 yes, 1 no, 3 could not ask.
sid_live() {
    local rows pid sid pstart
    rows=$(helper sessions 2>/dev/null) || return 3
    while IFS="$US" read -r pid sid _ _ _ _ _ _ pstart _; do
        [ "$sid" = "$1" ] && same_proc "$pid" "$pstart" && return 0
    done <<<"$rows"
    return 1
}
pane_shell() {   # <pane pid>: the shell's name, if it is one we can type to
    local w
    w=$(ps -o command= -p "$1" 2>/dev/null)
    w=${w%% *}; w=${w##*/}; w=${w#-}
    case "$w" in zsh|bash) printf '%s' "$w"; return 0 ;; esac
    printf '%s' "${w:-gone}"
    return 1
}
ps_num() { ps -o "$1=" -p "$2" 2>/dev/null | tr -d ' '; }
# The pane is still this window's, its process still that shell, and the shell is
# at a prompt: it holds its terminal and its line editor reads it (-icanon).
# → p_tty
at_prompt() {   # <pane> <pane pid> <window>
    local out
    out=$(tmux display-message -p -t "$1" "#{pane_pid}${US}#{window_id}${US}#{pane_dead}${US}#{pane_tty}" 2>/dev/null)
    case "$out" in "$2${US}$3${US}0${US}"/dev/*) p_tty=${out##*"$US"} ;; *) return 1 ;; esac
    pane_shell "$2" >/dev/null || return 1
    [ "$(ps_num tpgid "$2")" = "$2" ] && /bin/stty -f "$p_tty" -a 2>/dev/null | grep -q '[[:space:]]-icanon'
}

# --- versions ------------------------------------------------------------------
plain_version() {
    [[ $1 =~ ^[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}$ ]]
}
older() {   # <a> <b>: a < b
    local a b i x y
    IFS=. read -r -a a <<<"$1"
    IFS=. read -r -a b <<<"$2"
    for i in 0 1 2; do
        x=$((10#${a[i]})) y=$((10#${b[i]}))
        [ "$x" -lt "$y" ] && return 0
        [ "$x" -gt "$y" ] && return 1
    done
    return 1
}
# The installed version → INSTALLED; else 1 with WHY.
installed() {
    local link="$HOME/.local/bin/claude" t mt
    INSTALLED=""
    [ -L "$link" ] || { no "$link is no symlink"; return 1; }
    t=$(readlink "$link")
    case "$t" in /*) ;; *) t="$HOME/.local/bin/$t" ;; esac
    { [ -f "$t" ] && [ -x "$t" ]; } || { no "installed claude is not executable: $t"; return 1; }
    plain_version "${t##*/}" || { no "installed version unreadable: ${t##*/}"; return 1; }
    mt=$(stat -L -f %m "$t" 2>/dev/null)
    case "$mt" in ''|*[!0-9]*) no "installed claude cannot be read"; return 1 ;; esac
    [ $(( $(date +%s) - mt )) -gt "$MIN_INSTALL_AGE" ] || { no "installed ${t##*/} is under $((MIN_INSTALL_AGE / 60)) min old"; return 1; }
    INSTALLED=${t##*/}
}

# --- the resume command ----------------------------------------------------------
# closed-tabs.sh's own lookup, read-only in a subshell that has sourced it
# (stash.sh's pin_dry_why does exactly this; `pinned-json` is a harmless verb
# on the way in). Prints "<tool> US <pid> US <sid> US <cmd>".
resume_cmd() {   # <pane>
    # shellcheck disable=SC2016  # the inner bash expands it
    bounded 30 bash -c '
        source "$1" pinned-json >/dev/null 2>&1 || exit 1
        set +e
        for f in agent_snapshot agent_session build_cmd; do declare -F "$f" >/dev/null || exit 1; done
        info=$(tmux display-message -p -t "$2" "#{pane_id}|#{pane_pid}|#{pane_tty}|#{pane_current_path}" 2>/dev/null)
        IFS="|" read -r pane_id pane_pid pane_tty cwd <<<"$info"
        [ -n "$pane_id" ] || exit 1
        tool="" apid="" args="" copy="" sid="" cli_args="" model="" env_json="" scwd=""
        snap=$(agent_snapshot "$pane_pid" "$pane_tty")
        [ -n "$snap" ] && IFS=$'"'\\x1f'"' read -r tool apid args copy <<<"$snap"
        if [ "$tool" = claude ]; then
            sess=$(agent_session "$tool" "$apid" "$args" "$cwd" "$copy" 0)
            [ -n "$sess" ] && IFS=$'"'\\x1f'"' read -r sid cli_args model env_json scwd <<<"$sess"
            cmd=$(build_cmd "$tool" "$sid" "$cli_args" "$model" "$env_json")
        fi
        [ -n "$copy" ] && [ -d "$copy" ] && rm -rf "$copy"
        printf "%s\x1f%s\x1f%s\x1f%s\n" "$tool" "$apid" "$sid" "${cmd:-}"
    ' _ "$CLOSED_TABS_SH" "$1" 2>/dev/null
}
# Text typed but not sent? stash.sh's own test (its `count` verb runs on the
# way in, harmlessly). 0 = yes, or cannot tell.
has_unsent() {   # <pane>
    # shellcheck disable=SC2016
    bounded 20 bash -c 'source "$1" count >/dev/null 2>&1; declare -F has_unsent_input >/dev/null || exit 0; has_unsent_input "$2"' \
        _ "$STASH_SH" "$1" 2>/dev/null
    case $? in 1) return 1 ;; *) return 0 ;; esac
}
stash_check() {   # <window>: silent 0 = clear; else WHY
    local out rc
    out=$(bounded 60 bash "$STASH_SH" check "$1" 2>/dev/null); rc=$?
    [ "$rc" = 0 ] && [ -z "$out" ] && return 0
    out=${out%%$'\n'*}
    no "stash: ${out:-check failed ($rc)}"
}

# --- MCP -----------------------------------------------------------------------
# Claude Code connects to its HTTP MCP servers once, at startup: a resume while
# one is down comes up without those tools (assistant-restore.sh). Loopback
# servers only; one try each. Prints the ones that do not answer.
mcp_down() {
    local urls u down=""
    if [ -n "${AGENT_REFRESH_MCP_URLS+x}" ]; then urls=$AGENT_REFRESH_MCP_URLS
    elif [ -f "$HOME/.claude.json" ]; then
        urls="$(bounded 10 jq -r '(.mcpServers // {})[] | select(.type == "http" or .type == "sse") | .url // empty
            | select(test("^https?://(127\\.0\\.0\\.1|localhost|\\[::1\\])[:/]"))' "$HOME/.claude.json" 2>/dev/null | tr '\n' ' ')"
    else urls=""
    fi
    for u in $urls; do
        [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "$u" 2>/dev/null)" = 000 ] && down="$down $u"
    done
    printf '%s' "${down# }"
}

# --- one window, all the rules --------------------------------------------------
failed_before() { grep -qF -- "$1 $2 $3 " "$FAILED" 2>/dev/null; }
record_failed() {   # <window> <sid> <version> <why>
    mkdir -p "$STATE_DIR"
    printf '%s %s %s %s %s\n' "$1" "$2" "$3" "$(date +%s)" "$4" >>"$FAILED"
    if [ "$(wc -l <"$FAILED" | tr -d ' ')" -gt 400 ]; then
        tail -n 200 "$FAILED" >"$FAILED.$$" && mv -f "$FAILED.$$" "$FAILED"
    fi
}
journal_of() { printf '%s/%s.json' "$JOURNAL" "${1#@}"; }

# The parsed row's window, judged to the end: 0 = restart it, with c_cmd
# and s_*; 1 = WHY. s_ver is set as soon as it is known.
judge() {
    local out tool apid sid rc tp shell model
    c_cmd=""
    s_ver=""
    row_ok; rc=$?
    [ "$rc" = 0 ] || [ "$dry" = 1 ] || return 1
    local row_why=$WHY
    session_in "$r_win" "$r_ppid"; local srch=$?
    [ "$rc" = 0 ] || { WHY=$row_why; return 1; }
    case "$srch" in
        1) no "no registered Claude Code process"; return 1 ;;
        2) no "two Claude Code processes"; return 1 ;;
        3) no "the session files could not be read"; return 1 ;;
    esac
    plain_version "$s_ver" || { no "running version unreadable: ${s_ver:-none}"; return 1; }
    older "$s_ver" "$INSTALLED" || { no "up to date"; return 1; }
    case "$s_status" in idle|shell) ;; *) no "session status ${s_status:-unknown}"; return 1 ;; esac
    failed_before "$r_win" "$s_sid" "$INSTALLED" && { no "failed before on $INSTALLED (not retried)"; return 1; }
    [ -e "$(journal_of "$r_win")" ] && { no "a restart is already in flight"; return 1; }
    [ -d "$s_cwd" ] || { no "its directory is gone: $s_cwd"; return 1; }

    # The agent must be the pane shell's foreground job: with no shell under it
    # the pane dies with it, and a suspended one has no prompt to return to.
    shell=$(pane_shell "$r_ppid") || { no "the pane runs $shell, not a shell"; return 1; }
    { [ "$(ps_num tpgid "$r_ppid")" != "$r_ppid" ] && [ "$(ps_num pgid "$s_pid")" = "$(ps_num tpgid "$r_ppid")" ]; } \
        || { no "not its shell's foreground job"; return 1; }

    stash_check "$r_win" || return 1

    tp=$s_tp
    [ -n "$tp" ] || { no "its transcript is not under its directory (--resume would miss it)"; return 1; }
    out=$(helper scan "$tp" "${s_started:-0}" "$(now)" 2>/dev/null); rc=$?
    case "$rc" in
        0) model=$out ;;
        3) no "$out"; return 1 ;;
        *) no "transcript could not be read"; return 1 ;;
    esac
    # What this process started and never saw finish, however quiet.
    helper bg "$BG_PENDING" "$tp" "$s_sid" "${s_started:-0}" >/dev/null 2>&1; rc=$?
    case "$rc" in
        0) ;;
        3) no "background work still out"; return 1 ;;
        *) no "agent-bg-pending could not be asked"; return 1 ;;
    esac

    # Resolved BEFORE the kill: Claude Code deletes its own records as it exits.
    out=$(resume_cmd "$r_pane")
    IFS="$US" read -r tool apid sid c_cmd <<<"$out"
    [ "$tool" = claude ] || { no "no agent found in the pane"; return 1; }
    [ "$apid" = "$s_pid" ] || { no "the pane's agent is pid ${apid:-?}, its session file says $s_pid"; return 1; }
    [ "$sid" = "$s_sid" ] || { no "session ids disagree (${sid:-none} / $s_sid)"; return 1; }
    case "$c_cmd" in
        *"command claude "*" --resume '$sid'"|*"command claude --resume '$sid'") ;;
        *) no "no resume command of Claude Code's own"; return 1 ;;
    esac
    case "$c_cmd" in
        *"'-c'"*|*"'--continue'"*|*"'--fork-session'"*|*"'--session-id"*|*"'-w'"*|*"'--worktree"*|*"'--tmux"*|*"'-p'"*|*"'--print'"*|*"'--from-pr"*)
            no "launched with a flag that does not go with --resume: $c_cmd"; return 1 ;;
    esac
    # Its --model is the one it STARTED on; the resume gets the one it is on.
    c_cmd=$(helper setmodel "$c_cmd" "$model" 2>/dev/null); rc=$?
    case "$rc" in
        0) ;;
        3) no "$c_cmd"; return 1 ;;
        *) no "its model could not be set"; return 1 ;;
    esac
    case "$c_cmd$s_cwd" in *[[:cntrl:]]*) no "control characters in the resume command"; return 1 ;; esac
    return 0
}
# The leading space keeps it out of the shell's history.
resume_line() { printf ' clear; cd %s && %s' "$(squote "$1")" "$2"; }

# --- the restart ------------------------------------------------------------------
# A toast waits in toasts/ (one file each, the title on its first line) until a
# client was active within 10 min, is posted once, and is done: posted to
# nobody, and again, it would only push others out of notify.sh's few records.
post_toast() {   # <file>
    local h='#' title text kind=error ttl=3600 a t seen=""
    t=$(date +%s)
    while IFS= read -r a; do
        case "$a" in ''|*[!0-9]*) continue ;; esac
        [ $((t - a)) -lt 600 ] && seen=1
    done < <(tmux list-clients -F '#{client_activity}' 2>/dev/null)
    [ -n "$seen" ] || return 0
    { IFS= read -r title; IFS= read -r text; } <"$1"
    case "$title" in *restarting) kind=warn ttl=120 ;; esac
    text=${text//$h/$h$h}   # notify.sh expands formats; ours never means one
    if [ -x "$NOTIFY_SH" ]; then
        bounded 10 "$NOTIFY_SH" -c '*' -k "$kind" -t "$ttl" -T "$title" -- "$text" >/dev/null 2>&1
    else
        bounded 10 zsh -f "$NOTIFY_SH" -c '*' -k "$kind" -t "$ttl" -T "$title" -- "$text" >/dev/null 2>&1
    fi
    rm -f "$1"
}
toast() {   # <title> <text> [file name]
    local f
    mkdir -p "$TOASTS"
    f="$TOASTS/${3:-$(date +%s)-$$-$RANDOM}"
    printf '%s\n%s\n' "$1" "$(printf '%s' "$2" | tr '\n' ' ')" >"$f" && post_toast "$f"
}
pending_toasts() {
    local f
    for f in "$TOASTS"/*; do [ -f "$f" ] && post_toast "$f"; done
    return 0
}

j_load() {   # <journal> → j_*; 1 = unreadable, 3 = could not ask
    local f out rc
    for f in window pane pane_pid pid sid cwd cmd line from to title ts server phase typed told opts; do eval "j_$f=''"; done
    out=$(helper jload "$1" 2>/dev/null); rc=$?
    [ "$rc" -le 1 ] || return 3
    eval "$out"
    [ -n "$j_window" ] && [ -n "$j_pane" ] && [ -n "$j_pane_pid" ] && [ -n "$j_pid" ] && [ -n "$j_sid" ] && [ -n "$j_line" ]
}
# Gone for certain (a tmux that does not answer says nothing).
pane_gone() {
    local out
    out=$(tmux list-panes -a -F '#{pane_id}' 2>/dev/null) && [ -n "$out" ] || return 1
    case $'\n'"$out"$'\n' in *$'\n'"$j_pane"$'\n'*) return 1 ;; esac
    return 0
}
# The Claude Code in the window (s_*) is this journal's conversation: its id, or
# (a compaction gives a new one) started to resume that id.
ours() {
    [ "$s_sid" = "$j_sid" ] && return 0
    case "$(ps -ww -o command= -p "$s_pid" 2>/dev/null)" in *"--resume $j_sid"*|*"--resume=$j_sid"*) return 0 ;; esac
    return 1
}

# Gave up on the loaded journal: the record is kept in archive/, the (window,
# session, version) never tried again, and Mack told, with the command that
# brings it back by hand ("running": it is still or again up, no command).
fail() {   # <journal> <why> [running]
    rm -f "$TOASTS/restarting-${j_window#@}"
    record_failed "$j_window" "$j_sid" "$j_to" "$2"
    helper jset "$1" failed "$2" 2>/dev/null
    mkdir -p "$ARCHIVE"
    mv -f "$1" "$ARCHIVE/${j_window#@}-$(date +%s).json"
    # shellcheck disable=SC2012  # our own names
    ls -t "$ARCHIVE" 2>/dev/null | tail -n +51 | while IFS= read -r f; do rm -f "$ARCHIVE/$f"; done
    say "FAILED $j_window ($j_title): $2"
    if [ -n "${3:-}" ]; then toast "Agent restart" "$j_title: $2"
    else toast "Agent restart failed" "$j_title: $2. Bring it back: cd $(squote "$j_cwd") && $j_cmd"; fi
}

# Put the saved options back, if the tab is exactly as the restart left it:
# idle, no turn since (@agent_turn_start is unset by SessionEnd and set by
# every turn), its idle stamp the SessionStart's own (or already the old one).
# 0 put back (look again), 1 settled or moved on, 2 tmux did not answer.
restore_opts() {   # (s_started: the new process's start)
    local o v st turn since epoch old="" cur set=()
    while IFS= read -r -d $'\x1e' o; do
        v=${o#*"$US"}; o=${o%%"$US"*}
        [ -n "$v" ] || continue
        [ "$o" = @agent_since ] && old=$v
        # An argument ending in ; would end the tmux command.
        case "$v" in *';') v="${v%;}\\;" ;; esac
        [ "${#set[@]}" -gt 0 ] && set+=(';')
        set+=(set-option -w -t "$j_window" -- "$o" "$v")
    done < <(printf '%s\x1e' "$j_opts")
    cur=$(tmux display-message -p -t "$j_window" "#{@agent_state}${US}#{@agent_turn_start}${US}#{@agent_since}" 2>/dev/null) || return 2
    IFS="$US" read -r st turn since <<<"$cur"
    epoch=${since%% *}
    case "$epoch" in ''|*[!0-9]*) epoch=0 ;; esac
    if [ "$st" != idle ] || [ -n "$turn" ] || [ "$epoch" = 0 ] || [ $((epoch - s_started)) -gt "$RESTAMP_MAX" ]; then
        say "$j_window moved on (state ${st:-unset}): its options are left alone"
        return 1
    fi
    [ "${#set[@]}" -gt 0 ] || return 1
    tmux "${set[@]}" 2>/dev/null || return 2
    [ "$since" != "$old" ]   # 1 = it was already there: settled
}
RESTAMP_MAX=90   # s between a resumed process's start and its SessionStart

# The conversation is back in a new process (s_*): put the options back, close the journal.
finish() {   # <journal>
    local i=0
    while [ "$i" -lt 40 ] && [ -z "$(tmux show-options -wqv -t "$j_window" @agent_state 2>/dev/null)" ]; do
        sleep 0.25; i=$((i + 1))
    done
    # The SessionStart hook can come after the watcher's own seed, and repaints
    # the summary a beat after the state: put them back until they stay.
    for i in 1 2 3; do
        sleep 2
        restore_opts
        case $? in
            1) break ;;
            2) say "$j_window is back, but tmux did not answer; the next tick puts its options back"; return 2 ;;
        esac
    done
    # The sidebar and the notch redraw on this notice (agent-tab-indicator.sh).
    local notice="${AGENT_STATE_NOTICE-com.mackhaymond.tmux.agent-state}"
    [ -n "$notice" ] && notifyutil -p "$notice" >/dev/null 2>&1
    if plain_version "$s_ver" && ! older "$s_ver" "$j_to"; then
        rm -f "$1" "$TOASTS/restarting-${j_window#@}"
        say "restarted $j_window ($j_title): $j_from → $s_ver, session $s_sid"
        return 0
    fi
    [ "$s_sid" = "$j_sid" ] || record_failed "$j_window" "$s_sid" "$j_to" "same, under its new session id"
    fail "$1" "it came back on ${s_ver:-an unknown version}, not $j_to" running
    return 1
}

# Type the resume into the shell at its prompt, in ONE tmux command list: only
# while the window is off screen and the pane still that shell's; C-c (zsh's vi
# mode makes C-u no line-kill, and runs text in vicmd as commands: C-c always
# gives a fresh insert-mode prompt), then, once that prompt is read (_ready), the
# line and Enter. 1 = not typed, or tmux did not answer: the journal stays.
type_line() {   # (p_tty from at_prompt)
    local buf="agent-refresh-$$" self=$here/${BASH_SOURCE[0]##*/} h='#' rc
    self=${self//$h/$h$h}
    tmux set-buffer -b "$buf" -- "$j_line" ';' \
        if-shell -F -t "$j_pane" "#{&&:#{==:#{window_active_clients},0},#{==:#{pane_pid},$j_pane_pid}}" \
        "copy-mode -q -t $j_pane ; send-keys -t $j_pane C-c ; if-shell \"/bin/bash '$self' _ready $p_tty $j_pane_pid\" \"paste-buffer -d -r -b $buf -t $j_pane ; send-keys -t $j_pane Enter\"" \
        >/dev/null 2>&1
    rc=$?
    # Pasting deletes the buffer.
    tmux show-buffer -b "$buf" >/dev/null 2>&1 && { tmux delete-buffer -b "$buf" 2>/dev/null; return 1; }
    [ "$rc" = 0 ]
}

# The loaded journal's agent was told to end: wait for the prompt, type, wait
# for a NEW agent. 0 back, 1 failed, 2 not settled (the next tick goes on; NOTYPE
# says why it could not be typed, if that is why).
NOTYPE=""
bring_back() {   # <journal>
    local i=0 ok=0 rc on
    NOTYPE=""
    while [ "$ok" -lt 3 ]; do
        if [ "$i" -ge "$EXIT_TICKS" ]; then
            NOTYPE="its pane is not at a prompt"
            kill -0 "$j_pid" 2>/dev/null && NOTYPE="it has not exited"
            say "$j_window: after $((EXIT_TICKS / 4))s $NOTYPE (pid $j_pid); the next tick looks again"
            return 2
        fi
        if kill -0 "$j_pid" 2>/dev/null; then ok=0
        elif at_prompt "$j_pane" "$j_pane_pid" "$j_window"; then ok=$((ok + 1))
        else
            ok=0
            pane_gone && { fail "$1" "its tab closed when the agent exited"; return 1; }
        fi
        sleep 0.25; i=$((i + 1))
    done
    if ! type_line; then
        NOTYPE="its prompt did not answer"
        on=$(tmux display-message -p -t "$j_pane" '#{window_active_clients}' 2>/dev/null)
        case "$on" in
            ''|0|*[!0-9]*) ;;
            *)  NOTYPE="its tab is on screen"
                # He is looking at a bare shell: say so, once.
                if [ -z "$j_told" ] && helper jset "$1" told 1; then
                    toast "Agent restarting" "$j_title is being restarted on $j_to and reopens when you leave its tab. Or now: cd $(squote "$j_cwd") && $j_cmd" "restarting-${j_window#@}"
                fi ;;
        esac
        say "$j_window: not typed, $NOTYPE; the next tick looks again"
        return 2
    fi
    helper jset "$1" typed "$((${j_typed:-0} + 1))"
    i=0
    while [ "$i" -lt "$BACK_TICKS" ]; do
        session_in "$j_window" "$j_pane_pid"; rc=$?
        if [ "$rc" = 0 ] && [ "$s_pid" != "$j_pid" ]; then
            ours || { fail "$1" "another Claude Code session is in its tab"; return 1; }
            finish "$1"; return $?
        fi
        sleep 0.5; i=$((i + 2))
    done
    if [ "$rc" != 3 ] && at_prompt "$j_pane" "$j_pane_pid" "$j_window"; then
        fail "$1" "the resume did not start"
        return 1
    fi
    say "$j_window: still starting after $((BACK_TICKS / 4))s; the next tick looks again"
    return 2
}

# Right before the signal: the deep guards again, then the cheap facts and the
# composer, read fresh. 1 = WHY.
last_look() {   # <window> <pane> <pane pid> <pid> <procStart> <sid>
    WHY=""
    stash_check "$1" || return 1
    parse_row "$(row_of "$1")"
    [ "$r_win" = "$1" ] && [ "$r_pane" = "$2" ] || { no "the tab changed"; return 1; }
    row_ok || return 1
    session_in "$1" "$3" && [ "$s_pid" = "$4" ] && [ "$s_sid" = "$6" ] || { no "its session changed"; return 1; }
    case "$s_status" in idle|shell) ;; *) no "session status $s_status"; return 1 ;; esac
    has_unsent "$2" && { no "unsent text in the agent's composer"; return 1; }
    [ "$(tmux display-message -p -t "$2" "#{@agent_state}${US}#{window_active_clients}${US}#{window_id}" 2>/dev/null)" = "idle${US}0${US}$1" ] \
        && same_proc "$4" "$5" || { no "it was just used"; return 1; }
}

# Restart the judged window (r_*, s_*, c_cmd). 3 = refused at the last moment.
restart() {
    local j win=$r_win pid=$s_pid o v fmt="" vals kv=()
    j=$(journal_of "$win")
    mkdir -p "$JOURNAL"
    # All or nothing: a read that failed must not pass for an unset option.
    for o in $SAVE_OPTS; do fmt="$fmt#{$o}$US"; done
    vals=$(tmux display-message -p -t "$win" "${fmt}end" 2>/dev/null) \
        || { no "its options could not be read"; return 3; }
    for o in $SAVE_OPTS; do
        case "$vals" in *"$US"*) ;; *) no "its options could not be read"; return 3 ;; esac
        v=${vals%%"$US"*}; vals=${vals#*"$US"}
        [ -n "$v" ] && kv+=("opt:$o" "$v")
    done
    [ "$vals" = end ] || { no "its options could not be read"; return 3; }
    # "killing" from before the last look: nothing but the kill comes after it.
    if ! { helper jset "$j" phase killing ts "$(date +%s)" server "$SERVER_START" window "$win" pane "$r_pane" pane_pid "$r_ppid" \
            pid "$pid" sid "$s_sid" cwd "$s_cwd" cmd "$c_cmd" line "$(resume_line "$s_cwd" "$c_cmd")" from "$s_ver" to "$INSTALLED" \
            title "$r_title" ${kv[@]+"${kv[@]}"} && j_load "$j"; }; then
        rm -f "$j"; no "could not write the journal"; return 3
    fi
    if ! last_look "$win" "$r_pane" "$r_ppid" "$pid" "$s_pstart" "$s_sid"; then
        rm -f "$j"; no "changed at the last moment: $WHY"; return 3
    fi
    kill -TERM "$pid" 2>/dev/null
    helper jset "$j" phase signalled
    say "restarting $win ($j_title): $j_from → $j_to, pid $pid, session $j_sid"
    bring_back "$j"
}

# --- recovery ---------------------------------------------------------------------
# The loaded journal's resume cannot be typed now: later, or, 30 min (wall time)
# after the kill, over to Mack. Only ever after trying: a lid-close ages it too.
stuck() {   # <journal> <age> <why>
    if [ "$2" -ge "$JOURNAL_GIVE_UP" ]; then fail "$1" "$3"
    else say "recovery: $j_window waits: $3"; fi
}
recover() {
    local j age here_rc live_rc
    for j in "$JOURNAL"/*.json; do
        [ -f "$j" ] || continue
        j_load "$j"
        case $? in 0) ;; 3) say "recovery: ${j##*/} could not be read; later"; continue ;; *)
            mkdir -p "$ARCHIVE"; mv -f "$j" "$ARCHIVE/unreadable-$(date +%s).json"
            say "recovery: unreadable journal ${j##*/} archived"
            continue ;;
        esac
        case "$j_ts" in ''|*[!0-9]*) j_ts=0 ;; esac
        age=$(( $(date +%s) - j_ts ))
        s_pid="" here_rc=1
        # (After a new tmux server every pane id means something else.)
        if [ "$j_server" = "$SERVER_START" ]; then session_in "$j_window" "$j_pane_pid"; here_rc=$?; fi
        sid_live "$j_sid"; live_rc=$?
        if [ "$here_rc" = 3 ] || [ "$live_rc" = 3 ]; then
            # Which is not "none": nothing is typed.
            say "recovery: the session files could not be read; $j_window later"
        elif [ "$here_rc" = 2 ]; then
            stuck "$j" "$age" "two Claude Code processes are in its tab"
        elif [ -n "$s_pid" ] && [ "$s_pid" = "$j_pid" ]; then
            if [ "$j_phase" != signalled ]; then
                rm -f "$j"
                say "recovery: $j_window was never signalled; nothing to do"
            elif [ "$age" -ge 120 ]; then
                fail "$j" "it did not exit on SIGTERM (still running)" running
            fi
        elif [ -n "$s_pid" ] && ours; then
            say "recovery: $j_window is back"
            finish "$j"
        elif [ "$live_rc" = 0 ]; then
            rm -f "$j" "$TOASTS/restarting-${j_window#@}"
            say "recovery: $j_window's session is running elsewhere"
        elif [ -n "$s_pid" ]; then
            fail "$j" "another Claude Code session is in its tab"
        elif [ "$j_server" != "$SERVER_START" ]; then
            fail "$j" "tmux restarted in the middle of it"
        elif pane_gone; then
            fail "$j" "its tab is gone"
        elif ! at_prompt "$j_pane" "$j_pane_pid" "$j_window"; then
            stuck "$j" "$age" "its pane is not at a prompt"
        elif [ "${j_typed:-0}" -ge 2 ]; then
            fail "$j" "the resume did not start, twice"
        # Not while it would come up toolless (and never on screen: type_line).
        elif [ -n "$(mcp_down)" ]; then
            stuck "$j" "$age" "MCP is down"
        else
            say "recovery: typing $j_window's resume"
            bring_back "$j"
            [ $? = 2 ] && [ -n "$NOTYPE" ] && stuck "$j" "$age" "$NOTYPE"
        fi
    done
}

# --- the lock ---------------------------------------------------------------------
LOCKED=0
# shellcheck disable=SC2046,SC2005  # word-split on purpose: one blank each
lstart() { echo $(LC_ALL=C ps -o lstart= -p "$1" 2>/dev/null); }
# The lock's holder is alive and IS an agent-refresh: the recorded start time
# and a command line that says so (never a reused pid).
holder_ours() {   # <pid> <lstart>
    [ -n "$2" ] && kill -0 "$1" 2>/dev/null && [ "$(lstart "$1")" = "$2" ] || return 1
    case "$(ps -ww -o command= -p "$1" 2>/dev/null)" in *agent-refresh.sh*) return 0 ;; esac
    return 1
}
lock() {
    local pid="" start="" mt age i
    mkdir -p "$STATE_DIR"
    if ! mkdir "$LOCK" 2>/dev/null; then
        mt=$(stat -f %m "$LOCK" 2>/dev/null) || return 1
        age=$(( $(date +%s) - mt ))
        [ -f "$LOCK/pid" ] && { read -r pid; read -r start; } <"$LOCK/pid"
        case "$pid" in
            ''|*[!0-9]*)
                # No pid yet: being taken right now, or left by a crash.
                [ "$age" -gt 10 ] || return 1 ;;
            *)
                if holder_ours "$pid" "$start"; then
                    [ "$age" -gt "$LOCK_MAX" ] || return 1
                    # Hung past its own watchdog: ended (its journal stays).
                    say "lock held ${age}s by $pid: ending it"
                    kill -TERM "$pid" 2>/dev/null
                    for i in 1 2 3 4 5 6; do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
                    holder_ours "$pid" "$start" && kill -KILL "$pid" 2>/dev/null
                fi ;;
        esac
        # Stale: renamed first, so of two breakers only one takes it. (A holder
        # that just ended may have removed it itself.)
        mv "$LOCK" "$LOCK.stale.$$" 2>/dev/null && rm -rf "$LOCK.stale.$$"
        mkdir "$LOCK" 2>/dev/null || return 1
    fi
    printf '%s\n%s\n' $$ "$(lstart $$)" >"$LOCK/pid"
    LOCKED=1
    trap unlock EXIT
    trap 'exit 1' INT TERM HUP
    # The watchdog: one process, ours alone (it acts only while still our child).
    PERL_BADLANG=0 perl -e 'my ($t, $p) = @ARGV; sleep $t; kill "TERM", $p if getppid == $p;
        sleep 15; kill "KILL", $p if getppid == $p' "$TICK_MAX" $$ >/dev/null 2>&1 </dev/null &
    WATCHDOG=$!
}
unlock() {
    [ "$LOCKED" = 1 ] || return 0
    kill "$WATCHDOG" 2>/dev/null
    local pid=""
    [ -f "$LOCK/pid" ] && read -r pid <"$LOCK/pid"
    [ "$pid" = $$ ] && rm -rf "$LOCK"
    LOCKED=0
}

# --- main ---------------------------------------------------------------------------
fmt_idle() {
    case "${1:-}" in
        ''|*[!0-9]*) printf '?' ;;
        *) if [ "$1" -ge 86400 ]; then printf '%dd%02dh' $(($1 / 86400)) $(($1 % 86400 / 3600))
           else printf '%dh%02dm' $(($1 / 3600)) $(($1 % 3600 / 60)); fi ;;
    esac
}
describe() { printf '%s %s [%s] "%s"' "$r_win" "${r_key:--}" "$r_sess" "$r_title"; }

# Nothing restarts this tick: `check` is done (0), `once` refused (3).
stop() {
    say "skip: $*"
    [ "$verb" = once ] && exit 3
    exit 0
}
if [ "$dry" = 0 ]; then
    lock || stop "another agent-refresh is running"
    [ "$verb" = check ] && { pending_toasts; recover; }
fi
installed || stop "$WHY"
paused=$(helper tick 2>/dev/null) || paused="its own checks failed"
[ -n "$paused" ] && [ "$dry" = 0 ] && stop "$paused"

# The agents, longest idle first (a window with no idle stamp sorts last).
agents=$(all_rows | awk -F"$US" -v OFS="$US" -v only="$only" '
    only != "" ? $1 == only : ($11 != "" && $3 == 0) {
        if (seen[$1]++) next
        t = ($14 ~ / idle$/) ? $14 + 0 : 9999999999
        print t, $0 }' | sort -t"$US" -k1,1n -s)
agents=$(printf '%s\n' "$agents" | sed "s/^[0-9]*$US//")

if [ "$dry" = 1 ]; then
    down=$(mcp_down)
    for why in "$paused" "${down:+MCP down: $down}"; do
        [ -n "$why" ] && echo "nothing would restart now: $why"
    done
    echo "installed: $INSTALLED"
    n=0
    while IFS= read -r row; do
        [ -n "$row" ] || continue
        parse_row "$row"
        judge; rc=$?
        printf '%s  %s → %s  idle %s\n' "$(describe)" "${s_ver:-?}" "$INSTALLED" "$(fmt_idle "$r_idle")"
        if [ "$rc" = 0 ]; then
            n=$((n + 1))
            if [ "$verb" = check ] && [ "$n" -gt "$MAX_PER_TICK" ]; then printf '    would restart on a later tick (max %s each):\n' "$MAX_PER_TICK"
            else printf '    WOULD RESTART:\n'; fi
            printf '    %s\n' "$(resume_line "$s_cwd" "$c_cmd")"
        else
            printf '    skip: %s\n' "$WHY"
        fi
    done <<<"$agents"
    exit 0
fi

[ "$verb" = once ] && [ -z "$agents" ] && stop "no window $only"

# A save taken in the gap has the tab as a shell: save again now that it is an
# agent (resurrect's fingerprint holds the foreground command). Waited for: it
# debounces 5 s, and launchd ends a job's leftovers.
save_soon() { [ -f "$SAVE_SOON_SH" ] && bounded 60 bash "$SAVE_SOON_SH" >/dev/null 2>&1; return 0; }

done_n=0 tried=0 skipped="" mcp_checked=0
while IFS= read -r row <&3; do
    [ -n "$row" ] || continue
    [ "$tried" -lt "$MAX_PER_TICK" ] && [ "$SECONDS" -lt "$TICK_START_MAX" ] || break
    parse_row "$row"
    # (A restart above may have taken minutes: judge what is there now.)
    [ "$tried" -gt 0 ] && parse_row "$(row_of "${row%%"$US"*}")"
    if ! judge; then
        skipped="$skipped; $r_win ${r_key:--}: $WHY"
        [ "$verb" = once ] && { say "refused $r_win: $WHY"; exit 3; }
        continue
    fi
    if [ "$mcp_checked" = 0 ]; then
        down=$(mcp_down); mcp_checked=1
        [ -z "$down" ] || stop "MCP down: $down"
    fi
    restart; rc=$?
    case "$rc" in
        0) done_n=$((done_n + 1)); tried=$((tried + 1)); save_soon ;;
        3) skipped="$skipped; ${row%%"$US"*}: $WHY" ;;
        *) tried=$((tried + 1)) ;;
    esac
    if [ "$verb" = once ]; then
        [ "$rc" = 3 ] && { say "refused ${row%%"$US"*}: $WHY"; exit 3; }
        [ "$rc" = 0 ] && exit 0
        exit 1
    fi
done 3<<<"$agents"
say "tick: $done_n restarted${skipped:+$skipped}"
exit 0
