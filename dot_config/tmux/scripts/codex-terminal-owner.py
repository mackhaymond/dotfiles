#!/usr/bin/env python3
"""Bind Codex threads to terminal clients, never the shared daemon's environment.

The CLI wrapper forces --no-daemon. Hooks resolve the owning frontend through
the nearest interactive Codex ancestor and its matching TTY; shared daemons
never qualify. Explicit `bind` records cover clients that hooks cannot reach.
"""
import contextlib
import fcntl
import json
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(os.environ.get("CODEX_TERMINAL_OWNER_DIR", str(Path.home() / ".cache/codex-terminal-owners")))
SID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
OPTION_OPERANDS = {"-c", "--config", "--enable", "--disable", "-C", "--cd", "-m", "--model", "-p", "--profile",
                   "-s", "--sandbox", "-a", "--ask-for-approval", "-i", "--image", "--add-dir", "--local-provider"}


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


def thread_row(home, columns, sid):
    """One row of Codex's WAL-mode state DB, read-only.

    When the last connection closes cleanly SQLite deletes state_5.sqlite-shm,
    and a mode=ro open cannot recreate it: "unable to open database file". An
    idle --no-daemon frontend holds no connection, so that state is ordinary
    and made direct_owner() report unbound. No -shm means no live writer and a
    fully checkpointed main file, so that one case reads it as immutable.
    """
    path = home / "state_5.sqlite"
    query = f"SELECT {columns} FROM threads WHERE id=?"
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.1) as db:
            return db.execute(query, (sid,)).fetchone()
    except sqlite3.OperationalError:
        if not path.exists() or (home / "state_5.sqlite-shm").exists():
            raise
    with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=.1) as db:
        return db.execute(query, (sid,)).fetchone()


def leading_args(args):
    """(options before the first positional, that positional or None, the rest).

    Only this prefix is trustworthy: after it come prompts, whose words (such
    as "--managed-daemon" or "app-server") say nothing about the process.
    """
    skip = False
    for index, arg in enumerate(args):
        if skip:
            skip = False
        elif arg == "--":
            return args[:index], None, args[index + 1:]
        elif arg in OPTION_OPERANDS:
            skip = True
        elif not arg.startswith("-"):
            return args[:index], arg, args[index + 1:]
    return args, None, []


def codex_role(args):
    """Classify a codex process by its argv: "frontend", "helper" or None.

    A frontend is an interactive TUI; its hooks carry its pane. A helper is a
    private child of one (`sandbox`, a stdio `app-server` without its own
    subcommand) and is walked through. Anything else, including a shared
    daemon (`--managed-daemon`, an app-server listening on a socket) whose
    inherited TMUX_PANE belongs to whoever started it, or a codex that cannot
    be read with confidence, ends the walk: guessing past it could land on an
    unrelated outer frontend.
    """
    options, positional, rest = leading_args(args)
    if "--managed-daemon" in options:
        return None
    # The wrapper adds --no-daemon first and only to interactive launches, so
    # in the leading options it marks a frontend whatever the prompt says.
    if "--no-daemon" in options:
        return "frontend"
    if positional == "sandbox":
        return "helper"
    if positional == "app-server":
        listen, skip = "stdio://", False
        for index, arg in enumerate(rest):
            if skip:
                skip = False
            elif arg == "--listen":
                listen, skip = (rest[index + 1] if index + 1 < len(rest) else ""), True
            elif arg.startswith("--listen="):
                listen = arg.split("=", 1)[1]
            elif arg == "--managed-daemon":
                return None
            elif arg in OPTION_OPERANDS or arg == "--code-mode-host":
                skip = True
            elif not arg.startswith("-"):
                return None  # daemon, proxy, ...: never a private backend
        return "helper" if listen == "stdio://" else None
    return "frontend" if interactive_args(args) else None


