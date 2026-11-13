#!/bin/bash

# Draw a JankyBorders border around FLOATING windows only.
#
# yabai has no native per-window border, and JankyBorders (`borders`) filters by
# APP NAME, not by float state -- it has no idea what yabai considers floating. So
# this computes the set of apps that CURRENTLY have a floating window and drives the
# borders daemon's `whitelist` to exactly that set: it starts the daemon (with the
# style below) when the first floating window appears, live-updates the whitelist as
# the set changes, and kills the daemon when the last floating window goes away (so
# borders runs only while something floats -- not a brew service / always-on).
#
# SCOPE = ALL is-floating windows. yabai reports `is-floating: true` for a window you
# floated (hyper+t) AND for a manage=off app (Finder, System Settings, ...) -- there
# is no per-window "managed" flag to tell them apart -- so manage=off apps are
# bordered whenever open. (Chosen over a drift-prone second copy of the yabairc
# manage=off list.)
#
# CAVEAT (inherent to JankyBorders' app-granularity, NOT a bug): the whitelist is by
# app, so if one app has BOTH a floating and a tiled window (e.g. a floating Little
# Arc popup while the main Arc window is tiled), BOTH windows get the border.
#
# SINGLE-FLIGHT (mkdir lock, mirrors yabai_heal.sh): coalesces a burst of float-set
# changes into one reconcile AND -- critically -- holds the lock across the whole
# start/update decision so two concurrent syncs (e.g. a window_created racing the
# hyper+t toggle) can't BOTH cold-start and spawn DUPLICATE daemons. `borders` is
# NOT a process-level singleton (two cold-starts => two daemons, verified), so the
# lock is load-bearing; a daemon count != 1 is also self-healed to exactly one.
#
#   yabai_float_borders.sh sync   # reconcile borders to the current floating set
#
# NOT bordered right away: a pinned app's main window that yabai floated on its own
# (GRACE below) -- the float sweep normally tiles it within a moment.
#
# Wired to: yabairc window_created / window_destroyed signals + a startup sync, and
# the hyper+t toggle (yabai_toggle_float.sh) -- the events where the float set changes
# (a plain --toggle float emits no window_created/destroyed, so the toggle script
# calls sync itself).

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"
export LANG="${LANG:-en_US.UTF-8}"   # borders needs a UTF-8 locale (brew caveat)

BORDERS=/opt/homebrew/bin/borders
[ -x "$BORDERS" ] || exit 0          # degrade to a no-op if borders isn't installed

# Border style. active = focused window (white), inactive = unfocused (subtle gray).
# Round, 2.0pt -- the subtle-white look. Style is set once at daemon start; later
# syncs only push the whitelist (the daemon keeps the style).
BORDERS_STYLE=(style=round width=2.0 active_color=0xffffffff inactive_color=0xff5c6370 hidpi=on)

cmd="${1:-sync}"
[ "$cmd" = "sync" ] || exit 64

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=/dev/null  # required sibling: the shared state dir + pinned apps
. "$SCRIPT_DIR/yabai_common.sh"
# Shared state dir, not ${TMPDIR:-/tmp}: this is called from yabai signals (which
# have yabai's TMPDIR) AND from skhd's hyper+t (which has none) -- under TMPDIR the
# two callers locked different directories and the single-flight was fiction.
LOCK="$YABAI_STATE_DIR/float_borders.lock"
AGAIN="$YABAI_STATE_DIR/float_borders.again"
SETTLE="${YABAI_BORDERS_SETTLE:-0.15}"

