"""Isolated transport/ownership regressions; never writes to a real tmux server."""
import contextlib
import importlib.util
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('terminal_owner', Path(__file__).resolve().parents[1] / 'scripts/codex-terminal-owner.py')
owner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(owner)


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cx-owner-test-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root_patch = patch.object(owner, 'ROOT', self.root / 'registry')
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.identity = dict(frontend_pid=42, frontend_start='Thu Oct 1 12:00:00 2026',
                             tty='ttys111', pane='%1', pane_pid=41, tmux_socket='/tmp/test-socket', token='owner-a')

    def test_pid_reuse_and_deleted_pane_are_rejected(self):
        proc = {k: self.identity[k] for k in ('frontend_pid', 'frontend_start', 'tty')}
        pane = dict(pane='%1', tty='ttys111', pane_pid=41)
        with patch.object(owner, 'process_identity', return_value=proc), patch.object(owner, 'pane_identity', return_value=pane):
            self.assertTrue(owner.valid(self.identity))
            self.assertFalse(owner.valid(dict(self.identity, frontend_start='different start')))
            self.assertFalse(owner.valid(dict(self.identity, pane_pid=99)))
        with patch.object(owner, 'process_identity', return_value=proc), patch.object(owner, 'pane_identity', return_value=None):
            self.assertFalse(owner.valid(self.identity))

    def test_same_pane_replacement_and_exit_cannot_erase_new_owner(self):
        with patch.object(owner, 'valid', return_value=True):
            a = owner.bind('thread-a', self.identity)
            b = owner.bind('thread-b', self.identity)
            self.assertNotEqual(a['binding_id'], b['binding_id'])
            self.assertEqual(owner.resolve('thread-a')['status'], 'unbound')
            newer = owner.bind('thread-b', dict(self.identity, token='owner-b', pane='%2'))
            owner.release('owner-a')
            self.assertEqual(owner.resolve('thread-b'), newer)

    def test_concurrent_same_cwd_threads_keep_distinct_panes(self):
        with patch.object(owner, 'valid', return_value=True):
            owner.bind('a', self.identity)
            owner.bind('b', dict(self.identity, pane='%2', token='owner-b'))
            self.assertEqual(owner.resolve('a')['pane'], '%1')
            self.assertEqual(owner.resolve('b')['pane'], '%2')

    def test_malformed_identity_is_unbound(self):
        self.assertEqual(owner.resolve('../oops')['status'], 'unbound')
        owner.ROOT.mkdir()
        (owner.ROOT / 'bindings.json').write_text('{')
        self.assertEqual(owner.resolve('okay')['status'], 'unbound')

    def test_direct_fallback_requires_user_thread_and_real_no_daemon_ancestor(self):
        with sqlite3.connect(self.root / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads (id TEXT, thread_source TEXT, source TEXT, agent_path TEXT)')
            db.executemany('INSERT INTO threads VALUES (?,?,?,?)', [('root', 'user', 'vscode', None), ('child', 'agent', 'subagent', '/root/child')])
        with patch.dict(os.environ, CODEX_HOME=str(self.root)), patch.object(owner, 'run', return_value=SimpleNamespace(returncode=0, stdout='1 /x/codex --no-daemon')) as run, patch.object(owner, 'capture', return_value=self.identity):
            record = owner.direct_owner('root')
            self.assertEqual(record['session_id'], 'root')
            self.assertTrue(record['direct'])
            self.assertIsNone(owner.direct_owner('child'))
            self.assertEqual(run.call_count, 1)
        with patch.dict(os.environ, CODEX_HOME=str(self.root)), patch.object(owner, 'run', return_value=SimpleNamespace(returncode=0, stdout='1 /x/codex app-server --listen unix:// --managed-daemon')), patch.object(owner, 'capture') as capture:
            self.assertIsNone(owner.direct_owner('root'))
            capture.assert_not_called()

    def walk(self, tree, sid='root'):
        """direct_owner() from a hook whose parent is tree's first pid.

        tree maps pid -> 'PPID COMMAND' as `ps -o ppid=,command=` prints it.
        Returns (record, pid passed to capture or None).
        """
        with sqlite3.connect(self.root / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE IF NOT EXISTS threads (id TEXT, thread_source TEXT, source TEXT, agent_path TEXT)')
            db.execute("INSERT INTO threads VALUES ('root', 'user', 'cli', NULL)")
        def ps(args):
            line = tree.get(int(args[2]))
            return SimpleNamespace(returncode=0 if line else 1, stdout=line or '')
        captured = []
        def capture(pid, sock, pane):
            captured.append((pid, sock, pane))
            return dict(self.identity, frontend_pid=pid)
        env = dict(CODEX_HOME=str(self.root), TMUX='/tmp/test-socket,1,0', TMUX_PANE='%1')
        with patch.dict(os.environ, env), patch.object(owner, 'run', side_effect=ps), \
             patch.object(owner.os, 'getppid', return_value=next(iter(tree))), \
             patch.object(owner, 'capture', side_effect=capture):
            record = owner.direct_owner(sid)
        return record, (captured[0][0] if captured else None)

    def test_bare_bun_frontend_binds_its_native_child(self):
        # `codex` from ~/.bun/bin (no wrapper, no --no-daemon) with no daemon
        # running hosts its backend in-process: hook -> sh -> native codex ->
        # node launcher -> pane shell. The native child is the frontend pid.
        native = '/u/.bun/install/global/node_modules/@openai/codex-darwin-arm64/vendor/codex/codex'
        record, pid = self.walk({
            300: '200 /bin/sh -c bash agent-tab-indicator.sh heartbeat codex',
            200: f'100 {native}',
            100: '50 node /u/.bun/bin/codex',
            50: '1 -zsh',
        })
        self.assertEqual(pid, 200)
        self.assertEqual(record['frontend_pid'], 200)
        self.assertTrue(record['direct'])
        # The token formula is shared with resurrect-save-repair; keep it.
        expected = owner.hashlib.sha256(
            f"200:{self.identity['frontend_start']}:{self.identity['tmux_socket']}:%1".encode()).hexdigest()
        self.assertEqual(record['token'], expected)
        self.assertEqual(record['binding_id'], expected + ':root')
        for args in ('resume 019a-id', "fix don't break it", '-m gpt-5 -c a=b prompt', 'fork --last'):
            with self.subTest(args=args):
                self.assertEqual(self.walk({300: '200 sh', 200: f'1 {native} {args}'})[1], 200)

    def test_shared_daemon_and_app_servers_are_still_rejected(self):
        # A bare frontend attached to a running daemon: its hooks run under the
        # daemon, whose inherited TMUX_PANE names whoever started it.
        for daemon in ('/x/codex app-server --listen unix:// --managed-daemon',
                       '/x/codex app-server --listen unix:///tmp/codex-daemon-501/s',
                       '/x/codex app-server --listen=ws://127.0.0.1:4000',
                       '/x/codex --managed-daemon'):
            with self.subTest(daemon=daemon):
                # Even when that daemon is itself a child of a frontend.
                record, pid = self.walk({300: '200 sh -c hook', 200: f'100 {daemon}',
                                         100: '50 /x/codex --no-daemon', 50: '1 -zsh'})
                self.assertIsNone(record)
                self.assertIsNone(pid)
        # Non-frontend codex processes never claim a pane themselves.
        for helper in ('/x/codex exec prompt', '/x/codex app-server proxy', '/x/codex --remote unix:///s'):
            with self.subTest(helper=helper):
                self.assertEqual(self.walk({300: f'1 {helper}'}), (None, None))

    def test_private_stdio_helpers_are_walked_through_to_the_frontend(self):
        for helper in ('/x/codex app-server', '/x/codex app-server --listen stdio://', '/x/codex sandbox macos -- sh'):
            with self.subTest(helper=helper):
                self.assertEqual(self.walk({300: f'200 {helper}', 200: '100 /x/codex', 100: '1 -zsh'})[1], 200)

    def test_unclassified_nested_codex_never_binds_the_outer_pane(self):
        # A codex started inside a wrapper frontend's tool PTY whose argv can't
        # be read as a frontend (prompt starts with a subcommand word; a -c
        # value with spaces) must end the walk, not fall through to the outer
        # frontend's pane.
        outer = {150: '120 /bin/zsh -lc codex', 120: '100 /n/codex --no-daemon -c m=1',
                 100: '50 node /u/.bun/bin/codex --no-daemon', 50: '1 -zsh'}
        for inner in ('/n/codex review the diff please', '/n/codex -c instructions=do exec now',
                      '/n/codex app-server proxy', '/n/codex app-server daemon start',
                      '/n/codex exec -m gpt-5 task', '/n/codex --remote unix:///s'):
            with self.subTest(inner=inner):
                tree = {300: '200 /bin/sh -c hook', 200: f'150 {inner}', **outer}
                self.assertEqual(self.walk(tree), (None, None))

    def test_prompt_words_never_reclassify_a_frontend(self):
        # Only the leading options describe the process; the prompt after them
        # may mention daemons or subcommands.
        for args in ('--no-daemon -c m=1 why does --managed-daemon hang',
                     '--no-daemon explain app-server --listen unix://x',
                     '--no-daemon app-server is slow', '--no-daemon review my branch',
                     '--no-daemon -c m=1 exec this literally'):
            with self.subTest(wrapper=args):
                self.assertEqual(self.walk({300: '200 sh', 200: f'1 /n/codex {args}'})[1], 200)
        for args in ('fix --managed-daemon hang', 'explain app-server --listen unix://x',
                     'why is --no-daemon slow', '-m gpt-5 resume'):
            with self.subTest(bare=args):
                self.assertEqual(self.walk({300: '200 sh', 200: f'100 /n/codex {args}', 100: '1 -zsh'})[1], 200)
        # A bare prompt that starts with a subcommand word is unreadable: it
        # stays unbound rather than guess (the wrapper launch above binds).
        self.assertEqual(self.walk({300: '200 sh', 200: '100 /n/codex review my branch', 100: '1 -zsh'}),
                         (None, None))
        # Daemon flags count in the leading options / app-server argv only.
        for args in ('--managed-daemon', '-c a=b --managed-daemon', 'app-server --managed-daemon',
                     'app-server -c a=b --listen unix:///s', 'app-server --listen=ws://127.0.0.1:1'):
            with self.subTest(daemon=args):
                self.assertIsNone(owner.codex_role(args.split()))

    def test_agents_browser_never_gets_no_daemon(self):
        # `codex agents` browses the shared daemon and rejects --no-daemon.
        self.assertFalse(owner.interactive_args(['agents']))
        self.assertEqual(owner.local_session_args(['agents']), ['agents'])
        self.assertEqual(owner.local_session_args(['-c', 'a=b', 'agents']), ['-c', 'a=b', 'agents'])

    def test_wrapper_frontend_binding_is_unchanged(self):
        # ~/.local/bin/codex execs node with --no-daemon and max context; the
        # native child carries the same argv, whatever the prompt says.
        native = '/n/vendor/codex/codex --no-daemon -c model_context_window=872000'
        for args in ('', ' resume saved', ' exec this prompt literally', " don't"):
            with self.subTest(args=args):
                record, pid = self.walk({300: '200 /bin/sh -c hook', 200: f'100 {native}{args}',
                                         100: '50 node /u/.bun/bin/codex --no-daemon', 50: '1 -zsh'})
                self.assertEqual(pid, 200)
                self.assertEqual(record['session_id'], 'root')

    def test_thread_lookup_survives_idle_wal_database_without_shm(self):
        path = self.root / 'state_5.sqlite'
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE threads (id TEXT, thread_source TEXT, source TEXT, agent_path TEXT)')
            db.execute("INSERT INTO threads VALUES ('root', 'user', 'cli', NULL)")
            db.commit()
        # Codex's SQLite removes -wal/-shm on a clean last close — the state an
        # idle Codex leaves. Apple's system SQLite keeps them (persistent WAL),
        # so remove them by hand; the close above already checkpointed.
        for suffix in ('-wal', '-shm'):
            (self.root / f'state_5.sqlite{suffix}').unlink(missing_ok=True)
        with self.assertRaises(sqlite3.OperationalError):
            sqlite3.connect(path.as_uri() + '?mode=ro', uri=True).execute('SELECT 1 FROM threads').fetchone()
        self.assertEqual(owner.thread_row(self.root, 'thread_source, source', 'root'), ('user', 'cli'))
        self.assertIsNone(owner.thread_row(self.root, 'thread_source', 'missing'))
        with patch.dict(os.environ, CODEX_HOME=str(self.root)), patch.object(owner, 'run', return_value=SimpleNamespace(returncode=0, stdout='1 /x/codex --no-daemon')), patch.object(owner, 'capture', return_value=self.identity):
            self.assertEqual(owner.direct_owner('root')['session_id'], 'root')

    def test_passthrough_subcommands_and_remote(self):
        for args in (['exec', 'prompt'], ['--remote=unix:///tmp/a'], ['-m', 'model', 'mcp', 'list'], ['--help']):
            self.assertFalse(owner.interactive_args(args), args)
        for args in ([], ['--no-daemon'], ['fork', '--no-daemon'], ['resume', 'id'], ['fork', 'id'], ['-c', 'a=b', 'prompt'], ['--', 'exec']):
            self.assertTrue(owner.interactive_args(args), args)

    def test_native_passthrough_flags_respect_option_positions(self):
        for args in (['resume', 'id', '--remote', 'unix:///tmp/server'],
                     ['prompt', '--help']):
            self.assertFalse(owner.interactive_args(args), args)
        for args in (['--', '-pwork'], ['--', '--profile=work'], ['--', '--remote=x'],
                     ['--', '--help'], ['-c', 'key=-pwork'], ['-c', '--profile=work'],
                     ['resume', '--', '-pwork'], ['-c', '--remote=x'],
                     ['-p', 'work'], ['--profile', 'work'], ['--profile=work'], ['-pwork'],
                     ['resume', 'id', '-p', 'work'], ['fork', '--profile=work']):
            self.assertTrue(owner.interactive_args(args), args)

    def test_local_launch_never_starts_shared_server(self):
        with patch.object(owner, 'real_codex', return_value='/native/codex'), \
             patch.object(owner.os, 'execv', side_effect=SystemExit) as execute, \
             patch.object(owner.subprocess, 'run') as start, \
             patch.object(owner, 'capture') as capture:
            for args in ([], ['prompt'], ['resume', 'saved'], ['fork', 'saved'],
                         ['-c', 'model_context_window=872000', 'resume', '--last']):
                with self.assertRaises(SystemExit):
                    owner.launch(args)
                execute.assert_called_with('/native/codex', ['/native/codex', '--no-daemon'] + args)
            for args in (['exec', 'prompt'], ['app-server', 'proxy'], ['--no-daemon'],
                         ['--no-daemon', 'resume', 'saved'], ['--help']):
                with self.assertRaises(SystemExit):
                    owner.launch(args)
                execute.assert_called_with('/native/codex', ['/native/codex'] + args)
            start.assert_not_called()
            capture.assert_not_called()

    def test_remote_is_disabled_without_mistaking_prompts_or_option_values(self):
        for args in (['--remote', 'unix:///server'], ['resume', 'saved', '--remote=unix:///server']):
            with self.assertRaisesRegex(SystemExit, 'servers are disabled'):
                owner.local_session_args(args)
        for args in (['--', '--remote=literal prompt'], ['-c', '--remote=literal'],
                     ['-m', '--no-daemon'], ['--', '--no-daemon']):
            self.assertEqual(owner.local_session_args(args), ['--no-daemon'] + args)


if __name__ == '__main__':
    unittest.main()
