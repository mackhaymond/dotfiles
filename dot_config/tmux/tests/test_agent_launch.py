"""⌘E, the LAUNCH leader (spaces.conf's `launch` table + scripts/agent-launch.sh).

Shape: WezTerm's CMD+E sends one private sequence (User27), the leader arms
`launch`, ⏎ and ⌘E again are prefix k's claude, every letter and capital is
bound (so none leaks to the pane or to prefix v's stash root keys).

Live, on test_spaces' harness (a PRIVATE `-L` server started with
`-f /dev/null`, spaces.conf sourced with @spaces on, one client attached from
a pty this process opens; never the user's server): keys pressed with
`send-keys -K -c <client>`. No real agent ever runs: the server's
default-shell is /bin/sh and its default-command only records the new pane's
ZSH_AUTOSTART into the test's temp dir, then sleeps. Which agents are
INSTALLED is a fake PATH (LAUNCH_PATH: stub executables in the temp dir) plus
a fake HOME (~/.grok/bin/grok, ~/.local/bin/cursor-agent), set in the
server's global environment for the run-shell jobs. Messages go through the
real notify.sh, whose log (XDG_STATE_HOME, the temp dir) is read back.
Teardown is test_spaces.Server's: the client killed, kill-server on that
socket, its file removed. Run with unittest or pytest.
"""
import os
from pathlib import Path
import pty
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

import test_spaces as ts   # module only: the server harness

HERE = ts.HERE
LAUNCH = HERE / "scripts" / "executable_agent-launch.sh"
SPACES = ts.SPACES
WEZTERM = ts.WEZTERM
TMUX = ts.TMUX
ZSH = shutil.which("zsh")
FOCUS = 'run-shell -b "notifyutil -p com.mackhaymond.tmux.focus"'
# A hookless agent's ZSH_AUTOSTART (aider's): the harness mark is unset in an
# `always` block, so even a ^C'd agent leaves a plain shell.
UNSET = 'tmux set-option -uw -t "$TMUX_PANE" @agent_harness 2>/dev/null'
AIDER_AUTOSTART = "{ aider; } always { %s; }" % UNSET


def on_lines(prefix):
    return [l for l in ts.on_branch().splitlines() if l.startswith(prefix)]


@unittest.skipUnless(ZSH, "zsh not installed")
class HooklessCleanup(unittest.TestCase):
    """The autostart line as .zshrc runs it (`eval` in an interactive zsh, in
    a pty this process opens; `zsh -f`, no rc files), with `sleep` for the
    agent and a fake `tmux` that records its arguments: ^C kills the agent,
    and the unset still runs. The plain `agent; unset` list it replaced is
    run too, to show the harness can tell (zsh drops the rest of that list)."""

    def interrupted(self, line):
        tmp = tempfile.mkdtemp(prefix="launch-zsh-")
        self.addCleanup(shutil.rmtree, tmp, True)
        log = os.path.join(tmp, "tmux.log")
        fake = os.path.join(tmp, "tmux")
        Path(fake).write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {log}\n')
        os.chmod(fake, 0o755)
        env = {"PATH": f"{tmp}:/usr/bin:/bin", "HOME": tmp, "TERM": "xterm-256color", "TMUX_PANE": "%99",
               "AUTO": line.replace("aider", "sleep 30", 1)}
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.execve(ZSH, [ZSH, "-f", "-i"], env)
            finally:
                os._exit(127)
        out = b""
        try:
            def until(pred, timeout=5.0):
                nonlocal out
                deadline = time.time() + timeout
                while time.time() < deadline:
                    import select
                    r, _, _ = select.select([fd], [], [], 0.05)
                    if r:
                        try:
                            out += os.read(fd, 65536)
                        except OSError:
                            return False
                    if pred():
                        return True
                return False
            os.write(fd, b'eval "$AUTO"; print -r -- AFTER-$?\n')
            # The agent (sleep) is running: its process exists.
            self.assertTrue(until(lambda: subprocess.run(["pgrep", "-P", str(pid), "sleep"],
                                                         capture_output=True).returncode == 0), out)
            os.write(fd, b"\x03")
            time.sleep(0.5)
            os.write(fd, b"print -r -- PROMPT-BACK\n")
            self.assertTrue(until(lambda: b"PROMPT-BACK" in out.split(b"print -r -- PROMPT-BACK")[-1]), out)
            os.write(fd, b"exit\n")
            until(lambda: False, 0.3)
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            os.waitpid(pid, 0)
            os.close(fd)
        return Path(log).read_text() if os.path.exists(log) else ""

    def test_ctrl_c_still_unsets_the_harness(self):
        self.assertEqual(self.interrupted(AIDER_AUTOSTART), "set-option -uw -t %99 @agent_harness\n")

    def test_the_plain_list_would_have_kept_it(self):
        self.assertEqual(self.interrupted("aider; " + UNSET), "")


