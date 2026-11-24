"""scripts/review-nudge.sh: the periodic "review your old agents" toast.

The rule: (agents idle 8h+ >= 3, or some visible window holds a 2-char
overflow key `;x`) AND no nudge in the last 4 hours AND a tmux server with a
client attached, with input in the last 5 minutes. The nudge is one info toast for every client (`notify.sh -c
'*'`) whose click action opens the review popup (`review.sh open {client}`).

Every test drives the real script under /bin/bash (3.2, as the LaunchAgent
runs it) and the real notify.sh against a REAL private tmux server (`-L
<unique> -f /dev/null` under a private TMUX_TMPDIR, $TMUX unset, killed in
tearDown). The old-agent count is a stub (REVIEW_COUNT_CMD); review.sh is a
path that never runs here (REVIEW_SH); "now" is fixed (REVIEW_NUDGE_NOW); the
state goes to a scratch XDG_STATE_HOME. A sidebar is faked as notify.sh's
tests fake it (@sidebar_pids = this live process), so the toast lands in
@toast where it can be read back; clients are nested `tmux attach`es in a
pane of the same private server.
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
NUDGE = SCRIPTS / "executable_review-nudge.sh"
NOTIFY = SCRIPTS / "executable_notify.sh"
REAL_TMUX = shutil.which("tmux")
BASH = "/bin/bash"
NOW = 1_800_000_000
REVIEW_SH = "/x/scripts/review.sh"


@unittest.skipUnless(REAL_TMUX and os.path.exists("/bin/zsh"), "tmux and zsh required")
class ReviewNudgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nudge-test-")
        self.root = Path(self.tmp.name)
        self.sock = "nudge-test-" + uuid.uuid4().hex[:8]
        self.sockdir = Path(tempfile.mkdtemp(prefix="nudge-", dir="/tmp"))
        self.state = self.root / "state"
        self.tmux_words = f"{REAL_TMUX} -L {self.sock} -f /dev/null"
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env.update({
            "TMUX_TMPDIR": str(self.sockdir),
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.root / "home"),
            "XDG_STATE_HOME": str(self.state),
            "REVIEW_NUDGE_NOW": str(NOW),
            "REVIEW_NUDGE_TMUX": self.tmux_words,
            "REVIEW_NUDGE_ACTIVE": "999999999",   # the clients' clock is real, NOW is not
            "NOTIFY_TMUX": self.tmux_words,
            "NOTIFY_SH": str(NOTIFY),
            "NOTIFY_LOG": str(self.root / "notify.log"),
            "AGENT_FOCUS_NOTICE": "",
            "REVIEW_SH": REVIEW_SH,
            "REVIEW_COUNT_CMD": "echo 0",
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

    def nudge(self, *args, old=0, env=None):
        e = dict(self.env, REVIEW_COUNT_CMD=f"echo {old}")
        e.update(env or {})
        out = subprocess.run([BASH, str(NUDGE), *(args or ("check",))], env=e,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual((out.returncode, out.stderr), (0, ""))
        return out.stdout

    def records(self):
        raw = self.tmux("show-options", "-gqv", "@toast").rstrip("\n")
        return [r.split("\t") for r in raw.split("\x1e")] if raw else []

    @property
    def last_file(self):
        return self.state / "agent-review" / "last-nudge"

    def attach(self, session="work"):
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

    def with_sidebar(self):
        """A client attached, and a live sidebar for it (so the toast is
        written to @toast)."""
        tty = self.attach()
        self.tmux("set-option", "-g", "@sidebar_pids", str(os.getpid()))
        self.tmux("set-option", "-g", "@sidebar_clients", tty)
        return tty

    def assert_nudged(self, text):
        recs = self.records()
        self.assertEqual(len(recs), 1, recs)
        _seq, _ms, kind, ttl, client, shown, title, action = recs[0]
        self.assertEqual((kind, ttl, client, shown, title), ("info", "300", "", text, "Review agents"))
        self.assertEqual(action, f"'{REVIEW_SH}' open {{client}}")
        self.assertEqual(self.last_file.read_text().strip(), str(NOW))

    def assert_quiet(self):
        self.assertEqual(self.records(), [])
        self.assertFalse(self.last_file.exists())

    # -- the rule -----------------------------------------------------------

    def test_below_the_threshold_no_toast(self):
        self.with_sidebar()
        for old in (0, 2):
            self.nudge(old=old)
            self.assert_quiet()
        # Junk from the count is 0.
        self.nudge(env={"REVIEW_COUNT_CMD": "echo lots; exit 3"})
        self.assert_quiet()

    def test_three_old_agents_toast_with_the_review_action(self):
        self.with_sidebar()
        self.nudge(old=9)
        self.assert_nudged("9 agents idle 8h+ · ⌃S O or click to review")
        log = (self.state / "agent-review" / "log").read_text()
        self.assertIn("nudged: 9 idle 8h+, 0/26 letters held, 0 overflow keys", log)

    def test_only_letter_holders_of_visible_sessions_count(self):
        """The letters held: @agent_key a-z of windows in visible sessions
        that are not homes (the format tmux evaluates; the 26-letter cases
        run on the fake tmux below, so as not to need 26 ptys)."""
        tty = self.with_sidebar()          # session "work"; main:1 runs the attach
        held = lambda: self.nudge("--dry-run", old=0)
        self.tmux("set-option", "-w", "-t", "main:0", "@agent_key", "a")
        self.tmux("set-option", "-w", "-t", "main:1", "@agent_key", "b")
        self.tmux("set-option", "-w", "-t", "main:1", "@space_home", "1")
        self.tmux("set-option", "-w", "-t", "work:0", "@agent_key", "c")
        self.tmux("set-option", "-g", "@space_hidden_sessions", "stash work")
        self.assertEqual(held(), "skip: 0 idle 8h+ (needs 3), 1/26 letters held\n")
        # Not a home any more: counted.
        self.tmux("set-option", "-wu", "-t", "main:1", "@space_home")
        self.assertEqual(held(), "skip: 0 idle 8h+ (needs 3), 2/26 letters held\n")
        # Hidden by its own @space_hidden: still not counted; then visible.
        self.tmux("set-option", "-gu", "@space_hidden_sessions")
        self.tmux("set-option", "-t", "work", "@space_hidden", "1")
        self.assertEqual(held(), "skip: 0 idle 8h+ (needs 3), 2/26 letters held\n")
        self.tmux("set-option", "-t", "work", "-u", "@space_hidden")
        self.assertEqual(held(), "skip: 0 idle 8h+ (needs 3), 3/26 letters held\n")
        # An overflow key alone is no trigger.
        self.tmux("set-option", "-w", "-t", "work:0", "@agent_key", ";a")
        self.nudge(old=0)
        self.assert_quiet()
        self.assertTrue(tty)

    def test_within_four_hours_of_the_last_nudge_nothing(self):
        self.with_sidebar()
        self.last_file.parent.mkdir(parents=True)
        self.last_file.write_text(f"{NOW - 4 * 3600 + 60}\n")
        self.nudge(old=9)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.last_file.read_text().strip(), str(NOW - 4 * 3600 + 60))
        out = self.nudge("check", "--dry-run", old=9)
        self.assertEqual(out, "skip: nudged 239m ago (one per 4h)\n")
        # Four hours on: again.
        self.last_file.write_text(f"{NOW - 4 * 3600}\n")
        self.nudge(old=9)
        self.assert_nudged("9 agents idle 8h+ · ⌃S O or click to review")
        # And now, straight after, nothing more.
        self.tmux("set-option", "-gu", "@toast")
        self.nudge(old=9)
        self.assertEqual(self.records(), [])

    def test_a_junk_or_future_stamp_does_not_hold_it_back(self):
        self.with_sidebar()
        self.last_file.parent.mkdir(parents=True)
        # (A leading 0 is decimal, never "value too great for base".)
        for stamp in ("junk", str(NOW + 3600), "0999"):
            self.tmux("set-option", "-gu", "@toast")
            self.last_file.write_text(stamp + "\n")
            self.nudge(old=5)
            self.assertEqual(len(self.records()), 1, stamp)

    def test_no_client_attached_nothing(self):
        # A sidebar registered, but no client: no toast, no stamp.
        self.tmux("set-option", "-g", "@sidebar_pids", str(os.getpid()))
        self.nudge(old=9)
        self.assert_quiet()
        self.assertEqual(self.nudge("--dry-run", old=9), "skip: no client attached\n")

    def test_nobody_at_the_terminal_nothing(self):
        """No client input lately: no toast, and the 4 hours do not start;
        back at it, the next tick nudges."""
        tty = self.with_sidebar()
        act = int(self.tmux("display-message", "-p", "-c", tty, "#{client_activity}"))
        away = {"REVIEW_NUDGE_ACTIVE": "300", "REVIEW_NUDGE_NOW": str(act + 60 * 60)}
        self.assertEqual(self.nudge("--dry-run", old=9, env=away), "skip: no input for 60m\n")
        self.nudge(old=9, env=away)
        self.assert_quiet()
        self.nudge(old=9, env=dict(away, REVIEW_NUDGE_NOW=str(act + 299)))
        self.assertEqual(len(self.records()), 1)

    def test_no_server_nothing(self):
        self.tmux("kill-server")
        self.assertEqual(self.nudge("--dry-run", old=9), "skip: no tmux server\n")
        self.nudge(old=9)
        self.assertFalse(self.last_file.exists())

    def test_dry_run_posts_and_writes_nothing(self):
        self.with_sidebar()
        out = self.nudge("check", "--dry-run", old=7)
        self.assertEqual(out.splitlines(), [
            "nudge: 7 idle 8h+, 0/26 letters held, 0 overflow keys → would toast: 7 agents idle 8h+ · ⌃S O or click to review",
            f"action: '{REVIEW_SH}' open {{client}}",
        ])
        self.assert_quiet()
        self.assertFalse((self.state / "agent-review" / "log").exists())
        self.assertEqual(self.nudge("--dry-run", old=1), "skip: 1 idle 8h+ (needs 3), 0/26 letters held\n")

    def test_a_client_without_a_sidebar_gets_the_text(self):
        """No sidebar: tmux's own message on every client (no click there)."""
        tty = self.attach()
        self.nudge(old=3)
        self.assertEqual(self.records(), [])
        self.assertTrue(self.last_file.exists())
        t = time.time() + 5
        while not any(f"{tty} message: " in l and l.endswith("3 agents idle 8h+ · ⌃S O or click to review")
                      for l in self.tmux("show-messages").splitlines()):
            self.assertLess(time.time(), t)
            time.sleep(0.02)

    def test_a_hash_in_the_review_path_survives_run_shell(self):
        self.with_sidebar()
        self.nudge(old=3, env={"REVIEW_SH": "/x/a#b/review.sh"})
        self.assertEqual(self.records()[0][7], "'/x/a##b/review.sh' open {client}")

    def test_the_default_count_runs_review_sh_count(self):
        """Without REVIEW_COUNT_CMD: `'<review.sh>' count`."""
        self.with_sidebar()
        fake = self.root / "review.sh"
        fake.write_text("#!/bin/sh\n[ \"$1\" = count ] && echo 6\n")
        fake.chmod(0o755)
        e = {k: v for k, v in self.env.items() if k != "REVIEW_COUNT_CMD"}
        e["REVIEW_SH"] = str(fake)
        out = subprocess.run([BASH, str(NUDGE), "--dry-run"], env=e, capture_output=True, text=True, timeout=30)
        self.assertTrue(out.stdout.startswith("nudge: 6 idle 8h+"), out)


