"""stash.sh: snooze (2026-11-13) — `snooze`, `set-wake`, `wake`, the rows'
7th field, the sidecar's field 12, and the `snooze.sh reschedule` nudge.

Same harness as test_stash_freeze.py (a private `-L` tmux started with
`-f /dev/null`, HOME/TMPDIR/XDG_STATE_HOME in a temp dir, everything killed and
removed in cleanup); closed-tabs.sh and snooze.sh are stubs that record their
calls. The pin stub prints the new entry's id, as `close --pin` does.
"""
import json
from pathlib import Path
import time
import unittest

from test_stash_freeze import Base, SEP, STUB, STASH as STASH_PATH

PIN_STUB = STUB.replace(
    'pin=""; [ "$1" = close ] && [ "$2" = --pin ] && { pin=1; set -- close "$3"; }',
    'pin=""; wake=""\n'
    'if [ "$1" = close ] && [ "$2" = --pin ]; then\n'
    '    pin=1\n'
    '    if [ "$3" = --wake ]; then wake="$4"; set -- close "$5"; else set -- close "$3"; fi\n'
    'fi').replace(
    'echo "$1${pin:+ --pin} $2 $where" >> "$calls"',
    'echo "$1${pin:+ --pin}${wake:+ --wake $wake} $2 $where" >> "$calls"').replace(
    'pin) tmux kill-pane -t "$2"; exit 0 ;;',
    'pin) tmux kill-pane -t "$2"; echo "1700000000-42"; exit 0 ;;')


