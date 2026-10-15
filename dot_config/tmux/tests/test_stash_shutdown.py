"""stash.sh: the sidecar mirror never judges parked state lost on the wrong server.

The incident (2026-10-15 09:40): Mack's tmux server was killed, and
window-unlinked publishes run by a server that came up in between — same
config, so same hook, never restored — merged stash-state.tsv against a world
with no `stash` session, moved all four suspended sessions to
stash-orphans.tsv and DELETED the sidecar. The restore 30 s later found no file
and every parked tab came back "no recorded origin".

What these tests established about tmux itself (3.7c), and pin down:
  - kill-server runs NO window-unlinked hook and SIGTERMs run-shell jobs in
    flight, so a dying server never answers a publish with half its windows;
  - the damage comes from a DIFFERENT server answering: a fresh one, or the
    replacement a long-running job reaches after its own server died.

REAL tmux, on a private socket (`tmux -L <name> -f <scratch conf>`, through a
wrapper on PATH that hardcodes both, so the script's bare `tmux` can reach
nothing else), with the production hook pointed at the script under test,
HOME/TMPDIR in a temp dir and @resurrect-dir there too. Every server is killed
and its socket removed in tearDown. Set STASH_TEST_SOCKET_PREFIX to choose the
socket names.
"""
import itertools
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from test_stash_restore_match import BASH_DIR, SEP, STASH

REAL_TMUX = shutil.which("tmux") or "/opt/homebrew/bin/tmux"
PREFIX = os.environ.get("STASH_TEST_SOCKET_PREFIX", "stashshut")
_seq = itertools.count()

SIDS = ["98c7f75b-2ae1-4f0b-92f4-746787ac10f4", "98f73e7a-1111-4f0b-92f4-746787ac10f4",
        "664db357-2222-4f0b-92f4-746787ac10f4", "57f2f7cd-3333-4f0b-92f4-746787ac10f4"]
GONE = "aaaaaaaa-4444-4f0b-92f4-746787ac10f4"


