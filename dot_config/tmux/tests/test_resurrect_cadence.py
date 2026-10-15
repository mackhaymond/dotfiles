"""Save cadence: resurrect-save.sh's gates, fingerprint fast path and `final`
mode, and resurrect-save-soon.sh's debounce (docs/resurrect-cadence.md).

Every test runs against its own throwaway server (test_resurrect_fast_paths'
ThrowawayServer: `-f /dev/null`, a scratch HOME and @resurrect-dir, killed and
its socket removed in cleanup). Nothing touches the live server or
~/.tmux/resurrect.
"""
import os
from pathlib import Path
import subprocess
import time
import unittest

from test_resurrect_fast_paths import SCRIPTS, SAVE, TMUX as TMUX_BIN, ThrowawayServer

SOON = SCRIPTS / "executable_resurrect-save-soon.sh"
GUARD = SCRIPTS / "executable_resurrect-guard.sh"


class Cadence(ThrowawayServer):
    def setUp(self):
        super().setUp()
        self.hooks = self.root / "hooks-ran"
        self.tmux("set", "-g", "@resurrect-capture-pane-contents", "off", ";",
                  "set", "-g", "@resurrect-hook-post-save-all", f"echo x >> '{self.hooks}'")
        self.wait_for_prompt("main:")

    def run_save(self, *mode, **extra):
        return subprocess.run(["bash", str(SAVE), *mode], env=dict(self.env, **extra),
                              capture_output=True, text=True, timeout=60)

    def auto(self, mode="auto"):
        r = self.run_save(mode)
        self.assertEqual(r.returncode, 0, r.stderr)

    def full_saves(self):
        log = self.dir / "save.log"
        return sum(" saved " in f" {line}" for line in log.read_text().splitlines()) if log.exists() else 0

    def settle(self):
        """automatic-rename names a new window "tmux" for a moment."""
        for _ in range(50):
            if "tmux" not in self.tmux("list-windows", "-a", "-F", "#{window_name}").split():
                return
            time.sleep(0.1)

    def hook_runs(self):
        return len(self.hooks.read_text().splitlines()) if self.hooks.exists() else 0

    def snapshots(self):
        return sorted(self.dir.glob("tmux_resurrect_*.txt"))


