"""POPUP POSITION: tmux.conf.tmpl's @popup_x centres popups and menus on the
whole WezTerm row while the agent sidebar (a WezTerm split to the right of the
tmux client) is open, instead of on the narrower client.

Static: every centred display-popup / display-menu in the config and the
scripts carries the shared -x (escaped as ##{...} inside `run-shell -C`, with
the unset fallback in scripts).

Live: a PRIVATE tmux server (`-L popup-x-<uuid>`, `-f /dev/null`, its own
TMUX_TMPDIR, never the user's) with a nested REAL client attached from one of
its own panes. A popup's column is read off that pane's screen
(capture-pane), i.e. where the client actually drew it. Teardown kills the
server. Run with unittest or pytest.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

HERE = Path(__file__).resolve().parents[1]
CONF = HERE / "tmux.conf.tmpl"
SPACES = HERE / "spaces.conf"
SCRIPTS = HERE / "scripts"
TMUX = shutil.which("tmux")
FALLBACK = "#{?#{@popup_x},#{E:@popup_x},#{popup_centre_x}}"
COLS, ROWS = 120, 40


def code_lines(path):
    """(number, line) for every line that is not a comment."""
    return [(i + 1, l) for i, l in enumerate(path.read_text().splitlines()) if not l.lstrip().startswith("#")]


class ConfigShape(unittest.TestCase):
    def test_popup_x_is_set_once(self):
        lines = [l for l in CONF.read_text().splitlines() if l.startswith("set -g @popup_x ")]
        self.assertEqual(len(lines), 1, lines)
        self.assertFalse([l for _, l in code_lines(SPACES) if re.search(r"\bset\b.*@popup_x ", l)])

    def test_no_binary_repair_drops_the_sidebar_options(self):
        # spaces.conf's repair without agent-ui: no sidebar can be open, so
        # nothing may keep shifting popups (or toasting into a dead one).
        line = next(l for _, l in code_lines(SPACES) if l.startswith("set -g @space_sidebar_repair "))
        other = line.split("else", 1)[1]
        for opt in ("@sidebar_open", "@sidebar_clients", "@sidebar_width"):
            self.assertIn("set -gu " + opt, other)

    def test_config_popups_and_menus_carry_popup_x(self):
        seen = 0
        for path in (CONF, SPACES):
            for n, l in code_lines(path):
                for m in re.finditer(r"(run-shell -C \")?(display-popup|display-menu)\b", l):
                    seen += 1
                    rest = l[m.start():]
                    # Only the -x of THIS command: up to the next popup/menu on the line.
                    nxt = re.search(r"(display-popup|display-menu)\b", rest[len(m.group(0)):])
                    rest = rest[:len(m.group(0)) + nxt.start()] if nxt else rest
                    want = "-x '##{E:@popup_x}'" if m.group(1) else "-x '#{E:@popup_x}'"
                    self.assertIn(want, rest, "%s:%d" % (path.name, n))
                    self.assertNotRegex(rest, r"-x C\b", "%s:%d" % (path.name, n))
        # q/C-q/M-w twice (python, agent-ui), a/C-a twice, K, x (tmux.conf);
        # M-n, Q, x, C-t, K, and the off branch's K and x (spaces.conf).
        self.assertGreaterEqual(seen, 18)

    def test_scripts_pass_popup_x_with_a_fallback(self):
        # agent-pause.py places its popup itself (popup_geometry: the WezTerm
        # tab's centre from `wezterm cli list`), so it keeps its computed -x.
        own = {"executable_agent-pause.py"}
        seen = set()
        for path in sorted(SCRIPTS.iterdir()):
            if not path.is_file() or path.name in own or path.suffix not in (".sh", ".py"):
                continue
            text = path.read_text()
            for n, l in code_lines(path):
                # A call, not prose: tmux's argv in python, the command in shell.
                if re.search(r'"display-popup"|tmux display-popup|\(display-menu\b', l):
                    seen.add(path.name)
                    self.assertNotRegex(l, r"-x C\b", "%s:%d" % (path.name, n))
            if path.name in seen:
                self.assertIn(FALLBACK, text, path.name)
                self.assertNotRegex(text, r"\(-x C -y C", path.name)
        self.assertEqual(seen, {"codexbar-usage-key.sh", "executable_agent-roster.py", "executable_closed-tabs.sh",
                                "executable_space.sh", "executable_stash.sh", "executable_toggle-btop-popup.sh"})
        # Each popup call in them passes it (space.sh builds its menu's argv);
        # a call may continue over `\`-ended lines (stash.sh's sel-kill box).
        for name, n in (("executable_closed-tabs.sh", 1), ("executable_stash.sh", 3),
                        ("executable_toggle-btop-popup.sh", 2)):
            text = (SCRIPTS / name).read_text()
            calls = re.findall(r"tmux display-popup(?:[^\n]*\\\n)*[^\n]*", text)
            self.assertEqual(len(calls), n, name)
            self.assertTrue(all(re.search(r'-x "\$(POPUP_X|popup_x)"', c) for c in calls), calls)
        self.assertIn('args+=(-x "$POPUP_X" -y C --)', (SCRIPTS / "executable_space.sh").read_text())
        self.assertIn("-x '%s'" % FALLBACK, (SCRIPTS / "codexbar-usage-key.sh").read_text())


@unittest.skipIf(TMUX is None, "no tmux")
class Live(unittest.TestCase):
    """Where the client draws a popup, on a private server."""

    def setUp(self):
        self.sock = "popup-x-" + uuid.uuid4().hex[:12]
        self.tmp = tempfile.mkdtemp(prefix="popup-x-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env["TMUX_TMPDIR"] = self.tmp
        self.env["TERM"] = "xterm-256color"
        self.addCleanup(lambda: subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", "kill-server"],
                                               env=self.env, capture_output=True, timeout=10))
        self.t("new-session", "-d", "-s", "work", "-x", str(COLS), "-y", str(ROWS), "sleep 600")
        self.t("set", "-g", "status", "off")
        attach = "env -u TMUX -u TMUX_PANE tmux -L %s -f /dev/null attach -t work" % self.sock
        self.t("new-session", "-d", "-s", "host", "-x", str(COLS), "-y", str(ROWS), attach)
        self.tty = self.wait(lambda: next((l.split()[0] for l in self.t("list-clients", "-F",
                                           "#{client_tty} #{session_name}").splitlines() if l.endswith(" work")), ""))
        line = next(l for l in CONF.read_text().splitlines() if l.startswith("set -g @popup_x "))
        conf = os.path.join(self.tmp, "popup.conf")
        Path(conf).write_text(line + "\n")
        self.t("source-file", conf)

    def t(self, *args, raw=False):
        r = subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", *args], env=self.env,
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, (args, r.stderr))
        return r.stdout if raw else r.stdout.strip()

    def wait(self, f, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            v = f()
            if v:
                return v
            time.sleep(0.05)
        self.fail("timed out")

    def drawn(self):
        """(left column, width) of the popup/menu box on the client's screen, or None."""
        for l in self.t("capture-pane", "-p", "-t", "host:0", raw=True).splitlines():
            for a, b in (("┌", "┐"), ("╭", "╮")):
                if a in l and b in l:
                    return l.index(a), l.rindex(b) - l.index(a) + 1
        return None

    def col(self, *args):
        """Open a popup/menu with `args` (a tmux command), read where it is drawn, close it."""
        p = subprocess.Popen([TMUX, "-L", self.sock, "-f", "/dev/null", *args], env=self.env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            got = self.wait(self.drawn)
            time.sleep(0.1)      # a second read: the box is fully drawn
            got = self.drawn() or got
        finally:
            subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", "display-popup", "-c", self.tty, "-C"],
                           env=self.env, capture_output=True, timeout=10)
            subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", "send-keys", "-t", "host:0", "Escape"],
                           env=self.env, capture_output=True, timeout=10)
            self.wait(lambda: self.drawn() is None)
            p.kill()
            p.wait()
        return got

    def popup(self, x, w=40):
        return self.col("display-popup", "-c", self.tty, "-w", str(w), "-h", "10", "-x", x, "sleep 30")

    def want(self, w, extra=0):
        """tmux's centre on a row `extra` columns wider than the client, clamped into it."""
        n = (COLS - 1 + extra) // 2 - w // 2
        return (min(max(n, 0), COLS - w), w)

    def test_popups_centre_on_the_row_for_a_client_with_a_sidebar(self):
        X = "#{E:@popup_x}"
        # No sidebar: exactly tmux's own -x C.
        self.assertEqual(self.popup("C"), self.want(40))
        self.assertEqual(self.popup(X), self.want(40))
        # This client has a 46-column sidebar: +47 columns (its divider too).
        self.t("set", "-g", "@sidebar_clients", "/dev/ttys999 " + self.tty)
        self.t("set", "-g", "@sidebar_width", "47")
        self.assertEqual(self.popup(X), self.want(40, 47))
        self.assertEqual(self.popup(X), (63, 40))
        self.assertEqual(self.popup(X, w=41), self.want(41, 47))
        # Too wide to fit shifted: as far right as fits (tmux clamps).
        self.assertEqual(self.popup(X, w=100), (COLS - 100, 100))
        # The menus (prefix x's y/n box) move the same way.
        menu = ("display-menu", "-c", self.tty, "-T", "close tab", "-x", X, "-y", "C", "Cancel", "n", "", "Close", "y", "")
        got = self.col(*menu)
        self.assertEqual(got, self.want(got[1], 47))
        # Through run-shell -C (prefix q, Option-N...): escaped, it expands at
        # popup time, not at run-shell time (when popup_* do not exist).
        rs = "display-popup -c '%s' -w 40 -h 10 -x '##{E:@popup_x}' 'sleep 30'" % self.tty
        self.assertEqual(self.col("run-shell", "-t", "work", "-C", rs), self.want(40, 47))
        # The scripts' form, with the option set.
        self.assertEqual(self.popup(FALLBACK), self.want(40, 47))
        # A sidebar LEFT of the client (STRIP_SIDE = "Left"): a negative
        # width, the row's middle is left of the client's.
        self.t("set", "-g", "@sidebar_width", "-47")
        self.assertEqual(self.popup(X), self.want(40, -47))
        self.assertEqual(self.popup(X), (16, 40))
        self.assertEqual(self.popup(X, w=100), (0, 100))        # clamped to the left edge
        self.t("set", "-g", "@sidebar_width", "47")
        # A client without a sidebar is left alone.
        self.t("set", "-g", "@sidebar_clients", "/dev/ttys999")
        self.assertEqual(self.popup(X), self.want(40))
        # Listed but no width (an older sidebar): plain centre.
        self.t("set", "-g", "@sidebar_clients", self.tty)
        self.t("set", "-gu", "@sidebar_width")
        self.assertEqual(self.popup(X), self.want(40))

    def test_the_scripts_fallback_centres_without_popup_x(self):
        # A server that has not loaded the new tmux.conf: tmux's centre, not column 0.
        self.t("set", "-gu", "@popup_x")
        self.assertEqual(self.popup("#{E:@popup_x}"), (0, 40))   # why the fallback exists
        self.assertEqual(self.popup(FALLBACK), self.want(40))


if __name__ == "__main__":
    unittest.main()
