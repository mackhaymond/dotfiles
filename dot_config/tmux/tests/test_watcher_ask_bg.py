"""agent-tab-watcher.sh and a raised ask (the indicator's ASKS WHILE WORK IS
OUT): needs-input + @agent_ask_bg.

  - the viewed-discharge (Mack Cmd-Tabs back to the tab) of a raised ask
    while its work is still out (@agent_workflow) is ONE guarded command
    list: running, @agent_since "E running", @agent_seen_at E, detail kind
    and @agent_ask_bg unset, the shown line kept as @agent_ask_seen; with
    nothing out, the ordinary idle + pending discharge (ask_bg dropped too);
  - a hook that moved the window first wins (CONDITIONAL WRITES);
  - its gear stays true for a background TASK (agent-bg-pending), which the
    watcher otherwise only asks about for `running`;
  - the GC drops @agent_ask_bg and @agent_ask_seen with the rest.
Fakes only (test_agent_jump_watcher's fake tmux; test_bg_task_hold's
session layout); no real tmux server is touched.
"""
import json
import time
import unittest

import test_agent_jump_watcher as jw
import test_bg_task_hold as bh
import test_watcher_conditional_writes as cw
from test_agent_state_notice import FAKE_LSAPPINFO, write_exe

ASKED = {"@agent_state": "needs-input", "@agent_since": "100 needs-input", "@agent_kind": "claude",
         "@agent_ask_bg": "1", "@agent_ask_from": "main", "@agent_detail_kind": "ask",
         "@agent_detail": "pick A or B"}


class WatcherAskBgTests(cw.WatcherConditionalWriteTests):
    for _n in [n for n in dir(cw.WatcherConditionalWriteTests) if n.startswith("test_")]:
        locals()[_n] = None
    del _n

    def test_seen_with_work_out_goes_running_in_one_guarded_write(self):
        self.make(dict(ASKED, **{"@agent_workflow": "1"}), viewed=True)
        o = self.ticks(1)
        self.assertEqual(o["@agent_state"], "running")
        seen = o["@agent_seen_at"]
        self.assertRegex(seen, r"^\d+$")
        self.assertEqual(o["@agent_since"], seen + " running")
        for gone in ("@agent_detail_kind", "@agent_ask_bg", "@agent_ask_from", "@agent_pending"):
            self.assertNotIn(gone, o)
        self.assertEqual(o["@agent_ask_seen"], "pick A or B")
        self.assertEqual(o["@agent_detail"], "pick A or B")
        # One guarded call: an if-shell whose command string carries it all.
        calls = [c for c in self.f.read()["calls"] if c[0] == "if-shell" and "@agent_ask_bg" in c[-1]]
        self.assertEqual(len(calls), 1, calls)
        body = calls[0][-1]
        for part in ("@agent_state running", "@agent_since '%s running'" % seen, "@agent_seen_at " + seen,
                     "-uw -t @1 @agent_detail_kind", "-uw -t @1 @agent_ask_bg",
                     "-F @agent_ask_seen '#{@agent_detail}'"):
            self.assertIn(part, body)

    def test_seen_with_nothing_out_is_the_ordinary_discharge(self):
        self.make(dict(ASKED), viewed=True)
        o = self.ticks(1)
        self.assertEqual(o["@agent_state"], "idle")
        self.assertRegex(o["@agent_pending"], r"^\d+$")
        self.assertNotIn("@agent_ask_bg", o)
        self.assertNotIn("@agent_ask_from", o)
        self.assertEqual(o["@agent_ask_seen"], "pick A or B")

    def test_a_hook_that_moved_first_wins(self):
        self.make(dict(ASKED, **{"@agent_workflow": "1"}), viewed=True)
        o = self.ticks(1, race="@agent_state=running,@agent_since=200 running")
        self.assertEqual(o["@agent_since"], "200 running")
        self.assertNotIn("@agent_seen_at", o)
        self.assertNotIn("@agent_ask_seen", o)
        self.assertEqual(o["@agent_detail_kind"], "ask")

    def test_not_viewed_is_left_alone(self):
        self.make(dict(ASKED, **{"@agent_workflow": "1"}))
        o = self.ticks(2)
        self.assertEqual(o["@agent_state"], "needs-input")
        self.assertEqual(o["@agent_ask_bg"], "1")

    def test_a_plain_gate_is_unchanged(self):
        plain = {k: v for k, v in ASKED.items() if k != "@agent_ask_bg"}
        self.make(dict(plain, **{"@agent_workflow": "1"}), viewed=True)
        o = self.ticks(1)
        self.assertEqual(o["@agent_state"], "idle")
        self.assertNotIn("@agent_ask_seen", o)

    def test_a_look_copies_only_an_ask(self):
        # @agent_ask_bg still set, but the detail is no longer the ask: the
        # look must not record it as a seen ask, on either branch.
        for extra in ({"@agent_workflow": "1"}, {}):
            self.make(dict(ASKED, **{"@agent_detail_kind": "perm", "@agent_detail": "Bash make deploy"}, **extra),
                      viewed=True)
            o = self.ticks(1)
            self.assertNotIn("@agent_ask_seen", o, extra)
            self.assertNotIn("@agent_ask_bg", o, extra)

    def test_gc_drops_the_ask_options(self):
        self.make(dict(ASKED, **{"@agent_ask_seen": "pick A or B", "@agent_turn_start": "123"}),
                  tty="/dev/ttys901")
        o = self.ticks(6)
        for name in ("@agent_state", "@agent_ask_bg", "@agent_ask_seen", "@agent_turn_start"):
            self.assertNotIn(name, o)


class AskBgGearTests(bh.BgTaskHoldTests):
    for _n in [n for n in dir(bh.BgTaskHoldTests) if n.startswith("test_")]:
        locals()[_n] = None
    del _n

    def asked(self, **extra):
        st = self.f.read()
        st["windows"]["@1"]["opts"] = dict(ASKED, **extra)
        self.f.state.write_text(json.dumps(st))

    def test_a_raised_ask_keeps_its_gear_for_a_background_task(self):
        self.asked()
        o = self.ticks(2, rc=0)
        self.assertEqual(o["@agent_state"], "needs-input")
        self.assertEqual(o.get("@agent_workflow"), "1")
        self.assertEqual(len(self.asks()), 1)

    def test_a_plain_gate_never_asks(self):
        self.asked()
        st = self.f.read()
        del st["windows"]["@1"]["opts"]["@agent_ask_bg"]
        self.f.state.write_text(json.dumps(st))
        o = self.ticks(2, rc=0)
        self.assertEqual(o["@agent_state"], "needs-input")
        self.assertNotIn("@agent_workflow", o)
        self.assertEqual(self.asks(), [])


if __name__ == "__main__":
    unittest.main()
