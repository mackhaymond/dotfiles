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

  echo "$module"
}
