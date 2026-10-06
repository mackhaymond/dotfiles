# tmux 3.7c: cursor jumps during synchronized updates

tmux 3.7 started honouring applications' synchronized output (DECSET 2026) and
answering DECRQM 2026, so Claude Code and Codex now wrap every redraw in a sync
block. While a pane is mid-sync tmux freezes its contents, but
`server_client_reset_state()` — run on every event-loop pass — still moved the
outer terminal's cursor to the app's *in-progress* cursor position. Result: the
cursor flashes around the screen on every agent redraw.

Fixed upstream on master by 57a13664 ("Do not allow cursor on/off to escape
synchronized updates", 2026-09-01) and 5e5e15f6 (2026-09-28); neither is in
3.7c. Master also removes popups, so `brew install --HEAD` is not an option.

`tmux-3.7c-sync-cursor.patch` is the backport (server-client.c only).
`tmux.rb` is Homebrew's formula with that patch inlined. Verified 2026-10-06:
with a raw-byte recording of an attached client, stock 3.7c sends the mid-sync
`ESC[5;10H` every cycle and the patched build sends none; final cursor
position, popups, splits and copy-mode cursor unchanged.

## Install (local tap, build from source)

    brew tap-new --no-git mackhaymond/local
    cp ~/.config/tmux/patches/tmux.rb "$(brew --repository mackhaymond/local)/Formula/tmux.rb"
    brew uninstall --ignore-dependencies tmux     # tmux is missing until the build finishes
    brew install --build-from-source mackhaymond/local/tmux

The running server keeps its old binary; the fix applies from the next server
start. Clients and server of the same version interoperate, so nothing needs
restarting right away.

## Undo (also: once a tmux release ships 57a13664)

    brew uninstall --ignore-dependencies mackhaymond/local/tmux
    brew untap mackhaymond/local
    brew install tmux
