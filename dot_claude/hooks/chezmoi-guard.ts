// chezmoi-guard (Claude Code native hook port)
//
// Port of the opencode in-process plugin (plugins/chezmoi-guard.ts) to Claude
// Code native hooks. A SINGLE dispatcher invoked as a fresh subprocess per hook
// call, branching on hook_event_name:
//
//   PreToolUse        - HARD-BLOCK edits (Edit/Write/MultiEdit/NotebookEdit) to
//                       chezmoi-managed paths, Bash writes to managed live files,
//                       and destructive/history-rewriting git ops on the chezmoi
//                       source repo. Emits a permissionDecision:deny JSON.
//   PostToolUse       - remember source-repo writes this session made; recompute
//                       the dirty set (git status self-heals). Pure bookkeeping.
//   UserPromptSubmit  - inject the "uncommitted/unpushed chezmoi changes" complaint as
//                       additionalContext (per-turn analog of system.transform).
//   Stop              - if session-touched chezmoi paths are still uncommitted/unpushed, block
//                       the stop with a continuation prompt (loop-guarded).
//
// Because each hook is a separate subprocess with NO shared memory, all state
// from the source plugin (managed-set cache, per-session touchedPaths, the
// continuation guard) is externalized to disk under
// /Users/mackhaymond/.claude/.chezmoi-guard, keyed by session_id.
//
// SUBAGENTS share the parent session_id, so subagent source writes land in the
// SAME sessions/<key>.json as the parent; continuation enforcement happens ONLY
// at top-level Stop (SubagentStop is intentionally NOT registered, by design — a
// subagent cannot meaningfully commit/push mid-parent-task). The parent Stop,
// sharing session state, is the backstop and sees all subagent-made dirty paths.
//
// This guard is best-effort, NAME-BASED interception of the five built-in write
// tools (Edit/Write/MultiEdit/NotebookEdit) + Bash. It is NOT a sandbox: a
// future write-capable MCP server or new CC built-in tool that bypasses these
// tool names would not be intercepted.
//
// Uses only node:child_process / node:fs / node:path, so the command may be
// swapped from bun to node without code changes.
//
// FAIL-OPEN philosophy: any uncaught/internal error -> exit 0 with empty stdout.
// The two HARD blocks are pure functions of (tool_input, managed.json) with
// ZERO dependence on session state, so a corrupt/locked session file can never
// weaken a block.

import { execFileSync } from "node:child_process"
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  realpathSync,
  renameSync,
  rmdirSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs"
import { createHash } from "node:crypto"
import { relative, resolve } from "node:path"

// ---------------------------------------------------------------------------
// Module constants
// ---------------------------------------------------------------------------

const TTL_MS = 5 * 60 * 1000 // 300000 steady-state
const COLD_TTL_MS = 15_000 // cold-start retry window
const MAX_CONTINUATIONS = 3
const CONTINUATION_WINDOW_MS = 2 * 60 * 1000 // 120000

const STATE_DIR = "/Users/mackhaymond/.claude/.chezmoi-guard"
const SESSIONS_DIR = STATE_DIR + "/sessions"
const MANAGED_FILE = STATE_DIR + "/managed.json"
const MANAGED_REFRESH_LOCK = STATE_DIR + "/managed.refresh.lock"
const LOG_FILE = STATE_DIR + "/chezmoi-guard.log"

// Subprocess env. `which chezmoi` is a shell FUNCTION wrapper, absent in a
// non-interactive subprocess; we must call the real binary. Hardcode the
// Homebrew path and fall back to a PATH lookup of `chezmoi` (NEVER the shell
// function). GIT_BIN is the system git.
const GIT_BIN = "/usr/bin/git"
const SUBPROC_ENV = {
  HOME: process.env.HOME ?? "",
  PATH: "/opt/homebrew/bin:/usr/bin:/bin",
}

function isExecutable(p: string): boolean {
  try {
    statSync(p)
    return true
  } catch {
    return false
  }
}

const CHEZMOI_BIN = isExecutable("/opt/homebrew/bin/chezmoi")
  ? "/opt/homebrew/bin/chezmoi"
  : "chezmoi"

// ---------------------------------------------------------------------------
// Path normalization (verbatim from source, HOME-based realpath/expansion)
// ---------------------------------------------------------------------------

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

const CHEZMOI_SOURCE_DIR = normalizePath("~/.local/share/chezmoi")

// ---------------------------------------------------------------------------
// Debug log (best-effort, never throws; identical signature for verbatim reuse)
// ---------------------------------------------------------------------------

function debugLog(message: string, data?: Record<string, unknown>): void {
  try {
    mkdirSync(STATE_DIR, { recursive: true })
    appendFileSync(
      LOG_FILE,
      `[${new Date().toISOString()}] ${message}${data ? ` ${JSON.stringify(data)}` : ""}\n`,
    )
  } catch {
    // Logging must never interfere with guard behavior.
  }
}

// ---------------------------------------------------------------------------
// Atomic write helper (temp + rename on the same APFS device)
// ---------------------------------------------------------------------------

function atomicWrite(target: string, contents: string): void {
  const tmp = `${target}.${process.pid}.${Math.random().toString(36).slice(2)}.tmp`
  writeFileSync(tmp, contents)
  renameSync(tmp, target)
}