class FingerprintTests(Cadence):
    def test_unchanged_layout_only_redates_last(self):
        self.auto()
        self.assertEqual((self.full_saves(), self.hook_runs()), (1, 1))
        target = (self.dir / "last").resolve()
        old = time.time() - 3600
        os.utime(target, (old, old))
        log = (self.dir / "save.log").read_text()
        for mode in ("auto", "quiet"):
            self.auto(mode)
        self.assertEqual((self.full_saves(), self.hook_runs()), (1, 1))
        self.assertEqual((self.dir / "save.log").read_text(), log, "the fast path logs nothing")
        self.assertGreater(target.stat().st_mtime, time.time() - 60)
        self.assertFalse((self.dir / ".save.lock").exists())

    def test_volatile_state_does_not_force_a_full_save(self):
        self.tmux("select-pane", "-t", "main:", "-T", "✳ Tidy the parser", ";", "new-window", "-d", "-t", "main:")
        self.wait_for_prompt("main:1")
        for _ in range(50):   # automatic-rename names a new window "tmux" for a moment
            if self.tmux("display", "-p", "-t", "main:1", "#{window_name}") == "zsh\n":
                break
            time.sleep(0.1)
        self.auto()
        # Window switch, scrollback, the agent's spinner glyph, a client-less
        # layout change: none of it is what a restore rebuilds.
        self.tmux("select-window", "-t", "main:1", ";", "select-pane", "-t", "main:0", "-T", "⠐ Tidy the parser")
        self.tmux("send-keys", "-t", "main:1", "echo noise", "Enter")
        time.sleep(0.3)
        self.auto()
        self.assertEqual(self.full_saves(), 1)

    def test_structural_changes_force_a_full_save(self):
        self.tmux("select-pane", "-t", "main:", "-T", "✳ Tidy the parser")
        self.auto()
        steps = [
            ("new window", lambda: self.tmux("new-window", "-d", "-t", "main:")),
            ("window renamed", lambda: self.tmux("rename-window", "-t", "main:0", "work")),
            ("split", lambda: self.tmux("split-window", "-d", "-t", "main:0")),
            ("new session", lambda: self.tmux("new-session", "-d", "-s", "other")),
            ("session renamed", lambda: self.tmux("rename-session", "-t", "other", "other2")),
            ("tab identity", lambda: self.tmux("select-pane", "-t", "main:0.0", "-T", "✳ Write the release notes")),
            ("command", lambda: self.tmux("send-keys", "-t", "main:0.0", "sleep 300", "Enter")),
            ("pane closed", lambda: self.tmux("kill-pane", "-t", "main:0.1")),
        ]
        for n, (what, change) in enumerate(steps, start=2):
            change()
            time.sleep(0.4)
            self.auto()
            self.assertEqual(self.full_saves(), n, what)

    def test_excluded_sessions_never_force_a_full_save(self):
        self.auto()
        self.tmux("new-session", "-d", "-s", "agents", ";", "new-window", "-d", "-t", "agents:", ";",
                  "new-session", "-d", "-s", "tasks")
        self.auto()
        self.assertEqual(self.full_saves(), 1)

    def test_a_full_save_still_happens_every_n_minutes(self):
        self.auto()
        fp = self.dir / ".save-fingerprint"
        when, rest = fp.read_text().split("\n", 1)
        fp.write_text(f"{int(when) - 16 * 60}\n{rest}")
        self.auto()
        self.assertEqual(self.full_saves(), 2)
        self.auto()
        self.assertEqual(self.full_saves(), 2)
        self.tmux("set", "-g", "@resurrect-full-every-minutes", "0")   # 0: always full
        self.auto()
        self.assertEqual(self.full_saves(), 3)

    def test_manual_and_auto_full_saves_are_always_full(self):
        self.auto()
        self.auto("auto-full")
        self.assertEqual(self.full_saves(), 2)
        r = self.run_save()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.full_saves(), 3)

    def test_no_fingerprint_is_recorded_while_last_is_held_elsewhere(self):
        # What a guard revert looks like from here: `last` moved off this save.
        keep = self.dir / "tmux_resurrect_20200101T000000.txt"
        keep.write_text("pane\n")
        self.tmux("set", "-g", "@resurrect-hook-post-save-all", f"ln -sfn '{keep.name}' '{self.dir}/last'")
        self.auto()
        self.assertFalse((self.dir / ".save-fingerprint").exists())
        self.tmux("set", "-gu", "@resurrect-hook-post-save-all")
        self.auto()    # same layout, but nothing vouches for `last`: full save
        self.assertEqual(self.full_saves(), 2)
        self.assertTrue((self.dir / ".save-fingerprint").exists())

    def test_fast_path_cost(self):
        # ~20 panes, like Mack's server. Reported, loosely bounded.
        for i in range(9):
            self.tmux("new-window", "-d", "-t", "main:", ";", "split-window", "-d", "-t", f"main:{i + 1}")
        self.tmux("new-session", "-d", "-s", "agents")
        time.sleep(0.5)
        self.auto()
        runs = []
        for _ in range(20):
            t = time.perf_counter()
            self.auto()
            runs.append(time.perf_counter() - t)
        self.assertEqual(self.full_saves(), 1)
        runs.sort()
        print(f"\nfast path with {self.tmux('list-panes', '-a').count(chr(10))} panes: "
              f"median {runs[10] * 1000:.1f} ms, max {runs[-1] * 1000:.1f} ms")
        self.assertLess(runs[10], 0.25)


