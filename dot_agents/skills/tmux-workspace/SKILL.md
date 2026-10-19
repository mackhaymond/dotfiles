---
name: tmux-workspace
description: How Mack's tmux workspace is organized (spaces, homes, shells, agents, the letters agents and shells share, the sidebar, parking/stash, NEEDS YOU) and how to find and reach any pane in it. Load whenever the user says things like "agent D", "shell E", "tell S to…", "ask the sidebar agent", "shell 2 in cua-notch", "the home shell", "space 3", "⌃3/⌘4/⌥W e", "park it", "unpark", "it's in needs-you", "the sidebar", or asks you to message, check on, find, or read another agent, tab, or pane, or to say which key to press.
---

# Mack's tmux workspace

One tmux server (the default socket) holds everything. Mack sees it through
WezTerm: the tmux client on the left, the **sidebar** on the right. They think in
**spaces**, **shells** and **agents**, not in tmux sessions and windows. This
skill maps their words to panes, and says how to reach a pane without asking them
or another agent.

Authoritative docs (chezmoi source, read before changing behaviour):
`~/.local/share/chezmoi/dot_config/tmux/docs/` (`spaces.md`, `agent-roster.md`,
`nav.md`, `agent-tab-indicator.md`, `kill-guard.md`, `agent-pause.md`), keys in
`dot_config/tmux/spaces.conf` (the `── keys ──` section) and `tmux.conf.tmpl`.

## Mack's own words

<!-- The user will dictate their mental model here. -->

Questions they can answer out loud to fill this in:

1. What is a space to you: a project, a context, a day of work? When do you
   open a new one instead of working in main?
2. What are your shells for (dev servers, git, scratch, nvim)? May an agent
   run commands in one of your shells, or only in its own?
3. When you say "agent D" or "tell S to…", what do you expect: a message in
   its prompt, a summary of what it's doing, or that I hand it the task?
4. What does parking mean to you ("later", "done but keep", "don't touch")?
   Should an agent ever park, close or move a tab on its own?
5. When should one agent message another instead of asking you, and is there
   anyone who must never be interrupted?

## The model

```
tmux server (default socket)
├── space  = a tmux session WITHOUT @space_hidden        "space 3", "cua-notch", ⌃3
│   ├── home    = window @space_home=1 (lowest); the space "home", "the home shell", ⌃3
│   │            itself (dashboard pane on top, the home shell pane below; no letter)
│   ├── shells  = every other window WITHOUT @agent_state "shell E" (⌥W e), cua-notch:2
│   └── agents  = windows WITH @agent_state               "agent D" (⌥W d), ⌘1-9
└── hidden sessions (@space_hidden=1): agents tasks stash scratch btop-popup
    (no number, no home, never shown as spaces; `stash` holds parked tabs,
     `agents` holds the pty MCP helper shells)
```

- **Space**: a tmux session, numbered by the session option `@space_index`
  (`main` is always 1, the rest follow the registry order) and reached with
  ⌃N. Coloured by `@space_color`. Usually a project folder (`session_path`);
  `main` is `~`. A space lasts until it is deliberately closed (prefix Q).
  Closing all its tabs never closes it, because its home cannot be closed.
  The registry (`~/.local/state/tmux-spaces/spaces.tsv`) recreates spaces
  after a restart.
- **Home**: every space has exactly one, its lowest window, named `home`.
  A space IS its home: ⌃N lands there (never on the last-active window;
  ⌃N for the space you are in goes home), and so does a click on the
  space's chip in the status line, its header in the sidebar or its row in
  the sidebar's dock. It holds a 9-row dashboard pane on top (name, branch,
  agents with their letters; never focused) and the home shell pane below
  (`@space_home_pane`). It has no letter, no shell tab and no sidebar row of
  its own (the space stands for it). It cannot be closed by hand and
  respawns if its shell exits.
- **Shell**: any other window of a space without `@agent_state`. Each has a
  **letter** from the agents' pool (below), and Mack names shells by it:
  "shell E" is the shell whose `@agent_key` is `e`, reached with ⌥W e.
  For addressing there is also the ordinal form `<space>:<N>`: shells
  numbered per space, 1-based, in `window_index` order among shells only,
  home = 1, so `cua-notch:2` = "shell 2 of cua-notch". tmux-mcp reads it
  the same way. **For the tmux CLI it is NOT the target `cua-notch:2`**,
  which is window index 2 and may be an agent. Resolve it with the recipes
  below before you hand it to `tmux`.
