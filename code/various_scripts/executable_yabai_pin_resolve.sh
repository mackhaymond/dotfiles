#!/bin/bash

# Unresolved-pin heal -- restarts yabai when a PINNED app has a window yabai can see
# but cannot act on, which strands that window on whatever space it opened on until
# a manual restart (see YABAI_JQ_PIN_UNRESOLVED in yabai_common.sh for the yabai
# internals). The recurring case is Claude Desktop: after login, and after its
# stealth update relaunch, its window sits on a random space (`agent` on 2026-10-06)
# and `rule --apply`, `window --space` and the float sweep all fail on it with
# "could not locate the window to act on!". A restart re-runs yabai's brute-force
# window discovery; the space= rules + startup reconcile then home and tile it.
#
#   yabai_pin_resolve.sh <event> [--delay S] [--watch S] [--dry-run]
#
#   --delay S  sleep S seconds before the first look (wake / startup: let windows settle)
#   --watch S  keep looking for up to S seconds (app launch: its window shows up a few
#              seconds AFTER application_launched -- ~6s for a stealth relaunch)
#
# Triggers (yabairc): application_launched (pinned apps), system_woke, the end of the
# startup poll, and every float-sweep run (space_changed / window_focused /
# window_deminimized) whose single query already shows an unresolved pin -- so the
# idle cost is zero extra forks: this script only runs when something is wrong or a
# pinned app just launched.
#
# Guards, since the remedy is a full restart:
#   - CONFIRM: the same window must still be unresolved CONFIRM_SECONDS later (a window
#     yabai is mid-resolving via kAXWindowCreated is briefly unresolved too).
#   - Never during a live startup poll (the poll calls this itself when it ends).
#   - At most one restart per MIN_GAP seconds, machine-wide.
#   - At most MAX_PER_WINDOW restarts per window id (CGWindowIDs survive yabai
#     restarts; memo entries expire after a day): a window a restart cannot resolve
#     -- an AX-invisible helper -- is logged once and left alone, never restart-looped.
#
# Log: ~/Library/Logs/yabai/resolve.log. State: ~/Library/Caches/yabai/.

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

EVENT="${1:-manual}"
shift 2>/dev/null || true
DELAY=0
WATCH=0
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --delay)   DELAY="${2:-0}"; shift 2 ;;
    --watch)   WATCH="${2:-0}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "usage: $0 <event> [--delay S] [--watch S] [--dry-run]" >&2; exit 64 ;;
  esac
done

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/yabai_common.sh"

LOGN=resolve
LOCK="$YABAI_STATE_DIR/pin_resolve.lock"
STARTUP_LOCK="$YABAI_STATE_DIR/startup_reconcile.lock"
MEMO="$YABAI_STATE_DIR/resolve_memo"        # lines: <window id> <restarts> <epoch of last>
LAST="$YABAI_STATE_DIR/resolve_last_restart" # epoch of the last restart we issued
mkdir -p "$YABAI_STATE_DIR" 2>/dev/null
touch "$MEMO" 2>/dev/null

CONFIRM_SECONDS=2
MIN_GAP=30
MAX_PER_WINDOW=2

# Single-flight: a burst of triggers (launch + focus + space change) runs one check.
# A holder older than 120s is an orphan (a restart kills yabai's process group).
if ! mkdir "$LOCK" 2>/dev/null; then
  m=$(stat -f %m "$LOCK" 2>/dev/null || echo 0)
  [ $(( $(date +%s) - m )) -ge 120 ] || exit 0
  rm -rf "$LOCK" 2>/dev/null
  mkdir "$LOCK" 2>/dev/null || exit 0
fi
trap 'rm -rf "$LOCK" 2>/dev/null || true' EXIT