class Server:
    """A private tmux server. allow=True opts its socket in (STASH_ALLOW_SOCKET)
    to the sidecar writes stash.sh otherwise keeps for the default server."""

    def __init__(self, allow=True, sock=None):
        self.dir = Path(tempfile.mkdtemp(prefix="stash-shutdown-")).resolve()
        self.home = self.dir / "home"; self.tmp = self.dir / "tmp"; self.bin = self.dir / "bin"
        self.rdir = self.dir / "resurrect"; self.work = self.dir / "work"
        for d in (self.home, self.tmp, self.bin, self.rdir, self.work):
            d.mkdir()
        (self.home / "Library/Logs").mkdir(parents=True)
        self.sidecar = self.rdir / "stash-state.tsv"
        self.orphans = self.rdir / "stash-orphans.tsv"
        self.logfile = self.home / "Library/Logs/tmux-stash.log"
        self.hookdone = self.dir / "hook.done"
        self.calls = self.dir / "tmux.calls"
        self.socket = "%s-%d-%d" % (PREFIX, os.getpid(), next(_seq))
        # Honours TMUX_TMPDIR, so a run can keep even its private sockets out
        # of the directory the real server lives in.
        self.tmux_tmpdir = os.environ.get("TMUX_TMPDIR") or "/tmp"
        # As tmux reports #{socket_path}: through realpath (/tmp -> /private/tmp).
        self.socket_path = Path(os.path.realpath(self.tmux_tmpdir)) / ("tmux-%d" % os.getuid()) / self.socket
        # Or an explicit socket path (-S), for a server whose socket is NAMED
        # like the real one but lives somewhere private.
        sel = '-L "%s"' % self.socket
        if sock is not None:
            self.socket_path = Path(os.path.realpath(sock.parent)) / sock.name
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)
            assert str(self.socket_path) not in ("/private/tmp/tmux-%d/default" % os.getuid(),
                                                 "/tmp/tmux-%d/default" % os.getuid())
            sel = '-S "%s"' % self.socket_path
        conf = self.dir / "tmux.conf"
        # The production hook, verbatim but for the script path — plus a
        # marker so the test knows when the backgrounded publish has finished.
        conf.write_text(
            'set -g @resurrect-dir "%s"\n'
            "set-hook -g 'window-unlinked[1]' 'run-shell -b \"bash %s publish; echo x >> %s\"'\n"
            % (self.rdir, STASH, self.hookdone))
        w = self.bin / "tmux"
        w.write_text('#!/bin/bash\nprintf "%%s\\n" "$*" >> "%s"\nexec "%s" %s -f "%s" "$@"\n'
                     % (self.calls, REAL_TMUX, sel, conf))
        w.chmod(0o755)
        self.env = {"HOME": str(self.home), "TMPDIR": str(self.tmp) + "/",
                    "PATH": f"{self.bin}:{BASH_DIR}:/usr/bin:/bin",
                    "STASH_SELF": "/usr/bin/true", "AGENT_STATE_NOTICE": "", "SHELL": "/bin/sh",
                    # A tmux client in a non-UTF-8 locale prints every control
                    # character of a -F format as "_" — the \x1f separators
                    # included. stash.sh now fixes its own locale (see
                    # Locale); this keeps the test's own tmux calls exact.
                    "LANG": "en_US.UTF-8", "TMUX_TMPDIR": self.tmux_tmpdir}
        if allow:
            # Exported before the server starts, so its hook jobs inherit it.
            self.env["STASH_ALLOW_SOCKET"] = str(self.socket_path)
        if "TZ" in os.environ:
            self.env["TZ"] = os.environ["TZ"]

    # --- the server --------------------------------------------------------
    def tmux(self, *args, check=True):
        r = subprocess.run([str(self.bin / "tmux"), *args], env=self.env,
                           capture_output=True, text=True, timeout=20)
        if check and r.returncode != 0:
            raise AssertionError("tmux %s: %s" % (args, r.stderr))
        return r.stdout.strip()

    def start(self):
        self.tmux("new-session", "-d", "-s", "main", "-x", "120", "-y", "30",
                  "-c", str(self.work), "sleep 1000")
        got = self.tmux("display", "-p", "#{socket_path}")
        assert got == str(self.socket_path), (got, str(self.socket_path))

    def hand_sidecar(self, text):
        """A sidecar this server wrote (by mtime): stamped a second after its start."""
        self.sidecar.write_text(text)
        t = int(self.tmux("display", "-p", "#{start_time}")) + 1
        os.utime(self.sidecar, (t, t))

    def ok(self, *args):
        return subprocess.run([str(self.bin / "tmux"), *args], env=self.env,
                              capture_output=True, timeout=20).returncode == 0

    def alive(self):
        return self.ok("list-sessions")

    def kill(self):
        subprocess.run([str(self.bin / "tmux"), "kill-server"], env=self.env, capture_output=True, timeout=20)
        for _ in range(100):
            if not self.alive():
                break
            time.sleep(0.05)

    def cleanup(self):
        self.kill()
        self.socket_path.unlink(missing_ok=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def window(self, sess, cwd, opts=None):
        cwd.mkdir(parents=True, exist_ok=True)
        if self.ok("has-session", "-t", "=" + sess):
            wid = self.tmux("new-window", "-d", "-t", sess + ":", "-c", str(cwd), "-P", "-F", "#{window_id}",
                            "sleep 1000")
        else:
            wid = self.tmux("new-session", "-d", "-s", sess, "-c", str(cwd), "-P", "-F", "#{window_id}",
                            "sleep 1000")
        for k, v in (opts or {}).items():
            self.tmux("set-option", "-w", "-t", wid, k, v)
        return wid

    def parked(self, i, sid, label=None):
        cwd = self.work / ("p%d" % i)
        o = {"@stash_origin": "main", "@stash_label": label or "Tab %d" % i, "@stash_ts": str(1790000000 + i)}
        if sid:
            o.update({"@stash_session": sid, "@stash_cwd": str(cwd), "@stash_pane_idx": "0"})
        return self.window("stash", cwd, o)

    # --- the script ----------------------------------------------------------
    def run(self, *args, extra=None):
        return subprocess.run(["bash", str(STASH), *args], env={**self.env, **(extra or {})},
                              capture_output=True, text=True, timeout=60)

    def popen(self, *args):
        return subprocess.Popen(["bash", str(STASH), *args], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def hooks_done(self):
        return len(self.hookdone.read_text().splitlines()) if self.hookdone.exists() else 0

    def wait_hooks(self, n, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if self.hooks_done() >= n:
                return
            time.sleep(0.05)
        raise AssertionError("hook publish did not finish (%d of %d)" % (self.hooks_done(), n))

    def sidecar_sids(self):
        if not self.sidecar.exists():
            return []
        return [l.split(SEP)[6] for l in self.sidecar.read_text().splitlines() if l and l.split(SEP)[6:7] != [""]]

    def orphan_sids(self):
        if not self.orphans.exists():
            return []
        return [l.split(SEP)[6] for l in self.orphans.read_text().splitlines() if l]

    def log(self):
        return self.logfile.read_text() if self.logfile.exists() else ""

    def snapshots_taken(self):
        """How many `list-windows -a` save_state made (its one snapshot call)."""
        if not self.calls.exists():
            return 0
        return sum(1 for l in self.calls.read_text().splitlines()
                   if l.startswith("list-windows -a -F") and "#{@stash_session}" in l)


class Shutdown(unittest.TestCase):
    def setUp(self):
        self.s = Server()
        self.addCleanup(self.s.cleanup)

    def four_suspended(self):
        s = self.s
        s.start()
        for i, sid in enumerate(SIDS, 1):
            s.parked(i, sid)
        r = s.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(s.sidecar_sids()), sorted(SIDS))

    def test_the_incident_a_replacement_server_neither_orphans_nor_deletes(self):
        """FAILS on the pre-fix stash.sh: four sids orphaned, sidecar deleted."""
        s = self.s
        self.four_suspended()
        before = s.sidecar.read_bytes()

        # The kill. tmux runs no window-unlinked hook while dying.
        s.kill()
        self.assertEqual(s.hooks_done(), 0)
        self.assertEqual(s.sidecar.read_bytes(), before)

        # The server that came up in between: same config, never restored.
        # A window closing on it fires the hook, as the pty-mcp shells did.
        s.start()
        w = s.window("agents", s.work / "a")
        s.tmux("kill-window", "-t", w)
        s.wait_hooks(1)
        self.assertEqual(s.sidecar.read_bytes(), before, s.log())
        self.assertEqual(s.orphan_sids(), [], s.log())
        self.assertNotIn("moved to stash-orphans.tsv", s.log())
        self.assertIn("has not adopted stash-state.tsv", s.log())

        # ...and when restore does run, every row is still there to place.
        s.kill()
        s.start()
        for i, sid in enumerate(SIDS, 1):
            s.window("stash", s.work / ("p%d" % i))       # resurrect: windows, no options
        r = s.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = s.tmux("list-windows", "-t", "=stash", "-F", "#{@stash_session}").splitlines()
        self.assertEqual(sorted(got), sorted(SIDS), s.log())
        self.assertNotIn("no recorded origin", s.log())

    def test_a_process_whose_server_died_does_not_judge_the_replacement(self):
        """$TMUX names the server that started us; another pid answering is not it."""
        s = self.s
        s.start()
        s.parked(1, SIDS[0])
        s.hand_sidecar(SEP.join(["stash", "7", "zsh", "0", "main", "Gone", GONE,
                                       str(s.work / "gone"), "1790000009", str(s.work / "gone")]) + "\n")
        before = s.sidecar.read_bytes()
        r = s.run("publish", extra={"TMUX": "%s,999999,0" % s.socket_path})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(s.sidecar.read_bytes(), before)
        self.assertEqual(s.orphan_sids(), [])
        self.assertIn("answered by pid", s.log())

    def test_a_server_killed_during_the_settle_leaves_the_disk_alone(self):
        s = self.s
        s.start()
        s.parked(1, SIDS[0])
        s.hand_sidecar(SEP.join(["stash", "7", "zsh", "0", "main", "Gone", GONE,
                                       str(s.work / "gone"), "1790000009", str(s.work / "gone")]) + "\n")
        before = s.sidecar.read_bytes()
        p = s.popen("publish")
        time.sleep(0.6)                 # inside SAVE_SETTLE (1.5 s)
        s.kill()
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(s.sidecar.read_bytes(), before, s.log())
        self.assertEqual(s.orphan_sids(), [], s.log())

    def test_a_real_loss_on_a_steady_server_is_still_orphaned_after_the_settle(self):
        s = self.s
        s.start()
        s.parked(1, SIDS[0])
        s.hand_sidecar(SEP.join(["stash", "7", "zsh", "0", "main", "Gone", GONE,
                                       str(s.work / "gone"), "1790000009", str(s.work / "gone")]) + "\n")
        t = time.time()
        r = s.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreaterEqual(time.time() - t, 1.4)
        self.assertEqual(s.orphan_sids(), [GONE], s.log())
        self.assertEqual(s.sidecar_sids(), [SIDS[0]])
        self.assertIn("suspended session aaaaaaaa has no window any more", s.log())
        self.assertEqual(s.snapshots_taken(), 2)

    def test_the_common_publish_takes_one_snapshot_and_no_settle(self):
        s = self.s
        s.start()
        s.parked(1, SIDS[0])
        s.parked(2, None)
        r = s.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        s.calls.unlink()
        r = s.run("publish")            # nothing changed
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(s.snapshots_taken(), 1)
        # count()'s has-session is the only one: hold_exists in save_state is
        # paid on the "would lose something" path alone.
        calls = s.calls.read_text().splitlines()
        self.assertEqual(len([c for c in calls if c.startswith("has-session -t =stash")]), 1, calls)

    def test_an_unpark_style_drop_is_not_held_or_settled(self):
        """A parked row without a session id whose window left: dropped at once."""
        s = self.s
        s.start()
        s.parked(1, SIDS[0])
        w2 = s.parked(2, None, label="Leaving")
        r = s.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Leaving", s.sidecar.read_text())
        s.calls.unlink()
        t = time.time()
        s.tmux("move-window", "-s", w2, "-t", "main:")      # the hook publishes
        s.wait_hooks(1)
        self.assertLess(time.time() - t, 1.4)
        self.assertNotIn("Leaving", s.sidecar.read_text())
        self.assertEqual(s.snapshots_taken(), 1)


STASH_OPTS = ("@stash_origin", "@stash_label", "@stash_ts", "@stash_session", "@stash_cwd", "@stash_pane_idx")
NEW = "bbbbbbbb-5555-4f0b-92f4-746787ac10f4"


class Adoption(unittest.TestCase):
    """Only a server that has ADOPTED the sidecar may judge a row lost.

    Review round 1: the "is anything parked here?" stand-in kept genuinely
    dead ids on a healthy server (and one could later be stamped on the wrong
    tab), and stopped protecting an unrestored server the moment anything was
    parked on it."""

    def setUp(self):
        self.s = Server()
        self.addCleanup(self.s.cleanup)

    def unpark(self, w, hooks):
        """Home to main with its options gone — what an unpark leaves."""
        s = self.s
        s.tmux("move-window", "-s", w, "-t", "main:")
        s.wait_hooks(hooks)
        for k in STASH_OPTS:
            s.tmux("set-option", "-uw", "-t", w, k)

    def test_a_resumed_session_with_a_new_id_is_orphaned_not_kept(self):
        """Item 1: unpark resumed S1 as S1' — S1 is dead, and goes to the orphans
        file. A busy tab parked later in the same directory lands in S1's old
        slot; neither the sidecar nor a restore may put S1 on it."""
        s = self.s
        s.start()
        w = s.parked(1, SIDS[0])                  # stash:0, in p1
        self.assertEqual(s.run("publish").returncode, 0)
        self.unpark(w, 1)
        # resume_agent: the agent is back in that window — registered under
        # its NEW id — so it clears the options and mirrors.
        pid, pane = s.tmux("display", "-p", "-t", w, "#{pane_pid} #{pane_id}").split()
        sessions = s.home / ".claude/sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / ("%s.json" % pid)).write_text(
            '{"pid":%s,"sessionId":"%s","cwd":"%s","status":"idle","tmux":"main:%s.%s"}'
            % (pid, NEW, s.work / "p1", w, pane))
        r = s.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(s.orphan_sids(), [SIDS[0]], s.log())
        self.assertNotIn(SIDS[0], s.sidecar.read_text() if s.sidecar.exists() else "")

        busy = s.window("stash", s.work / "p1", {"@stash_origin": "main", "@stash_label": "Busy",
                                                  "@stash_ts": "1790000100"})
        self.assertEqual(s.tmux("display", "-p", "-t", busy, "#{window_index}"), "0")
        self.assertEqual(s.run("publish").returncode, 0)
        rows = [l.split(SEP) for l in s.sidecar.read_text().splitlines() if l]
        self.assertEqual([(r[0], r[1], r[5], r[6]) for r in rows], [("stash", "0", "Busy", "")])

    def test_closing_the_only_suspended_window_by_hand_orphans_it(self):
        s = self.s
        s.start()
        w = s.parked(1, SIDS[0])
        self.assertEqual(s.run("publish").returncode, 0)
        s.tmux("kill-window", "-t", w)
        s.wait_hooks(1)
        self.assertEqual(s.orphan_sids(), [SIDS[0]], s.log())
        self.assertFalse(s.sidecar.exists() and SIDS[0] in s.sidecar.read_text())

    def test_a_park_before_restore_keeps_every_old_row(self):
        """Item 2: the first park on an unrestored server used to switch the
        protection off — every old row then failed window_for_row and was
        orphaned after the settle."""
        s = self.s
        s.start()
        for i, sid in enumerate(SIDS, 1):
            s.parked(i, sid)
        self.assertEqual(s.run("publish").returncode, 0)
        before = s.sidecar.read_text()
        s.kill()

        s.start()                                   # restarted, NOT restored
        s.parked(9, NEW, label="Parked before restoring")   # lands at stash:0, in p9
        r = s.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(s.sidecar_sids()), sorted(SIDS + [NEW]), s.log())
        for line in before.splitlines():
            self.assertIn(line, s.sidecar.read_text())
        self.assertEqual(s.orphan_sids(), [], s.log())
        self.assertNotIn("moved to", s.log())
        self.assertEqual(s.log().count("has not adopted"), 1, s.log())
        r = s.run("publish")                         # once per server, not per save
        self.assertEqual(s.log().count("has not adopted"), 1, s.log())
        listing = s.run("list").stdout
        self.assertIn("awaiting a restore", listing)
        for sid in SIDS:
            self.assertIn("claude --resume " + sid, listing)
        self.assertNotIn("claude --resume " + NEW, listing)   # it is on a window

        # The restore (from the snapshot taken before the restart) puts all
        # four back; the newer park is in another directory, so it is no
        # rival for stash:0.
        s.kill()
        s.start()
        for i in range(1, 5):
            s.window("stash", s.work / ("p%d" % i))
        r = s.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = s.tmux("list-windows", "-t", "=stash", "-F", "#{@stash_session}").splitlines()
        self.assertEqual(sorted(got), sorted(SIDS), s.log())
        # The tab parked before restoring is not in that snapshot: restore-state
        # itself files it as lost, by its own rules.
        self.assertEqual(s.orphan_sids(), [NEW], s.log())

        # Adopted by restore-state: a loss from here on is a loss.
        w = s.tmux("list-windows", "-t", "=stash", "-F", "#{window_id}").splitlines()[3]
        s.tmux("kill-window", "-t", w)
        s.wait_hooks(1)
        self.assertEqual(s.orphan_sids(), [NEW, SIDS[3]], s.log())

    def test_a_pre_adoption_sidecar_from_an_older_server_is_not_adopted_by_writing_it(self):
        """No .owner (written before adoption existed) and older than the
        server: not adopted. Its own additive write must not make it so."""
        s = self.s
        s.start()
        s.hand_sidecar(SEP.join(["stash", "7", "zsh", "0", "main", "Gone", GONE,
                                       str(s.work / "gone"), "1790000009", str(s.work / "gone")]) + "\n")
        old = int(s.tmux("display", "-p", "#{start_time}")) - 3600
        os.utime(s.sidecar, (old, old))
        s.parked(1, SIDS[0])
        for _ in range(2):
            r = s.run("publish")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(sorted(s.sidecar_sids()), sorted([GONE, SIDS[0]]), s.log())
            self.assertEqual(s.orphan_sids(), [])
        self.assertEqual((s.rdir / "stash-state.tsv.owner").read_text(), "none\n")

    def test_restore_does_not_stamp_an_old_id_on_a_newer_tab_in_its_slot(self):
        """The restore-time half of item 1, for a file no server judged: a kept
        S1 row and a busy tab parked later at the same slot, same directory."""
        s = self.s
        p1 = s.work / "p1"
        s.sidecar.write_text(
            SEP.join(["stash", "0", "zsh", "0", "main", "Old", SIDS[0], str(p1), "1790000001", str(p1)]) + "\n"
            + SEP.join(["stash", "0", "zsh", "", "work", "Busy", "", "", "1790000100", str(p1)]) + "\n")
        s.start()
        w = s.window("stash", p1)                    # resurrect: the busy tab, no options
        r = s.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(s.tmux("display", "-p", "-t", w, "#{@stash_session}"), "")
        self.assertEqual(s.tmux("display", "-p", "-t", w, "#{@stash_label}"), "Busy", s.log())
        self.assertEqual(s.orphan_sids(), [SIDS[0]], s.log())

    def test_unparking_the_last_plain_tab_logs_nothing_about_restores(self):
        """Item 3: a healthy, adopted server whose last parked tab (no session
        id) came home is not "a server that was never restored"."""
        s = self.s
        s.start()
        w = s.parked(1, None, label="Last one")
        self.assertEqual(s.run("publish").returncode, 0)
        self.unpark(w, 1)
        self.assertEqual(s.run("publish").returncode, 0)
        self.assertFalse(s.sidecar.exists() and "Last one" in s.sidecar.read_text())
        log = s.log()
        for phrase in ("never restored", "no window on this server", "has not adopted"):
            self.assertNotIn(phrase, log)

    def test_an_unwritable_orphans_file_keeps_the_row_in_the_sidecar(self):
        """Item 4: a failed append is not "moved to"."""
        s = self.s
        s.start()
        s.parked(1, SIDS[0])
        s.hand_sidecar(SEP.join(["stash", "7", "zsh", "0", "main", "Gone", GONE,
                                       str(s.work / "gone"), "1790000009", str(s.work / "gone")]) + "\n")
        s.orphans.write_text("")
        s.orphans.chmod(0o444)
        self.addCleanup(s.orphans.chmod, 0o644)
        r = s.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(GONE, s.sidecar_sids(), s.log())
        self.assertEqual(s.orphans.read_text(), "")
        self.assertIn("could not be written — kept it in stash-state.tsv", s.log())
        self.assertNotIn("moved to", s.log())


