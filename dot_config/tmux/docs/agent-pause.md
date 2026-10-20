# Pause key (`prefix P`)

Before Mack closes the laptop, `prefix P` tells every agent that is **actually
working** to drop its work now and clean up, and saves exactly which agents it told.
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
`claude-<pid>.json`, `codex-<pid>.json` or `opencode-<pid>.json` record in
tmux-assistant-resurrect's directory, found the same way and in the same search
order as tmux-mcp's `readAgents`. An opencode record counts only when its
process is the interactive TUI, never `opencode serve`, `web`, `attach` and the
like (the same argv rule as agent-tab-watcher.sh). `message_agent` refuses any
pane without such a record. If a window has no record, the script falls back to
a pane whose command looks like an agent (`claude`, `codex`, Claude's
version-named binary, or `opencode` in a window marked `@agent_kind opencode`,
which tmux-mcp also accepts without a record). The other fallback deliveries
will probably be refused, and they show up as failures. Pausing an OpenCode
agent is the same text message as for the others, with no signal sent. A working window with
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

- **PAUSE**: urgent. The lid is closing now, so drop the work immediately
  rather than finishing the task or step (Mack's call). Clean up only what
  would be left broken: finish or undo the one edit in flight so no file is
  half-written, stop your own dev servers and long jobs, and stop subagents and
  workflows now. A message reaches a subagent only after its current tool call,
  so one in a long build kept a pause waiting for minutes (2026-10-19): stop it
  outright (TaskStop) and kill its pty, noting where it was for the RESUME. Start nothing else (no edits, tests, builds, commits
  or pushes), reply in 2-3 lines with where you stopped and what's next, and
  stay stopped until RESUME.
- **RESUME**: pick up from the noted next steps, and restart anything you
  stopped if you still need it.

## The popup (opt-in)

**Off by default** (Mack, 2026-10-19). The status lives in the sidebar's pause
banner (see "In the sidebar") and CuaNotch's pulsing "all paused" glow, and a
press says its outcome as a toast (`notify.sh`). Turn the popup back on with
`set -g @agent_pause_popup on` in `agent-pause.conf` (`AGENT_PAUSE_POPUP`
overrides it; the tests use that). Everything below describes it when on.

A line at the bottom of the screen is too easy to miss with the lid half
shut (Mack's call at the time), so with the popup on, any press with someone
to message opens a big popup on the client that pressed the key. It is border-less, about 70% wide and 60%
tall, never smaller than 60x16 (or the whole client when that is smaller), on
a solid tint with dark text.

**It opens at once, before delivery.** Under the lock, the toggle:

1. makes the cheap plan (reads tmux and the session records);
2. writes `progress.json`, with every target's result set to `sending`;
3. launches the popup with `Popen(..., start_new_session=True)`, without
   waiting (display-popup blocks until the popup closes, and the popup must
   outlive the toggle);
4. delivers, rewriting `progress.json` as each result lands;
5. writes the final state, then releases the lock.

The popup is `agent-pause.py watch pause|resume --press <id>`. While its press
is in flight it shows rows reading `<spinner> sending…` and the line
`sending PAUSE to N agents… K of N done`. **Enter is ignored while it is
sending**, so it cannot race the delivery; the footer says so. Esc still
closes it, and then the toggle also shows the outcome as a message line once
delivery ends. Whenever the popup is not still up when delivery finishes, the
line is shown, so an Esc'd popup never hides a failed pause. When
`progress.json` says done, the popup moves to the view for the outcome:

| press | outcome | view |
|---|---|---|
| pause | reached anyone (`paused.json` written) | the live PAUSE view below |
| pause | reached nobody (no `paused.json`) | peach **NOT PAUSED**, "nothing is paused", plus the error; stays up until any key |
| resume | reached anyone | green **RESUMED** with the results; closes after 4 s |
| resume | reached nobody, or tmux-mcp down | peach **NOT RESUMED** with the message line (what is still paused, what was dropped); closes after 4 s |

A second press while delivery runs finds the lock held and shows "busy". It is
never read as a resume, because `paused.json` does not exist until the pause
has reached someone.

**The popup never spins forever.** If the slow half of a press raises (for
example, `paused.json` cannot be written), `finished()` first marks the press
done with the error and then re-raises it to the usual handler (log plus line).
The popup then shows NOT PAUSED / NOT RESUMED with that error. A pause whose
`paused.json` did get written still counts as paused. If the toggle process
dies without marking the press done, the popup notices within a second (the
press id is `<ms>-<pid>`) and shows "the pause key crashed: see …/log".

**Position: centred on the WezTerm window, not just the tmux client.** Mack's
WezTerm can hold the sidebar as a native split beside the tmux pane (the strip
in `wezterm.lua`). A tmux popup centres on the tmux client only, so it looked
off-centre, and WezTerm has no floating panes. So `popup_geometry` shifts the
popup instead:

- It runs `wezterm cli --no-auto-start list --format json` (0.5 s timeout;
  `wezterm` from PATH, else `/Applications/WezTerm.app/Contents/MacOS/wezterm`)
  and finds the pane whose `tty_name` is the client's `#{client_tty}`.
  `--no-auto-start` matters: without a GUI to find (ssh, WezTerm quit, a stale
  socket), plain `cli list` would spawn a wezterm-mux-server daemon.
- It takes the bounding box of every pane in that tab and puts the popup's
  centre at the tab's centre, as far as it can while staying inside the
  client.
- The width becomes 70% of the tab, but no wider than twice the room on the
  nearer side of that centre, and never below the usual minimum.
- `-x` is the left column. Vertical centring is `-y C`, unless panes are
  stacked vertically too; then the same maths runs, giving a numeric `-y`,
  which tmux 3.7c reads as the popup's **bottom** row (measured: `-y N` puts
  the top at `N - h`).
- If there is no wezterm, it is slow, the tty isn't found, or the data is odd,
  you get the plain centred popup. Nothing is cached.
- Each press logs its `geometry` in a `popup` log line.

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
  shows the peach NOT RESUMED view instead, so nothing claims a resume that
  did not happen.

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
| `progress.json` | the press in flight, or the last one, for the popup: `press`, `mode`, `done`, `outcome` (`paused`/`failed`, or `resumed`/`not_resumed`/`failed`), `message`, and `targets` with `result` (`sending` until it lands). **Display data only**: the toggle never reads it. |
| `log` | one JSON line per press (mode, the message shown, every target and its result), plus a `popup` line with the geometry. Rolled to `log.1` at 1 MB. |
| `lock` | `flock`ed for the whole press |

The tmux global option `@agents_paused` holds the number of agents still saved
while paused, and is unset once none are left.

## The paused state

`paused` is an agent state, alongside idle, running, needs-input, failed and
done. Mack's rule (2026-10-19): a paused agent is not done, it is stopped for a
while. So there is no green, no NEEDS YOU row, no notification and no notch
glow for a single agent. The one loud signal is **all paused**, shown once the
whole saved set has stopped and kept until the resume.

| piece | writer | meaning |
|---|---|---|
| `@agent_paused` (window) = `<epoch>` | agent-pause.py sets it on every target BEFORE the PAUSE goes out (a reply's Stop can land within seconds), then unsets it on the targets the PAUSE did not reach. It unsets it BEFORE the RESUME, and sets it back on a refused RESUME. The watcher GC and `clear_state` drop it with the session. | this agent is in the saved paused set |
| `@agent_state` = `paused` | the tab hook: wherever a turn end would write `done` or `idle`, or do a quiet restore, while `@agent_paused` is set. `needs-input` and `failed` are written as always: only Mack can answer, and a failure must show. | the agent has stopped for the pause |
| `paused` → `idle` | agent-pause.py, when it clears the flag for a resume (the RESUME's prompt then makes it `running`); back to `paused` if that RESUME is refused | |

Every reader:

- `paused` is not working (agent-pause's own rule, and the safe-to-close
  check) and not attention. It never appears in NEEDS YOU, ⌥S, agent-jump or
  the mru tiers, and it gets no done notification and no per-agent glow.
- The watcher's reconciles (the seen-it discharge, the stuck-`running`
  reconcile, the idle seed) leave `paused` alone.
- **Display:** `⏸` in lavender (`#b4befe`) on the tab, the sidebar tile, the
  agent menu and CuaNotch's row. It is the same colour on every surface, and
  the sidebar's safe-to-close and ready banners are lavender too. Readers
  also count `@agent_paused` with `done` or `idle` as paused (the sidebar
  does, b1e2795), so a tile is right even before the hook writes `paused`.
- agent-pause.py posts `notifyutil -p com.mackhaymond.tmux.agent-state`
  after setting or clearing the flag, so the sidebar redraws at once.
- **All paused** = `paused.json` lists targets and none of their windows is
  working (the sidebar's safe and ready phases). CuaNotch shows one distinct
  lavender glow for it until `paused.json` goes away (the resume); the
  sidebar shows its banner.
- **Paused beats a quiet finish.** The PAUSE arrives through
  `message_agent`, so the tab hook may classify that turn as agent-started
  (`agent-tab-indicator.md` §6). `paused` wins over the quiet restore.

## In the sidebar

Mack closes the popup (Esc) or a tab and still wants to see where the pause
stands, so the sidebar (agent-ui) shows it too. It reads the files above plus
its own window options; the script publishes nothing extra. Phases, first
match wins:

| phase | when | shows |
|---|---|---|
| resuming | `progress.json`: `mode` resume, `done` false | resuming N agents… |
| pausing (sending) | `progress.json`: `mode` pause, `done` false | pausing, sending to N agents (K of N done; K = targets whose `result` is no longer `sending`) |
| ready to resume | `paused.json` has targets AND the last wake (`sysctl kern.waketime`) is after `paused_at` | N agents paused, prefix P (or a click) resumes; their labels |
| pausing (stopping) | `paused.json` has targets and any target window is still WORKING (`@agent_state` running, `@agent_workflow` or `@agent_cua` set) | pausing, K of N stopped; who is still working |
| safe to close | `paused.json` has targets, none working, no wake since `paused_at` | paused, safe to close |
| (nothing) | no `paused.json` and no press in flight | nothing |

A target's window is its `window_id`; after a resurrect restore that may be
gone (the script itself re-finds agents by session id), and a gone window
counts as stopped. A click resumes with `agent-pause.py resume
<client_name>`, never `toggle`: `resume` does nothing when no set is saved, so
a click on a stale banner cannot start a pause. Both files are written
atomically (temp file + rename).

The sidebar's own refinements (agent-ui `src/sidebar/pause.rs`, 41d6932):

- A boot after `paused_at` (`kern.boottime`) counts as a wake, so after a
  reboot it says ready, never safe to close.
- If the tmux server started after `paused_at`, every saved `window_id`
  counts as gone (a new server reuses `@N` ids).
- A press is in flight only while `done` is false, its `<ms>-<pid>` process
  lives, it began after the last boot and less than 20 minutes ago, so a
  toggle killed mid-delivery never holds the banner.
- A pause's `unidentified` rows and a resume's `gone` rows are left out of N.
- Only safe to close and ready are clickable.

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
| an error after delivery (writing state) | the popup's NOT PAUSED / NOT RESUMED view with `pause key error: …`, plus the line | whatever was written before the error |
| popup closed with Esc while sending | the outcome as a message line once delivery ends | as usual |

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

`agent-pause.py resume <client_name>` is the key's resume half alone (same
popup), and does nothing when nothing is saved. The sidebar's click uses it.

`dry-run` only reads tmux and the session records. It takes no lock, opens no
MCP connection and writes nothing.

Tests use a private tmux server under its own `TMUX_TMPDIR`, a stub MCP
server, and a private HOME and state dir, never the live server or port 7998.
The frame builder, the key handling (`on_key`, `state_gone`), the popup size
and `popup_geometry` are pure functions with unit tests. The `Popup` tests open
real popups, but only on the private server, on a client attached from a pty
the test owns. The wezterm CLI is never run from tests:
`AGENT_PAUSE_WEZTERM=off` skips it, and a file path there is read in its place
(that is how a sidebar layout is injected).

```sh
cd ~/.local/share/chezmoi/dot_config/tmux
( unset TMUX TMUX_PANE; python3 -m pytest -q tests/test_agent_pause.py )
```
