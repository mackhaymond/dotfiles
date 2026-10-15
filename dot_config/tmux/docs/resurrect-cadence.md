# Resurrect save cadence

How often tmux's layout is saved, what each save costs, and what stops a
fast save from destroying the good snapshot. Changed 2026-10-15, then revised
the same day after review round 1.

## Goal

Before this change, continuum saved every 15 minutes. A crash could lose up to
15 minutes of layout: new agent tabs, closed tabs, parks. The goal was to
bound that loss to about a minute, with no measurable battery cost. The other
requirement was that frequent saves must never let a crashed server's tiny
replacement layout overwrite the good `last` snapshot.

## What runs when

| Trigger | Command | Gated | Fingerprint fast path |
|---|---|---|---|
| continuum, every minute (`@continuum-save-interval '1'`) | `resurrect-save.sh quiet` | yes | yes |
| structural tmux event (hooks, debounced ~5s) | `resurrect-save-soon.sh` → `resurrect-save.sh auto` | yes | yes |
| `client-detached` (debounced) | `resurrect-save-soon.sh full` → `auto-full` if no client is left, else `auto` | yes | only when a client remains |
| prefix C-s | `resurrect-save.sh` | refused during a restore | no: always full |
| kill-guard, right before it kills the server | `resurrect-save.sh final` | no | no: always full |

### Gates (quiet / auto / auto-full)

These saves are skipped silently in three cases:

- **The server is under `@resurrect-startup-grace-seconds` old** (default 120).
  On 2026-10-15 at 09:40, a save ran 2 minutes after the server was killed and
  captured the fresh 1-pane server. The guard rejected it, but only because the
  streak rule still had room.
- **A restore is in progress.** `@resurrect_restoring` holds an epoch. tmux.conf's
  `pre-restore-all` hook sets it. The end of the `post-restore-all` chain sets
  `@resurrect_restored` and then clears the marker, after assistant-restore has
  relaunched the agents. A save in the middle of a restore would record half the
  panes as bare shells and rewrite the assistant sidecar without their
  sessions. A marker older than 15 minutes is ignored, so a restore that died
  cannot stop autosave for good.
- **`@kill_guard_shutdown` holds an epoch under 120 seconds old.** kill-guard
  sets it right before killing the server, and saves fired by teardown hooks
  must never capture the dying server. A guard that dies mid-kill (C-c,
  SIGKILL) therefore cannot leave a flag that silently stops every autosave,
  which is the same failure class as the 38-hour gap. `resurrect-save-soon.sh`
  checks the flag before it debounces, and `resurrect-save.sh` checks it again
  afterwards.

Every epoch gate requires an age of at least 0. A non-numeric value or a
future epoch (for example after a clock jump) counts as unset. The same rule
applies to the fingerprint's own epoch, which counts as stale.

A manual C-s during a restore is refused with a message. `final` is never
gated.

### Fingerprint fast path (quiet / auto)

A full save costs 1.76s wall, 1.38s CPU and 197MB peak RSS. Most of that is the
post-save-all hook chain. Running it every minute would be real battery drain.

So quiet and auto first build a **layout fingerprint**. It comes from the same
single tmux call that reads the options. If both of the following hold, the
save only re-dates `last` (`touch -c`, so the stale chip stays quiet) and exits
with no snapshot, no hooks and no log line:

- the fingerprint equals the one recorded at the last full save
- that save is under `@resurrect-full-every-minutes` old (default 15; `0` means
  always full)

The fingerprint is one line per pane. It covers session, window index and
name, pane index and pid, foreground command, cwd, and pane title.

Excluded sessions are left out, otherwise every tmux-pty-mcp shell an agent
opens would force a full save.

- **The pane title is included because it is the only cheap tab identity.**
  Claude Code names every window after its version (`2.1.300`). The title
  (`✳ BAI info session slide deck`) is what tells tabs apart.
- **The leading status glyph is stripped from the title**, because it spins
  while an agent works.
