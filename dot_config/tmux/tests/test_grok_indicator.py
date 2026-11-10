"""agent-tab-indicator.sh driven as Grok Build's hooks drive it: `<mode> grok`.

Covers the @agent_kind whitelist, the GROK_HOOK_EVENT compat-drop (Grok
running Claude's own hooks as `<mode> claude`), Grok's payload quirks
(subagentType events inert, the session-end Stop's reason, promptId as the
turn id, SessionStart sources new/load), extract_summary's grok branch
(summary.json, never updates.jsonl), the rate_limit split (429 = a limit,
503/529 capacity = failed), backgroundTasks holding `running`, `interrupt`
and the new `settle` mode.

Two harnesses: the fake tmux of test_codex_indicator (as
test_opencode_indicator), and test_agent_limit's private-server one (a real
`tmux -L <unique> -f /dev/null`, killed in cleanup) wherever a real format
read matters. Both run on a PATH of fakes plus bash/jq/python3 and the
system dirs only, so no title condenser (ai-exec) can ever be found.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

import test_agent_limit as tl
from test_codex_indicator import FAKE_PS, FAKE_TMUX, REAL_TOOLS, SOURCE, SYSTEM_PATH

FAILING_COPILOT = "#!/bin/sh\nexit 1\n"
SID = "01a19d40-92df-7fb3-b8b9-334d0ac836b8"
LIMIT_429 = ("Turn failed: API error (status 429 Too Many Requests): subscription:free-usage-exceeded; "
             "resets over a rolling 24-hour window")
# A real one, verbatim (its token counts must not read as a capacity code).
REAL_429 = ("API error (status 429 Too Many Requests): subscription:free-usage-exhausted: You've used all the "
            "included free usage for model grok-4.7 for now. Usage resets over a rolling 24-hour window — "
            "tokens (actual/limit): 617133/600000. Upgrade to a Grok subscription for higher limits: "
            "https://grok.com/supergrok")


def tools_dir(root):
    d = root / "tools"
    d.mkdir(exist_ok=True)
    for name, real in REAL_TOOLS.items():
        if real and not (d / name).exists():
            (d / name).symlink_to(real)
    return d


class GrokIndicatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="grok-indicator-test-")
        self.root = Path(self.tmp.name)
        self.bin = self.root / ".local/bin"
        self.bin.mkdir(parents=True)
        self.state = self.root / "tmux.json"
        self.state.write_text(json.dumps({"calls": [], "windows": {"@7": {}, "@99": {"@agent_state": "unrelated"}}}))
        self.script = self.root / "agent-tab-indicator.sh"
        shutil.copyfile(SOURCE, self.script)
        for name, source in [("tmux", FAKE_TMUX), ("copilot", FAILING_COPILOT), ("ps", FAKE_PS)]:
            path = self.bin / name
            path.write_text(source)
            path.chmod(0o755)
        # Grok runs its hooks as children of the TUI: they inherit the pane's
        # own TMUX/TMUX_PANE, and Grok's runner sets GROK_HOOK_EVENT.
        self.env = dict(os.environ, HOME=str(self.root),
                        PATH=":".join([str(self.bin), str(tools_dir(self.root)), SYSTEM_PATH]),
                        TMPDIR=str(self.root), TMUX="/fake/owned,12,0", TMUX_PANE="%42",
                        FAKE_TMUX_STATE=str(self.state), AGENT_TAB_CONDENSE_MODEL="",
                        AGENT_STATE_NOTICE="", GROK_HOOK_EVENT="Stop",
                        FAKE_FORK_LOG=str(self.root / "forks.log"))
        for key in ("AGENT_TAB_SOCKET", "AGENT_TAB_OWNER_SESSION", "AGENT_TAB_OWNER_TOKEN",
                    "AGENT_TAB_OWNER_BINDING", "AGENT_TAB_OWNER_RECORD", "CODEX_THREAD_ID", "CODEX_SANDBOX"):
            self.env.pop(key, None)

    def tearDown(self):
        time.sleep(.1)
        self.tmp.cleanup()

    def run_indicator(self, mode, agent="grok", env=None, **payload):
        data = dict(session_id=SID, sessionId=SID, cwd="/work/project", **payload)
        result = subprocess.run(["bash", str(self.script), mode, agent], input=json.dumps(data),
                                text=True, capture_output=True, env=env or self.env, cwd=self.root, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def hook(self, mode, agent="grok", env=None, **payload):
        self.run_indicator(mode, agent, env=env, **payload)
        return self.window()

    def window(self):
        return json.loads(self.state.read_text())["windows"]["@7"]

    def put(self, **options):
        state = json.loads(self.state.read_text())
        state["windows"]["@7"].update(options)
        self.state.write_text(json.dumps(state))

    # ------------------------------------------------------------ whitelist
    def test_lifecycle_writes_grok_kind_and_details(self):
        w = self.hook("idle", hook_event_name="SessionStart", source="new")
        self.assertEqual((w["@agent_state"], w["@agent_kind"]), ("idle", "grok"))
        self.assertEqual(w["@agent_summary"], "project/New Session")
        w = self.hook("running", hook_event_name="UserPromptSubmit", promptId="p-1",
                      prompt="Fix the roast bot\nsecond line")
        self.assertEqual((w["@agent_state"], w["@agent_kind"]), ("running", "grok"))
        self.assertEqual((w["@agent_detail_kind"], w["@agent_detail"]), ("run", "Fix the roast bot"))
        # Grok's permission Notification names no tool: the note path.
        w = self.hook("needs-approval", hook_event_name="Notification", notification_type="permission_prompt",
                      message="use_tool")
        self.assertEqual(w["@agent_state"], "needs-input")
        self.assertEqual((w["@agent_detail_kind"], w["@agent_detail"]), ("perm", "use_tool"))
        self.assertEqual(self.hook("heartbeat", hook_event_name="PostToolUse")["@agent_state"], "running")
        w = self.hook("done", hook_event_name="Stop", promptId="p-1", reason="end_turn",
                      lastAssistantMessage="Done, 42 tests pass.", last_assistant_message="Done, 42 tests pass.")
        self.assertEqual((w["@agent_state"], w["@agent_kind"]), ("done", "grok"))
        self.assertEqual((w["@agent_detail_kind"], w["@agent_detail"]), ("done", "Done, 42 tests pass."))
        w = self.hook("failed", hook_event_name="StopFailure", error="server_error", errorDetails="stream reset")
        self.assertEqual((w["@agent_state"], w["@agent_detail"]), ("failed", "server_error: stream reset"))
        # (@thread_at, Mack's last prompt, belongs to the window: it stays.)
        self.assertEqual(set(self.hook("clear", hook_event_name="SessionEnd", reason="exit")), {"@agent_seen_at", "@thread_at"})
        self.assertEqual(json.loads(self.state.read_text())["windows"]["@99"], {"@agent_state": "unrelated"})

    def test_usage_names_grok_and_settle(self):
        err = self.run_indicator("bogus").stderr
        self.assertIn("claude|codex|opencode|grok", err)
        self.assertIn("|settle|", err)

    # ------------------------------------------------------------ compat-drop
    def test_claude_hook_run_by_grok_is_inert(self):
        # Grok's [compat.claude] hooks run ~/.claude/settings.json's
        # `<mode> claude` with a Grok payload: GROK_HOOK_EVENT drops them.
        for mode, extra in [("idle", {"source": "new"}), ("running", {"prompt": "x", "promptId": "p-1"}),
                            ("needs-approval", {"message": "use_tool"}), ("done", {"reason": "end_turn"}),
                            ("failed", {"error": "rate_limit", "error_details": LIMIT_429}), ("clear", {})]:
            with self.subTest(mode=mode):
                self.assertEqual(self.hook(mode, agent="claude", **extra), {})
        # Grok's own hooks carry the same variable, and paint.
        self.assertEqual(self.hook("running", prompt="x")["@agent_kind"], "grok")
        # A real Claude hook (no GROK_HOOK_EVENT) is untouched by the rule.
        env = {k: v for k, v in self.env.items() if k != "GROK_HOOK_EVENT"}
        self.assertEqual(self.hook("running", agent="claude", env=env, prompt="y")["@agent_kind"], "claude")

    # ------------------------------------------------------------ payload quirks
    def test_subagent_events_are_inert_for_the_main_tab(self):
        self.hook("running", promptId="p-1", prompt="main task")
        before = self.window()
        for mode, extra in [("running", {"prompt": "sub task"}), ("done", {"reason": "end_turn"}),
                            ("failed", {"error": "server_error"}), ("interrupt", {}), ("settle", {}),
                            ("idle", {"source": "new"}), ("clear", {"reason": "exit"}), ("heartbeat", {})]:
            with self.subTest(mode=mode):
                self.assertEqual(self.hook(mode, subagentType="explore", **extra), before)

    def test_only_an_end_turn_stop_is_a_finish(self):
        self.hook("running", promptId="p-1", prompt="work")
        before = self.window()
        for reason in ("shutdown", "channel_closed"):
            with self.subTest(reason=reason):
                self.assertEqual(self.hook("done", promptId="p-1", reason=reason,
                                           last_assistant_message="bye"), before)
        self.assertEqual(self.hook("done", promptId="p-1", reason="end_turn")["@agent_state"], "done")
        self.hook("running", promptId="p-2", prompt="more")
        self.assertEqual(self.hook("done", promptId="p-2")["@agent_state"], "done", "no reason: a finish")

    def test_interrupt_discharges_but_keeps_label(self):
        self.hook("running", prompt="long task")
        self.put(**{"@agent_summary": "project/Current title", "@agent_pending": "123"})
        w = self.hook("interrupt", hook_event_name="StopCancelled")
        self.assertEqual(w["@agent_state"], "idle")
        self.assertEqual(w["@agent_summary"], "project/Current title")
        self.assertNotIn("@agent_pending", w)
        self.assertEqual(w["@agent_kind"], "grok")

    # ------------------------------------------------------------ extract_summary
    def test_title_from_summary_json_never_updates_jsonl(self):
        sdir = self.root / ".grok/sessions/%2Fwork%2Fproject" / SID
        sdir.mkdir(parents=True)
        updates = sdir / "updates.jsonl"
        # Claude-shaped title rows in Grok's updates.jsonl must never be read.
        updates.write_text('{"type":"ai-title","aiTitle":"Wrong title"}\n'
                           '{"type":"custom-title","customTitle":"Wrong custom"}\n')
        tp = dict(transcript_path=str(updates))
        w = self.hook("running", prompt="Fix the roast bot", **tp)
        self.assertEqual(w["@agent_summary"], "project/Fix the roast bot")
        (sdir / "summary.json").write_text(json.dumps({"generated_title": "Roast bot fixes",
                                                       "session_summary": "x", "title_is_manual": False}))
        self.assertEqual(self.hook("done", reason="end_turn", **tp)["@agent_summary"], "project/Roast bot fixes")
        (sdir / "summary.json").write_text(json.dumps({"generated_title": "my-renamed-task",
                                                       "title_is_manual": True}))
        w = self.hook("running", prompt="next", **tp)
        self.assertEqual(w["@agent_summary"], "project/my renamed task")
        self.assertEqual(w["@agent_summary_cond"], "1")

    # ------------------------------------------------------------ nesting
    def test_grok_in_another_agents_tool_shell_is_inert(self):
        parent = os.getppid()
        grand = subprocess.run(["/bin/ps", "-o", "ppid=", "-p", str(parent)],
                               capture_output=True, text=True).stdout.strip()
        if not grand.isdigit() or int(grand) <= 1:
            self.skipTest("this test runner has no grandparent process to relabel")
        for me, above, agent in [("grok", "claude", "grok"), ("grok-macos-aarch64", "codex", "grok"),
                                 ("2.1.308", "grok", "claude")]:
            with self.subTest(me=me, above=above):
                env = dict(self.env, FAKE_PS_COMM=f"{os.getpid()}:{me},{parent}:zsh,{grand}:{above}")
                if agent == "claude":
                    env.pop("GROK_HOOK_EVENT")
                self.assertEqual(self.hook("running", agent=agent, env=env, prompt="child"), {})


class GrokServerTests(tl.LimitHookHarness):
    """The private-server harness: real formats, if-shell and display-message."""

    def setUp(self):
        super().setUp()
        self.env.update(PATH=":".join([str(self.bin), str(tools_dir(self.root)), SYSTEM_PATH]),
                        GROK_HOOK_EVENT="Stop")

    def gk(self, mode, **payload):
        data = dict(session_id=SID, sessionId=SID, cwd="/work/project", **payload)
        return self.hook(mode, "grok", payload=data)

    def prompt_g(self, pid, text="build it"):
        return self.gk("running", hook_event_name="UserPromptSubmit", promptId=pid, prompt=text)

    def fail_g(self, pid, details, key="error_details"):
        return self.gk("failed", hook_event_name="StopFailure", promptId=pid, error="rate_limit",
                       **{key: details})

    def test_prompt_id_is_the_turn_id(self):
        self.prompt_g("p-1")
        self.assertEqual(self.opt("@agent_turn_origin"), "user p-1 -")
        self.gk("done", hook_event_name="Stop", promptId="p-1", reason="end_turn",
                last_assistant_message="Built.")
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertEqual(self.opt("@agent_turn_result"), "p-1 loud")
        self.assertEqual(self.opt("@agent_kind"), "grok")

    def test_429_rate_limit_marks_a_grok_limit(self):
        for key in ("error_details", "errorDetails"):
            with self.subTest(key=key):
                self.prompt_g("p-1")
                self.fail_g("p-1", LIMIT_429, key)
                self.assertEqual(self.opt("@agent_state"), "paused")
                e = self.opt("@agent_paused")
                self.assertRegex(e, r"^\d+$")
                self.assertEqual(self.limit(), ["0", "grok", e])
                self.assertEqual(self.opt("@agent_detail_kind"), "limit")
                self.assertEqual(self.opt("@agent_detail"), "Grok limit · resets over 24h")
                self.assertEqual(self.opt("@agent_turn_result"), "p-1 limited 0")
                self.assertEqual(self.opt("@agent_kind"), "grok")
                self.n_marks = getattr(self, "n_marks", 0) + 1
                self.assertEqual(self.pause_calls(self.n_marks)[-1], "limit-add " + self.win)
                # The next clean finish ends it, as for Claude.
                self.prompt_g("p-2", "try again")
                self.gk("done", promptId="p-2", reason="end_turn")
                self.assertEqual(self.opt("@agent_state"), "done")
                self.assertFalse(self.has("@agent_limit"))

    def test_real_429_in_details_or_only_in_the_final_message(self):
        for payload in (dict(errorDetails=REAL_429, error_details=REAL_429),
                        dict(last_assistant_message="Turn failed: " + REAL_429,
                             lastAssistantMessage="Turn failed: " + REAL_429),
                        dict(lastAssistantMessage="Turn failed: " + REAL_429)):
            with self.subTest(keys=sorted(payload)):
                self.prompt_g("p-1")
                self.gk("failed", hook_event_name="StopFailure", promptId="p-1", error="rate_limit", **payload)
                self.assertEqual(self.opt("@agent_state"), "paused")
                self.assertEqual(self.limit()[:2], ["0", "grok"])
                self.n_marks = getattr(self, "n_marks", 0) + 1
                self.pause_calls(self.n_marks)
                self.prompt_g("p-2", "go on")
                self.gk("done", promptId="p-2", reason="end_turn")

    def test_capacity_rate_limit_is_a_red_failure(self):
        for details in ("Turn failed: API error (status 503 Service Unavailable): model at capacity",
                        "status 529: overloaded", "The model is at capacity, try again", "", "something else"):
            with self.subTest(details=details):
                self.prompt_g("p-1")
                self.fail_g("p-1", details)
                self.assertEqual(self.opt("@agent_state"), "failed")
                self.assertEqual(self.opt("@agent_turn_result"), "p-1 loud")
                self.assertEqual(self.opt("@agent_detail_kind"), "fail")
                self.assertFalse(self.has("@agent_limit"))
                self.assertFalse(self.has("@agent_paused"))
        time.sleep(0.3)
        self.assertFalse(self.calls.exists())

    def age_running(self, secs=120):
        self.set_opt("@agent_since", "%d running" % (int(time.time()) - secs))

    def test_settle_moves_only_running_to_idle(self):
        self.prompt_g("p-1")
        self.set_opt("@agent_summary", "project/Kept")
        # A fresh turn: the idle_prompt may be the idle spell's before it.
        self.gk("settle", hook_event_name="Notification", notification_type="idle_prompt")
        self.assertEqual(self.opt("@agent_state"), "running")
        self.age_running(40)
        self.gk("settle", notification_type="idle_prompt")
        self.assertEqual(self.opt("@agent_state"), "running", "under 50 s")
        self.age_running()
        self.gk("settle", hook_event_name="Notification", notification_type="idle_prompt")
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assertEqual(self.opt("@agent_summary"), "project/Kept")
        for st in ("done", "needs-input", "failed", "paused", "idle"):
            with self.subTest(state=st):
                self.set_opt("@agent_state", st)
                self.set_opt("@agent_since", "%d %s" % (int(time.time()) - 120, st))
                self.gk("settle", notification_type="idle_prompt")
                self.assertEqual(self.opt("@agent_state"), st)
        # A Stop held running for background work is still waiting on it.
        self.set_opt("@agent_state", "running")
        self.age_running()
        self.set_opt("@agent_bg_owner", "user")
        self.gk("settle", notification_type="idle_prompt")
        self.assertEqual(self.opt("@agent_state"), "running")

    def test_settle_of_a_paused_window_lands_paused(self):
        self.prompt_g("p-1")
        self.age_running()
        self.set_opt("@agent_paused", str(int(time.time()) - 30))
        self.gk("settle", notification_type="idle_prompt")
        self.assertEqual(self.opt("@agent_state"), "paused")

    def test_settle_race_after_a_new_prompt_keeps_running(self):
        self.prompt_g("p-1")
        self.gk("done", promptId="p-1", reason="end_turn")
        self.prompt_g("p-2", "next")
        self.gk("settle", hook_event_name="Notification", notification_type="idle_prompt")
        self.assertEqual(self.opt("@agent_state"), "running")

    def test_late_cancel_or_failure_of_an_older_turn_is_inert(self):
        self.prompt_g("p-1")
        self.prompt_g("p-2", "next")
        for mode, extra in [("interrupt", dict(hook_event_name="StopCancelled", reason="user_interrupt",
                                               cancelledBy="user")),
                            ("interrupt", dict(hook_event_name="StopCancelled", reason="no_progress",
                                               cancelledBy="runtime")),
                            ("failed", dict(hook_event_name="StopFailure", error="server_error",
                                            error_details="boom")),
                            ("failed", dict(hook_event_name="StopFailure", error="rate_limit",
                                            error_details=REAL_429))]:
            with self.subTest(mode=mode, reason=extra.get("reason", extra.get("error"))):
                self.gk(mode, promptId="p-1", **extra)
                self.assertEqual(self.opt("@agent_state"), "running")
                self.assertEqual(self.opt("@agent_turn_origin"), "user p-2 -")
                self.assertFalse(self.has("@agent_limit"))
        # The running turn's own cancel still lands.
        self.gk("interrupt", hook_event_name="StopCancelled", promptId="p-2", cancelledBy="user")
        self.assertEqual(self.opt("@agent_state"), "idle")

    def test_late_cancel_or_failure_never_touches_the_newer_turns_state(self):
        # Ctrl+C on p-1, p-2 sent at once: whatever p-2 shows when p-1's
        # off-loop report lands - its prompt, its finish, its failure - stays.
        stale = [("interrupt", dict(hook_event_name="StopCancelled", cancelledBy="user")),
                 ("interrupt", dict(hook_event_name="StopCancelled", cancelledBy="runtime")),
                 ("failed", dict(hook_event_name="StopFailure", error="server_error", error_details="boom"))]
        self.prompt_g("p-1")
        self.prompt_g("p-2", "next")
        for state, enter in [("needs-input", lambda: self.gk("needs-approval", hook_event_name="Notification",
                                                             notification_type="permission_prompt",
                                                             message="use_tool")),
                             ("done", lambda: self.gk("done", promptId="p-2", reason="end_turn")),
                             ("failed", lambda: self.gk("failed", promptId="p-2", error="server_error"))]:
            enter()
            self.assertEqual(self.opt("@agent_state"), state)
            for mode, extra in stale:
                with self.subTest(state=state, mode=mode, by=extra.get("cancelledBy")):
                    self.gk(mode, promptId="p-1", **extra)
                    self.assertEqual(self.opt("@agent_state"), state)

    def test_runtime_cancel_finishes_like_a_stop(self):
        for i, reason in enumerate(("max_turns", "no_progress", "hook_stopped")):
            with self.subTest(reason=reason):
                pid = "r-%d" % i
                self.prompt_g(pid)
                self.gk("interrupt", hook_event_name="StopCancelled", promptId=pid, reason=reason,
                        cancelledBy="runtime", last_assistant_message="Gave up: no progress.")
                self.assertEqual(self.opt("@agent_state"), "done")
                self.assertEqual(self.opt("@agent_turn_result"), pid + " loud")
                self.assertEqual(self.opt("@agent_detail"), "Gave up: no progress.")
        # cancelledBy unknown is unknown, not the runtime: the interrupt.
        self.prompt_g("u-1")
        self.gk("interrupt", hook_event_name="StopCancelled", promptId="u-1", cancelledBy="unknown")
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assertNotEqual(self.opt("@agent_turn_result"), "u-1 loud")
        # Watched: seen as it lands.
        self.prompt_g("p-2")
        self.hook("interrupt", "grok", payload=dict(session_id=SID, cwd="/work/project", promptId="p-2",
                                                    cancelledBy="runtime"), **self.VIEWING)
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assertRegex(self.opt("@agent_seen_at"), r"^\d+$")
        # Mack's own cancel stays the plain idle, no finish verdict.
        self.prompt_g("p-3")
        self.gk("interrupt", hook_event_name="StopCancelled", promptId="p-3", cancelledBy="user")
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assertNotEqual(self.opt("@agent_turn_result"), "p-3 loud")

    def test_background_tasks_hold_running(self):
        self.prompt_g("p-1")
        self.gk("done", promptId="p-1", reason="end_turn", last_assistant_message="Started the build.",
                backgroundTasks=[{"id": "bash_1", "type": "shell", "status": "running", "command": "make"}])
        self.assertEqual(self.opt("@agent_state"), "running")
        self.assertEqual(self.opt("@agent_bg_owner"), "user")
        self.assertEqual(self.opt("@agent_turn_result"), "p-1 loud")
        self.assertEqual((self.opt("@agent_detail_kind"), self.opt("@agent_detail")), ("done", "Started the build."))
        self.prompt_g("p-2")
        self.gk("done", promptId="p-2", reason="end_turn", backgroundTasks=[], sessionCrons=[{"id": "c1"}])
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertFalse(self.has("@agent_bg_owner"))

    def test_a_lone_monitor_is_not_work_out(self):
        # No agent-bg-pending (this HOME has none): the jq fallback leaves
        # monitors out, so a lone one no longer holds the tab for good.
        self.assertFalse((self.home / ".local/bin/agent-bg-pending").exists())
        self.prompt_g("p-1")
        self.gk("done", promptId="p-1", reason="end_turn",
                backgroundTasks=[{"id": "mon_1", "type": "monitor", "status": "running"}])
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertFalse(self.has("@agent_bg_owner"))
        # A monitor beside a subagent: the subagent holds.
        self.prompt_g("p-2")
        self.gk("done", promptId="p-2", reason="end_turn",
                backgroundTasks=[{"id": "mon_1", "type": "Monitor"},
                                 {"id": "sub_1", "type": "subagent", "status": "running"}])
        self.assertEqual(self.opt("@agent_state"), "running")

    def test_stop_asks_agent_bg_pending_grok_stop(self):
        # Both callers ask the one program, the Stop payload on stdin: its
        # verdict holds or finishes the tab, whatever the list's length says.
        log, rc = self.root / "bg.log", self.root / "bg.rc"
        stub = ('#!/bin/sh\necho "$*" >> "%s"\ncat >> "%s"\necho >> "%s"\n'
                'r=$(cat "%s"); [ "$r" = 0 ] && echo 1\nexit "$r"\n') % (log, log, log, rc)
        path = self.home / ".local/bin/agent-bg-pending"
        path.write_text(stub)
        path.chmod(0o755)
        rc.write_text("1")
        self.prompt_g("p-1")
        self.gk("done", promptId="p-1", reason="end_turn",
                backgroundTasks=[{"id": "x", "type": "shell", "status": "running"}])
        self.assertEqual(self.opt("@agent_state"), "done")       # the program said none
        lines = log.read_text().splitlines()
        self.assertEqual(lines[0], "--grok-stop")
        self.assertEqual(json.loads(lines[1])["backgroundTasks"][0]["id"], "x")
        rc.write_text("0")
        self.prompt_g("p-2")
        self.gk("done", promptId="p-2", reason="end_turn",
                backgroundTasks=[{"id": "m", "type": "monitor"}])
        self.assertEqual(self.opt("@agent_state"), "running")    # ...and here it said one
        self.assertEqual(self.opt("@agent_bg_owner"), "user")

    def test_interrupt_goes_idle(self):
        self.prompt_g("p-1")
        self.gk("interrupt", hook_event_name="StopCancelled")
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assertEqual(self.opt("@agent_kind"), "grok")

    def test_source_new_is_a_fresh_session_and_load_a_resume(self):
        marks = {"@agent_paused": "100", "@agent_pause_voided": "101", "@agent_limit": "0 grok 100",
                 "@agent_limit_lifted": "102"}
        for k, v in marks.items():
            self.set_opt(k, v)
        self.gk("idle", hook_event_name="SessionStart", source="load")
        self.assertEqual({k: self.opt(k) for k in marks}, marks, "a resume keeps them")
        self.assertEqual(self.opt("@agent_summary"), "", "a resume keeps its title (none here)")
        self.gk("idle", hook_event_name="SessionStart", source="new")
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assertEqual(self.opt("@agent_summary"), "project/New Session")
        for k in marks:
            self.assertFalse(self.has(k), k)

    def test_subagent_permission_records_its_asker(self):
        self.prompt_g("p-1")
        self.gk("needs-approval", hook_event_name="Notification", subagentType="explore",
                tool_name="run_terminal_command", tool_input={"command": "ls"})
        self.assertEqual(self.opt("@agent_state"), "needs-input")
        self.assertEqual(self.opt("@agent_ask_from"), "explore")
        self.gk("heartbeat", hook_event_name="PostToolUse", tool_name="read_file")    # main's call
        self.assertEqual(self.opt("@agent_state"), "needs-input")
        self.gk("heartbeat", hook_event_name="PostToolUse", subagentType="explore", tool_name="run_terminal_command")
        self.assertEqual(self.opt("@agent_state"), "running")

    def test_permission_notification_arms_the_asker_gate(self):
        # Grok's only permission event is the Notification (no tool name): it
        # still records whose prompt it is, so a background subagent's tool
        # call can't clear main's open prompt, nor main's a subagent's.
        note = dict(hook_event_name="Notification", notification_type="permission_prompt", message="use_tool")
        self.prompt_g("p-1")
        self.gk("needs-approval", **note)
        self.assertEqual((self.opt("@agent_state"), self.opt("@agent_ask_from")), ("needs-input", "main"))
        self.gk("heartbeat", hook_event_name="PostToolUse", subagentType="explore", tool_name="read_file")
        self.assertEqual(self.opt("@agent_state"), "needs-input")
        self.gk("heartbeat", hook_event_name="PostToolUse", tool_name="run_terminal_command")
        self.assertEqual(self.opt("@agent_state"), "running")
        self.gk("needs-approval", subagentType="explore", **note)
        self.assertEqual(self.opt("@agent_ask_from"), "explore")
        self.gk("heartbeat", hook_event_name="PostToolUse", tool_name="read_file")
        self.assertEqual(self.opt("@agent_state"), "needs-input")

    def test_failed_reopens_only_for_a_main_tool_call(self):
        self.prompt_g("p-1")
        self.gk("failed", promptId="p-1", error="server_error", error_details="boom")
        self.gk("heartbeat", subagentType="explore", tool_name="read_file")
        self.assertEqual(self.opt("@agent_state"), "failed")
        self.gk("heartbeat", tool_name="read_file")
        self.assertEqual(self.opt("@agent_state"), "running")


if __name__ == "__main__":
    unittest.main()
