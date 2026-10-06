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
// and refreshed on a 5-minute TTL. Once loaded, a stale cache is served
// as-is for the block decision and re-spawned in the BACKGROUND (never on
// the tool-call critical path); newly-tracked files become blocked within
// ~5 minutes of the next tool call. Force-refresh by restarting opencode.
//
// Log: ~/.local/share/opencode/chezmoi-guard.log — rotated to `.1` at 1 MB.
// Only decisions (deny), continuations, refreshes and errors are logged;
// set CHEZMOI_GUARD_DEBUG=1 for per-tool-call trace lines.

import type { Plugin } from "@opencode-ai/plugin"
import { execFile, execFileSync } from "node:child_process"
import { appendFileSync, lstatSync, mkdirSync, readFileSync, realpathSync, renameSync, statSync, unlinkSync, writeFileSync } from "node:fs"
import { createHash } from "node:crypto"
import { dirname, relative, resolve } from "node:path"

const TTL_MS = 5 * 60 * 1000
const COLD_TTL_MS = 15_000 // cold-start retry window
const MAX_CONTINUATIONS = 3
const CONTINUATION_WINDOW_MS = 2 * 60 * 1000
const LOG_ROTATE_BYTES = 1_000_000
const TRACE = process.env.CHEZMOI_GUARD_DEBUG === "1"
let managed = new Set<string>()
let loaded = false
let lastLoad = 0
let refreshing = false

// Subprocess env. opencode may be launched from a GUI/launchd context whose
// PATH lacks /opt/homebrew/bin, and `chezmoi` is a shell FUNCTION wrapper in
// the interactive shell — so pin the real binary (PATH lookup only as a
// fallback, and log which one is in use) and the system git.
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

const CHEZMOI_BIN = isExecutable("/opt/homebrew/bin/chezmoi") ? "/opt/homebrew/bin/chezmoi" : "chezmoi"
const CHEZMOI_MANAGED_ARGS = ["managed", "--include=files", "--path-style", "absolute"]

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

function applyManagedOutput(out: string, mode: "sync" | "async"): void {
  managed = new Set(
    out
      .trim()
      .split("\n")
      .filter(Boolean)
      .map(normalizePath),
  )
  loaded = true
  debugLog("managed refreshed", { mode, size: managed.size, bin: CHEZMOI_BIN, loadedAt: new Date().toISOString() })
}

function refresh(): void {
  // TTL gating with two regimes:
  //   - Steady-state (loaded): throttle BOTH success and failure for
  //     TTL_MS. A hung/erroring chezmoi must not pay 3s per blocked tool
  //     call. The time-only gate makes that throttle actually work — an
  //     earlier `loaded && timeElapsed` form let failures retry every
  //     call because `loaded` stays false on error.
  //     The refresh itself is ASYNC: the current call is decided against
  //     the stale set (never fail-open, since the stale set is a superset
  //     of "known managed"), and the re-spawn lands before a later call.
  //   - Cold-start (not loaded): use a much shorter retry window (15s)
  //     so a chezmoi that's transiently unavailable at plugin load
  //     doesn't fail-open the entire 5min steady-state TTL. While the
  //     cache is empty, every Edit/Write tool would slip past silently
  //     because `managed.has(p)` is always false on an empty Set — so
  //     this path stays SYNCHRONOUS (the only time we pay the spawn on
  //     the critical path).
  const ttl = loaded ? TTL_MS : COLD_TTL_MS
  if (Date.now() - lastLoad < ttl) return
  lastLoad = Date.now()
  if (loaded) {
    if (refreshing) return
    refreshing = true
    execFile(
      CHEZMOI_BIN,
      CHEZMOI_MANAGED_ARGS,
      { encoding: "utf-8", env: SUBPROC_ENV, timeout: 3000 },
      (err, out) => {
        refreshing = false
        if (err) {
          // Stale cache is better than no cache (steady-state).
          debugLog("chezmoi managed failed", { mode: "async", bin: CHEZMOI_BIN, error: String(err), size: managed.size })
          return
        }
        applyManagedOutput(out, "async")
      },
    )
    return
  }
  try {
    const out = execFileSync(CHEZMOI_BIN, CHEZMOI_MANAGED_ARGS, {
      encoding: "utf-8",
      stdio: ["pipe", "pipe", "ignore"],
      env: SUBPROC_ENV,
      timeout: 3000,
    })
    applyManagedOutput(out, "sync")
  } catch (err) {
    // Cold-start failure leaves the cache EMPTY (fail-open) for one
    // short-TTL window — say so loudly, this is the one state in which
    // the guard silently protects nothing.
    debugLog("chezmoi managed failed", { mode: "cold", bin: CHEZMOI_BIN, error: String(err), size: managed.size })
  }
}

const BLOCKED_TOOLS = new Set(["edit", "write", "apply_patch", "multiedit"])

const CHEZMOI_SOURCE_DIR = normalizePath("~/.local/share/chezmoi")
const LOG_FILE = normalizePath("~/.local/share/opencode/chezmoi-guard.log")

// Single-slot rotation: once the log passes LOG_ROTATE_BYTES it is renamed
// to `.1` (overwriting the previous `.1`) before the append. statSync is a
// few microseconds; every failure is ignored.
function rotateLogIfLarge(): void {
  try {
    if (statSync(LOG_FILE).size > LOG_ROTATE_BYTES) renameSync(LOG_FILE, LOG_FILE + ".1")
  } catch {
    // Missing log or rename failure: nothing to rotate.
  }
}

