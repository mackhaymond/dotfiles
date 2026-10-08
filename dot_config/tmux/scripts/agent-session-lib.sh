# agent-session-lib.sh — the ONE implementation of two questions both
# agent-tab-watcher.sh and stash.sh have to answer about a claude process:
#
#   1. WHERE DOES THIS SESSION'S RUNTIME STATE LIVE?  (resolve_session_bases)
#      Under ~/.claude/projects/<munged cwd>/<sessionId>/, where sessionId is
#      the one ~/.claude/sessions/<pid>.json reports. Nothing else.
#      Until 2026-10-07 this also walked a compaction LINEAGE (a 2026-08-25
#      incident: a compacted session's subagents had moved under a new id,
#      so every guard looked in the empty old dir and a park SIGTERMed two
#      live reviewers). The walk grepped every transcript in the project
#      (643 MB here, ~8 s per session, cached only 60 s) and made watcher
#      ticks take tens of seconds. Removed 2026-10-07 on evidence: across
#      all 1.9 GB of ~/.claude/projects, 6 transcripts carry a compaction
#      summary and every one references only ITSELF — current Claude Code
#      compacts in place, so the sessions file's id is authoritative.
#      IF CLAUDE CODE EVER REVERTS to new-id compaction: that session's tab
#      loses its workflow/subagent gear, and stash.sh's park guard cannot see
#      its subagents (a park could SIGTERM live work again). The signature is
#      a transcript whose "isCompactSummary":true record names a DIFFERENT
#      <sid>.jsonl; the walk is in git history (before 2026-10-07).
#
#   2. IS THIS SUBAGENT / WORKFLOW FINISHED?
#      Not by the subagent's own transcript tail — two rules were tried there
#      and a 101-transcript corpus refuted both (2.1.245 writes stop_reason
#      null on the final record, and "last record is an assistant text block"
#      misfires on the text→tool_use gap, which scales with the tool call's
#      payload: 23% of measured gaps beat 5s, the worst 86s). The PARENT
#      knows: when a background agent finishes, the harness appends a
#      <task-notification> naming its <task-id> to the parent transcript,
#      promptly. So a subagent is finished when its parent was notified about
#      it SINCE its transcript last moved (a resumed agent moves again and is
#      running again), or when the user hit Esc on it (its last record is the
#      interrupt marker — the parent is never notified for those), else it is
#      running, for at most the one-hour age backstop a dead parent's agents
#      are given. "Since" has SUBAGENT_NOTIFY_GRACE seconds of slack: the
#      notice lands ~90ms before the agent's own last write (measured
#      2026-09-29) and both sides are whole seconds here, so an exact test
#      held every finished background agent "running" for the full hour.
#      A workflow is in-flight iff its runtime dir
#      subagents/workflows/wf_<id>/ exists without its completion file
#      workflows/wf_<id>.json, with the same one-hour mtime backstop
#      (transcripts go quiet during long stalls — worst measured gap 394s —
#      so the backstop must dwarf that; an hour gives 9x).
#
# HISTORY. These functions were born in the watcher, and stash.sh grew its
# own parallel copies of both answers. The private copies drifted: stash's
# chain walk was one level deep (moot since the walk itself is gone, see
# point 1), and its subagent rule was
# the refuted transcript-shape test with a 30s quiet window — inside the
# 86s text→tool_use gap, i.e. a rule that could read a live subagent as
# finished on the KILL path. Extracting the watcher's newer rules here and
# deleting the copies is the fix for both the drift and the holes.
#
# CONSUMERS AND THEIR FAILURE DIRECTIONS. The watcher calls these once per
# tick per window to paint @agent_workflow (a wrong "finished" costs a
# missing gear); stash.sh calls them on its kill path where a wrong
# "finished" is a SIGTERM into live work. So every ambiguity here must
# resolve toward "running": an unreadable parent transcript, an unparseable
# notification, a session file with no sid — all read as "still running",
# and each consumer's source-failure stub (see their `.` lines) keeps that
# direction when this file itself is missing.
#
# These rules are PINNED to CuaNotch's workflowInfo / tallyNotifications /
# subagentInterrupted — see check-invariants in the cua-notch repo. Change
# them here and there together, or the notch and the tab bar disagree.
#
# Requires bash (BASH_REMATCH, associative-array caches, %(%s)T). Sourced,
# not executed; safe under callers running set -u and pipefail. The caches
# make repeat calls cheap for the watcher's long-lived loop and are merely
# harmless for one-shot callers like stash.sh.
#
# COST. The watcher runs both checks for every claude window every tick, so
# the common path is fork-free except ONE `stat` per session that has a
# subagents/ dir (all its agent-*.jsonl, plus the parent transcript, in one
# call). Sessions here carry dozens of subagent transcripts; the old
# per-file `stat -f %m` + `stat -f %z` + notified_ids' own stat was ~3 forks
# per file per tick. Python (new parent bytes, read by seek — see
# notified_ids) and tail (a file that moved) run only when their input
# changed. Beware bash pattern ops on big strings: `[[ $s == *x* ]]` on the
# 30 KB token list and `${s##*$'\n'}` on a 64 KB tail each cost 150-300 ms
# (measured 2026-10-07) — hence NOTIF_WHEN and the `| tail -n 1`.