# "<id> <app> <space>" per unresolved pinned window still within its restart budget
# (one query + one jq + one awk). Budget-spent ids are logged ONCE (marker file) and
# dropped here, so a permanently stuck helper costs nothing but this look.
unresolved() {
  local now
  now=$(date +%s)
  yabai -m query --windows 2>/dev/null | jq -r --arg re "$YABAI_PINNED_APPS_RE" "
    [ .[] | select(.app | test(\$re)) ] | $YABAI_JQ_PIN_UNRESOLVED
    | \"\(.id) \(.app | gsub(\" \"; \"_\")) \(.space)\"" 2>/dev/null \
  | awk -v max="$MAX_PER_WINDOW" -v now="$now" -v memo="$MEMO" '
      BEGIN { while ((getline l < memo) > 0) { split(l, f, " "); if (f[3] > now - 86400) n[f[1]] = f[2] } }
      { if (($1 in n) && n[$1] >= max) { print "SPENT " $0 } else print }'
}

startup_live() {
  [ -d "$STARTUP_LOCK" ] || return 1
  local m
  m=$(stat -f %m "$STARTUP_LOCK/alive" 2>/dev/null || stat -f %m "$STARTUP_LOCK" 2>/dev/null || echo 0)
  [ $(( $(date +%s) - m )) -lt 30 ]
}

# Splits unresolved() output: logs each budget-spent window once, prints the rest.
actionable() {
  local tag id app space
  while read -r tag id app space; do
    [ -n "$tag" ] || continue
    if [ "$tag" = SPENT ]; then
      if [ ! -e "$YABAI_STATE_DIR/resolve_gaveup_$id" ]; then
        : >"$YABAI_STATE_DIR/resolve_gaveup_$id"
        yabai_log $LOGN "gave-up id=$id app=$app space=$space ev=$EVENT ($MAX_PER_WINDOW restarts did not resolve it)"
      fi
      continue
    fi
    # Not SPENT: the first field is the id.
    printf '%s %s %s\n' "$tag" "$id" "$app"
  done
}

[ "$DELAY" != 0 ] && sleep "$DELAY"
deadline=$(( $(date +%s) + WATCH ))

while :; do
  first=$(unresolved | actionable)
  [ -n "$first" ] && break
  [ "$(date +%s)" -lt "$deadline" ] || exit 0
  sleep 2
done

startup_live && exit 0

sleep "$CONFIRM_SECONDS"
second=$(unresolved | actionable)
# Still stuck = the same window id in both looks.
stuck=$(printf '%s\n' "$second" | awk 'NR == FNR { seen[$1] = 1; next } ($1 in seen)' \
  <(printf '%s\n' "$first") - 2>/dev/null)
[ -n "$stuck" ] || exit 0
summary=$(printf '%s' "$stuck" | tr '\n' ';')

now=$(date +%s)
last=$(cat "$LAST" 2>/dev/null || echo 0)
if [ $((now - ${last:-0})) -lt "$MIN_GAP" ]; then
  yabai_log $LOGN "defer ev=$EVENT reason=min-gap last=$((now - last))s-ago stuck=[$summary]"
  exit 0
fi

if [ "$DRY_RUN" = 1 ]; then
  echo "would restart yabai for: $summary"
  exit 0
fi

yabai_log_trim $LOGN
# Memo + log BEFORE the restart: launchd tears down yabai's process group, which
# includes this script when a yabai signal launched it.
awk -v now="$now" -v ids="$(printf '%s\n' "$stuck" | awk '{ printf "%s ", $1 }')" '
  BEGIN { k = split(ids, a, " "); for (i = 1; i <= k; i++) want[a[i]] = 1 }
  ($1 in want) { cnt[$1] = $2; next }
  $3 > now - 86400 { print }
  END { for (id in want) print id, cnt[id] + 1, now }' "$MEMO" >"$MEMO.tmp" 2>/dev/null \
  && mv "$MEMO.tmp" "$MEMO" 2>/dev/null
echo "$now" >"$LAST"
yabai_log $LOGN "restart ev=$EVENT stuck=[$summary]"
rm -rf "$LOCK" 2>/dev/null
yabai --restart-service >/dev/null 2>&1 || yabai_log $LOGN "restart-FAILED ev=$EVENT"
