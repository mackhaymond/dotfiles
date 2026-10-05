"""Exercise full cache refreshes with fake CodexBar/tmux and an isolated HOME."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


def window(percent, minutes=None):
    result = {'usedPercent': percent, 'resetsAt': '2030-01-01T00:00:00Z'}
    if minutes is not None:
        result['windowMinutes'] = minutes
    return result


class CodexWindowsTests(unittest.TestCase):
    def refresh(self, payload):
        scripts = Path(__file__).resolve().parents[1] / 'scripts'
        source = scripts / 'codexbar-usage-status.sh'
        if not source.exists():
            source = scripts / 'executable_codexbar-usage-status.sh'
        with tempfile.TemporaryDirectory(prefix='codex-windows-') as directory:
            root = Path(directory)
            tools = root / 'bin'
            tools.mkdir()
            for name, code in {
                'codexbar': 'cat "$CODEX_WINDOW_FIXTURE"',
                'codex': 'exit 0',
                'tmux': 'exit 0',
            }.items():
                stub = tools / name
                stub.write_text('#!/bin/sh\n' + code + '\n')
                stub.chmod(0o700)
            fixture = root / 'fixture.json'
            fixture.write_text(json.dumps([{'provider': 'codex', **payload}]))
            cache = root / '.cache/codexbar-tmux'
            cache.mkdir(parents=True)
            # Refresh must clear old meters when the account loses a window.
            old = dict(state='ok', updated_at=1, session_used=99,
                       weekly_used=99, scoped_used=99)
            summary = cache / 'usage.json'
            summary.write_text(json.dumps({'schema': 2, 'providers': {'codex': old}}))
            result = subprocess.run(
                ['/bin/bash', str(source), '--refresh'], capture_output=True,
                text=True, timeout=15, env={**os.environ, 'HOME': directory,
                    'PATH': str(tools) + ':/opt/homebrew/bin:/usr/bin:/bin',
                    'CODEX_WINDOW_FIXTURE': str(fixture),
                    'CODEX_CLI_PATH': str(tools / 'codex'),
                    'CODEXBAR_USAGE_PROVIDERS': 'codex',
                    'CODEXBAR_USAGE_FORCE_REFRESH': '1'})
            self.assertEqual(result.returncode, 0, result.stderr)
            block = json.loads(summary.read_text())['providers']['codex']
            history = cache / 'usage-history-codex.jsonl'
            samples = [json.loads(line) for line in history.read_text().splitlines()] \
                if history.exists() else []
            return block, samples

    def test_reported_windows_are_not_duplicated(self):
        short, week = window(7, 300), window(29, 10080)
        reserve = {'title': 'gpt-reserve', 'window': window(10, 10080)}
        cases = [
            ({'primary': None, 'secondary': week, 'extraRateWindows': [reserve]},
             (None, 29, 10)),
            ({'primary': week, 'secondary': None}, (None, 29, None)),
            ({'primary': short, 'secondary': week}, (7, 29, None)),
            ({'primary': week, 'secondary': short}, (7, 29, None)),
            ({'primary': short, 'secondary': None}, (7, None, None)),
            ({'primary': window(7), 'secondary': window(29)}, (7, 29, None)),
            ({'primary': None, 'secondary': window(29)}, (None, 29, None)),
            ({'primary': window(0, 300), 'secondary': week}, (0, 29, None)),
        ]
        for usage, expected in cases:
            with self.subTest(usage=usage):
                block, samples = self.refresh({'usage': usage, 'credits': {'remaining': 0}})
                self.assertEqual(block['state'], 'ok')
                self.assertEqual(tuple(block[f'{f}_used'] for f in
                                       ('session', 'weekly', 'scoped')), expected)
                self.assertEqual(block['credits_remaining'], 0)
                self.assertEqual(len(samples), 1)
                self.assertEqual(tuple(samples[0][f] for f in ('s', 'w', 'sc')), expected)
                for family, value in zip(('session', 'weekly', 'scoped'), expected):
                    if value is None:
                        self.assertIsNone(block[f'{family}_window_minutes'])
                        self.assertIsNone(block[f'{family}_resets_at'])
                        self.assertEqual(block[f'{family}_text'], 'n/a')
                if expected[2] is not None:
                    self.assertEqual(block['scoped_label'], 'gpt-reserve')

    def test_legacy_dashboard_windows(self):
        block, samples = self.refresh({'usage': {}, 'openaiDashboard': {
            'primaryLimit': window(29, 10080)}})
        self.assertEqual(block['state'], 'ok')
        self.assertIsNone(block['session_used'])
        self.assertEqual(block['weekly_used'], 29)
        self.assertEqual(samples[0]['w'], 29)

    def test_unusable_payload_retains_previous_numbers_as_error(self):
        for usage in ({'primary': None, 'secondary': None},
                      {'primary': window('invalid', 300), 'secondary': window(29, 10080)}):
            with self.subTest(usage=usage):
                block, samples = self.refresh({'usage': usage})
                self.assertEqual(block['state'], 'error')
                self.assertEqual(block['updated_at'], 1)
                self.assertEqual(block['session_used'], 99)
                self.assertEqual(block['weekly_used'], 99)
                self.assertEqual(samples, [])


if __name__ == '__main__':
    unittest.main()
