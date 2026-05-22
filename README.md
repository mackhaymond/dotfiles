# dotfiles

These are my personal macOS dotfiles, managed with [chezmoi](https://chezmoi.io/).
Highly opinionated and tuned for my workflow on Apple Silicon — feel free to
fork, adapt, and steal patterns. Don't blindly apply this to your machine.

> [!WARNING]
> This repo writes to `~`, modifies macOS system defaults, installs ~250
> Homebrew packages, and clones external repositories. **Read the code before
> running it.** If you want to use any of this, fork first and adapt to your
> needs.

<!-- TODO: terminal screenshot here -->

---

## What's inside

- **Shell**: zsh with [antidote](https://github.com/mattmc3/antidote), [starship](https://starship.rs/), lazy-loaded language tooling
- **Editor**: neovim (managed as a separate repo via [`.chezmoiexternal.toml`](./.chezmoiexternal.toml)) + ideavim
- **Multiplexers**: tmux ([tpm](https://github.com/tmux-plugins/tpm)-managed plugins) and zellij
- **Terminal**: [WezTerm](https://wezfurlong.org/wezterm/) primarily, iTerm2 fallback
- **Window management** (macOS): [yabai](https://github.com/koekeishiya/yabai) + [skhd](https://github.com/koekeishiya/skhd) + [sketchybar](https://github.com/FelixKratz/SketchyBar)
- **Secrets**: [age](https://age-encryption.org/) for whole-file encryption, [1Password](https://1password.com/) `op://` templates for rotatable tokens
- **Git**: [delta](https://github.com/dandavison/delta) pager, SSH commit signing via 1Password agent
- **Bootstrap**: `chezmoi init --apply` plus a handful of `run_once_after_*` scripts
- **Brewfile**: ~400 entries spanning taps, brews, casks, vscode extensions, cargo, and uv. For the live breakdown: `awk '/^(tap|brew|cask|vscode|cargo|uv) /{print $1}' Brewfile.tmpl | sort | uniq -c`

---

## Quick start

### Option 1: dry-run first (recommended for tire-kickers)

Look without touching:

```sh
chezmoi init --apply=false https://github.com/mackhaymond/dotfiles.git
chezmoi diff
```

This clones the source dir to `~/.local/share/chezmoi/` so you can browse,
runs the init prompts, and shows what would change without writing anything.

### Option 2: full bootstrap (you've reviewed and want to apply)

Prerequisites: macOS, Homebrew installed, a 1Password account with the
desktop app's CLI integration enabled (Settings → Developer → "Integrate
with 1Password CLI"), and a personal age key (see [Secrets](#secrets)).

```sh
brew install chezmoi age gh
brew install --cask 1password-cli   # if not already installed

# 1. Authenticate to clone over HTTPS
gh auth login

# 2. Place your age private key (replace item name with your own backup)
mkdir -p ~/.config/chezmoi
op read "op://Developer/chezmoi-age-key-mackbook/notesPlain" \
    > ~/.config/chezmoi/key.txt
chmod 600 ~/.config/chezmoi/key.txt

# 3. Apply
chezmoi init --apply https://github.com/mackhaymond/dotfiles.git
```

`chezmoi init` will prompt for: name, email, GitHub username. Answers are
saved to `~/.config/chezmoi/chezmoi.toml` and reused. The git commit
signing key is pulled from 1Password automatically (no prompt) — see the
[Secrets](#secrets) section.

`--apply` will:
- Render all templates and copy files into `~`
- Decrypt age files with your local key
- Pull op:// values from 1Password at apply time
- Clone external repos (`mackhaymond/nvim`, `tmux-plugins/tpm`)
- Run `brew bundle install` against the templated Brewfile
- Run idempotent setup scripts: macOS defaults, tpm plugins, broot launcher, opam user-setup

OAuth flows that can't be scripted (re-auth per machine):

```sh
gh auth login    # already done above; re-run as needed
aws sso login    # AWS SSO if you use it
```

### Option 3: clone the source first, then apply

```sh
git clone https://github.com/mackhaymond/dotfiles.git ~/Code/dotfiles
chezmoi init --source=~/Code/dotfiles
# review ~/.local/share/chezmoi/ — actually it's symlinked from ~/Code/dotfiles
chezmoi diff
chezmoi apply
```

---

## Selective install

This repo is intentionally **monolithic** — no init-time prompts asking "do
you want X?" The cost of maintaining feature flags isn't justified for a
single-author repo, and chezmoi already gives you three good ways to install
only what you want:

### Path-based — install specific files or trees

```sh
chezmoi init https://github.com/mackhaymond/dotfiles.git    # init WITHOUT --apply
chezmoi diff                                               # browse what's available

# Pick specific things:
chezmoi apply ~/.zshrc ~/.zprofile ~/.gitconfig            # just shell + git
chezmoi apply ~/.config/tmux                               # the whole tmux tree
chezmoi apply ~/.config/nvim ~/.config/wezterm             # editor + terminal
```

### Type-based — install only certain entry types

Chezmoi categorizes everything into types: `files`, `dirs`, `scripts`,
`externals`, `encrypted`, `templates`. Filter with `--include` / `--exclude`:

```sh
# All dotfiles, but skip the bootstrap automation (no Brewfile install,
# no macOS defaults script, no broot/tpm/opam setup)
chezmoi apply --exclude=scripts

# Files only - no scripts, no external repos cloned
chezmoi apply --exclude=scripts,externals

# Just decrypt and write the age-encrypted files
chezmoi apply --include=encrypted

# Useful for a server install: skip GUI tools, big package install,
# external repo clones
chezmoi apply --exclude=scripts,externals
```

### Fork-and-delete — permanent exclusion

For a fork that doesn't want certain tools, the lowest-friction approach is
to delete what you don't want **before** the first apply:

```sh
git clone https://github.com/mackhaymond/dotfiles.git ~/Code/dotfiles
cd ~/Code/dotfiles

# Drop macOS-only window-management bundle:
rm -rf dot_config/yabai dot_config/skhd dot_config/sketchybar dot_config/karabiner

# Drop the AI tooling configs:
rm -rf dot_config/opencode dot_agents

# Drop the Brewfile install entirely:
rm Brewfile.tmpl .chezmoiscripts/run_onchange_after_install-brew-packages.sh.tmpl

# Then init from your local source:
chezmoi init --source=~/Code/dotfiles --apply
```

This is the [`mathiasbynens/dotfiles`](https://github.com/mathiasbynens/dotfiles)
philosophy — fork it, review it, customize it. No prompts to memorize, no
flags to keep in sync with reality.

### What you might want to skip in practice

The components most likely to be unwanted on a non-primary machine:

| Component | How to skip |
|---|---|
| Brewfile install (250 packages) | `--exclude=scripts` OR delete `Brewfile.tmpl` + the brew-bundle script |
| macOS window management (yabai/skhd/sketchybar/karabiner) | Auto-skipped on non-darwin; on darwin delete the `dot_config/{yabai,skhd,sketchybar,karabiner}/` trees |
| macOS defaults script (modifies system settings) | `--exclude=scripts` OR delete `.chezmoiscripts/run_once_after_set-macos-defaults.sh.tmpl` |
| External repo clones (nvim, tpm) | `--exclude=externals` OR remove entries from `.chezmoiexternal.toml` |
| AI tooling configs | Delete `dot_config/opencode/`, `dot_agents/`, `dot_config/cursor/` |

---

## Architecture

The repo is organized around chezmoi's source-state conventions:

| Source filename | Becomes in `$HOME` |
|---|---|
| `dot_zshrc.tmpl` | `.zshrc` (Go-templated render) |
| `private_dot_gitconfig.tmpl` | `.gitconfig` mode 0600, templated |
| `executable_some-script.sh` | `some-script.sh` with `+x` |
| `encrypted_*_dot_*.age` | age-decrypted at apply time |
| `Brewfile.tmpl` | `Brewfile` (templated) |
| `.chezmoiscripts/run_once_after_*.sh.tmpl` | executed once per machine, never deployed |
| `.chezmoiscripts/run_onchange_*.sh.tmpl` | re-executed when rendered content changes |

Stacking order (when multiple prefixes apply):
`encrypted_` → `private_` → `readonly_` → `empty_` → `executable_` → `dot_`.

Templates reference data variables computed at init time:

| Variable | Source |
|---|---|
| `.homebrew_prefix` | computed from os+arch (`darwin/arm64` → `/opt/homebrew`, `darwin/amd64` → `/usr/local`, `linux` → `/home/linuxbrew/.linuxbrew`) |
| `.email`, `.name`, `.github_username` | prompted on first init via `promptStringOnce` |
| `.chezmoi.os`, `.chezmoi.arch`, `.chezmoi.homeDir`, `.chezmoi.hostname` | auto-populated by chezmoi |

`.chezmoiignore` uses Go-template conditionals so macOS-only configs
(yabai, skhd, sketchybar, karabiner, raycast) get filtered out on Linux:

```
{{ if ne .chezmoi.os "darwin" -}}
.config/yabai/**
.config/skhd/**
...
{{ end -}}
```

Preview the rendered ignore list with `chezmoi execute-template < .chezmoiignore`.

---

## Secrets

Hybrid approach with two patterns:

**age** for static, multi-secret files where rotation is rare:
- `~/.zshenv.private` — env exports (sourced by `.zshenv`)
- `~/.ssh/config` — private hostnames, internal IPs, agent socket paths

**1Password `op://` templates** for rotatable single-value tokens:

| File | 1Password reference |
|---|---|
| `~/.tmate.conf` | `op://Developer/tmate-api-key/credential` |
| `~/.config/raycast/config.json` | `op://Developer/raycast-access-token/credential` |
| `~/.config/sketchybar/plugins/todos.sh` | `op://Developer/todoist-api-token/credential` |
| `~/.gitconfig` (just the `signingkey` field) | `op://Personal/Github SSH Key/public_key` |

Templates contain only `{{ onepasswordRead "..." }}` placeholders — never
the literal token. Rotation is "edit in 1Password → `chezmoi apply`."

The age private key is backed up in 1Password as a Secure Note. Lose the
key without the backup and every age-encrypted file becomes unrecoverable.

SSH keys are managed by 1Password's SSH agent; the public side is in
`.gitconfig` for commit signing. No SSH private keys are tracked.

---

## Customization

If you want to use this as a starting point, **fork first**. Then:

1. **Update the bootstrap one-liner** in this README to point at your fork.
2. **Rename or delete `.chezmoi.toml.tmpl`** prompts you don't need.
3. **Rotate the age key** — generate your own (`age-keygen`) and replace the
   `recipient` in `.chezmoi.toml.tmpl`. Re-encrypt each `.age` file with
   your new key.
4. **Replace the op:// references** with your own 1Password vault structure.
5. **Trim the Brewfile** — most of these packages are mine, not yours.
   Run `brew bundle dump --describe --force --file=Brewfile.tmpl` after
   pruning, or strip what you don't want.
6. **Strip macOS-only configs** if you're on Linux (delete the
   `dot_config/{yabai,skhd,sketchybar,karabiner,raycast}/` trees and the
   matching `.chezmoiignore` darwin block).
7. **Replace `.chezmoiexternal.toml` URLs** with your own nvim repo (or
   delete the entry entirely if you vendor your nvim config).
8. **Edit `.chezmoiscripts/run_once_after_set-macos-defaults.sh.tmpl`** —
   these reflect my preferences, not yours.

For machine-specific settings that don't need to live in git, put them in
`~/.config/chezmoi/chezmoi.toml` directly:

```toml
[data]
work_machine = true
extra_path = "/some/local/path"
```

These are then accessible as `{{ .work_machine }}` in any template.

---

## Cross-platform

The repo is templated for cross-platform use, but only macOS is exercised
today. To add a Linux machine:

1. Run the bootstrap. `homebrew_prefix` resolves to `/home/linuxbrew/.linuxbrew`.
2. macOS-only configs filter out automatically via `.chezmoiignore`.
3. `Brewfile.tmpl` renders empty on Linux. Add an `{{ else if eq .chezmoi.os "linux" }}` block or a separate `apt`/`dnf` install script.
4. Add Linux-only configs (`.config/i3/`, `.Xresources`, etc.) and gate
   them in `.chezmoiignore`.

Shell, git, editor, and most CLI configs already work on Linux because they
use `{{ .homebrew_prefix }}` and `{{ .chezmoi.homeDir }}` instead of
literal paths.

---

## Maintainer ops

Day-to-day workflows for editing/adding/removing dotfiles, rotating
secrets, multi-machine sync, etc. are documented in **[OPERATIONS.md](./OPERATIONS.md)**.

---

## Acknowledgments

Patterns and inspiration from:

- [twpayne/dotfiles](https://github.com/twpayne/dotfiles) — chezmoi's author shows the canonical patterns
- [mathiasbynens/dotfiles](https://github.com/mathiasbynens/dotfiles) — the macOS `defaults write` reference
- [holman/dotfiles](https://github.com/holman/dotfiles) — topical organization, philosophy-first README
- [thoughtbot/dotfiles](https://github.com/thoughtbot/dotfiles) — `.local` overrides pattern
- [chezmoi.io](https://chezmoi.io/) — phenomenal docs

---

## License

MIT — see [LICENSE](./LICENSE).
