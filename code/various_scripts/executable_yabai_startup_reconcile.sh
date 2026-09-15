#!/bin/bash

# Startup reconciliation -- fixes pinned apps landing on the WRONG space, or landing
# FLOATING, after login or a yabai restart -- plus a deferred FLOAT SWEEP that keeps
# retrying the un-float, event-driven, for as long as a pinned window stays floating.
#
#   yabai_startup_reconcile.sh [startup]   # poll from yabairc (backgrounded)
#   yabai_startup_reconcile.sh float       # one un-float pass, from signals
#   yabai_startup_reconcile.sh float --dry-run
#
# 1. WRONG SPACE. At login, macOS restores app windows around the same time yabai
# starts. Windows created before yabai registered its signals get no window_created/
# application_launched event, and yabairc's one-shot `rule --apply` can run BEFORE
# those windows exist -- or before `sudo yabai --load-sa` finishes (window->space
# moves need the scripting addition). Result: pinned apps (WezTerm, Todoist, Granola,
# Spark Mail, Notion Calendar, Messages, ChatGPT, Claude, the coding-agent apps; Arc
# via Hammerspoon) sit on the wrong space until a manual `yabai --restart-service`.
#
# 2. FLOATING. yabai classifies every window it finds at startup BEFORE the config
# runs (main.c: window_manager_begin() precedes exec_config_file()), so no rule can
# influence that pass. A window it momentarily cannot move is flagged FLOAT for good
# (window_manager.c:1505-1511: `sticky || !can_move || !is_standard || !level_is_
# standard || (!can_resize && undersized)` -> WINDOW_FLOAT) -- i.e. any window whose
# AX position attribute is not settable at the instant yabai looks, which a window
# still settling after a restore (or an Electron app whose AX is slow after wake) can
# be. Nothing clears the flag afterwards: `rule --apply` re-pins the SPACE but leaves
# the window floating, so it sits unmanaged on that space indefinitely. Observed
# 2026-08-21 and again 2026-08-28 (Claude floating on `ai` for 34h) while ChatGPT on
# the same space tiled normally. `manage=on` on the space= rules does NOT fix this --
# see the comment on unfloat_pins() for why it makes things worse.
#
# WHY A STARTUP POLL ALONE CANNOT FIX (2): yabai CACHES can-move/role/subrole at
# window creation and refreshes them only on minimize/deminimize/native-fullscreen
# transitions (event_loop.c). `--toggle float` (force=false) is silently dropped --
# exit 0, no error -- while the CACHED can_move is false (window_manager.c:2185-2192).
# So ten retries over 20s against a cache that never refreshes are ten identical
# drops, and the 2026-08-28 incident was exactly that: 11 passes (MAX_ATTEMPTS + 1,
# counted from the arcSync launches in `log show`), gave up, and nothing on the
# machine ever tried again. The startup poll therefore HANDS OFF to the float sweep:
#
# 3. FLOAT SWEEP (`float` mode). yabairc runs this from `space_changed`,
# `window_deminimized` and `window_focused` (pinned apps only). It does ONE thing --
# unfloat_pins() over the current state -- with no `rule --apply`, no arcSync, no
# sudo, and exits in one query + one jq when nothing pinned is floating (the common
# path). Landing on the window's home space is the no-flip direct route; a
# deminimize is the one event that refreshes the cached flags. It keeps retrying,
# bounded by a per-window backoff memo, for as long as the window floats, and LOGS
# every attempt with the window's pre/post state so the next incident can be
# explained (this one could not be: there was no log).
#
# 4. THE RECURRING CASE: CLAUDE DESKTOP'S STEALTH UPDATE (found 2026-09-15). Every
# auto-update ends in a "[stealth-relaunch]" (main.log): ShipIt relaunches the app,
# which brings its window back BEHIND everything so the update goes unnoticed --
# `setAlwaysOnTop(true, "normal", -1)` + `showInactive()` in app.asar, i.e. CG window
# level -1 -- and restores level 0 only on the window's first `focus` event. yabai
# refuses to manage any window whose level is not 0, so the relaunched window is
# classified FLOAT at creation and every sweep pass is a no-op (reconcile.log shows
# `lvl:-1` on each drop) until the user clicks it. Not the can-move cache: `cm` is
# true throughout. That focus is the level-restoring event, so `window_focused` is a
# sweep trigger too, and one that bypasses the backoff memo like a deminimize does.
# Seen on 10 updates between 2026-08-25 and 2026-09-14 -- roughly every release.
#
# STARTUP mode POLLS UNTIL STABLE: re-load the scripting addition once, then
# repeatedly re-apply the space= rules, re-pin Arc, and un-float any misclassified
# pinned window until every RUNNING pinned app is home AND tiled (or a hard time cap).
# Polling self-truncates on a fast login (exits in a couple of seconds) and
# self-extends for slow-launching apps (Electron: ChatGPT, Notion Calendar, Messages).
#
# Run BACKGROUNDED from yabairc (`"$YABAI_STARTUP_RECONCILE" &`) so it NEVER blocks
# yabai startup. Single-flighted per mode (mkdir locks under $YABAI_STATE_DIR) so
# repeated restarts don't stack overlapping polls and a sweep never overlaps a live
# poll. Every action is idempotent.
#
# Log: ~/Library/Logs/yabai/reconcile.log. State: ~/Library/Caches/yabai/.

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"
HS="/opt/homebrew/bin/hs"

