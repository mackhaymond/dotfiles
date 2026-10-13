"""Last agent (agent-ui menu's Option-W Option-W): the session-window-changed[2]
and client-session-changed[2] hooks in tmux.conf.tmpl that keep the GLOBAL
options @agent_mru_cur / @agent_mru_prev (window ids of the agent window a
client last landed on, and the one before it).

Real tmux, never the live server: each test gets its own `-L` socket started
with `-f /dev/null`, sources ONLY the two hook blocks pulled out of
tmux.conf.tmpl, and attaches one client from a pseudo-terminal this process
opens itself (pty.fork), because switch-client and window_active_clients need
a real client. Teardown kills the client, kill-servers the socket and removes
the socket file. Run with unittest or pytest.
"""
import os
from pathlib import Path
import pty
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
import uuid

CONF = Path(__file__).parents[1] / "tmux.conf.tmpl"
TMUX = shutil.which("tmux")
HOOKS = ("session-window-changed[2]", "client-session-changed[2]")


BLOCK = re.compile(r"^set-hook -g '([^']+)' \{\n(.*?)\n\}$", re.M | re.S)


def hook_blocks():
    """{hook name: body} for the brace-block hooks that write @agent_mru_*."""
    return {m.group(1): m.group(2) for m in BLOCK.finditer(CONF.read_text())
            if "@agent_mru_" in m.group(2)}


def hook_conf():
    return "".join(f"set-hook -g '{n}' {{\n{b}\n}}\n" for n, b in hook_blocks().items())


@unittest.skipUnless(TMUX, "tmux not installed")
class ConfigShape(unittest.TestCase):
    def test_two_hooks_identical_bodies_no_fork(self):
        blocks = hook_blocks()
        self.assertEqual(sorted(blocks), sorted(HOOKS))
        bodies = set(blocks.values())
        self.assertEqual(len(bodies), 1, "the two hook bodies drifted apart")
        body = bodies.pop()
        self.assertNotIn("run-shell", body)
        self.assertNotIn("{{", body)  # chezmoi would read it as a template action
        self.assertTrue(all(l.strip().startswith("set -gF @agent_mru_") for l in body.splitlines()))

    def test_indexes_do_not_collide(self):
        # [0] and [1] belong to clear-current and cua-notch-visit.
        taken = re.findall(r"^set-hook -g '?(session-window-changed|client-session-changed)(\[\d+\])?",
                           CONF.read_text(), re.M)
        idx = [(n, i or "[0]") for n, i in taken]
        self.assertEqual(len(idx), len(set(idx)), idx)


