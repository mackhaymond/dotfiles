#!/bin/bash

# cmd+M in the pinned apps and the main browsers (Karabiner, see karabiner-base.json):
# minimize the focused window, unless it is pinned.
#
#   yabai_minimize.sh
#
# Pinned = yabai_is_pinned_at (yabai_common.sh): on its home space, or a main browser on
# main/school. Karabiner swallowed the key, so this does the minimize itself when the
# window is NOT pinned. If a query fails there is no telling, so it does nothing.
# yabai_unminimize.sh is the net under every path that does not come through here.

set -u

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

# shellcheck source=/dev/null  # required sibling: yabai_is_pinned_at
. "$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)/yabai_common.sh"

# "<id>|<space>|<app>". Split by hand, not with `read`: that would trim an app name's
# outer spaces into a pinned one.
info=$(yabai -m query --windows --window 2>/dev/null | jq -r '
  select(type == "object") | "\(.id)|\(.space)|\(.app)"' 2>/dev/null)
id=${info%%|*}; rest=${info#*|}
space=${rest%%|*}; app=${rest#*|}
case "$id" in ''|*[!0-9]*) exit 0 ;; esac
case "$space" in ''|*[!0-9]*) exit 0 ;; esac

# The leading "=" tells an unlabeled space from no answer.
label=$(yabai -m query --spaces --space "$space" 2>/dev/null | jq -r '
  select(type == "object") | "=\(.label // "")"' 2>/dev/null)
case "$label" in =*) label=${label#=} ;; *) exit 0 ;; esac

yabai_is_pinned_at "$app" "$label" && exit 0
yabai -m window "$id" --minimize >/dev/null 2>&1
exit 0
