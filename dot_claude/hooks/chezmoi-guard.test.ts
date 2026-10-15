#!/usr/bin/env bun
// Evidence-attribution tests for chezmoi-guard.ts. The guard runs as a
// subprocess (exactly as Claude Code runs it) on synthetic hook JSON, against
// a throwaway world: a temp HOME holding a temp git repo as the chezmoi source
// dir, a temp state dir with a stub managed.json that never goes stale, a
// temp claims dir, `false` as the chezmoi binary, and a file standing in for
// the tmux pty-cwd query. Nothing here reads or writes the real state, the
// real source repo, or the real home.
import { afterEach, beforeEach, expect, test } from "bun:test"
import { execFileSync, spawnSync } from "node:child_process"
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, utimesSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

const GUARD = join(import.meta.dir, "chezmoi-guard.ts")
const CLASSIFIER = join(import.meta.dir, "pty-read-only.ts")

let root: string, home: string, src: string, state: string, claims: string
let elsewhere: string, project: string, ptyCwds: string
let n = 0

function git(...args: string[]) {
  gitAt(true, ...args)
}
function gitAt(backdate: boolean, ...args: string[]) {
  execFileSync("git", ["-C", src, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", ...args], {
    stdio: "ignore",
    // Fixture commits are backdated, so they are never "a commit made inside
    // the window" (attribution (b)).
    env: {
      PATH: process.env.PATH ?? "/usr/bin:/bin",
      HOME: home,
      GIT_CONFIG_NOSYSTEM: "1",
      ...(backdate ? { GIT_AUTHOR_DATE: "2026-01-01T00:00:00Z", GIT_COMMITTER_DATE: "2026-01-01T00:00:00Z" } : {}),
    },
  })
}

beforeEach(() => {
  root = realpathSync(mkdtempSync(join(tmpdir(), "chezmoi-guard-test-")))
  home = join(root, "home")
  src = join(home, ".local/share/chezmoi")
  state = join(root, "state")
  claims = join(root, "claims")
  elsewhere = join(root, "elsewhere") // outside $HOME, holds nothing managed
  project = join(home, "code/proj") // inside $HOME, holds nothing managed
  ptyCwds = join(root, "pty-cwds")
  for (const d of [src, state, claims, elsewhere, project, join(home, ".config/tmux/scripts")]) mkdirSync(d, { recursive: true })
  writeFileSync(join(src, "a.txt"), "a\n")
  writeFileSync(join(src, "b.txt"), "b\n")
  git("init", "-q")
  git("add", "-A")
  git("commit", "-qm", "init")
  // An upstream, so a commit made during a call is "committed but unpushed".
  execFileSync("git", ["init", "-q", "--bare", join(root, "remote.git")], { stdio: "ignore" })
  git("remote", "add", "origin", join(root, "remote.git"))
  git("push", "-q", "-u", "origin", "HEAD")
  writeFileSync(
    join(state, "managed.json"),
    JSON.stringify({
      version: 1,
      loadedAt: Date.now() + 365 * 24 * 3600 * 1000, // never stale: no refresh spawn
      everLoaded: true,
      paths: [join(home, ".zshrc"), join(home, ".config/tmux/tmux.conf")],
    }),
  )
})

afterEach(() => {
  rmSync(root, { recursive: true, force: true })
})

// Overrides travel ONLY as `--test-env=<json>`; the guard ignores env vars
// for them (see test 6c).
function runHook(
  input: Record<string, unknown>,
  opts: { cwd?: string; env?: Record<string, string> } = {},
): { status: number | null; stdout: string } {
  const testEnv = join(root, "test-env.json")
  writeFileSync(
    testEnv,
    JSON.stringify({
      STATE_DIR: state,
      CLAIMS_DIR: claims,
      SOURCE_DIR: src,
      CHEZMOI_BIN: "/usr/bin/false",
      READ_ONLY_CLASSIFIER: CLASSIFIER,
      TMUX_SESSION: "chezmoi-guard-test-no-such-session",
      PTY_CWDS_FILE: ptyCwds,
    }),
  )
  const r = spawnSync(process.execPath, [GUARD, `--test-env=${testEnv}`], {
    input: JSON.stringify(input),
    encoding: "utf-8",
    cwd: opts.cwd ?? root,
    env: { HOME: home, PATH: process.env.PATH ?? "/usr/bin:/bin", ...opts.env },
  })
  return { status: r.status, stdout: r.stdout }
}

type Call = { tool: "Bash" | "mcp__pty__run"; command: string; cwd?: string; pty?: string }

