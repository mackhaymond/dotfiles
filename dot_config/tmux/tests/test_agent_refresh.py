"""scripts/agent-refresh.sh: idle, outdated Claude Code tabs restarted in place.

Drives the real script under /bin/bash (3.2, as the LaunchAgent runs it), with
the real stash.sh, closed-tabs.sh and tmux-assistant-resurrect plugin, against
a REAL private tmux server: `-f /dev/null` under a private TMUX_TMPDIR, $TMUX
unset, killed in cleanup; AGENT_REFRESH_ALLOW_SOCKET names it. HOME, TMPDIR
and XDG_STATE_HOME are a temp dir.

The agent is a small C program named `claude` (a script's ps line would start
with its interpreter): it registers itself as Claude Code does
(~/.claude/sessions/<pid>.json, the plugin's claude-<pid>.json), draws an
empty composer, plays the SessionStart and SessionEnd hooks' part on its
window's options, and exits on SIGTERM. Its version is what
~/.local/bin/claude links to, unless FAKE_VERSION says otherwise. SIGUSR1 puts
a draft in its composer. agent-bg-pending is a stub module (one test asks the
real one); home/stall makes every tmux call hang.
"""
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
import unittest
import uuid

REAL_BG = Path.home() / ".local/bin/agent-bg-pending"
SCRIPTS = Path(os.environ.get(
    "AGENT_TMUX_SCRIPTS",
    str(Path.home() / ".local/share/chezmoi/dot_config/tmux/scripts")))
REFRESH = SCRIPTS / "executable_agent-refresh.sh"
PLUGIN = Path.home() / ".config/tmux/plugins/tmux-assistant-resurrect"
TMUX = shutil.which("tmux")
CC = shutil.which("cc")
HOUR = 3600
NEW, OLD = "2.1.330", "2.1.99"

FAKE_C = r"""
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
static volatile sig_atomic_t stop_now = 0;
static volatile sig_atomic_t draft_now = 0;
static void on_term(int s) { (void)s; stop_now = 1; }
static void on_usr1(int s) { (void)s; draft_now = 1; }
#define RULE "\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\n"
static void draw(const char *draft) {
    printf("hello\n" RULE "\xe2\x9d\xaf %s\n" RULE "  status\n", draft ? draft : "");
    fflush(stdout);
}
static void hook(const char *what, int argc, char **argv) {
    char cmd[8192]; int i; size_t n;
    snprintf(cmd, sizeof cmd, "/usr/bin/env python3 -I \"$HOME/fake.py\" %s %d", what, (int)getpid());
    for (i = 1; i < argc; i++) { n = strlen(cmd); snprintf(cmd + n, sizeof cmd - n, " '%s'", argv[i]); }
    system(cmd);
}
int main(int argc, char **argv) {
    char p[2048];
    signal(SIGTERM, on_term);
    signal(SIGUSR1, on_usr1);
    snprintf(p, sizeof p, "%s/broken", getenv("HOME"));
    if (access(p, F_OK) == 0) { puts("broken"); return 1; }
    hook("start", argc, argv);
    draw(getenv("FAKE_DRAFT"));
    snprintf(p, sizeof p, "%s/ignore-term", getenv("HOME"));
    for (;;) { pause(); if (draft_now) { draft_now = 0; draw("half a thought"); } if (stop_now && access(p, F_OK) != 0) break; stop_now = 0; }
    hook("stop", argc, argv);
    return 0;
}
"""

FAKE_PY = r"""
import json, os, shutil, subprocess, sys, time
what, pid, args = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
H = os.environ["HOME"]
pane = os.environ["TMUX_PANE"]
def t(*a):
    return subprocess.run(["tmux", *a], capture_output=True, text=True).stdout.strip()
where = t("display-message", "-p", "-t", pane, "#{session_name}:#{window_id}.#{pane_id}")
win = t("display-message", "-p", "-t", pane, "#{window_id}")
sess = "%s/.claude/sessions/%d.json" % (H, pid)
rec = "%s/claude-%d.json" % (os.environ["TMUX_ASSISTANT_RESURRECT_DIR"], pid)
if what == "start":
    with open(H + "/argv.log", "a") as f:
        f.write(json.dumps(args) + "\n")
    sid = os.environ.get("FAKE_SID") or args[args.index("--resume") + 1]
    ver = os.environ.get("FAKE_VERSION") or os.path.basename(os.path.realpath(H + "/.local/bin/claude"))
    if os.path.exists(H + "/next-sid"):
        sid = open(H + "/next-sid").read().strip()
    if os.path.exists(H + "/next-version"):
        ver = open(H + "/next-version").read().strip()
    ps = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                        env={"LC_ALL": "C", "TZ": "UTC0", "PATH": "/usr/bin:/bin"}).stdout
    json.dump({"session_id": sid, "model": os.environ.get("FAKE_MODEL", ""),
               "env": json.loads(os.environ.get("FAKE_ENV", "{}"))}, open(rec, "w"))
    if not t("show-options", "-wqv", "-t", win, "@agent_state"):
        t("set-option", "-w", "-t", win, "@agent_kind", "claude", ";",
          "set-option", "-w", "-t", win, "@agent_state", "running" if os.path.exists(H + "/comes-back-busy") else "idle", ";",
          "set-option", "-w", "-t", win, "@agent_since", "%d idle" % time.time(), ";",
          "set-option", "-w", "-t", win, "@agent_summary", "from the hook")
    if os.path.exists(H + "/late-hook"):
        subprocess.Popen(["sh", "-c", "sleep 3; tmux set-option -w -t %s @agent_since \"$(date +%%s) idle\"" % win])
    json.dump({"pid": pid, "sessionId": sid, "cwd": os.getcwd(), "version": ver, "status": "shell", "tmux": where,
               "startedAt": int(time.time() * 1000), "procStart": " ".join(ps.split())}, open(sess + ".tmp", "w"))
    os.rename(sess + ".tmp", sess)
else:
    j = H + "/state/agent-refresh/journal"
    if os.path.isdir(j):
        shutil.copytree(j, H + "/journal-at-exit", dirs_exist_ok=True)
    os.unlink(sess)
    os.unlink(rec)
    if os.path.exists(H + "/select-on-exit"):
        t("select-window", "-t", win)
    for o in "state summary summary_cond summary_key since kind harness detail detail_kind turn_start".split():
        t("set-option", "-uw", "-t", win, "@agent_" + o)
    if os.path.exists(H + "/stall-on-exit"):
        os.rename(H + "/stall-on-exit", H + "/stall")
"""

FAKE_BG = """#!/usr/bin/env python3
import os
MODE = os.path.expanduser("~/bg-mode")
def mode():
    return open(MODE).read().strip() if os.path.exists(MODE) else ""
def _session_chain(path):
    if mode() == "raise":
        raise RuntimeError("boom")
    return os.path.dirname(path), [] if mode() == "nochain" else [os.path.basename(path)[:-6]]
def _pty_windows():
    return None if mode() == "notmux" else ""
def _pty_procs(ttys):
    return []
def agents_pending(path, floor=None, lift=False):
    open(os.path.expanduser("~/bg-args"), "w").write("%r %r" % (floor, lift))
    return mode() == "agents"
def tasks_pending(path, proj, chain, floor=None, lift=False):
    if mode() == "late raise":
        raise KeyError("x")
    return mode() == "tasks"
def subagents_pending(proj, chain, since=0, floor=None):
    return mode() == "subagents"
def workflow_pending(proj, chain, since=0, floor=None):
    return mode() == "workflow"
def ptys_pending(sids, floor=None, lift=False, proj=None, chain=()):
    return mode() == "ptys"
"""

FAKE_NOTIFY = """#!/bin/sh
{ for a in "$@"; do printf '%s\\037' "$a"; done; printf '\\n'; } >> "$HOME/toasts"
"""


def setUpModule():
    global BUILD
    BUILD = None
    if CC:
        BUILD = tempfile.mkdtemp(prefix="agent-refresh-cc-")
        Path(BUILD, "claude.c").write_text(FAKE_C)
        subprocess.run([CC, "-w", "-o", f"{BUILD}/claude", f"{BUILD}/claude.c"], check=True, capture_output=True)


def tearDownModule():
    if BUILD:
        shutil.rmtree(BUILD, ignore_errors=True)


@unittest.skipUnless(TMUX and CC and shutil.which("jq") and (PLUGIN / "scripts/lib-detect.sh").exists(),
                     "tmux, cc, jq and tmux-assistant-resurrect required")
