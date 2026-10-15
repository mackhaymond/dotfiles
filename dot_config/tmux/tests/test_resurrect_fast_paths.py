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
# Lets a harness (or an agent with a mandated socket namespace) tell its test
# servers apart in `ps`; every server still gets its own pid/id suffix.
SOCKET_PREFIX = os.environ.get("RESURRECT_TEST_SOCKET_PREFIX", "rtest")


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
        self.socket = f"{SOCKET_PREFIX}-{os.getpid()}-{id(self)}"
        self.env = dict(os.environ, HOME=str(self.root), ZDOTDIR=str(self.root / "zdot"),
                        TMUX_ASSISTANT_RESURRECT_PLUGIN=str(PLUGIN))
        # Never inherit the caller's server: tmux uses $TMUX before -L/TMUX_TMPDIR
        # for anything not given an explicit socket.
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "main", "-x", "120", "-y", "30")
        self.addCleanup(lambda: subprocess.run([TMUX, "-L", self.socket, "kill-server"], capture_output=True))
        self.tmux("set", "-g", "exit-empty", "off", ";", "set", "-g", "default-shell", "/bin/zsh", ";",
                  "set", "-g", "@resurrect-dir", str(self.dir), ";",
                  "set", "-g", "@resurrect-capture-pane-contents", "on", ";",
                  # Autosaves skip a server younger than this (default 120s);
                  # every server here is seconds old.
                  "set", "-g", "@resurrect-startup-grace-seconds", "0")
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

    def save(self, *mode):
        subprocess.run(["bash", str(SAVE), *(mode or ("quiet",))], env=self.env, check=True, capture_output=True)
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
        # Manual saves (no argument): always full, so the second one reaches the
        # identical-file path rather than the fingerprint fast path.
        first = (self.save(""), (self.dir / "last").resolve())
        time.sleep(1.1)  # a new timestamp, same content
        second = (self.save(""), (self.dir / "last").resolve())
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

    def test_exclusions_are_shell_patterns(self):
        # A matching session exists, so only the exclusion can stop the resume.
        self.tmux("new-session", "-d", "-s", "ptymcp-1", ";", "new-session", "-d", "-s", "tasks", ";",
                  "set", "-g", "@resurrect-exclude-sessions", "agents tasks ptymcp-*")
        self.wait_for_prompt("ptymcp-1:")
        self.sidecar(dict(pane="ptymcp-1:0.0", tool="claude", session_id="glob", cwd=""),
                     dict(pane="tasks:0.0", tool="claude", session_id="exact", cwd=""))
        subprocess.run(["bash", str(RESTORE_HOOK)], env=self.env, check=True, capture_output=True, timeout=60)
        log = (self.dir / "assistant-restore.log").read_text()
        self.assertIn("session 'ptymcp-1' is excluded", log)
        self.assertIn("session 'tasks' is excluded", log)
        self.assertFalse(self.stub_log.exists())


ASSISTANT_SAVE = SCRIPTS / "executable_assistant-save.sh"
# A stand-in for the plugin's save-assistant-sessions.sh: assistant-save.sh
# sources it and calls main(); this main() just writes a canned sidecar, so
# only assistant-save.sh's own exclusion pass is under test.
FAKE_PLUGIN_SAVE = """set -euo pipefail
STATE_DIR="$FAKE_STATE"
OUTPUT_FILE="$FAKE_OUTPUT"
log() { printf '%s\\n' "$*" >>"$FAKE_STATE/log"; }
main() { cp "$FAKE_INPUT" "$OUTPUT_FILE"; }
"""


class AssistantSaveExclusionTests(ThrowawayServer):
    def test_exclusions_are_shell_patterns_and_exact_names_still_work(self):
        plugin = self.root / "plugin"
        (plugin / "scripts").mkdir(parents=True)
        (plugin / "scripts/save-assistant-sessions.sh").write_text(FAKE_PLUGIN_SAVE)
        state = self.root / "state"
        state.mkdir()
        # Only tmux and jq: no claude/codex on PATH, so no --help is ever run.
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for tool in ("tmux", "jq"):
            (bin_dir / tool).symlink_to(shutil.which(tool))
        rows = [dict(pane=p, tool="claude", session_id=p) for p in
                ("main:0.0", "ptymcp-1:0.0", "ptymcp-x:2.1", "tasks:0.0", "agents:3.0", "ptymcp:0.0")]
        (self.root / "in.json").write_text(json.dumps(dict(timestamp="t", sessions=rows)))
        out = self.dir / "assistant-sessions.json"
        self.tmux("set", "-g", "@resurrect-exclude-sessions", "agents tasks ptymcp-*")
        env = dict(self.env, TMUX_ASSISTANT_RESURRECT_PLUGIN=str(plugin), FAKE_STATE=str(state),
                   FAKE_OUTPUT=str(out), FAKE_INPUT=str(self.root / "in.json"),
                   PATH=f"{bin_dir}:/usr/bin:/bin")
        r = subprocess.run(["bash", str(ASSISTANT_SAVE)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        kept = [row["pane"] for row in json.loads(out.read_text())["sessions"]]
        self.assertEqual(kept, ["main:0.0", "ptymcp:0.0"])   # `ptymcp-*` needs the dash
        self.assertIn("dropped excluded-session rows", (state / "log").read_text())


if __name__ == "__main__":
    unittest.main()
