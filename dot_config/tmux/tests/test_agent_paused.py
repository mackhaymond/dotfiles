"""The `paused` agent state (docs/agent-pause.md, "The paused state";
docs/agent-tab-indicator.md, "Agent-originated turns"): while agent-pause.py's
@agent_paused flag is set, a turn end that would write done or idle, or do a
quiet restore, writes `paused` instead; the watcher's reconciles leave a
paused window alone and end a flagged turn as paused; the tab shows a
electric violet ⏸ in the glyph slot.

Indicator tests reuse test_agent_origin's private-server harness
(OriginHarness: a real `tmux -L <unique> -f /dev/null`, killed in cleanup);
watcher tests its fake-tmux harness (WatcherHarness); the tab glyph is
rendered from tmux.conf.tmpl's own format strings on a private server.
agent-pause.py's side (set the flag before the PAUSE; unset it and turn
paused -> idle before the RESUME) is reproduced with the exact tmux commands
its mark_paused() sends. Run with unittest or pytest.
"""
import json
import os
import re
import subprocess
import time
import unittest
from pathlib import Path

import test_agent_origin as ao
from test_agent_state_notice import write_exe

TMUX_CONF = ao.SCRIPTS.parent / "tmux.conf.tmpl"


class PausedHookTests(ao.OriginHarness):
    def pause(self):
        """agent-pause.py, before its PAUSE goes out."""
        self.set_opt("@agent_paused", str(int(time.time())))

    def unpause(self):
        """agent-pause.py's mark_paused(off), before the RESUME: one call."""
        r = subprocess.run(self.tmuxcmd + [
            "set-option", "-wu", "-t", self.pane, "@agent_paused", ";",
            "if-shell", "-F", "-t", self.pane, "#{==:#{@agent_state},paused}",
            "set-option -w -t %s @agent_state idle" % self.pane], text=True, capture_output=True, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)

    def bg(self, out):
        stub = self.home / ".local/bin/agent-bg-pending"
        if out:
            write_exe(stub, "#!/bin/sh\nexit 0\n")
        else:
            stub.unlink(missing_ok=True)

    PAUSE = "[pause key] PAUSE: Mack is stepping away. Stop at a safe point."
    RESUME = "[pause key] RESUME: Mack is back. Pick up where you paused."

    # --- the paused Stop ---

    def test_users_own_turn_ends_paused_not_done(self):
        self.prompt("build it", "u1")
        self.pause()
        self.stop("u1")
        self.assertEqual(self.opt("@agent_state"), "paused")
        self.assertRegex(self.opt("@agent_since"), r"^\d+ paused$")
        self.assertEqual(self.opt("@agent_turn_result"), "u1 paused")

    def test_pause_queued_mid_turn_ends_that_turn_paused(self):
        self.prompt("build it", "u1")
        self.pause()
        self.prompt(self.deliver(self.PAUSE, frm="pause-resume-agents"), "u1")   # joins the turn
        self.stop("u1")
        self.assertEqual(self.opt("@agent_state"), "paused")
        self.assertEqual(self.opt("@agent_turn_result"), "u1 paused")

    def test_paused_beats_the_quiet_restore(self):
        self.set_opt("@agent_state", "idle")
        self.pause()
        self.prompt(self.deliver(self.PAUSE, frm="pause-resume-agents"), "p1")
        self.assertEqual(self.opt("@agent_turn_origin"), "agent p1 pause-resume-agents")
        self.stop("p1")
        self.assertEqual(self.opt("@agent_state"), "paused")
        self.assertEqual(self.opt("@agent_turn_result"), "p1 paused")
        self.assertFalse(self.has("@agent_quiet"))          # no "replied" log line for a pause
        self.assertEqual(self.opt("@agent_prior_state"), "idle")   # kept for after the resume

    def test_flagged_for_mack_still_ends_paused(self):
        self.set_opt("@agent_state", "idle")
        self.prompt(self.deliver("deploy?"), "p1")
        self.set_opt("@agent_for_user", "1 needs your OK")
        self.pause()
        self.stop("p1")
        self.assertEqual(self.opt("@agent_state"), "paused")

    def test_bg_held_while_paused_stays_running(self):
        self.prompt("build it", "u1")
        self.pause()
        self.bg(True)
        self.stop("u1")
        self.assertEqual(self.opt("@agent_state"), "running")
        self.assertEqual(self.opt("@agent_turn_result"), "u1 paused")
        self.assertEqual(self.opt("@agent_bg_owner"), "user")

    def test_needs_input_and_failed_are_unchanged(self):
        self.prompt("build it", "u1")
        self.pause()
        self.hook("needs-approval", "claude", payload={"session_id": "s1", "tool_name": "Bash",
                                                       "tool_input": {"command": "ls"}})
        self.assertEqual(self.opt("@agent_state"), "needs-input")
        self.hook("failed", "claude", payload={"session_id": "s1", "prompt_id": "u1", "error": "overloaded"})
        self.assertEqual(self.opt("@agent_state"), "failed")
        self.assertEqual(self.opt("@agent_turn_result"), "u1 loud")

    def test_interrupt_while_paused_is_paused(self):
        self.prompt("build it", None, agent="opencode")
        self.pause()
        self.hook("interrupt", "opencode", payload={"session_id": "s1", "cwd": str(self.root)})
        self.assertEqual(self.opt("@agent_state"), "paused")

    def test_focus_leaves_paused_alone(self):
        self.prompt("build it", "u1"); self.pause(); self.stop("u1")
        self.clear_current(**self.VIEWING)
        self.assertEqual(self.opt("@agent_state"), "paused")
        self.assertFalse(self.has("@agent_seen_at"))

    def test_session_end_drops_the_flag(self):
        self.prompt("build it", "u1"); self.pause(); self.stop("u1")
        self.hook("clear", "claude", payload={"session_id": "s1", "reason": "other"})
        self.assertFalse(self.has("@agent_paused"))
        self.assertFalse(self.has("@agent_state"))

    # --- across the resume ---

    def test_unseen_green_from_before_the_pause_comes_back_after_resume(self):
        since = self.seed_done()
        self.pause()
        self.prompt(self.deliver(self.PAUSE, frm="pause-resume-agents"), "p1")
        self.assertEqual(self.opt("@agent_prior_state"), "done")
        self.stop("p1")
        self.assertEqual(self.opt("@agent_state"), "paused")
        self.assertEqual(self.opt("@agent_prior_state"), "done")
        # A ping while still paused neither saves `paused` nor loses the green.
        self.prompt(self.deliver("status?"), "p2")
        self.assertEqual(self.opt("@agent_prior_state"), "done")
        self.stop("p2")
        self.assertEqual(self.opt("@agent_state"), "paused")
        self.unpause()
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.prompt(self.deliver(self.RESUME, frm="pause-resume-agents"), "r1")
        self.assertEqual(self.opt("@agent_prior_state"), "done")    # not the resume's idle
        self.stop("r1")
        self.assertEqual((self.opt("@agent_state"), self.opt("@agent_since")), ("done", since))
        self.assertEqual(self.opt("@agent_turn_result"), "r1 quiet done")

    def test_green_seen_during_the_pause_stays_seen(self):
        self.seed_done()
        self.pause()
        self.prompt(self.deliver(self.PAUSE, frm="pause-resume-agents"), "p1")
        self.stop("p1")
        self.clear_current()                                 # Mack looks at the paused tab
        self.assertEqual(self.opt("@agent_prior_state"), "idle")
        self.unpause()
        self.prompt(self.deliver(self.RESUME, frm="pause-resume-agents"), "r1")
        self.stop("r1")
        self.assertEqual(self.opt("@agent_state"), "idle")

    def test_resume_turn_is_never_paused_and_a_typed_turn_is_loud(self):
        self.set_opt("@agent_state", "idle")
        self.pause()
        self.prompt(self.deliver(self.PAUSE, frm="pause-resume-agents"), "p1")
        self.stop("p1")
        self.unpause()
        self.prompt("ok, carry on with the parser", "u1")       # Mack types instead
        self.assertFalse(self.has("@agent_prior_state"))
        self.stop("u1")
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertEqual(self.opt("@agent_turn_result"), "u1 loud")

    # --- review round 1 ---

    def test_resume_of_macks_paused_turn_is_his_and_loud(self):
        self.prompt("refactor the parser", "u1")
        self.pause()
        self.prompt(self.deliver(self.PAUSE, frm="pause-resume-agents"), "u1")   # joins his turn
        self.stop("u1")
        self.assertEqual(self.opt("@agent_state"), "paused")
        self.unpause()
        out = self.prompt(self.deliver(self.RESUME, frm="pause-resume-agents"), "r1")
        self.assertEqual(out, "")                              # no "Mack won't be notified"
        self.assertEqual(self.opt("@agent_turn_origin"), "user r1 -")
        self.assertFalse(self.has("@agent_prior_state"))
        self.stop("r1", msg="Parser refactor finished.")
        self.assertEqual(self.opt("@agent_state"), "done")
        self.assertEqual(self.opt("@agent_turn_result"), "r1 loud")

    def test_resume_of_an_agent_started_paused_turn_stays_quiet(self):
        self.set_opt("@agent_state", "idle")
        self.prompt(self.deliver("run the migration"), "a1")
        self.pause()
        self.prompt(self.deliver(self.PAUSE, frm="pause-resume-agents"), "a1")
        self.stop("a1")
        self.unpause()
        out = self.prompt(self.deliver(self.RESUME, frm="pause-resume-agents"), "r1")
        self.assertTrue(out.strip())
        self.assertEqual(self.opt("@agent_turn_origin"), "agent r1 pause-resume-agents")
        self.stop("r1")
        self.assertEqual(self.opt("@agent_state"), "idle")
        self.assertEqual(self.opt("@agent_turn_result"), "r1 quiet idle")

    def test_fresh_session_drops_a_stale_flag(self):
        for source in ("clear", "startup"):
            with self.subTest(source=source):
                self.pause()
                self.hook("idle", "claude", payload={"session_id": "s2", "cwd": str(self.root), "source": source})
                self.assertFalse(self.has("@agent_paused"))
                self.prompt("new work", "n1"); self.stop("n1")
                self.assertEqual(self.opt("@agent_state"), "done")
        for source in ("resume", "compact"):
            with self.subTest(source=source):
                self.pause()
                self.hook("idle", "claude", payload={"session_id": "s2", "cwd": str(self.root), "source": source})
                self.assertTrue(self.has("@agent_paused"))

    def test_flag_cleared_between_read_and_write_lands_idle(self):
        # agent-pause.py's resume clears the flag right after the hook read it.
        write_exe(self.bin / "tmux", """#!/bin/sh
case "$*" in *'#{window_active_clients}'*) echo 0; exit 0 ;; esac
case "$*" in
  *display-message*'#{@agent_paused}'*)
    "%s" "$@"; rc=$?
    "%s" set-option -uw -t "$RACE_WIN" @agent_paused
    exit $rc ;;
esac
exec "%s" "$@"
""" % (ao.REAL_TMUX, ao.REAL_TMUX, ao.REAL_TMUX))
        self.prompt("build it", "u1")
        self.pause()
        self.stop("u1", RACE_WIN=self.win)
        self.assertFalse(self.has("@agent_paused"))
        self.assertEqual(self.opt("@agent_state"), "idle")    # never a flagless paused
        # Control: with the flag still there the same guard keeps paused.
        write_exe(self.bin / "tmux", ao.FAKE_TMUX_WRAPPER % ao.REAL_TMUX)
        self.prompt("again", "u2"); self.pause(); self.stop("u2")
        self.assertEqual(self.opt("@agent_state"), "paused")

    def test_ping_while_macks_turn_is_paused_still_ends_paused(self):
        self.prompt("build it", "u1"); self.pause(); self.stop("u1")    # Mack's turn, paused
        self.prompt(self.deliver("status?"), "p2")
        self.assertEqual(self.opt("@agent_turn_origin"), "user p2 -")   # still his work
        self.stop("p2")
        self.assertEqual(self.opt("@agent_state"), "paused")
        self.assertEqual(self.opt("@agent_turn_result"), "p2 paused")


