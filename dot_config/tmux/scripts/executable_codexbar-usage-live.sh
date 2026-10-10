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
#   codexbar-usage-live.sh '{"five_hour":{"used_percentage":7,"resets_at":E},...}' \
#     [<session_id> <cost.total_api_duration_ms>]
#
# The last two are optional (see "ONLY NEWS FROM THE API COUNTS" below); a
# caller without them still feeds the numbers, it just never confirms them.
#
# ONE SAMPLE FILE FOR EVERY SESSION, MERGED, NEVER OVERWRITTEN. An idle
# session repaints with whatever it last saw, so the live feed alone cannot
# tell a genuine drop from a stale number: within one window (the same
# resets_at) the HIGHER reading wins, and across windows the later window
# wins. A window whose resets_at has passed is dropped. Only a changed sample
# is written, and only a written sample wakes codexbar-usage-status.sh
# --merge-live, which folds it into usage.json; most repaints end at the
# comparison.
#
# THE ENDPOINT CAN RETIRE A READING. Usage does not only rise inside a window
# (2026-10-09: the weekly fell from 22% to 10% mid-window), and higher-wins
# alone then keeps the stale high number until the window ends. The usage
# endpoint is the authority: when its reading, taken 10+ minutes after a live
# value was first seen (`t`), is lower, the status script shows the endpoint's
# number and deletes that window from this sample, under its lock, so the
# sessions' current readings can refill it (see apply_claude_live_sample and
# retire_overridden_live_readings there). It also records the dropped number
# in `retired`, and this script refuses that exact number for that window and
# account until the window closes: idle sessions that still hold it would
# otherwise repaint it straight back. A reading refused that way is not news
# about now, so it does not refresh `seen` either. (Idle repaints are kept
# out by "ONLY NEWS FROM THE API COUNTS" below too; the record also covers a
# caller that cannot prove a call, and one whose newest call still carries
# the retired number.)
#
# UNCHANGED IS STILL A READING. A repaint that brings exactly the numbers the
# sample already holds says "still this, as of now", and the status script
# uses that to move usage.json's updated_at (it confirms the block only when
# both windows agree with it; see merge_live_claude_locked there). So the
# sample carries `seen`, the last time a session repainted with a current
# reading for this account (one at the frontier: none of its readings below
# the sample's own), and an unchanged sample is still rewritten — and
# --merge-live still woken — once `seen` is a minute old. That caps the extra
# writes at one a minute however many sessions repaint.
#
# ONLY NEWS FROM THE API COUNTS. The status line hands over the session's LAST
# rate_limits however old they are, so an idle session repainting a cached
# 35%/22% says nothing about now: the account may have moved on (claude.ai,
# the phone, another machine) since that session last talked to the API. Such
# a repaint used to stamp `seen` and so advance usage.json's updated_at on
# numbers hours old. So `seen` now needs evidence that the session heard from
# the API since its last report, judged per session id against what that
# session reported last time. Either of two things counts:
#
#   - its cost.total_api_duration_ms moved (Claude Code's total time waiting
#     on API responses; moves on every SUCCESSFUL stream, any direction
#     counts, a restarted session starts over), or
#   - its own rate_limits changed: a window it reports now that it did not
#     report before, or one whose used/resets_at differs. Claude Code takes
#     rate_limits from a process-wide utilization that response headers
#     update, but so do the quota probe and the error path of a 429 ("usage
#     limit reached"), neither of which moves the API time. Without this, a
#     session that hit the limit repainted 100% with its API time frozen, every
#     repaint was dropped, and the bar sat at 97% until the next endpoint poll
#     (2.1.296, 2026-10-09 review). A window merely DISAPPEARING does not
#     count: Claude Code drops a window when its resets_at passes, and the
#     idle session's other, stale window must not ride in on that.
#
# An idle session repaints identical numbers with identical API time, so it
# still never counts. The evidence is kept in claude-live-sessions.json,
# {"<session id>": {"ms": N, "r": {"five_hour": [used, resets_at], ...},
# "at": E}}, `at` being that session's last REPORT (refreshed at most once a
# minute while nothing else changes), and capped at the 64 most recently
# reporting sessions, so a session that is painting is never the one evicted:
# 64 is far above the sessions one machine runs at once, and a cap below that
# number is a cliff (20 entries and 21 sessions calling in turn admitted 0 of
# 84 readings: each evicted the next to report). A session not in the map
# (new, evicted, or from before it existed) is recorded but not believed on
# that report — its rate_limits could be any age — so a new session's first
# report never counts, and its next change does.
#
# Costs, stated plainly. The map is written (mktemp + mv) on every report that
# carries evidence and at most once a minute per painting session otherwise;
# an idle repaint inside that minute writes nothing. It never wakes
# --merge-live. It has NO lock — the status line must not wait on one — so
# sessions reporting within a few milliseconds of each other overwrite each
# other's entries, last writer wins: four sessions reporting at the same
# instant, forty times over, kept only 40 of 160 updates (2026-10-09 review).
# Real reports are far sparser — each session's status line is debounced and
# fires around its own turns — but the cost of a lost update is what matters,
# and it is bounded: that session's NEXT report is judged against an older
# entry, so it counts as evidence even if the session has gone idle since
# (its numbers are then those of the call whose update was lost, which was
# genuine news a moment ago), or against no entry at all (a first sighting,
# ignored once). One report misjudged per lost update, never a stuck state.
# The cap is a cliff in the same way the old 20 was: 65+ sessions all
# reporting in strict rotation would each evict the next. Without the two
# arguments, no `seen`.
#
# The same evidence gates the NUMBERS, not only `seen`: a report without it
# is not merged at all — it may neither fill an empty window nor raise one, in
# either window. Such a repaint is by construction a copy of an older
# reading, so it can only be as current as what the reporting sessions say,
# or staler; admitting it can only make the sample worse. Narrower rules leave
# holes: after the endpoint retires a stale number the window is empty, and
# "higher wins" then hands it to whichever idle session repaints first — its
# stale 21%, then once that is retired another's 18% — each holding the bar
# for the 10+ minutes until the next endpoint reading, while the busy session
# reporting the true 10% is outranked (2026-10-09 review). It also keeps an
# idle session's reading for an account switched away from out of the
# sample. The cost is one report's delay for a session the map does not know
# yet. A caller with no evidence to give (no session id or API time) is
# merged as before — the live feed must not die if the status line ever
# loses those fields — and only the `retired` record protects that path.
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
#    "fence": [{"a": "<previous account>", "r": E}, ...],
#    "retired": [{"w": "seven_day", "u": 22, "r": E, "a": "<account>"}, ...],
#    "seen": E}
#   used       percent, exactly as Claude Code reported it
#   resets_at  epoch seconds
#   t          when THIS value was first seen, not when it was last repainted
#   account    whose numbers; absent when ~/.claude.json could not be read
#   fence      earlier accounts' still-open windows; absent when empty
#   retired    readings the endpoint overrode (window key, used, resets_at,
#              account), written by the status script; an incoming reading
#              equal to one of the current account's is dropped; each lapses
#              with its window and survives account switches
#   seen       when a session that had just heard from the API last repainted
#              with open, unfenced, unretired readings for this account, none
#              lower than the sample's — refreshed at most once a minute while
#              the numbers hold still; dropped with the readings on a switch

