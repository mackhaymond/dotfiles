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
| `scripts/space-dash.sh` | the home dashboard (below); `--once` draws one frame |
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
`list-keys` (notes included), same `status`/`status-format`, with two
deliberate differences, both about WezTerm's digit chords, which reach tmux
whatever the flag says and must keep meaning what they always meant: prefix
M-1..M-9 select window N (tmux's stock prefix M-1..M-7 layout presets are
gone in both modes), and the chord keys `User0`-`User26` (below) do what
their chords did before spaces: CTRL+N and CMD+N select window N, OPT+N
`send-keys M-N` to the pane. Every other `%else` line undoes its `%if` twin:
`set -gu status-format` restores tmux's default array (the default format plus
catppuccin's window formats), prefix 1-9 get back `select-window -t :=N` with
their default notes, the root keys are unbound, and the rebound keys (c, C-t,
k, C-k, x, C-l) are re-bound verbatim (the test pins them to tmux.conf's
lines). prefix a / C-a (the session picker) are unbound when on; the off
branch leaves them alone because `prefix r` re-runs tmux.conf, which binds
them, before spaces.conf. The test checks cold `off`, and `on → off`.

Not gated: the hidden marker, the `@space_index` numbering and its
`@space_key` label, `@space_color` from the registry (session options), the
`user-keys` (the WezTerm chords), the `pane-died[20]` hook that respawns a
home shell or dashboard, and the `window-pane-changed[20]` hook that keeps
the focus off a dashboard. A home made while on stays a home when the flag
goes off (an ordinary shell then, closable with prefix x, still respawning if
its shell exits; its dashboard likewise). With the flag off, sync makes no
homes or dashboards and writes no registry. `status` is always on, and there
is no `jump` table (M-w is tmux.conf's ⌥W menu again).

## Hidden sessions

`agents tasks stash scratch btop-popup` get `@space_hidden 1`: by the
`session-created[20]` hook (pure tmux, at birth) and by `space.sh sync` (on
load, on `session-closed[20]`/`session-renamed[20]`). They have no tab, no
number, no home and no registry entry, and the status formats never loop into
them — `agents` alone holds hundreds of windows.

## Home: the space's main shell

Every visible space has exactly one home: a normal interactive shell at the
space folder, the space's LOWEST window, named `home`, shell 1 (󰋜 on its
shell tab). It is what keeps a space alive: closing the last other tab lands
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

## Home: the dashboard

The home window is two panes: on top, full width and 9 rows, the
**dashboard** (`space-dash.sh`); below it the home shell, which stays the
focused pane and keeps Option-1.

```
  ▀█▀ █▄█ █ █ █ █   ⌃2  ~/.local/share/chezmoi
   █  █▀█ █ █ ▄▀▄    main  ±16
   ▀  ▀ ▀ ▀▀▀ ▀ ▀   4 agents · 2 shells · 1 parked

  ◐ ⌘1 a  tmux/Tmux Agent Sidebar  Two implementers are running in parallel
  ◉ ⌘2 d  tmux/home dashboard  Allow Bash(notifyutil -p ...)?
  ✓ ⌘3 f  tmux/resurrect guard  Everything is live on your server
  ○ ⌘4    tmux/Canvas syllabus
  ⌥1 home · ⌥2-9 shells · ⌘1-9 agents · ⌥` agent↔shell · ⌥W jump · ⌥N new space · prefix Q close
```

Idle cost, measured on a test server over 60 s: 0.00-0.02 s of CPU (under
0.04%) for the dashboard's bash, its reader and its `notifyutil`, wakes for
the live server's notices included. A dashboard whose pane is gone (tmux
answers a missing target with empty fields, not an error, so it checks its
own `#{pane_id}` comes back) exits at its next draw, at most 60 s later.

The name, large (a 3x5 font in half blocks; plain bold when the pane is too
narrow), in the space's colour; its ⌃N key and folder; git branch, dirty
count (`±N`, or `clean`) and ahead/behind; the agent / shell / parked counts
(parked = windows in `stash` whose `@stash_origin` is this space); one line
per agent, by ordinal (⌘N), with its state shape (✕ ◉ ✓ ◐ ○, colours as the
roster), its ⌥W letter (`@agent_key`), its summary and detail (as many as
fit, then `+N more`); a key hint.

