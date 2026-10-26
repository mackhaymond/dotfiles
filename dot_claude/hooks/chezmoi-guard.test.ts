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
    timeout: 10_000, // a runaway guard fails its test instead of hanging the suite
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

// ---------------------------------------------------------------------------
// PreToolUse hard blocks (2026-10-25 review findings). `pre` runs only the
// PreToolUse hook and reports the decision. For mcp__pty__run, `ptyCwd` is the
// pty shell's real cwd (the tmux pane_current_path stub); undefined = unknown.
// ---------------------------------------------------------------------------

function pre(
  tool: "Bash" | "mcp__pty__run",
  command: string,
  opts: { cwd?: string; ptyCwd?: string } = {},
): "deny" | "allow" {
  const sid = `test-sid-${++n}`
  if (tool === "mcp__pty__run") {
    if (opts.ptyCwd) ptyAt(sid, "main", opts.ptyCwd)
    else rmSync(ptyCwds, { force: true })
  }
  const tool_input = tool === "Bash" ? { command } : { command, pty: "main" }
  const r = runHook({
    hook_event_name: "PreToolUse",
    session_id: sid,
    tool_use_id: `toolu_pre_${n}`,
    tool_name: tool,
    tool_input,
    cwd: opts.cwd ?? project,
  })
  expect(r.status).toBe(0)
  if (r.stdout === "") return "allow"
  return JSON.parse(r.stdout).hookSpecificOutput.permissionDecision === "deny" ? "deny" : "allow"
}

test("F1: pty git hazards use the pty shell's real cwd (an earlier `cd` in the persistent shell)", () => {
  for (const c of ["git stash", "git reset --hard", "git restore .", "git clean -fd"]) {
    expect([c, pre("mcp__pty__run", c, { ptyCwd: src })]).toEqual([c, "deny"])
    expect([c, pre("mcp__pty__run", c, { ptyCwd: join(src, ".git") })]).toEqual([c, "deny"])
  }
  // the pty stands in another repo: allowed
  expect(pre("mcp__pty__run", "git stash", { ptyCwd: project })).toBe("allow")
  expect(pre("mcp__pty__run", "git reset --hard", { ptyCwd: project })).toBe("allow")
  // ordinary source-repo work from the pty stays allowed
  for (const c of ["git status", "git diff", "git log -n 3", "git add a.txt", "git commit -m msg", "git push", "git stash list", "chezmoi apply"]) {
    expect([c, pre("mcp__pty__run", c, { ptyCwd: src })]).toEqual([c, "allow"])
  }
})

test("F1: pty cwd unknown -> hazard verbs are denied unless the command cds somewhere literal first", () => {
  expect(pre("mcp__pty__run", "git stash")).toBe("deny")
  expect(pre("mcp__pty__run", "git reset --hard HEAD")).toBe("deny")
  expect(pre("mcp__pty__run", `cd ${project} && git stash`)).toBe("allow")
  expect(pre("mcp__pty__run", `cd ${project}\ngit reset --hard`)).toBe("allow")
  expect(pre("mcp__pty__run", `cd ${src} && git stash`)).toBe("deny")
  expect(pre("mcp__pty__run", `cd ${join(root, "nope")}; git stash`)).toBe("deny") // cd fails / zoxide query
  expect(pre("mcp__pty__run", `git -C ${project} stash`)).toBe("allow")
  for (const c of ["git status", "git log -n 3", "git add -A", "git commit -m x", "git commit -m 'undo: git reset was wrong'", "git push origin main", "git branch -c a b", "ls", "chezmoi apply"]) {
    expect([c, pre("mcp__pty__run", c)]).toEqual([c, "allow"])
  }
})

