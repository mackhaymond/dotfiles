"""ASKER GATE: @agent_ask_from and the heartbeat's needs-input re-arm.

PostToolUse (the `heartbeat` mode) fires for every agent in a Claude session,
subagents included. Until 2026-10-21 any of them re-armed `running` over the
main agent's open question: a background subagent's tool call turned an
AskUserQuestion's yellow into "working" while the question was still on
screen. Now needs-approval records who asked (@agent_ask_from: the payload's
agent_id, or "main") and, while the window is needs-input, the heartbeat
re-arms only for that agent - found by peeking at the head of the payload,
where Claude Code puts agent_id (before hook_event_name and the megabytes of
tool_response).

Drives the real script against a real, private tmux server (`tmux -L
<unique> -f /dev/null`, killed and its socket removed in tearDown), with the
same thin wrapper and stubs as test_agent_state_notice: no client is ever
attached, `#{window_active_clients}` answers $FAKE_ACTIVE_CLIENTS, lsappinfo
and notifyutil are stubbed. Payloads are built in Claude Code's own key
order and compact separators (JSON.stringify), see payload().
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

import test_agent_state_notice as sn

REAL_TMUX = sn.REAL_TMUX
MAIN = None   # payload(agent=MAIN): the main agent (no agent_id key at all)


def payload(event, agent=MAIN, compact=True, **rest):
    """A hook payload as Claude Code 2.1.306 serializes it: the base fields
    (wu()) first - agent_id among them, only for a subagent - then
    hook_event_name and the event's own fields."""
    d = {"session_id": "11111111-2222-3333-4444-555555555555",
         "transcript_path": "/Users/x/.claude/projects/-work/11111111.jsonl",
         "cwd": "/work/project",
         "permission_mode": "default"}
    if agent is not MAIN:
        d["agent_id"] = agent
        d["agent_type"] = "general-purpose"
    d["hook_event_name"] = event
    d.update(rest)
    return json.dumps(d, separators=(",", ":")) if compact else json.dumps(d, indent=2)


def post_tool_use(agent=MAIN, tool="Bash", tool_input=None, response="ok", **kw):
    return payload("PostToolUse", agent, tool_name=tool,
                   tool_input=tool_input if tool_input is not None else {"command": "ls"},
                   tool_response=response, tool_use_id="toolu_01", duration_ms=12, **kw)


def ask(agent=MAIN):
    return payload("PermissionRequest", agent, tool_name="AskUserQuestion",
                   tool_input={"questions": [{"question": "Which branch?", "options": []}]})


def perm(agent=MAIN, command="git push"):
    return payload("PermissionRequest", agent, tool_name="Bash", tool_input={"command": command})


def notification():
    # Claude builds the Notification without an agent context: never an agent_id.
    return payload("Notification", MAIN, message="Claude needs your permission to use Bash",
                   notification_type="permission_prompt")


