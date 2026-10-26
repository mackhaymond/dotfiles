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
    // aliases, same rules as their long forms; show-window-options is
    // show-options -w
    "show", "show-window-options", "showw", "display", "showenv", "lsk", "lscm",
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
  "--edit-description", "-e", "--unset-all", "--rename-section", "--remove-section",
]);
const GIT_BARE_ONLY = new Set(["branch", "tag", "config", "stash", "worktree", "notes", "bisect", "remote"]);
const GIT_SUBSUB_READ = new Set(["list", "show", "get", "get-all", "get-regexp", "-l", "--list", "--get", "-v", "--verbose", "log"]);
// `git config` reads only as new-style `list`/`get` (the very next word) or
// legacy `--get`/`-l`/`--list`. A read word anywhere else is just a value:
// `git config user.name get` sets user.name to "get". Write actions
// (--add, --unset, -e …) are already refused through WRITE_FLAGS.
function gitConfigIsRead(rest: string[]): boolean {
  if (rest[0] === "list" || rest[0] === "get") return true;
  const verb = positionals(rest)[0];
  if (verb && ["set", "unset", "rename-section", "remove-section", "edit"].includes(verb)) return false;
  return rest.some((a) => a === "--get" || a === "-l" || a === "--list");
}

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

// Unquoted constructs whose quoting or extent this parser doesn't model.
// Each one lets the shell's idea of "quoted" drift from ours, which is how a
// live `;` ends up looking like argument text, so reject them outright:
//   $'…'  ANSI-C quoting, where \' does NOT close the string
//   #     a comment at word start hides its quotes from the shell, not from us
//         (a trailing one on the LAST line is handled by isReadOnlyCommand)
//   <<    a heredoc body is data to the shell, commands-and-quotes to us
//         (`<<<` herestrings are a single word and stay allowed)
const isCommentStart = (s: string, i: number) => s[i] === "#" && (i === 0 || /[\s;&|()<>]/.test(s[i - 1]));
function unmodeled(s: string, i: number, hashLiteral = false): boolean {
  const c = s[i];
  if (c === "$" && s[i + 1] === "'") return true;
  if (!hashLiteral && isCommentStart(s, i)) return true;
  if (c === "<" && s[i + 1] === "<" && s[i + 2] !== "<" && s[i - 1] !== "<") return true;
  return false;
}

// End of a ` … ` body starting at `start`, or -1. A backslash-escaped
// backtick inside one is a NESTED substitution the shell runs after
// unescaping, which we can't see; reject it.
function findBacktick(s: string, start: number): number {
  for (let i = start; i < s.length; i++) {
    if (s[i] === "\\" && s[i + 1] === "`") return -1;
    if (s[i] === "`") return i;
  }
  return -1;
}

// Index of the `)` closing a $( … ) body that starts at `start`, or -1.
// Quote-aware: parens inside quotes don't count, and a $( … ) inside double
// quotes opens a fresh quoting context.
function findClose(s: string, start: number): number {
  let depth = 1;
  let quote: '"' | "'" | null = null;
  for (let i = start; i < s.length; i++) {
    const c = s[i];
    if (quote === "'") { if (c === "'") quote = null; continue; }
    if (c === "\\") { i++; continue; }
    if (!quote && unmodeled(s, i)) return -1;
    if (quote && c === "$" && s[i + 1] === "(") {
      const end = findClose(s, i + 2);
      if (end < 0) return -1;
      i = end;
      continue;
    }
    if (c === "`") {
      const end = findBacktick(s, i + 1);
      if (end < 0) return -1;
      i = end;
      continue;
    }
    if (c === '"') { quote = quote ? null : '"'; continue; }
    if (c === "'" && !quote) { quote = "'"; continue; }
    if (quote) continue;
    if (c === "(") depth++;
    else if (c === ")" && --depth === 0) return i;
  }
  return -1;
}