test("F1: pty relative writes resolve against the pty shell's real cwd", () => {
  const tmuxDir = join(home, ".config/tmux")
  expect(pre("mcp__pty__run", "rm tmux.conf", { ptyCwd: tmuxDir })).toBe("deny")
  expect(pre("mcp__pty__run", "echo x > tmux.conf", { ptyCwd: tmuxDir })).toBe("deny")
  expect(pre("mcp__pty__run", "rm tmux.conf", { ptyCwd: project })).toBe("allow")
  expect(pre("mcp__pty__run", "echo x > out.txt", { ptyCwd: tmuxDir })).toBe("allow")
})

test("F2: tee blocks every operand, and an unparsed redirect is checked beside a parsed one", () => {
  expect(pre("Bash", "echo x | tee /tmp/a ~/.zshrc")).toBe("deny")
  expect(pre("Bash", "echo x | tee -a /tmp/a ~/.zshrc")).toBe("deny")
  expect(pre("Bash", "echo hi>~/.zshrc 2>/tmp/err")).toBe("deny")
  expect(pre("Bash", "echo x | tee /tmp/a /tmp/b")).toBe("allow")
  expect(pre("Bash", "cat ~/.zshrc > /tmp/copy")).toBe("allow") // a read of a managed file
})

test("F2: a tee into a source file is claimed", () => {
  const sid = call({ tool: "Bash", command: `echo x | tee /tmp/zz-${n} ${src}/b.txt` }, () =>
    writeFileSync(join(src, "b.txt"), "teed\n"),
  )
  const f = readdirSync(join(state, "sessions")).find((e) => e.startsWith(sid + "-") && e.endsWith(".json"))!
  expect(JSON.parse(readFileSync(join(state, "sessions", f), "utf-8")).exactPaths).toContain(join(src, "b.txt"))
})

test("F3: `cd <dir> && <relative write>` resolves against the cd target", () => {
  const tmuxDir = join(home, ".config/tmux")
  expect(pre("Bash", `cd ${tmuxDir} && echo x > tmux.conf`)).toBe("deny")
  expect(pre("Bash", "cd ~/.config/tmux && rm tmux.conf")).toBe("deny")
  expect(pre("Bash", "cd ~/.config && mv tmux/tmux.conf /tmp/x")).toBe("deny")
  expect(pre("Bash", "cd ~/.config/tmux; sed -i '' s/a/b/ tmux.conf")).toBe("deny")
  expect(pre("Bash", "cd\necho x > .zshrc")).toBe("deny") // bare cd = $HOME
  expect(pre("mcp__pty__run", "cd ~/.config/tmux && echo x > tmux.conf", { ptyCwd: project })).toBe("deny")
  expect(pre("Bash", `cd ${project} && echo x > tmux.conf`)).toBe("allow")
  expect(pre("Bash", `cd ${tmuxDir} && cat tmux.conf > /tmp/x`)).toBe("allow")
})

test("F4: git-hazard regexes stop at a newline", () => {
  expect(pre("Bash", `cd ${src}\ngit clean -fd\nhead -n 3 README`)).toBe("deny")
  expect(pre("Bash", `cd ${src}\ngit clean -fd\nsort -n x`)).toBe("deny")
  expect(pre("Bash", `cd ${src} && git clean \\\n  -fd`)).toBe("deny") // a continued line is still one command
  expect(pre("Bash", `cd ${src} && git clean -nd`)).toBe("allow")
  expect(pre("Bash", `cd ${src}\ngit switch main\nrm -f /tmp/zz`)).toBe("allow")
  expect(pre("Bash", `cd ${src}\ngit push origin main\nrm -f /tmp/zz`)).toBe("allow")
  expect(pre("Bash", `cd ${src}\ngit commit -m msg\nmytool --amend`)).toBe("allow")
  expect(pre("Bash", `cd ${src}\ngit checkout main\ncat ./x.txt`)).toBe("allow")
  expect(pre("Bash", `cd ${src}\ngit switch -f main`)).toBe("deny")
  expect(pre("Bash", `cd ${src}\ngit push --force`)).toBe("deny")
})

