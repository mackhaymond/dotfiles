# Codex terminal ownership

Codex 0.160's terminal UI connects to a shared app-server. Hook processes run
under that server, so its `TMUX`, `TMUX_PANE` and PID do not identify the terminal
showing a particular thread. A stale pane followed by CuaNotch's old directory
fallback could lead to an unrelated tool PTY.

`~/.local/bin/codex` wraps interactive tmux launches with a private Unix socket
relay to the native `codex app-server proxy`. It preserves the shared server and
forwards the HTTP upgrade and WebSocket frames unchanged. Successful client start/resume/fork
responses and explicit turn-start requests provide exact thread IDs. Broadcasts,
thread reads, titles, directory names, and tool-created PTYs cannot claim panes.

`codex-terminal-owner resolve THREAD_ID` returns a verified binding or `unbound`.
The registry is `~/.cache/codex-terminal-owners/bindings.json`, written atomically
under a lock. A binding includes the frontend PID and start time, terminal TTY,
tmux socket, pane, pane shell PID, and invocation token. Hook consumers validate
these identities, rather than retaining stale environment values. One pane has
one foreground thread; switching conversations supersedes the old binding.

The tmux indicator, CuaNotch hook and `codex-session-track` all consume this
binding. Detached title condensers check ownership again before writing. Notch
navigation and acknowledgment require the exact socket/pane for Codex; an
unresolved session remains visible but cannot jump to a guessed terminal.

Initial SessionStart may run before a thread/start reply supplies the thread
ID. The bridge reconciles ownership/title after the reply without downgrading
newer status. No hook definitions are changed and no hook-trust bypass is used.

Noninteractive commands, explicit `--remote` and `--no-daemon`, and launches
outside tmux retain native argument handling. A failed bridge launch prints a
short warning and falls back to native Codex; unverified ownership stays unbound.
The relay blocks on I/O while idle and does not add a polling daemon.

## Verification and recovery

- `codex --version` should report the installed CLI unchanged.
- `codex-terminal-owner resolve THREAD_ID` should name its actual frontend and
  socket/pane. `unbound` is safer than guessing.
- `tmux list-windows -a -F '#{window_id} #{@agent_state} #{@agent_summary}'`
  shows the status/title output.
- Tests live under `~/.config/tmux/tests/`; CuaNotch's `dev/test-codex-owner`
  checks its hook and navigation policy without moving windows.
- Existing CLI processes predate the wrapper. They may be bound explicitly with
  `codex-terminal-owner bind THREAD_ID FRONTEND_PID SOCKET PANE` only after
  verifying the thread displayed in that terminal. New shell launches use the
  wrapper automatically; a shell with a cached Codex path may need `rehash`.
- Do not restart the shared server to fix a pane association: it would disrupt
  every attached session and would still leave ownership ambiguous.

Managed tmux scripts and launchers are stored in chezmoi. CuaNotch's source is
in its own repository; deploy its reviewed hook and executable together, without
accidentally including unrelated work in the bundle.
