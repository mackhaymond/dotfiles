"""RATE-LIMITED AGENTS (docs/agent-tab-indicator.md, "Rate-limited agents"):
a usage limit makes an agent PAUSED with a reason, never `failed`.

  - Claude: StopFailure error == rate_limit marks the window from the
    transcript's last main-thread rate_limit record, in ONE tmux command list
    and in the contract's order (@agent_paused, @agent_limit, detail kind,
    detail, turn result, since, state last), then runs agent-pause.py
    limit-add in the background. Model limits and records without resetsAt
    are type `model`, R 0. Every other StopFailure stays `failed`.
  - A prompt while @agent_limit is set: held (nothing written) while the
    limit's pause stands and R is ahead, unless Mack typed it; otherwise a
    LOUD running turn with the ordinary `run` detail. A successful Stop
    clears the limit; SessionStart clear/startup, SessionEnd and the
    watcher's GC drop it with @agent_paused.
  - Codex: the watcher marks a rollout task_complete usage_limit_exceeded the
    same way, R from "try again at ..." (both formats), else token_count.
  - The watcher spawns agent-pause.py limit-tick on due / a cswap switch / a
    wake gap, at most once per 20 s.
  - The tab shows the peach limit glyph from tmux.conf.tmpl's one spot.

Indicator tests use test_agent_origin's private-server harness (a real
`tmux -L <unique> -f /dev/null`, killed in cleanup); watcher tests the fake
tmux of test_agent_jump_watcher; agent-pause.py is a logging stub
(AGENT_LIMIT_PAUSE_PY). Run with unittest or pytest.
"""
from datetime import datetime, timedelta, timezone
import json
import os
import re
import subprocess
import time
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

import test_agent_jump_watcher as jw
import test_agent_origin as ao
from test_agent_state_notice import FAKE_LSAPPINFO, write_exe

TZ_NAME = "America/Los_Angeles"
TZ = ZoneInfo(TZ_NAME)
TMUX_CONF = ao.SCRIPTS.parent / "tmux.conf.tmpl"
WATCHER = ao.SCRIPTS / "executable_agent-tab-watcher.sh"
MARK_ORDER = ["@agent_paused", "@agent_limit", "@agent_detail_kind", "@agent_detail",
              "@agent_turn_result", "@agent_since", "@agent_state"]
LABEL = {"five_hour": "session limit", "seven_day": "weekly limit", "seven_day_opus": "Opus weekly limit",
         "seven_day_sonnet": "Sonnet weekly limit", "overage": "overage limit", "codex": "Codex limit"}
STUB = """#!/bin/sh
echo "$*" >> "%s"
"""


def hm(d):
    return "%d:%02d %s" % (d.hour % 12 or 12, d.minute, "AM" if d.hour < 12 else "PM")


def when_local(r, now):
    """The shared date rule (agent-pause.py fmt_local, agent-ui text::when):
    by calendar days in local time - today "5:31 PM", the next six days
    "Tue 5:31 PM", anything else (a week or more on, or past) "Nov 2 5:31 PM"."""
    dr, dn = datetime.fromtimestamp(r, TZ), datetime.fromtimestamp(now, TZ)
    days = (dr.date() - dn.date()).days
    if days == 0:
        return hm(dr)
    if 1 <= days <= 6:
        return dr.strftime("%a") + " " + hm(dr)
    return "%s %d %s" % (dr.strftime("%b"), dr.day, hm(dr))


def ltext(r, ty, now):
    """limit_text, in Python."""
    lab = LABEL.get(ty, "usage limit")
    if r <= 0:
        return lab
    return "%s · resets %s" % (lab, when_local(r, now))


def local_epoch(d, hh, mm):
    """Epoch of local date d (a date) at hh:mm."""
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ).timestamp())


