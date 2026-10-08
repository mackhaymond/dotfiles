"""agent-tab-watcher.sh pulse-child lifecycle + pidfile atomicity, and the
indicator's @agent_since stamp at transition time.

Isolated HOME and TMPDIR, a fake `tmux` (and a `pgrep`/`ps` that fake only
the agent discovery) on PATH. The watcher's singleton keys on
$HOME/.config/tmux/scripts/agent-tab-watcher.sh, so the isolated HOME keeps it
from ever seeing the real daemon. No real tmux server is touched.
Run with unittest.
"""
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

import test_codex_indicator as ci   # module, not the class: keeps its tests out of this module
import test_agent_jump_watcher as jw   # same: module only, for the shared process fakes

SCRIPTS = Path(os.environ.get(
    "AGENT_TMUX_SCRIPTS",
    str(Path.home() / ".local/share/chezmoi/dot_config/tmux/scripts")))
WATCHER = SCRIPTS / "executable_agent-tab-watcher.sh"
LIB = SCRIPTS / "agent-session-lib.sh"
BASH_DIR = str(Path(shutil.which("bash") or "/opt/homebrew/bin/bash").parent)

# Same shape as test_agent_jump_watcher.py's fake, trimmed to what the watcher calls.
FAKE_TMUX = r'''#!/usr/bin/env python3
import fcntl, json, os, re, sys
from pathlib import Path
p = Path(os.environ["FAKE_TMUX_STATE"])

def split_cmds(argv):
    cmds, cur = [], []
    for a in argv:
        if a == ";":
            cmds.append(cur); cur = []
        else:
            cur.append(a)
    cmds.append(cur)
    return [c for c in cmds if c]

def arg(c, f):
    return c[c.index(f) + 1] if f in c else None

def render(fmt, v):
    return re.sub(r"#\{([@\w]+)\}", lambda m: str(v.get(m.group(1), "")), fmt)

def wvars(s, wid):
    w = s["windows"][wid]
    v = {"window_id": wid, "session_name": w["session"], "window_index": w["index"],
         "window_name": "zsh", "window_active_clients": 0}
    v.update(w.get("opts", {}))
    return v

with p.with_suffix(".lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    s = json.loads(p.read_text())
    rc, out = 0, []
    for c in split_cmds(sys.argv[1:]):
        s["calls"].append(c)
        cmd = c[0]
        if cmd == "list-windows":
            for wid in s["windows"]:
                out.append(render(arg(c, "-F"), wvars(s, wid)))
        elif cmd == "list-panes":
            for pn in s["panes"]:
                v = wvars(s, pn["window"]); v["pane_tty"] = pn["tty"]
                out.append(render(arg(c, "-F"), v))
        elif cmd == "list-sessions":
            out.extend(sorted({w["session"] for w in s["windows"].values()}))
        elif cmd == "show-options":
            if "-gqv" in c:
                out.append(s["globals"].get(c[-1], ""))
        elif cmd == "set-option":
            unset = any(x in c for x in ("-uw", "-gu", "-u"))
            glob = any(x in c for x in ("-g", "-gu"))
            t = arg(c, "-t")
            rest = [x for x in c[1:] if not x.startswith("-")]
            if t: rest.remove(t)
            name = rest[0]; val = rest[1] if len(rest) > 1 else ""
            if glob and not t:
                if unset: s["globals"].pop(name, None)
                else: s["globals"][name] = val
            else:
                if t not in s["windows"]: rc = 1; continue
                o = s["windows"][t].setdefault("opts", {})
                if unset: o.pop(name, None)
                else: o[name] = val
    p.write_text(json.dumps(s))
    if out: print("\n".join(out))
    sys.exit(rc)
'''

# The agent-discovery fakes are shared with test_agent_jump_watcher.py: pgrep
# and single-pid ps answer from a "tty pid comm" table; every other query
# (pid/ppid/command lookups, the -f singleton sweep) goes to the real tools
# so identity checks see real processes.
FAKE_PGREP = jw.FAKE_PGREP
FAKE_PS = jw.FAKE_PS

