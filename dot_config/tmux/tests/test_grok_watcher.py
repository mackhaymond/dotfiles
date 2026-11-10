"""agent-tab-watcher.sh: Grok Build as an agent kind - presence (comm `grok`
or the real binary's `grok-macos-aarch64`, interactive TUI only: `grok -p`,
`grok agent stdio`, `grok leader ...`, `grok sessions` in a shell are not
agents, unless the pid has a session record: ps loses argv quoting), the
@agent_kind seed, and the stuck-`running` reconcile against Grok's own
records (grok_status):
    $GROK_HOME/active_sessions.json            pid -> session id
    $GROK_HOME/sessions/<cwd>/<sid>/events.jsonl  its last line (turn_ended =
                                               idle, in-turn events = busy)

Reuses test_agent_jump_watcher's fake-tmux harness (isolated HOME and TMPDIR,
fake `tmux`, `pgrep`, `ps`; the fake ps answers the argv look from
$FAKE_ARGS), plus a logging `tail` to count the events reads. No real tmux
server, watcher, grok or GUI is touched. Run with unittest.
"""
import json
import os
from pathlib import Path
import subprocess
import time
import unittest

import test_agent_jump_watcher as jw   # module only, for the shared fakes

# grok_subcommand + grok_tui_args extracted from the watcher, run on their own.
EXTRACT = ('eval "$(awk \'/^(grok_subcommand|grok_tui_args)\\(\\) \\{/{f=1} f{print} '
           'f&&/^\\}/{f=0}\' "$1")"\n')


def tui(args_line):
    """0 = the TUI, 1 = firmly not (headless/help), 2 = a subcommand word."""
    r = subprocess.run(["bash", "-c", EXTRACT + 'grok_tui_args "$2"', "t", str(jw.WATCHER), args_line],
                       capture_output=True, text=True, timeout=10)
    assert r.returncode in (0, 1, 2), r.stderr
    return r.returncode


class TuiArgsTests(unittest.TestCase):
    def test_interactive_tui_forms_are_agents(self):
        for a in ("grok", "/Users/x/.grok/bin/grok", "/Users/x/.grok/downloads/grok-macos-aarch64",
                  "grok fix the roast bot", "grok -c", "grok --continue", "grok -r 01a19d40",
                  "grok -r", "grok --resume", "grok -r 01a fix it", "grok dashboard",
                  "grok -m grok-4.7", "grok --model=grok-4.7 hello", "grok -s 01abc",
                  "grok -w", "grok -w feat-x", "grok --worktree feat", "grok --cwd /tmp",
                  "grok --permission-mode plan --effort high", "grok -- sessions",
                  "grok -- -p", "grok --yolo", "grok -c --add-dir ../lib"):
            self.assertEqual(tui(a), 0, a)

    def test_headless_flags_are_firmly_not_agents(self):
        for a in ("grok -p x", "grok -p", "grok --single hi", "grok --prompt-file f.md",
                  "grok --prompt-file=f.md", "grok --prompt-json j", "grok -c -p hi",
                  "grok fix -p", "grok -h", "grok --help", "grok --help=all", "grok -v",
                  "grok --version", "grok --version=x", "grok sessions -p x"):
            self.assertEqual(tui(a), 1, a)

    def test_subcommand_words_are_probably_not_agents(self):
        # 2: the watcher still asks for a session record (ps lost the quoting
        # of `grok "help me fix X"`).
        for a in ("grok agent stdio", "grok agent serve", "grok agent headless",
                  "grok leader list", "grok worker", "grok sessions", "grok sessions list",
                  "grok -m grok-4 sessions", "grok --cwd /tmp export x", "grok -r sessions",
                  "grok -w doctor", "grok v", "grok version", "grok mcp list", "grok update",
                  "grok login", "grok du", "grok disk-usage", "grok models", "grok plugin list",
                  "grok worktree list", "grok wrap", "grok trace x", "grok completions zsh",
                  "grok help me fix X"):
            self.assertEqual(tui(a), 2, a)

    def test_double_dash_after_a_subcommand_keeps_the_subcommand(self):
        # GLUED SHORT FLAGS (v3): `--` only ends option scanning; the first
        # bare word before it decides.
        for a in ("grok mcp add foo npx -- -y @x/srv", "grok sessions --", "grok -m x mcp add y -- z"):
            self.assertEqual(tui(a), 2, a)
        for a in ("grok --", "grok -- mcp add", "grok -- help", "grok fix it -- now"):
            self.assertEqual(tui(a), 0, a)

    def test_glued_short_flags(self):
        # clap takes a short flag's value glued on: -p's makes it headless,
        # and -h / -v clusters are help / version.
        for a in ("grok -phi", "grok -p=hi", "grok -c -pfix", "grok -hv", "grok -h=x", "grok -vv",
                  "grok -version"):
            self.assertEqual(tui(a), 1, a)
        for a in ("grok -c", "grok -m=grok-4", "grok -r=01a", "grok --pretty"):
            self.assertEqual(tui(a), 0, a)