export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

CACHE_DIR="$HOME/.cache/codexbar-tmux"
SAMPLE="$CACHE_DIR/claude-live.json"
SESSIONS="$CACHE_DIR/claude-live-sessions.json"
SRC="$HOME/.config/tmux/scripts/codexbar-usage-status.sh"
# The SESSION's own config, CLAUDE_CONFIG_DIR included: the rate_limits this
# script is handed are that session's, so the stamp is that session's account
# (a `cswap run N` profile's, there). codexbar-usage-status.sh ignores samples
# stamped for an account other than the default login.
CLAUDE_GLOBAL_CONFIG="${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json"

incoming="${1:-}"
session_id="${2:-}"
api_ms="${3:-}"
[[ -n "$incoming" ]] || exit 0
command -v jq >/dev/null 2>&1 || exit 0
mkdir -p "$CACHE_DIR" 2>/dev/null || exit 0

prev='{}'
if [[ -f "$SAMPLE" ]]; then
  prev="$(jq -c . "$SAMPLE" 2>/dev/null)" || prev='{}'
  [[ -n "$prev" ]] || prev='{}'
fi

# The per-session API-call map, read with the `read` builtin (no fork: this
# runs on every repaint of every session) and parsed inside the merge's jq, so
# a damaged file reads as empty rather than failing the merge.
sessions=''
if [[ -n "$session_id" && -f "$SESSIONS" ]]; then
  IFS= read -r -d '' sessions <"$SESSIONS" || true
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

