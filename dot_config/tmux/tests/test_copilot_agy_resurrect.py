"""GitHub Copilot CLI and Antigravity CLI (agy) in resurrect and closed tabs.

* copilot-agy-session-track.sh keeps $STATE_DIR/<kind>-<pid>.json for the
  CLI's TUI above the hook (a tty, no -p), follows the session, and removes it
  only for the session that ended.
* assistant-save.sh adds a row per copilot / agy TUI pane after upstream's
  save, its id from the record, else the CLI's own lock (copilot's
  inuse.<pid>.lock, agy's open presence lock), else the argv: fake `ps`,
  `tmux`, `pgrep`, `lsof` and a stand-in plugin, so nothing live is read.
* assistant-restore.sh resumes them as `command copilot … --resume <sid>` and
  `command agy … --conversation <sid>` (POSIX and nu forms).
* closed-tabs.sh records a closed copilot / agy tab's resume command (on a
  throwaway server), reopens it born with its kind, and reads its transcript.
* Every port of the TUI rule (watcher, hook, save, closed-tabs, agent-pause)
  gives the same verdicts.

The "copilot" / "agy" processes here are copies of bash under those names, so
`ps -o comm=` says copilot / agy; nothing here runs either real CLI.
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
TRACK = SCRIPTS / "executable_copilot-agy-session-track.sh"
SAVE = SCRIPTS / "executable_assistant-save.sh"
RESTORE = SCRIPTS / "executable_assistant-restore.sh"
CLOSED = SCRIPTS / "executable_closed-tabs.sh"
WATCHER = SCRIPTS / "executable_agent-tab-watcher.sh"
JQ = shutil.which("jq")
BASH = shutil.which("bash")
SCRIPT = shutil.which("script")
SID = "a92f5efc-59bd-4461-944b-c77538b9bc7d"
SID2 = "315c538f-b928-4dd9-ba11-4bf8058e0718"


def fake_cli(bin_dir, name):
    """A bash copy named copilot / agy: ps reports its comm as .../<name>."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    g = bin_dir / name
    # Once per name: copying over a binary that has run rewrites it in place,
    # and macOS then SIGKILLs the next exec of it (its code-signature cache:
    # `script` exits 9, no stderr), intermittently.
    if not g.exists():
        shutil.copy(BASH, g)
        g.chmod(0o755)
    return g


