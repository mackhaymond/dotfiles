#!/usr/bin/env bash
#
# SPACES home dashboard (docs/spaces.md, "Home: the dashboard"). Runs in the
# top pane of every space's home window (@space_dash_pane), above the home
# shell: the space's name, large, in its colour (@space_color); its folder
# and git branch with a dirty count; one line per agent of the space; the
# shell count; how many tabs are parked from it; the key hints (one row, or
# two when there is room; a narrow pane drops the least important first).
#
# Redraws when com.mackhaymond.tmux.agent-state or com.mackhaymond.tmux.focus
# is posted: ONE long-lived `notifyutil -w` per dashboard, read with a 60 s
# timeout, so an idle dashboard is a blocked read (no polling, no forks); the
# timeout is the slow fallback refresh. A burst of notices is drawn once.
# Each redraw is one tmux call; git runs only while the dashboard can be seen
# (its session attached and the home window current), at most every 3 s.
#
# `space-dash.sh --once` draws one frame and exits (tests, a quick look).
#
# Started and respawned by space.sh (sync, respawn-home); never by hand
# outside tmux: without $TMUX a plain `tmux` reaches the default server.

set -uo pipefail
[[ "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" == *[Uu][Tt][Ff]* ]] || export LC_CTYPE=en_US.UTF-8

TMUX_BIN="${SPACE_TMUX_BIN:-tmux}"
t() { "$TMUX_BIN" "$@"; }
PANE="${TMUX_PANE:-}"
if [[ -z "${TMUX:-}" || -z "$PANE" ]]; then
  echo "space-dash.sh: not in a tmux pane; refusing" >&2
  exit 3
fi
US=$'\x1f'
NOTICES=(com.mackhaymond.tmux.agent-state com.mackhaymond.tmux.focus)
FALLBACK=60       # seconds between redraws with no notice at all
GIT_EVERY=3       # git at most this often
ROWS=9            # the dashboard's height: space.sh's DASH_ROWS

# ── colours (catppuccin mocha) ─────────────────────────────────────────────
E=$'\e'
fg() { local h="${1#\#}"; printf '%s[38;2;%d;%d;%dm' "$E" "0x${h:0:2}" "0x${h:2:2}" "0x${h:4:2}"; }
C_TEXT="$(fg '#cdd6f4')" C_SUB="$(fg '#a6adc8')" C_DIM="$(fg '#6c7086')"
C_RED="$(fg '#f38ba8')" C_YEL="$(fg '#f9e2af')" C_GRN="$(fg '#a6e3a1')" C_PINK="$(fg '#f5c2e7')"
C_VIO="$(fg '#7f6bff')"   # paused ⏸: electric violet (the agent UIs' `violet`)
BOLD="$E[1m" RST="$E[0m"

