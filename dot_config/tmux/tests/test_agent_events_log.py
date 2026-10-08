"""agent-tab-watcher.sh's agent event log ($TMPDIR/agent-events.$UID.log).

Reuses test_agent_jump_watcher's fake-tmux harness (isolated HOME and TMPDIR
- with a trailing slash, as macOS sets it - and a fake `tmux`, `pgrep`, `ps`).
The fake here also replays a SCRIPT: each list-panes call (one per watcher
tick) first applies the next step, so "tick N sees change X" is exact rather
than timed. No real tmux server, watcher or GUI is touched. Run with unittest.
"""
import json
import os
from pathlib import Path
import stat
import time
import unittest

import test_agent_jump_watcher as jw   # module only, for the shared fakes

# A step maps window id -> None (the window and its panes vanish), a full
# window dict carrying "tty" (created, one pane), or an opts patch (a None
# value unsets that option).
SCRIPT_HOOK = '''        elif cmd == "list-panes":
            for wid, ch in (s["script"].pop(0) if s.get("script") else {}).items():
                if ch is None:
                    s["windows"].pop(wid, None)
                    s["panes"] = [pn for pn in s["panes"] if pn["window"] != wid]
                elif "tty" in ch:
                    s["windows"][wid] = {k: v for k, v in ch.items() if k != "tty"}
                    s["panes"].append({"window": wid, "tty": ch["tty"]})
                else:
                    o = s["windows"][wid].setdefault("opts", {})
                    for k, v in ch.items():
                        if v is None: o.pop(k, None)
                        else: o[k] = v
'''
FAKE_TMUX = jw.FAKE_TMUX.replace('        elif cmd == "list-panes":\n', SCRIPT_HOOK, 1)
assert FAKE_TMUX != jw.FAKE_TMUX

AGENT_TTY = "/dev/ttys900"   # jw.DEFAULT_PROCS: claude pid 4242 lives here, so nothing is GC'd


