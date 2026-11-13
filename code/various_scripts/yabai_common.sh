#!/bin/bash
# Shared constants + helpers for the yabai workspace scripts.
#
# This file is SOURCED, not executed:
#   SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
#   . "$SCRIPT_DIR/yabai_common.sh"
#
# It is a REQUIRED sibling of the yabai_* scripts (deployed together by chezmoi
# into the same directory). It carries the single copies of: the master-display
# UUID, the canonical label list, the master-index resolver, and the topology-
# cache loader -- previously duplicated across 5+ scripts.
#
# vim:ft=bash

# Canonical master (laptop) display UUID. Override via env for a different machine
# (a board swap changes the UUID). Defined once here -- nowhere else.
: "${YABAI_MASTER_DISPLAY_UUID:=37D8832A-2D66-02CA-B9F7-8F30A301B230}"

# Displays the workspace scripts must treat as if they don't exist, `|`-separated
# UUIDs. Default: the BetterDisplay virtual screen agent-chrome parks agent browsers
# on (cua-notch bin/agent-chrome VD_UUID). Counting it made caps+F2 (F14 ->
# yabai_display.sh external) focus the agent display whenever it was attached.
: "${YABAI_IGNORED_DISPLAY_UUIDS:=F573F4AB-DD26-4536-8012-F2BA4929E1F4}"

# Live `yabai -m query --displays` minus the ignored displays. Indices are yabai's
# own (unchanged), so they stay valid for `display --focus N` / `space --display N`.
# Emits nothing (and returns 1) if the query fails or isn't a JSON array.
yabai_displays_json() {
  yabai -m query --displays 2>/dev/null | yabai_filter_displays
}

