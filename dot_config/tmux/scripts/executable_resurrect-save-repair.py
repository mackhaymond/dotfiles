#!/usr/bin/env python3
"""Repair save metadata from verified foreground owners, never cwd guesses."""
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

VALUE_FLAGS = {"-c", "--config", "--enable", "--disable", "-C", "--cd", "-m", "--model",
               "-p", "--profile", "-s", "--sandbox", "-a", "--ask-for-approval", "-i", "--image",
               "--add-dir", "--local-provider", "--remote"}
PICKERS = {"--last", "--all", "--include-noninteractive", "--include-non-interactive"}
SHELLS = {"zsh", "bash", "sh", "fish", "dash", "ksh", "tcsh", "csh", "nu"}
# Native subcommands (codex-terminal-owner.interactive_args()): a first
# positional word naming one is not an interactive session's prompt.
SUBCOMMANDS = {"exec", "e", "review", "login", "logout", "mcp", "mcp-server", "plugin", "app-server",
               "remote-control", "app", "agents", "completion", "update", "doctor", "sandbox", "debug", "apply",
               "queue", "archive", "delete", "migrate-rollouts", "unarchive", "cloud", "exec-server", "features",
               "help"}


def run(arguments):
    return subprocess.run(arguments, capture_output=True, text=True, timeout=3, check=True).stdout.strip()


def load_owner(path):
    spec = importlib.util.spec_from_file_location("resurrect_owner", path)
    owner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(owner)
    return owner


def pane_snapshot(socket):
    fields = "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_current_path}\t#{pane_active}\t#{pane_current_command}\t#{pane_pid}\t:#{@stash_session}\t:#{@stash_pane_idx}\t:#{@stash_cwd}"
    panes = {}
    for line in run(["tmux", "-S", socket, "list-panes", "-a", "-F", fields]).splitlines():
        values = line.split("\t")
        pane, target, cwd, active, command, pid = values[:6]
        metadata = values[6:] or [":", ":", ":"]
        if len(metadata) != 3 or any(not value.startswith(":") for value in metadata):
            raise ValueError("Invalid live parked pane metadata")
        if not re.fullmatch(r"%\d+", pane) or not os.path.isabs(cwd) or active not in ("0", "1"):
            raise ValueError("Invalid live pane snapshot")
        panes[pane] = dict(target=target, cwd=cwd, active=active, command=command, pid=pid)
        panes[pane].update(zip(("stash_session", "stash_pane_idx", "stash_cwd"), (value[1:] for value in metadata)))
    return panes


def codex_args(command, drop_prompt=False):
    """(restorable flags, `resume` id, model) from a frontend's command line.

    drop_prompt: the thread is already known from evidence tied to the process,
    so an initial prompt is left out (resuming must not send it again). ps
    prints argv space-joined, never quoted, so the prompt is the first word
    that is not an option, operand or resume/fork id, plus everything after it:
    a later "-x" word may be prompt text, and a flag after a prompt is lost
    rather than guessed. Only the words kept must be unambiguous.
    """
    words = command.split() if drop_prompt else shlex.split(command)
    if not words:
        raise ValueError("Verified frontend has no command line")
    if Path(words[0]).name == "node":
        words = words[1:]
    if not words or Path(words[0]).name not in ("codex", "codex.js"):
        raise ValueError("Verified frontend is not a Codex executable")
    words = words[1:]
    if words and Path(words[0]).name in ("codex", "codex.js"):
        words = words[1:]
    # The upstream restore hook whitespace-splits cli_args, not shell-parses
    # them. Reject ambiguous quoting/spaces rather than change argument values.
    if not drop_prompt and (any(character in command for character in "'\"\\")
                            or any(any(c.isspace() for c in word) for word in words)):
        raise ValueError("Quoted/spaced Codex arguments require a token-aware restore hook")
    kept, resume, model, index = [], None, "", 0
    while index < len(words):
        word = words[index]
        if word in ("resume", "fork"):
            index += 1
            if index < len(words) and not words[index].startswith("-"):
                if word == "resume":
                    resume = words[index]
                index += 1
            continue
        if word in PICKERS:
            index += 1
            continue
        flag, separator, value = word.partition("=")
        if flag in VALUE_FLAGS:
            if not separator:
                index += 1
                if index >= len(words):
                    raise ValueError("Missing Codex option operand")
                value = words[index]
            if flag != "--remote" or not re.fullmatch(r"unix:///tmp/cx-tmux-[^/]+/s", value):
                kept.extend([word] if separator else [word, value])
            if flag in ("--model", "-m"):
                model = value
        elif word.startswith("-") and word != "--":
            kept.append(word)
        elif not drop_prompt:
            raise ValueError("Positional Codex arguments cannot be restored safely")
        elif word in SUBCOMMANDS:
            raise ValueError("A Codex subcommand is not an interactive session")
        else:
            break
        index += 1
    if drop_prompt and any(character in " ".join(words[:index]) for character in "'\"\\"):
        raise ValueError("Quoted Codex arguments require a token-aware restore hook")
    return " ".join(kept), resume, model