def wait_for(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def rl_record(resets=None, rtype="seven_day", text="You've hit your weekly limit", api=None,
              params=None, sidechain=False):
    r = {"type": "assistant", "isSidechain": sidechain, "isApiErrorMessage": True, "error": "rate_limit",
         "apiError": api, "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
    if resets is not None:
        r["quotaLimits"] = {"status": "rejected", "resetsAt": resets, "rateLimitType": rtype}
    if params is not None:
        r["apiErrorParams"] = params
    return r


class LimitHookHarness(ao.OriginHarness):
    def setUp(self):
        super().setUp()
        self.calls = self.root / "pause-calls.log"
        self.pausepy = self.bin / "agent-pause.py"
        write_exe(self.pausepy, STUB % self.calls)
        self.env.update(AGENT_LIMIT_PAUSE_PY=str(self.pausepy), TZ=TZ_NAME)

    def transcript_rows(self, *rows):
        with self.transcript.open("a") as f:
            f.write('{"type":"user","message":{"content":"hi"}}\n')
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def stop_failure(self, pid, error="rate_limit", **extra):
        payload = {"session_id": "s1", "cwd": str(self.root), "hook_event_name": "StopFailure",
                   "prompt_id": pid, "error": error, "error_details": "429",
                   "last_assistant_message": extra.pop("msg", "API Error"),
                   "transcript_path": str(self.transcript)}
        payload.update(extra)
        r = self.hook("failed", "claude", payload=payload)
        if error == "rate_limit" and "agent_id" not in extra and self.pausepy.exists():
            # limit-add runs detached through the server: let it land before
            # the next step (and before cleanup removes its log's directory).
            self.n_marks = getattr(self, "n_marks", 0) + 1
            self.pause_calls(self.n_marks)
        return r

    def pause_calls(self, n=1):
        def got():
            if not self.calls.exists():
                return None
            lines = self.calls.read_text().splitlines()
            return lines if len(lines) >= n else None
        return wait_for(got) or []

    def mark_week(self, pid="u1", days=3):
        r = int(time.time()) + days * 86400 + 600
        self.transcript_rows(rl_record(r, "seven_day"))
        self.stop_failure(pid)
        return r

    def limit(self):
        return self.opt("@agent_limit").split()


class LimitMarkTests(LimitHookHarness):
    def test_weekly_limit_marks_paused_in_one_ordered_list(self):
        log = self.root / "tmux-argv.log"
        write_exe(self.bin / "tmux", """#!/bin/sh
case "$*" in *'#{window_active_clients}'*) echo 0; exit 0 ;; esac
case "$*" in *'@agent_limit '*) printf '%%s\\n' "$@" > "%s" ;; esac
exec "%s" "$@"
""" % (log, ao.REAL_TMUX))
        self.prompt("build it", "u1")
        now = int(time.time())
        r = self.mark_week()
        self.assertEqual(self.opt("@agent_state"), "paused")
        e = self.opt("@agent_paused")
        self.assertRegex(e, r"^\d+$")
        self.assertEqual(self.limit(), [str(r), "seven_day", e])
        self.assertEqual(self.opt("@agent_detail_kind"), "limit")
        self.assertEqual(self.opt("@agent_detail"), ltext(r, "seven_day", now))
        self.assertTrue(self.opt("@agent_detail").startswith("weekly limit · resets "))
        self.assertEqual(self.opt("@agent_turn_result"), "u1 limited %d" % r)
        self.assertEqual(self.opt("@agent_since"), e + " paused")
        # One tmux call; the seven writes in the contract's order, state last.
        argv = log.read_text().splitlines()
        names = [argv[i + 4] for i, a in enumerate(argv)
                 if a == "set-option" and argv[i + 1] == "-w" and argv[i + 4] in MARK_ORDER]
        self.assertEqual(names, MARK_ORDER)
        self.assertEqual(argv[-1], "paused")
        self.assertEqual(self.pause_calls(), ["limit-add " + self.win])

    def test_session_limit_resets_today_reads_as_a_time(self):
        now = int(time.time())
        r = now + 600
        self.transcript_rows(rl_record(r, "five_hour", "You've hit your session limit"))
        self.stop_failure("u1")
        self.assertEqual(self.limit()[:2], [str(r), "five_hour"])
        self.assertEqual(self.opt("@agent_detail"), ltext(r, "five_hour", now))

    def test_model_limit_is_type_model_r_zero(self):
        self.transcript_rows(rl_record(None, text="You've reached your Fable limit. Run /usage-credits to "
                                                  "continue or switch models with /model.",
                                       api="model_requires_usage_credits"))
        self.stop_failure("m1")
        self.assertEqual(self.opt("@agent_state"), "paused")
        self.assertEqual(self.limit()[:2], ["0", "model"])
        self.assertEqual(self.opt("@agent_detail"), "Fable limit · /model to switch")
        self.assertEqual(self.opt("@agent_turn_result"), "m1 limited 0")
        self.assertEqual(self.pause_calls(), ["limit-add " + self.win])

    def test_model_limit_from_last_assistant_message_without_a_transcript(self):
        self.transcript.unlink()
        self.stop_failure("m1", msg="You've reached your Opus limit. Switch models with /model.")
        self.assertEqual(self.limit()[:2], ["0", "model"])
        self.assertEqual(self.opt("@agent_detail"), "Opus limit · /model to switch")

    def test_no_resets_at_is_model_with_its_own_words(self):
        self.transcript_rows(rl_record(None, text="You've hit your limit · resets 6pm"))
        self.stop_failure("u1")
        self.assertEqual(self.limit()[:2], ["0", "model"])
        self.assertEqual(self.opt("@agent_detail"), "You've hit your limit · resets 6pm")

    def test_api_error_params_fallback_and_unknown_type(self):
        r = int(time.time()) + 2 * 86400
        rec = rl_record(None, api="usage_limit_reached",
                        params={"rate_limit_info": {"resetsAt": r, "rateLimitType": "seven_day_overage_included"}})
        self.transcript_rows(rec)
        self.stop_failure("u1")
        self.assertEqual(self.limit()[:2], [str(r), "seven_day"])

    def test_sidechain_record_is_ignored(self):
        now = int(time.time())
        main, side = now + 4 * 86400, now + 3600
        self.transcript_rows(rl_record(main, "seven_day"), rl_record(side, "five_hour", sidechain=True))
        self.stop_failure("u1")
        self.assertEqual(self.limit()[:2], [str(main), "seven_day"])

    def test_other_stop_failures_stay_failed(self):
        self.prompt("build it", "u1")
        self.transcript_rows(rl_record(int(time.time()) + 3600))
        self.stop_failure("u1", error="overloaded", msg="529")
        self.assertEqual(self.opt("@agent_state"), "failed")
        self.assertEqual(self.opt("@agent_turn_result"), "u1 loud")
        self.assertEqual(self.opt("@agent_detail_kind"), "fail")
        self.assertFalse(self.has("@agent_limit"))
        self.assertFalse(self.has("@agent_paused"))
        time.sleep(0.3)
        self.assertFalse(self.calls.exists())

    def test_subagent_rate_limit_is_ignored(self):
        self.prompt("build it", "u1")
        self.transcript_rows(rl_record(int(time.time()) + 3600))
        self.stop_failure("u1", agent_id="sub-1")
        self.assertEqual(self.opt("@agent_state"), "running")
        self.assertFalse(self.has("@agent_limit"))

    def test_remark_is_idempotent(self):
        self.prompt("build it", "u1")
        r = self.mark_week()
        first = self.limit()
        time.sleep(1.1)
        self.stop_failure("n2")                         # a queued task-notification's turn
        self.assertEqual(self.opt("@agent_state"), "paused")
        again = self.limit()
        self.assertEqual(again[:2], [str(r), "seven_day"])
        self.assertGreater(int(again[2]), int(first[2]))   # since refreshed
        self.assertEqual(self.opt("@agent_paused"), again[2])
        self.assertEqual(self.opt("@agent_turn_result"), "n2 limited %d" % r)
        self.assertEqual(len(self.pause_calls(2)), 2)

    def test_a_missing_pause_script_costs_nothing(self):
        self.pausepy.unlink()
        self.mark_week()
        self.assertEqual(self.opt("@agent_state"), "paused")

    def test_needs_input_asker_goes_with_the_mark(self):
        self.prompt("build it", "u1")
        self.hook("needs-approval", "claude", payload={"session_id": "s1", "tool_name": "Bash",
                                                       "tool_input": {"command": "ls"}})
        self.assertEqual(self.opt("@agent_ask_from"), "main")
        self.mark_week()
        self.assertFalse(self.has("@agent_ask_from"))


class LimitPromptTests(LimitHookHarness):
    WAKE = "<task-notification><task-id>b1</task-id><status>completed</status></task-notification>"
    RESUME = ("[message from @pause-resume-agents] Your usage limit has reset. Continue the task you were "
              "working on when the limit was reached; do not repeat work that is already complete.")

    def test_wakeup_before_reset_is_held_paused(self):
        self.prompt("build it", "u1")
        self.mark_week()
        before = {n: self.opt(n) for n in ("@agent_since", "@agent_turn_origin", "@agent_detail")}
        self.prompt(self.WAKE, "w1")
        self.assertEqual(self.opt("@agent_state"), "paused")
        self.assertEqual({n: self.opt(n) for n in before}, before)
        self.prompt(self.deliver("status?"), "p1")      # a peer's ping: held too
        self.assertEqual(self.opt("@agent_state"), "paused")
        self.assertTrue(self.has("@agent_paused"))

    def test_resume_after_limit_tick_is_loud_and_running(self):
        self.prompt(self.deliver("run the migration"), "a1")   # an AGENT-started turn hits the limit
        self.mark_week()
        subprocess.run(self.tmuxcmd + ["set-option", "-uw", "-t", self.win, "@agent_paused"], check=True)
        out = self.prompt(self.deliver(self.RESUME.split("] ", 1)[1], frm="pause-resume-agents"), "r1")
        self.assertEqual(out, "")                              # no "Mack won't be notified"
        self.assertEqual(self.opt("@agent_state"), "running")
        self.assertEqual(self.opt("@agent_turn_origin"), "user r1 -")
        self.assertEqual(self.opt("@agent_detail_kind"), "run")
        self.assertTrue(self.opt("@agent_detail").startswith("[message from @pause-resume-agents]"))
        self.assertTrue(self.has("@agent_limit"))              # kept until the outcome
        self.stop("r1", msg="Migration finished.")
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertEqual(self.opt("@agent_turn_result"), "r1 loud")
        self.assertFalse(self.has("@agent_limit"))

    def test_mack_typing_before_reset_runs_and_unpauses(self):
        self.prompt("build it", "u1")
        self.mark_week()
        self.prompt("try again anyway", "u2")
        self.assertEqual(self.opt("@agent_state"), "running")
        self.assertFalse(self.has("@agent_paused"))
        self.assertTrue(self.has("@agent_limit"))
        self.assertEqual(self.opt("@agent_detail_kind"), "run")
        self.assertEqual(self.opt("@agent_detail"), "try again anyway")
        self.assertEqual(self.opt("@agent_turn_origin"), "user u2 -")

    def test_native_auto_continuation_after_reset_runs_and_unpauses(self):
        self.prompt("build it", "u1")
        now = int(time.time())
        self.transcript_rows(rl_record(now + 2, "five_hour"))
        self.stop_failure("u1")
        e = self.opt("@agent_paused")
        time.sleep(2.2)                                       # past R
        self.prompt(self.WAKE, "c1")
        self.assertEqual(self.opt("@agent_state"), "running")
        self.assertFalse(self.has("@agent_paused"), e)
        self.assertEqual(self.opt("@agent_turn_origin"), "user c1 -")
        self.assertNotEqual(self.opt("@agent_detail_kind"), "limit")
        # ...and its clean Stop is a loud done, not paused.
        self.stop("c1", msg="Continued and finished.")
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertEqual(self.opt("@agent_turn_result"), "c1 loud")
        self.assertFalse(self.has("@agent_limit"))

    def test_any_unheld_prompt_unpauses_even_a_foreign_stamp(self):
        # limit-tick never ran and the flag holds some other epoch (a refused
        # RESUME re-set it, say): the window is still the limit's `paused`.
        self.prompt("build it", "u1")
        now = int(time.time())
        self.transcript_rows(rl_record(now + 2, "five_hour"))
        self.stop_failure("u1")
        self.set_opt("@agent_paused", str(now - 50))
        time.sleep(2.2)
        self.prompt(self.deliver(self.RESUME.split("] ", 1)[1], frm="pause-resume-agents"), "r1")
        self.assertFalse(self.has("@agent_paused"))
        self.stop("r1")
        self.assertEqual((self.opt("@agent_state"), self.opt("@agent_turn_result")), ("done", "r1 loud"))

    def test_model_limit_is_never_held(self):
        self.transcript_rows(rl_record(None, api="model_requires_usage_credits",
                                       text="You've reached your Fable limit."))
        self.stop_failure("m1")
        self.prompt(self.WAKE, "w1")                           # R 0: nothing to wait for
        self.assertEqual(self.opt("@agent_state"), "running")

    def test_the_pause_keys_stamp_on_a_working_window_survives_its_pause(self):
        self.prompt("build it", "u1")
        self.mark_week()
        self.prompt("go on anyway", "u2")                       # Mack: running, limit kept
        self.assertFalse(self.has("@agent_paused"))
        e = int(self.limit()[2])
        self.set_opt("@agent_limit", "%d seven_day %d" % (int(time.time()) - 5, e))   # R passed: not held
        self.set_opt("@agent_paused", str(e + 5))               # prefix P while it works
        self.prompt(self.deliver("[pause key] PAUSE: stop at a safe point.", frm="pause-resume-agents"), "u2")
        self.assertEqual(self.opt("@agent_state"), "running")
        self.assertEqual(self.opt("@agent_paused"), str(e + 5))
        self.stop("u2")
        self.assertEqual(self.opt("@agent_state"), "paused")   # the manual pause stands
        self.assertFalse(self.has("@agent_limit"))

    def test_held_turn_that_got_through_ends_done_not_paused(self):
        # cswap lifted the limit early: the held wake-up's turn succeeds.
        self.prompt("build it", "u1")
        self.mark_week()
        self.prompt(self.WAKE, "w1")
        self.assertEqual(self.opt("@agent_state"), "paused")
        self.set_opt("@agent_limit_lifted", str(int(time.time())))
        self.stop("w1", msg="All done.")
        self.assertEqual(self.opt("@agent_state"), "done")
        for name in ("@agent_limit", "@agent_limit_lifted", "@agent_paused"):
            self.assertFalse(self.has(name), name)

    def test_a_stop_for_the_limited_turn_itself_changes_nothing(self):
        # Codex may run its Stop hook for the failed turn after the watcher
        # marked it; the turn result names that turn.
        self.prompt("build it", "u1")
        r = self.mark_week("u1")
        self.stop("u1")
        self.assertEqual(self.opt("@agent_state"), "paused")
        self.assertEqual(self.opt("@agent_turn_result"), "u1 limited %d" % r)
        self.assertTrue(self.has("@agent_limit"))

    def test_session_start_clear_drops_the_limit_resume_keeps_it(self):
        for source, kept in (("clear", False), ("startup", False), ("resume", True)):
            with self.subTest(source=source):
                self.mark_week()
                self.set_opt("@agent_limit_lifted", "1")
                self.hook("idle", "claude", payload={"session_id": "s2", "cwd": str(self.root), "source": source})
                self.assertEqual(self.has("@agent_limit"), kept)
                self.assertEqual(self.has("@agent_limit_lifted"), kept)

    def test_session_end_drops_the_limit(self):
        self.mark_week()
        self.set_opt("@agent_limit_lifted", "1")
        self.hook("clear", "claude", payload={"session_id": "s1", "reason": "other"})
        for name in ("@agent_limit", "@agent_limit_lifted", "@agent_paused", "@agent_state"):
            self.assertFalse(self.has(name), name)

    def test_heartbeat_never_rearms_a_limited_window(self):
        self.prompt("build it", "u1")
        self.mark_week()
        self.hook("heartbeat", "claude", payload={"session_id": "s1", "tool_name": "Bash"})
        self.assertEqual(self.opt("@agent_state"), "paused")


class LimitReviewTests(LimitHookHarness):
    """Round 2 (bug review + integration review)."""

    def prompt_src(self, text, pid, source):
        payload = {"session_id": "s1", "cwd": str(self.root), "hook_event_name": "UserPromptSubmit",
                   "prompt": text, "prompt_id": pid, "source": source, "transcript_path": str(self.transcript)}
        return self.hook("running", "claude", payload=payload).stdout

    # --- F1: UserPromptSubmit `source` wake-ups are not Mack ---

    def test_wakeup_sources_are_held_before_the_reset(self):
        for src in ("loop_wakeup", "schedule_wakeup", "poll_event"):
            with self.subTest(source=src):
                self.prompt("build it", "u-" + src)
                self.mark_week()
                before = {n: self.opt(n) for n in ("@agent_since", "@agent_turn_origin", "@agent_paused")}
                self.prompt_src("check the CI build again", "w-" + src, src)
                self.assertEqual(self.opt("@agent_state"), "paused")
                self.assertEqual({n: self.opt(n) for n in before}, before)

    def test_a_plain_system_prompt_is_not_held(self):
        # Native auto-continuation is source `system`: never treated as a
        # wake-up by its source alone.
        self.prompt("build it", "u1")
        self.mark_week()
        self.prompt_src("Continue from where you left off.", "c1", "system")
        self.assertEqual(self.opt("@agent_state"), "running")
        self.assertFalse(self.has("@agent_paused"))

    def test_wakeup_sources_on_an_unlimited_window_are_unchanged(self):
        for src in ("loop_wakeup", "schedule_wakeup", "poll_event", "user", "sdk"):
            with self.subTest(source=src):
                self.prompt_src("check the CI build again", "w-" + src, src)
                self.assertEqual(self.opt("@agent_state"), "running")
                self.assertEqual(self.opt("@agent_turn_origin"), "user w-%s -" % src)

    # --- F2: any outcome but a rate limit ends the limit ---

    def test_a_non_rate_limit_failure_ends_the_limit_and_a_peer_is_a_peer_again(self):
        self.prompt("build it", "u1")
        self.mark_week()
        self.prompt("try again anyway", "u2")                   # Mack: running, limit kept
        self.set_opt("@agent_limit_lifted", "5")
        self.stop_failure("u2", error="server_error", msg="500")
        self.assertEqual(self.opt("@agent_state"), "failed")
        self.assertFalse(self.has("@agent_limit"))
        self.assertFalse(self.has("@agent_limit_lifted"))
        out = self.prompt(self.deliver("status?", frm="peer"), "a1")
        self.assertEqual(self.opt("@agent_turn_origin"), "agent a1 peer")
        self.assertTrue(out.strip())                            # the "started by @peer" context

    def test_a_failure_on_the_limited_pause_drops_the_limits_flag(self):
        self.prompt("build it", "u1")
        self.mark_week()
        self.stop_failure("u1", error="server_error", msg="500")
        self.assertEqual(self.opt("@agent_state"), "failed")
        self.assertFalse(self.has("@agent_limit"))
        self.assertFalse(self.has("@agent_paused"))              # it held the limit's E

    def interrupt_case(self, paused):
        now = int(time.time())
        self.prompt("build it", None, agent="opencode")
        self.set_opt("@agent_limit", "%d five_hour %d" % (now + 3600, now - 60))
        self.set_opt("@agent_limit_lifted", "5")
        self.set_opt("@agent_paused", str(paused(now - 60)))
        self.hook("interrupt", "opencode", payload={"session_id": "s1", "cwd": str(self.root)})

    def test_an_interrupt_ends_the_limit(self):
        self.interrupt_case(lambda e: e)                         # the limit's own flag
        self.assertEqual(self.opt("@agent_state"), "idle")
        for name in ("@agent_limit", "@agent_limit_lifted", "@agent_paused"):
            self.assertFalse(self.has(name), name)

    def test_an_interrupt_keeps_a_pause_keys_flag(self):
        self.interrupt_case(lambda e: e + 30)                    # prefix P, after the mark
        self.assertEqual(self.opt("@agent_state"), "paused")
        self.assertTrue(self.has("@agent_paused"))
        self.assertFalse(self.has("@agent_limit"))


class CodexStopTests(LimitHookHarness):
    """Codex runs its Stop for a turn that ended on a usage limit: the
    indicator rules it `limited` from the rollout, at once."""
    def setUp(self):
        super().setUp()
        owners = self.root / "owners.json"
        owners.write_text(json.dumps({"cx1": {"status": "bound", "session_id": "cx1",
            "tmux_socket": self.socket, "pane": self.pane, "token": "tok", "binding_id": "b-cx1",
            "frontend_pid": 1, "frontend_start": "x"}}))
        write_exe(self.home / ".local/bin/codex-terminal-owner", ao.FAKE_OWNER)
        self.cx = dict(FAKE_OWNER_STATE=str(owners), FAKE_FRONTEND="1", TMUX="/stale,1,0", TMUX_PANE="%999")
        self.hook("idle", "codex", payload={"session_id": "cx1", "cwd": str(self.root), "source": "startup"},
                  **self.cx)
        self.rp = self.root / "rollout.jsonl"
        self.set_opt("@agent_rollout", str(self.rp))
        self.hook("running", "codex", payload={"session_id": "cx1", "turn_id": "t1", "cwd": str(self.root),
                                                "hook_event_name": "UserPromptSubmit", "prompt": "build it"},
                  **self.cx)

    def rollout(self, turn, limited, when="6:32 PM"):
        now = int(time.time())
        rows = [{"timestamp": iso(now - 20), "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}}]
        tc = {"type": "task_complete", "turn_id": turn, "last_agent_message": None}
        if limited:
            tc["error"] = {"message": "You’ve hit your usage limit. Visit https://chatgpt.com/settings/"
                                      "usage to purchase more credits or try again at %s." % when,
                           "codex_error_info": "usage_limit_exceeded"}
        rows.append({"timestamp": iso(now - 1), "ordinal": 9, "type": "event_msg", "payload": tc})
        self.rp.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))

    def cstop(self, turn):
        self.hook("done", "codex", payload={"session_id": "cx1", "turn_id": turn, "cwd": str(self.root),
                                             "hook_event_name": "Stop", "last_assistant_message": None}, **self.cx)

    def test_the_stop_of_a_limited_turn_marks_it_limited(self):
        when = datetime.fromtimestamp(time.time(), TZ) + timedelta(days=4)
        text = "%s %d%s, %d %s" % (when.strftime("%b"), when.day, "th" if when.day in (11, 12, 13)
                                   else {1: "st", 2: "nd", 3: "rd"}.get(when.day % 10, "th"), when.year, hm(when))
        r = int(datetime(when.year, when.month, when.day, when.hour, when.minute, tzinfo=TZ).timestamp())
        self.rollout("t1", True, text)
        self.cstop("t1")
        self.assertEqual(self.opt("@agent_state"), "paused")
        e = self.opt("@agent_paused")
        self.assertEqual(self.opt("@agent_limit"), "%d codex %s" % (r, e))
        self.assertEqual(self.opt("@agent_turn_result"), "t1 limited %d" % r)
        self.assertEqual(self.opt("@agent_detail_kind"), "limit")
        self.assertEqual(self.opt("@agent_kind"), "codex")
        self.assertEqual(self.pause_calls(), ["limit-add " + self.win])

    def test_a_limit_for_another_turn_is_an_ordinary_stop(self):
        self.rollout("t0", True)
        self.cstop("t1")
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertFalse(self.has("@agent_limit"))

    def test_an_ordinary_task_complete_is_done(self):
        self.rollout("t1", False)
        self.cstop("t1")
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertEqual(self.opt("@agent_turn_result"), "t1 loud")


class LimitCoreTests(unittest.TestCase):
    def test_limit_core_is_byte_identical(self):
        pat = re.compile(r"^# >>> LIMIT CORE >>>.*?^# <<< LIMIT CORE <<<$", re.S | re.M)
        a = pat.search(ao.INDICATOR.read_text()).group(0)
        b = pat.search(WATCHER.read_text()).group(0)
        self.assertEqual(a, b)

    def run_core(self, cases):
        core = re.search(r"^# >>> LIMIT CORE >>>.*?^# <<< LIMIT CORE <<<$", ao.INDICATOR.read_text(),
                         re.S | re.M).group(0)
        script = core + "\n" + "\n".join(
            'limit_text %d %s %d; printf "%%s\\n" "$LIM_TXT"' % (r, t, now) for r, t, now in cases)
        out = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                             env=dict(os.environ, TZ=TZ_NAME), timeout=5)
        return out.stdout.splitlines()

    def test_the_shared_date_rule_by_calendar_days(self):
        # E2: agent-pause.py fmt_local / agent-ui text::when - by calendar
        # days in local time, not by a 6 x 24 h span.
        d = (datetime.now(TZ) + timedelta(days=20)).date()   # a fixed future day, no DST in the span checked
        day = lambda k: d + timedelta(days=k)
        cases = [
            (local_epoch(day(6), 23, 0), "seven_day", local_epoch(d, 1, 0)),     # +6 days, 6 d 22 h: weekday
            (local_epoch(day(7), 0, 30), "seven_day", local_epoch(d, 23, 0)),    # +7 days, 25.5 h short of 7 d: date
            (local_epoch(day(1), 0, 5), "five_hour", local_epoch(d, 23, 55)),    # past midnight: tomorrow's weekday
            (local_epoch(d, 23, 55), "five_hour", local_epoch(d, 0, 5)),          # same day: the time
            (local_epoch(day(-1), 23, 0), "seven_day", local_epoch(d, 1, 0)),    # past: the date
        ]
        got = self.run_core(cases)
        want = [ltext(r, t, now) for r, t, now in cases]
        self.assertEqual(got, want)
        self.assertTrue(want[0].split("resets ")[1][:3] in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))
        self.assertRegex(want[1], r"resets [A-Z][a-z]{2} \d{1,2} 12:30 AM$")
        self.assertRegex(want[4], r"resets [A-Z][a-z]{2} \d{1,2} 11:00 PM$")

    def test_limit_text_formats(self):
        core = re.search(r"^# >>> LIMIT CORE >>>.*?^# <<< LIMIT CORE <<<$", ao.INDICATOR.read_text(),
                         re.S | re.M).group(0)
        now = int(time.time())
        cases = [(now + 300, "five_hour"), (now + 2 * 86400, "seven_day"), (now + 10 * 86400, "codex"),
                 (0, "model"), (now + 86400, "seven_day_opus")]
        script = core + "\n" + "\n".join(
            'limit_text %d %s %d; printf "%%s\\n" "$LIM_TXT"' % (r, t, now) for r, t in cases)
        out = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                             env=dict(os.environ, TZ=TZ_NAME), timeout=5)
        self.assertEqual(out.stdout.splitlines(), [ltext(r, t, now) for r, t in cases])


