"""closed-tabs.sh: snooze (plan snooze-plan.md, unit S1).

Snoozed = a pinned entry with "wake_at" (epoch). `snooze <epoch> <id…>` pins if
needed and sets it, `unsnooze` drops it (still pinned), unpin / forget /
reopen drop it with the pin, `close --pin --wake <epoch>` makes one straight
from a tab (and every `close --pin` prints the new id), `wake <id…>` brings
entries back at the end of their own space's bar (main when that is gone or
hidden) without moving a client and prints "<label>\\t<session>" per entry.
Every wake_at change runs `snooze.sh reschedule` (detached). The picker gains
a Snoozed section (entries + stash rows with a 7th, wake, field), soonest
first, labelled "in 30m" / "tmrw 9:00" / "mon 9:00"; ⌃z hands the picked keys
to `snooze.sh menu <tty> <keys…>`.

snooze.sh and stash.sh are fakes (SNOOZE_SH / STASH_SH) that log their calls;
the server tests run on a private `-L` tmux server (test_closed_tabs_pin's
ServerBase: `-f /dev/null`, its own TMUX_TMPDIR, killed in teardown).
"""
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import unittest

from test_closed_tabs_pin import ANSI, SCRIPT, TMUX, Base, ServerBase, entry


def local_epoch(days_ahead, hour, minute=0):
    d = datetime.datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    return int((d + datetime.timedelta(days=days_ahead)).timestamp())


