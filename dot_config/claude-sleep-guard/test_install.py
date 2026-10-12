"""Installer rollback and managed-hook removal, entirely in temporary folders."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[2]


def render(relative):
    return subprocess.check_output(['chezmoi', 'execute-template'],
                                   input=(SOURCE / relative).read_bytes()).decode()


FAKE_LAUNCHCTL = '''#!/usr/bin/python3
import json, os, pathlib, plistlib, sys
root = pathlib.Path(os.environ['GUARD_TEST_ROOT'])
marker = root / 'running'
action = sys.argv[1]
if action == 'print':
    if not marker.exists(): sys.exit(1)
    print('state = running\\npid = 12345\\nprogram = ' + marker.read_text())
elif action == 'bootout':
    if not marker.exists(): sys.exit(1)
    marker.unlink()
elif action == 'bootstrap':
    plist = plistlib.loads(pathlib.Path(sys.argv[3]).read_bytes())
    runtime = plist['ProgramArguments'][1]
    previous = '/previous/' in runtime
    with (root / 'attempts').open('a') as stream: stream.write(runtime + '\\n')
    if not previous and os.environ.get('GUARD_TEST_HOLDS'):
        state = root / '.local/state/claude-sleep-guard/state.json'
        state.parent.mkdir(parents=True, exist_ok=True)
        value = pathlib.Path(os.environ['GUARD_TEST_HOLDS']).read_text()
        state.write_text(value)
        state.with_name('state.json.backup').write_text(value)
    if not previous and os.environ.get('GUARD_TEST_FAIL') == 'bootstrap': sys.exit(1)
    marker.write_text(runtime)
    if previous or os.environ.get('GUARD_TEST_FAIL') != 'readiness':
        ready = root / '.local/state/claude-sleep-guard/ready.json'
        ready.parent.mkdir(parents=True, exist_ok=True)
        ready.write_text(json.dumps({'pid':12345, 'runtime':runtime, 'backend':'test'}))
else: sys.exit(2)
'''


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        command = self.bin / 'launchctl'
        command.write_text(FAKE_LAUNCHCTL)
        command.chmod(0o755)
        self.env = dict(os.environ, GUARD_TEST_ROOT=str(self.root),
                        PATH=str(self.bin) + ':' + os.environ['PATH'])
        config = self.root / '.config/claude-sleep-guard'
        config.mkdir(parents=True)
        code = (SOURCE / 'dot_config/claude-sleep-guard/guard.py').read_bytes()
        (config / 'guard.py').write_bytes(code)
        self.plist = self.root / 'Library/LaunchAgents/com.mackhaymond.claude-sleep-guard.plist'
        self.plist.parent.mkdir(parents=True)
        self.previous_runtime = str(self.root / '.local/state/claude-sleep-guard/versions/previous/guard.py')
        old = {'Label':'com.mackhaymond.claude-sleep-guard',
               'ProgramArguments':['/usr/bin/python3', self.previous_runtime, 'daemon']}
        self.previous = plistlib.dumps(old)
        self.plist.write_bytes(self.previous)
        (self.root / 'running').write_text(self.previous_runtime)
        (config / 'launchagent.plist').write_text(
            render('dot_config/claude-sleep-guard/launchagent.plist.tmpl').replace(str(Path.home()), str(self.root)))
        self.script = render('.chezmoiscripts/run_onchange_after_zz-claude-sleep-guard.sh.tmpl').replace(str(Path.home()), str(self.root))

    def tearDown(self):
        self.temp.cleanup()

    def run_install(self, failure=''):
        return subprocess.run(['/bin/sh'], input=self.script, text=True,
                              env=dict(self.env, GUARD_TEST_FAIL=failure),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)

    def test_success_pins_immutable_runtime_and_waits_for_native_readiness(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = plistlib.loads(self.plist.read_bytes())
        runtime = Path(plist['ProgramArguments'][1])
        self.assertEqual(hashlib.sha256(runtime.read_bytes()).hexdigest(), runtime.parent.name)
        self.assertEqual((self.root / 'running').read_text(), str(runtime))

    def test_bootstrap_failure_restores_previous_runtime(self):
        result = self.run_install('bootstrap')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.plist.read_bytes(), self.previous)
        self.assertEqual((self.root / 'running').read_text(), self.previous_runtime)

    def test_readiness_failure_restores_previous_runtime(self):
        result = self.run_install('readiness')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.plist.read_bytes(), self.previous)
        self.assertEqual((self.root / 'running').read_text(), self.previous_runtime)

    def test_corrupt_plist_leaves_existing_service_running(self):
        (self.root / '.config/claude-sleep-guard/launchagent.plist').write_text('broken')
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.plist.read_bytes(), self.previous)
        self.assertEqual((self.root / 'running').read_text(), self.previous_runtime)

    def test_source_mismatch_leaves_existing_service_running(self):
        (self.root / '.config/claude-sleep-guard/guard.py').write_text('different source')
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / 'running').read_text(), self.previous_runtime)

    def test_mismatched_reinstall_cannot_overwrite_active_version(self):
        self.assertEqual(self.run_install().returncode, 0)
        runtime = Path((self.root / 'running').read_text())
        expected = runtime.read_bytes()
        (self.root / '.config/claude-sleep-guard/guard.py').write_text('different source')
        self.assertNotEqual(self.run_install().returncode, 0)
        self.assertEqual(runtime.read_bytes(), expected)
        self.assertEqual((self.root / 'running').read_text(), str(runtime))

    def legacy_owned_child(self):
        legacy = Path(self.previous_runtime)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.with_name('.legacy').write_text('legacy')
        child = subprocess.Popen([sys.executable, '-c', 'import time; print("ready",flush=True); time.sleep(30)'],
                                 stdout=subprocess.PIPE, text=True)
        self.assertEqual(child.stdout.readline().strip(), 'ready')
        def cleanup():
            if child.poll() is None:
                os.kill(child.pid, signal.SIGCONT)
                child.terminate()
            child.wait(timeout=5)
            child.stdout.close()
        self.addCleanup(cleanup)
        spec = importlib.util.spec_from_file_location('installer_test_guard', SOURCE / 'dot_config/claude-sleep-guard/guard.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        proc = module.process(child.pid)
        self.assertIsNotNone(proc)
        os.kill(child.pid, signal.SIGSTOP)
        fixture = self.root / 'held-fixture.json'
        fixture.write_text(json.dumps({'generation':4,'held':{str(child.pid):{'start':proc['start'],'path':proc['path']}}}))
        self.env['GUARD_TEST_HOLDS'] = str(fixture)
        return child, module

    def test_legacy_rollback_releases_candidate_owned_child_when_allowed(self):
        child, module = self.legacy_owned_child()
        self.script = self.script.replace('if not guard.allowed(guard.Power().snapshot()):', 'if False:')
        self.assertNotEqual(self.run_install('bootstrap').returncode, 0)
        self.assertEqual((self.root / 'running').read_text(), self.previous_runtime)
        state = self.root / '.local/state/claude-sleep-guard/state.json'
        self.assertEqual(json.loads(state.read_text())['held'], {})
        self.assertFalse(module.process(child.pid)['stopped'])

    def test_legacy_rollback_preserves_hold_and_removes_autoload_when_denied(self):
        child, module = self.legacy_owned_child()
        self.script = self.script.replace('if not guard.allowed(guard.Power().snapshot()):', 'if True:')
        result = self.run_install('bootstrap')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Legacy rollback blocked', result.stderr)
        self.assertFalse(self.plist.exists())
        self.assertFalse((self.root / 'running').exists())
        self.assertTrue(module.process(child.pid)['stopped'])
        state = self.root / '.local/state/claude-sleep-guard/state.json'
        self.assertIn(str(child.pid), json.loads(state.read_text())['held'])

    def test_legacy_wrapper_is_pinned_before_live_source_changes(self):
        wrapper = {'Label':'com.mackhaymond.claude-sleep-guard',
                   'ProgramArguments':[str(self.root / '.local/bin/claude-sleep-guard'),'daemon']}
        self.plist.write_bytes(plistlib.dumps(wrapper))
        (self.root / 'running').write_text(str(self.root / '.local/bin/claude-sleep-guard'))
        original = (self.root / '.config/claude-sleep-guard/guard.py').read_bytes()
        script = render('.chezmoiscripts/run_once_before_zz-pin-legacy-sleep-guard.py.tmpl').replace(str(Path.home()),str(self.root))
        result = subprocess.run(['/usr/bin/python3', '-c', script], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        runtime = Path(plistlib.loads(self.plist.read_bytes())['ProgramArguments'][1])
        (self.root / '.config/claude-sleep-guard/guard.py').write_text('new code')
        self.assertEqual(runtime.read_bytes(), original)
        self.assertTrue(runtime.with_name('.legacy').exists())
        self.assertEqual((self.root / 'running').read_text(), str(runtime))


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.script = render('dot_claude/modify_settings.json.tmpl')

    def apply(self, script, live):
        result = subprocess.run(['/bin/sh', '-c', script], input=json.dumps(live), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return json.loads(result.stdout)

    def test_removed_guard_event_does_not_survive_managed_hook_replacement(self):
        live = self.apply(self.script, {})
        self.assertIn('PostToolBatch', live['hooks'])
        prefix, body = self.script.split("FULL=$(cat <<'EOF'\n", 1)
        payload, suffix = body.split('\nEOF\n)', 1)
        desired = json.loads(payload)
        desired['hooks'].pop('PostToolBatch')
        script = prefix + "FULL=$(cat <<'EOF'\n" + json.dumps(desired) + '\nEOF\n)' + suffix
        self.assertNotIn('PostToolBatch', self.apply(script, live)['hooks'])

    def test_preserves_unmanaged_preferences_and_replaces_managed_keys(self):
        result = self.apply(self.script, {'model':'custom-model','theme':'light',
                                        'personalSetting':{'keep':True},
                                        'hooks':{'ObsoleteEvent':[]},
                                        'permissions':{'obsolete':'old'}})
        self.assertEqual(result['model'], 'custom-model')
        self.assertEqual(result['theme'], 'light')
        self.assertEqual(result['personalSetting'], {'keep':True})
        self.assertNotIn('ObsoleteEvent', result['hooks'])
        self.assertNotIn('obsolete', result['permissions'])


if __name__ == '__main__':
    unittest.main()
