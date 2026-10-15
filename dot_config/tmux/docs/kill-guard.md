# kill-guard: Touch ID (and a verified save) before the real server dies

On 2026-10-15 at 09:40 a subagent ran a zsh script under bash. The array
`T=(tmux -L u2btest -f /dev/null)` collapsed to plain `tmux`, so `$T kill-server`
killed Mack's real default server and every session in it. The kill guard makes
that class of accident (runtime expansion, the wrong shell, a dropped `-L`) ask
for Touch ID first. It also makes sure the state is saved before the kill:

> "I want tmux kill server to literally not be allowed without saving
> everything first. At least in regards to my primary server instance. Spare
> servers I don't really care about." (Mack)

The threat model is accidents by agents and scripts. It does not try to stop a
determined adversary: anything that can run `/opt/homebrew/bin/tmux kill-ser`
or `kill <server pid>` can still do it. See the gaps below.

## Pieces

| piece | source (chezmoi) | installed as |
|---|---|---|
| guard script | `dot_config/tmux/scripts/executable_kill-guard.sh` | `~/.config/tmux/scripts/kill-guard.sh` |
| server alias | one line in `tmux.conf.tmpl` (below) | server option `command-alias[90]` |
| CLI shim (C) | `dot_config/tmux/kill-guard/tmux-shim.c` | `~/.local/bin/tmux` |
| Touch ID helper (Swift) | `dot_config/tmux/kill-guard/touchid.swift` | `~/.local/bin/tmux-touchid` |
| build | `.chezmoiscripts/run_onchange_after_build-tmux-kill-guard.sh.tmpl` | runs on `chezmoi apply` when a source hash changes |
| tests | `dot_config/tmux/tests/test_kill_guard.py` | |
| decision log | | `~/.local/state/tmux-kill-guard/log` (rotated to `.1` at 1 MB) |

"Protected" means the real default socket, which is exactly
`/tmp/tmux-$UID/default` (realpath `/private/tmp/...`). Mack never sets
`TMUX_TMPDIR`. When `TMUX_TMPDIR` points elsewhere, a bare `tmux` reaches a
different server, and that server is by definition not the real one. Agents'
private test servers work this way: every tmux-testing subagent exports
`TMUX_TMPDIR=<scratch>`, and those servers load the real tmux.conf, alias
included. Their `kill-server` kills them at once, with no prompt and no save,
because spare servers must die freely. `TMUX_TMPDIR` still decides which socket
a CLI call *reaches* (below). It never decides what is protected, so it can't
make the real `/tmp/tmux-$UID/default` look like a spare server either.

Two sockets count as the same if their canonical paths match. They also count
as the same if both exist and share a device:inode (`stat -L -f %d:%i`), so
`-S ~/link-to-default` is the real default too. `KILL_GUARD_PROTECT_SOCKET`
adds one more socket to the protected list. It can only add protection, never
remove it.

### 1. Server side: `kill-server` alias

This is the line for `tmux.conf.tmpl`. chezmoi renders the absolute paths.

```tmux
set -s 'command-alias[90]' "kill-server=run-shell '[ -x {{ .chezmoi.homeDir }}/.config/tmux/scripts/kill-guard.sh ] && exec {{ .chezmoi.homeDir }}/.config/tmux/scripts/kill-guard.sh server #{q:socket_path}; case #{q:socket_path} in */default) echo \"kill-guard: kill-guard.sh missing; refusing to kill the default server (bypass: {{ .homebrew_prefix }}/bin/tmux kill-ser)\"; exit 1;; esac; exec {{ .homebrew_prefix }}/bin/tmux -S #{q:socket_path} kill-ser'"
```

It does not depend on `#{HOME}` or on the script existing:

* If the guard is present, the alias execs it.
* If the guard is missing, a `*/default` socket is refused with a message, and
  any other server is killed directly. This keeps a missing script from blocking
  spare servers.

On tmux 3.7c, the alias catches an exact `kill-server` from every client: the
plain CLI, `\;` chains, `source-file`, key bindings and the command prompt.
`run-shell` without `-b` blocks the issuing client until the guard finishes.
Its stdout becomes the client's output, and a non-zero exit becomes a client
error, so a refused CLI `tmux kill-server` prints why and exits 1. stderr from
run-shell is dropped, so the guard writes its refusal to stdout.

`kill-guard.sh server SOCKET` runs these steps:

1. **Not protected** (any test or spare server that happens to load tmux.conf):
   run `/opt/homebrew/bin/tmux -S SOCKET kill-ser` at once. No prompt, no save.
