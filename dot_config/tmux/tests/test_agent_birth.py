"""AGENT BIRTH (agent-birth.conf, docs/agent-tab-indicator.md "Born an agent").

A new claude tab must be an agent from the instant it exists, not a shell for
the seconds before claude's first hook. Two halves:

  * Every creation path, against a real tmux: each test gets its own `-L`
    socket started with `-f /dev/null`, sources agent-birth.conf and the real
    binding line (tmux.conf.tmpl / spaces.conf, extracted the way
    test_spaces.py does), attaches one client from a pseudo-terminal this
    process opens, and presses the key with `send-keys -K`. Scripts run their
    own code: claude-dir-picker.sh's new-window statement and closed-tabs.sh's
    reopen_one function, cut out of the files (as test_codex_launchers.py
    does) and run with TMUX pointing at the test server. No real zsh or
    claude: the server's default-command is `sleep`, and its HOME is a temp
    dir. The new window must carry @agent_state (+ companions) the FIRST time
    it is seen, and the shell-vs-agent formats (spaces.conf's counts,
    space.sh's a/s) must call it an agent.
  * The watcher's BIRTH GRACE, on test_agent_jump_watcher's fake-tmux harness
    (isolated HOME/TMPDIR, fake tmux/pgrep/ps; no real server or watcher).

Teardown kill-servers each socket via -L and removes its file. Run with
unittest or pytest.
"""
import fcntl
import json
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

import test_agent_jump_watcher as jw   # module only: the watcher fakes

HERE = Path(__file__).resolve().parents[1]
CONF = HERE / "tmux.conf.tmpl"
SPACES = HERE / "spaces.conf"
BIRTH = HERE / "agent-birth.conf"
PICKER = HERE / "scripts" / "executable_claude-dir-picker.sh"
CLOSED = HERE / "scripts" / "executable_closed-tabs.sh"
SPACE_SH = HERE / "scripts" / "executable_space.sh"
TMUX = shutil.which("tmux")
COLS, ROWS = 120, 20


def lines_starting(path, prefix):
    return [l for l in path.read_text().splitlines() if l.startswith(prefix)]


def on_branch():
    text = SPACES.read_text()
    return text[text.index('\n%if "#{==:#{@spaces},on}"\n'):text.index("\n%else\n")]


def on_line(prefix):
    hits = [l for l in on_branch().splitlines() if l.startswith(prefix)]
    assert len(hits) == 1, (prefix, hits)
    return hits[0]


def conf_line(prefix):
    hits = lines_starting(CONF, prefix)
    assert len(hits) == 1, (prefix, hits)
    return hits[0]


def spaces_set(name):
    """The format spaces.conf stores in a global option (`set -g NAME '...'`)."""
    m = re.search(r"^set -g %s '(.*)'$" % re.escape(name), SPACES.read_text(), re.M)
    assert m, name
    return m.group(1)


class Shape(unittest.TestCase):
    def test_every_claude_tab_site_is_born_an_agent(self):
        sites = [l for p in (CONF, SPACES) for l in p.read_text().splitlines()
                 if "ZSH_AUTOSTART=claude" in l and not l.lstrip().startswith("#")]
        self.assertEqual(len(sites), 6, sites)   # k + C-k in tmux.conf, spaces on, spaces off
        for l in sites:
            # One birth per new-window, in the same command list.
            self.assertEqual(l.count('run-shell -C "#{?@agent_birth,#{@agent_birth} claude,}"'), l.count("new-window"), l)
        picker = PICKER.read_text()
        self.assertIn("run-shell -C '#{?@agent_birth,#{@agent_birth} claude,}'", picker)
        self.assertIn('run-shell -C "#{?@agent_birth,#{@agent_birth} $tool,}"', CLOSED.read_text())

    def test_birth_conf_is_sourced_by_both_files(self):
        src = 'source-file -F "#{d:current_file}/agent-birth.conf"'
        self.assertIn(src, CONF.read_text().splitlines())
        self.assertIn(src, SPACES.read_text().splitlines())
        # An option, never a command-alias: an alias is expanded at PARSE time,
        # and tmux parses a whole config file before running any of it.
        self.assertNotRegex(BIRTH.read_text(), r"(?m)^set.*command-alias")

    def test_space_sh_classifies_by_agent_state(self):
        # The a/s split the server tests below evaluate is space.sh's own.
        self.assertIn("#{?#{@agent_state},a,s}", SPACE_SH.read_text())


