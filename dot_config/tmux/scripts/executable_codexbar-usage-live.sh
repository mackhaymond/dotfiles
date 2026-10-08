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
# ONE ACCOUNT PER SAMPLE. Those rules hold within one account only. claude-swap
# (cswap) can switch the login to another account at any minute, and then the
# old account's 90% would outrank the new one's 3% — same window, or a window
# that ends later — and stick until that window ends: up to a week. So the
# sample is stamped with the account logged in when it was written (read from
# ~/.claude.json, which cswap rewrites on every switch; the same key as
# codexbar-usage-status.sh, see "Which Claude account" there), and a sample
# for another account is replaced, never merged into.
#
# Stamping alone is not enough: a session that sat idle across the switch
# still repaints the OLD account's last reading, and would get the new stamp.
# That reading carries the old account's resets_at, though, so on a switch the
# old account's open windows become a FENCE: an incoming reading whose
# resets_at is within 10 minutes of a fenced one is dropped. The fence lapses
# with those windows. The cost: if the new account's window happens to end
# within 10 minutes of the old one's, its live readings for that window are
# dropped too, and the bar falls back to the endpoint poll for it.
#
# Sample (claude-live.json):
#   {"five_hour": {"used": 7, "resets_at": E, "t": E}, "seven_day": {...},
#    "account": "me@example.com/<org uuid>",
#    "fence": [{"a": "<previous account>", "r": E}, ...]}
#   used       percent, exactly as Claude Code reported it
#   resets_at  epoch seconds
#   t          when THIS value was first seen, not when it was last repainted
#   account    whose numbers; absent when ~/.claude.json could not be read
#   fence      earlier accounts' still-open windows; absent when empty

export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

CACHE_DIR="$HOME/.cache/codexbar-tmux"
SAMPLE="$CACHE_DIR/claude-live.json"
SRC="$HOME/.config/tmux/scripts/codexbar-usage-status.sh"
# The SESSION's own config, CLAUDE_CONFIG_DIR included: the rate_limits this
# script is handed are that session's, so the stamp is that session's account
# (a `cswap run N` profile's, there). codexbar-usage-status.sh ignores samples
# stamped for an account other than the default login.
CLAUDE_GLOBAL_CONFIG="${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json"

incoming="${1:-}"
[[ -n "$incoming" ]] || exit 0
command -v jq >/dev/null 2>&1 || exit 0
mkdir -p "$CACHE_DIR" 2>/dev/null || exit 0

prev='{}'
if [[ -f "$SAMPLE" ]]; then
  prev="$(jq -c . "$SAMPLE" 2>/dev/null)" || prev='{}'
  [[ -n "$prev" ]] || prev='{}'
fi

# The same key codexbar-usage-status.sh computes (CLAUDE_ACCOUNT_JQ there):
# lower-cased email, plus "/<organization uuid>" when there is one. Empty when
# the file is missing or holds no login; then nothing is stamped or fenced.
account=''
if [[ -f "$CLAUDE_GLOBAL_CONFIG" ]]; then
  account="$(jq -r '(.oauthAccount // {}) as $a
    | (($a.emailAddress // "") | tostring) as $e
    | (($a.organizationUuid // "") | tostring) as $o
    | if $e == "" then "" else ($e | ascii_downcase) + (if $o == "" then "" else "/" + $o end) end
  ' "$CLAUDE_GLOBAL_CONFIG" 2>/dev/null)" || account=''
fi

# Unreadable (a torn write, a logout) means UNKNOWN, not "the same account as
# last time": right after a switch, merging a reading into the stamped sample
# would file the new account's numbers under the old stamp, and the next
# readable write would then fence them — up to a week for the weekly window.
# A stamped sample is left alone until the account can be read again. (An
# unstamped one, as before stamping existed, still merges.)
if [[ -z "$account" ]]; then
  prev_account="$(printf '%s' "$prev" | jq -r '.account // empty | strings' 2>/dev/null)"
  [[ -z "$prev_account" ]] || exit 0
fi

merged="$(jq -nc --argjson prev "$prev" --argjson in "$incoming" --argjson now "$(date +%s)" \
  --arg acct "$account" '
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

  (($prev.account // "") | strings) as $pa
  # Another account was logged in at the last write: drop its readings and
  # fence their windows. A sample with no stamp at all is adopted as it is.
  # That is what every sample looked like before stamping, and fencing it
  # would drop the live numbers of this very account until its windows end.
  | (if $acct != "" and $pa != "" and $pa != $acct then
       { five_hour: null, seven_day: null,
         fence: ([ ($prev.fence // [])[]? | objects | select(.a != $acct) ]
                 + [ $prev.five_hour, $prev.seven_day | objects
                     | select((.resets_at | type) == "number")
                     | {a: $pa, r: .resets_at} ]) }
     else
       { five_hour: $prev.five_hour, seven_day: $prev.seven_day,
         fence: [ ($prev.fence // [])[]? | objects ] }
     end) as $base
  | [ $base.fence[] | select((.r | type) == "number" and .r > $now) ] as $fence
  | def unfenced($x):
      if $x == null then null
      elif any($fence[]; ((.r - $x.resets_at) | fabs) <= 600) then null
      else $x end;
  { five_hour: pick(open($base.five_hour); unfenced(open(reading($in.five_hour)))),
    seven_day: pick(open($base.seven_day); unfenced(open(reading($in.seven_day)))) }
  | with_entries(select(.value != null))
  | if $acct != "" then .account = $acct else . end
  | if ($fence | length) > 0 then .fence = $fence else . end
' 2>/dev/null)" || exit 0
[[ -n "$merged" ]] || exit 0
[[ "$merged" == "$prev" ]] && exit 0

tmp="$(mktemp "${SAMPLE}.tmp.XXXXXX" 2>/dev/null)" || exit 0
if printf '%s\n' "$merged" >"$tmp" 2>/dev/null && mv -f "$tmp" "$SAMPLE" 2>/dev/null; then
  # Without CLAUDE_CONFIG_DIR: under `cswap run N` it names the profile. That
  # is right for THIS script's stamp (the session's own account), wrong for
  # the status script, whose token is the default login's.
  [[ -x "$SRC" ]] && env -u CLAUDE_CONFIG_DIR "$SRC" --merge-live >/dev/null 2>&1
else
  rm -f "$tmp" 2>/dev/null
fi
exit 0