2. **Protected**: Touch ID. The reason names the requester: the
   `tmux ... kill-server` CLI process and its parent chain from `ps`, for
   example `tmux kill-server [123] <- zsh [100] <- claude [90]`.
   `#{client_pid}` is empty for CLI clients, and run-shell's `#{client_name}`
   falls back to the most recently active attached client, which is Mack's own
   tty. Neither is used: without a CLI process, the reason says "key binding,
   command prompt, hook or config file". A denial, a timeout or an unavailable
   prompt refuses the kill and exits 1.
3. **Save first** (below). Any failure refuses the kill.
4. `exec /opt/homebrew/bin/tmux -S SOCKET kill-ser`. The guard's own kill uses
   the ABBREVIATION, which no alias matches, through the ABSOLUTE path, which
   skips the shim, so it never recurses. The tests confirm that an approved
   kill takes exactly one guard invocation. If the exec itself fails
   (`execfail`), the guard unsets the flag and exits 1.

tmux's own shutdown paths run no command, so they are unaffected. When the
last pane exits, or the last session is killed and `exit-empty` ends the
server, the guard is never invoked. The tests confirm this as well.

### 2. CLI side: the `tmux` shim

`~/.local/bin/tmux` comes before `/opt/homebrew/bin` on PATH, both in Mack's
shells and in tmux.conf's `set-environment -g PATH` for everything the server
spawns.

zsh caches command paths. A long-lived shell, such as a pty-MCP shell, that ran
`tmux` before the shim was installed keeps the cached `/opt/homebrew/bin/tmux`
until it runs `rehash` or `hash -r`, so `whence -p tmux` is the check. New
shells, `run-shell` jobs and Claude Code's per-call Bash are unaffected. The
full-word `kill-server` doesn't depend on the shim, because the server-side alias
catches it whatever PATH resolves to.

* **Fast path.** No argument contains `kill-se`, so it calls
  `execv("/opt/homebrew/bin/tmux", argv)`. There is no allocation and no
  environment parsing. The cost is one extra exec: about **+1.4 ms per call**
  (1000 interleaved `display -p x` calls against a test server took 6.2-6.4 ms
  direct and 7.6-7.9 ms through the shim).
* **Slow path.** `exec /bin/bash kill-guard.sh cli ARGS...`. The substring
  `kill-se` covers `kill-session`, `kill-server` and every unique prefix of
  either. `kill-se` itself is ambiguous and tmux rejects it. On an unprotected
  socket the slow path costs about 13 ms against about 6 ms for real tmux.
* **Fail open.** If the guard is missing or bash can't start, the shim prints
  `running tmux UNGUARDED` on stderr and runs the real tmux. A broken guard
  must not break the status line, scripts and agents. The server alias still
  guards the full-word `kill-server`.

`kill-guard.sh cli` parses tmux's global flags (`2c:CDdf:hlL:NqS:T:uUvV`,
including clustered forms like `-u2L name`) and resolves the socket the same
way tmux does: `-S`, then `-L` under `${TMUX_TMPDIR:-/tmp}/tmux-$UID`, then
`$TMUX` (ignored when it starts with `,`, as tmux does), then the default.
`-c` (shell-command mode) passes through. **Not protected** means `exec` the
real tmux unchanged. On a protected socket it splits the command list on a
standalone `;` or a trailing unescaped `;` (`\;` is a literal), then checks
each command:

| command | verdict |
|---|---|
| `kill-session -C` (clears alerts only) | pass |
| `kill-session -a` | **protect** |
| `kill-session [-t T]`, target is in `@kill-guard-protect` (server option, space-separated, default `main`) | **protect** |
| `kill-session [-t T]`, target is the last session left (counting earlier kills in the same chain) | **protect** (the server would exit) |
| any other `kill-session` (e.g. `ptymcp-*` throwaways, stash, btop popup) | pass |
| `kill-ser`, `kill-serv`, `kill-serve` (abbreviated kill-server; the alias matches only the full word) while the server is up | **protect** |
| full `kill-server` | pass, and the server alias asks. **Exception:** if the server has no kill-server alias (started without tmux.conf), the shim protects it, so there is never a double prompt and never a gap |
| unparseable (unknown flag, missing value, stray args: tmux would error anyway) | pass |

The target is resolved the way kill-session itself resolves it:
`list-windows [-t T] -F '#{session_name}…'`, first line, under the caller's own
environment. `list-windows -t` does a SESSION-type lookup, which handles
`=name`, `name:`, `$id` and unique prefixes. With no `-t` it picks the current
session. `display -t` would be the wrong choice because it does a PANE lookup:
`main` can match a window called main in the caller's session, `=main` errors,
and `-t c` from inside main can match main's window `claude`.

A protected list gets **one** Touch ID prompt for the whole invocation, then the
save-first steps. After that the guard runs the real tmux with the original
arguments as a child, not via exec. When that returns,
`@kill_guard_shutdown` is unset again unless the server went away.

