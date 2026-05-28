#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Restart yabai
# @raycast.mode compact

# Optional parameters:
# @raycast.icon 🤖

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

if yabai --restart-service; then
  echo "yabai restarted"
else
  status=$?
  echo "failed to restart yabai"
  exit "$status"
fi
