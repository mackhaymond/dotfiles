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

**The PAUSE overrides the draft hold** (Mack, 2026-10-21: "pause should
override that hold", after one PAUSE waited 6 minutes behind a draft). It goes
out with `urgent: true` (tmux-mcp 5b96c99): a draft no longer holds it, it is
sent along with the draft behind a note telling the agent that text was an
unsent draft, not a request, and it is submitted. Nothing is lost, since the
draft stays in that agent's conversation. A dialog (a permission prompt, a
menu) still holds it, because an Enter there would answer the dialog; such a
hold shows in the toast as `WAITING on <agent> (<tmux-mcp's reason>)` and as its own row in the sidebar (8a0b766). The RESUME is not
urgent and waits for a clear box as before.

- **PAUSE**: urgent. The lid is closing now, so drop the work immediately
  rather than finishing the task or step (Mack's call). Clean up only what
  would be left broken: finish or undo the one edit in flight so no file is
  half-written, stop your own dev servers and long jobs, and stop subagents and
  workflows now. A message reaches a subagent only after its current tool call,
  so one in a long build kept a pause waiting for minutes (2026-10-19): stop it
  outright (TaskStop) and kill its pty, noting where it was for the RESUME.
  **In that order** (2026-10-25): agent k killed its shells first, a workflow
  agent still alive re-created one 9 s later with a `sleep 240` poll, and the
  TaskStop that followed cancelled the agent's call but not the command, so
  k's tab read `running` (a busy pty, agent-bg-pending) for 6.5 min. Start nothing else (no edits, tests, builds, commits
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
| `limited.json` | the rate-limited set (see "Rate-limited agents"): `targets`, one per limited agent window (`pane_id`, `window_id`, `session_id`, `kind`, `label`, `resets_at`, `type`, `since`, `account`, `attempts`, and once they happen `lifted_at`, `resumed_at`, `last_attempt`, `last_result`; plus `location`, `pane_pid` and `pane_cmd` for `locate`; `held_at` while a held RESUME may still be queued in tmux-mcp). Written atomically. Separate from `paused.json`: a limited agent is never in the pause key's saved set. |
| `limit.lock` | `flock`ed while `limited.json` is read, changed and written (`limit-add`, `limit-tick`, `limit-resume`, and a resume press's limit half, after it has released the pause lock). Never the same lock as the pause key's, and never held together with it. |

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
- **Display:** `⏸` in violet `#7f6bff` on the tab, the sidebar tile, the
  agent menu, the space dashboard and CuaNotch's row. It is the same colour on
  every surface (cua-notch `dev/check-invariants` §65 enforces it), and the
  sidebar's banners use it too (a dimmer `#6c5bd9` while pausing or
  resuming). It was lavender `#b4befe`, then Mack asked for something more
  interesting; `#9d6bff` was 2.8° from CuaNotch's backup-running violet, so
  the two rings would have looked the same. Readers
  also count `@agent_paused` with `done` or `idle` as paused (the sidebar
  does, b1e2795), so a tile is right even before the hook writes `paused`.
- agent-pause.py posts `notifyutil -p com.mackhaymond.tmux.agent-state`
  after setting or clearing the flag, so the sidebar redraws at once.
- **All paused** = `paused.json` lists targets and none of their windows is
  working (the sidebar's safe and ready phases).
- **The notch, two phases** (Mack, 2026-10-19: "pulse while it's pausing and
  some strong solid glow of another color when it's done"; cua-notch
  fffa8f1). **Pausing** (a pause press in flight, or a saved target still
  working): violet `#7f6bff` pulsing hard, near-off to full every 1.7 s and
  swelling at the peak. **All paused**: a strong, steady electric cyan
  `#00d9ff` ring at that peak reach (white `#ffffff` first; Mack: "solid white
  is no good", and he picked cyan; cua-notch f01c127), held until
  `paused.json` goes away (the resume, wakes included). A resume press in
  flight shows neither. The cyan is the cue that the lid can close. The
  sidebar matches: dim violet banners while pausing or resuming, cyan for
  safe to close and ready.
- **Paused beats a quiet finish.** The PAUSE arrives through
  `message_agent`, so the tab hook may classify that turn as agent-started
  (`agent-tab-indicator.md` §6). `paused` wins over the quiet restore.

## Dismiss and void (after the lid opens)

Mack, 2026-10-21: on opening the laptop he may not want everyone back yet, but
also doesn't want the notch shouting cyan. So the paused set now has two more
ways out besides the resume:

| action | what changes | what stays | how |
|---|---|---|---|
| **dismiss** the glow | paused.json gains `glow_dismissed_at`; the notch's "all paused" ring goes dark | the saved set, `@agent_paused`, the `paused` rows (notch menu, tabs, sidebar cards) and the sidebar's banner | `agent-pause.py dismiss [client]`; a click on the notch while it shows the cyan ring |
| **void** the pause | paused.json is removed (recorded in paused.last.json as `voided_at`); every target's `@agent_paused` is cleared and a window still `paused` goes `idle`; no RESUME is sent to anyone | the agents themselves: they stay where they stopped, idle, with their notes | `agent-pause.py void [client]`; the sidebar banner's void action (confirmed); the notch menu's "Void pause" |
| **resume** | as always | | prefix P; a click on the banner; the notch menu's "Resume" |

- The dismissal lasts until the set is resumed, voided or replaced by a new
  pause; the violet pulse while pausing cannot be dismissed (it ends by
  itself). Both commands take the press lock and post
  `com.mackhaymond.tmux.agent-state`, so the sidebar and the notch redraw at
  once.
- **Where the actions live.** Sidebar (720b0f1): a plain click on the
  settled banner resumes; its `✕ void` chip (just `✕` at 34 columns) arms on
  the first click (red "void? click again") and voids on a second click
  0.7–3 s later; a dismissed glow shows as a dim "glow off". CuaNotch
  (cua-notch 160164e): a click on the notch while the cyan ring is the TOP
  glow dismisses it (a failure, question, finished agent or ask on top keeps
  its click); the menu's "Paused (N)" row has Resume and a two-press Void
  (0.7–4 s).
- **The banner stays until resume or void.** It shows whenever paused.json
  lists targets, dismissed or not. **The notch menu keeps the agents paused**
  until resume or void: a void removes paused.json, which the notch already
  treats like a resume for its rows (hook-written `paused` → ready), while
  nothing is sent to the agents.

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

## Rate-limited agents

When a turn hits a usage limit, the agent is a **paused agent with a
reason**, not a second state machine. It keeps every paused behaviour: no
NEEDS YOU row, no notification, its slot released. What it adds is a way
back: once the limit lifts, this script sends it the limit's own resume
text. The design contract is the "Rate-limited agents" contract (v1.1); this
section covers the script's half of it, the resume engine.

**Who marks it.** The tab indicator (StopFailure `rate_limit`; for codex, the
watcher's rollout reconcile) writes, in one tmux list, `@agent_paused`,
`@agent_limit "<resets_at> <type> <since>"`, the detail, the turn result and
`@agent_state paused` last, then runs `agent-pause.py limit-add <window_id>`
in the background. The indicator and agent-tab-watcher.sh are owned by
@tmux-agent-sidebar, and so is the watcher's trigger for `limit-tick`: it
spawns one when `@agent_limit_due` comes, when `sequence.json` changes (a
cswap switch), or on wake, at most once per 20 s.

| option | meaning |
|---|---|
| `@agent_limit` (window) | `"<resets_at> <type> <since>"`. `resets_at` is epoch s, or 0 when unknown (a model/Fable limit). `type` is `five_hour`, `seven_day`, `seven_day_opus`, `seven_day_sonnet`, `model`, `overage` or `codex`. `since` is when it was marked. Its presence means rate-limited; a Stop (success) clears it. |
| `@agent_limit_lifted` (window) | `<epoch>`: the limit has lifted but the agent waits for Mack (auto-resume off, or past the attempt cap). Set by `limit-tick`; unset when a limit RESUME gets through or a re-mark starts a new limit. |
| `@agent_limit_autoresume` (global) | `on` (agent-pause.conf's default) or `off`. Mack's setting. |
| `@agent_limit_due` (global) | the earliest epoch at which `limit-tick` could act on anyone, or unset when nothing can happen without Mack (or nothing is limited). Kept by this script, read by the watcher. |

### The commands

- **`limit-add W`** (`W` a window id, or one of its panes). Reads the
  window's `@agent_limit`, its agent pane and session id (picked as for a
  pause target), its label, and `account` from usage.json's root. The merge
  is idempotent, by window or session id: a re-mark (queued task
  notifications each hit the limit again) updates `resets_at`, `type`,
  `since` and `account` and keeps `attempts`. A re-mark with a new `since`
  is a new limit, so `lifted_at` and `resumed_at` go and
  `@agent_limit_lifted` is unset. A re-mark more than 15 min after our last
  RESUME is a **new episode**, not that RESUME's false lift (which re-marks
  within minutes): `attempts` starts over and `last_result` goes, so a cap
  reached hours ago cannot cap the next limit. Any other entry for the same
  window or session is pruned (its episode has ended), and an entry whose
  window now runs another session starts over as a new target. A window
  without `@agent_limit` is not added. It waits up to 180 s for `limit.lock`
  (a background job; a lost add would leave the agent unresumed). Then it
  recomputes `@agent_limit_due`.
- **`limit-tick`**. One pass over `limited.json` (details below). If another
  run holds `limit.lock`, it does nothing.
- **`limit-auto on|off|toggle`**. Sets the live global
  `@agent_limit_autoresume` (agent-pause.conf holds the default). Turned on
  while lifted targets exist, it sets `@agent_limit_due` to now so the
  watcher's next tick resumes them. Says the result as a toast.
- **`limit-resume [--force] W`**. Mack's manual resume of one limited window
  (the sidebar's click on a limited card). Refused with the toast
  `rate-limited until <local time>` unless the target is eligible by either
  rule below, or lifted, or `resets_at` is 0 (only Mack can judge a model
  limit), or `--force`. A click in the 120 s just after the reset gets
  `limit just reset · auto-continue first, try after <t>`. It skips the wake
  and network graces and the saved-set deferral (Mack asked); only a pause
  press still delivering stops it. It refuses a window that is already
  running, and otherwise needs only `@agent_limit` (not `@agent_paused` or
  `paused`), so an agent left idle by a RESUME that never landed can still
  be resumed by hand. While a held RESUME may still be queued for it, it
  refuses with `a resume is already waiting behind the draft in <label>'s
  box` unless `--force`: tmux-mcp would queue a second copy behind the
  first. It delivers the same way as the tick.

All four are hook, watcher and click paths: like the key, they never print
and always exit 0. Every outcome is in the log, and Mack's own actions
(`limit-auto`, `limit-resume`) also get a toast.

### limit-tick

For each target in `limited.json`:

1. **Drop it** if its window is gone, or no longer carries `@agent_limit`:
   a Stop after its RESUME (or after Claude's native auto-continue) cleared
   it, or Mack moved it on. A target is found the way the pause key finds a
   saved one (`locate`: by session id, so a resurrect restore is followed).
   The window's live `@agent_limit` then wins over the stored one (the same
   renewal as a re-mark), so a `limit-add` that never ran cannot leave a
   stale `since` for the cswap rule or the flag stamp. The stored `account`
   stays (it is the one the limit was hit on); now's only fills a target
   that has none.
2. **Actionable only while** the window has `@agent_state paused` AND
   `@agent_paused` AND `@agent_limit`. A running window (its RESUME's turn,
   or a prompt Mack typed) is skipped but kept: that turn's outcome decides,
   a Stop drops it, a fresh limit re-marks it.
3. **Eligible when** either:
   - **the clock:** `now >= resets_at + 120`. The 120 s grace lets Claude's
     native auto-continue (reset + 30-90 s) go first, so there is never a
     double prompt; or
   - **a cswap switch freed it**, which needs all four of:
     - `~/.claude-swap-backup/sequence.json`'s mtime is newer than the
       target's `since`;
     - `~/.cache/codexbar-tmux/usage.json`'s `updated_at` is newer than that
       mtime (the numbers are the new account's);
     - usage.json's root `account` differs from the target's `account`;
     - the matching window is free on that account. The family is
       `five_hour` → `session`; `seven_day_opus` / `seven_day_sonnet` →
       `scoped` (or `weekly` while `scoped_used` is null); `seven_day`,
       `overage` and anything else → `weekly`. Free means `<family>_locked`
       is `false` AND either `<family>_used` is a number under 100, or (no
       reading) usage.json's `fetched_at` is newer than the switch. The flag
       alone proves nothing: right after a switch the publisher adopts
       cswap's reading with every `*_locked` reset to false and `fetched_at`
       left behind, so a switch to an exhausted account would pass.

   `resets_at` 0 is **never** eligible by itself. `codex` targets go by the
   clock only. A target already lifted stays eligible until a re-mark.
4. **Graces.** The whole tick waits while `now - kern.waketime < 30 s` (the
   later of `kern.waketime` and `kern.boottime`, as the sidebar reads them):
   the network is still coming up. A target is skipped unless its host
   resolves (`api.anthropic.com`, or `chatgpt.com` for codex), checked with
   `getaddrinfo` in a thread with a 2 s timeout.
5. **A manual pause defers everything.** While `paused.json` lists targets,
   or a pause press is still delivering (its `paused.json` is written only
   afterwards, so `progress.json` says it: mode `pause`, `done` false, its
   toggle alive, and the press younger than 10 min by its id's millisecond
   stamp, so a record left behind whose pid was reused never stalls it),
   the tick acts on nobody; the toggle's resume takes the
   lifted ones along (below). The pause lock itself is never probed: the key
   takes it non-blocking, so even a brief hold would turn Mack's press away
   as busy.
6. **Auto-resume off** (or past the attempt cap): `@agent_limit_lifted
   <epoch>` on the window, `lifted_at` in the file, and one notify.sh toast,
   `limit lifted · resume? <label>`, once per lift. No message is sent.
7. **Auto-resume on:** each eligible target, at least 5 s apart:
   1. re-checked right before its delivery: still actionable, `paused.json`
      still empty and no pause press delivering. If a manual pause began,
      the tick **stops** there and the rest wait for its resume;
   2. `@agent_paused` unset (as for a pause RESUME; `@agent_limit` stays, so
      the paste makes it running and the turn's outcome decides);
   3. the message, through tmux-mcp's `message_agent`, **not urgent** (a
      half-typed draft holds it, like the pause RESUME):
      `[limit] Your usage limit has reset. Continue the task you were
      working on when the limit was reached; do not repeat work that is
      already complete.` (the native auto-continue's own words);
   4. **sent, held or no reply** counts as reached: `resumed_at`,
      `@agent_limit_lifted` cleared, the flag left off. A **held** one sits
      in tmux-mcp's per-pane queue, with no dedupe, until the box clears or
      for up to 2 h, so it is never sent again while a copy may be queued
      (`held_at`). `held_at` goes on any re-mark (a turn has run since, so
      the copy has landed) and after a later `sent`. If a held (or
      unanswered) RESUME has not landed 10 min on (the window neither
      running nor paused again), it is lifted for Mack, never resent, with
      one toast that says why: `limit lifted · a resume is waiting behind a
      draft in <label>'s box` (or, unanswered, `<label>'s resume got no
      reply: resume?`);
   5. on a **refusal**, `@agent_paused` goes back on (and `idle` back to
      `paused`), stamped with the limit's own `since`, never now: the
      indicator clears the flag on the turn's outcome only when it equals
      that value. Retried after the backoff;
   6. `attempts` + 1, `last_attempt`, `last_result`.
8. Finally `@agent_limit_due` is recomputed.

**Attempts, backoff and the cap.** Each RESUME that goes out counts one
attempt, whatever its result. The next one waits `120 s × 2^(attempts-1)`
after the last. After 3 the target is treated as auto-resume off: lifted, one
toast, and it waits for Mack (`limit-resume`, which still counts). If
tmux-mcp cannot be reached at all, nothing went out and no attempt is
counted. A new episode (a re-mark more than 15 min after the last RESUME)
starts the count over.

**No false-lift loop.** If a RESUME's turn hits the limit again (the switch
landed on an account that is out too), the indicator re-marks it with a new
`since`. The switch that freed it last time is now older than `since`, so
only a newer switch, or the wall-clock reset, can free it again.

**`@agent_limit_due`** is the minimum, over the actionable targets, of
`max(resets_at + 120, last_attempt + backoff)`, or the backoff alone for a
target whose limit has already lifted (eligible now, e.g. freed by a switch,
or lifted while auto-resume is on): a refused retry of a switch-freed agent
must not wait for a reset days away. A target with a held copy possibly
queued waits until that copy would expire, and a held RESUME still not
landed counts its 10 min check. A target waiting for Mack, a `resets_at` 0
target, and one that is not actionable (running) do not count.
If that moment has already passed after a tick (something was due but could
not be acted on: offline, deferred, refused), it becomes now + 60 s, or the
end of the wake grace, so the watcher does not respawn a tick every 20 s.
Unset when nothing counts.

**A switch waits for usage.json.** The watcher runs one tick when
`sequence.json` changes, but usage.json describes the new account only after
the publisher's next status tick (every 15 s), so that tick sees `updated_at`
not newer than the switch and cannot free anyone. While a switch is newer
than an actionable target's `since` and usage.json is not newer than the
switch, that target's due is at most now + 20 s, so the watcher keeps
calling until usage.json catches up; for at most 5 min after the switch, so a
usage.json that never settles stops the polling (back to the normal due).
This holds with auto-resume off too (the tick must still see the lift to mark
it), but not for a target already waiting for Mack, a codex target or a
model limit, none of which a switch frees.

**Times** read the same on every surface (toasts, refusals, the log,
status/dry-run), by calendar days in local time: today `5:31 PM`, the next six
days `Tue 5:31 PM`, anything else, a week or more on or in the past, `Nov 2
5:31 PM`. No leading zeros. (A bare weekday for a reset 6 days 20 h out read
as today.)

### With the pause key

- **Pause** never targets a limited window: it is not working, even with a
  workflow still out (that one is limited too). Only its own RESUME's turn
  (running, `@agent_limit` kept) is working, and is paused like any other.
- **Resume** (prefix P, the sidebar's banner, Enter in the popup): a saved
  target whose window now carries `@agent_limit` and is **not running** (its
  PAUSE reply's turn hit the limit) gets **no** pause RESUME, which would
  spend a turn on the same error. It keeps `@agent_paused`, rewritten to the
  limit's own `since` (see the refusal step above), leaves `paused.json`, is
  merged into `limited.json`, and the toast says `<label> rate-limited until
  <t>` (the popup row: `rate-limited (left paused)`). One that is running (P
  pressed during a `[limit]` turn) is resumed like any running target: handed
  over, its Stop would clear the limit and leave it paused in neither set.
- **After the RESUMEs**, the limited agents whose limit has lifted (lifted,
  or eligible by either rule) get the `[limit]` text, never the pause
  RESUME, staggered after (auto-resume on, not past the cap, no held copy
  queued), or are marked lifted without a message (off; the resume's toast
  says so). No wake or DNS grace: Mack pressed the key. This resume's own
  leftover misses in `paused.json` do not stop it; a new pause does. Then
  `@agent_limit_due` is recomputed.
- **None of the limit half runs under the pause lock.** Under it, the press
  only decides what goes where and writes `paused.json` (and the flag
  stamps). The merge into `limited.json` and the `[limit]` deliveries run
  once the press has released the lock (`limit_after_press`), so a pause
  press meanwhile is never turned away as busy. They also run after the
  resume's own toast, so their staggers never hold it up: their outcome is a
  second toast, and a `limit-after-resume` log line.
- **void** and **dismiss** never touch `limited.json` or `@agent_limit`.
- **status** and **dry-run** list the limited set: window, kind, type,
  `resets <time>` or `lifted`, attempts and label (dry-run adds which are
  eligible now, and marks a saved target that resume would hand over).

Tests: the `RateLimits` class (private server, stub tmux-mcp, a fake
notify.sh) injects the clock, the last wake and DNS through
`AGENT_PAUSE_NOW`, `AGENT_PAUSE_WAKETIME` and `AGENT_PAUSE_DNS` (`ok`,
`fail`, or a comma list of hosts that resolve), and sets
`AGENT_PAUSE_LIMIT_STAGGER=0`.

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