@unittest.skipUnless(shutil.which("jq"), "jq not installed")
class SnoozeBookkeeping(Base):
    """History, picker rows and ⌃z; tmux only records, no server."""

    def setUp(self):
        super().setUp()
        self.tmux_log = Path(self.tmp, "tmux.log")
        (self.bin / "tmux").write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.tmux_log}"\nexit 0\n')
        (self.bin / "tmux").chmod(0o755)
        self.now = int(time.time())

    def by_id(self):
        return {e["id"]: e for e in self.entries()}

    def test_snooze_pins_and_sets_the_alarm(self):
        self.write(entry("a", self.now - 100), entry("b", self.now - 50, pinned=True, stashed_ts=self.now - 40))
        wake = self.now + 3600
        self.run_script("snooze", str(wake), "a", "b")
        es = self.by_id()
        self.assertTrue(es["a"]["pinned"])
        self.assertAlmostEqual(es["a"]["stashed_ts"], self.now, delta=5)
        self.assertEqual(es["b"]["stashed_ts"], self.now - 40)          # already pinned: kept
        self.assertEqual((es["a"]["wake_at"], es["b"]["wake_at"]), (wake, wake))
        calls = self.snooze_calls("reschedule")
        self.assertEqual(calls[0], f"reschedule | {SCRIPT}")             # this copy as its CLOSED_TABS_SH
        self.stash_calls("publish-count")                               # a was newly pinned
        # A new alarm replaces the old one.
        self.run_script("snooze", str(wake + 60), "b")
        self.assertEqual(self.by_id()["b"]["wake_at"], wake + 60)
        # Refused: a bad epoch, no ids, ids not in the history.
        for args in (("soon", "a"), (str(wake),), (str(wake), "nope")):
            with self.subTest(args=args):
                self.run_script("snooze", *args, rc=1)

    def test_unsnooze_keeps_it_stashed(self):
        self.write(entry("a", self.now, pinned=True, stashed_ts=self.now, wake_at=self.now + 600),
                   entry("b", self.now, pinned=True, stashed_ts=self.now))
        self.run_script("unsnooze", "a")
        a = self.by_id()["a"]
        self.assertTrue(a["pinned"])
        self.assertNotIn("wake_at", a)
        self.snooze_calls("reschedule")
        # Nothing snoozed among them: nothing to reschedule.
        self.snooze_log.unlink()
        self.run_script("unsnooze", "b")
        time.sleep(0.3)
        self.assertEqual(self.snooze_calls(), [])
        self.run_script("unsnooze", "nope", rc=1)

    def test_leaving_the_shelf_drops_the_alarm(self):
        for verb in ("unpin", "forget"):
            with self.subTest(verb):
                self.write(entry("a", self.now, pinned=True, stashed_ts=self.now, wake_at=self.now + 600))
                if self.snooze_log.exists():
                    self.snooze_log.unlink()
                self.run_script(verb, "a")
                [a] = self.entries()
                self.assertNotIn("pinned", a)
                self.assertNotIn("wake_at", a)
                self.snooze_calls("reschedule")

    def test_pinned_json_carries_wake_at(self):
        self.write(entry("a", self.now, pinned=True, stashed_ts=self.now, wake_at=self.now + 600),
                   entry("b", self.now, pinned=True, stashed_ts=self.now - 5))
        out = {e["id"]: e for e in json.loads(self.run_script("pinned-json").stdout)}
        self.assertEqual(out["a"]["wake_at"], self.now + 600)
        self.assertNotIn("wake_at", out["b"])

    def rows(self, *args):
        out = self.run_script("pick-rows", *args).stdout
        return [ANSI.sub("", l).split("\t") for l in out.splitlines()]

    def test_picker_has_a_snoozed_section_soonest_first(self):
        tmrw9, in3d, in10d = local_epoch(1, 9), local_epoch(3, 9), local_epoch(10, 9)
        self.write(entry("old", self.now - 7200),
                   entry("s1", self.now - 100, pinned=True, stashed_ts=self.now - 100, wake_at=tmrw9),
                   entry("p1", self.now - 50, pinned=True, stashed_ts=self.now - 50),
                   entry("s2", self.now - 100, pinned=True, stashed_ts=self.now - 100, wake_at=self.now + 1800),
                   entry("s3", self.now - 100, pinned=True, stashed_ts=self.now - 100, wake_at=in10d),
                   entry("due", self.now - 100, pinned=True, stashed_ts=self.now - 100, wake_at=self.now - 30),
                   # a wake_at left on an unpinned entry is no alarm
                   entry("b1", self.now - 30, wake_at=self.now + 60))
        self.stash_rows.write_text(
            f"@7\x1fproj\x1fdev server\x1f{self.now - 3600}\x1ffrozen\x1f/tmp/web\x1f{in3d}\n"
            f"@8\x1fproj\x1fplain\x1f{self.now - 60}\x1fidle\x1f/tmp/p\x1f\n"
            f"@9\x1fmain\x1fold stash.sh\x1f{self.now - 90}\x1fidle\x1f/tmp/o\n"         # 6 fields
            # pending: on the bar already, never Snoozed whatever it carries
            f"@12\x1fwork\x1fresumer\x1f{self.now - 70}\x1fpending\x1f/tmp/r\x1f{self.now + 5}\n")
        rows = self.rows()
        keys = [r[0] for r in rows]
        self.assertEqual(keys, ["-", "p:due", "p:s2", "p:s1", "w:@7", "p:s3",
                                "-", "p:p1", "w:@8", "r:@12", "w:@9",
                                "-", "b:b1", "b:old"])
        self.assertIn("Snoozed", rows[0][1])
        self.assertIn("Stashed", rows[6][1])
        self.assertIn("Backlog", rows[11][1])
        when = {r[0]: r[1].split()[0:2] for r in rows if r[0] != "-"}
        self.assertEqual(when["p:due"][0], "due")
        self.assertEqual(when["p:s2"], ["in", "30m"])
        self.assertEqual(when["p:s1"], ["tmrw", "9:00"])
        wd = datetime.datetime.fromtimestamp(in3d).strftime("%a").lower()
        self.assertEqual(when["w:@7"], [wd, "9:00"])
        d10 = datetime.datetime.fromtimestamp(in10d)
        self.assertEqual(when["p:s3"], [d10.strftime("%b"), str(d10.day)])
        self.assertTrue(rows[8][1].startswith("1m ago"), rows[8])      # stashed rows keep their age
        # --stashed: Snoozed and Stashed, no Backlog.
        self.assertEqual([r[0] for r in self.rows("--stashed")], keys[:11])

    def test_past_fifty_minutes_reads_as_a_clock_time(self):
        # Never a rounded "in 1h": beside an "in 50m" row it read as sooner
        # than a row that was really later. Up to 50 minutes "in Nm"; past
        # that, today, the clock time it comes back at.
        near, far = self.now + 50 * 60, self.now + 55 * 60
        self.write(entry("near", self.now, pinned=True, stashed_ts=self.now, wake_at=near),
                   entry("far", self.now, pinned=True, stashed_ts=self.now, wake_at=far))
        when = {r[0]: r[1].split()[0:2] for r in self.rows() if r[0] != "-"}
        self.assertEqual(when["p:near"], ["in", "50m"])
        d = datetime.datetime.fromtimestamp(far)
        hm = f"{d.hour}:{d.minute:02d}"
        if d.date() == datetime.date.today():
            self.assertEqual(when["p:far"], ["at", hm])
        else:
            self.assertEqual(when["p:far"], ["tmrw", hm])

    def test_ctrl_z_hands_the_picked_rows_to_the_snooze_menu(self):
        notes = Path(self.tmp, "notes.log")
        Path(self.env["NOTIFY_SH"]).write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{notes}"\n')
        Path(self.env["NOTIFY_SH"]).chmod(0o755)
        self.run_script("pick-act", "ctrl-z", "$1", "/dev/ttys999", "-", "p:a", "b:b", "w:@3", "r:@4")
        [call] = self.snooze_calls("menu /dev/ttys999 p:a b:b w:@3")
        self.assertTrue(call.endswith("executable_closed-tabs.sh") or call.endswith("closed-tabs.sh"), call)
        # Only a pending row: nothing to snooze here.
        self.snooze_log.unlink()
        self.run_script("pick-act", "ctrl-z", "", "/dev/ttys999", "r:@4")
        self.assertIn("already back on the bar", notes.read_text())
        # No snooze.sh: said so, nothing else happens.
        self.env["SNOOZE_SH"] = os.path.join(self.tmp, "no-snooze.sh")
        self.run_script("pick-act", "ctrl-z", "", "/dev/ttys999", "p:a")
        self.assertIn("snooze.sh is not installed", notes.read_text())
        self.assertEqual(self.snooze_calls(), [])

    def test_the_picker_offers_ctrl_z(self):
        fzf_args = Path(self.tmp, "fzf.args")
        (self.bin / "fzf").write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{fzf_args}"\ncat >/dev/null\n')
        (self.bin / "fzf").chmod(0o755)
        self.write(entry("a", self.now, pinned=True, stashed_ts=self.now), entry("b", self.now))
        for args in (("$1", ""), ("--stashed", "$1", "")):
            with self.subTest(args=args):
                self.run_script("pick", *args)
                lines = fzf_args.read_text().splitlines()
                self.assertIn("⌃z snooze", lines[lines.index("--header") + 1])
                self.assertIn("ctrl-z", next(l for l in lines if l.startswith("--expect")))
        # And the popup carries SNOOZE_SH through the re-entry.
        self.run_script("pick-popup", "$1", "/dev/ttys999")
        popup = [l for l in self.tmux_log.read_text().splitlines() if l.startswith("display-popup")][-1]
        self.assertIn(f"SNOOZE_SH='{self.snooze}'", popup)


