"""Grok in resurrect and closed tabs.

* grok-session-track.sh keeps $STATE_DIR/grok-<pid>.json (the claude-<pid>.json
  shape, tool grok) for the grok TUI that runs it, follows /new, and removes it
  only for the session it records.
* assistant-save.sh adds a row per Grok TUI pane after upstream's save: fake
  `ps`, fake `tmux` and a stand-in plugin, so nothing live is read.
* assistant-restore.sh resumes a grok row as `<grok> <leading options> -r <sid>`
  (POSIX and nu forms), against a throwaway tmux server with a stub grok.
* closed-tabs.sh snapshots a grok tab, records its resume command, reopens it
  as a grok agent, and reads its transcript through `grok export`.

The "grok" process in these tests is a copy of bash under that name, so
`ps -o comm=` says grok; nothing here runs the real Grok.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_resurrect_fast_paths as fast  # noqa: E402  (ThrowawayServer)

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
TRACK = SCRIPTS / "executable_grok-session-track.sh"
SAVE = SCRIPTS / "executable_assistant-save.sh"
RESTORE = SCRIPTS / "executable_assistant-restore.sh"
CLOSED = SCRIPTS / "executable_closed-tabs.sh"
JQ = shutil.which("jq")
BASH = shutil.which("bash")
SID = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"


def fake_grok(bin_dir):
    """A bash copy named grok: ps reports its comm as .../grok."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    g = bin_dir / "grok"
    shutil.copy(BASH, g)
    g.chmod(0o755)
    return g


