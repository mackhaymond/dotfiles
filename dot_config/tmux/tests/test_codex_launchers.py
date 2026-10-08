"""Codex resume commands name the ~/.local/bin wrapper and survive odd HOMEs.

The closed-tab reopen (closed-tabs.sh build_cmd) and post-restore resume
(assistant-restore.sh) type a command line into a pane shell. Both must run
$HOME/.local/bin/codex when it exists, so a PATH with ~/.bun/bin first cannot
skip --no-daemon, and fall back to `command codex` otherwise. The code under
test is cut from the scripts and run against a fake wrapper in a temp HOME
whose name needs quoting; PATH holds only fakes and /usr/bin:/bin, so the real
Codex and the real tmux server are never reached.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
LIB_DETECT = Path.home() / '.config/tmux/plugins/tmux-assistant-resurrect/scripts/lib-detect.sh'
# The bash `#!/usr/bin/env bash` finds. The plugin's posix_quote mangles an
# apostrophe under macOS's bash 3.2 (backslashes survive ${//} replacement),
# so the apostrophe is only part of HOME when this bash is 4 or later.
BASH = shutil.which('bash')
MODERN_BASH = bool(BASH) and subprocess.run(
    [BASH, '-c', '((BASH_VERSINFO[0] >= 4))'], capture_output=True).returncode == 0

# Run each script's own code: closed-tabs' build_cmd function, and
# assistant-restore's codex_cmd lines, JQ program and rows= invocation.
HARNESS = r'''
set -u
mode=$1
if [ "$mode" = closed-tabs ]; then
    eval "$(awk '/^build_cmd\(\) \{/{f=1} f{print} f&&/^\}/{exit}' "$SCRIPTS/executable_closed-tabs.sh")"
    build_cmd codex s-1 "${CLI_ARGS---no-daemon -c k=v}" "" "{}"
else
    source "$LIB_DETECT"
    log() { echo "log: $*" >&2; }
    src="$SCRIPTS/executable_assistant-restore.sh"
    eval "$(awk "/^read -r -d '' JQ_PROG <<'JQ'\$/{f=1} f{print} f&&/^JQ\$/{exit}" "$src")"
    eval "$(grep -E '^(\[ -x "\$HOME/.local/bin/codex" \] && )?codex_cmd=' "$src")"
    capture_json='[]'
    eval "$(grep -E '^rows=' "$src")"
    printf '%s\n' "$rows" | awk -F '\037' '{print $5}'
fi
'''


@unittest.skipUnless(LIB_DETECT.exists() and BASH and shutil.which('jq') and shutil.which('zsh'),
                     'needs the tmux-assistant-resurrect plugin, jq and zsh')
class CodexLauncherTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cx-launch-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.home = root / ("it's" if MODERN_BASH else 'its') / 'a "[home]" dir $x *'
        (self.home / '.local/bin').mkdir(parents=True)
        self.wrapper = self.home / '.local/bin/codex'
        self.write(self.wrapper, '#!/bin/sh\nprintf "<%s>" "$0" "$@"; echo\n')
        fakes, bare = root / 'fakes', root / 'bare'
        fakes.mkdir()
        bare.mkdir()
        self.write(fakes / 'tmux', '#!/bin/sh\nexit 0\n')  # show-option prints nothing
        os.symlink(shutil.which('jq'), fakes / 'jq')
        self.write(bare / 'codex', '#!/bin/sh\nprintf "<bare>"; printf "<%s>" "$@"; echo\n')
        self.harness = root / 'harness.sh'
        self.harness.write_text(HARNESS)
        self.sidecar = root / 'sessions.json'
        self.env = {'HOME': str(self.home), 'PATH': f'{fakes}:{bare}:/usr/bin:/bin', 'SCRIPTS': str(SCRIPTS),
                    'LIB_DETECT': str(LIB_DETECT), 'INPUT_FILE': str(self.sidecar), 'JQ_PROG': '',
                    'RESURRECT_SAVE': str(LIB_DETECT.with_name('save-assistant-sessions.sh'))}
        self.saved('--no-daemon -c k=v')

    def saved(self, cli_args, tool='codex'):
        """The cli_args both scripts read: the sidecar row and build_cmd's argument."""
        self.sidecar.write_text(json.dumps({'sessions': [
            {'pane': 's:1.1', 'tool': tool, 'session_id': 's-1', 'cli_args': cli_args}]}))
        self.env['CLI_ARGS'] = cli_args

    @staticmethod
    def write(path, text):
        path.write_text(text)
        path.chmod(0o755)

    def command(self, script):
        result = subprocess.run([BASH, str(self.harness), script], env=self.env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, result.stdout + result.stderr)
        return lines[0]

    def run_typed(self, line, shell):
        # What the pane shell does with the typed line.
        argv = ['zsh', '-f', '-c', line] if shell == 'zsh' else [BASH, '-c', line]
        result = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_resume_runs_the_wrapper_by_path_under_a_quoting_hostile_home(self):
        expected = f'<{self.wrapper}><--no-daemon><-c><k=v><resume><s-1>'
        for script in ('closed-tabs', 'assistant-restore'):
            line = self.command(script)
            self.assertNotIn('command codex', line)
            for shell in ('bash', 'zsh'):
                with self.subTest(script=script, shell=shell):
                    self.assertEqual(self.run_typed(line, shell), expected)

    def resumed(self, script, cli_args):
        """argv the typed resume line runs, after checking bash and zsh agree."""
        self.saved(cli_args)
        line = self.command(script)
        self.assertNotIn('command codex', line)
        outputs = {shell: self.run_typed(line, shell) for shell in ('bash', 'zsh')}
        self.assertEqual(outputs['bash'], outputs['zsh'])
        return outputs['bash']

    def test_resume_drops_stale_context_overrides_so_the_wrapper_adds_one(self):
        # The wrapper prepends a fresh `-c model_context_window=<max>`; a saved
        # copy would follow it and win, one more per close/reopen or (when the
        # save repair could not rewrite the sidecar) per restore.
        stale = ('--no-daemon -c model_context_window=272000 -c k=v --config=model_context_window=1 '
                 '-cmodel_context_window=2 -c=model_context_window=3 --config model_context_window=4 '
                 '-c model_context_window_x=5 -cfoo=1')
        for script in ('closed-tabs', 'assistant-restore'):
            for cli_args, kept in ((stale, '<--no-daemon><-c><k=v><-c><model_context_window_x=5><-cfoo=1>'),
                                   ('-c model_context_window=1', ''), ('', '')):
                with self.subTest(script=script, cli_args=cli_args):
                    self.assertEqual(self.resumed(script, cli_args), f'<{self.wrapper}>{kept}<resume><s-1>')

    def test_resume_filter_keeps_every_flag_operand_and_stops_at_double_dash(self):
        # Value-taking flags (VALUE_FLAGS in resurrect-save-repair.py) keep
        # their operand whatever it looks like; nothing after `--` is touched.
        cases = (
            # A dangling -c takes the next -c as its value: nothing swallows resume.
            ('--no-daemon -c -c model_context_window=1', '<--no-daemon><-c><-c><model_context_window=1>'),
            ('-m -c model_context_window=1 x', '<-m><-c><model_context_window=1><x>'),
            ('--profile -cmodel_context_window=1 -c model_context_window=2', '<--profile><-cmodel_context_window=1>'),
            ('--no-daemon -- -c model_context_window=1', '<--no-daemon><--><-c><model_context_window=1>'),
            ('-c model_context_window=1 --add-dir --config=model_context_window=2',
             '<--add-dir><--config=model_context_window=2>'),
            ('--no-daemon -c', '<--no-daemon><-c>'),
        )
        for script in ('closed-tabs', 'assistant-restore'):
            for cli_args, kept in cases:
                with self.subTest(script=script, cli_args=cli_args):
                    self.assertEqual(self.resumed(script, cli_args), f'<{self.wrapper}>{kept}<resume><s-1>')

    def test_other_tools_keep_their_arguments(self):
        # Only Codex rows are filtered (the closed-tabs harness builds Codex only).
        self.saved('-c model_context_window=1', tool='opencode')
        self.assertIn("command opencode '-c' 'model_context_window=1' -s", self.command('assistant-restore'))

    def test_resume_falls_back_to_path_lookup_without_the_wrapper(self):
        self.wrapper.unlink()
        for script in ('closed-tabs', 'assistant-restore'):
            line = self.command(script)
            self.assertTrue(line.startswith('command codex '), line)
            for shell in ('bash', 'zsh'):
                with self.subTest(script=script, shell=shell):
                    self.assertEqual(self.run_typed(line, shell), '<bare><--no-daemon><-c><k=v><resume><s-1>')


if __name__ == '__main__':
    unittest.main()
