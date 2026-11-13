"""The seam between stash.sh and closed-tabs.sh, with nothing stubbed.

Both REAL source copies run against each other on a PRIVATE tmux server: a
`tmux` on PATH that pins `-L closed-seam-<uuid> -f /dev/null` (the scripts call
plain `tmux`, and commands the server runs inherit that PATH), its own short
TMUX_TMPDIR, killed in teardown. HOME, XDG_STATE_HOME and TMPDIR are the
test's temp dir; STASH_SELF / STASH_SH / CLOSED_TABS_SH point each script at
the other's source copy. Ported from the orchestrator's itest.sh probe.

What it holds the two units to is the interface only (plan: "Interfaces"):
parking a foreground job freezes it (state T, @stash_frozen) and an idle shell
is `idle`; `stash.sh rows` feeds `closed-tabs.sh pick-rows`; `publish-count`
counts live stash windows plus pinned entries; the picker's ⏎ on a window row
thaws it back onto the bar, its ⌃x discards it through `kill-many` → `close`
into the backlog (unpinned, under its origin); reopening a pinned entry takes
it out of the history and republishes the count. stash.sh is being changed
alongside this, so nothing here checks its wording, only behaviour.
"""
import json
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
CLOSED = HERE / "scripts" / "executable_closed-tabs.sh"
STASH = HERE / "scripts" / "executable_stash.sh"
TMUX = shutil.which("tmux")
ZSH = shutil.which("zsh") or "/bin/zsh"
ANSI = re.compile(r"\x1b\[[0-9;:?]*[A-Za-z]")


