"""The STATE NOTICE and @agent_seen_at: agent-tab-indicator.sh and
agent-tab-watcher.sh post `notifyutil -p $AGENT_STATE_NOTICE` when a write
changes a window's @agent_state (or discharges it), and stamp
@agent_seen_at "<epoch>" in the same command as every discharge.

Indicator tests drive the real script against a real, private tmux server
(`tmux -L <unique> -f /dev/null`, killed and its socket removed in tearDown).
No client is ever attached: a thin `tmux` wrapper on PATH passes everything to
the real binary except the `#{window_active_clients}` read, which answers
$FAKE_ACTIVE_CLIENTS (the same seam test_codex_watcher_parity fakes with
"active_clients"). `lsappinfo` is stubbed to name the frontmost app, and
`notifyutil` is stubbed to append its argv to a log, so a test can count
notices and check the name. Watcher tests reuse test_agent_jump_watcher's
fake-tmux harness. No real notification is posted and no GUI is touched.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

import test_agent_jump_watcher as jw   # module only, for the watcher fakes

SCRIPTS = Path(os.environ.get(
    "AGENT_TMUX_SCRIPTS",
    str(Path.home() / ".local/share/chezmoi/dot_config/tmux/scripts")))
INDICATOR = SCRIPTS / "executable_agent-tab-indicator.sh"
REAL_TMUX = shutil.which("tmux")

FAKE_NOTIFYUTIL = """#!/bin/sh
printf '%s\\n' "$*" >> "$NOTICE_LOG"
"""

# `lsappinfo front` then `lsappinfo info -only bundleid <asn>`, as both
# scripts call it. FAKE_FRONT=wez names WezTerm, anything else Finder.
FAKE_LSAPPINFO = """#!/bin/sh
case "$1" in
    front) echo "ASN:0x0-0x1:" ;;
    info) if [ "${FAKE_FRONT:-}" = wez ]; then
              echo '"CFBundleIdentifier"="com.github.wez.wezterm"'
          else
              echo '"CFBundleIdentifier"="com.apple.finder"'
          fi ;;
esac
"""

FAKE_TMUX_WRAPPER = """#!/bin/sh
case "$*" in
    *'#{window_active_clients}'*)
        if [ -n "${FAKE_ACTIVE_CLIENTS:-}" ]; then echo "$FAKE_ACTIVE_CLIENTS"; exit 0; fi ;;
