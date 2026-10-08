"""stash.sh: the orphans file lists only conversations that really lost their window.

Live, 2026-10-07: stash-orphans.tsv held aa3db67d (x3), bcf8d86e (x2) and
4664ef27 (x3) although @83/@84/@85 carried those very session ids again, so
`stash.sh list` reported three conversations as lost that were sitting in the
stash. Nothing removed an orphan once its session was placed again, and
restore-state appended without the lock and without deduplicating.

The invariant on the other side: an orphan row is the LAST pointer to its
conversation, so it goes only when some window carries that session id right
now. 2e410003 and 88d5cc09 are real orphans and must survive everything here.

Same harness as test_stash_restore_match.py (fake tmux, isolated HOME/TMPDIR,
@resurrect-dir in a temp dir); nothing real is touched.
"""
import json
import os
import shutil
import subprocess
import threading
import unittest

from test_stash_restore_match import AUTO, BASH_DIR, SEP, STASH, FakeEnv, row, win

AA = "aa3db67d-9fc9-49f8-a307-47f8732edc0e"
BC = "bcf8d86e-34b7-4f79-b230-703d9d3b2398"
CA = "4664ef27-ce8f-4a13-aa4c-b38ede24d3c7"
HO = "98c7f75b-2ae1-4f0b-92f4-746787ac10f4"
BRUIN = "2e410003-1961-49cb-9f98-f50708d6687f"     # real orphan
OLD = "88d5cc09-014a-4c6a-b400-332c6ad8e353"       # real orphan, 8-field row

U = "/Users/u"
PROJ = "/Users/u/code/projects/cua-notch"

# The live file, field for field (home dir shortened).
LIVE_ORPHANS = (
    row("main", "3", "2.1.245", "1", "", "", OLD, U)
    + row("stash", "1", "zsh", "1", "schedule", "~/BruinLearn contact", BRUIN, U)
    + row("stash", "1", "zsh", "1", "main", "~/Backyard Stage Spec", AA, U)
    + row("stash", "2", "zsh", "1", "main", "cua-notch/Cua Notch Esc Handling", BC, PROJ)
    + row("stash", "3", "zsh", "1", "main", "~/Canvas Syllabus", CA, U)
    + row("stash", "1", "zsh", "1", "main", "~/Backyard Stage Spec", AA, U)
    + row("stash", "3", "zsh", "1", "main", "~/Canvas Syllabus", CA, U)
    + row("stash", "1", "zsh", "1", "main", "~/Backyard Stage Spec", AA, U)
    + row("stash", "3", "zsh", "1", "main", "~/Canvas Syllabus", CA, U)
    + row("stash", "2", "zsh", "1", "main", "cua-notch/Cua Notch Esc Handling", BC, PROJ))


def suspended(index, sid, cwd, label, ts):
    w = win("stash", index, "zsh", cwd,
            {**AUTO, "@stash_origin": "main", "@stash_label": label, "@stash_session": sid,
             "@stash_cwd": cwd, "@stash_pane_idx": "1", "@stash_ts": ts})
    w["panes"][0]["index"] = 1
    return w


def live_server():
    """Today's stash: the three 'orphans' are back on @83/@84/@85."""
    return {
        "@9": win("main", 1, "zsh", U, AUTO),
        "@83": suspended(1, AA, U, "~/Backyard Stage Spec", "1790796914"),
        "@84": suspended(2, BC, PROJ, "cua-notch/Cua Notch Esc Handling", "1790811939"),
        "@85": suspended(3, CA, U, "~/Canvas Syllabus", "1790811956"),
        "@86": win("stash", 4, "claude", U, {**AUTO, "@stash_origin": "main",
                                               "@stash_label": "~/Bruincast Media Download",
                                               "@stash_ts": "1790812058"}),
        "@77": suspended(5, HO, U, "~/House Maintenance", "1791296890"),
    }


