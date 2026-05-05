show_codex_session() {
  local index icon color text module

  tmux_batch_setup_status_module "codex_session"
  run_tmux_batch_commands

  index=$1
  icon=$(get_tmux_batch_option "@catppuccin_codex_session_icon" "S:")
  text=$(get_tmux_batch_option "@catppuccin_codex_session_text" "#{?@codex_session_text,#{@codex_session_text},--%%}#(#{HOME}/.config/tmux/scripts/codexbar-usage-status.sh --tick >/dev/null 2>&1 || true)")
  color=$(get_tmux_batch_option "@catppuccin_codex_session_color" "#{@codex_session_color}")

  module=$(build_status_module "$index" "$icon" "$color" "$text")

  echo "$module"
}
