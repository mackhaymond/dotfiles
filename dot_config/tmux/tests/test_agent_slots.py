"""Sticky agent number slots (@agent_slot): agent-tab-watcher.sh's tick and
agent-tab-indicator.sh's stamp, which share the SLOT CORE.

Reuses test_agent_jump_watcher's fake-tmux harness (isolated HOME and TMPDIR,
fake `tmux`, `pgrep`, `ps`). Like test_agent_events_log, the fake replays a
SCRIPT: each list-panes call (one per watcher tick) first applies the next
step, so "tick N sees change X" is exact rather than timed. A step maps window
id -> None (the window and its panes vanish), a full window dict carrying
"tty" (created, one pane), or an opts patch (None unsets that option; the
keys "session" and "links" move the window instead). This fake is stricter
than the shared one, as real tmux is: a failed command ends the rest of its
command list; a link may carry its own index ([session, index]); and every
invocation is marked in the call log ("<inv>"). No real tmux server, watcher
or GUI is touched. Run with unittest.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import unittest

import test_agent_jump_watcher as jw   # module only, for the shared fakes

INDICATOR = jw.SCRIPTS / "executable_agent-tab-indicator.sh"

SCRIPT_HOOK = '''            for wid in (s.pop("vanish_after_list_windows", []) if cmd == "list-windows" else []):
                s["windows"].pop(wid, None)
                s["panes"] = [pn for pn in s["panes"] if pn["window"] != wid]
        elif cmd == "list-panes":
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
                        if k in ("session", "links"): s["windows"][wid][k] = v
                        elif v is None: o.pop(k, None)
                        else: o[k] = v
'''
PATCHES = [
    ('        elif cmd == "list-panes":\n', SCRIPT_HOOK),
    # Real tmux: a failed command ends the rest of the command list.
    ('if t not in s["windows"]: rc = 1; continue', 'if t not in s["windows"]: rc = 1; break'),
    # A link may be [session, index]: a linked window has its own index per session.
    ('v = wvars(s, wid); v["session_name"] = ls',
     'v = wvars(s, wid); v["session_name"], v["window_index"] = ls if isinstance(ls, list) else (ls, v["window_index"])'),
    ('v = dict(v, session_name=ls)',
     'v = dict(v, session_name=ls[0], window_index=ls[1]) if isinstance(ls, list) else dict(v, session_name=ls)'),
    ('v["session_name"] = s["windows"][t]["links"][-1]',
     'l = s["windows"][t]["links"][-1]; v["session_name"] = l[0] if isinstance(l, list) else l'),
    # show-options -wqv (the indicator's window_state), and invocation markers.
    ('            if flag(c, "-gqv"):\n                out.append(s["globals"].get(name, ""))\n',
     '            if flag(c, "-gqv"):\n                out.append(s["globals"].get(name, ""))\n'
     '            elif flag(c, "-wqv"):\n'
     '                out.append(str(s["windows"].get(arg(c, "-t"), {}).get("opts", {}).get(name, "")))\n'),
    ('    rc, out = 0, []\n', '    rc, out = 0, []\n    s["calls"].append(["<inv>"])\n'),
    # FAKE_LIST_DELAY: a slow list-windows (outside the fake's own file
    # lock), to widen a read-decide-write race window on purpose.
    ('with p.with_suffix(".lock").open("a") as lock:\n',
     'import time\n'
     'if sys.argv[1:2] == ["list-windows"] and os.environ.get("FAKE_LIST_DELAY"):\n'
     '    time.sleep(float(os.environ["FAKE_LIST_DELAY"]))\n'
     'with p.with_suffix(".lock").open("a") as lock:\n'),
]
FAKE_TMUX = jw.FAKE_TMUX
for old, new in PATCHES:
    assert old in FAKE_TMUX, old
    FAKE_TMUX = FAKE_TMUX.replace(old, new)

# jw.DEFAULT_PROCS: claude pid 4242 owns this tty, so no window here is ever
# GC'd and every one of them has a live agent; only its state decides.
AGENT_TTY = "/dev/ttys900"
HIDDEN = ("agents", "tasks", "stash", "scratch", "btop-popup")


def A(session, index, state=None, slot=None, **opts):
    """A window; state/slot become @agent_state/@agent_slot when given."""
    if state is not None:
        opts["@agent_state"] = state
    if slot is not None:
        opts["@agent_slot"] = slot
    return jw.W(session, index, **opts)


class SlotTests(unittest.TestCase):
    def setUp(self):
        self.f = None

    def tearDown(self):
        if self.f:
            self.f.close()

    def env(self, windows, script=()):
        self.f = jw.FakeEnv(windows=windows,
                            panes=[{"window": w, "tty": AGENT_TTY} for w in windows])
        tmux = self.f.bin / "tmux"; tmux.write_text(FAKE_TMUX)
        self.edit(lambda s: s.update(script=list(script)))

    def edit(self, fn):
        s = self.f.read(); fn(s); self.f.state.write_text(json.dumps(s))

    def run_ticks(self, n):
        """Run a FRESH watcher for n ticks (calls cleared first); return the state.
        Under a UTF-8 locale, as tmux runs it: the fill order must still be bytes."""
        self.edit(lambda s: s.update(calls=[]))
        r = self.f.run("agent-tab-watcher.sh", timeout=10 + 2 * n,
                       AGENT_TAB_WATCHER_MAX_TICKS=str(n), LC_ALL="en_US.UTF-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.f.read()

    def slots(self, s):
        return {w: v["opts"]["@agent_slot"] for w, v in s["windows"].items()
                if "@agent_slot" in v.get("opts", {})}

    def slot_calls(self, s):
        return [c for c in s["calls"] if c[0] == "set-option" and "@agent_slot" in c]

    def invocations(self, s):
        """The call log split into tmux invocations (lists of commands)."""
        out = []
        for c in s["calls"]:
            if c == ["<inv>"]:
                out.append([])
            elif out:
                out[-1].append(c)
        return out

    def lock_path(self):
        return Path(str(self.f.tmp) + "/agent-slot.%d.lock" % os.getuid())

    # --- assignment ---

    def test_lowest_free_in_session_then_index_order(self):
        self.env({"@5": A("main", 2, "running"),
                  "@3": A("main", 1, "done"),
                  "@7": A("alpha", 4, "needs-input"),
                  "@8": A("main", 10, "failed"),
                  "@9": A("main", 3, "idle"),         # idle: not active
                  "@2": A("main", 9)})                # agent, no state: seeded idle, not active
        s = self.run_ticks(1)
        # Index is numeric (main:10 after main:2), session first (alpha < main).
        self.assertEqual(self.slots(s), {"@7": "1", "@3": "2", "@5": "3", "@8": "4"})
        self.assertEqual(s["windows"]["@2"]["opts"].get("@agent_state"), "idle")
        # Decided again from a fresh list-windows under the slot lock, then
        # ONE invocation for all four writes; the redraw is its own call
        # after it (never behind a write that can fail).
        inv = self.invocations(s)
        i = next(k for k, v in enumerate(inv) if v and "@agent_slot" in v[0])
        self.assertEqual(inv[i - 1][0][:2], ["list-windows", "-a"])
        self.assertEqual(inv[i], self.slot_calls(s))
        self.assertIn([["refresh-client", "-S"]], inv[i + 1:])
        self.assertFalse(self.lock_path().exists())

    def test_fill_order_is_byte_order_session_then_index_then_id(self):
        # Bytes, not the locale: "Alpha" < "Zeta" < "alpha" < "beta"
        # (en_US.UTF-8 collation would put alpha before Zeta).
        self.env({"@1": A("beta", 1, "running"),
                  "@2": A("alpha", 2, "running"),
                  "@3": A("Zeta", 9, "running"),
                  "@4": A("alpha", 10, "running"),
                  "@5": A("Alpha", 3, "running")})
        s = self.run_ticks(1)
        self.assertEqual(self.slots(s), {"@5": "1", "@3": "2", "@2": "3", "@4": "4", "@1": "5"})

    def test_linked_window_sorts_by_its_byte_smallest_visible_session(self):
        w1 = A("zeta", 1, "running"); w1["links"] = ["stash", "Main"]   # key: Main:1
        w2 = A("main", 1, "running")
        w3 = A("Aa", 5, "running"); w3["links"] = []
        w4 = A("agents", 1, "running"); w4["links"] = ["Ab"]          # hidden "agents" < "Ab": key Ab:1
        self.env({"@1": w1, "@2": w2, "@3": w3, "@4": w4})
        s = self.run_ticks(1)
        self.assertEqual(self.slots(s), {"@3": "1", "@4": "2", "@1": "3", "@2": "4"})

    def test_working_flags_count_and_idle_does_not(self):
        # @agent_cua (the watcher's own write, from activity.json) makes an
        # idle window active; a leftover @agent_workflow the watcher clears
        # this tick (no workflow behind the agent) does not.
        self.env({"@1": A("main", 1, "idle"),
                  "@2": A("main", 2, "idle", **{"@agent_workflow": "1"}),
                  "@3": A("main", 3, "done", **{"@agent_workflow": "1"})})
        act = self.f.home / "Library/Application Support/CuaNotch/activity.json"
        act.parent.mkdir(parents=True, exist_ok=True)
        act.write_text(json.dumps({"sessions": {"s1": {"agent_pid": 4242, "ts": time.time()}}}))
        s = self.run_ticks(1)
        # 4242 owns every pane here, so all three now drive an app (robot):
        # all active. Without the activity file only done counts.
        self.assertEqual(self.slots(s), {"@1": "1", "@2": "2", "@3": "3"})
        act.unlink()
        self.edit(lambda s: [w["opts"].pop("@agent_slot") for w in s["windows"].values()])
        s = self.run_ticks(1)
        self.assertNotIn("@agent_workflow", s["windows"]["@2"]["opts"])
        self.assertEqual(self.slots(s), {"@3": "1"})

    def test_gap_stays_and_new_agents_fill_it(self):
        new = lambda sess, idx: dict(A(sess, idx, "running"), tty=AGENT_TTY)
        self.env({"@1": A("main", 1, "running", "1"), "@2": A("main", 2, "done", "2"),
                  "@3": A("main", 3, "needs-input", "3"), "@4": A("main", 4, "running", "4")},
                 script=[{},
                         {"@3": {"@agent_state": "idle"}},     # tick 2: 3 finishes, goes idle
                         {"@2": None},                         # tick 3: 2's window closes
                         {"@9": new("zeta", 1), "@8": new("beta", 7)},   # tick 4: two arrive
                         {}])
        s = self.run_ticks(5)
        self.assertEqual(self.slots(s), {"@1": "1", "@4": "4", "@8": "2", "@9": "3"})
        # The only slot writes: 3 released, then the two newcomers, beta first.
        self.assertEqual(self.slot_calls(s), [
            ["set-option", "-uw", "-t", "@3", "@agent_slot"],
            ["set-option", "-w", "-t", "@8", "@agent_slot", "2"],
            ["set-option", "-w", "-t", "@9", "@agent_slot", "3"]])

    def test_next_active_agent_takes_the_freed_number(self):
        # The contract's example: 1-4 held, 3 goes idle, the next agent to
        # become active (an idle window starting a turn) takes 3.
        self.env({"@1": A("main", 1, "running", "1"), "@2": A("main", 2, "running", "2"),
                  "@3": A("main", 3, "running", "3"), "@4": A("main", 4, "running", "4"),
                  "@5": A("main", 5, "idle")},
                 script=[{}, {"@3": {"@agent_state": "idle"}}, {"@5": {"@agent_state": "running"}}, {}])
        s = self.run_ticks(4)
        self.assertEqual(self.slots(s), {"@1": "1", "@2": "2", "@4": "4", "@5": "3"})

    # --- release ---

    def test_parking_releases_and_unparking_takes_the_lowest_free(self):
        self.env({"@1": A("main", 1, "running", "1"), "@2": A("main", 2, "done", "2"),
                  "@3": A("main", 3, "idle")},
                 script=[{}, {"@2": {"session": "stash"}},              # parked
                         {"@3": {"@agent_state": "running"}},           # takes 2
                         {"@2": {"session": "main"}}, {}])              # unparked: 3
        s = self.run_ticks(5)
        self.assertEqual(self.slots(s), {"@1": "1", "@3": "2", "@2": "3"})
        self.assertIn(["set-option", "-uw", "-t", "@2", "@agent_slot"], self.slot_calls(s))
        self.assertFalse([c for c in self.slot_calls(s) if "@1" in c], s["calls"])

    def test_exited_agent_releases_once_its_state_is_collected(self):
        self.env({"@1": A("main", 1, "done", "1"), "@2": A("main", 2, "running", "2")})
        self.f.procs.write_text("ttys901 4343 zsh\n")       # the agent is gone
        s = self.run_ticks(3)
        self.assertEqual(self.slots(s), {"@1": "1", "@2": "2"})   # state held: still active
        s = self.run_ticks(6)                                     # past GC_TICKS
        self.assertNotIn("@agent_state", s["windows"]["@1"]["opts"])
        self.assertEqual(self.slots(s), {})

    def test_hidden_sessions_never_get_a_slot(self):
        wins = {"@%d" % (i + 1): A(sess, 1, "running") for i, sess in enumerate(HIDDEN)}
        wins["@9"] = A("agents", 2, "failed", "1")                # a stale slot there: released
        wins["@10"] = A("main", 1, slot="7")                      # a slot and nothing else: released
        self.env(wins)
        def no_agent(s):                                          # @10's pane runs a shell
            for p in s["panes"]:
                if p["window"] == "@10":
                    p["tty"] = "/dev/ttys901"
        self.edit(no_agent)
        s = self.run_ticks(2)
        self.assertEqual(self.slots(s), {})
        self.assertFalse([c for c in self.slot_calls(s) if "-w" in c], s["calls"])

    def test_linked_window_gets_one_slot_if_any_link_is_visible(self):
        w1 = A("stash", 1, "running"); w1["links"] = ["main"]      # visible via main
        w2 = A("main", 2, "done"); w2["links"] = ["agents", "work"]
        w3 = A("agents", 3, "running"); w3["links"] = ["tasks"]     # hidden everywhere
        self.env({"@1": w1, "@2": w2, "@3": w3})
        s = self.run_ticks(2)
        # Ordered by the FIRST VISIBLE link: main:1 (@1 via its link), main:2.
        self.assertEqual(self.slots(s), {"@1": "1", "@2": "2"})
        self.assertEqual(len(self.slot_calls(s)), 2, s["calls"])

    # --- repair ---

    def test_duplicates_and_junk_are_repaired(self):
        self.env({"@7": A("main", 7, "running", "1"),
                  "@100": A("main", 1, "running", "2"),       # duplicate: @100 > @12 numerically
                  "@12": A("main", 12, "running", "2"),
                  "@3": A("main", 3, "done", "0"),
                  "@5": A("main", 5, "done", "x"),
                  "@6": A("main", 6, "done", "-1"),
                  "@8": A("main", 8, "done", "007"),          # not canonical: junk
                  "@9": A("main", 9, "idle", "abc")})         # junk, inactive: just released
        s = self.run_ticks(2)
        # Reassigned in (session, index) order: main:1 (@100), 3, 5, 6, 8.
        self.assertEqual(self.slots(s), {"@7": "1", "@12": "2", "@100": "3", "@3": "4",
                                         "@5": "5", "@6": "6", "@8": "7"})
        self.assertFalse([c for c in self.slot_calls(s) if "@7" in c or "@12" in c], s["calls"])
        self.assertIn(["set-option", "-uw", "-t", "@9", "@agent_slot"], self.slot_calls(s))

    # --- restart / steady state ---

    def test_restart_keeps_slots_and_steady_ticks_write_nothing(self):
        self.env({"@1": A("main", 1, "running", "5"), "@2": A("main", 2, "done", "2"),
                  "@3": A("main", 3, "needs-input")})
        s = self.run_ticks(1)
        self.assertEqual(self.slots(s), {"@1": "5", "@2": "2", "@3": "1"})
        self.assertEqual(self.slot_calls(s), [["set-option", "-w", "-t", "@3", "@agent_slot", "1"]])
        s = self.run_ticks(3)                                     # a restart, then steady ticks
        self.assertEqual(self.slots(s), {"@1": "5", "@2": "2", "@3": "1"})
        self.assertEqual(self.slot_calls(s), [])

    # --- idle + pending, the missing-pane hold, failed writes, links ---

    def test_answered_prompt_keeps_its_number_and_a_seen_done_frees_it(self):
        # clear-current on needs-input writes idle + @agent_pending (one
        # command); the turn resumes as running with pending consumed. The
        # number never moves. A seen done (idle, no pending) is released.
        self.env({"@1": A("main", 1, "needs-input", "1"), "@2": A("main", 2, "done", "2"),
                  "@3": A("main", 3, "running", "3")},
                 script=[{},
                         {"@1": {"@agent_state": "idle", "@agent_pending": str(int(time.time()))},
                          "@2": {"@agent_state": "idle"}},
                         {},
                         {"@1": {"@agent_state": "running", "@agent_pending": None}},
                         {}])
        s = self.run_ticks(5)
        self.assertEqual(self.slots(s), {"@1": "1", "@3": "3"})
        self.assertEqual(self.slot_calls(s), [["set-option", "-uw", "-t", "@2", "@agent_slot"]])

    def test_pending_holds_a_slot_for_600_s_only_and_junk_never(self):
        # An approval answered with No/Esc fires no resume hook: idle +
        # @agent_pending would hold the number forever. It counts only while
        # the stamp is an epoch no older than 600 s; past that, or junk, the
        # slot is released and the watcher unsets the stamp itself.
        now = int(time.time())
        wins = {"@1": A("main", 1, "idle", "1", **{"@agent_pending": str(now - 500)}),   # fresh
                "@2": A("main", 2, "idle", "2", **{"@agent_pending": str(now - 601)}),   # expired
                "@3": A("main", 3, "idle", "3", **{"@agent_pending": "abc"}),
                "@4": A("main", 4, "idle", "4", **{"@agent_pending": "-5"}),
                "@5": A("main", 5, "idle", "5", **{"@agent_pending": "12 34"}),
                "@6": A("main", 6, "idle", "6", **{"@agent_pending": "1" * 19}),         # too long
                "@7": A("main", 7, "idle", **{"@agent_pending": "x"}),                   # never gets one
                "@8": A("main", 8, "running", "8", **{"@agent_pending": "abc"})}         # not idle: untouched
        self.env(wins)
        s = self.run_ticks(1)
        self.assertEqual(self.slots(s), {"@1": "1", "@8": "8"})
        o = {w: s["windows"][w]["opts"] for w in wins}
        self.assertEqual(o["@1"]["@agent_pending"], str(now - 500))
        for w in ("@2", "@3", "@4", "@5", "@6", "@7"):
            self.assertNotIn("@agent_pending", o[w], w)
        self.assertEqual(o["@8"]["@agent_pending"], "abc")
        s = self.run_ticks(2)                                     # and quiet from then on
        self.assertEqual(self.slot_calls(s), [])
        self.assertFalse([c for c in s["calls"] if "@agent_pending" in c], s["calls"])

    def test_hook_treats_an_expired_pending_slot_as_free(self):
        # Before any tick has unset it: the hook's own read must already see
        # idle + a 601 s old stamp as NOT active (the TTL lives in the
        # shared slot_active), and a fresh one as active.
        now = int(time.time())
        self.env({"@1": A("main", 1, "idle", "1", **{"@agent_pending": str(now - 601)}),
                  "@2": A("main", 2, "idle", "2", **{"@agent_pending": str(now - 30)}),
                  "@3": A("main", 3, "idle")})
        s = self.hook("@3", "running")
        self.assertEqual(self.slots(s), {"@2": "2", "@3": "1"})

    def test_seen_failed_frees_its_slot(self):
        # clear-current on a FAILED turn: idle with no pending (a dead turn
        # never resumes), so the number is released; needs-input keeps its.
        self.env({"@1": A("main", 1, "failed", "1"), "@2": A("main", 2, "needs-input", "2")})
        shutil.copy(INDICATOR, self.f.scripts / "agent-tab-indicator.sh")
        for w in ("@1", "@2"):
            r = subprocess.run(["bash", str(self.f.scripts / "agent-tab-indicator.sh"), "clear-current", w],
                               text=True, capture_output=True, timeout=20, env=self.f.env(TMUX="/fake/sock,1,0"))
            self.assertEqual(r.returncode, 0, r.stderr)
        s = self.f.read()
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "idle")
        self.assertNotIn("@agent_pending", s["windows"]["@1"]["opts"])
        self.assertIn("@agent_pending", s["windows"]["@2"]["opts"])
        s = self.run_ticks(1)
        self.assertEqual(self.slots(s), {"@2": "2"})

    def test_heartbeat_rearms_within_the_window(self):
        # An answered prompt 5 minutes ago: still inside both windows (600 s
        # slot, 3600 s re-arm). The heartbeat turns it back to running and
        # the number never moves.
        self.env({"@1": A("main", 1, "idle", "3", **{"@agent_pending": str(int(time.time()) - 300)})})
        s = self.run_ticks(1)
        self.assertEqual(self.slots(s), {"@1": "3"})
        s = self.hook("@1", "heartbeat")
        o = s["windows"]["@1"]["opts"]
        self.assertEqual((o["@agent_state"], o["@agent_slot"]), ("running", "3"))
        self.assertNotIn("@agent_pending", o)

    def test_missing_agent_pane_holds_flags_and_slot_until_the_gc(self):
        # tmux-thumbs swaps the agent pane out for seconds: no agent pane, so
        # the gear/robot would drop at once - and with the state idle the
        # slot with it. Both are held for GC_TICKS ticks, then collected.
        self.env({"@1": A("main", 1, "idle", "1", **{"@agent_workflow": "1"}),
                  "@2": A("main", 2, "idle", "2", **{"@agent_cua": "1"}),
                  "@3": A("main", 3, "running", "3")})
        self.f.procs.write_text("ttys901 4343 zsh\n")             # every agent pane gone
        s = self.run_ticks(3)
        self.assertEqual(self.slots(s), {"@1": "1", "@2": "2", "@3": "3"})
        self.assertEqual(s["windows"]["@1"]["opts"].get("@agent_workflow"), "1")
        self.assertEqual(s["windows"]["@2"]["opts"].get("@agent_cua"), "1")
        self.assertEqual(self.slot_calls(s), [])
        self.assertFalse([c for c in s["calls"] if c[0] == "set-option"
                          and ("@agent_workflow" in c or "@agent_cua" in c)], s["calls"])
        s = self.run_ticks(6)                                     # a real exit: past GC_TICKS
        for w in ("@1", "@2", "@3"):
            o = s["windows"][w]["opts"]
            self.assertFalse({"@agent_workflow", "@agent_cua", "@agent_state", "@agent_slot"} & set(o), (w, o))

    def test_a_failed_slot_write_never_drops_the_redraw(self):
        # @9 closes between the locked re-read and the write: its set fails,
        # which ends that command list (the fake does what tmux does), so @8's
        # set behind it waits a tick. The tick's redraw (the idle seed of @4
        # changed state) is its own call and still happens.
        self.env({"@9": A("aaa", 1, "running"), "@8": A("bbb", 1, "running"), "@4": A("ccc", 1)})
        self.edit(lambda s: s.update(vanish_after_list_windows=["@9"]))
        s = self.run_ticks(1)
        self.assertEqual(self.slots(s), {})
        self.assertIn(["set-option", "-w", "-t", "@9", "@agent_slot", "1"], s["calls"])
        self.assertIn([["refresh-client", "-S"]], self.invocations(s))
        self.assertFalse(self.lock_path().exists())
        s = self.run_ticks(1)
        self.assertEqual(self.slots(s), {"@8": "1"})

    def test_linked_window_uses_its_index_in_the_smallest_session(self):
        w1 = A("zeta", 1, "running"); w1["links"] = [["alpha", 9]]   # key alpha:9, not alpha:1
        w2 = A("alpha", 3, "running")
        w3 = A("stash", 1, "running"); w3["links"] = [["beta", 2], ["Beta", 7]]   # key Beta:7
        self.env({"@1": w1, "@2": w2, "@3": w3})
        s = self.run_ticks(1)
        self.assertEqual(self.slots(s), {"@3": "1", "@2": "2", "@1": "3"})

    def test_watcher_waits_for_the_slot_lock(self):
        # A live holder (this test process): the tick gives up after ~1 s and
        # writes nothing; once the lock is free the next tick stamps.
        self.env({"@1": A("main", 1, "running")})
        self.lock_path().write_text("%d %d\n" % (os.getpid(), int(time.time())))
        s = self.run_ticks(1)
        self.assertEqual(self.slots(s), {})
        self.assertTrue(self.lock_path().exists())                # not ours: left alone
        self.lock_path().unlink()
        s = self.run_ticks(1)
        self.assertEqual(self.slots(s), {"@1": "1"})

    def test_a_dead_holders_lock_is_broken(self):
        p = subprocess.Popen(["true"]); p.wait()                  # a pid that is gone
        self.env({"@1": A("main", 1, "running")})
        self.lock_path().write_text("%d %d\n" % (p.pid, int(time.time())))
        s = self.run_ticks(1)
        self.assertEqual(self.slots(s), {"@1": "1"})
        self.assertFalse(self.lock_path().exists())

    # --- the hook's stamp (agent-tab-indicator.sh) ---

    def hook(self, win, mode, payload="{}", **extra):
        shutil.copy(INDICATOR, self.f.scripts / "agent-tab-indicator.sh")
        self.edit(lambda s: s.update(calls=[]))
        t0 = time.monotonic()
        r = subprocess.run(["bash", str(self.f.scripts / "agent-tab-indicator.sh"), mode, "claude"],
                           input=payload, text=True, capture_output=True, timeout=20,
                           env=self.f.env(TMUX="/fake/sock,1,0", TMUX_PANE=win, **extra))
        self.elapsed = time.monotonic() - t0
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.f.read()

    def test_hook_stamps_the_lowest_free_slot_with_the_state(self):
        self.env({"@1": A("main", 1, "running", "1"), "@2": A("main", 2, "done", "3"),
                  "@3": A("main", 3, "idle")})
        s = self.hook("@3", "running")
        self.assertEqual(self.slots(s), {"@1": "1", "@2": "3", "@3": "2"})
        # The slot rides in the state write's own invocation.
        inv = next(v for v in self.invocations(s) if ["set-option", "-w", "-t", "@3", "@agent_state", "running"] in v)
        self.assertIn(["set-option", "-w", "-t", "@3", "@agent_slot", "2"], inv)
        self.assertFalse(self.lock_path().exists())
        # The watcher agrees and writes nothing.
        s = self.run_ticks(1)
        self.assertEqual(self.slots(s), {"@1": "1", "@2": "3", "@3": "2"})
        self.assertEqual(self.slot_calls(s), [])

    def test_hook_keeps_a_valid_slot_without_listing_or_locking(self):
        self.env({"@1": A("main", 1, "idle", "4")})
        s = self.hook("@1", "running")
        self.assertEqual(self.slots(s), {"@1": "4"})
        self.assertFalse([c for c in s["calls"] if c[0] == "list-windows"], s["calls"])
        self.assertEqual(self.slot_calls(s), [])

    def test_hook_moves_a_stale_number_in_the_same_command(self):
        # @2 went idle (inactive) but no tick has released its 1 yet: the
        # stamp takes 1 and unsets it on @2 in the same invocation, so two
        # windows never both carry it.
        self.env({"@1": A("main", 1, "running", "2"), "@2": A("main", 2, "idle", "1"),
                  "@3": A("main", 3, "idle")})
        s = self.hook("@3", "running")
        self.assertEqual(self.slots(s), {"@1": "2", "@3": "1"})
        inv = next(v for v in self.invocations(s) if ["set-option", "-w", "-t", "@3", "@agent_slot", "1"] in v)
        self.assertIn(["set-option", "-uw", "-t", "@2", "@agent_slot"], inv)

    def test_hook_leaves_hidden_sessions_and_idle_to_the_watcher(self):
        self.env({"@1": A("agents", 1, "idle"), "@2": A("main", 2, "running")})
        s = self.hook("@1", "running")                            # headless claude -p in `agents`
        self.assertEqual(self.slots(s), {})
        self.assertFalse([c for c in s["calls"] if c[0] == "list-windows"], s["calls"])
        s = self.hook("@2", "idle")                               # SessionStart: idle, not active
        self.assertEqual(self.slots(s), {})

    def test_hook_skips_the_stamp_when_the_lock_is_held(self):
        # A live holder: the hook gives up after SLOT_GIVEUP polls (~75 ms),
        # not after the old ~1.3 s wait; the state still lands.
        self.env({"@1": A("main", 1, "idle"), "@2": A("main", 2, "idle")})
        self.hook("@2", "running")                                # unblocked baseline (stamps)
        base = self.elapsed
        self.lock_path().write_text("%d %d\n" % (os.getpid(), int(time.time())))
        s = self.hook("@1", "running")
        self.assertEqual(s["windows"]["@1"]["opts"]["@agent_state"], "running")
        self.assertNotIn("@agent_slot", s["windows"]["@1"]["opts"])
        self.assertLess(self.elapsed - base, 0.5, (self.elapsed, base))
        self.lock_path().unlink()

    def test_a_held_lock_older_than_slot_stale_is_broken(self):
        # A live holder that has held it 5 s is wedged (a section takes ms).
        self.env({"@1": A("main", 1, "idle")})
        self.lock_path().write_text("%d %d\n" % (os.getpid(), int(time.time()) - 5))
        s = self.hook("@1", "running")
        self.assertEqual(self.slots(s), {"@1": "1"})
        self.assertFalse(self.lock_path().exists())

    def test_answered_prompt_round_trip_through_the_hook(self):
        # needs-input -> focus (clear-current: idle + pending in ONE command)
        # -> heartbeat (running + pending consumed in ONE command): the
        # number is held throughout and the watcher never moves it.
        self.env({"@1": A("main", 1, "needs-input", "1"), "@2": A("main", 2, "running", "2")})
        shutil.copy(INDICATOR, self.f.scripts / "agent-tab-indicator.sh")
        self.edit(lambda s: s.update(calls=[]))
        r = subprocess.run(["bash", str(self.f.scripts / "agent-tab-indicator.sh"), "clear-current", "@1"],
                           text=True, capture_output=True, timeout=20,
                           env=self.f.env(TMUX="/fake/sock,1,0"))
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.f.read()
        o = s["windows"]["@1"]["opts"]
        self.assertEqual((o["@agent_state"], o["@agent_slot"]), ("idle", "1"))
        inv = next(v for v in self.invocations(s) if ["set-option", "-w", "-t", "@1", "@agent_state", "idle"] in v)
        self.assertTrue([c for c in inv if "@agent_pending" in c], inv)
        s = self.run_ticks(1)
        self.assertEqual(self.slots(s), {"@1": "1", "@2": "2"})
        s = self.hook("@1", "heartbeat")
        o = s["windows"]["@1"]["opts"]
        self.assertEqual(o["@agent_state"], "running")
        self.assertNotIn("@agent_pending", o)
        inv = next(v for v in self.invocations(s) if ["set-option", "-w", "-t", "@1", "@agent_state", "running"] in v)
        self.assertIn(["set-option", "-uw", "-t", "@1", "@agent_pending"], inv)
        self.assertEqual(self.slots(s), {"@1": "1", "@2": "2"})

    def test_concurrent_hooks_never_share_a_number(self):
        # Six agents start a turn at once: every stamp runs under the slot
        # lock, so the six numbers are distinct (without it, each hook reads
        # the same "lowest free" and all of them take 1 - checked by removing
        # the lock). The fake's list-windows is slowed to 0.12 s so the
        # sections really overlap, and every hook gets its payload from a
        # file: fed through pipes one by one, each hook's `cat` would wait
        # for its EOF and serialize them all.
        wins = {"@%d" % i: A("main", i, "idle") for i in range(1, 7)}
        self.env(wins)
        shutil.copy(INDICATOR, self.f.scripts / "agent-tab-indicator.sh")
        payload = self.f.dir / "payload"; payload.write_text("{}")
        procs = []
        for w in wins:
            with payload.open() as fh:
                procs.append(subprocess.Popen(
                    ["bash", str(self.f.scripts / "agent-tab-indicator.sh"), "running", "claude"],
                    stdin=fh, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    env=self.f.env(TMUX="/fake/sock,1,0", TMUX_PANE=w, FAKE_LIST_DELAY="0.12")))
        for p in procs:
            _, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, err)
        s = self.f.read()
        slots = self.slots(s)
        # A hook that keeps meeting the same live holder gives up after a
        # few polls (~75 ms) instead of queueing, so not every hook stamps -
        # but no two ever share a number, and the sections never interleave.
        self.assertTrue(slots, s["calls"])
        self.assertEqual(len(set(slots.values())), len(slots), slots)
        seq = [c[0] for c in s["calls"] if c[0] == "list-windows" or "@agent_slot" in c]
        self.assertEqual(seq, ["list-windows", "set-option"] * (len(seq) // 2), seq)
        self.assertFalse(self.lock_path().exists())
        # The watcher's next tick stamps the rest: 1..6, the hooks' kept.
        s2 = self.run_ticks(1)
        self.assertEqual(sorted(self.slots(s2).values(), key=int), [str(i) for i in range(1, 7)])
        for w, v in slots.items():
            self.assertEqual(self.slots(s2)[w], v)

    def test_slot_core_is_identical_in_both_scripts(self):
        def core(p):
            t = p.read_text()
            return t[t.index("# >>> SLOT CORE >>>"):t.index("# <<< SLOT CORE <<<")]
        self.assertEqual(core(jw.WATCHER), core(INDICATOR))

    def test_exclude_list_is_agent_jumps(self):
        m = re.search(r'^EXCLUDE="([^"]*)"$', jw.JUMP.read_text(), re.M)
        n = re.search(r'^SLOT_EXCLUDE="([^"]*)"$', jw.WATCHER.read_text(), re.M)
        self.assertTrue(m and n)
        self.assertEqual(n.group(1), m.group(1))
        self.assertEqual(m.group(1).split(), list(HIDDEN))


if __name__ == "__main__":
    unittest.main()
