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

The `chezmoi-guard` plugin will refuse `bash` commits/pushes targeting the
chezmoi source repo by default. To run a commit the user has explicitly
approved, make `CHEZMOI_COMMIT_OK=1` the **first token** of the bash command:

```bash
CHEZMOI_COMMIT_OK=1 git -C ~/.local/share/chezmoi commit -m "..."
CHEZMOI_COMMIT_OK=1 git -C ~/.local/share/chezmoi push
```

Use this ONLY when you have direct, explicit user approval for THIS specific
commit/push. The env var is an attestation that you obtained permission;
silently using it without the user's go-ahead is a hard-rule violation.

Constraints (enforced by regex
`^\s*(?:(?:export\s+)?[A-Za-z_]\w*=\S*\s*[;&]*\s+)*CHEZMOI_COMMIT_OK=1\s+`,
defined in `dot_config/opencode/plugins/chezmoi-guard.ts` — keep in sync):

- `CHEZMOI_COMMIT_OK=1` must be the first non-preamble token. **Preamble**
  = leading env-var assignments (e.g. `CI=true`, `GIT_TERMINAL_PROMPT=0`)
  optionally with `export` and chained by `;`/`&&`. So all of these work:
  ```
  CHEZMOI_COMMIT_OK=1 git -C ~/.local/share/chezmoi commit -m "..."
  CI=true CHEZMOI_COMMIT_OK=1 git ... commit
  export CI=true && CHEZMOI_COMMIT_OK=1 git ... commit
  ```
  But `cat foo ; CHEZMOI_COMMIT_OK=1 git commit` does NOT (the leading
  `cat` is a real command, not preamble). Split into separate bash
  invocations or restructure.
- Only `=1` is accepted. Not `=true`, not `=yes`. One canonical shape.
- `CHEZMOI_COMMIT_OK=1` appearing inside a quoted string (e.g. a commit
  message that mentions this feature) does NOT trigger the bypass — by
  design, so documenting the feature can't accidentally enable it.
