"""agent-tab-watcher.sh: GitHub Copilot CLI (comm `copilot`) and Google's
Antigravity CLI (comm `agy`) as agent kinds - presence (their interactive TUIs
only: `copilot -p`, `copilot mcp list`, `agy -p`, `agy plugin list` in a shell
are not agents), the @agent_kind seed, the birth kept, and no stuck-`running`
reconcile against another agent's record (neither CLI keeps a per-pid status).

Reuses test_agent_jump_watcher's fake-tmux harness (isolated HOME and TMPDIR,
fake `tmux`, `pgrep`, `ps`; the fake ps answers the argv look from
$FAKE_ARGS). No real tmux server, watcher, copilot, agy or GUI is touched.
"""
import json
import subprocess
import time
import unittest

import test_agent_jump_watcher as jw   # module only, for the shared fakes

EXTRACT = ('eval "$(awk \'/^(copilot_tui_args|agy_tui_args)\\(\\) \\{/{f=1} f{print} '
           'f&&/^\\}/{f=0}\' "$1")"\n')


def tui(fn, args_line):
    """0 = the TUI, 1 = not."""
    r = subprocess.run(["bash", "-c", EXTRACT + fn + ' "$2"', "t", str(jw.WATCHER), args_line],
                       capture_output=True, text=True, timeout=10)
    assert r.returncode in (0, 1), r.stderr
    return r.returncode


class TuiArgsTests(unittest.TestCase):
    def test_copilot_tui_forms(self):
        for a in ("copilot", "/opt/homebrew/bin/copilot", "copilot --continue", "copilot -r",
                  "copilot -r 1a2b", "copilot --resume=1a2b", "copilot -i fix the build",
                  "copilot --model gpt-5 --allow-all-tools", "copilot --agent sessions",
                  "copilot -n plugin", "copilot --banner", "copilot -C /tmp", "copilot -- -p",
                  # ps loses the quoting of -i's prompt: its words are no subcommand
                  "copilot -i please update the docs", "copilot --interactive fix the -p flag",
                  "copilot --model auto -i mcp list please"):
            self.assertEqual(tui("copilot_tui_args", a), 0, a)

    def test_copilot_commands_are_not_agents(self):
        for a in ("copilot -p hi", "copilot -p", "copilot -phi", "copilot --prompt x", "copilot --prompt=x",
                  "copilot -s --model auto -p x", "copilot -h", "copilot --help", "copilot -v",
                  "copilot --version", "copilot mcp list", "copilot plugin install x", "copilot sessions",
                  "copilot login", "copilot update", "copilot help hooks", "copilot --model auto version"):
            self.assertEqual(tui("copilot_tui_args", a), 1, a)

    def test_agy_tui_forms(self):
        for a in ("agy", "/opt/homebrew/bin/agy", "agy -c", "agy --continue", "agy --conversation abc",
                  "agy -i fix it", "agy --prompt-interactive=fix", "agy -prompt-interactive go",
                  "agy --model gemini-3 --mode plan", "agy --agent plugin", "agy --sandbox", "agy -- -p",
                  "agy -i please install deps", "agy --prompt-interactive update the -p docs"):
            self.assertEqual(tui("agy_tui_args", a), 0, a)

    def test_agy_commands_are_not_agents(self):
        for a in ("agy -p hi", "agy -p=hi", "agy --print hi", "agy -print hi", "agy --prompt x",
                  "agy --prompt=x", "agy -h", "agy --help", "agy -help", "agy --version",
                  "agy plugin list", "agy plugins", "agy acp", "agy mcp list", "agy remote-control status",
                  "agy update", "agy models", "agy --model x changelog", "agy mic-serve"):
            self.assertEqual(tui("agy_tui_args", a), 1, a)


CP_TTY, CP_P_TTY, CP_MCP_TTY, AGY_TTY, AGY_P_TTY, AGY_PLUG_TTY, LS_TTY = (
    "/dev/ttys940", "/dev/ttys941", "/dev/ttys942", "/dev/ttys943", "/dev/ttys944", "/dev/ttys945",
    "/dev/ttys946")
# Copilot's single executable runs the CLI under a restart supervisor of the
# same comm: two copilot pids on one tty (7101 the child).
PROCS = ("ttys940 7100 /opt/homebrew/bin/copilot\n"
         "ttys940 7101 /opt/homebrew/Caskroom/copilot-cli/1.0.51/copilot\n"
         "ttys941 7200 copilot\n"
         "ttys942 7300 copilot\n"
         "ttys943 7400 /opt/homebrew/bin/agy\n"
         "ttys944 7500 agy\n"
         "ttys945 7600 agy\n"
         "ttys946 7700 copilot-language-server\n")
ARGS = ("7100 /opt/homebrew/bin/copilot --continue\n"
        "7101 /opt/homebrew/Caskroom/copilot-cli/1.0.51/copilot --continue\n"
        "7200 copilot -p hello\n"
        "7300 copilot mcp list\n"
        "7400 /opt/homebrew/bin/agy -c\n"
        "7500 agy -p hello\n"
        "7600 agy plugin list\n"
        "7700 copilot-language-server --stdio\n")


