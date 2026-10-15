# Spaces

A **space** is a tmux session. Most have a project folder (`session_path`);
`main` is the home space (`~`). In a space, an **agent** is a window with a
non-empty `@agent_state` (the agent-tab indicator sets it for every live
claude/codex window, idle included, and the GC clears it); every other window
is a **shell**. One shell per space is its **home** (`@space_home 1`).

## Files

| file | role |
|---|---|
| `tmux.conf.tmpl` | `set -g @spaces on` (the one flag) and `@space_hidden_sessions` (the one hidden list), near the top; `source-file -F "#{d:current_file}/spaces.conf"` as the last line |
| `spaces.conf` | everything else: hooks, status lines, keys. `%if @spaces on … %else … %endif` |
| `scripts/space.sh` | the logic a format can't do: `sync`, `agent`/`shell N`, `next`/`prev`, `toggle`, `close`, `move-menu`/`move-to`, `respawn-home`, `new-space`, `open-dir` |
| `tests/test_spaces.py` | real `-L` servers, keys sent with `send-keys -K`, clicks as SGR reports |
| `wezterm.lua.tmpl` | the digit rows: CTRL+N and CMD+N send prefix keys (below) |

Why a second file: `%if` is decided when a file is *parsed*, before any of its
commands run, so a `%if` on `@spaces` inside tmux.conf would see the value
from the previous load. By the time tmux.conf's `source-file` runs, its `set`
has.

`space.sh` refuses everything but `preview` when `$TMUX` is empty: outside a
tmux server, a plain `tmux` reaches the DEFAULT server, whichever one the
caller meant. (A hand-run test closed a real space that way on 2026-10-15.)
Keys, hooks and popups always have `$TMUX`; the tests set it.

## The flag

`set -g @spaces off` + `prefix r` gives back the pre-spaces config: same
`list-keys` (notes included), same `status`/`status-format`, with ONE
deliberate difference: prefix M-1..M-9 select window N (tmux's stock prefix
M-1..M-7 layout presets are gone in both modes). WezTerm's CMD+1-9 send
prefix M-digit whatever the flag says, and off they must keep meaning what
CMD+1-9 always meant. Every other `%else` line undoes its `%if` twin:
`set -gu status-format` restores tmux's default array (the default format plus
catppuccin's window formats), prefix 1-9 get back `select-window -t :=N` with
their default notes, the root keys are unbound, and the rebound keys (c, C-t,
k, C-k, x, C-l) are re-bound verbatim (the test pins them to tmux.conf's
lines). prefix a / C-a (the session picker) are unbound when on; the off
branch leaves them alone because `prefix r` re-runs tmux.conf, which binds
them, before spaces.conf. The test checks cold `off`, and `on → off`.

Not gated: the hidden marker and `@space_index` numbering (session options
nothing else reads), and the `pane-died[20]` hook that respawns a home shell.
A home made while on stays a home when the flag goes off (an ordinary shell
then, closable with prefix x, still respawning if its shell exits). With the
flag off, sync makes no homes and writes no registry.

## Hidden sessions

`agents tasks stash scratch btop-popup` get `@space_hidden 1`: by the
`session-created[20]` hook (pure tmux, at birth) and by `space.sh sync` (on
load, on `session-closed[20]`/`session-renamed[20]`). They have no tab, no
number, no home and no registry entry, and the status formats never loop into
them — `agents` alone holds hundreds of windows.

## Home: the space's main shell

Every visible space has exactly one home: a normal interactive shell at the
space folder, the space's LOWEST window, named `home`, shell 1 (󰋜 on its
line-1 tab). It is what keeps a space alive: closing the last other tab lands
you on home, still in the space. It cannot be closed by hand:

The WINDOW carries `@space_home`; the home shell's PANE carries
`@space_home_pane` and a pane-level `remain-on-exit on`. Other panes split
into the home window are ordinary: they close, exit and take x / C-l normally.

- prefix x (CMD+W) and prefix C-l (CMD+SHIFT+W) on the home pane refuse with
  a message pointing at prefix Q; prefix C-k (replace this tab) in the home
  window only adds a tab.
- `exit` / Ctrl-D in the home pane: it stays (dead) and `pane-died[20]` runs
  `space.sh respawn-home`, which respawns that pane in place at the space
  folder (never more than once every 2 s, so a shell that dies at once cannot
  spin the server).
