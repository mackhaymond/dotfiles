#!/bin/bash

# Startup reconciliation -- fixes pinned apps landing on the WRONG space after login.
#
# At login, macOS restores app windows around the same time yabai starts. Windows
# created before yabai registered its signals get no window_created/application_
# launched event, and yabairc's one-shot `rule --apply` can run BEFORE those windows
# exist -- or before `sudo yabai --load-sa` finishes (window->space moves need the
# scripting addition). Result: pinned apps (WezTerm, Todoist, Granola, Spark Mail,
# Notion Calendar, Messages, ChatGPT, Codex; Arc via Hammerspoon) sit on the wrong
# space until a manual `yabai --restart-service`.
#
# This POLLS UNTIL STABLE: re-load the scripting addition once, then repeatedly
# re-apply the space= rules + re-pin Arc until every RUNNING pinned app is on its
# home space (or a hard time cap). Polling self-truncates on a fast login (exits in
# a couple of seconds) and self-extends for slow-launching apps (Electron: ChatGPT,
# Notion Calendar, Messages) -- more robust than a fixed ramp, which could miss an
# app that finishes restoring after the last pass.
#
# Run BACKGROUNDED from yabairc (`"$YABAI_STARTUP_RECONCILE" &`) so it NEVER blocks
# yabai startup. Single-flighted (mkdir lock, like yabai_heal.sh) so repeated
# restarts don't stack overlapping polls. Every action is idempotent.

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"
HS="/opt/homebrew/bin/hs"

CAP_SECONDS="${YABAI_RECONCILE_CAP:-90}"   # hard stop so a never-launching app can't poll forever
LOCK="${TMPDIR:-/tmp}/yabai_startup_reconcile.lock"

# Single-flight (mirrors yabai_heal.sh): one reconcile at a time. A concurrent
# (re)start drops -- the in-flight poll already re-applies against current state.
# Recover a lock orphaned by a crash, older than the cap + margin so a live poll is
# never reaped.
if ! mkdir "$LOCK" 2>/dev/null; then
  now=$(date +%s)
  mtime=$(stat -f %m "$LOCK" 2>/dev/null || printf '%s' "$now")
  if [ "$((now - mtime))" -ge "$((CAP_SECONDS + 30))" ]; then
    rmdir "$LOCK" 2>/dev/null || true
    mkdir "$LOCK" 2>/dev/null || exit 0
  else
    exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

# Are all RUNNING pinned apps on their home space? Arc is handled by arcSync and is
# excluded here; windows on an UNLABELED space (e.g. native-fullscreen) are ignored.
# Returns 0 (settled) when no pinned-app window sits on a wrong labeled space.
pins_settled() {
  local win spaces off
  win=$(yabai -m query --windows 2>/dev/null) || return 1
  spaces=$(yabai -m query --spaces 2>/dev/null) || return 1
  off=$(jq -n --argjson w "$win" --argjson s "$spaces" '
    ($s | map({ (.index|tostring): (.label // "") }) | add) as $lbl
    | { "wezterm-gui":"terminal", "WezTerm":"terminal", "Todoist":"todo",
        "Granola":"schedule", "Spark Mail":"mail", "Notion Calendar":"calendar",
        "Messages":"messages", "ChatGPT":"ai", "Claude":"ai", "Codex":"codex" } as $home
    | [ $w[]
        | select($home[.app] != null)
        | { want: $home[.app], have: ($lbl[(.space|tostring)] // "") }
        | select(.have != "" and .have != .want) ]
    | length' 2>/dev/null) || return 1
  [ "${off:-1}" = "0" ]
}

# Ensure the scripting addition is loaded (window->space moves need it). `-n` so a
# stale NOPASSWD hash fast-fails instead of ever waiting on a prompt in this
# TTY-less context.
sudo -n yabai --load-sa >/dev/null 2>&1 || true

# Re-assert pins until everything restored has landed home, or we hit the cap.
deadline=$(( $(date +%s) + CAP_SECONDS ))
while :; do
  yabai -m rule --apply >/dev/null 2>&1 || true
  "$HS" -c "arcSync()" >/dev/null 2>&1 || true
  pins_settled && break
  [ "$(date +%s)" -ge "$deadline" ] && break
  sleep 2
done