// Pull out $( … ) and ` … ` bodies so they get classified as commands in
// their own right rather than passing as inert argument text. <( … ), >( … )
// and zsh's =( … ) are process substitutions — same treatment. Tracks quotes
// the way the shell does: nothing is live inside single quotes, $( … ) and
// backticks are still live inside double quotes, process substitutions only
// outside quotes. Anything it can't model → null (reject).
//
// A trailing comment (unquoted word-start `#` with no newline after it) ends
// the scan and sets `comment`. With `hashLiteral`, `#` is ordinary text
// instead: that's how zsh reads it when interactive_comments is off, which
// is the default for an interactive shell like the pty's.
function extractSubstitutions(
  command: string,
  hashLiteral = false,
): { stripped: string; inner: string[]; comment: boolean } | null {
  const inner: string[] = [];
  let stripped = "";
  let quote: '"' | "'" | null = null;
  for (let i = 0; i < command.length; i++) {
    const c = command[i];
    if (quote === "'") { if (c === "'") quote = null; stripped += c; continue; }
    if (c === "\\") { stripped += c + (command[i + 1] ?? ""); i++; continue; }
    if (!quote && !hashLiteral && isCommentStart(command, i) && !command.includes("\n", i))
      return { stripped, inner, comment: true };
    if (!quote && unmodeled(command, i, hashLiteral)) return null;
    if (command[i + 1] === "(" && (c === "$" || (!quote && "<>=".includes(c)))) {
      const end = findClose(command, i + 2);
      if (end < 0) return null; // unbalanced or unmodeled — reject
      inner.push(command.slice(i + 2, end));
      i = end;
      stripped += "SUBST";
      continue;
    }
    if (c === "`") {
      const end = findBacktick(command, i + 1);
      if (end < 0) return null;
      inner.push(command.slice(i + 1, end));
      i = end;
      stripped += "SUBST";
      continue;
    }
    if (c === '"') quote = quote ? null : '"';
    else if (c === "'" && !quote) quote = "'";
    stripped += c;
  }
  if (quote) return null;
  return { stripped, inner, comment: false };
}

// A redirection writes to the filesystem. /dev/null and fd-dup are fine.
// The target must END there (`> /dev/nullx`, `> /dev/null/../x` are files).
function hasWritingRedirect(segment: string): boolean {
  const END = String.raw`(?=[\s;|&)]|$)`;
  const cleaned = segment
    .replace(new RegExp(String.raw`[12]?>>?\s*\/dev\/(null|stderr|stdout)` + END, "g"), "")
    .replace(new RegExp(String.raw`[12]>&[12]` + END, "g"), "")
    .replace(new RegExp(String.raw`&>\s*\/dev\/null` + END, "g"), "");
  return />/.test(cleaned);
}

const positionals = (args: string[]) => args.filter((a) => !a.startsWith("-"));

// `--out=x` is `--output=x` to GNU getopt_long, which takes any unambiguous
// prefix of a long option. True when `arg` is a (possibly abbreviated) `--full`.
function longOpt(arg: string, full: string): boolean {
  if (!arg.startsWith("--")) return false;
  const name = arg.slice(2).split("=")[0];
  return name.length > 0 && full.startsWith(name);
}

// A short-flag cluster: `-dr` is `-d -r`.
const shortCluster = (a: string) => /^-[a-zA-Z]{2,}$/.test(a);

// Leading VAR=value assignments that can't point the command at a program
// (GIT_PAGER, GIT_EXTERNAL_DIFF, DYLD_INSERT_LIBRARIES, LESSOPEN … all can).
const SAFE_ASSIGNMENT = /^((LC_[A-Z]+|LANG|LANGUAGE|TZ|NO_COLOR|CLICOLOR|FORCE_COLOR|COLUMNS|LINES)=|(GIT_)?PAGER=cat$)/;

// Global flags that may sit between a head and its subcommand. Anything else
// there could take a separate value and swallow the word we read as the
// subcommand (`tmux -f ls kill-server`, `git -C status commit`), so it's
// rejected. `--flag=value` is self-contained and checked as before.
const SAFE_GLOBAL_FLAGS: Record<string, Set<string>> = {
  git: new Set(["--no-pager", "-P", "--paginate", "-p", "--no-optional-locks", "--literal-pathspecs", "--no-replace-objects"]),
  tmux: new Set(["-2", "-u"]),
  npm: new Set(["-g", "--global"]),
};

