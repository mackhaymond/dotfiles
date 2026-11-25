# Review old agents (prefix O)

Agents pile up: a finished claude sits in a tab for days. **prefix O** opens a
popup that walks them one at a time, so cleaning up is a few keystrokes per
agent instead of a hunt through every space.

- Code: `agent-ui review` (`agent-ui/src/review.rs`, the screen in
  `src/review/view.rs`), opened by `scripts/review.sh open <client>`.
- Binding: `spaces.conf` (spaces on only; `prefix O` is unbound with spaces
  off). prefix R was taken (⌘⌥T, replace the tab), so it is O for "old".

## Which agents ("candidates")

An agent window (`@agent_state` set) is up for review when all of these hold:

| rule | why |
|---|---|
| its session is a space: not `@space_hidden`, not `agents`/`tasks`/`scratch`/`btop-popup`, not `stash`, not a pending space | hidden sessions are not Mack's tabs |
| not a space's home window | a home is never parked or closed |
| state is `idle`, `done`, `failed` or `needs-input` | never `running`, `paused` or an unknown state |
| no `@agent_workflow` / `@agent_cua` | background work is still out |
| idle through at least **8 hours of use** by its `@agent_since` epoch | time Mack was at tmux, not wall-clock: a shut laptop ages nobody. `review.sh` passes it as `--min-idle` (below) |
| not pinned (`@agent_pinned`) | a pin says "keep this one" |
| not an abby-delegate child (`@agent_child`) | its parent's card owns it |
| not **kept** (below) | |

Order: space by space in ⌘N order (THE space order, `view::space_order`:
numbered spaces first, the rest by name), and in a space the longest idle
first. A window linked into two sessions is listed once.

## The screen

One agent per screen, in the sidebar's look (its canvas, one-colour tiles,
square corners). The card is its letter (`@agent_key`, shown as stored, so a
two-character overflow key such as `;a` shows whole) and title
(`@agent_summary`), then its harness logo, its space (⌘N and name in its
colour), its state and since when. Under it: its last reply
(`@agent_detail` of a done / ask / fail turn) and the last lines of its
pane on a terminal strip. Those are tidied: Claude's composer and status
line, rules and runs of blanks go (a typed draft stays), prose wraps, what
Mack typed is bold.

The popup takes 90% x 88% of the client (at least 108x32, or the whole
client when that is smaller). At 88 columns or wider the whole queue is
on the left by space, the one on screen lifted, the decided ones dim with
their outcome. The header has a segment per agent in its outcome's colour
(closed red, parked mauve, snoozed peach, kept teal) and `3 of 9`.

The dock holds the keys and is always there: a narrow popup tightens it,
then drops words. A message (what was just done, or why not) has its own
row above it, until the next key.

| key | does |
|---|---|
| `c` | close it: `closed-tabs.sh close <agent pane>` (⌘Z reopens it, agent resumed) |
| `p` | park it: `stash.sh stash <win>` (prefix H's; prefix h brings it back) |
| `s` | snooze it: a chooser with snooze.sh's menu (1 hour, 3 hours, this evening 18:00, tomorrow 9:00, Monday 9:00, Custom…); Custom is a typed box read by `snooze.sh parse`, so it takes free text ("next monday", "dec 3 at 2pm"; the forms are in snooze-when.py's header; a bad time says why under the box); then `snooze.sh apply -c <client> <epoch> w:@N` parks it with an alarm |
| `k` / Space | keep it: not asked again until it has worked again (below) |
| `v` | go to it: the popup closes and the client switches there (a card already closed / parked / snoozed, or a window gone or moved meanwhile, says so above the key bar instead) |
| `←` / `h` | back one (a closed / parked / snoozed one shows its badge and stays decided; a kept one can be decided again) |
| `→` / `l` / Tab | skip it |
| `q` / Esc | quit |

Before an action runs, the window is checked again against a fresh
snapshot: gone, moved to another session, working again, worked since the
card was made (its `@agent_since` changed), or pinned meanwhile is refused
with the reason above the key bar, and nothing changes (keep too). Park and
snooze also refuse up front what stash.sh would (a split tab with an
agent, …); close refuses a split tab as well (only the agent's pane would
go, and the tab would stay in its space).

Close, park and snooze are handed to the tmux server (`run-shell -b`), as
the agent menu's are, but nothing is recorded until the window has
actually left its session: the message says `parking…` (`closing…`,
`snoozing…`) and the popup polls for up to 3 s (`SETTLE`). Gone or moved:
recorded, and on to the next agent (or the space question). Still there:
the script refused (stash.sh keeps a half-typed draft, an agent without a
session id, a busy one…, and says why in a toast), so the message says
`stash.sh refused (see toast) · left alone` and the card stays undecided.
While an action is pending only q / Esc get through.

## Kept: `@review_kept`

`k` sets the window option `@review_kept` to the agent's current
`@agent_since` epoch. The agent is left out while the two are equal, so it
comes back only after its state has changed (it ran again, or was seen and
went from done to idle) and then sat idle for another 8 hours. Nothing else
reads or clears the option; it dies with the window.

## Closing an emptied space

When the review closes, parks or snoozes the last agent of a space (no
other agent window is left in it, running ones included), and that space
is not `main` nor hidden, the popup asks:

    No agents left in notes (2 shells). Close the space?   y / n

(shells counted as prefix Q counts them: every tab without an agent, home
included.) `y` runs `review.sh close-space <sid> <client> <wins…>`, which
closes the space only when both hold, else a warn toast and nothing closes:

1. every window this review took out of it has left the session within
   10 s (one still there was refused by its script: closing the space
   would put that tab, draft and all, into the backlog);
2. no other window there has `@agent_state` (an agent started, reopened or
   unparked into the space while the question was up; the same re-check
   closed-tabs.sh's ask-space-yes makes).

Then `@space_sh close <sid> <client>` (run as closed-tabs.sh runs it):
prefix Q's close, every tab through closed-tabs.sh (⌘Z reopens), the
registry entry dropped, clients on it moved to main. `n` / Esc continue
the review. tests/test_review.py covers both refusals.

## The end

After the last agent: `closed 3 · parked 2 · snoozed 1 · kept 2` (and the
spaces closed); any key closes. With nothing to review it says so and any
key closes.

## 8 hours of use

`review-nudge.sh` logs an epoch to `~/.local/state/agent-review/in-use` on each
5-minute tick that finds client input in the last 5 minutes. `review.sh`
(`min_idle`) walks that log back from now to the moment 8 hours of use ago and
passes the wall-clock distance as `--min-idle`, so the popup's `idle 2d+` is the
real time back to it. Nothing that old: the popup opens at 2 hours of use
instead. No log, or one with under 8 hours in it: 2 days (or as far as the log
reaches). `agent-ui review` run directly still defaults to 8 wall-clock hours
(`MIN_IDLE`).

## For scripts

- `agent-ui review --count [--min-idle SECS]` (or `review.sh count`) prints
  how many agents are up for review: one tmux call, no TUI (the nudge timer
  uses it).
- `agent-ui review --once WxH [--keys SCRIPT] [--plain]` renders one frame of
  the live server; `--keys` types into it first (`<esc>` `<enter>` `<left>`
  `<right>` `<bs>` `<up>` `<down>` `<tab>`, anything else a character) with
  every action dry: what would run is printed after the frame as `# …`.
