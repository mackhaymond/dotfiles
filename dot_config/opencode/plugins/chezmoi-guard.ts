// chezmoi-guard: hard-block tool calls that would edit chezmoi-managed files.
//
// Why: chezmoi is the source of truth for tracked dotfiles. Direct edits to
// the live file get reverted on the next `chezmoi apply` (chezmoi re-add
// silently skips templates), and edits made through opencode's Edit/Write
// tools bypass the source-of-truth entirely. This plugin intercepts those
// tool calls before they execute and throws an error that points the agent
// at `chezmoi edit --apply <path>`.
//
// Scope:
//   - Blocks: edit, write, apply_patch, multiedit
//   - Blocks (best-effort, regex-based): bash commands that redirect/write
//     to managed live files, or destructive/history-rewriting git operations
//     inside the chezmoi source repo. Heuristic — meant to catch typical
//     agent bypasses (`echo X > ~/.zshrc`, `sed -i ~/.gitconfig`,
//     `git -C ~/.local/share/chezmoi reset`), not to be a sandbox.
//   - Does NOT cover: GUI editors, apps writing their own configs
//     (those are handled by Tier C drift detection in starship.toml)
//
// Cache: `chezmoi managed --path-style absolute` is invoked at plugin load
// and refreshed on a 5-minute TTL. Newly-tracked files become blocked
// within 5 minutes; force-refresh by restarting the opencode session.

import type { Plugin } from "@opencode-ai/plugin"
import { execFileSync, execSync } from "node:child_process"
import { appendFileSync, mkdirSync, realpathSync } from "node:fs"
import { relative, resolve } from "node:path"

const TTL_MS = 5 * 60 * 1000
let managed = new Set<string>()
let loaded = false
let lastLoad = 0

// Normalize an arbitrary path string the agent passed (relative, ~-prefixed,
// containing /./ or symlinks) into a canonical absolute path. We compare
// canonical paths on both sides so equivalence-bypasses (e.g. `./.zshrc`,
// `/Users/me/./.zshrc`, or a symlink alias of a managed file) don't slip past.
function normalizePath(p: string): string {
  const home = process.env.HOME ?? ""
  const expanded = p.startsWith("~/") ? home + p.slice(1) : p === "~" ? home : p
  const absolute = resolve(expanded)
  try {
    return realpathSync(absolute)
  } catch {
    return absolute
  }
}

function refresh(): void {
  // TTL gating with two regimes:
  //   - Steady-state (loaded): throttle BOTH success and failure for
  //     TTL_MS. A hung/erroring chezmoi must not pay 3s per blocked tool
  //     call. The time-only gate makes that throttle actually work — an
  //     earlier `loaded && timeElapsed` form let failures retry every
  //     call because `loaded` stays false on error.
  //   - Cold-start (not loaded): use a much shorter retry window (15s)
  //     so a chezmoi that's transiently unavailable at plugin load
  //     doesn't fail-open the entire 5min steady-state TTL. While the
  //     cache is empty, every Edit/Write tool would slip past silently
  //     because `managed.has(p)` is always false on an empty Set.
  const ttl = loaded ? TTL_MS : 15_000
  if (Date.now() - lastLoad < ttl) return
  try {
    const out = execSync("chezmoi managed --include=files --path-style absolute", {
      encoding: "utf-8",
      stdio: ["pipe", "pipe", "ignore"],
      timeout: 3000,
    })
    managed = new Set(
      out
        .trim()
        .split("\n")
        .filter(Boolean)
        .map(normalizePath),
    )
    loaded = true
  } catch {
    // Stale cache is better than no cache (steady-state). Cold-start
    // failure leaves the cache empty for one short-TTL window — see
    // the cold-start branch above for the rationale and trade-off.
  }
  lastLoad = Date.now()
}

const BLOCKED_TOOLS = new Set(["edit", "write", "apply_patch", "multiedit"])