// Value-taking global flags we understand, consumed as flag+value pairs
// before looking for the subcommand. tmux `-L name` / `-S path` only pick
// the server; `-f` loads a config that can run-shell, so only `-f /dev/null`.
// git `-C dir` (repeatable) only changes directory; `-c k=v` is NOT here
// (it can set core.pager and friends).
const GLOBAL_PAIRS: Record<string, (flag: string, value: string | undefined) => boolean> = {
  tmux: (f, v) => (f === "-L" || f === "-S") ? v !== undefined : f === "-f" && v === "/dev/null",
  git: (f, v) => f === "-C" && v !== undefined,
};
const GLOBAL_ATTACHED: Record<string, RegExp> = {
  tmux: /^-(L.+|S.+|f\/dev\/null)$/,
};

// Drop leading global flags (safe ones and understood pairs) so the first
// remaining word is the subcommand. A `-f` that isn't /dev/null → null.
function stripGlobalPairs(head: string, args: string[]): string[] | null {
  const pair = GLOBAL_PAIRS[head];
  if (!pair) return args;
  let k = 0;
  while (k < args.length && args[k].startsWith("-")) {
    const f = args[k];
    if (SAFE_GLOBAL_FLAGS[head]?.has(f) || GLOBAL_ATTACHED[head]?.test(f)) { k++; continue; }
    if (pair(f, args[k + 1])) { k += 2; continue; }
    if (head === "tmux" && f.startsWith("-f")) return null;
    break; // leave it for the caller's checks
  }
  return args.slice(k);
}

// ── sed ───────────────────────────────────────────────────────────────────
// sed's w/W commands and s///w write files; e and s///e run a command. Its
// delimiters can be any character (sXaXbXw f), so a regex over the script
// can't find them reliably: parse the script instead. Anything the parser
// doesn't recognise, and anything GNU and BSD sed read differently, is
// rejected — a false "unsafe" only costs a classifier call.

// Flags allowed on the command line: -n -E -r -s -u -z (no value), and -e
// with a script. -i/-I edit in place, -f reads a script file, and -l takes a
// value on GNU but not BSD, so the next word may or may not be the script.
const SED_SHORT_OK = new Set(["n", "E", "r", "s", "u", "z"]);
const SED_LONG_OK = new Set([
  "--quiet", "--silent", "--regexp-extended", "--separate", "--unbuffered",
  "--null-data", "--zero-terminated", "--posix", "--debug", "--sandbox", "--version", "--help",
]);

function sedArgsAreSafe(args: string[]): boolean {
  const scripts: string[] = [];
  const operands: string[] = [];
  let sawE = false;
  for (let k = 0; k < args.length; k++) {
    const a = args[k];
    if (a === "--") { operands.push(...args.slice(k + 1)); break; }
    if (a === "-" || !a.startsWith("-")) { operands.push(a); continue; }
    if (a === "--expression" || a.startsWith("--expression=")) {
      const v = a === "--expression" ? args[++k] : a.slice("--expression=".length);
      if (v === undefined) return false;
      scripts.push(v);
      sawE = true;
      continue;
    }
    if (a.startsWith("--")) { if (!SED_LONG_OK.has(a)) return false; continue; }
    // short cluster: `-ne script`, `-es/a/b/`
    for (let j = 1; j < a.length; j++) {
      if (a[j] === "e") {
        const v = a.slice(j + 1) || args[++k];
        if (v === undefined) return false;
        scripts.push(v);
        sawE = true;
        break;
      }
      if (!SED_SHORT_OK.has(a[j])) return false;
    }
  }
  if (!sawE) {
    if (!operands.length) return args.length === 1 && (args[0] === "--version" || args[0] === "--help");
    scripts.push(operands[0]);
  }
  return scripts.every(sedScriptIsSafe);
}