class Server(unittest.TestCase):
    """A -L test server with agent-birth.conf sourced and one attached client."""

    def setUp(self):
        self.sock = "birth-test-" + uuid.uuid4().hex[:12]
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="birth-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.proj = os.path.join(self.tmp, "work", "my-project")
        os.makedirs(self.home)
        os.makedirs(self.proj)
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env.update(TERM="xterm-256color", HOME=self.home)
        self.path = None
        self.child = None
        self.addCleanup(self.reap)
        self.t("new-session", "-d", "-s", "main", "-x", str(COLS), "-y", str(ROWS), "-c", self.proj, "sleep 600")
        self.path = self.t("display", "-p", "#{socket_path}")
        self.t("set", "-g", "exit-empty", "off")
        self.t("set", "-g", "default-command", "exec sleep 600")   # never a real zsh / claude
        self.t("source-file", str(BIRTH))
        self.first = self.t("display", "-p", "-t", "main:", "#{window_id}")

    def t(self, *args, check=True):
        r = subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", *args], env=self.env,
                           capture_output=True, text=True, timeout=10)
        if check and r.returncode:
            raise AssertionError(f"tmux {args}: {r.stderr}")
        return r.stdout.strip()

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

    def attach(self):
        pid, fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
                os.execvpe(TMUX, [TMUX, "-L", self.sock, "-f", "/dev/null", "attach", "-t", "main"], self.env)
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

    def bind(self, line):
        conf = os.path.join(self.tmp, "bind.conf")
        Path(conf).write_text(line + "\n")
        self.t("source-file", conf)

    FIELDS = ["window_id", "@agent_state", "@agent_kind", "@agent_born", "@agent_since", "@agent_summary"]

    def rows(self):
        # "|", not US: str.strip() eats a trailing \x1f, i.e. empty last fields.
        out = self.t("list-windows", "-t", "main", "-F", "|".join("#{%s}" % f for f in self.FIELDS))
        return {r.split("|")[0]: dict(zip(self.FIELDS, r.split("|"))) for r in out.splitlines()}

    def first_sighting(self, before, timeout=4.0):
        """The new window's fields the FIRST time any list shows it."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            new = {w: r for w, r in self.rows().items() if w not in before}
            if new:
                self.assertEqual(len(new), 1, new)
                return next(iter(new.values()))
            time.sleep(0.005)
        self.fail("no new window appeared")

    def assert_born(self, row, kind="claude", summary="my-project/New Session"):
        now = int(time.time())
        self.assertEqual(row["@agent_state"], "idle", row)
        self.assertEqual(row["@agent_kind"], kind, row)
        self.assertRegex(row["@agent_born"], r"^\d+$", row)
        self.assertLessEqual(abs(int(row["@agent_born"]) - now), 5, row)
        self.assertEqual(row["@agent_since"], row["@agent_born"] + " idle", row)
        if summary is not None:
            self.assertEqual(row["@agent_summary"], summary, row)

    def assert_counted_as_agent(self, wid):
        # spaces.conf's shell/agent counts and space.sh's a/s split, evaluated
        # against the session with the new window in it.
        n_sh = self.t("display", "-p", "-t", "main:", spaces_set("@space_n_shells"))
        n_ag = self.t("display", "-p", "-t", "main:", spaces_set("@space_n_agents"))
        kinds = dict(l.split() for l in self.t("list-windows", "-t", "main", "-F",
                                                "#{window_id} #{?#{@agent_state},a,s}").splitlines())
        self.assertEqual(kinds[wid], "a")
        self.assertEqual(int(n_ag), list(kinds.values()).count("a"))
        self.assertEqual(int(n_sh), list(kinds.values()).count("s"))

    def press(self, line, key):
        self.bind(line)
        self.attach()
        before = self.rows()
        self.t("send-keys", "-K", "-c", self.client, "C-b", key)
        row = self.first_sighting(before)
        return before, row

    # -- the key bindings ------------------------------------------------
    def test_tmux_conf_k(self):
        _, row = self.press(conf_line("bind k "), "k")
        self.assert_born(row)
        self.assert_counted_as_agent(row["window_id"])

    def test_tmux_conf_ctrl_k_replaces_the_tab(self):
        before, row = self.press(conf_line("bind C-k "), "C-k")
        self.assert_born(row)
        # The tab it replaced goes; the new one is the agent.
        self.wait_gone(self.first)
        self.assert_counted_as_agent(row["window_id"])

    def test_spaces_k(self):
        _, row = self.press(on_line("bind k "), "k")
        self.assert_born(row)
        self.assert_counted_as_agent(row["window_id"])

    def test_spaces_ctrl_k_both_branches(self):
        line = on_line("bind C-k ")
        # Not home: replaces the current tab.
        before, row = self.press(line, "C-k")
        self.assert_born(row)
        self.wait_gone(self.first)
        # Home (@space_home on the current window): only adds one.
        home = row["window_id"]
        self.t("set", "-w", "-t", home, "@space_home", "1")
        before = self.rows()
        self.t("send-keys", "-K", "-c", self.client, "C-b", "C-k")
        row2 = self.first_sighting(before)
        self.assert_born(row2)
        self.assertIn(home, self.rows())
        self.assert_counted_as_agent(row2["window_id"])

    def test_spaces_off_copies_match(self):
        # The %else copies are tmux.conf's lines verbatim (test_spaces checks
        # the round trip); so they are born too.
        text = SPACES.read_text()
        off = text[text.index("\n%else\n"):text.index("\n%endif")].splitlines()
        self.assertIn(conf_line("bind k "), off)
        self.assertIn(conf_line("bind C-k "), off)

    def wait_gone(self, wid, timeout=4.0):
        deadline = time.time() + timeout
        while wid in self.rows() and time.time() < deadline:
            time.sleep(0.02)
        self.assertNotIn(wid, self.rows())

    # -- the scripts -----------------------------------------------------
    def script_env(self):
        return dict(self.env, TMUX=f"{self.path},0,0")

    def test_claude_dir_picker(self):
        text = PICKER.read_text()
        m = re.search(r'^  tmux new-window -a -t "\$\{session\}:\{end\}".*?\|\| die [^\n]*$', text, re.M | re.S)
        self.assertTrue(m, "picker's new-window statement not found")
        prog = 'die() { echo "die: $*" >&2; exit 9; }\nsession=main\ntarget="$1"\n' + m.group(0) + "\n"
        before = self.rows()
        r = subprocess.run(["bash", "-c", prog, "picker", self.proj], env=self.script_env(),
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_born(self.first_sighting(before))

    def test_claude_dir_picker_without_the_option_still_opens(self):
        self.t("set", "-gu", "@agent_birth")
        text = PICKER.read_text()
        m = re.search(r'^  tmux new-window -a -t "\$\{session\}:\{end\}".*?\|\| die [^\n]*$', text, re.M | re.S)
        prog = 'die() { echo "die: $*" >&2; exit 9; }\nsession=main\ntarget="$1"\n' + m.group(0) + "\n"
        before = self.rows()
        r = subprocess.run(["bash", "-c", prog, "picker", self.proj], env=self.script_env(),
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.first_sighting(before)["@agent_state"], "")   # a plain tab, as before

    def reopen(self, rec):
        fn = 'eval "$(awk \'/^reopen_one\\(\\) \\{/{f=1} f{print} f&&/^\\}/{exit}\' "$1")"\n'
        prog = 'msg() { :; }\n' + fn + 'reopen_one "$2" main\n'
        before = self.rows()
        r = subprocess.run(["bash", "-c", prog, "reopen", str(CLOSED), json.dumps(rec)],
                           env=self.script_env(), capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout.strip(), r"^@\d+$")
        return self.first_sighting(before)

    def rec(self, **kw):
        rec = dict(session="main", index=99, cwd=self.proj, cmd="", tool="", label="zsh")
        rec.update(kw)
        return rec

    def test_closed_tabs_reopens_claude_as_an_agent(self):
        row = self.reopen(self.rec(tool="claude", cmd="command claude --resume 'abc'",
                                   label="my-project/Login bug;; "))
        # Born, wearing the name it closed with (trailing ';' trimmed: it
        # would end the command list).
        self.assert_born(row, summary="my-project/Login bug")
        self.assert_counted_as_agent(row["window_id"])

    def test_closed_tabs_reopens_codex_as_a_codex_agent(self):
        row = self.reopen(self.rec(tool="codex", cmd="command codex resume 'abc'", label="-dash first"))
        # A name that would read as a flag is skipped: the placeholder stays.
        self.assert_born(row, kind="codex")

    def test_closed_tabs_reopens_opencode_as_an_opencode_agent(self):
        row = self.reopen(self.rec(tool="opencode", cmd="command opencode -s 'ses_x'", label="proj/Fix it"))
        self.assert_born(row, kind="opencode", summary="proj/Fix it")
        self.assert_counted_as_agent(row["window_id"])

    def test_closed_tabs_shell_stays_a_shell(self):
        row = self.reopen(self.rec())
        self.assertEqual(row["@agent_state"], "")
        self.assertEqual(row["@agent_born"], "")

    # -- the format itself -----------------------------------------------
    def test_home_project_is_tilde(self):
        before = self.rows()
        self.t("new-window", "-a", "-t", "main:", "-c", self.home, ";", "run-shell", "-C", "#{@agent_birth} claude")
        self.assert_born(self.first_sighting(before), summary="~/New Session")

    def test_start_path_is_tidied(self):
        # -c is taken as typed (made absolute, never normalized): the picker
        # hands over "$HOME/" for a "~/" query, "." stays ".../.".
        os.makedirs(os.path.join(self.proj, "sub"))
        cases = [(self.home + "/", "~"), (self.home + "//.", "~"), (self.proj + "/", "my-project"),
                 (self.proj + "/./", "my-project"), (self.proj + "/sub/..", "my-project")]
        for c, want in cases:
            before = self.rows()
            self.t("new-window", "-a", "-t", "main:", "-c", c, ";", "run-shell", "-C", "#{@agent_birth} claude")
            self.assert_born(self.first_sighting(before), summary=want + "/New Session")
        # A relative -c, resolved against the client's cwd.
        for c in (".", "./"):
            before = self.rows()
            subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", "new-window", "-a", "-t", "main:", "-c", c,
                            ";", "run-shell", "-C", "#{@agent_birth} claude"],
                           env=self.env, cwd=self.proj, check=True, capture_output=True, timeout=10)
            self.assert_born(self.first_sighting(before))

    def test_long_project_cut_like_project_name(self):
        d = os.path.join(self.tmp, "an-extremely-long-project-directory-name")
        os.makedirs(d)
        before = self.rows()
        self.t("new-window", "-a", "-t", "main:", "-c", d, ";", "run-shell", "-C", "#{@agent_birth} claude")
        # project_name(): basename | cut -c1-20
        self.assert_born(self.first_sighting(before), summary="an-extremely-long-pr/New Session")

    def test_project_is_the_start_path_not_the_live_cwd(self):
        # The live cwd is read from the pane's foreground process, which right
        # after new-window can be not there yet (empty: "/New Session"). The
        # folder new-window was given is always there. A pane that moved away
        # at once shows which one the birth reads, every time.
        # (Not -d: run-shell -C's commands act on the session's current window.)
        wid = self.t("new-window", "-P", "-F", "#{window_id}", "-t", "main:", "-c", self.proj,
                     "cd /usr && exec sleep 600")
        deadline = time.time() + 4
        while self.t("display", "-p", "-t", wid, "#{pane_current_path}") != "/usr" and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.t("display", "-p", "-t", wid, "#{pane_current_path}"), "/usr")
        self.t("run-shell", "-C", "#{@agent_birth} claude")
        self.assert_born(self.rows()[wid])

    def test_project_right_after_new_window_every_time(self):
        # The live race itself: birth in new-window's own command list.
        for _ in range(15):
            before = self.rows()
            self.t("new-window", "-a", "-t", "main:", "-c", self.proj, ";", "run-shell", "-C", "#{@agent_birth} claude")
            self.assert_born(self.first_sighting(before))


def born_window(born, tty="/dev/ttys950", **extra):
    opts = {"@agent_state": "idle", "@agent_kind": "claude", "@agent_born": str(born),
            "@agent_since": f"{born} idle", "@agent_summary": "my-project/New Session"}
    opts.update(extra)
    return jw.W("main", 1, **opts), {"window": "@1", "tty": tty}


class WatcherGrace(unittest.TestCase):
    """agent-tab-watcher.sh's BIRTH GRACE (GC_TICKS is 5)."""

    def env_for(self, born, tty="/dev/ttys950", **extra):
        w, pane = born_window(born, tty, **extra)
        self.f = jw.FakeEnv(windows={"@1": w, "@2": jw.W("main", 2)},
                            panes=[pane, {"window": "@2", "tty": "/dev/ttys901"}])
        self.addCleanup(self.f.close)

    def run_ticks(self, n):
        r = self.f.run("agent-tab-watcher.sh", timeout=30, AGENT_TAB_WATCHER_MAX_TICKS=str(n))
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.f.read()["windows"]["@1"].get("opts", {})

    def test_fresh_birth_is_held_past_gc_ticks(self):
        now = int(time.time())
        self.env_for(now)
        opts = self.run_ticks(7)   # more than GC_TICKS agent-less ticks
        self.assertEqual(opts.get("@agent_state"), "idle")
        self.assertEqual(opts.get("@agent_born"), str(now))
        self.assertEqual(opts.get("@agent_summary"), "my-project/New Session")
        self.assertEqual(opts.get("@agent_since"), f"{now} idle")   # not restamped

    def test_expired_birth_is_collected_with_the_mark(self):
        old = int(time.time()) - 40
        self.env_for(old)
        opts = self.run_ticks(6)
        for k in ("@agent_state", "@agent_born", "@agent_summary", "@agent_kind", "@agent_since"):
            self.assertNotIn(k, opts)

    def test_expired_mark_alone_is_collected(self):
        # SessionEnd cleared the state before any tick saw the agent; an old
        # watcher-less mark must not linger.
        old = int(time.time()) - 40
        self.env_for(old)
        s = self.f.read()
        s["windows"]["@1"]["opts"] = {"@agent_born": str(old)}
        self.f.state.write_text(json.dumps(s))
        self.assertNotIn("@agent_born", self.run_ticks(6))

    def test_agent_arrival_retires_the_mark(self):
        now = int(time.time())
        self.env_for(now, tty="/dev/ttys900")   # jw.DEFAULT_PROCS: claude 4242 on ttys900
        opts = self.run_ticks(1)
        self.assertNotIn("@agent_born", opts)
        self.assertEqual(opts.get("@agent_state"), "idle")
        self.assertEqual(opts.get("@agent_kind"), "claude")

    def test_future_mark_beyond_grace_is_not_trusted(self):
        self.env_for(int(time.time()) + 3600)
        self.assertNotIn("@agent_state", self.run_ticks(6))


if __name__ == "__main__":
    unittest.main()