# ------------------------------------------------------------ the watcher

def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# The shared fake tmux, plus two things this file needs: a format resolves a
# user option window -> GLOBAL (as tmux does, so @agent_limit_due can live
# where agent-pause.py puts it), and FAKE_SLOW_LISTPANES="N:secs" makes the
# Nth list-panes call (the watcher's one read per tick) take that long - a
# real gap between two ticks.
FAKE_TMUX_G = (jw.FAKE_TMUX
               .replace("import fcntl, json, os, re, sys", "import fcntl, json, os, re, sys, time")
               .replace('    v = {"window_id": wid,', '    v = dict(s.get("globals", {})); v.update({"window_id": wid,')
               .replace('w.get("active_clients", 0)}', 'w.get("active_clients", 0)})')
               .replace('        elif cmd == "list-panes":\n',
                        '        elif cmd == "list-panes":\n'
                        '            s["lp_count"] = s.get("lp_count", 0) + 1\n'
                        '            _sl = os.environ.get("FAKE_SLOW_LISTPANES", "")\n'
                        '            if _sl and int(_sl.split(":")[0]) == s["lp_count"]:\n'
                        '                time.sleep(float(_sl.split(":")[1]))\n'))
assert FAKE_TMUX_G.count("lp_count") == 3 and "v.update({" in FAKE_TMUX_G