def gone_row(s, ts="1790000009"):
    return SEP.join(["stash", "7", "zsh", "0", "main", "Gone", GONE,
                     str(s.work / "gone"), ts, str(s.work / "gone")]) + "\n"


def agent_file(s, w, sid, cwd):
    """A live claude in window <w>, as ~/.claude/sessions/<pid>.json records it."""
    pid, pane, sess = s.tmux("display", "-p", "-t", w, "#{pane_pid} #{pane_id} #{session_name}").split()
    d = s.home / ".claude/sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / ("%s.json" % pid)).write_text('{"pid":%s,"sessionId":"%s","cwd":"%s","status":"idle","tmux":"%s:%s.%s"}'
                                       % (pid, sid, cwd, sess, w, pane))


class FinalRound(unittest.TestCase):
    def setUp(self):
        self.s = Server()
        self.addCleanup(self.s.cleanup)

    def test_m1_a_stale_plain_row_does_not_cost_a_later_park_its_label(self):
        """Unadopted: park X in projA, unpark it, park Y in projA, restart,
        restore — Y is placed. X's stale row used to share Y's key d:projA and
        make resolve_parked_rows refuse both."""
        s = self.s
        s.start()
        s.sidecar.write_text(gone_row(s))            # an earlier server's file...
        old = int(s.tmux("display", "-p", "#{start_time}")) - 3600
        os.utime(s.sidecar, (old, old))              # ...so this one has not adopted it
        proj = s.work / "projA"
        x = s.window("stash", proj, {"@stash_origin": "main", "@stash_label": "X", "@stash_ts": "1790000100"})
        self.assertEqual(s.run("publish").returncode, 0)
        s.tmux("move-window", "-s", x, "-t", "main:")    # unpark X
        s.wait_hooks(1)
        for k in STASH_OPTS:
            s.tmux("set-option", "-uw", "-t", x, k)
        self.assertEqual(s.run("publish").returncode, 0)
        self.assertIn("\x1fX\x1f", s.sidecar.read_text())  # kept: unadopted, nothing holds its key
        s.window("stash", proj, {"@stash_origin": "work", "@stash_label": "Y", "@stash_ts": "1790000200"})
        self.assertEqual(s.run("publish").returncode, 0)
        text = s.sidecar.read_text()
        self.assertNotIn("\x1fX\x1f", text, s.log())
        self.assertIn("\x1fY\x1f", text)
        self.assertIn(GONE, text)                         # the earlier server's id is untouched
        self.assertEqual(s.orphan_sids(), [])

        s.kill()
        s.start()
        y = s.window("stash", proj)                       # resurrect: Y, no options
        r = s.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(s.tmux("display", "-p", "-t", y, "#{@stash_label}"), "Y", s.log())
        self.assertEqual(s.tmux("display", "-p", "-t", y, "#{@stash_origin}"), "work")

    def restore_with_agent(self, agent_sid):
        s = self.s
        p1 = s.work / "p1"
        s.sidecar.write_text(SEP.join(["stash", "0", "zsh", "0", "main", "Suspended", SIDS[0],
                                       str(p1), "1790000001", str(p1)]) + "\n")
        s.start()
        w = s.window("stash", p1)
        agent_file(s, w, agent_sid, p1)               # assistant-restore got there first
        r = s.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        return w

    def test_m2_the_rows_own_session_running_in_its_window_is_applied(self):
        s = self.s
        w = self.restore_with_agent(SIDS[0])
        self.assertEqual(s.tmux("display", "-p", "-t", w, "#{@stash_session}"), SIDS[0], s.log())
        self.assertEqual(s.tmux("display", "-p", "-t", w, "#{@stash_label}"), "Suspended")
        self.assertEqual(s.orphan_sids(), [])

    def test_m2_another_session_running_in_its_window_is_refused(self):
        s = self.s
        w = self.restore_with_agent(NEW)
        self.assertEqual(s.tmux("display", "-p", "-t", w, "#{@stash_session}"), "", s.log())
        self.assertEqual(s.orphan_sids(), [SIDS[0]], s.log())

    def test_low1_a_sidecar_from_the_servers_own_first_second_is_not_adopted(self):
        s = self.s
        s.start()
        s.parked(1, SIDS[0])
        s.sidecar.write_text(gone_row(s))
        t = int(s.tmux("display", "-p", "#{start_time}"))
        os.utime(s.sidecar, (t, t))                   # same second: maybe the predecessor's
        r = s.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(GONE, s.sidecar_sids(), s.log())
        self.assertEqual(s.orphan_sids(), [])


