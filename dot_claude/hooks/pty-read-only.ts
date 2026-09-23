#!/usr/bin/env bun
// PreToolUse hook: give mcp__pty__run the per-command read-only treatment
// Claude Code gives the native Bash tool, in plan mode AND auto mode.
//
// WHY THIS EXISTS
// Bash computes read-only-ness PER COMMAND (`ls` yes, `rm -rf` no). An MCP
// tool can't: its isReadOnly() is the static `annotations.readOnlyHint`,
// which ignores the input. With Bash deny-listed and mcp__pty__run as its
// replacement, that cost two things:
//   • plan mode had no shell at all, not even `ls`;
//   • auto mode sent EVERY command — `ls`, `dig`, `git status` — to the
//     classifier, which native Bash skips for read-only commands. The
//     classifier then blocked plain reads as "scouting" for an earlier
//     denied action (2026-09-23, mack.link session: `ls <file>` and `dig`
//     denied as [Production Deploy]).
// This hook restores the per-command half: it classifies the command and
// returns `allow` for read-only ones, which resolves before the classifier.
//
// It only ever WIDENS and never narrows:
//   • plan/auto + read-only cmd → allow
//   • anything else             → silent passthrough, so plan mode's
//                                 "Cannot call <tool>" gate or the auto-mode
//                                 classifier still judges it.
// Classification failure of any kind falls through, so the failure mode is
// "the classifier looks at it", never "a write ran unreviewed".
//
// Auto mode adds one gate plan mode doesn't need: commands that touch
// credentials or personal data stores (isSensitive) fall through to the
// classifier even when they're read-only, because reading a secret is the
// risk there, not writing.
//
// The read_pty / list_ptys / spawn_pty / send_keys / kill_pty tools need no
// hook: they carry honest readOnlyHint annotations in the server itself.

interface HookInput {
  permission_mode?: string;
  tool_name?: string;
  tool_input?: { command?: string };
}

// ── read-only command vocabulary ──────────────────────────────────────────
// Heads that cannot mutate anything on their own. Mirrors Claude Code's own
// read-only sets (src/utils/shell/readOnlyCommandValidation.ts), plus the
// obvious inspection tools. Heads whose flags can write or execute are
// narrowed per-head in segmentIsReadOnly.
const READ_ONLY_HEADS = new Set([
  // search
  "find", "grep", "rg", "ag", "ack", "locate", "which", "whereis", "type",
  // read / slice / transform on stdout
  "cat", "bat", "head", "tail", "wc", "stat", "file", "strings", "jq", "yq",
  "awk", "cut", "sort", "uniq", "tr", "column", "fold", "nl", "rev", "tee",
  "diff", "cmp", "comm", "md5", "md5sum", "shasum", "sha256sum", "base64",
  "xxd", "od", "sed",
  // listing / paths
  "ls", "tree", "du", "df", "basename", "dirname", "realpath", "readlink",
  "pwd", "cd",
  // trivial output
  "echo", "printf", "true", "false", ":", "seq", "test", "[", "sleep",
  // machine / process facts
  "date", "uname", "hostname", "whoami", "id", "env", "printenv", "ps",
  "uptime", "getconf", "sw_vers", "arch", "groups", "locale",
  // DNS lookups (see NETWORK_HEADS)
  "dig", "host", "nslookup",
  // version probes are read-only regardless of the binary
  "node", "bun", "python3", "git", "gh", "docker", "tmux", "npm", "cargo",
  "go", "rustc", "swift", "brew", "kubectl", "terraform", "claude",
]);

// Heads that send their arguments off the machine. A literal hostname is
// fine; anything expanded into it ($VAR, $(…)) could carry data out.
const NETWORK_HEADS = new Set(["dig", "host", "nslookup"]);

