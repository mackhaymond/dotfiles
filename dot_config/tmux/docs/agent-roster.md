# Agent roster (`prefix e`)

A herdr-style list of every agent across every tmux session, in one popup.
It is a **reader**: everything it shows comes from the per-window options the
hook pipeline already maintains (see [agent-tab-indicator.md](agent-tab-indicator.md)).
It keeps no state and runs only while the popup is open.

| Key | Does |
|---|---|
| `prefix e` / `prefix C-e` | open the roster |
| Tab / Shift-Tab, `j` / `k`, arrows | move |
| Space / ⏎ | go there (a parked tab comes back via `stash.sh unstash`) |
| `g` | next tab that needs you (same as `prefix g`) |
| `x` | close the agent's pane through `closed-tabs.sh`, after y/n; CMD+Z undoes it |
| `H` | park the tab (`stash.sh stash`) |
| `/` | filter by `session:index title`; ⏎ keeps it, esc clears it |
| `a` | also show windows with no agent |
| `r` | restart the watcher (only needed when the red banner says so) |
| esc / `q` | close |

Related keys: `prefix g` / `prefix C-g` jump to the next tab that needs you,
`prefix G` jumps back (scripts/agent-jump.sh).

## Layout

- **NEEDS YOU** first, in exactly the order `agent-jump.sh list` returns:
  failed > needs-input > done (not done while a workflow runs), oldest
  `@agent_since` first. The roster never sorts this itself, so it and
  `prefix g` cannot disagree.
- Then one group per session, the client's own session first, then most
  recently used. `agents`, `scratch` and `btop-popup` are never shown; `tasks`
  is.
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

- Bound through `run-shell`, because `display-popup` does not expand formats
  in its command and the roster has to be told which client opened it
  (`--client #{client_tty}`).
- Once a second, while open: one `tmux list-windows -a` (US-separated fields)
  and one `agent-jump.sh list`.
- Moves go through `agent-jump.sh goto|next`: `select-window` then
  `switch-client`, so the visit discharges the tint like a tab click does.
- Watcher health: the mtime of `$TMPDIR/agent-tab-watcher.$UID.pid`, which the
  watcher restamps every tick. Older than 30 s (ensure_watcher's grace) shows
  a red banner; `r` respawns the watcher the same way `prefix r` does.
- Drawn with raw ANSI truecolour, not curses: inside tmux, curses only gets the
  256-colour palette, and these hues have to match the tab bar and CuaNotch
  exactly. The pulse reads `@agent_blink`, so it beats in step with the tabs.

## Troubleshooting

- **Popup flashes and vanishes**: run it by hand in a pane to see the error:
  `/usr/bin/python3 ~/.config/tmux/scripts/agent-roster.py --client "$(tmux display -p '#{client_tty}')"`.
  Careful: Space/⏎/`g` in that copy really do move that client.
- **Red "client is gone" banner**: the tty passed in no longer matches an
  attached client (for example, the terminal was reattached). Close the popup and reopen it.
- **Ages all read the same**: `@agent_since` is stamped by the watcher, so
  every window it found already running when it was first deployed shows that
  moment. Ages become accurate from the next state change.
