# OPERATIONS.md

Personal ops manual for this dotfiles repo. Day-to-day workflows for
editing, adding, removing, rotating, syncing, debugging.

For the public-facing intro and architecture, see [README.md](./README.md).

---

## Table of contents

1. [Cheatsheet](#cheatsheet)
2. [Daily flow](#daily-flow)
3. [Adding a new dotfile](#adding-a-new-dotfile)
4. [Editing an existing dotfile](#editing-an-existing-dotfile)
5. [Removing a dotfile](#removing-a-dotfile)
6. [Multi-machine sync](#multi-machine-sync)
7. [Rotating an op:// secret](#rotating-an-op-secret)
8. [Adding a new op:// secret](#adding-a-new-op-secret)
9. [Working with age-encrypted files](#working-with-age-encrypted-files)
10. [Rotating the age key (rare)](#rotating-the-age-key-rare)
11. [Working with the external nvim repo](#working-with-the-external-nvim-repo)
12. [Adding another external git repo](#adding-another-external-git-repo)
13. [Adding a bootstrap script](#adding-a-bootstrap-script)
14. [Updating the Brewfile](#updating-the-brewfile)
15. [Updating macOS defaults](#updating-macos-defaults)
16. [Onboarding a new machine](#onboarding-a-new-machine)
17. [Selective install (for forks or partial deployments)](#selective-install-for-forks-or-partial-deployments)
18. [Pulling changes from another machine](#pulling-changes-from-another-machine)
18. [Resolving merge conflicts](#resolving-merge-conflicts)
19. [Debugging](#debugging)
20. [Disaster recovery](#disaster-recovery)
21. [Decision tree: when to use what](#decision-tree-when-to-use-what)

---

## Cheatsheet

```sh
chezmoi diff              # what would apply do?
chezmoi apply             # apply pending changes
chezmoi apply --force ~/.foo   # discard live edits, restore from source
chezmoi edit --apply ~/.zshrc  # edit source (works for plain/templated/encrypted) + auto-apply
chezmoi merge ~/.foo      # 3-way merge if live and source diverged
chezmoi add ~/.foo        # start tracking a new file
chezmoi re-add ~/.foo     # propagate live edits to source (PLAIN FILES ONLY - silently skips templates)
chezmoi forget --force ~/.bar  # stop tracking
chezmoi cd                # cd into source dir for git ops
chezmoi update            # git pull + apply
chezmoi managed           # list everything chezmoi controls
chezmoi data              # dump all template data
chezmoi cat ~/.tmate.conf # render and print (decrypts age + resolves op://)
chezmoi source-path ~/.zshrc   # show source filename (e.g., dot_zshrc.tmpl)
chezmoi execute-template < FILE  # render a template through chezmoi
chezmoi doctor            # health check
chezmoi state delete-bucket --bucket=scriptState  # reset run_once tracking
```

**Editing rule of thumb:** `chezmoi edit --apply` is the universal "edit
this managed file" command. Works for plain, templated, encrypted, and
op://-templated files. `re-add` is only useful for plain files — it
silently skips templates.

---

## Daily flow

After editing things by hand or pulling from origin:

```sh
chezmoi diff       # see if source and live drifted
chezmoi apply      # apply if needed
```

After making a config change you want to track:

```sh
# Universal pattern (works for plain, templated, encrypted, op:// files)
chezmoi edit --apply ~/.zshrc      # opens source in $EDITOR, auto-applies on save
chezmoi cd && git diff             # review
chezmoi cd && git add . && git commit -m "..." && git push

# Alternative ONLY for plain (non-templated) files
$EDITOR ~/.bashrc                  # edit the LIVE file directly
chezmoi re-add ~/.bashrc           # mirror back to source
# WARNING: re-add silently skips templated files. For dot_zshrc.tmpl,
# private_dot_gitconfig.tmpl, etc. this would be a no-op and your edits
# would be reverted on next `chezmoi apply`. Use `chezmoi edit` instead.
```

`chezmoi edit --apply` is the safe default — it edits the source (handling
template/encryption transparently), auto-applies, no re-add gotchas.

---

## Adding a new dotfile

### Step 1: decide the storage type

| Content | Use |
|---|---|
| Static config, no machine differences, no secrets | **plain** (`chezmoi add`) |
| Same purpose, but path/value differs per machine | **template** (`chezmoi add` + rename to `.tmpl`) |
| Whole file is sensitive (private keys, env exports, host inventory) | **age-encrypt** (`chezmoi add --encrypt`) |
| Has one or two rotatable tokens in plaintext otherwise | **op:// template** (manual: rename to `.tmpl` + use `onepasswordRead`) |

### Step 2: add it

**Plain:**
```sh
chezmoi add ~/.config/foo/config.toml
```

**Templated** (after `chezmoi add`, edit the source to use template syntax):
```sh
chezmoi add ~/.config/foo/config.toml
$EDITOR $(chezmoi source-path ~/.config/foo/config.toml)
mv $(chezmoi source-path ~/.config/foo/config.toml){,.tmpl}   # rename to .tmpl
chezmoi diff   # verify it still renders to the same content
```

Inside the `.tmpl`, reference `{{ .chezmoi.homeDir }}`, `{{ .homebrew_prefix }}`,
`{{ .email }}`, `{{ .name }}`, `{{ .github_username }}`, or any conditional like
`{{ if eq .chezmoi.os "darwin" }}...{{ end }}`. For the SSH commit-signing
public key, use `{{ onepasswordRead "op://Personal/Github SSH Key/public_key" }}`
directly in the template (don't store it as a `data` variable — it would
need re-init on rotation).

**Age-encrypted:**
```sh
chezmoi add --encrypt ~/.zshenv.private
```

This produces `encrypted_private_dot_zshenv.private.age` in the source dir
(with `private_` if perms were 0600). Decrypts on apply.

**op:// templated** (multi-step):

1. Create the 1Password item:
   ```sh
   op item create --category="API Credential" --vault="Developer" \
       --title="my-new-token" \
       "credential[concealed]=<value>"
   ```
2. Verify the reference works:
   ```sh
   op read "op://Developer/my-new-token/credential"
   ```
3. Create the source file directly (don't use `chezmoi add`):
   ```sh
   cd $(chezmoi source-path)
   # create dot_config/foo/config.json.tmpl with the placeholder:
   cat > dot_config/foo/config.json.tmpl <<'EOF'
   {
     "token": "{{ onepasswordRead "op://Developer/my-new-token/credential" }}"
   }
   EOF
   ```
4. Verify render:
   ```sh
   chezmoi cat ~/.config/foo/config.json   # should show the actual token
   chezmoi diff   # should match live file (or show what'll change)
   ```
5. Apply + commit:
   ```sh
   chezmoi apply
   chezmoi cd && git add . && git commit -m "Add foo config (op:// templated)" && git push
   ```

### Step 3: ensure perms are right

Source filename prefix encodes target perms:
- File is 0600 in live? → use `private_` prefix
- File needs `+x`? → use `executable_` prefix
- Stack: `encrypted_private_executable_dot_foo` is valid

`chezmoi add` figures this out automatically from the live file's actual mode.

### Step 4: commit

```sh
chezmoi cd
git add .
git commit -m "Add ~/.config/foo/config.toml"
git push
```

---

## Editing an existing dotfile

### Universal pattern: `chezmoi edit --apply`

```sh
chezmoi edit --apply ~/.zshrc
```

This is the **only command you need** to edit a managed file, regardless
of whether it's plain, templated, encrypted, or op://-templated. It:

- Opens the source in `$EDITOR`
- For plain files: edits the source directly
- For templated files (`.tmpl`): opens the template; the `.tmpl` extension
  is preserved so editor syntax-highlighting can detect template directives.
  Combined with `--hardlink` (default), the editor sees the target filename
  (e.g., `.zshrc.tmpl`) for proper syntax highlighting
- For encrypted files (`.age`): decrypts to a tmp file, you edit, re-encrypts
  on save (transparent)
- For op:// templates: same as templated — edit the `.tmpl`
- On save, auto-applies the rendered output to the live target

### Editing the template directly (without `--apply`)

If you want to verify the diff before applying:

```sh
chezmoi edit ~/.zshrc      # opens source, no auto-apply
chezmoi diff               # see what would change
chezmoi apply              # apply when satisfied
```

Or open the source path manually:

```sh
$EDITOR $(chezmoi source-path ~/.zshrc)   # opens dot_zshrc.tmpl directly
chezmoi diff
chezmoi apply
```

### ⚠️ `chezmoi re-add` does NOT propagate template edits

Per `chezmoi re-add --help`:

> "chezmoi will **not overwrite templates**, and all entries that are not
> files are ignored."

This is a safety feature (your `{{ }}` directives won't get clobbered) but
also a **silent no-op trap**: if you edit `~/.zshrc` directly and run
`chezmoi re-add ~/.zshrc`, your live changes are NOT propagated to the
source template. `chezmoi re-add` reports success and exits cleanly,
leaving the source unchanged. Next `chezmoi apply` reverts your live edits.

**`re-add` is only useful for plain, non-templated files.** For our repo,
that's a small subset (`dot_bashrc`, `dot_profile`, `dot_zshenv`, etc.).

### Recovering from "I accidentally edited the live file"

It happens. `chezmoi diff` will show the drift. Three recovery paths:

```sh
# Option A: discard live edits, restore from source
chezmoi apply --force ~/.zshrc

# Option B: keep live edits, manually port them to the template source
chezmoi edit ~/.zshrc          # opens dot_zshrc.tmpl
# manually copy your live changes in, save
chezmoi apply ~/.zshrc

# Option C: interactive 3-way merge between source/target/destination
chezmoi merge ~/.zshrc         # opens vimdiff (or whatever merge.command is)
```

For the merge tool, our chezmoi config doesn't override merge.command, so
it uses the default: `vimdiff`. Three buffers: source-rendered, current
target, your local destination. Resolve, save, exit.

### Templated files vs plain files at-a-glance

| Source filename | What `chezmoi edit` does | Can `chezmoi re-add` propagate live edits? |
|---|---|---|
| `dot_bashrc` (plain) | Opens source in editor | ✅ Yes |
| `dot_zshrc.tmpl` (templated) | Opens .tmpl source in editor | ❌ No — silently skipped |
| `private_dot_gitconfig.tmpl` (templated) | Opens .tmpl in editor | ❌ No |
| `encrypted_private_dot_zshenv.private.age` | Decrypts, opens, re-encrypts | N/A — use `chezmoi edit` |
| `dot_tmate.conf.tmpl` (op://) | Opens .tmpl with op:// reference visible | ❌ No |

Bottom line: **`chezmoi edit --apply` works for everything; `re-add`
only works for plain files.** When in doubt, use `chezmoi edit`.

### Convenient alias

If you edit managed files often, add to `~/.zshrc`:

```sh
alias ce='chezmoi edit --apply'
# Then: ce ~/.zshrc
```

---

## Removing a dotfile

### Stop tracking but keep the live file

```sh
chezmoi forget --force ~/.config/foo/config.toml
```

Removes from source dir. Live file at `~/.config/foo/config.toml` stays put
but is no longer managed.

### Stop tracking and delete the live file

```sh
chezmoi forget --force ~/.config/foo/config.toml
rm ~/.config/foo/config.toml
```

Or use `chezmoi destroy` to remove from both source and target:

```sh
chezmoi destroy ~/.config/foo/config.toml
```

### Tell other machines to delete it on next apply

Add to `.chezmoiremove` in the source dir root:

```
.config/foo/config.toml
```

On `chezmoi apply` on each machine, that file gets deleted from the home dir.
After all machines are synced, you can remove the entry.

### Permanently exclude from chezmoi

Add to `.chezmoiignore`:

```
.config/foo/config.toml
```

Useful when a tool autogenerates a file that shouldn't be tracked (e.g.,
`.config/btop/btop.log`).

---

## Multi-machine sync

### Push changes from this machine

```sh
chezmoi cd
git status
git add -A
git commit -m "..."
git push
```

`chezmoi cd` opens a subshell rooted at the source dir. Exit with `exit` or `Ctrl-D`.

### Pull changes onto another machine

```sh
chezmoi update
```

This is `git pull` + `chezmoi apply` in one. If there are local uncommitted
changes in the source dir (rare), it'll fail; commit/stash first.

### Sync state of an external repo (nvim) across machines

The `nvim` external is `--autostash --rebase` — uncommitted changes in
`~/.config/nvim/` get stashed, the pull rebases your local commits onto
upstream, then re-applies the stash. If conflicts, resolve manually inside
`~/.config/nvim/` then continue:

```sh
cd ~/.config/nvim
git status   # see conflict
# fix conflicts
git add .
git rebase --continue
git stash pop   # if there was a stash
```

---

## Rotating an op:// secret

For files using `{{ onepasswordRead "op://Developer/X/credential" }}`:

1. Open 1Password → Developer vault → the item → edit `credential` field with the new value
2. On each machine: `chezmoi apply` (renders the template with the new value)
3. Restart the consuming app (sketchybar, raycast, etc.) if it caches at startup

**No git commit. No re-encryption. No key rotation.** This is the entire
reason op:// templates are preferred for rotatable tokens.

---

## Adding a new op:// secret

```sh
# 1. Add to 1Password
op item create --category="API Credential" --vault="Developer" \
    --title="new-service-name" \
    --tags="dotfiles,chezmoi" \
    "credential[concealed]=<paste-token>"

# 2. Verify the reference resolves
op read "op://Developer/new-service-name/credential"

# 3. Reference it in your template
# In a .tmpl file:
#   {{ onepasswordRead "op://Developer/new-service-name/credential" }}
```

**Naming rules**: op:// references can't contain `(`, `)`, `:`, `/`, `@`,
`[`, `]`, `{`, `}`, etc. Stick to letters, numbers, hyphens, underscores.
Spaces work but require quoting the whole reference.

---

## Working with age-encrypted files

### List which files are encrypted

```sh
find $(chezmoi source-path) -name "*.age"
```

### Decrypt and view a managed file

```sh
chezmoi cat ~/.zshenv.private
```

### Add a new file as encrypted

```sh
chezmoi add --encrypt ~/.somefile
```

### Convert a tracked file from plain to encrypted (or vice versa)

```sh
chezmoi forget --force ~/.somefile
chezmoi add --encrypt ~/.somefile     # now encrypted
# or:
chezmoi add ~/.somefile               # now plain
```

### Verify a roundtrip

```sh
shasum -a 256 ~/.somefile
chezmoi cat ~/.somefile | shasum -a 256
# Hashes should match.
```

---

## Rotating the age key (rare)

This is a multi-step process. Only do it if you suspect the current key
is compromised, or you want per-machine keys (advanced).

```sh
# 1. Generate new key
age-keygen -o ~/.config/chezmoi/key.txt.new

# 2. Get the new public key (recipient)
NEW_RECIPIENT=$(grep "public key:" ~/.config/chezmoi/key.txt.new | sed 's/# public key: //')
echo "$NEW_RECIPIENT"

# 3. For every encrypted file, decrypt with old key + re-encrypt with new
cd $(chezmoi source-path)
for f in $(find . -name "*.age"); do
    age -d -i ~/.config/chezmoi/key.txt "$f" \
        | age -e -r "$NEW_RECIPIENT" -a -o "$f.new"
    mv "$f.new" "$f"
done

# 4. Update the recipient in chezmoi config
$EDITOR ~/.config/chezmoi/chezmoi.toml   # update [age] recipient = "..."
$EDITOR .chezmoi.toml.tmpl                # update the template's recipient too

# 5. Replace local key
mv ~/.config/chezmoi/key.txt.new ~/.config/chezmoi/key.txt
chmod 600 ~/.config/chezmoi/key.txt

# 6. Verify everything still decrypts
chezmoi diff   # should be empty
for f in /Users/mackhaymond/.zshenv.private /Users/mackhaymond/.ssh/config; do
    LIVE=$(shasum -a 256 "$f" | awk '{print $1}')
    RENDERED=$(chezmoi cat "$f" | shasum -a 256 | awk '{print $1}')
    [[ "$LIVE" == "$RENDERED" ]] && echo "OK: $f" || echo "MISMATCH: $f"
done

# 7. Update the 1Password backup
op item edit chezmoi-age-key-mackbook --vault Developer \
    "notesPlain=$(cat ~/.config/chezmoi/key.txt)"

# 8. Commit + push
chezmoi cd
git add -A
git commit -m "Rotate age key"
git push

# 9. On every other machine: pull the new key from 1Password, then chezmoi update
op read "op://Developer/chezmoi-age-key-mackbook/notesPlain" \
    > ~/.config/chezmoi/key.txt
chmod 600 ~/.config/chezmoi/key.txt
chezmoi update
```

---

## Working with the external nvim repo

`~/.config/nvim` is **not** managed by chezmoi — it's a separate git repo
at `github.com/SpyicyDev/nvim`, cloned and refreshed via `.chezmoiexternal.toml`.

### Edit nvim config

```sh
cd ~/.config/nvim
$EDITOR lua/...
git add .
git commit -m "..."
git push
```

Just standard git workflow. Chezmoi doesn't get involved.

### Pull upstream nvim changes

```sh
cd ~/.config/nvim
git pull --autostash --rebase
```

Or wait for `chezmoi apply` to do it (when `refreshPeriod = "168h"` elapses)
or force with `chezmoi apply -R`.

### Don't try to `chezmoi add` files inside nvim

```sh
chezmoi add ~/.config/nvim/init.lua   # ⚠️ DON'T — produces "inconsistent state"
```

The whole `.config/nvim/**` is delegated to git via the external. To
co-manage anything inside that tree, you'd need to switch from `git-repo`
to `archive` external (different tradeoffs).

---

## Adding another external git repo

Edit `.chezmoiexternal.toml`:

```toml
[".some/path"]
    type = "git-repo"
    url = "https://github.com/owner/repo.git"
    refreshPeriod = "168h"   # weekly auto-pull on apply
    [".some/path".clone]
        args = ["--depth", "1"]    # shallow if read-only consumption
    [".some/path".pull]
        args = ["--ff-only"]        # or --autostash --rebase if you commit locally
```

Then `chezmoi apply` clones it. Don't add `.chezmoiignore` for the path —
chezmoi knows it's an external.

---

## Adding a bootstrap script

Create in `.chezmoiscripts/`:

| Naming | When it runs |
|---|---|
| `run_<name>.sh` | every `chezmoi apply` |
| `run_once_<name>.sh` | once per machine (tracked by content hash) |
| `run_onchange_<name>.sh.tmpl` | re-runs when the rendered content changes |
| `run_before_*` / `run_after_*` | order relative to file apply (default: after) |

Example:

```sh
# .chezmoiscripts/run_once_after_install-mytool.sh.tmpl
#!/bin/bash
set -euo pipefail

{{ if ne .chezmoi.os "darwin" -}}
exit 0
{{ end -}}

if ! command -v mytool >/dev/null 2>&1; then
    echo "[mytool] not installed; Brewfile should install it"
    exit 0
fi
mytool --setup
```

Always make scripts idempotent — they run repeatedly across machines.

---

## Updating the Brewfile

After installing/uninstalling brew packages on this machine:

```sh
# Snapshot current state
brew bundle dump --describe --force --file=$(chezmoi source-path)/Brewfile.tmpl
# Re-add the {{ if eq .chezmoi.os "darwin" }} wrapper at top + {{ end }} at bottom
# (the dump strips the template directives)
```

Then commit. The `run_onchange_after_install-brew-packages.sh.tmpl` script
will detect the content change (via `includeTemplate ... | sha256sum`) and
re-run `brew bundle install` on each machine on next apply.

If the wrapper restoration is annoying, dump to a temp file and use sed:

```sh
TMP=$(mktemp)
brew bundle dump --describe --force --file="$TMP"
{
    echo '{{ if eq .chezmoi.os "darwin" -}}'
    cat "$TMP"
    echo '{{- end }}'
} > $(chezmoi source-path)/Brewfile.tmpl
rm "$TMP"
```

---

## Updating macOS defaults

Edit `.chezmoiscripts/run_once_after_set-macos-defaults.sh.tmpl`. The
script will re-run on machines that haven't seen the new content (chezmoi
tracks the run-once hash).

To force a re-run on this machine:

```sh
chezmoi state delete-bucket --bucket=scriptState
chezmoi apply   # all run_once scripts re-execute
```

To find a defaults key for something you want to script:
1. Change the setting via System Settings UI
2. `defaults read > /tmp/before` (before)
3. `defaults read > /tmp/after` (after)
4. `diff /tmp/before /tmp/after` — shows what changed

---

## Onboarding a new machine

See [README.md → Quick start](./README.md#quick-start). Summary:

```sh
brew install chezmoi age gh
brew install --cask 1password-cli
gh auth login
mkdir -p ~/.config/chezmoi
op read "op://Developer/chezmoi-age-key-mackbook/notesPlain" \
    > ~/.config/chezmoi/key.txt
chmod 600 ~/.config/chezmoi/key.txt
chezmoi init --apply https://github.com/SpyicyDev/dotfiles.git
```

After init prompts run, the apply step does everything else automatically.

---

## Selective install (for forks or partial deployments)

The repo is intentionally monolithic — no init-time module flags. When you
or someone else wants to install only parts, three mechanisms cover the
ground without adding complexity to the source:

```sh
# Init without applying - clones source, runs prompts, skips file copy
chezmoi init https://github.com/SpyicyDev/dotfiles.git
chezmoi diff       # browse what's available

# Path-based selective apply
chezmoi apply ~/.zshrc ~/.gitconfig          # specific files
chezmoi apply ~/.config/tmux                  # whole tree

# Type-based filtering
chezmoi apply --exclude=scripts               # skip Brewfile install + macOS defaults + tpm/broot/opam
chezmoi apply --exclude=scripts,externals     # also skip cloning nvim + tpm
chezmoi apply --include=files                 # only file deployment, nothing else
chezmoi apply --include=encrypted             # only age-decrypt the secret files

# Fork-and-delete (for permanent exclusion)
git clone https://github.com/SpyicyDev/dotfiles.git ~/Code/dotfiles
cd ~/Code/dotfiles
rm -rf dot_config/yabai dot_config/skhd dot_config/sketchybar    # drop window mgmt
rm Brewfile.tmpl                                                  # skip the brew install
chezmoi init --source=~/Code/dotfiles --apply
```

**Why no module flags?** The cost of maintaining `promptBoolOnce` flags
plus conditional `.chezmoiignore` blocks plus README drift exceeds the
~5-minute savings vs. fork-and-delete. See README.md "Selective install"
section for the public-facing version. If a real need ever emerges (e.g.,
you start deploying to servers regularly), the cleanest minimal addition
is a single `promptChoiceOnce` profile selector with three options:
`full` / `cli-only` / `minimal`.

---

## Pulling changes from another machine

You committed something on machine A; want it on machine B.

```sh
# On machine B:
chezmoi update      # = git pull + chezmoi apply
```

Or step by step:

```sh
chezmoi cd
git pull
exit
chezmoi diff
chezmoi apply
```

If `chezmoi apply` would clobber a local edit, it'll prompt. To skip the
prompt and overwrite:

```sh
chezmoi apply --force
```

---

## Resolving merge conflicts

### In the source repo (rare)

```sh
chezmoi cd
git pull
# resolve conflicts in source files
git add .
git commit
exit
chezmoi apply
```

### In a managed file (live ≠ source after pulling)

```sh
chezmoi merge ~/.zshrc
```

Opens `vimdiff` (or whatever `merge.command` is) with three buffers: source,
live, target. Resolve, save, exit.

### In the nvim external repo

```sh
cd ~/.config/nvim
git status   # see conflict
# resolve
git add .
git rebase --continue   # if rebase in progress
git stash pop          # if a stash needs reapplying
```

---

## Debugging

### "Why is this template rendering wrong?"

```sh
chezmoi execute-template '{{ .homebrew_prefix }}'
chezmoi execute-template < /Users/mackhaymond/.local/share/chezmoi/dot_zshrc.tmpl
chezmoi cat ~/.zshrc   # shows what would be applied
```

### "Why is this file showing up in chezmoi diff?"

```sh
chezmoi diff ~/.zshrc                           # see exact difference
diff <(cat ~/.zshrc) <(chezmoi cat ~/.zshrc)   # raw line-level
```

### "Why isn't my run_once script running?"

```sh
chezmoi state dump | head                        # see persistent state
chezmoi state delete-bucket --bucket=scriptState # reset, will re-run
```

### "Did my last apply actually do anything?"

```sh
chezmoi apply -v       # verbose, shows each operation
chezmoi apply --dry-run --verbose
```

### "What data is available in templates?"

```sh
chezmoi data | jq
```

### "Is my chezmoi config valid?"

```sh
chezmoi doctor
```

### Common error messages

| Error | Fix |
|---|---|
| `dest-dir ~ is a git working tree` | A stray `~/.git/` exists. `rm -rf ~/.git` (after verifying it's empty/stale). |
| `inconsistent state` when running `chezmoi add` inside an external | The path is delegated to a `git-repo` external. Edit the file directly via the nvim repo workflow. |
| `op read failed: not signed in` | 1Password CLI session expired. Tap a key in the 1Password app, or `op signin`. |
| `age-keygen: open ... permission denied` | Key file isn't 0600 or doesn't exist. `ls -la ~/.config/chezmoi/key.txt`. |
| `template: at <.email>: map has no entry for key "email"` | `~/.config/chezmoi/chezmoi.toml` is missing the `[data]` section. Re-init or add manually. |

---

## Disaster recovery

### Lost age key, still have 1Password

```sh
op read "op://Developer/chezmoi-age-key-mackbook/notesPlain" > ~/.config/chezmoi/key.txt
chmod 600 ~/.config/chezmoi/key.txt
chezmoi apply
```

Done. Encrypted files decrypt again.

### Lost age key AND 1Password access

The age-encrypted files are cryptographically gone. Nothing decrypts them
without the key. **Mitigation**: keep a paper backup of the age private
key stored physically (safe deposit box, etc.).

For the user-facing impact: `~/.zshenv.private` and `~/.ssh/config` are
the only files that go dark. Re-create them manually with new contents.
The op:// templated files are unaffected as long as 1Password is recoverable.

### Lost 1Password access, still have age key

Op:// templates fail to render. `chezmoi apply` errors out on the 3 affected
files. Workaround: temporarily edit the templates to use literal values, or
set up a new 1Password account and re-create the items.

### Lost the GitHub repo

Local source dir at `~/.local/share/chezmoi/` still has everything (it's a
git working tree). Push it to a new remote:

```sh
chezmoi cd
git remote set-url origin git@github.com:NEW-LOCATION/dotfiles.git
git push -u origin main
```

### Lost everything (this machine + repo + 1Password)

If you have an off-site age key backup (paper, safe deposit box), and the
push history exists somewhere (forks, mirrors), you can reassemble.
Otherwise, start from scratch.

---

## Decision tree: when to use what

### "I want to track a new file."

```
Does it contain credentials?
├── Yes
│   ├── Just one rotatable token? → op:// .tmpl template
│   └── Multiple secrets / static / can't easily template? → age-encrypt whole file
└── No
    ├── Has paths/values that differ per machine? → .tmpl template
    └── Same on every machine? → plain
```

### "Where do I put a new bootstrap script?"

```
Does it install/configure something once per machine?
├── Yes → .chezmoiscripts/run_once_after_X.sh.tmpl
└── No
    ├── Should re-run when its content changes? → .chezmoiscripts/run_onchange_after_X.sh.tmpl
    └── Should run every apply? → .chezmoiscripts/run_after_X.sh.tmpl
```

### "Should I add this dotfile to chezmoi at all?"

```
Is it auto-generated by another tool (logs, caches, lock files)?
├── Yes → DON'T track. Add to .chezmoiignore.
├── Per-machine state (atuin sync token, gh hosts.yml)? → DON'T track. Re-auth per machine.
├── A whole external git repo (nvim config)? → DON'T add files; use .chezmoiexternal.toml.
└── Hand-edited config? → Track per the decision tree above.
```

### "I edited a file and it's not picked up."

```
Did I edit the source file or the live file?
├── Source file (~/.local/share/chezmoi/...) → chezmoi apply
└── Live file (~/...)
    │
    ├── Is it a templated file (source filename ends in .tmpl)?
    │   ├── Yes → chezmoi re-add silently skips it.
    │   │       Either: chezmoi apply --force (discard live edits)
    │   │       Or:     manually port to template via chezmoi edit
    │   │       Or:     chezmoi merge ~/path (3-way interactive)
    │   └── No  → chezmoi re-add ~/path (mirrors live to source)
```

Quick check: `chezmoi source-path ~/path` shows the source filename. If
it ends in `.tmpl`, it's templated.

---

## Pre-public hygiene checklist

If you ever flip the repo back to public after adding new tracked files:

- [ ] `grep -rE "ghp_|sk-ant-|tmk-|password\s*=" .` in source dir — should be empty
- [ ] No real email addresses in tracked files (other than templated `{{ .email }}`)
- [ ] No `private_*` files containing actual credentials in plaintext
- [ ] Run `chezmoi cat` on each new templated file to see what would be public
- [ ] No new external repo URLs that point to private repos (people can't clone the bootstrap)
- [ ] op:// references look reasonable (vault/item names will be public metadata)
