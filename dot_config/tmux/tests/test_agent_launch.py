"""⌘E / ⌘⇧E, the agent LAUNCHER (spaces.conf's User27 / User28 + `agent-ui
launch` + scripts/agent-launch.sh).

Shape: WezTerm's CMD+E and CMD+SHIFT+E send one private sequence each
(User27, User28), which open the `agent-ui launch` popup (⌘⇧E with
--pick-space; with no agent-ui, a notice); no `launch` key table is left. The
popup hands every launch to agent-launch.sh, the one table of agents: ⌘E to
`go LETTER CLIENT PANE` (a tab beside this one), ⌘⇧E to `pick --after PID
LETTER CLIENT PANE` (⌘⇧R's own space picker, once the popup is gone).

Live, on test_spaces' harness (a PRIVATE `-L` server started with
`-f /dev/null`, spaces.conf sourced with @spaces on, one client attached from
a pty this process opens; never the user's server): the script driven
directly, and the real popup driven by keys (`send-keys -K -c <client>`) when
a built agent-ui is found (AGENT_UI_BIN, else the crate's target/debug or
target/release). No real agent ever runs: the server's default-shell is
/bin/sh and its default-command only records the new pane's ZSH_AUTOSTART
into the test's temp dir, then sleeps. Which agents are INSTALLED is a fake
PATH (LAUNCH_PATH: stub executables in the temp dir) plus a fake HOME
(~/.grok/bin/grok, ~/.local/bin/cursor-agent), set in the server's global
environment for the run-shell jobs. Messages go through the real notify.sh,
whose log (XDG_STATE_HOME, the temp dir) is read back. Teardown is
test_spaces.Server's: the client killed, kill-server on that socket, its file
removed. Run with unittest or pytest (set TMUX_TMPDIR to a short private dir).
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
# A hookless agent's ZSH_AUTOSTART (aider's): the harness mark is unset in an
# `always` block, so even a ^C'd agent leaves a plain shell.
UNSET = 'tmux set-option -uw -t "$TMUX_PANE" @agent_harness 2>/dev/null'
AIDER_AUTOSTART = "{ aider; } always { %s; }" % UNSET
INSTALLED_SET = "c:claude:claude a:abby:abby x:codex:codex g:grok:grok u:cursor:cursor-agent d:aider:aider"


def agent_ui_bin():
    """A built agent-ui, else None (the popup tests skip)."""
    env = os.environ.get("AGENT_UI_BIN")
    cands = [Path(env)] if env else []
    cands += [HERE / "agent-ui" / "target" / d / "agent-ui" for d in ("debug", "release")]
    if os.environ.get("CARGO_TARGET_DIR"):
        cands.append(Path(os.environ["CARGO_TARGET_DIR"]) / "debug" / "agent-ui")
    for c in cands:
        if c.is_file() and os.access(c, os.X_OK):
            r = subprocess.run([str(c), "launch", "--bogus"], capture_output=True, text=True)
            if "agent-ui launch" in r.stderr:   # a binary that knows the subcommand
                return str(c)
    return None


AGENT_UI = agent_ui_bin()


def on_lines(prefix):
    return [l.strip() for l in ts.on_branch().splitlines() if l.strip().startswith(prefix)]


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
    def test_cmd_e_and_cmd_shift_e_are_one_user_key_each(self):
        text = WEZTERM.read_text()
        self.assertRegex(text, r'key = "e",\s*mods = "CMD",\s*action = wezterm\.action\.SendString\("\\x1b\[9140~"\)')
        self.assertRegex(text, r'key = "E",\s*mods = "CMD",\s*action = wezterm\.action\.SendString\("\\x1b\[9141~"\)')
        head = SPACES.read_text().split('\n%if "#{==:#{@spaces},on}"\n')[0]
        self.assertIn('set -s user-keys[27] "\\e[9140~"', head.splitlines())
        self.assertIn('set -s user-keys[28] "\\e[9141~"', head.splitlines())

    def test_both_keys_open_the_launcher_popup(self):
        # Per key, the fallback first (no agent-ui: a notice), then, when the
        # binary is there at load, the popup: sized to the installed set,
        # centred on the WezTerm row (@popup_x), the client's pane handed
        # over; ⌘⇧E's with --pick-space.
        on = ts.on_branch()
        for key, flag in (("User27", ""), ("User28", "--pick-space ")):
            binds = on_lines(f"bind -n {key} ")
            self.assertEqual(len(binds), 2, binds)
            self.assertIn("launch: agent-ui is not installed", binds[0])
            self.assertNotIn("display-popup", binds[0])
            popup = binds[1]
            self.assertIn("display-popup -c '#{client_tty}' -E -B", popup)
            self.assertIn("-w '#{?#{e|<:#{client_width},46},#{client_width},46}'", popup)
            self.assertIn("-h '#{e|+:#{?#{n:@launch_count},#{@launch_count},9},6}'", popup)
            self.assertIn("-x '##{E:@popup_x}'", popup)
            self.assertTrue(popup.endswith(f"'#{{@space_agent_ui}}' launch {flag}--client '#{{client_tty}}' --pane '#{{pane_id}}'\""), popup)
            self.assertIn("  " + popup + "\n", on[on.index("if-shell \"test -x '#{@space_agent_ui}'\" {\n"):])
        # The popup's width and its chrome are agent-ui's numbers.
        src = (HERE / "agent-ui" / "src" / "launch.rs").read_text()
        self.assertIn("pub const POPUP_W: u16 = 46;", src)
        self.assertIn("pub const CHROME_H: u16 = 6;", (HERE / "agent-ui" / "src" / "launch" / "view.rs").read_text())

    def test_no_launch_table_and_off_drops_both_keys(self):
        text = SPACES.read_text()
        self.assertFalse([l for l in text.splitlines() if re.match(r"\s*bind\b.*-T launch\b", l)])
        self.assertIn("unbind -aq -T launch", on_lines("unbind -aq -T launch"))
        off = ts.else_branch().splitlines()
        self.assertIn("unbind -q -n User27", off)
        self.assertIn("unbind -q -n User28", off)
        self.assertIn("unbind -aq -T launch", off)

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
        self.pane = self.rows()[self.here]["pane_id"]
        self.sid = self.t("display", "-p", "-t", "main:", "#{session_id}")

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
            self.ready = os.path.join(base, "launch-ready")
            if AGENT_UI:
                self.t("set", "-g", "@space_agent_ui", AGENT_UI)
                self.t("set-environment", "-g", "AGENT_UI_LAUNCH_TRACE", self.ready)
        super().source()

    @staticmethod
    def stub(path):
        Path(path).write_text("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)

    # -- harness ---------------------------------------------------------
    FIELDS = ["window_id", "window_index", "@agent_state", "@agent_kind", "@agent_harness", "@agent_born",
              "@agent_summary", "pane_id", "pane_current_path"]

    def rows(self, session="main"):
        out = self.t("list-windows", "-t", session, "-F", "|".join("#{%s}" % f for f in self.FIELDS))
        return {r.split("|")[0]: dict(zip(self.FIELDS, r.split("|"))) for r in out.splitlines()}

    def all_rows(self):
        out = {}
        for s in self.t("list-sessions", "-F", "#{session_name}").split():
            out.update(self.rows(s))
        return out

    def new_one(self, before, timeout=4.0, session="main"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            new = {w: r for w, r in self.rows(session).items() if w not in before}
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

    def script(self, *args, env=None):
        e = dict(self.env, TMUX=f"{self.path},0,0", LAUNCH_HOME=self.fhome, LAUNCH_PATH=self.fake, **(env or {}))
        return subprocess.run(["bash", str(LAUNCH), *args], env=e, capture_output=True, text=True, timeout=20)

    def go(self, letter):
        """agent-launch.sh go LETTER (as ⌘E's popup runs it); the new window's row."""
        before = self.rows()
        r = self.script("go", letter, self.client, self.pane)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        return self.new_one(before)

    def recorder(self):
        """@space_sh as a stand-in that logs SPACE_LAUNCH and its arguments;
        the log's lines."""
        log = os.path.join(self.tmp, "space-sh.log")
        rec = os.path.join(self.tmp, "space-sh-rec.sh")
        Path(rec).write_text(f'printf "%s|%s\\n" "$SPACE_LAUNCH" "$*" >> {log}\n')
        self.t("set", "-g", "@space_sh", f"bash {rec}")
        return lambda: Path(log).read_text().splitlines() if os.path.exists(log) else []

    def assert_after_here(self, row):
        self.assertEqual(int(row["window_index"]), int(self.rows()[self.here]["window_index"]) + 1, row)
        self.assertEqual(row["pane_current_path"], self.proj)

    def assert_born(self, row, kind):
        self.assertEqual((row["@agent_state"], row["@agent_kind"]), ("idle", kind), row)
        self.assertRegex(row["@agent_born"], r"^\d+$")
        self.assertRegex(row["@agent_summary"], r"/New Session$")

    def other_space(self):
        """A second space, `notes` (⌘2 once synced): its session id."""
        notes = os.path.join(os.path.realpath(self.tmp), "notes")
        os.makedirs(notes, exist_ok=True)
        self.new_session("notes", path=notes)
        self.wait_home("notes")
        self.wait(lambda: self.t("show", "-t", "notes", "-qv", "@space_index") != "", True)
        return self.t("display", "-p", "-t", "notes:", "#{session_id}"), notes

    # -- the installed set -------------------------------------------------
    def test_refresh_records_the_installed_agents_from_path_and_home(self):
        # The load's own refresh already ran on this server (spaces.conf,
        # through the global environment); the script on demand agrees.
        self.wait(lambda: self.t("show", "-gqv", "@launch_agents"), INSTALLED_SET)
        self.assertEqual(self.t("show", "-gqv", "@launch_count"), "6")
        self.t("set", "-gu", "@launch_agents")
        self.t("set", "-gu", "@launch_count")
        r = self.script("refresh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.t("show", "-gv", "@launch_agents"), INSTALLED_SET)
        self.assertEqual(self.t("show", "-gv", "@launch_count"), "6")
        # agy installed: its row joins, in the table's order.
        self.stub(os.path.join(self.fake, "agy"))
        self.script("refresh")
        self.assertEqual(self.t("show", "-gv", "@launch_agents"), INSTALLED_SET.replace(" u:", " y:agy:agy u:"))
        self.assertEqual(self.t("show", "-gv", "@launch_count"), "7")
        self.assertEqual(self.script("list").stdout.split("\n")[0], "claude c claude")

    def test_popup_height_follows_the_count_zero_included(self):
        # The binding's own -h format, evaluated by this server: one row per
        # agent plus the 6 of chrome; unset (no refresh yet) is 9 agents; a
        # recorded 0 is 0 (a bare `#{?@launch_count}` read "0" as unset: 15).
        popup = on_lines("bind -n User27 ")[1]
        fmt = re.search(r"-h '([^']*)'", popup).group(1)
        for count, rows in ((None, "15"), ("0", "6"), ("1", "7"), ("6", "12"), ("9", "15")):
            if count is None:
                self.t("set", "-gu", "@launch_count")
            else:
                self.t("set", "-g", "@launch_count", count)
            self.assertEqual(self.t("display", "-p", fmt), rows, count)

    # -- go: what the popup runs ------------------------------------------
    def test_claude_and_a_letter_open_here_born(self):
        row = self.go("c")
        self.assert_born(row, "claude")
        self.assert_after_here(row)
        self.assertEqual((self.autostart(row), row["@agent_harness"]), ("claude", ""))
        self.wait(lambda: self.on("#{window_id}"), row["window_id"])
        self.t("select-window", "-t", self.here)
        row = self.go("x")
        self.assert_born(row, "codex")
        self.assert_after_here(row)
        self.assertEqual(self.autostart(row), "codex")
        # abby: claude's hooks, its own harness mark.
        self.t("select-window", "-t", self.here)
        row = self.go("a")
        self.assert_born(row, "claude")
        self.assertEqual((row["@agent_harness"], self.autostart(row)), ("abby", "abby"))
        # agy (Antigravity) and copilot: their own kinds, born agents.
        for name, key in (("agy", "y"), ("copilot", "p")):
            self.stub(os.path.join(self.fake, name))
            row = self.go(key)
            self.assert_born(row, name)
            self.assertEqual((row["@agent_harness"], self.autostart(row)), ("", name))

    def test_a_hookless_agent_is_a_shell_wearing_its_harness(self):
        row = self.go("d")
        # Not born an agent (no hooks, no process the watcher knows): no
        # state for the GC to collect, only the harness mark, which the tab's
        # command unsets again when aider exits.
        self.assertEqual((row["@agent_state"], row["@agent_kind"], row["@agent_born"]), ("", "", ""), row)
        self.assertEqual(row["@agent_harness"], "aider")
        self.assert_after_here(row)
        self.assertEqual(self.autostart(row), AIDER_AUTOSTART)
        # cursor-agent (in ~/.local/bin): harness `cursor`, the sidebar's id.
        row = self.go("u")
        self.assertEqual((row["@agent_state"], row["@agent_harness"]), ("", "cursor"))
        self.assertEqual(self.autostart(row), AIDER_AUTOSTART.replace("aider", "cursor-agent"))

    # -- pick: what ⌘⇧E's popup runs ------------------------------------------
    def test_pick_opens_the_space_picker_for_that_agent(self):
        # The popup's argv: ⌘⇧R's picker (space.sh pick-tab agent), plus the agent.
        r = self.script("pick", "--print", "x", self.client, self.pane)
        argv = r.stdout.splitlines()
        self.assertEqual(argv[0], "display-popup")
        self.assertEqual(argv[argv.index("SPACE_LAUNCH=codex") - 1], "-e")
        self.assertIn("-x", argv)
        self.assertRegex(argv[-1], r"pick-tab agent ")
        # Run: the picker popup runs space.sh (a recorder here) with SPACE_LAUNCH.
        calls = self.recorder()
        r = self.script("pick", "x", self.client, self.pane)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.wait(lambda: [c.split(" ")[0] for c in calls()], ["codex|pick-tab"])
        self.assertIn("|pick-tab agent ", calls()[0])
        # Not installed: says so, no picker.
        self.script("pick", "y", self.client, self.pane)
        self.wait(lambda: ["warn", self.client, "launch: agy not installed"] in self.said(), True)
        self.assertEqual(len(calls()), 1)

    def test_pick_after_waits_for_the_launcher_to_be_gone(self):
        # --after PID: no picker while PID (the launcher's popup) lives.
        calls = self.recorder()
        held = subprocess.Popen(["sleep", "0.6"])
        t0 = time.time()
        r = self.script("pick", "--after", str(held.pid), "c", self.client, self.pane)
        held.wait()
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertGreaterEqual(time.time() - t0, 0.5)
        self.wait(lambda: [c.split(" ")[0] for c in calls()], ["claude|pick-tab"])
        # A pid long gone (or junk): no wait to speak of.
        t0 = time.time()
        self.script("pick", "--after", str(held.pid), "c", self.client, self.pane)
        self.script("pick", "--after", "x", "c", self.client, self.pane)
        self.assertLess(time.time() - t0, 3.0)
        self.wait(lambda: len(calls()), 3)

    def test_a_missing_agent_says_so_and_opens_nothing(self):
        before = self.all_rows()
        self.t("set", "-g", "@launch_agents", "stale")
        for key, text in (("y", "launch: agy not installed"), ("m", "launch: nothing on m"), ("b", "launch: nothing on b")):
            r = self.script("go", key, self.client, self.pane)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
            self.wait(lambda: ["warn", self.client, text] in self.said(), True)
        time.sleep(0.3)
        self.assertEqual(self.all_rows().keys(), before.keys())
        # A miss refreshed the installed set.
        self.assertEqual(self.t("show", "-gv", "@launch_agents"), INSTALLED_SET)

    def test_run_shell_paths_always_exit_0_and_say_nothing(self):
        # run-shell -b shows a nonzero exit ("... returned 1") or any output
        # in the pane's view mode. A tmux stand-in that fails the call each
        # path ends on: still 0, still silent; a space.sh that fails too.
        fake = os.path.join(self.tmp, "failing-tmux")
        Path(fake).write_text("#!/bin/sh\n"
                              'case "$1" in display-popup|new-window|set) echo "boom $1" >&2; exit 1 ;; esac\n'
                              f'exec {TMUX} "$@"\n')
        os.chmod(fake, 0o755)
        before = self.all_rows()
        self.t("set", "-gu", "@launch_agents")
        self.t("set", "-g", "@space_sh", "sh -c 'echo boom; exit 3' x")
        for args in (("go", "x", self.client, self.pane), ("go", "x", self.client, "%999"),
                     ("go", "m", self.client, self.pane), ("pick", "x", self.client, self.pane),
                     ("pick", "--after", "1", "y", self.client, self.pane), ("pick", "m", self.client, self.pane),
                     ("pick", "--after"), ("refresh",)):
            r = self.script(*args, env={"LAUNCH_TMUX_BIN": fake})
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), args)
        self.assertEqual(self.all_rows().keys(), before.keys())

    def test_the_picker_half_opens_that_agent_in_the_chosen_space(self):
        # space.sh new-tab (what ⌘⇧R's picker runs on a pick, `pick`'s
        # SPACE_LAUNCH preset): the agent's tab at the END of that space,
        # born its way.
        before = self.rows()
        env = dict(self.env, TMUX=f"{self.path},0,0", SPACE_LAUNCH="codex")
        r = subprocess.run(["bash", str(ts.SCRIPT), "new-tab", "agent", self.sid, self.client], env=env,
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        row = self.new_one(before)
        self.assertEqual((row["@agent_state"], row["@agent_kind"]), ("idle", "codex"))
        self.assertEqual(int(row["window_index"]), max(int(w["window_index"]) for w in self.rows().values()))
        self.assertEqual(self.autostart(row), "codex")
        # Without SPACE_LAUNCH (⌘⇧R itself): claude, as before.
        before = self.rows()
        env.pop("SPACE_LAUNCH")
        subprocess.run(["bash", str(ts.SCRIPT), "new-tab", "agent", self.sid, self.client], env=env,
                       capture_output=True, text=True, timeout=20)
        row = self.new_one(before)
        self.assertEqual((row["@agent_kind"], self.autostart(row)), ("claude", "claude"))

    def born_row(self, before, kind, session="main"):
        """The popup's new window, once its birth (same command list, a
        moment after the window appears) has landed."""
        wid = self.new_one(before, session=session)["window_id"]
        self.wait(lambda: self.rows(session)[wid]["@agent_kind"], kind)
        row = self.rows(session)[wid]
        self.assert_born(row, kind)
        return row

    # -- the popup itself ----------------------------------------------------
    def open_popup(self, key="User27"):
        """⌘E (or ⌘⇧E); until the popup has drawn (its trace), so keys reach it."""
        n = len(Path(self.ready).read_text().splitlines()) if os.path.exists(self.ready) else 0
        self.key(key)
        self.wait(lambda: len(Path(self.ready).read_text().splitlines()) if os.path.exists(self.ready) else 0,
                  n + 1, timeout=6.0)

    @unittest.skipUnless(AGENT_UI, "no built agent-ui with `launch` (cargo build, or AGENT_UI_BIN)")
    def test_the_popup_launches_by_enter_and_letter(self):
        self.wait(lambda: self.t("show", "-gqv", "@launch_count"), "6")
        # ⏎: claude (preselected), beside this tab.
        before = self.all_rows()
        self.open_popup()
        self.key("Enter")
        row = self.born_row(before, "claude")
        self.assert_after_here(row)
        # A letter: that agent at once.
        self.t("select-window", "-t", self.here)
        self.wait(lambda: self.on("#{window_id}"), self.here)
        before = self.all_rows()
        self.open_popup()
        self.key("x")
        row = self.born_row(before, "codex")
        self.assert_after_here(row)
        # ↓ ⏎: the second row (abby).
        self.t("select-window", "-t", self.here)
        self.wait(lambda: self.on("#{window_id}"), self.here)
        before = self.all_rows()
        self.open_popup()
        # Typed into the client's terminal: `send-keys -K Down` never reaches
        # a popup's program (tmux 3.7c drops it; a typed ↓ arrives as ESC [ B).
        os.write(self.fd, b"\x1b[B\r")
        wid = self.new_one(before)["window_id"]
        # (The harness mark lands in the same command list as the window,
        # a moment after it: the server runs the launch once the popup is gone.)
        self.wait(lambda: (self.rows()[wid]["@agent_kind"], self.rows()[wid]["@agent_harness"]), ("claude", "abby"))

    @unittest.skipUnless(AGENT_UI, "no built agent-ui with `launch` (cargo build, or AGENT_UI_BIN)")
    def test_the_popup_closes_on_esc_and_q_and_nothing_cycles(self):
        # Esc, q: closed, nothing opened. ⌘E inside the popup never arrives
        # (tmux drops a user key while a popup is up), ⇥ does nothing.
        before = self.all_rows()
        for close in ("Escape", "q"):
            self.open_popup()
            self.key("User27")
            self.key("Tab")
            time.sleep(0.3)
            self.assertEqual(self.all_rows().keys(), before.keys())
            self.key(close)
            time.sleep(0.3)
        self.assertEqual(self.all_rows().keys(), before.keys())
        # Closed for real: the next ⌘E opens a fresh popup.
        self.open_popup()
        self.key("Escape")

    @unittest.skipUnless(AGENT_UI, "no built agent-ui with `launch` (cargo build, or AGENT_UI_BIN)")
    def test_cmd_shift_e_hands_the_agent_to_the_space_picker(self):
        # ⌘⇧E, a letter: the launcher closes and ⌘⇧R's picker opens (its own
        # popup, which could not while the launcher's was up) for that agent.
        calls = self.recorder()
        before = self.all_rows()
        self.open_popup("User28")
        self.key("x")
        self.wait(lambda: [c.split(" ")[0] for c in calls()], ["codex|pick-tab"], timeout=6.0)
        self.assertIn(f"|pick-tab agent {self.client}", calls()[0])
        self.assertEqual(self.all_rows().keys(), before.keys())   # the picker (a recorder) picked nothing
        # ⏎: claude, the same way.
        self.open_popup("User28")
        self.key("Enter")
        self.wait(lambda: [c.split(" ")[0] for c in calls()], ["codex|pick-tab", "claude|pick-tab"], timeout=6.0)


if __name__ == "__main__":
    unittest.main()
