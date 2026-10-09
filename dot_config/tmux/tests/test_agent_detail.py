"""@agent_kind / @agent_detail_kind / @agent_detail: the sidebar's per-window fields.

Reuses the fake-tmux harnesses of test_codex_indicator (indicator hooks) and
test_agent_jump_watcher (watcher GC). Isolated HOME/TMPDIR; no real tmux
server, model call, watcher or GUI is touched. Run with unittest.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import unittest

import test_codex_indicator as ci
import test_agent_jump_watcher as jw

# The indicator fake, plus a log of each INVOCATION's raw argv, so a test can
# check that the detail rides in the same tmux command list as the state.
FAKE_TMUX = ci.FAKE_TMUX.replace(
    " s=json.loads(p.read_text())\n",
    " s=json.loads(p.read_text())\n s.setdefault('invocations',[]).append(sys.argv[1:])\n", 1)
assert FAKE_TMUX != ci.FAKE_TMUX

DETAIL = ("@agent_kind", "@agent_detail_kind", "@agent_detail")


class DetailHookTests(ci.IndicatorIntegrationTests):
    # Inherit the harness only, not the inherited test methods.
    def setUp(self):
        super().setUp()
        (self.bin / "tmux").write_text(FAKE_TMUX)

    def claude(self, mode, **payload):
        return self.hook(mode, agent="claude", **payload)

    def detail(self, w=None):
        w = self.window() if w is None else w
        return w.get("@agent_detail_kind"), w.get("@agent_detail")

    def snapshot(self):
        return json.loads(self.state.read_text())

    def test_each_event_sets_kind_and_text(self):
        w = self.claude("running", prompt="Refactor the parser\nsecond line ignored")
        self.assertEqual(w["@agent_kind"], "claude")
        self.assertEqual(self.detail(w), ("run", "Refactor the parser"))

        w = self.claude("needs-approval", hook_event_name="PermissionRequest",
                        tool_name="Bash", tool_input={"command": "git push origin main", "description": "push"})
        self.assertEqual(w["@agent_state"], "needs-input")
        self.assertEqual(self.detail(w), ("perm", "Bash git push origin main"))

        w = self.claude("needs-approval", tool_name="Edit",
                        tool_input={"file_path": "/work/project/scripts/foo.py", "old_string": "a"})
        self.assertEqual(self.detail(w), ("perm", "Edit scripts/foo.py"))

        w = self.claude("needs-approval", tool_name="WebFetch",
                        tool_input={"url": "https://www.example.com", "prompt": "read it"})
        self.assertEqual(self.detail(w), ("perm", "WebFetch example.com"))

        w = self.claude("needs-approval", tool_name="Read",
                        tool_input={"file_path": str(self.root / "notes/x.md")})
        self.assertEqual(self.detail(w), ("perm", "Read ~/notes/x.md"))

        w = self.claude("needs-approval", tool_name="Grep", tool_input={"pattern": "TODO", "path": "src"})
        self.assertEqual(self.detail(w), ("perm", "Grep TODO"))

        w = self.claude("needs-approval", tool_name="AskUserQuestion",
                        tool_input={"questions": [{"question": "Which branch?", "options": []},
                                                  {"question": "Second?"}]})
        self.assertEqual(w["@agent_state"], "needs-input")
        self.assertEqual(self.detail(w), ("ask", "Which branch?"))

        w = self.claude("failed", hook_event_name="StopFailure", error="overloaded",
                        error_details="529 Overloaded")
        self.assertEqual(w["@agent_state"], "failed")
        self.assertEqual(self.detail(w), ("fail", "overloaded: 529 Overloaded"))
        self.assertEqual(self.detail(self.claude("failed")), ("fail", "turn failed"))

        w = self.claude("done", hook_event_name="Stop",
                        last_assistant_message="\n\nAll tests pass.\nDetails follow.")
        self.assertEqual(w["@agent_state"], "done")
        self.assertEqual(self.detail(w), ("done", "All tests pass."))

    def test_detail_rides_in_the_state_command_list(self):
        self.claude("running", prompt="go")
        inv = self.snapshot()["invocations"]
        writes = [a for a in inv if "@agent_state" in a and "set-option" in a]
        self.assertTrue(writes)
        last = writes[-1]
        for name in ("@agent_state", "@agent_since", "@agent_detail_kind", "@agent_detail", "@agent_kind"):
            self.assertIn(name, last, last)

    def test_detail_written_even_when_state_unchanged(self):
        self.claude("needs-approval", tool_name="Bash", tool_input={"command": "ls"})
        w = self.claude("needs-approval", tool_name="Bash", tool_input={"command": "rm -rf build"})
        self.assertEqual(self.detail(w), ("perm", "Bash rm -rf build"))

    def test_notification_does_not_clobber_its_permission_request(self):
        self.claude("needs-approval", tool_name="Bash", tool_input={"command": "make deploy"})
        w = self.claude("needs-approval", hook_event_name="Notification",
                        notification_type="permission_prompt",
                        message="Claude needs your permission to use Bash")
        self.assertEqual(self.detail(w), ("perm", "Bash make deploy"))
        # Arriving first (from a run detail), it stands in with the tool name.
        self.claude("running", prompt="next")
        w = self.claude("needs-approval", hook_event_name="Notification",
                        message="Claude needs your permission to use WebSearch")
        self.assertEqual(self.detail(w), ("perm", "WebSearch"))

    def test_done_falls_back_to_transcript_tail(self):
        tp = self.root / "t.jsonl"
        lines = [{"type": "user", "message": {"content": "hi"}},
                 {"type": "assistant", "message": {"content": [{"type": "text", "text": "Old reply"}]}},
                 {"type": "assistant", "message": {"content": [{"type": "text", "text": "Final # answer\nmore"},
                                                               {"type": "tool_use", "name": "x"}]}},
                 {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "y"}]}}]
        # Compact separators, as Claude writes transcripts (the grep pre-filter keys on them);
        # the first line is a cut-off fragment, as a 64KB tail begins.
        tp.write_text("partial line}\n" + "\n".join(json.dumps(l, separators=(",", ":")) for l in lines) + "\n")
        w = self.claude("done", transcript_path=str(tp))
        self.assertEqual(self.detail(w), ("done", "Final answer"))
        w = self.claude("done")
        self.assertEqual(self.detail(w), ("done", "turn finished"))

    def test_sanitizing(self):
        w = self.claude("running", prompt='  Fix  "the"\t#{pane_id} 100%\x07 bug;\x1b[31m  ;; ')
        self.assertEqual(self.detail(w), ("run", "Fix the {pane_id} 100 bug; [31m"))
        w = self.claude("running", prompt="tail;;")
        self.assertEqual(self.detail(w)[1], "tail")
        w = self.claude("running", prompt=" été — café")
        self.assertEqual(self.detail(w)[1], "été — café")
        long = "word " * 40 + "é" * 50
        w = self.claude("needs-approval", tool_name="Bash", tool_input={"command": long})
        text = self.detail(w)[1]
        self.assertLessEqual(len(text), 80)
        self.assertTrue(text.endswith("…"), text)
        self.assertTrue(text.startswith("Bash word word"), text)
        for t in [w["@agent_detail"]]:
            for bad in ("#", '"', "%", "\n", "\t", "\x07", "\x1b"):
                self.assertNotIn(bad, t)
        # Nothing that reaches tmux argv ends in ';' (tmux would cut the list there).
        for inv in self.snapshot()["invocations"]:
            for arg in inv:
                self.assertFalse(arg != ";" and arg.endswith(";"), inv)

    def test_hostile_text_is_never_evaluated(self):
        w = self.claude("running", prompt="$(touch PWNED) `touch PWNED2` ; touch PWNED3")
        self.assertEqual(self.detail(w)[1], "$(touch PWNED) `touch PWNED2` ; touch PWNED3")
        for name in ("PWNED", "PWNED2", "PWNED3"):
            self.assertFalse((self.root / name).exists())

    def test_subagent_events_are_ignored(self):
        self.claude("running", prompt="main task")
        # State and detail only: running's detached title condenser may land
        # on @agent_summary while these run, which is not a subagent's doing.
        keys = ("@agent_state", "@agent_since", "@agent_detail_kind", "@agent_detail", "@agent_kind")
        before = {k: self.window().get(k) for k in keys}
        self.claude("done", agent_id="child", last_assistant_message="subagent done")
        self.claude("heartbeat", agent_id="child")
        self.claude("failed", agent_id="child", error="boom")
        self.assertEqual({k: self.window().get(k) for k in keys}, before)

    def test_subagent_permission_prompt_is_the_tabs_needs_input(self):
        # A subagent's approval is shown in the main session and blocks on the
        # user: its PermissionRequest must turn the tab yellow at once, not
        # wait for the Notification that follows seconds later.
        self.claude("running", prompt="main task")
        w = self.claude("needs-approval", agent_id="child", tool_name="Bash", tool_input={"command": "x"})
        self.assertEqual(w["@agent_state"], "needs-input")
        self.assertEqual(self.detail(w), ("perm", "Bash x"))

    def test_heartbeat_never_touches_detail(self):
        self.claude("needs-approval", tool_name="Bash", tool_input={"command": "ls"})
        n = len(self.snapshot()["calls"])
        w = self.claude("heartbeat")
        self.assertEqual(w["@agent_state"], "running")
        self.assertEqual(self.detail(w), ("perm", "Bash ls"))
        new = self.snapshot()["calls"][n:]
        self.assertFalse([c for c in new if "@agent_detail" in c or "@agent_detail_kind" in c], new)
        # A no-op heartbeat writes nothing at all.
        n = len(self.snapshot()["calls"])
        self.claude("heartbeat")
        self.assertFalse([c for c in self.snapshot()["calls"][n:] if c[0] == "set-option"])

    def test_session_start_and_clear_unset(self):
        self.claude("running", prompt="work")
        w = self.claude("idle", source="resume")
        self.assertEqual(w["@agent_kind"], "claude")
        self.assertNotIn("@agent_detail_kind", w)
        self.assertNotIn("@agent_detail", w)
        self.claude("done", last_assistant_message="ok")
        self.assertEqual(self.claude("clear"), {})
        # compact SessionStart fires mid-turn: the turn's detail survives it.
        self.claude("running", prompt="long turn")
        w = self.claude("idle", source="compact")
        self.assertEqual(self.detail(w), ("run", "long turn"))

    def test_focus_discharge_keeps_detail_and_kind(self):
        self.claude("needs-approval", tool_name="Bash", tool_input={"command": "ls"})
        result = subprocess.run(["bash", str(self.script), "clear-current", "@7"], input="",
                                text=True, capture_output=True, timeout=5,
                                env=dict(self.env, TMUX="/fake/owned,12,0"))
        self.assertEqual(result.returncode, 0, result.stderr)
        w = self.window()
        self.assertEqual(w["@agent_state"], "idle")
        self.assertEqual(w["@agent_kind"], "claude")
        self.assertEqual(self.detail(w), ("perm", "Bash ls"))

    def test_codex_path(self):
        w = self.hook("running", prompt="Port the CLI to Rust")
        self.assertEqual(w["@agent_kind"], "codex")
        self.assertEqual(self.detail(w), ("run", "Port the CLI to Rust"))
        w = self.hook("needs-approval", tool_name="Bash",
                      tool_input={"command": ["bash", "-lc", "cargo build"]})
        self.assertEqual(self.detail(w), ("perm", "Bash bash -lc cargo build"))
        w = self.hook("done", last_assistant_message="Ported.\nNotes...")
        self.assertEqual(self.detail(w), ("done", "Ported."))
        self.assertEqual(self.hook("clear"), {})

    def test_codex_background_thread_is_ignored(self):
        (self.root / ".codex").mkdir()
        with sqlite3.connect(self.root / ".codex/state_5.sqlite") as db:
            db.execute("create table threads (id text, thread_source text, rollout_path text, name text, title text)")
            db.execute("insert into threads values ('aaaa', 'user', '', '', '')")
        self.hook("running", prompt="user work")
        before = self.window()
        # The same bound session id, now registered as a non-user thread.
        with sqlite3.connect(self.root / ".codex/state_5.sqlite") as db:
            db.execute("update threads set thread_source = 'review' where id = 'aaaa'")
        self.hook("done", last_assistant_message="background chatter")
        self.hook("needs-approval", tool_name="Bash", tool_input={"command": "x"})
        with sqlite3.connect(self.root / ".codex/state_5.sqlite") as db:
            db.execute("update threads set thread_source = 'user' where id = 'aaaa'")
        self.hook("running", prompt="You are a Memory Writing Agent. Summarize")
        w = self.window()
        self.assertEqual(self.detail(w), self.detail(before))
        self.assertEqual(self.detail(w), ("run", "user work"))

    def test_invisible_and_bidi_controls_are_removed(self):
        hidden = "".join(map(chr, [0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C,
                                   0x202D, 0x202E, 0x2060, 0x2066, 0x2067, 0x2068, 0x2069, 0xFEFF, 0x061C]))
        w = self.claude("running", prompt=hidden + "ok" + "‮" + "txt.exe" + hidden + "end")
        self.assertEqual(self.detail(w), ("run", "ok txt.exe end"))

    def timed(self, mode, agent="claude", **payload):
        t0 = time.monotonic()
        w = self.hook(mode, agent=agent, **payload)
        return w, time.monotonic() - t0

    def test_huge_payloads_stay_fast_and_correct(self):
        # Unbounded per-character work was quadratic (152 KB took 16 s), and
        # a slow hook blocks the permission dialog / prompt (codex kills it at
        # 10 s, state write and all). A whole hook run is bash + several jq
        # parses + fake tmux, so the bound is looser than the jq-only test's.
        big_line = "x" * 250_000
        heredoc = "cat > out.txt <<'EOF'\n" + "".join(
            "line of heredoc text %d\n" % i for i in range(12_000)) + "EOF"
        self.assertGreater(len(heredoc), 200_000)
        for agent in ("claude", "codex"):
            w, dt = self.timed("needs-approval", agent=agent, tool_name="Bash",
                               tool_input={"command": heredoc})
            self.assertLess(dt, 3, dt)
            self.assertEqual(w["@agent_state"], "needs-input")
            self.assertEqual(w["@agent_kind"], agent)
            kind, text = self.detail(w)
            self.assertEqual(kind, "perm")
            self.assertTrue(text.startswith("Bash cat > out.txt <<'EOF' line of heredoc text 0 line"), text)
            self.assertTrue(text.endswith("…"), text)
            self.assertEqual(len(text), 80)

        w, dt = self.timed("running", prompt="Build " + big_line + "\nnext")
        self.assertLess(dt, 3, dt)
        self.assertEqual(w["@agent_state"], "running")
        self.assertEqual(self.detail(w), ("run", "Build " + "x" * 73 + "…"))

        w, dt = self.timed("done", last_assistant_message="\n" * 10 + "Done: " + big_line)
        self.assertLess(dt, 3, dt)
        self.assertEqual(w["@agent_state"], "done")
        self.assertEqual(self.detail(w), ("done", "Done: " + "x" * 73 + "…"))

    def test_detail_jq_alone_is_linear(self):
        # The extraction by itself, no bash/tmux overhead: < 1 s at 1 MB.
        prog = self.script.read_text().split("DETAIL_JQ='", 1)[1].split("\n'\n", 1)[0]
        cases = [("needs-approval", {"tool_name": "Bash", "tool_input": {"command": "a\n" * 500_000}}),
                 ("running", {"prompt": "p" * 1_000_000}),
                 ("done", {"last_assistant_message": "m " * 500_000}),
                 ("failed", {"error": "server_error", "error_details": "d" * 1_000_000})]
        for mode, payload in cases:
            t0 = time.monotonic()
            r = subprocess.run(["jq", "-r", "--arg", "mode", mode, prog + "detail($mode)"],
                               input=json.dumps(payload), text=True, capture_output=True, timeout=60)
            dt = time.monotonic() - t0
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertLess(dt, 1, (mode, dt))
            kind, text = r.stdout.rstrip("\n").split("\t", 1)
            self.assertLessEqual(len(text), 80, (mode, text))
            self.assertTrue(text.endswith("…"), (mode, text))

    def test_paneless_hook_is_dropped(self):
        env = dict(self.env, TMUX="/fake/owned,12,0")
        env.pop("TMUX_PANE")
        result = subprocess.run(["bash", str(self.script), "running", "claude"],
                                input=json.dumps({"prompt": "x"}), text=True,
                                capture_output=True, env=env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.window(), {})


# Keep the harness's own tests from running twice.
for _name in [n for n in dir(ci.IndicatorIntegrationTests) if n.startswith("test_")]:
    setattr(DetailHookTests, _name, None)


class DetailWatcherGCTests(unittest.TestCase):
    def setUp(self):
        self.f = jw.FakeEnv(
            windows={
                # Agent gone: state + detail left behind.
                "@1": jw.W("main", 1, **{"@agent_state": "done", "@agent_kind": "claude",
                                         "@agent_detail_kind": "done", "@agent_detail": "All good"}),
                # Agent gone, ONLY the sidebar fields left (no state, no summary).
                "@2": jw.W("main", 2, **{"@agent_kind": "codex", "@agent_detail_kind": "run",
                                         "@agent_detail": "x"}),
                # Live agent: kept.
                "@3": jw.W("main", 3, **{"@agent_state": "idle", "@agent_kind": "claude",
                                         "@agent_detail_kind": "perm", "@agent_detail": "Bash ls"}),
            },
            panes=[{"window": "@1", "tty": "/dev/ttys901"}, {"window": "@2", "tty": "/dev/ttys902"},
                   {"window": "@3", "tty": "/dev/ttys900"}])

    def tearDown(self):
        self.f.close()

    def test_gc_unsets_kind_and_detail(self):
        r = self.f.run("agent-tab-watcher.sh", timeout=20, AGENT_TAB_WATCHER_MAX_TICKS="6")
        self.assertEqual(r.returncode, 0, r.stderr)
        w = self.f.read()["windows"]
        for wid in ("@1", "@2"):
            for name in DETAIL + ("@agent_state",):
                self.assertNotIn(name, w[wid]["opts"], (wid, w[wid]["opts"]))
        self.assertEqual(w["@3"]["opts"]["@agent_detail"], "Bash ls")
        self.assertEqual(w["@3"]["opts"]["@agent_kind"], "claude")


if __name__ == "__main__":
    unittest.main()
