"""NAV (docs/nav.md): scripts/nav.sh, Option-X back / Option-C forward.

Real tmux, never the live server: each test gets its own `-L` socket started
with `-f /dev/null`, sets what tmux.conf sets for spaces (@space_hidden_sessions
and the @space_hidden marker on hidden sessions), then loads the nav lines
(hooks and bindings) with the script path pointed at this source copy. The
lines come from tmux.conf.tmpl when it carries them, else from the block in
docs/nav.md, so the test always runs what tmux.conf will. Clients attach from
pseudo-terminals this process opens (pty.fork); keys go through the client's
key tables with `send-keys -K -c <client>`. Run with unittest or pytest.
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
DOC = HERE / "docs" / "nav.md"
SCRIPT = HERE / "scripts" / "executable_nav.sh"
LIVE_PATH = "#{HOME}/.config/tmux/scripts/nav.sh"
TMUX = shutil.which("tmux")
HIDDEN = "agents tasks stash scratch btop-popup"
COLS, ROWS = 120, 20


def nav_lines():
    """The tmux.conf lines that wire nav.sh in (hooks + bindings)."""
    keep = lambda l: "scripts/nav.sh" in l and l.startswith(("set-hook", "bind-key"))
    lines = [l for l in CONF.read_text().splitlines() if keep(l)]
    if not lines:
        block = re.search(r"```tmux\n(.*?)```", DOC.read_text(), re.S)
        lines = [l for l in block.group(1).splitlines() if keep(l)]
    return lines


class Nav(unittest.TestCase):
    def setUp(self):
        self.sock = "nav-test-" + uuid.uuid4().hex[:12]
        self.tmp = tempfile.mkdtemp(prefix="nav-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env["TERM"] = "xterm-256color"
        self.path = None
        self.children = []
        self.addCleanup(self.reap)
        self.t("new-session", "-d", "-s", "main", "-x", str(COLS), "-y", str(ROWS))
        self.path = self.t("display", "-p", "#{socket_path}")
        self.t("set", "-g", "exit-empty", "off")
        self.t("set", "-g", "@space_hidden_sessions", HIDDEN)
        self.t("new-session", "-d", "-s", "stash")
        self.t("set", "-t", "stash", "@space_hidden", "1")
        self.t("new-session", "-d", "-s", "other")
        conf = Path(self.tmp) / "nav.conf"
        conf.write_text("\n".join(l.replace(LIVE_PATH, f"bash {SCRIPT}") for l in nav_lines()) + "\n")
        self.t("source-file", str(conf))
        self.main = [self.t("display", "-p", "-t", "main", "#{window_id}")]
        self.main += [self.new_window("main") for _ in range(4)]
        self.other = [self.t("display", "-p", "-t", "other", "#{window_id}"), self.new_window("other")]
        self.sid = {s: self.t("display", "-p", "-t", s, "#{session_id}") for s in ("main", "other", "stash")}

    # -- harness ---------------------------------------------------------
    def t(self, *args, check=True):
        r = subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", *args], env=self.env,
                           capture_output=True, text=True, timeout=10)
        if check and r.returncode:
            raise AssertionError(f"tmux {args}: {r.stderr}")
        return r.stdout.strip()

    def new_window(self, session):
        return self.t("new-window", "-d", "-t", f"{session}:", "-P", "-F", "#{window_id}")

    def attach(self, session="main"):
        before = set(self.t("list-clients", "-F", "#{client_name}").split())
        pid, fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
                os.execvpe(TMUX, [TMUX, "-L", self.sock, "-f", "/dev/null", "attach", "-t", session], self.env)
            finally:
                os._exit(127)
        self.children.append((pid, fd))

        def drain():
            while True:
                try:
                    if not os.read(fd, 65536):
                        return
                except OSError:
                    return
        threading.Thread(target=drain, daemon=True).start()
        for _ in range(100):
            new = set(self.t("list-clients", "-F", "#{client_name}").split()) - before
            if new:
                client = new.pop()
                # the attach hook seeds the history with where the client is
                self.wait(lambda: self.hist(client)["cur"], self.on(client))
                return client
            time.sleep(0.05)
        self.fail("test client never attached")

    def reap(self):
        for pid, fd in self.children:
            try:
                os.kill(pid, signal.SIGTERM)
                os.waitpid(pid, 0)
            except OSError:
                pass
            try:
                os.close(fd)
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

    def on(self, client):
        """"<session_id>:<window_id>" the client is on (list-clients expands per client)."""
        rows = self.t("list-clients", "-F", "#{client_name}\t#{session_id}:#{window_id}").splitlines()
        return next(r.split("\t", 1)[1] for r in rows if r.startswith(client + "\t"))

    def e(self, session, win):
        return f"{self.sid[session]}:{win}"

    def hist(self, client):
        k = re.sub(r"[^A-Za-z0-9]", "_", client)
        cur, _, rest = self.t("show", "-gqv", f"@nav_{k}").partition("|")
        back, _, fwd = rest.partition("|")
        return {"cur": cur, "back": back.split(), "fwd": fwd.split()}

    def go(self, client, session, win):
        """A navigation that is not nav's own: what a click or agent-jump does."""
        self.t("select-window", "-t", f"{session}:{win}")
        if self.on(client).split(":")[0] != self.sid[session]:
            self.t("switch-client", "-c", client, "-t", session)
        self.wait(lambda: self.hist(client)["cur"], self.e(session, win))

    def key(self, client, k):
        self.t("send-keys", "-K", "-c", client, k)

    def back(self, client, session, win):
        self.key(client, "M-x")
        self.wait(lambda: self.on(client), self.e(session, win))

    def forward(self, client, session, win):
        self.key(client, "M-c")
        self.wait(lambda: self.on(client), self.e(session, win))

    def settle(self, client):
        """Let every queued record hook run; then the history must not move."""
        before = self.hist(client)
        time.sleep(0.6)
        self.assertEqual(self.hist(client), before)

    def messages(self):
        return self.t("show-messages")

    # -- tests -----------------------------------------------------------
    def test_back_and_forward_across_windows(self):
        c = self.attach()
        a, b, d = self.main[0], self.main[1], self.main[2]
        self.go(c, "main", b)
        self.go(c, "main", d)
        self.back(c, "main", b)
        self.back(c, "main", a)
        self.forward(c, "main", b)
        self.forward(c, "main", d)
        self.settle(c)
        self.assertEqual(self.hist(c), {"cur": self.e("main", d), "fwd": [],
                                        "back": [self.e("main", a), self.e("main", b)]})

    def test_back_and_forward_across_sessions(self):
        c = self.attach()
        self.go(c, "other", self.other[1])
        self.go(c, "main", self.main[3])
        self.back(c, "other", self.other[1])
        self.back(c, "main", self.main[0])
        self.forward(c, "other", self.other[1])
        self.forward(c, "main", self.main[3])

    def test_a_new_navigation_clears_forward(self):
        c = self.attach()
        self.go(c, "main", self.main[1])
        self.go(c, "main", self.main[2])
        self.back(c, "main", self.main[1])
        self.assertEqual(self.hist(c)["fwd"], [self.e("main", self.main[2])])
        self.go(c, "other", self.other[0])
        self.assertEqual(self.hist(c)["fwd"], [])
        self.key(c, "M-c")
        self.wait(lambda: "nothing to go forward to" in self.messages(), True)
        self.assertEqual(self.on(c), self.e("other", self.other[0]))
        self.back(c, "main", self.main[1])

    def test_back_is_not_recorded_as_a_navigation(self):
        c = self.attach()
        a, b, d = self.main[0], self.main[1], self.main[2]
        self.go(c, "main", b)
        self.go(c, "other", self.other[1])
        self.go(c, "main", d)
        self.back(c, "other", self.other[1])
        self.settle(c)                       # the move's own hooks have run
        self.assertEqual(self.hist(c), {"cur": self.e("other", self.other[1]),
                                        "back": [self.e("main", a), self.e("main", b)],
                                        "fwd": [self.e("main", d)]})
        self.back(c, "main", b)              # not back to d
        self.back(c, "main", a)

    def test_a_move_whose_hook_has_not_run_yet_is_caught_up(self):
        c = self.attach()
        self.go(c, "main", self.main[1])
        # select and back in ONE command list: the select's hook runs after M-x.
        self.t("select-window", "-t", f"main:{self.main[2]}", ";", "send-keys", "-K", "-c", c, "M-x")
        self.wait(lambda: self.on(c), self.e("main", self.main[1]))
        self.settle(c)
        self.assertEqual(self.hist(c)["fwd"], [self.e("main", self.main[2])])

    def test_a_burst_of_moves_and_keys_stays_consistent(self):
        c = self.attach()
        for i in range(12):
            self.t("select-window", "-t", self.main[i % 4 + 1])
            if i % 3 == 2:
                self.key(c, "M-x")
        time.sleep(1.0)
        h = self.hist(c)
        self.assertEqual(h["cur"], self.on(c))
        for seq in (h["back"], h["fwd"]):
            wins = [e.split(":")[1] for e in seq]
            self.assertFalse(any(a == b for a, b in zip(wins, wins[1:])), seq)
        self.back(c, *(("main", h["back"][-1].split(":")[1]) if h["back"] else ("main", h["cur"].split(":")[1])))

    def test_a_burst_of_back_presses_all_land(self):
        c = self.attach()
        for w in self.main[1:] + self.other:
            self.go(c, "other" if w in self.other else "main", w)
        # six presses at once: each its own run-shell, all racing for the CAS
        self.t("send-keys", "-K", "-c", c, *["M-x"] * 6)
        self.wait(lambda: self.on(c), self.e("main", self.main[0]), timeout=10)
        self.settle(c)
        self.assertEqual(self.hist(c)["back"], [])
        self.assertEqual(len(self.hist(c)["fwd"]), 6)

    def test_back_within_a_session_does_not_switch_client(self):
        c = self.attach()
        self.go(c, "main", self.main[1])
        self.go(c, "other", self.other[0])
        self.go(c, "main", self.main[2])
        self.t("set", "-g", "@csc", "0")
        self.t("set-hook", "-g", "client-session-changed[99]", "set -gF @csc '#{e|+:#{@csc},1}'")
        self.back(c, "other", self.other[0])            # across sessions: one switch
        self.wait(lambda: self.t("show", "-gv", "@csc"), "1")
        self.go(c, "main", self.main[3])                  # a switch of the test's own: 2
        self.wait(lambda: self.t("show", "-gv", "@csc"), "2")
        self.go(c, "main", self.main[4])
        self.back(c, "main", self.main[3])                # same session: no switch
        self.settle(c)
        self.assertEqual(self.t("show", "-gv", "@csc"), "2")

    def test_dead_and_hidden_entries_are_skipped(self):
        c = self.attach()
        a, dead, parked, d = self.main[0], self.main[1], self.main[2], self.main[3]
        for w in (dead, parked, d):
            self.go(c, "main", w)
        self.t("kill-window", "-t", dead)
        self.t("move-window", "-s", parked, "-t", "stash:")   # a park
        self.back(c, "main", a)
        self.settle(c)
        self.assertEqual(self.hist(c)["back"], [])
        self.assertEqual(self.hist(c)["fwd"], [self.e("main", d)])
        self.key(c, "M-x")
        self.wait(lambda: "nothing to go back to" in self.messages(), True)
        self.assertEqual(self.on(c), self.e("main", a))

    def test_a_window_moved_to_another_space_is_followed(self):
        c = self.attach()
        moved = self.main[1]
        self.go(c, "main", moved)
        self.go(c, "main", self.main[2])
        self.t("move-window", "-s", moved, "-t", "other:")
        self.back(c, "other", moved)

    def test_never_lands_in_a_hidden_session(self):
        c = self.attach()
        self.go(c, "main", self.main[1])
        self.t("switch-client", "-c", c, "-t", "stash")      # e.g. a stray switch
        self.settle(c)
        self.assertEqual(self.hist(c)["cur"], self.e("main", self.main[1]))   # not recorded
        # nor by the script itself, should a hook ever ask (the gate is in tmux.conf)
        subprocess.run(["bash", str(SCRIPT), "record", "client", c], env=dict(self.env, TMUX=f"{self.path},0,0"),
                       check=True, timeout=10)
        self.assertEqual(self.hist(c)["cur"], self.e("main", self.main[1]))
        self.back(c, "main", self.main[1])                   # back = out of there

    def test_two_clients_keep_independent_histories(self):
        c1 = self.attach("main")
        c2 = self.attach("other")
        self.go(c1, "main", self.main[2])
        self.go(c2, "other", self.other[1])
        self.go(c1, "main", self.main[3])
        self.back(c1, "main", self.main[2])
        self.assertEqual(self.on(c2), self.e("other", self.other[1]))
        self.back(c2, "other", self.other[0])
        self.assertEqual(self.on(c1), self.e("main", self.main[2]))
        self.back(c1, "main", self.main[0])
        self.assertEqual(self.hist(c2)["back"], [])

    def test_empty_history_says_so(self):
        c = self.attach()
        self.key(c, "M-x")
        self.wait(lambda: "nothing to go back to" in self.messages(), True)
        self.key(c, "M-c")
        self.wait(lambda: "nothing to go forward to" in self.messages(), True)
        self.assertEqual(self.on(c), self.e("main", self.main[0]))

    def test_consecutive_duplicates_collapse_and_history_is_capped(self):
        c = self.attach()
        k =re.sub(r"[^A-Za-z0-9]", "_", c)
        many = " ".join(f"{self.sid['main']}:{self.main[i % 2]}" for i in range(60))
        self.t("set", "-g", f"@nav_{k}", f"{self.e('main', self.main[0])}|{many}|")
        self.go(c, "main", self.main[4])
        self.go(c, "main", self.main[2])
        back = self.hist(c)["back"]
        self.assertEqual(len(back), 50)
        self.assertEqual(back[-2:], [self.e("main", self.main[0]), self.e("main", self.main[4])])

    def test_detach_forgets(self):
        c = self.attach()
        self.go(c, "main", self.main[1])
        pid, fd = self.children[-1]
        self.t("detach-client", "-t", c)
        k = re.sub(r"[^A-Za-z0-9]", "_", c)
        self.wait(lambda: self.t("show", "-gqv", f"@nav_{k}"), "")


@unittest.skipUnless(TMUX, "tmux not installed")
class Wiring(unittest.TestCase):
    def test_lines_found_and_slots_free(self):
        lines = nav_lines()
        hooks = [l for l in lines if l.startswith("set-hook")]
        binds = [l for l in lines if l.startswith("bind-key")]
        self.assertEqual(len(hooks), 4)
        self.assertEqual(sorted(re.search(r"bind-key -n (\S+)", b).group(1) for b in binds), ["M-c", "M-x"])
        ours = {re.search(r"'([a-z-]+\[\d+\])'", h).group(1) for h in hooks}
        text = CONF.read_text() + (HERE / "spaces.conf").read_text()
        theirs = {m for m in re.findall(r"set-hook -g '?([a-z-]+\[\d+\])", text)
                  if not any(m in l for l in lines)}
        self.assertFalse(ours & theirs, ours & theirs)


Nav = unittest.skipUnless(TMUX, "tmux not installed")(Nav)

if __name__ == "__main__":
    unittest.main()
