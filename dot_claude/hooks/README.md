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
| **PreToolUse** | `Edit\|Write\|MultiEdit\|NotebookEdit\|Bash\|mcp__pty__run\|mcp__pty__spawn_pty` | HARD-BLOCK (1) edit-class tools targeting a chezmoi-**managed** path (exact match), (2) shell commands that write to a managed live file (prefix-aware), and (3) destructive / history-rewriting git against the chezmoi source repo (see the git-hazard table below). Emits `permissionDecision:"deny"`. |
| **PostToolUse** / **PostToolUseFailure** | `Edit\|Write\|MultiEdit\|NotebookEdit\|Bash\|mcp__pty__run\|mcp__pty__spawn_pty` | Bookkeeping. Remembers chezmoi-**source** writes this session made — by command-text heuristics AND by evidence (see *Evidence attribution* below) — and recomputes the dirty set via `git status` (self-healing). Also runs the managed-set TTL refresh. Empty stdout. |
| **UserPromptSubmit** | *(none)* | If session-touched chezmoi source paths are still uncommitted, injects an "uncommitted chezmoi changes" complaint as `additionalContext`. Per-turn analog of opencode's `system.transform`. Also runs the managed-set TTL refresh. |
| **Stop** | *(none)* | If session-touched chezmoi paths are still uncommitted, unpushed, **or `chezmoi status` reports live≠source for a target this session worked on** (what the zsh prompt's `~` glyph shows, scoped to this session), blocks the stop with a continuation prompt (`{"decision":"block","reason":...}`) telling the agent to apply/re-add, commit + push. Loop-guarded. Also runs the managed-set TTL refresh. |

The shell tools are the built-in `Bash` **and** the MCP pty shell
(`mcp__pty__run`, `mcp__pty__spawn_pty`) — on this machine `Bash` is
deny-listed and every shell command goes through `mcp__pty__run`, so the
matcher must name it or the shell guard never runs. `mcp__pty__send_keys` is
not matched by PreToolUse (its `keys`/`text` are keystrokes, not a command
line); it IS matched by PostToolUse, only to notice keys that start chezmoi
work in a pty (see *Commands that outlive their tool call*).

### Evidence attribution (why an opaque writer can't slip past the Stop guard)

The shell-text heuristics only see writes spelled as redirects / `cp` / `mv` /
`sed -i`. On 2026-08-30 a session edited the source tree with a
`python3 - <<'PY' … write_text()` heredoc after `cd $(chezmoi source-path) &&`
(a command substitution the hook cannot expand), left two commits unpushed and
a source edit unapplied — and Stop logged `stop clean`, because `touchedPaths`
was empty. So PostToolUse now ALSO attributes by what changed on disk while
a write-capable shell call RAN — `[startedAt − 2s, now]`, where `startedAt` is
stamped by that call's PreToolUse (`sessions/<key>.calls/<tool_use_id>`;
without a stamp, the last 10 min, never reaching back past the previous
PostToolUse):

1. source working tree: `git status --porcelain -uall` paths whose mtime (for a
   deletion, the parent dir's mtime) is inside the window AND whose content
   differs from the call's PreToolUse snapshot (see *Content and reachability*
   below);
2. source commits: if HEAD moved since last look, every path in `old..new`
   committed inside the window;
3. managed **live** targets whose mtime is inside the window → `liveTouched`.

Edit-class tools never time-attribute (they write exactly their `file_path`,
remembered by name), and neither does a shell command the pty read-only
classifier (`pty-read-only.ts` → `isReadOnlyCommand`) accepts (a leading
`VAR=value` always counts as write-capable: it can point git or a pager at any
program). `PostToolUseFailure` runs the same bookkeeping for shell calls — a
failed command may still have written — but a failed Edit wrote nothing and
claims nothing.

**Commands that outlive their tool call** (`outlivesCall`): a pty `run` that
TIMED OUT or an interrupted shell call (always), and a `spawn_pty`, `cmd &` /
`nohup` / `disown` / `setsid`, `run_in_background`, or keys typed with
`mcp__pty__send_keys` (only when chezmoi-relevant: names chezmoi or its source
tree, opens an editor, or names a managed path — a dev server must not
re-widen anything). These set `lingerUntil = now + 1 h`, during which EVERY
hook event (PostToolUse of any matched tool, UserPromptSubmit, Stop)
attributes continuously since the previous one — the old wide window, only
while something we started may still be writing. `send_keys` is in the
PostToolUse matcher for this alone. Commits are checked by `--since` the
window start, falling back to the newest 50 commits when no HEAD was
remembered, so a session's first write-capable call still sees its commit.

