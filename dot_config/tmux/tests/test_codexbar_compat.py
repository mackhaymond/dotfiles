"""Exercise the actual adapter with a fake CLI; no account or tmux access."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class CodexBarCompatibilityTests(unittest.TestCase):
    def test_only_legacy_read_only_usage_probe_changes(self):
        scripts = Path(__file__).resolve().parents[1] / 'scripts'
        adapter = scripts / 'codexbar-codex-compat.sh'
        if not adapter.exists():
            adapter = scripts / 'executable_codexbar-codex-compat.sh'
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / 'fake codex'
            fake.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
            fake.chmod(0o700)
            cases = [
                (['-s', 'read-only', '-a', 'untrusted', 'app-server'],
                 ['-s', 'read-only', '-a', 'on-request', 'app-server']),
                (['--version'], ['--version']),
                (['-s', 'read-only', '-a', 'never', 'app-server'],
                 ['-s', 'read-only', '-a', 'never', 'app-server']),
                (['-s', 'read-only', '-a', 'untrusted', 'exec'],
                 ['-s', 'read-only', '-a', 'untrusted', 'exec']),
                (['prompt with spaces', '$(literal)'],
                 ['prompt with spaces', '$(literal)']),
            ]
            for incoming, expected in cases:
                with self.subTest(args=incoming):
                    result = subprocess.run(
                        ['/bin/bash', str(adapter), *incoming], check=True,
                        capture_output=True, text=True,
                        env={**os.environ, 'CODEXBAR_CODEX_REAL_CLI': str(fake)})
                    self.assertEqual(json.loads(result.stdout), expected)


if __name__ == '__main__':
    unittest.main()