// One tool call: PreToolUse, then `during` (what happens on disk while the
// command "runs"), then PostToolUse. Returns the session id.
function call(c: Call, during: () => void, opts: { sid?: string; skipPre?: boolean } = {}): string {
  const sid = opts.sid ?? `test-sid-${++n}`
  const tool_use_id = `toolu_${n}_${Math.random().toString(36).slice(2)}`
  const tool_input = c.tool === "Bash" ? { command: c.command } : { command: c.command, pty: c.pty ?? "main" }
  const base = { session_id: sid, tool_use_id, tool_name: c.tool, tool_input, cwd: c.cwd ?? project }
  if (!opts.skipPre) expect(runHook({ ...base, hook_event_name: "PreToolUse" }).stdout).toBe("")
  during()
  const post = runHook({ ...base, hook_event_name: "PostToolUse", tool_response: "exit 0\n" })
  expect(post.status).toBe(0)
  expect(post.stdout).toBe("")
  return sid
}

function touched(sid: string): string[] {
  const prefix = sid.replace(/[^A-Za-z0-9._-]/g, "_") + "-"
  const f = readdirSync(join(state, "sessions")).find((e) => e.startsWith(prefix) && e.endsWith(".json"))
  if (!f) return []
  return JSON.parse(readFileSync(join(state, "sessions", f), "utf-8")).touchedPaths
}

const rewriteSame = (p: string) => {
  writeFileSync(p, readFileSync(p)) // same bytes, fresh mtime
  const now = new Date()
  utimesSync(p, now, now)
}

const ptyAt = (sid: string, pty: string, cwd: string) => writeFileSync(ptyCwds, `other-sid;;${pty};;/\n${sid};;${pty};;${cwd}\n`)

test("1: a dirty file rewritten with identical content (pre-commit stash/restore) is not attributed", () => {
  writeFileSync(join(src, "a.txt"), "someone else's work in progress\n") // dirty before the call
  const sid = call({ tool: "Bash", command: "./build.sh", cwd: src }, () => rewriteSame(join(src, "a.txt")))
  expect(touched(sid)).not.toContain(join(src, "a.txt"))
})

test("1b: same call without a PreToolUse stamp falls back to mtime and attributes it", () => {
  writeFileSync(join(src, "a.txt"), "someone else's work in progress\n")
  const sid = call({ tool: "Bash", command: "./build.sh", cwd: src }, () => rewriteSame(join(src, "a.txt")), { skipPre: true })
  expect(touched(sid)).toContain(join(src, "a.txt"))
})

test("1c: the 2026-10-15 incident — our `git commit` whose pre-commit stash/restore rewrites another agent's dirty file", () => {
  writeFileSync(join(src, "a.txt"), "another agent's unstaged work\n")
  writeFileSync(join(src, "b.txt"), "ours\n")
  const sid = call({ tool: "Bash", command: "git add b.txt && git commit -m ours", cwd: src }, () => {
    gitAt(false, "add", "b.txt")
    rewriteSame(join(src, "a.txt")) // "Stashing unstaged files … Restored changes"
    gitAt(false, "commit", "-qm", "ours")
  })
  const t = touched(sid)
  expect(t).toContain(join(src, "b.txt")) // (b): committed inside the window, push still owed
  expect(t).not.toContain(join(src, "a.txt"))
})

test("2: a content change during a call that names the chezmoi source is attributed (cwd unrelated)", () => {
  const sid = `test-sid-${++n}`
  ptyAt(sid, "w", elsewhere)
  call(
    { tool: "mcp__pty__run", command: `python3 ${src}/gen.py`, pty: "w" },
    () => writeFileSync(join(src, "b.txt"), "changed\n"),
    { sid },
  )
  expect(touched(sid)).toContain(join(src, "b.txt"))
})

test("3: a non-chezmoi command whose pty cwd cannot reach chezmoi files is not attributed", () => {
  for (const cwd of [() => elsewhere, () => project]) {
    const sid = `test-sid-${++n}`
    ptyAt(sid, "poll", cwd())
    call(
      { tool: "mcp__pty__run", command: "for i in 1 2 3; do ./poll.sh; sleep 0; done", pty: "poll" },
      () => writeFileSync(join(src, "b.txt"), `changed by another agent ${n}\n`),
      { sid },
    )
    expect(touched(sid)).not.toContain(join(src, "b.txt"))
  }
})

test("4: same command with an unknown pty cwd is attributed", () => {
  // no window tagged with this session/pty name
  let sid = `test-sid-${++n}`
  ptyAt("someone-else", "poll", elsewhere)
  call({ tool: "mcp__pty__run", command: "./poll.sh", pty: "poll" }, () => writeFileSync(join(src, "b.txt"), "x\n"), { sid })
  expect(touched(sid)).toContain(join(src, "b.txt"))
  // tmux lookup fails outright
  rmSync(ptyCwds, { force: true })
  sid = `test-sid-${++n}`
  call({ tool: "mcp__pty__run", command: "./poll.sh", pty: "poll" }, () => writeFileSync(join(src, "a.txt"), "y\n"), { sid })
  expect(touched(sid)).toContain(join(src, "a.txt"))
})

