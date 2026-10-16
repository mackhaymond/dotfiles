"""scripts/notify.sh: the CLI (-c / -k / -t / --), sanitising, the toast vs
display-message decision, the @toast / @toast_seq transport, the notice and
the bounded log.

Every test drives the real script against a REAL private tmux server
(`tmux -L <unique> -f /dev/null` under a private TMUX_TMPDIR, $TMUX unset,
killed and removed in tearDown) through NOTIFY_TMUX, which points at a thin
wrapper that logs each call's argv and execs the real tmux on that server.
`notifyutil` is a stub on PATH that logs its argv; the notice name is a
test-only one (AGENT_FOCUS_NOTICE). The log goes to NOTIFY_LOG in the
scratch dir. Clients, when a test needs one, are nested `tmux attach`es in a
pane of the same private server, so nothing ever reaches a real client.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

SCRIPTS = Path(os.environ.get(
    "AGENT_TMUX_SCRIPTS",
    str(Path.home() / ".local/share/chezmoi/dot_config/tmux/scripts")))
NOTIFY = SCRIPTS / "executable_notify.sh"
REAL_TMUX = shutil.which("tmux")
ZSH = "/bin/zsh"

FAKE_NOTIFYUTIL = """#!/bin/sh
printf '%s\\n' "$*" >> "$NOTICE_LOG"
"""

# Logs each call (one line, args joined by a unit separator), then runs it.
FAKE_TMUX = """#!/bin/sh
{ for a in "$@"; do printf '%%s\\037' "$a"; done; printf '\\n'; } >> "$TMUX_CALLS"
exec %s -L %s -f /dev/null "$@"
"""


def write_exe(path, body):
    path.write_text(body)
    path.chmod(0o755)


@unittest.skipUnless(REAL_TMUX and os.path.exists(ZSH), "tmux and zsh required")
class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="notify-test-")
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.sock = "notify-test-" + uuid.uuid4().hex[:8]
        self.calls = self.root / "calls"
        self.notices = self.root / "notices"
        self.log = self.root / "state" / "log"
        self.notice = "com.example.test.notify." + uuid.uuid4().hex[:8]
        write_exe(self.bin / "notifyutil", FAKE_NOTIFYUTIL)
        self.fake_tmux = self.root / "tmux"
        write_exe(self.fake_tmux, FAKE_TMUX % (REAL_TMUX, self.sock))
        # Sockets in a short private dir (a socket path must fit in 104 bytes).
        self.sockdir = Path(tempfile.mkdtemp(prefix="notify-", dir="/tmp"))
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env.update({
            "TMUX_TMPDIR": str(self.sockdir),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "NOTIFY_TMUX": str(self.fake_tmux),
            "NOTIFY_LOG": str(self.log),
            "AGENT_FOCUS_NOTICE": self.notice,
            "NOTICE_LOG": str(self.notices),
            "TMUX_CALLS": str(self.calls),
            "HOME": str(self.root / "home"),
        })
        self.tmux("new-session", "-d", "-s", "main", "-x", "80", "-y", "24", "sleep 600")

    def tearDown(self):
        subprocess.run([REAL_TMUX, "-L", self.sock, "-f", "/dev/null", "kill-server"],
                       env=self.env, capture_output=True)
        shutil.rmtree(self.sockdir, ignore_errors=True)
        self.tmp.cleanup()

    # -- helpers ----------------------------------------------------------

    def tmux(self, *args):
        out = subprocess.run([REAL_TMUX, "-L", self.sock, "-f", "/dev/null", *args],
                             env=self.env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, f"tmux {args}: {out.stderr}")
        return out.stdout

    def opt(self, name):
        return self.tmux("show-options", "-gqv", name).rstrip("\n")

    def notify(self, *args, env=None):
        """Run the script → its argv'd tmux calls (each a list)."""
        if self.calls.exists():
            self.calls.unlink()
        e = dict(self.env, **(env or {}))
        out = subprocess.run([ZSH, "-f", str(NOTIFY), *args], env=e,
                             capture_output=True, text=True, timeout=20)
        self.assertEqual((out.returncode, out.stdout, out.stderr), (0, "", ""))
        if not self.calls.exists():
            return []
        return [l.split("\x1f")[:-1] for l in self.calls.read_text().splitlines()]

    def toast(self):
        raw = self.opt("@toast")
        return raw.split("\t") if raw else None

    def log_lines(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def notices_posted(self, wait=2.0):
        t = time.time() + wait
        while time.time() < t:
            if self.notices.exists() and self.notices.read_text():
                return self.notices.read_text().splitlines()
            time.sleep(0.02)
        return []

    def open_sidebar(self, clients="", pids=None):
        """What a sidebar's registry writes: the pids (default: this live
        test process, once per client word) and their clients."""
        if pids is None:
            pids = [os.getpid()] * max(1, len(clients.split()))
        self.tmux("set-option", "-g", "@sidebar_pids", " ".join(map(str, pids)))
        self.tmux("set-option", "-g", "@sidebar_open", str(len(pids)))
        if clients:
            self.tmux("set-option", "-g", "@sidebar_clients", clients)

    @staticmethod
    def dead_pid():
        p = subprocess.Popen(["/usr/bin/true"])
        p.wait()
        return p.pid

    def attach(self, session):
        """A nested client attached to `session` → its tty."""
        if subprocess.run([REAL_TMUX, "-L", self.sock, "-f", "/dev/null", "has-session", "-t", session],
                          env=self.env, capture_output=True).returncode != 0:
            self.tmux("new-session", "-d", "-s", session, "sleep 600")
        cmd = f"env -u TMUX -u TMUX_PANE TMUX_TMPDIR={self.sockdir} {REAL_TMUX} -L {self.sock} -f /dev/null attach -t {session}"
        self.tmux("new-window", "-d", "-t", "main:", cmd)
        t = time.time() + 10
        while time.time() < t:
            for line in self.tmux("list-clients", "-F", "#{client_name} #{session_name}").splitlines():
                name, _, sess = line.partition(" ")
                if sess == session:
                    return name
            time.sleep(0.02)
        self.fail("no nested client")

    def said(self, client, text):
        return any(f"{client} message: " in l and l.endswith(text)
                   for l in self.tmux("show-messages").splitlines())

    # -- the decision -----------------------------------------------------

    def test_no_sidebar_is_tmuxs_own_message(self):
        calls = self.notify("-k", "ok", "hello")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][:2], ["display-message", "-p"])
        self.assertEqual(calls[1], ["display-message", "--", "hello"])
        self.assertIsNone(self.toast())
        self.assertEqual(self.notices_posted(wait=0.3), [])

    def test_open_sidebar_without_a_client_toasts_everywhere(self):
        self.open_sidebar()
        before = int(time.time() * 1000)
        calls = self.notify("-k", "ok", "--", "parked", "Canvas Syllabus")
        after = int(time.time() * 1000)
        self.assertEqual(len(calls), 2, calls)     # one read, one write
        self.assertEqual(calls[1][:4], ["set-option", "-gq", "@toast_seq", "1"])
        self.assertEqual(calls[1][4:7], [";", "set-option", "-gq"])
        seq, ms, kind, ttl, client, text = self.toast()
        self.assertEqual((seq, kind, ttl, client, text), ("1", "ok", "4", "", "parked Canvas Syllabus"))
        self.assertTrue(before <= int(ms) <= after, (before, ms, after))
        self.assertEqual(self.opt("@toast_seq"), "1")
        self.assertEqual(self.notices_posted(), [f"-p {self.notice}"])
        # No display-message at all.
        self.assertFalse(any(c[0] == "display-message" and "-p" not in c for c in calls))

    def test_seq_increments_and_newer_replaces(self):
        self.open_sidebar()
        self.tmux("set-option", "-g", "@toast_seq", "41")
        self.notify("one")
        self.assertEqual(self.toast()[0], "42")
        self.notify("two")
        self.assertEqual((self.toast()[0], self.toast()[5], self.opt("@toast_seq")), ("43", "two", "43"))

    def test_a_client_with_a_sidebar_gets_the_toast(self):
        tty = self.attach("work")
        self.open_sidebar(f"- {tty}")
        self.notify("-c", tty, "-k", "warn", "#{session_name} saved")
        seq, _, kind, ttl, client, text = self.toast()
        # The text expanded in that client's context, as display-message would.
        self.assertEqual((kind, ttl, client, text), ("warn", "4", tty, "work saved"))
        self.assertFalse(self.said(tty, "saved"))

    def test_a_client_without_a_sidebar_gets_display_message(self):
        tty = self.attach("work")
        other = self.attach("zeta")
        self.open_sidebar(other)
        calls = self.notify("-c", tty, "for you")
        self.assertEqual(calls[-1], ["display-message", "-c", tty, "--", "for you"])
        self.assertIsNone(self.toast())
        t = time.time() + 5
        while not self.said(tty, "for you"):
            self.assertLess(time.time(), t)
            time.sleep(0.02)

    def test_a_crashed_sidebar_is_never_toasted(self):
        """A SIGKILLed sidebar stays listed until a repair: its pid is dead,
        so the message goes to tmux, never into a toast nobody draws."""
        tty = self.attach("work")
        dead = self.dead_pid()
        self.open_sidebar(f"- {tty}", pids=[os.getpid(), dead])
        calls = self.notify("-c", tty, "after the crash")
        self.assertIsNone(self.toast())
        self.assertEqual(calls[-1], ["display-message", "-c", tty, "--", "after the crash"])
        # No client named: needs at least one LIVE sidebar.
        self.tmux("set-option", "-g", "@sidebar_clients", "-")
        self.tmux("set-option", "-g", "@sidebar_pids", str(dead))
        self.tmux("detach-client", "-t", tty)
        t = time.time() + 5
        while self.tmux("list-clients").strip():
            self.assertLess(time.time(), t)
            time.sleep(0.02)
        calls = self.notify("nobody alive")
        self.assertEqual((self.toast(), calls[-1]), (None, ["display-message", "--", "nobody alive"]))
        self.open_sidebar(pids=[dead, os.getpid()])
        self.notify("one alive")
        self.assertEqual(self.toast()[5], "one alive")
        # Its client's pid alive again: the toast.
        tty = self.attach("work")
        self.open_sidebar(f"- {tty}", pids=[dead, os.getpid()])
        self.notify("-c", tty, "alive")
        self.assertEqual(self.toast()[4:], [tty, "alive"])

    def test_a_trailing_semicolon_shows_as_display_message_would(self):
        """tmux reads an argument ending in `;` as a separator and one ending
        in `\\;` as a literal `;`: the toast keeps what display-message shows."""
        self.open_sidebar()
        for arg, shown in [("saved\\;", "saved;"), ("done;", "done"), ("x\\\\;", "x\\;"), ("a \\; b", None), ("a;b", "a;b")]:
            want = self.tmux("display-message", "-p", "--", arg).rstrip("\n")
            if shown is not None:
                self.assertEqual(want, shown, f"tmux itself: {arg!r}")
            self.notify(arg)
            self.assertEqual(self.toast()[5], want, arg)

    def test_no_c_uses_the_calling_contexts_client(self):
        tty = self.attach("work")
        self.open_sidebar(tty)
        self.notify("hi")
        self.assertEqual(self.toast()[4], tty)

    def test_an_unknown_client_never_toasts(self):
        self.open_sidebar()
        calls = self.notify("-c", "/dev/nonexistent", "lost")
        self.assertIsNone(self.toast())
        self.assertEqual(calls[-1], ["display-message", "-c", "/dev/nonexistent", "--", "lost"])

    # -- the CLI ----------------------------------------------------------

    def test_kinds_and_ttls(self):
        self.open_sidebar()
        for args, kind, ttl in [
            (["-k", "error", "x"], "error", "8"),
            (["-k", "error", "-t", "2", "x"], "error", "2"),
            (["-t", "2.5", "x"], "info", "2.5"),
            (["-t", "junk", "x"], "info", "4"),
            (["-t", "0", "x"], "info", "4"),
            (["-k", "nope", "x"], "info", "4"),
            (["x"], "info", "4"),
        ]:
            self.notify(*args)
            self.assertEqual(self.toast()[2:4], [kind, ttl], args)

    def test_text_is_one_sanitised_line(self):
        self.open_sidebar()
        self.notify("a\tb", "c\nd\re")
        self.assertEqual(self.toast()[5], "a b c d e")
        # `--` lets a message start with a dash; options after the text are text.
        self.notify("--", "-k", "ok")
        self.assertEqual(self.toast()[2::3], ["info", "-k ok"])
        self.notify("-c")
        self.assertEqual(self.toast()[5], "-k ok", "a lone -c with nothing to say: nothing")

    def test_empty_text_shows_nothing(self):
        self.open_sidebar()
        for args in [[], ["--"], ["", " "], ["\t\n"], ["-k", "ok"]]:
            self.assertEqual(self.notify(*args), [], args)
        self.assertIsNone(self.toast())
        self.assertEqual(self.log_lines(), [])

    def test_an_empty_notice_name_posts_nothing(self):
        self.open_sidebar()
        self.notify("quiet", env={"AGENT_FOCUS_NOTICE": ""})
        self.assertEqual(self.toast()[5], "quiet")
        self.assertEqual(self.notices_posted(wait=0.3), [])

    def test_no_server_is_still_quiet(self):
        self.notify("nobody home", env={"NOTIFY_TMUX": f"{REAL_TMUX} -L {self.sock}-none -f /dev/null"})
        self.assertEqual(len(self.log_lines()), 1)

    # -- the log ----------------------------------------------------------

    def test_every_message_is_logged(self):
        tty = self.attach("work")
        self.notify("-k", "warn", "-c", tty, "first")
        self.open_sidebar()
        self.notify("second")
        lines = [l.split("\t") for l in self.log_lines()]
        self.assertEqual([l[1:] for l in lines], [["warn", tty, "first"], ["info", tty, "second"]])
        self.assertTrue(all(abs(int(l[0]) - time.time()) < 60 for l in lines))

    def test_the_log_is_bounded(self):
        self.log.parent.mkdir(parents=True)
        self.log.write_text("".join(f"0\tinfo\t\told {i}\n" for i in range(999)))
        self.notify("at the limit")
        self.assertEqual(len(self.log_lines()), 1000)
        self.notify("past it")
        lines = self.log_lines()
        self.assertEqual(len(lines), 500)
        self.assertTrue(lines[-1].endswith("\tpast it") and lines[-2].endswith("\tat the limit"))
        self.assertEqual(lines[0], "0\tinfo\t\told 501")


if __name__ == "__main__":
    unittest.main()
