<!-- CODEGRAPH_START -->
## CodeGraph

This project has a CodeGraph MCP server (`codegraph_*` tools) configured. CodeGraph is a tree-sitter-parsed knowledge graph of every symbol, edge, and file. Reads are sub-millisecond and return structural information grep cannot.

### When to prefer codegraph over native search

Use codegraph for **structural** questions — what calls what, what would break, where is X defined, what is X's signature. Use native grep/read only for **literal text** queries (string contents, comments, log messages) or after you already have a specific file open.

| Question | Tool |
|---|---|
| "Where is X defined?" / "Find symbol named X" | `codegraph_search` |
| "What calls function Y?" | `codegraph_callers` |
| "What does Y call?" | `codegraph_callees` |
| "How does X reach/become Y? / trace the flow from X to Y" | `codegraph_trace` (one call = the whole path, incl. callback/React/JSX dynamic hops) |
| "What would break if I changed Z?" | `codegraph_impact` |
| "Show me Y's signature / source / docstring" | `codegraph_node` |
| "Give me focused context for a task/area" | `codegraph_context` |
| "See several related symbols' source at once" | `codegraph_explore` |
| "What files exist under path/" | `codegraph_files` |
| "Is the index healthy?" | `codegraph_status` |

### Rules of thumb

- **Answer directly — don't delegate exploration.** For "how does X work" / architecture questions, answer with 2-3 codegraph calls: `codegraph_context` first, then ONE `codegraph_explore` for the source of the symbols it surfaces. For a specific **flow** ("how does X reach Y") start with `codegraph_trace` from→to — one call returns the whole path with dynamic hops bridged — then ONE `codegraph_explore` for the bodies; don't rebuild the path with `codegraph_search` + `codegraph_callers`. Codegraph IS the pre-built index, so spawning a separate file-reading sub-task/agent — or running a grep + read loop — repeats work codegraph already did and costs more for the same answer.
- **Trust codegraph results.** They come from a full AST parse. Do NOT re-verify them with grep — that's slower, less accurate, and wastes context.
- **Don't grep first** when looking up a symbol by name. `codegraph_search` is faster and returns kind + location + signature in one call.
- **Don't chain `codegraph_search` + `codegraph_node`** when you just want context — `codegraph_context` is one call.
- **Don't loop `codegraph_node` over many symbols** — one `codegraph_explore` call returns several symbols' source grouped in a single capped call, while each separate node/Read call re-reads the whole context and costs far more.
- **Index lag — check the staleness banner, don't guess a wait.** When a codegraph response starts with "⚠️ Some files referenced below were edited since the last index sync…", the listed files are pending re-index — Read those specific files for accurate content. Files NOT in that banner are fresh and codegraph is authoritative for them. `codegraph_status` also lists pending files under "Pending sync".

### If `.codegraph/` doesn't exist

If CodeGraph is not initialized for the current project, run `codegraph init -i`
from the project root before using CodeGraph for structural navigation. If the
current directory is not clearly inside a project, do not initialize CodeGraph in
`$HOME`; briefly report that there is no clear project root and continue with
normal file inspection. If `codegraph` is unavailable or initialization fails,
report the issue and fall back to normal file inspection.
<!-- CODEGRAPH_END -->

## GUI work (cua plugin enforces + briefs; this is the always-on core)

The user works at this machine while agents drive it: GUI work is
BACKGROUND-ONLY. The cua plugin injects a live briefing per session and a
playbook when GUI work is detected; skills carry the mechanics
(cua:drive-app, cua:browse, cua:arc, cua:electron-door). The always-true
core, even if the plugin is absent:

- `open` is allowed, but it brings its target to the front: use it to show
  the user something, not to launch an app you will drive in the background.
- Never AppleScript activate, bring_to_front, or `yabai --focus` — launch
  apps for background driving with `cua-bg-launch`, drive with `mcp__cua__*`
  (start_session first, end_session when done).
- Browsers: `agent-browser` (headless) for web tasks; `agent-chrome` for
  bot-blocked sites; the user's Arc via cua AX tools only.
- Never kill -9 a GUI app, never trigger a modal sheet on a background
  window, never `screencapture -v -R` while the user is present.