class PausedWatcherTests(ao.WatcherHarness):
    def set_opts(self, **opts):
        st = self.f.read(); st["windows"]["@1"]["opts"].update(opts)
        self.f.state.write_text(json.dumps(st))

    def test_bg_drain_while_paused_writes_paused_and_keeps_the_prior(self):
        self.set_opts(**{"@agent_paused": "1", "@agent_bg_owner": "user", "@agent_pending": "1"})
        o = self.ticks(5, FAKE_BG_RC="1")
        self.assertEqual(o["@agent_state"], "paused")
        self.assertRegex(o["@agent_since"], r"^\d+ paused$")
        self.assertEqual(o["@agent_prior_state"], "done")     # for after the resume
        for name in ("@agent_bg_owner", "@agent_pending", "@agent_quiet"):
            self.assertNotIn(name, o)

    def test_bg_still_out_while_paused_holds_running(self):
        self.set_opts(**{"@agent_paused": "1"})
        o = self.ticks(4, FAKE_BG_RC="0")
        self.assertEqual(o["@agent_state"], "running")

    def test_paused_window_is_left_alone(self):
        self.set_clients(1)                                   # viewed, WezTerm front
        self.set_opts(**{"@agent_state": "paused", "@agent_since": "100 paused", "@agent_paused": "1"})
        o = self.ticks(4, FAKE_BG_RC="1", FAKE_FRONT="wez")
        self.assertEqual((o["@agent_state"], o["@agent_since"]), ("paused", "100 paused"))
        self.assertEqual(o["@agent_prior_state"], "idle")    # the saved green is seen, though
        self.assertIn("@agent_seen_at", o)
        # ...and a paused state with no flag (agent-pause died) is not moved either.
        self.set_opts(**{"@agent_paused": ""})
        st = self.f.read(); st["windows"]["@1"]["opts"].pop("@agent_paused"); self.f.state.write_text(json.dumps(st))
        o = self.ticks(3, FAKE_BG_RC="1")
        self.assertEqual(o["@agent_state"], "paused")

    # The shared fake runs if-shell -F itself now (the watcher's cas_write,
    # whose guarded list nests the `#{?#{@agent_paused},0,1}` re-check -
    # 2026-10-22). This wrapper only stages the race: with RACE=1 it clears
    # the flag right before the call carrying the paused write -
    # agent-pause.py's resume landing between the tick's read and it.
    IF_SHELL = r'''#!/usr/bin/env python3
import os, subprocess, sys
real = os.environ["REAL_FAKE_TMUX"]
if os.environ.get("RACE") == "1" and any("@agent_state paused" in a for a in sys.argv[1:]):
    subprocess.run([real, "set-option", "-uw", "-t", "@1", "@agent_paused"])
os.execv(real, [real] + sys.argv[1:])
'''

    def with_if_shell(self):
        real = self.f.bin / "tmux.fake"
        (self.f.bin / "tmux").rename(real)
        write_exe(self.f.bin / "tmux", self.IF_SHELL)
        return str(real)

    def test_flag_cleared_before_the_watchers_write_lands_idle(self):
        real = self.with_if_shell()
        self.set_opts(**{"@agent_paused": "1"})
        o = self.ticks(5, FAKE_BG_RC="1", REAL_FAKE_TMUX=real, RACE="1")
        self.assertNotIn("@agent_paused", o)
        self.assertEqual(o["@agent_state"], "idle")

    def test_flag_still_set_keeps_paused_through_the_guard(self):
        real = self.with_if_shell()
        self.set_opts(**{"@agent_paused": "1"})
        o = self.ticks(5, FAKE_BG_RC="1", REAL_FAKE_TMUX=real, RACE="0")
        self.assertEqual(o["@agent_state"], "paused")

    def test_gc_drops_the_flag(self):
        st = self.f.read(); st["panes"] = [{"window": "@1", "tty": "/dev/ttys903"}]   # agent gone
        st["windows"]["@1"]["opts"].update({"@agent_state": "paused", "@agent_paused": "1"})
        self.f.state.write_text(json.dumps(st))
        o = self.ticks(6)
        self.assertNotIn("@agent_paused", o)
        self.assertNotIn("@agent_state", o)


