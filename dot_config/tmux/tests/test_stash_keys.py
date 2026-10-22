"""prefix v's range selection: the keys hold until the mode is ended, and the
root keys that make them hold exist only while a selection does.

tmux.conf.tmpl's stash section (@stash_live, prefix v, the `stash` key table)
sourced into a real tmux on its own `-L` socket started
with `-f /dev/null`, one client attached from a pseudo-terminal this process
opens (pty.fork), and the chezmoi source copy of stash.sh doing the work — it
sources scripts/stash-root-keys.conf (beside it) on sel-start. HOME
and TMPDIR are the test's temp dir, so the bindings' #{HOME}/.config/tmux/
scripts/stash.sh is a wrapper that runs that copy, its log and lock land there, and it
never touches the real server (its sidecar writer refuses any socket but the
default one). Typed input is written into the client's terminal: keys, SGR
mouse reports, focus reports (\\e[O / \\e[I), bracketed pastes. Panes that must
be compared byte for byte run `stty raw -echo; exec cat > FILE`.
Run with unittest or pytest.
"""
import fcntl
import os
from pathlib import Path
import pty
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
import unittest
import uuid

HERE = Path(__file__).resolve().parents[1]
CONF = HERE / "tmux.conf.tmpl"
STASH = HERE / "scripts" / "executable_stash.sh"
ROOT_CONF = HERE / "scripts" / "stash-root-keys.conf"
TMUX = shutil.which("tmux")
COLS, ROWS = 120, 20
KEYS = ["h", "l", "H", "Enter", "m", "a", "Escape", "q", "C-c", "Space"]
TYPED = {"h": b"h", "l": b"l", "H": b"H", "Enter": b"\r", "m": b"m", "a": b"a",
         "Escape": b"\x1b", "q": b"q", "C-c": b"\x03", "Space": b" "}


def stash_section():
    """The lines of tmux.conf.tmpl that make up prefix v's mode."""
    return [l for l in CONF.read_text().splitlines()
            if l.startswith(("set -g @stash_live ", "bind-key v ", "bind-key -T stash "))]


class Section(unittest.TestCase):
    def test_every_stash_key_has_a_root_twin_doing_the_same(self):
        table = {}
        for l in stash_section():
            m = re.match(r"^bind-key -T stash (\S+) +(.*)$", l)
            if m:
                table[m.group(1)] = m.group(2).replace(" \\; ", " ; ")
        root = {}
        for l in ROOT_CONF.read_text().splitlines():
            if l.startswith("#") or not l.strip():
                continue
            m = re.match(r'^bind-key -n (\S+) +if -F "#\{E:@stash_live\}" \{ (.*) \} \{ send-keys (\S+) \}$', l)
            self.assertTrue(m, l)                               # nothing else in the file
            self.assertEqual(m.group(3), m.group(1), l)         # the pass-through is the key itself
            root[m.group(1)] = m.group(2)
        self.assertEqual(sorted(table), sorted(KEYS))
        self.assertEqual(root, table)
        self.assertEqual(sum(1 for l in stash_section() if l.startswith("set -g @stash_live ")), 1)

    def test_root_keys_are_not_in_tmux_conf_and_the_unbind_list_matches(self):
        # Bound for good, a key missing in the PREFIX table falls back to them.
        self.assertFalse(re.search(r"(?m)^bind-key -n (" + "|".join(map(re.escape, KEYS)) + r") ",
                                   CONF.read_text()))
        m = re.search(r'(?m)^STASH_ROOT_KEYS="([^"]*)"$', STASH.read_text())
        self.assertEqual(m.group(1).split(), KEYS)


