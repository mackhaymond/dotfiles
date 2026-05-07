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
approved you must mint a **single-use nonce** via the `chezmoi-approve-commit`
zsh helper (defined in `dot_zshrc.tmpl`) and pass it as `CHEZMOI_COMMIT_OK`:

```bash
CHEZMOI_COMMIT_OK=$(chezmoi-approve-commit) git -C ~/.local/share/chezmoi commit -m "..."
CHEZMOI_COMMIT_OK=$(chezmoi-approve-commit) git -C ~/.local/share/chezmoi push
```

The nonce is **deleted on first match**. A second commit/push needs a
fresh nonce — re-run the helper. This forces each commit to be a deliberate
code path, not a side-effect of a previously-granted blanket approval.

Use this ONLY when you have direct, explicit user approval for THIS specific
commit/push. Minting a nonce IS a security action; doing so without the
user's go-ahead is a hard-rule violation.

#### Constraints

Regex (in `dot_config/opencode/plugins/chezmoi-guard.ts` — keep in sync):

```
^\s*(?:(?!CHEZMOI_COMMIT_OK=)(?:export\s+)?[A-Za-z_]\w*=\S*\s*[;&]*\s+)*CHEZMOI_COMMIT_OK=([0-9a-fA-F]{8,})\s+
```

- `CHEZMOI_COMMIT_OK=<nonce>` must be the first non-preamble token. **Preamble**
  = leading env-var assignments (e.g. `CI=true`, `GIT_TERMINAL_PROMPT=0`)
  optionally with `export` and chained by `;`/`&&`. So all of these work:
  ```
  CHEZMOI_COMMIT_OK=$(chezmoi-approve-commit) git -C ~/.local/share/chezmoi commit -m "..."
  CI=true CHEZMOI_COMMIT_OK=$N git ... commit          # where N is your fresh nonce
  export CI=true && CHEZMOI_COMMIT_OK=$N git ... commit
  ```
  But `cat foo ; CHEZMOI_COMMIT_OK=$N git commit` does NOT (the leading
  `cat` is a real command, not preamble). Split into separate bash
  invocations or restructure.
- Nonce shape: `[0-9a-fA-F]{8,}` (≥8 hex chars). The helper mints 32-char
  hex (16 bytes from `openssl rand`). Authoritative check is byte-equal
  compare against `~/.cache/chezmoi-guard/approve-nonce`.
- TTL: 5 minutes from helper invocation. Stale nonces are rejected and
  cleaned up — keep approval and commit close in time.
- `CHEZMOI_COMMIT_OK=<nonce>` appearing inside a quoted string (e.g. a
  commit message that mentions this feature) does NOT trigger the bypass —
  by design, so documenting the feature can't accidentally enable it.

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