MODE="${1:-startup}"
DRY_RUN=0
[ "${2:-}" = "--dry-run" ] && DRY_RUN=1
case "$MODE" in startup|float) ;; *) echo "usage: $0 [startup|float [--dry-run]]" >&2; exit 64 ;; esac

CAP_SECONDS="${YABAI_RECONCILE_CAP:-90}"   # hard stop so a never-launching app can't poll forever

# Required siblings, resolved by directory rather than $HOME so a chezmoi checkout or
# a test copy runs against its own copies: yabai_common.sh (the agent app list, the
# pinned-home map, the eligibility filter, the state/log dirs) and
# yabai_float_borders.sh (called after an un-float).
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/yabai_common.sh"

LOGN=reconcile
STARTUP_LOCK="$YABAI_STATE_DIR/startup_reconcile.lock"
SWEEP_LOCK="$YABAI_STATE_DIR/float_sweep.lock"
MEMO="$YABAI_STATE_DIR/float_memo"            # lines: <window id> <consecutive fails> <epoch of last fail>
KEEP_FLOAT="$YABAI_STATE_DIR/keep-float"      # one file per window id the user floated on purpose
mkdir -p "$YABAI_STATE_DIR" 2>/dev/null

# The running yabai's pid. `pgrep` can come back EMPTY in the first second of a
# restart (seen 2026-08-29: `yabai_pid=` in the startup log line), and when this
# script is launched from yabairc its grandparent IS yabai (yabai forks the config
# shell), so fall back to that. Emits nothing only if both fail.
yabai_pid_now() {
  local p
  p=$(pgrep -x yabai 2>/dev/null | head -n 1)
  if [ -z "$p" ]; then
    p=$(ps -o ppid= -p "$PPID" 2>/dev/null | tr -d ' ')
    [ "$(ps -o comm= -p "${p:-0}" 2>/dev/null)" = "/opt/homebrew/bin/yabai" ] || p=""
  fi
  printf '%s' "$p"
}

# Seconds since a lock was last touched. A live startup poll touches $LOCK/alive
# every pass, so "old" here means "orphaned" and never "busy".
lock_age() {
  local now m
  now=$(date +%s)
  m=$(stat -f %m "$1/alive" 2>/dev/null || stat -f %m "$1" 2>/dev/null || printf '%s' "$now")
  printf '%s' "$((now - m))"
}

if [ "$MODE" = float ]; then
  # Never two writers. Defer to a LIVE startup poll only; an orphan (holder gone
  # without its EXIT trap -- SIGKILL on a fast double restart) used to disable this
  # sweep for the whole login session, silently, so it is reaped by age instead.
  if [ -d "$STARTUP_LOCK" ]; then
    [ "$(lock_age "$STARTUP_LOCK")" -lt 30 ] && exit 0
    rm -rf "$STARTUP_LOCK" 2>/dev/null
    yabai_log $LOGN "float reaped-orphan-startup-lock"
  fi
  if ! mkdir "$SWEEP_LOCK" 2>/dev/null; then
    [ "$(lock_age "$SWEEP_LOCK")" -ge 30 ] || exit 0
    rm -rf "$SWEEP_LOCK" 2>/dev/null
    mkdir "$SWEEP_LOCK" 2>/dev/null || exit 0
  fi
  LOCK="$SWEEP_LOCK"
