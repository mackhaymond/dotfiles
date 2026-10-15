# Back / forward (Option-X / Option-C)

Browser-style history of the tabs each client has been on.

- **Option-X**: back to the previous tab. The tab you leave goes onto forward.
- **Option-C**: forward again.
- Any other move (a key, a click, Option-S, the sidebar, a menu, a
  `switch-client` from a script) is a new navigation: it is recorded and it
  clears forward.

This replaces Option-X's old meaning ("back to where the Option-S jumps
began", `agent-jump.sh back`). An Option-S jump is an ordinary navigation, so
Option-X right after it returns to where you were. After a run of several
Option-S jumps, Option-X steps back one jump at a time. prefix D still returns
to the start of the chain in one step.

## Rules

- One history per client, keyed by client name (its tty). Attaching starts a
  fresh one; detaching drops it.
- Entries are `<session_id>:<window_id>`. Consecutive repeats of a window
  collapse, and back keeps at most 50 entries (`NAV_CAP`).
- Back and forward skip, and drop, entries whose window is gone or now lives
  only in a hidden session (`@space_hidden`, or a name in
  `@space_hidden_sessions`: agents tasks stash scratch btop-popup). A parked
  tab is one example. A window moved to another visible space is followed
  there. Crossing spaces is fine. Landing in a hidden session never happens.
- A client sitting in a hidden session records nothing. Option-X from there
  returns to the last recorded tab.
- If there is nothing to move to, a chip says "nothing to go back to" or
  "nothing to go forward to".
- The sidebar refresh (`notifyutil -p com.mackhaymond.tmux.focus`) comes from
  the existing `session-window-changed[50]` / `client-session-changed[50]`
  hooks, which the move fires. nav.sh posts nothing itself.

## How it works

`scripts/nav.sh` keeps two global options per client:
`@nav_<key> = "<cur>|<back...>|<forward...>"` and a write counter,
`@nav_seq_<key>`.

- **Recording.** The hooks only say who may have moved. The script reads
  where that client is now, so a late or reordered `run-shell -b` hook can
  only record the truth. That costs one fork per focus change, gated in-server
  so that hidden and unwatched sessions cost nothing.
- **Back/forward are not navigations.** The new history (cur = target) is
  written in the same tmux command list as the move
  (`select-window ; switch-client ; set-option`). When the move's own hooks
  run, they find the client already on `cur` and do nothing. `cur` is the
  per-client marker.
- **No lock.** Every write is a compare-and-set on the counter
  (`if -F "#{==:#{@nav_seq_<key>},<read>}" {...}`). tmux runs one command list
  at a time. A writer that read stale state changes nothing and retries.
- Back first catches up with a move whose hook hasn't run yet, for example a
  click followed at once by Option-X.

## tmux.conf lines

These lines are what `tests/test_nav.py` loads, read from tmux.conf.tmpl once
they are there. Hook slot 30 is unused elsewhere.

```tmux
set-hook -g 'client-attached[30]' { run-shell -b "#{HOME}/.config/tmux/scripts/nav.sh attach '#{hook_client}'" }
set-hook -g 'client-detached[30]' { run-shell -b "#{HOME}/.config/tmux/scripts/nav.sh forget '#{hook_client}'" }
set-hook -g 'client-session-changed[30]' { if-shell -F "#{?#{@space_hidden},,1}" { run-shell -b "#{HOME}/.config/tmux/scripts/nav.sh record client '#{hook_client}'" } }
set-hook -g 'session-window-changed[30]' { if-shell -F "#{&&:#{session_attached},#{?#{@space_hidden},0,1}}" { run-shell -b "#{HOME}/.config/tmux/scripts/nav.sh record session '#{hook_session}'" } }
bind-key -n M-x run-shell -b "#{HOME}/.config/tmux/scripts/nav.sh back '#{client_name}'"
bind-key -n M-c run-shell -b "#{HOME}/.config/tmux/scripts/nav.sh forward '#{client_name}'"
```

## Keys checked

Option-C (`M-c`) is not bound in tmux's root table. Claude Code's Meta keys
don't include it (no `~/.claude/keybindings.json`). zsh isn't in vi mode, and
WezTerm has no Option-C binding. As with Option-X, the root binding takes the
chord from whatever runs in the pane: zsh's emacs-mode `M-c`
(capitalize-word) is lost.
