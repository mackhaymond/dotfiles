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
//     to managed live files, or commit/push from inside the chezmoi source
//     repo. Heuristic — meant to catch typical agent bypasses (`echo X >
//     ~/.zshrc`, `sed -i ~/.gitconfig`, `git -C ~/.local/share/chezmoi
//     commit`), not to be a sandbox.
//   - Does NOT cover: GUI editors, apps writing their own configs
//     (those are handled by Tier C drift detection in starship.toml)
//
// Cache: `chezmoi managed --path-style absolute` is invoked at plugin load
// and refreshed on a 5-minute TTL. Newly-tracked files become blocked
// within 5 minutes; force-refresh by restarting the opencode session.

import type { Plugin } from "@opencode-ai/plugin"
import { execSync } from "node:child_process"
import { readFileSync, realpathSync, statSync, unlinkSync } from "node:fs"
import { resolve } from "node:path"

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

// One-shot nonce escape hatch. Replaces the previous fixed `=1` attestation
// with a per-commit, file-backed nonce that the user-facing helper
// `chezmoi-approve-commit` (defined in dot_zshrc.tmpl) generates. Each
// successful bypass DELETES the nonce file, so a stale nonce can't be
// reused for a follow-up commit/push without an explicit fresh approval.
//
// Threat model: the previous `=1` attestation gave one-blanket-approval-
// per-session. An agent that interpreted "go ahead" too broadly would happily
// fire ten commits off the same approval. The nonce flow forces the agent
// to ALSO regenerate the token (which is at minimum a deliberate step in
// agent code, not an inherited blanket).
//
// File: ~/.cache/chezmoi-guard/approve-nonce — contents = hex nonce, mtime
// = creation time. TTL = NONCE_TTL_MS. Stale nonces are deleted on
// inspection, mirroring single-use semantics.
const NONCE_FILE = `${process.env.HOME ?? ""}/.cache/chezmoi-guard/approve-nonce`
const NONCE_TTL_MS = 5 * 60 * 1000