// Heads reachable only via an explicit read-only subcommand allowlist. Any
// head listed here but absent from SUBCOMMANDS is rejected outright.
const SUBCOMMANDS: Record<string, Set<string>> = {
  git: new Set([
    "status", "log", "diff", "show", "blame", "branch", "tag", "remote",
    "rev-parse", "rev-list", "describe", "ls-files", "ls-remote", "ls-tree",
    "shortlog", "reflog", "cat-file", "name-rev", "whatchanged", "grep",
    "count-objects", "symbolic-ref", "check-ignore", "config", "stash",
    "worktree", "notes", "bisect",
  ]),
  gh: new Set(["pr", "issue", "run", "repo", "release", "api", "auth", "search", "workflow"]),
  docker: new Set(["ps", "images", "logs", "inspect", "version", "info", "top", "port", "stats", "diff", "history"]),
  tmux: new Set([
    "list-windows", "list-panes", "list-sessions", "list-clients", "list-keys",
    "list-commands", "capture-pane", "display-message", "show-options",
    "show-environment", "has-session", "info", "lsw", "lsp", "ls",
  ]),
  npm: new Set(["ls", "list", "view", "info", "outdated", "why", "config", "root", "prefix", "bin", "search", "audit"]),
  cargo: new Set(["tree", "search", "metadata", "verify-project", "locate-project"]),
  go: new Set(["list", "env", "version", "doc", "vet"]),
  brew: new Set(["list", "info", "search", "config", "deps", "outdated", "--prefix"]),
  kubectl: new Set(["get", "describe", "logs", "explain", "top", "api-resources", "api-versions", "version", "config"]),
  terraform: new Set(["show", "output", "providers", "validate", "version", "fmt"]),
  claude: new Set(["--version", "-v", "mcp", "config", "doctor"]),
  // interpreters: a bare `--version`/`-v` probe only (see isVersionProbe)
  node: new Set(), bun: new Set(), python3: new Set(), rustc: new Set(),
  swift: new Set(), git_placeholder: new Set(),
};

// Subcommands above that are read-only ONLY as a bare listing — any of these
// flags turns them into a write.
const WRITE_FLAGS = new Set([
  "-d", "-D", "-m", "-M", "-f", "--force", "--delete", "--set", "--unset",
  "--add", "--edit", "--replace-all", "--set-upstream", "--move", "--create",
  "--prune", "--rename", "-i", "--in-place", "--write", "-w", "--fix",
  "-c", "-C", "--copy", "-u", "--unset-upstream", "--set-upstream-to",
]);
const GIT_BARE_ONLY = new Set(["branch", "tag", "config", "stash", "worktree", "notes", "bisect", "remote"]);
const GIT_SUBSUB_READ = new Set(["list", "show", "get", "get-all", "get-regexp", "-l", "--list", "--get", "-v", "--verbose", "log"]);
// `git branch foo` / `git tag v1` CREATE; with one of these flags a
// positional is a filter instead.
const GIT_REF_LIST_FLAGS = new Set([
  "-l", "--list", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at",
]);
// Any git subcommand: flags that write a file or run a program.
const GIT_EXEC_OR_WRITE = /^(--output|--ext-diff|--open-files-in-pager|-O|--textconv|--exec|--upload-pack|--receive-pack|--config-env)/;

function isVersionProbe(argv: string[]): boolean {
  return argv.length === 2 && ["--version", "-v", "-V", "--help", "-h"].includes(argv[1]);
}

// Tokenize one segment, respecting quotes well enough that quoted separators
// never masquerade as real ones. Unbalanced quotes → null (reject).
function tokenize(segment: string): string[] | null {
  const out: string[] = [];
  let cur = "";
  let quote: string | null = null;
  for (let i = 0; i < segment.length; i++) {
    const c = segment[i];
    if (quote) {
      if (c === "\\" && quote === '"') { cur += segment[++i] ?? ""; continue; }
      if (c === quote) { quote = null; continue; }
      cur += c;
      continue;
    }
    if (c === "'" || c === '"') { quote = c; continue; }
    if (c === "\\") { cur += segment[++i] ?? ""; continue; }
    // Unquoted parens left after extractSubstitutions are subshells, zsh glob
    // qualifiers (`*(e:'cmd':)` runs cmd), or `${(e)var}` flags — reject.
    if (c === "(" || c === ")") return null;
    if (/\s/.test(c)) { if (cur) { out.push(cur); cur = ""; } continue; }
    cur += c;
  }
  if (quote) return null;
  if (cur) out.push(cur);
  return out;
}