TUI_TTY, P_TTY, STDIO_TTY, LEADER_TTY, SESS_TTY, REAL_TTY, PLAIN_TTY, CLAUDE_TTY, MCP_TTY = (
    "/dev/ttys920", "/dev/ttys921", "/dev/ttys922", "/dev/ttys923", "/dev/ttys924",
    "/dev/ttys925", "/dev/ttys926", "/dev/ttys927", "/dev/ttys929")
PROCS = ("ttys920 7100 /Users/x/.grok/bin/grok\n"
         "ttys921 7200 grok\n"
         "ttys922 7300 grok\n"
         "ttys923 7400 /Users/x/.grok/bin/grok\n"
         "ttys924 7500 grok\n"
         "ttys925 7600 /Users/x/.grok/downloads/grok-macos-aarch64\n"
         "ttys926 7700 agent\n"
         "ttys927 4242 claude\n"
         "ttys929 7900 grok-mcp-server\n")
ARGS = ("7100 /Users/x/.grok/bin/grok -c\n"
        "7200 grok -p x\n"
        "7300 grok agent stdio\n"
        "7400 /Users/x/.grok/bin/grok leader list\n"
        "7500 grok sessions\n"
        "7600 /Users/x/.grok/downloads/grok-macos-aarch64\n"
        "7700 agent\n"
        "7900 grok-mcp-server\n")
# `grok "help me fix X"` as ps shows it: the quoting is gone.
HELP_TTY = "/dev/ttys930"
HELP_PROC = "ttys930 8100 /Users/x/.grok/bin/grok\n"
HELP_ARGS = "8100 /Users/x/.grok/bin/grok help me fix X\n"
SID = "01a19d40-92df-7fb3-b8b9-334d0ac836b8"
CWD = "/Users/x/code/proj"

# A logging pass-through `tail`: one line per call, so a test can count the
# events.jsonl reads the watcher makes.
FAKE_TAIL = r"""#!/bin/sh
[ -n "$FAKE_TAIL_LOG" ] && echo "$*" >> "$FAKE_TAIL_LOG"
exec /usr/bin/tail "$@"
"""


def ev(kind, **extra):
    d = {"ts": "2026-11-02T17:38:39.769Z", "type": kind}
    d.update(extra)
    return json.dumps(d, separators=(",", ":")) + "\n"


STARTED = ev("turn_started", session_id=SID, turn_number=1, session_relationship="primary")
ENDED = ev("turn_ended", outcome="completed")
PHASE = ev("phase_changed", phase="waiting_for_model")
MCP_UP = ev("mcp_server_connected", server="codegraph")


