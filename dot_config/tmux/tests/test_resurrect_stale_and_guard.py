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
        prefix = os.environ.get("RESURRECT_TEST_SOCKET_PREFIX", "u2test")
        return Path(f"/private/tmp/tmux-{os.getuid()}/{prefix}-{os.getpid()}-{id(self)}")

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
        self.env.pop("TMUX", None)        # never inherit the caller's server
        self.env.pop("TMUX_PANE", None)
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
                  "set", "-g", "@resurrect-startup-grace-seconds", "0", ";",   # servers here are seconds old
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


class PrivateDefaultServerRefused(ScratchServer):
    """A PRIVATE server whose socket is named `default` — what every agent's
    server is under its own TMUX_TMPDIR — is not the real default server, and
    with no opt-in nothing may save it into @resurrect-dir: not the save (TMUX
    set, as run-shell/continuum run it, or unset with the CLI steered there by
    TMUX_TMPDIR), not resurrect-save-soon.sh, and continuum-ensure.sh must not
    add the autosave hook. Until 2026-10-15 all three matched the socket's
    basename and accepted it. (The real default socket is only ever named by
    path in the scripts; no test may touch it.)"""

    def socket_path(self):
        return Path(self.temp.name).resolve() / f"tmux-{os.getuid()}" / "default"

    def assert_refused(self, r, reason):
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(list(self.dir.glob("tmux_resurrect_*")), [])
        self.assertFalse((self.dir / "last").exists())
        self.assertFalse(self.all_marker.exists())
        self.assertIn(f"REFUSED: not the default tmux server — {reason}", (self.dir / "save.log").read_text())

    def unset_env(self):
        env = dict(TMUX_TMPDIR=str(self.root))
        # Safety first: this env must reach the SCRATCH server, never the
        # user's default one.
        probe = subprocess.run([TMUX, "display", "-p", "#{socket_path}"],
                               env={k: v for k, v in dict(self.env, **env).items() if k != "TMUX"},
                               capture_output=True, text=True)
        self.assertEqual(probe.stdout.strip(), self.path, "would not reach the scratch server")
        return env

    def test_save_refused_with_TMUX_set(self):
        self.assertTrue(self.path.endswith(f"/tmux-{os.getuid()}/default"), self.path)
        self.assert_refused(self.save(TMUX=f"{self.path},0,0"), f"TMUX names socket {self.path}")

    def test_save_refused_with_TMUX_unset(self):
        self.assert_refused(self.save(**self.unset_env()), f"the tmux CLI reaches {self.path}")

    def test_final_refused(self):
        r = subprocess.run(["bash", str(SAVE), "final"], env=dict(self.env, TMUX=f"{self.path},0,0"),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)

    def test_save_soon_does_nothing(self):
        tmp = self.root / "tmp"
        tmp.mkdir()
        env = dict(self.env, TMUX=f"{self.path},0,0", TMPDIR=str(tmp), RESURRECT_SAVE_SOON_DELAY="0.1",
                   RESURRECT_SAVE_SCRIPT=str(SAVE))
        r = subprocess.run(["bash", str(SCRIPTS / "executable_resurrect-save-soon.sh"), "full"], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(list(tmp.iterdir()), [])
        self.assertFalse((self.dir / "save.log").exists())
        self.assertEqual(list(self.dir.glob("tmux_resurrect_*")), [])

    def test_continuum_ensure_adds_no_autosave_hook(self):
        fake = self.root / ".config/tmux/plugins/tmux-continuum/scripts/continuum_save.sh"
        fake.parent.mkdir(parents=True)
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        self.tmux("set", "-g", "status-right", "untouched")
        ensure = SCRIPTS / "executable_continuum-ensure.sh"
        for env in (dict(TMUX=f"{self.path},0,0"), self.unset_env()):
            # self.env never holds TMUX; the unset case reaches the scratch
            # server through TMUX_TMPDIR (unset_env() proved it).
            r = subprocess.run(["bash", str(ensure)], env=dict(self.env, **env), capture_output=True,
                               text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self.tmux("show", "-gv", "status-right").strip(), "untouched", env)


class FinalSaveRefusal(ScratchServer):
    def test_a_refused_final_save_fails_loudly(self):
        # kill-guard's go/no-go: a refusal must never read as "saved".
        r = subprocess.run(["bash", str(SAVE), "final"], env=dict(self.env, TMUX=f"{self.path},0,0"),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(list(self.dir.glob("tmux_resurrect_*")), [])
        self.assertIn("REFUSED", (self.dir / "save.log").read_text())


GUARD = SCRIPTS / "executable_resurrect-guard.sh"


class ResurrectGuardTests(unittest.TestCase):
    """resurrect-guard.sh on its own, against scratch save files. RESURRECT_DIR
    and RESURRECT_EXCLUDE come from the env, so it never asks tmux; TMUX names
    a socket that does not exist, so a call that slipped through would fail
    rather than reach a real server."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        # Server facts resurrect-save.sh passes in. Set-but-empty = unknown
        # (no tmux query): the guard then never treats the server as a
        # replacement, its behaviour before replacement detection existed.
        self.env = dict(os.environ, RESURRECT_DIR=str(self.dir), RESURRECT_EXCLUDE="agents tasks",
                        TMUX="/nonexistent/unitA-guard-sock,0,0", RESURRECT_SERVER_START="",
                        RESURRECT_RESTORED="", RESURRECT_RESTORING="")
        self.env.pop("RESURRECT_SAVE_MODE", None)
        self.env.pop("TMUX_PANE", None)
        self.n = 0
        # Snapshot names carry their capture time; these start 6h ago.
        self.base = int(time.time()) - 6 * 3600
        (self.dir / "assistant-sessions.json").write_text("{}")

    def write(self, panes, session="main", extra=()):
        self.n += 1
        name = "tmux_resurrect_%s.txt" % time.strftime("%Y%m%dT%H%M%S", time.localtime(self.base + self.n))
        rows = "".join(f"pane\t{session}\t{i}\t1\t:*\t0\tt\t:/\t1\tzsh\t:\n" for i in range(panes))
        rows += "".join(f"pane\t{s}\t{i}\t1\t:*\t0\tt\t:/\t1\tzsh\t:\n" for s, n in extra for i in range(n))
        (self.dir / name).write_text(rows + "state\tmain\t\n")
        return name

    def run_guard(self, *args, mode=None, **extra):
        env = dict(self.env, **({"RESURRECT_SAVE_MODE": mode} if mode else {}), **extra)
        return subprocess.run(["bash", str(GUARD), *args], env=env, capture_output=True, text=True, timeout=30)

    def save(self, panes, mode=None, extra=(), **server):
        """Write a snapshot of `panes` panes, point `last` at it, run the guard."""
        name = self.write(panes, extra=extra)
        last = self.dir / "last"
        last.unlink(missing_ok=True)
        last.symlink_to(name)
        r = self.run_guard(mode=mode, **server)
        self.assertEqual(r.returncode, 0, r.stderr)
        return name

    def capture_epoch(self, name):
        return int(time.mktime(time.strptime(name[len("tmux_resurrect_"):-4], "%Y%m%dT%H%M%S")))

    def replacement(self, good, uptime=600, **more):
        """Server facts for a server started AFTER `good` was captured."""
        start = max(self.capture_epoch(good) + 60, int(time.time()) - uptime)
        return dict(RESURRECT_SERVER_START=str(start), **more)

    def last(self):
        return os.readlink(self.dir / "last")

    def state(self):
        return dict(line.split("=", 1) for line in (self.dir / ".guard-state").read_text().splitlines())

    def test_two_shrunk_saves_seconds_apart_never_confirm_a_crash(self):
        good = self.save(10)
        self.save(1)
        self.save(1)
        self.save(1)
        self.assertEqual(self.last(), good)
        st = self.state()
        self.assertEqual(st["good_file"], good)
        self.assertEqual(st["streak"], "3")
        self.assertTrue(st["first_reject"].isdigit())
        self.assertIn("shrunk for", (self.dir / "guard.log").read_text())

    def test_a_shrink_held_for_ten_minutes_is_accepted(self):
        good = self.save(10)
        self.save(1)
        st = self.state()
        (self.dir / ".guard-state").write_text(
            f"good_file={good}\nstreak={st['streak']}\nfirst_reject={int(st['first_reject']) - 601}\n")
        shrunk = self.save(1)
        self.assertEqual(self.last(), shrunk)
        self.assertEqual(self.state(), dict(good_file=shrunk, streak="0", first_reject=""))
        self.assertIn("shrink confirmed 2x over", (self.dir / "guard.log").read_text())

    def test_time_alone_is_not_enough(self):
        # One rejected save, however old, is still one: the streak must hold too.
        good = self.save(10)
        (self.dir / ".guard-state").write_text(f"good_file={good}\nstreak=0\nfirst_reject={int(time.time()) - 3600}\n")
        self.save(1)
        self.assertEqual(self.last(), good)

    def test_old_state_file_without_first_reject_starts_the_clock_now(self):
        good = self.save(10)
        (self.dir / ".guard-state").write_text(f"good_file={good}\nstreak=1\n")
        self.save(1)
        self.assertEqual(self.last(), good)
        st = self.state()
        self.assertEqual(st["streak"], "2")
        self.assertLess(abs(int(st["first_reject"]) - time.time()), 30)

    def test_recovery_resets_the_streak_and_the_clock(self):
        self.save(10)
        self.save(1)
        back = self.save(10)
        self.assertEqual(self.last(), back)
        self.assertEqual(self.state(), dict(good_file=back, streak="0", first_reject=""))

    def test_a_final_save_is_never_reverted(self):
        self.save(10)
        self.save(1)                      # rejected: last reverted
        final = self.save(1, mode="final")
        self.assertEqual(self.last(), final)
        self.assertEqual(self.state()["good_file"], final)
        self.assertIn("final save before a deliberate kill", (self.dir / "guard.log").read_text())

    # --- replacement servers (review round 1, MED-2) ---------------------------

    def held_too_long(self, good):
        """State as if the shrink had been rejected many times over an hour."""
        (self.dir / ".guard-state").write_text(
            f"good_file={good}\nstreak=9\nfirst_reject={int(time.time()) - 3600}\n")

    def test_a_replacement_server_never_confirms_a_shrink_however_long(self):
        good = self.save(10)
        self.held_too_long(good)
        self.save(1, **self.replacement(good, uptime=3600))
        self.assertEqual(self.last(), good)
        self.assertIn("replacement server never restored", (self.dir / "guard.log").read_text())

    def trust_state(self, good, run, ago, streak=9, held=4 * 3600):
        """A hold streak whose last counted hold was `ago` seconds back."""
        now = int(time.time())
        (self.dir / ".guard-state").write_text(
            f"good_file={good}\nstreak={streak}\nfirst_reject={now - held}\n"
            f"trust_run={run}\ntrust_at={now - ago}\nlast_hold={now - ago}\n")

    def test_uptime_alone_never_trusts_a_replacement_server(self):
        # Round 2: wall-clock uptime (like wall-clock `held`) proves nothing.
        good = self.save(10)
        self.held_too_long(good)
        self.save(1, **self.replacement(good, uptime=5 * 3600))
        self.assertEqual(self.last(), good)
        self.assertEqual(self.state()["trust_run"], "1")

    def test_laptop_sleep_restarts_the_trust_count(self):
        # Crash at 18:00, restore skipped, lid closed; first save at 08:00:
        # 14h of uptime and of `held`, but the previous hold was 14h ago.
        self.base = int(time.time()) - 20 * 3600
        good = self.save(10)
        self.trust_state(good, run=15, ago=14 * 3600, held=14 * 3600)
        self.save(1, **self.replacement(good, uptime=14 * 3600))
        self.assertEqual(self.last(), good)
        st = self.state()
        self.assertEqual((st["trust_run"], st["last_hold"]), ("1", st["trust_at"]))
        self.assertIn("1/16 holds in a row", (self.dir / "guard.log").read_text())

    def test_holds_closer_than_the_rejudge_interval_count_once(self):
        good = self.save(10)
        self.trust_state(good, run=5, ago=60)
        self.save(1, **self.replacement(good, uptime=5 * 3600))
        self.assertEqual(self.state()["trust_run"], "5")

    def test_sixteen_holds_fifteen_minutes_apart_trust_a_fresh_server(self):
        good = self.save(10)
        server = self.replacement(good, uptime=5 * 3600)
        shrunk = None
        for i in range(1, 17):
            if i > 1:   # as if the previous hold ran 15 minutes ago
                self.trust_state(good, run=int(self.state()["trust_run"]), ago=900,
                                 streak=int(self.state()["streak"]))
            shrunk = self.save(1, **server)
            if i < 16:
                self.assertEqual(self.last(), good, f"accepted after only {i} holds")
                self.assertEqual(self.state()["trust_run"], str(i))
        self.assertEqual(self.last(), shrunk)
        self.assertEqual(self.state(), dict(good_file=shrunk, streak="0", first_reject=""))
        self.assertIn("16 holds in a row", (self.dir / "guard.log").read_text())

    def test_an_ordinary_server_keeps_streak_and_ten_minutes(self):
        # Not a replacement: no trust count, the old rule, three-line state.
        good = self.save(10)
        self.save(1)
        self.assertNotIn("trust_run", self.state())
        self.held_too_long(good)
        shrunk = self.save(1)
        self.assertEqual(self.last(), shrunk)

    def test_a_restored_replacement_server_follows_the_normal_rule(self):
        good = self.save(10)
        server = self.replacement(good, RESURRECT_RESTORED=str(int(time.time()) - 60))
        self.save(1, **server)
        self.assertEqual(self.last(), good)        # streak 1: still held
        self.held_too_long(good)
        shrunk = self.save(1, **server)
        self.assertEqual(self.last(), shrunk)

    def test_the_servers_own_good_save_is_not_a_replacement(self):
        good = self.save(10)
        start = dict(RESURRECT_SERVER_START=str(self.capture_epoch(good) - 60))
        self.held_too_long(good)
        shrunk = self.save(1, **start)
        self.assertEqual(self.last(), shrunk)

    # --- check mode: the verdict before `last` moves (MED-3) -------------------

    def test_check_rejects_without_touching_last_and_counts_once(self):
        good = self.save(10)
        shrunk = self.write(1)
        r = self.run_guard("check", shrunk)
        self.assertEqual(r.returncode, 2, r.stderr)
        held_on, reason = r.stdout.rstrip("\n").split("\x1f")
        self.assertEqual((held_on, self.last()), (good, good))
        self.assertIn("1 pane(s) vs 10", reason)
        self.assertEqual(self.state()["streak"], "1")
        self.assertEqual(self.run_guard("check", self.write(1)).returncode, 2)
        self.assertEqual(self.state()["streak"], "2")

    def test_check_accepts_without_writing_state_and_the_bookkeeping_run_agrees(self):
        good = self.save(10)
        (self.dir / ".guard-state").write_text(f"good_file={good}\nstreak=1\nfirst_reject={int(time.time()) - 601}\n")
        before = (self.dir / ".guard-state").read_text()
        shrunk = self.write(1)
        r = self.run_guard("check", shrunk)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertEqual((self.dir / ".guard-state").read_text(), before)
        (self.dir / "last").unlink()
        (self.dir / "last").symlink_to(shrunk)
        self.assertEqual(self.run_guard().returncode, 0)
        self.assertEqual(self.last(), shrunk)
        self.assertEqual(self.state(), dict(good_file=shrunk, streak="0", first_reject=""))
        self.assertEqual(self.run_guard().returncode, 0)              # idempotent
        self.assertEqual(self.state(), dict(good_file=shrunk, streak="0", first_reject=""))

    def test_check_holds_a_final_save_on_a_replacement_that_never_restored(self):
        good = self.save(1)
        r = self.run_guard("check", self.write(5), mode="final", **self.replacement(good))
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout.split("\x1f")[1].strip(), "replacement server never restored")
        restored = self.replacement(good, RESURRECT_RESTORED=str(int(time.time())))
        self.assertEqual(self.run_guard("check", self.write(1), mode="final", **restored).returncode, 0)

    def test_check_holds_a_final_save_mid_restore(self):
        self.save(10)
        live = dict(RESURRECT_RESTORING=str(int(time.time()) - 5))
        r = self.run_guard("check", self.write(10), mode="final", **live)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout.split("\x1f")[1].strip(), "restore in progress")
        for stale in (str(int(time.time()) - 901), str(int(time.time()) + 3600), "x"):
            self.assertEqual(self.run_guard("check", self.write(10), mode="final",
                                            RESURRECT_RESTORING=stale).returncode, 0, stale)

    # --- MED-4 / LOW-7 ---------------------------------------------------------

    def test_a_save_identical_to_the_good_one_resets_the_hold(self):
        good = self.save(10)
        self.save(1)
        self.assertEqual(self.state()["streak"], "1")
        # resurrect-save.sh kept `last` on the good file (identical content).
        self.assertEqual(self.last(), good)
        self.assertEqual(self.run_guard().returncode, 0)
        self.assertEqual(self.state(), dict(good_file=good, streak="0", first_reject=""))

    def test_exclusions_are_shell_patterns(self):
        self.env["RESURRECT_EXCLUDE"] = "agents tasks ptymcp-*"
        self.save(10, extra=(("ptymcp-1", 20), ("ptymcp-two", 5)))
        same = self.save(10)        # the throwaways went away: not a shrink
        self.assertEqual(self.last(), same)
        self.assertEqual(self.state()["streak"], "0")


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
