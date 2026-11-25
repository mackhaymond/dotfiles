"""External subagents on the tab bar and the status surfaces (U6).

docs/agent-tab-indicator.md §8 and Mack's direction of 2026-11-23: a child
tab (@agent_child) is not drawn unless it is the current window or under the
stash tint; its parent's tab shows `↳N` (the
total from @agent_kids_sub, else @agent_kids), `↳N?` while @agent_kids_ask is
set, with no tint of its own. Also the home dashboard (space-dash.sh: kids
indented under their parent) and the space picker's rollup
(mru-session-switch.sh: kids counted only while they ask or failed).

Every case runs a PRIVATE tmux: TMUX/TMUX_PANE dropped from the environment,
a private TMUX_TMPDIR, `-L <unique>` and `-f /dev/null`, killed by its socket
path in cleanup. The live server is never touched. Each generated snippet is
parsed with `source-file -n` before it is sourced. Run with unittest or pytest.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
import uuid

HERE = Path(__file__).resolve().parents[1]
TMUX_CONF = HERE / "tmux.conf.tmpl"
SCRIPTS = HERE / "scripts"
REAL_TMUX = shutil.which("tmux")
CATPPUCCIN = Path.home() / ".config/tmux/plugins/tmux/catppuccin.tmux"
STYLE = re.compile(r"#\[[^\]]*\]")


def visible(s):
    """What a rendered format shows: its #[...] styles dropped."""
    return STYLE.sub("", s)


def conf_lines(pattern):
    text = TMUX_CONF.read_text()
    return [l for l in text.splitlines() if re.match(pattern, l)]


def wrapper_block():
    """The post-tpm wrapper of window-status-format, verbatim."""
    text = TMUX_CONF.read_text()
    m = re.search(r"^if -F '#\{!:#\{m:\*@agent_tab_\*.*?^set -gw window-status-current-format .*?$",
                  text, re.M | re.S)
    assert m, "the window-status-format wrapper is missing from tmux.conf.tmpl"
    return m.group(0)


class Server:
    """A private tmux server; session `s` holds window w0 (a plain shell)."""

    def __init__(self, test):
        base = Path("/private/tmp/claude-%d" % os.getuid())
        base.mkdir(parents=True, exist_ok=True)
        self.dir = Path(tempfile.mkdtemp(prefix="u6-", dir=str(base)))   # short: socket path limit
        (self.dir / "sock").mkdir()
        self.name = "u6-" + uuid.uuid4().hex[:10]
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env["TMUX_TMPDIR"] = str(self.dir / "sock")
        self.tm = [REAL_TMUX, "-L", self.name, "-f", "/dev/null"]
        self.socket = None
        test.addCleanup(self.close)
        out = self.tmux("new-session", "-d", "-s", "s", "-x", "200", "-y", "24", "-c", str(self.dir),
                        "-P", "-F", "#{window_id}|#{socket_path}", "/bin/sleep 600")
        self.w0, self.socket = out.split("|", 1)
        assert self.socket.startswith(str(self.dir)), self.socket      # never the live server
        self.tmux("set", "-g", "exit-empty", "off")
        # A `tmux` on PATH that reaches only this server (catppuccin.tmux,
        # space-dash.sh, mru-session-switch.sh call a bare tmux).
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        (self.bin / "tmux").write_text('#!/bin/sh\nexec %s -S %s "$@"\n' % (REAL_TMUX, self.socket))
        (self.bin / "tmux").chmod(0o755)

    def close(self):
        if self.socket:                       # explicit socket only, never a bare kill-server
            subprocess.run([REAL_TMUX, "-S", self.socket, "kill-server"], env=self.env,
                           capture_output=True, timeout=10)
        shutil.rmtree(self.dir, ignore_errors=True)

    def tmux(self, *args, check=True):
        r = subprocess.run(self.tm + list(args), text=True, capture_output=True, env=self.env, timeout=20)
        if check and r.returncode != 0:
            raise AssertionError("tmux %s: %s" % (args, r.stderr))
        return r.stdout.rstrip("\n")

    def source(self, text):
        """source-file -n (parse only) first, then for real."""
        f = self.dir / ("snip-%s.conf" % uuid.uuid4().hex[:6])
        f.write_text(text + "\n")
        self.tmux("source-file", "-n", str(f))
        self.tmux("source-file", str(f))

    def window(self, name="w", **opts):
        w = self.tmux("new-window", "-d", "-t", "s:", "-n", name, "-P", "-F", "#{window_id}", "/bin/sleep 600")
        self.set(w, **opts)
        return w

    def set(self, win, **opts):
        for k, v in opts.items():
            if v is None:
                self.tmux("set-option", "-uw", "-t", win, k)
            else:
                self.tmux("set-option", "-w", "-t", win, k, v)

    def path_env(self):
        e = dict(self.env)
        e["PATH"] = "%s:%s" % (self.bin, e.get("PATH", "/usr/bin:/bin"))
        return e


