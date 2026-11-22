"""review.sh close-space (docs/review.md "Closing an emptied space"): the
review popup's y closes a space only when every tab it just took out has
left the session and no other agent is there; otherwise a warn toast and
nothing closes.

One private tmux server (`-L`, a private TMUX_TMPDIR, `-f /dev/null`),
killed in tearDownClass. The script runs with $TMUX naming that server, so
its plain `tmux` calls reach it and nothing else. @space_sh and @notify_sh
are stubs that log their arguments.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid

HERE = Path(__file__).resolve().parent.parent
REVIEW = HERE / "scripts" / "executable_review.sh"


class CloseSpace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="u2r", dir="/tmp")
        cls.name = f"review-{uuid.uuid4().hex[:8]}"
        cls.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        cls.env["TMUX_TMPDIR"] = cls.tmp
        cls.t("-f", "/dev/null", "new-session", "-d", "-s", "main", "sleep 600")
        cls.t("set", "-g", "default-command", "exec sleep 600")
        cls.sock = cls.t("display", "-p", "#{socket_path}")
        cls.log = os.path.join(cls.tmp, "log")
        for stub, tag in (("space", "SPACE"), ("notify", "NOTIFY")):
            p = os.path.join(cls.tmp, stub)
            Path(p).write_text(f'#!/bin/sh\nprintf "{tag} %s\\n" "$*" >> "{cls.log}"\n')
            os.chmod(p, 0o755)
        cls.t("set", "-g", "@space_sh", os.path.join(cls.tmp, "space"))
        cls.t("set", "-g", "@notify_sh", os.path.join(cls.tmp, "notify"))

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["tmux", "-L", cls.name, "kill-server"], env=cls.env, capture_output=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def t(cls, *args):
        o = subprocess.run(["tmux", "-L", cls.name, *args], env=cls.env, capture_output=True, text=True)
        assert o.returncode == 0, (args, o.stderr)
        return o.stdout.strip()

    def setUp(self):
        Path(self.log).write_text("")
        self.space = f"s{uuid.uuid4().hex[:6]}"
        self.sid = self.t("new-session", "-d", "-P", "-F", "#{session_id}", "-s", self.space)

    def tearDown(self):
        subprocess.run(["tmux", "-L", self.name, "kill-session", "-t", self.sid], env=self.env, capture_output=True)

    def win(self, agent=False):
        w = self.t("new-window", "-d", "-P", "-F", "#{window_id}", "-t", f"{self.sid}:")
        if agent:
            self.t("set", "-w", "-t", w, "@agent_state", "idle")
        return w

    def close_space(self, *wins):
        env = dict(self.env, TMUX=f"{self.sock},0,0", REVIEW_CLOSE_WAIT="3")
        subprocess.run(["bash", str(REVIEW), "close-space", self.sid, "/dev/ttys999", *wins], env=env, check=True,
                       timeout=20)
        return Path(self.log).read_text()

    def test_closes_when_the_tabs_left(self):
        gone = self.win(agent=True)
        self.win()  # a shell stays: fine
        self.t("kill-window", "-t", gone)
        log = self.close_space(gone)
        self.assertEqual(log, f"SPACE close {self.sid} /dev/ttys999\n")

    def test_a_tab_that_stayed_aborts(self):
        stayed = self.win(agent=True)  # its park was refused: still here
        log = self.close_space(stayed)
        self.assertNotIn("SPACE", log)
        self.assertIn(f"NOTIFY -k warn -c /dev/ttys999 -- review: a tab stayed in {self.space} ({stayed}) · space not closed",
                      log)

    def test_an_agent_back_in_the_space_aborts(self):
        gone = self.win(agent=True)
        self.win(agent=True)  # started while the question was up
        self.t("kill-window", "-t", gone)
        log = self.close_space(gone)
        self.assertNotIn("SPACE", log)
        self.assertIn(f"review: {self.space} has an agent again · space not closed", log)

    def test_a_space_gone_meanwhile_is_nothing(self):
        sid = self.sid
        self.t("kill-session", "-t", sid)
        self.assertEqual(self.close_space("@999"), "")


if __name__ == "__main__":
    unittest.main()
