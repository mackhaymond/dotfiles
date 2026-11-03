"""Closing the last tab of a space asks to close the space (docs/spaces.md
"Home: the space's main shell"): closed-tabs.sh `close --ask-space` /
`ask-space`, and stash.sh's range close calling it once.

A PRIVATE tmux server (`-L close-last-<uuid>`, `-f /dev/null`, its own
TMUX_TMPDIR, never the user's) with spaces faked by their options (@spaces on,
@space_home / @space_home_pane on each home, @space_hidden on a hidden
session), and a nested REAL client attached to it from a pane of a hidden
`host` session: where the client is comes from list-clients, whether the box
is up from that pane's screen (capture-pane), and keys reach the box through
send-keys into that pane. @space_sh is a recorder, so a "Close space" only
writes what prefix Q's command would have run. HOME, XDG_STATE_HOME and TMPDIR
are the test's temp dir, so the history and the script's log land there.
Teardown kills the server. Run with unittest or pytest.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

HERE = Path(__file__).resolve().parents[1]
CLOSED = HERE / "scripts" / "executable_closed-tabs.sh"
STASH = HERE / "scripts" / "executable_stash.sh"
SPACES = HERE / "spaces.conf"
TMUX = shutil.which("tmux")
LIFE = "sleep 600"
HIDDEN = "agents tasks stash scratch btop-popup pending"


@unittest.skipUnless(TMUX and shutil.which("jq"), "tmux / jq not installed")
class CloseLastTab(unittest.TestCase):
    def setUp(self):
        self.sock = "close-last-" + uuid.uuid4().hex[:12]
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="close-last-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # Short, private: a socket path must fit in sun_path.
        sockdir = tempfile.mkdtemp(prefix="clt-", dir="/private/tmp")
        self.addCleanup(shutil.rmtree, sockdir, True)
        bin_ = Path(self.tmp, "bin")
        bin_.mkdir()
        (bin_ / "ai-exec").write_text("#!/bin/sh\nexit 1\n")      # describe never leaves
        (bin_ / "ai-exec").chmod(0o755)
        self.wrapper = Path(self.tmp, "closed-tabs.sh")
        self.wrapper.write_text(f'#!/bin/sh\nexec bash "{CLOSED}" "$@"\n')
        self.wrapper.chmod(0o755)
        self.space_log = Path(self.tmp, "space.log")
        Path(self.tmp, "space.sh").write_text(f'printf "%s\\n" "$*" >> "{self.space_log}"\n')
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env.update(TERM="xterm-256color", HOME=self.tmp, TMPDIR=self.tmp + "/", TMUX_TMPDIR=sockdir,
                        XDG_STATE_HOME=os.path.join(self.tmp, "state"), AGENT_FOCUS_NOTICE="",
                        NOTIFY_SH=os.path.join(self.tmp, "no-notify"), NOTIFY_TMUX="true",
                        CLOSED_TABS_SH=str(self.wrapper), PATH=f"{bin_}:{os.environ.get('PATH', '/usr/bin:/bin')}")
        self.procs = []
        self.addCleanup(self.reap)
        self.main = self.space("main", 0)
        self.path = self.t("display", "-p", "#{socket_path}")
        for opt, val in (("exit-empty", "off"), ("@spaces", "on"), ("@space_hidden_sessions", HIDDEN),
                         ("@space_sh", f"bash {self.tmp}/space.sh")):
            self.t("set", "-g", opt, val)
        attach = f"env -u TMUX -u TMUX_PANE tmux -L {self.sock} -f /dev/null attach -t main"
        self.t("new-session", "-d", "-s", "host", "-x", "120", "-y", "40", attach)
        self.t("set", "-t", "host", "@space_hidden", "1")
        self.client = self.wait(lambda: self.t("list-clients", "-F", "#{client_name}"))

    # -- harness ---------------------------------------------------------
    def t(self, *args):
        r = subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", *args], env=self.env,
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, (args, r.stderr))
        return r.stdout.strip()

    def reap(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
        subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", "kill-server"], env=self.env,
                       capture_output=True, timeout=10)

    def wait(self, f, want=None, timeout=5.0):
        end = time.time() + timeout
        while True:
            v = f()
            if (v == want) if want is not None else v:
                return v
            if time.time() > end:
                self.fail(f"timed out: last {v!r}, want {want!r}")
            time.sleep(0.05)

    def space(self, name, tabs):
        """A session whose first window is its home: (home window, home pane, [tab panes])."""
        if name == "main":
            self.t("new-session", "-d", "-s", "main", "-x", "120", "-y", "38", LIFE)
        else:
            self.t("new-session", "-d", "-s", name, LIFE)
        home = self.t("display", "-p", "-t", f"={name}:", "#{window_id}")
        pane = self.t("display", "-p", "-t", f"={name}:", "#{pane_id}")
        self.t("set", "-w", "-t", home, "@space_home", "1")
        self.t("set", "-p", "-t", pane, "@space_home_pane", "1")
        return home, pane, [self.t("new-window", "-d", "-t", f"={name}:", "-P", "-F", "#{pane_id}", LIFE)
                            for _ in range(tabs)]

    def script_env(self):
        return dict(self.env, TMUX=f"{self.path},0,0")

    def run_script(self, *args, script=CLOSED):
        r = subprocess.run(["bash", str(script), *args], env=self.script_env(), capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def start(self, *args, script=CLOSED):
        """The script in the background: with the box up it holds until the box is answered."""
        p = subprocess.Popen(["bash", str(script), *args], env=self.script_env(),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def where(self):
        """(session, window, pane) the client is on."""
        for l in self.t("list-clients", "-F", "#{client_name}\t#{session_name}\t#{window_id}\t#{pane_id}").splitlines():
            c, *rest = l.split("\t")
            if c == self.client:
                return tuple(rest)
        return None

    def box(self):
        return "close space" in subprocess.run(
            [TMUX, "-L", self.sock, "-f", "/dev/null", "capture-pane", "-p", "-t", "host:"], env=self.env,
            capture_output=True, text=True, timeout=10).stdout

    def asks(self):
        log = Path(self.tmp, "state", "tmux-closed-tabs", "log")
        return log.read_text().count("ask-space") if log.exists() else 0

    def windows(self, name):
        return self.t("list-windows", "-t", f"={name}", "-F", "#{window_id}").split()

    def key(self, k):
        self.t("send-keys", "-t", "host:", k)

    def assert_no_box(self, settle=0.8):
        time.sleep(settle)
        self.assertFalse(self.box())
        self.assertEqual(self.asks(), 0)

    # -- tests -----------------------------------------------------------
    def test_a_plain_close_never_asks(self):
        home, _, [tab] = self.space("proj", 1)
        self.run_script("close", tab)
        self.assertEqual(self.windows("proj"), [home])
        self.assert_no_box()
        self.assertEqual(self.where()[0], "main")

    def test_no_box_while_other_tabs_remain(self):
        home, _, [a, b] = self.space("proj", 2)
        self.run_script("close", "--ask-space", self.client, a)      # returns: nothing to ask
        self.assertEqual(len(self.windows("proj")), 2)
        self.assert_no_box()
        self.assertEqual(self.where()[0], "main")

    def test_no_or_dismissed_lands_on_the_home_of_that_space(self):
        # From another space (the menu closes any space's tab): the client is
        # taken to the space's home before the box, so a dismissed box (Esc,
        # no callback) and Cancel (n) both leave it there.
        for n, k in enumerate(("Escape", "n"), 1):
            with self.subTest(k):
                name = "proj" + k
                home, hpane, [tab] = self.space(name, 1)
                self.t("switch-client", "-c", self.client, "-t", "main")
                p = self.start("close", "--ask-space", self.client, tab)
                self.wait(self.box)
                self.wait(self.asks, n)                 # the script saw it up
                self.assertEqual(self.where(), (name, home, hpane))
                self.key(k)
                self.wait(self.box, False)
                p.wait(timeout=5)
                self.assertEqual(self.where(), (name, home, hpane))
                self.assertEqual(self.windows(name), [home])
                self.assertFalse(self.space_log.exists())
        self.assertEqual(self.asks(), 2)

    def test_yes_closes_the_space_as_prefix_q_does(self):
        home, hpane, [tab] = self.space("proj", 1)
        sid = self.t("display", "-p", "-t", "=proj:", "#{session_id}")
        self.t("switch-client", "-c", self.client, "-t", tab)
        p = self.start("close", "--ask-space", self.client, tab)
        self.wait(self.box)
        self.wait(self.asks, 1)
        self.key("y")
        p.wait(timeout=5)
        self.wait(lambda: self.space_log.exists() and self.space_log.read_text())
        self.assertEqual(self.space_log.read_text(), f"close {sid} {self.client}\n")

    def test_yes_rechecks_and_a_tab_opened_meanwhile_keeps_the_space(self):
        notes = Path(self.tmp, "notify.log")
        notifier = Path(self.env["NOTIFY_SH"])                # in the server's env too
        notifier.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{notes}"\n')
        notifier.chmod(0o755)
        home, hpane, [tab] = self.space("proj", 1)
        p = self.start("close", "--ask-space", self.client, tab)
        self.wait(self.box)
        self.wait(self.asks, 1)
        self.t("new-window", "-d", "-t", "=proj:", LIFE)        # a tab while the box is up
        self.key("y")
        p.wait(timeout=5)
        self.wait(lambda: notes.exists() and "space has tabs again" in notes.read_text())
        self.assertFalse(self.space_log.exists())
        self.assertEqual(len(self.windows("proj")), 2)

    def test_closing_a_split_of_home_never_asks(self):
        # A space already down to its home, with a split in it: closing the
        # split leaves "only home", but it closed no tab.
        home, hpane, _ = self.space("proj", 0)
        split = self.t("split-window", "-d", "-t", home, "-P", "-F", "#{pane_id}", LIFE)
        self.run_script("close", "--ask-space", self.client, split)
        self.assertEqual(self.t("list-panes", "-t", home, "-F", "#{pane_id}"), hpane)
        self.assert_no_box()
        self.assertEqual(self.where()[0], "main")

    def test_the_lock_is_its_holders_while_it_lives(self):
        state = Path(self.tmp, "state", "tmux-closed-tabs")
        self.space("proj", 0)
        lock = state / ("ask." + self.t("display", "-p", "-t", "=proj:", "#{session_id}").lstrip("$"))
        lock.mkdir(parents=True)
        (lock / "pid").write_text(f"{os.getpid()}\n")           # a live holder: no second box
        self.run_script("ask-space", self.client, "=proj:")
        self.assert_no_box()
        self.assertTrue(lock.exists())                          # never released by another
        dead = subprocess.Popen(["true"]); dead.wait()
        (lock / "pid").write_text(f"{dead.pid}\n")              # a dead holder: taken over
        p = self.start("ask-space", self.client, "=proj:")
        self.wait(self.box)
        self.wait(self.asks, 1)
        self.assertEqual((lock / "pid").read_text().strip(), str(p.pid))
        self.key("Escape")
        p.wait(timeout=5)
        self.assertFalse(lock.exists())

    def test_main_never_asks_and_lands_on_its_home(self):
        mhome, mpane, _ = self.main
        split = self.t("split-window", "-d", "-t", mhome, "-P", "-F", "#{pane_id}", LIFE)
        self.t("select-pane", "-t", split)
        tab = self.t("new-window", "-t", "=main:", "-P", "-F", "#{pane_id}", LIFE)
        self.wait(self.where, ("main", self.t("display", "-p", "-t", tab, "#{window_id}"), tab))
        self.run_script("close", "--ask-space", self.client, tab)
        self.assertEqual(self.where(), ("main", mhome, mpane))
        self.assert_no_box()

    def test_hidden_sessions_and_spaces_off_never_ask(self):
        home, _, [tab] = self.space("scratch", 1)
        self.t("set", "-t", "scratch", "@space_hidden", "1")
        self.run_script("close", "--ask-space", self.client, tab)
        self.assertEqual(self.windows("scratch"), [home])
        self.t("set", "-g", "@spaces", "off")
        home, _, [tab] = self.space("proj", 1)
        self.run_script("close", "--ask-space", self.client, tab)
        self.assertEqual(self.windows("proj"), [home])
        self.assert_no_box()
        self.assertEqual(self.where()[0], "main")

    def test_two_closes_racing_to_empty_a_space_ask_once(self):
        home, hpane, [a, b] = self.space("proj", 2)
        self.run_script("close", a)
        self.run_script("close", b)                     # both gone, then both ask
        ps = [self.start("ask-space", self.client, "=proj:") for _ in range(2)]
        self.wait(self.box)
        self.wait(self.asks, 1)
        self.key("n")
        for p in ps:
            p.wait(timeout=5)
        time.sleep(0.5)
        self.assertFalse(self.box())
        self.assertEqual(self.asks(), 1)
        self.assertEqual(self.where(), ("proj", home, hpane))

    def test_the_box_waits_for_another_overlay_to_go(self):
        # The agent-ui menu (a popup) stays open after its x: the box comes
        # once that popup is gone, not never.
        home, hpane, [tab] = self.space("proj", 1)
        popup = subprocess.Popen([TMUX, "-L", self.sock, "-f", "/dev/null", "display-popup", "-c", self.client,
                                  "-w", "30", "-h", "5", "-T", "menu", "sleep 60"], env=self.env)
        self.procs.append(popup)
        self.wait(lambda: "menu" in self.t("capture-pane", "-p", "-t", "host:"))
        p = self.start("close", "--ask-space", self.client, tab)
        self.wait(self.where, ("proj", home, hpane))
        time.sleep(1.0)
        self.assertFalse(self.box())
        self.t("display-popup", "-c", self.client, "-C")
        self.wait(self.box)
        self.wait(self.asks, 1)
        self.key("Escape")
        p.wait(timeout=5)
        self.assertEqual(self.where(), ("proj", home, hpane))
        self.assertEqual(self.asks(), 1)

    def test_range_close_asks_once_after_all_its_closes(self):
        home, hpane, tabs = self.space("proj", 3)
        wins = [self.t("display", "-p", "-t", p, "#{window_id}") for p in tabs]
        self.t("switch-client", "-c", self.client, "-t", tabs[0])
        p = self.start("sel-kill-go", self.client, "0", *wins, script=STASH)
        self.wait(self.box)
        self.assertEqual(self.windows("proj"), [home])
        self.assertEqual(self.where(), ("proj", home, hpane))
        hist = Path(self.tmp, "state", "tmux-closed-tabs", "history.jsonl")
        self.assertEqual(len(hist.read_text().splitlines()), 3)
        self.wait(self.asks, 1)
        self.key("Escape")
        p.wait(timeout=5)
        time.sleep(0.5)
        self.assertFalse(self.box())
        self.assertEqual(self.asks(), 1)
        self.assertFalse(self.space_log.exists())

    def test_prefix_x_and_c_l_opt_in_on_the_pressing_client(self):
        # spaces.conf's own lines (the on branch's, the first of each), run as
        # installed: #{HOME}/.config/tmux/scripts/closed-tabs.sh is a wrapper
        # of the source copy here.
        text = SPACES.read_text()
        on = text[:text.index("\n%else\n")].splitlines()
        lines = [next(l for l in on if l.startswith(f"bind {k} ")) for k in ("x", "C-l")]
        self.assertIn("closed-tabs.sh close --ask-space #{client_name} #{pane_id}'", lines[0])
        self.assertIn("closed-tabs.sh close --ask-space '#{client_name}' '#{pane_id}'", lines[1])
        scripts = Path(self.tmp, ".config", "tmux", "scripts")
        scripts.mkdir(parents=True)
        shutil.copy(self.wrapper, scripts / "closed-tabs.sh")
        conf = Path(self.tmp, "close.conf")
        conf.write_text("\n".join(lines) + "\n")
        self.t("set", "-g", "@notify_sh", "true")
        self.t("source-file", str(conf))
        for n, keys in enumerate((["C-l"], ["x", "y"]), 1):
            with self.subTest(keys[0]):
                name = "proj" + keys[0].replace("-", "")
                home, hpane, [tab] = self.space(name, 1)
                self.t("switch-client", "-c", self.client, "-t", tab)
                self.wait(lambda: self.where()[2], tab)
                self.t("send-keys", "-K", "-c", self.client, "C-b", keys[0])
                if len(keys) > 1:
                    self.wait(lambda: "Close" in self.t("capture-pane", "-p", "-t", "host:"))
                    self.t("send-keys", "-K", "-c", self.client, keys[1])
                self.wait(self.box)
                self.wait(self.asks, n)
                self.assertEqual(self.where(), (name, home, hpane))
                self.key("Escape")
                self.wait(self.box, False)
                self.assertEqual(self.windows(name), [home])

    def test_range_close_leaving_tabs_does_not_ask(self):
        home, _, tabs = self.space("proj", 3)
        wins = [self.t("display", "-p", "-t", p, "#{window_id}") for p in tabs[:2]]
        self.run_script("sel-kill-go", self.client, "0", *wins, script=STASH)
        self.assertEqual(len(self.windows("proj")), 2)
        self.assert_no_box()


if __name__ == "__main__":
    unittest.main()
