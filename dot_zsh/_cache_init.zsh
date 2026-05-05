# ~/.zsh/_cache_init.zsh
#
# Cache `<tool> init` shell output to disk; regenerate only when the tool's
# binary is newer than the cache. Saves a fork+exec per shell start.
#
# Sourced from BOTH .zprofile (login shells, for PATH-shaping inits like
# `pyenv init --path`) and .zshrc (interactive shells, for prompt/hook inits).
# Idempotent: re-sourcing just redefines the function.
#
# Usage:
#   _cache_init <name> <command>
#
# Example:
#   _cache_init pyenv "pyenv init --path --no-rehash zsh"
#
# Cache file: ~/.cache/zsh/<name>.zsh (+ .zwc bytecode sibling)
# Invalidates when the first word of <command> resolves to a binary newer
# than the cache file (catches version upgrades via package manager).
# Bytecode-compiles to .zwc; zsh transparently sources the compiled form
# when newer than source, saving ~1ms per cached source on hot shells.
_cache_init() {
  local name=$1 cmd=$2
  local cache="$HOME/.cache/zsh/${name}.zsh"
  local bin="${cmd%% *}"
  local binpath
  binpath=${commands[$bin]}
  [[ -z "$binpath" ]] && return
  if [[ ! -s "$cache" || "$binpath" -nt "$cache" ]]; then
    [[ -d "$HOME/.cache/zsh" ]] || mkdir -p "$HOME/.cache/zsh"
    eval "$cmd" >| "$cache"
  fi
  # Bytecode-compile (or recompile if cache is newer than .zwc).
  [[ ! -f "${cache}.zwc" || "$cache" -nt "${cache}.zwc" ]] && \
    zcompile -R -- "${cache}.zwc" "$cache" 2>/dev/null
  source "$cache"
}
