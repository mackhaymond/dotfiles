"""SPACES (docs/spaces.md): spaces.conf + scripts/space.sh.

Real tmux, never the live server: each test gets its own `-L` socket started
with `-f /dev/null`, sets what tmux.conf would set before the source-file
(@space_hidden_sessions, @spaces, and @space_sh pointed at the chezmoi source
copy of the script), sources spaces.conf, and attaches one client from a
pseudo-terminal this process opens itself (pty.fork). Keys are delivered with
`send-keys -K -c <client>`, i.e. through the client's key tables exactly as
typed; mouse clicks are SGR mouse reports written into the client's terminal.
The registry and every other state file live in the test's temp dir
(@space_registry, and XDG_STATE_HOME in the server's environment); tabs a
close would hand to closed-tabs.sh go to @space_closer instead (none, or a
recorder). The script is only ever run with TMUX pointing at the test server.
Teardown kills the client, kill-servers the socket and removes its file.
Run with unittest or pytest.
"""
import fcntl
import importlib.util
import os
from pathlib import Path
import pty
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
import unittest
import uuid

HERE = Path(__file__).resolve().parents[1]
CONF = HERE / "tmux.conf.tmpl"
SPACES = HERE / "spaces.conf"
SCRIPT = HERE / "scripts" / "executable_space.sh"
DASH = HERE / "scripts" / "executable_space-dash.sh"
ROSTER = HERE / "scripts" / "executable_agent-roster.py"
WEZTERM = HERE.parent / "wezterm" / "wezterm.lua.tmpl"
TMUX = shutil.which("tmux")
HIDDEN = "agents tasks stash scratch btop-popup"
COLS, ROWS = 140, 20


def conf_line(prefix):
    """The one tmux.conf.tmpl line starting with `prefix`."""
    hits = [l for l in CONF.read_text().splitlines() if l.startswith(prefix)]
    assert len(hits) == 1, (prefix, hits)
    return hits[0]


# tmux.conf lines spaces.conf rebinds when on and restores verbatim when off.
TODAY_BINDS = [conf_line(p) for p in ("bind c ", "bind C-t ", "bind k ", "bind C-k ", "bind x ", "bind C-l ")]
# The session picker: tmux.conf binds it on every load, spaces.conf unbinds it
# when on and leaves it alone when off (prefix r re-runs tmux.conf first).
PICKER_BINDS = [conf_line(p) for p in ("bind-key a ", "bind-key C-a ")]


def else_branch():
    text = SPACES.read_text()
    return text[text.index("\n%else\n"):text.index("\n%endif")]


def on_branch():
    text = SPACES.read_text()
    return text[text.index('\n%if "#{==:#{@spaces},on}"\n'):text.index("\n%else\n")]


# The one place off differs from a pre-spaces server: prefix M-1..9 select
# window N, and WezTerm's digit chords (User0..26, one private sequence each)
# do what their keys did before spaces.
OFF_META = [l for l in else_branch().splitlines()
            if re.match(r'bind -N "Select window \d" M-\d |bind -n User\d+ ', l)]
# WezTerm's digit chords: row (CTRL space, ALT shell, CMD agent) -> sequence.
# This space's chip, as text: █⌃N name, then the powerline edge.
CHIP_RE = "█⌃(\\d) ([\\w-]+)\ue0b0"
CHORD = {(row, n): f"\x1b[91{r}{n}~" for r, row in ((1, "CTRL"), (2, "ALT"), (3, "CMD")) for n in range(1, 10)}


class ConfigShape(unittest.TestCase):
    def test_else_branch_restores_todays_rebound_keys_verbatim(self):
        lines = else_branch().splitlines()
        for b in TODAY_BINDS:
            self.assertIn(b, lines)

    def test_flag_and_hidden_list_live_in_tmux_conf(self):
        text = CONF.read_text()
        self.assertRegex(text, r"(?m)^set -g @spaces (on|off)$")
        self.assertIn(f"set -g @space_hidden_sessions '{HIDDEN}'", text)
        self.assertNotIn("@space_hidden_sessions '", SPACES.read_text())
        # Sourced last, after the flag is set (parse-time %if).
        last = [l for l in text.splitlines() if l.strip() and not l.startswith("#")][-1]
        self.assertEqual(last, 'source-file -F "#{d:current_file}/spaces.conf"')

    def test_no_shell_commands_in_status_formats(self):
        for l in on_branch().splitlines():
            if l.startswith("set -g") and ("status-format" in l or "@space_" in l):
                self.assertNotIn("#(", l)

    def test_hook_slots_do_not_collide_with_tmux_conf(self):
        ours = set(re.findall(r"set-hook -g '([a-z-]+\[\d+\])'", SPACES.read_text()))
        theirs = set(re.findall(r"set-hook -g '?([a-z-]+(?:\[\d+\])?)", CONF.read_text()))
        theirs = {t if "[" in t else t + "[0]" for t in theirs}
        self.assertTrue(ours)
        self.assertFalse(ours & theirs, ours & theirs)
        self.assertFalse(any(o.endswith("[90]") for o in ours))  # tmux-mcp's poke hooks

    def test_else_unbinds_every_root_key_the_on_branch_binds(self):
        user = {f"User{i}" for i in range(27)}
        on = set(re.findall(r"^bind -n (\S+)", on_branch(), re.M)) - {"MouseDown1Status"}
        off = set(re.findall(r"^unbind -q -n (\S+)", else_branch(), re.M))
        self.assertEqual(on - user, off)
        # The WezTerm chords are bound in both branches (off: the old thing).
        self.assertEqual(on & user, user)
        self.assertEqual(set(re.findall(r"^bind -n (User\d+) ", else_branch(), re.M)), user)

    def test_user_keys_match_wezterm_and_are_set_in_both_modes(self):
        head = SPACES.read_text().split('\n%if "#{==:#{@spaces},on}"\n')[0]
        got = dict(re.findall(r'^set -s user-keys\[(\d+)\] "\\e(\[\d+~)"$', head, re.M))
        want = {str(i): CHORD[(row, n)][1:] for i, (row, n) in
                enumerate((row, n) for row in ("CTRL", "ALT", "CMD") for n in range(1, 10))}
        self.assertEqual(got, want)

    def test_no_meta_j_k(self):
        # Claude Code binds meta+j (app:toggleTerminal); next/prev use M-l/M-h.
        self.assertNotRegex(on_branch(), r"(?m)^bind -n M-[jk] ")

    def test_off_binds_prefix_meta_digits_to_windows(self):
        self.assertEqual([l.split()[5] for l in OFF_META if l.startswith("bind -N")], [f"M-{n}" for n in range(1, 10)])

    def test_wezterm_digit_rows(self):
        # CTRL+N → \e[911N~ (space), ALT+N → \e[912N~ (shell), CMD+N →
        # \e[913N~ (agent): one sequence per chord, no prefix, no bare ESC N.
        # The mixed chords do nothing.
        text = WEZTERM.read_text()
        loop = text[text.rindex("for i = 1, 9 do"):]
        self.assertIn('{ { "CTRL", "1" }, { "ALT", "2" }, { "CMD", "3" } }', loop)
        self.assertIn('SendString("\\x1b[91" .. row[2] .. d .. "~")', loop)
        self.assertIn('{ "CMD|ALT", "CMD|CTRL", "CTRL|ALT", "CMD|SHIFT" }', loop)
        self.assertIn("wezterm.action.Nop", loop)
        self.assertNotIn('mods = "CTRL" }', loop)   # no prefix (C-s) sent

    def test_script_refuses_without_tmux(self):
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        for args in (["sync"], ["close", "$1"], ["move-to", "@1", "$1"], ["shell", "1", "$0"]):
            r = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 3, args)
            self.assertIn("refusing", r.stderr)

    def test_roster_drops_home_windows(self):
        spec = importlib.util.spec_from_file_location("roster_for_spaces", ROSTER)
        R = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(R)
        row = lambda wid, home, state="": R.US.join(["main", "1", wid, "home", state] + [""] * 15
                                                    + ([home] if home is not None else []))
        got = R.parse_windows("\n".join([row("@1", "1"), row("@2", ""), row("@3", None), row("@4", "1", "running")]))
        self.assertEqual([w["id"] for w in got], ["@2", "@3", "@4"])   # a home running an agent is an agent