// Split on shell separators that are OUTSIDE quotes. Anything hidden inside
// quotes stays in its segment, where it either parses as a normal argument or
// trips the head check — both safe outcomes.
function splitSegments(command: string): string[] | null {
  const segs: string[] = [];
  let cur = "";
  let quote: string | null = null;
  for (let i = 0; i < command.length; i++) {
    const c = command[i];
    if (quote) {
      if (c === "\\" && quote === '"') { cur += c + (command[++i] ?? ""); continue; }
      if (c === quote) quote = null;
      cur += c;
      continue;
    }
    if (c === "'" || c === '"') { quote = c; cur += c; continue; }
    if (c === "\\") { cur += c + (command[++i] ?? ""); continue; }
    // `2>&1`, `<&0`, `&>/dev/null` are redirections, not separators
    if (c === "&" && (command[i - 1] === ">" || command[i - 1] === "<" || command[i + 1] === ">")) { cur += c; continue; }
    if (c === ";" || c === "\n" || c === "&" || c === "|") {
      if ((c === "&" || c === "|") && command[i + 1] === c) i++; // && ||
      segs.push(cur);
      cur = "";
      continue;
    }
    cur += c;
  }
  if (quote) return null;
  segs.push(cur);
  return segs.filter((s) => s.trim());
}

// Pull out $( … ) and ` … ` bodies so they get classified as commands in
// their own right rather than passing as inert argument text. <( … ), >( … )
// and zsh's =( … ) are process substitutions — same treatment.
function extractSubstitutions(command: string): { stripped: string; inner: string[] } | null {
  const inner: string[] = [];
  let stripped = "";
  for (let i = 0; i < command.length; i++) {
    if ("$<>=".includes(command[i]) && command[i + 1] === "(") {
      let depth = 1;
      let j = i + 2;
      let body = "";
      while (j < command.length && depth > 0) {
        if (command[j] === "(") depth++;
        else if (command[j] === ")") { depth--; if (!depth) break; }
        body += command[j++];
      }
      if (depth) return null; // unbalanced — reject
      inner.push(body);
      i = j;
      stripped += "SUBST";
      continue;
    }
    if (command[i] === "`") {
      const end = command.indexOf("`", i + 1);
      if (end === -1) return null;
      inner.push(command.slice(i + 1, end));
      i = end;
      stripped += "SUBST";
      continue;
    }
    stripped += command[i];
  }
  return { stripped, inner };
}

// A redirection writes to the filesystem. /dev/null and fd-dup are fine.
function hasWritingRedirect(segment: string): boolean {
  const cleaned = segment
    .replace(/[12]?>>?\s*\/dev\/(null|stderr|stdout)/g, "")
    .replace(/[12]>&[12]/g, "")
    .replace(/&>\s*\/dev\/null/g, "");
  return />/.test(cleaned);
}

const positionals = (args: string[]) => args.filter((a) => !a.startsWith("-"));