def load_tab_options(srv):
    """Every option the tab formats read: the catppuccin window options, the
    rate-limit mark and the kids mark, exactly as tmux.conf.tmpl sets them."""
    lines = conf_lines(r'^set -g @(catppuccin_window_|agent_limit_|agent_kids_tab )')
    assert any("@agent_kids_tab" in l for l in lines)
    srv.source("\n".join(lines))


@unittest.skipUnless(REAL_TMUX, "tmux executable required")
class TabFormatTests(unittest.TestCase):
    """The @catppuccin_window_* formats and @agent_kids_tab, rendered by a
    real private tmux (ported from test_agent_kids.py's KidsTabFormatTests,
    which predates the ↳N mark)."""

    @classmethod
    def setUpClass(cls):
        text = TMUX_CONF.read_text()
        cls.fmt = {}
        for name in ("default_text", "current_text", "default_background", "current_background",
                     "default_color", "current_color"):
            m = re.search(r'^set -g @catppuccin_window_%s "(.*)"$' % name, text, re.M)
            assert m, name
            cls.fmt[name] = m.group(1)

    def setUp(self):
        self.s = Server(self)
        load_tab_options(self.s)

    def render(self, name, **opts):
        w = self.s.window()
        self.s.set(w, **{k: v for k, v in opts.items() if v is not None})
        return self.s.tmux("display-message", "-p", "-t", w, self.fmt[name])

    def test_child_text_is_unchanged(self):
        # Never drawn now (the wrapper), but under the stash tint it is: the
        # plain grey ↳ tab, as before.
        for state in ("running", "done", "needs-input", "failed"):
            for name in ("default_background", "current_background", "default_color", "current_color"):
                with self.subTest(state=state, name=name):
                    self.assertEqual(self.render(name, **{"@agent_state": state, "@agent_child": "@1",
                                                          "@agent_workflow": "1"}),
                                     self.render(name))
            for name in ("default_text", "current_text"):
                out = self.render(name, **{"@agent_state": state, "@agent_child": "@1", "@agent_summary": "p/kid",
                                           "@agent_workflow": "1", "@agent_cua": "1"})
                self.assertEqual(out, "#[fg=#6c7086]↳ #[fg=#cdd6f4]p/kid")

    def test_intermediate_child_shows_its_own_subtree(self):
        # Drawn while current: an intermediate child's own ↳N, grey on its
        # untinted body whatever its state; crust only under the stash tint.
        for state in ("running", "needs-input", "failed"):
            for name in ("default_text", "current_text"):
                with self.subTest(state=state, name=name):
                    out = self.render(name, **{"@agent_state": state, "@agent_child": "@1",
                                               "@agent_summary": "p/mid", "@agent_kids_sub": "2 1"})
                    self.assertEqual(out, "#[fg=#6c7086]↳ #[fg=#cdd6f4]p/mid#[fg=#6c7086] ↳2")
        self.assertTrue(self.render("current_text", **{"@agent_child": "@1", "@agent_kids_sub": "2 1",
                                                       "@stash_sel": "1"}).endswith("#[fg=#11111b] ↳2"))

    def test_no_kids_no_mark(self):
        for name in ("default_text", "current_text"):
            self.assertEqual(self.render(name, **{"@agent_state": "running", "@agent_summary": "p/solo"}),
                             "#[fg=#cdd6f4]p/solo")

    def test_parent_shows_its_total(self):
        p = {"@agent_state": "running", "@agent_workflow": "1", "@agent_summary": "p/abby",
             "@agent_kids": "3 2", "@agent_kids_sub": "3 2"}
        for name in ("default_text", "current_text"):
            out = self.render(name, **p)
            self.assertTrue(out.endswith("#[fg=#cdd6f4]p/abby#[fg=#6c7086] ↳3"), out)
        # @agent_kids_sub wins (an intermediate has only that one); else @agent_kids.
        self.assertTrue(self.render("default_text", **dict(p, **{"@agent_kids": None, "@agent_kids_sub": "2 0"}))
                        .endswith(" ↳2"))
        self.assertTrue(self.render("default_text", **dict(p, **{"@agent_kids_sub": None, "@agent_kids": "5 1"}))
                        .endswith(" ↳5"))
        # 10+ children: the whole number.
        self.assertTrue(self.render("default_text", **dict(p, **{"@agent_kids_sub": "12 3"})).endswith(" ↳12"))

    def test_asked_for_parent_takes_the_childs_state_and_a_question_mark(self):
        ask = {"@agent_state": "running", "@agent_kids": "2 1", "@agent_kids_sub": "2 1", "@agent_workflow": "1"}
        yellow = dict(ask, **{"@agent_kids_ask": "@7\tneeds-input\tfailed to x"})
        red = dict(ask, **{"@agent_kids_ask": "@7\tfailed\tboom"})
        for name in ("default_background", "current_background"):
            self.assertEqual(self.render(name, **yellow), "#f9e2af")
            self.assertEqual(self.render(name, **red), "#f38ba8")
        for name in ("default_color", "current_color"):
            self.assertEqual(self.render(name, **yellow), "#11111b")
        out = self.render("default_text", **dict(yellow, **{"@agent_summary": "p/abby"}))
        self.assertEqual(out, "#[fg=#11111b]󰒓 #[fg=#11111b]p/abby#[fg=#11111b] ↳2?")
        self.assertEqual(self.render("current_text", **dict(red, **{"@agent_summary": "p/abby"})),
                         "#[fg=#11111b]󰒓 #[fg=#11111b]p/abby#[fg=#11111b] ↳2?")
        # The parent's own attention state wins over its child's; the ? stays.
        self.assertEqual(self.render("default_background", **dict(yellow, **{"@agent_state": "failed"})),
                         "#f38ba8")
        self.assertTrue(visible(self.render("default_text", **dict(yellow, **{"@agent_state": "failed"})))
                        .endswith(" ↳2?"))
        # The ask beats the parent's own green.
        self.assertEqual(self.render("default_background", **dict(red, **{"@agent_state": "done",
                                                                          "@agent_workflow": None})), "#f38ba8")

    def test_the_mark_adds_no_tint(self):
        # A parent with busy kids and no ask: the stock body, the gear.
        p = {"@agent_state": "idle", "@agent_kids": "1 1", "@agent_kids_sub": "1 1", "@agent_workflow": "1"}
        self.assertIn("󰒓", self.render("default_text", **p))
        self.assertEqual(self.render("default_background", **p), "#313244")
        self.assertEqual(self.render("current_background", **p), "#1e1e2e")
        # On a green done (no workflow) the mark goes dark ink like the label.
        out = self.render("default_text", **{"@agent_state": "done", "@agent_kids_sub": "1 0",
                                              "@agent_kids": "1 0", "@agent_summary": "p/x"})
        self.assertTrue(out.endswith("#[fg=#11111b]p/x#[fg=#11111b] ↳1"), out)