class LimitWatcherHarness(unittest.TestCase):
    def setUp(self):
        self.f = jw.FakeEnv(windows={}, panes=[])
        self.addCleanup(self.f.close)
        write_exe(self.f.bin / "tmux", FAKE_TMUX_G)
        write_exe(self.f.bin / "lsappinfo", FAKE_LSAPPINFO)
        # agent-bg-pending: nothing out (so a session that says idle un-sticks).
        (self.f.home / ".local/bin").mkdir(parents=True, exist_ok=True)
        write_exe(self.f.home / ".local/bin/agent-bg-pending", "#!/bin/sh\nexit 1\n")
        self.calls = self.f.dir / "pause-calls.log"
        self.pausepy = self.f.bin / "agent-pause.py"
        write_exe(self.pausepy, STUB % self.calls)
        self.seq = self.f.dir / "sequence.json"
        self.now = int(time.time())

    def window(self, kind="claude", **opts):
        self.f.procs.write_text("ttys902 4242 %s\n" % kind)
        st = self.f.read()
        st["windows"] = {"@1": jw.W("main", 1, **dict({"@agent_kind": kind}, **opts))}
        st["panes"] = [{"window": "@1", "tty": "/dev/ttys902"}]
        self.f.state.write_text(json.dumps(st))

    def set_opts(self, **opts):
        st = self.f.read(); st["windows"]["@1"]["opts"].update(opts)
        self.f.state.write_text(json.dumps(st))

    def ticks(self, n, **env):
        e = dict(AGENT_LIMIT_PAUSE_PY=str(self.pausepy), AGENT_LIMIT_SEQ_FILE=str(self.seq), TZ=TZ_NAME)
        e.update(env)
        r = self.f.run("agent-tab-watcher.sh", timeout=10 + 2 * n, AGENT_TAB_WATCHER_MAX_TICKS=str(n), **e)
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.f.read()["windows"]["@1"]["opts"]

    def pause_calls(self, n=1, timeout=3.0):
        def got():
            if not self.calls.exists():
                return None
            lines = self.calls.read_text().splitlines()
            return lines if len(lines) >= n else None
        return wait_for(got, timeout) or ([] if not self.calls.exists() else self.calls.read_text().splitlines())


