"""AGENT-ORIGINATED TURNS: a turn another agent started (tmux-mcp's
message_agent) finishes QUIET - the window goes back to the attention state
it had before - unless the agent flagged Mack, the turn failed, or its origin
is unknown.

Indicator tests drive the real agent-tab-indicator.sh against a real, private
tmux server (`tmux -L <unique> -f /dev/null`, killed in cleanup), the same
seams as test_agent_state_notice: a `tmux` wrapper answers
#{window_active_clients} from $FAKE_ACTIVE_CLIENTS, `lsappinfo` names the
frontmost app ($FAKE_FRONT=wez), `notifyutil` and `copilot` are inert stubs.
tmux-mcp's side (@agent_inbound entries) is written by the test exactly as the
contract specifies: "<epoch> <sha1(trimmed body)> <from> <for>", "|"-joined.
Watcher tests reuse test_agent_jump_watcher's fake-tmux harness. Run with
unittest or pytest.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

import test_agent_jump_watcher as jw
from test_agent_state_notice import FAKE_LSAPPINFO, FAKE_TMUX_WRAPPER, write_exe
from test_codex_indicator import FAKE_OWNER

SCRIPTS = Path(os.environ.get(
    "AGENT_TMUX_SCRIPTS",
    str(Path.home() / ".local/share/chezmoi/dot_config/tmux/scripts")))
INDICATOR = SCRIPTS / "executable_agent-tab-indicator.sh"
NOTIFY_USER = SCRIPTS / "executable_agent-notify-user.sh"
REAL_TMUX = shutil.which("tmux")

CTX_START = "This turn was started by a message from @"


def sha1(body):
    return hashlib.sha1(body.strip(" \t\r\n").encode()).hexdigest()


@unittest.skipUnless(REAL_TMUX, "tmux executable required")
class OriginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agent-origin-test-")
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"; self.bin.mkdir()
        self.home = self.root / "home"; (self.home / ".local/bin").mkdir(parents=True)
        write_exe(self.bin / "notifyutil", "#!/bin/sh\nexit 0\n")
        write_exe(self.bin / "copilot", "#!/bin/sh\nexit 1\n")     # never a model call
        write_exe(self.bin / "lsappinfo", FAKE_LSAPPINFO)
        write_exe(self.bin / "tmux", FAKE_TMUX_WRAPPER % REAL_TMUX)
        self.script = self.root / "agent-tab-indicator.sh"
        shutil.copyfile(INDICATOR, self.script)
        self.server = "agent-origin-test-" + uuid.uuid4().hex[:12]
        self.tmuxcmd = [REAL_TMUX, "-L", self.server, "-f", "/dev/null"]
        self.socket = None
        self.addCleanup(self.cleanup)
        clean = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        r = subprocess.run(self.tmuxcmd + ["new-session", "-d", "-s", "origin", "-x", "80", "-y", "24",
                                           "-P", "-F", "#{pane_id}|#{window_id}|#{socket_path}",
                                           "/bin/sleep 600"],
                           text=True, capture_output=True, env=clean, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.pane, self.win, self.socket = r.stdout.strip().split("|", 2)
        self.env = dict(os.environ, HOME=str(self.home), TMPDIR=str(self.root) + "/",
                        PATH=str(self.bin) + ":" + os.environ["PATH"],
                        TMUX="%s,1,0" % self.socket, TMUX_PANE=self.pane,
                        AGENT_STATE_NOTICE="", AGENT_TAB_CONDENSE_MODEL="",
                        FAKE_FRONT="", FAKE_ACTIVE_CLIENTS="0")
        for key in ("AGENT_TAB_SOCKET", "AGENT_TAB_OWNER_SESSION", "AGENT_TAB_OWNER_TOKEN",
                    "AGENT_TAB_OWNER_BINDING", "AGENT_TAB_OWNER_RECORD", "CODEX_THREAD_ID"):
            self.env.pop(key, None)
        self.transcript = self.root / "t.jsonl"
        self.transcript.write_text("")

    def cleanup(self):
        subprocess.run(self.tmuxcmd + ["kill-server"], capture_output=True, timeout=5)
        if self.socket:
            Path(self.socket).unlink(missing_ok=True)
        self.tmp.cleanup()

    # --- helpers ---

    def opt(self, name):
        r = subprocess.run(self.tmuxcmd + ["show-options", "-wqv", "-t", self.win, name],
                           text=True, capture_output=True, timeout=5)
        return r.stdout.rstrip("\n")

    def has(self, name):
        r = subprocess.run(self.tmuxcmd + ["show-options", "-w", "-t", self.win],
                           text=True, capture_output=True, timeout=5)
        return any(line.split(" ", 1)[0] == name for line in r.stdout.splitlines())

    def set_opt(self, name, value):
        r = subprocess.run(self.tmuxcmd + ["set-option", "-w", "-t", self.win, name, value],
                           text=True, capture_output=True, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)

    def deliver(self, body, frm="peer", fr="agent", age=0, raw=False):
        """tmux-mcp's half: stamp @agent_inbound for a message about to be
        submitted. Returns the prompt text the agent will see."""
        text = body if raw else "[message from @%s] %s" % (frm, body)
        entry = "%d %s %s %s" % (int(time.time()) - age, sha1(text), frm, fr)
        cur = self.opt("@agent_inbound")
        self.set_opt("@agent_inbound", (cur + "|" if cur else "") + entry)
        return text

    def hook(self, *argv, payload=None, **env):
        e = dict(self.env, **env)
        stdin = "" if payload is None else json.dumps(payload)
        r = subprocess.run(["bash", str(self.script), *argv], input=stdin, text=True,
                           capture_output=True, env=e, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def prompt(self, text, pid, agent="claude", **env):
        payload = {"session_id": "s1", "cwd": str(self.root), "hook_event_name": "UserPromptSubmit",
                   "prompt": text, "transcript_path": str(self.transcript)}
        if pid is not None:
            payload["prompt_id" if agent == "claude" else "turn_id"] = pid
        return self.hook("running", agent, payload=payload, **env).stdout

    def stop(self, pid, agent="claude", msg="Done.", **env):
        payload = {"session_id": "s1", "cwd": str(self.root), "hook_event_name": "Stop",
                   "last_assistant_message": msg, "transcript_path": str(self.transcript)}
        if pid is not None:
            payload["prompt_id" if agent == "claude" else "turn_id"] = pid
        return self.hook("done", agent, payload=payload, **env).stdout

    def clear_current(self, **env):
        return self.hook("clear-current", self.win, **env)

    def ctx(self, out):
        self.assertTrue(out.strip(), "no additionalContext printed")
        j = json.loads(out)
        self.assertEqual(j["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        return j["hookSpecificOutput"]["additionalContext"]

    def seed_done(self, since_age=300, detail="Earlier result"):
        """An unseen green from Mack's own earlier turn."""
        since = "%d done" % (int(time.time()) - since_age)
        self.set_opt("@agent_state", "done")
        self.set_opt("@agent_since", since)
        self.set_opt("@agent_detail_kind", "done")
        self.set_opt("@agent_detail", detail)
        return since

    VIEWING = dict(FAKE_ACTIVE_CLIENTS="1", FAKE_FRONT="wez")

    # --- new-turn agent origin: quiet restore ---

    def test_agent_turn_from_idle_is_quiet_back_to_idle(self):
        self.set_opt("@agent_state", "idle")
        text = self.deliver("please rerun the suite")
        out = self.prompt(text, "p1")
        c = self.ctx(out)
        self.assertTrue(c.startswith(CTX_START + "peer, not by Mack."), c)
        self.assertIn("notify_user", c)
        self.assertIn('agent-notify-user.sh \\"<reason>\\"'.replace("\\", ""), c)
        self.assertEqual(self.opt("@agent_state"), "running")
        self.assertEqual(self.opt("@agent_turn_origin"), "agent p1 peer")
        self.assertEqual(self.opt("@agent_prior_state"), "idle")
        self.assertFalse(self.has("@agent_inbound"))          # the match is consumed
        self.assertEqual(self.stop("p1"), "")
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assertEqual(self.opt("@agent_turn_result"), "p1 quiet idle")
        for name in ("@agent_prior_state", "@agent_prior_since", "@agent_prior_detail_kind"):
            self.assertFalse(self.has(name), name)

    def test_unseen_done_stays_done_with_its_stamp_and_detail(self):
        since = self.seed_done()
        self.prompt(self.deliver("ping"), "p1")
        self.assertEqual(self.opt("@agent_prior_state"), "done")
        self.assertEqual(self.opt("@agent_prior_since"), since)
        self.stop("p1", msg="Replied to peer.")
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertEqual(self.opt("@agent_since"), since)
        self.assertEqual((self.opt("@agent_detail_kind"), self.opt("@agent_detail")), ("done", "Earlier result"))
        self.assertEqual(self.opt("@agent_turn_result"), "p1 quiet done")
        self.assertFalse(self.has("@agent_seen_at"))

    def test_restored_done_while_viewing_lands_idle_and_seen(self):
        self.seed_done()
        self.prompt(self.deliver("ping"), "p1")
        self.stop("p1", **self.VIEWING)
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assertRegex(self.opt("@agent_seen_at"), r"^\d+$")
        self.assertEqual(self.opt("@agent_turn_result"), "p1 quiet idle")

    def test_raw_multiline_message_matches_with_cr_line_ends(self):
        # paste-buffer without -r turns LF into CR: tmux-mcp hashed LF.
        body = "line one\nline two\n"
        self.deliver(body, raw=True)
        out = self.prompt("line one\rline two\r", "p1")
        self.assertTrue(out.strip())
        self.assertEqual(self.opt("@agent_turn_origin"), "agent p1 peer")

    def test_raw_message_matches_by_hash_alone(self):
        text = self.deliver("  plain body, no prefix\t\n", raw=True)
        self.prompt(text, "p1")
        self.assertEqual(self.opt("@agent_turn_origin"), "agent p1 peer")

    # --- loud turns ---

    def test_user_turn_is_loud_and_prints_nothing(self):
        self.set_opt("@agent_state", "idle")
        self.assertEqual(self.prompt("fix the parser", "p1"), "")
        self.assertEqual(self.opt("@agent_turn_origin"), "user p1 -")
        self.assertFalse(self.has("@agent_prior_state"))
        self.stop("p1")
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertEqual(self.opt("@agent_turn_result"), "p1 loud")

    def test_prefix_without_inbound_entry_is_a_user_prompt(self):
        # Typing the opener by hand is not a delivered message.
        self.assertEqual(self.prompt("[message from @peer] hi", "p1"), "")
        self.assertEqual(self.opt("@agent_turn_origin"), "user p1 -")

    def test_stale_inbound_entry_is_ignored_and_pruned(self):
        text = self.deliver("old news", age=181)
        self.assertEqual(self.prompt(text, "p1"), "")
        self.assertEqual(self.opt("@agent_turn_origin"), "user p1 -")
        self.assertFalse(self.has("@agent_inbound"))
        self.stop("p1")
        self.assertEqual(self.opt("@agent_turn_result"), "p1 loud")

    def test_other_fresh_entries_survive_a_match(self):
        other = "%d %s other agent" % (int(time.time()), sha1("[message from @other] x"))
        self.set_opt("@agent_inbound", other)
        self.prompt(self.deliver("mine"), "p1")
        self.assertEqual(self.opt("@agent_inbound"), other)

    def test_mixed_turn_upgrades_to_user_never_back(self):
        self.seed_done()
        self.prompt(self.deliver("start"), "p1")
        self.assertEqual(self.prompt("actually also do X", "p1"), "")   # Mack types mid-turn
        self.assertEqual(self.opt("@agent_turn_origin"), "user p1 -")
        self.assertFalse(self.has("@agent_prior_state"))
        self.assertEqual(self.prompt(self.deliver("more"), "p1"), "")   # never downgraded
        self.assertEqual(self.opt("@agent_turn_origin"), "user p1 -")
        self.stop("p1")
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertEqual(self.opt("@agent_turn_result"), "p1 loud")

    def test_mid_turn_agent_message_in_a_user_turn_stays_loud(self):
        self.prompt("build it", "p1")
        text = self.deliver("status?")
        self.assertEqual(self.prompt(text, "p1"), "")                   # no context mid-turn
        self.assertEqual(self.opt("@agent_turn_origin"), "user p1 -")
        seen = self.opt("@agent_inbound_seen").split(" ")
        self.assertEqual(seen[1:], [sha1(text), "peer", "agent"])
        self.stop("p1")
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertEqual(self.opt("@agent_turn_result"), "p1 loud")

    def test_for_user_reply_chain_is_loud(self):
        text = self.deliver("here is the answer you asked for", fr="user")
        self.assertEqual(self.prompt(text, "p1"), "")
        self.assertEqual(self.opt("@agent_turn_origin"), "user p1 -")
        self.stop("p1")
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertEqual(self.opt("@agent_turn_result"), "p1 loud")

    def test_notify_user_flag_makes_it_loud_with_the_reason(self):
        self.set_opt("@agent_for_user", "1 stale flag from an earlier turn")
        self.prompt(self.deliver("deploy?"), "p1")
        self.assertFalse(self.has("@agent_for_user"))                   # cleared at turn start
        r = subprocess.run(["bash", str(NOTIFY_USER), "Deploy needs your\nOK | #now;"],
                           env=dict(self.env, PATH=self.env["PATH"]), text=True,
                           capture_output=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        flag = self.opt("@agent_for_user")
        self.assertRegex(flag, r"^\d+ Deploy needs your OK now$")
        self.stop("p1")
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertEqual(self.opt("@agent_detail"), "Deploy needs your OK now")
        self.assertEqual(self.opt("@agent_turn_result"), "p1 loud")

    def test_notify_user_cli_refuses_without_a_pane(self):
        self.set_opt("@agent_state", "idle")
        env = dict(self.env); env.pop("TMUX_PANE")
        r = subprocess.run(["bash", str(NOTIFY_USER), "x"], env=env, text=True, capture_output=True, timeout=10)
        self.assertEqual(r.returncode, 1)
        r = subprocess.run(["bash", str(NOTIFY_USER)], env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(r.returncode, 2)
        long = "é" * 300
        r = subprocess.run(["bash", str(NOTIFY_USER), long], env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.opt("@agent_for_user").split(" ", 1)[1]), 200)

    def pty_window(self, owner_pid=None):
        """A pty-MCP shell window in a hidden `agents` session, as the pty
        server makes them: its pane is what an agent's shell sees as
        $TMUX_PANE. Returns (pane, window)."""
        r = subprocess.run(self.tmuxcmd + ["new-session", "-d", "-s", "agents", "-P", "-F",
                                           "#{pane_id}|#{window_id}", "/bin/sleep 600"],
                           text=True, capture_output=True, timeout=5)
        if r.returncode != 0:   # the session exists: add a window
            r = subprocess.run(self.tmuxcmd + ["new-window", "-d", "-t", "agents", "-P", "-F",
                                               "#{pane_id}|#{window_id}", "/bin/sleep 600"],
                               text=True, capture_output=True, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)
        pane, win = r.stdout.strip().split("|")
        if owner_pid is not None:
            subprocess.run(self.tmuxcmd + ["set-option", "-w", "-t", win, "@pty_claude_pid", str(owner_pid)],
                           check=True, timeout=5)
        return pane, win

    def agent_pane_pid(self):
        r = subprocess.run(self.tmuxcmd + ["display-message", "-p", "-t", self.pane, "#{pane_pid}"],
                           text=True, capture_output=True, timeout=5)
        return int(r.stdout.strip())

    def notify_from(self, pane, reason="look at this"):
        return subprocess.run(["bash", str(NOTIFY_USER), reason], env=dict(self.env, TMUX_PANE=pane),
                              text=True, capture_output=True, timeout=10)

    def test_notify_user_cli_from_a_pty_shell_flags_the_owning_agent(self):
        # The agent pane runs a process on its tty; the pty window names it.
        self.set_opt("@agent_state", "running")
        pane, pwin = self.pty_window(owner_pid=self.agent_pane_pid())
        r = self.notify_from(pane)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(self.opt("@agent_for_user"), r"^\d+ look at this$")
        out = subprocess.run(self.tmuxcmd + ["show-options", "-wqv", "-t", pwin, "@agent_for_user"],
                             text=True, capture_output=True, timeout=5).stdout
        self.assertEqual(out.strip(), "")                  # never on the pty's own window

    def test_notify_user_cli_refuses_a_window_no_agent_owns(self):
        self.set_opt("@agent_state", "running")
        for owner in (None, 999999):                       # untagged; a dead pid
            with self.subTest(owner=owner):
                pane, pwin = self.pty_window(owner_pid=owner)
                r = self.notify_from(pane)
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn("NOT notified", r.stderr)
                self.assertFalse(self.has("@agent_for_user"))
        # And the agent window itself without a state is no agent window.
        subprocess.run(self.tmuxcmd + ["set-option", "-uw", "-t", self.win, "@agent_state"], check=True)
        r = self.notify_from(self.pane)
        self.assertEqual(r.returncode, 1)
        self.assertFalse(self.has("@agent_for_user"))

    # --- review round 1 ---

    def test_wakeup_after_a_quiet_restore_keeps_macks_green(self):
        # Mack's own turn finishes green (unseen); an agent pings; its quiet
        # finish restores green; then a background task it launched reports
        # back. That wake-up turn must restore green too.
        self.prompt("do it", "u1"); self.stop("u1")
        since = self.opt("@agent_since")
        self.prompt(self.deliver("ping"), "a1"); self.stop("a1")
        self.assertEqual(self.opt("@agent_state"), "done")
        self.prompt("<task-notification>\n<task-id>x</task-id></task-notification>", "w1")
        self.assertEqual(self.opt("@agent_turn_origin"), "agent w1 peer")
        self.assertEqual(self.opt("@agent_prior_state"), "done")
        self.stop("w1")
        self.assertEqual((self.opt("@agent_state"), self.opt("@agent_since")), ("done", since))
        self.assertEqual(self.opt("@agent_turn_result"), "w1 quiet done")

    def test_agent_ping_into_macks_bg_held_turn_ends_green(self):
        # Mack's turn ended loud but is held running by background work.
        self.prompt("do it", "u1")
        self.set_opt("@agent_turn_result", "u1 loud")
        self.set_opt("@agent_detail_kind", "done")
        self.set_opt("@agent_detail", "Built it")
        self.prompt(self.deliver("ping"), "a1")
        self.assertEqual(self.opt("@agent_prior_state"), "done")
        self.assertEqual(self.opt("@agent_prior_detail"), "Built it")
        self.stop("a1")
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertEqual(self.opt("@agent_detail"), "Built it")
        self.assertEqual(self.opt("@agent_turn_result"), "a1 quiet done")
        # ...and its background work reporting back later keeps it green.
        self.prompt("<task-notification>\n<task-id>b</task-id>", "w1")
        self.stop("w1")
        self.assertEqual(self.opt("@agent_state"), "done")

    def test_agent_ping_into_a_running_agent_held_turn_is_not_green(self):
        # Control: held by an AGENT turn's background work, the prior it saved stands.
        self.set_opt("@agent_state", "idle")
        self.prompt(self.deliver("go"), "a1")
        write_exe(self.home / ".local/bin/agent-bg-pending", "#!/bin/sh\nexit 0\n")
        self.stop("a1")
        (self.home / ".local/bin/agent-bg-pending").unlink()
        self.prompt(self.deliver("again"), "a2")
        self.assertEqual(self.opt("@agent_prior_state"), "idle")
        self.stop("a2")
        self.assertEqual(self.opt("@agent_state"), "idle")

    def test_hostile_handle_is_sanitized_before_tmux(self):
        self.set_opt("@agent_state", "idle")
        text = self.deliver("ping", frm="evil;")
        out = self.prompt(text, "a1")
        self.assertEqual(self.opt("@agent_state"), "running")
        self.assertEqual(self.opt("@agent_turn_origin"), "agent a1 evil")
        self.assertIn("@evil,", self.ctx(out))
        self.prompt("mack again", "a1")                     # upgrade
        text = self.deliver("more", frm="x;y z")
        self.prompt(text, "a1")
        self.assertEqual(self.opt("@agent_inbound_seen").split(" ")[2:], ["xy", "agent"])

    def test_quiet_restore_leaves_a_log_mark(self):
        self.seed_done()
        self.prompt(self.deliver("ping"), "a1")
        self.stop("a1")
        self.assertEqual(self.opt("@agent_quiet"), "peer")
        self.assertEqual(self.opt("@agent_detail"), "Earlier result")   # the window's detail untouched
        # No watcher tick consumed it: the next turn's prompt drops it...
        self.prompt("mine", "u2")
        self.assertFalse(self.has("@agent_quiet"))
        # ...and so does a loud finish.
        self.set_opt("@agent_quiet", "peer")
        self.stop("u2")
        self.assertFalse(self.has("@agent_quiet"))

    # --- final round: whose background work is out ---

    def bg(self, out):
        stub = self.home / ".local/bin/agent-bg-pending"
        if out:
            write_exe(stub, "#!/bin/sh\nexit 0\n")
        else:
            stub.unlink(missing_ok=True)

    def test_macks_bg_wakeup_after_an_agent_turn_is_loud(self):
        self.prompt("build and test it", "u1")
        self.bg(True)
        self.stop("u1")                                       # held: Mack's work is out
        self.assertEqual((self.opt("@agent_state"), self.opt("@agent_bg_owner")), ("running", "user"))
        self.prompt(self.deliver("ping"), "a1")
        self.assertEqual(self.opt("@agent_prior_state"), "done")
        self.stop("a1")                                       # still held
        self.assertEqual(self.opt("@agent_turn_result"), "a1 quiet running")
        self.assertEqual(self.opt("@agent_bg_owner"), "user")  # sticky toward user
        self.clear_current()                                  # Mack looks: prior done -> idle
        self.assertEqual(self.opt("@agent_prior_state"), "idle")
        self.bg(False)
        self.prompt("<task-notification>\n<task-id>u1-tests</task-id>", "w1")
        self.assertEqual(self.opt("@agent_turn_origin"), "user w1 -")
        self.assertFalse(self.has("@agent_prior_state"))
        self.stop("w1")
        self.assertEqual(self.opt("@agent_state"), "done")   # Mack's own result shows
        self.assertEqual(self.opt("@agent_turn_result"), "w1 loud")
        self.assertFalse(self.has("@agent_bg_owner"))         # not held: cleared

    def test_agent_only_bg_wakeup_stays_quiet(self):
        self.set_opt("@agent_state", "idle")
        self.prompt(self.deliver("run the slow suite"), "a1")
        self.bg(True)
        self.stop("a1")
        self.assertEqual(self.opt("@agent_bg_owner"), "agent")
        self.bg(False)
        self.prompt("<task-notification>\n<task-id>a1-suite</task-id>", "w1")
        self.assertEqual(self.opt("@agent_turn_origin"), "agent w1 peer")
        self.stop("w1")
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assertEqual(self.opt("@agent_turn_result"), "w1 quiet idle")
        self.assertFalse(self.has("@agent_bg_owner"))

    def test_wakeup_without_hook_inherits_the_bg_owner(self):
        self.prompt("build it", "u1")
        self.bg(True)
        self.stop("u1")
        self.prompt(self.deliver("ping"), "a1")
        self.stop("a1")
        self.bg(False)
        self.write_rows({"type": "user", "promptId": "w1", "promptSource": "system",
                         "origin": {"kind": "task-notification"},
                         "message": {"role": "user", "content": "<task-notification>\n<task-id>b</task-id>"}})
        self.stop("w1")
        self.assertEqual(self.opt("@agent_turn_result"), "w1 loud")
        self.assertEqual(self.opt("@agent_state"), "done")

    def test_split_loud_held_stop_still_saves_a_green_prior(self):
        # Claude's split case: the loud Stop carries an id the origin never saw.
        self.prompt("build it", "u1")
        self.write_rows({"type": "user", "promptId": "u2", "promptSource": "queued",
                         "origin": {"kind": "human"}, "message": {"role": "user", "content": "and docs"}})
        self.bg(True)
        self.stop("u2")
        self.assertEqual((self.opt("@agent_turn_origin"), self.opt("@agent_turn_result")), ("user u1 -", "u2 loud"))
        self.prompt(self.deliver("ping"), "a1")
        self.assertEqual(self.opt("@agent_prior_state"), "done")

    def test_split_loud_verdict_fallback_without_bg_owner(self):
        # A window marked before @agent_bg_owner existed.
        self.prompt("build it", "u1")
        self.set_opt("@agent_turn_result", "u2 loud")
        self.prompt(self.deliver("ping"), "a1")
        self.assertEqual(self.opt("@agent_prior_state"), "done")

    def test_stop_failure_in_an_agent_turn_is_red(self):
        self.seed_done()
        self.prompt(self.deliver("go"), "p1")
        self.hook("failed", "claude", payload={"session_id": "s1", "prompt_id": "p1",
                                              "error": "overloaded", "hook_event_name": "StopFailure"})
        self.assertEqual(self.opt("@agent_state"), "failed")
        self.assertEqual(self.opt("@agent_turn_result"), "p1 loud")
        self.assertFalse(self.has("@agent_prior_state"))

    def test_unknown_origin_is_loud(self):
        self.stop("p9")   # no UserPromptSubmit ever recorded an origin
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertEqual(self.opt("@agent_turn_result"), "p9 loud")

    # --- seen-it on the saved prior ---

    def test_clear_current_downgrades_the_saved_prior(self):
        self.seed_done()
        self.prompt(self.deliver("go"), "p1")
        self.clear_current()
        self.assertEqual(self.opt("@agent_state"), "running")         # the turn itself untouched
        self.assertEqual(self.opt("@agent_prior_state"), "idle")
        self.assertRegex(self.opt("@agent_seen_at"), r"^\d+$")
        self.stop("p1")
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assertEqual(self.opt("@agent_turn_result"), "p1 quiet idle")

    def test_watched_permission_prompt_downgrades_the_prior(self):
        self.seed_done()
        self.prompt(self.deliver("go"), "p1")
        self.hook("needs-approval", "claude", payload={"session_id": "s1", "tool_name": "Bash",
                                                      "tool_input": {"command": "ls"}}, **self.VIEWING)
        self.assertEqual(self.opt("@agent_prior_state"), "idle")

    # --- unseen prompt ids: Claude's split case and harness wake-ups ---

    def write_rows(self, *rows):
        with self.transcript.open("a") as f:
            for r in rows:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")

    def test_split_case_queued_agent_message_is_quiet_and_leaves_the_state(self):
        self.prompt("build it", "p1")
        text = self.deliver("status?")
        self.prompt(text, "p1")                       # mid-turn, recorded as seen
        self.stop("p1")
        self.assertEqual(self.opt("@agent_state"), "done")
        self.write_rows({"type": "user", "promptId": "p2", "promptSource": "queued",
                         "origin": {"kind": "human"}, "message": {"role": "user", "content": text}},
                        {"type": "user", "promptId": "p2", "message": {"role": "user", "content": [
                            {"type": "tool_result", "tool_use_id": "t", "content": "x"}]}})
        self.stop("p2", msg="Status: green")
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertEqual(self.opt("@agent_turn_result"), "p2 quiet done")

    def test_split_case_for_user_message_is_loud(self):
        self.prompt("build it", "p1")
        text = self.deliver("the answer", fr="user")
        self.prompt(text, "p1")
        self.stop("p1")
        self.write_rows({"type": "user", "promptId": "p2", "promptSource": "queued",
                         "origin": {"kind": "human"}, "message": {"role": "user", "content": text}})
        self.stop("p2")
        self.assertEqual(self.opt("@agent_turn_result"), "p2 loud")

    def test_split_case_typed_prompt_is_loud(self):
        self.prompt("build it", "p1")
        self.stop("p1")
        self.write_rows({"type": "user", "promptId": "p2", "promptSource": "queued",
                         "origin": {"kind": "human"}, "message": {"role": "user", "content": "and docs"}})
        self.stop("p2")
        self.assertEqual(self.opt("@agent_turn_result"), "p2 loud")

    def test_wakeup_without_hook_inherits_a_quiet_origin(self):
        self.seed_done()
        self.prompt(self.deliver("run the tests in the background"), "p1")
        self.write_rows({"type": "user", "promptId": "p2", "promptSource": "system",
                         "origin": {"kind": "task-notification"},
                         "message": {"role": "user", "content": "<task-notification>\n<task-id>b1</task-id>"}})
        self.stop("p2")
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertEqual(self.opt("@agent_turn_result"), "p2 quiet done")

    def test_task_notification_prompt_inherits_origin_and_keeps_the_prior(self):
        since = self.seed_done()
        self.prompt(self.deliver("go"), "p1")
        out = self.prompt("<task-notification>\n<task-id>b1</task-id>\n</task-notification>", "p2")
        self.assertEqual(out, "")
        self.assertEqual(self.opt("@agent_turn_origin"), "agent p2 peer")
        self.assertEqual(self.opt("@agent_prior_state"), "done")
        self.stop("p2")
        self.assertEqual((self.opt("@agent_state"), self.opt("@agent_since")), ("done", since))
        self.assertEqual(self.opt("@agent_turn_result"), "p2 quiet done")
        # ...and after Mack's own turn, the same wake-up is his: loud.
        self.prompt("next thing", "p3")
        self.prompt("<task-notification>\n<task-id>b2</task-id>", "p4")
        self.assertEqual(self.opt("@agent_turn_origin"), "user p4 -")
        self.stop("p4")
        self.assertEqual(self.opt("@agent_turn_result"), "p4 loud")

    def test_bg_held_quiet_stop_keeps_running_and_the_prior(self):
        write_exe(self.home / ".local/bin/agent-bg-pending", "#!/bin/sh\nexit 0\n")
        self.seed_done()
        self.prompt(self.deliver("kick off the build"), "p1")
        self.stop("p1")
        self.assertEqual(self.opt("@agent_state"), "running")
        self.assertEqual(self.opt("@agent_prior_state"), "done")
        self.assertEqual(self.opt("@agent_turn_result"), "p1 quiet running")

    # --- other agents ---

    def test_codex_payload_shape(self):
        owners = self.root / "owners.json"
        owners.write_text(json.dumps({"cx1": {"status": "bound", "session_id": "cx1",
            "tmux_socket": self.socket, "pane": self.pane, "token": "tok", "binding_id": "b-cx1",
            "frontend_pid": 1, "frontend_start": "x"}}))
        write_exe(self.home / ".local/bin/codex-terminal-owner", FAKE_OWNER)
        env = dict(FAKE_OWNER_STATE=str(owners), FAKE_FRONTEND="1", TMUX="/stale,1,0", TMUX_PANE="%999")
        # SessionStart binds the window to the thread first (a first hook from
        # a new thread resets the window, bookkeeping included).
        self.hook("idle", "codex", payload={"session_id": "cx1", "cwd": str(self.root),
                                            "source": "startup"}, **env)
        text = self.deliver("codex, take a look")
        p = {"session_id": "cx1", "turn_id": "turn-1", "cwd": str(self.root),
             "hook_event_name": "UserPromptSubmit", "prompt": text}
        out = self.hook("running", "codex", payload=p, **env).stdout
        self.assertTrue(self.ctx(out).startswith(CTX_START + "peer"))
        self.assertEqual(self.opt("@agent_turn_origin"), "agent turn-1 peer")
        self.hook("done", "codex", payload={"session_id": "cx1", "turn_id": "turn-1", "cwd": str(self.root),
                                           "hook_event_name": "Stop", "last_assistant_message": "ok"}, **env)
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assertEqual(self.opt("@agent_turn_result"), "turn-1 quiet idle")
        # The memory agent (cwd ~/.codex/memories) never touches any of it.
        self.deliver("x")
        before = self.opt("@agent_inbound")
        mem = dict(p, cwd=str(self.home / ".codex/memories"), turn_id="turn-2")
        self.assertEqual(self.hook("running", "codex", payload=mem, **env).stdout, "")
        self.assertEqual(self.opt("@agent_turn_origin"), "agent turn-1 peer")
        self.assertEqual(self.opt("@agent_inbound"), before)

    def test_opencode_without_turn_ids(self):
        self.set_opt("@agent_state", "idle")
        out = self.prompt(self.deliver("hi"), None, agent="opencode")
        self.assertTrue(out.strip())
        self.assertEqual(self.opt("@agent_turn_origin"), "agent - peer")
        self.stop(None, agent="opencode")
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assertEqual(self.opt("@agent_turn_result"), "- quiet idle")
        # Next prompt is a new turn (the window is not mid-turn): Mack's.
        self.assertEqual(self.prompt("now me", None, agent="opencode"), "")
        self.assertEqual(self.opt("@agent_turn_origin"), "user - -")

    def test_session_end_drops_the_bookkeeping(self):
        self.prompt(self.deliver("go"), "p1")
        self.deliver("another")
        self.hook("clear", "claude", payload={"session_id": "s1", "reason": "other"})
        for name in ("@agent_turn_origin", "@agent_prior_state", "@agent_inbound"):
            self.assertFalse(self.has(name), name)


class WatcherOriginTests(unittest.TestCase):
    """The watcher ends an agent-originated turn by restoring its prior."""
    SID = "11111111-aaaa-bbbb-cccc-000000000001"

    def setUp(self):
        self.f = jw.FakeEnv(windows={}, panes=[])
        self.addCleanup(self.f.close)
        self.f.procs.write_text("ttys902 4242 claude\n")
        sessions = self.f.home / ".claude/sessions"; sessions.mkdir(parents=True)
        (sessions / "4242.json").write_text(json.dumps(
            {"pid": 4242, "sessionId": self.SID, "cwd": "/Users/x/proj", "status": "shell"},
            separators=(",", ":")))
        tp = self.f.home / ".claude/projects/-Users-x-proj" / (self.SID + ".jsonl")
        tp.parent.mkdir(parents=True); tp.write_text('{"type":"user"}\n')
        self.stub = self.f.home / ".local/bin/agent-bg-pending"
        self.stub.parent.mkdir(parents=True)
        write_exe(self.stub, "#!/bin/sh\nexit \"${FAKE_BG_RC:-1}\"\n")
        write_exe(self.f.bin / "lsappinfo", FAKE_LSAPPINFO)
        now = int(time.time())
        self.prior_since = "%d done" % (now - 600)
        w = jw.W("main", 1, **{"@agent_state": "running", "@agent_kind": "claude",
                               "@agent_since": "%d running" % (now - 30),
                               "@agent_detail_kind": "done", "@agent_detail": "agent turn reply",
                               "@agent_prior_state": "done", "@agent_prior_since": self.prior_since,
                               "@agent_prior_detail_kind": "done", "@agent_prior_detail": "Mack's result"})
        st = self.f.read()
        st["windows"] = {"@1": w}
        st["panes"] = [{"window": "@1", "tty": "/dev/ttys902"}]
        self.f.state.write_text(json.dumps(st))

    def ticks(self, n, **env):
        r = self.f.run("agent-tab-watcher.sh", timeout=10 + 2 * n, AGENT_TAB_WATCHER_MAX_TICKS=str(n), **env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.f.read()["windows"]["@1"]["opts"]

    def set_clients(self, n):
        st = self.f.read(); st["windows"]["@1"]["active_clients"] = n
        self.f.state.write_text(json.dumps(st))

    def test_bg_drain_restores_the_prior_instead_of_idle(self):
        o = self.ticks(5, FAKE_BG_RC="1")
        self.assertEqual(o["@agent_state"], "done")
        self.assertEqual(o["@agent_since"], self.prior_since)
        self.assertEqual((o["@agent_detail_kind"], o["@agent_detail"]), ("done", "Mack's result"))
        for name in ("@agent_prior_state", "@agent_prior_since", "@agent_prior_detail_kind",
                     "@agent_prior_detail"):
            self.assertNotIn(name, o)

    def test_bg_drain_clears_the_bg_owner(self):
        for prior in (True, False):
            with self.subTest(prior=prior):
                self.setUp()
                opts = {"@agent_bg_owner": "user"}
                st = self.f.read(); w = st["windows"]["@1"]["opts"]; w.update(opts)
                if not prior:
                    for k in ("@agent_prior_state", "@agent_prior_since", "@agent_prior_detail_kind",
                              "@agent_prior_detail"):
                        w.pop(k)
                self.f.state.write_text(json.dumps(st))
                o = self.ticks(5, FAKE_BG_RC="1")
                self.assertEqual(o["@agent_state"], "done" if prior else "idle")
                self.assertNotIn("@agent_bg_owner", o)

    def test_bg_still_out_holds_running_and_keeps_the_prior(self):
        o = self.ticks(4, FAKE_BG_RC="0")
        self.assertEqual(o["@agent_state"], "running")
        self.assertEqual(o["@agent_prior_state"], "done")

    def test_viewed_while_running_downgrades_the_prior_then_restores_idle(self):
        self.set_clients(1)
        o = self.ticks(2, FAKE_BG_RC="0", FAKE_FRONT="wez")
        self.assertEqual(o["@agent_state"], "running")
        self.assertEqual(o["@agent_prior_state"], "idle")
        self.assertIn("@agent_seen_at", o)
        self.set_clients(0)
        o = self.ticks(5, FAKE_BG_RC="1")
        self.assertEqual(o["@agent_state"], "idle")
        self.assertNotIn("@agent_prior_state", o)

    def test_restore_while_viewed_lands_idle(self):
        self.set_clients(1)
        st = self.f.read(); st["windows"]["@1"]["opts"]["@agent_prior_state"] = "failed"
        st["windows"]["@1"]["opts"]["@agent_prior_since"] = "100 failed"
        self.f.state.write_text(json.dumps(st))
        o = self.ticks(5, FAKE_BG_RC="1", FAKE_FRONT="wez")
        self.assertEqual(o["@agent_state"], "failed")        # red is not discharged by watching
        self.assertEqual(o["@agent_since"], "100 failed")

    def set_opts(self, **opts):
        st = self.f.read(); st["windows"]["@1"]["opts"].update(opts)
        self.f.state.write_text(json.dumps(st))

    def events(self):
        log = Path(str(self.f.tmp) + "/agent-events.%d.log" % os.getuid())
        return [l.split("\x1f") for l in log.read_text().splitlines()] if log.exists() else []

    def test_quiet_restore_is_logged_as_a_reply(self):
        self.set_opts(**{"@agent_turn_origin": "agent a1 peer"})
        o = self.ticks(5, FAKE_BG_RC="1")
        self.assertEqual(o["@agent_state"], "done")
        self.assertEqual((o["@agent_detail_kind"], o["@agent_detail"]), ("done", "Mack's result"))
        self.assertNotIn("@agent_quiet", o)                   # consumed by the tick that logged it
        ev = [e for e in self.events() if e[4] == "done"]
        self.assertEqual(len(ev), 1, self.events())
        self.assertEqual((ev[0][5], ev[0][6], ev[0][8]), ("running", "quiet", "↩ replied to @peer"))

    def test_quiet_restore_to_idle_is_logged_too(self):
        self.set_opts(**{"@agent_prior_state": "idle", "@agent_prior_since": "5 idle",
                         "@agent_turn_origin": "agent a1 peer"})
        self.ticks(5, FAKE_BG_RC="1")
        ev = [e for e in self.events() if e[4] == "idle"]
        self.assertEqual((ev[-1][6], ev[-1][8]), ("quiet", "↩ replied to @peer"))

    def test_hook_restore_mark_is_logged_and_consumed(self):
        # The indicator's quiet Stop restored between two ticks.
        env = self.f.env(AGENT_TAB_WATCHER_MAX_TICKS="4", FAKE_BG_RC="0")
        p = subprocess.Popen(["bash", str(self.f.scripts / "agent-tab-watcher.sh")], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 10
        while not any(c[0] == "list-panes" for c in self.f.read()["calls"]) and time.monotonic() < deadline:
            time.sleep(.05)
        time.sleep(.3)                                       # the baseline tick: running, held
        import fcntl
        with self.f.state.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            st = self.f.read()
            w = st["windows"]["@1"]["opts"]
            for k in ("@agent_prior_state", "@agent_prior_since", "@agent_prior_detail_kind",
                      "@agent_prior_detail"):
                w.pop(k)
            w.update({"@agent_state": "done", "@agent_since": self.prior_since, "@agent_quiet": "peer"})
            self.f.state.write_text(json.dumps(st))
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        o = self.f.read()["windows"]["@1"]["opts"]
        self.assertNotIn("@agent_quiet", o)
        last = self.events()[-1]
        self.assertEqual((last[4], last[6], last[8]), ("done", "quiet", "↩ replied to @peer"))

    def test_prior_gone_since_the_read_writes_nothing(self):
        # The indicator's quiet Stop restores between the tick's read and
        # the restore's own re-read: the watcher must not write idle over it.
        real = self.f.bin / "tmux.real"
        (self.f.bin / "tmux").rename(real)
        write_exe(self.f.bin / "tmux", """#!/bin/sh
case "$*" in
  *display-message*@agent_prior_since*)
    "%s" set-option -uw -t @1 @agent_prior_state
    "%s" set-option -w -t @1 @agent_state done ;;
esac
exec "%s" "$@"
""" % (real, real, real))
        o = self.ticks(4, FAKE_BG_RC="1")
        self.assertEqual(o["@agent_state"], "done")

    def test_gc_drops_the_bookkeeping(self):
        st = self.f.read(); st["panes"] = [{"window": "@1", "tty": "/dev/ttys903"}]   # agent gone
        st["windows"]["@1"]["opts"]["@agent_inbound"] = "1 a b agent"
        self.f.state.write_text(json.dumps(st))
        o = self.ticks(6)
        for name in ("@agent_state", "@agent_prior_state", "@agent_inbound"):
            self.assertNotIn(name, o)


if __name__ == "__main__":
    unittest.main()
