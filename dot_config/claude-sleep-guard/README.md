Claude sleep guard
=================

The `com.mackhaymond.claude-sleep-guard` LaunchAgent receives IOKit
`SystemWillSleep` notifications and sends SIGSTOP to local Claude Code CLI
processes before acknowledging sleep. A background wake without the IOKit
graphics capability leaves them suspended. Full wake sends SIGCONT only to
processes this guard suspended, checking PID and process start time first.
Subagents inside those CLI processes pause with their parent. Existing tool
processes can continue during wake windows; this does not cancel shell jobs.

Screen locking and display sleep do not change system graphics capability
and do not trigger a pause. The guard never changes Mac sleep settings,
account selection, cswap thresholds, or quota policies. It never signals the
Claude desktop GUI. It recognizes the installed native CLI binaries under
`~/.local/share/claude/versions/` and Homebrew `Cellar/claude-code/`.

UserPromptSubmit and PostToolBatch hooks also stop a new agent loop if a CLI
starts during DarkWake. Already-running sessions are protected by the native
process suspension; hook configuration may need a session restart to load.

An API request already accepted by the server can finish there while the
client is suspended. This prevents further local requests, not retroactive
cancellation of server work. A real sleep/DarkWake cycle must be inspected
after use; automated tests simulate transitions with an isolated child.

Inspect: `claude-sleep-guard status`
Log: `~/Library/Logs/claude-sleep-guard.log`
Test: `python3 ~/.config/claude-sleep-guard/test_guard.py`

To uninstall: boot out `gui/$(id -u)/com.mackhaymond.claude-sleep-guard`,
resume any owned PIDs listed in its state file only after confirming their
start times, remove the two managed hooks and the LaunchAgent, and remove
the guard's chezmoi files/install script. Unloading alone deliberately does
not thaw paused work during DarkWake.