# Resolve a claude PID to its session's runtime dirs. SESSION_PROJ is the
# project dir (~/.claude/projects/<proj>) and SESSION_BASES the session ids
# under it that belong to this process: exactly the sessions file's own id
# (see the header's point 1 for why it is no longer a compaction lineage).
# Kept an array so callers need not change if a lineage ever comes back.
SESSION_PROJ=""
SESSION_BASES=()
resolve_session_bases() {
    local pid="$1" sf sid="" cwd="" proj raw
    SESSION_PROJ=""
    SESSION_BASES=()
    [ -n "$pid" ] || return 1
    sf="$HOME/.claude/sessions/$pid.json"
    [ -f "$sf" ] || return 1
    # Bash builtins, not `grep -o | head -1 | cut`: the watcher runs this for
    # EVERY claude window on EVERY 1s tick. The pipeline form cost 8
    # processes and ~4.7ms per call (measured, 200 iterations) against
    # 0.045ms here — ~105x, and with 4 live windows it was ~2.8M spawns/day
    # for a function that returns "no workflow" almost always.
    raw="$(<"$sf")"
    [[ $raw =~ \"sessionId\":\"([^\"]+)\" ]] && sid="${BASH_REMATCH[1]}"
    [[ $raw =~ \"cwd\":\"([^\"]+)\" ]] && cwd="${BASH_REMATCH[1]}"
    { [ -n "$sid" ] && [ -n "$cwd" ]; } || return 1
    # Claude munges the project dir name from cwd: every character that is not
    # an ASCII letter or digit ('/', '.', '_', ...) becomes '-'.
    proj="${cwd//[^A-Za-z0-9]/-}"
    SESSION_PROJ="$HOME/.claude/projects/$proj"
    SESSION_BASES=("$sid")
}

# The parent transcript is read INCREMENTALLY (bytes appended since the last
# look, one python over the delta, only when a fresh subagent file makes the
# answer matter), and the interrupt verdict is cached by mtime+size, so a
# finished file costs one tail read rather than two forks a second.
declare -A NOTIF_SIZE NOTIF_WHEN INT_CACHE
SUBAGENT_NOTIFY_GRACE=10   # see the header's point 2
# The subagent age window. MUST equal the literal in session_has_running_
# subagent's `[ "$age" -lt … ] || continue` (check-invariants pins that
# literal against CuaNotch; the unit test pins this against it).
SUBAGENT_WINDOW=3600
# First bounded read's opening chunk (grows 4x until it finds its anchor).
NOTIF_FIRST_CHUNK=1048576

# The python3 notified_ids runs, resolved once per process and without a
# fork: the first python3 on PATH that is not a version-manager shim. The
# watcher inherits a PATH with ~/.pyenv/shims first, and that shim is a
# script that costs ~260 ms per call against ~30 ms for a real interpreter
# (measured 2026-10-07). The script needs only the stdlib, so any python3 is
# fine; bare `python3` is the fallback.
_SL_PY=""
_sl_python() {
    [ -n "$_SL_PY" ] && return 0
    local d
    local -a dirs
    IFS=: read -ra dirs <<<"$PATH"
    for d in "${dirs[@]}"; do
        case "$d" in */shims|*/shims/) continue ;; esac
        [ -x "$d/python3" ] && { _SL_PY="$d/python3"; return 0; }
    done
    _SL_PY=python3
}

# notified_ids PARENT [SIZE [HORIZON]] → NOTIF_WHEN["PARENT|<id>"] = epoch of
# that id's LATEST notice. SIZE is the parent's size when the caller already
# stat'ed it (empty = missing, read nothing); without it this stats itself.
#
# HORIZON (epoch, optional) bounds the FIRST read. A notice older than
# now - SUBAGENT_WINDOW - GRACE can never finish an in-window subagent (its
# file would have to have moved within GRACE of the notice, so it would be
# out of the window too), so the first read starts at the last assistant
# record stamped before HORIZON - 3600 rather than at byte 0. Anchored on
# assistant records because timestamps are NOT in file order: a queued
# <task-notification> attachment is stamped when queued and written later,
# trailing earlier lines by up to 70 min (hook attachments by up to 78 h).
# An assistant record carries the time its streaming STARTED but is written
# when it ends, so a notice written just before it can be stamped later:
# measured 2026-10-07 over all 198 parent transcripts, up to 788 s (an
# enqueue record). Hence an hour of slack, not minutes; erring early only
# costs reading a little more of the file. No such anchor (format change, young file) → read from 0.
# Every later read is incremental from where this one stopped.
#
# Everything parsed from a transcript is hostile until proven otherwise: ids
# must be [A-Za-z0-9_-]+ (python and here) and epochs all digits, because a
# value reaching $(( )) is evaluated — `when=a[$(cmd)]` runs cmd.
notified_ids() {
    local p="$1" size have out consumed k tok key when line=""
    local horizon="${3:-0}"
    local -a toks
    if [ $# -ge 2 ]; then
        size="$2"; [ -n "$size" ] || return 0
    else
        size=$(stat -f %z "$p" 2>/dev/null) || return 0
    fi
    case "$size" in *[!0-9]*) return 0 ;; esac
    case "$horizon" in ''|*[!0-9]*) horizon=0 ;; esac
    have="${NOTIF_SIZE[$p]:-0}"
    [ "$size" = "$have" ] && return 0
    if [ "$size" -lt "$have" ]; then                                  # rotated
        have=0
        for k in "${!NOTIF_WHEN[@]}"; do
            [[ $k == "$p|"* ]] && unset 'NOTIF_WHEN[$k]'
        done
    fi
    # Measured 2026-10-07 on a 41 MB parent: python seeks itself (BSD
    # `tail -c +N` took 3.2 s), decodes only the lines holding the tag
    # (splitting everything cost ~100 ms), and is _sl_python's interpreter,
    # not a ~260 ms pyenv shim — re-resolved if it vanished (brew relink).
    [ -x "$_SL_PY" ] || _SL_PY=""
    _sl_python
    out=$("$_SL_PY" -I -S -c '
import sys, re, datetime
# THE notification tag, defined once, on the line check-invariants pins
# against CuaNotch (it greps the quoted text): change it there too.
TAG = """if "<task-id>" not in line""".split("\"")[1]
ID = re.compile(re.escape(TAG.encode()) + rb"([A-Za-z0-9_-]+)" + re.escape(TAG.replace("<", "</", 1).encode()))
TS = re.compile(rb"\"timestamp\":\"([^\"]+)\"")
ANCHOR = re.compile(rb"\"type\":\"assistant\",\"uuid\":\"[^\"]*\",\"timestamp\":\"([^\"]+)\"")
def epoch(raw):
    try:
        return int(datetime.datetime.fromisoformat(raw.decode().replace("Z", "+00:00")).timestamp())
    except Exception:
        return None
off, horizon, chunk = int(sys.argv[2]), int(sys.argv[3]), max(int(sys.argv[4]), 4096)
with open(sys.argv[1], "rb") as fh:
    begin = off
    if off == 0 and horizon > 0:
        end = fh.seek(0, 2)
        bound = horizon - 3600
        while True:
            pos = max(0, end - chunk)
            fh.seek(pos)
            data = fh.read()
            nl = data.find(b"\n")
            first = 0 if pos == 0 else (nl + 1 if nl >= 0 else len(data))   # whole lines only
            hit = -1
            for m in ANCHOR.finditer(data, first):
                t = epoch(m.group(1))
                if t is not None and t < bound:
                    hit = m.start()
            if hit >= 0:
                begin = pos + data.rfind(b"\n", 0, hit) + 1
                data = data[begin - pos:]
                break
            if pos == 0:
                break
            chunk *= 4
    else:
        fh.seek(off)
        data = fh.read()
cut = data.rfind(b"\n") + 1            # never consume a half-written record
def candidates(tag):                   # whole \n-terminated lines holding tag
    i = data.find(tag, 0, cut)
    while i != -1:
        s = data.rfind(b"\n", 0, i) + 1
        e = data.find(b"\n", i, cut)   # found: data[cut - 1] is the newline
        yield data[s:e]
        i = data.find(tag, e, cut)
ids = []
for line in candidates(TAG.encode()):
    m = ID.search(line)
    if not m:
        continue
    t = TS.search(line)
    ep = epoch(t.group(1)) if t else None
    ids.append("%s=%d" % (m.group(1).decode(), ep or 0))
print(begin - off + cut)
print(" ".join(ids))' "$p" "$have" "$horizon" "${NOTIF_FIRST_CHUNK:-1048576}" 2>/dev/null)
    # Nothing at all = the interpreter failed (or the file is unreadable):
    # forget it so the next call re-resolves one.
    if [ -z "$out" ]; then _SL_PY=""; return 0; fi
    { IFS= read -r consumed; IFS= read -r line; } <<<"$out"
    case "$consumed" in ''|*[!0-9]*) return 0 ;; esac
    IFS=' ' read -ra toks <<<"$line"     # not $line unquoted: caller's IFS/globs
    for tok in "${toks[@]}"; do          # in file order, so the latest wins
        key="${tok%=*}"; when="${tok##*=}"
        case "$key" in ''|*[!A-Za-z0-9_-]*) continue ;; esac
        case "$when" in ''|*[!0-9]*) continue ;; esac
        NOTIF_WHEN["$p|$key"]="$when"
    done
    NOTIF_SIZE[$p]=$((have + consumed))
}

