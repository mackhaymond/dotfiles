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
YABAI_LABELS="terminal main school todo schedule mail calendar messages chatgpt codex"

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