@unittest.skipUnless(REAL_TMUX, "tmux executable required")
class WrapperTests(unittest.TestCase):
    """The post-tpm wrapper: a child renders nothing, everything else renders
    catppuccin's tab unchanged."""

    # Stand-ins for catppuccin's built formats: commas at the top level (as
    # in #[fg=..,bg=..]) are the case the wrapper must survive.
    DEF = "#[fg=#89b4fa,bg=default]█#[fg=#11111b,bg=#89b4fa]#I #{?#{@agent_summary},#{@agent_summary},#W}█"
    CUR = "#[fg=#fab387,bg=default]█#[fg=#11111b,bg=#fab387]#I*#{?#{@agent_summary},#{@agent_summary},#W}█"

    def setUp(self):
        self.s = Server(self)
        self.s.tmux("set", "-gw", "window-status-format", self.DEF)
        self.s.tmux("set", "-gw", "window-status-current-format", self.CUR)
        self.s.source(wrapper_block())

    def tab(self, w, current=False):
        return self.s.tmux("display-message", "-p", "-t", w,
                           "#{T:window-status-%sformat}" % ("current-" if current else ""))

    def bar(self):
        return visible(self.s.tmux("display-message", "-p", "-t", "s:",
                                   "#{W:#{T:window-status-format}|,#{T:window-status-current-format}|}"))

    def test_children_render_nothing(self):
        parent = self.s.window("p", **{"@agent_state": "running", "@agent_summary": "parent"})
        kid = self.s.window("k", **{"@agent_state": "running", "@agent_child": parent, "@agent_summary": "kid"})
        self.assertEqual(self.tab(kid), "")
        self.assertEqual(self.tab(parent), self.s.tmux("display-message", "-p", "-t", parent, self.DEF))
        self.assertEqual(self.tab(parent, current=True),
                         self.s.tmux("display-message", "-p", "-t", parent, self.CUR))
        self.assertNotIn("kid", self.bar())
        self.assertIn("parent", self.bar())

    def test_a_child_that_is_the_current_window_is_drawn(self):
        # A jump onto a child's ask lands on a highlighted tab.
        parent = self.s.window("p", **{"@agent_state": "running", "@agent_summary": "parent"})
        kid = self.s.window("k", **{"@agent_state": "needs-input", "@agent_child": parent,
                                    "@agent_summary": "kid"})
        self.assertEqual(self.tab(kid, current=True), self.s.tmux("display-message", "-p", "-t", kid, self.CUR))
        self.s.tmux("select-window", "-t", kid)
        self.assertIn("2*kid█|", self.bar())     # drawn with the current format
        self.s.tmux("select-window", "-t", parent)
        self.assertNotIn("kid", self.bar())

    def test_a_child_under_the_stash_tint_is_drawn(self):
        parent = self.s.window("p", **{"@agent_state": "running"})
        kid = self.s.window("k", **{"@agent_child": parent, "@agent_summary": "kid", "@stash_sel": "1"})
        self.assertIn("kid", visible(self.tab(kid)))

    def test_a_reload_without_tpm_keeps_the_kept_formats(self):
        # tpm did not rebuild the formats: the wrapper must not capture itself.
        self.s.source(wrapper_block())
        self.assertEqual(self.s.tmux("show", "-gv", "@agent_tab_default"), self.DEF)
        self.assertEqual(self.s.tmux("show", "-gv", "@agent_tab_current"), self.CUR)
        # A reload whose tpm DID rebuild them: the new strings are kept.
        self.s.tmux("set", "-gw", "window-status-format", self.DEF + "x")
        self.s.source(wrapper_block())
        self.assertEqual(self.s.tmux("show", "-gv", "@agent_tab_default"), self.DEF + "x")