class NotTheDefaultServer(unittest.TestCase):
    """LOW2: a spare `tmux -L agents` loaded the real tmux.conf — the publish
    hook and the real @resurrect-dir — and wrote Mack's sidecar from its own
    windows. Only the default server (or the one socket a test opts in) may
    write the sidecar or the orphans file."""

    def setUp(self):
        self.s = Server(allow=False)
        self.addCleanup(self.s.cleanup)

    def test_a_foreign_server_never_writes_the_sidecar_or_the_orphans_file(self):
        s = self.s
        p1 = s.work / "p1"
        text = (gone_row(s)
                + SEP.join(["stash", "0", "zsh", "0", "main", "Suspended", SIDS[1], str(p1), "1790000001", str(p1)]) + "\n"
                + SEP.join(["stash", "3", "zsh", "", "main", "Plain", "", "", "1790000002", str(s.work / "pp")]) + "\n")
        s.sidecar.write_text(text)
        before = s.sidecar.read_bytes()
        s.start()
        w1 = s.parked(1, SIDS[0])
        other = s.window("agents", s.work / "a")
        s.tmux("kill-window", "-t", other)                 # the hook publishes
        s.wait_hooks(1)
        for args in (("publish",), ("restore-state",), ("kill-many", w1)):
            r = s.run(*args)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(s.sidecar.read_bytes(), before)
        self.assertFalse(s.orphans.exists())
        self.assertEqual(list(s.rdir.iterdir()), [s.sidecar])
        self.assertFalse(s.ok("has-session", "-t", "=stash"))   # kill-many still killed it
        self.assertNotIn("stash-state", s.log())
        self.assertNotIn("orphans", s.log())

    def test_a_private_socket_named_default_is_not_the_default_server(self):
        """Every agent's private server under its own TMUX_TMPDIR sits at
        <dir>/tmux-$UID/default. Named like the real one, it is not it."""
        base = Path(tempfile.mkdtemp(prefix="sd-", dir=os.environ.get("TMUX_TMPDIR") or None))
        self.addCleanup(shutil.rmtree, base, True)
        s = Server(allow=False, sock=base / ("tmux-%d" % os.getuid()) / "default")
        self.addCleanup(s.cleanup)
        s.sidecar.write_text(gone_row(s))
        before = s.sidecar.read_bytes()
        s.start()
        self.assertTrue(str(s.socket_path).endswith("/tmux-%d/default" % os.getuid()))
        s.parked(1, SIDS[0])
        other = s.window("agents", s.work / "a")
        s.tmux("kill-window", "-t", other)                 # the hook publishes
        s.wait_hooks(1)
        for args in (("publish",), ("restore-state",)):
            r = s.run(*args)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(s.sidecar.read_bytes(), before)
        self.assertFalse(s.orphans.exists())
        self.assertEqual(list(s.rdir.iterdir()), [s.sidecar])

    def test_the_opt_in_names_one_exact_socket(self):
        """A STASH_ALLOW_SOCKET for some other path admits nothing."""
        s = self.s
        s.sidecar.write_text(gone_row(s))
        before = s.sidecar.read_bytes()
        s.start()
        s.parked(1, SIDS[0])
        r = s.run("publish", extra={"STASH_ALLOW_SOCKET": str(s.socket_path) + "x"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(s.sidecar.read_bytes(), before)
        r = s.run("publish", extra={"STASH_ALLOW_SOCKET": str(s.socket_path)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(SIDS[0], s.sidecar_sids())


class KillGuard(unittest.TestCase):
    """@kill_guard_shutdown: the epoch the kill guard stamped just before a kill.

    `set -g @kill_guard_shutdown "$(date +%s)"`; it counts only while numeric
    and under 120 s old, so a guard that died mid-kill cannot leave orphaning
    switched off on a server that lived on."""

    def setUp(self):
        self.s = Server()
        self.addCleanup(self.s.cleanup)

    def setup_two_and_a_plain_one(self, guard=None, scope="-g", s=None):
        s = s or self.s
        s.start()
        s.parked(1, SIDS[0])
        w2 = s.parked(2, SIDS[1])
        w3 = s.parked(3, None, label="Plain parked")
        r = s.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        if guard is not None:
            s.tmux("set-option", scope, "@kill_guard_shutdown", guard)
        return w2, w3

    def assert_real_losses(self, s, w2, w3):
        s.tmux("kill-window", "-t", w2)
        s.wait_hooks(1)
        s.tmux("kill-window", "-t", w3)
        s.wait_hooks(2)
        self.assertEqual(s.sidecar_sids(), [SIDS[0]], s.log())
        self.assertNotIn("Plain parked", s.sidecar.read_text())
        self.assertEqual(s.orphan_sids(), [SIDS[1]], s.log())
        self.assertNotIn("@kill_guard_shutdown is set", s.log())

    def test_with_the_guard_nothing_is_orphaned_or_dropped(self):
        """FAILS on the pre-fix stash.sh: SIDS[1] orphaned, "Plain parked" dropped."""
        s = self.s
        w2, w3 = self.setup_two_and_a_plain_one(guard=str(int(time.time())))
        s.tmux("kill-window", "-t", w2)
        s.tmux("kill-window", "-t", w3)
        s.wait_hooks(2)
        self.assertEqual(sorted(s.sidecar_sids()), sorted(SIDS[:2]), s.log())
        self.assertIn("Plain parked", s.sidecar.read_text())
        self.assertEqual(s.orphan_sids(), [], s.log())
        self.assertIn("@kill_guard_shutdown is set", s.log())

        # The guard's own synchronous publish still mirrors what is live.
        s.parked(4, SIDS[2])
        r = s.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(s.sidecar_sids()), sorted(SIDS[:3]), s.log())
        self.assertIn("Plain parked", s.sidecar.read_text())
        self.assertEqual(s.orphan_sids(), [])

    def test_a_server_option_counts_too(self):
        s = self.s
        w2, _ = self.setup_two_and_a_plain_one(guard=str(int(time.time()) - 60), scope="-s")
        s.tmux("kill-window", "-t", w2)
        s.wait_hooks(1)
        self.assertIn(SIDS[1], s.sidecar_sids())
        self.assertEqual(s.orphan_sids(), [])

    def test_without_the_guard_the_same_kills_are_real_losses(self):
        self.assert_real_losses(self.s, *self.setup_two_and_a_plain_one())

    def test_a_stale_stamp_is_not_a_guard(self):
        """The guard died 5 minutes ago; this server lived on."""
        s = self.s
        self.assert_real_losses(s, *self.setup_two_and_a_plain_one(guard=str(int(time.time()) - 300)))

    def test_garbage_is_not_a_guard(self):
        # "1" is numeric but 1970; the last is far in the future.
        for value in ("1", "yes", "on", "17920x", str(int(time.time()) + 100000)):
            with self.subTest(value=value):
                s = Server()
                try:
                    self.assert_real_losses(s, *self.setup_two_and_a_plain_one(guard=value, s=s))
                finally:
                    s.cleanup()


class Locale(unittest.TestCase):
    """A tmux client outside a UTF-8 locale prints \\x1f in -F output as "_".

    Every record stash.sh reads is split on \\x1f, so a publish from such a
    caller (launchd, `env -i`, the kill guard) used to parse no rows at all.
    The script now puts itself in a UTF-8 locale."""

    def setUp(self):
        self.s = Server()
        self.addCleanup(self.s.cleanup)

    def publish_with(self, **locale):
        s = self.s
        s.start()
        s.parked(1, SIDS[0])
        s.parked(2, None, label="Plain parked")
        env = {k: v for k, v in s.env.items() if k not in ("LANG", "LC_ALL", "LC_CTYPE")}
        env.update(locale)
        r = subprocess.run(["bash", str(STASH), "publish"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(s.sidecar_sids(), [SIDS[0]], s.log())
        rows = [l.split(SEP) for l in s.sidecar.read_text().splitlines() if l]
        self.assertEqual(len(rows), 2, rows)
        self.assertTrue(all(len(r) == 10 for r in rows), rows)
        self.assertIn("Plain parked", s.sidecar.read_text())

    def test_publish_with_no_locale_at_all(self):
        self.publish_with()

    def test_publish_with_lc_all_c(self):
        self.publish_with(LC_ALL="C", LANG="en_US.UTF-8")

    def test_publish_with_lc_ctype_c(self):
        self.publish_with(LC_CTYPE="C")


if __name__ == "__main__":
    unittest.main()
