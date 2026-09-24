#!/bin/bash
#
# codexbar-usage-push.sh — refresh the usage cache because something just
# USED some, instead of waiting for the poll.
#
# Fired by a Claude Code Stop hook: a turn ended, so the numbers moved. The
# tmux tick keeps polling on @codexbar_stale_after_seconds for everything
# this can't see (the claude.ai app, a phone); this makes the common case —
# a Claude Code session finishing a turn — show up in seconds instead of
# ~2 minutes.
#
# THE WHOLE POINT IS THE COALESCING. With five or six sessions running, Stop
# fires in clusters, and the underlying script's --refresh is neither
# rate-limited nor debounced (it only yields when its lock is *currently*
# held). So this holds its own lock and promises two things:
#
#   1. At most one fetch per MIN_GAP seconds, however many Stops arrive.
#   2. No Stop is lost: one that arrives while a fetch is in flight leaves a
#      pending marker, and the holder runs exactly one trailing fetch after
#      the current one — so a session that finished mid-fetch is counted now,
#      not at the next poll.
#
# It also honours the script's backoff file, which --refresh itself does not.

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

SRC="$HOME/.config/tmux/scripts/codexbar-usage-status.sh"
CACHE_DIR="$HOME/.cache/codexbar-tmux"
CACHE_FILE="$CACHE_DIR/usage.json"
LOCK="$CACHE_DIR/push.lock"
PENDING="$CACHE_DIR/push.pending"
# Seconds between fetches, whatever the Stop rate. Was 20: the usage
# endpoint's budget is per account token and shared with every Claude Code
# session on it, and under heavy use a fetch every 20s answered
# rate_limit_error often enough to hold the numbers still for minutes.
MIN_GAP=60
LOCK_STALE=120    # a holder older than this is dead; take the lock

[[ -x "$SRC" ]] || exit 0
mkdir -p "$CACHE_DIR" 2>/dev/null || exit 0

now() { date +%s; }

# Whatever the tmux status line is configured for — with the same fallback
# the app uses, NOT the script's own default of codex: both providers share
# one usage.json, and guessing wrong would label Codex numbers as Claude.
provider() {
  local p
  p="$(tmux show-option -gqv @codexbar_provider 2>/dev/null || true)"
  printf '%s' "${p:-claude}"
}

acquire() {
  if mkdir "$LOCK" 2>/dev/null; then
    printf '%s\n' "$(now)" >"$LOCK/started_at" 2>/dev/null || true
    return 0
  fi
  # Someone holds it. Dead holder (a killed session mid-fetch) or live one?
  local started
  started="$(cat "$LOCK/started_at" 2>/dev/null || echo 0)"
  [[ "$started" =~ ^[0-9]+$ ]] || started=0
  if (( $(now) - started > LOCK_STALE )); then
    rm -rf "$LOCK" 2>/dev/null
    mkdir "$LOCK" 2>/dev/null && printf '%s\n' "$(now)" >"$LOCK/started_at" && return 0
  fi
  return 1
}

if ! acquire; then
  # A fetch is in flight. Leave word that another Stop happened during it so
  # the holder runs one more; then get out of Claude Code's way.
  touch "$PENDING" 2>/dev/null
  exit 0
fi
# This is a short-lived script, not a persistent shell: an EXIT trap here
# fires when THIS process ends, which is the right moment.
trap 'rm -rf "$LOCK"' EXIT

PROVIDER="$(provider)"

for pass in 1 2; do
  rm -f "$PENDING"

  # The script's backoff: "fail_count next_allowed". It is written on failure
  # and cleared on success or by prefix+u, and --refresh ignores it — so it
  # is checked here, the way the script's own --tick path would.
  backoff="$CACHE_DIR/refresh_backoff_$PROVIDER"
  if [[ -f "$backoff" ]]; then
    read -r _ next_allowed <"$backoff" 2>/dev/null || next_allowed=0
    [[ "$next_allowed" =~ ^[0-9]+$ ]] || next_allowed=0
    (( $(now) < next_allowed )) && exit 0
  fi

  # Rate limit by waiting out the remainder of the gap rather than dropping
  # the event: while we sleep, further Stops fold into the pending marker,
  # and the fetch that follows counts all of them.
  updated=0
  if [[ -f "$CACHE_FILE" ]] && command -v jq >/dev/null 2>&1; then
    updated="$(jq -r '.updated_at // 0' "$CACHE_FILE" 2>/dev/null || echo 0)"
    [[ "$updated" =~ ^[0-9]+$ ]] || updated=0
  fi
  age=$(( $(now) - updated ))
  if (( age >= 0 && age < MIN_GAP )); then
    sleep $(( MIN_GAP - age ))
    rm -f "$PENDING"
  fi

  # EAGER: an unforced --refresh skips providers whose numbers are younger
  # than the poll interval, which is exactly when this runs; this provider
  # just moved, so waive that for it (its backoff still applies).
  CODEXBAR_USAGE_PROVIDER="$PROVIDER" CODEXBAR_USAGE_EAGER_PROVIDERS="$PROVIDER" \
    "$SRC" --refresh >/dev/null 2>&1 || true
  # --refresh already publishes the tmux user options; this makes the status
  # line redraw now rather than at its next status-interval.
  tmux refresh-client -S >/dev/null 2>&1 || true

  # A Stop landed while that fetch ran. Its usage may or may not have made it
  # into the response we just got, so go once more — once, not a loop.
  [[ -e "$PENDING" ]] || break
done
exit 0
