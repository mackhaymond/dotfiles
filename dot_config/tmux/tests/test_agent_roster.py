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

    def test_keys(self):
        self.assertEqual(R.parse_keys("\x1b[A\x1b[B\x1b[Z\t\r j"), ["up", "down", "btab", "tab", "enter", "space", "j"])
        # SS3 and unknown CSI sequences are swallowed, never read as esc
        self.assertEqual(R.parse_keys("\x1bOF\x1b[1;5Cq"), ["q"])
        self.assertEqual(R.parse_keys("\x1b"), ["esc"])
        self.assertEqual(R.parse_keys("\x03"), ["ctrl-c"])

    def test_clip(self):
        self.assertEqual(R.clip("abcdef", 4), "abc…")
        self.assertEqual(R.clip_ansi("\x1b[1mabcdef", 3), "\x1b[1mabc")
        self.assertEqual(R.ago(None, 0), "")
        self.assertEqual((R.ago(0, 59), R.ago(0, 61), R.ago(0, 7300)), ("59s", "1m", "2h"))


if __name__ == "__main__":
    unittest.main()