// ---------------------------------------------------------------------------
// (1) MANAGED-SET CACHE  ->  managed.json
//   { version:1, loadedAt:<epoch_ms>, everLoaded:<bool>, paths:[<canon abs>...] }
//
//   everLoaded latches TRUE forever after the first success (selects the 300s
//   steady-state TTL). A transient failure after a good load PRESERVES
//   everLoaded + the stale-but-good paths and only advances the throttle clock.
//   Only a genuine cold start (everLoaded never true) sits in the 15s regime.
// ---------------------------------------------------------------------------

type ManagedCache = { loadedAt: number; everLoaded: boolean; paths: string[] }

function readManagedCache(): ManagedCache {
  try {
    const raw = readFileSync(MANAGED_FILE, "utf-8")
    const j = JSON.parse(raw)
    return {
      loadedAt: typeof j.loadedAt === "number" ? j.loadedAt : 0,
      everLoaded: j.everLoaded === true,
      paths: Array.isArray(j.paths) ? j.paths.filter((p: unknown) => typeof p === "string") : [],
    }
  } catch {
    return { loadedAt: 0, everLoaded: false, paths: [] }
  }
}

function writeManagedCache(c: ManagedCache): void {
  try {
    atomicWrite(
      MANAGED_FILE,
      JSON.stringify({ version: 1, loadedAt: c.loadedAt, everLoaded: c.everLoaded, paths: c.paths }),
    )
  } catch (e) {
    debugLog("managed cache write failed", { error: String(e) })
  }
}

// Attempt the actual chezmoi spawn, de-duplicated against the cold-start herd
// by a double-check stat + a short managed.refresh.lock.
function refreshManaged(prev: ManagedCache): ManagedCache {
  // Double-check: another process may have just refreshed.
  const recheck = readManagedCache()
  const ttlNow = recheck.everLoaded ? TTL_MS : COLD_TTL_MS
  if (Date.now() - recheck.loadedAt < ttlNow && (recheck.everLoaded || recheck.paths.length > 0)) {
    return recheck
  }

  // Best-effort de-dupe lock. If held, brief spin then re-read instead of
  // spawning. If we cannot acquire and the file is still stale, spawn anyway
  // (benign: equivalent content, last writer wins).
  let haveLock = false
  try {
    mkdirSync(MANAGED_REFRESH_LOCK)
    haveLock = true
  } catch {
    for (let i = 0; i < 10; i++) {
      const r = readManagedCache()
      const ttl = r.everLoaded ? TTL_MS : COLD_TTL_MS
      if (Date.now() - r.loadedAt < ttl && (r.everLoaded || r.paths.length > 0)) return r
      try {
        const start = Date.now()
        while (Date.now() - start < 20) {
          /* tiny busy spin ~20ms (no foreground sleep available) */
        }
      } catch {
        /* ignore */
      }
    }
  }

  try {
    const out = execFileSync(
      CHEZMOI_BIN,
      ["managed", "--include=files", "--path-style", "absolute"],
      { encoding: "utf-8", stdio: ["pipe", "pipe", "ignore"], timeout: 3000, env: SUBPROC_ENV },
    )
    const paths = out
      .trim()
      .split("\n")
      .filter(Boolean)
      .map(normalizePath)
    const next: ManagedCache = { loadedAt: Date.now(), everLoaded: true, paths }
    writeManagedCache(next)
    return next
  } catch (e) {
    if (CHEZMOI_BIN === "chezmoi") {
      debugLog("chezmoi managed failed (PATH fallback binary)", { error: String(e) })
    } else {
      debugLog("chezmoi managed failed", { error: String(e) })
    }
    // Preserve everLoaded + stale paths; advance the throttle clock only.
    const next: ManagedCache = {
      loadedAt: Date.now(),
      everLoaded: prev.everLoaded,
      paths: prev.paths,
    }
    writeManagedCache(next)
    return next
  } finally {
    if (haveLock) {
      try {
        rmdirSync(MANAGED_REFRESH_LOCK)
      } catch {
        /* ignore */
      }
    }
  }
}

// loadManaged: returns the managed path array.
//   coldSpawnOnly=true (PreToolUse hot path): use cached/stale paths; only spawn
//   chezmoi if everLoaded===false (genuine cold start). Never block the hot path
//   on a steady-state refresh.
//   coldSpawnOnly=false (PostToolUse/UserPromptSubmit/Stop): full TTL refresh.
function loadManaged(opts?: { coldSpawnOnly?: boolean }): string[] {
  const coldSpawnOnly = opts?.coldSpawnOnly === true
  const cache = readManagedCache()

  if (coldSpawnOnly) {
    // Already loaded once: trust stale paths, never spawn on the hot path.
    if (cache.everLoaded || cache.paths.length > 0) return cache.paths
    // Genuine cold start: a single bounded spawn is allowed.
    if (Date.now() - cache.loadedAt < COLD_TTL_MS) return cache.paths
    return refreshManaged(cache).paths
  }

  const ttl = cache.everLoaded ? TTL_MS : COLD_TTL_MS
  if (Date.now() - cache.loadedAt < ttl) return cache.paths
  return refreshManaged(cache).paths
}

