"""agent-tab-watcher.sh's CONDITIONAL WRITES, EXPIRED PENDING's busy turn,
and the GC's @agent_summary_key unset (2026-10-22).

Every state the watcher writes is decided from the tick's one read, which
can be seconds old when the write lands. A hook that moved the window in
between must win: each write is an if-shell -F guarded by the value it was
decided from. The race is staged by a tmux wrapper that lets a "hook" write
land right before the watcher's first guarded call. Fakes only (the shared
one from test_agent_jump_watcher.py); no real tmux server is touched.
"""

import json
import os
import time
import unittest

import test_agent_jump_watcher as jw   # module only, for the shared fakes
from test_agent_state_notice import FAKE_LSAPPINFO, write_exe

# The hook that lands mid-tick: on the FIRST call that writes @agent_state or
# @agent_pending (plain, or inside a guarded if-shell), $RACE_OPTS
# ("name=value,...") is written to @1 before the call itself runs. Once per
# test (a mark file).
RACE_TMUX = r'''#!/usr/bin/env python3
import os, subprocess, sys
real = os.environ["REAL_FAKE_TMUX"]
mark = os.environ["FAKE_TMUX_STATE"] + ".raced"
argv = sys.argv[1:]
writes = ("set-option" in argv or "if-shell" in argv) \
    and any("@agent_state" in a or "@agent_pending" in a for a in argv)
if writes and os.environ.get("RACE_OPTS") and not os.path.exists(mark):
    open(mark, "w").close()
    for kv in os.environ["RACE_OPTS"].split(","):
        k, v = kv.split("=", 1)
        subprocess.run([real, "set-option", "-w", "-t", "@1", k, v])
os.execv(real, [real] + sys.argv[1:])
'''