- killed some other way (prefix &, `kill-window`): `window-unlinked[20]` runs
  a sync, which makes a new one. `window-linked[20]` runs one too, so a window
  that lands before home (closed-tabs reopens with -b) sends home back to the
  front and Option-1 stays home. Both hooks are gated on the hook's own
  session (`hook_session_name`), never a hidden one, whatever client is where.
- An agent started in the home pane makes it an agent for display (roster,
  ordinals); it stays uncloseable.

`sync` keeps it so, per visible space: the lowest `@space_home` window is it
(its home pane respawned if dead, the window moved back in front if it is
not; any second one only loses the markers); else a shell
window NAMED home is adopted (that is what resurrect brings back: names and
slots, not window options); else a space whose only window is a shell adopts
that shell (a new space does not get a second one); else a new shell is
inserted as the lowest window. Homes without an agent are dropped from the
agent roster and the Python strip (`@space_home` is the last field of
agent-roster.py's FMT).

## Registry: spaces survive a restart

`@space_registry` (default `~/.local/state/tmux-spaces/spaces.tsv`): one
`name<TAB>folder` line per space, in tab order, main included. Written only
with the flag on, under a mkdir lock (held only briefly; a holder removes it
only while it still holds its own token), by `sync` (a new space is appended, a
renamed one keeps its line, one renamed into the hidden list is dropped, one
killed some other way than `close` is dropped), and by `close`.

On config load, `sync load` RECONCILES: a registered space with no session is
recreated (a session at its folder whose one window is its home), and a live
one whose folder differs gets the registry's back (`attach-session -t S -c
DIR` is the one command that sets a session's folder; from a script it then
fails "not a terminal", after setting it). The numbers follow the registry,
so the order survives too.

The first 90 s after the server starts are special. If continuum's
auto-restore is on (and `~/tmux_no_auto_restore` absent), resurrect is about
to rebuild sessions window by window, matching them by index, and it only
treats a server with ONE pane as "restoring from scratch". So the first sync
sets `@space_restore_pending` and starts `space.sh restore-wait`, which waits
for continuum_restore.sh / resurrect's restore.sh (post-restore hooks
included) to come and go, then marks it `done` and reconciles. Until then sync
only marks and numbers: no homes, no registry writes (a pending older than
15 min counts as done). Without auto-restore, early syncs still never prune
the registry or overwrite its folders, since they may run before the
load-time reconcile. Resurrect saves home windows like any other; they come
back as shells named `home` and are adopted.

## Status (flag on: `status 2`)

**Line 0 — spaces.** The visible spaces by `@space_index` (main = 1, then the
registry's order). `S:` loops can sort only by id, name or time, so the line
has one `S:` loop per number 1-9 (each a cheap test per session) plus one for
anything past 9. A space tab is a catppuccin window tab whose state is the
space's **rollup**, with the same colour language as the window tabs:

- chip: the space number; peach when current, pulsing pink↔blue while any
  agent is in flight (running / computer use / workflow), crust under a tint
- body: red failed > yellow needs-* > green done (done+workflow does not
  count) > stock
- glyph: 󰒓 any background workflow, else 󰍽 any computer use

Each tab is `#[range=session|$id]`, so tmux's own `MouseDown1Status`
(`switch-client -t =`) switches on click. Inside a hidden session its name is
shown dimmed after the tabs. `status-right` stays on the right, unchanged
(except that the stash chip is hidden while spaces are on:
`stash-statusline.sh` folds `@spaces` into its "nothing parked" test; the
stash summary moves to the sidebar).

**Line 1 — shells of this space**, each `#[range=window|N]` (click selects),
numbered by their ORDINAL among the space's shells (home = 1, agents skipped,
so the numbers never jump), labelled with the folder while a shell sits at
the prompt, else the command (nvim, nu…); peach chip on the one you're on.
The ordinal is a nested `W:` count; a nested loop cannot see the outer
window's index, so `@space_shell_ord` is expanded twice (`#{E:#{E:…}}`): the
first pass bakes the outer index in and turns `##` into `#`. Then `[+]`
(`#[range=user|space-new]`): the `MouseDown1Status` binding opens a shell at
the space folder for that range and falls back to `switch-client -t =`.

**During a stash range selection** (prefix v; `stash.sh sel-*`) line 1 shows
*every* window of the space as today's catppuccin tab
(`window-status-format`/`-current-format`, `@stash_sel` mauve tint included),
since the selection's only feedback is that tint and agents have no tab
otherwise. The trigger is any window of the space carrying `@stash_sel`.

No `#()` in either line. Cost measured at ~1 ms per line-0 expansion with 7
spaces × 8 windows.

