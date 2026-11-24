## Agent refresh

Claude Code updates itself on disk, but a running tab keeps the version it
started on. A timer restarts idle, outdated tabs **in place, in the
background**: the same tab, letter, pin and conversation come back on the
installed version. Success is silent (the log only); a failure is a toast with
the command that brings the tab back by hand.

| piece | source (chezmoi) |
|---|---|
| script | `dot_config/tmux/scripts/executable_agent-refresh.sh` (bash 3.2, as launchd runs it) |
| timer | LaunchAgent `com.mackhaymond.tmux-agent-refresh`, `agent-refresh.sh check` every 600 s (no RunAtLoad); installed by `.chezmoiscripts/run_onchange_after_zzz-agent-refresh-launchagent.sh.tmpl` |
| refusal guards | `stash.sh check @W` |
| resume command | `closed-tabs.sh` (`agent_snapshot`, `agent_session`, `build_cmd`) |
| tests | `dot_config/tmux/tests/test_agent_refresh.py` |

### Which tabs

All of these must hold:

1. The default tmux server, by exact socket path.
2. A one-pane, non-home window of a visible space, kind claude. No abby tabs:
   `agent-bg-pending`'s pty check does not see `~/.claude-abby`.
3. `@agent_state` is exactly `idle`. Never `done`, `needs-input` or `failed`:
   those are unseen, and a restart would lose them. Not paused, rate-limited,
   pending, in a workflow or cua, and on neither side of a delegation. The
   whole tick is skipped while an agent-pause set is active.
4. Idle for 2 h (`@agent_since`). Past the 1 h prompt-cache TTL, so a resume
   costs nothing extra.
5. The running version (the process's own `~/.claude/sessions/<pid>.json`) is
   lower than the version `~/.local/bin/claude` links to, which must be a
   plain X.Y.Z, executable and over 10 min old.
6. Not on screen on any client.
7. Nothing lives only in the process: no `ScheduleWakeup` still ahead, no cron
   from the last 8 days that was not deleted, no running `Monitor`, and nothing
   out by `agent-bg-pending`'s detectors since the process start. They are
   loaded read-only and called directly, because the program answers "nothing
   out" when it fails. Missing, failing or unreadable counts as armed.
8. `stash.sh check` passes: busy, caffeinate, composer draft, subagents, and so
   on.
9. The agent is its shell's foreground job, its transcript is where `--resume`
   looks, and the plugin's session id for that pid matches its sessions file.
10. Its model is known (see *The model*).
11. The default model is not opusplan, and `resumeReturnDismissed` is true in
    `~/.claude.json` (else the whole tick is skipped: a resume could stop at
    Claude Code's own dialog).
12. The local MCP servers answer (else the whole tick is skipped: a resume
    would come up without their tools).
13. This (window, session, installed version) has not failed before.

At most 3 per tick, one after another, longest idle first. Pinned tabs count.

On battery under 40% a tick begins no restart. It still finishes one it finds
half done, and `once` by hand ignores the rule.

### The model

closed-tabs.sh's command carries the model a session *started* on. The resume
gets the one it is *on*: the last main-thread reply's `requestedModel` in the
transcript, if it dates from this process. With none, the command's own
`--model` stays; with no model at all, the tab is refused. Also refused: a last
turn that ran under a skill (the skill may have set its model), a `/model` after
the last reply, and opusplan.

### The restart

1. A journal record is written (`journal/<window>.json`: pane, session, cwd,
   command, versions, the window options).
2. The guards run once more, down to the composer. Then, with nothing in
   between, SIGTERM, never -9.
3. Once the process is gone and the pane's shell is at its prompt (up to 30 s),
   one tmux command list clears the line with C-c, waits until the shell's
   line editor reads the fresh prompt, and enters ` clear; cd <cwd> &&
   <resume>`. Never while the tab is on screen: then a toast says so once,
   with the command, and the next ticks do it.
4. Within 90 s a new process of that conversation (its session id, or its argv
   naming it) must register in that window. Any other Claude Code there is a
   failure. The saved options
   (`@agent_since`, the summary and detail, the harness, pin and review mark)
   go back, if the tab is still idle with no turn since. `@agent_since` keeps
   its old epoch, so age and the review popup's 8 h clock do not reset. A tab
   that moved on is left alone.
5. The journal goes away, and `resurrect-save-soon.sh` saves the tab as an
   agent again.

A tick that died in the middle is finished by the next one: a tab that is back
gets its options; one still at its prompt, and off screen, gets the resume
typed once more; otherwise a toast. A resume that cannot be typed (on screen,
not at a prompt, MCP down) 30 min after the kill is given up with that reason;
it is always tried first, since a closed lid ages it too. If the session files
cannot be read, nothing is typed. A failure's toast waits until a client was
active within 10 min, is posted once, and shows for an hour. Failures go to `failed` and are not retried until a newer
version is installed.

Nothing waits forever: each tmux, python, jq, stash.sh, closed-tabs.sh and
notify call is bounded, a watchdog ends a tick after 8 min, and a lock older
than 15 min is taken over. A tmux that stalls after the kill leaves the journal
for the next tick.

Kept: whatever the resume command keeps (launch flags), and the model. Not
kept, as with ⌘Z and a reboot restore: what was changed inside the session
only, such as a permission mode switched since it started.

### By hand

```sh
~/.config/tmux/scripts/agent-refresh.sh --dry-run            # every agent: the resume line or why not; writes nothing
~/.config/tmux/scripts/agent-refresh.sh once @12             # one window now
~/.config/tmux/scripts/agent-refresh.sh once @12 --force-idle   # without the 2 h rule (only that one)
rm ~/.local/state/agent-refresh/failed                       # forget failures
launchctl bootout gui/$(id -u)/com.mackhaymond.tmux-agent-refresh   # timer off
```

State is in `~/.local/state/agent-refresh/`: `journal/`, `archive/` (given up
on, the newest 50), `failed`, `log` (one line per tick with every skip and its
reason, trimmed to 500 lines past 1000), `launchd.log`. `once` exits 0
restarted, 3 refused, 1 failed.