// EXACT match for patch/edit-class blocks (mirrors source `managed.has(p)`).
function managedHas(p: string, managed: string[]): boolean {
  return managed.includes(p)
}

// PREFIX-aware for bash write targets only (mirrors source touchesManagedPath:
// `managed.has(p) || some managedPath startsWith p + "/"`).
function touchesManagedPath(p: string, managed: string[]): boolean {
  if (managed.includes(p)) return true
  for (const managedPath of managed) {
    if (managedPath.startsWith(p + "/")) return true
  }
  return false
}

// ---------------------------------------------------------------------------
// chezmoi-source helpers (verbatim from source)
// ---------------------------------------------------------------------------

function isInChezmoiSource(p: string): boolean {
  const normalized = normalizePath(p)
  return normalized === CHEZMOI_SOURCE_DIR || normalized.startsWith(CHEZMOI_SOURCE_DIR + "/")
}

function sourceRelativePath(p: string): string {
  return relative(CHEZMOI_SOURCE_DIR, normalizePath(p))
}

// ---------------------------------------------------------------------------
// (2)/(3) PER-SESSION STATE  ->  sessions/<key>.json
//   key = sanitize(session_id) + '-' + sha256(session_id).slice(0,16)  ALWAYS
//   { version:1, touchedPaths:[...], continuationFiredAt:<ms|0>,
//     continuationCount:<int>, updatedAt:<ms> }
// ---------------------------------------------------------------------------

type SessionState = {
  touchedPaths: string[]
  continuationFiredAt: number
  continuationCount: number
}

function sessionKey(sessionId: string): string {
  const sanitized = String(sessionId).replace(/[^A-Za-z0-9._-]/g, "_")
  const hash = createHash("sha256").update(String(sessionId)).digest("hex").slice(0, 16)
  return `${sanitized}-${hash}`
}

function sessionFile(sessionId: string): string {
  return `${SESSIONS_DIR}/${sessionKey(sessionId)}.json`
}

function sessionLockDir(sessionId: string): string {
  return `${SESSIONS_DIR}/${sessionKey(sessionId)}.lock`
}

function readSessionState(sessionId: string): SessionState {
  try {
    const raw = readFileSync(sessionFile(sessionId), "utf-8")
    const j = JSON.parse(raw)
    return {
      touchedPaths: Array.isArray(j.touchedPaths)
        ? j.touchedPaths.filter((p: unknown) => typeof p === "string")
        : [],
      continuationFiredAt: typeof j.continuationFiredAt === "number" ? j.continuationFiredAt : 0,
      continuationCount: typeof j.continuationCount === "number" ? j.continuationCount : 0,
    }
  } catch {
    return { touchedPaths: [], continuationFiredAt: 0, continuationCount: 0 }
  }
}

function writeSessionState(sessionId: string, state: SessionState): void {
  try {
    mkdirSync(SESSIONS_DIR, { recursive: true })
    atomicWrite(
      sessionFile(sessionId),
      JSON.stringify({
        version: 1,
        touchedPaths: [...new Set(state.touchedPaths)],
        continuationFiredAt: state.continuationFiredAt,
        continuationCount: state.continuationCount,
        updatedAt: Date.now(),
      }),
    )
  } catch (e) {
    debugLog("session state write failed", { sessionId, error: String(e) })
  }
}

// Per-session mkdir lock with safe stale-break + holder.json (pid+ts).
function acquireSessionLock(sessionId: string): boolean {
  const lockDir = sessionLockDir(sessionId)
  const holder = `${lockDir}/holder.json`
  try {
    mkdirSync(SESSIONS_DIR, { recursive: true })
  } catch {
    /* ignore */
  }
  for (let attempt = 0; attempt < 25; attempt++) {
    try {
      mkdirSync(lockDir) // atomic; EEXIST => held
      try {
        writeFileSync(holder, JSON.stringify({ pid: process.pid, ts: Date.now() }))
      } catch {
        /* ignore */
      }
      return true
    } catch {
      // Held. Attempt a safe stale-break: only if holder is old AND its pid is
      // dead, then re-mkdir and verify WE own it.
      try {
        const h = JSON.parse(readFileSync(holder, "utf-8"))
        const age = Date.now() - (typeof h.ts === "number" ? h.ts : 0)
        let dead = false
        if (typeof h.pid === "number" && h.pid > 0) {
          try {
            process.kill(h.pid, 0)
            dead = false
          } catch (err: any) {
            dead = err && err.code === "ESRCH"
          }
        }
        if (age > 3000 && dead) {
          try {
            unlinkSync(holder)
          } catch {
            /* ignore */
          }
          try {
            rmdirSync(lockDir)
          } catch {
            /* ignore */
          }
          // Re-enter the loop; the next mkdir attempt establishes ownership.
          continue
        }
      } catch {
        // holder unreadable: do not break; just spin.
      }
      const start = Date.now()
      const wait = 20 + Math.floor(Math.random() * 10) // jitter
      while (Date.now() - start < wait) {
        /* busy spin ~20-30ms */
      }
    }
  }
  return false
}

