# Pause key (`prefix P`)

Before Mack closes the laptop, `prefix P` tells every agent that is **actually
working** to stop at a safe point, and saves exactly which agents it told.
Pressing `prefix P` again tells **that saved set** to resume. It does not scan
again, because by then states have moved on: some paused agents went idle and
some idle agents started up. Idle agents are never messaged (Mack's
correction), so a fresh scan would resume the wrong set.

| piece | source (chezmoi) |
|---|---|
| binding | `dot_config/tmux/agent-pause.conf` (sourced from `tmux.conf.tmpl`) |
| script | `dot_config/tmux/scripts/executable_agent-pause.py` (python3, stdlib only) |
| tests | `dot_config/tmux/tests/test_agent_pause.py` |

## What counts as working

It reads the per-window options that the tab indicator hooks and
`agent-tab-watcher.sh` keep (see [agent-tab-indicator.md](agent-tab-indicator.md)):

| window option | meaning |
|---|---|
| `@agent_state` = `running` | a turn is in progress |
| `@agent_workflow` non-empty | a background workflow or subagent is still out, even if the main agent is idle or done waiting for it |
| `@agent_cua` non-empty | it is driving an app |

Anything else is **not** working: `idle`, `done`, `needs-input`, and `failed`
with neither flag. A `needs-input` agent is already blocked on Mack, and
tmux-mcp would only hold the message behind its dialog anyway.