Stop / UserPromptSubmit then run `chezmoi status --recursive=false` over
`liveTouched ∪ target-path(touched sources)` (filtered through a fresh
`chezmoi managed`, since one unmanaged arg aborts `status`) and report any
drifted target; a touched source whose target is drifted is never pruned as
"done" even once committed and pushed. Session-state fields: `lastSeenAt`,
`headSha`, `liveTouched`, `exactPaths`, `lingerUntil`.

**Cross-session claims (2026-10-06).** Until then the window ran from the END
of the previous tool call (or session start), so it spanned model think-time,
the user's idle time and read-only commands; every session got blamed for
whatever other agents wrote meanwhile (four sessions held the same four tmux
files at once). Besides the narrowed window, every path a session certainly
WROTE by name (`exactPaths`: a successful Edit/Write, or a shell segment's
parsed write target — never the all-path-tokens fallback, so
`cat <src>/x > /tmp/y` claims nothing) is published to a ledger shared with the
codex and opencode guards,
`~/.local/state/chezmoi-guard/claims/<sha256(path)[:32]>.json`
`{path, owner: "claude:<sid>"|"codex:<sid>"|"opencode:<root>", at}` (last
writer wins). A claim vouches only for the write it records: before
reporting, a path this session holds only by time evidence is SUPPRESSED (not
deleted — it reports again if the claim lapses) when another session claims
it AND the file's mtime is not later than the claim (`foreignClaimed`). A
write after the claim is not the claimant's, so it stays ours. Claims are
released when their owner's path is committed + pushed (pruned); the 24 h TTL
is only a backstop. A session's own exact claims are never suppressed, nor is
a deleted file. What remains misattributable: a write-capable command of ours
running at the same moment another agent writes a path it never names (e.g.
via its own opaque heredoc). Claude Code itself offers no
per-command file list (as of 2.1.291: PostToolUse carries only stdout,
checkpoints exclude Bash writes, `FileChanged` has no writer attribution).

**Content and reachability (2026-10-15).** A session that only edited two
files was nagged about ~8 others: (1) its `git commit` ran pre-commit, which
stashes and restores every unstaged file in the tree — other agents' dirty
files, same bytes, fresh mtime, inside the commit's window; (2) long
non-chezmoi shell calls the read-only classifier won't vouch for (a 4-minute
tmux polling loop) collected whatever anyone else wrote meanwhile. Now:

- The PreToolUse of a write-capable shell call snapshots the source repo's
  dirty set with a content fingerprint per path (sha1; symlink target; `-` for
  deleted; size+mtime above 5 MB or past a 32 MB per-snapshot budget) into the
  call's stamp file, `{"v":2,"start","cwds","snap":[[rel,fp],…]}`.
  PostToolUse attributes a working-tree path only if it is newly dirty or its
  fingerprint changed (mtime stays as the prefilter). No snapshot (old stamp,
  git failure, >2000 dirty paths) or a lingering window → mtime only.
- A shell call time-attributes only if it names chezmoi (`chezmoiRelevant`)
  or its shell stood, at PreToolUse or PostToolUse, somewhere that reaches
  chezmoi files: inside the source tree, containing it or a managed target
  (`$HOME` and above), or inside a directory that directly holds a managed
  target other than `$HOME` (`~/.config/tmux/**`, `~/.claude/**`). `cd` /
  `pushd` hops and `..` paths in the command count too. The cwd is Bash's
  hook `cwd`, a pty's tmux `pane_current_path` (window in session `agents`
  tagged `@pty_claude_sid` = session_id and `@pty_name`), or spawn_pty's
  `cwd` (default `$HOME`). Unknown anywhere → reachable. Live targets (3)
  keep the mtime test behind this gate.