class Orphans(unittest.TestCase):
    def setUp(self):
        self.env = FakeEnv()
        self.addCleanup(self.env.cleanup)

    def orphan_rows(self):
        e = self.env
        if not e.orphans.exists():
            return []
        return [l.split(SEP) for l in e.orphans.read_text().splitlines() if l]

    def orphan_sids(self):
        return [r[6] for r in self.orphan_rows()]

    def test_publish_drops_sessions_that_are_on_a_window_again(self):
        """The live file through save_state: only the two real orphans remain."""
        e = self.env
        e.orphans.write_text(LIVE_ORPHANS)
        e.set_server(live_server())
        r = e.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.orphan_sids(), [OLD, BRUIN], e.orphans.read_text())
        # Rows survive byte for byte, including the 8-field one.
        self.assertEqual(e.orphans.read_text(), LIVE_ORPHANS.splitlines(True)[0]
                         + LIVE_ORPHANS.splitlines(True)[1])
        log = e.log()
        for sid in (AA, BC, CA):
            self.assertEqual(log.count("suspended session %s is on a window again" % sid[:8]), 1, log)
        self.assertNotIn(BRUIN[:8], log)
        self.assertNotIn(OLD[:8], log)

        listing = e.run("list").stdout
        self.assertIn("claude --resume " + BRUIN, listing)
        self.assertIn("claude --resume " + OLD, listing)
        for sid in (AA, BC, CA):
            self.assertNotIn("claude --resume " + sid, listing.split("lost their window")[-1])

        # Idempotent: nothing left to drop, nothing rewritten.
        before = e.orphans.stat().st_mtime_ns
        r = e.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(e.orphans.stat().st_mtime_ns, before)

    def test_a_sid_only_in_the_sidecar_is_not_live(self):
        """Carried forward to a window with no options yet is NOT on a window."""
        e = self.env
        e.orphans.write_text(row("stash", "1", "zsh", "1", "schedule", "~/BruinLearn contact", BRUIN, U))
        # Restarted, restore-state not run: the window exists without options,
        # so save_state carries the sidecar row forward — the sid is in the
        # sidecar but no window holds it.
        e.sidecar.write_text(row("stash", "1", "zsh", "1", "schedule", "~/BruinLearn contact", BRUIN, U, "1790000000", U))
        e.set_server({"@5": win("stash", 1, "zsh", U, AUTO), "@9": win("main", 1, "zsh", U, AUTO)})
        r = e.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(BRUIN, e.sidecar.read_text())
        self.assertIn("carried forward suspended session 2e410003", e.log())
        self.assertEqual(self.orphan_sids(), [BRUIN])

    def test_restore_that_places_an_orphan_removes_it(self):
        e = self.env
        e.orphans.write_text(
            row("main", "3", "2.1.245", "1", "", "", OLD, U)
            + row("stash", "1", "zsh", "1", "main", "~/Backyard Stage Spec", AA, U))
        e.sidecar.write_text(row("stash", "1", "zsh", "1", "main", "~/Backyard Stage Spec", AA, U, "1790796914", U))
        w = win("stash", 1, "zsh", U, AUTO); w["panes"][0]["index"] = 1
        e.set_server({"@83": w, "@9": win("main", 1, "zsh", "/Users/u/x", AUTO)})
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(e.opts("@83").get("@stash_session"), AA)
        self.assertEqual(self.orphan_sids(), [OLD])

    def test_restore_appends_each_unplaceable_session_once(self):
        e = self.env
        e.orphans.write_text(row("stash", "1", "zsh", "1", "schedule", "~/BruinLearn contact", BRUIN, U))
        # Two rows for sessions with no window at all, one of them already an
        # orphan, and a duplicate sidecar row for the other.
        e.sidecar.write_text(
            row("stash", "7", "zsh", "1", "schedule", "~/BruinLearn contact", BRUIN, U, "1790000000", U)
            + row("stash", "8", "zsh", "1", "main", "Gone", OLD, U, "1790000001", U)
            + row("stash", "8", "zsh", "1", "main", "Gone", OLD, U, "1790000001", U))
        e.set_server({"@9": win("main", 1, "zsh", U, AUTO), "@5": win("stash", 1, "zsh", "/elsewhere", AUTO)})
        for _ in range(2):
            r = e.run("restore-state")
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.orphan_sids(), [BRUIN, OLD], e.orphans.read_text())

    def test_restore_never_drops_an_orphan_no_window_carries(self):
        e = self.env
        e.orphans.write_text(LIVE_ORPHANS)
        # Nothing placed: no sidecar, and no window carries any of the ids.
        e.set_server({"@9": win("main", 1, "zsh", U, AUTO), "@5": win("stash", 1, "zsh", U, AUTO)})
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        # Repeats fold to their first row; every distinct session is kept.
        self.assertEqual(self.orphan_sids(), [OLD, BRUIN, AA, BC, CA], e.orphans.read_text())

    def test_restore_waits_out_a_held_lock_instead_of_appending_unlocked(self):
        """It used to give up after ~10 s and append without the lock."""
        e = self.env
        e.sidecar.write_text(row("stash", "8", "zsh", "1", "main", "Gone", OLD, U, "1790000001", U))
        e.set_server({"@9": win("main", 1, "zsh", U, AUTO)})
        lock = e.tmp / ("tmux-stash-save.%d.lock" % os.getuid())
        lock.mkdir()
        (lock / "pid").write_text(str(os.getpid()))          # a live holder
        release = threading.Timer(12.0, shutil.rmtree, args=(lock,))
        release.start()
        self.addCleanup(release.cancel)
        r = e.run("restore-state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.orphan_sids(), [OLD])
        self.assertEqual(list(e.rdir.glob("stash-orphans.tsv.pending.*")), [])
        self.assertNotIn("could not take the lock", e.log())

    def test_with_no_lock_possible_rows_go_to_a_pending_file_then_merge(self):
        e = self.env
        e.orphans.write_text(row("stash", "1", "zsh", "1", "schedule", "~/BruinLearn contact", BRUIN, U))
        e.sidecar.write_text(row("stash", "8", "zsh", "1", "main", "Gone", OLD, U, "1790000001", U))
        e.set_server({"@9": win("main", 1, "zsh", U, AUTO)})
        r = e.run("restore-state", tmpdir=str(e.dir / "no-such-dir"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("could not create the lock", e.log())
        self.assertEqual(self.orphan_sids(), [BRUIN])                # main file untouched
        pend = list(e.rdir.glob("stash-orphans.tsv.pending.*"))
        self.assertEqual(len(pend), 1, pend)
        self.assertIn(OLD, pend[0].read_text())
        self.assertIn("claude --resume " + OLD, e.run("list").stdout)
        r = e.run("publish")                                          # the next locked save
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.orphan_sids(), [BRUIN, OLD])
        self.assertEqual(list(e.rdir.glob("stash-orphans.tsv.pending.*")), [])


class ResumedByHand(unittest.TestCase):
    """An orphan typed back with `claude --resume <sid>` is running, not lost.

    No window carries it in @stash_session, so the reconcile cannot see it;
    only ~/.claude/sessions/<pid>.json says it is back. `list` shows it as
    running again, but its row STAYS: that file vanishes when claude exits,
    and the row is then once more the only pointer to the conversation."""

    def setUp(self):
        self.env = FakeEnv()
        self.addCleanup(self.env.cleanup)
        self.sessions = self.env.home / ".claude/sessions"
        self.sessions.mkdir(parents=True)
        self.env.orphans.write_text(
            row("main", "3", "2.1.245", "1", "", "", OLD, U)
            + row("stash", "1", "zsh", "1", "schedule", "~/BruinLearn contact", BRUIN, U))

    def child(self):
        p = subprocess.Popen(["sleep", "120"])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        return p.pid

    def dead_pid(self):
        p = subprocess.Popen(["true"]); p.wait()
        return p.pid

    @staticmethod
    def proc_start(pid, tz="UTC0"):
        """As claude records it: `ps -o lstart=`, in UTC on this machine."""
        return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                              text=True, env={**os.environ, **({"TZ": tz} if tz else {})}).stdout.strip()

    def session_file(self, pid, sid, tmux=None, proc_start=None):
        rec = {"pid": pid, "sessionId": sid, "cwd": U, "kind": "interactive", "status": "idle"}
        if proc_start is not None:
            rec["procStart"] = proc_start
        if tmux is not None:
            rec["tmux"] = tmux
        (self.sessions / ("%d.json" % pid)).write_text(json.dumps(rec, separators=(",", ":")))

    def server_with_pane(self, pid):
        w = win("main", 1, "2.1.291", U, AUTO); w["panes"][0]["pid"] = pid
        self.env.set_server({"@9": w, "@5": win("stash", 1, "zsh", U, AUTO)})

    def sections(self):
        out = self.env.run("list").stdout
        lost, _, back = out.partition("running again:")
        return out, lost, back

    def test_list_shows_it_running_and_the_dead_one_still_lost(self):
        alive = self.child()
        self.session_file(alive, BRUIN, tmux="main:@9.%9", proc_start=self.proc_start(alive))
        self.session_file(self.dead_pid(), OLD, tmux="main:@9.%9")    # a crash's leftover
        self.server_with_pane(alive)
        out, lost, back = self.sections()
        self.assertIn("lost their window", lost, out)
        self.assertIn("claude --resume " + OLD, lost, out)
        self.assertNotIn(BRUIN, lost, out)
        self.assertIn("~/BruinLearn contact", back, out)
        self.assertIn("running again in main:1  (session %s)" % BRUIN, back, out)

    def test_reconcile_keeps_the_row_until_a_window_carries_it(self):
        alive = self.child()
        self.session_file(alive, BRUIN, tmux="main:@9.%9", proc_start=self.proc_start(alive))
        self.server_with_pane(alive)
        before = self.env.orphans.read_bytes()
        r = self.env.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.env.orphans.read_bytes(), before)
        self.assertNotIn("on a window again", self.env.log())
        # Parked again, with the id on the window: now it goes.
        self.env.set_server({"@9": win("main", 1, "zsh", U, AUTO),
                             "@83": suspended(1, BRUIN, U, "~/BruinLearn contact", "1791300000")})
        r = self.env.run("publish")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([l.split(SEP)[6] for l in self.env.orphans.read_text().splitlines()], [OLD])

    def test_a_reused_pid_is_not_the_resumed_session(self):
        """A stale sessions file whose pid now belongs to someone else."""
        alive = self.child()
        self.session_file(alive, BRUIN, tmux="main:@9.%9", proc_start="Mon Jan  1 00:00:00 2001")
        self.server_with_pane(alive)
        out, lost, back = self.sections()
        self.assertIn("claude --resume " + BRUIN, lost, out)
        self.assertNotIn("running again", out)

    def test_local_time_proc_start_also_matches(self):
        alive = self.child()
        self.session_file(alive, BRUIN, proc_start=self.proc_start(alive, tz=None))   # the script's own zone
        self.server_with_pane(alive)
        out, lost, back = self.sections()
        self.assertNotIn(BRUIN, lost, out)
        self.assertIn(BRUIN, back, out)

    def test_another_servers_window_id_is_not_trusted(self):
        """No procStart (older claude): alive is enough. The "tmux" field names
        @9, but the pid is not under @9's pane here — another server's @9."""
        alive = self.child()
        self.session_file(alive, BRUIN, tmux="main:@9.%9")
        self.server_with_pane(self.dead_pid())
        out, lost, back = self.sections()
        self.assertIn("running again in pid %d  (session %s)" % (alive, BRUIN), back, out)

    def test_all_running_prints_no_lost_header(self):
        a, b = self.child(), self.child()
        self.session_file(a, BRUIN)
        self.session_file(b, OLD)
        self.server_with_pane(a)
        out = self.env.run("list").stdout
        self.assertNotIn("lost their window", out)
        self.assertNotIn("claude --resume", out)
        self.assertIn("running again", out)
        self.assertIn(str(self.env.orphans), out)       # the file is still there, and said so


