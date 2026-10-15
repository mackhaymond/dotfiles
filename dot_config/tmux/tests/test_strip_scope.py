"""The strip's scope (space | all), agent numbers, and the @spaces shell rule.

agent-roster.py --strip reads @spaces, @strip_scope and the client's session
from the client rows of its one tmux call (CLIENT_FMT). With @spaces off and
@strip_scope unset the strip must be exactly the classic one: TodayTests pins
that against frames rendered by the pre-scope script."""
import hashlib
import os
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_agent_roster as T  # noqa: E402  (R, fixtures; the module, so its TestCases are not re-collected)

R = T.R
US = T.US
line = T.line
plain = T.strip_plain


def client(tty="/dev/ttys999", win="@1", session="", spaces="", scope="", short=False):
    """A CLIENT_FMT row; short=True is the old 4-field form."""
    f = ["", R.CLIENT_TAG, tty, win]
    return US.join(f if short else f + [session, spaces, scope])


def with_client(text, **kw):
    """`text` with its client row(s) replaced by one built from kw."""
    rows = [l for l in text.splitlines() if not l.startswith(US + R.CLIENT_TAG + US)]
    return "\n".join(rows + [client(**kw)])


def strip(text, **kw):
    s = R.Strip(client="/dev/ttys999")
    s.load(with_client(text, **kw) if kw else text)
    return s


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = (R.watcher_age, R.tmux, R.subprocess.run)
        R.watcher_age = lambda: 1.0
        self.tmux_calls = []

        class Ok:
            returncode, stdout = 0, ""
        self.Ok = Ok
        R.tmux = lambda *a: (self.tmux_calls.append(a), Ok)[1]
        self.runs = []
        R.subprocess.run = lambda argv, **kw: (self.runs.append(list(argv)), Ok)[1]

    def tearDown(self):
        R.watcher_age, R.tmux, R.subprocess.run = self.saved

    def frame(self, s, cols=38, rows=40, now=1000):
        out = [plain(l) for l in s.render(cols, rows, now)]
        self.assertEqual(len(out), rows)
        for l in out:
            self.assertLessEqual(R.dwidth(l), cols - 1, (cols, rows, l))
            if l and l[0] in "│├╭╰":
                self.assertEqual(R.dwidth(l), cols - 1, (cols, rows, s.level, l))
                self.assertIn(l[-1], "│┤╮╯", (cols, rows, l))
        return out


LAYOUT = T.StripLayoutTests.TEXT          # alpha + zeta, client on @2 (zeta:2)

# The classic strip for LAYOUT at 38x24, rendered by the script before scopes existed.
TODAY_38x24 = [
    " AGENTS  ✕1 ◉1 ✓1 ◐2 ○2",
    " ⏵ next  ☰ menu",
    "╭ NEEDS YOU ✕1 ◉1 ✓1 ───────────────╮",
    "│ ✕ a/failed                    16m │",
    "│   fail  529 overloaded    alpha:1 │",
    "│ ◉ ✳ z/asking                  20s │",
    "│   asks  Which deck should… zeta:3 │",
    "│ ✓ a/done                      16m │",
    "│   done  7 fixes applied   alpha:2 │",
    "╰───────────────────────────────────╯",
    "╭ alpha ✕1 ✓1 ◐1 ○1 ────────────────╮",
    "│ ✕ a/failed                    16m │",
    "│ ✓ a/done                      16m │",
    "│ ◐ a/flow                    ⚙ 16m │",
    "│ ○ a/four                      16m │",
    "╰───────────────────────────────────╯",
    "╭ zeta ◉1 ◐1 ○1 ────────────────────╮",
    "│ ◉ ✳ z/asking                  20s │",
    "│ ◐ ⬢ z/working                 50s │",
    "│ ○ z/idle one                   1m │",
    "╰───────────────────────────────────╯",
    "",
    "",
    " ▸ parked 1 · ☰",
]
# sha256 of the raw ANSI frames of fleet(15, n_needs=5) at these sizes, joined by
# "\n" per frame, rendered by the pre-scope script (now=100000).
TODAY_FLEET_SIZES = ((34, 74), (38, 40), (38, 24), (40, 14))
TODAY_FLEET_SHA = "8efd71907e16b893d9e80a76055e91ad945235f9c1739dbeaa22c4b08ec2b9b4"


