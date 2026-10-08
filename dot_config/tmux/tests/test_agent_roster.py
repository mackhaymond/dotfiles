"""agent-roster.py's pure model: parsing, grouping, key decoding. No real tmux, no terminal.

NeedsOrderTests runs the REAL agent-jump.sh against test_agent_jump_watcher's
fake tmux, to pin the roster's in-process NEEDS YOU order to prefix d's."""
import importlib.util
import locale
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import unittest

SRC = Path(os.environ.get(
    "AGENT_ROSTER_SOURCE",
    str(Path.home() / ".local/share/chezmoi/dot_config/tmux/scripts/executable_agent-roster.py")))
spec = importlib.util.spec_from_file_location("agent_roster", SRC)
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_agent_jump_watcher import JUMP, FakeEnv, W  # noqa: E402  (the fake tmux, shared)

TMUX_CONF = Path(os.environ.get(
    "AGENT_TMUX_CONF", str(Path.home() / ".local/share/chezmoi/dot_config/tmux/tmux.conf.tmpl")))
WEZTERM_CONF = Path(os.environ.get(
    "AGENT_WEZTERM_CONF", str(Path.home() / ".local/share/chezmoi/dot_config/wezterm/wezterm.lua.tmpl")))

US = "\x1f"
SAVED_WATCHER_AGE = R.watcher_age


def line(session, index, wid, state="", summary="", workflow="", cua="", since="", attached=0, name="zsh",
         stash_label="", stash_session="", stash_ts="", active="0", panes="1", path="", kind="", detail_kind="",
         detail=""):
    return US.join([session, str(index), wid, name, state, summary, workflow, cua, since, str(attached), "1",
                    stash_label, stash_session, stash_ts, active, panes, path, kind, detail_kind, detail])


WINDOWS = "\n".join([
    line("main", 1, "@1", "idle", "proj/One", since="100 idle", attached=50),
    line("main", 2, "@2"),                                              # plain shell
    line("main", 3, "@3", "running", "proj/Three", attached=50),
    line("work", 1, "@4", "failed", "proj/Four", since="300 failed", attached=90),
    line("work", 2, "@5", "done", "proj/Five", workflow="1", attached=90),
    line("agents", 1, "@6", "running", "pty"),                          # never shown
    line("stash", 1, "@7", "needs-input", "proj/Parked"),
    line("stash", 2, "@8"),                                             # plain parked shell
    line("stash", 3, "@9", summary="stale/summary", stash_label="proj/Suspended", stash_session="abc-123",
         stash_ts="500"),
    line("main", 4, "@10", summary="live/summary", stash_label="frozen/label"),   # left the stash some other way
])


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.ws = R.parse_windows(WINDOWS)

    def kinds(self, items):
        out = []
        for it in items:
            if it["kind"] == "win":
                out.append(it["w"]["id"])
            elif it["kind"] == "sess":
                out.append("[" + it["name"] + "]")
            else:
                out.append(it["kind"])
        return out

    def test_parse(self):
        self.assertEqual(len(self.ws), 10)
        one = self.ws[0]
        self.assertEqual((one["index"], one["since_t"], one["label"]), (1, 100, "proj/One"))
        self.assertEqual(self.ws[1]["label"], "zsh")
        self.assertEqual(R.parse_windows("garbage\n" + WINDOWS)[0]["id"], "@1")

    def test_layout(self):
        items = R.build_items(self.ws, ["@4"], cur_win="@2")
        self.assertEqual(self.kinds(items),
                         ["label", "@4",                       # NEEDS YOU, in agent-jump's order
                          "[main]", "@1", "@2", "@3",          # current session first; @2 shown: it is under the popup
                          "[work]", "@4", "@5",
                          "parked"])                           # stash collapsed; agents never

    def test_plain_windows_hidden_unless_current_or_all(self):
        items = R.build_items(self.ws, [], cur_win="@1")
        self.assertNotIn("@2", self.kinds(items))
        self.assertIn("@2", self.kinds(R.build_items(self.ws, [], cur_win="@1", show_all=True)))

    def test_parked(self):
        items = R.build_items(self.ws, [], cur_win="@1")
        parked = [it for it in items if it["kind"] == "parked"][0]
        self.assertEqual((parked["n"], parked["attn"]), (3, 1))
        # Every parked tab is listed, `a` or not: a suspended agent has no
        # @agent_state left, and a parked shell was parked on purpose too.
        opened = self.kinds(R.build_items(self.ws, [], cur_win="@1", parked_open=True))
        self.assertEqual(opened[-4:], ["parked", "@7", "@8", "@9"])
        nine = [w for w in self.ws if w["id"] == "@9"][0]
        self.assertEqual((nine["label"], nine["stash_t"]), ("proj/Suspended", 500))
        ten = [w for w in self.ws if w["id"] == "@10"][0]
        self.assertEqual(ten["label"], "live/summary")          # @stash_label only counts while parked

    def test_parked_rows_say_suspended_or_parked(self):
        roster = R.Roster("/dev/ttys999")
        strip = lambda s: R.re.sub(r"\x1b\[[0-9;]*m", "", s)
        by = {w["id"]: w for w in self.ws}
        eight = strip(roster.row({"kind": "win", "w": by["@8"], "long": False}, 120, False, 1000, False))
        nine = strip(roster.row({"kind": "win", "w": by["@9"], "long": False}, 120, False, 1000, False))
        self.assertRegex(eight, r"zsh\s+parked")
        self.assertRegex(nine, r"proj/Suspended\s+suspended\s+8m")

    def test_done_with_fleet_out_is_not_attention(self):
        five = [w for w in self.ws if w["id"] == "@5"][0]
        self.assertFalse(R.is_attn(five))
        self.assertTrue(R.in_flight(five))

    def test_filter_is_flat_and_skips_hidden(self):
        items = R.build_items(self.ws, ["@4"], cur_win="@1", query="proj")
        ids = self.kinds(items)
        self.assertNotIn("@6", ids)
        self.assertEqual(ids[-2:], ["@7", "@9"])               # parked sorts last
        self.assertTrue(all(it["kind"] == "win" for it in items))

    def test_tasks_hidden(self):
        # CuaNotch's broker: agent-jump.sh and the session pickers skip it too
        self.assertIn("tasks", R.HIDDEN)
        ws = R.parse_windows(WINDOWS + "\n" + line("tasks", 1, "@20", "running", "broker", attached=999))
        self.assertNotIn("[tasks]", self.kinds(R.build_items(ws, [], cur_win="@1", show_all=True)))
        self.assertNotIn("@20", self.kinds(R.build_items(ws, [], cur_win="@1", query="broker")))

    def test_clip(self):
        self.assertEqual(R.clip("abcdef", 4), "abc…")
        self.assertEqual(R.clip_ansi("\x1b[1mabcdef", 3), "\x1b[1mabc")
        self.assertEqual(R.ago(None, 0), "")
        self.assertEqual((R.ago(0, 59), R.ago(0, 61), R.ago(0, 7300)), ("59s", "1m", "2h"))


