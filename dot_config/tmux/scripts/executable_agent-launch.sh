#!/usr/bin/env bash
# agent-launch.sh — ⌘E: any installed coding agent in a new tab. ⌘E opens the
# launcher popup (`agent-ui launch`, spaces.conf: one row per installed agent,
# claude first and preselected; ⏎ or a letter launches, ⇥ picks the space);
# the popup hands every launch to this script, so what each agent is lives in
# ONE place: the AGENTS table below. Claude stays primary (⌘R too).
#
#   agent-launch.sh refresh                 record the installed set in the
#                                           global @launch_agents and its size
#                                           in @launch_count (config load;
#                                           every miss keeps it current)
#   agent-launch.sh list                    the installed rows: id letter command
#   agent-launch.sh go LETTER CLIENT PANE [SPACE]
#                                           that agent in a new tab after PANE's
#                                           window, in PANE's folder (the
#                                           popup's ⏎ / letter, as ⌘R); with
#                                           SPACE ($N, the popup's ⇥) another
#                                           than PANE's: at that space's end,
#                                           in its root (⌘⇧R's half: space.sh
#                                           new-tab agent, SPACE_LAUNCH=<id>)
#   agent-launch.sh open ID TARGET DIR      new-window -a -t TARGET -c DIR for
#                                           agent ID, born the agent's way;
#                                           prints the new window id (space.sh
#                                           new-tab calls this for SPACE_LAUNCH)
#
# THE TABLE. One row per agent: id, its ⌘E letter (fixed, so muscle memory
# holds; a free letter is a new row), the command ZSH_AUTOSTART runs (.zshrc
# evals it in the tab's interactive zsh, so quitting it leaves a prompt, as a
# ⌘R tab), the @agent_kind it is born with, and its @agent_harness (the
# sidebar's logo, agent-ui model.rs HARNESSES; - for none).
#   kind set:  an agent the hooks know (claude, codex, opencode, grok, agy,
#              copilot; abby is claude on its own config dir). Born an agent
#              exactly as ⌘R's claude: `#{@agent_birth} KIND` in the
#              new-window's own command list (agent-birth.conf), so the
#              watcher and hooks take over. (agy is Google's Antigravity CLI,
#              which replaced the Gemini CLI.)
#   kind -:    no hooks (cursor-agent, aider). NOT born an
#              agent: the watcher knows no such process (AGENT_PAT), so a
#              birth would show an idle "New Session" agent for the 30 s
#              BIRTH GRACE and then collect it back into a shell. The tab is
#              a shell from the start (its row in the sidebar, its letter),
#              with @agent_harness set to the agent's id, which no GC touches
#              on a window with no agent state; the command unsets it again
#              when the agent exits, so the shell left behind is a plain one.
# Ollama is not a coding agent: not listed.
#
# INSTALLED means `command -v` finds the command's binary in tmux's PATH
# (tmux.conf.tmpl) plus the per-user dirs a login shell adds and tmux's PATH
# lacks (~/.grok/bin: grok's installer; the rest are already there). A pyenv
# shim (aider) counts as installed. Tests set LAUNCH_PATH (in place of tmux's
# PATH) and LAUNCH_HOME (in place of $HOME, for those dirs); only the check
# uses them.
#
# @launch_agents: "LETTER:ID:COMMAND ..." in table order, the installed ones
# only (`c:claude:claude a:abby:abby ... u:cursor:cursor-agent`), the popup's
# rows (agent-ui launch.rs parse_agents). @launch_count: how many, the
# popup's height (spaces.conf's User27). Each written only when it changed.
#
# Messages go to the client that pressed the key through notify.sh
# (@notify_sh; tests point it at a stand-in): `launch: agy not installed`
# (an agent gone since the last refresh: the popup still listed it),
# `launch: nothing on b`.

set -uo pipefail

TMUX_BIN="${LAUNCH_TMUX_BIN:-tmux}"
t() { "$TMUX_BIN" "$@"; }
US=$'\x1f'

