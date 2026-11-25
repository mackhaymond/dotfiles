## Nudge

Every so often the system asks Mack to review his old agents, so stale tabs
don't pile up until the letters run out. A timer checks every 5 minutes and,
when it is worth it and he is at the terminal, puts one toast in every
sidebar, a solid mauve card (any toast with a click action is one):

```text
 ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
 ██● Review agents ›             14:02██
 ██9 agents idle 8h+ of use · ⌃S O   ██
 ██or click to review                ██
 ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
```

**Clicking the toast opens the review popup** on the client that clicked
(`review.sh open <client>`). Clicking it also dismisses it, as with any toast.
`⌃S O` (prefix O) opens the same popup. The toast stays for 5 minutes, or
until the popup opens on any client (`review.sh open` takes it out of
`@toast`, so every sidebar drops it).

| piece | source (chezmoi) |
|---|---|
| script | `dot_config/tmux/scripts/executable_review-nudge.sh` (bash 3.2, as launchd runs it) |
| timer | LaunchAgent `com.mackhaymond.tmux-review-nudge`, `review-nudge.sh check` every 300 s (no RunAtLoad: the first check comes 5 min after load); installed by `.chezmoiscripts/run_onchange_after_zzz-review-nudge-launchagent.sh.tmpl` (the snooze timer's pattern) |
| toast click action | `notify.sh -a ACTION` (8th `@toast` field); agent-ui `src/sidebar/status.rs` `Toast::action_line`, `src/sidebar.rs` `App::click` |
| tests | `dot_config/tmux/tests/test_review_nudge.py`, `test_notify.py` (`-a`), agent-ui `cargo test toast` |

### When it nudges

All four must hold:

1. **There is something to review.** Either at least **3 agents have been idle
   8h+ of use** (time Mack was at tmux, so a night with the laptop shut counts
   for nothing), or **the letters ran out**. It is the review popup's own rule
   (docs/review.md, "8 hours of use"):
   the nudge asks `review.sh count`, which runs `agent-ui review --count`. The
   letters have run out only while **all 26 single letters a–z are held right
   now**: each is the `@agent_key` of some window in a visible session (not
   `@space_hidden`, not listed in `@space_hidden_sessions`) that is not a home
   (`@space_home` unset), i.e. an agent or a non-home shell. A window on a
   2-char overflow key (`;a`, ...) is not enough on its own: a window keeps its
   key for life, so it can outlast the shortage.
2. **No nudge in the last 4 hours.** The time of the last nudge is in
   `~/.local/state/agent-review/last-nudge` (an epoch). Every nudge restarts
   the 4 hours, whether or not anyone clicked. A junk stamp, or one in the
   future after a clock jump, does not hold the nudge back. A tick
   inside the 4 hours costs one tmux call: it still keeps the use log.
3. **Someone is there.** The tmux server must be running with at least one
   client attached. No server, or no client, means no nudge, and the 4 hours
   do not restart.
4. **Mack is at it.** Some client had input in the last 5 minutes
   (`#{client_activity}`). The timer also fires overnight and on dark wakes;
   without this, a nudge at 3am nobody saw used up the 4 hours. Away, the 4
   hours do not restart, so the nudge waits: it shows within 5 minutes of him
   coming back.

The text names whichever trigger fired:

| trigger | text |
|---|---|
| ≥ 3 idle 8h+ of use | `9 agents idle 8h+ of use · ⌃S O or click to review` |
| both | `9 agents idle 8h+ of use · letters ran out · ⌃S O or click to review` |
| letters only | `letters ran out (2 on ; keys) · ⌃S O or click to review` |
| letters only, no window on a `;` key yet | `letters ran out · ⌃S O or click to review` |

The toast goes out as `notify.sh -c '*' -k info -t 300 -m 30 -T "Review agents" -a
"'<review.sh>' open {client}"`, which means one toast for every sidebar. A
client with no sidebar gets tmux's own `display-message` with the text alone,
for 30 s (`-m`: tmux holds the panes still under a message), and it has
nothing to click.

### Toast click actions (`notify.sh -a`)

This is a general mechanism, and the nudge is its first user. `notify.sh -a
ACTION` stores a shell line as the 8th field of the toast's `@toast` record.
Records with 6 or 7 fields, from before this field existed, still parse
everywhere. When a toast has an action:

- its card is a solid mauve block (`ask_card`, agent-ui
  `src/sidebar/layout.rs`) and its title ends in ` ›`;
- a click runs the action on the clicking sidebar's client through `tmux
  run-shell -b`, with its output and exit status silenced, then dismisses the
  toast in that sidebar only. In the action, `{client}` becomes the client,
  shell-quoted. `#{client_name}` and `#{client_tty}` become the client's
  name. run-shell expands any other tmux format, so a literal `#` must be
  written `##`;
- the card is a *guarded* click target, like the diff badge: toasts land at
  the top of the sidebar without warning, so a click on one that appeared
  under the pointer less than 400 ms ago is ignored.

A toast without an action works as before: a click only dismisses it, and
its card is not guarded.

A sidebar still running a binary from before actions existed reads the 8th
field as part of the title: it shows the shell line there, and a click only
dismisses. Restart sidebars once the new agent-ui is built. The timer's
install script is named `zzz-` so that it sorts after
`run_onchange_after_zz-build-agent-ui`, and it has no RunAtLoad, so no nudge
goes out before the build.

### By hand

```sh
~/.config/tmux/scripts/review-nudge.sh --dry-run   # the decision; posts and writes nothing
~/.config/tmux/scripts/review-nudge.sh check       # what the timer runs
rm ~/.local/state/agent-review/last-nudge          # allow a nudge on the next tick
launchctl bootout gui/$(id -u)/com.mackhaymond.tmux-review-nudge   # timer off
```

Every tick that gets past the 4-hour check writes one line to
`~/.local/state/agent-review/log`, saying why it skipped or that it nudged.
The log is trimmed to its last 500 lines once it passes 1000. The
LaunchAgent's stdout and stderr go to `launchd.log` beside it.

Environment overrides, for tests: `REVIEW_NUDGE_NOW`, `REVIEW_NUDGE_TMUX`,
`REVIEW_NUDGE_ACTIVE`, `REVIEW_COUNT_CMD` (run by `bash -c`, default `'<review.sh>' count`),
`REVIEW_SH`, `NOTIFY_SH`, `XDG_STATE_HOME`.
