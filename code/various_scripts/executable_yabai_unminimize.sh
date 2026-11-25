#!/bin/bash

# A pinned window must not stay minimized: restore it.
#
#   yabai_unminimize.sh [window id]   # from yabairc's window_minimized signal (label
#                                     # unminimize_pinned), id in $YABAI_WINDOW_ID
#   yabai_unminimize.sh sweep         # every minimized window that qualifies: from the
#                                     # space_changed signal (unminimize_space_changed)
#                                     # and once at yabai start
#
# yabai_minimize.sh already swallows cmd+M on a pinned window; this catches the paths no
# key handler sees (the yellow button, the Window menu, an app minimizing itself).
# Pinned = yabai_is_pinned_at (yabai_common.sh): on its home space, or a main browser on
# main/school. Anything else stays minimized.
#
# The signal fires at the END of the minimize animation (~0.55s); the restore settles
# ~0.7s later. No --focus and no --space here. NOT filtered on AXStandardWindow: yabai
# reports subrole AXDialog for a minimized window. Label and visibility come from the
# INDEXED spaces query, as the bulk one sometimes answers a bare "[" (yabai_spaces_json).
#
# Only while the window's space is VISIBLE (on any display), re-read before every try:
# restoring a window of the ACTIVE app on another space could switch the space, or land
# the window wrong. Otherwise it is left for the sweep the arrival on that space fires.
# The sweep also covers a yabai that was restarted before it could signal the minimize.
#
# Loop breaker: after LOOP_MAX restores of one window within LOOP_SECS (an app that keeps
# re-minimizing it) there is a pause just past LOOP_SECS, then one last try. The lock is
# held meanwhile, so the total must stay under its 30s stale age. A sweep does not pause:
# the window's own signal handler does.
# Log: ~/Library/Logs/yabai/unminimize.log

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=/dev/null  # required sibling: yabai_is_pinned_at, locks, log
. "$SCRIPT_DIR/yabai_common.sh"

LOOP_MAX=8
LOOP_SECS=15
STATE="$HOME/.cache/yabai/unminimize"

mode=signal
[ "${1:-}" = sweep ] && mode=sweep

# look <window id>: set $seen (plus $app and $label) to what the window is right now:
#   go        minimized, root, pinned here, on a visible space
#   deferred  the same, but its space is not visible
#   restored  not minimized
#   unpinned  not a root window, or not pinned here
#   gone      the query failed: no such window, or no yabai
#   unknown   an answer that is not what it should be
look() {
  local out mini root space place
  seen=unknown
  out=$(yabai -m query --windows --window "$1" 2>/dev/null) || { seen=gone; return; }
  out=$(printf '%s' "$out" | jq -r 'select(type == "object")
    | "\(.["is-minimized"])|\(.["root-window"])|\(.space)|\(.app)"' 2>/dev/null)
  # Split by hand, not with `read`: that would trim an app name's outer spaces into a
  # pinned one.
  mini=${out%%|*};  out=${out#*|}
  root=${out%%|*};  out=${out#*|}
  space=${out%%|*}; app=${out#*|}
  case "$mini" in
    false) seen=restored; return ;;
    true) ;;
    *) return ;;
  esac
  case "$space" in ''|*[!0-9]*) return ;; esac
  [ "$root" = true ] || { seen=unpinned; return; }
  place=$(yabai -m query --spaces --space "$space" 2>/dev/null | jq -r '
    select(type == "object") | "\(.["is-visible"])|\(.label // "")"' 2>/dev/null)
  [ -n "$place" ] || return
  label=${place#*|}
  yabai_is_pinned_at "$app" "$label" || { seen=unpinned; return; }
  seen=deferred
  [ "${place%%|*}" = true ] && seen=go
}

log() { yabai_log unminimize "$1 id=$id app=\"$app\" label=$label${2:+ $2}"; }

# attempt <n> [log tag]: up to n deminimize + check rounds; logs the outcome. A command that
# lands during an animation can be dropped, hence the rounds. Each waits 0.8s: a clean
# restore settles in ~0.7s, so it costs one command.
attempt() {
  local try tag="${2:+ $2}"
  for ((try = 1; try <= $1; try++)); do
    look "$id"
    case "$seen" in
      go) yabai -m window --deminimize "$id" >/dev/null 2>&1 ;;
      unknown) ;;
      *) log "$seen" "try=$try$tag"; return ;;
    esac
    for _ in 1 2 3 4; do
      sleep 0.2
      look "$id"
      case "$seen" in restored|gone) log "$seen" "try=$try$tag"; return ;; esac
    done
  done
  log FAILED "tries=$1$tag"
}