@unittest.skipUnless(REAL_TMUX and CATPPUCCIN.is_file(), "tmux and the catppuccin plugin required")
class RealBarTests(unittest.TestCase):
    """The real assembly: tmux.conf.tmpl's options, the real catppuccin
    plugin building the formats (its bare `tmux` reaches only the private
    server), then the wrapper. A parent, a running child and an asking child:
    the children are not drawn, the parent reads `↳2?`."""

    def test_render(self):
        s = Server(self)
        load_tab_options(s)
        r = subprocess.run(["bash", str(CATPPUCCIN)], env=s.path_env(), capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("@agent_tab_", s.tmux("show", "-gwv", "window-status-format"))   # catppuccin's own
        s.source(wrapper_block())
        parent = s.window("parent", **{"@agent_state": "running", "@agent_workflow": "1",
                                       "@agent_summary": "proj/orchestrator", "@agent_kids": "2 2",
                                       "@agent_kids_sub": "2 2", "@agent_kids_ask": "@x\tneeds-input\tBash ls"})
        s.window("kid-a", **{"@agent_state": "running", "@agent_child": parent, "@agent_summary": "proj/kid-run"})
        s.window("kid-b", **{"@agent_state": "needs-input", "@agent_child": parent,
                             "@agent_summary": "proj/kid-ask"})
        s.tmux("select-window", "-t", parent)
        bar = s.tmux("display-message", "-p", "-t", "s:",
                     "#{W:#{T:window-status-format},#{T:window-status-current-format}}")
        shown = visible(bar)
        self.assertNotIn("kid-run", shown)
        self.assertNotIn("kid-ask", shown)
        self.assertIn("proj/orchestrator ↳2?", shown)
        # w0 (a plain tab, its name whatever catppuccin's rename makes it),
        # then the parent; tabs 2 and 3 are not drawn.
        self.assertRegex(shown, r"^█0█ [^█]*██1█ \U000f0493 proj/orchestrator ↳2\?█$")
        # Window indexes 2 and 3 still exist; they are just not drawn.
        self.assertEqual(s.tmux("list-windows", "-t", "s", "-F", "#{window_index}").split(), ["0", "1", "2", "3"])
        print("\n[tab bar, parent current] " + shown)
        # Jump onto the asking child: its tab is drawn, highlighted; the
        # running child stays hidden.
        s.tmux("select-window", "-t", "s:3")
        shown = visible(s.tmux("display-message", "-p", "-t", "s:",
                               "#{W:#{T:window-status-format},#{T:window-status-current-format}}"))
        self.assertNotIn("kid-run", shown)
        self.assertRegex(shown, r"^█0█ [^█]*██1█ \U000f0493 proj/orchestrator ↳2\?██3█ ↳ proj/kid-ask█$")
        print("[tab bar, asking child current] " + shown)


@unittest.skipUnless(REAL_TMUX, "tmux executable required")
class SpaceDashTests(unittest.TestCase):
    """space-dash.sh --once: children sit indented under their parent and
    are folded out of the agent count."""

    def test_kids_render_under_their_parent(self):
        s = Server(self)
        p = s.window("p", **{"@agent_state": "running", "@agent_key": "a", "@agent_summary": "proj/parent",
                             "@agent_workflow": "1"})
        k1 = s.window("k1", **{"@agent_state": "running", "@agent_child": p, "@agent_summary": "proj/kid-one"})
        s.window("o", **{"@agent_state": "idle", "@agent_key": "b", "@agent_summary": "proj/other",
                         "@agent_detail": "x\x1fy"})
        s.window("g", **{"@agent_state": "needs-input", "@agent_child": k1, "@agent_summary": "proj/grandkid",
                         "@agent_key": ";z"})   # a stale key on a child: not shown, never widens the column
        s.window("k2", **{"@agent_state": "done", "@agent_child": p, "@agent_summary": "proj/kid-two"})
        # A loop (the reconcile never writes one): drawn from its first
        # window, counted as one agent, the other nested under it.
        la = s.window("la", **{"@agent_state": "idle", "@agent_summary": "proj/loop-a"})
        lb = s.window("lb", **{"@agent_state": "idle", "@agent_summary": "proj/loop-b", "@agent_child": la})
        s.set(la, **{"@agent_child": lb})
        env = s.path_env()
        env.update({"TMUX": "%s,1,0" % s.socket, "TMUX_PANE": s.tmux("display", "-p", "-t", s.w0, "#{pane_id}"),
                    "SPACE_TMUX_BIN": str(s.bin / "tmux")})
        r = subprocess.run(["bash", str(SCRIPTS / "executable_space-dash.sh"), "--once"], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", l).rstrip() for l in r.stdout.replace("\r", "").split("\n")]
        text = "\n".join(lines)
        self.assertIn("3 agents", text)
        rows = [l for l in lines if "proj/" in l]
        self.assertEqual([re.sub(r"\s+", " ", l.strip()) for l in rows], [
            "◐ a proj/parent",
            "◐ ↳ proj/kid-one",
            "◉ ↳ proj/grandkid",
            "✓ ↳ proj/kid-two",
            "○ b proj/other x y",
            "○ proj/loop-a",
            "○ ↳ proj/loop-b",
        ])
        # The key column is one cell: "  ◐ a  proj/parent".
        self.assertEqual(rows[0].index("proj/parent"), 7)
        # Indentation: a child's ↳ in its parent's label column, a
        # grandchild's two cells further in.
        self.assertEqual(rows[1].index("↳"), rows[0].index("proj/parent"))
        self.assertEqual(rows[2].index("↳"), rows[1].index("↳") + 2)


@unittest.skipUnless(REAL_TMUX, "tmux executable required")
class SpacePickerTests(unittest.TestCase):
    """mru-session-switch.sh's rollup: a child counts only while it asks or
    failed (its parent's gear already counts its work)."""

    def decorate(self, s, sessions):
        src = (SCRIPTS / "executable_mru-session-switch.sh").read_text()
        src = re.sub(r'^main "\$@"\s*$', "", src, flags=re.M)
        f = s.dir / "mru.sh"
        f.write_text(src + '\ndecorate "$1"\n')
        r = subprocess.run(["bash", str(f), sessions], env=s.path_env(), capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return {l.split("\t")[0]: re.sub(r"\x1b\[[0-9;]*m", "", l.split("\t")[1]) for l in r.stdout.splitlines()}

    def test_children_fold_into_the_parent(self):
        s = Server(self)
        s.tmux("new-session", "-d", "-s", "work", "/bin/sleep 600")
        p = s.tmux("display", "-p", "-t", "work:", "#{window_id}")
        s.set(p, **{"@agent_state": "idle", "@agent_workflow": "1"})
        for st in ("running", "running", "done"):
            w = s.tmux("new-window", "-d", "-t", "work:", "-P", "-F", "#{window_id}", "/bin/sleep 600")
            s.set(w, **{"@agent_state": st, "@agent_child": p})
        self.assertEqual(self.decorate(s, "work")["work"], "● 1 working")
        w = s.tmux("new-window", "-d", "-t", "work:", "-P", "-F", "#{window_id}", "/bin/sleep 600")
        s.set(w, **{"@agent_state": "needs-input", "@agent_child": p})
        self.assertEqual(self.decorate(s, "work")["work"], "● 1 needs you · 1 working")


if __name__ == "__main__":
    unittest.main()
