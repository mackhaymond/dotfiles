"""agent-tab-watcher.sh state parsing / @agent_since / pulse child, and agent-jump.sh.

Isolated HOME and TMPDIR, a fake `tmux` and `ps` on PATH. No real tmux server,
watcher, or GUI is touched. Run with unittest.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

SCRIPTS = Path(os.environ.get(
    "AGENT_TMUX_SCRIPTS",
    str(Path.home() / ".local/share/chezmoi/dot_config/tmux/scripts")))
WATCHER = SCRIPTS / "executable_agent-tab-watcher.sh"
LIB = SCRIPTS / "agent-session-lib.sh"
JUMP = SCRIPTS / "executable_agent-jump.sh"
BASH_DIR = str(Path(shutil.which("bash") or "/opt/homebrew/bin/bash").parent)

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

def flag(c, f):
    return f in c

def arg(c, f):
    return c[c.index(f) + 1] if f in c else None

def render(fmt, v):
    fmt = fmt.replace("#{?#{n:#{@agent_summary}},#{@agent_summary},#{window_name}}",
                      v.get("@agent_summary") or v.get("window_name", ""))
    return re.sub(r"#\{([@\w]+)\}", lambda m: str(v.get(m.group(1), "")), fmt)

def wvars(s, wid):
    w = s["windows"][wid]
    v = {"window_id": wid, "session_name": w["session"], "window_index": w["index"],
         "window_name": w.get("name", "zsh"), "window_active_clients": w.get("active_clients", 0)}
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
        elif cmd == "list-clients":
            for cl in s["clients"]:
                out.append(render(arg(c, "-F"), {"client_tty": cl["tty"], "session_name": cl["session"],
                                                 "window_id": cl["window"]}))
        elif cmd == "list-sessions":
            out.extend(sorted({w["session"] for w in s["windows"].values()}))
        elif cmd == "show-options":
            name = c[-1]
            if flag(c, "-gqv"):
                out.append(s["globals"].get(name, ""))
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
        elif cmd == "display-message":
            if flag(c, "-p"):
                t = arg(c, "-t")
                if t not in s["windows"]: rc = 1; continue
                out.append(render(c[-1], wvars(s, t)))
            else:
                s["messages"].append([arg(c, "-c"), c[-1]])
        elif cmd == "select-window":
            t = arg(c, "-t")
            if t not in s["windows"]: rc = 1; continue
            s["active"][s["windows"][t]["session"]] = t
        elif cmd == "switch-client":
            tty, sess = arg(c, "-c"), arg(c, "-t").lstrip("=")
            for cl in s["clients"]:
                if cl["tty"] == tty:
                    cl["session"] = sess; cl["window"] = s["active"].get(sess, cl["window"])
    p.write_text(json.dumps(s))
    if out: print("\n".join(out))
    sys.exit(rc)
'''

FAKE_PS = """#!/bin/sh
# tty pid comm
echo "ttys900 4242 claude"
echo "ttys901 4343 zsh"
"""


class FakeEnv:
    def __init__(self, windows, panes=(), clients=(), active=None):
        self.dir = Path(tempfile.mkdtemp(prefix="agentjump-"))
        self.home = self.dir / "home"; self.tmp = self.dir / "tmp"; self.bin = self.dir / "bin"
        for d in (self.home, self.tmp, self.bin):
            d.mkdir()
        scripts = self.home / ".config/tmux/scripts"; scripts.mkdir(parents=True)
        shutil.copy(WATCHER, scripts / "agent-tab-watcher.sh")
        shutil.copy(LIB, scripts / "agent-session-lib.sh")
        shutil.copy(JUMP, scripts / "agent-jump.sh")
        self.scripts = scripts
        for name, body in (("tmux", FAKE_TMUX), ("ps", FAKE_PS)):
            f = self.bin / name; f.write_text(body); f.chmod(0o755)
        self.state = self.dir / "tmux.json"
        self.state.write_text(json.dumps({
            "windows": windows, "panes": list(panes), "clients": list(clients),
            "active": active or {}, "globals": {}, "calls": [], "messages": []}))

    def env(self, **extra):
        e = {"HOME": str(self.home), "TMPDIR": str(self.tmp) + "/",
             "PATH": f"{self.bin}:{BASH_DIR}:/usr/bin:/bin", "FAKE_TMUX_STATE": str(self.state)}
        e.update(extra)
        return e

    def run(self, script, *args, timeout=30, **extra):
        return subprocess.run(["bash", str(self.scripts / script), *args], env=self.env(**extra),
                              capture_output=True, text=True, timeout=timeout)

    def read(self):
        return json.loads(self.state.read_text())

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def W(session, index, **opts):
    return {"session": session, "index": index, "opts": {k.replace("_", "@agent_", 1) if k.startswith("_") else k: v
                                                        for k, v in opts.items()}}


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.f = FakeEnv(
            windows={
                "@1": W("main", 1),                                   # agent pane, no state yet
                "@2": W("main", 2),                                   # plain shell, no state
                "@3": W("main", 3, **{"@agent_state": "running", "@agent_since": "100 running"}),
                "@4": W("main", 4, **{"@agent_since": "5 idle"}),     # stale stamp, no state
            },
            panes=[{"window": "@1", "tty": "/dev/ttys900"}, {"window": "@2", "tty": "/dev/ttys901"},
                   {"window": "@3", "tty": "/dev/ttys900"}, {"window": "@4", "tty": "/dev/ttys901"}])

    def tearDown(self):
        self.f.close()

    def test_empty_state_is_a_field(self):
        r = self.f.run("agent-tab-watcher.sh", AGENT_TAB_WATCHER_MAX_TICKS="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.f.read()
        # Agent window with no state is seeded (the misparse made state "0" and skipped this).
        self.assertEqual(s["windows"]["@1"]["opts"].get("@agent_state"), "idle")
        self.assertRegex(s["windows"]["@1"]["opts"].get("@agent_since", ""), r"^\d+ idle$")
        # A plain shell window is left completely alone: no GC unsets at all.
        self.assertFalse([c for c in s["calls"] if c[0] == "set-option" and "@2" in c], s["calls"])
        # A stamp whose state matches is not rewritten; a stamp with no state is removed.
        self.assertEqual(s["windows"]["@3"]["opts"]["@agent_since"], "100 running")
        self.assertNotIn("@agent_since", s["windows"]["@4"]["opts"])
        # Something is running, so the pulse flag is raised; the pidfile is ours to clean up.
        self.assertTrue(Path(str(self.f.tmp) + "/agent-tab-blink." + str(os.getuid())).exists())
        self.assertFalse(Path(str(self.f.tmp) + "/agent-tab-watcher." + str(os.getuid()) + ".pid").exists())

    def test_stamp_follows_a_state_change(self):
        s = json.loads(self.f.state.read_text())
        s["windows"]["@3"]["opts"]["@agent_state"] = "done"
        self.f.state.write_text(json.dumps(s))
        self.f.run("agent-tab-watcher.sh", AGENT_TAB_WATCHER_MAX_TICKS="1")
        since = self.f.read()["windows"]["@3"]["opts"]["@agent_since"]
        self.assertRegex(since, r"^\d+ done$")
        self.assertGreater(int(since.split()[0]), 100)

    def test_pulse_has_its_own_clock(self):
        t0 = time.time()
        r = self.f.run("agent-tab-watcher.sh", AGENT_TAB_WATCHER_MAX_TICKS="3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.time() - t0, 10)
        calls = self.f.read()["calls"]
        toggles = [c for c in calls if c[:2] == ["set-option", "-g"] and "@agent_blink" in c]
        self.assertGreaterEqual(len(toggles), 1, calls)
        # Each toggle redraws in the same tmux invocation.
        self.assertTrue(any(c[0] == "refresh-client" for c in calls))
        # The child is gone once the parent exits (nothing left matching its argv).
        time.sleep(1.5)
        left = subprocess.run(["pgrep", "-f", str(self.f.scripts / "agent-tab-watcher.sh")],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(left, "")


class JumpTests(unittest.TestCase):
    TTY = "/dev/ttys000"

    def make(self, **extra_windows):
        windows = {
            "@10": W("main", 1),                                                        # where we are
            "@11": W("main", 2, **{"@agent_state": "done", "@agent_since": "300 done"}),
            "@12": W("bai", 2, **{"@agent_state": "needs-input", "@agent_since": "200 needs-input"}),
            "@13": W("work", 1, **{"@agent_state": "failed", "@agent_since": "400 failed"}),
            "@14": W("work", 2, **{"@agent_state": "done", "@agent_workflow": "1", "@agent_since": "50 done"}),
            "@15": W("stash", 1, **{"@agent_state": "failed", "@agent_since": "10 failed"}),
            "@16": W("tasks", 1, **{"@agent_state": "done", "@agent_since": "10 done"}),
            "@17": W("bai", 3, **{"@agent_state": "needs-input"}),                     # no stamp: last in tier
        }
        windows.update(extra_windows)
        self.f = FakeEnv(windows, clients=[{"tty": self.TTY, "session": "main", "window": "@10"}],
                         active={"main": "@10", "bai": "@12", "work": "@13"})
        return self.f

    def tearDown(self):
        self.f.close()

    def test_list_order_and_exclusions(self):
        out = self.make().run("agent-jump.sh", "list").stdout.split("\n")
        wins = [l.split("\t")[0] for l in out if l]
        self.assertEqual(wins, ["@13", "@12", "@17", "@11"])

    def test_next_selects_before_switching(self):
        f = self.make()
        f.run("agent-jump.sh", "next", self.TTY)
        s = f.read()
        moves = [c[0] for c in s["calls"] if c[0] in ("select-window", "switch-client")]
        self.assertEqual(moves, ["select-window", "switch-client"])
        self.assertEqual(s["clients"][0]["window"], "@13")
        self.assertEqual(s["globals"]["@agent_jump__dev_ttys000"], "@10 @13")
        self.assertIn("· failed · 3 more", s["messages"][-1][1])

    def test_chain_and_back(self):
        f = self.make()
        f.run("agent-jump.sh", "next", self.TTY)
        f.run("agent-jump.sh", "next", self.TTY)          # still on the landing: origin kept
        s = f.read()
        self.assertEqual(s["globals"]["@agent_jump__dev_ttys000"], "@10 @12")
        f.run("agent-jump.sh", "back", self.TTY)
        s = f.read()
        self.assertEqual(s["clients"][0]["window"], "@10")
        self.assertNotIn("@agent_jump__dev_ttys000", s["globals"])

    def test_back_after_moving_away_does_nothing(self):
        f = self.make()
        f.run("agent-jump.sh", "next", self.TTY)
        s = f.read(); s["clients"][0]["window"] = "@11"; s["calls"] = []
        f.state.write_text(json.dumps(s))
        f.run("agent-jump.sh", "back", self.TTY)
        s = f.read()
        self.assertFalse([c for c in s["calls"] if c[0] == "select-window"])
        self.assertEqual(s["messages"][-1][1], "nothing to go back to")

    def test_new_chain_after_moving_away(self):
        f = self.make()
        f.run("agent-jump.sh", "next", self.TTY)
        s = f.read(); s["clients"][0]["window"] = "@11"
        f.state.write_text(json.dumps(s))
        f.run("agent-jump.sh", "next", self.TTY)
        self.assertTrue(f.read()["globals"]["@agent_jump__dev_ttys000"].startswith("@11 "))

    def test_nothing_needs_you(self):
        f = FakeEnv({"@1": W("main", 1)}, clients=[{"tty": self.TTY, "session": "main", "window": "@1"}])
        self.f = f
        f.run("agent-jump.sh", "next", self.TTY)
        s = f.read()
        self.assertEqual(s["messages"][-1][1], "nothing needs you")
        self.assertFalse([c for c in s["calls"] if c[0] == "select-window"])

    def test_unknown_client_never_selects(self):
        f = self.make()
        f.run("agent-jump.sh", "next", "/dev/ttys999")
        self.assertFalse([c for c in f.read()["calls"] if c[0] == "select-window"])


if __name__ == "__main__":
    unittest.main()
