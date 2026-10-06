"""Isolated transport/ownership regressions; never writes to a real tmux server."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import struct
import sys
import sqlite3
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location('terminal_owner', Path(__file__).resolve().parents[1] / 'scripts/codex-terminal-owner.py')
owner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(owner)


def frame(payload, opcode=1, final=True, masked=True):
    if isinstance(payload, dict):
        payload = json.dumps(payload).encode()
    prefix = bytes([(0x80 if final else 0) | opcode])
    length = len(payload)
    flag = 0x80 if masked else 0
    if length < 126:
        prefix += bytes([flag | length])
    elif length < 65536:
        prefix += bytes([flag | 126]) + struct.pack('!H', length)
    else:
        prefix += bytes([flag | 127]) + struct.pack('!Q', length)
    if masked:
        mask = b'abcd'
        return prefix + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return prefix + payload


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

    def test_old_reconciliation_same_owner_is_discarded(self):
        with patch.object(owner, 'valid', return_value=True):
            old = owner.bind('thread-a', self.identity)
            owner.bind('thread-a', self.identity)
            with patch.object(owner.subprocess, 'run') as run:
                owner.notify_bound(old)
                run.assert_not_called()

    def test_malformed_identity_is_unbound(self):
        self.assertEqual(owner.resolve('../oops')['status'], 'unbound')
        owner.ROOT.mkdir()
        (owner.ROOT / 'bindings.json').write_text('{')
        self.assertEqual(owner.resolve('okay')['status'], 'unbound')

    def test_observer_ignores_broadcast_read_and_errors(self):
        binds = []
        observer = owner.ProtocolObserver({}, lambda sid, _: binds.append(sid), lambda *_: None)
        observer.server({'method': 'thread/started', 'params': {'thread': {'id': 'unrelated'}}})
        observer.client({'id': 1, 'method': 'thread/read', 'params': {'threadId': 'read'}})
        observer.server({'id': 1, 'result': {'thread': {'id': 'read'}}})
        observer.client({'id': 2, 'method': 'thread/resume', 'params': {'threadId': 'bad'}})
        observer.server({'id': 2, 'error': {'code': -1}})
        self.assertEqual(binds, [])

    def test_observer_reordered_replies_do_not_steal_latest_selection(self):
        binds = []
        observer = owner.ProtocolObserver({}, lambda sid, _: binds.append(sid), lambda *_: None)
        observer.client({'id': 'a', 'method': 'thread/start'})
        observer.client({'id': 'b', 'method': 'thread/start'})
        observer.server({'id': 'b', 'result': {'thread': {'id': 'new'}}})
        observer.server({'id': 'a', 'result': {'thread': {'id': 'old'}}})
        self.assertEqual(binds, ['new'])
        observer.client({'id': 'c', 'method': 'thread/resume'})
        observer.client({'id': 'd', 'method': 'turn/start', 'params': {'threadId': 'active'}})
        observer.server({'id': 'c', 'result': {'thread': {'id': 'stale'}}})
        self.assertEqual(binds, ['new', 'active'])

    def test_native_title_helper_cannot_replace_real_foreground_or_title(self):
        binds, titles = [], []
        def claim(sid, identity):
            binds.append(sid)
            return owner.bind(sid, identity)
        observer = owner.ProtocolObserver(self.identity, claim, lambda record, thread: titles.append(thread.get('name')))
        with patch.object(owner, 'valid', return_value=True):
            observer.client({'id': 1, 'method': 'thread/start'})
            observer.server({'id': 1, 'result': {'thread': {'id': 'real', 'ephemeral': False, 'name': 'Fix Codex status'}}})
            observer.client({'id': 2, 'method': 'turn/start', 'params': {'threadId': 'real'}})
            foreground = owner.resolve('real')
            observer.client({'id': 3, 'method': 'thread/start', 'params': {'ephemeral': True}})
            observer.server({'id': 3, 'result': {'thread': {'id': 'title-helper', 'ephemeral': True}}})
            observer.client({'id': 4, 'method': 'turn/start', 'params': {'threadId': 'title-helper'}})
            observer.server({'method': 'turn/completed', 'params': {'threadId': 'title-helper', 'turn': {'status': 'completed'}}})
            observer.server({'method': 'turn/completed', 'params': {'threadId': 'real', 'turn': {'status': 'completed'}}})
            self.assertEqual(owner.resolve('real'), foreground)
            self.assertEqual(owner.resolve('title-helper')['status'], 'unbound')
        deadline = time.monotonic() + 1
        while not titles and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(binds, ['real', 'real'])
        self.assertEqual(titles, ['Fix Codex status'])

    def test_auxiliary_metadata_does_not_invalidate_pending_resume(self):
        for request_ephemeral in (False, True):
            for helper_reply_first in (False, True):
                with self.subTest(request_ephemeral=request_ephemeral, helper_reply_first=helper_reply_first):
                    binds = []
                    observer = owner.ProtocolObserver({}, lambda sid, _: binds.append(sid))
                    observer.client({'id': 'real', 'method': 'thread/resume', 'params': {'threadId': 'real'}})
                    observer.client({'id': 'aux', 'method': 'thread/start', 'params': {'ephemeral': request_ephemeral}})
                    helper = {'id': 'aux', 'result': {'thread': {'id': 'aux', 'ephemeral': True}}}
                    real = {'id': 'real', 'result': {'thread': {'id': 'real', 'ephemeral': False}}}
                    for reply in ([helper, real] if helper_reply_first else [real, helper]):
                        observer.server(reply)
                    observer.client({'method': 'turn/start', 'params': {'threadId': 'aux'}})
                    self.assertEqual(binds, ['real'])

    def test_request_ephemeral_and_forks_stay_ignored_across_reconnect(self):
        binds = []
        observer = owner.ProtocolObserver({}, lambda sid, _: binds.append(sid))
        observer.client({'id': 1, 'method': 'thread/fork', 'params': {'threadId': 'root', 'ephemeral': True}})
        # Request metadata is sufficient even if the response omits the field.
        observer.server({'id': 1, 'result': {'thread': {'id': 'aux'}}})
        observer.client({'id': 'abandoned', 'method': 'thread/start'})
        observer.reset_connection()
        observer.server({'id': 'abandoned', 'result': {'thread': {'id': 'stale'}}})
        observer.client({'method': 'turn/start', 'params': {'threadId': 'aux'}})
        observer.client({'id': 1, 'method': 'thread/resume', 'params': {'threadId': 'aux'}})
        observer.server({'id': 1, 'result': {'thread': {'id': 'aux', 'ephemeral': False}}})
        observer.client({'id': 2, 'method': 'thread/fork', 'params': {'threadId': 'aux'}})
        observer.server({'id': 2, 'result': {'thread': {'id': 'aux-child'}}})
        observer.client({'method': 'turn/start', 'params': {'threadId': 'aux-child'}})
        observer.client({'id': 3, 'method': 'thread/start'})
        observer.server({'id': 3, 'result': {'thread': {'id': 'real-new', 'ephemeral': False}}})
        observer.client({'method': 'turn/start', 'params': {'threadId': 'real-new'}})
        self.assertEqual(binds, ['real-new', 'real-new'])

    def test_auxiliary_cache_cap_never_reenables_uncached_helper_turns(self):
        binds = []
        observer = owner.ProtocolObserver({}, lambda sid, _: binds.append(sid))
        observer.client({'method': 'turn/start', 'params': {'threadId': 'real'}})
        with patch.object(owner, 'MAX_AUXILIARY_THREADS', 1):
            for sid in ('aux-one', 'aux-overflow'):
                observer.client({'id': sid, 'method': 'thread/start', 'params': {'ephemeral': True}})
                observer.server({'id': sid, 'result': {'thread': {'id': sid, 'ephemeral': True}}})
        self.assertEqual(observer.auxiliary_threads, {'aux-one'})
        for sid in ('aux-one', 'aux-overflow'):
            observer.client({'method': 'turn/start', 'params': {'threadId': sid}})
        observer.client({'method': 'turn/start', 'params': {'threadId': 'real'}})
        observer.client({'id': 'new', 'method': 'thread/start'})
        observer.server({'id': 'new', 'result': {'thread': {'id': 'real-new', 'ephemeral': False}}})
        observer.client({'method': 'turn/start', 'params': {'threadId': 'real-new'}})
        self.assertEqual(binds, ['real', 'real', 'real-new', 'real-new'])

    def test_interrupt_updates_only_existing_same_client_binding(self):
        calls = []
        observer = owner.ProtocolObserver(self.identity, lambda *_: self.fail('must not bind'), lambda *args: calls.append(args))
        notification = {'method': 'turn/completed', 'params': {'threadId': 'a', 'turn': {'status': 'interrupted'}}}
        record = dict(self.identity, status='bound', session_id='a')
        with patch.object(owner, 'resolve', return_value=dict(record, token='different')):
            observer.server(notification)
        self.assertEqual(calls, [])
        with patch.object(owner, 'resolve', return_value=record):
            observer.server(notification)
        deadline = time.monotonic() + 1
        while not calls and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(calls, [(record, None, 'Interrupt')])

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

    def test_frames_unchanged_and_turn_bound_before_forward(self):
        events = []
        class Output(io.BytesIO):
            def write(self, data):
                events.append('write')
                return super().write(data)
        observer = owner.ProtocolObserver({}, lambda *_: events.append('bind'))
        frame = b'{"id":1,"method":"turn/start","params":{"threadId":"a"}}\n'
        stream = frame + b'not json\n' + b'[]\n'
        dest = Output()
        owner.relay(io.BytesIO(stream), dest, observer.client)
        self.assertEqual(dest.getvalue(), stream)
        self.assertEqual(events[:2], ['bind', 'write'])

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

    def test_new_session_directory_respects_options_and_resume(self):
        cwd = '/launch/folder with spaces'
        for args in ([], ['prompt'], ['--', 'resume'], ['-c', '--cd'],
                     ['--model', '-Celsewhere'], ['-p', 'resume', 'prompt'],
                     ['--', '--cd=/prompt']):
            self.assertEqual(owner.new_session_args(args, cwd), ['--cd', cwd] + args, args)
        for args in (['-C', '/chosen'], ['--cd', '/chosen'], ['--cd=/chosen'],
                     ['-C/chosen'], ['-C=/chosen'], ['resume', 'id'], ['fork', 'id'],
                     ['-m', 'model', 'resume', '--last'], ['-c', 'key=value', 'fork']):
            self.assertEqual(owner.new_session_args(args, cwd), args, args)

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

    def test_websocket_mask_fragment_ping_and_extended_lengths(self):
        message = {'id': 1, 'method': 'turn/start', 'params': {'threadId': 'a'}}
        raw = json.dumps(message).encode()
        upgrade = b'GET / HTTP/1.1\r\nUpgrade: websocket\r\n\r\n'
        wire = upgrade + frame(raw[:20], final=False) + frame(b'ping', opcode=9) + frame(raw[20:], opcode=0)
        wire += frame({'huge': 'x' * 70000}, masked=False)
        wire += frame(b'not-json', masked=False)
        dest = io.BytesIO()
        observed = []
        owner.relay_websocket(io.BytesIO(wire), dest, observed.append)
        self.assertEqual(dest.getvalue(), wire)
        self.assertEqual(observed, [message, {'huge': 'x' * 70000}])

    def test_websocket_binary_compressed_and_oversized_are_opaque(self):
        upgrade = b'HTTP/1.1 101 Switching Protocols\r\n\r\n'
        compressed = bytearray(frame({'compressed': True}, masked=False))
        compressed[0] |= 0x40
        wire = upgrade + bytes(compressed) + frame(b'binary', opcode=2)
        wire += frame(b'x' * (16 * 1024 * 1024 + 1), masked=False)
        dest = io.BytesIO()
        observed = []
        owner.relay_websocket(io.BytesIO(wire), dest, observed.append)
        self.assertEqual(dest.getvalue(), wire)
        self.assertEqual(observed, [])

    def test_socket_disconnect_stops_proxy_and_cleans_owner(self):
        # Executable fake proxy uses only stdio; no Codex or tmux process is run.
        fake = self.root / 'proxy'
        fake.write_text('#!' + sys.executable + '\nimport os\nwhile True:\n chunk=os.read(0,65536)\n if not chunk:break\n os.write(1,chunk)\n')
        fake.chmod(0o700)
        path = str(self.root / 'sock')
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(path)
        listener.listen(1)
        parent, child = socket.socketpair()
        pid = os.fork()
        if not pid:
            parent.close()
            try:
                owner.serve(listener, str(fake), self.identity, child)
            except BaseException:
                import traceback
                traceback.print_exc()
                os._exit(1)
            else:
                os._exit(0)
        child.close()
        listener.close()
        parent.settimeout(3)
        self.assertEqual(parent.recv(1), b'1')
        parent.close()
        try:
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(3)
                client.connect(path)
                wire = b'GET / HTTP/1.1\r\n\r\n' + frame({'id': 1, 'method': 'initialize', 'params': {}})
                client.sendall(wire)
                received = b''
                while len(received) < len(wire):
                    chunk = client.recv(4096)
                    self.assertTrue(chunk, "proxy closed before echoing its complete handshake/frame")
                    received += chunk
                self.assertEqual(received, wire)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    self.assertEqual(status, 0)
                    self.assertFalse(Path(path).exists())
                    return
                time.sleep(.02)
            self.fail('proxy did not exit after client disconnected')
        finally:
            try:
                os.kill(pid, 15)
                os.waitpid(pid, 0)
            except ProcessLookupError:
                pass

    def test_frontend_can_reconnect_after_upstream_disconnect(self):
        fake = self.root / 'proxy-reconnect'
        # Upstream exits after one websocket message, emulating daemon update.
        fake.write_text('#!' + sys.executable + '\nimport os\nwhile True:\n chunk=os.read(0,65536)\n if not chunk:break\n os.write(1,chunk)\n if b"upgrade-end" in chunk:break\n')
        fake.chmod(0o700)
        done = self.root / 'frontend-exited'
        path = str(self.root / 'reconnect.sock')
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(path)
        listener.listen(1)
        parent, child = socket.socketpair()
        pid = os.fork()
        if not pid:
            parent.close()
            owner.frontend_alive = lambda _: not done.exists()
            try:
                owner.serve(listener, str(fake), self.identity, child)
            except BaseException:
                import traceback
                traceback.print_exc()
                os._exit(1)
            else:
                os._exit(0)
        child.close()
        listener.close()
        parent.settimeout(3)
        self.assertEqual(parent.recv(1), b'1')
        parent.close()
        try:
            for attempt in range(2):
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(3)
                    client.connect(path)
                    wire = b'GET / HTTP/1.1\r\nX-End: upgrade-end\r\n\r\n'
                    client.sendall(wire)
                    response = b''
                    while True:
                        chunk = client.recv(4096)
                        if not chunk:break
                        response += chunk
                    self.assertEqual(response, wire)
            done.touch()
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                ended, status = os.waitpid(pid, os.WNOHANG)
                if ended:
                    self.assertEqual(status, 0)
                    self.assertFalse(Path(path).exists())
                    return
                time.sleep(.02)
            self.fail('reconnect server failed to notice frontend exit')
        finally:
            try:
                os.kill(pid, 15)
                os.waitpid(pid, 0)
            except ProcessLookupError:
                pass


if __name__ == '__main__':
    unittest.main()
