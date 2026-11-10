"""@agent_harness: the launcher behind @agent_kind (abby = claude on ~/.claude-abby).

Reuses the fake-tmux harnesses of test_codex_indicator (indicator hooks, via
test_agent_detail's invocation-logging fake) and test_agent_jump_watcher
(watcher GC). Isolated HOME/TMPDIR; no real tmux server is touched.
"""
import json
import unittest

import test_codex_indicator as ci
import test_agent_jump_watcher as jw
import test_agent_detail as ad

ABBY = {"CLAUDE_CONFIG_DIR": "/x/.claude-abby"}


class HarnessHookTests(ci.IndicatorIntegrationTests):
    def setUp(self):
        super().setUp()
        (self.bin / "tmux").write_text(ad.FAKE_TMUX)

    def claude(self, mode, env=None, **payload):
        return self.hook(mode, agent="claude", _env=env, **payload)

    def test_abby_config_dir_sets_harness(self):
        w = self.claude("running", env=ABBY, prompt="go")
        self.assertEqual((w["@agent_kind"], w.get("@agent_harness")), ("claude", "abby"))
        # Trailing slashes and abby's own absolute value ($HOME/.claude-abby).
        for d in ("/x/.claude-abby/", "/x/.claude-abby//", str(self.root / ".claude-abby")):
            self.claude("idle", env={"CLAUDE_CONFIG_DIR": ""}, source="startup")
            self.assertNotIn("@agent_harness", self.window())
            w = self.claude("done", env={"CLAUDE_CONFIG_DIR": d}, last_assistant_message="ok")
            self.assertEqual(w.get("@agent_harness"), "abby", d)

    def test_harness_rides_the_kind_command_list(self):
        self.claude("running", env=ABBY, prompt="go")
        inv = json.loads(self.state.read_text())["invocations"]
        writes = [a for a in inv if "@agent_kind" in a]
        self.assertTrue(writes)
        for a in writes:
            self.assertIn("@agent_harness", a, a)
            self.assertIn("@agent_state", a, a)

    def test_other_config_dirs_are_not_abby(self):
        for d in ("/x/.claude", "/x/.claude-abby-old", "/x/claude-abby", "/.claude-abby/x", "/"):
            w = self.claude("running", env={"CLAUDE_CONFIG_DIR": d}, prompt=d)
            self.assertNotIn("@agent_harness", w, d)

    def test_plain_claude_unsets_it(self):
        self.claude("running", env=ABBY, prompt="go")
        w = self.claude("done", last_assistant_message="ok")
        self.assertEqual(w["@agent_kind"], "claude")
        self.assertNotIn("@agent_harness", w)

    def test_codex_unsets_it(self):
        # The pane went from abby to codex; codex ignores CLAUDE_CONFIG_DIR.
        self.claude("running", env=ABBY, prompt="go")
        self.assertEqual(self.window().get("@agent_harness"), "abby")
        w = self.hook("running", _env=ABBY, prompt="Port the CLI")
        self.assertEqual(w["@agent_kind"], "codex")
        self.assertNotIn("@agent_harness", w)

    def test_clear_unsets_it(self):
        self.claude("running", env=ABBY, prompt="go")
        self.claude("done", env=ABBY, last_assistant_message="ok")
        self.assertEqual(self.window().get("@agent_harness"), "abby")
        self.assertEqual(set(self.claude("clear", env=ABBY)), {"@agent_seen_at"})

    def test_noop_heartbeat_writes_nothing(self):
        self.claude("running", env=ABBY, prompt="go")
        n = len(json.loads(self.state.read_text())["calls"])
        self.claude("heartbeat", env=ABBY)
        new = json.loads(self.state.read_text())["calls"][n:]
        self.assertFalse([c for c in new if c[0] == "set-option"], new)
        self.assertEqual(self.window().get("@agent_harness"), "abby")


# Keep the harness's own tests from running twice.
for _name in [n for n in dir(ci.IndicatorIntegrationTests) if n.startswith("test_")]:
    setattr(HarnessHookTests, _name, None)


class HarnessWatcherGCTests(unittest.TestCase):
    def setUp(self):
        self.f = jw.FakeEnv(
            windows={
                # Agent gone: kind + harness left behind.
                "@1": jw.W("main", 1, **{"@agent_state": "done", "@agent_kind": "claude",
                                         "@agent_harness": "abby"}),
                # Live agent: kept.
                "@3": jw.W("main", 3, **{"@agent_state": "idle", "@agent_kind": "claude",
                                         "@agent_harness": "abby"}),
            },
            panes=[{"window": "@1", "tty": "/dev/ttys901"}, {"window": "@3", "tty": "/dev/ttys900"}])

    def tearDown(self):
        self.f.close()

    def test_gc_unsets_harness_with_kind(self):
        r = self.f.run("agent-tab-watcher.sh", timeout=20, AGENT_TAB_WATCHER_MAX_TICKS="6")
        self.assertEqual(r.returncode, 0, r.stderr)
        w = self.f.read()["windows"]
        for name in ("@agent_state", "@agent_kind", "@agent_harness"):
            self.assertNotIn(name, w["@1"]["opts"], w["@1"]["opts"])
        self.assertEqual(w["@3"]["opts"].get("@agent_harness"), "abby")


if __name__ == "__main__":
    unittest.main()
