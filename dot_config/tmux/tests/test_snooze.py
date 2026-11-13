"""snooze.sh: when-parsing, the apply dispatch, reschedule, the wake-due timer
tick and the menu.

A snooze is a stash with a wake time: a pinned closed-tabs entry with
"wake_at", or a window in the `stash` session with @stash_wake. snooze.sh only
parses, dispatches to closed-tabs.sh / stash.sh and runs the timer, so both
are fakes here (CLOSED_TABS_SH / STASH_SH) that log their argv and answer
`rows` / `wake` / `snooze` from files; notify.sh is a fake that logs its
argv (NOTIFY_SH). "Now" is fixed through SNOOZE_NOW and TZ is America/Denver,
so named times are checked against zoneinfo.

Two harnesses:
  * No server: a `tmux` on PATH that logs its argv and answers display-message
    for a window from a file, list-sessions from a flag file, and nothing else.
  * A PRIVATE tmux server (`-L snooze-<uuid>`, `-f /dev/null`, a short
    TMUX_TMPDIR, killed in teardown) that runs the menu's item commands and
    the Custom… prompt's template (with %%% substituted as tmux does), to
    prove they parse and re-enter the script with the right arguments.
The script runs under /bin/bash (3.2), as the LaunchAgent runs it.
Run with unittest or pytest.
"""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get("SNOOZE_UNDER_TEST", HERE / "scripts" / "executable_snooze.sh"))
BASH = "/bin/bash" if os.path.exists("/bin/bash") else shutil.which("bash")
TMUX = shutil.which("tmux")
TZ = ZoneInfo("America/Denver")
US = "\x1f"


def local(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp())


# Mon 2026-11-16 10:15 MST.
MON_1015 = local(2026, 11, 16, 10, 15)

RECORDER = r'''#!/bin/bash
# Log this call's argv (US-separated) as one line.
{ printf '%s' "$(basename "$0")"; for a in "$@"; do printf '\037%s' "$a"; done; printf '\n'; } >> "$FAKE_DIR/calls.log"
'''

FAKE_TMUX = RECORDER + r'''
case "$1" in
    list-sessions) [ -e "$FAKE_DIR/no-server" ] && exit 1; echo "main: 1 windows"; exit 0 ;;
    display-message)
        # display-message -p -t <win> <fmt>: the window's canned line, or fail.
        w=""; while [ $# -gt 0 ]; do [ "$1" = -t ] && { w="$2"; shift; }; shift; done
        [ -f "$FAKE_DIR/win$w" ] || exit 1
        cat "$FAKE_DIR/win$w"; exit 0 ;;
    show-options) cat "$FAKE_DIR/option" 2>/dev/null; exit 0 ;;
esac
exit 0
'''

FAKE_JQ = RECORDER + "exit 1\n"

FAKE_STASH = RECORDER + r'''
case "$1" in
    rows) cat "$FAKE_DIR/rows" 2>/dev/null ;;
    snooze) [ -e "$FAKE_DIR/refuse" ] || echo "w:$3" ;;
    wake)
        shift
        for w in "$@"; do
            printf '%s\t%s\n' "label$w" "origin$w"
            # Gone from the stash: drop its row.
            grep -v "^$w$(printf '\037')" "$FAKE_DIR/rows" > "$FAKE_DIR/rows.tmp"; mv "$FAKE_DIR/rows.tmp" "$FAKE_DIR/rows"
        done ;;
esac
exit 0
'''