- **Redraws** on `com.mackhaymond.tmux.agent-state` and
  `com.mackhaymond.tmux.focus`: one long-lived `notifyutil -w` per dashboard,
  read in a background `read -t 60` that the loop `wait`s for (a trapped
  SIGWINCH interrupts `wait`, not `read`, so a resize redraws at once). Idle
  is a blocked `wait`: no polling, no forks; the 60 s timeout is the
  fallback refresh. A burst of notices is drawn once (0.15 s settle). Each
  redraw is one tmux call; git (`--no-optional-locks status`) runs only
  while the dashboard can be seen (session attached, home window current),
  at most every 3 s.
- **Height**: a window resize shares the change among panes, so a redraw
  that finds the pane not 9 rows resizes it back (not while zoomed, not in a
  window under 15 rows).
- **Never focused**: `window-pane-changed[20]` (not gated) hands the focus
  to the pane below it, whatever made it active (a click, prefix Up,
  vim-tmux-navigator, the pane below closing). Pure tmux, no fork.
- **Not a shell, not a tab**: shells are windows, so it is never counted or
  listed as one; `close` sends no dashboard through the closer.
- **Cannot be closed**: the pane carries `@space_dash_pane` and
  `remain-on-exit on`; when it dies `pane-died[20]` respawns it (as the home
  shell, at most every 2 s); prefix x / C-l refuse it. Splits in home close
  normally.
- **sync** keeps exactly one per home: a home without one (from before the
  dashboard) gets one, after its other commands, under the registry lock (so
  concurrent syncs never add two); a dead one, or one not running the
  dashboard, is respawned; a second marked one is killed. It also has the
  title `space-dash`, which resurrect restores (it restores titles, not
  options or this command): a restored home window's `space-dash` pane is
  respawned as the dashboard, never adopted as the home shell. A home left
  with only its dashboard gets a new shell below it.
  A new home window is made in a tmux call of its own, and a dashboard is
  split only into a window read back as `@space_home` just before, never by
  position (`$sid:^`): a tmux command list stops at its first failing
  command (a pane gone between sync's read and its write), which could drop
  the new home and leave the lowest window, an agent's, to get the
  dashboard. sync's batched commands rerun one by one when the batch fails.
- `last-pane` (prefix ;) in a home window: right after a bounce the "last"
  pane is the dashboard, so prefix ; then stays put. tmux has no way to
  restore the last-pane marker; it only matters with your own splits in
  home, and only after something tried to focus the dashboard.

## Colours

Each space has a stable colour, the session option `@space_color` (hex),
which the sidebar reads too. main is always lavender `#b4befe`; the others
get, when registered, the palette colour the fewest registered spaces use
(ties in palette order): mauve, teal, pink, sapphire, peach, flamingo, sky,
rosewater, blue. No red, yellow or green: those are states. Kept in the
registry's third column, so it survives a restart and a rename; a line from
before the column (or with a bad value) gets one at the next sync. Hidden
sessions have none.

## Registry: spaces survive a restart

`@space_registry` (default `~/.local/state/tmux-spaces/spaces.tsv`): one
`name<TAB>folder<TAB>colour` line per space, in tab order, main included. Written only
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

## Status (flag on: ONE line)

```
█⌃1 main ●  ⌃3● █1█ 󰋜 ~██2█ ~█ [+]                       … status-right
```

The full list of spaces, with their ⌃N keys, is in the sidebar. The status
line shows, left to right:

**This space's chip**, `⌃N name`: a solid block in the space's colour
(`@space_color`, peach until it has one), bold crust text, ended by a
powerline edge (U+E0B0, which WezTerm draws itself) and a gap, so it never
reads as a shell tab. Red while the prefix is pressed (what catppuccin's
`session` module showed, see below). The label is the session option
`@space_key` (the key that reaches the space; `space.sh sync` sets it next to
`@space_index`, `⌃N` for now), so a new key scheme is one line of the script.
Spaces past 9 have no key: `@space_key` is unset, the chip shows the name
alone and their alert shows the name (`proj●`). After the edge, the space's
**rollup**, in the window tabs' colour language:

- a ●: red failed > yellow needs-* > green done (done+workflow does not
  count); nothing otherwise
- glyph: 󰒓 any background workflow, else 󰍽 any computer use

Inside a hidden session its name (`[agents]`) stands in for the tab: dimmed,
or crust on red while the prefix is pressed, like the chip.

**Other spaces that need you**: `⌃N●`, the ⌃N in that space's colour and the
● red / yellow / green, for each other visible space whose rollup is failed /
needs-* / done (the states prefix d walks). In-flight and idle spaces never show, so this is usually empty. One
`S:` loop, in name order. Each is `#[range=session|$id]`, so tmux's own
`MouseDown1Status` (`switch-client -t =`) switches on click.

**The shells of this space** (below), then `status-right` on the right:
today's, minus catppuccin's `session` module while spaces are on
(`tmux.conf.tmpl` builds `@catppuccin_status_modules_right` with `set -gF`
from `@spaces`, and tpm rebuilds status-right from it on every load, so the
flag still takes a `prefix r`). The stash chip is hidden while spaces are on
(`stash-statusline.sh` folds `@spaces` into its "nothing parked" test; the
stash summary moves to the sidebar); continuum's `#()` autosave hook and the
stale-save chip are untouched.

**Shells of this space**, each `#[range=window|N]` (click selects),
numbered by their ORDINAL among the space's shells (home = 1, agents skipped,
so the numbers never jump), labelled with the folder while a shell sits at
the prompt, else the command (nvim, nu…); peach chip on the one you're on.
The ordinal is a nested `W:` count; a nested loop cannot see the outer
window's index, so `@space_shell_ord` is expanded twice (`#{E:#{E:…}}`): the
first pass bakes the outer index in and turns `##` into `#`. Then `[+]`
(`#[range=user|space-new]`): the `MouseDown1Status` binding opens a shell at
the space folder for that range and falls back to `switch-client -t =`.