const F5_DENY = [
  "git rm -rf .",
  "git rm -r dir",
  "git rm -f a.txt",
  "git rm -r --cached .",
  "git rm '*.txt'",
  "git branch -f main HEAD~3",
  "git branch -D old",
  "git branch -d old",
  "git branch --delete old",
  "git branch -M main",
  "git branch -C old main",
  "git branch --force main HEAD~1",
  "git push origin --delete main",
  "git push origin -d main",
  "git push origin :main",
  "git push --prune origin",
  "git update-ref refs/heads/main HEAD~1",
  "git update-ref -d refs/heads/main",
  "git read-tree -u --reset HEAD",
  "git read-tree HEAD",
  "git reflog expire --expire=now --all",
  "git reflog delete HEAD@{1}",
  "git gc --prune=now",
  "git gc",
  "git prune",
  "git worktree remove --force ../wt",
  "git worktree remove ../wt",
  "git filter-branch --tree-filter x HEAD",
  "git filter-repo --path x",
  "git switch -C main",
  "git checkout -B main",
  "git checkout-index -a -f",
]
const F5_ALLOW = [
  "git rm a.txt",
  "git rm --cached a.txt",
  "git branch",
  "git branch -a",
  "git branch -vv",
  "git branch --merged",
  "git branch newfeature",
  "git push -u origin main",
  "git push origin main:main",
  "git push --follow-tags",
  "git reflog",
  "git worktree list",
  "git status && git diff && git log --oneline -5",
  "git add -A && git commit -m 'msg; with -f in it' && git push",
  "git switch -c feat",
  "git checkout -b feat",
  "chezmoi apply",
  "chezmoi diff",
]

test("F5: more source-repo git ops that discard work or rewrite refs are denied", () => {
  for (const c of F5_DENY) expect([c, pre("Bash", `cd ${src} && ${c}`)]).toEqual([c, "deny"])
  for (const c of F5_DENY.slice(0, 3)) expect([c, pre("Bash", c, { cwd: src })]).toEqual([c, "deny"])
})

test("F5: ordinary source-repo commands stay allowed (and hazards elsewhere are not this guard's business)", () => {
  for (const c of F5_ALLOW) expect([c, pre("Bash", `cd ${src} && ${c}`)]).toEqual([c, "allow"])
  for (const c of F5_ALLOW) expect([c, pre("Bash", c, { cwd: src })]).toEqual([c, "allow"])
  for (const c of F5_DENY) expect([c, pre("Bash", `cd ${project} && ${c}`, { cwd: project })]).toEqual([c, "allow"])
  expect(pre("Bash", `cd ${elsewhere} && ls`)).toBe("allow")
})

// ---------------------------------------------------------------------------
// Review round 2 (2026-10-25): per-segment cwd, cd scoping, quoted data.
// ---------------------------------------------------------------------------

test("R1: after `cd <dir>` relative writes resolve against that dir only, not the start cwd", () => {
  for (const c of [
    `cd ${project} && cp /tmp/s.json .config/tmux/tmux.conf`,
    `cd ${project} && rm -rf .config`,
    `cd ${project} && echo '{}' > .zshrc`,
  ]) {
    expect([c, pre("mcp__pty__run", c, { ptyCwd: home })]).toEqual([c, "allow"])
    expect([c, pre("Bash", c, { cwd: home })]).toEqual([c, "allow"])
  }
  // a cd that can't be followed still checks every directory seen
  expect(pre("mcp__pty__run", `cd ${join(root, "nope")}; echo x > .zshrc`, { ptyCwd: home })).toBe("deny")
  expect(pre("Bash", "cd - && echo x > .zshrc", { cwd: home })).toBe("deny")
  expect(pre("Bash", "echo x > .zshrc", { cwd: home })).toBe("deny")
})

