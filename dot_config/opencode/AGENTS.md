# Global OpenCode Rules

## Dotfile changes (chezmoi-managed)

This machine's dotfiles are managed by chezmoi. The source repo is at
`~/.local/share/chezmoi`. Files like `~/.zshrc`, `~/.gitconfig`, `~/.config/**`
are RENDERED outputs — direct edits get reverted on the next `chezmoi apply`.

When you need to change a managed dotfile:

1. Edit the source via `chezmoi edit --apply <live-path>` (auto-applies on
   save), OR open the source directly under `~/.local/share/chezmoi/`.
2. After all your edits across all files are done, run `chezmoi apply` if
   you bypassed `--apply` at any point.
3. Inspect `git status`, `git diff`, and `git log --oneline -10` in the
   chezmoi source repo.
4. Stage only the intended files, commit with a concise message, and push.
   Do this automatically at the end of dotfile-editing tasks without asking.

### Hard rules — never violated

- **Never** edit a chezmoi-managed live file directly via `Edit`/`Write`/
  `apply_patch`/`multiedit`, or via shell redirection / `tee` / `sed -i` /
  `perl -i` / `cp` / `mv` in `bash`. Use the chezmoi flow above. The
  `chezmoi-guard` plugin will block these attempts; treat the block as a
  guidepost, not an obstacle to circumvent. Read-only inspection of managed
  files (`cat`, `grep`, `sed -n`, `awk '{print}'`, `perl -ne`) is allowed.
- **Never** use destructive or history-rewriting git operations in the chezmoi
  repo without explicit user approval. Only `git add`, `git commit` (no
  `--amend`), non-force `git push`, and read-only git (`status`, `diff`, `log`,
  `stash list`/`show`) are permitted there. Other agents routinely have
  uncommitted work in the same tree, so `git restore`, `git checkout -- <path>`,
  `git checkout .`, `git clean -f/-d/-x`, and `git stash` destroy or hide work
  that is not yours.
- **Never** stage unrelated changes. Review the diff first and stage only the
  files that belong to the current task.

### Automatic chezmoi commits

Agents editing dotfiles should commit and push their own completed changes in
`~/.local/share/chezmoi` without asking. Before committing:

- Run `git status` to see the full working tree state.
- Run `git diff` to inspect unstaged changes.
- Run `git log --oneline -10` to match local commit style.
- Stage only intended files.
- Commit with a concise message that describes the dotfile change.
- Push after a successful commit.

Do not use `--no-verify`, bypass hooks, force-push, amend, rebase, reset, or
merge unless the user explicitly asks for that specific operation.

#### What the plugin checks for unsafe git attempts

The guard catches destructive/history-rewriting git intent across all of these
vectors:

- `git -C <chezmoi-src> <hazard>` where hazard is any of: `reset`, `rebase`,
  `merge`, `restore`, `checkout -- <path>` / `checkout <path>` / `checkout .` /
  `checkout <treeish> -- <path>` / `checkout <treeish> .` / `checkout -f` /
  `checkout -p` (plain branch switching `checkout main` / `-b x` / `HEAD~1` is
  allowed), `switch -f` / `--force` / `--discard-changes` (bare `switch <branch>`
  / `switch -c x` is allowed), `clean -f/-d/-x`
  (`clean -n` / `--dry-run` / `-nd` is allowed), `stash` push/pop/drop/apply/clear (`stash list` and
  `stash show` are allowed), `commit --amend`, and force pushes
  (`--force`, `--force-with-lease`, `-f`, `--mirror`, `+ref`)
- `git --git-dir=<chezmoi-src>/.git <hazard>` and `--work-tree=<chezmoi-src>`
- `chezmoi git -- <hazard>`
- `cd <chezmoi-src> && git <hazard>` (and subshell `(...)` / `pushd` variants)
- **Bash-tool `workdir` parameter set to chezmoi-src** + a git write-verb in
  the command (e.g. `bash(workdir="~/.local/share/chezmoi", command="git reset")`).
  This bypass would otherwise sneak past every detector that scans the
  command string only — the workdir is supplied by the tool wrapper, not
  the command shell.