# ── the big name: a 3x5 pixel font drawn with half blocks (3 rows) ─────────
# One glyph = five rows of three bits, top to bottom.
glyph() {
  case "$1" in
    a) G=(010 101 111 101 101) ;; b) G=(110 101 110 101 110) ;; c) G=(011 100 100 100 011) ;;
    d) G=(110 101 101 101 110) ;; e) G=(111 100 110 100 111) ;; f) G=(111 100 110 100 100) ;;
    g) G=(011 100 101 101 011) ;; h) G=(101 101 111 101 101) ;; i) G=(111 010 010 010 111) ;;
    j) G=(001 001 001 101 010) ;; k) G=(101 101 110 101 101) ;; l) G=(100 100 100 100 111) ;;
    m) G=(101 111 111 101 101) ;; n) G=(110 101 101 101 101) ;; o) G=(010 101 101 101 010) ;;
    p) G=(110 101 110 100 100) ;; q) G=(010 101 101 110 011) ;; r) G=(110 101 110 101 101) ;;
    s) G=(011 100 010 001 110) ;; t) G=(111 010 010 010 010) ;; u) G=(101 101 101 101 111) ;;
    v) G=(101 101 101 101 010) ;; w) G=(101 101 111 111 101) ;; x) G=(101 101 010 101 101) ;;
    y) G=(101 101 010 010 010) ;; z) G=(111 001 010 100 111) ;;
    0) G=(111 101 101 101 111) ;; 1) G=(010 110 010 010 111) ;; 2) G=(110 001 010 100 111) ;;
    3) G=(110 001 010 001 110) ;; 4) G=(101 101 111 001 001) ;; 5) G=(111 100 110 001 110) ;;
    6) G=(011 100 111 101 111) ;; 7) G=(111 001 010 010 010) ;; 8) G=(111 101 111 101 111) ;;
    9) G=(111 101 111 001 110) ;; -) G=(000 000 111 000 000) ;; _) G=(000 000 000 000 111) ;;
    .) G=(000 000 000 000 010) ;; *) G=(000 000 000 000 000) ;;
  esac
}
BIG=("" "" "") BIG_W=0 BIG_FOR=""
big_name() {
  [[ "$1" == "$BIG_FOR" ]] && return 0
  BIG_FOR="$1" BIG=("" "" "")
  local s ch r c top bot k
  s="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  for ((k = 0; k < ${#s}; k++)); do
    ch="${s:k:1}"
    glyph "$ch"
    G+=(000)
    for r in 0 1 2; do
      ((k)) && BIG[r]+=" "
      for c in 0 1 2; do
        top="${G[2 * r]:c:1}" bot="${G[2 * r + 1]:c:1}"
        case "$top$bot" in 11) BIG[r]+="█" ;; 10) BIG[r]+="▀" ;; 01) BIG[r]+="▄" ;; *) BIG[r]+=" " ;; esac
      done
    done
  done
  BIG_W=$((${#s} ? 4 * ${#s} - 1 : 0))
}

# ── a line, clipped to the pane width ──────────────────────────────────────
# seg COLOUR TEXT appends TEXT (counted one cell per character) while there is
# room; the colour codes are never counted.
LINE="" LW=0 W=80
seg() {
  local room=$((W - LW)) s="$2"
  ((room > 0)) || return 0
  ((${#s} > room)) && s="${s:0:room-1}…"
  LINE+="$1$s$RST" LW=$((LW + ${#s}))
}
pad() { local n=$(($1 - LW)); ((n > 0)) && seg "" "$(printf '%*s' "$n" '')"; return 0; }
FRAME=()
emit() { FRAME+=("$LINE"); LINE="" LW=0; }

# ── the key hints ──────────────────────────────────────────────────────────
# "ROW PRIORITY TEXT", in display order. Row 1 moves around, row 2 makes and
# manages; priority 1 is kept longest. A pane too narrow for them drops whole
# hints, least important first: never wraps, never cuts one. Checked against
# spaces.conf / tmux.conf.tmpl / wezterm.lua.tmpl (2026-10-22): ⌘N is space
# N's home (User18..26), ⌥W + letter / digit / ⇧letter the jump table's
# agent-or-shell / pin / diff popup, ⌥` space.sh toggle, ⌥N the new-space
# picker, ⌘T / ⌘R prefix c / k (this tab's folder), ⌘⇧T / ⌘⇧R prefix C-t / K
# (space.sh pick-tab: which space, at its folder), prefix v the tab
# selection, prefix Q the close-space menu.
HINTS=(
  "1 2 ⌘1-9 space"
  "1 1 ⌥W letter jump"
  "1 9 ⌥W digit pin"
  "1 11 ⌥W ⇧letter diff"
  "1 5 ⌥\` agent↔shell"
  "2 4 ⌘T new shell"
  "2 3 ⌘R new agent"
  "2 10 ⌘⇧T/R in a space"
  "2 6 ⌥N new space"
  "2 8 prefix v select tabs"
  "2 7 prefix Q close space"
)
HSEP=" · "
# hint_line ROW ROOM: HINT_OUT = the hints of ROW (0: all of them) that fit
# in ROOM cells, joined by HSEP; HINT_DROPPED = how many did not. No forks.
HINT_OUT="" HINT_DROPPED=0
hint_line() {
  local want="$1" room="$2" h row rest i total cnt maxp drop
  local -a T=() P=() K=()
  for h in "${HINTS[@]}"; do
    row="${h%% *}" rest="${h#* }"
    ((want && row != want)) && continue
    P+=("${rest%% *}") T+=("${rest#* }") K+=(1)
  done
  HINT_DROPPED=0
  while :; do
    total=0 cnt=0
    for i in "${!T[@]}"; do
      ((K[i])) && total=$((total + ${#T[i]})) cnt=$((cnt + 1))
    done
    ((cnt > 1)) && total=$((total + (cnt - 1) * ${#HSEP}))
    ((total <= room || cnt == 0)) && break
    maxp=0 drop=0
    for i in "${!T[@]}"; do
      ((K[i] && P[i] > maxp)) && maxp=${P[i]} drop=$i
    done
    K[drop]=0 HINT_DROPPED=$((HINT_DROPPED + 1))
  done
  HINT_OUT=""
  for i in "${!T[@]}"; do
    ((K[i])) && HINT_OUT+="${HINT_OUT:+$HSEP}${T[i]}"
  done
  return 0
}

# ── git, cached ────────────────────────────────────────────────────────────
G_DIR="" G_AT=0 G_BRANCH="" G_DIRTY=0 G_AB="" G_REPO=0
git_info() {
  local dir="$1" now line out
  now="$(date +%s)"
  [[ "$dir" == "$G_DIR" ]] && ((now - G_AT < GIT_EVERY)) && return 0
  G_DIR="$dir" G_AT="$now" G_BRANCH="" G_DIRTY=0 G_AB="" G_REPO=0
  command -v git >/dev/null 2>&1 || return 0
  out="$(cd "$dir" 2>/dev/null && git --no-optional-locks status --porcelain=v2 --branch 2>/dev/null)" || return 0
  G_REPO=1
  while IFS= read -r line; do
    case "$line" in
      "# branch.head "*) G_BRANCH="${line#\# branch.head }" ;;
      "# branch.ab "*)
        local a b
        read -r _ _ a b <<<"$line"
        a="${a#+}" b="${b#-}"
        ((a)) && G_AB+=" ↑$a"
        ((b)) && G_AB+=" ↓$b"
        ;;
      "#"*) ;;
      ?*) G_DIRTY=$((G_DIRTY + 1)) ;;
    esac
  done <<<"$out"
}

# ── one frame ──────────────────────────────────────────────────────────────
VISIBLE=0
render() {
  local out
  out="$(t display-message -p -t "$PANE" "S${US}#{pane_id}${US}#{session_name}${US}#{session_path}${US}#{@space_color}${US}#{@space_key}${US}#{pane_width}${US}#{pane_height}${US}#{&&:#{session_attached},#{window_active}}${US}#{?window_zoomed_flag,0,#{window_height}}" \
    ";" list-windows -t "$PANE" -F "W${US}#{?#{&&:#{@agent_paused},#{m/r:^(done|idle)$,#{@agent_state}}},paused,#{@agent_state}}${US}#{@agent_workflow}${US}#{@agent_cua}${US}#{@agent_key}${US}#{@agent_summary}${US}#{window_name}${US}#{@agent_detail}" \
    ";" list-windows -t "=stash" -F "P${US}#{@stash_origin}" 2>/dev/null)"
  [[ -n "$out" ]] || return 1
  local kind me="" name="" path="" color="" key="" H=9 WH=0 a b c d e f g h
  local -a A_STATE=() A_WF=() A_CUA=() A_KEY=() A_LABEL=() A_DETAIL=()
  local shells=0 parked=0 origins=()
  while IFS="$US" read -r kind a b c d e f g h; do
    case "$kind" in
      S) IFS="$US" read -r me name path color key W H VISIBLE WH <<<"$a$US$b$US$c$US$d$US$e$US$f$US$g$US$h" ;;
      W)
        if [[ -n "$a" ]]; then
          A_STATE+=("$a") A_WF+=("$b") A_CUA+=("$c") A_KEY+=("$d") A_LABEL+=("${e:-$f}") A_DETAIL+=("$g${h:+ $h}")
        else
          shells=$((shells + 1))
        fi
        ;;
      P) origins+=("$a") ;;
    esac
  done <<<"$out"
  # tmux answers a target that no longer exists with empty fields, not an
  # error: our own pane id coming back is the proof the pane is still there.
  [[ "$me" == "$PANE" ]] || return 1
  local o
  for o in "${origins[@]+"${origins[@]}"}"; do [[ "$o" == "$name" ]] && parked=$((parked + 1)); done
  [[ "$W" =~ ^[0-9]+$ ]] || W=80
  [[ "$H" =~ ^[0-9]+$ ]] || H=9
  # A window resize shares the change out among its panes; take back our
  # height (the shell below gets the rest). Not while zoomed, not when the
  # window is too short for both; the resize's own WINCH redraws.
  if ((H != ROWS && WH >= ROWS + 6)); then
    t resize-pane -t "$PANE" -y "$ROWS" 2>/dev/null
  fi
  [[ -n "$color" ]] || color="#fab387"
  local C_SPACE; C_SPACE="$(fg "$color")"
  local shown="$path"
  [[ "$shown" == "$HOME" ]] && shown="~"
  [[ "$shown" == "$HOME/"* ]] && shown="~/${shown#"$HOME"/}"
  [[ "$VISIBLE" == 1 || "$G_DIR" != "$path" ]] && git_info "$path"

  FRAME=()
  # Rows 1-3: the name, large (or plain and bold when the pane is too narrow
  # for it), and beside it the folder, git, the counts.
  local big=1 col
  big_name "$name"
  ((BIG_W + 40 > W)) && big=0
  col=$((big ? BIG_W + 5 : 0))
  local r
  for r in 0 1 2; do
    LINE="" LW=0
    if ((big)); then seg "" "  "; seg "$C_SPACE$BOLD" "${BIG[r]}"; seg "" "   "
    elif ((r == 0)); then seg "" "  "; seg "$C_SPACE$BOLD" "$name"; seg "" "  "
    else seg "" "  "
    fi
    case "$r" in
      0)
        [[ -n "$key" ]] && { seg "$C_SPACE$BOLD" "$key"; seg "" "  "; }
        seg "$C_TEXT" "$shown"
        ;;
      1)
        if ((G_REPO)); then
          seg "$C_SUB" " ${G_BRANCH:-?}"
          if ((G_DIRTY)); then seg "$C_YEL" "  ±$G_DIRTY"; else seg "$C_DIM" "  clean"; fi
          [[ -n "$G_AB" ]] && seg "$C_SUB" " $G_AB"
        else
          seg "$C_DIM" "no git"
        fi
        ;;
      2)
        local n=${#A_STATE[@]} sa=s ss=s
        ((n == 1)) && sa=""
        ((shells == 1)) && ss=""
        seg "$C_TEXT" "$n"; seg "$C_DIM" " agent$sa · "
        seg "$C_TEXT" "$shells"; seg "$C_DIM" " shell$ss · "
        seg "$C_TEXT" "$parked"; seg "$C_DIM" " parked"
        ;;
    esac
    emit
  done
  # Then one line per agent (⌥W and its letter reach it), as many as fit above
  # the hint row; a last "+N more" when they do not.
  local rows=$((H - 5)) n=${#A_STATE[@]} i st glyph gc
  ((rows < 0)) && rows=0
  LINE="" LW=0; emit
  if ((n == 0)); then
    ((rows)) && { seg "" "  "; seg "$C_DIM" "no agents here · ⌘R starts one"; emit; rows=$((rows - 1)); }
  fi
  for ((i = 0; i < n && rows > 0; i++)); do
    if ((rows == 1 && i < n - 1)); then
      seg "" "  "; seg "$C_DIM" "+$((n - i)) more"; emit; rows=0; break
    fi
    st="${A_STATE[i]}"
    if [[ "$st" == failed ]]; then glyph="✕" gc="$C_RED"
    elif [[ "$st" == needs-* ]]; then glyph="◉" gc="$C_YEL"
    elif [[ "$st" == done && -z "${A_WF[i]}" ]]; then glyph="✓" gc="$C_GRN"
    elif [[ "$st" == running || -n "${A_WF[i]}" || -n "${A_CUA[i]}" ]]; then glyph="◐" gc="$C_PINK"
    elif [[ "$st" == paused ]]; then glyph="⏸" gc="$C_VIO"
    else glyph="○" gc="$C_DIM"; fi
    seg "" "  "; seg "$gc" "$glyph"; seg "" " "
    if [[ -n "${A_KEY[i]}" ]]; then seg "$C_SPACE" "${A_KEY[i]}"; else seg "" " "; fi
    seg "" "  "; seg "$C_TEXT" "${A_LABEL[i]//$'\t'/ }"
    [[ -n "${A_DETAIL[i]}" ]] && { seg "" "  "; seg "$C_DIM" "${A_DETAIL[i]//$'\t'/ }"; }
    emit
    rows=$((rows - 1))
  done
  # The key hints: one row when they all fit; two (moving around, then making
  # and managing) when they do not and an agent row is spare; whatever does
  # not fit is dropped by priority (hint_line).
  local room=$((W - 2)) hr
  hint_line 0 "$room"
  if ((HINT_DROPPED && rows > 0)); then
    rows=$((rows - 1))
    while ((rows > 0)); do emit; rows=$((rows - 1)); done
    for hr in 1 2; do
      hint_line "$hr" "$room"
      seg "" "  "; seg "$C_DIM" "$HINT_OUT"; emit
    done
  else
    while ((rows > 0)); do emit; rows=$((rows - 1)); done
    seg "" "  "; seg "$C_DIM" "$HINT_OUT"; emit
  fi
  # Draw: home, each line cleared to its end, the rest of the pane cleared.
  local buf="$E[H" k
  for ((k = 0; k < ${#FRAME[@]} && k < H; k++)); do
    ((k)) && buf+=$'\r\n'
    buf+="${FRAME[k]}$E[K"
  done
  printf '%s%s[J' "$buf" "$E"
}

if [[ "${1:-}" == "--once" ]]; then
  render
  printf '\n'
  exit 0
fi

# ── the loop ───────────────────────────────────────────────────────────────
stty -echo 2>/dev/null
printf '%s[?25l%s[H%s[2J' "$E" "$E" "$E"
WATCH=""
trap cleanup EXIT
trap 'exit 0' HUP TERM INT
trap ':' WINCH
start_watch() {
  if command -v notifyutil >/dev/null 2>&1; then
    local -a w=()
    local n
    for n in "${NOTICES[@]}"; do w+=(-w "$n"); done
    exec 3< <(exec notifyutil "${w[@]}")
  else
    exec 3< <(while sleep 15; do echo tick; done)
  fi
  WATCH=$!
}
start_watch
# The wait for a notice is a `read` in a background subshell, waited for with
# `wait`: a trapped signal interrupts `wait` (a resize, SIGWINCH, redraws at
# once) but not `read`. The reader survives the interruption and is waited
# for again, so no notice is lost and nothing forks while idle.
READER=""
cleanup() {
  [[ -n "$READER" ]] && kill "$READER" 2>/dev/null
  [[ -n "$WATCH" ]] && kill "$WATCH" 2>/dev/null
  printf '%s[?25h' "$E"
}
while :; do
  # A draw that fails means its pane or its whole server may be gone (a
  # killed server does not always deliver the SIGHUP): never linger as an
  # orphan. The 60 s fallback bounds how long that takes.
  if ! render; then
    [[ "$(t display-message -p -t "$PANE" '#{pane_id}' 2>/dev/null)" == "$PANE" ]] || exit 0
  fi
  if [[ -z "$READER" ]]; then
    read -r -t "$FALLBACK" -u 3 _ &
    READER=$!
  fi
  wait "$READER"
  rc=$?
  if ((rc > 128)) && kill -0 "$READER" 2>/dev/null; then
    continue                     # a signal (a resize): redraw, same reader
  fi
  READER=""
  if ((rc == 0)); then
    # A burst (an agent's state flips, a switch posts focus twice) is one
    # redraw: wait until the notices pause.
    while read -r -t 0.15 -u 3 _; do :; done
  elif ((rc == 1)); then
    # EOF: the watcher died. Start another, never in a tight loop.
    exec 3<&-
    sleep 5
    start_watch
  fi
  # Otherwise the fallback timeout: redraw.
done