# Two lines out: the merged sample, then the new session map (null: unchanged).
out="$(jq -nc --argjson prev "$prev" --argjson in "$incoming" --argjson now "$(date +%s)" \
  --arg acct "$account" --arg sid "$session_id" --arg ms "$api_ms" --arg smap_raw "$sessions" '
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

  # The per-session evidence map ("ONLY NEWS FROM THE API COUNTS" above).
  # $called: this session is known, and since its last report either its API
  # time moved (any direction: a restarted session starts over) or one of its
  # own readings appeared or changed (a 429 or the quota probe moves
  # rate_limits without moving the API time). A window that merely vanished
  # does not count. An entry from before `r` existed is judged on ms alone.
  (($smap_raw | fromjson? // {}) | if type == "object" then . else {} end) as $smap
  | ($ms | tonumber? // null) as $msn
  | ($sid != "" and $msn != null) as $has
  | (if $has then $smap[$sid] else null end) as $last
  | (reduce ("five_hour", "seven_day") as $w ({};
       ($in[$w]) as $x
       | if ($x | type) == "object" and ($x.used_percentage | type) == "number"
            and ($x.resets_at | type) == "number"
         then . + {($w): [$x.used_percentage, $x.resets_at]} else . end)) as $rd
  | (($last | type) == "object") as $known
  | ($known and ($last.r | type) == "object"
     and any($rd | to_entries[]; $last.r[.key] != .value)) as $moved
  | ($has and $known and ($last.ms != $msn or $moved)) as $called
  # Rewritten on evidence, on any change of the stored readings, and once a
  # minute while the session keeps reporting (`at` = last REPORT, which is
  # what eviction goes by); 64 most recent kept.
  | (if $has and (($known | not) or $last.ms != $msn or $last.r != $rd
                  or ($now - (($last.at | numbers) // 0)) >= 60) then
       [ ($smap + {($sid): {ms: $msn, r: $rd, at: $now}}) | to_entries[]
         | select((.value | type) == "object") ]
       | sort_by(-((.value.at | numbers) // 0)) | .[:64] | from_entries
     else null end) as $newmap

  | ((($prev.account // "") | strings) as $pa
  # Another account was logged in at the last write: drop its readings and
  # fence their windows. A sample with no stamp at all is adopted as it is.
  # That is what every sample looked like before stamping, and fencing it
  # would drop the live numbers of this very account until its windows end.
  # The retired readings are NOT dropped on a switch: each carries its account
  # (`a`; one written before stamping, or with no usable `a`, belongs to the
  # sample it sat in) and stays until its window closes, like the fence, so
  # after A->B->A the idle sessions of A still cannot write the number
  # retired for A back. One that belongs to an UNSTAMPED sample (a = "") is
  # claimed by the account now logged in, exactly as the unstamped sample
  # itself is adopted below; left at "", it would match no account ever and
  # the retired number would come straight back.
  | [ ($prev.retired // [])[]? | objects
      | .a = (if (.a | type) == "string" then .a else $pa end)
      | if .a == "" and $pa == "" and $acct != "" then .a = $acct else . end
    ] as $pretired
  | (if $acct != "" and $pa != "" and $pa != $acct then
       { five_hour: null, seven_day: null,
         fence: ([ ($prev.fence // [])[]? | objects | select(.a != $acct) ]
                 + [ $prev.five_hour, $prev.seven_day | objects
                     | select((.resets_at | type) == "number")
                     | {a: $pa, r: .resets_at} ]),
         retired: $pretired }
     else
       { five_hour: $prev.five_hour, seven_day: $prev.seven_day,
         fence: [ ($prev.fence // [])[]? | objects ],
         retired: $pretired }
     end) as $base
  | [ $base.fence[] | select((.r | type) == "number" and .r > $now) ] as $fence
  | [ $base.retired[] | select((.r | type) == "number" and .r > $now) ] as $retired
  | [ $retired[] | select(.a == $acct) ] as $mine
  | def unfenced($x):
      if $x == null then null
      elif any($fence[]; ((.r - $x.resets_at) | fabs) <= 600) then null
      else $x end;
    # The exact number the endpoint overrode for this window, for THIS
    # account (see "THE ENDPOINT CAN RETIRE A READING" above).
    def unretired($w; $x):
      if $x == null then null
      elif any($mine[]; .w == $w and .u == $x.used
                        and ((.r - $x.resets_at) | fabs) <= 600) then null
      else $x end;
    # Admitted at all? See "ONLY NEWS FROM THE API COUNTS" above: a session
    # that brought evidence ($has) but has heard nothing from the API since
    # its last report is repainting an old reading, and may neither fill nor
    # raise any window. A caller with no evidence to give is admitted as
    # before.
    def admitted($x): if $has and ($called | not) then null else $x end;
  unfenced(open(reading($in.five_hour))) as $r5
  | unfenced(open(reading($in.seven_day))) as $r7
  | admitted(unretired("five_hour"; $r5)) as $i5
  | admitted(unretired("seven_day"; $r7)) as $i7
  | { five_hour: pick(open($base.five_hour); $i5),
      seven_day: pick(open($base.seven_day); $i7) }
  | with_entries(select(.value != null))
  | if $acct != "" then .account = $acct else . end
  | if ($fence | length) > 0 then .fence = $fence else . end
  | if ($retired | length) > 0 then .retired = $retired else . end
  # `seen`. Only a session that just heard from the API ($called) and is at the
  # FRONTIER refreshes it: every reading it brought is in the same window as
  # the merged one and at least as high. An idle session repainting an older,
  # lower number is not news about now, and neither is one repainting a
  # retired number ($r kept, $i dropped).
  # The previous `seen` carries over unless the account changed under it.
  | . as $m
  | def frontier($r; $i; $x):
      $r == null
      or ($i != null and $x != null
          and (($i.resets_at - $x.resets_at) | fabs) <= 600 and $i.used >= $x.used);
    ($called and ($i5 != null or $i7 != null)
     and frontier($r5; $i5; $m.five_hour) and frontier($r7; $i7; $m.seven_day)) as $fresh
  | (if $acct != "" and $pa != "" and $pa != $acct then null
     elif ($prev.seen | type) == "number" then $prev.seen
     else null end) as $pseen
  | ($m == ($prev | del(.seen))) as $same
  | if $fresh then
      # Unchanged and confirmed less than a minute ago: nothing to write.
      if $same and $pseen != null and $pseen <= $now and ($now - $pseen) < 60 then $prev
      else $m + {seen: $now} end
    elif $same then $prev
    elif $pseen != null then $m + {seen: $pseen}
    else $m end) as $merged
  | $merged, $newmap
' 2>/dev/null)" || exit 0
merged="${out%%$'\n'*}"
newmap="${out#*$'\n'}"
[[ "$merged" == '{'* ]] || exit 0

# The map first: it moves on every report with evidence (and once a minute
# per painting session), the sample far less often. No lock (see "Costs,
# stated plainly" above): a concurrent write can lose this update, which
# costs that session at most one report believed or ignored wrongly.
if [[ "$newmap" == '{'* ]]; then
  mtmp="$(mktemp "${SESSIONS}.tmp.XXXXXX" 2>/dev/null)" \
    && { { printf '%s\n' "$newmap" >"$mtmp" && mv -f "$mtmp" "$SESSIONS"; } 2>/dev/null \
         || rm -f "$mtmp" 2>/dev/null; }
fi

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
