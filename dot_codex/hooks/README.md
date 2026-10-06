# chezmoi-guard (codex native hook)

Port of the long-lived opencode plugin
`~/.config/opencode/plugins/chezmoi-guard.ts` to **codex-cli 0.148** native hooks
(`[features] hooks = true` in `~/.codex/config.toml`; the old `codex_hooks` /
`plugin_hooks` flag names are gone). A single dispatcher script, invoked as a fresh subprocess
per hook call, that protects chezmoi-managed dotfiles from out-of-band edits and
nudges the agent to commit/push its chezmoi source changes before stopping.

## Files

| Path | Role |
|------|------|
| `~/.codex/hooks/chezmoi-guard.ts` | The dispatcher. Reads stdin JSON, branches on `hook_event_name`. Run via `bun` (node-compatible — uses only `node:*`). |
| `~/.codex/hooks/README.md` | This file. |
| `~/.codex/hooks.json` | Hook registration (merged; see below). |
| `~/.codex/.tmp/chezmoi-guard/` | State root (see "State files"). |

## What it does (four behaviors)

1. **PreToolUse — HARD BLOCK** (registered with **no matcher**, fires on every
   tool call; the dispatcher filters on `tool_name`/`tool_input` shape):
   - `apply_patch` (first-class **or** `command:["apply_patch", <patch>]`) that
     targets a chezmoi-managed file -> **deny** (EXACT path match).
   - shell-family tools (`shell`, `shell_command`, `unified_exec`,
     `exec_command`, `local_shell`, or any unknown tool carrying a command):
     best-effort block of writes (`>`, `>>`, `tee`, `cp`, `mv`, `sed -i`, `dd
     of=`, …) whose target resolves to a managed file (PREFIX-aware), and of
     destructive/history-rewriting git aimed at the chezmoi **source** repo
     (table below). Read-only `sed`/`awk`/`perl`/`ruby` on a managed file
     (`sed -n 1p ~/.zshrc`, `awk '{print}' ~/.zshrc`, `perl -ne …`) are
     allowed; only the in-place forms (`sed -i`/`-i.bak`/`--in-place`/BSD
     `-i ''`, `perl -pi`/`-i.bak`, `ruby -i`, `awk -i inplace`) or a `>`
     redirect onto the managed path count as writes. The in-place detection
     only looks across OPTION tokens, so a downstream `| grep -i …` never
     turns a read into a write, and a perl/ruby `-Mstrict`/`-Ilib` cluster is
     not an `-i`. Empty operands (the `''` of BSD `sed -i ''`) are ignored
     rather than resolved to the cwd. Same regexes/behaviour as the Claude
     hook and the opencode plugin.
   - Relative paths and bare `git` commands are resolved against
     `tool_input.workdir`, falling back to the payload's top-level `cwd`
     (codex's shell tool documents `workdir` as "defaults to the turn cwd").

   | git in the chezmoi source repo | |
   |---|---|
   | **allowed** | `add`, `commit`, non-force `push`, `status`, `diff`, `log`, `stash list`/`show`, `checkout <branch>`, `checkout -b`, `checkout HEAD~1`, `switch <branch>`, `switch -c`, `clean -n`/`--dry-run` |
   | **blocked** | `reset`, `rebase`, `merge`, `restore`, `stash` (push/pop/drop/clear/apply), `switch -f`/`--force`/`--discard-changes`, any `checkout` that restores files (`--` anywhere, `-f`, `-p`, `.`, `./…`, `~…`, an operand containing `/` or ending in `.ext` — so `checkout HEAD -- .`, `checkout main -- x`, `checkout README.md` are all blocked), `clean` with `-f`/`-d`/`-x`, `commit --amend`, `push --force`/`--force-with-lease`/`-f`/`--mirror`/`+refspec` |

   Reached via `git -C <src>`, `--git-dir`/`--work-tree`, `GIT_DIR=`,
   `cd <src> && git …`, `chezmoi git …`, or a bare `git` with workdir/cwd in
   the source tree. The `checkout` walker is the same one the Claude hook
   uses (`gitCheckoutRestoresFiles`), so a branch name containing `/` or a
   dotted suffix is blocked too (indistinguishable from a path restore) —
   use a plain `switch <branch>` for those.
   - Block wire: `{"hookSpecificOutput":{"hookEventName":"PreToolUse",
     "permissionDecision":"deny","permissionDecisionReason":"<text>"}}`, exit 0.
   - Allow = exit 0 with **empty** stdout.
2. **PostToolUse — bookkeeping** (no matcher): remembers source-repo writes this
   session made into `touchedPaths`, then recomputes the dirty set via
   `git status --porcelain` (which self-heals: committed files are pruned).
   Pure side effect; empty stdout. Never touches the continuation guard.
3. **UserPromptSubmit — reminder injection** (appended as a 2nd group): if the
   session has dirty touched chezmoi paths, injects the "uncommitted chezmoi
   changes" complaint via `hookSpecificOutput.additionalContext`. Per-turn
   analog of the opencode `experimental.chat.system.transform`.
4. **Stop — continuation** (appended as a 2nd group): if dirty, blocks the stop
   with `{"decision":"block","reason":"<continuation prompt>"}` telling the
   agent to apply/commit/push, then re-print its final summary. Loop-guarded
   (see below). When clean, resets the guard and allows the stop.
   The dispatcher also accepts `SubagentStop` (codex 0.148 emits it, same
   `decision:block`/`reason` wire) and routes it to the same session-keyed
   handler, but **no SubagentStop group is registered** in `hooks.json` yet, so
   a subagent's turn currently ends without the continuation nag.

UserPromptSubmit and Stop are also where the managed-set cache is refreshed
(see `managed.json` below) — once per turn, never on the tool-call path.

There is **no** `edit`/`write`/`multiedit` tool in codex (those were opencode
tools and are dropped). There is **no** `session.idle` event and **no** TUI
publish API in codex — `Stop` is the continuation mechanism, and there is no
toast.

## State files (`~/.codex/.tmp/chezmoi-guard/`)

Each hook is a separate subprocess with no shared memory, so all state is on
disk. All writes are atomic (`<file>.<pid>.<rnd>.tmp` then `rename`).

| Path | Contents |
|------|----------|
| `managed.json` | Global cache of `chezmoi managed --include=files --path-style absolute`. `{ version, loadedAt, everLoaded, paths[] }`. **Refreshed only from UserPromptSubmit and Stop** (`refreshManagedOffHotPath`), at most once per **300 s** once ever loaded (**15 s** cold-start retry); each refresh logs `managed refreshed {count, ms}` (~20 ms). PreToolUse/PostToolUse only read the cache and never spawn chezmoi except on a true cold start, so a file newly added with `chezmoi add` is guarded from the next turn. `everLoaded` latches true forever; a transient chezmoi failure keeps the stale-but-good paths and the 300 s throttle. |
| `sessions/<key>.json` | Per-session state. `key = sanitize(session_id) + '-' + sha256(session_id)[:16]` (collision-free). `{ version, touchedPaths[], continuationFiredAt, continuationCount, updatedAt }`. |
| `sessions/<key>.lock/` | Per-session mkdir lock (with `holder.json` pid+ts) serializing read-modify-write. Safe stale-break (age>3 s AND pid dead). Lock-acquire failure falls back to a union-merge (add-only; prune only verified-clean entries). |
| `managed.refresh.lock/` | Short-lived lock de-duplicating the cold-start chezmoi spawn. |
| `chezmoi-guard.log` | Append-only best-effort debug log: denies, continuation decisions, `managed refreshed`, missing-binary warnings, errors. The per-tool-call `pretool`/`posttool`/`userpromptsubmit` bookkeeping lines are written only when the hook runs with `CHEZMOI_GUARD_DEBUG=1`. Rotated once it exceeds 1 MB: renamed to `chezmoi-guard.log.1` (overwriting the previous `.1`) at the start of the next invocation. |

GC is opportunistic and best-effort: `*.tmp` orphans older than ~5 min and
session files older than 7 days are unlinked, and the log is rotated as above.

### Loop / re-fire guards (Stop)

- **Primary:** stdin `stop_hook_active === true` -> immediately allow the stop.
- **Backstop (PostToolUse-independent):** a monotonic `continuationFiredAt`
  timestamp + `continuationCount`. Stop refuses to block again if
  `(now - continuationFiredAt) < 120 000 ms` **or** `continuationCount >= 3`.
- **Fail-safe:** unreadable/corrupt session state in Stop -> allow the stop
  (never block on unrecoverable state).

### Binaries / environment

- `chezmoi`: `/opt/homebrew/bin/chezmoi` (verified), fallback to a PATH lookup of
  `chezmoi` — **never** the user's shell-function wrapper (absent in a
  non-interactive subprocess).
