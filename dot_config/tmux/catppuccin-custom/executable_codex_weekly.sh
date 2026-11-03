show_codex_weekly() {
  local index icon color text module

  tmux_batch_setup_status_module "codex_weekly"
  run_tmux_batch_commands

  index=$1
  # With no 5-hour session (@codex_session_limit "0") codex_session draws
  # nothing, and this module leads the usage segment instead, so the account
  # label moves here: "ihave27ki… W:". Otherwise the plain "W:". The label is
  # tested with != against empty, as codex_session does: "0" is a legal alias.
  icon=$(get_tmux_batch_option "@catppuccin_codex_weekly_icon" "#{?#{==:#{@codex_session_limit},0},#{?#{!=:#{@codex_account_label},},#{@codex_account_label} ,},}W:")
  text=$(get_tmux_batch_option "@catppuccin_codex_weekly_text" "#{?@codex_weekly_text,#{@codex_weekly_text},--%%}#(#{HOME}/.config/tmux/scripts/codexbar-usage-status.sh --tick >/dev/null 2>&1 || true)")
  color=$(get_tmux_batch_option "@catppuccin_codex_weekly_color" "#{@codex_weekly_color}")

  module=$(build_status_module "$index" "$icon" "$color" "$text")

  echo "$module"
}