esac
exec %s "$@"
"""


def write_exe(path, body):
    path.write_text(body)
    path.chmod(0o755)


def notices(log):
    return log.read_text().splitlines() if log.exists() else []


@unittest.skipUnless(REAL_TMUX, "tmux executable required")
class IndicatorNoticeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agent-notice-test-")
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"; self.bin.mkdir()
        self.home = self.root / "home"; self.home.mkdir()
        self.log = self.root / "notices.log"
        self.name = "com.example.test.agent-state." + uuid.uuid4().hex[:8]
        write_exe(self.bin / "notifyutil", FAKE_NOTIFYUTIL)
        write_exe(self.bin / "lsappinfo", FAKE_LSAPPINFO)
        write_exe(self.bin / "tmux", FAKE_TMUX_WRAPPER % REAL_TMUX)
        self.script = self.root / "agent-tab-indicator.sh"
        shutil.copyfile(INDICATOR, self.script)

        self.server = "agent-notice-test-" + uuid.uuid4().hex[:12]
        self.tmuxcmd = [REAL_TMUX, "-L", self.server, "-f", "/dev/null"]
        self.socket = None
        self.addCleanup(self.kill_server)
        clean = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        r = subprocess.run(self.tmuxcmd + ["new-session", "-d", "-s", "notice", "-x", "80", "-y", "24",
                                           "-P", "-F", "#{pane_id}|#{window_id}|#{socket_path}",
                                           "/bin/sleep 600"],
                           text=True, capture_output=True, env=clean, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.pane, self.win, self.socket = r.stdout.strip().split("|", 2)

        self.env = dict(os.environ, HOME=str(self.home), TMPDIR=str(self.root) + "/",
                        PATH=str(self.bin) + ":" + os.environ["PATH"],
                        TMUX="%s,1,0" % self.socket, TMUX_PANE=self.pane,
                        AGENT_STATE_NOTICE=self.name, NOTICE_LOG=str(self.log),
                        AGENT_TAB_CONDENSE_MODEL="", FAKE_FRONT="", FAKE_ACTIVE_CLIENTS="0")
        for key in ("AGENT_TAB_SOCKET", "AGENT_TAB_OWNER_SESSION", "AGENT_TAB_OWNER_TOKEN",
                    "AGENT_TAB_OWNER_BINDING", "AGENT_TAB_OWNER_RECORD"):
            self.env.pop(key, None)

    def kill_server(self):
        subprocess.run(self.tmuxcmd + ["kill-server"], capture_output=True, timeout=5)
        if self.socket:   # kill-server can leave the socket file behind
            Path(self.socket).unlink(missing_ok=True)
        self.tmp.cleanup()

    # --- helpers ---

    def opt(self, name):
        r = subprocess.run(self.tmuxcmd + ["show-options", "-wqv", "-t", self.win, name],
                           text=True, capture_output=True, timeout=5)
        return r.stdout.strip()

    def has_opt(self, name):
        r = subprocess.run(self.tmuxcmd + ["show-options", "-w", "-t", self.win],
                           text=True, capture_output=True, timeout=5)
        return any(line.split(" ", 1)[0] == name for line in r.stdout.splitlines())

    def set_opt(self, name, value):
        r = subprocess.run(self.tmuxcmd + ["set-option", "-w", "-t", self.win, name, value],
                           text=True, capture_output=True, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)

    def run_mode(self, *argv, payload=None, **env):
        e = dict(self.env, **env)
        stdin = "" if payload is None else json.dumps(payload)
        r = subprocess.run(["bash", str(self.script), *argv], input=stdin, text=True,
                           capture_output=True, env=e, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def hook(self, mode, **env):
        return self.run_mode(mode, "claude", payload={"session_id": "s1", "cwd": str(self.root)}, **env)

    def clear_current(self, **env):
        return self.run_mode("clear-current", self.win, **env)

    def assert_recent_epoch(self, value):
        self.assertRegex(value, r"^\d+$")
        self.assertLessEqual(abs(int(value) - time.time()), 5, value)

    def assert_notices(self, n):
        got = notices(self.log)
        self.assertEqual(got, ["-p " + self.name] * n, got)

    # --- clear-current (the focus discharge) ---

    def test_clear_current_on_done_discharges_stamps_and_notifies_once(self):
        self.set_opt("@agent_state", "done")
        self.clear_current()
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assert_recent_epoch(self.opt("@agent_seen_at"))
        self.assertFalse(self.has_opt("@agent_pending"))   # done gets no pending
        self.assert_notices(1)

    def test_clear_current_on_failed_discharges_too(self):
        self.set_opt("@agent_state", "failed")
        self.clear_current()
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assert_recent_epoch(self.opt("@agent_seen_at"))
        self.assert_notices(1)

    def test_clear_current_on_needs_input_sets_pending_and_seen_at(self):
        self.set_opt("@agent_state", "needs-input")
        self.clear_current()
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assert_recent_epoch(self.opt("@agent_pending"))
        self.assert_recent_epoch(self.opt("@agent_seen_at"))
        self.assert_notices(1)

    def test_clear_current_on_idle_or_running_writes_nothing(self):
        for state in ("idle", "running"):
            with self.subTest(state=state):
                self.set_opt("@agent_state", state)
                self.clear_current()
                self.assertEqual(self.opt("@agent_state"), state)
                self.assertFalse(self.has_opt("@agent_seen_at"))
                self.assert_notices(0)

    def test_seen_at_is_overwritten_by_a_later_discharge(self):
        self.set_opt("@agent_state", "done")
        self.set_opt("@agent_seen_at", "1000")
        self.clear_current()
        self.assert_recent_epoch(self.opt("@agent_seen_at"))
        self.assert_notices(1)

    # --- state changes without a discharge, and no-ops ---

    def test_state_change_without_discharge_notifies_without_seen_at(self):
        self.set_opt("@agent_state", "idle")
        self.hook("running")
        self.assertEqual(self.opt("@agent_state"), "running")
        self.assertFalse(self.has_opt("@agent_seen_at"))
        self.assert_notices(1)

    def test_same_state_twice_and_heartbeat_post_nothing_more(self):
        self.set_opt("@agent_state", "idle")
        self.hook("running")
        self.assert_notices(1)
        self.hook("running")       # same state; only the detail is rewritten
        self.assert_notices(1)
        self.run_mode("heartbeat", "claude")
        self.assertEqual(self.opt("@agent_state"), "running")
        self.assert_notices(1)
        self.assertFalse(self.has_opt("@agent_seen_at"))

    # --- clear (SessionEnd) ---

    def test_clear_with_a_state_unsets_but_keeps_seen_at(self):
        self.set_opt("@agent_state", "done")
        self.set_opt("@agent_summary", "proj/thing")
        self.set_opt("@agent_kind", "claude")
        self.run_mode("clear", "claude", payload={"session_id": "s1", "reason": "other"})
        for name in ("@agent_state", "@agent_summary", "@agent_kind"):
            self.assertFalse(self.has_opt(name), name)
        self.assert_recent_epoch(self.opt("@agent_seen_at"))
        self.assert_notices(1)

    def test_clear_without_a_state_is_silent(self):
        self.set_opt("@agent_summary", "proj/thing")
        self.run_mode("clear", "claude", payload={"session_id": "s1", "reason": "other"})
        self.assertFalse(self.has_opt("@agent_summary"))
        self.assertFalse(self.has_opt("@agent_seen_at"))
        self.assert_notices(0)

    # --- the name ---

    def test_empty_notice_name_posts_nothing(self):
        self.set_opt("@agent_state", "done")
        self.clear_current(AGENT_STATE_NOTICE="")
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assert_recent_epoch(self.opt("@agent_seen_at"))   # the stamp is independent
        self.hook("running", AGENT_STATE_NOTICE="")
        self.assertEqual(self.opt("@agent_state"), "running")
        self.assertFalse(self.log.exists(), notices(self.log))

    # --- viewing_now: window active for a client AND WezTerm frontmost ---

    VIEWING = dict(FAKE_ACTIVE_CLIENTS="1", FAKE_FRONT="wez")

    def test_done_while_viewing_is_idle_with_seen_at(self):
        self.set_opt("@agent_state", "running")
        self.hook("done", **self.VIEWING)
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assert_recent_epoch(self.opt("@agent_seen_at"))
        self.assert_notices(1)

    def test_done_while_viewing_an_already_idle_window_still_acks(self):
        # The hooks race: the state does not change, but the discharge is
        # still stamped and announced.
        self.set_opt("@agent_state", "idle")
        self.hook("done", **self.VIEWING)
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assert_recent_epoch(self.opt("@agent_seen_at"))
        self.assert_notices(1)

    def test_done_while_not_viewing_is_done_without_seen_at(self):
        for i, env in enumerate((dict(FAKE_ACTIVE_CLIENTS="1", FAKE_FRONT=""),     # WezTerm behind
                                 dict(FAKE_ACTIVE_CLIENTS="0", FAKE_FRONT="wez"))):  # not the active window
            with self.subTest(env=env):
                self.set_opt("@agent_state", "running")
                self.hook("done", **env)
                self.assertEqual(self.opt("@agent_state"), "done")
                self.assertFalse(self.has_opt("@agent_seen_at"))
                self.assert_notices(i + 1)   # a plain state change, announced once

    def test_needs_approval_while_viewing_is_idle_with_pending_and_seen_at(self):
        self.set_opt("@agent_state", "running")
        self.hook("needs-approval", **self.VIEWING)
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assert_recent_epoch(self.opt("@agent_pending"))
        self.assert_recent_epoch(self.opt("@agent_seen_at"))
        self.assert_notices(1)

    def test_needs_approval_while_not_viewing_has_no_seen_at(self):
        self.set_opt("@agent_state", "running")
        self.hook("needs-approval")
        self.assertEqual(self.opt("@agent_state"), "needs-input")
        self.assertFalse(self.has_opt("@agent_seen_at"))
        self.assert_notices(1)


class WatcherNoticeTests(unittest.TestCase):
    def setUp(self):
        now = int(time.time())
        w = jw.W("main", 1, **{"@agent_state": "done", "@agent_since": "%d done" % (now - 60),
                               "@agent_kind": "claude"})
        w["active_clients"] = 1
        # ttys900 runs a claude (jw.DEFAULT_PROCS): the agent is present.
        self.f = jw.FakeEnv(windows={"@1": w}, panes=[{"window": "@1", "tty": "/dev/ttys900"}])
        self.addCleanup(self.f.close)
        write_exe(self.f.bin / "notifyutil", FAKE_NOTIFYUTIL)
        write_exe(self.f.bin / "lsappinfo", FAKE_LSAPPINFO)
        self.log = self.f.dir / "notices.log"
        self.name = "com.example.test.agent-state." + uuid.uuid4().hex[:8]

    def run_ticks(self, n, **extra):
        env = dict(AGENT_TAB_WATCHER_MAX_TICKS=str(n), AGENT_STATE_NOTICE=self.name,
                   NOTICE_LOG=str(self.log))
        env.update(extra)
        r = self.f.run("agent-tab-watcher.sh", timeout=10 + 2 * n, **env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.f.read()["windows"]["@1"]["opts"]

    def test_viewed_done_is_discharged_with_seen_at_and_one_notice(self):
        o = self.run_ticks(1, FAKE_FRONT="wez")
        self.assertEqual(o["@agent_state"], "idle")
        self.assertRegex(o["@agent_seen_at"], r"^\d+$")
        self.assertLessEqual(abs(int(o["@agent_seen_at"]) - time.time()), 5)
        self.assertEqual(notices(self.log), ["-p " + self.name])
        # Later ticks have nothing to discharge: no repeat.
        o = self.run_ticks(2, FAKE_FRONT="wez")
        self.assertEqual(o["@agent_state"], "idle")
        self.assertEqual(notices(self.log), ["-p " + self.name])

    def test_unviewed_done_is_left_alone_and_silent(self):
        o = self.run_ticks(1, FAKE_FRONT="")
        self.assertEqual(o["@agent_state"], "done")
        self.assertNotIn("@agent_seen_at", o)
        self.assertEqual(notices(self.log), [])

    def test_empty_notice_name_posts_nothing(self):
        o = self.run_ticks(1, FAKE_FRONT="wez", AGENT_STATE_NOTICE="")
        self.assertEqual(o["@agent_state"], "idle")
        self.assertIn("@agent_seen_at", o)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