class Base(unittest.TestCase):
    def setUp(self):
        self.home = Path(os.path.realpath(tempfile.mkdtemp(prefix="agent-refresh-")))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.sockdir = Path(tempfile.mkdtemp(prefix="agref-", dir="/private/tmp"))   # short: sun_path
        self.addCleanup(shutil.rmtree, self.sockdir, True)
        h = self.home
        for d in (".claude/sessions", ".local/bin", ".local/share/claude/versions", ".config/tmux/plugins", "bin", "ard",
                  "tmp", "work"):
            (h / d).mkdir(parents=True)
        (h / ".config/tmux/plugins/tmux-assistant-resurrect").symlink_to(PLUGIN)
        (h / ".config/tmux/scripts").mkdir()
        (h / ".config/tmux/scripts/agent-session-lib.sh").symlink_to(SCRIPTS / "agent-session-lib.sh")
        (h / "fake.py").write_text(FAKE_PY)
        self.exe(h / "notify.sh", FAKE_NOTIFY)
        # Every tmux of the scripts and hooks: the private server, never a config.
        self.exe(h / "bin/tmux", f'#!/bin/sh\n[ -e "$HOME/stall" ] && exec sleep 60\n[ "$1" = list-clients ] && [ -e "$HOME/client-idle" ] && {{ echo 1000; exit 0; }}\nexec {TMUX} -f /dev/null "$@"\n')
        self.exe(h / "bg-pending", FAKE_BG)
        self.exe(h / "save-soon.sh", '#!/bin/sh\necho "$@" >> "$HOME/saves"\n')
        (h / ".claude.json").write_text('{"resumeReturnDismissed": true}')
        self.exe(h / "pmset", '#!/bin/sh\n[ ! -e "$HOME/batt" ] || cat "$HOME/batt"\n')
        self.install(NEW)
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE", "CLAUDE_CONFIG_DIR")}
        self.sock = f"{self.sockdir}/tmux-{os.getuid()}/default"
        self.env.update({
            "HOME": str(h), "TMPDIR": f"{h}/tmp/", "TMUX_TMPDIR": str(self.sockdir), "XDG_STATE_HOME": f"{h}/state",
            "PATH": f"{h}/bin:{h}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "TMUX_ASSISTANT_RESURRECT_DIR": f"{h}/ard", "AGENT_REFRESH_ALLOW_SOCKET": self.sock,
            "AGENT_REFRESH_MCP_URLS": "", "AGENT_REFRESH_EXIT_TICKS": "40", "AGENT_REFRESH_BACK_TICKS": "40",
            "NOTIFY_SH": f"{h}/notify.sh", "AGENT_BG_PENDING": f"{h}/bg-pending", "AGENT_STATE_NOTICE": "",
            "AGENT_REFRESH_PMSET": f"{h}/pmset", "SAVE_SOON_SH": f"{h}/save-soon.sh", "AGENT_FOCUS_NOTICE": "", "LANG": "en_US.UTF-8", "BASH_SILENCE_DEPRECATION_WARNING": "1", "PS1": "$ ",
        })
        for k in ("LC_ALL", "LC_CTYPE", "AGENT_PAUSE_STATE_DIR", "XDG_RUNTIME_DIR", "STASH_SH", "CLOSED_TABS_SH"):
            self.env.pop(k, None)
        self.child = None
        self.addCleanup(self.reap)
        self.t("new-session", "-d", "-s", "main", "-x", "100", "-y", "30", "sleep 900")
        self.t("set", "-g", "exit-empty", "off")
        self.t("set", "-g", "default-command", "exec /bin/bash --norc --noprofile -i")
        self.t("set", "-w", "-t", "main:0", "@space_home", "1")
        self.assertEqual(os.path.realpath(self.t("display", "-p", "-t", "main", "#{socket_path}")), self.sock)

    # -- harness --------------------------------------------------------------

    def exe(self, path, body):
        path.write_text(body)
        path.chmod(0o755)

    def install(self, version, age=2 * HOUR):
        f = self.home / ".local/share/claude/versions" / version
        shutil.copy(f"{BUILD}/claude", f)
        os.utime(f, (time.time() - age,) * 2)
        link = self.home / ".local/bin/claude"
        link.unlink(missing_ok=True)
        link.symlink_to(f)
        return f

    def reap(self):
        if self.child:
            try:
                os.kill(self.child, signal.SIGKILL)
                os.waitpid(self.child, 0)
                os.close(self.fd)
            except OSError:
                pass
        for f in (self.home / ".claude/sessions").glob("*.json"):
            try:
                os.kill(int(f.stem), signal.SIGKILL)
            except OSError:
                pass
        subprocess.run([TMUX, "-f", "/dev/null", "kill-server"], env=self.env, capture_output=True, timeout=10)

    def t(self, *args):
        r = subprocess.run([TMUX, "-f", "/dev/null", *args], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, (args, r.stderr))
        return r.stdout.strip()

    def opt(self, win, name, value=None):
        if value is None:
            return self.t("show-options", "-wqv", "-t", win, name)
        if value == "":
            return self.t("set-option", "-uw", "-t", win, name)
        return self.t("set-option", "-w", "-t", win, "--", name, value)

    def until(self, what, fn, secs=10):
        end = time.time() + secs
        while time.time() < end:
            v = fn()
            if v:
                return v
            time.sleep(0.05)
        self.fail(f"never: {what}")

    def transcript(self, sid, cwd=None, records=(), root=".claude"):
        d = self.home / root / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(cwd or self.home / "work"))
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{sid}.jsonl"
        f.write_text("".join(json.dumps(r) + "\n" for r in records or [{"type": "user", "message": {"content": "hi"}}]))
        return f

    def records(self):
        return {d["sessionId"]: d for d in (json.loads(f.read_text())
                for f in (self.home / ".claude/sessions").glob("*.json"))}

    def agent(self, args="", idle=3 * HOUR, version=OLD, cwd=None, sid=None, env="", command=None, transcript=True):
        """An idle agent in a window of its own → (window id, session id)."""
        sid = sid or str(uuid.uuid4())
        cwd = cwd or self.home / "work"
        cwd.mkdir(exist_ok=True)
        if transcript:
            self.transcript(sid, cwd)
        win = self.t("new-window", "-d", "-t", "main:", "-c", str(cwd), "-P", "-F", "#{window_id}", *([command] if command else []))
        self.start(win, sid, args, version, env, launch=command is None)
        self.opt(win, "@agent_since", f"{int(time.time()) - idle} idle")
        self.opt(win, "@agent_summary", "proj/My agent")
        return win, sid

    def start(self, win, sid, args="", version=OLD, env="", launch=True):
        if launch:
            line = f" FAKE_SID={sid} FAKE_VERSION={version} FAKE_MODEL=claude-opus-5-5 {env} claude {args}"
            self.t("send-keys", "-t", win, "-l", line)
            self.t("send-keys", "-t", win, "Enter")
        self.until("the agent registered", lambda: sid in self.records())
        self.until("its composer", lambda: "status" in self.t("capture-pane", "-p", "-t", win))

    def run_it(self, *args, env=None, rc=0, timeout=90):
        r = subprocess.run(["/bin/bash", str(REFRESH), *args], env=dict(self.env, **(env or {})),
                           capture_output=True, text=True, timeout=timeout)
        self.assertEqual((r.returncode, r.stderr), (rc, ""), r.stdout)
        return r.stdout

    def verdict(self, win, *args, **kw):
        """`once <win> --dry-run`: its resume line, or the skip reason."""
        out = self.run_it("once", win, "--dry-run", *args, **kw)
        m = re.search(r"WOULD RESTART:\n(.*)\n", out)
        return m.group(1).strip() if m else out.split("skip: ", 1)[1].strip()

    def refused(self, win, why, *args, **kw):
        v = self.verdict(win, *args, **kw)
        self.assertNotIn("&& command ", v)
        self.assertIn(why, v)

    def taken(self, win, **kw):
        self.assertIn("&& command ", self.verdict(win, **kw))

    def attach(self):
        pid, fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
                os.execvpe(TMUX, [TMUX, "-f", "/dev/null", "attach", "-t", "main"], dict(self.env, TERM="xterm-256color"))
            finally:
                os._exit(127)
        self.child, self.fd = pid, fd

        def drain():
            while True:
                try:
                    if not os.read(fd, 65536):
                        return
                except OSError:
                    return
        threading.Thread(target=drain, daemon=True).start()
        self.until("client", lambda: self.t("list-clients"))

    @property
    def state(self):
        return self.home / "state/agent-refresh"

    def journals(self):
        return sorted((self.state / "journal").glob("*")) if (self.state / "journal").exists() else []

    def toasts(self):
        """The toasts posted, then those still waiting for someone to be there."""
        d = self.state / "toasts"
        wait = [f.read_text().splitlines() for f in sorted(d.iterdir())] if d.exists() else []
        return self.posted() + [["", "", "", "", "", "", "-T", title, "--", text] for title, text in wait]

    def posted(self):
        f = self.home / "toasts"
        return [x.split("\x1f") for x in f.read_text().splitlines()] if f.exists() else []

    def log(self):
        return (self.state / "log").read_text()


