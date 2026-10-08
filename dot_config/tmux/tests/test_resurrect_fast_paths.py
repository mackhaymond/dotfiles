"""resurrect-save.sh and assistant-restore.sh against a throwaway tmux server.

Each test gets its own socket, a scratch @resurrect-dir and a bare zsh, so the
live server and ~/.tmux/resurrect are never touched.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).parents[1] / "scripts"
SAVE = SCRIPTS / "executable_resurrect-save.sh"
RESTORE_HOOK = SCRIPTS / "executable_assistant-restore.sh"
PLUGIN = Path.home() / ".config/tmux/plugins/tmux-assistant-resurrect"
TMUX = shutil.which("tmux")


@unittest.skipUnless(TMUX, "tmux not installed")
class ThrowawayServer(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dir = self.root / "resurrect"
        self.dir.mkdir()
        (self.root / "zdot").mkdir()
        (self.root / "zdot/.zshrc").write_text("PS1='$ '\n")
        self.socket = f"rtest-{os.getpid()}-{id(self)}"
        self.env = dict(os.environ, HOME=str(self.root), ZDOTDIR=str(self.root / "zdot"),
                        TMUX_ASSISTANT_RESURRECT_PLUGIN=str(PLUGIN))
        self.env.pop("TMUX", None)
        self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "main", "-x", "120", "-y", "30")
        self.addCleanup(lambda: subprocess.run([TMUX, "-L", self.socket, "kill-server"], capture_output=True))
        self.tmux("set", "-g", "exit-empty", "off", ";", "set", "-g", "default-shell", "/bin/zsh", ";",
                  "set", "-g", "@resurrect-dir", str(self.dir), ";",
                  "set", "-g", "@resurrect-capture-pane-contents", "on")
        path = self.tmux("display", "-p", "#{socket_path}").strip()
        self.env["TMUX"] = f"{path},0,0"
        # resurrect-save.sh refuses any server but the default one; this is its opt-in.
        self.env["RESURRECT_SAVE_ALLOW_SOCKET"] = path
        # kill-server leaves the socket file behind. Cleanups run LIFO, so this
        # one goes before the -L kill above: kill first, then remove the file.
        def reap(p=path):
            subprocess.run([TMUX, "-S", p, "kill-server"], capture_output=True)
            Path(p).unlink(missing_ok=True)
        self.addCleanup(reap)

    def tmux(self, *args):
        env = getattr(self, "env", dict(os.environ))
        env = {k: v for k, v in env.items() if k != "TMUX"}
        return subprocess.run([TMUX, "-L", self.socket, *args], env=env, capture_output=True,
                              text=True, check=True).stdout

    def wait_for_prompt(self, target):
        for _ in range(100):
            if self.tmux("display", "-p", "-t", target, "#{cursor_x}") != "0\n":
                return
            time.sleep(0.05)
        self.fail(f"no prompt in {target}")

    def save(self):
        subprocess.run(["bash", str(SAVE), "quiet"], env=self.env, check=True, capture_output=True)
        return (self.dir / "last").resolve().read_text()


class SaveTests(ThrowawayServer):
    def test_layout_rows_are_well_formed_and_excluded_sessions_are_skipped(self):
        spaced = self.root / "dir with  spaces"
        spaced.mkdir()
        spaced = spaced.resolve()  # /var -> /private/var, as tmux reports it
        self.tmux("new-session", "-d", "-s", "agents", ";", "new-session", "-d", "-s", "work", "-c", str(spaced))
        self.tmux("select-pane", "-t", "work:", "-T", "")
        self.wait_for_prompt("work:")
        self.tmux("send-keys", "-t", "work:", "echo saved-text", "Enter")
        time.sleep(0.3)

        layout = self.save()
        panes = [line.split("\t") for line in layout.splitlines() if line.startswith("pane\t")]
        self.assertEqual({row[1] for row in panes}, {"main", "work"})
        self.assertTrue(all(len(row) == 11 for row in panes), panes)
        work = next(row for row in panes if row[1] == "work")
        self.assertEqual(work[6], ":")  # blank title: a placeholder, not a shifted row
        self.assertEqual(work[7], ":" + str(spaced).replace(" ", "\\ "))
        self.assertEqual(work[9], "zsh")
        self.assertEqual(work[10], ":")
        windows = [line.split("\t") for line in layout.splitlines() if line.startswith("window\t")]
        self.assertEqual({row[1] for row in windows}, {"main", "work"})
        self.assertTrue(all(len(row) == 8 for row in windows), windows)
        self.assertTrue(layout.splitlines()[-1].startswith("state\t"))

        with tarfile.open(self.dir / "pane_contents.tar.gz") as archive:
            member = archive.extractfile("./pane_contents/pane-work:0.0")
            self.assertIn(b"saved-text", member.read())
            self.assertNotIn("./pane_contents/pane-agents:0.0", archive.getnames())

    def test_pane_command_is_the_exact_child_not_a_pid_prefix_match(self):
        self.wait_for_prompt("main:")
        self.tmux("send-keys", "-t", "main:", "sleep 3001", "Enter")
        time.sleep(0.5)
        row = next(line.split("\t") for line in self.save().splitlines() if line.startswith("pane\tmain"))
        self.assertEqual(row[10], ":sleep 3001")

    def test_identical_save_keeps_last_and_hooks_run_with_output_captured(self):
        marker = self.root / "hook-ran"
        self.tmux("set", "-g", "@resurrect-hook-post-save-all", f"echo noisy; touch '{marker}'")
        first = (self.save(), (self.dir / "last").resolve())
        time.sleep(1.1)  # a new timestamp, same content
        second = (self.save(), (self.dir / "last").resolve())
        self.assertEqual(first, second)
        self.assertEqual(len(list(self.dir.glob("tmux_resurrect_*.txt"))), 1)
        self.assertTrue(marker.exists())
        self.assertIn("noisy", (self.dir / "save.log").read_text())

    def test_exclusion_can_be_turned_off(self):
        self.tmux("new-session", "-d", "-s", "agents", ";", "set", "-g", "@resurrect-exclude-sessions", "none")
        self.assertIn("pane\tagents\t", self.save())


@unittest.skipUnless((PLUGIN / "scripts/lib-detect.sh").exists(), "tmux-assistant-resurrect not installed")
class RestoreHookTests(ThrowawayServer):
    def setUp(self):
        super().setUp()
        self.stub_log = self.root / "stub.log"
        stubs = self.root / "stub"
        stubs.mkdir()
        for tool in ("claude", "codex"):
            stub = stubs / tool
            stub.write_text(f'#!/bin/bash\necho "{tool} $*" >> "{self.stub_log}"\n')
            stub.chmod(0o755)
        # Set in .zshrc, after /etc/zprofile's path_helper has reordered PATH:
        # nothing here may ever reach the real claude/codex.
        with (self.root / "zdot/.zshrc").open("a") as rc:
            rc.write(f"export PATH='{stubs}:/usr/bin:/bin'\n")
        self.tmux("set", "-g", "@assistant-resurrect-client-wait", "0")

    def sidecar(self, *sessions):
        (self.dir / "assistant-sessions.json").write_text(json.dumps(dict(timestamp="t", sessions=list(sessions))))

    def test_resumes_every_pane_with_its_arguments_and_skips_what_it_must(self):
        # Created after PATH was set, so their shells find the stubs.
        self.tmux("new-session", "-d", "-s", "work", ";", "new-window", "-t", "work:", ";",
                  "new-window", "-t", "work:", "sleep 600", ";", "new-session", "-d", "-s", "agents")
        self.sidecar(
            dict(pane="work:0.0", tool="claude", session_id="c-1", cwd=str(self.root), cli_args="--model x[1m]", model="x[1m]"),
            dict(pane="work:1.0", tool="codex", session_id="x-2", cwd="", cli_args="--no-daemon -c k=v"),
            dict(pane="work:2.0", tool="claude", session_id="busy", cwd=""),        # not a shell
            dict(pane="agents:0.0", tool="claude", session_id="excluded", cwd=""),  # excluded session
            dict(pane="gone:0.0", tool="claude", session_id="missing", cwd=""),     # no such session
        )
        subprocess.run(["bash", str(RESTORE_HOOK)], env=self.env, check=True, capture_output=True, timeout=60)
        for _ in range(100):
            if self.stub_log.exists() and len(self.stub_log.read_text().splitlines()) >= 2:
                break
            time.sleep(0.05)
        log = (self.dir / "assistant-restore.log").read_text()
        self.assertTrue(self.stub_log.exists(), log + self.tmux("capture-pane", "-p", "-t", "work:0"))
        self.assertEqual(sorted(self.stub_log.read_text().splitlines()),
                         ["claude --model x[1m] --resume c-1", "codex --no-daemon -c k=v resume x-2"])
        self.assertIn("pane work:2.0 is running 'sleep' (not a shell)", log)
        self.assertIn("session 'agents' is excluded", log)
        self.assertIn("session 'gone' does not exist", log)
        self.assertIn("restored 2 of 5", log)


if __name__ == "__main__":
    unittest.main()
