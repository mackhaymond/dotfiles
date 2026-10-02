#!/usr/bin/env python3
"""Bind Codex threads to terminal clients, never the shared daemon's environment.

The CLI wrapper relays Codex's Unix WebSocket transport without changing
messages. Only that client's start/resume replies and turn/start requests claim
a pane. Records contain identities, not prompts or protocol transcripts.
"""
import contextlib
import fcntl
import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import shlex
import signal
import socket
import struct
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(os.environ.get("CODEX_TERMINAL_OWNER_DIR", str(Path.home() / ".cache/codex-terminal-owners")))
SCRIPT = Path(__file__).resolve()
SID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_AUXILIARY_THREADS = 4096


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, timeout=3, **kwargs)


def process_identity(pid):
    result = run(["ps", "-p", str(int(pid)), "-o", "lstart=,tty="])
    parts = result.stdout.strip().split()
    if result.returncode or len(parts) != 6:
        return None
    return {"frontend_pid": int(pid), "frontend_start": " ".join(parts[:5]), "tty": parts[5]}


def pane_identity(sock, pane):
    if not sock or not os.path.isabs(sock) or not re.fullmatch(r"%\d+", pane or ""):
        return None
    r = run(["tmux", "-S", sock, "display-message", "-p", "-t", pane,
             "#{pane_id}\t#{pane_tty}\t#{pane_pid}\t#{window_id}"])
    p = r.stdout.strip().split("\t")
    if r.returncode or len(p) != 4 or p[0] != pane:
        return None
    return {"pane": p[0], "tty": p[1].removeprefix("/dev/"), "pane_pid": int(p[2]), "window": p[3]}


def capture(pid, sock, pane):
    proc = process_identity(pid)
    target = pane_identity(sock, pane)
    if not proc or not target or proc["tty"] in ("??", "?") or proc["tty"] != target["tty"]:
        return None
    return dict(proc, pane=pane, pane_pid=target["pane_pid"], tmux_socket=os.path.realpath(sock),
                term="tmux", status="bound", token=uuid.uuid4().hex)


def valid(record):
    try:
        proc = process_identity(record["frontend_pid"])
        pane = pane_identity(record["tmux_socket"], record["pane"])
        return bool(proc and pane and proc["frontend_start"] == record["frontend_start"]
                    and proc["tty"] == pane["tty"] == record["tty"]
                    and pane["pane_pid"] == record["pane_pid"])
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        return False