- `git`: `/usr/bin/git`.
- Subprocess env is pinned to `PATH=/opt/homebrew/bin:/usr/bin:/bin` and the
  caller's `HOME`. All internal commands run with a 3 s timeout.

## hooks.json registration (already merged)

`~/.codex/hooks.json` is shared with other tooling (cua-notch, tmux agent-tab
indicator, Bartender, codex-session-track), so **locate the guard's groups by
their command string, not by index** — the indexes have moved before (the
OpenIsland entries that used to sit at index 0 are gone; see
`~/.codex/hooks.json.pre-openisland-delete.bak`). The guard is registered on
four events, each as one group with **no matcher**:

- `PreToolUse` (timeout 10 s) and `PostToolUse` (timeout 10 s);
- `UserPromptSubmit` (timeout 10 s) and `Stop` (timeout 15 s).

Command for all four entries:

```
/Users/mackhaymond/.bun/bin/bun /Users/mackhaymond/.codex/hooks/chezmoi-guard.ts
```

PreToolUse/PostToolUse are intentionally registered with **no matcher** so the
guard sees every current and future tool (including `exec_command`, which a
`apply_patch|shell|unified_exec` matcher would miss) and filters internally.

Not registered: `SubagentStop` (the dispatcher handles it; add a group with the
same command if subagent turns should get the continuation nag too).

