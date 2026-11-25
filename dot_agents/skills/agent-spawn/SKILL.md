---
name: agent-spawn
description: Start another coding agent (Claude, Codex, OpenCode, Grok, Copilot, agy or abby) as a CHILD in its own background tmux tab, then wait for it, read its result, follow up or cancel it, with the `agent-spawn` CLI or the tmux MCP tools (spawn_subagent, wait_subagents, subagent_result, send_subagent, list_subagents, cancel_subagent). Load when you want a tab subagent, an external subagent, a child agent, "another harness" or "a codex/opencode/grok/copilot/agy agent" to do a piece of work; when a message starting `[agent-spawn] s-…` arrives (a child finished, asks, or closed); when your prompt starts `[task from @… · agent-spawn s-…]` (you ARE a child); or when Mack says child, kids, ↳ or subagent tab.
---

# Tab subagents (agent-spawn)

Any agent tab can start a **child**: another agent of any hooked harness in a
new background tab of its own space. The child is a full agent (its own
harness, model, permissions, context and MCP servers), Mack can watch and
answer it, and its result outlives the tab. One tool does it: the
`agent-spawn` CLI (`~/.local/bin/agent-spawn`, every harness, from a shell) or
the tmux MCP tools, which are thin wrappers over the same CLI.

Docs: `agent-spawn --help`; tmux-mcp `~/code/tmux-mcp/README.md` ("External
subagents"); tmux side `~/.config/tmux/docs/agent-tab-indicator.md` §8 and
`agent-roster.md` ("External subagents").

## Tab subagent or your own Agent tool?

| Use your in-process Agent/Task tool when | Use a tab subagent when |
|---|---|
| a quick search, read or review that only you need to see | the unit should run in a DIFFERENT harness or model (Codex for a second opinion, Claude from abby, agy, Grok…) |
| the result is all you want, not the process | Mack may want to watch it, answer its permission prompts, or talk to it |
| the work is short and you will wait for it anyway | it is long, so you can end your turn and be woken when it finishes |
| it needs your context window's knowledge passed along cheaply | it needs its own full context, tools and MCP servers, or a different directory |

A tab child costs more: each Claude child is 0.6-1.4 GB of RAM plus usage,
abby spends abliteration.ai money, and every tab is one more thing on Mack's
screen. Don't spawn for a one-liner. Only abby has a special reason to always
use it: its own Agent tool runs on its own model, never Claude.

## The two front doors

- **MCP** (Claude Code, abby and Codex: `mcp__tmux__*`; OpenCode's
  orchestrator: `tmux_*`): `spawn_subagent`, `wait_subagents`,
  `subagent_result`, `send_subagent`, `list_subagents`, `cancel_subagent`.
  The parent is always the calling tab (never an argument).
- **CLI** (every harness, including Grok, Copilot and agy): run `agent-spawn`
  in your shell. In Claude Code that is the pty `run` tool; it resolves to
  your tab even from a pty helper shell. If a sandbox refuses (it needs the
  tmux socket and localhost:7998), ask for escalation. Every verb takes
  `--parent PANE` (your pane, when auto-detection fails) and `--json`.

| MCP tool | CLI |
|---|---|
| `spawn_subagent {prompt, harness?, cwd?, model?, task?, notify?, close_on_done?}` | `agent-spawn spawn [flags] -- PROMPT` (or `-` for stdin) |
| `wait_subagents {ids?, mode?: all\|any, timeout_s?}` (default 45 s, max 540; on timeout says `still running: call again`; no ids = your live children whose latest finish is uncollected, running ones included) | `agent-spawn wait ID... [--any\|--all] [--timeout S]` (default 540) |
| `subagent_result {id, turn?}` | `agent-spawn result ID [--turn N] [--max-chars N]` |
| `send_subagent {id, message}` | `agent-spawn send ID MSG` (or `-`) |
| `list_subagents {all?}` | `agent-spawn list [--all]` |
| `cancel_subagent {id, close?}` | `agent-spawn cancel ID [--close]`, `agent-spawn close ID` |
| (whoami also says whose subagent you are) | `agent-spawn whoami` |

`--auto`, `--space` and `--force` exist only on the CLI: `spawn_subagent` has
none, so an MCP caller in a home tab or a hidden session is refused ("pass
--space NAME"); use the CLI for those.

## Spawning

```sh
agent-spawn spawn --harness codex --dir ~/code/projects/foo --task "Review parser diff" - <<'EOF'
<the brief>
EOF
# -> s-mf3k2a7x %214 @97 -        (id, pane, window, letter: children get none)
```

Flags (spawn and run; all before `--`):
- `--harness claude|abby|codex|opencode|grok|copilot|agy` (default claude;
  abby only when you mean abby).
- `--dir D`: the child's working directory. Default: your pane's folder with
  `--parent`, else the shell's cwd. Pass it explicitly from a pty shell.
- `--model M`: in that harness's own syntax (OpenCode wants `provider/model`).
- `--task T`: the one-line title (at most 80 chars) shown on the sidebar,
  the menu and in wake messages. Default: the prompt's first line.
- `--no-notify`: no wake message when it finishes (you will `wait`).
  `spawn` notifies by default; `run` never does.
- `--close-on-done`: close the tab once you have collected a done result.
- `--auto`: the harness's "don't ask" mode (claude `acceptEdits`, copilot
  `--allow-all-tools`, agy `--mode auto`, grok `--always-approve`). Off by
  default; use it only when the brief is safe to run unattended.
- `--space NAME`: open it in that space (needed when you are a space's home
  tab or in a hidden session).
- `--force`: past the depth/fan-out limits (and Codex's hook-trust check).

`agent-spawn run [same flags] [--timeout S] -- PROMPT` = spawn without a wake,
then wait, and print the result: the synchronous form. In Claude Code give
the pty `run` call a `timeout_ms` above the wait (e.g. `--timeout 500` with
`timeout_ms: 560000`), or spawn and wait in short slices instead.

## The brief

The child starts with nothing but its directory and your prompt. agent-spawn
wraps the prompt in a header (`[task from @you · agent-spawn s-…]`) and a
footer telling it it is a subagent (below), so write only the task:

```
Goal: <what and why, in Mack's terms>.
Where: <repo/dir>; files: <the files it owns>; don't touch: <others' files>.
Known: <findings, constraints, what was ruled out, interfaces to honour>.
Done means: <the test/command/visible result that proves it>.
Commit: <"don't commit" (the default) or "commit and push when done">.
Report: <what to put in the final reply: per-file summary, open questions>.
```

Be straight with the child about what the work is for. Independent units go
to separate children at once; units that touch the same files go to one
child, or one after another.

## Results and wake messages

After `spawn` you normally **end your turn**. When a child's turn ends you get
a message in your prompt (via tmux-mcp `message_agent`, attributed to the
child, so your woken turn ends quietly):

```
[agent-spawn] s-mf3k2a7x (codex child %214) 'Review parser diff' finished (done). Run: agent-spawn result s-mf3k2a7x
[agent-spawn] s-… (…) '…' finished (failed: weekly reserve). Run: agent-spawn result s-…
[agent-spawn] s-… (…) '…' closed while still working. agent-spawn result s-… still works
```

A child's **ask does not wake you**: a permission prompt or startup dialog
shows on YOUR NEEDS YOU card (Mack answers it there), and a `wait` on that
child returns `needs-input` (exit 10). Nor does a child you cancelled.

Children finishing within ~5 s of each other arrive as one message, one line
each. Then read the result (`agent-spawn result ID` or `subagent_result`),
check the work, and act. Don't poll in a loop; a wait is for when you have
nothing else to do.

**Exit codes** (CLI; the MCP tools report the same words): 0 done,
10 needs-input, 11 failed, 12 gone or cancelled, 13 refused, 75 timeout
(still running: wait again), 2 usage, 1 error (no tmux, no such id, tmux-mcp
down). `wait --all` returns at once if any child asks or fails.

**Statuses**: `done`, `needs-input` (a permission prompt or a startup dialog:
Mack answers it, or you `send` an answer if it asked in words), `failed`
(with its reason: `weekly reserve`, a limit, "not started"), `gone` (the tab
closed; the result still reads), `cancelled`, and the non-final `starting`,
`running`, `paused` (agent-pause; never counts as done).

**The result** prints `ID status · harness · reply: SOURCE`, `---`, then the
reply. `--json` gives `{id, harness, status, state, detail, reply,
reply_source, truncated, result, session_id, transcript, pane, window, task,
started, ended, turns, chezmoi_paths}`. `reply_source` is where it came from,
best first: `report` (the child's `agent-spawn report`), `reply` (its final
message, captured at turn end), `transcript`, then `screen` (a pane tail:
treat it as partial). Reading a done or failed result marks it collected (`✓` dims on
the sidebar). The reply is never cut silently; only `--max-chars` (the MCP
uses 24000) cuts it, with a `[truncated N chars]` marker.

**chezmoi paths.** A child does not commit dotfiles. A Claude, Codex or
OpenCode child's chezmoi-guard records its uncommitted chezmoi paths, and the
result ends with

```
chezmoi paths touched (uncommitted): dot_config/tmux/foo.sh, dot_local/bin/executable_bar
```

(`chezmoi_paths` in JSON; source-relative, else `~/…`). Those are now yours:
review the diff, run the postcondition, apply, and commit them with your own
work. Only a Claude, Codex or OpenCode parent's guard also takes them over
(counts them as its own); a Grok, Copilot or agy parent has just this line.
A Grok, Copilot or agy child records nothing, so its reply's own list is all
there is. Ignore other dirty paths.

## Following up, cancelling, closing

- **Follow up with `send`** (`agent-spawn send ID MSG` / `send_subagent`), not
  `message_agent`: send records a new turn, so `wait`, `result` and the wake
  track it. It also clears the child's `✓`.
- `cancel ID` sends the harness's interrupt (Escape); `--close` also closes it.
- `close ID` closes the tab through closed-tabs (⌘Z brings it back), with its
  own children when it has any (close-family). Finished
  tabs stay open (✓) until you or Mack close them; close a child once its work
  is verified and Mack doesn't need it. The registry keeps every record
  (`~/.local/state/agent-spawn/<id>/`), so `result` works after the close.
- You act only on your own children (exit 13 otherwise). After ⌘Z, an unpark
  or a snooze wake you are a new tab; your children still count as yours by
  your session id.

## Limits and refusals (exit 13)

- Depth 2: you, your child, its child. A grandchild can't spawn.
- 6 OPEN child tabs per root family (finished ✓ tabs count until closed).
- Only the seven hooked harnesses; cursor-agent and aider are hookless and
  refused, as is an uninstalled one, Codex with untrusted hooks, and Grok in
  leader mode.
- The parent must be an agent tab in a visible space: a home tab or a hidden
  session needs `--space NAME`.
- `--force` goes past the depth, fan-out and Codex-trust checks only.

## What Mack sees

- The child opens in YOUR space, right after your last child; focus never
  moves. It has **no letter** and no tab of its own in the tab bar: your tab
  shows `↳3`, and your sidebar card a folded chip `↳ 3 · 2 working`. While
  any child works your card reads working.
- Mack reaches a child by clicking that chip (it opens a row per child), in
  the prefix q / ⌥W menu (children nested under you; arrows and Enter), or
  from **your** NEEDS YOU card: a child's permission prompt or failure shows
  there, titled with your name, and jumping lands on the child's pane.
- A child never gets NEEDS YOU of its own, so if a child needs a decision only
  Mack can make, YOU bring him in (in Claude Code, `agent-needs-you` as your
  last call when work is still out).
- Closing a child tells you (`… closed`) only when it was busy or its result
  unread, and never when you closed it yourself or you close in the same
  batch (a space close). If your agent exits, your children
  are cut loose (orphans, `↳ (parent gone)`); closing you by hand asks Mack
  what to do with them; parking or snoozing you takes them along, and a
  working child refuses the whole park (as a busy parent does).

## When you are the child

Your prompt starts with `[task from @handle · agent-spawn s-…]` and ends with
the subagent footer (env `AGENT_ROLE=child`, `AGENT_SPAWN_ID`;
`agent-spawn whoami` says whose child you are). Then:

- **Your final reply is your report.** Make it complete and self-contained:
  what you did, per file, how you verified it, what is left.
- **Never ask Mack anything and never run `agent-needs-you`** (it refuses
  children). Put questions and blockers in your final reply; your parent
  decides whether to bring Mack in. Permission prompts still reach him
  through your parent's card.
- **Don't commit or push** unless the brief says to. Dotfile (chezmoi) edits:
  edit the source, don't apply unless told, and leave them uncommitted. As a
  Claude, Codex or OpenCode child your guard records the paths for your
  parent (it won't demand a commit); as a Grok, Copilot or agy child nothing
  does, so list them in your reply (do that anyway: it costs nothing).
- Optional: `agent-spawn report --status ok|blocked|failed --file F` (or `-`,
  or text) hands back a structured report that wins over your final message.
- You may spawn children of your own (depth 2 allows one more level); your
  parent's limits count them.
- A follow-up from your parent arrives as a new prompt; answer it the same way.
