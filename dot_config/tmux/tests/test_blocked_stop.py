"""A BLOCKED STOP: another Stop hook blocks, the turn goes on, the tab re-arms.

Stop hooks run in parallel, so when one blocks the stop (chezmoi-guard) the
indicator has already written done/idle for a turn that continues, and no
prompt event follows. The continuation's first main-agent PostToolUse must
re-arm `running` - but only when the transcript's last turn mark is the
harness's "Stop hook feedback" record: a late tool event after a real end (or
an interrupt) resurrects nothing. Same private-server harness as
test_agent_ask_from. Run with unittest.
"""
import json
import unittest

import test_agent_ask_from as af
import test_agent_state_notice as sn

FEEDBACK = {"isSidechain": False, "type": "user", "message": {
    "role": "user", "content": "Stop hook feedback:\nchezmoi-guard: unfinished dotfile work"},
    "isMeta": True, "timestamp": "2026-11-25T15:00:33.665Z"}
TURN_END = {"isSidechain": False, "type": "system", "subtype": "turn_duration",
            "durationMs": 152176}
INTERRUPT = {"type": "user", "message": {"role": "user", "content": [
    {"type": "text", "text": "[Request interrupted by user]"}]}}
QUOTED = {"type": "user", "message": {"role": "user", "content": [
    {"type": "tool_result", "content": json.dumps(FEEDBACK, separators=(",", ":"))}]}}


@unittest.skipUnless(sn.REAL_TMUX, "tmux executable required")
class BlockedStopTests(sn.IndicatorNoticeTests):
    for _n in [n for n in dir(sn.IndicatorNoticeTests) if n.startswith("test_")]:
        locals()[_n] = None
    del _n

    raw = af.AskerGateTests.raw
    state = af.AskerGateTests.state

    def setUp(self):
        super().setUp()
        sn.write_exe(self.bin / "copilot", "#!/bin/sh\nexit 1\n")
        self.tp = self.bin / "transcript.jsonl"

    def hook(self, event, agent=None, **rest):
        d = {"session_id": "11111111-2222-3333-4444-555555555555",
             "transcript_path": str(self.tp), "cwd": "/work/project", "prompt_id": "p1"}
        if agent:
            d["agent_id"] = agent
        d["hook_event_name"] = event
        d.update(rest)
        return json.dumps(d, separators=(",", ":"))

    def write(self, *recs):
        self.tp.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in recs))

    def finish(self, watched=False):
        if watched:
            self.env.update(FAKE_ACTIVE_CLIENTS="1", FAKE_FRONT="wez")
        self.raw("running", self.hook("UserPromptSubmit", prompt="do it"))
        self.raw("done", self.hook("Stop", last_assistant_message="All set."))
        self.assertIn(self.state(), ("done", "idle"))
        return self.state()

    def tool(self, agent=None, response="x" * 200_000):
        self.raw("heartbeat", self.hook("PostToolUse", agent, tool_name="Read",
                                        tool_input={}, tool_response=response))

    def test_a_blocked_stop_rearms_on_the_next_tool_call(self):
        self.write(TURN_END)
        self.finish()
        self.write(TURN_END, FEEDBACK)
        self.tool()
        self.assertEqual(self.state(), "running")
        self.raw("done", self.hook("Stop", last_assistant_message="Committed."))
        self.assertEqual(self.state(), "done")

    def test_a_real_end_stays_finished(self):
        for recs in ((), (TURN_END,), (FEEDBACK, TURN_END), (FEEDBACK, INTERRUPT),
                     (TURN_END, QUOTED)):
            self.write()
            was = self.finish()
            self.write(*recs)
            self.tool()
            self.assertEqual(self.state(), was, recs)

    def test_a_watched_finish_rearms_from_idle(self):
        self.write(TURN_END)
        self.assertEqual(self.finish(watched=True), "idle")
        self.write(TURN_END, FEEDBACK)
        self.tool()
        self.assertEqual(self.state(), "running")

    def test_a_rearm_takes_the_blocked_turn_end_back(self):
        self.write()
        self.finish()
        self.assertTrue(self.opt("@agent_turn_end"))
        self.write(FEEDBACK)
        self.tool()
        self.assertFalse(self.opt("@agent_turn_end"))
        self.raw("done", self.hook("Stop", last_assistant_message="Committed."))
        self.assertIn(" p1 loud", self.opt("@agent_turn_end"))

    def test_a_subagent_does_not_rearm_it(self):
        self.write()
        was = self.finish()
        self.write(FEEDBACK)
        self.tool(agent="a4f9c2e1d0b3")
        self.assertEqual(self.state(), was)

    def test_an_api_request_log_is_not_a_turn_mark(self):
        self.write()
        was = self.finish()
        self.write(TURN_END, {"type": "api-request-blob", "message": FEEDBACK["message"]})
        self.tool()
        self.assertEqual(self.state(), was)

    def test_no_transcript_stays_finished(self):
        was = self.finish()
        self.assertFalse(self.tp.exists())
        self.tool()
        self.assertEqual(self.state(), was)


if __name__ == "__main__":
    unittest.main()
