show_codex_scoped() {
  local index icon color text module

  tmux_batch_setup_status_module "codex_scoped"
  run_tmux_batch_commands

  index=$1
  # The third window is named by whichever provider is being displayed — the
  # model-scoped weekly cap on Claude ("Fable" -> "F:"), the reserve pool on
  # Codex ("gpt-reserve" -> "G:") — so the refresher publishes the letter with
  # the number. "F:" is the fallback for a cache that predates it.
  icon=$(get_tmux_batch_option "@catppuccin_codex_scoped_icon" "#{?@codex_scoped_icon,#{@codex_scoped_icon},F:}")
  text=$(get_tmux_batch_option "@catppuccin_codex_scoped_text" "#{?@codex_scoped_text,#{@codex_scoped_text},--%%}#(#{HOME}/.config/tmux/scripts/codexbar-usage-status.sh --tick >/dev/null 2>&1 || true)")
  color=$(get_tmux_batch_option "@catppuccin_codex_scoped_color" "#{@codex_scoped_color}")

  module=$(build_status_module "$index" "$icon" "$color" "$text")

  echo "$module"
}