# A tmux that answers list-clients with one client and list-windows from a
# file (rows as the script's format renders them: hidden US home US key);
# a notify.sh that logs its argv. No server, no ptys.
FAKE_TMUX = """#!/bin/sh
case "$1" in
    list-clients) echo "$REVIEW_NUDGE_NOW" ;;
    list-windows) cat "$ROWS" ;;
    *) exit 1 ;;
esac
"""
FAKE_NOTIFY = """#!/bin/sh
{ for a in "$@"; do printf '%s\\037' "$a"; done; printf '\\n'; } >> "$NOTIFY_CALLS"
"""
LETTERS = "abcdefghijklmnopqrstuvwxyz"


class LettersRanOutTests(unittest.TestCase):
    """The letters trigger: all 26 single letters held by letter-holders
    (visible, non-home windows), whatever overflow keys exist."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nudge-letters-")
        self.root = Path(self.tmp.name)
        for name, body in (("tmux", FAKE_TMUX), ("notify.sh", FAKE_NOTIFY)):
            (self.root / name).write_text(body)
            (self.root / name).chmod(0o755)
        self.rows = self.root / "rows"
        self.calls = self.root / "calls"
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env.update({
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.root / "home"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "REVIEW_NUDGE_NOW": str(NOW),
            "REVIEW_NUDGE_TMUX": str(self.root / "tmux"),
            "NOTIFY_SH": str(self.root / "notify.sh"),
            "NOTIFY_CALLS": str(self.calls),
            "REVIEW_SH": REVIEW_SH,
            "ROWS": str(self.rows),
        })

    def tearDown(self):
        self.tmp.cleanup()

    def windows(self, *rows):
        """Each row (key, hidden=0, home="")."""
        lines = []
        for r in rows:
            key, hidden, home = (r + (0, ""))[:3] if isinstance(r, tuple) else (r, 0, "")
            lines.append(f"{hidden}\x1f{home}\x1f{key}")
        self.rows.write_text("\n".join(lines) + "\n")

    def nudge(self, old=0):
        """→ the toast's text, or None (and no stamp) when quiet."""
        if self.calls.exists():
            self.calls.unlink()
        e = dict(self.env, REVIEW_COUNT_CMD=f"echo {old}")
        out = subprocess.run([BASH, str(NUDGE), "check"], env=e, capture_output=True, text=True, timeout=30)
        self.assertEqual((out.returncode, out.stdout, out.stderr), (0, "", ""))
        stamp = self.root / "state" / "agent-review" / "last-nudge"
        if not self.calls.exists():
            self.assertFalse(stamp.exists())
            return None
        stamp.unlink()
        argv = self.calls.read_text().split("\n")[0].split("\x1f")[:-1]
        self.assertEqual(argv[:11], ["-c", "*", "-k", "info", "-t", "300", "-m", "30", "-T", "Review agents", "-a"])
        self.assertEqual(argv[11:13], [f"'{REVIEW_SH}' open {{client}}", "--"])
        return argv[13]

    def test_all_26_held_is_out_of_letters(self):
        self.windows(*LETTERS)
        self.assertEqual(self.nudge(), "letters ran out · ⌃S O or click to review")
        self.windows(*LETTERS, ";a", ";b")
        self.assertEqual(self.nudge(old=1), "letters ran out (2 on ; keys) · ⌃S O or click to review")
        self.assertEqual(self.nudge(old=4), "4 agents idle 8h+ · letters ran out · ⌃S O or click to review")

    def test_an_overflow_key_with_a_free_letter_is_no_trigger(self):
        """Overflow keys are kept for life: once a letter frees up, a `;x`
        window alone never nudges (duplicates count once)."""
        self.windows(*LETTERS[:-1], "a", "b", ";a", ";b", ";c")
        self.assertIsNone(self.nudge(old=2))
        # The 26th only on a home, or in a hidden session: still free.
        self.windows(*LETTERS[:-1], ("z", 0, "1"), ("z", 1, ""), ";a")
        self.assertIsNone(self.nudge(old=2))
        # Junk keys are no letters.
        self.windows(*LETTERS[:-1], "Z", "zz", ";", "")
        self.assertIsNone(self.nudge())
        # ... and the old-agent trigger still works on its own.
        self.assertEqual(self.nudge(old=3), "3 agents idle 8h+ · ⌃S O or click to review")


if __name__ == "__main__":
    unittest.main()