class EventLogTests(unittest.TestCase):
    def setUp(self):
        self.f = None

    def tearDown(self):
        if self.f:
            self.f.close()

    def env(self, windows, script=(), **extra_opts):
        self.f = jw.FakeEnv(windows=windows,
                            panes=[{"window": w, "tty": AGENT_TTY} for w in windows])
        tmux = self.f.bin / "tmux"; tmux.write_text(FAKE_TMUX)
        s = self.f.read(); s["script"] = list(script); self.f.state.write_text(json.dumps(s))
        self.log = Path(str(self.f.tmp) + "/agent-events.%d.log" % os.getuid())

    def run_ticks(self, n, **extra):
        self.t0 = int(time.time())
        r = self.f.run("agent-tab-watcher.sh", timeout=10 + 2 * n,
                       AGENT_TAB_WATCHER_MAX_TICKS=str(n), **extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.t1 = int(time.time())

    def lines(self):
        if not self.log.exists():
            return []
        raw = self.log.read_bytes().decode()
        self.assertTrue(raw == "" or raw.endswith("\n"), repr(raw))
        return [ln.split("\x1f") for ln in raw.splitlines()]

    def events(self):
        """Lines minus the epoch, after checking it is this run's integer seconds."""
        out = []
        for ln in self.lines():
            self.assertEqual(len(ln), 9, ln)
            self.assertRegex(ln[0], r"^\d+$")
            self.assertTrue(self.t0 <= int(ln[0]) <= self.t1, (ln, self.t0, self.t1))
            out.append(ln[1:])
        return out

    def test_baseline_and_unchanged_ticks_are_silent(self):
        self.env({"@1": jw.W("main", 1, **{"@agent_state": "running", "@agent_summary": "p/x"}),
                  "@2": jw.W("main", 2, **{"@agent_state": "done"}),
                  "@3": jw.W("main", 3)})   # agent pane, no state: seeded idle by the watcher
        self.run_ticks(1)
        self.assertEqual(self.lines(), [])
        self.assertEqual(self.f.read()["windows"]["@3"]["opts"]["@agent_state"], "idle")
        # A fresh watcher (baseline again, @3's seed now included) and several
        # ticks with nothing changing: still nothing.
        self.run_ticks(4)
        self.assertEqual(self.lines(), [])

    def test_watcher_made_change_is_logged_once(self):
        # The idle seed is the watcher's own write: logged the tick after it
        # lands (that is when a read first holds it), and never again.
        self.env({"@1": jw.W("main", 1, **{"@agent_state": "running"}), "@3": jw.W("main", 3)})
        self.run_ticks(4)
        self.assertEqual(self.events(), [["@3", "main", "3", "idle", "", "", "zsh", ""]])

    def test_one_line_per_transition_in_field_order(self):
        w1 = jw.W("main", 1, **{"@agent_state": "running", "@agent_summary": "proj/fix bug",
                                "@agent_detail_kind": "run", "@agent_detail": "Fix it"})
        w1["links"] = ["side"]   # listed twice per read, logged once
        w2 = jw.W("main", 2, **{"@agent_state": "done"}); w2["name"] = "mywin"
        script = [
            {},                                                        # tick 1: baseline
            {"@1": {"@agent_state": "done", "@agent_detail_kind": "done",
                    "@agent_detail": "All tests pass."}},             # tick 2
            {},                                                        # tick 3: no change
            {"@2": {"@agent_state": "needs-input", "@agent_detail_kind": "perm",
                    "@agent_detail": "Bash ls"},
             "@3": dict(jw.W("work", 5, **{"@agent_state": "running", "@agent_detail_kind": "run",
                                           "@agent_detail": "go"}), tty=AGENT_TTY)},   # tick 4
            {"@1": {"@agent_detail": "edited, same state"}},           # tick 5: detail only
        ]
        self.env({"@1": w1, "@2": w2}, script)
        self.run_ticks(6)
        self.assertEqual(self.events(), [
            ["@1", "main", "1", "done", "running", "done", "proj/fix bug", "All tests pass."],
            ["@2", "main", "2", "needs-input", "done", "perm", "mywin", "Bash ls"],
            ["@3", "work", "5", "running", "", "run", "zsh", "go"],
        ])

    def test_values_are_sanitized(self):
        # The summary rides last in the read, so a US inside it reaches the log
        # intact and must be stripped there; CRLF text is the realistic \r.
        script = [{}, {"@1": {"@agent_state": "done",
                              "@agent_summary": "proj/a\x1fb\r",
                              "@agent_detail": "x\r\ty"}},
                  {"@2": {"@agent_state": "failed", "@agent_summary": "first\r\nsecond"}}]
        self.env({"@1": jw.W("main", 1, **{"@agent_state": "running"}),
                  "@2": jw.W("main", 2, **{"@agent_state": "running"})}, script)
        self.run_ticks(4)
        ev = self.events()
        self.assertEqual(ev[0], ["@1", "main", "1", "done", "running", "", "proj/ab", "x\ty"])
        # A newline ends the pane row: the rest never reaches the log, and the
        # file still has exactly one line per event.
        self.assertEqual(ev[1], ["@2", "main", "2", "failed", "running", "", "first", ""])
        self.assertEqual(len(ev), 2)

    def test_separator_or_newline_inside_detail(self):
        # Pinned CURRENT behaviour, not a goal: the indicator strips control
        # characters from @agent_detail, so neither can reach a real read.
        # Mid-row, a US shifts the rest into the summary; a newline cuts the row.
        script = [{}, {"@1": {"@agent_state": "done", "@agent_detail_kind": "done",
                              "@agent_detail": "a\x1fb"}},
                  {"@2": {"@agent_state": "done", "@agent_detail_kind": "done",
                          "@agent_detail": "c\nd"}}]
        self.env({"@1": jw.W("main", 1, **{"@agent_state": "running", "@agent_summary": "p/t"}),
                  "@2": jw.W("main", 2, **{"@agent_state": "running", "@agent_summary": "p/u"})},
                 script)
        self.run_ticks(4)
        self.assertEqual(self.events(), [
            ["@1", "main", "1", "done", "running", "done", "bp/t", "a"],
            ["@2", "main", "2", "done", "running", "done", "zsh", "c"],
        ])

    def test_idle_lines_carry_no_detail(self):
        # The discharge / reconcile / bare SessionStart change only the state:
        # the window still holds the previous event's detail, the line must not.
        script = [{}, {"@1": {"@agent_state": "idle"}}]
        self.env({"@1": jw.W("main", 1, **{"@agent_state": "done", "@agent_summary": "Notch Tasks",
                                            "@agent_detail_kind": "done",
                                            "@agent_detail": "3 tasks filed"})}, script)
        self.run_ticks(3)
        self.assertEqual(self.events(), [["@1", "main", "1", "idle", "done", "", "Notch Tasks", ""]])

    def set_state(self, fn):
        s = self.f.read(); fn(s); self.f.state.write_text(json.dumps(s))

    def test_restart_logs_a_transition_from_the_gap(self):
        script = [{}, {"@1": {"@agent_state": "done"}, "@2": {"@agent_state": "running"}}]
        self.env({"@1": jw.W("main", 1, **{"@agent_state": "running"}),
                  "@2": jw.W("main", 2, **{"@agent_state": "done"}),
                  "@3": jw.W("main", 3, **{"@agent_state": "done"})}, script)
        self.run_ticks(3)
        self.assertEqual([e[:5] for e in self.events()], [["@1", "main", "1", "done", "running"],
                                                          ["@2", "main", "2", "running", "done"]])
        # No watcher running: @1 changes, @2 does not, @3 (never logged) does.
        def gap(s):
            s["windows"]["@1"]["opts"].update({"@agent_state": "needs-input", "@agent_detail_kind": "ask",
                                               "@agent_detail": "Which branch?"})
            s["windows"]["@3"]["opts"]["@agent_state"] = "running"
        self.set_state(gap)
        self.run_ticks(3)
        ev = self.lines()
        self.assertEqual(len(ev), 3, ev)
        self.assertEqual(ev[2][1:], ["@1", "main", "1", "needs-input", "done", "ask", "zsh", "Which branch?"])

    def test_restart_seed_needs_the_same_session_and_index(self):
        # Window ids restart with the tmux server: a logged @1/@2 whose
        # session+index no longer match is a different window, so baseline.
        script = [{}, {"@1": {"@agent_state": "done"}, "@2": {"@agent_state": "done"}}]
        self.env({"@1": jw.W("main", 1, **{"@agent_state": "running"}),
                  "@2": jw.W("main", 2, **{"@agent_state": "running"})}, script)
        self.run_ticks(3)
        self.assertEqual(len(self.lines()), 2)
        def gap(s):
            s["windows"]["@1"]["index"] = 7
            s["windows"]["@2"]["session"] = "other"
            for w in ("@1", "@2"):
                s["windows"][w]["opts"]["@agent_state"] = "running"
        self.set_state(gap)
        self.run_ticks(2)
        self.assertEqual(len(self.lines()), 2, self.lines())

    def test_fifo_or_symlink_at_the_log_is_skipped(self):
        script = [{}, {"@1": {"@agent_state": "done"}}, {"@1": {"@agent_state": "running"}}]
        self.env({"@1": jw.W("main", 1, **{"@agent_state": "running"})}, script)
        os.mkfifo(self.log)
        self.run_ticks(4)   # timeout 18 s: a blocked open would raise TimeoutExpired
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.log).st_mode))
        os.unlink(self.log)
        target = self.f.dir / "elsewhere"; target.write_text("")
        self.log.symlink_to(target)
        self.set_state(lambda s: s.update(script=script))
        self.run_ticks(4)
        self.assertTrue(self.log.is_symlink())
        self.assertEqual(target.read_text(), "")

    def test_startup_sweeps_rotation_temps(self):
        self.env({"@1": jw.W("main", 1, **{"@agent_state": "running"})})
        stale = Path(str(self.log) + ".99999.tmp"); stale.write_text("half a rewrite\n")
        keep = Path(str(self.log) + ".bak"); keep.write_text("not ours\n")
        self.run_ticks(1)
        self.assertFalse(stale.exists())
        self.assertTrue(keep.exists())

    def test_disappearing_or_cleared_window_is_not_logged(self):
        script = [{}, {"@2": None, "@1": {"@agent_state": None}}, {},
                  {"@1": {"@agent_state": "running"}}]
        self.env({"@1": jw.W("main", 1, **{"@agent_state": "done", "@agent_summary": "p/t"}),
                  "@2": jw.W("main", 2, **{"@agent_state": "running"})}, script)
        # The watcher re-seeds @1's cleared state as idle (its agent is alive);
        # tick 4 then overrides it with running.
        self.run_ticks(5)
        ev = self.events()
        self.assertFalse([e for e in ev if e[0] == "@2"], ev)
        self.assertEqual([e[:5] for e in ev], [["@1", "main", "1", "idle", ""],
                                               ["@1", "main", "1", "running", "idle"]])

    def flip_script(self, n):
        return [{}] + [{"@1": {"@agent_state": "done" if i % 2 == 0 else "running"}} for i in range(n)]

    def test_cap_rewrites_to_the_newest_lines(self):
        self.env({"@1": jw.W("main", 1, **{"@agent_state": "running"})}, self.flip_script(4))
        self.log.write_text("".join("old%d\n" % i for i in range(1, 5)))
        self.run_ticks(5, AGENT_TAB_WATCHER_EVENTS_MAX="5", AGENT_TAB_WATCHER_EVENTS_KEEP="3")
        lines = self.lines()
        # 4 old + e1 = 5 (at the cap); + e2 = 6 > 5 -> newest 3 (old4 e1 e2); + e3 + e4 = 5.
        self.assertEqual(lines[0], ["old4"])
        self.assertEqual([ln[4:6] for ln in lines[1:]],
                         [["done", "running"], ["running", "done"], ["done", "running"], ["running", "done"]])
        self.assertEqual(sorted(p.name for p in self.f.tmp.iterdir() if ".tmp" in p.name), [])

    def test_cap_defaults(self):
        self.env({"@1": jw.W("main", 1, **{"@agent_state": "running"})}, self.flip_script(2))
        self.log.write_text("".join("old%d\n" % i for i in range(1, 2001)))
        self.run_ticks(3)
        lines = self.lines()
        # 2000 + e1 = 2001 > 2000 -> newest 1000 (old1002..old2000, e1); + e2.
        self.assertEqual(len(lines), 1001)
        self.assertEqual(lines[0], ["old1002"])
        self.assertEqual([ln[4] for ln in lines[-2:]], ["done", "running"])


if __name__ == "__main__":
    unittest.main()