class GrokWatcherTests(unittest.TestCase):
    def make(self, windows, panes):
        self.f = jw.FakeEnv(windows=windows, panes=panes)
        self.f.procs.write_text(PROCS)
        self.args = self.f.dir / "args"; self.args.write_text(ARGS)
        self.args_log = self.f.dir / "args.log"
        self.tail_log = self.f.dir / "tail.log"
        t = self.f.bin / "tail"; t.write_text(FAKE_TAIL); t.chmod(0o755)
        return self.f

    def tearDown(self):
        self.f.close()

    def env(self, n, **extra):
        return dict(AGENT_TAB_WATCHER_MAX_TICKS=str(n), FAKE_ARGS=str(self.args),
                    FAKE_ARGS_LOG=str(self.args_log), FAKE_TAIL_LOG=str(self.tail_log), **extra)

    def run_ticks(self, n, **extra):
        r = self.f.run("agent-tab-watcher.sh", timeout=10 + 2 * n, **self.env(n, **extra))
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.f.read()

    def session(self, events, pid=7100, sid=SID, cwd=CWD, root=None):
        """active_sessions.json (Grok's pretty-printed shape) + the events file."""
        g = Path(root) if root else self.f.home / ".grok"
        g.mkdir(parents=True, exist_ok=True)
        a = g / "active_sessions.json"
        rows = json.loads(a.read_text()) if a.exists() else [
            {"session_id": "01other", "pid": 99999, "pid_start": 1, "cwd": "/x",
             "opened_at": "2026-11-02T17:00:00Z"}]
        rows.append({"session_id": sid, "pid": pid, "pid_start": 1793639615200652, "cwd": cwd,
                     "opened_at": "2026-11-02T17:13:37.405728Z"})
        a.write_text(json.dumps(rows, indent=2))
        d = g / "sessions" / cwd.replace("/", "%2F") / sid
        d.mkdir(parents=True, exist_ok=True)
        p = d / "events.jsonl"
        p.write_text(events)
        return p

    def record(self, pid):
        """grok-session-track.sh's resurrect record for pid."""
        p = self.f.tmp / "tmux-assistant-resurrect" / ("grok-%d.json" % pid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"tool": "grok", "pid": pid, "session_id": SID}))
        return p

    def gk_window(self, idx, state, since, **extra):
        opts = {"@agent_state": state, "@agent_since": "%d %s" % (since, state), "@agent_kind": "grok"}
        opts.update(extra)
        return jw.W("main", idx, **opts)

    def calls(self, s, wid):
        return [c for c in s["calls"] if c[0] == "set-option" and wid in c]

    def tails(self):
        return self.tail_log.read_text().splitlines() if self.tail_log.exists() else []

    def help_tab(self):
        self.make({"@1": jw.W("main", 1)}, [{"window": "@1", "tty": HELP_TTY}])
        self.f.procs.write_text(HELP_PROC)
        self.args.write_text(HELP_ARGS)

    # --- presence ---

    def test_tui_is_seeded_as_grok_and_commands_are_shells(self):
        ttys = (TUI_TTY, P_TTY, STDIO_TTY, LEADER_TTY, SESS_TTY, REAL_TTY, PLAIN_TTY, MCP_TTY)
        self.make({"@%d" % i: jw.W("main", i) for i in range(1, 9)},
                  [{"window": "@%d" % i, "tty": t} for i, t in enumerate(ttys, 1)])
        s = self.run_ticks(3)
        w = s["windows"]
        for wid in ("@1", "@6"):   # `grok -c`, and the real binary by its own path
            self.assertEqual(w[wid]["opts"].get("@agent_state"), "idle", wid)
            self.assertEqual(w[wid]["opts"].get("@agent_kind"), "grok", wid)
        # -p, agent stdio, leader, sessions, `agent`, `grok-mcp-server`
        for wid in ("@2", "@3", "@4", "@5", "@7", "@8"):
            self.assertNotIn("@agent_state", w[wid]["opts"], wid)
            self.assertNotIn("@agent_kind", w[wid]["opts"], wid)
            self.assertFalse(self.calls(s, wid), s["calls"])
        # One argv look per NEW grok pid, cached across ticks (agent and not);
        # `agent` (7700) and `grok-mcp-server` (7900) are never candidates.
        self.assertEqual(sorted(self.args_log.read_text().split()),
                         ["7100", "7200", "7300", "7400", "7500", "7600"])
        # A settled idle grok window reads no Grok record at all.
        self.assertEqual(self.tails(), [])

    def test_gc_after_exit(self):
        self.make({"@1": jw.W("main", 1)}, [{"window": "@1", "tty": TUI_TTY}])
        s = self.run_ticks(2)
        self.assertEqual(s["windows"]["@1"]["opts"].get("@agent_kind"), "grok")
        st = self.f.read()
        st["windows"]["@1"]["opts"]["@agent_summary"] = "proj/Fix it"
        self.f.state.write_text(json.dumps(st))
        self.f.procs.write_text("ttys920 7101 zsh\n")       # grok exited
        s = self.run_ticks(4)
        self.assertEqual(s["windows"]["@1"]["opts"].get("@agent_state"), "idle")   # GC_TICKS hysteresis
        s = self.run_ticks(6)
        o = s["windows"]["@1"]["opts"]
        for name in ("@agent_state", "@agent_kind", "@agent_summary"):
            self.assertNotIn(name, o, o)

    def test_transient_empty_argv_is_retried_next_tick(self):
        self.make({"@1": jw.W("main", 1)}, [{"window": "@1", "tty": TUI_TTY}])
        s = self.run_ticks(2, FAKE_ARGS_EMPTY_FIRST="7100")
        o = s["windows"]["@1"]["opts"]
        self.assertEqual((o.get("@agent_state"), o.get("@agent_kind")), ("idle", "grok"))

    # --- a subcommand-looking argv with a session record (TUI v2) ---

    def test_prompt_that_looks_like_a_subcommand_with_a_record_is_an_agent(self):
        self.help_tab()
        self.record(8100)
        o = self.run_ticks(2)["windows"]["@1"]["opts"]
        self.assertEqual((o.get("@agent_state"), o.get("@agent_kind")), ("idle", "grok"))

    def test_active_sessions_entry_counts_as_a_record(self):
        self.help_tab()
        self.session(STARTED + ENDED, pid=8100)
        o = self.run_ticks(2)["windows"]["@1"]["opts"]
        self.assertEqual(o.get("@agent_kind"), "grok")

    def test_same_argv_without_a_record_is_a_shell(self):
        self.help_tab()
        s = self.run_ticks(3)
        self.assertNotIn("@agent_state", s["windows"]["@1"]["opts"])
        self.assertFalse(self.calls(s, "@1"), s["calls"])

    def test_record_written_later_makes_it_an_agent_without_another_ps(self):
        # SessionStart lands a beat after the process: the first look says
        # "probably a command", a later tick finds the record ([ -f ], no ps).
        self.help_tab()
        p = subprocess.Popen(["bash", str(self.f.scripts / "agent-tab-watcher.sh")],
                             env=self.f.env(**self.env(6)), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not (self.args_log.exists()
                                                  and "8100" in self.args_log.read_text()):
                time.sleep(0.05)
            time.sleep(1.2)
            self.assertNotIn("@agent_state", self.f.read()["windows"]["@1"]["opts"])
            self.record(8100)
            _, err = p.communicate(timeout=30)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertEqual(p.returncode, 0, err)
        o = self.f.read()["windows"]["@1"]["opts"]
        self.assertEqual((o.get("@agent_state"), o.get("@agent_kind")), ("idle", "grok"))
        self.assertEqual(self.args_log.read_text().split(), ["8100"])

    def test_headless_flag_stays_a_shell_even_with_a_record(self):
        self.make({"@2": jw.W("main", 2)}, [{"window": "@2", "tty": P_TTY}])
        self.record(7200)
        s = self.run_ticks(2)
        self.assertNotIn("@agent_state", s["windows"]["@2"]["opts"])

    # --- the events.jsonl reconcile ---

    def test_running_to_idle_after_three_ticks_when_last_event_is_turn_ended(self):
        now = int(time.time())
        self.make({"@1": self.gk_window(1, "running", now - 60, **{
            "@agent_detail_kind": "run", "@agent_detail": "go", "@agent_pending": str(now - 5)})},
            [{"window": "@1", "tty": TUI_TTY}])
        self.session(STARTED + PHASE * 3 + ENDED)
        s = self.run_ticks(2)
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "running")   # hysteresis
        s = self.run_ticks(3)
        o = s["windows"]["@1"]["opts"]
        self.assertEqual(o["@agent_state"], "idle")
        self.assertRegex(o["@agent_since"], r"^\d+ idle$")
        # State only, as Claude's and OpenCode's rule: detail and a fresh pending stay.
        self.assertEqual((o["@agent_detail"], o["@agent_kind"], o["@agent_pending"]),
                         ("go", "grok", str(now - 5)))

    def test_record_session_id_stands_in_for_an_empty_active_sessions(self):
        # active_sessions.json has been seen `[]` while a TUI ran: the pid's
        # session record (grok-<pid>.json) names the session instead.
        now = int(time.time())
        self.make({"@1": self.gk_window(1, "running", now - 60)}, [{"window": "@1", "tty": TUI_TTY}])
        self.session(STARTED + PHASE + ENDED)
        (self.f.home / ".grok" / "active_sessions.json").write_text("[]\n")
        s = self.run_ticks(5)
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "running", "no record: unknown")
        self.record(7100)
        s = self.run_ticks(5)
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "idle")

    def test_running_stays_when_last_event_is_turn_started(self):
        now = int(time.time())
        self.make({"@1": self.gk_window(1, "running", now - 60)}, [{"window": "@1", "tty": TUI_TTY}])
        p = self.session(STARTED + ENDED + STARTED)
        s = self.run_ticks(5)
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "running")
        self.assertFalse([c for c in s["calls"] if "@agent_state" in c], s["calls"])
        # Fork-light: the file did not move, so one small tail for five ticks.
        self.assertEqual(self.tails(), ["-c 4096 %s" % p])

    def test_long_file_reads_only_the_last_line(self):
        # A turn_ended at the end of a file far bigger than any read: idle,
        # from one 4 KB tail a look.
        now = int(time.time())
        self.make({"@1": self.gk_window(1, "running", now - 60)}, [{"window": "@1", "tty": TUI_TTY}])
        self.session(STARTED + PHASE * 2000 + ENDED)
        s = self.run_ticks(4)
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "idle")
        self.assertTrue(self.tails() and all(t.startswith("-c 4096 ") for t in self.tails()),
                        self.tails())

    def test_in_turn_last_line_is_busy_and_session_start_or_no_record_is_unknown(self):
        now = int(time.time())
        self.make({}, [])
        self.f.procs.write_text(PROCS + "ttys928 7800 grok\n")
        self.args.write_text(ARGS + "7800 grok\n")
        st = self.f.read()
        st["windows"] = {"@1": self.gk_window(1, "running", now - 60),
                         "@6": self.gk_window(6, "running", now - 60),
                         "@8": self.gk_window(8, "running", now - 60)}
        st["panes"] = [{"window": "@1", "tty": TUI_TTY}, {"window": "@6", "tty": REAL_TTY},
                       {"window": "@8", "tty": "/dev/ttys928"}]
        self.f.state.write_text(json.dumps(st))
        # 7100: a long turn, its turn_started beyond any tail; 7600: only MCP
        # servers starting; 7800: not in active_sessions.json at all.
        self.session(ENDED + STARTED + PHASE * 2000)
        self.session(MCP_UP * 3, pid=7600, sid="01sid7600")
        s = self.run_ticks(5)
        for wid in ("@1", "@6", "@8"):
            self.assertEqual(s["windows"][wid]["opts"]["@agent_state"], "running", wid)

    def test_unknown_last_event_falls_back_to_the_scan(self):
        # A type this does not know after the turn_ended: the awk scan of
        # the bigger tail finds the turn_ended -> idle (a second, bigger tail).
        now = int(time.time())
        self.make({"@1": self.gk_window(1, "running", now - 60)}, [{"window": "@1", "tty": TUI_TTY}])
        self.session(STARTED + ENDED + ev("brand_new_event"))
        s = self.run_ticks(4)
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "idle")
        self.assertEqual([t.split()[1] for t in self.tails()], ["4096", "65536"])

    def test_grok_home_is_honoured(self):
        now = int(time.time())
        self.make({"@1": self.gk_window(1, "running", now - 60)}, [{"window": "@1", "tty": TUI_TTY}])
        alt = self.f.dir / "grokhome"
        self.session(STARTED + ENDED, root=alt)
        self.session(STARTED)                    # the default location says busy: not read
        s = self.run_ticks(4, GROK_HOME=str(alt))
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "idle")

    def test_needs_input_is_never_cleared(self):
        now = int(time.time())
        self.make({"@1": self.gk_window(1, "needs-input", now - 60, **{"@agent_pending": "1"})},
                  [{"window": "@1", "tty": TUI_TTY}])
        self.session(STARTED + ENDED)
        s = self.run_ticks(5)
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "needs-input")
        self.assertEqual(self.tails(), [])

    def test_grok_never_reads_claude_session_or_codex_rollout(self):
        now = int(time.time())
        self.make({}, [])
        rp = self.f.home / ".codex/sessions/r.jsonl"
        rp.parent.mkdir(parents=True)
        rp.write_text('{"timestamp":"2026-10-08T00:00:00.000Z","ordinal":1,"type":"event_msg",'
                      '"payload":{"type":"task_complete","turn_id":"t"}}\n')
        (self.f.home / ".claude/sessions").mkdir(parents=True)
        (self.f.home / ".claude/sessions/7100.json").write_text('{"pid":7100,"status":"idle"}')
        st = self.f.read()
        st["windows"] = {"@1": self.gk_window(1, "running", now - 60, **{"@agent_rollout": str(rp)})}
        st["panes"] = [{"window": "@1", "tty": TUI_TTY}]
        self.f.state.write_text(json.dumps(st))
        self.session(STARTED)
        s = self.run_ticks(5)
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "running")

    # --- expired pending ---

    def test_expired_pending_of_a_busy_turn_becomes_running(self):
        old = str(int(time.time()) - 700)
        self.make({"@1": self.gk_window(1, "idle", int(old), **{"@agent_pending": old})},
                  [{"window": "@1", "tty": TUI_TTY}])
        self.session(ENDED + STARTED + PHASE)
        o = self.run_ticks(1)["windows"]["@1"]["opts"]
        self.assertEqual(o["@agent_state"], "running")
        self.assertNotIn("@agent_pending", o)

    def test_expired_pending_of_a_long_turn_becomes_running(self):
        # The turn_started is ~200 KB back; the in-turn last line says busy.
        old = str(int(time.time()) - 700)
        self.make({"@1": self.gk_window(1, "idle", int(old), **{"@agent_pending": old})},
                  [{"window": "@1", "tty": TUI_TTY}])
        self.session(ENDED + STARTED + PHASE * 3000 + ev("tool_started", tool="run_terminal_command"))
        o = self.run_ticks(1)["windows"]["@1"]["opts"]
        self.assertEqual(o["@agent_state"], "running")

    def test_expired_pending_of_a_finished_turn_is_just_dropped(self):
        old = str(int(time.time()) - 700)
        self.make({"@1": self.gk_window(1, "idle", int(old), **{"@agent_pending": old})},
                  [{"window": "@1", "tty": TUI_TTY}])
        self.session(STARTED + ENDED)
        o = self.run_ticks(2)["windows"]["@1"]["opts"]
        self.assertEqual(o["@agent_state"], "idle")
        self.assertNotIn("@agent_pending", o)

    # --- child subagents still out (GROK SUBAGENTS HOLD A TAB TOO) ---
    #
    # agent-bg-pending is a stub that logs its argv and exits with the
    # number in $FAKE_BG_RC_FILE: `--grok-session <sid> [cwd]`, exit 0 (stdout
    # the count) while a child is out, 1 for none.

    BG_STUB = ('#!/bin/sh\necho "$*" >> "$FAKE_BG_LOG"\nrc=$(cat "$FAKE_BG_RC_FILE")\n'
               '[ "$rc" = 0 ] && echo 2\nexit "$rc"\n')

    def held_tab(self, rc, children=True):
        now = int(time.time())
        self.make({"@1": self.gk_window(1, "running", now - 60, **{"@agent_bg_owner": "user"})},
                  [{"window": "@1", "tty": TUI_TTY}])
        p = self.session(STARTED + PHASE + ENDED)
        self.child = p.parent / "subagents" / "01child-1"
        if children:
            self.child.mkdir(parents=True)
            (self.child / "meta.json").write_text("{}")
        stub = self.f.home / ".local/bin/agent-bg-pending"
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text(self.BG_STUB)
        stub.chmod(0o755)
        self.bg_log = self.f.dir / "bg.log"
        self.bg_rc = self.f.dir / "bg.rc"
        self.bg_rc.write_text(str(rc))
        return p

    def bg_env(self):
        return dict(FAKE_BG_LOG=str(self.bg_log), FAKE_BG_RC_FILE=str(self.bg_rc))

    def asks(self):
        return self.bg_log.read_text().splitlines() if self.bg_log.exists() else []

    def test_child_out_holds_a_turn_ended_tab_running_with_gear(self):
        self.held_tab(rc=0)
        o = self.run_ticks(6, **self.bg_env())["windows"]["@1"]["opts"]
        self.assertEqual(o["@agent_state"], "running")
        self.assertEqual(o.get("@agent_workflow"), "1")
        self.assertEqual(o.get("@agent_bg_owner"), "user")
        # The parent's session id and its decoded cwd - and cached: nothing
        # moved, so once for all six ticks.
        self.assertEqual(self.asks(), ["--grok-session %s %s" % (SID, CWD)])

    def test_parent_events_appends_alone_do_not_re_ask(self):
        # A mid-turn parent appends to its events.jsonl all the time; the
        # verdict does not read it, so that alone must not fork the program
        # again (only subagents/, a child's dir or meta.json, or the
        # STAMP_RING expiry, which 7 ticks stay inside).
        ev_path = self.held_tab(rc=0)
        env = self.f.env(**self.env(7, **self.bg_env()))
        p = subprocess.Popen(["bash", str(self.f.scripts / "agent-tab-watcher.sh")], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 15
            while len(self.asks()) < 1 and time.monotonic() < deadline:
                time.sleep(.1)
            while p.poll() is None:
                with ev_path.open("a") as fh:
                    fh.write(PHASE)
                time.sleep(.3)
            _, err = p.communicate(timeout=30)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(len(self.asks()), 1, self.asks())
        o = self.f.read()["windows"]["@1"]["opts"]
        self.assertEqual((o["@agent_state"], o.get("@agent_workflow")), ("running", "1"))

    def test_no_child_out_idles_and_releases(self):
        self.held_tab(rc=1)
        o = self.run_ticks(5, **self.bg_env())["windows"]["@1"]["opts"]
        self.assertEqual(o["@agent_state"], "idle")
        self.assertNotIn("@agent_workflow", o)
        self.assertNotIn("@agent_bg_owner", o)
        # A "none" holds while nothing moves: one ask.
        self.assertEqual(len(self.asks()), 1, self.asks())

    def test_no_subagents_dir_never_asks(self):
        self.held_tab(rc=0, children=False)
        o = self.run_ticks(5, **self.bg_env())["windows"]["@1"]["opts"]
        self.assertEqual(o["@agent_state"], "idle")
        self.assertEqual(self.asks(), [])

    def test_child_finishes_then_idles(self):
        # ONE watcher run: held on the cached yes until the child's dir moves
        # (it writes output.json as it ends); then asked again, and idled.
        self.held_tab(rc=0)
        env = self.f.env(**self.env(9, **self.bg_env()))
        p = subprocess.Popen(["bash", str(self.f.scripts / "agent-tab-watcher.sh")], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 15
            while len(self.asks()) < 1 and time.monotonic() < deadline:
                time.sleep(.1)
            time.sleep(3.5)                     # past the 3-tick un-stick, still held
            o = self.f.read()["windows"]["@1"]["opts"]
            self.assertEqual((o["@agent_state"], o.get("@agent_workflow")), ("running", "1"))
            self.bg_rc.write_text("1")
            (self.child / "output.json").write_text("{}")
            _, err = p.communicate(timeout=30)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertEqual(p.returncode, 0, err)
        o = self.f.read()["windows"]["@1"]["opts"]
        self.assertEqual(o["@agent_state"], "idle")
        self.assertNotIn("@agent_workflow", o)
        self.assertNotIn("@agent_bg_owner", o)
        self.assertEqual(len(self.asks()), 2, self.asks())

    def test_child_out_lights_the_gear_on_an_idle_tab(self):
        self.held_tab(rc=0)
        st = self.f.read()
        st["windows"]["@1"] = self.gk_window(1, "idle", int(time.time()) - 60)
        self.f.state.write_text(json.dumps(st))
        o = self.run_ticks(3, **self.bg_env())["windows"]["@1"]["opts"]
        self.assertEqual((o["@agent_state"], o.get("@agent_workflow")), ("idle", "1"))

    def test_claude_reconcile_unchanged(self):
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