AGENTS=(
  # id        letter  command        kind      harness
  "claude     c       claude         claude    -"
  "abby       a       abby           claude    abby"
  "codex      x       codex          codex     -"
  "opencode   o       opencode       opencode  -"
  "grok       g       grok           grok      -"
  "agy        y       agy            agy       -"
  "cursor     u       cursor-agent   -         cursor"
  "copilot    p       copilot        copilot   -"
  "aider      d       aider          -         aider"
)

DETECT_HOME="${LAUNCH_HOME:-$HOME}"
DETECT_PATH="${LAUNCH_PATH:-$PATH}:$DETECT_HOME/.grok/bin:$DETECT_HOME/.local/bin:$DETECT_HOME/.pyenv/shims:$DETECT_HOME/.bun/bin:$DETECT_HOME/.cargo/bin"

# row_by FIELD VALUE: the row whose FIELD (1 id, 2 letter) is VALUE, split into
# R_ID R_KEY R_CMD R_KIND R_HARNESS. Fails when there is none.
row_by() {
  local f="$1" v="$2" r id key cmd kind harness
  for r in "${AGENTS[@]}"; do
    read -r id key cmd kind harness <<<"$r"
    if { [[ "$f" == 1 && "$id" == "$v" ]] || [[ "$f" == 2 && "$key" == "$v" ]]; }; then
      R_ID="$id" R_KEY="$key" R_CMD="$cmd" R_KIND="$kind" R_HARNESS="$harness"
      return 0
    fi
  done
  return 1
}

installed() {
  local PATH="$DETECT_PATH"
  command -v "${1%% *}" >/dev/null 2>&1
}

# The installed set as @launch_agents spells it.
installed_set() {
  local r id key cmd kind harness out=""
  for r in "${AGENTS[@]}"; do
    read -r id key cmd kind harness <<<"$r"
    installed "$cmd" && out+="${out:+ }$key:$id:$cmd"
  done
  printf '%s' "$out"
}

