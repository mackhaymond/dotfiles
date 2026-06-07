#!/bin/bash

# Startup reconciliation -- fixes pinned apps landing on the WRONG space after login.
#
# At login, macOS restores app windows around the same time yabai starts. Windows
# created before yabai registered its signals get no window_created/application_
# launched event, and yabairc's one-shot `rule --apply` can run BEFORE those windows
# exist -- or before `sudo yabai --load-sa` finishes (window->space moves need the
# scripting addition). Result: pinned apps (WezTerm, Todoist, Granola, Spark Mail,
# Notion Calendar, Messages, ChatGPT, Codex, Arc) sit on the wrong space until a
# manual `yabai --restart-service`.
#
# This re-asserts the space= rules + re-pins the Arc main windows a few times as the
# session settles, and re-loads the scripting addition once in case the login-time
# load raced. Run BACKGROUNDED from yabairc (`"$YABAI_STARTUP_RECONCILE" &`) so it
# NEVER blocks yabai startup. Idempotent: each pass is a no-op once everything is
# already on its home space.

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"
HS="/opt/homebrew/bin/hs"

# Let the login settle a moment, then ensure the scripting addition is loaded (a
# retry of yabairc's load in case it raced -- moving windows across spaces needs it).
sleep 3
sudo yabai --load-sa >/dev/null 2>&1 || true

# Re-apply the space= rules + re-pin Arc on a short ramp, because apps finish
# launching / restoring their windows over the first ~30s after login. Cheap and
# idempotent, so a few passes cost nothing once windows have settled.
for delay in 0 4 8 15; do
  sleep "$delay"
  yabai -m rule --apply >/dev/null 2>&1 || true
  "$HS" -c "arcSync()" >/dev/null 2>&1 || true
done