class Snooze(Base):
    def setUp(self):
        super().setUp()
        self.stub.write_text(PIN_STUB)
        self.resched = Path(self.tmp, "resched-calls")
        snooze = Path(self.tmp, "snooze.sh")
        snooze.write_text(f'#!/bin/bash\necho "$*" >> "{self.resched}"\n')
        self.env["SNOOZE_SH"] = str(snooze)
        self.when = str(int(time.time()) + 3600)

    def rescheduled(self, n=1):
        self.wait_for(lambda: self.resched.exists() and self.resched.read_text().count("reschedule") >= n,
                      "snooze.sh reschedule was not run")

    def rows(self):
        return {r.split(SEP)[0]: r.split(SEP) for r in self.script("rows").splitlines()}

    def fake(self, name):
        f = Path(self.tmp, "bin", name)
        f.parent.mkdir(exist_ok=True)
        import shutil
        shutil.copy("/bin/sleep", f)
        return f

    # -- snooze ------------------------------------------------------------
    def test_a_tab_is_parked_frozen_and_gets_its_alarm(self):
        self.keep_one()
        w = self.new_window()
        job = self.run_job(self.pane(w), "sleep 1000")
        out = self.script("snooze", self.when, w)
        self.assertEqual(out, f"w:{w}\n")
        self.assertEqual(self.session_of(w), "stash")
        self.assertTrue(self.state(job).startswith("T"))
        self.assertEqual(self.opt(w, "@stash_wake"), self.when)
        row = self.rows()[w]
        self.assertEqual(len(row), 7)
        self.assertEqual((row[4], row[6]), ("frozen", self.when))
        self.rescheduled()

    def test_a_one_pane_agent_is_pinned_with_the_wake(self):
        self.mode.write_text("pin")
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        self.run_job(p, f"'{self.fake('codex')}' 1000")
        out = self.script("snooze", self.when, w)
        self.assertEqual(out, "p:1700000000-42\n")
        self.assertEqual(self.calls.read_text().split()[:5], ["close", "--pin", "--wake", self.when, p])
        self.assertEqual(self.session_of(w), "")

    def test_a_refusal_snoozes_nothing_and_says_what_stash_says(self):
        self.keep_one()
        w = self.new_window()
        self.run_job(self.pane(w), f"'{self.fake('ssh')}' 1000")
        self.assertEqual(self.script("snooze", self.when, w), "")
        self.assertEqual(self.session_of(w), "main")
        self.assertEqual(self.opt(w, "@stash_wake"), "")
        self.assertIn("ssh in the foreground", self.log())
        time.sleep(0.3)
        self.assertFalse(self.resched.exists())

    def test_a_plain_stash_has_no_alarm_and_prints_nothing(self):
        self.keep_one()
        w = self.new_window()
        self.t("set", "-w", "-t", w, "@stash_wake", "123")      # stale, from nowhere
        self.assertEqual(self.script("stash", w), "")
        self.assertEqual(self.opt(w, "@stash_wake"), "")
        self.assertEqual(self.rows()[w][6], "")

    def test_a_bad_wake_time_does_nothing(self):
        self.keep_one()
        w = self.new_window()
        self.script("snooze", "soon", w)
        self.assertEqual(self.session_of(w), "main")

    # -- set-wake ----------------------------------------------------------
    def test_set_wake_sets_and_clears_only_parked_windows(self):
        self.keep_one()
        w = self.new_window()
        other = self.new_window()
        self.script("stash", w)
        self.script("set-wake", self.when, w, other)
        self.assertEqual(self.opt(w, "@stash_wake"), self.when)
        self.assertEqual(self.opt(other, "@stash_wake"), "")       # not parked: skipped
        self.rescheduled(1)
        self.script("set-wake", "", w)
        self.assertEqual(self.opt(w, "@stash_wake"), "")
        self.assertEqual(self.session_of(w), "stash")              # still stashed
        self.rescheduled(2)

    # -- wake --------------------------------------------------------------
    def test_wake_brings_it_home_at_the_end_thawed_without_switching(self):
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        self.t("rename-window", "-t", w, "devsrv")
        self.t("set", "-w", "-t", w, "automatic-rename", "off")
        job = self.run_job(p, "sleep 1000")
        self.script("snooze", self.when, w)
        self.new_window()                                          # another tab after it
        self.t("select-window", "-t", "main:1")
        active = self.t("display", "-p", "-t", "main:", "#{window_id}")
        out = self.script("wake", w)
        self.assertEqual(out, "devsrv\tmain\n")
        self.assertEqual(self.session_of(w), "main")
        last = self.t("list-windows", "-t", "main", "-F", "#{window_id}").split()[-1]
        self.assertEqual(last, w)                                  # end of the bar
        self.assertEqual(self.t("display", "-p", "-t", "main:", "#{window_id}"), active)
        self.assertEqual(self.opt(w, "@stash_wake"), "")
        self.assertEqual(self.opt(w, "@stash_frozen"), "")
        self.wait_for(lambda: self.tpgid(p) == job and not self.state(job).startswith("T"), "not thawed")
        self.rescheduled(2)

    def test_wake_with_its_origin_gone_lands_in_main(self):
        self.keep_one()
        self.t("new-session", "-d", "-s", "work")
        self.t("new-window", "-d", "-t", "work:")
        w = self.t("new-window", "-d", "-t", "work:", "-P", "-F", "#{window_id}")
        self.script("snooze", self.when, w)
        self.t("kill-session", "-t", "=work")
        out = self.script("wake", w)
        self.assertTrue(out.endswith("\tmain\n"), out)
        self.assertEqual(self.session_of(w), "main")

    def test_wake_never_lands_in_a_hidden_session(self):
        self.keep_one()
        w = self.new_window()
        self.script("snooze", self.when, w)
        self.t("new-session", "-d", "-s", "agents")
        self.t("set", "-w", "-t", w, "@stash_origin", "agents")
        self.script("wake", w)
        self.assertEqual(self.session_of(w), "main")

    def test_wake_skips_what_is_not_parked(self):
        self.keep_one()
        w = self.new_window()
        self.assertEqual(self.script("wake", w, "@999"), "")
        self.assertEqual(self.session_of(w), "main")

    # -- early unpark and discard ------------------------------------------
    def test_unstash_before_the_alarm_clears_it(self):
        self.keep_one()
        w = self.new_window()
        self.script("snooze", self.when, w)
        self.rescheduled(1)
        self.script("unstash-many", w)
        self.assertEqual(self.session_of(w), "main")
        self.assertEqual(self.opt(w, "@stash_wake"), "")
        self.rescheduled(2)

    def test_kill_many_of_a_snoozed_window_reschedules(self):
        self.keep_one()
        w = self.new_window()
        self.script("snooze", self.when, w)
        self.rescheduled(1)
        self.script("kill-many", w)
        self.assertEqual(self.session_of(w), "")
        self.rescheduled(2)

    # -- surviving a restart ------------------------------------------------
    def test_the_alarm_and_the_job_survive_a_restart(self):
        self.keep_one()
        w = self.new_window()
        p = self.pane(w)
        job = self.run_job(p, "sleep 1000")
        self.script("snooze", self.when, w)
        self.script("publish")
        rows = [r.split(SEP) for r in (self.rdir / "stash-state.tsv").read_text().splitlines()]
        parked = [r for r in rows if r[0] == "stash"]
        self.assertEqual(len(parked[0]), 12)
        self.assertEqual(parked[0][11], self.when)
        self.assertEqual(json.loads(parked[0][10])[0]["cmd"], "sleep 1000")

        self.reap()
        self.wait_for(lambda: self.state(job) == "", "the frozen job outlived its server")
        self.start_server()
        self.keep_one()
        self.t("new-session", "-d", "-s", "stash", "-c", str(self.work))
        w2 = self.t("list-windows", "-t", "stash", "-F", "#{window_id}")
        self.wait_prompt(self.pane(w2))
        n = self.resched.read_text().count("reschedule")
        self.script("restore-state")
        self.assertEqual(self.opt(w2, "@stash_wake"), self.when, self.log())
        self.assertEqual(self.opt(w2, "@stash_cold"), "1")
        self.rescheduled(n + 1)
        self.script("wake", w2)
        p2 = self.pane(w2)
        self.wait_for(lambda: self.tpgid(p2) != self.pane_pid(p2), "the command was not re-run")
        new = self.tpgid(p2)
        self.pids.append(new)
        self.wait_for(lambda: self.argv_of(new) == "sleep 1000", "wrong command re-run")

    # -- check (round 2) ---------------------------------------------------
    def check(self, *args):
        import subprocess
        r = subprocess.run(["bash", str(STASH_PATH), "check", *args], env=self.script_env(),
                           capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout

    def test_check_says_yes_with_no_side_effect(self):
        self.keep_one()
        w = self.new_window()
        job = self.run_job(self.pane(w), "sleep 1000")
        self.assertEqual(self.check(w), (0, ""))
        self.assertEqual(self.session_of(w), "main")
        self.assertFalse(self.state(job).startswith("T"))
        self.assertEqual(self.opt(w, "@stash_frozen"), "")
        self.assertNotIn("stash", self.t("list-sessions", "-F", "#{session_name}").split())
        self.assertEqual(self.log(), "")

    def test_check_gives_the_refusal_and_3(self):
        self.keep_one()
        w = self.new_window()
        job = self.run_job(self.pane(w), f"'{self.fake('ssh')}' 1000")
        self.assertEqual(self.check(w), (3, "ssh in the foreground — not parking it\n"))
        self.assertEqual(self.check("--snooze", w), (3, "ssh in the foreground — not snoozing it\n"))
        self.assertEqual(self.session_of(w), "main")
        self.assertFalse(self.state(job).startswith("T"))
        self.assertNotIn("refused", self.log())

    def test_check_covers_home_only_tab_and_already_parked(self):
        home = self.t("list-windows", "-t", "main", "-F", "#{window_id}").split()[0]
        self.t("set", "-w", "-t", home, "@space_home", "1")
        self.assertEqual(self.check(home), (3, "that's the space's home tab — it stays\n"))
        self.t("new-session", "-d", "-s", "solo")
        solo = self.t("list-windows", "-t", "solo", "-F", "#{window_id}")
        self.assertEqual(self.check(solo), (3, "that's the only tab in this session — not parking it\n"))
        self.keep_one()
        w = self.new_window()
        self.script("stash", w)
        self.assertEqual(self.check(w), (3, "already parked\n"))

    def test_check_runs_the_deep_guards(self):
        self.keep_one()
        w = self.new_window()
        self.run_job(self.pane(w), f"'{self.fake('claude')}' 1000")
        # A fake claude shows no composer: "a draft may be there" — refused.
        self.assertEqual(self.check(w), (3, "unsent text in the agent's composer — not parking it\n"))
        self.assertFalse(self.calls.exists())                     # never offered to close --pin

    def test_a_snooze_refusal_says_not_snoozing(self):
        said = Path(self.tmp, "said")
        rec = Path(self.tmp, "notify-rec")
        rec.write_text(f'#!/bin/sh\necho "$*" >> "{said}"\n')
        rec.chmod(0o755)
        self.env["NOTIFY_SH"] = str(rec)
        self.keep_one()
        w = self.new_window()
        self.run_job(self.pane(w), f"'{self.fake('ssh')}' 1000")
        self.script("snooze", self.when, w)
        self.assertIn("ssh in the foreground — not snoozing it", said.read_text())
        self.script("stash", w)
        self.assertIn("ssh in the foreground — not parking it", said.read_text().splitlines()[-1])

    def test_previews_follow_xdg_state_home(self):
        out = self.source('printf "%s" "$PREVIEW_DIR"')
        self.assertEqual(out, str(Path(self.env["XDG_STATE_HOME"], "tmux-stash", "previews")))

    def test_a_wake_only_row_keeps_fields_10_and_11_in_place(self):
        self.keep_one()
        w = self.new_window()
        self.script("snooze", self.when, w)
        self.script("publish")
        row = [r.split(SEP) for r in (self.rdir / "stash-state.tsv").read_text().splitlines()
               if r.startswith("stash" + SEP)][0]
        self.assertEqual(len(row), 12)
        self.assertEqual((row[10], row[11]), ("", self.when))
        self.assertTrue(row[9])                                    # icwd still there


if __name__ == "__main__":
    unittest.main()
