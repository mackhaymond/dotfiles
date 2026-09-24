#!/usr/bin/env bash

set -euo pipefail

# Not every caller hands this script a login PATH. UsageBar is a LaunchAgent
# and runs it under launchd's /usr/bin:/bin:/usr/sbin:/sbin, where codexbar,
# tmux and the Homebrew jq do not exist: every Codex fetch it started failed as
# "missing tool codexbar" and walked Codex's backoff ladder up to an hour, and
# none of the @codexbar_* options were read. Append (never prepend — a caller
# with a real PATH keeps its own precedence) the places those tools live.
for _bin in /opt/homebrew/bin /opt/homebrew/sbin /usr/local/bin "$HOME/.local/bin" "$HOME/.bun/bin"; do
  if [[ -d "$_bin" && ":${PATH:-}:" != *":${_bin}:"* ]]; then
    PATH="${PATH:+${PATH}:}${_bin}"
  fi
done
unset _bin
export PATH


CACHE_DIR="${HOME}/.cache/codexbar-tmux"
CACHE_FILE="${CACHE_DIR}/usage.json"
LOCKDIR="${CACHE_FILE}.lock"

# Rolling sample history for recent-rate pacing projection. Each line is
# {"t":epoch,"s":session_used,"w":weekly_used,"sr":session_resets_at,
# "wr":weekly_resets_at,"f":fable_used,"fr":fable_resets_at}. The
# "sr"/"wr"/"fr" fields let us discard samples that belong to a previous
# rolling window when projecting forward. Lines written before the fable
# module existed have no "f"/"fr" and are simply never selected for it.
#
# THIS FILE IS CLAUDE'S, AND ONLY CLAUDE'S. A second provider gets its own
# history file with the SAME short keys (history_file_for), rather than new
# letters on these lines: CuaNotch pins the s/w/f case block in
# pace_recent_rate against its own reader, and a per-provider FILE keeps that
# mapping — and every line already written — exactly as it is. Same reasoning
# for usage-raw.json. The whole cross-repo contract is spelled out above
# write_usage_cache.
HISTORY_FILE="${CACHE_DIR}/usage-history.jsonl"
HISTORY_MAX_LINES=120

# Claude's session/weekly utilization as Claude Code itself last saw it,
# written by codexbar-usage-live.sh from the status line (see that script),
# and a copy of the sample most recently folded into usage.json — a tick
# compares the two to catch a sample whose own --merge-live found the lock busy.
LIVE_SAMPLE_FILE="${CACHE_DIR}/claude-live.json"
LIVE_MERGED_MARKER="${CACHE_DIR}/claude-live.merged"
HISTORY_RECENT_WINDOW_SECONDS=1800

CODEXBAR_TMP_FILES=()

cleanup_tmp_files() {
  local f
  for f in "${CODEXBAR_TMP_FILES[@]:-}"; do
    [[ -n "${f:-}" ]] && rm -f "$f" 2>/dev/null || true
  done
  CODEXBAR_TMP_FILES=()
}

trap 'cleanup_tmp_files' EXIT INT TERM HUP

tmux_opt_or_empty() {
  local opt_name="${1:-}"

  command -v tmux >/dev/null 2>&1 || { printf '%s' ""; return 0; }
  [[ -n "${opt_name:-}" ]] || { printf '%s' ""; return 0; }

  tmux show-option -gqv "$opt_name" 2>/dev/null || true
}

opt_or_env_or_default() {
  local opt_name="${1:-}" env_name="${2:-}" default_value="${3:-}"

  local v
  v="$(tmux_opt_or_empty "$opt_name")"
  if [[ -n "${v:-}" ]]; then
    printf '%s' "$v"
    return 0
  fi

  if [[ -n "${env_name:-}" ]]; then
    v="${!env_name:-}"
    if [[ -n "${v:-}" ]]; then
      printf '%s' "$v"
      return 0
    fi
  fi

  printf '%s' "$default_value"
}

parse_int_with_default() {
  local raw="${1:-}" default_value="${2:-0}"

  if [[ "${raw:-}" =~ ^-?[0-9]+$ ]]; then
    printf '%s' "$raw"
  else
    printf '%s' "$default_value"
  fi

  return 0
}

clamp_int_range() {
  local raw="${1:-}" min="${2:-0}" max="${3:-0}"

  local v
  v="$(parse_int_with_default "$raw" "$min")"

  if (( v < min )); then
    v=$min
  elif (( v > max )); then
    v=$max
  fi

  printf '%s' "$v"
  return 0
}

CODEXBAR_USAGE_DEBUG="$(parse_int_with_default "$(opt_or_env_or_default '@codexbar_debug' 'CODEXBAR_USAGE_DEBUG' '0')" 0)"
USAGE_REFRESH_LOG_FILE="${CACHE_DIR}/usage-refresh.log"

STALE_AFTER_SECONDS="$(parse_int_with_default "$(opt_or_env_or_default '@codexbar_stale_after_seconds' 'CODEXBAR_USAGE_STALE_AFTER_SECONDS' '300')" 300)"

if (( CODEXBAR_USAGE_DEBUG == 0 && STALE_AFTER_SECONDS < 30 )); then
  STALE_AFTER_SECONDS=30
fi

WEB_TIMEOUT_SECONDS="$(clamp_int_range "$(opt_or_env_or_default '@codexbar_web_timeout' 'CODEXBAR_USAGE_WEB_TIMEOUT' '2')" 1 30)"
AUTH_REQUIRED_COLOR="$(opt_or_env_or_default '@codexbar_auth_required_color' 'CODEXBAR_USAGE_AUTH_REQUIRED_COLOR' '#cba6f7')"
AUTH_REQUIRED_TEXT="$(opt_or_env_or_default '@codexbar_auth_required_text' 'CODEXBAR_USAGE_AUTH_REQUIRED_TEXT' 'Need to log in')"

# The third window ("scoped") is whichever narrower cap the provider reports
# alongside its session and weekly ones. On Claude that is the model-scoped
# weekly limit under .limits[] with kind "weekly_scoped", matched by the
# scope's model display name, case-insensitively and by prefix, so a versioned
# name ("Fable 5.1") still matches. On Codex it is the reserve window in
# .usage.extraRateWindows[]. The KEY is structural ("scoped") and the NAME is
# whatever the provider calls it, published as <family>_label — the key used
# to be "fable", which named one provider's model in a slot the other provider
# fills with something else entirely.
SCOPED_MODEL_NAME="$(opt_or_env_or_default '@codexbar_scoped_model' 'CODEXBAR_USAGE_SCOPED_MODEL' '')"
if [[ -z "${SCOPED_MODEL_NAME:-}" ]]; then
  SCOPED_MODEL_NAME="$(opt_or_env_or_default '@codexbar_fable_model' 'CODEXBAR_USAGE_FABLE_MODEL' 'Fable')"
fi

# ── Providers ───────────────────────────────────────────────────────────────
#
# TWO AXES, DELIBERATELY SEPARATE.
#
#   @codexbar_providers        which providers get FETCHED. Normally both:
#                              the numbers are cheap to hold and every reader
#                              downstream (the notch's panel, UsageBar's
#                              popover) wants them side by side.
#   @codexbar_display_provider which ONE of them the tmux status line renders.
#                              Three modules is already the whole right-hand
#                              side of the bar; six would be a second bar.
#
# The legacy single-valued @codexbar_provider still works and seeds both, so a
# config that predates this reads exactly as it did.
PROVIDER_PRIMARY='claude'   # whose numbers the TOP LEVEL of usage.json carries

provider_is_known() {
  case "${1:-}" in
    claude|codex) return 0 ;;
    *) return 1 ;;
  esac
}

provider_label_for() {
  case "${1:-}" in
    claude) printf '%s' 'Claude' ;;
    codex)  printf '%s' 'Codex' ;;
    *)      printf '%s' "${1:-}" ;;
  esac
}

# Accepts "claude codex", "claude,codex" or any mix; drops unknown names and
# duplicates while keeping the order the user wrote.
normalize_provider_list() {
  local raw="${1:-}" seen=' ' out='' p
  for p in ${raw//,/ }; do
    provider_is_known "$p" || continue
    [[ "$seen" == *" $p "* ]] && continue
    seen+="$p "
    out+="${out:+ }$p"
  done
  printf '%s' "$out"
}

USAGE_PROVIDER_LEGACY="$(opt_or_env_or_default '@codexbar_provider' 'CODEXBAR_USAGE_PROVIDER' '')"

USAGE_PROVIDERS="$(normalize_provider_list "$(opt_or_env_or_default '@codexbar_providers' 'CODEXBAR_USAGE_PROVIDERS' '')")"
if [[ -z "${USAGE_PROVIDERS:-}" ]]; then
  USAGE_PROVIDERS="$(normalize_provider_list "$USAGE_PROVIDER_LEGACY")"
fi
if [[ -z "${USAGE_PROVIDERS:-}" ]]; then
  USAGE_PROVIDERS='claude codex'
fi

provider_enabled() {
  local want="${1:-}" p
  for p in $USAGE_PROVIDERS; do
    [[ "$p" == "$want" ]] && return 0
  done
  return 1
}

DISPLAY_PROVIDER="$(opt_or_env_or_default '@codexbar_display_provider' 'CODEXBAR_USAGE_DISPLAY_PROVIDER' '')"
provider_is_known "$DISPLAY_PROVIDER" || DISPLAY_PROVIDER=''
if [[ -z "${DISPLAY_PROVIDER:-}" ]] && provider_is_known "$USAGE_PROVIDER_LEGACY"; then
  DISPLAY_PROVIDER="$USAGE_PROVIDER_LEGACY"
fi
if [[ -z "${DISPLAY_PROVIDER:-}" ]] || ! provider_enabled "$DISPLAY_PROVIDER"; then
  DISPLAY_PROVIDER="${USAGE_PROVIDERS%% *}"
fi

# Per-provider file layout. The PRIMARY provider keeps the unsuffixed names,
# because those three names are a cross-repo contract: CuaNotch and UsageBar
# both read them and neither writes them (dev/check-invariants §17 in cua-notch
# pins them against this script). A second provider gets suffixed files with
# identical INTERNAL shape — same short history keys, same fields — so every
# reader is one filename away from handling any number of providers.
history_file_for() {
  if [[ "${1:-}" == "$PROVIDER_PRIMARY" ]]; then
    printf '%s' "$HISTORY_FILE"
  else
    printf '%s' "${CACHE_DIR}/usage-history-${1}.jsonl"
  fi
}

raw_file_for() {
  if [[ "${1:-}" == "$PROVIDER_PRIMARY" ]]; then
    printf '%s' "${CACHE_DIR}/usage-raw.json"
  else
    printf '%s' "${CACHE_DIR}/usage-raw-${1}.json"
  fi
}

backoff_file_for() {
  printf '%s' "${CACHE_DIR}/refresh_backoff_${1}"
}

# Everything that is per-provider state — which history file pacing reads,
# which backoff ladder a failure arms, what the log line says — hangs off this
# one setter, so a caller switches providers in a single call and cannot half
# switch (the bug shape when three globals are set at three call sites).
ACTIVE_PROVIDER=''
ACTIVE_HISTORY_FILE="$HISTORY_FILE"
BACKOFF_FILE="$(backoff_file_for "$DISPLAY_PROVIDER")"

select_provider() {
  ACTIVE_PROVIDER="${1:-}"
  ACTIVE_HISTORY_FILE="$(history_file_for "$ACTIVE_PROVIDER")"
  BACKOFF_FILE="$(backoff_file_for "$ACTIVE_PROVIDER")"
}

select_provider "$DISPLAY_PROVIDER"

USAGE_LOG_MAX_BYTES=$(( 256 * 1024 ))

rotate_log_if_oversized() {
  [[ -f "$USAGE_REFRESH_LOG_FILE" ]] || return 0

  local size
  size="$(stat -f %z "$USAGE_REFRESH_LOG_FILE" 2>/dev/null || stat -c %s "$USAGE_REFRESH_LOG_FILE" 2>/dev/null || echo 0)"
  [[ "$size" =~ ^[0-9]+$ ]] || return 0
  (( size > USAGE_LOG_MAX_BYTES )) || return 0

  mv -f "$USAGE_REFRESH_LOG_FILE" "${USAGE_REFRESH_LOG_FILE}.1" 2>/dev/null || true
}

log_line() {
  local level="${1:-INFO}" msg="${2:-}"
  mkdir -p "$CACHE_DIR" 2>/dev/null || true
  rotate_log_if_oversized
  printf '%s pid=%s [%s] %s\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S%z')" \
    "$$" "$level" "$msg" \
    >>"$USAGE_REFRESH_LOG_FILE" 2>/dev/null || true
}

log_info()  { log_line INFO  "${1:-}"; }
log_warn()  { log_line WARN  "${1:-}"; }
log_error() { log_line ERROR "${1:-}"; }

log_debug() {
  [[ -n "${CODEXBAR_USAGE_DEBUG:-}" && "${CODEXBAR_USAGE_DEBUG:-}" != "0" ]] || return 0
  log_line DEBUG "${1:-}"
}

