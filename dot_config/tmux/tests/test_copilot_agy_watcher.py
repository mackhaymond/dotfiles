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


class WatcherBase(unittest.TestCase):
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


class CopilotAgyWatcherTests(WatcherBase):
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


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".371Z"


def cp_line(typ, data, epoch):
    # Copilot's own line shape (key order as written): type, data, id,
    # timestamp, parentId. Ids redacted.
    return json.dumps({"type": typ, "data": data, "id": "00000000-0000-4000-8000-000000000000",
                       "timestamp": iso(epoch), "parentId": None}, separators=(",", ":")) + "\n"


class CopilotRecordTests(WatcherBase):
    """COPILOT ESC MID-TURN: the persisted `abort` (or a turn-ending
    session.error) in the session's events.jsonl, after the state's stamp,
    ends a running / yellow copilot turn (three ticks)."""

    def setup_copilot(self, state, stamp, tail, detail="run"):
        self.make({"@1": jw.W("main", 1, **{"@agent_state": state, "@agent_kind": "copilot",
                                             "@agent_since": "%d %s" % (stamp, state),
                                             "@agent_detail_kind": detail, "@agent_detail": "go"})},
                  [{"window": "@1", "tty": CP_TTY}])
        ev = self.f.home / ".copilot/session-state/5390b68f-fc54-4939-841f-ad0c81359870/events.jsonl"
        ev.parent.mkdir(parents=True)
        ev.write_text("".join(tail))
        st = self.f.read()
        st["windows"]["@1"]["opts"]["@agent_record"] = str(ev)
        self.f.state.write_text(json.dumps(st))
        return ev

    def turn(self, t0):
        return [cp_line("user.message", {"content": "fix the build"}, t0),
                cp_line("assistant.turn_start", {"turnId": "0", "interactionId": "x"}, t0 + 1),
                cp_line("assistant.message", {"messageId": "m", "content": "", "toolRequests": []}, t0 + 2),
                cp_line("tool.execution_start", {"toolCallId": "c", "toolName": "bash"}, t0 + 3)]

    def opts(self):
        return self.f.read()["windows"]["@1"]["opts"]

    def test_an_abort_after_the_stamp_ends_a_running_turn(self):
        now = int(time.time())
        self.setup_copilot("running", now - 60, self.turn(now - 60) + [
            cp_line("abort", {"reason": "user_initiated"}, now - 5),
            cp_line("assistant.turn_end", {"turnId": "0"}, now - 5)])
        o = self.run_ticks(2)["windows"]["@1"]["opts"]
        self.assertEqual(o["@agent_state"], "running", "three ticks of hysteresis")
        o = self.run_ticks(4)["windows"]["@1"]["opts"]
        self.assertEqual(o["@agent_state"], "idle")
        self.assertNotIn("@agent_detail_kind", o)

    def test_an_abort_before_the_stamp_is_the_previous_turns(self):
        # A new prompt whose user.message is not written yet: the last turn
        # marker is the previous turn's abort, older than the stamp.
        now = int(time.time())
        self.setup_copilot("running", now - 5, self.turn(now - 90) + [
            cp_line("abort", {"reason": "user_initiated"}, now - 60)])
        self.assertEqual(self.run_ticks(5)["windows"]["@1"]["opts"]["@agent_state"], "running")

    def test_a_turn_going_on_after_an_abort_is_not_ended(self):
        now = int(time.time())
        self.setup_copilot("running", now - 60, self.turn(now - 60) + [
            cp_line("abort", {"reason": "user_initiated"}, now - 30)] + self.turn(now - 20))
        self.assertEqual(self.run_ticks(5)["windows"]["@1"]["opts"]["@agent_state"], "running")

    def test_an_abort_ends_a_permission_prompt(self):
        now = int(time.time())
        self.setup_copilot("needs-input", now - 30, self.turn(now - 60) + [
            cp_line("permission.requested", {"requestId": "r", "permissionRequest": {"kind": "shell"}}, now - 30),
            cp_line("abort", {"reason": "user_initiated"}, now - 3)], detail="perm")
        o = self.run_ticks(5)["windows"]["@1"]["opts"]
        self.assertEqual(o["@agent_state"], "idle")
        self.assertNotIn("@agent_detail_kind", o)

    def test_a_turn_ending_error_ends_a_running_turn(self):
        # The real quota error (request ids redacted), after the turn's end.
        now = int(time.time())
        self.setup_copilot("running", now - 60, self.turn(now - 60) + [
            cp_line("assistant.turn_end", {"turnId": "0"}, now - 10),
            cp_line("session.error", {"errorType": "quota", "message": "You have exceeded your monthly quota "
                                      "(Request ID: 0000:000000:0000000:0000000:00000000)", "statusCode": 402,
                                      "errorCode": "quota_exceeded"}, now - 10)])
        self.assertEqual(self.run_ticks(5)["windows"]["@1"]["opts"]["@agent_state"], "idle")

    def test_an_auto_switch_question_is_still_the_turn(self):
        now = int(time.time())
        self.setup_copilot("running", now - 60, self.turn(now - 60) + [
            cp_line("session.error", {"errorType": "rate_limit", "message": "You've hit your rate limit.",
                                      "errorCode": "user_global_rate_limited", "eligibleForAutoSwitch": True},
                    now - 10)])
        self.assertEqual(self.run_ticks(5)["windows"]["@1"]["opts"]["@agent_state"], "running")


