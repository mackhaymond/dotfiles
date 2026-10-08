#!/usr/bin/env bash
# Keep tmux-continuum's autosave hook in status-right, on the default server.
#
# WHY. continuum schedules its 15-minute saves through a #(continuum_save.sh)
# interpolation in status-right. catppuccin rebuilds status-right from scratch
# on every config load, and continuum only puts its hook back
# `if ! another_tmux_server_running` - a check that counts this user's
# processes whose command line STARTS WITH "tmux" and compares that with the
# number of attached clients. Agents here routinely start `tmux -L <name>` test
# servers; three of them left orphaned on 2026-10-06 08:10 outnumbered the one
# real client, so from the next reload on continuum silently declined to add
# its hook and NO autosave ran for ~38 h (found 2026-10-08). The real server
# does not even count: its command line is /opt/homebrew/bin/tmux.
#
# So the hook is re-asserted here, after tpm, bypassing that check - but ONLY
# for the default server. The check exists for a reason: a test server that
# sources this config must never autosave into ~/.tmux/resurrect (it would
# move `last` to a snapshot of the test server). run-shell jobs inherit
# TMUX="<socket>,<pid>,<session>", so the socket name says which server this
# is; an empty TMUX means the tmux CLI targets the default server anyway (same
# rule as agent-tab-watcher.sh).
set -uo pipefail

_sock="${TMUX%%,*}"
[ -z "$_sock" ] || [ "${_sock##*/}" = default ] || exit 0

save="$HOME/.config/tmux/plugins/tmux-continuum/scripts/continuum_save.sh"
[ -x "$save" ] || exit 0

cur=$(tmux show -gv status-right 2>/dev/null) || exit 0
# Idempotent: continuum may have added it itself (no stray servers).
case "$cur" in *continuum_save.sh*) exit 0 ;; esac
tmux set-option -g status-right "#($save)$cur"