// Parse one sed script. True only if every command is a known one that
// neither writes nor executes.
function sedScriptIsSafe(script: string): boolean {
  const s = script;
  let i = 0;
  const ws = () => { while (i < s.length && (s[i] === " " || s[i] === "\t")) i++; };

  // Read a delimited part (regex or replacement) up to the closing `d`.
  // In a regex, a delimiter inside a bracket expression is literal to BSD
  // sed but not to GNU, so that case is rejected rather than guessed.
  const part = (d: string, isRegex: boolean): boolean => {
    let bracket = false;
    for (; i < s.length; i++) {
      const c = s[i];
      if (c === "\\") { i++; continue; }
      if (c === "\n" && !isRegex) return false;
      if (isRegex && !bracket && c === "[") { bracket = true; if (s[i + 1] === "^") i++; if (s[i + 1] === "]") i++; continue; }
      if (bracket && c === "[" && /[:.=]/.test(s[i + 1] ?? "")) {
        // [:alpha:] / [.x.] / [=e=] inside a bracket: its `]` doesn't close it
        const close = s.indexOf(s[i + 1] + "]", i + 2);
        if (close < 0) return false;
        i = close + 1;
        continue;
      }
      if (bracket && c === "]") { bracket = false; continue; }
      if (c === d) { if (bracket) return false; i++; return true; }
    }
    return false;
  };
  // A delimiter that is a word character or whitespace reads differently
  // across seds (and `sXaXbXw` is exactly how the old regex was dodged).
  const delim = (): string | null => {
    const d = s[i];
    if (d === undefined || /[\sA-Za-z0-9_\\]/.test(d)) return null;
    i++;
    return d;
  };
  const address = (): boolean => {
    if (/[0-9]/.test(s[i] ?? "")) { while (/[0-9~]/.test(s[i] ?? "")) i++; return true; }
    if (s[i] === "$") { i++; return true; }
    if (s[i] === "/" || s[i] === "\\") {
      if (s[i] === "\\") i++;
      const d = delim();
      if (!d || !part(d, true)) return false;
      while (s[i] === "I" || s[i] === "M") i++;
      return true;
    }
    return true; // no address
  };
  // After a command: optional blanks, then end, newline, `;`, `}` or `#`.
  const end = (): boolean => {
    ws();
    return i >= s.length || s[i] === "\n" || s[i] === ";" || s[i] === "}" || s[i] === "#";
  };
  // Only a/i/c text continues past a backslash-newline; after r/R or `#`, BSD
  // ends the line there and runs the next one as a command.
  const toEol = (cont = false) => { while (i < s.length && s[i] !== "\n") { if (cont && s[i] === "\\") i++; i++; } };

  while (i < s.length) {
    ws();
    const c0 = s[i];
    if (c0 === undefined) break;
    if (c0 === "\n" || c0 === ";" || c0 === "}") { i++; continue; }
    if (c0 === "#") { toEol(); continue; }
    if (!address()) return false;
    if (s[i] === ",") {
      i++;
      if (s[i] === "+" || s[i] === "~") { i++; if (!/[0-9]/.test(s[i] ?? "")) return false; }
      if (!address()) return false;
    }
    ws();
    while (s[i] === "!") { i++; ws(); }
    const cmd = s[i++];
    switch (cmd) {
      case "{":
        continue;
      case "s": {
        const d = delim();
        if (!d || !part(d, true) || !part(d, false)) return false;
        // flags: g p N i I m M; w writes, e executes
        while (i < s.length && /[gpiImM0-9]/.test(s[i])) i++;
        if (!end()) return false;
        continue;
      }
      case "y": {
        const d = delim();
        if (!d || !part(d, false) || !part(d, false)) return false;
        if (!end()) return false;
        continue;
      }
      case "a": case "i": case "c": // text runs to the end of the line, `\`-newline continues it
        toEol(true);
        continue;
      case "r": case "R": // read a file: the name runs to the end of the line
        toEol();
        continue;
      case ":": case "b": case "t": case "T": {
        // Label. BSD runs it to the end of the line, GNU stops at `;`: stop
        // at the earlier one so a command after `;` still gets checked.
        ws();
        while (i < s.length && !/[\s;}]/.test(s[i])) i++;
        if (!end()) return false;
        continue;
      }
      case "l": case "q": case "Q": case "L":
        ws();
        while (/[0-9]/.test(s[i] ?? "")) i++;
        if (!end()) return false;
        continue;
      case "=": case "d": case "D": case "g": case "G": case "h": case "H":
      case "n": case "N": case "p": case "P": case "x": case "z": case "F":
        if (!end()) return false;
        continue;
      default: // w W e, and anything unknown
        return false;
    }
  }
  return true;
}

