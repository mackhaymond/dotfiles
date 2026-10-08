#!/usr/bin/env bash
# Prepend the stash indicator to the left end of catppuccin's right-hand status.
#
# WHY NOT A CATPPUCCIN MODULE. It was one, and being first is exactly what broke
# it. catppuccin bakes each module's left-separator background from its position
# in the list: only the FIRST module gets bg=default so its powerline arc meets
# the bare status bar, everything after it gets #313244. The separator is
# U+E0B2 + █, and the arc's negative space SHOWS that background — so a module
# that renders nothing most of the time cannot sit at the front of that chain:
# it permanently demotes `directory` to a #313244 arc, which is then wrong on
# the bare bar for as long as nothing is parked (i.e. nearly always).
#
# So catppuccin keeps `directory` first, and this segment is prepended OUTSIDE
# the chain — but prepending alone is not enough. A powerline chain joins
# because each arc is drawn over the PREVIOUS module's background; directory's
# arc is drawn over bg=default, so with a pill to its left the arc's negative
# space showed the bare bar and left a dark wedge between the two (measured: a
# 3px #1e1e2d notch between #313243 and the pink arc).
#
# Hence the one surgical edit below: directory's leading bg becomes a format
# that follows whether this segment is showing. Formats inside #[...] style
# specs are fine — window-status-format in tmux.conf is built entirely from
# them. This segment then ends ON #313244 rather than capping back to default,
# so the join is seamless when shown and untouched when hidden.
set -uo pipefail

cur=$(tmux show -gv status-right 2>/dev/null) || exit 0
# Idempotent: sourcing the config re-runs tpm, which rebuilds status-right from
# scratch — but if it ever doesn't, this must not stack a second copy.
case "$cur" in *stash_count*) exit 0 ;; esac

opt() { local v; v=$(tmux show -gv "$1" 2>/dev/null); [ -n "$v" ] && printf '%s' "$v" || printf '%s' "$2"; }

sep=$(opt @catppuccin_status_left_separator '█')
color=$(opt @catppuccin_stash_color '#f9e2af')
thm_bg=$(opt @thm_bg '#1e1e2e')
thm_fg=$(opt @thm_fg '#cdd6f4')
thm_gray=$(opt @thm_gray '#313244')

# Commas inside #[...] are escaped as `#,` because this whole thing sits in a
# #{?...} branch, and tmux splits those on commas — an unescaped #[fg=x,bg=y]
# ends the branch at `fg=x` and the rest is dropped silently, rendering nothing.
red=$(opt @thm_red '#f38ba8')

# STALE-SAVE CHIP, leftmost: "󰀦 last save 3h ago" in red while the global
# @resurrect_stale is set. agent-tab-watcher.sh sets it (default server only)
# when the newest resurrect snapshot is older than @resurrect-stale-minutes,
# and resurrect-save.sh clears it as a save lands — continuum once went ~38 h
# without saving and nothing on screen said so. Same anatomy as the stash chip
# and the same chaining rule: whatever sits to its right draws its arc over
# #313244 while it shows.
stale_seg="#[fg=${red}#,bg=default#,nobold#,nounderscore#,noitalics]${sep}"
stale_seg="${stale_seg}#[fg=${thm_bg}#,bg=${red}#,nobold#,nounderscore#,noitalics]󰀦 "
# resurrect-save.sh sets the value `refused` when its default-server guard
# turned a save away; that reads "save refused" instead of an age.
stale_seg="${stale_seg}#[fg=${thm_fg}#,bg=${thm_gray}] #{?#{==:#{@resurrect_stale},refused},save refused,last save #{@resurrect_stale} ago} "
stale_cond="#{?#{@resurrect_stale},${stale_seg},}"

seg="#[fg=${color}#,bg=#{?#{@resurrect_stale},${thm_gray},default}#,nobold#,nounderscore#,noitalics]${sep}"
seg="${seg}#[fg=${thm_bg}#,bg=${color}#,nobold#,nounderscore#,noitalics] "
seg="${seg}#[fg=${thm_fg}#,bg=${thm_gray}] 󰒲 #{E:@stash_count} "

# Empty when nothing is parked — a hidden tab is the only thing this reports,
# so at zero it should not cost a cell. Unset counts as zero (before the first
# publish).
showing="#{||:#{==:#{E:@stash_count},0},#{==:#{E:@stash_count},}}"
cond="#{?${showing},,${seg}}"

# The first `bg=default` in catppuccin's string is the leading module's arc —
# the only one drawn against the bare bar, and so the only one that has to
# change when something sits to its left (the stale chip or the stash chip).
# Replacing just that one occurrence leaves every other module's chaining
# exactly as catppuccin built it. The replacement is QUOTED: bash 5.2's
# patsub_replacement would otherwise read any `&` in it as the matched text.
dir_bg="bg=#{?#{@resurrect_stale},${thm_gray},#{?${showing},default,${thm_gray}}}"
patched="${cur/bg=default/"$dir_bg"}"

tmux set -g status-right "${stale_cond}${cond}${patched}"
