"""agent-tab-watcher.sh: OpenCode as an agent kind - presence (comm `opencode`,
interactive TUI only: `opencode serve|web|mcp|...` in a shell is not an agent),
the @agent_kind seed, and the stuck-`running` reconcile against the status
file the OpenCode plugin keeps:
    ${XDG_STATE_HOME:-$HOME/.local/state}/tmux-agent/opencode/<pid>.status

Reuses test_agent_jump_watcher's fake-tmux harness (isolated HOME and TMPDIR,
fake `tmux`, `pgrep`, `ps`; the fake ps answers the opencode argv look from
$FAKE_ARGS). No real tmux server, watcher or GUI is touched. Run with unittest.
"""
import json
import os
from pathlib import Path
import subprocess
import time
import unittest

import test_agent_jump_watcher as jw   # module only, for the shared fakes

# opencode_tui_args extracted from the watcher, run on its own.
EXTRACT = 'eval "$(awk \'/^opencode_tui_args\\(\\) \\{/{f=1} f{print} f&&/^\\}/{exit}\' "$1")"\n'


def tui(args_line):
    r = subprocess.run(["bash", "-c", EXTRACT + 'opencode_tui_args "$2"', "t", str(jw.WATCHER), args_line],
                       capture_output=True, text=True, timeout=10)
    assert r.returncode in (0, 1), r.stderr
    return r.returncode == 0


class TuiArgsTests(unittest.TestCase):
    def test_interactive_tui_forms_are_agents(self):
        for a in ("opencode", "/opt/homebrew/bin/opencode", "opencode --port 0",
                  "opencode --port 0 -c", "opencode -s ses_abc123", "opencode --session ses_x --fork",
                  "opencode /Users/x/code/proj", "opencode .", "opencode --mini",
                  "opencode -m openai/gpt-5 --agent build", "opencode --log-level DEBUG ~/proj",
                  "opencode --print-logs", "opencode pr 123", "opencode --prompt=fix -c",
                  "opencode --prompt hello", "opencode -- serve"):
            self.assertTrue(tui(a), a)

    def test_subcommands_are_not_agents(self):
        for a in ("opencode serve", "opencode serve --port 4096", "opencode --port 4096 serve",
                  "/opt/homebrew/bin/opencode web", "opencode mcp list", "opencode debug config",
                  "opencode acp", "opencode run hello there", "opencode -m a/b run hi",
                  "opencode attach http://127.0.0.1:4096", "opencode export ses_x",
                  "opencode stats", "opencode models", "opencode auth login", "opencode db",
                  "opencode session list", "opencode upgrade", "opencode github install",
                  "opencode --log-level INFO serve", "opencode --hostname 0.0.0.0 web"):
            self.assertFalse(tui(a), a)


OC_TTY, SERVE_TTY, PLAIN_TTY, CLAUDE_TTY = "/dev/ttys910", "/dev/ttys911", "/dev/ttys912", "/dev/ttys913"
PROCS = ("ttys910 6100 /opt/homebrew/bin/opencode\n"
         "ttys911 6200 opencode\n"
         "ttys912 6300 zsh\n"
         "ttys913 4242 claude\n")
ARGS = ("6100 /opt/homebrew/bin/opencode --port 0 -c\n"
        "6200 opencode serve --port 4096\n")