class Shape(unittest.TestCase):
    def test_cmd_e_is_one_user_key(self):
        text = WEZTERM.read_text()
        self.assertRegex(text, r'key = "e",\s*mods = "CMD",\s*action = wezterm\.action\.SendString\("\\x1b\[9140~"\)')
        head = SPACES.read_text().split('\n%if "#{==:#{@spaces},on}"\n')[0]
        self.assertIn('set -s user-keys[27] "\\e[9140~"', head.splitlines())

    def test_the_leader_arms_launch_and_off_drops_it(self):
        self.assertEqual(on_lines("bind -n User27 "),
                         [f"bind -n User27 switch-client -T launch \\; {FOCUS}"])
        off = ts.else_branch().splitlines()
        self.assertIn("unbind -q -n User27", off)
        self.assertIn("unbind -aq -T launch", off)
        self.assertFalse([l for l in off if "-T launch" in l and l.startswith("bind")])

    def test_enter_and_cmd_e_again_are_prefix_k(self):
        k = on_lines("bind k ")[0][len("bind k "):].replace(" \\; ", " ; ")
        for key in ("Enter", "User27"):
            self.assertEqual(on_lines(f"bind -T launch {key} "), [f"bind -T launch {key} {{ {k} ; {FOCUS} }}"])

    def test_every_letter_goes_through_the_script(self):
        for c in "abcdefghijklmnopqrstuvwxyz":
            self.assertEqual(on_lines(f"bind -T launch {c} "),
                             [f"bind -T launch {c} {{ run-shell -b \"#{{@launch_sh}} go {c} '#{{client_name}}' '#{{pane_id}}'\" ; {FOCUS} }}"])
            C = c.upper()
            self.assertEqual(on_lines(f"bind -T launch {C} "),
                             [f"bind -T launch {C} {{ run-shell -b \"#{{@launch_sh}} pick {c} '#{{client_name}}' '#{{pane_id}}'\" ; {FOCUS} }}"])
        for key in ("Escape", "Space", "C-c"):
            self.assertEqual(on_lines(f"bind -T launch {key} "), [f"bind -T launch {key} {FOCUS}"])
        self.assertNotIn("bind -T launch Any", SPACES.read_text())

    def test_the_table_is_the_scripts_one(self):
        # The letters Mack chose, ollama nowhere.
        text = LAUNCH.read_text()
        rows = re.findall(r'^  "(\S+)\s+(\S)\s+(\S+)\s+(\S+)\s+(\S+)"$', text, re.M)
        self.assertEqual([(r[0], r[1]) for r in rows],
                         [("claude", "c"), ("abby", "a"), ("codex", "x"), ("opencode", "o"), ("grok", "g"),
                          ("agy", "y"), ("cursor", "u"), ("copilot", "p"), ("aider", "d")])
        self.assertNotIn("gemini", "".join(r[2] for r in rows))   # replaced by agy
        self.assertEqual(len({r[1] for r in rows}), len(rows))
        self.assertNotIn("ollama", "".join(r[2] for r in rows))


