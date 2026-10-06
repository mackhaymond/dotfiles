// chezmoi-guard (codex-cli native hook port)
//
// Port of the opencode in-process plugin (plugins/chezmoi-guard.ts) to codex
// native hooks (codex-cli 0.148, `[features] hooks = true`). A SINGLE
// dispatcher invoked as a fresh subprocess per hook call, branching on
// hook_event_name:
//
//   PreToolUse        - HARD-BLOCK apply_patch / shell-family writes to
//                       chezmoi-managed paths, and destructive/history-rewriting
//                       git ops on the chezmoi source repo. Emits a deny JSON.
//   PostToolUse       - remember source-repo writes this session made: by
//                       command-text heuristics AND by evidence (anything in the
//                       source tree, source commits, or managed live targets
//                       whose mtime landed inside this tool call's window — see
//                       attributeSessionWrites). Recompute the dirty set.
//   UserPromptSubmit  - inject the "uncommitted/unpushed/drifted chezmoi changes"
//                       complaint as additionalContext.
//   Stop              - if session-touched chezmoi paths are still uncommitted,
//                       unpushed, OR `chezmoi status` reports live!=source for a
//                       target this session worked on, block the stop with a
//                       continuation prompt (loop-guarded).
//   SubagentStop      - same handler as Stop (session-keyed). Only reachable if
//                       a SubagentStop group is registered in hooks.json.
//
// The managed-set cache is REFRESHED (TTL 300s) only from UserPromptSubmit and
// Stop — once per turn, off the tool-call critical path. PreToolUse and
// PostToolUse only ever read the cache (a stale-but-good set beats latency).
//
// Because each hook is a separate subprocess with NO shared memory, all state
// from the source plugin (managed-set cache, per-session touchedPaths, the
// continuation guard) is externalized to disk under
// /Users/mackhaymond/.codex/.tmp/chezmoi-guard, keyed by session_id.
//
// Uses only node:child_process / node:fs / node:path, so the command may be
// swapped from bun to node (v26 confirmed) without code changes.
//
// FAIL-OPEN philosophy: any uncaught/internal error -> exit 0 with empty stdout.
// The two HARD blocks are pure functions of (tool_input, managed.json) with
// ZERO dependence on session state, so a corrupt/locked session file can never
// weaken a block.

