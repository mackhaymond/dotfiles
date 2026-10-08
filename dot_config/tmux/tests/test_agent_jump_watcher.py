"""agent-tab-watcher.sh state parsing / @agent_since / pulse child, and agent-jump.sh.

Isolated HOME and TMPDIR, a fake `tmux`, `pgrep` and `ps` on PATH. No real tmux server,
watcher, or GUI is touched. Run with unittest.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

SCRIPTS = Path(os.environ.get(
    "AGENT_TMUX_SCRIPTS",
    str(Path.home() / ".local/share/chezmoi/dot_config/tmux/scripts")))
WATCHER = SCRIPTS / "executable_agent-tab-watcher.sh"
LIB = SCRIPTS / "agent-session-lib.sh"
JUMP = SCRIPTS / "executable_agent-jump.sh"
BASH_DIR = str(Path(shutil.which("bash") or "/opt/homebrew/bin/bash").parent)

FAKE_TMUX = r'''#!/usr/bin/env python3
import fcntl, json, os, re, sys
from pathlib import Path
p = Path(os.environ["FAKE_TMUX_STATE"])

def split_cmds(argv):
    cmds, cur = [], []
    for a in argv:
        if a == ";":
            cmds.append(cur); cur = []
        else:
            cur.append(a)
    cmds.append(cur)
    return [c for c in cmds if c]

def flag(c, f):
    return f in c

def arg(c, f):
    return c[c.index(f) + 1] if f in c else None

def render(fmt, v):
    # Substituted values are never re-expanded (as in tmux), so a label holding #{...} survives.
    v = dict(v, __label=v.get("@agent_summary") or v.get("window_name", ""))
    fmt = fmt.replace("#{?#{n:#{@agent_summary}},#{@agent_summary},#{window_name}}", "#{__label}")
    return re.sub(r"#\{([@\w]+)\}", lambda m: str(v.get(m.group(1), "")), fmt)

def wvars(s, wid):
    w = s["windows"][wid]
    v = {"window_id": wid, "session_name": w["session"], "window_index": w["index"],
         "window_name": w.get("name", "zsh"), "window_active_clients": w.get("active_clients", 0)}
    v.update(w.get("opts", {}))
    return v

with p.with_suffix(".lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    s = json.loads(p.read_text())
    rc, out = 0, []
    for c in split_cmds(sys.argv[1:]):
        s["calls"].append(c)
        cmd = c[0]
        if cmd == "list-windows":
            for wid in s["windows"]:
                out.append(render(arg(c, "-F"), wvars(s, wid)))
                # A linked window ("links": [sessions]) is listed once per session, like -a does.
                for ls in s["windows"][wid].get("links", []):
                    v = wvars(s, wid); v["session_name"] = ls
                    out.append(render(arg(c, "-F"), v))
        elif cmd == "list-panes":
            for pn in s["panes"]:
                v = wvars(s, pn["window"]); v["pane_tty"] = pn["tty"]
                out.append(render(arg(c, "-F"), v))
                # -a walks sessions, so a linked window's panes are listed once per link too.
                for ls in s["windows"][pn["window"]].get("links", []):
                    v = dict(v, session_name=ls)
                    out.append(render(arg(c, "-F"), v))
        elif cmd == "list-clients":
            for cl in s["clients"]:
                out.append(render(arg(c, "-F"), {"client_tty": cl["tty"], "session_name": cl["session"],
                                                 "window_id": cl["window"]}))
        elif cmd == "list-sessions":
            out.extend(sorted({w["session"] for w in s["windows"].values()}))
        elif cmd == "show-options":
            name = c[-1]
            if flag(c, "-gqv"):
                out.append(s["globals"].get(name, ""))
        elif cmd == "set-option":
            unset = any(x in c for x in ("-uw", "-gu", "-u"))
            glob = any(x in c for x in ("-g", "-gu"))
            t = arg(c, "-t")
            rest = [x for x in c[1:] if not x.startswith("-")]
            if t: rest.remove(t)
            name = rest[0]; val = rest[1] if len(rest) > 1 else ""
            if glob and not t:
                if unset: s["globals"].pop(name, None)
                else: s["globals"][name] = val
            else:
                if t not in s["windows"]: rc = 1; continue
                o = s["windows"][t].setdefault("opts", {})
                if unset: o.pop(name, None)
                else: o[name] = val
        elif cmd == "display-message":
            if flag(c, "-p"):
                t = arg(c, "-t")
                if t not in s["windows"]: rc = 1; continue
                v = wvars(s, t)
                # Real tmux resolves a linked window to its most recently active session; say the last link.
                if s["windows"][t].get("links"): v["session_name"] = s["windows"][t]["links"][-1]
                out.append(render(c[-1], v))
            else:
                s["messages"].append([arg(c, "-c"), c[-1]])
        elif cmd == "select-window":
            # A failed command aborts the rest of the list, as in tmux (no switch-client after it).
            t = arg(c, "-t")
            if ":" in t:                                    # =session:@id: must be linked there
                sess, t = t.lstrip("=").split(":", 1)
                w = s["windows"].get(t)
                if not w or sess not in [w["session"]] + w.get("links", []): rc = 1; break
                s["active"][sess] = t; continue
            if t not in s["windows"]: rc = 1; break
            s["active"][s["windows"][t]["session"]] = t
        elif cmd == "switch-client":
            tty, sess = arg(c, "-c"), arg(c, "-t").lstrip("=")
            for cl in s["clients"]:
                if cl["tty"] == tty:
                    cl["session"] = sess; cl["window"] = s["active"].get(sess, cl["window"])
    p.write_text(json.dumps(s))
    if out: print("\n".join(out))
    sys.exit(rc)
'''

# The process table both fakes read: "tty pid comm" lines in $FAKE_PROCS.
DEFAULT_PROCS = "ttys900 4242 claude\nttys901 4343 zsh\n"

# The watcher's agent discovery is `pgrep -ax PATTERN`, matched the way macOS
# pgrep does: -x against the comm basename; exit 1 = nothing matched. Any -f
# call (the startup singleton sweep) goes to the real pgrep. FAKE_PGREP_RC
# forces an exit status (>= 2 = a real pgrep error).
FAKE_PGREP = r"""#!/bin/sh
for a in "$@"; do
    case "$a" in -*f*) exec /usr/bin/pgrep "$@" ;; esac