- **Left out on purpose, because they change all day without changing what a
  restore rebuilds:**
  - active window and active pane
  - `history_size` and cursor
  - `window_layout`: a resized split is restored with its old proportions until
    the next 15-minute full save.

`.save-fingerprint` holds a first line of `<epoch>` or `<epoch> held`, followed
by the fingerprint. The rules for writing and reading it:

- **After a save:** a full save records the fingerprint when `last` points at
  its snapshot.
- **After a held save** (see the verdict below): the fingerprint is recorded
  with `held`. An unchanged layout then fast-paths without re-dating `last`,
  and the stale chip may show that saves are not landing. Nothing is
  re-judged for 15 minutes unless the layout changes.
- **Only excluded sessions** (an empty fingerprint): it is recorded too, so
  that server settles into the fast path instead of retrying and logging every
  minute.
- **When it is read:** the layout is read before the snapshot is taken, so a
  recorded fingerprint is never newer than its snapshot. `final` re-reads the
  layout after waiting for the lock.

### Debounced event saves (`resurrect-save-soon.sh`)

The hooks are all at index `[9]`:

- `session-created`, `session-closed`, `session-renamed`
- `window-linked`, `window-unlinked`
- `after-split-window`, `pane-exited`, `after-kill-pane`
- `after-rename-window`
- `client-detached`, which passes `full`

tmux 3.7c has no `after-join-pane` or `after-move-pane` hook ("invalid option"),
so a join-pane is picked up by the 1-minute fingerprint poll instead. tmux lists
`pane-exited` under `show-hooks -gw`, not `-g`.

The first caller becomes the owner by creating a mkdir lock under `$TMPDIR`
that holds its pid. It sleeps `RESURRECT_SAVE_SOON_DELAY` (5s) and then runs
one save. Later callers drop a `pending` marker (and a `full` marker if they
asked for one) and exit. If a marker arrived after the owner's save began, the
owner runs another round, up to 4 rounds. A caller that finds the lock but no
living owner breaks it.

**`full` is honoured only if `tmux list-clients` is empty after the
debounce.** Popups (floax Option-Q, btop Option-A) attach nested clients, and
each close fires `client-detached`. Quitting WezTerm leaves zero clients;
closing a popup does not.

## The verdict: `resurrect-guard.sh check`, before `last` moves

Before this revision, the save linked `last` first, and the guard reverted it
in the post-save-all hook. During a hold, that left `last` pointing at the
rejected snapshot for about 1.4 seconds of every minute. A C-r or a power loss
in that window would restore the tiny layout.

Now `resurrect-save.sh` asks `resurrect-guard.sh check <file>` for every new
snapshot that differs from `last`:

- **Exit 0: accept.** The save links `last`, captures pane contents, and runs
  the post-save-all hooks. The guard's bookkeeping run then archives the
  sidecars and makes the save the known-good one.
- **Exit 2: hold.** stdout is `<good>\x1f<reason>`. The snapshot stays on disk
  as evidence. Three things are skipped: linking `last`, the pane capture, and
  every post-save-all hook. Those hooks rewrite `assistant-sessions.json` and
  `pane_contents.tar.gz` in place, so running them would corrupt the good
  sidecars. The guard updates the streak once, and the log says
  `saved N panes in Xs (kept last on <good> (<reason>); evidence <file>)`.

`check` writes no state when it accepts. The bookkeeping run (`resurrect-guard.sh`
with no argument) recomputes the same verdict from the same state and is
idempotent, so it never counts a streak twice. `resurrect-save.sh` always runs
it after the hooks. If tmux.conf's chain still ends in the guard, it simply runs
twice. A save identical to the known-good one (the save keeps `last`) clears
any hold: the streak and its clock reset.

### Rules

- **Not a shrink:** the new save keeps at least half the known-good panes, or
  the known-good save has fewer than 4 panes. It is accepted.
- **A shrink on an ordinary server:** held until it has been seen twice in a
  row AND 10 minutes have passed since the first hold. On 2026-10-06, the old
  streak-only rule confirmed 87 → 19 panes in 20 seconds.