class OpencodeWatcherTests(unittest.TestCase):
    def make(self, windows, panes):
        self.f = jw.FakeEnv(windows=windows, panes=panes)
        self.f.procs.write_text(PROCS)
        self.args = self.f.dir / "args"; self.args.write_text(ARGS)
        self.args_log = self.f.dir / "args.log"
        return self.f

    def tearDown(self):
        self.f.close()

    def run_ticks(self, n, **extra):
        r = self.f.run("agent-tab-watcher.sh", timeout=10 + 2 * n,
                       AGENT_TAB_WATCHER_MAX_TICKS=str(n), FAKE_ARGS=str(self.args),
                       FAKE_ARGS_LOG=str(self.args_log), **extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.f.read()

    def status(self, pid, text, base=None):
        d = Path(base) if base else self.f.home / ".local/state"
        p = d / "tmux-agent/opencode" / ("%d.status" % pid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def oc_window(self, idx, state, since, **extra):
        opts = {"@agent_state": state, "@agent_since": "%d %s" % (since, state), "@agent_kind": "opencode"}
        opts.update(extra)
        return jw.W("main", idx, **opts)

    def calls(self, s, wid):
        return [c for c in s["calls"] if c[0] == "set-option" and wid in c]

    # --- presence ---

    def test_tui_is_seeded_as_opencode_and_serve_is_a_shell(self):
        self.make({"@1": jw.W("main", 1), "@2": jw.W("main", 2), "@3": jw.W("main", 3)},
                  [{"window": "@1", "tty": OC_TTY}, {"window": "@2", "tty": SERVE_TTY},
                   {"window": "@3", "tty": PLAIN_TTY}])
        s = self.run_ticks(3)
        w = s["windows"]
        self.assertEqual(w["@1"]["opts"].get("@agent_state"), "idle")
        self.assertEqual(w["@1"]["opts"].get("@agent_kind"), "opencode")
        self.assertNotIn("@agent_state", w["@2"]["opts"])
        self.assertNotIn("@agent_kind", w["@2"]["opts"])
        self.assertFalse(self.calls(s, "@2"), s["calls"])
        self.assertFalse(self.calls(s, "@3"), s["calls"])
        # The argv look is one ps per NEW opencode pid, cached across ticks
        # (both verdicts: the agent and the not-an-agent).
        self.assertEqual(sorted(self.args_log.read_text().split()), ["6100", "6200"])

    def test_serve_window_left_with_a_state_is_collected(self):
        # A tab that WAS an agent and now runs `opencode serve`: no agent pane,
        # so the GC takes it after GC_TICKS like any other exit.
        self.make({"@2": self.oc_window(2, "idle", 5, **{"@agent_summary": "p/x"})},
                  [{"window": "@2", "tty": SERVE_TTY}])
        s = self.run_ticks(6)
        o = s["windows"]["@2"]["opts"]
        for name in ("@agent_state", "@agent_kind", "@agent_summary"):
            self.assertNotIn(name, o, o)

    def test_vanished_argv_is_not_an_agent(self):
        # ps -o args finds nothing (the pid exited between pgrep and ps).
        self.make({"@1": jw.W("main", 1)}, [{"window": "@1", "tty": OC_TTY}])
        self.args.write_text("")
        s = self.run_ticks(2)
        self.assertNotIn("@agent_state", s["windows"]["@1"]["opts"])

    # --- the status-file reconcile ---

    def test_running_to_idle_after_three_idle_ticks(self):
        now = int(time.time())
        self.make({"@1": self.oc_window(1, "running", now - 60, **{
            "@agent_detail_kind": "run", "@agent_detail": "go", "@agent_pending": str(now - 5)})},
            [{"window": "@1", "tty": OC_TTY}])
        self.status(6100, "idle\n")
        s = self.run_ticks(2)
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "running")   # hysteresis
        s = self.run_ticks(3)
        o = s["windows"]["@1"]["opts"]
        self.assertEqual(o["@agent_state"], "idle")
        self.assertRegex(o["@agent_since"], r"^\d+ idle$")
        # State only, as Claude's rule: the detail and a fresh pending stay.
        self.assertEqual((o["@agent_detail"], o["@agent_kind"], o["@agent_pending"]),
                         ("go", "opencode", str(now - 5)))

    def test_xdg_state_home_is_honoured(self):
        now = int(time.time())
        self.make({"@1": self.oc_window(1, "running", now - 60)}, [{"window": "@1", "tty": OC_TTY}])
        xdg = self.f.dir / "xdg"
        self.status(6100, "idle", base=xdg)
        self.status(6100, "busy")            # the default location says busy: not read
        s = self.run_ticks(4, XDG_STATE_HOME=str(xdg))
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "idle")

    def test_busy_missing_or_junk_status_keeps_running(self):
        now = int(time.time())
        self.make({}, [])
        # Second and third opencode TUIs for the missing / junk files.
        self.f.procs.write_text(PROCS + "ttys914 6400 opencode\nttys915 6500 opencode\n")
        self.args.write_text(ARGS + "6400 opencode\n6500 opencode\n")
        st = self.f.read()
        st["windows"] = {"@1": self.oc_window(1, "running", now - 60),
                         "@4": self.oc_window(4, "running", now - 60),
                         "@5": self.oc_window(5, "running", now - 60)}
        st["panes"] = [{"window": "@1", "tty": OC_TTY}, {"window": "@4", "tty": "/dev/ttys914"},
                       {"window": "@5", "tty": "/dev/ttys915"}]
        self.f.state.write_text(json.dumps(st))
        self.status(6100, "busy")
        self.status(6500, "idle-ish")
        s = self.run_ticks(5)
        for wid in ("@1", "@4", "@5"):
            self.assertEqual(s["windows"][wid]["opts"]["@agent_state"], "running", wid)
        self.assertFalse([c for c in s["calls"] if "@agent_state" in c], s["calls"])

    def test_needs_input_is_never_cleared_by_the_status_file(self):
        now = int(time.time())
        self.make({"@1": self.oc_window(1, "needs-input", now - 60, **{"@agent_pending": "1"})},
                  [{"window": "@1", "tty": OC_TTY}])
        self.status(6100, "idle")
        s = self.run_ticks(5)
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "needs-input")

    def test_opencode_never_reads_claude_session_or_codex_rollout(self):
        # A Claude session file for the same pid says idle, and a stale codex
        # rollout path sits on the window: neither is opencode's record.
        now = int(time.time())
        self.make({}, [])
        rp = self.f.home / ".codex/sessions/r.jsonl"
        rp.parent.mkdir(parents=True)
        rp.write_text('{"timestamp":"2026-10-08T00:00:00.000Z","type":"event_msg",'
                      '"payload":{"type":"task_complete"}}\n')
        (self.f.home / ".claude/sessions").mkdir(parents=True)
        (self.f.home / ".claude/sessions/6100.json").write_text('{"pid":6100,"status":"idle"}')
        st = self.f.read()
        st["windows"] = {"@1": self.oc_window(1, "running", now - 60, **{"@agent_rollout": str(rp)})}
        st["panes"] = [{"window": "@1", "tty": OC_TTY}]
        self.f.state.write_text(json.dumps(st))
        self.status(6100, "busy")
        s = self.run_ticks(5)
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "running")

    def test_no_status_file_never_falls_through_to_a_stale_rollout(self):
        # No status file at all (st stays ""), and a stale codex rollout path
        # whose last marker is a task_complete: only the opencode guard on the
        # rollout branch keeps this tab running (without it: idle in 3 ticks).
        now = int(time.time())
        self.make({}, [])
        rp = self.f.home / ".codex/sessions/r.jsonl"
        rp.parent.mkdir(parents=True)
        rp.write_text('{"timestamp":"2026-10-08T00:00:00.000Z","ordinal":1,"type":"event_msg",'
                      '"payload":{"type":"task_complete","turn_id":"t"}}\n')
        st = self.f.read()
        st["windows"] = {"@1": self.oc_window(1, "running", now - 60, **{"@agent_rollout": str(rp)})}
        st["panes"] = [{"window": "@1", "tty": OC_TTY}]
        self.f.state.write_text(json.dumps(st))
        s = self.run_ticks(5)
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "running")
        self.assertFalse([c for c in s["calls"] if "@agent_state" in c], s["calls"])

    def test_transient_empty_argv_is_retried_next_tick(self):
        # The first `ps -o args` for the TUI answers nothing; that must not be
        # cached as "not an agent" - the next tick looks again and seeds it.
        self.make({"@1": jw.W("main", 1)}, [{"window": "@1", "tty": OC_TTY}])
        s = self.run_ticks(2, FAKE_ARGS_EMPTY_FIRST="6100")
        o = s["windows"]["@1"]["opts"]
        self.assertEqual((o.get("@agent_state"), o.get("@agent_kind")), ("idle", "opencode"))
        self.assertEqual(self.args_log.read_text().split().count("6100"), 2)

    def test_claude_reconcile_unchanged(self):
        # Claude's own rule still runs beside opencode's.
        now = int(time.time())
        self.make({"@3": jw.W("main", 3, **{"@agent_state": "running", "@agent_kind": "claude",
                                            "@agent_since": "%d running" % (now - 60)})},
                  [{"window": "@3", "tty": CLAUDE_TTY}])
        (self.f.home / ".claude/sessions").mkdir(parents=True)
        (self.f.home / ".claude/sessions/4242.json").write_text('{"pid":4242,"status":"idle"}')
        s = self.run_ticks(4)
        self.assertEqual(s["windows"]["@3"]["opts"]["@agent_state"], "idle")


if __name__ == "__main__":
    unittest.main()