**The agent pane** in a working window is the one tmux-mcp itself recognises
as an agent. That is a pane named by the `env.tmux_pane` of a live
`claude-<pid>.json` or `codex-<pid>.json` record in tmux-assistant-resurrect's
directory, found the same way and in the same search order as tmux-mcp's
`readAgents`. `message_agent` refuses any pane without such a record. If a
window has no record, the script falls back to a pane whose command looks like
an agent (`claude`, `codex`, or Claude's version-named binary). Those deliveries
will probably be refused, and they show up as failures. A working window with
neither is reported as "no agent pane", on the key's message line and in
`dry-run`.

## Messages and delivery

Delivery goes through tmux-mcp's `message_agent` tool, with `raw: true` and its
own `[pause key]` prefix. The script talks MCP Streamable HTTP to
`http://127.0.0.1:7998/mcp`, with the bearer token read from
`~/.local/state/tmux-mcp/token`. The token never goes on a command line or into
a log. Every call shares one session, deliveries run concurrently, and the
session is closed (`DELETE`) at the end.

tmux-mcp has a **draft guard**: if a target's prompt box holds a half-typed
draft or a dialog, the message is held and delivered once the box is clear. A
held message therefore counts as delivered.

- **PAUSE**: stop at the next safe point. That means no half-written file,
  stopping your own dev servers and long jobs, telling subagents and workflows
  to stop, starting nothing new, and replying in 2-3 lines with where you
  stopped and what's next. Wait for RESUME before continuing.
- **RESUME**: pick up from the noted next steps, and restart anything you
  stopped if you still need it.

## The popup

A line at the bottom of the screen is too easy to miss with the lid half
shut (Mack's call), so any press that sent something opens a big **centred
popup** on the client that pressed the key. It is border-less, about 70% wide
and 60% tall, never smaller than 60x16 (or the whole client when that is
smaller), on a solid tint with dark text. The toggle sends everything and
writes the state **before** opening it, after the lock is released, so the
popup never blocks a later press. The view is `agent-pause.py watch
pause|resume`, which reads `paused.json` or `paused.last.json` itself.

- **PAUSE popup** (peach `#fab387`). A block-letter **PAUSING** banner sits
  over one live row per agent: its label, its location, and one of
  `pausing…`, `held (draft in box)`, `stopped ✓`, `failed`, `gone`, or
  `not messaged (no agent pane)`. Rows are re-read from the window options
  once a second. When none of `@agent_state`=running, `@agent_workflow` or
  `@agent_cua` holds any more for every row (failed and gone rows count as
  settled), the banner flips to **SAFE TO CLOSE** and the tint turns green.
  The time since the press is shown. After 3 minutes with agents still
  pausing, a line reads `still working: <names> — close anyway or wait`. This
  is the popup Mack closes the lid on, and it stays open across the sleep
  (one tick a second, never a spin). The footer reads
  `⏎ Enter  resume all now   ·   Esc  close (resume later with prefix P)`.
  - **Enter** resumes right there: the same resume code as the toggle, under
    the same lock. If the resume reached anyone, it shows the RESUMED view for
    3 s (or until any key), with `N still paused — press prefix P again to
    retry` when a retry set remains. If it reached nobody, the popup stays on
    the PAUSE view with that line, and Enter retries.
  - **Esc** closes the popup. The saved set stays, so `prefix P` resumes it
    later. A popup killed under it (SIGHUP) counts as Esc.
  - **Every other key is ignored**, so a stray press on wake cannot dismiss
    it. That includes escape sequences such as arrows and focus reports.
  - **If the saved set changes while the popup is open** (someone pressed
    `prefix P` on another client), the popup notices by comparing `paused_at`
    and `resume_attempts`.
  - **After Enter, or after such a change, the popup decides by whether that
    resume reached anyone**, never by whether `paused.json` still exists. The
    file is also removed when a resume reached nobody and dropped the last
    misses.
    - Reached someone: it shows the RESUMED view, partial result included, and
      closes itself.
    - Reached nobody, but a retry set remains: it stays paused with a note.
    - Reached nobody and nothing remains: it shows a peach (never green) **NOT
      RESUMED** view, `dropped N unreachable agents, nothing resumed`, and
      closes after 3 s or on any key.
- **RESUMED popup** (green `#a6e3a1`). This is what `prefix P` shows when it
  resumes: a **RESUMED** banner and rows reading `resumed ✓`, `working` (once
  `@agent_state` is running again), `held (draft in box)`, `sent (no reply)`,
  `gone` or `failed`, plus the retry line when some agents are still paused.
  It closes by itself after 4 s, or on any key. A resume that reached nobody
  opens no popup, only the message line, so nothing claims a resume that did
  not happen.

The one-line `display-message` is now only the fallback: no client name, a
popup that would not open, or a press that sent nothing ("nothing paused",
tmux-mcp down, "busy").

tmux 3.7c quirk: while a status message is showing on a client, a popup opened
on it draws almost none of its content until the message expires. So
`open_popup` first replaces any message with an empty 1 ms one. The test
`test_prefix_p_resume_popup_closes_by_itself` covers this.

Preview without sending anything: `agent-pause.py watch pause --demo` (or
`watch resume --demo`) in any terminal plays the view with fake agents.

## State

Everything lives in `~/.local/state/agent-pause/` (override:
`AGENT_PAUSE_STATE_DIR`).

| file | contents |
|---|---|
| `paused.json` | while paused: `paused_at` plus `targets`, one entry per agent the PAUSE reached (`pane_id`, `window_id`, `location`, `label` (= `@agent_summary`), `kind`, `session_id`, `pid`, `why`, `result`, and after a resume miss `miss_count` and `first_miss_at`). Also `resume_attempts`, plus `failed` and `unidentified` (kept for the record). Written atomically. |
| `paused.last.json` | the latest resume attempt: the saved state it started from, plus `resumed_at`, `resume` (every target's result), `still_paused` (how many stayed in `paused.json`) and `resume_attempts` |
| `log` | one JSON line per press (mode, the message shown, every target and its result). Rolled to `log.1` at 1 MB. |
| `lock` | `flock`ed for the whole press |

The tmux global option `@agents_paused` holds the number of agents still saved
while paused, and is unset once none are left. It is there for a future status
indicator; nothing renders it yet.

**The toggle rule:** if `paused.json` exists and lists at least one target, the
press resumes. Otherwise it pauses.

- **Pause with nothing working:** no state is written and nothing is sent. The
  line says "no agents are working — nothing paused".
- **Pause:** agents the message *reached* are saved. Reached means *sent*,
  *held*, or *unknown*. Unknown means the request went out but no usable reply
  came back (a timeout, a reset, a malformed response). tmux-mcp never aborts
  a delivery when its client disconnects, so an unknown agent was probably
  paused. Saving it costs at most one stray RESUME; not saving it would leave
  an agent paused that nobody resumes. If every delivery *failed* (refused or
  an error reply), no state is written, so the next press tries a pause again.
  The state is written from whatever results exist, even if delivery hit
  something unexpected partway.
- **Resume:** for each saved agent, the script uses its old pane if that pane
  still exists and still runs the saved session id. Otherwise it looks for the
  pane now running that session id (a tmux-resurrect restore renumbers panes).
  If neither exists, the agent is reported as *gone*. The session id comes from
  `~/.claude/sessions/<pid>.json` for claude (the convention in
  `agent-session-lib.sh`) and from the record itself for codex.
- **Only reached agents leave the saved set.** After a resume, agents that were
  sent, held or unknown move to `paused.last.json`. Every **miss**, whether the
  agent failed or is gone, stays in `paused.json`, with a `miss_count` and a
  `first_miss_at`. The line then says `N still paused — press prefix P again
  to retry`, `@agents_paused` holds N, and the next press resumes again rather
  than starting a fresh pause.
- **When a miss is dropped.** An agent is dropped only when it has missed **at
  least 2 times AND its first miss is at least 60 s old** (`MISS_DROP`,
  `MISS_AGE`). The line then says "dropped after N misses". The count alone
  would let two quick presses right after a resurrect restore drop an agent
  whose SessionStart hook has not rewritten its record yet. The age alone would
  drop an agent on its very first retry after a long sleep. With both rules, an
  agent that tmux-mcp always refuses, or one that is really closed, cannot hold
  the toggle in resume mode forever. `paused.json` is removed, and
  `@agents_paused` unset, only once nothing is left.

## Failure modes

| what | what you see | state |
|---|---|---|
| tmux-mcp refusing connections, before anything was sent | `… tmux-mcp unreachable (…), fix: launchctl kickstart -k gui/501/com.mackhaymond.tmux-mcp` | pause writes nothing; resume keeps `paused.json`, so the next press retries |
| tmux-mcp not answering `initialize` (nothing sent yet) | `… no reply from tmux-mcp (…)`, with **no** kickstart advice: a kickstart drops the messages tmux-mcp is holding | same as above |
| token missing or rejected | `tmux-mcp token missing …` / `rejected the token …` | same as above |
| a message went out but no reply came back (timeout after 60 s, reset, malformed HTTP) | `… · n unconfirmed` | counted as reached and saved (see the toggle rule) |
| some deliveries refused | `… · FAILED n: labels` | pause saves only the reached ones; resume keeps the failed ones for a retry (see "When a miss is dropped") |
| a saved agent's pane is gone | `… · gone n: labels` | kept for a retry; dropped after 2+ misses spanning 60 s |
| double press | `pause key busy` (the second press sends nothing) | untouched |
| an error before delivery | `pause key error: …` | untouched |
| an error during delivery | each agent still gets a result (unexpected errors count as *unknown*) | written from the results |

The key path never exits non-zero and never prints, because `run-shell` would
open a view pane over the window. Every outcome is either the popup or a
`display-message` line on the client that pressed the key, plus one log line.
If the popup's view itself crashes, it shows the error and waits for a key (up
to 30 s) instead of blinking shut.

## Trying it without sending anything

```sh
~/.config/tmux/scripts/agent-pause.py dry-run   # who WOULD be paused (or resumed, and where), and why
~/.config/tmux/scripts/agent-pause.py status    # the saved set, or "not paused"
```

`dry-run` only reads tmux and the session records. It takes no lock, opens no
MCP connection and writes nothing.

Tests use a private tmux server under its own `TMUX_TMPDIR`, a stub MCP
server, and a private HOME and state dir, never the live server or port 7998.
The frame builder, the key handling (`on_key`, `state_gone`) and the popup size
are pure functions with unit tests. The `Popup` tests open real popups, but
only on the private server, on a client attached from a pty the test owns.

```sh
cd ~/.local/share/chezmoi/dot_config/tmux
( unset TMUX TMUX_PANE; python3 -m pytest -q tests/test_agent_pause.py )
```
