"""Delegated children (docs/agent-tab-indicator.md, "Delegated children").

abby-delegate marks the tabs it opens with @agent_parent; agent-session-lib.sh's
kids_reconcile derives @agent_child / @agent_kids / @agent_kids_ask from it,
the indicator holds a parent's Stop while children run and finishes a child
as seen, the watcher reconciles every tick and finishes a held parent when its
last child stops, agent-jump.sh's list (and agent-roster.py's copy) queues a
child's question under the parent's label, and the tab formats render a child
plain and an asked-for parent tinted.

Real-tmux tests run a private `tmux -L <unique> -f /dev/null` (killed in
cleanup, TMUX/TMUX_PANE dropped from the child env); watcher and list tests use
test_agent_jump_watcher's fake tmux. Run with unittest or pytest.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

import test_agent_jump_watcher as jw
import test_agent_origin as ao
from test_agent_roster import R

LIB = jw.LIB
TMUX_CONF = ao.SCRIPTS.parent / "tmux.conf.tmpl"
REAL_TMUX = ao.REAL_TMUX
TAB = "\t"


def clean_env(**extra):
    e = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    e.update(extra)
    return e


class Server:
    """A private tmux server whose first session `s` holds window w0."""

    def __init__(self, test):
        self.dir = Path(tempfile.mkdtemp(prefix="agent-kids-"))
        self.name = "agent-kids-" + uuid.uuid4().hex[:12]
        self.tm = [REAL_TMUX, "-L", self.name, "-f", "/dev/null"]
        self.socket = None
        test.addCleanup(self.close)
        r = self.tmux("new-session", "-d", "-s", "s", "-x", "80", "-y", "24", "-P", "-F",
                      "#{window_id}|#{pane_id}|#{socket_path}", "/bin/sleep 600")
        self.w0, self.p0, self.socket = r.split("|", 2)

    def close(self):
        subprocess.run(self.tm + ["kill-server"], capture_output=True, timeout=5)
        if self.socket:
            Path(self.socket).unlink(missing_ok=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def tmux(self, *args, check=True):
        r = subprocess.run(self.tm + list(args), text=True, capture_output=True, env=clean_env(), timeout=10)
        if check and r.returncode != 0:
            raise AssertionError("tmux %s: %s" % (args, r.stderr))
        return r.stdout.rstrip("\n")

    def window(self, session="s"):
        return self.tmux("new-window", "-d", "-t", session + ":", "-P", "-F", "#{window_id}", "/bin/sleep 600")

    def session(self, name, hidden=False):
        self.tmux("new-session", "-d", "-s", name, "/bin/sleep 600")
        if hidden:
            self.tmux("set-option", "-t", name, "@space_hidden", "1")
        return self.tmux("display-message", "-p", "-t", name + ":", "#{window_id}")

    def set(self, win, **opts):
        for k, v in opts.items():
            if v is None:
                self.tmux("set-option", "-uw", "-t", win, k)
            else:
                self.tmux("set-option", "-w", "-t", win, k, v)

    def opt(self, win, name):
        return self.tmux("show-options", "-wqv", "-t", win, name)

    def has(self, win, name):
        out = self.tmux("show-options", "-w", "-t", win)
        return any(line.split(" ", 1)[0] == name for line in out.splitlines())


class KidsReconcileTests(unittest.TestCase):
    """kids_reconcile against a real private server."""

    def setUp(self):
        self.s = Server(self)
        # A logging wrapper in front of the real tmux, so a test can count writes.
        self.bin = self.s.dir / "bin"; self.bin.mkdir()
        self.log = self.s.dir / "tmux.log"
        (self.bin / "tmux").write_text('#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s"\nexec %s -L %s -f /dev/null "$@"\n'
                                       % (self.log, REAL_TMUX, self.s.name))
        (self.bin / "tmux").chmod(0o755)

    def reconcile(self):
        r = subprocess.run(["bash", "-c", 'set -euo pipefail; . "$1"; kids_reconcile', "_", str(LIB)],
                           env=clean_env(PATH="%s:%s" % (self.bin, os.environ["PATH"])),
                           text=True, capture_output=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)

    def writes(self):
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        return [l for l in lines if "set-option" in l]

    def parent(self, state="running"):
        p = self.s.w0
        self.s.set(p, **{"@agent_state": state})
        return p

    def child(self, parent, state="", since=None, detail=None, session="s", started=True, **more):
        c = self.s.window(session)
        opts = {"@agent_parent": parent, "@agent_parent_pane": "%0"}
        opts.update(more)
        if started and state:
            opts["@agent_turn_start"] = "100"      # its first prompt went in
        if state:
            opts["@agent_state"] = state
        if since is not None:
            opts["@agent_since"] = "%d %s" % (since, state)
        if detail is not None:
            opts["@agent_detail"] = detail
        self.s.set(c, **opts)
        return c

    # --- liveness ---

    def test_live_parent_marks_children_and_counts_them(self):
        p = self.parent()
        a = self.child(p, "running")
        b = self.child(p, "idle")                # finished its turn
        c = self.child(p)                        # born, claude not up yet: still a child, and busy
        self.reconcile()
        for w in (a, b, c):
            self.assertEqual(self.s.opt(w, "@agent_child"), p)
        self.assertEqual(self.s.opt(p, "@agent_kids"), "3 2")
        self.assertFalse(self.s.has(p, "@agent_kids_ask"))
        self.assertFalse(self.s.has(p, "@agent_child"))
        # The parent's own state is never written.
        self.assertEqual(self.s.opt(p, "@agent_state"), "running")

    def test_not_live_parents(self):
        p = self.s.w0                                       # no @agent_state: a plain shell
        a = self.child(p, "running")
        ghost = self.child("@9999", "running")              # no such window
        selfish = self.s.window()
        self.s.set(selfish, **{"@agent_parent": selfish, "@agent_state": "running"})
        hidden_p = self.s.session("stash", hidden=True)     # a parked parent
        self.s.set(hidden_p, **{"@agent_state": "idle"})
        parked = self.child(hidden_p, "running")
        self.reconcile()
        for w in (a, ghost, selfish, parked):
            self.assertFalse(self.s.has(w, "@agent_child"), w)
        for w in (p, hidden_p, selfish):
            self.assertFalse(self.s.has(w, "@agent_kids"), w)
            self.assertFalse(self.s.has(w, "@agent_kids_sub"), w)
        # No window at all: an ORPHAN at once, for good. A parent window
        # with no agent (a shell): the orphan grace starts (its first
        # sighting stamped), and only once it has run out is it orphaned.
        # A parked parent and a self-link: left as they are.
        self.assertEqual(self.s.opt(ghost, "@agent_kid_orphan"), "@9999")
        self.assertFalse(self.s.has(a, "@agent_kid_orphan"))
        self.assertEqual(self.s.opt(a, "@agent_parent"), p)
        stamp = self.s.opt(a, "@agent_kid_orphan_at")
        self.assertLessEqual(abs(int(stamp) - time.time()), 5)
        self.reconcile()
        self.assertEqual(self.s.opt(a, "@agent_kid_orphan_at"), stamp)       # kept, not restamped
        self.s.set(a, **{"@agent_kid_orphan_at": str(int(time.time()) - 6)})
        self.reconcile()
        self.assertEqual(self.s.opt(a, "@agent_kid_orphan"), p)
        self.assertFalse(self.s.has(a, "@agent_kid_orphan_at"))
        for w in (a, ghost):
            self.assertFalse(self.s.has(w, "@agent_parent"), w)
            self.assertFalse(self.s.has(w, "@agent_parent_pane"), w)
        self.assertEqual(self.s.opt(parked, "@agent_parent"), hidden_p)
        self.assertEqual(self.s.opt(selfish, "@agent_parent"), selfish)
        self.assertFalse(self.s.has(parked, "@agent_kid_orphan"))
        self.log.unlink()
        self.reconcile()
        self.assertEqual(self.writes(), [])                 # settled: nothing more

    def test_hidden_session_link_does_not_hide_a_visible_parent(self):
        p = self.parent()
        self.s.session("stash", hidden=True)
        self.s.tmux("link-window", "-d", "-s", p, "-t", "stash:")
        a = self.child(p, "running")
        self.reconcile()
        self.assertEqual(self.s.opt(a, "@agent_child"), p)
        self.assertEqual(self.s.opt(p, "@agent_kids"), "1 1")

    def test_parent_stateless_for_a_moment_keeps_its_children(self):
        # A codex session switch (/new): clear_state, then the new state a
        # moment later. A plan in that gap must not orphan the children.
        p = self.parent()
        a = self.child(p, "running")
        self.reconcile()
        self.s.set(p, **{"@agent_state": None})
        self.reconcile()
        self.assertFalse(self.s.has(a, "@agent_child"))               # not live meanwhile
        self.assertTrue(self.s.has(a, "@agent_kid_orphan_at"))
        self.assertEqual(self.s.opt(a, "@agent_parent"), p)
        self.s.set(p, **{"@agent_state": "idle"})
        self.reconcile()
        self.assertEqual(self.s.opt(a, "@agent_child"), p)            # back, linked
        self.assertFalse(self.s.has(a, "@agent_kid_orphan_at"))
        self.assertFalse(self.s.has(a, "@agent_kid_orphan"))
        self.assertEqual(self.s.opt(p, "@agent_kids"), "1 1")

    def test_parked_parent_makes_children_plain_and_killed_one_orphans_them(self):
        p = self.s.window()
        self.s.set(p, **{"@agent_state": "running"})
        a = self.child(p, "running")
        self.reconcile()
        self.assertEqual(self.s.opt(a, "@agent_child"), p)
        # Parked (moved into a hidden session): plain while away, linked again
        # once back.
        self.s.session("stash", hidden=True)
        self.s.tmux("move-window", "-d", "-s", p, "-t", "stash:")
        self.reconcile()
        self.assertFalse(self.s.has(a, "@agent_child"))
        self.assertEqual(self.s.opt(a, "@agent_parent"), p)
        self.s.tmux("move-window", "-d", "-s", p, "-t", "s:")
        self.reconcile()
        self.assertEqual(self.s.opt(a, "@agent_child"), p)
        # Killed: orphaned by window id (no letter known).
        self.s.tmux("kill-window", "-t", p)
        self.reconcile()
        for o in ("@agent_child", "@agent_parent", "@agent_parent_pane"):
            self.assertFalse(self.s.has(a, o), o)
        self.assertEqual(self.s.opt(a, "@agent_kid_orphan"), p)

    def test_parent_exit_orphans_children_and_a_new_agent_adopts_none(self):
        p = self.s.window()
        self.s.set(p, **{"@agent_state": "running", "@agent_key": "q"})
        a = self.child(p, "running", **{"@agent_spawn_id": "s-abc1", "@agent_spawn_task": "Parser"})
        g = self.child(a, "running")                         # a grandchild stays a's
        self.reconcile()
        self.assertEqual(self.s.opt(p, "@agent_kids"), "2 2")
        # The parent's agent exits (its SessionEnd's clear_state, or the GC),
        # and stays gone past the orphan grace.
        self.s.set(p, **{"@agent_state": None})
        self.reconcile()
        self.assertFalse(self.s.has(a, "@agent_kid_orphan"))           # the grace runs
        self.s.set(a, **{"@agent_kid_orphan_at": str(int(time.time()) - 6)})
        self.reconcile()
        self.assertEqual(self.s.opt(a, "@agent_kid_orphan"), "q")    # its letter
        for o in ("@agent_parent", "@agent_parent_pane", "@agent_child", "@agent_kid_since"):
            self.assertFalse(self.s.has(a, o), o)
        self.assertEqual(self.s.opt(a, "@agent_spawn_id"), "s-abc1")  # the identity stays
        for o in ("@agent_kids", "@agent_kids_sub", "@agent_kids_ask"):
            self.assertFalse(self.s.has(p, o), o)
        # a is a root now: its own child is counted under it.
        self.assertEqual(self.s.opt(g, "@agent_child"), a)
        self.assertEqual(self.s.opt(a, "@agent_kids"), "1 1")
        # A new agent in the parent's window adopts nobody.
        self.s.set(p, **{"@agent_state": "running"})
        self.reconcile()
        self.assertFalse(self.s.has(a, "@agent_child"))
        self.assertFalse(self.s.has(p, "@agent_kids"))
        self.assertFalse(self.s.has(p, "@agent_kids_sub"))

    # --- the ask ---

    def test_ask_picks_failed_then_oldest_then_lowest_id(self):
        p = self.parent()
        now = int(time.time())
        n_old = self.child(p, "needs-input", since=now - 500, detail="Bash git push")
        self.child(p, "needs-input", since=now - 100, detail="Edit x")
        self.reconcile()
        self.assertEqual(self.s.opt(p, "@agent_kids_ask"), TAB.join([n_old, "needs-input", "Bash git push"]))
        self.assertEqual(self.s.opt(p, "@agent_kids"), "2 2")     # an asking child is busy
        f_new = self.child(p, "failed", since=now - 10, detail="overloaded: 529")
        self.reconcile()
        self.assertEqual(self.s.opt(p, "@agent_kids_ask"), TAB.join([f_new, "failed", "overloaded: 529"]))
        f_tie = self.child(p, "failed", since=now - 10, detail="tie")
        self.reconcile()
        self.assertEqual(self.s.opt(p, "@agent_kids_ask").split(TAB)[0], f_new)     # lower id wins a tie
        self.assertNotEqual(f_tie, f_new)

    def test_child_that_stops_asking_clears_the_ask(self):
        p = self.parent()
        a = self.child(p, "needs-input", since=int(time.time()), detail="Bash ls")
        self.reconcile()
        self.assertTrue(self.s.opt(p, "@agent_kids_ask").startswith(a + TAB))
        self.s.set(a, **{"@agent_state": "running"})
        self.reconcile()
        self.assertFalse(self.s.has(p, "@agent_kids_ask"))
        self.assertEqual(self.s.opt(p, "@agent_kids"), "1 1")

    # --- busy ---

    def test_busy_is_a_turn_not_over(self):
        p = self.parent()
        busy = [self.child(p, "running"), self.child(p, "needs-input"), self.child(p, "paused"),
                self.child(p),                                   # no state: claude not up
                self.child(p, "idle", started=False),            # up, first prompt not in yet
                self.child(p, "idle", **{"@agent_pending": "5"})]   # a gate Mack is answering
        done = [self.child(p, "failed"), self.child(p, "idle"), self.child(p, "done")]
        self.reconcile()
        self.assertEqual(self.s.opt(p, "@agent_kids"), "%d %d" % (len(busy) + len(done), len(busy)))

    def test_never_started_child_is_busy_only_for_the_grace(self):
        p = self.parent()
        c = self.child(p)                        # abby-delegate's tab; claude not up
        self.reconcile()
        stamp = self.s.opt(c, "@agent_kid_since")
        self.assertRegex(stamp, r"^\d+$")
        self.assertLessEqual(abs(int(stamp) - time.time()), 5)
        self.assertEqual(self.s.opt(p, "@agent_kids"), "1 1")
        self.reconcile()
        self.assertEqual(self.s.opt(c, "@agent_kid_since"), stamp)   # kept, not restamped
        old = str(int(time.time()) - 61)
        self.s.set(c, **{"@agent_kid_since": old})
        self.reconcile()
        self.assertEqual(self.s.opt(p, "@agent_kids"), "1 0")       # claude never came: not busy
        # Up at last, but its first prompt never went in: still not started,
        # on the same clock (A2).
        self.s.set(c, **{"@agent_state": "idle"})
        self.reconcile()
        self.assertEqual(self.s.opt(c, "@agent_kid_since"), old)
        self.assertEqual(self.s.opt(p, "@agent_kids"), "1 0")
        # Its first prompt goes in: started, so the stamp goes.
        self.s.set(c, **{"@agent_state": "running", "@agent_turn_start": str(int(time.time()))})
        self.reconcile()
        self.assertFalse(self.s.has(c, "@agent_kid_since"))
        self.assertEqual(self.s.opt(p, "@agent_kids"), "1 1")
        # A stamp on a tab that lost its mark goes too.
        d = self.s.window()
        self.s.set(d, **{"@agent_kid_since": "5"})
        self.reconcile()
        self.assertFalse(self.s.has(d, "@agent_kid_since"))

    def test_born_child_that_never_starts(self):
        # A2: agent-spawn's targeted birth makes the child idle + @agent_born
        # at once; with no @agent_turn_start it is busy for the grace only.
        p = self.parent()
        born = str(int(time.time()) - 5)        # agent-spawn stamps it in the birth call
        c = self.child(p, "idle", started=False, **{"@agent_born": born, "@agent_kid_since": born})
        self.reconcile()
        stamp = self.s.opt(c, "@agent_kid_since")
        self.assertEqual(stamp, born)                               # kept until it starts
        self.assertEqual(self.s.opt(p, "@agent_kids"), "1 1")       # held: it is starting
        self.reconcile()
        self.assertEqual(self.s.opt(c, "@agent_kid_since"), stamp)   # kept
        self.s.set(c, **{"@agent_kid_since": str(int(time.time()) - 61)})
        self.reconcile()
        self.assertEqual(self.s.opt(p, "@agent_kids"), "1 0")       # never started: not busy
        self.assertEqual(self.s.opt(c, "@agent_child"), p)          # still its child
        out = self.plan("${KIDS_RUN[%s]:-x} ${KIDS_PAUSE[%s]:-x}" % (p, p))
        self.assertEqual(out, "0 0")

    def plan(self, expr):
        """kids_reconcile, then print EXPR (bash) - KIDS_RUN / KIDS_PAUSE."""
        r = subprocess.run(["bash", "-c", '. "$1"; kids_reconcile; eval "echo \"$2\""', "_", str(LIB), expr],
                           env=clean_env(PATH="%s:%s" % (self.bin, os.environ["PATH"])),
                           text=True, capture_output=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_every_live_parent_carries_its_own_subtree(self):
        p = self.parent()
        c = self.child(p, "running")                  # an intermediate: its Stop held for g
        g = self.child(c, "running")
        h = self.child(c, "idle")                     # finished
        self.reconcile()
        self.assertEqual(self.s.opt(p, "@agent_kids"), "3 2")
        self.assertEqual(self.s.opt(p, "@agent_kids_sub"), "3 2")    # a root: the same
        self.assertEqual(self.s.opt(c, "@agent_kids_sub"), "2 1")
        self.assertFalse(self.s.has(c, "@agent_kids"))               # still root-only
        for w in (g, h):
            self.assertFalse(self.s.has(w, "@agent_kids_sub"))
        self.assertEqual(self.plan("${KIDS_RUN[%s]} ${KIDS_RUN[%s]}" % (p, c)), "2 1")
        self.s.set(g, **{"@agent_state": "idle"})
        self.reconcile()
        self.assertEqual(self.s.opt(c, "@agent_kids_sub"), "2 0")

    def test_paused_children_are_busy_but_split_for_the_hold(self):
        p = self.parent()
        self.child(p, "paused"); self.child(p, "paused"); self.child(p, "idle")
        self.reconcile()
        self.assertEqual(self.s.opt(p, "@agent_kids"), "3 2")        # counts as before
        self.assertEqual(self.s.opt(p, "@agent_kids_sub"), "3 2")
        self.assertEqual(self.plan("${KIDS_RUN[%s]} ${KIDS_PAUSE[%s]}" % (p, p)), "0 2")

    def test_kids_any_guard_follows_the_links(self):
        p = self.parent()
        self.assertEqual(self.s.tmux("show-options", "-gqv", "@agent_kids_any"), "")
        a = self.child(p, "running")
        self.reconcile()
        self.assertEqual(self.s.tmux("show-options", "-gqv", "@agent_kids_any"), "1")
        self.log.unlink()
        self.reconcile()
        self.assertEqual(self.writes(), [])                          # read back: no rewrite
        self.s.set(a, **{"@agent_parent": None})
        self.reconcile()
        self.assertEqual(self.s.tmux("show-options", "-gqv", "@agent_kids_any"), "")
        # Gone even when no window carries a kid field any more.
        self.s.tmux("set-option", "-g", "@agent_kids_any", "1")
        self.s.set(a, **{"@agent_child": None}); self.s.set(p, **{"@agent_kids": None, "@agent_kids_sub": None})
        self.reconcile()
        self.assertEqual(self.s.tmux("show-options", "-gqv", "@agent_kids_any"), "")

    def test_grandchildren_belong_to_the_root(self):
        p = self.parent()
        now = int(time.time())
        c = self.child(p, "running")
        g = self.child(c, "needs-input", since=now, detail="Bash make")
        self.reconcile()
        self.assertEqual(self.s.opt(c, "@agent_child"), p)          # the IMMEDIATE parent
        self.assertEqual(self.s.opt(g, "@agent_child"), c)
        self.assertEqual(self.s.opt(p, "@agent_kids"), "2 2")       # over all descendants
        self.assertEqual(self.s.opt(p, "@agent_kids_ask"), TAB.join([g, "needs-input", "Bash make"]))
        self.assertFalse(self.s.has(c, "@agent_kids"))              # only the root carries them
        self.assertFalse(self.s.has(c, "@agent_kids_ask"))

    def test_child_loop_has_no_root(self):
        p = self.parent()
        a = self.child(p, "running")
        b = self.child(a, "running")
        self.s.set(a, **{"@agent_parent": b})                       # a <-> b
        self.reconcile()
        self.assertFalse(self.s.has(p, "@agent_kids"))
        for w in (a, b):
            self.assertFalse(self.s.has(w, "@agent_kids"))

    def test_ask_ranks_unseen_before_seen(self):
        p = self.parent()
        now = int(time.time())
        f = self.child(p, "failed", since=now - 300, detail="boom", **{"@agent_seen_at": str(now - 200)})
        n = self.child(p, "needs-input", since=now - 10, detail="Bash ls")
        self.reconcile()
        self.assertEqual(self.s.opt(p, "@agent_kids_ask").split(TAB)[0], n)    # f was looked at
        self.s.set(f, **{"@agent_seen_at": str(now - 400)})                  # a look before this failure
        self.reconcile()
        self.assertEqual(self.s.opt(p, "@agent_kids_ask").split(TAB)[0], f)    # unseen failed first
        self.s.set(n, **{"@agent_seen_at": str(now - 10)})                   # seen at the same second
        self.reconcile()
        self.assertEqual(self.s.opt(p, "@agent_kids_ask").split(TAB)[0], f)

    def test_parked_child_is_plain_and_not_counted(self):
        p = self.parent()
        a = self.child(p, "running")
        self.s.session("stash", hidden=True)
        parked = self.child(p, "running", session="stash")
        self.reconcile()
        self.assertEqual(self.s.opt(a, "@agent_child"), p)
        self.assertFalse(self.s.has(parked, "@agent_child"))
        self.assertEqual(self.s.opt(p, "@agent_kids"), "1 1")

    def test_parent_in_a_home_window_is_not_live(self):
        p = self.parent()
        self.s.set(p, **{"@space_home": "1"})
        a = self.child(p, "running")
        self.reconcile()
        self.assertFalse(self.s.has(a, "@agent_child"))
        self.assertFalse(self.s.has(p, "@agent_kids"))

    def test_writes_only_what_changed_in_one_call(self):
        p = self.parent()
        self.child(p, "running"); self.child(p, "needs-input", since=1, detail="q")
        self.reconcile()
        w = self.writes()
        self.assertEqual(len(w), 1, w)                  # one command list
        self.log.unlink()
        self.reconcile()
        self.assertEqual(self.writes(), [])             # nothing changed: nothing written
        reads = self.log.read_text().splitlines()
        self.assertEqual(len(reads), 1, reads)          # one list-windows -a

    def test_detail_with_shell_and_tmux_syntax_is_stored_verbatim(self):
        p = self.parent()
        d = "Bash echo $(x) ; #{@agent_state} 'q' \"z\""
        a = self.child(p, "needs-input", since=5, detail=d)
        self.reconcile()
        self.assertEqual(self.s.opt(p, "@agent_kids_ask"), TAB.join([a, "needs-input", d]))


@unittest.skipUnless(REAL_TMUX, "tmux executable required")
class KidsIndicatorTests(ao.OriginHarness):
    """agent-tab-indicator.sh on a parent or a child window (real private server)."""

    def setUp(self):
        super().setUp()
        lib = self.home / ".config/tmux/scripts"
        lib.mkdir(parents=True)
        shutil.copyfile(LIB, lib / "agent-session-lib.sh")
        # The real tmux answers #{window_active_clients} (0: detached), so the
        # hook's one read also carries the kid fields.
        self.env["FAKE_ACTIVE_CLIENTS"] = ""

    def t(self, *args):
        r = subprocess.run(self.tmuxcmd + list(args), text=True, capture_output=True, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.rstrip("\n")

    def wopt(self, win, name):
        return self.t("show-options", "-wqv", "-t", win, name)

    def whas(self, win, name):
        out = self.t("show-options", "-w", "-t", win)
        return any(line.split(" ", 1)[0] == name for line in out.splitlines())

    def other(self):
        return self.t("new-window", "-d", "-t", "origin:", "-P", "-F", "#{window_id}", "/bin/sleep 600")

    def ask(self):
        self.hook("needs-approval", "claude", payload={"session_id": "s1", "cwd": str(self.root),
                                                       "tool_name": "Bash", "tool_input": {"command": "ls"}})

    # --- the hook's window is a CHILD ---

    def make_child(self):
        p = self.other()
        self.t("set-option", "-w", "-t", p, "@agent_state", "idle")
        self.set_opt("@agent_parent", p)
        self.set_opt("@agent_parent_pane", "%1")
        return p

    def test_child_events_reconcile_the_parent_at_once(self):
        p = self.make_child()
        self.prompt("build it", "u1")
        self.assertEqual(self.opt("@agent_child"), p)
        self.assertEqual(self.wopt(p, "@agent_kids"), "1 1")
        self.ask()
        self.assertEqual(self.opt("@agent_state"), "needs-input")
        self.assertEqual(self.wopt(p, "@agent_kids_ask"), TAB.join([self.win, "needs-input", "Bash ls"]))
        self.assertEqual(self.wopt(p, "@agent_kids"), "1 1")        # asking is busy
        # The parent itself never goes yellow: readers surface the ask.
        self.assertEqual(self.wopt(p, "@agent_state"), "idle")

    def test_child_finish_is_seen_never_an_unseen_green(self):
        p = self.make_child()
        self.prompt("build it", "u1")
        self.stop("u1", msg="All done.")
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assertRegex(self.opt("@agent_since"), r"^\d+ idle$")
        self.assertTrue(self.has("@agent_seen_at"))
        self.assertEqual((self.opt("@agent_detail_kind"), self.opt("@agent_detail")), ("done", "All done."))
        self.assertEqual(self.wopt(p, "@agent_kids"), "1 0")         # its turn is over

    def test_child_question_stands_until_a_look(self):
        # The 2026-11-18 abby-delegate run (@3853): a child's AskUserQuestion
        # stays needs-input and surfaced on the parent through its own
        # Notification twin (Mack not looking); only a look discharges it -
        # there, ⌥S's jump to the child (agent-jump.sh -> clear-current).
        # Discharged, it is idle + @agent_pending with seen_at on since's
        # epoch and the question kept, and still BUSY on the parent ("1 1":
        # a gate Mack is answering), its ask no longer surfaced.
        p = self.make_child()
        self.prompt("Delegation test", "u1")
        cwd = str(self.root)
        self.hook("needs-approval", "claude", payload={
            "session_id": "s1", "cwd": cwd, "hook_event_name": "PermissionRequest",
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "Pick a colour.", "header": "Colour",
                                          "options": [{"label": "Red"}, {"label": "Blue"}]}]}})
        self.hook("needs-approval", "claude", payload={
            "session_id": "s1", "cwd": cwd, "hook_event_name": "Notification",
            "notification_type": "permission_prompt",
            "message": "Claude needs your permission to use AskUserQuestion"})
        self.assertEqual(self.opt("@agent_state"), "needs-input")
        self.assertEqual(self.wopt(p, "@agent_kids_ask"), TAB.join([self.win, "needs-input", "Pick a colour."]))
        self.assertEqual(self.wopt(p, "@agent_kids"), "1 1")
        self.clear_current()
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assertEqual(self.opt("@agent_since").split(" ")[0], self.opt("@agent_seen_at"))
        self.assertTrue(self.has("@agent_pending"))
        self.assertEqual(self.opt("@agent_detail"), "Pick a colour.")
        r = subprocess.run(["bash", "-c", '. "$1"; kids_reconcile', "_", str(LIB)], env=self.env,
                           text=True, capture_output=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.wopt(p, "@agent_kids"), "1 1")
        self.assertFalse(self.whas(p, "@agent_kids_ask"))

    def test_plain_child_of_a_dead_parent_finishes_done(self):
        p = self.make_child()
        self.prompt("build it", "u1")
        self.assertEqual(self.opt("@agent_child"), p)
        self.t("kill-window", "-t", p)
        # The watcher's next tick (the same reconcile).
        r = subprocess.run(["bash", "-c", '. "$1"; kids_reconcile', "_", str(LIB)], env=self.env,
                           text=True, capture_output=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.has("@agent_child"))
        self.stop("u1")
        self.assertEqual(self.opt("@agent_state"), "done")

    def test_child_session_end_drops_the_mark(self):
        p = self.make_child()
        self.prompt("build it", "u1")
        self.assertEqual(self.wopt(p, "@agent_kids"), "1 1")
        self.hook("clear", "claude", payload={"session_id": "s1", "reason": "other"})
        for o in ("@agent_parent", "@agent_parent_pane", "@agent_child"):
            self.assertFalse(self.has(o), o)
        self.assertFalse(self.whas(p, "@agent_kids"))

    # --- the hook's window is a PARENT ---

    def make_parent(self, child_state, started=True):
        c = self.other()
        self.t("set-option", "-w", "-t", c, "@agent_parent", self.win)
        if child_state:
            self.t("set-option", "-w", "-t", c, "@agent_state", child_state)
            if started:
                self.t("set-option", "-w", "-t", c, "@agent_turn_start", "100")
        return c

    def test_parent_stop_is_held_while_a_child_runs(self):
        c = self.make_parent("running")
        self.prompt("delegate it", "u1")     # (a parent's own event: no reconcile; the watcher's tick)
        self.set_opt("@agent_kids", "1 1")   # as the watcher's tick wrote it
        self.stop("u1", msg="Spawned two agents.")
        self.assertEqual(self.opt("@agent_state"), "running")
        self.assertEqual(self.opt("@agent_bg_owner"), "user")
        self.assertEqual(self.opt("@agent_kids_hold"), "1")
        self.assertEqual(self.opt("@agent_detail"), "Spawned two agents.")
        self.assertEqual(self.wopt(c, "@agent_state"), "running")

    def test_parent_stop_rereads_a_stale_count(self):
        # "1 0" left by two children's racing reconciles; the child runs.
        self.make_parent("running")
        self.prompt("delegate it", "u1")
        self.set_opt("@agent_kids", "1 0")
        self.stop("u1")
        self.assertEqual(self.opt("@agent_state"), "running")
        self.assertEqual(self.opt("@agent_kids_hold"), "1")
        self.assertEqual(self.opt("@agent_kids"), "1 1")             # the re-read fixed it
        # ...and the other way: "1 1" stale, the child finished.
        self.make_parent_finished_only()

    def make_parent_finished_only(self):
        for w in self.t("list-windows", "-a", "-F", "#{window_id}").split():
            if w != self.win:
                self.t("set-option", "-w", "-t", w, "@agent_state", "idle")
        self.prompt("again", "u2")
        self.assertFalse(self.has("@agent_kids_hold"))                # a prompt drops the hold
        self.set_opt("@agent_kids", "1 1")
        self.stop("u2")
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertFalse(self.has("@agent_kids_hold"))

    def test_parent_stop_right_after_spawning_is_held(self):
        # The child tab exists, its claude is not up (no state), or is up
        # with its first prompt not in yet.
        for state in ("", "idle"):
            with self.subTest(state=state):
                c = self.make_parent(state, started=False)
                self.prompt("spawn", "u-" + state)
                self.set_opt("@agent_kids", "1 1")
                self.stop("u-" + state)
                self.assertEqual(self.opt("@agent_state"), "running")
                self.assertEqual(self.opt("@agent_kids_hold"), "1")
                self.t("kill-window", "-t", c)

    def test_parent_stop_with_only_finished_or_failed_children_finishes(self):
        self.make_parent("idle")
        self.make_parent("failed")
        self.prompt("delegate it", "u1")
        self.set_opt("@agent_kids", "2 0")
        self.stop("u1")
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertFalse(self.has("@agent_bg_owner"))
        self.assertFalse(self.has("@agent_kids_hold"))

    def test_hold_mark_goes_with_failure_and_session_start(self):
        self.make_parent("running")
        for ending in ("failed", "idle"):
            with self.subTest(ending=ending):
                self.prompt("delegate it", "u1")
                self.set_opt("@agent_kids", "1 1")
                self.stop("u1")
                self.assertEqual(self.opt("@agent_kids_hold"), "1")
                if ending == "failed":
                    self.hook("failed", "claude", payload={"session_id": "s1", "prompt_id": "u1",
                                                           "error": "overloaded"})
                else:
                    self.hook("idle", "claude", payload={"session_id": "s1", "cwd": str(self.root),
                                                         "source": "startup"})
                self.assertFalse(self.has("@agent_kids_hold"))

    def test_parent_session_end_orphans_its_children(self):
        c = self.make_parent("running")
        self.set_opt("@agent_state", "idle")
        self.set_opt("@agent_key", "a")
        self.t("set-option", "-w", "-t", c, "@agent_child", self.win)
        self.set_opt("@agent_kids", "1 1")
        self.set_opt("@agent_kids_sub", "1 1")
        self.set_opt("@agent_kids_hold", "1")
        # /clear and --resume keep them (A8): the SessionEnd exits early.
        for reason in ("clear", "resume"):
            self.hook("clear", "claude", payload={"session_id": "s1", "reason": reason})
            self.assertEqual(self.wopt(c, "@agent_parent"), self.win)
            self.assertEqual(self.wopt(c, "@agent_child"), self.win)
        self.hook("clear", "claude", payload={"session_id": "s1", "reason": "other"})
        self.assertFalse(self.has("@agent_state"))
        self.assertFalse(self.has("@agent_kids_hold"))
        self.assertFalse(self.whas(c, "@agent_child"))
        self.assertTrue(self.whas(c, "@agent_kid_orphan_at"))         # the orphan grace runs
        self.t("set-option", "-w", "-t", c, "@agent_kid_orphan_at", str(int(time.time()) - 6))
        r = subprocess.run(["bash", "-c", '. "$1"; kids_reconcile', "_", str(LIB)], env=self.env,
                           text=True, capture_output=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        for o in ("@agent_child", "@agent_parent", "@agent_parent_pane", "@agent_kid_orphan_at"):
            self.assertFalse(self.whas(c, o), o)
        self.assertEqual(self.wopt(c, "@agent_kid_orphan"), "a")
        self.assertFalse(self.has("@agent_kids"))
        self.assertFalse(self.has("@agent_kids_sub"))
        # The next agent in this window is nobody's parent.
        self.prompt("fresh start", "u9")
        self.stop("u9")
        self.assertFalse(self.whas(c, "@agent_child"))
        self.assertEqual(self.opt("@agent_state"), "done")

    # --- turn ends and results (EXTERNAL SUBAGENTS) ---

    def spawned(self, mode="async"):
        """This window as agent-spawn's child of a fresh parent: the registry
        dir, the identity, and a stub agent-spawn that logs its argv."""
        p = self.make_child()
        sid = "s-k3x9ab12"
        self.reg = self.home / ".local/state/agent-spawn" / sid
        self.reg.mkdir(parents=True)
        self.set_opt("@agent_spawn_id", sid)
        self.set_opt("@agent_spawn_mode", mode)
        self.deliver_log = self.root / "deliver.log"
        stub = self.root / "agent-spawn"
        stub.write_text('#!/bin/sh\necho "$*" >> "%s"\n' % self.deliver_log)
        stub.chmod(0o755)
        self.env["AGENT_SPAWN_BIN"] = str(stub)
        return p, sid

    def delivered(self, n, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            got = self.deliver_log.read_text().splitlines() if self.deliver_log.exists() else []
            if len(got) >= n:
                return got
            time.sleep(.1)
        return self.deliver_log.read_text().splitlines() if self.deliver_log.exists() else []

    def turn_end(self):
        e, tid, v = self.opt("@agent_turn_end").split(" ")
        self.assertLessEqual(abs(int(e) - time.time()), 5)
        return tid, v

    def test_reply_md_written_once_per_turn(self):
        p, sid = self.spawned()
        self.prompt("build it", "u1")
        self.stop("u1", msg="Result: parser rewritten.\nDetails follow.\n" + "x" * 5000)
        reply = self.reg / "reply.md"
        body = reply.read_text()
        self.assertTrue(body.startswith("Result: parser rewritten.\nDetails follow.\n"))
        self.assertEqual(len(body), len("Result: parser rewritten.\nDetails follow.\n") + 5000)   # whole
        self.assertEqual(reply.stat().st_mode & 0o777, 0o600)
        self.assertRegex(self.opt("@agent_reply_at"), r"^\d+$")
        self.assertEqual(self.opt("@agent_spawn_result"), "ready")
        self.assertEqual(self.turn_end(), ("u1", "loud"))
        self.assertEqual(self.opt("@agent_state"), "idle")          # a child: seen
        self.assertEqual(self.delivered(1), ["_deliver " + sid])
        # The same turn's Stop again: reply.md stays as it was.
        self.set_opt("@agent_spawn_result", "read")
        self.stop("u1", msg="a stray second Stop")
        self.assertEqual(reply.read_text(), body)
        # The next turn writes its own.
        self.prompt("and the tests", "u2")
        self.stop("u2", msg="Tests added.")
        self.assertEqual(reply.read_text(), "Tests added.")
        self.assertEqual(self.turn_end(), ("u2", "loud"))
        self.assertEqual(self.opt("@agent_spawn_result"), "ready")
        self.assertEqual(len(self.delivered(3)), 3)
        self.assertEqual(sorted(p.name for p in self.reg.iterdir()), ["reply.md"])   # no tmp left

    def test_sync_child_is_ready_but_wakes_nobody(self):
        p, sid = self.spawned(mode="sync")
        self.prompt("build it", "u1")
        self.stop("u1", msg="ok")
        self.assertEqual(self.opt("@agent_spawn_result"), "ready")
        time.sleep(.5)
        self.assertFalse(self.deliver_log.exists())

    def test_quiet_follow_up_still_ends_a_turn(self):
        # The abby-delegate wait-hang repro: a parent's follow-up (an agent
        # message) finishes QUIET, restoring the idle and @agent_since the
        # child had - a since-based wait never saw it end. @agent_turn_end
        # does.
        p, sid = self.spawned()
        self.prompt("build it", "u1")
        self.stop("u1", msg="first")
        since = self.opt("@agent_since")
        time.sleep(1.1)
        text = self.deliver("now the docs", frm="parent")
        self.prompt(text, "u2")
        self.assertEqual(self.opt("@agent_state"), "running")
        self.stop("u2", msg="docs done")
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assertEqual(self.opt("@agent_since"), since)          # restored: the old signal is blind
        self.assertEqual(self.turn_end(), ("u2", "quiet"))
        self.assertGreaterEqual(int(self.opt("@agent_turn_end").split()[0]), int(self.opt("@agent_turn_start")))
        self.assertEqual((self.reg / "reply.md").read_text(), "docs done")
        self.assertEqual(self.opt("@agent_spawn_result"), "ready")

    def test_every_turn_end_is_recorded_for_any_agent(self):
        # Not only children: a plain agent's loud finish, a failure, an
        # interrupt.
        self.prompt("go", "u1")
        self.stop("u1")
        self.assertEqual(self.turn_end(), ("u1", "loud"))
        self.prompt("again", "u2")
        self.hook("failed", "claude", payload={"session_id": "s1", "prompt_id": "u2", "error": "overloaded"})
        self.assertEqual(self.turn_end(), ("u2", "failed"))
        self.assertFalse(self.has("@agent_spawn_result"))          # no child: no result
        self.prompt("and again", "u3", agent="opencode")
        self.hook("interrupt", "opencode", payload={"session_id": "s1", "cwd": str(self.root), "turn_id": "u3"})
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assertEqual(self.turn_end(), ("u3", "loud"))
        # Per session: SessionEnd drops it (ORIGIN_OPTS).
        self.hook("clear", "claude", payload={"session_id": "s1", "reason": "other"})
        self.assertFalse(self.has("@agent_turn_end"))

    def test_child_session_end_drops_its_identity(self):
        p, sid = self.spawned()
        self.prompt("build it", "u1")
        self.stop("u1")
        self.hook("clear", "claude", payload={"session_id": "s1", "reason": "other"})
        for o in ("@agent_spawn_id", "@agent_spawn_mode", "@agent_spawn_result", "@agent_reply_at",
                  "@agent_turn_end", "@agent_parent"):
            self.assertFalse(self.has(o), o)
        self.assertTrue((self.reg / "reply.md").exists())           # the registry keeps it

    def test_intermediate_is_held_while_its_grandchild_runs(self):
        p = self.make_child()                       # this window: a child of p ...
        g = self.make_parent("running")             # ... and the parent of g
        self.prompt("delegate further", "u1")       # (a child's event: reconciles)
        self.assertEqual(self.opt("@agent_kids_sub"), "1 1")
        self.assertFalse(self.has("@agent_kids"))   # not a root
        self.assertEqual(self.wopt(p, "@agent_kids"), "2 2")
        self.stop("u1", msg="Spawned the grandchild.")
        self.assertEqual(self.opt("@agent_state"), "running")
        self.assertEqual(self.opt("@agent_kids_hold"), "1")
        self.assertEqual(self.opt("@agent_detail"), "Spawned the grandchild.")
        self.assertEqual(self.wopt(p, "@agent_kids"), "2 2")         # the root still waits

    def test_parent_held_by_paused_children_only(self):
        self.make_parent("paused")
        self.prompt("delegate it", "u1")
        self.set_opt("@agent_kids", "1 1")
        self.stop("u1")
        self.assertEqual(self.opt("@agent_state"), "running")       # held; the watcher drains it
        self.assertEqual(self.opt("@agent_kids_hold"), "1")


class KidsWatcherTests(unittest.TestCase):
    """agent-tab-watcher.sh ticks, on test_agent_jump_watcher's fake tmux."""

    SID = "11111111-aaaa-bbbb-cccc-000000000002"

    def setUp(self):
        self.f = jw.FakeEnv(windows={}, panes=[])
        # Parent claude on ttys902 (its session says it is at the prompt: a
        # held Stop), child claude on ttys903 (no session file: left as is).
        self.f.procs.write_text("ttys902 4242 claude\nttys903 4343 claude\n")
        sessions = self.f.home / ".claude/sessions"
        sessions.mkdir(parents=True)
        (sessions / "4242.json").write_text(json.dumps(
            {"pid": 4242, "sessionId": self.SID, "cwd": "/Users/x/proj", "status": "shell"}, separators=(",", ":")))
        now = int(time.time())
        st = self.f.read()
        st["windows"] = {
            "@1": jw.W("main", 1, **{"@agent_state": "running", "@agent_kind": "claude",
                                     "@agent_since": "%d running" % (now - 30), "@agent_bg_owner": "user",
                                     "@agent_kids_hold": "1",
                                     "@agent_detail_kind": "done", "@agent_detail": "Spawned it.",
                                     "@agent_summary": "proj/abby"}),
            "@2": jw.W("main", 2, **{"@agent_state": "running", "@agent_kind": "claude",
                                     "@agent_since": "%d running" % (now - 20), "@agent_parent": "@1",
                                     "@agent_parent_pane": "%1", "@agent_turn_start": str(now - 20)}),
        }
        st["panes"] = [{"window": "@1", "tty": "/dev/ttys902"}, {"window": "@2", "tty": "/dev/ttys903"}]
        self.f.state.write_text(json.dumps(st))

    def tearDown(self):
        self.f.close()

    def opts(self, w):
        return self.f.read()["windows"][w]["opts"]

    def ticks(self, n):
        r = self.f.run("agent-tab-watcher.sh", timeout=10 + 2 * n, AGENT_TAB_WATCHER_MAX_TICKS=str(n))
        self.assertEqual(r.returncode, 0, r.stderr)

    def edit(self, fn):
        # Under the fake tmux's own lock: a watcher may be mid-call.
        with self.f.state.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            s = self.f.read(); fn(s); self.f.state.write_text(json.dumps(s))

    def test_running_child_holds_the_parent_with_the_gear(self):
        self.ticks(5)
        p, c = self.opts("@1"), self.opts("@2")
        self.assertEqual(c.get("@agent_child"), "@1")
        self.assertEqual(p.get("@agent_kids"), "1 1")
        self.assertEqual(p["@agent_state"], "running")      # not idled by the un-stick
        self.assertEqual(p.get("@agent_workflow"), "1")
        # Five ticks, one reconcile write: nothing changed after the first.
        writes = [c for c in self.f.read()["calls"] if c[0] == "set-option" and "@agent_kids" in c]
        self.assertEqual(len(writes), 1, writes)

    def test_held_parent_finishes_done_when_its_last_child_is_done(self):
        env = dict(self.f.env(AGENT_TAB_WATCHER_MAX_TICKS="10"))
        proc = subprocess.Popen(["bash", str(self.f.scripts / "agent-tab-watcher.sh")], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 15
        while self.opts("@1").get("@agent_kids") != "1 1" and time.monotonic() < deadline:
            time.sleep(.1)
        time.sleep(1.5)
        self.assertEqual(self.opts("@1")["@agent_state"], "running")
        self.edit(lambda s: s["windows"]["@2"]["opts"].update({"@agent_state": "idle"}))
        _, err = proc.communicate(timeout=40)
        self.assertEqual(proc.returncode, 0, err)
        p = self.opts("@1")
        self.assertEqual(p["@agent_state"], "done")
        self.assertRegex(p["@agent_since"], r"^\d+ done$")
        self.assertNotIn("@agent_workflow", p)
        self.assertNotIn("@agent_bg_owner", p)
        self.assertNotIn("@agent_kids_hold", p)
        self.assertEqual(p.get("@agent_kids"), "1 0")
        self.assertEqual(p["@agent_detail"], "Spawned it.")        # the held Stop's reply

    def test_child_done_before_the_first_tick_still_finishes_the_parent(self):
        self.edit(lambda s: s["windows"]["@2"]["opts"].update({"@agent_state": "idle"}))
        self.ticks(5)
        self.assertEqual(self.opts("@1")["@agent_state"], "done")

    def test_no_hold_mark_keeps_the_idle_release(self):
        # Esc mid-turn while a child ran, or a hold that was the bg work's
        # alone: no @agent_kids_hold, so the bare idle release.
        self.edit(lambda s: s["windows"]["@1"]["opts"].pop("@agent_kids_hold"))
        self.edit(lambda s: s["windows"]["@2"]["opts"].update({"@agent_state": "idle"}))
        self.ticks(5)
        p = self.opts("@1")
        self.assertEqual(p["@agent_state"], "idle")
        self.assertNotIn("@agent_bg_owner", p)

    def test_asking_child_keeps_the_parent_held_and_surfaces(self):
        now = int(time.time())
        self.edit(lambda s: s["windows"]["@2"]["opts"].update(
            {"@agent_state": "needs-input", "@agent_since": "%d needs-input" % now,
             "@agent_detail_kind": "perm", "@agent_detail": "Bash git push"}))
        self.ticks(4)
        p = self.opts("@1")
        self.assertEqual(p.get("@agent_kids_ask"), "@2\tneeds-input\tBash git push")
        self.assertEqual(p.get("@agent_kids"), "1 1")
        self.assertEqual(p["@agent_state"], "running")
        self.assertEqual(p.get("@agent_workflow"), "1")
        self.edit(lambda s: s["windows"]["@2"]["opts"].update({"@agent_state": "running"}))
        self.ticks(1)
        self.assertNotIn("@agent_kids_ask", self.opts("@1"))

    def test_parked_parent_frees_its_child(self):
        self.ticks(1)
        self.assertEqual(self.opts("@2").get("@agent_child"), "@1")
        self.edit(lambda s: s["windows"]["@1"]["opts"].update({"@space_hidden": "1"}))   # its session, as read
        self.ticks(1)
        self.assertNotIn("@agent_child", self.opts("@2"))
        self.assertNotIn("@agent_kids", self.opts("@1"))

    def test_never_started_child_holds_only_for_the_grace_then_is_collected(self):
        # @3: abby-delegate's tab, its claude died before SessionStart (no
        # agent pane, no state, only the mark).
        self.edit(lambda s: (s["windows"].update({"@3": jw.W("main", 3, **{"@agent_parent": "@1",
                                                                           "@agent_parent_pane": "%1"})}),
                             s["windows"]["@2"]["opts"].update({"@agent_state": "idle"}),
                             s["panes"].append({"window": "@3", "tty": "/dev/ttys999"})))
        self.ticks(6)
        p, c = self.opts("@1"), self.opts("@3")
        self.assertRegex(c.get("@agent_kid_since", ""), r"^\d+$")
        self.assertEqual(c.get("@agent_parent"), "@1")                 # held off the GC
        self.assertEqual(p.get("@agent_kids"), "2 1")
        self.assertEqual(p["@agent_state"], "running")
        # The grace runs out.
        self.edit(lambda s: s["windows"]["@3"]["opts"].update({"@agent_kid_since": str(int(time.time()) - 61)}))
        self.ticks(7)
        p, c = self.opts("@1"), self.opts("@3")
        self.assertEqual(p["@agent_state"], "done")
        for o in ("@agent_parent", "@agent_parent_pane", "@agent_kid_since", "@agent_child"):
            self.assertNotIn(o, c)
        self.assertEqual(p.get("@agent_kids"), "1 0")

    def test_gc_drops_the_childs_mark(self):
        self.f.procs.write_text("ttys902 4242 claude\n")         # the child's claude exited
        self.ticks(7)
        c = self.opts("@2")
        for o in ("@agent_state", "@agent_parent", "@agent_parent_pane", "@agent_child"):
            self.assertNotIn(o, c)
        self.assertNotIn("@agent_kids", self.opts("@1"))

    # --- EXTERNAL SUBAGENTS ---

    def test_paused_children_drain_the_held_parent(self):
        def child(state):
            self.edit(lambda s: s["windows"]["@2"]["opts"].update({"@agent_state": state}))
        child("paused")
        self.ticks(2)
        p = self.opts("@1")
        self.assertEqual(p["@agent_state"], "paused")
        self.assertEqual(p.get("@agent_kids_hold"), "1")             # still held for them
        self.assertEqual(p.get("@agent_workflow"), "1")
        self.assertEqual(p.get("@agent_kids"), "1 1")
        child("running")                                             # resumed
        self.ticks(1)
        self.assertEqual(self.opts("@1")["@agent_state"], "running")
        child("paused")
        self.ticks(1)
        self.assertEqual(self.opts("@1")["@agent_state"], "paused")
        child("idle")                                                # done while drained
        self.ticks(1)
        p = self.opts("@1")
        self.assertEqual(p["@agent_state"], "done")
        self.assertNotIn("@agent_kids_hold", p)
        self.assertRegex(p.get("@agent_turn_end", ""), r"^\d+ - loud$")

    def test_own_pause_flag_is_not_drained_here(self):
        self.edit(lambda s: (s["windows"]["@2"]["opts"].update({"@agent_state": "paused"}),
                             s["windows"]["@1"]["opts"].update({"@agent_paused": "5"})))
        self.ticks(2)
        self.assertEqual(self.opts("@1")["@agent_state"], "running")

    def test_intermediate_is_held_then_finishes_seen(self):
        now = int(time.time())
        self.f.procs.write_text("ttys902 4242 claude\nttys903 4343 claude\nttys904 4444 claude\n")
        (self.f.home / ".claude/sessions/4343.json").write_text(json.dumps(
            {"pid": 4343, "sessionId": "22222222-aaaa-bbbb-cccc-000000000003", "cwd": "/Users/x/proj",
             "status": "shell"}, separators=(",", ":")))
        def add(s):
            s["windows"]["@2"]["opts"].update({"@agent_kids_hold": "1", "@agent_bg_owner": "user",
                                               "@agent_detail_kind": "done", "@agent_detail": "Spawned g."})
            s["windows"]["@3"] = jw.W("main", 3, **{"@agent_state": "running", "@agent_kind": "claude",
                                                    "@agent_since": "%d running" % (now - 10),
                                                    "@agent_parent": "@2", "@agent_parent_pane": "%2",
                                                    "@agent_turn_start": str(now - 10)})
            s["panes"].append({"window": "@3", "tty": "/dev/ttys904"})
        self.edit(add)
        self.ticks(3)
        c, p = self.opts("@2"), self.opts("@1")
        self.assertEqual(c["@agent_state"], "running")               # held for its grandchild
        self.assertEqual(c.get("@agent_kids_sub"), "1 1")
        self.assertEqual(c.get("@agent_workflow"), "1")
        self.assertNotIn("@agent_kids", c)
        self.assertEqual(p.get("@agent_kids"), "2 2")
        self.assertEqual(self.opts("@3").get("@agent_child"), "@2")
        self.edit(lambda s: s["windows"]["@3"]["opts"].update({"@agent_state": "idle"}))
        self.ticks(9)
        c, p = self.opts("@2"), self.opts("@1")
        self.assertEqual(c["@agent_state"], "idle")                  # a child: seen, never green
        self.assertIn("@agent_seen_at", c)
        self.assertEqual(c["@agent_detail"], "Spawned g.")
        self.assertEqual(c.get("@agent_kids_sub"), "1 0")
        self.assertEqual(p["@agent_state"], "done")                  # then the root

    def test_parent_gc_orphans_its_children_for_good(self):
        self.f.procs.write_text("ttys903 4343 claude\n")              # the parent's claude exited
        self.ticks(7)
        p, c = self.opts("@1"), self.opts("@2")
        self.assertNotIn("@agent_state", p)
        self.assertRegex(c.get("@agent_kid_orphan_at", ""), r"^\d+$")   # the grace runs
        self.assertEqual(c.get("@agent_parent"), "@1")
        self.edit(lambda s: s["windows"]["@2"]["opts"].update({"@agent_kid_orphan_at": str(int(time.time()) - 6)}))
        self.ticks(1)
        p, c = self.opts("@1"), self.opts("@2")
        self.assertEqual(c.get("@agent_kid_orphan"), "@1")
        self.assertNotIn("@agent_kid_orphan_at", c)
        for o in ("@agent_parent", "@agent_parent_pane", "@agent_child"):
            self.assertNotIn(o, c)
        for o in ("@agent_kids", "@agent_kids_sub"):
            self.assertNotIn(o, p)
        # A new agent in the old parent's window adopts nobody.
        self.f.procs.write_text("ttys902 4242 claude\nttys903 4343 claude\n")
        self.edit(lambda s: s["windows"]["@1"]["opts"].update({"@agent_state": "running"}))
        self.ticks(2)
        self.assertNotIn("@agent_child", self.opts("@2"))
        self.assertNotIn("@agent_kids", self.opts("@1"))

    def test_born_child_that_never_starts_holds_only_for_the_grace(self):
        # A2: agent-spawn's targeted birth (idle + @agent_born, the birth
        # grace long gone), its harness never up: held off the GC and busy
        # for KIDS_START_GRACE from @agent_kid_since, then collected.
        now = int(time.time())
        self.edit(lambda s: (s["windows"].update({"@3": jw.W("main", 3, **{
                                 "@agent_state": "idle", "@agent_born": str(now - 100),
                                 "@agent_since": "%d idle" % (now - 100), "@agent_parent": "@1",
                                 "@agent_parent_pane": "%1", "@agent_spawn_id": "s-born1"})}),
                             s["panes"].append({"window": "@3", "tty": "/dev/ttys999"})))
        self.ticks(6)
        p, c = self.opts("@1"), self.opts("@3")
        self.assertRegex(c.get("@agent_kid_since", ""), r"^\d+$")
        self.assertEqual(c.get("@agent_parent"), "@1")
        self.assertEqual(c.get("@agent_state"), "idle")
        self.assertEqual(p.get("@agent_kids"), "2 2")                 # starting: busy
        self.edit(lambda s: s["windows"]["@3"]["opts"].update({"@agent_kid_since": str(int(time.time()) - 61)}))
        self.ticks(7)
        p, c = self.opts("@1"), self.opts("@3")
        for o in ("@agent_state", "@agent_parent", "@agent_kid_since", "@agent_spawn_id"):
            self.assertNotIn(o, c)
        self.assertEqual(p.get("@agent_kids"), "1 1")
        self.assertEqual(p["@agent_state"], "running")               # @2 still runs


class KidsCodexChildTests(unittest.TestCase):
    """A codex child's finish on the watcher's rollout path (no Stop seen):
    SEEN, recorded as a turn end, ready, and its parent woken; an aborted
    turn is the child's failure."""

    def setUp(self):
        import test_codex_watcher_parity as cx
        self.cx = cx
        self.f = jw.FakeEnv(windows={}, panes=[])
        self.addCleanup(self.f.close)
        self.f.procs.write_text(cx.PROCS)
        self.log = self.f.dir / "deliver.log"
        self.stub = self.f.dir / "agent-spawn"
        self.stub.write_text('#!/bin/sh\necho "$*" >> "%s"\n' % self.log)
        self.stub.chmod(0o755)

    def run_child(self, end, ticks=4):
        cx, now = self.cx, int(time.time())
        rp = self.f.home / ".codex/sessions/2026/11/24/rollout-k.jsonl"
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(cx.ev(now - 90, "task_started") + cx.filler() + end(now - 10))
        st = self.f.read()
        st["windows"] = {
            "@1": jw.W("main", 1, **{"@agent_state": "idle", "@agent_kind": "claude",
                                     "@agent_since": "%d idle" % (now - 100)}),
            "@2": jw.W("main", 2, **{"@agent_state": "running", "@agent_kind": "codex",
                                     "@agent_since": "%d running" % (now - 60), "@agent_rollout": str(rp),
                                     "@agent_detail_kind": "run", "@agent_detail": "do it",
                                     "@agent_parent": "@1", "@agent_parent_pane": "%1",
                                     "@agent_turn_start": str(now - 60), "@agent_spawn_id": "s-cx1",
                                     "@agent_spawn_mode": "async"}),
        }
        st["panes"] = [{"window": "@1", "tty": cx.CLAUDE_TTY}, {"window": "@2", "tty": cx.CODEX_TTY}]
        self.f.state.write_text(json.dumps(st))
        r = self.f.run("agent-tab-watcher.sh", timeout=10 + 2 * ticks, AGENT_TAB_WATCHER_MAX_TICKS=str(ticks),
                       AGENT_SPAWN_BIN=str(self.stub))
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.f.read()["windows"]

    def delivered(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.log.exists():
            time.sleep(.1)
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_codex_child_finish_is_seen_and_ready(self):
        w = self.run_child(lambda t: self.cx.ev(t, "task_complete", last_agent_message="All set."))
        c = w["@2"]["opts"]
        self.assertEqual(c["@agent_state"], "idle")                  # not an unseen green
        self.assertIn("@agent_seen_at", c)
        self.assertEqual((c["@agent_detail_kind"], c["@agent_detail"]), ("done", "turn finished"))
        self.assertRegex(c.get("@agent_turn_end", ""), r"^\d+ - loud$")
        self.assertEqual(c.get("@agent_spawn_result"), "ready")
        self.assertEqual(self.delivered(), ["_deliver s-cx1"])

    def test_codex_child_aborted_turn_is_failed(self):
        w = self.run_child(lambda t: self.cx.ev(t, "turn_aborted", reason="interrupted"), ticks=5)
        c, p = w["@2"]["opts"], w["@1"]["opts"]
        self.assertEqual(c["@agent_state"], "failed")
        self.assertEqual((c["@agent_detail_kind"], c["@agent_detail"]), ("fail", "turn aborted"))
        self.assertRegex(c.get("@agent_turn_end", ""), r"^\d+ - failed$")
        self.assertEqual(p.get("@agent_kids_ask"), "@2\tfailed\tturn aborted")   # surfaced on the parent


@unittest.skipUnless(REAL_TMUX, "tmux executable required")
class KidsUnlinkHookTests(unittest.TestCase):
    """The window-unlinked[3] line documented in agent-tab-indicator.md §8
    (copied verbatim into tmux.conf.tmpl), on a private server."""

    DOC = ao.SCRIPTS.parent / "docs/agent-tab-indicator.md"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="kh-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.sock = str(self.dir / "s")
        self.tm = [REAL_TMUX, "-S", self.sock, "-f", "/dev/null"]
        home = self.dir / "home"; lib = home / ".config/tmux/scripts"; lib.mkdir(parents=True)
        shutil.copyfile(LIB, lib / "agent-session-lib.sh")
        b = self.dir / "bin"; b.mkdir()
        # The lib's bare `tmux` must reach THIS server, never the default one.
        (b / "tmux").write_text("#!/bin/sh\nexec %s -S %s \"$@\"\n" % (REAL_TMUX, self.sock))
        (b / "tmux").chmod(0o755)
        self.env = clean_env(HOME=str(home), PATH="%s:%s:%s" % (b, jw.BASH_DIR, os.environ["PATH"]))
        self.addCleanup(lambda: subprocess.run(self.tm + ["kill-server"], capture_output=True, timeout=5))
        self.t("new-session", "-d", "-s", "s", "/bin/sleep 600")
        m = re.search(r"^  (set-hook -g 'window-unlinked\[3\]'.*)$", self.DOC.read_text(), re.M)
        self.assertTrue(m, "hook line missing from the doc")
        conf = self.dir / "hook.conf"; conf.write_text(m.group(1) + "\n")
        self.t("source-file", str(conf))

    def t(self, *args):
        r = subprocess.run(self.tm + list(args), text=True, capture_output=True, env=self.env, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.rstrip("\n")

    def pair(self):
        p = self.t("new-window", "-d", "-P", "-F", "#{window_id}", "/bin/sleep 600")
        c = self.t("new-window", "-d", "-P", "-F", "#{window_id}", "/bin/sleep 600")
        self.t("set-option", "-w", "-t", p, "@agent_state", "running")
        for k, v in (("@agent_parent", p), ("@agent_state", "running"), ("@agent_turn_start", "100")):
            self.t("set-option", "-w", "-t", c, k, v)
        return p, c

    def test_killed_parent_orphans_at_once_only_while_guarded(self):
        # Unguarded (nobody reconciled: @agent_kids_any unset): the hook runs nothing.
        q, d = self.pair()
        self.t("kill-window", "-t", q)
        time.sleep(1)
        self.assertEqual(self.t("show-options", "-wqv", "-t", d, "@agent_parent"), q)
        self.t("set-option", "-wu", "-t", d, "@agent_parent")
        # Guarded: a reconcile sets the global; killing the parent orphans now.
        p, c = self.pair()
        r = subprocess.run(["bash", "-c", '. "$1" && kids_reconcile', "_", str(LIB)], env=self.env,
                           text=True, capture_output=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.t("show-options", "-gqv", "@agent_kids_any"), "1")
        self.assertEqual(self.t("show-options", "-wqv", "-t", c, "@agent_child"), p)
        self.t("kill-window", "-t", p)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.t("show-options", "-wqv", "-t", c, "@agent_kid_orphan"):
            time.sleep(.1)
        self.assertEqual(self.t("show-options", "-wqv", "-t", c, "@agent_kid_orphan"), p)
        self.assertEqual(self.t("show-options", "-wqv", "-t", c, "@agent_parent"), "")
        # No link left anywhere: the guard is down again.
        self.assertEqual(self.t("show-options", "-gqv", "@agent_kids_any"), "")


class KidsNeedsListTests(unittest.TestCase):
    """agent-jump.sh `list` and agent-roster.py's needs_order, on the fake tmux."""

    def make(self, **windows):
        self.f = jw.FakeEnv(windows=windows)
        self.addCleanup(self.f.close)

    def listed(self):
        r = self.f.run("agent-jump.sh", "list")
        self.assertEqual(r.returncode, 0, r.stderr)
        return [l.split("\t") for l in r.stdout.splitlines() if l]

    def roster(self):
        r = subprocess.run([str(self.f.bin / "tmux"), "list-windows", "-a", "-F", R.FMT],
                           env=self.f.env(), capture_output=True, text=True, timeout=30)
        return R.parse_windows(r.stdout)

    def roster_pairs(self):
        R.collation("C")
        try:
            return R.needs_order(self.roster())
        finally:
            R._XFRM = None

    def test_every_asking_child_is_a_row_under_the_parents_label(self):
        self.make(**{
            "@1": jw.W("main", 1, **{"@agent_state": "running", "@agent_summary": "proj/abby",
                                     "@agent_kids": "3 2", "@agent_kids_ask": "@3\tneeds-input\tBash ls"}),
            "@2": jw.W("main", 2, **{"@agent_state": "done", "@agent_child": "@1", "@agent_summary": "proj/kid-a"}),
            "@3": jw.W("main", 3, **{"@agent_state": "needs-input", "@agent_since": "100 needs-input",
                                     "@agent_child": "@1", "@agent_summary": "proj/kid-b"}),
            "@5": jw.W("main", 5, **{"@agent_state": "needs-input", "@agent_since": "200 needs-input",
                                     "@agent_child": "@1", "@agent_summary": "proj/kid-c"}),
            "@4": jw.W("main", 4, **{"@agent_state": "failed", "@agent_since": "50 failed",
                                     "@agent_summary": "other"}),
        })
        rows = self.listed()
        self.assertEqual([r[0] for r in rows], ["@4", "@3", "@5"])     # the done child: no row
        self.assertEqual(rows[1], ["@3", "main", "3", "needs-input", "100", "proj/abby"])
        self.assertEqual(rows[2], ["@5", "main", "5", "needs-input", "200", "proj/abby"])
        self.assertEqual(self.roster_pairs(), [("@4", "main"), ("@3", "main"), ("@5", "main")])

    def test_rows_come_from_the_childs_own_state(self):
        self.make(**{
            "@1": jw.W("main", 1, **{"@agent_state": "running", "@agent_summary": "proj/abby",
                                     "@agent_kids_ask": "@3\tneeds-input\tBash ls"}),   # stale
            "@3": jw.W("main", 3, **{"@agent_state": "running", "@agent_child": "@1"}),
        })
        self.assertEqual(self.listed(), [])
        self.assertEqual(self.roster_pairs(), [])

    def test_failed_child_ranks_with_the_failures(self):
        self.make(**{
            "@1": jw.W("main", 1, **{"@agent_state": "idle", "@agent_summary": "proj/abby",
                                     "@agent_kids_ask": "@3\tfailed\toverloaded"}),
            "@3": jw.W("main", 3, **{"@agent_state": "failed", "@agent_since": "300 failed", "@agent_child": "@1"}),
            "@4": jw.W("main", 4, **{"@agent_state": "needs-input", "@agent_since": "10 needs-input"}),
        })
        rows = self.listed()
        self.assertEqual([(r[0], r[3], r[5]) for r in rows], [("@3", "failed", "proj/abby"), ("@4", "needs-input", "zsh")])
        self.assertEqual([i for i, _ in self.roster_pairs()], ["@3", "@4"])

    def test_grandchild_is_labelled_with_the_root(self):
        self.make(**{
            "@1": jw.W("main", 1, **{"@agent_state": "running", "@agent_summary": "proj/abby"}),
            "@2": jw.W("main", 2, **{"@agent_state": "running", "@agent_child": "@1", "@agent_summary": "proj/kid"}),
            "@3": jw.W("main", 3, **{"@agent_state": "needs-input", "@agent_since": "100 needs-input",
                                     "@agent_child": "@2", "@agent_summary": "proj/grandkid"}),
            # A loop: no root, its own label.
            "@4": jw.W("main", 4, **{"@agent_state": "failed", "@agent_since": "50 failed",
                                     "@agent_child": "@5", "@agent_summary": "loop-a"}),
            "@5": jw.W("main", 5, **{"@agent_state": "running", "@agent_child": "@4", "@agent_summary": "loop-b"}),
        })
        rows = self.listed()
        self.assertEqual([(r[0], r[5]) for r in rows], [("@4", "loop-a"), ("@3", "proj/abby")])
        self.assertEqual(self.roster_pairs(), [("@4", "main"), ("@3", "main")])
        R.collation("C")
        try:
            ws = self.roster()
            keyed = R.needs_order(ws)
        finally:
            R._XFRM = None
        self.assertEqual([i for i, _ in keyed], ["@4", "@3"])

    def test_roster_folds_children(self):
        self.make(**{
            "@1": jw.W("main", 1, **{"@agent_state": "running", "@agent_summary": "proj/abby", "@agent_kids": "2 1"}),
            "@2": jw.W("main", 2, **{"@agent_state": "running", "@agent_child": "@1", "@agent_summary": "proj/kid-a"}),
            "@3": jw.W("main", 3, **{"@agent_state": "needs-input", "@agent_since": "100 needs-input",
                                     "@agent_child": "@1", "@agent_summary": "proj/kid-b"}),
        })
        ws = self.roster()
        self.assertEqual(R.counts(ws), [("working", 1)])
        R.collation("C")
        try:
            needs = R.needs_order(ws)
        finally:
            R._XFRM = None
        items = R.build_items(ws, needs, None)
        sess_rows = [it["w"]["id"] for it in items if it.get("key", ("",))[0] == "sess"]
        need_rows = [it["w"]["id"] for it in items if it.get("key", ("",))[0] == "need"]
        self.assertEqual(sess_rows, ["@1"])           # no own rows for children
        self.assertEqual(need_rows, ["@3"])           # an asking one is in NEEDS YOU


if __name__ == "__main__":
    unittest.main()