class KeyTests(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(R.parse_keys("\x1b[A\x1b[B\x1b[Z\t\r j"),
                         (["up", "down", "btab", "tab", "enter", "space", "j"], ""))
        # SS3 and unknown CSI sequences are swallowed, never read as esc
        self.assertEqual(R.parse_keys("\x1bOF\x1b[1;5Cq\x1b[200~"), (["q"], ""))
        self.assertEqual(R.parse_keys("\x03"), (["ctrl-c"], ""))

    def test_lone_esc_waits(self):
        # Without the wait it is carried, not esc; after the wait it is esc.
        self.assertEqual(R.parse_keys("j\x1b"), (["j"], "\x1b"))
        self.assertEqual(R.parse_keys("\x1b", final=True), (["esc"], ""))
        r = R.KeyReader()
        self.assertEqual(r.feed(b"\x1b"), [])
        self.assertTrue(r.pending)
        self.assertEqual(r.flush(), ["esc"])
        self.assertFalse(r.pending)

    def test_partial_sequences_are_carried(self):
        for head, tail, want in (("\x1b[", "A", ["up"]), ("\x1b[1;", "5C", []),
                                 ("\x1bO", "B", ["down"]), ("\x1b[6", "~k", ["pgdn", "k"])):
            keys, left = R.parse_keys("j" + head)
            self.assertEqual((keys, left), (["j"], head), head)
            r = R.KeyReader()
            self.assertEqual(r.feed(("j" + head).encode()), ["j"])
            self.assertTrue(r.pending)
            self.assertEqual(r.feed(tail.encode()), want, head)
            self.assertFalse(r.pending)
        # A sequence that never completes is dropped, never esc.
        self.assertEqual(R.parse_keys("\x1b[1;", final=True), ([], ""))
        r = R.KeyReader()
        r.feed(b"\x1b[")
        self.assertEqual(r.flush(), [])

    def test_alt_key_is_not_esc(self):
        self.assertEqual(R.parse_keys("\x1bj"), ([], ""))
        self.assertEqual(R.parse_keys("\x1bxk"), (["k"], ""))
        self.assertEqual(R.parse_keys("\x1b\x1b", final=True), (["esc", "esc"], ""))

    def test_split_utf8(self):
        r = R.KeyReader()
        data = "é😀".encode()
        self.assertEqual(r.feed(data[:1]), [])
        self.assertEqual(r.feed(data[1:4]), ["é"])
        self.assertFalse(r.pending)
        self.assertEqual(r.feed(data[4:]), ["😀"])


class PaneTests(unittest.TestCase):
    PS = "\n".join(["??       launchd", "ttys001  -zsh", "ttys002  /opt/homebrew/bin/codex",
                    "ttys002  node", "ttys003  2.1.291", "ttys004  /usr/bin/claude",
                    "ttys005  10.0.0.1", "ttys006  opencode"])

    def panes(self, *rows):
        return "\n".join(US.join(r) for r in rows)

    def test_single_pane_is_it(self):
        self.assertEqual(R.pick_agent_pane(self.panes(("%1", "/dev/ttys001", "1")), None), ("%1", None))

    def test_matched_by_tty_not_active(self):
        # codex shows as `node` in pane_current_command; the shell is active.
        panes = self.panes(("%1", "/dev/ttys001", "1"), ("%2", "/dev/ttys002", "0"))
        self.assertEqual(R.pick_agent_pane(panes, self.PS), ("%2", None))
        panes = self.panes(("%1", "/dev/ttys003", "0"), ("%2", "/dev/ttys001", "1"))
        self.assertEqual(R.pick_agent_pane(panes, self.PS), ("%1", None))

    def test_refuses_to_guess(self):
        # Split, no agent pane (IP-like and opencode comms don't count): refuse.
        panes = self.panes(("%1", "/dev/ttys001", "1"), ("%2", "/dev/ttys005", "0"), ("%3", "/dev/ttys006", "0"))
        self.assertEqual(R.pick_agent_pane(panes, self.PS), (None, R.PANE_UNSURE))
        self.assertEqual(R.pick_agent_pane(panes, None), (None, R.PANE_UNSURE))
        # Two agent panes: the active one, else refuse.
        two = self.panes(("%1", "/dev/ttys003", "0"), ("%2", "/dev/ttys004", "1"))
        self.assertEqual(R.pick_agent_pane(two, self.PS), ("%2", None))
        two = self.panes(("%1", "/dev/ttys003", "0"), ("%2", "/dev/ttys004", "0"), ("%3", "/dev/ttys001", "1"))
        self.assertEqual(R.pick_agent_pane(two, self.PS), (None, R.PANE_UNSURE))
        self.assertEqual(R.pick_agent_pane("", self.PS), (None, R.PANE_GONE))

    def test_is_agent_comm(self):
        for c in ("claude", "codex", "/usr/local/bin/codex", "2.1.291"):
            self.assertTrue(R.is_agent_comm(c), c)
        for c in ("node", "-zsh", "10.0.0.1", "1.2.3-beta", "opencode", "claude-helper"):
            self.assertFalse(R.is_agent_comm(c), c)


class ActTests(unittest.TestCase):
    """Roster.act driven by keys, with every side effect stubbed out."""

    def setUp(self):
        self.calls = []
        self.saved = (R.run_bg, R.agent_pane, R.subprocess.run, R.tmux)
        R.run_bg = lambda cmd: self.calls.append(("bg", cmd))
        R.agent_pane = lambda win: (self.calls.append(("pane", win)) or ("%9", None))
        R.tmux = lambda *a: None

        def fake_run(argv, **kw):
            self.calls.append(("run", argv))
            return None
        R.subprocess.run = fake_run
        self.r = R.Roster("/dev/ttys999")
        self.r.windows = R.parse_windows(WINDOWS)
        self.r.cur_win = "@1"
        self.r.rebuild()

    def tearDown(self):
        R.run_bg, R.agent_pane, R.subprocess.run, R.tmux = self.saved

    def test_park_needs_confirm(self):
        self.assertEqual(self.r.sel_key, ("sess", "main", "@1"))   # default: the window you are on
        self.assertFalse(self.r.act("H"))
        self.assertEqual(self.calls, [])                    # nothing parked yet
        self.assertEqual(self.r.confirm["action"], "park")
        self.assertFalse(self.r.act("n"))
        self.assertEqual((self.calls, self.r.confirm), ([], None))
        self.r.act("H"); self.r.act("y")
        self.assertEqual(len(self.calls), 1)
        self.assertIn("stash '@1'", self.calls[0][1])
        self.assertTrue(self.r.msg.startswith("parking "))  # a request: stash.sh may refuse a busy agent

    def test_typing_into_the_popup_parks_nothing(self):
        self.assertFalse(self.r.handle(list("Hello there")))
        self.assertFalse(any("stash" in str(c) for c in self.calls))

    def test_confirm_never_answered_by_its_own_batch(self):
        # A paste / fast "Hy" or "xy" arrives in ONE read: the y is dropped
        # with the rest of the batch, and the question stays open.
        for burst in ("Hy", "xy", "Hyy", "xYy"):
            self.calls.clear(); self.r.confirm = None
            self.assertFalse(self.r.handle(list(burst)))
            self.assertEqual(self.calls, [], burst)
            self.assertIsNotNone(self.r.confirm, burst)
        # The answer in a later read does act.
        self.r.confirm = None
        self.r.handle(["H"]); self.r.handle(["y"])
        self.assertEqual(len(self.calls), 1)
        self.assertIn("stash '@1'", self.calls[0][1])

    def test_stale_confirm_is_cancelled(self):
        # x pressed on @1 in `main`; the tab is parked before the y arrives.
        self.r.handle(["x"])
        self.r.windows = R.parse_windows(WINDOWS)           # the refresh tick: fresh dicts
        for w in self.r.windows:
            if w["id"] == "@1":
                w["session"] = "stash"
        self.r.handle(["y"])
        self.assertEqual(self.calls, [])                    # neither closed-tabs nor kill-many
        self.assertIsNone(self.r.confirm)
        self.assertIn("moved", self.r.msg)
        # Window gone altogether between H and y.
        self.r.windows = [w for w in self.r.windows if w["id"] != "@3"]
        self.r.confirm = {"action": "park", "w": {"id": "@3", "session": "main"}}
        self.r.handle(["y"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.r.msg, R.PANE_GONE)

    def test_close_needs_confirm_and_uses_agent_pane(self):
        self.r.act("x")
        self.assertEqual(self.calls, [])
        self.r.act("y")
        self.assertEqual(self.calls[0], ("pane", "@1"))
        self.assertIn("close '%9'", self.calls[1][1])

    def test_close_refused_says_why(self):
        R.agent_pane = lambda win: (None, R.PANE_UNSURE)
        self.r.act("x"); self.r.act("y")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.r.msg, R.PANE_UNSURE)

    def test_goto_timeout_keeps_popup(self):
        def hang(argv, **kw):
            raise R.subprocess.TimeoutExpired(argv, 10)
        R.subprocess.run = hang
        self.assertFalse(self.r.act("enter"))
        self.assertIn("timed out", self.r.msg)

        def missing(argv, **kw):
            raise FileNotFoundError(2, "No such file or directory")
        R.subprocess.run = missing
        self.assertFalse(self.r.act("d"))
        self.assertIn("failed", self.r.msg)

    def test_goto_closes_popup(self):
        self.assertTrue(self.r.act("enter"))
        self.assertEqual(self.calls[0][1][2:], ["goto", "/dev/ttys999", "@1", "main"])


class NavTests(unittest.TestCase):
    """Selection keys are per ROW: a window listed twice must not trap move()."""
    strip = staticmethod(lambda s: R.re.sub(r"\x1b\[[0-9;]*m", "", s))

    def roster(self, text, needs, cur):
        r = R.Roster("/dev/ttys999")
        r.windows, r.needs, r.cur_win = R.parse_windows(text), needs, cur
        r.rebuild()
        return r

    def walk(self, r, n):
        seen = [r.sel_key]
        for _ in range(n):
            r.act("down")
            seen.append(r.sel_key)
        return seen

    def highlighted(self, r):
        return [l for l in r.render(80, 30, 1000) if R.bg("surface0") in l]   # the selection background

    def test_down_passes_through_the_duplicate(self):
        text = "\n".join([line("main", 1, "@1", "idle", "one"), line("main", 2, "@2", "needs-input", "two"),
                          line("main", 3, "@3", "running", "three"), line("main", 4, "@4", "idle", "four")])
        r = self.roster(text, ["@2"], "@1")
        self.assertEqual(r.sel_key, ("sess", "main", "@1"))
        r.sel_key = ("need", "@2")                           # top of the list
        seen = self.walk(r, 8)
        self.assertEqual(seen[:5], [("need", "@2"), ("sess", "main", "@1"), ("sess", "main", "@2"),
                                    ("sess", "main", "@3"), ("sess", "main", "@4")])
        self.assertEqual(set(seen[5:]), {("sess", "main", "@4")})   # stops at the bottom, no loop
        for _ in range(8):
            r.act("up")
        self.assertEqual(r.sel_key, ("need", "@2"))
        # Exactly one highlighted row, even on a window shown twice.
        for key in (("need", "@2"), ("sess", "main", "@2")):
            r.sel_key = key
            hl = self.highlighted(r)
            self.assertEqual(len(hl), 1, key)
            # The index column is the row's hotkey: NEEDS YOU row 1 (+ dim
            # session:index), the same window's group row 3.
            self.assertIn("1 main:2  two" if key[0] == "need" else " 3  two", self.strip(hl[0]))
            self.assertEqual(r.selected()["w"]["id"], "@2")

    def test_linked_window_in_two_groups(self):
        # list-windows -a reports a linked window once per session, same id.
        text = "\n".join([line("a", 1, "@1", "idle", "one", attached=9),
                          line("a", 2, "@5", "running", "shared", attached=9),
                          line("b", 1, "@5", "running", "shared", attached=1),
                          line("b", 2, "@6", "idle", "six", attached=1)])
        r = self.roster(text, [], "@1")
        seen = self.walk(r, 5)
        self.assertEqual(seen[:4], [("sess", "a", "@1"), ("sess", "a", "@5"), ("sess", "b", "@5"),
                                    ("sess", "b", "@6")])
        r.sel_key = ("sess", "b", "@5")
        self.assertEqual(len(self.highlighted(r)), 1)
        self.assertEqual(r.selected()["w"]["id"], "@5")

    def test_selection_survives_refresh_and_falls_back(self):
        text = "\n".join([line("main", 1, "@1", "idle", "one"), line("main", 2, "@2", "needs-input", "two")])
        r = self.roster(text, ["@2"], "@1")
        r.sel_key = ("need", "@2")
        r.rebuild()
        self.assertEqual(r.sel_key, ("need", "@2"))          # same key kept across a tick
        r.needs = []; r.rebuild()                            # discharged: same window's group row
        self.assertEqual(r.sel_key, ("sess", "main", "@2"))
        r.windows = R.parse_windows(line("main", 1, "@1", "idle", "one")); r.rebuild()
        self.assertEqual(r.sel_key, ("sess", "main", "@1"))  # gone: the current window's row
        r.query = "one"; r.rebuild()
        self.assertEqual(r.sel_key, ("hit", "main", "@1"))
        r.query = ""; r.rebuild()
        self.assertEqual(r.sel_key, ("sess", "main", "@1"))


class HotkeyTests(unittest.TestCase):
    """Number keys: one unique label per window row, acted on as displayed."""
    strip = staticmethod(lambda s: R.re.sub(r"\x1b\[[0-9;]*m", "", s))

    def setUp(self):
        self.calls = []
        self.saved = (R.run_bg, R.subprocess.run, R.tmux)
        R.run_bg = lambda cmd: self.calls.append(("bg", cmd))
        R.tmux = lambda *a: None

        def fake_run(argv, **kw):
            self.calls.append(("run", argv))
            return None
        R.subprocess.run = fake_run

    def tearDown(self):
        R.run_bg, R.subprocess.run, R.tmux = self.saved

    def roster(self, text, needs, cur, rows=40):
        r = R.Roster("/dev/ttys999")
        r.windows, r.needs, r.cur_win = R.parse_windows(text), needs, cur
        r.rebuild()
        r.render(100, rows, 1000)                 # what the user sees: the numbers come from this frame
        return r

    def gotos(self):
        return [c[1][c[1].index("goto") + 2] for c in self.calls if c[0] == "run" and "goto" in c[1]]

    def many(self, n):
        return "\n".join(line("main", i, "@%d" % i, "idle", "w%d" % i) for i in range(1, n + 1))

    def test_labels_unique_and_prefix_free(self):
        self.assertEqual(R.hotkey_labels(3), ["1", "2", "3"])
        self.assertEqual(R.hotkey_labels(9), [str(i) for i in range(1, 10)])
        self.assertEqual(R.hotkey_labels(10), [str(i) for i in range(1, 10)] + ["01"])   # never a bare 0
        self.assertEqual(R.hotkey_labels(12)[8:], ["9", "01", "02", "03"])
        self.assertEqual(R.hotkey_labels(19)[9:11], ["001", "002"])
        self.assertEqual(R.hotkey_labels(19)[-1], "010")
        for n in range(0, 250):
            labels = R.hotkey_labels(n)
            self.assertEqual(len(labels), n)
            self.assertEqual(len(set(labels)), n, n)
            self.assertEqual(labels[:9], [str(i) for i in range(1, min(n, 9) + 1)], n)   # 1-9: always one key
            self.assertNotIn("0", labels, n)
            self.assertEqual(len({len(l) for l in labels[9:]}), min(1, max(0, n - 9)), n)   # 0-labels: one width
            for a in labels:
                for b in labels:
                    self.assertFalse(a != b and b.startswith(a), (n, a, b))

    def test_window_shown_twice_gets_two_numbers(self):
        text = "\n".join([line("main", 1, "@1", "idle", "one"), line("main", 2, "@2", "needs-input", "two")])
        r = self.roster(text, ["@2"], "@1")
        self.assertEqual({l: it["w"]["id"] for l, it in r.drawn.items()}, {"1": "@2", "2": "@1", "3": "@2"})
        frame = [self.strip(l) for l in r.render(100, 40, 1000)]
        self.assertTrue(any("1 main:2  two" in l for l in frame), frame)
        self.assertTrue(any(l.startswith(" ● 3  two") or " 3  two" in l for l in frame), frame)
        # the parked header carries no number; expanded parked rows do
        r = self.roster(WINDOWS, ["@4"], "@1")
        r.parked_open = True; r.rebuild(); r.render(100, 40, 1000)
        parked = [l for l, it in r.drawn.items() if it["w"]["session"] == "stash"]
        self.assertEqual(len(parked), 3)
        self.assertEqual(len(r.drawn), len(set(r.drawn)))

    def test_single_digit_goes_at_once(self):
        r = self.roster(self.many(4), [], "@1")
        self.assertTrue(r.handle(["3"]))          # closes the popup
        self.assertEqual(self.gotos(), ["@3"])

    def test_digit_on_parked_row_unstashes(self):
        r = self.roster(WINDOWS, [], "@1")
        r.parked_open = True; r.rebuild(); r.render(100, 40, 1000)
        label = next(l for l, it in r.drawn.items() if it["w"]["id"] == "@9")
        self.assertTrue(r.handle(list(label)))
        self.assertEqual(self.calls, [("bg", "'%s' unstash '@9' '/dev/ttys999'" % R.STASH)])

    def test_past_ten_rows(self):
        r = self.roster(self.many(12), [], "@1")
        self.assertTrue(r.handle(["1"]))          # 1 still acts on the first key
        self.assertEqual(self.gotos(), ["@1"])
        self.calls.clear()
        self.assertFalse(r.handle(["0"]))         # 0 waits for the second digit, no timer
        self.assertEqual((self.calls, r.digits), ([], "0"))
        foot = self.strip(r.render(100, 40, 1000)[-1])
        self.assertIn("0", foot); self.assertIn("next digit", foot)
        self.assertTrue(r.handle(["2"]))
        self.assertEqual(self.gotos(), ["@11"])   # "02": the 11th row
        # a paste of both digits in one read works too
        self.calls.clear()
        r = self.roster(self.many(12), [], "@1")
        self.assertTrue(r.handle(["0", "3"]))
        self.assertEqual(self.gotos(), ["@12"])
        # exactly ten rows: the tenth is 01, a bare 0 is never complete
        self.calls.clear()
        r = self.roster(self.many(10), [], "@1")
        self.assertFalse(r.handle(["0"]))
        self.assertEqual(self.calls, [])
        self.assertTrue(r.handle(["1"]))
        self.assertEqual(self.gotos(), ["@10"])

    def test_half_typed_number_cancels(self):
        r = self.roster(self.many(12), [], "@1")
        r.handle(["0"])
        self.assertFalse(r.handle(["esc"]))       # esc drops the digit, the popup stays
        self.assertEqual((r.digits, self.calls), ("", []))
        r.handle(["0"]); r.handle(["bs"])
        self.assertEqual(r.digits, "")
        r.handle(["0"]); r.handle(["9"])          # "09" is no row (only 01-03)
        self.assertEqual(self.calls, [])
        self.assertIn("no row 09", r.msg)
        sel = r.sel_key
        r.handle(["j"])                           # another key ends the swallowing and still moves
        self.assertNotEqual(r.sel_key, sel)
        r.handle(["0"]); r.handle(["j"])          # ... and ends a half-typed number the same way
        self.assertEqual(r.digits, "")

    def test_dead_number_swallows_its_tail(self):
        """Probe: 19 rows, row 14 reads `005`; a row discharges and the frame
        redraws with 18 rows (`01`-`09`) before the user types 0 0 5. `00`
        matches nothing; the `5` must NOT then jump to row 5."""
        nineteen = self.many(19)
        r = self.roster(nineteen, [], "@1")
        self.assertEqual(r.drawn["005"]["w"]["id"], "@14")
        r.windows = R.parse_windows("\n".join(l for l in nineteen.splitlines() if "\x1f@3\x1f" not in l))
        r.rebuild(); r.render(100, 40, 1000)     # the new frame: 18 rows
        self.assertNotIn("005", r.drawn)
        self.assertFalse(r.handle(["0", "0", "5"]))
        self.assertEqual(self.calls, [])
        self.assertIn("no row 00", r.msg)
        self.assertFalse(r.handle(["5"]))         # still swallowed, in a later read too
        self.assertEqual(self.calls, [])
        r.handle(["esc"])                         # esc ends it without closing the popup
        self.assertTrue(r.handle(["5"]))          # a fresh 5 is a jump again
        self.assertEqual(self.gotos(), ["@6"])    # row 5 of the 18 (@3 gone)

    def test_ten_rows_after_eleven(self):
        """Probe: 11 rows (`01`, `02`), one discharges → 10 rows. A stale `01`
        stays a two-key label: the `0` alone does nothing, so the `1` cannot
        fall through into the agent pane a jump on `0` would have focused."""
        eleven = self.many(11)
        r = self.roster(eleven, [], "@1")
        self.assertEqual(sorted(l for l in r.drawn if l.startswith("0")), ["01", "02"])
        r.windows = R.parse_windows("\n".join(l for l in eleven.splitlines() if "\x1f@2\x1f" not in l))
        r.rebuild(); r.render(100, 40, 1000)
        self.assertEqual(sorted(l for l in r.drawn if l.startswith("0")), ["01"])
        self.assertFalse(r.handle(["0"]))
        self.assertEqual(self.calls, [])
        self.assertTrue(r.handle(["1"]))          # both keys consumed by the popup, as one label
        self.assertEqual(self.gotos(), ["@11"])

    def test_sequence_resolves_against_the_first_keys_frame(self):
        # 0 typed on the 19-row frame; a refresh redraws 18 rows mid-number.
        nineteen = self.many(19)
        r = self.roster(nineteen, [], "@1")
        r.handle(["0"])
        r.windows = R.parse_windows("\n".join(l for l in nineteen.splitlines() if "\x1f@3\x1f" not in l))
        r.rebuild(); r.render(100, 40, 1000)
        self.assertTrue(r.handle(["0", "5"]))
        self.assertEqual(self.gotos(), ["@14"])   # what `005` said when the number was started

    def test_digits_type_in_the_filter(self):
        r = self.roster(self.many(12), [], "@1")
        r.handle(["/"]); r.handle(["1"]); r.handle(["1"])
        self.assertEqual((r.query, self.calls), ("11", []))
        r.handle(["enter"]); r.render(100, 40, 1000)  # filter kept: digits are hotkeys again
        self.assertEqual({l: it["w"]["id"] for l, it in r.drawn.items()}, {"1": "@11"})
        self.assertTrue(r.handle(["1"]))
        self.assertEqual(self.gotos(), ["@11"])

    def test_number_means_the_row_as_displayed(self):
        text = "\n".join([line("main", 1, "@1", "idle", "one"), line("main", 2, "@2", "idle", "two"),
                          line("main", 3, "@3", "needs-input", "three")])
        r = self.roster(text, [], "@1")           # drawn: 1=@1 2=@2 3=@3
        r.needs = ["@3"]; r.rebuild()             # a refresh renumbers (NEEDS YOU on top), not yet drawn
        self.assertTrue(r.handle(["1"]))
        self.assertEqual(self.gotos(), ["@1"])    # the row that SAID 1, not the new first row
        # A row whose window moved (parked) since it was drawn: refused.
        self.calls.clear()
        r = self.roster(text, [], "@1")
        r.windows = R.parse_windows(text.replace(US.join(["main", "2", "@2"]), US.join(["stash", "1", "@2"])))
        r.rebuild()                               # the refresh tick: fresh dicts, @2 now parked
        self.assertFalse(r.handle(["2"]))
        self.assertEqual(self.calls, [])
        self.assertIn("moved", r.msg)
        # A number not on screen (scrolled off) is not acted on.
        r = self.roster(self.many(30), [], "@1", rows=10)
        self.assertNotIn("9", r.drawn)
        self.assertFalse(r.handle(["9"]))
        self.assertEqual(self.calls, [])


class StripTests(unittest.TestCase):
    """--strip: clicks, focus hand-back, client resolution. All I/O stubbed."""
    strip = staticmethod(lambda s: R.re.sub(r"\x1b\[[0-9;]*m", "", s))
    TEXT = "\n".join([line("main", 1, "@1", "idle", "one", attached=5),
                      line("main", 2, "@2", "needs-input", "two", since="5 needs-input", attached=5),
                      line("stash", 1, "@7", "", "parked", stash_label="proj/Parked"),
                      US.join(["", R.CLIENT_TAG, "/dev/ttys004", "@1"])])

    def setUp(self):
        self.calls = []
        self.saved = (R.run_bg, R.subprocess.run, R.tmux, R.wezterm_panes)
        R.run_bg = lambda cmd: self.calls.append(("bg", cmd))
        R.tmux = lambda *a: None

        class Ok:
            returncode, stdout = 0, ""

        def fake_run(argv, **kw):
            self.calls.append(("run", list(argv)))
            return Ok
        R.subprocess.run = fake_run
        self.panes = [{"pane_id": 7, "tab_id": 1, "tty_name": "/dev/ttys030", "is_active": False},   # the strip
                      {"pane_id": 3, "tab_id": 1, "tty_name": "/dev/ttys004", "is_active": True},    # tmux
                      {"pane_id": 9, "tab_id": 2, "tty_name": "/dev/ttys005", "is_active": True}]
        R.wezterm_panes = lambda exe: (self.calls.append(("list", exe)), self.panes)[1]

    def tearDown(self):
        R.run_bg, R.subprocess.run, R.tmux, R.wezterm_panes = self.saved

    def make(self, **kw):
        s = R.Strip(wezterm="/x/wezterm", own="7", **kw)
        s.load(self.TEXT)
        self.lines = [self.strip(l) for l in s.render(34, 20, 1000)]
        return s

    def line_of(self, s, text):
        return next(i + 1 for i, l in enumerate(self.lines) if text in l)

    def test_mouse_keys(self):
        self.assertEqual(R.parse_keys("\x1b[<0;5;7M\x1b[<0;5;7m\x1b[<65;1;2M"),
                         (["mouse:0:5:7:M", "mouse:0:5:7:m", "mouse:65:1:2:M"], ""))
        self.assertEqual(R.parse_keys("\x1b[<0;5"), ([], "\x1b[<0;5"))     # cut off: carried

    def test_pick_tmux_pane(self):
        clients = {"/dev/ttys004": "@1", "/dev/ttys005": "@9"}
        self.assertEqual(R.pick_tmux_pane(self.panes, "7", clients), ("/dev/ttys004", "3"))
        self.assertEqual(R.pick_tmux_pane(self.panes, 7, {"/dev/ttys005": "@9"}), (None, None))   # other tab only
        self.assertEqual(R.pick_tmux_pane(self.panes, "42", clients), (None, None))   # not in the list
        self.assertEqual(R.pick_tmux_pane(None, "7", clients), (None, None))          # wezterm cli failed
        two = self.panes + [{"pane_id": 4, "tab_id": 1, "tty_name": "/dev/ttys006", "is_active": False}]
        clients["/dev/ttys006"] = "@2"
        self.assertEqual(R.pick_tmux_pane(two, "7", clients, hint="4"), ("/dev/ttys006", "4"))
        self.assertEqual(R.pick_tmux_pane(two, "7", clients), ("/dev/ttys004", "3"))  # the active one

    def test_resolves_client_once(self):
        s = self.make()
        self.assertEqual((s.client, s.tmux_pane, s.cur_win), ("/dev/ttys004", "3", "@1"))
        s.load(self.TEXT); s.load(self.TEXT)
        self.assertEqual(len([c for c in self.calls if c[0] == "list"]), 1)   # not per tick

    def test_no_client_line(self):
        self.panes = self.panes[:1]
        s = self.make()
        self.assertIsNone(s.client)
        self.assertEqual(self.lines[1].strip(), "no tmux client")
        s.load(self.TEXT)                                   # throttled: no second list within 5 s
        self.assertEqual(len([c for c in self.calls if c[0] == "list"]), 1)

    def test_click_hands_focus_back_then_goes(self):
        s = self.make()
        y = self.line_of(s, "two")                          # the NEEDS YOU row
        self.assertFalse(s.handle(["mouse:0:4:%d:M" % y, "mouse:0:4:%d:m" % y], b"\x1b[<0;4;%dM" % y))
        runs = [c[1] for c in self.calls if c[0] == "run"]
        self.assertEqual(runs[0], ["/x/wezterm", "cli", "activate-pane", "--pane-id", "3"])
        self.assertEqual(runs[1][2:], ["goto", "/dev/ttys004", "@2", "main"])
        self.assertEqual(len(runs), 2)                      # the release does nothing

    def test_click_parked_opens_the_popup(self):
        popens = []
        saved = R.subprocess.Popen
        R.subprocess.Popen = lambda argv, **kw: (popens.append((argv, kw)), self)[1]
        try:
            s = self.make()
            self.assertTrue(self.lines[-1].startswith(" ▸ parked 1"))     # anchored at the bottom
            s.handle(["mouse:0:2:%d:M" % len(self.lines)])
        finally:
            R.subprocess.Popen = saved
        self.assertEqual(popens[0][0], R.roster_popup_argv("/dev/ttys004"))
        self.assertTrue(popens[0][1]["start_new_session"])
        runs = [c[1] for c in self.calls if c[0] == "run"]
        self.assertEqual([r[2] for r in runs], ["activate-pane"])        # focus back first, no goto

    def test_click_on_blank_and_wheel(self):
        s = self.make()
        s.handle(["mouse:0:2:1:M"])                        # header line: focus back only
        self.assertEqual([c[1][2] for c in self.calls if c[0] == "run"], ["activate-pane"])
        self.calls.clear()
        s.handle(["mouse:65:2:5:M"])                       # wheel: no focus change, no move
        self.assertEqual(self.calls, [])

    def test_stray_keys_are_forwarded(self):
        sent = []
        run = R.subprocess.run
        R.subprocess.run = lambda argv, **kw: (sent.append(kw.get("input")), run(argv, **kw))[1]
        s = self.make()
        s.handle(["ctrl-s", "c"], b"\x13c\x1b[<0;1;1m")     # CMD+T while the strip had focus (+ a release)
        runs = [c[1] for c in self.calls if c[0] == "run"]
        self.assertEqual(runs[0], ["/x/wezterm", "cli", "send-text", "--pane-id", "3", "--no-paste"])
        self.assertEqual(sent[0], b"\x13c")                 # the keys, never the mouse report
        self.assertEqual(runs[1][2], "activate-pane")

    def test_cut_mouse_report_is_not_typing(self):
        # ESC_WAIT flushed in the middle of a report: neither half is forwarded.
        s = self.make()
        self.assertEqual(s.stray_bytes(b"\x1b[<0;5"), b"")
        self.assertEqual(s.stray_bytes(b";7M"), b"")
        self.assertEqual(s.stray_bytes(b"x\x1b[<0;5;7Mq\x1b[<"), b"xq")
        self.assertEqual(s.stray_bytes(b"0;1Mab"), b"ab")
        self.assertEqual(s.stray_bytes(b"\x1b"), b"\x1b")          # a real Esc still goes to tmux
        s.handle([], b"\x1b[<0;4;2")                              # cut: no send-text at all
        self.assertFalse(any("send-text" in c[1] for c in self.calls if c[0] == "run"))

    def test_failed_list_keeps_the_client(self):
        s = self.make()
        R.wezterm_panes = lambda exe: None                         # `wezterm cli list` failed once
        s.resolve()
        self.assertEqual((s.client, s.tmux_pane), ("/dev/ttys004", "3"))

    def test_focus_back_failure_is_said(self):
        s = self.make()

        class Fail:
            returncode, stdout = 1, ""
        R.subprocess.run = lambda argv, **kw: (self.calls.append(("run", list(argv))), Fail)[1]
        s.handle(["mouse:0:2:1:M"])
        acts = [c for c in self.calls if c[0] == "run" and "activate-pane" in c[1]]
        self.assertEqual(len(acts), 2)                             # tried, re-resolved, tried again
        self.assertIn("couldn't focus tmux", s.msg)

    def test_identical_frames_are_not_rewritten(self):
        class Out:
            def __init__(self):
                self.writes = []

            def write(self, s):
                self.writes.append(s)

            def flush(self):
                pass
        s, out = self.make(), Out()
        R.watcher_age = lambda: 1.0
        try:
            self.assertTrue(s.draw(out))
            self.assertFalse(s.draw(out))                          # nothing changed: no write, no repaint
            self.assertEqual(len(out.writes), 1)
            s.say("hello")
            self.assertTrue(s.draw(out))
            s.last_frame = None                                    # SIGWINCH
            self.assertTrue(s.draw(out))
        finally:
            R.watcher_age = SAVED_WATCHER_AGE

    def test_session_order_is_stable(self):
        # The popup puts the client's session first; the strip must not
        # reshuffle under the mouse when a click moves the client.
        text = "\n".join([line("zeta", 1, "@1", "idle", "z", attached=1),
                          line("alpha", 1, "@2", "idle", "a", attached=9)])
        for cur in ("@1", "@2"):
            s = R.Strip(client="/dev/ttys999")
            s.load(text + "\n" + US.join(["", R.CLIENT_TAG, "/dev/ttys999", cur]))
            self.assertEqual([it["name"] for it in s.items if it["kind"] == "sess"], ["alpha", "zeta"])
        p = R.Roster("/dev/ttys999")
        p.load(text + "\n" + US.join(["", R.CLIENT_TAG, "/dev/ttys999", "@1"]))
        self.assertEqual([it["name"] for it in p.items if it["kind"] == "sess"], ["zeta", "alpha"])

    def test_fixed_client_never_calls_wezterm_list(self):
        s = self.make(client="/dev/ttys004")
        self.assertEqual([c for c in self.calls if c[0] == "list"], [])
        self.assertIsNone(s.tmux_pane)
        s.handle(["mouse:0:4:%d:M" % self.line_of(s, "two")])
        runs = [c[1] for c in self.calls if c[0] == "run"]
        self.assertEqual(len(runs), 1)                      # goto only: no pane to hand focus to
        self.assertIn("goto", runs[0])

    def test_lines_fit(self):
        labels = ["❤️❤️❤️❤️ love " * 6, "日本語のタイトル" * 6, "tab\there\x1bbad" * 6]
        text = "\n".join(line("a-very-long-session-name", i, "@%d" % (30 + i), "needs-input", lab,
                              workflow="1", since="1 x", attached=5) for i, lab in enumerate(labels))
        s = R.Strip(client="/dev/ttys999")
        s.load(text)
        s.needs = ["@30", "@31"]; s.rebuild()
        for cols in (12, 20, 34, 50):
            out = s.render(cols, 12, 1000)
            self.assertEqual(len(out), 12)
            for l in out:
                self.assertLessEqual(R.dwidth(self.strip(l)), cols - 1, (cols, l))


def strip_plain(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def fleet(n_agents, n_sessions=6, n_needs=None, now=100000, client_win=None, details=True):
    """A synthetic snapshot: n_agents spread round-robin over n_sessions,
    states cycling through every kind, the first n_needs of them needing you."""
    states = ["running", "idle", "idle", "running", "done", "idle", "needs-input", "failed"]
    rows = []
    for i in range(n_agents):
        s = "s%02d-%s" % (i % n_sessions, "x" * (i % 5))
        st = states[i % len(states)]
        if n_needs is not None:
            st = ("needs-input", "failed", "done")[i % 3] if i < n_needs else ("running", "idle")[i % 2]
        kw = {}
        if details:
            kw = {"kind": ("claude", "codex")[i % 2],
                  "detail_kind": {"needs-input": "perm", "failed": "fail", "done": "done", "running": "run"}.get(st, ""),
                  "detail": "detail text for agent %d, long enough to clip somewhere" % i}
        rows.append(line(s, i // n_sessions + 1, "@%d" % (i + 1), st, "proj%d/Agent title number %d" % (i, i),
                         workflow="1" if i % 11 == 5 else "", cua="1" if i % 13 == 3 else "",
                         since="%d x" % (now - 37 * i), active="1" if i < n_sessions else "0",
                         path="/nonexistent/%d" % i, **kw))
    rows.append(line("stash", 1, "@900", "needs-input", "parked/one"))
    rows.append(US.join(["", R.CLIENT_TAG, "/dev/ttys999", client_win or "@1"]))
    return "\n".join(rows)


class StripLadderTests(unittest.TestCase):
    """The strip never scrolls: every frame is exactly the pane, at every
    height, and whatever does not fit is counted, never silently dropped."""

    def setUp(self):
        self.saved = R.watcher_age
        R.watcher_age = lambda: 1.0

    def tearDown(self):
        R.watcher_age = self.saved

    def strip_for(self, text):
        s = R.Strip(client="/dev/ttys999")
        s.load(text)
        return s

    def check_frame(self, s, cols, rows, now=100000):
        out = s.render(cols, rows, now)
        ctx = (cols, rows, s.level)
        self.assertEqual(len(out), rows, ctx)
        self.assertEqual(len(s.targets), rows, ctx)
        plain = [strip_plain(l) for l in out]
        for l in plain:
            self.assertNotIn("\x1b", l, ctx)
            self.assertLessEqual(R.dwidth(l), cols - 1, ctx + (l,))
            if l and l[0] in "│├╭╰":                       # box lines run exactly to the right border
                self.assertEqual(R.dwidth(l), cols - 1, ctx + (l,))
                self.assertIn(l[-1], "│┤╮╯", ctx + (l,))
        return plain

    def accounted(self, s, plain):
        """Every NEEDS YOU window and every session is on screen or counted."""
        needs, groups, _ = s.strip_model(100000)
        acts = [a for row in s.targets for _, _, a in row]
        shown_needs = []
        in_needs = True
        for i, row in enumerate(s.targets):
            if plain[i].startswith("├") and "NEEDS YOU" not in plain[i] or plain[i].startswith("╰"):
                in_needs = False
            for _, _, a in row:
                if in_needs and a[0] == "goto":
                    shown_needs.append(a[1]["id"])
        more = [int(m.group(1)) for l in plain for m in [re.search(r"… (\d+) more ·", l)] if m]
        if needs and any("NEEDS YOU" in l for l in plain):
            self.assertEqual(len(dict.fromkeys(shown_needs)) + sum(more), len(needs), plain)
        sess = [a[1] for a in acts if a[0] == "session"]
        smore = [int(m.group(1)) for l in plain for m in [re.search(r"… (\d+) more sessions", l)] if m]
        self.assertEqual(len(sess) + sum(smore), len(groups), plain)
        self.assertEqual(sess, sorted(sess))           # name order

    def test_every_height_fits(self):
        for cols in (34, 40):
            for n in range(0, 41):
                s = self.strip_for(fleet(n))
                for rows in range(12, 81):
                    plain = self.check_frame(s, cols, rows)
                    self.accounted(s, plain)

    def test_odd_widths_fit(self):
        s = self.strip_for(fleet(40, n_needs=12))
        for cols in (20, 28, 37, 38, 60):
            for rows in (12, 24, 40, 74):
                self.check_frame(s, cols, rows)

    def test_tiny_heights_still_exact(self):
        s = self.strip_for(fleet(30, n_needs=9))
        for rows in range(1, 12):
            self.assertEqual(len(s.render(34, rows, 100000)), rows)

    def test_ladder_steps_in_order(self):
        # Heavy: 15 agents, 6 sessions, 5 needing you. Taller never shows less.
        s = self.strip_for(fleet(15, n_needs=5))
        order = ["rich", "full", "joined", "needs1", "fold"]
        seen = []
        last_lines = None
        for rows in range(80, 11, -1):
            plain = self.check_frame(s, 38, rows)
            seen.append(s.level)
            content = sum(1 for l in plain if l.strip())
            if last_lines is not None:
                self.assertLessEqual(content, last_lines + 0, rows)   # shrinking the pane never adds content
            last_lines = content
        steps = list(dict.fromkeys(seen))
        idx = [order.index(x) if x in order else len(order) for x in steps]
        self.assertEqual(idx, sorted(idx), steps)
        self.assertIn("cap", steps)

    def test_needs_detail_two_lines_then_one(self):
        s = self.strip_for(fleet(15, n_needs=5))
        tall = self.check_frame(s, 38, 74)
        self.assertIn(s.level, ("rich", "full"))
        i = next(i for i, l in enumerate(tall) if "NEEDS YOU" in l)
        self.assertRegex(tall[i + 2], r"│   (perm|fail|done)  detail text")
        self.assertRegex(tall[i + 2], r"s\d\d-x*:\d │$")              # where it is, right-aligned
        short = self.check_frame(s, 38, 24)
        self.assertNotIn("detail text", "".join(short))
        self.assertTrue(any(re.search(r"(perm|fail|done) +\d+[smhd] │", l) for l in short), short)

    def test_overflow_says_how_many(self):
        s = self.strip_for(fleet(40, n_needs=30))
        plain = self.check_frame(s, 34, 14)
        self.assertEqual(s.level, "cap")
        self.assertTrue(any(re.search(r"… \d+ more · ☰ menu", l) for l in plain), plain)
        self.assertTrue(any(re.search(r"… \d+ more sessions", l) for l in plain), plain)
        self.accounted(s, plain)

    def test_nothing_running(self):
        s = self.strip_for(US.join(["", R.CLIENT_TAG, "/dev/ttys999", "@1"]))
        plain = self.check_frame(s, 34, 20)
        self.assertIn("no agents running", "".join(plain))
        self.assertIn("no agents", plain[0])


class StripLayoutTests(unittest.TestCase):
    """Ordering, shapes, rollups and click targets of the strip's frame."""
    TEXT = "\n".join([
        line("zeta", 1, "@1", "idle", "z/idle one", since="900 idle", active="1"),
        line("zeta", 2, "@2", "running", "z/working", since="950 running", kind="codex"),
        line("zeta", 3, "@3", "needs-input", "z/asking", since="980 needs-input", kind="claude",
             detail_kind="ask", detail="Which deck should I use?"),
        line("alpha", 4, "@4", "idle", "a/four", since="10 idle"),
        line("alpha", 2, "@5", "done", "a/done", since="20 done", active="1", detail_kind="done",
             detail="7 fixes applied"),
        line("alpha", 1, "@6", "failed", "a/failed", since="30 failed", detail_kind="fail", detail="529 overloaded"),
        line("alpha", 3, "@7", "running", "a/flow", since="40 running", workflow="1"),
        line("alpha", 5, "@8", "", "", name="zsh"),                         # plain shell: not shown
        line("tasks", 1, "@9", "running", "broker"),                        # hidden session
        line("stash", 1, "@10", "", "", stash_label="p/parked"),
        US.join(["", R.CLIENT_TAG, "/dev/ttys999", "@2"])])

    def setUp(self):
        self.calls = []
        self.saved = (R.run_bg, R.subprocess.run, R.subprocess.Popen, R.tmux, R.watcher_age)
        R.run_bg = lambda cmd: self.calls.append(("bg", cmd))
        R.watcher_age = lambda: 1.0

        class Snap:                     # the refresh after a move: the same snapshot again
            returncode, stdout = 0, self.TEXT
        R.tmux = lambda *a: Snap

        class Ok:
            returncode, stdout = 0, ""

            @staticmethod
            def poll():
                return 0
        R.subprocess.run = lambda argv, **kw: (self.calls.append(("run", list(argv))), Ok)[1]
        R.subprocess.Popen = lambda argv, **kw: (self.calls.append(("popen", list(argv))), Ok)[1]
        self.s = R.Strip(client="/dev/ttys999")
        self.s.load(self.TEXT)
        self.plain = [strip_plain(l) for l in self.s.render(38, 40, 1000)]

    def tearDown(self):
        R.run_bg, R.subprocess.run, R.subprocess.Popen, R.tmux, R.watcher_age = self.saved

    def at(self, text):
        return next(i + 1 for i, l in enumerate(self.plain) if text in l)

    def test_order(self):
        needs, groups, parked = self.s.strip_model(1000)
        self.assertEqual([w["id"] for w in needs], self.s.needs)            # needs_order, unchanged
        self.assertEqual([w["id"] for w in needs], ["@6", "@3", "@5"])
        self.assertEqual([g["name"] for g in groups], ["alpha", "zeta"])     # by name, tasks/stash never
        self.assertEqual([w["id"] for w in groups[0]["rows"]], ["@6", "@5", "@7", "@4"])   # rank, then index
        self.assertEqual([w["id"] for w in groups[1]["rows"]], ["@3", "@2", "@1"])
        self.assertEqual([w["id"] for w in parked], ["@10"])
        self.assertEqual(self.s.level, "rich")
        self.assertLess(self.at("NEEDS YOU"), self.at("╰"))
        self.assertLess(self.at("alpha"), self.at("zeta"))

    def test_shapes_and_rollups(self):
        self.assertIn("✕1 ◉1 ✓1 ◐2 ○2", self.plain[0])                     # global count bar
        self.assertRegex(self.plain[self.at("alpha ") - 1], r"╭ alpha ✕1 ✓1 ◐1 ○1 ─+╮")
        self.assertRegex(self.plain[self.at("zeta ") - 1], r"╭ zeta ◉1 ◐1 ○1 ─+╮")
        self.assertRegex(self.plain[self.at("a/flow") - 1], r"│ ◐ a/flow +⚙ +\S+ │")
        self.assertRegex(self.plain[self.at("z/working") - 1], r"│ ◐ ⬢ z/working")       # kind glyph
        self.assertRegex(self.plain[self.at("z/asking") - 1], r"│ ◉ ✳ z/asking")
        self.assertRegex(self.plain[self.at("z/asking")], r"^│   asks  Which deck should… +zeta:3 │$")
        self.assertIn("│   fail  529 overloaded", self.plain[self.at("a/failed")])

    def test_shape_colour_is_dots_colour(self):
        # The palette is checked on dot()/glyph(); shape() must only swap the glyph.
        for w in self.s.windows:
            d, sh = self.s.dot(w, True), self.s.shape(w, True)
            if R.cat(w):
                self.assertEqual(sh[:-1], d[:-1], w["id"])
                self.assertEqual(sh[-1], R.CAT_GLYPH[R.cat(w)])
        r = R.Roster("/dev/ttys999")
        for st, c in (("failed", "failed"), ("needs-input", "needs-input"), ("done", "done"), ("idle", "idle")):
            w = R.parse_windows(line("m", 1, "@50", st, "x"))[0]
            self.assertEqual(r.dot(w, True)[:-1], R.fg(R.CAT_HUE[c]))
        w = R.parse_windows(line("m", 1, "@50", "running", "x"))[0]
        self.assertEqual(r.dot(w, True)[:-1], R.fg(R.CAT_HUE["working"]))     # the pulse's pink half

    def test_current_window_row_has_a_background(self):
        raw = self.s.render(38, 40, 1000)
        rows = [l for l in raw if "z/working" in strip_plain(l)]
        self.assertTrue(rows and all(R.bg("surface0") in l for l in rows))
        self.assertFalse(any(R.bg("surface0") in l for l in raw if "a/four" in strip_plain(l)))

    def actions(self):
        return {i + 1: [a for _, _, a in row] for i, row in enumerate(self.s.targets)}

    def test_every_clickable_line_maps(self):
        acts = self.actions()
        for y, l in enumerate(self.plain, 1):
            if not l.strip():
                self.assertEqual(acts[y], [], y)
            kinds = [a[0] for a in acts[y]]
            if "NEEDS YOU" in l:
                self.assertEqual(kinds, ["next"])
            elif l.startswith(("╭", "├")):
                self.assertEqual(acts[y], [("session", l.split()[1])], l)
            elif l.startswith("│"):
                self.assertEqual(kinds, ["goto"], l)
                w = acts[y][0][1]
                title = w["label"].split("/", 1)[1]
                if not l.startswith("│   "):                         # a NEEDS YOU second line names no title
                    self.assertIn(title, l)
            elif "parked" in l:
                self.assertEqual(kinds, ["menu"])
        # the toolbar: two targets on one line
        self.assertEqual(self.s.target(2, 2), ("next",))
        self.assertEqual(self.s.target(11, 2), ("menu",))
        self.assertIsNone(self.s.target(3, 1))                     # the count bar does nothing
        # both lines of a NEEDS YOU entry go to it
        y = self.at("z/asking")
        self.assertEqual(self.s.target(5, y)[1]["id"], "@3")
        self.assertEqual(self.s.target(5, y + 1)[1]["id"], "@3")

    def click(self, x, y):
        self.calls.clear()
        self.s.handle(["mouse:0:%d:%d:M" % (x, y)], b"\x1b[<0;%d;%dM" % (x, y))

    def test_clicks_run_the_right_thing(self):
        self.click(5, self.at("a/four"))
        self.assertEqual(self.calls[0][1][2:], ["goto", "/dev/ttys999", "@4", "alpha"])
        self.click(5, self.at("alpha "))                          # header: that session's current window
        self.assertEqual(self.calls[0][1][2:], ["goto", "/dev/ttys999", "@5", "alpha"])
        self.click(3, 2)                                           # ⏵ next
        self.assertEqual(self.calls[0][1][2:], ["next", "/dev/ttys999"])
        self.click(12, 2)                                          # ☰ menu
        self.assertEqual(self.calls, [("popen", R.roster_popup_argv("/dev/ttys999"))])
        self.click(5, self.at("NEEDS YOU"))
        self.assertEqual(self.calls[0][1][2:], ["next", "/dev/ttys999"])
        self.click(5, len(self.plain))                             # parked
        self.assertEqual(self.calls[0][0], "popen")
        self.click(5, len(self.plain) - 1)                         # the blank gap: nothing
        self.assertEqual(self.calls, [])

    def test_linked_window_goes_to_the_session_clicked(self):
        # @20 is linked into alpha AND zeta: each box's row and header name its own session.
        text = "\n".join([line("alpha", 1, "@20", "running", "l/linked", active="1"),
                          line("zeta", 7, "@20", "running", "l/linked", active="1"),
                          US.join(["", R.CLIENT_TAG, "/dev/ttys999", "@20"])])
        R.tmux = lambda *a: type("Snap", (), {"returncode": 0, "stdout": text})
        self.s.load(text)
        self.plain = [strip_plain(l) for l in self.s.render(38, 30, 1000)]
        rows = [i + 1 for i, l in enumerate(self.plain) if "l/linked" in l]
        self.click(5, rows[1])                                      # the row in zeta's box
        self.assertEqual(self.calls[0][1][2:], ["goto", "/dev/ttys999", "@20", "zeta"])
        self.click(5, rows[0])
        self.assertEqual(self.calls[0][1][2:], ["goto", "/dev/ttys999", "@20", "alpha"])
        self.click(5, self.at("zeta "))                             # zeta's header
        self.assertEqual(self.calls[0][1][2:], ["goto", "/dev/ttys999", "@20", "zeta"])

    def test_watcher_restart_is_clickable(self):
        R.watcher_age = lambda: None
        plain = [strip_plain(l) for l in self.s.render(38, 40, 1000)]
        self.assertIn("watcher off", plain[1])
        self.click(3, 2)
        self.assertEqual(self.calls, [("bg", "bash '%s'" % R.WATCHER)])

    def test_menu_is_the_prefix_q_binding(self):
        cmd = next(l for l in TMUX_CONF.read_text().splitlines() if l.startswith("bind-key q "))
        m = re.search(r"display-popup (.*) /bin/dash -c '(.*)' '\{\{ \.chezmoi\.homeDir \}\}/\.config/tmux/scripts/"
                      r"agent-roster\.py' '#\{client_tty\}'\"$", cmd)
        self.assertIsNotNone(m, cmd)
        flags, script = m.group(1), m.group(2).replace('\\"', '"').replace("\\$", "$")
        argv = R.roster_popup_argv("/dev/ttysX", prefix="/opt/homebrew")
        self.assertEqual(argv[:4], ["tmux", "display-popup", "-c", "/dev/ttysX"])
        self.assertEqual(" ".join(argv[4:11]), flags.replace("-c '#{client_tty}' ", "").replace("' agents '", " agents "))
        self.assertEqual(argv[11:13], ["/bin/dash", "-c"])
        self.assertEqual(argv[13], script.replace("{{ .homebrew_prefix }}", "/opt/homebrew"))
        self.assertEqual(argv[14:], [os.path.join(R.SCRIPTS, "agent-roster.py"), "/dev/ttysX"])

    def test_fold_and_collapse(self):
        text = "\n".join([line("big", i, "@%d" % i, "idle", "p/idle %d" % i, active="1" if i == 1 else "0")
                          for i in range(1, 11)] + [US.join(["", R.CLIENT_TAG, "/dev/ttys999", "@1"])])
        s = R.Strip(client="/dev/ttys999")
        s.load(text)
        plain = [strip_plain(l) for l in s.render(34, 12, 1000)]
        self.assertEqual(s.level, "fold")
        self.assertTrue(any("○○○○○○ 9 idle" in l for l in plain), plain)   # the current window stays a row
        self.assertEqual(s.target(5, next(i for i, l in enumerate(plain, 1) if "9 idle" in l)), ("menu",))
        self.assertTrue(any("p/idle 1" in l for l in plain))


class BranchTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = Path(tempfile.mkdtemp(prefix="roster-git-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def repo(self, name, head):
        g = self.d / name / ".git"
        g.mkdir(parents=True)
        (g / "HEAD").write_text(head)
        return self.d / name

    def test_branch_from_a_subdirectory(self):
        r = self.repo("main", "ref: refs/heads/feat/strip\n")
        (r / "a/b").mkdir(parents=True)
        self.assertEqual(R.git_head(str(r / "a/b")), "feat/strip")

    def test_worktree_git_file(self):
        r = self.repo("main", "ref: refs/heads/main\n")
        wt_git = r / ".git/worktrees/wt"
        wt_git.mkdir(parents=True)
        (wt_git / "HEAD").write_text("ref: refs/heads/wt-branch\n")
        wt = self.d / "wt"
        (wt / "sub").mkdir(parents=True)
        (wt / ".git").write_text("gitdir: ../main/.git/worktrees/wt\n")            # relative
        self.assertEqual(R.git_head(str(wt / "sub")), "wt-branch")
        (wt / ".git").write_text("gitdir: %s\n" % wt_git)                           # absolute
        self.assertEqual(R.git_head(str(wt)), "wt-branch")

    def test_detached_and_none(self):
        r = self.repo("det", "0123456789abcdef0123456789abcdef01234567\n")
        self.assertEqual(R.git_head(str(r)), "0123456")
        (self.d / "plain").mkdir()
        self.assertEqual(R.git_head(str(self.d / "plain")) in ("", R.git_head(str(self.d))), True)
        self.assertEqual(R.git_head(""), "")
        self.assertEqual(R.git_head("relative/path"), "")
        bad = self.d / "bad"
        bad.mkdir()
        (bad / ".git").write_text("not a gitdir line\n")
        self.assertEqual(R.git_head(str(bad)), "")

    def test_cached_per_cwd_for_30s(self):
        r = self.repo("c", "ref: refs/heads/one\n")
        s = R.Strip(client="/dev/ttys999")
        self.assertEqual(s.branch(str(r), 1000), "one")
        (r / ".git/HEAD").write_text("ref: refs/heads/two\n")
        self.assertEqual(s.branch(str(r), 1029), "one")                # cached
        self.assertEqual(s.branch(str(r), 1030), "two")                # 30 s on: read again

    def test_fifo_head_never_blocks(self):
        import threading
        r = self.repo("fifo", "")
        (r / ".git/HEAD").unlink()
        os.mkfifo(str(r / ".git/HEAD"))                                  # open() on it would block forever
        wt = self.d / "wt"
        wt.mkdir()
        os.mkfifo(str(wt / ".git"))                                      # a FIFO .git is not a gitdir file either
        out = []
        t = threading.Thread(target=lambda: out.extend([R.git_head(str(r)), R.git_head(str(wt))]), daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "git_head blocked on a FIFO")
        self.assertEqual(out, ["", ""])

    def test_remote_mounts_are_never_read(self):
        reads = []
        saved = R.git_head
        R.git_head = lambda cwd: (reads.append(cwd), "x")[1]
        try:
            s = R.Strip(client="/dev/ttys999")
            for p in ("/Volumes/share/repo", "/Network/Servers/x", "/net/host/x", "/Volumes"):
                self.assertEqual(s.branch(p, 1000), "", p)
        finally:
            R.git_head = saved
        self.assertEqual(reads, [])
        self.assertEqual(R.git_head("/Volumes/share/repo"), "")         # git_head refuses them itself too

    def test_slow_read_never_stalls_a_frame(self):
        import threading
        import time
        gate, reads = threading.Event(), []
        saved = R.git_head

        def slow(cwd):
            reads.append(cwd)
            gate.wait(5)
            return "late"
        R.git_head = slow
        try:
            s = R.Strip(client="/dev/ttys999")
            t0 = time.time()
            self.assertEqual(s.branch("/slow/disk", 1000), "")
            self.assertEqual(s.branch("/slow/disk", 1001), "")          # still reading: no second read
            self.assertLess(time.time() - t0, 1.0)
            self.assertEqual(len(reads), 1)
            gate.set()
            s.branch_reads["/slow/disk"][0].join(5)
            self.assertEqual(s.branch("/slow/disk", 1002), "late")       # the late answer is used
            self.assertEqual(len(reads), 1)
        finally:
            gate.set()
            R.git_head = saved

    def test_cache_is_pruned(self):
        saved = R.git_head
        R.git_head = lambda cwd: "b"
        try:
            s = R.Strip(client="/dev/ttys999")
            for i in range(50):
                s.branch("/d/%d" % i, 1000)
            self.assertEqual(len(s.branches), 50)
            s.branch("/d/new", 1000 + R.BRANCH_TTL)                      # every old entry expired
            self.assertEqual(list(s.branches), ["/d/new"])
        finally:
            R.git_head = saved

    def test_branch_shows_in_the_session_header(self):
        r = self.repo("h", "ref: refs/heads/feature\n")
        text = "\n".join([line("proj", 1, "@1", "running", "p/t", active="1", path=str(r)),
                          US.join(["", R.CLIENT_TAG, "/dev/ttys999", "@1"])])
        s = R.Strip(client="/dev/ttys999")
        s.load(text)
        saved = R.watcher_age
        R.watcher_age = lambda: 1.0
        try:
            plain = [strip_plain(l) for l in s.render(38, 20, 1000)]
        finally:
            R.watcher_age = saved
        self.assertTrue(any(re.match(r"╭ proj ⎇ feature ◐1 ─+╮$", l) for l in plain), plain)


class FitLabelTests(unittest.TestCase):
    def test_fit(self):
        self.assertEqual(R.fit_label("proj/Title", 20), ("proj/", "Title"))
        self.assertEqual(R.fit_label("~/Lost suit jacket here", 15), ("~/", "Lost suit ja…"))
        self.assertEqual(R.fit_label("math_econ_sched/Prose extraction", 20), ("", "Prose extraction"))
        self.assertEqual(R.fit_label("no slash at all", 8), ("", "no slas…"))
        self.assertEqual(R.fit_label("/abs/path", 20), ("", "/abs/path"))
        self.assertEqual(R.fit_label("a\tb/c", 20), ("a b/", "c"))


class PeekTests(unittest.TestCase):
    """`p` in the popup: one capture-pane on demand, any key closes it."""

    def setUp(self):
        self.calls = []
        self.saved = (R.tmux, R.agent_pane, R.watcher_age)
        R.watcher_age = lambda: 1.0

        class Cap:
            returncode = 0
            stdout = "".join("line %d\n" % i for i in range(1, 31)) + "\n\n"
        R.tmux = lambda *a: (self.calls.append(a), Cap)[1]
        R.agent_pane = lambda win: (self.calls.append(("pane", win)), ("%42", None))[1]
        self.r = R.Roster("/dev/ttys999")
        self.r.windows = R.parse_windows("\n".join([
            line("main", 1, "@1", "idle", "one"),
            line("main", 2, "@2", "needs-input", "two", panes="2", detail_kind="perm", detail="Bash rm -rf build")]))
        self.r.needs, self.r.cur_win = ["@2"], "@1"
        self.r.rebuild()

    def tearDown(self):
        R.tmux, R.agent_pane, R.watcher_age = self.saved

    def test_peek_one_pane_window(self):
        self.assertFalse(self.r.handle(["p"]))
        self.assertEqual(self.calls, [("capture-pane", "-p", "-J", "-S", "-15", "-t", "@1")])
        self.assertEqual(self.r.peek["lines"], ["line %d" % i for i in range(16, 31)])   # trailing blanks gone
        frame = [strip_plain(l) for l in self.r.render(80, 30, 1000)]
        self.assertTrue(any(l.startswith("╭─ peek · main:1 one") for l in frame), frame)
        self.assertIn("│ line 30", "\n".join(frame))
        self.assertIn("any key closes the peek", frame[-1])
        self.calls.clear()
        self.assertFalse(self.r.handle(["enter"]))          # closes, and only closes
        self.assertIsNone(self.r.peek)
        self.assertEqual(self.calls, [])

    def test_peek_split_window_finds_the_agent(self):
        self.r.move(-10)                                     # top row: NEEDS YOU, @2 (two panes)
        self.r.handle(["p"])
        self.assertEqual(self.calls[0], ("pane", "@2"))
        self.assertEqual(self.calls[1][-2:], ("-t", "%42"))

    def test_peek_drops_the_rest_of_its_batch(self):
        self.assertFalse(self.r.handle(["p", "q"]))          # a fast "pq" must not close the popup
        self.assertIsNotNone(self.r.peek)
        self.assertFalse(self.r.handle(["q"]))               # q closes the peek, not the popup
        self.assertIsNone(self.r.peek)
        self.assertTrue(self.r.handle(["q"]))

    def test_capture_failure_is_said(self):
        R.tmux = lambda *a: None
        self.r.handle(["p"])
        self.assertIsNone(self.r.peek)
        self.assertIn("can't capture", self.r.msg)

    def test_needs_row_shows_detail(self):
        frame = [strip_plain(l) for l in self.r.render(120, 30, 1000)]
        row = next(l for l in frame if "main:2" in l)
        self.assertRegex(row, r"two  perm  Bash rm -rf build")
        narrow = [strip_plain(l) for l in self.r.render(40, 30, 1000)]
        self.assertFalse(any("perm" in l for l in narrow))   # no room: the title keeps it


FAKE_SLOW_TMUX = """#!/bin/sh
# list-windows: the first call answers frame A at once; every later call
# sleeps (a slow refresh) and answers frame B. Anything else is ignored.
case "$1" in
  list-windows)
    n=$(cat "$D/count" 2>/dev/null || echo 0); echo $((n + 1)) > "$D/count"
    if [ "$n" -ge 1 ]; then sleep 0.8; cat "$D/B"; else cat "$D/A"; fi ;;
esac
"""


class MainLoopTests(unittest.TestCase):
    """main() in a real pty: input typed while a refresh is in flight acts on
    the frame that was on screen, not on the one the refresh is about to draw."""

    def test_digit_during_refresh_uses_the_old_frame(self):
        import pty
        import select
        import shutil
        import tempfile
        import time
        d = Path(tempfile.mkdtemp(prefix="roster-main-"))
        try:
            (d / "bin").mkdir()
            scripts = d / "home/.config/tmux/scripts"
            scripts.mkdir(parents=True)
            (d / "bin/tmux").write_text(FAKE_SLOW_TMUX); (d / "bin/tmux").chmod(0o755)
            (scripts / "agent-jump.sh").write_text('echo "$@" >> "$D/jump.log"\n')
            client = US.join(["", R.CLIENT_TAG, "/dev/ttys999", "@1"])
            one = line("main", 1, "@1", "idle", "one")
            # A: 1=@1 2=@2. B: @2 failed, so NEEDS YOU puts it on top: 1=@2 2=@1 3=@2.
            (d / "A").write_text("\n".join([one, line("main", 2, "@2", "idle", "two"), client]) + "\n")
            (d / "B").write_text("\n".join([one, line("main", 2, "@2", "failed", "two", since="1 failed"),
                                            client]) + "\n")
            env = {"PATH": "%s:/usr/bin:/bin" % (d / "bin"), "HOME": str(d / "home"), "TMPDIR": str(d) + "/",
                   "D": str(d), "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"}
            pid, fd = pty.fork()
            if pid == 0:
                import fcntl
                import struct
                import termios
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))   # a real popup size
                os.execve(sys.executable, [sys.executable, "-S", str(SRC), "--client", "/dev/ttys999"], env)
            t0 = time.time()

            def drain(until):
                while time.time() < until:
                    r, _, _ = select.select([fd], [], [], 0.05)
                    if r:
                        try:
                            os.read(fd, 65536)
                        except OSError:
                            return
            drain(t0 + 1.4)                       # first frame (A: 1=@1, 2=@2); the refresh at ~1 s is now sleeping
            os.write(fd, b"1")                    # typed against frame A, during the slow refresh
            status = None
            while status is None and time.time() < t0 + 6:
                drain(time.time() + 0.2)
                done, st = os.waitpid(pid, os.WNOHANG)
                status = st if done else None
            if status is None:                    # never leave a roster running
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            os.close(fd)
            log = (d / "jump.log").read_text() if (d / "jump.log").exists() else ""
            self.assertEqual(log.split(), ["goto", "/dev/ttys999", "@1", "main"], log)   # frame B's row 1 is @2
            self.assertIsNotNone(status, "the popup did not close")
        finally:
            shutil.rmtree(d, ignore_errors=True)


class WeztermBindingTests(unittest.TestCase):
    """CMD+B in wezterm.lua.tmpl launches the strip the way prefix q launches the popup."""

    def test_strip_binding(self):
        src = WEZTERM_CONF.read_text()
        self.assertIn('{ key = "b", mods = "CMD", action = toggle_strip }', src)
        self.assertIn("[ -x {{ .homebrew_prefix }}/bin/python3 ] && exec {{ .homebrew_prefix }}/bin/python3 -I -S ", src)
        self.assertIn("exec /usr/bin/python3 -S ", src)     # never -I/-E on the xcrun stub
        self.assertIn('"/bin/dash", "-c", STRIP_PY, STRIP_SCRIPT, "--strip"', src)
        self.assertIn("top_level = true", src)
        self.assertIn('get_user_vars().agent_strip == "1"', src)
        self.assertIn('"%s"' % R.STRIP_TITLE, src)
        self.assertEqual(R.STRIP_VAR, "agent_strip")


class WidthTests(unittest.TestCase):
    strip = staticmethod(lambda s: R.re.sub(r"\x1b\[[0-9;]*m", "", s))

    def test_widths(self):
        self.assertEqual(R.dwidth("abc"), 3)
        self.assertEqual(R.dwidth("日本"), 4)
        self.assertEqual(R.dwidth("❤️"), 2)                   # VS16: emoji presentation
        self.assertEqual(R.dwidth("❤"), 1)
        self.assertEqual(R.dwidth("🫠"), 2)                   # newer than this python's Unicode
        self.assertEqual(R.dwidth("é"), 1)              # combining mark
        self.assertEqual(R.clip("a❤️bcd", 3), "a…")            # never splits the VS16 off

    def test_every_line_fits(self):
        labels = ["❤️❤️❤️❤️ love " * 6, "🫠🫠🫠 melt " * 6, "日本語のタイトル" * 6, "tab\there\x1bbad" * 6]
        text = "\n".join(line("main", i, "@%d" % (30 + i), "running", lab, workflow="1", since="1 x", attached=5)
                         for i, lab in enumerate(labels))
        r = R.Roster("/dev/ttys999")
        r.windows = R.parse_windows(text)
        r.cur_win = "@30"
        r.needs = ["@31"]
        r.rebuild()
        for cols in (20, 41, 80, 133):
            r.msg, r.msg_until = "🫠 " * 80, 1e12
            lines = r.render(cols, 12, 1000)
            self.assertEqual(len(lines), 12)
            for l in lines:
                plain = self.strip(l)
                self.assertNotIn("\x1b", plain)
                self.assertLessEqual(R.dwidth(plain), cols - 1, (cols, plain))
            r.confirm = {"action": "park", "w": r.windows[0]}
            foot = self.strip(r.render(cols, 12, 1000)[-1])
            self.assertTrue(foot.startswith(" park main:0"), foot)
            self.assertLessEqual(R.dwidth(foot), cols - 1)
            r.confirm = None


def ws(session, index, state="", since=None, workflow="", links=()):
    opts = {"@agent_state": state} if state else {}
    if since is not None:
        opts["@agent_since"] = since
    if workflow:
        opts["@agent_workflow"] = workflow
    w = W(session, index, **opts)
    if links:
        w["links"] = list(links)
    return w


# Every rule of agent-jump.sh's `list`, and the ties that need its exact sort.
NEEDS_FIXTURE = {
    # tiers, oldest stamp first within a tier
    "@1": ws("main", 1, "done", "300 done"),
    "@2": ws("main", 2, "needs-input", "200 needs-input"),
    "@3": ws("work", 1, "failed", "400 failed"),
    "@4": ws("work", 2, "done", "50 done", workflow="1"),       # fleet out: not queued
    "@5": ws("work", 3, "running", "10 running"),               # not queued
    "@6": ws("work", 4),                                        # plain shell
    "@7": ws("main", 3, "needs-input"),                         # no stamp: last in its tier
    "@8": ws("main", 4, "needs-input", ""),
    "@9": ws("main", 5, "needs-input", "abc"),
    "@10": ws("main", 6, "needs-input", "12a needs-input"),
    "@11": ws("main", 7, "needs-input", "  150 needs-input"),   # awk split skips leading blanks
    "@12": ws("main", 8, "needs-input", "0150\tneeds-input"),   # leading zero, tab separator
    "@13": ws("main", 9, "failed", "99999999999 failed"),       # past the no-stamp sentinel
    "@14": ws("main", 10, "done", "300 done", workflow="0"),    # any workflow value counts
    # excluded sessions, and agent-jump's substring match on " name "
    "@20": ws("agents", 1, "failed", "1 failed"),
    "@21": ws("tasks", 1, "failed", "1 failed"),
    "@22": ws("stash", 1, "failed", "1 failed"),
    "@23": ws("scratch", 1, "failed", "1 failed"),
    "@24": ws("btop-popup", 1, "failed", "1 failed"),
    "@25": ws("tasks stash", 1, "failed", "1 failed"),
    "@26": ws("stash2", 1, "failed", "1 failed"),               # not excluded
    # equal stamps: session name in the locale's collation, then index as a number
    "@30": ws("B", 1, "done", "500 done"),
    "@31": ws("a", 1, "done", "500 done"),
    "@32": ws("_x", 1, "done", "500 done"),
    "@33": ws("Ä", 1, "done", "500 done"),
    "@34": ws("aa", 1, "done", "500 done"),
    "@35": ws("a-b", 1, "done", "500 done"),
    "@36": ws("Z", 1, "done", "500 done"),
    "@37": ws("10", 1, "done", "500 done"),
    "@38": ws("9", 1, "done", "500 done"),
    "@39": ws("a", 10, "done", "500 done"),
    "@40": ws("a", 2, "done", "500 done"),
    # No collation weight under en_US.UTF-8 (emoji, Greek, CJK): wcscoll calls
    # these equal, so sort takes the SHORTER key, then the next key, then the
    # whole line (where the window id decides). Not code point order.
    "@60": ws("dev 🚀", 1, "done", "700 done"),
    "@61": ws("dev 🔥", 1, "done", "700 done"),
    "@62": ws("dev 🔥🔥", 1, "done", "700 done"),
    "@71": ws("Ω", 9, "needs-input"),                          # the reviewer's repro: no stamps
    "@72": ws("Ω", 7, "needs-input"),
    "@73": ws("日本", 4, "needs-input"),
    "@79": ws("ω", 1, "done", "800 done"),                     # Ω:1 vs ω:1: only the line differs
    "@80": ws("Ω", 1, "done", "800 done"),
    "@81": ws("Ж本", 1, "done", "900 done"),
    "@82": ws("ωΩ🚀Ж", 1, "done", "900 done"),
    # linked windows: queued once, from the first sorted row; excluded links never win
    "@50": ws("zz", 1, "failed", "5 failed", links=("main", "stash")),
    "@51": ws("stash", 2, "failed", "6 failed", links=("yy",)),
}


class NeedsOrderTests(unittest.TestCase):
    """needs_order() must give exactly agent-jump.sh `list`'s order: NEEDS YOU
    and prefix d agree only as long as these two do."""
    TTY = "/dev/ttys999"

    def setUp(self):
        self.f = FakeEnv(NEEDS_FIXTURE, clients=[{"tty": self.TTY, "session": "main", "window": "@2"}])
        self.saved = locale.setlocale(locale.LC_COLLATE)

    def tearDown(self):
        self.f.close()
        locale.setlocale(locale.LC_COLLATE, self.saved)
        R._XFRM = None

    def jump_list(self, loc):
        r = self.f.run("agent-jump.sh", "list", LC_ALL=loc)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [l.split("\t")[0] for l in r.stdout.splitlines() if l]

    def roster(self, loc):
        """The roster's own snapshot call, answered by the same fake tmux."""
        R.collation(loc)
        r = subprocess.run([str(self.f.bin / "tmux"), "list-windows", "-a", "-F", R.FMT, ";",
                            "list-clients", "-F", R.CLIENT_FMT],
                           env=self.f.env(), capture_output=True, text=True, timeout=30)
        roster = R.Roster(self.TTY)
        roster.load(r.stdout)
        return roster

    def test_same_order_as_agent_jump(self):
        for loc in ("en_US.UTF-8", "C"):
            want = self.jump_list(loc)
            self.assertGreater(len(want), 20, loc)
            self.assertEqual(self.roster(loc).needs, want, loc)

    def test_fixture_exercises_the_rules(self):
        got = self.roster("en_US.UTF-8").needs
        self.assertEqual(got[:3], ["@26", "@50", "@51"])   # failed, oldest first; linked rows once
        self.assertNotIn("@4", got); self.assertNotIn("@14", got)
        for wid in ("@20", "@21", "@22", "@23", "@24", "@25"):
            self.assertNotIn(wid, got)
        self.assertIn("@26", got)
        self.assertLess(got.index("@40"), got.index("@39"))  # a:2 before a:10
        self.assertLess(got.index("@11"), got.index("@12"))   # both 150: main:7 before main:8
        self.assertLess(got.index("@12"), got.index("@2"))    # "0150" is 150, before 200
        self.assertLess(got.index("@9"), got.index("@1"))     # no stamp: last of its tier, not of all

    def test_locale_changes_ties(self):
        # The reason for collation(): en_US puts `a` before `B`, C does not.
        en, c = self.roster("en_US.UTF-8").needs, self.roster("C").needs
        self.assertLess(en.index("@31"), en.index("@30"))
        self.assertLess(c.index("@30"), c.index("@31"))

    def test_weightless_names(self):
        # What /usr/bin/sort does here (and the parity test holds us to):
        # equal collation → shorter key first → next key → whole line.
        got = self.roster("en_US.UTF-8").needs

        def before(a, b):
            self.assertLess(got.index(a), got.index(b), (a, b, got))
        before("@72", "@71")                     # Ω:7 before Ω:9
        before("@71", "@73")                     # Ω (1 char) before 日本 (2), whatever the index
        before("@60", "@62"); before("@61", "@62")   # "dev 🔥🔥" is longer
        before("@60", "@61")                     # equal keys: the line, i.e. the id, decides
        before("@79", "@80")                     # ω:1 before Ω:1 by id, though Ω < ω in code points
        before("@81", "@82")                     # shorter, though ω < Ж in code points

    def test_exclude_matches_the_script(self):
        m = re.search(r'^EXCLUDE="([^"]*)"$', JUMP.read_text(), re.M)
        self.assertEqual(m.group(1), R.JUMP_EXCLUDE)

    def test_snapshot_parses_the_client(self):
        roster = self.roster("C")
        self.assertEqual(roster.cur_win, "@2")
        self.assertFalse(roster.gone)
        self.assertEqual(len([w for w in roster.windows if w["id"] == "@50"]), 3)   # one row per link


class RefreshTests(unittest.TestCase):
    """The first frame waits on exactly ONE tmux call, and on no bash."""

    def setUp(self):
        self.saved = (R.tmux, R.subprocess.run)
        self.calls = []
        text = "\n".join([line("main", 1, "@1", "failed", "one", since="5 failed"),
                          line("main", 2, "@2", "idle", "two"),
                          US.join(["", R.CLIENT_TAG, "/dev/ttys999", "@2"]),
                          US.join(["", R.CLIENT_TAG, "/dev/ttys001", "@1"])])

        class Done:
            returncode, stdout = 0, text
        R.tmux = lambda *a: (self.calls.append(a), Done)[1]
        R.subprocess.run = lambda *a, **k: self.fail("refresh forked %r" % (a,))

    def tearDown(self):
        R.tmux, R.subprocess.run = self.saved

    def test_one_call(self):
        r = R.Roster("/dev/ttys999")
        r.refresh()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][:4], ("list-windows", "-a", "-F", R.FMT))
        self.assertIn(";", self.calls[0])
        self.assertEqual((r.cur_win, r.needs, r.gone), ("@2", ["@1"], False))
        self.assertEqual(len(r.windows), 2)                      # client rows are not windows

    def test_client_gone(self):
        r = R.Roster("/dev/ttys555")
        r.refresh()
        self.assertTrue(r.gone)


