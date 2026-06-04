# chezmoi-guard (codex native hook)

Port of the long-lived opencode plugin
`~/.config/opencode/plugins/chezmoi-guard.ts` to **codex-cli 0.135** native hooks
(`codex_hooks=true`). A single dispatcher script, invoked as a fresh subprocess
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
     destructive/history-rewriting git (`reset`/`rebase`/`merge`/force-push)
     aimed at the chezmoi **source** repo.
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

There is **no** `edit`/`write`/`multiedit` tool in codex (those were opencode
tools and are dropped). There is **no** `session.idle` event and **no** TUI
publish API in codex — `Stop` is the continuation mechanism, and there is no
toast.

## State files (`~/.codex/.tmp/chezmoi-guard/`)

Each hook is a separate subprocess with no shared memory, so all state is on
disk. All writes are atomic (`<file>.<pid>.<rnd>.tmp` then `rename`).

| Path | Contents |
|------|----------|
| `managed.json` | Global cache of `chezmoi managed --include=files --path-style absolute`. `{ version, loadedAt, everLoaded, paths[] }`. TTL: **300 s** once ever loaded, **15 s** cold-start retry. `everLoaded` latches true forever; a transient chezmoi failure keeps the stale-but-good paths and the 300 s throttle. PreToolUse uses stale paths and only spawns chezmoi on a true cold start (never blocks the hot path). |
| `sessions/<key>.json` | Per-session state. `key = sanitize(session_id) + '-' + sha256(session_id)[:16]` (collision-free). `{ version, touchedPaths[], continuationFiredAt, continuationCount, updatedAt }`. |
| `sessions/<key>.lock/` | Per-session mkdir lock (with `holder.json` pid+ts) serializing read-modify-write. Safe stale-break (age>3 s AND pid dead). Lock-acquire failure falls back to a union-merge (add-only; prune only verified-clean entries). |
| `managed.refresh.lock/` | Short-lived lock de-duplicating the cold-start chezmoi spawn. |
| `chezmoi-guard.log` | Append-only best-effort debug log: events, observed `tool_name`, decisions, missing-binary warnings, errors. |

GC is opportunistic and best-effort: `*.tmp` orphans older than ~5 min and
session files older than 7 days are unlinked.

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

The existing **OpenIsland** entries are preserved byte-identical at index 0
(`SessionStart` matcher `startup|resume`, `Stop`, `UserPromptSubmit`). The merge
**appended** only:

- new top-level `PreToolUse` and `PostToolUse` arrays (each one group, **no
  matcher**, timeout 10 s);
- a **second** group on `UserPromptSubmit` (timeout 10 s) and on `Stop`
  (timeout 15 s).

Command for all four new entries:

```
/Users/mackhaymond/.bun/bin/bun /Users/mackhaymond/.codex/hooks/chezmoi-guard.ts
```

PreToolUse/PostToolUse are intentionally registered with **no matcher** so the
guard sees every current and future tool (including `exec_command`, which a
`apply_patch|shell|unified_exec` matcher would miss) and filters internally.

If `bun` is ever removed, swap the command to `/path/to/node` (v26 confirmed) —
the script uses only `node:child_process` / `node:fs` / `node:path` / `node:crypto`.

A backup of the pre-merge file is at `~/.codex/hooks.json.pre-chezmoi-guard.bak`.

## REQUIRED manual trust/enable steps

codex hooks are untrusted+disabled until **you** approve them interactively.
This script does **not** (and must not) write `trusted_hash`/`enabled` into
`~/.codex/config.toml` — that is your manual step.

1. **Launch `codex` once.** You will see **four** `New hook — review required`
   prompts:
   - `PreToolUse` (`pre_tool_use:0:0`)
   - `PostToolUse` (`post_tool_use:0:0`)
   - the appended `UserPromptSubmit` group (`user_prompt_submit:1:0`)
   - the appended `Stop` group (`stop:1:0`)
   **Approve / trust all four.** Until trusted **and** enabled, none of the
   guard behaviors run — the guard fails open (a total parity gap, not an
   error).
2. **Or, non-interactively for one session:** launch with
   `codex --dangerously-bypass-hook-trust`.
3. **Verify** in `~/.codex/config.toml` that the four new
   `[hooks.state."<abs hooks.json path>:<key>"]` entries show `enabled = true`
   and have a `trusted_hash = "sha256:..."`. The four keys are
   `pre_tool_use:0:0`, `post_tool_use:0:0`, `user_prompt_submit:1:0`,
   `stop:1:0`.
4. **NOTE:** the three existing OpenIsland entries currently show
   `enabled = false` (dormant). Appending did not change that. The new
   chezmoi-guard entries must end up `enabled = true` — do not let them inherit
   a disabled state.

## Quick sanity check (optional)

```sh
printf '%s' '{"hook_event_name":"PreToolUse","session_id":"x","tool_name":"shell","tool_input":{"command":"git -C ~/.local/share/chezmoi reset --hard"}}' \
  | /Users/mackhaymond/.bun/bin/bun /Users/mackhaymond/.codex/hooks/chezmoi-guard.ts
# expect: {"hookSpecificOutput":{...,"permissionDecision":"deny",...}}
```

A clean command (e.g. `git -C ~/.local/share/chezmoi commit -m x`) produces
empty stdout (allow).
