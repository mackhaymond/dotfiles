"""ASKS WHILE WORK IS OUT: agent-tab-indicator.sh's Stop asks agent-turn-asks
whether a turn held `running` by background work ended ASKING Mack, and
raises needs-input (detail kind ask, @agent_ask_bg 1) when it did.

The contract (cua-notch 547f0e7, the orchestrator's pins):
  - a synchronous `agent-turn-asks --marker-only --since @agent_turn_start`,
    SIGTERMed at 300 ms; exit 0 raises at once;
  - else ONE detached full run, whose exit 0 raises only while the window is
    still `running` with the same turn bookkeeping;
  - a look at a raised ask while the work is still out (@agent_workflow) is
    ONE command list: @agent_seen_at E, @agent_state running, @agent_since
    "E running", @agent_detail_kind and @agent_ask_bg unset, the shown line
    kept as @agent_ask_seen; with nothing out, the ordinary idle discharge;
  - a later Stop whose line equals @agent_ask_seen raises nothing; a real
    prompt from Mack (not a harness wake-up, not a peer) and a non-compact
    SessionStart unset @agent_ask_seen.

Drives the real script against a private tmux server (the harness of
test_agent_ask_from / test_agent_state_notice). agent-bg-pending and
agent-turn-asks are fakes in the fake $HOME/.local/bin; the fake decider's
behaviour is baked into its file (the detached run is started by the tmux
server, whose environment is not the test's).
"""
import json
import os
from pathlib import Path
import subprocess
import time
import unittest

import test_agent_ask_from as af
import test_agent_state_notice as sn

payload = af.payload

FAKE_ASKS = r"""#!/bin/bash
log=%(log)s
echo "argv $*" >> "$log"
trap 'echo term >> "$log"; exit 2' TERM
cat > /dev/null
case " $* " in
    *" --marker-only "*) sleep %(m_sleep)s & wait $!; line=%(m_line)s; rc=%(m_rc)s ;;
    *) echo full >> "$log"; sleep %(f_sleep)s & wait $!; line=%(f_line)s; rc=%(f_rc)s ;;
esac
[ -z "$line" ] || printf '%%s\n' "$line"
exit "$rc"
"""

# The private server's tmux, logging every argv (one line per invocation,
# US between arguments) so a test can see what landed in ONE command list.
LOGGING_TMUX = """#!/bin/sh
case "$*" in
    *'#{window_active_clients}'*)
        if [ -n "${FAKE_ACTIVE_CLIENTS:-}" ]; then echo "$FAKE_ACTIVE_CLIENTS"; exit 0; fi ;;
esac
if [ -n "${TMUX_ARGV_LOG:-}" ]; then
    (IFS=$(printf '\\037'); printf '%%s\\n' "$*") >> "$TMUX_ARGV_LOG"
fi
exec %s "$@"
"""


