show_codex_session() {
  local index icon color text module

  tmux_batch_setup_status_module "codex_session"
  run_tmux_batch_commands

  index=$1
  # The session module leads the usage segment, so it also says whose usage
  # it is: "ihave27ki… S:" — the active Claude account's cswap alias or email
  # local part, published by the refresher as @codex_account_label (empty for
  # Codex, with no login, or with @codexbar_account_label_max 0, which leaves
  # the plain "S:"). Tested with != against empty, not for truth: tmux reads
  # a value of "0" as false, and "0" is a legal alias.
  icon=$(get_tmux_batch_option "@catppuccin_codex_session_icon" "#{?#{!=:#{@codex_account_label},},#{@codex_account_label} ,}S:")
  text=$(get_tmux_batch_option "@catppuccin_codex_session_text" "#{?@codex_session_text,#{@codex_session_text},--%%}#(#{HOME}/.config/tmux/scripts/codexbar-usage-status.sh --tick >/dev/null 2>&1 || true)")
  color=$(get_tmux_batch_option "@catppuccin_codex_session_color" "#{@codex_session_color}")

  module=$(build_status_module "$index" "$icon" "$color" "$text")

  # An account with no 5-hour session window at all (@codex_session_limit
  # "0", published by the refresher for a weekly-only Claude account or a
  # Codex plan without a primary window) draws no S module, and codex_weekly
  # takes the account label. "1" and "" (not known yet: another account's
  # numbers right after a switch, no cache) draw it as always. The weekly
  # module runs the same --tick, so hiding this one stops nothing.
  #
  # The module goes inside a conditional, where a top-level comma would end
  # the branch: the #[fg=…,bg=…] styles' commas are escaped as "#,". Commas
  # inside a nested #{…} are NOT — tmux skips over those by brace depth, and
  # escaping them would break the nested conditionals.
  echo "#{?#{==:#{@codex_session_limit},0},,$(codex_session_escape_commas "$module")}"
}

# "#," for every comma outside #{…}, the way tmux's format_skip reads a
# conditional's branches: #{ opens a level, } closes one, and #, ## #} #:
# are two-character escapes taken as they stand.
codex_session_escape_commas() {
  local s="$1" out='' depth=0 i c next
  for (( i = 0; i < ${#s}; i++ )); do
    c="${s:i:1}"
    next="${s:i+1:1}"
    if [[ "$c" == '#' && -n "$next" ]]; then
      case "$next" in
        '{') depth=$(( depth + 1 )); out+='#{'; i=$(( i + 1 )); continue ;;
        ','|'#'|'}'|':') out+="#$next"; i=$(( i + 1 )); continue ;;
      esac
    fi
    if [[ "$c" == '}' ]]; then
      (( depth > 0 )) && depth=$(( depth - 1 ))
    elif [[ "$c" == ',' ]] && (( depth == 0 )); then
      out+='#,'
      continue
    fi
    out+="$c"
  done
  printf '%s' "$out"
}