### 3. Save first (protected kills only)

After Touch ID approves:

1. `set -g @kill_guard_shutdown <epoch seconds>` on that server. stash.sh,
   resurrect-save.sh and resurrect-save-soon.sh treat the flag as set **only
   while it is younger than 120 s**. While it is set they skip their work, so
   teardown hooks can't wipe the state that was just saved. A flag left behind
   by a crash, a SIGKILL, or a `kill-session … \; attach` chain that never
   returns therefore expires on its own instead of stopping autosaves forever.
   From this point until the kill, a `trap` on INT/TERM/HUP unsets the flag,
   stops the running save step and exits 1.
2. `~/.config/tmux/scripts/stash.sh publish` runs synchronously, which mirrors
   the parked-tab sidecar. Cap: 10 s.
3. `~/.config/tmux/scripts/resurrect-save.sh final` runs as a full,
   synchronous save that waits for the save lock. It gets whatever is left of
   the 30 s budget, at least 5 s.
4. Verify:
   * the exit status was 0;
   * the target of `@resurrect-dir/last` has an mtime at or after step 3's
     start;
   * `save.log` has gained a new `saved N panes` line.
5. Only then kill. If steps 2-4 fail or time out, the guard unsets the flag and
   refuses. It says why on stdout or stderr and in a `display-message`, logs
   it, and exits non-zero.

**How the steps run.** Steps 2 and 3 run under a small perl supervisor. It puts
the step in its own process group and, on the deadline or when the guard is
signalled, sends the whole group TERM and then KILL, so post-save hooks don't
linger. macOS has no `timeout(1)`. The guard waits on the supervisor in the
background, so a signal is handled at once rather than after the step ends.
The scripts need bash 4 or later (`declare -A`, `EPOCHREALTIME`), and their
`#!/usr/bin/env bash` shebang would pick macOS's 3.2 for a caller whose PATH
lacks Homebrew. So the guard runs them with an explicit bash:
`/opt/homebrew/bin/bash` if present, else the first bash on PATH that reports
`BASH_VERSINFO >= 4`. If there is none, the kill is refused.

**Environment for the steps.** The children run with `TMUX=<that socket>`, no
`TMUX_PANE`, and a UTF-8 locale. The socket is given by its canonical path:
`/private/tmp/tmux-$UID/default` for the real server even when the caller used
`-S ~/link-to-default`, because the scripts check for the default server by
path. The locale is forced to `LANG`/`LC_CTYPE`
`en_US.UTF-8` unless `LC_ALL`/`LC_CTYPE`/`LANG` already give UTF-8, because a
non-UTF-8 client prints the `\x1f` separators in `-F` output as `_`. They save
the server that is being killed, whatever the caller's environment said. Their
output goes to the guard log.

**Not covered by any of this:** a reboot, logout or shutdown sends SIGTERM
straight to the server. No command runs and nothing can save first. The
per-minute saves bound that loss.

### 4. Touch ID helper