cmd_refresh() {
  local now was n=0
  local -a words=()
  now="$(installed_set)"
  [[ -n "$now" ]] && read -ra words <<<"$now"
  n=${#words[@]}
  was="$(t show -gqv @launch_agents 2>/dev/null)"
  [[ "$now" == "$was" ]] || t set -g @launch_agents "$now" 2>/dev/null
  was="$(t show -gqv @launch_count 2>/dev/null)"
  [[ "$n" == "$was" ]] || t set -g @launch_count "$n" 2>/dev/null
  return 0
}

cmd_list() {
  local r id key cmd kind harness
  for r in "${AGENTS[@]}"; do
    read -r id key cmd kind harness <<<"$r"
    installed "$cmd" && printf '%s %s %s\n' "$id" "$key" "$cmd"
  done
  return 0
}

# say CLIENT KIND TEXT: "launch: TEXT" to CLIENT, through notify.sh.
say() {
  local client="$1" kind="$2" n
  local -a cmd
  shift 2
  # A command line, as tmux runs it ("zsh -f /path" in tests).
  n="$(t show -gqv @notify_sh 2>/dev/null)"
  [[ -n "$n" ]] || n="$HOME/.config/tmux/scripts/notify.sh"
  read -ra cmd <<<"$n"
  if [[ -x "${cmd[0]}" ]] || { ((${#cmd[@]} > 1)) && command -v "${cmd[0]}" >/dev/null 2>&1; }; then
    "${cmd[@]}" ${client:+-c "$client"} -k "$kind" -- "launch: $*"
  else
    t display-message ${client:+-c "$client"} -- "launch: $*" 2>/dev/null
  fi
  return 0
}

focus() { command -v notifyutil >/dev/null 2>&1 && notifyutil -p com.mackhaymond.tmux.focus >/dev/null 2>&1; return 0; }

# resolve LETTER CLIENT: the row for LETTER, installed, else say why and fail.
resolve() {
  local letter="$1" client="$2"
  if ! row_by 2 "$letter"; then
    say "$client" warn "nothing on $letter"
    return 1
  fi
  if ! installed "$R_CMD"; then
    say "$client" warn "$R_ID not installed"
    cmd_refresh
    return 1
  fi
  return 0
}

# open_window TARGET DIR: agent R_* in a new window after TARGET, at DIR; the
# new window id on stdout. One tmux call: the birth (or the hookless harness
# mark) rides the new-window's own command list, which makes the new window
# its target, so the tab is never drawn as anything else first.
open_window() {
  local target="$1" dir="$2" auto="$R_CMD"
  local -a cmd=(new-window -a -t "$target" -c "$dir" -P -F '#{window_id}')
  if [[ "$R_KIND" == - ]]; then
    auto="{ $R_CMD; } always { tmux set-option -uw -t \"\$TMUX_PANE\" @agent_harness 2>/dev/null; }"
    cmd+=(-e "ZSH_AUTOSTART=$auto" ";" set-option -w @agent_harness "$R_HARNESS")
  else
    cmd+=(-e "ZSH_AUTOSTART=$auto" ";" run-shell -C "#{?@agent_birth,#{@agent_birth} $R_KIND,}")
    [[ "$R_HARNESS" != - ]] && cmd+=(";" set-option -w @agent_harness "$R_HARNESS")
  fi
  local wid
  wid="$(t "${cmd[@]}" 2>/dev/null)"
  wid="${wid%%$'\n'*}"
  [[ "$wid" == @* ]] || return 1
  printf '%s\n' "$wid"
}

cmd_go() {
  local letter="${1:-}" client="${2:-}" pane="${3:-}" space="${4:-}" wid dir sid new
  resolve "$letter" "$client" || return 0
  IFS="$US" read -r wid dir sid <<<"$(t display-message -p -t "$pane" "#{window_id}${US}#{?@tab_dir,#{E:@tab_dir},#{pane_current_path}}${US}#{session_id}" 2>/dev/null)"
  if [[ -n "$space" && "$space" != "$sid" ]]; then
    in_space "$space" "$client"
    return 0
  fi
  [[ "$wid" == @* ]] || { say "$client" warn "no tab to open beside"; return 0; }
  [[ -d "$dir" ]] || dir="$HOME"
  new="$(open_window "$wid" "$dir")" || { say "$client" error "could not open $R_ID"; return 0; }
  [[ -n "$client" ]] && t switch-client -c "$client" -t "$new" 2>/dev/null
  focus
}

# in_space SPACE CLIENT: agent R_* at the END of space SPACE ($N), in its
# root folder, CLIENT switched to it: exactly ⌘⇧R's pick (space.sh new-tab
# agent), which hands any agent but claude back to `open` (SPACE_LAUNCH), so
# its birth stays this script's. space.sh says what went wrong itself (a
# hidden session, a space closed meanwhile); its exit and output are dropped
# (run-shell -b would show them in the pane's view mode).
in_space() {
  local space="$1" client="$2" s
  local -a cmd
  [[ "$space" =~ ^\$[0-9]+$ ]] || { say "$client" warn "no space $space"; return 0; }
  s="$(t show -gqv @space_sh 2>/dev/null)"
  [[ -n "$s" ]] || s="$HOME/.config/tmux/scripts/space.sh"
  read -ra cmd <<<"$s"   # a command line, as tmux runs it ("bash /path" in tests)
  SPACE_LAUNCH="$R_ID" "${cmd[@]}" new-tab agent "$space" "$client" >/dev/null 2>&1
  return 0
}

cmd_open() {
  local id="${1:-}" target="${2:-}" dir="${3:-$HOME}"
  row_by 1 "$id" || { echo "agent-launch: no agent '$id'" >&2; return 1; }
  open_window "$target" "$dir"
}

[[ -n "${TMUX:-}${LAUNCH_TMUX_BIN:-}" ]] || [[ "${1:-}" == list ]] || {
  echo "agent-launch: not inside tmux" >&2
  exit 1
}

# refresh and go are what tmux's run-shell runs: they always exit 0
# (run-shell -b shows a nonzero exit in the pane's view mode).
case "${1:-}" in
  refresh) cmd_refresh; exit 0 ;;
  list) cmd_list ;;
  go) shift; cmd_go "$@"; exit 0 ;;
  open) shift; cmd_open "$@" ;;
  *)
    echo "usage: agent-launch.sh refresh | list | go LETTER CLIENT PANE [SPACE] | open ID TARGET DIR" >&2
    exit 2
    ;;
esac