class CopilotAgyWatcherTests(unittest.TestCase):
    def make(self, windows, panes):
        self.f = jw.FakeEnv(windows=windows, panes=panes)
        self.f.procs.write_text(PROCS)
        self.args = self.f.dir / "args"; self.args.write_text(ARGS)
        self.args_log = self.f.dir / "args.log"
        return self.f

    def tearDown(self):
        self.f.close()

    def run_ticks(self, n, **extra):
        env = dict(AGENT_TAB_WATCHER_MAX_TICKS=str(n), FAKE_ARGS=str(self.args),
                   FAKE_ARGS_LOG=str(self.args_log), **extra)
        r = self.f.run("agent-tab-watcher.sh", timeout=10 + 2 * n, **env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.f.read()

    def calls(self, s, wid):
        return [c for c in s["calls"] if c[0] == "set-option" and wid in c]

    def test_tuis_are_seeded_with_their_kind_and_commands_are_shells(self):
        ttys = (CP_TTY, CP_P_TTY, CP_MCP_TTY, AGY_TTY, AGY_P_TTY, AGY_PLUG_TTY, LS_TTY)
        self.make({"@%d" % i: jw.W("main", i) for i in range(1, 8)},
                  [{"window": "@%d" % i, "tty": t} for i, t in enumerate(ttys, 1)])
        s = self.run_ticks(3)
        w = s["windows"]
        self.assertEqual((w["@1"]["opts"].get("@agent_state"), w["@1"]["opts"].get("@agent_kind")),
                         ("idle", "copilot"))
        self.assertEqual((w["@4"]["opts"].get("@agent_state"), w["@4"]["opts"].get("@agent_kind")),
                         ("idle", "agy"))
        # copilot -p, copilot mcp, agy -p, agy plugin, copilot-language-server
        for wid in ("@2", "@3", "@5", "@6", "@7"):
            self.assertNotIn("@agent_state", w[wid]["opts"], wid)
            self.assertNotIn("@agent_kind", w[wid]["opts"], wid)
            self.assertFalse(self.calls(s, wid), s["calls"])
        # One argv look per NEW copilot/agy pid, cached across ticks; the
        # language server is never a candidate (pgrep -x).
        self.assertEqual(sorted(self.args_log.read_text().split()),
                         ["7100", "7101", "7200", "7300", "7400", "7500", "7600"])

    def test_a_hook_kind_is_never_overwritten(self):
        self.make({"@1": jw.W("main", 1, **{"@agent_state": "running", "@agent_kind": "copilot"})},
                  [{"window": "@1", "tty": CP_TTY}])
        s = self.run_ticks(2)
        self.assertEqual(s["windows"]["@1"]["opts"].get("@agent_kind"), "copilot")

    def test_born_tab_keeps_its_birth_and_is_gc_d_after_exit(self):
        now = int(time.time())
        born = {"@agent_state": "idle", "@agent_born": str(now), "@agent_since": "%d idle" % now,
                "@agent_kind": "agy", "@agent_summary": "proj/New Session"}
        self.make({"@1": jw.W("main", 1, **born)}, [{"window": "@1", "tty": AGY_TTY}])
        s = self.run_ticks(3)
        o = s["windows"]["@1"]["opts"]
        self.assertEqual((o.get("@agent_state"), o.get("@agent_kind")), ("idle", "agy"))
        self.assertNotIn("@agent_born", o, "the agent arrived: the mark goes")
        self.f.procs.write_text("ttys943 7401 zsh\n")         # agy exited
        s = self.run_ticks(10)
        o = s["windows"]["@1"]["opts"]
        for name in ("@agent_state", "@agent_kind", "@agent_summary"):
            self.assertNotIn(name, o, o)

    def test_running_is_not_reconciled_from_a_claude_record_or_a_rollout(self):
        # A stale Claude session file for the pid (or a codex rollout left by
        # an earlier codex in the pane) must not idle a copilot/agy turn.
        old = int(time.time()) - 600
        self.make({"@1": jw.W("main", 1, **{"@agent_state": "running", "@agent_kind": "copilot",
                                             "@agent_since": "%d running" % old}),
                   "@4": jw.W("main", 4, **{"@agent_state": "running", "@agent_kind": "agy",
                                             "@agent_since": "%d running" % old})},
                  [{"window": "@1", "tty": CP_TTY}, {"window": "@4", "tty": AGY_TTY}])
        sessions = self.f.home / ".claude/sessions"
        sessions.mkdir(parents=True)
        for pid in (7100, 7101, 7400):
            (sessions / ("%d.json" % pid)).write_text(json.dumps({"pid": pid, "status": "idle"}))
        roll = self.f.home / "rollout.jsonl"
        roll.write_text(json.dumps({"timestamp": "2026-11-10T00:00:00.000Z", "type": "event_msg",
                                    "payload": {"type": "task_complete", "turn_id": "t"}}) + "\n")
        st = self.f.read()
        for wid in ("@1", "@4"):
            st["windows"][wid]["opts"]["@agent_rollout"] = str(roll)
        self.f.state.write_text(json.dumps(st))
        s = self.run_ticks(5)
        for wid in ("@1", "@4"):
            self.assertEqual(s["windows"][wid]["opts"].get("@agent_state"), "running", wid)


if __name__ == "__main__":
    unittest.main()