else
  yabai_log_trim $LOGN
  # Startup single-flight. A second restart while a poll from the PREVIOUS yabai is
  # still running must NOT be dropped (the old poll is repairing against a daemon
  # that no longer exists -- and "restart it again" is the natural human reaction to
  # "it didn't fix it"). Same yabai + live holder -> let it run; otherwise ask the
  # holder to stop between passes and take over.
  if ! mkdir "$STARTUP_LOCK" 2>/dev/null; then
    hp=$(cat "$STARTUP_LOCK/pid" 2>/dev/null || true)
    hy=$(cat "$STARTUP_LOCK/yabai_pid" 2>/dev/null || true)
    if [ -n "$hp" ] && [ -n "$hy" ] && kill -0 "$hp" 2>/dev/null && [ "$hy" = "$(yabai_pid_now)" ]; then
      exit 0
    fi
    : >"$STARTUP_LOCK/stop" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8; do [ -d "$STARTUP_LOCK" ] || break; sleep 0.25; done
    rm -rf "$STARTUP_LOCK" 2>/dev/null
    mkdir "$STARTUP_LOCK" 2>/dev/null || exit 0
    yabai_log $LOGN "startup preempted-previous-holder pid=${hp:-?} yabai_pid=${hy:-?}"
  fi
  LOCK="$STARTUP_LOCK"
  echo $$ >"$LOCK/pid"
  yabai_pid_now >"$LOCK/yabai_pid"
  # Post-restart float state is never the user's choice (yabai re-classified from
  # scratch), and CGWindowIDs are session-scoped, so stale markers only ever block
  # a repair. Same for the backoff memo.
  rm -rf "$KEEP_FLOAT" "$MEMO" 2>/dev/null
fi
trap 'rm -rf "$LOCK" 2>/dev/null || true' EXIT

# The pinned app -> home space map (JSON) comes from yabai_common.sh, which is where
# this codebase keeps lists that more than one script needs. Computed ONCE at startup,
# not per pass: it is a pure function of two constants, and the poll can run ~45 times.
PIN_HOMES=$(yabai_home_map_json)

# Startup-mode retry budget per window: ~20s at the loop's 2s cadence. Long enough
# for a slow Electron window to become movable, short enough that a permanently
# stuck one does not cost the full cap -- the float sweep takes it from there.
MAX_ATTEMPTS=10

# Float-mode backoff: after this many consecutive dropped un-floats, skip the window
# for BACKOFF_SECONDS unless the triggering event is one that changes what blocked it
# (a deminimize refreshes yabai's cache; a focus is when Claude Desktop's stealth
# relaunch restores its window level, see header §4). Without this a window stuck
# for good would cost a toggle + a query on every space switch, forever.
MAX_FAILS=3
BACKOFF_SECONDS=600

# Events that may bypass the backoff memo (see above).
case "${YABAI_EVENT:-}" in
  window_deminimized|window_focused) BYPASS_BACKOFF=1 ;;
  *)                                 BYPASS_BACKOFF=0 ;;
esac