class GateTests(Cadence):
    def assert_skipped(self, *modes):
        for mode in modes:
            self.auto(mode)
        self.assertEqual(self.snapshots(), [])
        self.assertFalse((self.dir / "save.log").exists())
        self.assertEqual(self.hook_runs(), 0)

    def test_a_young_server_is_not_autosaved_but_manual_saves_work(self):
        self.tmux("set", "-gu", "@resurrect-startup-grace-seconds")    # back to the 120s default
        self.assert_skipped("quiet", "auto", "auto-full")
        r = self.run_save()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.snapshots()), 1)

    def test_a_restore_in_progress_blocks_autosaves_until_it_ends_or_goes_stale(self):
        self.tmux("set", "-g", "@resurrect_restoring", str(int(time.time())))
        self.assert_skipped("quiet", "auto", "auto-full")
        self.tmux("set", "-g", "@resurrect_restoring", str(int(time.time()) - 901))   # a restore that died
        self.auto()
        self.assertEqual(self.full_saves(), 1)

    def test_kill_guard_shutdown_blocks_autosaves_but_not_final(self):
        self.tmux("set", "-g", "@kill_guard_shutdown", str(int(time.time())))
        self.assert_skipped("quiet", "auto", "auto-full")
        r = self.run_save("final")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.full_saves(), 1)

    def test_a_stale_or_garbage_shutdown_flag_does_not_stop_autosave(self):
        # A guard that died mid-kill must not silence autosave forever.
        for n, value in enumerate((str(int(time.time()) - 121), "1", "yes", "-5"), start=1):
            self.tmux("set", "-g", "@kill_guard_shutdown", value)
            self.auto("auto-full")
            self.assertEqual(self.full_saves(), n, value)

    def test_no_utf8_locale_still_parses_rows(self):
        # A non-UTF-8 tmux client prints \x1f and tab in -F output as `_`.
        base = {k: v for k, v in self.env.items() if k not in ("LANG", "LC_ALL", "LC_CTYPE")}
        for extra in ({}, {"LC_ALL": "C"}, {"LANG": "C"}):
            env = dict(base, **extra)
            r = subprocess.run(["bash", str(SAVE), "auto-full"], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            rows = [l.split("\t") for l in (self.dir / "last").resolve().read_text().splitlines()
                    if l.startswith("pane\t")]
            self.assertEqual([len(row) for row in rows], [11], extra)
            self.assertEqual(rows[0][1], "main", extra)
            # ...and the fingerprint matches across locales: the fast path holds.
            n = self.full_saves()
            r = subprocess.run(["bash", str(SAVE), "auto"], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual((r.returncode, self.full_saves()), (0, n), extra)
        self.assertTrue((self.dir / ".save-fingerprint").read_text().split("\n")[1].startswith("main\x1f0\x1f"))

    def test_final_ignores_the_startup_grace(self):
        self.tmux("set", "-gu", "@resurrect-startup-grace-seconds")
        r = self.run_save("final")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.snapshots()), 1)


