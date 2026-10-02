#!/usr/bin/env python3
"""Repair save metadata from verified foreground owners, never cwd guesses."""
import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import tarfile
import tempfile

VALUE_FLAGS = {"-c", "--config", "--enable", "--disable", "-C", "--cd", "-m", "--model",
               "-p", "--profile", "-s", "--sandbox", "-a", "--ask-for-approval", "-i", "--image",
               "--add-dir", "--local-provider", "--remote"}
PICKERS = {"--last", "--all", "--include-noninteractive", "--include-non-interactive"}
SHELLS = {"zsh", "bash", "sh", "fish", "dash", "ksh", "tcsh", "csh", "nu"}


def run(arguments):
    return subprocess.run(arguments, capture_output=True, text=True, timeout=3, check=True).stdout.strip()


def load_owner(path):
    spec = importlib.util.spec_from_file_location("resurrect_owner", path)
    owner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(owner)
    return owner


def pane_snapshot(socket):
    fields = "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_current_path}\t#{pane_active}\t#{pane_current_command}\t#{pane_pid}"
    panes = {}
    for line in run(["tmux", "-S", socket, "list-panes", "-a", "-F", fields]).splitlines():
        pane, target, cwd, active, command, pid = line.split("\t")
        if not re.fullmatch(r"%\d+", pane) or not os.path.isabs(cwd) or active not in ("0", "1"):
            raise ValueError("Invalid live pane snapshot")
        panes[pane] = dict(target=target, cwd=cwd, active=active, command=command, pid=pid)
    return panes


def codex_args(command):
    words = shlex.split(command)
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
    if any(character in command for character in "'\"\\") or any(any(c.isspace() for c in word) for word in words):
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
        else:
            raise ValueError("Positional Codex arguments cannot be restored safely")
        index += 1
    return " ".join(kept), resume, model


def persisted_root(codex_home, sid):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(sid or "")):
        return False
    try:
        with sqlite3.connect((codex_home / "state_5.sqlite").as_uri() + "?mode=ro", uri=True, timeout=.3) as database:
            row = database.execute("SELECT thread_source,source,agent_path,rollout_path FROM threads WHERE id=?", (sid,)).fetchone()
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
    unresolved = []
    for entry in data["sessions"]:
        pid = str(entry.get("pid", ""))
        if entry.get("tool") == "codex" and pid.isdigit() and int(pid) not in accepted and owner.process_identity(int(pid)):
            unresolved.append(int(pid))
    if unresolved:
        raise ValueError("Unresolved living Codex frontend PIDs: " + ",".join(map(str, sorted(set(unresolved)))))
    data["sessions"] = sessions + sorted(rebuilt, key=lambda entry: entry["pane"])
    current = {pane["target"]: pane for pane in panes.values()}
    lines, fixed = [], 0
    for line in layout.splitlines(keepends=True):
        f = line.rstrip("\r\n").split("\t")
        if f[0] == "pane" and (len(f) < 8 or not f[7].startswith(":")):
            target = f"{f[1]}:{f[2]}.{f[5]}" if len(f) >= 6 else ""
            pane = current.get(target)
            if (len(f) != 11 or not pane or f[9] != pane["pid"]
                    or f[8] != pane["command"] or pane["command"] not in SHELLS or f[10] != ":"):
                raise ValueError("Malformed pane row cannot be verified against its live shell")
            f = f[:6] + [":", ":" + pane["cwd"], pane["active"], pane["command"], ":"]
            line = "\t".join(f) + ("\n" if line.endswith("\n") else "")
            fixed += 1
        lines.append(line)
    report = dict(codex=len(rebuilt), preserved=len(sessions), invalid_bindings=invalid, repaired_panes=fixed,
                  fallback_bindings=fallback)
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