function releaseSessionLock(sessionId: string): void {
  const lockDir = sessionLockDir(sessionId)
  const holder = `${lockDir}/holder.json`
  try {
    unlinkSync(holder)
  } catch {
    /* ENOENT tolerated */
  }
  try {
    rmdirSync(lockDir)
  } catch {
    /* ENOENT tolerated */
  }
}

// Run a read-modify-write against the session file under the lock. On lock
// acquire FAILURE, mutate a fresh read and persist via UNION-MERGE (add-only;
// prune only entries personally verified clean AND present in the read).
// `fn` receives a mutable state and may set fields; it returns nothing.
// The caller's mutations are then merged. We track which paths `fn` removed so
// the fallback can union-merge correctly.
function withSessionLock(sessionId: string, fn: (state: SessionState) => void): void {
  const locked = acquireSessionLock(sessionId)
  try {
    const state = readSessionState(sessionId)
    const before = new Set(state.touchedPaths)
    fn(state)
    if (locked) {
      writeSessionState(sessionId, state)
      return
    }
    // Lock-failure UNION-MERGE fallback. Re-read latest on disk and merge.
    const latest = readSessionState(sessionId)
    const afterSet = new Set(state.touchedPaths)
    // Adds this writer made: in `after` but not in `before`.
    const adds = [...afterSet].filter((p) => !before.has(p))
    // Removals this writer personally verified: in `before` but not in `after`.
    const removes = new Set([...before].filter((p) => !afterSet.has(p)))
    const merged = new Set(latest.touchedPaths)
    for (const p of removes) merged.delete(p)
    for (const p of adds) merged.add(p)
    // Continuation guard fields: take this writer's intent as-is. Stop is the
    // only continuation mutator and it acquires the lock; this fallback path is
    // defensive and PostToolUse/UserPromptSubmit never touch those fields.
    const mergedState: SessionState = {
      touchedPaths: [...merged],
      continuationFiredAt: state.continuationFiredAt,
      continuationCount: state.continuationCount,
    }
    writeSessionState(sessionId, mergedState)
    debugLog("session lock acquire failed; union-merged", { sessionId })
  } finally {
    if (locked) releaseSessionLock(sessionId)
  }
}

// rememberSourceWrites: union-add canonical chezmoi-source paths (verbatim
// semantics; always runs - no exit_code skip).
function rememberSourceWrites(state: SessionState, rawPaths: string[]): void {
  const set = new Set(state.touchedPaths)
  for (const raw of rawPaths) {
    const p = normalizePath(raw)
    if (isInChezmoiSource(p)) {
      set.add(p)
    }
  }
  state.touchedPaths = [...set]
}

// unpushedRels: of the given session-touched rels, return the subset that
// appears in commits ahead of the upstream (@{u}..HEAD) — i.e. committed but not
// yet pushed. The pathspec restricts the log to those rels AND we intersect with
// the rels set, so a file riding along in someone else's commit is never blamed.
// FAIL-QUIET: no upstream configured / detached HEAD / any git error -> empty
// set (treat as "nothing unpushed"), so a repo without a remote behaves exactly
// like the old commit-only guard.
function unpushedRels(rels: string[]): Set<string> {
  const out = new Set<string>()
  if (rels.length === 0) return out
  const relsSet = new Set(rels)
  try {
    const raw = execFileSync(
      GIT_BIN,
      ["-C", CHEZMOI_SOURCE_DIR, "log", "@{u}..HEAD", "--name-only", "--pretty=format:", "--", ...rels],
      { encoding: "utf-8", stdio: ["pipe", "pipe", "ignore"], timeout: 3000, env: SUBPROC_ENV },
    )
    for (const line of raw.split("\n")) {
      const t = line.trim()
      if (t && relsSet.has(t)) out.add(t)
    }
  } catch {
    // No upstream / detached HEAD / git error: fail quiet (nothing unpushed).
  }
  return out
}

// pendingTouchedPaths: classify the session-touched rels into the work still
// outstanding — `dirty` (uncommitted working-tree changes, via git status
// --porcelain, incl. rename dests) and `unpushed` (committed but ahead of
// upstream, via unpushedRels). PRUNES a path from tracking only once it is BOTH
// clean in the working tree AND already pushed: the self-heal boundary moves
// from "committed" to "committed AND pushed", so the guard keeps nagging until
// the push lands. Mutates state in place.
function pendingTouchedPaths(state: SessionState): { dirty: string[]; unpushed: string[] } {
  if (state.touchedPaths.length === 0) return { dirty: [], unpushed: [] }
  const rels = [...new Set(state.touchedPaths)]
    .map(sourceRelativePath)
    .filter((p) => p && !p.startsWith(".."))
    .sort()
  if (rels.length === 0) return { dirty: [], unpushed: [] }

  const dirty = new Set<string>()
  try {
    const out = execFileSync(
      GIT_BIN,
      ["-C", CHEZMOI_SOURCE_DIR, "status", "--porcelain", "--", ...rels],
      { encoding: "utf-8", stdio: ["pipe", "pipe", "ignore"], timeout: 3000, env: SUBPROC_ENV },
    )
    for (const line of out.split("\n")) {
      if (!line.trim()) continue
      // Porcelain v1 is `XY path` or `XY old -> new`. For renames, track the
      // destination path because that is what remains uncommitted.
      const raw = line.slice(3)
      const renamed = raw.includes(" -> ") ? raw.split(" -> ").pop() : raw
      if (renamed) dirty.add(renamed)
    }
  } catch {
    // If status fails, fail quiet rather than blame the agent for stale or
    // unverifiable state, and do NOT prune. The normal hard guards still run.
    return { dirty: [], unpushed: [] }
  }

  const unpushed = unpushedRels(rels)

  const set = new Set(state.touchedPaths)
  for (const p of dirty) {
    const abs = resolve(CHEZMOI_SOURCE_DIR, p)
    if (isInChezmoiSource(abs)) set.add(abs)
  }
  // Self-heal at the PUSH boundary: drop a path only when it is neither
  // uncommitted (working-tree dirty) nor committed-but-unpushed.
  for (const p of rels) {
    if (!dirty.has(p) && !unpushed.has(p)) set.delete(resolve(CHEZMOI_SOURCE_DIR, p))
  }
  state.touchedPaths = [...set]

  return { dirty: [...dirty], unpushed: [...unpushed] }
}