class CodexLimitTests(LimitWatcherHarness):
    def rollout(self, message, ts=None, token=None, extra_after=None):
        ts = ts if ts is not None else self.now - 5
        rp = self.f.dir / "rollout.jsonl"
        rows = [{"timestamp": iso(ts - 20), "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t9"}}]
        if token is not None:
            rows.append({"timestamp": iso(ts - 1), "type": "event_msg",
                         "payload": {"type": "token_count", "info": None, "rate_limits": token}})
        rows.append({"timestamp": iso(ts), "ordinal": 12, "type": "event_msg",
                     "payload": {"type": "task_complete", "turn_id": "t9", "last_agent_message": None,
                                 "error": {"message": message, "codex_error_info": "usage_limit_exceeded"}}})
        rp.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
        return rp

    MSG = ("You’ve hit your usage limit. Visit https://chatgpt.com/settings/usage to purchase more "
           "credits or try again at %s.")

    def running(self, rp, since_age=30):
        self.window("codex", **{"@agent_state": "running", "@agent_since": "%d running" % (self.now - since_age),
                                "@agent_rollout": str(rp), "@agent_detail_kind": "run", "@agent_detail": "go"})

    def assert_marked(self, o, r):
        self.assertEqual(o["@agent_state"], "paused")
        e = o["@agent_paused"]
        self.assertEqual(o["@agent_limit"], "%d codex %s" % (r, e))
        self.assertEqual(o["@agent_detail_kind"], "limit")
        self.assertEqual(o["@agent_detail"], ltext(r, "codex", int(e)))
        self.assertEqual(o["@agent_turn_result"], "t9 limited %d" % r)
        self.assertEqual(o["@agent_since"], e + " paused")
        self.assertEqual(self.pause_calls(), ["limit-add @1"])

    def test_time_only_format_is_today_or_tomorrow(self):
        for delta in (2 * 3600, -3600):
            with self.subTest(delta=delta):
                self.calls.unlink(missing_ok=True)
                t = datetime.fromtimestamp(self.now + delta, TZ)
                cand = t.replace(second=0, microsecond=0)
                today = datetime.fromtimestamp(self.now, TZ)
                cand = datetime(today.year, today.month, today.day, cand.hour, cand.minute, tzinfo=TZ)
                if cand.timestamp() <= self.now:
                    cand = cand + timedelta(days=1)
                rp = self.rollout(self.MSG % hm(t))
                self.running(rp)
                o = self.ticks(1)
                self.assert_marked(o, int(cand.timestamp()))

    def test_full_date_format(self):
        when = datetime.fromtimestamp(self.now, TZ) + timedelta(days=5)
        day = when.day
        suf = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        text = "%s %d%s, %d %s" % (when.strftime("%b"), day, suf, when.year, hm(when))
        r = int(datetime(when.year, when.month, day, when.hour, when.minute, tzinfo=TZ).timestamp())
        rp = self.rollout(self.MSG % text)
        self.running(rp)
        self.assert_marked(self.ticks(1), r)

    def test_token_count_fallback(self):
        r5, r7 = self.now + 4000, self.now + 5 * 86400
        token = {"primary": {"used_percent": 100.0, "window_minutes": 300, "resets_at": r5},
                 "secondary": {"used_percent": 40.0, "window_minutes": 10080, "resets_at": r7}}
        rp = self.rollout("You’ve hit your usage limit.", token=token)
        self.running(rp)
        self.assert_marked(self.ticks(1), r5)

    def test_no_reset_anywhere_is_zero(self):
        rp = self.rollout("You’ve hit your usage limit.")
        self.running(rp)
        self.assert_marked(self.ticks(1), 0)

    def test_needs_input_and_a_same_second_done_are_marked(self):
        rp = self.rollout(self.MSG % "Oct 31st, 2026 11:10 PM", ts=self.now - 5)
        r = int(datetime(2026, 10, 31, 23, 10, tzinfo=TZ).timestamp())
        for state, since in (("needs-input", self.now - 30), ("done", self.now - 5)):
            with self.subTest(state=state):
                self.calls.unlink(missing_ok=True)
                self.window("codex", **{"@agent_state": state, "@agent_since": "%d %s" % (since, state),
                                        "@agent_rollout": str(rp)})
                self.assert_marked(self.ticks(1), r)

    def test_an_old_limit_on_an_idle_window_is_left_alone(self):
        rp = self.rollout(self.MSG % "6:32 PM", ts=self.now - 600)
        self.window("codex", **{"@agent_state": "idle", "@agent_since": "%d idle" % (self.now - 60),
                                "@agent_rollout": str(rp)})
        o = self.ticks(2)
        self.assertEqual(o["@agent_state"], "idle")
        self.assertNotIn("@agent_limit", o)

    def test_an_ordinary_task_complete_is_still_done(self):
        rp = self.f.dir / "rollout.jsonl"
        rp.write_text(json.dumps({"timestamp": iso(self.now - 5), "type": "event_msg",
                                  "payload": {"type": "task_complete", "turn_id": "t9"}},
                                 separators=(",", ":")) + "\n")
        self.running(rp)
        o = self.ticks(4)
        self.assertEqual(o["@agent_state"], "done")
        self.assertNotIn("@agent_limit", o)

    def test_a_long_final_message_still_marks(self):
        # F3: the usage-limit error sits past byte 8192 of the task_complete
        # line; detection and parsing must agree, or the turn wedges running.
        rp = self.f.dir / "rollout.jsonl"
        tc = {"type": "task_complete", "turn_id": "t9", "last_agent_message": "x" * 9000,
              "error": {"message": self.MSG % "Oct 31st, 2026 11:10 PM", "codex_error_info": "usage_limit_exceeded"}}
        rows = [{"timestamp": iso(self.now - 25), "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t9"}},
                {"timestamp": iso(self.now - 5), "type": "event_msg", "payload": tc}]
        rp.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
        self.running(rp)
        self.assert_marked(self.ticks(1), int(datetime(2026, 10, 31, 23, 10, tzinfo=TZ).timestamp()))

    def test_try_again_time_is_read_against_the_events_own_time(self):
        # F4: "try again at H:MM PM" is today or tomorrow relative to the
        # task_complete itself, not to whenever the watcher looks.
        ev = self.now - 2 * 3600
        t = datetime.fromtimestamp(ev + 3600, TZ)                 # an hour after the event, an hour ago now
        evd = datetime.fromtimestamp(ev, TZ)
        cand = datetime(evd.year, evd.month, evd.day, t.hour, t.minute, tzinfo=TZ)
        if cand.timestamp() <= ev:
            cand += timedelta(days=1)
        rp = self.rollout(self.MSG % hm(t), ts=ev)
        self.running(rp, since_age=3 * 3600)
        self.assert_marked(self.ticks(1), int(cand.timestamp()))

    def test_a_normal_codex_finish_ends_a_kept_limit(self):
        # F2 (codex): a resumed limited turn that completes ends the limit.
        rp = self.f.dir / "rollout.jsonl"
        rp.write_text(json.dumps({"timestamp": iso(self.now - 5), "type": "event_msg",
                                  "payload": {"type": "task_complete", "turn_id": "t9"}},
                                 separators=(",", ":")) + "\n")
        self.running(rp)
        self.set_opts(**{"@agent_limit": "%d codex %d" % (self.now - 100, self.now - 900),
                         "@agent_limit_lifted": str(self.now - 90)})
        o = self.ticks(4)
        self.assertEqual(o["@agent_state"], "done")
        self.assertNotIn("@agent_limit", o)
        self.assertNotIn("@agent_limit_lifted", o)

    def test_claude_window_with_a_stale_rollout_is_not_marked(self):
        rp = self.rollout(self.MSG % "6:32 PM")
        self.window("claude", **{"@agent_state": "done", "@agent_since": "%d done" % (self.now - 5),
                                 "@agent_rollout": str(rp)})
        self.assertNotIn("@agent_limit", self.ticks(2))