truncate_msg() {
  local msg="${1:-}" max="${2:-200}"
  msg="${msg//$'\n'/\\n}"
  msg="${msg//$'\r'/}"
  if (( ${#msg} > max )); then
    msg="${msg:0:max}..."
  fi
  printf '%s' "$msg"
}

log_debug_trunc() { log_debug "$(truncate_msg "${1:-}" "${2:-200}")"; }
log_warn_trunc()  { log_warn  "$(truncate_msg "${1:-}" "${2:-200}")"; }
log_error_trunc() { log_error "$(truncate_msg "${1:-}" "${2:-200}")"; }

debug_flash_codex_icons() {
  (( CODEXBAR_USAGE_DEBUG != 0 )) || return 0
  command -v tmux >/dev/null 2>&1 || return 0

  local flash_color
  flash_color="$(tmux_opt_or_empty '@codexbar_debug_flash_color')"
  if [[ -z "${flash_color:-}" ]]; then
    flash_color='default'
  fi

  local prev_session prev_weekly prev_scoped nonce
  prev_session="$(tmux show-option -gqv @codex_session_color 2>/dev/null || true)"
  prev_weekly="$(tmux show-option -gqv @codex_weekly_color 2>/dev/null || true)"
  prev_scoped="$(tmux show-option -gqv @codex_scoped_color 2>/dev/null || true)"

  nonce="$(date +%s%N 2>/dev/null || date +%s)"

  tmux set-option -gq @codexbar_debug_flash_nonce "$nonce" >/dev/null 2>&1 || true
  tmux set-option -gq @codexbar_debug_flash_prev_session_color "$prev_session" >/dev/null 2>&1 || true
  tmux set-option -gq @codexbar_debug_flash_prev_weekly_color "$prev_weekly" >/dev/null 2>&1 || true
  tmux set-option -gq @codexbar_debug_flash_prev_scoped_color "$prev_scoped" >/dev/null 2>&1 || true

  tmux set-option -gq @codex_session_color "$flash_color" >/dev/null 2>&1 || true
  tmux set-option -gq @codex_weekly_color "$flash_color" >/dev/null 2>&1 || true
  tmux set-option -gq @codex_scoped_color "$flash_color" >/dev/null 2>&1 || true
  tmux refresh-client -S >/dev/null 2>&1 || true
  log_debug "flash: on color=${flash_color}"

  tmux run-shell -b "sleep 0.5; n=\$(tmux show-option -gqv @codexbar_debug_flash_nonce 2>/dev/null); [ \"\$n\" = \"$nonce\" ] || exit 0; fc='$flash_color'; cs=\$(tmux show-option -gqv @codex_session_color 2>/dev/null || true); cw=\$(tmux show-option -gqv @codex_weekly_color 2>/dev/null || true); csc=\$(tmux show-option -gqv @codex_scoped_color 2>/dev/null || true); s=\$(tmux show-option -gqv @codexbar_debug_flash_prev_session_color 2>/dev/null); w=\$(tmux show-option -gqv @codexbar_debug_flash_prev_weekly_color 2>/dev/null); f=\$(tmux show-option -gqv @codexbar_debug_flash_prev_scoped_color 2>/dev/null); if [ \"\$cs\" = \"\$fc\" ]; then if [ -n \"\$s\" ]; then tmux set-option -gq @codex_session_color \"\$s\"; else tmux set-option -gu @codex_session_color; fi; fi; if [ \"\$cw\" = \"\$fc\" ]; then if [ -n \"\$w\" ]; then tmux set-option -gq @codex_weekly_color \"\$w\"; else tmux set-option -gu @codex_weekly_color; fi; fi; if [ \"\$csc\" = \"\$fc\" ]; then if [ -n \"\$f\" ]; then tmux set-option -gq @codex_scoped_color \"\$f\"; else tmux set-option -gu @codex_scoped_color; fi; fi; tmux refresh-client -S;" >/dev/null 2>&1 || true
}

script_abs_path() {
  local script="$0"
  if [[ "$script" != /* ]]; then
    script="$(cd -- "$(dirname -- "$script")" && pwd)/$(basename -- "$script")"
  fi
  printf '%s' "$script"
}

debug_flash_loop_nonce_opt='@codexbar__debug_flash_loop_nonce'
debug_update_counter_opt='@codexbar__debug_update_counter'

debug_flash_loop_enabled() {
  (( CODEXBAR_USAGE_DEBUG != 0 )) || return 1
  command -v tmux >/dev/null 2>&1 || return 1
  (( STALE_AFTER_SECONDS > 0 )) || return 1
  return 0
}

schedule_debug_flash_tick() {
  local nonce="${1:-}" period="${2:-0}"

  [[ -n "${nonce:-}" ]] || return 0
  [[ "$period" =~ ^[0-9]+$ ]] || return 0
  (( period > 0 )) || return 0

  local script
  script="$(script_abs_path)"

  tmux run-shell -b "sleep $period; n=\$(tmux show-option -gqv $debug_flash_loop_nonce_opt 2>/dev/null || true); [ \"\$n\" = \"$nonce\" ] || exit 0; \"$script\" --debug-flash-tick \"$nonce\" >/dev/null 2>&1" >/dev/null 2>&1 || true
}

start_debug_flash_loop_if_needed() {
  command -v tmux >/dev/null 2>&1 || return 0

  if (( CODEXBAR_USAGE_DEBUG == 0 )); then
    return 0
  fi

  if ! debug_flash_loop_enabled; then
    tmux set-option -gu $debug_flash_loop_nonce_opt >/dev/null 2>&1 || true

    local prev_counter
    prev_counter="$(tmux show-option -gqv "$debug_update_counter_opt" 2>/dev/null || true)"
    if [[ -n "${prev_counter:-}" && "${prev_counter:-}" != "0" ]]; then
      tmux set-option -gq "$debug_update_counter_opt" 0 >/dev/null 2>&1 || true
    fi

    return 0
  fi

  local existing
  existing="$(tmux show-option -gqv $debug_flash_loop_nonce_opt 2>/dev/null || true)"
  [[ -n "${existing:-}" ]] && return 0

  local nonce
  nonce="$(date +%s%N 2>/dev/null || date +%s)"

  tmux set-option -gq $debug_flash_loop_nonce_opt "$nonce" >/dev/null 2>&1 || true
  log_debug "flash-loop: start nonce=${nonce} period=${STALE_AFTER_SECONDS}"

  schedule_debug_flash_tick "$nonce" "$STALE_AFTER_SECONDS"
}

debug_flash_tick() {
  local expected_nonce="${1:-}"

  command -v tmux >/dev/null 2>&1 || return 0
  [[ -n "${expected_nonce:-}" ]] || return 0

  local current
  current="$(tmux show-option -gqv $debug_flash_loop_nonce_opt 2>/dev/null || true)"
  [[ -n "${current:-}" && "$current" == "$expected_nonce" ]] || return 0

  if ! debug_flash_loop_enabled; then
    tmux set-option -gu $debug_flash_loop_nonce_opt >/dev/null 2>&1 || true
    log_debug "flash-loop: stop"
    return 0
  fi

  debug_flash_codex_icons
  schedule_debug_flash_tick "$expected_nonce" "$STALE_AFTER_SECONDS"
}

LOCK_STALE_SECONDS=120

# A status redraw fires --tick on tmux's status-interval (a few seconds). A gap
# between consecutive ticks far larger than that means the host was suspended
# (laptop sleep) or the client was detached; either way any refresh backoff
# armed beforehand is stale and no longer reflects current network health. Used
# to clear backoff once on resume so auto-refresh recovers without prefix+u.
WAKE_GAP_SECONDS=60

usage() {
  printf '%s\n' "Usage: $0 {session|weekly|scoped|--refresh|--publish|--merge-live|--tick|--auth-required|--login|--debug-flash-tick <nonce>}" >&2
}

now_epoch() {
  date +%s
}

# Backoff state to avoid spawning refresh every status tick when
# remote refresh keeps failing (battery/network friendly).
# Format: "fail_count next_allowed_epoch" (plain text, no jq required).
read_refresh_backoff() {
  local fail_count next_allowed

  if [[ -f "$BACKOFF_FILE" ]]; then
    read -r fail_count next_allowed <"$BACKOFF_FILE" 2>/dev/null || true
  fi

  if [[ -z "${fail_count:-}" || ! "$fail_count" =~ ^[0-9]+$ ]]; then
    fail_count=0
  fi
  if [[ -z "${next_allowed:-}" || ! "$next_allowed" =~ ^[0-9]+$ ]]; then
    next_allowed=0
  fi

  printf '%s %s\n' "$fail_count" "$next_allowed"
}

refresh_backoff_delay_seconds() {
  local fail_count="${1:-0}"
  if [[ -z "${fail_count:-}" || ! "$fail_count" =~ ^[0-9]+$ ]]; then
    fail_count=0
  fi

  case "$fail_count" in
    0|1) printf '%s' 60 ;;
    2)   printf '%s' 120 ;;
    3)   printf '%s' 300 ;;
    4)   printf '%s' 600 ;;
    5)   printf '%s' 1800 ;;
    *)   printf '%s' 3600 ;;
  esac
}

reset_refresh_backoff() {
  rm -f "$BACKOFF_FILE" 2>/dev/null || true
}

# $1 (optional): the lowest rung this failure may land on, for failures that
# are known to need more than the 60s first step.
record_refresh_backoff_failure() {
  mkdir -p "$CACHE_DIR" 2>/dev/null || true

  local fail_count next_allowed now delay min_rung="${1:-0}"
  read -r fail_count next_allowed < <(read_refresh_backoff)

  fail_count=$(( fail_count + 1 ))
  (( fail_count >= min_rung )) || fail_count=$min_rung
  delay="$(refresh_backoff_delay_seconds "$fail_count")"
  now="$(now_epoch)"
  next_allowed=$(( now + delay ))

  log_warn "backoff: fail_count=${fail_count} next_attempt_in=${delay}s provider=${ACTIVE_PROVIDER}"

  # Write atomically (mktemp + mv) so a concurrent reader in read_refresh_backoff
  # — which does not hold any lock — never observes a truncated/half-written
  # file and mis-evaluates the backoff gate. Mirrors the cache write below.
  umask 077
  local bo_tmp
  if bo_tmp="$(mktemp "${BACKOFF_FILE}.tmp.XXXXXX" 2>/dev/null)"; then
    if printf '%s %s\n' "$fail_count" "$next_allowed" >"$bo_tmp" 2>/dev/null \
       && mv -f "$bo_tmp" "$BACKOFF_FILE" 2>/dev/null; then
      :
    else
      rm -f "$bo_tmp" 2>/dev/null || true
      printf '%s %s\n' "$fail_count" "$next_allowed" >"$BACKOFF_FILE" 2>/dev/null || true
    fi
  else
    printf '%s %s\n' "$fail_count" "$next_allowed" >"$BACKOFF_FILE" 2>/dev/null || true
  fi
}

refresh_fail() {
  record_refresh_backoff_failure
  return 1
}

iso_utc_to_epoch() {
  local iso="${1:-}"
  [[ -n "$iso" ]] || return 1

  if [[ "$iso" =~ ^([0-9-]+T[0-9:]+)\.[0-9]+(.*)$ ]]; then
    iso="${BASH_REMATCH[1]}${BASH_REMATCH[2]}"
  fi
  iso="${iso/+00:00/Z}"

  local epoch
  epoch="$(date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$iso" +%s 2>/dev/null || true)"
  if [[ -z "${epoch:-}" ]]; then
    if date -u -d "$iso" +%s >/dev/null 2>&1; then
      epoch="$(date -u -d "$iso" +%s 2>/dev/null || true)"
    fi
  fi

  [[ "$epoch" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "$epoch"
}

codex_reset_description_to_epoch() {
  local description="${1:-}" now="${2:-}" window_minutes="${3:-}"

  [[ -n "$description" ]] || return 1
  [[ "$now" =~ ^[0-9]+$ ]] || return 1

  # Normalize CodexBar/OpenAI copy like "Resets 7:38 PM". The app may use a
  # narrow no-break space before AM/PM, so collapse whitespace before parsing.
  description="${description#Resets }"
  description="$(printf '%s' "$description" | tr '\302\240\342\200\257' '   ' | awk '{gsub(/[[:space:]]+/, " "); sub(/^ /, ""); sub(/ $/, ""); print}')"

  local time_part
  time_part="$(printf '%s' "$description" | awk 'match($0, /[0-9]{1,2}:[0-9]{2} ?[AP]M/) { print substr($0, RSTART, RLENGTH); exit }')"
  [[ -n "${time_part:-}" ]] || return 1

  local today epoch
  today="$(date -r "$now" '+%Y-%m-%d' 2>/dev/null || date -d "@$now" '+%Y-%m-%d' 2>/dev/null || true)"
  [[ -n "${today:-}" ]] || return 1

  epoch="$(date -j -f '%Y-%m-%d %I:%M %p' "$today $time_part" +%s 2>/dev/null || true)"
  if [[ -z "${epoch:-}" ]]; then
    epoch="$(date -d "$today $time_part" +%s 2>/dev/null || true)"
  fi
  [[ "$epoch" =~ ^[0-9]+$ ]] || return 1

  # If today's occurrence already passed, it is tomorrow's reset. Keep the
  # result bounded by the advertised window to avoid confusing weekly text with
  # a session reset.
  if (( epoch <= now )); then
    epoch=$(( epoch + 86400 ))
  fi

  if [[ "$window_minutes" =~ ^[0-9]+$ ]]; then
    local duration delta
    duration=$(( window_minutes * 60 ))
    delta=$(( epoch - now ))
    (( duration > 0 && delta <= duration )) || return 1
  fi

  printf '%s' "$epoch"
}

clamp_0_100_int() {
  local raw="$1" int

  if [[ "$raw" == *.* ]]; then
    int="${raw%%.*}"
  else
    int="$raw"
  fi

  if ! [[ "$int" =~ ^-?[0-9]+$ ]]; then
    return 1
  fi

  if (( int < 0 )); then
    int=0
  elif (( int > 100 )); then
    int=100
  fi

  printf '%s' "$int"
}

color_for_used_percent() {
  local used="$1"
  if (( used <= 49 )); then
    printf '%s' 'green'
  elif (( used <= 79 )); then
    printf '%s' 'yellow'
  else
    printf '%s' 'red'
  fi
}

# Compute signed pacing delta in percentage points: actual_used% - expected_used%
# at the current point in the window. Positive => over pace, negative => under pace.
# Prints a signed integer like "+5" or "-3" on success; prints nothing when the
# pace is not computable (window not started, just-reset, missing fields, etc.).
pace_delta() {
  local actual_used_percent="$1" window_minutes="$2" resets_at="$3" now="$4"

  [[ "$actual_used_percent" =~ ^[0-9]+$ ]] || return 0
  [[ "$window_minutes" =~ ^[0-9]+$ ]] || return 0
  [[ "$resets_at" =~ ^[0-9]+$ ]] || return 0
  [[ "$now" =~ ^[0-9]+$ ]] || return 0

  local duration time_until_reset elapsed
  duration=$(( window_minutes * 60 ))
  (( duration > 0 )) || return 0

  time_until_reset=$(( resets_at - now ))

  (( time_until_reset > 0 )) || return 0

  if (( time_until_reset > duration )); then
    return 0
  fi

  elapsed=$(( duration - time_until_reset ))
  if (( elapsed < 0 )); then
    elapsed=0
  elif (( elapsed > duration )); then
    elapsed=$duration
  fi

  if (( elapsed == 0 && actual_used_percent > 0 )); then
    return 0
  fi

  awk -v a="$actual_used_percent" -v e="$elapsed" -v d="$duration" 'BEGIN {
    if (d <= 0) { exit }
    expected = (e / d) * 100
    delta = a - expected
    if (delta < 0) { sign = "-"; delta = -delta } else { sign = "+" }
    printf "%s%d", sign, int(delta + 0.5)
  }'
}

# Format the pacing delta as a status-bar suffix like " (+5%)" or " (-3%)".
# Prints nothing when pace_delta is not computable.
pace_suffix() {
  local delta
  delta="$(pace_delta "$@")"
  [[ -n "$delta" ]] || return 0
  printf ' (%s%%)' "$delta"
}

# Map a signed pacing delta (in percentage points) to a status-bar color.
# Thresholds: delta <= 5 green, <= 15 yellow, > 15 red.
# Returns 1 (and prints nothing) if the delta is not a parseable integer.
color_for_pace_delta() {
  local delta="$1"
  [[ "$delta" =~ ^[+-]?[0-9]+$ ]] || return 1

  if (( delta <= 5 )); then
    printf '%s' 'green'
  elif (( delta <= 15 )); then
    printf '%s' 'yellow'
  else
    printf '%s' 'red'
  fi
}

# Pure pace color for a usage window: pacing-delta-based when pacing math is
# computable, absolute-usage color otherwise (e.g., right after a reset, or when
# window/resets_at fields are missing). Published as <family>_pace_color.
pace_color_for_window() {
  local used="$1" window_minutes="$2" resets_at="$3" now="$4"

  local delta color
  delta="$(pace_delta "$used" "$window_minutes" "$resets_at" "$now")"
  if [[ -n "$delta" ]]; then
    if color="$(color_for_pace_delta "$delta")"; then
      printf '%s' "$color"
      return 0
    fi
  fi

  color_for_used_percent "$used"
}

# Level thresholds: how close the window is to running out, regardless of pace.
# Defaults match CuaNotch's warnPct/criticalPct so the two surfaces agree.
LEVEL_WARN_PERCENT="$(clamp_int_range "$(opt_or_env_or_default '@codexbar_warn_percent' 'CODEXBAR_USAGE_WARN_PERCENT' '80')" 1 100)"
LEVEL_CRITICAL_PERCENT="$(clamp_int_range "$(opt_or_env_or_default '@codexbar_critical_percent' 'CODEXBAR_USAGE_CRITICAL_PERCENT' '95')" 1 100)"

color_rank() {
  case "${1:-}" in
    red)    printf '%s' 2 ;;
    yellow) printf '%s' 1 ;;
    *)      printf '%s' 0 ;;
  esac
}

# Pick the status-bar color for a usage window: the worse of the pace color and
# the level band, with one escalation — ahead of pace AND near the end is red,
# because the lead no longer fits in what's left. 100% is always red: a +11%
# pace reads "a bit ahead", but there is nothing left to be ahead with.
color_for_window() {
  local used="$1" window_minutes="$2" resets_at="$3" now="$4"

  local pace_color pace_rank level_rank=0 rank
  pace_color="$(pace_color_for_window "$used" "$window_minutes" "$resets_at" "$now")"
  pace_rank="$(color_rank "$pace_color")"

  if [[ "$used" =~ ^[0-9]+$ ]]; then
    if (( used >= 100 || used >= LEVEL_CRITICAL_PERCENT )); then
      level_rank=2
    elif (( used >= LEVEL_WARN_PERCENT )); then
      level_rank=1
    fi
  fi

  rank=$(( pace_rank > level_rank ? pace_rank : level_rank ))
  if (( pace_rank >= 1 && level_rank >= 1 )); then
    rank=2
  fi

  case "$rank" in
    2) printf '%s' 'red' ;;
    1) printf '%s' 'yellow' ;;
    *) printf '%s' 'green' ;;
  esac
}

# When ONE provider was last FETCHED from its endpoint: the block's fetched_at,
# which only a successful fetch moves, falling back to updated_at for a block
# written before fetched_at existed. Not updated_at itself: Claude's updated_at
# also moves when Claude Code's live numbers are folded in (merge_live_claude),
# and gating the poll on that would stop fetching the things only the endpoint
# has (the scoped cap, severities, the breakdown) whenever a session is busy.
# Read from the file's own fields rather than its mtime, because usage.json is
# rewritten for reasons that are not "these numbers are new". 0 when the
# provider has no numbers yet, which reads as stale.
provider_updated_at() {
  local provider="${1:-}" ts=''
  [[ -f "$CACHE_FILE" ]] || { printf '%s' 0; return 0; }

  if command -v jq >/dev/null 2>&1; then
    # A cache written before the providers map existed IS the primary's block.
    ts="$(jq -r --arg p "$provider" --arg primary "$PROVIDER_PRIMARY" '
      ( .providers[$p].fetched_at
        // .providers[$p].updated_at
        // (if (has("providers") | not) and $p == $primary then .updated_at else null end)
        // 0 ) | floor
    ' "$CACHE_FILE" 2>/dev/null || true)"
  fi
  [[ "${ts:-}" =~ ^[0-9]+$ ]] || ts=0
  printf '%s' "$ts"
}

# True when this provider is worth a network call right now: its numbers are
# stale AND its own backoff ladder has run out.
#
# PER PROVIDER, NOT "OLDEST STALE + ANYONE ALLOWED". The gate used to take the
# oldest provider's updated_at for staleness and then ask whether ANY ladder
# was open. With Codex failing (stale for good) and Claude healthy (ladder
# open), that pair was true on every status tick, so every tick re-fetched
# Claude — five or six times inside ten seconds whenever its backoff cleared,
# until the endpoint answered rate_limit_error and armed the ladder again. The
# visible result was Claude's numbers sticking for minutes at a time, while
# the provider that was actually stale never got anything from it.
provider_refresh_due() {
  local provider="${1:-}" now="${2:-}" ts age fc na saved="$ACTIVE_PROVIDER" due=1
  [[ "$now" =~ ^[0-9]+$ ]] || now="$(now_epoch)"

  ts="$(provider_updated_at "$provider")"
  age=$(( now - ts ))
  # A backward wall-clock step (e.g. NTP correction on wake) makes age
  # negative; treat that as stale so the recovery refresh isn't suppressed.
  if (( age < 0 || age >= STALE_AFTER_SECONDS )); then
    select_provider "$provider"
    read -r fc na < <(read_refresh_backoff)
    (( now >= na )) && due=0
    [[ -n "${saved:-}" ]] && select_provider "$saved"
  fi
  return $due
}

any_provider_refresh_due() {
  local now="${1:-}" p
  [[ "$now" =~ ^[0-9]+$ ]] || now="$(now_epoch)"
  for p in $USAGE_PROVIDERS; do
    provider_refresh_due "$p" "$now" && return 0
  done
  return 1
}

strip_legacy_label_prefix() {
  local v="${1:-}"

  v="${v#S:}"
  v="${v#W:}"

  if [[ "$v" == "--" ]]; then
    v="--%"
  fi

  printf '%s' "$v"
}

format_time_until_reset() {
  local resets_at_epoch="$1" now="$2"

  if [[ -z "${resets_at_epoch:-}" || ! "$resets_at_epoch" =~ ^[0-9]+$ ]]; then
    printf '%s' '--'
    return 0
  fi

  local delta_seconds
  delta_seconds=$(( resets_at_epoch - now ))
  if (( delta_seconds <= 0 )); then
    printf '%s' '--'
    return 0
  fi

  local total_minutes days hours minutes
  total_minutes=$(( delta_seconds / 60 ))
  if (( total_minutes <= 0 )); then
    printf '%s' '--'
    return 0
  fi

  days=$(( total_minutes / (60 * 24) ))
  hours=$(( (total_minutes / 60) % 24 ))
  minutes=$(( total_minutes % 60 ))

  if (( days >= 1 )); then
    printf '%sd%sh' "$days" "$hours"
  elif (( hours >= 1 )); then
    printf '%sh%sm' "$hours" "$minutes"
  else
    printf '%sm' "$minutes"
  fi
}

# Append a successful-refresh sample to the rolling history file. Called from
# refresh_cache after the cache file is committed. Best-effort: silently skips
# on any I/O failure rather than failing the refresh. Trims to the last
# HISTORY_MAX_LINES entries to bound disk usage and read cost.
append_usage_history() {
  local now="$1" session_used="$2" weekly_used="$3" session_resets="$4" weekly_resets="$5"
  local scoped_used="${6:-}" scoped_resets="${7:-}"

  [[ "$now" =~ ^[0-9]+$ ]] || return 0
  [[ "$session_used" =~ ^[0-9]+$ ]] || return 0
  [[ "$weekly_used"  =~ ^[0-9]+$ ]] || return 0

  mkdir -p "$CACHE_DIR" 2>/dev/null || return 0

  local file="$ACTIVE_HISTORY_FILE"
  [[ -n "${file:-}" ]] || file="$HISTORY_FILE"

  local sr_json wr_json sc_json scr_json
  sr_json='null'
  wr_json='null'
  sc_json='null'
  scr_json='null'
  [[ "$session_resets" =~ ^[0-9]+$ ]] && sr_json="$session_resets"
  [[ "$weekly_resets"  =~ ^[0-9]+$ ]] && wr_json="$weekly_resets"
  [[ "$scoped_used"    =~ ^[0-9]+$ ]] && sc_json="$scoped_used"
  [[ "$scoped_resets"  =~ ^[0-9]+$ ]] && scr_json="$scoped_resets"

  umask 077
  printf '{"t":%s,"s":%s,"w":%s,"sr":%s,"wr":%s,"sc":%s,"scr":%s}\n' \
    "$now" "$session_used" "$weekly_used" "$sr_json" "$wr_json" "$sc_json" "$scr_json" \
    >>"$file" 2>/dev/null || return 0

  local line_count
  line_count="$(wc -l <"$file" 2>/dev/null | tr -d ' \t' || echo 0)"
  [[ "$line_count" =~ ^[0-9]+$ ]] || return 0
  if (( line_count > HISTORY_MAX_LINES )); then
    local tmp
    tmp="$(mktemp "${file}.trim.XXXXXX" 2>/dev/null)" || return 0
    if tail -n "$HISTORY_MAX_LINES" "$file" >"$tmp" 2>/dev/null; then
      mv -f "$tmp" "$file" 2>/dev/null || rm -f "$tmp" 2>/dev/null
    else
      rm -f "$tmp" 2>/dev/null
    fi
  fi
}

# Recent-rate (% per second) from the history file, scoped to the current
# rolling window. Picks samples with matching resets_at within the last
# window_seconds, then derives rate = du / dt across the chronological
# earliest and latest sample in that slice. Requires at least 2 samples
# spanning at least min_span_seconds (guards against extrapolating from a
# single message). Empty stdout when not computable (no jq, no history,
# too few samples, too short a span, plateaued/decreased usage), so callers
# can cleanly fall back to a wider window or window-start extrapolation.
#
# Args: mode current_resets_at now window_seconds [min_span_seconds]
pace_recent_rate() {
  local mode="$1" current_resets_at="$2" now="$3"
  local window_seconds="${4:-1800}" min_span_seconds="${5:-0}"

  local history_file="$ACTIVE_HISTORY_FILE"
  [[ -n "${history_file:-}" ]] || history_file="$HISTORY_FILE"

  [[ -f "$history_file" ]] || return 0
  command -v jq >/dev/null 2>&1 || return 0
  [[ "$now" =~ ^[0-9]+$ ]] || return 0
  [[ "$current_resets_at" =~ ^[0-9]+$ ]] || return 0
  [[ "$window_seconds" =~ ^[0-9]+$ ]] || return 0

  local used_key resets_key
  case "$mode" in
    session) used_key='s'; resets_key='sr' ;;
    weekly)  used_key='w'; resets_key='wr' ;;
    scoped)  used_key='sc'; resets_key='scr' ;;
    *) return 0 ;;
  esac

  local cutoff samples
  cutoff=$(( now - window_seconds ))

  samples="$(jq -c \
    --argjson c "$cutoff" \
    --argjson r "$current_resets_at" \
    --arg uk "$used_key" \
    --arg rk "$resets_key" \
    'select((.t // 0) >= $c and (.[$rk] // null) == $r) | [.t, (.[$uk] // 0)]' \
    "$history_file" 2>/dev/null || true)"
  [[ -n "$samples" ]] || return 0

  awk -v min_span="$min_span_seconds" '
    BEGIN { n = 0 }
    {
      line = $0
      gsub(/[\[\]]/, "", line)
      split(line, a, ",")
      t = a[1] + 0; u = a[2] + 0
      if (n == 0) { min_t = t; max_t = t; min_u = u; max_u = u }
      else {
        if (t < min_t) { min_t = t; min_u = u }
        if (t > max_t) { max_t = t; max_u = u }
      }
      n++
    }
    END {
      if (n < 2) exit
      dt = max_t - min_t
      du = max_u - min_u
      if (dt <= 0) exit
      if (dt < min_span) exit
      if (du <= 0) exit
      printf "%.10f", du / dt
    }
  ' <<<"$samples"
}

# Project seconds-until-exhaust given current usage and window-elapsed time.
# Prefers a "recent rate" computed from the rolling history file (last ~30
# min of samples in the current window), so a recent burst is reflected in
# the ETA. Falls back to linear extrapolation from window start - rate =
# used / elapsed, eta = (100 - used) / rate - when the history doesn't yet
# have enough usable signal (just installed, just reset, plateaued, or
# decreased). The fallback path keeps a min-elapsed floor of ~1% of the
# window so a single early API ping right after a reset doesn't produce
# wildly noisy ETAs. Returns the integer second count on stdout; prints
# nothing (empty stdout) when the projection is not computable.
#
# Args: used window_minutes resets_at now [mode]
#   mode = "session", "weekly" or "scoped" - enables the recent-rate path.
#   Omit to force long-term-only behavior (used by tests).
pace_eta_seconds() {
  local used="$1" window_minutes="$2" resets_at="$3" now="$4" mode="${5:-}"

  [[ "$used" =~ ^[0-9]+$ ]] || return 0
  [[ "$window_minutes" =~ ^[0-9]+$ ]] || return 0
  [[ "$resets_at" =~ ^[0-9]+$ ]] || return 0
  [[ "$now" =~ ^[0-9]+$ ]] || return 0

  local duration time_until_reset elapsed
  duration=$(( window_minutes * 60 ))
  (( duration > 0 )) || return 0

  time_until_reset=$(( resets_at - now ))
  (( time_until_reset > 0 )) || return 0
  (( time_until_reset <= duration )) || return 0

  elapsed=$(( duration - time_until_reset ))
  (( elapsed > 0 )) || return 0
  (( used > 0 )) || return 0

  if (( used >= 100 )); then
    printf '%s' 0
    return 0
  fi

  local rate=''
  if [[ -n "$mode" ]]; then
    local rate10 rate30 rate_long
    rate10="$(pace_recent_rate "$mode" "$resets_at" "$now" 600 180 2>/dev/null || true)"
    rate30="$(pace_recent_rate "$mode" "$resets_at" "$now" 1800 0 2>/dev/null || true)"

    rate_long=''
    local min_elapsed
    min_elapsed=$(( duration / 100 ))
    if (( min_elapsed < 60 )); then
      min_elapsed=60
    fi
    if (( elapsed >= min_elapsed )); then
      rate_long="$(awk -v u="$used" -v e="$elapsed" 'BEGIN { printf "%.10f", u/e }')"
    fi

    rate="$(awk -v r10="${rate10:-0}" -v r30="${rate30:-0}" -v rL="${rate_long:-0}" '
      BEGIN {
        m = 0
        if (r10+0 > m) m = r10+0
        if (r30+0 > m) m = r30+0
        if (rL+0  > m) m = rL+0
        if (m > 0) printf "%.10f", m
      }
    ')"
  fi

  if [[ -z "${rate:-}" ]]; then
    local min_elapsed_fb
    min_elapsed_fb=$(( duration / 100 ))
    if (( min_elapsed_fb < 60 )); then
      min_elapsed_fb=60
    fi
    (( elapsed >= min_elapsed_fb )) || return 0
    rate="$(awk -v u="$used" -v e="$elapsed" 'BEGIN { printf "%.10f", u/e }')"
    [[ -n "${rate:-}" ]] || return 0
  fi

  awk -v u="$used" -v r="$rate" 'BEGIN {
    rem = 100 - u
    if (rem <= 0) { printf "%d", 0; exit }
    if (r <= 0) { exit }
    eta = rem / r
    if (eta < 0) eta = 0
    printf "%d", int(eta + 0.5)
  }'
}

# Render a positive integer second count as a compact duration: 45m, 1h30m,
# or 2d5h - matching format_time_until_reset's vocabulary so the two strings
# read consistently when shown side by side ("2h15m (1h30m)").
format_duration_compact() {
  local total_seconds="$1"

  [[ "$total_seconds" =~ ^[0-9]+$ ]] || return 1
  if (( total_seconds <= 0 )); then
    printf '%s' '0m'
    return 0
  fi

  local total_minutes days hours minutes
  total_minutes=$(( total_seconds / 60 ))
  if (( total_minutes <= 0 )); then
    printf '%s' '<1m'
    return 0
  fi

  days=$(( total_minutes / (60 * 24) ))
  hours=$(( (total_minutes / 60) % 24 ))
  minutes=$(( total_minutes % 60 ))

  if (( days >= 1 )); then
    printf '%dd%dh' "$days" "$hours"
  elif (( hours >= 1 )); then
    printf '%dh%dm' "$hours" "$minutes"
  else
    printf '%dm' "$minutes"
  fi
}

# Round an epoch to the nearest top-of-hour (1800s = 30min half-window).
# Used by the short-daytime renderers so projection times read cleanly as
# "6pm" instead of "6:47pm" - hour precision is plenty given the inherent
# uncertainty of pacing extrapolation.
round_epoch_to_hour() {
  local epoch="$1"
  [[ "$epoch" =~ ^[0-9]+$ ]] || return 1
  printf '%s' $(( ((epoch + 1800) / 3600) * 3600 ))
}

# Render an epoch as a short calendar marker: "Mon 6pm". Local time, rounded
# to the nearest hour. Uses BSD `date -r` first, falls back to GNU `date -d
# @epoch`. AM/PM is lowercased, ":00" trimmed, internal padding collapsed.
# Returns empty on failure; callers must guard.
format_short_daytime() {
  local epoch="$1"

  [[ "$epoch" =~ ^[0-9]+$ ]] || return 1
  epoch="$(round_epoch_to_hour "$epoch")" || return 1

  local raw
  raw="$(date -r "$epoch" "+%a %l:%M%p" 2>/dev/null || true)"
  if [[ -z "${raw:-}" ]]; then
    raw="$(date -d "@$epoch" "+%a %l:%M%p" 2>/dev/null || true)"
  fi
  [[ -n "${raw:-}" ]] || return 1

  printf '%s' "$raw" | awk '{
    gsub(/AM/, "am"); gsub(/PM/, "pm")
    sub(/:00/, "")
    gsub(/  +/, " ")
    sub(/^ +/, "")
    sub(/ +$/, "")
    print
  }'
}

# Render an epoch with month + day: "May 22 6pm". Rounded to the nearest
# hour. Used for projections beyond ~6 days where bare weekday names become
# ambiguous (weekdays repeat every 7 days). Same lowercase/`:00`-trim post-
# processing as format_short_daytime. Returns empty on failure.
format_short_daytime_with_date() {
  local epoch="$1"

  [[ "$epoch" =~ ^[0-9]+$ ]] || return 1
  epoch="$(round_epoch_to_hour "$epoch")" || return 1

  local raw
  raw="$(date -r "$epoch" "+%b %e %l:%M%p" 2>/dev/null || true)"
  if [[ -z "${raw:-}" ]]; then
    raw="$(date -d "@$epoch" "+%b %e %l:%M%p" 2>/dev/null || true)"
  fi
  [[ -n "${raw:-}" ]] || return 1

  printf '%s' "$raw" | awk '{
    gsub(/AM/, "am"); gsub(/PM/, "pm")
    sub(/:00/, "")
    gsub(/  +/, " ")
    sub(/^ +/, "")
    sub(/ +$/, "")
    print
  }'
}

# Pick the most readable absolute-time format for a projected epoch based on
# how far in the future it is. Within 6 days: "Mon 5pm" (weekday is intuitive
# and unambiguous). Beyond 6 days: "May 22 5pm" (weekdays would repeat and
# become ambiguous, so we switch to month + day).
format_projected_daytime() {
  local epoch="$1" now="$2"

  [[ "$epoch" =~ ^[0-9]+$ ]] || return 1
  [[ "$now"   =~ ^[0-9]+$ ]] || return 1

  local delta
  delta=$(( epoch - now ))

  if (( delta < 6 * 86400 )); then
    format_short_daytime "$epoch"
  else
    format_short_daytime_with_date "$epoch"
  fi
}

# Compose the session reset-view string: "<time-until-reset> (<projected-eta>)".
# The parens echo the percent-view's pacing suffix idiom. Falls through to just
# the time-until-reset when the projection can't be computed (no usage yet,
# missing window/resets_at, just-reset, etc.). Caps absurd ETAs at 99h+ so a
# near-idle window doesn't render "(437h)".
format_session_reset_text() {
  local resets_at="$1" used="$2" window_minutes="$3" now="$4"

  local base
  base="$(format_time_until_reset "$resets_at" "$now")"

  # Guard against provider/schema mixups: a 5-hour session reset should never
  # render as a multi-day weekly reset. If the reset is farther out than the
  # advertised window, treat it as unavailable instead of showing nonsense.
  if [[ "$base" != "--" && "$window_minutes" =~ ^[0-9]+$ ]]; then
    local duration time_until
    duration=$(( window_minutes * 60 ))
    time_until=$(( resets_at - now ))
    if (( duration <= 0 || time_until > duration )); then
      base='--'
    fi
  fi

  printf '%s' "$base"

  [[ "$base" != "--" ]] || return 0

  local eta_seconds
  eta_seconds="$(pace_eta_seconds "$used" "$window_minutes" "$resets_at" "$now" "session")"
  [[ -n "${eta_seconds:-}" ]] || return 0
  [[ "$eta_seconds" =~ ^[0-9]+$ ]] || return 0

  local eta_text
  if (( eta_seconds > 99 * 3600 )); then
    eta_text='99h+'
  else
    eta_text="$(format_duration_compact "$eta_seconds")"
  fi
  [[ -n "${eta_text:-}" ]] || return 0

  printf ' (%s)' "$eta_text"
}

# Compose the weekly reset-view string:
#   "<reset-daytime> in <time-until-reset> (<projected-exhaust-daytime>)".
# When the reset day/time can't be rendered (no resets_at, far-past), fall
# back to just the duration. The projected-exhaust marker switches format
# based on how far out the projection lands (see format_projected_daytime):
# weekday name for the near term, month+day for projections that would
# otherwise be ambiguous as a bare weekday.
format_weekly_reset_text() {
  local resets_at="$1" used="$2" window_minutes="$3" now="$4" mode="${5:-weekly}"

  local time_until reset_daytime
  time_until="$(format_time_until_reset "$resets_at" "$now")"

  if [[ "$time_until" != "--" && "$window_minutes" =~ ^[0-9]+$ ]]; then
    local duration delta
    duration=$(( window_minutes * 60 ))
    delta=$(( resets_at - now ))
    if (( duration <= 0 || delta > duration )); then
      time_until='--'
    fi
  fi

  reset_daytime=''
  if [[ "$resets_at" =~ ^[0-9]+$ ]] && (( resets_at > now )); then
    reset_daytime="$(format_short_daytime "$resets_at" 2>/dev/null || true)"
  fi

  if [[ -n "${reset_daytime:-}" && "$time_until" != "--" ]]; then
    printf '%s in %s' "$reset_daytime" "$time_until"
  else
    printf '%s' "$time_until"
  fi

  [[ "$time_until" != "--" ]] || return 0

  local eta_seconds
  eta_seconds="$(pace_eta_seconds "$used" "$window_minutes" "$resets_at" "$now" "$mode")"
  [[ -n "${eta_seconds:-}" ]] || return 0
  [[ "$eta_seconds" =~ ^[0-9]+$ ]] || return 0

  local eta_epoch eta_daytime
  eta_epoch=$(( now + eta_seconds ))
  eta_daytime="$(format_projected_daytime "$eta_epoch" "$now" 2>/dev/null || true)"
  [[ -n "${eta_daytime:-}" ]] || return 0

  printf ' (%s)' "$eta_daytime"
}

load_print_context() {
  PRINT_VIEW_BASELINE='percent'
  PRINT_PREVIEW_UNTIL='0'
  PRINT_VIEW_SCOPE='both'
  PRINT_DEBUG_COUNTER='0'

  command -v tmux >/dev/null 2>&1 || return 0

  local opts
  opts="$(tmux show-options -g 2>/dev/null || true)"
  [[ -n "${opts:-}" ]] || return 0

  local line key val
  while IFS= read -r line; do
    case "$line" in
      "@codexbar_view "*)
        val="${line#@codexbar_view }"
        val="${val#\"}"; val="${val%\"}"
        case "$val" in
          percent|reset) PRINT_VIEW_BASELINE="$val" ;;
        esac
        ;;
      "@codexbar_reset_preview_until "*)
        val="${line#@codexbar_reset_preview_until }"
        val="${val#\"}"; val="${val%\"}"
        PRINT_PREVIEW_UNTIL="$val"
        ;;
      "@codexbar_reset_view_scope "*)
        val="${line#@codexbar_reset_view_scope }"
        val="${val#\"}"; val="${val%\"}"
        case "$val" in
          session|weekly|both) PRINT_VIEW_SCOPE="$val" ;;
        esac
        ;;
      "$debug_update_counter_opt "*)
        val="${line#$debug_update_counter_opt }"
        val="${val#\"}"; val="${val%\"}"
        PRINT_DEBUG_COUNTER="$val"
        ;;
    esac
  done <<<"$opts"
}

effective_view_from_context() {
  local mode="${1:-}"

  local until now
  until="$(parse_int_with_default "$PRINT_PREVIEW_UNTIL" 0)"
  now="$(now_epoch)"

  local preview_active=0
  if (( until == -1 || until > now )); then
    preview_active=1
  fi

  if (( preview_active == 0 )); then
    printf '%s' "$PRINT_VIEW_BASELINE"
    return 0
  fi

  case "$PRINT_VIEW_SCOPE" in
    both)
      printf '%s' 'reset'
      ;;
    session)
      [[ "$mode" == 'session' ]] && printf '%s' 'reset' || printf '%s' "$PRINT_VIEW_BASELINE"
      ;;
    weekly)
      # the scoped window is a weekly-window limit, so it follows the weekly scope.
      [[ "$mode" == 'weekly' || "$mode" == 'scoped' ]] && printf '%s' 'reset' || printf '%s' "$PRINT_VIEW_BASELINE"
      ;;
  esac
}

# Load one provider's fields out of the cache. Defaults to the display
# provider; pass a name to read another block.
#
# THE SELECTOR IS A PREFIX, NOT A RESHAPE. `(.providers[$p] // …)` picks the
# block and the field list below is unchanged — same names, same order, same
# @tsv — because a provider block carries exactly the key names the top level
# does. That is also what keeps cua-notch's invariant check on this line
# meaningful: it reads the field list to learn which window families exist,
# and the families are a property of a block, not of the root.
load_cache_fields() {
  local provider="${1:-$DISPLAY_PROVIDER}"

  CACHE_STATE='ok'
  CACHE_SESSION_TEXT=''
  CACHE_WEEKLY_TEXT=''
  CACHE_SESSION_COLOR=''
  CACHE_WEEKLY_COLOR=''
  CACHE_SESSION_RESETS=''
  CACHE_WEEKLY_RESETS=''
  CACHE_SESSION_USED=''
  CACHE_WEEKLY_USED=''
  CACHE_SESSION_WINDOW_MINUTES=''
  CACHE_WEEKLY_WINDOW_MINUTES=''
  CACHE_SCOPED_TEXT=''
  CACHE_SCOPED_COLOR=''
  CACHE_SCOPED_RESETS=''
  CACHE_SCOPED_USED=''
  CACHE_SCOPED_WINDOW_MINUTES=''
  CACHE_SCOPED_LABEL=''

  [[ -f "$CACHE_FILE" ]] || return 0
  command -v jq >/dev/null 2>&1 || return 0

  # Tab is IFS *whitespace*, so `IFS=$'\t' read` collapses runs of tabs and
  # drops empty fields, shifting every later field left. That happens for real:
  # the endpoint reports session_resets_at as null whenever no 5-hour window is
  # open, which slid the weekly reset into CACHE_SESSION_RESETS and pushed the
  # tail fields off the end. Use a non-whitespace separator (US, \037) so empty
  # fields survive; @tsv escapes any literal tab in a value, so this is lossless.
  local parsed
  parsed="$(jq -r --arg p "$provider" --arg primary "$PROVIDER_PRIMARY" \
    '((.providers[$p]? // (if $p == $primary then . else {} end)) // {}) | [.state//"ok", .session_text//"", .weekly_text//"", .session_color//"", .weekly_color//"", .session_resets_at//"", .weekly_resets_at//"", .session_used//"", .weekly_used//"", .session_window_minutes//"", .weekly_window_minutes//"", .scoped_text//"", .scoped_color//"", .scoped_resets_at//"", .scoped_used//"", .scoped_window_minutes//"", .scoped_label//""] | @tsv' \
    "$CACHE_FILE" 2>/dev/null | tr '\t' '\037' || true)"
  [[ -n "$parsed" ]] || return 0
  IFS=$'\037' read -r CACHE_STATE CACHE_SESSION_TEXT CACHE_WEEKLY_TEXT CACHE_SESSION_COLOR CACHE_WEEKLY_COLOR CACHE_SESSION_RESETS CACHE_WEEKLY_RESETS CACHE_SESSION_USED CACHE_WEEKLY_USED CACHE_SESSION_WINDOW_MINUTES CACHE_WEEKLY_WINDOW_MINUTES CACHE_SCOPED_TEXT CACHE_SCOPED_COLOR CACHE_SCOPED_RESETS CACHE_SCOPED_USED CACHE_SCOPED_WINDOW_MINUTES CACHE_SCOPED_LABEL <<<"$parsed"
}

render_text_for_mode() {
  local mode="$1" view="$2" debug_suffix="$3"
  local out=''

  if [[ "$CACHE_STATE" == "auth_required" ]]; then
    printf '%s%s' "$AUTH_REQUIRED_TEXT" "$debug_suffix"
    return 0
  fi

  if [[ "$view" == "reset" ]]; then
    local now resets_at='' used='' window_minutes=''
    now="$(now_epoch)"
    case "$mode" in
      session)
        resets_at="$CACHE_SESSION_RESETS"
        used="$CACHE_SESSION_USED"
        window_minutes="$CACHE_SESSION_WINDOW_MINUTES"
        if [[ -z "$resets_at" && "$used" == "0" ]]; then
          # No 5-hour window open, so there is no reset to count down to.
          out='idle'
        else
          out="$(format_session_reset_text "$resets_at" "$used" "$window_minutes" "$now")"
        fi
        ;;
      weekly)
        resets_at="$CACHE_WEEKLY_RESETS"
        used="$CACHE_WEEKLY_USED"
        window_minutes="$CACHE_WEEKLY_WINDOW_MINUTES"
        out="$(format_weekly_reset_text "$resets_at" "$used" "$window_minutes" "$now")"
        ;;
      scoped)
        resets_at="$CACHE_SCOPED_RESETS"
        used="$CACHE_SCOPED_USED"
        window_minutes="$CACHE_SCOPED_WINDOW_MINUTES"
        if [[ -z "$used" ]]; then
          # The provider reported no narrower scoped window at all.
          out='n/a'
        else
          out="$(format_weekly_reset_text "$resets_at" "$used" "$window_minutes" "$now" "scoped")"
        fi
        ;;
    esac
  else
    case "$mode" in
      session) out="$CACHE_SESSION_TEXT" ;;
      weekly)  out="$CACHE_WEEKLY_TEXT" ;;
      scoped)  out="$CACHE_SCOPED_TEXT" ;;
    esac
    if [[ -z "$out" ]]; then
      out="--%"
    else
      out="$(strip_legacy_label_prefix "$out")"
    fi
  fi

  printf '%s%s' "$out" "$debug_suffix"
}

# The status line renders ONE provider. Everything below is that provider's
# numbers; which provider it is comes from @codexbar_display_provider and is
# published alongside them as @codex_provider / @codex_provider_label, so a
# status-line format can say whose numbers these are without asking the
# option back.
#
# The @codex_fable_* options are still published as aliases of the scoped
# ones. A tmux server that has not re-sourced tmux.conf since the rename is
# still running the old module definitions, and a status line that silently
# empties is exactly the failure this rename was meant to stop having.
publish_text_and_alias() {
  local opt="${1:-}" alias_opt="${2:-}" value="${3:-}"

  tmux set-option -gq "$opt" "$value" >/dev/null 2>&1 || true
  [[ -n "${alias_opt:-}" ]] && tmux set-option -gq "$alias_opt" "$value" >/dev/null 2>&1 || true
  return 0
}

# "Fable" -> "F:", "gpt-reserve" -> "G:". Empty for an unnamed window, which
# leaves the module's own default in place rather than publishing a colon.
icon_for_label() {
  local label="${1:-}"
  [[ -n "${label:-}" ]] || { printf '%s' ''; return 0; }

  local first="${label:0:1}"
  [[ "$first" =~ ^[A-Za-z0-9]$ ]] || { printf '%s' ''; return 0; }

  printf '%s:' "$(printf '%s' "$first" | tr '[:lower:]' '[:upper:]')"
}

publish_to_tmux_opts() {
  command -v tmux >/dev/null 2>&1 || return 0

  load_print_context
  load_cache_fields "$DISPLAY_PROVIDER"

  local debug_suffix=''
  if (( CODEXBAR_USAGE_DEBUG != 0 )); then
    local c="$PRINT_DEBUG_COUNTER"
    [[ "$c" =~ ^[0-9]+$ ]] || c=0
    debug_suffix=" d${c}"
  fi

  local session_view weekly_view scoped_view session_text weekly_text scoped_text
  session_view="$(effective_view_from_context session)"
  weekly_view="$(effective_view_from_context weekly)"
  scoped_view="$(effective_view_from_context scoped)"
  session_text="$(render_text_for_mode session "$session_view" "$debug_suffix")"
  weekly_text="$(render_text_for_mode weekly  "$weekly_view"  "$debug_suffix")"
  scoped_text="$(render_text_for_mode scoped  "$scoped_view"  "$debug_suffix")"

  publish_text_and_alias @codex_session_text '' "$session_text"
  publish_text_and_alias @codex_weekly_text  '' "$weekly_text"
  publish_text_and_alias @codex_scoped_text  @codex_fable_text "$scoped_text"

  tmux set-option -gq @codex_provider "$DISPLAY_PROVIDER" >/dev/null 2>&1 || true
  tmux set-option -gq @codex_provider_label "$(provider_label_for "$DISPLAY_PROVIDER")" >/dev/null 2>&1 || true
  tmux set-option -gq @codex_scoped_icon "$(icon_for_label "$CACHE_SCOPED_LABEL")" >/dev/null 2>&1 || true

  if [[ "$CACHE_STATE" == "auth_required" ]]; then
    publish_text_and_alias @codex_session_color '' "$AUTH_REQUIRED_COLOR"
    publish_text_and_alias @codex_weekly_color  '' "$AUTH_REQUIRED_COLOR"
    publish_text_and_alias @codex_scoped_color  @codex_fable_color "$AUTH_REQUIRED_COLOR"
  else
    if [[ -n "$CACHE_SESSION_COLOR" ]]; then
      publish_text_and_alias @codex_session_color '' "$CACHE_SESSION_COLOR"
    fi
    if [[ -n "$CACHE_WEEKLY_COLOR" ]]; then
      publish_text_and_alias @codex_weekly_color '' "$CACHE_WEEKLY_COLOR"
    fi
    if [[ -n "$CACHE_SCOPED_COLOR" ]]; then
      publish_text_and_alias @codex_scoped_color @codex_fable_color "$CACHE_SCOPED_COLOR"
    fi
  fi
}

print_value() {
  local mode="$1" view debug_suffix=''

  load_print_context
  load_cache_fields "$DISPLAY_PROVIDER"

  view="$(effective_view_from_context "$mode")"

  if (( CODEXBAR_USAGE_DEBUG != 0 )); then
    local c="$PRINT_DEBUG_COUNTER"
    [[ "$c" =~ ^[0-9]+$ ]] || c=0
    debug_suffix=" d${c}"
  fi

  if command -v tmux >/dev/null 2>&1; then
    if [[ "$CACHE_STATE" == "auth_required" ]]; then
      publish_text_and_alias @codex_session_color '' "$AUTH_REQUIRED_COLOR"
      publish_text_and_alias @codex_weekly_color  '' "$AUTH_REQUIRED_COLOR"
      publish_text_and_alias @codex_scoped_color  @codex_fable_color "$AUTH_REQUIRED_COLOR"
    else
      case "$mode" in
        session) [[ -n "$CACHE_SESSION_COLOR" ]] && publish_text_and_alias @codex_session_color '' "$CACHE_SESSION_COLOR" || true ;;
        weekly)  [[ -n "$CACHE_WEEKLY_COLOR" ]]  && publish_text_and_alias @codex_weekly_color  '' "$CACHE_WEEKLY_COLOR"  || true ;;
        scoped)  [[ -n "$CACHE_SCOPED_COLOR" ]]  && publish_text_and_alias @codex_scoped_color  @codex_fable_color "$CACHE_SCOPED_COLOR" || true ;;
      esac
    fi
  fi

  printf '%s\n' "$(render_text_for_mode "$mode" "$view" "$debug_suffix")"
}

lockdir_mtime_epoch() {
  local path="${1:-}"
  [[ -n "${path:-}" ]] || return 1

  local mtime
  mtime="$(stat -f %m "$path" 2>/dev/null || true)"
  if [[ "${mtime:-}" =~ ^[0-9]+$ ]]; then
    printf '%s' "$mtime"
    return 0
  fi

  mtime="$(stat -c %Y "$path" 2>/dev/null || true)"
  [[ "${mtime:-}" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "$mtime"
}

clear_stale_lock_if_needed() {
  [[ -d "$LOCKDIR" ]] || return 0

  local started_at now age recorded_pid pid_alive
  recorded_pid="$(cat "$LOCKDIR/pid" 2>/dev/null || true)"
  pid_alive=0
  if [[ "${recorded_pid:-}" =~ ^[0-9]+$ ]] && (( recorded_pid > 0 )); then
    if kill -0 "$recorded_pid" 2>/dev/null; then
      pid_alive=1
    fi
  fi

  if [[ -f "$LOCKDIR/started_at" ]]; then
    started_at="$(cat "$LOCKDIR/started_at" 2>/dev/null || true)"
  else
    now="$(now_epoch)"
    local mtime
    mtime="$(lockdir_mtime_epoch "$LOCKDIR" 2>/dev/null || true)"
    if [[ "${mtime:-}" =~ ^[0-9]+$ ]]; then
      age=$(( now - mtime ))
      if (( age > 2 )); then
        rm -rf "$LOCKDIR" 2>/dev/null || true
      fi
    fi
    return 0
  fi

  now="$(now_epoch)"
  if [[ "$started_at" =~ ^[0-9]+$ ]]; then
    age=$(( now - started_at ))
  else
    rm -rf "$LOCKDIR" 2>/dev/null || true
    return 0
  fi

  if [[ "${recorded_pid:-}" =~ ^[0-9]+$ ]] && (( recorded_pid > 0 )) && (( pid_alive == 0 )); then
    log_warn "lock: clearing (recorded pid=${recorded_pid} not alive, age=${age})"
    rm -rf "$LOCKDIR" 2>/dev/null || true
    return 0
  fi

  if (( age > LOCK_STALE_SECONDS )); then
    if (( pid_alive == 1 )); then
      local cmdline
      cmdline="$(ps -p "$recorded_pid" -o command= 2>/dev/null || true)"
      if [[ "$cmdline" == *"codexbar-usage-status.sh"* ]]; then
        # Do not kill a live tmux run-shell worker just because wall-clock time
        # advanced past the stale threshold. After laptop sleep/wake, a valid
        # refresh can look "old" even though it was merely suspended; killing it
        # makes tmux report the background command as signal 9 / exit 137.
        log_warn "lock: worker still alive pid=${recorded_pid} age=${age}s; keeping lock"
        return 0
      else
        log_warn "lock: pid ${recorded_pid} reused by unrelated process; skipping kill"
        return 0
      fi
    fi
    rm -rf "$LOCKDIR" 2>/dev/null || true
  fi
}

try_acquire_lock() {
  local pid_value="${1:-STARTING}"

  # mkdir is the atomic acquisition primitive; do NOT clear before the first
  # attempt or a concurrent acquirer's live lock can be deleted out from under
  # it (clear-then-mkdir TOCTOU). Only on contention do we clear a genuinely
  # stale lock and retry once. Write pid before started_at so clear_stale_lock_
  # if_needed's pid-alive guard starts protecting the lock as early as possible.
  if mkdir "$LOCKDIR" 2>/dev/null; then
    printf '%s\n' "$pid_value" >"$LOCKDIR/pid" 2>/dev/null || { rm -rf "$LOCKDIR" 2>/dev/null || true; return 1; }
    printf '%s\n' "$(now_epoch)" >"$LOCKDIR/started_at" 2>/dev/null || { rm -rf "$LOCKDIR" 2>/dev/null || true; return 1; }
    return 0
  fi

  clear_stale_lock_if_needed
  mkdir "$LOCKDIR" 2>/dev/null || return 1
  printf '%s\n' "$pid_value" >"$LOCKDIR/pid" 2>/dev/null || { rm -rf "$LOCKDIR" 2>/dev/null || true; return 1; }
  printf '%s\n' "$(now_epoch)" >"$LOCKDIR/started_at" 2>/dev/null || { rm -rf "$LOCKDIR" 2>/dev/null || true; return 1; }
  return 0
}

release_lock() {
  rm -rf "$LOCKDIR" 2>/dev/null || true
}

spawn_background_refresh_locked() {
  local script="$0"
  if [[ "$script" != /* ]]; then
    script="$(cd -- "$(dirname -- "$script")" && pwd)/$(basename -- "$script")"
  fi

  if ! try_acquire_lock; then
    log_debug "spawn: skip (lock busy)"
    return 0
  fi

  if command -v tmux >/dev/null 2>&1; then
    log_debug "spawn: tmux run-shell -b"
    if tmux run-shell -b "CODEXBAR_USAGE_LOCK_HELD=1 CODEXBAR_USAGE_LOCKDIR=\"$LOCKDIR\" \"$script\" --refresh >/dev/null 2>&1" >/dev/null 2>&1; then
      log_debug "spawn: tmux ok"
      return 0
    fi

    log_debug "spawn: tmux failed"
    release_lock
    return 1
  fi

  log_debug "spawn: nohup"
  nohup env CODEXBAR_USAGE_LOCK_HELD=1 CODEXBAR_USAGE_LOCKDIR="$LOCKDIR" "$script" --refresh >/dev/null 2>&1 &
  local nohup_status=$?
  if (( nohup_status == 0 )); then
    log_debug "spawn: nohup ok"
    return 0
  fi

  log_debug "spawn: nohup failed status=${nohup_status}"
  release_lock
  return 1
}

ensure_codex_cli_in_path() {
  command -v codex >/dev/null 2>&1 && return 0

  local bin

  local had_nullglob=0
  if shopt -q nullglob; then
    had_nullglob=1
  fi
  shopt -s nullglob
  local candidate
  for candidate in "$HOME/.nvm/versions/node/"*/bin/codex; do
    if [[ -x "$candidate" ]]; then
      bin="${candidate%/codex}"
      PATH="$bin:$PATH"
      export PATH
      break
    fi
  done
  if (( had_nullglob == 0 )); then
    shopt -u nullglob
  fi

  command -v codex >/dev/null 2>&1 && return 0

  if [[ -x "$HOME/.bun/bin/codex" ]]; then
    PATH="$HOME/.bun/bin:$PATH"
    export PATH
    return 0
  fi

  if [[ -x "$HOME/.local/bin/codex" ]]; then
    PATH="$HOME/.local/bin:$PATH"
    export PATH
    return 0
  fi

  return 0
}

ensure_claude_cli_in_path() {
  command -v claude >/dev/null 2>&1 && return 0

  local candidate bin
  for candidate in \
    "$HOME/.local/bin/claude" \
    "$HOME/.bun/bin/claude" \
    "$HOME/.nvm/versions/node/"*/bin/claude; do
    if [[ -x "$candidate" ]]; then
      bin="${candidate%/claude}"
      PATH="$bin:$PATH"
      export PATH
      return 0
    fi
  done

  return 1
}

CLAUDE_OAUTH_KEYCHAIN_SERVICE='Claude Code-credentials'
CLAUDE_OAUTH_REFRESH_LOCKDIR="${CACHE_DIR}/oauth-refresh.lock"
CLAUDE_OAUTH_TOKEN_URL='https://platform.claude.com/v1/oauth/token'
CLAUDE_OAUTH_CLIENT_ID='9d1c250a-e61b-44d9-88ed-5944d1962f5e'
CLAUDE_OAUTH_REFRESH_REAUTH_REQUIRED=0

read_claude_oauth_keychain_blob() {
  command -v security >/dev/null 2>&1 || return 1

  local raw
  raw="$(security find-generic-password -s "$CLAUDE_OAUTH_KEYCHAIN_SERVICE" -w 2>/dev/null || true)"
  [[ -n "${raw:-}" ]] || return 1

  printf '%s' "$raw"
}

read_claude_oauth_access_token() {
  command -v jq >/dev/null 2>&1 || return 1

  local raw token
  raw="$(read_claude_oauth_keychain_blob 2>/dev/null || true)"
  [[ -n "${raw:-}" ]] || return 1

  token="$(printf '%s' "$raw" | jq -er '.claudeAiOauth.accessToken' 2>/dev/null || true)"
  [[ -n "${token:-}" && "$token" == sk-ant-* ]] || return 1

  printf '%s' "$token"
}

read_claude_oauth_refresh_token() {
  command -v jq >/dev/null 2>&1 || return 1

  local raw token
  raw="$(read_claude_oauth_keychain_blob 2>/dev/null || true)"
  [[ -n "${raw:-}" ]] || return 1

  token="$(printf '%s' "$raw" | jq -er '.claudeAiOauth.refreshToken' 2>/dev/null || true)"
  [[ -n "${token:-}" && "$token" == sk-ant-ort* ]] || return 1

  printf '%s' "$token"
}

# Returns 0 (true) when the keychain access token is missing an expiry, already
# expired, or within 120s of expiring. .claudeAiOauth.expiresAt is epoch ms.
claude_oauth_access_token_is_expired() {
  command -v jq >/dev/null 2>&1 || return 1

  local raw exp_ms now_ms
  raw="$(read_claude_oauth_keychain_blob 2>/dev/null || true)"
  [[ -n "${raw:-}" ]] || return 1

  exp_ms="$(printf '%s' "$raw" | jq -er '.claudeAiOauth.expiresAt' 2>/dev/null || true)"
  [[ "$exp_ms" =~ ^[0-9]+$ ]] || return 0   # unknown expiry -> assume it needs refreshing

  now_ms=$(( $(now_epoch) * 1000 ))
  (( exp_ms <= now_ms + 120000 ))
}

# Atomically updates the Claude Code keychain entry with new access/refresh tokens
# while preserving any other fields (subscriptionType, scopes, rateLimitTier, etc.).
# Verifies the refresh token in the keychain still matches `expected_old_refresh`
# right before writing - if Claude Code or another script already rotated it, we
# bail out (prevents overwriting a newer rotation with our own stale tokens).
write_claude_oauth_credentials() {
  local new_access="$1" new_refresh="$2" new_expires_at_ms="$3" expected_old_refresh="$4"

  command -v security >/dev/null 2>&1 || return 1
  command -v jq >/dev/null 2>&1 || return 1

  [[ -n "$new_access" && "$new_access" == sk-ant-oat* ]] || return 1
  [[ -n "$new_refresh" && "$new_refresh" == sk-ant-ort* ]] || return 1
  [[ "$new_expires_at_ms" =~ ^[0-9]+$ ]] || return 1

  local current_blob current_refresh updated_blob
  current_blob="$(read_claude_oauth_keychain_blob 2>/dev/null || true)"
  if [[ -z "${current_blob:-}" ]]; then
    log_warn "oauth-refresh: keychain blob unavailable for write-verify"
    return 1
  fi

  current_refresh="$(printf '%s' "$current_blob" | jq -er '.claudeAiOauth.refreshToken' 2>/dev/null || true)"
  if [[ "$current_refresh" != "$expected_old_refresh" ]]; then
    log_info "oauth-refresh: keychain rotated by another writer; skipping our write"
    return 2
  fi

  updated_blob="$(
    printf '%s' "$current_blob" | jq -c \
      --arg at "$new_access" \
      --arg rt "$new_refresh" \
      --argjson exp "$new_expires_at_ms" \
      '.claudeAiOauth.accessToken = $at
       | .claudeAiOauth.refreshToken = $rt
       | .claudeAiOauth.expiresAt = $exp' 2>/dev/null || true
  )"
  [[ -n "$updated_blob" ]] || { log_warn "oauth-refresh: jq merge failure"; return 1; }

  if ! security add-generic-password -U \
       -s "$CLAUDE_OAUTH_KEYCHAIN_SERVICE" \
       -a "$USER" \
       -w "$updated_blob" >/dev/null 2>&1; then
    log_warn "oauth-refresh: security write failed"
    return 1
  fi

  local verify_blob verify_refresh
  verify_blob="$(read_claude_oauth_keychain_blob 2>/dev/null || true)"
  verify_refresh="$(printf '%s' "$verify_blob" | jq -er '.claudeAiOauth.refreshToken' 2>/dev/null || true)"
  if [[ "$verify_refresh" != "$new_refresh" ]]; then
    log_warn "oauth-refresh: write verification failed (read-back mismatch)"
    return 1
  fi

  return 0
}

try_acquire_refresh_lock() {
  mkdir -p "$CACHE_DIR" 2>/dev/null || true

  if [[ -d "$CLAUDE_OAUTH_REFRESH_LOCKDIR" ]]; then
    local started
    started="$(cat "$CLAUDE_OAUTH_REFRESH_LOCKDIR/started_at" 2>/dev/null || echo 0)"
    # Guard arithmetic against a corrupt/partial lock file: a non-numeric token
    # in $(( )) is treated as a variable name and, under set -u, aborts the
    # whole script (and leaves the lock held). Treat garbage as "stale" (age
    # from epoch 0) so the rm below reclaims it.
    [[ "$started" =~ ^[0-9]+$ ]] || started=0
    local age=$(( $(now_epoch) - started ))
    if (( age > 60 )); then
      rm -rf "$CLAUDE_OAUTH_REFRESH_LOCKDIR" 2>/dev/null || true
    fi
  fi

  mkdir "$CLAUDE_OAUTH_REFRESH_LOCKDIR" 2>/dev/null || return 1
  printf '%s\n' "$(now_epoch)" >"$CLAUDE_OAUTH_REFRESH_LOCKDIR/started_at" 2>/dev/null || true
  return 0
}

release_refresh_lock() {
  rm -rf "$CLAUDE_OAUTH_REFRESH_LOCKDIR" 2>/dev/null || true
}

# Returns 0 on success (new tokens in keychain ready to use), non-zero on failure.
# On success, the caller can re-read the access token from keychain and retry.
try_oauth_token_refresh() {
  command -v curl >/dev/null 2>&1 || return 1
  command -v jq >/dev/null 2>&1 || return 1

  if ! try_acquire_refresh_lock; then
    log_info "oauth-refresh: another instance is refreshing; skipping"
    return 1
  fi

  local current_refresh
  current_refresh="$(read_claude_oauth_refresh_token 2>/dev/null || true)"
  if [[ -z "${current_refresh:-}" ]]; then
    log_warn "oauth-refresh: no refresh token in keychain"
    release_refresh_lock
    return 1
  fi

  local body_file
  body_file="$(umask 077 && mktemp "${CACHE_DIR}/oauth.body.XXXXXX")" || { release_refresh_lock; return 1; }
  CODEXBAR_TMP_FILES+=("$body_file")

  {
    printf 'grant_type=refresh_token'
    printf '&client_id=%s' "$CLAUDE_OAUTH_CLIENT_ID"
    printf '&refresh_token='
    printf '%s' "$current_refresh"
  } >"$body_file" 2>/dev/null || { rm -f "$body_file"; release_refresh_lock; return 1; }

  local response rc
  response="$(curl -sS --max-time 10 \
    -X POST "$CLAUDE_OAUTH_TOKEN_URL" \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    -H 'User-Agent: claude-cli/1.0' \
    --data-binary @"$body_file" 2>/dev/null)"
  rc=$?
  rm -f "$body_file" 2>/dev/null

  if (( rc != 0 )); then
    log_warn "oauth-refresh: curl failed rc=${rc}"
    release_refresh_lock
    return 1
  fi

  if printf '%s' "$response" | jq -e '.error' >/dev/null 2>&1; then
    local err_type err_msg
    err_type="$(printf '%s' "$response" | jq -r '
      (try .error.type catch null) //
      (if (.error | type) == "string" then .error else "unknown" end)
    ' 2>/dev/null)"
    err_msg="$(printf '%s' "$response" | jq -r '
      (try .error.message catch null) //
      .error_description //
      ""
    ' 2>/dev/null)"
    err_msg="${err_msg//$'\n'/ }"
    case "$err_type" in
      invalid_grant|invalid_token|authentication_error)
        CLAUDE_OAUTH_REFRESH_REAUTH_REQUIRED=1
        ;;
    esac
    log_warn "oauth-refresh: endpoint returned error type=${err_type} msg=${err_msg}"
    release_refresh_lock
    return 1
  fi

  local new_access new_refresh expires_in
  new_access="$(printf '%s' "$response" | jq -er '.access_token' 2>/dev/null || true)"
  new_refresh="$(printf '%s' "$response" | jq -er '.refresh_token' 2>/dev/null || true)"
  expires_in="$(printf '%s' "$response" | jq -er '.expires_in' 2>/dev/null || echo 28800)"

  if [[ -z "$new_access" || "$new_access" != sk-ant-oat* ]] \
     || [[ -z "$new_refresh" || "$new_refresh" != sk-ant-ort* ]]; then
    log_warn "oauth-refresh: malformed response from token endpoint"
    release_refresh_lock
    return 1
  fi

  local new_expires_at_ms
  new_expires_at_ms=$(( ($(now_epoch) + expires_in) * 1000 ))

  local write_rc
  write_claude_oauth_credentials "$new_access" "$new_refresh" "$new_expires_at_ms" "$current_refresh"
  write_rc=$?
  release_refresh_lock

  if (( write_rc == 2 )); then
    return 0
  fi
  if (( write_rc != 0 )); then
    log_warn "oauth-refresh: keychain write failed; new tokens lost (will retry next failure)"
    return 1
  fi

  log_info "oauth-refresh: success (token rotated, expires in ${expires_in}s)"
  return 0
}

fetch_claude_oauth_usage_json() {
  local token="$1"
  command -v curl >/dev/null 2>&1 || return 1

  local cfg result rc
  cfg="$(umask 077 && mktemp "${CACHE_DIR}/curl.cfg.XXXXXX")" || return 1
  CODEXBAR_TMP_FILES+=("$cfg")

  {
    printf 'silent\n'
    printf 'show-error\n'
    printf 'max-time = %s\n' "$WEB_TIMEOUT_SECONDS"
    printf 'header = "Authorization: Bearer %s"\n' "$token"
    printf 'header = "anthropic-beta: oauth-2025-04-20"\n'
    printf 'header = "Content-Type: application/json"\n'
    printf 'url = "https://api.anthropic.com/api/oauth/usage"\n'
  } >"$cfg" 2>/dev/null || { rm -f "$cfg" 2>/dev/null; return 1; }

  result="$(curl --config "$cfg" 2>/dev/null)"
  rc=$?
  rm -f "$cfg" 2>/dev/null
  (( rc == 0 )) || return 1
  printf '%s' "$result"
}

# One fetch's worth of output, in the shape every provider answers in. The
# three families are structural — session (the short rolling window), weekly,
# and scoped (whatever narrower cap the provider also enforces, if any) — and
# each carries the same five facts, so refresh_cache renders any provider
# through one code path.
FETCH_SESSION_USED=''
FETCH_WEEKLY_USED=''
FETCH_SCOPED_USED=''
FETCH_SESSION_WINDOW_MINUTES=''
FETCH_WEEKLY_WINDOW_MINUTES=''
FETCH_SCOPED_WINDOW_MINUTES=''
FETCH_SESSION_RESETS_AT=''
FETCH_WEEKLY_RESETS_AT=''
FETCH_SCOPED_RESETS_AT=''
FETCH_SESSION_LABEL='Session'
FETCH_WEEKLY_LABEL='Weekly'
FETCH_SCOPED_LABEL=''
FETCH_SESSION_SEVERITY='normal'
FETCH_WEEKLY_SEVERITY='normal'
FETCH_SCOPED_SEVERITY='normal'
FETCH_SESSION_LOCKED=0
FETCH_WEEKLY_LOCKED=0
FETCH_SCOPED_LOCKED=0
# What spent the window, as the provider itself breaks it down: a JSON array
# of {key, display_name, percent}. Published in the provider's block so a
# reader gets it without opening the raw file. "[]" where the provider has no
# such breakdown, which is every provider but Claude today.
FETCH_BREAKDOWN_JSON='[]'
FETCH_AUTH_REQUIRED=0
# The endpoint answered, but with rate_limit_error. Not a broken fetch: the
# budget is per account token and shared with every Claude Code session on it,
# so this happens under heavy use even at one request per poll interval.
FETCH_RATE_LIMITED=0

reset_fetch_outputs() {
  FETCH_SESSION_USED=''
  FETCH_WEEKLY_USED=''
  FETCH_SCOPED_USED=''
  FETCH_SESSION_WINDOW_MINUTES=''
  FETCH_WEEKLY_WINDOW_MINUTES=''
  FETCH_SCOPED_WINDOW_MINUTES=''
  FETCH_SESSION_RESETS_AT=''
  FETCH_WEEKLY_RESETS_AT=''
  FETCH_SCOPED_RESETS_AT=''
  FETCH_SESSION_LABEL='Session'
  FETCH_WEEKLY_LABEL='Weekly'
  FETCH_SCOPED_LABEL=''
  FETCH_SESSION_SEVERITY='normal'
  FETCH_WEEKLY_SEVERITY='normal'
  FETCH_SCOPED_SEVERITY='normal'
  FETCH_SESSION_LOCKED=0
  FETCH_WEEKLY_LOCKED=0
  FETCH_SCOPED_LOCKED=0
  FETCH_BREAKDOWN_JSON='[]'
  FETCH_AUTH_REQUIRED=0
  FETCH_RATE_LIMITED=0
  CLAUDE_OAUTH_REFRESH_REAUTH_REQUIRED=0
}

# Persist a provider's raw payload next to the summary, for consumers that
# want more than the three percentages. Claude's keeps the unsuffixed name
# (usage-raw.json) because that name is part of the published contract.
persist_raw_payload() {
  local provider="${1:-}" payload="${2:-}"

  [[ -n "${provider:-}" && -n "${payload:-}" ]] || return 0

  local target tmp
  target="$(raw_file_for "$provider")"
  tmp="$(umask 077 && mktemp "${target}.tmp.XXXXXX" 2>/dev/null)" || return 0
  if printf '%s\n' "$payload" >"$tmp" 2>/dev/null; then
    mv -f "$tmp" "$target" 2>/dev/null || rm -f "$tmp" 2>/dev/null || true
  else
    rm -f "$tmp" 2>/dev/null || true
  fi
  return 0
}

fetch_via_codexbar_codex() {
  reset_fetch_outputs

  if ! command -v codexbar >/dev/null 2>&1; then
    log_warn "refresh[codex]: missing tool codexbar"
    return 1
  fi

  ensure_codex_cli_in_path

  local fetch_out fetch_err fetch_status stderr_file
  stderr_file="$(mktemp "${CACHE_DIR}/codexbar.stderr.XXXXXX")"
  CODEXBAR_TMP_FILES+=("$stderr_file")

  set +e
  fetch_out="$(codexbar --provider codex --format json --json-only --web-timeout "$WEB_TIMEOUT_SECONDS" 2>"$stderr_file")"
  local fetch_status=$?
  set -e
  fetch_err="$(cat "$stderr_file" 2>/dev/null || true)"

  if (( fetch_status != 0 )); then
    if [[ "$fetch_out" == *"Unknown option --json-only"* || "$fetch_err" == *"Unknown option --json-only"* ]]; then
      : >"$stderr_file" 2>/dev/null || true
      set +e
      fetch_out="$(codexbar --provider codex --format json --web-timeout "$WEB_TIMEOUT_SECONDS" 2>"$stderr_file")"
      fetch_status=$?
      set -e
      fetch_err="$(cat "$stderr_file" 2>/dev/null || true)"
    fi
  fi

  rm -f "$stderr_file" 2>/dev/null || true

  if (( fetch_status != 0 )); then
    log_warn_trunc "refresh[codex]: codexbar nonzero status=${fetch_status} err=${fetch_err} out=${fetch_out}" 300
    return 1
  fi

  local normalized session_limit weekly_limit session_raw weekly_raw
  if ! normalized="$(printf '%s' "$fetch_out" | jq -ser '[.[] | (if type=="array" then .[0] else . end)] | map(select(.usage?)) | .[0]' 2>/dev/null)"; then
    log_warn_trunc "refresh[codex]: jq parse failure (normalize) out=${fetch_out}" 300
    return 1
  fi

  # CodexBar has changed/expanded its JSON a few times. Do not trust field
  # names alone: select the 5-hour/session and 7-day/weekly limits by their
  # windowMinutes, falling back to primary/secondary only if needed.
  if ! session_limit="$(printf '%s' "$normalized" | jq -cer '
    [.usage.primary?, .usage.secondary?, .openaiDashboard.primaryLimit?, .openaiDashboard.secondaryLimit?]
    | map(select(type == "object" and (.usedPercent? != null)))
    | (map(select(((.windowMinutes? // 0) | tonumber) > 0 and ((.windowMinutes? // 0) | tonumber) <= 360)) | first) // .[0] // empty
  ' 2>/dev/null)"; then
    log_warn "refresh[codex]: jq parse failure (session limit)"
    return 1
  fi

  if ! weekly_limit="$(printf '%s' "$normalized" | jq -cer '
    [.usage.primary?, .usage.secondary?, .openaiDashboard.primaryLimit?, .openaiDashboard.secondaryLimit?]
    | map(select(type == "object" and (.usedPercent? != null)))
    | (map(select(((.windowMinutes? // 0) | tonumber) >= 1000)) | first) // .[1] // empty
  ' 2>/dev/null)"; then
    log_warn "refresh[codex]: jq parse failure (weekly limit)"
    return 1
  fi

  if ! session_raw="$(printf '%s' "$session_limit" | jq -er '.usedPercent | tonumber' 2>/dev/null)"; then
    log_warn "refresh[codex]: jq parse failure (session usedPercent)"
    return 1
  fi
  if ! weekly_raw="$(printf '%s' "$weekly_limit" | jq -er '.usedPercent | tonumber' 2>/dev/null)"; then
    log_warn "refresh[codex]: jq parse failure (weekly usedPercent)"
    return 1
  fi

  FETCH_SESSION_USED="$session_raw"
  FETCH_WEEKLY_USED="$weekly_raw"
  FETCH_SESSION_WINDOW_MINUTES="$(printf '%s' "$session_limit" | jq -er '.windowMinutes // empty | tonumber' 2>/dev/null || true)"
  FETCH_WEEKLY_WINDOW_MINUTES="$(printf '%s' "$weekly_limit" | jq -er '.windowMinutes // empty | tonumber' 2>/dev/null || true)"

  local iso
  iso="$(printf '%s' "$session_limit" | jq -er -r '.resetsAt // empty | tostring' 2>/dev/null || true)"
  if [[ -n "${iso:-}" ]]; then
    FETCH_SESSION_RESETS_AT="$(iso_utc_to_epoch "$iso" 2>/dev/null || true)"
  fi
  if [[ -z "${FETCH_SESSION_RESETS_AT:-}" ]]; then
    local reset_description fetch_now
    reset_description="$(printf '%s' "$session_limit" | jq -er -r '.resetDescription // empty | tostring' 2>/dev/null || true)"
    fetch_now="$(now_epoch)"
    if [[ -n "${reset_description:-}" ]]; then
      FETCH_SESSION_RESETS_AT="$(codex_reset_description_to_epoch "$reset_description" "$fetch_now" "$FETCH_SESSION_WINDOW_MINUTES" 2>/dev/null || true)"
    fi
  fi
  iso="$(printf '%s' "$weekly_limit" | jq -er -r '.resetsAt // empty | tostring' 2>/dev/null || true)"
  if [[ -n "${iso:-}" ]]; then
    FETCH_WEEKLY_RESETS_AT="$(iso_utc_to_epoch "$iso" 2>/dev/null || true)"
  fi

  # Codex's third window. `tertiary` when the account has one; otherwise the
  # first extra rate window CodexBar reports (today: "gpt-reserve", a 7-day
  # reserve pool). Named by the provider, not by us — see SCOPED_MODEL_NAME.
  # Absent is normal and is not a failure: the block publishes scoped_used
  # null and the module renders "n/a".
  local scoped_limit
  scoped_limit="$(printf '%s' "$normalized" | jq -c '
    (.usage.tertiary? | select(type == "object" and (.usedPercent? != null)))
    // ([ .usage.extraRateWindows[]?
          | select((.window?.usedPercent? // null) != null)
          | (.window + {title: (.title // .id // "")}) ] | first)
    // empty
  ' 2>/dev/null || true)"

  if [[ -n "${scoped_limit:-}" ]]; then
    FETCH_SCOPED_USED="$(printf '%s' "$scoped_limit" | jq -er '.usedPercent | tonumber' 2>/dev/null || true)"
    if [[ -n "${FETCH_SCOPED_USED:-}" ]]; then
      FETCH_SCOPED_WINDOW_MINUTES="$(printf '%s' "$scoped_limit" | jq -er '.windowMinutes // empty | tonumber' 2>/dev/null || true)"
      FETCH_SCOPED_LABEL="$(printf '%s' "$scoped_limit" | jq -er -r '.title // empty' 2>/dev/null || true)"
      [[ -n "${FETCH_SCOPED_LABEL:-}" ]] || FETCH_SCOPED_LABEL='Reserve'

      iso="$(printf '%s' "$scoped_limit" | jq -er -r '.resetsAt // empty | tostring' 2>/dev/null || true)"
      if [[ -n "${iso:-}" ]]; then
        FETCH_SCOPED_RESETS_AT="$(iso_utc_to_epoch "$iso" 2>/dev/null || true)"
      fi
      if [[ -z "${FETCH_SCOPED_RESETS_AT:-}" ]]; then
        local scoped_description
        scoped_description="$(printf '%s' "$scoped_limit" | jq -er -r '.resetDescription // empty | tostring' 2>/dev/null || true)"
        if [[ -n "${scoped_description:-}" ]]; then
          FETCH_SCOPED_RESETS_AT="$(codex_reset_description_to_epoch "$scoped_description" "$(now_epoch)" "$FETCH_SCOPED_WINDOW_MINUTES" 2>/dev/null || true)"
        fi
      fi
    fi
  else
    log_debug "refresh[codex]: no tertiary or extra rate window reported"
  fi

  # The normalized object rather than the raw array: one provider payload per
  # file, the same shape Claude's raw file has.
  persist_raw_payload codex "$normalized"

  return 0
}

claude_oauth_response_is_auth_error() {
  local response="$1"
  [[ -n "$response" ]] || return 1
  printf '%s' "$response" | jq -e '
    ((try .error.type catch null) == "authentication_error")
    or ((.error | type) == "string" and (.error == "invalid_token" or .error == "invalid_grant"))
  ' >/dev/null 2>&1
}

fetch_via_claude_oauth() {
  reset_fetch_outputs

  local token raw
  # Proactively refresh a known-expired/near-expired access token (common after
  # a long sleep) so the first post-wake attempt isn't wasted on an expired
  # token and a transient network blip can't push backoff to its ceiling.
  if claude_oauth_access_token_is_expired; then
    log_info "refresh[claude]: access token expired/near-expiry; refreshing proactively"
    try_oauth_token_refresh || true
  fi

  token="$(read_claude_oauth_access_token 2>/dev/null || true)"
  if [[ -z "${token:-}" ]]; then
    log_warn "refresh[claude]: keychain token unavailable"
    if (( CLAUDE_OAUTH_REFRESH_REAUTH_REQUIRED != 0 )) \
       || [[ -z "$(read_claude_oauth_refresh_token 2>/dev/null || true)" ]]; then
      FETCH_AUTH_REQUIRED=1
    fi
    return 1
  fi

  raw="$(fetch_claude_oauth_usage_json "$token" 2>/dev/null || true)"

  # An empty or unparseable body can be a transient blip OR an auth-rejected
  # request whose 401/403 carried no JSON body (fetch_claude_oauth_usage_json
  # does not surface the HTTP status, so we can't tell from the body alone).
  # Make one token-refresh attempt and re-fetch before giving up: a healthy
  # token simply succeeds on retry, an expired one gets rotated.
  if [[ -z "${raw:-}" ]] || ! printf '%s' "$raw" | jq -e . >/dev/null 2>&1; then
    log_warn "refresh[claude]: empty/unparseable oauth response; attempting token refresh"
    if try_oauth_token_refresh; then
      token="$(read_claude_oauth_access_token 2>/dev/null || true)"
      if [[ -n "${token:-}" ]]; then
        raw="$(fetch_claude_oauth_usage_json "$token" 2>/dev/null || true)"
      fi
    fi
    if [[ -z "${raw:-}" ]]; then
      log_warn "refresh[claude]: empty oauth response after refresh attempt"
      (( CLAUDE_OAUTH_REFRESH_REAUTH_REQUIRED != 0 )) && FETCH_AUTH_REQUIRED=1
      return 1
    fi
  fi

  if claude_oauth_response_is_auth_error "$raw"; then
    log_info "refresh[claude]: auth error detected; attempting token refresh"
    if try_oauth_token_refresh; then
      token="$(read_claude_oauth_access_token 2>/dev/null || true)"
      if [[ -n "${token:-}" ]]; then
        raw="$(fetch_claude_oauth_usage_json "$token" 2>/dev/null || true)"
      fi
    fi

    if [[ -z "${raw:-}" ]]; then
      log_warn "refresh[claude]: empty oauth response after refresh attempt"
      (( CLAUDE_OAUTH_REFRESH_REAUTH_REQUIRED != 0 )) && FETCH_AUTH_REQUIRED=1
      return 1
    fi
    if claude_oauth_response_is_auth_error "$raw"; then
      log_warn "refresh[claude]: auth error persists after refresh attempt"
      FETCH_AUTH_REQUIRED=1
      return 1
    fi
  fi

  if printf '%s' "$raw" | jq -e '(try .error.type catch null) == "rate_limit_error"' >/dev/null 2>&1; then
    log_warn "refresh[claude]: rate limited by the usage endpoint"
    FETCH_RATE_LIMITED=1
    return 1
  fi

  if ! printf '%s' "$raw" | jq -e '.five_hour and .seven_day' >/dev/null 2>&1; then
    log_warn_trunc "refresh[claude]: missing usage fields out=${raw}" 300
    return 1
  fi

  # Persist the full endpoint response for consumers that want more than the
  # session/weekly percentages (the extra-usage fields, the per-limit detail).
  # Best-effort.
  persist_raw_payload claude "$raw"

  local session_raw weekly_raw
  if ! session_raw="$(printf '%s' "$raw" | jq -er '.five_hour.utilization' 2>/dev/null)"; then
    log_warn "refresh[claude]: missing five_hour.utilization"
    return 1
  fi
  if ! weekly_raw="$(printf '%s' "$raw" | jq -er '.seven_day.utilization' 2>/dev/null)"; then
    log_warn "refresh[claude]: missing seven_day.utilization"
    return 1
  fi

  FETCH_SESSION_USED="$session_raw"
  FETCH_WEEKLY_USED="$weekly_raw"
  FETCH_SESSION_WINDOW_MINUTES=300
  FETCH_WEEKLY_WINDOW_MINUTES=10080

  local iso
  iso="$(printf '%s' "$raw" | jq -er -r '.five_hour.resets_at // empty | tostring' 2>/dev/null || true)"
  if [[ -n "${iso:-}" ]]; then
    FETCH_SESSION_RESETS_AT="$(iso_utc_to_epoch "$iso" 2>/dev/null || true)"
  fi
  iso="$(printf '%s' "$raw" | jq -er -r '.seven_day.resets_at // empty | tostring' 2>/dev/null || true)"
  if [[ -n "${iso:-}" ]]; then
    FETCH_WEEKLY_RESETS_AT="$(iso_utc_to_epoch "$iso" 2>/dev/null || true)"
  fi

  # Model-scoped weekly limit (Fable, by default). It lives only in .limits[];
  # there is no top-level seven_day_<model> key for it. Absent scope => module
  # renders "n/a" rather than failing the whole refresh.
  local scoped_limit
  scoped_limit="$(printf '%s' "$raw" | jq -c \
    --arg m "$SCOPED_MODEL_NAME" '
      [ .limits[]?
        | select((.kind? // "") == "weekly_scoped")
        | select(((.scope?.model?.display_name? // "") | ascii_downcase)
                 | startswith($m | ascii_downcase))
      ] | first // empty
    ' 2>/dev/null || true)"

  if [[ -n "${scoped_limit:-}" ]]; then
    FETCH_SCOPED_USED="$(printf '%s' "$scoped_limit" | jq -er '.percent | tonumber' 2>/dev/null || true)"
    if [[ -n "${FETCH_SCOPED_USED:-}" ]]; then
      FETCH_SCOPED_WINDOW_MINUTES=10080
      # The endpoint's own name for the window, not the option we matched on:
      # @codexbar_scoped_model matches by prefix, so "Fable" can select a limit
      # the endpoint calls "Fable 5.1", and the panel should say the latter.
      FETCH_SCOPED_LABEL="$(printf '%s' "$scoped_limit" | jq -er -r '.scope?.model?.display_name // empty' 2>/dev/null || true)"
      [[ -n "${FETCH_SCOPED_LABEL:-}" ]] || FETCH_SCOPED_LABEL="$SCOPED_MODEL_NAME"
      iso="$(printf '%s' "$scoped_limit" | jq -er -r '.resets_at // empty | tostring' 2>/dev/null || true)"
      if [[ -n "${iso:-}" ]]; then
        FETCH_SCOPED_RESETS_AT="$(iso_utc_to_epoch "$iso" 2>/dev/null || true)"
      fi
    fi
  else
    log_debug "refresh[claude]: no weekly_scoped limit for model=${SCOPED_MODEL_NAME}"
  fi

  # Severity and the locked flag, per family, straight from .limits[]. They
  # travel in the provider block so a reader has every fact about a window in
  # one file instead of reopening the raw payload for one boolean.
  local kinds=(session weekly_all weekly_scoped) families=(SESSION WEEKLY SCOPED)
  local i limit_entry severity locked
  for i in 0 1 2; do
    limit_entry="$(printf '%s' "$raw" | jq -c --arg k "${kinds[$i]}" \
      '[ .limits[]? | select((.kind? // "") == $k) ] | first // empty' 2>/dev/null || true)"
    [[ -n "${limit_entry:-}" ]] || continue

    severity="$(printf '%s' "$limit_entry" | jq -er -r '.severity // empty' 2>/dev/null || true)"
    [[ -n "${severity:-}" ]] || severity='normal'
    if printf '%s' "$limit_entry" | jq -e '(.locked_reason // null) != null' >/dev/null 2>&1; then
      locked=1
    else
      locked=0
    fi

    printf -v "FETCH_${families[$i]}_SEVERITY" '%s' "$severity"
    printf -v "FETCH_${families[$i]}_LOCKED" '%s' "$locked"
  done

  # What spent the week ("Claude Code 98%, Chats 1%").
  FETCH_BREAKDOWN_JSON="$(printf '%s' "$raw" | jq -c '
    [ .seven_day_breakdown?.rows[]?
      | select((.percent? // null) != null)
      | {key: (.key // ""), display_name: (.display_name // .key // ""), percent: .percent}
    ]' 2>/dev/null || true)"
  [[ -n "${FETCH_BREAKDOWN_JSON:-}" ]] || FETCH_BREAKDOWN_JSON='[]'

  return 0
}

cache_auth_required() {
  provider_enabled claude || return 1
  [[ -f "$CACHE_FILE" ]] || return 1
  command -v jq >/dev/null 2>&1 || return 1
  # The provider block is the authority; the top level is checked too so a
  # cache written before the providers map existed still answers.
  jq -e '((.providers.claude.state? // .state) == "auth_required")' "$CACHE_FILE" >/dev/null 2>&1
}

login_claude_oauth() {
  if ! ensure_claude_cli_in_path; then
    printf '%s\n' 'Claude CLI was not found. Install it, then press prefix + u again.' >&2
    printf '%s' 'Press Enter to close... '
    IFS= read -r _ || true
    return 1
  fi

  printf '%s\n\n' 'Opening Claude subscription login in your browser...'
  if ! env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN claude auth login --claudeai; then
    printf '\n%s\n' 'Login was not completed.' >&2
    printf '%s' 'Press Enter to close... '
    IFS= read -r _ || true
    return 1
  fi

  reset_refresh_backoff
  CODEXBAR_USAGE_FORCE_REFRESH=1 refresh_cache || true
  publish_to_tmux_opts || true
  command -v tmux >/dev/null 2>&1 && tmux refresh-client -S >/dev/null 2>&1 || true

  if cache_auth_required; then
    printf '\n%s\n' 'Login finished, but usage could not be refreshed yet. The bar will retry automatically.'
    printf '%s' 'Press Enter to close... '
    IFS= read -r _ || true
  fi
}

# ── The published cache ─────────────────────────────────────────────────────
#
# usage.json is written HERE and nowhere else, and read by three programs that
# never write it: the tmux modules, UsageBar's menu bar popover, and CuaNotch's
# usage panel. That makes its shape a cross-repo contract, and cua-notch's
# dev/check-invariants pins four parts of it against this file on every commit.
#
#   {
#     "updated_at": …, "state": "ok",          ← the PRIMARY provider's block,
#     "session_used": …, "weekly_used": …,        flattened. Always Claude.
#     "scoped_used": …, … ,                       Does NOT follow the display
#     "schema": 2,                                option — a reader that wants
#     "primary_provider": "claude",               "the Claude numbers" can go
#     "display_provider": "claude",               on reading the root forever.
#     "providers": { "claude": {…}, "codex": {…} }
#   }
#
# A provider block carries the SAME key names as the root plus its own
# label/severity/locked/breakdown/file pointers, so a reader written against
# the root works against a block unchanged. A provider that is not configured
# has NO ENTRY — absence, not a state string, is how "draw nothing" is said.
#
# Merging, not replacing: a fetch failure for one provider must never blank
# another's numbers (the 2am bug), so each refresh merges its blocks over what
# is already on disk and a failed provider contributes only a state/checked_at
# patch.
enabled_providers_json() {
  local p out=''
  for p in $USAGE_PROVIDERS; do
    out+="${out:+,}\"${p}\""
  done
  printf '[%s]' "$out"
}

json_num_or_null() {
  if [[ "${1:-}" =~ ^[0-9]+$ ]]; then
    printf '%s' "$1"
  else
    printf '%s' 'null'
  fi
}

json_bool() {
  if [[ "${1:-0}" == "1" ]]; then
    printf '%s' 'true'
  else
    printf '%s' 'false'
  fi
}

# A minimal well-formed block for a provider that was attempted and did not
# answer. Merged OVER the existing one, so the last good numbers survive with
# an honest state and a fresh checked_at on top.
provider_status_patch() {
  local provider="${1:-}" state="${2:-error}" checked_at="${3:-0}"

  jq -nc \
    --arg provider "$provider" \
    --arg label "$(provider_label_for "$provider")" \
    --arg state "$state" \
    --argjson checked_at "$(json_num_or_null "$checked_at")" \
    --arg raw_file "$(basename "$(raw_file_for "$provider")")" \
    --arg history_file "$(basename "$(history_file_for "$provider")")" \
    '{provider: $provider, label: $label, state: $state, checked_at: $checked_at,
      raw_file: $raw_file, history_file: $history_file}' 2>/dev/null || true
}

# Merge provider blocks into usage.json and rewrite it atomically.
# $1: a JSON object of provider -> block (or patch).
write_usage_cache() {
  local blocks_json="${1:-}"
  [[ -n "${blocks_json:-}" ]] || blocks_json='{}'

  mkdir -p "$CACHE_DIR" 2>/dev/null || return 1
  command -v jq >/dev/null 2>&1 || return 1

  local prev='{}'
  if [[ -f "$CACHE_FILE" ]]; then
    prev="$(jq -c '.' "$CACHE_FILE" 2>/dev/null || true)"
    [[ -n "${prev:-}" ]] || prev='{}'
  fi

  local merged
  merged="$(jq -n \
    --argjson prev "$prev" \
    --argjson blocks "$blocks_json" \
    --argjson enabled "$(enabled_providers_json)" \
    --arg primary "$PROVIDER_PRIMARY" \
    --arg display "$DISPLAY_PROVIDER" \
    --arg primary_raw "$(basename "$(raw_file_for "$PROVIDER_PRIMARY")")" \
    --arg primary_history "$(basename "$(history_file_for "$PROVIDER_PRIMARY")")" '
      def strip_root: del(.providers, .schema, .primary_provider, .display_provider);
      # The third family used to be keyed on a model name. Carry a pre-rename
      # cache forward rather than dropping that window on the floor for the
      # first refresh after an upgrade.
      def unfable: with_entries(
        if (.key | startswith("fable_")) then .key |= sub("^fable_"; "scoped_") else . end);

      ($prev // {}) as $p
      # A cache written before the providers map existed IS the primary
      # provider, so seed it as that block. Without this, a first refresh in
      # which the primary fetch fails would merge its status patch onto
      # nothing and publish a block with no numbers — the upgrade itself
      # would look exactly like a wiped cache.
      | (if ($p | has("providers")) then $p.providers
         elif ($p | has("updated_at"))
         then {($primary): (($p | strip_root | unfable)
                            + {provider: $primary, label: "Claude",
                               raw_file: $primary_raw, history_file: $primary_history})}
         else {} end) as $seed
      # `*` is a recursive merge, so a status patch updates state/checked_at
      # and leaves the numbers under it alone. Only configured providers
      # survive the filter: a provider dropped from @codexbar_providers
      # should stop being drawn, not linger with month-old numbers.
      | (($seed * $blocks)
         | with_entries(select(.key as $k | $enabled | index($k)))
         | with_entries(.value |=
             ({updated_at: 0, checked_at: 0, state: "error", breakdown: []} * .))) as $providers
      | (if ($providers | has($primary))
         then $providers[$primary]
         else ($p | strip_root | unfable)
         end) as $root
      | (if ($root | type) == "object" and (($root | length) > 0)
         then $root
         else {updated_at: 0, state: "missing"}
         end)
        + {schema: 2, primary_provider: $primary, display_provider: $display,
           providers: $providers}
    ' 2>/dev/null || true)"
  [[ -n "${merged:-}" ]] || { log_error "cache: merge failed; leaving previous usage.json in place"; return 1; }

  umask 077
  local tmp
  tmp="$(mktemp "${CACHE_FILE}.tmp.XXXXXX")" || return 1
  if printf '%s\n' "$merged" >"$tmp" 2>/dev/null && mv -f "$tmp" "$CACHE_FILE" 2>/dev/null; then
    return 0
  fi
  rm -f "$tmp" 2>/dev/null || true
  return 1
}

# Turn one fetch's FETCH_* outputs into a provider block.
#
# It ANSWERS IN GLOBALS, not on stdout, and that is deliberate: a caller that
# wrote `block="$(render_provider_block …)"` would run it in a subshell, and
# the RENDER_* values below — which the history sample and the log line are
# built from — would die with it. That bug is invisible in the cache (the
# block is correct) and shows up only as a history file that never grows and
# a projection that never appears.
RENDER_BLOCK=''
RENDER_SESSION_USED=''
RENDER_WEEKLY_USED=''
RENDER_SCOPED_USED=''
RENDER_SESSION_RESETS=''
RENDER_WEEKLY_RESETS=''
RENDER_SCOPED_RESETS=''

render_provider_block() {
  local provider="$1" updated_at="$2"

  RENDER_BLOCK=''
  local session_used weekly_used scoped_used=''
  session_used="$(clamp_0_100_int "$FETCH_SESSION_USED")" || return 1
  weekly_used="$(clamp_0_100_int "$FETCH_WEEKLY_USED")" || return 1

  # A missing scoped window is normal (other providers, other plans); it
  # blanks that one module instead of failing the refresh.
  if [[ -n "${FETCH_SCOPED_USED:-}" ]]; then
    scoped_used="$(clamp_0_100_int "$FETCH_SCOPED_USED" || true)"
  fi

  local session_window="$FETCH_SESSION_WINDOW_MINUTES" weekly_window="$FETCH_WEEKLY_WINDOW_MINUTES"
  local scoped_window="$FETCH_SCOPED_WINDOW_MINUTES"
  local session_resets="$FETCH_SESSION_RESETS_AT" weekly_resets="$FETCH_WEEKLY_RESETS_AT"
  local scoped_resets="$FETCH_SCOPED_RESETS_AT"

  local session_pace weekly_pace session_text weekly_text session_color weekly_color
  session_pace="$(pace_suffix "$session_used" "$session_window" "$session_resets" "$updated_at")"
  weekly_pace="$(pace_suffix  "$weekly_used"  "$weekly_window"  "$weekly_resets"  "$updated_at")"
  session_text="${session_used}%${session_pace}"
  weekly_text="${weekly_used}%${weekly_pace}"
  session_color="$(color_for_window "$session_used" "$session_window" "$session_resets" "$updated_at")"
  weekly_color="$(color_for_window "$weekly_used"  "$weekly_window"  "$weekly_resets"  "$updated_at")"
  local session_pace_color weekly_pace_color
  session_pace_color="$(pace_color_for_window "$session_used" "$session_window" "$session_resets" "$updated_at")"
  weekly_pace_color="$(pace_color_for_window "$weekly_used"  "$weekly_window"  "$weekly_resets"  "$updated_at")"

  local scoped_text='' scoped_color='' scoped_pace_color=''
  if [[ -n "${scoped_used:-}" ]]; then
    local scoped_pace
    scoped_pace="$(pace_suffix "$scoped_used" "$scoped_window" "$scoped_resets" "$updated_at")"
    scoped_text="${scoped_used}%${scoped_pace}"
    scoped_color="$(color_for_window "$scoped_used" "$scoped_window" "$scoped_resets" "$updated_at")"
    scoped_pace_color="$(pace_color_for_window "$scoped_used" "$scoped_window" "$scoped_resets" "$updated_at")"
  else
    scoped_text='n/a'
    scoped_color='brightblack'
    scoped_pace_color='brightblack'
  fi

  # Between 5-hour windows the endpoint reports utilization 0 with a null
  # resets_at — there is no window, so pacing is undefined and the reset time is
  # unknown. Say "idle" in gray instead of a bare green "0%", which is
  # indistinguishable from a live-but-unpaced reading or a stalled fetch.
  if [[ -z "${session_resets:-}" ]] && (( session_used == 0 )); then
    session_text='idle'
    session_color='brightblack'
    session_pace_color='brightblack'
  fi

  RENDER_SESSION_USED="$session_used"
  RENDER_WEEKLY_USED="$weekly_used"
  RENDER_SCOPED_USED="$scoped_used"
  RENDER_SESSION_RESETS="$session_resets"
  RENDER_WEEKLY_RESETS="$weekly_resets"
  RENDER_SCOPED_RESETS="$scoped_resets"

  local breakdown="$FETCH_BREAKDOWN_JSON"
  printf '%s' "$breakdown" | jq -e 'type == "array"' >/dev/null 2>&1 || breakdown='[]'

  RENDER_BLOCK="$(jq -nc \
    --arg provider "$provider" \
    --arg label "$(provider_label_for "$provider")" \
    --argjson updated_at "$(json_num_or_null "$updated_at")" \
    --argjson session_used "$(json_num_or_null "$session_used")" \
    --argjson weekly_used "$(json_num_or_null "$weekly_used")" \
    --argjson scoped_used "$(json_num_or_null "$scoped_used")" \
    --argjson session_window "$(json_num_or_null "$session_window")" \
    --argjson weekly_window "$(json_num_or_null "$weekly_window")" \
    --argjson scoped_window "$(json_num_or_null "$scoped_window")" \
    --argjson session_resets "$(json_num_or_null "$session_resets")" \
    --argjson weekly_resets "$(json_num_or_null "$weekly_resets")" \
    --argjson scoped_resets "$(json_num_or_null "$scoped_resets")" \
    --arg session_text "$session_text" \
    --arg weekly_text "$weekly_text" \
    --arg scoped_text "$scoped_text" \
    --arg session_color "$session_color" \
    --arg weekly_color "$weekly_color" \
    --arg scoped_color "$scoped_color" \
    --arg session_pace_color "$session_pace_color" \
    --arg weekly_pace_color "$weekly_pace_color" \
    --arg scoped_pace_color "$scoped_pace_color" \
    --arg session_label "$FETCH_SESSION_LABEL" \
    --arg weekly_label "$FETCH_WEEKLY_LABEL" \
    --arg scoped_label "$FETCH_SCOPED_LABEL" \
    --arg session_severity "$FETCH_SESSION_SEVERITY" \
    --arg weekly_severity "$FETCH_WEEKLY_SEVERITY" \
    --arg scoped_severity "$FETCH_SCOPED_SEVERITY" \
    --argjson session_locked "$(json_bool "$FETCH_SESSION_LOCKED")" \
    --argjson weekly_locked "$(json_bool "$FETCH_WEEKLY_LOCKED")" \
    --argjson scoped_locked "$(json_bool "$FETCH_SCOPED_LOCKED")" \
    --argjson breakdown "$breakdown" \
    --arg raw_file "$(basename "$(raw_file_for "$provider")")" \
    --arg history_file "$(basename "$(history_file_for "$provider")")" \
    --argjson fetched_at "$(json_num_or_null "${RENDER_FETCHED_AT:-$updated_at}")" \
    '{
       provider: $provider, label: $label, state: "ok",
       updated_at: $updated_at, checked_at: $updated_at,
       # updated_at: as of when the numbers are current (any source).
       # fetched_at: the last endpoint fetch, which the poll gates on.
       fetched_at: $fetched_at,

       session_used: $session_used, session_window_minutes: $session_window,
       session_resets_at: $session_resets, session_text: $session_text,
       session_color: $session_color, session_pace_color: $session_pace_color,
       session_label: $session_label,
       session_severity: $session_severity, session_locked: $session_locked,

       weekly_used: $weekly_used, weekly_window_minutes: $weekly_window,
       weekly_resets_at: $weekly_resets, weekly_text: $weekly_text,
       weekly_color: $weekly_color, weekly_pace_color: $weekly_pace_color,
       weekly_label: $weekly_label,
       weekly_severity: $weekly_severity, weekly_locked: $weekly_locked,

       scoped_used: $scoped_used, scoped_window_minutes: $scoped_window,
       scoped_resets_at: $scoped_resets, scoped_text: $scoped_text,
       scoped_color: $scoped_color, scoped_pace_color: $scoped_pace_color,
       scoped_label: $scoped_label,
       scoped_severity: $scoped_severity, scoped_locked: $scoped_locked,

       breakdown: $breakdown,
       raw_file: $raw_file, history_file: $history_file,

       # Retained spellings from the first version of this file. Cheap to
       # keep, and something out there may still read them.
       session_windowMinutes: $session_window, session_resetsAt: $session_resets,
       weekly_windowMinutes: $weekly_window, weekly_resetsAt: $weekly_resets
     }' 2>/dev/null || true)"

  [[ -n "${RENDER_BLOCK:-}" ]] || return 1
  return 0
}

# ── Claude Code's live numbers ──────────────────────────────────────────────
#
# BEST OF BOTH. The endpoint is the only source of the scoped cap, the
# severities, the locked flags and the breakdown, and the only one that sees
# usage Claude Code cannot (claude.ai, the phone) — but it is a poll, and its
# budget is shared with every session. Claude Code's status line carries the
# session and weekly utilization off every API response, free and live after
# every turn. So the endpoint keeps its poll, the live sample fills the time
# between polls, and for each window the two are reconciled by one rule:
#
#   same window (resets_at within 10 min)  the HIGHER reading — usage only
#                                          rises inside a window, so the
#                                          higher one is simply the newer one
#   different windows                      the LATER window
#   a live reading whose window has passed ignored; an endpoint reading is
#                                          never dropped, it is what we have
#
# Applied to FETCH_SESSION_* / FETCH_WEEKLY_* in place. $1 is the sample JSON,
# $2 the time the FETCH_* values are as of. LIVE_AS_OF becomes the newest
# first-seen time among the readings kept (never older than $2).
LIVE_AS_OF=0
# The fetched_at render_provider_block stamps; empty means "this render IS a
# fetch" (fetched_at = updated_at).
RENDER_FETCHED_AT=''
apply_claude_live_sample() {
  local sample="${1:-}" base_t="${2:-0}" out
  LIVE_AS_OF="$base_t"
  [[ -n "${sample:-}" ]] || return 0
  [[ "$base_t" =~ ^[0-9]+$ ]] || base_t=0

  out="$(printf '%s' "$sample" | jq -r \
    --argjson now "$(now_epoch)" --argjson base_t "$base_t" \
    --arg su "${FETCH_SESSION_USED:-}" --arg sr "${FETCH_SESSION_RESETS_AT:-}" \
    --arg wu "${FETCH_WEEKLY_USED:-}" --arg wr "${FETCH_WEEKLY_RESETS_AT:-}" '
      def num($s): ($s | tonumber? // null);
      def api($u; $r):
        if num($u) == null then null
        else {used: num($u), resets_at: num($r), t: $base_t} end;
      def open($x):
        if ($x | type) == "object" and ($x.used | type) == "number"
           and ($x.resets_at // 0) > $now
        then $x else null end;
      def pick($a; $l):
        if $l == null then $a
        elif $a == null or $a.resets_at == null then $l
        elif (($a.resets_at - $l.resets_at) | fabs) <= 600 then
          (if $l.used > $a.used then $l else $a end)
        elif $l.resets_at > $a.resets_at then $l
        else $a end;
      pick(api($su; $sr); open(.five_hour)) as $s
      | pick(api($wu; $wr); open(.seven_day)) as $w
      | if $s == null or $w == null then empty else
          [ $s.used, ($s.resets_at // ""), $w.used, ($w.resets_at // ""),
            ([$s.t, $w.t, $base_t] | max) ]
          | map(tostring) | join(" ")
        end
    ' 2>/dev/null || true)"
  [[ -n "${out:-}" ]] || return 0

  local su sr wu wr as_of
  read -r su sr wu wr as_of <<<"$out"
  FETCH_SESSION_USED="$su"
  FETCH_SESSION_RESETS_AT="$sr"
  FETCH_WEEKLY_USED="$wu"
  FETCH_WEEKLY_RESETS_AT="$wr"
  [[ "$as_of" =~ ^[0-9]+$ ]] && LIVE_AS_OF="$as_of"
  return 0
}

# Reload a provider's FETCH_* from its block in usage.json, so the block can
# be re-rendered with some fields changed and everything else — the scoped
# cap, labels, severities, breakdown — exactly as the last fetch left it.
# Also sets BLOCK_STATE, BLOCK_UPDATED_AT and BLOCK_FETCHED_AT.
BLOCK_STATE=''
BLOCK_UPDATED_AT=0
BLOCK_FETCHED_AT=0
load_fetch_from_block() {
  local provider="$1" line
  reset_fetch_outputs
  [[ -f "$CACHE_FILE" ]] || return 1

  # \x1f, not a tab: tab is IFS whitespace, so `read` would merge the empty
  # fields a null scoped window produces and shift everything after them.
  line="$(jq -r --arg p "$provider" '
    .providers[$p] // empty
    | select(.session_used != null and .weekly_used != null)
    | [ .session_used, .weekly_used, .scoped_used,
        .session_window_minutes, .weekly_window_minutes, .scoped_window_minutes,
        .session_resets_at, .weekly_resets_at, .scoped_resets_at,
        .session_label, .weekly_label, .scoped_label,
        .session_severity, .weekly_severity, .scoped_severity,
        .session_locked, .weekly_locked, .scoped_locked,
        .state, (.updated_at // 0), (.fetched_at // .updated_at // 0) ]
    | map(if . == null then "" elif . == true then "1" elif . == false then "0"
          else tostring end)
    | join("\u001f")
  ' "$CACHE_FILE" 2>/dev/null || true)"
  [[ -n "${line:-}" ]] || return 1

  IFS=$'\x1f' read -r \
    FETCH_SESSION_USED FETCH_WEEKLY_USED FETCH_SCOPED_USED \
    FETCH_SESSION_WINDOW_MINUTES FETCH_WEEKLY_WINDOW_MINUTES FETCH_SCOPED_WINDOW_MINUTES \
    FETCH_SESSION_RESETS_AT FETCH_WEEKLY_RESETS_AT FETCH_SCOPED_RESETS_AT \
    FETCH_SESSION_LABEL FETCH_WEEKLY_LABEL FETCH_SCOPED_LABEL \
    FETCH_SESSION_SEVERITY FETCH_WEEKLY_SEVERITY FETCH_SCOPED_SEVERITY \
    FETCH_SESSION_LOCKED FETCH_WEEKLY_LOCKED FETCH_SCOPED_LOCKED \
    BLOCK_STATE BLOCK_UPDATED_AT BLOCK_FETCHED_AT <<<"$line"

  [[ -n "${FETCH_SESSION_LABEL:-}" ]] || FETCH_SESSION_LABEL='Session'
  [[ -n "${FETCH_WEEKLY_LABEL:-}" ]] || FETCH_WEEKLY_LABEL='Weekly'
  local fam
  for fam in SESSION WEEKLY SCOPED; do
    local sev="FETCH_${fam}_SEVERITY" lck="FETCH_${fam}_LOCKED"
    [[ -n "${!sev:-}" ]] || printf -v "$sev" '%s' 'normal'
    [[ "${!lck:-}" == 1 ]] || printf -v "$lck" '%s' 0
  done
  [[ "$BLOCK_UPDATED_AT" =~ ^[0-9]+$ ]] || BLOCK_UPDATED_AT=0
  [[ "$BLOCK_FETCHED_AT" =~ ^[0-9]+$ ]] || BLOCK_FETCHED_AT="$BLOCK_UPDATED_AT"

  FETCH_BREAKDOWN_JSON="$(jq -c --arg p "$provider" '.providers[$p].breakdown // []' \
    "$CACHE_FILE" 2>/dev/null || true)"
  [[ -n "${FETCH_BREAKDOWN_JSON:-}" ]] || FETCH_BREAKDOWN_JSON='[]'
  return 0
}

# Fold the live sample into Claude's block in usage.json. Run by
# codexbar-usage-live.sh whenever the sample changes (--merge-live), and by the
# tick for a sample that arrived while the lock was busy. Never touches the
# network. Re-renders the block — text, colours and pace all follow the new
# numbers — and leaves fetched_at alone, so the endpoint poll keeps its own
# schedule for the things only it knows.
merge_live_claude() {
  provider_enabled claude || return 0
  [[ -f "$LIVE_SAMPLE_FILE" && -f "$CACHE_FILE" ]] || return 0
  command -v jq >/dev/null 2>&1 || return 0

  # A refresh in flight folds the sample in itself; a sample that lands after
  # it read the file is still newer than the marker, and the next tick has it.
  if ! try_acquire_lock "$$"; then
    log_debug "live[claude]: lock busy"
    return 0
  fi

  local rc=0
  merge_live_claude_locked || rc=$?
  release_lock
  return $rc
}

merge_live_claude_locked() {
  local sample
  sample="$(cat "$LIVE_SAMPLE_FILE" 2>/dev/null || true)"
  [[ -n "${sample:-}" ]] || return 0
  # The marker records what is being folded in, BEFORE the fold: a sample
  # written meanwhile differs from it and the next tick picks it up.
  printf '%s\n' "$sample" >"$LIVE_MERGED_MARKER" 2>/dev/null || true

  select_provider claude
  load_fetch_from_block claude || return 0

  local before after now
  before="$FETCH_SESSION_USED $FETCH_SESSION_RESETS_AT $FETCH_WEEKLY_USED $FETCH_WEEKLY_RESETS_AT"
  apply_claude_live_sample "$sample" "$BLOCK_UPDATED_AT"
  after="$FETCH_SESSION_USED $FETCH_SESSION_RESETS_AT $FETCH_WEEKLY_USED $FETCH_WEEKLY_RESETS_AT"
  [[ "$before" != "$after" ]] || return 0

  now="$(now_epoch)"
  RENDER_FETCHED_AT="$BLOCK_FETCHED_AT"
  render_provider_block claude "$LIVE_AS_OF" || return 1
  RENDER_FETCHED_AT=''

  # Live numbers do not clear a login problem — only a fetch proves the token
  # works — but they do supersede an "error", which only ever meant the
  # numbers had stopped moving.
  if [[ "$BLOCK_STATE" == 'auth_required' ]]; then
    RENDER_BLOCK="$(printf '%s' "$RENDER_BLOCK" | jq -c '.state = "auth_required"' 2>/dev/null || printf '%s' "$RENDER_BLOCK")"
  fi

  local blocks
  blocks="$(jq -nc --argjson b "$RENDER_BLOCK" '{claude: $b}' 2>/dev/null || true)"
  [[ -n "${blocks:-}" ]] || return 1
  write_usage_cache "$blocks" || return 1

  append_usage_history "$LIVE_AS_OF" "$RENDER_SESSION_USED" "$RENDER_WEEKLY_USED" \
    "$RENDER_SESSION_RESETS" "$RENDER_WEEKLY_RESETS" \
    "$RENDER_SCOPED_USED" "$RENDER_SCOPED_RESETS" || true

  log_info "live[claude]: from Claude Code session=${RENDER_SESSION_USED}% weekly=${RENDER_WEEKLY_USED}% (endpoint fetched $(( now - BLOCK_FETCHED_AT ))s ago)"

  publish_to_tmux_opts || true
  if command -v tmux >/dev/null 2>&1; then
    tmux refresh-client -S >/dev/null 2>&1 || true
  fi
  return 0
}

# Fetch ONE provider. Leaves its block — or, on failure, its status patch —
# in RENDER_BLOCK (see render_provider_block on why this is not stdout) and
# returns 0 only when the provider actually produced numbers.
refresh_one_provider() {
  local provider="$1" now="$2"

  select_provider "$provider"
  RENDER_BLOCK=''

  local fetch_ok=0
  case "$provider" in
    claude)
      if fetch_via_claude_oauth; then
        fetch_ok=1
      elif (( FETCH_RATE_LIMITED != 0 )); then
        # Throttled, not broken: leave the block (and its state) exactly as
        # the last good fetch wrote it, so readers show aging numbers rather
        # than an error, and start the ladder at 5 minutes — the 60s first
        # rung only ever bought another rate_limit_error.
        record_refresh_backoff_failure 3
        return 1
      elif (( FETCH_AUTH_REQUIRED != 0 )); then
        log_warn "refresh[claude]: authentication required"
        RENDER_BLOCK="$(provider_status_patch "$provider" auth_required "$now")"
      else
        RENDER_BLOCK="$(provider_status_patch "$provider" error "$now")"
      fi
      ;;
    codex)
      if fetch_via_codexbar_codex; then
        fetch_ok=1
      else
        RENDER_BLOCK="$(provider_status_patch "$provider" error "$now")"
      fi
      ;;
    *)
      log_debug "refresh: unknown provider ${provider}"
      return 1
      ;;
  esac

  if (( fetch_ok == 0 )); then
    record_refresh_backoff_failure
    return 1
  fi

  # The endpoint and Claude Code's live numbers are two views of the same
  # counters; whichever saw more usage in the current window is the newer one.
  if [[ "$provider" == 'claude' && -f "$LIVE_SAMPLE_FILE" ]]; then
    apply_claude_live_sample "$(cat "$LIVE_SAMPLE_FILE" 2>/dev/null || true)" "$now"
  fi
  RENDER_FETCHED_AT="$now"
  local rendered=0
  render_provider_block "$provider" "$now" && rendered=1
  RENDER_FETCHED_AT=''

  if (( rendered == 0 )); then
    log_warn "refresh[${provider}]: unusable numbers; keeping the previous block"
    RENDER_BLOCK="$(provider_status_patch "$provider" error "$now")"
    record_refresh_backoff_failure
    return 1
  fi

  append_usage_history "$now" "$RENDER_SESSION_USED" "$RENDER_WEEKLY_USED" \
    "$RENDER_SESSION_RESETS" "$RENDER_WEEKLY_RESETS" \
    "$RENDER_SCOPED_USED" "$RENDER_SCOPED_RESETS" || true

  log_info "refresh[${provider}]: success updated_at=${now} session=${RENDER_SESSION_USED}% weekly=${RENDER_WEEKLY_USED}% scoped=${RENDER_SCOPED_USED:-n/a}%"
  reset_refresh_backoff
  return 0
}

refresh_cache() {
  mkdir -p "$CACHE_DIR"
  log_debug "refresh: start providers=[${USAGE_PROVIDERS}] display=${DISPLAY_PROVIDER}"

  if [[ "${CODEXBAR_USAGE_LOCK_HELD:-}" == "1" && "${CODEXBAR_USAGE_LOCKDIR:-}" == "$LOCKDIR" ]]; then
    log_debug "refresh: lock inherited"
    printf '%s\n' "$$" >"$LOCKDIR/pid" 2>/dev/null || true
    printf '%s\n' "$(now_epoch)" >"$LOCKDIR/started_at" 2>/dev/null || true
  else
    if ! try_acquire_lock "$$"; then
      # A user-initiated refresh (prefix+u sets CODEXBAR_USAGE_FORCE_REFRESH=1)
      # must always be honoured — it is the manual escape hatch. If a wedged or
      # suspended worker holds the lock, steal it. Auto-refresh
      # (spawn_background_refresh_locked) does not set the flag, so it still
      # yields on contention.
      if [[ "${CODEXBAR_USAGE_FORCE_REFRESH:-}" == "1" ]]; then
        log_warn "refresh: lock busy; force-stealing for manual refresh"
        release_lock
        if ! try_acquire_lock "$$"; then
          log_debug "refresh: lock still busy after steal"
          return 0
        fi
      else
        log_debug "refresh: lock busy"
        return 0
      fi
    fi
    log_debug "refresh: lock acquired"
  fi
  trap 'release_lock; cleanup_tmp_files' EXIT INT TERM HUP

  if ! command -v jq >/dev/null 2>&1; then
    log_debug "refresh: missing tool jq"
    refresh_fail
    return 1
  fi

  # Every configured provider, each behind its OWN backoff ladder: one that is
  # failing must neither be retried ahead of its ladder nor keep the others
  # off the network. A forced refresh (prefix+u, UsageBar's "Refresh now")
  # bypasses every ladder — it is the manual escape hatch.
  local blocks='{}' provider block any_ok=0 now fc na
  for provider in $USAGE_PROVIDERS; do
    select_provider "$provider"
    now="$(now_epoch)"

    # An unforced refresh also leaves a provider alone while its numbers are
    # still fresh: this runs whenever SOME provider is due, and re-fetching
    # the healthy one on every such run is what got Claude rate-limited.
    # CODEXBAR_USAGE_EAGER_PROVIDERS names providers the caller KNOWS just
    # moved (codexbar-usage-push.sh after a Claude Code turn): for those,
    # freshness is waived and only the backoff ladder still applies.
    local eager=" ${CODEXBAR_USAGE_EAGER_PROVIDERS:-} "
    eager="${eager//,/ }"
    if [[ "${CODEXBAR_USAGE_FORCE_REFRESH:-}" != "1" ]]; then
      if [[ "$eager" == *" ${provider} "* ]]; then
        read -r fc na < <(read_refresh_backoff)
        if (( now < na )); then
          log_debug "refresh[${provider}]: eager but in backoff until ${na} (fail_count=${fc})"
          continue
        fi
      elif ! provider_refresh_due "$provider" "$now"; then
        read -r fc na < <(read_refresh_backoff)
        log_debug "refresh[${provider}]: not due (updated_at=$(provider_updated_at "$provider") backoff_until=${na} fail_count=${fc})"
        continue
      fi
    fi

    if refresh_one_provider "$provider" "$now"; then
      any_ok=1
    fi
    block="$RENDER_BLOCK"
    [[ -n "${block:-}" ]] || continue

    blocks="$(jq -nc --argjson acc "$blocks" --arg p "$provider" --argjson b "$block" \
      '$acc + {($p): $b}' 2>/dev/null || printf '%s' "$blocks")"
  done

  if [[ "$blocks" == '{}' ]]; then
    log_debug "refresh: nothing written (every provider fresh, in backoff, or rate limited)"
    return 1
  fi

  write_usage_cache "$blocks" || return 1

  publish_to_tmux_opts || true

  if command -v tmux >/dev/null 2>&1; then
    local debug_opt
    debug_opt="$(tmux show-option -gqv @codexbar_debug 2>/dev/null || true)"

    if [[ "${debug_opt:-}" =~ ^[0-9]+$ ]] && (( debug_opt != 0 )); then
      local n
      n="$(tmux show-option -gqv "$debug_update_counter_opt" 2>/dev/null || true)"
      if ! [[ "${n:-}" =~ ^[0-9]+$ ]]; then
        n=0
      fi
      n=$(( n + 1 ))
      tmux set-option -gq "$debug_update_counter_opt" "$n" >/dev/null 2>&1 || true
    else
      tmux set-option -gq "$debug_update_counter_opt" 0 >/dev/null 2>&1 || true
    fi

    tmux refresh-client -S >/dev/null 2>&1 || true
  fi

  (( any_ok == 1 )) || return 1
  return 0
}

main() {
  local mode="${1:-}"

  case "$mode" in
    --refresh)
      refresh_cache || true
      publish_to_tmux_opts || true
      exit 0
      ;;
    --publish)
      publish_to_tmux_opts || true
      exit 0
      ;;
    --merge-live)
      merge_live_claude || true
      exit 0
      ;;
    --auth-required)
      cache_auth_required
      exit $?
      ;;
    --login)
      login_claude_oauth
      exit $?
      ;;
    --tick)
      # Debounce: the catppuccin modules all render in the same status tick, so
      # only the first --tick does work; the rest see a fresh marker and bail.
      local tick_marker="${CACHE_DIR}/last_tick"
      local tick_marker_age=999
      if [[ -f "$tick_marker" ]]; then
        local marker_ts
        marker_ts="$(stat -f %m "$tick_marker" 2>/dev/null || stat -c %Y "$tick_marker" 2>/dev/null || echo 0)"
        tick_marker_age=$(( $(now_epoch) - marker_ts ))
      fi
      if (( tick_marker_age < 2 )); then
        exit 0
      fi
      mkdir -p "$CACHE_DIR"

      # Wake / resume detection. Ticks normally arrive every status-interval
      # (a few seconds); a much larger gap means the machine was suspended
      # (laptop sleep). A refresh backoff is an absolute future epoch, so a
      # backoff armed before sleep keeps gating every post-wake auto-refresh
      # (for up to an hour) while only prefix+u — which bypasses backoff —
      # recovers it. Clear the backoff once on a detected wake so this tick can
      # attempt a refresh immediately. Fires only on a large gap, so
      # steady-state backoff behaviour is unchanged.
      if (( tick_marker_age >= WAKE_GAP_SECONDS )); then
        log_info "tick: wake/resume detected (gap=${tick_marker_age}s); resetting refresh backoff"
        # Every ladder, not just the displayed provider's: the one armed before
        # sleep is as likely to be the provider nobody is looking at.
        local wake_p
        for wake_p in $USAGE_PROVIDERS; do
          rm -f "$(backoff_file_for "$wake_p")" 2>/dev/null || true
        done
      fi

      : >"$tick_marker" 2>/dev/null || true

      # Best-effort reap of orphaned fetch temp files. The EXIT/INT/TERM/HUP
      # trap cannot run when a worker is SIGKILLed (tmux reports exit 137 /
      # signal 9 around sleep/wake), so tracked temp files can leak. Anything
      # older than 5 minutes is well past any live fetch (curl max-time <=30s).
      # The -name group MUST be parenthesized so -mmin/-delete apply to every
      # pattern, not just the last one.
      find "$CACHE_DIR" -maxdepth 1 -type f \
        \( -name 'codexbar.stderr.*' -o -name 'curl.cfg.*' -o -name 'oauth.body.*' \) \
        -mmin +5 -delete 2>/dev/null || true

      publish_to_tmux_opts || true
      # Backstop for a live sample whose own --merge-live found the lock busy.
      if [[ -f "$LIVE_SAMPLE_FILE" ]] && ! cmp -s "$LIVE_SAMPLE_FILE" "$LIVE_MERGED_MARKER"; then
        merge_live_claude || true
      fi
      if any_provider_refresh_due "$(now_epoch)"; then
        spawn_background_refresh_locked || true
      fi
      exit 0
      ;;
    --debug-flash-tick)
      debug_flash_tick "${2:-}"
      exit 0
      ;;
    session|weekly|scoped)
      :
      ;;
    fable)
      # The third window's key before it was named structurally. Still
      # accepted so a status line from an un-reloaded tmux.conf keeps working.
      mode='scoped'
      ;;
    *)
      usage
      exit 2
      ;;
  esac

  start_debug_flash_loop_if_needed

  print_value "$mode"

  if any_provider_refresh_due "$(now_epoch)"; then
    mkdir -p "$CACHE_DIR"
    log_debug "stale: spawn refresh"
    spawn_background_refresh_locked || true
  fi
}

main "$@"
