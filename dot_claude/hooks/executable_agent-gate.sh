#!/bin/sh
# Claude Code gate against unattended usage burn. Usage: agent-gate.sh pre-tool|prompt
#
# 1. DarkWake hold (pre-tool only). With the lid closed macOS DarkWakes every
#    few minutes for ~9 s; agents mid-task re-sent their whole ~450k-token
#    context on each one (2026-10-11: 29% of a week in 15 awake minutes).
#    While the system lacks the Graphics capability (DarkWake), wait here
#    instead: the agent sleeps with the Mac and resumes on a real wake.
#    Awake with the display off (lid closed under Awake) keeps Graphics.
# 2. Weekly reserve. Stop work on the main account at RESERVE_PCT weekly so the
#    rest stays usable from the phone. `touch ~/.claude/reserve-override`
#    bypasses it until the next weekly reset.
#
# Fails OPEN: any unreadable input lets Claude run. Never SIGSTOPs anything.
cat >/dev/null 2>&1

MAIN=mackhaymond@gmail.com
RESERVE_PCT=90
LIVE=$HOME/.cache/codexbar-tmux/claude-live.json
OVERRIDE=$HOME/.claude/reserve-override

dark_wake() {
	caps=$(pmset -g systemstate 2>/dev/null) || return 1
	case $caps in *"System Capabilities"*) ;; *) return 1 ;; esac
	case $caps in *Graphics*) return 1 ;; esac
	return 0
}

if [ "${1:-}" = pre-tool ]; then
	while dark_wake; do sleep 5; done
fi

acct=$(plutil -extract oauthAccount.emailAddress raw -o - "${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json" 2>/dev/null) || exit 0
[ "$acct" = "$MAIN" ] || exit 0
live_acct=$(plutil -extract account raw -o - "$LIVE" 2>/dev/null) || exit 0
[ "${live_acct%%/*}" = "$MAIN" ] || exit 0
used=$(plutil -extract seven_day.used raw -o - "$LIVE" 2>/dev/null) || exit 0
resets=$(plutil -extract seven_day.resets_at raw -o - "$LIVE" 2>/dev/null) || exit 0
used=${used%%.*}
case $used$resets in '' | *[!0-9]*) exit 0 ;; esac
[ "$(date +%s)" -lt "$resets" ] || exit 0
[ "$used" -ge "$RESERVE_PCT" ] || exit 0
if [ -f "$OVERRIDE" ] && [ "$(stat -f %m "$OVERRIDE")" -ge $((resets - 604800)) ]; then
	exit 0
fi
printf '{"continue":false,"stopReason":"Weekly reserve: %s is at %s%% (reserve starts at %s%%). touch ~/.claude/reserve-override to keep going until the reset."}\n' \
	"$MAIN" "$used" "$RESERVE_PCT"