@unittest.skipUnless(TMUX, "tmux not installed")
class AgentLast(unittest.TestCase):
    def setUp(self):
        self.sock = "agent-last-test-" + uuid.uuid4().hex[:12]
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env["TERM"] = "xterm-256color"
        self.path = None
        self.child = None
        self.addCleanup(self.reap)
        # session a: 0 agent A, 1 shell, 2 agent B, 3 agent C
        # session b: 0 agent D, 1 shell, 2 agent E
        self.t("new-session", "-d", "-s", "a", "-x", "80", "-y", "24", "/bin/sleep 600")
        self.path = self.t("display", "-p", "#{socket_path}")
        self.t("set", "-g", "exit-empty", "off")
        for _ in range(3):
            self.t("new-window", "-d", "-t", "a:", "/bin/sleep 600")
        self.t("new-session", "-d", "-s", "b", "/bin/sleep 600")
        for _ in range(2):
            self.t("new-window", "-d", "-t", "b:", "/bin/sleep 600")
        ids = dict(l.split("=") for l in self.t(
            "list-windows", "-a", "-F", "#{session_name}:#{window_index}=#{window_id}").splitlines())
        self.A, self.SH, self.B, self.C = ids["a:0"], ids["a:1"], ids["a:2"], ids["a:3"]
        self.D, self.SHB, self.E = ids["b:0"], ids["b:1"], ids["b:2"]
        for w in (self.A, self.B, self.C, self.D, self.E):
            self.t("set", "-w", "-t", w, "@agent_state", "idle")
        self.t("set", "-w", "-t", self.B, "@agent_state", "running")  # any state counts
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write(hook_conf())
        self.addCleanup(os.unlink, f.name)
        self.t("source-file", f.name)
        self.attach()
        # Park the client on the shell window, then forget everything.
        self.t("select-window", "-t", "a:1")
        time.sleep(0.2)
        self.reset()

    # -- harness ---------------------------------------------------------
    def t(self, *args):
        r = subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", *args], env=self.env,
                           capture_output=True, text=True, timeout=10)
        if r.returncode:
            raise AssertionError(f"tmux {args}: {r.stderr}")
        return r.stdout.strip()

    def attach(self):
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.execvpe(TMUX, [TMUX, "-L", self.sock, "-f", "/dev/null", "attach", "-t", "a"], self.env)
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

    def reset(self):
        for o in ("cur", "prev", "here", "left"):
            self.t("set", "-g", f"@agent_mru_{o}", "")

    def mru(self):
        return tuple(self.t("display", "-p", "#{@agent_mru_cur}|#{@agent_mru_prev}").split("|"))

    def expect(self, cur, prev):
        want = (cur, prev)
        deadline = time.time() + 3
        while time.time() < deadline:
            if self.mru() == want:
                break
            time.sleep(0.03)
        time.sleep(0.15)  # nothing late may overwrite it
        self.assertEqual(self.mru(), want)

    def on(self):
        return self.t("display", "-c", self.client, "-p", "#{window_id}")

    def sel(self, target):
        self.t("select-window", "-t", target)

    def switch(self, target):
        self.t("switch-client", "-c", self.client, "-t", target)

    # -- tests -------------------------------------------------------------
    def test_a_to_b(self):
        self.sel("a:0"); self.expect(self.A, "")
        self.sel("a:2"); self.expect(self.B, self.A)
        self.assertEqual(self.on(), self.B)

    def test_a_shell_b(self):
        self.sel("a:0"); self.expect(self.A, "")
        self.sel("a:1"); self.expect(self.A, "")   # a shell changes nothing
        self.sel("a:2"); self.expect(self.B, self.A)

    def test_back_and_forth_swaps(self):
        self.sel("a:0"); self.sel("a:2"); self.expect(self.B, self.A)
        self.sel("a:0"); self.expect(self.A, self.B)
        self.sel("a:2"); self.expect(self.B, self.A)

    def test_noop_select_changes_nothing(self):
        self.sel("a:0"); self.sel("a:2"); self.expect(self.B, self.A)
        self.sel("a:2"); self.expect(self.B, self.A)
        self.switch("a"); self.expect(self.B, self.A)  # switch to own session: no-op too

    def test_cross_session_switch_client(self):
        self.sel("a:2"); self.expect(self.B, "")
        self.switch("b"); self.expect(self.D, self.B)   # b's current window unchanged
        self.switch("a"); self.expect(self.B, self.D)

    def test_agent_jump_select_then_switch_one_call(self):
        # agent-jump.sh: select-window then switch-client in one command list.
        self.sel("a:2"); self.expect(self.B, "")
        self.t("select-window", "-t", "b:2", ";", "switch-client", "-c", self.client, "-t", "b")
        self.expect(self.E, self.B)
        self.assertEqual(self.on(), self.E)

    def test_cross_session_noop_select_lands_right(self):
        # The after-select-window trap: the target is already b's current
        # window, so select-window is a no-op and would report a:'s window.
        self.sel("a:2"); self.expect(self.B, "")
        self.t("select-window", "-t", "b:0", ";", "switch-client", "-c", self.client, "-t", "b")
        self.expect(self.D, self.B)

    def test_select_in_unwatched_session_waits_for_the_client(self):
        self.sel("a:2"); self.expect(self.B, "")
        self.sel("b:2"); self.expect(self.B, "")   # nobody is looking at b
        self.switch("b"); self.expect(self.E, self.B)

    def test_switch_client_window_target(self):
        self.sel("a:0"); self.expect(self.A, "")
        self.switch("b:2"); self.expect(self.E, self.A)

    def test_next_previous_last_window(self):
        self.sel("a:0"); self.expect(self.A, "")
        self.t("next-window", "-t", "a"); self.expect(self.A, "")      # shell
        self.t("next-window", "-t", "a"); self.expect(self.B, self.A)
        self.t("next-window", "-t", "a"); self.expect(self.C, self.B)
        self.t("last-window", "-t", "a"); self.expect(self.B, self.C)
        self.t("previous-window", "-t", "a"); self.expect(self.B, self.C)  # shell
        self.t("previous-window", "-t", "a"); self.expect(self.A, self.B)

    def become_agent(self, w):
        self.t("set", "-w", "-t", w, "@agent_state", "idle")  # claude started in it

    def test_reviewer_repro_menu_go_from_a_new_agent(self):
        # B, then a shell that turns into an agent while the client is on it.
        self.sel("a:2"); self.expect(self.B, "")
        self.sel("a:1"); self.expect(self.B, "")
        self.become_agent(self.SH)
        # Option-W Option-W from SH: the menu goes to cur=B (agent-jump shape).
        self.t("select-window", "-t", "a:2", ";", "switch-client", "-c", self.client, "-t", "a")
        self.expect(self.B, self.SH)   # SH was the window being left
        # ...and Option-W Option-W from B comes back to SH.
        self.sel("a:1"); self.expect(self.SH, self.B)

    def test_cmd_n_exit_from_a_new_agent(self):
        self.sel("a:2"); self.expect(self.B, "")
        self.sel("a:1"); self.expect(self.B, "")
        self.become_agent(self.SH)
        self.sel("a:0"); self.expect(self.A, self.SH)   # Cmd-N sends a select-window
        self.sel("a:1"); self.expect(self.SH, self.A)

    def test_switch_client_exit_from_a_new_agent(self):
        self.sel("a:0"); self.sel("a:1"); self.expect(self.A, "")
        self.become_agent(self.SH)
        self.switch("b"); self.expect(self.D, self.SH)
        self.switch("a"); self.expect(self.SH, self.D)

    def test_shell_left_behind_stays_out(self):
        self.sel("a:2"); self.sel("a:1"); self.sel("a:0")
        self.expect(self.A, self.B)                     # the shell never enters

    def test_burst_in_one_command_list(self):
        # Hooks run after the whole list: the pass-through windows record
        # nothing and the chain stays consistent (cur=C, prev=B).
        self.sel("a:2"); self.expect(self.B, "")
        self.t("select-window", "-t", "a:0", ";", "select-window", "-t", "a:1", ";",
               "select-window", "-t", "a:3")
        self.expect(self.C, self.B)
        self.sel("a:2"); self.expect(self.B, self.C)

    def test_killed_current_window_is_not_pushed(self):
        self.sel("a:0"); self.sel("a:2"); self.expect(self.B, self.A)
        self.t("kill-window", "-t", "a:2")            # tmux falls back to a:0
        self.assertEqual(self.on(), self.A)
        self.expect(self.A, self.B)                   # B lingers; the reader drops it
        self.sel("a:3"); self.expect(self.C, self.A)


if __name__ == "__main__":
    unittest.main()