class Restart(Base):
    def test_restart_in_place_keeps_the_old_since_and_the_command(self):
        cwd = self.home / "it's a $dir #1"
        win, sid = self.agent("--model 'claude-opus-5-5[1m]' --effort high", cwd=cwd, env="FAKE_MODEL='claude-opus-5-5[1m]'")
        pane = self.t("display", "-p", "-t", win, "#{pane_id}")
        since = self.opt(win, "@agent_since")
        saved = {"@agent_summary": "proj/My agent; it's #{here};", "@agent_summary_cond": "1", "@agent_summary_key": "-k",
                 "@agent_detail": "100%H done;", "@agent_detail_kind": "done", "@agent_pinned": "1792455117",
                 "@review_kept": since.split()[0]}
        for o, v in saved.items():
            self.opt(win, o, v.replace("here};", "here}\\;") if v.endswith("};") else (v[:-1] + "\\;" if v.endswith(";") else v))
            self.assertEqual(self.opt(win, o), v)
        pid = self.records()[sid]["pid"]
        out = self.run_it("check")
        self.assertEqual(out, "")
        # The same conversation, same tab and pane, a new process on the new version.
        rec = self.records()[sid]
        self.assertNotEqual(rec["pid"], pid)
        self.assertEqual((rec["version"], rec["cwd"]), (NEW, str(cwd)))
        self.assertEqual(self.t("display", "-p", "-t", win, "#{pane_id}"), pane)
        argv = [json.loads(x) for x in (self.home / "argv.log").read_text().splitlines()]
        self.assertEqual(argv[-1], ["--effort", "high", "--model", "claude-opus-5-5[1m]", "--resume", sid])
        # Its options are what they were, the idle clock above all.
        self.assertEqual(self.opt(win, "@agent_state"), "idle")
        self.assertEqual(self.opt(win, "@agent_since"), since)
        for o, v in saved.items():
            self.assertEqual(self.opt(win, o), v, o)
        # The journal was complete while the old process was still exiting.
        j = json.loads((self.home / f"journal-at-exit/{win[1:]}.json").read_text())
        self.assertEqual((j["phase"], j["sid"], j["pid"], j["cwd"], j["pane"]), (j["phase"], sid, str(pid), str(cwd), pane))
        self.assertEqual((j["from"], j["to"], j["opt:@agent_since"]), (OLD, NEW, since))
        self.assertTrue(j["cmd"].endswith(f"--resume '{sid}'"))
        self.assertIn(j["phase"], ("killing", "signalled"))
        self.assertEqual((self.home / "saves").read_text(), "\n")   # resurrect-save-soon.sh, once
        # Verified back: no journal, no failure, silent.
        self.assertEqual(self.journals(), [])
        self.assertFalse((self.state / "failed").exists())
        self.assertEqual(self.toasts(), [])
        self.assertIn(f"restarted {win} (", self.log())
        self.assertFalse((self.state / "lock").exists())
        # And it is a candidate no more.
        self.refused(win, "up to date")

    def test_launched_with_c_comes_back_by_id(self):
        win, sid = self.agent("-c")
        line = self.verdict(win)
        self.assertEqual(line, f"clear; cd '{self.home}/work' && command claude --model 'claude-opus-5-5' --resume '{sid}'")
        self.run_it("once", win)
        argv = [json.loads(x) for x in (self.home / "argv.log").read_text().splitlines()]
        self.assertEqual(argv, [["-c"], ["--model", "claude-opus-5-5", "--resume", sid]])

    def test_moved_on_options_are_left_alone(self):
        (self.home / "comes-back-busy").touch()
        win, sid = self.agent()
        self.opt(win, "@agent_state", "idle")
        self.run_it("check")
        self.assertEqual(self.records()[sid]["version"], NEW)
        self.assertEqual((self.opt(win, "@agent_state"), self.opt(win, "@agent_summary")), ("running", "from the hook"))
        self.assertIn("moved on", self.log())
        self.assertEqual((self.journals(), self.toasts()), ([], []))

    def test_a_late_session_start_does_not_reset_the_clock(self):
        """The watcher seeds idle, the SessionStart hook restamps seconds later."""
        win, _ = self.agent()
        since = self.opt(win, "@agent_since")
        (self.home / "late-hook").touch()
        self.run_it("check")
        time.sleep(1.5)
        self.assertEqual(self.opt(win, "@agent_since"), since)

    def test_three_per_tick_longest_idle_first(self):
        wins = [self.agent(idle=h * HOUR)[0] for h in (3, 9, 5, 7)]
        out = self.run_it("--dry-run")
        self.assertEqual(re.findall(r"^(@\d+)", out, re.M), [wins[1], wins[3], wins[2], wins[0]])
        self.assertEqual((out.count("WOULD RESTART:"), out.count("would restart on a later tick")), (3, 1))
        self.assertFalse(self.state.exists())   # a dry run writes nothing
        self.assertEqual({r["version"] for r in self.records().values()}, {OLD})
        self.run_it("check")
        left = [r["tmux"] for r in self.records().values() if r["version"] == OLD]
        self.assertEqual([x.split(":")[1].split(".")[0] for x in left], [wins[0]])
        self.run_it("check")
        self.assertEqual({r["version"] for r in self.records().values()}, {NEW})


