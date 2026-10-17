"""agent-tab-indicator.sh driven as the OpenCode plugin drives it: `<mode> opencode`.

Covers the @agent_kind whitelist, `interrupt` for opencode, extract_summary's
opencode branch (OpenCode's "New session - <date>" placeholder title is not a
name), and the Codex-ownership clearing that now applies to every non-codex
agent. Isolated HOME, fake tmux (from test_codex_indicator) plus one smoke run
against a private detached tmux server. No real model call: the copilot the
condenser would run is a stub that fails at once.
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

from test_codex_indicator import FAKE_TMUX, SOURCE

FAILING_COPILOT = "#!/bin/sh\nexit 1\n"


class OpencodeIndicatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="opencode-indicator-test-")
        self.root = Path(self.tmp.name)
        self.bin = self.root / ".local/bin"
        self.bin.mkdir(parents=True)
        self.state = self.root / "tmux.json"
        self.state.write_text(json.dumps({"calls": [], "windows": {"@7": {}, "@99": {"@agent_state": "unrelated"}}}))
        self.script = self.root / "agent-tab-indicator.sh"
        shutil.copyfile(SOURCE, self.script)
        for name, source in [("tmux", FAKE_TMUX), ("copilot", FAILING_COPILOT)]:
            path = self.bin / name
            path.write_text(source)
            path.chmod(0o755)
        # The OpenCode plugin runs inside the TUI process: it inherits the
        # pane's own TMUX/TMUX_PANE, exactly like a Claude hook.
        self.env = dict(os.environ, HOME=str(self.root), PATH=str(self.bin) + ":" + os.environ["PATH"],
                        TMPDIR=str(self.root), TMUX="/fake/owned,12,0", TMUX_PANE="%42",
                        FAKE_TMUX_STATE=str(self.state), AGENT_TAB_CONDENSE_MODEL="",
                        AGENT_STATE_NOTICE="")
        for key in ("AGENT_TAB_SOCKET", "AGENT_TAB_OWNER_SESSION", "AGENT_TAB_OWNER_TOKEN",
                    "AGENT_TAB_OWNER_BINDING", "AGENT_TAB_OWNER_RECORD"):
            self.env.pop(key, None)

    def tearDown(self):
        time.sleep(.1)  # let a detached (failing) condenser finish before cleanup
        self.tmp.cleanup()

    def run_indicator(self, mode, agent="opencode", env=None, **payload):
        data = dict(session_id="ses_1", cwd="/work/project", **payload)
        result = subprocess.run(["bash", str(self.script), mode, agent], input=json.dumps(data),
                                text=True, capture_output=True, env=env or self.env, cwd=self.root, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def hook(self, mode, agent="opencode", **payload):
        self.run_indicator(mode, agent, **payload)
        return self.window()

    def window(self):
        return json.loads(self.state.read_text())["windows"]["@7"]

    def put(self, **options):
        state = json.loads(self.state.read_text())
        state["windows"]["@7"].update(options)
        self.state.write_text(json.dumps(state))

    # ------------------------------------------------------------ whitelist
    def test_lifecycle_writes_opencode_kind_and_details(self):
        w = self.hook("idle", source="startup", session_title="New session - 2026-10-16T13:34:12.345Z")
        self.assertEqual(w["@agent_state"], "idle")
        self.assertEqual(w["@agent_kind"], "opencode")
        self.assertEqual(w["@agent_summary"], "project/New Session")
        w = self.hook("running", prompt="Fix the websocket reconnect bug\nsecond line",
                      session_title="New session - 2026-10-16T13:34:12.345Z")
        self.assertEqual((w["@agent_state"], w["@agent_kind"]), ("running", "opencode"))
        self.assertEqual((w["@agent_detail_kind"], w["@agent_detail"]), ("run", "Fix the websocket reconnect bug"))
        w = self.hook("needs-approval", tool_name="Bash", tool_input={"command": "git push origin main"})
        self.assertEqual(w["@agent_state"], "needs-input")
        self.assertEqual((w["@agent_detail_kind"], w["@agent_detail"]), ("perm", "Bash git push origin main"))
        w = self.hook("needs-approval", tool_name="AskUserQuestion",
                      tool_input={"questions": [{"question": "Which branch?", "header": "Branch", "options": []}]})
        self.assertEqual((w["@agent_detail_kind"], w["@agent_detail"]), ("ask", "Which branch?"))
        self.assertEqual(self.hook("heartbeat")["@agent_state"], "running")
        w = self.hook("done", last_assistant_message="All tests pass.\nDetails.", session_title="Websocket reconnect fix")
        self.assertEqual((w["@agent_state"], w["@agent_kind"]), ("done", "opencode"))
        self.assertEqual((w["@agent_detail_kind"], w["@agent_detail"]), ("done", "All tests pass."))
        self.assertEqual(w["@agent_summary"], "project/Websocket reconnect fix")
        w = self.hook("failed", error="Overloaded (529)")
        self.assertEqual((w["@agent_state"], w["@agent_detail"]), ("failed", "Overloaded (529)"))
        self.assertEqual(set(self.hook("clear", reason="exit")), {"@agent_seen_at"})
        self.assertEqual(json.loads(self.state.read_text())["windows"]["@99"], {"@agent_state": "unrelated"})

    def test_unknown_agent_still_gets_no_kind(self):
        self.assertNotIn("@agent_kind", self.hook("running", agent="gemini", prompt="hi"))

    def test_usage_names_opencode(self):
        result = self.run_indicator("bogus")
        self.assertIn("claude|codex|opencode", result.stderr)

    # ------------------------------------------------------------ interrupt
    def test_interrupt_for_opencode_discharges_but_keeps_label(self):
        self.hook("running", prompt="long task")
        self.put(**{"@agent_summary": "project/Current title", "@agent_pending": "123"})
        w = self.hook("interrupt", session_title="Current title")
        self.assertEqual(w["@agent_state"], "idle")
        self.assertEqual(w["@agent_summary"], "project/Current title")
        self.assertNotIn("@agent_pending", w)
        self.assertEqual(w["@agent_kind"], "opencode")

    def test_interrupt_stays_inert_for_claude(self):
        self.hook("running", agent="claude", prompt="long task")
        before = self.window()
        self.assertEqual(self.hook("interrupt", agent="claude"), before)

    # ------------------------------------------------------------ extract_summary
    def test_placeholder_title_falls_back_to_prompt(self):
        for placeholder in ("New session - 2026-10-16T13:34:12.345Z", "Child session - 2026-10-16T13:00:00.000Z"):
            with self.subTest(placeholder=placeholder):
                w = self.hook("running", prompt="Repair the flaky login test", session_title=placeholder)
                self.assertEqual(w["@agent_summary"], "project/Repair the flaky login")
                self.assertNotIn("New session", w["@agent_summary"])

    def test_real_title_wins_and_reconcile_updates_it(self):
        w = self.hook("running", prompt="Repair the flaky login test", session_title="Flaky login test")
        self.assertEqual(w["@agent_summary"], "project/Flaky login test")
        w = self.hook("reconcile", session_title="Login retry backoff")
        self.assertEqual(w["@agent_summary"], "project/Login retry backoff")
        self.assertEqual(w["@agent_state"], "running", "reconcile never downgrades a live state")

    def test_reconcile_with_placeholder_and_no_prompt_changes_nothing(self):
        self.hook("running", prompt="Repair the flaky login test")
        before = self.window()
        w = self.hook("reconcile", session_title="New session - 2026-10-16T13:34:12.345Z")
        self.assertEqual(w["@agent_summary"], before["@agent_summary"])

    def test_reconcile_presence_on_unset_window(self):
        w = self.hook("reconcile", session_title="Resumed work")
        self.assertEqual((w["@agent_state"], w["@agent_kind"]), ("idle", "opencode"))
        self.assertEqual(w["@agent_summary"], "project/Resumed work")

    # ------------------------------------------------------------ codex ownership
    def test_opencode_hook_clears_previous_codex_ownership(self):
        self.put(**{"@agent_session_id": "codex-thread", "@agent_owner_token": "frontend-token"})
        w = self.hook("idle", source="startup")
        self.assertNotIn("@agent_session_id", w)
        self.assertNotIn("@agent_owner_token", w)
        self.assertEqual(w["@agent_kind"], "opencode")

    def test_unprovable_owner_environment_leaves_window_untouched(self):
        # A leaked condenser environment (Codex owner binding the hook can't
        # prove) keeps every non-codex hook inert, ownership marks included.
        (self.bin / "codex-terminal-owner").write_text("#!/bin/sh\necho '{\"status\":\"unbound\"}'\n")
        (self.bin / "codex-terminal-owner").chmod(0o755)
        self.put(**{"@agent_state": "running", "@agent_session_id": "aaaa", "@agent_owner_token": "tok"})
        before = self.state.read_text()
        env = dict(self.env, AGENT_TAB_OWNER_SESSION="aaaa", AGENT_TAB_OWNER_TOKEN="tok",
                   AGENT_TAB_OWNER_BINDING="tok:aaaa", AGENT_TAB_SOCKET="/fake/owned")
        for agent in ("opencode", "claude"):
            for mode in ("idle", "running", "done", "interrupt"):
                with self.subTest(agent=agent, mode=mode):
                    self.run_indicator(mode, agent, env=env, prompt="x", source="startup")
                    windows = json.loads(self.state.read_text())["windows"]
                    self.assertEqual(windows, json.loads(before)["windows"])

    # ------------------------------------------------------------ real tmux
    @unittest.skipUnless(shutil.which("tmux"), "tmux executable required")
    def test_real_detached_server_opencode_lifecycle(self):
        # A unique private server with no user config, in a private socket
        # directory; never attaches a client. Killed by name in finally.
        tmux = shutil.which("tmux")
        sockdir = tempfile.mkdtemp(prefix="oct")
        server = "oc-" + uuid.uuid4().hex[:8]
        env = dict(self.env, PATH=os.environ["PATH"], TMUX_TMPDIR=sockdir)
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        command = [tmux, "-L", server, "-f", "/dev/null"]
        try:
            result = subprocess.run(command + ["new-session", "-d", "-s", "smoke", "-P", "-F",
                "#{pane_id}|#{socket_path}|#{pid}", "/bin/sleep 30"], text=True, capture_output=True, env=env, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            pane, socket, pid = result.stdout.strip().split("|", 2)
            env.update(TMUX=f"{socket},{pid},0", TMUX_PANE=pane)

            def show(option):
                return subprocess.run(command + ["show-options", "-wqv", "-t", pane, option],
                                      text=True, capture_output=True, env=env, timeout=5).stdout.strip()
            for mode, payload, expected in [
                ("idle", {"source": "startup"}, "idle"),
                ("running", {"prompt": "Fix the build"}, "running"),
                ("needs-approval", {"tool_name": "Bash", "tool_input": {"command": "make"}}, "needs-input"),
                ("heartbeat", {}, "running"),
                ("interrupt", {}, "idle"),
                ("running", {"prompt": "again"}, "running"),
                ("done", {"last_assistant_message": "Built."}, "done"),
                ("clear", {"reason": "exit"}, ""),
            ]:
                result = subprocess.run(["bash", str(self.script), mode, "opencode"],
                    input=json.dumps(dict(session_id="ses_1", cwd="/work/project", **payload)),
                    text=True, capture_output=True, env=env, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(show("@agent_state"), expected, mode)
                self.assertEqual(show("@agent_kind"), "opencode" if expected else "", mode)
        finally:
            subprocess.run(command + ["kill-server"], capture_output=True, env=env, timeout=5)
            shutil.rmtree(sockdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
