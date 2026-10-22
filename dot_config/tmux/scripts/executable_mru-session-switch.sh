#!/usr/bin/env bash

set -euo pipefail

CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

# No `.`: tmux silently turns it into `_` when creating a session, so the
# switch-client to the typed name then fails.
is_valid_session_name() {
  [[ "$1" =~ ^[A-Za-z0-9_-]+$ ]]
}

# Append each session's agent rollup to its row: a dot in the colour of its
# most urgent tab, then counts. Same hues and the same ranking as the tab bar
# and agent-jump.sh: failed (red) > needs-input (yellow) > done (green; NOT
# while @agent_workflow is set — that tab renders untinted) > in flight (pink:
# running, a workflow, or driving an app) > idle. Paused (`paused`, or done/
# idle with the pause key's @agent_paused flag, folded in the format) ranks
# with idle, never as needing you (docs/agent-pause.md). One list-windows for all
# sessions; fields split on US because an empty option must stay a field.
decorate() {
  local us=$'\x1f'
  # FILENAME, not NR == FNR: with no agent state anywhere the rollup input is
  # EMPTY, NR == FNR then holds for the session list too, every row was
  # swallowed into d[] and the picker showed nothing at all.
  awk -F '\t' 'FILENAME == ARGV[1] { d[$1] = $2; next } { print $1 "\t" d[$1] }' \
    <(tmux list-windows -a -F "#{session_name}${us}#{?#{&&:#{@agent_paused},#{m/r:^(done|idle)$,#{@agent_state}}},paused,#{@agent_state}}${us}#{@agent_workflow}${us}#{@agent_cua}" 2>/dev/null |
      awk -F "$us" '
        function rank(st, wf, cua) {
          if (st == "failed") return 0
          if (st == "needs-input") return 1
          if (st == "done" && wf == "") return 2
          if (st == "running" || wf != "" || cua != "") return 3
          if (st != "") return 4
          return 9
        }
        {
          r = rank($2, $3, $4)
          if (r == 9) next
          seen[$1] = 1
          if (!($1 in best) || r < best[$1]) best[$1] = r
          if (r <= 2) need[$1]++
          else if (r == 3) work[$1]++
          else idle[$1]++
        }
        END {
          col[0] = "243;139;168"; col[1] = "249;226;175"; col[2] = "166;227;161"; col[3] = "245;194;231"; col[4] = "108;112;134"
          for (s in seen) {
            out = ""
            if (need[s]) out = need[s] " needs you"
            if (work[s]) out = out (out == "" ? "" : " · ") work[s] " working"
            if (out == "") out = idle[s] " idle"
            printf "%s\t\033[38;2;%sm●\033[0m \033[38;2;108;112;134m%s\033[0m\n", s, col[best[s]], out
          }
        }') \
    <(printf '%s\n' "$1")
}

main() {
  if ! command -v fzf >/dev/null 2>&1; then
    notify_user -k error -- "mru-session-switch: fzf not found in PATH"
    exit 1
  fi

  local current_session
  current_session="$(tmux display-message -p '#S')"

  # Each row is "name<TAB>agent rollup". The three field flags are what keep
  # the decoration from leaking into anything that matters:
  #   --nth 1         search only the name, so typing a NEW name like "work"
  #                   can't match some row's "2 working" and hijack Enter away
  #                   from create-by-typing
  #   --accept-nth 1  print only the name (--with-nth would change only what
  #                   is SHOWN; fzf would still print the whole line, and the
  #                   name check below would reject every pick)
  #   {1}             the preview gets the bare name too
  # --tabstop lines the rollups up in one column.
  local fzf_command=(fzf --print-query --reverse --ansi --info=hidden
    --delimiter $'\t' --nth 1 --accept-nth 1)
  local preview_script="$CURRENT_DIR/preview_session.sh"
  if [[ "${PREVIEW_DISABLED:-0}" != "1" && -x "$preview_script" ]]; then
    fzf_command+=(
      --preview "$preview_script {1}"
      --preview-window=down:70%:nowrap:noinfo
    )
  fi

  local list
  list="$({
    # Hidden = @space_hidden or listed in @space_hidden_sessions (pending,
    # btop-popup, …), the same test double-tap-switch.sh uses (2026-10-22).
    tmux list-sessions -F $'#{session_last_attached}\t#{?#{||:#{@space_hidden},#{m:* #{session_name} *, #{@space_hidden_sessions} }},1,0}\t#{session_name}' 2>/dev/null \
      | awk -F $'\t' -v cur="$current_session" '$2 == "0" && $3 != cur { print $1 "\t" $3 }' \
      | sort -t $'\t' -k1,1nr \
      | cut -f2-
  } || true)"

  if [[ -z "$list" ]]; then
    fzf_command+=(--header 'No other sessions — type a name and press Enter to create one')
  else
    list="$(decorate "$list")"
    fzf_command+=(--tabstop "$(printf '%s\n' "$list" | awk -F $'\t' '{ if (length($1) > m) m = length($1) } END { print m + 3 }')")
  fi

  local out status
  set +e
  out="$({ [[ -n "$list" ]] && printf '%s\n' "$list"; } | "${fzf_command[@]}")"
  status=$?
  set -e

  if [[ $status -ne 0 && $status -ne 1 ]]; then
    exit 0
  fi

  local query selection target
  query="$(printf '%s\n' "$out" | sed -n '1p' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  selection="$(printf '%s\n' "$out" | sed -n '2p')"

  if [[ -n "${selection:-}" ]]; then
    target="$selection"
  else
    target="${query:-}"
  fi

  [[ -n "${target:-}" ]] || exit 0

  if [[ "${target:-}" == "scratch" ]]; then
    exit 0
  fi

  if ! is_valid_session_name "$target"; then
    notify_user -k warn -- "Invalid session name (allowed: A-Z a-z 0-9 _ -): $target"
    exit 0
  fi

  if tmux has-session -t "=$target" 2>/dev/null; then
    tmux switch-client -t "=$target"
    exit 0
  fi

  tmux command-prompt -b -k -p "Create and go to [$target] session? [Y/n]" \
    "if-shell -F '#{m/r:^(Enter|C-m|y|Y)$,%1}' { new-session -d -s \"$target\" -c ~ ; switch-client -t \"=$target\" } { display-message Cancelled }"
}

main "$@"