@unittest.skipUnless(TMUX and shutil.which("jq") and os.path.exists(ZSH), "tmux / jq / zsh not installed")
class Seam(unittest.TestCase):
    def setUp(self):
        self.sock = "closed-seam-" + uuid.uuid4().hex[:10]
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="closed-seam-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        sockdir = tempfile.mkdtemp(prefix="t.", dir="/tmp")           # short: sun_path
        self.addCleanup(shutil.rmtree, sockdir, True)
        bin_ = Path(self.tmp, "bin")
        bin_.mkdir()
        (bin_ / "tmux").write_text(f'#!/bin/sh\nexec "{TMUX}" -L {self.sock} -f /dev/null "$@"\n')
        (bin_ / "stash-self").write_text(f'#!/bin/sh\nexec bash "{STASH}" "$@"\n')
        (bin_ / "ai-exec").write_text("#!/bin/sh\nexit 1\n")         # describe never leaves
        for f in bin_.iterdir():
            f.chmod(0o755)
        self.hist = Path(self.tmp, "state", "tmux-closed-tabs", "history.jsonl")
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env.update(PATH=f"{bin_}:{self.env.get('PATH', '/usr/bin:/bin')}", HOME=self.tmp,
                        XDG_STATE_HOME=os.path.join(self.tmp, "state"), TMPDIR=self.tmp + "/",
                        TMUX_TMPDIR=sockdir, TERM="xterm-256color", SHELL=ZSH,
                        NOTIFY_SH=os.path.join(self.tmp, "no-notify"), NOTIFY_TMUX="true",
                        STASH_SELF=str(bin_ / "stash-self"), STASH_SH=str(STASH), CLOSED_TABS_SH=str(CLOSED))
        # The wrapper by its path: never a bare `tmux kill-server`.
        self.tmux = str(bin_ / "tmux")
        self.addCleanup(lambda: subprocess.run([self.tmux, "kill-server"], env=self.env,
                                               capture_output=True, timeout=10))
        self.t("new-session", "-d", "-s", "main", "-x", "200", "-y", "50", f"{ZSH} -f")
        for opt, val in (("exit-empty", "off"), ("default-command", f"{ZSH} -f"),
                         ("base-index", "1"), ("renumber-windows", "on")):
            self.t("set", "-g", opt, val)

    # -- harness ---------------------------------------------------------
    def t(self, *args, check=True):
        r = subprocess.run([self.tmux, *args], env=self.env, capture_output=True, text=True, timeout=10)
        if check:
            self.assertEqual(r.returncode, 0, (args, r.stderr))
        return r.stdout.strip()

    def run_(self, script, *args):
        return subprocess.run(["bash", str(script), *args], env=self.env, capture_output=True,
                              text=True, timeout=60)

    def stash(self, *args):
        return self.run_(STASH, *args)

    def closed(self, *args):
        r = self.run_(CLOSED, *args)
        self.assertEqual(r.returncode, 0, (args, r.stdout, r.stderr))
        return r.stdout

    def wait(self, f, timeout=8.0, what=""):
        end = time.time() + timeout
        while True:
            v = f()
            if v:
                return v
            if time.time() > end:
                self.fail(f"timed out waiting for {what or f}")
            time.sleep(0.1)

    def window(self, cmd=None):
        w = self.t("new-window", "-d", "-P", "-F", "#{window_id}", "-t", "=main:", "-c", "/tmp")
        self.wait(lambda: self.t("display", "-p", "-t", w, "#{pane_current_command}") == "zsh", what="a prompt")
        if cmd:
            self.t("send-keys", "-t", w, cmd, "Enter")
            self.wait(lambda: self.t("display", "-p", "-t", w, "#{pane_current_command}") == cmd.split()[0],
                      what=f"{cmd} running")
        return w

    def session_of(self, w):
        return self.t("display", "-p", "-t", w, "#{session_name}", check=False)

    def job_state(self, w, needle="sleep 1000"):
        tty = self.t("display", "-p", "-t", w, "#{pane_tty}").replace("/dev/", "")
        out = subprocess.run(["ps", "-t", tty, "-o", "stat=,args="], capture_output=True, text=True).stdout
        return next((l.split()[0] for l in out.splitlines() if needle in l), "")

    def rows(self, *args):
        return {(l.split("\t")[0]): ANSI.sub("", l) for l in self.closed("pick-rows", *args).splitlines()}

    def stash_rows(self):
        out = self.stash("rows").stdout
        return {f[0]: f for f in (l.split("\x1f") for l in out.splitlines()) if len(f) >= 6}

    def count(self):
        v = self.t("show", "-gqv", "@stash_count")
        return int(v) if v.isdigit() else None

    def entries(self):
        if not self.hist.exists():
            return []
        return [json.loads(l) for l in self.hist.read_text().splitlines() if l.strip()]

    def pin_fixture(self):
        now = int(time.time())
        self.hist.parent.mkdir(parents=True, exist_ok=True)
        e = {"id": f"{now}-1", "ts": now, "session": "main", "index": 9, "cwd": "/tmp", "label": "zsh",
             "tool": "", "session_id": "", "cmd": "", "pinned": True, "stashed_ts": now}
        with self.hist.open("a") as f:
            f.write(json.dumps(e) + "\n")
        return e["id"]

    # -- tests -----------------------------------------------------------
    def test_park_rows_thaw_and_discard(self):
        busy, idle = self.window("sleep 1000"), self.window()
        for w in (busy, idle):
            r = self.stash("stash", w)
            self.assertEqual(r.returncode, 0, (w, r.stdout, r.stderr))
            self.wait(lambda: self.session_of(w) == "stash", what=f"{w} parked")
        # Frozen: the job is stopped, and the window says so.
        self.assertTrue(self.t("show", "-wqv", "-t", busy, "@stash_frozen"))
        self.wait(lambda: "T" in self.job_state(busy), what="sleep stopped")
        # rows → the merged picker.
        rows = self.stash_rows()
        self.assertEqual(rows[busy][4], "frozen")
        self.assertEqual(rows[idle][4], "idle")
        self.assertEqual(rows[busy][1], "main")                         # origin
        picker = self.rows("--stashed")
        self.assertIn("frozen", picker[f"w:{busy}"])
        self.assertIn("idle", picker[f"w:{idle}"])
        # The count: two live windows and a pinned entry.
        self.pin_fixture()
        self.stash("publish-count")
        self.assertEqual(self.count(), 3)
        # ⏎ on the frozen row: back on the bar, running again.
        self.closed("pick-act", "", "", "", f"w:{busy}")
        self.wait(lambda: self.session_of(busy) == "main", what="unstashed")
        self.wait(lambda: (lambda s: s and "T" not in s)(self.job_state(busy)), what="sleep running again")
        # ⌃x on the idle row: closed through closed-tabs into the backlog,
        # unpinned, under the space it came from.
        self.closed("pick-act", "ctrl-x", "", "", f"w:{idle}")
        self.wait(lambda: self.session_of(idle) == "", what="discarded")
        back = self.wait(lambda: [e for e in self.entries() if not e.get("pinned")], what="a backlog entry")
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0]["session"], "main")
        self.wait(lambda: self.count() == 1, what="count 1 (the pinned entry)")

    def test_reopening_a_pinned_entry_leaves_the_shelf(self):
        pid = self.pin_fixture()
        self.stash("publish-count")
        self.assertEqual(self.count(), 1)
        self.assertIn(f"p:{pid}", self.rows("--stashed"))
        before = len(self.t("list-windows", "-t", "=main", "-F", "#{window_id}").split())
        self.closed("pick-act", "", "", "", f"p:{pid}")
        self.assertEqual(self.entries(), [])
        self.assertEqual(len(self.t("list-windows", "-t", "=main", "-F", "#{window_id}").split()), before + 1)
        self.wait(lambda: self.count() == 0, what="count republished")

    def test_a_legacy_parked_window_is_a_window_row(self):
        # A window parked the old way (an agent suspended, @stash_session on
        # it): listed by rows, shown by the picker as a parked window row.
        w = self.window()
        self.t("set", "-w", "-t", w, "@stash_session", "11111111-2222-3333-4444-555555555555")
        self.t("set", "-w", "-t", w, "@stash_origin", "main")
        self.t("set", "-w", "-t", w, "@stash_label", "Old agent")
        self.t("new-session", "-d", "-s", "stash", f"{ZSH} -f")
        self.t("move-window", "-s", w, "-t", "=stash:")
        self.assertIn(w, self.stash_rows())
        row = self.rows("--stashed").get(f"w:{w}", "")
        self.assertIn("Old agent", row)
        self.assertIn("from main", row)


if __name__ == "__main__":
    unittest.main()
