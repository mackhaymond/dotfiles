#!/usr/bin/env bash
#
# SPACES (docs/spaces.md): a space is a tmux session, an agent is a window
# whose @agent_state is non-empty, every other window is a shell. One shell
# per space is its HOME (@space_home 1): the space's main shell, its lowest
# window, which cannot be closed and respawns when its shell exits, so a space
# outlives its last tab. This script holds the logic behind
# the spaces keys that a tmux format cannot do on its own (counting, wrapping,
# validating a remembered window, the registry). The keys and the status
# line live in spaces.conf; the hidden-session list lives in tmux.conf
# (@space_hidden_sessions).
#
#   sync [load|reconcile]     mark hidden sessions (@space_hidden 1) and number
#                             the visible ones (@space_index: main = 1, then
#                             registry order). With @spaces on it also gives
#                             every visible space exactly one live home shell
#                             with its dashboard pane above it (space-dash.sh)
#                             and keeps the registry (and each space's colour,
#                             @space_color) in step. `load` (config
#                             load) and `reconcile` also recreate registered
#                             spaces that are missing and restore their folder.
#   (agent/shell/next/prev/toggle take an optional trailing CLIENT, for
#    messages; all refuse a hidden session)
#   agent N SID               go to agent N of session SID (1-based ordinal
#                             among windows with @agent_state, by window_index;
#                             the agent sidebar numbers agents the same way)
#   shell N SID               go to shell N of SID (same ordinal, among shells,
#                             home = 1; no key runs it since Option-N was
#                             dropped: shells are reached by letter, ⌥W)
#   next|prev SID             on an agent: next / previous agent; on a shell:
#                             next / previous shell; wrapping
#   respawn-home PANE         the pane-died hook: a home shell (or dashboard)
#                             that exited comes back in place (at most every 2 s)
#   toggle SID                agent <-> last-visited shell of this space (a
#                             new shell at the space folder if there is none);
#                             from a shell, back to the last-visited agent
#   new-space CLIENT          folder picker (run inside display-popup), then
#                             create or reuse a session for that folder and
#                             switch CLIENT to it
#   open-dir DIR [CLIENT]     the same without the picker
#   pick-tab shell|agent CLIENT
#                             which space? (run inside display-popup): the
#                             spaces in ⌃N order, then zoxide's folders; a
#                             pick (or a typed folder) runs new-tab
#   new-tab shell|agent TARGET [CLIENT]
#                             a new shell / claude tab at the END of space
#                             TARGET (a session id, $N or N) at its folder,
#                             and switch CLIENT to it. TARGET may be a folder:
#                             its space (open-dir's create-or-reuse); a shell
#                             in a space made just now is its home, no extra
#                             tab. Never renames, closes or reorders a space
#   preview-tab KEY           pick-tab's preview of one row
#   close SID CLIENT         close a space for good: every tab through
#                             @space_closer (closed-tabs.sh, so CMD+Z can bring
#                             it back), unregister, kill the session. Never main
#   move-menu WID CLIENT      menu of spaces; picking one runs move-to
#   move-to WID SID CLIENT    move window WID to the end of space SID
#
# Outside tmux ($TMUX empty) everything but `preview` refuses: a plain `tmux`
# there reaches the DEFAULT server, whatever server the caller had in mind.
#
# REGISTRY (@spaces on only): @space_registry, default
# ~/.local/state/tmux-spaces/spaces.tsv, one "name<TAB>folder<TAB>colour" line
# per space in tab order (a two-column line from before the colour gets one). It is what survives a server restart that resurrect does not
# cover: empty spaces, the order, the folder. Written under a mkdir lock.
#
# Cost: every key is one bash fork plus one list call and one action call to
# tmux. Callers pass '#{session_id}' so nothing has to be asked twice; a
# script calling new-window must pass -c itself (a CLI new-window would use
# this script's cwd, not the session's path).

set -uo pipefail

TMUX_BIN="${SPACE_TMUX_BIN:-tmux}"
t() { "$TMUX_BIN" "$@"; }

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
US=$'\x1f'
# Centred on the whole WezTerm row while a sidebar sits beside the client
# (tmux.conf.tmpl's @popup_x); tmux's own centre on a server without it.
POPUP_X='#{?#{@popup_x},#{E:@popup_x},#{popup_centre_x}}'

# Messages go to the client whose key ran us (CLIENT, passed by the binding as
# '#{client_name}'); without one tmux picks its "best" client, which may be a
# different terminal.
CLIENT=""
# The Rust sidebar redraws at once on this Darwin notification; posted after
# anything it shows changes (a space closed, created or recreated, a tab moved).
notify() { command -v notifyutil >/dev/null 2>&1 && notifyutil -p com.mackhaymond.tmux.focus >/dev/null 2>&1; return 0; }
# Messages go through notify.sh: a toast in the agent sidebar when one is open
# for the client, else tmux's own display-message (its header has the rest).
# NOTIFY_SH names another (tests); beside this script it is notify.sh
# (executable_notify.sh in the chezmoi source); with none, display-message.
#   notify_user [-c CLIENT] [-k info|ok|warn|error] [-t SECONDS] [--] TEXT
notify_user() {
  local n="${NOTIFY_SH:-}" d="${BASH_SOURCE[0]%/*}" c=
  [ "$d" = "${BASH_SOURCE[0]}" ] && d=.
  if [ -z "$n" ]; then
    for n in "$d/notify.sh" "$d/executable_notify.sh" "$HOME/.config/tmux/scripts/notify.sh"; do
      [ -f "$n" ] && break
    done
  fi
  if [ -x "$n" ]; then "$n" "$@"
  elif [ -f "$n" ]; then zsh -f "$n" "$@"
  else
    while [ $# -gt 0 ]; do
      case "$1" in
        -c|-k|-t) [ "$1" = -c ] && c="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --) shift; break ;;
        *) break ;;
      esac
    done
    ${NOTIFY_TMUX:-tmux} display-message ${c:+-c "$c"} -- "$*" 2>/dev/null
  fi
  return 0
}
# msg [-k KIND] TEXT: "space: TEXT" to CLIENT (else tmux's pick), through
# SPACE_TMUX_BIN's tmux.
msg() {
  local k=info
  [[ "${1:-}" == -k ]] && { k="$2"; shift 2; }
  NOTIFY_TMUX="${NOTIFY_TMUX:-$TMUX_BIN}" notify_user ${CLIENT:+-c "$CLIENT"} -k "$k" -- "space: $*"
}

# ── globals, registry, lock ────────────────────────────────────────────────
# One call, and it works with no session at all (config load on a cold start
# runs before the first session exists).
read_opts() {
  local o
  o="$(t display-message -p "#{@space_hidden_sessions}${US}#{@spaces}${US}#{@space_registry}${US}#{@space_restore_pending}${US}#{start_time}${US}#{@continuum-restore}${US}#{@space_closer}${US}#{@space_dash}")" || return 1
  IFS="$US" read -r HIDDEN_LIST SPACES_ON REG_OPT PENDING START_T CONT_RESTORE CLOSER_OPT DASH_OPT <<<"$o"
  REG="${REG_OPT:-${XDG_STATE_HOME:-$HOME/.local/state}/tmux-spaces/spaces.tsv}"
  DASH_CMD="${DASH_OPT:-$HOME/.config/tmux/scripts/space-dash.sh}"
}

is_hidden_name() { [[ " $HIDDEN_LIST " == *" $1 "* ]]; }

