"""agent-roster.py's pure model: parsing, grouping, key decoding. No tmux, no terminal."""
import importlib.util
import os
from pathlib import Path
import unittest

SRC = Path(os.environ.get(
    "AGENT_ROSTER_SOURCE",
    str(Path.home() / ".local/share/chezmoi/dot_config/tmux/scripts/executable_agent-roster.py")))
spec = importlib.util.spec_from_file_location("agent_roster", SRC)
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

US = "\x1f"


def line(session, index, wid, state="", summary="", workflow="", cua="", since="", attached=0, name="zsh",
         stash_label="", stash_session="", stash_ts=""):
    return US.join([session, str(index), wid, name, state, summary, workflow, cua, since, str(attached), "1",
                    stash_label, stash_session, stash_ts])


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
        self.assertEqual(self.r.sel_key, "@1")             # default: the window you are on
        self.assertFalse(self.r.act("H"))
        self.assertEqual(self.calls, [])                    # nothing parked yet
        self.assertEqual(self.r.confirm["action"], "park")
        self.assertFalse(self.r.act("n"))
        self.assertEqual((self.calls, self.r.confirm), ([], None))
        self.r.act("H"); self.r.act("y")
        self.assertEqual(len(self.calls), 1)
        self.assertIn("stash '@1'", self.calls[0][1])

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
        self.assertFalse(self.r.act("g"))
        self.assertIn("failed", self.r.msg)

    def test_goto_closes_popup(self):
        self.assertTrue(self.r.act("enter"))
        self.assertEqual(self.calls[0][1][2:], ["goto", "/dev/ttys999", "@1"])


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


if __name__ == "__main__":
    unittest.main()