FAKE_CLOSED = RECORDER + r'''
case "$1" in
    wake)
        shift
        for id in "$@"; do
            printf '%s\t%s\n' "entry $id" main
            # Reopened: the entry leaves the history.
            grep -v "\"id\": \"$id\"" "$HIST" > "$HIST.tmp"; mv "$HIST.tmp" "$HIST"
        done ;;
esac
exit 0
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="snooze-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake = Path(self.tmp, "fake")
        self.fake.mkdir()
        self.bin = Path(self.tmp, "bin")
        self.bin.mkdir()
        self.state = Path(self.tmp, "state")
        self.sdir = self.state / "tmux-snooze"
        self.next_file = self.sdir / "snooze-next"
        self.hist = self.state / "tmux-closed-tabs" / "history.jsonl"
        self.hist.parent.mkdir(parents=True)
        self.stash = self.script("stash.sh", FAKE_STASH)
        self.closed = self.script("closed-tabs.sh", FAKE_CLOSED)
        self.notify = self.script("notify.sh", RECORDER)
        self.now = MON_1015
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env.update(HOME=self.tmp, XDG_STATE_HOME=str(self.state), TZ="America/Denver",
                        PATH=f"{self.bin}:{self.env.get('PATH', '/usr/bin:/bin')}",
                        FAKE_DIR=str(self.fake), HIST=str(self.hist),
                        STASH_SH=str(self.stash), CLOSED_TABS_SH=str(self.closed),
                        NOTIFY_SH=str(self.notify))

    def script(self, name, text, where=None):
        p = Path(where or self.fake, name)
        p.write_text(text)
        p.chmod(0o755)
        return p

    def fake_tmux(self):
        self.script("tmux", FAKE_TMUX, self.bin)

    def run_script(self, *args, rc=0, now=None):
        env = dict(self.env, SNOOZE_NOW=str(now if now is not None else self.now))
        r = subprocess.run([BASH, str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30)
        if rc is not None:
            self.assertEqual(r.returncode, rc, (args, r.stdout, r.stderr))
        return r

    def calls(self, who=None):
        p = self.fake / "calls.log"
        lines = p.read_text().splitlines() if p.exists() else []
        out = [l.split(US) for l in lines]
        return [c[1:] for c in out if who is None or c[0] == who] if who else out

    def window(self, win, session, wake="", label="lbl"):
        (self.fake / f"win{win}").write_text(f"{session}{US}{wake}{US}{label}\n")

    def rows(self, *rows):
        """rows((win, wake), …): stash.sh rows, 7 fields."""
        (self.fake / "rows").write_text("".join(
            US.join([w, "main", f"label{w}", "1", "frozen", "/", str(wake)]) + "\n" for w, wake in rows))

    def history(self, *entries):
        self.hist.write_text("".join(json.dumps(e) + "\n" for e in entries))


def entry(id_, **kw):
    e = {"id": id_, "ts": 1, "session": "main", "label": f"tab {id_}", "cwd": "/"}
    e.update(kw)
    return e


class Parse(Base):
    def parse(self, text, now=None):
        r = self.run_script("parse", *text.split(), rc=None, now=now)
        return r

    def check(self, now, table):
        for text, want in table:
            with self.subTest(now=now, text=text):
                r = self.parse(text, now)
                self.assertEqual(r.returncode, 0, (text, r.stderr))
                got = int(r.stdout.strip())
                self.assertEqual(got, want, (text, datetime.fromtimestamp(got, TZ), datetime.fromtimestamp(want, TZ)))

    def test_durations(self):
        n = MON_1015
        self.check(n, [
            ("30m", n + 1800), ("2h", n + 7200), ("1d", n + 86400), ("1w", n + 7 * 86400),
            ("2h30m", n + 9000), ("2h 30m", n + 9000), ("45 min", n + 2700), ("in 1 hour", n + 3600),
            ("90min", n + 5400), ("3 hours", n + 3 * 3600),
        ])

    def test_named_times_monday_morning(self):
        n = MON_1015   # Monday 10:15
        self.check(n, [
            ("tonight", local(2026, 11, 16, 18)), ("evening", local(2026, 11, 16, 18)),
            ("this evening", local(2026, 11, 16, 18)),
            ("tomorrow", local(2026, 11, 17, 9)), ("tmrw", local(2026, 11, 17, 9)),
            # Monday after 9:00: next Monday.
            ("mon", local(2026, 11, 23, 9)), ("monday", local(2026, 11, 23, 9)),
            ("tue", local(2026, 11, 17, 9)), ("fri", local(2026, 11, 20, 9)), ("sun", local(2026, 11, 22, 9)),
            ("9am", local(2026, 11, 17, 9)), ("14:30", local(2026, 11, 16, 14, 30)),
            ("2pm", local(2026, 11, 16, 14)), ("2:15pm", local(2026, 11, 16, 14, 15)),
            ("10:00", local(2026, 11, 17, 10)), ("10:30", local(2026, 11, 16, 10, 30)),
            ("noon", local(2026, 11, 16, 12)), ("midnight", local(2026, 11, 17, 0)),
            ("12am", local(2026, 11, 17, 0)), ("12pm", local(2026, 11, 16, 12)),
            ("fri 9am", local(2026, 11, 20, 9)), ("tomorrow 2pm", local(2026, 11, 17, 14)),
            ("mon 11am", local(2026, 11, 16, 11)), ("mon 9am", local(2026, 11, 23, 9)),
            ("tonight 9pm", local(2026, 11, 16, 21)), ("today 5pm", local(2026, 11, 16, 17)),
            ("Fri 9 AM", local(2026, 11, 20, 9)), ("tomorrow at 2pm", local(2026, 11, 17, 14)),
        ])

    def test_monday_before_nine_is_today(self):
        n = local(2026, 11, 16, 8, 0)
        self.check(n, [("mon", local(2026, 11, 16, 9)), ("9am", local(2026, 11, 16, 9))])

    def test_rollover_after_the_time(self):
        n = local(2026, 11, 16, 19, 0)
        self.check(n, [("tonight", local(2026, 11, 17, 18)), ("2pm", local(2026, 11, 17, 14)),
                       ("sun", local(2026, 11, 22, 9))])
        # Saturday evening: sun is tomorrow, sat is a week out.
        n = local(2026, 11, 21, 20, 0)
        self.check(n, [("sun", local(2026, 11, 22, 9)), ("sat", local(2026, 11, 28, 9)),
                       ("tomorrow", local(2026, 11, 22, 9))])

    def test_dst_end(self):
        # Sat 2026-10-31 10:00 MDT; DST ends Sun 2026-11-01 02:00.
        n = local(2026, 10, 31, 10, 0)
        self.check(n, [("tomorrow", local(2026, 11, 1, 9)), ("sun 9am", local(2026, 11, 1, 9)),
                       ("1d", n + 86400)])

    def test_garbage(self):
        for text in ("garbage", "9", "", "25:00", "13pm", "10:75", "today 8am", "fri mon",
                     "2pm 3pm", "0m", "*", "tomorrow 2pm extra"):
            with self.subTest(text=text):
                r = self.parse(text)
                self.assertEqual(r.returncode, 1, (text, r.stdout))
                self.assertEqual(r.stdout, "")
                self.assertIn("snooze:", r.stderr)


class Apply(Base):
    def setUp(self):
        super().setUp()
        self.fake_tmux()
        self.history(entry("A", label="alpha"), entry("B", label="beta", pinned=True, wake_at=self.now + 50))
        self.window("@5", "stash", "", "parked five")
        self.window("@7", "main", "", "seven")

    def test_entries_snooze_and_unsnooze(self):
        e = self.now + 3600
        self.run_script("apply", str(e), "p:A", "b:B")
        self.assertEqual(self.calls("closed-tabs.sh"), [["snooze", str(e), "A", "B"]])
        notes = self.calls("notify.sh")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0][-1], "⏰ snoozed 2 tabs — back at 11:15")
        self.run_script("apply", "unsnooze", "p:B")
        self.assertEqual(self.calls("closed-tabs.sh")[-1], ["unsnooze", "B"])
        self.assertEqual(self.calls("notify.sh")[-1][-1], "⏰ unsnoozed beta — still stashed")

    def test_window_in_stash_gets_set_wake(self):
        e = self.now + 86400
        self.run_script("apply", "-c", "/dev/ttys999", str(e), "w:@5")
        self.assertEqual(self.calls("stash.sh")[0], ["set-wake", str(e), "@5"])
        note = self.calls("notify.sh")[-1]
        self.assertEqual(note[:2], ["-c", "/dev/ttys999"])
        self.assertEqual(note[-1], "⏰ snoozed parked five — back tomorrow 10:15")
        self.run_script("apply", "unsnooze", "w:@5")
        self.assertIn(["set-wake", "", "@5"], self.calls("stash.sh"))

    def test_window_elsewhere_is_parked_by_stash_snooze(self):
        e = local(2026, 11, 20, 9)
        self.run_script("apply", str(e), "w:@7")
        self.assertEqual(self.calls("stash.sh")[0], ["snooze", str(e), "@7"])
        self.assertEqual(self.calls("notify.sh")[-1][-1], "⏰ snoozed seven — back Fri 9:00")
        # A bare window id too; r: rows and gone windows are ignored.
        self.run_script("apply", str(e), "@7", "r:@5", "w:@99")
        self.assertEqual([c for c in self.calls("stash.sh") if c[0] != "rows"],
                         [["snooze", str(e), "@7"], ["snooze", str(e), "@7"]])

    def test_unsnooze_leaves_unparked_windows_alone(self):
        self.run_script("apply", "unsnooze", "w:@7")
        self.assertEqual([c for c in self.calls("stash.sh") if c[0] != "rows"], [])
        self.assertEqual(self.calls("notify.sh"), [])

    def test_refused_park_no_toast(self):
        (self.fake / "refuse").touch()
        self.run_script("apply", str(self.now + 60), "w:@7")
        self.assertIn(["snooze", str(self.now + 60), "@7"], self.calls("stash.sh"))
        self.assertEqual(self.calls("notify.sh"), [])

    def test_soon_says_in_minutes(self):
        self.run_script("apply", str(self.now + 45 * 60), "p:A")
        self.assertEqual(self.calls("notify.sh")[-1][-1], "⏰ snoozed alpha — back in 45m")

    def test_bad_epoch(self):
        self.run_script("apply", "soon", "p:A", rc=1)
        self.assertEqual(self.calls("closed-tabs.sh"), [])

    def test_apply_text(self):
        self.run_script("apply-text", "-c", "/dev/ttys999", "2h", "w:@7")
        self.assertEqual(self.calls("stash.sh")[0], ["snooze", str(self.now + 7200), "@7"])
        self.run_script("apply-text", "-c", "/dev/ttys999", "fri 9am", "p:A")
        self.assertEqual(self.calls("closed-tabs.sh")[-1], ["snooze", str(local(2026, 11, 20, 9)), "A"])

    def test_apply_text_garbage_changes_nothing(self):
        self.run_script("apply-text", "-c", "/dev/ttys999", "whenever", "p:A", "w:@7", rc=1)
        self.assertEqual(self.calls("closed-tabs.sh"), [])
        self.assertEqual(self.calls("stash.sh"), [])
        note = self.calls("notify.sh")
        self.assertEqual(len(note), 1)
        self.assertEqual(note[0][:4], ["-c", "/dev/ttys999", "-k", "warn"])
        self.assertIn("can't read 'whenever'", note[0][-1])

    def test_apply_prompt_reads_and_unsets_the_option(self):
        (self.fake / "option").write_text("3h\n")
        self.run_script("apply-prompt", "-c", "/dev/ttys999", "@snooze_prompt_42", "p:A")
        self.assertEqual(self.calls("closed-tabs.sh")[-1], ["snooze", str(self.now + 3 * 3600), "A"])
        self.assertIn(["set-option", "-gu", "@snooze_prompt_42"], self.calls("tmux"))

    def test_apply_reschedules(self):
        self.run_script("apply", str(self.now + 60), "p:A")
        self.assertEqual(self.next_file.read_text().strip(), str(self.now + 50))   # B's wake_at


class Reschedule(Base):
    def setUp(self):
        super().setUp()
        self.fake_tmux()

    def test_min_over_entries_and_windows(self):
        self.history(entry("A", pinned=True, wake_at=self.now + 300),
                     entry("B", pinned=True),
                     entry("C", wake_at=self.now + 10))           # not pinned: no alarm
        with self.hist.open("a") as f:
            f.write("not json\n")
        self.rows(("@5", self.now + 250), ("@6", ""), ("@7", self.now + 900))
        self.run_script("reschedule")
        self.assertEqual(self.next_file.read_text().strip(), str(self.now + 250))
        self.assertEqual(self.run_script("next").stdout.strip(), str(self.now + 250))
        self.rows(("@6", ""))
        self.run_script("reschedule")
        self.assertEqual(self.next_file.read_text().strip(), str(self.now + 300))

    def test_nothing_snoozed_removes_the_file(self):
        self.sdir.mkdir(parents=True)
        self.next_file.write_text("123\n")
        self.history(entry("A", pinned=True))
        self.rows(("@6", ""))
        self.run_script("reschedule")
        self.assertFalse(self.next_file.exists())
        self.assertEqual(self.run_script("next").stdout, "")


class WakeDue(Base):
    def setUp(self):
        super().setUp()
        self.fake_tmux()
        self.sdir.mkdir(parents=True)

    def test_fast_path_calls_neither_tmux_nor_jq(self):
        self.script("jq", FAKE_JQ, self.bin)
        for content in (None, f"{self.now + 100}\n"):
            with self.subTest(next=content):
                if content is None:
                    self.next_file.unlink(missing_ok=True)
                else:
                    self.next_file.write_text(content)
                self.history(entry("A", pinned=True, wake_at=self.now - 10))   # stale file: still no work
                self.run_script("wake-due")
                self.assertEqual(self.calls(), [])

    def test_due_wakes_both_kinds_with_one_toast(self):
        self.next_file.write_text(f"{self.now - 5}\n")
        self.history(entry("A", pinned=True, wake_at=self.now - 5),
                     entry("B", pinned=True, wake_at=self.now + 600),
                     entry("C", wake_at=self.now - 100))
        self.rows(("@5", self.now), ("@6", self.now + 900), ("@8", ""))
        self.run_script("wake-due")
        self.assertEqual(self.calls("closed-tabs.sh"), [["wake", "A"]])
        self.assertIn(["wake", "@5"], self.calls("stash.sh"))
        notes = self.calls("notify.sh")
        self.assertEqual(len(notes), 1, notes)
        self.assertEqual(notes[0][:2], ["-c", "*"])
        self.assertEqual(notes[0][-1], "⏰ back: 2 snoozed tabs — entry A, label@5")
        self.assertEqual(self.next_file.read_text().strip(), str(self.now + 600))

    def test_single_toast_names_the_space(self):
        self.next_file.write_text(f"{self.now}\n")
        self.history(entry("A", pinned=True, wake_at=self.now))
        self.rows()
        self.run_script("wake-due")
        self.assertEqual(self.calls("notify.sh")[0][-1], "⏰ back: entry A in main")
        self.assertFalse(self.next_file.exists())

    def test_no_server_does_nothing(self):
        (self.fake / "no-server").touch()
        self.next_file.write_text(f"{self.now - 5}\n")
        self.history(entry("A", pinned=True, wake_at=self.now - 5))
        self.run_script("wake-due")
        self.assertEqual(self.calls("closed-tabs.sh"), [])
        self.assertEqual(self.calls("notify.sh"), [])
        self.assertEqual(self.next_file.read_text().strip(), str(self.now - 5))   # kept for the next tick

    def test_held_lock_skips(self):
        (self.sdir / "wake.lock").mkdir()
        self.next_file.write_text(f"{self.now - 5}\n")
        self.history(entry("A", pinned=True, wake_at=self.now - 5))
        self.run_script("wake-due")
        self.assertEqual(self.calls("closed-tabs.sh"), [])
        self.assertTrue((self.sdir / "wake.lock").is_dir())

    def test_failed_wake_retries_later_not_every_tick(self):
        # closed-tabs.sh's wake does nothing: the entry stays due.
        self.script("closed-tabs.sh", RECORDER + "exit 1\n")
        self.next_file.write_text(f"{self.now - 5}\n")
        self.history(entry("A", pinned=True, wake_at=self.now - 5))
        self.rows()
        self.run_script("wake-due")
        self.assertEqual(self.calls("closed-tabs.sh"), [["wake", "A"]])
        self.assertEqual(self.calls("notify.sh"), [])
        self.assertEqual(self.next_file.read_text().strip(), str(self.now + 300))
        self.assertFalse((self.sdir / "wake.lock").exists())
        # A tick before the retry time does nothing (the fast path).
        (self.fake / "calls.log").unlink()
        self.run_script("wake-due", now=self.now + 100)
        self.assertEqual(self.calls(), [])

    def test_a_stuck_alarm_does_not_hold_back_the_others(self):
        self.script("closed-tabs.sh", RECORDER + "exit 1\n")
        self.next_file.write_text(f"{self.now - 5}\n")
        self.history(entry("A", pinned=True, wake_at=self.now - 5))
        self.rows(("@6", self.now + 60))
        self.run_script("wake-due")
        self.assertEqual(self.next_file.read_text().strip(), str(self.now + 60))
        # At +60 only the window is tried; A waits out its retry.
        (self.fake / "calls.log").unlink()
        self.run_script("wake-due", now=self.now + 60)
        self.assertEqual(self.calls("closed-tabs.sh"), [])
        self.assertIn(["wake", "@6"], self.calls("stash.sh"))
        self.assertEqual(self.calls("notify.sh")[0][-1], "⏰ back: label@6 in origin@6")
        self.assertEqual(self.next_file.read_text().strip(), str(self.now + 300))

    def test_gives_up_after_three_tries_once(self):
        self.script("closed-tabs.sh", RECORDER + "exit 1\n")
        self.next_file.write_text(f"{self.now - 5}\n")
        self.history(entry("A", label="stuck one", pinned=True, wake_at=self.now - 5))
        self.rows()
        for k in range(3):
            self.run_script("wake-due", now=self.now + 300 * k)
        self.assertEqual(self.calls("closed-tabs.sh"), [["wake", "A"]] * 3)
        notes = self.calls("notify.sh")
        self.assertEqual(len(notes), 1, notes)
        self.assertEqual(notes[0][:4], ["-c", "*", "-k", "warn"])
        self.assertEqual(notes[0][-1], "⏰ couldn't bring back stuck one — it's still in Snoozed (⌘Z)")
        # Given up: no alarm left to keep, so no tick does anything any more.
        self.assertFalse(self.next_file.exists())
        (self.fake / "calls.log").unlink()
        self.run_script("wake-due", now=self.now + 5000)
        self.assertEqual(self.calls(), [])
        # Snoozed again by hand: the record is dropped and it counts from zero.
        self.history(entry("A", label="stuck one", pinned=True, wake_at=self.now + 9000))
        self.run_script("reschedule", now=self.now + 5000)
        self.assertFalse((self.sdir / "failures").exists())
        self.assertEqual(self.next_file.read_text().strip(), str(self.now + 9000))

    def test_given_up_keeps_its_record_while_others_reschedule(self):
        (self.sdir / "failures").write_text(f"p:A\t3\t{self.now - 100}\n")
        self.history(entry("A", pinned=True, wake_at=self.now - 500),
                     entry("B", pinned=True, wake_at=self.now + 700))
        self.rows()
        self.run_script("reschedule")
        self.assertEqual(self.next_file.read_text().strip(), str(self.now + 700))
        self.assertIn("p:A\t3\t", (self.sdir / "failures").read_text())


class Menu(Base):
    def setUp(self):
        super().setUp()
        self.fake_tmux()
        self.history(entry("A", label="alpha"), entry("B", label="beta", pinned=True, wake_at=self.now + 50))
        self.window("@5", "stash", "", "parked five")
        self.window("@7", "main", "", "seven")
        self.window("@9", "stash", str(self.now + 99), "snoozed nine")

    def menu_call(self):
        menus = [c for c in self.calls("tmux") if c and c[0] == "display-menu"]
        self.assertEqual(len(menus), 1, self.calls("tmux"))
        return menus[0]

    def items(self, argv):
        """{name: (key, command)} from display-menu's argv."""
        i = argv.index("C") + 1     # after -y C
        out = {}
        while i < len(argv):
            if argv[i] == "":
                i += 1
                continue
            out[argv[i]] = (argv[i + 1], argv[i + 2])
            i += 3
        return out

    def test_builds_display_menu(self):
        self.run_script("menu", "/dev/ttys999", "w:@7")
        argv = self.menu_call()
        self.assertEqual(argv[1:3], ["-c", "/dev/ttys999"])
        for flag, val in (("-b", "rounded"), ("-s", "bg=#1e1e2e,fg=#cdd6f4"),
                          ("-H", "bg=#45475a,fg=#cdd6f4,bold"), ("-y", "C")):
            self.assertEqual(argv[argv.index(flag) + 1], val)
        self.assertIn("snooze seven", argv[argv.index("-T") + 1])
        items = self.items(argv)
        self.assertEqual(list(items), ["1 hour", "3 hours", "This evening 18:00", "Tomorrow 9:00",
                                       "Monday 9:00", "Custom…"])
        self.assertIn("apply-text -c '/dev/ttys999' '1h' 'w:@7'", items["1 hour"][1])
        self.assertTrue(items["1 hour"][1].startswith("run-shell -b "))
        self.assertIn("'tonight'", items["This evening 18:00"][1])
        self.assertTrue(items["Custom…"][1].startswith('command-prompt -c "/dev/ttys999" -p "snooze until:" '))
        self.assertIn("%%%", items["Custom…"][1])

    def test_unsnooze_only_when_snoozed(self):
        self.run_script("menu", "/dev/ttys999", "p:A", "w:@5")
        self.assertNotIn("Unsnooze", self.items(self.menu_call()))
        (self.fake / "calls.log").unlink()
        self.run_script("menu", "/dev/ttys999", "p:A", "p:B")
        argv = self.menu_call()
        self.assertIn("snooze 2 tabs", argv[argv.index("-T") + 1])
        self.assertIn("apply -c '/dev/ttys999' unsnooze 'p:A' 'p:B'", self.items(argv)["Unsnooze"][1])
        (self.fake / "calls.log").unlink()
        self.run_script("menu", "/dev/ttys999", "w:@9")
        self.assertIn("Unsnooze", self.items(self.menu_call()))

    def test_evening_after_six(self):
        self.run_script("menu", "/dev/ttys999", "w:@7", now=local(2026, 11, 16, 19))
        items = self.items(self.menu_call())
        self.assertIn("Tomorrow evening 18:00", items)
        self.assertIn("'tomorrow 18:00'", items["Tomorrow evening 18:00"][1])

    def test_nothing_to_snooze(self):
        self.run_script("menu", "/dev/ttys999", "r:@5", "w:@99")
        self.assertEqual([c for c in self.calls("tmux") if c and c[0] == "display-menu"], [])
        self.assertIn("nothing to snooze", self.calls("notify.sh")[0][-1])


@unittest.skipUnless(TMUX, "tmux not installed")
class MenuCommandsOnAServer(Base):
    """The menu's item commands, run by a real (private) tmux: they parse, and
    re-enter the script with the right arguments."""

    def setUp(self):
        super().setUp()
        self.history(entry("A", label="alpha"))
        self.sock = "snooze-" + uuid.uuid4().hex[:12]
        sockdir = tempfile.mkdtemp(prefix="snz-", dir="/private/tmp")   # short: sun_path
        self.addCleanup(shutil.rmtree, sockdir, True)
        self.env.update(TERM="xterm-256color", TMUX_TMPDIR=sockdir, SNOOZE_NOW=str(self.now))
        self.addCleanup(self.kill_server)
        self.t("new-session", "-d", "-s", "main", "sleep 600")
        self.t("set", "-g", "exit-empty", "off")
        # The menu itself comes from a recording tmux, so its argv can be read.
        rec = Path(self.tmp, "recbin")
        rec.mkdir()
        self.script("tmux", FAKE_TMUX, rec)
        env = dict(self.env, PATH=f"{rec}:{self.env['PATH']}")
        subprocess.run([BASH, str(SCRIPT), "menu", "/dev/ttys999", "p:A"], env=env, check=True,
                       capture_output=True, timeout=30)
        menu = [c for c in self.calls("tmux") if c and c[0] == "display-menu"][0]
        self.items = Menu.items(None, menu)
        (self.fake / "calls.log").unlink()

    def kill_server(self):
        subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", "kill-server"], env=self.env,
                       capture_output=True, timeout=10)

    def t(self, *args):
        r = subprocess.run([TMUX, "-L", self.sock, "-f", "/dev/null", *args], env=self.env,
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, (args, r.stderr))
        return r.stdout.strip()

    def wait_calls(self, who, n=1, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            c = self.calls(who)
            if len(c) >= n:
                return c
            time.sleep(0.05)
        self.fail(f"{who} never called: {self.calls()}")

    def test_preset_item(self):
        self.t("if-shell", "true", self.items["Tomorrow 9:00"][1])
        self.assertEqual(self.wait_calls("closed-tabs.sh")[0], ["snooze", str(local(2026, 11, 17, 9)), "A"])

    def prompt(self, text):
        """command-prompt's %%% substitution, then the template as tmux runs it."""
        cmd = self.items["Custom…"][1]
        # command-prompt -c "<tty>" -p "snooze until:" "<template>": let tmux
        # unquote the template by echoing it through display-message -p.
        template = cmd.split('-p "snooze until:" ', 1)[1]
        template = self.t("if-shell", "true", f"display-message -p -l {template}")
        esc = "".join("\\" + ch if ch in '"\\$;~' else ch for ch in text)
        self.t("if-shell", "true", template.replace("%%%", esc))

    def test_custom_prompt(self):
        self.prompt("fri 9am")
        self.assertEqual(self.wait_calls("closed-tabs.sh")[0], ["snooze", str(local(2026, 11, 20, 9)), "A"])
        # The prompt's option is gone afterwards.
        time.sleep(0.2)
        self.assertNotIn("@snooze_prompt", self.t("show-options", "-g"))

    def test_custom_prompt_is_never_shell_code(self):
        pwned = Path(self.tmp, "pwned")
        self.prompt(f'$(touch {pwned}); "x\\ ~ `touch {pwned}`')
        note = self.wait_calls("notify.sh")[0]
        self.assertEqual(note[2:4], ["-k", "warn"])
        self.assertFalse(pwned.exists())
        self.assertEqual(self.calls("closed-tabs.sh"), [])


SCRIPTS = HERE / "scripts"
CLOSED_REAL = SCRIPTS / "executable_closed-tabs.sh"
STASH_REAL = SCRIPTS / "executable_stash.sh"


@unittest.skipUnless(TMUX and shutil.which("jq") and CLOSED_REAL.exists() and STASH_REAL.exists(),
                     "tmux / jq / closed-tabs.sh / stash.sh missing")
class LaunchdEnvironment(Base):
    """wake-due as launchd runs it: `env -i` with no locale, /bin/bash, the
    real closed-tabs.sh and stash.sh, a private server behind a `tmux`
    wrapper on PATH. Without a UTF-8 locale a tmux client prints \\x1f, tabs
    and non-ASCII as "_" and every wake of a snoozed entry failed."""

    def setUp(self):
        super().setUp()
        sockdir = tempfile.mkdtemp(prefix="snl-", dir="/private/tmp")   # short: sun_path
        self.addCleanup(shutil.rmtree, sockdir, True)
        sock = os.path.join(sockdir, "s")
        self.tmux = str(self.script("tmux", f'#!/bin/sh\nexec "{TMUX}" -S "{sock}" -f /dev/null "$@"\n', self.bin))
        self.script("ai-exec", "#!/bin/sh\nexit 1\n", self.bin)
        # The wrapper by its path: never a bare `tmux kill-server`.
        self.addCleanup(lambda: subprocess.run([self.tmux, "kill-server"], env=self.env,
                                               capture_output=True, timeout=10))
        self.env.update(TERM="xterm-256color", SHELL="/bin/sh", TMPDIR=self.tmp + "/")
        self.t("new-session", "-d", "-s", "main", "-x", "120", "-y", "30", "/bin/sh")
        for opt, val in (("exit-empty", "off"), ("default-shell", "/bin/sh"), ("default-command", "/bin/sh")):
            self.t("set", "-g", opt, val)

    def t(self, *args):
        r = subprocess.run([self.tmux, *args], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, (args, r.stderr))
        return r.stdout.strip()

    def test_wakes_a_due_entry_without_a_locale(self):
        now = int(time.time())
        self.history(entry("e2eentry", label="entry—label", cwd="/tmp", index=9, tool="", session_id="",
                           cmd="", tmux_session="", session_created="", pinned=True, stashed_ts=now,
                           wake_at=now - 5))
        self.sdir.mkdir(parents=True)
        self.next_file.write_text(f"{now - 5}\n")
        before = self.t("list-windows", "-t", "main", "-F", "#{window_id}").split()
        path = ":".join(dict.fromkeys([str(self.bin), os.path.dirname(shutil.which("jq")),
                                       "/opt/homebrew/bin", "/usr/bin", "/bin"]))
        # The real notify.sh: its text goes through a tmux client, which is
        # where a missing locale turned "⏰ back: entry—label" into "_ back:
        # entry_label". Its log records the text as tmux handed it back.
        # notify.sh has no locale guard of its own (its \x1f split needs one):
        # a wrapper records the locale it inherits from snooze.sh.
        notify_log = Path(self.tmp, "notify.log")
        locale_log = Path(self.tmp, "notify.locale")
        notify = self.script("notify-wrap", "#!/bin/sh\n"
                             f'printf "%s\\n" "${{LC_ALL:-${{LC_CTYPE:-${{LANG:-}}}}}}" >> "{locale_log}"\n'
                             f'exec /bin/zsh -f "{SCRIPTS / "executable_notify.sh"}" "$@"\n')
        launchd = {"HOME": self.tmp, "PATH": path, "TMPDIR": self.tmp + "/",
                   "XDG_STATE_HOME": str(self.state), "FAKE_DIR": str(self.fake),
                   "STASH_SH": str(STASH_REAL), "CLOSED_TABS_SH": str(CLOSED_REAL),
                   "NOTIFY_SH": str(notify), "NOTIFY_LOG": str(notify_log),
                   "AGENT_FOCUS_NOTICE": "", "SNOOZE_SH": str(SCRIPT)}
        r = subprocess.run(["/usr/bin/env", "-i", *(f"{k}={v}" for k, v in launchd.items()),
                            "/bin/bash", str(SCRIPT), "wake-due"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        after = self.t("list-windows", "-t", "main", "-F", "#{window_id}").split()
        self.assertEqual(len(after), len(before) + 1, (before, after, self.sdir.joinpath("log").read_text()))
        self.assertNotIn("e2eentry", self.hist.read_text())
        logged = notify_log.read_text() if notify_log.exists() else ""
        backs = [l.split("\t")[3] for l in logged.splitlines() if "back:" in l]
        self.assertEqual(backs, ["⏰ back: entry—label in main"], logged)
        self.assertFalse((self.sdir / "failures").exists())
        locales = locale_log.read_text().splitlines()
        self.assertTrue(locales and all("UTF-8" in l for l in locales), locales)


if __name__ == "__main__":
    unittest.main()
