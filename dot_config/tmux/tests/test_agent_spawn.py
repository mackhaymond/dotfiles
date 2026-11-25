"""agent-spawn (dot_local/bin/executable_agent-spawn), its abby-delegate shim,
agent-launch.sh's `table` verb and agent-self-lib.sh: external subagents
(plan: external-subagents/plan.md §2.1-2.3).

Unit tests load the CLI as a module: the per-harness transcript readers on
the fixtures in tests/fixtures/agent-spawn/ (one per harness), the launch
lines built from agent-launch.sh's table, the exit codes (A1), Codex hook
trust and Grok leader mode.

Live tests run against a PRIVATE tmux server (`tmux -L <unique> -f
/dev/null`, a private short TMUX_TMPDIR, TMUX unset, killed in cleanup). No
real agent ever runs: the server's default-command is a FAKE HARNESS that
records the tab's ZSH_AUTOSTART and env, prints a startup dialog when told
to, and (when told to) plays one turn the way the hooks would: running with
@agent_turn_start / @agent_turn_origin, then idle with @agent_turn_end,
reply.md, @agent_reply_at and @agent_spawn_result=ready (U1's writes). Other
cases set those options directly. tmux-mcp is a stub HTTP server in this
process (TMUX_MCP_URL); the token comes from the fake HOME's token file.
Run with unittest or pytest.
"""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TMUXDIR = Path(__file__).resolve().parent.parent           # dot_config/tmux
CHEZ = TMUXDIR.parent.parent
SPAWN = CHEZ / "dot_local/bin/executable_agent-spawn"
SHIM = CHEZ / "dot_local/bin/executable_abby-delegate"
SCRIPTS = TMUXDIR / "scripts"
LAUNCH = SCRIPTS / "executable_agent-launch.sh"
FIX = TMUXDIR / "tests/fixtures/agent-spawn"
REAL_TMUX = shutil.which("tmux")


def load_cli():
    ldr = importlib.machinery.SourceFileLoader("agent_spawn_cli", str(SPAWN))
    spec = importlib.util.spec_from_loader("agent_spawn_cli", ldr)
    mod = importlib.util.module_from_spec(spec)
    ldr.exec_module(mod)
    return mod


AS = load_cli()


def write_exe(path, text):
    Path(path).write_text(text)
    os.chmod(path, 0o755)