# ── colours ────────────────────────────────────────────────────────────────
# Every space has a stable colour (@space_color, a hex string), kept in the
# registry's third column so it survives a restart. main is always lavender;
# the others get the palette colour fewest registered spaces use, first in
# palette order on a tie, when they are registered. Catppuccin mocha accents
# that read on the dark theme, minus the state colours (red failed, yellow
# needs-*, green done), so a space's colour never reads as a state.
SPACE_MAIN_COLOR="#b4befe"
SPACE_PALETTE=("#cba6f7" "#94e2d5" "#f5c2e7" "#74c7ec" "#fab387" "#f2cdcd" "#89dceb" "#f5e0dc" "#89b4fa")
# mauve teal pink sapphire peach flamingo sky rosewater blue
is_color() { [[ "$1" =~ ^#[0-9a-fA-F]{6}$ ]]; }
# The colour for registry entry $1 (its name; main is fixed), from the others'.
pick_color() {
  [[ "$1" == "main" ]] && { printf '%s' "$SPACE_MAIN_COLOR"; return 0; }
  local best="" bestn=-1 c k n
  for c in "${SPACE_PALETTE[@]}"; do
    n=0
    for k in "${!R_NAME[@]}"; do
      [[ "${R_NAME[k]}" != "$1" && "${R_COLOR[k]:-}" == "$c" ]] && n=$((n + 1))
    done
    if ((bestn < 0 || n < bestn)); then best="$c" bestn=$n; fi
  done
  printf '%s' "$best"
}

R_NAME=() R_PATH=() R_COLOR=()
reg_load() {
  R_NAME=() R_PATH=() R_COLOR=()
  [[ -r "$REG" ]] || return 0
  local n p c
  while IFS=$'\t' read -r n p c; do
    [[ -n "$n" ]] || continue
    R_NAME+=("$n") R_PATH+=("$p") R_COLOR+=("$c")
  done <"$REG"
}
reg_find() {
  local i
  for i in "${!R_NAME[@]}"; do [[ "${R_NAME[i]}" == "$1" ]] && { echo "$i"; return 0; }; done
  echo -1
}
reg_del() {
  local i="$1" k
  local -a n=() p=() c=()
  for k in "${!R_NAME[@]}"; do
    ((k == i)) && continue
    n+=("${R_NAME[k]}") p+=("${R_PATH[k]}") c+=("${R_COLOR[k]:-}")
  done
  R_NAME=("${n[@]+"${n[@]}"}") R_PATH=("${p[@]+"${p[@]}"}") R_COLOR=("${c[@]+"${c[@]}"}")
}
reg_save() {
  local tmp="$REG.tmp.$$" i
  {
    for i in "${!R_NAME[@]}"; do printf '%s\t%s\t%s\n' "${R_NAME[i]}" "${R_PATH[i]}" "${R_COLOR[i]:-}"; done
  } >"$tmp" && mv -f "$tmp" "$REG"
}

# mkdir is the lock (macOS has no flock(1)); one older than 15 s is a crashed
# holder and is broken. The holder writes a token into it and only ever
# removes a lock that still holds its own token, so a holder whose lock was
# broken never removes the next one's. Held only for short stretches (a sync,
# close's registry edit), never across slow work. Released on exit.
LOCKED="" LOCK_TOKEN=""
reg_lock() {
  local lock="$REG.lock" i=0 m
  mkdir -p "$(dirname "$REG")" 2>/dev/null || return 1
  until mkdir "$lock" 2>/dev/null; do
    if ((i % 20 == 19)); then
      m="$(stat -f %m "$lock" 2>/dev/null || stat -c %Y "$lock" 2>/dev/null || echo 0)"
      (($(date +%s) - m > 15)) && rm -rf "$lock" 2>/dev/null
    fi
    i=$((i + 1))
    ((i >= 200)) && return 1
    sleep 0.05
  done
  LOCK_TOKEN="$$.$RANDOM.$(date +%s)"
  printf '%s\n' "$LOCK_TOKEN" >"$lock/owner"
  LOCKED="$lock"
  trap 'reg_unlock' EXIT
}
reg_unlock() {
  if [[ -n "$LOCKED" && "$(cat "$LOCKED/owner" 2>/dev/null)" == "$LOCK_TOKEN" ]]; then
    rm -rf "$LOCKED" 2>/dev/null
  fi
  LOCKED=""
}

# ── home windows ───────────────────────────────────────────────────────────
# Options a home carries. The WINDOW: @space_home, its name. The PANE (the
# home shell itself; other panes split into the window are ordinary):
# @space_home_pane and remain-on-exit, so when its shell exits (`exit`,
# Ctrl-D) the pane stays, dead, instead of closing (for a space's last window
# that would end the session), and spaces.conf's pane-died hook respawns it
# (respawn-home); a sync respawns any it finds dead too. The window-level
# remain-on-exit an earlier version set is cleared.
home_opts() {
  local w="$1" p="$2"
  add set -w -t "$w" @space_home 1 ";" set -w -t "$w" automatic-rename off \
    ";" set -w -t "$w" allow-rename off ";" set -wu -t "$w" remain-on-exit ";" rename-window -t "$w" home
  home_pane_opts "$p"
}
home_pane_opts() {
  add set -p -t "$1" @space_home_pane 1 ";" set -p -t "$1" remain-on-exit on
}
# Live, and a shell rather than the landing screen an earlier home ran.
home_ok() { [[ "$1" == "0" && "$2" != *space*home* ]]; }   # pane_dead, pane_start_command

# The DASHBOARD (space-dash.sh): the home window's top pane, full width,
# DASH_ROWS high, above the home shell. It carries @space_dash_pane, a
# pane-level remain-on-exit (the pane-died hook respawns it, as the home
# shell) and the title space-dash, which is what resurrect brings back (it
# restores titles, not options or this command): a restored home window's
# space-dash pane is respawned as the dashboard, never adopted as the shell.
# It is never the active pane: spaces.conf's window-pane-changed hook hands
# focus straight back to the pane below it.
DASH_TITLE=space-dash
DASH_ROWS=9
dash_pane_opts() { add set -p -t "$1" @space_dash_pane 1 ";" set -p -t "$1" remain-on-exit on ";" select-pane -t "$1" -T "$DASH_TITLE"; }
dash_ok() { [[ "$1" == "0" && ( "$2" == *space-dash* || "$2" == "$DASH_CMD" ) ]]; }   # pane_dead, pane_start_command

# ── sync ────────────────────────────────────────────────────────────────────
cmd_sync() {
  local mode="${1:-normal}"
  read_opts || return 0
  local now on=0 struct=0 cold=0
  now="$(date +%s)"
  [[ "$SPACES_ON" == "on" ]] && on=1 && struct=1
  [[ -n "$START_T" ]] && ((now - START_T < 90)) && cold=1
  # A cold start with continuum's auto-restore on: resurrect is about to
  # rebuild sessions window by window, matching them by index, and treats a
  # server with more than one pane as "not from scratch". A home window
  # inserted now would shift its indexes, so nothing structural happens until
  # restore-wait sees the restore finish and runs `sync reconcile`.
  if ((on && cold)) && [[ "$CONT_RESTORE" == "on" && ! -f "$HOME/tmux_no_auto_restore" && "$PENDING" != "done" ]]; then
    if [[ -z "$PENDING" ]]; then
      t set -g @space_restore_pending "$now"
      PENDING="$now"
      (bash "$SELF" restore-wait </dev/null >/dev/null 2>&1 &)
    fi
  fi
  if [[ "$PENDING" =~ ^[0-9]+$ ]]; then
    if ((now - PENDING < 900)); then struct=0; else t set -g @space_restore_pending done; fi
  fi
  [[ "$mode" == "load" ]] && mode=reconcile
  ((struct)) && { reg_lock || struct=0; }

  local srows
  srows="$(t list-sessions -F "#{session_id}${US}#{@space_hidden}${US}#{@space_index}${US}#{@space_key}${US}#{session_windows}${US}#{@space_closing}${US}#{@space_reg}${US}#{@space_color}${US}#{session_path}${US}#{session_name}")" || return 0
  local clients
  clients="$(t list-clients -F '#{client_name}' 2>/dev/null)"
  # Sessions by numeric id (= creation order; renames are matched in this
  # order, so a renamed main is seen before a new one takes its name).
  local -a S_ID=() S_HID=() S_IDX=() S_KEY=() S_NWIN=() S_CLOSING=() S_REG=() S_COLOR=() S_PATH=() S_NAME=()
  local id hid idx key nwin closing reg color path name
  while IFS="$US" read -r id hid idx key nwin closing reg color path name; do
    [[ -n "$id" ]] || continue
    S_ID+=("$id") S_HID+=("$hid") S_IDX+=("$idx") S_KEY+=("$key") S_NWIN+=("$nwin") S_CLOSING+=("$closing") S_REG+=("$reg") S_COLOR+=("$color") S_PATH+=("$path") S_NAME+=("$name")
  done < <(printf '%s\n' "$srows" | awk -F"$US" 'NF { print substr($1, 2) + 0 FS $0 }' | sort -t"$US" -k1,1n | cut -d"$US" -f2-)

  local -a args=()
  add() { ((${#args[@]})) && args+=(";"); args+=("$@"); }
  local i
  reg_load
  local -a recreate=() repath=()
  if ((struct)); then
    local changed=0 ri rn
    for i in "${!S_ID[@]}"; do
      name="${S_NAME[i]}" reg="${S_REG[i]}"
      if is_hidden_name "$name"; then
        if [[ -n "$reg" ]]; then
          ri="$(reg_find "$reg")"; ((ri >= 0)) && { reg_del "$ri"; changed=1; }
          add set-option -u -t "${S_ID[i]}" @space_reg
        fi
        continue
      fi
      # Being closed (cmd_close has unregistered it): never re-add it.
      [[ "${S_CLOSING[i]}" == "1" ]] && continue
      # Renamed since it was registered: same entry, same place, new name.
      if [[ -n "$reg" && "$reg" != "$name" ]]; then
        ri="$(reg_find "$reg")"
        if ((ri >= 0)) && (($(reg_find "$name") < 0)); then R_NAME[ri]="$name"; changed=1; fi
      fi
      ri="$(reg_find "$name")"
      if ((ri < 0)); then
        R_NAME+=("$name") R_PATH+=("${S_PATH[i]}") R_COLOR+=("")
        R_COLOR[${#R_NAME[@]} - 1]="$(pick_color "$name")"; changed=1
      elif [[ "${R_PATH[ri]}" != "${S_PATH[i]}" ]]; then
        # Normally the live folder wins; after a restart the registry does
        # (resurrect recreates a session with its first pane's folder), and
        # in the first 90 s nothing overwrites it before the load-time
        # reconcile has had its say.
        if [[ "$mode" == "reconcile" && -d "${R_PATH[ri]}" ]]; then repath+=("${S_ID[i]}" "${R_PATH[ri]}")
        elif ((!cold)); then R_PATH[ri]="${S_PATH[i]}"; changed=1; fi
      fi
      [[ "$reg" == "$name" ]] || add set-option -t "${S_ID[i]}" @space_reg "$name"
    done
    # Registered but not running.
    for ((ri = ${#R_NAME[@]} - 1; ri >= 0; ri--)); do
      rn="${R_NAME[ri]}"
      local live=0
      for i in "${!S_NAME[@]}"; do [[ "${S_NAME[i]}" == "$rn" ]] && { live=1; break; }; done
      ((live)) && continue
      if [[ "$mode" == "reconcile" ]]; then
        recreate=("$rn" "${R_PATH[ri]}" "${recreate[@]+"${recreate[@]}"}")
      elif ((!cold)); then
        # Killed some other way than `close` (a kill-session): gone for good.
        # Never in the first 90 s, when the first syncs may run before the
        # load-time one has recreated anything.
        reg_del "$ri"; changed=1
      fi
    done
    # Colours: a line from before the colour column (or a hand edit) gets
    # one now, in registry order; main's is fixed.
    for ri in "${!R_NAME[@]}"; do
      if [[ "${R_NAME[ri]}" == "main" ]]; then
        [[ "${R_COLOR[ri]:-}" == "$SPACE_MAIN_COLOR" ]] || { R_COLOR[ri]="$SPACE_MAIN_COLOR"; changed=1; }
      elif ! is_color "${R_COLOR[ri]:-}"; then
        R_COLOR[ri]="$(pick_color "${R_NAME[ri]}")"; changed=1
      fi
    done
    ((changed)) && reg_save
  fi

  # Numbers: main first, then registry order, then anything unregistered by
  # id. prefix 1-9 / the Ctrl+N chords pick spaces by these numbers.
  local n=2 rn2 k
  local -a order=()
  for i in "${!S_ID[@]}"; do
    [[ "${S_NAME[i]}" == "main" ]] && ! is_hidden_name main && order+=("$i")
  done
  for rn2 in "${R_NAME[@]+"${R_NAME[@]}"}"; do
    [[ "$rn2" == "main" ]] && continue
    for i in "${!S_ID[@]}"; do [[ "${S_NAME[i]}" == "$rn2" ]] && order+=("$i"); done
  done
  for i in "${!S_ID[@]}"; do
    [[ "${S_NAME[i]}" == "main" ]] && continue
    is_hidden_name "${S_NAME[i]}" && continue
    (($(reg_find "${S_NAME[i]}") >= 0)) && continue
    order+=("$i")
  done
  local want ri2
  for i in "${!S_ID[@]}"; do
    if is_hidden_name "${S_NAME[i]}"; then
      [[ "${S_HID[i]}" == "1" ]] || add set-option -t "${S_ID[i]}" @space_hidden 1
      [[ -z "${S_IDX[i]}" ]] || add set-option -u -t "${S_ID[i]}" @space_index
      [[ -z "${S_KEY[i]}" ]] || add set-option -u -t "${S_ID[i]}" @space_key
      [[ -z "${S_COLOR[i]}" ]] || add set-option -u -t "${S_ID[i]}" @space_color
    else
      [[ -z "${S_HID[i]}" ]] || add set-option -u -t "${S_ID[i]}" @space_hidden
      # @space_color: the registry's (an unregistered space keeps what it
      # has until it is registered; main's is fixed).
      want=""
      ri2="$(reg_find "${S_NAME[i]}")"
      ((ri2 >= 0)) && is_color "${R_COLOR[ri2]:-}" && want="${R_COLOR[ri2]}"
      [[ "${S_NAME[i]}" == "main" ]] && want="$SPACE_MAIN_COLOR"
      [[ -n "$want" && "${S_COLOR[i]}" != "$want" ]] && add set-option -t "${S_ID[i]}" @space_color "$want"
    fi
  done
  # @space_key: the label the status line draws for the space (its tab and
  # its alert chip): the key that reaches it. Ctrl+N today, so only spaces
  # 1-9 have one; past 9 it is unset (no key to show).
  n=1
  for k in "${order[@]+"${order[@]}"}"; do
    [[ "${S_IDX[k]}" == "$n" ]] || add set-option -t "${S_ID[k]}" @space_index "$n"
    if ((n <= 9)); then
      [[ "${S_KEY[k]}" == "⌃$n" ]] || add set-option -t "${S_ID[k]}" @space_key "⌃$n"
    else
      [[ -z "${S_KEY[k]}" ]] || add set-option -u -t "${S_ID[k]}" @space_key
    fi
    n=$((n + 1))
  done

  if ((struct)); then
    sync_homes
  fi
  # Redraw EVERY client's tabs (a bare refresh-client reaches only one), in the
  # same call, and only when a number or marker actually changed.
  if ((${#args[@]})); then
    local c
    while IFS= read -r c; do [[ -n "$c" ]] && add refresh-client -S -t "$c"; done <<<"$clients"
    run_args
  fi
  if ((struct)); then
    add_panes
    # One call each: a failing command ends a tmux command list.
    for ((i = 0; i < ${#recreate[@]}; i += 2)); do
      local d="${recreate[i + 1]}"
      [[ -d "$d" ]] || d="$HOME"
      args=(new-session -d -s "${recreate[i]}" -c "$d" -n home)
      home_opts "=${recreate[i]}:^" "=${recreate[i]}:^"
      args+=(";" set-option -t "=${recreate[i]}" @space_reg "${recreate[i]}")
      t "${args[@]}" >/dev/null 2>&1
    done
    # attach-session -c is the one command that sets a session's folder. From
    # here it then fails ("not a terminal"), after the folder is set.
    for ((i = 0; i < ${#repath[@]}; i += 2)); do
      t attach-session -t "${repath[i]}" -c "${repath[i + 1]}" </dev/null >/dev/null 2>&1
    done
    ((${#recreate[@]})) && { notify; renumber_after_recreate; }
  fi
  return 0
}

# A recreated session was born after the numbers above were handed out; its
# own session-created sync waits on our lock, so number it now.
renumber_after_recreate() {
  reg_unlock
  cmd_sync normal
}

# Every visible space: exactly one home, alive, as its lowest window. Adds to
# `args` (the caller runs them).
#   1. a window with @space_home: the lowest one is it; its home pane (the
#      @space_home_pane one, else its first pane) is respawned if dead, and
#      the window is moved back in front if something landed before it (a
#      closed-tabs reopen inserts with -b). Any other loses the markers (two
#      syncs raced) but keeps its shells
#   2. else a shell window NAMED home: one a resurrect restore brought back
#      (resurrect keeps names and slots, not window options); adopted
#   3. else the session's only window, if it is not an agent: a new space's
#      first shell becomes its home rather than gaining a second one
#   4. else a new shell at the space folder, inserted as the lowest window
#   Each home also has exactly one dashboard pane (DASH_TITLE above): a pane
#   marked @space_dash_pane, else one titled space-dash (a resurrect restore)
#   is it, respawned as the dashboard if it is not running one; a second
#   marked one is killed; a home without one gets one (DASH_ADD, after the
#   caller has run `args`, since a new pane's id is needed to mark it). The
#   dashboard is never the home shell: the home pane is the first pane that
#   is not the dashboard; a home left with only its dashboard gets a new
#   shell below it (SHELL_ADD).
# One row per PANE (list-panes -a, in window then pane order), so the first
# row of a window is its first pane.
DASH_ADD=() SHELL_ADD=()
sync_homes() {
  local hrows
  DASH_ADD=() SHELL_ADD=() HOME_NEW=()
  hrows="$(t list-panes -a -f '#{||:#{@space_home},#{||:#{==:#{window_name},home},#{==:#{session_windows},1}}}' \
    -F "#{session_id}${US}#{window_id}${US}#{@space_home}${US}#{?#{@agent_state},a,}${US}#{pane_dead}${US}#{?#{==:#{window_name},home},n,}${US}#{window_start_flag}${US}#{@space_home_pane}${US}#{pane_current_command}${US}#{pane_id}${US}#{@space_dash_pane}${US}#{?#{==:#{pane_title},$DASH_TITLE},t,}${US}#{pane_start_command}" 2>/dev/null)"
  local i s sid wid home agent dead named first hpane cmd pane dpane titled start spath
  # The window being read (w_*), and what it turned out to be.
  local lastw w_home w_agent w_named w_first fp fpstate fpcmd hp hpstate dp dpstate dpmarked
  local -a w_xdash=() w_xhome=()
  local keep keepfirst k_hp k_hpstate k_fp k_fpstate k_dp k_dpstate k_dpmarked
  local cand cfirst cpane cstate c_dp c_dpstate c_dpmarked
  local only ofirst opane ostate o_dp o_dpstate o_dpmarked
  local -a kill_dash=()
  # Classify the window just read; see the list above.
  finish_window() {
    [[ -n "$lastw" ]] || return 0
    if [[ "$w_home" == "1" ]]; then
      if [[ -z "$keep" ]]; then
        keep="$lastw" keepfirst="$w_first" k_hp="$hp" k_hpstate="$hpstate" k_fp="$fp" k_fpstate="$fpstate"
        k_dp="$dp" k_dpstate="$dpstate" k_dpmarked="$dpmarked"
        local x
        for x in "${w_xhome[@]+"${w_xhome[@]}"}"; do add set -pu -t "$x" @space_home_pane ";" set -pu -t "$x" remain-on-exit; done
        kill_dash+=("${w_xdash[@]+"${w_xdash[@]}"}")
      else
        # A second home (two syncs raced): it loses the markers, keeps its
        # shells, and its dashboard goes.
        add set -wu -t "$lastw" @space_home ";" set -wu -t "$lastw" remain-on-exit ";" set -wu -t "$lastw" allow-rename \
          ";" set -wu -t "$lastw" automatic-rename
        [[ -n "$hp" ]] && add set -pu -t "$hp" @space_home_pane ";" set -pu -t "$hp" remain-on-exit
        local x
        for x in "${w_xhome[@]+"${w_xhome[@]}"}"; do add set -pu -t "$x" @space_home_pane ";" set -pu -t "$x" remain-on-exit; done
        [[ "$dpmarked" == "1" ]] && kill_dash+=("$dp")
        kill_dash+=("${w_xdash[@]+"${w_xdash[@]}"}")
      fi
    elif [[ -z "$w_agent" && -n "$w_named" && -z "$cand" && -n "$fp" ]] && [[ "${fpstate:0:1}" == "1" || "$fpcmd" =~ ^-?(zsh|bash|sh|fish|nu)$ ]]; then
      cand="$lastw" cfirst="$w_first" cpane="$fp" cstate="$fpstate" c_dp="$dp" c_dpstate="$dpstate" c_dpmarked="$dpmarked"
    elif [[ -z "$w_agent" && "${S_NWIN[i]}" == "1" && -n "$fp" ]]; then
      only="$lastw" ofirst="$w_first" opane="$fp" ostate="$fpstate" o_dp="$dp" o_dpstate="$dpstate" o_dpmarked="$dpmarked"
    fi
  }
  # The dashboard of home window $1 (pane $2, "dead+start" $3, marked $4):
  # make sure it is marked and running, or have one added.
  ensure_dash() {
    local w="$1" p="$2" st="$3" marked="$4"
    if [[ -z "$p" ]]; then DASH_ADD+=("$w" "${S_PATH[i]:-$HOME}"); return 0; fi
    [[ "$marked" == "1" ]] || dash_pane_opts "$p"
    dash_ok "${st:0:1}" "${st:1}" || add respawn-pane -k -t "$p" -c "${S_PATH[i]:-$HOME}" "$DASH_CMD"
  }
  for i in "${!S_ID[@]}"; do
    is_hidden_name "${S_NAME[i]}" && continue
    [[ "${S_CLOSING[i]}" == "1" ]] && continue
    sid="${S_ID[i]}" lastw=""
    keep="" keepfirst="" k_hp="" k_hpstate="" k_fp="" k_fpstate="" k_dp="" k_dpstate="" k_dpmarked=""
    cand="" cfirst="" cpane="" cstate="" c_dp="" c_dpstate="" c_dpmarked=""
    only="" ofirst="" opane="" ostate="" o_dp="" o_dpstate="" o_dpmarked=""
    kill_dash=()
    while IFS="$US" read -r s wid home agent dead named first hpane cmd pane dpane titled start; do
      [[ "$s" == "$sid" ]] || continue
      if [[ "$wid" != "$lastw" ]]; then
        finish_window
        lastw="$wid" w_home="$home" w_agent="$agent" w_named="$named" w_first="$first"
        fp="" fpstate="" fpcmd="" hp="" hpstate="" dp="" dpstate="" dpmarked="" w_xdash=() w_xhome=()
      fi
      if [[ "$dpane" == "1" ]]; then
        # Marked dashboards: the first is it (a titled one found earlier
        # yields), any other goes.
        if [[ "$dpmarked" != "1" ]]; then dp="$pane" dpstate="$dead$start" dpmarked=1
        else w_xdash+=("$pane"); fi
        continue
      fi
      if [[ -n "$titled" && "$hpane" != "1" && -z "$dp" ]]; then
        dp="$pane" dpstate="$dead$start" dpmarked=""
        continue
      fi
      [[ -z "$fp" ]] && fp="$pane" fpstate="$dead$start" fpcmd="$cmd"
      if [[ "$hpane" == "1" ]]; then
        if [[ -z "$hp" ]]; then hp="$pane" hpstate="$dead$start"; else w_xhome+=("$pane"); fi
      fi
    done <<<"$hrows"
    finish_window
    if [[ -n "$keep" ]]; then
      if [[ -z "$k_hp" && -n "$k_fp" ]]; then
        # No pane marked (a home from before panes were marked): its first.
        k_hp="$k_fp" k_hpstate="$k_fpstate"
        home_pane_opts "$k_hp"
        add set -wu -t "$keep" remain-on-exit
      fi
      if [[ -n "$k_hp" ]]; then
        # hpstate is pane_dead then the start command: "0..." is a live pane.
        home_ok "${k_hpstate:0:1}" "${k_hpstate:1}" || add respawn-pane -k -t "$k_hp" -c "${S_PATH[i]:-$HOME}"
      elif [[ -n "$k_dp" ]]; then
        SHELL_ADD+=("$k_dp" "${S_PATH[i]:-$HOME}")
      fi
      ensure_dash "$keep" "$k_dp" "$k_dpstate" "$k_dpmarked"
      local x
      for x in "${kill_dash[@]+"${kill_dash[@]}"}"; do add kill-pane -t "$x"; done
      [[ "$keepfirst" == "1" ]] || add move-window -b -s "$keep" -t "$sid:^"
      continue
    fi
    if [[ -z "$cand" && -n "$only" ]]; then
      cand="$only" cfirst="$ofirst" cpane="$opane" cstate="$ostate" c_dp="$o_dp" c_dpstate="$o_dpstate" c_dpmarked="$o_dpmarked"
    fi
    if [[ -n "$cand" ]]; then
      home_ok "${cstate:0:1}" "${cstate:1}" || add respawn-pane -k -t "$cpane" -c "${S_PATH[i]:-$HOME}"
      home_opts "$cand" "$cpane"
      ensure_dash "$cand" "$c_dp" "$c_dpstate" "$c_dpmarked"
      [[ "$cfirst" == "1" ]] || add move-window -b -s "$cand" -t "$sid:^"
    else
      # Made after `args` ran (add_panes), on its own: a command list stops at
      # its first failing command, and a home must not depend on the rest.
      HOME_NEW+=("$sid" "${S_PATH[i]:-$HOME}")
    fi
  done
}

# Run `args` (run_list: its arguments) as one tmux call. A command list stops at its first failing
# command (say a kill-pane or respawn-pane -k on a pane that vanished since
# it was read), and the rest must not be lost with it: on a failure, every
# command runs again on its own. They are all idempotent (options, renames,
# moves to the front, respawns, kills), so the ones that did run are harmless
# the second time.
run_args() { ((${#args[@]})) || return 0; run_list "${args[@]}"; }
# Append one command to the caller's `args` list (cmd_sync defines the same).
add() { ((${#args[@]})) && args+=(";"); args+=("$@"); }
run_list() {
  t "$@" >/dev/null 2>&1 && return 0
  local -a one=()
  local a
  for a in "$@"; do
    if [[ "$a" == ";" ]]; then
      ((${#one[@]})) && t "${one[@]}" >/dev/null 2>&1
      one=()
    else
      one+=("$a")
    fi
  done
  ((${#one[@]})) && t "${one[@]}" >/dev/null 2>&1
  return 0
}

# After `args` ran: the new homes, then the dashboards and shells sync_homes
# asked for. One call per window or pane (its id is needed to mark it); still
# under the registry lock, so no other sync adds a second. A dashboard is
# split only into a window that IS a home (@space_home, read back by id just
# before): a target that turned out to be some other window, an agent's, is
# never given one (no later sync would take it away). A window too small to
# split is left for the next sync.
HOME_NEW=()
add_panes() {
  local i p w
  local -a args=()
  for ((i = 0; i < ${#HOME_NEW[@]}; i += 2)); do
    w="$(t new-window -d -b -t "${HOME_NEW[i]}:^" -n home -c "${HOME_NEW[i + 1]}" -P -F '#{window_id}' 2>/dev/null)" || continue
    [[ "$w" == @* ]] || continue
    args=()
    home_opts "$w" "$w"
    run_args
    DASH_ADD+=("$w" "${HOME_NEW[i + 1]}")
  done
  HOME_NEW=()
  for ((i = 0; i < ${#DASH_ADD[@]}; i += 2)); do
    # (tmux answers a missing target with empty fields, not an error)
    w="$(t display-message -p -t "${DASH_ADD[i]}" '#{window_id}|#{@space_home}' 2>/dev/null)"
    [[ "$w" == @*"|1" ]] || continue
    p="$(t split-window -d -f -b -v -l "$DASH_ROWS" -t "${w%|*}" -c "${DASH_ADD[i + 1]}" -P -F '#{pane_id}' "$DASH_CMD" 2>/dev/null)" || continue
    [[ "$p" == %* ]] || continue
    run_list set -p -t "$p" @space_dash_pane 1 ";" set -p -t "$p" remain-on-exit on ";" select-pane -t "$p" -T "$DASH_TITLE"
  done
  for ((i = 0; i < ${#SHELL_ADD[@]}; i += 2)); do
    # Below a dashboard of a home, nowhere else.
    w="$(t display-message -p -t "${SHELL_ADD[i]}" '#{pane_id}|#{@space_dash_pane}|#{@space_home}' 2>/dev/null)"
    [[ "$w" == "${SHELL_ADD[i]}|1|1" ]] || continue
    p="$(t split-window -d -v -t "${SHELL_ADD[i]}" -c "${SHELL_ADD[i + 1]}" -P -F '#{pane_id}' 2>/dev/null)" || continue
    [[ "$p" == %* ]] || continue
    run_list set -p -t "$p" @space_home_pane 1 ";" set -p -t "$p" remain-on-exit on ";" select-pane -t "$p"
  done
}

# The pane-died hook (spaces.conf) for a home pane: back in place, in the space
# folder. Never more than once every 2 s, so a shell that dies at once cannot
# spin the server.
# The dashboard pane (@space_dash_pane) comes back the same way, running the
# dashboard again.
cmd_respawn_home() {
  local pane="$1" info home dash dead last path dcmd now
  info="$(t display-message -p -t "$pane" "#{@space_home_pane}${US}#{@space_dash_pane}${US}#{pane_dead}${US}#{@space_respawned}${US}#{session_path}${US}#{@space_dash}" 2>/dev/null)" || return 0
  IFS="$US" read -r home dash dead last path dcmd <<<"$info"
  [[ ("$home" == "1" || "$dash" == "1") && "$dead" == "1" ]] || return 0
  now="$(date +%s)"
  [[ "$last" =~ ^[0-9]+$ ]] && ((now - last < 2)) && sleep 2
  local -a cmd=()
  [[ "$dash" == "1" ]] && cmd=("${dcmd:-$HOME/.config/tmux/scripts/space-dash.sh}")
  t set -p -t "$pane" @space_respawned "$(date +%s)" ";" respawn-pane -t "$pane" -c "${path:-$HOME}" "${cmd[@]+"${cmd[@]}"}" 2>/dev/null
  return 0
}

# Runs detached from a cold-start sync: waits for continuum's auto-restore
# (continuum_restore.sh sleeps 1 s, then runs resurrect's restore.sh, whose
# post-restore hooks run inside it) to come and go, then reconciles.
cmd_restore_wait() {
  local quiet=0 waited=0
  sleep 3
  while ((waited < 600)); do
    if pgrep -f 'continuum_restore\.sh|tmux-resurrect[^ ]*/scripts/restore\.sh' >/dev/null 2>&1; then
      quiet=0
    else
      quiet=$((quiet + 1))
      ((quiet >= 2)) && break
    fi
    sleep 2
    waited=$((waited + 2))
  done
  t set -g @space_restore_pending done 2>/dev/null || return 0
  cmd_sync reconcile
}

# ── windows of one session ─────────────────────────────────────────────────
# Fills AGENTS and SHELLS (window ids, by window_index; the home window is a
# shell, the first), CUR (active window id), CUR_KIND (a|s), LAST_A / LAST_S
# (the session's remembered ids) and SPATH, all from ONE list-windows call.
load_session() {
  local sid="$1" id active kind la ls sp hid
  AGENTS=() SHELLS=() CUR="" CUR_KIND="" LAST_A="" LAST_S="" SPATH=""
  local out
  out="$(t list-windows -t "$sid" -F '#{window_id}|#{window_active}|#{?#{@agent_state},a,s}|#{@space_last_agent}|#{@space_last_shell}|#{session_path}|#{@space_hidden}')" || return 1
  # The bindings already refuse hidden sessions without forking; this is the
  # backstop for any other caller. Never select or create windows in agents,
  # stash, tasks, ... (status 2 = refused).
  if [[ "${out##*|}" == "1" ]]; then
    msg -k warn "not a space (hidden session)"
    return 2
  fi
  while IFS='|' read -r id active kind la ls sp hid; do
    [[ -n "$id" ]] || continue
    LAST_A="$la" LAST_S="$ls" SPATH="$sp"
    if [[ "$kind" == "a" ]]; then AGENTS+=("$id"); else SHELLS+=("$id"); fi
    if [[ "$active" == "1" ]]; then CUR="$id" CUR_KIND="$kind"; fi
  done <<<"$out"
}

contains() {
  local needle="$1"; shift
  local x
  for x in "$@"; do [[ "$x" == "$needle" ]] && return 0; done
  return 1
}

cmd_agent() {
  local n="$1" sid="$2"
  [[ "$n" =~ ^[1-9][0-9]*$ ]] || { msg -k warn "bad agent number '$n'"; return 1; }
  load_session "$sid" || return 1
  if ((n > ${#AGENTS[@]})); then
    msg -k warn "no agent $n here (${#AGENTS[@]} in this space)"
    return 0
  fi
  t select-window -t "${AGENTS[n - 1]}"
}

cmd_shell() {
  local n="$1" sid="$2"
  [[ "$n" =~ ^[1-9][0-9]*$ ]] || { msg -k warn "bad shell number '$n'"; return 1; }
  load_session "$sid" || return 1
  if ((n > ${#SHELLS[@]})); then
    msg -k warn "no shell $n here (${#SHELLS[@]} in this space)"
    return 0
  fi
  t select-window -t "${SHELLS[n - 1]}"
}

cmd_step() {
  local dir="$1" sid="$2"
  load_session "$sid" || return 1
  local -a ring=()
  local what
  # Kind-aware: a shell (home included) cycles the shells, an agent the agents.
  if [[ "$CUR_KIND" == "s" ]]; then ring=("${SHELLS[@]}") what=shells
  else ring=("${AGENTS[@]}") what=agents; fi
  local count=${#ring[@]}
  ((count)) || { msg -k warn "no $what in this space"; return 0; }
  local i target=""
  for i in "${!ring[@]}"; do
    [[ "${ring[i]}" == "$CUR" ]] || continue
    if [[ "$dir" == next ]]; then target="${ring[(i + 1) % count]}"
    else target="${ring[(i - 1 + count) % count]}"; fi
    break
  done
  if [[ -n "$target" && "$target" != "$CUR" ]]; then t select-window -t "$target"
  elif ((count == 1)); then msg "the only one of its kind here"; fi
  return 0
}

cmd_toggle() {
  local sid="$1"
  load_session "$sid" || return 1
  local -a ag=("${AGENTS[@]+"${AGENTS[@]}"}") sh=("${SHELLS[@]+"${SHELLS[@]}"}")
  if [[ "$CUR_KIND" == "a" ]]; then
    if [[ -n "$LAST_S" ]] && contains "$LAST_S" "${sh[@]+"${sh[@]}"}"; then
      t select-window -t "$LAST_S"
    elif ((${#SHELLS[@]})); then
      t select-window -t "${SHELLS[0]}"
    else
      t new-window -a -t "$sid:{end}" -c "${SPATH:-$HOME}"
    fi
  else
    if [[ -n "$LAST_A" ]] && contains "$LAST_A" "${ag[@]+"${ag[@]}"}"; then
      t select-window -t "$LAST_A"
    elif ((${#AGENTS[@]})); then
      t select-window -t "${AGENTS[0]}"
    else
      msg -k warn "no agents in this space"
    fi
  fi
}

# ── close a space ──────────────────────────────────────────────────────────
cmd_close() {
  local sid="$1"
  read_opts || return 1
  [[ "$SPACES_ON" == "on" ]] || { msg -k warn "spaces are off"; return 1; }
  local info name hid
  info="$(t display-message -p -t "$sid" "#{@space_hidden}${US}#{session_name}" 2>/dev/null)"
  IFS="$US" read -r hid name <<<"$info"
  [[ -n "$name" ]] || { msg -k warn "no such space"; return 1; }
  if [[ "$name" == "main" ]]; then msg -k warn "main is never closed"; return 1; fi
  if [[ "$hid" == "1" ]] || is_hidden_name "$name"; then msg -k warn "not a space (hidden session)"; return 1; fi

  # Marked first, so no sync re-registers it or gives it a new home while its
  # tabs close; then unregistered under the lock, which is NOT held across the
  # closers (they can take many seconds, past the lock's stale age).
  t set-option -t "$sid" @space_closing 1 2>/dev/null
  reg_lock || { t set-option -u -t "$sid" @space_closing 2>/dev/null; msg -k warn "registry busy, try again"; return 1; }
  reg_load
  local ri; ri="$(reg_find "$name")"
  ((ri >= 0)) && { reg_del "$ri"; reg_save; }
  reg_unlock

  # Clients on it go to main first: detach-on-destroy would close them.
  local rows c csid dest
  dest="$(t list-sessions -F "#{session_id}${US}#{session_name}${US}#{@space_hidden}" \
    | awk -F"$US" -v me="$sid" '$1 != me && $3 != "1" { if ($2 == "main") { print $1; f = 1; exit } if (!a) a = $1 } END { if (!f && a) print a }')"
  rows="$(t list-clients -F "#{client_name}${US}#{session_id}" 2>/dev/null)"
  while IFS="$US" read -r c csid; do
    [[ -n "$c" && "$csid" == "$sid" && -n "$dest" ]] && t switch-client -c "$c" -t "$dest"
  done <<<"$rows"

  # Every tab through the closer (closed-tabs.sh: snapshot, agent session id,
  # CMD+Z reopens it), home too, in parallel; then the session.
  local closer="${CLOSER_OPT:-$HOME/.config/tmux/scripts/closed-tabs.sh close}"
  if [[ "$closer" != "none" ]]; then
    local p
    while IFS= read -r p; do
      [[ -n "$p" ]] || continue
      bash -c "$closer \"\$1\"" _ "$p" </dev/null >/dev/null 2>&1 &
    done < <(t list-panes -s -t "$sid" -f '#{!:#{@space_dash_pane}}' -F '#{pane_id}')   # a dashboard is not a tab
    wait
  fi
  t kill-session -t "$sid" 2>/dev/null
  notify
  msg -k ok "closed $name"
}

# ── move a tab to another space ────────────────────────────────────────────
# Spaces in tab order, visible ones only:
# "index<US>session_id<US>name<US>hidden<US>key<US>folder" (key: @space_key).
spaces_in_order() {
  t list-sessions -F "#{@space_index}${US}#{session_id}${US}#{session_name}${US}#{@space_hidden}${US}#{@space_key}${US}#{session_path}" \
    | awk -F"$US" '$4 != "1" && $1 != ""' | sort -t"$US" -k1,1n
}

cmd_move_menu() {
  local wid="$1" info sid home hid nwin
  info="$(t display-message -p -t "$wid" "#{session_id}${US}#{@space_home}${US}#{@space_hidden}${US}#{session_windows}")" || return 1
  IFS="$US" read -r sid home hid nwin <<<"$info"
  [[ "$hid" == "1" ]] && { msg -k warn "not a space (hidden session)"; return 1; }
  [[ "$home" == "1" ]] && { msg -k warn "home stays in its space"; return 1; }
  local -a args=(display-menu -T "#[align=centre] move tab to space ")
  [[ -n "$CLIENT" ]] && args+=(-c "$CLIENT")
  # "--": a disabled item starts with "-", which would read as a flag.
  args+=(-x "$POPUP_X" -y C --)
  local idx id name h label
  while IFS="$US" read -r idx id name h; do
    [[ -n "$id" ]] || continue
    label="$idx  ${name//#/##}"
    if [[ "$id" == "$sid" ]]; then
      args+=("-$label  (here)" "" "")
    else
      local key=""; ((idx <= 9)) && key="$idx"
      # Ids go in without their sigils: tmux's parser would read "$3" in a
      # double-quoted string as an environment variable. move-to adds them.
      args+=("$label" "$key" "run-shell -b \"bash '$SELF' move-to '${wid#@}' '${id#\$}' '$CLIENT'\"")
    fi
  done < <(spaces_in_order)
  t "${args[@]}"
}

cmd_move_to() {
  local wid="$1" dst="$2" info sid home hid nwin dhid dname
  [[ "$wid" =~ ^[0-9]+$ ]] && wid="@$wid"
  [[ "$dst" =~ ^[0-9]+$ ]] && dst="\$$dst"
  info="$(t display-message -p -t "$wid" "#{session_id}${US}#{@space_home}${US}#{@space_hidden}${US}#{session_windows}")" || return 1
  IFS="$US" read -r sid home hid nwin <<<"$info"
  [[ "$hid" == "1" ]] && { msg -k warn "not a space (hidden session)"; return 1; }
  [[ "$home" == "1" ]] && { msg -k warn "home stays in its space"; return 1; }
  ((nwin > 1)) || { msg -k warn "the last window would end this space"; return 1; }
  [[ "$sid" == "$dst" ]] && return 0
  info="$(t display-message -p -t "$dst" "#{@space_hidden}${US}#{session_name}")" || { msg -k warn "no such space"; return 1; }
  IFS="$US" read -r dhid dname <<<"$info"
  [[ "$dhid" == "1" ]] && { msg -k warn "not a space: $dname"; return 1; }
  t move-window -d -a -s "$wid" -t "$dst:{end}" && { notify; msg -k ok "moved to $dname"; }
}

# ── new space ──────────────────────────────────────────────────────────────
# tmux refuses '.' and ':' in session names (it rewrites them to '_'); spaces
# and other oddities read badly in the tab, so keep [A-Za-z0-9_-] only.
sanitize() {
  local s="$1"
  s="${s//[^A-Za-z0-9_-]/-}"
  s="${s##-}"
  printf '%s' "${s:-space}"
}

# A folder as the reuse matches compare it: no trailing /, but / stays /.
norm_dir() {
  local d="$1"
  while [[ "$d" == */ && "$d" != / ]]; do d="${d%/}"; done
  printf '%s' "${d:-/}"
}

# The space already on DIR (absolute): print its session id, or nothing. ~ is
# main's. Hidden sessions never count.
space_for_dir() {
  local dir rows id name path hid
  dir="$(norm_dir "$1")"
  [[ "$dir" == "$HOME" ]] && { t list-sessions -F '#{session_id}|#{session_name}' | awk -F'|' '$2=="main"{print $1; exit}'; return 0; }
  rows="$(t list-sessions -F '#{session_id}|#{session_name}|#{session_path}|#{@space_hidden}')" || return 1
  while IFS='|' read -r id name path hid; do
    [[ "$hid" == "1" ]] && continue
    if [[ "$(norm_dir "$path")" == "$dir" ]]; then printf '%s\n' "$id"; return 0; fi
  done <<<"$rows"
  return 0
}

# Create-or-reuse for DIR, print the session id to switch to. Pure tmux, no
# picker, so tests can drive it: `space.sh open-dir DIR`.
# DIR must already be absolute (cmd_open_dir makes it so).
open_dir() {
  local dir="${1%/}" rows name base sid
  [[ -n "$dir" ]] || dir="/"
  sid="$(space_for_dir "$dir")" || return 1
  [[ -n "$sid" || "$dir" == "$HOME" ]] && { printf '%s\n' "$sid"; return 0; }
  rows="$(t list-sessions -F '#{session_id}|#{session_name}|#{session_path}|#{@space_hidden}')" || return 1
  local hidden
  # Every hidden name counts as taken even with no such session yet: a folder
  # called stash/tasks/agents/... must become stash-2, never a hidden session.
  hidden=" $(t show -gqv @space_hidden_sessions 2>/dev/null) "
  base="$(sanitize "$(basename "$dir")")"
  name="$base"
  local k=2
  while [[ "$hidden" == *" $name "* ]] \
    || printf '%s\n' "$rows" | awk -F'|' -v n="$name" '$2==n{f=1} END{exit !f}'; do
    name="$base-$k"; k=$((k + 1))
  done
  # Its session-created sync registers it (end of the order) and adds home.
  t new-session -d -s "$name" -c "$dir" -P -F '#{session_id}'
}

cmd_open_dir() {
  local dir="$1" client="${2:-}" sid
  [[ -d "$dir" ]] || { msg -k warn "not a directory: $dir"; return 1; }
  # Absolute before the reuse match and before it becomes a session_path: a
  # typed "../foo" or "." is relative to the popup's cwd, not to tmux's.
  dir="$(cd -- "$dir" 2>/dev/null && pwd)" || { msg -k error "cannot enter: $1"; return 1; }
  sid="$(open_dir "$dir")" || { msg -k error "could not open a space for $dir"; return 1; }
  [[ -n "$sid" ]] || { msg -k error "could not open a space for $dir"; return 1; }
  notify
  if [[ -n "$client" ]]; then t switch-client -c "$client" -t "$sid"; else t switch-client -t "$sid"; fi
}

# The folder candidates the pickers offer, best first, one absolute path a
# line: PANE_PATH (the tab's cwd), then zoxide by frecency — or, without
# zoxide, ~ and the folders under ~/code/projects and ~/code — deduped, dead
# dirs dropped. Same list as claude-dir-picker.sh. tilde_paths ~-shortens it.
dir_candidates() {
  local d
  {
    printf '%s\n' "$1"
    if command -v zoxide >/dev/null 2>&1; then
      zoxide query -l 2>/dev/null || true
    else
      printf '%s\n' "$HOME"
      for d in "$HOME"/code/projects/*/ "$HOME"/code/*/; do [[ -d "$d" ]] && printf '%s\n' "${d%/}"; done
    fi
  } | awk 'NF && !seen[$0]++' \
    | while IFS= read -r d; do [[ -d "$d" ]] && printf '%s\n' "$d"; done
}
tilde_paths() { sed "s|^$HOME\$|~|; s|^$HOME/|~/|"; }

# The client's session id and pane folder, as "sid<US>path". list-clients
# expands each row in THAT client's session (display-message -c does not: -c
# only picks where to display). CLIENT may be its name or its tty.
client_where() {
  local c="${1:-}" row
  if [[ -n "$c" ]]; then
    row="$(t list-clients -F "#{client_name}${US}#{client_tty}${US}#{session_id}${US}#{pane_current_path}" 2>/dev/null \
      | awk -F"$US" -v c="$c" '$1 == c || $2 == c { print $3 FS $4; exit }')"
    [[ -n "$row" ]] && { printf '%s\n' "$row"; return 0; }
  fi
  t display-message -p "#{session_id}${US}#{pane_current_path}"
}

cmd_new_space() {
  local client="${1:-}"
  CLIENT="$client"
  command -v fzf >/dev/null 2>&1 || { msg -k error "fzf not found in PATH"; return 1; }
  local pane_path list out status query selection target
  if [[ -n "$client" ]]; then pane_path="$(t display-message -c "$client" -p '#{pane_current_path}')"
  else pane_path="$(t display-message -p '#{pane_current_path}')"; fi
  [[ -d "$pane_path" ]] || pane_path="$HOME"
  # dir_candidates, ~-shortened. A typed path that is not listed still works
  # (--print-query).
  list="$(dir_candidates "$pane_path" | tilde_paths)"
  out="$(printf '%s\n' "$list" | fzf \
    --reverse --info=inline --print-query --tiebreak=index \
    --prompt='new space in: ' \
    --header='enter: open (or switch to) a space for this folder · esc: cancel' \
    --preview "bash '${BASH_SOURCE[0]}' preview {}" \
    --preview-window=right:50%:nowrap:noinfo)"
  status=$?
  [[ $status -eq 0 || $status -eq 1 ]] || return 0
  query="$(printf '%s\n' "$out" | sed -n '1p')"
  selection="$(printf '%s\n' "$out" | sed -n '2p')"
  target="${selection:-$query}"
  [[ -n "$target" ]] || return 0
  target="${target/#\~/$HOME}"
  cmd_open_dir "$target" "$client"
}

# ── new tab in a chosen space ──────────────────────────────────────────────
# CMD+SHIFT+T / CMD+SHIFT+R (prefix C-t / K, spaces.conf) ask WHICH SPACE:
# pick-tab is the popup, new-tab the pure-tmux half tests drive.
#
# new-tab KIND TARGET: KIND shell or agent (a claude tab). TARGET is a
# session id ($N, or a bare N as move-to takes) or a folder. A folder's space
# is reused, else made (open_dir). The tab goes at the END of the space, at
# its folder, and CLIENT is switched to it. The space itself is never renamed,
# closed or reordered. A shell in a space made just now is that space's only
# window, which its sync makes home: CLIENT lands there and no second shell is
# added. A claude tab is an agent from birth: @agent_birth runs in the SAME
# command list as its new-window (agent-birth.conf), so it never counts as a
# shell, even for a moment.
cmd_new_tab() {
  local kind="$1" target="$2" client="${3:-}" sid dir="" created=0 info hid name spath wid
  CLIENT="$client"
  [[ "$kind" == shell || "$kind" == agent ]] || { msg -k warn "new-tab: shell or agent, not '$kind'"; return 1; }
  [[ -n "$target" ]] || { msg -k warn "new-tab: no space or folder"; return 1; }
  read_opts || return 1
  if [[ "$target" =~ ^\$?[0-9]+$ ]]; then
    sid="\$${target#\$}"
  else
    [[ -d "$target" ]] || { msg -k warn "not a directory: $target"; return 1; }
    # Absolute, as cmd_open_dir: a typed "../foo" is relative to the popup.
    dir="$(cd -- "$target" 2>/dev/null && pwd)" || { msg -k error "cannot enter: $target"; return 1; }
    sid="$(space_for_dir "$dir")" || { msg -k error "could not open a space for $dir"; return 1; }
    if [[ -z "$sid" ]]; then
      sid="$(open_dir "$dir")"
      [[ "$sid" == \$* ]] || { msg -k error "could not open a space for $dir"; return 1; }
      created=1
    fi
  fi
  info="$(t display-message -p -t "$sid" "#{session_id}${US}#{@space_hidden}${US}#{session_name}${US}#{session_path}" 2>/dev/null)"
  IFS="$US" read -r sid hid name spath <<<"$info"
  [[ "$sid" == \$* && -n "$name" ]] || { msg -k warn "no such space"; return 1; }
  if [[ "$hid" == "1" ]] || is_hidden_name "$name"; then msg -k warn "not a space (hidden session)"; return 1; fi

  if ((created)) && [[ "$kind" == shell ]]; then
    notify
    t switch-client ${client:+-c "$client"} -t "$sid"
    return 0
  fi
  # A claude tab in a space made just now: its first shell must be home
  # before a second window lands beside it, or the sync (which adopts a
  # space's ONLY window) would add a home shell of its own. One sync here,
  # under the registry lock, does what the session-created one would (home
  # and its dashboard at once). A sync that does nothing structural (a
  # restore pending, the lock busy) makes no home, so then the window gets
  # the home marks here, exactly the ones sync's adoption sets (home_opts);
  # the next structural sync keeps it as home (a marked window wins) and adds
  # its dashboard (ensure_dash, add_panes), and never a second home.
  if ((created)); then
    cmd_sync normal; reg_unlock
    local fw fp fh
    IFS="$US" read -r fw fp fh <<<"$(t display-message -p -t "$sid:^" "#{window_id}${US}#{pane_id}${US}#{@space_home}" 2>/dev/null)"
    if [[ "$fw" == @* && "$fp" == %* && "$fh" != 1 ]]; then
      local -a args=()
      home_opts "$fw" "$fp"
      run_args
    fi
  fi

  local -a cmd=(new-window -a -t "$sid:{end}" -c "${spath:-$HOME}" -P -F '#{window_id}')
  [[ "$kind" == agent ]] && cmd+=(-e ZSH_AUTOSTART=claude ";" run-shell -C '#{?@agent_birth,#{@agent_birth} claude,}')
  wid="$(t "${cmd[@]}" 2>/dev/null)"
  wid="${wid%%$'\n'*}"
  [[ "$wid" == @* ]] || { msg -k error "could not open a tab in $name"; return 1; }
  notify
  t switch-client ${client:+-c "$client"} -t "$sid:$wid"
}

# pick-tab KIND CLIENT (inside display-popup): the spaces in ⌃N order, the
# client's own marked (here) but still a pick, then the folder candidates
# (dir_candidates) that are not already a space's folder. Each row is
# "KEY<TAB>label"; fzf shows and filters the label only (--with-nth=2) and
# hands back the whole row, so a pick is read from KEY (s:N for space $N,
# d:/abs/path for a folder), never from the label. A typed path that matches
# no row is taken as a folder (--print-query). Esc / ^C: nothing.
cmd_pick_tab() {
  local kind="$1" client="${2:-}"
  CLIENT="$client"
  [[ "$kind" == shell || "$kind" == agent ]] || { msg -k warn "pick-tab: shell or agent, not '$kind'"; return 1; }
  command -v fzf >/dev/null 2>&1 || { msg -k error "fzf not found in PATH"; return 1; }
  local where cur pane_path
  where="$(client_where "$client")"
  IFS="$US" read -r cur pane_path <<<"$where"
  [[ -d "$pane_path" ]] || pane_path="$HOME"

  # No fork per row: zoxide easily lists hundreds of folders, and a
  # $(norm_dir) plus a sed for each made the popup take over a second to
  # open. The space rows are built in bash, the folder rows by one awk, and
  # both stream into fzf, so it opens at once.
  local -a s_id=() s_label=() s_name=() s_path=()
  local idx id name h key path p w=4 i
  while IFS="$US" read -r idx id name h key path; do
    [[ -n "$id" ]] || continue
    p="$path"; while [[ "$p" == */ && "$p" != / ]]; do p="${p%/}"; done   # norm_dir, inline
    s_id+=("$id") s_label+=("${key:-$idx}") s_name+=("$name") s_path+=("${p:-/}")
    ((${#name} > w)) && w=${#name}
  done < <(spaces_in_order)
  local list="" short here row taken=""
  for i in "${!s_id[@]}"; do
    p="${s_path[i]}"
    if [[ "$p" == "$HOME" ]]; then short="~"
    elif [[ "$p" == "$HOME"/* ]]; then short="~${p#"$HOME"}"
    else short="$p"; fi
    here=""; [[ "${s_id[i]}" == "$cur" ]] && here="  (here)"
    printf -v row 's:%s\t%-3s %-*s  %s%s\n' "${s_id[i]#\$}" "${s_label[i]}" "$w" "${s_name[i]}" "$short" "$here"
    list+="$row" taken+="$p$US"
  done
  # Folder rows: drop the ones that already are a space's folder (both sides
  # normalized as norm_dir does), ~-shorten the label as tilde_paths does.
  # The taken list is $US-joined: macOS awk refuses a newline in a -v value.
  local -a folder_rows=(awk -v home="$HOME" -v taken="$taken" '
    BEGIN { n = split(taken, t, "\037"); for (i = 1; i <= n; i++) if (t[i] != "") seen[t[i]] = 1 }
    { p = $0; while (p ~ /.\/$/) sub(/\/$/, "", p); if (p in seen) next
      l = p; if (l == home) l = "~"; else if (index(l, home "/") == 1) l = "~" substr(l, length(home) + 1)
      print "d:" $0 "\t" l }')

  local what=shell; [[ "$kind" == agent ]] && what=claude
  local out status query selection target
  # pipefail off in here: a pick made before every row has streamed in
  # leaves the feeder dying of SIGPIPE, and that 141 must not read as a cancel.
  out="$(set +o pipefail; { printf '%s' "$list"; dir_candidates "$pane_path" | "${folder_rows[@]}"; } | fzf \
    --reverse --info=inline --print-query --tiebreak=index \
    --delimiter=$'\t' --with-nth=2 \
    --prompt="new $what in: " \
    --header="enter: new $what tab at the end of that space (a folder: its space, made if new) · esc: cancel" \
    --preview "bash '${BASH_SOURCE[0]}' preview-tab {1}" \
    --preview-window=right:50%:nowrap:noinfo)"
  status=$?
  [[ $status -eq 0 || $status -eq 1 ]] || return 0
  query="$(printf '%s\n' "$out" | sed -n '1p')"
  selection="$(printf '%s\n' "$out" | sed -n '2p')"
  if [[ -n "$selection" ]]; then
    key="${selection%%$'\t'*}"
    case "$key" in
      s:*) target="\$${key#s:}" ;;
      d:/*) target="${key#d:}" ;;
      *) return 0 ;;
    esac
  else
    [[ -n "$query" ]] || return 0
    target="${query/#\~/$HOME}"
    # Never a bare number (new-tab would read it as a session id).
    [[ "$target" == /* ]] || target="$PWD/$target"
  fi
  cmd_new_tab "$kind" "$target" "$client"
}

# A folder's listing, for the pickers' previews (~ allowed).
preview_dir() {
  local d="${1:-}"
  d="${d/#\~/$HOME}"
  ls -1p -- "$d" 2>/dev/null | head -200
}

# pick-tab's preview of row KEY: a space's tabs (shells in tab order, home
# first and unlettered, the rest with their ⌥W letter; then the agents with
# their ⌥W letter and summary), or a folder's listing.
cmd_preview_tab() {
  local key="${1:-}"
  case "$key" in
    s:*) ;;
    d:*) preview_dir "${key#d:}"; return 0 ;;
    *) return 0 ;;
  esac
  local sid="\$${key#s:}" rows k home letter summary state wname cmd path
  rows="$(t list-windows -t "$sid" -F "#{?#{@agent_state},a,s}${US}#{@space_home}${US}#{@agent_key}${US}#{@agent_summary}${US}#{@agent_state}${US}#{window_name}${US}#{pane_current_command}${US}#{pane_current_path}" 2>/dev/null)" || return 0
  local n=0 mark
  # Shells as the status line marks them: home unlettered (⌃N reaches it),
  # the others by their ⌥W letter, · until they have one.
  printf 'shells\n'
  while IFS="$US" read -r k home letter summary state wname cmd path; do
    [[ "$k" == s ]] || continue
    if [[ "$home" == 1 ]]; then mark=" " cmd=home; else mark="${letter:-·}"; fi
    printf '  %s  %-10s %s\n' "$mark" "$cmd" "$(printf '%s\n' "$path" | tilde_paths)"
  done <<<"$rows"
  printf '\nagents\n'
  n=0
  while IFS="$US" read -r k home letter summary state wname cmd path; do
    [[ "$k" == a ]] || continue
    n=$((n + 1))
    printf '  %s  %s  (%s)\n' "${letter:--}" "${summary:-$wname}" "$state"
  done <<<"$rows"
  ((n)) || printf '  none\n'
}

main() {
  local cmd="${1:-}"
  shift || true
  # Every caller that should act (keys, hooks, popups, tests) runs inside a
  # tmux server and has $TMUX. Without it, `tmux` means the default server,
  # which is how a hand-run test once closed a real space (2026-10-15).
  if [[ -z "${TMUX:-}" && "$cmd" != "preview" ]]; then
    echo "space.sh: \$TMUX is empty (not run by a tmux server); refusing '$cmd'" >&2
    return 3
  fi
  case "$cmd" in
    sync) cmd_sync "${1:-normal}" ;;
    restore-wait) cmd_restore_wait ;;
    respawn-home) cmd_respawn_home "${1:-}" ;;
    agent) CLIENT="${3:-}"; cmd_agent "${1:-}" "${2:-}" ;;
    shell) CLIENT="${3:-}"; cmd_shell "${1:-}" "${2:-}" ;;
    next | prev) CLIENT="${2:-}"; cmd_step "$cmd" "${1:-}" ;;
    toggle) CLIENT="${2:-}"; cmd_toggle "${1:-}" ;;
    close) CLIENT="${2:-}"; cmd_close "${1:-}" ;;
    move-menu) CLIENT="${2:-}"; cmd_move_menu "${1:-}" ;;
    move-to) CLIENT="${3:-}"; cmd_move_to "${1:-}" "${2:-}" ;;
    new-space) cmd_new_space "${1:-}" ;;
    open-dir) CLIENT="${2:-}"; cmd_open_dir "${1:-}" "${2:-}" ;;
    pick-tab) cmd_pick_tab "${1:-}" "${2:-}" ;;
    new-tab) cmd_new_tab "${1:-}" "${2:-}" "${3:-}" ;;
    preview-tab) cmd_preview_tab "${1:-}" ;;
    preview) preview_dir "${1:-}" ;;
    *)
      echo "usage: space.sh sync [load|reconcile] | agent|shell N SID [CLIENT] | next|prev SID [CLIENT] | toggle SID [CLIENT] | close SID [CLIENT] | move-menu WID [CLIENT] | move-to WID SID [CLIENT] | respawn-home PANE | new-space CLIENT | open-dir DIR [CLIENT] | pick-tab shell|agent CLIENT | new-tab shell|agent SID|DIR [CLIENT] | preview-tab KEY" >&2
      return 2
      ;;
  esac
}

main "$@"