@unittest.skipUnless(REAL_TMUX, "tmux executable required")
class AskerGateTests(sn.IndicatorNoticeTests):
    # The harness only (private server, stubs, env), not its test methods.
    for _n in [n for n in dir(sn.IndicatorNoticeTests) if n.startswith("test_")]:
        locals()[_n] = None
    del _n

    def setUp(self):
        super().setUp()
        # `running` composes a title, whose detached condenser would call the
        # real copilot (it unpacks itself into $HOME): a stub that fails.
        sn.write_exe(self.bin / "copilot", "#!/bin/sh\nexit 1\n")

    def raw(self, mode, stdin):
        r = subprocess.run(["bash", str(self.script), mode, "claude"], input=stdin, text=True,
                           capture_output=True, env=self.env, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def state(self):
        return self.opt("@agent_state")

    def start_turn(self):
        self.raw("running", payload("UserPromptSubmit", prompt="main task", prompt_id="p1"))
        self.assertEqual(self.state(), "running")

    # --- the reported bug ---

    def test_main_question_survives_a_subagents_heartbeat(self):
        self.start_turn()
        self.raw("needs-approval", ask())
        self.assertEqual(self.state(), "needs-input")
        self.assertEqual(self.opt("@agent_detail_kind"), "ask")
        self.assertEqual(self.opt("@agent_ask_from"), "main")
        since = self.opt("@agent_since")
        # A background subagent keeps working: its PostToolUse, with a
        # tool_response far past the 8 KB peek and the 64 KB pipe buffer.
        self.raw("heartbeat", post_tool_use("a4f9c2e1d0b3", response="x" * 300_000))
        self.assertEqual(self.state(), "needs-input")
        self.assertEqual(self.opt("@agent_since"), since)
        self.assertEqual(self.opt("@agent_ask_from"), "main")
        # The answer: AskUserQuestion's own PostToolUse, from the main agent.
        self.raw("heartbeat", post_tool_use(tool="AskUserQuestion", tool_input={"questions": []}))
        self.assertEqual(self.state(), "running")
        self.assertFalse(self.has_opt("@agent_ask_from"))

    def test_main_heartbeat_rearms_its_own_gate(self):
        self.start_turn()
        self.raw("needs-approval", perm())
        self.raw("heartbeat", post_tool_use(response="y" * 50_000))
        self.assertEqual(self.state(), "running")
        self.assertFalse(self.has_opt("@agent_ask_from"))

    # --- a subagent's own gate ---

    def test_subagent_gate_rearms_only_for_that_subagent(self):
        self.start_turn()
        self.raw("needs-approval", perm("aaaa1111"))
        self.assertEqual(self.state(), "needs-input")
        self.assertEqual(self.opt("@agent_ask_from"), "aaaa1111")
        self.raw("heartbeat", post_tool_use("bbbb2222"))
        self.assertEqual(self.state(), "needs-input", "another subagent's tool call")
        self.raw("heartbeat", post_tool_use())
        self.assertEqual(self.state(), "needs-input", "the main agent's tool call")
        self.raw("heartbeat", post_tool_use("aaaa1111"))
        self.assertEqual(self.state(), "running")
        self.assertFalse(self.has_opt("@agent_ask_from"))

    def test_notification_twin_keeps_the_askers_id(self):
        self.start_turn()
        self.raw("needs-approval", perm("aaaa1111"))
        self.raw("needs-approval", notification())
        self.assertEqual(self.opt("@agent_ask_from"), "aaaa1111")
        self.assertEqual(self.opt("@agent_detail_kind"), "perm")
        self.raw("heartbeat", post_tool_use("bbbb2222"))
        self.assertEqual(self.state(), "needs-input")

    def test_permission_request_losing_the_race_still_records_the_asker(self):
        # The twin lands first: a gate with no asker yet (the old rule).
        self.start_turn()
        self.raw("needs-approval", notification())
        self.assertEqual(self.state(), "needs-input")
        self.assertFalse(self.has_opt("@agent_ask_from"))
        # Its PermissionRequest lands second, on the standing gate: recorded,
        # and from then on only that subagent re-arms.
        self.raw("needs-approval", perm("cccc3333"))
        self.assertEqual(self.opt("@agent_ask_from"), "cccc3333")
        self.assertEqual(self.opt("@agent_detail_kind"), "perm")
        self.raw("heartbeat", post_tool_use("bbbb2222"))
        self.raw("heartbeat", post_tool_use())
        self.assertEqual(self.state(), "needs-input")
        self.raw("heartbeat", post_tool_use("cccc3333"))
        self.assertEqual(self.state(), "running")

    def test_notification_alone_keeps_the_old_rule(self):
        self.start_turn()
        self.raw("needs-approval", notification())
        self.raw("heartbeat", post_tool_use("bbbb2222"))
        self.assertEqual(self.state(), "running")

    # --- the main agent's Stop and a subagent's gate ---

    def test_main_stop_leaves_a_subagents_gate_standing(self):
        self.start_turn()
        self.raw("needs-approval", perm("aaaa1111", command="make deploy"))
        since = self.opt("@agent_since")
        self.raw("done", payload("Stop", last_assistant_message="Main is done."))
        self.assertEqual(self.state(), "needs-input")
        self.assertEqual(self.opt("@agent_since"), since)
        self.assertEqual(self.opt("@agent_ask_from"), "aaaa1111")
        self.assertEqual((self.opt("@agent_detail_kind"), self.opt("@agent_detail")),
                         ("perm", "Bash make deploy"))
        # Held like background work: the turn's result and owner recorded.
        self.assertTrue(self.opt("@agent_turn_result").endswith(" loud"))
        self.assertEqual(self.opt("@agent_bg_owner"), "user")
        # Another agent's tool call still leaves it; the asker's re-arms the
        # running that a held Stop stands on (the watcher drains it later).
        self.raw("heartbeat", post_tool_use("bbbb2222"))
        self.assertEqual(self.state(), "needs-input")
        self.raw("heartbeat", post_tool_use("aaaa1111"))
        self.assertEqual(self.state(), "running")
        self.assertFalse(self.has_opt("@agent_ask_from"))

    def test_held_stop_detail_returns_when_the_gate_ends(self):
        # Its reply waits in @agent_held_detail while the gate shows; the
        # asker's answer lands the window where the held Stop would have.
        self.start_turn()
        self.raw("needs-approval", perm("aaaa1111", command="make deploy"))
        self.raw("done", payload("Stop", last_assistant_message="Main is done."))
        self.assertEqual(self.opt("@agent_detail_kind"), "perm")
        self.assertEqual(self.opt("@agent_held_detail"), "done\tMain is done.")
        self.raw("heartbeat", post_tool_use("aaaa1111"))
        self.assertEqual(self.state(), "running")
        self.assertEqual((self.opt("@agent_detail_kind"), self.opt("@agent_detail")),
                         ("done", "Main is done."))
        self.assertFalse(self.has_opt("@agent_held_detail"))

    def test_held_stop_keeps_an_agent_turns_notify_user_reason(self):
        # An agent-originated turn that flagged Mack (notify_user) ends while
        # a subagent's gate stands: its reason is kept for when the gate ends,
        # also when the gate was seen first (idle + pending, consumed by any
        # heartbeat).
        self.start_turn()
        self.set_opt("@agent_turn_origin", "agent p9 peer")
        self.set_opt("@agent_for_user", "1792600000 Please review the deploy")
        self.raw("needs-approval", perm("aaaa1111"))
        self.raw("done", payload("Stop", prompt_id="p9", last_assistant_message="reply to peer"))
        self.assertEqual(self.state(), "needs-input")
        self.assertEqual(self.opt("@agent_held_detail"), "done\tPlease review the deploy")
        self.clear_current()
        self.assertEqual(self.state(), "idle")
        self.raw("heartbeat", post_tool_use("aaaa1111"))
        self.assertEqual(self.state(), "running")
        self.assertEqual(self.opt("@agent_detail"), "Please review the deploy")
        self.assertFalse(self.has_opt("@agent_held_detail"))

    def test_a_new_prompt_drops_the_held_detail(self):
        self.start_turn()
        self.raw("needs-approval", perm("aaaa1111"))
        self.raw("done", payload("Stop", last_assistant_message="Main is done."))
        self.raw("running", payload("UserPromptSubmit", prompt="next", prompt_id="p2"))
        self.assertFalse(self.has_opt("@agent_held_detail"))
        self.assertEqual(self.opt("@agent_detail"), "next")

    # --- subagent-stop (SubagentStop; not wired in settings.json yet) ---

    def test_subagent_stop_ends_only_its_own_gate(self):
        self.start_turn()
        self.raw("needs-approval", perm("aaaa1111"))
        self.raw("done", payload("Stop", last_assistant_message="Main is done."))
        self.raw("subagent-stop", payload("SubagentStop", "bbbb2222"))
        self.assertEqual(self.state(), "needs-input")
        self.raw("subagent-stop", payload("SubagentStop", MAIN))
        self.assertEqual(self.state(), "needs-input")
        self.raw("subagent-stop", payload("SubagentStop", "aaaa1111"))
        self.assertEqual(self.state(), "running")
        self.assertEqual(self.opt("@agent_detail"), "Main is done.")
        self.assertFalse(self.has_opt("@agent_ask_from"))

    def test_subagent_stop_is_inert_off_its_gate(self):
        self.start_turn()
        self.raw("failed", payload("StopFailure", error="overloaded"))
        self.raw("subagent-stop", payload("SubagentStop", "aaaa1111"))
        self.assertEqual(self.state(), "failed")
        self.raw("needs-approval", ask())   # main's own gate
        self.raw("subagent-stop", payload("SubagentStop", "aaaa1111"))
        self.assertEqual(self.state(), "needs-input")
        self.clear_current()
        self.raw("subagent-stop", payload("SubagentStop", "aaaa1111"))
        self.assertEqual(self.state(), "idle")
        self.assertTrue(self.opt("@agent_pending"))

    def test_main_stop_ends_its_own_or_an_unrecorded_gate(self):
        for raise_gate in (lambda: self.raw("needs-approval", ask()),
                           lambda: self.raw("needs-approval", notification())):
            self.start_turn()
            raise_gate()
            self.raw("done", payload("Stop", last_assistant_message="ok"))
            self.assertEqual(self.state(), "done")
            self.assertFalse(self.has_opt("@agent_ask_from"))
            self.assertFalse(self.has_opt("@agent_bg_owner"))

    # --- where the id is read from ---

    def test_only_the_top_level_agent_id_counts(self):
        self.start_turn()
        self.raw("needs-approval", perm())
        # The main agent's tool call whose INPUT object has its own agent_id
        # key, and whose response string quotes one: still the main agent.
        self.raw("heartbeat", post_tool_use(tool="mcp__x__poke", tool_input={"agent_id": "zzzz"},
                                            response='{"agent_id":"qqqq"}'))
        self.assertEqual(self.state(), "running")

    def test_whitespace_in_the_payload(self):
        self.start_turn()
        self.raw("needs-approval", perm())
        self.raw("heartbeat", payload("PostToolUse", "dddd4444", compact=False, tool_name="Bash",
                                      tool_input={"command": "ls"}, tool_response="ok"))
        self.assertEqual(self.state(), "needs-input")

    def test_legacy_needs_input_without_an_asker_keeps_the_old_rule(self):
        # A window that went yellow before this deploy has no @agent_ask_from:
        # any tool call re-arms, as before (a gate nobody can end would stand).
        self.start_turn()
        self.set_opt("@agent_state", "needs-input")
        self.raw("heartbeat", post_tool_use("eeee5555"))
        self.assertEqual(self.state(), "running")

    def test_empty_or_unreadable_stdin_is_main(self):
        self.start_turn()
        self.raw("needs-approval", perm())
        self.raw("heartbeat", "")
        self.assertEqual(self.state(), "running")

    # --- every other path, as before ---

    def test_leaving_needs_input_drops_the_asker(self):
        for leave in (lambda: self.clear_current(),
                      lambda: self.raw("running", payload("UserPromptSubmit", prompt="next", prompt_id="p2")),
                      lambda: self.raw("failed", payload("StopFailure", error="overloaded")),
                      lambda: self.raw("idle", payload("SessionStart", source="resume"))):
            self.start_turn()
            self.raw("needs-approval", perm("aaaa1111"))
            self.assertEqual(self.opt("@agent_ask_from"), "aaaa1111")
            leave()
            self.assertNotEqual(self.state(), "needs-input")
            self.assertFalse(self.has_opt("@agent_ask_from"))
        self.raw("needs-approval", perm("aaaa1111"))
        self.raw("clear", payload("SessionEnd", reason="other"))
        self.assertFalse(self.has_opt("@agent_ask_from"))

    def test_running_failed_and_pending_are_unchanged(self):
        self.start_turn()
        self.raw("heartbeat", post_tool_use("ffff6666"))
        self.assertEqual(self.state(), "running")
        self.raw("failed", payload("StopFailure", error="overloaded"))
        self.raw("heartbeat", post_tool_use("ffff6666"))
        self.assertEqual(self.state(), "running")
        # A seen prompt (idle + @agent_pending) re-arms on the next heartbeat,
        # whoever's: the pending path never reads the payload.
        self.raw("needs-approval", ask())
        self.clear_current()
        self.assertEqual(self.state(), "idle")
        self.assertTrue(self.opt("@agent_pending"))
        self.raw("heartbeat", post_tool_use("ffff6666"))
        self.assertEqual(self.state(), "running")
        self.assertFalse(self.has_opt("@agent_pending"))
        # Bare idle/done stay inert.
        self.raw("done", payload("Stop", last_assistant_message="ok"))
        self.raw("heartbeat", post_tool_use())
        self.assertEqual(self.state(), "done")

    def test_viewed_prompt_writes_no_asker(self):
        # Lands in front of Mack's eyes: discharged to idle + pending at once.
        self.start_turn()
        env = dict(self.env, FAKE_FRONT="wez", FAKE_ACTIVE_CLIENTS="1")
        r = subprocess.run(["bash", str(self.script), "needs-approval", "claude"], input=ask("aaaa1111"),
                           text=True, capture_output=True, env=env, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state(), "idle")
        self.assertFalse(self.has_opt("@agent_ask_from"))

    def test_heartbeat_drains_a_huge_payload(self):
        # Written the way Claude writes it: the whole payload, then EOF. The
        # drain must really read the pipe (`<&0`; the bare `( cat & )` that
        # stood before read /dev/null), so the writer never gets EPIPE - and
        # the hook must not wait for it. 4 MB is far past the 64 KB pipe
        # buffer, so a hook that exits without draining always breaks the
        # pipe under the write. Both the running path and the needs-input
        # path (which reads 8 KB first) are checked.
        self.start_turn()
        big = post_tool_use("ffff6666", response="z" * 4_000_000).encode()
        for setup in (lambda: None, lambda: self.raw("needs-approval", perm())):
            setup()
            t = time.monotonic()
            p = subprocess.Popen(["bash", str(self.script), "heartbeat", "claude"],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, env=self.env)
            try:
                p.stdin.write(big)
                p.stdin.close()
            except BrokenPipeError:
                self.fail("the hook left its stdin undrained (EPIPE on the writer)")
            finally:
                self.assertEqual(p.wait(timeout=15), 0)
            self.assertLess(time.monotonic() - t, 5)


if __name__ == "__main__":
    unittest.main()
