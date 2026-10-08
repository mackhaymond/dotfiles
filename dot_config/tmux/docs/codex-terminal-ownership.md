# Codex terminal ownership

Codex 0.160 can run a shared app-server daemon. Hooks for a frontend attached
to it run under the daemon, so their `TMUX`, `TMUX_PANE` and PID describe
whoever started the daemon, not the terminal showing the thread. Hook
consumers therefore never trust an inherited pane; they ask
`codex-terminal-owner resolve THREAD_ID` for a verified binding or `unbound`.

## Launch

`~/.local/bin/codex` runs `codex-max-context.py launch`, which adds
`-c model_context_window=<catalog maximum>` and hands off to
`codex-terminal-owner.py launch`. That execs the installed CLI (the first
`codex` on PATH after the wrapper) with `--no-daemon` added to interactive
launches, resumes and forks, so the TUI hosts its own backend and its hooks
run as its descendants. `--remote` is refused; noninteractive subcommands
(`exec`, `mcp`, `app-server`, ...) and `--help`/`--version` pass through
unchanged. No relay, socket or background process is started.

The wrapper must win PATH lookup. `.zprofile` ranks `~/.local/bin` above
`~/.bun/bin`; `.zshrc` restores that order for non-login shells (resurrect's
`exec zsh` panes inherit tmux's global PATH, which can list `~/.bun/bin`
first). The closed-tab reopen (`closed-tabs.sh`) and post-restore resume
(`assistant-restore.sh`) name `~/.local/bin/codex` explicitly.

## Resolve

`resolve` first checks the registry (`~/.cache/codex-terminal-owners/bindings.json`,
written atomically under a lock) for an explicit `bind` record and validates
it: frontend PID and start time, TTY, tmux socket, pane and pane shell PID
must all still match. One pane has one foreground thread; binding another
thread to it supersedes the old record.

Otherwise it derives a direct binding from the hook's own ancestry
(`direct_owner()`), never persisted:

- The thread must be a root user thread (`thread_source=user`, source `cli`
  or `vscode`, no `agent_path`) in `$CODEX_HOME/state_5.sqlite`.
- Walking up from the hook, the first `codex` process decides. A shared
  server (`--managed-daemon`, or any `app-server` not on `stdio://`) means
  unbound. An interactive frontend (`--no-daemon`, or argv that is not a
  noninteractive subcommand) is the owner if it sits on the TTY of
  `$TMUX_PANE` on `$TMUX`'s socket. Private helpers (a stdio app-server,
  `sandbox`) are walked through.
- With the npm/bun install the frontend PID is the native `codex` child of
  the `node .../codex` launcher. The token is
  `sha256("PID:START:SOCKET:PANE")` and the binding id `TOKEN:THREAD_ID`;
  `resurrect-save-repair.py` and CuaNotch compute the same values.

A bare `codex` (the raw CLI, no wrapper) binds the same way when no daemon is
running (`daemon_auto_start = false` keeps one from starting): it hosts its
backend in-process. If a daemon is running, the bare frontend attaches to it,
its hooks run under the daemon, and the thread stays unbound. That is why the
wrapper forces `--no-daemon`. Unbound is safer than guessing a pane.

## Consumers

The tmux indicator, CuaNotch hook and `codex-session-track` all consume the
resolved binding and exit quietly when it is unbound. Detached title
condensers check ownership again before writing. Notch navigation and
acknowledgment require the exact socket/pane for Codex; an unresolved session
remains visible but cannot jump to a guessed terminal. No hook definitions are
changed and no hook-trust bypass is used.

## Verification and recovery

- `whence -p codex` in a pane should print `~/.local/bin/codex`; a shell with
  a cached path may need `rehash`.
- `codex-terminal-owner resolve THREAD_ID`, run from outside Codex, only finds
  explicit `bind` records; direct bindings exist only for hooks. Check
  `tmux list-windows -a -F '#{window_id} #{@agent_kind} #{@agent_state}'`.
- A frontend that started bare against a daemon can be bound explicitly with
  `codex-terminal-owner bind THREAD_ID FRONTEND_PID SOCKET PANE` after
  verifying the thread displayed in that terminal; restarting it through the
  wrapper is simpler.
- Tests live under `~/.config/tmux/tests/` (`test_codex_*.py`); CuaNotch's
  `dev/test-codex-owner` checks its hook and navigation policy without moving
  windows.
- Do not restart the shared server to fix a pane association: it would disrupt
  every attached session and would still leave ownership ambiguous.

Managed tmux scripts and launchers are stored in chezmoi. CuaNotch's source is
in its own repository; deploy its reviewed hook and executable together, without
accidentally including unrelated work in the bundle.
