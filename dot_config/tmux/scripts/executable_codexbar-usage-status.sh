#!/usr/bin/env bash

set -euo pipefail


CACHE_DIR="${HOME}/.cache/codexbar-tmux"
CACHE_FILE="${CACHE_DIR}/usage.json"
LOCKDIR="${CACHE_FILE}.lock"

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

USAGE_PROVIDER="$(opt_or_env_or_default '@codexbar_provider' 'CODEXBAR_USAGE_PROVIDER' 'claude')"
case "$USAGE_PROVIDER" in
  claude|codex) ;;
  *) USAGE_PROVIDER='claude' ;;
esac

BACKOFF_FILE="${CACHE_DIR}/refresh_backoff_${USAGE_PROVIDER}"

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

  local prev_session prev_weekly nonce
  prev_session="$(tmux show-option -gqv @codex_session_color 2>/dev/null || true)"
  prev_weekly="$(tmux show-option -gqv @codex_weekly_color 2>/dev/null || true)"

  nonce="$(date +%s%N 2>/dev/null || date +%s)"

  tmux set-option -gq @codexbar_debug_flash_nonce "$nonce" >/dev/null 2>&1 || true
  tmux set-option -gq @codexbar_debug_flash_prev_session_color "$prev_session" >/dev/null 2>&1 || true
  tmux set-option -gq @codexbar_debug_flash_prev_weekly_color "$prev_weekly" >/dev/null 2>&1 || true

  tmux set-option -gq @codex_session_color "$flash_color" >/dev/null 2>&1 || true
  tmux set-option -gq @codex_weekly_color "$flash_color" >/dev/null 2>&1 || true
  tmux refresh-client -S >/dev/null 2>&1 || true
  log_debug "flash: on color=${flash_color}"

  tmux run-shell -b "sleep 0.5; n=\$(tmux show-option -gqv @codexbar_debug_flash_nonce 2>/dev/null); [ \"\$n\" = \"$nonce\" ] || exit 0; fc='$flash_color'; cs=\$(tmux show-option -gqv @codex_session_color 2>/dev/null || true); cw=\$(tmux show-option -gqv @codex_weekly_color 2>/dev/null || true); s=\$(tmux show-option -gqv @codexbar_debug_flash_prev_session_color 2>/dev/null); w=\$(tmux show-option -gqv @codexbar_debug_flash_prev_weekly_color 2>/dev/null); if [ \"\$cs\" = \"\$fc\" ]; then if [ -n \"\$s\" ]; then tmux set-option -gq @codex_session_color \"\$s\"; else tmux set-option -gu @codex_session_color; fi; fi; if [ \"\$cw\" = \"\$fc\" ]; then if [ -n \"\$w\" ]; then tmux set-option -gq @codex_weekly_color \"\$w\"; else tmux set-option -gu @codex_weekly_color; fi; fi; tmux refresh-client -S;" >/dev/null 2>&1 || true
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