# Filter a displays-JSON array on stdin down to the real (non-ignored) displays.
yabai_filter_displays() {
  jq -c --arg ignored "$YABAI_IGNORED_DISPLAY_UUIDS" '
    ($ignored | ascii_upcase | split("|")) as $ig
    | if type == "array" then [.[] | select((.uuid // "" | ascii_upcase) as $u | $ig | index($u) | not)]
      else error("not an array") end
  ' 2>/dev/null
}

# True if display index $1 is an ignored display (live query).
yabai_display_ignored() {
  local uuid
  uuid=$(yabai -m query --displays --display "$1" 2>/dev/null | jq -r '.uuid // empty' 2>/dev/null)
  [ -n "$uuid" ] || return 1
  case "|$(printf '%s' "$YABAI_IGNORED_DISPLAY_UUIDS" | tr '[:lower:]' '[:upper:]')|" in
    *"|$(printf '%s' "$uuid" | tr '[:lower:]' '[:upper:]')|"*) return 0 ;;
  esac
  return 1
}

# Canonical labeled spaces, in stable display order. The single source of this list.
# (Consumed by the sourcing scripts' `for label in $YABAI_LABELS` loops.)
# shellcheck disable=SC2034
YABAI_LABELS="terminal main school todo schedule mail calendar messages ai agent"

# --- agent: the generic coding-agent view (hyper+esc) ---------------------------
# `agent` is a normal labeled space, but it is NOT pinned to one app: any coding-
# agent GUI homes there, whichever of them happens to be running (Conductor is the
# primary). It replaced the old single-app `codex` space when Codex.app went away.
#
# This list is the SINGLE source of the app set. It is consumed by:
#   - yabairc            (the `space=agent` rule, via YABAI_AGENT_APPS_RE)
#   - yabai_workspace_refresh.sh   (labeling the space by whoever is running)
#   - yabai_send_window{,_external}.sh / yabai_toggle_float.sh  (pinned-home guard)
#   - yabai_startup_reconcile.sh   (the login-race home map, via yabai_home_map_json)
# Add an app HERE and every consumer picks it up. Names are yabai's `.app` field
# (the app's display name), `|`-separated.
YABAI_AGENT_LABEL="agent"
YABAI_AGENT_APPS="Conductor|Claudia|OpenChamber|OpenCode|Jean|T3 Code (Alpha)|Muse"

# The same list as a yabai `rule --add app=` regex: anchored, with the literal
# parens of names like "T3 Code (Alpha)" escaped so they aren't read as a group.
# shellcheck disable=SC2034
YABAI_AGENT_APPS_RE="^($(printf '%s' "$YABAI_AGENT_APPS" | sed 's/[()]/\\&/g'))$"

# --- pinned apps: app -> home space label ---------------------------------------
# The single SHELL-side source of the pinned-app home map, in the same `|`-delimited
# no-eval-no-regex shape as YABAI_AGENT_APPS (so names with spaces need no quoting
# care). yabairc's `space=` rules are generated from it (yabai_pin_rules_add below),
# and Arc is deliberately absent here: its two main windows are pinned by
# Hammerspoon's arcSync(), not by a `space=` rule.
#
# Consumed by: yabai_pin_rules_add (yabairc + yabai_workspace_refresh.sh),
# yabai_startup_reconcile.sh (the login-race + stray-float home map), and
# yabai_workspace_refresh.sh (which label a pinned app's space should carry).
# yabai_send_window{,_external}.sh and yabai_toggle_float.sh still carry their own
# `case` copies; folding those in is a worthwhile follow-up, not a silent one.
YABAI_PINNED_HOMES="wezterm-gui:terminal|WezTerm:terminal|Todoist:todo|Granola:schedule|Spark Mail:mail|Notion Calendar:calendar|Messages:messages|Claude:ai|ChatGPT:ai"

# Pinned apps whose rule also carries `manage=on`, `|`-separated. Claude Desktop comes
# back from its background auto-update at CG window level -1 (it restores level 0 on
# the first focus), and yabai floats any window whose level is not 0 -- unless a
# manage=on rule matches, which skips that classification. A standard window only
# (yabai ignores manage=on for other subroles unless the rule names one), so helper
# windows and panels are untouched. REQUIRES the patched yabai (README, "Patched
# yabai"): upstream re-tiles a manage=on window onto the ACTIVE space on every
# `rule --apply`, which put a background Claude into the focused space's stack. Side
# effect: hyper+t floating such an app off its home space lasts until the next
# `rule --apply` (any app launch).
YABAI_PINNED_MANAGED="Claude"

# Every app the float sweep can act on -- the pinned map's app names plus the agent
# apps -- as one anchored yabai regex, for the `window_focused` sweep signal's app=
# filter (yabairc). Same escaping as YABAI_AGENT_APPS_RE.
# shellcheck disable=SC2034
YABAI_PINNED_APPS_RE="^($(printf '%s' "$YABAI_PINNED_HOMES" | sed 's/:[^|]*//g')|$(printf '%s' "$YABAI_AGENT_APPS" | sed 's/[()]/\\&/g'))$"

# (Re)register the app -> space pinning rules from the map above, one LABELED rule per
# app (`pin_<app>`, `pin_agent`). Labels matter because yabai resolves `space=<label>`
# to a space ID when the rule is ADDED (message.c, parse_space_selector -> effects.sid),
# not on each apply: when a labeled space is destroyed and yabai_workspace_refresh.sh
# re-creates the label on another space, an unlabeled rule kept pointing at the dead
# id and that app silently stopped pinning until a yabai restart. rule_add() replaces
# a same-label rule, so refresh re-binds everything by calling this again. A rule
# whose label is missing right now fails to parse and leaves the old one in place.
# Called by yabairc (startup) and yabai_workspace_refresh.sh (after any relabel).
yabai_pin_rules_add() {
  local pair app label manage
  local IFS='|'
  for pair in $YABAI_PINNED_HOMES; do
    app=${pair%%:*}; label=${pair#*:}
    case "|$YABAI_PINNED_MANAGED|" in *"|$app|"*) manage="manage=on" ;; *) manage="" ;; esac
    # $manage unquoted on purpose: it is one word or nothing (IFS is `|` here).
    # shellcheck disable=SC2086
    yabai -m rule --add label="pin_$(printf '%s' "$app" | tr -cd 'A-Za-z0-9')" \
      app="^${app}\$" space="$label" $manage >/dev/null 2>&1
  done
  yabai -m rule --add label=pin_agent app="$YABAI_AGENT_APPS_RE" \
    space="${YABAI_AGENT_LABEL:-agent}" >/dev/null 2>&1
  return 0
}

# --- shared state + logs ---------------------------------------------------------
# One FIXED directory for every lock, marker and memo the yabai_* scripts share.
# NOT `${TMPDIR:-/tmp}`: the scripts are launched from three different parents
# with three different environments -- yabai's signal actions and yabairc inherit
# yabai's TMPDIR (/var/folders/.../T/), skhd's keybind actions have NO TMPDIR
# (verified 2026-08-29: `ps -Eww -p $(pgrep -x skhd)`), and a Raycast/terminal run
# has the user's. A marker written under one and read under another is simply never
# seen, and a lock that lives in two places single-flights nothing.
: "${YABAI_STATE_DIR:=$HOME/Library/Caches/yabai}"
: "${YABAI_LOG_DIR:=$HOME/Library/Logs/yabai}"

# yabai_log <name> <message>: append one timestamped line to $YABAI_LOG_DIR/<name>.log.
# Never fails the caller (a signal handler must not die on a full disk). Pair with
# yabai_log_trim from a rare path (startup) to keep a chatty log bounded.
yabai_log() {
  local f="$YABAI_LOG_DIR/$1.log"
  mkdir -p "$YABAI_LOG_DIR" 2>/dev/null || return 0
  printf '%s pid=%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$$" "$2" >>"$f" 2>/dev/null || true
}
yabai_log_trim() {
  local f="$YABAI_LOG_DIR/$1.log" size
  [ -f "$f" ] || return 0
  size=$(stat -f %z "$f" 2>/dev/null) || return 0
  [ "${size:-0}" -gt 204800 ] || return 0
  tail -n 2000 "$f" >"$f.tmp" 2>/dev/null && mv "$f.tmp" "$f" 2>/dev/null || true
}

# --- single-flight locks ---------------------------------------------------------
# mkdir-based (flock does not exist on this machine), with the holder's pid in
# <dir>/pid so a lock has an OWNER. The old per-script form (`mkdir` + an
# unconditional `trap 'rmdir "$LOCK"' EXIT`) dropped the single-flight after a
# takeover: run A stalls past the stale age, run B breaks and re-takes the lock, A
# finally exits and its trap removes B's lock, and a third run starts alongside B.
#
#   yabai_lock_acquire <dir> [stale_seconds=30]   0 = this process now owns <dir>
#   yabai_lock_release <dir>                      removes <dir> only if we still own it
#
# Never blocks and never sleeps on the uncontended path (one mkdir). A held lock is
# broken when it is STALE: its recorded owner is no longer running (a yabai restart
# kills every handler; no need to sit out the age), or it is older than
# stale_seconds by mtime (`stat -f %m`, BSD). Breaking and releasing both happen
# under a second, momentary mkdir mutex (<dir>.break) and re-check under it, so two
# runs that judged the same lock stale cannot both take it (the loser would otherwise
# delete the winner's fresh lock), and a stalled owner's late release cannot land
# between a breaker's remove and re-create.

# The momentary mutex guarding break/release of lock $1. Held for a few syscalls, so
# one older than 10s was orphaned by a kill mid-break and is cleared.
_yabai_lock_mutex() {
  local m="$1.break" mtime
  mkdir "$m" 2>/dev/null && return 0
  mtime=$(stat -f %m "$m" 2>/dev/null) || return 1
  [ "$(( $(date +%s) - mtime ))" -ge 10 ] || return 1
  rmdir "$m" 2>/dev/null
  mkdir "$m" 2>/dev/null
}

# True if lock $1 (an existing dir) may be broken: dead owner, or older than $2 seconds.
# A lock with no pid yet (its owner is between mkdir and the write) is judged by age only.
_yabai_lock_stale() {
  local owner="" mtime
  { read -r owner <"$1/pid"; } 2>/dev/null
  case "$owner" in
    ''|*[!0-9]*) ;;
    *) kill -0 "$owner" 2>/dev/null || return 0 ;;
  esac
  mtime=$(stat -f %m "$1" 2>/dev/null) || return 1
  [ "$(( $(date +%s) - mtime ))" -ge "$2" ]
}