import { execFileSync } from "node:child_process"
import {
  appendFileSync,
  existsSync,
  lstatSync,
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
import { dirname, relative, resolve } from "node:path"

// ---------------------------------------------------------------------------
// Module constants
// ---------------------------------------------------------------------------

const TTL_MS = 5 * 60 * 1000 // 300000 steady-state
const COLD_TTL_MS = 15_000 // cold-start retry window
const MAX_CONTINUATIONS = 3
const CONTINUATION_WINDOW_MS = 2 * 60 * 1000 // 120000

const STATE_DIR = "/Users/mackhaymond/.codex/.tmp/chezmoi-guard"
const SESSIONS_DIR = STATE_DIR + "/sessions"
const MANAGED_FILE = STATE_DIR + "/managed.json"
const MANAGED_REFRESH_LOCK = STATE_DIR + "/managed.refresh.lock"
const LOG_FILE = STATE_DIR + "/chezmoi-guard.log"
const LOG_ROTATE_BYTES = 1_000_000 // rotate to <log>.1 (overwrite) past 1 MB
// Exact-claims ledger SHARED by the Claude, codex and opencode guards: one
// file per chezmoi source path a session wrote by name (apply_patch, or a
// shell write the text heuristics resolved). Time-based evidence never
// outranks another session's exact claim — see dropForeignClaims.
const CLAIMS_DIR = "/Users/mackhaymond/.local/state/chezmoi-guard/claims"
const CLAIM_TTL_MS = 24 * 60 * 60 * 1000
const CLAIM_OWNER = (sid: string) => `codex:${sid}`
// The pty read-only classifier (the Claude Code auto/plan-mode hook). A
// command it calls read-only cannot have written anything, so it gets no
// evidence attribution. Loaded lazily; a load failure means "not read-only".
const READ_ONLY_CLASSIFIER = "/Users/mackhaymond/.claude/hooks/pty-read-only.ts"
// Fallback window length when a PostToolUse finds no PreToolUse start stamp
// (missing tool_use_id, stamp write failed).
const UNSTAMPED_WINDOW_MS = 600_000
// How long a command that OUTLIVES its tool call (spawn_pty, a timed-out or
// interrupted run, `cmd &`, keys typed into a pty) keeps the session's
// attribution continuous — see outlivesCall.
const LINGER_MS = 60 * 60 * 1000
// This invocation's claim owner, set once in main() from session_id.
let OWNER = ""

// Per-tool-call bookkeeping lines (pretool/posttool/userpromptsubmit) are noise
// at ~90% of log volume; they are emitted only with CHEZMOI_GUARD_DEBUG=1.
// Block / continuation / refresh / error lines are always written.
const VERBOSE = process.env.CHEZMOI_GUARD_DEBUG === "1"

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

function traceLog(message: string, data?: Record<string, unknown>): void {
  if (VERBOSE) debugLog(message, data)
}

// Single-slot rotation: once per subprocess (from main), if the log exceeds
// LOG_ROTATE_BYTES rename it to `.1`, overwriting any previous `.1`.
function rotateLogIfLarge(): void {
  try {
    if (statSync(LOG_FILE).size > LOG_ROTATE_BYTES) renameSync(LOG_FILE, LOG_FILE + ".1")
  } catch {
    /* missing log or rename failure: ignore */
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
    const started = Date.now()
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
    debugLog("managed refreshed", { count: paths.length, ms: Date.now() - started })
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
//   coldSpawnOnly=false (UserPromptSubmit/Stop only — see refreshManagedOffHotPath):
//   full TTL refresh, re-spawning `chezmoi managed` at most once per 300s.
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

// The ONLY full-TTL refresh entry point. Called from the per-turn events
// (UserPromptSubmit, Stop) so PreToolUse keeps reading a cache that is at most
// ~300s + one turn stale, with zero spawn on the tool-call path. Never throws.
function refreshManagedOffHotPath(): void {
  try {
    loadManaged()
  } catch (e) {
    debugLog("managed refresh error", { error: String(e) })
  }
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
  // Evidence-based attribution (see attributeSessionWrites): the end of the
  // last PostToolUse we ran (window start for the next one), the source repo
  // HEAD we last saw, and managed LIVE targets whose mtime landed inside one of
  // this session's tool-call windows.
  lastSeenAt: number
  headSha: string
  liveTouched: string[]
  // The subset of touchedPaths this session wrote BY NAME (not by time
  // evidence). Only these are published to the claims ledger, and only the
  // others can be suppressed in favour of another session's claim.
  exactPaths: string[]
  // While > now, something this session started may still be writing after
  // its tool call returned: every hook event attributes [lastSeenAt, now]
  // (the pre-2026-10-06 continuous window), instead of only shell run time.
  lingerUntil: number
}

function emptySessionState(): SessionState {
  return {
    touchedPaths: [],
    continuationFiredAt: 0,
    continuationCount: 0,
    lastSeenAt: 0,
    headSha: "",
    liveTouched: [],
    exactPaths: [],
    lingerUntil: 0,
  }
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
      lastSeenAt: typeof j.lastSeenAt === "number" ? j.lastSeenAt : 0,
      headSha: typeof j.headSha === "string" ? j.headSha : "",
      liveTouched: Array.isArray(j.liveTouched)
        ? j.liveTouched.filter((p: unknown) => typeof p === "string")
        : [],
      exactPaths: Array.isArray(j.exactPaths)
        ? j.exactPaths.filter((p: unknown) => typeof p === "string")
        : [],
      lingerUntil: typeof j.lingerUntil === "number" ? j.lingerUntil : 0,
    }
  } catch {
    return emptySessionState()
  }
}

function writeSessionState(sessionId: string, state: SessionState): void {
  try {
    mkdirSync(SESSIONS_DIR, { recursive: true })
    const touched = new Set(state.touchedPaths)
    atomicWrite(
      sessionFile(sessionId),
      JSON.stringify({
        version: 1,
        touchedPaths: [...touched],
        continuationFiredAt: state.continuationFiredAt,
        continuationCount: state.continuationCount,
        lastSeenAt: state.lastSeenAt,
        headSha: state.headSha,
        liveTouched: [...new Set(state.liveTouched)],
        // exactPaths ⊆ touchedPaths: a pruned (committed + pushed) path drops
        // out of both.
        exactPaths: [...new Set(state.exactPaths)].filter((p) => touched.has(p)),
        lingerUntil: state.lingerUntil,
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
    // liveTouched: same add-only union (prunes are Stop/UserPromptSubmit-side
    // and re-derived from `chezmoi status`, so a lost prune only re-nags).
    const liveMerged = new Set([...latest.liveTouched, ...state.liveTouched])
    const mergedState: SessionState = {
      touchedPaths: [...merged],
      continuationFiredAt: state.continuationFiredAt,
      continuationCount: state.continuationCount,
      lastSeenAt: Math.max(latest.lastSeenAt, state.lastSeenAt),
      headSha: state.headSha || latest.headSha,
      liveTouched: [...liveMerged],
      exactPaths: [...new Set([...latest.exactPaths, ...state.exactPaths])],
      lingerUntil: Math.max(latest.lingerUntil, state.lingerUntil),
    }
    writeSessionState(sessionId, mergedState)
    debugLog("session lock acquire failed; union-merged", { sessionId })
  } finally {
    if (locked) releaseSessionLock(sessionId)
  }
}

// rememberSourceWrites: union-add canonical chezmoi-source paths (verbatim
// semantics; always runs - no exit_code skip). Heuristic: a write-intent
// segment contributes EVERY path token, reads included, so these are NOT
// exact claims — see claimExactWrites for that.
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

// claimExactWrites: paths this session certainly WROTE by name — an edit-class
// tool's file_path, or a shell segment's parsed write target (never the
// all-tokens fallback, so `cat <src>/x > /tmp/y` does not claim x). Tracked as
// touched AND published to the shared claims ledger.
function claimExactWrites(state: SessionState, rawPaths: string[]): void {
  const set = new Set(state.touchedPaths)
  const exact = new Set(state.exactPaths)
  for (const raw of rawPaths) {
    const p = normalizePath(raw)
    if (isInChezmoiSource(p)) {
      set.add(p)
      exact.add(p)
      writeClaim(p, OWNER)
    }
  }
  state.touchedPaths = [...set]
  state.exactPaths = [...exact]
}

// ---------------------------------------------------------------------------
// Exact-claims ledger (shared across the three guards)
//   CLAIMS_DIR/<sha256(path)[:32]>.json = { path, owner, at }   last writer wins
// A claim says "owner wrote this file at `at`". It vouches only for that
// write: once the file's mtime moves past `at` someone wrote it again, and the
// claim no longer explains the change. Released when the owner's own path is
// committed + pushed (pendingTouchedPaths); TTL is only the backstop.
// ---------------------------------------------------------------------------

const CLAIM_MTIME_SLACK_MS = 2000

function claimFile(p: string): string {
  return `${CLAIMS_DIR}/${createHash("sha256").update(p).digest("hex").slice(0, 32)}.json`
}

function writeClaim(p: string, owner: string): void {
  if (!owner) return
  try {
    mkdirSync(CLAIMS_DIR, { recursive: true })
    atomicWrite(claimFile(p), JSON.stringify({ path: p, owner, at: Date.now() }))
  } catch {
    /* best-effort: a lost claim only means a possible extra nag elsewhere */
  }
}

function readClaim(p: string): { owner: string; at: number } | undefined {
  try {
    const j = JSON.parse(readFileSync(claimFile(p), "utf-8"))
    if (j.path !== p || typeof j.owner !== "string" || typeof j.at !== "number") return undefined
    if (Date.now() - j.at > CLAIM_TTL_MS) return undefined
    return { owner: j.owner, at: j.at }
  } catch {
    return undefined
  }
}

// Release OUR claim on a path we no longer track (committed + pushed).
function releaseClaim(p: string): void {
  if (!OWNER) return
  try {
    if (readClaim(p)?.owner === OWNER) unlinkSync(claimFile(p))
  } catch {
    /* already gone */
  }
}

// foreignClaimed: paths this session holds only by TIME evidence whose current
// content another session wrote BY NAME (its claim is fresh and the file has
// not been written since). Those are suppressed from this session's report,
// NOT deleted from its state — if the claim lapses or the file is written
// again, the path reports again. Our own exact claims are never suppressed; a
// deleted file (no mtime to compare) is never suppressed.
function foreignClaimed(state: SessionState): Set<string> {
  const out = new Set<string>()
  const exact = new Set(state.exactPaths)
  for (const p of state.touchedPaths) {
    if (exact.has(p)) continue
    const c = readClaim(p)
    if (!c || c.owner === OWNER) continue
    try {
      if (lstatSync(p).mtimeMs <= c.at + CLAIM_MTIME_SLACK_MS) out.add(p)
    } catch {
      /* deleted: cannot tell whose deletion it was — keep reporting */
    }
  }
  return out
}

// ---------------------------------------------------------------------------
// Tool-call start stamps: PreToolUse records when each call began, so the
// matching PostToolUse attributes only what changed while the tool RAN — not
// model think-time, not the user's idle time between turns, not "since the
// session started". Lock-free: one tiny file per in-flight call.
//   SESSIONS_DIR/<key>.calls/<sanitized tool_use_id>   (content: start ms)
// ---------------------------------------------------------------------------

function callStampFile(input: any): string | undefined {
  const sid = input?.session_id
  const id = input?.tool_use_id
  if (typeof sid !== "string" || !sid || typeof id !== "string" || !id) return undefined
  return `${SESSIONS_DIR}/${sessionKey(sid)}.calls/${id.replace(/[^A-Za-z0-9._-]/g, "_")}`
}

function stampToolStart(input: any): void {
  try {
    const f = callStampFile(input)
    if (!f) return
    mkdirSync(dirname(f), { recursive: true })
    writeFileSync(f, String(Date.now()))
  } catch {
    /* no stamp: PostToolUse falls back to UNSTAMPED_WINDOW_MS */
  }
}

function takeToolStart(input: any): number | undefined {
  const f = callStampFile(input)
  if (!f) return undefined
  try {
    const t = Number(readFileSync(f, "utf-8"))
    return Number.isFinite(t) && t > 0 ? t : undefined
  } catch {
    return undefined
  } finally {
    try {
      unlinkSync(f)
    } catch {
      /* already gone */
    }
  }
}

let readOnlyClassifier: ((cmd: string) => boolean) | null | undefined
function isReadOnlyShellCommand(cmd: string): boolean {
  if (readOnlyClassifier === undefined) {
    try {
      const m = require(READ_ONLY_CLASSIFIER)
      readOnlyClassifier = typeof m?.isReadOnlyCommand === "function" ? m.isReadOnlyCommand : null
    } catch {
      readOnlyClassifier = null
    }
  }
  try {
    return readOnlyClassifier ? readOnlyClassifier(cmd) === true : false
  } catch {
    return false
  }
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

// ---------------------------------------------------------------------------
// Evidence-based attribution.
//
// The shell heuristics above only SEE writes spelled as redirects / cp / mv /
// sed -i. A `python3 - <<EOF ... write_text()` heredoc, `chezmoi edit`, an
// editor, or a relative path behind `cd $(chezmoi source-path) &&` (a command
// substitution the hook cannot expand) is invisible to them — which is how a
// session once left two commits unpushed and a source edit unapplied with the
// Stop guard reporting "clean". So we also look at what actually changed on
// disk while something of ours could have been writing:
//
//   - a write-capable shell call (shouldAttribute): [startedAt - slack, now],
//     startedAt = this call's PreToolUse stamp (stampToolStart); without a
//     stamp, the last UNSTAMPED_WINDOW_MS, never past the previous call;
//   - while a command we started may still be running after its tool call
//     returned (lingerUntil, see outlivesCall): every hook event covers
//     [lastSeenAt - slack, now], i.e. continuously.
//
// (Until 2026-10-06 the window was ALWAYS the continuous one, from session
// start on — it covered model think-time, the user's idle time between turns
// and read-only commands, and every session got blamed for whatever other
// agents wrote meanwhile.)
//
//   (a) source repo working tree: every `git status --porcelain` path whose
//       mtime (or, for a deletion, its parent dir's mtime) is inside the window
//   (b) source repo commits made inside the window (the session committed;
//       the push guard needs to know)
//   (c) managed LIVE targets whose mtime is inside the window — the session
//       wrote a live file some other way, or ran `chezmoi apply`; either way the
//       Stop guard must check them against the source.
//
// Attribution is still by TIME, so a concurrent agent writing the same repo
// inside one of our windows is misattributed to us — unless that agent wrote
// the path BY NAME and nobody wrote it since, in which case its claim
// suppresses it from our report (foreignClaimed). The residual cost is one
// extra nag (whose text says to leave unrelated paths alone); the cost of a
// blind spot is silently losing work — accepted.
// ---------------------------------------------------------------------------

const ATTRIB_SLACK_MS = 2000

// Only shell calls can write unseen: apply_patch writes exactly its patch
// paths (remembered by name), and a command the read-only classifier accepts
// wrote nothing. A leading VAR=value makes any command suspect (it can point
// git/a pager at an arbitrary program), so it never counts as read-only.
// `cmd` is the classifiable command (see classifiableCommand).
function shouldAttribute(cmd: string): boolean {
  if (!cmd.trim()) return false
  if (/(^|[;&|(\n]\s*)[A-Za-z_][A-Za-z0-9_]*=\S*\s+\S/.test(cmd)) return true
  return !isReadOnlyShellCommand(cmd)
}

// codex's `shell` tool sends argv like ["bash","-lc","<script>"]; joined, it
// starts with `bash` and never classifies as read-only. Unwrap the script.
function classifiableCommand(ti: any, cmd: string): string {
  const argv = Array.isArray(ti?.command) ? ti.command : Array.isArray(ti?.cmd) ? ti.cmd : undefined
  if (
    argv &&
    argv.length >= 3 &&
    typeof argv[2] === "string" &&
    /^(?:.*\/)?(?:ba|z)?sh$/.test(String(argv[0])) &&
    /^-l?c$/.test(String(argv[1]))
  ) {
    return argv[2]
  }
  return cmd
}

// Something that can matter for chezmoi: names chezmoi or its source tree,
// opens an editor, or names a managed live path.
function chezmoiRelevant(text: string): boolean {
  if (/\bchezmoi\b/.test(text) || text.includes(CHEZMOI_SOURCE_DIR) || text.includes("~/.local/share/chezmoi")) return true
  if (/(^|[\s;&|("'`])(n?vim?|vi|nano|emacs|hx|micro|code|subl)(\s|$|["'])/.test(text)) return true
  const managed = readManagedCache().paths
  return pathsFromBashCommand(text).some((p) => touchesManagedPath(normalizePath(p), managed))
}

// The tool result's own leading text (not the command's output, which may
// legitimately contain anything): a string, or the first text block.
function responseHead(input: any): string {
  const r = input?.tool_response
  try {
    if (typeof r === "string") return r
    const blocks = Array.isArray(r) ? r : Array.isArray(r?.content) ? r.content : []
    const first = blocks.find((b: any) => typeof b?.text === "string")
    return first ? first.text : ""
  } catch {
    return ""
  }
}

// Quoted strings and heredoc bodies are data, not shell syntax: a `&` in a
// commit message or a sed replacement must not read as backgrounding.
function stripQuoted(cmd: string): string {
  return cmd
    .replace(/<<-?\s*(['"]?)(\w+)\1[^\n]*\n[\s\S]*?\n\s*\2\s*(?=\n|$)/g, " ")
    .replace(/'[^']*'/g, "''")
    .replace(/"(?:[^"\\]|\\.)*"/g, '""')
}

// codex's exec result starts with a metadata header ("Chunk ID …", "Wall
// time …", "Process running with session ID N" / "Process exited with code N")
// before "Output:"; only the header is the tool's own word.
function execHeader(input: any): string {
  return responseHead(input).split(/\nOutput:/)[0]
}

// outlivesCall: did this tool call leave something chezmoi-relevant running
// that may write AFTER the call returned? Then attribution stays continuous
// for LINGER_MS. Gated on relevance: codex yields long commands routinely, and
// a build must not re-widen every window for an hour.
//   - an exec that yielded while still running (header, not output text),
//     `cmd &` / nohup / disown / setsid (outside quotes/heredocs), spawn_pty;
//   - write_stdin / keys typed into a running process: the input is relevant.
function outlivesCall(input: any, ti: any, cmd: string): boolean {
  if (!cmd.trim()) {
    // No command: stdin / keystrokes into something already running.
    let typed = ""
    try {
      typed = JSON.stringify(ti ?? {})
    } catch {
      return false
    }
    return /write_stdin|send_keys/.test(String(input?.tool_name ?? "")) && chezmoiRelevant(typed)
  }
  if (!shouldAttribute(classifiableCommand(ti, cmd))) return false
  const bare = stripQuoted(cmd)
  const stillRunning =
    /^Process running with session ID \d+/m.test(execHeader(input)) ||
    /spawn_pty$/.test(String(input?.tool_name ?? "")) ||
    /(^|[^&|>])&(?![&>])/.test(bare) ||
    /(^|[\s;&|(])(nohup|disown|setsid)(\s|$)/.test(bare)
  return stillRunning && chezmoiRelevant(cmd)
}

// A shell call that demonstrably failed (exec header "Process exited with
// code N", N != 0) may not have written its targets: no claims.
function shellCallFailed(input: any): boolean {
  const m = /^Process exited with code (\d+)/m.exec(execHeader(input))
  return !!m && m[1] !== "0"
}

// At UserPromptSubmit / Stop: while something we started may still be
// running, attribute what changed since the previous hook event (a spawned
// `chezmoi edit` saved between turns has no tool call of its own).
function sweepLingering(state: SessionState): void {
  if (state.lingerUntil <= 0) return
  const now = Date.now()
  if (state.lastSeenAt > 0) attributeSessionWrites(state, state.lastSeenAt, readManagedCache().paths)
  state.lastSeenAt = now
  if (now > state.lingerUntil) state.lingerUntil = 0
}

function attributeSessionWrites(state: SessionState, start: number, managed: string[]): void {
  const now = Date.now()
  const since = start > 0 ? start - ATTRIB_SLACK_MS : 0
  const inWindow = (ms: number) => since > 0 && ms >= since && ms <= now + 1000
  const touched = new Set(state.touchedPaths)
  const live = new Set(state.liveTouched)
  let added = 0

  // (a) working tree
  try {
    const out = execFileSync(
      GIT_BIN,
      ["-C", CHEZMOI_SOURCE_DIR, "status", "--porcelain", "-uall", "--no-renames"],
      { encoding: "utf-8", stdio: ["pipe", "pipe", "ignore"], timeout: 3000, env: SUBPROC_ENV },
    )
    for (const line of out.split("\n")) {
      if (!line.trim()) continue
      const rel = line.slice(3).replace(/^"|"$/g, "")
      const abs = resolve(CHEZMOI_SOURCE_DIR, rel)
      let t: number | undefined
      try {
        t = lstatSync(abs).mtimeMs
      } catch {
        try {
          t = statSync(dirname(abs)).mtimeMs // deleted: the dir entry changed
        } catch {
          /* gone entirely */
        }
      }
      if (t !== undefined && inWindow(t) && !touched.has(abs)) {
        touched.add(abs)
        added++
      }
    }
  } catch {
    /* git unavailable / not a repo: heuristics alone */
  }

  // (b) commits made inside the window. HEAD is only sampled on attributing
  // calls, so with no remembered HEAD (first write-capable call) or an
  // unreachable one (rewritten history), fall back to the newest commits —
  // the --since filter is what scopes them to this window either way.
  try {
    const head = execFileSync(GIT_BIN, ["-C", CHEZMOI_SOURCE_DIR, "rev-parse", "HEAD"], {
      encoding: "utf-8",
      stdio: ["pipe", "pipe", "ignore"],
      timeout: 3000,
      env: SUBPROC_ENV,
    }).trim()
    if (since > 0 && head && head !== state.headSha) {
      const gitLog = (range: string[]) =>
        execFileSync(
          GIT_BIN,
          ["-C", CHEZMOI_SOURCE_DIR, "log", ...range, "--name-only", "--pretty=format:", `--since=${new Date(since).toISOString()}`],
          { encoding: "utf-8", stdio: ["pipe", "pipe", "ignore"], timeout: 3000, env: SUBPROC_ENV },
        )
      let raw: string
      try {
        raw = state.headSha ? gitLog([`${state.headSha}..${head}`]) : gitLog([head, "--max-count=50"])
      } catch {
        raw = gitLog([head, "--max-count=50"])
      }
      for (const line of raw.split("\n")) {
        const rel = line.trim()
        if (!rel) continue
        const abs = resolve(CHEZMOI_SOURCE_DIR, rel)
        if (!touched.has(abs)) {
          touched.add(abs)
          added++
        }
      }
    }
    if (head) state.headSha = head
  } catch {
    /* old sha unreachable (rewritten history) or git error: skip */
  }

  // (c) live managed targets written inside the window
  if (since > 0) {
    for (const p of managed) {
      try {
        if (inWindow(lstatSync(p).mtimeMs) && !live.has(p)) {
          live.add(p)
          added++
        }
      } catch {
        /* target missing: `chezmoi status` will report it if it matters */
      }
    }
  }

  state.touchedPaths = [...touched]
  state.liveTouched = [...live]
  if (added > 0 && VERBOSE) debugLog("attributed by evidence", { added, since })
}

// Fresh `chezmoi managed` list (files, dirs, symlinks; absolute). NOT the TTL
// cache: a source file added seconds ago must count, and `chezmoi status`
// aborts on the first target it does not manage.
function currentManagedTargets(): Set<string> | undefined {
  try {
    const out = execFileSync(
      CHEZMOI_BIN,
      ["managed", "--include=files,dirs,symlinks", "--path-style=absolute"],
      { encoding: "utf-8", stdio: ["pipe", "pipe", "ignore"], timeout: 5000, env: SUBPROC_ENV },
    )
    return new Set(out.split("\n").map((s) => s.trim()).filter(Boolean).map(normalizePath))
  } catch {
    return undefined
  }
}

// driftedTargets: run `chezmoi status` over (live targets this session wrote)
// ∪ (targets of the source paths this session touched) and return the drifted
// ones as home-relative paths — exactly what the zsh prompt's `~` glyph shows,
// scoped to this session's work. Also returns the SOURCE paths behind them so
// the caller can keep those tracked (a committed-and-pushed but never applied
// source edit must not be pruned as "done"). Prunes clean liveTouched entries.
// FAIL-QUIET: any chezmoi error -> nothing drifted (the hard guards still run).
function driftedTargets(state: SessionState): { drifted: string[]; keepSources: Set<string> } {
  const none = { drifted: [] as string[], keepSources: new Set<string>() }
  const sources = [...new Set(state.touchedPaths)].filter((p) => isInChezmoiSource(p))
  if (sources.length === 0 && state.liveTouched.length === 0) return none

  const targetToSource = new Map<string, string>()
  if (sources.length > 0) {
    try {
      const out = execFileSync(CHEZMOI_BIN, ["target-path", ...sources], {
        encoding: "utf-8",
        stdio: ["pipe", "pipe", "ignore"],
        timeout: 5000,
        env: SUBPROC_ENV,
      })
      const lines = out.split("\n").map((s) => s.trim()).filter(Boolean)
      if (lines.length === sources.length) {
        lines.forEach((t, i) => targetToSource.set(normalizePath(t), sources[i]))
      }
    } catch {
      /* one bad path aborts the batch: fall through with live targets only */
    }
  }

  const managed = currentManagedTargets()
  if (!managed) return none
  const targets = new Set<string>()
  for (const t of targetToSource.keys()) if (managed.has(t)) targets.add(t)
  for (const t of state.liveTouched) {
    const n = normalizePath(t)
    if (managed.has(n)) targets.add(n)
  }
  if (targets.size === 0) {
    state.liveTouched = []
    return none
  }

  let out: string
  try {
    out = execFileSync(CHEZMOI_BIN, ["status", "--recursive=false", "--", ...targets], {
      encoding: "utf-8",
      stdio: ["pipe", "pipe", "ignore"],
      timeout: 20000, // a onepassword-templated target costs ~0.75s each
      env: SUBPROC_ENV,
    })
  } catch {
    debugLog("chezmoi status failed in drift check", { targets: targets.size })
    return none
  }
  const home = process.env.HOME ?? ""
  const drifted: string[] = []
  const driftedAbs = new Set<string>()
  for (const line of out.split("\n")) {
    if (!line.trim()) continue
    const rel = line.slice(3).trim()
    if (!rel) continue
    drifted.push(rel)
    driftedAbs.add(normalizePath(resolve(home, rel)))
  }
  // Prune live entries `chezmoi status` says are clean.
  state.liveTouched = state.liveTouched.filter((t) => driftedAbs.has(normalizePath(t)))
  const keepSources = new Set<string>()
  for (const [t, s] of targetToSource) if (driftedAbs.has(t)) keepSources.add(s)
  return { drifted: drifted.sort(), keepSources }
}

// pendingTouchedPaths: classify the session-touched rels into the work still
// outstanding — `dirty` (uncommitted working-tree changes, via git status
// --porcelain, incl. rename dests) and `unpushed` (committed but ahead of
// upstream, via unpushedRels). PRUNES a path from tracking only once it is BOTH
// clean in the working tree AND already pushed: the self-heal boundary moves
// from "committed" to "committed AND pushed", so the guard keeps nagging until
// the push lands. Mutates state in place.
function pendingTouchedPaths(
  state: SessionState,
  keep: Set<string> = new Set(),
): { dirty: string[]; unpushed: string[] } {
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
  // ...and only when its live target is not drifted (`keep`): pushed but never
  // applied is still unfinished work.
  for (const p of rels) {
    const abs = resolve(CHEZMOI_SOURCE_DIR, p)
    if (!dirty.has(p) && !unpushed.has(p) && !keep.has(abs)) {
      set.delete(abs)
      releaseClaim(abs)
    }
  }
  state.touchedPaths = [...set]

  return { dirty: [...dirty], unpushed: [...unpushed] }
}

// ---------------------------------------------------------------------------
// Complaint / continuation prompt builders (verbatim TEXT from source)
// ---------------------------------------------------------------------------

function uncommittedChezmoiComplaint(state: SessionState): string | undefined {
  // Report without the paths another session demonstrably wrote (see
  // foreignClaimed); they stay in state and are re-checked every time.
  const foreign = foreignClaimed(state)
  if (foreign.size > 0 && VERBOSE) debugLog("suppressed foreign-claimed paths", { count: foreign.size })
  state.touchedPaths = state.touchedPaths.filter((p) => !foreign.has(p))
  let drifted: string[], keepSources: Set<string>, dirty: string[], unpushed: string[]
  try {
    ;({ drifted, keepSources } = driftedTargets(state))
    ;({ dirty, unpushed } = pendingTouchedPaths(state, keepSources))
  } finally {
    state.touchedPaths = [...new Set([...state.touchedPaths, ...foreign])]
  }
  if (dirty.length === 0 && unpushed.length === 0 && drifted.length === 0) return undefined
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
  if (drifted.length > 0) {
    sections.push(
      `LIVE != SOURCE drift (\`chezmoi status\`, home-relative) on paths this session worked on:\n${fmt(drifted)}\n` +
        `For each one decide which side is right. If the SOURCE is right: ` +
        `\`chezmoi apply --force -- ~/<path> </dev/null\` (without --force and </dev/null it prompts and hangs the pty). ` +
        `If the LIVE file is right: fold it back into the source (\`chezmoi re-add ~/<path>\` for a plain file, ` +
        `or edit the template / modify_ script), then commit and push.`,
    )
  }
  return (
    `CHEZMOI-GUARD: chezmoi work from this session is unfinished (uncommitted, unpushed, or live!=source).\n` +
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
    `In the chezmoi source repo only add, commit, and non-force push are\n` +
    `permitted; anything that rewrites history or discards working-tree\n` +
    `changes is blocked by this guard. Never stage other agents' unrelated\n` +
    `dirty paths.`
  )
}

const GIT_HAZARD_MESSAGE =
  `[chezmoi-guard] bash command appears to run a destructive or\n` +
  `history-rewriting git operation in the chezmoi source repo.\n` +
  `\n` +
  `Only add, commit, and non-force push are permitted there. Anything that\n` +
  `rewrites history or discards working-tree changes (other agents may have\n` +
  `uncommitted work in this tree) is blocked. Stage only the files you\n` +
  `changed, commit, and push.`

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

// In-place forms of the stream editors. These are the ONLY forms under which
// sed/perl/ruby/awk operands count as write targets: `sed -n 1p file`,
// `awk '{print}' file`, `perl -ne ... file` are reads and must be allowed.
//   sed : -i / -I in a flag cluster (`-i`, `-i.bak`, `-E -i`, `-n -i.bak`,
//         `-i ''`), or --in-place
//   perl/ruby : an `i` ENDING a flag cluster (`-i`, `-pi`, `-i.bak`, `-ni -e`);
//               `-Mstrict`, `-Ilib` are not in-place (i must end the cluster)
//   awk : `-i inplace` (gawk)
// The tokens between the program name and the in-place flag must be OPTIONS
// (`-\S*`) only, so the pattern can never cross a script/operand/pipe boundary:
// `sed -n 1p ~/.zshrc | grep -i foo` and `awk '{print}' f | grep -i inplace`
// are reads. Identical regexes in the Claude hook and the opencode plugin.
const SED_INPLACE_RE = /(?:^|[\s|;&({`])sed\s+(?:-\S*\s+)*?(?:-[a-zA-Z]*[iI]|--in-place)/
const PERL_RUBY_INPLACE_RE = /(?:^|[\s|;&({`])(?:perl|ruby)\s+(?:-\S+\s+)*-[a-zA-Z]*i(?:\.\S*)?(?=$|\s)/
const AWK_INPLACE_RE = /(?:^|[\s|;&({`])awk\s+(?:-\S*\s+)*?-i\s+inplace/

function isInPlaceEditor(cmd: string): boolean {
  return SED_INPLACE_RE.test(cmd) || PERL_RUBY_INPLACE_RE.test(cmd) || AWK_INPLACE_RE.test(cmd)
}

const WRITE_PATTERNS: RegExp[] = [
  /(?:[0-9]?&?>>?[\|!]?|&>[\|!]?)\s*['"]?[~/$]/,
  /(?:^|[\s|;&({`])tee\b/,
  /(?:^|[\s|;&({`])cp\b/,
  /(?:^|[\s|;&({`])mv\b/,
  /(?:^|[\s|;&({`])ln\b/,
  /(?:^|[\s|;&({`])install\b/,
  /(?:^|[\s|;&({`])rsync\b/,
  SED_INPLACE_RE,
  PERL_RUBY_INPLACE_RE,
  AWK_INPLACE_RE,
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
    // Skip operands that are EMPTY after quote-stripping: BSD `sed -i ''`
    // passes '' as the backup suffix, and an empty path would resolve to the
    // cwd and (prefix-aware) match every managed file beneath it.
    const stripped = p?.replace(/^['"]|['"]$/g, "")
    if (stripped) out.push(stripped)
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
      // Stream editors mutate their operands ONLY in in-place mode. A plain
      // read (`sed -n 1p file`) contributes no targets; a `>` redirect on the
      // same segment is still picked up by redirectRe above.
      if (!isInPlaceEditor(m[0])) continue
      for (const p of parts) push(p)
      // In-place mode edits EVERY file operand, so also take every absolute /
      // ~ path token of the segment: the operand list above is cut short by a
      // `(` inside the script (`ruby -pi -e 'gsub(/a/,"b")' ~/.zshrc`). Only
      // tokens AFTER the editor, so an upstream `sed -n 1p ~/.zshrc |` is not
      // blamed.
      for (const p of pathsFromBashCommand(expanded.slice(m.index))) push(p)
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
// Hazard verbs, shared by GIT_HAZARD_RE and the `chezmoi git` form. Blocked:
//   reset / rebase / merge / restore  (any form)
//   stash                              (push/pop/drop/clear/apply… — NOT list/show)
//   switch -f / --force / --discard-changes
//   checkout file-restore forms        (see gitCheckoutRestoresFiles below; a
//                                      bare branch name like `checkout main`,
//                                      `checkout -b x`, `checkout HEAD~1` stay allowed)
//   clean with -f/-d/-x                (unless --dry-run / -n is also present)
//   commit --amend
//   push --force / --force-with-lease / -f / --mirror / +refspec
// Still allowed: add, commit, non-force push, status, diff, log, stash list/show.
const GIT_HAZARD_VERBS = String.raw`(?:reset|rebase|merge|restore)(?=$|[\s|;&)])|stash(?=$|[\s|;&)])(?!\s+(?:list|show)\b)|switch\b[^|;&]*\s(?:-f\b|--force\b|--discard-changes\b)|clean\b(?![^|;&]*(?:--dry-run|\s-[a-zA-Z]*n))[^|;&]*\s(?:-[a-zA-Z]*[fdx][a-zA-Z]*|--force)(?=$|[\s|;&)])|commit\b[^|;&]*\s--amend\b|push\b[^|;&]*(?:\s['"]?\+\S+['"]?|\s(?:--force(?:-with-lease)?|-\w*f\w*|--mirror\b))`
const GIT_HAZARD_RE = new RegExp(`${GIT_PREFIX}(?:${GIT_HAZARD_VERBS})`)
const GIT_CHECKOUT_RE = new RegExp(`${GIT_PREFIX}checkout\\b([^|;&]*)`, "g")
// `chezmoi git [--] <verb ...>` always runs in the source repo; the tail is
// re-tested with the same hazard set as a bare `git` (see bashHazardsChezmoiRepo).
const CHEZMOI_GIT_RE = /(?:^|[\s|;&(])chezmoi\s+git\b\s+(?:--\s+)?/g

// `git checkout` discards working-tree changes when given paths (`-- <path>`,
// `<tree-ish> -- <path>`, `.`, `-f`, `-p`, or a bare operand that looks like a
// path). A plain branch switch (`checkout main`, `checkout -b name`,
// `checkout HEAD~1`) stays allowed. When in doubt (operand contains `/` or a
// dotted suffix) we treat it as a path: conservative. Same walker as the
// Claude hook so the two guards converge.
function gitCheckoutRestoresFiles(cmd: string): boolean {
  let m: RegExpExecArray | null
  GIT_CHECKOUT_RE.lastIndex = 0
  while ((m = GIT_CHECKOUT_RE.exec(cmd)) !== null) {
    const toks = m[1].trim().split(/\s+/).filter(Boolean)
    let skipNext = false
    for (const t of toks) {
      if (skipNext) {
        skipNext = false
        continue
      }
      if (t === "--" || t === "-f" || t === "--force" || t === "-p" || t === "--patch") return true
      if (t === "-b" || t === "-B" || t === "--orphan" || t === "-t" || t === "--track") {
        skipNext = true
        continue
      }
      if (t.startsWith("-")) continue
      if (t === "." || t.startsWith("./") || t.startsWith("~") || t.includes("/") || /\.\w+$/.test(t)) return true
    }
  }
  return false
}

function gitHasHazard(cmd: string): boolean {
  return GIT_HAZARD_RE.test(cmd) || gitCheckoutRestoresFiles(cmd)
}

function resolveAgainstWorkdir(raw: string, workdir?: string): string {
  if (!workdir || raw.startsWith("/") || raw.startsWith("~")) return raw
  return resolve(normalizePath(workdir), raw)
}

function bashHazardsChezmoiRepo(cmd: string, workdir?: string): boolean {
  const expanded = expandHomeVars(cmd)
  let cm: RegExpExecArray | null
  CHEZMOI_GIT_RE.lastIndex = 0
  while ((cm = CHEZMOI_GIT_RE.exec(expanded)) !== null) {
    if (gitHasHazard("git " + expanded.slice(cm.index + cm[0].length))) return true
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

function isApplyPatch(input: any, ti: any): boolean {
  return (
    input.tool_name === "apply_patch" ||
    (Array.isArray(ti.command) && ti.command[0] === "apply_patch")
  )
}

// extractPatchPaths: collect candidate patch text from several possible shapes,
// then run the apply_patch grammar over it. Shape-tolerant.
function extractPatchPaths(ti: any): string[] {
  if (!ti) return []
  const candidates: string[] = []
  if (Array.isArray(ti.command) && ti.command[0] === "apply_patch") {
    candidates.push(ti.command.slice(1).join("\n"))
  } else if (typeof ti.command === "string") {
    candidates.push(ti.command)
  }
  if (typeof ti.input === "string") candidates.push(ti.input)
  if (typeof ti.patch === "string") candidates.push(ti.patch)
  if (typeof ti.patchText === "string") candidates.push(ti.patchText)
  const text = candidates.filter(Boolean).join("\n")
  if (!text) return []
  const re = /\*\*\* (?:Add|Update|Delete) File: (.+)|\*\*\* Move to: (.+)/g
  const out: string[] = []
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) out.push((m[1] ?? m[2] ?? "").trim())
  return out.filter(Boolean)
}

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
  traceLog("pretool", { session_id: input.session_id, name })

  // STEP A: apply_patch (first-class OR via shell argv). EXACT managed match.
  if (isApplyPatch(input, ti)) {
    const paths = extractPatchPaths(ti)
    const managed = loadManaged({ coldSpawnOnly: true })
    for (const raw of paths) {
      const p = normalizePath(raw)
      if (managedHas(p, managed)) {
        debugLog("deny apply_patch managed", { session_id: input.session_id, path: p })
        denyPreToolUse(managedPathError(p))
      }
    }
    // Do NOT return early — fall through so an apply_patch-via-shell whose
    // command string ALSO contains git/write hazards is scanned in B/C.
  }

  const { cmd, workdir } = extractShell(ti)
  // Start of this call's attribution window. Written before any deny below
  // (a denied call never gets a PostToolUse; GC sweeps its stamp).
  if (cmd && !isApplyPatch(input, ti)) stampToolStart(input)
  // codex's shell tool documents `workdir` as "defaults to the turn cwd", and
  // every PreToolUse/PostToolUse payload carries a top-level `cwd` (required in
  // the embedded schema). Resolve exactly in that order.
  const resolvedWorkdir: string | undefined =
    workdir || (typeof input.cwd === "string" && input.cwd ? input.cwd : undefined)

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
  traceLog("posttool", { session_id: sid, name })
  const applyPatch = isApplyPatch(input, ti)
  const startedAt = applyPatch ? undefined : takeToolStart(input)

  withSessionLock(sid, (state) => {
    // STEP remember (always; no exit_code skip — git status self-heals).
    let shellCmd = ""
    if (applyPatch) {
      claimExactWrites(state, extractPatchPaths(ti))
    } else {
      const { cmd, workdir: tiWorkdir } = extractShell(ti)
      shellCmd = cmd
      const workdir: string | undefined =
        tiWorkdir || (typeof input.cwd === "string" && input.cwd ? input.cwd : undefined)
      if (cmd) {
        const paths: string[] = []
        const targets: string[] = []
        for (const seg of splitBashSegments(cmd)) {
          const targetPaths = pathsFromBashWriteTargets(seg)
          if (
            !bashHasWriteIntent(seg) &&
            !(workdir && isInChezmoiSource(workdir) && targetPaths.length > 0)
          ) {
            continue
          }
          paths.push(...pathsFromBashCommand(seg), ...targetPaths)
          targets.push(...targetPaths)
        }
        const resolveAll = (ps: string[]) =>
          workdir && isInChezmoiSource(workdir) ? ps.map((p) => resolveAgainstWorkdir(p, workdir)) : ps
        rememberSourceWrites(state, resolveAll(paths))
        if (!shellCallFailed(input)) claimExactWrites(state, resolveAll(targets))
      }
    }
    // STEP attribute by evidence: what actually changed on disk while this
    // shell call ran — or, while something we started may still be running
    // (lingerUntil), since the previous hook event (source tree, source
    // commits, managed live targets). Catches every writer the text
    // heuristics above cannot see. Reads the managed cache only (PostToolUse
    // never spawns chezmoi).
    const now = Date.now()
    const starts: number[] = []
    if (shouldAttribute(classifiableCommand(ti, shellCmd)))
      starts.push(startedAt ?? Math.max(state.lastSeenAt, now - UNSTAMPED_WINDOW_MS))
    if (state.lingerUntil > 0 && state.lastSeenAt > 0) starts.push(state.lastSeenAt)
    if (starts.length > 0) attributeSessionWrites(state, Math.min(...starts), readManagedCache().paths)
    if (state.lingerUntil > 0 && now > state.lingerUntil) state.lingerUntil = 0 // final sweep done
    if (!applyPatch && outlivesCall(input, ti, shellCmd)) state.lingerUntil = now + LINGER_MS
    state.lastSeenAt = now
    // STEP recompute: prune + persist via the lock writer.
    pendingTouchedPaths(state)
    // DO NOT touch continuationFiredAt/continuationCount here (Stop backstop).
  })

  process.exit(0) // EMPTY stdout
}

function handleUserPromptSubmit(input: any): void {
  const sid = input.session_id
  traceLog("userpromptsubmit", { session_id: sid })
  refreshManagedOffHotPath() // once per turn; ~150ms at most every 300s
  let complaint: string | undefined
  withSessionLock(sid, (state) => {
    sweepLingering(state)
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

  refreshManagedOffHotPath()

  let prompt: string | undefined
  let block = false
  let lockedOk = true
  try {
    withSessionLock(sid, (state) => {
      sweepLingering(state)
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
          } else if (entry.endsWith(".calls")) {
            // Start stamps whose PostToolUse never came (denied / crashed
            // calls). Nothing legitimately runs longer than a day.
            for (const stamp of readdirSync(full)) {
              try {
                if (now - statSync(`${full}/${stamp}`).mtimeMs > CLAIM_TTL_MS) unlinkSync(`${full}/${stamp}`)
              } catch {
                /* ignore */
              }
            }
            if (now - st.mtimeMs > sessionMaxAge) rmdirSync(full) // throws if non-empty: fine
          }
        } catch {
          /* ignore */
        }
      }
    }
    // Expired exact claims (readClaimOwner already ignores them; this only
    // keeps the shared dir small). Shared with the codex/opencode guards.
    if (existsSync(CLAIMS_DIR)) {
      for (const entry of readdirSync(CLAIMS_DIR)) {
        const full = `${CLAIMS_DIR}/${entry}`
        try {
          if (now - statSync(full).mtimeMs > CLAIM_TTL_MS) unlinkSync(full)
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
  rotateLogIfLarge()

  if (CHEZMOI_BIN === "chezmoi" && !isExecutable("/opt/homebrew/bin/chezmoi")) {
    debugLog("chezmoi binary not at /opt/homebrew/bin/chezmoi; using PATH lookup")
  }

  const ev = input?.hook_event_name
  if (typeof input?.session_id === "string" && input.session_id) OWNER = CLAIM_OWNER(input.session_id)
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
      case "SubagentStop": // codex 0.148 emits it; only fires if registered in hooks.json
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
