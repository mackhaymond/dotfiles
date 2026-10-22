"""stash.sh: the 2026-10-22 review fixes, against a real tmux.

  - an unpark whose origin is gone never lands in a hidden session (agents,
    pending, ...) — the client's own session first, then an attached visible
    one; from outside tmux the bare display-message answered tmux's best
    session, which was `agents` whenever tmux-pty-mcp had just opened a window;
  - suspend_agent's final "still parked?" test and the SIGTERM happen under
    the stash lock, AFTER the 0.6s snapshot — an unpark during the snapshot no
    longer gets its agent killed;
  - a pending resume (home already, @stash_session still set) is reachable
    while other tabs are parked: unstash-many resumes it, the picker lists it;
  - a space's home tab is never parked or sent;
  - the send target is refused for every hidden session, not a fixed few.

Real tmux on a private `-L` socket started with `-f /dev/null`; HOME, TMPDIR
and XDG_STATE_HOME are a temp dir, NOTIFY_SH a missing file (messages go to
display-message), and STASH_SELF a recorder, so every `run-shell -b "'$SELF'
resume <win>"` the script hands off is written to a file instead of resuming
anything. The server is killed and its socket removed in cleanup.
STASH_UNDER_TEST runs another copy of the script (to check a test fails on the
old code). Run with unittest or pytest.
"""
import fcntl
import os
from pathlib import Path
import pty
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
STASH = Path(os.environ.get("STASH_UNDER_TEST", HERE / "scripts" / "executable_stash.sh"))
TMUX = shutil.which("tmux")
SID = "5f0c1d2e-aaaa-4bbb-8ccc-0123456789ab"