const CHEZMOI_SOURCE_DIR = normalizePath("~/.local/share/chezmoi")
const LOG_FILE = normalizePath("~/.local/share/opencode/chezmoi-guard.log")

function debugLog(message: string, data?: Record<string, unknown>): void {
  try {
    mkdirSync(resolve(LOG_FILE, ".."), { recursive: true })
    appendFileSync(
      LOG_FILE,
      `[${new Date().toISOString()}] ${message}${data ? ` ${JSON.stringify(data)}` : ""}\n`,
    )
  } catch {
    // Logging must never interfere with guard behavior.
  }
}

type SessionChezmoiState = {
  // Canonical absolute paths under CHEZMOI_SOURCE_DIR that THIS opencode
  // session wrote to through an observed tool call. This is intentionally
  // path-scoped instead of repo-wide so simultaneous agents with unrelated
  // dotfile work do not complain about each other's uncommitted changes.
  touchedPaths: Set<string>
  continuationPending: boolean
}

const sessionState = new Map<string, SessionChezmoiState>()

function stateForSession(sessionID: string): SessionChezmoiState {
  let state = sessionState.get(sessionID)
  if (!state) {
    state = { touchedPaths: new Set(), continuationPending: false }
    sessionState.set(sessionID, state)
  }
  return state
}

function isInChezmoiSource(p: string): boolean {
  const normalized = normalizePath(p)
  return normalized === CHEZMOI_SOURCE_DIR || normalized.startsWith(CHEZMOI_SOURCE_DIR + "/")
}

function sourceRelativePath(p: string): string {
  return relative(CHEZMOI_SOURCE_DIR, normalizePath(p))
}

function rememberSourceWrites(sessionID: string, rawPaths: string[]): void {
  const state = stateForSession(sessionID)
  for (const raw of rawPaths) {
    const p = normalizePath(raw)
    if (isInChezmoiSource(p)) {
      state.touchedPaths.add(p)
      debugLog("remembered source write", { sessionID, path: sourceRelativePath(p) })
    }
  }
}

function dirtyTouchedPaths(sessionID: string): string[] {
  const state = sessionState.get(sessionID)
  if (!state || state.touchedPaths.size === 0) return []
  const rels = [...state.touchedPaths]
    .map(sourceRelativePath)
    .filter((p) => p && !p.startsWith(".."))
    .sort()
  if (rels.length === 0) return []

  try {
    const out = execFileSync("git", ["-C", CHEZMOI_SOURCE_DIR, "status", "--porcelain", "--", ...rels], {
      encoding: "utf-8",
      stdio: ["pipe", "pipe", "ignore"],
      timeout: 3000,
    })
    const dirty = new Set<string>()
    for (const line of out.split("\n")) {
      if (!line.trim()) continue
      // Porcelain v1 is `XY path` or `XY old -> new`. For renames, track the
      // destination path because that is what remains uncommitted.
      const raw = line.slice(3).replace(/^.* -> /, "")
      if (raw) dirty.add(raw)
    }
    for (const p of rels) {
      if (!dirty.has(p)) state.touchedPaths.delete(resolve(CHEZMOI_SOURCE_DIR, p))
    }
    return rels.filter((p) => dirty.has(p))
  } catch {
    // If status fails, fail quiet rather than blame the agent for stale or
    // unverifiable state. The normal hard guards still run independently.
    return []
  }
}

function uncommittedChezmoiComplaint(sessionID: string): string | undefined {
  const dirty = dirtyTouchedPaths(sessionID)
  if (dirty.length === 0) return undefined
  const shown = dirty.slice(0, 12).map((p) => `- ${p}`).join("\n")
  const more = dirty.length > 12 ? `\n- ...and ${dirty.length - 12} more` : ""
  return (
    `CHEZMOI-GUARD: You made chezmoi source changes in this session that are still uncommitted.\n` +
    `Before finishing this dotfile task, run chezmoi apply if needed, inspect git status/diff, ` +
    `stage only the intended files, commit, and push.\n` +
    `Ignore unrelated chezmoi dirty paths you did not touch; they are likely from another concurrent agent.\n` +
    `Session-touched dirty paths:\n${shown}${more}`
  )
}