class Refusals(Base):
    """Every candidate rule, through `once <win> --dry-run`."""

    def test_window_rules(self):
        win, _ = self.agent()
        self.taken(win)
        for o, v, why in (
                ("@agent_state", "done", "state done"), ("@agent_state", "needs-input", "state needs-input"),
                ("@agent_state", "failed", "state failed"), ("@agent_state", "running", "state running"),
                ("@agent_state", "paused", "state paused"), ("@agent_kind", "codex", "not Claude Code"),
                ("@space_home", "1", "a home tab"), ("@agent_workflow", "1", "workflow"), ("@agent_cua", "1", "cua"),
                ("@agent_limit", "1 x 2", "rate-limited"), ("@agent_limit_lifted", "9", "rate-limited"),
                ("@agent_paused", "1", "paused"), ("@agent_pending", "5", "pending-prompt"),
                ("@agent_child", "@9", "delegated-child"), ("@agent_parent", "@9", "delegated-child"),
                ("@agent_kids", "1 0", "delegating"), ("@agent_kids_hold", "1", "delegating"),
                ("@agent_since", "junk", "idle since when is unknown"),
                ("@agent_since", f"{int(time.time()) - 9 * HOUR} done", "idle since when is unknown"),
                ("@agent_since", f"{int(time.time()) - 2 * HOUR + 60} idle", "idle under 2h")):
            with self.subTest(f"{o}={v}"):
                was = self.opt(win, o)
                self.opt(win, o, v)
                self.refused(win, why)
                self.opt(win, o, was)
        self.opt(win, "@agent_since", f"{int(time.time()) - 60} idle")
        self.assertIn("--resume", self.verdict(win, "--force-idle"))   # skips that rule
        self.opt(win, "@agent_state", "done")
        self.refused(win, "state done", "--force-idle")                # and only that
        self.opt(win, "@agent_state", "idle")
        self.opt(win, "@agent_since", f"{int(time.time()) - 2 * HOUR - 1} idle")
        self.taken(win)
        # Spaces: hidden by its own flag, by the list, by the built-in names.
        self.t("set-option", "-t", "main", "@space_hidden", "1")
        self.refused(win, "hidden space")
        self.t("set-option", "-u", "-t", "main", "@space_hidden")
        self.t("set-option", "-g", "@space_hidden_sessions", "a main b")
        self.refused(win, "hidden space")
        self.t("set-option", "-g", "@space_hidden_sessions", "a b")
        self.t("new-session", "-d", "-s", "stash", "sleep 900")
        self.t("link-window", "-d", "-s", win, "-t", "stash:")
        v = self.verdict(win)
        self.assertNotIn("--resume", v)
        self.t("unlink-window", "-t", f"stash:{win}")
        self.taken(win)
        pane = self.t("split-window", "-d", "-t", win, "-P", "-F", "#{pane_id}", "sleep 900")
        self.refused(win, "split tab")
        self.t("kill-pane", "-t", pane)
        self.taken(win)

    def test_on_screen(self):
        win, _ = self.agent()
        self.t("select-window", "-t", "main:0")
        self.attach()
        self.taken(win)
        self.t("select-window", "-t", win)
        self.refused(win, "on screen")
        self.refused(win, "on screen", "--force-idle")

    def test_abby_is_refused(self):
        sid = str(uuid.uuid4())
        self.t("set", "-g", "@assistant-resurrect-capture-env", "CLAUDE_CONFIG_DIR")
        win, _ = self.agent(sid=sid, env="FAKE_ENV='{\"CLAUDE_CONFIG_DIR\":\"%s/.claude-abby\"}'" % self.home)
        self.refused(win, "no resume command of Claude Code's own")   # (its tab not marked)
        self.opt(win, "@agent_harness", "abby")
        self.refused(win, "harness abby")

    def test_session_rules(self):
        win, sid = self.agent()
        rec = self.records()[sid]
        f = self.home / f".claude/sessions/{rec['pid']}.json"
        for change, why in (({"status": "busy"}, "session status busy"), ({"status": "waiting"}, "session status waiting"),
                            ({"status": None}, "session status unknown"),
                            ({"version": "2.1.330-beta"}, "running version unreadable"),
                            ({"procStart": "Mon Jan  5 00:00:00 2026"}, "no registered Claude Code process"),
                            ({"procStart": ""}, "no registered Claude Code process"),
                            ({"sessionId": "other-id"}, ""), ({"cwd": "/nonexistent"}, "its directory is gone"),
                            ({"cwd": "/private/tmp"}, "transcript is not under its directory"),
                            ({"tmux": "main:@99.%99"}, "no registered Claude Code process")):
            with self.subTest(str(change)):
                f.write_text(json.dumps(dict(rec, **change)))
                self.refused(win, why)
        f.write_text(json.dumps(dict(rec, status="idle")))
        self.taken(win)
        # Another pid's record claiming this window (another tmux server's ids
        # collide): not under this pane, ignored.
        other = subprocess.Popen(["sleep", "60"])
        self.addCleanup(other.kill)
        (self.home / f".claude/sessions/{other.pid}.json").write_text(json.dumps(dict(rec, pid=other.pid)))
        self.taken(win)
        # The id the resume would use must be the process's own.
        ard = self.home / f"ard/claude-{rec['pid']}.json"
        ard.write_text(json.dumps({"session_id": "0123"}))
        self.transcript("0123")
        self.refused(win, "session ids disagree")
        ard.write_text("{}")
        self.refused(win, "")   # no id at all: stash.sh's check already refuses

    def test_versions(self):
        win, sid = self.agent(version="2.1.99")
        rec = self.records()[sid]
        f = self.home / f".claude/sessions/{rec['pid']}.json"

        def judge(running):
            f.write_text(json.dumps(dict(rec, version=running)))
            return self.verdict(win)
        for running, installed, take in (("2.1.99", "2.1.330", True), ("2.1.9", "2.1.10", True), ("2.1.10", "2.1.9", False),
                                         ("2.1.330", "2.1.330", False), ("2.2.0", "2.1.330", False), ("1.9.9", "2.0.0", True),
                                         ("2.1.08", "2.1.9", True)):
            with self.subTest(f"{running} vs {installed}"):
                self.install(installed)
                v = judge(running)
                self.assertEqual("--resume" in v, take, v)
                if not take:
                    self.assertEqual(v, "up to date")
        f.write_text(json.dumps(rec))
        for what, fix, why in (("young", lambda: self.install(NEW, age=300), "under 10 min old"),
                               ("odd name", lambda: self.install("2.1.330-rc1"), "installed version unreadable"),
                               ("not executable", lambda: self.install(NEW).chmod(0o644), "not executable"),
                               ("gone", lambda: self.install(NEW).unlink(), "not executable")):
            with self.subTest(what):
                fix()
                self.assertIn(why, self.run_it("--dry-run"))
                self.run_it("check")
        self.assertEqual(self.records()[sid]["pid"], rec["pid"])

    def test_process_rules(self):
        # No shell under it: the pane would go with the agent.
        sid = str(uuid.uuid4())
        win, _ = self.agent(sid=sid, command=f"FAKE_SID={sid} FAKE_VERSION={OLD} exec {self.home}/.local/bin/claude")
        self.refused(win, "not a shell")
        # A draft in the composer, and whatever else stash.sh refuses.
        win, _ = self.agent(env="FAKE_DRAFT=half-a-thought")
        self.refused(win, "unsent text")
        # Suspended by ^Z: no agent at its terminal.
        win, sid = self.agent()
        self.taken(win)
        self.t("send-keys", "-t", win, "C-z")
        self.until("stopped", lambda: subprocess.run(["ps", "-o", "stat=", "-p", str(self.records()[sid]["pid"])],
                                                     capture_output=True, text=True).stdout.startswith("T"))
        self.refused(win, "not its shell's foreground job")

    def test_what_only_the_process_holds(self):
        win, sid = self.agent()
        now = time.time()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 60))
        self.transcript(sid, records=tool("Monitor", {}, {"taskId": "b1", "timeoutMs": 1000, "persistent": True},
                                          time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now + 5))))
        self.refused(win, "a Monitor is still running")
        self.transcript(sid, records=tool("CronCreate", {}, {"id": "ab12cd34"}, stamp))
        self.refused(win, "cron")
        self.transcript(sid)

    def test_background_work_fails_closed(self):
        win, sid = self.agent()
        mode = self.home / "bg-mode"
        for m in ("agents", "tasks", "subagents", "workflow", "ptys"):
            mode.write_text(m)
            self.refused(win, "background work still out")
        for m in ("raise", "late raise", "nochain", "notmux"):
            mode.write_text(m)
            self.refused(win, "agent-bg-pending could not be asked")
        mode.unlink()
        self.taken(win)
        start = self.records()[sid]["startedAt"] // 1000
        self.assertEqual((self.home / "bg-args").read_text(), f"{float(start)} True")
        self.assertEqual(list(self.home.glob("**/__pycache__")), [])   # never written beside the program
        (self.home / "bg-pending").chmod(0o644)
        self.refused(win, "agent-bg-pending could not be asked")
        self.refused(win, "agent-bg-pending could not be asked", env={"AGENT_BG_PENDING": "/nonexistent"})

    @unittest.skipUnless(REAL_BG.exists(), "no agent-bg-pending")
    def test_the_real_agent_bg_pending(self):
        win, sid = self.agent()
        env = {"AGENT_BG_PENDING": str(REAL_BG)}
        self.taken(win, env=env)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + 5))
        self.transcript(sid, records=[{"type": "user", "timestamp": ts, "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t", "content": "Command running in background with ID: bx1"}]}}])
        self.refused(win, "background work still out", env=env)

    def test_the_resume_gets_the_model_it_is_on(self):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + 600))
        for flags, own in (("--model 'claude-opus-5-5[1m]' --effort high", "claude-opus-5-5[1m]"),
                           ("--model=claude-opus-5-5 --effort high", "claude-opus-5-5"), ("--effort high", "claude-opus-5-5")):
            win, sid = self.agent(flags)
            line = lambda m: f"&& command claude '--effort' 'high' --model '{m}' --resume '{sid}'"
            self.assertIn(line(own), self.verdict(win))               # nothing said yet: its own
            self.transcript(sid, records=[said("claude-fable-5-2", stamp)])
            self.assertIn(line("claude-fable-5-2"), self.verdict(win))
        self.transcript(sid, records=[said("claude-fable-5-2", "2020-01-01T00:00:00.000Z")])
        self.assertIn(line(own), self.verdict(win))                   # an earlier process's
        self.transcript(sid, records=[said("claude-fable-5-2", stamp, attributionSkill="x:y")])
        self.refused(win, "its last turn ran under a skill")
        win, sid = self.agent(env="FAKE_MODEL=")
        self.refused(win, "no model known")
        self.transcript(sid, records=[said("claude-fable-5-2", stamp)])
        self.assertIn("command claude --model 'claude-fable-5-2' --resume", self.verdict(win))
        win, sid = self.agent("--model opusplan")
        self.transcript(sid, records=[said("claude-opus-5-5", stamp)])
        self.refused(win, "opusplan")

    def test_tick_rules(self):
        win, sid = self.agent()
        pid = self.records()[sid]["pid"]
        # Any other server than the default one.
        env = dict(AGENT_REFRESH_ALLOW_SOCKET="")
        self.assertIn("not the default tmux server", self.run_it("--dry-run", env=env))
        self.run_it("check", env=env)
        # A local MCP server down.
        env = dict(AGENT_REFRESH_MCP_URLS="http://127.0.0.1:1/mcp")
        self.assertIn("MCP down: http://127.0.0.1:1/mcp", self.run_it("--dry-run", env=env))
        self.run_it("check", env=env)
        self.run_it("once", win, env=env, rc=3)
        # A resume that could stop at Claude Code's own dialog; a plan-switching model.
        cfg, want = self.home / ".claude.json", "would ask how to resume"
        for body in ('{"resumeReturnDismissed": false}', "{}", "torn {", None):
            cfg.unlink(missing_ok=True)
            if body:
                cfg.write_text(body)
            self.assertIn(want, self.run_it("--dry-run"))
            self.run_it("check")
        cfg.write_text('{"resumeReturnDismissed": true}')
        (self.home / ".claude/settings.json").write_text('{"model": "opusplan"}')
        self.run_it("once", win, rc=3)
        (self.home / ".claude/settings.json").write_text('{"model": "opus"}')
        # An agent-pause set.
        p = self.home / ".local/state/agent-pause/paused.json"
        p.parent.mkdir(parents=True)
        for body in ('{"targets": [{"win": "@1"}]}', "torn {"):
            p.write_text(body)
            self.assertIn("nothing would restart now: agent", self.run_it("--dry-run"))
            self.run_it("check")
        p.write_text('{"targets": []}')
        self.taken(win)
        self.assertEqual(self.records()[sid]["pid"], pid)
        self.assertEqual(self.journals(), [])
        self.assertIn("MCP down", self.log())

    def test_low_battery(self):
        win, sid = self.agent()
        pid = self.records()[sid]["pid"]

        def batt(source, pct, state):
            (self.home / "batt").write_text(f"Now drawing from '{source}'\n -InternalBattery-0 (id=22216803)\t{pct}%; "
                                            f"{state}; 1:51 remaining present: true\n")
        for pct in (9, 25, 39):
            batt("Battery Power", pct, "discharging")
            self.assertIn(f"nothing would restart now: on battery at {pct}%", self.run_it("--dry-run"))
        self.run_it("check")
        self.assertIn("skip: on battery at 39% (under 40%)", self.log())
        self.assertEqual((self.records()[sid]["pid"], self.journals()), (pid, []))
        self.taken(win)                                    # by hand it still goes
        for args in (("Battery Power", 40, "discharging"), ("Battery Power", 100, "discharging"), ("AC Power", 5, "charging")):
            batt(*args)
            self.assertNotIn("nothing would restart", self.run_it("--dry-run"))
        (self.home / "batt").write_text("Now drawing from 'AC Power'\n")      # no battery
        self.run_it("check")
        self.until("the restart", lambda: self.records()[sid]["pid"] != pid)

    def test_last_look(self):
        """The guards again after the journal is written, right before the
        kill: a refusal there removes it and leaves the agent running."""
        win, sid = self.agent()
        pid = self.records()[sid]["pid"]
        real = SCRIPTS / "executable_stash.sh"
        stub = self.home / "stash-stub.sh"
        n = self.home / "stash-calls"
        for second in ('echo "that agent is still working"; exit 3',
                       f'tmux set-option -w -t {win} @agent_state running; exit 0',
                       f'tmux set-option -w -t {win} @agent_pending 1; exit 0'):
            with self.subTest(second):
                n.unlink(missing_ok=True)
                self.exe(stub, '#!/usr/bin/env bash\nif [ "${1:-}" = check ]; then\n'
                               f'echo x >> "{n}"\nif [ "$(wc -l < "{n}")" -ge 2 ]; then {second}; fi\nexit 0\nfi\n'
                               f'. "{real}" "$@"\n')
                out = self.run_it("once", win, env={"STASH_SH": str(stub)}, rc=3)
                self.assertIn("changed at the last moment", out)
                self.assertEqual(self.records()[sid]["pid"], pid)
                self.assertEqual((self.journals(), self.toasts()), ([], []))
                self.opt(win, "@agent_state", "idle")
                self.opt(win, "@agent_pending", "")

    def test_options_that_cannot_be_read_stop_it(self):
        """A tmux that fails (or answers half) is not "no such option"."""
        win, sid = self.agent()
        pid = self.records()[sid]["pid"]
        real = (self.home / "bin/tmux").read_text()
        for answer in ("exit 1", "printf 'x\\037y'; exit 0", "sleep 60"):
            with self.subTest(answer):
                self.exe(self.home / "bin/tmux", real.replace("\nexec ", "\ncase \"$*\" in *@review_kept*) " + answer + " ;; esac\nexec ", 1)
                         if "\nexec " in real else real)
                out = self.run_it("once", win, rc=3, env={"AGENT_REFRESH_TMUX_TIMEOUT": "2"})
                self.assertIn("its options could not be read", out)
                self.assertEqual((self.records()[sid]["pid"], self.journals()), (pid, []))
        self.exe(self.home / "bin/tmux", real)

    def test_the_journal_says_killing_before_the_last_look(self):
        """So nothing is written between the last check and the signal; a draft
        that shows up during the look still stops it, and undoes the journal."""
        win, sid = self.agent()
        pid = self.records()[sid]["pid"]
        n, stub, seen = self.home / "stash-calls", self.home / "stash-stub.sh", self.home / "seen"
        self.exe(stub, '#!/usr/bin/env bash\nif [ "${1:-}" = check ]; then\n'
                       f'echo x >> "{n}"\nif [ "$(wc -l < "{n}")" -ge 2 ]; then cat "$HOME"/state/agent-refresh/journal/*.json > "{seen}"; '
                       f'kill -USR1 {pid}; sleep 0.3; fi\nexit 0\nfi\n. "{SCRIPTS}/executable_stash.sh" "$@"\n')
        out = self.run_it("once", win, env={"STASH_SH": str(stub)}, rc=3)
        self.assertIn("unsent text", out)
        self.assertEqual(json.loads(seen.read_text())["phase"], "killing")
        self.assertEqual((self.records()[sid]["pid"], self.journals()), (pid, []))