class LimitUnstickTests(LimitWatcherHarness):
    def test_the_unstick_idle_ends_a_kept_limit(self):
        # F2: a resumed limited Claude turn Esc'd (no Stop): the session says
        # idle, the watcher idles it - and the limit ends with the turn.
        sessions = self.f.home / ".claude/sessions"; sessions.mkdir(parents=True)
        (sessions / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": "s", "status": "shell"},
                                                      separators=(",", ":")))
        self.window("claude", **{"@agent_state": "running", "@agent_since": "%d running" % (self.now - 30),
                                 "@agent_limit": "%d seven_day %d" % (self.now + 9999, self.now - 900),
                                 "@agent_limit_lifted": "5"})
        o = self.ticks(5)
        self.assertEqual(o["@agent_state"], "idle")
        self.assertNotIn("@agent_limit", o)
        self.assertNotIn("@agent_limit_lifted", o)


class LimitGcTests(LimitWatcherHarness):
    def test_gc_drops_the_limit(self):
        self.window("claude", **{"@agent_state": "paused", "@agent_paused": "1",
                                 "@agent_limit": "1 seven_day 1", "@agent_limit_lifted": "2"})
        st = self.f.read(); st["panes"] = [{"window": "@1", "tty": "/dev/ttys903"}]   # agent gone
        self.f.state.write_text(json.dumps(st))
        o = self.ticks(6)
        for name in ("@agent_limit", "@agent_limit_lifted", "@agent_paused", "@agent_state"):
            self.assertNotIn(name, o)

    def test_watcher_leaves_a_limited_window_alone(self):
        self.window("claude", **{"@agent_state": "paused", "@agent_since": "100 paused", "@agent_paused": "1",
                                 "@agent_limit": "%d seven_day 1" % (self.now + 9999)})
        o = self.ticks(3)
        self.assertEqual((o["@agent_state"], o["@agent_since"]), ("paused", "100 paused"))