@unittest.skipUnless(JQ and BASH and SCRIPT, "jq/bash/script not installed")
class SessionTrackHook(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="ca-track-")))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.state = self.root / "state"
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE", "CUA_NOTCH_IGNORE")}
        self.env.update(TMUX_ASSISTANT_RESURRECT_DIR=str(self.state), TMUX_PANE="%7", PATH="/usr/bin:/bin")

    def payload(self, name, **kw):
        p = self.root / name
        p.write_text(json.dumps(kw))
        return p

    def run_in(self, cli, steps, flags=(), tty=True, env=None):
        """Run `steps` (shell lines) inside a fake CLI process, on a pty
        (script(1)) unless tty is False; -> that process's pid."""
        pidf = self.root / "pid"
        prog = fake_cli(self.root / "bin", cli)
        body = "; ".join([f"echo $$ > '{pidf}'", *steps, "true"])
        argv = [str(prog), *flags, "-c", body]
        if tty:
            argv = [SCRIPT, "-q", "/dev/null", *argv]
        # Its own session either way: without script(1) it has no controlling
        # terminal at all (ps says ??), whatever terminal runs the tests.
        r = subprocess.run(argv, env=env or self.env, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=30, start_new_session=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return int(pidf.read_text())

    def test_copilot_records_follow_the_session_and_end_only_their_own(self):
        start = self.payload("start", source="startup", sessionId=SID, timestamp=1, cwd="/proj/a")
        new = self.payload("new", source="new", sessionId=SID2, timestamp=2, cwd="/proj/a")
        old_end = self.payload("old_end", reason="user_exit", sessionId=SID, cwd="/proj/a")
        sub = self.payload("sub", source="startup", sessionId="sub-1", agentId="a-1", cwd="/x")
        end = self.payload("end", reason="user_exit", sessionId=SID2, cwd="/proj/a")
        snaps = [self.root / f"snap{i}.json" for i in range(3)]
        h = f"bash '{TRACK}' copilot"
        rec = f"'{self.state}'/copilot-$$.json"
        pid = self.run_in("copilot", [
            f"{h} start < '{start}'", f"cp {rec} '{snaps[0]}'",
            f"{h} start < '{new}'", f"{h} start < '{sub}'", f"{h} end < '{old_end}'", f"cp {rec} '{snaps[1]}'",
            f"{h} end < '{end}'"])
        first = json.loads(snaps[0].read_text())
        self.assertEqual((first["tool"], first["ppid"], first["session_id"], first["cwd"]), ("copilot", pid, SID, "/proj/a"))
        self.assertEqual(first["env"]["tmux_pane"], "%7")
        self.assertNotIn("transcript_path", first)
        # /new rewrote it; a subagent's start and the old session's end left it.
        self.assertEqual(json.loads(snaps[1].read_text())["session_id"], SID2)
        self.assertEqual(list(self.state.glob("copilot-*.json")), [])   # its own end removed it

    def test_agy_files_the_conversation_its_turns_ran(self):
        start = self.payload("start", conversationId=SID, cwd="/proj/b", hookEventName="SessionStart")
        stop = self.payload("stop", conversationId=SID2, workspacePaths=["/proj/c"], terminationReason="NO_TOOL_CALL")
        sub = self.payload("sub", conversationId="c-sub", parentConversationId=SID2, cwd="/x")
        snaps = [self.root / f"snap{i}.json" for i in range(2)]
        h = f"bash '{TRACK}' agy"
        rec = f"'{self.state}'/agy-$$.json"
        pid = self.run_in("agy", [
            f"{h} start < '{start}'", f"cp {rec} '{snaps[0]}'",
            f"{h} start < '{stop}'", f"{h} start < '{sub}'", f"{h} start < '{stop}'", f"cp {rec} '{snaps[1]}'"])
        a = json.loads(snaps[0].read_text())
        self.assertEqual((a["tool"], a["ppid"], a["session_id"], a["cwd"]), ("agy", pid, SID, "/proj/b"))
        b = json.loads(snaps[1].read_text())
        self.assertEqual((b["session_id"], b["cwd"]), (SID2, "/proj/c"))   # workspacePaths[0]: no cwd given

    def test_nothing_is_written_outside_a_tui_in_tmux(self):
        p = self.payload("p", sessionId=SID, cwd="/a")
        h = f"bash '{TRACK}' copilot start < '{p}'"
        self.run_in("copilot", [h], flags=["-p"])                      # `copilot -p …`: headless
        self.run_in("copilot", [h], tty=False)                          # no tty
        self.run_in("copilot", [h], env=dict(self.env, CUA_NOTCH_IGNORE="1"))
        self.run_in("copilot", [h], env={k: v for k, v in self.env.items() if k != "TMUX_PANE"})
        self.run_in("agy", [f"bash '{TRACK}' copilot start < '{p}'"])   # the wrong CLI above it
        self.run_in("agy", [f"bash '{TRACK}' agy start < '{p}'"], flags=["-p"])   # agy -p: headless
        with p.open() as f:                                             # no CLI above it at all
            subprocess.run(["bash", str(TRACK), "copilot", "start"], stdin=f, env=self.env, check=True, timeout=30)
        self.assertFalse(self.state.exists() and any(self.state.iterdir()))


# assistant-save.sh sources this instead of the plugin (test_grok_resurrect's).
FAKE_PLUGIN = """set -euo pipefail
STATE_DIR="$FAKE_STATE"
OUTPUT_FILE="$FAKE_OUTPUT"
log() { printf '%s\\n' "$*" >>"$FAKE_STATE/log"; }
main() { cp "$FAKE_INPUT" "$OUTPUT_FILE"; }
strip_assistant_pane_contents() { echo strip >>"$FAKE_STATE/log"; }
"""
PANES = [("main:0.0", 100, "/pane/a"), ("work:1.0", 200, "/pane/b"), ("work:2.0", 300, "/pane/c"),
         ("work:3.0", 400, "/pane/d"), ("work:4.0", 500, "/pane/e"), ("work:5.0", 600, "/pane/f"),
         ("work:6.0", 700, "/pane/g"), ("work:7.0", 800, "/pane/h"), ("work:8.0", 900, "/pane/i"),
         ("work:9.0", 1000, "/pane/j")]
# {REC} is a live pid (records whose pid is gone are pruned).
PS = f"""\
  100     1 -zsh
  101   100 /opt/homebrew/bin/copilot --model gpt-5 --resume {SID2}
  102   101 copilot
  200     1 -zsh
  201   200 copilot -p hello
  300     1 -zsh
  301   300 copilot sessions
  400     1 zsh
  {{REC}}   400 /opt/homebrew/bin/agy --model g3
  500     1 -zsh
  501   500 agy --print hi
  600     1 -zsh
  601   600 agy -conversation={SID2} --sandbox
  700     1 -zsh
  701   700 copilot
  702   701 claude -p summarize
  800     1 -zsh
  801   800 agy
  900     1 -zsh
  901   900 copilot --yolo
 1000     1 -zsh
 1001  1000 agy models
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
class SaveRows(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="ca-save-")))
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
        self.rec = live_pid(self)
        (self.root / "ps.out").write_text(PS.replace("{REC}", str(self.rec)))
        (bin_ / "ps").write_text(f"#!/bin/sh\necho \"$*\" >>'{self.ps_log}'\ncat '{self.root}/ps.out'\n")
        us = "\x1f"
        (self.root / "panes.out").write_text("".join(f"{t}{us}{p}{us}{c}\n" for t, p, c in PANES))
        (bin_ / "tmux").write_text(
            f"#!/bin/sh\ncase \"$1\" in list-panes) cat '{self.root}/panes.out' ;; esac\nexit 0\n")
        self.pgrep = self.root / "pgrep.rc"
        self.pgrep.write_text("0")
        (bin_ / "pgrep").write_text(f"#!/bin/sh\nexit $(cat '{self.pgrep}')\n")
        # lsof -a -p <pid> -Fn: agy 801 holds its presence lock open.
        (bin_ / "lsof").write_text(f"""#!/bin/sh
[ "$3" = 801 ] && printf 'p801\\nn/x/.gemini/antigravity-cli/presence/{SID}.lock\\nn/dev/null\\n'
exit 0
""")
        for f in ("ps", "tmux", "pgrep", "lsof"):
            (bin_ / f).chmod(0o755)
        home = self.root / "home"
        # Copilot's own lock: pid 102 (the CLI under the supervisor 101) holds
        # SID, and 701 holds SID2 (with a workspace.yaml cwd).
        for sid, pid in ((SID, 102), (SID2, 701)):
            d = home / ".copilot/session-state" / sid
            d.mkdir(parents=True)
            (d / f"inuse.{pid}.lock").write_text(str(pid))
        (home / f".copilot/session-state/{SID2}/workspace.yaml").write_text(
            f"id: {SID2}\ncwd: /proj/ws\nclient_name: github/cli\n")
        self.upstream = [dict(pane="main:9.0", tool="claude", session_id="c-9", pid="9000"),
                         dict(pane="work:6.0", tool="claude", session_id="c-6", pid="702")]
        (self.root / "in.json").write_text(json.dumps(dict(timestamp="t", sessions=self.upstream)))
        self.out = self.root / "assistant-sessions.json"
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE", "COPILOT_HOME")}
        self.env.update(HOME=str(home), TMUX_ASSISTANT_RESURRECT_PLUGIN=str(self.root / "plugin"),
                        FAKE_STATE=str(self.state), FAKE_OUTPUT=str(self.out),
                        FAKE_INPUT=str(self.root / "in.json"), PATH=f"{bin_}:/usr/bin:/bin")

    def save(self):
        r = subprocess.run(["bash", str(SAVE)], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return {row["pane"]: row for row in json.loads(self.out.read_text())["sessions"]}

    def test_copilot_and_agy_tui_panes_get_rows(self):
        (self.state / f"agy-{self.rec}.json").write_text(json.dumps(dict(
            tool="agy", session_id="c-401", cwd="/proj/d", ppid=self.rec, env=dict(tmux_pane="%4", FOO="bar"))))
        rows = self.save()
        a = rows["main:0.0"]
        # Copilot's lock (held by the CLI under its supervisor) beats the argv.
        self.assertEqual((a["tool"], a["session_id"], a["pid"], a["cli_args"], a["model"]),
                         ("copilot", SID, "101", f"--model gpt-5 --resume {SID2}", ""))
        self.assertEqual(a["cwd"], "/pane/a")     # no record, no workspace.yaml: the pane's
        self.assertNotIn("work:1.0", rows)        # copilot -p: headless
        self.assertNotIn("work:2.0", rows)        # copilot sessions: a utility
        d = rows["work:3.0"]                      # agy: its record
        self.assertEqual((d["tool"], d["session_id"], d["cwd"], d["env"]["FOO"]), ("agy", "c-401", "/proj/d", "bar"))
        self.assertNotIn("work:4.0", rows)        # agy --print: headless
        self.assertEqual((rows["work:5.0"]["session_id"], rows["work:5.0"]["tool"]), (SID2, "agy"))   # its argv
        # A claude the copilot ran is replaced; the lock's workspace.yaml gives the cwd.
        g = rows["work:6.0"]
        self.assertEqual((g["tool"], g["session_id"], g["cwd"]), ("copilot", SID2, "/proj/ws"))
        self.assertEqual((rows["work:7.0"]["tool"], rows["work:7.0"]["session_id"]), ("agy", SID))  # presence lock
        self.assertNotIn("work:8.0", rows)        # copilot with no id anywhere: no row
        self.assertNotIn("work:9.0", rows)        # agy models
        self.assertEqual(rows["main:9.0"]["session_id"], "c-9")
        self.assertIn("strip", (self.state / "log").read_text())

    def test_a_pgrep_that_cannot_tell_means_scan(self):
        # macOS pgrep exits 3 under a UTF-8 locale when some argv[0] is not
        # UTF-8: that is "unknown", never "none running". No record here, so
        # only the scan finds the fresh copilot (by its lock).
        self.pgrep.write_text("3")
        rows = self.save()
        self.assertEqual((rows["main:0.0"]["tool"], rows["main:0.0"]["session_id"]), ("copilot", SID))

    def test_an_upstream_row_without_a_pid_keeps_its_pane(self):
        # Its empty pid must not match the copilot row's descendant list.
        (self.root / "in.json").write_text(json.dumps(dict(timestamp="t", sessions=[
            dict(pane="main:0.0", tool="claude", session_id="c-0", pid="")])))
        rows = self.save()
        self.assertEqual((rows["main:0.0"]["tool"], rows["main:0.0"]["session_id"]), ("claude", "c-0"))
        self.assertEqual(rows["work:6.0"]["tool"], "copilot")

    def test_neither_cli_in_use_costs_no_ps_and_stale_records_go(self):
        self.pgrep.write_text("1")
        stale = self.state / f"copilot-{dead_pid()}.json"
        stale.write_text(json.dumps(dict(session_id="gone", tool="copilot")))
        rows = self.save()
        self.assertEqual(sorted(rows), ["main:9.0", "work:6.0"])
        self.assertFalse(self.ps_log.exists())
        self.assertFalse(stale.exists())


def jq_prog():
    text = RESTORE.read_text()
    return re.search(r"read -r -d '' JQ_PROG <<'JQ'\n(.*?)\nJQ\n", text, re.S).group(1)


@unittest.skipUnless(JQ, "jq not installed")
class RestoreCommand(unittest.TestCase):
    def rows(self, *sessions):
        doc = json.dumps(dict(timestamp="t", sessions=list(sessions)))
        r = subprocess.run([JQ, "-r", "--argjson", "cap", '["FOO"]', "--arg", "codex", "command codex",
                            "--arg", "codexnu", "^codex", "--arg", "grok", "command grok", "--arg", "groknu", "^grok",
                            jq_prog()], input=doc, capture_output=True, text=True, check=True)
        return [line.split("\x1f") for line in r.stdout.splitlines()]

    def test_copilot_rows_resume_with_their_options(self):
        [row] = self.rows(dict(pane="w:1.0", tool="copilot", session_id="s-1", cwd="/p q",
                               cli_args="--model gpt-5 --allow-tool shell write --resume old -C /y --continue"
                                        " -n nm --name=x --session-id 1 --connect --yolo --resume=z --fleet"
                                        " --add-dir ../lib -i fix the -p flag --model z",
                               env=dict(FOO="b'r", tmux_pane="%1")))
        self.assertEqual(row[1:4], ["copilot", "s-1", "/p q"])
        self.assertEqual(row[4], "FOO='b'\"'\"'r' command copilot '--model' 'gpt-5' '--allow-tool' 'shell' 'write'"
                                 " '--yolo' '--add-dir' '../lib' --resume 's-1'")
        self.assertEqual(row[5], "if (r###'/p q'### | path exists) { cd r###'/p q'### }; "
                                 "with-env {FOO: r###'b'r'###} { ^copilot r###'--model'### r###'gpt-5'###"
                                 " r###'--allow-tool'### r###'shell'### r###'write'### r###'--yolo'###"
                                 " r###'--add-dir'### r###'../lib'### --resume r###'s-1'### }")

    def test_a_value_ps_split_is_dropped_with_its_option(self):
        # `--allow-tool 'shell(git push)'` reads `shell(git push)` in ps: its
        # parentheses no longer balance per word, so the option goes whole;
        # a balanced pattern stays.
        [row] = self.rows(dict(pane="w:1.0", tool="copilot", session_id="s-1",
                               cli_args="--allow-tool shell(git push) --deny-tool shell(rm:*) --model m"
                                        " --allow-url=x(a b) --yolo"))
        self.assertEqual(row[4], "command copilot '--deny-tool' 'shell(rm:*)' '--model' 'm' '--yolo' --resume 's-1'")

    def test_agy_rows_resume_with_their_leading_options(self):
        rows = self.rows(dict(pane="a:0.0", tool="agy", session_id="c-1",
                              cli_args="--model g3 -conversation old --sandbox -c --effort=high --new-project"
                                       " -conversation=x --add-dir /abs fix it --model z"),
                         dict(pane="b:0.0", tool="agy", session_id="c-1", cli_args="-i fix -p it"),
                         dict(pane="c:0.0", tool="agy", session_id="c-1", cli_args=""))
        self.assertEqual([r[4] for r in rows], [
            "command agy '--model' 'g3' '--sandbox' '--effort=high' '--add-dir' '/abs' --conversation 'c-1'",
            "command agy --conversation 'c-1'", "command agy --conversation 'c-1'"])
        self.assertEqual(rows[2][5], "^agy --conversation r###'c-1'###")


@unittest.skipUnless((fast.PLUGIN / "scripts/lib-detect.sh").exists(), "tmux-assistant-resurrect not installed")
class RestoreHook(fast.ThrowawayServer):
    def test_copilot_row_resumes(self):
        log = self.root / "stub.log"
        stubs = self.root / "stub"
        stubs.mkdir()
        for name in ("copilot", "agy"):
            (stubs / name).write_text(f'#!/bin/bash\necho "{name} $* @$PWD" >> "{log}"\n')
            (stubs / name).chmod(0o755)
        work = self.root / "proj"
        work.mkdir()
        with (self.root / "zdot/.zshrc").open("a") as rc:
            rc.write(f"export PATH='{stubs}:/usr/bin:/bin'\n")
        self.tmux("set", "-g", "@assistant-resurrect-client-wait", "0", ";", "new-session", "-d", "-s", "work",
                  ";", "new-window", "-t", "work:")
        (self.dir / "assistant-sessions.json").write_text(json.dumps(dict(timestamp="t", sessions=[
            dict(pane="work:0.0", tool="copilot", session_id=SID, cwd=str(work), cli_args="--model gpt-5"),
            dict(pane="work:1.0", tool="agy", session_id=SID2, cwd=str(work), cli_args="--sandbox fix it")])))
        subprocess.run(["bash", str(RESTORE)], env=self.env, check=True, capture_output=True, timeout=60)
        for _ in range(100):
            if log.exists() and len(log.read_text().splitlines()) == 2:
                break
            time.sleep(0.05)
        rlog = (self.dir / "assistant-restore.log").read_text()
        self.assertTrue(log.exists(), rlog)
        self.assertEqual(sorted(log.read_text().splitlines()),
                         [f"agy --sandbox --conversation {SID2} @{work}", f"copilot --model gpt-5 --resume {SID} @{work}"])
        self.assertIn("restored 2 of 2", rlog)


@unittest.skipUnless((fast.PLUGIN / "scripts/lib-detect.sh").exists(), "tmux-assistant-resurrect not installed")
class ClosedTabs(fast.ThrowawayServer):
    def setUp(self):
        super().setUp()
        root = self.root
        self.state = root / "xdg"
        self.track = root / "track"
        self.track.mkdir()
        bin_ = root / "bin"
        bin_.mkdir()
        for tool in ("tmux", "jq"):
            (bin_ / tool).symlink_to(shutil.which(tool))
        self.copilot = fake_cli(root / "fakebin", "copilot")
        self.agy = fake_cli(root / "fakebin", "agy")
        plugins = root / ".config/tmux/plugins"
        plugins.mkdir(parents=True)
        (plugins / "tmux-assistant-resurrect").symlink_to(fast.PLUGIN)
        self.proj = root / "proj"
        self.proj.mkdir()
        self.cenv = dict(self.env, XDG_STATE_HOME=str(self.state), NOTIFY_SH=str(root / "no-notify"),
                         NOTIFY_TMUX="true", TMUX_ASSISTANT_RESURRECT_DIR=str(self.track),
                         PATH=f"{bin_}:/usr/bin:/bin")
        self.cenv.pop("COPILOT_HOME", None)

    def run_ct(self, *args):
        r = subprocess.run(["bash", str(CLOSED), *args], env=self.cenv, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def history(self):
        f = self.state / "tmux-closed-tabs/history.jsonl"
        return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []

    def test_close_records_copilot_from_its_lock_and_reopen_is_a_copilot_agent(self):
        self.tmux("new-window", "-t", "main:", "-n", "cop", f"'{self.copilot}' -c 'sleep 600; true'")
        pane, pid = self.tmux("display", "-p", "-t", "main:1", "#{pane_id} #{pane_pid}").split()
        d = self.root / ".copilot/session-state" / SID
        d.mkdir(parents=True)
        (d / f"inuse.{pid}.lock").write_text(pid)
        (d / "workspace.yaml").write_text(f"id: {SID}\ncwd: {self.proj}\n")
        self.tmux("set", "-g", "@agent_birth", "set -w @born_kind")
        self.run_ct("close", pane)
        [rec] = self.history()
        self.assertEqual((rec["tool"], rec["session_id"], rec["cwd"]), ("copilot", SID, str(self.proj)))
        self.assertEqual(rec["cmd"], f"command copilot '-c' 'sleep' '600;' 'true' --resume '{SID}'")
        self.run_ct("reopen", "main", "")
        self.assertEqual(self.history(), [])
        win = self.tmux("list-windows", "-t", "main:", "-F", "#{window_index} #{@born_kind} #{@agent_summary}")
        self.assertIn("1 copilot cop", win.splitlines())

    def test_close_records_agy_from_its_record(self):
        self.tmux("new-window", "-t", "main:", "-n", "ag", f"'{self.agy}' -c 'sleep 600; true'")
        pane, pid = self.tmux("display", "-p", "-t", "main:1", "#{pane_id} #{pane_pid}").split()
        (self.track / f"agy-{pid}.json").write_text(json.dumps(dict(
            tool="agy", session_id=SID2, cwd=str(self.proj), ppid=int(pid), env=dict(tmux_pane=pane))))
        self.run_ct("close", pane)
        [rec] = self.history()
        self.assertEqual((rec["tool"], rec["session_id"], rec["cwd"]), ("agy", SID2, str(self.proj)))
        # (`-c` is agy's --continue, dropped; the first bare word ends the options.)
        self.assertEqual(rec["cmd"], f"command agy --conversation '{SID2}'")

    def test_a_headless_copilot_is_not_an_agent(self):
        self.tmux("new-window", "-t", "main:", f"'{self.copilot}' -p -c 'sleep 600; true'")  # bash's own -p
        pane = self.tmux("display", "-p", "-t", "main:1", "#{pane_id}").strip()
        self.run_ct("close", pane)
        [rec] = self.history()
        self.assertEqual((rec["tool"], rec["cmd"]), ("", ""))

    def test_resume_commands(self):
        script = (f"set -- list; source '{CLOSED}' >/dev/null; "
                  "build_cmd copilot s-1 '--model m --allow-tool a b -r old --connect -C /x -n nm --continue "
                  "--resume=z --yolo -i fix -p it' '' '{}'; "
                  "build_cmd agy c-1 '--model g3 -conversation old --sandbox -c --effort=high fix it' '' '{}'")
        r = subprocess.run(["bash", "-c", script], env=self.cenv, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.splitlines(), [
            "command copilot '--model' 'm' '--allow-tool' 'a' 'b' '--yolo' --resume 's-1'",
            "command agy '--model' 'g3' '--sandbox' '--effort=high' --conversation 'c-1'"], r.stderr)
        script = (f"set -- list; source '{CLOSED}' >/dev/null; "
                  "build_cmd copilot s-1 '--allow-tool shell(git push) --deny-tool shell(rm:*) --model m "
                  "--allow-url=x(a b) --yolo' '' '{}'; build_cmd copilot s-2 '' '' '{}'")
        r = subprocess.run(["bash", "-c", script], env=self.cenv, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.splitlines(), [
            "command copilot '--deny-tool' 'shell(rm:*)' '--model' 'm' '--yolo' --resume 's-1'",
            "command copilot --resume 's-2'"], r.stderr)

    def test_transcripts(self):
        d = self.root / ".copilot/session-state" / SID
        d.mkdir(parents=True)
        (d / "events.jsonl").write_text("\n".join(json.dumps(e) for e in [
            dict(type="session.start", data=dict(sessionId=SID)),
            dict(type="user.message", data=dict(content="Fix the roast bot")),
            dict(type="assistant.message", data=dict(content="Looking at it.", toolRequests=[
                dict(name="view", arguments=dict(path="bot.py"))])),
        ]) + "\n")
        t = self.root / ".gemini/antigravity-cli/brain" / SID2 / ".system_generated/logs"
        t.mkdir(parents=True)
        (t / "transcript.jsonl").write_text("\n".join(json.dumps(e) for e in [
            dict(type="USER_INPUT", content="<USER_REQUEST>\nPort the parser\n</USER_REQUEST>\n<ADDITIONAL_METADATA>x"),
            dict(type="PLANNER_RESPONSE", content="Porting."),
            dict(type="GENERIC", content="noise"),
            dict(type="USER_INPUT", content="<USER_REQUEST>\nand the tests"),
        ]) + "\n")
        script = (f"set -- list; source '{CLOSED}' >/dev/null; transcript_digest copilot {SID}; "
                  f"echo ---; transcript_digest agy {SID2}")
        r = subprocess.run(["bash", "-c", script], env=self.cenv, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.splitlines(), [
            "USER: Fix the roast bot", "ASSISTANT: Looking at it.", '  [view] {"path":"bot.py"}', "---",
            "USER: Port the parser", "ASSISTANT: Porting.", "USER: and the tests"], r.stderr)


# Every port of the TUI rule: the watcher's (the reference), the record hook's,
# closed-tabs' ca_kind, the save's awk and agent-pause.py's.
PORTS_HARNESS = r'''
S=$1
for f in copilot_tui_args agy_tui_args; do
    eval "$(awk "/^$f\(\) \{/{f=1} f{print} f&&/^\}/{exit}" "$S/executable_agent-tab-watcher.sh")"
    hk=$(awk "/^$f\(\) \{/{f=1} f{print} f&&/^\}/{exit}" "$S/executable_copilot-agy-session-track.sh")
    eval "${hk/$f()/hook_$f()}"
done
eval "$(awk '/^ca_kind\(\) \{/{f=1} f{print} f&&/^\}/{exit}' "$S/executable_closed-tabs.sh")"
fn=$(awk '/function tui\(kind, line/{f=1} f{print} f&&/^\t\t\}$/{exit}' "$S/executable_assistant-save.sh")
begin=$(awk '/^ca_rows\(\) \{/{c=1} c&&/^\t\tBEGIN \{/{f=1} f{print} f&&/^\t\t\}$/{exit}' "$S/executable_assistant-save.sh")
v() { [ "$1" = 0 ] && echo T || echo N; }
while IFS= read -r line; do
    k=${line%% *}
    "${k}_tui_args" "$line"; w=$?
    "hook_${k}_tui_args" "$line"; h=$?
    [ "$(ca_kind "$line")" = "$k" ]; c=$?
    a=$(printf '%s\n' "$line" | awk "$fn $begin { print tui(\"$k\", \$0) }")
    case $a in 1) a=0 ;; *) a=1 ;; esac
    echo "$line|$(v $w)|$(v $h)|$(v $c)|$(v $a)"
done
'''
PORT_CASES = {
    "copilot": "T", "copilot --resume": "T", f"copilot --resume {SID}": "T", "copilot --model gpt-5 --yolo": "T",
    "copilot -i fix the -p flag": "T", "copilot -- sessions": "T", "copilot -n help": "T",
    "copilot -p hi": "N", "copilot -phi": "N", "copilot --prompt=x": "N", "copilot --help": "N", "copilot -v": "N",
    "copilot sessions": "N", "copilot --model x login": "N", "copilot --yolo fix -p": "T",
    "agy": "T", f"agy --conversation {SID}": "T", f"agy -conversation={SID}": "T", "agy -c": "T",
    "agy -i fix -p": "T", "agy --model g3 fix it": "T", "agy -- models": "T",
    "agy -p hi": "N", "agy --print hi": "N", "agy -prompt=x": "N", "agy --help": "N", "agy -version": "N",
    "agy models": "N", "agy --model g3 plugin list": "N"}


class TuiFilterPortsAgree(unittest.TestCase):
    def test_watcher_hook_closed_tabs_and_save_agree(self):
        r = subprocess.run(["bash", "-c", PORTS_HARNESS, "harness", str(SCRIPTS)], input="\n".join(PORT_CASES) + "\n",
                           capture_output=True, text=True, timeout=60)
        got = {}
        for row in r.stdout.splitlines():
            line, *verdicts = row.split("|")
            got[line] = verdicts
        self.assertEqual(got, {k: [v] * 4 for k, v in PORT_CASES.items()}, r.stderr)

    def test_agent_pause_agrees(self):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("agent_pause_ports", str(SCRIPTS / "executable_agent-pause.py"))
        spec = importlib.util.spec_from_loader("agent_pause_ports", loader)
        ap = importlib.util.module_from_spec(spec)
        loader.exec_module(ap)
        for line, want in PORT_CASES.items():
            f = ap.copilot_tui_args if line.startswith("copilot") else ap.agy_tui_args
            self.assertEqual("T" if f(line) else "N", want, line)


if __name__ == "__main__":
    unittest.main()
