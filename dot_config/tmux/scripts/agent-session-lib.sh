# agent-session-lib.sh — the ONE implementation of two questions both
# agent-tab-watcher.sh and stash.sh have to answer about a claude process:
#
#   1. WHERE DOES THIS SESSION'S RUNTIME STATE LIVE?  (resolve_session_bases)
#      After a compaction, ~/.claude/sessions/<pid>.json can go on reporting
#      the ORIGINAL sessionId while the continued conversation gets a NEW id
#      and its transcript, subagents/ and workflows/ all move under that one.
#      Measured 2026-08-25: a process reporting 88d5cc09 had two running
#      reviewers under 42a666ca/; every guard that looked under 88d5cc09/
#      found nothing and let a park SIGTERM it with both reviewers mid-run.
#      The lineage's one precise anchor is the continued transcript's
#      compaction record — a user entry flagged "isCompactSummary":true whose
#      text names the old transcript by PATH (…/<old sid>.jsonl), both on ONE
#      line. A bare match on the id is wrong: transcripts quote other
#      sessions' ids in conversation all day (measured: four false
#      descendants where the anchor found exactly the real one). Walked
#      TRANSITIVELY — a session can compact more than once, and a one-level
#      walk goes blind to the grandchild dir exactly the way the original
#      incident's guards went blind to the child.
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
#      are given. A workflow is in-flight iff its runtime dir
#      subagents/workflows/wf_<id>/ exists without its completion file
#      workflows/wf_<id>.json, with the same one-hour mtime backstop
#      (transcripts go quiet during long stalls — worst measured gap 394s —
#      so the backstop must dwarf that; an hour gives 9x).
#
# HISTORY. These functions were born in the watcher, and stash.sh grew its
# own parallel copies of both answers while the watcher was blind to
# compaction (fixed in c554ffa). The private copies drifted: stash's chain
# walk was one level deep and mtime-filtered (blind to a double compaction
# and to a chain whose middle link had gone quiet), and its subagent rule was
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

# Resolve a claude PID to its session's runtime dirs. SESSION_PROJ is the
# project dir (~/.claude/projects/<proj>) and SESSION_BASES the session ids
# under it that belong to this process — PLURAL, because of compaction (see
# the header). Cached per pid+sid for a minute: the grep over every
# transcript in the project is the one thing here that is not free.
declare -A SB_CACHE SB_CACHE_AT
SESSION_PROJ=""
SESSION_BASES=()
resolve_session_bases() {
    local pid="$1" sf sid="" cwd="" proj raw now key dirs frontier found id f fid
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
    key="$pid:$sid"
    printf -v now '%(%s)T' -1
    if [ -n "${SB_CACHE[$key]:-}" ] && [ $((now - SB_CACHE_AT[$key])) -lt 60 ]; then
        read -ra SESSION_BASES <<<"${SB_CACHE[$key]}"
        return 0
    fi
    dirs="$sid"; frontier="$sid"
    while [ -n "$frontier" ]; do
        found=""
        for id in $frontier; do
            while IFS= read -r f; do
                [ -n "$f" ] || continue
                fid="${f##*/}"; fid="${fid%.jsonl}"
                case " $dirs " in *" $fid "*) continue ;; esac
                # Both markers on one line, either order.
                grep -q -m1 -E "\"isCompactSummary\":true.*/$id\.jsonl|/$id\.jsonl.*\"isCompactSummary\":true" "$f" 2>/dev/null || continue
                dirs="$dirs $fid"; found="$found $fid"
            done <<EOF
$(grep -lF -- "/$id.jsonl" "$SESSION_PROJ"/*.jsonl 2>/dev/null)
EOF
        done
        frontier="$found"
    done
    SB_CACHE[$key]="$dirs"; SB_CACHE_AT[$key]=$now
    read -ra SESSION_BASES <<<"$dirs"
}

# The parent transcript is read INCREMENTALLY (bytes appended since the last
# look, one python over the delta, only when a fresh subagent file makes the
# answer matter), and the interrupt verdict is cached by mtime+size, so a
# finished file costs one tail read rather than two forks a second.
declare -A NOTIF_SIZE NOTIF_IDS INT_CACHE
notified_ids() {   # $1 parent transcript → NOTIF_IDS[$1] = " id=epoch id=epoch "
    local p="$1" size have out consumed
    size=$(stat -f %z "$p" 2>/dev/null) || return 0
    have="${NOTIF_SIZE[$p]:-0}"
    [ "$size" = "$have" ] && return 0
    if [ "$size" -lt "$have" ]; then NOTIF_IDS[$p]=" "; have=0; fi   # rotated
    out=$(tail -c +$((have + 1)) "$p" 2>/dev/null | python3 -c '
import sys, re, datetime
data = sys.stdin.buffer.read()
cut = data.rfind(b"\n") + 1            # never consume a half-written record
ids = []
for line in data[:cut].decode("utf-8", "replace").splitlines():
    if "<task-id>" not in line:
        continue
    m = re.search(r"<task-id>([^<]+)</task-id>", line)
    if not m:
        continue
    ep = 0
    t = re.search(r"\"timestamp\":\"([^\"]+)\"", line)
    if t:
        try:
            ep = int(datetime.datetime.fromisoformat(t.group(1).replace("Z", "+00:00")).timestamp())
        except Exception:
            pass
    ids.append("%s=%d" % (m.group(1), ep))
print(" ".join(ids))
print(cut)')
    consumed="${out##*$'\n'}"
    out="${out%$'\n'*}"
    [ "$consumed" -eq "$consumed" ] 2>/dev/null || return 0
    NOTIF_IDS[$p]="${NOTIF_IDS[$p]:- }${out} "
    NOTIF_SIZE[$p]=$((have + consumed))
}

# True if the claude session owning PID has a background SUBAGENT (the Agent
# tool) still out. The rules are the header's point 2; the order below is
# notified → interrupted → running, each `continue` a way to be finished.
session_has_running_subagent() {
    local f mt size now age base id p tok when v last
    resolve_session_bases "$1" || return 1
    printf -v now '%(%s)T' -1
    for base in "${SESSION_BASES[@]}"; do
        [ -d "$SESSION_PROJ/$base/subagents" ] || continue
        p="$SESSION_PROJ/$base.jsonl"
        for f in "$SESSION_PROJ/$base"/subagents/agent-*.jsonl; do
            [ -f "$f" ] || continue
            mt=$(stat -f %m "$f" 2>/dev/null)
            [ -n "$mt" ] || continue
            age=$((now - mt))
            [ "$age" -lt 3600 ] || continue
            id="${f##*/agent-}"; id="${id%.jsonl}"
            # 1. Notified since it last moved → finished.
            notified_ids "$p"
            when=""
            for tok in ${NOTIF_IDS[$p]:-}; do
                case "$tok" in "$id="*) when="${tok#*=}" ;; esac
            done
            [ -n "$when" ] && [ "$when" -ge "$mt" ] && continue
            # 2. Interrupted by the user → finished. Cached by mtime+size.
            size=$(stat -f %z "$f" 2>/dev/null)
            v="${INT_CACHE[$f]:-}"
            if [ "${v%=*}" != "$mt.$size" ]; then
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
        [ -n "$mt" ] && [ $((now - mt)) -lt 3600 ] && return 0
    done
    done
    return 1
}
