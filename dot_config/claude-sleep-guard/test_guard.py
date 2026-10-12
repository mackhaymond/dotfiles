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


def snapshot(cpu=True, graphics=True, closed=False, hold=False, override=False):
    return {'cpu': cpu, 'graphics': graphics, 'lidClosed': closed,
            'liveAwakeHold': hold, 'clamshellOverride': override}


class Power:
    def __init__(self):
        self.value = snapshot()
    def snapshot(self):
        return self.value.copy()


class PolicyTests(unittest.TestCase):
    def test_all_closed_lid_incidental_wakes_are_denied_without_Awake(self):
        # Categories/counts from the inspected pmset log, including Full
        # Notification (which can include graphics and fooled the old policy).
        categories = {'wifibt': 144, 'SleepService': 18, 'Maintenance': 6, 'Full Notification': 2}
        for category, count in categories.items():
            for _ in range(count):
                with self.subTest(category=category):
                    self.assertFalse(guard.allowed(snapshot(closed=True, graphics=category == 'Full Notification')))

    def test_closed_headless_Awake_is_intentional(self):
        self.assertTrue(guard.allowed(snapshot(closed=True, graphics=False, hold=True, override=True)))
        for cpu, hold, override in ((False, True, True), (True, False, True), (True, True, False)):
            self.assertFalse(guard.allowed(snapshot(cpu=cpu, closed=True, hold=hold, override=override)))

    def test_lid_open_requires_full_capability_but_not_display_on(self):
        self.assertTrue(guard.allowed(snapshot()))
        self.assertFalse(guard.allowed(snapshot(graphics=False)))
        self.assertFalse(guard.allowed(snapshot(cpu=False)))
        self.assertFalse(guard.allowed({}))
        self.assertFalse(guard.allowed(snapshot(closed=None)))

    def test_hook_uses_the_same_policy(self):
        power = Power()
        with patch.object(guard, 'Power', return_value=power):
            for value in (snapshot(), snapshot(closed=True), snapshot(closed=True, graphics=False, hold=True, override=True)):
                power.value = value
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    guard.hook()
                if guard.allowed(value):
                    self.assertEqual(output.getvalue(), '')
                else:
                    self.assertFalse(json.loads(output.getvalue())['continue'])

    def test_hook_native_failure_fails_closed(self):
        with patch.object(guard, 'Power', side_effect=RuntimeError), contextlib.redirect_stdout(io.StringIO()) as output:
            guard.hook()
        self.assertFalse(json.loads(output.getvalue())['continue'])

    def test_Awake_preference_defaults_and_normalization(self):
        self.assertEqual(guard.awake_lid_preferences(None, None), (True, 20))
        self.assertEqual(guard.awake_lid_preferences(False, 30), (False, 30))
        self.assertEqual(guard.awake_lid_preferences(1, 50), (True, 50))
        self.assertEqual(guard.awake_lid_preferences(0, 10), (False, 10))
        for bad in ('false', [], 2):
            self.assertFalse(guard.awake_lid_preferences(bad, 20)[0])
        for bad in (None, True, '30', 25, 0):
            self.assertEqual(guard.awake_lid_preferences(True, bad)[1], 20)

    def test_battery_floor_strict_boundary_AC_and_unknown(self):
        for floor in (10, 20, 30, 50):
            for percent, expected in ((floor - 1, False), (floor, False), (floor + 1, True)):
                self.assertEqual(guard.headless_eligible(True, floor, {'onAC': False, 'batteryPercent': percent}), expected)
        self.assertTrue(guard.headless_eligible(True, 50, {'onAC': True, 'batteryPercent': 1}))
        self.assertTrue(guard.headless_eligible(True, 20, {'onAC': True, 'batteryPercent': None}))
        self.assertFalse(guard.headless_eligible(False, 20, {'onAC': True, 'batteryPercent': 100}))
        for unknown in ({}, {'onAC': None, 'batteryPercent': 100}, {'onAC': False, 'batteryPercent': None}, {'onAC': False, 'batteryPercent': 101}):
            self.assertFalse(guard.headless_eligible(True, 20, unknown))

    def test_stale_published_override_is_vetoed_by_floor_or_disabled_setting(self):
        power = guard.Power.__new__(guard.Power)
        def assertions(pointer):
            guard.C.cast(pointer, guard.C.POINTER(guard.C.c_void_p))[0] = 1
            return 0
        power.io = SimpleNamespace(IOPMCopyAssertionsByProcess=assertions)
        power.cf = SimpleNamespace(CFRelease=lambda value: None)
        power.decode = lambda value: {99: [{'AssertName': 'com.mackhaymond.Awake: Manual hold', 'AssertLevel': 1, 'AssertType': 'PreventUserIdleSystemSleep'}]}
        power.property = lambda name: {'System Capabilities': 1, 'AppleClamshellState': True, 'AppleClamshellCausesSleep': False}[name]
        with patch.object(guard, 'native', return_value=SimpleNamespace(path=lambda pid: '/Applications/Awake.app/Contents/MacOS/Awake')):
            for enabled, onAC, percent, expected in ((True, False, 19, False), (True, False, 20, False), (True, False, 21, True), (True, True, 1, True), (False, True, 100, False), (True, None, 100, False)):
                power.awake_preferences = lambda: (enabled, 20)
                power.power_status = lambda: {'onAC': onAC, 'batteryPercent': percent}
                actual = power.snapshot()
                self.assertTrue(actual['clamshellPublishedOverride'])
                self.assertTrue(actual['liveAwakeHold'])
                self.assertEqual(actual['headlessEligible'], expected)
                self.assertEqual(guard.allowed(actual), expected)

    def test_assertion_identity_name_level_type(self):
        app = '/Applications/Awake.app/Contents/MacOS/Awake'
        entry = {'AssertName': 'com.mackhaymond.Awake: Manual hold', 'AssertLevel': 1, 'AssertType': 'PreventUserIdleSystemSleep'}
        self.assertTrue(guard.qualified_awake({99: [entry]}, lambda pid: app))
        self.assertTrue(guard.qualified_awake({99: [{**entry, 'AssertName': 'com.mackhaymond.Awake: Timed hold (15m)'}]}, lambda pid: app))
        for altered in ({'AssertName': 'com.mackhaymond.Awake: Launch hold'}, {'AssertLevel': 0}, {'AssertType': 'OtherAssertion'}):
            self.assertFalse(guard.qualified_awake({99: [{**entry, **altered}]}, lambda pid: app))
        self.assertFalse(guard.qualified_awake({99: [entry]}, lambda pid: '/tmp/Awake'))
        self.assertFalse(guard.qualified_awake({99: [entry]}, lambda pid: '/Applications/Other.app/Contents/MacOS/Other'))
        self.assertFalse(guard.qualified_awake({99: []}, lambda pid: app))
        self.assertTrue(guard.qualified_awake({99: [{**entry, 'AssertType': 'PreventUserIdleDisplaySleep'}]}, lambda pid: app))

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
        self.now = 0
        self.controller = self.create()
    def tearDown(self):
        self.temp.cleanup()
    def create(self):
        return guard.Guard(self.power, self.path, scanner=lambda: {pid: proc.copy() for pid, proc in self.table.items()},
                           lookup=lambda pid: self.table.get(str(pid)), kill=self.kill, clock=lambda: self.now)
    def kill(self, pid, sig):
        self.signals.append((str(pid), sig))
        self.table[str(pid)]['stopped'] = sig == signal.SIGSTOP
    def deny(self):
        self.power.value = snapshot(closed=True)
        self.controller.tick()

    def test_pause_root_and_children_and_resume_on_intent(self):
        self.deny()
        self.assertEqual(set(self.controller.state['held']), {'991', '992'})
        self.assertTrue(all(proc['stopped'] for proc in self.table.values()))
        self.power.value = snapshot(closed=True, graphics=False, hold=True, override=True)
        self.controller.tick()
        self.assertFalse(any(proc['stopped'] for proc in self.table.values()))

    def test_Awake_off_expiry_and_kernel_override_floor(self):
        self.power.value = snapshot(closed=True, graphics=False, hold=True, override=True)
        self.controller.tick()
        self.assertEqual(self.signals, [])
        self.power.value['liveAwakeHold'] = False
        self.controller.tick()
        self.assertTrue(self.table['991']['stopped'])
        self.power.value['liveAwakeHold'] = True
        self.power.value['clamshellOverride'] = False
        self.controller.tick()
        self.assertTrue(self.table['991']['stopped'])

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
        self.power.value = snapshot(closed=True)
        with patch.object(self.controller.journal, 'save', side_effect=OSError):
            with self.assertRaises(OSError):
                self.controller.tick()
        self.assertEqual(self.signals, [])

    def test_failed_mirror_write_is_retried_before_any_later_stop(self):
        self.power.value = snapshot(closed=True)
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
        self.controller.clear_transition()
        self.controller.tick()
        self.assertFalse(self.table['991']['stopped'])

    def test_missed_transition_expires_and_target_wake_does_not_thaw_early(self):
        self.controller.transition(snapshot(cpu=False, graphics=False))
        self.now = 11
        self.controller.tick()
        self.assertFalse(self.table['991']['stopped'])
        self.deny()
        self.controller.transition(snapshot())
        self.assertTrue(self.table['991']['stopped'])

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

    def test_target_caps_uses_shared_CPU_video_bits(self):
        power = guard.Power.__new__(guard.Power)
        power.snapshot = lambda: snapshot(closed=True, hold=True, override=True)
        seen, tokens = [], []
        class Controller:
            def transition(self, value):
                seen.append(value)
        power.target_event(Controller(), 12, 1 | 16, tokens.append)
        self.assertTrue(guard.allowed(seen[0]))
        self.assertFalse(seen[0]['graphics'])
        self.assertEqual(tokens, [12])

    def test_legacy_ack_and_cancel(self):
        power = guard.Power.__new__(guard.Power)
        power.snapshot = lambda: snapshot()
        class Controller:
            def __init__(self):
                self.target = None
                self.cleared = False
            def transition(self, value):
                self.target = value
            def clear_transition(self):
                self.cleared = True
            def tick(self):
                pass
        controller = Controller()
        ack = []
        power.legacy_event(controller, guard.WILL_SLEEP, 8, ack.append)
        self.assertFalse(controller.target['cpu'])
        self.assertEqual(ack, [8])
        power.legacy_event(controller, guard.WILL_NOT_SLEEP, 0, ack.append)
        self.assertTrue(controller.cleared)

    def test_connection_Awake_headless_is_not_overridden_by_legacy_sleep(self):
        power = guard.Power.__new__(guard.Power)
        power.backend = 'connection'
        power.snapshot = lambda: snapshot(closed=True, graphics=False, hold=True, override=True)
        with tempfile.TemporaryDirectory() as temp:
            controller = guard.Guard(power, Path(temp) / 'state.json', scanner=lambda: {}, lookup=lambda pid: None)
            power.target_event(controller, 42, 1, lambda token: None)
            power.legacy_event(controller, guard.WILL_SLEEP, 88, lambda token: None)
            self.assertTrue(controller.policy(power.snapshot()))
            self.assertIsNone(controller.target)


@unittest.skipUnless(sys.platform == 'darwin', 'native macOS APIs')
class NativeTests(unittest.TestCase):
    def test_native_identity_and_power_snapshot_read_only(self):
        proc = guard.native().get(os.getpid())
        self.assertTrue(Path(proc['path']).is_file())
        self.assertTrue(guard.safe_descendant(proc['path']))
        self.assertFalse(proc['stopped'])
        value = guard.Power().snapshot()
        self.assertIn(value['lidClosed'], (True, False, None))
        self.assertIsInstance(value['liveAwakeHold'], bool)

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
                def clear_transition(self):
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
                power.value = snapshot(closed=True)
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
                power.value = snapshot(closed=True, graphics=False, hold=True, override=True)
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