# GRACE: a pinned app's window that yabai floated BY ITSELF is not bordered until it
# has stayed floating this many seconds. yabai sets FLOAT at window creation on a
# window it momentarily cannot move, or whose level is not 0, and never clears it on
# its own (window_manager_create_and_add_window); the float sweep
# (yabai_startup_reconcile.sh) un-floats it at the next focus / space switch. The
# window_created sync ran in between and whitelisted the app, so a border flashed
# around a window that ended up tiled. The windows deferred are exactly the sweep's
# candidates: a pinned (or coding-agent) app, YABAI_JQ_PIN_ELIGIBLE, and no keep-float
# marker. Everything else is bordered at once, as before: an unpinned app, a manage=off
# app, a pinned app's dialog or fixed-size window, and a pinned window the user floated
# with hyper+t (yabai_toggle_float.sh writes keep-float/<id> BEFORE it calls sync).
# One that the sweep cannot un-float keeps floating and gets its border after GRACE.
# grace/<window id> = "first seen floating", by mtime. 0 disables the deferral.
GRACE="${YABAI_BORDERS_GRACE:-2}"
case "$GRACE" in ''|*[!0-9]*) GRACE=2 ;; esac
GRACE=$((10#$GRACE))                  # "08" is not octal
GRACE_DIR="$YABAI_STATE_DIR/float_borders.grace"
# The one pending "look again" sleeper (its pid), and how many times in a row a look
# found yabai not answering (handed from one recheck to the next; see reconcile).
RECHECK="$YABAI_STATE_DIR/float_borders.recheck"
retry="${YABAI_BORDERS_RETRY:-0}"
case "$retry" in ''|*[!0-9]*) retry=0 ;; esac
retry=$((10#$retry))
KEEP_FLOAT="$YABAI_STATE_DIR/keep-float"
mkdir -p "$GRACE_DIR" 2>/dev/null     # also creates $YABAI_STATE_DIR
# No place to remember "first seen" -> no deferral (it would never end).
defer=true
{ [ "$GRACE" -gt 0 ] && [ -d "$GRACE_DIR" ] && [ -w "$GRACE_DIR" ]; } || defer=false

# Single-flight. Recover a lock orphaned by a crashed holder (older than 30s) using
# BSD stat -- matching yabai_heal.sh / yabai_displays.sh (GNU `find -mmin` is not
# portable here).
#
# A concurrent sync that loses the mkdir leaves $AGAIN. Just dropping was only right
# while the holder was still in its settle sleep: one that had ALREADY read the float
# set pushed that older set afterwards, so the sync the float sweep sends after an
# un-float could be lost to the window_created sync of the same window and the
# whitelist entry (and the border) stayed until the next window opened or closed.
# The request is HANDED OVER, never dropped: the holder releases the lock FIRST and
# only then looks for $AGAIN (bottom of this file), and the loser writes $AGAIN FIRST
# and only then tries the lock once more. Whichever way the two interleave, either the
# holder sees the file or the loser gets the lock and does the pass itself.
if ! mkdir "$LOCK" 2>/dev/null; then
  now=$(date +%s)
  mtime=$(stat -f %m "$LOCK" 2>/dev/null || printf '%s' "$now")
  [ "$((now - mtime))" -ge 30 ] && rmdir "$LOCK" 2>/dev/null
  if ! mkdir "$LOCK" 2>/dev/null; then
    : 2>/dev/null >"$AGAIN"
    mkdir "$LOCK" 2>/dev/null || exit 0
  fi
fi
# `held` so the explicit release below and this trap never remove the lock twice --
# the second rmdir would take the lock of whoever acquired it in between.
held=1
trap '[ "$held" = 1 ] && rmdir "$LOCK" 2>/dev/null' EXIT

# Look again in GRACE+1 seconds (+1: mtimes are whole seconds): nothing signals "still
# floating". At most ONE sleeper at a time, however many syncs ask -- $RECHECK holds
# its pid, and counts only while that process is alive and the file is recent (a pid
# reused by something else after a restart must not block rechecks for good). The
# sleeper removes the file before it syncs, so that sync can schedule the next one.
# Survives this short-lived signal/keybind shell. $1 = retry count to hand on.
schedule_recheck() {
  local pid="" m now
  if [ -e "$RECHECK" ]; then
    { read -r pid <"$RECHECK"; } 2>/dev/null
    now=$(date +%s)
    m=$(stat -f %m "$RECHECK" 2>/dev/null || echo 0)
    case "$pid" in
      ''|*[!0-9]*) ;;
      *) [ "$((now - m))" -le "$((GRACE + 5))" ] && kill -0 "$pid" 2>/dev/null && return 0 ;;
    esac
  fi
  ( trap '' HUP; sleep "$((GRACE + 1))"; rm -f "$RECHECK"
    YABAI_BORDERS_RETRY="$1" exec "$SCRIPT_DIR/${0##*/}" sync ) >/dev/null 2>&1 &
  echo "$!" 2>/dev/null >"$RECHECK"
  disown 2>/dev/null || true
}

# Windows whose grace marker could not be written: bordered now instead of deferred.
force=""

reconcile() {
  local f m now="" ripe="$force" keep="" out apps wait cand id failed=0 marks=0 n

  # Deferred windows whose grace has run out, and the user's own floats. Globs, not
  # `ls`: both directories are normally empty and this then forks nothing.
  for f in "$GRACE_DIR"/*; do
    [ -e "$f" ] || continue
    marks=1
    [ -n "$now" ] || now=$(date +%s)
    m=$(stat -f %m "$f" 2>/dev/null) || continue
    [ "$((now - m))" -ge "$GRACE" ] && ripe="$ripe ${f##*/}"
  done
  for f in "$KEEP_FLOAT"/*; do
    [ -e "$f" ] && keep="$keep ${f##*/}"
  done

  # Line 1: distinct app names that have a floating window to border, comma-joined
  # (empty if none). manage=off apps are intentionally included (see header).
  # Line 2: ids of the windows deferred this pass. Line 3: ids of every window the
  # deferral applies to, ripe or not (the grace markers to keep).
  # A failed or garbled query is "don't know", not "nothing floats": jq exits non-zero
  # on anything but an array (and on no input at all), and we keep the daemon as it is
  # rather than pkill it.
  out=$(yabai -m query --windows 2>/dev/null \
    | jq -re --arg pinned "$YABAI_PINNED_HOMES" --arg agents "$YABAI_AGENT_APPS" \
          --arg keep "$keep" --arg ripe "$ripe" --argjson defer "$defer" '
      def ids: split(" ") | map(select(. != ""));
      if type != "array" then error("not an array") else . end
      | (($pinned | split("|") | map(split(":")[0])) + ($agents | split("|"))) as $pins
      | ($keep | ids) as $keep
      | [.[] | select(."is-floating" == true)] as $floating
      | [ $floating[]
          | select($defer)
          | select(.app as $a | $pins | any(. == $a))
          | '"$YABAI_JQ_PIN_ELIGIBLE"'
          | (.id | tostring)
          | select(. as $i | $keep | any(. == $i) | not) ] as $cand
      | ($cand - ($ripe | ids)) as $wait
      | ([ $floating[] | select((.id | tostring) as $i | $wait | any(. == $i) | not) | .app ]
          | unique | join(",")),
        ($wait | join(" ")),
        ($cand | join(" "))' 2>/dev/null) || {
    # Windows are waiting for their border and this look told us nothing: look again,
    # or they stay bare until an unrelated window opens or closes. A few times only --
    # with yabai gone for good this must not tick forever.
    [ "$marks" = 1 ] && [ "$retry" -lt 5 ] && schedule_recheck "$((retry + 1))"
    return 0
  }
  { IFS= read -r apps; IFS= read -r wait; IFS= read -r cand; } <<EOF