class TodayTests(Base):
    """@spaces off + @strip_scope unset: byte-identical to the classic strip."""

    def test_layout_frame_unchanged(self):
        for kw in ({}, {"win": "@2", "short": True}, {"win": "@2"}, {"win": "@2", "session": "zeta"},
                   {"win": "@2", "session": "zeta", "spaces": "off"}):
            s = strip(LAYOUT, **kw)
            self.assertEqual(s.scope(), "all", kw)
            self.assertFalse(s.scope_shown(), kw)
            self.assertEqual(self.frame(s, 38, 24), TODAY_38x24, kw)

    def test_fleet_frames_unchanged(self):
        for kw in ({}, {"win": "@1", "session": "s00-"}):
            h = hashlib.sha256()
            for cols, rows in TODAY_FLEET_SIZES:
                s = strip(T.fleet(15, n_needs=5), **kw)
                h.update("\n".join(s.render(cols, rows, 100000)).encode())
            self.assertEqual(h.hexdigest(), TODAY_FLEET_SHA, kw)

    def test_old_and_new_client_rows_render_alike(self):
        fx = T.fleet(30, n_needs=7)
        for cols in (20, 34, 40):
            for rows in range(8, 70, 7):
                a, b = strip(fx, short=True), strip(fx, session="s00-")
                self.assertEqual(a.render(cols, rows, 100000), b.render(cols, rows, 100000), (cols, rows))
                self.assertEqual(a.targets, b.targets)

    def test_header_does_nothing(self):
        s = strip(LAYOUT, win="@2", session="zeta")
        self.frame(s)
        self.assertIsNone(s.target(3, 1))                      # no scope toggle on the title


class ParseTests(Base):
    def test_client_fmt_carries_the_scope_inputs(self):
        self.assertEqual(R.CLIENT_FMT.split(US)[2:],
                         ["#{client_tty}", "#{window_id}", "#{session_name}", "#{@spaces}", "#{@strip_scope}"])
        text = "\n".join([client("/dev/a", "@3", "main", "on", "all"), client("/dev/b", "@4", short=True)])
        self.assertEqual(R.parse_client_rows(text), {
            "/dev/a": {"win": "@3", "session": "main", "spaces": "on", "scope": "all"},
            "/dev/b": {"win": "@4", "session": "", "spaces": "", "scope": ""}})
        self.assertEqual(R.parse_clients(text), {"/dev/a": "@3", "/dev/b": "@4"})

    def test_still_one_tmux_call(self):
        text = with_client(LAYOUT, win="@2", session="zeta", spaces="on", scope="")

        class Snap:
            returncode, stdout = 0, text
        calls = []
        R.tmux = lambda *a: (calls.append(a), Snap)[1]
        s = R.Strip(client="/dev/ttys999")
        s.refresh()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:4], ("list-windows", "-a", "-F", R.FMT))
        self.assertIn(R.CLIENT_FMT, calls[0])
        self.assertEqual((s.spaces, s.scope_opt, s.cur_sess, s.scope()), (True, "", "zeta", "space"))

    def test_options_from_another_client_row(self):
        # Our client is missing (just detached): the options are global, any row will do.
        s = R.Strip(client="/dev/ttys999")
        s.load(LAYOUT.replace("/dev/ttys999", "/dev/other").replace(
            "/dev/other" + US + "@2", "/dev/other" + US + "@2" + US + "zeta" + US + "on" + US + "all"))
        self.assertEqual((s.spaces, s.scope_opt, s.cur_sess, s.scope()), (True, "all", None, "all"))

    def test_scope_resolution(self):
        for spaces, opt, want in (("", "", "all"), ("on", "", "space"), ("off", "", "all"),
                                  ("on", "all", "all"), ("", "space", "space"), ("on", "bogus", "space"),
                                  ("", "bogus", "all"), ("1", "", "all"), ("yes", "", "all")):
            s = strip(LAYOUT, win="@2", session="zeta", spaces=spaces, scope=opt)
            self.assertEqual(s.scope(), want, (spaces, opt))


