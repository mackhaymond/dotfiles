"""kill-guard: Touch ID plus a verified save before the real tmux server or main dies.

Pieces under test (docs/kill-guard.md):
  scripts/executable_kill-guard.sh   server mode (tmux.conf's kill-server alias)
                                     and cli mode (exec'd by the shim)
  kill-guard/tmux-shim.c             ~/.local/bin/tmux, compiled here per run
  ALIAS_LINE                         the exact tmux.conf.tmpl line, rendered here

SAFETY. Nothing here touches the user's real tmux server or pops a real Touch
ID prompt:
  * every server is `/opt/homebrew/bin/tmux -L unitB-kg-<pid>-<n>` started with
    a scratch config (the alias line, a sleep default-command), killed, waited
    for and its socket removed in cleanup;
  * protected paths are exercised on those test servers through
    KILL_GUARD_PROTECT_SOCKET, with KILL_GUARD_AUTH=approve|deny deciding and
    the save/publish steps stubbed (KILL_GUARD_SAVE_CMD / _PUBLISH_CMD);
  * HOME points at a temp dir, so even a path that reached for the Touch ID
    helper would find none (unavailable -> refused), never the installed one;
  * the "overrides are ignored on the real default socket" checks use the
    read-only auth-source / save-source modes (at most a stat() of the real
    socket file) and KILL_GUARD_DRYRUN with an ABBREVIATED kill-server, which
    the guard decides without asking any server.
"""
import itertools
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parents[1]
GUARD = HERE / "scripts" / "executable_kill-guard.sh"
SHIM_SRC = HERE / "kill-guard" / "tmux-shim.c"
CONF_TMPL = HERE / "tmux.conf.tmpl"
REAL = "/opt/homebrew/bin/tmux"
UID = os.getuid()
REAL_DEFAULT = f"/private/tmp/tmux-{UID}/default"
PREFIX = "unitB-kg"            # every test socket name starts with this
_counter = itertools.count()

# The exact line for tmux.conf.tmpl (docs/kill-guard.md). If the guard script
# is missing, a non-default server is still killed and the default refused.
GUARD_TMPL = "{{ .chezmoi.homeDir }}/.config/tmux/scripts/kill-guard.sh"
ALIAS_LINE = (
    "set -s 'command-alias[90]' \"kill-server=run-shell '"
    f"[ -x {GUARD_TMPL} ] && exec {GUARD_TMPL} server #{{q:socket_path}}; "
    "case #{q:socket_path} in */default) echo \\\"kill-guard: kill-guard.sh missing; refusing to kill "
    "the default server (bypass: {{ .homebrew_prefix }}/bin/tmux kill-ser)\\\"; exit 1;; esac; "
    "exec {{ .homebrew_prefix }}/bin/tmux -S #{q:socket_path} kill-ser'\"")


def render_alias(guard_path):
    return (ALIAS_LINE.replace(GUARD_TMPL, str(guard_path))
            .replace("{{ .homebrew_prefix }}", "/opt/homebrew"))