If `bun` is ever removed, swap the command to `/path/to/node` (v26 confirmed) —
the script uses only `node:child_process` / `node:fs` / `node:path` / `node:crypto`.

A backup of the pre-merge file is at `~/.codex/hooks.json.pre-chezmoi-guard.bak`.

## REQUIRED manual trust steps

codex hooks are untrusted until **you** approve them interactively. This script
does **not** (and must not) write `trusted_hash` into `~/.codex/config.toml` —
that is your manual step.

1. **Launch `codex` once.** For each guard group (and again whenever
   `hooks.json`'s entry for it changes) you get a `New hook — review required`
   prompt. **Approve / trust all four.** Until trusted, none of the guard
   behaviors run — the guard fails open (a total parity gap, not an error).
2. **Or, non-interactively for one session:** launch with
   `codex --dangerously-bypass-hook-trust`.
3. **Verify** in `~/.codex/config.toml` that each guard group has a
   `[hooks.state."/Users/mackhaymond/.codex/hooks.json:<event>:<group>:<hook>"]`
   table carrying a `trusted_hash = "sha256:..."`. Trust is the only switch —
   there is no `enabled` key. Find the right `<group>` index by matching the
   command string in `hooks.json` (today: `pre_tool_use:0:0`,
   `post_tool_use:0:0`, `user_prompt_submit:0:0`, `stop:0:0`).

## Quick sanity check (optional)

```sh
printf '%s' '{"hook_event_name":"PreToolUse","session_id":"x","tool_name":"shell","tool_input":{"command":"git -C ~/.local/share/chezmoi reset --hard"}}' \
  | /Users/mackhaymond/.bun/bin/bun /Users/mackhaymond/.codex/hooks/chezmoi-guard.ts
# expect: {"hookSpecificOutput":{...,"permissionDecision":"deny",...}}
```

A clean command (e.g. `git -C ~/.local/share/chezmoi commit -m x` or
`sed -n 1p ~/.zshrc`) produces empty stdout (allow). Use a throwaway
`session_id` and delete `sessions/<id>-*.json` afterwards if you probe
PostToolUse/Stop.

## Evidence attribution and drift (2026-08-30)

Same mechanism as the Claude Code hook — see `dot_claude/hooks/README.md`,
"Evidence attribution". PostToolUse additionally attributes, by mtime inside
the window while a write-capable shell call RAN (`startedAt − 2s .. now`,
`startedAt` stamped by PreToolUse under `sessions/<key>.calls/<tool_use_id>`;
without a stamp the last 10 min, never past the previous PostToolUse), every
changed source-tree path, every path in commits that moved HEAD inside the
window, and every managed live target → `liveTouched`. apply_patch never
time-attributes (it writes exactly its patch paths), nor does a command the
Claude-side pty read-only classifier accepts (`shell`'s
`["bash","-lc",script]` argv is unwrapped to `script` first). Stop and
UserPromptSubmit run `chezmoi status --recursive=false` over the session's
targets and complain about live≠source drift alongside uncommitted/unpushed
work. Session-state fields: `lastSeenAt`, `headSha`, `liveTouched`,
`exactPaths`, `lingerUntil`.

A chezmoi-relevant command that outlives its call — an exec that yielded
("Process running with session ID …"), `cmd &` / nohup / disown / setsid, a
spawn_pty, or chezmoi-relevant `write_stdin` input — sets `lingerUntil`
(1 h): meanwhile every hook event attributes continuously since the previous
one. Yields are gated on relevance because codex yields long commands
routinely.

Since 2026-10-06 certain by-name writes (apply_patch paths, parsed shell
write targets) are also published to the shared claims ledger
`~/.local/state/chezmoi-guard/claims/` (owner `codex:<sid>`), and a path this
session holds only by time evidence is suppressed from the report while
another session's claim on it is fresh and the file has not been written
since — see "Cross-session claims" in the Claude README.