## Keys (flag on)

| key | tmux | does |
|---|---|---|
| Control-1-9 | prefix 1-9 | space N, tab order. No fork: an `S:` loop finds the session whose `@space_index` is N |
| Option-1-9 | M-1..M-9 | shell N of this space (home = 1) |
| Command-1-9 | prefix M-1..M-9 | agent N of this space |
| ⌥L / ⌥H | M-l / M-h | kind-aware: on an agent, next / previous agent; on a shell, next / previous shell; wrapping |
| ⌥\` | M-\` | agent → last-visited shell of this space (a new shell at the space folder if there is none); shell → last-visited agent |
| ⌥M | M-m | move this tab to another space: a menu of the spaces (number = key); the tab goes to the end of that space, you stay here. Not home, not from a hidden session |
| ⌥N | M-n | new space: zoxide/fzf folder picker (as `prefix K`), then reuse the session already on that folder, else `new-session -d -s <basename> -c <dir>`; switch to it. `~` → main |
| ⌥V | M-v | sidebar scope, this space ↔ all spaces (posts `com.mackhaymond.tmux.focus` so the Rust sidebar redraws at once) |
| — | prefix Q | close this space: `confirm-before` "close space bai: 2 agents, 1 shell? (y/n)", then `space.sh close`: it is marked `@space_closing` (no sync re-registers it or rebuilds its home meanwhile) and unregistered, clients on it move to main (detach-on-destroy would close them), every pane goes through `closed-tabs.sh close` (snapshot + agent session id, so CMD+Z can bring each back; `@space_closer` overrides, `none` skips), the session is killed and unregistered. Never main, never a hidden session |
| CMD+W, CMD+SHIFT+W | prefix x, C-l | as tmux.conf, except on home (refuse) |
| — | c, C-t, k, C-k | in a project space (path ≠ `~`, not hidden) the new tab starts in the space folder; elsewhere unchanged |

WezTerm (wezterm.lua.tmpl, the last loop): CTRL+N sends `C-s N`, CMD+N sends
`C-s` then `ESC N`; OPT+N reaches tmux as M-N on its own (left Option is
Meta). WezTerm matches CTRL/ALT/SHIFT/SUPER without left/right variants, so
it is any Control. Ctrl+digit is free: no WezTerm default, macOS "Switch to
Desktop N" is off, Karabiner only claims digits with all four modifiers.

In a hidden session the agent/shell/next/prev/toggle/move keys only say "not
a space" (an `if -F` on `@space_hidden`, no fork); `space.sh` refuses hidden
sessions as well, so nothing ever selects or creates windows in agents/stash.
Script messages go to the invoking client (`'#{client_name}'` is passed
along). `space.sh` posts `com.mackhaymond.tmux.focus` (notifyutil) after it
closes, creates or recreates a space or moves a tab.

**Ordinals** (agents shared with the agent sidebar): position among the
session's windows with non-empty `@agent_state` (agents) or empty (shells),
by `window_index`, 1-based.

Last-visited agent/shell are session options `@space_last_agent` /
`@space_last_shell` (window ids), written by `session-window-changed[20]` and
`client-session-changed[20]` (pure `set -F`); `space.sh toggle` re-checks the
id is still a shell/agent of the session before using it.

### Why not ⌥J/⌥K

Claude Code binds `meta+j` to `app:toggleTerminal` (Global). ⌥H/⌥L are free
in Claude Code, zsh (vi keymap: no `^[` bindings in viins) and nvim (only
`<A-e>`, `<A-o>`, `<A-g>` are mapped). ⌥1-9, ⌥M, ⌥N, ⌥\` are free in all
three. Already-taken root keys (M-s M-x M-w M-a M-q) are untouched.

## For the Rust sidebar (agent-ui)

Home windows have no `@agent_state`, so with spaces on they are never agent
rows already. Anywhere it lists plain windows (show-all, spaces off), it
should drop `@space_home` windows the way agent-roster.py does.

## Session names

`new-space`/`open-dir` keep `[A-Za-z0-9_-]` from the folder's basename
(tmux rewrites `.`/`:` anyway) and add `-2`, `-3`… when the name is taken by a
different folder, or is one of `@space_hidden_sessions` (a folder called
`stash` becomes `stash-2`, never a hidden session). The folder is made
absolute (`cd && pwd`) before matching or creating, so a typed `../x` or `.`
works. Without zoxide the picker offers this tab's folder, `~`, and the
folders under `~/code/projects` and `~/code`.