done
[ -n "$FAKE_PGREP_RC" ] && exit "$FAKE_PGREP_RC"
for pat; do :; done
rc=1
while read -r tty pid comm; do
    [ -n "$pid" ] || continue
    if printf '%s\n' "${comm##*/}" | grep -Eqx -- "$pat"; then
        echo "$pid"; rc=0
    fi
done < "$FAKE_PROCS"
exit $rc
"""

# Single-pid `ps -o tty=,pid=,comm= -p PID` answers from $FAKE_PROCS (and is
# logged to $FAKE_PS_LOG); an all-process listing is refused loudly so a
# regression to `ps -ax` fails the tests; anything else (the startup
# identity check, `ps -o command= -p`) is the real ps.
FAKE_PS = r"""#!/bin/sh
if [ $# = 4 ] && [ "$1" = -o ] && [ "$2" = tty=,pid=,comm= ] && [ "$3" = -p ]; then
    [ -n "$FAKE_PS_LOG" ] && echo "$4" >> "$FAKE_PS_LOG"
    while read -r tty pid comm; do
        [ "$pid" = "$4" ] && { echo "$tty $pid $comm"; exit 0; }
    done < "$FAKE_PROCS"
    exit 1
fi
case " $* " in *" -ax "*|*" -A "*|*" -e "*) echo "fake ps: all-process listing: $*" >&2; exit 3 ;; esac
exec /bin/ps "$@"
"""

# A pass-through `stat` that logs its argv, one line per call, so a test can
# count the forks the watcher (and the session lib) still make.
FAKE_STAT = r"""#!/bin/sh
[ -n "$FAKE_STAT_LOG" ] && echo "$*" >> "$FAKE_STAT_LOG"
exec /usr/bin/stat "$@"
"""


class FakeEnv:
    def __init__(self, windows, panes=(), clients=(), active=None):
        self.dir = Path(tempfile.mkdtemp(prefix="agentjump-"))
        self.home = self.dir / "home"; self.tmp = self.dir / "tmp"; self.bin = self.dir / "bin"
        for d in (self.home, self.tmp, self.bin):
            d.mkdir()
        scripts = self.home / ".config/tmux/scripts"; scripts.mkdir(parents=True)
        shutil.copy(WATCHER, scripts / "agent-tab-watcher.sh")
        shutil.copy(LIB, scripts / "agent-session-lib.sh")
        shutil.copy(JUMP, scripts / "agent-jump.sh")
        self.scripts = scripts
        for name, body in (("tmux", FAKE_TMUX), ("ps", FAKE_PS), ("pgrep", FAKE_PGREP)):
            f = self.bin / name; f.write_text(body); f.chmod(0o755)
        self.procs = self.dir / "procs"; self.procs.write_text(DEFAULT_PROCS)
        self.ps_log = self.dir / "ps.log"
        self.state = self.dir / "tmux.json"
        self.state.write_text(json.dumps({
            "windows": windows, "panes": list(panes), "clients": list(clients),
            "active": active or {}, "globals": {}, "calls": [], "messages": []}))

    def env(self, **extra):
        e = {"HOME": str(self.home), "TMPDIR": str(self.tmp) + "/",
             "PATH": f"{self.bin}:{BASH_DIR}:/usr/bin:/bin", "FAKE_TMUX_STATE": str(self.state),
             "FAKE_PROCS": str(self.procs), "FAKE_PS_LOG": str(self.ps_log)}
        e.update(extra)
        return e

    def run(self, script, *args, timeout=30, **extra):
        return subprocess.run(["bash", str(self.scripts / script), *args], env=self.env(**extra),
                              capture_output=True, text=True, timeout=timeout)

    def read(self):
        return json.loads(self.state.read_text())

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def W(session, index, **opts):
    return {"session": session, "index": index, "opts": {k.replace("_", "@agent_", 1) if k.startswith("_") else k: v
                                                        for k, v in opts.items()}}


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.f = FakeEnv(
            windows={
                "@1": W("main", 1),                                   # agent pane, no state yet
                "@2": W("main", 2),                                   # plain shell, no state
                "@3": W("main", 3, **{"@agent_state": "running", "@agent_since": "100 running"}),
                "@4": W("main", 4, **{"@agent_since": "5 idle"}),     # stale stamp, no state
            },
            panes=[{"window": "@1", "tty": "/dev/ttys900"}, {"window": "@2", "tty": "/dev/ttys901"},
                   {"window": "@3", "tty": "/dev/ttys900"}, {"window": "@4", "tty": "/dev/ttys901"}])

    def tearDown(self):
        self.f.close()

    def test_empty_state_is_a_field(self):
        r = self.f.run("agent-tab-watcher.sh", AGENT_TAB_WATCHER_MAX_TICKS="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.f.read()
        # Agent window with no state is seeded (the misparse made state "0" and skipped this).
        self.assertEqual(s["windows"]["@1"]["opts"].get("@agent_state"), "idle")
        self.assertRegex(s["windows"]["@1"]["opts"].get("@agent_since", ""), r"^\d+ idle$")
        # A plain shell window is left completely alone: no GC unsets at all.
        self.assertFalse([c for c in s["calls"] if c[0] == "set-option" and "@2" in c], s["calls"])
        # A stamp whose state matches is not rewritten; a stamp with no state is removed.
        self.assertEqual(s["windows"]["@3"]["opts"]["@agent_since"], "100 running")
        self.assertNotIn("@agent_since", s["windows"]["@4"]["opts"])
        # Something is running, so the pulse flag is raised; the pidfile is ours to clean up.
        self.assertTrue(Path(str(self.f.tmp) + "/agent-tab-blink." + str(os.getuid())).exists())
        self.assertFalse(Path(str(self.f.tmp) + "/agent-tab-watcher." + str(os.getuid()) + ".pid").exists())

    def test_stamp_follows_a_state_change(self):
        s = json.loads(self.f.state.read_text())
        s["windows"]["@3"]["opts"]["@agent_state"] = "done"
        self.f.state.write_text(json.dumps(s))
        self.f.run("agent-tab-watcher.sh", AGENT_TAB_WATCHER_MAX_TICKS="1")
        since = self.f.read()["windows"]["@3"]["opts"]["@agent_since"]
        self.assertRegex(since, r"^\d+ done$")
        self.assertGreater(int(since.split()[0]), 100)

    def test_pulse_has_its_own_clock(self):
        t0 = time.time()
        r = self.f.run("agent-tab-watcher.sh", AGENT_TAB_WATCHER_MAX_TICKS="3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.time() - t0, 10)
        calls = self.f.read()["calls"]
        toggles = [c for c in calls if c[:2] == ["set-option", "-g"] and "@agent_blink" in c]
        self.assertGreaterEqual(len(toggles), 1, calls)
        # Each toggle redraws in the same tmux invocation.
        self.assertTrue(any(c[0] == "refresh-client" for c in calls))
        # The child is gone once the parent exits (nothing left matching its argv).
        time.sleep(1.5)
        left = subprocess.run(["pgrep", "-f", str(self.f.scripts / "agent-tab-watcher.sh")],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(left, "")

    def window_calls(self, s, wid):
        return [c for c in s["calls"] if c[0] == "set-option" and wid in c]

    def test_zero_agents_is_not_a_failed_tick(self):
        # pgrep exits 1 when nothing matches: a normal tick, not a failure. A
        # failed tick skips the MAX_TICKS countdown, so a misread would hang
        # here instead of returning.
        self.f.procs.write_text("ttys901 4343 zsh\n")
        r = self.f.run("agent-tab-watcher.sh", timeout=15, AGENT_TAB_WATCHER_MAX_TICKS="3")
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.f.read()
        # The ticks really reconciled (the stale stamp on @4 was removed) ...
        self.assertNotIn("@agent_since", s["windows"]["@4"]["opts"])
        # ... but nothing is seeded, and the running window is NOT collected
        # before GC_TICKS agent-less ticks in a row.
        self.assertNotIn("@agent_state", s["windows"]["@1"]["opts"])
        self.assertEqual(s["windows"]["@3"]["opts"].get("@agent_state"), "running")
        self.assertFalse(self.window_calls(s, "@2"), s["calls"])
        # Past GC_TICKS it is collected: the agent really is gone.
        r = self.f.run("agent-tab-watcher.sh", timeout=15, AGENT_TAB_WATCHER_MAX_TICKS="6")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("@agent_state", self.f.read()["windows"]["@3"]["opts"])

    def test_pgrep_error_is_a_failed_tick(self):
        # Exit >= 2 is a real error: the tick is skipped whole (no seed, no
        # GC, no stamp edits), and it never counts toward MAX_TICKS.
        with self.assertRaises(subprocess.TimeoutExpired):
            self.f.run("agent-tab-watcher.sh", timeout=3, AGENT_TAB_WATCHER_MAX_TICKS="1",
                       FAKE_PGREP_RC="2")
        time.sleep(1.5)   # the orphaned pulse child retires once its parent is gone
        s = self.f.read()
        for wid in ("@1", "@2", "@3", "@4"):
            self.assertFalse(self.window_calls(s, wid), s["calls"])

    def test_tty_lookup_is_cached_per_pid(self):
        # Steady state is one pgrep per tick: each agent pid gets ONE ps, ever
        # (non-matching processes never get one at all).
        r = self.f.run("agent-tab-watcher.sh", AGENT_TAB_WATCHER_MAX_TICKS="3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.f.ps_log.read_text().split(), ["4242"])
        self.assertEqual(self.f.read()["windows"]["@1"]["opts"].get("@agent_state"), "idle")

    def test_codex_by_path_basename(self):
        # A path comm counts by its basename; a tty-less agent is skipped.
        self.f.procs.write_text("ttys901 5151 /x/vendor/aarch64/bin/codex\n?? 6161 claude\n")
        r = self.f.run("agent-tab-watcher.sh", AGENT_TAB_WATCHER_MAX_TICKS="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.f.read()
        self.assertEqual(s["windows"]["@2"]["opts"].get("@agent_state"), "idle")
        self.assertNotIn("@agent_state", s["windows"]["@1"]["opts"])

    def trace_path(self):
        return Path(str(self.f.tmp) + "/agent-tab-watcher." + str(os.getuid()) + ".trace")

    def test_no_trace_unless_the_file_exists(self):
        r = self.f.run("agent-tab-watcher.sh", AGENT_TAB_WATCHER_MAX_TICKS="2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.trace_path().exists())

    def test_trace_one_line_per_tick(self):
        self.trace_path().write_text("")
        t0 = int(time.time())
        r = self.f.run("agent-tab-watcher.sh", AGENT_TAB_WATCHER_MAX_TICKS="3")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = self.trace_path().read_text().splitlines()
        self.assertEqual(len(lines), 3, lines)
        for ln in lines:
            # <epoch> <tick_ms> <windows> <agents>: 4 windows, 1 tty-owning agent.
            self.assertRegex(ln, r"^\d+ \d+ 4 1$")
            epoch, ms = int(ln.split()[0]), int(ln.split()[1])
            self.assertTrue(t0 - 1 <= epoch <= time.time() + 1, ln)
            self.assertLess(ms, 10000, ln)

    # --- the one-read tick, the idle fast path, and the change gates ---

    def edit_state(self, fn):
        s = self.f.read(); fn(s); self.f.state.write_text(json.dumps(s))

    def tmux_reads(self, s):
        return [c for c in s["calls"] if c[0] in ("list-windows", "list-panes")]

    def stat_log(self):
        p = self.f.dir / "stat.log"
        return p.read_text().splitlines() if p.exists() else []

    def with_fake_stat(self):
        f = self.f.bin / "stat"; f.write_text(FAKE_STAT); f.chmod(0o755)
        return {"FAKE_STAT_LOG": str(self.f.dir / "stat.log")}

    def later(self, delay, fn):
        t = threading.Timer(delay, fn); t.start()
        self.addCleanup(t.cancel)

    def test_one_read_keeps_spaced_summaries_and_empty_middle_fields(self):
        # Every per-window field arrives in ONE US-separated read. Each of
        # these windows has an EMPTY field before its one set field, so a
        # read that dropped or merged empty fields would shift it into the
        # wrong variable; the summary has runs of spaces and a tab.
        def add(s):
            s["windows"]["@5"] = W("main", 5, **{"@agent_summary": "fix  the login\tbug "})
            s["windows"]["@6"] = W("main", 6, **{"@agent_workflow": "1"})
            s["windows"]["@7"] = W("main", 7, **{"@agent_cua": "1", "@agent_summary": "a b"})
            s["windows"]["@8"] = W("main", 8, **{"@agent_rollout": "/r/rollout-1.jsonl"})
            s["panes"] += [{"window": w, "tty": "/dev/ttys80%s" % w[1:]} for w in ("@5", "@6", "@7", "@8")]
        self.edit_state(add)
        r = self.f.run("agent-tab-watcher.sh", timeout=20, AGENT_TAB_WATCHER_MAX_TICKS="6")
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.f.read()
        self.assertEqual(len(self.tmux_reads(s)), 6, self.tmux_reads(s))   # one read per tick
        self.assertEqual({c[0] for c in self.tmux_reads(s)}, {"list-panes"})
        # No agent anywhere here: a leftover workflow/cua flag is cleared on
        # the first tick, and since @6 has no state or summary that is ALL
        # that ever happens to it (it is on the idle fast path afterwards).
        self.assertEqual(self.window_calls(s, "@6"), [["set-option", "-uw", "-t", "@6", "@agent_workflow"]])
        self.assertNotIn("@agent_cua", s["windows"]["@7"]["opts"])
        # A summary (spaces and all) is a summary: collected after GC_TICKS.
        self.assertNotIn("@agent_summary", s["windows"]["@5"]["opts"])
        self.assertNotIn("@agent_summary", s["windows"]["@7"]["opts"])
        # A lone rollout path is neither state nor summary: nothing to collect.
        self.assertFalse(self.window_calls(s, "@8"), s["calls"])

    def test_idle_windows_cost_nothing_and_agents_panes_still_reconcile(self):
        # 20 pty-MCP shells in `agents`: no agent, no options -> not one write.
        # @150 is an `agents` pane running a headless `claude -p`: it must be
        # seeded and stamped like any agent window, and collected once it
        # exits. @1 is linked into stash as well: listed twice, reconciled once.
        def add(s):
            for i in range(20):
                wid = "@%d" % (100 + i)
                s["windows"][wid] = W("agents", i + 1)
                s["panes"].append({"window": wid, "tty": "/dev/ttys%d" % (700 + i)})
            s["windows"]["@150"] = W("agents", 50)
            s["panes"].append({"window": "@150", "tty": "/dev/ttys950"})
            s["windows"]["@1"]["links"] = ["stash"]
        self.edit_state(add)
        self.f.procs.write_text(DEFAULT_PROCS + "ttys950 5050 claude\n")
        self.trace_path().write_text("")
        r = self.f.run("agent-tab-watcher.sh", AGENT_TAB_WATCHER_MAX_TICKS="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.f.read()
        for i in range(20):
            self.assertFalse(self.window_calls(s, "@%d" % (100 + i)), s["calls"])
        self.assertEqual(s["windows"]["@150"]["opts"].get("@agent_state"), "idle")
        self.assertRegex(s["windows"]["@150"]["opts"].get("@agent_since", ""), r"^\d+ idle$")
        seeds = [c for c in self.window_calls(s, "@1") if "@agent_state" in c]
        self.assertEqual(seeds, [["set-option", "-w", "-t", "@1", "@agent_state", "idle"]])
        # Windows are counted once each: 4 + 20 + 1, the link not again.
        self.assertRegex(self.trace_path().read_text(), r"^\d+ \d+ 25 2\n$")
        # The headless claude exits: collected after GC_TICKS, like any window.
        self.f.procs.write_text(DEFAULT_PROCS)
        r = self.f.run("agent-tab-watcher.sh", timeout=20, AGENT_TAB_WATCHER_MAX_TICKS="6")
        self.assertEqual(r.returncode, 0, r.stderr)
        o = self.f.read()["windows"]["@150"]["opts"]
        self.assertNotIn("@agent_state", o)
        self.assertNotIn("@agent_since", o)

    def make_session(self, pid=4242, sid="sid1"):
        """A claude session for `pid` with one fresh background subagent."""
        home = self.f.home
        (home / ".claude/sessions").mkdir(parents=True, exist_ok=True)
        (home / (".claude/sessions/%d.json" % pid)).write_text(json.dumps(
            {"pid": pid, "sessionId": sid, "cwd": "/x/proj", "status": "busy"}, separators=(",", ":")))
        proj = home / ".claude/projects/-x-proj"
        (proj / sid / "subagents").mkdir(parents=True, exist_ok=True)
        (proj / (sid + ".jsonl")).write_text('{"type":"user","message":"go"}\n')
        agent = proj / sid / "subagents/agent-abc123.jsonl"
        agent.write_text('{"type":"assistant","message":"working"}\n')
        return agent

    def lib_stats(self):
        return [l for l in self.stat_log() if l.startswith("-f %m %z %N")]

    def test_subagent_check_only_reruns_when_its_inputs_change(self):
        self.make_session()
        r = self.f.run("agent-tab-watcher.sh", timeout=20, AGENT_TAB_WATCHER_MAX_TICKS="4",
                       **self.with_fake_stat())
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.f.read()
        # Running subagent -> gear, on both of 4242's windows (@1 and @3) ...
        self.assertEqual(s["windows"]["@1"]["opts"].get("@agent_workflow"), "1")
        self.assertEqual(s["windows"]["@3"]["opts"].get("@agent_workflow"), "1")
        # ... from ONE look at the session over four ticks: nothing it reads moved.
        self.assertEqual(len(self.lib_stats()), 1, self.stat_log())

    def test_subagent_check_sees_a_change_mid_run(self):
        agent = self.make_session()
        def interrupt():
            with agent.open("a") as fh:
                fh.write('{"type":"user","message":{"role":"user","content":[{"type":"text",'
                         '"text":"[Request interrupted by user]"}]}}\n')
        self.later(2.0, interrupt)
        r = self.f.run("agent-tab-watcher.sh", timeout=20, AGENT_TAB_WATCHER_MAX_TICKS="5",
                       **self.with_fake_stat())
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.f.read()
        # The gear went up, and came down once the interrupt landed.
        self.assertIn(["set-option", "-w", "-t", "@1", "@agent_workflow", "1"], s["calls"])
        self.assertNotIn("@agent_workflow", s["windows"]["@1"]["opts"])
        self.assertGreaterEqual(len(self.lib_stats()), 2, self.stat_log())

    def test_subagent_check_new_session_behind_same_pid(self):
        # /clear: same pid, new sessionId with no subagents -> no gear, at once,
        # even though none of the NEW session's (absent) files "changed".
        self.make_session()
        def clear():
            (self.f.home / ".claude/sessions/4242.json").write_text(json.dumps(
                {"pid": 4242, "sessionId": "sid2", "cwd": "/x/proj", "status": "busy"}, separators=(",", ":")))
        self.later(2.0, clear)
        r = self.f.run("agent-tab-watcher.sh", timeout=20, AGENT_TAB_WATCHER_MAX_TICKS="5")
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.f.read()
        self.assertIn(["set-option", "-w", "-t", "@1", "@agent_workflow", "1"], s["calls"])
        self.assertNotIn("@agent_workflow", s["windows"]["@1"]["opts"])

    def activity(self, ts):
        act = self.f.home / "Library/Application Support/CuaNotch/activity.json"
        act.parent.mkdir(parents=True, exist_ok=True)
        act.write_text(json.dumps({"sessions": {"s1": {"agent_pid": 4242, "ts": ts}}}))
        return act

    def test_cua_flag_follows_activity_stat_only_on_change(self):
        act = self.activity(time.time())
        # Mid-run the shim rewrites it with only a stale session: the flag
        # must clear on the next tick (the change is seen via the stamps).
        self.later(2.0, lambda: act.write_text(json.dumps(
            {"sessions": {"s1": {"agent_pid": 4242, "ts": time.time() - 120}}})))
        r = self.f.run("agent-tab-watcher.sh", timeout=20, AGENT_TAB_WATCHER_MAX_TICKS="5",
                       **self.with_fake_stat())
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.f.read()
        self.assertIn(["set-option", "-w", "-t", "@1", "@agent_cua", "1"], s["calls"])
        self.assertIn(["set-option", "-uw", "-t", "@1", "@agent_cua"], s["calls"])
        self.assertNotIn("@agent_cua", s["windows"]["@1"]["opts"])
        # Stat'ed at the cold start and once for the change, not once per tick.
        self.assertEqual(len([l for l in self.stat_log() if "activity.json" in l]), 2, self.stat_log())

    def test_cua_rename_with_an_old_mtime_is_seen(self):
        # Writers replace activity.json by rename, and the new file keeps its
        # temp file's mtime - here one that predates every stamp of the run.
        # Python first answers "nobody" (cached); the rename must still be
        # noticed (via the directory) and re-read.
        act = self.activity(time.time() - 120)
        def replace():
            tmp = act.with_name(".activity.tmp")
            tmp.write_text(json.dumps({"sessions": {"s1": {"agent_pid": 4242, "ts": time.time()}}}))
            old = time.time() - 30
            os.utime(tmp, (old, old))
            os.replace(tmp, act)
        self.later(2.0, replace)
        r = self.f.run("agent-tab-watcher.sh", timeout=20, AGENT_TAB_WATCHER_MAX_TICKS="5")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.f.read()["windows"]["@1"]["opts"].get("@agent_cua"), "1")

    def test_cua_failed_interpreter_is_not_cached_as_nobody(self):
        # The first interpreter run fails (EAGAIN / jetsam stand-in); the
        # next tick must ask again rather than cache " " for the 60 s window.
        self.activity(time.time())
        count = self.f.dir / "py.count"
        fake = self.f.bin / "flaky-python3"
        fake.write_text('#!/bin/sh\nn=$(cat "%s" 2>/dev/null || echo 0); echo $((n + 1)) > "%s"\n'
                        'if [ "$n" = 0 ]; then cat >/dev/null; exit 1; fi\nexec /usr/bin/python3 "$@"\n'
                        % (count, count))
        fake.chmod(0o755)
        w = self.f.scripts / "agent-tab-watcher.sh"
        body = w.read_text()
        anchor = '/usr/bin/python3 - "$ACTIVITY"'
        self.assertIn(anchor, body)
        w.write_text(body.replace(anchor, '%s - "$ACTIVITY"' % fake))
        r = self.f.run("agent-tab-watcher.sh", timeout=20, AGENT_TAB_WATCHER_MAX_TICKS="3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreaterEqual(int(count.read_text()), 2)
        self.assertEqual(self.f.read()["windows"]["@1"]["opts"].get("@agent_cua"), "1")

    def test_stamps_are_cleaned_up(self):
        r = self.f.run("agent-tab-watcher.sh", AGENT_TAB_WATCHER_MAX_TICKS="2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([p.name for p in self.f.tmp.iterdir() if ".stamp." in p.name], [])


class JumpTests(unittest.TestCase):
    TTY = "/dev/ttys000"

    def make(self, **extra_windows):
        windows = {
            "@10": W("main", 1),                                                        # where we are
            "@11": W("main", 2, **{"@agent_state": "done", "@agent_since": "300 done"}),
            "@12": W("bai", 2, **{"@agent_state": "needs-input", "@agent_since": "200 needs-input"}),
            "@13": W("work", 1, **{"@agent_state": "failed", "@agent_since": "400 failed"}),
            "@14": W("work", 2, **{"@agent_state": "done", "@agent_workflow": "1", "@agent_since": "50 done"}),
            "@15": W("stash", 1, **{"@agent_state": "failed", "@agent_since": "10 failed"}),
            "@16": W("tasks", 1, **{"@agent_state": "done", "@agent_since": "10 done"}),
            "@17": W("bai", 3, **{"@agent_state": "needs-input"}),                     # no stamp: last in tier
        }
        windows.update(extra_windows)
        self.f = FakeEnv(windows, clients=[{"tty": self.TTY, "session": "main", "window": "@10"}],
                         active={"main": "@10", "bai": "@12", "work": "@13"})
        return self.f

    def tearDown(self):
        self.f.close()

    def test_list_order_and_exclusions(self):
        out = self.make().run("agent-jump.sh", "list").stdout.split("\n")
        wins = [l.split("\t")[0] for l in out if l]
        self.assertEqual(wins, ["@13", "@12", "@17", "@11"])

    def test_next_selects_before_switching(self):
        f = self.make()
        f.run("agent-jump.sh", "next", self.TTY)
        s = f.read()
        moves = [c[0] for c in s["calls"] if c[0] in ("select-window", "switch-client")]
        self.assertEqual(moves, ["select-window", "switch-client"])
        self.assertEqual(s["clients"][0]["window"], "@13")
        self.assertEqual(s["globals"]["@agent_jump__dev_ttys000"], "@10 @13")
        self.assertIn("· failed · 3 more", s["messages"][-1][1])

    def test_chain_and_back(self):
        f = self.make()
        f.run("agent-jump.sh", "next", self.TTY)
        f.run("agent-jump.sh", "next", self.TTY)          # still on the landing: origin kept
        s = f.read()
        self.assertEqual(s["globals"]["@agent_jump__dev_ttys000"], "@10 @12")
        f.run("agent-jump.sh", "back", self.TTY)
        s = f.read()
        self.assertEqual(s["clients"][0]["window"], "@10")
        self.assertNotIn("@agent_jump__dev_ttys000", s["globals"])

    def test_back_after_moving_away_does_nothing(self):
        f = self.make()
        f.run("agent-jump.sh", "next", self.TTY)
        s = f.read(); s["clients"][0]["window"] = "@11"; s["calls"] = []
        f.state.write_text(json.dumps(s))
        f.run("agent-jump.sh", "back", self.TTY)
        s = f.read()
        self.assertFalse([c for c in s["calls"] if c[0] == "select-window"])
        self.assertEqual(s["messages"][-1][1], "nothing to go back to")

    def test_new_chain_after_moving_away(self):
        f = self.make()
        f.run("agent-jump.sh", "next", self.TTY)
        s = f.read(); s["clients"][0]["window"] = "@11"
        f.state.write_text(json.dumps(s))
        f.run("agent-jump.sh", "next", self.TTY)
        self.assertTrue(f.read()["globals"]["@agent_jump__dev_ttys000"].startswith("@11 "))

    def test_nothing_needs_you(self):
        f = FakeEnv({"@1": W("main", 1)}, clients=[{"tty": self.TTY, "session": "main", "window": "@1"}])
        self.f = f
        f.run("agent-jump.sh", "next", self.TTY)
        s = f.read()
        self.assertEqual(s["messages"][-1][1], "nothing needs you")
        self.assertFalse([c for c in s["calls"] if c[0] == "select-window"])

    def test_unknown_client_never_selects(self):
        f = self.make()
        f.run("agent-jump.sh", "next", "/dev/ttys999")
        self.assertFalse([c for c in f.read()["calls"] if c[0] == "select-window"])

    def test_back_refuses_an_origin_parked_in_stash(self):
        f = self.make()
        f.run("agent-jump.sh", "next", self.TTY)            # chain "@10 @13"
        s = f.read(); s["windows"]["@10"]["session"] = "stash"; s["calls"] = []   # origin parked
        f.state.write_text(json.dumps(s))
        f.run("agent-jump.sh", "back", self.TTY)
        s = f.read()
        self.assertFalse([c for c in s["calls"] if c[0] in ("select-window", "switch-client")], s["calls"])
        self.assertEqual(s["clients"][0]["session"], "work")
        self.assertNotIn("@agent_jump__dev_ttys000", s["globals"])
        msg = s["messages"][-1][1]
        self.assertIn("parked", msg)
        self.assertNotRegex(msg, r"^(no|not|can't|cannot|invalid|unknown|failed|error)\b")

    def test_goto_refuses_excluded_session(self):
        f = self.make()
        f.run("agent-jump.sh", "goto", self.TTY, "@16")      # lives in `tasks`
        s = f.read()
        self.assertFalse([c for c in s["calls"] if c[0] in ("select-window", "switch-client")], s["calls"])
        self.assertEqual(s["clients"][0]["session"], "main")

    def test_hash_in_label_is_escaped(self):
        f = self.make(**{"@18": W("zz#{host}", 1, **{"@agent_state": "failed", "@agent_since": "1 failed",
                                                   "@agent_summary": "fix #{pane_title} #[fg=red]"})})
        f.run("agent-jump.sh", "next", self.TTY)
        s = f.read()
        self.assertEqual(s["clients"][0]["window"], "@18")
        msg = s["messages"][-1][1]
        self.assertIn("zz##{host}:1 fix ##{pane_title} ##[fg=red]", msg)
        self.assertNotRegex(msg, r"(?<!#)#[{\[]")

    def test_linked_windows_land_in_a_normal_session(self):
        # The fake's display-message -t @id reports the LAST link (stash), as tmux's activity pick can.
        w19 = W("bai", 4, **{"@agent_state": "failed", "@agent_since": "1 failed"}); w19["links"] = ["stash"]
        w20 = W("bai", 5); w20["links"] = ["main"]
        f = self.make(**{"@19": w19, "@20": w20})
        s = f.read(); s["windows"]["@10"]["links"] = ["stash"]; f.state.write_text(json.dumps(s))

        f.run("agent-jump.sh", "next", self.TTY)
        s = f.read()
        self.assertEqual([c for c in s["calls"] if c[0] == "select-window"][-1], ["select-window", "-t", "=bai:@19"])
        self.assertEqual((s["clients"][0]["session"], s["clients"][0]["window"]), ("bai", "@19"))
        self.assertTrue(s["messages"][-1][1].startswith("→ bai:4 "), s["messages"][-1])

        f.run("agent-jump.sh", "back", self.TTY)            # origin @10 is main + stash
        s = f.read()
        self.assertEqual((s["clients"][0]["session"], s["clients"][0]["window"]), ("main", "@10"))
        self.assertEqual(s["messages"][-1][1], "← back")

        f.run("agent-jump.sh", "goto", self.TTY, "@19")     # bai + stash, client in main
        s = f.read()
        self.assertEqual((s["clients"][0]["session"], s["clients"][0]["window"]), ("bai", "@19"))

        f.run("agent-jump.sh", "goto", self.TTY, "@20")     # bai + main, client in bai: stay in bai
        s = f.read()
        self.assertEqual((s["clients"][0]["session"], s["clients"][0]["window"]), ("bai", "@20"))
        s["clients"][0].update(session="main", window="@10"); f.state.write_text(json.dumps(s))
        f.run("agent-jump.sh", "goto", self.TTY, "@20")     # from main: prefer the client's own session
        s = f.read()
        self.assertEqual((s["clients"][0]["session"], s["clients"][0]["window"]), ("main", "@20"))
        self.assertFalse([c for c in s["calls"] if c[0] == "switch-client" and "=stash" in c], s["calls"])

    def test_goto_named_session_wins_for_a_linked_window(self):
        # The strip's `bai` header (or a row under it) for a window linked into
        # main and bai: the client is in main, and must still land in bai.
        w20 = W("bai", 5); w20["links"] = ["main"]
        f = self.make(**{"@20": w20})
        f.run("agent-jump.sh", "goto", self.TTY, "@20", "bai")
        s = f.read()
        moves = [c for c in s["calls"] if c[0] in ("select-window", "switch-client")]
        self.assertEqual(moves[0], ["select-window", "-t", "=bai:@20"])            # select first
        self.assertEqual(moves[1][0], "switch-client")
        self.assertEqual((s["clients"][0]["session"], s["clients"][0]["window"]), ("bai", "@20"))
        s["clients"][0].update(session="bai", window="@12"); f.state.write_text(json.dumps(s))
        f.run("agent-jump.sh", "goto", self.TTY, "@20", "main")                    # and the other way
        s = f.read()
        self.assertEqual((s["clients"][0]["session"], s["clients"][0]["window"]), ("main", "@20"))

    def test_goto_named_session_is_checked(self):
        w20 = W("bai", 5); w20["links"] = ["stash"]
        f = self.make(**{"@20": w20})
        f.run("agent-jump.sh", "goto", self.TTY, "@20", "stash")    # holds it, but excluded: refused
        s = f.read()
        self.assertFalse([c for c in s["calls"] if c[0] in ("select-window", "switch-client")], s["calls"])
        self.assertIn("parked", s["messages"][-1][1])
        f.run("agent-jump.sh", "goto", self.TTY, "@20", "work")     # not linked there (any more)
        s = f.read()
        self.assertFalse([c for c in s["calls"] if c[0] in ("select-window", "switch-client")], s["calls"])
        self.assertEqual(s["clients"][0]["session"], "main")
        self.assertIn("has left work", s["messages"][-1][1])
        self.assertNotRegex(s["messages"][-1][1], r"^(no|not|can't|cannot|invalid|unknown|failed|error)\b")

    def test_linked_window_listed_and_counted_once(self):
        f = self.make()
        s = f.read(); s["windows"]["@12"]["links"] = ["main", "stash"]
        f.state.write_text(json.dumps(s))
        out = f.run("agent-jump.sh", "list").stdout.split("\n")
        rows = [l.split("\t") for l in out if l]
        self.assertEqual([r[0] for r in rows], ["@13", "@12", "@17", "@11"])
        self.assertTrue(all(len(r) == 6 for r in rows), rows)
        self.assertNotEqual(rows[1][1], "stash")
        f.run("agent-jump.sh", "next", self.TTY)
        self.assertIn("· failed · 3 more", f.read()["messages"][-1][1])


if __name__ == "__main__":
    unittest.main()