AGY_SCHEMA = ("CREATE TABLE `steps` (`idx` integer,`step_type` integer NOT NULL DEFAULT 0,"
              "`status` integer NOT NULL DEFAULT 0,`has_subtrajectory` numeric NOT NULL DEFAULT false,"
              "`metadata` blob,`error_details` blob,`permissions` blob,`task_details` blob,`render_info` blob,"
              "`step_payload` blob,`step_format` integer NOT NULL DEFAULT 0,PRIMARY KEY (`idx`));"
              "CREATE INDEX `idx_steps_status` ON `steps`(`status`);")


def agy_payload(tool, summary, command="uv sync"):
    # The shape of a real step_payload (protobuf around the tool call's JSON
    # args, which carry CommandLine / toolAction / toolSummary; Go's JSON
    # writes & as &); ids redacted. What follows the JSON is binary, as
    # in the real rows: invalid UTF-8 and a newline included.
    args = json.dumps({"CommandLine": command, "toolAction": "Running command", "toolSummary": summary},
                      separators=(",", ":")).replace("&", "\\u0026")
    return (b"\x08\x01\x12\x0bcall_000000\x12" + bytes([len(tool)]) + tool.encode() + b"\x1a"
            + bytes([len(args) % 256]) + args.encode() + b"2\x0c\x08\xc7\xb1\xff\xfe\n\x80\x00tail\xc3")