@unittest.skipUnless(sn.REAL_TMUX, "tmux executable required")
class AsksWhileWorkIsOutTests(af.AskerGateTests):
    for _n in [n for n in dir(af.AskerGateTests) if n.startswith("test_")]:
        locals()[_n] = None
    del _n

    def setUp(self):
        super().setUp()
        self.lbin = self.home / ".local/bin"
        self.lbin.mkdir(parents=True)
        self.asks_log = self.root / "asks.log"
        self.bg_pending(True)
        self.decider()
        self.argv_log = self.root / "tmux-argv.log"
        sn.write_exe(self.bin / "tmux", LOGGING_TMUX % sn.REAL_TMUX)

    # --- fakes and helpers ---

    def bg_pending(self, out):
        sn.write_exe(self.lbin / "agent-bg-pending", "#!/bin/sh\nexit %d\n" % (0 if out else 1))

    def decider(self, m_rc=2, m_line="", m_sleep=0, f_rc=1, f_line="", f_sleep=0):
        q = lambda s: "'" + s.replace("'", "'\\''") + "'"
        sn.write_exe(self.lbin / "agent-turn-asks", FAKE_ASKS % dict(
            log=q(str(self.asks_log)), m_rc=m_rc, m_line=q(m_line), m_sleep=m_sleep,
            f_rc=f_rc, f_line=q(f_line), f_sleep=f_sleep))

    def calls(self):
        return self.asks_log.read_text().splitlines() if self.asks_log.exists() else []

    def stop(self, msg="Three reviewers are still out.", **kw):
        kw.setdefault("prompt_id", "p1")
        t = time.monotonic()
        self.raw("done", payload("Stop", last_assistant_message=msg, **kw))
        return time.monotonic() - t

    def wait_for(self, pred, secs=10):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            if pred():
                return True
            time.sleep(0.1)
        return pred()

    def raised(self, line):
        self.assertEqual(self.state(), "needs-input")
        self.assertEqual((self.opt("@agent_detail_kind"), self.opt("@agent_detail")), ("ask", line))
        self.assertEqual(self.opt("@agent_ask_bg"), "1")
        self.assertEqual(self.opt("@agent_ask_from"), "main")

    def not_raised(self):
        self.assertEqual(self.state(), "running")
        self.assertFalse(self.has_opt("@agent_ask_bg"))
        self.assertNotEqual(self.opt("@agent_detail_kind"), "ask")

    def raise_by_marker(self, line="pick (A) in-memory or (B) on-disk"):
        self.start_turn()
        self.decider(m_rc=0, m_line=line)
        self.stop()
        self.raised(line)

    # --- the marker (synchronous) ---

    def test_marker_hit_raises_needs_input(self):
        self.start_turn()
        start = self.opt("@agent_turn_start")
        self.assertRegex(start, r"^\d+$")
        self.decider(m_rc=0, m_line="the new staging connection string")
        self.stop()
        self.raised("the new staging connection string")
        # Held like background work: a loud turn, Mack's work out.
        self.assertTrue(self.opt("@agent_turn_result").endswith(" loud"))
        self.assertEqual(self.opt("@agent_bg_owner"), "user")
        self.assertEqual(self.calls(), ["argv --marker-only --since " + start])
        # A background subagent's tool call never re-arms over it.
        self.raw("heartbeat", af.post_tool_use("a4f9c2e1d0b3"))
        self.assertEqual(self.state(), "needs-input")
        # No full run after a marker hit.
        time.sleep(0.5)
        self.assertNotIn("full", self.calls())

    def test_marker_line_is_sanitised(self):
        self.start_turn()
        self.decider(m_rc=0, m_line='pick #1 or "two"; ')
        self.stop()
        self.raised("pick 1 or two")

    def test_no_background_work_never_asks(self):
        self.bg_pending(False)
        self.start_turn()
        self.decider(m_rc=0, m_line="anything")
        self.stop()
        self.assertEqual(self.state(), "done")
        self.assertEqual(self.calls(), [])

    def test_marker_makes_an_agent_started_turn_loud(self):
        self.start_turn()
        self.set_opt("@agent_turn_origin", "agent p9 peer")
        self.set_opt("@agent_prior_state", "idle")
        self.decider(m_rc=0, m_line="approve the deploy")
        self.stop(prompt_id="p9")
        self.raised("approve the deploy")
        self.assertEqual(self.opt("@agent_turn_result"), "p9 loud")
        self.assertFalse(self.has_opt("@agent_prior_state"))

    def test_quiet_turn_gets_no_model_run(self):
        self.start_turn()
        self.set_opt("@agent_turn_origin", "agent p9 peer")
        self.decider(m_rc=2, f_rc=0, f_line="a question for the peer")
        self.stop(prompt_id="p9")
        time.sleep(1.0)
        self.not_raised()
        self.assertEqual(self.calls(), [c for c in self.calls() if c.startswith("argv --marker-only")])

    def test_viewed_marker_stays_running_and_is_seen(self):
        self.start_turn()
        self.decider(m_rc=0, m_line="which branch")
        env = dict(self.env, FAKE_FRONT="wez", FAKE_ACTIVE_CLIENTS="1")
        r = subprocess.run(["bash", str(self.script), "done", "claude"], text=True, capture_output=True,
                           input=payload("Stop", last_assistant_message="m", prompt_id="p1"), env=env, timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.not_raised()
        self.assertEqual(self.opt("@agent_ask_seen"), "which branch")
        sn.IndicatorNoticeTests.assert_recent_epoch(self, self.opt("@agent_seen_at"))

    # --- the budget ---

    def test_a_slow_marker_call_is_stopped_at_the_budget(self):
        self.start_turn()
        self.stop()                      # warm-up: the same path, a fast decider
        fast = self.stop()
        self.decider(m_rc=0, m_line="too late", m_sleep=5)
        slow = self.stop()
        self.assertLess(slow, fast + 0.8, (fast, slow))
        self.assertLess(slow, 3.0)
        self.assertTrue(self.wait_for(lambda: "term" in self.calls(), 2), self.calls())
        self.not_raised()

    # --- the detached full run ---

    def test_no_marker_full_run_raises_while_the_turn_is_unchanged(self):
        self.start_turn()
        self.decider(m_rc=2, f_rc=0, f_line="run sudo make install", f_sleep=0.5)
        self.stop()
        self.assertEqual(self.state(), "running")
        self.assertTrue(self.wait_for(lambda: self.state() == "needs-input"), self.calls())
        self.raised("run sudo make install")
        # One full run, with no --marker-only (and the turn's --since).
        self.assertEqual(self.calls().count("full"), 1)
        self.assertEqual([c for c in self.calls() if c.startswith("argv") and "--marker-only" not in c],
                         ["argv --since " + self.opt("@agent_turn_start")])

    def test_full_run_saying_no_writes_nothing(self):
        self.start_turn()
        self.decider(m_rc=2, f_rc=1)
        self.stop()
        self.assertTrue(self.wait_for(lambda: "full" in self.calls(), 5))
        time.sleep(0.8)
        self.not_raised()

    def test_a_new_prompt_discards_the_full_runs_answer(self):
        self.start_turn()
        self.decider(m_rc=2, f_rc=0, f_line="late answer", f_sleep=1.5)
        self.stop()
        self.assertTrue(self.wait_for(lambda: "full" in self.calls(), 5))
        self.raw("running", payload("UserPromptSubmit", prompt="next thing", prompt_id="p2"))
        time.sleep(2.5)
        self.not_raised()

    def test_a_woken_turn_discards_it_too(self):
        self.start_turn()
        self.decider(m_rc=2, f_rc=0, f_line="late answer", f_sleep=1.5)
        self.stop()
        self.raw("running", payload("UserPromptSubmit", prompt="<task-notification>done</task-notification>",
                                    prompt_id="p3"))
        time.sleep(2.5)
        self.not_raised()

    def test_a_window_no_longer_running_is_left_alone(self):
        self.start_turn()
        self.decider(m_rc=2, f_rc=0, f_line="late answer", f_sleep=1.0)
        self.stop()
        self.set_opt("@agent_state", "idle")
        time.sleep(2.0)
        self.assertEqual(self.state(), "idle")
        self.assertFalse(self.has_opt("@agent_ask_bg"))

    def test_full_run_is_stopped_with_sigterm_at_its_bound(self):
        self.start_turn()
        self.decider(m_rc=2, f_rc=0, f_line="never", f_sleep=10)
        self.env["AGENT_TAB_ASK_BG_TIMEOUT"] = "1"
        self.stop()
        self.assertTrue(self.wait_for(lambda: "term" in self.calls(), 6), self.calls())
        self.not_raised()

    # --- a look ---

    def look_list(self):
        return [l.split("\x1f") for l in self.argv_log.read_text().splitlines()] if self.argv_log.exists() else []

    def test_look_while_work_is_out_goes_running_in_one_command_list(self):
        self.raise_by_marker("pick A or B")
        self.set_opt("@agent_workflow", "1")
        self.env["TMUX_ARGV_LOG"] = str(self.argv_log)
        self.clear_current()
        self.assertEqual(self.state(), "running")
        seen = self.opt("@agent_seen_at")
        sn.IndicatorNoticeTests.assert_recent_epoch(self, seen)
        self.assertEqual(self.opt("@agent_since"), seen + " running")
        self.assertFalse(self.has_opt("@agent_detail_kind"))
        self.assertFalse(self.has_opt("@agent_ask_bg"))
        self.assertFalse(self.has_opt("@agent_ask_from"))
        self.assertFalse(self.has_opt("@agent_pending"))
        self.assertEqual(self.opt("@agent_ask_seen"), "pick A or B")
        self.assertEqual(self.opt("@agent_detail"), "pick A or B")   # may stay
        # ONE tmux invocation carries all of it.
        writes = [a for a in self.look_list() if "@agent_state" in a and "set-option" in a]
        self.assertEqual(len(writes), 1, writes)
        w = writes[0]
        for must in (["@agent_state", "running"], ["@agent_since", seen + " running"],
                     ["@agent_seen_at", seen], ["-uw", "-t", self.win, "@agent_detail_kind"],
                     ["-uw", "-t", self.win, "@agent_ask_bg"], ["-F", "@agent_ask_seen", "#{@agent_detail}"]):
            self.assertTrue(any(w[i:i + len(must)] == must for i in range(len(w))), (must, w))

    def test_look_with_nothing_out_is_the_ordinary_discharge(self):
        self.raise_by_marker("pick A or B")
        self.clear_current()
        self.assertEqual(self.state(), "idle")
        sn.IndicatorNoticeTests.assert_recent_epoch(self, self.opt("@agent_pending"))
        sn.IndicatorNoticeTests.assert_recent_epoch(self, self.opt("@agent_seen_at"))
        self.assertFalse(self.has_opt("@agent_ask_bg"))
        self.assertEqual(self.opt("@agent_ask_seen"), "pick A or B")

    def test_a_plain_gate_look_is_unchanged(self):
        self.start_turn()
        self.raw("needs-approval", af.ask())
        self.set_opt("@agent_workflow", "1")
        self.clear_current()
        self.assertEqual(self.state(), "idle")
        self.assertFalse(self.has_opt("@agent_ask_seen"))

    def test_leaving_needs_input_any_way_drops_ask_bg(self):
        for leave in (lambda: self.raw("running", payload("UserPromptSubmit", prompt="answer", prompt_id="p2")),
                      lambda: self.raw("failed", payload("StopFailure", error="overloaded")),
                      lambda: self.raw("heartbeat", af.post_tool_use()),
                      lambda: self.raw("clear", payload("SessionEnd", reason="other"))):
            self.raise_by_marker()
            leave()
            self.assertNotEqual(self.state(), "needs-input")
            self.assertFalse(self.has_opt("@agent_ask_bg"))

    # --- seen once, not re-pinged ---

    def test_the_same_line_is_not_raised_again(self):
        self.raise_by_marker("pick A or B")
        self.set_opt("@agent_workflow", "1")
        self.clear_current()
        self.assertEqual(self.opt("@agent_ask_seen"), "pick A or B")
        # The work reports back: a woken turn, its Stop with the marker standing.
        self.raw("running", payload("UserPromptSubmit", prompt="<task-notification>x</task-notification>",
                                    prompt_id="p2"))
        self.assertEqual(self.opt("@agent_ask_seen"), "pick A or B")   # not Mack replying
        self.stop(prompt_id="p2")
        self.not_raised()
        self.assertNotIn("full", self.calls())
        # Same text after the sanitiser (a quote, extra spaces): still seen.
        self.decider(m_rc=0, m_line='pick  "A" or B ')
        self.stop(prompt_id="p2")
        self.not_raised()
        # A different question raises.
        self.decider(m_rc=0, m_line="now pick C or D")
        self.stop(prompt_id="p2")
        self.raised("now pick C or D")

    def test_the_full_run_respects_the_seen_line(self):
        self.raise_by_marker("pick A or B")
        self.set_opt("@agent_workflow", "1")
        self.clear_current()
        self.decider(m_rc=2, f_rc=0, f_line="pick A or B", f_sleep=0.2)
        self.stop()
        self.assertTrue(self.wait_for(lambda: "full" in self.calls(), 5))
        time.sleep(1.0)
        self.not_raised()

    def test_mack_replying_or_a_new_session_forgets_the_seen_line(self):
        for forget, gone in (
                (lambda: self.raw("running", payload("UserPromptSubmit", prompt="A", prompt_id="p5")), True),
                (lambda: self.raw("idle", payload("SessionStart", source="resume")), True),
                (lambda: self.raw("idle", payload("SessionStart", source="compact")), False),
                (lambda: self.raw("running", payload("UserPromptSubmit", prompt="[message from @peer] hi",
                                                     prompt_id="p6")), True)):
            self.set_opt("@agent_ask_seen", "pick A or B")
            forget()
            self.assertEqual(not self.has_opt("@agent_ask_seen"), gone)

    def test_a_verified_peer_message_is_not_mack_replying(self):
        self.start_turn()
        self.set_opt("@agent_ask_seen", "pick A or B")
        body = "[message from @peer] status?"
        import hashlib
        sha = hashlib.sha1(body.encode()).hexdigest()
        self.set_opt("@agent_inbound", "%d %s peer agent" % (int(time.time()), sha))
        self.raw("running", payload("UserPromptSubmit", prompt=body, prompt_id="p7"))
        self.assertEqual(self.opt("@agent_ask_seen"), "pick A or B")

    # --- review round 1 ---

    def test_a_subagents_gate_replaces_a_raised_ask(self):
        self.raise_by_marker("pick A or B")
        self.raw("needs-approval", af.perm("aaaa1111", command="make deploy"))
        self.assertEqual(self.state(), "needs-input")
        self.assertFalse(self.has_opt("@agent_ask_bg"))
        self.assertEqual(self.opt("@agent_ask_from"), "aaaa1111")
        self.assertEqual(self.opt("@agent_detail_kind"), "perm")
        # Its look is a gate's: idle + pending, no ask seen.
        self.set_opt("@agent_workflow", "1")
        self.clear_current()
        self.assertEqual(self.state(), "idle")
        self.assertFalse(self.has_opt("@agent_ask_seen"))

    def test_the_notification_twin_alone_replaces_it_too(self):
        self.raise_by_marker("pick A or B")
        self.raw("needs-approval", af.notification())
        self.assertFalse(self.has_opt("@agent_ask_bg"))
        self.assertEqual(self.opt("@agent_detail_kind"), "perm")
        self.assertFalse(self.has_opt("@agent_ask_from"))   # no asker known yet

    def test_a_look_copies_only_an_ask(self):
        self.raise_by_marker("pick A or B")
        self.set_opt("@agent_detail_kind", "perm")   # not an ask any more
        self.set_opt("@agent_workflow", "1")
        self.clear_current()
        self.assertEqual(self.state(), "running")
        self.assertFalse(self.has_opt("@agent_ask_seen"))

    def test_a_look_takes_the_counted_form_too(self):
        self.raise_by_marker("pick A or B")
        self.set_opt("@agent_ask_bg", "1 3")
        self.set_opt("@agent_workflow", "1")
        self.clear_current()
        self.assertEqual(self.state(), "running")
        self.assertFalse(self.has_opt("@agent_ask_bg"))

    def worker(self, tok, pf=None, **env):
        if pf is None:
            pf = self.root / "payload.json"
            pf.write_text(payload("Stop", last_assistant_message="m", prompt_id="p1"))
        # Real tmux answers #{window_active_clients} (0: no client) unless
        # a test fakes a viewer, as the detached worker sees it.
        env.setdefault("FAKE_ACTIVE_CLIENTS", "")
        r = subprocess.run(["bash", str(self.script), "ask-bg", self.win, tok, str(pf), "claude"],
                           text=True, capture_output=True, env=dict(self.env, **env), timeout=15)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_full_runs_write_is_guarded_in_tmux(self):
        # The window leaves `running` between the worker's look and its
        # write: a guarded write lands nothing over it.
        self.start_turn()
        self.decider(m_rc=2, f_rc=0, f_line="late answer", f_sleep=0.6)
        armed = self.root / "race.armed"
        raced = self.root / "race.done"
        sn.write_exe(self.bin / "tmux", (
            "#!/bin/sh\n"
            "if [ -f %(armed)s ] && [ ! -f %(raced)s ]; then\n"
            "  case \"$*\" in *needs-input*) : > %(raced)s; %(real)s set-option -w -t %(win)s @agent_state idle ;; esac\n"
            "fi\n" % dict(armed=armed, raced=raced, real=sn.REAL_TMUX, win=self.win))
            + LOGGING_TMUX.split("\n", 1)[1] % sn.REAL_TMUX)
        self.stop()
        armed.touch()
        self.assertTrue(self.wait_for(lambda: raced.exists(), 6), self.calls())
        time.sleep(0.8)
        self.assertEqual(self.state(), "idle")
        self.assertFalse(self.has_opt("@agent_ask_bg"))
        self.assertFalse(self.has_opt("@agent_ask_line"))

    def test_an_unsafe_token_writes_nothing(self):
        self.start_turn()
        self.set_opt("@agent_turn_origin", "user p1 a,b}")
        self.set_opt("@agent_turn_result", "p1 loud")
        self.decider(f_rc=0, f_line="anything")
        self.worker("user p1 a,b}|p1 loud")
        self.assertEqual(self.state(), "running")
        self.assertFalse(self.has_opt("@agent_ask_bg"))

    def test_the_worker_raises_with_a_matching_token(self):
        self.start_turn()
        self.set_opt("@agent_turn_result", "p1 loud")
        tok = self.opt("@agent_turn_origin") + "|p1 loud"
        self.decider(f_rc=0, f_line='pick "A"; ')
        self.worker(tok)
        self.raised("pick A")
        self.assertFalse(self.has_opt("@agent_ask_line"))
        self.assertRegex(self.opt("@agent_since"), r"^\d+ needs-input$")
        self.assertEqual(self.opt("@agent_kind"), "claude")

    def test_the_worker_seen_branch_stamps_seen_at(self):
        self.start_turn()
        self.set_opt("@agent_turn_result", "p1 loud")
        tok = self.opt("@agent_turn_origin") + "|p1 loud"
        self.decider(f_rc=0, f_line="which branch")
        self.worker(tok, FAKE_FRONT="wez", FAKE_ACTIVE_CLIENTS="1")
        self.not_raised()
        self.assertEqual(self.opt("@agent_ask_seen"), "which branch")
        sn.IndicatorNoticeTests.assert_recent_epoch(self, self.opt("@agent_seen_at"))

    def test_the_payload_file_is_gone_while_the_decider_runs(self):
        self.start_turn()
        self.decider(m_rc=2, f_rc=1, f_sleep=1.5)
        self.stop()
        self.assertTrue(self.wait_for(lambda: "full" in self.calls(), 5))
        time.sleep(0.3)
        self.assertEqual(sorted(p.name for p in self.root.glob("agent-tab-askbg.*")), [])

    def test_one_sanitiser_fork_and_no_extra_read_per_stop(self):
        real_jq = __import__("shutil").which("jq")
        jq_log = self.root / "jq.log"
        sn.write_exe(self.bin / "jq", '#!/bin/sh\nprintf "%%s\\n" "$*" | tail -c 40 | tr -d "\\n" >> %s; echo >> %s\nexec %s "$@"\n'
                     % (jq_log, jq_log, real_jq))
        self.start_turn()
        self.set_opt("@agent_ask_seen", "an older question")
        self.decider(m_rc=0, m_line="a new question")
        self.env["TMUX_ARGV_LOG"] = str(self.argv_log)
        jq_log.write_text("")
        self.stop()
        self.raised("a new question")
        cleans = [l for l in jq_log.read_text().splitlines() if l.endswith("clean")]
        self.assertEqual(len(cleans), 1, cleans)
        reads = [a for a in self.look_list() if "show-options" in a and "@agent_turn_start" in a]
        self.assertEqual(reads, [])


if __name__ == "__main__":
    unittest.main()