UID = os.getuid()


class WatcherEnv:
    def __init__(self, patch=None):
        self.dir = Path(tempfile.mkdtemp(prefix="agentblink-"))
        self.home = self.dir / "home"; self.tmp = self.dir / "tmp"; self.bin = self.dir / "bin"
        for d in (self.home, self.tmp, self.bin):
            d.mkdir()
        scripts = self.home / ".config/tmux/scripts"; scripts.mkdir(parents=True)
        self.script = scripts / "agent-tab-watcher.sh"
        body = WATCHER.read_text()
        if patch:
            old, new = patch
            assert old in body, "test patch anchor missing from the watcher: %r" % old
            body = body.replace(old, new)
        self.script.write_text(body)
        shutil.copy(LIB, scripts / "agent-session-lib.sh")
        for name, text in (("tmux", FAKE_TMUX), ("ps", FAKE_PS), ("pgrep", FAKE_PGREP)):
            f = self.bin / name; f.write_text(text); f.chmod(0o755)
        self.procs = self.dir / "procs"; self.procs.write_text(jw.DEFAULT_PROCS)
        self.state = self.dir / "tmux.json"
        # One window running (raises the pulse flag), one plain shell.
        self.state.write_text(json.dumps({
            "windows": {"@1": {"session": "main", "index": 1,
                               "opts": {"@agent_state": "running", "@agent_since": "100 running"}},
                        "@2": {"session": "main", "index": 2, "opts": {}}},
            "panes": [{"window": "@1", "tty": "/dev/ttys900"}, {"window": "@2", "tty": "/dev/ttys901"}],
            "globals": {}, "calls": []}))
        self.pidfile = self.tmp / ("agent-tab-watcher.%d.pid" % UID)
        self.proc = None

    def env(self, **extra):
        e = {"HOME": str(self.home), "TMPDIR": str(self.tmp) + "/",
             "PATH": f"{self.bin}:{BASH_DIR}:/usr/bin:/bin", "FAKE_TMUX_STATE": str(self.state),
             "FAKE_PROCS": str(self.procs)}
        e.update(extra)
        return e

    def start(self, ticks, **extra):
        # stderr to a FILE, not a pipe: an orphaned child holding the pipe
        # would keep communicate() from ever reaping the parent, and a zombie
        # parent still answers the child's `kill -0` (tmux reaps run-shell
        # jobs on SIGCHLD, so production never sees that deadlock).
        self.errf = (self.dir / "stderr").open("w")
        self.proc = subprocess.Popen(["bash", str(self.script)],
                                     env=self.env(AGENT_TAB_WATCHER_MAX_TICKS=str(ticks), **extra),
                                     stdout=subprocess.DEVNULL, stderr=self.errf)
        return self.proc

    def wait(self, timeout=30):
        self.proc.wait(timeout=timeout)
        self.errf.close()
        return self.proc.returncode, (self.dir / "stderr").read_text()

    def read(self):
        with self.state.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return json.loads(self.state.read_text())

    def toggles(self):
        return sum(1 for c in self.read()["calls"]
                   if c[:2] == ["set-option", "-g"] and "@agent_blink" in c)

    def children(self):
        """Forked subshells of the watcher (same argv). Comsub forks are too."""
        out = subprocess.run(["pgrep", "-P", str(self.proc.pid), "-fx", f"bash {self.script}"],
                             capture_output=True, text=True).stdout.split()
        return {int(x) for x in out}

    def blink_child(self):
        """The persistent child: present in every one of several samples
        (transient command-substitution subshells share the argv)."""
        seen = None
        for _ in range(4):
            cur = self.children()
            seen = cur if seen is None else seen & cur
            time.sleep(0.1)
        return seen

    def anything_left(self):
        return subprocess.run(["pgrep", "-fx", f"bash {self.script}"],
                              capture_output=True, text=True).stdout.strip()

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill(); self.proc.wait()
        for pid in self.anything_left().split():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        shutil.rmtree(self.dir, ignore_errors=True)


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_until(pred, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


class PulseChildTests(unittest.TestCase):
    def tearDown(self):
        self.e.close()

    def test_child_survives_an_emptied_pidfile(self):
        self.e = e = WatcherEnv()
        e.start(ticks=10)
        self.assertTrue(wait_until(lambda: len(e.blink_child()) == 1, 4))
        (child,) = e.blink_child()
        # Hold the pidfile empty (truncated in place every 20 ms: the old
        # writer's race window, made wide) across ~4 of the child's 1 s beats.
        # The child must neither exit NOR stop pulsing while it reads empty.
        before = e.toggles()
        end = time.monotonic() + 4.2
        while time.monotonic() < end:
            with open(e.pidfile, "w"):
                pass
            time.sleep(0.02)
        during = e.toggles() - before
        self.assertTrue(alive(child), "pulse child exited on an empty pidfile read")
        self.assertEqual(e.blink_child(), {child}, "child was replaced, not kept")
        self.assertGreaterEqual(during, 2, "pulse stopped toggling while the pidfile read empty")
        rc, err = e.wait()
        self.assertEqual(rc, 0, err)

    def test_cleanup_kills_its_own_child_at_once(self):
        # Child's beat stretched to 30 s, so it cannot retire on its own
        # schedule: only cleanup's kill (and the nap being interruptible) can
        # explain it being gone right after the watcher exits.
        self.e = e = WatcherEnv(patch=('sleep "$POLL_SECONDS" & nap=$!', 'sleep 30 & nap=$!'))
        e.start(ticks=3)
        self.assertTrue(wait_until(lambda: len(e.blink_child()) == 1, 3))
        (child,) = e.blink_child()
        naps = [int(x) for x in subprocess.run(["pgrep", "-P", str(child), "sleep"],
                                               capture_output=True, text=True).stdout.split()]
        self.assertTrue(naps, "no nap under the child")
        rc, err = e.wait()
        self.assertEqual(rc, 0, err)
        self.assertTrue(wait_until(lambda: not alive(child), 0.5), "cleanup did not kill its child")
        self.assertTrue(wait_until(lambda: not any(alive(n) for n in naps), 0.5), "child's nap left behind")

    def test_parent_reforks_a_killed_child(self):
        self.e = e = WatcherEnv()
        e.start(ticks=9)
        self.assertTrue(wait_until(lambda: len(e.blink_child()) == 1, 4))
        (old,) = e.blink_child()
        os.kill(old, signal.SIGKILL)
        self.assertTrue(wait_until(lambda: (lambda c: len(c) == 1 and old not in c)(e.blink_child()), 4),
                        "no replacement pulse child")
        before = e.toggles()
        time.sleep(2.5)
        self.assertGreater(e.toggles(), before, "re-forked child does not toggle")
        self.assertEqual(len(e.blink_child()), 1, "more than one pulse child")
        rc, err = e.wait()
        self.assertEqual(rc, 0, err)
        self.assertTrue(wait_until(lambda: e.anything_left() == "", 3), "pulse child outlived the watcher")

    def test_cleanup_never_kills_a_pid_that_is_not_its_child(self):
        # Simulate pid reuse: at exit, BLINK_PID names a live process that is
        # NOT the watcher's child. The old cleanup SIGTERMed it unconditionally.
        stranger = subprocess.Popen(["sleep", "30"])
        try:
            self.e = e = WatcherEnv(patch=(
                '[ "$MAX_TICKS" -gt 0 ] || exit 0',
                '[ "$MAX_TICKS" -gt 0 ] || { BLINK_PID=$STRANGER_PID; exit 0; }'))
            e.start(ticks=2, STRANGER_PID=str(stranger.pid))
            rc, err = e.wait()
            self.assertEqual(rc, 0, err)
            time.sleep(0.3)
            self.assertIsNone(stranger.poll(), "cleanup killed an unrelated process")
            # The real (now untracked) child still retires on its own.
            self.assertTrue(wait_until(lambda: e.anything_left() == "", 3))
        finally:
            stranger.kill(); stranger.wait()

    def test_pidfile_is_atomic_and_still_a_heartbeat(self):
        self.e = e = WatcherEnv()
        # A stale pidfile from a dead instance: the startup CLAIM must replace
        # it by rename (new inode), never by truncating it in place.
        e.pidfile.write_text("999999\n")
        stale_ino = e.pidfile.stat().st_ino
        p = e.start(ticks=6)
        self.assertTrue(wait_until(lambda: e.pidfile.read_text() == f"{p.pid}\n", 3))
        claimed_ino = e.pidfile.stat().st_ino
        self.assertNotEqual(claimed_ino, stale_ino, "startup claim was not an atomic rename")
        os.utime(e.pidfile, (time.time() - 100, time.time() - 100))
        reads, bad, inodes = 0, [], set()
        end = time.monotonic() + 3
        while time.monotonic() < end:
            try:
                with open(e.pidfile) as f:
                    inodes.add(os.fstat(f.fileno()).st_ino)
                    txt = f.read()
            except FileNotFoundError:
                continue
            reads += 1
            if txt != f"{p.pid}\n":
                bad.append(txt)
        self.assertGreater(reads, 100)
        self.assertEqual(bad, [], "a reader saw a partial/empty pidfile")
        # The per-tick restamp rewrites the same bytes in place (no mv fork).
        self.assertEqual(inodes, {claimed_ino}, "heartbeat restamp replaced the file")
        self.assertGreater(e.pidfile.stat().st_mtime, time.time() - 5, "mtime heartbeat did not advance")
        rc, err = e.wait()
        self.assertEqual(rc, 0, err)
        self.assertFalse(e.pidfile.exists(), "pidfile not cleaned up on exit")
        self.assertEqual([x.name for x in e.tmp.iterdir() if x.name.startswith(e.pidfile.name + ".")], [],
                         "temp pidfile left behind")


class SinceStampTests(unittest.TestCase):
    """set_state stamps @agent_since at the transition, clear_state unsets it."""
    setUp = ci.IndicatorIntegrationTests.setUp
    tearDown = ci.IndicatorIntegrationTests.tearDown
    bind = ci.IndicatorIntegrationTests.bind
    hook = ci.IndicatorIntegrationTests.hook
    window = ci.IndicatorIntegrationTests.window

    def calls(self):
        return json.loads(self.state.read_text())["calls"]

    def test_transition_stamps_and_clear_unsets(self):
        t0 = int(time.time())
        w = self.hook("running")
        self.assertEqual(w["@agent_state"], "running")
        ts, st = w["@agent_since"].split()
        self.assertEqual(st, "running")
        self.assertGreaterEqual(int(ts), t0)
        # done -> running -> done inside one watcher tick: the stamp must be
        # the LAST transition's, not a stale one a per-tick compare would keep.
        s = json.loads(self.state.read_text())
        s["windows"]["@7"]["@agent_state"] = "done"
        s["windows"]["@7"]["@agent_since"] = "100 done"
        self.state.write_text(json.dumps(s))
        self.hook("running")
        w = self.hook("done")
        ts, st = w["@agent_since"].split()
        self.assertEqual(st, "done")
        self.assertGreaterEqual(int(ts), t0)
        # A no-op heartbeat (state unchanged) writes nothing.
        n = len(self.calls())
        w = self.hook("heartbeat")
        self.assertEqual(w["@agent_state"], "done")
        self.assertFalse([c for c in self.calls()[n:] if c[0] == "set-option" and "@agent_since" in c])
        self.assertEqual(self.hook("clear"), {})


if __name__ == "__main__":
    unittest.main()