# remember: rewrite the window's memo (one epoch second per line) to its entries
# of the last LOOP_SECS plus now, and set $count to how many there were. Anything else
# in the file is ignored. Written before acting: a window re-minimized too fast to be
# seen restored must still reach the limit.
remember() {
  local now t age recent="" memo="$STATE/$id"
  now=$(date +%s)
  count=0
  if [ -f "$memo" ]; then
    while IFS= read -r t || [ -n "$t" ]; do
      case "$t" in *[!0-9]*) continue ;; esac
      [ "${#t}" = 10 ] || continue
      age=$(( now - 10#$t ))
      [ "$age" -ge 0 ] && [ "$age" -lt "$LOOP_SECS" ] || continue
      recent="$recent$t
"
      count=$((count + 1))
    done < <(tail -n 20 "$memo" 2>/dev/null)
  fi
  [ "$count" -lt "$LOOP_MAX" ] || return
  { printf '%s%s\n' "$recent" "$now" >"$memo"; } 2>/dev/null
}

LOCK=""
trap '[ -z "$LOCK" ] || yabai_lock_release "$LOCK"' EXIT

# restore <window id>: the whole job for one window, for both modes.
restore() {
  local wait=50
  id="$1" app="" label=""
  look "$id"
  case "$seen" in
    go) ;;
    # Not from a sweep: it would repeat the line on every space switch.
    deferred) [ "$mode" = sweep ] || log deferred; return ;;
    *) return ;;
  esac
  # One handler per window: a second one (minimized again meanwhile) waits for the first,
  # then looks again. A sweep with other windows to see waits less.
  [ "$mode" = sweep ] && wait=10
  LOCK="$STATE/$id.lock"
  yabai_lock_acquire_wait "$LOCK" "$wait" || return
  look "$id"
  if [ "$seen" != restored ] && [ "$seen" != gone ]; then
    yabai_log_trim unminimize
    find "$STATE" -maxdepth 1 -type f -mtime +1 -delete 2>/dev/null
    remember
    if [ "$count" -lt "$LOOP_MAX" ]; then
      attempt 3
    elif [ "$mode" = signal ]; then
      log loop-break "restored=$count in=${LOOP_SECS}s; one more try in $((LOOP_SECS + 1))s"
      sleep $((LOOP_SECS + 1))
      remember
      attempt 1 after-loop-break
    fi
  fi
  yabai_lock_release "$LOCK"
}

if [ "$mode" = sweep ]; then
  # Same app filter as the signal's, so a minimized window of any other app costs nothing.
  ids=$(yabai -m query --windows 2>/dev/null | jq -r --arg re "$YABAI_PINNED_OR_BROWSER_RE" '
    select(type == "array") | .[]
    | select(.["is-minimized"] == true and .["root-window"] == true)
    | select((.app // "") | test($re)) | .id' 2>/dev/null)
  for id in $ids; do
    case "$id" in ''|*[!0-9]*) continue ;; esac
    restore "$id"
  done
  exit 0
fi

id="${YABAI_WINDOW_ID:-${1:-}}"
case "$id" in ''|*[!0-9]*) exit 0 ;; esac
restore "$id"
exit 0
