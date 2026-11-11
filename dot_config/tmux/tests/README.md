# tmux config tests

Python tests (unittest style, run by pytest) for the scripts and conf files in
this directory's parent. Most start a private tmux server (`-L <unique>` or a
private `TMUX_TMPDIR`, always `-f /dev/null`) or a fake-tmux harness on a
private `PATH`; none touch the live server.

Run everything from the chezmoi source's `dot_config/tmux`.

## Running

| What | Command | Wall time (2026-11-10, 14 cores) |
|---|---|---|
| Smoke, everyday | `python -m pytest tests/ -m smoke -n 4 -q` | ~1 min 10 s (`-n auto` ~30 s, serial ~4 min) |
| Everything but slow | `python -m pytest tests/ -m "not slow" -n 4 -q` | (`-n auto` ~2 min 15 s) |
| Full | `python -m pytest tests/ -n 4 -q` | ~9 min (`-n auto` ~3.5-4 min) |
| Full, serial | `python -m pytest tests/ -q` | ~32 min |
| One file / test | `python -m pytest tests/test_spaces.py -k dashboard` | |

`-n 4` while Mack is using the machine; `-n auto` (14 workers) saturates
every core and is for an idle machine only. `-n` needs pytest-xdist
(`python -m pip install pytest-xdist`, into the same pyenv python as
pytest). Without it, drop `-n`.

Tests that cannot overlap each other even with private servers are grouped
onto one worker (`conftest.py`'s `GROUPS`, run with `--dist loadgroup`,
which conftest picks for you): today only `test_kill_guard.py`.

Many tests run the watcher or a hook in real time and count ticks or reads,
so a heavily loaded machine (load average far past the core count) can still
fail one of them in either mode. Rerun just the failures, serially, before
believing them: `python -m pytest tests/ --lf -q`.

A full `-n auto` run holds up to ~130 ptys at its peak (every test server's
panes and attached clients) on top of what the live terminals hold; macOS
allows 511 (`sysctl kern.tty.ptmx_max`). Another reason to cap the workers.

Markers, given in `conftest.py` so the test files stay plain unittest (each
still runs as `python tests/test_x.py`):

- `slow`: every test listed in `slow_tests.txt`, those that took 3 s or more
  on their own serially. Mostly watcher runs that wait out real ticks or GC
  grace, and typing sweeps against an attached client.
- `smoke`: every test in `conftest.py`'s `SMOKE_FILES` that is not slow:
  the indicator hooks and the watcher for each agent kind (claude, codex,
  copilot/agy, grok, opencode), the key catalog, the sidebar's python side
  (roster, detail, strip scope, popup x) and the launcher.

To refresh `slow_tests.txt` after the suite changes, run it serially with
`--durations=0` and keep the tests at 3 s or more:

```sh
python -m pytest tests/ -q --durations=0 > /tmp/durations.log
python - /tmp/durations.log > tests/slow_tests.new <<'EOF'
import collections, re, sys
t = collections.Counter()
for line in open(sys.argv[1]):
    m = re.match(r"\s*([\d.]+)s (?:call|setup|teardown)\s+tests/(\S+)", line)
    if m:
        t[m[2]] += float(m[1])
print("# Tests marked `slow` by conftest.py: >= 3 s alone, serially.")
for test, secs in sorted(t.items()):
    if secs >= 3:
        print(f"{secs:.1f}\t{test}")
EOF
mv tests/slow_tests.new tests/slow_tests.txt
```

## Rules for new tests

Every test must pass under `-n auto`: up to 14 run at once, in separate
processes, in any order.

- **A private server, always.** `-L` with a name made unique per test
  (`"<area>-" + uuid.uuid4().hex[:12]`), or a private `TMUX_TMPDIR` from
  `tempfile.mkdtemp()`, and `-f /dev/null`. Never a fixed `-L` name, never
  the default socket, never a bare `kill-server`: kill your own by `-L` or
  `-S <path>`. Drop `TMUX`/`TMUX_PANE` from the child env.
- **Private dirs.** Point `HOME`, `TMPDIR`, `XDG_STATE_HOME` and any
  script-specific state dir (see the harnesses) into a `tempfile` dir.
  Nothing under the real `~/.local/state`, `~/.tmux` or `~/.claude`, no fixed
  `/tmp/...` path, no file written next to the tests.
- **Scope process checks to your own processes.** A machine-wide
  `pgrep -f <script>` also sees the same script run by every other test
  server at that moment. Match a path only your test has (a script copied
  into your temp HOME), or walk your server's descendants
  (`test_spaces.py`'s `Server.syncs_running`).
- **Wait for the state, not a fixed time.** A `time.sleep()` long enough on
  an idle machine is not under `-n auto` load: poll for the condition with a
  deadline (the `wait`/`wait_for` helpers in the harnesses). Wait for the
  state you assert, not a neighbour of it: a script that sets two options in
  two tmux calls lands them apart under load. And make sure everything your
  setup set off has finished (hooks' `run-shell -b`, a title worker, a
  shell still starting) before you write state it could write over.
- No ports but port 0 (let the OS pick), and restore any `os.environ` you
  change (`mock.patch.dict`).
- A test of 3 s or more goes in `slow_tests.txt`. If it covers an area in
  `SMOKE_FILES`, a fast sibling should stay in smoke.