@contextlib.contextmanager
def registry():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (ROOT / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            records = json.loads((ROOT / "bindings.json").read_text())
            if not isinstance(records, dict):
                records = {}
        except (OSError, ValueError):
            records = {}
        yield records
        fd, tmp = tempfile.mkstemp(dir=ROOT, prefix=".bindings-")
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(records, out)
            os.replace(tmp, ROOT / "bindings.json")
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def bind(sid, owner):
    if not SID.fullmatch(sid or "") or not valid(owner):
        return None
    record = dict(owner, session_id=sid, bound_at=time.time(), binding_id=uuid.uuid4().hex)
    with registry() as records:
        # A pane has one foreground conversation. Late events from the previous
        # conversation must not overwrite its replacement after /new or resume.
        for old_sid, old in list(records.items()):
            if (old.get("tmux_socket"), old.get("pane")) == (record["tmux_socket"], record["pane"]):
                del records[old_sid]
        records[sid] = record
    return record


def resolve(sid):
    empty = {"status": "unbound", "session_id": sid, "pane": "", "tmux_socket": "", "term": "", "frontend_pid": 0}
    if not SID.fullmatch(sid or ""):
        return empty
    try:
        records = json.loads((ROOT / "bindings.json").read_text())
        record = records.get(sid)
        if record and valid(record):
            return record
    except (OSError, ValueError, AttributeError):
        pass
    return direct_owner(sid) or empty


def direct_owner(sid):
    """Keep explicit --no-daemon hooks working without trusting inherited PTYs.

    A direct CLI ancestor is evidence only for root user threads. Shared daemon
    hooks and tool-created PTYs cannot claim its inherited terminal identity.
    This fallback does not persist a selection: a delayed hook must not rebind
    an old thread in the shared registry.
    """
    try:
        home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        with sqlite3.connect((home / "state_5.sqlite").as_uri() + "?mode=ro", uri=True, timeout=.1) as db:
            row = db.execute("SELECT thread_source, source, agent_path FROM threads WHERE id=?", (sid,)).fetchone()
        if not row or row[0] != "user" or row[1] not in ("cli", "vscode") or row[2]:
            return None
        pid = os.getppid()
        for _ in range(32):
            result = run(["ps", "-p", str(pid), "-o", "ppid=,command="])
            fields = result.stdout.strip().split(None, 1)
            if result.returncode or len(fields) != 2:
                return None
            parent, command = int(fields[0]), shlex.split(fields[1])
            if command and os.path.basename(command[0]) == "codex":
                if "--managed-daemon" in command or ("app-server" in command and "--listen" in command and "unix://" in command):
                    return None
                if "--no-daemon" in command:
                    record = capture(pid, os.environ.get("TMUX", "").split(",")[0], os.environ.get("TMUX_PANE", ""))
                    if record:
                        identity = f'{pid}:{record["frontend_start"]}:{record["tmux_socket"]}:{record["pane"]}'
                        token = hashlib.sha256(identity.encode()).hexdigest()
                        return dict(record, session_id=sid, token=token, binding_id=token + ":" + sid,
                                    bound_at=0, direct=True)
                    return None
            if parent <= 1 or parent == pid:
                break
            pid = parent
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
        pass
    return None


def release(token):
    with registry() as records:
        for sid, record in list(records.items()):
            if record.get("token") == token:
                del records[sid]


def notify_bound(record, thread=None, event=None):
    """Recover SessionStart hooks that ran before thread/start returned its id."""
    thread = thread or {}
    sid = record["session_id"]
    # A later turn/start or resume may already have replaced this selection.
    now = resolve(sid)
    if now.get("binding_id") != record["binding_id"]:
        return
    status = thread.get("status") or {}
    active = status.get("type") == "active"
    payload = {"session_id": sid, "hook_event_name": event or ("UserPromptSubmit" if active else "SessionStart"),
               "source": "resume", "cwd": thread.get("cwd", ""), "terminal_reconcile": True,
               "terminal_binding_id": record["binding_id"]}
    raw = json.dumps(payload)
    env = dict(os.environ, TMUX=record["tmux_socket"] + ",0,0", TMUX_PANE=record["pane"])
    commands = [
        ["bash", str(SCRIPT.with_name("agent-tab-indicator.sh")), "interrupt" if event == "Interrupt" else "reconcile", "codex"],
        [str(Path.home() / ".local/bin/cua-notch-agent-hook"), "codex"],
        [str(Path.home() / ".local/bin/codex-session-track")],
    ]
    for command in commands:
        if resolve(sid).get("binding_id") != record["binding_id"]:
            return
        try:
            subprocess.run(command, input=raw, text=True, env=env, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=4)
        except (OSError, subprocess.SubprocessError):
            pass


class ProtocolObserver:
    """Observe only correlated client actions. Unrelated broadcasts do nothing."""
    def __init__(self, owner, on_bind=bind, notify=notify_bound):
        self.owner = owner
        self.pending = {}
        self.sequence = 0
        self.selected_sequence = 0
        self.selected_thread = None
        self.auxiliary_threads = set()
        self.auxiliary_overflow = False
        self.lock = threading.Lock()
        self.on_bind = on_bind
        self.notify = notify

    def reset_connection(self):
        # JSON-RPC request IDs may be reused after reconnect. Auxiliary thread
        # identities remain valid for this terminal invocation across sockets.
        with self.lock:
            self.pending.clear()

    def client(self, message):
        method = message.get("method")
        params = message.get("params") or {}
        with self.lock:
            if method in ("thread/start", "thread/resume", "thread/fork") and "id" in message:
                self.sequence += 1
                auxiliary = params.get("ephemeral") is True or params.get("threadId") in self.auxiliary_threads
                self.pending[message["id"]] = (self.sequence, auxiliary)
            elif method == "turn/start":
                sid = params.get("threadId", "")
                if sid in self.auxiliary_threads or (self.auxiliary_overflow and sid != self.selected_thread):
                    return
                self.sequence += 1
                self.selected_sequence = self.sequence
                self.selected_thread = sid
                self.on_bind(sid, self.owner)

    def server(self, message):
        if message.get("method") == "turn/completed":
            params = message.get("params") or {}
            if params.get("threadId") in self.auxiliary_threads:
                return
            if (params.get("turn") or {}).get("status") == "interrupted":
                record = resolve(params.get("threadId", ""))
                # Broadcasts may describe other clients or subagents. They can
                # update our existing binding, but can never claim a new one.
                if record.get("token") == self.owner.get("token") and record.get("status") == "bound":
                    threading.Thread(target=self.notify, args=(record, None, "Interrupt"), daemon=True).start()
            return
        with self.lock:
            if "method" in message or message.get("id") not in self.pending:
                return
            sequence, auxiliary = self.pending.pop(message["id"])
            if "error" in message:
                return
            thread = (message.get("result") or {}).get("thread") or {}
            sid = thread.get("id", "")
            if not SID.fullmatch(sid or ""):
                return
            # The native TUI also creates ephemeral title-generator threads on
            # this connection. Their starts AND later turns are background work,
            # even though both originate in the terminal client itself.
            if auxiliary or thread.get("ephemeral") is True or sid in self.auxiliary_threads:
                if len(self.auxiliary_threads) < MAX_AUXILIARY_THREADS:
                    self.auxiliary_threads.add(sid)
                else:
                    # Never evict an active helper and accidentally permit its
                    # later turn to claim the pane. At the cap, unknown turns
                    # need a foreground lifecycle reply before they can bind.
                    self.auxiliary_overflow = True
                return
            # Only a confirmed foreground selection supersedes earlier replies;
            # a pending auxiliary start must not invalidate a real resume.
            if sequence < self.selected_sequence:
                return
            self.selected_sequence = sequence
            self.selected_thread = sid
            record = self.on_bind(sid, self.owner)
        if record:
            threading.Thread(target=self.notify, args=(record, thread), daemon=True).start()


def relay(source, destination, observe):
    for line in iter(source.readline, b""):
        try:
            message = json.loads(line)
            if isinstance(message, dict):
                observe(message)
        except (ValueError, TypeError, OSError, subprocess.SubprocessError):
            # Ownership is advisory. Never change/drop a native protocol frame.
            pass
        destination.write(line)
        destination.flush()


def read_exact(source, length):
    chunks = []
    while length:
        part = source.read(length)
        if not part:
            raise EOFError("transport closed")
        chunks.append(part)
        length -= len(part)
    return b"".join(chunks)


def relay_websocket(source, destination, observe):
    """Pass HTTP upgrade and original frames; inspect uncompressed JSON text.

    Codex's Unix transport uses WebSocket, unlike its JSONL stdio app-server.
    app-server proxy preserves those raw bytes. Masking and fragmentation are
    handled only for observation; bytes on the wire remain exactly unchanged.
    """
    while True:
        line = source.readline()
        if not line:
            return
        destination.write(line)
        destination.flush()
        if line in (b"\r\n", b"\n"):
            break
    fragments = None
    while True:
        try:
            header = read_exact(source, 2)
            final, opcode = bool(header[0] & 0x80), header[0] & 0x0f
            masked, length = bool(header[1] & 0x80), header[1] & 0x7f
            if length == 126:
                extended = read_exact(source, 2)
                header += extended
                length = struct.unpack("!H", extended)[0]
            elif length == 127:
                extended = read_exact(source, 8)
                header += extended
                length = struct.unpack("!Q", extended)[0]
            mask = read_exact(source, 4) if masked else b""
            header += mask
            # Observe up to the native advertised 16 MiB message limit. Larger
            # or binary frames still pass through in bounded chunks unchanged.
            limit = 16 * 1024 * 1024
            inspect = opcode in (0, 1) and not (header[0] & 0x70) and length <= limit
            if not inspect:
                if opcode in (0, 1, 2):
                    fragments = None
                destination.write(header)
                while length:
                    chunk = read_exact(source, min(length, 65536))
                    destination.write(chunk)
                    destination.flush()
                    length -= len(chunk)
                destination.flush()
                continue
            payload = read_exact(source, length)
        except EOFError:
            return
        decoded = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload)) if masked else payload
        if opcode == 1:
            fragments = bytearray(decoded)
        elif fragments is not None:
            if len(fragments) + len(decoded) <= limit:
                fragments.extend(decoded)
            else:
                fragments = None
        if final and fragments is not None:
            try:
                message = json.loads(fragments)
                if isinstance(message, dict):
                    observe(message)
            except (ValueError, TypeError, OSError, subprocess.SubprocessError):
                pass
            fragments = None
        destination.write(header)
        destination.write(payload)
        destination.flush()