class SpaceScopeTests(Base):
    def test_only_the_current_session_and_global_needs(self):
        s = strip(LAYOUT, win="@2", session="zeta", spaces="on")
        needs, groups, parked = s.strip_model(1000)
        self.assertEqual([w["id"] for w in needs], ["@6", "@3", "@5"])          # alpha's too: global
        self.assertEqual([g["name"] for g in groups], ["zeta"])
        self.assertEqual([w["id"] for w in groups[0]["rows"]], ["@1", "@2", "@3"])   # index order
        self.assertEqual([w["id"] for w in parked], ["@10"])
        f = self.frame(s)
        self.assertEqual(f[0], " SPACE  ◉1 ◐1 ○1")                              # this space's counts
        self.assertFalse(any("alpha " in l and l.startswith("╭") for l in f))
        self.assertFalse(any("a/four" in l or "a/flow" in l for l in f))         # alpha's non-needs agents
        self.assertTrue(any("a/failed" in l for l in f))                        # ...but its NEEDS YOU entry
        box = f.index(next(l for l in f if l.startswith("╭ zeta")))
        self.assertRegex(f[box + 1], r"^│ 1 ○ z/idle one")
        self.assertRegex(f[box + 2], r"^│ 2 ◐ ⬢ z/working")
        self.assertRegex(f[box + 3], r"^│ 3 ◉ ✳ z/asking")
        self.assertTrue(f[box + 4].startswith("╰"))
        need_rows = f[f.index(next(l for l in f if "NEEDS YOU" in l)) + 1:box]
        self.assertFalse(any(re.match(r"│ \d ", l) for l in need_rows))         # numbers only in the space box

    def test_explicit_space_scope_without_spaces(self):
        s = strip(LAYOUT, win="@8", session="alpha", scope="space")            # @8: alpha's plain shell
        self.assertEqual(s.scope(), "space")
        _, groups, _ = s.strip_model(1000)
        self.assertEqual([g["name"] for g in groups], ["alpha"])
        self.assertIn("@8", [w["id"] for w in groups[0]["rows"]])               # @spaces off: classic shell rule
        self.assertEqual(s.ordinals, {"@6": "1", "@5": "2", "@7": "3", "@4": "4"})
        self.assertNotIn("@8", s.ordinals)

    def test_session_with_no_agents_shows_an_empty_box(self):
        text = LAYOUT + "\n" + line("empty", 1, "@30", "", "", active="1")
        s = strip(text, win="@30", session="empty", spaces="on")
        f = self.frame(s)
        box = f.index(next(l for l in f if l.startswith("╭ empty")))
        self.assertIn("no agents in this space", f[box + 1])
        self.assertTrue(f[box + 2].startswith("╰"))
        self.assertTrue(any("NEEDS YOU" in l for l in f))                       # needs still global
        self.assertEqual(f[0], " SPACE  no agents")

    def test_hidden_or_stash_session_falls_back_to_all(self):
        # A client sitting in `agents` (or the stash) is not in a space: the
        # strip shows every space, and title, counts and body agree on it.
        text = LAYOUT + "\n" + line("agents", 1, "@60", "running", "pty", active="1")
        classic = self.frame(strip(LAYOUT, win="@2", session="zeta", spaces="on", scope="all"))
        for sess, win, scope in (("agents", "@60", ""), ("agents", "@60", "space"), ("tasks", "@9", ""),
                                 ("stash", "@10", "")):
            s = strip(text, win=win, session=sess, spaces="on", scope=scope)
            self.assertEqual(s.chosen_scope(), "space", sess)
            self.assertEqual(s.scope(), "all", sess)
            _, groups, _ = s.strip_model(1000)
            self.assertEqual([g["name"] for g in groups], ["alpha", "zeta"], sess)
            self.assertIsNone(s.ordinals, sess)
            f = self.frame(s)
            self.assertEqual(f[0], " AGENTS  ✕1 ◉1 ✓1 ◐2 ○2", sess)   # global counts, hidden ones excluded
            self.assertEqual(f[0], classic[0], sess)
            self.assertFalse(any("pty" in l or "no agents in this space" in l for l in f), sess)
        # The toggle still flips what is chosen.
        s = strip(text, win="@60", session="agents", spaces="on")
        s.handle([], b"\x1bv")
        self.assertEqual(s.chosen_scope(), "all")
        self.assertEqual([a for a in self.tmux_calls if a[0] == "set-option"],
                         [("set-option", "-g", "@strip_scope", "all")])

    def test_unknown_session_falls_back_to_all(self):
        s = R.Strip(client="/dev/ttys999")
        s.load(with_client(LAYOUT, tty="/dev/other", win="@2", session="zeta", spaces="on"))
        self.assertIsNone(s.cur_sess)
        _, groups, _ = s.strip_model(1000)
        self.assertEqual([g["name"] for g in groups], ["alpha", "zeta"])
        self.assertIsNone(s.ordinals)

    def test_every_height_and_width_fits(self):
        fx = T.fleet(40, n_needs=9)
        for spaces, scope in (("on", ""), ("on", "all"), ("", "space")):
            s = strip(fx, win="@1", session="s00-", spaces=spaces, scope=scope)
            for cols in (20, 28, 34, 40):
                for rows in range(4, 60, 3):
                    self.frame(s, cols, rows, 100000)


