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

- Never `open`/`open -a`/`open <url>`, AppleScript activate, bring_to_front,
  or `yabai --focus` — launch with `cua-bg-launch`, drive with `mcp__cua__*`
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
- **One command per pty at a time.** Concurrent `run`s on one pty are
  serialized (they queue), and a busy pty refuses new runs. Parallel work
  — especially concurrent subagents, which share this session's ptys —
  must each use their own named pty: pass `pty: "<name>"` to `run`
  (auto-creates). Never `send_keys` a command line into a busy pane.
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
  death: every window lingers for a ~1h grace period, and if the user
  resumes the session (`claude --resume`) within it, ALL its PTYs are
  adopted back automatically — the main shell with its cwd/env intact,
  plus any spawned ones. **Check `list_ptys` at the start of any session
  that might be a resume** — adoption can take a few seconds after session
  start, so if prior shell state is expected and the list is empty, wait
  briefly and check once more before creating new state.
- **Cleanup is still your job while working**: when a spawned process is no
  longer needed — task done, server no longer under test — `kill_pty` it
  right away; the grace sweep is a slow backstop (orphans reaped after ~1h),
  not your cleanup plan, and every live PTY clutters the user's
  `tmux attach -t agents` view.
- `spawn_pty(..., persist: true)` opts a PTY out of reaping entirely —
  it survives indefinitely until someone kills it. Use it only for
  processes genuinely worth keeping (a dev server mid-debugging), and
  still `kill_pty` it once its purpose is over.
- stdout/stderr are merged (real PTY); a timed-out `run` keeps running —
  `read_pty` to check, `send_keys ["C-c"]` to cancel.
- In zsh, quote `'=agents'` in hand-written tmux targets — an unquoted
  leading `=` triggers zsh command-path expansion and aborts the line.

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