class LimitTriggerTests(LimitWatcherHarness):
    """@agent_limit_due is a GLOBAL, as agent-pause.py sets it; the pane
    format resolves it through the window (FAKE_TMUX_G does as tmux does)."""
    def due(self, v):
        self.window("claude", **{"@agent_state": "idle", "@agent_since": "1 idle"})
        st = self.f.read(); st["globals"]["@agent_limit_due"] = str(v)
        self.f.state.write_text(json.dumps(st))

    def test_due_is_read_from_the_global(self):
        self.due(self.now - 1)
        self.assertNotIn("@agent_limit_due", self.f.read()["windows"]["@1"]["opts"])
        self.assertEqual(self.ticks_calls(2), ["limit-tick"])

    def test_a_real_wake_gap_is_remembered_through_the_rate_limit(self):
        # Tick 1 fires (a cswap switch). Tick 2's read takes 3 s: a real gap
        # past the wake threshold (2 s here), but inside the rate limit (6 s
        # here) - so it may not fire then, and must fire once the limit
        # allows, with nothing else new.
        self.due(self.now + 3600)
        self.seq.write_text("{}")
        calls = self.ticks_calls(7, AGENT_LIMIT_WAKE_GAP="2", AGENT_LIMIT_TICK_EVERY="6",
                                 FAKE_SLOW_LISTPANES="2:3")
        self.assertEqual(calls, ["limit-tick", "limit-tick"])

    def test_no_wake_without_a_real_gap_under_the_same_settings(self):
        self.due(self.now + 3600)
        self.seq.write_text("{}")
        self.assertEqual(self.ticks_calls(7, AGENT_LIMIT_WAKE_GAP="2", AGENT_LIMIT_TICK_EVERY="6"),
                         ["limit-tick"])

    def ticks_calls(self, n, **env):
        self.ticks(n, **env)
        time.sleep(0.4)
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_due_now_fires_once_per_20s(self):
        self.due(self.now - 1)
        self.assertEqual(self.ticks_calls(3), ["limit-tick"])

    def test_due_ahead_does_nothing(self):
        self.due(self.now + 3600)
        self.assertEqual(self.ticks_calls(2), [])

    def test_no_due_does_nothing_even_after_a_switch(self):
        self.window("claude", **{"@agent_state": "idle", "@agent_since": "1 idle"})
        self.seq.write_text("{}")
        self.assertEqual(self.ticks_calls(2, AGENT_LIMIT_WAKE_GAP="0"), [])

    def test_cswap_switch_fires_and_the_mark_is_touched(self):
        self.due(self.now + 3600)
        self.seq.write_text("{}")
        self.assertEqual(self.ticks_calls(2), ["limit-tick"])
        mark = Path(str(self.f.tmp)) / ("agent-limit-seq.%d.mark" % os.getuid())
        self.assertTrue(mark.exists())
        self.assertFalse(self.seq.stat().st_mtime_ns > mark.stat().st_mtime_ns)
        self.calls.unlink()
        self.assertEqual(self.ticks_calls(2), [])               # nothing new since the mark
        time.sleep(0.05)
        self.seq.write_text('{"n":2}')                          # another switch
        self.assertEqual(self.ticks_calls(2), ["limit-tick"])

    def test_wake_gap_fires_while_due_is_set(self):
        self.due(self.now + 3600)
        self.assertEqual(self.ticks_calls(3, AGENT_LIMIT_WAKE_GAP="0"), ["limit-tick"])

    def test_no_wake_without_a_gap(self):
        self.due(self.now + 3600)
        self.assertEqual(self.ticks_calls(3), [])