class Server(unittest.TestCase):
    """A -L test server with spaces.conf sourced and one attached client."""
    flag = "on"

    def setUp(self):
        self.sock = "spaces-test-" + uuid.uuid4().hex[:12]
        self.tmp = tempfile.mkdtemp(prefix="spaces-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env["TERM"] = "xterm-256color"
        self.env["XDG_STATE_HOME"] = os.path.join(self.tmp, "state")
        self.home_dir = os.path.realpath(self.tmp)
        self.registry = os.path.join(self.tmp, "spaces.tsv")
        self.path = None
        self.child = None
        self.addCleanup(self.reap)
        self.t("new-session", "-d", "-s", "main", "-x", str(COLS), "-y", str(ROWS), "-c", self.env["HOME"])
        self.path = self.t("display", "-p", "#{socket_path}")
        self.t("set", "-g", "exit-empty", "off")
        self.t("set", "-g", "base-index", "1")
        self.t("set", "-g", "renumber-windows", "on")
        self.t("set", "-g", "mouse", "on")
        self.t("set", "-g", "status-left", "")
        self.t("set", "-g", "status-right", "RIGHT")
        self.t("move-window", "-s", "main:0", "-t", "main:1")
        self.t("set", "-g", "@space_hidden_sessions", HIDDEN)
        self.t("set", "-g", "@space_sh", f"bash {SCRIPT}")
        self.t("set", "-g", "@space_dash", f"bash {DASH}")
        self.t("set", "-g", "@space_agent_ui", os.path.join(self.tmp, "no-agent-ui"))   # never the real one
        self.t("set", "-g", "@space_registry", self.registry)
        self.t("set", "-g", "@space_closer", "none")
        self.t("set", "-g", "@spaces", self.flag)
        self.source()
        if self.flag == "on":
            self.wait_home("main")

    # -- harness ---------------------------------------------------------
    def t(self, *args, check=True):
        r = subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", *args], env=self.env,
                           capture_output=True, text=True, timeout=10)
        if check and r.returncode:
            raise AssertionError(f"tmux {args}: {r.stderr}")
        return r.stdout.strip()

    def source(self):
        self.t("source-file", str(SPACES))

    def script_env(self):
        return dict(self.env, TMUX=f"{self.path},0,0")

    def script(self, *args, **kw):
        return subprocess.run(["bash", str(SCRIPT), *args], env=self.script_env(), capture_output=True,
                              text=True, timeout=20, **kw)

    def homes(self, session):
        return self.t("list-windows", "-t", session, "-F", "#{?#{@space_home},#{window_id},}").split()

    def dash_panes(self, session):
        """The dashboard panes of `session` (its home window's top pane)."""
        return self.t("list-panes", "-s", "-t", session, "-f", "#{@space_dash_pane}", "-F", "#{pane_id}").split()

    def wait_home(self, session):
        """Until `session` has its home (the sync runs in the background)."""
        self.wait(lambda: len(self.homes(session)), 1)
        return self.homes(session)[0]

    def attach(self, session="main"):
        pid, fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
                os.execvpe(TMUX, [TMUX, "-L", self.sock, "-f", "/dev/null", "attach", "-t", session], self.env)
            finally:
                os._exit(127)
        self.child, self.fd = pid, fd

        def drain():
            while True:
                try:
                    if not os.read(fd, 65536):
                        return
                except OSError:
                    return
        threading.Thread(target=drain, daemon=True).start()
        for _ in range(100):
            c = self.t("list-clients", "-F", "#{client_name}")
            if c:
                self.client = c.splitlines()[0]
                return
            time.sleep(0.05)
        self.fail("test client never attached")

    def reap(self):
        if self.child:
            try:
                os.kill(self.child, signal.SIGTERM)
                os.waitpid(self.child, 0)
            except OSError:
                pass
            try:
                os.close(self.fd)
            except OSError:
                pass
        subprocess.run([TMUX, "-L", self.sock, "kill-server"], env=self.env, capture_output=True, timeout=10)
        if self.path:
            Path(self.path).unlink(missing_ok=True)

    def new_session(self, name, path=None):
        args = ["new-session", "-d", "-s", name]
        if path:
            args += ["-c", path]
        self.t(*args)

    def new_window(self, session, state="", path="#{session_path}"):
        args = ["new-window", "-d", "-t", f"{session}:", "-P", "-F", "#{window_id}", "-c", path]
        wid = self.t(*args)
        if state:
            self.t("set", "-w", "-t", wid, "@agent_state", state)
        return wid

    def wait(self, fn, want, timeout=4.0):
        deadline = time.time() + timeout
        got = fn()
        while got != want and time.time() < deadline:
            time.sleep(0.03)
            got = fn()
        self.assertEqual(got, want)

    def key(self, *keys):
        self.t("send-keys", "-K", "-c", self.client, *keys)

    def on(self, fmt="#{session_name}:#{window_id}"):
        # list-clients expands each row in THAT client's session/window.
        # (`display -c CLIENT -p` does not: -c only picks where to display, and
        # the format target is tmux's "current" session, often the newest.)
        rows = self.t("list-clients", "-F", "#{client_name}\t" + fmt).splitlines()
        return next(r.split("\t", 1)[1] for r in rows if r.startswith(self.client + "\t"))

    def snapshot(self):
        return (self.t("list-keys"), self.t("list-keys", "-N", "-a"), self.t("show", "-g", "status"),
                self.t("show", "-g", "status-format"))


@unittest.skipUnless(TMUX, "tmux not installed")
class FlagOff(Server):
    flag = "off"

    def test_off_and_on_off_round_trip_leave_todays_keys_and_status(self):
        # A fresh server plus tmux.conf's own bindings is "today", plus the one
        # deliberate change: prefix M-1..9 select window N (WezTerm's CMD+N).
        today_conf = os.path.join(self.tmp, "today.conf")
        Path(today_conf).write_text("\n".join(TODAY_BINDS + PICKER_BINDS) + "\n")
        base_conf = os.path.join(self.tmp, "base.conf")
        Path(base_conf).write_text("\n".join(TODAY_BINDS + PICKER_BINDS + OFF_META) + "\n")
        self.t("source-file", today_conf)
        # setUp already sourced spaces.conf with @spaces off, before TODAY_BINDS;
        # re-source to be in tmux.conf's order (binds, then spaces.conf).
        self.source()
        off = self.snapshot()
        other = "spaces-test-" + uuid.uuid4().hex[:12]
        try:
            ref = subprocess.run([TMUX, "-L", other, "-f", "/dev/null", "new-session", "-d", "-x", "80", "-y", "20"],
                                 env=self.env, capture_output=True, text=True, timeout=10)
            self.assertEqual(ref.returncode, 0, ref.stderr)
            run = lambda *a: subprocess.run([TMUX, "-L", other, *a], env=self.env, capture_output=True,
                                            text=True, timeout=10).stdout.strip()
            run("source-file", base_conf)
            baseline = (run("list-keys"), run("list-keys", "-N", "-a"), run("show", "-g", "status"),
                        run("show", "-g", "status-format"))
            sp = run("display", "-p", "#{socket_path}")
        finally:
            subprocess.run([TMUX, "-L", other, "kill-server"], env=self.env, capture_output=True, timeout=10)
        if sp:
            Path(sp).unlink(missing_ok=True)
        self.assertEqual(off, baseline)

        self.t("set", "-g", "@spaces", "on")
        self.source()
        on = self.snapshot()
        self.assertNotEqual(on[0], baseline[0])
        self.assertEqual(self.t("show", "-gv", "status"), "on")                # ONE status line
        self.assertEqual(len(self.t("show", "-g", "status-format").splitlines()),
                         len(baseline[3].splitlines()))                        # no stray line 1 override
        self.assertNotIn("status-format[1] \"#[align=left]#{?#{@space_hidden}", self.t("show", "-g", "status-format"))
        self.assertEqual(self.t("list-keys", "-T", "prefix", "a", check=False), "")    # picker gone
        self.assertEqual(self.t("list-keys", "-T", "prefix", "C-a", check=False), "")
        bound = lambda table: next(l for l in self.t("list-keys", "-T", table).splitlines()
                                   if re.match(rf"bind-key +-T {table} +M-1 ", l))
        self.assertIn(" agent 1 ", bound("prefix"))
        self.assertIn(" shell 1 ", bound("root"))

        self.t("set", "-g", "@spaces", "off")
        self.t("source-file", today_conf)   # prefix r re-runs tmux.conf, then spaces.conf
        self.source()
        self.assertEqual(self.snapshot(), baseline)

    def test_session_module_only_while_spaces_are_off(self):
        # tmux.conf's module list for catppuccin's status-right: the space tab
        # names the session while spaces are on, so its module goes.
        f = os.path.join(self.tmp, "modules.conf")
        Path(f).write_text(conf_line("set -gF @catppuccin_status_modules_right ") + "\n")
        mods = lambda: self.t("show", "-gv", "@catppuccin_status_modules_right").split()
        self.t("source-file", f)
        self.assertEqual(mods(), ["directory", "codex_session", "codex_weekly", "codex_scoped", "session"])
        self.t("set", "-g", "@spaces", "on")
        self.t("source-file", f)
        self.assertEqual(mods(), ["directory", "codex_session", "codex_weekly", "codex_scoped"])

    def test_wezterm_chords_do_the_old_thing_when_off(self):
        # Written into the client's terminal, as WezTerm sends them.
        w1 = self.t("display", "-t", "main:1", "-p", "#{window_id}")
        w2 = self.t("new-window", "-d", "-t", "main:", "-P", "-F", "#{window_id}")
        w3 = self.t("new-window", "-d", "-t", "main:", "-P", "-F", "#{window_id}", "cat -v")
        self.attach()
        os.write(self.fd, CHORD[("CMD", 2)].encode())             # CMD+2: window 2
        self.wait(lambda: self.on("#{window_id}"), w2)
        os.write(self.fd, CHORD[("CTRL", 3)].encode())            # CTRL+3: window 3
        self.wait(lambda: self.on("#{window_id}"), w3)
        os.write(self.fd, CHORD[("ALT", 1)].encode())             # OPT+1: M-1 to the pane
        self.wait(lambda: "^[1" in self.t("capture-pane", "-p", "-t", w3), True)
        self.assertEqual(self.on("#{window_id}"), w3)
        os.write(self.fd, CHORD[("CTRL", 1)].encode())
        self.wait(lambda: self.on("#{window_id}"), w1)

    def test_hidden_marker_is_set_even_when_off(self):
        self.new_session("agents", "/")
        self.wait(lambda: self.t("show", "-t", "agents", "-v", "@space_hidden"), "1")

    def test_off_never_runs_the_sidebar_repair(self):
        log = os.path.join(self.tmp, "agent-ui.log")
        f = os.path.join(self.tmp, "agent-ui")
        Path(f).write_text(f'#!/bin/sh\necho "$*" >> {log}\n')
        os.chmod(f, 0o755)
        self.t("set", "-g", "@space_agent_ui", f)
        self.source()
        self.assertNotIn("run-shell", self.t("show-hooks", "-g", "client-attached[21]", check=False))
        self.assertFalse(os.path.exists(log))

    def test_off_has_no_jump_key_and_keeps_the_status_line(self):
        self.assertEqual(self.t("list-keys", "-T", "root", "M-f", check=False), "")
        self.assertEqual(self.t("list-keys", "-T", "jump", check=False), "")
        self.t("set", "-g", "@sidebar_open", "1")
        self.source()
        self.assertEqual(self.t("show", "-gv", "status"), "on")

    def test_no_home_windows_or_registry_when_off(self):
        self.new_session("proj", "/usr")
        self.wait(lambda: self.t("show", "-t", "proj", "-qv", "@space_index"), "2")
        r = self.script("sync", "load")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.t("list-windows", "-a", "-F", "#{@space_home}").split(), [])
        self.assertFalse(os.path.exists(self.registry))


