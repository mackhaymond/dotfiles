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
  failed red, working blue, done green, idle overlay); a `project/` prefix
  of the title moves to the dim tag on the right (a bare `~/` is dropped);
  `· here ·` marks the window the popup is covering; idle titles are dimmer
  but readable. Short of room the project tag goes first, then `here`.
- **Filters** (Tab / Shift-Tab): All, Active (working or needing you, or a
  prompt just answered), Needs you (agent-jump.sh's queue, in its order),
  Working, Idle, Parked (every parked tab). The sidebar's idle fold opens
  the menu on Idle, its `… N more` rows on All.
- **Initial cursor**: the oldest agent that needs you (the first in
  `agent-jump.sh list` order), else the last agent (where ⌥W would go), else
  the window the popup covers, else the first row. A state change never
  moves it; if the selected row goes away it moves to the nearest surviving
  row, and ⏎ / Space typed before a frame showed that are refused ("that tab
  moved · pick it again"), never retargeted.
- **Preview**: off by default. Space p turns on a strip of up to 10 lines
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
| Space | the actions strip for the selected row, in the footer; the next key runs one, Esc cancels: `p` preview strip on/off, `s` park (`stash.sh stash`, after y/n), `x` close the agent's pane through `closed-tabs.sh` after y/n (CMD+Z undoes it; on a parked row: discard via `stash.sh kill-many`), `r` restart the watcher, ⏎ go |
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

- **Who has one**: every agent window the menu lists (an `@agent_state`, in
  a shown session: not parked, not in `agents`/`tasks`/`scratch`/
  `btop-popup`), idle ones included. Parked tabs, plain shells and spaces'
  home windows (`@space_home`) have none. A window linked into two sessions
  is one agent (one letter, keyed by window id).
- **One assignment, two UIs**: the menu and the CMD+B sidebar both call
  `menu::letters::of_view` (snapshot + unscoped view + their own in-flight
  `KeyMemo`), so every open menu and sidebar shows and writes the same
  letters from the same snapshot; a write tmux already shows is never sent
  again, and two writers that raced converge on the next refresh (a
  duplicate goes to the lower window id). UB's ⌥F table reads the same
  `@agent_key`.
- **Alphabet**, in hand-out order: `a s d f g h j k l q w e r t y u i o p z
  x c v b n m` (26). Past 26 agents a row has no badge but is still
  navigable.
- **Stored** on the window as the tmux user option `@agent_key`, read in the
  same snapshot as everything else (no extra tmux call). On each refresh
  (`menu::letters::assign`): an agent whose `@agent_key` is a valid letter
  keeps it, unless another listed agent holds it too, where the lower
  numeric window id wins and the other is reassigned; every agent without a
  valid letter takes the lowest free one in alphabet order, agents taken in
  display order. A letter whose holder is no longer listed (closed, parked,
  exited) is free at once; if an unlisted window still carries a letter a
  listed agent now holds, its stale `@agent_key` is unset, so it gets a free
  letter (not its old one back) when it returns. A parked agent whose letter
  nobody took gets it back on unpark.
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
  scrolled off or on another filter still goes). If that agent closed and a
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
Command-N still goes by position): every agent row and NEEDS YOU card
starts with the agent's [letter](#agent-ui-menu-letter-keys) badge instead.

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
this section describes. agent-ui's sidebar starts each agent row and NEEDS
YOU card with the agent's lifetime letter, the same one the Option-W menu
uses (see [letter keys](#agent-ui-menu-letter-keys)), and replaces the tmux
status line while it is open. agent-ui's sidebar has its own layout (status
block, NEEDS YOU, agents, spaces; no log), its own sync (Darwin notices, not a poll) and leaves when
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
  for 1-9 only (10+ keep the blank column). That is the number tmux.conf's
  `M-1`..`M-9` jump by, so the two must change together. The number takes a
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
  resolution, same hidden/stash fallback, same shell rule, same ordinals,
  same Option-V / title toggle (`set-option -g @strip_scope`, the old scope
  kept and `couldn't set @strip_scope` shown on failure; the toggle is the
  AGENTS header's scope word once a client is known, else the ` agents` /
  ` space` title).
  In its own layout: the header's counts are that session's in space scope,
  the agents section (`agents  this space`, else `by space`) holds only the
  client's session's rule and rows; every row keeps its letter badge (no
  ordinals any more), and NEEDS YOU, the spaces list and parked stay
  global. A `@strip_scope` set from outside (tmux's `M-v`)
  shows at once when the binding posts the focus notice (see
  [agent-ui sidebar](#agent-ui-sidebar-layout-sync-lifecycle)). Its client row carries one more field than the
  Python's, `#{@space_hidden}` (the client's session's), for the hidden
  test. Spaces off and `@strip_scope` unset: frames byte-identical to before
  (`tests/sidebar.rs` `once_follows_the_scope_options` checks unset → set →
  unset).
- **Shapes and colours** (shape and colour both carry the state, herdr's
  rule): failed red `✕`, needs-input yellow `◉`, done green `✓` (finished and
  unseen; a visit discharges it), working `◐` pulsing pink/blue on
  `@agent_blink` (the window you are on stays blue), idle grey `○`; workflow
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
   ▄ prefix ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄    the usage panel: a TILE (filled, a
    S ━━━━━┃━━━━━━━━━  21% (-4%)       half-block pad above and below, no
    W ━━━━━━━━━┃━━━━━  60% (+17%)      border); the key table's mark a solid
   ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀    tab on its top pad; one meter per window
   NEEDS YOU                      2    NEEDS YOU, agent-jump.sh order: cards are
   ▌ a  Slides export          ✕ 3m    tiles tinted in the state colour, a ▌
   ▌    failed                         stripe (letter, title; reason)
   ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
   AGENTS          ⌥V  all spaces      THE BODY: every space (or the client's
   ● main   main          ◉1 ◐1 ○1    in space scope); every agent a 2-line
   ▌ d  Sidebar status ⠹ working      tile; the second line (below), the age
   ▌  › Running cargo test     26s     at its end; a state stripe at its left
   ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀    edge, a half-block pad under it while
     h  Canvas Syllabus        idle    there is room (else edge to edge,
        Synced 4 assignments     1h    zebra-shaded)
   ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
   SHELLS                              the client's space's shells, one-row
   ▌ ⌥1  ⌂ ~                           button tiles (⌥N; click: go there),
                                       the active one lifted
                                       spare rows collect here
   SPACES                              one row per space, ⌃N order: ` N ` on
    1 main ▶            ◐1 ○3          its colour, the name, its counts; the
    2  bai                 ○2          client's as @space_here (red on prefix)
   PARKED  3                           always one line: the stash's count; a
                                       message or the watcher on the right
  ```

  Chips take a page out of the tmux status line (its catppuccin blocks,
  square ends, no caps): a module is ` key ` bold in dark ink on a colour,
  then ` label ` on `surface0` — the NEEDS YOU and PARKED counts, the scope
  toggle ` ⌥V `, the shells in one line (`@space_shell`: ` ⌥N ` on blue,
  peach when active), the spaces as chips, `+K`. The client's space is
  `@space_here`: one chip in its colour (peach without one, red while the
  prefix is armed) ending in the powerline arrow U+E0B0 (`▶` above). Every
  tile and button leaves one canvas column at each side of the sidebar;
  section and space headers are text on the canvas. Palette: `tile`
  (#282839), `tile_alt` (the zebra's other shade), `tile_hi` (the client's
  agent, the active shell), key-caps on `surface1`. The panel shows no
  space chip (SPACES says which space the client is in) and no shells
  (their own section). Under pressure every agent row stays the same
  height: idle agents fold first, then every agent goes to one line at
  once; the SHELLS section becomes one line of modules before any agent
  loses detail.

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

- **Letters**: every agent tile (both scopes) and every NEEDS YOU card
  starts, after its stripe, with a ` a ` key-cap (a raised `surface1` chip):
  the agent's lifetime letter, from
  the menu's own assignment (`menu::letters::of_view`, see
  [letter keys](#agent-ui-menu-letter-keys)). A live sidebar persists the
  letters it assigns to `@agent_key` exactly like the menu (one batched
  background write, `KeyMemo`), so UB's ⌥W table, the menu and every
  sidebar agree; `--once` never writes. While the client's key table is
  `jump` (⌥W armed) every badge turns peach and the panel's top pad carries
  a `⌥W jump │ type a letter` tab. No letter (past 26): a blank badge; no letter
  anywhere: no column. The 1-9 ordinals and slot numbers are gone.
- **Status block** (`status::Bar`, built from the one per-tick snapshot,
  before home windows are hidden; no script runs per frame):
  - a tab on the usage panel's top pad: a red ` prefix ` chip while
    `#{client_key_table}` is `prefix`, a peach ` ⌥W jump ` chip and a
    ` type a letter ` one while it is `jump` (no panel: a row of their own);
  - this space's shells, in the SHELLS section under the agents (windows
    without `@agent_state`, window-index order, so the home window is shell
    1, exactly space.sh's `SHELLS`), each `⌥N` plus its label (spaces.conf's
    rule: a shell shows `~` or its cwd's last part, anything else its window
    name; `⌂` marks home), the active one lifted; a click goes there
    (`Target::Shell`, agent-jump.sh goto; a right-click never parks it);
  - usage meters: `@codex_<w>_used` / `_expected` / `_delta` / `_resets`
    (`<w>` = `session` / `weekly` / `scoped`, falling back to `_text`) with
    their `_color`; the pace as the status bar wrote it, `60% (+17%)`, no
    `Σ` for a pooled one; `--` while one is unset. A meter is Textual's
    progress bar: `━` in the label's colour over a `surface1` `━` track, `┃`
    where the pace expects it. `_members` (`slot:used:active[:cap],…`, the
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
  `@stash_sel`. Every selected agent tile (both lines) and shell button is
  drawn on a mauve tint with a mauve stripe, and the panel's first row reads ` N selected  h/l extend · ⏎ park · m move · esc cancel` (as
  many keys as fit). stash.sh posts the focus notice after sel-start,
  sel-move, sel-cancel and the park, so it shows at once.

  There is no event log and no divider any more (`@agent_sidebar_split` is
  no longer read; `--split` is accepted and ignored). The spaces list has no
  git line, so the sidebar never runs `git status`; the branch still shows on
  each rule.
- **SPACES** (the status bar is hidden, so this is where every space is
  seen): a section under SHELLS, after a gap: ` SPACES`, then one row per
  session, sessions with `@space_hidden` 1 left out, ordered by the session
  option `@space_index` (space.sh's number: main = 1, then the registry),
  unnumbered ones last by name; the agents section's groups use the same
  order. A row is the status line's module stood on end: ` N ` (⌃N, Ctrl+N;
  blank past 9) bold in dark ink on the space's `@space_color` (peach
  without one), its name on the row's tile, its branch dim when there is
  room, its agents' counts on the right (the AGENTS headers' glyphs). The
  client's space is `@space_here`: one chip across number and name in its
  colour, red while the prefix is armed, ending in the U+E0B0 arrow. As
  rows run short: the client's space and those with an attention agent and
  `+K more spaces` (click: the menu's All tab), then chips on as few as one
  line (`@space_here` and modules). A click switches the client to that
  space (`agent-jump.sh goto` on its active window, in that session). The
  index, hidden mark and colour are SESSION options read through the
  window-row format, so they cost no extra tmux call.
- **Stash** (PARKED, the last section, after a gap): always ONE line, at
  every height: ` PARKED` and the count as a square mauve chip (`none` dim
  with nothing parked), a message or the watcher's problem on the right.
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
- **Density ladder** (`layout::STEPS`): agents lose detail last. Before
  anything else the spaces' gap and one line of chips are held back, with
  the PARKED header; the steps that keep decoration (gaps, pads) must also
  leave the SPACES list its rows. A frame then takes the first step at
  which every agent still gets two lines: (1) SPACES and PARKED take the
  leftover rows, the richest form that fits (every space a row, else the
  client's and the attention ones, else chips); (2)
  the blank gaps go; (3) the pads go, the cards' and shell buttons' first,
  then the agents' (tiles edge to edge, alternating `tile` / `tile_alt`);
  (4) NEEDS YOU cards go to 1 line; (5) the shells to one line of buttons.
  Only then do agents compress (idle folded into a `○○○ 3 idle · …` tile,
  every agent one line, folded again, spaces collapsed to their header,
  then `… N more`), with NEEDS YOU held to at least 2 rows and a third of
  the body (`… N more need you`). Never two heights of agent row in one
  frame. Nothing is dropped silently: every agents-section row records the
  agents it stands for (`Row::covers`), and tests prove each is shown,
  folded, collapsed or counted at every size 2-110 rows.
- **Colour**: the canvas is the terminal's own background (`Color::Reset`,
  `--once` writes no `48;` code on it), so the split is WezTerm's colour.
  The surfaces are tiles and buttons in solid fills (no box-drawing lines
  around anything but a toast card): `tile` / `tile_alt` / `tile_hi`,
  NEEDS YOU cards in their state colour mixed over the canvas (`Ink::Mix`),
  the stash selection's mauve, the key-caps and the status-line modules.
  Rows left over after all that buy spacing back: the gaps above AGENTS,
  SHELLS and NEEDS YOU, the cards' pads, the gaps between spaces in
  AGENTS (never PARKED: it stays one line). Dim text on any
  surface is `overlay2` (`overlay` there is under 3:1).
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
  ones as nvim-notify cards OVER the top of the frame (no row moves; the
  rows beneath keep what they stand for), oldest on top, at most 3:

  ```
   ╭─  Done ──────────────── 10:02 ─╮    info  Info blue · ok  Done green
   │ parked Canvas Syllabus           │    warn  Warning peach
   ╰──────────────────────────────────╯    error  Error red
  ```

  A rounded border in the kind's colour over a fill of it (12% over the
  canvas), a canvas margin each side; the Nerd icon and title (`-T`'s, else
  the kind's) bold in the top border, the local time it was posted
  (`HH:MM`, libc `localtime_r`) right-aligned in it, dropped when narrow;
  the text word-wrapped into at most 3 lines, `…` on the last when more is
  left. Short frames drop the oldest card first, then body lines; a card
  never covers the last row, nor the key-table marks and the selection
  hint (the cards start under them). Under 16 columns or 4 free rows the
  newest toast is one compact row instead, down to 2 free rows. Each shows
  until `epoch + ttl` (default 4 s, error 8 s): the loop wakes at the
  earliest deadline on screen to draw that one away (no polling); a left
  click anywhere on a card dismisses THAT toast in that sidebar only, by
  its (seq, epoch_ms) id (the seq is read then written, so two racing
  posts can share one; never their posting time too). The dismissed ids
  are bounded (8) and forgotten once `@toast` no longer holds them.
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
  as the client's). It is the space's shell 1 in the status block.
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