- delivery_mode stays "background"; watch mode only on the user's explicit
  request; verify postconditions from rendered evidence, not accepted calls.

## Shell commands: use the pty MCP tools (native Bash is disallowed)

The native Bash tool forks a fresh shell per command — measured as the top
agent battery drain on this machine — so it is deny-listed. Run shell
commands with the `pty` MCP server instead (`mcp__pty__*`):

- `run(command)` — Bash-equivalent: synchronous, returns output + exit code.
  It executes in a persistent zsh, so cwd, env vars, functions, aliases,
  shell options, background jobs — **and traps** — carry over between calls;
  no need to re-`cd` or re-export each time — **but the shell starts in the
  home directory, so `cd` to the project on first use**. Don't `exit` in a
  command (it kills the persistent shell).
  Default timeout is 120s (max 600s via `timeout_ms`): **raise it for
  builds/tests expected to take minutes** — a timeout doesn't kill the
  command, it leaves the pty busy. Anything that may exceed 10 minutes:
  `spawn_pty` it and poll `read_pty`. Output beyond ~30k chars is clipped
  middle-out — redirect big logs to a file and grep them.
- **Never hang cleanup on shell exit.** Everything else in that persistence
  list is passive — it changes how your NEXT command behaves. A trap is
  active: it runs code later, unattended. `trap ... EXIT` does not fire when
  your command ends, it fires when the shell finally exits — which, since
  PTYs linger ~1h and are adopted back on resume, may be a session restart
  or a grace-period reap long after you are gone. (Real incident: a
  `trap 'cp backup orig' EXIT` armed for a two-minute test fired an hour
  later and put a stale backup over an hour of new work, with `git status`
  clean.) Do cleanup inline, and commit work you can't afford to lose.
  `run` warns once when a pty's EXIT/HUP/TERM traps appear or change, and
  every shell death is recorded with its reason in the pty's log and in
  `~/.local/state/tmux-pty-mcp/exits.log` — grep there first when something
  on disk changed and nothing explains it.
- **Batch related shell commands** into one `run` call (`&&`/`;` chains)
  rather than many small invocations — each call is a tool round-trip plus
  tmux client forks, and batching avoids queueing behind the per-pty
  serialization.
- **One shell per session by default.** Sequential work goes in the
  default `main` pty. Open a named pty (`pty: "<name>"` on `run`,
  auto-created) only for work that truly runs in parallel — concurrent
  subagents, which share this session's ptys, each use their own name —
  or when you need a clean shell context. Concurrent `run`s on one pty
  queue, and a busy pty refuses new runs. Never `send_keys` a command line
  into a busy pane.
- `spawn_pty(name, command?)` — for anything long-lived (dev servers,
  watchers, REPLs); never `run` a command that doesn't exit. Check on it
  with `read_pty`, interact via `send_keys` (tmux key names like `C-c`),
  stop it with `kill_pty`.
- **Secrets**: command text and ALL pty output are written to plaintext
  logs under `~/.local/state/tmux-pty-mcp/`. Never inline tokens or
  passwords in commands (source them from env/files/keychain), and never
  type a password via `send_keys`. **sudo is hooked up to Touch ID
  (pam_tid)**: running `sudo <cmd>` pops a Touch ID prompt on the user's
  screen — so you CAN run sudo commands when needed; tell the user a
  Touch ID prompt is coming, give the run a generous timeout, and only do
  it when they're around to approve. Never use `send_keys` to type a
  password, and never spam repeated sudo attempts (each one prompts).
- **If pty tools fail to connect**, the shared server (LaunchAgent
  `com.mackhaymond.tmux-pty-mcp`) is down and you have NO shell. Do not
  improvise another execution path (AppleScript, GUI terminals); report it
  and give the user the fix:
  `launchctl kickstart -k gui/501/com.mackhaymond.tmux-pty-mcp`.
- **Lifecycle — resume beats cleanup.** The user usually ends Claude
  sessions by closing the tmux tab, so PTYs are NOT torn down on session
  death: every window lingers for a ~1h grace period (idle named ptys are
  reaped sooner, see below), and if the user
  resumes the session (`claude --resume`) within it, ALL its PTYs are
  adopted back automatically — the main shell with its cwd/env intact,
  plus any spawned ones. **Check `list_ptys` at the start of any session
  that might be a resume** — adoption can take a few seconds after session
  start, so if prior shell state is expected and the list is empty, wait
  briefly and check once more before creating new state.