yabai_lock_acquire() {
  local dir="$1" stale="${2:-30}" got=1
  if mkdir "$dir" 2>/dev/null; then
    echo "$$" >"$dir/pid" 2>/dev/null
    return 0
  fi
  if [ ! -d "$dir" ]; then
    # Not contention: the parent is missing (first run after a cache wipe).
    mkdir -p "${dir%/*}" 2>/dev/null
    mkdir "$dir" 2>/dev/null || return 1
    echo "$$" >"$dir/pid" 2>/dev/null
    return 0
  fi
  _yabai_lock_stale "$dir" "$stale" || return 1
  _yabai_lock_mutex "$dir" || return 1
  # Re-check under the mutex: another breaker may have just replaced the stale lock
  # with its own fresh one, which must survive.
  if [ ! -d "$dir" ] || _yabai_lock_stale "$dir" "$stale"; then
    rm -rf "$dir" 2>/dev/null
    if mkdir "$dir" 2>/dev/null; then
      echo "$$" >"$dir/pid" 2>/dev/null
      got=0
    fi
  fi
  rmdir "$dir.break" 2>/dev/null
  return "$got"
}

# Safe to call unconditionally (an EXIT trap, a run that never got the lock): it only
# removes a lock whose pid file names this process. If the mutex stays busy -- someone
# is breaking our lock right now, so we are stale -- the lock is left for them (or for
# the dead-owner check once we exit). Always returns 0.
yabai_lock_release() {
  local dir="$1" owner="" _try
  { read -r owner <"$dir/pid"; } 2>/dev/null
  [ "$owner" = "$$" ] || return 0
  for _try in 1 2 3 4 5; do
    if _yabai_lock_mutex "$dir"; then
      owner=""
      { read -r owner <"$dir/pid"; } 2>/dev/null
      [ "$owner" = "$$" ] && rm -rf "$dir" 2>/dev/null
      rmdir "$dir.break" 2>/dev/null
      return 0
    fi
    sleep 0.02
  done
  return 0
}