class Failures(Base):
    def test_a_resume_that_does_not_start_is_told_once_and_not_retried(self):
        win, sid = self.agent("--model 'claude-opus-5-5[1m]'")
        (self.home / "broken").touch()   # the installed claude exits at once
        self.run_it("check")
        self.assertEqual(self.journals(), [])
        [toast] = self.toasts()
        self.assertEqual((toast[7], self.posted()), ("Agent restart failed", []))   # nobody there: it waits
        self.assertIn(f"Bring it back: cd '{self.home}/work' && command claude --model 'claude-opus-5-5[1m]' --resume '{sid}'", toast[9])
        self.assertIn(f"{win} {sid} {NEW} ", (self.state / "failed").read_text())
        self.assertEqual(len(list((self.state / "archive").iterdir())), 1)
        # Brought back by hand, on the old version again: left alone.
        (self.home / "broken").unlink()
        self.start(win, sid)
        self.opt(win, "@agent_since", f"{int(time.time()) - 5 * HOUR} idle")
        self.refused(win, "failed before")
        pid = self.records()[sid]["pid"]
        self.run_it("check")
        self.assertEqual(self.records()[sid]["pid"], pid)
        self.assertEqual((self.toasts(), self.posted()), ([toast], []))
        self.t("select-window", "-t", "main:0")
        self.attach()
        (self.home / "client-idle").touch()    # attached, but nobody has been there
        self.run_it("check")
        self.assertEqual((self.toasts(), self.posted()), ([toast], []))
        (self.home / "client-idle").unlink()
        self.run_it("check")
        self.run_it("check")
        [shown] = self.toasts()                # posted, once
        self.assertEqual(shown, self.posted()[0])
        self.assertEqual(shown[:10], ["-c", "*", "-k", "error", "-t", "3600", *toast[6:10]])
        # A newer version is a new try.
        self.install("2.1.331")
        self.taken(win)

    def test_back_on_the_wrong_version_is_not_a_loop(self):
        win, sid = self.agent()
        (self.home / "next-version").write_text("2.1.200")
        self.run_it("once", win, rc=1)
        self.assertEqual(self.records()[sid]["version"], "2.1.200")
        self.assertIn("came back on 2.1.200, not " + NEW, self.toasts()[0][9])
        self.assertNotIn("Bring it back", self.toasts()[0][9])
        self.refused(win, "failed before", "--force-idle")
        self.assertFalse((self.home / "saves").exists())

    def test_ignoring_sigterm(self):
        win, sid = self.agent()
        (self.home / "ignore-term").touch()
        pid = self.records()[sid]["pid"]
        self.run_it("check", env={"AGENT_REFRESH_EXIT_TICKS": "4"})
        [j] = self.journals()
        self.assertEqual((json.loads(j.read_text())["phase"], self.toasts()), ("signalled", []))
        self.refused(win, "already in flight")
        # Within two minutes it may still be exiting; after that, given up on.
        self.run_it("check")
        self.assertEqual(self.journals(), [j])
        j.write_text(json.dumps(dict(json.loads(j.read_text()), ts=str(int(time.time()) - 600))))
        self.run_it("check")
        self.assertEqual(self.journals(), [])
        self.assertIn("did not exit on SIGTERM", self.toasts()[0][9])
        self.assertEqual(self.records()[sid]["pid"], pid)
        self.refused(win, "failed before")


