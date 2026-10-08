"""agent-tab-watcher.sh: Codex parity - the launch-time @agent_kind seed and the
rollout turn-end reconcile (Esc on an approval / mid-turn fires no codex hook).

Reuses test_agent_jump_watcher's fake-tmux harness (isolated HOME and TMPDIR,
fake `tmux`, `pgrep`, `ps`), plus a pass-through `tail` that logs its argv so
a test can see exactly which rollouts the watcher read. Rollout fixtures use
the exact line shape codex 0.160 writes. No real tmux server, watcher or GUI is
touched. Run with unittest.
"""
import json
import os
from pathlib import Path
import time
import unittest

import test_agent_jump_watcher as jw   # module only, for the shared fakes

CODEX_TTY, PLAIN_TTY, CLAUDE_TTY = "/dev/ttys900", "/dev/ttys901", "/dev/ttys902"
# pid 5151 is a codex native binary (path comm), 4242 a claude.
PROCS = "ttys900 5151 /x/vendor/aarch64/bin/codex\nttys901 4343 zsh\nttys902 4242 claude\n"

FAKE_TAIL = r"""#!/bin/sh
[ -n "$FAKE_TAIL_LOG" ] && echo "$*" >> "$FAKE_TAIL_LOG"
exec /usr/bin/tail "$@"
"""