test("R2: hazards are judged per segment, after cds, ignoring commit-message text", () => {
  const ok = [
    `cd ${project} && git reset --hard`,
    `cd ${project} && git checkout -- src/app.ts`,
    `(cd ${project} && git reset --hard)`,
    "git commit -m 'guard: deny git stash in the pty'",
    'git commit -m "undo: git reset --hard was wrong"',
    "git commit -F - <<'EOF'\nguard: block git reset --hard\nand git stash\nEOF",
    "git commit -m \"$(cat <<'EOF'\nguard: block git reset --hard\n\nEOF\n)\"",
    "echo 'then git stash' > /tmp/notes",
    "rg -n 'git reset --hard' dot_claude/hooks/",
    'grep -rn "git stash pop" .',
  ]
  for (const c of ok) {
    expect([c, pre("mcp__pty__run", c, { ptyCwd: src })]).toEqual([c, "allow"])
    expect([c, pre("Bash", c, { cwd: src })]).toEqual([c, "allow"])
  }
  expect(pre("Bash", `cd ${src} && git commit -m 'no git stash here' && git push`)).toBe("allow")
  const bad = [
    "git stash push -m 'wip'",
    "bash -c 'git stash'",
    "eval 'git reset --hard'",
    `cd ${project} && sh -c "cd ${src} && git stash"`,
    'echo "$(git stash)"',
    `cd ${project} && cd ${src} && git reset --hard`,
    "git commit -m x; git reset --hard",
  ]
  for (const c of bad) {
    expect([c, pre("mcp__pty__run", c, { ptyCwd: src })]).toEqual([c, "deny"])
    expect([c, pre("Bash", c, { cwd: src })]).toEqual([c, "deny"])
  }
})

test("R2: the deny message says when the pty's cwd is the reason", () => {
  const reason = (sid: string) =>
    JSON.parse(
      runHook({
        hook_event_name: "PreToolUse",
        session_id: sid,
        tool_use_id: `toolu_${sid}`,
        tool_name: "mcp__pty__run",
        tool_input: { command: "git stash", pty: "main" },
        cwd: project,
      }).stdout,
    ).hookSpecificOutput.permissionDecisionReason
  expect(reason("test-sid-msg")).toContain("could not be determined") // no pty-cwd stub yet: unknown
  ptyAt("test-sid-msg2", "main", src)
  expect(reason("test-sid-msg2")).toContain(`pty shell is in ${src}`)
})

test("R3: a pty that cd'd away from the source does not claim a same-named source file", () => {
  writeFileSync(join(src, "a.txt"), "another agent's work in progress\n") // dirty: never pruned
  const sid = `test-sid-${++n}`
  ptyAt(sid, "main", src)
  call({ tool: "mcp__pty__run", command: `cd ${project} && echo hello > a.txt` }, () => writeFileSync(join(project, "a.txt"), "hello\n"), { sid })
  const f = readdirSync(join(state, "sessions")).find((e) => e.startsWith(sid + "-") && e.endsWith(".json"))!
  expect(JSON.parse(readFileSync(join(state, "sessions", f), "utf-8")).exactPaths).not.toContain(join(src, "a.txt"))
})

test("R3: a relative write after a cd into the source is claimed", () => {
  const sid = `test-sid-${++n}`
  ptyAt(sid, "main", project)
  call({ tool: "mcp__pty__run", command: `cd ${src} && echo hello > b.txt` }, () => writeFileSync(join(src, "b.txt"), "hello\n"), { sid })
  const f = readdirSync(join(state, "sessions")).find((e) => e.startsWith(sid + "-") && e.endsWith(".json"))!
  expect(JSON.parse(readFileSync(join(state, "sessions", f), "utf-8")).exactPaths).toContain(join(src, "b.txt"))
})

