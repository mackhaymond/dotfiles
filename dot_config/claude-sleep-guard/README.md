Claude sleep guard
=================

The `com.mackhaymond.claude-sleep-guard` LaunchAgent controls local native
Claude Code and Codex CLI processes and their provably owned descendants.
It uses native CPU capability-loss notifications to suspend work before actual
system sleep is acknowledged. The CPU-target observer must register
successfully: legacy "WillSleep" notifications also occur when graphics turn
off while CPUs remain awake, so they cannot substitute for actual CPU sleep.

The admission rule is shared by the daemon and Claude hooks:

| State | Coding work |
| --- | --- |
| CPU stays awake without actual system sleep | Allowed, including display-off/headless operation |
| System enters actual CPU sleep | Paused before acknowledgment |
| Background wake after actual sleep | Remains paused, including full notification wakes |
| Interactive full wake after actual sleep | Resumes owned work |

Any successful keep-awake mechanism prevents CPU sleep normally; the guard
then has nothing to suspend. It never reads Awake or another app's preferences,
assertions, battery policy or lid-close override. Graphics loss and lid closure
alone cannot arm a hold. Wireless DarkWake, SleepService, Maintenance and
incidental notification wakes retain a hold that actual sleep already armed.
Generic macOS user-wake metadata distinguishes interactive full wakes.

The sleep hold is durable, with a phase that prevents stale pre-sleep user
metadata from thawing work before the machine has slept. `kern.waketime`
changes only after CPUs return from actual sleep; its nonzero epoch lets the
guard distinguish a completed sleep/wake from graphics-only transitions even
after daemon restart. A sleep cancellation applies to the pending sleep
attempt, never to a completed background wake. There is no timed auto-release.

The guard never changes Mac sleep settings, any keep-awake app, account selection,
cswap thresholds, or quota policies. It never signals GUI app processes or
shared tmux servers. CLI identification uses executable paths; identity checks
include native process start seconds/microseconds and executable path before
each signal. Children are rescanned until the owned family is suspended,
including tools that outlive their agent root. Manually stopped processes
are not claimed or resumed.

UserPromptSubmit and PostToolBatch hooks also stop a new Claude loop while the
same system-sleep hold denies work. Existing sessions are protected by native
process suspension; hook changes may need a session restart to load. Polling
reconciles new CLI processes and OS wake metadata every five seconds. This is
not a kernel-level veto of every new process spawn.

An API request already accepted by the server can finish while its client is
suspended. Detached jobs launched through a shared tmux/MCP server have no
provable descendant relationship and remain outside suspension; pausing a
shared server would affect other sessions. Remote/cloud agents are also
outside this local guard. A real sleep/keep-awake cycle must still be inspected
after use; tests exercise real isolated process suspension and native observer
registration without changing the user's power state.

Ownership and sleep state are written durably to two validated journals before STOP. Restart
recovers from either intact copy. If both are corrupt, the guard refuses to
guess ownership; preserve them for manual inspection. On graceful shutdown it
releases owned work only when the current admission rule permits it.

The installer pins each daemon to a verified immutable runtime, waits for
native listener readiness (matching PID and runtime), and restores a prior
runtime if startup fails. The initial legacy migration requires lid-open full
wake and no owned pauses. It pins the legacy source before replacement. A
legacy rollback cannot start while incompatible new ownership remains: it
releases those stops only when policy permits, otherwise preserves the
journals and removes the unsafe auto-load plist for explicit recovery.

Inspect: `claude-sleep-guard status`
Log: `~/Library/Logs/claude-sleep-guard.log`
Test: `/usr/bin/python3 ~/.config/claude-sleep-guard/test_guard.py`
Installer/settings tests (source tree):
`/usr/bin/python3 ~/.local/share/chezmoi/dot_config/claude-sleep-guard/test_install.py`

To uninstall during a normal awake session:
`launchctl bootout gui/$(id -u)/com.mackhaymond.claude-sleep-guard`, then
`claude-sleep-guard release`. Release/recover acquires the daemon lock and
resumes only owned matching identities; it is an explicit override of the
sleep policy, so do not use it to resume work unintentionally while asleep.
Remove the two managed hooks, LaunchAgent, and chezmoi guard/install files.
Unloading alone deliberately does not thaw work while the admission rule
denies it. Fix/reinstall after a blocked legacy rollback before the next sleep.

If the daemon dies during an armed attempt that is subsequently canceled,
and misses both the cancellation and all later CPU transitions, the unchanged
wake epoch cannot prove cancellation. It conservatively preserves the hold
until a new proven wake or explicit recovery; it never guesses from a timeout.