**During a stash range selection** (prefix v; `stash.sh sel-*`) the shells
give way to *every* window of the space as today's catppuccin tab
(`window-status-format`/`-current-format`, `@stash_sel` mauve tint included),
since the selection's only feedback is that tint and agents have no tab
otherwise. The trigger is any window of the space carrying `@stash_sel`.

No `#()` in the line, one `S:` loop, and hidden sessions are skipped before
any `W:` runs inside them.

**While the sidebar is open.** The agent sidebar (agent-ui) keeps the global
`@sidebar_open` (how many sidebars are open) and turns `status` off while any
is. A reload honours it: with spaces on, `status` is off when
`@sidebar_open` > 0, else on. With spaces off it is always on.
A sidebar killed outright (SIGKILL) never counts itself out, so with spaces
on `agent-ui sidebar --repair` (`@space_agent_ui`, default
`~/.local/bin/agent-ui`) recounts: synchronously at load, before that gate,
and on every `client-attached[21]`. No binary means no sidebar: the count is
unset and the line stays on.

## The prefix: a root binding (tmux.conf.tmpl)

`set -g prefix None` and `bind -n C-s switch-client -T prefix \; run-shell -b
"notifyutil -p com.mackhaymond.tmux.focus"`, so arming the prefix posts the
focus notice and the sidebar can show it at once (it reads
`#{client_key_table}`; the status line, hidden then, no longer can). The
`prefix` option has to be None: tmux checks it before any root binding, so a
root binding of the prefix key never runs. Checked on a test server
(`test_prefix_as_a_root_binding_keeps_every_prefix_use`): C-s <key>, C-s C-s
(the resurrect save), the `-r` repeat keys (C-s during a repeat re-arms), C-s
from copy mode (copy-mode-vi has no C-s, so tmux falls back to the root
table) all behave as before; `#{client_prefix}` still means "in the prefix
table", so the chip still turns red. `send-prefix` would send nothing; nothing
binds it (C-s C-s is the save; tmux-sensible's own `bind None …` attempts
fail silently). Only the arming edge is posted: tmux has no hook for leaving
a key table, so the sidebar re-reads `#{client_key_table}` after the next key.

## Keys (flag on)

