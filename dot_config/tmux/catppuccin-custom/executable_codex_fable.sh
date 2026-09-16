# SUPERSEDED BY codex_scoped.sh — kept because a tmux server that has not
# re-sourced tmux.conf since the rename still has "codex_fable" in its module
# list, and catppuccin would render nothing at all for a module whose file has
# gone. The refresher publishes @codex_fable_text/@codex_fable_color as
# aliases of the scoped ones for exactly as long as this file exists.
show_codex_fable() {
  local index icon color text module

  tmux_batch_setup_status_module "codex_fable"
  run_tmux_batch_commands

  index=$1
  icon=$(get_tmux_batch_option "@catppuccin_codex_fable_icon" "F:")
  text=$(get_tmux_batch_option "@catppuccin_codex_fable_text" "#{?@codex_fable_text,#{@codex_fable_text},--%%}#(#{HOME}/.config/tmux/scripts/codexbar-usage-status.sh --tick >/dev/null 2>&1 || true)")
  color=$(get_tmux_batch_option "@catppuccin_codex_fable_color" "#{@codex_fable_color}")

  module=$(build_status_module "$index" "$icon" "$color" "$text")

  echo "$module"
}