class ShellTests(Base):
    """With @spaces on, a plain shell is never an agent row, in either scope."""

    def rows_of(self, s):
        _, groups, _ = s.strip_model(1000)
        return {g["name"]: [w["id"] for w in g["rows"]] for g in groups}

    def test_current_shell_hidden_with_spaces(self):
        for scope in ("all", "space"):
            s = strip(LAYOUT, win="@8", session="alpha", spaces="on", scope=scope)
            self.assertNotIn("@8", self.rows_of(s)["alpha"], scope)
            self.assertFalse(any("zsh" in l for l in self.frame(s)), scope)

    def test_current_shell_kept_without_spaces(self):
        s = strip(LAYOUT, win="@8", session="alpha")
        self.assertIn("@8", self.rows_of(s)["alpha"])

    def test_shell_only_session_gone_in_all_scope(self):
        text = LAYOUT + "\n" + line("plain", 1, "@40", "", "", active="1")
        self.assertIn("plain", self.rows_of(strip(text, win="@40", session="plain")))
        self.assertNotIn("plain", self.rows_of(strip(text, win="@40", session="plain", spaces="on", scope="all")))


class OrdinalTests(Base):
    TEXT = "\n".join([
        line("proj", 1, "@1", "", "", active="1"),                 # shell
        line("proj", 2, "@2", "idle", "p/two"),
        line("proj", 3, "@3", "", ""),                             # shell
        line("proj", 5, "@5", "failed", "p/five", since="900 failed"),
        line("proj", 4, "@4", "running", "p/four"),
        line("proj", 9, "@9", "", ""),                             # shell
        line("proj", 12, "@12", "needs-input", "p/twelve"),
        line("other", 1, "@50", "running", "o/one"),
    ])

    def test_position_among_agents_by_index(self):
        ws = R.parse_windows(self.TEXT)
        self.assertEqual(R.agent_ordinals(ws, "proj"), {"@2": "1", "@4": "2", "@5": "3", "@12": "4"})
        self.assertEqual(R.agent_ordinals(ws, "other"), {"@50": "1"})
        self.assertEqual(R.agent_ordinals(ws, "nope"), {})

    def test_rows_show_them(self):
        s = strip(self.TEXT, win="@1", session="proj", spaces="on")
        f = self.frame(s, 38, 30)
        got = [(m.group(1), m.group(2)) for l in f for m in [re.match(r"│ (\d) . (p/\w+)", l)] if m]
        self.assertEqual(got, [("1", "p/two"), ("2", "p/four"), ("3", "p/five"), ("4", "p/twelve")])

    def test_none_past_nine(self):
        text = "\n".join(line("big", i, "@%d" % i, "running", "b/agent %d" % i) for i in range(1, 12))
        s = strip(text, win="@1", session="big", spaces="on")
        f = self.frame(s, 38, 40)
        self.assertEqual(s.ordinals["@9"], "9")
        self.assertEqual((s.ordinals["@10"], s.ordinals["@11"]), ("", ""))
        self.assertTrue(any(l.startswith("│ 9 ◐ b/agent 9") for l in f), f)
        self.assertTrue(any(l.startswith("│   ◐ b/agent 10") for l in f), f)   # same column, no number

    def test_dropped_when_too_narrow(self):
        # The number only takes its 2 cells while the title keeps 4.
        s = strip(self.TEXT, win="@1", session="proj", spaces="on")
        w = next(x for x in s.windows if x["id"] == "@5")                # age " 16m": 4 fixed cells
        for width, shown in ((8, False), (11, False), (12, True), (30, True)):
            c, cw = s.agent_content(w, width, False, 1000, None, "3")
            self.assertEqual(plain(c).startswith("3 "), shown, width)
            self.assertEqual(cw, R.dwidth(plain(c)), width)
            if width >= 10:
                self.assertEqual(cw, width, width)
        self.assertTrue(any(re.match(r"│ \d ", l) for l in self.frame(s, 20, 30)))


