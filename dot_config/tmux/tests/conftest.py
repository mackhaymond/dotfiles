import os
from pathlib import Path

import pytest

# The tab scripts post a Darwin notification (com.mackhaymond.tmux.agent-state,
# the STATE NOTICE) on every state change, and the live CuaNotch re-reads the
# real tmux server on each one. Tests never post it: an empty name turns the
# notice off. Harnesses that build their env from scratch set it themselves.
os.environ["AGENT_STATE_NOTICE"] = ""

# MARKERS (tests/README.md). The test files stay plain unittest (each still
# runs as `python tests/test_x.py`), so the markers are given here, not in them:
#   slow   every test listed in slow_tests.txt (>= 3 s alone, serially)
#   smoke  every test of SMOKE_FILES that is not slow: the everyday run
HERE = Path(__file__).resolve().parent

# The agent-state hooks (indicator) and watcher for each kind, the key
# catalog, the sidebar's python side, and the launcher.
SMOKE_FILES = {
    "test_codex_indicator.py", "test_copilot_agy_indicator.py", "test_grok_indicator.py",
    "test_opencode_indicator.py", "test_agent_state_notice.py", "test_thread_at.py",
    "test_agent_jump_watcher.py", "test_codex_watcher_parity.py", "test_copilot_agy_watcher.py",
    "test_grok_watcher.py", "test_opencode_watcher.py",
    "test_keys_catalog.py",
    "test_agent_roster.py", "test_agent_detail.py", "test_strip_scope.py", "test_scope_cycle.py",
    "test_popup_x.py",
    "test_agent_launch.py", "test_codex_launchers.py",
}


# Files whose tests must never overlap EACH OTHER under pytest -n: each gets
# an xdist_group, so one worker runs all of them in turn (the others run the
# rest of the suite meanwhile). Only for what a private server cannot fix.
#   kill-guard: kill-guard.sh names the requester as the FIRST tmux CLI on the
#   machine whose argv says kill-server, so a second guarded kill-server in
#   flight (another worker's) gets named in this one's log line.
GROUPS = {"test_kill_guard.py": "kill-guard"}


def _slow_ids():
    ids = set()
    for line in (HERE / "slow_tests.txt").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            ids.add(line.split("\t", 1)[1].strip())
    return ids


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    config.addinivalue_line("markers", "slow: takes >= 3 s on its own (tests/slow_tests.txt)")
    config.addinivalue_line("markers", "smoke: the everyday subset (conftest.py SMOKE_FILES, minus slow)")
    config.addinivalue_line("markers", "xdist_group(name): run on one pytest-xdist worker (conftest.py GROUPS)")
    # -n's default scheduling is `load`; `loadgroup` is the same plus GROUPS.
    # (An explicit --dist is left alone.)
    if getattr(config.option, "dist", None) == "load":
        config.option.dist = "loadgroup"
    # Python 3.12 warns on every pty.fork() from a threaded process. Under
    # pytest-xdist every worker is (execnet's thread), so each attach-a-client
    # harness warns; the child execs tmux at once, so it is noise.
    config.addinivalue_line("filterwarnings",
                            "ignore:This process .* is multi-threaded, use of forkpty:DeprecationWarning")


@pytest.hookimpl(tryfirst=True)   # before xdist's own, which reads xdist_group
def pytest_collection_modifyitems(config, items):
    slow = _slow_ids()
    for item in items:
        # Relative to tests/, whatever the rootdir or the directory pytest ran from.
        name = Path(str(item.fspath)).name
        test_id = name + "::" + item.nodeid.split("::", 1)[1] if "::" in item.nodeid else name
        if test_id in slow:
            item.add_marker(pytest.mark.slow)
        elif name in SMOKE_FILES:
            item.add_marker(pytest.mark.smoke)
        if name in GROUPS:
            item.add_marker(pytest.mark.xdist_group(GROUPS[name]))