def iso(t):
    """A rollout timestamp for epoch t, as codex writes it (UTC, ms, Z)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".123Z"


def ev(t, typ, ordinal=1, **payload):
    body = ",".join('"%s":"%s"' % kv for kv in [("type", typ), ("turn_id", "01a1-turn")] + list(payload.items()))
    return '{"timestamp":"%s","ordinal":%d,"type":"event_msg","payload":{%s}}\n' % (iso(t), ordinal, body)


def filler(n=3):
    return "".join('{"timestamp":"2026-10-08T00:00:00.000Z","ordinal":9,"type":"response_item",'
                   '"payload":{"type":"message","content":"x"}}\n' for _ in range(n))


class CodexParityTests(unittest.TestCase):
    def make(self, windows, panes):
        self.f = jw.FakeEnv(windows=windows, panes=panes)
        self.f.procs.write_text(PROCS)
        tail = self.f.bin / "tail"; tail.write_text(FAKE_TAIL); tail.chmod(0o755)
        self.tail_log = self.f.dir / "tail.log"
        self.log = Path(str(self.f.tmp) + "/agent-events.%d.log" % os.getuid())
        return self.f

    def tearDown(self):
        self.f.close()

    def rollout(self, name, text):
        p = self.f.home / ".codex/sessions/2026/10/08" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return str(p)

    def run_ticks(self, n):
        r = self.f.run("agent-tab-watcher.sh", timeout=10 + 2 * n,
                       AGENT_TAB_WATCHER_MAX_TICKS=str(n), FAKE_TAIL_LOG=str(self.tail_log))
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.f.read()

    def tails(self):
        return self.tail_log.read_text().splitlines() if self.tail_log.exists() else []

    def events(self):
        if not self.log.exists():
            return []
        return [ln.split("\x1f")[1:] for ln in self.log.read_text().splitlines()]

    def calls(self, s, wid):
        return [c for c in s["calls"] if c[0] == "set-option" and wid in c]

    def codex_window(self, idx, state, since, rollout, **extra):
        opts = {"@agent_state": state, "@agent_since": "%d %s" % (since, state),
                "@agent_kind": "codex", "@agent_rollout": rollout}
        opts.update(extra)
        return jw.W("main", idx, **opts)

    # --- @agent_kind at launch ---

    def test_kind_seeded_from_comm_and_never_overwrites_a_hook_kind(self):
        s = self.make(
            windows={"@1": jw.W("main", 1),                                     # fresh codex
                     "@2": jw.W("main", 2),                                     # plain shell
                     "@3": jw.W("main", 3),                                     # fresh claude
                     "@4": jw.W("main", 4, **{"@agent_state": "idle", "@agent_since": "5 idle",
                                              "@agent_kind": "claude"}),         # hook-set kind
                     "@5": jw.W("main", 5, **{"@agent_state": "idle", "@agent_since": "5 idle"})},  # held, no kind
            panes=[{"window": "@1", "tty": CODEX_TTY}, {"window": "@2", "tty": PLAIN_TTY},
                   {"window": "@3", "tty": CLAUDE_TTY}, {"window": "@4", "tty": CODEX_TTY},
                   {"window": "@5", "tty": CODEX_TTY}])
        s = self.run_ticks(3)
        w = s["windows"]
        self.assertEqual(w["@1"]["opts"].get("@agent_state"), "idle")
        self.assertEqual(w["@1"]["opts"].get("@agent_kind"), "codex")
        self.assertEqual(w["@3"]["opts"].get("@agent_kind"), "claude")
        self.assertEqual(w["@4"]["opts"]["@agent_kind"], "claude")              # never overwritten
        self.assertEqual(w["@5"]["opts"].get("@agent_kind"), "codex")
        self.assertFalse(self.calls(s, "@2"), s["calls"])
        # Written once each over three ticks; @4's is never written at all.
        for wid in ("@1", "@3", "@5"):
            self.assertEqual(len([c for c in self.calls(s, wid) if "@agent_kind" in c]), 1, s["calls"])
        self.assertFalse([c for c in self.calls(s, "@4") if "@agent_kind" in c], s["calls"])
        # The seed's kind rides in the stamp's invocation (the fake logs each
        # command of a list in order): the idle seed, its stamp, then the kind.
        c1 = self.calls(s, "@1")
        i = c1.index(["set-option", "-w", "-t", "@1", "@agent_kind", "codex"])
        self.assertEqual(c1[i - 1][:5], ["set-option", "-w", "-t", "@1", "@agent_since"])
        # The seeded kind never makes a window GC-proof: the agent exits ->
        # collected after GC_TICKS, kind included.
        self.f.procs.write_text("ttys901 4343 zsh\n")
        s = self.run_ticks(6)
        for wid in ("@1", "@3", "@5"):
            for name in ("@agent_state", "@agent_kind"):
                self.assertNotIn(name, s["windows"][wid]["opts"], (wid, s["windows"][wid]["opts"]))

    # --- the turn-end reconcile ---

    def test_needs_input_to_idle_after_turn_aborted(self):
        now = int(time.time())
        self.make({}, [])
        rp = self.rollout("rollout-a.jsonl", filler() + ev(now - 120, "task_started")
                          + filler() + ev(now - 30, "turn_aborted", reason="interrupted",
                                          started_at=str(now - 120)) + filler(1))
        st = self.f.read()
        st["windows"]["@1"] = self.codex_window(1, "needs-input", now - 60, rp, **{
            "@agent_pending": "123", "@agent_detail_kind": "perm", "@agent_detail": "Bash rm -rf x",
            "@agent_summary": "proj/thing"})
        st["panes"] = [{"window": "@1", "tty": CODEX_TTY}]
        self.f.state.write_text(json.dumps(st))
        # Two ticks: still inside the hysteresis, nothing written yet.
        s = self.run_ticks(2)
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "needs-input")
        s = self.run_ticks(4)
        o = s["windows"]["@1"]["opts"]
        self.assertEqual(o["@agent_state"], "idle")
        self.assertRegex(o["@agent_since"], r"^\d+ idle$")
        for gone in ("@agent_pending", "@agent_detail_kind", "@agent_detail"):
            self.assertNotIn(gone, o)
        self.assertEqual((o["@agent_kind"], o["@agent_summary"], o["@agent_rollout"]), ("codex", "proj/thing", rp))
        # One write, the stamp in it: the backstop never restamps it.
        self.assertEqual(len([c for c in self.calls(s, "@1") if "@agent_since" in c]), 1, s["calls"])
        # Logged exactly once (the second run's first tick read needs-input
        # from the log's own baseline; the change shows up a tick after it lands).
        self.assertEqual([e[3:5] for e in self.events()], [["idle", "needs-input"]])
        # Bounded read: the last 64 KB, of this rollout only.
        self.assertTrue(self.tails())
        self.assertTrue(all(t == "-c 65536 " + rp for t in self.tails()), self.tails())

    def test_running_to_done_after_task_complete_and_idle_after_abort(self):
        now = int(time.time())
        self.make({}, [])
        done = self.rollout("rollout-d.jsonl", ev(now - 90, "task_started") + filler()
                            + ev(now - 10, "task_complete", last_agent_message="All set.",
                                 completed_at=str(now - 10)) + filler(2))
        abort = self.rollout("rollout-x.jsonl", ev(now - 90, "task_started") + filler()
                             + ev(now - 10, "turn_aborted", reason="interrupted"))
        st = self.f.read()
        st["windows"] = {
            "@1": self.codex_window(1, "running", now - 60, done, **{
                "@agent_detail_kind": "run", "@agent_detail": "do it", "@agent_pending": "9"}),
            "@2": self.codex_window(2, "running", now - 60, abort, **{
                "@agent_detail_kind": "run", "@agent_detail": "do it"}),
        }
        st["panes"] = [{"window": "@1", "tty": CODEX_TTY}, {"window": "@2", "tty": CODEX_TTY}]
        self.f.state.write_text(json.dumps(st))
        s = self.run_ticks(4)
        o1, o2 = s["windows"]["@1"]["opts"], s["windows"]["@2"]["opts"]
        self.assertEqual(o1["@agent_state"], "done")
        self.assertRegex(o1["@agent_since"], r"^\d+ done$")
        self.assertEqual((o1["@agent_detail_kind"], o1["@agent_detail"]), ("done", "turn finished"))
        self.assertNotIn("@agent_pending", o1)
        self.assertEqual(o2["@agent_state"], "idle")
        self.assertNotIn("@agent_detail_kind", o2)
        self.assertNotIn("@agent_detail", o2)
        self.assertEqual([e[:1] + e[3:5] for e in self.events()],
                         [["@1", "done", "running"], ["@2", "idle", "running"]])
        # Nothing running any more: the pulse flag is down.
        self.assertFalse(Path(str(self.f.tmp) + "/agent-tab-blink." + str(os.getuid())).exists())

    def test_old_turn_end_before_the_stamp_is_ignored(self):
        now = int(time.time())
        self.make({}, [])
        old = self.rollout("rollout-o.jsonl", ev(now - 600, "task_started") + filler()
                           + ev(now - 300, "turn_aborted", reason="interrupted") + filler(1))
        same = self.rollout("rollout-s.jsonl", ev(now - 600, "task_started")
                            + ev(now - 60, "turn_aborted", reason="interrupted"))
        st = self.f.read()
        st["windows"] = {
            # An open prompt raised after the last turn end: must stay yellow.
            "@1": self.codex_window(1, "needs-input", now - 60, old, **{
                "@agent_pending": "123", "@agent_detail_kind": "perm", "@agent_detail": "Bash ls"}),
            # A turn end in the stamp's own second is not "after" it.
            "@2": self.codex_window(2, "needs-input", now - 60, same),
            # A stale stamp (another state's) is "don't know": the backstop
            # restamps it, and the next tick finds the abort is older.
            "@3": self.codex_window(3, "needs-input", now - 900, old,
                                    **{"@agent_since": "%d running" % (now - 900)}),
        }
        st["panes"] = [{"window": w, "tty": CODEX_TTY} for w in ("@1", "@2", "@3")]
        self.f.state.write_text(json.dumps(st))
        s = self.run_ticks(5)
        o = s["windows"]["@1"]["opts"]
        self.assertEqual(o["@agent_state"], "needs-input")
        self.assertEqual((o["@agent_pending"], o["@agent_detail"]), ("123", "Bash ls"))
        self.assertEqual(s["windows"]["@2"]["opts"]["@agent_state"], "needs-input")
        self.assertEqual(s["windows"]["@3"]["opts"]["@agent_state"], "needs-input")
        self.assertRegex(s["windows"]["@3"]["opts"]["@agent_since"], r"^\d+ needs-input$")
        self.assertFalse([c for c in s["calls"] if "@agent_state" in c], s["calls"])
        self.assertEqual(self.events(), [])

    def test_running_with_an_old_turn_end_keeps_the_older_rule(self):
        # The pre-existing stuck-running reconcile: a running codex tab whose
        # last turn end predates its stamp still goes idle after the streak,
        # touching nothing but the state (as before).
        now = int(time.time())
        self.make({}, [])
        rp = self.rollout("rollout-r.jsonl", ev(now - 600, "task_started")
                          + ev(now - 300, "task_complete", last_agent_message="ok"))
        st = self.f.read()
        st["windows"] = {"@1": self.codex_window(1, "running", now - 60, rp, **{
            "@agent_detail_kind": "run", "@agent_detail": "go", "@agent_pending": "7"})}
        st["panes"] = [{"window": "@1", "tty": CODEX_TTY}]
        self.f.state.write_text(json.dumps(st))
        s = self.run_ticks(3)
        o = s["windows"]["@1"]["opts"]
        self.assertEqual(o["@agent_state"], "idle")
        self.assertEqual((o["@agent_detail"], o["@agent_pending"]), ("go", "7"))

    def test_live_turn_and_non_codex_windows_are_untouched(self):
        now = int(time.time())
        self.make({}, [])
        live = self.rollout("rollout-l.jsonl", ev(now - 300, "turn_aborted") + filler()
                            + ev(now - 30, "task_started") + filler())
        ended = self.rollout("rollout-e.jsonl", ev(now - 90, "task_started")
                             + ev(now - 10, "turn_aborted", reason="interrupted"))
        st = self.f.read()
        st["windows"] = {
            # Codex, mid-turn (last marker is a start): stays running.
            "@1": self.codex_window(1, "running", now - 60, live),
            # A CLAUDE window carrying a stale rollout path in needs-input: the
            # rollout is not its record, so it is neither read nor acted on.
            "@2": jw.W("main", 2, **{"@agent_state": "needs-input", "@agent_kind": "claude",
                                     "@agent_since": "%d needs-input" % (now - 60),
                                     "@agent_rollout": ended}),
        }
        st["panes"] = [{"window": "@1", "tty": CODEX_TTY}, {"window": "@2", "tty": CLAUDE_TTY}]
        self.f.state.write_text(json.dumps(st))
        s = self.run_ticks(4)
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "running")
        self.assertEqual(s["windows"]["@2"]["opts"]["@agent_state"], "needs-input")
        self.assertFalse([c for c in s["calls"] if "@agent_state" in c], s["calls"])
        self.assertFalse([t for t in self.tails() if ended in t], self.tails())

    def test_fast_path_and_settled_windows_read_no_rollouts(self):
        now = int(time.time())
        self.make({}, [])
        ended = self.rollout("rollout-e.jsonl", ev(now - 90, "task_started")
                             + ev(now - 10, "turn_aborted", reason="interrupted"))
        st = self.f.read()
        st["windows"] = {"@%d" % (100 + i): jw.W("agents", i + 1) for i in range(20)}   # fast path
        st["panes"] = [{"window": "@%d" % (100 + i), "tty": "/dev/ttys%d" % (700 + i)} for i in range(20)]
        # Codex windows that are settled (idle, done, failed): never read.
        for i, state in enumerate(("idle", "done", "failed"), 1):
            st["windows"]["@%d" % i] = self.codex_window(i, state, now - 60, ended)
            st["panes"].append({"window": "@%d" % i, "tty": CODEX_TTY})
        # An agent-less window still holding a codex needs-input (being GC'd):
        # no agent, no read.
        st["windows"]["@9"] = self.codex_window(9, "needs-input", now - 60, ended)
        st["panes"].append({"window": "@9", "tty": "/dev/ttys999"})
        self.f.state.write_text(json.dumps(st))
        s = self.run_ticks(3)
        self.assertEqual(self.tails(), [])
        for i in range(20):
            self.assertFalse(self.calls(s, "@%d" % (100 + i)), s["calls"])
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "idle")
        self.assertEqual(s["windows"]["@3"]["opts"]["@agent_state"], "failed")

    def test_turn_end_beyond_the_tail_window_is_not_seen(self):
        # Bounded read: a turn end more than 64 KB from EOF is outside it, so
        # the answer is "unknown" and the prompt is left alone.
        now = int(time.time())
        self.make({}, [])
        big = "".join(
            '{"timestamp":"2026-10-08T00:00:00.000Z","ordinal":9,"type":"response_item",'
            '"payload":{"type":"message","content":"%s"}}\n' % ("y" * 1000) for _ in range(80))
        rp = self.rollout("rollout-b.jsonl", ev(now - 90, "task_started")
                          + ev(now - 10, "turn_aborted", reason="interrupted") + big)
        st = self.f.read()
        st["windows"] = {"@1": self.codex_window(1, "needs-input", now - 60, rp)}
        st["panes"] = [{"window": "@1", "tty": CODEX_TTY}]
        self.f.state.write_text(json.dumps(st))
        s = self.run_ticks(4)
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "needs-input")
        self.assertTrue(self.tails())

    # --- review round 1: gaps ---

    def put(self, windows, panes, script=None):
        st = self.f.read()
        st["windows"], st["panes"] = windows, panes
        if script is not None:
            st["script"] = script
        self.f.state.write_text(json.dumps(st))

    def test_claude_running_with_a_stale_rollout_keeps_the_old_idle_rule(self):
        # A claude agent (no session file) in a window whose @agent_rollout an
        # earlier codex left behind: the rollout's turn end, though after the
        # stamp, is not THIS agent's, so no codex "done" - the pre-existing
        # stuck-running rule's idle, state only, detail untouched.
        now = int(time.time())
        self.make({}, [])
        rp = self.rollout("rollout-c.jsonl", ev(now - 90, "task_started") + ev(now - 10, "task_complete"))
        self.put({"@2": jw.W("main", 2, **{"@agent_state": "running", "@agent_kind": "claude",
                                           "@agent_since": "%d running" % (now - 60), "@agent_rollout": rp,
                                           "@agent_detail_kind": "run", "@agent_detail": "x",
                                           "@agent_pending": "5"})},
                 [{"window": "@2", "tty": CLAUDE_TTY}])
        s = self.run_ticks(4)
        o = s["windows"]["@2"]["opts"]
        self.assertEqual(o["@agent_state"], "idle")
        self.assertEqual((o["@agent_detail_kind"], o["@agent_detail"], o["@agent_pending"]), ("run", "x", "5"))
        self.assertFalse([c for c in s["calls"] if "done" in c], s["calls"])

    def test_task_complete_while_viewed_is_idle(self):
        # The Stop path's viewing_now: active for a client with WezTerm in front.
        lsapp = '#!/bin/sh\ncase "$1" in front) echo "ASN:0x0-0x1:" ;; ' \
                'info) echo \'"CFBundleIdentifier"="com.github.wez.wezterm"\' ;; esac\n'
        now = int(time.time())
        self.make({}, [])
        f = self.f.bin / "lsappinfo"; f.write_text(lsapp); f.chmod(0o755)
        rp = self.rollout("rollout-v.jsonl", ev(now - 90, "task_started") + ev(now - 10, "task_complete"))
        w = self.codex_window(1, "running", now - 60, rp); w["active_clients"] = 1
        self.put({"@1": w}, [{"window": "@1", "tty": CODEX_TTY}])
        s = self.run_ticks(4)
        o = s["windows"]["@1"]["opts"]
        self.assertEqual(o["@agent_state"], "idle")
        self.assertEqual((o["@agent_detail_kind"], o["@agent_detail"]), ("done", "turn finished"))
        self.assertFalse([c for c in s["calls"] if "@agent_state" in c and "done" in c], s["calls"])

    def test_needs_input_then_task_complete_is_done(self):
        # The prompt was answered (no tmux focus, so no heartbeat re-arm) and
        # the turn finished without a Stop hook: done.
        now = int(time.time())
        self.make({}, [])
        rp = self.rollout("rollout-n.jsonl", ev(now - 90, "task_started") + filler()
                          + ev(now - 10, "task_complete", last_agent_message="ok"))
        self.put({"@1": self.codex_window(1, "needs-input", now - 60, rp, **{
            "@agent_pending": "1", "@agent_detail_kind": "perm", "@agent_detail": "Bash ls"})},
                 [{"window": "@1", "tty": CODEX_TTY}])
        s = self.run_ticks(4)
        o = s["windows"]["@1"]["opts"]
        self.assertEqual(o["@agent_state"], "done")
        self.assertEqual((o["@agent_detail_kind"], o["@agent_detail"]), ("done", "turn finished"))
        self.assertNotIn("@agent_pending", o)

    def test_marker_whose_timestamp_the_cut_removed_is_never_after(self):
        # The 64 KB tail starts 5 bytes into the last line: its marker is still
        # in view, its timestamp is not, so it can never count as "after".
        # Control: the same line whole in view does.
        now = int(time.time())
        self.make({}, [])
        def last_line(total):
            base = ev(now - 10, "task_complete", last_agent_message="")
            pad = total - len(base)
            return ev(now - 10, "task_complete", last_agent_message="z" * pad)
        cut = last_line(65536 + 5)
        whole = last_line(65536 - 100)
        self.assertEqual((len(cut), len(whole)), (65541, 65436))
        rp_cut = self.rollout("rollout-cut.jsonl", ev(now - 90, "task_started") + filler() + cut)
        rp_whole = self.rollout("rollout-whole.jsonl", ev(now - 90, "task_started") + filler() + whole)
        self.put({"@1": self.codex_window(1, "needs-input", now - 60, rp_cut),
                  "@2": self.codex_window(2, "needs-input", now - 60, rp_whole)},
                 [{"window": "@1", "tty": CODEX_TTY}, {"window": "@2", "tty": CODEX_TTY}])
        s = self.run_ticks(4)
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "needs-input")
        self.assertEqual(s["windows"]["@2"]["opts"]["@agent_state"], "done")

    def test_version_named_claude_comm_seeds_claude(self):
        self.make({}, [])
        self.f.procs.write_text(PROCS + "ttys903 5252 2.1.170\nttys904 30 codex\nttys904 40 claude\n")
        self.put({"@1": jw.W("main", 1), "@2": jw.W("main", 2)},
                 [{"window": "@1", "tty": "/dev/ttys903"}, {"window": "@2", "tty": "/dev/ttys904"}])
        s = self.run_ticks(2)
        self.assertEqual(s["windows"]["@1"]["opts"].get("@agent_kind"), "claude")
        # Two agents on one tty: the lowest pid's comm decides, as for W_PID.
        self.assertEqual(s["windows"]["@2"]["opts"].get("@agent_kind"), "codex")

    def test_unchanged_rollout_is_read_once_and_again_after_an_append(self):
        import threading
        now = int(time.time())
        self.make({}, [])
        rp = self.rollout("rollout-k.jsonl", ev(now - 600, "task_started")
                          + ev(now - 300, "turn_aborted", reason="interrupted") + filler(2))
        self.put({"@1": self.codex_window(1, "needs-input", now - 60, rp)},
                 [{"window": "@1", "tty": CODEX_TTY}])
        s = self.run_ticks(6)
        self.assertEqual(self.tails(), ["-c 65536 " + rp])           # one read over six ticks
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "needs-input")
        # Mid-run the user Escs the prompt: the append is seen, read once more,
        # and the reconcile acts on it.
        self.tail_log.unlink()
        def esc():
            with open(rp, "a") as fh:
                fh.write(ev(int(time.time()), "turn_aborted", reason="interrupted"))
        t = threading.Timer(2.5, esc); t.start(); self.addCleanup(t.cancel)
        s = self.run_ticks(8)
        self.assertEqual(self.tails(), ["-c 65536 " + rp] * 2, self.tails())
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "idle")

    def test_cache_is_dropped_when_the_window_leaves_the_states(self):
        # needs-input (read) -> idle (not asked: entry dropped) -> needs-input
        # again on the SAME unchanged file: read afresh, not served stale.
        import test_agent_events_log as el
        now = int(time.time())
        self.make({}, [])
        (self.f.bin / "tmux").write_text(el.FAKE_TMUX)
        rp = self.rollout("rollout-q.jsonl", ev(now - 600, "task_started")
                          + ev(now - 300, "turn_aborted", reason="interrupted"))
        self.put({"@1": self.codex_window(1, "needs-input", now - 60, rp)},
                 [{"window": "@1", "tty": CODEX_TTY}],
                 script=[{}, {"@1": {"@agent_state": "idle"}}, {"@1": {"@agent_state": "needs-input"}}, {}])
        self.run_ticks(4)
        self.assertEqual(self.tails(), ["-c 65536 " + rp] * 2, self.tails())


if __name__ == "__main__":
    unittest.main()
