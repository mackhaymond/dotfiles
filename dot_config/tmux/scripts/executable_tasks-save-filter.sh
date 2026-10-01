#!/usr/bin/env bash
# Run after assistant-resurrect saves its sidecar. The restore hook runs too
# early for a post-restore prune: it would already have resumed task panes.
set -euo pipefail
/usr/bin/python3 - <<'PY'
import json
import os
from pathlib import Path
import secrets

root = Path.home() / '.tmux/resurrect'
last = root / 'last'
sidecar = root / 'assistant-sessions.json'


def replace_if_changed(path, original, revised):
    if original == revised:
        return
    temp = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
    try:
        with open(temp, 'wb') as stream:
            stream.write(revised)
        os.chmod(temp, path.stat().st_mode & 0o777)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


if last.exists():
    # Rewrite the symlink target: replacing `last` itself would break resurrect's
    # pointer, including when it points to an identical prior save.
    save = last.resolve()
    original = save.read_bytes()
    kept = []
    for line in original.splitlines(keepends=True):
        fields = line.rstrip(b'\r\n').split(b'\t', 2)
        if len(fields) >= 2 and fields[0] in (b'pane', b'window', b'grouped_session') and fields[1] == b'tasks':
            continue
        kept.append(line)
    replace_if_changed(save, original, b''.join(kept))

if sidecar.exists():
    original = sidecar.read_bytes()
    data = json.loads(original)
    sessions = data.get('sessions', [])
    data['sessions'] = [row for row in sessions if not str(row.get('pane', '')).startswith('tasks:')]
    if len(data['sessions']) != len(sessions):
        replace_if_changed(sidecar, original, (json.dumps(data, separators=(',', ':')) + '\n').encode())
PY
