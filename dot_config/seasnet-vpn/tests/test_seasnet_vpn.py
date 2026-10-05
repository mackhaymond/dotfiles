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

    def request(self, identity="current", attempt=None):
        vpn.write_json(self.state / "login-request.json",
                       {"id": identity, "url": "https://shb.ais.ucla.edu/login", "attempt": attempt})

    def auth_context(self, auth, owned=None):
        contexts = [patch.object(vpn, "snapshot", return_value={}),
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

    def test_real_lease_protects_connection_then_cleans(self):
        leases = self.state / "connections"
        leases.mkdir()
        lease = leases / "child"
        script = "import fcntl,sys; f=open(sys.argv[1],'w'); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); sys.stdin.read()"
        child = subprocess.Popen([sys.executable, "-c", script, str(lease)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            with patch.object(vpn, "capture", return_value=Mock(stdout="")):
                self.assertEqual(vpn.active_connections(clean=True), 1)
                self.assertTrue(lease.exists())
                child.communicate(timeout=5)
                self.assertEqual(vpn.active_connections(clean=True), 0)
                self.assertFalse(lease.exists())
        finally:
            if child.poll() is None:
                child.terminate()
                child.communicate(timeout=5)

    def test_idle_expiry_retries_incomplete_shutdown(self):
        activity = self.state / "last-used"
        activity.touch()
        os.utime(activity, (0, 0))
        clock = SimpleNamespace(time=lambda: 1000, sleep=Mock())
        with patch.object(vpn, "owned_pid", return_value=88), \
                patch.object(vpn, "active_connections", return_value=0), \
                patch.object(vpn, "stop", side_effect=[1, 0]) as stop, \
                patch.object(vpn, "control_lock", return_value=nullcontext()), \
                patch.object(vpn, "time", clock):
            self.assertEqual(vpn.idle_watch(), 0)
        self.assertEqual(stop.call_count, 2)
        clock.sleep.assert_called_once_with(15)

    def test_active_lease_refreshes_idle_before_later_expiry(self):
        activity = self.state / "last-used"
        activity.touch()
        os.utime(activity, (0, 0))

        def sleep(_):
            self.assertGreater(activity.stat().st_mtime, 0)
            os.utime(activity, (0, 0))

        clock = SimpleNamespace(time=lambda: 1000, sleep=sleep)
        with patch.object(vpn, "owned_pid", return_value=88), \
                patch.object(vpn, "active_connections", side_effect=[1, 0]), \
                patch.object(vpn, "stop", return_value=0) as stop, \
                patch.object(vpn, "control_lock", return_value=nullcontext()), \
                patch.object(vpn, "time", clock):
            self.assertEqual(vpn.idle_watch(), 0)
        stop.assert_called_once()

    def test_hangup_cleans_nc_and_lease_without_stdout_messages(self):
        handlers = {}
        client = Mock()
        client.poll.return_value = None
        client.wait.side_effect = [KeyboardInterrupt, 0]
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(vpn, "owned_pid", return_value=None), \
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


if __name__ == "__main__":
    unittest.main()