function segmentIsReadOnly(segment: string): boolean {
  if (hasWritingRedirect(segment)) return false;
  const tokens = tokenize(segment);
  if (!tokens || !tokens.length) return false;

  // drop leading VAR=value assignments — only harmless ones
  let i = 0;
  while (i < tokens.length && /^[A-Za-z_][A-Za-z0-9_]*=/.test(tokens[i])) {
    if (!SAFE_ASSIGNMENT.test(tokens[i])) return false;
    i++;
  }
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
    case "sed": // -i/-I edit in place, -f reads a script we can't see, and
      // the scripts are parsed command by command (see sedArgsAreSafe)
      if (!sedArgsAreSafe(args)) return false;
      break;
    case "awk": // system(), getline from a command, pipes; and every flag but
      // -F/-v: -f/-E/-i load program files (gawk -i inplace edits files),
      // -l loads a shared library, -d/-o/-p write dump/profile files
      if (args.some((a) => a.startsWith("-") && a !== "-" && a !== "--version" && !/^-[Fv]/.test(a))) return false;
      if (args.some((a) => /system|getline|\|/.test(a))) return false;
      break;
    case "tee": // tee's whole job is writing
      return false;
    case "sort": // -o writes; --compress-program runs a program
      if (args.some((a) => /^-[a-zA-Z]*o/.test(a) || longOpt(a, "output") || longOpt(a, "compress-program"))) return false;
      break;
    case "uniq": // `uniq in out` writes out
      if (positionals(args).length > 1) return false;
      break;
    case "tree": // -o writes; -R re-runs tree with -o 00Tree.html per directory
      if (args.some((a) => /^-[a-zA-Z]*[oR]/.test(a) || longOpt(a, "output"))) return false;
      break;
    case "base64":
      if (args.some((a) => /^-[a-zA-Z]*o/.test(a) || longOpt(a, "output"))) return false;
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
    case "date": // anything but +FORMAT sets the clock, and so does GNU -s/--set
      if (positionals(args).some((a) => !a.startsWith("+"))) return false;
      if (args.some((a) => /^-[a-zA-Z]*s/.test(a) || longOpt(a, "set"))) return false;
      break;
    case "hostname": // any operand sets the name; GNU -F/--file sets it from a file
      if (positionals(args).length) return false;
      if (args.some((a) => /^-[a-zA-Z]*F/.test(a) || longOpt(a, "file"))) return false;
      break;
    case "tmux": // #(…) in a format string runs a shell command; an argument
      // that is or ends in `;` (`\;`, `';'`) chains another tmux command
      if (args.some((a) => a.includes("#(") || a.endsWith(";"))) return false;
      break;
    case "cd":
      return true;
  }

  const subs = SUBCOMMANDS[head];
  if (!subs) return true; // plain read-only binary, no subcommand grammar
  if (isVersionProbe(argv)) return true;
  const cargs = stripGlobalPairs(head, args);
  if (!cargs) return false;
  const sub = cargs.find((a) => !a.startsWith("-"));
  const subOrFlag = sub ?? cargs[0];
  if (!subOrFlag || !subs.has(subOrFlag)) return false;
  if (sub && cargs.slice(0, cargs.indexOf(sub)).some((f) => !f.includes("=") && !SAFE_GLOBAL_FLAGS[head]?.has(f)))
    return false;
  const rest = cargs.slice(cargs.indexOf(subOrFlag) + 1);
  const verb = rest.find((a) => !a.startsWith("-"));
  // Where the verb decides read vs write, it must come straight after the
  // subcommand: a flag in between may take a separate value and swallow the
  // word we'd check (`gh pr -R list merge 42` runs merge).
  const verbIs = (ok: string[]) => !!verb && rest[0] === verb && ok.includes(verb);

  // display-message -I forwards stdin into a pane, i.e. types into it
  if (head === "tmux" && (subOrFlag === "display-message" || subOrFlag === "display")) {
    if (rest.some((a) => /^-[a-zA-Z]*I/.test(a))) return false;
  }

  if (head === "git") {
    if (args.some((a) => GIT_EXEC_OR_WRITE.test(a))) return false;
    if (GIT_BARE_ONLY.has(subOrFlag)) {
      // `--set-upstream-to=x` is `--set-upstream-to`; `-dr` is `-d -r`
      const writeFlag = (a: string) =>
        WRITE_FLAGS.has(a.split("=")[0]) || (shortCluster(a) && [...a.slice(1)].some((ch) => WRITE_FLAGS.has(`-${ch}`)));
      if (rest.some((a) => a.startsWith("-") && writeFlag(a))) return false;
      if (subOrFlag === "config" && !gitConfigIsRead(rest)) return false;
      if (["stash", "worktree", "notes", "bisect"].includes(subOrFlag)) {
        if (!verbIs([...GIT_SUBSUB_READ])) return false;
      }
      // `git branch foo` / `git tag v1` create a ref unless a list flag
      // turns the positional into a pattern. --format/--sort/-F take a
      // separate value, which can swallow the list flag
      // (`git branch --format --list newbranch` creates newbranch).
      if ((subOrFlag === "branch" || subOrFlag === "tag") && verb) {
        if (!rest.some((a) => GIT_REF_LIST_FLAGS.has(a.split("=")[0]))) return false;
        if (rest.some((a) => ["--format", "--sort", "-F", "--file"].includes(a))) return false;
      }
      // `git remote add/set-url/remove/rename/prune` write
      if (subOrFlag === "remote" && verb && !["show", "get-url"].includes(verb)) return false;
    }
    // `git reflog expire/delete/drop` rewrite the reflog; a bare `git reflog`
    // (or one led by flags) is `show`
    if (subOrFlag === "reflog" && verb && !verbIs(["show", "list", "exists"])) return false;
    // `git symbolic-ref HEAD <ref>` repoints HEAD, -d deletes it; reading
    // takes exactly one name and no other flags
    if (subOrFlag === "symbolic-ref") {
      if (rest.some((a) => a.startsWith("-") && !["-q", "--quiet", "--short", "--no-recurse", "--recurse"].includes(a))) return false;
      if (positionals(rest).length !== 1) return false;
    }
  }
  if (head === "gh") {
    if (subOrFlag === "api") {
      // pflag accepts attached short values (-XDELETE, -ftitle=x) and short
      // clusters (-iXPOST); any -f/-F field also makes gh default to POST
      const mutating = (a: string) =>
        /^--(method|field|raw-field|input)(=|$)/.test(a) || (/^-[^-]/.test(a) && /[XfF]/.test(a));
      if (rest.some(mutating)) return false;
    } else if (!verbIs(["view", "list", "diff", "checks", "status", "ls"])) {
      return false;
    }
  }
  if (head === "kubectl" && subOrFlag === "config") {
    if (!verbIs(["view", "get-contexts", "current-context"])) return false;
  }
  if (head === "npm" && subOrFlag === "config") {
    if (!verbIs(["get", "list", "ls"])) return false;
  }
  if (head === "npm" && subOrFlag === "audit" && verb) return false; // `npm audit fix`
  if (head === "go" && subOrFlag === "env" && rest.some((a) => /^--?[wu](=|$)/.test(a))) return false;
  if (head === "terraform" && subOrFlag === "fmt" && !rest.includes("-check")) return false;
  // `terraform providers lock|mirror` write lock files / a mirror directory
  if (head === "terraform" && subOrFlag === "providers" && verb && !verbIs(["schema"])) return false;
  if (head === "claude") {
    if (subOrFlag === "mcp" && !verbIs(["list", "get"])) return false;
    if (subOrFlag === "config" && !verbIs(["get", "list", "ls"])) return false;
  }
  return true;
}

function classify(ex: ReturnType<typeof extractSubstitutions>): boolean {
  if (!ex) return false;
  // each substitution body is a full command line of its own (fresh quoting,
  // possibly more substitutions inside)
  if (!ex.inner.every(isReadOnlyCommand)) return false;
  const segs = splitSegments(ex.stripped);
  if (!segs || !segs.length) return false;
  return segs.every(segmentIsReadOnly);
}

export function isReadOnlyCommand(command: string): boolean {
  if (!command.trim()) return false;
  const ex = extractSubstitutions(command);
  // A trailing comment is a comment only if the shell has
  // interactive_comments on; otherwise its words are more arguments
  // (`tmux ls # \; kill-server`). Both readings must be read-only.
  if (ex?.comment && !classify(extractSubstitutions(command, true))) return false;
  return classify(ex);
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