@unittest.skipUnless(TMUX, "tmux not installed")
class Base(unittest.TestCase):
    def setUp(self):
        self.sock = "stash-fix-" + uuid.uuid4().hex[:12]
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="stash-fix-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls = os.path.join(self.tmp, "self-calls")
        rec = Path(self.tmp, "self-recorder")
        rec.write_text(f'#!/bin/sh\necho "$*" >> "{self.calls}"\n')
        rec.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env.update(TERM="xterm-256color", HOME=self.tmp, TMPDIR=self.tmp + "/",
                        AGENT_FOCUS_NOTICE="", XDG_STATE_HOME=os.path.join(self.tmp, "state"),
                        NOTIFY_SH=os.path.join(self.tmp, "no-notify"), STASH_SELF=str(rec))
        self.child = None
        self.path = None
        self.addCleanup(self.reap)
        self.t("new-session", "-d", "-s", "main", "-x", "120", "-y", "30")
        self.path = self.t("display", "-p", "#{socket_path}")
        self.t("set", "-g", "exit-empty", "off")
        self.t("set", "-g", "base-index", "1")

    # -- harness ---------------------------------------------------------
    def t(self, *args, check=True):
        r = subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", *args], env=self.env,
                           capture_output=True, text=True, timeout=10)
        if check and r.returncode:
            raise AssertionError(f"tmux {args}: {r.stderr}")
        return r.stdout.strip()

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

    def script_env(self):
        return dict(self.env, TMUX=f"{self.path},0,0")

    def script(self, *args):
        r = subprocess.run(["bash", str(STASH), *args], env=self.script_env(),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def source(self, body, timeout=30):
        """Run bash with the script's functions loaded (its `count` verb runs
        on the way in, harmlessly), then `body`."""
        r = subprocess.run(["bash", "-c", f'source "$1" count >/dev/null\n{body}', "_", str(STASH)],
                           env=self.script_env(), capture_output=True, text=True, timeout=timeout)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def attach(self, session):
        pid, fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
                os.execvpe(TMUX, [TMUX, "-L", self.sock, "-f", "/dev/null", "attach", "-t", session], self.env)
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
                time.sleep(0.2)
                return c.splitlines()[0]
            time.sleep(0.05)
        self.fail("test client never attached")

    def new_window(self, session):
        return self.t("new-window", "-d", "-t", f"{session}:", "-P", "-F", "#{window_id}")

    def first_window(self, session):
        return self.t("list-windows", "-t", session, "-F", "#{window_id}").split()[0]

    def session_of(self, win):
        return self.t("display", "-p", "-t", win, "#{session_name}")

    def resumed(self, want, timeout=5.0):
        deadline = time.time() + timeout
        got = []
        while time.time() < deadline:
            try:
                got = Path(self.calls).read_text().split("\n")
            except FileNotFoundError:
                got = []
            got = sorted(l for l in got if l)
            if got == sorted(want):
                break
            time.sleep(0.05)
        self.assertEqual(got, sorted(want))

    def park_by_hand(self, win, origin=""):
        """A parked window, the way a park leaves it (origin optional)."""
        if subprocess.run([TMUX, "-L", self.sock, "has-session", "-t", "=stash"],
                          env=self.env, capture_output=True).returncode:
            boot = self.t("new-session", "-d", "-s", "stash", "-P", "-F", "#{window_id}")
            self.t("move-window", "-s", win, "-t", "stash:")
            self.t("kill-window", "-t", boot)
        else:
            self.t("move-window", "-s", win, "-t", "stash:")
        if origin:
            self.t("set", "-w", "-t", win, "@stash_origin", origin)


class OriginGone(Base):
    """Bug 1: the fallback for a parked tab whose origin session was closed."""

    def setUp(self):
        super().setUp()
        self.new_window("main")
        self.w = self.new_window("main")
        self.park_by_hand(self.w, origin="spaceX")        # spaceX: closed since
        # tmux-pty-mcp opens a window in `agents` — now the newest session.
        time.sleep(1.1)
        self.t("new-session", "-d", "-s", "agents")

    def test_from_outside_tmux_it_never_lands_in_agents(self):
        self.script("unstash-many", self.w)
        self.assertEqual(self.session_of(self.w), "main")

    def test_with_a_client_it_lands_in_the_clients_session(self):
        self.t("new-session", "-d", "-s", "work")
        client = self.attach("work")
        time.sleep(1.1)
        self.t("new-session", "-d", "-s", "pending")       # newest again, and hidden
        self.script("unstash-many", "-c", client, self.w)
        self.assertEqual(self.session_of(self.w), "work")
        self.assertEqual(self.t("list-clients", "-F", "#{client_session}"), "work")

    def test_a_hidden_client_session_is_not_used_either(self):
        client = self.attach("agents")
        self.script("unstash-many", "-c", client, self.w)
        self.assertEqual(self.session_of(self.w), "main")

    def test_with_no_visible_session_it_stays_parked(self):
        self.t("kill-session", "-t", "=main")
        self.script("unstash-many", self.w)
        self.assertEqual(self.session_of(self.w), "stash")


class PendingResume(Base):
    """Bug 4: a pending resume while another tab is parked."""

    def setUp(self):
        super().setUp()
        self.new_window("main")
        self.parked = self.new_window("main")
        self.park_by_hand(self.parked, origin="main")
        self.pending = self.new_window("main")
        self.t("set", "-w", "-t", self.pending, "@stash_session", SID)
        self.t("set", "-w", "-t", self.pending, "@stash_cwd", self.tmp)

    def test_unstash_many_resumes_it_while_something_else_is_parked(self):
        self.script("unstash-many", self.pending)
        self.resumed([f"resume {self.pending}"])
        self.assertEqual(self.session_of(self.parked), "stash")       # untouched

    def test_picked_together_both_come_back(self):
        self.script("unstash-many", self.parked, self.pending)
        self.resumed([f"resume {self.parked}", f"resume {self.pending}"])
        self.assertEqual(self.session_of(self.parked), "main")

    def test_unstash_with_the_id_resumes_it_with_nothing_parked(self):
        self.script("unstash-many", self.parked)
        self.resumed([f"resume {self.parked}"])
        Path(self.calls).unlink(missing_ok=True)
        self.script("unstash", self.pending)
        self.resumed([f"resume {self.pending}"])

    def test_the_picker_lists_it(self):
        rows = self.source("pick_rows 0").splitlines()
        ids = [r.split("\t")[0] for r in rows]
        self.assertEqual(ids, [self.parked, self.pending])
        self.assertIn("resume pending", rows[1])


class SuspendRace(Base):
    """Bug 2: an unpark during the snapshot must not get the agent killed."""

    def setUp(self):
        super().setUp()
        self.new_window("main")
        fake = Path(self.tmp, "claude")
        shutil.copy("/bin/sleep", fake)
        self.w = self.t("new-window", "-d", "-t", "main:", "-P", "-F", "#{window_id}",
                        f"exec '{fake}' 300")
        # Kept after the kill, so the window's options can still be read.
        self.t("set", "-w", "-t", self.w, "remain-on-exit", "on")
        self.pane = self.t("display", "-p", "-t", self.w, "#{pane_id}")
        self.pid = int(self.t("display", "-p", "-t", self.w, "#{pane_pid}"))
        for _ in range(50):
            if "claude" in subprocess.run(["ps", "-p", str(self.pid), "-o", "comm="],
                                          capture_output=True, text=True).stdout:
                break
            time.sleep(0.05)
        self.park_by_hand(self.w, origin="main")

    def suspend(self, snapshot):
        S = "\x1f"
        return self.source(f"""
TERM_WAIT=2
live_sessions() {{ printf '%s{S}%s{S}idle{S}%s{S}%s{S}%s\\n' {self.pid} {SID} '{self.w}' '{self.pane}' '{self.tmp}'; }}
pid_in_window() {{ return 0; }}
is_working() {{ return 1; }}
has_unsent_input() {{ return 1; }}
session_has_running_workflow() {{ return 1; }}
session_has_running_subagent() {{ return 1; }}
has_live_cua() {{ return 1; }}
save_state() {{ :; }}
snapshot_pane() {{ {snapshot}; }}
suspend_agent '{self.w}'
""")

    def alive(self):
        return subprocess.run(["kill", "-0", str(self.pid)], capture_output=True).returncode == 0

    def test_unparked_during_the_snapshot_is_left_running(self):
        # prefix+h lands while the snapshot sleeps: the window goes home.
        self.suspend("tmux move-window -s \"$1\" -t main:")
        time.sleep(0.3)
        self.assertTrue(self.alive())
        self.assertEqual(self.t("show", "-wqv", "-t", self.w, "@stash_session"), "")

    def test_still_parked_it_is_suspended(self):
        self.suspend(":")
        for _ in range(60):
            if not self.alive():
                break
            time.sleep(0.05)
        self.assertFalse(self.alive())
        self.assertEqual(self.t("show", "-wqv", "-t", self.w, "@stash_session"), SID)

    def test_the_kill_waits_for_a_held_lock_and_rechecks(self):
        # An unstash holding the lock while it moves the window home: the
        # suspend waits for it, then sees the window has gone.
        lock = os.path.join(self.tmp, f"tmux-stash.{os.getuid()}.lock")
        snap = (f"mkdir '{lock}'; "
                f"( sleep 0.5; tmux move-window -s \"$1\" -t main:; rm -rf '{lock}' ) & "
                f"echo $! > '{lock}/pid'")
        self.suspend(snap)
        time.sleep(0.3)
        self.assertTrue(self.alive())
        self.assertEqual(self.session_of(self.w), "main")


class HomeTab(Base):
    """Bug 5: a space's home is never parked or sent."""

    def setUp(self):
        super().setUp()
        self.home = self.first_window("main")
        self.t("set", "-w", "-t", self.home, "@space_home", "1")
        self.a = self.new_window("main")
        self.b = self.new_window("main")

    def test_park_refuses_the_home(self):
        self.script("stash-many", self.home)
        self.assertEqual(self.session_of(self.home), "main")

    def test_a_range_with_the_home_parks_the_rest(self):
        self.script("stash-many", self.home, self.a)
        self.assertEqual(self.session_of(self.home), "main")
        self.assertEqual(self.session_of(self.a), "stash")

    def test_send_leaves_the_home(self):
        self.script("send-many", "", "stay", "other", self.home, self.a)
        self.assertEqual(self.session_of(self.home), "main")
        self.assertEqual(self.session_of(self.a), "other")


class HiddenTargets(Base):
    """Bug 6: no hidden session is a send target."""

    def setUp(self):
        super().setUp()
        self.new_window("main")
        self.a = self.new_window("main")

    def test_pending_and_btop_popup_are_refused(self):
        for target in ("pending", "btop-popup"):
            self.script("send-many", "", "stay", target, self.a)
            self.assertEqual(self.session_of(self.a), "main", target)
            self.assertNotEqual(
                subprocess.run([TMUX, "-L", self.sock, "has-session", "-t", f"={target}"],
                               env=self.env, capture_output=True).returncode, 0, target)

    def test_the_configured_list_and_the_session_flag_count(self):
        self.t("set", "-g", "@space_hidden_sessions", "agents tasks stash scratch btop-popup pending lab")
        self.t("new-session", "-d", "-s", "flagged")
        self.t("set", "-t", "flagged", "@space_hidden", "1")
        for target in ("lab", "flagged"):
            self.script("send-many", "", "stay", target, self.a)
            self.assertEqual(self.session_of(self.a), "main", target)
        hidden = self.source("hidden_sessions").split()
        self.assertIn("lab", hidden)
        self.assertIn("flagged", hidden)
        self.assertNotIn("main", hidden)

    def test_a_visible_session_still_works(self):
        self.script("send-many", "", "stay", "work", self.a)
        self.assertEqual(self.session_of(self.a), "work")

    def test_the_picker_lists_no_hidden_session(self):
        # A stand-in fzf records the list it is handed and then acts like Esc
        # (130), so send-pick cancels without moving anything.
        offered = os.path.join(self.tmp, "offered")
        bindir = Path(self.tmp, "bin")
        bindir.mkdir()
        fake = bindir / "fzf"
        fake.write_text(f'#!/bin/sh\ncat > "{offered}"\nexit 130\n')
        fake.chmod(0o755)
        self.env["PATH"] = f"{bindir}:{self.env.get('PATH', '')}"
        for s in ("pending", "btop-popup", "agents", "work", "flagged"):
            self.t("new-session", "-d", "-s", s)
        self.t("set", "-t", "flagged", "@space_hidden", "1")
        self.script("send-pick", "", self.a)
        listed = Path(offered).read_text().split()
        self.assertIn("work", listed)
        for s in ("pending", "btop-popup", "agents", "flagged", "main"):
            self.assertNotIn(s, listed)
        self.assertEqual(self.session_of(self.a), "main")


class HiddenConfigured(Base):
    """The real server sets @space_hidden_sessions (tmux.conf) to the same
    names as the built-in floor. `hidden_sessions | grep -q` under pipefail
    then said "not hidden" for every floor name: grep -q quit on the first
    match and the printf loop died of SIGPIPE (141)."""

    def setUp(self):
        super().setUp()
        self.t("set", "-g", "@space_hidden_sessions", "agents tasks stash scratch btop-popup pending")
        self.new_window("main")
        self.a = self.new_window("main")

    def test_every_floor_name_is_hidden(self):
        out = self.source("for s in stash agents tasks scratch btop-popup pending main; do "
                          "is_hidden_session \"$s\"; echo \"$s $?\"; done")
        got = dict(l.split() for l in out.splitlines())
        for s in ("stash", "agents", "tasks", "scratch", "btop-popup", "pending"):
            self.assertEqual(got[s], "0", s)
        self.assertEqual(got["main"], "1")

    def test_send_to_a_floor_name_is_refused(self):
        for target in ("pending", "btop-popup", "agents", "scratch"):
            self.script("send-many", "", "stay", target, self.a)
            self.assertEqual(self.session_of(self.a), "main", target)
            self.assertNotEqual(
                subprocess.run([TMUX, "-L", self.sock, "has-session", "-t", f"={target}"],
                               env=self.env, capture_output=True).returncode, 0, target)

    def test_the_unpark_fallback_never_picks_agents(self):
        self.park_by_hand(self.a, origin="gone")
        time.sleep(1.1)
        self.t("new-session", "-d", "-s", "agents")
        self.t("set", "-t", "agents", "@space_hidden", "1")
        self.script("unstash-many", self.a)
        self.assertEqual(self.session_of(self.a), "main")


if __name__ == "__main__":
    unittest.main()
