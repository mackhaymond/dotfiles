# Agent roster (`prefix q`)

A herdr-style list of every agent across every tmux session, in one popup.
It is a **reader**: everything it shows comes from the per-window options the
hook pipeline already maintains (see [agent-tab-indicator.md](agent-tab-indicator.md)).
It keeps no state and runs only while the popup is open.

Two implementations exist. **agent-ui** (Rust, see [below](#agent-ui-primary-and-the-python-fallback))
is the primary one for both the Option-W popup and the CMD+B sidebar.
`scripts/agent-roster.py`, which most of this page describes, is the
**fallback**, used when the binary is not installed.

## agent-ui (primary) and the Python fallback

`~/.local/bin/agent-ui` is one ratatui/crossterm binary for both surfaces:

| Surface | Command |
|---|---|
| Option-W / `prefix q` / `prefix C-q` popup | `agent-ui menu --client <tty> [--tab all\|needs\|working\|idle\|parked]` |
| CMD+B WezTerm sidebar | `agent-ui sidebar [--client <tty>]` (wezterm.lua also passes `--tmux-pane`, `--wezterm`) |
| screenshot / test render | `agent-ui sidebar --once WxH`, `agent-ui menu --client <tty> --once WxH` |

- **Source**: the crate lives only in the chezmoi source tree,
  `~/.local/share/chezmoi/dot_config/tmux/agent-ui/` (`.chezmoiignore` keeps
  it out of `~/.config`).
- **Build**: `chezmoi apply` runs
  `.chezmoiscripts/run_onchange_after_zz-build-agent-ui.sh.tmpl`, which runs
  `cargo build --release --locked` with `CARGO_TARGET_DIR=~/.cache/agent-ui/target`
  and installs the result to `~/.local/bin/agent-ui` atomically (copy to a
  temp file beside it, then `mv`), so a running sidebar or menu keeps its old
  inode. It re-runs whenever `Cargo.toml`, `Cargo.lock` or any file under
  `src/` changes (a hash of all of them is rendered into the script), or when
  cargo appears on PATH. No cargo: one line, exit 0, the fallback stays in use.
  A failed build prints cargo's error and exits non-zero, so chezmoi reports
  it; the installed binary is left alone.
- **Rebuild by hand** (ignores the hash):
  `chezmoi execute-template < ~/.local/share/chezmoi/.chezmoiscripts/run_onchange_after_zz-build-agent-ui.sh.tmpl | bash`.
  It is idempotent: cargo is a no-op and an identical binary is not reinstalled.
- **Which one runs**:
  - tmux checks for the binary once, when tmux.conf is loaded: the python
    bindings are written first, then an `if-shell "test -x …/agent-ui"` block
    re-binds q, C-q and M-w to `display-popup -E -B -w 75% -h 75% …/agent-ui
    menu --client '#{client_tty}'`. agent-ui draws its own rounded border and
    title, hence `-B` and no `-T`. After installing or removing the binary,
    `prefix r`.
  - WezTerm checks at every CMD+B (`io.open`, no fork), so no reload is needed.
- **Force the fallback**: `rm ~/.local/bin/agent-ui`, then `prefix r`. The next
  `chezmoi apply` reinstalls it only if the crate changed. To get it back
  sooner, rebuild by hand as above.

Why the popup's choice is made at load time (keypress → first frame, private
`tmux -L` server with `-f /dev/null`, one window, a real client in a pty,
`send-keys -K M-w`, until the menu has drawn the window's name; send-keys
round trip subtracted; 20-50 runs each at load 6-10, 2026-10-08):

| Binding | median | min |
|---|---|---|
| python fallback (unchanged line) | ~41 ms | ~39 ms |
| agent-ui, chosen at config load (**used**) | ~8-13 ms | ~7 ms |
| agent-ui via `if-shell -F '#{@agent_ui}'`, option set at load | ~11-14 ms | ~6 ms |
| agent-ui via `if-shell "test -x …"` on every press | ~11-17 ms | ~9-12 ms |
| agent-ui via `run-shell "test -x … && tmux display-popup …"` | ~19-20 ms | ~15 ms |

tmux 3.7 formats cannot test for a file, so a per-press check needs a fork:
`if-shell` with a shell command costs a `/bin/sh` (+2-4 ms), and plain
`run-shell` a `zsh -c` plus a tmux client (+~10 ms). `if-shell -F` on an
option set at load is no cheaper than binding the right command at load, and
just as stale. Binding at load also keeps the python `bind-key q` / `C-q`
lines verbatim, which `BindingTests` and `roster_popup_argv` (the python
strip's ☰ button) compare against. The test server has one window; on the
real one (169 windows) the first tmux snapshot takes longer (~9 ms for the
python), the same for every variant.

| Key | Does |
|---|---|
| **Option-W**, or `prefix q` / `prefix C-q` | open the roster |
| `1`-`9`, then `01`, `02`, … from the tenth row | go to that row at once and close (see [Number keys](#number-keys)) |
| Tab / Shift-Tab, `j` / `k`, arrows | move |
| Space / ⏎ | go there (a parked tab comes back via `stash.sh unstash`) |
| `p` | peek: the last 15 lines of the selected agent's pane, in a panel over the list; any key closes it (and does nothing else) |
| `d` | next tab that needs you (same as `prefix d`) |
| `x` | close the agent's pane through `closed-tabs.sh`, after y/n; CMD+Z undoes it. On a parked row: discard it through `stash.sh kill-many` |
| `H` | park the tab (`stash.sh stash`, which suspends the agent), after y/n |
| `/` | filter by `session:index title`; ⏎ keeps it, esc clears it |
| `a` | also show windows with no agent |
| `r` | restart the watcher (only needed when the red banner says so) |
| esc / `q` | close |

Related keys: **Option-S** (or `prefix d` / `prefix C-d`) jumps to the next tab
that needs you, **Option-X** (or `prefix D`) jumps back (scripts/agent-jump.sh).

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
the tab bar's hues, the row's hotkey number (NEEDS YOU and filter rows add a
dim `session:index`, since no session header names theirs), the title
(`@agent_summary`, else the window name), the gear or mouse glyph, the state,
and the time since `@agent_since`. A NEEDS YOU row whose window carries
`@agent_detail` adds it after the title, dim, when there is room
(`perm  Bash git push origin main`, `asks  Which deck…`, `fail  529
overloaded`, `done  7 fixes applied`); `@agent_detail_kind` gives the word.
Both options are set by agent-tab-indicator.sh and may be missing, in which
case the row is as before.

## Peek (`p`)

`p` on a window row runs ONE `tmux capture-pane -p -J -S -15 -t <target>`,
on demand only (never on the refresh tick), and shows the last 15
non-blank-tailed lines in a rounded panel over the list (`peek_lines`,
`Roster.peek_panel`). The target is the window (its active pane); a split
window (`#{window_panes}` > 1) first asks `agent_pane` which pane runs the
agent, the same TTY match `x` uses, and falls back to the active pane when it
cannot tell. Any key closes the peek and does nothing else, and like the y/n
of `x`/`H`, the key that opened it drops the rest of its read, so a fast `pq`
leaves the peek open rather than closing the popup. The number labels of the
list stay as they were last drawn.

## Number keys

Every **window** row (NEEDS YOU, session-group rows, expanded parked rows,
filter hits) carries a number, unique across the list and assigned in display
order, so a window shown twice (in NEEDS YOU and in its session) has two.
Session headers and the `parked (n)` line have none. Pressing a number does
what ⏎ does on that row (`agent-jump.sh goto`, or `stash.sh unstash` for a
parked row) and closes the popup.

The labels (`hotkey_labels`):

| rows | labels |
|---|---|
| 1-9 | `1` … `9`, every one a single key |
| 10-18 | `1` … `9`, then `01` … `09` |
| 19-108 | `1` … `9`, then `001` … `099`; and so on |

A bare `0` is never a label, not even for exactly ten rows. If the list
shrinks from 11 to 10 rows while you are reading, a stale `01` must stay a
two-key label inside the `0…` namespace. If the `0` acted alone, the `1`
would fall through into the agent pane the jump had just focused, often a
permission menu where `1` means Yes.

Why this and not "wait ~350 ms for a possible second digit": the first nine
rows are NEEDS YOU and the current session, the ones actually pressed, and with
a plain 1..N numbering `1` would have to wait whenever there are 10+ rows
(the usual case here). Keeping 1-9 single and moving everything past nine
behind a `0` prefix of FIXED width makes the label set prefix-free: every key
sequence is complete the moment its last digit lands, nothing ever waits on a
timer, ⏎ is never needed, and the label on the row is exactly what to type.
While a `0…` is half typed the footer shows it; esc drops it, backspace takes
a digit back, any other key drops it and does its own thing.

- A number means **the row it was drawn on**: the whole sequence resolves
  against the frame on screen at its FIRST digit (`Roster.drawn`, labels and
  their width included), never a list rebuilt by a refresh since. Numbers
  renumber when the list changes (a NEEDS YOU row appears, `a`, a parked
  fold), which is fine because the screen changes with them. If the window
  has gone, or left the session the row showed (parked/unparked meanwhile),
  nothing runs and the footer says so.
- **A number that matches nothing swallows its tail**: say row 14 read `005`
  and the list shrank to 18 rows (`01`…`09`) before you typed. `00` is no
  row, so the footer says so, and every further digit is ignored until a
  non-digit key. Esc, ⏎ and backspace only end that state; any other key
  ends it and does its own thing. Without this, the `5` would start over as
  a single-digit jump to some other window.
- **Input during a refresh**: a key or click that arrives while the snapshot
  is being taken is handled against the frame still on screen, before the new
  frame is drawn (main loop: a zero-timeout poll after `refresh()` skips the
  draw when input is waiting).
- Only rows on screen count: a number scrolled out of view does nothing.
- Inside the `/` filter, digits type into the query. After ⏎ keeps a filter,
  the hits are numbered from 1 and digits are hotkeys again.

## Mechanics

- Bound through `run-shell -C`, because `display-popup` does not expand
  formats in its command and the roster has to be told which client opened it
  (`--client #{client_tty}`). See [Launch speed](#launch-speed).
- Once a second, while open, and before the first frame: ONE tmux call,
  `list-windows -a -F … \; list-clients -F …` (US-separated fields; client
  rows are tagged and have 4 fields, window rows 20). NEEDS YOU, the
  client's current window, each session's current window and its active
  pane's cwd (the strip's branch), and the `@agent_kind` /
  `@agent_detail_kind` / `@agent_detail` options all come out of that one
  snapshot.
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

## WezTerm strip (`CMD+B`)

An always-visible, click-only agent list in a narrow WezTerm split to the
left of the tmux pane. `CMD+B` (wezterm.lua) opens it, and closes it again if
the tab already has one. It runs `agent-ui sidebar` when
`~/.local/bin/agent-ui` exists (see [agent-ui](#agent-ui-primary-and-the-python-fallback));
otherwise this same program, `agent-roster.py --strip`, which the rest of
this section describes.

- **Layout** (designed for 34-40 columns, any height, **never scrolls**):

  ```
   AGENTS  ✕1 ◉1 ◐2 ○1                    count bar, zero counts hidden
   ⏵ next  ☰ menu                         toolbar (or a status / message)
  ╭ NEEDS YOU ✕1 ◉1 ──────────────────╮
  │ ✕ ⬢ Prose extraction model    15m │   state · kind · [project/]title · age
  │   fail  529 overloaded schedule:2 │   detail kind + @agent_detail · where
  │ ◉ ✳ Notch Tasks Orchestrator   3m │
  │   perm  Bash git push ori… main:1 │
  ╰───────────────────────────────────╯
  ╭ main ⎇ main ◉1 ◐2 ○1 ─────────────╮   session · branch · rollup
  │ ◉ ✳ Notch Tasks Orchestrator   3m │
  │ ◐ ✳ Handy.app Speech CLI    ◎  9m │
  │ ◐ ✳ tmux/Tmux Agent Sidebar    6m │   the current window: a background
  │ ○ ✳ ~/Lost suit jacket        12m │
  ╰───────────────────────────────────╯
  ╭ schedule ✕1 ──────────────────────╮
  │ ✕ ⬢ Prose extraction model    15m │
  ╰───────────────────────────────────╯
                                         (spare rows)
   ▸ parked 2 · 1 need you · ☰            always the bottom line
  ```

  NEEDS YOU on top, in `agent-jump.sh list` order (`needs_order`, the same
  parity-tested list as the popup). Then one box per session, in **name**
  order (not "current first": a click moves the client, and the popup's
  order would reshuffle the list under the mouse); inside, agents sorted
  attention (failed > needs-input > done) > working > idle, then window
  index. A window that needs you is listed both in NEEDS YOU and in its
  session, so a box does not change size when an agent asks something.
  Sessions with no agent (and `agents`, `tasks`, `scratch`, `btop-popup`) are
  not shown; parked tabs are the one bottom line. Spare rows sit between the
  last box and `parked`.
- **Shapes and colours** (shape and colour both carry the state, herdr's
  rule): failed red `✕`, needs-input yellow `◉`, done green `✓` (finished and
  unseen; a visit discharges it), working `◐` pulsing pink/blue on
  `@agent_blink` (the window you are on stays blue), idle grey `○`; workflow
  teal `⚙` and computer use blue `◎` pulse like the tab glyphs. The colours
  are not chosen anywhere new: `Roster.shape()` is `dot()` with the glyph
  swapped and `strip_glyph()` is `glyph()` with ⚙/◎ for the Nerd Font ones,
  so cua-notch's palette check (section 65, which reads `dot()`/`glyph()`)
  still covers the strip. A dim `✳` (Claude) or `⬢` (Codex) follows the
  state when `@agent_kind` is set. Titles: `project/` is kept only if it fits
  whole in a third of the width, else the title gets everything
  (`fit_label`).
- **Branch**: dim `⎇ name` after the session name, when it fits. Read from
  the session's current window's active-pane cwd by walking up to `.git`:
  a directory is the git dir, a file (worktree, submodule) points at it with
  `gitdir:`; `HEAD` gives `ref: refs/heads/<name>`, or a detached sha (shown
  as 7 chars). Pure file reads, no `git`; cached per cwd for 30 s
  (`git_head`, `Strip.branch`). It can never stall the strip:
  - nothing at or under `/Volumes`, `/Network`, `/net`, `/home` is read
    (`REMOTE_PREFIXES`; an SMB share whose server dropped would hang a stat
    for the network timeout). A local disk under `/Volumes` shows no branch
    either; a mount table or `statvfs` check was not used because `statvfs`
    itself can hang on a dead mount and `mount` is a fork;
  - `.git` files and `HEAD` are read only if they are regular files, checked
    with `fstat` on a descriptor opened `O_NONBLOCK` (`read_small`), so a
    FIFO named HEAD is refused instead of blocking `open()` forever;
  - each read runs on a daemon thread and a frame waits for it at most 50 ms
    (`BRANCH_WAIT`). A read that takes longer finishes on its own; until
    then, and for 10 minutes (`BRANCH_SLOW_TTL`), that cwd keeps its last
    branch (or none) and starts no new read;
  - the cache drops expired entries on every insert, so a day of `cd`s does
    not grow it.
- **Density ladder** (`LADDER`, `Strip.ladder`). Each frame takes the first
  step whose plan fits the pane height; each step only removes lines, so a
  taller pane never shows less:

  | step | what changes |
  |---|---|
  | `rich` | full layout, plus a dim second line under a working agent with `@agent_detail_kind` `run` |
  | `full` | no run lines. NEEDS YOU entries are 2 lines, every session its own box |
  | `joined` | the boxes share borders: one box, sections split by `├ name ─┤` |
  | `needs1` | NEEDS YOU entries 1 line each (the detail word, e.g. `perm`, moves before the age) |
  | `fold` | in each box, 2+ idle agents (never the current window) become one `○○○ 3 idle` row |
  | `collapseK` | the last K sessions, bottom up, shrink to their header line `├ ▸ bai ○2 ─┤` |
  | `nobar` | the toolbar goes; a status or message moves onto line 1 |
  | `cap` | NEEDS YOU keeps as many entries as fit while sessions keep 3 lines, the rest is `… N more · ☰ menu`; then sessions past what fits become `├ … N more sessions ─┤` |

  Nothing is dropped silently: every hidden thing is counted on a line that
  opens the popup. `StripLadderTests` renders every height 12-80 for 0-40
  agents (34 columns; every third count at 40) and requires exactly `rows`
  lines, no line wider than `cols - 1`, box lines exactly that wide, and
  every NEEDS YOU window and session either on screen or counted. Below 12
  rows the frame is still exactly the pane, just cut off.
- **Mouse only**: SGR mouse reporting (DECSET 1000 + 1006). A left press acts
  on what was drawn at that cell (`Strip.targets`, per line a list of
  `(x0, x1, action)`):

  | where | does |
  |---|---|
  | an agent row (and a run line) | go there (`agent-jump.sh goto`) |
  | a NEEDS YOU entry, either line | go there |
  | the `NEEDS YOU` header | `agent-jump.sh next` |
  | a session header | that session's current window (`window_active`), via goto |
  | `⏵ next` | `agent-jump.sh next` (prefix d) |
  | `☰ menu`, `parked`, any `… more` / `idle` fold row | the prefix q popup on the strip's tmux client |
  | `⟳ restart` (only while the watcher is dead) | respawn the watcher, like `r` in the popup |

  The popup is `tmux display-popup -c <client>` with exactly `bind-key q`'s
  `/bin/dash` line (`roster_popup_argv`; a test compares it with
  tmux.conf.tmpl), started with `Popen` in its own session and reaped on the
  refresh tick, since `display-popup -E` may hold its client until the popup
  closes. The wheel does nothing. A click is resolved against the frame as
  last drawn, and a goto re-checks the window like a number key.
  Every goto (strip clicks, and popup ⏎ / numbers too) passes the session the
  row belongs to: `agent-jump.sh goto <tty> <win> <session>`. Without it, a
  window linked into two sessions resolves to the client's current one, so
  clicking B's header (or B's row) would leave you in A. agent-jump.sh checks
  that the window is still linked there ("has left … · pick it again"
  otherwise) and still refuses EXCLUDE sessions.
- **Focus**: clicking a pane makes WezTerm focus it (and, with the default
  `swallow_mouse_click_on_pane_focus = false`, still delivers the click), and
  every CMD shortcut in wezterm.lua is a `SendKey` to the ACTIVE pane. So each
  click first hands focus back, `wezterm cli activate-pane --pane-id <tmux
  pane>`, then moves. Any key that lands in the strip anyway is forwarded to
  the tmux pane (`wezterm cli send-text --no-paste`) and focus handed back, so
  a CMD+T typed at the wrong moment still reaches tmux. Mouse reports never
  count as typing, not even one cut in two by the 25 ms ESC wait (both
  halves are dropped). If focus cannot be handed back (two `activate-pane`
  failures with a re-resolve between them), the strip says
  `couldn't focus tmux · click it`.
- **Which tmux client**: `wezterm cli list --format json` → the pane in the
  strip's own tab (`$WEZTERM_PANE`) that is not the strip and whose `tty_name`
  is an attached tmux `client_tty` (preferring the pane CMD+B was pressed in,
  passed as `--tmux-pane`). Resolved at start; while there is no such client
  (shown as a dim `no tmux client` line) it retries at most every 5 s, and an
  `activate-pane` that fails re-resolves once. A failed `wezterm cli list`
  keeps the client and pane it already had.
- **Cost**: the same ONE tmux call per second as the popup; `wezterm cli`
  runs only at start, on a click, on a stray key, or while unresolved; the
  branch is a few `stat`s and one small read per session cwd every 30 s; a
  frame (ladder included) renders in ~3 ms with 40 agents. A
  frame identical to the last one written is not written again (popup and
  strip), so an idle strip makes WezTerm repaint nothing; SIGWINCH forces
  a full redraw.
- **Toggle** (wezterm.lua `toggle_strip`): the strip is recognized by the user
  var it sets on start (OSC 1337 `SetUserVar=agent_strip=1`), by its title
  `agent-strip`, or by the pane id recorded in `wezterm.GLOBAL` when it was
  split off (covers the ~30 ms before python paints, so a fast double CMD+B
  cannot stack two). agent-ui sets the same user var and title. Open:
  `pane:split{direction="Left", size=34, top_level=true, args=strip_argv(pane)}`,
  then the tmux pane is re-activated. `strip_argv` picks
  `~/.local/bin/agent-ui sidebar` if `io.open` finds it, else the python
  line below, and appends `--tmux-pane <id> --wezterm <exe>` to either.
  Close: `wezterm cli kill-pane` in the background (the Lua Pane has no
  kill, and CloseCurrentPane would close the active pane, i.e. tmux); either
  implementation exits quietly on the SIGHUP.
- **Absolute paths**: WezTerm launched from the Dock has a thin PATH, so the
  split runs agent-ui by its full path, or `/bin/dash -c` with the same python
  choice as prefix q (Homebrew `python3 -I -S`, else `/usr/bin/python3 -S`),
  sets `PATH` to Homebrew's bin + the system dirs (agent-jump.sh and stash.sh
  call `tmux`), sets `LANG` / `LC_CTYPE` to `en_US.UTF-8` (a Dock launch has
  no locale, and the strip is all box-drawing and state glyphs), and passes
  `--wezterm <executable_dir>/wezterm`.
- **Window size**: while the strip is open the tmux client is 35 columns
  narrower (34 + the split line); tmux resizes the window as for any terminal
  resize, and it grows back when the strip closes.
- **Trying it by hand** without touching the real client:
  `agent-roster.py --strip --client /dev/ttys999` pins a (fake) client and
  never calls `wezterm cli list`; clicks still run agent-jump.sh, so point
  `PATH` at a fake tmux (tests/test_agent_jump_watcher.py's `FakeEnv`) first.

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

- **Which implementation is bound**: `tmux list-keys -T root M-w` shows
  `…/agent-ui menu` or the python line. Not what you expect: check
  `test -x ~/.local/bin/agent-ui`, then `prefix r`.
- **agent-ui popup flashes and vanishes**: run it by hand in a pane,
  `~/.local/bin/agent-ui menu --client "$(tmux display -p '#{client_tty}')"`
  (same care as below: it really moves that client), or render one frame
  with `--once 120x40`. If the binary was removed since the last config load,
  `prefix r` switches back to python. A build that fails shows up in
  `chezmoi apply`'s output; rebuild by hand to see cargo's error again.
- **Popup flashes and vanishes** (python): run it by hand in a pane to see the error:
  `/opt/homebrew/bin/python3 -I -S ~/.config/tmux/scripts/agent-roster.py --client "$(tmux display -p '#{client_tty}')"`
  (or `/usr/bin/python3 -S …` when Homebrew's python is missing).
  Careful: Space/⏎/`g` in that copy really do move that client.
- **Strip says `no tmux client`**: the other pane in its tab is not an
  attached tmux client (e.g. tmux was detached). It re-checks every 5 s.
- **CMD+B does nothing**: `wezterm show-keys | grep -w b` should list
  `SUPER b -> EmitEvent(...)`; run the strip by hand inside a WezTerm split
  to see the error: `~/.local/bin/agent-ui sidebar` (or `agent-ui sidebar
  --once 34x40` for one frame), or for the fallback
  `/opt/homebrew/bin/python3 -I -S ~/.config/tmux/scripts/agent-roster.py --strip`.
- **Red "client is gone" banner**: the tty passed in no longer matches an
  attached client (for example, the terminal was reattached). Close the popup and reopen it.
- **Ages all read the same**: `@agent_since` ("<epoch> <state>") is stamped at
  each transition by the hook's `set_state` (agent-tab-indicator.sh). The
  watcher is the backstop: it stamps changes that bypass the hook (the seen-it
  discharge, the stuck-running reconcile, the GC) and seeds windows it finds
  with no stamp (the idle seed). So every window the watcher seeded at once,
  for example on first deploy, shows that same moment. Ages become accurate
  from the next state change.