function tryConsumeBypassNonce(cmd: string): boolean {
  const m = COMMIT_OK_RE.exec(cmd)
  if (!m) return false
  const provided = m[1]
  let stored: string
  let ageMs: number
  try {
    stored = readFileSync(NONCE_FILE, "utf-8").trim()
    ageMs = Date.now() - statSync(NONCE_FILE).mtimeMs
  } catch {
    return false
  }
  if (!stored) return false
  if (stored !== provided) return false
  if (ageMs > NONCE_TTL_MS) {
    // Stale: agent generated the nonce but didn't use it within TTL.
    // Clean up so future bypasses must mint a fresh nonce. Cleanup
    // failure here is fine to swallow — return value is false either way.
    try { unlinkSync(NONCE_FILE) } catch { /* idempotent */ }
    return false
  }
  // Race-tight consume: two parallel tool calls could both reach this
  // point with a valid file. The atomic deletion is the SERIALIZATION
  // POINT. Whichever caller's unlinkSync succeeds wins the race; the
  // other gets ENOENT and must NOT report a successful bypass. Without
  // this, both callers swallow the ENOENT silently and bypass the same
  // single-shot nonce.
  try {
    unlinkSync(NONCE_FILE)
  } catch {
    return false
  }
  return true
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

// Detects bash commands that commit or push from the chezmoi source repo.
// The user policy is "never commit/push dotfiles yourself" — applies whether
// the agent uses raw `git -C <chezmoi-src>`, `git --git-dir=<chezmoi-src>/.git`,
// `chezmoi git -- ...`, the natural `cd <chezmoi-src> && git commit` pattern,
// OR the bash-tool's `workdir` parameter (each requires a separate detector
// — `-C`/`--git-dir=`/`workdir=` are independent ways to set the repo, and
// `cd` sets it via cwd inside the command itself).
//
// ESCAPE HATCH: when the user explicitly approves a commit/push, the agent
// must FIRST mint a single-use nonce by running the zsh helper
// `chezmoi-approve-commit` (defined in dot_zshrc.tmpl), then prefix the
// bash command with `CHEZMOI_COMMIT_OK=<nonce>`. Canonical form:
//
//   CHEZMOI_COMMIT_OK=$(chezmoi-approve-commit) \
//     git -C ~/.local/share/chezmoi commit -m "..."
//
// Also accepted: env-var preamble before the attestation, because most
// agentic shells auto-prepend safety env vars (CI=true, GIT_TERMINAL_PROMPT
// =0, etc.) and forcing CHEZMOI_COMMIT_OK=<nonce> to be the LITERAL first
// token would block legitimate use:
//
//   CI=true CHEZMOI_COMMIT_OK=<nonce> git ... commit
//   export CI=true && CHEZMOI_COMMIT_OK=<nonce> git ... commit
//
// Single-use semantics: the nonce file is DELETED on first match. A second
// commit/push using the same nonce → file gone → bypass denied. The agent
// must rerun `chezmoi-approve-commit` for each commit/push, which means
// each commit is a deliberate code path rather than a side-effect of an
// earlier blanket approval.
//
// TTL: 5 minutes. A nonce that's been sitting around for >5min is also
// rejected (and cleaned up). Forces approval-then-commit to stay temporally
// close, so a nonce minted "for later" doesn't outlive its context.
//
// Regex anchored to start-of-command (`^`) and uses negative lookahead so
// the preamble pattern doesn't greedily consume `CHEZMOI_COMMIT_OK=<nonce>`
// as just-another-env-assignment. Specifically REJECTS:
//   - `git commit -m "doc: CHEZMOI_COMMIT_OK=abc escape"` — var inside a
//     quoted string, preceded by non-preamble tokens → no match.
//   - `cat /tmp/x ; CHEZMOI_COMMIT_OK=abc git commit` — leading non-
//     preamble command (preamble allows env assignments only) → no match.
// Nonce shape required: `[0-9a-fA-F]{8,}` (lowercase OR uppercase hex,
// minimum 8 chars). The shape constraint is purely defensive — the
// authoritative check is a byte-equal compare against the file contents.
const GIT_VERB_RE = /\b(?:commit|push|reset|rebase|merge)\b/
const GIT_WRITE_VERB_RE = /(?:^|[\s|;&(])git\s+(?:[^|;&]*?\s+)?(?:commit|push|reset|rebase|merge)\b/
// IF YOU CHANGE THIS REGEX: keep `dot_config/opencode/AGENTS.md` in sync.
// The regex literal is reproduced there for the user/agent-facing docs.
const COMMIT_OK_RE = /^\s*(?:(?!CHEZMOI_COMMIT_OK=)(?:export\s+)?[A-Za-z_]\w*=\S*\s*[;&]*\s+)*CHEZMOI_COMMIT_OK=([0-9a-fA-F]{8,})\s+/

function bashCommitsChezmoiRepo(cmd: string, workdir?: string): boolean {
  if (tryConsumeBypassNonce(cmd)) return false
  const expanded = expandHomeVars(cmd)
  if (/(?:^|[\s|;&(])chezmoi\s+git\b[^|;&]*\b(?:commit|push|reset|rebase|merge)\b/.test(expanded)) {
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
      GIT_VERB_RE.test(expanded)
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
      GIT_VERB_RE.test(expanded)
    ) {
      return true
    }
  }
  // Pattern A2.5: GIT_DIR / GIT_WORK_TREE env vars in the command preamble.
  // `GIT_DIR=<chezmoi>/.git git commit` and `export GIT_WORK_TREE=<chezmoi>;
  // git commit` are both ways to redirect git at the chezmoi repo without
  // any `-C`/`--git-dir`/`cd` syntax. Match the env-assignment, normalize
  // the path (stripping a trailing /.git), and require a git write-verb
  // anywhere in the rest of the command.
  const gitEnvRe = /(?:^|[\s|;&(])(?:export\s+)?(?:GIT_DIR|GIT_WORK_TREE)=(['"]?)([^\s'"|;&]+)\1/g
  while ((m = gitEnvRe.exec(expanded)) !== null) {
    const dir = normalizePath(m[2].replace(/\/\.git$/, ""))
    if (
      (dir === CHEZMOI_SOURCE_DIR || dir.startsWith(CHEZMOI_SOURCE_DIR + "/")) &&
      GIT_WRITE_VERB_RE.test(expanded)
    ) {
      return true
    }
  }
  // Pattern A3: bash-tool `workdir` parameter pointed at chezmoi src + a
  // git write-verb in the command. Without this, `bash(workdir=<chezmoi>,
  // command="git commit")` slips past every other detector — there's no
  // syntactic chezmoi reference in the command string itself, so A1/A2/B
  // can't match. The workdir is supplied by the bash tool wrapper (above
  // the regex layer), not by the user/agent's command shell.
  if (workdir) {
    const dir = normalizePath(workdir)
    if (
      (dir === CHEZMOI_SOURCE_DIR || dir.startsWith(CHEZMOI_SOURCE_DIR + "/")) &&
      GIT_WRITE_VERB_RE.test(expanded)
    ) {
      return true
    }
  }
  // Pattern B: implicit cwd via cd/pushd into chezmoi src + later git verb.
  // Boundary set must include `(` and `{` so subshell wrappers like
  // `(cd ~/.local/share/chezmoi && git commit)` are caught — those are the
  // most natural way an agent would isolate the cd from the surrounding
  // shell state and would otherwise bypass a `[\s|;&]`-only boundary.
  const cdRe = /(?:^|[\s|;&({])(?:cd|pushd)\s+(['"]?)([^\s'"|;&]+)\1/g
  while ((m = cdRe.exec(expanded)) !== null) {
    const dir = normalizePath(m[2])
    if (dir === CHEZMOI_SOURCE_DIR || dir.startsWith(CHEZMOI_SOURCE_DIR + "/")) {
      const restOfCmd = expanded.slice(m.index + m[0].length)
      if (GIT_WRITE_VERB_RE.test(restOfCmd)) {
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
      `  2. Ask the user whether to commit & push the chezmoi repo\n` +
      `     changes.\n` +
      `\n` +
      `Do NOT commit or push yourself. The user always commits dotfile\n` +
      `changes themselves once the entire change is done.`,
  )
}

export const ChezmoiGuard: Plugin = async () => {
  refresh()
  return {
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
      // commit/push to the chezmoi source repo.
      if (input.tool === "bash") {
        const cmd = bashCommandFromArgs(output.args)
        if (!cmd) return
        const workdir = bashWorkdirFromArgs(output.args)
        // Commit detection runs against the WHOLE command — `cd <src> && git
        // commit` legitimately spans segments, and the whitelist is narrow
        // enough that whole-command match is appropriate here. The optional
        // `workdir` parameter is consulted for Pattern A3 (bash-tool
        // workdir-set commits with no syntactic chezmoi reference).
        if (bashCommitsChezmoiRepo(cmd, workdir)) {
          throw new Error(
            `[chezmoi-guard] bash command appears to commit or push the\n` +
              `chezmoi source repo. The user always commits dotfile changes\n` +
              `themselves — stop and ask first.\n` +
              `\n` +
              `If you have completed your edits and applied them, summarize\n` +
              `what changed and ask the user whether to commit & push.\n` +
              `\n` +
              `IF the user has already explicitly approved this commit/push:\n` +
              `  1. mint a single-use nonce:  N=$(chezmoi-approve-commit)\n` +
              `  2. prefix the bash command:  CHEZMOI_COMMIT_OK=$N git ...\n` +
              `Each commit/push needs a fresh nonce; the helper is a zsh\n` +
              `function defined in dot_zshrc.tmpl. Nonces expire 5 min after\n` +
              `mint and are deleted on first use (single-shot).\n` +
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
  }
}
