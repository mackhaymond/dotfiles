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
# TMUX="<socket>,<pid>,<session>", so the socket path says which server this
# is; with TMUX empty, the server the tmux CLI reaches is asked. Only THE real
# default socket counts, by full path (/private/tmp/tmux-$UID/default as tmux
# reports it on macOS, or /tmp/tmux-$UID/default) — not any socket named
# `default`: agents' private servers live at $TMUX_TMPDIR/tmux-$UID/default.
set -uo pipefail

_sock="${TMUX:-}"
_sock="${_sock%%,*}"
[ -n "$_sock" ] || _sock="$(tmux display-message -p '#{socket_path}' 2>/dev/null)"
case "$_sock" in "/private/tmp/tmux-$UID/default" | "/tmp/tmux-$UID/default") ;; *) exit 0 ;; esac

save="$HOME/.config/tmux/plugins/tmux-continuum/scripts/continuum_save.sh"
[ -x "$save" ] || exit 0

cur=$(tmux show -gv status-right 2>/dev/null) || exit 0
# Idempotent: continuum may have added it itself (no stray servers).
case "$cur" in *continuum_save.sh*) exit 0 ;; esac
tmux set-option -g status-right "#($save)$cur"
