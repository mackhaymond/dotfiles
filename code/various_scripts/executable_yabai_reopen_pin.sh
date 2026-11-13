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
#
# And only when that app has been running for MIN_APP_AGE seconds. A just-relaunched
# Claude has a level -1 window that yabai does not track yet, and an untracked window
# is not in `query --windows`: `ai` only LOOKS empty. Landing on it within seconds of
# the relaunch sent `reopen`, which activated Claude and pulled the user to whatever
# space that window was really on. The patched yabai's last adoption check for an app
# runs +30s after its launch (README, "Patched yabai"), so before that an empty space
# proves nothing and the run does nothing at all -- not even for the next app in the
# list, which would be reopened on the strength of the same unproven emptiness.

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

MIN_APP_AGE=35

while IFS= read -r app; do
  [ -n "$app" ] || continue
  # `"pid"=1234` when running, nothing otherwise: one call answers both "is it running"
  # and "which process do I age".
  pid=$(lsappinfo info -only pid -app "$app" 2>/dev/null | sed -n 's/.*"pid"=\([0-9][0-9]*\).*/\1/p' | head -n 1)
  [ -n "$pid" ] || continue
  # etime is [[dd-]hh:]mm:ss (same conversion as yabai_startup_reconcile.sh). An age
  # that cannot be read is treated as old, i.e. the pre-existing behaviour.
  age=$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ' \
    | awk -F'[-:]' 'NF >= 2 { n = NF; t = $n + 60 * $(n - 1); if (n >= 3) t += 3600 * $(n - 2);
                              if (n >= 4) t += 86400 * $(n - 3); print t }')
  case "$age" in ''|*[!0-9]*) age=$MIN_APP_AGE ;; esac
  [ "$age" -ge "$MIN_APP_AGE" ] || exit 0
  # Trim only here, on the one path that appends: the common path stays one query.
  yabai_log_trim reopen
  osascript -e "tell application \"$app\" to reopen" >/dev/null 2>&1 \
    && yabai_log reopen "reopen app=\"$app\" label=$label" \
    || yabai_log reopen "reopen-FAILED app=\"$app\" label=$label"
  exit 0
done <<<"$apps"
exit 0