# True if lock $1 is held by a run that can still be working: the dir exists AND its
# recorded owner is running, or (no pid recorded -- the owner is between mkdir and
# the write, or it is a lock of the older pid-less form) it is younger than
# $2 (default 30) seconds. For READERS that only ask "is X in progress?": a bare
# `[ -d "$lock" ]` trusted a lock whose holder a yabai restart had killed, until
# something else reaped it. No forks when the lock is absent or has a pid.
yabai_lock_live() {
  local owner="" mtime
  [ -d "$1" ] || return 1
  { read -r owner <"$1/pid"; } 2>/dev/null
  case "$owner" in
    ''|*[!0-9]*)
      mtime=$(stat -f %m "$1" 2>/dev/null) || return 1
      [ "$(( $(date +%s) - mtime ))" -lt "${2:-30}" ] ;;
    *) kill -0 "$owner" 2>/dev/null ;;
  esac
}

# Take lock $1, waiting up to ~$2 tenths of a second (default 50) for a live holder
# to finish. 0 = we own it; 1 = still busy after the wait, and the caller decides
# (the callers here proceed without it rather than hang). No sleep when it is free.
yabai_lock_acquire_wait() {
  local _waited=0
  while :; do
    yabai_lock_acquire "$1" 30 && return 0
    [ "$_waited" -ge "${2:-50}" ] && return 1
    sleep 0.1
    _waited=$((_waited + 1))
  done
}

