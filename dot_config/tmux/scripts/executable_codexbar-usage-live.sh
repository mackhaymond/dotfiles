#!/bin/bash
#
# codexbar-usage-live.sh — record the subscription usage Claude Code already
# knows, so the usage cache moves between endpoint polls.
#
# Claude Code hands every status line a rate_limits object: the 5-hour and
# 7-day utilization off the headers of that session's latest API response.
# That is live after every turn and costs no request, where the usage endpoint
# is polled, has a budget shared with every session, and answers
# rate_limit_error under heavy use. ~/.claude/statusline.sh passes the object
# here, backgrounded:
#
#   codexbar-usage-live.sh '{"five_hour":{"used_percentage":7,"resets_at":E},...}'
#
# ONE SAMPLE FILE FOR EVERY SESSION, MERGED, NEVER OVERWRITTEN. An idle
# session repaints with whatever it last saw, so within one window (the same
# resets_at) the HIGHER reading wins — usage only rises inside a window — and
# across windows the later window wins. A window whose resets_at has passed is
# dropped. Only a changed sample is written, and only a written sample wakes
# codexbar-usage-status.sh --merge-live, which folds it into usage.json; most
# repaints end at the comparison.
#
# Sample (claude-live.json):
#   {"five_hour": {"used": 7, "resets_at": E, "t": E}, "seven_day": {...}}
#   used       percent, exactly as Claude Code reported it
#   resets_at  epoch seconds
#   t          when THIS value was first seen, not when it was last repainted

export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

CACHE_DIR="$HOME/.cache/codexbar-tmux"
SAMPLE="$CACHE_DIR/claude-live.json"
SRC="$HOME/.config/tmux/scripts/codexbar-usage-status.sh"

incoming="${1:-}"
[[ -n "$incoming" ]] || exit 0
command -v jq >/dev/null 2>&1 || exit 0
mkdir -p "$CACHE_DIR" 2>/dev/null || exit 0

prev='{}'
if [[ -f "$SAMPLE" ]]; then
  prev="$(jq -c . "$SAMPLE" 2>/dev/null)" || prev='{}'
  [[ -n "$prev" ]] || prev='{}'
fi

merged="$(jq -nc --argjson prev "$prev" --argjson in "$incoming" --argjson now "$(date +%s)" '
  def reading($r):
    if ($r | type) == "object"
       and ($r.used_percentage | type) == "number"
       and ($r.resets_at | type) == "number"
    then {used: $r.used_percentage, resets_at: $r.resets_at, t: $now}
    else null end;
  def open($x): if ($x | type) == "object" and ($x.resets_at // 0) > $now then $x else null end;
  # $a stored, $b incoming. Ten minutes of slack on "same window": the two
  # are the same header value in practice, and no window is that short.
  def pick($a; $b):
    if $a == null then $b
    elif $b == null then $a
    elif (($a.resets_at - $b.resets_at) | fabs) <= 600 then
      (if $b.used > $a.used then $b else $a end)
    elif $b.resets_at > $a.resets_at then $b
    else $a end;
  { five_hour: pick(open($prev.five_hour); open(reading($in.five_hour))),
    seven_day: pick(open($prev.seven_day); open(reading($in.seven_day))) }
  | with_entries(select(.value != null))
' 2>/dev/null)" || exit 0
[[ -n "$merged" ]] || exit 0
[[ "$merged" == "$prev" ]] && exit 0

tmp="$(mktemp "${SAMPLE}.tmp.XXXXXX" 2>/dev/null)" || exit 0
if printf '%s\n' "$merged" >"$tmp" 2>/dev/null && mv -f "$tmp" "$SAMPLE" 2>/dev/null; then
  [[ -x "$SRC" ]] && "$SRC" --merge-live >/dev/null 2>&1
else
  rm -f "$tmp" 2>/dev/null
fi
exit 0