@unittest.skipUnless(JQ and BASH, "jq/bash not installed")
class SessionTrackHook(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="grok-track-")))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.state = self.root / "state"
        self.grok = fake_grok(self.root / "bin")
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("TMUX", "TMUX_PANE", "GROK_HOOK_EVENT")}
        self.env.update(TMUX_ASSISTANT_RESURRECT_DIR=str(self.state), TMUX_PANE="%7")

    def payload(self, name, **kw):
        p = self.root / name
        p.write_text(json.dumps(kw))
        return p

    def test_records_follow_the_session_and_end_only_their_own(self):
        p1 = self.payload("p1", sessionId="s-1", session_id="s-1", cwd="/proj/a",
                          hookEventName="session_start", source="new")
        p2 = self.payload("p2", hook_event_name="SessionStart", sessionId="s-2", cwd="/proj/a", source="new")
        old_end = self.payload("p3", sessionId="s-1", reason="other")
        child_end = self.payload("p4", sessionId="s-2", subagentType="explore")
        end = self.payload("p5", session_id="s-2", reason="exit")
        snap1, snap2, snap3 = (self.root / f"snap{i}.json" for i in (1, 2, 3))
        pidf = self.root / "pid"
        h = f"bash '{TRACK}'"
        rec = f"'{self.state}'/grok-$$.json"
        # Each hook is a direct child of the "grok" (none is the last command,
        # so bash forks rather than execs).
        script = "; ".join([
            f"echo $$ > '{pidf}'",
            f"{h} start < '{p1}'", f"cp {rec} '{snap1}'",
            f"{h} < '{p2}'", f"cp {rec} '{snap2}'",
            f"{h} end < '{old_end}'", f"GROK_HOOK_EVENT=SessionEnd {h} < '{child_end}'",
            f"cp {rec} '{snap3}'",
            f"GROK_HOOK_EVENT=session_end {h} < '{end}'", "true"])
        r = subprocess.run([str(self.grok), "-c", script], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        pid = int(pidf.read_text())
        first = json.loads(snap1.read_text())
        self.assertEqual((first["tool"], first["ppid"], first["session_id"], first["cwd"]),
                         ("grok", pid, "s-1", "/proj/a"))
        self.assertEqual(first["env"]["tmux_pane"], "%7")
        self.assertEqual(first["source"], "new")          # the payload is kept, as for claude
        self.assertEqual(json.loads(snap2.read_text())["session_id"], "s-2")   # /new rewrote it
        self.assertEqual(json.loads(snap3.read_text())["session_id"], "s-2")   # old/child ends kept it
        self.assertEqual(list(self.state.glob("grok-*.json")), [])            # its own end removed it

    def test_nothing_is_written_outside_grok(self):
        p = self.payload("p", session_id="s-1", hook_event_name="SessionStart")
        with p.open() as f:
            subprocess.run(["bash", str(TRACK)], stdin=f, env=self.env, check=True, timeout=30)
        # A lookalike name is not grok either (grok-<os>-<arch> only).
        other = self.root / "bin2/grok-mcp-server"
        other.parent.mkdir()
        shutil.copy(BASH, other)
        subprocess.run([str(other), "-c", f"bash '{TRACK}' start < '{p}'; true"], env=self.env,
                       check=True, timeout=30)
        self.assertFalse(self.state.exists() and any(self.state.iterdir()))


# assistant-save.sh sources this instead of the plugin: main() copies a canned
# upstream sidecar, and strip_assistant_pane_contents() only notes the call.
FAKE_PLUGIN = """set -euo pipefail
STATE_DIR="$FAKE_STATE"
OUTPUT_FILE="$FAKE_OUTPUT"
log() { printf '%s\\n' "$*" >>"$FAKE_STATE/log"; }
main() { cp "$FAKE_INPUT" "$OUTPUT_FILE"; }
strip_assistant_pane_contents() { echo strip >>"$FAKE_STATE/log"; }
"""
PANES = [("main:0.0", 100, "/pane/a"), ("work:1.0", 200, "/pane/b"), ("work:2.0", 300, "/pane/c"),
         ("work:3.0", 400, "/pane/d"), ("work:4.0", 500, "/pane/e"), ("work:5.0", 600, "/pane/f"),
         ("main:6.0", 700, "/pane/g"), ("agents:0.0", 800, "/pane/h"), ("work:7.0", 1000, "/pane/i"),
         ("work:8.0", 1100, "/pane/j"), ("work:9.0", 1200, "/pane/k"), ("work:10.0", 1300, "/pane/l"),
         ("work:11.0", 1400, "/pane/m"), ("work:12.0", 1500, "/pane/n"), ("work:13.0", 1600, "/pane/o"),
         ("work:14.0", 1700, "/pane/p"), ("work:15.0", 1800, "/pane/q")]
# {REC} and {HELP} are live pids (records whose pid is gone are pruned).
PS = f"""\
  100     1 -zsh
  101   100 /Users/x/.grok/bin/grok --model grok-4.7 -r {SID} fix the bug
  102   101 node /opt/codegraph/server.js
  200     1 -zsh
  201   200 grok -p hello
  300     1 -zsh
  301   300 /Users/x/.grok/downloads/grok-macos-aarch64 sessions list
  400     1 zsh
  {{REC}}   400 /Users/x/.grok/downloads/grok-macos-aarch64 -c
  500     1 -zsh
  501   500 grok --effort high
  502   501 claude -p summarize this
  600     1 -zsh
  601   600 /Users/x/.grok/bin/agent
  700     1 -zsh
  701   700 claude --resume c-7
  702   700 grok
  800     1 -zsh
  801   800 grok
 1000     1 -zsh
 {{HELP}}  1000 grok help me fix the tests
 1100     1 -zsh
 1101  1100 /opt/grok-mcp-server
 1200     1 -zsh
 1201  1200 grok --help=all
 1300     1 -zsh
 1301  1300 grok --resume g-x --add-dir /Users/x/code/claude
 1400     1 -zsh
 1401  1400 grok -phi
 1500     1 -zsh
 1501  1500 grok -p=hi
 1600     1 -zsh
 1601  1600 grok -- sessions
 1700     1 -zsh
 1701  1700 grok -- fix it
 1800     1 -zsh
 1801  1800 grok -hv
"""


def live_pid(test):
    p = subprocess.Popen(["sleep", "300"])
    test.addCleanup(lambda: (p.kill(), p.wait()))
    return p.pid


def dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


@unittest.skipUnless(JQ, "jq not installed")
class SaveGrokRows(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="grok-save-")))
        self.addCleanup(shutil.rmtree, self.root, True)
        plugin = self.root / "plugin/scripts"
        plugin.mkdir(parents=True)
        (plugin / "save-assistant-sessions.sh").write_text(FAKE_PLUGIN)
        self.state = self.root / "state"
        self.state.mkdir()
        bin_ = self.root / "bin"
        bin_.mkdir()
        (bin_ / "jq").symlink_to(JQ)
        self.ps_log = self.root / "ps.log"
        self.rec, self.help = live_pid(self), live_pid(self)
        (self.root / "ps.out").write_text(PS.replace("{REC}", str(self.rec)).replace("{HELP}", str(self.help)))
        (bin_ / "ps").write_text(f"#!/bin/sh\necho \"$*\" >>'{self.ps_log}'\ncat '{self.root}/ps.out'\n")
        us = "\x1f"
        (self.root / "panes.out").write_text("".join(f"{t}{us}{p}{us}{c}\n" for t, p, c in PANES))
        (bin_ / "tmux").write_text(
            f"#!/bin/sh\ncase \"$1\" in list-panes) cat '{self.root}/panes.out' ;; esac\nexit 0\n")
        # No copilot / agy here (assistant-save.sh's ca_rows asks pgrep first).
        (bin_ / "pgrep").write_text("#!/bin/sh\nexit 1\n")
        for f in ("ps", "tmux", "pgrep"):
            (bin_ / f).chmod(0o755)
        (self.root / "home/.grok").mkdir(parents=True)
        self.upstream = [dict(pane="main:9.0", tool="claude", session_id="c-9", pid="900"),
                         dict(pane="work:4.0", tool="claude", session_id="c-4", pid="502"),
                         dict(pane="main:6.0", tool="claude", session_id="c-7", pid="701"),
                         # upstream's awk reading grok's own argv as claude
                         dict(pane="work:10.0", tool="claude", session_id="c-x", pid="1301")]
        (self.root / "in.json").write_text(json.dumps(dict(timestamp="t", sessions=self.upstream)))
        self.out = self.root / "assistant-sessions.json"
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE", "GROK_HOME")}
        self.env.update(HOME=str(self.root / "home"), TMUX_ASSISTANT_RESURRECT_PLUGIN=str(self.root / "plugin"),
                        FAKE_STATE=str(self.state), FAKE_OUTPUT=str(self.out),
                        FAKE_INPUT=str(self.root / "in.json"), PATH=f"{bin_}:/usr/bin:/bin")

    def save(self):
        r = subprocess.run(["bash", str(SAVE)], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return {row["pane"]: row for row in json.loads(self.out.read_text())["sessions"]}

    def record(self, pid, sid, **kw):
        (self.state / f"grok-{pid}.json").write_text(json.dumps(dict(session_id=sid, tool="grok", ppid=pid, **kw)))

    def test_grok_tui_panes_get_rows(self):
        (self.root / "home/.grok/active_sessions.json").write_text(json.dumps([
            dict(session_id="g-new", pid=101, pid_start=1, cwd="/proj/a", opened_at="x"),
            dict(session_id="g-501", pid=501, cwd="/proj/e"),
            dict(session_id="g-mcp", pid=1101), dict(session_id="g-help", pid=1201),
            dict(session_id="g-1301", pid=1301), dict(session_id="g-1401", pid=1401),
            dict(session_id="g-1501", pid=1501), dict(session_id="g-1601", pid=1601),
            dict(session_id="g-1701", pid=1701),
            dict(session_id="g-1801", pid=1801)]))
        self.record(self.rec, "g-401", cwd="/proj/d", env=dict(tmux_pane="%4", FOO="bar"))
        self.record(self.help, "g-help-me", cwd="/proj/i")
        (self.state / f"grok-{os.getpid()}.json").write_text("{not json")   # a torn record elsewhere
        rows = self.save()
        a = rows["main:0.0"]
        # active_sessions.json beats the -r in argv (a /new since launch).
        self.assertEqual((a["tool"], a["session_id"], a["pid"], a["cwd"]), ("grok", "g-new", "101", "/proj/a"))
        self.assertEqual(a["cli_args"], f"--model grok-4.7 -r {SID} fix the bug")
        self.assertEqual(a["model"], "")
        self.assertNotIn("work:1.0", rows)       # grok -p: headless
        self.assertNotIn("work:2.0", rows)       # grok sessions list: a utility
        self.assertNotIn("work:5.0", rows)       # the `agent` symlink is never matched
        # No active_sessions.json entry for this one: its record gives the
        # session, cwd and env (the torn record costs only itself).
        d = rows["work:3.0"]
        self.assertEqual((d["session_id"], d["cwd"], d["env"]["FOO"]), ("g-401", "/proj/d", "bar"))
        # `grok "help me fix the tests"` reads as the help subcommand; its
        # session record makes it the TUI it is.
        h = rows["work:7.0"]
        self.assertEqual((h["session_id"], h["cli_args"]), ("g-help-me", "help me fix the tests"))
        self.assertNotIn("work:8.0", rows)       # grok-mcp-server is not grok, session or not
        self.assertNotIn("work:9.0", rows)       # --help=all: headless is final, session or not
        # Glued short flags (-phi, -p=hi, -hv) are headless, session or not;
        # words after `--` are prompt text, so `-- sessions` is a TUI too.
        for pane in ("work:11.0", "work:12.0", "work:15.0"):
            self.assertNotIn(pane, rows)
        self.assertEqual(rows["work:13.0"]["session_id"], "g-1601")
        self.assertEqual(rows["work:14.0"]["session_id"], "g-1701")
        # Upstream mistook the grok itself for a claude: the grok row wins.
        self.assertEqual((rows["work:10.0"]["tool"], rows["work:10.0"]["session_id"]), ("grok", "g-1301"))
        # A claude its grok ran is replaced; an unrelated one in a pane is kept.
        self.assertEqual((rows["work:4.0"]["tool"], rows["work:4.0"]["session_id"]), ("grok", "g-501"))
        self.assertEqual(rows["main:6.0"]["tool"], "claude")
        self.assertEqual(rows["main:9.0"]["session_id"], "c-9")
        self.assertNotIn("agents:0.0", rows)     # excluded session, no session id either
        self.assertIn("strip", (self.state / "log").read_text())

    def test_record_and_argv_fallbacks(self):
        self.record(self.rec, "g-401", cwd="/proj/d", env=dict(tmux_pane="%4", FOO="bar"))
        rows = self.save()
        d = rows["work:3.0"]
        self.assertEqual((d["session_id"], d["cwd"], d["cli_args"], d["env"]),
                         ("g-401", "/proj/d", "-c", dict(tmux_pane="%4", FOO="bar")))
        self.assertEqual((rows["main:0.0"]["session_id"], rows["main:0.0"]["cwd"]), (SID, "/pane/a"))
        self.assertNotIn("work:4.0", [p for p, r in rows.items() if r["tool"] == "grok"])  # no id: claude kept
        self.assertNotIn("work:7.0", rows)       # `grok help …` with no session: the help subcommand

    def test_no_grok_in_use_costs_no_ps_and_stale_records_go(self):
        (self.root / "home/.grok/active_sessions.json").write_text("[\n]\n")
        stale = self.state / f"grok-{dead_pid()}.json"
        stale.write_text(json.dumps(dict(session_id="gone", tool="grok")))
        rows = self.save()
        self.assertEqual(sorted(rows), ["main:6.0", "main:9.0", "work:10.0", "work:4.0"])
        self.assertFalse(self.ps_log.exists())
        self.assertFalse(stale.exists())


# The TUI filter's bash/awk ports must give the watcher's verdicts: T = TUI,
# N = headless (final), S = subcommand word (a TUI only with a session).
PORTS_HARNESS = r'''
S=$1
eval "$(awk '/^grok_subcommand\(\) \{/{f=1} f{print} f&&/^\}/{exit}' "$S/executable_agent-tab-watcher.sh")"
eval "$(awk '/^grok_tui_args\(\) \{/{f=1} f{print} f&&/^\}/{exit}' "$S/executable_agent-tab-watcher.sh")"
eval "$(grep '^GROK_SUBCMDS=' "$S/executable_closed-tabs.sh")"
ct=$(awk '/^grok_tui\(\) \{/{f=1} f{print} f&&/^\}/{exit}' "$S/executable_closed-tabs.sh")
eval "${ct/grok_tui()/ct_tui()}"
fn=$(awk '/function tui\(line/{f=1} f{print} f&&/^\t\t\}$/{exit}' "$S/executable_assistant-save.sh")
begin=$(awk '/^\t\tBEGIN \{/{f=1} f{print} f&&/^\t\t\}$/{exit}' "$S/executable_assistant-save.sh")
v() { case $1 in 0) echo T ;; 1) echo N ;; 2) echo S ;; esac; }
while IFS= read -r line; do
    grok_tui_args "$line"; w=$?
    read -r -a arr <<<"$line"; ct_tui "${arr[@]}"; c=$?
    a=$(printf '%s\n' "$line" | awk "$fn $begin { print tui(\$0) }")
    case $a in 1) a=0 ;; 0) a=1 ;; esac
    echo "$line|$(v $w)|$(v $c)|$(v $a)"
done
'''
PORT_CASES = {
    "grok": "T", "grok fix it": "T", "grok -phi": "N", "grok -p=hi": "N", "grok --single=x": "N",
    "grok --single x": "N", "grok --prompt-json=x": "N", "grok fix the -v flag": "N", "grok fix -p thing": "N",
    "grok -- help": "T", "grok mcp add x -- -y srv": "S", "grok -r sessions": "S", "grok -r abc help": "S",
    "grok --model=x sessions": "S", "grok -m x": "T", "grok --help=x": "N", "grok -": "T", "grok -cp x": "T",
    "grok -c": "T", "grok -r": "T", "grok -w feat dashboard": "T", "grok --resume=abc help": "S",
    "grok -s ID sessions": "S", "grok help me fix the tests": "S"}


class TuiFilterPortsAgree(unittest.TestCase):
    def test_watcher_closed_tabs_and_save_agree(self):
        r = subprocess.run(["bash", "-c", PORTS_HARNESS, "harness", str(SCRIPTS)], input="\n".join(PORT_CASES) + "\n",
                           capture_output=True, text=True, timeout=60)
        got = {}
        for row in r.stdout.splitlines():
            line, *verdicts = row.split("|")
            got[line] = verdicts
        self.assertEqual(got, {k: [v, v, v] for k, v in PORT_CASES.items()}, r.stderr)


def jq_prog():
    text = RESTORE.read_text()
    return re.search(r"read -r -d '' JQ_PROG <<'JQ'\n(.*?)\nJQ\n", text, re.S).group(1)


@unittest.skipUnless(JQ, "jq not installed")
class RestoreCommand(unittest.TestCase):
    def rows(self, *sessions, grok="command grok", groknu="^grok"):
        doc = json.dumps(dict(timestamp="t", sessions=list(sessions)))
        r = subprocess.run([JQ, "-r", "--argjson", "cap", '["FOO"]', "--arg", "codex", "command codex",
                            "--arg", "codexnu", "^codex", "--arg", "grok", grok, "--arg", "groknu", groknu,
                            jq_prog()], input=doc, capture_output=True, text=True, check=True)
        return [line.split("\x1f") for line in r.stdout.splitlines()]

    def test_grok_rows_resume_with_their_leading_options(self):
        [row] = self.rows(dict(pane="w:1.0", tool="grok", session_id="g-1", cwd="/p q",
                               cli_args="--model grok-4.7 -c -r old --fork-session --restore-code -s 0000"
                                        " --effort high -w wt --ref main --cwd /x --add-dir ../lib"
                                        " --add-dir /abs/lib --add-dir=rel --add-dir=/abs2 --cwd=sub"
                                        " fix the bug -m y",
                               env=dict(FOO="b'r", tmux_pane="%1")))
        self.assertEqual(row[1:4], ["grok", "g-1", "/p q"])
        # One-shot flags and anything relative to the launch dir are gone.
        self.assertEqual(row[4], "FOO='b'\"'\"'r' command grok '--model' 'grok-4.7' '--effort' 'high'"
                                 " '--add-dir' '/abs/lib' '--add-dir=/abs2' -r 'g-1'")
        self.assertEqual(row[5], "if (r###'/p q'### | path exists) { cd r###'/p q'### }; "
                                 "with-env {FOO: r###'b'r'###} { ^grok r###'--model'### r###'grok-4.7'###"
                                 " r###'--effort'### r###'high'### r###'--add-dir'### r###'/abs/lib'###"
                                 " r###'--add-dir=/abs2'### -r r###'g-1'### }")

    def test_optional_values_and_equals_forms(self):
        rows = self.rows(dict(pane="a:0.0", tool="grok", session_id="s", cli_args="-r -m x"),
                         dict(pane="b:0.0", tool="grok", session_id="s", cli_args="--resume=t --worktree"),
                         dict(pane="c:0.0", tool="grok", session_id="s", cli_args=""),
                         grok="'/h/.grok/bin/grok'", groknu="^r###'/h/.grok/bin/grok'###")
        self.assertEqual([r[4] for r in rows], ["'/h/.grok/bin/grok' '-m' 'x' -r 's'",
                                                "'/h/.grok/bin/grok' -r 's'", "'/h/.grok/bin/grok' -r 's'"])
        self.assertEqual(rows[2][5], "^r###'/h/.grok/bin/grok'### -r r###'s'###")


@unittest.skipUnless((fast.PLUGIN / "scripts/lib-detect.sh").exists(), "tmux-assistant-resurrect not installed")
class RestoreHook(fast.ThrowawayServer):
    def test_grok_row_resumes_through_the_grok_link(self):
        log = self.root / "stub.log"
        stub_dir = self.root / ".grok/bin"
        stub_dir.mkdir(parents=True)
        stub = stub_dir / "grok"
        stub.write_text(f'#!/bin/bash\necho "grok $* @$PWD" >> "{log}"\n')
        stub.chmod(0o755)
        work = self.root / "proj"
        work.mkdir()
        with (self.root / "zdot/.zshrc").open("a") as rc:
            rc.write("export PATH='/usr/bin:/bin'\n")
        self.tmux("set", "-g", "@assistant-resurrect-client-wait", "0", ";", "new-session", "-d", "-s", "work")
        (self.dir / "assistant-sessions.json").write_text(json.dumps(dict(timestamp="t", sessions=[
            dict(pane="work:0.0", tool="grok", session_id="g-1", cwd=str(work),
                 cli_args="--model grok-4.7 -c fix it")])))
        subprocess.run(["bash", str(RESTORE)], env=self.env, check=True, capture_output=True, timeout=60)
        for _ in range(100):
            if log.exists():
                break
            time.sleep(0.05)
        rlog = (self.dir / "assistant-restore.log").read_text()
        self.assertTrue(log.exists(), rlog + self.tmux("capture-pane", "-p", "-t", "work:0"))
        self.assertEqual(log.read_text().splitlines(), [f"grok --model grok-4.7 -r g-1 @{work}"])
        self.assertIn("restored 1 of 1", rlog)

    def test_without_the_opt_in_a_non_default_server_launches_nothing(self):
        log = self.root / "stub.log"
        stubs = self.root / "stub"
        stubs.mkdir()
        (stubs / "claude").write_text(f'#!/bin/bash\necho "claude $*" >> "{log}"\n')
        (stubs / "claude").chmod(0o755)
        with (self.root / "zdot/.zshrc").open("a") as rc:
            rc.write(f"export PATH='{stubs}:/usr/bin:/bin'\n")
        self.tmux("set", "-g", "@assistant-resurrect-client-wait", "0", ";", "new-session", "-d", "-s", "work")
        self.wait_for_prompt("work:")
        (self.dir / "assistant-sessions.json").write_text(json.dumps(dict(timestamp="t", sessions=[
            dict(pane="work:0.0", tool="claude", session_id="c-1", cwd="")])))
        env = {k: v for k, v in self.env.items() if k != "RESURRECT_SAVE_ALLOW_SOCKET"}
        subprocess.run(["bash", str(RESTORE)], env=env, check=True, capture_output=True, timeout=60)
        time.sleep(0.5)
        self.assertFalse(log.exists())
        self.assertIn("not the default server: ", (self.dir / "assistant-restore.log").read_text())


@unittest.skipUnless(shutil.which("tmux") and (fast.PLUGIN / "scripts/lib-detect.sh").exists(),
                     "tmux / tmux-assistant-resurrect not installed")
class RestoreRefusesPrivateDefault(unittest.TestCase):
    """The 2026-11-02 shape: an agent's private server, its socket literally
    named `default` under its own TMUX_TMPDIR, that sourced tmux.conf and so
    runs the post-restore hook (with that TMUX_TMPDIR in its environment)."""

    def setUp(self):
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="grk-")))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "zdot").mkdir()
        self.log = self.root / "stub.log"
        stubs = self.root / "stub"
        stubs.mkdir()
        (stubs / "claude").write_text(f'#!/bin/bash\necho "claude $*" >> "{self.log}"\n')
        (stubs / "claude").chmod(0o755)
        (self.root / "zdot/.zshrc").write_text(f"PS1='$ '\nexport PATH='{stubs}:/usr/bin:/bin'\n")
        self.dir = self.root / "resurrect"
        self.dir.mkdir()
        self.tmpdir = self.root / "t"
        self.tmpdir.mkdir()
        env = {k: v for k, v in os.environ.items()
               if k not in ("TMUX", "TMUX_PANE", "RESURRECT_SAVE_ALLOW_SOCKET")}
        env.update(HOME=str(self.root), ZDOTDIR=str(self.root / "zdot"), TMUX_TMPDIR=str(self.tmpdir),
                   TMUX_ASSISTANT_RESURRECT_PLUGIN=str(fast.PLUGIN))
        self.env = env
        # No -L/-S: with TMUX unset and TMUX_TMPDIR private, this is
        # <private>/tmux-$UID/default, never the real default socket.
        self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "work", "-x", "120", "-y", "30")
        self.addCleanup(lambda: subprocess.run(["tmux", "kill-server"], env=self.env, capture_output=True))
        self.tmux("set", "-g", "exit-empty", "off", ";", "set", "-g", "default-shell", "/bin/zsh", ";",
                  "set", "-g", "@resurrect-dir", str(self.dir), ";",
                  "set", "-g", "@assistant-resurrect-client-wait", "0")
        self.socket = self.tmux("display", "-p", "#{socket_path}").strip()
        self.assertTrue(self.socket.startswith(str(self.tmpdir)), self.socket)
        self.assertTrue(self.socket.endswith("/default"), self.socket)

    def tmux(self, *args):
        return subprocess.run(["tmux", *args], env=self.env, capture_output=True, text=True, check=True).stdout

    def test_restore_into_a_private_default_launches_nothing(self):
        for _ in range(100):
            if self.tmux("display", "-p", "-t", "work:", "#{cursor_x}") != "0\n":
                break
            time.sleep(0.05)
        (self.dir / "assistant-sessions.json").write_text(json.dumps(dict(timestamp="t", sessions=[
            dict(pane="work:0.0", tool="claude", session_id="c-1", cwd="")])))
        # As the hook runs: $TMUX names the private server.
        env = dict(self.env, TMUX=f"{self.socket},0,0")
        r = subprocess.run(["bash", str(RESTORE)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        time.sleep(0.5)
        self.assertFalse(self.log.exists())
        self.assertEqual((self.dir / "assistant-restore.log").read_text().count("\n"), 1)
        self.assertIn(f"not the default server: {self.socket}, restore skipped",
                      (self.dir / "assistant-restore.log").read_text())
        self.assertNotIn("claude", self.tmux("capture-pane", "-p", "-t", "work:0"))


@unittest.skipUnless((fast.PLUGIN / "scripts/lib-detect.sh").exists(), "tmux-assistant-resurrect not installed")
class ClosedTabs(fast.ThrowawayServer):
    def setUp(self):
        super().setUp()
        root = self.root
        self.state = root / "xdg"
        self.track = root / "track"
        self.track.mkdir()
        # Only what closed-tabs needs; no ai-exec, so no describe call ever leaves.
        bin_ = root / "bin"
        bin_.mkdir()
        for tool in ("tmux", "jq"):
            (bin_ / tool).symlink_to(shutil.which(tool))
        self.grok = fake_grok(root / "fakebin")
        plugins = root / ".config/tmux/plugins"
        plugins.mkdir(parents=True)
        (plugins / "tmux-assistant-resurrect").symlink_to(fast.PLUGIN)
        (root / ".grok/bin").mkdir(parents=True)
        self.export_log = root / "export.log"
        stub = root / ".grok/bin/grok"
        stub.write_text(f"""#!/bin/bash
echo "$*" >> '{self.export_log}'
[ "$1" = export ] && [ "$2" = '{SID}' ] || exit 1
printf '## User\\n\\nFix the roast bot\\n\\n## Assistant\\n\\nLooking at it.\\n\\n## Tools\\n\\n- Read: bot.py\\n'
""")
        stub.chmod(0o755)
        self.proj = root / "proj"
        self.proj.mkdir()
        self.cenv = dict(self.env, XDG_STATE_HOME=str(self.state), NOTIFY_SH=str(root / "no-notify"),
                         NOTIFY_TMUX="true", TMUX_ASSISTANT_RESURRECT_DIR=str(self.track),
                         PATH=f"{bin_}:/usr/bin:/bin")

    def run_ct(self, *args):
        r = subprocess.run(["bash", str(CLOSED), *args], env=self.cenv, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def history(self):
        f = self.state / "tmux-closed-tabs/history.jsonl"
        return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []

    def test_close_records_the_resume_and_reopen_is_a_grok_agent(self):
        self.tmux("new-window", "-t", "main:", "-n", "roast", f"'{self.grok}' -c 'sleep 600; true' --model x")
        pane, pid = self.tmux("display", "-p", "-t", "main:1", "#{pane_id} #{pane_pid}").split()
        (self.root / ".grok/active_sessions.json").write_text(json.dumps([
            dict(session_id=SID, pid=int(pid), cwd=str(self.proj))]))
        self.tmux("set", "-g", "@agent_birth", "set -w @born_kind")
        self.run_ct("close", pane)
        [rec] = self.history()
        self.assertEqual((rec["tool"], rec["session_id"], rec["cwd"]), ("grok", SID, str(self.proj)))
        self.assertEqual(rec["cmd"], f"'{self.root}/.grok/bin/grok' -r '{SID}'")
        self.assertEqual(self.tmux("list-windows", "-t", "main:", "-F", "#{window_name}").split(), ["zsh"])

        self.run_ct("reopen", "main", "")
        self.assertEqual(self.history(), [])
        win = self.tmux("list-windows", "-t", "main:", "-F", "#{window_index} #{@born_kind} #{@agent_summary}")
        self.assertIn("1 grok roast", win.splitlines())

    def test_a_headless_grok_is_not_an_agent(self):
        self.tmux("new-window", "-t", "main:", f"'{self.grok}' -p -c 'sleep 600; true'")  # bash's own -p
        pane = self.tmux("display", "-p", "-t", "main:1", "#{pane_id}").strip()
        self.run_ct("close", pane)
        [rec] = self.history()
        self.assertEqual((rec["tool"], rec["cmd"]), ("", ""))

    def help_tab(self):
        """A grok whose ps argv reads `grok help me`, as `grok "help me"` does."""
        d = self.root / "helpdir"
        d.mkdir(exist_ok=True)
        (d / "help").write_text("sleep 600; true\n")   # what the bash-as-grok runs
        self.tmux("new-window", "-t", "main:", "-c", str(d), f"'{self.grok}' help me")
        time.sleep(0.3)
        return self.tmux("display", "-p", "-t", "main:1", "#{pane_id} #{pane_pid}").split()

    def test_a_prompt_that_reads_as_a_subcommand_is_a_tui_with_a_session(self):
        pane, pid = self.help_tab()
        (self.track / f"grok-{pid}.json").write_text(json.dumps(dict(session_id=SID, tool="grok", ppid=int(pid))))
        self.run_ct("close", pane)
        [rec] = self.history()
        self.assertEqual((rec["tool"], rec["session_id"]), ("grok", SID))

    def test_a_subcommand_without_a_session_is_not_an_agent(self):
        pane, _ = self.help_tab()
        self.run_ct("close", pane)
        [rec] = self.history()
        self.assertEqual((rec["tool"], rec["cmd"]), ("", ""))

    def test_tui_rule_glued_short_flags_and_double_dash(self):
        # pid 1 has no grok session, so a subcommand verdict stays "not a TUI".
        cases = {"grok -phi": 1, "grok -p=hi": 1, "grok -hv": 1, "grok -vx": 1, "grok -- sessions": 0,
                 "grok -- help": 0, "grok mcp add x -- -y srv": 1, "grok -- fix it": 0, "grok -m x -c": 0,
                 "grok-macos-aarch64 -r": 0, "grok-mcp-server": 1, "grok --help=all": 1}
        script = f"set -- list; source '{CLOSED}' >/dev/null; " + "; ".join(
            f"is_grok '{argv}' 1; echo $?" for argv in cases)
        r = subprocess.run(["bash", "-c", script], env=self.cenv, capture_output=True, text=True, timeout=30)
        self.assertEqual(dict(zip(cases, map(int, r.stdout.split()))), cases, r.stderr)

    def test_resume_command_drops_one_shot_and_relative_flags(self):
        script = (f"set -- list; source '{CLOSED}' >/dev/null; build_cmd grok g-1 "
                  "'--model m -c --restore-code --fork-session --cwd sub --add-dir ../x --add-dir /abs "
                  "--add-dir=rel --add-dir=/abs2 --cwd=y -r old -s 1 fix it' '' '{}'")
        r = subprocess.run(["bash", "-c", script], env=self.cenv, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), f"'{self.root}/.grok/bin/grok' '--model' 'm' '--add-dir' '/abs'"
                                           " '--add-dir=/abs2' -r 'g-1'", r.stderr)

    def test_transcript_comes_from_grok_export_else_the_title(self):
        bin_ = self.root / "bin"
        prompt = self.root / "prompt.txt"
        (bin_ / "ai-exec").write_text(f"""#!/bin/bash
out=""
while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done
cat > '{prompt}'
echo "Roast bot limits" > "$out"
""")
        (bin_ / "ai-exec").chmod(0o755)
        self.assertEqual(self.run_ct("summarize", "grok", SID, str(self.proj), "roast", "/nonexistent"),
                         "Roast bot limits")
        text = prompt.read_text()
        self.assertIn("USER: Fix the roast bot\nASSISTANT: Looking at it.\n  [Read: bot.py]", text)
        self.assertIn(f"export {SID}", self.export_log.read_text())

        other = "0199a1b2-0000-7e5f-8a9b-0c1d2e3f4a5b"
        sdir = self.root / ".grok/sessions/%2Fproj" / other
        sdir.mkdir(parents=True)
        (sdir / "summary.json").write_text(json.dumps(dict(generated_title="Deploy refusal")))
        self.run_ct("summarize", "grok", other, str(self.proj), "roast", "/nonexistent")
        self.assertIn("TITLE: Deploy refusal", prompt.read_text())


if __name__ == "__main__":
    unittest.main()
