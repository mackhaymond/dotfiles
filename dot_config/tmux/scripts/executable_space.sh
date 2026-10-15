#!/usr/bin/env bash
#
# SPACES (docs/spaces.md): a space is a tmux session, an agent is a window
# whose @agent_state is non-empty, every other window is a shell. One shell
# per space is its HOME (@space_home 1): the space's main shell, its lowest
# window, which cannot be closed and respawns when its shell exits, so a space
# outlives its last tab. This script holds the logic behind
# the spaces keys that a tmux format cannot do on its own (counting, wrapping,
# validating a remembered window, the registry). The keys and the two status
# lines live in spaces.conf; the hidden-session list lives in tmux.conf
# (@space_hidden_sessions).
#
#   sync [load|reconcile]     mark hidden sessions (@space_hidden 1) and number
#                             the visible ones (@space_index: main = 1, then
#                             registry order). With @spaces on it also gives
#                             every visible space exactly one live home shell
#                             and keeps the registry in step. `load` (config
#                             load) and `reconcile` also recreate registered
#                             spaces that are missing and restore their folder.
#   (agent/shell/next/prev/toggle take an optional trailing CLIENT, for
#    messages; all refuse a hidden session)
#   agent N SID               go to agent N of session SID (1-based ordinal
#                             among windows with @agent_state, by window_index;
#                             the agent sidebar numbers agents the same way)
#   shell N SID               go to shell N of SID (same ordinal, among shells,
#                             home = 1; line 1 of the status shows them)
#   next|prev SID             on an agent: next / previous agent; on a shell:
#                             next / previous shell; wrapping
#   respawn-home PANE         the pane-died hook: a home shell that exited
#                             comes back in place (at most every 2 s)
#   toggle SID                agent <-> last-visited shell of this space (a
#                             new shell at the space folder if there is none);
#                             from a shell, back to the last-visited agent
#   new-space CLIENT          folder picker (run inside display-popup), then
#                             create or reuse a session for that folder and
#                             switch CLIENT to it
#   close SID CLIENT          close a space for good: every tab through
#                             @space_closer (closed-tabs.sh, so CMD+Z can bring
#                             it back), unregister, kill the session. Never main
#   move-menu WID CLIENT      menu of spaces; picking one runs move-to
#   move-to WID SID CLIENT    move window WID to the end of space SID
#
# Outside tmux ($TMUX empty) everything but `preview` refuses: a plain `tmux`
# there reaches the DEFAULT server, whatever server the caller had in mind.
#
# REGISTRY (@spaces on only): @space_registry, default
# ~/.local/state/tmux-spaces/spaces.tsv, one "name<TAB>folder" line per space
# in tab order. It is what survives a server restart that resurrect does not
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

# Messages go to the client whose key ran us (CLIENT, passed by the binding as
# '#{client_name}'); without one tmux picks its "best" client, which may be a
# different terminal.
CLIENT=""
# The Rust sidebar redraws at once on this Darwin notification; posted after
# anything it shows changes (a space closed, created or recreated, a tab moved).
notify() { command -v notifyutil >/dev/null 2>&1 && notifyutil -p com.mackhaymond.tmux.focus >/dev/null 2>&1; return 0; }
msg() {
  if [[ -n "$CLIENT" ]]; then t display-message -c "$CLIENT" "space: $*" 2>/dev/null
  else t display-message "space: $*" 2>/dev/null; fi
}

# ── globals, registry, lock ────────────────────────────────────────────────
# One call, and it works with no session at all (config load on a cold start
# runs before the first session exists).
read_opts() {
  local o
  o="$(t display-message -p "#{@space_hidden_sessions}${US}#{@spaces}${US}#{@space_registry}${US}#{@space_restore_pending}${US}#{start_time}${US}#{@continuum-restore}${US}#{@space_closer}")" || return 1
  IFS="$US" read -r HIDDEN_LIST SPACES_ON REG_OPT PENDING START_T CONT_RESTORE CLOSER_OPT <<<"$o"
  REG="${REG_OPT:-${XDG_STATE_HOME:-$HOME/.local/state}/tmux-spaces/spaces.tsv}"
}

is_hidden_name() { [[ " $HIDDEN_LIST " == *" $1 "* ]]; }

