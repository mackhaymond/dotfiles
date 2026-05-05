show_codex_weekly() {
  local index icon color text module

  tmux_batch_setup_status_module "codex_weekly"
  run_tmux_batch_commands

  index=$1
  icon=$(get_tmux_batch_option "@catppuccin_codex_weekly_icon" "W:")
  text=$(get_tmux_batch_option "@catppuccin_codex_weekly_text" "#{?@codex_weekly_text,#{@codex_weekly_text},--%%}#(#{HOME}/.config/tmux/scripts/codexbar-usage-status.sh --tick >/dev/null 2>&1 || true)")
  color=$(get_tmux_batch_option "@catppuccin_codex_weekly_color" "#{@codex_weekly_color}")

  module=$(build_status_module "$index" "$icon" "$color" "$text")

  echo "$module"
}
