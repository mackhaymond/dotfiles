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
  `cp` / `mv` in `bash`. Use the chezmoi flow above. The `chezmoi-guard`
  plugin will block these attempts; treat the block as a guidepost, not an
  obstacle to circumvent.
- **Never** use destructive or history-rewriting git operations in the chezmoi
  repo without explicit user approval: no `git reset`, `git rebase`,
  `git merge`, `git push --force`, `git push --force-with-lease`, or `git push -f`.
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

- `git -C <chezmoi-src> reset/rebase/merge/push --force`
- `git --git-dir=<chezmoi-src>/.git reset ...` and `--work-tree=<chezmoi-src>`
- `chezmoi git -- reset/rebase/merge/push --force`
- `cd <chezmoi-src> && git reset` (and subshell `(...)` / `pushd` variants)
- **Bash-tool `workdir` parameter set to chezmoi-src** + a git write-verb in
  the command (e.g. `bash(workdir="~/.local/share/chezmoi", command="git reset")`).
  This bypass would otherwise sneak past every detector that scans the
  command string only — the workdir is supplied by the tool wrapper, not
  the command shell.

The guard also tracks chezmoi source files written by the current opencode
session. If those same session-touched paths are still dirty when the session
becomes idle at turn end, it
submits a synthetic follow-up prompt so the agent continues and applies,
inspects, stages, commits, and pushes before it actually stops, and it shows a
toast so the user can see why the agent resumed. The synthetic prompt also
instructs the agent to re-print any final summary or user-facing text it had
already output before the guard fired. Before a later model turn, it also
injects a reminder with the same instruction. This check is path-scoped to
files written in the current session so simultaneous agents do not complain
about unrelated chezmoi changes they did not make. Agents should ignore
chezmoi dirty paths that appear unrelated to their own work; those are very
likely from another concurrent agent.

Debug logs for this behavior are written to
`~/.local/share/opencode/chezmoi-guard.log`.

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
