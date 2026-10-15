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


# WezTerm's CMD+1-9 send prefix M-1..9 in both modes; off, they select window
# N. These are the one place off differs from a pre-spaces server.
OFF_META = [l for l in else_branch().splitlines() if re.match(r'bind -N "Select window \d" M-\d ', l)]


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
        on = set(re.findall(r"^bind -n (\S+)", on_branch(), re.M)) - {"MouseDown1Status"}
        off = set(re.findall(r"^unbind -q -n (\S+)", else_branch(), re.M))
        self.assertEqual(on, off)

    def test_no_meta_j_k(self):
        # Claude Code binds meta+j (app:toggleTerminal); next/prev use M-l/M-h.
        self.assertNotRegex(on_branch(), r"(?m)^bind -n M-[jk] ")

    def test_off_binds_prefix_meta_digits_to_windows(self):
        self.assertEqual([l.split()[5] for l in OFF_META], [f"M-{n}" for n in range(1, 10)])

    def test_wezterm_digit_rows(self):
        # CTRL+N → prefix N (space), CMD+N → prefix + ESC N (agent).
        text = WEZTERM.read_text()
        loop = text[text.rindex("for i = 1, 9 do"):]
        self.assertIn('mods = "CTRL"', loop)
        self.assertIn('mods = "CMD"', loop)
        self.assertIn('SendString("\\x1b" .. tostring(i))', loop)

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
        self.assertEqual(self.t("show", "-gv", "status"), "2")
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

    def test_hidden_marker_is_set_even_when_off(self):
        self.new_session("agents", "/")
        self.wait(lambda: self.t("show", "-t", "agents", "-v", "@space_hidden"), "1")

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
            # remain-on-exit is the home PANE's, not the window's.
            self.assertEqual(self.t("list-panes", "-t", f"{s}:^", "-F", "#{@space_home_pane}|#{remain-on-exit}"), "1|on")
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
        self.wait(lambda: self.t("list-panes", "-t", self.M1, "-F", "#{pane_id}"), home_pane)
        time.sleep(0.5)
        self.assertEqual(self.t("list-panes", "-t", self.M1, "-F", "#{pane_id}|#{pane_dead}"), f"{home_pane}|0")

    def test_x_and_cmd_shift_w_refuse_only_on_the_home_pane(self):
        keys = self.t("list-keys", "-T", "prefix").splitlines()
        for k in ("x", "C-l"):
            line = next(l for l in keys if re.match(rf"bind-key +-T prefix +{k} ", l))
            self.assertTrue(line.split(None, 4)[4].startswith('if-shell -F "#{@space_home_pane}"'), line)

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
        panes = sorted(self.t("list-panes", "-s", "-t", "proj", "-F", "#{pane_id}").split())
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
        raw, text = self.status(0)
        self.assertEqual(re.findall(r"█(\d)█ (?:[^\s█]+ )?(\w+)█", text),
                         [("1", "main"), ("2", "gone"), ("3", "zed"), ("4", "proj")])

    def test_renamed_space_keeps_its_place_in_the_registry(self):
        self.wait(lambda: [r[0] for r in self.registered()], ["main", "proj", "zed"])
        self.t("rename-session", "-t", "proj", "pro2")
        self.wait(lambda: [r[0] for r in self.registered()], ["main", "pro2", "zed"])

    # -- status ------------------------------------------------------------
    def status(self, n):
        raw = self.on(f"#{{T:status-format[{n}]}}")
        return raw, re.sub(r"#\[[^\]]*\]", "", raw)

    def test_line0_spaces_in_order_hidden_absent_status_right_kept(self):
        raw, text = self.status(0)
        self.assertEqual(re.findall(r"█(\d)█ (?:[^\s█]+ )?(\w+)█", text), [("1", "main"), ("2", "proj"), ("3", "zed")])
        for h in ("agents", "scratch", "stash"):
            self.assertNotIn(h, text)
        self.assertIn("RIGHT", text)
        self.assertIn("align=right", raw)
        for sid in self.t("list-sessions", "-F", "#{session_id}").split():
            name = self.t("display", "-t", sid, "-p", "#{session_name}")
            self.assertEqual(f"range=session|{sid}" in raw, name in ("main", "proj", "zed"), name)

    def tab(self, raw, name):
        """The raw segment of line 0 drawing space `name`."""
        segs = raw.split("#[range=session|")
        return next(s for s in segs if re.search(rf"\]{re.escape(name)}#\[", s))

    def test_rollup_tints(self):
        raw, _ = self.status(0)
        self.assertIn("bg=#f9e2af", self.tab(raw, "main"))   # needs-input beats running
        self.assertIn("bg=#a6e3a1", self.tab(raw, "proj"))   # done
        self.assertIn("bg=#313244", self.tab(raw, "zed"))    # nothing
        self.t("set", "-w", "-t", self.P1, "@agent_state", "failed")
        raw, _ = self.status(0)
        self.assertIn("bg=#f38ba8", self.tab(raw, "proj"))   # failed beats done
        self.t("set", "-w", "-t", self.P1, "@agent_workflow", "1")
        raw, text = self.status(0)
        self.assertIn("󰒓", self.tab(raw, "proj"))

    def test_current_space_chip_is_peach(self):
        self.t("set", "-w", "-t", self.M4, "@agent_state", "idle")
        raw, _ = self.status(0)
        self.assertTrue(self.tab(raw, "main").startswith(self.t("display", "-t", "main", "-p", "#{session_id}") + "]#[fg=#fab387"))

    def test_line1_lists_this_spaces_shells_by_ordinal(self):
        raw, text = self.status(1)
        self.assertEqual([int(i) for i in re.findall(r"█(\d+)█", text)], [1, 2])   # windows 1 and 3
        self.assertIn("█1█ 󰋜 ", text)                                              # home marker
        self.assertNotIn("█2█ 󰋜", text)
        self.assertIn("range=window|3", raw)
        self.assertIn("[+]", text)
        self.assertIn("range=user|space-new", raw)
        self.t("switch-client", "-c", self.client, "-t", "agents")
        raw, text = self.status(1)
        self.assertNotIn("█", text)   # nothing looped in a hidden session
        _, line0 = self.status(0)
        self.assertIn("[agents]", line0)

    def test_rendered_on_a_real_client(self):
        """Both lines as drawn: a viewer server runs a nested client in a pane."""
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
                if len(lines) >= 2 and "[+]" in lines[-1]:
                    break
                time.sleep(0.1)
            self.assertRegex(lines[-2], r"^█1█ main██2█ proj██3█ zed█ +RIGHT$")
            self.assertEqual(lines[-1].rstrip(), "█1█ 󰋜 ~██2█ ~█ [+]")
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
        self.t("select-window", "-t", self.M2)                     # an agent: no tab on line 1
        self.assertNotIn("C2", self.status(1)[1])
        run("sel-start", "here", self.M2)                          # = prefix v
        self.wait(lambda: self.status(1)[1], "W1C2W3W4W5")         # every window, agents too
        self.assertEqual(self.status(1)[0].count("#cba6f7"), 1)
        self.assertIn("range=window|2", self.status(1)[0])
        run("sel-move", "right", self.M2)                          # = l
        self.wait(lambda: self.status(1)[0].count("#cba6f7"), 2)
        run("sel-cancel")
        self.wait(lambda: "[+]" in self.status(1)[1], True)        # back to shells only
        self.assertEqual([int(i) for i in re.findall(r"█(\d+)█", self.status(1)[1])], [1, 2])

    # -- mouse ---------------------------------------------------------------
    def click(self, x, y):
        """Left click at 0-based cell (x, y) of the client, as an SGR report."""
        os.write(self.fd, f"\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m".encode())

    def test_click_plus_opens_shell_at_space_folder_and_tabs_still_switch(self):
        self.t("switch-client", "-c", self.client, "-t", "proj")
        time.sleep(0.3)
        _, text = self.status(1)
        plus = text.index("[+]")
        before = len(self.t("list-windows", "-t", "proj").splitlines())
        self.click(plus + 1, ROWS - 1)
        self.wait(lambda: len(self.t("list-windows", "-t", "proj").splitlines()), before + 1)
        self.wait(lambda: self.on("#{pane_current_path}"), self.proj)
        _, line0 = self.status(0)
        time.sleep(0.6)   # past double-click-time, so this is a fresh single click
        self.click(line0.index("zed"), ROWS - 2)
        self.wait(lambda: self.on("#{session_name}"), "zed")


if __name__ == "__main__":
    unittest.main()