usage() {
  printf '%s\n' "Usage: $0 {session|weekly|--refresh|--debug-flash-tick <nonce>}" >&2
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

record_refresh_backoff_failure() {
  mkdir -p "$CACHE_DIR" 2>/dev/null || true

  local fail_count next_allowed now delay
  read -r fail_count next_allowed < <(read_refresh_backoff)

  fail_count=$(( fail_count + 1 ))
  delay="$(refresh_backoff_delay_seconds "$fail_count")"
  now="$(now_epoch)"
  next_allowed=$(( now + delay ))

  log_warn "backoff: fail_count=${fail_count} next_attempt_in=${delay}s provider=${USAGE_PROVIDER}"

  umask 077
  printf '%s %s\n' "$fail_count" "$next_allowed" >"$BACKOFF_FILE" 2>/dev/null || true
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

weekly_pace_suffix() {
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

  local expected_used delta_sign delta_abs
  expected_used="$(awk -v e="$elapsed" -v d="$duration" 'BEGIN { if (d <= 0) { print "0"; exit } printf "%.6f", (e / d) * 100 }')"


  read -r delta_sign delta_abs < <(
    awk -v a="$actual_used_percent" -v e="$expected_used" 'BEGIN {
      d = a - e
      if (d < 0) { sign = "-"; d = -d } else { sign = "+" }
      printf "%s %d", sign, int(d + 0.5)
    }'
  )

  printf ' (%s%s%%)' "$delta_sign" "$delta_abs"
}

cache_updated_at() {
  [[ -f "$CACHE_FILE" ]] || { printf '%s' 0; return 0; }

  local ts
  ts="$(stat -f %m "$CACHE_FILE" 2>/dev/null || stat -c %Y "$CACHE_FILE" 2>/dev/null || true)"
  if [[ "$ts" =~ ^[0-9]+$ ]]; then
    printf '%s' "$ts"
  else
    printf '%s' 0
  fi
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
      [[ "$mode" == 'weekly' ]] && printf '%s' 'reset' || printf '%s' "$PRINT_VIEW_BASELINE"
      ;;
  esac
}

load_cache_fields() {
  CACHE_SESSION_TEXT=''
  CACHE_WEEKLY_TEXT=''
  CACHE_SESSION_COLOR=''
  CACHE_WEEKLY_COLOR=''
  CACHE_SESSION_RESETS=''
  CACHE_WEEKLY_RESETS=''

  [[ -f "$CACHE_FILE" ]] || return 0
  command -v jq >/dev/null 2>&1 || return 0

  local parsed
  parsed="$(jq -r '[.session_text//"", .weekly_text//"", .session_color//"", .weekly_color//"", .session_resets_at//"", .weekly_resets_at//""] | @tsv' "$CACHE_FILE" 2>/dev/null || true)"
  [[ -n "$parsed" ]] || return 0
  IFS=$'\t' read -r CACHE_SESSION_TEXT CACHE_WEEKLY_TEXT CACHE_SESSION_COLOR CACHE_WEEKLY_COLOR CACHE_SESSION_RESETS CACHE_WEEKLY_RESETS <<<"$parsed"
}

render_text_for_mode() {
  local mode="$1" view="$2" debug_suffix="$3"
  local out=''

  if [[ "$view" == "reset" ]]; then
    local now resets_at=''
    now="$(now_epoch)"
    case "$mode" in
      session) resets_at="$CACHE_SESSION_RESETS" ;;
      weekly)  resets_at="$CACHE_WEEKLY_RESETS" ;;
    esac
    out="$(format_time_until_reset "$resets_at" "$now")"
  else
    case "$mode" in
      session) out="$CACHE_SESSION_TEXT" ;;
      weekly)  out="$CACHE_WEEKLY_TEXT" ;;
    esac
    if [[ -z "$out" ]]; then
      out="--%"
    else
      out="$(strip_legacy_label_prefix "$out")"
    fi
  fi

  printf '%s%s' "$out" "$debug_suffix"
}

publish_to_tmux_opts() {
  command -v tmux >/dev/null 2>&1 || return 0

  load_print_context
  load_cache_fields

  local debug_suffix=''
  if (( CODEXBAR_USAGE_DEBUG != 0 )); then
    local c="$PRINT_DEBUG_COUNTER"
    [[ "$c" =~ ^[0-9]+$ ]] || c=0
    debug_suffix=" d${c}"
  fi

  local session_view weekly_view session_text weekly_text
  session_view="$(effective_view_from_context session)"
  weekly_view="$(effective_view_from_context weekly)"
  session_text="$(render_text_for_mode session "$session_view" "$debug_suffix")"
  weekly_text="$(render_text_for_mode weekly  "$weekly_view"  "$debug_suffix")"

  tmux set-option -gq @codex_session_text "$session_text" >/dev/null 2>&1 || true
  tmux set-option -gq @codex_weekly_text  "$weekly_text"  >/dev/null 2>&1 || true

  if [[ -n "$CACHE_SESSION_COLOR" ]]; then
    tmux set-option -gq @codex_session_color "$CACHE_SESSION_COLOR" >/dev/null 2>&1 || true
  fi
  if [[ -n "$CACHE_WEEKLY_COLOR" ]]; then
    tmux set-option -gq @codex_weekly_color "$CACHE_WEEKLY_COLOR" >/dev/null 2>&1 || true
  fi
}