function uncommittedChezmoiContinuationPrompt(sessionID: string): string | undefined {
  const complaint = uncommittedChezmoiComplaint(sessionID)
  if (!complaint) return undefined
  return (
    `${complaint}\n\n` +
    `Continue now and resolve this before stopping: apply chezmoi if needed, inspect status/diff/log, ` +
    `stage only the session-touched intended files, commit with an appropriate message, and push. ` +
    `Do not stage unrelated dirty chezmoi paths from other concurrent agents. ` +
    `After the commit/push succeeds, re-print any final summary or user-facing text you output before this guard fired, ` +
    `updated with the commit result if relevant.`
  )
}

function pathsFromArgs(tool: string, args: any): string[] {
  if (!args) return []
  if (tool === "apply_patch" && typeof args.patchText === "string") {
    const re = /\*\*\* (?:Add|Update|Move to|Delete) File: (.+)/g
    const out: string[] = []
    let m: RegExpExecArray | null
    while ((m = re.exec(args.patchText)) !== null) out.push(m[1].trim())
    return out
  }
  if (typeof args.filePath === "string") return [args.filePath]
  if (typeof args.path === "string") return [args.path]
  return []
}

function bashCommandFromArgs(args: any): string {
  if (!args) return ""
  if (typeof args.command === "string") return args.command
  if (typeof args.cmd === "string") return args.cmd
  if (typeof args.script === "string") return args.script
  return ""
}

// Read the working directory the bash tool will run the command in. opencode's
// `bash` tool exposes `workdir`. Other shells/wrappers might use `cwd` or
// `workingDirectory` — we accept all to stay forward-compatible. Returns
// undefined if no workdir is set (command will run in opencode's default cwd).
function bashWorkdirFromArgs(args: any): string | undefined {
  if (!args) return undefined
  for (const key of ["workdir", "cwd", "workingDirectory", "directory"]) {
    if (typeof args[key] === "string" && args[key]) return args[key]
  }
  return undefined
}

