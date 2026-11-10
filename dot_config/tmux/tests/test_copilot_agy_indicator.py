"""agent-tab-indicator.sh driven as GitHub Copilot CLI's and agy's hooks drive
it: `<mode> copilot` (~/.copilot/hooks/tmux-agent.json) and `<mode> agy`
(~/.gemini/config/plugins/tmux-agent/hooks.json).

Covers the @agent_kind whitelist, the COPILOT AND AGY payload rewrite (native
camelCase payloads read as Claude-shaped: session/cwd/prompt/tool/agent
keys, copilot's tool names, its elicitation as an `ask`), the event filters
(copilot errorOccurred only when unrecoverable; agy's PreInvocation a no-op
mid-turn; agy's Stop by terminationReason: done / interrupt / failed), the
replies read from each CLI's own record (never a Claude transcript), the
guards (COPILOT_HOOK_DEPTH drops `<mode> claude`, CUA_NOTCH_IGNORE, headless
and nested runs), and the hook config files themselves.

The fake tmux of test_codex_indicator (as test_grok_indicator), on a PATH of
fakes plus bash/jq/python3 and the system dirs only, so no title condenser
(ai-exec) can ever be found.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from test_codex_indicator import FAKE_TMUX, REAL_TOOLS, SOURCE, SYSTEM_PATH

# The shared fake stores any window option, not only @agent_* ones (a
# running write also carries @thread_at).
FAKE_TMUX = FAKE_TMUX.replace("x.startswith('@agent_')", "x.startswith(('@agent_','@thread_'))")

FAILING_COPILOT = "#!/bin/sh\nexit 1\n"
CSID = "5390b68f-fc54-4939-841f-ad0c81359870"
CID = "3814da9d-d4f7-4e84-9c03-222335c5e3b3"
CHEZMOI = Path(SOURCE).resolve().parents[3]
COPILOT_HOOKS = CHEZMOI / "private_dot_copilot/hooks/tmux-agent.json"
AGY_PLUGIN = CHEZMOI / "dot_gemini/config/plugins/tmux-agent"

# test_codex_indicator's FAKE_PS (FAKE_PS_COMM relabels `ps -axo
# pid=,ppid=,comm=`), plus the hook's single-pid looks at its own agent:
# FAKE_PS_SELF="pid|tty|args" answers `-o tty=,comm=` and `-o args=` for pid.
FAKE_PS = r'''#!/usr/bin/env python3
import os,subprocess,sys
a=sys.argv[1:]
with open(os.environ['FAKE_FORK_LOG'],'a') as f: f.write('ps '+' '.join(a)+'\n')
me=os.environ.get('FAKE_PS_SELF','')
if me and '-p' in a and '-o' in a:
    pid,tty,args=me.split('|',2)
    if a[a.index('-p')+1]==pid:
        fmt=a[a.index('-o')+1]
        if fmt=='tty=,comm=': print(tty, args.split()[0]); sys.exit(0)
        if fmt=='args=': print(args); sys.exit(0)
if a!=['-axo','pid=,ppid=,comm=']: os.execv('/bin/ps',['ps']+a)
relabel=dict(x.split(':',1) for x in os.environ.get('FAKE_PS_COMM','').split(',') if x)
out=subprocess.run(['/bin/ps']+a,capture_output=True,text=True).stdout
for line in out.splitlines():
    pid,ppid,comm=(line.split(None,2)+['',''])[:3]
    print(pid,ppid,relabel.get(pid,comm))
'''


def tools_dir(root):
    d = root / "tools"
    d.mkdir(exist_ok=True)
    for name, real in REAL_TOOLS.items():
        if real and not (d / name).exists():
            (d / name).symlink_to(real)
    return d


class Harness(unittest.TestCase):
    AGENT = "copilot"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="copilot-agy-indicator-test-")
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
        # Both CLIs run their hooks as children of the TUI: the pane's own
        # TMUX/TMUX_PANE; Copilot's runner sets COPILOT_HOOK_DEPTH.
        self.env = dict(os.environ, HOME=str(self.root),
                        PATH=":".join([str(self.bin), str(tools_dir(self.root)), SYSTEM_PATH]),
                        TMPDIR=str(self.root), TMUX="/fake/owned,12,0", TMUX_PANE="%42",
                        FAKE_TMUX_STATE=str(self.state), AGENT_TAB_CONDENSE_MODEL="",
                        AGENT_STATE_NOTICE="", FAKE_FORK_LOG=str(self.root / "forks.log"))
        for key in ("AGENT_TAB_SOCKET", "AGENT_TAB_OWNER_SESSION", "AGENT_TAB_OWNER_TOKEN",
                    "AGENT_TAB_OWNER_BINDING", "AGENT_TAB_OWNER_RECORD", "CODEX_THREAD_ID", "CODEX_SANDBOX",
                    "GROK_HOOK_EVENT", "COPILOT_HOOK_DEPTH", "CUA_NOTCH_IGNORE", "COPILOT_HOME",
                    "FAKE_PS_COMM", "FAKE_PS_SELF"):
            self.env.pop(key, None)
        if self.AGENT == "copilot":
            self.env["COPILOT_HOOK_DEPTH"] = "1"

    def tearDown(self):
        time.sleep(.1)
        self.tmp.cleanup()

    def base(self):
        if self.AGENT == "copilot":
            return dict(sessionId=CSID, timestamp=1794326028339, cwd="/work/project")
        return dict(conversationId=CID, workspacePaths=["/work/project"], cwd="/work/project",
                    modelName="auto", transcriptPath=str(self.transcript()))

    def transcript(self):
        return self.root / ".gemini/antigravity-cli/brain" / CID / ".system_generated/logs/transcript.jsonl"

    def run_indicator(self, mode, agent=None, env=None, raw=None, **payload):
        data = self.base()
        data.update(payload)
        result = subprocess.run(["bash", str(self.script), mode, agent or self.AGENT],
                                input=raw if raw is not None else json.dumps(data),
                                text=True, capture_output=True, env=env or self.env, cwd=self.root, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def hook(self, mode, agent=None, env=None, **payload):
        self.run_indicator(mode, agent, env=env, **payload)
        return self.window()

    def window(self):
        return json.loads(self.state.read_text())["windows"]["@7"]

    def put(self, **options):
        state = json.loads(self.state.read_text())
        for k, v in options.items():   # None: unset it
            if v is None:
                state["windows"]["@7"].pop(k, None)
            else:
                state["windows"]["@7"][k] = v
        self.state.write_text(json.dumps(state))

    def relabel(self, me, above="zsh", grand="zsh"):
        parent = os.getppid()
        g = subprocess.run(["/bin/ps", "-o", "ppid=", "-p", str(parent)],
                           capture_output=True, text=True).stdout.strip()
        if not g.isdigit() or int(g) <= 1:
            self.skipTest("this test runner has no grandparent process to relabel")
        return f"{os.getpid()}:{me},{parent}:{above},{g}:{grand}"


class CopilotIndicatorTests(Harness):
    AGENT = "copilot"

    def events(self, *contents):
        p = self.root / ".copilot/session-state" / CSID / "events.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"type": "session.start", "data": {"sessionId": CSID}}]
        for c in contents:
            # None: a user turn with no reply yet (the Stop's own turn).
            rows += [{"type": "user.message", "data": {"content": "a prompt"}}]
            if c is None:
                continue
            rows += [{"type": "assistant.turn_start", "data": {"turnId": "0"}},
                     {"type": "assistant.message", "data": {"messageId": "m", "content": "Working on it."}},
                     {"type": "tool.execution_start", "data": {"toolName": "bash"}},
                     {"type": "assistant.message", "data": {"messageId": "m", "content": c, "phase": "final_answer"}},
                     {"type": "assistant.message", "data": {"messageId": "m", "content": "  "}},
                     {"type": "assistant.turn_end", "data": {"turnId": "0"}}]
        p.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
        return p

    def test_lifecycle_writes_copilot_kind_and_details(self):
        w = self.hook("idle", source="new")
        self.assertEqual((w["@agent_state"], w["@agent_kind"]), ("idle", "copilot"))
        self.assertEqual(w["@agent_summary"], "project/New Session")
        self.assertNotIn("@agent_harness", w)
        w = self.hook("running", prompt="Fix the flaky login test\nmore")
        self.assertEqual((w["@agent_state"], w["@agent_kind"]), ("running", "copilot"))
        self.assertEqual((w["@agent_detail_kind"], w["@agent_detail"]), ("run", "Fix the flaky login test"))
        self.assertTrue(w["@agent_summary"].startswith("project/Fix the flaky"), w["@agent_summary"])
        w = self.hook("needs-approval", notification_type="permission_prompt", title="Permission",
                      message="Copilot needs permission to use bash")
        self.assertEqual(w["@agent_state"], "needs-input")
        self.assertEqual((w["@agent_detail_kind"], w["@agent_detail"]), ("perm", "bash"))
        w = self.hook("heartbeat", toolName="bash", toolArgs={"command": "npm test"},
                      toolResult={"resultType": "success", "textResultForLlm": "ok"})
        self.assertEqual(w["@agent_state"], "running")
        self.events("Earlier reply.", "All 12 tests pass now.\nDetails below.")
        w = self.hook("done", stopReason="end_turn", transcriptPath="/somewhere/else.jsonl")
        self.assertEqual((w["@agent_state"], w["@agent_kind"]), ("done", "copilot"))
        self.assertEqual((w["@agent_detail_kind"], w["@agent_detail"]), ("done", "All 12 tests pass now."))
        # @thread_at belongs to the window, not the conversation: it stays.
        self.assertEqual(set(self.hook("clear", reason="user_exit")) - {"@thread_at"}, {"@agent_seen_at"})
        self.assertEqual(json.loads(self.state.read_text())["windows"]["@99"], {"@agent_state": "unrelated"})

    def test_elicitation_is_an_ask(self):
        self.hook("running", prompt="set it up")
        w = self.hook("needs-approval", notification_type="elicitation_dialog", title="Question",
                      message="Which database should I use?")
        self.assertEqual(w["@agent_state"], "needs-input")
        self.assertEqual((w["@agent_detail_kind"], w["@agent_detail"]), ("ask", "Which database should I use?"))
        self.assertEqual(w["@agent_ask_from"], "main")

    def test_stop_without_a_record_says_turn_finished(self):
        self.hook("running", prompt="go")
        w = self.hook("done", stopReason="end_turn")
        self.assertEqual((w["@agent_state"], w["@agent_detail"]), ("done", "turn finished"))

    def test_a_previous_turns_reply_is_never_this_turns(self):
        # The last reply in the file belongs to the turn BEFORE the last
        # user.message: this Stop's turn wrote none (yet).
        self.events("Old turn's reply.", None)
        self.hook("running", prompt="go")
        self.assertEqual(self.hook("done", stopReason="end_turn")["@agent_detail"], "turn finished")

    def test_error_occurred_only_unrecoverable_model_or_system(self):
        self.hook("running", prompt="go")
        before = self.window()
        for extra in [dict(error={"message": "429 retry", "name": "E"}, errorContext="model_call", recoverable=True),
                      dict(error={"message": "tool blew up"}, errorContext="tool_execution", recoverable=False),
                      dict(error="bad input", errorContext="user_input", recoverable=False)]:
            with self.subTest(extra=extra):
                self.assertEqual(self.hook("failed", **extra), before)
        w = self.hook("failed", error={"message": "model unavailable", "name": "CAPIError"},
                      errorContext="model_call", recoverable=False)
        self.assertEqual((w["@agent_state"], w["@agent_detail_kind"], w["@agent_detail"]),
                         ("failed", "fail", "model unavailable"))

    def test_usage_limits_are_the_rate_limited_mark(self):
        # Copilot's own words: the 402 seen 81 times in session-state
        # (request id redacted), and the runtime's rate-limit strings.
        now = int(time.time())
        for msg, want in [
                ("You have exceeded your monthly quota (Request ID: 0000:000000:0000000:0000000:00000000)",
                 "Copilot monthly quota"),
                ("You've reached your weekly rate limit. Learn More (https://docs.github.com/copilot/concepts/rate-limits).",
                 "Copilot weekly limit"),
                ("You've hit your session rate limit.", "Copilot session limit"),
                ("You've hit the rate limit for this model. Please switch models or wait for your limit to reset",
                 "Copilot model limit · /model to switch"),
                ("You've run out of your included AI credits for the month. Manage budget: https://github.com/settings/copilot/features",
                 "Copilot monthly quota"),
                ("You've hit your rate limit. Please wait for your limit to reset", "Copilot limit")]:
            with self.subTest(msg=msg[:40]):
                self.hook("running", prompt="go")
                w = self.hook("failed", error={"message": msg, "name": "CAPIError"},
                              errorContext="model_call", recoverable=False)
                self.assertEqual((w["@agent_state"], w["@agent_detail_kind"], w["@agent_detail"]),
                                 ("paused", "limit", want))
                r, typ, e = w["@agent_limit"].split()
                self.assertEqual((r, typ), ("0", "copilot"))
                self.assertEqual(w["@agent_paused"], e)
        # A reset in the message is the limit's resets_at.
        self.hook("running", prompt="go")
        w = self.hook("failed", error="Rate limit hit (You've hit your session rate limit.) "
                                      "(current limit resets in ~3 hours)",
                      errorContext="model_call", recoverable=False)
        r = int(w["@agent_limit"].split()[0])
        self.assertTrue(now + 10800 <= r <= now + 10800 + 30, r)
        self.assertTrue(w["@agent_detail"].startswith("Copilot session limit · resets "), w["@agent_detail"])
        # A non-limit failure stays red. (Ending a standing limit reads the
        # window through display-message formats this fake does not expand:
        # test_agent_limit covers it on a private tmux server.)
        self.put(**{"@agent_limit": None})
        self.hook("running", prompt="go")
        w = self.hook("failed", error={"message": "model unavailable"}, errorContext="model_call", recoverable=False)
        self.assertEqual((w["@agent_state"], w["@agent_detail_kind"]), ("failed", "fail"))

    def test_the_record_names_the_session_events(self):
        ev = str(self.root / ".copilot/session-state" / CSID / "events.jsonl")
        self.assertEqual(self.hook("idle", source="new")["@agent_record"], ev)
        self.put(**{"@agent_record": "stale"})
        self.assertEqual(self.hook("running", prompt="go")["@agent_record"], ev)
        # A subagent's prompt is inert, record and all; COPILOT_HOME moves it.
        self.put(**{"@agent_record": "kept"})
        self.assertEqual(self.hook("running", agentId="sub-1", prompt="sub")["@agent_record"], "kept")
        env = dict(self.env, COPILOT_HOME=str(self.root / "ch"))
        self.assertEqual(self.hook("running", env=env, prompt="go")["@agent_record"],
                         str(self.root / "ch/session-state" / CSID / "events.jsonl"))
        # The session's end drops it with the rest.
        self.assertNotIn("@agent_record", self.hook("clear", reason="user_exit"))

    def test_subagent_events_are_inert_but_its_gate_paints(self):
        self.hook("running", prompt="main task")
        before = self.window()
        for mode, extra in [("running", {"prompt": "sub task"}), ("done", {}), ("idle", {"source": "new"}),
                            ("heartbeat", {"toolName": "bash"}), ("clear", {"reason": "complete"})]:
            with self.subTest(mode=mode):
                self.assertEqual(self.hook(mode, agentId="sub-1", agentType="explore", **extra), before)
        w = self.hook("needs-approval", agentId="sub-1", notification_type="elicitation_dialog", message="Pick one")
        self.assertEqual((w["@agent_state"], w["@agent_ask_from"]), ("needs-input", "sub-1"))
        # Only the asker's own tool call re-arms (COPILOT_SUB_JQ, the whole payload).
        self.assertEqual(self.hook("heartbeat", toolName="view")["@agent_state"], "needs-input")
        self.assertEqual(self.hook("heartbeat", agentId="sub-1", toolName="view")["@agent_state"], "running")

    def test_claude_hook_run_by_copilot_is_inert(self):
        # A repo's .claude/settings.json hooks, run by Copilot as `<mode> claude`.
        for mode, extra in [("idle", {"source": "startup"}), ("running", {"prompt": "x"}), ("done", {}),
                            ("clear", {})]:
            with self.subTest(mode=mode):
                self.assertEqual(self.hook(mode, agent="claude", **extra), {})
        env = {k: v for k, v in self.env.items() if k != "COPILOT_HOOK_DEPTH"}
        self.assertEqual(self.hook("running", agent="claude", env=env, prompt="y")["@agent_kind"], "claude")

    def test_ai_exec_legs_are_inert(self):
        env = dict(self.env, CUA_NOTCH_IGNORE="1")
        for mode in ("idle", "running", "done", "heartbeat", "needs-approval", "clear"):
            with self.subTest(mode=mode):
                self.assertEqual(self.hook(mode, env=env, prompt="label this"), {})

    def test_headless_copilot_is_inert_and_the_tui_paints(self):
        comm = self.relabel("copilot")
        me = os.getpid()
        for tty, args in [("??", "copilot --model auto"), ("ttys001", "copilot -p hello"),
                          ("ttys001", "copilot -s -phello"), ("ttys001", "/opt/homebrew/bin/copilot --prompt=x"),
                          ("ttys001", "copilot --model auto --allow-all-tools -p x")]:
            with self.subTest(tty=tty, args=args):
                env = dict(self.env, FAKE_PS_COMM=comm, FAKE_PS_SELF=f"{me}|{tty}|{args}")
                self.assertEqual(self.hook("running", env=env, prompt="x"), {})
        # ps loses the quoting: `copilot -i "fix the -p flag"` is a TUI, and
        # so is a -p inside a value or after the first positional word.
        for args in ("/opt/homebrew/bin/copilot --continue", "copilot -i fix the -p flag",
                     "copilot --name -p --continue", "copilot --banner hello -p"):
            with self.subTest(args=args):
                self.put(**{"@agent_state": "idle"})
                env = dict(self.env, FAKE_PS_COMM=comm, FAKE_PS_SELF=f"{me}|ttys001|{args}")
                self.assertEqual(self.hook("running", env=env, prompt="x")["@agent_state"], "running")

    def test_copilot_in_another_agents_tool_shell_is_inert(self):
        env = dict(self.env, FAKE_PS_COMM=self.relabel("copilot", "zsh", "claude"))
        self.assertEqual(self.hook("running", env=env, prompt="child"), {})

    def test_usage_names_both(self):
        err = self.run_indicator("bogus").stderr
        self.assertIn("|copilot|agy]", err)

    def test_hook_file_wires_every_mode(self):
        cfg = json.loads(COPILOT_HOOKS.read_text())
        self.assertEqual(cfg["version"], 1)
        modes = {}
        for event, entries in cfg["hooks"].items():
            for e in entries:
                self.assertEqual(e["type"], "command")
                # Other scripts may ride the same events (the resurrect
                # session tracker); only the indicator's entries are ours.
                if "agent-tab-indicator.sh" not in e["bash"]:
                    self.assertLessEqual(e["timeoutSec"], 10)
                    continue
                self.assertTrue(e["bash"].startswith('bash "$HOME/.config/tmux/scripts/agent-tab-indicator.sh" '))
                self.assertTrue(e["bash"].endswith(" copilot"), e)
                self.assertLessEqual(e["timeoutSec"], 10)
                modes[event] = e["bash"].split()[-2]
        self.assertEqual(modes, {"sessionStart": "idle", "userPromptSubmitted": "running",
                                 "postToolUse": "heartbeat", "postToolUseFailure": "heartbeat",
                                 "notification": "needs-approval", "agentStop": "done",
                                 "errorOccurred": "failed", "sessionEnd": "clear"})
        self.assertEqual(cfg["hooks"]["notification"][0]["matcher"], "permission_prompt|elicitation_dialog")


class AgyIndicatorTests(Harness):
    AGENT = "agy"

    def steps(self, *replies):
        p = self.transcript()
        p.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
                 "content": "<USER_REQUEST>\nfix it\n</USER_REQUEST>"}]
        for r in replies:
            if r is None:   # a new user turn, no reply yet
                rows.append(dict(rows[0]))
                continue
            rows += [{"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                      "tool_calls": [{"name": "view_file", "args": {}}]},
                     {"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "DONE", "content": "file"},
                     {"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                      "content": r}]
        p.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))

    def pre(self, prompt="<USER_REQUEST>\nFix the flaky login test\n</USER_REQUEST>", n=0, **extra):
        return self.hook("running", hookEventName="PreInvocation", invocationNum=n, initialNumSteps=4,
                         lastUserInput=prompt, **extra)

    def test_lifecycle_writes_agy_kind_and_details(self):
        w = self.hook("idle", hookEventName="SessionStart")
        self.assertEqual((w["@agent_state"], w["@agent_kind"]), ("idle", "agy"))
        self.assertEqual(w["@agent_summary"], "project/New Session")
        w = self.pre()
        self.assertEqual((w["@agent_state"], w["@agent_kind"]), ("running", "agy"))
        self.assertEqual((w["@agent_detail_kind"], w["@agent_detail"]), ("run", "Fix the flaky login test"))
        self.assertTrue(w["@agent_summary"].startswith("project/Fix the flaky"), w["@agent_summary"])
        self.assertEqual(w["@agent_turn_origin"], "user - -")
        # Every later model call of the turn: a no-op.
        self.assertEqual(self.pre("something else entirely", n=3), w)
        w = self.hook("heartbeat", hookEventName="PostToolUse", toolCall={"id": "t1", "name": "run_command",
                                                                         "args": {"CommandLine": "npm test"}})
        self.assertEqual(w["@agent_state"], "running")
        self.steps("Old reply.", "Fixed: the login test waits for the redirect.\nMore.")
        w = self.hook("done", hookEventName="Stop", executionNum=1, terminationReason="NO_TOOL_CALL", fullyIdle=True)
        self.assertEqual((w["@agent_state"], w["@agent_kind"]), ("done", "agy"))
        self.assertEqual((w["@agent_detail_kind"], w["@agent_detail"]),
                         ("done", "Fixed: the login test waits for the redirect."))
        self.assertEqual(w["@agent_turn_result"], "- loud")
        # The next turn's first call starts a new turn.
        self.assertEqual(self.pre("next thing")["@agent_state"], "running")

    def test_stop_reasons(self):
        self.pre()
        self.put(**{"@agent_summary": "project/Current title", "@agent_pending": "123"})
        w = self.hook("done", terminationReason="USER_CANCELED", fullyIdle=True)
        self.assertEqual((w["@agent_state"], w["@agent_summary"]), ("idle", "project/Current title"))
        self.assertNotIn("@agent_pending", w)
        self.pre()
        w = self.hook("done", terminationReason="ERROR", error="model call failed: 503")
        self.assertEqual((w["@agent_state"], w["@agent_detail"]), ("failed", "model call failed: 503"))
        for reason in ("MAX_INVOCATIONS", "MAX_TOKEN_BUDGET_EXCEEDED", "TERMINAL_STEP_TYPE", ""):
            with self.subTest(reason=reason):
                self.pre()
                self.assertEqual(self.hook("done", terminationReason=reason)["@agent_state"], "done")

    def test_the_stop_carries_its_own_reply(self):
        self.steps("Transcript reply.")
        self.pre()
        w = self.hook("done", terminationReason="NO_TOOL_CALL", finalModelOutput="From the Stop itself.\nmore")
        self.assertEqual(w["@agent_detail"], "From the Stop itself.")

    def test_a_previous_turns_reply_is_never_this_turns(self):
        self.steps("Old turn's reply.", None)
        self.pre()
        self.assertEqual(self.hook("done", terminationReason="NO_TOOL_CALL")["@agent_detail"], "turn finished")

    def test_prompt_is_the_user_request_without_its_metadata(self):
        for raw, want in [("<USER_REQUEST>\nFix the login test\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\n"
                           "The current local time is 10:00.\n</ADDITIONAL_METADATA>", "Fix the login test"),
                          ("<USER_REQUEST>\nFix the login test", "Fix the login test"),
                          ("Fix the login test", "Fix the login test")]:
            with self.subTest(raw=raw[:40]):
                self.put(**{"@agent_state": "idle"})
                w = self.pre(raw)
                self.assertEqual((w["@agent_detail_kind"], w["@agent_detail"]), ("run", want))

    def test_subagents_never_move_the_main_tab(self):
        self.pre()
        before = self.window()
        sub = dict(conversationId="sub-conv-1", parentConversationId=CID, rootConversationId=CID,
                   agentName="browser_subagent", lastUserInput="<USER_REQUEST>open the page</USER_REQUEST>")
        for mode, extra in [("idle", {}), ("done", {"terminationReason": "NO_TOOL_CALL", "finalModelOutput": "x"}),
                            ("done", {"terminationReason": "USER_CANCELED"}), ("done", {"terminationReason": "ERROR"})]:
            with self.subTest(mode=mode, extra=extra):
                self.assertEqual(self.hook(mode, **sub, **extra), before)
        # Its PreInvocation, on a window that is NOT running: still inert.
        self.put(**{"@agent_state": "done"})
        before = self.window()
        self.assertEqual(self.pre(**{k: v for k, v in sub.items() if k != "lastUserInput"}), before)
        # A failed main turn is not re-armed by the subagent's tool call.
        self.put(**{"@agent_state": "failed"})
        self.assertEqual(self.hook("heartbeat", **sub, toolCall={"name": "click"})["@agent_state"], "failed")
        self.assertEqual(self.hook("heartbeat", toolCall={"name": "view_file"})["@agent_state"], "running")

    def test_midturn_preinvocation_forks_nothing(self):
        self.pre()
        log = self.root / "forks.log"
        log.write_text("")
        self.pre("x", n=5)
        self.assertEqual(log.read_text(), "", "no ps walk, no jq: one show-options")

    def test_background_tasks_hold_the_tab_running(self):
        # fullyIdle false: background tasks still out. Held `running` like
        # Claude's bg hold (the watcher releases it from the step store),
        # the reply kept as the detail.
        self.pre()
        w = self.hook("done", terminationReason="NO_TOOL_CALL", fullyIdle=False, finalModelOutput="Started the build.")
        self.assertEqual((w["@agent_state"], w["@agent_detail_kind"], w["@agent_detail"]),
                         ("running", "done", "Started the build."))
        self.assertEqual(w["@agent_bg_owner"], "user")
        # Mack's NEW prompt on the held tab: its first PreInvocation is a new
        # turn (the mid-turn fast exit is only for a window not held), so
        # the detail is his prompt's `run` and the turn stamps move.
        self.put(**{"@agent_turn_start": "1", "@thread_at": "1"})
        w = self.pre("<USER_REQUEST>\nNow deploy it\n</USER_REQUEST>", n=0)
        self.assertEqual((w["@agent_state"], w["@agent_detail_kind"], w["@agent_detail"]),
                         ("running", "run", "Now deploy it"))
        self.assertNotEqual(w["@agent_turn_start"], "1")
        self.assertNotEqual(w["@thread_at"], "1")
        # ...and its later calls are the fast no-op again.
        self.assertEqual(self.pre("something else entirely", n=2), w)
        # fullyIdle true (or absent) finishes as ever.
        w = self.hook("done", terminationReason="NO_TOOL_CALL", fullyIdle=True, finalModelOutput="Build passed.")
        self.assertEqual(w["@agent_state"], "done")
        self.assertNotIn("@agent_bg_owner", w)

    def test_the_record_names_the_conversation_db(self):
        db = str(self.root / ".gemini/antigravity-cli/conversations" / (CID + ".db"))
        self.assertEqual(self.hook("idle", hookEventName="SessionStart")["@agent_record"], db)
        self.put(**{"@agent_record": "stale"})
        self.assertEqual(self.pre()["@agent_record"], db)
        # A subagent's conversation never replaces it; nor does a transcript
        # path outside ~/.gemini or of another shape.
        self.put(**{"@agent_state": "idle"})
        sub = dict(conversationId="sub-conv-1", parentConversationId=CID, rootConversationId=CID)
        self.assertEqual(self.pre(**sub).get("@agent_record"), db)
        self.put(**{"@agent_state": "idle", "@agent_record": "kept"})
        self.assertEqual(self.pre(transcriptPath="/tmp/brain/%s/.system_generated/logs/transcript.jsonl" % CID)
                         ["@agent_record"], "kept")

    def test_usage_limit_is_the_rate_limited_mark(self):
        # Real agy wording (a transcript ERROR_MESSAGE), its reset a Go duration.
        self.pre()
        now = int(time.time())
        w = self.hook("done", terminationReason="ERROR",
                      error="API error (attempt 3): RESOURCE_EXHAUSTED (code 429): You have exhausted your "
                            "capacity on this model. Resets in 1h2m5s.")
        r, typ, e = w["@agent_limit"].split()
        self.assertEqual(typ, "agy")
        self.assertTrue(now + 3725 <= int(r) <= now + 3725 + 5, (r, now))
        self.assertEqual((w["@agent_state"], w["@agent_detail_kind"]), ("paused", "limit"))
        self.assertTrue(w["@agent_detail"].startswith("agy model limit · resets "), w["@agent_detail"])
        self.assertEqual(w["@agent_paused"], e)
        self.assertEqual(w["@agent_turn_result"], "- limited " + r)
        # A stated reset under a minute ("Resets in 0s", the one on disk):
        # the quota is already back, nothing to wait for - the red failure
        # Mack retries, never a pause that would not resume.
        for err in ("RESOURCE_EXHAUSTED (code 429): You have exhausted your capacity on this model. Resets in 0s.",
                    "RESOURCE_EXHAUSTED (code 429): You have exhausted your capacity on this model. Resets in 42.5s."):
            with self.subTest(err=err[-14:]):
                self.put(**{"@agent_limit": None, "@agent_paused": None})
                self.pre()
                w = self.hook("done", terminationReason="ERROR", error=err)
                self.assertEqual((w["@agent_state"], w["@agent_detail_kind"]), ("failed", "fail"))
                self.assertNotIn("@agent_limit", w)
        # No reset stated at all: a limit with resets_at 0, the label alone.
        self.pre()
        w = self.hook("done", terminationReason="ERROR",
                      error="RESOURCE_EXHAUSTED (code 429): Quota exceeded for this project.")
        self.assertEqual((w["@agent_state"], w["@agent_detail"], w["@agent_limit"].split()[0]),
                         ("paused", "agy limit", "0"))
        # A capacity / availability error is a failure, never a limit.
        for err in ("UNAVAILABLE (code 503): The model is overloaded. Please try again later.",
                    "model call failed: 503"):
            with self.subTest(err=err):
                self.put(**{"@agent_limit": None, "@agent_paused": None})
                self.pre()
                w = self.hook("done", terminationReason="ERROR", error=err)
                self.assertEqual((w["@agent_state"], w["@agent_detail_kind"]), ("failed", "fail"))

    def test_transcript_outside_gemini_is_never_read(self):
        outside = self.root / "elsewhere/transcript.jsonl"
        outside.parent.mkdir(parents=True)
        outside.write_text(json.dumps({"type": "PLANNER_RESPONSE", "content": "WRONG"}) + "\n")
        self.pre()
        w = self.hook("done", terminationReason="NO_TOOL_CALL", transcriptPath=str(outside))
        self.assertEqual(w["@agent_detail"], "turn finished")

    def test_ai_exec_and_headless_runs_are_inert(self):
        self.assertEqual(self.hook("running", env=dict(self.env, CUA_NOTCH_IGNORE="1"), lastUserInput="x"), {})
        comm = self.relabel("agy")
        me = os.getpid()
        for tty, args in [("??", "agy"), ("ttys002", "agy -p hi"), ("ttys002", "agy --print hi"),
                          ("ttys002", "agy --prompt=hi")]:
            with self.subTest(tty=tty, args=args):
                env = dict(self.env, FAKE_PS_COMM=comm, FAKE_PS_SELF=f"{me}|{tty}|{args}")
                self.assertEqual(self.hook("running", env=env, lastUserInput="x"), {})
        for args in ("agy -i fix it", "agy -project p1"):   # Go's -project is no -p
            with self.subTest(args=args):
                self.put(**{"@agent_state": "idle"})
                env = dict(self.env, FAKE_PS_COMM=comm, FAKE_PS_SELF=f"{me}|ttys002|{args}")
                self.assertEqual(self.hook("running", env=env, lastUserInput="x")["@agent_state"], "running")

    def test_plugin_files(self):
        manifest = json.loads((AGY_PLUGIN / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "tmux-agent")
        cfg = json.loads((AGY_PLUGIN / "hooks.json").read_text())
        self.assertEqual(list(cfg), ["tmux-agent"])
        spec = cfg["tmux-agent"]
        modes = {}
        for event, entries in spec.items():
            if event == "PostToolUse":
                self.assertEqual(entries[0]["matcher"], "*")
                entries = entries[0]["hooks"]
            for e in entries:
                self.assertEqual(e["type"], "command")
                cmd = e["command"]
                # agy wants a JSON object on stdout from every hook.
                self.assertIn(">/dev/null 2>&1; echo '", cmd)
                json.loads(cmd.rsplit("echo '", 1)[1].rstrip("'"))
                self.assertLessEqual(e["timeout"], 10)
                # Other scripts may ride the same events (the resurrect
                # session tracker); only the indicator's entries are ours.
                if "agent-tab-indicator.sh" not in cmd:
                    continue
                self.assertTrue(cmd.startswith('bash "$HOME/.config/tmux/scripts/agent-tab-indicator.sh" '), cmd)
                modes[event] = cmd.split()[2]
                self.assertEqual(cmd.split()[3], "agy")
        self.assertEqual(modes, {"SessionStart": "idle", "PreInvocation": "running",
                                 "PostToolUse": "heartbeat", "Stop": "done"})
        # The hook's own echo really is what stdout carries.
        out = subprocess.run(["sh", "-c", spec["Stop"][0]["command"]], input="{}", text=True,
                             capture_output=True, env=dict(self.env, HOME=str(self.root / "none")), timeout=10)
        self.assertEqual(json.loads(out.stdout), {"decision": "allow"})


if __name__ == "__main__":
    unittest.main()