def direct_owner(sid):
    """Bind a hook to the terminal frontend whose own backend ran it.

    A frontend hosts its backend in-process when started with --no-daemon (the
    wrapper always adds it) or when no shared daemon is running, as with a bare
    `codex` that bypassed the wrapper. Either way the hook descends from the
    TUI, which must sit on the pane's TTY. A bare `codex` attached to a running
    daemon has its hooks run under the daemon instead; the walk meets that
    daemon first and stays unbound rather than guess. Only known helpers
    (`sandbox`, a private stdio app-server) are walked through; any other
    codex ancestor that is not a frontend ends the walk unbound.

    A direct ancestor is evidence only for root user threads; tool-created
    PTYs fail the TTY check. This fallback does not persist a selection: a
    delayed hook must not rebind an old thread in the shared registry.
    """
    try:
        home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        row = thread_row(home, "thread_source, source, agent_path", sid)
        if not row or row[0] != "user" or row[1] not in ("cli", "vscode") or row[2]:
            return None
        pid = os.getppid()
        for _ in range(32):
            result = run(["ps", "-p", str(pid), "-o", "ppid=,command="])
            fields = result.stdout.strip().split(None, 1)
            if result.returncode or len(fields) != 2:
                return None
            # ps prints argv space-joined, never shell-quoted: a prompt such as
            # "don't" is not a syntax error, just words.
            parent, command = int(fields[0]), fields[1].split()
            if command and os.path.basename(command[0]) == "codex":
                role = codex_role(command[1:])
                if role is None:
                    return None
                if role == "frontend":
                    record = capture(pid, os.environ.get("TMUX", "").split(",")[0], os.environ.get("TMUX_PANE", ""))
                    if record:
                        token = direct_token(record)
                        return dict(record, session_id=sid, token=token, binding_id=token + ":" + sid,
                                    bound_at=0, direct=True)
                    return None
            if parent <= 1 or parent == pid:
                break
            pid = parent
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
        pass
    return None


def direct_token(record):
    """direct_owner()'s token: a hash of the frontend's pid, start, socket and pane."""
    identity = f'{record["frontend_pid"]}:{record["frontend_start"]}:{record["tmux_socket"]}:{record["pane"]}'
    return hashlib.sha256(identity.encode()).hexdigest()


def direct_valid(sid, token, record):
    """A direct_owner() record still names `sid`'s frontend, live on its pane.

    For callers that cannot re-run the ancestor walk, such as the tab
    indicator's detached title condenser, a tmux-server child. The token is
    re-derived from the record, so a record only vouches for the token of the
    process it describes, and valid() checks that process (same start time) is
    still on that pane's tty: an exited or replaced frontend fails.
    """
    try:
        return bool(isinstance(record, dict) and record.get("direct") is True
                    and SID.fullmatch(sid or "") and record.get("session_id") == sid
                    and token and record.get("token") == token
                    and direct_token(record) == token and valid(record))
    except (KeyError, TypeError, ValueError):
        return False


def release(token):
    with registry() as records:
        for sid, record in list(records.items()):
            if record.get("token") == token:
                del records[sid]


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
    commands = {"agents", "exec", "e", "review", "login", "logout", "mcp", "plugin", "app-server", "remote-control", "app",
                "completion", "update", "doctor", "sandbox", "debug", "apply", "queue", "archive", "delete",
                "migrate-rollouts", "unarchive", "cloud", "exec-server", "features", "help"}
    skip = False
    positional = None
    for arg in args:
        if skip:
            skip = False
        elif arg == "--":
            break
        elif arg in ("--help", "-h", "--version", "-V", "--remote") or arg.startswith("--remote="):
            return False
        elif arg in OPTION_OPERANDS:
            skip = True
        elif not arg.startswith("-") and positional is None:
            positional = arg
    return positional not in commands


def local_session_args(args):
    """Force interactive sessions onto their own backend, including resumes."""
    skip = False
    no_daemon = False
    for arg in args:
        if skip:
            skip = False
        elif arg == "--":
            break
        elif arg == "--remote" or arg.startswith("--remote="):
            raise SystemExit("codex: shared/remote servers are disabled; remove --remote")
        elif arg == "--no-daemon":
            no_daemon = True
        elif arg in OPTION_OPERANDS:
            skip = True
    if interactive_args(args) and not no_daemon:
        return ["--no-daemon"] + args
    return args


def launch(args):
    real = real_codex()
    os.execv(real, [real] + local_session_args(args))


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
    elif mode == "valid":
        # valid <sid> <token>, the resolved record on stdin; exit 0 if it holds.
        sid, token = args
        try:
            record = json.loads(sys.stdin.read())
        except ValueError:
            record = None
        raise SystemExit(0 if direct_valid(sid, token, record) else 1)


if __name__ == "__main__":
    main()
