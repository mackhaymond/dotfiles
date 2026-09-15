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
YABAI_AGENT_APPS="Conductor|Claudia|OpenChamber|OpenCode|Jean|T3 Code (Alpha)"

# The same list as a yabai `rule --add app=` regex: anchored, with the literal
# parens of names like "T3 Code (Alpha)" escaped so they aren't read as a group.
# shellcheck disable=SC2034
YABAI_AGENT_APPS_RE="^($(printf '%s' "$YABAI_AGENT_APPS" | sed 's/[()]/\\&/g'))$"

# --- pinned apps: app -> home space label ---------------------------------------
# The single SHELL-side source of the pinned-app home map, in the same `|`-delimited
# no-eval-no-regex shape as YABAI_AGENT_APPS (so names with spaces need no quoting
# care). yabairc's `space=` rules are the one unavoidable mirror -- yabai cannot read
# a shell map -- and Arc is deliberately absent here: its two main windows are pinned
# by Hammerspoon's arcSync(), not by a `space=` rule.
#
# Consumed by: yabai_startup_reconcile.sh (the login-race + stray-float home map).
# yabai_send_window{,_external}.sh and yabai_toggle_float.sh still carry their own
# `case` copies; folding those in is a worthwhile follow-up, not a silent one.
YABAI_PINNED_HOMES="wezterm-gui:terminal|WezTerm:terminal|Todoist:todo|Granola:schedule|Spark Mail:mail|Notion Calendar:calendar|Messages:messages|ChatGPT:ai|Claude:ai"

# Every app the float sweep can act on -- the pinned map's app names plus the agent
# apps -- as one anchored yabai regex, for the `window_focused` sweep signal's app=
# filter (yabairc). Same escaping as YABAI_AGENT_APPS_RE.
# shellcheck disable=SC2034
YABAI_PINNED_APPS_RE="^($(printf '%s' "$YABAI_PINNED_HOMES" | sed 's/:[^|]*//g')|$(printf '%s' "$YABAI_AGENT_APPS" | sed 's/[()]/\\&/g'))$"

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
yabai_master_index() {
  local djson="${1:-}"
  [ -n "$djson" ] || djson=$(yabai -m query --displays 2>/dev/null)
  printf '%s' "$djson" | jq -r --arg uuid "$YABAI_MASTER_DISPLAY_UUID" '
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

# Ensure an `ext` space exists on external display $1 and emit its index. Reuses an
# existing `ext`. Otherwise CREATES a space (it lands on whatever display is active
# -- usually master) and then MOVES it onto the external by its stable id, appending
# after the scratch so `ext` is never the external's first (disconnect-fragile)
# space. We move-by-id rather than focus-then-create because `display --focus`
# races, and a create that lands on the wrong display silently mislabels the scratch.
# Needs the SA (space create/move/label).
yabai_ensure_ext() {
  local ext="$1" idx before newid d
  idx=$(yabai_ext_index)
  case "$idx" in ''|*[!0-9]*) ;; *) printf '%s' "$idx"; return 0 ;; esac
  case "$ext" in ''|*[!0-9]*) return 1 ;; esac

  before=$(yabai -m query --spaces 2>/dev/null | jq -c '[.[].id]' 2>/dev/null)
  yabai -m space --create >/dev/null 2>&1 || true
  newid=$(yabai -m query --spaces 2>/dev/null | jq -r --argjson before "${before:-[]}" '
    ([.[].id] - $before)[0] // empty' 2>/dev/null)
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