# Space index -> label, plus the label of the FOCUSED space. Focused, not merely
# visible: yabai re-tiles an un-floated window onto space_manager_active_space(),
# which resolves to the space of the focused window's display -- i.e. the has-focus
# space, the one place an un-float lands correctly with no repair.
JQ_SPACE_MAPS='
  ($s | map({ (.index|tostring): (.label // "") }) | add) as $lbl
  | (($s | map(select(."has-focus")) | first | .label) // "") as $focused'

# Are all RUNNING pinned apps on their home space AND not flagged FLOAT? Arc is handled
# by arcSync and is excluded; windows on an UNLABELED space (e.g. a native-fullscreen
# one) are ignored -- yabai never tiles those, so a floating one is correct.
#
# The float half is NOT re-derived here. unfloat_pins() runs first each pass and leaves
# PENDING_FLOATS = how many windows it still intends to retry; anything it decided to
# skip for good (ineligible, floating on a bsp space it must not rebuild, or past its
# attempt budget -- handed to the float sweep) is deliberately NOT counted. Deriving
# that twice is how the predicate and the repair drift apart, and a window the repair
# will never touch keeping the poll hot -- and `rule --apply` + arcSync firing every 2s
# -- for the whole cap is the expensive kind of drift. One decision point, in
# unfloat_pins; this just reads the tally. GAVE_UP is logged so the exit is honest.
#
# Takes the windows/spaces blobs so the poll queries yabai once per pass rather than
# once per predicate.
pins_settled() {
  local off
  [ -n "${1:-}" ] && [ -n "${2:-}" ] || return 1
  [ "${PENDING_FLOATS:-0}" = "0" ] || return 1
  off=$(jq -n --argjson w "$1" --argjson s "$2" --argjson home "$PIN_HOMES" '
    ($s | map({ (.index|tostring): (.label // "") }) | add) as $lbl
    | [ $w[]
        | select($home[.app] != null)
        # Only windows yabai would ever move. An Electron app publishes hidden helper
        # windows with an EMPTY subrole and can-move=false -- Claude Desktop ships two,
        # parked on whatever space it launched from. `rule --apply` cannot move them, so
        # counting them meant this poll could NEVER settle and burned its whole cap,
        # re-running rule --apply + arcSync every 2s, at every login. (Pre-dates the
        # un-float work; found 2026-08-21 when two such windows appeared mid-test.)
        | select(.subrole == "AXStandardWindow")
        | select(."root-window")
        | { want: $home[.app], have: ($lbl[(.space|tostring)] // "") }
        | select(.have != "" and .have != .want) ]
    | length' 2>/dev/null) || return 1
  [ "${off:-1}" = "0" ]
}

# The fields the next incident will need: which of yabai's float predicates the
# window was failing (cached can-move, subrole, level, has-ax-reference).
pre_state() {
  printf '%s' "$1" | jq -c --argjson id "$2" '
    .[] | select(.id == $id)
    | { app, cm: ."can-move", cr: ."can-resize", sr: .subrole, lvl: .level,
        ax: ."has-ax-reference", f: ."is-floating", sp: .space }' 2>/dev/null
}

# Label, layout and display of the space that is focused RIGHT NOW (tab-separated).
# Re-read immediately before each toggle: the tile target is evaluated inside yabai
# when the message arrives, and a space switch, a transient `space --focus` from
# yabai_reorder_spaces.sh / yabai_heal.sh, or a second display can all change it
# between the pass's snapshot and the toggle.
focused_now() {
  yabai -m query --spaces --space 2>/dev/null \
    | jq -r '"\(.label // "")\t\(.type // "")\t\(.display // "")"' 2>/dev/null
}

memo_get() { awk -v i="$1" '$1 == i { print $2, $3 }' "$MEMO" 2>/dev/null; }
memo_set() {
  { grep -v "^$1 " "$MEMO" 2>/dev/null
    [ "$2" -gt 0 ] && echo "$1 $2 $(date +%s)"; } >"$MEMO.tmp" 2>/dev/null
  mv "$MEMO.tmp" "$MEMO" 2>/dev/null || true
}

# Re-manage pinned windows that yabai misclassified as floating (see failure mode 2
# in the header). `--toggle float` is the only lever, and it comes with a trap:
#
#   yabai re-tiles an un-floated window onto the ACTIVE space, not onto the window's
#   own space -- window_manager_make_window_floating() ends in
#   `space_manager_tile_window_on_space(sm, window, space_manager_active_space())`.
#
# So un-floating a window that lives on a BACKGROUND space silently registers it in
# the active space's view: two windows on different spaces sharing one stack, and the
# window that legitimately owned that view gets pushed out. Verified live 2026-08-21
# (Claude on `ai` un-floated while `terminal` was active -> WezTerm and Claude came
# back as stack-index 1 and 2 of the same view, ChatGPT ejected to 0).
#
# The repair is to REBUILD THE HOME SPACE'S VIEW afterwards -- `--layout bsp` then back
# to `--layout stack` re-derives the tree from actual window->space membership, which
# both re-homes the window and drops the stale registration from the other view.
# Verified live 2026-08-21: with `terminal` active, a corrupted `todo` (Todoist sharing
# WezTerm's view at stack-index 1/2) came back correct -- Todoist alone on `todo`,
# WezTerm alone on `terminal` -- by flipping `todo` ALONE, addressed by label.
#
# Two routes, decided per window from the focus state read immediately before acting:
#   direct  home IS the focused space (same display, no heal in flight): the toggle
#           tiles it correctly by itself; a flip there would only churn the view the
#           user is looking at.
#   flip    home is a background STACK space AND the active space is also a stack:
#           toggle, then rebuild home. The transient registration in the active view
#           is frame-invisible on a stack (every node shares one frame); on a bsp
#           active space it would visibly re-split the user's tiles, so that case is
#           skipped and left for the direct route (yabai_skhd_mode.sh can make a
#           space bsp; every space here is stack by default).
#   skip    home is bsp and not focused: a flip re-derives a bsp tree from scratch,
#           losing hand-tuned splits -- left floating, the benign failure.
#
# The obvious alternative -- bouncing the window through another space and back, so
# send_window_to_space() re-tiles it on the destination -- was implemented first and is
# WRONG here. It needs the scripting addition, which may not be loaded yet at login
# (`window --space` then fails SILENTLY and exit-0, so the failure is undetectable and
# the window is left cross-registered); yabai REFUSES to move a window into a
# native-fullscreen space, which strands WezTerm off `terminal` whenever
# yabai_terminal_follow.sh has moved that label onto a fullscreen Space; the outbound
# leg hands focus to another window when the source space is visible
# (window_manager.c:2099); and it parks the window somewhere
# yabai_workspace_refresh.sh's label-follows-app logic can see and re-label it. The
# flip touches no window and needs no scripting addition.
#
# This is also why `manage=on` on the yabairc space= rules is the wrong fix: it does
# suppress the misclassification for windows created while yabai is already running
# (window_manager.c:1498 skips the FLOAT classification for WINDOW_RULE_MANAGED), but
# it cannot help at startup (rules do not exist yet during window discovery), and it
# makes `rule --apply` -- which yabairc fires at startup and on every
# application_launched -- take the un-float path above from whatever space happens to
# be active. That trades a floating window for a corrupted cross-space stack. And a
# one-off `rule --apply app=^X$ manage=on` as an escalation is worse still: it is
# per-APP, sets WINDOW_RULE_MANAGED before the eligibility check, and force-tiles
# every root window of the app (helpers included) into the ACTIVE view.
#
# Manual escape hatch for a window whose cached flags never refresh (never
# minimized): `yabai -m window <managed sibling id> --stack <stuck id>` -- SIBLING
# FIRST. It clears FLOAT without the can-move gate and joins the sibling's view on the
# home space regardless of the active space (window_manager.c:1804-1816). Not
# automated: it needs a managed sibling on the same space and changes stack order.
unfloat_pins() {
  # shellcheck disable=SC2034  # snap_focused is the pass-time value; routes use focused_now
  local floaters id label layout snap_focused disp attempts changed=0 pending=0
  local fails when now fl ft fd route cur post

  PENDING_FLOATS=0
  [ -n "${1:-}" ] && [ -n "${2:-}" ] || return 0

  # id, home LABEL (never an index -- indices renumber, and yabai_workspace_refresh.sh
  # / yabai_reorder_spaces.sh / yabai_displays.sh can all renumber them mid-poll; the
  # siblings compare by label for exactly this reason), the home space's layout,
  # whether that space is the focused one, and its display. App names are never a
  # positional field here (three pinned apps have spaces in theirs).
  #
  # Float mode only considers a floater already ON its home space: it runs no
  # `rule --apply`, so a pinned window floating elsewhere is either the user's own
  # (hyper+t is only allowed off home) or one the next application_launched will
  # re-home first.
  floaters=$(jq -r -n --argjson w "$1" --argjson s "$2" --argjson home "$PIN_HOMES" \
      --argjson homeonly "$([ "$MODE" = float ] && echo true || echo false)" "
    $JQ_SPACE_MAPS
    | (\$s | map({ (.index|tostring): (.type // \"\") }) | add) as \$type
    | (\$s | map({ (.index|tostring): (.display // 0) }) | add) as \$disp
    | \$w[]
    | select(\$home[.app] != null)
    | select(.\"is-floating\")
    | $YABAI_JQ_PIN_ELIGIBLE
    | { l: (\$lbl[(.space|tostring)] // \"\"), t: (\$type[(.space|tostring)] // \"\"),
        d: (\$disp[(.space|tostring)] // 0) } as \$sp
    | select(\$sp.l != \"\")
    | select((\$homeonly | not) or \$sp.l == \$home[.app])
    | \"\(.id) \(\$sp.l) \(\$sp.t) \(if \$sp.l == \$focused then 1 else 0 end) \(\$sp.d)\"
    " 2>/dev/null) || return 0
  [ -n "$floaters" ] || return 0

  now=$(date +%s)
  while read -r id label layout snap_focused disp; do
    [ -n "$id" ] && [ -n "$label" ] || continue

    # The user's own float (hyper+t on a pinned app off home, later re-homed).
    [ -e "$KEEP_FLOAT/$id" ] && continue

    if [ "$MODE" = startup ]; then
      # Give up on a window that keeps refusing, so one stubborn case cannot keep the
      # poll hot -- and the active space thrashing -- for the whole 90s cap. The float
      # sweep owns it from here; say so once, with the state that explains the drops.
      attempts="ATTEMPT_${id}"
      eval "attempts=\${$attempts:-0}"
      if [ "$attempts" -ge "$MAX_ATTEMPTS" ]; then
        if [ "$attempts" -eq "$MAX_ATTEMPTS" ]; then
          yabai_log $LOGN "startup gave-up id=$id home=$label attempts=$attempts pre=$(pre_state "$1" "$id") -> float sweep takes over"
          eval "ATTEMPT_${id}=$((attempts + 1))"
          GAVE_UP=$((GAVE_UP + 1))
        fi
        continue
      fi
      eval "ATTEMPT_${id}=$((attempts + 1))"
    else
      attempts=0
      read -r fails when <<<"$(memo_get "$id")"
      fails=${fails:-0}; when=${when:-0}
      if [ "$fails" -ge "$MAX_FAILS" ] && [ $((now - when)) -lt "$BACKOFF_SECONDS" ] \
         && [ "$BYPASS_BACKOFF" = 0 ]; then
        continue
      fi
    fi

    # Route, from the focus state as of right now (see focused_now). During a heal
    # (refresh/reorder focusing spaces transiently) never trust "focused": flip.
    IFS=$'\t' read -r fl ft fd <<<"$(focused_now)"
    if [ "$fl" = "$label" ] && [ "$fd" = "$disp" ] && [ ! -d "$YABAI_STATE_DIR/heal.lock" ]; then
      route=direct
    elif [ "$layout" = stack ] && [ "$ft" = stack ]; then
      route=flip
    else
      yabai_log $LOGN "$MODE skip id=$id home=$label reason=$([ "$layout" = stack ] && echo active-bsp || echo home-bsp) focused=$fl"
      continue
    fi

    if [ "$DRY_RUN" = 1 ]; then
      echo "would unfloat id=$id home=$label route=$route pre=$(pre_state "$1" "$id")"
      continue
    fi

    # Act on the window's state as of NOW, not the pass's snapshot: the user may have
    # hyper+t'd it back in the meantime, and a toggle then would FLOAT a tiled window.
    cur=$(yabai -m query --windows --window "$id" 2>/dev/null | jq -r '."is-floating"' 2>/dev/null)
    [ "$cur" = "true" ] || { [ "$cur" = "false" ] && memo_set "$id" 0; continue; }

    yabai -m window "$id" --toggle float >/dev/null 2>&1 || { pending=$((pending + 1)); continue; }
    # An un-float attempted while the cached can-move is false is dropped SILENTLY
    # and exit-0 (the force=false path returns without a daemon_fail), so the exit
    # status proves nothing. Confirm the post-state.
    post=$(yabai -m query --windows --window "$id" 2>/dev/null | jq -r '."is-floating"' 2>/dev/null)
    yabai_log $LOGN "$MODE unfloat id=$id home=$label route=$route attempt=$((attempts + 1)) ev=${YABAI_EVENT:-} pre=$(pre_state "$1" "$id") post_floating=${post:-?}"
    case "$post" in
      false) : ;;
      *)     [ "$MODE" = float ] && memo_set "$id" $((fails + 1))
             pending=$((pending + 1)); continue ;;
    esac
    memo_set "$id" 0
    changed=1

    [ "$route" = direct ] && continue
    # Rebuild the home view. Never die between the two flips (a startup takeover
    # sends nothing, but a stray TERM here would strand the space in bsp).
    trap '' TERM
    if yabai -m space "$label" --layout bsp >/dev/null 2>&1; then
      yabai -m space "$label" --layout stack >/dev/null 2>&1 || yabai_log $LOGN "$MODE flip-FAILED id=$id home=$label (space left bsp!)"
    else
      yabai_log $LOGN "$MODE flip-FAILED id=$id home=$label (bsp step)"
    fi
    trap - TERM
  done <<EOF
$floaters
EOF

  # What pins_settled() reads: windows still floating that this function intends to
  # retry. Skipped-for-good cases were never counted, so they cannot hold the poll open.
  PENDING_FLOATS=$pending

  # An un-float changes the float set but emits no window_created/destroyed -- the same
  # gap yabai_toggle_float.sh covers after hyper+t. Without this the borders daemon
  # keeps the stale whitelist yabairc's startup `sync` gave it and draws a floating-
  # window border around a window that is now tiled. Gated on an actual change: the
  # sync forks a full query + a 0.15s settle, and on a stubborn window the poll would
  # otherwise fire ~45 of them, most of which lose that script's own single-flight lock.
  [ "$changed" = "1" ] || return 0
  "$SCRIPT_DIR/yabai_float_borders.sh" sync >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------------
if [ "$MODE" = float ]; then
  # A focus signal fires before the app's own focus handler has run; Claude Desktop
  # restores its window level (header §4) in that handler, asynchronously, so give it
  # a beat or the query below still sees level -1 and the toggle is dropped again.
  # Only on this event -- the sweep must stay ~40 ms on a space switch.
  [ "${YABAI_EVENT:-}" = window_focused ] && sleep 0.3
  # Common path: nothing pinned and eligible is floating -> one query, one jq, exit.
  win=$(yabai -m query --windows 2>/dev/null) || exit 0
  printf '%s' "$win" | jq -e --argjson home "$PIN_HOMES" "
      any(.[]; .\"is-floating\" and \$home[.app] != null and (($YABAI_JQ_PIN_ELIGIBLE) | true))
    " >/dev/null 2>&1 || exit 0
  spaces=$(yabai_spaces_json) || { yabai_log $LOGN "float spaces-query-failed ev=${YABAI_EVENT:-}"; exit 0; }
  GAVE_UP=0
  unfloat_pins "$win" "$spaces"
  exit 0
fi

# Ensure the scripting addition is loaded (window->space moves need it). `-n` so a
# stale NOPASSWD hash fast-fails instead of ever waiting on a prompt in this
# TTY-less context.
if sudo -n yabai --load-sa >/dev/null 2>&1; then
  yabai_log $LOGN "startup begin sa=ok yabai_pid=$(cat "$LOCK/yabai_pid" 2>/dev/null)"
else
  yabai_log $LOGN "startup begin sa=FAIL yabai_pid=$(cat "$LOCK/yabai_pid" 2>/dev/null)"
fi

# Re-assert pins until everything restored has landed home and tiled, or we hit the
# cap. unfloat_pins runs AFTER `rule --apply` each pass so a window is already on its
# home space before it is un-floated -- the space= move re-tiles on the destination
# by itself, leaving only genuine misclassifications for unfloat_pins to repair.
t0=$(date +%s)
deadline=$((t0 + CAP_SECONDS))
passes=0
GAVE_UP=0
reason=cap
while :; do
  touch "$LOCK/alive" 2>/dev/null
  [ -e "$LOCK/stop" ] && { reason=preempted; break; }
  passes=$((passes + 1))

  yabai -m rule --apply >/dev/null 2>&1 || true
  "$HS" -c "arcSync()" >/dev/null 2>&1 || true

  # One pair of queries per pass, shared by both predicates below (they used to query
  # yabai twice each -- 4 round trips a pass, ~45 passes at the cap).
  win=$(yabai -m query --windows 2>/dev/null) || win=""
  # Not a bare `yabai -m query --spaces`: that call intermittently returns "[" for
  # minutes at a time, and without the fallback both predicates below go blind and the
  # poll burns its whole cap doing nothing (seen exactly that way while testing this).
  spaces=$(yabai_spaces_json) || spaces=""

  unfloat_pins "$win" "$spaces"
  # unfloat_pins mutates what pins_settled reads, so re-read rather than reuse the
  # blobs above -- otherwise a pass that just healed everything still reports unsettled
  # and the poll always costs one extra 2s sleep.
  pins_settled "$(yabai -m query --windows 2>/dev/null)" "$spaces" && { reason=settled; break; }
  [ "$(date +%s)" -ge "$deadline" ] && break
  sleep 2
done
yabai_log $LOGN "startup end passes=$passes elapsed=$(( $(date +%s) - t0 ))s reason=$reason pending=${PENDING_FLOATS:-0} gave_up=$GAVE_UP"