Tests: `bun test ~/.local/share/chezmoi/dot_claude/hooks/` runs the guard as
a subprocess against a temp repo/state. Its overrides (state/claims/source
dir, chezmoi binary, classifier, tmux stub) are accepted ONLY from a
`--test-env=<abs json path>` argument with absolute-path values — never from
env vars, because bun auto-loads `.env` from the session's project dir.

### Block detail

- **Edit-class** (`Edit`/`Write`/`MultiEdit` -> `file_path`; `NotebookEdit` ->
  `notebook_path`, with a forward-compat fallback to `file_path`): **exact**
  match against the managed-files set. Never prefix — prefix would over-block
  directories.
- **Shell write targets**: **prefix-aware** match (`managed.has(p)` or any
  managed path under `p/`), gated by write-intent heuristics (`>`, `tee`, `cp`,
  `mv`, `rm`, `dd of=`, etc.). `sed`/`perl`/`ruby`/`awk` count as writes ONLY
  with an in-place flag (`sed -i`/`--in-place`, `perl -i`/`-pi`, `ruby -i`,
  `awk -i inplace`) or a `>` redirect at the managed path — bare `sed -n`,
  `awk '{print}' file`, `perl -ne`, `perl -Mstrict`, `ruby -Ilib` are reads
  and pass, and a later `| grep -i` cannot be mistaken for the editor's flag.
  BSD `sed -i ''`'s empty backup-suffix operand is skipped (it is not a
  target; it used to normalize to the cwd and deny everything under `$HOME`).
  A relative target resolves against where the shell stands at that segment
  (`walkSegmentCwds`). It starts at the hook's top-level `cwd` plus where the
  shell really is (Bash: that same `cwd`; `mcp__pty__run`: tmux's
  `pane_current_path` of this session's pty window, since the persistent pty
  keeps every earlier `cd`; `spawn_pty`: its `cwd` param or `$HOME`), and
  every followable `cd`/`pushd` in the command moves it. Followable means:
  - a literal path, relative ones included, with `-P`/`-L`/`-e`/`-@`/`--`
    skipped (bare `cd` = `$HOME`);
  - a literal path that doesn't exist yet, entered when the cd is followed by
    `&&` (`git worktree add ../wt && cd ../wt`). After `;` the cd may fail,
    so the old cwd counts too;
  - `cd "$(git rev-parse --show-toplevel)"`, resolved to the nearest
    ancestor holding `.git` (a filesystem walk, no git fork).

  After `cd ~/code/proj &&` only `~/code/proj` counts. A cd that may not run
  (after `&&` or `||`, or inside if/case/while/until/for/select) moves only
  the rest of its own `&&` chain. Once that chain ends, the old and new cwd
  both count: `[ -d x ] && cd /tmp && git stash` judges `/tmp`, but
  `false && cd /tmp; git stash` judges both. Once a hop can't be
  followed (`cd -`, `cd "$d"`, `cd $X`, `z`, popd, a bare name that isn't an
  existing directory — `cd` is zoxide here) every directory seen so far
  counts, capped at 64 (past that a relative hop is unknown, an absolute one
  still counts). A cd inside `( … )` ends at the `)`, and one behind a single
  `&` or in a pipeline moves only that child shell. Shell code in quotes
  (`bash -c "cd ~/.config/tmux && echo x > tmux.conf"`) is walked the same
  way. So `cd ~/.config/tmux && echo x > tmux.conf`, and `rm tmux.conf` in a
  pty that cd'd there in an earlier call, are both denied. `tee` checks every
  operand, and a redirect glued to the word before it (`echo hi>~/.zshrc`)
  is a target even beside a parsed harmless one.
