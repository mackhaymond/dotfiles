#!/usr/bin/env bash
# fzf preview for mru-session-switch.sh. Mirrors tmux's native choose-tree
# preview: shows the bottom-left rectangle of the session's active pane that
# fits in the fzf preview window.
#
# Invoked once per fzf highlight change with the session name as $1.
# fzf provides $FZF_PREVIEW_COLUMNS and $FZF_PREVIEW_LINES sized to the
# preview pane.

set -euo pipefail

session="${1:-}"
[[ -n "$session" ]] || exit 0

# Mirror the validation in mru-session-switch.sh — never let an
# attacker-controlled string into a tmux target.
[[ "$session" =~ ^[A-Za-z0-9._-]+$ ]] || exit 0
[[ "$session" == "scratch" ]] && exit 0

tmux has-session -t "=$session" 2>/dev/null || exit 0

cols="${FZF_PREVIEW_COLUMNS:-80}"
lines="${FZF_PREVIEW_LINES:-24}"

# Capture the active pane's visible viewport with SGR escapes, take the
# bottom N rows, then truncate+pad each row to W visible columns. Perl
# (not awk) because BSD awk counts bytes-not-graphemes, so multibyte
# glyphs (Nerd Font, box drawing, emoji) make the right edge jagged.
# /usr/bin/perl ships with macOS; -CSD enables UTF-8 on stdin/stdout.
tmux capture-pane -ep -t "=$session:" 2>/dev/null \
  | tail -n "$lines" \
  | W="$cols" perl -CSD -e '
      my $W = $ENV{W} || 80;

      # East-Asian-Width approximation: combining marks → 0, CJK / wide
      # emoji blocks → 2, everything else → 1. Covers the cases that
      # actually appear in dev terminals; perfect Unicode width would
      # need Text::CharWidth which is not in core perl.
      sub cw {
          my $cp = shift;
          return 0 if ($cp >= 0x0300 && $cp <= 0x036F)
                   || ($cp >= 0x200B && $cp <= 0x200F)
                   || ($cp >= 0xFE00 && $cp <= 0xFE0F)
                   ||  $cp == 0xFEFF;
          return 2 if ($cp >= 0x1100  && $cp <= 0x115F)
                   || ($cp >= 0x2E80  && $cp <= 0x4DBF)
                   || ($cp >= 0x4E00  && $cp <= 0x9FFF)
                   || ($cp >= 0xA000  && $cp <= 0xA4CF)
                   || ($cp >= 0xAC00  && $cp <= 0xD7A3)
                   || ($cp >= 0xF900  && $cp <= 0xFAFF)
                   || ($cp >= 0xFE30  && $cp <= 0xFE4F)
                   || ($cp >= 0xFF00  && $cp <= 0xFF60)
                   || ($cp >= 0x1F300 && $cp <= 0x1F64F)
                   || ($cp >= 0x1F680 && $cp <= 0x1F6FF)
                   || ($cp >= 0x1F900 && $cp <= 0x1F9FF)
                   || ($cp >= 0x1FA70 && $cp <= 0x1FAFF);
          return 1;
      }

      while (defined(my $line = <STDIN>)) {
          chomp $line;
          my @c   = split //, $line;
          my $n   = scalar @c;
          my $out = "";
          my $vis = 0;
          my $i   = 0;
          while ($i < $n && $vis < $W) {
              if ($c[$i] eq "\e") {
                  # CSI: ESC [ params... letter. Pass through, no width.
                  my $j = $i + 1;
                  if ($j < $n && $c[$j] eq "[") {
                      $j++;
                      $j++ while $j < $n && $c[$j] !~ /[A-Za-z]/;
                      $out .= join("", @c[$i..$j]);
                      $i = $j + 1;
                  } else {
                      $out .= $c[$i] . ($j < $n ? $c[$j] : "");
                      $i = $j + 1;
                  }
              } else {
                  my $w = cw(ord $c[$i]);
                  last if $vis + $w > $W;
                  $out .= $c[$i];
                  $vis += $w;
                  $i++;
              }
          }

          # capture-pane -e ends each line with \e[0m. Stripping it lets
          # the padding spaces inherit the line bg, matching tmux native
          # which paints cell bg to the grid edge.
          $out =~ s/\e\[0?m$//;
          $out .= " " x ($W - $vis) if $vis < $W;
          print $out, "\e[0m\n";
      }
    '