@unittest.skipUnless(ao.REAL_TMUX, "tmux executable required")
class LimitGlyphTests(unittest.TestCase):
    """tmux.conf.tmpl's tab formats and its one @agent_limit_* spot, rendered by a real private tmux."""

    @classmethod
    def setUpClass(cls):
        text = TMUX_CONF.read_text()
        cls.fmt = {}
        for name in ("default_text", "current_text", "default_background", "current_background"):
            m = re.search(r'^set -g @catppuccin_window_%s "(.*)"$' % name, text, re.M)
            assert m, name
            cls.fmt[name] = m.group(1)
        cls.glob = dict(re.findall(r'^set -g (@agent_limit_(?:fg|glyph)) "(.*)"$', text, re.M))

    def setUp(self):
        self.server = "agent-limit-glyph-" + str(time.time_ns())
        self.tm = [ao.REAL_TMUX, "-L", self.server, "-f", "/dev/null"]
        clean = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        r = subprocess.run(self.tm + ["new-session", "-d", "-s", "g", "-P", "-F", "#{window_id}|#{socket_path}",
                                      "/bin/sleep 600"], text=True, capture_output=True, env=clean, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.win, self.socket = r.stdout.strip().split("|")
        self.addCleanup(self.kill)
        for k, v in self.glob.items():
            subprocess.run(self.tm + ["set-option", "-g", k, v], check=True, timeout=5)

    def kill(self):
        subprocess.run(self.tm + ["kill-server"], capture_output=True, timeout=5)
        Path(self.socket).unlink(missing_ok=True)

    def render(self, name, **opts):
        for k in ("@agent_state", "@agent_workflow", "@agent_cua", "@stash_sel", "@agent_summary",
                  "@agent_limit", "@agent_limit_lifted"):
            subprocess.run(self.tm + ["set-option", "-uw", "-t", self.win, k], capture_output=True, timeout=5)
        for k, v in opts.items():
            subprocess.run(self.tm + ["set-option", "-w", "-t", self.win, k, v], check=True, timeout=5)
        r = subprocess.run(self.tm + ["display-message", "-p", "-t", self.win, self.fmt[name]],
                           text=True, capture_output=True, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.rstrip("\n")

    def test_the_one_spot(self):
        self.assertEqual(self.glob, {"@agent_limit_fg": "#fab387", "@agent_limit_glyph": "󱎫"})

    def test_limited_and_lifted_show_the_peach_glyph(self):
        lim = {"@agent_state": "paused", "@agent_summary": "proj/thing", "@agent_limit": "9 seven_day 1"}
        for name in ("default_text", "current_text"):
            with self.subTest(name=name):
                out = self.render(name, **lim)
                self.assertTrue(out.startswith("#[fg=#fab387]󱎫 "), out)
                self.assertNotIn("⏸", out)
                self.assertTrue(out.endswith("]proj/thing"), out)
                out = self.render(name, **dict(lim, **{"@agent_limit_lifted": "5"}))
                self.assertTrue(out.startswith("#[fg=#fab387]󱎫 "), out)       # one colour, lifted too
                out = self.render(name, **{"@agent_state": "paused"})
                self.assertTrue(out.startswith("#[fg=#7f6bff]⏸ "), out)       # a plain pause is unchanged
                out = self.render(name, **dict(lim, **{"@stash_sel": "1"}))
                self.assertTrue(out.startswith("#[fg=#11111b]󱎫 "), out)
                self.assertNotIn("󱎫", self.render(name, **dict(lim, **{"@agent_workflow": "1"})))
                # A limit kept through a running resume shows nothing extra.
                self.assertNotIn("󱎫", self.render(name, **dict(lim, **{"@agent_state": "running"})))

    def test_an_idle_limited_window_shows_it_too(self):
        # E1: the engine flips paused -> idle as it sends its RESUME; a paste
        # held behind a draft leaves idle + @agent_limit (maybe lifted).
        lim = {"@agent_summary": "proj/thing", "@agent_limit": "9 seven_day 1"}
        for name in ("default_text", "current_text"):
            with self.subTest(name=name):
                for extra in ({}, {"@agent_limit_lifted": "5"}):
                    out = self.render(name, **dict(lim, **{"@agent_state": "idle"}, **extra))
                    self.assertTrue(out.startswith("#[fg=#fab387]󱎫 "), out)
                for st in ("running", "failed", "done", "needs-input"):
                    self.assertNotIn("󱎫", self.render(name, **dict(lim, **{"@agent_state": st})), st)
                self.assertNotIn("󱎫", self.render(name, **{"@agent_state": "idle"}))
                self.assertNotIn("⏸", self.render(name, **{"@agent_state": "idle"}))

    def test_limited_is_neither_attention_nor_motion(self):
        for name in ("default_background", "current_background"):
            with self.subTest(name=name):
                self.assertEqual(self.render(name, **{"@agent_state": "paused", "@agent_limit": "9 x 1"}),
                                 self.render(name, **{"@agent_state": "idle"}))


if __name__ == "__main__":
    unittest.main()