class Unit(unittest.TestCase):
    def test_exit_codes_are_pinned(self):
        self.assertEqual((AS.EXIT_DONE, AS.EXIT_NEEDS, AS.EXIT_FAILED, AS.EXIT_GONE, AS.EXIT_REFUSED,
                          AS.EXIT_TIMEOUT, AS.EXIT_USAGE), (0, 10, 11, 12, 13, 75, 2))
        self.assertEqual(AS.STATUS_EXIT["cancelled"], 12)
        r = subprocess.run(["python3", str(SPAWN), "wait"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)                       # argparse usage

    def test_id_format(self):
        ids = {AS.new_id() for _ in range(20)}
        for i in ids:
            self.assertRegex(i, r"^s-[0-9a-z]{6}[0-9a-z]{4}$")
            self.assertTrue(AS.valid_id(i))
        self.assertGreater(len(ids), 15)
        self.assertFalse(AS.valid_id("../x"))

    def test_one_line(self):
        self.assertEqual(AS.one_line("fix #3 | now;\n\tthen ;"), "fix 3 now; then")
        self.assertEqual(len(AS.one_line("é" * 200)), 80)

    def test_readers_on_fixtures(self):
        self.assertEqual(AS.read_claude(FIX / "claude.jsonl"), "Final part A.\n\nFinal part B: ünïcode kept.")
        self.assertEqual(AS.read_codex(FIX / "codex.jsonl"), "Codex final answer.")
        self.assertEqual(AS.read_copilot(FIX / "copilot-events.jsonl"), "Copilot final answer.")
        self.assertEqual(AS.read_agy(FIX / "agy-transcript.jsonl"), "Agy final answer.")
        self.assertEqual(AS.read_opencode_export((FIX / "opencode-export.json").read_text()), "OpenCode final answer.")
        self.assertEqual(AS.read_grok_export((FIX / "grok-export.md").read_text()),
                         "Grok final line one.\nGrok final line two.")
        self.assertIsNone(AS.read_claude(FIX / "missing.jsonl"))

    def test_table_and_launch_lines(self):
        env = dict(os.environ, LAUNCH_PATH="/nonexistent", LAUNCH_HOME="/nonexistent")
        env.pop("TMUX", None)
        out = subprocess.run(["bash", str(LAUNCH), "table"], capture_output=True, text=True, env=env).stdout
        rows = {l.split()[0]: l.split() for l in out.splitlines()}
        self.assertEqual(rows["opencode"], ["opencode", "o", "opencode", "opencode", "-", "--prompt", "-m", "0"])
        self.assertEqual(rows["aider"][5:7], ["-", "-"])
        AS.LAUNCH = str(LAUNCH)
        t = {k: dict(zip(("id", "letter", "cmd", "kind", "harness", "prompt", "model", "installed"), v))
             for k, v in rows.items()}
        f = "/s/s-x/task.md"
        q = "\"$(<'/s/s-x/task.md')\""
        old_home = AS.HOME
        AS.HOME = "/nonexistent"
        try:
            self.assertEqual(AS.launch_line(t["claude"], f, "opus"),
                             "unset CLAUDE_CONFIG_DIR ANTHROPIC_BASE_URL ANTHROPIC_CUSTOM_HEADERS; "
                             "command claude --model 'opus' -- " + q)
            self.assertEqual(AS.launch_line(t["abby"], f), "command abby " + q)
            self.assertEqual(AS.launch_line(t["codex"], f, "m1"), "command codex -m 'm1' -- " + q)
            self.assertEqual(AS.launch_line(t["opencode"], f, "p/m"), "command opencode -m 'p/m' --prompt " + q)
            self.assertEqual(AS.launch_line(t["copilot"], f, auto=True), "command copilot --allow-all-tools -i " + q)
            self.assertEqual(AS.launch_line(t["agy"], f, "g"), "command agy --model 'g' -i " + q)
            self.assertTrue(AS.launch_line(t["grok"], f).endswith("grok -- " + q))
            with self.assertRaises(AS.Fail) as c:
                AS.launch_line(dict(t["copilot"], prompt="-p"), f)
            self.assertEqual(c.exception.code, 13)
            with self.assertRaises(AS.Fail):
                AS.launch_line(t["aider"], f)
        finally:
            AS.HOME = old_home

    def test_codex_trust_and_grok_leader(self):
        tmp = Path(tempfile.mkdtemp(prefix="as-unit-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / ".codex").mkdir()
        hp = tmp / ".codex/hooks.json"
        hp.write_text(json.dumps({"hooks": {
            "SessionStart": [{"hooks": [{"command": "x"}]}, {"hooks": [{"command": "~/agent-tab-indicator.sh idle codex"}]}],
            "Stop": [{"hooks": [{"command": "~/agent-tab-indicator.sh done codex"}]}]}}))
        cfg = tmp / ".codex/config.toml"
        good = ('[hooks.state."%s:session_start:1:0"]\ntrusted_hash = "sha256:a"\n'
                '[hooks.state."%s:stop:0:0"]\ntrusted_hash = "sha256:b"\n') % (hp, hp)
        old = AS.HOME
        AS.HOME = str(tmp)
        try:
            cfg.write_text(good)
            self.assertIsNone(AS.codex_trust())
            cfg.write_text(good.split("[hooks.state.\"%s:stop" % hp)[0])
            self.assertIn("Stop", AS.codex_trust())
            (tmp / ".grok").mkdir()
            (tmp / ".grok/config.toml").write_text("[cli]\nuse_leader = true\n")
            os.environ["GROK_HOME"] = str(tmp / ".grok")
            self.assertTrue(AS.grok_leader())
        finally:
            AS.HOME = old
            os.environ.pop("GROK_HOME", None)


class Observe(unittest.TestCase):
    """observe/collect on hand-made rows (no tmux)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="as-obs-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.old = AS.STATE
        AS.STATE = self.dir
        self.addCleanup(setattr, AS, "STATE", self.old)
        self.t = AS.now()

    def meta(self, turns):
        sid = "s-" + uuid.uuid4().hex[:10]
        os.makedirs(os.path.join(self.dir, sid))
        m = {"id": sid, "created": self.t - 300, "mode": "async", "notify": True, "server": {},
             "parent": {"window": "@1", "pane": "%1"},
             "child": {"pane": "%9", "window": "@9", "kind": "claude", "harness": "claude"}, "turns": turns}
        AS.write_file(AS.reg(sid, "meta.json"), json.dumps(m))
        return m

    def row(self, m, **k):
        r = dict.fromkeys(AS.ROW_FIELDS, "")
        r.update(pane="%9", win="@9", spawn_id=m["id"])
        r.update(k)
        return r

    class NoScan:
        def out(self, *a):
            raise AssertionError("scanned the screen: %r" % (a,))

        def run(self, *a):
            return None

    def test_busy_child_is_running_not_unstarted(self):
        t = self.t
        m = self.meta([{"n": 1, "sent_at": t - 900, "turn_start": t - 890, "status": "running"},
                       {"n": 2, "sent_at": t - 600, "status": "pending"}])
        for st, hold in (("running", ""), ("idle", "1")):
            r = self.row(m, state=st, turn_start=str(t - 890), turn_origin="user t1 -", kids_hold=hold)
            self.assertEqual(AS.observe(m, r, self.NoScan())[0], "running")

    def test_held_or_paused_turn_end_is_not_done(self):
        t = self.t
        m = self.meta([{"n": 1, "sent_at": t - 300, "turn_start": t - 290, "status": "running"}])
        end = "%d t1 loud" % (t - 5)
        base = dict(turn_start=str(t - 290), turn_origin="user t1 -", turn_end=end)
        self.assertEqual(AS.observe(m, self.row(m, state="running", **base))[0], "running")
        self.assertEqual(AS.observe(m, self.row(m, state="idle", kids_hold="1", **base))[0], "running")
        self.assertEqual(AS.observe(m, self.row(m, state="paused", **base))[0], "paused")
        r = self.row(m, state="idle", **dict(base, turn_end="%d t1 paused" % (t - 5)))
        res = AS.collect(m, {"%9": r}, self.NoScan.__new__(self.NoScan))
        self.assertEqual((res["status"], res["result"]), ("paused", None))
        self.assertFalse(os.path.exists(AS.reg(m["id"], "reply-1.md")))
        self.assertEqual(AS.observe(m, self.row(m, state="idle", **base))[0], "done")

    def test_turn_ids_beat_epochs(self):
        t = self.t
        r = {"turn_origin": "user t2 -", "turn_end": "%d t2 loud" % (t - 100)}
        self.assertIsNotNone(AS.turn_ended(r, t - 50))        # ids match: the epoch is not compared
        r["turn_end"] = "%d t1 loud" % t
        self.assertIsNone(AS.turn_ended(r, t - 50))
        r = {"turn_origin": "user - -", "turn_end": "%d - done" % (t - 100)}
        self.assertIsNone(AS.turn_ended(r, t - 50))
        # A `send` turn with no ids must start strictly after the message.
        m = self.meta([{"n": 1, "sent_at": t - 300, "turn_start": t - 290, "status": "done"},
                       {"n": 2, "sent_at": t - 20, "status": "pending"}])
        r = self.row(m, state="idle", turn_start=str(t - 20), turn_origin="user - -", turn_end="%d - loud" % (t - 20))
        self.assertEqual(AS.observe(m, r, None)[0], "starting")


class FakeMcp:
    """tmux-mcp's HTTP endpoint, recording every tools/call."""

    def __init__(self):
        calls = self.calls = []
        replies = self.replies = []     # texts to answer tools/call with, in order

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_DELETE(self):
                self.send_response(200)
                self.end_headers()

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if "id" not in body:
                    self.send_response(202)
                    self.end_headers()
                    return
                if body.get("method") == "tools/call":
                    calls.append({"auth": self.headers.get("Authorization"), "pane": self.headers.get("X-Tmux-Pane"),
                                  "name": body["params"]["name"], "args": body["params"]["arguments"]})
                    rp = replies.pop(0) if replies else "delivered"
                    if isinstance(rp, str):
                        rp = {"text": rp}
                    res = {"content": [{"type": "text", "text": rp["text"]}], "isError": rp.get("isError", False)}
                else:
                    res = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "fake"}}
                data = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": res}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("mcp-session-id", "sess-1")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/mcp" % self.srv.server_address[1]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


FAKE_HARNESS = r"""#!/bin/sh
# The tab's "agent": records what it was launched with, never runs one.
printf '%s\n' "$ZSH_AUTOSTART" > "$FAKE_DIR/autostart-$AGENT_SPAWN_ID"
env > "$FAKE_DIR/env-$AGENT_SPAWN_ID"
[ -f "$FAKE_DIR/dialog" ] && cat "$FAKE_DIR/dialog"
[ -f "$FAKE_DIR/screen" ] && cat "$FAKE_DIR/screen"
if [ -f "$FAKE_DIR/auto" ]; then
    w=$(tmux display-message -p -t "$TMUX_PANE" '#{window_id}')
    sleep 0.3
    now=$(date +%s)
    tmux set -w -t "$w" @agent_state running \; set -w -t "$w" @agent_turn_start "$now" \; \
         set -w -t "$w" @agent_turn_origin "user p1 -"
    sleep 0.3
    printf 'Fake reply for %s\n' "$AGENT_SPAWN_ID" > "$HOME/.local/state/agent-spawn/$AGENT_SPAWN_ID/reply.md"
    now=$(date +%s)
    tmux set -w -t "$w" @agent_state idle \; set -w -t "$w" @agent_since "$now idle" \; \
         set -w -t "$w" @agent_turn_end "$now p1 loud" \; set -w -t "$w" @agent_reply_at "$now" \; \
         set -w -t "$w" @agent_spawn_result ready
fi
exec sleep 600
"""


@unittest.skipUnless(REAL_TMUX, "tmux executable required")
class Live(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="u2-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "h"
        (self.home / ".local/state/tmux-mcp").mkdir(parents=True)
        (self.home / ".local/state/tmux-mcp/token").write_text("test-token\n")
        self.bin = self.tmp / "b"
        self.bin.mkdir()
        for c in ("claude", "abby", "codex", "opencode", "grok", "copilot", "agy", "cursor-agent", "aider"):
            write_exe(self.bin / c, "#!/bin/sh\nexit 1\n")
        write_exe(self.bin / "agent-spawn", '#!/bin/sh\nexec python3 "%s" "$@"\n' % SPAWN)
        self.fake = self.tmp / "f"
        self.fake.mkdir()
        write_exe(self.tmp / "harness.sh", FAKE_HARNESS)
        write_exe(self.tmp / "closed-tabs.sh",
                  '#!/bin/sh\necho "$@" >> "%s/closed.log"\ncase "$1" in close|close-family) tmux kill-pane -t "$2" ;; esac\nexit 0\n' % self.fake)
        self.res = self.tmp / "r"
        self.res.mkdir()
        self.tmpdir = self.tmp / "t"
        self.tmpdir.mkdir()
        self.name = "u2-" + uuid.uuid4().hex[:8]
        path = "%s:%s:/usr/bin:/bin" % (self.bin, os.path.dirname(REAL_TMUX))
        self.base = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE", "CODEX_THREAD_ID")}
        self.base.update(HOME=str(self.home), PATH=path, TMUX_TMPDIR=str(self.tmpdir), FAKE_DIR=str(self.fake))
        self.socket = None
        self.addCleanup(self.cleanup)
        r = self.tm("new-session", "-d", "-s", "main", "-x", "120", "-y", "30", "-P", "-F",
                    "#{pane_id} #{window_id} #{socket_path} #{pid}", "sleep 600")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.ppane, self.pwin, self.socket, pid = r.stdout.split()
        self.tm("set", "-g", "default-shell", "/bin/sh")
        self.tm("set", "-g", "default-command", str(self.tmp / "harness.sh"))
        self.setw(self.pwin, **{"@agent_state": "idle", "@agent_kind": "claude", "@agent_key": "a"})
        self.mcp = FakeMcp()
        self.addCleanup(self.mcp.close)
        self.env = dict(self.base, TMUX="%s,%s,0" % (self.socket, pid), TMUX_PANE=self.ppane,
                        LAUNCH_PATH=str(self.bin), LAUNCH_HOME=str(self.home),
                        AGENT_SPAWN_LAUNCH=str(LAUNCH), AGENT_SPAWN_SELF_LIB=str(SCRIPTS / "agent-self-lib.sh"),
                        AGENT_SPAWN_SESSION_LIB=str(SCRIPTS / "agent-session-lib.sh"),
                        AGENT_SPAWN_CLOSED_TABS=str(self.tmp / "closed-tabs.sh"),
                        AGENT_SPAWN_POLL="0.1", AGENT_SPAWN_BATCH_S="0", AGENT_SPAWN_DIALOG_AFTER="0",
                        AGENT_SPAWN_FOLLOWUP_GRACE="1", AGENT_SPAWN_RETRY_S="0.1",
                        TMUX_MCP_URL=self.mcp.url, TMUX_ASSISTANT_RESURRECT_DIR=str(self.res),
                        AGENT_SPAWN_BIN=str(self.bin / "agent-spawn"))
        self.env.pop("AGENT_SPAWN_STATE", None)
        self.state = self.home / ".local/state/agent-spawn"

    def cleanup(self):
        subprocess.run([REAL_TMUX, "-L", self.name, "-f", "/dev/null", "kill-server"], capture_output=True,
                       env=self.base, timeout=5)
        if self.socket:
            Path(self.socket).unlink(missing_ok=True)

    # --- helpers ---

    def tm(self, *a):
        return subprocess.run([REAL_TMUX, "-L", self.name, "-f", "/dev/null", *a], capture_output=True,
                              text=True, env=self.base, timeout=10)

    def opt(self, win, name):
        return self.tm("show-options", "-wqv", "-t", win, name).stdout.rstrip("\n")

    def setw(self, win, **opts):
        for k, v in opts.items():
            r = self.tm("set-option", "-w", "-t", win, k, str(v))
            self.assertEqual(r.returncode, 0, r.stderr)

    def cli(self, *a, timeout=30, input=None, **env):
        return subprocess.run(["python3", str(SPAWN), *a], capture_output=True, text=True, input=input,
                              env=dict(self.env, **env), timeout=timeout)

    def spawn(self, *a, **env):
        r = self.cli("spawn", *a, **env)
        self.assertEqual(r.returncode, 0, r.stderr)
        sid, pane, win, letter = r.stdout.split()
        return sid, pane, win

    def meta(self, sid):
        return json.loads((self.state / sid / "meta.json").read_text())

    def agent_window(self, session="main", **opts):
        r = self.tm("new-window", "-d", "-t", session, "-P", "-F", "#{pane_id} #{window_id}", "sleep 600")
        pane, win = r.stdout.split()
        self.setw(win, **dict({"@agent_state": "idle"}, **opts))
        return pane, win

    def start_turn(self, win, tid, ts=None):
        ts = ts or int(time.time())
        self.setw(win, **{"@agent_state": "running", "@agent_turn_start": ts, "@agent_turn_origin": "agent %s x" % tid})
        return ts

    def end_turn(self, win, tid, kind="loud", state="idle", epoch=None):
        e = epoch or int(time.time())
        self.setw(win, **{"@agent_state": state, "@agent_turn_end": "%d %s %s" % (e, tid, kind)})

    # --- spawn ---

    def test_spawn_births_child_in_place_and_never_moves_focus(self):
        active = self.tm("display-message", "-p", "-t", "main", "#{window_id}").stdout.strip()
        s1, p1, w1 = self.spawn("--task", "Parser rewrite", "--", "rewrite the parser")
        s2, p2, w2 = self.spawn("--harness", "codex", "--model", "m9", "--", "second\nline two")
        self.assertEqual(self.tm("display-message", "-p", "-t", "main", "#{window_id}").stdout.strip(), active)
        order = self.tm("list-windows", "-t", "main", "-F", "#{window_id}").stdout.split()
        self.assertEqual(order[:3], [self.pwin, w1, w2])               # after the parent's last child
        want = {"@agent_state": "idle", "@agent_kind": "claude", "@agent_parent": self.pwin,
                "@agent_parent_pane": self.ppane, "@agent_spawn_id": s1, "@agent_spawn_task": "Parser rewrite",
                "@agent_spawn_mode": "async", "@agent_key": ""}
        self.assertEqual({k: self.opt(w1, k) for k in want}, want)
        self.assertRegex(self.opt(w1, "@agent_born"), r"^\d+$")
        self.assertRegex(self.opt(w1, "@agent_since"), r"^\d+ idle$")
        self.assertTrue(self.opt(w1, "@agent_summary").endswith("/New Session"))
        self.assertEqual((self.opt(w2, "@agent_kind"), self.opt(w2, "@agent_spawn_task")), ("codex", "second"))
        self.assertEqual(self.opt(self.pwin, "@agent_kids"), "2 2")      # kids_reconcile ran
        # The launch line and env, as the tab got them.
        for _ in range(50):
            if (self.fake / ("autostart-" + s2)).exists():
                break
            time.sleep(0.1)
        task = self.state / s1 / "task.md"
        self.assertEqual((self.fake / ("autostart-" + s1)).read_text().strip(),
                         "unset CLAUDE_CONFIG_DIR ANTHROPIC_BASE_URL ANTHROPIC_CUSTOM_HEADERS; command claude -- "
                         "\"$(<'%s')\"" % task)
        self.assertIn("command codex -m 'm9' -- ", (self.fake / ("autostart-" + s2)).read_text())
        env = (self.fake / ("env-" + s1)).read_text().splitlines()
        for kv in ("AGENT_ROLE=child", "AGENT_SPAWN_ID=" + s1, "AGENT_PARENT_PANE=" + self.ppane, "AGENT_PARENT_KEY=a"):
            self.assertIn(kv, env)
        body = task.read_text()
        self.assertTrue(body.startswith("[task from @a · agent-spawn %s]\n\nrewrite the parser\n" % s1))
        self.assertIn("never run agent-needs-you", body)
        self.assertIn("put it in your final reply", body)
        # Registry: 0600 files in a 0700 dir; the server it belongs to.
        self.assertEqual(stat.S_IMODE(os.stat(self.state / s1).st_mode), 0o700)
        for f in ("task.md", "meta.json", "events.log"):
            self.assertEqual(stat.S_IMODE(os.stat(self.state / s1 / f).st_mode), 0o600, f)
        m = self.meta(s1)
        self.assertEqual((m["child"]["pane"], m["child"]["window"], m["mode"], m["notify"]), (p1, w1, "async", True))
        self.assertEqual(m["server"]["socket"], os.path.realpath(self.socket))
        self.assertTrue(m["server"]["start_time"].isdigit())
        self.assertEqual(m["parent"]["window"], self.pwin)
        self.assertEqual(m["turns"][0]["status"], "pending")
        # tmux-mcp's call: --parent and --json on every verb, the prompt on
        # stdin (`-- -`), the registry where AGENT_SPAWN_DIR says.
        other = self.tmp / "reg"
        r = self.cli("spawn", "--harness", "claude", "--task", "T", "--no-notify", "--parent", self.ppane,
                     "--json", "--", "-", input="from stdin\n", TMUX_PANE="", AGENT_SPAWN_DIR=str(other))
        self.assertEqual(r.returncode, 0, r.stderr)
        info = json.loads(r.stdout)
        self.assertEqual(set(info), {"id", "pane", "window", "letter", "harness", "dir"})
        self.assertIn("\n\nfrom stdin\n", (other / info["id"] / "task.md").read_text())
        self.assertEqual(json.loads((other / info["id"] / "meta.json").read_text())["parent"]["pane"], self.ppane)
        for verb in (["wait", "--all", "--timeout", "0.2"], ["result", "--max-chars", "5"], ["cancel"]):
            r = self.cli(*verb, "--parent", self.ppane, "--json", info["id"], AGENT_SPAWN_DIR=str(other))
            self.assertIn(r.returncode, (0, 12, 75), (verb, r.stderr))
            self.assertIn(info["id"], r.stdout)
        r = self.cli("list", "--parent", self.ppane, "--json", AGENT_SPAWN_DIR=str(other))
        self.assertEqual([x["id"] for x in json.loads(r.stdout)], [info["id"]])
        self.assertIn("result", json.loads(r.stdout)[0])

    def test_refusals(self):
        def refused(*a, **env):
            r = self.cli("spawn", *a, "--", "x", **env)
            self.assertEqual(r.returncode, 13, (a, r.stdout, r.stderr))
            return r.stderr
        self.assertIn("hookless", refused("--harness", "cursor"))
        self.assertIn("hookless", refused("--harness", "aider"))
        # A home tab, and a hidden session: refused unless --space.
        self.setw(self.pwin, **{"@space_home": "1"})
        self.assertIn("home", refused())
        self.spawn("--space", "main", "--", "ok")
        self.tm("set-option", "-wu", "-t", self.pwin, "@space_home")
        self.tm("new-session", "-d", "-s", "agents", "sleep 600")
        self.tm("set-option", "-t", "agents", "@space_hidden", "1")
        hp, hw = self.agent_window("agents")
        self.assertIn("hidden", refused(TMUX_PANE=hp))
        # Not an agent at all.
        sp = self.tm("new-window", "-d", "-t", "main", "-P", "-F", "#{pane_id}", "sleep 600").stdout.strip()
        self.assertIn("agent window", refused(TMUX_PANE=sp))
        # Depth: a grandchild is fine, a great-grandchild is not.
        _, aw = self.agent_window()
        bp, bw = self.agent_window(**{"@agent_parent": aw})
        cp, cw = self.agent_window(**{"@agent_parent": bw})
        self.assertIn("deep", refused(TMUX_PANE=cp))
        self.spawn("--force", "--", "x", TMUX_PANE=cp)
        # Fan-out: the root (aw) has b, c, and c's forced child: three more is six.
        for _ in range(3):
            self.spawn("--", "x", TMUX_PANE=bp)
        self.assertIn("live children", refused(TMUX_PANE=bp))
        # Not installed.
        os.unlink(self.bin / "agy")
        self.assertIn("not installed", refused("--harness", "agy"))

    # --- wait ---

    def test_wait_exit_codes(self):
        cases = {}
        for name in ("needs", "failed", "gone", "timeout", "unstarted"):
            cases[name] = self.spawn("--", name)
        s, _, w = cases["needs"]
        self.setw(w, **{"@agent_state": "needs-input", "@agent_detail": "Bash: rm -rf"})
        r = self.cli("wait", s)
        self.assertEqual(r.returncode, 10, r.stderr)
        self.assertIn("needs-input", r.stdout.splitlines()[0])
        s, _, w = cases["failed"]
        self.start_turn(w, "p1")
        self.end_turn(w, "p1", kind="failed", state="failed")
        self.assertEqual(self.cli("wait", s).returncode, 11)
        s, _, w = cases["gone"]
        self.tm("kill-window", "-t", w)
        r = self.cli("wait", s, "--json")
        self.assertEqual((r.returncode, json.loads(r.stdout)[0]["status"]), (12, "gone"))
        s, _, w = cases["timeout"]
        self.start_turn(w, "p1")
        r = self.cli("wait", s, "--timeout", "0.5")
        self.assertEqual(r.returncode, 75)
        self.assertIn("still going", r.stdout)
        s, _, w = cases["unstarted"]
        r = self.cli("wait", s, "--json", AGENT_SPAWN_START_GRACE="1")
        self.assertEqual(r.returncode, 11)
        self.assertIn("not started", json.loads(r.stdout)[0]["detail"])
        # --any returns on the first; --all reports the worst.
        r = self.cli("wait", cases["timeout"][0], cases["failed"][0], "--any", "--timeout", "5")
        self.assertEqual(r.returncode, 11)
        r = self.cli("wait", cases["needs"][0], cases["failed"][0], "--all", "--timeout", "5")
        self.assertEqual(r.returncode, 11)
        # --all ends at once on a failure, even with another child running.
        t0 = time.time()
        r = self.cli("wait", cases["timeout"][0], cases["failed"][0], "--all", "--timeout", "8")
        self.assertEqual(r.returncode, 11)
        self.assertLess(time.time() - t0, 5)
        # The same id twice is one wait.
        r = self.cli("wait", cases["failed"][0], cases["failed"][0], "--json")
        self.assertEqual(len(json.loads(r.stdout)), 1)

    def test_startup_dialog_is_needs_input(self):
        (self.fake / "dialog").write_text("Do you trust the files in this folder?\n> 1. Yes, proceed\n")
        s, _, _ = self.spawn("--", "x")
        time.sleep(0.5)
        r = self.cli("wait", s, "--json")
        self.assertEqual(r.returncode, 10, r.stderr)
        self.assertIn("startup dialog: Do you trust", json.loads(r.stdout)[0]["detail"])

    def test_quiet_follow_up_and_turn_ids(self):
        s, p, w = self.spawn("--", "first")
        t1 = self.start_turn(w, "t1")
        self.end_turn(w, "t1", kind="loud")
        (self.state / s / "reply.md").write_text("first answer\n")
        self.setw(w, **{"@agent_reply_at": int(time.time())})
        self.assertEqual(self.cli("wait", s).returncode, 0)
        time.sleep(1.1)
        r = self.cli("send", s, "-", "--parent", self.ppane, "--json", input="and then?")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["status"], "sent")
        call = self.mcp.calls[-1]
        self.assertEqual((call["name"], call["args"], call["pane"], call["auth"]),
                         ("message_agent", {"pane": p, "message": "and then?"}, self.ppane, "Bearer test-token"))
        self.assertEqual(len(self.meta(s)["turns"]), 2)
        # The message is in, the prompt not yet started: the old finish
        # must not count.
        self.assertEqual(self.cli("wait", s, "--timeout", "0.5").returncode, 75)
        # Started in the same second the old turn's end was stamped: ids say
        # it is not over (A13).
        t2 = self.start_turn(w, "t2")
        self.setw(w, **{"@agent_state": "idle", "@agent_turn_end": "%d t1 loud" % t2})
        self.assertEqual(self.cli("wait", s, "--timeout", "0.5").returncode, 75)
        # A quiet finish (prior_restore puts an OLD @agent_since back) ends it.
        bg = subprocess.Popen(["python3", str(SPAWN), "wait", s, "--json", "--timeout", "10"],
                              env=self.env, stdout=subprocess.PIPE, text=True)
        time.sleep(0.4)
        self.assertIsNone(bg.poll())
        (self.state / s / "reply.md").write_text("second answer\n")
        self.setw(w, **{"@agent_reply_at": int(time.time())})
        self.setw(w, **{"@agent_since": "%d done" % (t1 - 50)})
        self.end_turn(w, "t2", kind="quiet", state="done")
        out, _ = bg.communicate(timeout=15)
        self.assertEqual(bg.returncode, 0)
        r = json.loads(out)[0]
        self.assertEqual((r["status"], r["reply"], r["reply_source"]), ("done", "second answer\n", "reply"))
        self.assertEqual(self.opt(w, "@agent_spawn_result"), "read")
        # Turn 1's reply is still there by number.
        r = json.loads(self.cli("result", s, "--turn", "1", "--json").stdout)
        self.assertEqual(r["reply"], "first answer\n")
        # Without ids (`-`), epochs decide.
        s2, _, w2 = self.spawn("--", "x")
        ts = self.start_turn(w2, "-")
        self.setw(w2, **{"@agent_turn_origin": "user - -"})
        self.end_turn(w2, "-", epoch=ts)
        self.assertEqual(self.cli("wait", s2).returncode, 0)

    # --- result ---

    def test_result_sources(self):
        # report.json, written by the child itself (resolves its own window).
        s, p, w = self.spawn("--", "x")
        r = subprocess.run(["python3", str(SPAWN), "report", "--status", "blocked", "need", "the", "key"],
                           env=dict(self.env, TMUX_PANE=p), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.start_turn(w, "p1")
        self.end_turn(w, "p1")
        r = json.loads(self.cli("result", s, "--json").stdout)
        self.assertEqual((r["reply_source"], r["reply"]), ("report", "[blocked] need the key"))
        self.assertEqual(set(r) >= {"id", "harness", "status", "state", "detail", "reply", "reply_source",
                                    "truncated", "session_id", "transcript", "pane", "window", "letter",
                                    "started", "ended", "turns"}, True)
        # A transcript, found through the harness's session record for the pane.
        s, p, w = self.spawn("--", "x")
        pid = self.tm("display-message", "-p", "-t", p, "#{pane_pid}").stdout.strip()
        tr = self.tmp / "conv.jsonl"
        shutil.copy(FIX / "claude.jsonl", tr)
        os.utime(tr, (time.time() - 100, time.time() - 100))     # written before this turn
        (self.res / ("claude-%s.json" % pid)).write_text(json.dumps(
            {"session_id": "sid-1", "transcript_path": str(tr), "env": {"tmux_pane": p}}))
        self.start_turn(w, "p1")
        self.end_turn(w, "p1")
        r = json.loads(self.cli("result", s, "--json", "--max-chars", "10").stdout)
        self.assertEqual((r["reply_source"], r["session_id"], r["transcript"]), ("transcript", "sid-1", str(tr)))
        self.assertEqual(r["reply"], "Final part\n[truncated 32 chars]")
        self.assertTrue(r["truncated"])
        # Written before this turn started: returned, never frozen as its reply.
        self.assertFalse((self.state / s / "reply-1.md").exists())
        # Closed now: the result still reads (the snapshot it collected).
        self.tm("kill-window", "-t", w)
        r = json.loads(self.cli("result", s, "--json").stdout)
        self.assertEqual((r["status"], r["reply"]), ("done", "Final part A.\n\nFinal part B: ünïcode kept."))
        self.assertEqual(r["reply_source"], "transcript")
        # Nothing at all: the screen, flagged as such.
        (self.fake / "screen").write_text("fake screen text\n")
        s, p, w = self.spawn("--", "x")
        time.sleep(0.5)
        self.start_turn(w, "p1")
        self.end_turn(w, "p1")
        r = json.loads(self.cli("result", s, "--json").stdout)
        self.assertEqual(r["reply_source"], "screen")
        self.assertIn("fake screen text", r["reply"])

    def test_result_appends_chezmoi_paths(self):
        # The child's chezmoi-guard records its pending dotfiles at Stop
        # (chezmoi-paths.json) instead of blocking; result tells the parent.
        s, p, w = self.spawn("--", "x")
        ts = self.start_turn(w, "c1")
        self.end_turn(w, "c1")
        (self.state / s / "reply.md").write_text("did it\n")
        self.setw(w, **{"@agent_reply_at": int(time.time())})
        rec = self.state / s / "chezmoi-paths.json"
        src = self.home / ".local/share/chezmoi"
        paths = [str(src / "dot_zshrc"), str(self.home / "notes.txt")]
        rec.write_text(json.dumps({"paths": paths, "exact": paths[:1], "at": ts}))
        r = json.loads(self.cli("result", s, "--json").stdout)
        self.assertEqual(r["reply"], "did it\n\nchezmoi paths touched (uncommitted): dot_zshrc, ~/notes.txt")
        self.assertEqual(r["chezmoi_paths"], ["dot_zshrc", "~/notes.txt"])
        out = self.cli("result", s).stdout
        self.assertIn("chezmoi paths touched (uncommitted): dot_zshrc, ~/notes.txt", out)
        # A record from before this turn started is not this turn's.
        rec.write_text(json.dumps({"paths": paths, "exact": [], "at": ts - 100}))
        r = json.loads(self.cli("result", s, "--json").stdout)
        self.assertEqual((r["reply"], r["chezmoi_paths"]), ("did it\n", []))

    def test_run_end_to_end_with_the_fake_harness(self):
        (self.fake / "auto").write_text("")
        r = self.cli("run", "--json", "--timeout", "20", "--", "do it")
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)
        self.assertEqual((res["status"], res["reply_source"]), ("done", "reply"))
        self.assertEqual(res["reply"], "Fake reply for %s\n" % res["id"])
        self.assertEqual(self.opt(res["window"], "@agent_spawn_result"), "read")
        self.assertEqual(self.opt(res["window"], "@agent_spawn_mode"), "sync")
        self.assertFalse(self.meta(res["id"])["notify"])

    # --- delivery ---

    def test_deliver_batches_per_parent_and_close_notices(self):
        kids = [self.spawn("--task", "job %d" % i, "--", "x") for i in range(2)]
        for s, p, w in kids:
            self.start_turn(w, "p1")
            self.end_turn(w, "p1")
        procs = [subprocess.Popen(["python3", str(SPAWN), "_deliver", s], env=dict(self.env, AGENT_SPAWN_BATCH_S="1"))
                 for s, _, _ in kids]
        for pr in procs:
            self.assertEqual(pr.wait(timeout=20), 0)
        self.assertEqual(len(self.mcp.calls), 1)
        c = self.mcp.calls[0]
        self.assertEqual((c["name"], c["args"]["pane"], c["auth"]), ("message_agent", self.ppane, "Bearer test-token"))
        self.assertIn(c["pane"], {kids[0][1], kids[1][1]})            # the child's pane (A10)
        for s, _, _ in kids:
            self.assertIn("[agent-spawn] %s (claude child " % s, c["args"]["message"])
            self.assertIn("finished (done). Run: agent-spawn result %s" % s, c["args"]["message"])
        # The same finish delivered twice (a Stop and the watcher's restore): once.
        self.cli("_deliver", kids[1][0])
        self.assertEqual(len(self.mcp.calls), 1)
        # Collected already: no wake.
        self.cli("result", kids[0][0])
        self.cli("_deliver", kids[0][0])
        self.assertEqual(len(self.mcp.calls), 1)
        # Closed: told when busy or unread, not when read, never with the parent.
        self.assertEqual(self.cli("_notify-closed", kids[0][0]).returncode, 0)
        self.assertEqual(len(self.mcp.calls), 1)
        self.tm("kill-window", "-t", kids[1][2])
        self.cli("_notify-closed", kids[1][0])
        self.assertEqual(len(self.mcp.calls), 2)
        self.assertIsNone(self.mcp.calls[-1]["pane"])          # no live child to attribute to
        self.assertIn("%s (claude child %s) 'job 1' closed" % (kids[1][0], kids[1][1]),
                      self.mcp.calls[-1]["args"]["message"])
        s, _, w = self.spawn("--", "busy one")
        self.start_turn(w, "p1")
        self.cli("_notify-closed", s, "--parent-closing")
        self.assertEqual(len(self.mcp.calls), 2)
        self.cli("_notify-closed", s)
        self.assertIn("while still working", self.mcp.calls[-1]["args"]["message"])

    def test_owner_list_whoami_cancel_close(self):
        s, p, w = self.spawn("--", "mine")
        op, ow = self.agent_window()
        os_, _, _ = self.spawn("--", "theirs", TMUX_PANE=op)
        for verb in (["send", s, "hi"], ["wait", s, "--timeout", "1"], ["result", s]):
            r = self.cli(*verb, TMUX_PANE=op)
            self.assertEqual(r.returncode, 13, verb)
            self.assertIn("not your child", r.stderr)
        mine = json.loads(self.cli("list", "--json").stdout)
        self.assertEqual([x["id"] for x in mine], [s])
        both = json.loads(self.cli("list", "--all", "--json").stdout)
        self.assertEqual({x["id"] for x in both}, {s, os_})
        who = json.loads(self.cli("whoami", "--json", TMUX_PANE=p).stdout)
        self.assertEqual((who["spawn_id"], who["parent"]["window"]), (s, self.pwin))
        self.assertIn("you are subagent %s of @a" % s, self.cli("whoami", TMUX_PANE=p).stdout)
        # Cancel: marked before the interrupt, so its turn end wakes nobody
        # and lights no ✓.
        self.start_turn(w, "p1")
        self.assertEqual(self.cli("cancel", s).returncode, 0)
        self.assertEqual(self.opt(w, "@agent_spawn_result"), "read")
        self.end_turn(w, "p1")
        self.assertEqual(self.cli("_deliver", s).returncode, 0)
        self.assertEqual(self.mcp.calls, [])
        self.assertEqual(self.cli("wait", s).returncode, 12)
        self.assertEqual(self.cli("close", s).returncode, 0)
        self.assertIn("close %s" % p, (self.fake / "closed.log").read_text())
        self.assertEqual(self.cli("close", s).returncode, 12)
        # A child with children of its own closes as a family.
        s3, p3, w3 = self.spawn("--", "has kids")
        self.setw(w3, **{"@agent_kids_sub": "1 0"})
        self.assertEqual(self.cli("close", s3).returncode, 0)
        self.assertIn("close-family %s" % p3, (self.fake / "closed.log").read_text())

    def test_reopened_parent_keeps_its_children_by_session(self):
        s, p, w = self.spawn("--", "x")
        m = self.meta(s)
        m["parent"]["session_id"] = "sid-parent"
        (self.state / s / "meta.json").write_text(json.dumps(m))
        # ⌘Z brought the parent back as a new window, same session.
        np_, nw = self.agent_window()
        pid = self.tm("display-message", "-p", "-t", np_, "#{pane_pid}").stdout.strip()
        (self.res / ("claude-%s.json" % pid)).write_text(json.dumps(
            {"session_id": "sid-parent", "env": {"tmux_pane": np_}}))
        r = self.cli("result", s, "--json", TMUX_PANE=np_)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.meta(s)["parent"]["window"], self.meta(s)["parent"]["pane"]), (nw, np_))
        # Another session in that new window is still refused.
        (self.res / ("claude-%s.json" % pid)).write_text(json.dumps(
            {"session_id": "sid-other", "env": {"tmux_pane": np_}}))
        m = self.meta(s)
        m["parent"]["window"] = self.pwin
        (self.state / s / "meta.json").write_text(json.dumps(m))
        self.assertEqual(self.cli("result", s, TMUX_PANE=np_).returncode, 13)

    def test_follow_up_without_send_is_its_own_turn(self):
        s, p, w = self.spawn("--", "first")
        self.start_turn(w, "t1")
        (self.state / s / "reply.md").write_text("one\n")
        self.setw(w, **{"@agent_reply_at": int(time.time())})      # U1 writes both with the end
        self.end_turn(w, "t1")
        self.assertEqual(json.loads(self.cli("wait", s, "--json").stdout)[0]["reply"], "one\n")
        # Already collected, nothing new: the old result after the grace.
        r = self.cli("wait", s, "--json", AGENT_SPAWN_FOLLOWUP_GRACE="0.5")
        self.assertEqual((r.returncode, json.loads(r.stdout)[0]["reply"]), (0, "one\n"))
        # A message_agent follow-up (no `send`): wait gives it the grace to start.
        bg = subprocess.Popen(["python3", str(SPAWN), "wait", s, "--json", "--timeout", "15"], env=dict(
            self.env, AGENT_SPAWN_FOLLOWUP_GRACE="10"), stdout=subprocess.PIPE, text=True)
        time.sleep(1.2)
        self.start_turn(w, "t2")
        time.sleep(0.3)
        (self.state / s / "reply.md").write_text("two\n")
        self.setw(w, **{"@agent_reply_at": int(time.time())})
        self.end_turn(w, "t2", kind="quiet")
        out, _ = bg.communicate(timeout=20)
        r = json.loads(out)[0]
        self.assertEqual((bg.returncode, r["reply"], r["reply_turn"]), (0, "two\n", 2))
        self.assertTrue(self.meta(s)["turns"][1]["implicit"])
        self.assertEqual(json.loads(self.cli("result", s, "--turn", "1", "--json").stdout)["reply"], "one\n")

    def test_outbox_retries_and_keeps_until_delivered(self):
        s, p, w = self.spawn("--", "x")
        self.start_turn(w, "p1")
        self.end_turn(w, "p1")
        self.mcp.replies[:] = [{"text": "pane %s has no agent session" % self.ppane, "isError": True}]
        self.assertEqual(self.cli("_deliver", s, AGENT_SPAWN_RETRY_S="0.1").returncode, 0)
        self.assertEqual(len(self.mcp.calls), 2)                # refused (isError), then delivered
        self.assertEqual(list((self.state / "_outbox").glob("*/*.json")), [])
        s2, _, w2 = self.spawn("--", "y")
        self.start_turn(w2, "p1")
        self.end_turn(w2, "p1")
        self.mcp.replies[:] = [{"text": "error: nope", "isError": True}] * 3
        self.cli("_deliver", s2, AGENT_SPAWN_RETRY_S="0.1")
        self.assertEqual(len(self.mcp.calls), 5)                # three tries, then dropped
        self.assertIn("dropped after 3 tries", (self.state / s2 / "events.log").read_text())
        self.assertEqual(list((self.state / "_outbox").glob("*/*.json")), [])
        # Success text is never read as a refusal, whatever the handle says.
        s3, _, w3 = self.spawn("--", "z")
        self.start_turn(w3, "p1")
        self.end_turn(w3, "p1")
        self.mcp.replies[:] = ["sent to @stash-refusal-toast (could not wait: cannot)"]
        self.cli("_deliver", s3, AGENT_SPAWN_RETRY_S="0.1")
        self.assertEqual(len(self.mcp.calls), 6)                # once
        self.assertEqual(list((self.state / "_outbox").glob("*/*.json")), [])

    def test_birth_failure_takes_the_window_back(self):
        fake = self.tmp / "tmux-fails-birth"
        write_exe(fake, '#!/bin/sh\ncase "$*" in *@agent_spawn_mode*) echo "no birth" >&2; exit 1 ;; esac\n'
                        'exec "%s" "$@"\n' % REAL_TMUX)
        before = self.tm("list-windows", "-a", "-F", "#{window_id}").stdout.split()
        r = self.cli("spawn", "--", "x", LAUNCH_TMUX_BIN=str(fake))
        self.assertEqual(r.returncode, 1)
        self.assertIn("could not mark", r.stderr)
        self.assertEqual(self.tm("list-windows", "-a", "-F", "#{window_id}").stdout.split(), before)

    # --- the abby-delegate shim ---

    def test_abby_delegate_shim(self):
        (self.fake / "auto").write_text("")
        r = subprocess.run(["bash", str(SHIM), "spawn", "--", "do it"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        first, second = r.stdout.splitlines()
        pane, win, letter = first.split()
        self.assertEqual((pane[0], win[0], letter), ("%", "@", "-"))
        sid = second.split("id: ", 1)[1]
        self.assertEqual(self.opt(win, "@agent_spawn_id"), sid)
        r = subprocess.run(["bash", str(SHIM), "wait", pane, "--timeout", "20"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "state: done\n---\nFake reply for %s\n\n" % sid)
        r = subprocess.run(["bash", str(SHIM), "last", pane], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "Fake reply for %s" % sid)
        r = subprocess.run(["bash", str(SHIM), "list"], env=self.env, capture_output=True, text=True)
        self.assertIn(sid, r.stdout)
        r = subprocess.run(["bash", str(SHIM), "wait", self.ppane], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not an agent-spawn child", r.stderr)


if __name__ == "__main__":
    unittest.main()