@unittest.skipUnless(TMUX, "tmux not installed")
class Launch(ts.Server):
    INSTALLED = ("claude", "abby", "codex", "aider")

    def setUp(self):
        super().setUp()
        # No real shell, let alone a real agent: every new pane records its
        # ZSH_AUTOSTART and sleeps.
        self.t("set", "-g", "default-shell", "/bin/sh")
        self.t("set", "-g", "default-command", f'printf %s "$ZSH_AUTOSTART" > {self.auto}/"$TMUX_PANE"; exec sleep 600')
        self.here = self.new_window("main", path=self.proj)
        self.attach()
        self.t("select-window", "-t", self.here)
        self.wait(lambda: self.on("#{window_id}|#{pane_current_path}"), f"{self.here}|{self.proj}")

    def source(self):
        # Before spaces.conf's first load (the server is up, the config not
        # yet sourced): the fake PATH and HOME, in the global environment
        # its run-shell jobs get, and the script's source copy.
        if not hasattr(self, "fake"):
            base = os.path.realpath(self.tmp)
            self.fake = os.path.join(base, "bin")
            self.fhome = os.path.join(base, "fhome")
            self.auto = os.path.join(base, "auto")
            self.proj = os.path.join(base, "my-project")
            for d in (self.fake, self.auto, self.proj, os.path.join(self.fhome, ".grok", "bin"),
                      os.path.join(self.fhome, ".local", "bin")):
                os.makedirs(d)
            for name in self.INSTALLED:
                self.stub(os.path.join(self.fake, name))
            self.stub(os.path.join(self.fhome, ".grok", "bin", "grok"))
            self.stub(os.path.join(self.fhome, ".local", "bin", "cursor-agent"))
            self.t("set-environment", "-g", "LAUNCH_PATH", self.fake)
            self.t("set-environment", "-g", "LAUNCH_HOME", self.fhome)
            self.t("set", "-g", "@launch_sh", f"bash {LAUNCH}")
        super().source()

    @staticmethod
    def stub(path):
        Path(path).write_text("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)

    # -- harness ---------------------------------------------------------
    FIELDS = ["window_id", "window_index", "@agent_state", "@agent_kind", "@agent_harness", "@agent_born",
              "@agent_summary", "pane_id", "pane_current_path"]

    def rows(self):
        out = self.t("list-windows", "-t", "main", "-F", "|".join("#{%s}" % f for f in self.FIELDS))
        return {r.split("|")[0]: dict(zip(self.FIELDS, r.split("|"))) for r in out.splitlines()}

    def new_one(self, before, timeout=4.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            new = {w: r for w, r in self.rows().items() if w not in before}
            if new:
                self.assertEqual(len(new), 1, new)
                return next(iter(new.values()))
            time.sleep(0.02)
        self.fail("no new window appeared")

    def autostart(self, row, timeout=4.0):
        f = os.path.join(self.auto, row["pane_id"])
        self.wait(lambda: os.path.exists(f), True, timeout)
        time.sleep(0.05)
        return Path(f).read_text()

    def said(self):
        log = os.path.join(self.env["XDG_STATE_HOME"], "tmux-notify", "log")
        return [l.split("\t")[1:4] for l in Path(log).read_text().splitlines()] if os.path.exists(log) else []

    def table(self):
        return self.on("#{client_key_table}")

    def script(self, *args, env=None):
        e = dict(self.env, TMUX=f"{self.path},0,0", LAUNCH_HOME=self.fhome, LAUNCH_PATH=self.fake, **(env or {}))
        return subprocess.run(["bash", str(LAUNCH), *args], env=e, capture_output=True, text=True, timeout=20)

    def launch(self, *keys):
        """⌘E then KEYS; the new window's row (after the current one)."""
        before = self.rows()
        self.key("User27")
        self.wait(self.table, "launch")
        self.key(*keys)
        return before, self.new_one(before)

    def assert_after_here(self, row):
        self.assertEqual(int(row["window_index"]), int(self.rows()[self.here]["window_index"]) + 1, row)
        self.assertEqual(row["pane_current_path"], self.proj)

    def assert_born(self, row, kind):
        self.assertEqual((row["@agent_state"], row["@agent_kind"]), ("idle", kind), row)
        self.assertRegex(row["@agent_born"], r"^\d+$")
        self.assertEqual(row["@agent_summary"], "my-project/New Session")

    # -- the installed set -------------------------------------------------
    def test_refresh_records_the_installed_agents_from_path_and_home(self):
        # The load's own refresh already ran on this server (spaces.conf,
        # through the global environment); the script on demand agrees.
        want = "c:claude a:abby x:codex g:grok u:cursor d:aider"
        self.wait(lambda: self.t("show", "-gqv", "@launch_agents"), want)
        self.t("set", "-gu", "@launch_agents")
        r = self.script("refresh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.t("show", "-gv", "@launch_agents"), want)
        # agy installed: its letter joins, in the table's order.
        self.stub(os.path.join(self.fake, "agy"))
        self.script("refresh")
        self.assertEqual(self.t("show", "-gv", "@launch_agents"),
                         "c:claude a:abby x:codex g:grok y:agy u:cursor d:aider")
        self.assertEqual(self.script("list").stdout.split("\n")[0], "claude c claude")

    # -- the keys ----------------------------------------------------------
    def test_cmd_e_arms_launch_and_escape_cancels(self):
        before = self.rows()
        for cancel in ("Escape", "Space", "C-c"):
            self.key("User27")
            self.wait(self.table, "launch")
            self.key(cancel)
            self.wait(self.table, "root")
        time.sleep(0.3)
        self.assertEqual(self.rows().keys(), before.keys())

    def test_enter_opens_claude_born_an_agent(self):
        _, row = self.launch("Enter")
        self.assert_born(row, "claude")
        self.assert_after_here(row)
        self.assertEqual(self.autostart(row), "claude")
        self.assertEqual(row["@agent_harness"], "")
        self.wait(self.table, "root")

    def test_cmd_e_twice_opens_claude(self):
        _, row = self.launch("User27")
        self.assert_born(row, "claude")
        self.assertEqual(self.autostart(row), "claude")

    def test_a_letter_opens_that_agent_here(self):
        _, row = self.launch("x")
        self.assert_born(row, "codex")
        self.assert_after_here(row)
        self.assertEqual(self.autostart(row), "codex")
        self.wait(lambda: self.on("#{window_id}"), row["window_id"])
        # abby: claude's hooks, its own harness mark.
        self.t("select-window", "-t", self.here)
        _, row = self.launch("a")
        self.assert_born(row, "claude")
        self.assertEqual(row["@agent_harness"], "abby")
        self.assertEqual(self.autostart(row), "abby")
        # agy (Antigravity) and copilot: their own kinds, born agents.
        for name, key in (("agy", "y"), ("copilot", "p")):
            self.stub(os.path.join(self.fake, name))
            self.t("select-window", "-t", self.here)
            _, row = self.launch(key)
            self.assert_born(row, name)
            self.assertEqual((row["@agent_harness"], self.autostart(row)), ("", name))

    def test_a_hookless_agent_is_a_shell_wearing_its_harness(self):
        _, row = self.launch("d")
        # Not born an agent (no hooks, no process the watcher knows): no
        # state for the GC to collect, only the harness mark, which the tab's
        # command unsets again when aider exits.
        self.assertEqual((row["@agent_state"], row["@agent_kind"], row["@agent_born"]), ("", "", ""), row)
        self.assertEqual(row["@agent_harness"], "aider")
        self.assert_after_here(row)
        self.assertEqual(self.autostart(row), AIDER_AUTOSTART)
        # cursor-agent (in ~/.local/bin): harness `cursor`, the sidebar's id.
        self.t("select-window", "-t", self.here)
        _, row = self.launch("u")
        self.assertEqual((row["@agent_state"], row["@agent_harness"]), ("", "cursor"))
        self.assertEqual(self.autostart(row), AIDER_AUTOSTART.replace("aider", "cursor-agent"))

    def test_a_missing_agent_says_so_and_opens_nothing(self):
        before = self.rows()
        self.t("set", "-g", "@launch_agents", "stale")
        for key, text in (("y", "launch: agy not installed"), ("m", "launch: nothing on m"), ("b", "launch: nothing on b")):
            self.key("User27")
            self.wait(self.table, "launch")
            self.key(key)
            self.wait(lambda: ["warn", self.client, text] in self.said(), True)
        time.sleep(0.3)
        self.assertEqual(self.rows().keys(), before.keys())
        # A miss refreshed the installed set.
        self.assertEqual(self.t("show", "-gv", "@launch_agents"), "c:claude a:abby x:codex g:grok u:cursor d:aider")

    def test_shift_letter_opens_the_space_picker_for_that_agent(self):
        # The popup's argv: ⌘⇧R's picker (space.sh pick-tab agent), plus the agent.
        pane = self.rows()[self.here]["pane_id"]
        r = self.script("pick", "--print", "x", self.client, pane)
        argv = r.stdout.splitlines()
        self.assertEqual(argv[0], "display-popup")
        self.assertIn("SPACE_LAUNCH=codex", argv)
        self.assertEqual(argv[argv.index("SPACE_LAUNCH=codex") - 1], "-e")
        self.assertRegex(argv[-1], r"pick-tab agent ")
        # Pressed: the popup runs space.sh (a recorder here) with SPACE_LAUNCH.
        log = os.path.join(self.tmp, "space-sh.log")
        rec = os.path.join(self.tmp, "space-sh-rec.sh")
        Path(rec).write_text(f'printf "%s|%s\\n" "$SPACE_LAUNCH" "$*" >> {log}\n')
        self.t("set", "-g", "@space_sh", f"bash {rec}")
        self.key("User27")
        self.wait(self.table, "launch")
        self.key("X")
        calls = lambda: Path(log).read_text().splitlines() if os.path.exists(log) else []
        self.wait(lambda: [c.split(" ")[0] for c in calls()], ["codex|pick-tab"])
        self.assertIn("|pick-tab agent ", calls()[0])
        # Not installed: says so, no popup.
        self.key("User27")
        self.wait(self.table, "launch")
        self.key("Y")
        self.wait(lambda: ["warn", self.client, "launch: agy not installed"] in self.said(), True)
        self.assertEqual(len(calls()), 1)

    def test_run_shell_paths_always_exit_0_and_say_nothing(self):
        # run-shell -b shows a nonzero exit ("... returned 1") or any output
        # in the pane's view mode. A tmux stand-in that fails the call each
        # path ends on: still 0, still silent.
        fake = os.path.join(self.tmp, "failing-tmux")
        Path(fake).write_text("#!/bin/sh\n"
                              'case "$1" in display-popup|new-window|set) echo "boom $1" >&2; exit 1 ;; esac\n'
                              f'exec {TMUX} "$@"\n')
        os.chmod(fake, 0o755)
        pane = self.rows()[self.here]["pane_id"]
        before = self.rows()
        self.t("set", "-gu", "@launch_agents")
        for args in (("pick", "x", self.client, pane), ("go", "x", self.client, pane),
                     ("go", "x", self.client, "%999"), ("go", "m", self.client, pane),
                     ("pick", "y", self.client, pane), ("refresh",)):
            r = self.script(*args, env={"LAUNCH_TMUX_BIN": fake})
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), args)
        self.assertEqual(self.rows().keys(), before.keys())
        # Pressed, with a picker that fails: the pane never enters view mode.
        self.t("set", "-g", "@space_sh", "false")
        self.key("User27")
        self.wait(self.table, "launch")
        self.key("X")
        self.wait(self.table, "root")
        time.sleep(0.5)
        self.assertEqual(self.t("display", "-p", "-t", pane, "#{pane_in_mode}"), "0")

    def test_the_picker_opens_that_agent_in_the_chosen_space(self):
        # space.sh new-tab (what the picker runs on a pick) with SPACE_LAUNCH:
        # the agent's tab at the END of that space, born its way.
        sid = self.t("display", "-p", "-t", "main:", "#{session_id}")
        before = self.rows()
        env = dict(self.env, TMUX=f"{self.path},0,0", SPACE_LAUNCH="codex")
        r = subprocess.run(["bash", str(ts.SCRIPT), "new-tab", "agent", sid, self.client], env=env,
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        row = self.new_one(before)
        self.assertEqual((row["@agent_state"], row["@agent_kind"]), ("idle", "codex"))
        self.assertEqual(int(row["window_index"]), max(int(w["window_index"]) for w in self.rows().values()))
        self.assertEqual(self.autostart(row), "codex")
        self.wait(lambda: self.on("#{window_id}"), row["window_id"])
        # Without SPACE_LAUNCH (⌘⇧R itself): claude, as before.
        before = self.rows()
        env.pop("SPACE_LAUNCH")
        subprocess.run(["bash", str(ts.SCRIPT), "new-tab", "agent", sid, self.client], env=env,
                       capture_output=True, text=True, timeout=20)
        row = self.new_one(before)
        self.assertEqual((row["@agent_kind"], self.autostart(row)), ("claude", "claude"))


if __name__ == "__main__":
    unittest.main()