// Path-token extractor. Matches three families:
//   1. `~`- or `/`-rooted paths (`~/.zshrc`, `/Users/me/.zshrc`)
//   2. `$HOME`/`${HOME}` env-var paths (`$HOME/.zshrc`, `"${HOME}"/.zshrc`)
//   3. Quote-stripped variants of (1)/(2)
// Pre-substitutes the env-var forms before extraction so downstream
// normalization sees an absolute path.
//
// LIMITATION (acknowledged): bare relative paths after `cd <dir>` are NOT
// extracted. `cd ~ && echo X > .zshrc` slips through the WRITE+PATH check
// because `.zshrc` has no `~`/`/`/`$` prefix. We could track the most-
// recent `cd` argument and prepend it to subsequent unrooted tokens, but
// that's a meaningful escalation in regex complexity for a niche bypass.
// The agent would have to deliberately use this shape — cost-of-effort
// roughly equal to writing a Python one-liner (also out of scope per the
// header's "best-effort, not a sandbox" framing).
function expandHomeVars(cmd: string): string {
  const home = process.env.HOME ?? ""
  if (!home) return cmd
  return cmd
    .replace(/"\$\{?HOME\}?"/g, home)
    .replace(/'\$\{?HOME\}?'/g, "$HOME") // single-quoted is literal — leave alone
    .replace(/\$\{HOME\}/g, home)
    .replace(/\$HOME(?=[/\s'")\]}|;&]|$)/g, home)
}

const PATH_TOKEN_RE = /(?:^|[\s|;&()<>=])(['"]?)([~/][^\s|;&()<>'"`]*)\1/g

// Write-class shell idioms. Any of these in a bash command together with a
// path-token that resolves to a managed file = block. Patterns intentionally
// over-match (false positives are visible to the agent and easily routed
// around; false negatives silently let through bypasses).
//
// Boundary set `[\s|;&({` `]` — covers subshell `( cp ... )`, brace-group
// `{ sed -i ... ; }`, AND legacy backtick command substitution `` `cp ...` ``.
// Without backtick, `` `cp /tmp/x ~/.zshrc` `` would slip past the boundary
// check (modern `$(...)` is already covered via the `(` boundary).
const WRITE_PATTERNS: RegExp[] = [
  // Redirection family: > >> &> 2> N> N>>. Plus zsh clobber-overrides
  // `>|` `>>|` `>!` `>>!` and `&>` variants — `[\|!]?` catches the
  // optional pipe-or-bang clobber suffix. Without it, an agent could
  // bypass with `echo X >! ~/.zshrc` (zsh) which writes the same as
  // `echo X > ~/.zshrc`. No leading boundary anchor — the operator can
  // appear anywhere in the command string.
  /(?:[0-9]?&?>>?[\|!]?|&>[\|!]?)\s*['"]?[~/$]/,
  /(?:^|[\s|;&({`])tee\b/,
  /(?:^|[\s|;&({`])cp\b/,
  /(?:^|[\s|;&({`])mv\b/,
  /(?:^|[\s|;&({`])ln\b/,
  /(?:^|[\s|;&({`])install\b/,
  /(?:^|[\s|;&({`])rsync\b/,
  /(?:^|[\s|;&({`])sed\s+(?:[^\s]+\s+)*?-[a-zA-Z]*[iI]/,
  /(?:^|[\s|;&({`])(?:perl|ruby)\s+(?:-[a-zA-Z]*\s+)*-i/,
  /(?:^|[\s|;&({`])awk\s+(?:[^\s]+\s+)*-i\s+inplace/,
  /(?:^|[\s|;&({`])truncate\b/,
  /(?:^|[\s|;&({`])dd\s+[^|;&]*\bof=/,
]

function bashHasWriteIntent(cmd: string): boolean {
  for (const re of WRITE_PATTERNS) if (re.test(cmd)) return true
  return false
}

function pathsFromBashCommand(cmd: string): string[] {
  const expanded = expandHomeVars(cmd)
  const out: string[] = []
  let m: RegExpExecArray | null
  PATH_TOKEN_RE.lastIndex = 0
  while ((m = PATH_TOKEN_RE.exec(expanded)) !== null) out.push(m[2])
  return out
}

// Split a bash command into roughly-independent segments on shell statement
// terminators (`;`, `&&`, `||`, newline). Each segment is then checked
// independently for write-intent + managed-path. Without this, a command
// like `cmd1 > /tmp/x ; cat ~/.zshrc` would over-match: the `>` write
// intent + the `~/.zshrc` path token combine across segments to produce a
// false-positive block.
//
// Naive: doesn't respect quoting or heredocs perfectly. Good enough for
// the over-match reduction without regressing the actual coverage —
// pathological cases (heredoc with semicolons inside, etc.) still fall
// back to the conservative whole-cmd over-match because they end up as
// one big segment.
function splitBashSegments(cmd: string): string[] {
  return cmd
    .split(/(?:;|&&|\|\||\n)/g)
    .map((s) => s.trim())
    .filter(Boolean)
}

// Detects destructive/history-rewriting git operations targeting the chezmoi
// source repo. Normal `commit` and non-force `push` are intentionally allowed:
// agents are expected to commit and push their completed dotfile changes.
//
// Blocked operations include `reset`, `rebase`, `merge`, and force pushes.
// The repo target may be expressed with raw `git -C <chezmoi-src>`,
// `git --git-dir=<chezmoi-src>/.git`, `chezmoi git -- ...`, cwd-changing
// shell (`cd <chezmoi-src> && git reset`), or the bash tool's `workdir` arg.
const GIT_HAZARD_RE = /(?:^|[\s|;&(])git\s+[^|;&]*?(?:(?:reset|rebase|merge)\b|push\b[^|;&]*(?:\s(?:--force(?:-with-lease)?|-\w*f\w*)\b))/

function gitHasHazard(cmd: string): boolean {
  return GIT_HAZARD_RE.test(cmd)
}

function bashHazardsChezmoiRepo(cmd: string, workdir?: string): boolean {
  const expanded = expandHomeVars(cmd)
  if (/(?:^|[\s|;&(])chezmoi\s+git\b[^|;&]*(?:(?:reset|rebase|merge)\b|push\b[^|;&]*(?:\s(?:--force(?:-with-lease)?|-\w*f\w*)\b))/.test(expanded)) {
    return true
  }
  // Pattern A1: explicit `git -C <chezmoi-src>` + write-class git verb.
  // `-C\s*` (NOT `\s+`) accepts both `-C /path` AND the glued form `-C/path`
  // — git accepts both per its short-flag conventions, and the glued form
  // would otherwise sneak past a strict `-C\s+` matcher.
  const gitDashCRe = /(?:^|[\s|;&(])git\s+(?:-c\s+\S+\s+|--git-dir(?:=|\s+)\S+\s+|--work-tree(?:=|\s+)\S+\s+)*-C\s*(['"]?)([^\s'"|;&]+)\1/g
  let m: RegExpExecArray | null
  while ((m = gitDashCRe.exec(expanded)) !== null) {
    const dir = normalizePath(m[2])
    if (
      (dir === CHEZMOI_SOURCE_DIR || dir.startsWith(CHEZMOI_SOURCE_DIR + "/")) &&
      gitHasHazard(expanded.slice(m.index))
    ) {
      return true
    }
  }
  // Pattern A2: `git --git-dir=<chezmoi-src>/.git` (or --work-tree=) + verb.
  // The `-C` form was the only one matched before; agents can use --git-dir=
  // or --work-tree= to point at the chezmoi repo without a `-C` flag at all.
  // Both `=` and space separators are accepted (git supports both).
  const gitDirRe = /(?:^|[\s|;&(])git\s+(?:[^|;&]*?\s+)?(?:--git-dir|--work-tree)(?:=|\s+)(['"]?)([^\s'"|;&]+)\1/g
  while ((m = gitDirRe.exec(expanded)) !== null) {
    const dir = normalizePath(m[2].replace(/\/\.git$/, ""))
    if (
      (dir === CHEZMOI_SOURCE_DIR || dir.startsWith(CHEZMOI_SOURCE_DIR + "/")) &&
      gitHasHazard(expanded.slice(m.index))
    ) {
      return true
    }
  }
  // Pattern A2.5: GIT_DIR / GIT_WORK_TREE env vars in the command preamble.
  // `GIT_DIR=<chezmoi>/.git git commit` and `export GIT_WORK_TREE=<chezmoi>;
  // git commit` are both ways to redirect git at the chezmoi repo without
  // any `-C`/`--git-dir`/`cd` syntax. Match the env-assignment, normalize
  // the path (stripping a trailing /.git), and require a blocked git hazard
  // anywhere in the rest of the command.
  const gitEnvRe = /(?:^|[\s|;&(])(?:export\s+)?(?:GIT_DIR|GIT_WORK_TREE)=(['"]?)([^\s'"|;&]+)\1/g
  while ((m = gitEnvRe.exec(expanded)) !== null) {
    const dir = normalizePath(m[2].replace(/\/\.git$/, ""))
    if (
      (dir === CHEZMOI_SOURCE_DIR || dir.startsWith(CHEZMOI_SOURCE_DIR + "/")) &&
      gitHasHazard(expanded.slice(m.index))
    ) {
      return true
    }
  }
  // Pattern A3: bash-tool `workdir` parameter pointed at chezmoi src + a
  // destructive/history-rewriting git operation in the command. Without this,
  // `bash(workdir=<chezmoi>, command="git reset")` slips past every other
  // detector — there's no syntactic chezmoi reference in the command string
  // itself, so A1/A2/B can't match. The workdir is supplied by the bash tool
  // wrapper (above the regex layer), not by the user/agent's command shell.
  if (workdir) {
    const dir = normalizePath(workdir)
    if (
      (dir === CHEZMOI_SOURCE_DIR || dir.startsWith(CHEZMOI_SOURCE_DIR + "/")) &&
      gitHasHazard(expanded)
    ) {
      return true
    }
  }
  // Pattern B: implicit cwd via cd/pushd into chezmoi src + later git verb.
  // Boundary set must include `(` and `{` so subshell wrappers like
  // `(cd ~/.local/share/chezmoi && git reset)` are caught — those are the
  // most natural way an agent would isolate the cd from the surrounding
  // shell state and would otherwise bypass a `[\s|;&]`-only boundary.
  const cdRe = /(?:^|[\s|;&({])(?:cd|pushd)\s+(['"]?)([^\s'"|;&]+)\1/g
  while ((m = cdRe.exec(expanded)) !== null) {
    const dir = normalizePath(m[2])
    if (dir === CHEZMOI_SOURCE_DIR || dir.startsWith(CHEZMOI_SOURCE_DIR + "/")) {
      const restOfCmd = expanded.slice(m.index + m[0].length)
      if (gitHasHazard(restOfCmd)) {
        return true
      }
    }
  }
  return false
}

function managedPathError(p: string): Error {
  return new Error(
    `[chezmoi-guard] ${p} is chezmoi-managed.\n` +
      `Edit the source instead:\n` +
      `  chezmoi edit --apply ${p}\n` +
      `or open the source file directly:\n` +
      `  $(chezmoi source-path ${p})\n` +
      `\n` +
      `When ALL your edits are complete (end of the entire task):\n` +
      `  1. Ensure changes are applied (run \`chezmoi apply\` if you\n` +
      `     edited source files without --apply).\n` +
      `  2. Inspect status/diff, stage only intended files, commit, and\n` +
      `     push automatically.\n` +
      `\n` +
      `Do not use reset/rebase/merge or force-push unless the user\n` +
      `explicitly asks for that specific operation.`,
  )
}

export const ChezmoiGuard: Plugin = async ({ client }) => {
  refresh()
  debugLog("initialized", { source: CHEZMOI_SOURCE_DIR })
  return {
    event: async ({ event }) => {
      if (event.type !== "session.idle") return
      const sessionID = event.properties.sessionID
      const state = stateForSession(sessionID)
      if (state.continuationPending) {
        debugLog("idle skipped: continuation already pending", { sessionID })
        return
      }
      const prompt = uncommittedChezmoiContinuationPrompt(sessionID)
      if (!prompt) {
        debugLog("idle clean", { sessionID })
        return
      }
      state.continuationPending = true
      debugLog("idle dirty: prompting continuation", { sessionID })
      try {
        await client.tui.publish({
          body: {
            id: `chezmoi-guard-${Date.now()}`,
            type: "tui.toast.show",
            properties: {
              title: "Continuing to commit chezmoi edits",
              message: "chezmoi-guard found session-touched dirty dotfiles and is submitting a follow-up prompt before stopping.",
              variant: "warning",
              duration: 10_000,
            },
          },
        })
      } catch (err) {
        debugLog("toast failed", { sessionID, error: String(err) })
      }
      try {
        await client.tui.publish({
          body: {
            id: `chezmoi-guard-select-${Date.now()}`,
            type: "tui.session.select",
            properties: { sessionID },
          },
        })
        await client.tui.publish({
          body: {
            id: `chezmoi-guard-append-${Date.now()}`,
            type: "tui.prompt.append",
            properties: { text: prompt },
          },
        })
        await client.tui.publish({
          body: {
            id: `chezmoi-guard-submit-${Date.now()}`,
            type: "tui.command.execute",
            properties: { command: "prompt.submit" },
          },
        })
        debugLog("submitted continuation through tui", { sessionID })
      } catch (err) {
        state.continuationPending = false
        debugLog("tui continuation submit failed", { sessionID, error: String(err) })
      }
    },
    "experimental.chat.system.transform": async (input, output) => {
      if (!input.sessionID) return
      const complaint = uncommittedChezmoiComplaint(input.sessionID)
      if (complaint) {
        debugLog("system reminder injected", { sessionID: input.sessionID })
        output.system.push(complaint)
      }
    },
    "tool.execute.before": async (input, output) => {
      // Edit-class tools (edit/write/apply_patch/multiedit): block writes
      // to canonicalized managed paths.
      if (BLOCKED_TOOLS.has(input.tool)) {
        refresh()
        const paths = pathsFromArgs(input.tool, output.args)
        for (const raw of paths) {
          const p = normalizePath(raw)
          if (managed.has(p)) throw managedPathError(p)
        }
        return
      }
      // Bash: best-effort detection of writes to managed files and of
      // destructive/history-rewriting git operations in the chezmoi repo.
      if (input.tool === "bash") {
        const cmd = bashCommandFromArgs(output.args)
        if (!cmd) return
        const workdir = bashWorkdirFromArgs(output.args)
        // Hazard detection runs against the WHOLE command — `cd <src> && git
        // reset` legitimately spans segments, and the blocklist is narrow
        // enough that whole-command match is appropriate here. The optional
        // `workdir` parameter is consulted for Pattern A3 (bash-tool
        // workdir-set git operations with no syntactic chezmoi reference).
        if (bashHazardsChezmoiRepo(cmd, workdir)) {
          throw new Error(
            `[chezmoi-guard] bash command appears to run a destructive or\n` +
              `history-rewriting git operation in the chezmoi source repo.\n` +
              `\n` +
              `Normal commit and non-force push are allowed. Do not reset,\n` +
              `rebase, merge, or force-push unless the user explicitly asks\n` +
              `for that specific operation.\n` +
              `\n` +
              `Command (truncated): ${cmd.slice(0, 240)}`,
          )
        }
        // Write detection is per-segment so that `cmd > /tmp/x ; cat
        // ~/.zshrc` doesn't false-positive: the `>` intent and the
        // `~/.zshrc` path are in DIFFERENT statements and shouldn't be
        // paired. Each segment is checked independently for the
        // write-intent-AND-managed-path pairing.
        let refreshed = false
        for (const seg of splitBashSegments(cmd)) {
          if (!bashHasWriteIntent(seg)) continue
          if (!refreshed) { refresh(); refreshed = true }
          for (const raw of pathsFromBashCommand(seg)) {
            const p = normalizePath(raw)
            if (managed.has(p)) throw managedPathError(p)
          }
        }
      }
    },
    "tool.execute.after": async (input) => {
      const state = stateForSession(input.sessionID)
      state.continuationPending = false
      if (BLOCKED_TOOLS.has(input.tool)) {
        rememberSourceWrites(input.sessionID, pathsFromArgs(input.tool, input.args))
        return
      }
      if (input.tool !== "bash") return
      const cmd = bashCommandFromArgs(input.args)
      if (!cmd) return
      const workdir = bashWorkdirFromArgs(input.args)
      const paths: string[] = []
      for (const seg of splitBashSegments(cmd)) {
        if (!bashHasWriteIntent(seg)) continue
        paths.push(...pathsFromBashCommand(seg))
      }
      // Common non-interactive source edit shape: bash tool workdir points at
      // the chezmoi source and the command writes explicit rooted paths. We do
      // not mark repo-wide dirtiness for unknown relative writes; doing so
      // would falsely complain about unrelated changes from concurrent agents.
      if (workdir && isInChezmoiSource(workdir)) {
        rememberSourceWrites(input.sessionID, paths.map((p) => (p.startsWith("/") || p.startsWith("~") ? p : resolve(workdir, p))))
      } else {
        rememberSourceWrites(input.sessionID, paths)
      }
      // Recompute after every bash command so successful commits by this or
      // another process clear the session's pending reminder promptly.
      dirtyTouchedPaths(input.sessionID)
    },
  }
}