R_NAME=() R_PATH=()
reg_load() {
  R_NAME=() R_PATH=()
  [[ -r "$REG" ]] || return 0
  local n p
  while IFS=$'\t' read -r n p; do
    [[ -n "$n" ]] || continue
    R_NAME+=("$n") R_PATH+=("$p")
  done <"$REG"
}
reg_find() {
  local i
  for i in "${!R_NAME[@]}"; do [[ "${R_NAME[i]}" == "$1" ]] && { echo "$i"; return 0; }; done
  echo -1
}
reg_del() {
  local i="$1" k
  local -a n=() p=()
  for k in "${!R_NAME[@]}"; do
    ((k == i)) && continue
    n+=("${R_NAME[k]}") p+=("${R_PATH[k]}")
  done
  R_NAME=("${n[@]+"${n[@]}"}") R_PATH=("${p[@]+"${p[@]}"}")
}
reg_save() {
  local tmp="$REG.tmp.$$" i
  {
    for i in "${!R_NAME[@]}"; do printf '%s\t%s\n' "${R_NAME[i]}" "${R_PATH[i]}"; done
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
  args+=(";" set -w -t "$w" @space_home 1 ";" set -w -t "$w" automatic-rename off
    ";" set -w -t "$w" allow-rename off ";" set -wu -t "$w" remain-on-exit ";" rename-window -t "$w" home)
  home_pane_opts "$p"
}
home_pane_opts() {
  args+=(";" set -p -t "$1" @space_home_pane 1 ";" set -p -t "$1" remain-on-exit on)
}
# Live, and a shell rather than the landing screen an earlier home ran.
home_ok() { [[ "$1" == "0" && "$2" != *space*home* ]]; }   # pane_dead, pane_start_command

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
  srows="$(t list-sessions -F "#{session_id}${US}#{@space_hidden}${US}#{@space_index}${US}#{session_windows}${US}#{@space_closing}${US}#{@space_reg}${US}#{session_path}${US}#{session_name}")" || return 0
  local clients
  clients="$(t list-clients -F '#{client_name}' 2>/dev/null)"
  # Sessions by numeric id (= creation order; renames are matched in this
  # order, so a renamed main is seen before a new one takes its name).
  local -a S_ID=() S_HID=() S_IDX=() S_NWIN=() S_CLOSING=() S_REG=() S_PATH=() S_NAME=()
  local id hid idx nwin closing reg path name
  while IFS="$US" read -r id hid idx nwin closing reg path name; do
    [[ -n "$id" ]] || continue
    S_ID+=("$id") S_HID+=("$hid") S_IDX+=("$idx") S_NWIN+=("$nwin") S_CLOSING+=("$closing") S_REG+=("$reg") S_PATH+=("$path") S_NAME+=("$name")
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
        R_NAME+=("$name") R_PATH+=("${S_PATH[i]}"); changed=1
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
    ((changed)) && reg_save
  fi

  # Numbers: main first, then registry order, then anything unregistered by
  # id. The status line's S: loops pick tabs by these numbers.
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
  for i in "${!S_ID[@]}"; do
    if is_hidden_name "${S_NAME[i]}"; then
      [[ "${S_HID[i]}" == "1" ]] || add set-option -t "${S_ID[i]}" @space_hidden 1
      [[ -z "${S_IDX[i]}" ]] || add set-option -u -t "${S_ID[i]}" @space_index
    else
      [[ -z "${S_HID[i]}" ]] || add set-option -u -t "${S_ID[i]}" @space_hidden
    fi
  done
  n=1
  for k in "${order[@]+"${order[@]}"}"; do
    [[ "${S_IDX[k]}" == "$n" ]] || add set-option -t "${S_ID[k]}" @space_index "$n"
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
    t "${args[@]}" >/dev/null 2>&1
  fi
  if ((struct)); then
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
# One row per PANE (list-panes -a, in window then pane order), so the first
# row of a window is its first pane.
sync_homes() {
  local hrows
  hrows="$(t list-panes -a -f '#{||:#{@space_home},#{||:#{==:#{window_name},home},#{==:#{session_windows},1}}}' \
    -F "#{session_id}${US}#{window_id}${US}#{@space_home}${US}#{?#{@agent_state},a,}${US}#{pane_dead}${US}#{?#{==:#{window_name},home},n,}${US}#{window_start_flag}${US}#{@space_home_pane}${US}#{pane_current_command}${US}#{pane_id}${US}#{session_path}${US}#{pane_start_command}" 2>/dev/null)"
  local i s sid wid home agent dead named first hpane cmd pane spath start
  local keep keepfirst kp kpstate hp hpstate cand cfirst cpane cstate only ofirst opane ostate lastw
  for i in "${!S_ID[@]}"; do
    is_hidden_name "${S_NAME[i]}" && continue
    [[ "${S_CLOSING[i]}" == "1" ]] && continue
    sid="${S_ID[i]}" keep="" keepfirst="" kp="" kpstate="" hp="" hpstate="" cand="" cfirst="" cpane="" cstate=""
    only="" ofirst="" opane="" ostate="" lastw=""
    while IFS="$US" read -r s wid home agent dead named first hpane cmd pane spath start; do
      [[ "$s" == "$sid" ]] || continue
      local newwin=0
      [[ "$wid" != "$lastw" ]] && newwin=1 && lastw="$wid"
      if [[ "$home" == "1" ]]; then
        [[ -z "$keep" ]] && keep="$wid" keepfirst="$first"
        if [[ "$wid" == "$keep" ]]; then
          ((newwin)) && kp="$pane" kpstate="$dead$start"
          if [[ "$hpane" == "1" && -z "$hp" ]]; then hp="$pane" hpstate="$dead$start"
          elif [[ "$hpane" == "1" ]]; then add set -pu -t "$pane" @space_home_pane ";" set -pu -t "$pane" remain-on-exit
          fi
        else
          ((newwin)) && add set -wu -t "$wid" @space_home ";" set -wu -t "$wid" remain-on-exit ";" set -wu -t "$wid" allow-rename \
            ";" set -wu -t "$wid" automatic-rename
          [[ "$hpane" == "1" ]] && add set -pu -t "$pane" @space_home_pane ";" set -pu -t "$pane" remain-on-exit
        fi
      elif ((newwin)) && [[ -z "$agent" && -n "$named" && -z "$cand" ]] && [[ "$dead" == "1" || "$cmd" =~ ^-?(zsh|bash|sh|fish|nu)$ ]]; then
        cand="$wid" cfirst="$first" cpane="$pane" cstate="$dead$start"
      elif ((newwin)) && [[ -z "$agent" && "${S_NWIN[i]}" == "1" ]]; then
        only="$wid" ofirst="$first" opane="$pane" ostate="$dead$start"
      fi
    done <<<"$hrows"
    if [[ -n "$keep" ]]; then
      if [[ -z "$hp" ]]; then
        # No pane marked (a home from before panes were marked): its first.
        hp="$kp" hpstate="$kpstate"
        home_pane_opts "$hp"
        add set -wu -t "$keep" remain-on-exit
      fi
      # hpstate is pane_dead then the start command: "0..." is a live pane.
      home_ok "${hpstate:0:1}" "${hpstate:1}" || add respawn-pane -k -t "$hp" -c "${S_PATH[i]:-$HOME}"
      [[ "$keepfirst" == "1" ]] || add move-window -b -s "$keep" -t "$sid:^"
      continue
    fi
    [[ -z "$cand" && -n "$only" ]] && cand="$only" cfirst="$ofirst" cpane="$opane" cstate="$ostate"
    if [[ -n "$cand" ]]; then
      home_ok "${cstate:0:1}" "${cstate:1}" || add respawn-pane -k -t "$cpane" -c "${S_PATH[i]:-$HOME}"
      home_opts "$cand" "$cpane"
      [[ "$cfirst" == "1" ]] || add move-window -b -s "$cand" -t "$sid:^"
    else
      add new-window -d -b -t "$sid:^" -n home -c "${S_PATH[i]:-$HOME}"
      home_opts "$sid:^" "$sid:^"
    fi
  done
}

# The pane-died hook (spaces.conf) for a home pane: back in place, in the space
# folder. Never more than once every 2 s, so a shell that dies at once cannot
# spin the server.
cmd_respawn_home() {
  local pane="$1" info home dead last path now
  info="$(t display-message -p -t "$pane" "#{@space_home_pane}${US}#{pane_dead}${US}#{@space_respawned}${US}#{session_path}" 2>/dev/null)" || return 0
  IFS="$US" read -r home dead last path <<<"$info"
  [[ "$home" == "1" && "$dead" == "1" ]] || return 0
  now="$(date +%s)"
  [[ "$last" =~ ^[0-9]+$ ]] && ((now - last < 2)) && sleep 2
  t set -p -t "$pane" @space_respawned "$(date +%s)" ";" respawn-pane -t "$pane" -c "${path:-$HOME}" 2>/dev/null
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
    msg "not a space (hidden session)"
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
  [[ "$n" =~ ^[1-9][0-9]*$ ]] || { msg "bad agent number '$n'"; return 1; }
  load_session "$sid" || return 1
  if ((n > ${#AGENTS[@]})); then
    msg "no agent $n here (${#AGENTS[@]} in this space)"
    return 0
  fi
  t select-window -t "${AGENTS[n - 1]}"
}

cmd_shell() {
  local n="$1" sid="$2"
  [[ "$n" =~ ^[1-9][0-9]*$ ]] || { msg "bad shell number '$n'"; return 1; }
  load_session "$sid" || return 1
  if ((n > ${#SHELLS[@]})); then
    msg "no shell $n here (${#SHELLS[@]} in this space)"
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
  ((count)) || { msg "no $what in this space"; return 0; }
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
      msg "no agents in this space"
    fi
  fi
}

# ── close a space ──────────────────────────────────────────────────────────
cmd_close() {
  local sid="$1"
  read_opts || return 1
  [[ "$SPACES_ON" == "on" ]] || { msg "spaces are off"; return 1; }
  local info name hid
  info="$(t display-message -p -t "$sid" "#{@space_hidden}${US}#{session_name}" 2>/dev/null)"
  IFS="$US" read -r hid name <<<"$info"
  [[ -n "$name" ]] || { msg "no such space"; return 1; }
  if [[ "$name" == "main" ]]; then msg "main is never closed"; return 1; fi
  if [[ "$hid" == "1" ]] || is_hidden_name "$name"; then msg "not a space (hidden session)"; return 1; fi

  # Marked first, so no sync re-registers it or gives it a new home while its
  # tabs close; then unregistered under the lock, which is NOT held across the
  # closers (they can take many seconds, past the lock's stale age).
  t set-option -t "$sid" @space_closing 1 2>/dev/null
  reg_lock || { t set-option -u -t "$sid" @space_closing 2>/dev/null; msg "registry busy, try again"; return 1; }
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
    done < <(t list-panes -s -t "$sid" -F '#{pane_id}')
    wait
  fi
  t kill-session -t "$sid" 2>/dev/null
  notify
  msg "closed $name"
}

# ── move a tab to another space ────────────────────────────────────────────
# Spaces in tab order: "index<US>session_id<US>name", visible ones only.
spaces_in_order() {
  t list-sessions -F "#{@space_index}${US}#{session_id}${US}#{session_name}${US}#{@space_hidden}" \
    | awk -F"$US" '$4 != "1" && $1 != ""' | sort -t"$US" -k1,1n
}

cmd_move_menu() {
  local wid="$1" info sid home hid nwin
  info="$(t display-message -p -t "$wid" "#{session_id}${US}#{@space_home}${US}#{@space_hidden}${US}#{session_windows}")" || return 1
  IFS="$US" read -r sid home hid nwin <<<"$info"
  [[ "$hid" == "1" ]] && { msg "not a space (hidden session)"; return 1; }
  [[ "$home" == "1" ]] && { msg "home stays in its space"; return 1; }
  local -a args=(display-menu -T "#[align=centre] move tab to space ")
  [[ -n "$CLIENT" ]] && args+=(-c "$CLIENT")
  # "--": a disabled item starts with "-", which would read as a flag.
  args+=(-x C -y C --)
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
  [[ "$hid" == "1" ]] && { msg "not a space (hidden session)"; return 1; }
  [[ "$home" == "1" ]] && { msg "home stays in its space"; return 1; }
  ((nwin > 1)) || { msg "the last window would end this space"; return 1; }
  [[ "$sid" == "$dst" ]] && return 0
  info="$(t display-message -p -t "$dst" "#{@space_hidden}${US}#{session_name}")" || { msg "no such space"; return 1; }
  IFS="$US" read -r dhid dname <<<"$info"
  [[ "$dhid" == "1" ]] && { msg "not a space: $dname"; return 1; }
  t move-window -d -a -s "$wid" -t "$dst:{end}" && { notify; msg "moved to $dname"; }
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

# Create-or-reuse for DIR, print the session id to switch to. Pure tmux, no
# picker, so tests can drive it: `space.sh open-dir DIR`.
# DIR must already be absolute (cmd_open_dir makes it so).
open_dir() {
  local dir="${1%/}" rows id name path base
  [[ -n "$dir" ]] || dir="/"
  [[ "$dir" == "$HOME" ]] && { t list-sessions -F '#{session_id}|#{session_name}' | awk -F'|' '$2=="main"{print $1; exit}'; return 0; }
  rows="$(t list-sessions -F '#{session_id}|#{session_name}|#{session_path}|#{@space_hidden}')" || return 1
  local hid hidden
  while IFS='|' read -r id name path hid; do
    [[ "$hid" == "1" ]] && continue
    if [[ "${path%/}" == "$dir" ]]; then printf '%s\n' "$id"; return 0; fi
  done <<<"$rows"
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
  [[ -d "$dir" ]] || { msg "not a directory: $dir"; return 1; }
  # Absolute before the reuse match and before it becomes a session_path: a
  # typed "../foo" or "." is relative to the popup's cwd, not to tmux's.
  dir="$(cd -- "$dir" 2>/dev/null && pwd)" || { msg "cannot enter: $1"; return 1; }
  sid="$(open_dir "$dir")" || { msg "could not open a space for $dir"; return 1; }
  [[ -n "$sid" ]] || { msg "could not open a space for $dir"; return 1; }
  notify
  if [[ -n "$client" ]]; then t switch-client -c "$client" -t "$sid"; else t switch-client -t "$sid"; fi
}

cmd_new_space() {
  local client="${1:-}"
  CLIENT="$client"
  command -v fzf >/dev/null 2>&1 || { msg "fzf not found in PATH"; return 1; }
  local pane_path list out status query selection target
  if [[ -n "$client" ]]; then pane_path="$(t display-message -c "$client" -p '#{pane_current_path}')"
  else pane_path="$(t display-message -p '#{pane_current_path}')"; fi
  [[ -d "$pane_path" ]] || pane_path="$HOME"
  # Same candidate list as claude-dir-picker.sh: this tab's cwd, then zoxide
  # by frecency — or, without zoxide, ~ and the folders under ~/code/projects
  # and ~/code — deduped, dead dirs dropped, ~-shortened. A typed path that is
  # not listed still works (--print-query).
  list="$(
    {
      printf '%s\n' "$pane_path"
      if command -v zoxide >/dev/null 2>&1; then
        zoxide query -l 2>/dev/null || true
      else
        printf '%s\n' "$HOME"
        for d in "$HOME"/code/projects/*/ "$HOME"/code/*/; do [[ -d "$d" ]] && printf '%s\n' "${d%/}"; done
      fi
    } | awk 'NF && !seen[$0]++' \
      | while IFS= read -r d; do [[ -d "$d" ]] && printf '%s\n' "$d"; done \
      | sed "s|^$HOME\$|~|; s|^$HOME/|~/|"
  )"
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
    preview)
      local d="${1:-}"
      d="${d/#\~/$HOME}"
      ls -1p -- "$d" 2>/dev/null | head -200
      ;;
    *)
      echo "usage: space.sh sync [load|reconcile] | agent|shell N SID [CLIENT] | next|prev SID [CLIENT] | toggle SID [CLIENT] | close SID [CLIENT] | move-menu WID [CLIENT] | move-to WID SID [CLIENT] | respawn-home PANE | new-space CLIENT | open-dir DIR [CLIENT]" >&2
      return 2
      ;;
  esac
}

main "$@"
