# Spaces

A **space** is a tmux session. Most have a project folder (`session_path`);
`main` is the home space (`~`). In a space, an **agent** is a window with a
non-empty `@agent_state` (the agent-tab indicator sets it for every live
claude/codex window, idle included, and the GC clears it); every other window
is a **shell**.

## Files

| file | role |
|---|---|
| `tmux.conf.tmpl` | `set -g @spaces on` (the one flag) and `@space_hidden_sessions` (the one hidden list), near the top; `source-file -F "#{d:current_file}/spaces.conf"` as the last line |
| `spaces.conf` | everything else: hooks, status lines, keys. `%if @spaces on … %else … %endif` |
| `scripts/space.sh` | the logic a format can't do: `sync`, `agent N`, `next`/`prev`, `toggle`, `new-space`, `open-dir` |
| `tests/test_spaces.py` | real `-L` servers, keys sent with `send-keys -K`, clicks as SGR reports |

Why a second file: `%if` is decided when a file is *parsed*, before any of its
commands run, so a `%if` on `@spaces` inside tmux.conf would see the value
from the previous load. By the time tmux.conf's `source-file` runs, its `set`
has.

## The flag

`set -g @spaces off` + `prefix r` gives back exactly the pre-spaces config:
same `list-keys` (notes included), same `status`/`status-format`. Every
`%else` line undoes its `%if` twin — `set -gu status-format` restores tmux's
default array (which is what today's bar is: the default format plus
catppuccin's window formats), prefix 1-9 get back `select-window -t :=N` with
their default notes, the root keys are unbound, and the new-window keys are
re-bound verbatim (the test pins them to tmux.conf's lines). Verified both
cold (`off` from start) and as a live `on → off` reload, against the git-HEAD
tmux.conf rendered with `chezmoi execute-template` and every `run`/`run-shell`
line stripped (no tpm, no watchers).

Not gated: the hidden marker and `@space_index` numbering (session options
nothing else reads).

## Hidden sessions

`agents tasks stash scratch btop-popup` get `@space_hidden 1`: by the
`session-created[20]` hook (pure tmux, at birth) and by `space.sh sync` (on
load, on `session-closed[20]`/`session-renamed[20]`). They have no tab and no
number, and the status formats never loop into them — `agents` alone holds
hundreds of windows.

## Status (flag on: `status 2`)

**Line 0 — spaces.** `main`, then every other visible session in creation
order (an `S:` loop walks sessions by id; `space.sh sync` numbers them the
same way into `@space_index`). Ordering by id rather than `session_created`:
resurrect restores many sessions within one second, so the timestamps tie;
ids never do. A space tab is a catppuccin window tab whose state is the
space's **rollup**, with the same colour language as the window tabs:

- chip: the space number; peach when current, pulsing pink↔blue while any
  agent is in flight (running / computer use / workflow), crust under a tint
- body: red failed > yellow needs-* > green done (done+workflow does not
  count) > stock
- glyph: 󰒓 any background workflow, else 󰍽 any computer use

Each tab is `#[range=session|$id]`, so tmux's own `MouseDown1Status`
(`switch-client -t =`) switches on click. Inside a hidden session its name is
shown dimmed after the tabs. `status-right` stays on the right, unchanged.

**Line 1 — shells of this space**, each `#[range=window|N]` (click selects),
labelled with its folder while a shell sits at the prompt, else the command
(nvim, nu…); peach chip on the one you're on. Then `[+]`
(`#[range=user|space-new]`): the `MouseDown1Status` binding opens a shell at
the space folder for that range and falls back to `switch-client -t =`.

**During a stash range selection** (prefix v; `stash.sh sel-*`) line 1 shows
*every* window of the space as today's catppuccin tab
(`window-status-format`/`-current-format`, `@stash_sel` mauve tint included),
since the selection's only feedback is that tint and agents have no tab
otherwise. The trigger is any window of the space carrying `@stash_sel`
(stash.sh flags the first tab at once and clears it on commit/cancel), so no
extra marker exists. Back to shells only when it ends.

No `#()` in either line. Cost measured at ~1 ms per line-0 expansion with 7
spaces × 8 windows.

## Keys (flag on)

| key | does |
|---|---|
| prefix 1-9 (WezTerm ⌘1-9) | space N, tab order. No fork: an `S:` loop finds the session whose `@space_index` is N |
| ⌥1-9 | agent N of this space |
| ⌥L / ⌥H | next / previous agent in this space, wrapping, skipping shells |
| ⌥\` | agent → last-visited shell of this space (a new shell at the space folder if there is none); shell → last-visited agent |
| ⌥N | new space: zoxide/fzf folder picker (as `prefix K`), then reuse the session already on that folder, else `new-session -d -s <basename> -c <dir>`; switch to it. `~` → main |
| c, C-t, k, C-k | in a project space (path ≠ `~`, not hidden) the new tab starts in the space folder; elsewhere unchanged |

In a hidden session ⌥1-9, ⌥H/⌥L and ⌥\` only say "not a space" (an `if -F`
on `@space_hidden`, no fork); `space.sh` refuses hidden sessions as well, so
nothing ever selects or creates windows in agents/stash. Script messages go to
the invoking client (`'#{client_name}'` is passed along).

**Agent ordinal** (shared with the agent sidebar): position among the
session's windows with non-empty `@agent_state`, by `window_index`, 1-based.

Last-visited agent/shell are session options `@space_last_agent` /
`@space_last_shell` (window ids), written by `session-window-changed[20]` and
`client-session-changed[20]` (pure `set -F`); `space.sh toggle` re-checks the
id is still a shell/agent of the session before using it.

### Why not ⌥J/⌥K

Claude Code binds `meta+j` to `app:toggleTerminal` (Global). ⌥H/⌥L are free
in Claude Code, zsh (vi keymap: no `^[` bindings in viins) and nvim (only
`<A-e>`, `<A-o>`, `<A-g>` are mapped). ⌥1-9, ⌥N, ⌥\` are free in all three.
Already-taken root keys (M-s M-x M-w M-a M-q) are untouched.

## Session names

`new-space`/`open-dir` keep `[A-Za-z0-9_-]` from the folder's basename
(tmux rewrites `.`/`:` anyway) and add `-2`, `-3`… when the name is taken by a
different folder, or is one of `@space_hidden_sessions` (a folder called
`stash` becomes `stash-2`, never a hidden session). The folder is made
absolute (`cd && pwd`) before matching or creating, so a typed `../x` or `.`
works. Without zoxide the picker offers this tab's folder, `~`, and the
folders under `~/code/projects` and `~/code`.