- **Git hazard**: aimed at the chezmoi source repo explicitly (`git -C
  <src>`, `--git-dir=`/`--work-tree=`, `GIT_DIR=` env, `chezmoi git -- ...`),
  or by where the git command runs: each segment is judged by its own cwd
  from the same walk (the session `cwd` and the shell's real cwd, moved by
  the command's cds). So `cd ~/code/proj && git reset --hard` is fine even
  from a pty standing in the source, and `cd .local/share/chezmoi && git
  stash` from `$HOME` is not. A segment whose cwd is unknown — a pty whose cwd
  tmux can't report, or after a hop the walk can't follow (`z chezmoi`,
  `cd -`, `cd $DOTS`, `eval "cd …"`) — is treated as possibly the source: a
  hazard verb there is denied unless the git call names an absolute repo
  (`git -C /abs ...`). Starting the command with a literal `cd /abs/repo &&`
  avoids it, and the deny message says which case applied and to use a
  literal absolute path. Git is recognized as `git`, `\git` or
  `/usr/bin/git`, also right after a backtick, with any global options
  before the verb (`-P`, `--no-optional-locks`, `-c k=v`, `--git-dir=…`).

  Data is not a command. `scanShell` is a single linear lexer pass (quotes,
  `$( )`, `<( )`, backticks, arithmetic, heredocs, comments) that blanks
  data before matching:
  - commit messages: `-m '…'`, `--message=…`, and the
    `-m "$(cat <<'EOF' … EOF)"` idiom;
  - `echo`/`printf` arguments, unless the pipeline feeds `sh`/`bash`/`eval`/
    `source`/`xargs`;
  - a heredoc fed to `cat`/`tee`/`git` that expands nothing, unless its
    output runs (`bash -c "$(cat <<EOF…)"`, `eval "$(cat …)"`,
    `source <(cat …)`, `cat <<EOF | sh`);
  - comments. These are found before continuations are joined, so a trailing
    `\` in a comment doesn't swallow the next line.

  `<<` is a heredoc only in command context, not inside quotes or
  `$(( ))`/`(( ))` (`1 << n` is a shift). Shell code in quotes (`bash -c
  '…'`, `sh -c "…"`, `eval '…'`, `watch '…'`, echo text piped into a shell, or
  any quoted text that cds) is judged as a command of its own from the
  segment's cwd. A search such as `rg 'git reset' dir` is neither. Each scan
  stops at a newline or a closing backtick, so a later line's `-n`/`-f` is
  not read as this git command's flag; backslash-newline continuations
  outside comments are joined.

  | Blocked | Allowed |
  |---|---|
  | `reset`, `rebase`, `merge` (any form) | `add`, `commit`, non-force `push` |
  | `push --force` / `--force-with-lease` / `+ref` / `--mirror` / `--delete` / `-d` / `:ref` / `--prune` | `status`, `diff`, `log`, `show`, `reflog` (show) |
  | `restore` (any form) | `checkout <branch>`, `checkout -b <name>`, `checkout HEAD~1` |
  | `checkout -- <path>`, `checkout .`, `checkout <path-like>`, `checkout -f`/`-p`/`-B`, `checkout <treeish> -- <path>`, `checkout HEAD .` | `clean -n` / `--dry-run` (on the same line, incl. `-nd` / `-n -d`) |
  | `clean -f` / `-d` / `-x` / `--force` (without a dry-run flag) | `stash list`, `stash show` |
  | `stash` (push/pop/apply/drop/clear/bare — anchored to the subcommand, so `stash push -m 'list'` is still blocked) | `switch <branch>`, `switch -c <name>` |
  | `switch -f` / `--force` / `--discard-changes` / `-C` / `--force-create` | `rm <file>`, `rm --cached <file>` (git itself refuses these when the file has uncommitted changes) |
  | `commit --amend` | `branch` (list, `-a`, `-vv`, `--merged`), `branch <new>`, `branch -c` |
  | `rm -r` / `-f` / `--force` / `--recursive`, or a `.`/`..`/`:`/glob operand (incl. with `--cached`) | `worktree list` |
  | `branch -d`/`-D`/`-f`/`-m`/`-M`/`-C`/`--delete`/`--force`/`--move` | |
  | `update-ref`, `read-tree`, `filter-branch`, `filter-repo`, `gc`, `prune` (any form) | |
  | `reflog expire`/`delete`, `worktree remove`, `checkout-index -f`/`--force` | |

  "Path-like" for `checkout` means `.`, `./x`, `~`, anything containing `/`, or
  anything with a file extension — a branch named `feature/x` is therefore
  blocked too (conservative; the deny message says so). A git command that
  demonstrably runs in another repo is never blocked. One that may run in the
  source is: the session `cwd` is the source (kept as a candidate even when a
  pty stands elsewhere), or its cwd is unknown (see above).

### Fail-open

Any parse/read/handler error -> exit 0 with empty stdout (tool allowed). The two
hard blocks depend only on `(tool_input, managed.json)` and the shell's cwd
(tmux, for a pty; unreachable tmux makes a hazard verb STRICTER, never
looser), and never on session state, so a corrupt/locked session file cannot
weaken a block. A broken
bun/chezmoi binary disables the guard silently (by design — never wedge the
agent).

## State files

All runtime state lives under **`/Users/mackhaymond/.claude/.chezmoi-guard/`**
(created lazily; fully disposable; separate from CC's own `~/.claude` state and
from the codex `~/.codex/.tmp/chezmoi-guard` dir — they never share state).

| Path | Purpose |
|---|---|
| `chezmoi-guard.log` | Append-only best-effort debug log (never throws). Rotated once to `chezmoi-guard.log.1` (overwritten) when it exceeds 1 MB. Only deny / continuation / refresh / error lines are written by default; set `CHEZMOI_GUARD_DEBUG=1` in the hook's environment to also get the per-tool-call `pretool`/`posttool`/`userpromptsubmit` lines. |
| `chezmoi-guard.log.1` | Previous log generation. |
| `managed.json` | Managed-set cache `{ version, loadedAt, everLoaded, paths[] }`. TTL 300s steady / 15s cold. `everLoaded` latches true after the first success; a transient failure preserves stale paths and only advances the clock. **Who refreshes it**: PostToolUse, UserPromptSubmit and Stop each check the TTL and re-run `chezmoi managed` when it has lapsed (logged as `managed set refreshed`). PreToolUse only ever reads the cache (it spawns solely on a genuine cold start), so the block decision never waits on a refresh; a file added to chezmoi is guarded within ~5 min of the next tool call. |
| `managed.refresh.lock/` | `mkdir`-based de-dupe lock around the `chezmoi managed` spawn (avoids a cold-start herd). |
| `sessions/<key>.json` | Per-session state `{ version, touchedPaths[], exactPaths[], liveTouched[], lastSeenAt, headSha, continuationFiredAt, continuationCount, updatedAt }`. `key = sanitize(session_id) + "-" + sha256(session_id)[:16]`. |
| `sessions/<key>.calls/<tool_use_id>` | Start stamp (ms) of an in-flight shell call, written by PreToolUse, consumed by PostToolUse. Orphans (denied calls) GC'd after 24 h. |
| `~/.local/state/chezmoi-guard/claims/*.json` | Exact-claims ledger shared with the codex/opencode guards (see *Cross-session claims*). Expired entries GC'd after 24 h. |
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

The four hook groups live in `~/.claude/settings.json` under the `hooks` key.
**Do not edit the live file** — it is generated by the chezmoi `modify_`
script `dot_claude/modify_settings.json.tmpl` (chezmoi replaces the whole
`hooks` key on apply; Claude's own runtime keys like `model`/`effortLevel`
are passed through). Edit the JSON block in that script and `chezmoi apply`.
See OPERATIONS.md, "`modify_` files".

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

You should see a `managed set refreshed` line within a few tool calls (and
`deny ...` lines when a block fires). Per-tool-call `pretool`/`posttool` lines
only appear with `CHEZMOI_GUARD_DEBUG=1`.

Changing a hook's **matcher** in `settings.json` (e.g. adding `mcp__pty__run`)
is subject to the same hold-for-review: approve via `/hooks` or restart.

# agent-gate.sh (unattended-usage gate)

Plain `sh`, wired as `PreToolUse` (no matcher, `timeout: 86400`) and
`UserPromptSubmit` (`timeout: 5`). Fails open on any unreadable input and never
signals a process (its predecessor, claude-sleep-guard, SIGSTOPped agents and
broke tabs; removed in 8154d44).

- **DarkWake hold** (`pre-tool`): while `pmset -g systemstate` lacks
  `Graphics` (a DarkWake), loop on `sleep 5` so the agent sleeps with the Mac
  instead of re-sending a ~450k-token context on every background wake
  (2026-10-11: 29% of a week in 15.6 awake minutes). Releases within ~5 s of a
  real wake.
- **Weekly reserve**: when this session's account (`.claude.json`
  `oauthAccount`) is mackhaymond@gmail.com and `~/.cache/codexbar-tmux/claude-live.json`
  shows it at >=90% weekly before `resets_at`, return `continue:false`.
  `touch ~/.claude/reserve-override` bypasses until the next reset.
