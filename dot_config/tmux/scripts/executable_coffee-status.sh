#!/usr/bin/env bash
#
# coffee-status.sh
#
# One-shot caffeination probe. Sets tmux user options @coffee_glyph and
# @coffee_color based on whether any `caffeinate` process is currently
# keeping the Mac awake (Raycast's Coffee extension drives caffeinate -u -mi
# under the hood; manual `caffeinate` invocations are also picked up).
#
# The catppuccin coffee module reads @coffee_color for its chip color and
# @coffee_glyph for the glyph shown in its text section. Emits no stdout.

set -u

readonly GLYPH_ON=$'\xf3\xb1\x82\x9f'    # U+F109F nf-md-coffee_maker          (filled pot)
readonly GLYPH_OFF=$'\xf3\xb1\xa0\x9b'   # U+F181B nf-md-coffee_maker_outline  (empty pot)

readonly COLOR_ON='#fab387'    # peach - catppuccin's orange, warm/caffeinated
readonly COLOR_OFF='#74c7ec'   # sky   - cool pastel blue, chill/decaffeinated

if command -v pgrep >/dev/null 2>&1 && pgrep -xq caffeinate 2>/dev/null; then
  glyph="$GLYPH_ON"
  color="$COLOR_ON"
else
  glyph="$GLYPH_OFF"
  color="$COLOR_OFF"
fi

if ! command -v tmux >/dev/null 2>&1; then
  exit 0
fi

current_glyph=$(tmux show-option -gqv '@coffee_glyph' 2>/dev/null || true)
current_color=$(tmux show-option -gqv '@coffee_color' 2>/dev/null || true)

if [[ "$current_glyph" != "$glyph" ]]; then
  tmux set-option -g '@coffee_glyph' "$glyph" >/dev/null 2>&1 || true
fi
if [[ "$current_color" != "$color" ]]; then
  tmux set-option -g '@coffee_color' "$color" >/dev/null 2>&1 || true
fi

exit 0