// ---------------------------------------------------------------------------
// Complaint / continuation prompt builders (verbatim TEXT from source)
// ---------------------------------------------------------------------------

function uncommittedChezmoiComplaint(state: SessionState): string | undefined {
  const { dirty, unpushed } = pendingTouchedPaths(state)
  if (dirty.length === 0 && unpushed.length === 0) return undefined
  const fmt = (paths: string[]) => {
    const shown = paths
      .slice(0, 12)
      .map((p) => `- ${p}`)
      .join("\n")
    const more = paths.length > 12 ? `\n- ...and ${paths.length - 12} more` : ""
    return `${shown}${more}`
  }
  const sections: string[] = []
  if (dirty.length > 0) sections.push(`Uncommitted (working-tree) paths:\n${fmt(dirty)}`)
  if (unpushed.length > 0) sections.push(`Committed but UNPUSHED paths:\n${fmt(unpushed)}`)
  return (
    `CHEZMOI-GUARD: You made chezmoi source changes in this session that are not yet committed AND pushed.\n` +
    `Before finishing this dotfile task, run chezmoi apply if needed, inspect git status/diff/log, ` +
    `stage only the intended files, commit, and push.\n` +
    `Committing and pushing these chezmoi changes is PRE-AUTHORIZED by the user (standing approval for all ` +
    `chezmoi dotfile work) — do it automatically, do NOT pause to ask the user for commit/push confirmation.\n` +
    `Ignore unrelated chezmoi paths you did not touch; they are likely from another concurrent agent.\n` +
    sections.join("\n")
  )
}

