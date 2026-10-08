"""Behavioral hook tests: isolated HOME, tmux transport, and title condenser.

No real tmux server, model call, watcher, or GUI is used. Run with unittest.
"""
import hashlib
import json
import os
import signal
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
import uuid

SOURCE = Path(os.environ.get("AGENT_INDICATOR_SOURCE", str(Path.home() / ".local/share/chezmoi/dot_config/tmux/scripts/executable_agent-tab-indicator.sh")))
TRACKER = Path.home() / ".local/bin/codex-session-track"

FAKE_TMUX = r'''#!/usr/bin/env python3
import fcntl,json,os,subprocess,sys
from pathlib import Path
p=Path(os.environ['FAKE_TMUX_STATE'])
a=sys.argv[1:]
sock=os.environ.get('TMUX','').split(',')[0]
if a[:1]==['-S']: sock=a[1]; a=a[2:]
if sock!='/fake/owned': sys.exit(91)
cmds=[[]]
for x in a: cmds.append([]) if x==';' else cmds[-1].append(x)  # tmux command lists
with p.with_suffix('.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 s=json.loads(p.read_text())
 for a in [c for c in cmds if c]:
  s['calls'].append(a)
  cmd=a[0]; target=a[a.index('-t')+1] if '-t' in a else ''
  if cmd=='display-message':
   fmt=a[-1]
   if target=='%42': print('@7' if fmt=='#{window_id}' else '/work/project')
   elif target=='@7': print('0')
   else: sys.exit(1)
  elif cmd=='show-options': print(s['windows'].get(target,{}).get(a[-1],''))
  elif cmd=='set-option':
   if target not in s['windows']: sys.exit(1)
   key=next(x for x in a[1:] if x.startswith('@agent_'))
   if '-uw' in a: s['windows'][target].pop(key,None)
   else: s['windows'][target][key]=a[-1]
 p.write_text(json.dumps(s))
# Simulate only the title job, not the unrelated watcher watchdog request.
if cmd=='run-shell' and ' condense ' in a[-1]:
 subprocess.Popen(['bash','-c',a[-1]], env=os.environ.copy(),
                  cwd=str(p.parent), start_new_session=True,
                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
'''
FAKE_OWNER = r'''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
s=json.loads(Path(os.environ['FAKE_OWNER_STATE']).read_text())
print(json.dumps(s.get(sys.argv[-1],{'status':'unbound'})))
'''
FAKE_COPILOT = r'''#!/usr/bin/env python3
import os,time
from pathlib import Path
r=Path(os.environ['FAKE_CONDENSER'])
(r/'started').touch()
with (r/'calls').open('a') as f: f.write(str(os.getpid())+'\n')
while (r/'block').exists(): time.sleep(.02)
print('Condensed title')
(r/'finished').touch()
'''


class IndicatorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-indicator-test-")
        self.root = Path(self.tmp.name)
        self.bin = self.root / ".local/bin"
        self.bin.mkdir(parents=True)
        self.cond = self.root / "condenser"
        self.cond.mkdir()
        self.state = self.root / "tmux.json"
        self.state.write_text(json.dumps({"calls": [], "windows": {"@7": {}, "@99": {"@agent_state": "unrelated"}}}))
        self.owners = self.root / "owners.json"
        self.owners.write_text("{}")
        self.script = self.root / "agent-tab-indicator.sh"
        shutil.copyfile(SOURCE, self.script)
        for name, source in [("tmux", FAKE_TMUX), ("codex-terminal-owner", FAKE_OWNER), ("copilot", FAKE_COPILOT)]:
            path = self.bin / name
            path.write_text(source)
            path.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.root), PATH=str(self.bin)+":"+os.environ["PATH"],
                        TMPDIR=str(self.root), TMUX="/fake/stale,12,0", TMUX_PANE="%999",
                        FAKE_TMUX_STATE=str(self.state), FAKE_OWNER_STATE=str(self.owners),
                        FAKE_CONDENSER=str(self.cond), AGENT_TAB_CONDENSE_MODEL="",
                        TMUX_ASSISTANT_RESURRECT_DIR=str(self.root / "tmux-assistant-resurrect"))
        for key in ("AGENT_TAB_SOCKET", "AGENT_TAB_OWNER_SESSION", "AGENT_TAB_OWNER_TOKEN", "AGENT_TAB_OWNER_BINDING"):
            self.env.pop(key, None)
        self.bind("aaaa")

    def tearDown(self):
        (self.cond / "block").unlink(missing_ok=True)
        self.tmp.cleanup()

    def bind(self, sid, token="frontend-token"):
        self.owners.write_text(json.dumps({sid: {"status": "bound", "session_id": sid,
            "tmux_socket": "/fake/owned", "pane": "%42", "token": token, "binding_id": "binding-"+sid,
            "frontend_pid": 123, "frontend_start": "test start"}}))

    def hook(self, mode, sid="aaaa", agent="codex", **payload):
        data = dict(session_id=sid, cwd="/work/project", **payload)
        env = dict(self.env)
        if agent == "claude": env.update(TMUX="/fake/owned,12,0", TMUX_PANE="%42")
        result = subprocess.run(["bash", str(self.script), mode, agent], input=json.dumps(data),
                                text=True, capture_output=True, env=env, cwd=self.root, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.window()

    def window(self):
        return json.loads(self.state.read_text())["windows"]["@7"]

    def wait_for(self, filename):
        deadline = time.monotonic() + 5
        while not (self.cond / filename).exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue((self.cond / filename).exists(), filename)

    def test_lifecycle_uses_owner_socket_and_leaves_unrelated_window(self):
        self.assertEqual(self.hook("running")["@agent_state"], "running")
        self.assertEqual(self.hook("needs-approval")["@agent_state"], "needs-input")
        self.assertEqual(self.hook("heartbeat")["@agent_state"], "running")
        self.assertEqual(self.hook("done")["@agent_state"], "done")
        self.assertEqual(self.hook("heartbeat")["@agent_state"], "done")
        self.assertEqual(self.hook("clear"), {})
        state = json.loads(self.state.read_text())
        self.assertEqual(state["windows"]["@99"], {"@agent_state": "unrelated"})
        self.assertFalse(any("%999" in call for call in state["calls"]))

    def test_unbound_and_subagent_hooks_are_inert(self):
        self.hook("done")
        before = self.state.read_text()
        self.hook("heartbeat", agent_id="child")
        self.hook("running", sid="desktop")
        self.assertEqual(self.state.read_text(), before)

    def test_reconcile_preserves_newer_states(self):
        for mode, expected in [("running", "running"), ("needs-approval", "needs-input"), ("done", "done")]:
            self.hook(mode)
            self.assertEqual(self.hook("reconcile", hook_event_name="SessionStart")["@agent_state"], expected)

    def test_answering_waiting_prompt_rearms_running_after_focus(self):
        self.hook("needs-approval")
        result = subprocess.run(["bash", str(self.script), "clear-current", "@7"],
            input="", text=True, capture_output=True, timeout=5,
            env=dict(self.env, TMUX="/fake/owned,12,0"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.window()["@agent_state"], "idle")
        self.assertIn("@agent_pending", self.window())
        self.assertEqual(self.hook("heartbeat")["@agent_state"], "running")
        self.assertNotIn("@agent_pending", self.window())

    def test_stale_reconcile_is_inert(self):
        self.hook("running")
        before = self.state.read_text()
        self.hook("reconcile", terminal_binding_id="replaced-binding")
        self.assertEqual(self.state.read_text(), before)

    def test_interrupt_stops_working_but_preserves_conversation(self):
        self.hook("running")
        state = json.loads(self.state.read_text())
        state["windows"]["@7"].update({"@agent_summary": "project/Current title",
            "@agent_rollout": "/work/rollout.jsonl", "@agent_pending": "123"})
        self.state.write_text(json.dumps(state))
        w = self.hook("interrupt", hook_event_name="Interrupt")
        self.assertEqual(w["@agent_state"], "idle")
        self.assertEqual(w["@agent_summary"], "project/Current title")
        self.assertEqual(w["@agent_rollout"], "/work/rollout.jsonl")
        self.assertNotIn("@agent_pending", w)

    def test_switching_thread_resets_condensed_title_and_rejects_old_hooks(self):
        self.hook("running")
        state = json.loads(self.state.read_text())
        state["windows"]["@7"].update({"@agent_summary": "project/Old title", "@agent_summary_cond": "1"})
        self.state.write_text(json.dumps(state))
        self.bind("bbbb")
        w = self.hook("idle", sid="bbbb", source="startup")
        self.assertEqual(w["@agent_summary"], "project/New Session")
        self.assertNotIn("@agent_summary_cond", w)
        before = self.state.read_text()
        self.hook("clear", sid="aaaa")
        self.assertEqual(self.state.read_text(), before)

    def test_delayed_codex_summary_cannot_overwrite_new_thread(self):
        (self.cond / "block").touch()
        self.hook("running", prompt="Explain old conversation")
        self.wait_for("started")
        self.bind("bbbb")
        self.hook("idle", sid="bbbb", source="startup")
        (self.cond / "block").unlink()
        self.wait_for("finished")
        time.sleep(.25)
        self.assertEqual(self.window()["@agent_summary"], "project/New Session")

    def test_delayed_codex_summary_cannot_overwrite_claude(self):
        (self.cond / "block").touch()
        self.hook("running", prompt="Explain old conversation")
        self.wait_for("started")
        self.hook("idle", agent="claude", sid="cccc", source="startup")
        (self.cond / "block").unlink()
        self.wait_for("finished")
        time.sleep(.25)
        self.assertEqual(self.window()["@agent_summary"], "project/New Session")

    def test_delayed_summary_checks_registry_even_before_next_hook(self):
        (self.cond / "block").touch()
        self.hook("running", prompt="Explain old conversation")
        self.wait_for("started")
        previous = self.window()["@agent_summary"]
        # The protocol bridge selects another thread before its hook arrives.
        # Old tmux markers alone must not permit the late title update.
        self.bind("bbbb")
        (self.cond / "block").unlink()
        self.wait_for("finished")
        time.sleep(.25)
        self.assertEqual(self.window()["@agent_summary"], previous)

    def condenser_env(self):
        return dict(self.env, TMUX="/fake/owned,12,0", TMUX_PANE="%42",
                    AGENT_TAB_SOCKET="/fake/owned", AGENT_TAB_OWNER_SESSION="aaaa",
                    AGENT_TAB_OWNER_TOKEN="frontend-token", AGENT_TAB_OWNER_BINDING="binding-aaaa")

    def condense(self, raw, background=False):
        command = ["bash", str(self.script), "condense", "@7", "project", raw]
        kwargs = dict(env=self.condenser_env(), cwd=self.root,
                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if background:
            return subprocess.Popen(command, start_new_session=True, **kwargs)
        result = subprocess.run(command, timeout=8, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_orphan_directory_cannot_block_title(self):
        self.hook("running")
        raw = "Repair orphaned title"
        key = hashlib.sha256(raw.encode()).hexdigest()[:16]
        orphan = self.root / ("agent-tab-condense." + key + ".lock")
        orphan.mkdir()
        (orphan / "err").write_text("Previous model call finished")
        self.condense(raw)
        self.assertEqual(self.window()["@agent_summary"], "project/Condensed title")
        self.assertTrue(orphan.is_dir())  # no unsafe legacy lock deletion
        self.assertIn(key, (self.root / ".cache/agent-tab/titles.tsv").read_text())

    def test_advisory_lock_excludes_live_worker_and_releases_after_kill(self):
        self.hook("running")
        raw = "Recover terminated condenser"
        (self.cond / "block").touch()
        worker = self.condense(raw, background=True)
        try:
            self.wait_for("started")
            self.condense(raw)
            self.assertEqual(len((self.cond / "calls").read_text().splitlines()), 1)
            os.killpg(worker.pid, signal.SIGKILL)
            worker.communicate(timeout=5)
            (self.cond / "block").unlink()
            self.condense(raw)
            self.assertEqual(len((self.cond / "calls").read_text().splitlines()), 2)
            self.assertEqual(self.window()["@agent_summary"], "project/Condensed title")
            # A third worker reads the persisted cache, no new model call.
            self.condense(raw)
            self.assertEqual(len((self.cond / "calls").read_text().splitlines()), 2)
        finally:
            if worker.poll() is None:
                os.killpg(worker.pid, signal.SIGKILL)
                worker.communicate(timeout=5)

    def test_tmux_launch_quotes_title_and_owner_environment(self):
        self.bind("aaaa", token="frontend; touch TOKEN_INJECTED")
        self.env["AGENT_TAB_CONDENSE_MODEL"] = "model' $(touch MODEL_INJECTED)"
        self.hook("running", prompt="Say $(touch INJECTED) and `touch ALSO_INJECTED`")
        self.wait_for("finished")
        deadline = time.monotonic() + 5
        while self.window().get("@agent_summary") != "project/Condensed title" and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(self.window()["@agent_summary"], "project/Condensed title")
        calls = json.loads(self.state.read_text())["calls"]
        jobs = [a for a in calls if a[0] == "run-shell" and " condense " in a[-1]]
        self.assertTrue(jobs)
        self.assertIn("-b", jobs[-1])
        for name in ("INJECTED", "ALSO_INJECTED", "MODEL_INJECTED", "TOKEN_INJECTED"):
            self.assertFalse((self.root / name).exists(), name)

    def test_tracker_end_cannot_remove_another_thread(self):
        # A verified new thread may bind before its registry row is created.
        # The old thread being registered must not block replacement.
        (self.root / ".codex").mkdir()
        with sqlite3.connect(self.root / ".codex/state_5.sqlite") as db:
            db.execute("create table threads (id text, rollout_path text)")
            db.execute("insert into threads values (?, ?)", ("aaaa", "/work/old-rollout.jsonl"))
        def track(event, sid="aaaa"):
            result = subprocess.run([str(TRACKER)], input=json.dumps({"hook_event_name": event,
                "session_id": sid, "cwd": "/work/project"}), text=True, env=self.env, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0)
        track("SessionStart")
        path = self.root / "tmux-assistant-resurrect/codex-123.json"
        self.assertEqual(json.loads(path.read_text())["env"]["tmux_pane"], "%42")
        self.bind("bbbb")
        track("SessionStart", "bbbb")
        self.bind("aaaa")
        track("SessionEnd")
        self.assertEqual(json.loads(path.read_text())["session_id"], "bbbb")

    def test_tracker_reconcile_preserves_transcript_and_ignores_stale_binding(self):
        path = self.root / "tmux-assistant-resurrect/codex-123.json"
        def track(**extra):
            payload = dict(session_id="aaaa", cwd="/work/project", hook_event_name="SessionStart", **extra)
            result = subprocess.run([str(TRACKER)], input=json.dumps(payload), text=True,
                                    env=self.env, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0)
        track(transcript_path="/work/rollout.jsonl", model="codex")
        track(terminal_reconcile=True, terminal_binding_id="binding-aaaa")
        self.assertEqual(json.loads(path.read_text())["transcript_path"], "/work/rollout.jsonl")
        before = path.read_text()
        track(terminal_reconcile=True, terminal_binding_id="old-binding", transcript_path="/wrong")
        self.assertEqual(path.read_text(), before)
        track(agent_id="subagent", transcript_path="/wrong")
        self.assertEqual(path.read_text(), before)

    @unittest.skipUnless(shutil.which("tmux"), "tmux executable required")
    def test_real_detached_server_state_lifecycle(self):
        # Deliberately bypass every user config and use a unique server. This
        # never attaches a client or changes any existing terminal selection.
        tmux = shutil.which("tmux")
        server = "codex-indicator-test-" + uuid.uuid4().hex
        command = [tmux, "-L", server, "-f", "/dev/null"]
        env = dict(self.env, PATH=os.environ["PATH"])
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        try:
            result = subprocess.run(command + ["new-session", "-d", "-s", "smoke", "-P", "-F",
                "#{pane_id}|#{socket_path}", "/bin/sleep 30"], text=True, capture_output=True, env=env, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            pane, socket = result.stdout.strip().split("|", 1)
            owner = json.loads(self.owners.read_text())
            owner["aaaa"].update(pane=pane, tmux_socket=socket)
            self.owners.write_text(json.dumps(owner))
            env.update(TMUX="/fake/stale,12,0", TMUX_PANE="%999")
            for mode, expected in [("running", "running"), ("needs-approval", "needs-input"),
                                   ("interrupt", "idle"), ("done", "done"), ("clear", "")]:
                result = subprocess.run(["bash", str(self.script), mode, "codex"],
                    input=json.dumps({"session_id": "aaaa", "cwd": "/work/project"}),
                    text=True, capture_output=True, env=env, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                value = subprocess.run(command + ["show-options", "-wqv", "-t", pane, "@agent_state"],
                    text=True, capture_output=True, timeout=5).stdout.strip()
                self.assertEqual(value, expected)
        finally:
            subprocess.run(command + ["kill-server"], capture_output=True, timeout=5)


if __name__ == "__main__":
    unittest.main()