def thread_row(codex_home, sid):
    """Read-only lookup in Codex's WAL-mode state DB.

    With no Codex process holding the DB open there is no -shm file, and a
    plain mode=ro open cannot create one, so it fails with "unable to open
    database file" — which made every check here silently False. No -shm means
    no live writer and a fully checkpointed main file, so the retry reads that
    file as immutable.
    """
    uri = (codex_home / "state_5.sqlite").as_uri()
    query = "SELECT thread_source,source,agent_path,rollout_path FROM threads WHERE id=?"
    try:
        with sqlite3.connect(uri + "?mode=ro", uri=True, timeout=.3) as database:
            return database.execute(query, (sid,)).fetchone()
    except sqlite3.OperationalError:
        if (codex_home / "state_5.sqlite-shm").exists():
            raise
        with sqlite3.connect(uri + "?mode=ro&immutable=1", uri=True, timeout=.3) as database:
            return database.execute(query, (sid,)).fetchone()


def persisted_root(codex_home, sid):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(sid or "")):
        return False
    try:
        row = thread_row(codex_home, sid)
        if not row or row[0] != "user" or row[1] not in ("cli", "vscode") or row[2] or not row[3]:
            return False
        with Path(row[3]).open() as rollout:
            header = json.loads(rollout.readline(262144))
        return header.get("type") == "session_meta" and header.get("payload", {}).get("id") == sid
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return False