class FinalTests(Cadence):
    def final(self, **extra):
        return self.run_save("final", **extra)

    def test_final_saves_and_reports_success(self):
        r = self.final()
        self.assertEqual(r.returncode, 0, r.stderr)
        target = (self.dir / "last").resolve()
        self.assertEqual(target.name, self.snapshots()[-1].name)
        self.assertEqual(self.hook_runs(), 1)
        # Identical layout: still 0, `last` kept and re-dated.
        old = time.time() - 3600
        os.utime(target, (old, old))
        time.sleep(1.1)
        r = self.final()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.dir / "last").resolve(), target)
        self.assertGreater(target.stat().st_mtime, time.time() - 60)

    def test_final_is_full_even_when_the_fingerprint_matches(self):
        self.auto()
        self.assertEqual(self.final().returncode, 0)
        self.assertEqual(self.full_saves(), 2)

    def test_final_waits_for_a_running_save(self):
        lock = self.dir / ".save.lock"
        lock.mkdir()
        t = time.monotonic()
        proc = subprocess.Popen(["bash", str(SAVE), "final"], env=self.env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        self.assertIsNone(proc.poll(), "final gave up instead of waiting")
        lock.rmdir()
        self.assertEqual(proc.wait(timeout=30), 0)
        self.assertGreater(time.monotonic() - t, 1.4)
        self.assertEqual(self.full_saves(), 1)

    def test_final_fails_when_the_lock_never_frees(self):
        (self.dir / ".save.lock").mkdir()
        r = self.final(RESURRECT_FINAL_LOCK_WAIT="1")
        self.assertEqual(r.returncode, 3)
        self.assertEqual(self.snapshots(), [])
        self.assertIn("lock was still held", (self.dir / "save.log").read_text())
        # The other modes keep exiting 0 on a held lock.
        self.assertEqual(self.run_save("auto").returncode, 0)

    def test_final_on_an_all_excluded_server_keeps_last_and_succeeds(self):
        # Post-integration: a server holding only excluded sessions (pty-mcp's
        # `agents` after a crash) must still be killable through the guard:
        # rc 0, `last` re-dated, and the "saved N panes in" line it checks.
        self.assertEqual(self.run_save().returncode, 0)
        good = (self.dir / "last").resolve()
        old = time.time() - 3600
        os.utime(good, (old, old))
        self.tmux("set", "-g", "@resurrect-exclude-sessions", "main")
        start = time.time()
        r = self.final()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.dir / "last").resolve(), good)
        self.assertGreaterEqual(good.stat().st_mtime, int(start))
        self.assertRegex((self.dir / "save.log").read_text(),
                         r"saved 0 panes in [0-9.]+s \(nothing to save; kept last\)")
        # quiet/auto are unchanged: nothing saved, still exit 0.
        self.assertEqual(self.run_save("auto").returncode, 0)
        self.assertEqual(len(self.snapshots()), 1)

    def test_saves_in_the_same_second_never_overwrite_each_other(self):
        self.assertEqual(self.run_save().returncode, 0)
        first = (self.dir / "last").resolve()
        content = first.read_text()
        self.tmux("new-window", "-d", "-t", "main:")
        self.assertEqual(self.run_save().returncode, 0)
        self.assertEqual(first.read_text(), content)
        self.assertEqual(len(self.snapshots()), 2)

    def test_final_fails_when_a_hook_moves_last(self):
        keep = self.dir / "tmux_resurrect_20200101T000000.txt"
        keep.write_text("pane\n")
        self.tmux("set", "-g", "@resurrect-hook-post-save-all", f"ln -sfn '{keep.name}' '{self.dir}/last'")
        self.assertEqual(self.final().returncode, 4)

    def test_the_real_guard_accepts_a_final_shrink(self):
        self.tmux("set", "-g", "@resurrect-hook-post-save-all",
                  f"RESURRECT_DIR='{self.dir}' bash '{GUARD}'")
        for _ in range(5):
            self.tmux("new-window", "-d", "-t", "main:")
        time.sleep(0.3)
        self.assertEqual(self.run_save().returncode, 0)
        good = (self.dir / "last").resolve()
        for i in range(1, 6):
            self.tmux("kill-window", "-t", f"main:{i}")
        self.assertEqual(self.run_save().returncode, 0)
        self.assertEqual((self.dir / "last").resolve(), good, "an ordinary shrink is held back")
        r = self.final()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual((self.dir / "last").resolve(), good)
        rows = (self.dir / "last").resolve().read_text().splitlines()
        self.assertEqual(sum(row.startswith("pane\t") for row in rows), 1)
        self.assertIn("final save before a deliberate kill", (self.dir / "guard.log").read_text())