# True if the claude session owning PID has a background SUBAGENT (the Agent
# tool) still out. The rules are the header's point 2; the order below is
# notified → interrupted → running, each `continue` a way to be finished.
# Cost per base: ONE stat fork for the parent and every agent-*.jsonl (see
# the header's COST note), notified_ids at most once and only if some file is
# inside the age window, tail only for a file whose mtime+size changed.
session_has_running_subagent() {
    local f mt size now age base id p psize when v last line dir asked
    local -a files stats
    resolve_session_bases "$1" || return 1
    printf -v now '%(%s)T' -1
    for base in "${SESSION_BASES[@]}"; do
        dir="$SESSION_PROJ/$base/subagents"
        [ -d "$dir" ] || continue
        files=("$dir"/agent-*.jsonl)
        [ -e "${files[0]:-}" ] || continue   # no match (or nullglob: empty)
        p="$SESSION_PROJ/$base.jsonl"
        # "<mtime> <size> <path>" per file, parent first (stat keeps argument
        # order; a missing parent just has no line). %N last, so a path with
        # spaces survives the two-field split below.
        mapfile -t stats < <(stat -f '%m %z %N' "$p" "${files[@]}" 2>/dev/null)
        psize=""; asked=""
        for line in "${stats[@]}"; do
            mt="${line%% *}"; line="${line#* }"
            size="${line%% *}"; f="${line#* }"
            case "$mt" in ''|*[!0-9]*) continue ;; esac
            case "$size" in ''|*[!0-9]*) continue ;; esac
            if [ "$f" = "$p" ]; then psize="$size"; continue; fi
            age=$((now - mt))
            [ "$age" -lt 3600 ] || continue
            id="${f##*/agent-}"; id="${id%.jsonl}"
            # 1. Notified since it last moved → finished.
            if [ -z "$asked" ]; then
                notified_ids "$p" "$psize" "$((now - SUBAGENT_WINDOW - SUBAGENT_NOTIFY_GRACE))"
                asked=1
            fi
            # Its LAST notification, O(1): not a loop over every token (hundreds)
            # per fresh file, and not a glob match on the token string either
            # (bash's *…* matching is quadratic — ~300 ms on a 30 KB string).
            # All digits or nothing: it goes into $(( )) next (see notified_ids).
            when="${NOTIF_WHEN["$p|$id"]:-}"
            case "$when" in *[!0-9]*) when="" ;; esac
            [ -n "$when" ] && [ $((when + SUBAGENT_NOTIFY_GRACE)) -ge "$mt" ] && continue
            # 2. Interrupted by the user → finished. Cached by mtime+size.
            v="${INT_CACHE[$f]:-}"
            if [ "${v%=*}" != "$mt.$size" ]; then
                # Keep the `| tail -n 1` fork: `${last##*$'\n'}` on 64 KB is
                # quadratic in bash (measured 159 ms, 2026-10-07).
                last=$(tail -c 65536 "$f" 2>/dev/null | tail -n 1)
                case "$last" in
                    *'"type":"user"'*'"type":"text","text":"[Request interrupted by user'*) v="$mt.$size=1" ;;
                    *) v="$mt.$size=0" ;;
                esac
                INT_CACHE[$f]="$v"
            fi
            [ "${v#*=}" = 1 ] && continue
            # 3. Otherwise it is running.
            return 0
        done
    done
    return 1
}

