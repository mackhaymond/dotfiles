#!/usr/bin/env bash
#
# coffee-watcher.sh
#
# Long-running daemon that polls caffeination state at 500ms and pushes
# changes to tmux *immediately* via `refresh-client -S` instead of waiting
# for the next status-interval tick. Spawned by tmux.conf at server start.
#
# Why polling: Raycast doesn't publish extension events, and macOS power-
# assertion notifications are only available via IOKit (Swift/ObjC). A
# 500ms `pgrep` poll costs ~microseconds and is indistinguishable from
# event-driven in practice.
#
# Singleton: only one watcher per UID; re-invocation is a no-op.
# Self-terminates if the tmux server dies.

set -u

PID_FILE="${TMPDIR:-/tmp}/coffee-watcher.$(id -u).pid"

# Singleton guard.
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
  exit 0
fi
echo "$$" >"$PID_FILE"
trap 'rm -f "$PID_FILE"' EXIT INT TERM HUP

if ! command -v tmux >/dev/null 2>&1; then
  exit 0
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STATUS_SCRIPT="$SCRIPT_DIR/coffee-status.sh"

prev=""
while :; do
  # Bail when the tmux server is gone — keeps us from leaking forever.
  tmux list-sessions >/dev/null 2>&1 || break

  if pgrep -xq caffeinate 2>/dev/null; then
    cur="on"
  else
    cur="off"
  fi

  if [[ "$cur" != "$prev" ]]; then
    bash "$STATUS_SCRIPT"
    # Force every attached client to redraw the status bar NOW.
    tmux refresh-client -S 2>/dev/null || true
    prev="$cur"
  fi

  sleep 0.5
done
