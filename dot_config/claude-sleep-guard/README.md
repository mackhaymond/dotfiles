Claude sleep guard
=================

The `com.mackhaymond.claude-sleep-guard` LaunchAgent controls local native
Claude Code and Codex CLI processes and their provably owned descendants.
It uses native target-capability notifications to suspend work before an
unwanted transition is acknowledged, plus lid notifications and periodic
reconciliation. A public sleep-notification backend is available if the
target-capability SPI is unavailable; status/readiness identifies the backend.

The admission rule is shared by the daemon and Claude hooks:

| State | Coding work |
| --- | --- |
| Lid open, CPU and graphics available | Allowed, including display sleep/lock |
| Lid closed, Awake OFF | Paused, including full notification wakes |
| Lid closed, Awake ON and closed-lid mode enabled, above its battery floor or on AC, with a live hold and effective override | Allowed even without graphics |
| CPU unavailable or required native state unknown | Paused |

Wireless DarkWake, SleepService, Maintenance and incidental full wakes cannot
override closed-lid OFF intent. Awake intent comes from live native assertions
owned by the Awake executable; a saved "engaged" preference is never proof.
Current settings and battery checks also prevent stale kernel metadata from
allowing work after Awake disables its override at the configured floor.

The guard never changes Mac sleep settings, Awake settings, account selection,
cswap thresholds, or quota policies. It never signals GUI app processes or
shared tmux servers. CLI identification uses executable paths; identity checks
include native process start seconds/microseconds and executable path before
each signal. Children are rescanned until the owned family is suspended,
including tools that outlive their agent root. Manually stopped processes
are not claimed or resumed.

UserPromptSubmit and PostToolBatch hooks also stop a new Claude loop when the
same admission rule denies work. Existing sessions are protected by native
process suspension; hook changes may need a session restart to load. Polling
reconciles new CLI processes and Awake assertion/settings changes every five
seconds. This is not a kernel-level veto of every new process spawn.

An API request already accepted by the server can finish while its client is
suspended. Detached jobs launched through a shared tmux/MCP server have no
provable descendant relationship and remain outside suspension; pausing a
shared server would affect other sessions. Remote/cloud agents are also
outside this local guard. A real lid-close/Awake cycle must still be inspected
after use; tests exercise real isolated process suspension and native observer
registration without changing the user's power state.

Ownership is written durably to two validated journals before STOP. Restart
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