- **Idle named ptys clean themselves up.** The server reaps a named,
  non-persist pty once it has sat at an idle prompt (no command in flight,
  no background processes) for ~10 min, even while your session lives.
  Using the name again gives a fresh shell in the same cwd, and the result
  starts with a `[note]` that earlier env vars, functions and aliases are
  gone. `kill_pty` is still the fastest way to free a pty you're done
  with (a stopped server, a finished subagent task), but forgetting no
  longer leaks a shell. Each session has a soft cap of 12 named ptys
  (idle ones are evicted first), and new ptys are refused with an
  explicit error above 90% of the machine's pty limit — if you hit it,
  kill your idle ptys and tell the user which sessions the error names.
- `spawn_pty(..., persist: true)` opts a PTY out of reaping entirely —
  it survives indefinitely until someone kills it. Use it only for
  processes genuinely worth keeping (a dev server mid-debugging), and
  still `kill_pty` it once its purpose is over.
- stdout/stderr are merged (real PTY); a timed-out `run` keeps running —
  `read_pty` to check, `send_keys ["C-c"]` to cancel.
- In zsh, quote `'=agents'` in hand-written tmux targets — an unquoted
  leading `=` triggers zsh command-path expansion and aborts the line.

## Mack's tmux workspace: spaces, shells, agents

One tmux server, seen through WezTerm with the agent sidebar on the right
(⌘B). Mack talks in these terms, not tmux's; the `tmux-workspace` skill has
the full vocabulary table, read-only recipes and keys, so load it whenever
they name a space, shell, agent, letter, parked tab or NEEDS YOU.

- **Space** = a tmux session not marked `@space_hidden`, numbered ⌘N by
  `@space_index` (main = 1); it lasts until closed (prefix Q). Hidden
  sessions (agents, tasks, stash, scratch, btop-popup) are never spaces;
  `stash` holds parked tabs.
- **Home** = each space's uncloseable first window; a space IS its home
  (⌘N lands there). **Shell** = any other window of a space without
  `@agent_state`; it has a **letter** from the agents' pool ("shell e" =
  `@agent_key` e, no `@agent_state`), so ⌥W + letter reaches a shell or an
  agent in any space. Homes never get a letter.
- **Agent** = a window with `@agent_state` (idle/running/needs-input/failed/
  done). "Agent D" = `@agent_key` d with `@agent_state` set (trust the key
  for an agent only then). A letter is global and kept for the window's
  life, even when a shell becomes an agent or back. Agents and shells are
  reached only by ⌥W + letter (no number key), so name them by letter.
  Parked windows have no letter.
