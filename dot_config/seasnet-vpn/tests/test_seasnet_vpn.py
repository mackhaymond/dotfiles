"""Isolated lifecycle regressions; never connect VPN, launch GUI, or change routes."""
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


sys.dont_write_bytecode = True
source = Path(__file__).resolve().parents[3] / "dot_local/bin/private_executable_seasnet-vpn"
if not source.exists():
    source = Path.home() / ".local/bin/seasnet-vpn"
loader = importlib.machinery.SourceFileLoader("seasnet_helper", str(source))
spec = importlib.util.spec_from_loader(loader.name, loader)
vpn = importlib.util.module_from_spec(spec)
loader.exec_module(vpn)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        for name, value in {"STATE": self.state, "PIDFILE": self.state / "openconnect.pid",
                            "BASELINE": self.state / "network-before.json", "PORT": 0}.items():
            context = patch.object(vpn, name, value)
            context.start()
            self.addCleanup(context.stop)
        # Never depend on whether the test runner has a controlling terminal.
        for context in (patch.object(vpn, "has_tty", return_value=True),
                        patch.object(vpn, "to_terminal", lambda: redirect_stdout(sys.stderr))):
            context.start()
            self.addCleanup(context.stop)

    def request(self, identity="current", attempt=None):
        vpn.write_json(self.state / "login-request.json",
                       {"id": identity, "url": "https://shb.ais.ucla.edu/login", "attempt": attempt})

    def auth_context(self, auth, owned=None):
        contexts = [patch.object(vpn, "snapshot", return_value={}),
                    patch.object(vpn, "has_tty", return_value=True),
                    patch.object(vpn, "agent_context", return_value=False),
                    patch.object(vpn, "resume_sync"),
                    patch.object(vpn, "owned_pid", owned or Mock(return_value=None)),
                    patch.object(vpn.subprocess, "Popen", return_value=auth),
                    patch.object(vpn, "stop", return_value=0)]
        results = [context.start() for context in contexts]
        for context in contexts:
            self.addCleanup(context.stop)
        return results[-1]

    def test_atomic_state_and_separate_cancellation(self):
        self.request()
        vpn.write_json(self.state / "login-cancel.json", {"id": "old", "result": "cancelled"})
        self.assertFalse(vpn.login_cancelled())
        vpn.write_json(self.state / "login-cancel.json", {"id": "current", "result": "cancelled"})
        vpn.write_login_result("connected")
        self.assertTrue(vpn.login_cancelled())
        self.assertEqual((self.state / "login-request.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.state.glob(".*.tmp")))

    def test_late_result_does_not_overwrite_new_request(self):
        self.request("new", "new-attempt")
        vpn.write_login_result("failed", request_id="old")
        vpn.write_login_result("failed", attempt="old-attempt")
        self.assertFalse((self.state / "login-result.json").exists())

    def test_build_failure_reports_cancellation_without_launch(self):
        with patch.object(vpn, "build_login", side_effect=OSError("compiler unavailable")), \
                patch.object(vpn.subprocess, "run") as run:
            self.assertEqual(vpn.browser_login("https://shb.ais.ucla.edu/login"), 1)
            self.assertTrue(vpn.login_cancelled())
            run.assert_not_called()

    def test_driver_failure_reports_cancellation(self):
        with patch.object(vpn, "build_login"), patch.object(vpn.subprocess, "run",
                side_effect=subprocess.CalledProcessError(1, "cua-driver")) as run:
            self.assertEqual(vpn.browser_login("https://shb.ais.ucla.edu/login"), 1)
            self.assertTrue(vpn.login_cancelled())
            self.assertEqual(run.call_count, 1)

    def test_expired_callback_never_builds_or_launches(self):
        vpn.write_json(self.state / "login-attempt.json", {"id": "new"})
        with patch.dict(os.environ, {"SEASNET_VPN_LOGIN_ATTEMPT": "old"}), \
                patch.object(vpn, "build_login") as build:
            self.assertEqual(vpn.browser_login("https://shb.ais.ucla.edu/login"), 1)
            build.assert_not_called()
            self.assertFalse((self.state / "login-request.json").exists())

    def test_verification_exception_rolls_back_new_daemon(self):
        auth = Mock(returncode=0)
        auth.poll.return_value = 0
        stop = self.auth_context(auth, Mock(side_effect=[None, 88, 88]))
        with patch.object(vpn, "listener_ready", return_value=True), \
                patch.object(vpn, "verify", side_effect=subprocess.TimeoutExpired("netstat", 15)):
            with self.assertRaises(subprocess.TimeoutExpired):
                vpn.start()
        stop.assert_called_once()
        self.assertFalse((self.state / "login-attempt.json").exists())

    def test_cancel_after_auth_exit_rejects_daemon(self):
        auth = Mock(returncode=0)
        auth.poll.return_value = 0
        stop = self.auth_context(auth, Mock(side_effect=[None, 88]))
        with patch.object(vpn, "login_cancelled", return_value=True), patch.object(vpn, "verify") as verify:
            self.assertEqual(vpn.start(), 1)
            verify.assert_not_called()
        stop.assert_called_once()

    def test_failed_result_write_cannot_skip_daemon_cleanup(self):
        auth = Mock(returncode=0)
        auth.poll.return_value = 0
        stop = self.auth_context(auth, Mock(side_effect=[None, 88, 88]))
        with patch.object(vpn, "listener_ready", return_value=True), \
                patch.object(vpn, "verify", side_effect=subprocess.TimeoutExpired("netstat", 15)), \
                patch.object(vpn, "write_login_result", side_effect=OSError("disk full")):
            with self.assertRaises(subprocess.TimeoutExpired):
                vpn.start()
        stop.assert_called_once()

    def test_connected_result_write_failure_rolls_back(self):
        auth = Mock(returncode=0)
        auth.poll.return_value = 0
        stop = self.auth_context(auth, Mock(side_effect=[None, 88, 88]))
        with patch.object(vpn, "listener_ready", return_value=True), \
                patch.object(vpn, "verify", return_value=0), patch.object(vpn, "ensure_idle_watch"), \
                patch.object(vpn, "write_login_result", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                vpn.start()
        stop.assert_called_once()

    def test_attempt_marker_unlink_failure_cannot_skip_cleanup(self):
        auth = Mock(returncode=0)
        auth.poll.return_value = 0
        stop = self.auth_context(auth, Mock(side_effect=[None, 88, 88]))
        unlink = Path.unlink

        def guarded_unlink(path, *args, **kwargs):
            if path.name == "login-attempt.json":
                raise PermissionError("state became unwritable")
            return unlink(path, *args, **kwargs)

        with patch.object(vpn, "listener_ready", return_value=True), \
                patch.object(vpn, "verify", side_effect=subprocess.TimeoutExpired("netstat", 15)), \
                patch.object(Path, "unlink", guarded_unlink):
            with self.assertRaises(subprocess.TimeoutExpired):
                vpn.start()
        stop.assert_called_once()

    def test_unresponsive_auth_is_reaped_on_cancel(self):
        auth = Mock(returncode=None)
        auth.poll.return_value = None
        auth.wait.side_effect = [subprocess.TimeoutExpired("openconnect", 10), 0]
        self.auth_context(auth)
        with patch.object(vpn, "login_cancelled", return_value=True):
            self.assertEqual(vpn.start(), 1)
        auth.terminate.assert_called_once()
        auth.kill.assert_called_once()
        self.assertEqual(auth.wait.call_args_list[1].kwargs, {"timeout": 5})

    def test_idle_releases_singleton_before_control_lock(self):
        events = []
        real_flock = fcntl.flock

        @contextmanager
        def control():
            events.append("control-enter")
            yield
            events.append("control-exit")

        def flock(file, operation):
            if operation == fcntl.LOCK_UN:
                events.append("idle-unlock")
            return real_flock(file, operation)

        with patch.object(vpn, "control_lock", control), patch.object(vpn, "owned_pid", return_value=None), \
                patch.object(vpn.fcntl, "flock", side_effect=flock):
            self.assertEqual(vpn.idle_watch(), 0)
        self.assertLess(events.index("idle-unlock"), events.index("control-exit"))
        self.assertFalse((self.state / "idle.pid").exists())

    def hold_lease(self, kind):
        leases = self.state / "connections"
        leases.mkdir(exist_ok=True)
        lease = leases / kind
        script = ("import fcntl,sys; f=open(sys.argv[1],'w'); fcntl.flock(f,fcntl.LOCK_EX); "
                  "f.write(sys.argv[2]+'\\n'); f.flush(); print('ready',flush=True); sys.stdin.read()")
        child = subprocess.Popen([sys.executable, "-c", script, str(lease), kind], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: child.poll() is None and (child.terminate(), child.communicate(timeout=5)))
        self.assertEqual(child.stdout.readline().strip(), "ready")
        return child, lease

    def test_real_lease_protects_connection_then_cleans(self):
        child, lease = self.hold_lease("ssh")
        self.assertEqual(vpn.transports(clean=True), ["ssh"])
        self.assertTrue(lease.exists())
        child.communicate(timeout=5)
        self.assertEqual(vpn.transports(clean=True), [])
        self.assertFalse(lease.exists())

    def test_mutagen_transport_does_not_hold_idle_timer(self):
        self.hold_lease("mutagen")
        activity = self.state / "last-used"
        activity.touch()
        os.utime(activity, (0, 0))
        with patch.object(vpn, "tunnel_sessions", return_value=[]):
            idle, holders = vpn.idle_state()
        self.assertEqual(holders, [])
        self.assertGreater(idle, 10**8)
        self.hold_lease("interactive")
        with patch.object(vpn, "tunnel_sessions", return_value=[]):
            self.assertEqual(vpn.idle_state(), (0, ["interactive"]))
        self.assertGreater(activity.stat().st_mtime, 0)

    def test_synced_file_change_is_activity_but_ignored_and_paused_are_not(self):
        root = self.state / "project"
        (root / ".git").mkdir(parents=True)
        (root / "src").mkdir()
        (root / "src/main.c").write_text("int main;")
        activity = self.state / "last-used"
        activity.touch()
        for path in (activity, root, root / ".git", root / "src", root / "src/main.c"):
            os.utime(path, (0, 0))
        (root / ".git/index").write_text("noise")  # ignored path, new mtime
        session = {"alpha": {"path": str(root)}, "ignore": {"paths": [".git/"]}}
        os.utime(root / ".git", (0, 0))
        os.utime(root, (0, 0))
        with patch.object(vpn, "tunnel_sessions", return_value=[session]):
            self.assertGreater(vpn.idle_state()[0], 10**8)
            os.utime(root / "src/main.c", None)
            self.assertLess(vpn.idle_state()[0], 60)
        with patch.object(vpn, "tunnel_sessions", return_value=[{**session, "paused": True}]):
            self.assertGreater(vpn.idle_state()[0], 10**8)

    def test_ignore_patterns_follow_mutagen_shapes(self):
        patterns = [".git/", "__pycache__/", ".DS_Store", "/build", "*.o", "!keep.o"]
        self.assertTrue(vpn.ignored(".git", ".git", True, patterns))
        self.assertFalse(vpn.ignored(".git", ".git", False, patterns))
        self.assertTrue(vpn.ignored("a/b/__pycache__", "__pycache__", True, patterns))
        self.assertTrue(vpn.ignored("x/.DS_Store", ".DS_Store", False, patterns))
        self.assertTrue(vpn.ignored("build", "build", True, patterns))
        self.assertFalse(vpn.ignored("src/build", "build", True, patterns))
        self.assertTrue(vpn.ignored("src/a.o", "a.o", False, patterns))
        self.assertFalse(vpn.ignored("src/keep.o", "keep.o", False, patterns))
        # Doublestar: * stays in one component, **/ may match nothing, {a,b} alternates.
        self.assertFalse(vpn.ignored("src/deep/x.c", "x.c", False, ["src/*.c"]))
        self.assertTrue(vpn.ignored("src/x.c", "x.c", False, ["src/*.c"]))
        self.assertTrue(vpn.ignored("x.log", "x.log", False, ["**/x.log"]))
        self.assertTrue(vpn.ignored("a/b/x.log", "x.log", False, ["**/x.log"]))
        self.assertTrue(vpn.ignored("out/a/b", "b", True, ["out/**"]))
        self.assertTrue(vpn.ignored("a.pyc", "a.pyc", False, ["*.{pyc,pyo}"]))
        self.assertFalse(vpn.ignored("a.py", "a.py", False, ["*.{pyc,pyo}"]))

    def test_idle_expiry_pauses_sync_then_retries_incomplete_shutdown(self):
        events = []
        clock = SimpleNamespace(time=lambda: 1000, strftime=time.strftime, sleep=Mock())
        with patch.object(vpn, "owned_pid", return_value=88), \
                patch.object(vpn, "idle_state", return_value=(4000, [])), \
                patch.object(vpn, "transports", return_value=["mutagen"]), \
                patch.object(vpn, "pause_sync", side_effect=lambda reason: events.append("pause")), \
                patch.object(vpn, "stop", side_effect=lambda: events.append("stop") or len(events) < 4), \
                patch.object(vpn, "control_lock", return_value=nullcontext()), \
                patch.object(vpn, "time", clock):
            self.assertEqual(vpn.idle_watch(), 0)
        self.assertEqual(events, ["pause", "stop", "pause", "stop"])
        clock.sleep.assert_called_once_with(vpn.CHECK_SECONDS)

    def test_active_session_is_not_stopped(self):
        clock = SimpleNamespace(time=lambda: 1000, strftime=time.strftime, sleep=Mock(side_effect=[None, StopIteration]))
        with patch.object(vpn, "owned_pid", return_value=88), \
                patch.object(vpn, "idle_state", return_value=(0, ["interactive"])), \
                patch.object(vpn, "pause_sync") as pause, patch.object(vpn, "stop") as stop, \
                patch.object(vpn, "control_lock", return_value=nullcontext()), \
                patch.object(vpn, "time", clock):
            with self.assertRaises(StopIteration):
                vpn.idle_watch()
        pause.assert_not_called()
        stop.assert_not_called()

    def test_idle_minutes_config_and_env(self):
        config = self.state / "config.toml"
        with patch.object(vpn, "CONFIG", config), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SEASNET_VPN_IDLE_MINUTES", None)
            self.assertEqual(vpn.idle_seconds(), vpn.DEFAULT_IDLE_MINUTES * 60)
            config.write_text("idle_minutes = 45\n")
            self.assertEqual(vpn.idle_seconds(), 2700)
            config.write_text("idle_minutes = -1\n")
            self.assertEqual(vpn.idle_seconds(), vpn.DEFAULT_IDLE_MINUTES * 60)
            os.environ["SEASNET_VPN_IDLE_MINUTES"] = "0.5"
            self.assertEqual(vpn.idle_seconds(), 30)

    def proxy_context(self, kind, up=False):
        contexts = [patch.object(vpn, "caller_kind", return_value=kind),
                    patch.object(vpn, "parent_args", return_value="ssh seasnet"),
                    patch.object(vpn, "tunnel_up", return_value=up),
                    patch.object(vpn.signal, "signal")]
        mocks = [context.start() for context in contexts]
        for context in contexts:
            self.addCleanup(context.stop)
        return mocks

    def test_background_ssh_never_starts_login(self):
        for kind in ("mutagen", "agent", "batch"):
            self.proxy_context(kind)
            errors = io.StringIO()
            with patch.object(vpn, "start") as start, patch.object(vpn.subprocess, "Popen") as popen, \
                    redirect_stderr(errors):
                self.assertEqual(vpn.proxy(vpn.TARGET, "22"), 1)
            start.assert_not_called()
            self.assertIn("no UCLA login opens" if kind != "mutagen" else "never opens UCLA login",
                          errors.getvalue())
            if kind == "mutagen":  # Only Mutagen's sessions get paused, detached.
                self.assertEqual(popen.call_args.args[0][1:], ["pause-sync"])
                self.assertTrue(popen.call_args.kwargs["start_new_session"])
            else:
                popen.assert_not_called()

    def test_failed_logins_back_off_automatic_prompts(self):
        self.proxy_context("interactive")
        for _ in range(3):
            vpn.record_login(False)
        errors = io.StringIO()
        with patch.object(vpn, "start") as start, redirect_stderr(errors):
            self.assertEqual(vpn.proxy(vpn.TARGET, "22"), 1)
        start.assert_not_called()
        self.assertIn("seasnet-vpn start", errors.getvalue())
        failures = json.loads((self.state / "login-failures.json").read_text())
        self.assertEqual(failures["count"], 3)
        vpn.write_json(self.state / "login-failures.json", {"count": 3, "last": failures["last"] - 241})
        self.assertIsNone(vpn.login_cooldown())  # 4 min cooldown after three failures
        vpn.record_login(True)
        self.assertFalse((self.state / "login-failures.json").exists())

    def test_start_refuses_without_terminal_or_for_agents(self):
        with patch.object(vpn, "owned_pid", return_value=None), \
                patch.object(vpn.subprocess, "Popen") as popen, redirect_stderr(io.StringIO()):
            with patch.object(vpn, "has_tty", return_value=False):
                self.assertEqual(vpn.start(), 1)
            with patch.object(vpn, "has_tty", return_value=True), \
                    patch.object(vpn, "agent_context", return_value=True), \
                    patch.dict(os.environ, {"SEASNET_VPN_ALLOW_AGENT_LOGIN": ""}):
                self.assertEqual(vpn.start(), 1)
        popen.assert_not_called()
        self.assertFalse((self.state / "login-attempt.json").exists())

    def test_diagnostic_browser_login_needs_terminal(self):
        with patch.object(vpn, "has_tty", return_value=False), patch.object(vpn, "build_login") as build, \
                patch.dict(os.environ, {"SEASNET_VPN_LOGIN_ATTEMPT": ""}), redirect_stderr(io.StringIO()):
            os.environ.pop("SEASNET_VPN_LOGIN_ATTEMPT")
            self.assertEqual(vpn.browser_login("https://shb.ais.ucla.edu/login"), 1)
        build.assert_not_called()

    def test_pause_and_resume_touch_only_own_sessions(self):
        sessions = [{"identifier": "a", "name": "mine"}, {"identifier": "b", "name": "manual", "paused": True}]
        calls = []
        ok = Mock(returncode=0, stderr="")
        with patch.object(vpn, "tunnel_sessions", return_value=sessions), \
                patch.object(vpn, "mutagen", side_effect=lambda *args, **kw: calls.append(args) or ok):
            vpn.pause_sync("idle")
        self.assertEqual(calls, [("sync", "pause", "a")])
        self.assertEqual(json.loads((self.state / "paused-sync.json").read_text()), ["a"])
        calls.clear()
        paused = [{**sessions[0], "paused": True}, sessions[1]]
        with patch.object(vpn, "tunnel_sessions", return_value=paused), \
                patch.object(vpn, "mutagen", side_effect=lambda *args, **kw: calls.append(args) or ok):
            vpn.resume_sync()
        self.assertEqual(calls, [("sync", "resume", "a")])
        self.assertFalse((self.state / "paused-sync.json").exists())

    def test_hangup_cleans_nc_and_lease_without_stdout_messages(self):
        handlers = {}
        client = Mock()
        client.poll.return_value = None
        client.wait.side_effect = [KeyboardInterrupt, 0]
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(vpn, "caller_kind", return_value="interactive"), \
                patch.object(vpn, "parent_args", return_value="ssh seasnet"), \
                patch.object(vpn, "tunnel_up", side_effect=[False, False]), \
                patch.object(vpn, "owned_pid", side_effect=[None, 88]), \
                patch.object(vpn, "start", side_effect=lambda: print("login progress") or 0), \
                patch.object(vpn, "ensure_idle_watch"), patch.object(vpn.subprocess, "Popen", return_value=client), \
                patch.object(vpn.signal, "signal", side_effect=lambda number, handler: handlers.update({number: handler})), \
                redirect_stdout(output), redirect_stderr(errors):
            with self.assertRaises(KeyboardInterrupt):
                vpn.proxy(vpn.TARGET, "22")
        self.assertIn(signal.SIGHUP, handlers)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("login progress", errors.getvalue())
        client.terminate.assert_called_once()
        self.assertFalse(list((self.state / "connections").iterdir()))

    def test_resume_keeps_record_when_nothing_was_resumed(self):
        vpn.write_json(self.state / "paused-sync.json", ["a", "gone"])
        with patch.object(vpn, "tunnel_sessions", return_value=None), \
                patch.object(vpn, "mutagen") as mutagen:
            vpn.resume_sync()  # daemon down: touch nothing
        mutagen.assert_not_called()
        self.assertEqual(json.loads((self.state / "paused-sync.json").read_text()), ["a", "gone"])
        sessions = [{"identifier": "a", "name": "mine", "paused": True}]
        for failure in (Mock(returncode=1, stderr="boom"), subprocess.TimeoutExpired("mutagen", 30)):
            effect = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
            with patch.object(vpn, "tunnel_sessions", return_value=sessions), \
                    patch.object(vpn, "mutagen", **effect):
                vpn.resume_sync()
            # The terminated session is dropped; the failed one is kept for next time.
            self.assertEqual(json.loads((self.state / "paused-sync.json").read_text()), ["a"])

    def test_idle_stop_cancelled_by_new_session_resumes_sync(self):
        events = []
        clock = SimpleNamespace(time=lambda: 1000, strftime=time.strftime, sleep=Mock(side_effect=StopIteration))
        with patch.object(vpn, "owned_pid", return_value=88), \
                patch.object(vpn, "idle_state", return_value=(4000, [])), \
                patch.object(vpn, "transports", return_value=["mutagen", "interactive"]), \
                patch.object(vpn, "pause_sync", side_effect=lambda reason: events.append("pause")), \
                patch.object(vpn, "resume_sync", side_effect=lambda: events.append("resume")), \
                patch.object(vpn, "stop") as stop, \
                patch.object(vpn, "control_lock", return_value=nullcontext()), \
                patch.object(vpn, "time", clock):
            with self.assertRaises(StopIteration):
                vpn.idle_watch()
        self.assertEqual(events, ["pause", "resume"])
        stop.assert_not_called()

    def test_queued_caller_rechecks_backoff_under_lock(self):
        self.proxy_context("interactive")
        @contextmanager
        def lock(wait=False):
            vpn.record_login(False)  # the login ahead of us was cancelled meanwhile
            yield
        with patch.object(vpn, "control_lock", lock), patch.object(vpn, "start") as start, \
                redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(vpn.proxy(vpn.TARGET, "22"), 1)
        start.assert_not_called()
        self.assertIn("will not reopen UCLA login", errors.getvalue())

    def test_open_tunnel_resumes_sync_left_paused_for_people_not_mutagen(self):
        client = Mock()
        client.wait.return_value = 0
        for kind, expected in (("mutagen", 0), ("interactive", 1)):
            vpn.write_json(self.state / "paused-sync.json", ["a"])
            self.proxy_context(kind, up=True)
            with patch.object(vpn, "owned_pid", return_value=88), patch.object(vpn, "ensure_idle_watch"), \
                    patch.object(vpn, "resume_sync") as resume, \
                    patch.object(vpn.subprocess, "Popen", return_value=client), redirect_stderr(io.StringIO()):
                self.assertEqual(vpn.proxy(vpn.TARGET, "22"), 0)
            self.assertEqual(resume.call_count, expected)

    def test_short_connect_timeout_refuses_instead_of_half_login(self):
        self.proxy_context("interactive")
        with patch.object(vpn, "parent_args", return_value="ssh -o ConnectTimeout=10 seasnet true"), \
                patch.object(vpn, "start") as start, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(vpn.proxy(vpn.TARGET, "22"), 1)
        start.assert_not_called()
        self.assertIn("ConnectTimeout too short", errors.getvalue())

    def test_caller_kind_from_parent_ssh(self):
        with patch.object(vpn, "agent_context", return_value=False):
            self.assertEqual(vpn.caller_kind("/usr/bin/ssh -oConnectTimeout=5 -oServerAliveInterval=10 seasnet "
                                             ".mutagen/agents/0.18.1/mutagen-agent synchronizer"), "mutagen")
            self.assertEqual(vpn.caller_kind("ssh -o BatchMode=true seasnet"), "batch")
            self.assertEqual(vpn.caller_kind("ssh -oBatchMode=yes seasnet"), "batch")
            self.assertEqual(vpn.caller_kind("ssh seasnet"), "interactive")
            with patch.object(vpn, "has_tty", return_value=False):
                self.assertEqual(vpn.caller_kind("ssh seasnet"), "batch")
        with patch.object(vpn, "agent_context", return_value=True):
            self.assertEqual(vpn.caller_kind("ssh seasnet"), "agent")

    def test_agent_ancestor_walks_process_tree(self):
        me, parent = os.getpid(), os.getppid()
        table = f"{me} {parent} /opt/homebrew/bin/python3\n{parent} 500 /bin/zsh\n500 400 /Users/x/.codex/bin/codex\n400 1 launchd\n"
        with patch.object(vpn, "capture", return_value=Mock(stdout=table)):
            self.assertEqual(vpn.agent_ancestor(), "codex")
        with patch.object(vpn, "capture", return_value=Mock(stdout=table.replace("codex", "tmux"))):
            self.assertIsNone(vpn.agent_ancestor())

    def test_ours_tolerates_unparseable_args(self):
        with patch.object(vpn, "process", return_value=f"{os.getuid()} 1 /usr/bin/grep grep it's ocproxy"):
            self.assertIsNone(vpn.ours(123))
        with patch.object(vpn, "process", return_value=f"{os.getuid()} 1 /opt/homebrew/bin/ocproxy "
                                                       f"/opt/homebrew/bin/ocproxy -L {vpn.PORT}:{vpn.TARGET}:22 -k 30"):
            self.assertEqual(vpn.ours(123), "ocproxy")

    def test_nonfinite_idle_minutes_fall_back(self):
        with patch.dict(os.environ, {"SEASNET_VPN_IDLE_MINUTES": "nan"}):
            self.assertEqual(vpn.idle_seconds(), vpn.DEFAULT_IDLE_MINUTES * 60)


if __name__ == "__main__":
    unittest.main()