class Recovery(Base):
    """A tick that died in the middle: its journal is finished by the next."""

    def crashed(self, phase="signalled", kill=True, **over):
        win, sid = self.agent()
        rec = self.records()[sid]
        since = self.opt(win, "@agent_since")
        pane, ppid, start = self.t("display", "-p", "-t", win, "#{pane_id} #{pane_pid} #{start_time}").split()
        cmd = f"command claude --resume '{sid}'"
        j = {"phase": phase, "ts": str(int(time.time()) - 600), "server": start, "window": win, "pane": pane,
             "pane_pid": ppid, "pid": str(rec["pid"]), "sid": sid, "cwd": rec["cwd"], "cmd": cmd,
             "line": f" clear; cd '{rec['cwd']}' && {cmd}", "from": OLD, "to": NEW, "title": "My agent",
             "opt:@agent_since": since, "opt:@agent_summary": "proj/My agent", **over}
        self.state.joinpath("journal").mkdir(parents=True, exist_ok=True)
        (self.state / f"journal/{win[1:]}.json").write_text(json.dumps(j))
        if kill:
            os.kill(rec["pid"], signal.SIGTERM)
            self.until("exited", lambda: sid not in self.records())
            time.sleep(0.5)
        return win, sid, since

    def test_the_resume_is_typed_and_the_options_restored(self):
        win, sid, since = self.crashed()
        self.assertEqual(self.opt(win, "@agent_state"), "")
        (self.home / "batt").write_text("Now drawing from 'Battery Power'\n -InternalBattery-0\t9%; discharging;\n")   # even so
        self.run_it("check")
        self.assertEqual(self.records()[sid]["version"], NEW)
        self.assertEqual((self.opt(win, "@agent_since"), self.opt(win, "@agent_summary")), (since, "proj/My agent"))
        self.assertEqual((self.journals(), self.toasts()), ([], []))

    def test_already_back(self):
        win, sid, since = self.crashed(typed="1")
        self.start(win, sid, version=NEW)
        self.run_it("check")
        self.assertEqual(self.opt(win, "@agent_since"), since)
        self.assertEqual((self.journals(), self.toasts()), ([], []))
        self.assertEqual(len((self.home / "argv.log").read_text().splitlines()), 2)   # nothing typed

    def test_back_and_used_since(self):
        win, sid, _ = self.crashed(typed="1")
        self.start(win, sid, version=NEW)
        used = f"{int(time.time()) + 300} idle"   # its last turn ended well after it started
        self.opt(win, "@agent_since", used)
        self.run_it("check")
        self.assertEqual((self.opt(win, "@agent_since"), self.journals()), (used, []))

    def test_never_signalled(self):
        win, sid, since = self.crashed(phase="killing", kill=False, to="2.1.50")
        pid = self.records()[sid]["pid"]
        self.run_it("check", env={"AGENT_REFRESH_MCP_URLS": "http://127.0.0.1:1/"})   # (no restart of its own)
        self.assertEqual(self.records()[sid]["pid"], pid)
        self.assertEqual((self.journals(), self.toasts(), self.opt(win, "@agent_since")), ([], [], since))

    def test_typed_once_more_only(self):
        win, sid, _ = self.crashed(typed="2")
        self.run_it("check")
        self.assertEqual(self.journals(), [])
        self.assertIn("the resume did not start, twice", self.toasts()[0][9])
        self.assertIn(f"command claude --resume '{sid}'", self.toasts()[0][9])
        self.assertNotIn(sid, self.records())
        self.run_it("check")
        self.assertEqual(self.journals(), [])
        self.assertNotIn(sid, self.records())

    def test_a_pane_in_use_is_not_typed_into(self):
        win, sid, _ = self.crashed()
        self.t("send-keys", "-t", win, " sleep 77", "Enter")
        self.until("sleeping", lambda: self.t("display", "-p", "-t", win, "#{pane_current_command}") == "sleep")
        self.run_it("check")
        self.assertEqual((len(self.journals()), self.toasts()), (1, []))
        self.assertNotIn("claude --resume", self.t("capture-pane", "-p", "-t", win))
        self.t("send-keys", "-t", win, "C-c")
        # On screen: nor then (he may be typing there).
        self.t("select-window", "-t", win)
        self.attach()
        self.run_it("check")
        self.assertEqual((len(self.journals()), len(self.toasts())), (1, 1))
        # Too long: told.
        [f] = self.journals()
        f.write_text(json.dumps(dict(json.loads(f.read_text()), ts=str(int(time.time()) - 3600))))
        self.run_it("check")
        self.assertEqual(self.journals(), [])
        self.assertIn("its tab is on screen. Bring it back: ", self.toasts()[-1][9])
        self.assertEqual([x[7] for x in self.toasts()], ["Agent restarting", "Agent restart failed"])

    def test_an_old_journal_is_tried_before_it_is_given_up(self):
        """A lid-close ages it: hours old, never typed, at its prompt."""
        win, sid, since = self.crashed(ts=str(int(time.time()) - 5 * HOUR))
        self.run_it("check")
        self.assertEqual((self.records()[sid]["version"], self.opt(win, "@agent_since")), (NEW, since))
        self.assertEqual((self.journals(), self.toasts()), ([], []))

    def test_given_up_for_the_true_reason(self):
        old = str(int(time.time()) - 5 * HOUR)
        win, sid, _ = self.crashed(ts=old)
        self.run_it("check", env={"AGENT_REFRESH_MCP_URLS": "http://127.0.0.1:1/"})
        self.assertIn("MCP is down", self.toasts()[-1][9])
        win, sid, _ = self.crashed(ts=old)
        self.t("send-keys", "-t", win, " sleep 77", "Enter")
        self.until("sleeping", lambda: self.t("display", "-p", "-t", win, "#{pane_current_command}") == "sleep")
        self.run_it("check")
        self.assertIn("its pane is not at a prompt", self.toasts()[-1][9])
        self.assertEqual(self.journals(), [])

    def test_unreadable_session_files_are_not_no_session(self):
        """The helper fails: nothing is typed, dropped or given up."""
        for old in (600, 5 * HOUR):
            with self.subTest(old):
                win, sid, _ = self.crashed(ts=str(int(time.time()) - old))
                real = shutil.which("python3", path=self.env["PATH"])
                self.exe(self.home / "bin/python3", f'#!/bin/sh\ncase " $* " in *" sessions "*) exit 124 ;; esac\nexec {real} "$@"\n')
                self.run_it("check")
                (self.home / "bin/python3").unlink()
                self.assertEqual((len(self.journals()), self.toasts()), (1, []))
                time.sleep(1)
                self.assertNotIn(sid, self.records())      # (a typed line is cleared off the screen)
                # The journal itself unreadable for now: kept too.
                self.exe(self.home / "bin/python3", f'#!/bin/sh\ncase " $* " in *" jload "*) exit 124 ;; esac\nexec {real} "$@"\n')
                self.run_it("check")
                (self.home / "bin/python3").unlink()
                self.assertEqual((len(self.journals()), self.toasts()), (1, []))
                self.run_it("check")
                self.assertEqual((self.records()[sid]["version"], self.journals()), (NEW, []))

    def test_a_vi_mode_shell_mid_line(self):
        """zsh in vi mode, left in command mode on a half-typed line, with a
        slow prompt: the line still lands whole, once, on a fresh prompt."""
        win, sid, _ = self.crashed()
        rc = self.home / "zrc"
        rc.write_text("bindkey -v\nPS1='%% '\nprecmd() { sleep 0.8; touch $HOME/pre; }\n")
        self.t("send-keys", "-t", win, f" exec /bin/zsh -f", "Enter")
        time.sleep(0.5)
        self.t("send-keys", "-t", win, f" . {rc}", "Enter")
        self.until("its prompt", (self.home / "pre").exists)
        time.sleep(0.5)
        self.t("send-keys", "-t", win, "-l", "echo 'half a ")
        self.t("send-keys", "-t", win, "Escape")
        time.sleep(0.6)
        self.run_it("check")
        self.assertEqual(self.records()[sid]["version"], NEW)
        self.assertEqual(len((self.home / "argv.log").read_text().splitlines()), 2)

    def test_on_screen_when_the_prompt_is_back(self):
        """Mack opened the tab between the kill and the typing: not typed into."""
        win, sid = self.agent()
        since = self.opt(win, "@agent_since")
        self.t("select-window", "-t", "main:0")
        self.attach()
        (self.home / "select-on-exit").touch()
        self.run_it("check")
        self.assertEqual(len(self.journals()), 1)
        [told] = self.posted()       # he is looking at a bare shell: told at once
        self.assertEqual(told[:8], ["-c", "*", "-k", "warn", "-t", "120", "-T", "Agent restarting"])
        self.assertIn(f"reopens when you leave its tab. Or now: cd '{self.home}/work' && command claude", told[9])
        self.assertIn(f"--resume '{sid}'", told[9])
        self.run_it("check")
        self.assertEqual(len(self.toasts()), 1)       # once
        time.sleep(1)
        self.assertNotIn(sid, self.records())
        self.assertNotIn("claude --resume", self.t("capture-pane", "-p", "-t", win))
        self.t("select-window", "-t", "main:0")
        self.run_it("check")
        self.assertEqual((self.records()[sid]["version"], self.opt(win, "@agent_since"), self.journals()), (NEW, since, []))

    def test_tmux_stalls_between_the_kill_and_the_typing(self):
        win, sid = self.agent()
        since = self.opt(win, "@agent_since")
        (self.home / "stall-on-exit").touch()
        self.run_it("check", env={"AGENT_REFRESH_TMUX_TIMEOUT": "1", "AGENT_REFRESH_EXIT_TICKS": "4"}, timeout=40)
        self.assertEqual((len(self.journals()), self.toasts()), (1, []))
        self.assertFalse((self.state / "lock").exists())
        (self.home / "stall").unlink()
        self.run_it("check")
        self.assertEqual((self.records()[sid]["version"], self.opt(win, "@agent_since"), self.journals()), (NEW, since, []))

    def test_another_conversation_in_its_tab(self):
        win, sid, since = self.crashed()
        other = str(uuid.uuid4())
        self.start(win, other, version=NEW)
        mine = self.opt(win, "@agent_since")
        self.run_it("check")
        self.assertNotEqual(mine, since)
        self.assertEqual((self.opt(win, "@agent_since"), self.opt(win, "@agent_summary")), (mine, "from the hook"))
        self.assertEqual((self.journals(), len(list((self.state / "archive").iterdir()))), ([], 1))
        self.assertIn("another Claude Code session is in its tab", self.toasts()[0][9])
        self.assertIn(f"command claude --resume '{sid}'", self.toasts()[0][9])

    def test_a_new_id_of_the_same_conversation(self):
        """A resume can report a new id (a compaction): its argv names the old."""
        win, sid, since = self.crashed()
        new = str(uuid.uuid4())
        (self.home / "next-sid").write_text(new)
        self.run_it("check")
        self.assertEqual((self.records()[new]["version"], self.opt(win, "@agent_since")), (NEW, since))
        self.assertEqual((self.journals(), self.toasts()), ([], []))

    def test_gone_tab_new_server_resumed_elsewhere(self):
        win, sid, _ = self.crashed()
        self.t("kill-window", "-t", win)
        self.run_it("check")
        self.assertIn("its tab is gone", self.toasts()[0][9])
        win, sid, _ = self.crashed(server="12345")
        self.run_it("check")
        self.assertIn("tmux restarted", self.toasts()[-1][9])
        shutil.rmtree(self.state / "toasts")
        # Mack resumed it himself in another tab: nothing to say.
        win, sid, _ = self.crashed()
        self.agent(sid=sid, version=NEW)
        self.run_it("check")
        self.assertEqual((self.journals(), self.toasts()), ([], []))

    def test_unreadable_journal(self):
        (self.state / "journal").mkdir(parents=True)
        (self.state / "journal/7.json").write_text("{torn")
        self.run_it("check")
        self.assertEqual(self.journals(), [])
        self.assertIn("unreadable journal", self.log())


