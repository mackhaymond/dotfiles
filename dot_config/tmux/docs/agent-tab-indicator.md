# Agent tab indicator

Each tmux tab (window) reflects the state of the AI agent (Claude Code or
Codex CLI) running inside it, rendered through the Catppuccin status bar:

For Codex 0.160 and newer shared-server launches, see
[Codex terminal ownership](codex-terminal-ownership.md). Hooks use a verified
thread-to-client binding; the server's inherited `TMUX_PANE` is not authoritative.

Each tab carries **three independent channels**:

| Channel | Surface | Says |
|---|---|---|
| **number chip** | `@catppuccin_window_*_color` | *motion* — is this window in flight? |
| **tab body** | `@catppuccin_window_*_background` | *attention tier* — does it want you? |
| **glyph slot** | `@catppuccin_window_*_text` | *the exception* — a job the chip can't describe |

State by state (unselected tab):

| State | Trigger | Chip | Body | Glyph |
|---|---|---|---|---|
| *(none)* | no agent process in the window | blue, solid | stock | — |
| `idle` | agent open, not working | blue, solid | stock | — |
| `running` | agent mid-turn | **pink `#f5c2e7` ↔ blue `#89b4fa`, 1 s** | stock | — |
| `running` + cua | agent driving an app | **pink ↔ blue** | stock | blue `󰍽` pulsing |
| *background workflow* | a Claude Workflow, or a background subagent (the Agent tool), still running after the turn ended | **pink ↔ blue** | stock (green "done" tint suppressed — it isn't really finished) | teal `󰒓` pulsing |
| `needs-input` | waiting on you: a permission gate or a question (`needs-approval` survives only as the hook MODE name) | crust (yellow digit) | yellow `#f9e2af` | — |
| `failed` | the turn itself died (`StopFailure`: 529, overloaded) | crust (red digit) | red `#f38ba8` | — |
| `done` | turn finished | crust (green digit) | green `#a6e3a1` | — |
| any | agent has a conversation title | — | — | tab name = `project/short-title`, else `#W` |

**The selected tab never pulses**: solid peach `#fab387` for every ambient state (idle/running/cua/workflow). You are looking at it — the terminal itself is the progress indicator. Attention states still force the chip to crust even when selected, because the digit's fg *is* the bright body tint and needs a dark chip to read; focusing a prompt is not answering it.

**Priorities.** Chip: attention (`needs-*`, `done`) beats motion — a permission prompt raised mid-workflow is a red tab with a *still* chip. `done` + workflow is untinted, so it pulses like any other in-flight window. Glyph: `󰒓` workflow > `󰍽` cua > nothing; on a tinted tab the glyph goes crust and stops pulsing (teal on yellow is unreadable, and a tab at maximum urgency doesn't need a second animation).

**The glyph slot is exclusive, and the gear wins on purpose.** A session that is both driving an app *and* running a background workflow shows only the gear. That asymmetry with CuaNotch is deliberate — in the notch a workflow is an *overlay* that composes with the state, so its popup row renders both (blue dot + teal gear, "done ⚙ workflow"), and a tab has one 2-cell slot. The gear is the one that must survive: it is the **only** carrier of "a fleet is still out", and it outlives the turn, so without it a finished-looking tab silently hides live work. The mouse is a *refinement* of a signal already on screen — cua only happens mid-turn, so the chip is already pulsing "in flight"; suppressing the mouse costs detail, suppressing the gear would cost the fact. Rendering both (4 cells) was considered and rejected: it doubles the width jitter for a rare combination, and the notch popup is the disclosure surface for exactly this (glow = urgency, tab/wings = roster, popup = detail).

There is deliberately **no "an agent lives here" marker** — the old mauve `󰚩` robot is gone, and so is the `●` that marked attention states (the body tint already says it, in three distinguishable hues; the same dot on all three added nothing but 2 cells). An idle agent tab is just a stock blue tab; presence stays legible from the tab *name*, which reads `project/short-title` for agent windows and `#W` (zsh, nvim…) for everything else.

Blue is both catppuccin's resting chip accent and the computer-use hue. On the chip blue reads as "at rest", so the pink↔blue pulse reads as working↔resting rather than as a third state — and computer use moves to the `󰍽` glyph. Glyphs still never blink between two hues (they'd read as another state mid-pulse); only the chip does, and only because its second hue means "nothing happening".

Tabs are **not** width-stabilised: the glyph slot's 2 cells appear and vanish with the workflow/cua flags. Those flip once per workflow rather than once per turn, so reserving a blank on every agent window would pad the common case to pay for the rare one.

**Seen-it semantics:** focusing a tinted tab discharges it to `idle`
(`after-select-window` → `clear-current`). And a `done` or `needs-input`
that lands while you are *looking at that tab* — its window active for a
client (`#{window_active_clients}` > 0) **and** WezTerm frontmost
(`lsappinfo`) — never tints at all (2026-08-25, Mack's ask): the hook
discharges it on arrival, and the watcher re-checks once a second for the
other order (tint landed while WezTerm was behind something, then you
Cmd-Tabbed back without changing tabs, which fires no select hook). A
discharged `needs-input` stamps `@agent_pending` exactly as `clear-current`
does, so the heartbeat re-arms `running` when the answered turn resumes.
Before this, `done` skipped its tint on the watched test alone — a watched
window in a backgrounded terminal was the one way to miss a finish — and
`needs-input` was always asserted. Switching away before the prompt lands
still gets the yellow. Red is never discharged by the *watched-on-arrival*
test — a dead turn is not answered by being looked at — but focusing the tab
(`clear-current`, `needs-*|failed`) does discharge it like any other tint.

## Architecture

Two per-window tmux user options are the single source of truth:

- `@agent_state` — `idle | running | needs-input | failed | done` (unset = no agent)
- `@agent_since` — `"<epoch> <state>"`: when the state last changed. Stamped by the hook's `set_state` at the transition itself, in the same tmux command list as `@agent_state` so the watcher can never snapshot one without the other (and unset by `clear_state`), because a watcher tick can take 4–6 s and a `done→running→done` inside one tick otherwise kept the old stamp. The watcher stays the backstop for every other writer (seen-it discharge, stuck-running reconcile, idle seed, GC): it compares the live state with the stored stamp each tick, leaves a matching stamp alone, so restarts lose nothing; unset with the state. Read by `agent-jump.sh` (oldest-first order) and the `prefix e` roster's elapsed column.
- `@agent_summary` — short conversation title
- `@agent_workflow` — `1` while a background Claude Workflow is in flight (else unset); set by the watcher, orthogonal to `@agent_state`
- `@agent_cua` — `1` while the agent is driving an app through cua-driver (lingers up to `CUA_LIVE`, 60 s); set by the watcher
- `@agent_rollout` — codex only: the thread's rollout path, stashed by the indicator so the watcher can tell a live turn from an interrupted one

Three components maintain and render them:

### 1. `scripts/agent-tab-indicator.sh` (event-driven)

Invoked as `agent-tab-indicator.sh <mode> <agent>` with the hook's JSON
payload on stdin. Hook processes are children of the agent, so
`TMUX_PANE` identifies the agent's window. Wired into:

**Claude Code** (`~/.claude/settings.json`):

| Hook | Mode |
|---|---|
| `SessionStart` | `idle` (skips `source=compact` — fires mid-turn; a fresh session shows `project/New Session` until the first turn titles it) |
| `UserPromptSubmit` | `running` |
| `PostToolUse` | `heartbeat` (re-arms `running` *only* from `running`/`needs-input`, so a late tool call can't resurrect a finished tab; never reads stdin — `tool_response` can be huge) |
| `PermissionRequest`, `Notification` (matcher `permission_prompt`) | `needs-approval` — **unless `tool_name` is a question tool, then `needs-input`** (see below) |
| `StopFailure` | `needs-input` |
| `Stop` | `done` |
| `SessionEnd` | `clear` (skips reasons `clear`/`resume` — a new SessionStart follows) |

Note: only `permission_prompt` notifications tint the tab — Claude's
`idle_prompt` (fired after ~60 s of waiting) is deliberately *not* wired, so
a finished tab stays `done`/green rather than escalating to yellow.

**One gate, one color** (fixed 2026-08-19): `PermissionRequest` and
`Notification`/`permission_prompt` describe the *same* permission gate —
the first is structural (always fires, carries the tool name), the second is
the "look over here" that Claude suppresses while the terminal is focused.
They used to map to `needs-approval` and `needs-input` respectively, so one
gate painted red or amber depending on which event landed first and on whether
you happened to be looking at the window. A permission gate is unambiguously an
approval, so both are red now. The downgrade guard stays: red must still win
when two asks are somehow live at once.

**But `PermissionRequest` is not only about permission** (fixed 2026-08-20,
user-reported: a tab went red "when it asked a question"). Claude Code routes
**`AskUserQuestion`** through the same structural event — captured from a
scratch session driven into a question:

```
mode=needs-approval  evt=PermissionRequest  tool=AskUserQuestion
```

So the previous day's rule was right about the *events* and wrong about the
*tool*. `tool_name` is what separates "approve this action" from "answer this
question": a question has no side effect and carries no risk, it wants a
choice. Painting it red spends the loudest signal on the least urgent thing,
which teaches you to discount red. `is_question_payload` routes
`AskUserQuestion` to yellow `needs-input`; everything else stays red, and a
question still loses to a standing gate.

Yellow therefore means two things now — *the turn failed, retry* (`StopFailure`)
and *answer a question* — united by "wants your words, not your consent".

`QUESTION_TOOLS` is deliberately the same identifier used in
`~/.local/bin/cua-notch-agent-hook`, which makes the identical distinction: it
is the grep handle tying the two surfaces together, and a future ask-shaped
tool must be added to **both**. A tool absent from the set defaults to red,
which is the safe direction. The Notification branch is gated the same way even
though no question arrives that way today — matching on the message with
spacing stripped, since Claude renders the tool into prose as "Ask User
Question" and a literal camel-case match would silently fail.

**Codex** (`~/.codex/hooks.json` — native hooks; the legacy `notify` slot
stays untouched for SkyComputerUseClient): `SessionStart` (matcher
`startup|resume`) → `idle`, `UserPromptSubmit` → `running`, `PostToolUse` →
`heartbeat`, `PermissionRequest` → `needs-approval`, `Stop` → `done`,
`SessionEnd` → `clear` (best-effort — codex clamps its SessionEnd hook
timeout to 3s; the watcher still backstops cleanup). **Codex requires
interactive trust approval for new or edited hook entries** — run `codex`
and accept the "Hooks need review" prompt (or `/hooks` in the TUI). Until
approved the hooks silently don't fire (even under `codex exec`) and codex
windows only get watcher-driven `idle` presence. Codex 0.148 hook events:
PreToolUse, PermissionRequest, PostToolUse, PreCompact, PostCompact,
SessionStart, SessionEnd, UserPromptSubmit, SubagentStart, SubagentStop,
Stop — no `Notification`/`StopFailure`, so codex `needs-input` never fires;
its only blocked state is the red `needs-approval`.

Subagent-context events (payload has `agent_id`) are ignored so a
subagent's Stop can't flip the main agent's tab. Codex background threads
(subagents, review/guardian workers, the Memory Writing Agent) fire the
same hooks from the same process — the script drops events whose
`session_id` maps to a non-`user` `thread_source` in
`~/.codex/state_5.sqlite`, plus any prompt opening with "You are a Memory
Writing Agent" (codex 0.147's memory writer repainted tabs as "Memory
Writing" through exactly that hole). Hooks that arrive with no `TMUX_PANE`
are dropped outright — the old active-window fallback let pane-less codex
contexts (ChatGPT-app threads) paint whatever tab the user was looking at.

**Summary sources** (best first): Claude — last `ai-title` entry in the
transcript tail (`transcript_path` from the payload), else `session_title`,
else the submitted prompt; Codex — `threads.name` from
`~/.codex/state_5.sqlite` keyed by `session_id` (0.148 stopped writing
`session_index.jsonl`), else `threads.title` (the first user message,
treated as interim), else the prompt.
Sanitized (no `#`/`"`/`%`, one line, ≤60 chars). `extract_summary` tags its
output `final\t<title>` (the agent's own conversation title) or
`interim\t<title>` (the prompt, standing in until that title exists) — only
`final` is worth a model call, see below.

**Tab name format**: `project/short-title`. Project is the basename of the
hook's `cwd` (`~` for `$HOME`). The raw title is condensed to its 2–4 most
identifying words by a detached `copilot -p …` call (answer on stdout, stats
footer on stderr; a plain text prompt grants no tool permissions) and cached
in `~/.cache/agent-tab/titles.tsv` keyed by title hash. The model's output is
**validated** before caching (1–4 words, no colon/sentence punctuation, not an
apology/refusal/auth-error) so error strings can't poison the cache; a
failed/rejected condense writes a negative row that backs off retries for
`NEG_TTL` (600 s) instead of re-calling every turn. Until the condensation
lands (or if `copilot` fails), the tab shows `project/<raw title>`
(word-trimmed to 24 cells) as an interim.

**Condenser lifetime and locking.** Title jobs run through `tmux run-shell -b`,
so hook process-group cleanup cannot kill them. The launch carries shell-quoted
terminal ownership and runtime settings; job output stays off the user’s pane.
A per-title `.flock` file uses an OS advisory lock held through the worker’s
lifetime. The kernel releases it on exit, including SIGKILL; the file remains
and is never unlinked. Old `.lock` directories are ignored, so an abandoned
worker cannot permanently block that title. Each worker has a separate temporary
stderr file for model-error handling.

**Cache by source text.** Both the interim prompt and the agent's final title
can be condensed, so the first turn can acquire a short label before the agent
names the conversation. A changed title has a separate cache key; repeated
hooks reuse cached labels. Raw text never overwrites an already condensed
label while a newer title is being shortened.

**Model pin is best-effort.** `--model` is set from `CONDENSE_MODEL`
(default `claude-haiku-4.5`, override with `$AGENT_TAB_CONDENSE_MODEL`, empty
= always copilot's default). copilot's whitelist tracks the CLI version and
the account's entitlements, and a pin that disappears fails *every* condense —
CLI 1.0.75 rejected `claude-haiku-4.5` with `Model "…" is not available`, so
tabs sat on their raw interim titles and the cache filled with negatives. That
reads as "the renamer is slow" when it is actually dead, so the failure is now
recoverable: on a rejection the condenser retries immediately on copilot's
default model and touches `$TMPDIR/agent-tab-model-unavailable.$UID`, which
makes later runs skip the doomed call for 24 h (`MODEL_SKIP_TTL`) before
re-probing the pin.

### 2. `scripts/agent-tab-watcher.sh` (presence daemon)

Singleton, spawned from tmux.conf, polls every 1 s. The pulse is NOT
driven by that loop any more (2026-10-07): a tick is 1 s of sleep *plus* its
work (`ps -ax` ~250 ms, then a per-file subagent scan ~450 ms and a cold
compaction-lineage walk ~1.5 s per session every 60 s — both since cut, see
"No compaction lineage walk" below), so per-tick toggling gave 1.7 s phases
with 4–6 s spikes. A forked child, `blink_loop`, owns
`@agent_blink` and flips it every second while the loop's flag file
(`$TMPDIR/agent-tab-blink.$UID`) exists; the loop only raises or lowers the
flag (any window `running`, a workflow, or cua). The child exits as soon as
the parent is gone or a *different, non-empty* pid owns the pidfile. An empty
or unreadable pidfile is neither a reason to exit nor to skip the beat — the
parent is alive, so the child keeps pulsing (an empty read used to mean
"exit", and the truncate-then-write restamp made ~1–2 children a day misread
themselves as superseded and freeze the pulse for good). The parent re-forks
the child on any tick that finds it dead (`kill -0`), and on exit kills it
only after checking bash's job table (`jobs -rp`) that `BLINK_PID` is still
its own unreaped child — a bare `kill "$BLINK_PID"` could SIGTERM an unrelated
process once a dead child's pid was reused. The child naps with
`sleep & wait`, not a foreground `sleep`, so that kill lands at once instead
of pending until the nap ends (bash defers trapped signals during a
foreground command); its trap reaps the nap. So there is still exactly one
toggler, and it cannot silently stay dead. The singleton guard is
ownership-aware: each start reaps any prior instance (by PID file, plus a
`pgrep` sweep for stragglers whose PID file was lost — two live daemons would
both toggle `@agent_blink` per tick and cancel each other out) and only clears
the PID file if it still owns it, and signal traps route through `exit` so
`kill` actually stops the daemon — every `prefix r` reload converges back to
one watcher (the naive check-then-write version leaked a daemon per reload). It
matches agent processes to windows by TTY (`ps -o comm` basename
`claude`/`codex`, plus the bare `N.N.N` pattern — Claude's binary is
version-named and `#{pane_current_command}` reports that, so formats can't
detect presence). Reconciles:

- agent present, no state → seed `idle`
- state `running` but the session says otherwise → back to `idle` (see below)
- no agent, state **or** summary set → unset both options (covers SIGKILL,
  `kill-pane`, crashes — SessionEnd is best-effort and codex has none; the
  summary is read separately so an orphaned title written by a slow
  condenser after the agent died is still reaped)

Hook-set states are never overridden while the agent lives, with one
exception:

**Stuck `running`** (fixed 2026-08-20, reported from the field: a tab pulsing
for a session that wasn't doing anything, "when I hit esc a few times to
interrupt"). **Interrupting a turn fires no hook at all** — there is no
interrupt/abort event in the wired set, and Esc produces neither `Stop` nor
`StopFailure` — so `@agent_state` stayed `running` and the tab pulsed until the
*next* completed turn. The same stuck state arrives from a missed `Stop`, a
hook that failed to run, or the deliberate `SessionStart(compact)` skip.

Rather than hunt the cause, the watcher reconciles against ground truth:
`~/.claude/sessions/<pid>.json` carries a **`status`** field (`busy` | `idle`)
that Claude Code maintains itself. A `running` window whose session reads
`idle` goes back to `idle`. Notes:

- **Only `running`.** Attention states are "always asserted, discharged by
  focus" by design, and a session sitting on an open permission gate *also*
  reads `idle` — clearing those from here would silently drop live prompts,
  which is the one thing this indicator must never do. Verified: `needs-approval`,
  `needs-input` and `done` all survive on an idle session.
- **Three consecutive idle ticks, not one.** At turn start the hook and
  Claude's own status write race, so a single tick can legitimately see
  `running` against a stale `idle`. Acting on that would clear the tab for the
  *whole* turn, because `heartbeat` re-arms `running` only from
  `running`/`needs-input` — never from bare `idle` — so nothing would put it
  back. 3 s of latency on a tab that used to stay stuck indefinitely.
- **Codex too, by a different route** (added 2026-08-20). Confirmed the same
  bug there by experiment: interrupting a streaming codex turn left the tab at
  `running` for 12 s+ while the pane read "Conversation interrupted" — codex
  fires no hook on abort either. It has no `~/.claude/sessions` equivalent and
  no pid→thread mapping the watcher could follow, so the signal comes from its
  **rollout stream**, which records turn boundaries explicitly: `task_started`
  opens a turn, `task_complete` closes it, and an interrupt writes
  `turn_aborted` (51/41/8 across the on-disk corpus). Live iff the most recent
  of the three is `task_started`. `agent-tab-indicator.sh` stashes the thread's
  `rollout_path` in `@agent_rollout` — it already reads that row on every codex
  hook to check `thread_source`, so the path costs one extra column — and the
  watcher tails **256 KB** of it, never the whole file (rollouts reach 27 MB
  here; p90 791 KB). Unknown status values and a missing/unset path are
  deliberate no-ops.
- **The last-marker scan is `awk`, not bash string ops.** The obvious
  `${chunk##*"$marker"}` idiom for finding a last occurrence is O(n²) on a
  256 KB string and hung the function outright on the first large rollout it
  met. One linear pass instead: 13 ms on a 6.4 MB file.
- **Cost.** The tail read happens only for a codex window *already* showing
  `running`, so a long live turn pays one read per tick until it ends — the
  price of having no status file to poll. Claude's path stays fork-free. Known
limitation: a one-shot `claude -p` exits right after `Stop`, so its `done`
tint is GC'd within ~1 s. Per-window state also means two agents in one
window share a single state (last writer wins).

**Liveness.** The daemon is the single point of failure for the blink, the
workflow gear and the GC, and its death is silent — a frozen pulse is the only
tell, and it is now easy to *miss*: `@agent_blink` unset renders as the second
color of each pair, and for the chip that is plain blue — i.e. a dead watcher
makes every running tab look idle rather than looking broken. (A workflow gear
freezes on dim teal.) That is why the guards below matter more than they used
to: the surface no longer reports the failure, so something else has to.

**Heartbeat.** Being alive is not the same as turning. Every liveness test
here — the pidfile, `ensure_watcher`'s `kill -0`, the `ps` identity check —
proves a *process* exists; none prove the *loop* is still going round, and a
tmux call or the `live_cua_pids` python wedging would leave a healthy-looking
daemon that quietly stopped reconciling. So the loop restamps its pidfile every
tick, making the mtime a liveness clock. **It is never truncated**:
`echo $$ > pidfile` truncates first, and a reader in that window sees an empty
file (~22% of reads in a tight loop) — which is exactly what killed the pulse
child above. A *claim* (startup, or a tick that finds the file empty/missing)
writes a sibling temp file and `mv -f`s it over (atomic rename). The per-tick
*restamp*, when the file already reads our pid, rewrites those identical bytes
in place with `printf … 1<>pidfile` (open without truncation; builtin, no fork
— an `mv` every second would be ~86k forks a day): readers see the same
complete content throughout, and the write still advances the mtime that
`ensure_watcher` and the roster read;
`ensure_watcher` treats a stamp older than **30 s** as a wedge and reaps the
daemon before respawning. The kill is `TERM` then `CONT`, because a wedge that
is *stopped* rather than blocked leaves the TERM merely pending — it would hold
the singleton while the respawn stacked a second daemon on top, and two
daemons toggling `@agent_blink` per tick cancel each other out. The loop also
re-reads the pidfile each tick and exits if a newer instance has claimed it,
which settles the same race from the other side. Verified end to end: a
`SIGSTOP`ped watcher is reaped and replaced on the next hook event, and a
healthy one is left alone across repeated hooks.

**Two rules if you add another guard here.** Both were learned by getting them
wrong first, on this daemon and on CuaNotch's probe queue the same evening:

1. *A liveness guard must settle who wins, not merely detect and restart.* Both
   first attempts created a second actor without retiring the first — here, a
   respawn stacked on a stopped-but-unreaped daemon (two of them toggle
   `@agent_blink` per tick and cancel out); there, a released in-flight flag
   while the blocked worker was still alive to wake up and publish a stale
   snapshot over a fresher one. The naive insurance converts a stuck-and-
   obvious failure into a subtly-wrong-and-invisible one, which is strictly
   worse than the bug it was written to fix.
2. *Validate with a forced failure, not with reasoning.* Reasoning about the
   happy path is what produced the bug, so it cannot be what confirms the fix.
   `SIGSTOP` the watcher, wait out the grace, fire a hook, and check that the
   old pid is gone AND that exactly one successor exists — then check that a
   healthy watcher survives repeated hooks unchanged. Rule 1's failure is
   invisible to any test that only asserts "something is running afterwards".

More generally: every move of work off a visible path — the fork reductions
above, a background probe queue — converts a loud failure into a quiet one, and
correctness on the happy path is exactly what those failures preserve. Budget a
heartbeat or a timeout on the new path as part of the move, not later.

Two further guards: it no longer
exits on the first failed tmux command (a transient failure is not a dead
server — it tolerates a streak and quits only once `tmux list-sessions`
confirms the server is gone), and `agent-tab-indicator.sh` re-asserts it on
every hook invocation (PID-file + `kill -0`; respawn via `tmux run-shell -b`
so it lands under the server, not under the hook process). So a death now
self-heals at the next agent event instead of persisting until `prefix r`.

**Background-workflow awareness** (Claude only — codex has no workflows): a
backgrounded Workflow keeps running after the main turn's `Stop` fires, and
there's no hook for it. But the Workflow runtime writes a live dir
`~/.claude/projects/<proj>/<session>/subagents/workflows/wf_<id>/` and only
writes the completion file `…/workflows/wf_<id>.json` when it finishes — so a
workflow is in flight iff its runtime dir exists *without* that completion
file. Each tick the watcher maps each claude window pane → pid →
`~/.claude/sessions/<pid>.json` → sessionId/cwd → that session's workflow
dirs, and sets/clears the per-window `@agent_workflow` flag. The blink driver
also toggles while any workflow is in flight, not just while a window is
`running`.

**Background subagents wear the same gear** (2026-08-25). The Agent tool runs
in the background too, and a turn that ended with three reviewers still out
is in exactly the position of one with a fleet out. A subagent's transcript
`…/subagents/agent-<id>.jsonl` has no completion file, and its own tail cannot
be trusted: 2.1.245 writes `stop_reason: null` on the final record, and "last
record is an assistant text block" misfires on the text-then-tool_use gap,
which scales with the tool call's payload (23% of measured gaps beat 5s, the
worst 86s — an opus review of cua-notch v0.4.0 caught seven live agents
reading as finished). The **parent** knows: when a background agent finishes
the harness appends a `<task-notification>` naming its `<task-id>` to the
parent transcript, promptly — the same witness `agent-bg-pending` uses for the
session's state. So a subagent is finished when its parent was notified about
it *since its transcript last moved* (a resumed agent moves again and is
running again), or when its last record is the user's interrupt marker (the
parent is never notified for an Esc); otherwise it is running, for at most the
workflows' one-hour backstop. The parent transcript is read incrementally
(bytes appended since the last look) and the interrupt verdict is cached by
mtime+size. CuaNotch's `tallyNotifications`/`subagentInterrupted` apply the
same three rules; `check-invariants` pins the tag, the marker, the tail
budget and the hour.

**Cost: one `stat` per session.** Sessions carry dozens of subagent
transcripts, so the subagent check stats the parent transcript and every
`agent-*.jsonl` in ONE `stat -f '%m %z %N'` call per session, reads the
parent's notifications at most once, and forks `tail` only for a file whose
mtime+size changed; the workflow check forks nothing unless a runtime dir
lacks its completion file. The parent is read by a python that seeks to its
last offset itself (BSD `tail -c +N` took 3.2 s on a 41 MB parent) and is
the first non-shim `python3` on PATH (the pyenv shim the watcher inherits
costs ~260 ms per call; re-resolved if it vanishes).

The **first** read of a parent is bounded, not from byte 0: a notice older
than now − 1 h − grace can never finish an in-window subagent, so it starts
at the last assistant record stamped before that minus an hour. Assistant
records are the anchor because transcript timestamps are not in file order —
a queued `<task-notification>` is stamped when queued and written later
(trailing by up to 70 min; hook attachments by up to 78 h), and an assistant
record is stamped when its streaming started but written when it ended, so a
notice written just before one can carry a later stamp (worst seen across 198
parents on 2026-10-07: 788 s). Hence the hour of slack. No anchor found → it reads everything.
Bounded and full reads gave identical answers for every live session.

Transcript text is untrusted: notification ids must be `[A-Za-z0-9_-]+` and
epochs all digits before anything reaches `$(( ))` — a crafted
`<task-id>abc=a[$(cmd)]</task-id>` used to run `cmd` through bash
arithmetic (fixed 2026-10-07; the unit test reproduces it).

Measured 2026-10-07 at load average ~200, both checks, all 14 live sessions:
cold (first tick) 213–279 ms total, warm 138–151 ms. The parts: a session
with no `subagents/` dir costs under 1 ms, and one whose dir holds only
stale transcripts still pays its one `stat` (5–15 ms at that load). A
session with in-window subagents also pays a python run (~20 ms startup at
best) on its first read and whenever its parent has grown: 40–90 ms cold,
5–45 ms warm.

**No compaction lineage walk** (removed 2026-10-07). On 2026-08-25 a
compacted session's `subagents/` had moved under a new sessionId while
`~/.claude/sessions/<pid>.json` still reported the old one, so the guards
looked in an empty dir and a park SIGTERMed two live reviewers.
`resolve_session_bases` then grew a transitive walk that grepped every
transcript in the project for an `"isCompactSummary":true` record naming the
old transcript — 643 MB, ~8 s per session, cached only 60 s, which made
watcher ticks take tens of seconds. Measured 2026-10-07 across all 1.9 GB of
`~/.claude/projects`: 6 transcripts carry a compaction summary and every one
references only itself — current Claude Code compacts in place, so the
sessions file's id is authoritative and the walk is gone. If Claude Code ever
reverts to new-id compaction, that tab loses its gear and `stash.sh`'s park
guard can't see that session's subagents; the signature is a compaction
summary naming a *different* `<sid>.jsonl`.

**Staleness rule** (changed 2026-08-19): a runtime dir counts as live iff one
of its transcripts (`agent-*.jsonl` / `journal.jsonl`) moved in the **last
hour** — a plain age test, deliberately. mtime is the only liveness signal
there is, and it lies in both directions, so the hour is a compromise between
two opposite failures:

- The old floor was 600 s, which darkened the gear on workflows that were
  still running — transcripts go quiet during API backoff, a tool with no
  timeout, or a subagent sitting on a permission gate. Worst quiet gap measured
  on this machine: **394 s**, so 600 s was a near miss. An hour gives 9× that.
- Anchoring "live" to the session's own start instead (transcript newer than
  `~/.claude/sessions/<pid>.json`'s birth time) was tried and reverted: it
  short-circuits permanently for any dir created during the session, so one
  crashed run would pin the gear until the agent process died. That is not
  merely a stray glyph — `done` + workflow renders the tab **untinted** by
  design, so a pinned gear suppresses that window's green for the rest of the
  session, and every later finished turn reads as still-working. It fails in
  the direction that hides work, which is the exact signal the gear exists to
  protect. A bounded 1 h wrong beats an unbounded one, and a plain age test
  self-heals without depending on how Claude Code happens to renew session
  files.

CuaNotch's `runningWorkflows()` carries the identical rule — **this is one of
two places**, alongside `session_has_running_workflow`; change them together or
the tab and the notch disagree about the same workflow.

> **Copy across surfaces, point within one.** A rule the tab bar and CuaNotch
> both enforce is stated *in full on both sides*, on purpose: whoever edits
> CuaNotch.swift's color block needs the rule in front of them, not a pointer
> into another repo they won't open. What keeps those copies honest is the
> marker — every one says "this is one of two places, change both" and names
> its twin. Don't "fix" that duplication by collapsing it to a pointer.
> Duplication *within* one document is the opposite case: collapse it. The
> approval-routing rule rotted in two spots (2026-08-20) precisely because
> those restatements were incidental — a wiring bullet that happened to mention
> the mapping — with no marker and no owner.
>
> Honest caveat on that reasoning: the marked copies have not rotted, but they
> are *days* old, and line 436 rotted in about one. So this is a design intent,
> not a track record — and when the markers were audited (2026-08-20) two of
> four were missing on the CuaNotch side, including one written two hours
> earlier by the person then arguing markers were doing the work. The marker is
> not a property these pairs *have*; it is one somebody has to remember. If a
> future audit finds them rotted anyway, the answer is probably a check that
> fails loudly — a test asserting the two constants match — rather than more
> prose.

One divergence is inherent and not a staleness-rule regression: the tab reaches
a session only through `~/.claude/sessions/<pid>.json` (pane → pid → sid/cwd),
so if that file is missing or lacks `sessionId`/`cwd` — pty wrapper, pid churn
— `session_has_running_workflow` returns "no workflow" and the tab goes
gear-dark while the notch still shows teal (it takes sid and cwd straight from
`agents.json` and never needs the pid mapping). Accepted: the tab has no other
route from a pane to a session.

### 3. Rendering (`tmux.conf`, Catppuccin v0.2.0)

Catppuccin builds `window-status-format` **once at load** from **global**
options — per-window `@catppuccin_*` overrides are impossible. Instead the
global `@catppuccin_window_default_background` / `_current_background`
options are set to a nested `#{?…}` conditional on `#{@agent_state}`.
Catppuccin pastes that string into all four tab segments that use
`$background` (number fg, middle-sep bg, text bg, right-sep fg), so the
whole tab tints consistently at render time. Constraints: the expression
must be space-free and quote-free (catppuccin's option reader splits on
spaces and strips quotes); tmux expands conditionals inside `#[…]` style
blocks (verified on tmux 3.6b).

Side effect of `fill=number`: the window NUMBER's fg is that same
expression, which would render a pastel digit on the blue/orange accent
(WCAG contrast 1.2–1.7 — illegible). The companion
`@catppuccin_window_*_color` conditionals darken the accent to crust
`#11111b` on attention states so the state-colored digit reads against it.

`@catppuccin_window_*_color` is also the number chip's *background*, which is
what makes it the motion channel: `_default_color` resolves to
`#{?#{@agent_blink},#f5c2e7,#89b4fa}` whenever the window is in flight
(`@agent_workflow` ‖ `@agent_cua` ‖ state `running`), and to plain `#89b4fa`
otherwise. `_current_color` has no blink branch at all — that is the whole
implementation of "the selected tab never pulses". Both keep the crust
override on attention states, which is checked *first* so attention beats
motion. Contrast holds on both phases: the unselected digit is surface0
`#313244` on pink (8.2:1) and on blue (6.5:1).

**Why pink and not mauve** (changed 2026-08-19): the first cut pulsed mauve
`#cba6f7` ↔ blue and read as too subtle — mauve and blue have near-identical
relative luminance (0.467 vs 0.449) and sit in the same blue-violet family, so
the pulse moved *hue only*. Pink is L 0.638: a 1.42:1 brightness step on top of
a ~100° hue swing, so the chip visibly lightens as well as shifts. It is also
the only mocha hue still free — lavender/sky/sapphire are the blue family
(mauve's failure, worse) and rosewater/flamingo/maroon are the red family.
Pink sits 27° from needs-approval red `#f38ba8`, which sounds close but cannot
collide: attention states force the chip to crust, so a red chip and a pink
chip never exist on the same surface (and pink is far lighter, 0.638 vs 0.404).

The text options add the exception glyph (teal `󰒓` workflow, blue `󰍽` cua,
nothing otherwise; crust and unpulsed on a tinted tab), a readable fg on bright
backgrounds (crust `#11111b`), and the summary with `#W` fallback:
`#{?#{n:#{@agent_summary}},#{@agent_summary},#W}`. The summary is
pre-shortened by the indicator script, so there is no render-side
truncation.

`rename-window` is deliberately **not** used for titles: it disables
`automatic-rename` per window and tmux-resurrect persists both the stale
name and that flag across restores. User options aren't saved by resurrect,
so stale summaries simply vanish.

### 4. Tick cost (2026-10-07)

A tick is one `LC_ALL=C pgrep -ax 'claude|codex|N.N.N'` (a new pid gets one
single-pid `ps` for its tty, cached per pid), one `tmux list-panes -a` read
(US-separated, summary last), then the per-window loop. Windows with no
agent pane and no `@agent_*` option take a fast path with no writes. The
subagent verdict is reused until its inputs change, as judged by per-tick
stamp files (`agent-tab-watcher.$UID.$$.stamp.N`, removed on exit).
Measured live: first tick after a restart ~250 ms, steady ~100 ms with
~175 windows and ~13 agents. It was tens of seconds before the lineage scan
was dropped, and 279 ms average just after.

## Troubleshooting

- **How long is a tick?** `: > "$TMPDIR/agent-tab-watcher.$(id -u).trace"`
  makes the watcher append `<epoch> <tick_ms> <windows> <agents>` per tick;
  `rm` the file to stop. No restart is needed, and when the file is absent
  the cost is one builtin file test. Failed ticks write no line.

- **Tab stuck in a state, running tabs never pulse (they just look idle) / the
  workflow gear never appears** → the watcher is dead. All three symptoms have
  one cause. Check the **pidfile**, not a process count:
  `PF="${TMPDIR:-/tmp}/agent-tab-watcher.$(id -u).pid"; kill -0 "$(cat $PF)" &&
  echo "heartbeat $(( $(date +%s) - $(stat -f %m $PF) ))s old"` — under ~2 s
  means the loop is *turning*, not merely resident.
  **Do not count `pgrep -f agent-tab-watcher` and conclude there are two
  daemons.** A forked subshell inherits its parent's argv, so every command
  substitution in the loop appears as an extra match while it runs — which
  reads as a singleton failure that isn't one (caught doing exactly this on
  2026-08-20: the "second daemon" had the first as its ppid and `ps` showed it
  as a bare `(bash)`). Match count is *correlated* with daemon count, not equal
  to it. If you must look at processes, discard any whose `ps -o ppid=` is the
  watcher. Since 2026-10-07 there is also one LEGITIMATE second match:
  `blink_loop`, the pulse child, whose ppid is the watcher. Any agent hook now respawns it
  automatically (see *Liveness* above); to force it, `tmux run-shell -b "bash
  ~/.config/tmux/scripts/agent-tab-watcher.sh"` or reload with `prefix r`.
  Confirm it is driving the animation: `for i in 1 2 3 4; do tmux show -gv
  @agent_blink; sleep 0.5; done` should print alternating pairs — sampling on a
  whole-second boundary reads the same value twice and looks stuck.
- **Codex tabs only ever show idle** → hook trust not granted; run `codex`
  and approve, or check `[hooks.state]` entries in `~/.codex/config.toml`.
  (Installing the entries reserialized `hooks.json` — whitespace/`\/`
  escaping changed but existing groups kept their content and indices, so
  established trust hashes should survive; if codex unexpectedly re-prompts
  for chezmoi-guard/NotchBar, re-approving once is safe. Pre-merge backup:
  `~/.codex/hooks.json.bak-agent-tab`.)
- **No summary on a fresh Claude session** → no `ai-title` yet; the first
  prompt is used as fallback, the real title appears on later events.
- **Tabs keep the long raw title (renamer never shortens them)** → the
  condense is failing, not lagging. Check for negative rows piling up:
  `awk -F'\t' '$2==""' ~/.cache/agent-tab/titles.tsv | tail`. Then run one by
  hand — it prints copilot's own error:
  `bash ~/.config/tmux/scripts/agent-tab-indicator.sh condense @1 proj "some test title"`
  and `copilot -p hi --model "$AGENT_TAB_CONDENSE_MODEL"`. A dead pin
  self-heals onto the default model (see *Model pin* above); `rm
  $TMPDIR/agent-tab-model-unavailable.$UID` forces an immediate re-probe.
  Purge stale negatives with
  `awk -F'\t' '$2!=""' titles.tsv > t && mv t titles.tsv`.
- **Wrong/garbled tab title stuck** → a bad condense may be cached. Clear it
  with `rm ~/.cache/agent-tab/titles.tsv` (rebuilds on the next agent event);
  a single bad title backs off for 600 s on its own (`NEG_TTL`).
- **Inspect state**: `tmux list-windows -a -F '#{window_id} #{@agent_state} #{@agent_summary}'`
- All files are chezmoi-managed: edit the **source** under
  `~/.local/share/chezmoi/dot_config/tmux/…`, then `chezmoi apply`. Note the
  scripts are `executable_*.sh` and the rendering lives in `tmux.conf.tmpl`
  (a chezmoi *template*) — use `chezmoi source-path <deployed-file>` to find
  the source for any of them. (`~/.claude/settings.json` and
  `~/.codex/hooks.json` are *not* chezmoi-managed.)
