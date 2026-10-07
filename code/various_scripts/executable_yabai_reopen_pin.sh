#!/bin/bash

# Entering a pinned app's home space and finding it EMPTY while that app is running
# with no visible window -> ask the app to show its window, as a Dock click would.
#
#   yabai_reopen_pin.sh        # from yabairc's space_changed signal (label reopen_pin)
#
# The case it exists for (2026-10-06): Claude Desktop's stealth update relaunch, when
# another app is fullscreen at that moment, logs "Other app is fullscreen, staying
# hidden to avoid Space switch", calls app.hide() and never shows its (born-hidden)
# window. yabai sees NO window at all, so no rule, sweep or restart can place it --
# `ai` is just empty and Claude looks gone. Its `app.on("activate")` handler -- fired by
# the reopen Apple event a Dock click sends -- calls show() on the hidden window, and
# a window shown while you are on `ai` lands on `ai`. Same for any pinned app whose
# window was closed (cmd+W) while it kept running: going to its space brings it back.
#
# Only the FOCUSED space, only when nothing visible is on it (a space with any window
# -- ChatGPT on `ai`, say -- is left alone), only apps already running (`tell
# application` would otherwise LAUNCH one), and only the first running app of that
# label in YABAI_PINNED_HOMES order (Claude before ChatGPT). Common path: one query.

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/yabai_common.sh"

label=$(yabai -m query --spaces --space 2>/dev/null | jq -r '.label // ""' 2>/dev/null)
[ -n "$label" ] || exit 0

# Apps whose home is this label, in map order (wezterm-gui is the same app as WezTerm).
apps=$(printf '%s' "$YABAI_PINNED_HOMES" | tr '|' '\n' | awk -F: -v l="$label" '$2 == l && $1 != "wezterm-gui" { print $1 }')
[ -n "$apps" ] || exit 0

visible=$(yabai -m query --windows --space "$label" 2>/dev/null \
  | jq '[.[] | select((."is-hidden" | not) and (."is-minimized" | not))] | length' 2>/dev/null)
[ "$visible" = 0 ] || exit 0

while IFS= read -r app; do
  [ -n "$app" ] || continue
  lsappinfo info -only pid -app "$app" 2>/dev/null | grep -q '"pid"=[0-9]' || continue
  osascript -e "tell application \"$app\" to reopen" >/dev/null 2>&1 \
    && yabai_log reopen "reopen app=\"$app\" label=$label" \
    || yabai_log reopen "reopen-FAILED app=\"$app\" label=$label"
  exit 0
done <<<"$apps"
exit 0