def _clean_env():
    return {k: v for k, v in os.environ.items()
            if not k.startswith("KILL_GUARD_") and k not in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR")}


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@unittest.skipUnless(os.path.exists(REAL) and shutil.which("clang"), "needs Homebrew tmux and clang")
class KillGuardBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(dir="/tmp")
        b = Path(cls.build.name).resolve()
        cls.guard_exec = b / "kill-guard.sh"          # executable copy, as installed
        shutil.copy(GUARD, cls.guard_exec)
        cls.guard_exec.chmod(0o755)
        cls.shim = b / "tmux"
        cls.shim_missing = b / "tmux-missing-guard"
        cls.shim_fake = b / "tmux-fake-guard"
        cls.fake_guard = b / "fake-guard.sh"
        cls.fake_marker = b / "fake-guard-ran"
        cls.fake_guard.write_text(f"touch '{cls.fake_marker}'\nexit 99\n")
        for out, guard in ((cls.shim, GUARD), (cls.shim_missing, b / "no-such-guard.sh"),
                           (cls.shim_fake, cls.fake_guard)):
            subprocess.run(["clang", "-O2", "-Wall", "-Werror",
                            f'-DREAL_TMUX="{REAL}"', f'-DKILL_GUARD="{guard}"',
                            "-o", str(out), str(SHIM_SRC)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.log = self.root / "guard.log"
        self.resdir = self.root / "resurrect"
        self.resdir.mkdir()

    # -- servers -----------------------------------------------------------
    def start_server(self, *, alias=True, guard=None, protect=True, auth="deny", sessions=("main",),
                     save_ok=True, stamp_last=True, publish_ok=True, save_sleep=0, extra_env=None,
                     drop_env=()):
        name = f"{PREFIX}-{os.getpid()}-{next(_counter)}"
        assert name.startswith("unitB-")
        sock = Path(f"/private/tmp/tmux-{UID}/{name}")
        conf = self.root / f"{name}.conf"
        # Panes run sleep, not a shell: no zsh writing $HOME/.zsh_history on
        # SIGHUP while the temp dir is being removed.
        lines = ["set -g default-command 'exec sleep 3600'",
                 f"set -g @resurrect-dir '{self.resdir}'"]
        if alias:
            lines.append(render_alias(guard or self.guard_exec))
        conf.write_text("\n".join(lines) + "\n")
        stub_publish = self.root / f"{name}-publish.sh"
        stub_save = self.root / f"{name}-save.sh"
        flag = f'$(/opt/homebrew/bin/tmux -L {name} show -gqv @kill_guard_shutdown)'
        stub_publish.write_text(
            f'echo "STUB publish flag={flag} lang=$LANG ctype=$LC_CTYPE tmux=$TMUX sock=$KILL_GUARD_SOCKET" >>"$KILL_GUARD_LOG"\n'
            f'exit {0 if publish_ok else 3}\n')
        stub_save.write_text(
            f'echo "STUB save flag={flag} pid=$$" >>"$KILL_GUARD_LOG"\n'
            + (f'sleep {save_sleep}\necho "STUB save end" >>"$KILL_GUARD_LOG"\n' if save_sleep else "")
            + (f'f="{self.resdir}/snap-$$.txt"; echo x >"$f"; ln -sfn "$(basename "$f")" "{self.resdir}/last"\n'
               if stamp_last else "")
            + (f'echo "[now] saved 3 panes in 0.1s" >>"{self.resdir}/save.log"\n' if stamp_last else "")
            + f'exit {0 if save_ok else 4}\n')
        env = _clean_env()
        env.update(HOME=str(self.home), KILL_GUARD_LOG=str(self.log),
                   KILL_GUARD_PUBLISH_CMD=f"/bin/bash {stub_publish}",
                   KILL_GUARD_SAVE_CMD=f"/bin/bash {stub_save}")
        if auth:
            env["KILL_GUARD_AUTH"] = auth
        if protect:
            env["KILL_GUARD_PROTECT_SOCKET"] = str(sock)
        env.update(extra_env or {})
        for k in drop_env:
            env.pop(k, None)
        first, *rest = sessions
        subprocess.run([REAL, "-L", name, "-f", str(conf), "new-session", "-d", "-s", first,
                        "-x", "80", "-y", "20"], env=env, check=True, capture_output=True)
        spid = int(self.real(name, "display", "-p", "#{pid}").stdout.strip())

        def cleanup():
            subprocess.run([REAL, "-L", name, "kill-ser"], env=_clean_env(), capture_output=True)
            end = time.time() + 5
            while _pid_alive(spid) and time.time() < end:
                time.sleep(0.05)
            sock.unlink(missing_ok=True)
        self.addCleanup(cleanup)
        for s in rest:
            subprocess.run([REAL, "-L", name, "new-session", "-d", "-s", s], env=env,
                           check=True, capture_output=True)
        return name, sock, env

    def real(self, name, *args, env=None):
        assert name.startswith("unitB-")
        return subprocess.run([REAL, "-L", name, *args], env=env or _clean_env(),
                              capture_output=True, text=True)

    def alive(self, name):
        return self.real(name, "display", "-p", "x").stdout.strip() == "x"

    def sessions(self, name):
        r = self.real(name, "ls", "-F", "#{session_name}")
        return sorted(r.stdout.split()) if r.returncode == 0 else []

    def flag(self, name):
        return self.real(name, "show", "-gqv", "@kill_guard_shutdown").stdout.strip()

    def pane_of(self, name, session):
        return self.real(name, "display", "-p", "-t", f"={session}:", "#{pane_id}").stdout.strip()

    def wait_dead(self, name, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if not self.alive(name):
                return True
            time.sleep(0.05)
        return False

    def shim_run(self, *args, env, shim=None):
        return subprocess.run([str(shim or self.shim), *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def log_lines(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def assert_order(self, patterns):
        lines = self.log_lines()
        idx = []
        for p in patterns:
            hits = [i for i, l in enumerate(lines) if re.search(p, l)]
            self.assertTrue(hits, (p, lines))
            idx.append(hits[0])
        self.assertEqual(idx, sorted(idx), lines)

    def guard(self, *args, env):
        return subprocess.run(["/bin/bash", str(GUARD), *args], env=env,
                              capture_output=True, text=True, timeout=30)


SAVE_ORDER = [r"step=auth-approved", r"step=flag-set", r"STUB publish flag=\d{9,} ",
              r"STUB save flag=\d{9,} ", r"step=save-verified"]


class ServerAlias(KillGuardBase):
    """kill-server -> command-alias -> run-shell kill-guard.sh server."""

    def test_approve_saves_then_kills_in_one_invocation(self):
        name, sock, env = self.start_server(auth="approve")
        r = self.real(name, "kill-server", env=env)
        self.assertEqual(r.returncode, 0, r)
        self.assertTrue(self.wait_dead(name))
        lines = self.log_lines()
        guard_server = [l for l in lines if "mode=server" in l]
        pids = {w for l in guard_server for w in l.split() if w.startswith("pid=")}
        self.assertEqual(len(pids), 1, lines)              # one guard invocation
        self.assertFalse(any("unprotected-kill" in l for l in lines), lines)
        self.assertEqual(sum("decision=approved-kill" in l for l in lines), 1, lines)
        self.assert_order(SAVE_ORDER + [r"decision=approved-kill"])
        # The requester is the CLI process, never an attached client's tty.
        self.assertIn(f"requester={REAL} -L {name} kill-server [", lines[0])

    def test_deny_keeps_server_and_fails(self):
        name, sock, env = self.start_server(auth="deny")
        r = self.real(name, "kill-server", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refused", r.stdout + r.stderr)
        self.assertTrue(self.alive(name))
        self.assertEqual(self.flag(name), "")
        self.assertFalse(any("STUB" in l for l in self.log_lines()))

    def test_save_failure_refuses_and_unsets_flag(self):
        name, sock, env = self.start_server(auth="approve", save_ok=False)
        r = self.real(name, "kill-server", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("could not save first", r.stdout + r.stderr)
        self.assertTrue(self.alive(name))
        self.assertEqual(self.flag(name), "")
        self.assertTrue(any("refused-save-failed" in l for l in self.log_lines()))

    def test_publish_failure_refuses(self):
        name, sock, env = self.start_server(auth="approve", publish_ok=False)
        r = self.real(name, "kill-server", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(self.alive(name))
        self.assertEqual(self.flag(name), "")
        self.assertFalse(any("STUB save" in l for l in self.log_lines()))

    def test_save_claiming_success_without_snapshot_refuses(self):
        name, sock, env = self.start_server(auth="approve", stamp_last=False)
        r = self.real(name, "kill-server", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(self.alive(name))
        self.assertEqual(self.flag(name), "")

    def test_children_get_utf8_locale(self):
        name, sock, env = self.start_server(auth="approve", extra_env={"LANG": "C"},
                                            drop_env=("LC_ALL", "LC_CTYPE"))
        self.real(name, "kill-server", env=env)
        line = next(l for l in self.log_lines() if "STUB publish" in l)
        self.assertIn("lang=en_US.UTF-8 ctype=en_US.UTF-8", line)

    def test_unprotected_server_killed_without_auth_or_save(self):
        # auth=deny would refuse if consulted; an unprotected socket never asks.
        name, sock, env = self.start_server(protect=False, auth="deny")
        r = self.real(name, "kill-server", env=env)
        self.assertEqual(r.returncode, 0, r)
        self.assertTrue(self.wait_dead(name))
        lines = self.log_lines()
        self.assertTrue(any("decision=unprotected-kill" in l for l in lines), lines)
        self.assertFalse(any("STUB" in l or "auth" in l for l in lines), lines)

    def test_missing_guard_still_kills_non_default_server(self):
        name, sock, env = self.start_server(guard=self.root / "no-such-guard.sh", auth="deny")
        r = self.real(name, "kill-server", env=env)
        self.assertEqual(r.returncode, 0, r)
        self.assertTrue(self.wait_dead(name))

    def test_missing_guard_refuses_default_socket(self):
        """The alias's fallback, run by sh with a fake */default path: the case
        branch refuses before any tmux runs (and a tmux reached by mistake
        would find no server at that path)."""
        cmd = render_alias(self.root / "no-such-guard.sh")
        cmd = cmd.split("run-shell '", 1)[1].rsplit("'", 1)[0].replace('\\"', '"')
        cmd = cmd.replace("#{q:socket_path}", "/private/tmp/unitB-kg-nonexistent/default")
        r = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 1, r)
        self.assertIn("refusing to kill the default server", r.stdout)

    def test_private_tmpdir_default_server_dies_freely(self):
        """An agent's private server (TMUX_TMPDIR=<scratch>, socket named
        `default`, real tmux.conf with the alias): kill-server through the
        alias kills at once, with no auth, flag, publish or save."""
        sock, env = self.start_private(("main",))
        r = subprocess.run([REAL, "-S", str(sock), "kill-server"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r)
        self.assertFalse(self.private_alive(sock))
        lines = self.log_lines()
        self.assertTrue(any("decision=unprotected-kill" in l for l in lines), lines)
        self.assertFalse(any(re.search(r"auth|STUB|flag|save", l) for l in lines), lines)

    def test_private_tmpdir_kill_session_main_through_shim_passes(self):
        sock, env = self.start_private(("main", "other"))
        # No -L/-S/TMUX: the shim's guard must resolve TMUX_TMPDIR's default.
        self.assertTrue(env["TMUX_TMPDIR"].startswith(str(self.root)) and "TMUX" not in env)
        r = self.shim_run("kill-session", "-t", "main", env=env)
        self.assertEqual(r.returncode, 0, r)
        out = subprocess.run([REAL, "-S", str(sock), "ls", "-F", "#{session_name}"], env=env,
                             capture_output=True, text=True).stdout.split()
        self.assertEqual(out, ["other"])
        self.assertFalse(any(re.search(r"auth|STUB|flag|refused", l) for l in self.log_lines()))

    def start_private(self, sessions):
        tmpdir = self.root / "t"
        sockdir = tmpdir / f"tmux-{UID}"
        sockdir.mkdir(parents=True, mode=0o700)
        sock = sockdir / "default"
        assert str(sock).startswith(str(self.root)) and len(str(sock)) < 100
        conf = self.root / "private.conf"
        conf.write_text("set -g default-command 'exec sleep 3600'\n" + render_alias(self.guard_exec) + "\n")
        stub = f'/bin/bash -c \'echo STUB >>"$KILL_GUARD_LOG"\''
        env = dict(_clean_env(), HOME=str(self.home), KILL_GUARD_LOG=str(self.log), KILL_GUARD_AUTH="deny",
                   KILL_GUARD_SAVE_CMD=stub, KILL_GUARD_PUBLISH_CMD=stub, TMUX_TMPDIR=str(tmpdir))
        first, *rest = sessions
        subprocess.run([REAL, "-S", str(sock), "-f", str(conf), "new-session", "-d", "-s", first],
                       env=env, check=True, capture_output=True)
        spid = int(subprocess.run([REAL, "-S", str(sock), "display", "-p", "#{pid}"], env=env,
                                  capture_output=True, text=True).stdout.strip())

        def cleanup():
            subprocess.run([REAL, "-S", str(sock), "kill-ser"], env=env, capture_output=True)
            end = time.time() + 5
            while _pid_alive(spid) and time.time() < end:
                time.sleep(0.05)
            sock.unlink(missing_ok=True)
        self.addCleanup(cleanup)
        for s in rest:
            subprocess.run([REAL, "-S", str(sock), "new-session", "-d", "-s", s], env=env,
                           check=True, capture_output=True)
        return sock, env

    def private_alive(self, sock):
        end = time.time() + 5
        while time.time() < end:
            r = subprocess.run([REAL, "-S", str(sock), "display", "-p", "x"], env=_clean_env(),
                               capture_output=True, text=True)
            if r.stdout.strip() != "x":
                return False
            time.sleep(0.05)
        return True

    def test_chained_and_source_file_kill_server_hit_alias(self):
        name, sock, env = self.start_server(auth="deny")
        r = self.real(name, "display", "-p", "x", ";", "kill-server", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(self.alive(name))
        f = self.root / "k.conf"
        f.write_text("kill-server\n")
        self.real(name, "source-file", str(f), env=env)
        self.assertTrue(self.alive(name))

    def test_last_pane_exit_still_ends_server(self):
        """exit-empty is untouched: no command runs, so no alias, no guard."""
        name, sock, env = self.start_server(auth="deny")
        pid = int(self.real(name, "display", "-p", "-t", "main", "#{pane_pid}").stdout)
        os.kill(pid, signal.SIGTERM)
        self.assertTrue(self.wait_dead(name))
        self.assertEqual(self.log_lines(), [])

    def test_kill_sess_of_last_session_still_ends_server(self):
        name, sock, env = self.start_server(auth="deny")
        r = self.real(name, "kill-sess", "-t", "main", env=env)    # real tmux, abbreviation
        self.assertEqual(r.returncode, 0, r)
        self.assertTrue(self.wait_dead(name))
        self.assertEqual(self.log_lines(), [])

    def test_tmux_conf_line_matches_docs(self):
        self.assertIn(ALIAS_LINE, (HERE / "docs" / "kill-guard.md").read_text())
        text = CONF_TMPL.read_text()
        if "kill-guard.sh" not in text:
            self.skipTest("kill-guard alias not in tmux.conf.tmpl yet")
        self.assertIn(ALIAS_LINE, text)


class Shim(KillGuardBase):
    def test_fast_path_never_runs_guard(self):
        name, sock, env = self.start_server(protect=False)
        r = self.shim_run("-L", name, "display", "-p", "#{session_name}", env=env, shim=self.shim_fake)
        self.assertEqual(r.stdout.strip(), "main")
        self.assertFalse(self.fake_marker.exists())
        r = self.shim_run("-L", name, "display", "-p", "kill-session", env=env, shim=self.shim_fake)
        self.assertEqual(r.returncode, 99)                    # slow path went to the guard
        self.assertTrue(self.fake_marker.exists())
        self.fake_marker.unlink()

    def test_missing_guard_fails_open(self):
        name, sock, env = self.start_server(sessions=("main", "other"))
        r = self.shim_run("-L", name, "kill-session", "-t", "main", env=env, shim=self.shim_missing)
        self.assertEqual(r.returncode, 0, r)
        self.assertIn("UNGUARDED", r.stderr)
        self.assertEqual(self.sessions(name), ["other"])

    def test_kill_session_protected_deny(self):
        name, sock, env = self.start_server(sessions=("main", "other"))
        for t in ("main", "=main", "main:", "ma"):
            r = self.shim_run("-L", name, "kill-session", "-t", t, env=env)
            self.assertEqual(r.returncode, 1, (t, r))
            self.assertIn("refused", r.stderr)
        self.assertEqual(self.sessions(name), ["main", "other"])

    def test_window_named_like_protected_session(self):
        """`-t main` from inside a session that has a WINDOW called main is
        still the SESSION main (kill-session's lookup), so it is protected."""
        name, sock, env = self.start_server(sessions=("main", "other"))
        self.real(name, "rename-window", "-t", "=other:", "main")
        e = dict(env, TMUX=f"{sock},1,1", TMUX_PANE=self.pane_of(name, "other"))
        r = self.shim_run("kill-session", "-t", "main", env=e)
        self.assertEqual(r.returncode, 1, r)
        self.assertEqual(self.sessions(name), ["main", "other"])

    def test_target_prefix_of_window_in_main_is_not_main(self):
        """`-t c` from inside main, where main has a window "claude": session
        c, not main. No spurious prompt."""
        name, sock, env = self.start_server(sessions=("main", "c", "other"))
        self.real(name, "rename-window", "-t", "=main:", "claude")
        e = dict(env, TMUX=f"{sock},1,0", TMUX_PANE=self.pane_of(name, "main"))
        r = self.shim_run("kill-session", "-t", "c", env=e)
        self.assertEqual(r.returncode, 0, r)
        self.assertEqual(self.sessions(name), ["main", "other"])

    def test_dryrun_never_executes(self):
        """KILL_GUARD_DRYRUN prints a verdict and runs nothing: unprotected
        server, unprotected session on a protected server, abbreviation,
        and the alias's server mode."""
        name, sock, env = self.start_server(protect=False, sessions=("main", "other"),
                                            extra_env={"KILL_GUARD_DRYRUN": "1"})
        for args in (("kill-session", "-t", "other"), ("kill-ser",), ("kill-session", "-a"),
                     ("display", "-p", "x", ";", "kill-session", "-t", "main")):
            r = self.shim_run("-L", name, *args, env=env)
            self.assertEqual(r.returncode, 0, (args, r))
            self.assertIn("verdict=passthrough", r.stdout, args)
        r = self.real(name, "kill-server", env=env)          # alias -> server mode, dry
        self.assertIn("verdict=passthrough", r.stdout)
        self.assertTrue(self.alive(name))
        self.assertEqual(self.sessions(name), ["main", "other"])

        name2, sock2, env2 = self.start_server(sessions=("main", "scratch"),
                                               extra_env={"KILL_GUARD_DRYRUN": "1"})
        r = self.shim_run("-L", name2, "kill-session", "-t", "scratch", env=env2)
        self.assertEqual(r.returncode, 0, r)
        self.assertIn("verdict=passthrough", r.stdout)
        r = self.shim_run("-L", name2, "kill-session", "-t", "main", env=env2)
        self.assertIn("verdict=protect", r.stdout)
        self.assertEqual(self.sessions(name2), ["main", "scratch"])
        self.assertFalse(any("STUB" in l or "flag" in l for l in self.log_lines()))

    def test_kill_session_protected_approve_saves_first(self):
        name, sock, env = self.start_server(auth="approve", sessions=("main", "other"))
        r = self.shim_run("-L", name, "kill-session", "-t", "main", env=env)
        self.assertEqual(r.returncode, 0, r)
        self.assertEqual(self.sessions(name), ["other"])
        self.assertEqual(self.flag(name), "")                 # unset again: server alive
        self.assert_order(SAVE_ORDER + [r"decision=approved", r"step=flag-unset"])

    def test_symlinked_socket_children_get_canonical_path(self):
        """`-S <symlink>` matched by inode: the save/publish children are
        handed the canonical socket path, which the scripts' own
        default-server checks compare by path."""
        name, sock, env = self.start_server(auth="approve", sessions=("main", "other"))
        link = self.root / "link"
        link.symlink_to(sock)
        r = self.shim_run("-S", str(link), "kill-session", "-t", "main", env=env)
        self.assertEqual(r.returncode, 0, r)
        self.assertEqual(self.sessions(name), ["other"])
        line = next(l for l in self.log_lines() if "STUB publish" in l)
        self.assertIn(f"tmux={sock} sock={sock}", line)

    @unittest.skipUnless(os.path.exists("/opt/homebrew/bin/bash"), "needs Homebrew bash")
    def test_save_scripts_run_with_bash4_despite_thin_path(self):
        """No stubs: the real script slots (in the temp HOME) run under bash
        >= 4 even with a 3.2 shebang and a PATH without Homebrew."""
        name, sock, env = self.start_server(auth="approve", sessions=("main", "other"))
        scripts = self.home / ".config/tmux/scripts"
        scripts.mkdir(parents=True)
        (scripts / "stash.sh").write_text(
            '#!/bin/bash\ndeclare -A x=([a]=1)\n'
            'echo "FAKE stash $1 bash=${BASH_VERSINFO[0]} x=${x[a]} tmux=$TMUX" >>"$KILL_GUARD_LOG"\n')
        (scripts / "resurrect-save.sh").write_text(
            '#!/bin/bash\ndeclare -A x=([a]=1)\n'
            'echo "FAKE save $1 bash=${BASH_VERSINFO[0]} x=${x[a]}" >>"$KILL_GUARD_LOG"\n'
            f'f="{self.resdir}/snap-$$.txt"; echo x >"$f"; ln -sfn "$(basename "$f")" "{self.resdir}/last"\n'
            f'echo "[now] saved 2 panes" >>"{self.resdir}/save.log"\n')
        for f in scripts.iterdir():
            f.chmod(0o755)
        e = {k: v for k, v in env.items() if k not in ("KILL_GUARD_SAVE_CMD", "KILL_GUARD_PUBLISH_CMD")}
        e["PATH"] = "/usr/bin:/bin"
        r = self.shim_run("-L", name, "kill-session", "-t", "main", env=e)
        self.assertEqual(r.returncode, 0, (r, self.log_lines()))
        self.assertEqual(self.sessions(name), ["other"])
        lines = self.log_lines()
        stash = next(l for l in lines if l.startswith("FAKE stash"))
        save = next(l for l in lines if l.startswith("FAKE save"))
        self.assertRegex(stash, r"^FAKE stash publish bash=([4-9]|\d\d) x=1 ")
        self.assertRegex(save, r"^FAKE save final bash=([4-9]|\d\d) x=1$")

    def test_kill_session_save_failure_refuses(self):
        name, sock, env = self.start_server(auth="approve", save_ok=False, sessions=("main", "other"))
        r = self.shim_run("-L", name, "kill-session", "-t", "main", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.sessions(name), ["main", "other"])
        self.assertEqual(self.flag(name), "")

    def test_signal_during_save_unsets_flag_and_kills_save_group(self):
        name, sock, env = self.start_server(auth="approve", save_sleep=30, sessions=("main", "other"))
        p = subprocess.Popen([str(self.shim), "-L", name, "kill-session", "-t", "main"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            end = time.time() + 10
            stub_pid = None
            while time.time() < end and stub_pid is None:
                for l in self.log_lines():
                    m = re.search(r"STUB save flag=\d+ pid=(\d+)", l)
                    if m:
                        stub_pid = int(m.group(1))
                time.sleep(0.05)
            self.assertIsNotNone(stub_pid, self.log_lines())
            self.assertRegex(self.flag(name), r"^\d{9,}$")      # set, as an epoch
            os.kill(p.pid, signal.SIGTERM)                      # the guard (shim exec'd bash)
            self.assertEqual(p.wait(timeout=5), 1)
        finally:
            if p.poll() is None:
                p.kill()
            p.communicate()
        self.assertEqual(self.flag(name), "")
        self.assertEqual(self.sessions(name), ["main", "other"])
        end = time.time() + 3
        while _pid_alive(stub_pid) and time.time() < end:
            time.sleep(0.05)
        self.assertFalse(_pid_alive(stub_pid))
        self.assertFalse(any("STUB save end" in l for l in self.log_lines()))
        self.assertTrue(any("aborted by signal" in l for l in self.log_lines()))

    def test_kill_last_session_prompts(self):
        name, sock, env = self.start_server(sessions=("solo",))
        r = self.shim_run("-L", name, "kill-ses", "-t", "solo", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("last session", r.stderr)
        self.assertTrue(self.alive(name))

    def test_kill_session_all_prompts(self):
        name, sock, env = self.start_server(sessions=("a", "b", "c"))
        r = self.shim_run("-L", name, "kill-session", "-a", "-t", "a", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.sessions(name), ["a", "b", "c"])

    def test_kill_session_clear_alerts_passes(self):
        name, sock, env = self.start_server(sessions=("main", "b"))
        r = self.shim_run("-L", name, "kill-session", "-C", "-t", "main", env=env)
        self.assertEqual(r.returncode, 0, r)
        self.assertEqual(self.sessions(name), ["b", "main"])

    def test_unprotected_session_passes_without_auth(self):
        name, sock, env = self.start_server(auth="deny", sessions=("main", "scratch"))
        r = self.shim_run("-L", name, "kill-session", "-t", "scratch", env=env)
        self.assertEqual(r.returncode, 0, r)
        self.assertEqual(self.sessions(name), ["main"])
        self.assertFalse(any("auth" in l or "STUB" in l for l in self.log_lines()))

    def test_chain_where_second_kill_empties_server_prompts(self):
        name, sock, env = self.start_server(sessions=("a", "b"))
        r = self.shim_run("-L", name, "kill-session", "-t", "a", ";", "kill-session", "-t", "b", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.sessions(name), ["a", "b"])

    def test_protect_option(self):
        # "*" must stay a literal word, not glob against the cwd.
        name, sock, env = self.start_server(sessions=("main", "work", "x"))
        self.real(name, "set", "-s", "@kill-guard-protect", "work *")
        self.assertEqual(self.shim_run("-L", name, "kill-session", "-t", "work", env=env).returncode, 1)
        self.assertEqual(self.shim_run("-L", name, "kill-session", "-t", "main", env=env).returncode, 0)
        self.assertEqual(self.sessions(name), ["work", "x"])

    def test_dash_S_TMUX_env_and_symlink_resolution(self):
        name, sock, env = self.start_server(sessions=("main", "other"))
        r = self.shim_run("-S", str(sock), "kill-session", "-t", "main", env=env)
        self.assertEqual(r.returncode, 1)
        link = self.root / "link"
        link.symlink_to(sock)
        r = self.shim_run("-S", str(link), "kill-session", "-t", "main", env=env)
        self.assertEqual(r.returncode, 1, r)                  # same inode -> protected
        e = dict(env, TMUX=f"{sock},1,0", TMUX_PANE=self.pane_of(name, "main"))
        r = self.shim_run("kill-session", env=e)              # no -L/-S, no -t: current = main
        self.assertEqual(r.returncode, 1, r)
        self.assertIn('"main"', r.stderr)
        self.assertEqual(self.sessions(name), ["main", "other"])

    def test_chained_trailing_semicolon(self):
        name, sock, env = self.start_server(sessions=("main", "other"))
        r = self.shim_run("-L", name, "new-session", "-d", "-s", "x;", "kill-session", "-t", "main", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.sessions(name), ["main", "other"])   # nothing in the chain ran

    def test_abbreviated_kill_server_prompts_once_in_shim(self):
        name, sock, env = self.start_server(auth="deny")
        for abbrev in ("kill-ser", "kill-serv", "kill-serve"):
            r = self.shim_run("-L", name, abbrev, env=env)
            self.assertEqual(r.returncode, 1, (abbrev, r))
            self.assertTrue(self.alive(name))
        self.assertFalse(any("mode=server" in l for l in self.log_lines()))

    def test_abbreviated_kill_server_without_server_no_prompt(self):
        name = f"{PREFIX}-{os.getpid()}-dead"
        env = dict(_clean_env(), HOME=str(self.home), KILL_GUARD_LOG=str(self.log), KILL_GUARD_AUTH="deny",
                   KILL_GUARD_PROTECT_SOCKET=f"/private/tmp/tmux-{UID}/{name}")
        r = self.shim_run("-L", name, "kill-ser", env=env)
        self.assertNotIn("refused", r.stderr)
        self.assertTrue(any("decision=pass" in l for l in self.log_lines()), self.log_lines())

    def test_abbreviated_kill_server_approve_no_double_prompt(self):
        name, sock, env = self.start_server(auth="approve")
        r = self.shim_run("-L", name, "kill-ser", env=env)
        self.assertEqual(r.returncode, 0, r)
        self.assertTrue(self.wait_dead(name))
        lines = self.log_lines()
        self.assertEqual(sum("step=auth-approved" in l for l in lines), 1, lines)
        self.assertFalse(any("mode=server" in l for l in lines), lines)

    def test_full_kill_server_left_to_alias_no_double_prompt(self):
        name, sock, env = self.start_server(auth="approve")
        r = self.shim_run("-L", name, "kill-server", env=env)
        self.assertEqual(r.returncode, 0, r)
        self.assertTrue(self.wait_dead(name))
        lines = self.log_lines()
        auths = [l for l in lines if "step=auth-approved" in l]
        self.assertEqual(len(auths), 1, lines)
        self.assertIn("mode=server", auths[0])

    def test_full_kill_server_without_alias_guarded_by_shim(self):
        name, sock, env = self.start_server(alias=False, auth="deny")
        r = self.shim_run("-L", name, "kill-server", env=env)
        self.assertEqual(r.returncode, 1, r)
        self.assertIn("no kill-guard alias", r.stderr)
        self.assertTrue(self.alive(name))


class Decisions(KillGuardBase):
    """Server-free: verdicts and authorities, incl. the real default socket."""

    def dry(self, *args, **extra):
        env = _clean_env()
        env.update(HOME=str(self.home), KILL_GUARD_LOG=str(self.log), KILL_GUARD_DRYRUN="1")
        env.update(extra)
        return self.guard("cli", *args, env=env).stdout.strip()

    def test_auth_override_ignored_for_real_default(self):
        env = dict(_clean_env(), HOME=str(self.home), KILL_GUARD_AUTH="approve",
                   KILL_GUARD_SAVE_CMD="true", KILL_GUARD_PUBLISH_CMD="true")
        for sock in (f"/tmp/tmux-{UID}/default", REAL_DEFAULT):
            self.assertEqual(self.guard("auth-source", sock, env=env).stdout.strip(), "touchid")
            self.assertEqual(self.guard("save-source", sock, env=env).stdout.split(),
                             ["save=scripts", "publish=scripts"])
        # TMUX_TMPDIR elsewhere must not make /tmp/tmux-UID/default look like a test server.
        e = dict(env, TMUX_TMPDIR=str(self.root))
        self.assertEqual(self.guard("auth-source", f"/tmp/tmux-{UID}/default", env=e).stdout.strip(), "touchid")
        self.assertEqual(self.guard("auth-source", f"/tmp/tmux-{UID}/{PREFIX}-x", env=env).stdout.strip(),
                         "override:approve")
        self.assertEqual(self.guard("auth-source", f"/tmp/tmux-{UID}/default",
                                    env=dict(env, KILL_GUARD_AUTH="deny")).stdout.strip(), "override:deny")

    def test_private_tmpdir_default_is_not_real_default(self):
        """A `default` socket under a private TMUX_TMPDIR is a spare server."""
        tmpdir = self.root / "tt"
        (tmpdir / f"tmux-{UID}").mkdir(parents=True)
        priv = str(tmpdir / f"tmux-{UID}" / "default")
        env = dict(_clean_env(), HOME=str(self.home), KILL_GUARD_AUTH="approve",
                   KILL_GUARD_SAVE_CMD="true", TMUX_TMPDIR=str(tmpdir), KILL_GUARD_LOG=str(self.log))
        self.assertEqual(self.guard("auth-source", priv, env=env).stdout.strip(), "override:approve")
        self.assertEqual(self.guard("save-source", priv, env=env).stdout.split()[0], "save=stub")
        out = self.guard("server", priv, env=dict(env, KILL_GUARD_DRYRUN="1")).stdout
        self.assertIn("verdict=passthrough", out)
        # Bare `tmux kill-ser` under that TMUX_TMPDIR resolves to the private socket and passes.
        out = self.dry("kill-ser", TMUX_TMPDIR=str(tmpdir))
        self.assertIn("verdict=passthrough", out)
        self.assertIn(f"socket={priv}", out)

    @unittest.skipUnless(os.path.exists(REAL_DEFAULT), "no real default socket file to link to")
    def test_symlink_to_real_default_is_real_default(self):
        link = self.root / "s"
        link.symlink_to(REAL_DEFAULT)                     # only stat()ed, never connected to
        env = dict(_clean_env(), HOME=str(self.home), KILL_GUARD_AUTH="approve",
                   KILL_GUARD_SAVE_CMD="true", KILL_GUARD_PROTECT_SOCKET=str(link))
        self.assertEqual(self.guard("auth-source", str(link), env=env).stdout.strip(), "touchid")
        self.assertEqual(self.guard("save-source", str(link), env=env).stdout.split()[0], "save=scripts")
        out = self.dry("-S", str(link), "kill-ser", KILL_GUARD_AUTH="approve")
        self.assertIn("real_default=1", out)
        self.assertIn("auth=touchid", out)

    def test_dryrun_real_default_full_flow_uses_touchid(self):
        out = self.dry("-S", f"/tmp/tmux-{UID}/default", "kill-ser", KILL_GUARD_AUTH="approve")
        self.assertIn("verdict=protect", out)
        self.assertIn("real_default=1", out)
        self.assertIn("auth=touchid", out)
        env = dict(_clean_env(), HOME=str(self.home), KILL_GUARD_DRYRUN="1", KILL_GUARD_AUTH="approve",
                   KILL_GUARD_LOG=str(self.log))
        out = self.guard("server", f"/tmp/tmux-{UID}/default", env=env).stdout
        self.assertIn("auth=touchid", out)
        # Bare `tmux kill-ser` with no TMUX, and with a TMUX tmux itself ignores
        # (empty socket part): both are the default socket.
        self.assertIn("real_default=1", self.dry("kill-ser"))
        self.assertIn("real_default=1", self.dry("kill-ser", TMUX=",123,0"))

    def test_socket_resolution(self):
        p = f"/private/tmp/tmux-{UID}/{PREFIX}-res"
        prot = {"KILL_GUARD_PROTECT_SOCKET": p}
        self.assertIn("verdict=protect", self.dry("-L", f"{PREFIX}-res", "kill-ser", **prot))
        self.assertIn("verdict=protect", self.dry(f"-L{PREFIX}-res", "kill-ser", **prot))
        self.assertIn("verdict=protect", self.dry("-u2L", f"{PREFIX}-res", "kill-ser", **prot))
        self.assertIn("verdict=protect", self.dry("-S", f"/tmp/tmux-{UID}/{PREFIX}-res", "kill-ser", **prot))
        self.assertIn("verdict=protect", self.dry("kill-ser", TMUX=f"{p},1,2", **prot))
        self.assertIn("verdict=passthrough", self.dry("-L", "other", "kill-ser", **prot))
        self.assertIn("verdict=passthrough", self.dry("-c", "kill-ser", **prot))
        # -L under TMUX_TMPDIR
        alt = self.root / "tt"
        (alt / f"tmux-{UID}").mkdir(parents=True)
        q = str(alt / f"tmux-{UID}" / "x")
        self.assertIn("verdict=protect", self.dry("-L", "x", "kill-ser", TMUX_TMPDIR=str(alt),
                                                  KILL_GUARD_PROTECT_SOCKET=q))
        self.assertIn("verdict=passthrough", self.dry("-L", "x", "kill-ser", KILL_GUARD_PROTECT_SOCKET=q))

    def test_command_splitting(self):
        prot = {"KILL_GUARD_PROTECT_SOCKET": f"/private/tmp/tmux-{UID}/{PREFIX}-s"}
        L = ("-L", f"{PREFIX}-s")
        self.assertIn("verdict=protect", self.dry(*L, "display", "-p", "a", ";", "kill-ser", **prot))
        self.assertIn("verdict=protect", self.dry(*L, "display", "-p", "a;", "kill-serve", **prot))
        self.assertIn("verdict=passthrough", self.dry(*L, "display", "-p", "a\\;", "kill-ser", **prot))
        self.assertIn("verdict=passthrough", self.dry(*L, "send-keys", "tmux kill-ser", **prot))
        self.assertIn("verdict=passthrough", self.dry(*L, "kill-s", **prot))       # ambiguous
        self.assertIn("verdict=passthrough", self.dry(*L, "kill-serverx", **prot))


if __name__ == "__main__":
    unittest.main()