# Sweep husk spaces: destroy the surplus empty, UNLABELED, NON-fullscreen spaces,
# keeping exactly ONE empty per display (the SINGLE copy of this policy; it used to
# live only on yabai_terminal_follow.sh's slow path). Fullscreen enter/exit leaves the
# vacated Space behind as an empty desktop, and a refresh that raced or was lied to
# by `query --spaces` leaks spares; over time these pile up. One empty per display is
# kept on purpose: it makes the sweep idempotent (macOS recreates a space on
# fullscreen-exit if none is suitable), it is the laptop's spare after the labels and
# the external's reserved scratch. group_by(.display) so each display keeps its own.
# The kept one is the display's VISIBLE empty if it has one (never destroy the space
# the user is looking at); else, on the master, the highest-indexed (the spare after
# the labels), and on any other display the LOWEST-indexed: that is the external's
# first-position scratch, the slot macOS destroys on disconnect. Keeping a later husk
# instead left a labeled space first on the external, with nothing re-running
# yabai_reorder_spaces.sh afterwards. Which display is the master therefore matters,
# so if it cannot be resolved nothing is swept. Never touches a labeled space, a
# space with windows, a visible/focused space or a native-fullscreen Space; a no-op
# when every display has <=1 empty, and when the bulk query answers a bare "[".
# Indices are destroyed high-to-low so yabai's index compaction on --destroy can't
# stale a later target, and each target is re-read by index right before it goes and
# skipped unless it is still the same space id and still qualifies (another script
# may have labeled it, or put a window on it, since the snapshot). Needs the SA.
#
# A space some script has just created and not yet labeled looks exactly like a husk.
# The creators that matter take refresh.lock around create -> label (the refresh
# itself, yabai_ensure_ext), and the heal-path sweep runs under that same lock.
#
# The selection, as a jq program over the whole spaces array with $m = master display
# index, emitting "index:id" per space to destroy, highest index first.
# shellcheck disable=SC2016  # $m is jq's, bound with --argjson
YABAI_JQ_SWEEP_SURPLUS='
  [ .[] | select((.label // "") == "" and (.windows | length == 0)
                 and (.["is-native-fullscreen"] == false)) ]
  | group_by(.display)
  | map((if .[0].display == $m
         then sort_by([(.["is-visible"] == true), .index]) | reverse
         else sort_by([(.["is-visible"] != true), .index]) end)
        | .[1:]
        | map(select(.["is-visible"] == false and .["has-focus"] == false)))
  | flatten | sort_by(.index) | reverse | .[] | "\(.index):\(.id)"'

yabai_sweep_empty_spaces() {
  local surplus sidx sid master
  master=$(yabai_master_index)
  case "$master" in ''|*[!0-9]*) return 0 ;; esac
  surplus=$(yabai -m query --spaces 2>/dev/null | jq -r --argjson m "$master" "$YABAI_JQ_SWEEP_SURPLUS" 2>/dev/null)
  for sidx in $surplus; do
    sid=${sidx#*:}; sidx=${sidx%%:*}
    case "$sidx$sid" in ''|*[!0-9]*) continue ;; esac
    yabai -m query --spaces --space "$sidx" 2>/dev/null | jq -e --argjson id "$sid" '
      .id == $id and (.label // "") == "" and (.windows | length == 0)
      and .["is-native-fullscreen"] == false
      and .["is-visible"] == false and .["has-focus"] == false' >/dev/null 2>&1 || continue
    yabai -m space "$sidx" --destroy >/dev/null 2>&1 || true
  done
  return 0
}

# A pinned window an automated un-float may touch -- the SINGLE copy of that filter
# (yabai_startup_reconcile.sh, both its startup poll and its float sweep). yabai
# floats plenty of windows ON PURPOSE and every one of them must be left alone: a
# pinned app's non-standard second window (settings sheet, save panel, palette), a
# sticky window, a scratchpad window, a minimized/hidden one (also can-move=false,
# so an un-float would be dropped anyway), a native-fullscreen window, and a window
# that cannot be resized (yabai floats `!can_resize && undersized` deliberately,
# window_manager.c:1510). Deliberately NOT filtered on `can-move`: a window yabai
# cannot move right now is the transient case the repair exists for.
# shellcheck disable=SC2034
YABAI_JQ_PIN_ELIGIBLE='
  select(.subrole == "AXStandardWindow")
  | select(."root-window")
  | select(."can-resize" != false)
  | select(."is-minimized" == false and ."is-hidden" == false)
  | select(."is-sticky" == false and ."is-native-fullscreen" == false)
  | select((.scratchpad // "") == "")'

# A pinned window yabai can SEE but cannot ACT ON -- the SINGLE copy of that filter
# (yabai_pin_resolve.sh, and the float sweep's trigger in yabai_startup_reconcile.sh).
# yabai resolves an app's windows on other spaces by brute-forcing AX element ids
# (window_manager_add_existing_application_windows, refresh_index -1) ONLY at yabai
# start (window_manager_begin). Two ways a window misses it:
#   - login: the window exists but is not AX-ready when yabai starts -- the brute force
#     fails and the app goes on applications_to_refresh, whose retries (space change /
#     app activation) only read kAXWindows, i.e. ACTIVE-space windows;
#   - app launch (Claude's stealth update relaunch): the launch handler reads ONLY
#     kAXWindows (window_manager_add_application_windows) and never queues a retry, so
#     a window that comes up on an inactive space is never tracked (upstream #2833).
# Either way `query --windows` still lists it (from SkyLight, has-ax-reference:false,
# empty subrole, can-move:false) but every `window` command answers "could not locate
# the window to act on!", so neither `rule --apply` nor the float sweep can home it,
# and it sits on a space the user is not looking at. Only a yabai restart re-runs the
# brute force. Seen 2026-10-05 and 2026-10-06 (Claude stranded on `agent`).
# Takes the WHOLE windows array (not one window) and emits the stuck windows. Only an
# app with NO resolved standard window qualifies: Electron apps (Claude Desktop ships
# two) can publish hidden helper windows that may be unresolvable for good, and a
# restart over one of those -- next to a perfectly healthy main window -- would buy
# nothing. Compared by PID, not app name: an unresolved window reports the PROCESS
# name (window_nonax_serialize -> proc_name), a resolved one the app name, and those
# differ for WezTerm (`wezterm-gui` vs `WezTerm`). The size floor keeps tiny/offscreen
# helpers out too.
# shellcheck disable=SC2034
YABAI_JQ_PIN_UNRESOLVED='
  ([ .[] | select(."has-ax-reference" and .subrole == "AXStandardWindow") | .pid ] | unique) as $resolved_pids
  | .[]
  | select(."has-ax-reference" == false and ."root-window")
  | select(.frame.w >= 300 and .frame.h >= 200)
  | select(.pid as $p | ($resolved_pids | any(. == $p)) | not)'

# The same map as a JSON object, with the coding-agent apps folded in, for the jq
# consumers. Emits nothing on failure so callers can test for an empty result.
yabai_home_map_json() {
  jq -n --arg pinned "$YABAI_PINNED_HOMES" \
        --arg agents "$YABAI_AGENT_APPS" --arg agentlabel "$YABAI_AGENT_LABEL" '
    ($pinned | split("|") | map(split(":") | { (.[0]): .[1] }) | add) as $pin
    | ($agents | split("|") | map({ (.): $agentlabel }) | add) as $agent_home
    | $pin + $agent_home' 2>/dev/null
}

# `yabai -m query --spaces` intermittently returns a bare "[" -- observed 2026-08-20
# and again 2026-08-21, persisting for minutes -- while `--windows` and the INDEXED
# form `--spaces --space <selector>` keep working throughout. A caller that polls on
# the bulk form silently degrades to doing nothing until its time cap, so: retry
# briefly, then rebuild the array one canonical label at a time.
#
# The fallback covers LABELED spaces only, which is all any consumer here wants (an
# unlabeled space is one this workspace model does not manage). Emits nothing if even
# that fails, so callers can test for an empty result.
yabai_spaces_json() {
  local out label one parts="" _try
  for _try in 1 2 3; do
    out=$(yabai -m query --spaces 2>/dev/null)
    if printf '%s' "$out" | jq -e 'type == "array" and length > 0' >/dev/null 2>&1; then
      printf '%s' "$out"
      return 0
    fi
    sleep 0.2
  done

  for label in $YABAI_LABELS; do
    one=$(yabai -m query --spaces --space "$label" 2>/dev/null) || continue
    printf '%s' "$one" | jq -e 'type == "object" and has("index")' >/dev/null 2>&1 || continue
    parts="$parts$one,"
  done
  [ -n "$parts" ] || return 1
  printf '[%s]' "${parts%,}" | jq -c '.' 2>/dev/null
}

# True if $1 is one of the coding-agent apps. Substring match on a `|`-delimited
# list (no eval, no regex) so app names with spaces/parens need no quoting care.
yabai_is_agent_app() {
  case "|$YABAI_AGENT_APPS|" in
    *"|$1|"*) return 0 ;;
  esac
  return 1
}

# Resolve the master (laptop) display index from a displays-JSON blob passed as $1
# (or from a live `yabai -m query --displays` if $1 is empty/omitted): UUID match
# first, smallest-frame-area fallback. Emits the index, or nothing if it can't be
# resolved -- callers add their own cache/default fallback where they need one, and
# choose live-vs-cache ordering by what they pass in.
#
# If EVERY attached display is an ignored one (lid closed with only the BetterDisplay
# virtual screen left), the ignored display is the only one there is and every space
# lives on it, so it is the master: resolving nothing there left the cache without a
# MASTER_DISPLAY_INDEX and yabai_load_cache failing for every consumer.
yabai_master_index() {
  local djson="${1:-}" real
  [ -n "$djson" ] || djson=$(yabai -m query --displays 2>/dev/null)
  real=$(printf '%s' "$djson" | yabai_filter_displays)
  [ "$real" = "[]" ] && real=$djson
  printf '%s' "$real" | jq -r --arg uuid "$YABAI_MASTER_DISPLAY_UUID" '
    ([.[] | select(.uuid == $uuid) | .index][0]) // (min_by(.frame.w * .frame.h).index) // empty
  ' 2>/dev/null | head -n 1
}

# Source + validate the display-topology cache. Reads $CACHE_FILE (set by the
# caller) and sets DISPLAY_COUNT / MASTER_DISPLAY_INDEX / EXTERNAL_DISPLAY_INDEX as
# globals. Returns 0 if the cache is usable, 1 otherwise. Strict form: a multi-
# display cache must carry BOTH the master and external indices (so a consumer that
# relies on EXTERNAL never proceeds on a half-written cache; the single-display fast
# path needs only MASTER).
yabai_load_cache() {
  if [ -r "${CACHE_FILE:-}" ]; then
    # shellcheck disable=SC1090
    . "$CACHE_FILE"
  fi

  case "${DISPLAY_COUNT:-}" in
    ''|*[!0-9]*) return 1 ;;
  esac

  if [ "$DISPLAY_COUNT" -le 1 ] && [ -n "${MASTER_DISPLAY_INDEX:-}" ]; then
    return 0
  fi

  if [ "$DISPLAY_COUNT" -gt 1 ] && [ -n "${MASTER_DISPLAY_INDEX:-}" ] && [ -n "${EXTERNAL_DISPLAY_INDEX:-}" ]; then
    return 0
  fi

  return 1
}

# Pull any native-fullscreen Space stranded on a non-master display home to master.
# A native-fullscreen app (Preview, a browser, etc.) lives in its OWN, UNLABELED
# macOS Space outside the labeled model, so the label-based pull-home loops miss it
# -- which is why `hyper+0` home-all (and the undock safety net) would otherwise
# leave a fullscreened window behind on the external. Each such Space is moved by
# re-resolving its current index from a stable space id every iteration (indices
# renumber as spaces move). The window stays fullscreen and lands after the labeled
# spaces (reachable via hyper+3-9; yabai_reorder_spaces.sh ignores unlabeled Spaces).
# $1 = master display index. No-op with a single display (nothing is non-master).
yabai_pull_fullscreen_home() {
  local master="$1" ids id idx
  case "$master" in ''|*[!0-9]*) return 0 ;; esac
  ids=$(yabai -m query --spaces 2>/dev/null | jq -r --argjson m "$master" '
    .[] | select(.display != $m and .["is-native-fullscreen"] == true) | .id' 2>/dev/null)
  for id in $ids; do
    case "$id" in ''|*[!0-9]*) continue ;; esac
    idx=$(yabai -m query --spaces 2>/dev/null | jq -r --argjson id "$id" '
      .[] | select(.id == $id) | .index' 2>/dev/null | head -n 1)
    case "$idx" in ''|*[!0-9]*) continue ;; esac
    yabai -m space "$idx" --display "$master" >/dev/null 2>&1 || true
  done
}

