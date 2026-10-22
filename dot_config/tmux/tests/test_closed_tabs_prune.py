"""closed-tabs.sh: prune spares the snapshot of a close still in flight.

do_close writes previews/<id>.ansi first and its history row only after the
agent lookup, the kill and agent_session; closing a space runs every pane's
close at once, so the first close to reach prune deleted every other one's
screen (2026-10-22). An unreferenced snapshot younger than PREVIEW_GRACE is
now left alone; an old one is still an orphan and goes.

Runs `pick-popup` (prune, then the popup) with XDG_STATE_HOME in a temp dir and
a `tmux` on PATH that does nothing, so no server is reached and no real
history is touched. CLOSED_TABS_UNDER_TEST runs another copy of the script.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get("CLOSED_TABS_UNDER_TEST", HERE / "scripts" / "executable_closed-tabs.sh"))


@unittest.skipUnless(shutil.which("jq"), "jq not installed")
class Prune(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="closed-prune-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        bin_ = Path(self.tmp, "bin")
        bin_.mkdir()
        (bin_ / "tmux").write_text("#!/bin/sh\nexit 0\n")
        (bin_ / "tmux").chmod(0o755)
        self.state = Path(self.tmp, "state", "tmux-closed-tabs")
        self.previews = self.state / "previews"
        self.previews.mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env.update(HOME=self.tmp, XDG_STATE_HOME=os.path.join(self.tmp, "state"),
                        PATH=f"{bin_}:{self.env.get('PATH', '/usr/bin:/bin')}",
                        NOTIFY_SH=os.path.join(self.tmp, "no-notify"), NOTIFY_TMUX="true")

    def preview(self, id_, age):
        p = self.previews / f"{id_}.ansi"
        p.write_text("screen\n")
        t = time.time() - age
        os.utime(p, (t, t))
        return p

    def prune(self):
        r = subprocess.run(["bash", str(SCRIPT), "pick-popup", "main", ""], env=self.env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_an_in_flight_close_keeps_its_snapshot(self):
        now = int(time.time())
        (self.state / "history.jsonl").write_text(
            json.dumps({"id": f"{now}-1", "ts": now, "label": "done"}) + "\n")
        kept = self.preview(f"{now}-1", 5)          # referenced
        flying = self.preview(f"{now}-2", 1)        # its row is not written yet
        orphan = self.preview(f"{now - 3600}-3", 3600)   # a close that died an hour ago
        self.prune()
        self.assertTrue(kept.exists())
        self.assertTrue(flying.exists())
        self.assertFalse(orphan.exists())

    def test_with_an_empty_history_too(self):
        flying = self.preview("1-2", 1)
        orphan = self.preview("1-3", 600)
        self.prune()
        self.assertTrue(flying.exists())
        self.assertFalse(orphan.exists())


if __name__ == "__main__":
    unittest.main()