- **Agent**: a window with a non-empty `@agent_state` (`idle`, `running`
  (Mack says "working"), `needs-input`, `failed`, `done`). Claude, Codex and
  OpenCode tabs are agents from birth; `@agent_kind` =
  `claude`/`codex`/`opencode`, `@agent_summary` = the title the sidebar shows
  (`project/Title`). A shell where someone starts claude, codex or opencode
  becomes an agent.
  - **Ordinal** (⌘N): position among the agents of ONE space, by
    `window_index`. It shifts when tabs open or close, so it is good for
    keys and bad as a name. Name agents by their letter.
- **Letter** (`@agent_key`): ONE pool for agents and shells, GLOBAL across
  spaces, and kept for the window's life: a shell where claude starts keeps
  its letter as an agent, and an agent that exits back to a shell keeps it.
  The alphabet is handed out home row first: `a s d f g h j k l q w e r t y
  u i o p z x c v b n m`, in display order (spaces in ⌃N order; in each,
  its agents by window index, then its shells). Every agent (not parked,
  not in a hidden session) and every non-home shell of a space gets one;
  homes never, parked windows never, and past 26 there are none. Parking
  frees the letter: an unparked window gets it back only if nobody took it.
  Closing frees it at once. The sidebar and menu assign letters, so a
  brand-new window can be letterless for a moment (`·` on its status-line
  tab). Always lowercase in tmux. "Agent D" = `@agent_key` `d` with
  `@agent_state` set; "shell E" = `@agent_key` `e`, no `@agent_state`, not
  a home. tmux-mcp and the agent recipes trust `@agent_key` as an AGENT's
  only while `@agent_state` is set, so tmux-mcp's pane `"e"` never finds a
  shell: use recipe **letter**.
- **Tab / window / pane**: Mack's "tab" is a tmux window. A pane is a split
  inside one. "Window" may also mean a macOS/WezTerm window, so ask yourself
  which they mean.
- **Global vs per-space**: letters (agents' and shells'), NEEDS YOU, the
  parked stash, back/forward history and the sidebar's spaces list are
  global. Agent ordinals (⌘N), shell ordinals (`<space>:<N>`) and home are
  per space.

## The sidebar (agent-ui)

A WezTerm split on the right (⌘B toggles it). While it is open it is the
whole UI and the tmux status line is off. Top to bottom:

- a usage panel (meters), with a red `prefix` / peach `⌥W jump` tab while a
  key table is armed; toasts (`notify.sh`) appear as stacked cards over the
  top of the sidebar, newest lowest, until they expire or are clicked;
- **NEEDS YOU**: agents in `failed` > `needs-input` > `done` (finished and
  not seen yet), oldest first, the order `agent-jump.sh list` prints. Each
  card starts with the agent's letter badge;
- **AGENTS**, grouped by space (scope "all spaces"), or only the current
  space ("this space", ⌥V toggles). A space's header IS its home: a click
  goes home, and while Mack is on home the header is lifted (lavender tint,
  lavender right edge). Under it each agent is a two-line tile with its
  letter badge; a working agent's second line is a live activity blurb
  (`› Running cargo test`, `› Editing layout.rs`, read from its transcript),
  after it finishes the start of its reply. Then the space's non-home
  shells, one row each: letter cap, a faint terminal glyph (nf-fa-terminal,
  U+F120), the folder (at a prompt) or the running command; Mack's own
  shell lifted like his own agent card. With little room, 2+ shells fold
  into one line of pills. In all-spaces scope a space with only shells
  still gets a group;
- the dock, **SPACES**: one row per space in ⌃N order (`N name`, its agent
  counts; a click lands on its home), the current one a solid chip in its
  colour (red while the prefix is armed);
- **PARKED** and its count (the stash; a click opens the unpark picker), with
  up to 3 parked tabs listed when there is room.

A right-click on an agent row parks it. "It's in needs-you" means
`@agent_state` is `needs-input`, `failed` or `done` and the window is in a
visible space.

**Parked / stashed**: the window was moved into the hidden `stash` session
(`@stash_origin` remembers its space). A parked Claude agent is SIGTERMed
and resumed with `claude --resume` on unpark, so its process is gone while
parked. It has no letter, isn't messageable, and isn't listed by
`list_panes` unless `include_hidden`. Never "fix" a parked tab yourself.

## What Mack says → what it means → how to resolve it