def serve_connection(client, real, owner, observer=None):
    with client:
        proxy = subprocess.Popen([real, "app-server", "proxy"], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        observer = observer or ProtocolObserver(owner)
        observer.reset_connection()
        reader = client.makefile("rb")
        writer = client.makefile("wb")
        stop_lock = threading.Lock()
        def stop_proxy():
            # The npm launcher can have its own native child. Kill only
            # this dedicated proxy process group, never the daemon.
            with stop_lock:
                if proxy.poll() is not None:
                    return
                with contextlib.suppress(OSError):
                    os.killpg(proxy.pid, signal.SIGTERM)
                try:
                    proxy.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(OSError):
                        os.killpg(proxy.pid, signal.SIGKILL)
                    proxy.wait(timeout=2)
        def upstream():
            try:
                relay_websocket(reader, proxy.stdin, observer.client)
            except (OSError, ValueError):
                pass
            finally:
                with contextlib.suppress(OSError, ValueError):
                    proxy.stdin.close()
                stop_proxy()
        threading.Thread(target=upstream, daemon=True).start()
        try:
            relay_websocket(proxy.stdout, writer, observer.server)
        except (OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(OSError):
                client.shutdown(socket.SHUT_RDWR)
            stop_proxy()
            reader.close()
            writer.close()
            proxy.stdout.close()


def frontend_alive(owner):
    try:
        current = process_identity(owner["frontend_pid"])
        return bool(current and current["frontend_start"] == owner["frontend_start"])
    except (KeyError, OSError, ValueError, subprocess.SubprocessError):
        return False


def serve(listener, real, owner, ready):
    # The native TUI can reconnect after daemon restart. Keep this private
    # endpoint while its original frontend process exists, including reconnects.
    path = listener.getsockname()
    os.setsid()
    os.chdir("/")
    try:
        with listener:
            ready.sendall(b"1")
            ready.close()
            listener.settimeout(1)
            observer = ProtocolObserver(owner)
            while True:
                try:
                    client, _ = listener.accept()
                except socket.timeout:
                    if not frontend_alive(owner):
                        break
                    continue
                serve_connection(client, real, owner, observer)
                if not frontend_alive(owner):
                    break
    finally:
        release(owner["token"])
        with contextlib.suppress(OSError):
            os.unlink(path)


def real_codex():
    this = str(Path.home() / ".local/bin/codex")
    for directory in os.get_exec_path():
        p = os.path.join(directory, "codex")
        if os.path.isfile(p) and os.access(p, os.X_OK) and os.path.realpath(p) != os.path.realpath(this):
            return p
    raise RuntimeError("Cannot find the installed Codex CLI after the terminal wrapper")


def interactive_args(args):
    # Skip known option operands when finding a subcommand. Quoted prompts are
    # positional strings; only the actual native command names bypass the bridge.
    operands = {"-c", "--config", "--enable", "--disable", "-C", "--cd", "-m", "--model", "-p", "--profile",
                "-s", "--sandbox", "-a", "--ask-for-approval", "-i", "--image", "--add-dir", "--local-provider"}
    commands = {"exec", "e", "review", "login", "logout", "mcp", "plugin", "app-server", "remote-control", "app",
                "completion", "update", "doctor", "sandbox", "debug", "apply", "queue", "archive", "delete",
                "migrate-rollouts", "unarchive", "cloud", "exec-server", "features", "help"}
    skip = False
    positional = None
    for arg in args:
        if skip:
            skip = False
        elif arg == "--":
            break
        elif arg in ("--help", "-h", "--version", "-V", "--no-daemon", "--remote") or arg.startswith("--remote="):
            return False
        elif arg in operands:
            skip = True
        elif not arg.startswith("-") and positional is None:
            positional = arg
    return positional not in commands


def launch(args):
    real = real_codex()
    if not interactive_args(args) or not os.isatty(0):
        os.execv(real, [real] + args)
    sock = os.environ.get("TMUX", "").split(",")[0]
    owner = capture(os.getpid(), sock, os.environ.get("TMUX_PANE", ""))
    if not owner:
        os.execv(real, [real] + args)
    # Native command is idempotent and never restarts a healthy shared daemon.
    directory = None
    listener = parent = child = None
    pid = None
    try:
        started = subprocess.run([real, "app-server", "daemon", "start"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)
        if started.returncode:
            raise RuntimeError("shared server unavailable")
        directory = tempfile.mkdtemp(prefix="cx-tmux-", dir="/tmp")
        path = directory + "/s"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(1)
        parent, child = socket.socketpair()
        pid = os.fork()
        if pid == 0:
            parent.close()
            # Don't hold the terminal open or print transport errors into it.
            with open(os.devnull, "r+b", buffering=0) as null:
                for fd in (0, 1, 2):
                    os.dup2(null.fileno(), fd)
            try:
                serve(listener, real, owner, child)
            finally:
                shutil.rmtree(directory, ignore_errors=True)
                os._exit(0)
        child.close()
        listener.close()
        parent.settimeout(3)
        if parent.recv(1) != b"1":
            raise RuntimeError("terminal bridge did not start")
        parent.close()
        os.execv(real, [real, "--remote", "unix://" + path] + args)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        if pid:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
                os.waitpid(pid, 0)
        for handle in (listener, parent, child):
            if handle:
                with contextlib.suppress(OSError):
                    handle.close()
        if directory:
            shutil.rmtree(directory, ignore_errors=True)
        print("codex: terminal ownership bridge unavailable; continuing with native Codex", file=sys.stderr)
        os.execv(real, [real] + args)


def main():
    mode, *args = sys.argv[1:]
    if mode == "launch":
        launch(args)
    elif mode == "resolve":
        print(json.dumps(resolve(args[0])))
    elif mode == "bind":
        sid, pid, sock, pane = args
        owner = capture(int(pid), sock, pane)
        if not owner:
            raise SystemExit("terminal client does not own that pane")
        record = bind(sid, owner)
        print(json.dumps(record))
    elif mode == "release":
        release(args[0])


if __name__ == "__main__":
    main()