class ReviewRound1Tests(Cadence):
    """Review round 1 (2026-10-15): each test failed before its fix."""

    def guard_state(self):
        p = self.dir / ".guard-state"
        return dict(l.split("=", 1) for l in p.read_text().splitlines()) if p.exists() else {}

    def windows(self, n):
        for _ in range(n):
            self.tmux("new-window", "-d", "-t", "main:")
        time.sleep(0.3)

    def kill_windows(self):
        out = self.tmux("list-windows", "-t", "main", "-F", "#{window_index}").split()
        for idx in out[1:]:
            self.tmux("kill-window", "-t", f"main:{idx}")

    def fake_old_good(self, panes=10):
        """A known-good save captured long before this server started."""
        name = "tmux_resurrect_20200101T000000.txt"
        rows = "".join(f"pane\tmain\t{i}\t1\t:*\t0\tt\t:/\t1\tzsh\t:\n" for i in range(panes))
        (self.dir / name).write_text(rows + "state\tmain\t\n")
        old = time.time() - 86400
        os.utime(self.dir / name, (old, old))
        (self.dir / "last").symlink_to(name)
        (self.dir / ".guard-state").write_text(f"good_file={name}\nstreak=0\nfirst_reject=\n")
        return self.dir / name

    # HIGH-1
    def test_final_on_a_replacement_server_that_never_restored_keeps_last(self):
        good = self.fake_old_good(panes=1)
        start = time.time()
        r = self.run_save("final")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.dir / "last").resolve(), good.resolve())
        self.assertGreaterEqual(good.stat().st_mtime, int(start))
        self.assertEqual(len(self.snapshots()), 2, "the evidence snapshot is kept")
        log = (self.dir / "save.log").read_text()
        self.assertRegex(log, r"saved 1 panes in [0-9.]+s \(kept last on tmux_resurrect_20200101T000000\.txt "
                              r"\(replacement server never restored\); evidence tmux_resurrect_\d+T\d+\.txt\)")
        self.assertEqual(self.hook_runs(), 0, "sidecar hooks must not run for a held save")
        # Once this server has restored, final hands `last` over as before.
        self.tmux("set", "-g", "@resurrect_restored", str(int(time.time())))
        time.sleep(1.1)
        r = self.run_save("final")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual((self.dir / "last").resolve(), good.resolve())

    def test_final_mid_restore_keeps_last(self):
        self.assertEqual(self.run_save().returncode, 0)
        good = (self.dir / "last").resolve()
        self.windows(2)
        self.tmux("set", "-g", "@resurrect_restoring", str(int(time.time())))
        r = self.run_save("final")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.dir / "last").resolve(), good)
        self.assertIn("(restore in progress)", (self.dir / "save.log").read_text())

    # MED-3
    def test_a_held_save_never_links_last_nor_rewrites_sidecars(self):
        self.tmux("set", "-g", "@resurrect-capture-pane-contents", "on", ";",
                  "set", "-g", "@resurrect-hook-post-save-all",
                  f"echo x >> '{self.hooks}'; date +%s%N > '{self.dir}/assistant-sessions.json'")
        self.windows(5)
        self.assertEqual(self.run_save().returncode, 0)
        good = (self.dir / "last").resolve()
        sidecars = {n: (self.dir / n).read_bytes() for n in ("assistant-sessions.json", "pane_contents.tar.gz")}
        hooks = self.hook_runs()
        # The `last` symlink must never point at a rejected save, not even
        # for a moment: a watcher records every target it sees.
        seen = self.root / "seen"
        watcher = subprocess.Popen(["bash", "-c", f'while :; do readlink "{self.dir}/last" >> "{seen}"; done'])
        self.addCleanup(watcher.kill)
        self.kill_windows()
        self.auto()
        watcher.kill(); watcher.wait()
        self.assertEqual(set(seen.read_text().split()), {good.name})
        self.assertEqual({n: (self.dir / n).read_bytes() for n in sidecars}, sidecars)
        self.assertEqual(self.hook_runs(), hooks)
        self.assertEqual(len(self.snapshots()), 2, "the rejected snapshot is kept as evidence")
        self.assertEqual(self.guard_state()["streak"], "1")
        # Unchanged since the held save: the fast path, no second count, no evidence pile-up.
        self.auto()
        self.assertEqual((self.guard_state()["streak"], len(self.snapshots())), ("1", 2))
        self.assertEqual((self.dir / "last").resolve(), good)

    def test_manual_save_is_refused_while_a_restore_runs(self):
        self.tmux("set", "-g", "@resurrect_restoring", str(int(time.time())))
        r = self.run_save()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.snapshots(), [])
        self.assertEqual(self.hook_runs(), 0)

    # LOW-6
    def test_final_fails_when_the_sidecar_hooks_fail(self):
        self.tmux("set", "-g", "@resurrect-hook-post-save-all", "false")
        self.assertEqual(self.run_save("final").returncode, 6)
        self.assertEqual(self.run_save("auto-full").returncode, 0)   # other modes unchanged

    # LOW-7
    def test_exclusions_take_shell_patterns(self):
        self.tmux("set", "-g", "@resurrect-capture-pane-contents", "on", ";",
                  "set", "-g", "@resurrect-exclude-sessions", "agents tasks ptymcp-*", ";",
                  "new-session", "-d", "-s", "ptymcp-1")
        self.auto()
        layout = (self.dir / "last").resolve().read_text()
        self.assertNotIn("ptymcp", layout)
        import tarfile
        with tarfile.open(self.dir / "pane_contents.tar.gz") as archive:
            self.assertFalse([n for n in archive.getnames() if "ptymcp" in n])
        self.tmux("new-session", "-d", "-s", "ptymcp-2")
        self.auto()
        self.assertEqual(self.full_saves(), 1)

    # LOW-8
    def test_final_fingerprint_is_taken_after_the_lock_wait(self):
        lock = self.dir / ".save.lock"
        lock.mkdir()
        proc = subprocess.Popen(["bash", str(SAVE), "final"], env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.8)
        self.tmux("new-window", "-d", "-t", "main:")
        self.settle()
        lock.rmdir()
        self.assertEqual(proc.wait(timeout=30), 0)
        self.auto()
        self.assertEqual(self.full_saves(), 1, "the recorded fingerprint missed the new window")

    # LOW-9
    def test_future_epochs_count_as_unset_or_stale(self):
        future = str(int(time.time()) + 3600)
        self.tmux("set", "-g", "@kill_guard_shutdown", future, ";", "set", "-g", "@resurrect_restoring", future)
        self.auto()
        self.assertEqual(self.full_saves(), 1)
        fp = self.dir / ".save-fingerprint"
        when, rest = fp.read_text().split("\n", 1)
        fp.write_text(f"{int(future) + 7200}\n{rest}")
        self.auto()
        self.assertEqual(self.full_saves(), 2)

    # LOW-10
    def test_a_failed_snapshot_move_is_not_linked(self):
        self.assertEqual(self.run_save().returncode, 0)
        good = (self.dir / "last").resolve()
        shim = self.root / "shim"
        shim.mkdir()
        (shim / "mv").write_text('#!/bin/bash\ncase "$*" in *.txt.tmp.*tmux_resurrect_*.txt) exit 1 ;; esac\n'
                                 'exec /bin/mv "$@"\n')
        (shim / "mv").chmod(0o755)
        self.tmux("new-window", "-d", "-t", "main:")
        r = self.run_save("final", PATH=f"{shim}:{self.env['PATH']}")
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertEqual((self.dir / "last").resolve(), good)
        self.assertIn("could not write", (self.dir / "save.log").read_text())

    # LOW-11
    def test_an_all_excluded_server_settles_into_the_fast_path(self):
        self.tmux("set", "-g", "@resurrect-exclude-sessions", "main")
        self.auto()
        log = (self.dir / "save.log").read_text()
        for _ in range(3):
            self.auto()
        self.assertEqual((self.dir / "save.log").read_text(), log)


