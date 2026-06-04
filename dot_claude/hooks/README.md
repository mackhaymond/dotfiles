# chezmoi-guard (Claude Code native hook)

A single `bun` dispatcher (`chezmoi-guard.ts`) wired into four Claude Code hook
events. It is the Claude Code port of the opencode in-process plugin
(`~/.config/opencode/plugins/chezmoi-guard.ts`) and the codex native-hook port
(`~/.codex/hooks/chezmoi-guard.ts`). It keeps chezmoi the source of truth for
tracked dotfiles by hard-blocking direct edits to managed files and nudging the
agent to commit + push its chezmoi source changes before it stops.

The script is invoked fresh per hook call as
`/Users/mackhaymond/.bun/bin/bun /Users/mackhaymond/.claude/hooks/chezmoi-guard.ts`,
reads the hook JSON from stdin, and branches on `hook_event_name`.

## Behaviors

| Event | Matcher | Behavior |
|---|---|---|
| **PreToolUse** | `Edit\|Write\|MultiEdit\|NotebookEdit\|Bash` | HARD-BLOCK (1) edit-class tools targeting a chezmoi-**managed** path (exact match), (2) Bash commands that write to a managed live file (prefix-aware), and (3) destructive / history-rewriting git (`reset`/`rebase`/`merge`/force-push) against the chezmoi source repo. Emits `permissionDecision:"deny"`. |
| **PostToolUse** | `Edit\|Write\|MultiEdit\|NotebookEdit\|Bash` | Pure bookkeeping. Remembers chezmoi-**source** writes this session made and recomputes the dirty set via `git status` (self-healing). Empty stdout. |
| **UserPromptSubmit** | *(none)* | If session-touched chezmoi source paths are still uncommitted, injects an "uncommitted chezmoi changes" complaint as `additionalContext`. Per-turn analog of opencode's `system.transform`. |
| **Stop** | *(none)* | If session-touched chezmoi paths are still dirty, blocks the stop with a continuation prompt (`{"decision":"block","reason":...}`) telling the agent to commit + push. Loop-guarded. |

### Block detail

- **Edit-class** (`Edit`/`Write`/`MultiEdit` -> `file_path`; `NotebookEdit` ->
  `notebook_path`, with a forward-compat fallback to `file_path`): **exact**
  match against the managed-files set. Never prefix — prefix would over-block
  directories.
- **Bash write targets**: **prefix-aware** match (`managed.has(p)` or any managed
  path under `p/`), gated by write-intent heuristics (`>`, `tee`, `cp`, `mv`,
  `sed -i`, `rm`, `dd of=`, etc.). Relative paths resolve against the hook's
  top-level `cwd` (CC's `Bash` has no per-call workdir).
- **Git hazard**: blocks `reset`/`rebase`/`merge`/force-push aimed at the chezmoi
  source repo via `git -C <src>`, `--git-dir=`/`--work-tree=`, `GIT_DIR=` env,
  `chezmoi git -- ...`, the workdir, or `cd <src> && git ...`. Normal `commit`
  and non-force `push` are intentionally allowed.

### Fail-open

Any parse/read/handler error -> exit 0 with empty stdout (tool allowed). The two
hard blocks depend only on `(tool_input, managed.json)` and never on session
state, so a corrupt/locked session file cannot weaken a block. A broken
bun/chezmoi binary disables the guard silently (by design — never wedge the
agent).

## State files

All runtime state lives under **`/Users/mackhaymond/.claude/.chezmoi-guard/`**
(created lazily; fully disposable; separate from CC's own `~/.claude` state and
from the codex `~/.codex/.tmp/chezmoi-guard` dir — they never share state).

| Path | Purpose |
|---|---|
| `chezmoi-guard.log` | Append-only best-effort debug log (never throws). |
| `managed.json` | Managed-set cache `{ version, loadedAt, everLoaded, paths[] }`. TTL 300s steady / 15s cold. `everLoaded` latches true after the first success; a transient failure preserves stale paths and only advances the clock. |
| `managed.refresh.lock/` | `mkdir`-based de-dupe lock around the `chezmoi managed` spawn (avoids a cold-start herd). |
| `sessions/<key>.json` | Per-session state `{ version, touchedPaths[], continuationFiredAt, continuationCount, updatedAt }`. `key = sanitize(session_id) + "-" + sha256(session_id)[:16]`. |
| `sessions/<key>.lock/` | Per-session `mkdir` lock + `holder.json {pid,ts}`. Safe stale-break when age > 3s AND the holder pid is dead. |
| `*.tmp` | Orphaned atomic-write temp files (GC'd after 5 min). |

**Subagents share the parent `session_id`**, so parent + N subagents all read and
write the SAME `sessions/<key>.json`. This is intended: the parent Stop sees
dirty paths created by subagents. Continuation enforcement happens ONLY at
top-level **Stop** — `SubagentStop` is deliberately **not** registered (a
subagent cannot meaningfully commit/push mid-parent-task).

**GC**: every invocation runs an opportunistic, best-effort GC that removes
`*.tmp` orphans older than 5 min and `sessions/*.json` older than 7 days. Live
sessions are never touched. Deleting `STATE_DIR` forces a cold managed-set
re-fetch (<=15s) and drops continuation history — nothing else.

## Binaries (hardcoded)

- `bun`: `/Users/mackhaymond/.bun/bin/bun` (all four hook commands).
- `chezmoi`: `/opt/homebrew/bin/chezmoi`, with a PATH fallback to bare `chezmoi`.
  The interactive shell `chezmoi` function wrapper is absent in the
  non-interactive hook subprocess, so the real binary is resolved directly.
- `git`: `/usr/bin/git`.

If `bun` relocates, all four hooks silently fail-open and must be updated
together in `~/.claude/settings.json`.

## Configuration

The four hook groups live in `~/.claude/settings.json` under the `hooks` key
(appended after the existing OpenIsland / NotchBar / tmux-resurrect entries,
which are preserved). A pre-merge backup is at
`~/.claude/settings.json.pre-chezmoi-guard.bak`.

## Activation (manual — do NOT bypass)

Editing `settings.json` does **not** make these hooks fire immediately. Newly
added hook commands are held for review as an anti-tampering safeguard. To
activate, do **one** of:

1. **Restart the Claude Code session** (quit and relaunch), or
2. Run **`/hooks`** in Claude Code and review/approve the new chezmoi-guard
   entries.

Do not run anything that auto-trusts, auto-restarts, or programmatically
dismisses the hooks review — that approval is owned by you.

After activating, confirm with:

```
tail -f /Users/mackhaymond/.claude/.chezmoi-guard/chezmoi-guard.log
```

You should see `pretool`/`posttool` lines as you use tools.
