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
3. **STOP and ask the user** whether to commit & push the chezmoi repo.
   Summarize what changed.

### Hard rules — never violated

- **Never commit or push** in `~/.local/share/chezmoi`. Not via `git commit`,
  `git push`, `chezmoi git -- commit`, or any equivalent. The user always
  commits dotfile changes themselves at the end of a task.
- **Never** edit a chezmoi-managed live file directly via `Edit`/`Write`/
  `apply_patch`/`multiedit`, or via shell redirection / `tee` / `sed -i` /
  `cp` / `mv` in `bash`. Use the chezmoi flow above. The `chezmoi-guard`
  plugin will block these attempts; treat the block as a guidepost, not an
  obstacle to circumvent.
- **Never** "comply with the letter while violating the spirit." Examples
  of what is NOT acceptable: routing an edit through `chezmoi edit --apply`
  and then immediately running `git commit && git push` yourself.

### When the user gives explicit go-ahead

If the user says "commit it" / "go ahead" / "yes" in direct response to your
ask, you may run the commit (still no `--no-verify`, no force-push to main).
Otherwise default to ASK and stop.

The `chezmoi-guard` plugin refuses `bash` commits/pushes targeting the
chezmoi source repo by default. To run a commit the user has explicitly
approved, use the **two-call escape hatch**:

```bash
# Call 1 — mint a single-use approval token (writes ~/.cache/chezmoi-guard/approve-nonce)
chezmoi-approve-commit

# Call 2 — commit (in a SEPARATE bash invocation)
CHEZMOI_COMMIT_OK=approved git -C ~/.local/share/chezmoi commit -m "..."
CHEZMOI_COMMIT_OK=approved git -C ~/.local/share/chezmoi push
```

`chezmoi-approve-commit` is a script at `~/.local/bin/chezmoi-approve-commit`
(rendered from `dot_local/bin/executable_chezmoi-approve-commit`). It works
in any shell; a function-based helper would not, because non-interactive
shells skip `~/.zshrc` and the agent's bash never sees it.

The plugin validates approval by **file presence + 5-min TTL**, not by
matching the value of `CHEZMOI_COMMIT_OK`. The value is opaque — any
non-whitespace token works as the attestation prefix. Within the 5-min
window, the same approval covers any number of commits/pushes; after
expiry the agent must run `chezmoi-approve-commit` again.

Why two calls (not one): the plugin inspects the literal bash command
string BEFORE shell expansion, so a one-liner like
`CHEZMOI_COMMIT_OK=$(chezmoi-approve-commit) git commit` would only show
the plugin `$(chezmoi-approve-commit)` as text — never the actual nonce,
and the nonce file wouldn't exist yet anyway. Two calls is enforced by
the architecture.

Use this ONLY when you have direct, explicit user approval for THIS specific
commit/push. Minting a nonce IS a security action; doing so without the
user's go-ahead is a hard-rule violation.

#### Constraints

Regex (in `dot_config/opencode/plugins/chezmoi-guard.ts` — keep in sync):

```
^\s*(?:(?!CHEZMOI_COMMIT_OK=)(?:export\s+)?[A-Za-z_]\w*=\S*\s*[;&]*\s+)*CHEZMOI_COMMIT_OK=\S+\s+
```

- `CHEZMOI_COMMIT_OK=<value>` must be the first non-preamble token.
  **Preamble** = leading env-var assignments (e.g. `CI=true`,
  `GIT_TERMINAL_PROMPT=0`) optionally with `export` and chained by
  `;`/`&&`. So all of these work in call 2:
  ```
  CHEZMOI_COMMIT_OK=approved git -C ~/.local/share/chezmoi commit -m "..."
  CI=true CHEZMOI_COMMIT_OK=approved git ... commit
  export CI=true && CHEZMOI_COMMIT_OK=approved git ... commit
  ```
  But `cat foo ; CHEZMOI_COMMIT_OK=approved git commit` does NOT (the
  leading `cat` is a real command, not preamble). Split into separate
  bash invocations or restructure.
- Value: any non-whitespace string. The plugin doesn't compare it to the
  file contents — file existence + TTL is the authority.
- TTL: 5 minutes from the `chezmoi-approve-commit` invocation. Stale
  approvals are rejected and the file cleaned up. Within the window,
  multiple commits/pushes are allowed under the same approval.
- `CHEZMOI_COMMIT_OK=...` appearing inside a quoted string (e.g. a commit
  message that mentions this feature) does NOT trigger the bypass — by
  design, so documenting the feature can't accidentally enable it.

#### What the plugin checks for commit attempts

The guard catches commit/push intent across all of these vectors:

- `git -C <chezmoi-src> commit/push/reset/rebase/merge`
- `git --git-dir=<chezmoi-src>/.git commit ...` and `--work-tree=<chezmoi-src>`
- `chezmoi git -- commit ...`
- `cd <chezmoi-src> && git commit` (and subshell `(...)` / `pushd` variants)
- **Bash-tool `workdir` parameter set to chezmoi-src** + a git write-verb in
  the command (e.g. `bash(workdir="~/.local/share/chezmoi", command="git commit")`).
  This bypass would otherwise sneak past every detector that scans the
  command string only — the workdir is supplied by the tool wrapper, not
  the command shell. Always honor the policy: don't try to route a commit
  via a workdir-set bash call hoping the regex misses it.

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

The MCP server returns "not initialized." Ask the user: *"I notice this project doesn't have CodeGraph initialized. Want me to run `codegraph init -i` to build the index?"*
<!-- CODEGRAPH_END -->