| key | tmux | does |
|---|---|---|
| Control-1-9 | User0-8 (also prefix 1-9) | space N, tab order. No fork: an `S:` loop finds the session whose `@space_index` is N |
| Option-1-9 | User9-17 (also M-1..M-9) | shell N of this space (home = 1) |
| Command-1-9 | User18-26 (also prefix M-1..M-9) | agent N of this space |
| ⌥L / ⌥H | M-l / M-h | kind-aware: on an agent, next / previous agent; on a shell, next / previous shell; wrapping |
| ⌥\` | M-\` | agent → last-visited shell of this space (a new shell at the space folder if there is none); shell → last-visited agent |
| ⌥M | M-m | move this tab to another space: a menu of the spaces (number = key); the tab goes to the end of that space, you stay here. Not home, not from a hidden session |
| ⌥N | M-n | new space: zoxide/fzf folder picker (as `prefix K`), then reuse the session already on that folder, else `new-session -d -s <basename> -c <dir>`; switch to it. `~` → main |
| ⌥V | M-v | sidebar scope, this space ↔ all spaces (posts `com.mackhaymond.tmux.focus` so the Rust sidebar redraws at once) |
| ⌥W *letter* | M-w, then key table `jump` | the agent whose window option `@agent_key` is that letter, in any space (`switch-client -t '$S:@W'`, one step); hidden sessions never. Escape, or any other key (`Any`), cancels and is swallowed. Entering and leaving post `com.mackhaymond.tmux.focus` so the sidebar can show "jump mode" (`#{client_key_table}` = `jump`). The sidebar assigns the letters; this only reads them. No fork: an `S:`/`W:` loop, `#{s/ .*//:}` keeps the first match, the letter rides in the global `@space_jump_key` |
| — | prefix Q | close this space: `confirm-before` "close space bai: 2 agents, 1 shell? (y/n)", then `space.sh close`: it is marked `@space_closing` (no sync re-registers it or rebuilds its home meanwhile) and unregistered, clients on it move to main (detach-on-destroy would close them), every pane goes through `closed-tabs.sh close` (snapshot + agent session id, so CMD+Z can bring each back; `@space_closer` overrides, `none` skips), the session is killed and unregistered. Never main, never a hidden session |
| CMD+W, CMD+SHIFT+W | prefix x, C-l | as tmux.conf, except on home (refuse) |
| — | c, C-t, k, C-k | in a project space (path ≠ `~`, not hidden) the new tab starts in the space folder; elsewhere unchanged |

WezTerm (wezterm.lua.tmpl, the last loop) sends each digit chord as ONE
private escape sequence, which tmux reads as one key through its
`user-keys` (set in spaces.conf above the `%if`, so in both modes): CTRL+N
`\e[911N~` = `User(N-1)`, OPT+N `\e[912N~` = `User(8+N)`, CMD+N `\e[913N~` =
`User(17+N)`. No real terminal sends CSI 9111-9139 `~`. Nothing depends on
timing or on a stray modifier: CMD+N used to send `C-s` then `ESC N`, the
very bytes of OPT+N, so a dropped prefix or a touched Option landed on a
shell. The mixed chords (CMD+OPT, CMD+CTRL, CTRL+OPT, CMD+SHIFT + digit) are
`Nop` in WezTerm. The prefix 1-9, M-1..9 and prefix M-1..9 bindings stay for
other terminals (ssh, a nested client). The User keys work from copy mode too
(tmux falls back to the root table). WezTerm matches CTRL/ALT/SHIFT/SUPER
without left/right variants, so it is any Control or Option. Ctrl+digit is
free: no WezTerm default, macOS "Switch to Desktop N" is off, Karabiner only
claims digits with all four modifiers.

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
three. Already-taken root keys (M-s M-x M-a M-q) are untouched; M-w is the leader (below).

⌥W (M-w) was tmux.conf's agent menu; with spaces on the leader takes it
(2026-10-15: the menu is on hold, prefix q still opens it). It was ⌥F at
first; M-f is unbound again, so zsh's `forward-word` and Claude Code's meta+f
work in the pane. With spaces off tmux.conf's ⌥W menu binding stands and
there is no `jump` table.

## For the Rust sidebar (agent-ui)

Home windows have no `@agent_state`, so with spaces on they are never agent
rows already. Anywhere it lists plain windows (show-all, spaces off), it
should drop `@space_home` windows the way agent-roster.py does. Anywhere it
lists PANES, it should drop `@space_dash_pane` ones (the home dashboard).
It reads `@space_color` (each space's colour), keeps `@sidebar_open` (the
status line follows it), and shows "prefix armed" / "jump mode" from
`#{client_key_table}` (`prefix` / `jump`) on the focus notice.

## Session names

`new-space`/`open-dir` keep `[A-Za-z0-9_-]` from the folder's basename
(tmux rewrites `.`/`:` anyway) and add `-2`, `-3`… when the name is taken by a
different folder, or is one of `@space_hidden_sessions` (a folder called
`stash` becomes `stash-2`, never a hidden session). The folder is made
absolute (`cd && pwd`) before matching or creating, so a typed `../x` or `.`
works. Without zoxide the picker offers this tab's folder, `~`, and the
folders under `~/code/projects` and `~/code`.

## Rollout

Reload tmux (`prefix r`) BEFORE WezTerm picks up the new wezterm.lua: until
tmux has the `user-keys`, the digit chords type a raw `\e[91xN~` into panes.