- **A replacement server.** A server whose `#{start_time}` is after the
  known-good save's capture time (the stamp in its name) never had that
  layout: it is a crash or kill replacement. If no restore has completed on it
  (`@resurrect_restored` is unset), **every** save that differs from the good
  one is held, not just a shrink. continuum skips its auto-restore whenever
  any other `^tmux` process runs, which is the normal case here. Before this
  rule, a fresh server that grew to half the old pane count took `last` over,
  and C-r could no longer bring the old tabs back.
- **Exits from a replacement-server hold:**
  - **A restore on this server** sets `@resurrect_restored`. After that the
    server follows the ordinary rule.
  - **A manual C-s that is not a shrink** is accepted: it is Mack saving the
    layout in front of him, on purpose. A manual shrink stays held, because
    the 2026-08-22 loss was exactly a C-s on a near-empty post-reboot server.
  - **The trust count: 16 consecutive holds.** Holds come about every 15
    minutes, since the held fingerprint re-judges an unchanged layout that
    often. A gap of more than 20 minutes (sleep, a stopped clock) restarts the
    count, and holds less than 12 minutes apart count once. Uptime must also
    be at least 4 hours, and a shrink also needs the streak and the 10
    minutes. Wall-clock uptime or time held alone proves nothing: a crash at
    18:00, a skipped restore and a closed lid read as 14 hours of both at
    08:00, and a server pty-mcp starts headless burns hours before Mack
    attaches.
- **When a replacement server stops being one:** once any of its saves is
  accepted, because the known-good save was then captured on it.
- **The one-time notice.** The first hold of this kind on a server shows a
  10-second message on every attached client:
  `this server started after the last good save and was never restored;
  autosave keeps that layout. prefix C-r restores it.` The server option
  `@resurrect_replacement_noticed` limits it to once per server.
  - It can't go in the stale chip. Any value other than `refused` renders as
    "last save <value> ago", and agent-tab-watcher.sh rewrites the chip from
    `last`'s age.
- **`final`** is accepted, even when it shrank: a pre-kill layout is real.
  The exceptions are a replacement server that never restored, or a restore in
  progress (`@resurrect_restoring` live). In those cases it is held, `last`
  stays on the known-good save and is re-dated, and the exit status is still 0.
  The kill is safe, because the next restore brings the real layout back.

## `final` (kill-guard's pre-kill save)

`final` always takes a full save: no fast path and no gates. It is quiet, and
it waits up to 20s for the save lock (`RESURRECT_FINAL_LOCK_WAIT`).