print_value() {
  local mode="$1" view debug_suffix=''

  load_print_context
  load_cache_fields

  view="$(effective_view_from_context "$mode")"

  if (( CODEXBAR_USAGE_DEBUG != 0 )); then
    local c="$PRINT_DEBUG_COUNTER"
    [[ "$c" =~ ^[0-9]+$ ]] || c=0
    debug_suffix=" d${c}"
  fi

  if command -v tmux >/dev/null 2>&1; then
    case "$mode" in
      session) [[ -n "$CACHE_SESSION_COLOR" ]] && tmux set-option -gq @codex_session_color "$CACHE_SESSION_COLOR" >/dev/null 2>&1 || true ;;
      weekly)  [[ -n "$CACHE_WEEKLY_COLOR" ]]  && tmux set-option -gq @codex_weekly_color  "$CACHE_WEEKLY_COLOR"  >/dev/null 2>&1 || true ;;
    esac
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
        log_warn "lock: killing wedged worker pid=${recorded_pid} age=${age}s"
        kill -TERM "$recorded_pid" 2>/dev/null || true
        sleep 1
        if kill -0 "$recorded_pid" 2>/dev/null; then
          kill -KILL "$recorded_pid" 2>/dev/null || true
        fi
      else
        log_warn "lock: pid ${recorded_pid} reused by unrelated process; skipping kill"
      fi
    fi
    rm -rf "$LOCKDIR" 2>/dev/null || true
  fi
}

