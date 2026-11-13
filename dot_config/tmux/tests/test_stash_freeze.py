"""stash.sh: nothing in the stash is running (2026-11-11).

  - a parked tab's foreground job is FROZEN (⌃Z, SIGSTOP as the fallback, and
    straight away on a raw terminal) and recorded in @stash_frozen as its
    exact argv, shell-quoted; background jobs are stopped too; unparking types
    `fg` and continues them;
  - after a tmux restart (the sidecar's appended field 11, restore-state's
    @stash_cold) the recorded command is re-run in its directory instead;
  - refused up front: ssh in the foreground, a split tab with an agent, a
    one-pane agent tab closed-tabs.sh will not pin;
  - a one-pane agent tab is closed into a pinned entry (`close --pin`), never
    parked; a job closed-tabs.sh finds no agent in is parked and frozen;
  - `rows`, `count` / `publish-count` (pinned entries included), and
    `kill-many` closing through closed-tabs.sh.

Real tmux on a private `-L` socket started with `-f /dev/null`, HOME, TMPDIR
and XDG_STATE_HOME in a temp dir, NOTIFY_SH a missing file, STASH_SELF a
wrapper that runs THIS copy of the script (the thaw is handed off through
run-shell), CLOSED_TABS_SH a stub that records its calls. Every process a test
starts is killed, and the server and its socket are removed, in cleanup.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
import uuid

HERE = Path(__file__).resolve().parents[1]
STASH = Path(os.environ.get("STASH_UNDER_TEST", HERE / "scripts" / "executable_stash.sh"))
TMUX = shutil.which("tmux")
SEP = "\x1f"
import sys  # noqa: E402
PYTHON = os.path.realpath(sys.executable)

# closed-tabs.sh stand-in. `close --pin P`: MODE decides (pin = kill the pane
# and succeed, refuse = exit 3 with a reason, noagent = exit 3 "no agent").
# `close P`: kill the pane. Every call is recorded with the pane's session.
STUB = r'''#!/bin/bash
calls="$(dirname "$0")/closed-calls"
pin=""; [ "$1" = close ] && [ "$2" = --pin ] && { pin=1; set -- close "$3"; }
where=$(tmux display-message -p -t "$2" '#{session_name}' 2>/dev/null)
echo "$1${pin:+ --pin} $2 $where" >> "$calls"
mode=$(cat "$(dirname "$0")/closed-mode" 2>/dev/null)
if [ -n "$pin" ]; then
    case "$mode" in
        pin) tmux kill-pane -t "$2"; exit 0 ;;
        refuse) echo "closed-tabs: not stashed — couldn't find the claude session id" >&2; exit 3 ;;
        *) echo "closed-tabs: not stashed — no agent in this pane" >&2; exit 3 ;;
    esac
fi
[ "$1" = close ] && tmux kill-pane -t "$2"
exit 0
'''


@unittest.skipUnless(TMUX, "tmux not installed")
class Base(unittest.TestCase):
    def setUp(self):
        self.sock = "stash-frz-" + uuid.uuid4().hex[:12]
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="stash-frz-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pids = []
        wrapper = Path(self.tmp, "stash-self")
        wrapper.write_text(f'#!/bin/sh\nexec bash "{STASH}" "$@"\n')
        wrapper.chmod(0o755)
        self.stub = Path(self.tmp, "closed-tabs.sh")
        self.stub.write_text(STUB)
        self.calls = Path(self.tmp, "closed-calls")
        self.mode = Path(self.tmp, "closed-mode")
        self.rdir = Path(self.tmp, "resurrect")
        self.rdir.mkdir()
        Path(self.tmp, "Library/Logs").mkdir(parents=True)        # stash.sh's log
        self.work = Path(self.tmp, "work dir")
        self.work.mkdir()
        # A plain python3 first on PATH: a pyenv shim cannot find its root
        # with HOME in a temp dir, and stash.sh reads argv through python3.
        pybin = Path(self.tmp, "pybin")
        pybin.mkdir()
        (pybin / "python3").symlink_to(PYTHON)
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env["PATH"] = f"{pybin}:{self.env.get('PATH', '')}"
        self.env.update(TERM="xterm-256color", HOME=self.tmp, TMPDIR=self.tmp + "/", ZDOTDIR=self.tmp,
                        AGENT_FOCUS_NOTICE="", XDG_STATE_HOME=os.path.join(self.tmp, "state"),
                        NOTIFY_SH=os.path.join(self.tmp, "no-notify"), STASH_SELF=str(wrapper),
                        CLOSED_TABS_SH=str(self.stub), SHELL="/bin/zsh")
        self.path = None
        self.addCleanup(self.reap)
        self.start_server()

    def start_server(self):
        self.t("new-session", "-d", "-s", "main", "-x", "120", "-y", "30", "-c", self.tmp)
        self.path = self.t("display", "-p", "#{socket_path}")
        self.env["STASH_ALLOW_SOCKET"] = self.path
        self.t("set", "-g", "exit-empty", "off")
        self.t("set", "-g", "base-index", "1")
        self.t("set", "-g", "default-shell", "/bin/zsh")
        self.t("set", "-g", "@resurrect-dir", str(self.rdir))
        self.t("set-environment", "-g", "STASH_ALLOW_SOCKET", self.path)

    # -- harness ---------------------------------------------------------
    def t(self, *args, check=True):
        r = subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", *args], env=self.env,
                           capture_output=True, text=True, timeout=10)
        if check and r.returncode:
            raise AssertionError(f"tmux {args}: {r.stderr}")
        return r.stdout.strip()

    def reap(self):
        for pid in self.pids + self.tty_pids():
            for sig in (signal.SIGCONT, signal.SIGKILL):
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
        subprocess.run([TMUX, "-L", self.sock, "kill-server"], env=self.env, capture_output=True, timeout=10)
        if self.path:
            Path(self.path).unlink(missing_ok=True)

    def tty_pids(self):
        """Every process on any pane of the test server (cleanup's net)."""
        r = subprocess.run([TMUX, "-L", self.sock, "list-panes", "-a", "-F", "#{pane_tty}"], env=self.env,
                           capture_output=True, text=True, timeout=10)
        out = []
        for tty in r.stdout.split():
            ps = subprocess.run(["ps", "-t", tty.replace("/dev/", ""), "-o", "pid="], capture_output=True, text=True)
            out += [int(p) for p in ps.stdout.split() if p.isdigit()]
        return out

    def script_env(self):
        return dict(self.env, TMUX=f"{self.path},0,0")

    def script(self, *args):
        r = subprocess.run(["bash", str(STASH), *args], env=self.script_env(),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def source(self, body, stderr=False, env=None):
        """bash with the script's functions loaded (its `count` verb runs on
        the way in, harmlessly), then `body`. stderr=True: (stdout, stderr)."""
        r = subprocess.run(["bash", "-c", f'source "$1" count >/dev/null\n{body}', "_", str(STASH)],
                           env=dict(self.script_env(), **(env or {})), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return (r.stdout, r.stderr) if stderr else r.stdout

    def log(self):
        try:
            return Path(self.tmp, "Library/Logs/tmux-stash.log").read_text()
        except FileNotFoundError:
            return ""

    def new_window(self, session="main"):
        w = self.t("new-window", "-d", "-t", f"{session}:", "-c", str(self.work), "-P", "-F", "#{window_id}")
        self.wait_prompt(self.pane(w))
        return w

    def pane(self, win, index=None):
        rows = self.t("list-panes", "-t", win, "-F", "#{pane_index} #{pane_id}").splitlines()
        if index is None:
            return rows[0].split()[1]
        return dict(r.split() for r in rows)[str(index)]

    def pane_pid(self, pane):
        return int(self.t("display", "-p", "-t", pane, "#{pane_pid}"))

    def tpgid(self, pane):
        return int(subprocess.run(["ps", "-o", "tpgid=", "-p", str(self.pane_pid(pane))],
                                  capture_output=True, text=True).stdout.strip() or 0)

    def wait_prompt(self, pane, timeout=10):
        """The shell is up and has drawn a prompt."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.t("capture-pane", "-p", "-t", pane).strip():
                return
            time.sleep(0.05)
        self.fail("shell never drew a prompt")

    def run_job(self, pane, line, timeout=10):
        """Type `line` at the shell; wait until a job holds the terminal.
        Returns the foreground job's leader pid."""
        self.t("send-keys", "-t", pane, "-l", line)
        self.t("send-keys", "-t", pane, "Enter")
        deadline = time.time() + timeout
        while time.time() < deadline:
            fg = self.tpgid(pane)
            if fg and fg != self.pane_pid(pane):
                self.pids.append(fg)
                time.sleep(0.2)
                return fg
            time.sleep(0.05)
        self.fail(f"{line!r} never took the foreground")

    @staticmethod
    def state(pid):
        return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()

    @staticmethod
    def argv_of(pid):
        return subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()

    def session_of(self, win):
        return self.t("display", "-p", "-t", win, "#{session_name}", check=False)

    def opt(self, win, name):
        return self.t("show", "-wqv", "-t", win, name, check=False)

    def wait_for(self, cond, what, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return
            time.sleep(0.05)
        self.fail(what + "\n" + self.log())

    def keep_one(self):
        """A spare tab, so parking never takes a session's last one."""
        self.new_window("main")


class WarmFreeze(Base):
    def test_sleep_is_stopped_parked_and_runs_again_on_unpark(self):
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        job = self.run_job(p, "sleep 1000")
        self.script("stash-many", w)
        self.assertEqual(self.session_of(w), "stash")
        self.assertTrue(self.state(job).startswith("T"), self.state(job))
        frozen = json.loads(self.opt(w, "@stash_frozen"))
        self.assertEqual(len(frozen), 1)
        self.assertNotIn("argv", frozen[0])                  # cmd only: tmux's 16 KB limit
        self.assertEqual(frozen[0]["cmd"], "sleep 1000")
        self.assertEqual(frozen[0]["pgid"], job)
        self.assertEqual(frozen[0]["cwd"], str(self.work))
        rows = [r.split(SEP) for r in self.script("rows").splitlines()]
        self.assertEqual([(r[0], r[1], r[4]) for r in rows], [(w, "main", "frozen")])
        # CPU 0: a stopped process accrues no time.
        t0 = subprocess.run(["ps", "-o", "time=", "-p", str(job)], capture_output=True, text=True).stdout
        time.sleep(0.5)
        self.assertEqual(subprocess.run(["ps", "-o", "time=", "-p", str(job)], capture_output=True, text=True).stdout, t0)

        self.script("unstash-many", w)
        self.assertEqual(self.session_of(w), "main")
        self.wait_for(lambda: not self.state(job).startswith("T"), "the job never ran again")
        self.wait_for(lambda: self.tpgid(p) == job, "the job is not back in the foreground")
        self.wait_for(lambda: self.opt(w, "@stash_frozen") == "", "@stash_frozen was not cleared")

    def test_http_server_is_frozen_and_comes_back(self):
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        job = self.run_job(p, "python3 -m http.server 0 --bind 127.0.0.1")
        self.script("stash-many", w)
        self.assertTrue(self.state(job).startswith("T"), self.state(job))
        frozen = json.loads(self.opt(w, "@stash_frozen"))
        self.assertTrue(frozen[0]["cmd"].endswith(" -m http.server 0 --bind 127.0.0.1"), frozen)
        self.script("unstash-many", w)
        self.wait_for(lambda: not self.state(job).startswith("T"), "http.server never ran again")

    def test_a_job_ignoring_ctrl_z_is_sigstopped_and_fg_still_brings_it_back(self):
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        job = self.run_job(p, "python3 -c 'import signal, time; signal.signal(signal.SIGTSTP, "
                              "signal.SIG_IGN); time.sleep(1000)'")
        self.script("stash-many", w)
        self.assertTrue(self.state(job).startswith("T"), self.state(job))
        self.assertIn("SIGSTOP", self.log())
        self.script("unstash-many", w)
        self.wait_for(lambda: not self.state(job).startswith("T"), "the job never ran again")
        self.wait_for(lambda: self.tpgid(p) == job, "the job is not back in the foreground")

    def test_idle_shell_has_nothing_to_freeze(self):
        self.keep_one()
        w = self.new_window()
        self.script("stash-many", w)
        self.assertEqual(self.session_of(w), "stash")
        self.assertEqual(self.opt(w, "@stash_frozen"), "")
        self.assertEqual(self.script("rows").split(SEP)[4], "idle")
        self.assertEqual(self.calls.exists(), False)          # no close --pin for an idle shell


class ColdRestart(Base):
    def test_restart_restores_cold_and_reruns_the_exact_command(self):
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        # Quoting ps would flatten: a space inside an argument, and a quote.
        line = """python3 -c 'import time; time.sleep(1000)' "a b" "it's" """
        job = self.run_job(p, line)
        self.script("stash-many", w)
        self.assertTrue(self.state(job).startswith("T"))
        frozen = json.loads(self.opt(w, "@stash_frozen"))
        self.assertTrue(frozen[0]["cmd"].endswith(""" -c 'import time; time.sleep(1000)' 'a b' 'it'\\''s'"""), frozen)
        self.script("publish")
        rows = [r.split(SEP) for r in (self.rdir / "stash-state.tsv").read_text().splitlines()]
        parked = [r for r in rows if r[0] == "stash"]
        self.assertEqual(len(parked), 1)
        self.assertEqual(len(parked[0]), 11)
        self.assertEqual(json.loads(parked[0][10]), frozen)
        idx = parked[0][1]

        # The restart: the server dies with its processes; resurrect rebuilds
        # the layout with plain shells (resurrect-save-repair.py saves stash
        # panes with no command), then runs restore-state.
        self.reap()
        self.wait_for(lambda: self.state(job) == "", "the frozen job outlived its server")
        self.start_server()
        self.keep_one()
        self.t("new-session", "-d", "-s", "stash", "-c", str(self.work))
        self.assertEqual(idx, "1")
        w2 = self.t("list-windows", "-t", "stash", "-F", "#{window_id}")
        self.wait_prompt(self.pane(w2))
        self.script("restore-state")
        self.assertEqual(self.opt(w2, "@stash_cold"), "1", self.log())
        self.assertEqual(self.opt(w2, "@stash_origin"), "main")
        self.assertEqual(self.script("rows").split(SEP)[4], "cold")

        self.script("unstash-many", w2)
        p2 = self.pane(w2)
        self.wait_for(lambda: self.tpgid(p2) != self.pane_pid(p2), "the command was not re-run")
        new = self.tpgid(p2)
        self.pids.append(new)
        self.wait_for(lambda: "time.sleep(1000)" in self.argv_of(new), "wrong process re-run")
        out = subprocess.run([PYTHON, "-I", "-c", "import sys,json; exec(sys.argv[1])",
                              FROZEN_ARGV, str(new)], capture_output=True, text=True).stdout
        self.assertEqual(json.loads(out)[1:], ["-c", "import time; time.sleep(1000)", "a b", "it's"])
        self.assertEqual(Path(os.path.realpath(self.t("display", "-p", "-t", p2, "#{pane_current_path}"))),
                         Path(os.path.realpath(self.work)))
        self.wait_for(lambda: self.opt(w2, "@stash_cold") == "", "@stash_cold was not cleared")

    def test_old_ten_field_rows_still_restore(self):
        self.keep_one()
        self.t("new-session", "-d", "-s", "stash", "-c", str(self.work))
        w = self.t("list-windows", "-t", "stash", "-F", "#{window_id}")
        (self.rdir / "stash-state.tsv").write_text(
            SEP.join(["stash", "1", "zsh", "", "main", "lbl", "", "", "100", str(self.work)]) + "\n")
        self.script("restore-state")
        self.assertEqual(self.opt(w, "@stash_origin"), "main")
        self.assertEqual(self.opt(w, "@stash_label"), "lbl")
        self.assertEqual(self.opt(w, "@stash_cold"), "")
        self.assertEqual(self.opt(w, "@stash_frozen"), "")


# KERN_PROCARGS2 again, for the assertion (ps would flatten the argv).
FROZEN_ARGV = r'''
import ctypes, ctypes.util, struct
libc = ctypes.CDLL(ctypes.util.find_library("c"))
def sysctl(mib, size):
    arr = (ctypes.c_int * len(mib))(*mib); buf = ctypes.create_string_buffer(size); n = ctypes.c_size_t(size)
    assert libc.sysctl(arr, len(mib), buf, ctypes.byref(n), None, ctypes.c_size_t(0)) == 0
    return buf.raw[:n.value]
raw = sysctl([1, 49, int(sys.argv[2])], 1 << 20)
argc = struct.unpack("i", raw[:4])[0]; i = raw.find(b"\0", 4)
while raw[i] == 0: i += 1
out = []
for _ in range(argc):
    j = raw.find(b"\0", i); out.append(raw[i:j].decode()); i = j + 1
print(json.dumps(out))
'''


class Refusals(Base):
    def fake(self, name):
        f = Path(self.tmp, "bin", name)
        f.parent.mkdir(exist_ok=True)
        shutil.copy("/bin/sleep", f)
        return f

    def test_ssh_in_the_foreground_is_refused(self):
        self.keep_one()
        w = self.new_window()
        job = self.run_job(self.pane(w), f"'{self.fake('ssh')}' 1000")
        self.script("stash-many", w)
        self.assertEqual(self.session_of(w), "main")
        self.assertFalse(self.state(job).startswith("T"))
        self.assertIn("ssh in the foreground", self.log())

    def test_a_registered_claudes_deep_guards_refuse_before_anything_moves(self):
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        self.run_job(p, f"'{self.fake('claude')}' 1000")
        clear = {"is_working": 1, "has_unsent_input": 1, "session_has_running_workflow": 1,
                 "session_has_running_subagent": 1, "has_live_cua": 1}
        cases = [("has_unsent_input", "unsent text in the agent's composer"),
                 ("is_working", "that agent is still working"),
                 ("session_has_running_workflow", "background work (workflow or subagent) still in flight"),
                 ("session_has_running_subagent", "a background subagent is still running"),
                 ("has_live_cua", "it's driving an app through cua"),
                 (None, "")]
        for hit, why in cases:
            with self.subTest(hit=hit):
                stubs = "\n".join(f"{f}() {{ return {0 if f == hit else rc}; }}" for f, rc in clear.items())
                out = self.source(f"""
live_sessions() {{ printf '%s{SEP}%s{SEP}idle{SEP}%s{SEP}%s{SEP}%s\\n' 123 sid '{w}' '{p}' /tmp; }}
pid_in_window() {{ return 0; }}
{stubs}
classify_window '{w}'; printf '%s|%s' "$CL_KIND" "$CL_WHY"
""")
                self.assertEqual(out, f"refuse|{why}" if hit else "pin|")
        self.assertEqual(self.session_of(w), "main")

    def test_split_tab_with_an_agent_is_refused(self):
        self.keep_one()
        w = self.new_window()
        self.t("split-window", "-d", "-t", w, "-c", str(self.work))
        self.wait_prompt(self.pane(w, 1))
        job = self.run_job(self.pane(w, 1), f"'{self.fake('claude')}' 1000")
        self.script("stash-many", w)
        self.assertEqual(self.session_of(w), "main")
        self.assertFalse(self.state(job).startswith("T"))
        self.assertIn("split tab with an agent — can't park it yet", self.log())
        self.assertFalse(self.calls.exists())

    def test_one_pane_agent_is_pinned_not_parked(self):
        self.mode.write_text("pin")
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        # codex: a fake claude's screen has no composer, which reads as "a
        # draft may be there" (has_unsent_input) and is refused — tested above.
        self.run_job(p, f"'{self.fake('codex')}' 1000")
        self.script("stash-many", w)
        self.assertEqual(self.session_of(w), "")                 # closed, not parked
        self.assertEqual(self.calls.read_text().split()[:3], ["close", "--pin", p])
        self.assertNotIn("stash", self.t("list-sessions", "-F", "#{session_name}").split())

    def test_an_agent_closed_tabs_will_not_pin_stays(self):
        self.mode.write_text("refuse")
        self.keep_one()
        w = self.new_window()
        job = self.run_job(self.pane(w), f"'{self.fake('codex')}' 1000")
        self.script("stash-many", w)
        self.assertEqual(self.session_of(w), "main")
        self.assertFalse(self.state(job).startswith("T"))
        self.assertIn("couldn't find the claude session id", self.log())

    def test_a_non_agent_job_is_parked_and_frozen_without_a_pin_probe(self):
        # #8: no `close --pin` probe on a non-agent tab (one that met an
        # agent somewhere under the pane would kill the whole pane).
        self.mode.write_text("pin")
        self.keep_one()
        w = self.new_window()
        job = self.run_job(self.pane(w), "sleep 1000")
        self.script("stash-many", w)
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.session_of(w), "stash")
        self.assertTrue(self.state(job).startswith("T"))

    def test_an_agent_run_by_the_foreground_job_does_not_make_the_tab_an_agent(self):
        # #8: `make review` running `claude -p` — the agent is not the tab's.
        self.mode.write_text("pin")
        self.keep_one()
        w = self.new_window()
        runner = Path(self.tmp, "runner.sh")
        runner.write_text(f"#!/bin/bash\n'{self.fake('claude')}' 1000\n")
        runner.chmod(0o755)
        job = self.run_job(self.pane(w), f"'{runner}'")
        self.script("stash-many", w)
        self.assertFalse(self.calls.exists())                 # never closed
        self.assertEqual(self.session_of(w), "stash")
        self.assertTrue(self.state(job).startswith("T"))

    def test_abby_and_other_harnesses_are_rechecked_before_the_close(self):
        # #7: every harness's tab state, and claude/abby's composer, right
        # before close --pin — not only a registered claude's.
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        self.run_job(p, f"'{self.fake('abby')}' 1000")
        out = self.source(f"has_unsent_input() {{ return 0; }}\nclassify_window '{w}'; printf '%s|%s' \"$CL_KIND\" \"$CL_WHY\"")
        self.assertEqual(out, "refuse|unsent text in the agent's composer")
        out = self.source(f"has_unsent_input() {{ return 1; }}\nclassify_window '{w}'; printf '%s|%s' \"$CL_KIND\" \"$CL_AGENT\"")
        self.assertEqual(out, "pin|abby")
        for agent, setup, why in (("codex", "@agent_state running", "that agent is still working"),
                                  ("opencode", "@agent_workflow 1", "that agent is still working"),
                                  ("abby", "", "unsent text in the agent's composer")):
            with self.subTest(agent=agent):
                for o in ("@agent_state", "@agent_workflow"):
                    self.t("set", "-uw", "-t", w, o)
                if setup:
                    self.t("set", "-w", "-t", w, *setup.split())
                out = self.source(f"has_unsent_input() {{ return {0 if agent == 'abby' else 1}; }}\n"
                                  f"pin_recheck '{w}' '{p}' {agent}; printf '%s|%s' $? \"$RECHECK_WHY\"")
                self.assertEqual(out, f"0|{why}")
        out = self.source(f"has_unsent_input() {{ return 1; }}\npin_recheck '{w}' '{p}' codex; echo $?")
        self.assertEqual(out.strip(), "1")

    def test_a_range_parks_what_it_can_and_keeps_the_rest(self):
        self.keep_one()
        idle = self.new_window()
        remote = self.new_window()
        busy = self.new_window()
        ssh = self.run_job(self.pane(remote), f"'{self.fake('mosh-client')}' 1000")
        job = self.run_job(self.pane(busy), "sleep 1000")
        self.script("stash-many", idle, remote, busy)
        self.assertEqual(self.session_of(idle), "stash")
        self.assertEqual(self.session_of(busy), "stash")
        self.assertEqual(self.session_of(remote), "main")
        self.assertTrue(self.state(job).startswith("T"))
        self.assertFalse(self.state(ssh).startswith("T"))


class ReviewFixes(Base):
    """The 2026-11-12 review round, one regression test per finding."""

    def test_1_raw_mode_vim_gets_sigstop_not_a_literal_ctrl_z(self):
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        f = Path(self.work, "note.txt")
        job = self.run_job(p, f"vim -u NONE -N -n '{f}'")
        time.sleep(0.5)
        self.t("send-keys", "-t", p, "-l", "iabc")
        time.sleep(0.3)
        self.script("stash-many", w)
        self.assertTrue(self.state(job).startswith("T"), self.state(job))
        self.assertIn("raw terminal — SIGSTOP", self.log())
        self.script("unstash-many", w)
        self.wait_for(lambda: self.tpgid(p) == job and not self.state(job).startswith("T"), "vim did not come back")
        time.sleep(0.3)
        self.t("send-keys", "-t", p, "Escape")
        self.t("send-keys", "-t", p, "-l", ":wq")
        self.t("send-keys", "-t", p, "Enter")
        self.wait_for(lambda: f.exists() and self.state(job) == "", "vim did not write and quit")
        self.assertEqual(f.read_bytes(), b"abc\n")

    def test_2_a_rewritten_argv_is_command_unknown_and_cold_types_nothing(self):
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        job = self.run_job(p, """perl -e '$0 = "npm run dev"; sleep 1000' x y""")
        self.script("stash-many", w)
        frozen = json.loads(self.opt(w, "@stash_frozen"))
        self.assertEqual(frozen[0]["cmd"], "")
        # The restart, cut short: the process is gone, the window marked cold.
        os.kill(job, signal.SIGKILL)
        self.wait_for(lambda: self.state(job) == "", "perl outlived its SIGKILL")
        self.t("set", "-w", "-t", w, "@stash_cold", "1")
        self.script("unstash-many", w)
        self.wait_for(lambda: self.opt(w, "@stash_frozen") == "", "record not cleared")
        time.sleep(0.5)
        self.assertEqual(self.tpgid(p), self.pane_pid(p))         # nothing re-run
        self.assertEqual(self.opt(w, "@stash_cold"), "")

    def test_nit_a_pipeline_is_command_unknown_but_still_freezes_and_thaws(self):
        # Its members' order is not knowable from pids, so no cmd is guessed.
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        job = self.run_job(p, "sleep 1000 | cat")
        self.script("stash-many", w)
        frozen = json.loads(self.opt(w, "@stash_frozen"))
        self.assertEqual(frozen[0]["cmd"], "")
        self.assertTrue(self.state(job).startswith("T"))
        self.script("unstash-many", w)
        self.wait_for(lambda: not self.state(job).startswith("T"), "the pipeline did not resume")

    def test_3_kill_many_leaves_a_live_agents_sigterm_to_closed_tabs(self):
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        agent = self.run_job(p, f"'{self.fake('claude')}' 1000")
        self.park_by_hand(w)
        sess = Path(self.tmp, ".claude/sessions")
        sess.mkdir(parents=True)
        (sess / f"{agent}.json").write_text(json.dumps(
            {"pid": agent, "sessionId": "s-1", "status": "idle", "tmux": f"stash:{w}.{p}", "cwd": str(self.work)}))
        # The stub notes whether the agent was still alive when close ran.
        self.stub.write_text(STUB.replace('echo "$1${pin:+ --pin} $2 $where"',
                                          f'echo "$1${{pin:+ --pin}} $2 $where $(kill -0 {agent} 2>/dev/null && echo alive)"'))
        self.script("kill-many", w)
        self.assertEqual(self.calls.read_text().split(), ["close", p, "main", "alive"])

    def test_3_kill_many_keeps_the_old_path_for_a_suspended_session(self):
        self.keep_one()
        w = self.new_window()
        self.park_by_hand(w)
        self.t("set", "-w", "-t", w, "@stash_session", "aaaa-bbbb")
        self.t("set", "-w", "-t", w, "@stash_cwd", str(self.work))
        self.script("kill-many", w)
        self.assertEqual(self.session_of(w), "")
        self.assertFalse(self.calls.exists())                 # a plain kill-window, as before
        self.assertIn("resume by hand: cd", self.log())
        self.assertIn("claude --resume aaaa-bbbb", self.log())

    def test_4_cwd_is_the_shells_not_the_jobs(self):
        self.keep_one()
        Path(self.work, "sub").mkdir()
        dev = Path(self.work, "dev.sh")
        dev.write_text("#!/bin/bash\ncd sub\nsleep 1000\n")
        dev.chmod(0o755)
        w = self.new_window()
        self.run_job(self.pane(w), "./dev.sh")
        self.script("stash-many", w)
        frozen = json.loads(self.opt(w, "@stash_frozen"))
        self.assertEqual(os.path.realpath(frozen[0]["cwd"]), os.path.realpath(self.work))

    def test_5_a_pending_resume_is_a_row_and_unstash_resumes_it(self):
        self.keep_one()
        parked = self.new_window()
        self.script("stash-many", parked)
        w = self.new_window()
        self.t("set", "-w", "-t", w, "@stash_session", "aaaa-bbbb")
        self.t("set", "-w", "-t", w, "@stash_cwd", str(self.work))
        self.t("set", "-w", "-t", w, "@stash_label", "old agent")
        rows = {r.split(SEP)[0]: r.split(SEP) for r in self.script("rows").splitlines()}
        self.assertEqual(rows[w][1:3] + rows[w][4:5], ["main", "old agent", "pending"])
        self.assertEqual(rows[parked][4], "idle")
        rec = Path(self.tmp, "rec")
        rec.write_text(f'#!/bin/sh\necho "$*" >> "{self.tmp}/self-calls"\n')
        rec.chmod(0o755)
        subprocess.run(["bash", str(STASH), "unstash", w, ""], env=dict(self.script_env(), STASH_SELF=str(rec)),
                       capture_output=True, timeout=30)
        self.wait_for(lambda: Path(self.tmp, "self-calls").exists()
                      and f"resume {w}" in Path(self.tmp, "self-calls").read_text(), "no resume")

    def test_6_an_argv_too_long_for_tmux_is_recorded_without_it(self):
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        Path(self.work, "long.sh").write_text(
            "exec python3 -c 'import time; time.sleep(1000)' " + "x" * 20000 + "\n")
        job = self.run_job(p, "bash long.sh")
        self.script("stash-many", w)
        self.assertEqual(self.session_of(w), "stash")
        frozen = json.loads(self.opt(w, "@stash_frozen"))
        self.assertEqual(frozen[0]["cmd"], "")
        self.assertEqual(frozen[0]["pgid"], job)
        self.assertIn("without their commands", self.log())
        self.script("unstash-many", w)
        self.wait_for(lambda: not self.state(job).startswith("T"), "warm fg did not work")

    def test_nit_background_jobs_are_frozen_too_and_continued(self):
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        self.t("send-keys", "-t", p, "-l", "sleep 4000 &")
        self.t("send-keys", "-t", p, "Enter")
        tty = self.t("display", "-p", "-t", p, "#{pane_tty}").replace("/dev/", "")

        def bgpid():
            out = subprocess.run(["ps", "-t", tty, "-o", "pid=,args="], capture_output=True, text=True).stdout
            return next((int(l.split()[0]) for l in out.splitlines() if l.split()[1:] == ["sleep", "4000"]), None)
        self.wait_for(lambda: bgpid(), "no background job")
        bg = bgpid()
        self.pids.append(bg)
        self.script("stash-many", w)
        self.assertTrue(self.state(bg).startswith("T"))
        frozen = json.loads(self.opt(w, "@stash_frozen"))
        self.assertEqual((frozen[0]["pgid"], frozen[0]["xpg"]), (0, [bg]))
        self.script("unstash-many", w)
        self.wait_for(lambda: not self.state(bg).startswith("T"), "background job not continued")

    def test_nit_a_busy_pane_keeps_its_record_and_synchronize_panes_is_respected(self):
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        self.t("split-window", "-d", "-t", w, "-c", str(self.work))
        other = self.pane(w, 1)
        self.wait_prompt(other)
        job = self.run_job(p, "sleep 1000")
        self.t("set", "-w", "-t", w, "synchronize-panes", "on")
        self.script("stash-many", w)
        self.assertEqual(self.t("show", "-wqv", "-t", w, "synchronize-panes"), "on")
        # Home by hand, and the pane busy with another job: thaw must wait.
        self.t("move-window", "-s", w, "-t", "main:")
        self.t("set", "-w", "-t", w, "synchronize-panes", "off")
        busy = self.run_job(p, "sleep 999")
        self.t("set", "-w", "-t", w, "synchronize-panes", "on")
        self.script("thaw", w)
        self.assertTrue(self.state(job).startswith("T"))
        self.assertNotEqual(self.opt(w, "@stash_frozen"), "")
        os.kill(busy, signal.SIGKILL)
        self.wait_for(lambda: self.tpgid(p) == self.pane_pid(p), "the shell never got its prompt back")
        self.script("thaw", w)
        self.wait_for(lambda: not self.state(job).startswith("T"), "not resumed once the pane was free")
        self.assertEqual(self.opt(w, "@stash_frozen"), "")
        self.assertEqual(self.t("show", "-wqv", "-t", w, "synchronize-panes"), "on")
        self.assertNotIn("fg", self.t("capture-pane", "-p", "-t", other))

    def test_nit_a_failed_freeze_goes_back_to_its_own_slot(self):
        self.keep_one()
        a = self.new_window()
        w = self.new_window()
        self.new_window()
        idx = self.t("display", "-p", "-t", w, "#{window_index}")
        out = self.source(f"freeze_window() {{ FREEZE_WHY='nope'; return 1; }}\ndo_stash_many '{w}'")
        self.assertEqual(self.session_of(w), "main", out)
        self.assertEqual(self.t("display", "-p", "-t", w, "#{window_index}"), idx)
        self.assertEqual(self.opt(w, "@stash_ts"), "")
        self.assertEqual(self.opt(w, "@stash_origin"), "")
        self.assertEqual(self.session_of(a), "main")

    # -- round 2 ---------------------------------------------------------

    def test_r2_1_fg_resumes_the_foreground_job_not_a_background_one(self):
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        tty = self.t("display", "-p", "-t", p, "#{pane_tty}").replace("/dev/", "")
        self.t("send-keys", "-t", p, "-l", "sleep 301 &")
        self.t("send-keys", "-t", p, "Enter")

        def bgpid():
            out = subprocess.run(["ps", "-t", tty, "-o", "pid=,args="], capture_output=True, text=True).stdout
            return next((int(l.split()[0]) for l in out.splitlines() if l.split()[1:] == ["sleep", "301"]), None)
        self.wait_for(bgpid, "no background job")
        bg = bgpid()
        self.pids.append(bg)
        fg = self.run_job(p, "sleep 302")
        self.script("stash-many", w)
        self.assertTrue(self.state(fg).startswith("T") and self.state(bg).startswith("T"))
        self.script("unstash-many", w)
        self.wait_for(lambda: self.tpgid(p) == fg and not self.state(fg).startswith("T"),
                      "the foreground job is not back in front")
        self.wait_for(lambda: not self.state(bg).startswith("T"), "the background job was not continued")

    def test_r2_2_a_stale_record_never_outlives_a_new_park(self):
        self.keep_one()
        w = self.new_window()
        self.t("set", "-w", "-t", w, "@stash_frozen", '[{"pane":0,"pgid":4242,"cwd":"/","cmd":"sleep 9","xpg":[]}]')
        self.t("set", "-w", "-t", w, "@stash_cold", "1")
        self.script("stash-many", w)
        self.assertEqual((self.opt(w, "@stash_frozen"), self.opt(w, "@stash_cold")), ("", ""))
        self.assertEqual(self.script("rows").split(SEP)[4], "idle")

    def test_r2_3_a_title_over_argv_with_no_spaces_is_command_unknown(self):
        self.keep_one()
        w = self.new_window()
        self.run_job(self.pane(w), """perl -e '$0 = "grunt"; sleep 1000' x y""")
        self.script("stash-many", w)
        self.assertEqual(json.loads(self.opt(w, "@stash_frozen"))[0]["cmd"], "")

    def test_r2_3_trusted_rejects_titles_written_over_argv(self):
        import contextlib, io
        code = self.source('printf "%s" "$FROZEN_PY"')
        ns = {}
        with contextlib.redirect_stdout(io.StringIO()):
            old, sys.stdin = sys.stdin, io.StringIO("")
            try:
                exec(code, ns)
            finally:
                sys.stdin = old
        trusted = ns["trusted"]
        self.assertFalse(trusted(["grunt", "", ""]))                       # libuv
        self.assertFalse(trusted(["grunt", "     ", "HOME=/x"]))           # perl
        self.assertFalse(trusted(["npm run dev", "", ""]))
        self.assertTrue(trusted(["sleep", "1000"]))
        self.assertTrue(trusted(["grunt"]))
        self.assertTrue(trusted(["printf", "%s", ""]))                     # one empty arg is real

    def test_r2_4_a_job_that_will_not_stop_is_left_running(self):
        self.keep_one()
        w = self.new_window()
        job = self.run_job(self.pane(w), "sleep 1000")
        out = self.source(f"pg_wait_stopped() {{ return 1; }}\nfreeze_window '{w}'; echo \"rc=$? $FREEZE_WHY\"")
        self.assertIn("rc=1 couldn't stop", out)
        self.wait_for(lambda: self.state(job) and not self.state(job).startswith("T"), "left stopped")

    def test_r2_5_a_move_back_with_no_park_time_thaws_cleanly(self):
        self.keep_one()
        w = self.new_window()
        _, err = self.source(
            "freeze_window() { tmux set-option -w -t \"$1\" @stash_frozen "
            "'[{\"pane\":0,\"pgid\":0,\"cwd\":\"\",\"cmd\":\"\",\"xpg\":[]}]'; FREEZE_WHY=nope; return 1; }\n"
            f"do_stash_many '{w}'", stderr=True)
        self.assertEqual(self.session_of(w), "main")
        self.assertNotIn("integer expected", err)
        self.assertEqual(self.opt(w, "@stash_frozen"), "")

    def test_r2_6_a_lock_timeout_in_thaw_is_said(self):
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        job = self.run_job(p, "sleep 1000")
        self.script("stash-many", w)
        self.t("move-window", "-s", w, "-t", "main:")
        said = Path(self.tmp, "said")
        rec = Path(self.tmp, "notify-rec")
        rec.write_text(f'#!/bin/sh\necho "$*" >> "{said}"\n')
        rec.chmod(0o755)
        self.source(f"type_home() {{ return 1; }}\ndo_thaw '{w}'", env={"NOTIFY_SH": str(rec)})
        self.assertIn("pane 0: busy — type fg there", said.read_text())
        self.assertTrue(self.state(job).startswith("T"))
        self.assertNotEqual(self.opt(w, "@stash_frozen"), "")

    def test_r2_7_fg_is_not_swallowed_by_a_continuation_prompt(self):
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        job = self.run_job(p, "sleep 1000")
        self.script("stash-many", w)
        self.t("move-window", "-s", w, "-t", "main:")
        self.t("send-keys", "-t", p, "-l", 'echo "open')
        self.t("send-keys", "-t", p, "Enter")                 # PS2: dquote>
        time.sleep(0.3)
        self.script("thaw", w)
        self.wait_for(lambda: self.tpgid(p) == job and not self.state(job).startswith("T"),
                      "fg joined the open quote instead of resuming the job")

    def test_r2_8_a_suspended_legacy_window_is_a_suspended_row(self):
        self.keep_one()
        w = self.new_window()
        self.park_by_hand(w)
        self.t("set", "-w", "-t", w, "@stash_session", "aaaa-bbbb")
        rows = {r.split(SEP)[0]: r.split(SEP) for r in self.script("rows").splitlines()}
        self.assertEqual(rows[w][4], "suspended")

    def park_by_hand(self, win):
        if subprocess.run([TMUX, "-L", self.sock, "has-session", "-t", "=stash"],
                          env=self.env, capture_output=True).returncode:
            boot = self.t("new-session", "-d", "-s", "stash", "-P", "-F", "#{window_id}")
            self.t("move-window", "-s", win, "-t", "stash:")
            self.t("kill-window", "-t", boot)
        else:
            self.t("move-window", "-s", win, "-t", "stash:")
        self.t("set", "-w", "-t", win, "@stash_origin", "main")

    def fake(self, name):
        return Refusals.fake(self, name)


class Counting(Base):
    def test_count_rows_and_publish_count_include_pinned_entries(self):
        hist = Path(self.env["XDG_STATE_HOME"], "tmux-closed-tabs", "history.jsonl")
        hist.parent.mkdir(parents=True)
        hist.write_text("\n".join([json.dumps({"id": "1", "pinned": True}), json.dumps({"id": "2"}),
                                   "{torn", json.dumps({"id": "3", "pinned": True, "stashed_ts": 5})]) + "\n")
        self.assertEqual(self.script("count"), "2")
        self.keep_one()
        w = self.new_window()
        self.script("stash-many", w)
        self.assertEqual(self.script("count"), "3")
        self.t("set", "-gu", "@stash_count")
        self.script("publish-count")
        self.assertEqual(self.t("show", "-gqv", "@stash_count"), "3")


class KillMany(Base):
    def test_kill_many_closes_through_closed_tabs_from_its_origin(self):
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        job = self.run_job(p, "sleep 1000")
        self.script("stash-many", w)
        self.assertTrue(self.state(job).startswith("T"))
        self.script("kill-many", w)
        self.assertEqual(self.session_of(w), "")
        # Closed from main (moved home first), so its ⌘Z entry is main's.
        # (The first call is the park's `close --pin` probe: no agent.)
        self.assertEqual(self.calls.read_text().splitlines()[-1].split(), ["close", p, "main"])
        self.wait_for(lambda: self.state(job) == "", "the frozen job outlived its tab")


if __name__ == "__main__":
    unittest.main()