# --- ext: the on-demand external "scratch-work" space ---------------------------
# `ext` is a SPECIAL label, NOT one of the canonical 10 in YABAI_LABELS. It is
# created on demand on the EXTERNAL display to hold loose, unpinned windows flung
# there with hyper+fn+g, lives only while docked, and is dissolved back into `main`
# (its windows moved there, the space destroyed) on home-all / push-while-on-ext /
# undock. It always sits at a NON-first position on the external (never the
# disconnect-fragile scratch space).
YABAI_EXT_TARGET="main"   # where dissolved ext windows land on the laptop

# Emit the current `ext` space index, or nothing if it doesn't exist.
yabai_ext_index() {
  yabai -m query --spaces --space ext 2>/dev/null | jq -r '.index // empty' 2>/dev/null | head -n 1
}

# Every space id as a compact JSON array, or nothing unless the bulk query answered a
# non-empty array of numeric ids (it intermittently answers a bare "[").
yabai_space_ids_json() {
  yabai -m query --spaces 2>/dev/null | jq -c '
    if type == "array" and length > 0 and all(.[]; (.id | type) == "number")
    then [.[].id] else empty end' 2>/dev/null
}

# Ensure an `ext` space exists on external display $1 and emit its index. Reuses an
# existing `ext`. Otherwise CREATES a space (it lands on whatever display is active
# -- usually master) and then MOVES it onto the external by its stable id, appending
# after the scratch so `ext` is never the external's first (disconnect-fragile)
# space. We move-by-id rather than focus-then-create because `display --focus`
# races, and a create that lands on the wrong display silently mislabels the scratch.
# Needs the SA (space create/move/label).
#
# The create -> move -> label stretch runs under refresh.lock. Until the label lands
# the new space is empty and unlabeled, i.e. indistinguishable from a husk: a heal
# sweep in flight (yabai_sweep_empty_spaces, which runs under that lock) destroyed it
# -- the keypress then silently did nothing -- or destroyed the laptop's spare in its
# place; and a refresh could hand it a missing canonical label. Same bounded wait as
# the refresh, and like it we go ahead WITHOUT the lock if a live holder outlasts the
# wait: a keypress must not hang. No self-deadlock: the lock is taken after the
# reuse check and dropped before returning, nothing in between calls
# yabai_workspace_refresh.sh, and the one caller (yabai_send_window_external.sh)
# runs its own refresh before this, not around it.
yabai_ensure_ext() {
  local idx got rc
  idx=$(yabai_ext_index)
  case "$idx" in ''|*[!0-9]*) ;; *) printf '%s' "$idx"; return 0 ;; esac
  case "$1" in ''|*[!0-9]*) return 1 ;; esac
  yabai_lock_acquire_wait "$YABAI_STATE_DIR/refresh.lock"; got=$?
  _yabai_ensure_ext_create "$1"; rc=$?
  # Only a lock THIS call took (release would also drop one the caller already held).
  [ "$got" = 0 ] && yabai_lock_release "$YABAI_STATE_DIR/refresh.lock"
  return "$rc"
}

