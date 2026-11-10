"""@thread_at: the epoch of MACK's last own prompt into a window, stamped by
agent-tab-indicator.sh on UserPromptSubmit only when its classifier
(p_mack: not a verified peer message, no "[message from @h]" opener, not a
harness wake-up nor a /loop, schedule or poll source) says the prompt is
his, in set_state's one command list. The agent sidebar folds dormant
spaces and long-idle agents by it (docs/agent-tab-indicator.md).

Drives the real indicator against a private `tmux -L <unique> -f /dev/null`
server (test_agent_origin's OriginHarness, killed in cleanup). Run with
unittest or pytest.
"""
import time
import unittest

import test_agent_origin as ao


class ThreadAtTests(ao.OriginHarness):
    def stamp(self):
        return self.opt("@thread_at")

    def assert_fresh(self, value, before):
        self.assertRegex(value, r"^\d+$")
        self.assertGreaterEqual(int(value), before)
        self.assertLessEqual(int(value), int(time.time()) + 1)

    def test_macks_prompt_stamps_it(self):
        before = int(time.time())
        self.prompt("fix the parser", "p1")
        self.assertEqual(self.opt("@agent_state"), "running")
        self.assert_fresh(self.stamp(), before)

    def test_mid_turn_prompt_restamps(self):
        self.prompt("first", "p1")
        self.set_opt("@thread_at", "1000")
        self.prompt("and also this", "p1")          # same turn, Mack typing again
        self.assertNotEqual(self.stamp(), "1000")
        self.assert_fresh(self.stamp(), int(time.time()) - 5)

    def test_verified_peer_message_never_stamps(self):
        self.set_opt("@agent_state", "idle")
        self.prompt(self.deliver("please rerun the suite"), "p1")
        self.assertEqual(self.opt("@agent_turn_origin"), "agent p1 peer")
        self.assertFalse(self.has("@thread_at"))

    def test_unverified_peer_opener_never_stamps(self):
        # No @agent_inbound entry: unverified, still never Mack's.
        self.prompt("[message from @ghost] do the thing", "p1")
        self.assertFalse(self.has("@thread_at"))

    def test_harness_wakeup_never_stamps(self):
        self.prompt("<task-notification>\n<task-id>b1</task-id></task-notification>", "w1")
        self.assertFalse(self.has("@thread_at"))

    def test_loop_and_schedule_wakeups_never_stamp(self):
        for i, src in enumerate(("loop_wakeup", "schedule_wakeup", "poll_event")):
            payload = {"session_id": "s1", "cwd": str(self.root), "hook_event_name": "UserPromptSubmit",
                       "prompt": "check the deploy", "prompt_id": "l%d" % i, "source": src,
                       "transcript_path": str(self.transcript)}
            self.hook("running", "claude", payload=payload)
            self.assertFalse(self.has("@thread_at"), src)

    def source_prompt(self, src, pid):
        payload = {"session_id": "s1", "cwd": str(self.root), "hook_event_name": "UserPromptSubmit",
                   "prompt": "keep going", "prompt_id": pid, "source": src,
                   "transcript_path": str(self.transcript)}
        self.hook("running", "claude", payload=payload)

    def test_system_auto_continuation_never_stamps(self):
        # Native auto-continuation: Mack typed nothing. The turn still runs.
        self.source_prompt("system", "c1")
        self.assertEqual(self.opt("@agent_state"), "running")
        self.assertFalse(self.has("@thread_at"))

    def test_user_source_stamps(self):
        before = int(time.time())
        self.source_prompt("user", "u1")
        self.assert_fresh(self.stamp(), before)

    def test_peer_after_mack_keeps_macks_stamp(self):
        self.prompt("mine", "p1")
        self.stop("p1")
        self.set_opt("@thread_at", "1234")
        self.prompt(self.deliver("ping"), "p2")
        self.assertEqual(self.stamp(), "1234")
        self.prompt("<task-notification>\n<task-id>x</task-id>", "w3")
        self.assertEqual(self.stamp(), "1234")


if __name__ == "__main__":
    unittest.main()