# Drives orphans_reconcile itself, extracted from the script, with `mv` or
# `awk` wrapped so a row is appended at an exact point of the rewrite — what
# an unlocked writer, or one whose lock was broken as stale, could do.
RACE = r'''
set -uo pipefail
SEP=$'\x1f'
LOGFILE="$2"
eval "$(awk '/^log\(\) /' "$1")"
eval "$(awk '/^orphans_reconcile\(\) \{/,/^}$/' "$1")"
OF="$3"
mv() {
    if [ "$INJECT" = mv ] && [ "${!#}" = "$OF" ]; then printf '%s\n' "$ROW" >> "$OF"; fi
    command mv "$@"
}
awk() {
    command awk "$@"; local rc=$?
    if [ "$INJECT" = read ] && [[ " $* " == *" out="* ]]; then printf '%s\n' "$ROW" >> "$OF"; fi
    return $rc
}
orphans_reconcile "$OF" "$4"
'''


class RewriteRace(unittest.TestCase):
    def setUp(self):
        self.env = FakeEnv()
        self.addCleanup(self.env.cleanup)
        self.env.orphans.write_text(
            row("main", "3", "2.1.245", "1", "", "", OLD, U)
            + row("stash", "1", "zsh", "1", "main", "~/Backyard Stage Spec", AA, U))
        self.late = row("stash", "9", "zsh", "1", "main", "Late", BRUIN, U).rstrip("\n")

    def reconcile(self, inject):
        e = self.env
        r = subprocess.run(["bash", "-c", RACE, "race", str(STASH), str(e.logfile), str(e.orphans), AA],
                           env={"PATH": f"{BASH_DIR}:/usr/bin:/bin", "INJECT": inject, "ROW": self.late},
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [l.split(SEP)[6] for l in e.orphans.read_text().splitlines() if l]

    def test_an_append_between_the_check_and_the_rename_is_carried_over(self):
        self.assertEqual(self.reconcile("mv"), [OLD, BRUIN])
        self.assertIn("carried over", self.env.log())

    def test_an_append_during_the_read_abandons_the_rewrite(self):
        self.assertEqual(self.reconcile("read"), [OLD, AA, BRUIN])
        # ...and the next one, unraced, finishes the job.
        self.assertEqual(self.reconcile("none"), [OLD, BRUIN])

    def test_a_failed_read_keeps_the_file_and_every_pending_row(self):
        # awk exits 2 on an I/O error (an unreadable pending file here; a full
        # disk truncating $tmp in real life). That must abandon the rewrite:
        # no partial file renamed in, no pending file deleted unmerged.
        before = self.env.orphans.read_bytes()
        bad = self.env.orphans.with_name(self.env.orphans.name + ".pending.4242")
        bad.write_text(self.late + "\n")
        bad.chmod(0)
        self.addCleanup(bad.chmod, 0o600)
        self.reconcile("none")
        self.assertEqual(self.env.orphans.read_bytes(), before)
        self.assertTrue(bad.exists(), "an unmerged pending file was deleted")


if __name__ == "__main__":
    unittest.main()