class Lock(Base):
    def hold(self, proc, start=None, age=0):
        lock = self.state / "lock"
        lock.mkdir(parents=True, exist_ok=True)
        if start is None:
            start = " ".join(subprocess.run(["ps", "-o", "lstart=", "-p", str(proc.pid)], capture_output=True, text=True,
                                            env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"}).stdout.split())
        (lock / "pid").write_text(f"{proc.pid}\n{start}\n")
        os.utime(lock, (time.time() - age,) * 2)
        return lock

    def popen(self, name="agent-refresh.sh"):
        p = subprocess.Popen(["/bin/bash", "-c", f"exec -a '{name}' sleep 60"])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        time.sleep(0.2)
        return p

    MCP_DOWN = {"AGENT_REFRESH_MCP_URLS": "http://127.0.0.1:1/"}

    def past_the_lock(self, win):
        """It gets past the lock (and restarts nothing: MCP is down)."""
        self.assertIn("MCP down", self.run_it("once", win, rc=3, env=self.MCP_DOWN))
        self.assertFalse((self.state / "lock").exists())

    def test_one_at_a_time(self):
        win, sid = self.agent()
        pid = self.records()[sid]["pid"]
        holder = self.popen()
        lock = self.hold(holder)
        self.run_it("check")
        self.assertIn("another agent-refresh is running", self.log())
        self.run_it("once", win, rc=3)
        self.assertEqual(self.records()[sid]["pid"], pid)
        self.assertTrue(lock.exists())
        # Freshly made, no pid yet: being taken. Left by a crash: broken.
        (lock / "pid").unlink()
        os.utime(lock)
        self.run_it("once", win, rc=3)
        os.utime(lock, (time.time() - 60,) * 2)
        self.past_the_lock(win)
        # A dead holder; a reused pid (another start time, or another program).
        self.hold(holder, start="Mon Jan 5 00:00:00 2026")
        self.past_the_lock(win)
        other = self.popen("vim")
        self.hold(other, age=3600)
        self.past_the_lock(win)
        self.assertIsNone(other.poll())     # never killed: not this script
        self.assertIsNone(holder.poll())
        holder.kill()
        holder.wait()
        self.hold(holder, start="Mon Jan 5 00:00:00 2026")
        self.past_the_lock(win)

    def test_a_hung_holder_is_ended_by_age(self):
        win, _ = self.agent()
        holder = self.popen()
        self.hold(holder, age=800)
        self.run_it("once", win, rc=3)
        self.assertIsNone(holder.poll())
        self.hold(holder, age=1000)
        self.past_the_lock(win)
        self.assertEqual(holder.wait(5), -signal.SIGTERM)

    def test_the_watchdog_ends_a_tick_and_leaves_the_journal(self):
        win, sid = self.agent()
        (self.home / "ignore-term").touch()     # waits for it to exit, for long
        t0 = time.time()
        self.run_it("check", rc=1, env={"AGENT_REFRESH_TICK_MAX": "4", "AGENT_REFRESH_EXIT_TICKS": "400"})
        self.assertLess(time.time() - t0, 25)
        self.assertEqual(len(self.journals()), 1)
        self.assertFalse((self.state / "lock").exists())

    def test_a_hung_call_is_cut_short(self):
        """tmux hangs for good: the script ends anyway, nothing touched."""
        win, sid = self.agent()
        (self.home / "stall").touch()
        t0 = time.time()
        self.assertIn("no tmux server", self.run_it("--dry-run", env={"AGENT_REFRESH_TMUX_TIMEOUT": "1"}))
        self.assertLess(time.time() - t0, 10)
        (self.home / "stall").unlink()
        self.taken(win)

    def test_usage(self):
        for args in ((), ("once",), ("once", "main:1"), ("once", "@"), ("bogus",)):
            r = subprocess.run(["/bin/bash", str(REFRESH), *args], env=self.env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, args)
        self.run_it("once", "@999", rc=3)
        self.t("kill-server")
        self.assertIn("no tmux server", self.run_it("--dry-run"))


T0 = 1_800_000_000


def at(dt):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(T0 + dt))


_n = 0


def tool(name, inp, tur, ts, error=False, content="ok"):
    """One tool call as a transcript records it: the assistant's, the result's."""
    global _n
    _n += 1
    i = f"toolu_{_n:04d}"
    use = {"type": "assistant", "timestamp": ts, "message": {"content": [{"type": "tool_use", "id": i, "name": name, "input": inp}]}}
    res = {"type": "user", "timestamp": ts, "toolUseResult": tur,
           "message": {"content": [{"type": "tool_result", "tool_use_id": i, "content": content, **({"is_error": True} if error else {})}]}}
    return [use] if tur is None and not error else [use, res]


def note(tid, status, ts, kind="queue-operation"):
    text = f"<task-notification>\n<task-id>{tid}</task-id>\n" + (f"<status>{status}</status>\n" if status else "") + "<event>x</event>"
    if kind == "queue-operation":
        return [{"type": kind, "operation": "enqueue", "timestamp": ts, "content": text}]
    return [{"type": "user", "timestamp": ts, "message": {"content": text}}]


def said(model, ts, **kw):
    return {"type": "assistant", "timestamp": ts, "isSidechain": False, "requestedModel": model,
            "message": {"model": (model or "").split("[")[0], "content": [{"type": "text", "text": "x"}]}, **kw}


class Scan(unittest.TestCase):
    """`helper scan` alone: the script's own function, on small transcripts.
    The process started at T0; now is T0 + 4 h unless said."""

    def scan(self, *parts, now=4 * HOUR, raw=b"", rc=None):
        with tempfile.NamedTemporaryFile(suffix=".jsonl") as f:
            for p in parts:
                for r in p:
                    f.write(json.dumps(r, separators=(",", ":")).encode() + b"\n")
            f.write(raw)
            f.flush()
            helper = re.search(r"^helper\(\) \{\n.*?^\}\n", REFRESH.read_text(), re.M | re.S).group(0)
            bounded = re.search(r"^bounded\(\) \{\n.*?^\}\n", REFRESH.read_text(), re.M | re.S).group(0)
            r = subprocess.run(["/bin/bash", "-c", bounded + helper + 'helper scan "$@"', "_", f.name, str(T0), str(T0 + now)], capture_output=True, text=True)
        self.assertEqual(r.stderr, "")
        self.assertIn(r.returncode, (0, 3))
        if rc is None:
            self.assertEqual(r.returncode == 3, bool(r.stdout.strip()))
        else:
            self.assertEqual(r.returncode, rc, r.stdout)
        return r.stdout.strip()

    def wake(self, when, set_at=0):
        return tool("ScheduleWakeup", {"delaySeconds": 600}, {"scheduledFor": (T0 + when) * 1000}, at(set_at))

    def test_wakeups(self):
        self.assertEqual(self.scan(self.wake(HOUR)), "")
        self.assertEqual(self.scan(self.wake(4 * HOUR + 600)), "a wakeup is scheduled in 10m")
        stop = tool("ScheduleWakeup", {"stop": True}, {"scheduledFor": 0, "stopped": True}, at(60))
        self.assertEqual(self.scan(self.wake(5 * HOUR), stop), "")
        self.assertIn("wakeup", self.scan(stop, self.wake(5 * HOUR)))
        # Refused by the harness: not armed. No result: its own delay decides.
        self.assertEqual(self.scan(tool("ScheduleWakeup", {"delaySeconds": 99999}, None, at(0), error=True)), "")
        self.assertIn("wakeup", self.scan(tool("ScheduleWakeup", {"delaySeconds": 1200}, None, at(4 * HOUR - 60))))
        self.assertEqual(self.scan(tool("ScheduleWakeup", {"delaySeconds": 1200}, None, at(0))), "")
        self.assertIn("wakeup", self.scan(tool("ScheduleWakeup", {}, {"odd": 1}, at(4 * HOUR - 60))))

    def test_crons(self):
        cron = lambda ts=0, tur={"id": "e80c30d4", "recurring": True, "nextFireAt": (T0 + 60) * 1000}: \
            tool("CronCreate", {"cron": "17 * * * *"}, tur, at(ts), content="Scheduled recurring job e80c30d4 (x)")
        gone = tool("CronDelete", {"id": "e80c30d4"}, {"id": "e80c30d4"}, at(60))
        self.assertIn("cron", self.scan(cron()))
        self.assertEqual(self.scan(cron(), gone), "")
        self.assertIn("cron", self.scan(cron(), tool("CronDelete", {"id": "aaaaaaaa"}, {"id": "aaaaaaaa"}, at(60))))
        self.assertIn("cron", self.scan(cron(), tool("CronDelete", {"id": "e80c30d4"}, None, at(60), error=True)))
        self.assertIn("cron", self.scan(cron(), gone, cron(120)))
        # Whoever made it (an earlier process's is restored by --resume), 8 days.
        self.assertIn("cron", self.scan(cron(-5 * 86400)))
        self.assertEqual(self.scan(cron(-9 * 86400)), "")
        # A one-shot further out; an id only in the text; no result.
        self.assertIn("cron", self.scan(cron(-9 * 86400, {"id": "x", "recurring": False, "nextFireAt": (T0 + 9 * HOUR) * 1000})))
        self.assertEqual(self.scan(cron(0, "odd"), gone), "")
        self.assertIn("cron", self.scan(cron(0, None)))

    def test_monitors(self):
        mon = lambda ts=0, **kw: tool("Monitor", {"command": "x"},
                                      {"taskId": "bixuukdvg", "timeoutMs": 1800000, "persistent": False, **kw}, at(ts))
        live = mon(persistent=True)
        self.assertIn("Monitor", self.scan(live))
        self.assertEqual(self.scan(mon()), "")                       # its own timeout
        self.assertIn("Monitor", self.scan(mon(4 * HOUR - 60)))
        self.assertEqual(self.scan(mon(-60, persistent=True)), "")   # an earlier process's
        self.assertEqual(self.scan(live, note("bixuukdvg", "completed", at(9))), "")
        self.assertEqual(self.scan(live, note("bixuukdvg", "failed", at(9), kind="user")), "")
        self.assertIn("Monitor", self.scan(live, note("bixuukdvg", "", at(9))))        # an event, not its end
        self.assertIn("Monitor", self.scan(live, note("other", "completed", at(9))))
        stopped = tool("TaskStop", {"task_id": "bixuukdvg"}, {"task_id": "bixuukdvg"}, at(9))
        self.assertEqual(self.scan(live, stopped), "")
        self.assertIn("Monitor", self.scan(live, tool("TaskStop", {"task_id": "bixuukdvg"}, None, at(9), error=True)))
        self.assertIn("Monitor", self.scan(tool("Monitor", {}, None, at(0))))
        # A command's output that quotes one is no notification.
        quoted = tool("Bash", {}, {}, at(9), content=note("bixuukdvg", "completed", at(9))[0]["content"])
        self.assertIn("Monitor", self.scan(live, quoted))

    def test_artifact_watches(self):
        url = "https://claude.ai/artifact/JVYg"
        pub = lambda ts=0, sub="connected", u=url: tool("Artifact", {"file_path": "x"}, {"url": u, "liveSubscription": sub}, at(ts))
        watch = lambda ts=0, u=url, **kw: tool("ArtifactComments", {"action": "watch", "url": u, **kw}, {}, at(ts))
        lost = lambda u=url: [{"type": "system", "subtype": "informational", "timestamp": at(9),
                               "content": f"Artifact comment monitor for {u} didn't resume: it couldn't restart in this session."}]
        for p in (pub(), pub(sub="arming"), pub(-6 * 86400), watch(), watch(on=True)):
            self.assertIn("watches an artifact", self.scan(p))
        self.assertEqual(self.scan(pub(-8 * 86400)), "")
        for over in (lost(), watch(on=False)):
            self.assertEqual(self.scan(pub(), over), "")
            self.assertIn("watches an artifact", self.scan(pub(), over, pub(60)))
        self.assertIn("watches an artifact", self.scan(pub(), lost(url + "2")))
        self.assertIn("watches an artifact", self.scan(pub(), pub(u=url + "2"), lost()))
        # A publish without one, a listing, a read, an error, quoted text.
        for p in (pub(sub=None), tool("ArtifactComments", {"action": "watch"}, {}, at(0)),
                  tool("ArtifactComments", {"action": "read", "url": url}, {}, at(0)),
                  tool("ArtifactComments", {"action": "watch", "url": url}, None, at(0), error=True),
                  tool("Bash", {}, {"stdout": json.dumps({"url": url, "liveSubscription": "connected"})}, at(0))):
            self.assertEqual(self.scan(p), "")

    def test_the_model(self):
        ask = lambda dt, **kw: [{"type": "user", "timestamp": at(dt), "message": {"content": "do it"}, **kw}]
        f52, o55 = said("claude-fable-5-2", at(10)), said("claude-opus-5-5[1m]", at(20))
        get = lambda *p: self.scan(*p, rc=0)
        self.assertEqual(get([f52, o55]), "claude-opus-5-5[1m]")
        self.assertEqual(get([o55, f52]), "claude-fable-5-2")
        self.assertEqual(get([f52], [dict(o55, isSidechain=True)]), "claude-fable-5-2")
        self.assertEqual(get([f52], [said("<synthetic>", at(30))], [said(None, at(40))]), "claude-fable-5-2")
        self.assertEqual(get([f52], [dict(o55, message={"model": "<synthetic>", "content": []})]), "claude-fable-5-2")
        self.assertEqual(get([said("claude-fable-5-2", at(-10))]), "")     # an earlier process's
        for odd in ("opusplan", "gpt-6", "claude-x; rm", "claude-opus-5-5[2m]", "Claude-x"):
            self.assertEqual(get([said(odd, at(10))]), "")
        # Text that quotes a record is not one.
        self.assertEqual(get([f52], [{"type": "user", "timestamp": at(30), "message": {"content": json.dumps(o55)}}]), "claude-fable-5-2")
        # A skill may have put its turn on its own model.
        skill = lambda dt: [said("claude-opus-5-5", at(dt), attributionSkill="cua:arc")]
        self.assertIn("skill", self.scan(ask(0), [f52], ask(15), skill(20), rc=3))
        self.assertIn("skill", self.scan(ask(15), skill(20), [said("claude-opus-5-5", at(21))], rc=3))   # the whole turn
        self.assertIn("skill", self.scan(ask(15), skill(20), ask(21, isMeta=True), [said("claude-opus-5-5", at(22))], rc=3))
        self.assertIn("skill", self.scan(ask(15), skill(20), ask(30), rc=3))                             # nothing said since
        self.assertEqual(get(ask(15), skill(20), ask(30), [said("claude-fable-5-2", at(31))]), "claude-fable-5-2")
        self.assertEqual(get(skill(-20), [f52]), "claude-fable-5-2")
        # /model since the last reply.
        slash = [{"type": "user", "timestamp": at(25), "message": {"content": "<command-name>/model</command-name>"}}]
        self.assertIn("/model", self.scan([o55], slash, rc=3))
        self.assertEqual(get([o55], slash, [said("claude-fable-5-2", at(30))]), "claude-fable-5-2")

    def test_only_the_harness_records_count(self):
        [use, res] = tool("CronCreate", {}, {"id": "e80c30d4"}, at(0))
        blob = {"type": "api-request-blob", "sessionId": "s", "message": use["message"]}
        self.assertEqual(self.scan([blob, {"type": "attachment", "attachment": {"entries": [{"name": "CronCreate"}]}}]), "")
        self.assertEqual(self.scan([{"type": "user", "timestamp": at(0), "message": {"content": 'say "CronCreate"'}}]), "")
        # A line that mentions one and cannot be parsed: cannot tell.
        self.assertIn("unreadable", self.scan(raw=b'{"type":"assistant","message":{"content":[{"name":"CronCreate"'))
        self.assertEqual(self.scan(raw=b'{"type":"assistant","mess'), "")


if __name__ == "__main__":
    unittest.main()