test("5: a newly created untracked source file with a reachable cwd is attributed", () => {
  // ~/.config/tmux directly holds a managed target, so its subtree reaches.
  const sid = `test-sid-${++n}`
  ptyAt(sid, "main", join(home, ".config/tmux/scripts"))
  call({ tool: "mcp__pty__run", command: "./make-thing.sh" }, () => writeFileSync(join(src, "new.txt"), "new\n"), { sid })
  expect(touched(sid)).toContain(join(src, "new.txt"))
})

test("5b: `cd ../..` from an unrelated dir reaches $HOME and is attributed", () => {
  const sid = `test-sid-${++n}`
  ptyAt(sid, "main", project)
  call({ tool: "mcp__pty__run", command: "cd ../.. && ./x.sh" }, () => writeFileSync(join(src, "b.txt"), "z\n"), { sid })
  expect(touched(sid)).toContain(join(src, "b.txt"))
})

test("5c: a read-only command attributes nothing, whatever the cwd", () => {
  const sid = call({ tool: "Bash", command: "git status", cwd: src }, () => writeFileSync(join(src, "b.txt"), "q\n"))
  expect(touched(sid)).not.toContain(join(src, "b.txt"))
})

test("6: Stop blocks with the one-line pointer for a genuinely attributed path", () => {
  const sid = `test-sid-${++n}`
  ptyAt(sid, "main", src)
  call({ tool: "mcp__pty__run", command: "./gen.sh" }, () => writeFileSync(join(src, "b.txt"), "real work\n"), { sid })
  expect(touched(sid)).toContain(join(src, "b.txt"))
  const r = runHook({ hook_event_name: "Stop", session_id: sid, stop_hook_active: false })
  expect(r.status).toBe(0)
  const out = JSON.parse(r.stdout)
  expect(out.decision).toBe("block")
  const m = /^chezmoi-guard: unfinished dotfile work — read (\S+\/continuations\/\S+\.md) and act on it\.$/.exec(out.reason)
  expect(m).not.toBeNull()
  expect(m![1].startsWith(state)).toBe(true)
  const body = readFileSync(m![1], "utf-8")
  expect(body).toContain("CHEZMOI-GUARD")
  expect(body).toContain("b.txt")
})

test("hard block unchanged: an Edit of a managed live path is denied", () => {
  const r = runHook({
    hook_event_name: "PreToolUse",
    session_id: "test-sid-block",
    tool_use_id: "toolu_block",
    tool_name: "Edit",
    tool_input: { file_path: join(home, ".zshrc") },
    cwd: project,
  })
  expect(JSON.parse(r.stdout).hookSpecificOutput.permissionDecision).toBe("deny")
})

test("6b: Stop stays quiet for a session whose only candidate was content-identical", () => {
  writeFileSync(join(src, "a.txt"), "someone else's work in progress\n")
  const sid = call({ tool: "Bash", command: "./build.sh", cwd: src }, () => rewriteSame(join(src, "a.txt")))
  const r = runHook({ hook_event_name: "Stop", session_id: sid, stop_hook_active: false })
  expect(r.status).toBe(0)
  expect(r.stdout).toBe("")
})