The guard also tracks chezmoi source files written by the current opencode
session. If those same session-touched paths are still dirty (uncommitted or
unpushed) when the ROOT session becomes idle at turn end, it submits a
synthetic follow-up prompt so the agent continues and applies, inspects,
stages, commits, and pushes before it actually stops, and it shows a toast so
the user can see why the agent resumed. The synthetic prompt also instructs
the agent to re-print any final summary or user-facing text it had already
output before the guard fired. Before a later model turn, it also injects a
reminder with the same instruction. Writes made by a subagent are attributed
to its root session (the continuation is never driven into a subagent). The
continuation fires at most 3 times per session, at most once per 2 minutes;
after that the session is allowed to stop and only the reminder remains. This
check is path-scoped to files written in the current session so simultaneous
agents do not complain about unrelated chezmoi changes they did not make.
Agents should ignore chezmoi dirty paths that appear unrelated to their own
work; those are very likely from another concurrent agent.

Debug logs for this behavior are written to
`~/.local/share/opencode/chezmoi-guard.log` (denies, continuations, managed-set
refreshes and errors; rotated to `.1` at 1 MB; set `CHEZMOI_GUARD_DEBUG=1` in
opencode's environment for per-tool-call trace lines).

## Mack's tmux workspace: spaces, shells, agents

One tmux server, seen through WezTerm with the agent sidebar on the right
(⌘B). Mack talks in these terms, not tmux's; the `tmux-workspace` skill
(`~/.agents/skills/tmux-workspace/SKILL.md`) has the full vocabulary table,
read-only recipes and keys, so load it whenever they name a space, shell,
agent, letter, parked tab or NEEDS YOU.

- **Space** = a tmux session not marked `@space_hidden`, numbered ⌃N by
  `@space_index` (main = 1); it lasts until closed (prefix Q). Hidden
  sessions (agents, tasks, stash, scratch, btop-popup) are never spaces;
  `stash` holds parked tabs.
- **Shell** = a window of a space without `@agent_state`, numbered ⌥N per
  space in window order among shells; shell 1 is the uncloseable **home**.
  Written `<space>:<N>`: `cua-notch:2` is shell 2, not tmux window 2.
- **Agent** = a window with `@agent_state` (idle/running/needs-input/failed/
  done; Claude, Codex and OpenCode tabs, this one included). Its
  **letter** (`@agent_key`, valid only while `@agent_state` is set) is
  global and stable for its life: "agent D" = key d, any space (⌥W d). ⌘N
  is the Nth agent of the current space and shifts, so name agents by
  letter. Parked agents have no letter.
- **Reach a peer** through the shared `tmux` MCP server (pane `"d"` /
  `tmux://d/<handle>` for an agent, `"cua-notch:2"` for a shell). It is
  wired into OpenCode as `tmux_*` tools (`tmux_list_panes`,
  `tmux_read_pane`, `tmux_message_agent`, `tmux_name_pane`, `tmux_whoami`,
  `tmux_send_keys`; the orchestrator has them, subagents don't). Start with
  `tmux_list_panes`; prefer `tmux_message_agent` (it refuses non-agent
  panes) over `tmux_send_keys`. Peers see this OpenCode tab as an agent with
  a letter, so they can `message_agent` you. If the tools are missing,
  call the same server from the shell:
  `python3 ~/.agents/skills/tmux-workspace/scripts/tmux-mcp-call.py
  read_pane '{"pane": "d"}'` (also `list_panes`, `message_agent`). Read-only
  `tmux list-windows -a -F …` always works. Never type into an agent's pane.
- Don't close, kill, park, move or rename tabs or spaces you don't own.
  User-facing messages go through `~/.config/tmux/scripts/notify.sh`, never
  `tmux display-message`. Test tmux only on a private `-L` server
  (`unset TMUX`, `-f /dev/null`).

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