test("R4: relative, chained, zoxide and unfollowable hops into the source are caught with a known cwd", () => {
  mkdirSync(join(project, "sub"), { recursive: true })
  expect(pre("mcp__pty__run", "cd .local/share/chezmoi && git reset --hard", { ptyCwd: home })).toBe("deny")
  expect(pre("Bash", "cd .local/share/chezmoi && git reset --hard", { cwd: home })).toBe("deny")
  expect(pre("Bash", "cd ~/.local/share && cd chezmoi && git stash")).toBe("deny")
  expect(pre("mcp__pty__run", "cd ~/.local/share && cd chezmoi && git stash", { ptyCwd: project })).toBe("deny")
  expect(pre("mcp__pty__run", "z chezmoi && git stash", { ptyCwd: project })).toBe("deny")
  expect(pre("Bash", "z chezmoi && git stash")).toBe("deny")
  expect(pre("Bash", "cd - && git stash")).toBe("deny")
  expect(pre("Bash", "cd $DOTS && git stash")).toBe("deny")
  // and the ordinary forms stay allowed
  expect(pre("Bash", "cd sub && git stash", { cwd: project })).toBe("allow")
  expect(pre("Bash", "cd ~/.local/share && cd chezmoi && git status && git add -A && git commit -m x && git push")).toBe("allow")
})

test("R5: cd inside ( … ), behind a single &, or inside quotes does not move the shell", () => {
  expect(pre("mcp__pty__run", "(cd /tmp && ls); git stash")).toBe("deny")
  expect(pre("mcp__pty__run", "cd /tmp & git reset --hard")).toBe("deny")
  expect(pre("mcp__pty__run", "echo 'cd /tmp'; git stash")).toBe("deny")
  expect(pre("mcp__pty__run", "git commit -m 'cd /tmp' && git stash")).toBe("deny")
  expect(pre("mcp__pty__run", `(cd ${project} && ls); git reset --hard`, { ptyCwd: src })).toBe("deny")
  expect(pre("mcp__pty__run", `cd ${project} & git reset --hard`, { ptyCwd: src })).toBe("deny")
  expect(pre("mcp__pty__run", `(cd ${src} && git stash)`, { ptyCwd: project })).toBe("deny")
  expect(pre("mcp__pty__run", "cd /tmp && git reset --hard")).toBe("allow")
})

test("R6: a long chain of relative cds stays fast", () => {
  mkdirSync(join(project, "sub"), { recursive: true })
  const time = (c: string) => {
    const t = performance.now()
    pre("Bash", c, { cwd: project })
    return performance.now() - t
  }
  time("ls") // warm-up
  const base = Math.min(time("ls"), time("ls"))
  // distinct names: each hop resolved against every dir seen doubles the set
  const chain = Array.from({ length: 40 }, (_, i) => `cd d${i}`).join(" && ") + " && ls"
  expect(time(chain) - base).toBeLessThan(200)
})

test("R7: git with global flags, an absolute path or a backslash is still git", () => {
  for (const c of ["git -P stash", "git --no-optional-locks stash", "git --no-pager -c x=y reset --hard", "/usr/bin/git stash", "\\git stash", "git --namespace foo stash"]) {
    expect([c, pre("Bash", c, { cwd: src })]).toEqual([c, "deny"])
  }
  for (const c of ["git -P log", "git --no-optional-locks status", "/usr/bin/git diff", "git pull --rebase"]) {
    expect([c, pre("Bash", c, { cwd: src })]).toEqual([c, "allow"])
  }
})

// ---------------------------------------------------------------------------
// Review round 3 (2026-10-25): cost, false denies elsewhere, cap, `<<`, data
// that runs, backticks, nested writes.
// ---------------------------------------------------------------------------

// Hook wall time for one PreToolUse, minus a warm baseline.
function overBaseline(tool: "Bash" | "mcp__pty__run", c: string, opts: { cwd?: string; ptyCwd?: string }): { dec: string; ms: number } {
  const t = (cmd: string) => {
    const s = performance.now()
    const dec = pre(tool, cmd, opts)
    return { dec, ms: performance.now() - s }
  }
  t("ls")
  const base = Math.min(t("ls").ms, t("ls").ms)
  const r = t(c)
  return { dec: r.dec, ms: r.ms - base }
}