function uncommittedChezmoiContinuationPrompt(state: SessionState): string | undefined {
  const complaint = uncommittedChezmoiComplaint(state)
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

// managedPathError: CHANGED - RETURNS A STRING (the human text), not an Error.
function managedPathError(p: string): string {
  return (
    `[chezmoi-guard] ${p} is chezmoi-managed.\n` +
    `Edit the source instead:\n` +
    `  chezmoi edit --apply ${p}\n` +
    `or open the source file directly:\n` +
    `  $(chezmoi source-path ${p})\n` +
    `\n` +
    `When ALL your edits are complete (end of the entire task):\n` +
    `  1. Ensure changes are applied (run \`chezmoi apply\` if you\n` +
    `     edited source files without --apply).\n` +
    `  2. Inspect git status/diff/log, stage only intended files, commit, and\n` +
    `     push automatically.\n` +
    `\n` +
    `Do not use reset/rebase/merge or force-push; this guard blocks those\n` +
    `operations. Use normal git outside this guard only after explicit\n` +
    `manual handling if needed.`
  )
}

const GIT_HAZARD_MESSAGE =
  `[chezmoi-guard] bash command appears to run a destructive or\n` +
  `history-rewriting git operation in the chezmoi source repo.\n` +
  `\n` +
  `Normal commit and non-force push are allowed. Do not reset,\n` +
  `rebase, merge, or force-push; this guard blocks those actions.`

// ---------------------------------------------------------------------------
// Bash write-intent + path extraction (verbatim from source)
// ---------------------------------------------------------------------------

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

const WRITE_PATTERNS: RegExp[] = [
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
  /(?:^|[\s|;&({`])(?:rm|unlink)\b/,
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

function pathsFromBashWriteTargets(cmd: string): string[] {
  const expanded = expandHomeVars(cmd)
  const out: string[] = []
  const push = (p?: string) => {
    if (p) out.push(p.replace(/^['"]|['"]$/g, ""))
  }

  const redirectRe = /(?:^|[\s|;&({`])(?:[0-9]*&?>>?|&>>?)(?:\|?|!)?\s*([^\s|;&()<>`]+|['"][^'"]+['"])/g
  let m: RegExpExecArray | null
  while ((m = redirectRe.exec(expanded)) !== null) push(m[1])

  const commandTargetRe = /(?:^|[\s|;&({`])(cp|mv|tee|truncate|rm|unlink|install|rsync|ln|sed|perl|ruby|awk)\b([^\n;|&()]*)/g
  while ((m = commandTargetRe.exec(expanded)) !== null) {
    const kind = m[1]
    const parts = (m[2].match(/(?:['"][^'"]+['"]|\S+)/g) ?? []).filter((p) => !p.startsWith("-"))
    if (kind === "cp") push(parts.at(-1))
    else if (kind === "mv") {
      for (const p of parts) push(p)
    } else if (kind === "tee") push(parts[0])
    else if (kind === "truncate") push(parts.at(-1))
    else if (kind === "install" || kind === "rsync" || kind === "ln") push(parts.at(-1))
    else if (kind === "sed" || kind === "perl" || kind === "ruby" || kind === "awk") {
      for (const p of parts) push(p)
    } else for (const p of parts) push(p)
  }

  const ddTargetRe = /(?:^|[\s|;&({`])dd\s+[^|;&]*\bof=([^\s|;&()<>`]+|['"][^'"]+['"])/g
  while ((m = ddTargetRe.exec(expanded)) !== null) push(m[1])

  return out
}

function splitBashSegments(cmd: string): string[] {
  return cmd
    .split(/(?:;|&&|\|\||\n)/g)
    .map((s) => s.trim())
    .filter(Boolean)
}

// ---------------------------------------------------------------------------
// git-hazard detection (verbatim from source)
// ---------------------------------------------------------------------------

const GIT_PREFIX = String.raw`(?:^|[\s|;&(])git\s+(?:(?:-[cC]\s*\S+|-c\s+\S+|--(?:git-dir|work-tree)(?:=|\s+)\S+|--(?:no-pager|paginate|bare))\s+)*`
const GIT_HAZARD_RE = new RegExp(
  `${GIT_PREFIX}(?:(?:reset|rebase|merge)(?=$|[\\s|;&)])|push\\b[^|;&]*(?:\\s['"]?\\+\\S+['"]?|\\s(?:--force(?:-with-lease)?|-\\w*f\\w*|--mirror\\b)))`,
)

function gitHasHazard(cmd: string): boolean {
  return GIT_HAZARD_RE.test(cmd)
}

function resolveAgainstWorkdir(raw: string, workdir?: string): string {
  if (!workdir || raw.startsWith("/") || raw.startsWith("~")) return raw
  return resolve(normalizePath(workdir), raw)
}

function bashHazardsChezmoiRepo(cmd: string, workdir?: string): boolean {
  const expanded = expandHomeVars(cmd)
  if (
    /(?:^|[\s|;&(])chezmoi\s+git\b\s+(?:--\s+)?(?:(?:reset|rebase|merge)(?=$|[\s|;&)])|push\b[^|;&]*(?:\s['"]?\+\S+['"]?|\s(?:--force(?:-with-lease)?|-\w*f\w*|--mirror\b)))/.test(
      expanded,
    )
  ) {
    return true
  }
  // Pattern A1: explicit `git -C <chezmoi-src>` + write-class git verb.
  const gitDashCRe = /(?:^|[\s|;&(])git\s+(?:(?:-[cC]\s*\S+|-c\s+\S+|--(?:git-dir|work-tree)(?:=|\s+)\S+|--(?:no-pager|paginate|bare))\s+)*-C\s*(['"]?)([^\s'"|;&]+)\1/g
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
  // Pattern A3: workdir parameter pointed at chezmoi src + a hazard verb.
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

// ---------------------------------------------------------------------------
// NEW shape-tolerant tool_input extractors
// ---------------------------------------------------------------------------

// extractShell: pull the command string + workdir from a shell-family tool_input
// across all known shapes. command/cmd may be a STRING or an ARRAY; workdir may
// live under several key names (working_directory is the exec_command/local_shell
// array-family field).
function extractShell(ti: any): { cmd: string; workdir?: string } {
  if (!ti) return { cmd: "" }
  let cmd = ""
  if (typeof ti.command === "string") cmd = ti.command
  else if (Array.isArray(ti.command)) cmd = ti.command.join(" ")
  else if (typeof ti.cmd === "string") cmd = ti.cmd
  else if (Array.isArray(ti.cmd)) cmd = ti.cmd.join(" ")
  else if (typeof ti.script === "string") cmd = ti.script

  let workdir: string | undefined
  for (const key of [
    "workdir",
    "working_directory",
    "cwd",
    "workingDirectory",
    "directory",
    "working_dir",
    "dir",
  ]) {
    if (typeof ti[key] === "string" && ti[key]) {
      workdir = ti[key]
      break
    }
  }
  return { cmd, workdir }
}

// ---------------------------------------------------------------------------
// Output helpers
// ---------------------------------------------------------------------------

function denyPreToolUse(reason: string): never {
  let r = reason
  if (!r || !r.trim()) {
    r = "[chezmoi-guard] blocked: chezmoi-managed path or hazardous git operation."
  }
  process.stdout.write(
    JSON.stringify({
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason: r,
      },
    }),
  )
  process.exit(0)
}

function allow(): never {
  process.exit(0) // EMPTY stdout
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

function handlePreToolUse(input: any): void {
  const ti = input.tool_input ?? {}
  const name = input.tool_name
  debugLog("pretool", { session_id: input.session_id, name })

  // STEP A: edit-class tools (Edit/Write/MultiEdit/NotebookEdit). EXACT managed
  // match (mirrors the opencode BLOCKED_TOOLS path — NOT prefix; prefix on
  // edit-class would over-block directories). NotebookEdit uses a FORWARD-COMPAT
  // dual-key read: notebook_path OR file_path, so a CC build that sends file_path
  // for notebooks still triggers the block.
  const editPaths: string[] = []
  if (name === "Edit" || name === "Write" || name === "MultiEdit") {
    if (typeof ti.file_path === "string" && ti.file_path) editPaths.push(ti.file_path)
  } else if (name === "NotebookEdit") {
    const nb =
      typeof ti.notebook_path === "string" && ti.notebook_path
        ? ti.notebook_path
        : typeof ti.file_path === "string" && ti.file_path
          ? ti.file_path
          : ""
    if (nb) editPaths.push(nb)
  }
  if (editPaths.length > 0) {
    const managed = loadManaged({ coldSpawnOnly: true })
    for (const raw of editPaths) {
      const p = normalizePath(raw)
      if (managedHas(p, managed)) {
        debugLog("deny edit managed", { session_id: input.session_id, path: p })
        denyPreToolUse(managedPathError(p))
      }
    }
  }

  const { cmd, workdir } = extractShell(ti)
  const resolvedWorkdir = workdir || input.cwd

  // STEP B: git-hazard against the chezmoi source repo.
  if (cmd && bashHazardsChezmoiRepo(cmd, resolvedWorkdir)) {
    debugLog("deny git hazard", { session_id: input.session_id })
    denyPreToolUse(GIT_HAZARD_MESSAGE + "\nCommand (truncated): " + cmd.slice(0, 240))
  }

  // STEP C: per-segment write check (PREFIX-aware touchesManagedPath).
  if (cmd) {
    const managed = loadManaged({ coldSpawnOnly: true })
    for (const seg of splitBashSegments(cmd)) {
      const writeTargets = pathsFromBashWriteTargets(seg)
      // Only WRITE targets are blocked; a segment that merely reads or names a
      // managed path (e.g. `cat ~/.zshrc`) has no write target -> allowed.
      if (!bashHasWriteIntent(seg) && writeTargets.length === 0) continue
      // Prefer explicit write targets; fall back to all path tokens only when a
      // write-intent segment produced no parseable target (exotic quoting).
      const candidatePaths = writeTargets.length > 0 ? writeTargets : pathsFromBashCommand(seg)
      for (const raw of candidatePaths) {
        const p = normalizePath(resolveAgainstWorkdir(raw, resolvedWorkdir))
        if (touchesManagedPath(p, managed)) {
          debugLog("deny bash write managed", { session_id: input.session_id, path: p })
          denyPreToolUse(managedPathError(p))
        }
      }
    }
  }

  allow()
}

function handlePostToolUse(input: any): void {
  const ti = input.tool_input ?? {}
  const name = input.tool_name
  const sid = input.session_id
  debugLog("posttool", { session_id: sid, name })

  withSessionLock(sid, (state) => {
    // STEP remember (always; no exit_code skip — git status self-heals).
    // Edit-class tools use the SAME dual-key path logic as PreToolUse STEP A.
    const editPaths: string[] = []
    if (name === "Edit" || name === "Write" || name === "MultiEdit") {
      if (typeof ti.file_path === "string" && ti.file_path) editPaths.push(ti.file_path)
    } else if (name === "NotebookEdit") {
      const nb =
        typeof ti.notebook_path === "string" && ti.notebook_path
          ? ti.notebook_path
          : typeof ti.file_path === "string" && ti.file_path
            ? ti.file_path
            : ""
      if (nb) editPaths.push(nb)
    }
    if (editPaths.length > 0) {
      rememberSourceWrites(state, editPaths)
    } else {
      const { cmd, workdir } = extractShell(ti)
      const resolvedWorkdir = workdir || input.cwd
      if (cmd) {
        const paths: string[] = []
        for (const seg of splitBashSegments(cmd)) {
          const targetPaths = pathsFromBashWriteTargets(seg)
          if (
            !bashHasWriteIntent(seg) &&
            !(resolvedWorkdir && isInChezmoiSource(resolvedWorkdir) && targetPaths.length > 0)
          ) {
            continue
          }
          paths.push(...pathsFromBashCommand(seg), ...targetPaths)
        }
        if (resolvedWorkdir && isInChezmoiSource(resolvedWorkdir)) {
          rememberSourceWrites(
            state,
            paths.map((p) => resolveAgainstWorkdir(p, resolvedWorkdir)),
          )
        } else {
          rememberSourceWrites(state, paths)
        }
      }
    }
    // STEP recompute: prune + persist via the lock writer.
    pendingTouchedPaths(state)
    // DO NOT touch continuationFiredAt/continuationCount here (Stop backstop).
  })

  process.exit(0) // EMPTY stdout
}

function handleUserPromptSubmit(input: any): void {
  const sid = input.session_id
  debugLog("userpromptsubmit", { session_id: sid })
  let complaint: string | undefined
  withSessionLock(sid, (state) => {
    complaint = uncommittedChezmoiComplaint(state) // calls pendingTouchedPaths -> prune + persist
  })
  if (!complaint) {
    process.exit(0) // EMPTY stdout — omit additionalContext entirely
  }
  process.stdout.write(
    JSON.stringify({
      hookSpecificOutput: {
        hookEventName: "UserPromptSubmit",
        additionalContext: complaint,
      },
    }),
  )
  process.exit(0)
}

function handleStop(input: any): void {
  const sid = input.session_id

  // GUARD 1 (primary, confirmed): stop_hook_active short-circuit.
  if (input.stop_hook_active === true) {
    debugLog("stop loop guard (stop_hook_active)", { session_id: sid })
    process.exit(0) // allow stop, empty stdout
  }

  let prompt: string | undefined
  let block = false
  let lockedOk = true
  try {
    withSessionLock(sid, (state) => {
      const now = Date.now()
      // GUARD 2a: within the 2-min window -> allow stop.
      if (state.continuationFiredAt && now - state.continuationFiredAt < CONTINUATION_WINDOW_MS) {
        debugLog("stop within continuation window", { session_id: sid })
        return
      }
      // GUARD 2b: absolute cap -> allow stop.
      if ((state.continuationCount ?? 0) >= MAX_CONTINUATIONS) {
        debugLog("stop continuation cap reached", { session_id: sid })
        return
      }
      prompt = uncommittedChezmoiContinuationPrompt(state) // prune + persist inside
      if (!prompt) {
        // Clean -> reset backstop, allow stop.
        state.continuationFiredAt = 0
        state.continuationCount = 0
        debugLog("stop clean", { session_id: sid })
        return
      }
      state.continuationFiredAt = now
      state.continuationCount = (state.continuationCount ?? 0) + 1
      block = true
    })
  } catch (e) {
    lockedOk = false
    debugLog("stop state error (fail-safe allow)", { session_id: sid, error: String(e) })
  }

  // FAIL-SAFE: unreadable state / not blocking -> allow stop.
  if (!lockedOk || !block) {
    process.exit(0) // empty stdout, allow stop
  }
  if (!prompt || !prompt.trim()) {
    process.exit(0) // defensive: never block with empty reason
  }
  debugLog("stop blocking continuation", { session_id: sid })
  process.stdout.write(JSON.stringify({ decision: "block", reason: prompt }))
  process.exit(0)
}

// ---------------------------------------------------------------------------
// Opportunistic GC (best-effort, never throws)
// ---------------------------------------------------------------------------

function opportunisticGc(): void {
  try {
    const now = Date.now()
    const tmpMaxAge = 5 * 60 * 1000 // 5 min for *.tmp orphans
    const sessionMaxAge = 7 * 24 * 60 * 60 * 1000 // 7 days for session files
    // STATE_DIR-level *.tmp orphans
    for (const entry of readdirSync(STATE_DIR)) {
      if (!entry.endsWith(".tmp")) continue
      const full = `${STATE_DIR}/${entry}`
      try {
        if (now - statSync(full).mtimeMs > tmpMaxAge) unlinkSync(full)
      } catch {
        /* ignore */
      }
    }
    if (existsSync(SESSIONS_DIR)) {
      for (const entry of readdirSync(SESSIONS_DIR)) {
        const full = `${SESSIONS_DIR}/${entry}`
        try {
          const st = statSync(full)
          if (entry.endsWith(".tmp") && now - st.mtimeMs > tmpMaxAge) {
            unlinkSync(full)
          } else if (entry.endsWith(".json") && now - st.mtimeMs > sessionMaxAge) {
            unlinkSync(full)
          }
        } catch {
          /* ignore */
        }
      }
    }
  } catch {
    /* best-effort */
  }
}

// ---------------------------------------------------------------------------
// main / dispatch
// ---------------------------------------------------------------------------

function main(): void {
  let text = ""
  try {
    text = readFileSync(0, "utf-8") // read ALL of stdin (fd 0)
  } catch {
    process.exit(0) // fail open
  }

  let input: any
  try {
    input = JSON.parse(text)
  } catch {
    process.exit(0) // fail open on parse error
  }

  try {
    mkdirSync(STATE_DIR, { recursive: true })
    mkdirSync(SESSIONS_DIR, { recursive: true })
  } catch {
    /* ignore */
  }

  opportunisticGc()

  if (CHEZMOI_BIN === "chezmoi" && !isExecutable("/opt/homebrew/bin/chezmoi")) {
    debugLog("chezmoi binary not at /opt/homebrew/bin/chezmoi; using PATH lookup")
  }

  const ev = input?.hook_event_name
  try {
    switch (ev) {
      case "PreToolUse":
        handlePreToolUse(input)
        break
      case "PostToolUse":
        handlePostToolUse(input)
        break
      case "UserPromptSubmit":
        handleUserPromptSubmit(input)
        break
      case "Stop":
        handleStop(input)
        break
      default:
        process.exit(0)
    }
  } catch (e) {
    debugLog("handler error", { ev, error: String(e) })
    process.exit(0) // FAIL OPEN
  }
}

main()
