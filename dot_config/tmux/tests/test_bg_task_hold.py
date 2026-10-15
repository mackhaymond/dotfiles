"""agent-tab-watcher.sh: a background task the ended turn waits on holds the
tab running (BACKGROUND TASKS HOLD A TAB RUNNING).

A claude window left `running` by the Stop hook (agent-bg-pending said a
background task is still out) reads status "shell" in its session file. The
watcher's un-stick would idle it after 3 ticks; with agent-bg-pending still
answering "out" it must hold the tab running and light the gear instead.
Reuses test_agent_jump_watcher's fake-tmux harness; agent-bg-pending is a stub
that logs its argv and exits with $FAKE_BG_RC. Run with unittest.
"""
import json
import subprocess
import time
import unittest

import test_agent_jump_watcher as jw   # module only, for the shared fakes

CLAUDE_TTY = "/dev/ttys902"
PROCS = "ttys902 4242 claude\n"
SID = "11111111-aaaa-bbbb-cccc-000000000001"
CWD = "/Users/x/proj"

STUB = """#!/bin/sh
echo "$*" >> "$FAKE_BG_LOG"
exit "${FAKE_BG_RC:-1}"
"""


class BgTaskHoldTests(unittest.TestCase):
    def setUp(self):
        self.f = jw.FakeEnv(windows={}, panes=[])
        self.f.procs.write_text(PROCS)
        sessions = self.f.home / ".claude/sessions"
        sessions.mkdir(parents=True)
        (sessions / "4242.json").write_text(json.dumps(
            {"pid": 4242, "sessionId": SID, "cwd": CWD, "status": "shell"}, separators=(",", ":")))
        self.transcript = self.f.home / ".claude/projects/-Users-x-proj" / (SID + ".jsonl")
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text('{"type":"user"}\n')
        stub = self.f.home / ".local/bin/agent-bg-pending"
        stub.parent.mkdir(parents=True)
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.bg_log = self.f.dir / "bg.log"
        now = int(time.time())
        st = self.f.read()
        st["windows"] = {"@1": jw.W("main", 1, **{
            "@agent_state": "running", "@agent_kind": "claude",
            "@agent_since": "%d running" % (now - 30)})}
        st["panes"] = [{"window": "@1", "tty": CLAUDE_TTY}]
        self.f.state.write_text(json.dumps(st))

    def tearDown(self):
        self.f.close()

    def ticks(self, n, rc):
        r = self.f.run("agent-tab-watcher.sh", timeout=10 + 2 * n,
                       AGENT_TAB_WATCHER_MAX_TICKS=str(n),
                       FAKE_BG_RC=str(rc), FAKE_BG_LOG=str(self.bg_log))
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.f.read()["windows"]["@1"]["opts"]

    def asks(self):
        return self.bg_log.read_text().splitlines() if self.bg_log.exists() else []

    def test_task_out_holds_running_with_gear(self):
        o = self.ticks(5, rc=0)
        self.assertEqual(o["@agent_state"], "running")
        self.assertEqual(o.get("@agent_workflow"), "1")
        # Asked with the session's transcript, id and cwd - and cached: the
        # transcript didn't move, so once for all five ticks.
        self.assertEqual(self.asks(), ["%s %s %s" % (self.transcript, SID, CWD)])

    def test_nothing_out_keeps_the_old_unstick(self):
        o = self.ticks(5, rc=1)
        self.assertEqual(o["@agent_state"], "idle")
        self.assertNotIn("@agent_workflow", o)

    def test_task_finishes_then_idles(self):
        # ONE watcher run: the verdict is cached, so only the transcript
        # moving (the task's notification landing) can make it ask again
        # inside these 9 ticks (the cache's own expiry is 10).
        rc_file = self.f.dir / "bg.rc"
        rc_file.write_text("0")
        stub = self.f.home / ".local/bin/agent-bg-pending"
        stub.write_text('#!/bin/sh\necho "$*" >> "$FAKE_BG_LOG"\nexit "$(cat "$FAKE_BG_RC_FILE")"\n')
        env = dict(self.f.env(AGENT_TAB_WATCHER_MAX_TICKS="9",
                              FAKE_BG_LOG=str(self.bg_log), FAKE_BG_RC_FILE=str(rc_file)))
        p = subprocess.Popen(["bash", str(self.f.scripts / "agent-tab-watcher.sh")], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 15
        while len(self.asks()) < 1 and time.monotonic() < deadline:
            time.sleep(.1)
        time.sleep(2.5)                     # held across ticks on the cached yes
        self.assertEqual(self.f.read()["windows"]["@1"]["opts"]["@agent_state"], "running")
        rc_file.write_text("1")
        with self.transcript.open("a") as fh:
            fh.write('{"type":"queue-operation"}\n')
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        o = self.f.read()["windows"]["@1"]["opts"]
        self.assertEqual(o["@agent_state"], "idle")
        self.assertNotIn("@agent_workflow", o)
        self.assertEqual(len(self.asks()), 2, self.asks())

    def test_busy_session_never_asks(self):
        (self.f.home / ".claude/sessions/4242.json").write_text(json.dumps(
            {"pid": 4242, "sessionId": SID, "cwd": CWD, "status": "busy"}, separators=(",", ":")))
        o = self.ticks(4, rc=0)
        self.assertEqual(o["@agent_state"], "running")
        self.assertEqual(self.asks(), [])


if __name__ == "__main__":
    unittest.main()
