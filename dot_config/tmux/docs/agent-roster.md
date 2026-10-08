# Agent roster (`prefix q`)

A herdr-style list of every agent across every tmux session, in one popup.
It is a **reader**: everything it shows comes from the per-window options the
hook pipeline already maintains (see [agent-tab-indicator.md](agent-tab-indicator.md)).
It keeps no state and runs only while the popup is open.

| Key | Does |
|---|---|
| `prefix q` / `prefix C-q` | open the roster |
| Tab / Shift-Tab, `j` / `k`, arrows | move |
| Space / ⏎ | go there (a parked tab comes back via `stash.sh unstash`) |
| `d` | next tab that needs you (same as `prefix d`) |
| `x` | close the agent's pane through `closed-tabs.sh`, after y/n; CMD+Z undoes it. On a parked row: discard it through `stash.sh kill-many` |
| `H` | park the tab (`stash.sh stash`, which suspends the agent), after y/n |
| `/` | filter by `session:index title`; ⏎ keeps it, esc clears it |
| `a` | also show windows with no agent |
| `r` | restart the watcher (only needed when the red banner says so) |
| esc / `q` | close |

Related keys: `prefix d` / `prefix C-d` jump to the next tab that needs you,
`prefix D` jumps back (scripts/agent-jump.sh).

## Layout