test("RA: many quoted args or many segments stay linear", () => {
  const args = overBaseline("Bash", "git add " + Array.from({ length: 10_000 }, (_, i) => `"dir/file${i}.txt"`).join(" "), { cwd: src })
  console.log(`RA: 10,000 quoted args: ${args.ms.toFixed(0)} ms over baseline`)
  expect(args.dec).toBe("allow")
  expect(args.ms).toBeLessThan(500)
  const segs = overBaseline("mcp__pty__run", Array.from({ length: 10_000 }, (_, i) => `echo "line ${i}"`).join("\n"), { ptyCwd: project })
  console.log(`RA: 10,000 quoted segments: ${segs.ms.toFixed(0)} ms over baseline`)
  expect(segs.dec).toBe("allow")
  expect(segs.ms).toBeLessThan(500)
})

test("RB: cd options, not-yet-existing literal paths and the repo toplevel are followed", () => {
  mkdirSync(join(project, ".git"), { recursive: true }) // ~/code/proj is a repo
  const ok = [
    `cd -P ${project} && git stash`,
    "cd -- ~/code/proj && git stash",
    "git worktree add ../wt-feat && cd ../wt-feat && git reset --hard origin/main",
    'cd "$(git rev-parse --show-toplevel)" && git checkout -- .',
    "cd $(git rev-parse --show-toplevel) && git stash",
  ]
  for (const c of ok) expect([c, pre("mcp__pty__run", c, { ptyCwd: project })]).toEqual([c, "allow"])
  // the same toplevel from inside the source is the source
  expect(pre("mcp__pty__run", 'cd "$(git rev-parse --show-toplevel)" && git stash', { ptyCwd: src })).toBe("deny")
  // a not-yet-existing path after `;` may not be entered: the old cwd still counts
  expect(pre("mcp__pty__run", "cd ../nope; git stash", { ptyCwd: src })).toBe("deny")
  expect(pre("mcp__pty__run", "cd ../nope && git stash", { ptyCwd: src })).toBe("allow")
  // a bare word that isn't a directory is a zoxide query: unknown
  expect(pre("mcp__pty__run", "cd nosuchdir && git stash", { ptyCwd: project })).toBe("deny")
  // expansions stay unknown, and the message says what to do instead
  for (const c of ['for d in a b; do (cd "$d" && git stash); done', "cd $WT && git reset --hard"]) {
    const r = runHook({
      hook_event_name: "PreToolUse",
      session_id: `test-sid-${++n}`,
      tool_use_id: `toolu_rb_${n}`,
      tool_name: "Bash",
      tool_input: { command: c },
      cwd: project,
    })
    const out = JSON.parse(r.stdout).hookSpecificOutput
    expect([c, out.permissionDecision]).toEqual([c, "deny"])
    expect(out.permissionDecisionReason).toContain("literal absolute path")
  }
})

test("RC: an absolute cd still counts after the directory-set cap", () => {
  const c = "cd nope1; cd nope2; cd nope3; cd nope4; cd nope5; cd nope6; cd nope7; cd ~/.config/tmux; echo x > tmux.conf"
  expect(pre("mcp__pty__run", c, { ptyCwd: project })).toBe("deny")
  const z = Array.from({ length: 39 }, (_, i) => `cd ${i % 2 ? "a" : "z"}${i + 1}`).join("; ") + "; cd ~/.config/tmux; echo x > tmux.conf"
  expect(pre("mcp__pty__run", z, { ptyCwd: project })).toBe("deny")
})