@unittest.skipUnless(TMUX, "tmux not installed")
class Spaces(Server):
    def setUp(self):
        super().setUp()
        self.proj = os.path.join(self.tmp, "my.proj")
        os.mkdir(self.proj)
        self.proj = os.path.realpath(self.proj)
        self.new_session("agents", "/")
        self.new_session("proj", self.proj)
        self.new_session("scratch", self.proj)
        self.new_session("zed", "/usr")
        # A session's first shell became its home (shell 1): wait for that
        # before adding windows, or the sync would insert a new one instead.
        for s in ("proj", "zed"):
            self.wait_home(s)
        # main: 1 home shell, 2 agent(running), 3 shell, 4 agent(needs-input), 5 agent(idle)
        self.M1 = self.t("display", "-t", "main:1", "-p", "#{window_id}")
        self.assertEqual(self.homes("main"), [self.M1])
        self.M2 = self.new_window("main", "running")
        self.M3 = self.new_window("main")
        self.M4 = self.new_window("main", "needs-input")
        self.M5 = self.new_window("main", "idle")
        self.P1 = self.t("display", "-t", "proj:", "-p", "#{window_id}")
        self.P2 = self.new_window("proj", "done")
        for _ in range(3):
            self.new_window("agents")
        self.wait(self.indexes, "main=1 proj=2 zed=3")
        self.attach()
        self.t("select-window", "-t", "main:1")

    def indexes(self):
        rows = self.t("list-sessions", "-F", "#{session_name}=#{@space_index}#{?#{@space_hidden},H,}")
        return " ".join(sorted(r for r in rows.split() if not r.endswith("H")))

    def registered(self):
        try:
            return [l.split("\t") for l in Path(self.registry).read_text().splitlines()]
        except FileNotFoundError:
            return []

    # -- hidden + numbering ----------------------------------------------
    def test_hidden_sessions_marked_and_unnumbered(self):
        for s in ("agents", "scratch"):
            self.assertEqual(self.t("show", "-t", s, "-v", "@space_hidden"), "1")
            self.assertEqual(self.t("show", "-t", s, "-qv", "@space_index"), "")
        for s in ("main", "proj", "zed"):
            self.assertEqual(self.t("show", "-t", s, "-qv", "@space_hidden"), "")

    def test_close_and_rename_renumber(self):
        self.t("kill-session", "-t", "proj")
        self.wait(self.indexes, "main=1 zed=2")
        self.t("rename-session", "-t", "zed", "stash")   # renamed INTO the hidden list
        self.wait(self.indexes, "main=1")
        self.t("rename-session", "-t", "stash", "zz")
        self.wait(self.indexes, "main=1 zz=2")

    def test_main_is_first_even_when_created_later(self):
        self.t("rename-session", "-t", "main", "old")
        self.new_session("main")
        self.wait(self.indexes, "main=1 old=2 proj=3 zed=4")

    # -- home -----------------------------------------------------------------
    def test_every_space_has_one_home_shell_at_its_lowest_window(self):
        for s in ("main", "proj", "zed"):
            rows = self.t("list-windows", "-t", s, "-F", "#{@space_home}|#{window_name}").splitlines()
            self.assertEqual(rows[0], "1|home", s)
            self.assertEqual([r for r in rows if r.startswith("1|")], [rows[0]], s)
            # remain-on-exit is the home PANE's, not the window's; above it,
            # the dashboard pane.
            self.wait(lambda: self.t("list-panes", "-t", f"{s}:^", "-F", "#{@space_home_pane}|#{@space_dash_pane}|#{remain-on-exit}").split(),
                      ["|1|on", "1||on"])
            self.assertEqual(self.t("show", "-wqv", "-t", f"{s}:^", "remain-on-exit"), "")
        for s in ("agents", "scratch"):
            self.assertEqual(self.homes(s), [], s)

    def test_closing_the_last_tab_keeps_the_space_and_lands_on_home(self):
        self.t("switch-client", "-c", self.client, "-t", "zed")
        home = self.homes("zed")[0]
        w = self.new_window("zed")
        self.t("select-window", "-t", w)
        self.wait(lambda: self.on("#{window_id}"), w)
        self.t("kill-window", "-t", w)
        self.wait(lambda: self.on(), f"zed:{home}")
        time.sleep(0.3)
        self.assertIn("zed", self.t("list-sessions", "-F", "#{session_name}").split())

    def test_home_shell_exit_respawns_in_place(self):
        self.t("send-keys", "-t", self.M1, "exit", "Enter")
        self.wait(lambda: self.t("display", "-t", self.M1, "-p", "#{pane_dead}"), "0")
        time.sleep(0.5)
        self.wait(lambda: self.t("display", "-t", self.M1, "-p", "#{pane_dead}|#{@space_home}|#{window_index}"), "0|1|1")

    def test_x_and_cmd_shift_w_refuse_on_home(self):
        self.key("C-b", "x")
        time.sleep(0.2)
        self.key("y")
        self.key("C-b", "C-l")
        time.sleep(0.5)
        self.assertEqual(self.homes("main"), [self.M1])
        self.assertEqual(self.t("display", "-t", self.M1, "-p", "#{pane_dead}"), "0")

    def test_a_split_inside_home_is_an_ordinary_pane(self):
        home_pane = self.t("display", "-t", self.M1, "-p", "#{pane_id}")
        split = self.t("split-window", "-d", "-t", self.M1, "-P", "-F", "#{pane_id}")
        self.assertEqual(self.t("display", "-t", split, "-p", "#{@space_home_pane}|#{remain-on-exit}"), "|off")
        # Its shell exiting closes it (no remain-on-exit, no respawn); the
        # home pane stays, untouched.
        self.t("send-keys", "-t", split, "exit", "Enter")
        shells = lambda f: self.t("list-panes", "-t", self.M1, "-f", "#{!:#{@space_dash_pane}}", "-F", f)
        self.wait(lambda: shells("#{pane_id}"), home_pane)
        time.sleep(0.5)
        self.assertEqual(shells("#{pane_id}|#{pane_dead}"), f"{home_pane}|0")

    def test_x_and_cmd_shift_w_refuse_only_on_the_home_pane(self):
        keys = self.t("list-keys", "-T", "prefix").splitlines()
        for k in ("x", "C-l"):
            line = next(l for l in keys if re.match(rf"bind-key +-T prefix +{k} ", l))
            self.assertTrue(line.split(None, 4)[4].startswith('if-shell -F "#{||:#{@space_home_pane},#{@space_dash_pane}}"'), line)

    def test_home_moves_back_in_front_when_a_window_lands_before_it(self):
        w = self.t("new-window", "-d", "-b", "-t", self.M1, "-P", "-F", "#{window_id}")   # as closed-tabs reopens
        self.wait(lambda: self.t("display", "-t", "main:^", "-p", "#{window_id}"), self.M1)
        self.assertEqual(self.t("display", "-t", w, "-p", "#{window_index}"), "2")
        self.key("M-1"); self.wait(self.on, f"main:{self.M1}")                       # Option-1 = home

    def test_window_hooks_sync_only_for_a_space_whatever_the_client_is_on(self):
        log = os.path.join(self.tmp, "calls.log")
        wrap = os.path.join(self.tmp, "wrap.sh")
        Path(wrap).write_text(f'echo "$*" >> {log}\nexec bash {SCRIPT} "$@"\n')
        self.t("set", "-g", "@space_sh", f"bash {wrap}")
        self.t("switch-client", "-c", self.client, "-t", "agents")    # the client is in a hidden session
        self.wait(lambda: self.on("#{session_name}"), "agents")
        a = self.t("list-windows", "-t", "agents", "-F", "#{window_id}").split()[-1]
        self.t("kill-window", "-t", a)                                # agents churn: no sync
        time.sleep(0.6)
        self.assertFalse(os.path.exists(log) and "sync" in Path(log).read_text())
        self.t("kill-window", "-t", self.P1)                          # a space's home: repaired
        self.wait(lambda: len(self.homes("proj")), 1)
        self.assertIn("sync", Path(log).read_text())

    def test_killed_home_comes_back_at_the_space_folder(self):
        self.t("kill-window", "-t", self.P1)
        self.wait(lambda: len(self.homes("proj")), 1)
        new = self.homes("proj")[0]
        self.assertNotEqual(new, self.P1)
        self.wait(lambda: self.t("display", "-t", new, "-p", "#{window_index}|#{pane_current_path}"), f"1|{self.proj}")

    # -- close a space --------------------------------------------------------
    def test_close_prompt_counts_agents_and_shells(self):
        self.assertEqual(self.t("display", "-t", "main:", "-p", "#{E:@space_close_prompt}"),
                         "close space main: 3 agents, 2 shells? (y/n)")
        self.assertEqual(self.t("display", "-t", "proj:", "-p", "#{E:@space_close_prompt}"),
                         "close space proj: 1 agent, 1 shell? (y/n)")

    def test_close_key_closes_through_the_closer_and_unregisters(self):
        log = os.path.join(self.tmp, "closed.log")
        rec = os.path.join(self.tmp, "rec.sh")
        Path(rec).write_text(f'echo "$1" >> {log}\ntmux kill-pane -t "$1"\n')
        self.t("set", "-g", "@space_closer", f"bash {rec}")
        self.wait(lambda: len(self.dash_panes("proj")), 1)
        panes = sorted(self.t("list-panes", "-s", "-t", "proj", "-f", "#{!:#{@space_dash_pane}}", "-F", "#{pane_id}").split())   # no dashboard
        self.wait(lambda: [r[0] for r in self.registered()], ["main", "proj", "zed"])
        self.t("switch-client", "-c", self.client, "-t", "proj")
        self.wait(lambda: self.on("#{session_name}"), "proj")
        names = lambda: self.t("list-sessions", "-F", "#{session_name}").split()
        for _ in range(5):
            self.key("C-b", "Q")
            time.sleep(0.3)
            self.key("y")
            time.sleep(0.5)
            if "proj" not in names():
                break
        self.wait(lambda: "proj" in names(), False)
        self.assertEqual(self.on("#{session_name}"), "main")          # the client moved, not detached
        self.assertEqual(sorted(Path(log).read_text().split()), panes)
        self.wait(lambda: [r[0] for r in self.registered()], ["main", "zed"])
        self.wait(self.indexes, "main=1 zed=2")

    def test_close_refuses_main_and_hidden(self):
        sid = self.t("display", "-t", "main", "-p", "#{session_id}")
        self.assertNotEqual(self.script("close", sid, self.client).returncode, 0)
        sid = self.t("display", "-t", "scratch", "-p", "#{session_id}")
        self.assertNotEqual(self.script("close", sid, self.client).returncode, 0)
        self.key("C-b", "Q")
        time.sleep(0.3)
        self.key("y")
        time.sleep(0.3)
        self.assertEqual(set(self.t("list-sessions", "-F", "#{session_name}").split()),
                         {"main", "agents", "proj", "scratch", "zed"})

    # -- registry -------------------------------------------------------------
    def test_registry_recreates_missing_spaces_with_folder_and_order(self):
        self.wait(lambda: [r[0] for r in self.registered()], ["main", "proj", "zed"])
        gone = os.path.realpath(os.path.join(self.tmp, "gone"))
        os.mkdir(gone)
        Path(self.registry).write_text(
            f"main\t{self.env['HOME']}\ngone\t{gone}\nzed\t{self.home_dir}\nproj\t{self.proj}\n")
        r = self.script("sync", "load")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.wait(lambda: self.t("list-windows", "-t", "gone", "-F", "#{@space_home}|#{window_name}", check=False), "1|home")
        self.assertEqual(self.t("display", "-t", "gone", "-p", "#{session_path}"), gone)
        self.assertEqual(self.t("display", "-t", "zed", "-p", "#{session_path}"), self.home_dir)  # registry's folder
        self.wait(self.indexes, "gone=2 main=1 proj=4 zed=3")
        self.t("switch-client", "-c", self.client, "-t", "proj")
        self.wait(lambda: re.findall(CHIP_RE, self.status()[1]), [("4", "proj")])

    def test_renamed_space_keeps_its_place_in_the_registry(self):
        self.wait(lambda: [r[0] for r in self.registered()], ["main", "proj", "zed"])
        self.t("rename-session", "-t", "proj", "pro2")
        self.wait(lambda: [r[0] for r in self.registered()], ["main", "pro2", "zed"])
        self.assertEqual(self.registered()[1][2], "#cba6f7")                  # and its colour
        self.assertEqual(self.t("show", "-t", "pro2", "-qv", "@space_color"), "#cba6f7")

    # -- colours --------------------------------------------------------------
    def colors(self):
        rows = self.t("list-sessions", "-F", "#{session_name}=#{@space_color}")
        return dict(r.split("=", 1) for r in rows.split())

    def test_each_space_gets_a_stable_colour_kept_in_the_registry(self):
        # main is lavender; the others the least-used palette colour, in order.
        self.wait(self.registered, [["main", self.env["HOME"], "#b4befe"], ["proj", self.proj, "#cba6f7"],
                                    ["zed", "/usr", "#94e2d5"]])
        self.wait(self.colors, {"main": "#b4befe", "proj": "#cba6f7", "zed": "#94e2d5",
                                "agents": "", "scratch": ""})   # hidden: none
        self.new_session("n4", "/usr")
        self.wait(lambda: self.colors().get("n4"), "#f5c2e7")
        # A freed colour is the next one handed out.
        self.assertEqual(self.script("close", self.sid("proj"), self.client).returncode, 0)
        self.wait(lambda: [r[0] for r in self.registered()], ["main", "zed", "n4"])
        self.new_session("n5", "/usr")
        self.wait(lambda: self.colors().get("n5"), "#cba6f7")
        self.assertEqual(self.colors()["zed"], "#94e2d5")                    # nobody else moved

    def test_colours_survive_a_restart_and_old_lines_get_one(self):
        self.wait(lambda: [r[0] for r in self.registered()], ["main", "proj", "zed"])
        gone = os.path.realpath(os.path.join(self.tmp, "gone"))
        os.mkdir(gone)
        # gone: a colour from the registry (a restart); zed: a line from before
        # the colour column; main: a wrong colour (main's is fixed).
        Path(self.registry).write_text(
            f"main\t{self.env['HOME']}\t#f5e0dc\nproj\t{self.proj}\t#cba6f7\ngone\t{gone}\t#74c7ec\nzed\t/usr\n")
        self.assertEqual(self.script("sync", "load").returncode, 0)
        self.wait(lambda: self.colors().get("gone"), "#74c7ec")
        self.wait(lambda: [r[2] for r in self.registered()], ["#b4befe", "#cba6f7", "#74c7ec", "#94e2d5"])
        self.wait(self.colors, {"main": "#b4befe", "proj": "#cba6f7", "gone": "#74c7ec", "zed": "#94e2d5",
                                "agents": "", "scratch": ""})

    # -- the home dashboard ---------------------------------------------------
    def dash(self, session):
        self.wait(lambda: len(self.dash_panes(session)), 1)
        return self.dash_panes(session)[0]

    def dash_text(self, pane):
        return self.t("capture-pane", "-p", "-t", pane)

    def test_home_is_a_dashboard_above_the_home_shell(self):
        for s in ("main", "proj", "zed"):
            d = self.dash(s)
            home = self.homes(s)[0]
            self.wait(lambda: self.t("display", "-t", d, "-p", "#{pane_height}"), "9")
            rows = self.t("list-panes", "-t", home, "-F",
                          "#{pane_id}|#{@space_dash_pane}|#{@space_home_pane}|#{pane_active}|#{pane_top}|#{pane_height}|#{pane_title}").splitlines()
            self.assertEqual(len(rows), 2, s)
            self.assertEqual(rows[0].split("|")[:7], [d, "1", "", "0", "0", "9", "space-dash"], s)  # top, 9 rows
            # (kept at 9 when the window is resized: the dashboard resizes itself)
            self.assertEqual(rows[1].split("|")[1:4], ["", "1", "1"], s)    # the shell: focused
            self.assertIn("space-dash", self.t("display", "-t", d, "-p", "#{pane_start_command}"))
            self.assertEqual(self.t("show", "-p", "-t", d, "-v", "remain-on-exit"), "on")
        # Not a shell, not a tab: shells and the close prompt count windows.
        self.assertEqual(self.t("display", "-t", "main:", "-p", "#{E:@space_close_prompt}"),
                         "close space main: 3 agents, 2 shells? (y/n)")
        self.assertEqual([int(i) for i in re.findall(r"█(\d+)█", self.status()[1])], [1, 2])
        # Its content: the name (large), key, folder, git, agents, counts, hint.
        d = self.dash("main")
        self.t("set", "-w", "-t", self.M4, "@agent_summary", "tmux/fix the bar")
        self.t("set", "-w", "-t", self.M4, "@agent_key", "q")
        self.new_session("stash", "/")
        self.t("move-window", "-s", self.M3, "-t", "stash:")          # parked from main
        self.t("set", "-w", "-t", self.M3, "@stash_origin", "main")
        self.t("respawn-pane", "-k", "-t", d, f"bash {DASH}")          # a fresh draw (no notice needed)
        self.wait(lambda: "◉ ⌘2 q  tmux/fix the bar" in self.dash_text(d), True, timeout=6)
        text = self.dash_text(d)
        self.assertIn("█▄█", text)                                      # "main", large: half blocks
        self.assertIn("⌃1  ~", text)
        self.assertIn("3 agents · 1 shell · 1 parked", text)
        self.assertIn("◐ ⌘1", text)                                    # running
        self.assertIn("○ ⌘3", text)                                    # idle
        self.assertIn("⌥F jump", text)

    def test_dashboard_never_takes_the_focus(self):
        d = self.dash("main")
        shell = self.t("list-panes", "-t", self.M1, "-f", "#{@space_home_pane}", "-F", "#{pane_id}")
        self.t("select-pane", "-t", d)
        self.wait(lambda: self.t("display", "-t", self.M1, "-p", "#{pane_id}"), shell)
        self.key("C-b", "Up")                                           # select-pane -U from the shell
        time.sleep(0.3)
        self.assertEqual(self.t("display", "-t", self.M1, "-p", "#{pane_id}"), shell)
        # A split closing does not leave it focused either.
        split = self.t("split-window", "-t", shell, "-P", "-F", "#{pane_id}")
        self.t("kill-pane", "-t", split)
        self.wait(lambda: self.t("display", "-t", self.M1, "-p", "#{pane_id}"), shell)
        # x / C-l refuse it like the home shell.
        self.key("C-b", "x")
        time.sleep(0.2)
        self.key("y")
        time.sleep(0.4)
        self.assertEqual(self.dash_panes("main"), [d])

    def test_dashboard_respawns_and_sync_never_adds_a_second(self):
        d = self.dash("proj")
        self.t("respawn-pane", "-k", "-t", d, "exit 0")                 # its process ends
        self.wait(lambda: "space-dash" in self.t("display", "-t", d, "-p", "#{pane_start_command}"), True)
        self.wait(lambda: self.t("display", "-t", d, "-p", "#{pane_dead}"), "0")
        for _ in range(3):
            self.assertEqual(self.script("sync").returncode, 0)
        self.assertEqual(self.dash_panes("proj"), [d])
        self.assertEqual(len(self.t("list-panes", "-t", self.P1).splitlines()), 2)

    def test_dashboard_whose_pane_is_gone_exits(self):
        # Its pane (or server) gone without the SIGHUP reaching it: it must
        # not linger as an orphan.
        env = dict(self.script_env(), TMUX_PANE="%999")
        r = subprocess.run(["bash", str(DASH)], env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        self.assertEqual(r.returncode, 0)

    def test_a_failing_command_never_puts_a_dashboard_in_an_agent_window(self):
        # A tmux command list stops at its first failing command (a pane that
        # vanished between sync's read and its write). A tmux stand-in makes
        # EVERY multi-command call fail that way, first thing.
        wrap = os.path.join(self.tmp, "tmux-failing")
        Path(wrap).write_text("#!/bin/sh\n"
                              'for a in "$@"; do [ "$a" = ";" ] && exec ' + TMUX + ' kill-pane -t %99999 \\; "$@"; done\n'
                              "exec " + TMUX + ' "$@"\n')
        os.chmod(wrap, 0o755)
        env = dict(self.script_env(), SPACE_TMUX_BIN=wrap)
        subprocess.run([wrap, "set", "-g", "@probe_a", "1", ";", "set", "-g", "@probe_b", "1"], env=env, capture_output=True)
        self.assertEqual(self.t("show", "-gqv", "@probe_b"), "")         # the stand-in does fail them
        # A space whose only window is an agent, its home gone: sync must
        # make a new home (lowest) and put the dashboard THERE.
        self.new_session("ag", "/usr")
        home = self.wait_home("ag")
        self.dash("ag")
        self.t("set", "-g", "@space_sh", "true")                         # no hook syncs meanwhile
        self.wait(lambda: subprocess.run(["pgrep", "-f", f"{SCRIPT} sync"], capture_output=True).returncode, 1,
                  timeout=10)                                            # nor any still in flight
        A = self.new_window("ag", "running")
        self.t("kill-window", "-t", home)
        self.assertEqual(self.t("display", "-t", "ag:^", "-p", "#{window_id}"), A)   # the agent is lowest
        r = subprocess.run(["bash", str(SCRIPT), "sync"], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.t("set", "-g", "@space_sh", f"bash {SCRIPT}")
        self.assertEqual(self.t("list-panes", "-t", A, "-F", "x#{@space_dash_pane}").split(), ["x"])   # one pane, no dashboard
        new = self.homes("ag")
        self.assertEqual(len(new), 1)
        self.assertEqual(self.t("display", "-t", "ag:^", "-p", "#{window_id}"), new[0])    # the home made anyway
        self.assertEqual(len(self.dash_panes("ag")), 1)
        # Wherever a dashboard is, it is in a home.
        self.assertEqual(set(self.t("list-panes", "-a", "-f", "#{@space_dash_pane}", "-F", "#{@space_home}").split()), {"1"})

    def test_sync_adds_the_dashboard_to_an_old_home_once(self):
        # A home from before the dashboard: one pane.
        d = self.dash("zed")
        self.t("kill-pane", "-t", d)
        home = self.homes("zed")[0]
        # (a sync still running from setUp's window hooks may already be adding
        # it back: that is fine, the point is that it never adds two)
        runs =[subprocess.Popen(["bash", str(SCRIPT), "sync"], env=self.script_env()) for _ in range(3)]
        for r in runs:
            r.wait(timeout=20)                                          # three at once: the lock
        self.wait(lambda: len(self.dash_panes("zed")), 1)
        self.assertEqual(len(self.t("list-panes", "-t", home).splitlines()), 2)
        self.assertEqual(self.t("list-panes", "-t", home, "-F", "#{pane_top}:#{@space_dash_pane}|#{@space_home_pane}").split(),
                         ["0:1|", "10:|1"])                             # dashboard on top, shell below
        self.assertEqual(self.t("display", "-t", home, "-p", "#{@space_home_pane}"), "1")   # focus on the shell

    def test_a_restored_home_window_gets_its_dashboard_back_not_a_second_shell(self):
        # What resurrect brings back: a window NAMED home, its old dashboard a
        # plain shell that kept only its title, the shell below; no options.
        self.new_session("rest", "/usr")
        self.wait_home("rest")
        home = self.homes("rest")[0]
        old = self.dash("rest")
        self.t("set", "-wu", "-t", home, "@space_home")
        self.t("set", "-pu", "-t", old, "@space_dash_pane")
        self.t("respawn-pane", "-k", "-t", old)                         # a shell now, title kept
        self.t("select-pane", "-t", old, "-T", "space-dash")
        shell = self.t("list-panes", "-t", home, "-f", "#{@space_home_pane}", "-F", "#{pane_id}")
        self.t("set", "-pu", "-t", shell, "@space_home_pane")
        self.assertEqual(self.script("sync").returncode, 0)
        self.wait(lambda: self.dash_panes("rest"), [old])
        self.assertIn("space-dash", self.t("display", "-t", old, "-p", "#{pane_start_command}"))
        self.assertEqual(self.t("list-panes", "-t", home, "-f", "#{@space_home_pane}", "-F", "#{pane_id}"), shell)
        self.assertEqual(len(self.t("list-panes", "-t", home).splitlines()), 2)

    # -- the sidebar's status line -------------------------------------------
    def test_reload_keeps_the_status_line_off_while_a_sidebar_is_open(self):
        f = os.path.join(self.tmp, "agent-ui-alive")                   # its repair finds them alive
        Path(f).write_text("#!/bin/sh\nexit 0\n")
        os.chmod(f, 0o755)
        self.t("set", "-g", "@space_agent_ui", f)
        self.t("set", "-g", "@sidebar_open", "2")
        self.source()
        self.assertEqual(self.t("show", "-gv", "status"), "off")
        self.t("set", "-g", "@sidebar_open", "0")
        self.source()
        self.assertEqual(self.t("show", "-gv", "status"), "on")
        self.t("set", "-g", "@sidebar_open", "1")
        self.t("set", "-g", "@spaces", "off")
        self.source()
        self.assertEqual(self.t("show", "-gv", "status"), "on")          # off: always on

    def fake_agent_ui(self):
        """A stand-in agent-ui: logs its arguments, and `sidebar --repair`
        does what the real one does with no sidebar alive (count 0, line on)."""
        log = os.path.join(self.tmp, "agent-ui.log")
        f = os.path.join(self.tmp, "agent-ui")
        Path(f).write_text(f'#!/bin/sh\necho "$*" >> {log}\n'
                           'tmux set -g @sidebar_open 0 \\; set -g status on\necho 0\n')
        os.chmod(f, 0o755)
        self.t("set", "-g", "@space_agent_ui", f)
        return lambda: Path(log).read_text().splitlines() if os.path.exists(log) else []

    def test_a_stale_sidebar_count_is_repaired_before_the_gate(self):
        calls = self.fake_agent_ui()
        self.t("set", "-g", "@sidebar_open", "2")      # sidebars that died by SIGKILL
        self.t("set", "-g", "status", "off")
        self.source()
        self.assertEqual(calls(), ["sidebar --repair"])
        self.assertEqual(self.t("show", "-gv", "@sidebar_open"), "0")
        self.assertEqual(self.t("show", "-gv", "status"), "on")

    def test_no_agent_ui_binary_means_no_sidebar(self):
        self.t("set", "-g", "@space_agent_ui", os.path.join(self.tmp, "missing"))
        self.t("set", "-g", "@sidebar_open", "2")
        self.source()
        self.assertEqual(self.t("show", "-gqv", "@sidebar_open"), "")
        self.assertEqual(self.t("show", "-gv", "status"), "on")

    def test_a_client_attaching_repairs_the_sidebar_count(self):
        calls = self.fake_agent_ui()
        self.source()
        self.assertEqual(calls(), ["sidebar --repair"])                   # the load
        self.t("set", "-g", "@sidebar_open", "1")
        os.kill(self.child, signal.SIGTERM)
        os.waitpid(self.child, 0)
        self.child = None
        self.attach()
        self.wait(calls, ["sidebar --repair"] * 2)                        # client-attached[21]
        self.wait(lambda: self.t("show", "-gv", "@sidebar_open"), "0")

    # -- Option-F: jump to an agent by its letter -------------------------------
    def table(self):
        return self.on("#{client_key_table}")

    def test_option_f_jumps_to_the_agent_with_that_letter_in_any_space(self):
        self.t("set", "-w", "-t", self.P2, "@agent_key", "p")
        self.t("set", "-w", "-t", self.M5, "@agent_key", "m")
        hid = self.t("list-windows", "-t", "agents", "-F", "#{window_id}").split()[0]
        self.t("set", "-w", "-t", hid, "@agent_key", "h")               # in a hidden session: never
        self.key("M-f")
        self.wait(self.table, "jump")
        self.key("p")
        self.wait(self.on, f"proj:{self.P2}")                            # across spaces
        self.assertEqual(self.table(), "root")
        self.key("M-f", "m")
        self.wait(self.on, f"main:{self.M5}")
        here = self.on()
        for k in ("h", "z"):                                             # hidden, unknown: stays
            self.key("M-f", k)
            time.sleep(0.3)
            self.assertEqual(self.on(), here)
            self.assertEqual(self.table(), "root")

    def test_option_f_escape_or_another_key_cancels_and_is_swallowed(self):
        w = self.new_window("main", path=self.env["HOME"])
        self.t("respawn-pane", "-k", "-t", w, "cat -v")
        self.t("select-window", "-t", w)
        self.wait(lambda: self.on("#{window_id}"), w)
        self.t("set", "-w", "-t", self.M5, "@agent_key", "m")
        self.key("M-f")
        self.wait(self.table, "jump")
        self.key("Escape")
        self.wait(self.table, "root")
        self.key("M-f", "5")                                             # not a letter: cancels
        self.wait(self.table, "root")
        self.key("M-f", "M")                                             # uppercase: cancels
        self.wait(self.table, "root")
        self.key("m")                                                    # jump mode is over: typed
        self.wait(lambda: "m" in self.t("capture-pane", "-p", "-t", w), True)
        text = self.t("capture-pane", "-p", "-t", w)
        self.assertNotIn("5", text)
        self.assertNotIn("M", text)
        self.assertEqual(self.on("#{window_id}"), w)

    # -- the prefix: a root binding, so pressing it can post a notice ---------
    def test_prefix_as_a_root_binding_keeps_every_prefix_use(self):
        lines = [conf_line("set -g prefix "), conf_line("bind -n C-s ")]
        self.assertEqual(lines[0], "set -g prefix None")
        self.assertIn("switch-client -T prefix", lines[1])
        self.assertIn('notifyutil -p com.mackhaymond.tmux.focus', lines[1])
        f = os.path.join(self.tmp, "prefix.conf")
        Path(f).write_text("\n".join(lines + [
            "bind C-s set -g @saved 1",                                  # stands in for the resurrect save
            "bind -r Up set -gF @ups '#{@ups}x'"]) + "\n")
        self.t("source-file", f)
        C_S = b"\x13"
        os.write(self.fd, C_S)
        self.wait(self.table, "prefix")
        self.assertEqual(self.on("#{client_prefix}"), "1")              # the chip still sees it
        os.write(self.fd, b"3")                                          # prefix 3: space 3
        self.wait(lambda: self.on("#{session_name}"), "zed")
        os.write(self.fd, C_S); time.sleep(0.15); os.write(self.fd, C_S)  # C-s C-s
        self.wait(lambda: self.t("show", "-gqv", "@saved"), "1")
        os.write(self.fd, C_S); time.sleep(0.15)
        os.write(self.fd, b"\x1b[A"); time.sleep(0.15); os.write(self.fd, b"\x1b[A")   # repeat
        self.wait(lambda: self.t("show", "-gqv", "@ups"), "xx")
        self.assertEqual(self.table(), "prefix")
        time.sleep(0.7)                                                  # past repeat-time
        self.wait(self.table, "root")
        self.t("copy-mode", "-t", "zed:")                                # from copy mode too
        self.wait(lambda: self.on("#{pane_in_mode}"), "1")
        os.write(self.fd, C_S)
        self.wait(self.table, "prefix")
        os.write(self.fd, b"1")
        self.wait(lambda: self.on("#{session_name}"), "main")

    # -- status ------------------------------------------------------------
    def status(self):
        """The one status line as this client expands it: (raw, text)."""
        raw = self.on("#{T:status-format[0]}")
        return raw, re.sub(r"#\[[^\]]*\]", "", raw)

    def here(self):
        """The raw segment drawing this space's tab (up to the first alert)."""
        raw, _ = self.status()
        return raw[raw.index("#[range=session|"):].split("#[norange default]")[0]

    def alerts(self):
        """[(state colour, N)] of the other-space alert chips, left to right."""
        return [(dot, n) for _, n, dot in self.alert_chips()]

    def alert_chips(self):
        """[(space colour, N, state colour)]: ⌃N in its space's colour, ● in the state's."""
        return re.findall(r"#\[fg=(#\w+)\]⌃(\d+)#\[fg=(#\w+)\]●", self.status()[0])

    def sid(self, name):
        return self.t("display", "-t", name, "-p", "#{session_id}")

    def test_one_line_this_space_alerts_shells_status_right(self):
        raw, text = self.status()
        self.assertEqual(re.findall(CHIP_RE, text), [("1", "main")])            # this space only
        self.assertIn(f"range=session|{self.sid('main')}]", raw)
        for other in ("zed", "agents", "scratch", "stash"):                     # no tab, no alert
            self.assertNotIn(other, text)
        self.assertNotIn(f"range=session|{self.sid('zed')}]", raw)
        self.assertNotIn(f"range=session|{self.sid('agents')}]", raw)
        self.assertIn("⌃2●", text)                                              # proj: done
        self.assertNotIn("proj", text)
        self.assertEqual([int(i) for i in re.findall(r"█(\d+)█", text)], [1, 2])   # shells: windows 1 and 3
        self.assertIn("█1█ 󰋜 ", text)                                              # home marker
        self.assertNotIn("█2█ 󰋜", text)
        self.assertIn("range=window|3", raw)
        self.assertIn("[+]", text)
        self.assertIn("range=user|space-new", raw)
        self.assertTrue(text.rstrip().endswith("RIGHT"))
        self.assertIn("align=right", raw)
        self.assertLess(text.index("⌃1"), text.index("⌃2●"))
        self.assertLess(text.index("⌃2●"), text.index("█1█ 󰋜"))

    def state(self):
        """The raw tail after this space's chip: its rollup ● and glyph."""
        raw, _ = self.status()
        return raw[raw.index("#[range=session|"):].split("#[norange default]")[1].split("#[range=")[0]

    def test_this_space_chip_is_its_colour_and_the_rollup_follows_it(self):
        # A solid chip in the space's colour, bold crust text, a powerline edge.
        self.assertIn("#[fg=#b4befe,bg=default]█#[fg=#11111b,bg=#b4befe,bold]⌃1 main#[fg=#b4befe,bg=default,nobold]",
                      self.here())                                # main: lavender
        self.assertIn("#[fg=#f9e2af]●", self.state())             # needs-input beats running
        self.t("set", "-w", "-t", self.M4, "@agent_state", "idle")
        self.assertNotIn("●", self.state())                       # quiet: no dot
        self.t("switch-client", "-c", self.client, "-t", "proj")
        self.wait(lambda: "⌃2 proj" in self.status()[1], True)
        self.assertIn("bg=#cba6f7,bold]⌃2 proj", self.here())     # proj: the first palette colour
        self.assertIn("#[fg=#a6e3a1]●", self.state())             # done
        self.t("set", "-w", "-t", self.P1, "@agent_state", "failed")
        self.assertIn("#[fg=#f38ba8]●", self.state())             # failed beats done
        self.t("set", "-w", "-t", self.P1, "@agent_workflow", "1")
        self.assertIn("󰒓", self.state())
        # Set off from the shells: edge, state, a two-cell gap.
        self.assertTrue(self.status()[1].startswith("█⌃2 proj ● 󰒓   [+]"), self.status()[1])   # P1 is an agent now

    def test_prefix_turns_the_chip_red(self):
        self.t("set", "-w", "-t", self.M4, "@agent_state", "idle")
        self.key("C-b")
        self.wait(lambda: "#[fg=#f38ba8,bg=default]█#[fg=#11111b,bg=#f38ba8,bold]⌃1 main" in self.here(), True)
        self.key("Escape")
        self.wait(lambda: "#[fg=#b4befe,bg=default]█#[fg=#11111b,bg=#b4befe,bold]⌃1 main" in self.here(), True)

    def test_alert_number_in_its_space_colour_dot_in_the_state_colour(self):
        Z = self.new_window("zed", "needs-input")
        self.assertEqual(self.alert_chips(), [("#cba6f7", "2", "#a6e3a1"), ("#94e2d5", "3", "#f9e2af")])
        self.t("set", "-w", "-t", Z, "@agent_state", "failed")
        self.assertEqual(self.alert_chips()[1], ("#94e2d5", "3", "#f38ba8"))

    def test_hidden_session_name_turns_red_on_prefix(self):
        self.t("switch-client", "-c", self.client, "-t", "agents")
        self.wait(lambda: self.status()[1].startswith("[agents]"), True)
        self.assertIn("#[fg=#6c7086][agents]", self.status()[0])
        self.key("C-b")
        self.wait(lambda: "#[fg=#11111b,bg=#f38ba8][agents]" in self.status()[0], True)
        self.key("Escape")

    def test_spaces_past_nine_have_no_key(self):
        for i in range(4, 11):
            self.new_session(f"s{i}", "/usr")
        self.wait(lambda: self.t("show", "-t", "s10", "-qv", "@space_index"), "10")
        self.wait(lambda: self.t("show", "-t", "s9", "-qv", "@space_key"), "⌃9")
        self.assertEqual(self.t("show", "-t", "s10", "-qv", "@space_key"), "")
        self.new_window("s10", "needs-input")
        self.wait(lambda: "s10●" in self.status()[1], True)              # alert: the name, no key
        self.t("switch-client", "-c", self.client, "-t", "s10")
        self.wait(lambda: self.status()[1].startswith("█s10\ue0b0"), True)   # the name alone, no ⌃10

    def test_alerts_only_for_other_spaces_that_need_you(self):
        self.assertEqual(self.alerts(), [("#a6e3a1", "2")])     # proj done; main (here) never
        Z = self.new_window("zed", "running")
        self.assertEqual(self.alerts(), [("#a6e3a1", "2")])     # in flight: no alert
        self.t("set", "-w", "-t", Z, "@agent_state", "idle")
        self.assertEqual(self.alerts(), [("#a6e3a1", "2")])     # idle: none
        self.t("set", "-w", "-t", Z, "@agent_state", "needs-input")
        self.assertEqual(self.alerts(), [("#a6e3a1", "2"), ("#f9e2af", "3")])
        self.t("set", "-w", "-t", Z, "@agent_state", "failed")
        self.assertEqual(self.alerts(), [("#a6e3a1", "2"), ("#f38ba8", "3")])
        self.t("set", "-w", "-t", self.P2, "@agent_workflow", "1")   # done + workflow: not done
        self.assertEqual(self.alerts(), [("#f38ba8", "3")])
        for w in self.t("list-windows", "-t", "agents", "-F", "#{window_id}").split():
            self.t("set", "-w", "-t", w, "@agent_state", "needs-input")   # hidden: never
        self.assertEqual(self.alerts(), [("#f38ba8", "3")])
        self.t("switch-client", "-c", self.client, "-t", "zed")
        self.wait(self.alerts, [("#f9e2af", "1")])               # main needs-input, now elsewhere

    def test_hidden_session_shows_its_name_and_alerts_but_no_shells(self):
        self.t("switch-client", "-c", self.client, "-t", "agents")
        self.wait(lambda: self.status()[1].startswith("[agents]"), True)
        raw, text = self.status()
        self.assertNotIn("█", text)   # nothing looped in a hidden session
        self.assertEqual(self.alerts(), [("#f9e2af", "1"), ("#a6e3a1", "2")])

    def test_rendered_on_a_real_client(self):
        """The line as drawn: a viewer server runs a nested client in a pane."""
        view = "spaces-view-" + uuid.uuid4().hex[:12]
        run = lambda *a: subprocess.run([TMUX, "-L", view, "-f", "/dev/null", *a], env=self.env,
                                        capture_output=True, text=True, timeout=10).stdout
        try:
            run("new-session", "-d", "-x", str(COLS), "-y", "10",
                f"env -u TMUX TERM=xterm-256color {TMUX} -L {self.sock} attach -t main")
            sp = run("display", "-p", "#{socket_path}").strip()
            deadline, lines = time.time() + 5, []
            while time.time() < deadline:
                lines = run("capture-pane", "-p").rstrip("\n").split("\n")
                if "[+]" in lines[-1]:
                    break
                time.sleep(0.1)
            self.assertRegex(lines[-1], r"^█⌃1 main\ue0b0 ●  ⌃2● █1█ 󰋜 ~██2█ ~█ \[\+\] +RIGHT$")
            self.assertNotIn("[+]", lines[-2])   # one status line
        finally:
            run("kill-server")
            if "sp" in locals() and sp:
                Path(sp).unlink(missing_ok=True)

    # -- keys ----------------------------------------------------------------
    def test_prefix_digit_goes_to_space_n(self):
        self.key("C-b", "2"); self.wait(lambda: self.on("#{session_name}"), "proj")
        self.key("C-b", "3"); self.wait(lambda: self.on("#{session_name}"), "zed")
        self.key("C-b", "1"); self.wait(lambda: self.on("#{session_name}"), "main")
        self.key("C-b", "7"); time.sleep(0.2)
        self.assertEqual(self.on("#{session_name}"), "main")

    def test_prefix_meta_digit_goes_to_agent_n_of_this_space(self):
        self.key("C-b", "M-2"); self.wait(self.on, f"main:{self.M4}")
        self.key("C-b", "M-1"); self.wait(self.on, f"main:{self.M2}")
        self.key("C-b", "M-3"); self.wait(self.on, f"main:{self.M5}")
        self.key("C-b", "M-4"); time.sleep(0.3)   # only 3 agents: stays put
        self.assertEqual(self.on(), f"main:{self.M5}")

    def test_meta_digit_goes_to_shell_n_of_this_space(self):
        self.t("select-window", "-t", self.M4)
        self.key("M-2"); self.wait(self.on, f"main:{self.M3}")
        self.key("M-1"); self.wait(self.on, f"main:{self.M1}")   # home is shell 1
        self.key("M-3"); time.sleep(0.3)   # only 2 shells: stays put
        self.assertEqual(self.on(), f"main:{self.M1}")

    def chord(self, row, n):
        """A WezTerm digit chord, written into the client's terminal."""
        os.write(self.fd, CHORD[(row, n)].encode())

    def test_wezterm_chords_are_exact_keys(self):
        self.t("select-window", "-t", self.M4)
        self.chord("ALT", 2); self.wait(self.on, f"main:{self.M3}")     # OPT+2: shell 2
        self.chord("CMD", 1); self.wait(self.on, f"main:{self.M2}")     # CMD+1: agent 1, never a shell
        self.chord("CMD", 3); self.wait(self.on, f"main:{self.M5}")
        self.chord("ALT", 1); self.wait(self.on, f"main:{self.M1}")     # home
        self.chord("CTRL", 2); self.wait(lambda: self.on("#{session_name}"), "proj")
        self.chord("CTRL", 1); self.wait(lambda: self.on("#{session_name}"), "main")
        self.chord("CTRL", 7); time.sleep(0.3)                          # no space 7: stays
        self.assertEqual(self.on("#{session_name}"), "main")
        self.t("copy-mode", "-t", self.M1)                              # also from copy mode
        self.wait(lambda: self.on("#{pane_in_mode}"), "1")
        self.chord("CTRL", 3); self.wait(lambda: self.on("#{session_name}"), "zed")

    def test_next_prev_are_kind_aware(self):
        self.key("M-l"); self.wait(self.on, f"main:{self.M3}")   # from the home shell: next shell
        self.key("M-l"); self.wait(self.on, f"main:{self.M1}")   # wraps, agents skipped
        self.key("M-h"); self.wait(self.on, f"main:{self.M3}")
        self.t("select-window", "-t", self.M2)
        self.key("M-l"); self.wait(self.on, f"main:{self.M4}")   # from an agent: next agent
        self.key("M-l"); self.wait(self.on, f"main:{self.M5}")
        self.key("M-l"); self.wait(self.on, f"main:{self.M2}")   # wraps, shells skipped
        self.key("M-h"); self.wait(self.on, f"main:{self.M5}")

    def test_toggle_agent_and_last_shell(self):
        self.t("select-window", "-t", self.M3)
        self.t("select-window", "-t", self.M4)
        self.key("M-`"); self.wait(self.on, f"main:{self.M3}")   # last shell, not shell 1
        self.key("M-`"); self.wait(self.on, f"main:{self.M4}")   # back to last agent
        self.t("kill-window", "-t", self.M3)
        self.t("select-window", "-t", self.M4)
        self.key("M-`"); self.wait(self.on, f"main:{self.M1}")   # remembered shell gone: home

    def test_toggle_from_an_agent_lands_on_home_when_it_is_the_only_shell(self):
        self.t("switch-client", "-c", self.client, "-t", "proj")
        self.t("select-window", "-t", self.P2)
        self.wait(lambda: self.on("#{window_id}"), self.P2)
        self.key("M-`")
        self.wait(lambda: self.on("#{window_id}"), self.P1)
        self.assertEqual(self.t("list-windows", "-t", "proj", "-F", "#{?#{@agent_state},a,s}").split(), ["s", "a"])

    def test_move_tab_to_another_space(self):
        self.t("select-window", "-t", self.M3)
        self.wait(lambda: self.on("#{window_id}"), self.M3)
        where = lambda: self.t("display", "-t", self.M3, "-p", "#{session_name}")
        for _ in range(5):
            self.key("M-m")
            time.sleep(0.4)
            self.key("3")                                  # zed's number in the menu
            time.sleep(0.4)
            if where() == "zed":
                break
        self.wait(where, "zed")
        self.assertEqual(self.t("display", "-t", "zed:{end}", "-p", "#{window_id}"), self.M3)  # at the end
        self.assertEqual(self.on("#{session_name}"), "main")                                 # you stay here
        # Home never moves; the script refuses it too.
        sid = self.t("display", "-t", "zed", "-p", "#{session_id}")
        self.assertNotEqual(self.script("move-to", self.M1, sid, self.client).returncode, 0)
        self.assertEqual(self.homes("main"), [self.M1])

    def test_new_window_keys_use_the_space_folder_in_project_spaces(self):
        self.t("switch-client", "-c", self.client, "-t", "proj")
        self.t("send-keys", "-t", self.P1, f"cd {self.home_dir}", "Enter")
        self.wait(lambda: self.on("#{pane_current_path}"), self.home_dir)
        self.key("C-b", "c")
        self.wait(lambda: self.on("#{pane_current_path}"), self.proj)
        self.key("C-b", "C-t")
        self.wait(lambda: self.on("#{window_index}"), self.t("display", "-t", "proj:{end}", "-p", "#{window_index}"))
        self.wait(lambda: self.on("#{pane_current_path}"), self.proj)
        # main is not a project space: today's cwd rule.
        self.t("switch-client", "-c", self.client, "-t", "main")
        self.t("send-keys", "-t", self.M1, f"cd {self.home_dir}", "Enter")
        self.wait(lambda: self.t("display", "-t", self.M1, "-p", "#{pane_current_path}"), self.home_dir)
        self.key("C-b", "c")
        self.wait(lambda: self.on("#{pane_current_path}"), self.home_dir)

    def test_open_dir_creates_reuses_and_switches(self):
        newdir = os.path.join(self.tmp, "a b.c")
        os.mkdir(newdir)
        self.assertEqual(self.script("open-dir", newdir, self.client).returncode, 0)
        self.wait(lambda: self.on("#{session_name}|#{session_path}"), f"a-b-c|{newdir}")
        self.wait(lambda: self.t("show", "-t", "a-b-c", "-qv", "@space_index"), "4")
        self.wait(lambda: self.t("list-windows", "-t", "a-b-c", "-F", "#{@space_home}"), "1")  # one window: home
        self.script("open-dir", self.proj, self.client)
        self.wait(lambda: self.on("#{session_name}"), "proj")     # same folder: reuse
        other = os.path.join(self.tmp, "x", "proj")
        os.makedirs(other)
        self.script("open-dir", other, self.client)
        self.wait(lambda: self.on("#{session_name}"), "proj-2")   # name taken: suffix

    def test_open_dir_makes_relative_paths_absolute(self):
        os.mkdir(os.path.join(self.tmp, "rel"))
        self.script("open-dir", "rel", self.client, cwd=self.tmp)
        self.wait(lambda: self.on("#{session_name}|#{session_path}"),
                  f"rel|{os.path.realpath(os.path.join(self.tmp, 'rel'))}")
        # "." from inside a space's folder reuses that space, not a new "-" one.
        self.script("open-dir", ".", self.client, cwd=self.proj)
        self.wait(lambda: self.on("#{session_name}"), "proj")

    def test_open_dir_never_takes_a_hidden_name(self):
        for name in ("stash", "tasks"):
            d = os.path.join(self.tmp, name)
            os.mkdir(d)
            self.script("open-dir", d, self.client)
            self.wait(lambda: self.on("#{session_name}|#{session_path}"), f"{name}-2|{d}")
            self.wait(lambda: self.on("#{@space_hidden}|#{?#{@space_index},n,}"), "|n")
        names = self.t("list-sessions", "-F", "#{session_name}").split()
        self.assertNotIn("stash", names)
        self.assertNotIn("tasks", names)

    def test_agent_keys_do_nothing_in_hidden_sessions(self):
        self.t("switch-client", "-c", self.client, "-t", "agents")
        self.wait(lambda: self.on("#{session_name}"), "agents")
        here = self.on()
        windows = self.t("list-windows", "-t", "agents", "-F", "#{window_id}")
        for k in (("M-1",), ("C-b", "M-1"), ("M-l",), ("M-h",), ("M-`",), ("M-m",)):
            self.key(*k)
        time.sleep(0.5)
        self.assertEqual(self.on(), here)
        self.assertEqual(self.t("list-windows", "-t", "agents", "-F", "#{window_id}"), windows)
        # The script refuses too (any other caller), and creates no shell in
        # a hidden session that has none.
        self.new_session("stash", self.proj)
        sid = self.t("display", "-t", "stash", "-p", "#{session_id}")
        self.wait(lambda: self.t("show", "-t", "stash", "-qv", "@space_hidden"), "1")
        self.t("set", "-w", "-t", "stash:", "@agent_state", "idle")   # agent only: toggle would add a shell
        before = self.t("list-windows", "-t", "stash", "-F", "#{window_id}")
        for args in (["toggle", sid], ["agent", "1", sid], ["shell", "1", sid], ["next", sid]):
            r = self.script(*args, self.client)
            self.assertNotEqual(r.returncode, 0, args)
        self.assertEqual(self.t("list-windows", "-t", "stash", "-F", "#{window_id}"), before)
        self.assertEqual(self.homes("stash"), [])
        self.assertEqual(self.on(), here)

    def test_stash_selection_shows_every_window_with_the_tint(self):
        # Stand-ins for catppuccin's tab formats, with the same @stash_sel test.
        self.t("set", "-g", "window-status-format", "#[bg=#{?#{@stash_sel},#cba6f7,#313244}]W#I")
        self.t("set", "-g", "window-status-current-format", "#[bg=#{?#{@stash_sel},#cba6f7,#1e1e2e}]C#I")
        stash = HERE / "scripts" / "executable_stash.sh"
        run = lambda *a: subprocess.run(["bash", str(stash), *a], env=self.script_env(),
                                        capture_output=True, timeout=20)
        self.t("select-window", "-t", self.M2)                     # an agent: no shell tab
        self.assertNotIn("C2", self.status()[1])
        run("sel-start", "here", self.M2)                          # = prefix v
        self.wait(lambda: " W1C2W3W4W5RIGHT" in self.status()[1], True)   # every window, agents too
        self.assertNotIn("[+]", self.status()[1])
        self.assertTrue(self.status()[1].startswith("█⌃1 main\ue0b0"))   # this space's tab stays
        self.assertEqual(self.status()[0].count("bg=#cba6f7"), 1)   # (fg=: proj's colour)
        self.assertIn("range=window|2", self.status()[0])
        run("sel-move", "right", self.M2)                          # = l
        self.wait(lambda: self.status()[0].count("bg=#cba6f7"), 2)
        run("sel-cancel")
        self.wait(lambda: "[+]" in self.status()[1], True)         # back to shells only
        self.assertEqual([int(i) for i in re.findall(r"█(\d+)█", self.status()[1])], [1, 2])

    # -- mouse ---------------------------------------------------------------
    def click(self, x, y):
        """Left click at 0-based cell (x, y) of the client, as an SGR report."""
        os.write(self.fd, f"\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m".encode())

    def test_click_plus_opens_shell_and_an_alert_switches(self):
        self.t("switch-client", "-c", self.client, "-t", "proj")
        self.wait(lambda: self.on("#{session_name}"), "proj")
        _, text = self.status()
        plus = text.index("[+]")
        before = len(self.t("list-windows", "-t", "proj").splitlines())
        self.click(plus + 1, ROWS - 1)
        self.wait(lambda: len(self.t("list-windows", "-t", "proj").splitlines()), before + 1)
        self.wait(lambda: self.on("#{pane_current_path}"), self.proj)
        self.new_window("zed", "needs-input")                     # zed now needs you
        self.wait(lambda: "⌃3●" in self.status()[1], True)
        _, text = self.status()
        time.sleep(0.6)   # past double-click-time, so this is a fresh single click
        self.click(text.index("⌃3●"), ROWS - 1)
        self.wait(lambda: self.on("#{session_name}"), "zed")


if __name__ == "__main__":
    unittest.main()