| They say | Means | Resolve |
|---|---|---|
| "agent D", "D", "tell D…" | the agent whose `@agent_key` is `d`, in any space (a bare "D" may be a shell's letter: recipe **letter** says which) | tmux MCP pane `"d"` (or `tmux://d/<handle>`); CLI: recipe **letter** |
| "shell E", "E" (a shell) | the non-home shell whose `@agent_key` is `e`, no `@agent_state`, in any space | recipe **letter**, then tmux MCP by its pane id (`"%131"`); tmux-mcp's `"e"` finds agents only |
| "the sidebar agent", "the tmux-mcp agent" | an agent by its title/topic | `list_panes` filter, or grep `@agent_summary` in recipe **map** |
| "agent 3", "⌘3" | 3rd agent of the space Mack is looking at | recipe **where is Mack**, then **ordinal**; then address it by its letter |
| "space 3", "⌃3" | session with `@space_index` 3 | recipe **space** |
| "the cua-notch space" | session named `cua-notch` | `list_panes` filter `cua-notch`; CLI: `tmux has-session -t '=cua-notch'` |
| "the home shell", "home", "⌃3" (as a place) | home window of that space (their current one if unsaid); the shell pane, not the dashboard | tmux MCP pane `"cua-notch:1"`; CLI: recipe **shell N** with N=1 |
| "shell 2 in cua-notch", `cua-notch:2` | 2nd non-agent window of cua-notch, home being the 1st (the ordinal form; Mack usually says the letter) | tmux MCP pane `"cua-notch:2"` (`tmux://cua-notch:2/<handle>`); CLI: recipe **shell N** |
| "my shell", "the shell I'm in" | the window their client shows, if it's a shell | recipe **where is Mack** |
| "it's in needs-you", "it's waiting on me" | state `needs-input`/`failed`/`done` | `agent-jump.sh list` |
| "park it", "stash it" | prefix H on that tab (or right-click it in the sidebar) | they do it; you don't, unless asked |
| "unpark", "bring it back" | prefix h (picker), or the sidebar's PARKED section | `stash.sh list` shows what's parked |
| "close the space" | prefix Q (y/n; every tab goes through closed-tabs, ⌘Z reopens) | never main, never hidden ones |
| "this space" / "all spaces" (sidebar) | the scope, ⌥V | `tmux show -gv @strip_scope` |
| "the menu" | the agent menu: prefix q | |
| "the dashboard" | the top pane of a home window | |

## Recipes (read-only; tested on the live server 2026-10-16, the shell-letter forms of **map** and **letter** written 2026-10-19 from the config, not yet run live)

Your shell is INSIDE tmux and `$TMUX` points at the real server, so a plain
`tmux` reads it. In zsh, always quote exact-match targets: `-t '=main'`
(a bare `=main` is zsh path expansion and aborts the line).

**map**: every space, shell and agent, with shell ordinals (`shN`, home =
1), ⌘N ordinals (`cmdN`) and letters (agents' and shells'; a home shows
`-` even if a stale key sits on it):

```sh
tmux list-windows -a -F '#{?#{@space_hidden},-,#{@space_index}}	#{session_name}	#{window_index}	#{window_id}	#{pane_id}	#{?#{@agent_state},agent,shell}	#{?#{@space_home},home,}	#{@agent_key}	#{@agent_state}	#{@agent_summary}' |
awk -F'\t' '$1!="-" { if ($2!=s) {s=$2; a=0; h=0}
  o = ($6=="agent") ? "cmd" (++a) : "sh" (++h)
  printf "space %s %-10s %-5s win %s %s pane %s  %-6s %s %-11s %s\n", $1, $2, o, $3, $4, $5, ($6=="agent"?"agent":($7=="home"?"home":"shell")), ($7=="home" || $8=="" ? "-" : $8), $9, $10 }'
```
```
space 4 cua-notch  sh1   win 1 @34 pane %35  home   -
space 4 cua-notch  cmd1  win 2 @121 pane %122  agent  f idle        cua-notch/Pause-resume agent
space 4 cua-notch  sh2   win 3 @130 pane %131  shell  g
space 1 main       cmd3  win 4 @4 pane %5  agent  d running     tmux/Tmux Agent Sidebar
space 3 schedule   cmd1  win 2 @9 pane %10  agent  k idle        math_econ_sched/P0a multi-user execution
```

**letter**: which pane is agent D, or shell E? (field 6 says which kind;
homes and hidden sessions never match)

```sh
L=d; tmux list-windows -a -F '#{@space_hidden}|#{@agent_key}|#{session_name}:#{window_index}|#{window_id}|#{pane_id}|#{?#{@space_home},home,#{?#{@agent_state},agent,shell}}|#{@agent_state}|#{@agent_summary}' |
  awk -F'|' -v l="$L" '$1=="" && $2==l && $6!="home"'
# L=d → |d|main:4|@4|%5|agent|running|tmux/Tmux Agent Sidebar   (agent D, pane %5)
# L=g → |g|cua-notch:3|@130|%131|shell||                        (shell G, pane %131)
```

For "agent D" only, add `&& $6=="agent"`; for "shell E", `&& $6=="shell"`.

**shell N**: shell N of space S by ordinal (home = 1; prints nothing if there is no such shell):

```sh
S=cua-notch N=1; tmux list-windows -t "=$S" -F '#{window_index} #{window_id} #{pane_id} #{?#{@agent_state},a,s} #{window_name}' |
  awk -v n="$N" '$4=="s" && ++k==n'
# → 1 @34 %35 s home       (%35 is the home SHELL pane; the dashboard is %145, pane 1)
```

**ordinal**: agent ⌘N of space S: same as **shell N**, with `$4=="a"`.

**space**: space N → session: `tmux list-sessions -F '#{@space_index} #{session_name} #{session_path}' | awk -v n=3 '$1==n'`
→ `3 schedule /Users/mackhaymond/math_econ_sched`

**where is Mack** (the client's space and tab; `table` is `prefix`/`jump` while a key is half-typed):

```sh
tmux list-clients -F '#{client_name} space=#{@space_index} #{session_name}:#{window_index} #{window_id} key=#{@agent_key} table=#{client_key_table}'
# → /dev/ttys000 space=1 main:7 @0 key=j table=root
```

**where am I**: Claude Code and Codex: `mcp__tmux__whoami`; OpenCode:
`tmux_whoami`. Without the MCP:
`tmux display -p -t "$TMUX_PANE" '#{session_name} space=#{@space_index} #{window_index} key=#{@agent_key}'`.
If that says session `agents`, you are in a pty MCP helper shell, not your own
tab: use whoami.

**needs you**: `~/.config/tmux/scripts/agent-jump.sh list`
(`win_id session index state since label`, best first; empty = nothing).
**parked**: `~/.config/tmux/scripts/stash.sh list`.
Hidden-ness of a session: `tmux display -p -t '=stash' '#{@space_hidden}'`
(`show -v @space_hidden` errors when it is unset).

## Reaching a peer

1. **tmux MCP first; it speaks Mack's vocabulary** (Claude Code and Codex
   have `mcp__tmux__*`; OpenCode's orchestrator has `tmux_*`). Its
   resources and pane references use the sidebar's names:
   - `tmux://d/<handle>`: agent D (the letter, global; `list_panes` prints
     `[d] @handle`). A bare `"d"` (either case) also works as a pane
     reference. Mack can `@`-mention `@tmux:tmux://d/tmux-agent-sidebar`.
   - `tmux://cua-notch:2/<handle>`: shell 2 of cua-notch. As a pane
     reference, `"cua-notch:2"` means the shell ordinal in a space (home =
     1). Only when that space has no shell N does it fall back to tmux's
     window index 2, which may be an agent, so check the answer's `at`
     location. Mack names shells by letter ("shell E"), which tmux-mcp
     does not resolve (its letters are agents' only): find the shell with
     recipe **letter** and pass its pane id (`"%131"`).
   - `tmux://main/<handle>`: an agent without a letter yet.
     `tmux://stash:3/<handle>`: a hidden session, by window index.
   - Also accepted: a handle, `main:4.1` (the exact tmux location, pane
     included), or `%5`. A bare number is a window index and is ambiguous
     across spaces, so avoid it.
   - A lettered URI whose handle is gone does NOT fall back to the letter,
     because letters are reused. When a reference fails, run `list_panes`
     again.

   Then `list_panes` (`agents_only`, `filter`, `include_hidden` for stash),
   `read_pane` (its recent conversation turns), and `message_agent` (a
   bracketed paste into its prompt, queued if it is mid-turn, attributed
   `[message from @you]`, refused for non-agent panes).
2. **No tmux MCP tools** (a client without it configured, an OpenCode
   subagent, or the tools failed to load): call the same server from the
   shell with the bundled helper. The token is read from
   `~/.local/state/tmux-mcp/token` and never goes on the command line:
   ```sh
   H=~/.agents/skills/tmux-workspace/scripts/tmux-mcp-call.py
   python3 $H list_panes '{"agents_only": true, "filter": "cua-notch"}'
   python3 $H read_pane '{"pane": "d"}'
   python3 $H message_agent '{"pane": "d", "message": "…"}'
   ```
   It needs localhost network and file access. If a sandbox refuses, ask
   for escalation. Server down: `launchctl kickstart -k gui/$(id -u)/com.mackhaymond.tmux-mcp`.
3. **Read-only tmux** always works for looking: the recipes above, and
   `tmux capture-pane -p -J -S -40 -t %5` for a screen tail.
4. **Never type into an agent's pane** with `send-keys`/`send_keys` as a
   substitute for `message_agent`. In particular, never type into a busy one
   (`running`) or one in `needs-input`, where a keystroke answers its
   permission dialog. In a shell, type only when Mack asked for it and the
   pane is at a prompt (`#{pane_current_command}` is zsh/bash).
5. Say who you are and what you need in one message, and don't poll. Use
   `wait_ms` if you need the reply.

## How Mack drives it (tell them which key to press)

Prefix is **⌃S**. ⌘ chords come from WezTerm.

| Key | Does |
|---|---|
| ⌃1-9 | space N's home (by `@space_index`); in the space you're in, back home |
| ⌘1-9 | agent N of this space (ordinal) |
| ⌥1-9 | nothing of tmux's: they reach the program in the tab (shells go by letter, ⌥W) |
| ⌥L / ⌥H | next / previous of the same kind (agent→agent, shell→shell), wrapping |
| ⌥\` | agent ↔ last-visited shell of this space |
| ⌥W then a letter | jump to that agent or shell, any space; never a home (Esc cancels; a miss says "jump: nothing on x") |
| ⌥S | next tab that needs you (CuaNotch owns it; prefix d is tmux's) |
| ⌥X / ⌥C | back / forward through visited tabs (prefix D undoes a whole run of ⌥S jumps) |
| ⌥N | new space (folder picker; reuses a space already on that folder) |
| ⌥M | move this tab to another space |
| ⌥V | sidebar scope: this space ↔ all spaces |
| ⌘B | sidebar on/off |
| prefix q | agent menu (lists agents only; letters jump, a shell's too; Tab filters; Space then s park / x close) |
| prefix H / prefix h | park this tab / unpark (picker) |
| prefix v | select a range of tabs (h/l extend, H or ⏎ park, m move, Esc) |
| prefix Q | close this space (y/n; never main) |
| ⌘T / ⌘R | new shell tab / new claude tab (prefix c / prefix k) in THIS space, in its folder |
| ⌘⇧T / ⌘⇧R | picker: which space gets a new shell / claude tab (at its end; prefix C-t / prefix K). Type a folder to open or reuse a space for it |
| ⌘⌥R | replace this tab with a claude tab (prefix C-k; on home it adds one) |
| ⌘W / ⌘Z | close tab (refused on home) / reopen the last closed tab, agent resumed |
| prefix P | pause every working agent before closing the laptop; again to resume that set |
| prefix C-s | save the layout now (resurrect) |

## Do not

- Close, kill, park, move or rename windows, sessions or spaces you don't
  own. That covers `kill-window`, `kill-session`, `rename-session`,
  `move-window`, `stash.sh` and `space.sh close`. Spaces and their homes are
  Mack's, and the registry follows renames.
- Use `tmux display-message` for anything Mack should see. The status line is
  hidden behind the sidebar, so it reads as a glitch. Use
  `~/.config/tmux/scripts/notify.sh [-c CLIENT] [-k info|ok|warn|error] -- TEXT`
  (a sidebar toast, falling back to display-message on its own).
- `set-option` on the real server, unless that is your task and the docs say
  how.
- Kill the default server. `kill-server` there is behind Touch ID and a
  verified save (kill-guard). Treat it as forbidden.
- Test tmux against the real server. Use a private one, with nothing leaking
  from the real one:
  `unset TMUX; export TMUX_TMPDIR=<scratch dir>; tmux -L <name> -f /dev/null …`,
  then kill exactly that server (`tmux -L <name> kill-server`). Never a bare
  `kill-server`, and never a test server that loaded the real tmux.conf with
  resurrect/continuum on (it can resume every Claude session).
- Run `space.sh` outside tmux. With `$TMUX` empty it refuses, because a
  plain `tmux` would hit whatever the default server is.