// Reachability gate, case by case. Every call starts from an unrelated
// project dir (pty cwd and Bash cwd alike) while a source file changes; the
// command text alone decides. `true` = must be attributed (reachable or
// unknown), `false` = must be gated.
const GATE_CASES: [string, "Bash" | "mcp__pty__run", string, boolean][] = [
  // the reviewer's probe, kept as-is
  ["probe CONTROL: cd ../.. reaches $HOME", "mcp__pty__run", "cd ../.. && ./gen.sh", true],
  ["probe A: $VAR path word", "mcp__pty__run", 'cp out.txt "$DOTS/b.txt"', true],
  ["probe B: zoxide-style cd to a dir that isn't here", "Bash", "cd hooks && ./gen.sh", true],
  ["probe C: bare cd on its own line ($HOME)", "Bash", "cd\n./gen.sh", true],
  ["probe E: $PWD/.. path word", "mcp__pty__run", './gen.sh --out "$PWD/../../.local/share/x"', true],
  ["probe D: make -C into a dir holding managed targets", "mcp__pty__run", "make -C ~/.config/tmux/scripts", true],
  // 1: expansions as paths / write targets
  ["1: ${VAR}/ path word", "mcp__pty__run", "./gen.sh > ${OUT}/b.txt", true],
  ["1: $(...)/ path word", "mcp__pty__run", 'cp out.txt "$(dirs-of-mine)/b.txt"', true],
  ["1: backtick path word", "mcp__pty__run", "cp out.txt `dirs-of-mine`/b.txt", true],
  ["1: bare expansion as a write target", "mcp__pty__run", 'cp out.txt "$DEST"', true],
  ["1: polling loop with bare $i stays gated", "mcp__pty__run", "for i in 1 2 3; do tmux capture-pane -p -t $i 2>/dev/null | grep -q done && break; sleep $i; done", false],
  ["1: $X/ inside single quotes is literal", "mcp__pty__run", "./report.sh --fmt '$X/literal' 2>/dev/null", false],
  // 2: zoxide
  ["2: z hop", "Bash", "z dots && ./gen.sh", true],
  ["2: \\cd hop", "Bash", "\\cd /somewhere && ./gen.sh", true],
  ["2: cd into an existing subdir stays gated", "Bash", "cd sub && ./gen.sh", false],
  // 3: cd parsing
  ["3: cd followed by a comment is bare", "mcp__pty__run", "cd # go home\n./gen.sh", true],
  // 4: absolute directory arguments
  ["4: git -C under ~/.config/tmux (holds tmux.conf)", "mcp__pty__run", "git -C ~/.config/tmux/plugins/x gc", true],
  ["4: absolute dir arg elsewhere stays gated", "mcp__pty__run", `make -C ${"/usr/local/share"}`, false],
]

for (const [label, tool, command, expected] of GATE_CASES) {
  test(`gate: ${label}`, () => {
    mkdirSync(join(project, "sub"), { recursive: true })
    const sid = `test-sid-${++n}`
    ptyAt(sid, "main", project)
    call({ tool, command, cwd: project }, () => writeFileSync(join(src, "b.txt"), `changed ${n}\n`), { sid })
    expect(touched(sid).includes(join(src, "b.txt"))).toBe(expected)
  })
}

test("6c: overrides come only from --test-env; env vars and a project .env are ignored", () => {
  const evilState = join(root, "evil-state")
  const evilEnvState = join(root, "evil-env-state")
  const repo = join(root, "untrusted-repo")
  mkdirSync(repo, { recursive: true })
  // bun auto-loads .env from its cwd (the session's project dir in real use).
  // CHEZMOI_GUARD_DEBUG is honored from env on purpose (harmless), and proves
  // the .env really was loaded.
  writeFileSync(
    join(repo, ".env"),
    `CHEZMOI_GUARD_STATE_DIR=${evilEnvState}\nCHEZMOI_GUARD_SOURCE_DIR=${elsewhere}\nCHEZMOI_GUARD_DEBUG=1\n`,
  )
  const r = runHook(
    {
      hook_event_name: "PreToolUse",
      session_id: "test-sid-env",
      tool_use_id: "toolu_env",
      tool_name: "Bash",
      tool_input: { command: `git -C ${src} reset --hard` },
      cwd: repo,
    },
    { cwd: repo, env: { CHEZMOI_GUARD_STATE_DIR: evilState, CHEZMOI_GUARD_CHEZMOI_BIN: join(repo, "x") } },
  )
  // The hard block still knows the real (test-env) source dir.
  expect(JSON.parse(r.stdout).hookSpecificOutput.permissionDecision).toBe("deny")
  expect(existsSync(evilState)).toBe(false)
  expect(existsSync(evilEnvState)).toBe(false)
  expect(readFileSync(join(state, "chezmoi-guard.log"), "utf-8")).toContain("pretool")
})

test("7: touched paths with a space or non-ASCII byte survive the dirty-set recompute", () => {
  const sid = `test-sid-${++n}`
  for (const name of ["a b.txt", "café.txt"]) {
    const p = join(src, name)
    writeFileSync(p, "new\n")
    const r = runHook({
      hook_event_name: "PostToolUse",
      session_id: sid,
      tool_use_id: `toolu_w_${name}`,
      tool_name: "Write",
      tool_input: { file_path: p, content: "new\n" },
      tool_response: "ok",
      cwd: project,
    })
    expect(r.status).toBe(0)
  }
  const t = touched(sid)
  expect(t).toContain(join(src, "a b.txt"))
  expect(t).toContain(join(src, "café.txt"))
  // ...and the next recompute (UserPromptSubmit) still reports them.
  const ups = runHook({ hook_event_name: "UserPromptSubmit", session_id: sid, prompt: "hi" })
  const ctx = JSON.parse(ups.stdout).hookSpecificOutput.additionalContext
  expect(ctx).toContain("a b.txt")
  expect(ctx).toContain("café.txt")
})
