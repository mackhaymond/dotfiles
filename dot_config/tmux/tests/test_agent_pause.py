"""Pause key (prefix P): scripts/executable_agent-pause.py.

Real tmux, never the live server: each test starts its own server under a
private TMUX_TMPDIR (TMUX and TMUX_PANE removed from the environment, and
`-f /dev/null`), so the script's bare `tmux` calls reach only that server.
Teardown kills it by its explicit socket path and removes the directory.

Agents are faked: each agent window runs `sleep`, and a claude-<pid>.json
record for that sleep's pid goes into a private tmux-assistant-resurrect dir
(TMUX_ASSISTANT_RESURRECT_DIR), which is exactly how the script (and tmux-mcp)
recognise an agent pane. tmux-mcp is a stub HTTP server in a thread that
speaks just enough Streamable-HTTP MCP and records every tools/call; the real
port is never touched. HOME and AGENT_PAUSE_STATE_DIR are private too.
Run with unittest or pytest.
"""
import fcntl
import http.server
import importlib.util
import json
import os
from pathlib import Path
import pty
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "executable_agent-pause.py"
CONF = ROOT / "agent-pause.conf"
TMUX = shutil.which("tmux")
TOKEN = "test-token-not-real"


def load_module():
    spec = importlib.util.spec_from_file_location("agent_pause", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AP = load_module()


class StubMcp:
    """Records calls; per-pane replies: sent (default) | held | refuse | error,
    and the post-send failures slow | drop | garbage."""

    def __init__(self, sse=True):
        self.calls, self.methods, self.deleted = [], [], 0
        self.reply = {}
        self.slow = 2.5
        self.fail_initialized = False
        self.sse = sse
        self.lock = threading.Lock()
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, obj=None, sid=None):
                body = b""
                if obj is not None:
                    raw = json.dumps(obj)
                    body = ("event: message\ndata: %s\n\n" % raw).encode() if stub.sse else raw.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/event-stream" if stub.sse else "application/json")
                if sid:
                    self.send_header("mcp-session-id", sid)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_DELETE(self):
                with stub.lock:
                    stub.deleted += 1
                self.send(200)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n))
                if self.headers.get("Authorization") != "Bearer " + TOKEN:
                    self.send_response(401)
                    self.end_headers()
                    return
                m = req.get("method")
                with stub.lock:
                    stub.methods.append(m)
                if m == "initialize":
                    self.send(200, {"jsonrpc": "2.0", "id": req["id"], "result": {
                        "protocolVersion": req["params"]["protocolVersion"],
                        "capabilities": {"tools": {}}, "serverInfo": {"name": "stub", "version": "0"}}},
                        sid="stub-session")
                    return
                if m == "notifications/initialized":
                    self.send_response(500 if stub.fail_initialized else 202)
                    self.end_headers()
                    return
                assert self.headers.get("mcp-session-id") == "stub-session"
                args = req["params"]["arguments"]
                with stub.lock:
                    stub.calls.append(args)
                how = stub.reply.get(args["pane"], "sent")
                p = args["pane"]
                # Failures AFTER the request arrived (tmux-mcp would deliver):
                if how in ("slow", "slowrefuse"):   # a slow reply (sent / refused)
                    time.sleep(stub.slow)
                    how = "sent" if how == "slow" else "refuse"
                elif how == "drop":          # hangs up: RemoteDisconnected
                    self.close_connection = True
                    return
                elif how == "garbage":       # a malformed status line: BadStatusLine
                    self.close_connection = True
                    self.wfile.write(b"HTTP/1.1 abc\r\n\r\n")
                    return
                text = {"sent": "sent to @x (s:1.0, pane %s)" % p,
                        "held": "held for @x (s:1.0, pane %s): its prompt box has a half-written draft in it." % p,
                        "refuse": "refused: x has no agent session"}.get(how, "no pane " + p)
                res = {"content": [{"type": "text", "text": text}]}
                if how == "error":
                    res["isError"] = True
                try:
                    self.send(200, {"jsonrpc": "2.0", "id": req["id"], "result": res})
                except OSError:
                    pass   # "slow": the client is gone

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/mcp" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def panes(self, message=None):
        return sorted(c["pane"] for c in self.calls if message is None or c["message"] == message)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Harness(unittest.TestCase):
    """A private tmux server + stub tmux-mcp + private dirs. No tests here."""

    def setUp(self):
        base = Path("/private/tmp/claude-%d" % os.getuid())
        base.mkdir(parents=True, exist_ok=True)
        self.dir = Path(tempfile.mkdtemp(prefix="ap-", dir=str(base)))   # short: socket path limit
        self.addCleanup(shutil.rmtree, self.dir, True)
        for d in ("sock", "home", "state", "rec"):
            (self.dir / d).mkdir()
        (self.dir / "token").write_text(TOKEN + "\n")
        self.stub = StubMcp()
        self.addCleanup(self.stub.stop)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("TMUX", "TMUX_PANE", "XDG_RUNTIME_DIR", "TMUX_MCP_PORT")}
        self.env.update({
            "TMUX_TMPDIR": str(self.dir / "sock"),
            "HOME": str(self.dir / "home"),
            "AGENT_PAUSE_STATE_DIR": str(self.dir / "state"),
            "TMUX_MCP_URL": self.stub.url,
            "TMUX_MCP_TOKEN_FILE": str(self.dir / "token"),
            "TMUX_ASSISTANT_RESURRECT_DIR": str(self.dir / "rec"),
            "AGENT_PAUSE_WEZTERM": "off",      # never the real wezterm CLI
            "TERM": "xterm-256color",
        })
        self.sock = None
        self.addCleanup(self.kill_server)
        self.t("-f", "/dev/null", "new-session", "-d", "-s", "main", "-x", "120", "-y", "30",
               "/bin/sleep 600")
        self.sock = self.t("display", "-p", "#{socket_path}")
        self.assertTrue(self.sock.startswith(str(self.dir)), self.sock)   # never the live server
        self.t("set", "-g", "exit-empty", "off")
        self.n = 0

    def kill_server(self):
        if self.sock:   # explicit socket only, never a bare kill-server
            subprocess.run([TMUX, "-S", self.sock, "kill-server"], env=self.env,
                           capture_output=True, timeout=10)

    # -- harness ---------------------------------------------------------
    def t(self, *args):
        r = subprocess.run([TMUX, *args], env=self.env, capture_output=True, text=True, timeout=10)
        if r.returncode:
            raise AssertionError("tmux %s: %s" % (args, r.stderr))
        return r.stdout.strip()

    def run_script(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def press(self):
        r = self.run_script("toggle", "")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")   # the keybind path never prints
        return self.last_log()

    def last_log(self):
        lines = (self.dir / "state" / "log").read_text().splitlines()
        return json.loads(lines[-1])

    def record(self, pane, sid, kind="claude", pid=None):
        pid = pid or int(self.t("display", "-p", "-t", pane, "#{pane_pid}"))
        (self.dir / "rec" / ("%s-%d.json" % (kind, pid))).write_text(json.dumps({
            "session_id": sid, "cwd": "/x", "timestamp": "2026-10-15T10:00:%02dZ" % self.n,
            "env": {"tmux_pane": pane}}))

    def window(self, state=None, workflow="", cua="", agent=True, summary=None):
        """A new window -> its pane id; agent=True also writes a session record."""
        self.n += 1
        pane = self.t("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "main:", "/bin/sleep 600")
        for opt, val in (("@agent_state", state), ("@agent_workflow", workflow), ("@agent_cua", cua),
                         ("@agent_summary", summary or ("proj/agent %d" % self.n))):
            if val:
                self.t("set", "-w", "-t", pane, opt, val)
        if agent:
            self.record(pane, "sid-%d" % self.n)
        return pane

    def state(self):
        p = self.dir / "state" / "paused.json"
        return json.loads(p.read_text()) if p.exists() else None

    def paused_opt(self):
        return self.t("display", "-p", "#{@agents_paused}")

    def age_misses(self, seconds):
        """The clock, injected: move every saved first miss `seconds` back."""
        p = self.dir / "state" / "paused.json"
        st = json.loads(p.read_text())
        for t in st["targets"]:
            if t.get("first_miss_at"):
                t["first_miss_at"] -= seconds
        p.write_text(json.dumps(st))


@unittest.skipUnless(TMUX, "tmux not installed")
class PauseKey(Harness):
    # -- pause selection ---------------------------------------------------
    def test_only_working_agents_are_paused(self):
        run = self.window("running")
        wf = self.window("idle", workflow="1")          # main agent idle, its workflow out
        cua = self.window("done", cua="Safari")
        for s in ("idle", "done", "needs-input", "failed"):
            self.window(s)
        self.window(None)                                # agent with no state yet
        self.window("running", agent=False)              # working, but no agent pane
        self.window(None, agent=False)                   # a plain shell window
        entry = self.press()
        self.assertEqual(self.stub.panes(AP.PAUSE_MSG), sorted([run, wf, cua]))
        self.assertTrue(all(c["raw"] is True for c in self.stub.calls))
        st = self.state()
        self.assertEqual(sorted(t["pane_id"] for t in st["targets"]), sorted([run, wf, cua]))
        why = {t["pane_id"]: t["why"] for t in st["targets"]}
        self.assertEqual((why[run], why[wf], why[cua]), ("running", "workflow", "cua"))
        self.assertTrue(all(t["session_id"].startswith("sid-") for t in st["targets"]))
        self.assertEqual(self.paused_opt(), "3")
        self.assertIn("paused 3 agents", entry["message"])
        self.assertIn("1 working window had no agent pane", entry["message"])
        self.assertEqual(self.stub.deleted, 1)          # MCP session closed

    def test_zero_working_writes_nothing_and_contacts_nobody(self):
        self.window("idle")
        self.window("done")
        self.window("needs-input")
        entry = self.press()
        self.assertIn("no agents are working", entry["message"])
        self.assertIsNone(self.state())
        self.assertEqual(self.stub.methods, [])
        self.assertEqual(self.paused_opt(), "")

    def test_agent_pane_is_the_recorded_one_not_the_active_one(self):
        pane = self.window("running")
        other = self.t("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", pane, "/bin/sleep 600")
        self.t("select-pane", "-t", other)
        self.press()
        self.assertEqual(self.stub.panes(), [pane])

    # -- opencode -----------------------------------------------------------
    def fake_opencode(self, *args):
        """A pane whose process is argv `opencode - <args>` (exec -a) and
        sleeps: python reading its program from stdin (`-`, which the argv
        rule skips as an option), so any args can follow."""
        prog = self.dir / "sleep.py"
        prog.write_text("import time\ntime.sleep(600)\n")
        cmd = "exec -a opencode %s - %s < %s" % (shlex.quote(sys.executable),
                                                " ".join(shlex.quote(a) for a in args),
                                                shlex.quote(str(prog)))
        return self.t("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "main:",
                      "/bin/bash -c %s" % shlex.quote(cmd))

    def wait_args(self, pane, needle):
        pid = int(self.t("display", "-p", "-t", pane, "#{pane_pid}"))
        deadline = time.time() + 5
        while needle not in AP.proc_args(pid) and time.time() < deadline:
            time.sleep(0.05)
        self.assertIn(needle, AP.proc_args(pid))
        return pid

    def test_opencode_agent_is_paused_as_opencode(self):
        self.n += 1
        oc = self.fake_opencode("--port", "0", "-c")
        pid = self.wait_args(oc, "opencode")
        self.t("set", "-w", "-t", oc, "@agent_state", "running")
        self.t("set", "-w", "-t", oc, "@agent_kind", "opencode")
        self.record(oc, "ses_abc", kind="opencode", pid=pid)
        self.press()
        self.assertEqual(self.stub.panes(AP.PAUSE_MSG), [oc])
        (t,) = self.state()["targets"]
        self.assertEqual((t["kind"], t["session_id"], t["found_by"]), ("opencode", "ses_abc", "record"))

    def test_opencode_serve_and_attach_records_are_not_agents(self):
        panes = []
        for args in (("serve", "--port", "4096"), ("attach", "http://127.0.0.1:4096")):
            self.n += 1
            p = self.fake_opencode(*args)
            pid = self.wait_args(p, args[0])
            self.t("set", "-w", "-t", p, "@agent_state", "running")   # a leftover state
            self.record(p, "ses_%d" % self.n, kind="opencode", pid=pid)
            panes.append(p)
        entry = self.press()
        self.assertEqual(self.stub.calls, [])
        self.assertIsNone(self.state())
        self.assertIn("2 working windows had no agent pane", entry["message"])

    def test_opencode_command_fallback_needs_the_opencode_kind(self):
        # No record: an `opencode` pane is the agent only in a window marked
        # @agent_kind opencode (the watcher's TUI-only mark), and the kind is
        # opencode, never the old "claude" guess.
        b = self.dir / "bin"; b.mkdir()
        shutil.copy("/bin/sleep", b / "opencode")
        def win(kind):
            p = self.t("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "main:", "%s 600" % (b / "opencode"))
            self.t("set", "-w", "-t", p, "@agent_state", "running")
            if kind:
                self.t("set", "-w", "-t", p, "@agent_kind", kind)
            return p
        marked, unmarked = win("opencode"), win("")
        deadline = time.time() + 5
        while time.time() < deadline and self.t("display", "-p", "-t", marked, "#{pane_current_command}") != "opencode":
            time.sleep(0.05)
        self.assertEqual(self.t("display", "-p", "-t", unmarked, "#{pane_current_command}"), "opencode")
        entry = self.press()
        self.assertEqual(self.stub.panes(AP.PAUSE_MSG), [marked])
        (t,) = self.state()["targets"]
        self.assertEqual((t["kind"], t["found_by"]), ("opencode", "command"))
        self.assertIn("1 working window had no agent pane", entry["message"])

    # -- round trip --------------------------------------------------------
    def test_resume_goes_to_the_saved_set_not_a_fresh_scan(self):
        a = self.window("running")
        b = self.window("idle", workflow="1")
        idle = self.window("idle")
        self.press()
        # While the lid is shut: a finishes, the idle one starts working.
        self.t("set", "-w", "-t", a, "@agent_state", "done")
        self.t("set", "-w", "-t", idle, "@agent_state", "running")
        entry = self.press()
        self.assertEqual(self.stub.panes(AP.RESUME_MSG), sorted([a, b]))
        self.assertNotIn(idle, self.stub.panes())
        self.assertIsNone(self.state())
        last = json.loads((self.dir / "state" / "paused.last.json").read_text())
        self.assertEqual(sorted(t["result"] for t in last["resume"]), ["sent", "sent"])
        self.assertEqual(self.paused_opt(), "")
        self.assertIn("resumed 2 agents", entry["message"])
        # A third press is a fresh pause again: b's workflow is still out.
        self.press()
        self.assertEqual(sorted(t["pane_id"] for t in self.state()["targets"]), sorted([b, idle]))

    def test_gone_pane_is_reported_and_skipped(self):
        a = self.window("running")
        b = self.window("running")
        self.press()
        self.t("kill-pane", "-t", a)
        entry = self.press()
        self.assertEqual(self.stub.panes(AP.RESUME_MSG), [b])
        self.assertIn("gone 1", entry["message"])
        gone = [t for t in entry["targets"] if t["result"] == "gone"]
        self.assertEqual([t["pane_id"] for t in gone], [a])
        # A miss is kept (records lag after a resurrect restore)...
        self.assertIn("1 still paused — press prefix P again to retry", entry["message"])
        self.assertEqual([(t["pane_id"], t["miss_count"]) for t in self.state()["targets"]], [(a, 1)])
        self.assertEqual(self.paused_opt(), "1")
        # ...and a second miss a minute later drops it: the toggle leaves resume mode.
        self.age_misses(61)
        entry = self.press()
        self.assertIn("dropped after 2 misses", entry["message"])
        self.assertNotIn("still paused", entry["message"])
        self.assertIsNone(self.state())
        self.assertEqual(self.paused_opt(), "")
        self.assertEqual(self.stub.panes(AP.RESUME_MSG), [b])   # nothing new sent

    def test_resume_right_after_a_restore_retries_instead_of_pausing(self):
        a = self.window("running")
        self.press()
        recs = {f.name: f.read_text() for f in (self.dir / "rec").iterdir()}
        for f in (self.dir / "rec").iterdir():
            f.unlink()                                     # SessionStart hooks not run yet
        entry = self.press()
        self.assertIn("resume reached no agents", entry["message"])
        self.assertIsNotNone(self.state())
        entry = self.press()                               # an impatient second press
        self.assertNotIn("dropped", entry["message"])      # 2 misses, but not 60 s apart
        self.assertEqual(self.state()["targets"][0]["miss_count"], 2)
        for name, text in recs.items():                    # the hooks catch up
            (self.dir / "rec" / name).write_text(text)
        self.press()
        self.assertEqual(self.stub.panes(AP.RESUME_MSG), [a])   # resumed, not re-paused
        self.assertEqual(self.stub.panes(AP.PAUSE_MSG), [a])
        self.assertIsNone(self.state())

    def test_resume_follows_the_session_to_a_new_pane(self):
        a = self.window("running")
        self.press()
        # A resurrect restore: a's pane is gone, the session lives on elsewhere.
        for f in (self.dir / "rec").iterdir():
            f.unlink()
        self.t("kill-pane", "-t", a)
        new = self.t("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "main:", "/bin/sleep 600")
        self.record(new, "sid-1")
        self.press()
        self.assertEqual(self.stub.panes(AP.RESUME_MSG), [new])

    def test_pane_running_another_session_is_not_resumed(self):
        a = self.window("running")
        self.press()
        for f in (self.dir / "rec").iterdir():
            f.unlink()
        self.record(a, "sid-somebody-else")
        entry = self.press()
        self.assertEqual(self.stub.panes(AP.RESUME_MSG), [])
        self.assertEqual(entry["targets"][0]["detail"], "pane now runs another session")
        self.age_misses(61)
        self.press()                      # the second miss, a minute on, drops it
        self.assertEqual(self.stub.panes(AP.RESUME_MSG), [])
        self.assertIsNone(self.state())

    def test_claude_session_id_comes_from_the_sessions_file(self):
        a = self.window("running")
        pid = int(self.t("display", "-p", "-t", a, "#{pane_pid}"))
        d = self.dir / "home" / ".claude" / "sessions"
        d.mkdir(parents=True)
        (d / ("%d.json" % pid)).write_text(json.dumps({"pid": pid, "sessionId": "real-sid"}))
        self.press()
        self.assertEqual(self.state()["targets"][0]["session_id"], "real-sid")
        # Resume matches on it: the record still says sid-1, the pane runs real-sid.
        self.press()
        self.assertEqual(self.stub.panes(AP.RESUME_MSG), [a])

    def test_always_refused_agent_ages_out(self):
        a = self.window("running")
        self.press()
        self.stub.reply = {a: "refuse"}
        self.press()
        entry = self.press()                               # failed twice, quickly: kept
        self.assertNotIn("dropped", entry["message"])
        self.assertEqual(self.state()["targets"][0]["miss_count"], 2)
        self.assertEqual(self.paused_opt(), "1")
        self.age_misses(61)
        entry = self.press()                               # and again a minute on: dropped
        self.assertIn("1 dropped after 3 misses", entry["message"])
        self.assertIsNone(self.state())
        self.assertEqual(self.paused_opt(), "")
        last = json.loads((self.dir / "state" / "paused.last.json").read_text())
        self.assertTrue(last["resume"][0]["dropped"])
        self.press()                                       # the toggle is free: a fresh pause
        self.assertEqual(self.stub.panes(AP.PAUSE_MSG), [a, a])

    def test_resume_that_reaches_nobody_keeps_the_set(self):
        a = self.window("running")
        b = self.window("running")
        self.press()
        self.stub.reply = {a: "refuse", b: "error"}
        entry = self.press()
        self.assertIn("resume reached no agents", entry["message"])
        self.assertIn("2 still paused — press prefix P again to retry", entry["message"])
        self.assertEqual(sorted(t["pane_id"] for t in self.state()["targets"]), sorted([a, b]))
        self.assertEqual(self.paused_opt(), "2")
        self.stub.reply = {b: "refuse"}
        entry = self.press()                               # retry: a gets it, b still not
        self.assertIn("resumed 1 agent", entry["message"])
        self.assertEqual([t["pane_id"] for t in self.state()["targets"]], [b])
        self.assertEqual(self.paused_opt(), "1")
        self.stub.reply = {}
        self.press()
        self.assertIsNone(self.state())
        self.assertEqual(self.stub.panes(AP.PAUSE_MSG), sorted([a, b]))   # never re-paused
        last = json.loads((self.dir / "state" / "paused.last.json").read_text())
        self.assertEqual(last["resume_attempts"], 3)

    def test_paused_flag_follows_the_saved_set(self):
        """@agent_paused marks exactly the agents a PAUSE reached; a resume
        clears it (and a window still `paused` goes idle), a refused resume
        puts both back."""
        flag = lambda p: self.t("display", "-p", "-t", p, "#{@agent_paused}").strip()
        st = lambda p: self.t("display", "-p", "-t", p, "#{@agent_state}").strip()
        a = self.window("running")
        b = self.window("running")
        self.stub.reply = {b: "refuse"}
        self.press()
        self.assertTrue(flag(a).isdigit(), flag(a))
        self.assertEqual(flag(b), "")                      # refused: never paused
        self.t("set", "-w", "-t", a, "@agent_state", "paused")   # the hook's Stop
        self.stub.reply = {a: "refuse"}
        self.press()                                       # resume refused
        self.assertTrue(flag(a).isdigit())
        self.assertEqual(st(a), "paused")
        self.stub.reply = {}
        self.press()                                       # resume reaches a
        self.assertEqual((flag(a), st(a)), ("", "idle"))
        self.assertIsNone(self.state())
        # A window the RESUME's turn already made running is left running.
        self.t("set", "-w", "-t", b, "@agent_state", "running")
        self.press()
        self.t("set", "-w", "-t", b, "@agent_state", "running")
        self.press()
        self.assertEqual((flag(b), st(b)), ("", "running"))

    def test_gone_agent_leaves_no_flag_behind(self):
        """A paused pane that now runs another session (/clear, a restart)
        is gone for the resume, and its flag must still be cleared; a closed
        pane in the same set must not stop the others' flags from moving."""
        flag = lambda p: self.t("display", "-p", "-t", p, "#{@agent_paused}").strip()
        a = self.window("running")
        b = self.window("running")
        c = self.window("running")
        self.press()
        self.assertTrue(all(flag(p).isdigit() for p in (a, b, c)))
        self.record(a, "sid-new")                          # a's pane: another session now
        self.t("kill-pane", "-t", b)                       # b: closed
        self.t("set", "-w", "-t", a, "@agent_state", "paused")
        self.press()                                       # resume: a, b gone; c reached
        self.assertEqual(flag(a), "")
        self.assertEqual(self.t("display", "-p", "-t", a, "#{@agent_state}").strip(), "idle")
        self.assertEqual(flag(c), "")
        self.assertEqual(self.stub.panes(AP.RESUME_MSG), [c])

    def test_no_reply_after_send_counts_as_reached(self):
        a = self.window("running")
        b = self.window("running")
        self.env["AGENT_PAUSE_CALL_TIMEOUT"] = "1"
        self.stub.reply = {a: "slow"}
        entry = self.press()
        st = self.state()
        self.assertEqual({t["pane_id"]: t["result"] for t in st["targets"]}, {a: "unknown", b: "sent"})
        self.assertIn("1 unconfirmed", entry["message"])
        self.assertNotIn("kickstart", json.dumps(entry))   # a kickstart would drop held messages
        self.stub.reply = {}
        self.press()
        self.assertEqual(self.stub.panes(AP.RESUME_MSG), sorted([a, b]))

    def test_broken_http_mid_delivery_never_loses_the_pause(self):
        a = self.window("running")
        b = self.window("running")
        c = self.window("running")
        self.stub.reply = {a: "drop", b: "garbage"}
        entry = self.press()
        self.assertEqual(entry["mode"], "pause")           # no crash
        st = self.state()
        self.assertEqual({t["pane_id"]: t["result"] for t in st["targets"]},
                         {a: "unknown", b: "unknown", c: "sent"})
        self.assertIn("paused 3 agents", entry["message"])

    def test_initialize_failure_still_closes_the_session(self):
        self.window("running")
        self.stub.fail_initialized = True
        entry = self.press()
        self.assertIn("pause failed, nothing sent", entry["message"])
        self.assertEqual(self.stub.deleted, 1)
        self.assertEqual(self.stub.calls, [])
        self.assertIsNone(self.state())

    # -- delivery outcomes -------------------------------------------------
    def test_partial_failure_saves_only_the_delivered(self):
        a = self.window("running")
        b = self.window("running")
        h = self.window("running")
        self.stub.reply = {b: "refuse", h: "held"}
        entry = self.press()
        st = self.state()
        self.assertEqual(sorted(t["pane_id"] for t in st["targets"]), sorted([a, h]))
        self.assertEqual([t["pane_id"] for t in st["failed"]], [b])
        self.assertIn("FAILED 1", entry["message"])
        self.assertIn("WAITING on a draft in", entry["message"])
        self.assertIn("clear or send it", entry["message"])
        self.assertEqual(self.paused_opt(), "2")

    def test_all_failed_writes_no_state(self):
        a = self.window("running")
        b = self.window("running")
        self.stub.reply = {a: "refuse", b: "error"}
        entry = self.press()
        self.assertIsNone(self.state())
        self.assertIn("pause failed", entry["message"])
        self.assertEqual(self.paused_opt(), "")

    def test_json_responses_work_too(self):
        self.stub.sse = False
        a = self.window("running")
        self.press()
        self.assertEqual([t["pane_id"] for t in self.state()["targets"]], [a])

    def test_unreachable_server_fails_clearly(self):
        self.window("running")
        self.env["TMUX_MCP_URL"] = "http://127.0.0.1:9/mcp"
        entry = self.press()
        self.assertIn("launchctl kickstart -k", entry["message"])
        self.assertIsNone(self.state())

    def test_bad_token_fails_clearly(self):
        self.window("running")
        (self.dir / "token").write_text("wrong\n")
        entry = self.press()
        self.assertIn("rejected the token", entry["message"])
        self.assertNotIn("wrong", entry["message"])
        self.assertIsNone(self.state())

    def test_resume_with_server_down_keeps_the_saved_set(self):
        self.window("running")
        self.press()
        self.env["TMUX_MCP_URL"] = "http://127.0.0.1:9/mcp"
        entry = self.press()
        self.assertIn("still paused", entry["message"])
        self.assertIsNotNone(self.state())
        self.assertEqual(self.paused_opt(), "1")

    # -- lock, status, dry-run --------------------------------------------
    def test_second_press_while_busy_sends_nothing(self):
        self.window("running")
        with open(self.dir / "state" / "lock", "a") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            entry = self.press()
        self.assertEqual(entry["mode"], "busy")
        self.assertEqual(self.stub.methods, [])
        self.assertIsNone(self.state())

    def test_pause_is_urgent_resume_is_not(self):
        """The PAUSE overrides tmux-mcp's draft hold (urgent); the RESUME
        waits for a clear box like any other message."""
        a = self.window("running")
        self.press()
        self.press()
        urgent = {c["message"]: c.get("urgent") for c in self.stub.calls if c["pane"] == a}
        self.assertIs(urgent[AP.PAUSE_MSG], True)
        self.assertIsNone(urgent[AP.RESUME_MSG])

    def test_resume_only_never_pauses(self):
        a = self.window("running")
        r = self.run_script("resume", "")
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertIn("nothing to resume", self.last_log()["message"])
        self.assertEqual(self.stub.methods, [])
        self.assertIsNone(self.state())
        self.press()
        self.assertEqual(self.stub.panes(AP.PAUSE_MSG), [a])
        r = self.run_script("resume", "")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.stub.panes(AP.RESUME_MSG), [a])
        self.assertIsNone(self.state())

    def test_dry_run_and_status_send_nothing(self):
        a = self.window("running")
        self.window("idle")
        r = self.run_script("dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("would pause 1:", r.stdout)
        self.assertIn(a, r.stdout)
        self.assertIn("skipped, not working (1):", r.stdout)
        self.assertEqual(self.run_script("status").stdout.strip(), "not paused")
        self.assertEqual(self.stub.methods, [])
        self.assertFalse((self.dir / "state" / "paused.json").exists())
        self.press()
        r = self.run_script("dry-run")
        self.assertIn("mode: RESUME", r.stdout)
        self.assertIn("-> " + a, r.stdout)
        self.assertIn("paused since", self.run_script("status").stdout)
        self.assertEqual(len(self.stub.calls), 1)       # only the press sent anything


@unittest.skipUnless(TMUX, "tmux not installed")
class Popup(Harness):
    """The real popup, end to end, on the PRIVATE server only: one client
    attached from a pty this process opens (display-popup needs a client),
    keys typed into that pty. The toggle launches the popup and returns once
    delivery is done; the popup lives on in its own session, so "is it still
    up" is a pgrep for its press id (unique per press)."""

    def setUp(self):
        super().setUp()
        self.env["AGENT_PAUSE_POPUP"] = "on"   # opt-in; these tests are about it
        self.out = bytearray()
        self.child = None
        self.addCleanup(self.detach)
        pid, fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
                os.execvpe(TMUX, [TMUX, "attach", "-t", "main"], self.env)
            finally:
                os._exit(127)
        self.child, self.fd = pid, fd

        def drain():
            while True:
                try:
                    b = os.read(fd, 65536)
                except OSError:
                    return
                if not b:
                    return
                self.out.extend(b)
        threading.Thread(target=drain, daemon=True).start()
        self.client = self.wait_for(lambda: self.t("list-clients", "-F", "#{client_name}"), "client attach")
        self.press_proc = None

    def detach(self):
        if self.press_proc and self.press_proc.poll() is None:
            self.press_proc.kill()
            self.press_proc.wait()
        if self.child:
            try:
                os.kill(self.child, signal.SIGTERM)
                os.waitpid(self.child, 0)
            except OSError:
                pass
            try:
                os.close(self.fd)
            except OSError:
                pass

    def wait_for(self, fn, what, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            v = fn()
            if v:
                return v
            time.sleep(0.1)
        log = self.dir / "state" / "log"
        self.fail("timed out waiting for %s\nlog: %s" % (what, log.read_text()[-3000:] if log.exists() else "-"))

    def progress(self):
        p = self.dir / "state" / "progress.json"
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return {}

    def press_with_popup(self):
        self.out.clear()
        before = self.progress().get("press")
        self.press_proc = subprocess.Popen([sys.executable, str(SCRIPT), "toggle", self.client],
                                           env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.press_id = self.wait_for(
            lambda: self.progress().get("press") not in (None, before) and self.progress()["press"],
            "this press's progress.json")

    def popup_alive(self):
        return subprocess.run(["pgrep", "-f", "press " + self.press_id],
                              capture_output=True).returncode == 0

    def popup_up(self, text):
        return text.encode() in bytes(self.out)

    def key(self, b):
        os.write(self.fd, b)

    def press_done(self, timeout=10):
        """The toggle finished and the popup closed."""
        try:
            self.press_proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.fail("the toggle never finished")
        self.wait_for(lambda: not self.popup_alive(), "the popup to close", timeout)

    def test_popup_is_off_unless_enabled(self):
        """Opt-in: with neither the env override nor @agent_pause_popup on, a
        press on a real client opens no popup and still pauses; the tmux
        option alone turns it on."""
        a = self.window("running")
        del self.env["AGENT_PAUSE_POPUP"]
        r = subprocess.run([sys.executable, str(SCRIPT), "toggle", self.client],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        lines = [json.loads(l) for l in (self.dir / "state" / "log").read_text().splitlines()]
        self.assertFalse([l for l in lines if l["mode"] == "popup"])
        self.assertEqual([t["pane_id"] for t in self.state()["targets"]], [a])
        self.t("set", "-g", "@agent_pause_popup", "on")
        self.press_with_popup()                            # the resume, now with its popup
        self.wait_for(lambda: self.popup_up("RESUMED") or not self.popup_alive(), "the RESUMED popup")
        lines = [json.loads(l) for l in (self.dir / "state" / "log").read_text().splitlines()]
        self.assertTrue([l for l in lines if l["mode"] == "popup"])
        self.press_done()

    def test_pause_popup_ignores_stray_keys_and_esc_keeps_the_set(self):
        a = self.window("running")
        self.press_with_popup()
        self.wait_for(lambda: self.popup_up("resume all now"), "the PAUSE popup")
        self.assertEqual(self.stub.panes(AP.PAUSE_MSG), [a])
        self.key(b"x")
        time.sleep(1.5)
        self.assertTrue(self.popup_alive(), "a stray key closed the popup")
        self.key(b"\x1b")
        self.press_done()
        self.assertIsNotNone(self.state())                 # still paused for prefix P
        self.assertEqual(self.stub.panes(AP.RESUME_MSG), [])
        self.assertEqual(self.paused_opt(), "1")

    def test_enter_resumes_from_the_popup(self):
        a = self.window("running")
        self.press_with_popup()
        self.wait_for(lambda: self.popup_up("resume all now"), "the PAUSE popup")
        self.key(b"\r")
        self.wait_for(lambda: self.stub.panes(AP.RESUME_MSG), "the RESUME delivery")
        self.press_done()                                  # RESUMED view closes by itself
        self.assertEqual(self.stub.panes(AP.RESUME_MSG), [a])
        self.assertIsNone(self.state())
        self.assertTrue((self.dir / "state" / "paused.last.json").exists())
        self.assertEqual(self.paused_opt(), "")

    def test_resume_elsewhere_closes_the_open_pause_popup(self):
        a = self.window("running")
        self.press_with_popup()
        self.wait_for(lambda: self.popup_up("resume all now"), "the PAUSE popup")
        r = self.run_script("toggle", "")                  # prefix P from "another client"
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.stub.panes(AP.RESUME_MSG), [a])
        self.press_done()

    def test_prefix_p_resume_popup_closes_by_itself(self):
        self.window("running")
        # Paused with no client, so its fallback message is still on the
        # attached client's status line: a popup opened under a message drew
        # almost nothing on tmux 3.7c until open_popup cleared it.
        self.press()
        self.press_with_popup()                            # prefix P again: resume
        self.wait_for(lambda: self.popup_up("any key to close"), "the RESUMED popup")
        self.press_done()
        self.assertIsNone(self.state())

    def test_enter_that_reaches_nobody_stays_paused(self):
        a = self.window("running")
        self.press_with_popup()
        self.wait_for(lambda: self.popup_up("resume all now"), "the PAUSE popup")
        self.stub.reply = {a: "refuse"}
        self.key(b"\r")
        self.wait_for(lambda: self.popup_up("press prefix P again"), "the retry note")
        time.sleep(1.5)
        self.assertTrue(self.popup_alive(), "the popup claimed a resume and closed")
        self.assertIsNotNone(self.state())
        self.assertEqual(self.paused_opt(), "1")
        self.stub.reply = {}
        self.key(b"\r")                                    # a retry from the popup works
        self.wait_for(lambda: self.stub.panes(AP.RESUME_MSG) == [a, a], "the retried RESUME")
        self.press_done()
        self.assertIsNone(self.state())

    def test_partial_resume_elsewhere_shows_the_partial_result(self):
        a = self.window("running")
        b = self.window("running")
        self.press_with_popup()
        self.wait_for(lambda: self.popup_up("resume all now"), "the PAUSE popup")
        self.stub.reply = {b: "refuse"}
        self.run_script("toggle", "")                      # prefix P from "another client"
        self.wait_for(lambda: self.popup_up("1 still paused"), "the partial RESUMED view")
        self.press_done()
        self.assertEqual([t["pane_id"] for t in self.state()["targets"]], [b])

    def test_enter_that_drops_the_last_agent_is_not_a_green_resume(self):
        a = self.window("running")
        self.press_with_popup()
        self.wait_for(lambda: self.popup_up("resume all now"), "the PAUSE popup")
        for f in (self.dir / "rec").iterdir():
            f.unlink()
        self.t("kill-pane", "-t", a)                       # the agent is really gone
        self.key(b"\r")                                    # miss 1: kept, still paused
        self.wait_for(lambda: self.popup_up("press prefix P again"), "the retry note")
        self.age_misses(61)
        self.out.clear()
        self.key(b"\r")                                    # miss 2 a minute on: dropped
        self.wait_for(lambda: self.popup_up("dropped 1 unreachable agent, nothing resumed"),
                      "the NOT RESUMED view")
        self.press_done()                                  # closes by itself
        screen = bytes(self.out)
        self.assertNotIn(b"told to pick up", screen)       # never the RESUMED view
        self.assertIn(b"no paused agent could be reached", screen)
        self.assertIsNone(self.state())
        self.assertEqual(self.stub.panes(AP.RESUME_MSG), [])

    # -- the popup opens BEFORE delivery ----------------------------------
    def test_popup_is_up_while_delivery_runs(self):
        a = self.window("running")
        self.stub.reply = {a: "slow"}                      # tmux-mcp takes 2.5 s
        t0 = time.time()
        self.press_with_popup()
        self.wait_for(lambda: self.popup_up("sending PAUSE to 1 agent"), "the popup while sending", 2)
        self.assertLess(time.time() - t0, self.stub.slow)  # up before the reply
        self.assertIsNone(self.press_proc.poll())          # still delivering
        self.assertIsNone(self.state())                    # nothing resumable mid-flight
        self.assertEqual(self.run_script("toggle", "").returncode, 0)
        self.assertEqual(self.last_log()["mode"], "busy")  # a 2nd press is never a resume
        self.key(b"\r")                                    # Enter mid-delivery: ignored
        self.wait_for(lambda: self.popup_up("resume all now"), "the PAUSE view")
        time.sleep(0.5)
        self.assertEqual(self.stub.panes(AP.RESUME_MSG), [])
        self.assertEqual([t["pane_id"] for t in self.state()["targets"]], [a])
        self.assertTrue(self.progress()["done"])
        self.key(b"\x1b")
        self.press_done()

    def test_pause_that_reaches_nobody_shows_not_paused(self):
        a = self.window("running")
        self.stub.reply = {a: "refuse"}
        self.press_with_popup()
        self.wait_for(lambda: self.popup_up("nothing is paused"), "the NOT PAUSED view")
        self.assertIsNone(self.state())
        self.assertEqual(self.progress()["outcome"], "failed")
        time.sleep(1.5)
        self.assertTrue(self.popup_alive(), "NOT PAUSED must wait for a key")
        self.key(b"x")
        self.press_done()

    def test_prefix_p_resume_reaching_nobody_shows_not_resumed(self):
        a = self.window("running")
        self.press()                                       # paused, no popup
        self.stub.reply = {a: "refuse"}
        self.press_with_popup()
        self.wait_for(lambda: self.popup_up("no paused agent could be reached"), "the NOT RESUMED view")
        self.press_done()                                  # closes by itself
        self.assertNotIn(b"told to pick up", bytes(self.out))
        self.assertEqual([t["pane_id"] for t in self.state()["targets"]], [a])

    def test_esc_while_sending_still_reports_the_outcome(self):
        a = self.window("running")
        self.stub.reply = {a: "slowrefuse"}                # slow, then refused
        self.press_with_popup()
        self.wait_for(lambda: self.popup_up("sending PAUSE"), "the popup while sending", 2)
        self.key(b"\x1b")                                  # Esc still works mid-delivery
        self.wait_for(lambda: not self.popup_alive(), "the popup to close")
        self.out.clear()
        self.press_done()                                  # the toggle finishes delivering...
        self.assertIsNone(self.state())
        # ...and, the popup being gone, says how it went on the status line.
        self.wait_for(lambda: self.popup_up("pause failed"), "the outcome line")

    def test_error_in_delivery_finish_ends_the_sending_view(self):
        self.window("running")
        (self.dir / "state" / "paused.json").mkdir()       # write_json(paused.json) will fail
        self.press_with_popup()
        self.wait_for(lambda: self.popup_up("nothing is paused"), "the NOT PAUSED view")
        self.wait_for(lambda: self.popup_up("pause key error"), "the error in the popup")
        prog = self.progress()
        self.assertTrue(prog["done"])
        self.assertEqual(prog["outcome"], "failed")
        self.press_proc.wait(10)                           # main's handler: log + line
        modes = [json.loads(l)["mode"] for l in (self.dir / "state" / "log").read_text().splitlines()]
        self.assertIn("error", modes)
        self.key(b"x")
        self.wait_for(lambda: not self.popup_alive(), "the popup to close")

    def test_popup_stops_sending_when_the_toggle_died(self):
        dead = subprocess.Popen(["/usr/bin/true"])
        dead.wait()                                        # reaped: its pid is free
        self.press_id = "1-%d" % dead.pid
        (self.dir / "state" / "progress.json").write_text(json.dumps({
            "press": self.press_id, "mode": "pause", "done": False, "outcome": "", "message": "",
            "targets": [{"label": "proj/a", "location": "main:1.0", "pane_id": "%1",
                         "result": "sending"}]}))
        cmd = " ".join(shlex.quote(c) for c in
                       [sys.executable, str(SCRIPT), "watch", "pause", "--press", self.press_id])
        popup = subprocess.Popen([TMUX, "display-popup", "-c", self.client, "-E", "-w", "100",
                                  "-h", "24", cmd], env=self.env)
        self.addCleanup(lambda: popup.poll() is None and popup.kill())
        self.wait_for(lambda: self.popup_up("the pause key crashed"), "the crash note")
        self.assertIn(b"nothing is paused", bytes(self.out))   # NOT PAUSED, not a spinner
        self.key(b"x")
        popup.wait(10)

    def test_popup_centres_on_the_wezterm_tab(self):
        self.window("running")
        tty = self.t("list-clients", "-F", "#{client_tty}")
        wez = self.dir / "wez.json"
        wez.write_text(json.dumps([
            {"window_id": 0, "tab_id": 0, "pane_id": 0, "tty_name": tty, "left_col": 0, "top_row": 0,
             "size": {"cols": 140, "rows": 40}},
            {"window_id": 0, "tab_id": 0, "pane_id": 1, "tty_name": "/dev/ttys999", "left_col": 141,
             "top_row": 0, "size": {"cols": 59, "rows": 40}}]))
        self.env["AGENT_PAUSE_WEZTERM"] = str(wez)
        self.press_with_popup()
        self.wait_for(lambda: self.popup_up("resume all now"), "the PAUSE popup")
        geo = [json.loads(l) for l in (self.dir / "state" / "log").read_text().splitlines()
               if '"popup"' in l][-1]["geometry"]
        self.assertEqual(geo, [80, 24, 60, "C"])           # tab centre x=100: flush right
        self.assertRegex(bytes(self.out), rb"\x1b\[\d+;61H")  # drawn from column 61 (1-based)
        self.key(b"\x1b")
        self.press_done()


class Units(unittest.TestCase):
    def test_classify(self):
        ok = lambda t, err=False: {"result": {"content": [{"type": "text", "text": t}], "isError": err}}
        self.assertEqual(AP.classify(ok("sent to @a (m:1.0, pane %1)"))[0], "sent")
        self.assertEqual(AP.classify(ok("held for @a (m:1.0, pane %1): draft.\nmore"))[0], "held")
        self.assertEqual(AP.classify(ok("refused: a has no agent session"))[0], "failed")
        self.assertEqual(AP.classify(ok("no pane %9", True))[0], "failed")
        self.assertEqual(AP.classify({"error": {"message": "boom"}}), ("failed", "boom"))

    def test_response_parsing(self):
        sse = 'event: message\nid: 1\ndata: {"jsonrpc":"2.0","method":"x"}\n\ndata: {"jsonrpc":"2.0","id":7,"result":{}}\n\n'
        self.assertEqual(AP.Mcp._response(sse, 7)["id"], 7)
        self.assertEqual(AP.Mcp._response('{"jsonrpc":"2.0","id":3,"result":{"a":1}}', 3)["result"], {"a": 1})
        with self.assertRaises(AP.McpUncertain):
            AP.Mcp._response("", 1)

    def test_sse_multiline_data_and_raw_line_separators(self):
        # One event split over two data: lines (joined with \n), CRLF framing,
        # and raw U+2028 / U+0085 inside a JSON string (JSON.stringify leaves
        # both unescaped), which str.splitlines() would cut.
        text = "line sep\x85nel"
        body = ('event: message\r\ndata: {"jsonrpc":"2.0","id":5,\r\n'
                'data: "result":{"content":[{"type":"text","text":"%s"}]}}\r\n\r\n' % text)
        m = AP.Mcp._response(body, 5)
        self.assertEqual(m["result"]["content"][0]["text"], text)

    def test_resume_bookkeeping_helpers(self):
        self.assertEqual(AP.resume_note({"still_paused": 2}), "2 still paused — press prefix P again to retry")
        self.assertEqual(AP.resume_note({}), "")
        self.assertTrue(AP.resume_reached({"resume": [{"result": "gone"}, {"result": "unknown"}]}))
        self.assertFalse(AP.resume_reached({"resume": [{"result": "gone"}, {"result": "failed"}]}))
        self.assertEqual(AP.resume_status({"result": "unknown"}, None), AP.UNCONFIRMED)
        self.assertEqual(AP.dropped_note({"resume": [{"dropped": True}, {"result": "gone"}]}),
                         "dropped 1 unreachable agent, nothing resumed")
        self.assertEqual(AP.on_key("dropped", b"x"), "close")
        bg, lines = AP.build_frame("dropped", [("a", "m:1.0", AP.GONE)], 100, 24, 0, close_in=2,
                                   note=AP.dropped_note({"resume": [{"dropped": True}]}))
        text = "\n".join(lines)
        self.assertEqual(bg, AP.PEACH)                     # never green
        self.assertTrue(all(r in text for r in AP.block("NOT RESUMED")))
        self.assertIn("dropped 1 unreachable agent", text)
        self.assertEqual(lines[-1].strip(), "closes in 2s · any key to close")

    def test_agent_command_fallback(self):
        for c in ("claude", "codex", "2.1.170", "opencode"):
            self.assertTrue(AP.AGENT_CMD.match(c), c)
        for c in ("zsh", "sleep", "node", "2.1", "opencode-x"):
            self.assertFalse(AP.AGENT_CMD.match(c), c)
        self.assertEqual([AP.cmd_kind(c) for c in ("claude", "2.1.170", "codex", "opencode")],
                         ["claude", "claude", "codex", "opencode"])

    def test_opencode_tui_args(self):
        # Same table as the watcher's opencode_tui_args (test_opencode_watcher).
        for a in ("opencode", "/opt/homebrew/bin/opencode", "opencode --port 0 -c",
                  "opencode -s ses_abc", "opencode ~/code/proj", "opencode --mini",
                  "opencode -m openai/gpt-5 --agent build", "opencode pr 12", "opencode -- serve"):
            self.assertTrue(AP.opencode_tui_args(a), a)
        for a in ("opencode serve", "opencode --port 4096 serve", "opencode web", "opencode mcp list",
                  "opencode debug config", "opencode run hi", "opencode attach http://127.0.0.1:4096",
                  "opencode -m a/b run hi", "opencode export ses_x", "opencode --log-level INFO serve"):
            self.assertFalse(AP.opencode_tui_args(a), a)

    # -- the popup: pure frame + key handling ---------------------------------
    def frame(self, mode, items, cols=100, rows=30, elapsed=5, **kw):
        bg, lines = AP.build_frame(mode, items, cols, rows, elapsed, **kw)
        self.assertEqual(len(lines), rows)
        self.assertTrue(all(len(l) == cols for l in lines), [len(l) for l in lines])
        return bg, lines, "\n".join(lines)

    def contains_block(self, text, word):
        return all(r in text for r in AP.block(word))

    def test_pause_frame_while_pausing(self):
        items = [("tmux/a", "main:1.0", AP.PAUSING), ("tmux/b", "main:2.0", AP.STOPPED),
                 ("tmux/c", "main:3.0", AP.HELD)]
        bg, lines, text = self.frame("pause", items, elapsed=65)
        self.assertEqual(bg, AP.PEACH)
        self.assertTrue(self.contains_block(text, "PAUSING"))
        self.assertIn("%s stopping all agents… 1 of 3 stopped · 1:05" % AP.SPINNER[0], text)
        self.assertIn("tmux/a", text)
        self.assertIn("main:3.0", text)
        self.assertIn(AP.SPINNER[0] + " " + AP.HELD, text)
        self.assertIn(AP.SPINNER[0] + " " + AP.PAUSING, text)
        self.assertNotIn(AP.SPINNER[0] + " " + AP.STOPPED, text)
        _, _, text = self.frame("pause", items, elapsed=65, spin=3)
        self.assertIn(AP.SPINNER[3] + " " + AP.PAUSING, text)
        self.assertEqual(lines[-1].strip(), AP.PAUSE_FOOT)
        self.assertNotIn("still working", text)

    def test_pause_frame_flips_to_safe_to_close(self):
        items = [("a", "m:1.0", AP.STOPPED), ("b", "m:2.0", AP.GONE), ("c", "m:3.0", AP.FAILED)]
        bg, _, text = self.frame("pause", items)
        self.assertEqual(bg, AP.GREEN)
        self.assertTrue(self.contains_block(text, "SAFE TO CLOSE"))
        self.assertIn("nothing still working — safe to close the lid", text)
        _, _, text = self.frame("pause", items[:1])
        self.assertIn("all 1 stopped", text)

    def test_still_working_after_three_minutes(self):
        items = [("slowpoke", "m:1.0", AP.PAUSING), ("b", "m:2.0", AP.STOPPED)]
        _, _, text = self.frame("pause", items, elapsed=AP.STILL_AFTER)
        self.assertIn("still working: slowpoke — close anyway or wait", text)

    def test_banner_wraps_then_degrades_on_small_popups(self):
        items = [("a", "m:1.0", AP.STOPPED)]
        _, _, text = self.frame("pause", items, cols=60, rows=20)      # SAFE TO CLOSE is 77 wide
        self.assertTrue(self.contains_block(text, "CLOSE"))
        self.assertFalse(self.contains_block(text, "SAFE TO CLOSE"))
        _, _, text = self.frame("pause", items, cols=20, rows=8)
        self.assertNotIn("█", text)
        self.frame("pause", items, cols=10, rows=3)                     # no crash at the floor

    def test_many_items_are_cut_with_a_count(self):
        items = [("agent %d" % i, "m:%d.0" % i, AP.PAUSING) for i in range(30)]
        _, lines, text = self.frame("pause", items, rows=20)
        self.assertRegex(text, r"\+\d+ more")
        self.assertEqual(lines[-1].strip(), AP.PAUSE_FOOT)

    def test_resume_frame_and_note(self):
        items = [("a", "m:1.0", AP.RESUMED), ("b", "m:2.0", AP.WORKING), ("c", "m:3.0", AP.GONE)]
        bg, lines, text = self.frame("resume", items, close_in=3)
        self.assertEqual(bg, AP.GREEN)
        self.assertTrue(self.contains_block(text, "RESUMED"))
        self.assertIn("2 of 3 agents told", text)
        self.assertEqual(lines[-1].strip(), "closes in 3s · any key to close")
        _, _, text = self.frame("pause", items[:1], note="pause key busy — press Enter again")
        self.assertIn("press Enter again", text)

    def test_row_statuses(self):
        row = lambda st="", wf="", cua="": {"agent_state": st, "agent_workflow": wf, "agent_cua": cua}
        ps = AP.pause_status
        self.assertEqual(ps({"result": "sent"}, row("running")), AP.PAUSING)
        self.assertEqual(ps({"result": "held"}, row("idle", wf="1")), AP.HELD)
        self.assertEqual(ps({"result": "sent"}, row("done", cua="Safari")), AP.PAUSING)
        self.assertEqual(ps({"result": "held"}, row("idle")), AP.STOPPED)
        self.assertEqual(ps({"result": "sent"}, None), AP.GONE)
        self.assertEqual(ps({"result": "failed"}, row("running")), AP.FAILED)
        self.assertEqual(ps({"result": "unidentified"}, None), AP.NOT_SENT)
        rs = AP.resume_status
        self.assertEqual(rs({"result": "sent"}, row("idle")), AP.RESUMED)
        self.assertEqual(rs({"result": "sent"}, row("running")), AP.WORKING)
        self.assertEqual(rs({"result": "held"}, row("idle")), AP.HELD)
        self.assertEqual(rs({"result": "gone"}, None), AP.GONE)
        self.assertEqual(rs({"result": "failed"}, row("idle")), AP.FAILED)

    def test_keys(self):
        k = AP.on_key
        self.assertEqual(k("pause", b"\r"), "resume")
        self.assertEqual(k("pause", b"\n"), "resume")
        self.assertEqual(k("pause", b"\x1b"), "close")
        for stray in (b"x", b" ", b"q", b"\x1b[A", b"\x1b[I", b"\x7f", b""):
            self.assertIsNone(k("pause", stray), stray)
        for any_key in (b"x", b"\r", b"\x1b"):
            self.assertEqual(k("resumed", any_key), "close")
        self.assertIsNone(k("resumed", b""))

    def test_state_gone(self):
        st = {"paused_at": "T1", "targets": [{"pane_id": "%1"}, {"pane_id": "%2"}]}
        base = AP.baseline(st)
        self.assertEqual(base, ("T1", 0))
        self.assertFalse(AP.state_gone(base, st))
        self.assertTrue(AP.state_gone(base, None))
        self.assertTrue(AP.state_gone(base, dict(st, targets=[])))
        self.assertTrue(AP.state_gone(base, dict(st, paused_at="T2")))   # a newer pause
        # A partial resume elsewhere leaves a retry set: same pause, one more attempt.
        partial = dict(st, targets=st["targets"][1:], resume_attempts=1)
        self.assertTrue(AP.state_gone(base, partial))
        self.assertFalse(AP.state_gone(AP.baseline(partial), partial))

    def test_popup_size(self):
        self.assertEqual(AP.popup_size(200, 50), (140, 30))
        self.assertEqual(AP.popup_size(80, 24), (60, 16))
        self.assertEqual(AP.popup_size(50, 12), (50, 12))

    # -- popup_geometry: centre on the WezTerm tab ------------------------------
    @staticmethod
    def pane(tty, left, cols, top=0, rows=74, tab=0, window=0):
        return {"window_id": window, "tab_id": tab, "pane_id": left, "tty_name": tty,
                "left_col": left, "top_row": top, "size": {"cols": cols, "rows": rows}}

    def test_geometry_sidebar_right(self):
        # The live layout: tmux 0..236, divider 237, sidebar 238..271.
        wez = [self.pane("/dev/ttys000", 0, 237), self.pane("/dev/ttys018", 238, 34)]
        w, h, x, y = AP.popup_geometry(237, 74, "/dev/ttys000", wez)
        self.assertEqual((w, x, y), (190, 41, "C"))        # tab centre 136
        self.assertEqual(x + w / 2, 136)
        self.assertEqual(h, AP.popup_size(237, 74)[1])

    def test_geometry_sidebar_left(self):
        wez = [self.pane("/dev/ttys018", 0, 34), self.pane("/dev/ttys000", 35, 237)]
        w, h, x, y = AP.popup_geometry(237, 74, "/dev/ttys000", wez)
        self.assertEqual((w, x, y), (190, 6, "C"))         # 136 - 35 = 101 in client coords
        self.assertEqual(x + w / 2, 101)

    def test_geometry_no_sidebar_or_other_tabs_is_centred(self):
        alone = [self.pane("/dev/ttys000", 0, 237),
                 self.pane("/dev/ttys018", 0, 100, tab=1)]   # another tab does not count
        self.assertEqual(AP.popup_geometry(237, 74, "/dev/ttys000", alone),
                         AP.popup_size(237, 74) + ("C", "C"))

    def test_geometry_too_wide_sidebar_stays_inside(self):
        wez = [self.pane("/dev/ttys000", 0, 100), self.pane("/dev/ttys018", 101, 199)]
        w, h, x, y = AP.popup_geometry(100, 74, "/dev/ttys000", wez)
        self.assertEqual((w, x), (60, 40))                 # minimum width, flush right
        self.assertLessEqual(x + w, 100)

    def test_geometry_client_not_found_or_bad_data_is_centred(self):
        centred = AP.popup_size(237, 74) + ("C", "C")
        wez = [self.pane("/dev/ttys018", 238, 34)]
        self.assertEqual(AP.popup_geometry(237, 74, "/dev/ttys000", wez), centred)
        self.assertEqual(AP.popup_geometry(237, 74, "/dev/ttys000", []), centred)
        broken = [{"tty_name": "/dev/ttys000", "window_id": 0, "tab_id": 0}]
        self.assertEqual(AP.popup_geometry(237, 74, "/dev/ttys000", broken), centred)
        self.assertEqual(AP.popup_geometry(237, 74, "/dev/ttys000", [None, "x"]), centred)

    def test_geometry_stacked_vertically(self):
        wez = [self.pane("/dev/ttys000", 0, 140, top=0, rows=40),
               self.pane("/dev/ttys018", 0, 140, top=41, rows=20)]
        w, h, x, y = AP.popup_geometry(140, 40, "/dev/ttys000", wez)
        self.assertEqual(x, "C")
        self.assertEqual((h, y), (19, 40))                 # top 21, numeric -y = bottom
        self.assertLessEqual(y, 40)

    def test_wezterm_cli_never_auto_starts_a_mux(self):
        from unittest import mock
        seen = []

        def fake_run(argv, **kw):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        env = {k: v for k, v in os.environ.items() if k != "AGENT_PAUSE_WEZTERM"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("shutil.which", return_value="/fake/wezterm"), \
             mock.patch.object(AP.subprocess, "run", side_effect=fake_run):
            self.assertEqual(AP.wezterm_panes(), [])
        self.assertEqual(seen, [["/fake/wezterm", "cli", "--no-auto-start", "list", "--format", "json"]])

    def test_press_alive(self):
        self.assertTrue(AP.press_alive("1-%d" % os.getpid()))
        dead = subprocess.Popen(["/usr/bin/true"])
        dead.wait()
        self.assertFalse(AP.press_alive("1-%d" % dead.pid))
        self.assertTrue(AP.press_alive("garbage"))         # unparseable: assume alive

    def test_wezterm_panes_reads_an_injected_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump([self.pane("/dev/ttys000", 0, 10)], f)
        self.addCleanup(os.unlink, f.name)
        old = os.environ.get("AGENT_PAUSE_WEZTERM")
        try:
            os.environ["AGENT_PAUSE_WEZTERM"] = f.name
            self.assertEqual(AP.wezterm_panes()[0]["tty_name"], "/dev/ttys000")
            os.environ["AGENT_PAUSE_WEZTERM"] = "off"
            self.assertIsNone(AP.wezterm_panes())
        finally:
            if old is None:
                os.environ.pop("AGENT_PAUSE_WEZTERM", None)
            else:
                os.environ["AGENT_PAUSE_WEZTERM"] = old

    # -- sending: progress rows and what follows -------------------------------
    def test_progress_items_and_after_sending(self):
        row = {"agent_state": "running", "agent_workflow": "", "agent_cua": ""}
        prog = {"mode": "pause", "targets": [
            {"label": "a", "location": "m:1.0", "pane_id": "%1", "result": "sending"},
            {"label": "b", "location": "m:2.0", "pane_id": "%2", "result": "sent"},
            {"label": "c", "location": "m:3.0", "pane_id": "%3", "result": "failed"},
            {"label": "d", "location": "m:4.0", "result": "unidentified"}]}
        self.assertEqual([s for _, _, s in AP.progress_items(prog, {"%2": row})],
                         [AP.SENDING, AP.PAUSING, AP.FAILED, AP.NOT_SENT])
        rprog = {"mode": "resume", "targets": [
            {"label": "a", "resume_pane": "%1", "result": "sending"},
            {"label": "b", "resume_pane": None, "result": "gone"}]}
        self.assertEqual([s for _, _, s in AP.progress_items(rprog, {})], [AP.SENDING, AP.GONE])
        self.assertEqual(AP.after_sending({"mode": "pause", "outcome": "paused"}), "pause")
        self.assertEqual(AP.after_sending({"mode": "pause", "outcome": "failed"}), "notpaused")
        self.assertEqual(AP.after_sending({"mode": "resume", "outcome": "resumed"}), "resumed")
        self.assertEqual(AP.after_sending({"mode": "resume", "outcome": "not_resumed"}), "dropped")
        self.assertEqual(AP.after_sending({"mode": "resume", "outcome": "failed"}), "dropped")

    def test_sending_and_not_paused_frames(self):
        items = [("a", "m:1.0", AP.SENDING), ("b", "m:2.0", AP.PAUSING)]
        bg, lines = AP.build_frame("pause", items, 100, 24, 3, sending=True, spin=1)
        text = "\n".join(lines)
        self.assertEqual(bg, AP.PEACH)
        self.assertIn("sending PAUSE to 2 agents… 1 of 2 done", text)
        self.assertIn(AP.SPINNER[1] + " " + AP.SENDING, text)
        self.assertEqual(lines[-1].strip(), AP.SENDING_FOOT)
        bg, lines = AP.build_frame("resume", [("a", "m:1.0", AP.SENDING)], 100, 24, 0, sending=True)
        self.assertEqual(bg, AP.GREEN)
        self.assertIn("sending RESUME to 1 agent… 0 of 1 done", "\n".join(lines))
        self.assertIn(AP.SPINNER[0] + " " + AP.SENDING, "\n".join(lines))
        bg, lines = AP.build_frame("notpaused", [("a", "m:1.0", AP.FAILED)], 100, 24, 0,
                                   note="pause failed: no agent took the message")
        text = "\n".join(lines)
        self.assertEqual(bg, AP.PEACH)
        self.assertTrue(all(r in text for r in AP.block("NOT PAUSED")))
        self.assertIn("nothing is paused", text)
        self.assertIn("no agent took the message", text)
        self.assertEqual(lines[-1].strip(), "any key to close")
        self.assertEqual(AP.on_key("sending", b"\r"), None)    # Enter would race delivery
        self.assertEqual(AP.on_key("sending", b"x"), None)
        self.assertEqual(AP.on_key("sending", b"\x1b"), "close")
        self.assertEqual(AP.on_key("notpaused", b"x"), "close")

    def test_popup_cmd_carries_overrides_not_the_token(self):
        old = dict(os.environ)
        try:
            os.environ["TMUX_MCP_TOKEN_FILE"] = "/x/token"
            os.environ["AGENT_PAUSE_STATE_DIR"] = "/x/state dir"
            cmd = AP.popup_cmd("pause")
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertIn("TMUX_MCP_TOKEN_FILE=/x/token", cmd)
        self.assertIn("'AGENT_PAUSE_STATE_DIR=/x/state dir'", cmd)
        self.assertTrue(cmd.endswith(" watch pause"))

    def test_conf_binding(self):
        text = CONF.read_text()
        binds = [l for l in text.splitlines() if l.startswith("bind")]
        self.assertEqual(len(binds), 1)
        self.assertIn("bind-key P run-shell -b", binds[0])
        self.assertIn("scripts/agent-pause.py toggle '#{client_name}'", binds[0])
        self.assertNotIn("{{", text)
        self.assertTrue(SCRIPT.read_text().startswith("#!/usr/bin/env python3\n"))


if __name__ == "__main__":
    unittest.main()
