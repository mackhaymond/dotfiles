"""Exercise sleep/wake transitions against an isolated real child process."""
import contextlib
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
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('guard', Path(__file__).with_name('guard.py'))
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class Power:
    awake = True
    def full_wake(self):
        return self.awake


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_patch = patch.object(guard, 'STATE', Path(self.temp.name) / 'state.json')
        self.state_patch.start()
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.power = Power()
        self.child = subprocess.Popen(
            [sys.executable, '-c', 'import time; print("ready", flush=True); time.sleep(60)'],
            stdout=subprocess.PIPE, text=True)
        self.assertEqual(self.child.stdout.readline().strip(), 'ready')
        self.pid = str(self.child.pid)
        self.start = 'test-child-start'
        self.scan_patch = patch.object(guard, 'processes', side_effect=self.scan)
        self.scan_patch.start()
        self.controller = guard.Guard(self.power)

    def tearDown(self):
        self.scan_patch.stop()
        self.state_patch.stop()
        self.output.__exit__(None, None, None)
        if self.child.poll() is None:
            os.kill(self.child.pid, signal.SIGCONT)
            self.child.terminate()
        self.child.wait(timeout=5)
        self.child.stdout.close()
        self.temp.cleanup()

    def stopped(self):
        status = subprocess.check_output(['/bin/ps', '-p', self.pid, '-o', 'stat='], text=True)
        return 'T' in status

    def scan(self):
        return {self.pid: {'start': self.start, 'stopped': self.stopped()}}

    def wait_stopped(self, desired):
        for _ in range(30):
            if self.stopped() == desired:
                return
            time.sleep(.01)
        self.fail('child did not reach expected suspended state')

    def test_sleep_then_multiple_dark_wakes_then_full_wake(self):
        self.controller.tick(sleeping=True)
        self.wait_stopped(True)
        self.power.awake = False
        for _ in range(3):
            self.controller.tick()
            self.assertTrue(self.stopped())
        self.power.awake = True
        self.controller.tick()
        self.wait_stopped(False)
        self.assertEqual(self.controller.state['held'], {})

    def test_sleep_pending_prevents_early_thaw_before_hardware_sleeps(self):
        self.controller.sleep_pending = True
        self.controller.tick()
        self.wait_stopped(True)
        self.controller.tick()
        self.assertTrue(self.stopped())
        self.controller.sleep_pending = False
        self.controller.tick()
        self.wait_stopped(False)

    def test_daemon_restart_recovers_owned_pause(self):
        self.power.awake = False
        self.controller.tick()
        self.wait_stopped(True)
        restarted = guard.Guard(self.power)
        restarted.tick()
        self.assertTrue(self.stopped())
        self.power.awake = True
        restarted.tick()
        self.wait_stopped(False)

    def test_full_wake_does_not_resume_a_process_stopped_by_someone_else(self):
        os.kill(self.child.pid, signal.SIGSTOP)
        self.wait_stopped(True)
        self.power.awake = False
        self.controller.tick()
        self.power.awake = True
        self.controller.tick()
        self.assertTrue(self.stopped())
        self.assertEqual(self.controller.state['held'], {})

    def test_pid_reuse_does_not_resume_new_process(self):
        self.controller.state['held'][self.pid] = {'start': 'old-process'}
        os.kill(self.child.pid, signal.SIGSTOP)
        self.wait_stopped(True)
        self.controller.tick()
        self.assertTrue(self.stopped())
        self.assertEqual(self.controller.state['held'], {})

    def test_external_continue_during_dark_wake_is_held_again(self):
        self.power.awake = False
        self.controller.tick()
        self.wait_stopped(True)
        os.kill(self.child.pid, signal.SIGCONT)
        self.wait_stopped(False)
        # Allow SIGCONT delivery to settle before sending a second SIGSTOP.
        time.sleep(.05)
        self.controller.tick()
        self.wait_stopped(True)

    def test_full_wake_does_not_pause_running_child(self):
        self.controller.tick()
        self.assertFalse(self.stopped())

    def test_hook_blocks_dark_wake_and_allows_full_wake(self):
        with patch.object(guard, 'Power', return_value=self.power):
            for awake in [False, True]:
                self.power.awake = awake
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    guard.hook()
                if awake:
                    self.assertEqual(output.getvalue(), '')
                else:
                    self.assertFalse(json.loads(output.getvalue())['continue'])


if __name__ == '__main__':
    unittest.main()