| Status | Meaning |
|---|---|
| 0 | `last` points at this save's snapshot (new, or identical and re-dated), or the save was held as above. In every case `last`'s target mtime is at least the save's start time and save.log gains a `saved N panes in Xs (` line. |
| 0, all excluded | A server holding only excluded sessions (for example pty-mcp's `agents` after a crash). Nothing is saved, `last` is re-dated, and save.log gets `saved 0 panes in Xs (nothing to save; kept last)`, so such a server can still be killed through the guard. quiet and auto keep exiting 0 with nothing saved. |
| 1 | No panes listed (tmux unreachable). |
| 2 | Refused: not the default server. |
| 3 | The lock was still held after the wait. |
| 4 | After the hooks, `last` does not point at the snapshot. |
| 5 | The snapshot could not be written (`mv` failed). Nothing was linked. |
| 6 | A post-save-all hook failed (assistant-save or repair), so the sidecar may not hold every agent. |

Exit 6 needs tmux.conf's post-save-all chain to be just
`assistant-save && repair`, with the guard removed from it. The save now runs
the guard itself. If the chain still ends in `; guard`, its status is the
guard's and assistant-save failures are masked.

The other modes keep exiting 0 (or 1 for "no panes"), because continuum
ignores the status.

## Other robustness fixes

- **Session exclusions are shell patterns.** `@resurrect-exclude-sessions`
  entries such as `ptymcp-*` are matched as `case` patterns against whole
  session names. This applies in resurrect-save.sh (fingerprint, snapshot,
  pane capture), resurrect-guard.sh (pane counts) and resurrect-save-repair.py
  (`fnmatch`), and in assistant-save.sh (bash picks the excluded sessions and
  passes their names to its jq filter) and assistant-restore.sh.
- **Locale.** A tmux client without a UTF-8 locale prints `\x1f` and tab as `_`.
  If no UTF-8 locale is in effect, the save exports `LANG=en_US.UTF-8` and its
  hooks inherit it.
- **Same-second saves.** A save waits for a fresh second instead of
  overwriting an existing snapshot, which might be the known-good one.
- **A failed `mv` of the snapshot** is logged. Nothing is linked and no hooks
  run, and `final` exits 5.
- **`run_hook` under `set -u`.** It logged `$1` after a `shift`. Whenever
  post-save-all failed, that killed the whole save with status 1 before the
  fingerprint, the log line and the stale-chip clear. Fixed.
- **Repair.** The repair now handles invalid live rows in two ways:
  - **Excluded sessions** are skipped silently. On 2026-10-15, one `agents`
    pane with an empty cwd made every repair fail.
  - **Other sessions:** the row is named on stderr, and that pane's plugin row
    is kept exactly as saved (`kept_unverified` in the report) instead of
    being dropped or rebuilt.
- **Prune.** The glob is expanded in bash, so it never hits ARG_MAX.

## Costs (measured 2026-10-15, 20-pane test server)

**Fast path:** about 20ms wall, close to 0 CPU, 7.8MB RSS. That breaks down as
bash startup 5ms, one tmux call 7ms, lock 3.4ms and touch 1.8ms.

**Full saves:** on the real server, the fingerprint changed once in 5 minutes
of sampling. Expect a full save per structural change, plus one every 15
minutes.

**A held save** skips the pane capture and all post-save-all hooks, so it
costs only the snapshot itself. It runs at most once per layout change or per
15 minutes while a hold lasts.

**Growth:** snapshot files stay under the 30-day prune (at most ~9k files and
~30MB). save.log is capped at 3000 lines, trimmed to 1500. Sidecar archives
stay at 30.

## Why `tasks` stays excluded

agentd (CuaNotch's task broker) recreates the `tasks` session and resumes its
tasks on demand. If resurrect restored those panes, they would launch a second
time, outside the broker's bookkeeping.

## Failure modes and residual risks

- **Startup restore.** The pre-restore marker hook sits above the tpm line, so
  continuum's background restore sees it. The 120s startup gate covers the same
  window regardless.
- **The restore markers may never clear.** If continuum's restore finishes
  before tmux.conf applies the post-restore-all hook that sits below the tpm
  line, the plugin's own hook runs instead. Then `@resurrect_restored` is never
  set and `@resurrect_restoring` is never cleared. `@resurrect_restoring`
  expires on its own after 15 minutes. A replacement server whose restore
  went unrecorded stays held until the trust count (about 4 hours of holds)
  or a manual non-shrink C-s.
- **Default server, by path.** Every default-server check accepts only
  `/private/tmp/tmux-$UID/default` or `/tmp/tmux-$UID/default`, not any socket
  named `default`. That covers resurrect-save.sh, resurrect-save-soon.sh,
  continuum-ensure.sh and agent-tab-watcher.sh. Agents' private servers live at
  `$TMUX_TMPDIR/tmux-$UID/default`.
- **Changes the fingerprint cannot see** (a `/clear` that keeps its title, or a
  change of active window) wait for the 15-minute full save, a structural
  event, or a quit.
- **Lost marker in save-soon.** The minute poll catches it.
- **A genuine shrink is held for 10 to 15 minutes** on an ordinary server, and
  up to 4 hours on a replacement server that never restored. Pressing C-r
  ends the latter immediately. A crash during a hold restores the larger
  layout.
- **`final` on a replacement server that never restored** keeps `last` on the
  pre-crash layout, even if Mack rebuilt more tabs by hand. In practice, any
  accepted autosave on that server has already made it non-replacement.