@unittest.skipUnless(TMUX and shutil.which("jq"), "tmux / jq not installed")
class SnoozeOnAServer(ServerBase):
    """close --pin --wake and wake, against a private tmux server."""

    def test_close_pin_prints_the_id_and_wake_snoozes_it(self):
        wake = int(time.time()) + 3600
        r = self.run_script("close", "--pin", "--wake", str(wake), self.agent_tab())
        [e] = self.entries()
        self.assertEqual(r.stdout, e["id"] + "\n")
        self.assertTrue(e["pinned"])
        self.assertEqual(e["wake_at"], wake)
        self.snooze_calls("reschedule")
        # Without --wake: still the id, no alarm, nothing rescheduled.
        self.snooze_log.unlink()
        r = self.run_script("close", "--pin", self.agent_tab())
        e = self.entries()[-1]
        self.assertEqual(r.stdout, e["id"] + "\n")
        self.assertNotIn("wake_at", e)
        # A plain close prints nothing.
        pane = self.t("new-window", "-d", "-t", "=main:", "-P", "-F", "#{pane_id}", "sleep 600")
        self.assertEqual(self.run_script("close", pane).stdout, "")
        time.sleep(0.3)
        self.assertEqual(self.snooze_calls(), [])

    def test_close_wake_is_checked_before_anything_happens(self):
        pane = self.agent_tab()
        for args in (("--pin", "--wake", "soon", pane), ("--wake", str(int(time.time())), pane),
                     ("--pin", "--wake")):
            with self.subTest(args=args):
                self.run_script("close", *args, rc=1)
                self.assertIn(pane, self.panes())
        self.assertEqual(self.entries(), [])
        # A refused pin (no agent) still prints no id.
        shell = self.t("new-window", "-d", "-t", "=main:", "-P", "-F", "#{pane_id}", "sleep 600")
        r = self.run_script("close", "--pin", "--wake", str(int(time.time())), shell, rc=3)
        self.assertEqual(r.stdout, "")

    def windows(self, sess):
        return self.t("list-windows", "-t", f"={sess}", "-F",
                      "#{window_id} #{window_index} #{window_active}").splitlines()

    def test_wake_comes_back_at_the_end_of_its_space_without_moving_anyone(self):
        now = int(time.time())
        self.t("new-session", "-d", "-s", "proj", "sleep 600")
        for _ in range(2):
            self.t("new-window", "-d", "-t", "=proj:", "sleep 600")
        self.t("new-session", "-d", "-s", "stash", "sleep 600")
        before_proj, before_main = self.windows("proj"), self.windows("main")
        active_proj = [w for w in before_proj if w.endswith(" 1")]
        self.write(
            entry("home", now - 50, session="proj", tmux_session="", index=0, cwd=self.tmp,
                  label="writing the spec", pinned=True, stashed_ts=now - 50, wake_at=now - 5),
            entry("gone", now - 40, session="vanished", tmux_session="", cwd=self.tmp, label="zsh",
                  desc="tail the deploy log", pinned=True, stashed_ts=now - 40, wake_at=now - 1),
            entry("hid", now - 30, session="stash", tmux_session="", cwd=self.tmp, label="hidden one",
                  pinned=True, stashed_ts=now - 30, wake_at=now - 2),
            entry("keep", now - 20, session="proj", cwd=self.tmp, pinned=True, stashed_ts=now - 20,
                  wake_at=now + 3600))
        r = self.run_script("wake", "home", "gone", "hid")
        # One line per entry, soonest alarm first: label (or its description
        # for a generic one) and the session it landed in.
        self.assertEqual(r.stdout.splitlines(), ["writing the spec\tproj", "hidden one\tmain",
                                                 "tail the deploy log\tmain"])
        self.assertEqual([e["id"] for e in self.entries()], ["keep"])
        after_proj, after_main = self.windows("proj"), self.windows("main")
        # At the END of the bar (index 0 was asked for and exists), not active:
        # nobody's view moved.
        self.assertEqual(after_proj[:-1], before_proj)
        self.assertTrue(after_proj[-1].endswith(" 0"), after_proj)
        self.assertEqual([w for w in after_proj if w.endswith(" 1")], active_proj)
        self.assertEqual(len(after_main), len(before_main) + 2)
        self.assertEqual(after_main[:len(before_main)], before_main)
        self.assertFalse(any(w.endswith(" 1") for w in after_main[len(before_main):]))
        self.assertEqual(len(self.windows("stash")), 1)                   # never into a hidden one
        self.stash_calls("publish-count")
        self.snooze_calls("reschedule")
        # Nothing there to wake: status 1, quiet.
        r = self.run_script("wake", "home", rc=1)
        self.assertEqual(r.stdout, "")

    @unittest.skipUnless(os.path.exists("/bin/bash"), "no /bin/bash")
    def test_close_and_wake_under_macos_bin_bash(self):
        # macOS's /bin/bash is 3.2, where an empty array is "unbound" under
        # set -u: reopen_one's "${autostart[@]}" (a plain shell has no resume
        # command) killed every such wake there. PATH's bash is Homebrew's 5,
        # so the 3.2 one is run by its path.
        now = int(time.time())
        self.t("new-session", "-d", "-s", "proj", "sleep 600")
        # close: a plain one (no agent: every optional array empty) and a
        # pinned, snoozed agent tab.
        pane = self.t("new-window", "-d", "-t", "=proj:", "-P", "-F", "#{pane_id}", "sleep 600")
        r = self.run_script("close", pane, shell="/bin/bash")
        self.assertNotIn("unbound", r.stderr)
        self.assertNotIn(pane, self.panes())
        r = self.run_script("close", "--pin", "--wake", str(now + 600), self.agent_tab(), shell="/bin/bash")
        self.assertNotIn("unbound", r.stderr)
        agent = self.entries()[-1]
        self.assertEqual((r.stdout, agent["wake_at"], agent["session"]), (agent["id"] + "\n", now + 600, "main"))
        # wake: the agent entry (a resume command) and a shell entry (none).
        self.write(dict(agent, wake_at=now - 2),
                   entry("w", now - 50, session="proj", tmux_session="", cwd=self.tmp, label="plain one",
                         pinned=True, stashed_ts=now - 50, wake_at=now - 1))
        proj, main = len(self.windows("proj")), len(self.windows("main"))
        r = self.run_script("wake", agent["id"], "w", shell="/bin/bash")
        self.assertNotIn("unbound", r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(len(lines), 2, r.stdout)
        self.assertTrue(lines[0].endswith("\tmain"), lines)
        self.assertEqual(lines[1], "plain one\tproj")
        self.assertEqual((len(self.windows("proj")), len(self.windows("main"))), (proj + 1, main + 1))
        self.assertEqual(self.entries(), [])
        # The popup's command, with no client (an empty array) to name. No
        # client is attached, so tmux itself may refuse the popup: only the
        # shell's own failure counts here.
        self.write(entry("x", now, cwd=self.tmp))
        r = self.run_script("pick-popup", "main", "", shell="/bin/bash", rc=None)
        self.assertNotIn("unbound", r.stderr)

    def test_wake_works_without_a_locale(self):
        # As launchd runs snooze.sh's wake-due: `env -i`, no LANG / LC_*, no
        # TMUX — a `tmux` on PATH reaching the private server by its socket.
        # A tmux client without a UTF-8 locale prints \x1f in -F output as
        # "_", and session_visible read "main__": every wake failed until
        # closed-tabs.sh fixed its own locale.
        now = int(time.time())
        self.t("new-session", "-d", "-s", "proj", "sleep 600")
        bare = Path(self.tmp, "bare-bin")
        bare.mkdir()
        (bare / "tmux").write_text(f'#!/bin/sh\nexec "{TMUX}" -S "{self.path}" "$@"\n')
        (bare / "tmux").chmod(0o755)
        env = dict(HOME=self.tmp, TMPDIR=self.tmp + "/", PATH=f"{bare}:{self.bin}:/opt/homebrew/bin:/usr/bin:/bin",
                   XDG_STATE_HOME=self.env["XDG_STATE_HOME"], STASH_SH=self.env["STASH_SH"],
                   SNOOZE_SH=self.env["SNOOZE_SH"], NOTIFY_SH=self.env["NOTIFY_SH"], NOTIFY_TMUX="true")
        bare_env = ["env", "-i", *(f"{k}={v}" for k, v in env.items())]
        # The hazard is real in this environment (else the test proves nothing).
        raw = subprocess.run([*bare_env, "tmux", "display", "-p", "-t", "=proj:", "#{session_name}\x1fx"],
                             capture_output=True, text=True, timeout=10).stdout
        self.assertNotEqual(raw.strip(), "proj\x1fx", "this env no longer shows the hazard")
        self.write(entry("w", now - 50, session="proj", tmux_session="", cwd=self.tmp, label="back to proj",
                         pinned=True, stashed_ts=now - 50, wake_at=now - 1))
        before = len(self.windows("proj"))
        r = subprocess.run([*bare_env, "bash", str(SCRIPT), "wake", "w"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        self.assertEqual(r.stdout, "back to proj\tproj\n")
        self.assertEqual(len(self.windows("proj")), before + 1)
        self.assertEqual(self.entries(), [])


if __name__ == "__main__":
    unittest.main()