$out
EOF

  # Start the clock for a newly deferred window; forget the windows that are no longer
  # deferred-or-ripe (un-floated, closed, or floated by the user in the meantime).
  for id in $wait; do
    [ -e "$GRACE_DIR/$id" ] && continue
    : >"$GRACE_DIR/$id" || { force="$force $id"; failed=1; }
  done 2>/dev/null
  for f in "$GRACE_DIR"/*; do
    [ -e "$f" ] || continue
    case " $cand " in *" ${f##*/} "*) ;; *) rm -f "$f" 2>/dev/null ;; esac
  done
  # A window with no marker has no clock, so its deferral would never end: read the set
  # again with it counted as ripe. (Ends: an id in $force is never deferred again.)
  if [ "$failed" = 1 ]; then
    reconcile
    return 0
  fi
  # Anything still deferred -> make sure a look is pending. Not only when a marker is
  # new: the one look scheduled then can be lost (yabai not answering, sleeper killed).
  [ -n "$wait" ] && schedule_recheck 0

  n=$(pgrep -x borders | wc -l | tr -d ' ')

  if [ -z "$apps" ]; then
    # Nothing floats -> stop the daemon entirely (no borders anywhere).
    [ "${n:-0}" -gt 0 ] && pkill -x borders 2>/dev/null
    return 0
  fi

  if [ "${n:-0}" -eq 1 ]; then
    # Exactly one daemon (ours) -> live-update just the whitelist; it keeps the style.
    "$BORDERS" "whitelist=$apps" >/dev/null 2>&1 || true
  else
    # None (cold start), or >1 (duplicates slipped in before this lock existed) ->
    # normalize to EXACTLY one daemon. nohup + disown so it survives this short-lived
    # signal/keybind shell.
    [ "${n:-0}" -gt 0 ] && { pkill -x borders 2>/dev/null; sleep 0.2; }
    nohup "$BORDERS" "${BORDERS_STYLE[@]}" "whitelist=$apps" >/dev/null 2>&1 &
    disown 2>/dev/null || true
  fi
}

# Requests up to here are covered by the read below; later ones write $AGAIN anew.
[ -e "$AGAIN" ] && rm -f "$AGAIN" 2>/dev/null
# Let a burst (e.g. an app opening several windows) settle, then read the final set.
sleep "$SETTLE"
reconcile

# Release, THEN look for a request that came in after the read (order matters, see the
# single-flight comment). One was left -> do its pass as a fresh process: a new lock
# with a new mtime, so a chain of them never looks orphaned to the 30s recovery. No
# cap needed, and none wanted (a capped loop here dropped the LAST state): $AGAIN is
# only ever written by a sync that lost the lock, so every pass is paid for by an
# event, and each costs a settle sleep + one query, which is what paces a burst.
rmdir "$LOCK" 2>/dev/null
held=0
[ -e "$AGAIN" ] && YABAI_BORDERS_RETRY=0 exec "$SCRIPT_DIR/${0##*/}" sync
exit 0