- **Reach a peer** through the tmux MCP: pane `"d"` / `tmux://d/<handle>`
  for an agent; a shell as `"cua-notch:2"` (shell 2 by window order, home
  = 1, not tmux window 2; find "shell e" with the skill's recipe);
  `read_pane` to see what it is doing, `message_agent` to tell it. Never
  send_keys into an agent's pane.
- Don't close, kill, park, move or rename tabs or spaces you don't own.
  User-facing messages go through `~/.config/tmux/scripts/notify.sh`, never
  `tmux display-message`. Test tmux only on a private `-L` server
  (`unset TMUX`, `-f /dev/null`).
- **NEEDS YOU while still working.** A turn that ends while your
  subagents, workflows or background tasks are still out keeps your tab
  "working", so a question in it is invisible. If you truly need Mack (a
  decision only he can make, a blocker), run `agent-needs-you "<what you
  need, one line>"` as your LAST tool call before ending the turn. It is
  the ONLY way to raise NEEDS YOU while work is out: nothing reads your
  closing text for questions. Don't run it for optional offers ("want me
  to also…?") or status updates, or for a turn that ends with nothing
  still out (that finish already notifies him). Main agent only: a
  subagent or workflow agent never runs it; it reports to its
  orchestrator, which decides.

## Referencing other agents and panes (`tmux` MCP server)

Other Claude sessions run in sibling tmux tabs, and the `tmux` MCP server
(`mcp__tmux__*`, HTTP on 7998, LaunchAgent `com.mackhaymond.tmux-mcp`) makes
every pane addressable by a meaningful `@`-handle. Use it instead of the
built-in `ListAgents`/`SendMessage` for anything tmux-local — those name peers
`mackhaymond-f4` and bury them under dozens of offline cloud sessions.

- **Every pane is an MCP resource**, so the user can `@`-mention one directly:
  `@tmux:tmux://main/cuanotch-hover-popup-animation`. `tmux://all` is the
  whole roster in one mention.
- **`list_panes`** first — it gives each pane's handle, location, what it's
  working on, and whether it's a Claude session. A pane reference can be a
  handle, a `tmux://` URI, `main:1.1`, or `%214`.
- **`read_pane`** on an agent returns its recent *conversation turns* (parsed
  from the session transcript) plus anything queued, not a TUI scrape — this
  is the good way to find out what a peer is actually doing.
- **`message_agent`** delivers a message into a peer's prompt by bracketed
  paste. It needs no cooperation from the receiver; if that agent is mid-turn
  Claude queues the message. Pass `wait_ms` to get its reply back. Messages are
  attributed `[message from @your-handle]` automatically.
- **`name_pane`** sets a sticky handle that survives title changes — worth
  doing for a long-running session so peers have a stable address. `whoami`
  tells you your own handle to hand out.
- `message_agent` and `send_keys` **type into live terminals**. Check
  `list_panes` before writing, and prefer `message_agent` (it refuses
  non-agent panes) over `send_keys` when talking to a Claude session.

## Launchpad catalog (mackhaymond.co)

`mackhaymond.co` is the user's launchpad for everything they host. Whenever
you deploy or stand up something with a URL — a new Worker/Pages custom
domain, a tunnel hostname, a Vercel project, a self-hosted dashboard — or
retire one, update the catalog as part of finishing the task. Creating a new
repo or project folder counts too (`npm run catalog:sync` in that repo). The procedure
lives in `~/code/projects/mackhaymond.co/CLAUDE.md`; follow it. The repo is
PUBLIC: private entries go to KV, never into the repo. Mention the catalog
update in your final summary. If you're unsure whether something belongs
(a throwaway preview, a class project), ask in one line rather than skip.

## Coding work: orchestrate, delegate, review continuously

For any coding task bigger than a small single-file change, the main agent
is the orchestrator, not the typist:

- **Write plans for subagents to execute.** Break the work into units a
  subagent can pick up cold: the goal, the files it owns, the interfaces it
  must honor, and the postcondition (tests/commands) that proves it done.
  Mark which units are independent (dispatch those together in one message)
  and which wait on others. Parallel implementers must own disjoint files or
  run with `isolation: "worktree"`, and each uses its own named pty.
- **The main agent coordinates.** Dispatch units, read the diffs that come
  back, integrate, resolve conflicts, keep the user posted. Don't take over
  the bulk edits yourself; if a result is wrong, send it back to that agent
  (SendMessage) with specifics, or re-dispatch. A subagent's "done" or
  "tests pass" is a claim, not evidence: run the unit's postcondition
  yourself before accepting it.
- **Only the orchestrator talks to the user and to git.** Subagents never
  commit or push and never ask the user directly; questions route through
  the main agent, which relays what matters from their reports. The main
  agent commits after review, staging only the files the task touched.
- **Review runs alongside the work, not after it.** As each unit lands, hand
  its diff to a reviewer subagent (fresh context, never the author) along
  with the unit's goal and plan, so it can catch code that correctly does
  the wrong thing. It hunts for bugs: correctness, edge cases, broken
  callers, error paths, races. Reviewers are read-only (fixes go back to
  the implementer, so no two agents write one file) and check each finding
  against the code before reporting it (`file:line` plus a concrete failing
  scenario), dropping what doesn't hold up. Confirmed bugs get fixed before
  dependent units build on top of them. Cap it at about two review/fix
  rounds per unit; past that, the orchestrator rules or asks the user.
  Finish with one whole-change review pass before calling the task done.
- **Scale it to the task.** A one-liner, a config tweak, or a question
  doesn't need any of this; just do it. Run only as many concurrent agents
  as the work needs (each costs battery and usage limit) and say in the plan
  how many you intend to use.