class ReplacementServerTests(Cadence):
    """Post-integration: a server started after the good save was captured and
    never restored holds EVERY differing save, not only a shrink."""

    fake_old_good = ReviewRound1Tests.fake_old_good
    windows = ReviewRound1Tests.windows

    def test_growth_to_half_is_held_until_a_restore(self):
        good = self.fake_old_good(panes=4)
        self.windows(2)                     # 3 panes: not a shrink of 4
        self.auto()
        self.assertEqual((self.dir / "last").resolve(), good.resolve())
        self.assertEqual(self.hook_runs(), 0)
        self.assertIn("(replacement server never restored", (self.dir / "save.log").read_text())
        # A restore on this server lifts it: the next save takes `last`.
        self.tmux("set", "-g", "@resurrect_restored", str(int(time.time())))
        self.auto("auto-full")
        self.assertNotEqual((self.dir / "last").resolve(), good.resolve())

    def test_manual_save_takes_a_non_shrink_but_not_a_shrink(self):
        good = self.fake_old_good(panes=10)
        self.assertEqual(self.run_save().returncode, 0)       # 1 pane of 10: held
        self.assertEqual((self.dir / "last").resolve(), good.resolve())
        self.windows(5)                                        # 6 of 10: not a shrink
        self.assertEqual(self.run_save().returncode, 0)
        self.assertNotEqual((self.dir / "last").resolve(), good.resolve())
        self.assertIn("manual save on a replacement server", (self.dir / "guard.log").read_text())

    def test_the_notice_comes_once_per_server(self):
        self.fake_old_good(panes=10)
        self.auto()
        self.windows(1)
        self.auto()
        log = (self.dir / "save.log").read_text()
        self.assertEqual(log.count("notice: replacement server never restored"), 1, log)
        self.assertTrue(self.tmux("show", "-gqv", "@resurrect_replacement_noticed").strip().isdigit())

    def test_the_notice_reaches_an_attached_client(self):
        import pty
        self.fake_old_good(panes=10)
        master, slave = pty.openpty()
        env = {k: v for k, v in self.env.items() if k not in ("TMUX", "TMUX_PANE")}
        env["TERM"] = "xterm-256color"
        client = subprocess.Popen([TMUX_BIN, "-L", self.socket, "attach", "-t", "main"], env=env,
                                  stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        def reap():
            client.kill(); client.wait()
            os.close(master)
        self.addCleanup(reap)
        for _ in range(100):
            if self.tmux("list-clients", "-F", "x").strip():
                break
            time.sleep(0.05)
        self.auto()
        self.assertIn("told 1 client(s)", (self.dir / "save.log").read_text())


class SaveSoonTests(Cadence):
    def setUp(self):
        super().setUp()
        self.tmpdir = self.root / "tmp"
        self.tmpdir.mkdir()
        self.soon_env = dict(self.env, TMPDIR=str(self.tmpdir), RESURRECT_SAVE_SOON_DELAY="1",
                             RESURRECT_SAVE_SCRIPT=str(SAVE))

    def soon(self, *args, **extra):
        return subprocess.Popen(["bash", str(SOON), *args], env=dict(self.soon_env, **extra),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def locks(self):
        return [p for p in self.tmpdir.iterdir() if p.name.startswith("resurrect-save-soon.")]

    def wait_idle(self, procs, timeout=40):
        for p in procs:
            out, err = p.communicate(timeout=timeout)
            self.assertEqual((p.returncode, out, err), (0, b"", b""))

    def test_a_burst_coalesces_into_one_save_and_leaves_no_lock(self):
        self.tmux("new-window", "-d", "-t", "main:")
        procs = [self.soon() for _ in range(8)]
        self.wait_idle(procs)
        self.assertEqual(self.full_saves(), 1)
        self.assertEqual(self.locks(), [])

    def test_an_event_during_the_debounce_is_folded_into_the_owners_save(self):
        owner = self.soon()
        time.sleep(0.3)
        self.tmux("new-window", "-d", "-t", "main:")
        late = self.soon()
        t = time.monotonic()
        self.wait_idle([late])        # a non-owner exits at once
        self.assertLess(time.monotonic() - t, 0.9)
        self.wait_idle([owner])
        self.assertEqual(self.full_saves(), 1)
        self.assertIn("window\tmain\t1\t", (self.dir / "last").resolve().read_text())
        self.assertEqual(self.locks(), [])

    def test_an_event_during_the_save_earns_one_trailing_round(self):
        self.auto()
        # A save that is slow on purpose: the event lands while it runs.
        self.tmux("set", "-g", "@resurrect-hook-post-save-all", "sleep 1.5")
        owner = self.soon(RESURRECT_SAVE_SOON_DELAY="0.2")
        time.sleep(0.2)
        self.tmux("new-window", "-d", "-t", "main:")
        time.sleep(0.2)
        self.tmux("new-window", "-d", "-t", "main:")      # after the owner's save read the layout
        time.sleep(0.4)
        self.wait_idle([self.soon()])
        self.wait_idle([owner])
        self.assertIn("window\tmain\t2\t", (self.dir / "last").resolve().read_text())
        self.assertEqual(self.locks(), [])

    def test_full_asks_for_a_full_save_even_with_an_unchanged_layout(self):
        self.auto()
        self.wait_idle([self.soon()])
        self.assertEqual(self.full_saves(), 1)
        self.wait_idle([self.soon(), self.soon("full")])
        self.assertEqual(self.full_saves(), 2)

    def attach(self):
        """A real client on a pty, like WezTerm's (or a popup's nested one)."""
        import pty
        master, slave = pty.openpty()
        env = {k: v for k, v in self.env.items() if k != "TMUX"}
        env["TERM"] = "xterm-256color"
        proc = subprocess.Popen([TMUX_BIN, "-L", self.socket, "attach", "-t", "main"], env=env,
                                stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        def reap():
            proc.kill(); proc.wait()
            os.close(master)
        self.addCleanup(reap)
        for _ in range(100):
            if self.tmux("list-clients", "-F", "x").strip():
                return proc
            time.sleep(0.05)
        self.fail("client never attached")

    def test_full_only_when_no_client_remains(self):
        # MED-5: closing a popup detaches a nested client while the terminal
        # stays attached; only a detach that leaves nobody is a quit.
        self.auto()
        client = self.attach()
        self.wait_idle([self.soon("full")])
        self.assertEqual(self.full_saves(), 1)
        self.tmux("detach-client", "-s", "main")
        client.wait(timeout=10)
        self.wait_idle([self.soon("full")])
        self.assertEqual(self.full_saves(), 2)

    def test_a_dead_owner_lock_is_broken(self):
        stale = self.tmpdir / ("resurrect-save-soon.%d.%s" % (os.getuid(), self.env["TMUX"].split(",")[0].replace("/", "_")))
        stale.mkdir()
        dead = subprocess.Popen(["true"]); dead.wait()
        (stale / "pid").write_text(str(dead.pid))
        self.wait_idle([self.soon()])
        self.assertEqual(self.full_saves(), 1)
        self.assertEqual(self.locks(), [])

    def lock_path(self):
        return self.tmpdir / ("resurrect-save-soon.%d.%s" % (os.getuid(), self.env["TMUX"].split(",")[0].replace("/", "_")))

    def test_a_fresh_pidless_lock_is_honoured(self):
        # An owner between its mkdir and its pid file: hand over, save nothing.
        lock = self.lock_path()
        lock.mkdir()
        self.wait_idle([self.soon()], timeout=5)
        self.assertEqual(self.full_saves(), 0)
        self.assertTrue((lock / "pending").exists())

    def test_an_old_lock_is_broken_whatever_its_pid_says(self):
        # Round 2: no pid file, or a pid reused by a LIVE unrelated process
        # (this test runner): honoured forever, event saves stopped silently.
        for pid in (None, str(os.getpid())):
            lock = self.lock_path()
            lock.mkdir()
            if pid:
                (lock / "pid").write_text(pid)
            old = time.time() - 200          # > (1+1)*4+60 s at DELAY=1
            os.utime(lock, (old, old))
            n = self.full_saves()
            self.tmux("new-window", "-d", "-t", "main:")   # something to save
            self.settle()
            self.wait_idle([self.soon()])
            self.assertEqual(self.full_saves(), n + 1, pid)
            self.assertEqual(self.locks(), [], pid)

    def test_non_default_server_without_opt_in_is_refused(self):
        env = {k: v for k, v in self.soon_env.items() if k != "RESURRECT_SAVE_ALLOW_SOCKET"}
        p = subprocess.Popen(["bash", str(SOON)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.wait_idle([p], timeout=5)
        self.assertEqual(self.locks(), [])
        self.assertFalse((self.dir / "save.log").exists())

    def test_kill_guard_shutdown_skips_without_waiting(self):
        self.tmux("set", "-g", "@kill_guard_shutdown", str(int(time.time())))
        t = time.monotonic()
        self.wait_idle([self.soon(), self.soon("full")], timeout=5)
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertEqual(self.locks(), [])
        self.assertFalse((self.dir / "save.log").exists())

    def test_shutdown_set_during_the_debounce_still_skips(self):
        owner = self.soon(RESURRECT_SAVE_SOON_DELAY="1.5")
        time.sleep(0.3)
        self.tmux("set", "-g", "@kill_guard_shutdown", str(int(time.time())))
        self.wait_idle([owner])
        self.assertFalse((self.dir / "save.log").exists())

    def test_a_stale_or_garbage_shutdown_flag_is_ignored(self):
        for n, value in enumerate((str(int(time.time()) - 121), "1", "garbage", str(int(time.time()) + 3600)),
                                  start=1):
            self.tmux("set", "-g", "@kill_guard_shutdown", value)
            self.wait_idle([self.soon("full")])
            self.assertEqual(self.full_saves(), n, value)
        self.assertEqual(self.locks(), [])


if __name__ == "__main__":
    unittest.main()
