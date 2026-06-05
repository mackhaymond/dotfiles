#!/bin/bash

# Fling the focused (unpinned) window to the external display's on-demand `ext`
# space (created if needed) and FOLLOW focus to it. Multiple flung windows stack on
# `ext`; cycle them with hyper+z/x. No-op with a single display. Pinned-home apps
# are left alone -- they belong on their labels and would snap back anyway. `ext` is
# dissolved back into `main` on home-all / push-while-on-ext / undock (see
# yabai_common.sh and the README "External scratch-work space" note).
#
#   yabai_send_window_external.sh        (bound to hyper+fn+g)

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export USER="${USER:-$(id -un)}"

# shellcheck disable=SC2034  # consumed by yabai_load_cache (sourced from yabai_common.sh)
CACHE_FILE="${YABAI_WORKSPACE_CACHE:-${HOME}/.cache/yabai/workspace_cache.env}"
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/yabai_common.sh"

if ! yabai_load_cache; then
  "$SCRIPT_DIR/yabai_workspace_refresh.sh" >/dev/null 2>&1 || true
  yabai_load_cache || exit 0
fi

# Single display -> nowhere to fling.
[ "${DISPLAY_COUNT:-1}" -le 1 ] && exit 0
ext_disp="${EXTERNAL_DISPLAY_INDEX:-}"
case "$ext_disp" in ''|*[!0-9]*) exit 0 ;; esac

info=$(yabai -m query --windows --window 2>/dev/null) || exit 0
app=$(printf '%s' "$info" | jq -r '.app // ""' 2>/dev/null)
wid=$(printf '%s' "$info" | jq -r '.id // empty' 2>/dev/null)
cur=$(printf '%s' "$info" | jq -r '.space // empty' 2>/dev/null)
[ -z "$wid" ] && exit 0

# Pinned-home guard (mirrors yabai_send_window.sh): leave a pinned app that is
# sitting on its home space, and protect an Arc main window on main/school.
home=""
case "$app" in
  wezterm-gui|WezTerm) home=terminal ;;
  Todoist)             home=todo ;;
  Granola)             home=schedule ;;
  "Spark Mail")        home=mail ;;
  "Notion Calendar")   home=calendar ;;
  Messages)            home=messages ;;
  ChatGPT)             home=chatgpt ;;
  Codex)               home=codex ;;
esac
cur_label=$(yabai -m query --spaces --space "$cur" 2>/dev/null | jq -r '.label // ""' 2>/dev/null)
[ -n "$home" ] && [ "$cur_label" = "$home" ] && exit 0
if [ "$app" = "Arc" ]; then
  case "$cur_label" in main|school) exit 0 ;; esac
fi

# Already on ext -> nothing to do.
[ "$cur_label" = "ext" ] && exit 0

# Ensure the ext space exists on the external. Creating/relocating it churns yabai,
# so move the window onto ext with a short verify+retry (compared by label), then
# follow focus to it.
ext_idx=$(yabai_ensure_ext "$ext_disp") || exit 0
case "$ext_idx" in ''|*[!0-9]*) exit 0 ;; esac
for _try in 1 2 3 4 5 6; do
  yabai -m window "$wid" --space ext >/dev/null 2>&1 || true
  wsp=$(yabai -m query --windows --window "$wid" 2>/dev/null | jq -r '.space // empty' 2>/dev/null)
  wlbl=$(yabai -m query --spaces --space "$wsp" 2>/dev/null | jq -r '.label // ""' 2>/dev/null)
  [ "$wlbl" = "ext" ] && break
  sleep 0.2
done
yabai -m window "$wid" --focus >/dev/null 2>&1 ||
  yabai -m space --focus ext >/dev/null 2>&1 || true