class BindingTests(unittest.TestCase):
    """prefix q's launch path, as written in tmux.conf.tmpl."""

    def lines(self):
        return [l for l in TMUX_CONF.read_text().splitlines() if re.match(r"bind-key (q|C-q) ", l)]

    def test_fast_launch(self):
        ls = self.lines()
        self.assertEqual(len(ls), 2)
        self.assertEqual(ls[0].split(None, 2)[2], ls[1].split(None, 2)[2])   # q and C-q: same command
        cmd = ls[0]
        # No shell around display-popup, no tmux client process.
        self.assertIn('run-shell -C "display-popup ', cmd)
        self.assertNotIn("tmux display-popup", cmd)
        # Argv form (several args: exec'd, no zsh -c); dash only picks python.
        self.assertIn(" /bin/dash -c '", cmd)
        self.assertIn("{{ .homebrew_prefix }}/bin/python3 -I -S ", cmd)
        # The xcrun stub keeps its pycache prefix: -S, never -I or -E.
        self.assertRegex(cmd, r"exec /usr/bin/python3 -S \\\"\\\$0\\\"")
        self.assertNotIn("pyenv", cmd)
        self.assertIn("/.config/tmux/scripts/agent-roster.py' '#{client_tty}'", cmd)
        self.assertEqual(cmd.count("#{client_tty}"), 2)


if __name__ == "__main__":
    unittest.main()