`tmux-touchid "REASON"` evaluates `LAContext.evaluatePolicy(.deviceOwnerAuthentication)`:
Touch ID, with the login password as fallback. macOS shows the dialog as
"tmux-touchid is trying to REASON." The helper has a hard 60 s timeout, after
which it invalidates the context and denies. Exit codes: 0 approved, 1 denied
or timed out, 2 unavailable (no GUI session, as over ssh; no passcode; policy
can't be evaluated). The guard treats 2 and a missing helper as **refuse**.

### Overrides (tests)

| env | effect | real default socket (incl. symlinks to it) |
|---|---|---|
| `KILL_GUARD_AUTH=approve` | skip Touch ID, approve | **ignored**: Touch ID still asks (logged as `auth-override-ignored`) |
| `KILL_GUARD_AUTH=deny` | refuse without prompting | honoured (refusing is always safe) |
| `KILL_GUARD_SAVE_CMD` / `KILL_GUARD_PUBLISH_CMD` | stub the save and publish steps | **ignored**: the real scripts run |
| `KILL_GUARD_PROTECT_SOCKET=PATH` | treat PATH as protected | n/a (it only adds protection) |
| `KILL_GUARD_DRYRUN=1` | print `kill-guard: verdict=passthrough\|protect socket=…` and exit 0. **Nothing is executed**, not even an unprotected pass-through: no real tmux command, prompt, save or kill (server mode included). Only read-only queries run (`list-windows` for the target, `show` for the alias); abbreviations skip the liveness probe | honoured (does less) |
| `KILL_GUARD_LOG=PATH` | decision log location | honoured |

`kill-guard.sh auth-source SOCKET` and `kill-guard.sh save-source SOCKET` are
read-only. They print which authority, and which save/publish commands, a kill
on SOCKET would use. The tests use them to prove the overrides are ignored for
the real default socket without touching that server.

## Gaps (honest list)

* **Absolute path.** `/opt/homebrew/bin/tmux kill-session -t main` skips the
  shim, so kill-session and the abbreviations are unguarded. The abbreviations
  never reach the alias, and the shim sees them only for callers that resolve
  `tmux` through PATH, so `/opt/homebrew/bin/tmux kill-ser` is uncovered.
  The full word `kill-server` is still caught by the alias even through the
  absolute path.
* **Signals.** `kill <server pid>`, `pkill tmux`, `killall tmux`, and a
  reboot or logout (SIGTERM, no save).
* **kill-window / kill-pane / respawn-pane -k** of the last pane in the last
  session: the server exits through `exit-empty` and no kill command runs.
  `unlink-window`, `move-window` and similar are not guarded either.
* **Commands inside other commands.** `tmux if-shell true 'kill-session -t main'`,
  `confirm-before`, `bind`, `command-prompt` templates, `source-file` of a
  file containing `kill-session`, and control mode (`tmux -C`, commands on
  stdin) all bypass the CLI parse. They still reach the alias if they use the
  full word `kill-server`.
* **run-shell / hooks inside tmux** use the shim only while the server's
  `PATH` has `~/.local/bin` first. tmux.conf sets it, but a server started
  without tmux.conf, or a hook with an absolute tmux path, bypasses the shim.
* **No GUI** (ssh, screen locked with no password fallback, LaunchAgent
  contexts without an Aqua session): the helper returns "unavailable" and
  protected kills are **refused**. That is fail-closed by design; use the
  bypass below.
* **Requester naming** is best effort. It is the first `tmux` process whose
  argv mentions `kill-server`, and it is generic for key bindings, the
  command prompt, hooks and config files.
* **SIGKILL of the guard** between flag and kill can't run the trap. The flag
  stays set but expires after 120 s, because readers check its age.

## Emergency bypass (Mack)

These go around the guard on purpose. None of them saves first.

```sh
# whole server, no prompt, no save (the abbreviation skips the alias; the
# absolute path skips the shim)
/opt/homebrew/bin/tmux kill-ser
# one session
/opt/homebrew/bin/tmux kill-session -t main
# save by hand first if the server still answers
~/.config/tmux/scripts/resurrect-save.sh final
```

To switch the whole guard off, remove the alias
(`/opt/homebrew/bin/tmux set -su 'command-alias[90]'`) and delete
`~/.local/bin/tmux`. The next `chezmoi apply` restores both.

## Testing

```sh
cd ~/.local/share/chezmoi/dot_config/tmux && python3 -m pytest tests/test_kill_guard.py -q
```

The tests compile the shim into a temp dir and render the alias line above with
a temp guard path. They only start `/opt/homebrew/bin/tmux -L unitB-kg-<pid>-<n>`
servers, whose panes run `sleep`. They set `HOME` to a temp dir, so no path can
reach the installed Touch ID helper. They kill every server, wait for it to
exit and remove its socket.

A real Touch ID check, safe for the real server. The test server's own
environment carries the protect-socket override, and there is no auth override:

```sh
cat > /tmp/kg-alias.conf <<'EOF'
set -s 'command-alias[90]' 'kill-server=run-shell "$HOME/.config/tmux/scripts/kill-guard.sh server #{q:socket_path}"'
set -g default-command 'exec sleep 3600'
EOF
env -u TMUX -u TMUX_PANE KILL_GUARD_PROTECT_SOCKET=/private/tmp/tmux-$(id -u)/kgtouch \
    KILL_GUARD_PUBLISH_CMD=true \
    KILL_GUARD_SAVE_CMD='d=$HOME/.tmux/kgtouch; mkdir -p $d; f=$d/s.txt; date >$f; ln -sfn s.txt $d/last; echo "saved 1 panes" >>$d/save.log' \
    /opt/homebrew/bin/tmux -L kgtouch -f /tmp/kg-alias.conf new -d -s main \; set -g @resurrect-dir '~/.tmux/kgtouch'
env -u TMUX /opt/homebrew/bin/tmux -L kgtouch kill-server; echo "rc=$?"   # Touch ID: cancel -> rc 1, server alive
env -u TMUX /opt/homebrew/bin/tmux -L kgtouch kill-server; echo "rc=$?"   # Touch ID: approve -> rc 0, server gone
/opt/homebrew/bin/tmux -L kgtouch kill-ser 2>/dev/null; rm -f /private/tmp/tmux-$(id -u)/kgtouch; rm -rf ~/.tmux/kgtouch /tmp/kg-alias.conf
```