class ToggleTests(Base):
    def make(self, **kw):
        s = R.Strip(client="/dev/ttys999", wezterm="/x/wezterm")
        s.tmux_pane = "3"
        s.load(with_client(LAYOUT, win="@2", session="zeta", **kw))
        self.lines = self.frame(s)
        return s

    def sets(self):
        return [a for a in self.tmux_calls if a[:1] == ("set-option",)]

    def test_option_v_flips_and_persists(self):
        s = self.make(spaces="on")
        self.assertEqual(s.scope(), "space")
        self.assertFalse(s.handle([], b"\x1bv"))
        self.assertEqual(self.sets(), [("set-option", "-g", "@strip_scope", "all")])
        self.assertEqual(s.scope(), "all")
        self.assertEqual([g["name"] for g in s.strip_model(1000)[1]], ["alpha", "zeta"])
        self.assertEqual(self.frame(s)[0].split()[0], "AGENTS")
        s.handle([], b"\x1bV")
        self.assertEqual(self.sets()[-1], ("set-option", "-g", "@strip_scope", "space"))
        self.assertEqual(s.scope(), "space")
        # The key is not forwarded to tmux, but focus still goes back.
        self.assertFalse(any("send-text" in r for r in self.runs))
        self.assertEqual([r[2] for r in self.runs], ["activate-pane", "activate-pane"])

    def test_works_with_spaces_off_too(self):
        s = self.make()
        s.handle([], b"\x1bv")
        self.assertEqual(self.sets(), [("set-option", "-g", "@strip_scope", "space")])
        self.assertEqual(s.scope(), "space")
        self.assertTrue(s.scope_shown())

    def test_other_typing_still_forwarded(self):
        sent = []
        R.subprocess.run = lambda argv, **kw: (self.runs.append(list(argv)), sent.append(kw.get("input")), self.Ok)[2]
        s = self.make(spaces="on")
        s.handle(["c"], b"c\x1bvd\x1b[v\x1bs")
        self.assertEqual(len(self.sets()), 1)
        # CSI v is not the toggle; Option-S is tmux's M-s (agent-jump next): forwarded.
        self.assertEqual(sent[0], b"cd\x1b[v\x1bs")

    def test_title_click_toggles(self):
        s = self.make(spaces="on")
        self.assertEqual(self.lines[0].split()[0], "SPACE")
        self.assertEqual(s.target(2, 1), ("scope",))
        self.assertIsNone(s.target(12, 1))                      # the counts do nothing
        s.handle(["mouse:0:3:1:M"], b"\x1b[<0;3;1M")
        self.assertEqual(self.sets(), [("set-option", "-g", "@strip_scope", "all")])
        self.assertEqual(self.frame(s)[0].split()[0], "AGENTS")
        self.assertEqual(s.target(2, 1), ("scope",))            # still a toggle: @strip_scope is set now

    def test_failed_set_keeps_the_scope(self):
        class Fail:
            returncode, stdout = 1, ""
        R.tmux = lambda *a: Fail
        s = self.make(spaces="on")
        s.handle([], b"\x1bv")
        self.assertEqual(s.scope(), "space")
        self.assertIn("couldn't set @strip_scope", s.msg)

    def test_next_frame_reads_the_option_back(self):
        s = self.make(spaces="on", scope="all")
        self.assertEqual(s.scope(), "all")
        s.load(with_client(LAYOUT, win="@2", session="zeta", spaces="on", scope="space"))   # set elsewhere
        self.assertEqual(s.scope(), "space")


if __name__ == "__main__":
    unittest.main()