# True if the claude session owning PID has a background Workflow in flight.
# Runtime dir without its completion file = running (see header). Scoped to
# the session's own project/<sid> dirs so other panes' workflows don't leak in.
session_has_running_workflow() {
    local d wfid mt now base b
    resolve_session_bases "$1" || return 1
    printf -v now '%(%s)T' -1
    for b in "${SESSION_BASES[@]}"; do
    base="$SESSION_PROJ/$b"
    [ -d "$base/subagents/workflows" ] || continue
    for d in "$base"/subagents/workflows/wf_*/; do
        [ -d "$d" ] || continue
        # ${d%/} then ##*/, not basename: these paths are cwd-munged and
        # start with a dash ("-Users-mackhaymond-..."), which basename would
        # parse as options if the path were ever relative.
        wfid="${d%/}"; wfid="${wfid##*/}"
        [ -f "$base/workflows/$wfid.json" ] && continue   # completion file → done
        # Backstop against a crashed/stale runtime dir. mtime is the only
        # liveness signal, but transcripts go quiet during long stalls (API
        # backoff, a tool with no timeout, a permission gate), so the old
        # 600s floor darkened the gear on workflows that were still running
        # (worst measured quiet gap on this machine: 394s). An hour gives
        # 9x that margin and still SELF-HEALS: a plain age test, on purpose.
        # Anchoring "live" to the session's own start instead would mean a
        # dir created this session never expires, so one crashed run would
        # pin the gear — and since done+workflow renders the tab untinted,
        # that would suppress this window's green for the rest of the
        # session. CuaNotch's runningWorkflows() must keep the same rule.
        mt=$(stat -f %m "$d"/agent-*.jsonl "$d/journal.jsonl" 2>/dev/null | sort -rn | head -1)
        case "$mt" in *[!0-9]*) mt="" ;; esac   # digits only before $(( ))
        [ -n "$mt" ] && [ $((now - mt)) -lt 3600 ] && return 0
    done
    done
    return 1
}