function segmentIsReadOnly(segment: string): boolean {
  if (hasWritingRedirect(segment)) return false;
  const tokens = tokenize(segment);
  if (!tokens || !tokens.length) return false;

  // drop leading VAR=value assignments
  let i = 0;
  while (i < tokens.length && /^[A-Za-z_][A-Za-z0-9_]*=/.test(tokens[i])) i++;
  const argv = tokens.slice(i);
  if (!argv.length) return false; // bare assignment mutates shell state

  const head = argv[0].replace(/^.*\//, ""); // /bin/ls → ls
  const args = argv.slice(1);
  if (head === "sudo" || head === "doas" || head === "env" && args.length) return false;
  if (!READ_ONLY_HEADS.has(head)) return false;

  // Heads whose flags write files or run programs.
  switch (head) {
    case "find": // can execute, delete, or write its listing to a file
      if (args.some((a) => /^-(exec|execdir|delete|ok|okdir|fprint|fprint0|fprintf|fls)$/.test(a))) return false;
      break;
    case "sed": // -i edits in place; the w/W commands write files, e runs one
      if (args.some((a) => a === "--in-place" || /^-[a-zA-Z]*i/.test(a))) return false;
      if (args.some((a) => /(^|[\s;}\/0-9$])[wWe](\s|$|;)/.test(a))) return false;
      break;
    case "awk": // system(), getline from a command, pipes, -f program files
      if (args.some((a) => a === "-f" || /system|getline|\|/.test(a))) return false;
      break;
    case "tee": // tee's whole job is writing
      return false;
    case "sort":
      if (args.some((a) => a.startsWith("--output") || /^-[a-zA-Z]*o/.test(a))) return false;
      break;
    case "uniq": // `uniq in out` writes out
      if (positionals(args).length > 1) return false;
      break;
    case "tree":
    case "base64":
      if (args.some((a) => a === "-o" || a.startsWith("--output"))) return false;
      break;
    case "xxd": // -r patches; a second positional is an output file
      if (args.some((a) => /^-[a-zA-Z]*r/.test(a)) || positionals(args).length > 1) return false;
      break;
    case "yq":
      if (args.some((a) => a === "--inplace" || /^-[a-zA-Z]*i/.test(a))) return false;
      break;
    case "rg": // --pre runs a program on every file
      if (args.some((a) => a.startsWith("--pre"))) return false;
      break;
    case "bat":
      if (args.some((a) => a.startsWith("--pager"))) return false;
      break;
    case "date": // anything but +FORMAT sets the clock
    case "hostname":
      if (positionals(args).some((a) => !a.startsWith("+"))) return false;
      break;
    case "tmux": // #(…) in a format string runs a shell command
      if (args.some((a) => a.includes("#("))) return false;
      break;
    case "cd":
      return true;
  }

  const subs = SUBCOMMANDS[head];
  if (!subs) return true; // plain read-only binary, no subcommand grammar
  if (isVersionProbe(argv)) return true;
  const sub = args.find((a) => !a.startsWith("-"));
  const subOrFlag = sub ?? args[0];
  if (!subOrFlag || !subs.has(subOrFlag)) return false;
  const rest = args.slice(args.indexOf(subOrFlag) + 1);
  const verb = rest.find((a) => !a.startsWith("-"));

  if (head === "git") {
    if (args.some((a) => GIT_EXEC_OR_WRITE.test(a))) return false;
    if (GIT_BARE_ONLY.has(subOrFlag)) {
      if (rest.some((a) => a.startsWith("-") && WRITE_FLAGS.has(a))) return false;
      // `git config foo.bar value` writes; `git config --get foo.bar` reads
      if (subOrFlag === "config" && !rest.some((a) => GIT_SUBSUB_READ.has(a))) return false;
      if (["stash", "worktree", "notes", "bisect"].includes(subOrFlag)) {
        if (!verb || !GIT_SUBSUB_READ.has(verb)) return false;
      }
      // `git branch foo` / `git tag v1` create a ref unless a list flag
      // turns the positional into a pattern
      if ((subOrFlag === "branch" || subOrFlag === "tag") && verb && !rest.some((a) => GIT_REF_LIST_FLAGS.has(a.split("=")[0])))
        return false;
      // `git remote add/set-url/remove/rename/prune` write
      if (subOrFlag === "remote" && verb && !["show", "get-url"].includes(verb)) return false;
    }
  }
  if (head === "gh") {
    if (subOrFlag === "api") {
      if (rest.some((a) => /^(-X|--method|-f|--field|-F|--raw-field|--input)(=|$)/.test(a))) return false;
    } else if (!verb || !["view", "list", "diff", "checks", "status", "ls"].includes(verb)) {
      return false;
    }
  }
  if (head === "kubectl" && subOrFlag === "config") {
    if (!verb || !["view", "get-contexts", "current-context"].includes(verb)) return false;
  }
  if (head === "npm" && subOrFlag === "config") {
    if (!verb || !["get", "list", "ls"].includes(verb)) return false;
  }
  if (head === "npm" && subOrFlag === "audit" && verb) return false; // `npm audit fix`
  if (head === "go" && subOrFlag === "env" && rest.some((a) => a === "-w" || a === "-u")) return false;
  if (head === "terraform" && subOrFlag === "fmt" && !rest.includes("-check")) return false;
  if (head === "claude") {
    if (subOrFlag === "mcp" && (!verb || !["list", "get"].includes(verb))) return false;
    if (subOrFlag === "config" && (!verb || !["get", "list", "ls"].includes(verb))) return false;
  }
  return true;
}

