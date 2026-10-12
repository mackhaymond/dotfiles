"""Focused policy/ownership tests and isolated real-process heartbeat checks."""
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
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('guard', Path(__file__).with_name('guard.py'))
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def snapshot(cpu=True, graphics=True, wake='User', user=True, epoch='100:1'):
    return {'cpu': cpu, 'graphics': graphics, 'wakeType': wake,
            'userTriggeredFullWake': user, 'wakeEpoch': epoch}


class Power:
    def __init__(self):
        self.value = snapshot()
    def snapshot(self):
        return self.value.copy()


class PolicyTests(unittest.TestCase):
    def test_headless_without_system_sleep_permits_independent_of_apps(self):
        for wake, user in ((None, None), ('User', True), ('UserActivity Assertion', True)):
            self.assertTrue(guard.allowed(snapshot(graphics=False, wake=wake, user=user)))
        established = {'held': {}, 'sleepHold': False, 'sleepPhase': 'awake', 'lastWakeEpoch': '100:1'}
        for wake in guard.BACKGROUND_WAKE_TYPES:
            self.assertTrue(guard.allowed(snapshot(graphics=False, wake=wake, user=False), established))
        self.assertFalse(guard.allowed(snapshot(cpu=False)))

    def test_all_incidental_wakes_stay_held_after_actual_sleep(self):
        state = {'held': {}, 'sleepHold': True, 'sleepPhase': 'waking'}
        categories = {'Network': 144, 'SleepService': 18, 'Maintenance': 6, 'Notification': 2}
        for category, count in categories.items():
            for _ in range(count):
                self.assertFalse(guard.allowed(snapshot(wake=category, user=False, graphics=category == 'Notification'), state))

    def test_generic_interactive_wake_requires_CPU_and_graphics(self):
        state = {'held': {}, 'sleepHold': True, 'sleepPhase': 'waking'}
        for wake in ('User', 'HID Activity', 'Lid Open'):
            self.assertTrue(guard.allowed(snapshot(wake=wake, user=False), state))
            self.assertFalse(guard.allowed(snapshot(wake=wake, user=False, graphics=False), state))
        self.assertTrue(guard.allowed(snapshot(wake='UserActivity Assertion', user=True), state))
        self.assertFalse(guard.allowed(snapshot(cpu=False), state))

    def test_pre_sleep_stale_User_metadata_cannot_clear_armed_hold(self):
        state = {'held': {}, 'sleepHold': True, 'sleepPhase': 'armed', 'sleepWakeEpoch': '100:1'}
        self.assertFalse(guard.allowed(snapshot(), state))
        self.assertFalse(guard.allowed(snapshot(epoch=None), state))
        self.assertFalse(guard.allowed(snapshot(epoch='0:0'), state))
        self.assertTrue(guard.allowed(snapshot(epoch='101:1'), state))
        self.assertTrue(guard.allowed(snapshot(epoch='99:1'), state))  # clock adjustment
        self.assertFalse(guard.allowed(snapshot(epoch='101:1', wake='Notification', user=False), state))

    def test_startup_dark_wake_vs_headless_without_sleep(self):
        self.assertFalse(guard.allowed(snapshot(graphics=False, wake='Maintenance', user=False)))
        self.assertFalse(guard.allowed(snapshot(wake='Notification', user=False)))
        self.assertTrue(guard.allowed(snapshot(graphics=False, wake=None, user=None)))
        self.assertTrue(guard.allowed(snapshot(graphics=False)))
        old = {'held': {'99': {'start': 'old'}}}
        self.assertFalse(guard.allowed(snapshot(graphics=False), old))
        self.assertTrue(guard.allowed(snapshot(), old))

    def test_hook_uses_readonly_latch_and_same_policy(self):
        power = Power()
        with tempfile.TemporaryDirectory() as directory, patch.object(guard, 'STATE', Path(directory) / 'state.json'), patch.object(guard, 'Power', return_value=power):
            for value in (snapshot(), snapshot(graphics=False), snapshot(wake='Notification', user=False)):
                power.value = value
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    guard.hook()
                if guard.allowed(value):
                    self.assertEqual(output.getvalue(), '')
                else:
                    self.assertFalse(json.loads(output.getvalue())['continue'])
            guard.save_json(guard.STATE, {'held': {}, 'sleepHold': True, 'sleepPhase': 'armed', 'sleepWakeEpoch': '100:1'})
            power.value = snapshot()
            with patch.object(guard, 'save_json', side_effect=AssertionError('hook wrote journal')), contextlib.redirect_stdout(io.StringIO()) as output:
                guard.hook()
            self.assertFalse(json.loads(output.getvalue())['continue'])

    def test_unknown_capabilities_never_newly_arm_but_preserve_existing_hold(self):
        clear = {'held': {}, 'sleepHold': False, 'sleepPhase': 'awake'}
        hold = {'held': {}, 'sleepHold': True, 'sleepPhase': 'waking'}
        self.assertTrue(guard.allowed(snapshot(cpu=None, graphics=None), clear))
        self.assertFalse(guard.allowed(snapshot(cpu=None, graphics=None), hold))

    def test_readonly_policy_detects_actual_wake_while_daemon_was_down(self):
        state = {'held': {}, 'sleepHold': False, 'sleepPhase': 'awake', 'lastWakeEpoch': '100:1'}
        self.assertFalse(guard.allowed(snapshot(epoch='101:1', wake='Notification', user=False), state))
        self.assertTrue(guard.allowed(snapshot(epoch='101:1'), state))
        self.assertFalse(state['sleepHold'])  # readonly

    def test_hook_unknown_native_state_preserves_existing_hold_without_inventing_one(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(guard, 'STATE', Path(directory) / 'state.json'), patch.object(guard, 'Power', side_effect=RuntimeError), patch.object(guard, 'wake_epoch', return_value='100:1'):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                guard.hook()
            self.assertEqual(output.getvalue(), '')
            guard.save_json(guard.STATE, {'held': {}, 'sleepHold': True, 'sleepPhase': 'armed', 'sleepWakeEpoch': '100:1'})
            with contextlib.redirect_stdout(io.StringIO()) as output:
                guard.hook()
            self.assertFalse(json.loads(output.getvalue())['continue'])

    def test_installed_cli_roots_and_gui_exclusions(self):
        cases = {
            str(guard.HOME / '.local/share/claude/versions/2.0.1'): 'claude',
            '/opt/homebrew/Cellar/claude-code/2.0/bin/claude': 'claude',
            str(guard.HOME / '.bun/install/global/node_modules/@openai/codex-darwin-arm64/bin/codex'): 'codex',
            '/usr/local/lib/node_modules/@openai/codex/vendor/x86_64-apple-darwin/codex/codex': 'codex',
            '/opt/homebrew/Cellar/codex/0.1/bin/codex': 'codex',
            '/Applications/Codex.app/Contents/MacOS/codex': None,
            '/Applications/Claude.app/Contents/MacOS/claude': None,
            '/tmp/codex': None,
            str(guard.HOME / '.bun/install/global/node_modules/unrelated/bin/codex'): None,
        }
        for path, kind in cases.items():
            self.assertEqual(guard.agent_kind(path), kind, path)

    def test_only_proven_descendant_tree_is_selected(self):
        table = {'1': {'path': '/bin/claude', 'ppid': '0'}, '2': {'path': '/bin/sh', 'ppid': '1'},
                 '3': {'path': '/bin/tool', 'ppid': '2'}, '4': {'path': '/bin/tmux', 'ppid': '1'},
                 '5': {'path': '/bin/tool', 'ppid': '4'}, '6': {'path': '/Applications/App.app/Contents/MacOS/App', 'ppid': '1'}}
        self.assertEqual(guard.descendants(table, {'1'}), {'1', '2', '3'})

    def test_Xcode_cli_tools_are_descendants_but_GUI_helpers_are_excluded(self):
        self.assertTrue(guard.safe_descendant('/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang'))
        self.assertTrue(guard.safe_descendant('/Applications/Xcode.app/Contents/Developer/usr/bin/swift'))
        self.assertFalse(guard.safe_descendant('/Applications/Xcode.app/Contents/MacOS/Xcode'))
        self.assertFalse(guard.safe_descendant('/Applications/App.app/Contents/Frameworks/Helper.app/Contents/MacOS/Helper'))


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.json'
        self.power = Power()
        self.table = {'991': {'start': '10:20', 'path': str(guard.HOME / '.local/share/claude/versions/test'), 'ppid': '1', 'stopped': False},
                      '992': {'start': '11:20', 'path': '/bin/sh', 'ppid': '991', 'stopped': False}}
        self.signals = []
        self.controller = self.create()
    def tearDown(self):
        self.temp.cleanup()
    def create(self):
        return guard.Guard(self.power, self.path, scanner=lambda: {pid: proc.copy() for pid, proc in self.table.items()},
                           lookup=lambda pid: self.table.get(str(pid)), kill=self.kill)
    def kill(self, pid, sig):
        self.signals.append((str(pid), sig))
        self.table[str(pid)]['stopped'] = sig == signal.SIGSTOP
    def deny(self):
        self.power.value = snapshot(cpu=False, graphics=False, wake=None, user=None)
        self.controller.tick()

    def test_pause_root_and_children_and_resume_on_interactive_wake(self):
        self.deny()
        self.assertEqual(set(self.controller.state['held']), {'991', '992'})
        self.assertTrue(all(proc['stopped'] for proc in self.table.values()))
        self.power.value = snapshot()
        self.controller.tick()
        self.assertFalse(any(proc['stopped'] for proc in self.table.values()))

    def test_headless_continuous_CPU_never_stops_work(self):
        self.power.value = snapshot(graphics=False)
        self.controller.tick()
        for wake in guard.BACKGROUND_WAKE_TYPES:
            self.power.value = snapshot(graphics=False, wake=wake, user=False)
            self.controller.tick()
        self.assertEqual(self.signals, [])
        self.assertFalse(self.controller.state['sleepHold'])

    def test_restart_full_wake_thaws_held_children(self):
        self.deny()
        restarted = self.create()
        restarted.tick()
        self.assertTrue(self.table['992']['stopped'])
        self.power.value = snapshot()
        restarted.tick()
        self.assertEqual(restarted.state['held'], {})
        self.assertFalse(self.table['992']['stopped'])

    def test_corrupt_primary_repaired_from_backup(self):
        self.deny()
        self.path.write_text('{broken')
        restarted = self.create()
        self.assertEqual(set(restarted.state['held']), {'991', '992'})
        self.assertIsNotNone(guard.read_json(self.path))
        self.power.value = snapshot()
        restarted.tick()
        self.assertFalse(self.table['992']['stopped'])

    def test_corrupt_backup_repaired_from_primary_and_invalid_held_list(self):
        self.deny()
        self.path.with_name('state.json.backup').write_text('{"held": []}')
        self.assertEqual(set(self.create().state['held']), {'991', '992'})
        self.assertIsNotNone(guard.read_json(self.path.with_name('state.json.backup')))

    def run_status(self):
        with patch.object(guard, 'STATE', self.path), patch.object(guard, 'Power', return_value=self.power), patch.object(guard, 'processes', return_value={}), patch.object(sys, 'argv', ['guard.py', 'status']), contextlib.redirect_stdout(io.StringIO()) as output:
            guard.main()
        return json.loads(output.getvalue())

    def test_status_never_repairs_or_changes_mirror_generation(self):
        self.deny()
        backup = self.path.with_name('state.json.backup')
        # Invalid primary with a good backup is readable, but status must not
        # repair it: only the daemon/recovery lock owner may mutate journals.
        self.path.write_text('{"held": []}')
        before = (self.path.read_bytes(), backup.read_bytes())
        expected_generation = json.loads(backup.read_text())['generation']
        with patch.object(guard, 'save_json', side_effect=AssertionError('status wrote ownership')):
            result = self.run_status()
        self.assertEqual(result['state']['generation'], expected_generation)
        self.assertEqual(set(result['state']['held']), {'991', '992'})
        self.assertEqual(before, (self.path.read_bytes(), backup.read_bytes()))

    def test_status_during_backup_first_save_preserves_pending_ownership(self):
        self.deny()
        backup = self.path.with_name('state.json.backup')
        self.controller.state['held']['993'] = {'start': '12:20', 'path': '/bin/tool'}
        original = guard.save_json
        writes = []
        status_results = []
        def interleaved(path, value):
            writes.append(Path(path))
            original(path, value)
            if Path(path) == backup:
                status_results.append(self.run_status())
        with patch.object(guard, 'save_json', side_effect=interleaved):
            self.controller.journal.save()
        self.assertEqual(writes, [backup, self.path])
        self.assertIn('993', status_results[0]['state']['held'])
        self.assertEqual(guard.read_json(self.path), guard.read_json(backup))
        self.assertIn('993', guard.read_json(self.path)['held'])

    def test_both_corrupt_fail_without_signals(self):
        self.path.write_text('{"held": []}')
        self.path.with_name('state.json.backup').write_text('broken')
        with self.assertRaises(RuntimeError):
            self.create()
        self.assertEqual(self.signals, [])

    def test_state_write_must_complete_before_stop(self):
        self.power.value = snapshot(cpu=False, graphics=False, wake=None, user=None)
        with patch.object(self.controller.journal, 'save', side_effect=OSError):
            with self.assertRaises(OSError):
                self.controller.tick()
        self.assertEqual(self.signals, [])

    def test_failed_mirror_write_is_retried_before_any_later_stop(self):
        self.power.value = snapshot(cpu=False, graphics=False, wake=None, user=None)
        with patch.object(self.controller.journal, 'save', side_effect=OSError):
            for _ in range(2):
                with self.assertRaises(OSError):
                    self.controller.tick()
            self.assertEqual(self.signals, [])
        self.controller.tick()
        self.assertTrue(self.table['991']['stopped'])

    def test_manually_stopped_is_never_claimed_or_resumed(self):
        self.table['992']['stopped'] = True
        self.deny()
        self.assertNotIn('992', self.controller.state['held'])
        self.power.value = snapshot()
        self.controller.tick()
        self.assertTrue(self.table['992']['stopped'])

    def test_pid_reuse_does_not_signal_new_process(self):
        self.deny()
        self.signals.clear()
        self.table['991']['start'] = 'new'
        self.table['992']['path'] = '/bin/different'
        self.power.value = snapshot()
        self.controller.tick()
        self.assertEqual(self.signals, [])
        self.assertEqual(self.controller.state['held'], {})

    def test_reused_PID_as_new_CLI_is_paused_in_same_tick(self):
        self.deny()
        self.table['991'].update(start='replacement', stopped=False)
        self.signals.clear()
        self.controller.tick()
        self.assertIn(('991', signal.SIGSTOP), self.signals)
        self.assertEqual(self.controller.state['held']['991']['start'], 'replacement')

    def test_orphaned_owned_family_new_children_are_held(self):
        self.deny()
        del self.table['991']
        self.table['992'].update(ppid='1', stopped=False)
        self.table['993'] = {'start': '12:20', 'path': '/bin/tool', 'ppid': '992', 'stopped': False}
        self.controller.tick()
        self.assertTrue(self.table['992']['stopped'])
        self.assertTrue(self.table['993']['stopped'])
        self.assertIn('993', self.controller.state['held'])

    def test_child_fork_while_stop_is_delivered_is_held_in_same_tick(self):
        original = self.controller.kill
        forked = False
        def fork_on_stop(pid, sig):
            nonlocal forked
            if pid == 992 and sig == signal.SIGSTOP and not forked:
                forked = True
                self.table['993'] = {'start': '12:30', 'path': '/bin/tool', 'ppid': '992', 'stopped': False}
            original(pid, sig)
        self.controller.kill = fork_on_stop
        self.deny()
        self.assertTrue(self.table['993']['stopped'])
        self.assertIn('993', self.controller.state['held'])

    def test_recheck_identity_before_signal(self):
        original = self.controller.lookup
        calls = 0
        def changed(pid):
            nonlocal calls
            calls += 1
            value = original(pid).copy()
            if calls >= 2:
                value['start'] = 'replacement'
            return value
        self.controller.lookup = changed
        self.controller._pause('991', self.table['991'])
        self.assertEqual(self.signals, [])

    def test_explicit_release_and_termination_policy(self):
        self.deny()
        self.controller.terminate()
        self.assertTrue(self.table['991']['stopped'])
        self.controller.release()
        self.assertFalse(self.controller.state['sleepHold'])
        self.assertFalse(self.table['991']['stopped'])
        self.assertEqual(self.controller.state['held'], {})
        self.controller = self.create()
        self.deny()
        self.power.value = snapshot()
        self.controller.terminate()
        self.assertFalse(self.table['991']['stopped'])

    def test_transition_freezes_before_registry_then_cancellation_reconciles(self):
        self.controller.transition(snapshot(cpu=False, graphics=False))
        self.assertTrue(self.table['991']['stopped'])
        self.controller.tick()
        self.assertTrue(self.table['991']['stopped'])
        self.controller.cancel_sleep()
        self.controller.tick()
        self.assertFalse(self.table['991']['stopped'])

    def test_latch_has_no_time_expiry_and_target_wake_does_not_thaw_background(self):
        self.controller.transition(snapshot(cpu=False, graphics=False))
        for _ in range(20):
            self.controller.tick()
        self.assertTrue(self.table['991']['stopped'])
        self.power.value = snapshot(wake='Notification', user=False, epoch='101:1')
        self.controller.transition(snapshot(epoch='101:1'))
        self.assertTrue(self.table['991']['stopped'])
        self.power.value = snapshot(wake='HID Activity', user=False)
        self.controller.tick()
        self.assertFalse(self.table['991']['stopped'])

    def test_restart_pending_rejects_stale_User_then_changed_epoch_resumes(self):
        self.controller.transition(snapshot(cpu=False))
        restarted = self.create()
        restarted.tick()
        self.assertTrue(self.table['991']['stopped'])
        self.power.value = snapshot(epoch='101:1')
        restarted.tick()
        self.assertFalse(self.table['991']['stopped'])

    def test_restart_after_sleep_background_epoch_stays_held(self):
        self.controller.transition(snapshot(cpu=False))
        self.power.value = snapshot(epoch='101:1', wake='Maintenance', user=False, graphics=False)
        restarted = self.create()
        restarted.tick()
        self.assertTrue(self.table['991']['stopped'])
        self.assertEqual(restarted.state['sleepPhase'], 'waking')
        self.power.value = snapshot(epoch='101:1', wake='HID Activity', user=False)
        restarted.tick()
        self.assertFalse(self.table['991']['stopped'])

    def test_epoch_change_proves_sleep_missed_by_daemon_while_down(self):
        self.controller.tick()
        self.power.value = snapshot(epoch='101:1', wake='Notification', user=False)
        restarted = self.create()
        restarted.tick()
        self.assertTrue(self.table['991']['stopped'])
        self.power.value = snapshot(epoch='101:1', wake='User', user=True)
        restarted.tick()
        self.assertFalse(self.table['991']['stopped'])

    def test_arming_after_an_epoch_change_uses_current_baseline_not_stale_history(self):
        self.controller.tick()
        self.power.value = snapshot(epoch='101:1')
        self.controller.transition(snapshot(cpu=False, epoch='101:1'))
        self.assertTrue(self.table['991']['stopped'])
        self.assertEqual(self.controller.state['sleepPhase'], 'armed')
        self.assertEqual(self.controller.state['sleepWakeEpoch'], '101:1')

    def test_actual_sleep_then_Notification_ignores_unrelated_late_cancel(self):
        self.controller.tick()
        self.controller.transition(snapshot(cpu=False))
        self.power.value = snapshot(epoch='101:1', wake='Notification', user=False)
        self.controller.transition(snapshot(epoch='101:1'))
        self.assertNotIn('preSleepHold', self.controller.state)
        self.controller.cancel_sleep()
        self.assertTrue(self.table['991']['stopped'])
        self.assertTrue(self.controller.state['sleepHold'])
        self.controller.transition(snapshot(cpu=False, epoch='101:1'))
        self.controller.cancel_sleep()
        self.assertTrue(self.table['991']['stopped'])
        self.assertTrue(self.controller.state['sleepHold'])

    def test_synthetic_CPU_gain_cancels_sleep_for_headless_dark_client(self):
        self.power.value = snapshot(graphics=False, wake=None, user=None)
        self.controller.tick()
        self.controller.transition(snapshot(cpu=False, graphics=False, wake=None, user=None))
        self.assertTrue(self.table['991']['stopped'])
        self.controller.transition(self.power.snapshot())
        self.assertFalse(self.table['991']['stopped'])
        self.assertFalse(self.controller.state['sleepHold'])
        self.assertNotIn('preSleepHold', self.controller.state)

    def test_unknown_root_metadata_without_sleep_does_not_signal(self):
        self.power.value = snapshot(cpu=None, graphics=None)
        self.controller.tick()
        self.assertEqual(self.signals, [])

    def test_legacy_state_migration_is_identity_scoped(self):
        self.table['991']['legacyStart'] = 'Mon Oct 12 07:00:00 2026'
        guard.save_json(self.path, {'held': {'991': {'start': self.table['991']['legacyStart']}}})
        self.table['991']['stopped'] = True
        self.create().release()
        self.assertFalse(self.table['991']['stopped'])


class CallbackTests(unittest.TestCase):
    def test_target_callback_acknowledges_even_exception(self):
        power = guard.Power.__new__(guard.Power)
        power.snapshot = lambda: snapshot()
        class Broken:
            def transition(self, value):
                raise RuntimeError('injected')
        tokens = []
        with contextlib.redirect_stderr(io.StringIO()) as output:
            power.target_event(Broken(), 77, 0, tokens.append)
        self.assertEqual(tokens, [77])
        self.assertIn('RuntimeError', output.getvalue())

    def test_target_CPU_loss_freezes_before_ack_and_legacy_sleep_never_arms(self):
        power = guard.Power.__new__(guard.Power)
        power.snapshot = lambda: snapshot()
        with tempfile.TemporaryDirectory() as directory:
            table = {'991': {'start': '10:1', 'path': str(guard.HOME / '.local/share/claude/versions/test'), 'ppid': '1', 'stopped': False}}
            def kill(pid, sig):
                table[str(pid)]['stopped'] = sig == signal.SIGSTOP
            controller = guard.Guard(power, Path(directory) / 'state.json', scanner=lambda: table.copy(), lookup=lambda pid: table.get(str(pid)), kill=kill)
            acknowledgements = []
            def ack(token):
                self.assertTrue(table['991']['stopped'])
                self.assertTrue(guard.read_json(Path(directory) / 'state.json')['sleepHold'])
                acknowledgements.append(token)
            power.legacy_event(controller, guard.WILL_SLEEP, 88, acknowledgements.append)
            self.assertFalse(table['991']['stopped'])
            power.target_event(controller, 77, 0, ack)
            self.assertEqual(acknowledgements, [88, 77])
            power.legacy_event(controller, guard.WILL_NOT_SLEEP, 0, acknowledgements.append)
            self.assertFalse(table['991']['stopped'])

    def test_target_CPU_loss_still_freezes_if_root_snapshot_raises(self):
        power = guard.Power.__new__(guard.Power)
        power.snapshot = lambda: (_ for _ in ()).throw(RuntimeError('optional metadata unavailable'))
        with tempfile.TemporaryDirectory() as directory:
            table = {'991': {'start': '10:1', 'path': str(guard.HOME / '.local/share/claude/versions/test'), 'ppid': '1', 'stopped': False}}
            def kill(pid, sig):
                table[str(pid)]['stopped'] = sig == signal.SIGSTOP
            controller = guard.Guard(power, Path(directory) / 'state.json', scanner=lambda: table.copy(), lookup=lambda pid: table.get(str(pid)), kill=kill)
            ack = []
            power.target_event(controller, 55, 0, ack.append)
            self.assertTrue(table['991']['stopped'])
            self.assertTrue(controller.state['sleepHold'])
            self.assertEqual(ack, [55])

    def test_target_CPU_remains_on_even_video_loss_never_arms(self):
        power = guard.Power.__new__(guard.Power)
        power.snapshot = lambda: snapshot(graphics=False)
        with tempfile.TemporaryDirectory() as directory:
            controller = guard.Guard(power, Path(directory) / 'state.json', scanner=lambda: {}, lookup=lambda pid: None)
            ack = []
            power.target_event(controller, 12, 1 | 16, ack.append)
            power.legacy_event(controller, guard.WILL_SLEEP, 13, ack.append)
            self.assertTrue(controller.policy(power.snapshot()))
            self.assertFalse(controller.state['sleepHold'])
            self.assertEqual(ack, [12, 13])

    def test_canceled_sleep_preserves_prior_background_hold(self):
        power = guard.Power.__new__(guard.Power)
        power.snapshot = lambda: snapshot(wake='Notification', user=False)
        with tempfile.TemporaryDirectory() as directory:
            controller = guard.Guard(power, Path(directory) / 'state.json', scanner=lambda: {}, lookup=lambda pid: None)
            controller.tick()
            self.assertTrue(controller.state['sleepHold'])
            power.target_event(controller, 1, 0, lambda token: None)
            power.legacy_event(controller, guard.WILL_NOT_SLEEP, 0, lambda token: None)
            self.assertTrue(controller.state['sleepHold'])



@unittest.skipUnless(sys.platform == 'darwin', 'native macOS APIs')
class NativeTests(unittest.TestCase):
    def test_native_identity_and_power_snapshot_read_only(self):
        proc = guard.native().get(os.getpid())
        self.assertTrue(Path(proc['path']).is_file())
        self.assertTrue(guard.safe_descendant(proc['path']))
        self.assertFalse(proc['stopped'])
        value = guard.Power().snapshot()
        self.assertIn(value['cpu'], (True, False, None))
        self.assertTrue(value['wakeEpoch'] is None or guard.valid_epoch(value['wakeEpoch']))
        self.assertNotIn('liveAwakeHold', value)
        self.assertNotIn('lidClosed', value)

    def test_native_listener_registration_and_readiness_without_signals(self):
        power = guard.Power()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'state.json'
            ticks = []
            class Controller:
                def tick(self):
                    ticks.append(1)
                    threading.Timer(.2, power.stop).start()
                def transition(self, value):
                    pass
                def cancel_sleep(self):
                    pass
                def woke(self):
                    pass
            with patch.object(guard, 'STATE', path):
                power.run(Controller())
                self.assertFalse((path.parent / 'ready.json').exists())
            self.assertTrue(ticks)
            self.assertEqual(power.backend, 'connection')

    def test_root_and_child_real_heartbeats_freeze_resume_and_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            script = '''import os,sys,time,subprocess
from pathlib import Path
root,child=sys.argv[1:]
p=subprocess.Popen([sys.executable,'-c',"import time,sys; from pathlib import Path; p=Path(sys.argv[1]); n=0\\nwhile True: n+=1;p.write_text(str(n));time.sleep(.03)",child])
print(p.pid,flush=True)
n=0
while True: n+=1;Path(root).write_text(str(n));time.sleep(.03)
'''
            rootfile, childfile = directory / 'root', directory / 'child'
            proc = subprocess.Popen([sys.executable, '-c', script, str(rootfile), str(childfile)], stdout=subprocess.PIPE, text=True)
            child = int(proc.stdout.readline())
            rootpid, childpid = str(proc.pid), str(child)
            power = Power()
            def scan():
                table = {pid: guard.native().get(int(pid)) for pid in (rootpid, childpid)}
                for pid, value in table.items():
                    if pid == rootpid:
                        value = value.copy()
                        value['path'] = str(guard.HOME / '.local/share/claude/versions/isolated-test')
                        table[pid] = value
                return table
            def lookup(pid):
                return scan().get(str(pid))
            controller = guard.Guard(power, directory / 'state.json', scanner=scan, lookup=lookup)
            try:
                for _ in range(100):
                    if rootfile.exists() and childfile.exists():
                        break
                    time.sleep(.01)
                power.value = snapshot(cpu=False, graphics=False, wake=None, user=None)
                controller.tick()
                time.sleep(.1)
                before = (rootfile.read_text(), childfile.read_text())
                time.sleep(.2)
                self.assertEqual(before, (rootfile.read_text(), childfile.read_text()))
                self.assertTrue(guard.native().get(proc.pid)['stopped'])
                self.assertTrue(guard.native().get(child)['stopped'])
                restarted = guard.Guard(power, directory / 'state.json', scanner=scan, lookup=lookup)
                restarted.tick()
                self.assertTrue(guard.native().get(child)['stopped'])
                power.value = snapshot()
                restarted.tick()
                time.sleep(.15)
                self.assertNotEqual(before[0], rootfile.read_text())
                self.assertNotEqual(before[1], childfile.read_text())
            finally:
                for pid in (proc.pid, child):
                    try:
                        os.kill(pid, signal.SIGCONT)
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                proc.wait(timeout=5)
                proc.stdout.close()


if __name__ == '__main__':
    unittest.main()