class AgyStepStoreTests(WatcherBase):
    """AGY'S STEP STORE: a WAITING step in the conversation db raises
    needs-input (perm) and its end lowers it; a Stop held by fullyIdle false
    is released once nothing is in flight. The db is live WAL, held open by
    a writer the whole time (as agy holds it)."""

    CID = "3814da9d-d4f7-4e84-9c03-222335c5e3b3"

    def setup_agy(self, state, detail, steps):
        import sqlite3
        now = int(time.time())
        self.make({"@1": jw.W("main", 1, **{"@agent_state": state, "@agent_kind": "agy",
                                             "@agent_since": "%d %s" % (now - 30, state),
                                             "@agent_detail_kind": detail, "@agent_detail": "go"})},
                  [{"window": "@1", "tty": AGY_TTY}])
        d = self.f.home / ".gemini/antigravity-cli/conversations"
        d.mkdir(parents=True)
        self.db = d / (self.CID + ".db")
        self.conn = sqlite3.connect(str(self.db), isolation_level=None)
        self.addCleanup(self.conn.close)
        self.conn.execute("pragma journal_mode=wal")
        self.conn.executescript(AGY_SCHEMA)
        self.conn.execute("insert into steps (idx, step_type, status) values (0, 14, 3)")   # USER_INPUT, DONE
        for idx, (typ, status, payload) in enumerate(steps, 1):
            self.conn.execute("insert into steps (idx, step_type, status, step_payload) values (?,?,?,?)",
                              (idx, typ, status, payload))
        st = self.f.read()
        st["windows"]["@1"]["opts"]["@agent_record"] = str(self.db)
        self.f.state.write_text(json.dumps(st))

    def opts(self):
        return self.f.read()["windows"]["@1"]["opts"]

    def run_ticks(self, n, **extra):
        # tmux's own locale: a UTF-8 one (the payload bytes must not break
        # the parse there).
        return super().run_ticks(n, LC_ALL="en_US.UTF-8", LANG="en_US.UTF-8", **extra)

    def sqlite_forks(self, ticks):
        """Count sqlite3 runs over `ticks` ticks (a logging wrapper on PATH)."""
        log = self.f.dir / "sqlite.log"
        w = self.f.bin / "sqlite3"
        w.write_text('#!/bin/sh\necho x >> "%s"\nexec /usr/bin/sqlite3 "$@"\n' % log)
        w.chmod(0o755)
        self.run_ticks(ticks)
        return len(log.read_text().split()) if log.exists() else 0

    def test_a_waiting_step_raises_needs_input_and_its_end_lowers_it(self):
        self.setup_agy("running", "run", [(15, 3, agy_payload("view_file", "View notes.md")),
                                          (101, 9, agy_payload("run_command", "Run uv sync"))])
        self.run_ticks(3)
        o = self.opts()
        self.assertEqual((o["@agent_state"], o["@agent_detail_kind"], o["@agent_detail"], o["@agent_ask_from"]),
                         ("needs-input", "perm", "Run uv sync", "main"))
        # Approved: the step runs (no PostToolUse until it ends).
        self.conn.execute("update steps set status = 2 where idx = 2")
        self.run_ticks(3)
        o = self.opts()
        self.assertEqual(o["@agent_state"], "running")
        self.assertNotIn("@agent_ask_from", o)

    def test_a_wait_that_clears_within_a_tick_never_paints(self):
        self.setup_agy("running", "run", [(101, 9, agy_payload("run_command", "Run uv sync"))])
        self.run_ticks(1)
        self.assertEqual(self.opts()["@agent_state"], "running")

    def test_a_wait_without_a_summary_says_tool_approval(self):
        self.setup_agy("running", "run", [(101, 9, b"\x08\x01no json here")])
        self.run_ticks(3)
        self.assertEqual((self.opts()["@agent_state"], self.opts()["@agent_detail"]),
                         ("needs-input", "tool approval"))

    def test_a_summary_cannot_reach_tmuxs_parser(self):
        self.setup_agy("running", "run", [(101, 9, agy_payload("run_command", "Run x'; kill-server; #{y}$z"))])
        self.run_ticks(3)
        o = self.opts()
        self.assertEqual(o["@agent_state"], "needs-input")
        self.assertEqual(o["@agent_detail"], "Run x kill-server yz")

    def test_a_held_stop_is_released_once_nothing_is_in_flight(self):
        # fullyIdle false: running with the Stop's done detail; a background
        # command (a RUNNING step) is still out: held, gear lit.
        self.setup_agy("running", "done", [(101, 2, agy_payload("run_command", "Run the build"))])
        self.run_ticks(4)
        o = self.opts()
        self.assertEqual((o["@agent_state"], o.get("@agent_workflow")), ("running", "1"))
        self.conn.execute("update steps set status = 3 where idx = 1")
        self.run_ticks(5)
        o = self.opts()
        self.assertEqual(o["@agent_state"], "idle")
        self.assertNotIn("@agent_workflow", o)

    def test_a_server_never_holds_the_tab(self):
        # `npm run dev` stays RUNNING for as long as it serves: not work the
        # turn waits on (agent-bg-pending's server rule), so the hold drains.
        for cmd in ("cd /work/app && PORT=3000 npm run dev", "npx vite", "python3 -m http.server 8000",
                    "tail -f build.log"):
            with self.subTest(cmd=cmd):
                self.setup_agy("running", "done", [(101, 2, agy_payload("run_command", "Run server", cmd))])
                self.run_ticks(4)
                self.assertEqual(self.opts()["@agent_state"], "idle", cmd)
                self.f.close()
        # A build is work: held.
        self.setup_agy("running", "done", [(101, 2, agy_payload("run_command", "Run build", "npm run build"))])
        self.run_ticks(4)
        self.assertEqual(self.opts()["@agent_state"], "running")

    def test_a_held_stop_is_bounded(self):
        # The backstop: held at most AGY_HOLD_MAX s (an hour; 2 s here) from
        # the first tick that saw it held, whatever still reads RUNNING.
        self.setup_agy("running", "done", [(101, 2, agy_payload("run_command", "Run soak", "./soak-test.sh"))])
        self.run_ticks(7, AGENT_TAB_AGY_HOLD_MAX="2")
        self.assertEqual(self.opts()["@agent_state"], "idle")

    def test_a_still_store_costs_one_read(self):
        # Cached until the db or its -wal moves; a failed read too.
        self.setup_agy("running", "run", [(101, 9, agy_payload("run_command", "Run uv sync"))])
        self.assertEqual(self.sqlite_forks(4), 1)
        self.f.close()
        self.setup_agy("running", "run", [(101, 9, agy_payload("run_command", "Run uv sync"))])
        self.conn.close()
        (self.f.home / ".gemini/antigravity-cli/conversations" / (self.CID + ".db-shm")).unlink(missing_ok=True)
        self.db.write_bytes(b"not a database")
        self.assertEqual(self.sqlite_forks(4), 1)

    def test_a_plain_turn_is_never_ended_from_the_store(self):
        # Between model calls every step may read DONE: agy's own Stop ends
        # a turn, never this.
        self.setup_agy("running", "run", [(15, 3, agy_payload("view_file", "View notes.md"))])
        self.run_ticks(5)
        self.assertEqual(self.opts()["@agent_state"], "running")

    def test_a_db_outside_gemini_is_never_read(self):
        self.setup_agy("running", "run", [(101, 9, agy_payload("run_command", "Run uv sync"))])
        st = self.f.read()
        other = self.f.dir / "conversations" / (self.CID + ".db")
        other.parent.mkdir()
        import shutil
        shutil.copy(self.db, other)
        st["windows"]["@1"]["opts"]["@agent_record"] = str(other)
        self.f.state.write_text(json.dumps(st))
        self.run_ticks(3)
        self.assertEqual(self.opts()["@agent_state"], "running")


if __name__ == "__main__":
    unittest.main()