_yabai_ensure_ext_create() {
  local ext="$1" idx before after="" newid d _try _s
  # Again, now that we hold the lock: another fling may have created it while we waited.
  idx=$(yabai_ext_index)
  case "$idx" in ''|*[!0-9]*) ;; *) printf '%s' "$idx"; return 0 ;; esac
  case "$ext" in ''|*[!0-9]*) return 1 ;; esac

  # The new space is found as "the id that was not there before", so BOTH snapshots
  # must be real. `query --spaces` intermittently answers a bare "[" (see
  # yabai_spaces_json): with an empty `before`, every existing space looked new and
  # the first one -- terminal's -- was moved to the external and relabelled `ext`.
  # No valid `before` -> create nothing.
  before=$(yabai_space_ids_json)
  [ -n "$before" ] || return 1
  yabai -m space --create >/dev/null 2>&1 || true
  # The create has already happened, so give the after-query a few tries before
  # giving up (and leaving the new space unlabeled) on a transient bad answer.
  for _try in 1 2 3 4 5; do
    after=$(yabai_space_ids_json)
    [ -n "$after" ] && break
    sleep 0.2
  done
  [ -n "$after" ] || return 1
  newid=$(jq -n -r --argjson before "$before" --argjson after "$after" '
    ($after - $before)[0] // empty' 2>/dev/null)
  case "$newid" in ''|*[!0-9]*) return 1 ;; esac

  d=$(yabai -m query --spaces 2>/dev/null | jq -r --argjson id "$newid" '
    .[] | select(.id == $id) | .display' 2>/dev/null | head -n 1)
  if [ "$d" != "$ext" ]; then
    idx=$(yabai -m query --spaces 2>/dev/null | jq -r --argjson id "$newid" '
      .[] | select(.id == $id) | .index' 2>/dev/null | head -n 1)
    case "$idx" in ''|*[!0-9]*) ;; *) yabai -m space "$idx" --display "$ext" >/dev/null 2>&1 || true ;; esac
    # Wait for the cross-display space move to actually land before we label/use it
    # -- yabai is busy reindexing right after, and a too-soon follow-up move gets
    # silently dropped.
    for _s in 1 2 3 4 5 6; do
      d=$(yabai -m query --spaces 2>/dev/null | jq -r --argjson id "$newid" '
        .[] | select(.id == $id) | .display' 2>/dev/null | head -n 1)
      [ "$d" = "$ext" ] && break
      sleep 0.1
    done
  fi
  idx=$(yabai -m query --spaces 2>/dev/null | jq -r --argjson id "$newid" '
    .[] | select(.id == $id) | .index' 2>/dev/null | head -n 1)
  case "$idx" in ''|*[!0-9]*) return 1 ;; esac
  yabai -m space "$idx" --label ext >/dev/null 2>&1 || true
  printf '%s' "$idx"
}

# Dissolve `ext`: move all its windows to YABAI_EXT_TARGET (main) and destroy the
# now-empty space. No-op if `ext` doesn't exist (also cleans up an empty `ext`). If
# `ext` happens to be its display's LAST space (can't be destroyed), the label is
# dropped instead so no empty `ext` husk lingers. Does NOT change focus.
yabai_dissolve_ext() {
  local idx wids wid
  idx=$(yabai_ext_index)
  case "$idx" in ''|*[!0-9]*) return 0 ;; esac
  wids=$(yabai -m query --windows --space ext 2>/dev/null | jq -r '.[].id' 2>/dev/null)
  for wid in $wids; do
    case "$wid" in ''|*[!0-9]*) continue ;; esac
    yabai -m window "$wid" --space "$YABAI_EXT_TARGET" >/dev/null 2>&1 || true
  done
  idx=$(yabai_ext_index)
  case "$idx" in ''|*[!0-9]*) return 0 ;; esac
  yabai -m space "$idx" --destroy >/dev/null 2>&1 || true
  if [ -n "$(yabai_ext_index)" ]; then
    yabai -m space ext --label >/dev/null 2>&1 || true
  fi
}