@unittest.skipUnless(ao.REAL_TMUX, "tmux executable required")
class PausedGlyphTests(unittest.TestCase):
    """tmux.conf.tmpl's tab formats, rendered by a real private tmux."""

    @classmethod
    def setUpClass(cls):
        text = TMUX_CONF.read_text()
        cls.fmt = {}
        for name in ("default_text", "current_text", "default_background", "current_background",
                     "default_color", "current_color"):
            m = re.search(r'^set -g @catppuccin_window_%s "(.*)"$' % name, text, re.M)
            assert m, name
            cls.fmt[name] = m.group(1)

    def setUp(self):
        self.server = "agent-paused-glyph-" + str(time.time_ns())
        self.tm = [ao.REAL_TMUX, "-L", self.server, "-f", "/dev/null"]
        clean = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        r = subprocess.run(self.tm + ["new-session", "-d", "-s", "g", "-P", "-F", "#{window_id}|#{socket_path}",
                                      "/bin/sleep 600"], text=True, capture_output=True, env=clean, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.win, self.socket = r.stdout.strip().split("|")
        self.addCleanup(self.kill)

    def kill(self):
        subprocess.run(self.tm + ["kill-server"], capture_output=True, timeout=5)
        Path(self.socket).unlink(missing_ok=True)

    def render(self, name, **opts):
        for k in ("@agent_state", "@agent_workflow", "@agent_cua", "@stash_sel", "@agent_summary"):
            subprocess.run(self.tm + ["set-option", "-uw", "-t", self.win, k], capture_output=True, timeout=5)
        for k, v in opts.items():
            subprocess.run(self.tm + ["set-option", "-w", "-t", self.win, k, v], check=True, timeout=5)
        r = subprocess.run(self.tm + ["display-message", "-p", "-t", self.win, self.fmt[name]],
                           text=True, capture_output=True, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.rstrip("\n")

    def test_paused_shows_a_violet_glyph_in_both_formats(self):
        for name in ("default_text", "current_text"):
            with self.subTest(name=name):
                out = self.render(name, **{"@agent_state": "paused", "@agent_summary": "proj/thing"})
                self.assertTrue(out.startswith("#[fg=#7f6bff]⏸ "), out)
                self.assertTrue(out.endswith("]proj/thing"), out)
                self.assertNotIn("⏸", self.render(name, **{"@agent_state": "idle"}))
                self.assertNotIn("⏸", self.render(name, **{"@agent_state": "done"}))
                # Slot priority: gear > paused > mouse; stash selection darkens it.
                self.assertNotIn("⏸", self.render(name, **{"@agent_state": "paused", "@agent_workflow": "1"}))
                out = self.render(name, **{"@agent_state": "paused", "@agent_cua": "1"})
                self.assertIn("⏸", out); self.assertNotIn("󰍽", out)
                out = self.render(name, **{"@agent_state": "paused", "@stash_sel": "1"})
                self.assertTrue(out.startswith("#[fg=#11111b]⏸ "), out)

    def test_paused_is_neither_attention_nor_motion(self):
        # Stock body, solid chip: exactly what idle renders.
        for name in ("default_background", "current_background", "default_color", "current_color"):
            with self.subTest(name=name):
                self.assertEqual(self.render(name, **{"@agent_state": "paused"}),
                                 self.render(name, **{"@agent_state": "idle"}))


if __name__ == "__main__":
    unittest.main()