@unittest.skipUnless(TMUX, "tmux not installed")
class Keys(unittest.TestCase):
    def setUp(self):
        self.sock = "stash-keys-" + uuid.uuid4().hex[:12]
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="stash-keys-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        scripts = Path(self.tmp, ".config", "tmux", "scripts")
        scripts.mkdir(parents=True)
        # A wrapper, not a symlink: the chezmoi source copy is not executable.
        (scripts / "stash.sh").write_text(f'#!/bin/sh\nexec bash "{STASH}" "$@"\n')
        (scripts / "stash.sh").chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env.update(TERM="xterm-256color", HOME=self.tmp, TMPDIR=self.tmp + "/",
                        AGENT_FOCUS_NOTICE="", XDG_STATE_HOME=os.path.join(self.tmp, "state"),
                        NOTIFY_SH=os.path.join(self.tmp, "no-notify"))   # messages: display-message
        self.child = None
        self.path = None
        self.addCleanup(self.reap)
        self.t("new-session", "-d", "-s", "main", "-x", str(COLS), "-y", str(ROWS))
        self.path = self.t("display", "-p", "#{socket_path}")
        for opt, val in (("exit-empty", "off"), ("base-index", "1"), ("mouse", "on"),
                         ("focus-events", "on"), ("prefix", "C-s"), ("status-right", ""),
                         ("escape-time", "10")):
            self.t("set", "-g", opt, val)
        self.t("move-window", "-s", "main:0", "-t", "main:1")
        self.wins = [self.t("display", "-p", "-t", "main:1", "#{window_id}")]
        for _ in range(3):
            self.wins.append(self.t("new-window", "-d", "-t", "main:", "-P", "-F", "#{window_id}"))
        self.t("new-session", "-d", "-s", "other")
        conf = Path(self.tmp, "stash.conf")
        conf.write_text("\n".join(stash_section()) + "\n")
        self.t("source-file", str(conf))
        self.attach()

    # -- harness ---------------------------------------------------------
    def t(self, *args, check=True):
        r = subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", *args], env=self.env,
                           capture_output=True, text=True, timeout=10)
        if check and r.returncode:
            raise AssertionError(f"tmux {args}: {r.stderr}")
        return r.stdout.strip()

    def attach(self):
        pid, fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
                os.execvpe(TMUX, [TMUX, "-L", self.sock, "-f", "/dev/null", "attach", "-t", "main:1"], self.env)
            finally:
                os._exit(127)
        self.child, self.fd = pid, fd

        def drain():
            while True:
                try:
                    if not os.read(fd, 65536):
                        return
                except OSError:
                    return
        threading.Thread(target=drain, daemon=True).start()
        for _ in range(100):
            c = self.t("list-clients", "-F", "#{client_name}")
            if c:
                self.client = c.splitlines()[0]
                time.sleep(0.2)
                return
            time.sleep(0.05)
        self.fail("test client never attached")

    def reap(self):
        if self.child:
            try:
                os.kill(self.child, signal.SIGTERM)
                os.waitpid(self.child, 0)
            except OSError:
                pass
            try:
                os.close(self.fd)
            except OSError:
                pass
        subprocess.run([TMUX, "-L", self.sock, "kill-server"], env=self.env, capture_output=True, timeout=10)
        if self.path:
            Path(self.path).unlink(missing_ok=True)

    def wait(self, fn, want, timeout=5.0):
        deadline = time.time() + timeout
        got = fn()
        while got != want and time.time() < deadline:
            time.sleep(0.03)
            got = fn()
        self.assertEqual(got, want)
        return got

    def type(self, data, pause=0.15):
        os.write(self.fd, data)
        time.sleep(pause)

    def table(self):
        return self.t("list-clients", "-F", "#{client_key_table}")

    def root_bound(self):
        """Which of KEYS have a root binding, in KEYS order."""
        have = set(re.findall(r"(?m)^bind-key\s+-T root\s+(\S+)", self.t("list-keys", "-T", "root")))
        return [k for k in KEYS if k in have]

    def script(self, *args):
        r = subprocess.run(["bash", str(STASH), *args], env=dict(self.env, TMUX=f"{self.path},0,0"),
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def selected(self):
        return [w for w in self.t("list-windows", "-a", "-F", "#{?#{@stash_sel},#{window_id},}").split() if w]

    def recorder(self, target="main:1", prelude=""):
        out = os.path.join(self.tmp, "rec-" + uuid.uuid4().hex[:8])
        Path(out).touch()
        self.t("respawn-pane", "-k", "-t", target, f"{prelude}stty raw -echo; exec cat > {out}")
        time.sleep(0.4)
        return out

    def read(self, out, settle=0.4):
        time.sleep(settle)
        return Path(out).read_bytes()

    def start(self):
        """prefix v, typed, on main:1."""
        self.type(b"\x13")
        self.type(b"v")
        self.wait(self.selected, [self.wins[0]])
        self.wait(self.table, "stash")

    # -- pass-through ----------------------------------------------------
    def test_root_keys_pass_through_byte_exact_when_nothing_is_selected(self):
        def feed():
            out = self.recorder()
            for k in KEYS:                                   # typed, one at a time
                self.type(TYPED[k], 0.1)
            for k in KEYS:                                   # through the key tables
                self.t("send-keys", "-K", "-c", self.client, k)
            self.type(b"hello aqm\r", 0.3)                   # a burst, one write
            got = self.read(out)
            out = self.recorder(prelude="printf '\\033[?2004h'; ")
            self.type(b"\x1b[200~hl q\r\x1b[201~", 0.3)   # a bracketed paste, asked for
            return got, self.read(out)

        # Bound with nothing selected in this session: as while a selection
        # is up in another one.
        self.t("source-file", str(ROOT_CONF))
        self.assertEqual(self.root_bound(), KEYS)
        with_keys = feed()
        self.assertEqual(self.table(), "root")
        self.assertEqual(self.selected(), [])
        for k in KEYS:
            self.t("unbind-key", "-n", k)
        without = feed()
        self.assertEqual(with_keys, without)
        one = b"".join(TYPED[k] for k in KEYS)
        self.assertEqual(with_keys[0], one + one + b"hello aqm\r")
        self.assertEqual(with_keys[1], b"\x1b[200~hl q\r\x1b[201~")

    def test_a_mode_without_a_key_table_still_gets_its_keys(self):
        # choose-tree reads keys through the mode's own callback, not a table,
        # so root is consulted first: q must still close it.
        self.type(b"\x13")
        self.type(b"s")
        self.wait(lambda: self.t("display", "-p", "-t", "main:1", "#{pane_in_mode}"), "1")
        self.type(b"q")
        self.wait(lambda: self.t("display", "-p", "-t", "main:1", "#{pane_in_mode}"), "0")

    def test_a_selection_in_another_session_leaves_these_keys_alone(self):
        other = self.t("display", "-p", "-t", "other:", "#{window_id}")
        self.script("sel-start", "here", other)
        self.assertEqual(self.selected(), [other])
        self.assertEqual(self.root_bound(), KEYS)
        out = self.recorder()
        for k in ("h", "l", "Escape", "Space"):
            self.type(TYPED[k], 0.1)
        self.assertEqual(self.read(out), b"hl\x1b ")
        self.assertEqual(self.table(), "root")
        self.assertEqual(self.selected(), [other])

    # -- the root keys exist only while a selection does -------------------
    def test_root_keys_exist_only_while_a_selection_does(self):
        self.assertEqual(self.root_bound(), [])                       # at rest
        endings = [
            ("Esc", lambda: self.type(b"\x1b", 0.3)),
            ("prefix v", lambda: (self.type(b"\x13"), self.type(b"v"))),
            ("H parks", lambda: self.type(b"H")),
            ("prefix H parks one", lambda: (self.type(b"\x13"), self.type(b"H"))),
        ]
        for name, end in endings:
            self.t("select-window", "-t", "main:1")
            wid = self.t("display", "-p", "-t", "main:1", "#{window_id}")
            self.start_on(wid)
            self.assertEqual(self.root_bound(), KEYS, name)
            self.assertEqual(self.t("show", "-gqv", "@stash_root_bound"), "1")
            end()
            self.wait(self.root_bound, [])
            self.wait(self.selected, [])
            self.assertEqual(self.t("show", "-gqv", "@stash_root_bound"), "", name)
            if "parks" in name:                               # let the park land before the next start
                self.wait(lambda: wid in self.t("list-windows", "-t", "stash", "-F", "#{window_id}",
                                                check=False).split(), True)
                time.sleep(0.5)
        # sel-send's refusal (a tasks window) ends it too.
        self.t("new-session", "-d", "-s", "tasks")
        self.t("select-window", "-t", "main:1")
        self.start_on(self.t("display", "-p", "-t", "main:1", "#{window_id}"))
        self.script("sel-send", self.t("display", "-p", "-t", "tasks:", "#{window_id}"), self.client)
        self.assertEqual((self.root_bound(), self.selected()), ([], []))
        # ...and so does backing out of its picker.
        if shutil.which("fzf"):
            self.start_on(self.t("display", "-p", "-t", "main:1", "#{window_id}"))
            self.type(b"m", 1.5)                              # the picker is up,
            self.assertEqual(len(self.selected()), 1)          # the tint with it
            self.type(b"\x1b", 0.5)                            # fzf's Esc: send-pick cancels
            self.wait(self.root_bound, [])
            self.wait(self.selected, [])

    def test_a_key_missing_in_the_prefix_table_is_still_swallowed(self):
        # The review's catch: with the root keys bound for good, C-s Esc
        # reached the pane as \x1b (interrupting a Claude turn).
        # 0.8 s after each: on a bare 3.7c server with no binding of any of
        # these (measured), a C-s typed within ~0.5 s of C-s Esc is lost most
        # of the time, so the key after it reaches the pane — tmux's own
        # quirk, not these keys'.
        out = self.recorder()
        for key in (b"\r", b"\x03", b"\x1b"):
            self.type(b"\x13")
            self.type(key, 0.8)
        self.start()                                          # a selection,
        self.type(b"q", 0.3)                                  # ended
        self.wait(self.root_bound, [])
        for key in (b"\r", b"\x03", b"\x1b"):
            self.type(b"\x13")
            self.type(key, 0.8)
        self.assertEqual(self.read(out), b"")

    # -- the mode holds --------------------------------------------------
    def test_mode_survives_being_knocked_out_of_the_table(self):
        self.start()
        knocks = [
            ("unbound key", lambda: self.type(b"j")),
            ("click in the pane", lambda: self.type(b"\x1b[<0;10;5M\x1b[<0;10;5m")),
            ("focus out", lambda: self.type(b"\x1b[O")),
            ("focus in", lambda: self.type(b"\x1b[I")),
            ("another session and back", lambda: (
                self.t("switch-client", "-c", self.client, "-t", "other"),
                self.t("switch-client", "-c", self.client, "-t", "main:1"))),
        ]
        want = 1
        for name, knock in knocks:
            self.assertEqual(self.table(), "stash", name)
            knock()
            self.wait(self.table, "root")                    # what Mack hit
            self.type(b"l")
            want = min(want + 1, len(self.wins))
            self.wait(lambda: len(self.selected()), want)
            self.wait(self.table, "stash")                   # and l re-armed it
        self.type(b"h")                                      # h shrinks, from the table
        self.wait(lambda: len(self.selected()), want - 1)
        # Knocked out once more, H parks the lot from root.
        self.type(b"\x1b[O\x1b[I")
        self.wait(self.table, "root")
        picked = self.selected()
        self.type(b"H")
        self.wait(lambda: sorted(self.t("list-windows", "-t", "stash", "-F", "#{window_id}", check=False).split()),
                  sorted(picked))
        self.assertEqual(self.selected(), [])
        self.assertEqual(self.t("show", "-gqv", "@stash_anchor"), "")

    def test_escape_from_root_cancels_and_the_next_escape_is_the_panes(self):
        out = self.recorder("main:2")
        self.t("select-window", "-t", "main:2")
        self.start_on(self.wins[1])
        self.type(b"\x1b[<0;10;5M\x1b[<0;10;5m")             # a click: back to root
        self.wait(self.table, "root")
        self.type(b"\x1b", 0.3)
        self.wait(self.selected, [])
        self.assertEqual(self.t("show", "-gqv", "@stash_anchor"), "")
        self.type(b"\x1b", 0.3)
        self.type(b"q")
        self.assertEqual(self.read(out), b"\x1bq")             # only the keys after the cancel

    def start_on(self, wid):
        self.type(b"\x13")
        self.type(b"v")
        self.wait(self.selected, [wid])

    # -- prefix v toggles ------------------------------------------------
    def test_prefix_v_toggles(self):
        self.start()
        self.type(b"l")
        self.wait(lambda: len(self.selected()), 2)
        self.type(b"\x13")                                    # prefix from INSIDE the table
        self.wait(self.table, "prefix")
        self.type(b"v")
        self.wait(self.selected, [])
        self.assertEqual(self.t("show", "-gqv", "@stash_anchor"), "")
        self.wait(self.table, "root")
        self.start()                                          # and on again
        self.type(b"\x1b[O")                                  # knocked out: still toggles off
        self.wait(self.table, "root")
        self.type(b"\x13")
        self.type(b"v")
        self.wait(self.selected, [])


if __name__ == "__main__":
    unittest.main()