def selected_session(record, sid, resume, codex_home, tracker_dir):
    if persisted_root(codex_home, sid):
        return sid
    try:
        tracker = json.loads((tracker_dir / f"codex-{record['frontend_pid']}.json").read_text())
        env = tracker.get("env") or {}
        if (int(tracker["ppid"]) == int(record["frontend_pid"])
                and tracker["frontend_start"] == record["frontend_start"]
                and env.get("tmux_pane") == record["pane"]
                and os.path.realpath(env.get("tmux_socket", "")) == os.path.realpath(record["tmux_socket"])
                and persisted_root(codex_home, tracker.get("session_id"))):
            return tracker["session_id"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return resume if persisted_root(codex_home, resume) else None


ROLLOUT = re.compile(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
                     r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl")


def frontend_pids(pid):
    """The saved process and its direct children.

    The plugin saves the first Codex-looking process in the pane: for an npm
    install that is the `node …/codex` launcher, which spawns the native
    `codex` binary. Hooks resolve their owner to that child, so the tracker
    record and the window token name the child's pid, not the saved one.
    """
    try:
        return [pid] + [int(child) for child in run(["pgrep", "-P", str(pid)]).split()]
    except (ValueError, OSError, subprocess.SubprocessError):
        return [pid]


def direct_token(pid, frontend_start, socket, pane):
    """codex-terminal-owner.direct_owner()'s token for a --no-daemon frontend."""
    return hashlib.sha256(f"{pid}:{frontend_start}:{socket}:{pane}".encode()).hexdigest()


def window_of(target):
    return target.rsplit(".", 1)[0]


def codex_free_siblings(pane, panes, window_panes):
    """True when no other pane of `pane`'s window runs a Codex process.

    The tab indicator writes @agent_session_id, @agent_owner_token and
    @agent_rollout in separate tmux calls, so hooks from Codex frontends in two
    splits can interleave and leave one pane's token beside the other's thread.
    Window options can name this pane's thread only while it is the window's
    sole Codex. Unknown means no: a snapshot that disagrees with the window's
    pane count, or a process table that cannot be read.
    """
    window = window_of(panes[pane]["target"])
    siblings = [values for key, values in panes.items() if key != pane and window_of(values["target"]) == window]
    if len(siblings) + 1 != window_panes:
        return False
    if not siblings:
        return True
    children, commands = {}, {}
    for line in run(["ps", "-axo", "pid=,ppid=,command="]).splitlines():
        fields = line.split(None, 2)
        if len(fields) >= 2 and fields[0].isdigit() and fields[1].isdigit():
            children.setdefault(int(fields[1]), []).append(int(fields[0]))
            commands[int(fields[0])] = fields[2] if len(fields) > 2 else ""
    for sibling in siblings:
        if not str(sibling.get("pid", "")).isdigit():
            return False
        queue, seen = [int(sibling["pid"])], set()
        while queue:
            pid = queue.pop()
            if pid in seen:
                continue
            seen.add(pid)
            if any(Path(word).name in ("codex", "codex.js") for word in commands.get(pid, "").split()[:2]):
                return False
            queue.extend(children.get(pid, ()))
    return True


def live_owner(owner, pid, pane, panes, socket):
    """This living process, verified on `pane`'s terminal right now."""
    identity = owner.process_identity(pid)
    if (not identity or not identity.get("frontend_start") or pane not in panes
            or not str(panes[pane].get("pid", "")).isdigit()):
        return None
    record = dict(frontend_pid=pid, frontend_start=identity["frontend_start"], tty=identity.get("tty"),
                  pane=pane, pane_pid=int(panes[pane]["pid"]), tmux_socket=socket)
    return record if owner.valid(record) else None


def tracker_session(live, tracker_dir, codex_home):
    """codex-session-track's record, only if written for this very process.

    The tracker keys it by the frontend pid that codex-terminal-owner verified
    on the pane's tty; frontend_start rejects a pid since reused by another
    process, and the pane/socket must be the one the process is on now.
    """
    try:
        tracker = json.loads((tracker_dir / f"codex-{live['frontend_pid']}.json").read_text())
        env = tracker.get("env") or {}
        if (int(tracker["ppid"]) == live["frontend_pid"]
                and tracker["frontend_start"] == live["frontend_start"]
                and env.get("tmux_pane") == live["pane"]
                and os.path.realpath(env.get("tmux_socket") or "") == live["tmux_socket"]
                and persisted_root(codex_home, tracker.get("session_id"))):
            return tracker["session_id"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def rollout_session(live, panes, codex_home):
    """The thread in the window's @agent_rollout, only if this process put it there.

    The tab indicator writes @agent_rollout/@agent_session_id beside
    @agent_owner_token, the token of the owner it resolved. For a --no-daemon
    frontend that is codex-terminal-owner.direct_owner()'s token, a hash of
    pid, start time, socket and pane, so an option left by an earlier process
    does not match. The three are separate writes, so a second Codex split in
    the window can interleave its own: then none of them is trusted.
    """
    try:
        count, token, sid, rollout = run(["tmux", "-S", live["tmux_socket"], "display-message", "-p", "-t",
            live["pane"], "#{window_panes}\t#{@agent_owner_token}\t#{@agent_session_id}\t#{@agent_rollout}"]
            ).split("\t", 3)
        match = ROLLOUT.fullmatch(Path(rollout).name)
        if (not match or token != direct_token(live["frontend_pid"], live["frontend_start"], live["tmux_socket"],
                                               live["pane"])
                or sid not in ("", match[1]) or not persisted_root(codex_home, match[1])
                or not codex_free_siblings(live["pane"], panes, int(count))):
            return None
        row = thread_row(codex_home, match[1])
        if row and os.path.realpath(row[3]) == os.path.realpath(rollout):
            return match[1]
    except (OSError, ValueError, TypeError, sqlite3.Error, subprocess.SubprocessError):
        pass
    return None


def rollout_mtime(codex_home, sid):
    try:
        return os.stat(thread_row(codex_home, sid)[3]).st_mtime
    except (OSError, TypeError, IndexError, sqlite3.Error):
        return float("-inf")


def verified_session(entry, owner, panes, socket, tracker_dir, codex_home):
    """The thread a living frontend holds now, from records tied to that process.

    The tracker is rewritten only at SessionStart, which for /new arrives
    before the new thread is persisted, so it can still name the previous
    thread while the window options already name the new one. When both verify
    and disagree, the thread whose rollout was written last is the current one.
    Any failure to inspect the process leaves the row unverified (None).
    """
    try:
        matches = [pane for pane, values in panes.items() if values["target"] == entry.get("pane")]
        if len(matches) != 1:
            return None
        for pid in frontend_pids(int(entry["pid"])):
            live = live_owner(owner, pid, matches[0], panes, socket)
            if live:
                tracked = tracker_session(live, tracker_dir, codex_home)
                windowed = rollout_session(live, panes, codex_home)
                if tracked and windowed and tracked != windowed:
                    return max((windowed, tracked), key=lambda sid: rollout_mtime(codex_home, sid))
                if tracked or windowed:
                    return tracked or windowed
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        pass
    return None


def standalone_entry(entry, targets, used, codex_home, owner, panes, socket, tracker_dir, claimed, verified):
    """A living frontend with no relay binding, kept only on evidence tied to it.

    Since the wrapper forces --no-daemon (cfef2de), frontends get no relay
    binding, so every living one used to abort the whole repair — and the
    sidecar then kept the plugin's unverified rows for ALL Codex panes.
    repair() calls this twice per row, and every row's first pass runs before
    any row's second, so a thread verified for one process is never taken by
    another's weaker argv claim:
      verified=True: codex-session-track's record for this process
         (tracker_session) or the window's @agent_rollout owned by this
         process (rollout_session). A launch prompt is left out of cli_args.
      verified=False: `resume <id>` in its own --no-daemon command line, naming
         the thread the plugin saved — the binding fallback's evidence. It is
         last because argv names the launch thread, not one switched to later.
    Each must name a persisted root thread no other pane claims. Returns
    (row or None, whether this pass found a thread at all); a row nothing
    resolves is dropped by repair() and its pane restores as a plain shell.
    """
    if entry.get("pane") not in targets or entry.get("pane") in used:
        return None, False
    try:
        command = run(["ps", "-p", str(int(entry["pid"])), "-o", "command="])
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        return None, False
    if verified:
        selected = verified_session(entry, owner, panes, socket, tracker_dir, codex_home)
    else:
        selected = None
        try:
            arguments, resume, _ = codex_args(command)
            if ("--no-daemon" in arguments.split() and resume and resume == entry.get("session_id")
                    and persisted_root(codex_home, resume)):
                selected = resume
        except ValueError:
            pass
    if not selected:
        return None, False
    try:
        arguments, _, model = codex_args(command, drop_prompt=verified)
    except ValueError:
        return None, True
    if selected in claimed:
        return None, True
    return dict(pane=entry["pane"], tool="codex", session_id=selected, cwd=entry.get("cwd", ""),
                pid=str(entry["pid"]), model=model, cli_args=arguments, env=None), True


def repair(sidecar, layout, records, panes, socket, owner, codex_home, tracker_dir):
    socket = os.path.realpath(socket)
    data = json.loads(sidecar)
    sessions = [entry for entry in data["sessions"] if entry.get("tool") != "codex"]
    targets = {f"{f[1]}:{f[2]}.{f[5]}" for line in layout.splitlines()
               if (f := line.split("\t"))[0] == "pane" and len(f) >= 6}
    rebuilt, used, invalid, fallback = [], set(), 0, 0
    for sid, record in records.items():
        if (not isinstance(record, dict) or record.get("session_id", sid) != sid
                or not isinstance(record.get("tmux_socket"), str)
                or os.path.realpath(record.get("tmux_socket", "")) != socket
                or record.get("pane") not in panes or not owner.valid(record)):
            invalid += 1
            continue
        pane = panes[record["pane"]]
        if pane["target"] not in targets:
            invalid += 1
            continue
        arguments, resume, model = codex_args(run(["ps", "-p", str(record["frontend_pid"]), "-o", "command="]))
        selected = selected_session(record, sid, resume, codex_home, tracker_dir)
        if not selected or not owner.valid(record):
            invalid += 1
            continue
        if pane["target"] in used:
            raise ValueError("Multiple valid foreground bindings claim one pane")
        used.add(pane["target"])
        fallback += selected != sid
        rebuilt.append(dict(pane=pane["target"], tool="codex", session_id=selected, cwd=pane["cwd"],
                            pid=str(record["frontend_pid"]), model=model, cli_args=arguments, env=None))
    accepted = {int(entry["pid"]) for entry in rebuilt}
    candidates = [entry for entry in data["sessions"]
                  if entry.get("tool") == "codex" and str(entry.get("pid", "")).isdigit()
                  and int(entry["pid"]) not in accepted and owner.process_identity(int(entry["pid"]))]
    unresolved, standalone = [], 0
    for verified in (True, False):
        pending = []
        for entry in candidates:
            # A pane already kept (through its binding, or an earlier row) is
            # resolved even when the plugin saved another pid for it, e.g. the
            # npm launcher rather than the bound native child: skip, unreported.
            if entry.get("pane") in used:
                continue
            kept, found = standalone_entry(entry, targets, used, codex_home, owner, panes, socket, tracker_dir,
                                           {row["session_id"] for row in rebuilt}, verified)
            if kept:
                rebuilt.append(kept)
                used.add(kept["pane"])
                standalone += 1
            elif found or not verified:
                unresolved.append((int(entry["pid"]), str(entry.get("pane", ""))))
            else:
                pending.append(entry)
        candidates = pending
    # An unresolved living frontend (launched bare against the shared daemon,
    # or fresh with no persisted thread yet) used to abort the whole repair,
    # leaving every other pane, the archive and parked cwds unrepaired. Its row
    # is dropped instead (`sessions` above holds no plugin Codex rows), so that
    # pane restores as a plain shell rather than resuming a guessed thread.
    # Corrupt input below still aborts.
    if unresolved:
        print("resurrect save repair: dropped unresolved living Codex frontends: "
              + ", ".join(f"pid {pid} ({pane})" for pid, pane in sorted(set(unresolved))), file=sys.stderr)
    data["sessions"] = sessions + sorted(rebuilt, key=lambda entry: entry["pane"])
    current = {pane["target"]: pane for pane in panes.values()}
    lines, fixed, parked_cwds = [], 0, 0
    for line in layout.splitlines(keepends=True):
        f = line.rstrip("\r\n").split("\t")
        target = f"{f[1]}:{f[2]}.{f[5]}" if f[0] == "pane" and len(f) >= 6 else ""
        pane, changed = current.get(target), False
        if f[0] == "pane" and (len(f) < 8 or not f[7].startswith(":")):
            if (len(f) != 11 or not pane or f[9] != pane["pid"]
                    or f[8] != pane["command"] or pane["command"] not in SHELLS or f[10] != ":"):
                raise ValueError("Malformed pane row cannot be verified against its live shell")
            f = f[:6] + [":", ":" + pane["cwd"], pane["active"], pane["command"], ":"]
            changed = True
            fixed += 1
        # Window metadata applies only to its recorded pane, never another
        # split. A restored parked shell may currently be in HOME; retain the
        # explicitly recorded resume directory once this shell is verified.
        if (pane and len(f) == 11 and pane.get("stash_session", "").strip()
                and pane.get("stash_pane_idx") == f[5] and f[9] == pane["command"]
                and pane["command"] in SHELLS and f[10] == ":"
                and os.path.isabs(cwd := pane.get("stash_cwd", "")) and Path(cwd).is_dir()
                and f[7] != ":" + cwd):
            f[7], changed = ":" + cwd, True
            parked_cwds += 1
        if changed:
            line = "\t".join(f) + line[len(line.rstrip("\r\n")):]
        lines.append(line)
    report = dict(codex=len(rebuilt), preserved=len(sessions), invalid_bindings=invalid, repaired_panes=fixed,
                  fallback_bindings=fallback, parked_cwds=parked_cwds, standalone_resumes=standalone,
                  unresolved_dropped=len(set(unresolved)))
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode(), "".join(lines).encode(), used, report


def stripped_archive(path, targets):
    if not path.exists():
        return None, 0
    output, removed = io.BytesIO(), 0
    names = {f"pane_contents/pane-{target}" for target in targets}
    with tarfile.open(path, "r:gz") as source, tarfile.open(fileobj=output, mode="w:gz") as destination:
        for member in source:
            if member.name.removeprefix("./") in names:
                removed += 1
            else:
                destination.addfile(member, source.extractfile(member) if member.isfile() else None)
    return (output.getvalue() if removed else None), removed


def atomic_write(path, content):
    descriptor, temporary = tempfile.mkstemp(prefix=".repair-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resurrect-dir", type=Path, default=Path.home() / ".tmux/resurrect")
    parser.add_argument("--bindings", type=Path, default=Path.home() / ".cache/codex-terminal-owners/bindings.json")
    parser.add_argument("--owner-helper", type=Path, default=Path(__file__).with_name("codex-terminal-owner.py"))
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    tracker = os.environ.get("TMUX_ASSISTANT_RESURRECT_DIR") or str(Path(os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp") / "tmux-assistant-resurrect")
    parser.add_argument("--tracker-dir", type=Path, default=Path(tracker))
    parser.add_argument("--socket")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    socket = args.socket or os.environ.get("TMUX", "").rsplit(",", 2)[0]
    socket = os.path.realpath(socket or run(["tmux", "display-message", "-p", "#{socket_path}"]))
    sidecar, layout = args.resurrect_dir / "assistant-sessions.json", (args.resurrect_dir / "last").resolve(strict=True)
    records = json.loads(args.bindings.read_text()) if args.bindings.exists() else {}
    revised, repaired, targets, report = repair(sidecar.read_text(), layout.read_text(), records,
        pane_snapshot(socket), socket, load_owner(args.owner_helper), args.codex_home.resolve(), args.tracker_dir)
    archive = args.resurrect_dir / "pane_contents.tar.gz"
    contents, removed = stripped_archive(archive, targets)
    report.update(stripped_contents=removed, dry_run=args.dry_run)
    if not args.dry_run:
        for path, content in ((sidecar, revised), (layout, repaired), (archive, contents)):
            if content is not None and path.read_bytes() != content:
                atomic_write(path, content)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        raise SystemExit(f"resurrect save repair failed: {error}")
    except (OSError, sqlite3.Error, subprocess.SubprocessError, tarfile.TarError) as error:
        raise SystemExit(f"resurrect save repair failed: {type(error).__name__}; no command lines logged")
