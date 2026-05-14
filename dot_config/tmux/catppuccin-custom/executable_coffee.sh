show_coffee() {
  tmux_batch_setup_status_module "coffee"
  run_tmux_batch_commands

  local color glyph thm_bg thm_fg thm_gray left_sep

  color=$(get_tmux_batch_option "@catppuccin_coffee_color" "#{E:@coffee_color}")
  glyph=$(get_tmux_batch_option "@catppuccin_coffee_glyph" "#{E:@coffee_glyph}")

  thm_bg=$(tmux show-option -gqv "@thm_bg" 2>/dev/null)
  : "${thm_bg:=#313244}"
  thm_fg=$(tmux show-option -gqv "@thm_fg" 2>/dev/null)
  : "${thm_fg:=#cdd6f4}"
  thm_gray=$(tmux show-option -gqv "@thm_gray" 2>/dev/null)
  : "${thm_gray:=#313244}"
  left_sep=$(tmux show-option -gqv "@catppuccin_status_left_separator" 2>/dev/null)
  : "${left_sep:=█}"

  # 3 cells of color: 2 from $left_sep (powerline arc + solid block) plus
  # one extra space rendered with bg=$color. catppuccin's build_status_module
  # only supports 0 or 2 extra color cells (icon="" or icon=" "), so we
  # hand-roll the format to land exactly on 3.
  echo "#[fg=${color},bg=${thm_bg},nobold,nounderscore,noitalics]${left_sep}#[fg=${thm_bg},bg=${color},nobold,nounderscore,noitalics] #[fg=${thm_fg},bg=${thm_gray}] ${glyph} "
}