export function isReadOnlyCommand(command: string): boolean {
  if (!command.trim()) return false;
  const ex = extractSubstitutions(command);
  if (!ex) return false;
  const parts = [ex.stripped, ...ex.inner];
  for (const part of parts) {
    const segs = splitSegments(part);
    if (!segs) return false;
    for (const seg of segs) {
      // a substitution body may itself contain substitutions
      const nested = extractSubstitutions(seg);
      if (!nested) return false;
      if (nested.inner.length && !nested.inner.every(isReadOnlyCommand)) return false;
      if (!segmentIsReadOnly(nested.stripped)) return false;
    }
  }
  return true;
}

// ── auto mode: reads that still deserve the classifier ────────────────────
// Credential stores, secret-shaped names, personal data stores, and the pty
// server's own plaintext command/output logs. Deliberately loose: a false
// match only costs one classifier call.
const SENSITIVE = new RegExp(
  [
    String.raw`\.ssh\b`, String.raw`\.aws\b`, String.raw`\.gnupg`, String.raw`\.netrc`,
    String.raw`\.npmrc`, String.raw`\.pypirc`, String.raw`\.git-credentials`,
    String.raw`(^|[\s/'"=])\.(env|envrc|dev\.vars)\b`,String.raw`\.(pem|p12|pfx|key)\b`, String.raw`id_(rsa|ed25519|ecdsa|dsa)`,
    String.raw`\.kube/config`, String.raw`\.docker/config`, String.raw`gh/hosts\.yml`,
    String.raw`\.config/op\b`, "1password", "op://", String.raw`\.claude\.json`,
    "tmux-pty-mcp/", "keychain", "cookies", "login data",
    String.raw`library/(messages|mail)\b`, String.raw`chat\.db`,
    "credential", String.raw`secrets?([^a-z]|$)`, "passw",
    String.raw`(^|[^a-z])(api[_-]?)?tokens?([^a-z]|$)`,
    // environment dumps and secret-named variables
    String.raw`(^|[\s;|&(])(env|printenv)(\s|$|;|\|)`, String.raw`\$ENV\b`, String.raw`(^|[^.\w])env\.`,
    String.raw`\$\{?[A-Z_]*(TOK|KEY|SECRET|PASS|AUTH|CRED|SESSION|COOKIE)`,
  ].join("|"),
  "i",
);

export function isSensitive(command: string): boolean {
  if (SENSITIVE.test(command)) return true;
  // DNS lookups carry their argument off the machine: literal names only
  const heads = command.split(/[;&|\n(`]/).map((s) => s.trim().split(/\s+/)[0]?.replace(/^.*\//, ""));
  if (heads.some((h) => h && NETWORK_HEADS.has(h)) && /[$`]/.test(command)) return true;
  return false;
}

export function decide(input: HookInput): "allow" | null {
  const mode = input.permission_mode;
  if (mode !== "plan" && mode !== "auto") return null;
  const command = input.tool_input?.command;
  if (typeof command !== "string" || !isReadOnlyCommand(command)) return null;
  if (mode === "auto" && isSensitive(command)) return null;
  return "allow";
}

// ── hook entrypoint ───────────────────────────────────────────────────────
if (import.meta.main) {
  let raw = "";
  for await (const chunk of Bun.stdin.stream()) raw += Buffer.from(chunk).toString();
  let input: HookInput = {};
  try {
    input = JSON.parse(raw);
  } catch {
    process.exit(0); // unparseable → no opinion
  }
  if (decide(input) !== "allow") process.exit(0);
  console.log(
    JSON.stringify({
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "allow",
        permissionDecisionReason: `Read-only command is allowed in ${input.permission_mode} mode (same rule Claude Code applies to Bash)`,
      },
    }),
  );
}
