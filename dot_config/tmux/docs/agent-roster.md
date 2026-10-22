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
| Option-W / `prefix q` / `prefix C-q` popup | `agent-ui menu --client <tty> [--tab all\|active\|needs\|working\|idle\|parked]` |
| CMD+B WezTerm sidebar | `agent-ui sidebar [--client <tty>]` (wezterm.lua also passes `--tmux-pane`, `--wezterm`) |
| `prefix a` / `prefix C-a` session picker | `agent-ui sessions --client <tty>` (see [below](#agent-ui-sessions-prefix-a)) |
| screenshot / test render | `agent-ui sidebar --once WxH`, `agent-ui menu --once WxH [--client <tty>] [--tab x] [--select <letter>] [--preview] [--plain]` (read-only: never writes `@agent_key`), `agent-ui sessions --once WxH [--client <tty>] [--query q] [--ansi]` |

- **The agent-ui menu** (the Python fallback keeps the older layout described
  [below](#layout)) opens on **All**: every agent, grouped by space in the
  sidebar's fixed by-name order, then by window index. The order never
  depends on state, age or which session you were last in: an agent going
  from working to needs-input keeps its row and only changes glyph and
  colour; rows appear or disappear only when windows do. There is no NEEDS
  YOU section and no duplicate row (only a window linked into two spaces is
  listed under both): a needs-you agent stays in place, its title turns the
  needs colour (yellow; red failed, green done) and its reason shows inline
  (`asks Which deck?`, `perm Bash git push`, `failed`). Parked tabs fold
  into one row at the end, `▪ N parked ⇥`, which opens the Parked filter.
- **Header**: one status line in the top border, `agents · ◉ 1 need you ·
  ◐ 4 working · ○ 6 idle · ▪ 6 parked` (zero counts dropped; when narrow the
  last ones drop whole), plus the filter's name when it is not All
  (`Working ⇥`, click to cycle). A red banner there flags a stalled watcher
  or a gone client.
- **Rows**: `[letter] glyph Title  reason  · here ·  project  age`. The
  letter badge is reverse video on the agent's state colour (needs yellow,
  failed red, working blue, done green, paused violet, idle overlay); a `project/` prefix
  of the title moves to the dim tag on the right (a bare `~/` is dropped);
  `· here ·` marks the window the popup is covering; idle titles are dimmer
  but readable. Short of room the project tag goes first, then `here`.
- **Filters** (Tab / Shift-Tab): All, Active (working or needing you, or a
  prompt just answered), Needs you (agent-jump.sh's queue, in its order),
  Working, Idle (paused agents too), Parked (every parked tab). The sidebar's idle fold opens
  the menu on Idle, its `… N more` rows on All.
- **Paused** (the pause key's stopped agents, [agent-pause.md](agent-pause.md)
  "The paused state"): a state of its own, `⏸` in electric violet (`violet`,
  `#7f6bff`, the same on every surface; it was lavender `#b4befe`) on the
  row's glyph, its letter badge and a `⏸ N paused` header count (between working and idle). Paused is
  `@agent_state` `paused`, or `done` / `idle` on a window whose
  `@agent_paused` is set (before the tab hook writes `paused`); a window
  still running or asking with the flag keeps its live state. Never in
  NEEDS YOU, the Needs you or Active filters, never done-green; ranked with
  the quiet states. agent-ui folds it in where it parses a row
  (`model::paused_state`, so every reader sees one state); the Python
  fallback the same in `parse_windows` (`paused_state`), with its own
  bucket in `CATS` and a violet `⏸` dot. Both read `#{@agent_paused}` as
  the last field of their window format.
- **Initial cursor**: the oldest agent that needs you (the first in
  `agent-jump.sh list` order), else the last agent (where ⌥W would go), else
  the window the popup covers, else the first row. A state change never
  moves it; if the selected row goes away it moves to the nearest surviving
  row, and ⏎ / Space typed before a frame showed that are refused ("that tab
  moved · pick it again"), never retargeted.
- **Pinned**: a pinned agent (the sidebar's PINNED section, [Pinned
  agents](#agent-ui-sidebar-layout-sync-lifecycle)) keeps its row where it
  is and wears a dim pin glyph and its ⌥W digit after the title (`` 2``);
  Space p pins or unpins the selected agent.
- **Preview**: off by default. Space v turns on a strip of up to 10 lines
  under the list, full width, its rule naming `title · session:index · ✳
  claude · ⌥ branch · ~/path`; the pane's tail is captured on a worker
  thread only while it is on.
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
    re-binds q, C-q and M-w to `display-popup -E -B -w <cap> -h 75% …/agent-ui
    menu --client '#{client_tty}'`. agent-ui draws its own rounded border and
    title, hence `-B` and no `-T`. The width is capped at 100 columns (the
    whole client when narrower) so a row's title, project tag and age stay in
    one glance: `-w '#{?#{e|<:#{client_width},100},#{client_width},100}'`,
    expanded by the binding's `run-shell -C` (display-popup itself neither
    expands formats in `-w` nor accepts a width wider than the client; the
    sidebar's ≡ button asks for `#{client_width}` and applies the same cap).
    After installing or removing the binary, `prefix r`.
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

agent-ui menu keys:

| Key | Does |
|---|---|
| **Option-W**, or `prefix q` / `prefix C-q` | open the menu |
| an agent's **letter** (`a s d f g h j k l q w e r t y u i o p z x c v b n m`, capitals too) | go to that agent at once and close; the letter is its lifetime `@agent_key` (see [Letter keys](#agent-ui-menu-letter-keys)) |
| **Option-W** inside the menu | go to the [last agent](#last-agent) (so ⌥W ⌥W flips between two agents); with none, close |
| ↑ / ↓, C-p / C-n, PgUp / PgDn, Home / End, wheel | move (`j` / `k` are letters now) |
| ⏎ | go to the selected row (a parked tab comes back via `stash.sh unstash`; the `▪ N parked` row opens the Parked filter); a click selects, a second click goes |
| Tab / Shift-Tab | cycle the filter: All, Active, Needs you, Working, Idle, Parked |
| `/` | search title, session, `session:index`, detail and path as you type (letters and digits type); ⏎ goes to the selected match; Esc leaves and clears it (a second Esc closes); Backspace on an empty query leaves it |
| Space | the actions strip for the selected row, in the footer; the next key runs one, Esc cancels: `p` pin / unpin (the sidebar's PINNED section, at once, no y/n; the strip says which), `s` park (`stash.sh stash`, after y/n), `x` close the agent's pane through `closed-tabs.sh` after y/n (CMD+Z undoes it; on a parked row: discard via `stash.sh kill-many`), `d` its uncommitted diff in a popup (any row, parked too: the menu closes, and `agent-ui diff --after <menu pid>` opens it once the menu's popup is gone; see [Agent insight](#agent-insight-srcinsightrs)), `v` preview strip on/off (was `p`), `r` restart the watcher, ⏎ go. A parked row offers neither `p` nor `s`. A Claude agent waiting on a permission prompt offers `y` allow and `n` deny first (at once, no y/n: see [Answering a permission prompt](#answering-a-permission-prompt-srcanswerrs)) |
| Option-S / Option-X | next tab that needs you (agent-jump.sh) / one step back through the tabs you were on (nav.sh, the same Option-X as everywhere: [nav.md](nav.md)), and close. When CuaNotch's `keyJumpToNeedy` is opt+s (this machine's setting), CuaNotch owns Option-S, so while it runs only Option-X reaches the menu |
| Esc / C-c | close |

The menu also closes by itself when the client is moved to another window
by someone else while it is open (CuaNotch's Option-S jump, a tab click):
a refresh that finds the client's row on a different window than at open
ends it rather than leave the popup stale over the new window. A missing
client row never counts, and the menu's own park/close of the client's
window is followed, not closed on.

Python fallback keys:

| Key | Does |
|---|---|
| **Option-W**, or `prefix q` / `prefix C-q` | open the roster |
| a row number | row positions, `1`-`9` then `01`, `02`, … (see [Number keys](#python-fallback-row-positions)) |
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

Related keys: **Option-S** jumps to the next thing that needs you
(scripts/agent-jump.sh). **Option-X** / **Option-C** go back / forward through
every tab the client has been on, a jump included (scripts/nav.sh,
[nav.md](nav.md)); the menu's Option-X and the sidebar's `⤺ back` button run
the same `nav.sh back`. `prefix D` still undoes a whole run of jumps in one
step (agent-jump.sh `back`). When CuaNotch's
`keyJumpToNeedy` is opt+s (this machine's setting), CuaNotch owns Option-S as
a global macOS hotkey and tmux's `bind -n M-s` (`agent-jump.sh next`) is the
fallback, firing only when CuaNotch isn't running or hasn't claimed the key.
CuaNotch merges tmux's queue (`agent-jump.sh list`, which includes the
client's own window) with its own rows (Codex / Claude desktop) and picks one.
It skips the client's current window only while WezTerm is frontmost. For a
tmux target it runs `agent-jump.sh jump <client_tty> <win> <session>` (exit
codes and the full contract are in the script header) and activates WezTerm.
A `jump` onto the window the client already shows returns 0 and clears its
tint. `jump` records the same back-chain as `next`, even when two jumps race,
so `prefix D` still returns to where the jumps began; Option-X steps back from
a CuaNotch jump like from any other move. `prefix d` /
`prefix C-d` are always tmux's `next`.

## agent-ui sessions (`prefix a`)

The session picker, a port of `scripts/mru-session-switch.sh` (bash + fzf,
with `preview_session.sh`), which stays bound as the fallback the same way
the python roster does (the agent-ui `if-shell` block re-binds `a` and
`C-a`, `display-popup -E -B -w 60% -h 75%`).

- **Rows**: sessions by `session_last_attached`, newest first (ties by name,
  like the script's `sort -k1,1nr`), minus the client's own session and
  `scratch`, `agents`, `tasks`, `stash`. Each row: the name, then the agent
  rollup exactly as the script's `decorate()` computes it: a ● in the colour
  of the most urgent window (failed red > needs-input yellow > done green,
  not while `@agent_workflow` is set > in flight pink > idle overlay), then
  `N needs you · M working` or `N idle`. The dot does not pulse (the
  script's static colours, not the core's `dot_color`). Unit tests run the
  script's own `decorate()` and list pipeline (tmux faked) and diff them.
- **Filter**: typing matches the NAME only (never the rollup, so a new name
  like `work` can't hit "2 working"), case-insensitive: exact, then prefix,
  then substring, then subsequence; MRU order within each. The first match
  is selected. ↑/↓, C-p/C-n, C-k/C-j move; Backspace, C-u (clear), C-w
  (word). Esc or C-c closes (Backspace on an empty query does not: an extra
  Backspace while fixing a typo shouldn't close the picker).
- **⏎**: a match → `switch-client -c <tty> -t =<name>`. No match: an empty
  query closes; `scratch` closes; an invalid name (not `^[A-Za-z0-9_-]+$`)
  shows the script's message inline and stays open; an existing session
  (typed in full, e.g. `stash`) is switched to; anything else asks inline,
  "Create and go to [name]? Y/n": ⏎, y or Y runs `new-session -d -s <name>
  -c ~` and switches, any other key goes back to the list.
- **Preview**: the bottom rows of the selected session's active pane
  (`capture-pane -ep -t =<name>:`), coloured, in the lower ~70%; captured on a
  worker thread 40 ms after the selection settles, then every second.
- **Speed and typed-ahead keys**: raw mode is entered first thing, with
  `TCSANOW` (crossterm's `enable_raw_mode`; `TCSAFLUSH` would drop them), and
  the reader thread starts at once, so nothing typed after the key that
  opened the popup is lost. Bytes that arrived while the tty was still
  cooked (counted with `FIONREAD` on entry) went through ICRNL, so their LF
  is Enter. Then ONE tmux call (the core snapshot), the queued keys are
  applied to the loaded list (an early ⏎ acts on the filtered rows), and the
  first frame is drawn. `AGENT_UI_SESSIONS_TRACE=<file>` appends
  `first_frame_us`, `done_us <outcome>` and `cooked_bytes` lines.

Measured 2026-10-09 on a private `-L fastpick` server with a real client
(4 sessions), F12 bound like `prefix a`:

| | old (bash + fzf) | agent-ui sessions |
|---|---|---|
| key → list on the client's screen | 70-88 ms | 21-42 ms (the menu: 21-24 ms on the same harness) |
| first frame, in-process | — | 5.5-13 ms |
| "beta" ⏎ typed 1, 2, 5 ms apart (one control-mode client) | 0/10 landed each | 10/10 each |
| "beta" ⏎ typed 5 / 15 ms apart (a `send-keys` process per key) | 20/20, 20/20 | 20/20, 20/20 |

Keys that reach tmux before the popup exists at all (the same `send-keys`
command as the opening key) still go to the pane underneath: tmux routes
them before the binding's popup is created, for either picker.

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
the tab bar's hues, the row's number key (in agent-ui the agent's slot, see
[Number keys](#number-keys)) (NEEDS YOU and filter rows add a
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

### agent-ui menu: letter keys

The agent-ui menu takes no number keys any more: every listed agent owns a
home-row-first **letter** for its whole life, drawn as a badge at the start
of its row, and pressing it goes there at once (no second key, no wait).
Shells draw from the SAME pool (one set of letters for agents and shells,
`menu::letters::lettered`): the menu lists only agents, but a shell's
letter pressed there still goes to that shell.

- **Who has one**: every agent window (an `@agent_state`, in a shown
  session: not parked, not in `agents`/`tasks`/`scratch`/`btop-popup`),
  idle ones included, AND every shell of every space (a window without an
  `@agent_state` in a session that is not `@space_hidden`). Never a space's
  home window (`@space_home`: the space itself, reached by ⌘N), never a
  parked tab. A window linked into two sessions is one window (one letter,
  keyed by window id). A window keeps its letter for life across the
  shell/agent change: a shell where claude starts stays listed, and so does
  an agent that exits back to its shell.
- **One assignment, two UIs**: the menu and the CMD+B sidebar both call
  `menu::letters::of_view` (snapshot + unscoped view + their own in-flight
  `KeyMemo`), so every open menu and sidebar shows and writes the same
  letters from the same snapshot; a write tmux already shows is never sent
  again, and two writers that raced converge on the next refresh (a
  duplicate goes to the lower window id). spaces.conf's ⌥W `jump` table
  reads the same `@agent_key`, for agents and shells alike (never a home).
- **Alphabet**, in hand-out order: `a s d f g h j k l q w e r t y u i o p z
  x c v b n m` (26). Past 26 windows a row has no badge but is still
  navigable.
- **Stored** on the window as the tmux user option `@agent_key`, read in the
  same snapshot as everything else (no extra tmux call). On each refresh
  (`menu::letters::assign`): a window whose `@agent_key` is a valid letter
  keeps it, unless another listed window holds it too, where the lower
  numeric window id wins and the other is reassigned; every window without
  a valid letter takes the lowest free one in alphabet order, windows taken
  in DISPLAY order (spaces in ⌘N order, then by name; in each, its agents by
  window index, then its shells). A letter whose holder is no longer listed
  (closed, parked, made a home) is free at once; if an unlisted window
  still carries a letter a listed window now holds, its stale `@agent_key`
  is unset, so it gets a free letter (not its old one back) when it
  returns. A parked window whose letter nobody took gets it back on
  unpark. Readers that care only about agents (tmux-mcp, the
  tmux-workspace recipes) trust `@agent_key` as an agent's only while
  `@agent_state` is set: a key without a state is a shell's letter.
- **Writes**: the frame shows the assignment immediately (in memory); only
  when it differs from what tmux stores does the menu (or a live sidebar)
  send ONE batched
  command (`set-option -w -t @id @agent_key x \; set-option -wu -t @id2
  @agent_key …`) from a background thread, never blocking a frame. A write
  in flight is remembered (`KeyMemo`): it counts as the window's key until a
  snapshot shows it, and is never re-sent each tick. `--once` never writes.
- **What a letter resolves against**: the frame ON SCREEN. Each letter maps
  to the window that held it when the frame was drawn (its first row on
  screen, else its first row in the All order, so a letter whose row is
  scrolled off or on another filter still goes; a shell, which has no row,
  goes to the shell in its space). If that window closed and a
  newcomer took its letter before the next frame, the key still means the
  old window (the jump fails and says so), never the newcomer. A letter
  nobody holds says "no agent on k".
- The key bar shows the range in play in alphabet order, e.g. `a–j  go`.
- `menu --once … --select <letter>` selects that agent's row.

Neither the menu nor the sidebar draws the sticky slot numbers below any
more: the sidebar shows the letters too (see
[agent-ui sidebar](#agent-ui-sidebar-layout-sync-lifecycle)). The slots are
still computed (the watcher stamps `@agent_slot`, `view::assign_slots`) and
documented here because `agent-ui dump` carries them.

### agent-ui: sticky slots

In agent-ui every number is an agent's **slot**, not a row position, and it
sticks to the agent (no UI draws it now; the sidebar and the menu use the
letters above).

- **Who has one**: the ACTIVE set only, exactly the menu's Active tab:
  working (running, a workflow or cua out), needing you (failed,
  needs-input, done without a workflow: agent-jump.sh's queue), or a prompt
  just answered ("idle AND @agent_pending is an epoch no older than 600 s",
  the watcher's rule: you went to answer it and its turn has not resumed
  yet; a junk or empty stamp never counts, and once the stamp is 601 s old,
  say after answering No, the agent is plain idle and lets its number go),
  in a session outside agent-jump.sh's `EXCLUDE` (agents, tasks,
  stash, scratch, btop-popup). The answered agent stays on the Active tab
  with its number (drawn as idle `○`) and is never folded into the
  sidebar's idle line, so jumping to a question never costs it its number;
  it is NOT in NEEDS YOU, which stays byte-identical to `agent-jump.sh
  list`. Other idle agents, parked tabs and plain windows have none and
  show no number.
- **How numbers are given** (agent-tab-watcher.sh stamps `@agent_slot`;
  agent-ui computes the same thing from the same snapshot, `view::assign_slots`,
  so a new agent is numbered in its first frame instead of up to a second
  later): an Active agent keeps its valid stamp (`^[1-9][0-9]{0,8}$`; on a
  duplicate the lower numeric window id keeps it); a stamp on an agent that
  is no longer Active is ignored, its number free at once; every Active agent
  without one takes the LOWEST free number, in (session name in plain BYTE
  order, so `Zeta` before `alpha`; window index; numeric window id) order. A
  window linked into several sessions sorts by its byte-smallest non-hidden
  session and that link's index. The watcher sorts exactly the same way, so
  the two always agree. Nobody else's number ever changes: with 1-4 held, 3
  going idle frees 3, and 1, 2 and 4 stay put; the next agent to become
  active takes 3.
- **Provisional numbers stick** (`view::SlotMemo`, one per menu / sidebar
  process): an Active agent shown before the watcher stamped it keeps the
  number it was first shown with until it is stamped or leaves the Active
  set. A later arrival that sorts before it takes the next number free of
  both the stamps and those remembered numbers, never its number. A valid
  stamp always wins over the memory (if it differs, the stamp is adopted;
  rare, since the hook stamps synchronously). `--once` renders have no
  memory and show the watcher's assignment exactly.
- **Labels**: zero-padded to the digit count of the HIGHEST slot held,
  across everything, not just the rows on screen. Up to 9: one digit
  (`1`…`9`). Once any slot is 10 or more, every label is two digits (`01`,
  `03`, `12`) and a number is complete after two keys. Equal widths keep the
  set prefix-free, so nothing waits on a timer; a bare `0` is never a label.
  A window linked into two sessions shows the same number on both rows.

**Sidebar**: no longer draws slots (nor the space-scope 1-9 ordinals;
Command-N still goes by position): every agent row, shell row and NEEDS
YOU card starts with the window's [letter](#agent-ui-menu-letter-keys)
badge instead.

Known limit: two unstamped agents that first appear in the SAME frame are
numbered together in fill order; the watcher numbers them the same way.
Once a number is shown, it stays (above).

### Python fallback: row positions

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
  only on `x` (and on peek) and only for a split window, matching a comm of
  `claude`, `codex`, `opencode` or a bare version like `2.1.291`.
  `pane_current_command` is not used: codex launched through npm shows there
  as `node`. The window's `@agent_kind` steers it (`pick_agent_pane`'s
  `kind`, mirrored from agent-roster.py with the same tests): an `opencode`
  pane counts only in an opencode window, so `opencode serve` split beside a
  Claude is never what `x` closes; among several agent panes the one whose
  comm matches the kind (`comm_kind`: codex, opencode, else claude) wins
  before the active-pane fallback. A one-pane window closes that pane; a
  split window where no pane runs an agent, or several do and neither the
  kind nor the active pane picks one, is refused ("can't tell which pane is
  the agent · close it from the tab") rather than guessed at.
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

### Last agent

The data behind "Option-W Option-W jumps to the last agent" (a Cmd-Tab tap).
tmux keeps two GLOBAL options, each a window id (`@12`) or empty:

| option | meaning |
|---|---|
| `@agent_mru_cur` | the agent window a client most recently landed on |
| `@agent_mru_prev` | the agent window landed on before that; never equal to cur |

- **Agent window** = `@agent_state` is set. It is the one marker present on
  every live Claude/Codex/OpenCode window, idle ones included: the hooks write it and
  the watcher seeds `idle` for any agent process it finds without one, and its
  GC unsets it when the agent is gone (the same field the roster and
  agent-jump.sh filter on). `@agent_kind` would mostly work too, but it is a
  side field, never the presence test.
- **Landing** on agent window W: if W differs from cur, prev := cur and
  cur := W. Landing on a non-agent window changes nothing, so
  agent → shell → agent still pairs the two agents.
- **Landed** means W is now some client's current window
  (`#{window_active_clients}` non-zero). A `select-window` in a session no
  client is looking at records nothing until a client switches there.
- **The window being left is handled first.** A window can become an agent
  while the client sits on it (claude started in a shell tab), so landing on
  it recorded nothing. The internal option `@agent_mru_here` holds the window
  of the last landing, agent or not. On every landing, if `here` is now an
  agent and isn't cur, it is pushed first (prev := cur, cur := here), and
  then the landing rule runs. So B → shell → (claude starts) → B gives cur=B,
  prev=that window, however you leave it (the menu, Cmd-N, switch-client).
  `@agent_mru_left` holds that step's intermediate value. Readers ignore both
  options. `window_last_flag` / `client_last_session` are not used, because
  after kill-window, or agent-jump's select in a session the client isn't
  on, they name windows the client never looked at.
- A burst of selects in one command list runs the hooks after the whole list,
  so the windows passed through record nothing and the chain stays
  consistent. A killed `here` is simply not found, so it is never pushed.
- Kept by `session-window-changed[2]` and `client-session-changed[2]` in
  tmux.conf.tmpl, one identical `{ … }` block of `set -gF` commands each: no fork, no
  watcher tick, so it is current the instant the client moves. Not
  `after-select-window`: that one also fires for a no-op select and then
  expands `#{window_id}` against the client's OLD session (the trap noted at
  the clear-current hooks). `session-window-changed` fires only on a real
  change of a session's current window (select / next / previous /
  last-window, a tab click, Cmd-N) and `client-session-changed` covers a
  `switch-client` (agent-jump.sh, the menu's go) into a session whose current
  window stays put.
- A closed window's id can linger in either option; the reader treats an id
  that isn't a listed agent as absent.
- Tests: `tests/test_agent_last.py` (throwaway `-L` server with `-f
  /dev/null`, sources only these two hook blocks, attaches one client from a
  pty it opens itself).
- Check it live: `tmux show -gv @agent_mru_cur; tmux show -gv @agent_mru_prev`.

## WezTerm strip (`CMD+B`)

An always-visible, click-only agent list in a narrow WezTerm split beside the
tmux pane: on the right by default, or the left with `STRIP_SIDE = "Left"`
in wezterm.lua. `CMD+B` (wezterm.lua) opens it, and closes it again if
the tab already has one. It runs `agent-ui sidebar` when
`~/.local/bin/agent-ui` exists (see [agent-ui](#agent-ui-primary-and-the-python-fallback));
otherwise this same program, `agent-roster.py --strip`, which the rest of
this section describes. agent-ui's sidebar starts each agent row, shell row
and NEEDS YOU card with the window's lifetime letter, the same one the menu
and ⌥W use (see [letter keys](#agent-ui-menu-letter-keys)), and replaces the
tmux status line while it is open. agent-ui's sidebar has its own layout
(status block, NEEDS YOU, agents and shells by space, the dock; no log), its own sync (Darwin notices, not a poll) and leaves when
its tmux client does: see [agent-ui sidebar](#agent-ui-sidebar-layout-sync-lifecycle).
**The Python fallback (`--strip`) keeps the older layout and the 1 s poll
described in the rest of this section; it was not changed.**

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
- **Scope** (`Strip.scope`, global option `@strip_scope` = `space` | `all`).
  `all` is the layout above. `space` shows only the client's current session
  (a "space"): its one box, titled `SPACE` with that session's counts
  instead of `AGENTS` and the global ones, shown even when it holds no agent
  (`no agents in this space`), rows in window-index order, each starting with
  its **agent number** (`agent_ordinals`): the position among the session's
  windows with a non-empty `@agent_state`, by `window_index`, 1-based, shown
  for 1-9 only (10+ keep the blank column). It was the number ⌘N jumped by;
  since 2026-10-21 ⌘N is the space key and no key uses it (agents are
  reached by their ⌥W letter; agent-ui's sidebar does not draw it). The number takes a
  2-cell column only while the title keeps 4 cells, so narrow strips lose it
  first. NEEDS YOU and `parked` stay global in both scopes: an agent needing
  you in another space still surfaces. Unset, the scope is `space` when
  `@spaces` is `on`, else `all`; with `@spaces` off and `@strip_scope` unset
  the strip is byte-for-byte the classic one (tests/test_strip_scope.py
  `TodayTests` pins it). **Option-V** ("view"; not Option-S, which is tmux's
  root `M-s` = agent-jump next and must still reach tmux from the strip)
  typed into the strip, or a click on the
  title (only once `@spaces` is on or `@strip_scope` is set), flips the scope
  and persists it with `set -g @strip_scope`, so every strip and the next
  CMD+B agree; one set elsewhere shows up on the next tick.
  **Shells**: with `@spaces` on, a window without `@agent_state` is never an
  agent row, in either scope (the tab bar lists shells). With it off, the
  current window stays a row even as a plain shell (the popup's rule).
  The inputs cost no extra call: `CLIENT_FMT` carries `#{session_name}`,
  `#{@spaces}` and `#{@strip_scope}` on each client row of the one
  `list-windows \; list-clients` call (a client-context format falls back to
  the global session options); any client's row supplies the options when the
  strip's own client is missing. `@spaces` counts as on only when it is
  exactly `on` (spaces.conf's test). The scope in effect (`Strip.scope`) is
  `all` whenever the client's session is unknown, one of the hidden ones
  (`agents`, `tasks`, `scratch`, `btop-popup`) or `stash`, none of which is a
  space; title, counts and body follow it, and the toggle still flips the
  chosen `@strip_scope` (`chosen_scope`).
  **agent-ui's sidebar** (used instead when installed) implements the same
  scope (`view::Scope`, `ViewModel::apply_scope`): same options, same
  resolution, same hidden/stash fallback, same ordinals (shells, though,
  are rows of their own under each space's agents; see below), same
  Option-V / title toggle (`set-option -g @strip_scope`, the old scope
  kept and `couldn't set @strip_scope` shown on failure; the toggle is the
  AGENTS header's scope word once a client is known, else the ` agents` /
  ` space` title).
  In its own layout: the header's counts are that session's in space scope,
  the agents section (`agents  this space`, else `by space`) holds only the
  client's session's rule and rows (its agents and shells); every row keeps
  its letter badge (no ordinals any more), and NEEDS YOU, the spaces list
  and parked stay global. A `@strip_scope` set from outside (tmux's `M-v`)
  shows at once when the binding posts the focus notice (see
  [agent-ui sidebar](#agent-ui-sidebar-layout-sync-lifecycle)). Its client row carries one more field than the
  Python's, `#{@space_hidden}` (the client's session's), for the hidden
  test. Spaces off and `@strip_scope` unset: frames byte-identical to before
  (`tests/sidebar.rs` `once_follows_the_scope_options` checks unset → set →
  unset).
- **Shapes and colours** (shape and colour both carry the state, herdr's
  rule): failed red `✕`, needs-input yellow `◉`, done green `✓` (finished and
  unseen; a visit discharges it), working `◐` pulsing pink/blue on
  `@agent_blink` (the window you are on stays blue), paused violet `⏸`
  (steady), idle grey `○`; workflow
  teal `⚙` and computer use blue `◎` pulse like the tab glyphs. The colours
  are not chosen anywhere new: `Roster.shape()` is `dot()` with the glyph
  swapped and `strip_glyph()` is `glyph()` with ⚙/◎ for the Nerd Font ones,
  so cua-notch's palette check (section 65, which reads `dot()`/`glyph()`)
  still covers the strip. A dim `✳` (Claude), `⬢` (Codex) or U+F489
  (OpenCode, Nerd Font nf-oct-terminal) follows the state when
  `@agent_kind` is set. Titles: `project/` is kept only if it fits
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
  | the `AGENTS` / `SPACE` title (once a scope is in play, see Scope) | flip `@strip_scope` |

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
  a CMD+T typed at the wrong moment still reaches tmux. The one key kept is
  Option-V (`ESC v`/`ESC V`, `SCOPE_KEY`), the scope toggle. Mouse reports never
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

## agent-ui sidebar: layout, sync, lifecycle

What `agent-ui sidebar` (CMD+B, when installed) does differently from the
Python strip above. Code: `agent-ui/src/sidebar.rs`, `src/sidebar/layout.rs`,
`src/sidebar/status.rs`, `src/sidebar/notice.rs`.

- **The sidebar is the whole UI**: while any sidebar is open the tmux status
  line is hidden (`set -g status off`) and everything it showed is drawn in
  the sidebar's status block (below). See **Status line** further down.
- **Layout** (top to bottom; `agent-ui sidebar --once 34x40 --client <tty>`
  renders one frame):

  ```
   ▄ prefix ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄    the usage panel (`tile_lo`, half-block
    S ━━━━━┃━━━━━━━━━━━    21%   −4%     caps); the key table's mark a solid tab
    W ━━━━━━━━━━┃━━━━━━    60%  +17%     on its top cap; one meter per window
   ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
                                         a blank row above each section
   NEEDS YOU                        2    agent-jump.sh order: cards in one tint
   ▌  a  Slides export        ✕ error    of the state colour, a heavy ▌ rail,
   ▌     AX tree came back empty  3m     the key-cap solid, the reason in text

   PINNED               ⌥W 1-2   2    pinned agents, MOVED here, pin order;
   ▎  f  Kua Yu focus ti…  ◉ asks      every scope; ⌥W's digit under the letter,
   ▎  1  Which deck sho… main · 5m     the space before the age (see Pinned
      k  BAI Info Session Slides       agents below)
      2  Slides 4-9 rew… work · 9m

   AGENTS               ⌥V all spaces    THE BODY: every space (or the client's
  ▌ 1  main   main          ◉1 ◐1 ○1    in space scope); the client's space a
   ▎  d  Sidebar status   ⠹ working      BAND of its colour with a ▌ at the edge
   ▎     › Running cargo test     26s    (only it: "where am I"), any other
      h  Canvas Syllabus                 header on the canvas; ` N ` solid in
         Synced 4 assignments      1h    the colour, the name bold in it. The
      j  >_ agent-ui                     header IS the space's home (click: go
      k  >_ nvim                         home). Every agent a 2-line card on
                                         the card grid: rail, key-cap, title
    2  cs35l   main                ○1    (bold when it has a state), the state
   ▎  w  Eight Component     ✓ done      word right; idle says NOTHING (no
   ▎     I wrote tic-tac-toe       9s    rail, no word). Then the space's
                                         shells, one row each on the canvas
                                         (`>_` here = the terminal glyph)
                                         spare rows collect here
   ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄    THE DOCK: one panel to the last row
   ▌ 1  main             ◐1 ○3           every space a row (the client's on its
     2  bai                 ○2           band, its chip red on prefix), or chips;
    PARKED  3                            PARKED: always one line, a message or
   ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀    the watcher on the right
  ```

- **Spaces, homes and shells**: a space's HOME window (`@space_home`) is
  never listed; the space's header stands for it. A click on the header
  (or on the space's dock row or chip) goes to the home window
  (`actions::space_landing`: the home, else the session's active window),
  and while the client is ON the home the header wears the client's own
  lift instead of its band (`tile_me` and a full lavender cell down its
  right edge, as the client's own agent card). Every other window without
  an agent state is a SHELL of its space, listed in its space's group
  after the agents, one row each (`layout::shell_row`): on the card grid
  (rail, key-cap, label) but on the CANVAS, no tile, so it reads quieter
  than the agent cards; the key-cap exactly an agent's (its letter from the
  same pool, inverted while ⌥W is armed); then the terminal glyph
  (`SHELL_GLYPH`, nf-fa-terminal U+F120, faint) and the label in `sub`:
  the folder while it sits at a shell prompt (`~`, or the cwd's last
  part), else the running command (`view::shell_label`, spaces.conf's
  `@space_shell_label` rule; a home's dashboard pane never lends its
  name). The client's own shell is lifted like its own agent card; prefix
  v's selection tints it mauve. A click goes there (`Target::Shell`,
  agent-jump.sh goto; a right-click never parks it). In the steps that
  fold the idle agents, two or more shells fold into one line of pills
  (`layout::shells_line`: each its cap and label, `+K` for those that do
  not fit). In ALL scope every space with agents OR shells gets a group,
  so a space with only shells has one; a `@space_hidden` session lists no
  shells. There is no SHELLS section any more, and no shells' line in the
  dock.

- **Pending spaces** (docs/spaces.md "Pending spaces"; `@spaces` on only):
  a claude in the hidden `pending` session building space N (its window
  has `@pending_space` N) is the one hidden-session window the sidebar
  lists, as a space of its own AT N (`view::Space::pending`, named by its
  registry row `pending:@W`), in THE space order, in AGENTS and the dock:
  the header ` N ` solid in `@pending_color` (the colour N will have),
  `creating…` dim italic where the name goes, the working spinner after it
  while the claude works (its counts otherwise); under it the claude's
  ordinary card (letter, blink, rail, state word), whose second line is
  what it does now (todo, blurb), else what it asks, else
  `@pending_request` after the prompt mark. The dock row and chip say
  ` N  creating…` the same way. A click on the header, the dock row or
  chip (`Target::Pending`) or on the card (`Target::Go`, which
  `Actions::go` routes) runs `space.sh pending-open @W <client>` (its
  popup; never a select, the client stays where it is). It counts in the
  header and NEEDS YOU like any agent (its NEEDS YOU card's cap in the
  pending colour; a click there opens the popup too), it is lettered in
  display order at N (⌥W + the letter: spaces.conf's
  `@space_jump_pending`), a middle-click pins it (PINNED says `creating`
  where a space's name would be), a right-click (park) does nothing. The
  menu lists it as a group `N · creating…`, Enter its popup. On adoption
  the options clear and the window is an ordinary agent of its space.
  With `@spaces` off nothing changes (the `pending` session is the hidden,
  unnumbered group it always was).

  The look follows OpenCode's and herdr's sidebars: the sidebar paints
  its own canvas, `mantle` (#181825, a shade under the terminals' `base`),
  so it reads as chrome, never as a terminal printing coloured text; on it
  sit raised surfaces with one canvas column at each side; hierarchy is
  tone and weight (bold `text` titles over `overlay2` details, bold
  `overlay2` section labels), not lines; blank rows group sections and
  spaces. Palette: the zebra `tile` (#30314a) and `tile_lo` (#28283b): every
  card is one colour, both rows, consecutive agent cards alternating the
  two, afresh under each space's header (shell rows sit on the canvas);
  `tile_hi` (#31394f: lifted the old SHELLS section's active shell; no row
  wears it now, but bands still keep apart from it); `tile_me` (#474a66,
  lavender 30% over the canvas: the client's own agent or shell, or its
  space's header while it is on the home, below); the two
  panels (usage, dock) `tile_lo` behind `▄` / `▀` caps. The client's
  space's band is its colour over the canvas at the least strength (16%
  up) that keeps it 20+ RGB apart from the canvas and every card shade
  (`layout::band`), so it never melts into the client's lifted card under
  it. Chips are square (the tmux status line's catppuccin blocks): ` key `
  bold in dark ink on a colour, then ` label ` on `surface0`, for the
  NEEDS YOU and PARKED counts, the dock's spaces and `+K`. `colour0`-`colour15`
  space colours are read as WezTerm's catppuccin-mocha ANSI colours (what
  the terminal draws), not stock xterm's. The panel shows no space chip
  (the dock says which space the client is in) and no shells. Under
  pressure every agent row stays the same height: idle agents fold first
  (a space's shells into one line with them), then every agent goes to one
  line at once.
- **Cards and state**: a state rail `▎` down a card's left edge in its
  state's colour (blue working, yellow asking, red failed, green done,
  violet paused; the client's own idle agent lavender; the stash
  selection a heavier mauve `▌`), its glyph and word bold in that colour on
  the right (`⠹ working`, `◉ asks`, `✕ error`, `✓ done`, `⏸ paused`; the
  glyph alone when narrow). A PAUSED card's second line says `paused` (bold
  violet) in place of the agent's last reply, then, while a pause is
  saved (the banner's stopping, safe or ready), the key that resumes it, dim:
  `paused · prefix P resumes`, `paused · prefix P` at 34 columns; its first
  line keeps just the `⏸` (the word once, not twice). Paused agents never
  fold into the idle line, and every count (the space headers, the title,
  the dock) has a violet `⏸N` bucket after working. Idle is the
  absence of all that: no rail, no word, a regular title. The second line
  is `overlay2` (`sub` for an asking or failed agent: there it is the
  question), a live blurb after a blue `›`, the age at its end.
- **Your own card** ("you are here" at a glance): the client's own agent
  card is LIFTED, both rows, on `tile_me` (#474a66, lavender 30% over the
  canvas: 30+ RGB apart from both zebra shades, `tile_hi`, the stash
  selection and the NEEDS YOU tints; every space's band keeps 12+ apart
  from it), with a full lavender `█` cell down its right edge (from 26
  columns), its key-cap solid, its title bold `text`, its dim text `sub`
  (`overlay2` is about 3:1 there). The left state rail is as on any card.
  A window linked into two spaces is current in both (window ids): only
  the copy in the client's own space (`cur_session`) is lifted. The same
  lift marks the client's own shell row and, while the client is on its
  space's home, that space's header.

- **The second line** (`layout::second_line`): a WORKING agent's live
  activity, a few words of what it does right now, after a blue `›`
  (`Running cargo test`, `Editing layout.rs`, a Bash or Agent call's own
  `description`, an in-progress todo's `activeForm`, `Thinking…` before its
  first tool call), changing as the turn goes on, like Claude's subagent
  names; without one, its run detail as before. Every other agent as
  before: an asking or failed agent's message, a finished one's reply
  (`done` detail), an idle one's last outcome. The blurb comes from the
  agent's own transcript (`sidebar::activity`; no hook, no fork):
  `~/.claude/sessions/<pid>.json` maps the tmux window (its `tmux` field)
  to the session id and cwd (a live pid's file only), the transcript is
  `~/.claude/projects/<cwd slug>/<sid>.jsonl` (else found once by a scan,
  then cached). Only for agents whose state is working (an asking agent
  with a workflow or cua flag shows its question), only when the file's
  (len, mtime) changed, at most its last 64 KB (nothing usable there: the
  run detail), walked back from the end through the current turn (to the
  last real prompt; a tool result is none): the in-progress todo first (a
  task a later update closed no longer counts), else the latest tool call
  phrased (a hashed file name by what it is, `a transcript`, `tool output`;
  never a word that looks like a secret), else `Thinking…`. A Codex
  agent's comes from its rollout, whose path the window carries
  (`@agent_rollout`, set by agent-tab-indicator.sh; a field of the
  sidebar's one snapshot): the same 64 KB tail and caching, walked back to
  the turn's `task_started` — the in-progress `update_plan` step, else the
  latest call (`function_call`, `custom_tool_call` — `apply_patch`, or
  `exec`'s code-mode JavaScript, its last `tools.<name>(…)` call that says
  something —
  `local_shell_call`, `web_search_call`): a shell `Running cargo test` (an
  argv's `bash -lc` script unwrapped), a patch `Editing <first file>`, MCP
  `<server>: <tool>`, `Searching the web for …`, `Messaging <agent>`;
  reasoning only `Thinking…`; a turn already over (`task_complete`,
  `turn_aborted`) nothing, so the run detail shows. Inline code (`-c`,
  `-e`, `-m`) is shown as just the interpreter; a script file only by a
  plain, not secret-looking name. `AGENT_UI_CLAUDE_DIR` points the Claude side
  elsewhere (tests; Codex needs no directory). An OpenCode agent has no
  blurb (no transcript reader for it yet): its run detail shows, and it
  never borrows the words of a Claude session naming the same window.

- **Letters**: every agent tile and shell row (both scopes) and every
  NEEDS YOU card starts, after its rail and a pad, with a ` a ` key-cap in
  the window's SPACE's `@space_color` (peach without one), the colour of
  its space header's ` N ` chip, so a window says which space it lives in
  anywhere in the list. Quiet by default: the letter bold in the colour on the
  colour at 22% over its card (`text` for a dark colour). SOLID (dark ink
  on the colour) only where you look next: the client's own agent, an
  asking or failed one, every NEEDS YOU card. A WORKING agent's cap
  BLINKS (`layout::blink_on`): solid in the working blue (dark ink) in even
  wall-clock seconds (the client's own, already solid, inverts instead: a
  blue letter on `crust`, so it shows even in a blue space), its usual look
  (quiet, or the client's solid) in odd
  ones, so "working" reads from the left edge where the list is scanned,
  not only from the word on the right (Mack, 2026-10-21: "second on, second
  off"). Every sidebar blinks in step; it rides the spinner's 8 fps redraw
  (no wakeup of its own), and its rail stays a steady blue `▎` down both
  rows (the blinking cap is the one moving thing; a steady rail keeps the
  edge saying "working" in the off second). Asking, failed, done, idle and
  paused caps never blink; ⌥W armed wins over both phases. The letter is the window's lifetime letter (one
  pool for agents and shells), from the menu's own assignment
  (`menu::letters::of_view`, see [letter keys](#agent-ui-menu-letter-keys)).
  A live sidebar persists the letters it assigns to `@agent_key` exactly
  like the menu (one batched background write, `KeyMemo`), so the ⌥W
  `jump` table, the menu and every sidebar agree; `--once` never writes. While the client's key table is
  `jump` (⌥W armed) every cap is inverted (a bold peach letter on `crust`,
  darker than any surface: no calm cap looks like it, quiet or solid, a
  peach space's included) and the panel's top cap carries a `⌥W jump │
  type a letter` tab. No letter (past 26): a blank cap; no letter
  anywhere: no column. The 1-9 ordinals and slot numbers are gone.
- **Status block** (`status::Bar`, built from the one per-tick snapshot,
  before home windows are hidden; no script runs per frame):
  - a tab on the usage panel's top pad: a red ` prefix ` chip while
    `#{client_key_table}` is `prefix`, a peach ` ⌥W jump ` chip and a
    ` type a letter ` one while it is `jump` (no panel: a row of their own);
  - (no shells: they are rows of their space in the agents section now,
    see **Spaces, homes and shells** above);
  - usage meters: `@codex_<w>_used` / `_expected` / `_delta` / `_resets`
    (`<w>` = `session` / `weekly` / `scoped`, falling back to `_text`) with
    their `_color`; the percent bold, the pace as `+17%` / `−4%` (a real
    minus, no brackets), no `Σ` for a pooled one; `--` while one is unset.
    A meter is Textual's progress bar: `━` over a `surface1` `━` track, `┃`
    (`text`) where the pace expects it; the label, the fill and the delta
    wear the PACE's colour (`layout::pace_ink`): green on or under it, peach
    up to 15 points over, red beyond (no pace: the status line's `_color`),
    so "fine" or "over" reads before the number. `_members` (`slot:used:active[:cap],…`, the
    optional cap = 100 − the account's reserve, 100 when absent) is parsed
    into `status::Member` (not drawn yet);
  - `@resurrect_stale` as ` ! last save 2h ago` / `save refused`, red;
  - prefix v's range selection (below).
  All of it rides on the client row of the same `list-clients` call
  (`tmux::STATUS_FORMATS`) and the window rows (`@space_color`,
  `@stash_sel`). Shorter frames give up, in order: the meters for one usage
  line (under 30), the panel itself (under 16 rows or 20 columns: its
  content then sits on the canvas), usage and the stale save (under 12),
  the selection hint (under 6); a panel with nothing in it is not drawn.
- **Stash selection** (prefix v, stash.sh `sel-start` / `sel-move`): live
  while the client is in the `stash` key table or any window still has
  `@stash_sel`. Every selected agent tile (both lines, mauve 18%) and
  shell row is drawn on a mauve tint with a mauve stripe, and the panel's first row reads ` N selected  h/l extend · ⏎ park · m move · esc cancel` (as
  many keys as fit). stash.sh posts the focus notice after sel-start,
  sel-move, sel-cancel and the park, so it shows at once. Those keys hold
  for as long as a window of the client's session carries `@stash_sel`,
  even after a click, a focus change (into this sidebar and back) or a
  stray key has dropped the client out of the `stash` table: root twins of
  them (scripts/stash-root-keys.conf, bound by sel-start and unbound by
  every ending, so never at rest), guarded fork-free by `@stash_live`, do
  the table's action and otherwise pass the key to the pane unchanged.
  prefix v again cancels.

  There is no event log and no divider any more (`@agent_sidebar_split` is
  no longer read; `--split` is accepted and ignored). The spaces list has no
  git line, so the sidebar never runs `git status`; the branch still shows on
  each rule.
- **The dock** (the status bar is hidden, so this is where every space is
  seen): one `tile_lo` panel anchoring the bottom (OpenCode's prompt box),
  after a gap: its `▄` cap, the spaces, PARKED, its `▀` cap. The spaces: one row per session,
  sessions with `@space_hidden` 1 left out, ordered by the session option
  `@space_index` (space.sh's number: main = 1, then the registry),
  unnumbered ones last by name; the agents section's groups use the same
  order. A row is the status line's module stood on end: ` N ` (⌘N,
  Cmd+N; blank past 9) bold in dark ink on the space's `@space_color`
  (peach without one), its name, its branch dim when there is room, its
  agents' counts on the right (the AGENTS headers' glyphs). The client's
  space is on its band, a `▌` in its colour at the panel's edge, its name
  bold, its chip red while the prefix is armed. As rows run short: the
  client's space and those with an attention agent and `+K more spaces`
  (click: the menu's All tab), then chips on as few as one line (` N ` then
  ` name ` on `surface0`; the client's name bold on its colour at 30% over
  the dock; an attention agent's glyph inside it), each line of chips
  going only after the caps. A click switches the client to that space's
  HOME (`agent-jump.sh goto` on its `@space_home` window, else its active
  one, in that session), as ⌘N does. The index,
  hidden mark and colour are SESSION options read through the window-row
  format, so they cost no extra tmux call.
- **Stash** (PARKED, the dock's last line): always ONE line, at every
  height: `PARKED` and the count as a square mauve chip (`none` dim with
  nothing parked), a message or the watcher's problem on the right.
  The parked tabs themselves are never listed (prefix h's picker and the
  menu's Parked tab show them). A click on the line runs exactly prefix h's
  command, `tmux run-shell "stash.sh unstash '' <client>"` (the sidebar's
  client for `#{client_name}`; a unit test compares it with tmux.conf.tmpl's
  `bind-key h`), so the same picker popup opens on that client. A plain
  **right-click** (SGR button 2; wezterm.lua binds no right-click, so it
  reaches the pane) on an agent row or NEEDS YOU card parks that window:
  `run-shell -b "stash.sh stash <win>"`, prefix H's command for one window,
  which records `@stash_origin` so unparking puts it back in its space. The
  footer says `parking <name>…`, then `parked <name>` once a snapshot shows
  the window in `stash` (stash.sh's refusals, a working agent or a session's
  last tab, show on the status line as before). The move fires the
  `window-unlinked` hook's agent-state notice, so every sidebar redraws at
  once. A right press anywhere else, or with a modifier, only hands focus
  back.
- **Pinned agents** (the Codex app's pins; `agent-ui/src/pins.rs`,
  `layout::pin_block`):
  - **What**: the window option `@agent_pinned` = the epoch second it was
    pinned. A pinned AGENT (a state; never a shell or a home) MOVES out of
    its space's group into the PINNED block between NEEDS YOU and AGENTS
    (not duplicated; NEEDS YOU still lists it while it asks, as for every
    agent). The block is global in BOTH scopes: in "this space" a pin from
    another space still shows (that is what pinning is for). A window
    linked into two spaces is pinned once (its copy in the client's space).
  - **Order**: pin order, first pinned first (`@agent_pinned`, then the
    window id): pinning another appends it and never moves the ones
    already there, the sidebar's "nothing moves under the mouse" rule. A
    toggle stores `max(now, the latest pin + 1)`, so a pin made in the
    same second as another still sorts after it.
  - **Look**: ` PINNED` like the other section labels, `⌥W 1-N` (`⌥W` bold
    blue, the AGENTS header's `⌥V` style, the digits dim; just the count
    when narrow) and the pin glyph and count dim on the right. Each card is
    exactly its space-group card (letter cap in the space's colour, state
    rail and word, the client's lift, clicks: go / right-click park /
    middle-click unpin), zebra afresh; its second line ends `<space> ·
    <age>` (dim; dropped when the line's text would keep under 10 cells)
    and its digit sits as a neutral cap UNDER its letter cap (beside it on a
    one-line card), so no title room is lost. While ⌥W is armed the digit
    caps invert with every letter cap.
  - **Ladder**: pins take the frame's one card height (two lines, then one)
    but never fold into an idle line; the spaces collapse before a pin loses
    its card; only a frame too short for every pin on one line cuts them
    (the first pins, then `… N agents`). With every agent of the scope
    pinned, AGENTS says `all pinned above`.
  - **Toggle**: prefix t on the current tab (spaces.conf: `agent-ui pin
    toggle --window <win> --client <tty>`; it replaces tmux's clock-mode,
    which comes back with spaces off; not prefix p, next to prefix P's
    pause-everything), the menu's Space p (pin / unpin, no y/n), or a plain
    MIDDLE click (SGR button 1) on a sidebar card. Each sets or clears the
    option, renumbers the pins' digits, updates `pins.json`, posts the
    agent-state notice (every sidebar redraws at once) and says `pinned
    <title> · ⌥W 2` (notify.sh / the footer). Refused, aloud: a shell
    (`only agents pin`), a home, a parked tab (`a parked tab is never
    pinned · unpark it first`), an agent in a hidden session (`agents is
    not a space`), a gone tab. `agent-ui pin [toggle|on|off] [--window @id]
    [--client tty]`: exit 0 done, 1 refused, 2 usage; `agent-ui pin
    renumber` only renumbers.
  - **⌥W then 1-9** (`@pin_key`): the first nine pins in pin order hold
    1..9, no gap (`pins::pin_keys`); past nine, no key. Every toggle
    renumbers at once (`pins::renumber`), and the live sidebar repairs any
    gap on each refresh (a pin closed, parked, unpinned or exited
    elsewhere), like the letters: ONE batched `set-option` from a thread,
    only what differs, never re-sent while in flight (`pins::KeyMemo`,
    resent once after 5 s unseen). spaces.conf's `jump` table (⌥W, the one
    nav leader) binds 1-9 beside the letters: the window whose `@pin_key`
    is N and that is still a pinned agent (`@agent_pinned` and a state, so
    a stale key on an exited agent never lands), in any space but a hidden
    one, fork-free; a miss runs `agent-ui pin renumber` (no sidebar open to
    repair the gap) and says `no pin N` (docs/spaces.md).
  - **Parking** a pinned agent keeps its pin (a window option travels with
    the window): it leaves the list while parked (no digit then; the stash
    is hidden, so ⌥W never lands there) and comes back pinned, with a key,
    when unparked.
  - **Kept across a restore**: `~/.local/state/agent-ui/pins.json`
    (`$AGENT_UI_PINS`), one entry per pin: the agent's SESSION ID, the pin
    time, the server it was last seen in (`#{start_time}`), its window
    there, when last seen. A window's session id: claude's
    `~/.claude/sessions/<pid>.json` (its `"tmux"` field names
    `<session>:<@window>.<%pane>`; the pid alive, the pane still in that
    window, the newest `startedAt`), plus the ids its argv was started to
    RESUME (`--resume <id>` / `-r <id>` / `--resume=<id>`, read with
    `sysctl(KERN_PROCARGS2)`, no fork): every restore path (assistant-
    restore.sh, closed-tabs.sh, stash.sh's unpark) runs `claude --resume
    <sid>` and a resumed claude may report a NEW id (after a compaction), so
    an entry under the old id still finds its window and is RE-KEYED to the
    new one (`--continue` names no id and is not matched); codex's
    `@agent_session_id`; opencode's `-s <id>` / `--session <id>` from the
    argv of the opencode process under one of its panes (libproc's child
    list from `#{pane_pid}`), so one started without an id has none (its
    pin lives as long as its window). The live sidebar syncs
    (`pins::Sync`: only while anything is pinned or stored, when the windows
    changed or every 10 s, no fork): a pinned window's entry is kept
    current; an entry whose session now runs in an unpinned window is put
    back (`@agent_pinned` = its old time) when that is a NEW server (a
    resurrect restore) or a new window (closed-tabs.sh's ⌘Z reopen), and
    dropped when it is the same window in the same server (unpinned); an
    entry whose session runs nowhere is kept a week (parked, closed, not
    restored yet), dropped at once if its window is still here unpinned.
    Writes are whole-file renames; a lost race is repaired by the next
    sync, which never re-pins a window it saw unpinned in the same server.
    Only the DEFAULT server's processes touch the file: the server's
    socket path, resolved (`/tmp` → `/private/tmp`), must be
    `realpath(${TMUX_TMPDIR:-/tmp})/tmux-<uid>/default`, whether it comes
    from `--socket`, `$AGENT_UI_TMUX_SOCKET` or an ambient `$TMUX`
    (`pins::is_default_server`); any other needs an explicit
    `$AGENT_UI_PINS`, so tests, previews and a private server running the
    real config never rewrite it.
- **Density ladder** (`layout::STEPS`): agents lose detail last. Before
  anything else the dock's gap and one line of chips are held back, with
  the PARKED line; the first two steps (with the gaps, and the one right
  after) also leave the dock its rows (every space a row and its caps). A
  frame then takes the first step at which every agent still gets two
  lines: (1) the dock takes the leftover rows, the richest form that fits
  (every space a row, else the client's and the attention ones, else
  chips; its caps go before a line of chips); (2) the blank rows go, the
  gaps between spaces in AGENTS before the section gaps; (3) the dock's
  reserve goes; (4) NEEDS YOU cards go to 1 line. There are no pads at any
  step: list tiles always stack edge to edge, every card one colour, consecutive
  cards alternating `tile` / `tile_lo`.
  Only then do agents compress (idle folded into a `○○○ 3 idle · …` tile
  and a space's 2+ shells into one line, every agent one line, folded
  again, spaces collapsed to their header, their shells gone with it,
  then `… N more`), with NEEDS YOU held to at least 2 rows and a third of
  the body (`… N more need you`). Never two heights of agent row in one
  frame. Nothing is dropped silently: every agents-section row records the
  agents it stands for (`Row::covers`), and tests prove each is shown,
  folded, collapsed or counted at every size 2-110 rows.
- **Colour**: the canvas is the sidebar's own `mantle` (`palette::CANVAS`;
  every cell is painted, `--once` writes a `48;2;24;24;37` code on every
  untinted cell), and every tint is mixed over it. The surfaces are tiles
  and buttons in solid fills (no box-drawing lines anywhere): `tile` /
  `tile_lo` / `tile_hi` / `tile_me`, the panels, NEEDS YOU cards in their state colour
  mixed over the canvas (`Ink::Mix`, one tint per card, consecutive cards
  14% / 9%), the client's space's band (`layout::band`), the stash
  selection's mauve, the key-caps and the status-line modules; tests
  re-check every one against the canvas and its neighbours. Rows left
  over after all that buy spacing back: the gaps above AGENTS and
  NEEDS YOU, then the gaps between spaces in AGENTS (never a gap between
  cards; never PARKED: it stays one line). Dim text is `overlay2` (about
  6:1 on the canvas, 4:1 or better on any surface; `overlay` there is
  under 3:1).
- **Sync (told, not polling)**: the loop sleeps in `poll(2)` on stdin, the
  signal pipe, one Darwin-notification fd (`notify_register_file_descriptor`,
  libc FFI, no crate) and the client's exit watch. Two notice names:
  `com.mackhaymond.tmux.agent-state` (already posted by the indicator, the
  watcher and the window-linked/unlinked hooks on every state change) and
  `com.mackhaymond.tmux.focus` (posted by tmux hooks on session / window
  switches, session rename / close, and by the `M-v` scope binding; the
  sidebar posts it itself after its own scope toggle so other sidebars
  follow). `$AGENT_STATE_NOTICE` / `$AGENT_FOCUS_NOTICE` rename them (tests).
  A notice schedules one snapshot 12 ms later (a burst is one refresh). The
  timer is only a safety net: 5 s, or 0.5 s while a drawn agent pulses with
  `@agent_blink` (in flight, workflow or cua) or when notify(3) is missing.
  Measured on a private `-L` server (2026-10-15): window switch → sidebar
  shows `here` on the new row in ~40 ms, a state write + notice → card in
  ~37 ms (was 300-500 ms with the 0.5 s tick). Idle CPU over 60 s with
  nothing pulsing: 0.05 s sidebar (+children) and 0.02 s tmux server, down
  from 0.56 s and 0.12 s; with an agent pulsing, unchanged (~0.54 s + 0.13 s).
- **Never alone**: once the sidebar has seen its client (`#{client_pid}` in
  the snapshot's client rows), it exits (status 0, so the pane closes) the
  moment that process exits: a kqueue `EVFILT_PROC`/`NOTE_EXIT` fd in the
  same poll, free until it fires (detach, `tmux` exit, or the tmux pane
  closed). A client missing from the snapshot proves nothing by itself
  (tmux 3.7 leaves suspended, prefix C-z, and locked clients out of
  `list-clients`): it counts as gone only if its pid is dead. While no client was ever resolved, a `wezterm cli list` (at most one
  per 5 s, as before) that finds the CMD+B pane gone or nothing else in the
  tab ends it too. A pinned fake `--client` never exits this way.
- **Status line** (`status::Registry`): `@sidebar_pids` lists the open
  sidebars' pids and `@sidebar_open` is its length (UB's bindings read the
  count). A live sidebar registers on start and unregisters on every exit
  path (a guard dropped on a normal return, the never-alone exit, SIGHUP /
  SIGTERM, an error, or a panic unwinding). Each change runs under a
  `flock` (one lock file per tmux socket, in `$TMPDIR`), drops every listed
  pid that is no longer a live `agent-ui` process (`kill 0` +
  `proc_pidpath`, so a reused pid does not count), and writes the list, the
  count and `status off` (count > 0) or `status on` (count 0) in ONE tmux
  command. Crash repair (a SIGKILLed sidebar leaves its pid behind): the
  next sidebar to start drops it; a live sidebar's refresh repairs at once
  (at most every 2 s) when its own pid is missing, a dead one is listed,
  the count disagrees, or the status line is on again (a config reload);
  and `agent-ui sidebar --repair` (one read, one write; prints the count)
  does it with no sidebar open, turning the line back on, and leaves
  `status` alone when there was nothing to repair. Hook it wherever a
  cheap check fits (e.g. `client-attached`). `@sidebar_clients` rides in
  the same writes, word for word beside `@sidebar_pids`: each open
  sidebar's client (`-` until one is resolved); a sidebar whose client
  resolves or changes re-registers on its next refresh (the same repair
  check).
- **Popups centre on the WezTerm row** (`@sidebar_width`, tmux.conf.tmpl
  "POPUP POSITION"): with a sidebar beside it the tmux client is narrower
  than the window, so tmux's `-x C` put every popup and menu off the
  middle. The leader (lowest live pid, as for the status jobs) keeps
  `@sidebar_width` = its columns + 1 (WezTerm's split divider), negated
  when it sits LEFT of its client (`STRIP_SIDE = "Left"`: its `left_col` in
  the `wezterm cli list` that resolved the client is the smaller), written
  only when tmux's value differs (first frame, a resize, a dropped value);
  the registry write that empties the list, or a `--repair` that finds
  none, unsets it. `@popup_x` = `(client_width - 1 + extra) / 2 -
  popup_width / 2`, extra = `@sidebar_width` when `#{client_tty}` is a word
  of `@sidebar_clients`, else 0 (then it is tmux's own `popup_centre_x`);
  tmux clamps a popup too wide to fit shifted to the client's right edge.
  Every centred display-popup / display-menu passes `-x '#{E:@popup_x}'`
  (`##{…}` inside `run-shell -C`, which expands formats before the popup
  exists); scripts, agent-ui (`status::POPUP_X`) and agent-roster.py pass
  `#{?#{@popup_x},#{E:@popup_x},#{popup_centre_x}}`, so a server that has
  not reloaded tmux.conf still centres (an unset `@popup_x` would be column
  0). One width for all sidebars: two of different widths share the
  leader's. tests/test_popup_x.py reads the drawn column off a nested client.
- **Toasts** (`scripts/notify.sh`, `status::Toast`, layout.rs "Toasts"):
  with the status line off, `tmux display-message` draws on the client's
  bottom line, which reads as the status line coming back. Scripts say
  things through `notify.sh [-c CLIENT] [-k info|ok|warn|error] [-t SECONDS]
  [-T TITLE] [--] TEXT...` instead. One tmux read (`display -p` of the
  client's name, `@sidebar_pids`, `@sidebar_clients`, `@toast_seq`, the
  current `@toast` and the text expanded as display-message would) decides:
  a LIVE sidebar (its paired pid passes `kill -0`, so a SIGKILLed one still
  listed never swallows a message) open for that client (or, with no client
  resolved, any live sidebar) → a TOAST: one tmux write of `@toast_seq`
  (+1) and `@toast` = up to 4 records separated by RS (U+001E), oldest
  first, each `seq⇥epoch_ms⇥kind⇥ttl⇥client⇥text⇥title` (client empty =
  every sidebar, title empty = the kind's; expired records dropped, the
  newest 3 kept beside the new one; a trailing `;` escaped so tmux keeps
  it), then the focus notice (`notifyutil`, in the background). 6-field
  records (from before `-T`) still parse. Otherwise exactly the old
  `display-message [-c CLIENT] -- TEXT`. Every message is logged to
  `~/.local/state/tmux-notify/log` (`epoch⇥kind⇥client⇥text`, `⇥title`
  appended when `-T` gave one; trimmed to 500 lines past 1000). The
  sidebar reads `@toast` in its one snapshot (a `STATUS_FORMATS` field),
  keeps the records for its client or every sidebar, and draws the live
  ones as OpenCode-style cards OVER the top of the frame (no row moves; the
  rows beneath keep what they stand for), oldest on top, at most 3, a
  blank canvas row between two and under the stack:

  ```
   ┃ ✓ Done                         10:42 ┃    info ● Info blue · ok ✓ Done green
   ┃ parked Canvas Syllabus               ┃    warn ▲ Warning peach
                                               error ✕ Error red
  ```

  Square `┃` bars in the kind's colour down both sides of a `surface0`
  fill, no corners, a canvas margin each side; the glyph and title (`-T`'s,
  else the kind's) bold in the colour on the first row, the local time it
  was posted (`HH:MM`, libc `localtime_r`) right-aligned, dropped when
  narrow; the text word-wrapped under it into at most 3 lines, `…` on the
  last when more is left. Short frames drop the oldest card first, then
  body lines; a card never covers the last row, nor the key-table marks
  and the selection hint (the cards start under them). Under 16 columns or
  3 free rows the newest toast is one compact row instead (its bars, glyph
  and text), down to 2 free rows. The blank rows dismiss nothing. Each shows
  until `epoch + ttl` (default 4 s, error 8 s): the loop wakes at the
  earliest deadline on screen to draw that one away (no polling); a left
  click anywhere on a card dismisses THAT toast in that sidebar only, by
  its (seq, epoch_ms) id (the seq is read then written, so two racing
  posts can share one; never their posting time too). The dismissed ids
  are bounded (8) and forgotten once `@toast` no longer holds them.
- **Pause banner** (`sidebar/pause.rs`, layout.rs `pause_rows`; the
  contract is [agent-pause.md](agent-pause.md) "In the sidebar"): where
  prefix P's pause stands, so closing its popup or the tab loses nothing.
  `pause::phase` is pure (the parsed `progress.json` / `paused.json`, a
  clock of now / last wake / last boot, the snapshot's working and existing
  window ids and the server's `#{start_time}` → resuming, sending, ready,
  stopping, safe, or nothing), first match wins.
  `pause::Reader` stats both files in `$AGENT_PAUSE_STATE_DIR` (else
  `~/.local/state/agent-pause`) on every refresh and re-parses one only
  when its inode, size or mtime changed; `sysctl kern.waketime` only while
  `paused.json` lists targets, `kern.boottime` once. A press is in flight
  only while not done, its `<ms>-<pid>` process alive, begun after the
  boot and under 20 min ago (8 deliveries at a time, 60 s each: 160
  agents fit), so a killed toggle whose pid is reused never holds the
  banner; its record-only rows (a pause's `unidentified`, a resume's
  `gone`) are not counted. A wake OR a boot after `paused_at` is ready. A
  tmux server started after `paused_at` numbers windows from @0 again, so
  then every saved `window_id` counts as gone. WORKING is the script's rule on the RAW snapshot
  (`@agent_state` running, `@agent_workflow` or `@agent_cua` set, before
  homes are blanked); a gone window counts as stopped. While a press is in
  flight the files are re-stat'ed on the spinner's ticks (the script posts
  no notice per delivery). Malformed or torn files show nothing. The
  banner is the very top: one tile tinted in its phase's colour, a heavy
  `▌` rail, glyph and word bold in that colour, the meaning after it; a
  second row (dim) with who is still working or the labels. It matches
  CuaNotch (`layout::pause_hue`, `pause_bg`), never green (green means
  done), never peach: the MOVING phases (pausing, resuming: the spinner)
  the quieter `dimviolet` (`#6c5bd9`, violet at 85% brightness, same hue)
  on a 9% violet tint, as the notch pulses violet while pausing; the
  SETTLED ones (safe, ready: every paused agent stopped) electric `cyan`
  (`#00d9ff`) on a 14% cyan tint, as the notch glows cyan until the
  resume (white before; Mack: "solid white is no good"). Paused CARDS stay
  violet. The settled two part by emphasis: safe's `⏸ PAUSED` bold in
  cyan, ready's count on a SOLID cyan chip (dark ink), the one that asks
  for a press:

  ```
   ▌ ⠹ PAUSING  2 of 5 stopped          dim violet, 9% (also "sending 2 of 5")
   ▌   still working  Tmux Agent Sidebar, I…
   ▌ ⏸ PAUSED  safe to close            cyan, 14%, bold word
   ▌ [ ↻ 3 ready ] Tmux Agent Sidebar…  cyan, 14%, solid chip
   ▌   prefix P or click to resume
   ▌ ⠹ RESUMING  3 agents…              dim violet, 9%
  ```

  Two rows from 16 rows tall, one from 4: the panel under it and the
  sections take the squeeze, and toasts float under it, never over. Safe
  and ready rows click to `agent-pause.py resume <client>` through
  `run-shell -b` (prefix P's own shape; never `toggle`); the others do
  nothing.

  A target whose PAUSE tmux-mcp HELD (`"result": "held"` on a
  `progress.json` or `paused.json` target) and whose `window_id` still
  works is `pause::Held`: pausing (sending or stopping) lists it apart,
  never in `still working` (that row goes when only held agents are
  left), with its session and letter from the RAW snapshot (`@agent_key`,
  none in a hidden session). The PAUSE goes out urgent (through a draft),
  so tmux-mcp holds it only while it cannot read the prompt box; the
  reason is the `<why>` of the target's `detail` (`held for @h (…):
  <why>. It will be delivered…`, `pause::held_why`), and `pause::ask` maps
  tmux-mcp's wording to what to do: copy mode → `leave copy mode`, no
  prompt box on screen → `show its prompt`, the box without the cursor →
  `close its dialog`, a permission or question prompt → `answer its
  prompt`, a draft → `clear its prompt box`; any other reason is shown as
  it is (dim `sub`), none at all → `look at its tab`. `paused.json` keeps
  `held` until the resume, so `pause::Reader` remembers every held window
  it has seen existing and NOT working (per `paused_at`, per press):
  delivered, it is never held again in that pause (an agent woken while
  still paused is just `still working`); a window missing from the
  snapshot proves nothing. Each held agent gets a row of its own under the
  banner, on the banner's violet tint, its rail and `✎` in `yellow` (the
  "needs you" colour: the one thing to do there; never peach, ⌥W's armed
  caps). `waiting:` goes first when short of room, then a long title gives
  way to the agent's letter cap, then the title is cut. Up to 3 rows from
  16 rows tall (past 3: two and `+N more  look at their tabs`), one from
  10 (`N agents waiting` when several), none below. A click jumps to that
  agent's tab (`Target::Held`: the card's jump, but never a park or pin
  from the banner; `+N more` to the first not listed). Once its window
  stops working the row goes and the banner settles like any other.
  `Target::Held` and `Target::Resume` are guarded like the answer chips
  (`sidebar.rs` `guarded`, 400 ms `SETTLE`): when the held agent stops,
  the settled banner's resume row lands where the held row was, and a
  click aimed at the held row must not resume everyone.

  ```
   ▌ ⠹ PAUSING  1 of 2 stopped
   ▌ ✎ P0a  waiting: leave copy mode      44 cols
   ▌ ✎ P0a  leave copy mode               34 cols
   ▌ ✎ [k] clear its prompt box           34 cols, a long title
  ```

  **Void and glow off** ([agent-pause.md](agent-pause.md) "Dismiss and
  void"). The settled banner (safe, ready) carries a square `✕ void` chip
  at its first row's right end (`layout::void_row`), dim on the cyan a
  step up; ` ✕ ` when short of room, as at 34 columns (ready's labels keep
  10 cells before it shrinks). Voiding is confirmed: a first click only
  arms it (`Target::Void`), and the chip turns to ` ✕ void? click again `
  bold dark ink on solid red (` ✕ void? ` at 34; safe's `safe to close`
  makes way for it). A second click within 3 s (`sidebar::VOID_ARM`) on
  the armed chip (`Target::VoidArmed`) runs `agent-pause.py void <client>`
  through `run-shell -b`, exactly as the resume runs. The armed state
  reverts after 3 s (the loop wakes then to redraw it) and whenever the
  phase or the pause changes (safe → ready, resumed, voided, a new
  `paused_at`: compared on every re-read); a click on an armed chip that
  has just reverted arms it afresh, never voids. The armed chip ignores
  clicks for 700 ms after arming (`sidebar::VOID_FLOOR`; a re-arm starts
  it over), so a fast double click never confirms. Both chips are guarded like the resume (`guarded`, 400 ms
  `SETTLE`): a click aimed at a row a relayout moved never arms or
  confirms, and the armed chip is new under the pointer, so a double click
  never voids. A plain click anywhere else on the banner still resumes
  (and disarms). A moving banner has no chip. Right-click does nothing on
  the banner. `--once --void-armed` draws the armed state (previews).

  The banner ignores `glow_dismissed_at` (`agent-pause.py dismiss`, the
  notch's ring off): it shows whenever `paused.json` lists targets,
  dismissed or not, until resume or void. Dismissed, a settled banner says
  `glow off`, dim, at its second row's right end (`pause::Paused::glow_off`,
  `Reader::glow_off`); ready's hint shortens to `prefix P · click` for it
  at 34 columns.

  ```
   ▌ ⏸ PAUSED  safe to close   [✕ void]   44 cols ([✕] at 34)
   ▌   Island resize spring, C… glow off
   ▌ ⏸ PAUSED       [✕ void? click again]  armed, solid red ([✕ void?] at 34)
  ```
- **Status jobs** (`status::Keeper`): with the line hidden tmux runs none of
  status-right's `#()` jobs, and two of them must keep running:
  continuum's autosave (`continuum_save.sh`) and the usage tick
  (`codexbar-usage-status.sh --tick`). So the open sidebar with the lowest
  live pid reads `status-interval` and status-right (every 60 s) and runs
  each distinct `#()` command through `run-shell -b` once per interval (one
  tmux call), formats expanded as the line would. Needs a client row in the
  snapshot (no attached client: the line would not run them either).
- **Home windows**: a window with `@space_home` 1 (a space's landing
  screen) is never an agent row and never counted, in any scope, with
  `@spaces` on or off (its agent options are ignored; its space still shows
  as the client's). It has no row and no letter: the space's header stands
  for it (a click goes there; lifted while the client is on it).
- **Tests**: `tests/sidebar.rs` drives the live loop with test-only notice
  names (state and focus notices reflected in well under the 5 s net, scope
  set from outside, a window switch, the prefix / jump / stash key tables, a
  stray `@stash_sel`), checks the letters land in `@agent_key`, the
  registry (count, status line, crash repair by the next start, by a
  refresh and by `--repair`), the status job kept running, and that
  detaching a real nested client ends the sidebar within 1.5 s with the
  status line back on. `notify_toasts_in_the_sidebar_and_falls_back_without_one`
  runs notify.sh against the test server (`NOTIFY_TMUX`): the card on
  screen in under 1 s, gone at its ttl, dismissed by a click, two stacked
  (a `-T` title on the older), and `display-message` for a client without
  a sidebar and after the sidebar closes; `tests/test_notify.py` covers the
  script's CLI, sanitising, decision, the record stack (at most 4, expired
  dropped, 6-field records kept, the title field) and log.

### Agent insight (`src/insight.rs`)

Per agent, from its OWN transcript (several agents often share one checkout
with no worktrees, so `git diff` of the folder is everyone's work): the
files it edited that are still uncommitted (`+48 −12 · 3f`), what it is
doing, and how full its context is (`Agent::insight` in the view). On the
cards (`layout.rs`, `says` / `extras`):

```text
 ▎  d  Tmux Agent Sidebar      ⠹ working
 ▎     › Wiring the badge +48 −12 · 3f 85% 1m   working: its todo
    g  Lost suit jacket
       > find the receipt         +5 62% 7m    idle: your last prompt
```

- **Row 2 says**, first that applies: an asking / failed agent's reason
  (unchanged); a working agent's in-progress todo, else its live activity
  (both after the blue `›`), else its run detail (`Edit layout.rs`), else
  your last prompt; a done agent's message; an idle one's old outcome,
  else your last prompt. A prompt reads after a faint `> ` (how Claude Code
  marks what you typed), never the blue `›` of what it does now.
  `no reply yet` / `no activity yet` only when there is nothing at all.
- **Diff badge**, before the age: `+48` dim green, `−12` dim red (a zero
  side left out), ` · 3f` dim; none while clean. A click on it runs
  `agent-ui diff <win> --client <tty>` (nothing when that fails; `no tmux
  client` without one; ignored, like the answer chips, until the card has
  sat 400 ms), with `AGENT_UI_DIFF_FILES` naming its shared file (below)
  when there is one; a right or middle press there still parks or pins the
  card. NEEDS YOU cards carry it too (line 2, never beside the answer
  chips); a one-line card puts it before the state word.
- **One publisher per server** (`insight::Board`, `src/insight/board.rs`).
  Every WezTerm tab runs a sidebar; only the one holding `<dir>/<key>.lock`
  (`flock(LOCK_EX|LOCK_NB)`, held for its life, let go by the kernel
  however it exits) runs the insight engine and writes the shared files.
  The others run NO engine: they mirror `<key>.json` whenever `<key>.gen`
  changes, woken at once by the publisher's Darwin notice
  (`com.mackhaymond.agent-ui.insight`, `$AGENT_INSIGHT_NOTICE`; on a fd of
  its own, read with no snapshot), so every sidebar's badges agree; and
  they try the lock again on every refresh, so one takes over when the
  holder is gone. Taking the lock first removes what the key had published
  (`.gen` first: a crashed holder's files are never trusted) and other
  servers' leftovers whose lock nobody holds (`.gen`, `.json`, `.files`;
  their `.lock` once untouched a day; temp files after a minute). A worker
  that gives up (3 engine panics, or no thread) removes `.gen`, then lets
  the lock go, for good; a holder exiting cleanly does the same; a publish
  that cannot write (full or read-only disk) ends publishing (its own
  engine kept). A FAILED snapshot (tmux did not answer) changes nothing:
  the key stands and the engine never sees it (it would forget every
  window's history). No key known yet, or a folder it cannot use: its own
  engine, nothing published.
- **Shared files**, under `<dir>` = `$AGENT_UI_INSIGHT_DIR`, else
  `$XDG_STATE_HOME/agent-ui/insight`, else `~/.local/state/agent-ui/insight`
  (`insight::files_dir`). `<key>` = `<#{start_time}>_<8 hex: FNV-1a of
  #{socket_path}>` (`board::server_key`: a restart renumbers windows, so a
  new key; two servers started in the same second still differ):
  - `<key>-<window id without @>.files` (`files_path(key, win)`): that
    agent's dirty files, `insight-files`' format exactly (each repo's work
    tree on its own line, then its files repo-relative after a tab).
    Written only when the set changed; removed once the window has no
    dirty file or is gone.
  - `<key>.json`: every window's insight (`board::encode`: `generation`,
    then `windows` → `{kind, transcript, doing, todo, prompt, ctx_used,
    ctx_window, diff: {added, removed, files} | null, dirty_files,
    touched}`).
  - `<key>.gen`: `<pid> <generation> <complete>`, written LAST after each
    result (`complete` 1 once a pass with every repo answered whole landed
    since this holder took the lock); then the lock file is touched and
    the notice posted.
  - `<key>.lock`: the election.
  All written atomically (temp file + rename), 0600, folder 0700. A reader
  (`agent-ui diff`) trusts the `.files` only while `board::verified`: `.gen`
  there saying complete, and the lock held (a try fails); else its own
  one-off engine, ` · unverified`. `AGENT_UI_DIFF_FILES` (the click passes
  it) comes first.
- **Context meter**, from 50% full: `72%`, dim, amber (`peach`) from 80%,
  red from 90%. `CTX_STYLE` picks it; `$AGENT_UI_CTX_STYLE=glyph` draws one
  cell by eighths instead (`▄` half, `▇` 85%).
- **Narrow**: the file count goes first, then a dim meter, then the badge,
  then a PINNED card's space tag, then the meter, before the doing text
  has under 12 cells past its mark (`DOING_MIN`, or its whole). At 34
  columns a working card keeps `+48 −12` beside a 12-cell doing line; an
  amber or red meter outranks the badge there.
- **Freshness**: the worker's result lands after the refresh that asked
  for it; while it holds a snapshot (at most 10 s) the publisher's loop
  looks every 0.5 s (`TICK`) and puts a new generation into the view (and
  the shared files) without a snapshot; a mirror reads it when the
  insight notice wakes it. `sidebar --once` reads the insight once, synchronously.
  `AGENT_UI_NO_INSIGHT=1` reads none at all, live or `--once` (the
  integration tests set it).

- **Transcript.** Claude: the window's session id as the pins resolve it
  (`pins::sessions`), then `~/.claude/projects/<slug>/<sid>.jsonl` (else a
  scan of the project folders, cached), plus `<sid>/subagents/*.jsonl` for
  its subagents' edits. Codex: `@agent_rollout`, else
  `~/.codex/sessions/*/*/*/rollout-*-<@agent_session_id>.jsonl`. OpenCode:
  none.
- **Reads.** Incremental per file by (inode, len, mtime): only appended
  whole lines (a half-written one is read again whole); shrunk, replaced or
  rewritten → parsed again. At most the last 32 MB on first sight (an edit
  further back in a 100 MB transcript is not attributed). Lines are
  JSON-parsed only when a substring test says they matter.
- **Extracts.** Touched files: Claude Edit / Write / MultiEdit /
  NotebookEdit (an errored call does not count), Codex `FileChange` items
  (else `apply_patch` paths). Doing: the latest todo list's in-progress item
  (TodoWrite, TaskUpdate, Codex `update_plan`), else the last real prompt
  (no tool results, meta, notifications, peer messages; `<pasted_content>`
  tags off; first line). Context: Claude's latest usage (input + cache read
  + cache creation) of 1M for a `[1m]` id / Fable / anything seen past 200k,
  else 200k; Codex `token_count` (`last_token_usage.input_tokens` of
  `model_context_window`).
- **Diff.** Touched files by repo root; per repo one `git diff --numstat -z
  --no-renames HEAD -- <files>` plus `git ls-files --others
  --exclude-standard` (untracked = all lines added), `--no-optional-locks`,
  3 s timeout; asked again only when the files, index, HEAD or HEAD reflog
  changed, and at most every 5 s (a half answer, one of the two failed, is
  asked again). Committed files drop out by themselves, and once a current
  answer shows a file clean after an agent's last settled edit of it, a
  later change there is not that agent's until it edits the file again
  (A's committed edit never lends A the diff B makes afterwards).
- **Doing and context, refined.** A real prompt newer than the todo list
  hides its stale in-progress item; `[message from @…]` (another agent) is
  no prompt; a model switch or a compaction forgets the usage peak.
- **Threading.** The sidebar's `Insights` hands the latest snapshot to its
  own worker thread at most every 2 s, only while that is idle; a frame
  reads the last results and never waits (`generation()` moves when new
  ones land). A panic in the engine is caught (never stuck busy), the
  engine is made afresh, and three in a row turn insights off. Transcripts
  and untracked files are opened non-blocking and only when regular (a
  FIFO never hangs the worker).
- **CLI.** `agent-ui insight [@id] [--json]` prints them;
  `agent-ui insight-files @id` prints that agent's dirty files: each repo's
  work tree on its own line, then its files repo-relative, one per line
  after a tab.
- **Diff popup** (`src/diff.rs`). `agent-ui diff @id --client tty` (the
  client is required: tmux would guess one) shows those files' uncommitted
  diff, nobody else's. The files: `AGENT_UI_DIFF_FILES` (the sidebar's
  click passes its own published file), else, while `@sidebar_open` says a
  sidebar runs, its published file for the window (`insight::files_path`,
  keyed by the server's `#{start_time}`; none = nothing dirty, as the badge
  says), else a one-off engine over the window, whose title ends `·
  unverified` (no attribution history, last 32 MB only). Per repo `git diff
  --no-renames HEAD -- <files>` (the empty tree before a first commit),
  then each untracked one as an addition (`git diff --no-index /dev/null
  <file>`; at most 200, none over 1 MB: a `# untracked, not shown: …` line
  instead); pathspecs in batches of at most 128 KB, never one argv near
  ARG_MAX; several repos each start with a `# repo <root>` line (a magenta
  rule in the less fallback). A git that fails says `diff: git failed in
  <root>: <its first error line>`. The plain diff goes to a 0600 temp file
  and a `display-popup -w 90% -h 85% -x <@popup_x>` titled ` d · <title> ·
  +A −R · Nf ` pages it: `delta` (git's `core.pager` here, so its `[delta]`
  config) through `less -R -c --mouse`, else `less -R` over the diff in
  git's colours (`AGENT_UI_DIFF_PAGER=less` forces that). No `-E`: tmux
  closes the popup on Esc / C-c itself (less cannot tell a bare Esc from an
  arrow key's), and after `q` the script runs `display-popup -C -c <tty>`.
  The popup opens the file, unlinks it, then pages (its command and `-d`
  are not formats: no `#` doubling there). Nothing dirty: no popup,
  notify.sh says `d: no uncommitted changes` (the letter, else the title);
  not an agent / no transcript / gone: said the same way. Exit 0 shown, 1
  refused, 2 usage. It returns when the popup closes (tmux's display-popup
  client waits), so callers run it detached or from `run-shell -b`. tmux
  silently opens no popup over one still up: the temp file still there
  afterwards says so (`diff: popup could not open`, exit 1). The menu's
  Space d passes `--after <its pid>` and closes; the diff waits for that
  pid first and retries once 300 ms later. Keys: ⌥W then ⇧letter
  (spaces.conf, docs/spaces.md), the menu's Space d, a card's diff badge.
  `--print` prints the popup's tmux argv, one per line, instead.

### Answering a permission prompt (`src/answer.rs`)

A Claude agent in NEEDS YOU on a tool-permission prompt (`@agent_state`
needs-input, `@agent_detail_kind` perm, `@agent_kind` claude) can be
allowed or denied without going to its tab:

- **Sidebar.** Its NEEDS YOU card ends line 2 with two square caps,
  ` y  allow   n  deny` (solid green / red key-caps, the words on the
  card's own colour; still two rows, one background), and its age moves up
  beside `◉ perm`. Under 39 columns (Mack's usual 34) the caps alone,
  ` y   n `, so the detail keeps its room; under 26 (no second lines) and on
  one-line cards in a squeezed frame, none. A left click on a cap or its
  word answers; the rest of the card still goes there.
- **Menu.** On that agent's row the Space strip lists `y allow` and
  `n deny` first: Space y / Space n answer at once (no y/n of the menu's
  own; the capture below is the check).
- **CLI.** `agent-ui answer <@id> allow|deny [--client <tty>]`: exit 0
  sent, 1 refused (said through notify.sh, warn, to `--client` when given;
  `$NOTIFY_SH` names another), 2 usage.

Every path runs `answer::answer`, which sends nothing unless it is sure:

1. The window (as the current snapshot has it) is a Claude agent still
   in needs-input on a permission gate. Codex (`answer Codex prompts in its
   tab`), OpenCode and unknown kinds are refused: Codex's approval overlay
   ("Would you like to run the following command?", `› 1. Yes, proceed`)
   has not been verified on a live pane, so its keys are not guessed.
2. It is the prompt the click or key saw: a chip and a menu row carry the
   prompt they drew (`answer::prompt_key`: `@agent_since` and the detail),
   and a window on another prompt by now is refused (`that prompt changed
   · look again`). The sidebar also trusts a left press on a chip (or a
   diff badge) only once that same target (window, allow / deny, prompt)
   has sat at that cell for 400 ms (`answer::Arrivals`, kept per target
   and cell across redraws, so the spinner's 125 ms redraws never restart
   or fake it): perm cards' chips stack in one column, so a card leaving
   would slide the next card's chip under a click aimed at the old one. A
   right or middle press on a chip parks or pins the card, as anywhere on
   it.
3. `capture-pane -p` of the agent's own pane (`Actions::agent_pane`, by
   TTY in a split) shows Claude Code's dialog as the LAST thing on screen
   (`answer::detect`, against captures of Claude Code 2.1.306 in
   `src/answer/fixtures/`): the last line starting `Do you want to` (`…
   proceed?`, `… create x.txt?`, `… make this edit to …?`), numbered options
   under it in order, one reading exactly `Yes` and a `No…` one last, then
   only blanks, wrapped option lines and the `Esc to cancel · Tab to
   amend` footer. An answered prompt (the input box is back), an
   AskUserQuestion picker (`Type something.`, `Chat about this`, `Enter to
   select`) or any other layout is refused, with nothing sent; so is the
   select's `❯` anywhere but on the `Yes` line (moved onto an editable
   "don't ask again" row, a digit would type into it).
4. The capture rides in one tmux call with the pane's `cursor_y`,
   `history_size`, `pane_in_mode`, `pane_mode` and `synchronize-panes`,
   and the keys go through ONE `if-shell -F -t %N` whose condition is
   those same values plus the window's `@agent_since` and `@agent_detail`
   as the snapshot read them (escaped as format literals: `#,` `#}` `##`)
   (`answer::plan`): a pane that redrew or scrolled in between, or a
   prompt asked again in between, gets nothing (`its screen changed · try
   again`), checked by tmux itself at the send. Keys go to the
   pane by id: allow is the `Yes` option's digit (`1`: the single-use yes,
   never "Yes, and don't ask again"; Claude's select takes a digit as the
   pick), deny is `Escape` (the dialog's cancel, the same as its No). A
   pane left in copy or view mode (scrolled back) captures its live screen
   but would eat the keys as copy-mode commands, so `send-keys -X cancel`
   leads the same command list (only the scroll position is lost: the
   point is not having to go there); any other pane mode, and a window
   with `synchronize-panes` on (the key would reach every pane), is
   refused.
5. Then the window is discharged exactly as a look at it would: the
   indicator's own focus-hook entry point, `agent-tab-indicator.sh
   clear-current <window>` (needs-input → idle, `@agent_pending` and
   `@agent_seen_at` stamped), run through `run-shell` so it has the
   server's `$TMUX`. A denied call fires no PostToolUse, so without it the
   card stayed; and during a long allowed tool a second click would have
   answered the NEXT prompt. An allowed tool's heartbeat still re-arms
   `running` from the pending stamp, as after any answered prompt.

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
