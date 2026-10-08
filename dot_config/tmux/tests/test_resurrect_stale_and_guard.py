"""resurrect-save.sh's default-server guard, and the watcher's stale-save chip.

Guard tests run the save script against throwaway `tmux -L u2test-*` servers
(-f /dev/null, a scratch HOME and @resurrect-dir, killed in cleanup). Watcher
tests use test_agent_jump_watcher's fake tmux; nothing touches the live server
or ~/.tmux/resurrect. Run with unittest from this directory.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

from test_agent_jump_watcher import FakeEnv, W, FAKE_STAT

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SAVE = SCRIPTS / "executable_resurrect-save.sh"
TMUX = shutil.which("tmux")


@unittest.skipUnless(TMUX, "tmux not installed")
class ScratchServer(unittest.TestCase):
    """A throwaway server at an EXPLICIT socket path (-S): every tmux call here
    names it, so none can fall through to the user's default server. The server
    is killed and its socket file removed in cleanup (kill-server leaves the
    file behind)."""

    def socket_path(self):
        return Path(f"/private/tmp/tmux-{os.getuid()}/u2test-{os.getpid()}-{id(self)}")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")   # short: a unix socket path caps at 104
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.dir = self.root / "resurrect"
        self.dir.mkdir()
        self.sock = self.socket_path()
        self.sock.parent.mkdir(mode=0o700, exist_ok=True)
        (self.root / "zdot").mkdir()
        (self.root / "zdot/.zshrc").write_text("PS1='$ '\n")
        self.env = dict(os.environ, HOME=str(self.root), ZDOTDIR=str(self.root / "zdot"))
        self.env.pop("TMUX", None)
        self.env.pop("RESURRECT_SAVE_ALLOW_SOCKET", None)
        self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "main", "-x", "80", "-y", "20")
        self.addCleanup(self.sock.unlink, missing_ok=True)
        self.addCleanup(lambda: subprocess.run([TMUX, "-S", str(self.sock), "kill-server"],
                                               capture_output=True))
        self.layout_marker = self.root / "layout-hook-ran"
        self.all_marker = self.root / "all-hook-ran"
        self.tmux("set", "-g", "exit-empty", "off", ";",
                  "set", "-g", "@resurrect-dir", str(self.dir), ";",
                  "set", "-g", "@resurrect-capture-pane-contents", "off", ";",
                  "set", "-g", "@resurrect-hook-post-save-layout", f"touch '{self.layout_marker}'", ";",
                  "set", "-g", "@resurrect-hook-post-save-all", f"touch '{self.all_marker}'")
        self.path = self.tmux("display", "-p", "#{socket_path}").strip()

    def tmux(self, *args):
        env = {k: v for k, v in self.env.items() if k != "TMUX"}
        return subprocess.run([TMUX, "-S", str(self.sock), *args], env=env, capture_output=True,
                              text=True, check=True).stdout

    def save(self, **extra):
        return subprocess.run(["bash", str(SAVE), "quiet"], env=dict(self.env, **extra),
                              capture_output=True, text=True, timeout=30)


class SaveGuardTests(ScratchServer):
    def assert_refused(self, r, reason):
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(list(self.dir.glob("tmux_resurrect_*")), [])
        self.assertFalse((self.dir / "last").exists())
        self.assertFalse((self.dir / ".save.lock").exists())
        self.assertFalse(self.layout_marker.exists(), "post-save-layout hook ran on a refused save")
        self.assertFalse(self.all_marker.exists(), "post-save-all hook ran on a refused save")
        log = (self.dir / "save.log").read_text()
        self.assertIn("REFUSED: not the default tmux server", log)
        self.assertIn(reason, log)
        # Loud even in quiet mode: the stale chip reads "save refused".
        self.assertEqual(self.tmux("show", "-gqv", "@resurrect_stale").strip(), "refused")

    def test_refuses_a_server_named_by_TMUX(self):
        # What continuum's #() job or prefix C-s on a test server looks like.
        self.assert_refused(self.save(TMUX=f"{self.path},0,0"), f"TMUX names socket {self.path}")

    def test_refuses_when_the_cli_reaches_another_server(self):
        # TMUX empty, yet the tmux CLI is steered at the test server.
        wrap = self.root / "bin"; wrap.mkdir()
        (wrap / "tmux").write_text(f'#!/bin/sh\nexec "{TMUX}" -S "{self.sock}" "$@"\n')
        (wrap / "tmux").chmod(0o755)
        r = self.save(PATH=f"{wrap}:{self.env['PATH']}")
        self.assert_refused(r, f"the tmux CLI reaches {self.path}")

    def test_refusal_creates_no_directory(self):
        gone = self.root / "never"
        self.tmux("set", "-g", "@resurrect-dir", str(gone))
        r = self.save(TMUX=f"{self.path},0,0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(gone.exists())

    def test_opt_in_saves_clears_the_chip_and_refreshes_an_unchanged_last(self):
        env = dict(TMUX=f"{self.path},0,0", RESURRECT_SAVE_ALLOW_SOCKET=self.path)
        self.tmux("set", "-g", "@resurrect_stale", "3h")
        r = self.save(**env)
        self.assertEqual(r.returncode, 0, r.stderr)
        target = (self.dir / "last").resolve()
        self.assertTrue(target.is_file())
        self.assertTrue(self.all_marker.exists())
        self.assertEqual(self.tmux("show", "-gqv", "@resurrect_stale").strip(), "")
        # An unchanged save keeps the old file but re-dates it: that mtime is the
        # watcher's "last save" clock.
        old = time.time() - 7200
        os.utime(target, (old, old))
        time.sleep(1.1)
        r = self.save(**env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.dir / "last").resolve(), target)
        self.assertEqual(len(list(self.dir.glob("tmux_resurrect_*.txt"))), 1)
        self.assertGreater(target.stat().st_mtime, time.time() - 60)
        self.assertTrue((self.dir / "last").is_symlink())


class DefaultNamedServerSaves(ScratchServer):
    """The ALLOWED path, with no opt-in: a server whose socket is named
    `default` (here under a scratch TMUX_TMPDIR) must save, both as run-shell /
    continuum run it (TMUX set) and from a plain shell (TMUX unset). A broken
    `${1##*/} = default` branch would otherwise pass every other test (they
    all opt in) while real autosave was refused.

    Known gap, by design: the rule is the socket's NAME, so any socket called
    `default` under another TMUX_TMPDIR (or via -S .../default) is accepted —
    the same rule as agent-tab-watcher.sh and continuum-ensure.sh. Stray test
    servers use `-L <name>`, which this does catch."""

    def socket_path(self):
        return Path(self.temp.name).resolve() / f"tmux-{os.getuid()}" / "default"

    def assert_saved(self, r):
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.dir / "last").resolve().is_file())
        self.assertTrue(self.all_marker.exists())
        log = (self.dir / "save.log").read_text()
        self.assertNotIn("REFUSED", log)
        self.assertIn("saved ", log)
        self.assertEqual(self.tmux("show", "-gqv", "@resurrect_stale").strip(), "")

    def test_saves_with_TMUX_set(self):
        self.assertTrue(self.path.endswith("/default"), self.path)
        self.assert_saved(self.save(TMUX=f"{self.path},0,0"))

    def test_saves_with_TMUX_unset(self):
        env = dict(TMUX_TMPDIR=str(self.root))
        # Safety first: this env must reach the SCRATCH server, never the
        # user's default one, before anything is allowed to save through it.
        probe = subprocess.run([TMUX, "display", "-p", "#{socket_path}"],
                               env={k: v for k, v in dict(self.env, **env).items() if k != "TMUX"},
                               capture_output=True, text=True)
        self.assertEqual(probe.stdout.strip(), self.path, "would not reach the scratch server")
        self.assert_saved(self.save(**env))


# The shared fake tmux has no target-less `display-message -p`; this wrapper
# answers that one form from the fake's globals and hands the rest on.
DISPLAY_WRAPPER = r'''#!/usr/bin/env python3
import fcntl, json, os, re, sys
from pathlib import Path
a = sys.argv[1:]
if a[:2] == ["display-message", "-p"] and len(a) == 3:
    p = Path(os.environ["FAKE_TMUX_STATE"])
    with p.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        s = json.loads(p.read_text())
        s["calls"].append(a)
        p.write_text(json.dumps(s))
    print(re.sub(r"#\{([@\w-]+)\}", lambda m: str(s["globals"].get(m.group(1), "")), a[2]))
    sys.exit(0)
os.execv(os.path.join(os.path.dirname(__file__), "tmux-base"), ["tmux"] + a)
'''


class StaleChipTests(unittest.TestCase):
    def setUp(self):
        self.f = FakeEnv(windows={"@1": W("main", 1)}, panes=[{"window": "@1", "tty": "/dev/ttys901"}])
        self.addCleanup(self.f.close)
        (self.f.bin / "tmux").rename(self.f.bin / "tmux-base")
        (self.f.bin / "tmux").write_text(DISPLAY_WRAPPER); (self.f.bin / "tmux").chmod(0o755)
        (self.f.bin / "stat").write_text(FAKE_STAT); (self.f.bin / "stat").chmod(0o755)
        self.stat_log = self.f.dir / "stat.log"
        self.rdir = self.f.home / "resurrect"
        self.rdir.mkdir()
        self.target = self.rdir / "tmux_resurrect_20261007T000000.txt"
        self.target.write_text("pane\n")
        (self.rdir / "last").symlink_to(self.target.name)
        self.set_globals(**{"@resurrect-dir": "~/resurrect"})

    def set_globals(self, **g):
        s = self.f.read(); s["globals"].update(g); self.f.state.write_text(json.dumps(s))

    def age(self, seconds):
        t = time.time() - seconds
        os.utime(self.target, (t, t))

    def run_watcher(self, ticks, every="0", **extra):
        env = dict(AGENT_TAB_WATCHER_MAX_TICKS=str(ticks), FAKE_STAT_LOG=str(self.stat_log), **extra)
        if every is not None:
            env["AGENT_TAB_WATCHER_STALE_EVERY"] = every
        r = self.f.run("agent-tab-watcher.sh", timeout=30, **env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.f.read()

    def last_stats(self):
        lines = self.stat_log.read_text().splitlines() if self.stat_log.exists() else []
        return [ln for ln in lines if ln.endswith("/last")]

    def test_old_snapshot_sets_the_chip_and_redraws(self):
        self.age(3 * 3600 + 120)
        s = self.run_watcher(1)
        self.assertEqual(s["globals"].get("@resurrect_stale"), "3h")
        self.assertIn(["refresh-client", "-S"], s["calls"])

    def test_labels(self):
        for secs, want in ((75 * 60, "75m"), (5 * 3600, "5h"), (3 * 86400, "3d")):
            self.age(secs)
            self.assertEqual(self.run_watcher(1)["globals"].get("@resurrect_stale"), want, secs)

    def test_fresh_snapshot_clears_a_leftover_chip(self):
        self.age(10 * 60)
        self.set_globals(**{"@resurrect_stale": "5h"})
        self.assertNotIn("@resurrect_stale", self.run_watcher(1)["globals"])

    def test_threshold_is_an_option_and_zero_turns_it_off(self):
        self.age(3 * 3600)
        self.set_globals(**{"@resurrect-stale-minutes": "240"})
        self.assertNotIn("@resurrect_stale", self.run_watcher(1)["globals"])
        self.set_globals(**{"@resurrect-stale-minutes": "0"})
        self.age(30 * 86400)
        self.stat_log.unlink(missing_ok=True)
        self.assertNotIn("@resurrect_stale", self.run_watcher(1)["globals"])
        self.assertEqual(self.last_stats(), [])

    def test_no_last_means_no_chip(self):
        (self.rdir / "last").unlink()
        self.assertNotIn("@resurrect_stale", self.run_watcher(1)["globals"])
        self.assertEqual(self.last_stats(), [])

    def test_healthy_looks_are_fork_free_and_options_read_once(self):
        # Fresh: the first look stats once (no bound yet); every later look is
        # the builtin -nt compare against its stamp.
        self.age(5 * 60)
        s = self.run_watcher(4)
        self.assertEqual(len(self.last_stats()), 1, self.last_stats())
        self.assertNotIn("@resurrect_stale", s["globals"])
        reads = [c for c in s["calls"] if c[:2] == ["display-message", "-p"] and len(c) == 3]
        self.assertEqual(len(reads), 1, reads)
        # A chip that was never set is unset at most once (the first look reconciles).
        unsets = [c for c in s["calls"] if c[:2] == ["set-option", "-gu"] and "@resurrect_stale" in c]
        self.assertLessEqual(len(unsets), 1, unsets)

    def test_a_landing_save_clears_the_chip(self):
        self.age(2 * 3600)
        def save():
            self.target.write_text("pane\npane\n")   # in place: mtime = now
        t = threading.Timer(1.5, save); t.start(); self.addCleanup(t.cancel)
        s = self.run_watcher(5)
        self.assertNotIn("@resurrect_stale", s["globals"])
        sets = [c for c in s["calls"] if c[:2] == ["set-option", "-g"] and "@resurrect_stale" in c]
        self.assertTrue(sets, "the chip was never shown before the save")

    def test_default_period_does_not_look_in_the_first_minute(self):
        self.age(3 * 3600)
        s = self.run_watcher(2, every=None)
        self.assertNotIn("@resurrect_stale", s["globals"])
        self.assertFalse([c for c in s["calls"] if c[:2] == ["display-message", "-p"] and len(c) == 3])

    def test_stamp_is_cleaned_up(self):
        self.age(5 * 60)
        self.run_watcher(2)
        self.assertEqual([p.name for p in self.f.tmp.iterdir() if ".stamp." in p.name], [])


if __name__ == "__main__":
    unittest.main()