class WatcherConditionalWriteTests(unittest.TestCase):
    def make(self, opts, tty="/dev/ttys900", viewed=False, status=None):
        w = jw.W("main", 1, **opts)
        if viewed:
            w["active_clients"] = 1
        self.f = jw.FakeEnv(windows={"@1": w}, panes=[{"window": "@1", "tty": tty}])
        self.addCleanup(self.f.close)
        write_exe(self.f.bin / "lsappinfo", FAKE_LSAPPINFO)
        real = self.f.bin / "tmux.fake"
        (self.f.bin / "tmux").rename(real)
        write_exe(self.f.bin / "tmux", RACE_TMUX)
        self.real = str(real)
        if status is not None:
            sessions = self.f.home / ".claude/sessions"
            sessions.mkdir(parents=True)
            (sessions / "4242.json").write_text(json.dumps({"pid": 4242, "status": status},
                                                           separators=(",", ":")))

    def ticks(self, n, race="", **extra):
        env = dict(AGENT_TAB_WATCHER_MAX_TICKS=str(n), REAL_FAKE_TMUX=self.real, RACE_OPTS=race,
                   FAKE_FRONT="wez")
        env.update(extra)
        r = self.f.run("agent-tab-watcher.sh", timeout=15 + 2 * n, **env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.f.read()["windows"]["@1"]["opts"]

    # --- CONDITIONAL WRITES -------------------------------------------------

    def test_seen_done_discharge_never_lands_over_a_new_turn(self):
        # Mack Cmd-Tabs back to a green tab as a prompt starts its turn.
        self.make({"@agent_state": "done", "@agent_since": "100 done", "@agent_kind": "claude"}, viewed=True)
        o = self.ticks(1, race="@agent_state=running,@agent_since=200 running")
        self.assertEqual(o["@agent_state"], "running")
        self.assertNotIn("@agent_seen_at", o)
        self.assertNotIn("@agent_pending", o)
        self.assertEqual(o["@agent_since"], "200 running")

    def test_seen_done_discharge_without_a_race_still_lands(self):
        self.make({"@agent_state": "done", "@agent_since": "100 done", "@agent_kind": "claude"}, viewed=True)
        o = self.ticks(1)
        self.assertEqual(o["@agent_state"], "idle")
        self.assertIn("@agent_seen_at", o)

    def test_seen_gate_discharge_never_lands_over_a_new_turn(self):
        self.make({"@agent_state": "needs-input", "@agent_since": "100 needs-input",
                   "@agent_kind": "claude", "@agent_ask_from": "main"}, viewed=True)
        o = self.ticks(1, race="@agent_state=running")
        self.assertEqual(o["@agent_state"], "running")
        self.assertNotIn("@agent_pending", o)
        self.assertNotIn("@agent_seen_at", o)
        # Without the race: idle + pending + seen, the asker dropped.
        self.make({"@agent_state": "needs-input", "@agent_since": "100 needs-input",
                   "@agent_kind": "claude", "@agent_ask_from": "main"}, viewed=True)
        o = self.ticks(1)
        self.assertEqual(o["@agent_state"], "idle")
        self.assertRegex(o["@agent_pending"], r"^\d+$")
        self.assertNotIn("@agent_ask_from", o)

    def test_stuck_running_idle_never_lands_over_a_new_gate(self):
        # Three idle readings un-stick `running`; a permission prompt that
        # lands right before that write keeps its yellow.
        self.make({"@agent_state": "running", "@agent_since": "100 running", "@agent_kind": "claude"},
                  status="idle")
        o = self.ticks(3, race="@agent_state=needs-input,@agent_since=200 needs-input")
        self.assertEqual(o["@agent_state"], "needs-input")
        self.assertEqual(o["@agent_since"], "200 needs-input")
        self.make({"@agent_state": "running", "@agent_since": "100 running", "@agent_kind": "claude"},
                  status="idle")
        self.assertEqual(self.ticks(3)["@agent_state"], "idle")

    def test_paused_turn_end_never_lands_over_a_new_gate(self):
        # The same race on a paused key: the `paused` write is guarded on
        # the state too, not just the resume flag.
        paused = {"@agent_state": "running", "@agent_since": "100 running", "@agent_kind": "claude",
                  "@agent_paused": "123"}
        self.make(dict(paused), status="idle")
        o = self.ticks(3, race="@agent_state=needs-input,@agent_since=200 needs-input")
        self.assertEqual(o["@agent_state"], "needs-input")
        self.assertEqual(o["@agent_since"], "200 needs-input")
        # Without the race: paused.
        self.make(dict(paused), status="idle")
        o = self.ticks(3)
        self.assertEqual(o["@agent_state"], "paused")
        self.assertRegex(o["@agent_since"], r"^\d+ paused$")
        # A resume cleared the flag before the write: idle, in the same call.
        self.make(dict(paused), status="idle")
        o = self.ticks(3, race="@agent_paused=")
        self.assertEqual(o["@agent_state"], "idle")

    def test_idle_seed_never_lands_over_a_first_hook(self):
        self.make({})
        o = self.ticks(1, race="@agent_state=running")
        self.assertEqual(o["@agent_state"], "running")
        self.assertRegex(o["@agent_since"], r"^\d+ running$")
        self.make({})
        self.assertEqual(self.ticks(1)["@agent_state"], "idle")

    # --- EXPIRED PENDING: A TURN STILL GOING ----------------------------------

    def test_expired_pending_of_a_busy_turn_becomes_running(self):
        # An approved 12-minute tool: no PostToolUse until it ends, and the
        # stamp is past 600 s. The session file says the turn is going.
        old = str(int(time.time()) - 700)
        self.make({"@agent_state": "idle", "@agent_since": "100 idle", "@agent_kind": "claude",
                   "@agent_pending": old}, status="busy")
        o = self.ticks(1)
        self.assertEqual(o["@agent_state"], "running")
        self.assertRegex(o["@agent_since"], r"^\d+ running$")
        self.assertNotIn("@agent_pending", o)

    def test_expired_pending_of_a_finished_turn_is_just_dropped(self):
        old = str(int(time.time()) - 700)
        for status in ("idle", "shell", "waiting", None):
            self.make({"@agent_state": "idle", "@agent_since": "100 idle", "@agent_kind": "claude",
                       "@agent_pending": old}, status=status)
            o = self.ticks(1)
            self.assertEqual(o["@agent_state"], "idle", status)
            self.assertNotIn("@agent_pending", o, status)

    def test_expired_pending_never_drops_a_fresh_stamp(self):
        old = str(int(time.time()) - 700)
        fresh = str(int(time.time()))
        self.make({"@agent_state": "idle", "@agent_since": "100 idle", "@agent_kind": "claude",
                   "@agent_pending": old}, status="busy")
        o = self.ticks(1, race=f"@agent_pending={fresh}")
        self.assertEqual((o["@agent_state"], o["@agent_pending"]), ("idle", fresh))

    # --- GC --------------------------------------------------------------------

    def test_gc_unsets_the_summary_key(self):
        # The agent is gone (a plain zsh on the tty): after GC_TICKS the
        # condenser's key goes with the title, so a late worker can't paint.
        self.make({"@agent_state": "idle", "@agent_kind": "claude", "@agent_summary": "proj/Title",
                   "@agent_summary_cond": "1", "@agent_summary_key": "k123"}, tty="/dev/ttys901")
        o = self.ticks(6)
        for name in ("@agent_state", "@agent_summary", "@agent_summary_cond", "@agent_summary_key"):
            self.assertNotIn(name, o)


if __name__ == "__main__":
    unittest.main()