- **NEEDS YOU** first, in exactly the order `agent-jump.sh list` returns:
  failed > needs-input > done (not done while a workflow runs), oldest
  `@agent_since` first, then session name (in the locale's collation, as
  `sort` does: `_x` < `a` < `B` under en_US.UTF-8), then window index. Ties
  follow /usr/bin/sort (FreeBSD's) as well: names with no collation weight
  (emoji, Greek, Cyrillic, CJK) compare equal, so the SHORTER name goes
  first, not code point order, and rows equal on every key fall back to the
  whole line, where the window id decides. The `EXCLUDE` sessions are dropped, a linked window listed once. The roster computes
  this in-process (`needs_order`) from the same `list-windows` rows instead of
  forking the script every tick; `NeedsOrderTests` in
  tests/test_agent_roster.py runs the real `agent-jump.sh list` against a fake
  tmux and requires the identical order (under en_US.UTF-8 and C), and checks
  the roster's `JUMP_EXCLUDE` against the script's `EXCLUDE` line. Change the
  order in agent-jump.sh and that test fails until `needs_order` follows.
- Then one group per session, the client's own session first, then most
  recently used. `agents`, `tasks` (CuaNotch's broker), `scratch` and
  `btop-popup` are never shown, the same sessions `agent-jump.sh` and the
  session pickers skip.
- Then `parked (n)`, collapsed, with its own needs-you count.
- Windows with no agent are hidden until `a`, **except the window the popup is
  covering**. A prompt that lands under the popup is discharged as "seen" by
  the watcher (the window is active for a client and WezTerm is frontmost), so
  without this row it would vanish from the list as well as from the tab.

Each row shows: the peach bar (the window this client is on), a state dot in
the tab bar's hues, `session:index` (NEEDS YOU) or the index, the title
(`@agent_summary`, else the window name), the gear or mouse glyph, the state,
and the time since `@agent_since`.

## Mechanics

- Bound through `run-shell -C`, because `display-popup` does not expand
  formats in its command and the roster has to be told which client opened it
  (`--client #{client_tty}`). See [Launch speed](#launch-speed).
- Once a second, while open, and before the first frame: ONE tmux call,
  `list-windows -a -F … \; list-clients -F …` (US-separated fields; client
  rows are tagged and have 4 fields, window rows 14). NEEDS YOU and the
  client's current window both come out of that one snapshot.
- Moves go through `agent-jump.sh goto|next`: `select-window` then
  `switch-client`, so the visit discharges the tint like a tab click does.
- `x` finds the agent's pane by TTY, the way the watcher does: `list-panes`
  (`pane_id`, `pane_tty`, `pane_active`) plus one `ps -ax -o tty=,comm=`, run
  only on `x` and only for a split window, matching a comm of `claude`,
  `codex` or a bare version like `2.1.291`. `pane_current_command` is not
  used: codex launched through npm shows there as `node`. A one-pane window
  closes that pane; a split window where no pane runs an agent is refused
  ("can't tell which pane is the agent · close it from the tab") rather than
  guessed at.
- Keys: a read that ends in a bare ESC or a cut-off sequence (`ESC [`,
  `ESC [1;`) waits 25 ms for the rest; only silence makes a lone ESC close the
  popup. Alt+key is ignored, and UTF-8 is decoded incrementally across reads.
- The y/n after `x` / `H` has to come in a later read than the key that asked
  (a paste or a fast "Hy" never confirms itself), and `y` re-checks the
  window: if it was closed, parked or unparked in between, nothing runs and
  the footer says so.
- Every line is clipped to one cell short of the popup width (emoji and VS16
  sequences count as two cells), so nothing autowraps.
- Watcher health: the mtime of `$TMPDIR/agent-tab-watcher.$UID.pid`, which the
  watcher restamps every tick. Older than 30 s (ensure_watcher's grace) shows
  a red banner; `r` respawns the watcher the same way `prefix r` does.
- Drawn with raw ANSI truecolour, not curses: inside tmux, curses only gets the
  256-colour palette, and these hues have to match the tab bar and CuaNotch
  exactly. The pulse reads `@agent_blink`, so it beats in step with the tabs.

## Launch speed

Goal: the first frame well under 100 ms after the keypress. Measured
2026-10-07 on this machine (169 windows), keypress → first frame via
`send-keys -K` against a private `tmux -L` server and a fake client, with
the send-keys client's own round trip subtracted:

| | before | after |
|---|---|---|
| keypress → first frame, load ~4-7 | ~94 ms | ~35 ms (fallback python: ~42 ms) |

Where it went, per stage (medians at load ~4; at load ~200 from unrelated
jobs every stage roughly doubles):

| Stage | before | after |
|---|---|---|
| `run-shell` job through `zsh -c` | ~21 ms | gone: `run-shell -C` runs a tmux command, no shell |
| `tmux display-popup` client process | ~6 ms | gone |
| popup command through `zsh -c` | ~21 ms | gone: argv form is exec'd; `/bin/dash` picks python, ~3 ms |
| python start + the roster's imports | `/usr/bin/python3` (xcrun stub → 3.9): ~28 ms | Homebrew 3.14 `-I -S`: ~21 ms |
| `tmux list-windows -a` | ~9 ms | ~9 ms, one call with `list-clients` |
| `bash agent-jump.sh list` | ~15 ms | gone: `needs_order`, ~0.1 ms |
| `tmux list-clients` | ~7 ms | (in the call above) |
| parse + NEEDS YOU + render 150×40 | ~2 ms | ~1.5 ms |

The `zsh -c` rows are mostly ~/.zshenv's keychain lookup for the 1Password
token: the tmux server's environment does not carry it, so every `zsh -c` the
server forks runs `security` (`zsh -f -c true` is ~4 ms). The old binding
paid that twice.

Interpreter notes, the reasons behind the binding:

- Never a pyenv shim (`~/.pyenv/shims/python3`: 140-280 ms just to start).
- `/usr/bin/python3` is the xcrun stub. It sets `PYTHONPYCACHEPREFIX`
  (`~/Library/Caches/com.apple.python`), because the 3.9 stdlib is read-only:
  run with `-I` or `-E` (or the framework binary directly) and that prefix is
  ignored, so every launch recompiles every stdlib module it imports (the
  roster went from ~45 to ~85 ms). The fallback is therefore `-S` only.
- Homebrew's python3 runs `-I -S` (no user site, no env, no site.py).
- The roster stays Python 3.9-compatible, since 3.9 is the fallback.

Not done, if it ever needs the last ~10 ms: the snapshot could be expanded by
the binding itself (`#{S:#{W:…}}` loops) and passed in, removing the tmux
round trip before the first frame. It would put arbitrary window titles
through tmux command parsing, so it was left out.

## Troubleshooting

- **Popup flashes and vanishes**: run it by hand in a pane to see the error:
  `/opt/homebrew/bin/python3 -I -S ~/.config/tmux/scripts/agent-roster.py --client "$(tmux display -p '#{client_tty}')"`
  (or `/usr/bin/python3 -S …` when Homebrew's python is missing).
  Careful: Space/⏎/`g` in that copy really do move that client.
- **Red "client is gone" banner**: the tty passed in no longer matches an
  attached client (for example, the terminal was reattached). Close the popup and reopen it.
- **Ages all read the same**: `@agent_since` ("<epoch> <state>") is stamped at
  each transition by the hook's `set_state` (agent-tab-indicator.sh). The
  watcher is the backstop: it stamps changes that bypass the hook (the seen-it
  discharge, the stuck-running reconcile, the GC) and seeds windows it finds
  with no stamp (the idle seed). So every window the watcher seeded at once,
  for example on first deploy, shows that same moment. Ages become accurate
  from the next state change.