function debugLog(message: string, data?: Record<string, unknown>): void {
  try {
    mkdirSync(resolve(LOG_FILE, ".."), { recursive: true })
    rotateLogIfLarge()
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
  // Auto-continue bookkeeping (mirrors the Claude/Codex hooks' Stop backstop):
  // `continuationPendingSince` is set when a continuation is submitted and is
  // ONLY cleared by the idle handler (never by tool calls — a continuation
  // turn always runs tools, so clearing there would defeat the window), and
  // `continuationCount` caps the retries at MAX_CONTINUATIONS per session.
  continuationPendingSince?: number
  continuationCount: number
  // Evidence-based attribution (see attributeSessionWrites): the end of the
  // last tool.execute.after we ran, the source repo HEAD we last saw, and
  // managed LIVE targets whose mtime landed inside one of this session's
  // tool-call windows.
  lastSeenAt: number
  headSha: string
  liveTouched: Set<string>
  // The subset of touchedPaths this session wrote BY NAME (not by time
  // evidence). Only these are published to the claims ledger, and only the
  // others can be suppressed in favour of another session's claim.
  exactPaths: Set<string>
  // While > now, a bash command this session backgrounded (`&`, nohup, …)
  // may still be writing: every tool call / system.transform / idle attributes
  // [lastSeenAt, now] continuously, not just bash run time. See outlivesCall.
  lingerUntil: number
}

const sessionState = new Map<string, SessionChezmoiState>()
// Start time of each in-flight bash call (tool.execute.before → .after), so a
// call's attribution window covers only the time it RAN.
const callStartedAt = new Map<string, number>()

function stateForSession(sessionID: string): SessionChezmoiState {
  let state = sessionState.get(sessionID)
  if (!state) {
    state = {
      touchedPaths: new Set(),
      continuationCount: 0,
      lastSeenAt: 0,
      headSha: "",
      liveTouched: new Set(),
      exactPaths: new Set(),
      lingerUntil: 0,
    }
    sessionState.set(sessionID, state)
  }
  return state
}

// ---------------------------------------------------------------------------
// Exact-claims ledger SHARED with the Claude Code and codex guards: one file
// per chezmoi source path a session wrote BY NAME (edit/write/apply_patch, or a
// bash write the text heuristics resolved). Time-based evidence never outranks
// another session's exact claim — see foreignClaimed.
//   CLAIMS_DIR/<sha256(path)[:32]>.json = { path, owner, at }   last writer wins
// ---------------------------------------------------------------------------

const CLAIMS_DIR = "/Users/mackhaymond/.local/state/chezmoi-guard/claims"
const CLAIM_TTL_MS = 24 * 60 * 60 * 1000
const CLAIM_OWNER = (rootID: string) => `opencode:${rootID}`
// The pty read-only classifier (the Claude Code auto/plan-mode hook). A bash
// command it calls read-only cannot have written anything, so it gets no
// evidence attribution. Loaded lazily; a load failure means "not read-only".
const READ_ONLY_CLASSIFIER = "/Users/mackhaymond/.claude/hooks/pty-read-only.ts"
// Fallback window when tool.execute.after finds no start time for its callID.
const UNSTAMPED_WINDOW_MS = 600_000
const LINGER_MS = 60 * 60 * 1000
const CLAIM_MTIME_SLACK_MS = 2000

function claimFile(p: string): string {
  return `${CLAIMS_DIR}/${createHash("sha256").update(p).digest("hex").slice(0, 32)}.json`
}

function writeClaim(p: string, owner: string): void {
  try {
    mkdirSync(CLAIMS_DIR, { recursive: true })
    const f = claimFile(p)
    const tmp = `${f}.${process.pid}.${Date.now()}.tmp`
    writeFileSync(tmp, JSON.stringify({ path: p, owner, at: Date.now() }))
    renameSync(tmp, f)
  } catch {
    /* best-effort: a lost claim only means a possible extra nag elsewhere */
  }
}

// A claim says "owner wrote this file at `at`" and vouches only for that
// write: once the file's mtime moves past `at`, someone wrote it again.
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
function releaseClaim(p: string, owner: string): void {
  try {
    if (readClaim(p)?.owner === owner) unlinkSync(claimFile(p))
  } catch {
    /* already gone */
  }
}

// Paths this session holds only by TIME evidence whose current content
// another session wrote BY NAME (fresh claim, file not written since). They
// are suppressed from the report, NOT deleted from state — if the claim
// lapses or the file is written again, they report again. Deleted files are
// never suppressed (no mtime to compare).
function foreignClaimed(sessionID: string): Set<string> {
  const out = new Set<string>()
  const state = sessionState.get(sessionID)
  if (!state) return out
  const owner = CLAIM_OWNER(sessionID)
  for (const p of state.touchedPaths) {
    if (state.exactPaths.has(p)) continue
    const c = readClaim(p)
    if (!c || c.owner === owner) continue
    try {
      if (lstatSync(p).mtimeMs <= c.at + CLAIM_MTIME_SLACK_MS) out.add(p)
    } catch {
      /* deleted: keep reporting */
    }
  }
  return out
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

// Session parentage, learned for free from `session.created`/`session.updated`
// events (their payload is the full Session, which carries `parentID`; the
// `session.idle` payload does NOT). Subagent sessions are children: their
// source writes are attributed to the ROOT session, which is the one that
// must commit and push, and the idle continuation is never driven into a
// child (that would yank the TUI onto the subagent's session).
const sessionParent = new Map<string, string | undefined>()

function rootSessionID(sessionID: string): string {
  let id = sessionID
  for (let hops = 0; hops < 32; hops++) {
    const parent = sessionParent.get(id)
    if (!parent) return id
    id = parent
  }
  return id
}

function isInChezmoiSource(p: string): boolean {
  const normalized = normalizePath(p)
  return normalized === CHEZMOI_SOURCE_DIR || normalized.startsWith(CHEZMOI_SOURCE_DIR + "/")
}

function sourceRelativePath(p: string): string {
  return relative(CHEZMOI_SOURCE_DIR, normalizePath(p))
}

// `exact`: the paths were certainly WRITTEN by name (an edit-class tool's
// path, or a bash segment's parsed write target) — they also become exact
// claims. The all-tokens bash heuristic (reads included) is tracked, not claimed.
function rememberSourceWrites(sessionID: string, rawPaths: string[], exact: boolean): void {
  // Attribute to the root session (see sessionParent) so a subagent's edits
  // nag the parent that will actually finish the task.
  const root = rootSessionID(sessionID)
  const state = stateForSession(root)
  for (const raw of rawPaths) {
    const p = normalizePath(raw)
    if (isInChezmoiSource(p)) {
      state.touchedPaths.add(p)
      if (exact) {
        state.exactPaths.add(p)
        writeClaim(p, CLAIM_OWNER(root))
      }
      debugLog("remembered source write", { sessionID: root, via: root === sessionID ? undefined : sessionID, path: sourceRelativePath(p) })
    }
  }
}

// Move a child's touchedPaths (recorded before its parentage was known) onto
// the root session.
function migrateTouchedPathsToRoot(sessionID: string): void {
  const root = rootSessionID(sessionID)
  if (root === sessionID) return
  const child = sessionState.get(sessionID)
  if (!child || child.touchedPaths.size === 0) return
  const rootState = stateForSession(root)
  for (const p of child.touchedPaths) rootState.touchedPaths.add(p)
  for (const p of child.exactPaths) {
    rootState.exactPaths.add(p)
    writeClaim(p, CLAIM_OWNER(root))
  }
  debugLog("migrated child touched paths to root", { sessionID, root, count: child.touchedPaths.size })
  child.touchedPaths.clear()
  child.exactPaths.clear()
}

// unpushedRels: of the given session-touched rels, return the subset that
// appears in commits ahead of the upstream (@{u}..HEAD) — committed but not yet
// pushed. The pathspec restricts the log to those rels AND we intersect with the
// rels set, so a file riding along in someone else's commit is never blamed.
// FAIL-QUIET: no upstream / detached HEAD / any git error -> empty set, so a
// repo without a remote behaves exactly like the old commit-only guard.
function unpushedRels(rels: string[]): Set<string> {
  const out = new Set<string>()
  if (rels.length === 0) return out
  const relsSet = new Set(rels)
  try {
    const raw = execFileSync(
      GIT_BIN,
      ["-C", CHEZMOI_SOURCE_DIR, "log", "@{u}..HEAD", "--name-only", "--pretty=format:", "--", ...rels],
      { encoding: "utf-8", stdio: ["pipe", "pipe", "ignore"], env: SUBPROC_ENV, timeout: 3000 },
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
// Evidence-based attribution (mirrors the Claude/Codex hooks).
//
// The bash-text heuristics only SEE writes spelled as redirects / cp / mv /
// sed -i. A `python3 - <<EOF ... write_text()` heredoc, `chezmoi edit`, an
// editor, or a relative path behind `cd $(chezmoi source-path) &&` is invisible
// to them. So we also look at what actually changed on disk while something
// of ours could have been writing: a write-capable bash call (not one the
// read-only classifier accepts) for the time it RAN, [startedAt − slack, now]
// (startedAt from tool.execute.before; without one the last
// UNSTAMPED_WINDOW_MS, never past the previous call) — and, while a command
// we backgrounded may still be running (lingerUntil), continuously since the
// previous event: (a) source working-tree paths by mtime, (b) source commits
// made inside the window, (c) managed LIVE targets by mtime → liveTouched.
// Edit-class tools never time-attribute: they write exactly their named paths.
// (Until 2026-10-06 the window was always the continuous one, so it covered
// think-time, idle time between turns and read-only commands, and sessions got
// blamed for whatever other agents wrote meanwhile.)
// Attribution is still by time, so a concurrent agent writing the same repo
// inside our window is misattributed — unless it wrote the path by name and
// nobody wrote it since, in which case its claim suppresses it from our report
// (foreignClaimed). One extra nag, accepted over a silent miss.
// ---------------------------------------------------------------------------

const ATTRIB_SLACK_MS = 2000

// Something that can matter for chezmoi: names chezmoi or its source tree,
// opens an editor, or names a managed live path.
function chezmoiRelevant(text: string): boolean {
  if (/\bchezmoi\b/.test(text) || text.includes(CHEZMOI_SOURCE_DIR) || text.includes("~/.local/share/chezmoi")) return true
  if (/(^|[\s;&|("'`])(n?vim?|vi|nano|emacs|hx|micro|code|subl)(\s|$|["'])/.test(text)) return true
  return pathsFromBashCommand(text).some((p) => touchesManagedPath(normalizePath(p)))
}

// A leading VAR=value makes any command suspect (it can point git/a pager at
// an arbitrary program), so it never counts as read-only.
function bashMayWrite(cmd: string): boolean {
  if (!cmd.trim()) return false
  if (/(^|[;&|(\n]\s*)[A-Za-z_][A-Za-z0-9_]*=\S*\s+\S/.test(cmd)) return true
  return !isReadOnlyShellCommand(cmd)
}

// Quoted strings and heredoc bodies are data, not shell syntax: a `&` in a
// commit message or a sed replacement must not read as backgrounding.
function stripQuoted(cmd: string): string {
  return cmd
    .replace(/<<-?\s*(['"]?)(\w+)\1[^\n]*\n[\s\S]*?\n\s*\2\s*(?=\n|$)/g, " ")
    .replace(/'[^']*'/g, "''")
    .replace(/"(?:[^"\\]|\\.)*"/g, '""')
}

// Did this bash call leave a chezmoi-relevant process running past its
// return (`cmd &`, nohup, disown, setsid)? Then attribution stays continuous
// for LINGER_MS. (opencode's bash kills on timeout, and an aborted call gets
// no tool.execute.after at all — a known gap vs the Claude hook.)
function outlivesCall(cmd: string): boolean {
  if (!bashMayWrite(cmd)) return false
  const bare = stripQuoted(cmd)
  const backgrounded = /(^|[^&|>])&(?![&>])/.test(bare) || /(^|[\s;&|(])(nohup|disown|setsid)(\s|$)/.test(bare)
  return backgrounded && chezmoiRelevant(cmd)
}

// At system.transform / idle: while something we backgrounded may still be
// running, attribute what changed since the previous event.
function sweepLingering(sessionID: string): void {
  const state = sessionState.get(sessionID)
  if (!state || state.lingerUntil <= 0) return
  const now = Date.now()
  if (state.lastSeenAt > 0) attributeSessionWrites(sessionID, state.lastSeenAt)
  state.lastSeenAt = now
  if (now > state.lingerUntil) state.lingerUntil = 0
}

function attributeSessionWrites(sessionID: string, start: number): void {
  const root = rootSessionID(sessionID)
  const state = stateForSession(root)
  const now = Date.now()
  const since = start > 0 ? start - ATTRIB_SLACK_MS : 0
  const inWindow = (ms: number) => since > 0 && ms >= since && ms <= now + 1000
  let added = 0
  const opts = { encoding: "utf-8" as const, stdio: ["pipe", "pipe", "ignore"] as ["pipe", "pipe", "ignore"], env: SUBPROC_ENV, timeout: 3000 }

  // (a) working tree
  try {
    const out = execFileSync(GIT_BIN, ["-C", CHEZMOI_SOURCE_DIR, "status", "--porcelain", "-uall", "--no-renames"], opts)
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
      if (t !== undefined && inWindow(t) && !state.touchedPaths.has(abs)) {
        state.touchedPaths.add(abs)
        added++
      }
    }
  } catch {
    /* git unavailable: heuristics alone */
  }

  // (b) commits made inside the window. HEAD is only sampled on attributing
  // calls, so with no remembered HEAD (first write-capable call) or an
  // unreachable one, fall back to the newest commits — the --since filter is
  // what scopes them to this window either way.
  try {
    const head = execFileSync(GIT_BIN, ["-C", CHEZMOI_SOURCE_DIR, "rev-parse", "HEAD"], opts).trim()
    if (since > 0 && head && head !== state.headSha) {
      const gitLog = (range: string[]) =>
        execFileSync(
          GIT_BIN,
          ["-C", CHEZMOI_SOURCE_DIR, "log", ...range, "--name-only", "--pretty=format:", `--since=${new Date(since).toISOString()}`],
          opts,
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
        if (!state.touchedPaths.has(abs)) {
          state.touchedPaths.add(abs)
          added++
        }
      }
    }
    if (head) state.headSha = head
  } catch {
    /* old sha unreachable or git error: skip */
  }

  // (c) live managed targets written inside the window
  if (since > 0) {
    for (const p of managed) {
      try {
        if (inWindow(lstatSync(p).mtimeMs) && !state.liveTouched.has(p)) {
          state.liveTouched.add(p)
          added++
        }
      } catch {
        /* target missing: chezmoi status reports it if it matters */
      }
    }
  }
  if (added > 0) debugLog("attributed by evidence", { sessionID: root, added, since })
}

// Fresh `chezmoi managed` list (files, dirs, symlinks; absolute). NOT the TTL
// cache: a source file added seconds ago must count, and `chezmoi status`
// aborts on the first target it does not manage.
function currentManagedTargets(): Set<string> | undefined {
  try {
    const out = execFileSync(CHEZMOI_BIN, ["managed", "--include=files,dirs,symlinks", "--path-style=absolute"], {
      encoding: "utf-8",
      stdio: ["pipe", "pipe", "ignore"],
      env: SUBPROC_ENV,
      timeout: 5000,
    })
    return new Set(out.split("\n").map((s) => s.trim()).filter(Boolean).map(normalizePath))
  } catch {
    return undefined
  }
}

// driftedTargets: `chezmoi status` over (live targets this session wrote) ∪
// (targets of the source paths it touched); returns drifted home-relative
// paths plus the SOURCE paths behind them (kept tracked even once pushed).
// Prunes clean liveTouched entries. FAIL-QUIET on any chezmoi error.
function driftedTargets(sessionID: string): { drifted: string[]; keepSources: Set<string> } {
  const none = { drifted: [] as string[], keepSources: new Set<string>() }
  const state = sessionState.get(sessionID)
  if (!state) return none
  const sources = [...state.touchedPaths].filter((p) => isInChezmoiSource(p))
  if (sources.length === 0 && state.liveTouched.size === 0) return none

  const targetToSource = new Map<string, string>()
  if (sources.length > 0) {
    try {
      const out = execFileSync(CHEZMOI_BIN, ["target-path", ...sources], {
        encoding: "utf-8",
        stdio: ["pipe", "pipe", "ignore"],
        env: SUBPROC_ENV,
        timeout: 5000,
      })
      const lines = out.split("\n").map((s) => s.trim()).filter(Boolean)
      if (lines.length === sources.length) lines.forEach((t, i) => targetToSource.set(normalizePath(t), sources[i]))
    } catch {
      /* one bad path aborts the batch: live targets only */
    }
  }
  const managedNow = currentManagedTargets()
  if (!managedNow) return none
  const targets = new Set<string>()
  for (const t of targetToSource.keys()) if (managedNow.has(t)) targets.add(t)
  for (const t of state.liveTouched) {
    const n = normalizePath(t)
    if (managedNow.has(n)) targets.add(n)
  }
  if (targets.size === 0) {
    state.liveTouched.clear()
    return none
  }
  let out: string
  try {
    out = execFileSync(CHEZMOI_BIN, ["status", "--recursive=false", "--", ...targets], {
      encoding: "utf-8",
      stdio: ["pipe", "pipe", "ignore"],
      env: SUBPROC_ENV,
      timeout: 20000, // a onepassword-templated target costs ~0.75s each
    })
  } catch (err) {
    debugLog("chezmoi status failed in drift check", { sessionID, error: String(err) })
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
  for (const t of [...state.liveTouched]) if (!driftedAbs.has(normalizePath(t))) state.liveTouched.delete(t)
  const keepSources = new Set<string>()
  for (const [t, s] of targetToSource) if (driftedAbs.has(t)) keepSources.add(s)
  return { drifted: drifted.sort(), keepSources }
}

// pendingTouchedPaths: classify the session-touched rels into the work still
// outstanding — `dirty` (uncommitted working-tree changes) and `unpushed`
// (committed but ahead of upstream). Prunes a path from tracking only once it is
// BOTH clean in the working tree AND already pushed, so the self-heal boundary
// moves from "committed" to "committed AND pushed" — the guard keeps nagging
// until the push lands. Mutates the session's touchedPaths Set in place.
function pendingTouchedPaths(sessionID: string, keep: Set<string> = new Set()): { dirty: string[]; unpushed: string[] } {
  const state = sessionState.get(sessionID)
  if (!state || state.touchedPaths.size === 0) return { dirty: [], unpushed: [] }
  const rels = [...state.touchedPaths]
    .map(sourceRelativePath)
    .filter((p) => p && !p.startsWith(".."))
    .sort()
  if (rels.length === 0) return { dirty: [], unpushed: [] }

  const dirty = new Set<string>()
  try {
    const out = execFileSync(GIT_BIN, ["-C", CHEZMOI_SOURCE_DIR, "status", "--porcelain", "--", ...rels], {
      encoding: "utf-8",
      stdio: ["pipe", "pipe", "ignore"],
      env: SUBPROC_ENV,
      timeout: 3000,
    })
    for (const line of out.split("\n")) {
      if (!line.trim()) continue
      // Porcelain v1 is `XY path` or `XY old -> new`. For renames, track the
      // destination path because that is what remains uncommitted.
      const raw = line.slice(3)
      const renamed = raw.includes(" -> ") ? raw.split(" -> ").pop() : raw
      if (renamed) dirty.add(renamed)
    }
  } catch (err) {
    // If status fails, fail quiet rather than blame the agent for stale or
    // unverifiable state, and do NOT prune. The normal hard guards still run.
    debugLog("git status failed", { sessionID, error: String(err) })
    return { dirty: [], unpushed: [] }
  }

  const unpushed = unpushedRels(rels)

  for (const p of dirty) {
    const abs = resolve(CHEZMOI_SOURCE_DIR, p)
    if (isInChezmoiSource(abs)) state.touchedPaths.add(abs)
  }
  // Self-heal at the PUSH boundary: drop a path only when it is neither
  // uncommitted (working-tree dirty) nor committed-but-unpushed.
  // ...and only when its live target is not drifted (`keep`): pushed but never
  // applied is still unfinished work.
  for (const p of rels) {
    const abs = resolve(CHEZMOI_SOURCE_DIR, p)
    if (!dirty.has(p) && !unpushed.has(p) && !keep.has(abs)) {
      state.touchedPaths.delete(abs)
      releaseClaim(abs, CLAIM_OWNER(sessionID))
    }
  }
  // exactPaths ⊆ touchedPaths: a pruned (committed + pushed) path drops out of both.
  for (const p of [...state.exactPaths]) if (!state.touchedPaths.has(p)) state.exactPaths.delete(p)
  return { dirty: [...dirty], unpushed: [...unpushed] }
}

function uncommittedChezmoiComplaint(sessionID: string): string | undefined {
  sweepLingering(sessionID)
  // Report without the paths another session demonstrably wrote (see
  // foreignClaimed); they stay in state and are re-checked every time.
  const foreign = foreignClaimed(sessionID)
  const state = sessionState.get(sessionID)
  if (state) for (const p of foreign) state.touchedPaths.delete(p)
  let drifted: string[], keepSources: Set<string>, dirty: string[], unpushed: string[]
  try {
    ;({ drifted, keepSources } = driftedTargets(sessionID))
    ;({ dirty, unpushed } = pendingTouchedPaths(sessionID, keepSources))
  } finally {
    if (state) for (const p of foreign) state.touchedPaths.add(p)
  }
  if (dirty.length === 0 && unpushed.length === 0 && drifted.length === 0) return undefined
  const fmt = (paths: string[]) => {
    const shown = paths.slice(0, 12).map((p) => `- ${p}`).join("\n")
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
        `\`chezmoi apply --force -- ~/<path> </dev/null\` (without --force and </dev/null it prompts and hangs). ` +
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
    const re = /\*\*\* (?:Add|Update|Delete) File: (.+)|\*\*\* Move to: (.+)/g
    const out: string[] = []
    let m: RegExpExecArray | null
    while ((m = re.exec(args.patchText)) !== null) out.push((m[1] ?? m[2] ?? "").trim())
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
  // In-place sed/perl/ruby/awk forms are appended below (INPLACE_PATTERNS).
  /(?:^|[\s|;&({`])truncate\b/,
  /(?:^|[\s|;&({`])(?:rm|unlink)\b/,
  /(?:^|[\s|;&({`])dd\s+[^|;&]*\bof=/,
]

// In-place editor idioms. sed/perl/ruby/awk are READ tools unless they carry
// an in-place flag; only then do their file operands count as write targets
// (a bare `sed -n 1p ~/.zshrc` / `awk '{print}' file` / `perl -ne` must be
// allowed). Flag forms: `sed -i`/`-i.bak`/`-I`/`--in-place`; `perl`/`ruby`
// `-i`, `-i.bak` and clustered `-pi`/`-pi.bak` (the `i` must END the flag
// cluster, optionally followed by an attached backup suffix, so
// `-ne`/`-Mstrict`/`-Ilib` do not match); and `awk -i inplace` (gawk). An
// explicit `>` redirect targeting the managed path is caught separately by
// the redirect pattern above. Same three regexes as the Claude/Codex hooks.
const INPLACE_PATTERNS: RegExp[] = [
  // Intermediate tokens are restricted to OPTIONS (`-\S*`): a `[^\s]+` class
  // would skip across `|` and find the `-i` of a later pipeline stage
  // (`sed -n 1p ~/.zshrc | grep -i x`), since pipelines are one segment.
  /(?:^|[\s|;&({`])sed\s+(?:-\S*\s+)*?(?:-[a-zA-Z]*[iI]|--in-place)/,
  /(?:^|[\s|;&({`])(?:perl|ruby)\s+(?:-\S+\s+)*-[a-zA-Z]*i(?:\.\S*)?(?=$|\s)/,
  /(?:^|[\s|;&({`])awk\s+(?:-\S*\s+)*?-i\s+inplace/,
]
WRITE_PATTERNS.push(...INPLACE_PATTERNS)

function bashHasWriteIntent(cmd: string): boolean {
  for (const re of WRITE_PATTERNS) if (re.test(cmd)) return true
  return false
}

function bashHasInPlaceEdit(cmd: string): boolean {
  for (const re of INPLACE_PATTERNS) if (re.test(cmd)) return true
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
    // An empty operand (e.g. BSD `sed -i ''`) must not become a target: it
    // would normalize to the cwd and, from $HOME, match every managed file.
    const stripped = p?.replace(/^['"]|['"]$/g, "")
    if (stripped) out.push(stripped)
  }

  // Best-effort extraction for bare relative targets used by write/delete
  // commands. This intentionally over-approximates to catch common bypasses.
  const redirectRe = /(?:^|[\s|;&({`])(?:[0-9]*&?>>?|&>>?)(?:\|?|!)?\s*([^\s|;&()<>`]+|['"][^'"]+['"])/g
  let m: RegExpExecArray | null
  while ((m = redirectRe.exec(expanded)) !== null) push(m[1])

  const commandTargetRe = /(?:^|[\s|;&({`])(cp|mv|tee|truncate|rm|unlink|install|rsync|ln|sed|perl|ruby|awk)\b([^\n;|&()]*)/g
  while ((m = commandTargetRe.exec(expanded)) !== null) {
    const kind = m[1]
    const parts = (m[2].match(/(?:['"][^'"]+['"]|\S+)/g) ?? []).filter((p) => !p.startsWith("-"))
    if (kind === "cp") push(parts.at(-1))
    else if (kind === "mv") {
      // `mv old new` writes `new` and deletes/renames `old`; track both so
      // source-repo renames don't lose the deletion side of the change.
      for (const p of parts) push(p)
    }
    else if (kind === "tee") push(parts[0])
    else if (kind === "truncate") push(parts.at(-1))
    else if (kind === "install" || kind === "rsync" || kind === "ln") push(parts.at(-1))
    else if (kind === "sed" || kind === "perl" || kind === "ruby" || kind === "awk") {
      // Only an IN-PLACE invocation mutates its file operands; a read-only
      // `sed -n`/`awk '{print}'`/`perl -ne` contributes no write target (so
      // the segment falls through the write-intent gate and is allowed).
      // When in-place, extraction is best-effort and intentionally broad
      // after option filtering; the script/expression operand is an
      // acceptable false positive.
      if (!bashHasInPlaceEdit(m[0])) continue
      for (const p of parts) push(p)
      // In-place mode edits EVERY file operand, so also take every absolute /
      // ~ path token of the segment: the operand list above is cut short by a
      // `(` inside the script (`ruby -pi -e 'gsub(/a/,"b")' ~/.zshrc`). Only
      // tokens AFTER the editor, so an upstream `sed -n 1p ~/.zshrc |` is not
      // blamed.
      for (const p of pathsFromBashCommand(expanded.slice(m.index))) push(p)
    }
    else for (const p of parts) push(p)
  }

  const ddTargetRe = /(?:^|[\s|;&({`])dd\s+[^|;&]*\bof=([^\s|;&()<>`]+|['"][^'"]+['"])/g
  while ((m = ddTargetRe.exec(expanded)) !== null) push(m[1])

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
// Blocked operations (GIT_HAZARD_VERBS, shared with the `chezmoi git` form):
//   - `reset`, `rebase`, `merge` (any form)
//   - `restore` (any form) and the file-restore forms of `checkout`:
//     `checkout -- <path>`, `checkout -f`, `checkout -p`, `checkout .`,
//     `checkout <path/with/slash>`, `checkout <dotted.name>`,
//     `checkout <treeish> -- <path>`, `checkout <treeish> .`
//     (plain branch switching `checkout main` / `checkout -b x` /
//     `checkout HEAD~1` stays allowed; a branch name containing `/` is an
//     accepted false positive) — see gitCheckoutRestoresFiles
//   - `switch -f` / `--force` / `--discard-changes` (bare `switch <branch>`
//     and `switch -c x` stay allowed)
//   - `clean` with -f/-d/-x (a `-n`/`--dry-run` clean is allowed, incl. `-nd`)
//   - `stash` push/pop/drop/clear/apply (NOT `stash list` / `stash show`)
//   - `commit --amend`
//   - force pushes: `--force`, `--force-with-lease`, `-f`, `--mirror`, `+ref`
// Everything else — add, commit, non-force push, status, diff, log, branch — is
// allowed. The repo target may be expressed with raw `git -C <chezmoi-src>`,
// `git --git-dir=<chezmoi-src>/.git`, `chezmoi git -- ...`, cwd-changing
// shell (`cd <chezmoi-src> && git reset`), or the bash tool's `workdir` arg.
// Same hazard set and checkout walker as the Claude/Codex hooks.
const GIT_PREFIX = String.raw`(?:^|[\s|;&(])git\s+(?:(?:-[cC]\s*\S+|-c\s+\S+|--(?:git-dir|work-tree)(?:=|\s+)\S+|--(?:no-pager|paginate|bare))\s+)*`
const GIT_HAZARD_VERBS =
  String.raw`(?:reset|rebase|merge|restore)(?=$|[\s|;&)])` +
  // Anchored to the stash SUBCOMMAND token so a message like `-m 'list of
  // things'` cannot leak into the allowlist.
  String.raw`|stash(?=$|[\s|;&)])(?!\s+(?:list|show)\b)` +
  String.raw`|switch\b[^|;&]*\s(?:-f\b|--force\b|--discard-changes\b)` +
  String.raw`|clean\b(?![^|;&]*(?:--dry-run|\s-\w*n))[^|;&]*\s(?:-\w*[fdx]\w*|--force)(?=$|[\s|;&)])` +
  String.raw`|commit\b[^|;&]*\s--amend\b` +
  String.raw`|push\b[^|;&]*(?:\s['"]?\+\S+['"]?|\s(?:--force(?:-with-lease)?|-\w*f\w*|--mirror\b))`
const GIT_HAZARD_RE = new RegExp(`${GIT_PREFIX}(?:${GIT_HAZARD_VERBS})`)
const GIT_CHECKOUT_RE = new RegExp(`${GIT_PREFIX}checkout\\b([^|;&]*)`, "g")
// `chezmoi git [--] <verb ...>` always runs in the source repo; the tail is
// re-tested with the same hazard set as a bare `git` (see bashHazardsChezmoiRepo).
const CHEZMOI_GIT_RE = /(?:^|[\s|;&(])chezmoi\s+git\b\s+(?:--\s+)?/g

// `git checkout` discards working-tree changes when given paths (`-- <path>`,
// `<tree-ish> -- <path>`, `.`, `-f`, `-p`, or a bare operand that looks like a
// path). A plain branch switch (`checkout main`, `checkout -b name`,
// `checkout HEAD~1`) stays allowed. When in doubt (operand contains `/` or a
// dotted suffix) we treat it as a path: conservative.
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

// Positive statement of the rule, shared by both deny messages so an agent is
// never handed a blocklist to route around.
const GIT_ALLOWED_RULE =
  `In the chezmoi source repo only \`git add\`, \`git commit\` (no --amend),\n` +
  `non-force \`git push\`, and read-only git (status/diff/log/stash list) are\n` +
  `permitted. This guard blocks reset, rebase, merge, restore, checkout of\n` +
  `paths, switch -f/--discard-changes, clean -f/-d/-x, stash, --amend and\n` +
  `force-push.`

function gitHasHazard(cmd: string): boolean {
  return GIT_HAZARD_RE.test(cmd) || gitCheckoutRestoresFiles(cmd)
}

function resolveAgainstWorkdir(raw: string, workdir?: string): string {
  if (!workdir || raw.startsWith("/") || raw.startsWith("~")) return raw
  return resolve(normalizePath(workdir), raw)
}

function touchesManagedPath(p: string): boolean {
  if (managed.has(p)) return true
  for (const managedPath of managed) {
    if (managedPath.startsWith(p + "/")) return true
  }
  return false
}

function bashHazardsChezmoiRepo(cmd: string, workdir?: string): boolean {
  const expanded = expandHomeVars(cmd)
  let cm: RegExpExecArray | null
  CHEZMOI_GIT_RE.lastIndex = 0
  while ((cm = CHEZMOI_GIT_RE.exec(expanded)) !== null) {
    if (gitHasHazard("git " + expanded.slice(cm.index + cm[0].length))) return true
  }
  // Pattern A1: explicit `git -C <chezmoi-src>` + write-class git verb.
  // `-C\s*` (NOT `\s+`) accepts both `-C /path` AND the glued form `-C/path`
  // — git accepts both per its short-flag conventions, and the glued form
  // would otherwise sneak past a strict `-C\s+` matcher.
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
      `  2. Inspect git status/diff/log, stage only intended files, commit, and\n` +
      `     push automatically.\n` +
      `\n` +
      GIT_ALLOWED_RULE,
  )
}

export const ChezmoiGuard: Plugin = async ({ client, directory }) => {
  refresh()
  debugLog("initialized", { source: CHEZMOI_SOURCE_DIR, directory, bin: CHEZMOI_BIN, managed: managed.size })

  // Resolve a session's parent, from the event-fed cache first and the server
  // as a fallback (one request per unknown session; result cached).
  async function parentOf(sessionID: string): Promise<string | undefined> {
    if (sessionParent.has(sessionID)) return sessionParent.get(sessionID)
    try {
      const res = await client.session.get({ path: { id: sessionID } })
      const parent = res.data?.parentID || undefined
      sessionParent.set(sessionID, parent)
      return parent
    } catch (err) {
      debugLog("session lookup failed", { sessionID, error: String(err) })
      return undefined
    }
  }

  return {
    event: async ({ event }) => {
      if (event.type === "session.created" || event.type === "session.updated") {
        const info = event.properties.info
        sessionParent.set(info.id, info.parentID || undefined)
        return
      }
      if (event.type !== "session.idle") return
      const sessionID = event.properties.sessionID
      // Subagent turn ending: never drive a continuation into a child session
      // (it would select the child in the TUI and splice text into the user's
      // prompt). Hand its touched paths to the root, whose own idle will nag.
      if (await parentOf(sessionID)) {
        migrateTouchedPathsToRoot(sessionID)
        debugLog("idle skipped: subagent session", { sessionID, root: rootSessionID(sessionID) })
        return
      }
      const state = stateForSession(sessionID)
      if (state.continuationPendingSince !== undefined) {
        const age = Date.now() - state.continuationPendingSince
        if (age < CONTINUATION_WINDOW_MS) {
          debugLog("idle skipped: continuation already pending", { sessionID, age })
          return
        }
        debugLog("idle retrying stale continuation", { sessionID, age })
        state.continuationPendingSince = undefined
      }
      const prompt = uncommittedChezmoiContinuationPrompt(sessionID)
      if (!prompt) {
        if (state.continuationCount > 0 || state.continuationPendingSince !== undefined) {
          debugLog("idle clean: continuation state reset", { sessionID, continuations: state.continuationCount })
        } else if (TRACE) {
          debugLog("idle clean", { sessionID })
        }
        state.continuationCount = 0
        state.continuationPendingSince = undefined
        return
      }
      if (state.continuationCount >= MAX_CONTINUATIONS) {
        // Backstop: something (a rejected push, a hazard-blocked fix) keeps
        // the session dirty. Let it stop rather than loop a model turn per
        // idle forever; the system.transform reminder still covers the next
        // real turn.
        debugLog("idle dirty: continuation cap reached, allowing stop", { sessionID, continuations: state.continuationCount })
        return
      }
      state.continuationCount += 1
      state.continuationPendingSince = Date.now()
      debugLog("idle dirty: prompting continuation", { sessionID, attempt: state.continuationCount })
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
        debugLog("submitted continuation through tui", { sessionID, attempt: state.continuationCount })
      } catch (err) {
        state.continuationPendingSince = undefined
        debugLog("tui continuation submit failed", { sessionID, error: String(err) })
      }
    },
    "experimental.chat.system.transform": async (input, output) => {
      if (!input.sessionID) return
      // Root-keyed: a subagent's writes live on the root's state (see
      // rememberSourceWrites), and either session may do the commit.
      const complaint = uncommittedChezmoiComplaint(rootSessionID(input.sessionID))
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
        if (TRACE) debugLog("pretool", { sessionID: input.sessionID, tool: input.tool, paths })
        for (const raw of paths) {
          // Relative operands (common for apply_patch) are what the tool will
          // resolve against the session directory — the plugin instance is
          // per-directory, so `directory` is that base, not process.cwd().
          const p = normalizePath(resolveAgainstWorkdir(raw, directory))
          if (managed.has(p)) {
            debugLog("deny managed path", { sessionID: input.sessionID, tool: input.tool, path: p })
            throw managedPathError(p)
          }
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
        if (TRACE) debugLog("pretool", { sessionID: input.sessionID, tool: "bash", cmd: cmd.slice(0, 240) })
        if (bashHazardsChezmoiRepo(cmd, workdir)) {
          debugLog("deny git hazard", { sessionID: input.sessionID, cmd: cmd.slice(0, 240) })
          throw new Error(
            `[chezmoi-guard] bash command appears to run a destructive or\n` +
              `history-rewriting git operation in the chezmoi source repo.\n` +
              `\n` +
              GIT_ALLOWED_RULE +
              `\n\n` +
              `Command (truncated): ${cmd.slice(0, 240)}`,
          )
        }
        // Write detection is per-segment so that `cmd > /tmp/x ; cat
        // ~/.zshrc` doesn't false-positive: the `>` intent and the
        // `~/.zshrc` path are in DIFFERENT statements and shouldn't be
        // paired. Each segment is checked independently for the
        // write-intent-AND-managed-path pairing. Extracted write targets also
        // count as write intent so bare relative redirections can be resolved
        // against the bash tool's workdir.
        let refreshed = false
        for (const seg of splitBashSegments(cmd)) {
          const writeTargets = pathsFromBashWriteTargets(seg)
          // Only WRITE targets are blocked; a segment that merely reads or names
          // a managed path (e.g. `cat ~/.zshrc`) has no write target -> allowed.
          if (!bashHasWriteIntent(seg) && writeTargets.length === 0) continue
          // Prefer explicit write targets; fall back to all path tokens only when
          // a write-intent segment produced no parseable target (exotic quoting).
          // In-place editors already union the rooted paths from their own
          // position onward inside pathsFromBashWriteTargets.
          const candidatePaths = writeTargets.length > 0 ? writeTargets : pathsFromBashCommand(seg)
          if (!refreshed) { refresh(); refreshed = true }
          for (const raw of candidatePaths) {
            const p = normalizePath(resolveAgainstWorkdir(raw, workdir))
            if (touchesManagedPath(p)) {
              debugLog("deny bash write managed", { sessionID: input.sessionID, path: p, cmd: cmd.slice(0, 240) })
              throw managedPathError(p)
            }
          }
        }
        // Allowed: this call's attribution window starts now. (Set after the
        // denies, so a blocked call leaves no entry behind.)
        if (input.callID) {
          if (callStartedAt.size > 1000) callStartedAt.clear() // lost .after events: bound the map
          callStartedAt.set(input.callID, Date.now())
        }
      }
    },
    "tool.execute.after": async (input) => {
      // NOTE: continuation state is deliberately NOT touched here — see
      // SessionChezmoiState. A continuation turn always runs tools, so
      // clearing the window per tool call made it unreachable.
      const startedAt = input.callID ? callStartedAt.get(input.callID) : undefined
      if (input.callID) callStartedAt.delete(input.callID)
      let attribute = false
      let lingers = false
      if (BLOCKED_TOOLS.has(input.tool)) {
        rememberSourceWrites(
          input.sessionID,
          pathsFromArgs(input.tool, input.args).map((p) => resolveAgainstWorkdir(p, directory)),
          true,
        )
      } else if (input.tool === "bash") {
        const cmd = bashCommandFromArgs(input.args)
        attribute = !!cmd && bashMayWrite(cmd)
        lingers = !!cmd && outlivesCall(cmd)
        if (cmd) {
          const workdir = bashWorkdirFromArgs(input.args)
          const paths: string[] = []
          const targets: string[] = []
          for (const seg of splitBashSegments(cmd)) {
            const targetPaths = pathsFromBashWriteTargets(seg)
            if (!bashHasWriteIntent(seg) && !(workdir && isInChezmoiSource(workdir) && targetPaths.length > 0)) continue
            paths.push(...pathsFromBashCommand(seg), ...targetPaths)
            targets.push(...targetPaths)
          }
          // Common non-interactive source edit shape: bash tool workdir points at
          // the chezmoi source and the command writes explicit rooted paths.
          const resolveAll = (ps: string[]) =>
            workdir && isInChezmoiSource(workdir) ? ps.map((p) => resolveAgainstWorkdir(p, workdir)) : ps
          rememberSourceWrites(input.sessionID, resolveAll(paths), false)
          rememberSourceWrites(input.sessionID, resolveAll(targets), true)
        }
      }
      // Attribute by evidence: what actually changed on disk while a
      // write-capable bash call ran — or, while something we backgrounded may
      // still be running (lingerUntil), since the previous event (source tree,
      // source commits, managed live targets). Catches every writer the text
      // heuristics cannot see.
      const root = stateForSession(rootSessionID(input.sessionID))
      const now = Date.now()
      const starts: number[] = []
      if (attribute) starts.push(startedAt ?? Math.max(root.lastSeenAt, now - UNSTAMPED_WINDOW_MS))
      if (root.lingerUntil > 0 && root.lastSeenAt > 0) starts.push(root.lastSeenAt)
      if (starts.length > 0) attributeSessionWrites(input.sessionID, Math.min(...starts))
      if (root.lingerUntil > 0 && now > root.lingerUntil) root.lingerUntil = 0 // final sweep done
      if (lingers) root.lingerUntil = now + LINGER_MS
      root.lastSeenAt = now
      // Recompute after every tool call so successful commits/pushes by this
      // or another process clear the session's pending reminder promptly.
      pendingTouchedPaths(rootSessionID(input.sessionID))
    },
  }
}