try_acquire_lock() {
  local pid_value="${1:-STARTING}"

  clear_stale_lock_if_needed

  if mkdir "$LOCKDIR" 2>/dev/null; then
    printf '%s\n' "$(now_epoch)" >"$LOCKDIR/started_at" 2>/dev/null || { rm -rf "$LOCKDIR" 2>/dev/null || true; return 1; }
    printf '%s\n' "$pid_value" >"$LOCKDIR/pid" 2>/dev/null || { rm -rf "$LOCKDIR" 2>/dev/null || true; return 1; }
    return 0
  fi

  clear_stale_lock_if_needed
  mkdir "$LOCKDIR" 2>/dev/null || return 1
  printf '%s\n' "$(now_epoch)" >"$LOCKDIR/started_at" 2>/dev/null || { rm -rf "$LOCKDIR" 2>/dev/null || true; return 1; }
  printf '%s\n' "$pid_value" >"$LOCKDIR/pid" 2>/dev/null || { rm -rf "$LOCKDIR" 2>/dev/null || true; return 1; }
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

CLAUDE_OAUTH_KEYCHAIN_SERVICE='Claude Code-credentials'
CLAUDE_OAUTH_REFRESH_LOCKDIR="${CACHE_DIR}/oauth-refresh.lock"
CLAUDE_OAUTH_TOKEN_URL='https://platform.claude.com/v1/oauth/token'
CLAUDE_OAUTH_CLIENT_ID='9d1c250a-e61b-44d9-88ed-5944d1962f5e'

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
    log_warn_trunc "oauth-refresh: malformed response: ${response}" 300
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

FETCH_SESSION_USED=''
FETCH_WEEKLY_USED=''
FETCH_SESSION_WINDOW_MINUTES=''
FETCH_WEEKLY_WINDOW_MINUTES=''
FETCH_SESSION_RESETS_AT=''
FETCH_WEEKLY_RESETS_AT=''

reset_fetch_outputs() {
  FETCH_SESSION_USED=''
  FETCH_WEEKLY_USED=''
  FETCH_SESSION_WINDOW_MINUTES=''
  FETCH_WEEKLY_WINDOW_MINUTES=''
  FETCH_SESSION_RESETS_AT=''
  FETCH_WEEKLY_RESETS_AT=''
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
  fetch_out="$(codexbar --provider claude --format json --json-only --web-timeout "$WEB_TIMEOUT_SECONDS" 2>"$stderr_file")"
  local fetch_status=$?
  set -e
  fetch_err="$(cat "$stderr_file" 2>/dev/null || true)"

  if (( fetch_status != 0 )); then
    if [[ "$fetch_out" == *"Unknown option --json-only"* || "$fetch_err" == *"Unknown option --json-only"* ]]; then
      : >"$stderr_file" 2>/dev/null || true
      set +e
      fetch_out="$(codexbar --provider claude --format json --web-timeout "$WEB_TIMEOUT_SECONDS" 2>"$stderr_file")"
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

  local normalized session_raw weekly_raw
  if ! normalized="$(printf '%s' "$fetch_out" | jq -ser '[.[] | (if type=="array" then .[0] else . end)] | map(select(.usage?)) | .[0]' 2>/dev/null)"; then
    log_warn_trunc "refresh[codex]: jq parse failure (normalize) out=${fetch_out}" 300
    return 1
  fi

  if ! session_raw="$(printf '%s' "$normalized" | jq -er '.usage.primary.usedPercent | tonumber' 2>/dev/null)"; then
    log_warn "refresh[codex]: jq parse failure (session usedPercent)"
    return 1
  fi
  if ! weekly_raw="$(printf '%s' "$normalized" | jq -er '.usage.secondary.usedPercent | tonumber' 2>/dev/null)"; then
    log_warn "refresh[codex]: jq parse failure (weekly usedPercent)"
    return 1
  fi

  FETCH_SESSION_USED="$session_raw"
  FETCH_WEEKLY_USED="$weekly_raw"
  FETCH_SESSION_WINDOW_MINUTES="$(printf '%s' "$normalized" | jq -er '.usage.primary.windowMinutes // empty | tonumber' 2>/dev/null || true)"
  FETCH_WEEKLY_WINDOW_MINUTES="$(printf '%s' "$normalized" | jq -er '.usage.secondary.windowMinutes // empty | tonumber' 2>/dev/null || true)"

  local iso
  iso="$(printf '%s' "$normalized" | jq -er -r '.usage.primary.resetsAt // empty | tostring' 2>/dev/null || true)"
  if [[ -n "${iso:-}" ]]; then
    FETCH_SESSION_RESETS_AT="$(iso_utc_to_epoch "$iso" 2>/dev/null || true)"
  fi
  iso="$(printf '%s' "$normalized" | jq -er -r '.usage.secondary.resetsAt // empty | tostring' 2>/dev/null || true)"
  if [[ -n "${iso:-}" ]]; then
    FETCH_WEEKLY_RESETS_AT="$(iso_utc_to_epoch "$iso" 2>/dev/null || true)"
  fi

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
  token="$(read_claude_oauth_access_token 2>/dev/null || true)"
  if [[ -z "${token:-}" ]]; then
    log_warn "refresh[claude]: keychain token unavailable"
    return 1
  fi

  raw="$(fetch_claude_oauth_usage_json "$token" 2>/dev/null || true)"
  if [[ -z "${raw:-}" ]]; then
    log_warn "refresh[claude]: empty oauth response"
    return 1
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
      return 1
    fi
    if claude_oauth_response_is_auth_error "$raw"; then
      log_warn "refresh[claude]: auth error persists after refresh attempt"
      return 1
    fi
  fi

  if ! printf '%s' "$raw" | jq -e '.five_hour and .seven_day' >/dev/null 2>&1; then
    log_warn_trunc "refresh[claude]: missing usage fields out=${raw}" 300
    return 1
  fi

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

  return 0
}

refresh_cache() {
  mkdir -p "$CACHE_DIR"
  log_debug "refresh: start provider=${USAGE_PROVIDER}"

  if [[ "${CODEXBAR_USAGE_LOCK_HELD:-}" == "1" && "${CODEXBAR_USAGE_LOCKDIR:-}" == "$LOCKDIR" ]]; then
    log_debug "refresh: lock inherited"
    printf '%s\n' "$$" >"$LOCKDIR/pid" 2>/dev/null || true
    printf '%s\n' "$(now_epoch)" >"$LOCKDIR/started_at" 2>/dev/null || true
  else
    if ! try_acquire_lock "$$"; then
      log_debug "refresh: lock busy"
      return 0
    fi
    log_debug "refresh: lock acquired"
  fi
  trap 'release_lock; cleanup_tmp_files' EXIT INT TERM HUP

  if ! command -v jq >/dev/null 2>&1; then
    log_debug "refresh: missing tool jq"
    refresh_fail
    return 1
  fi

  case "$USAGE_PROVIDER" in
    claude)
      fetch_via_claude_oauth || { refresh_fail; return 1; }
      ;;
    codex)
      fetch_via_codexbar_codex || { refresh_fail; return 1; }
      ;;
    *)
      log_debug "refresh: unknown provider ${USAGE_PROVIDER}"
      refresh_fail
      return 1
      ;;
  esac

  local session_window_minutes="$FETCH_SESSION_WINDOW_MINUTES"
  local weekly_window_minutes="$FETCH_WEEKLY_WINDOW_MINUTES"
  local session_resets_at="$FETCH_SESSION_RESETS_AT"
  local weekly_resets_at="$FETCH_WEEKLY_RESETS_AT"

  local session_window_minutes_json session_resets_at_json weekly_window_minutes_json weekly_resets_at_json
  session_window_minutes_json='null'
  session_resets_at_json='null'
  weekly_window_minutes_json='null'
  weekly_resets_at_json='null'

  if [[ "$session_window_minutes" =~ ^[0-9]+$ ]]; then
    session_window_minutes_json="$session_window_minutes"
  fi
  if [[ "$session_resets_at" =~ ^[0-9]+$ ]]; then
    session_resets_at_json="$session_resets_at"
  fi
  if [[ "$weekly_window_minutes" =~ ^[0-9]+$ ]]; then
    weekly_window_minutes_json="$weekly_window_minutes"
  fi
  if [[ "$weekly_resets_at" =~ ^[0-9]+$ ]]; then
    weekly_resets_at_json="$weekly_resets_at"
  fi

  local session_used weekly_used
  session_used="$(clamp_0_100_int "$FETCH_SESSION_USED")" || { refresh_fail; return 1; }
  weekly_used="$(clamp_0_100_int "$FETCH_WEEKLY_USED")" || { refresh_fail; return 1; }

  local updated_at
  updated_at="$(now_epoch)"

  local weekly_pace
  weekly_pace="$(weekly_pace_suffix "$weekly_used" "$weekly_window_minutes" "$weekly_resets_at" "$updated_at")"

  local session_text weekly_text session_color weekly_color
  session_text="${session_used}%"
  weekly_text="${weekly_used}%${weekly_pace}"
  session_color="$(color_for_used_percent "$session_used")"
  weekly_color="$(color_for_used_percent "$weekly_used")"

  local tmp
  tmp="$(mktemp "${CACHE_DIR}/usage.json.tmp.XXXXXX")"

  umask 077
  cat >"$tmp" <<EOF
{"updated_at":${updated_at},"session_used":${session_used},"weekly_used":${weekly_used},"session_window_minutes":${session_window_minutes_json},"session_resets_at":${session_resets_at_json},"weekly_window_minutes":${weekly_window_minutes_json},"weekly_resets_at":${weekly_resets_at_json},"session_windowMinutes":${session_window_minutes_json},"session_resetsAt":${session_resets_at_json},"weekly_windowMinutes":${weekly_window_minutes_json},"weekly_resetsAt":${weekly_resets_at_json},"session_text":"${session_text}","weekly_text":"${weekly_text}","session_color":"${session_color}","weekly_color":"${weekly_color}"}
EOF

  mv -f "$tmp" "$CACHE_FILE"

  if command -v tmux >/dev/null 2>&1; then
    tmux set-option -gq @codex_session_color "$session_color" >/dev/null 2>&1 || true
    tmux set-option -gq @codex_weekly_color "$weekly_color" >/dev/null 2>&1 || true

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

  log_info "refresh: success updated_at=${updated_at} session=${session_used}% weekly=${weekly_used}% provider=${USAGE_PROVIDER}"

  reset_refresh_backoff
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
    --tick)
      # Debounce: when both catppuccin modules render in the same status tick,
      # only the first --tick does work; the second sees a fresh marker and bails.
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
      : >"$tick_marker" 2>/dev/null || true

      publish_to_tmux_opts || true
      local tick_ts tick_now tick_age
      tick_ts="$(cache_updated_at)"
      tick_now="$(now_epoch)"
      tick_age=$(( tick_now - tick_ts ))
      if [[ ! -f "$CACHE_FILE" ]] || (( tick_age >= STALE_AFTER_SECONDS )); then
        local fc na
        read -r fc na < <(read_refresh_backoff)
        if (( tick_now >= na )); then
          spawn_background_refresh_locked || true
        fi
      fi
      exit 0
      ;;
    --debug-flash-tick)
      debug_flash_tick "${2:-}"
      exit 0
      ;;
    session|weekly)
      :
      ;;
    *)
      usage
      exit 2
      ;;
  esac

  start_debug_flash_loop_if_needed

  print_value "$mode"

  local ts now age
  ts="$(cache_updated_at)"
  now="$(now_epoch)"
  age=$(( now - ts ))

  if [[ ! -f "$CACHE_FILE" ]] || (( age >= STALE_AFTER_SECONDS )); then
    mkdir -p "$CACHE_DIR"

    log_debug "stale: now=${now} ts=${ts} age=${age} threshold=${STALE_AFTER_SECONDS}"

    local fail_count next_allowed
    read -r fail_count next_allowed < <(read_refresh_backoff)
    if (( now < next_allowed )); then
      log_debug "stale: backoff fail_count=${fail_count} next_allowed=${next_allowed}"
      return 0
    fi

    log_debug "stale: spawn refresh"
    spawn_background_refresh_locked || true
  fi
}

main "$@"