test("RD: `<<` is a heredoc only outside quotes and arithmetic", () => {
  expect(pre("mcp__pty__run", "x=$((1 << n))\ncd ~/.local/share/chezmoi\ngit stash", { ptyCwd: project })).toBe("deny")
  expect(pre("mcp__pty__run", "(( y = 1 << 2 ))\ncd ~/.local/share/chezmoi\ngit stash", { ptyCwd: project })).toBe("deny")
  expect(pre("mcp__pty__run", "python3 -c 'print(1 << n)'\ncd ~/code/proj\ngit reset --hard", { ptyCwd: src })).toBe("allow")
  // the commit idiom keeps working
  expect(pre("mcp__pty__run", "git add -A && git commit -m \"$(cat <<'EOF'\nguard: deny git stash and git reset --hard\n\nEOF\n)\" && git push", { ptyCwd: src })).toBe("allow")
})

test("RE: blanked data that actually runs is code", () => {
  const bad = [
    "bash -c \"$(cat <<'EOF'\ngit stash\nEOF\n)\"",
    "eval \"$(cat <<'EOF'\ngit reset --hard\nEOF\n)\"",
    "source <(cat <<'EOF'\ngit stash\nEOF\n)",
    ". <(cat <<'EOF'\ngit stash\nEOF\n)",
    "echo 'git stash' | sh",
    "printf 'git reset --hard\\n' | bash",
    "echo 'git stash' | xargs -I{} sh -c '{}'",
    "ls # list files \\\ngit stash",
    "git commit -m \"$(cat <<EOF\nmsg $(git stash)\nEOF\n)\"",
  ]
  for (const c of bad) expect([c, pre("mcp__pty__run", c, { ptyCwd: src })]).toEqual([c, "deny"])
  const ok = [
    "echo 'git stash' > /tmp/notes",
    "echo 'git stash' | tee /tmp/notes",
    "cat <<'EOF' > /tmp/x\ngit stash\nEOF",
    "ls # git stash \\\nls",
  ]
  for (const c of ok) expect([c, pre("mcp__pty__run", c, { ptyCwd: src })]).toEqual([c, "allow"])
})

test("RF: a backtick before git is a word boundary", () => {
  expect(pre("mcp__pty__run", "x=`git reset --hard`", { ptyCwd: src })).toBe("deny")
  expect(pre("Bash", "x=`git reset --hard`", { cwd: src })).toBe("deny")
  expect(pre("Bash", 'echo "x" `git stash`', { cwd: src })).toBe("deny")
})

test("RG: writes inside `bash -c '…'` follow the inner cd", () => {
  expect(pre("mcp__pty__run", 'bash -c "cd ~/.config/tmux && echo x > tmux.conf"', { ptyCwd: project })).toBe("deny")
  expect(pre("mcp__pty__run", "sh -c 'cd ~/code/proj && echo x > tmux.conf'", { ptyCwd: join(home, ".config/tmux") })).toBe("deny") // outer cwd still counts
  expect(pre("mcp__pty__run", 'bash -c "cd ~/code/proj && echo x > out.txt"', { ptyCwd: project })).toBe("allow")
})

test("RH: a cd that may not run only moves the rest of its own && chain", () => {
  const bad = [
    "false && cd /tmp; git stash",
    "[ -d /nonexistent ] && cd /tmp; git reset --hard",
    'if [ -n "$X" ]; then cd /tmp; fi; git stash',
    "true || cd /tmp; git stash",
    "case $x in a) cd /tmp;; esac; git stash",
    "while read d; do cd /tmp; done < /dev/null\ngit stash",
    "[ -d /tmp ] && cd /tmp && ls; git stash",
  ]
  for (const c of bad) expect([c, pre("Bash", c, { cwd: src })]).toEqual([c, "deny"])
  expect(pre("mcp__pty__run", "[ -f package.json ] && cd /tmp; git checkout -- .", { ptyCwd: src })).toBe("deny")
  const ok = ["[ -d /tmp ] && cd /tmp && git stash", "cd /tmp && git stash", "cd /tmp; git stash", "cd /tmp || exit 1; git stash"]
  for (const c of ok) expect([c, pre("Bash", c, { cwd: src })]).toEqual([c, "allow"])
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
