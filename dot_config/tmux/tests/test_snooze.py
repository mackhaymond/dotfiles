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
import re
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
    # space.sh's restore marker (wake-due defers while it is pending).
    show) [ "$3" = @space_restore_pending ] && cat "$FAKE_DIR/restore-pending" 2>/dev/null; exit 0 ;;
esac
exit 0
'''

FAKE_JQ = RECORDER + "exit 1\n"

FAKE_STASH = RECORDER + r'''
case "$1" in
    rows) cat "$FAKE_DIR/rows" 2>/dev/null ;;
    snooze) [ -e "$FAKE_DIR/refuse" ] || [ -e "$FAKE_DIR/refuse$3" ] || echo "w:$3" ;;
    check) [ -e "$FAKE_DIR/refuse-check" ] && { echo "an agent is working — not snoozing it"; exit 3; } ;;
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
        # A recorder writes its line in pieces: one still being written is not
        # a call yet.
        text = p.read_text() if p.exists() else ""
        lines = text[:text.rfind("\n") + 1].splitlines()
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

    def test_tomorrow_late_at_night_is_the_day_just_begun(self):
        n = local(2026, 11, 17, 1, 30)      # Tue 01:30: still "Monday night"
        self.check(n, [("tomorrow", local(2026, 11, 17, 9)), ("tomorrow 2pm", local(2026, 11, 17, 14)),
                       ("tomorrow 2am", local(2026, 11, 17, 2)),
                       ("tomorrow 1am", local(2026, 11, 18, 1)),       # already past: the day after
                       ("tonight", local(2026, 11, 17, 18))])
        n = local(2026, 11, 17, 4, 59)
        self.check(n, [("tomorrow", local(2026, 11, 17, 9))])
        n = local(2026, 11, 17, 5, 0)
        self.check(n, [("tomorrow", local(2026, 11, 18, 9))])

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

    def test_freeform(self):
        # The grammar is test_snooze_when.py's; this is the way through the shell.
        self.check(MON_1015, [
            ("next monday", local(2026, 11, 23, 9)), ("tomorrow morning", local(2026, 11, 17, 9)),
            ("dec 3 at 2pm", local(2026, 12, 3, 14)), ("an hour and a half", MON_1015 + 5400),
            ("tonight at 9", local(2026, 11, 16, 21)), ("a week from friday 3pm", local(2026, 11, 27, 15)),
            ("tonight 9:30", local(2026, 11, 16, 21, 30)), ("friday next", local(2026, 11, 27, 9)),
        ])
        self.assertEqual(self.run_script("parse", "next\nmonday").stdout, f"{local(2026, 11, 23, 9)}\n")
        self.assertEqual(self.run_script("parse", " Next ", "MONDAY,").stdout, f"{local(2026, 11, 23, 9)}\n")
        self.assertEqual(self.run_script("parse", "5 o'clock").stdout, f"{local(2026, 11, 16, 17)}\n")

    def test_text_is_only_text(self):
        touched = Path(self.tmp, "touched")
        for text in (f"$(touch {touched})", f"`touch {touched}`", f"2h; touch {touched}", f"2h && touch {touched}",
                     f"2h\ntouch {touched}", '"', "'", "2h'", '2h"', "-5m", "--", "-", "-c", "--help", "parse", "batch",
                     "box", "*", "?", "~", "$HOME", "\\", "%s", "2h|", "#{pid}", "2h\t\x1b[31m", "\u00e9", "\u23f0 2h"):
            with self.subTest(text=text):
                r = self.run_script("parse", text, rc=1)
                self.assertEqual(r.stdout, "")
                why = "when? (try" if text in ("?", "-") else "can't read '"
                self.assertTrue(r.stderr.startswith("snooze: " + why), r.stderr)
                self.assertEqual(r.stderr.count("\n"), 1)
                self.assertNotIn("\x1b", r.stderr)
        self.assertFalse(touched.exists())
        self.run_script("apply-text", "-c", "/dev/ttys999", f"$(touch {touched})", "p:A")
        self.assertFalse(touched.exists())

    def test_a_broken_reader_is_said_and_never_a_time(self):
        good = str(MON_1015 + 60)
        readers = {"crash": "raise SystemError", "syntax": "def (", "usage": "import sys; sys.exit(2)",
                   "silent": "", "silent failure": "import sys; sys.exit(1)",
                   "words": "print('tomorrow')", "negative": "print(-5)", "two lines": f"print({good}); print({good})",
                   "a time, then a crash": f"print({good}); raise SystemError",
                   "a time and exit 2": f"import sys; print({good}); sys.exit(2)",
                   "killed": f"import os; print({good}, flush=True); os.kill(os.getpid(), 9)"}
        paths = {name: str(self.script(f"when-{i}.py", text + "\n"))
                 for i, (name, text) in enumerate(readers.items())}
        paths["missing"] = self.tmp + "/nowhere.py"
        self.fake_tmux()
        for name, path in paths.items():
            with self.subTest(reader=name):
                (self.fake / "calls.log").unlink(missing_ok=True)
                self.env["SNOOZE_WHEN"] = path
                r = self.run_script("parse", "1h", rc=1)
                self.assertEqual((r.stdout, r.stderr), ("", "snooze: the time reader isn't working\n"))
                self.run_script("apply-text", "-c", "/dev/ttys999", "1h", "p:A")
                self.assertEqual(self.calls("closed-tabs.sh"), [])
                self.assertEqual(self.calls("notify.sh")[-1][-1], "snooze: the time reader isn't working")
                self.run_script("prompt-popup", "-c", "/dev/ttys999", "p:A")      # no box to be stuck in
                self.assertEqual(self.calls("notify.sh")[-1][-1], "snooze: the time reader isn't working")
                self.assertEqual(self.calls("closed-tabs.sh"), [])

    def test_never_the_python_on_path(self):
        # A pyenv shim costs 130 ms a keystroke.
        self.script("python3", RECORDER + "exit 1\n", self.bin)
        self.assertEqual(self.run_script("parse", "1h").stdout, f"{MON_1015 + 3600}\n")
        self.assertEqual(self.calls("python3"), [])


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
        # Exit 0 once the toast has said it: a failing run-shell would also
        # show its status in the pane (view mode).
        self.run_script("apply-text", "-c", "/dev/ttys999", "whenever", "p:A", "w:@7", rc=0)
        self.assertEqual(self.calls("closed-tabs.sh"), [])
        self.assertEqual(self.calls("stash.sh"), [])
        note = self.calls("notify.sh")
        self.assertEqual(len(note), 1)
        self.assertEqual(note[0][:4], ["-c", "/dev/ttys999", "-k", "warn"])
        self.assertIn("can't read 'whenever'", note[0][-1])

    def test_toast_says_in_minutes_only_under_fifty(self):
        for mins, want in ((49, "in 49m"), (50, "at 11:05"), (55, "at 11:10"), (60, "at 11:15")):
            with self.subTest(mins=mins):
                self.run_script("apply", str(self.now + mins * 60), "p:A")
                self.assertEqual(self.calls("notify.sh")[-1][-1], f"⏰ snoozed alpha — back {want}")

    def test_hash_in_a_label_is_not_a_format(self):
        # notify.sh expands formats in its text: a # of a label is doubled.
        self.window("@7", "main", "", "a#{client_tty}")
        self.run_script("apply", str(self.now + 7200), "w:@7")
        self.assertEqual(self.calls("notify.sh")[-1][-1], "⏰ snoozed a##{client_tty} — back at 12:15")

    # -- --ask-space: a park may have emptied its space -----------------------
    TTY = "/dev/ttys999"

    def asks(self):
        return [c for c in self.calls("closed-tabs.sh") if c[0] == "ask-space"]

    def test_ask_space_once_per_session_it_parked_from(self):
        e = str(self.now + 7200)
        self.window("@8", "main", "", "eight")
        self.window("@9", "my proj", "", "nine")
        for opts in (["-c", self.TTY, "--ask-space"], ["--ask-space", "-c", self.TTY]):
            with self.subTest(opts=opts):
                (self.fake / "calls.log").unlink(missing_ok=True)
                self.run_script("apply", *opts, e, "w:@7", "w:@9", "w:@8")
                self.assertEqual(self.asks(), [["ask-space", "--snoozed", self.TTY, "=main:"],
                                               ["ask-space", "--snoozed", self.TTY, "=my proj:"]])
                # Last of all (it holds until the box is answered): after the
                # toast and the reschedule, which reads `stash.sh rows`.
                order = [c[:2] for c in self.calls() if c[0] in ("notify.sh", "stash.sh", "closed-tabs.sh")]
                first = order.index(["closed-tabs.sh", "ask-space"])
                self.assertLess(order.index(["stash.sh", "rows"]), first)
                self.assertLess([o[0] for o in order].index("notify.sh"), first)
                self.assertEqual(self.next_file.read_text().strip(), str(self.now + 50))
                self.assertEqual(self.calls("notify.sh")[0][:2], ["-c", self.TTY])

    def test_ask_space_only_for_what_was_really_parked(self):
        e = str(self.now + 7200)
        self.window("@9", "proj", "", "nine")
        (self.fake / "refuse@9").touch()
        self.run_script("apply", "-c", self.TTY, "--ask-space", e, "w:@9", "w:@7", "w:@5", "w:@99", "p:A")
        self.assertEqual(self.asks(), [["ask-space", "--snoozed", self.TTY, "=main:"]])

    def test_ask_space_never(self):
        e = str(self.now + 7200)
        c = ["-c", self.TTY]
        for name, args in (("no flag", [*c, e, "w:@7"]), ("no client", ["--ask-space", e, "w:@7"]),
                           ("unsnooze", [*c, "--ask-space", "unsnooze", "w:@7", "w:@5", "p:B"]),
                           ("already stashed", [*c, "--ask-space", e, "w:@5", "p:B", "b:A"])):
            with self.subTest(name):
                self.run_script("apply", *args)
                self.assertEqual(self.asks(), [])
        (self.fake / "refuse").touch()
        self.run_script("apply", *c, "--ask-space", e, "w:@7")
        self.run_script("apply-text", *c, "--ask-space", "whenever", "w:@7")
        self.assertEqual(self.asks(), [])
        # A failing ask-space is not the snooze's failure.
        (self.fake / "refuse").unlink()
        self.script("closed-tabs.sh", RECORDER + "exit 1\n")
        self.run_script("apply", *c, "--ask-space", e, "w:@7", rc=0)
        self.assertEqual(len(self.asks()), 1)

    def test_apply_text_passes_ask_space_on(self):
        for opts in (["-c", self.TTY, "--ask-space"], ["--ask-space", "-c", self.TTY]):
            with self.subTest(opts=opts):
                (self.fake / "calls.log").unlink(missing_ok=True)
                self.run_script("apply-text", *opts, "2h", "w:@7")
                self.assertEqual(self.calls("stash.sh")[0], ["snooze", str(self.now + 7200), "@7"])
                self.assertEqual(self.asks(), [["ask-space", "--snoozed", self.TTY, "=main:"]])

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

    def test_fast_path_starts_no_python(self):
        mark = Path(self.tmp, "python-ran")
        self.env["SNOOZE_WHEN"] = str(self.script("when.py", f"open({str(mark)!r}, 'w')\n"))
        self.sdir.mkdir(parents=True, exist_ok=True)
        self.next_file.write_text(f"{self.now + 60}\n")
        self.run_script("wake-due")
        self.assertFalse(mark.exists())

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

    def test_a_pending_restore_defers_every_wake(self):
        # A restart's restore is running (space.sh's @space_restore_pending,
        # an epoch under 15 min old): nothing wakes, no failure is counted,
        # the alarm stays due. `done`, or a marker past 15 min: wakes as usual.
        self.next_file.write_text(f"{self.now - 5}\n")
        self.history(entry("A", pinned=True, wake_at=self.now - 5))
        self.rows(("@5", self.now - 5))
        (self.fake / "restore-pending").write_text(f"{self.now - 60}\n")
        self.run_script("wake-due")
        self.assertEqual(self.calls("closed-tabs.sh"), [])
        self.assertNotIn("wake", [c[0] for c in self.calls("stash.sh")])
        self.assertEqual(self.calls("notify.sh"), [])
        self.assertEqual(self.next_file.read_text().strip(), str(self.now - 5))   # still due
        self.assertFalse((self.sdir / "failures").exists())
        self.assertIn("restore pending — wakes deferred", (self.sdir / "log").read_text())
        self.assertFalse((self.sdir / "wake.lock").exists())
        for marker in ("done", str(self.now - 901)):
            with self.subTest(marker=marker):
                (self.fake / "calls.log").unlink(missing_ok=True)
                self.history(entry("A", pinned=True, wake_at=self.now - 5))
                self.rows(("@5", self.now - 5))
                self.next_file.write_text(f"{self.now - 5}\n")
                (self.fake / "restore-pending").write_text(marker + "\n")
                self.run_script("wake-due")
                self.assertEqual(self.calls("closed-tabs.sh"), [["wake", "A"]])
                self.assertIn(["wake", "@5"], self.calls("stash.sh"))

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
        self.assertIn("apply-text -c '/dev/ttys999' --ask-space '1h' 'w:@7'", items["1 hour"][1])
        for name in list(items)[:5]:
            self.assertIn(" apply-text -c '/dev/ttys999' --ask-space '", items[name][1], name)
        self.assertTrue(items["1 hour"][1].startswith("run-shell -b "))
        self.assertIn("'tonight'", items["This evening 18:00"][1])
        self.assertTrue(items["Custom…"][1].startswith("run-shell -b "))
        self.assertIn("custom -c '/dev/ttys999' --ask-space 'w:@7' || true", items["Custom…"][1])
        for name, (_, cmd) in items.items():
            self.assertTrue(cmd.endswith(' || true"'), (name, cmd))
        # Not parked yet: stash.sh was asked whether it would refuse it.
        self.assertIn(["check", "--snooze", "@7"], self.calls("stash.sh"))

    def test_refused_tab_gets_no_menu(self):
        (self.fake / "refuse-check").touch()
        self.run_script("menu", "/dev/ttys999", "w:@7")
        self.assertEqual([c for c in self.calls("tmux") if c and c[0] == "display-menu"], [])
        notes = self.calls("notify.sh")
        self.assertEqual(len(notes), 1, notes)
        self.assertEqual(notes[0][:4], ["-c", "/dev/ttys999", "-k", "warn"])
        self.assertEqual(notes[0][-1], "an agent is working — not snoozing it")   # as-is

    def test_refused_tab_is_left_out_of_a_group(self):
        (self.fake / "refuse-check").touch()
        self.run_script("menu", "/dev/ttys999", "p:A", "w:@7", "w:@5")
        argv = self.menu_call()
        self.assertIn("snooze 2 tabs", argv[argv.index("-T") + 1])
        self.assertIn("'p:A' 'w:@5' || true", self.items(argv)["1 hour"][1])
        self.assertNotIn("@7", self.items(argv)["1 hour"][1])
        # Parked already (@5): never checked.
        self.assertFalse([c for c in self.calls("stash.sh") if c[:1] == ["check"] and "@5" in c])

    def test_hash_in_the_title_is_literal(self):
        self.window("@7", "main", "", "a#{client_tty}")
        self.run_script("menu", "/dev/ttys999", "w:@7")
        argv = self.menu_call()
        self.assertIn("snooze a##{client_tty} ", argv[argv.index("-T") + 1])

    def test_unsnooze_only_when_snoozed(self):
        self.run_script("menu", "/dev/ttys999", "p:A", "w:@5")
        self.assertNotIn("Unsnooze", self.items(self.menu_call()))
        (self.fake / "calls.log").unlink()
        self.run_script("menu", "/dev/ttys999", "p:A", "p:B")
        argv = self.menu_call()
        self.assertIn("snooze 2 tabs", argv[argv.index("-T") + 1])
        self.assertIn("apply -c '/dev/ttys999' unsnooze 'p:A' 'p:B'", self.items(argv)["Unsnooze"][1])
        self.assertNotIn("--ask-space", self.items(argv)["Unsnooze"][1])
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

    def test_every_preset_lands_where_it_says(self):
        for i, (item, want) in enumerate((("1 hour", self.now + 3600), ("3 hours", self.now + 3 * 3600),
                                          ("This evening 18:00", local(2026, 11, 16, 18)),
                                          ("Tomorrow 9:00", local(2026, 11, 17, 9)),
                                          ("Monday 9:00", local(2026, 11, 23, 9)))):
            with self.subTest(item=item):
                self.t("if-shell", "true", self.items[item][1])
                self.assertEqual(self.wait_calls("closed-tabs.sh", i + 1)[-1], ["snooze", str(want), "A"])

    def test_evening_preset_and_the_reader_override_reach_the_server(self):
        # SNOOZE_WHEN is carried through the menu, as the other overrides are.
        mark = Path(self.tmp, "reader")
        wrap = self.script("when.py", f"open({str(mark)!r}, 'a').write('x')\n"
                           f"exec(compile(open({str(HERE / 'scripts' / 'executable_snooze-when.py')!r}).read(), 'w', 'exec'))\n")
        now = local(2026, 11, 16, 19)
        env = dict(self.env, PATH=f"{Path(self.tmp, 'recbin')}:{self.env['PATH']}", SNOOZE_NOW=str(now),
                   SNOOZE_WHEN=str(wrap))
        subprocess.run([BASH, str(SCRIPT), "menu", "/dev/ttys999", "p:A"], env=env, check=True,
                       capture_output=True, timeout=30)
        menu = [c for c in self.calls("tmux") if c and c[0] == "display-menu"][-1]
        self.assertFalse(mark.exists())
        self.t("if-shell", "true", Menu.items(None, menu)["Tomorrow evening 18:00"][1])
        self.assertEqual(self.wait_calls("closed-tabs.sh")[-1], ["snooze", str(local(2026, 11, 17, 18)), "A"])
        self.assertTrue(mark.exists())

    def test_preset_item_asks_about_the_space_it_parked_from(self):
        w = self.t("new-window", "-d", "-P", "-F", "#{window_id}", "-t", "main:", "sleep 600")
        self.window(w, "main", "", "seven")             # for the recording tmux that builds the menu
        env = dict(self.env, PATH=f"{Path(self.tmp, 'recbin')}:{self.env['PATH']}")
        subprocess.run([BASH, str(SCRIPT), "menu", "/dev/ttys999", f"w:{w}"], env=env, check=True,
                       capture_output=True, timeout=30)
        menu = [c for c in self.calls("tmux") if c and c[0] == "display-menu"][0]
        self.t("if-shell", "true", Menu.items(None, menu)["1 hour"][1])
        self.assertEqual(self.wait_calls("closed-tabs.sh")[0],
                         ["ask-space", "--snoozed", "/dev/ttys999", "=main:"])
        self.assertIn(["snooze", str(self.now + 3600), w], self.calls("stash.sh"))

    def test_every_item_parses_as_tmux_commands(self):
        # A bad flag (command-prompt -c) is a parse error tmux only reports
        # when the item runs; `source-file -n` parses without running.
        self.assertIn("Custom…", self.items)
        for name, item in self.items.items():
            f = Path(self.tmp, "item.conf")
            f.write_text(item[1] + "\n")
            self.t("source-file", "-n", str(f))

    def test_garbage_through_a_menu_line_exits_zero(self):
        # The menu's run-shell line, run in the foreground: a bad time is a
        # toast, and run-shell sees status 0 (no "returned 1" in a pane).
        cmd = self.items["1 hour"][1].replace("run-shell -b ", "run-shell ", 1).replace("'1h'", "'whenever'")
        self.t("if-shell", "true", cmd)
        self.assertIn("can't read 'whenever'", self.wait_calls("notify.sh")[0][-1])
        self.assertEqual(self.calls("closed-tabs.sh"), [])

    def test_hash_label_read_raw_from_tmux(self):
        w = self.t("new-window", "-d", "-P", "-F", "#{window_id}", "-t", "main:", "sleep 600")
        self.t("set-option", "-w", "-t", w, "automatic-rename", "off")
        self.t("rename-window", "-t", w, "a##{client_tty}")      # rename-window expands formats
        self.assertEqual(self.t("display", "-p", "-t", w, "#{window_name}"), "a#{client_tty}")
        env = dict(self.env, TMUX=self.t("display", "-p", "#{socket_path}") + ",0,0")
        subprocess.run([BASH, str(SCRIPT), "apply", str(self.now + 7200), f"w:{w}"], env=env, check=True,
                       capture_output=True, timeout=30)
        self.assertEqual(self.calls("stash.sh")[0], ["snooze", str(self.now + 7200), w])
        self.assertEqual(self.calls("notify.sh")[-1][-1], "⏰ snoozed a##{client_tty} — back at 12:15")


@unittest.skipUnless(TMUX and shutil.which("fzf"), "tmux / fzf not installed")
class Popup(Base):
    """The Custom… box for real: a private server, a client attached to it in
    a pty, `custom` opens the popup on that client, keys go in through the
    client's terminal."""

    def setUp(self):
        super().setUp()
        import pty
        import struct
        import termios
        import threading
        self.history(entry("A", label="alpha"))
        sockdir = tempfile.mkdtemp(prefix="snp-", dir="/private/tmp")   # short: sun_path
        self.addCleanup(shutil.rmtree, sockdir, True)
        sock = os.path.join(sockdir, "s")
        self.tmux = str(self.script("tmux", f'#!/bin/sh\nexec "{TMUX}" -S "{sock}" -f /dev/null "$@"\n', self.bin))
        self.env.update(TERM="xterm-256color", SNOOZE_NOW=str(self.now), FZF_DEFAULT_OPTS="",
                        FZF_DEFAULT_COMMAND="")
        # The wrapper by its path: never a bare `tmux kill-server`.
        self.addCleanup(lambda: subprocess.run([self.tmux, "kill-server"], env=self.env,
                                               capture_output=True, timeout=10))
        self.t("new-session", "-d", "-s", "main", "-x", "100", "-y", "30", "sleep 600")
        self.t("set", "-g", "exit-empty", "off")
        pid, fd = pty.fork()
        if pid == 0:    # the client
            try:
                import fcntl
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
                os.execve(self.tmux, [self.tmux, "attach", "-t", "main"], self.env)
            finally:
                os._exit(127)
        self.fd = fd
        self.screen = bytearray()

        def drain():
            while True:
                try:
                    b = os.read(fd, 65536)
                except OSError:
                    return
                if not b:
                    return
                self.screen.extend(b)
        threading.Thread(target=drain, daemon=True).start()

        def reap():
            try:
                os.kill(pid, 9)     # its server is gone; on its own it takes 3 s to leave
            except ProcessLookupError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        self.addCleanup(reap)   # runs after kill-server (cleanups are LIFO)
        end = time.time() + 10
        self.tty = ""
        while time.time() < end and not self.tty:
            self.tty = self.t("list-clients", "-F", "#{client_tty}")
            time.sleep(0.05)
        self.assertTrue(self.tty, "no client attached")

    def t(self, *args):
        r = subprocess.run([self.tmux, *args], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, (args, r.stderr))
        return r.stdout.strip()

    def open_box(self, *args):
        self.screen.clear()
        self.custom = subprocess.Popen([BASH, str(SCRIPT), "custom", "-c", self.tty, *(args or ["p:A"])], env=self.env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: self.custom.poll() is None and self.custom.kill())
        end = time.time() + 10
        while b"until:" not in self.screen:
            if time.time() > end:
                self.fail(f"no time box: {bytes(self.screen)[-500:]!r}")
            time.sleep(0.05)
        time.sleep(0.3)     # fzf ready for input

    def type(self, s):
        os.write(self.fd, s.encode())

    def popup_gone(self, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if self.custom.poll() is not None:
                return self.custom.returncode
            time.sleep(0.05)
        self.fail("the popup never closed")


class CustomPopup(Popup):
    def test_typed_time_is_applied(self):
        self.open_box()
        self.assertIn(b"tonight", self.screen)          # the suggestions, with when
        self.type("1m")
        time.sleep(0.2)
        self.type("\r")
        self.assertEqual(self.popup_gone(), 0)
        end = time.time() + 10
        while not self.calls("notify.sh") and time.time() < end:     # the toast comes after the dispatch
            time.sleep(0.05)
        self.assertEqual(self.calls("closed-tabs.sh"), [["snooze", str(self.now + 60), "A"]])
        self.assertEqual(self.calls("notify.sh")[-1][-1], "⏰ snoozed alpha — back in 1m")

    def test_ask_space_reaches_apply_through_the_popup(self):
        w = self.t("new-window", "-d", "-P", "-F", "#{window_id}", "-t", "main:", "sleep 600")
        for args, want in ((["--ask-space", f"w:{w}"], [["ask-space", "--snoozed", self.tty, "=main:"]]),
                           ([f"w:{w}"], [])):
            with self.subTest(args=args):
                (self.fake / "calls.log").unlink(missing_ok=True)
                self.open_box(*args)
                self.type("1m")
                time.sleep(0.2)
                self.type("\r")
                self.assertEqual(self.popup_gone(), 0)
                end = time.time() + 10
                while ["rows"] not in self.calls("stash.sh") and time.time() < end:
                    time.sleep(0.05)
                time.sleep(0.5)
                self.assertEqual(self.calls("stash.sh")[0], ["snooze", str(self.now + 60), w])
                self.assertEqual(self.calls("closed-tabs.sh"), want)

    def test_suggestion_is_applied_when_the_text_is_no_time(self):
        self.open_box()
        self.type("tomo")           # no time, but it narrows to "tomorrow"
        time.sleep(0.3)
        self.type("\r")
        self.assertEqual(self.popup_gone(), 0)
        end = time.time() + 10
        while not self.calls("closed-tabs.sh") and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(self.calls("closed-tabs.sh")[0], ["snooze", str(local(2026, 11, 17, 9)), "A"])

    def test_esc_cancels_quietly(self):
        self.open_box()
        self.type("2h")
        time.sleep(0.2)
        self.type("\x1b")
        self.assertEqual(self.popup_gone(), 0)
        time.sleep(0.5)
        self.assertEqual(self.calls("closed-tabs.sh"), [])
        self.assertEqual(self.calls("notify.sh"), [])

    def enter(self, text):
        """Type text (maybe none), Enter, and wait for the box to close."""
        self.open_box()
        if text:
            self.type(text)
            time.sleep(0.3)
        self.type("\r")
        self.assertEqual(self.popup_gone(), 0)

    def stays(self, text, why):
        """Type text, Enter: the box says why and stays; Esc then leaves quietly."""
        self.open_box()
        self.type(text)
        time.sleep(0.3)
        self.type("\r")
        time.sleep(0.6)
        self.assertIsNone(self.custom.poll(), "the box closed")
        self.assertIn(why, self.text())
        self.type("\x1b")
        self.assertEqual(self.popup_gone(), 0)
        time.sleep(0.3)
        self.assertEqual(self.calls("closed-tabs.sh"), [])
        self.assertEqual(self.calls("notify.sh"), [])

    def text(self):
        # tmux draws a lone space as a cursor move (ESC [C): that is a space.
        text = re.sub(rb"\x1b\[C", b" ", bytes(self.screen))
        return re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\(B", b"", text).decode("utf-8", "replace")

    def test_garbage_keeps_the_box_open(self):
        self.stays("zzz", "try 2h, fri 9am, next monday")

    def test_every_suggestion_fits(self):
        self.open_box()
        time.sleep(0.3)
        for x in ("30m", "tonight", "tomorrow morning", "tomorrow 2pm", "mon", "fri 9am", "next monday", "1w"):
            self.assertIn(x + " \u2014", self.text(), x)

    def test_empty_enter_takes_the_highlighted_suggestion(self):
        self.enter("")
        end = time.time() + 10
        while not self.calls("closed-tabs.sh") and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(self.calls("closed-tabs.sh"), [["snooze", str(self.now + 1800), "A"]])   # 30m

    def test_a_fuzzy_match_that_is_no_prefix_is_not_taken(self):
        # "mrw" narrows to "tomorrow" fuzzily, but is not how it starts.
        self.stays("mrw", "try 2h, fri 9am, next monday")

    def test_a_bare_number_asks_for_the_unit(self):
        # "3" starts "3h", but 3 what? Nothing is applied.
        self.stays("3", "3 what? say 3m, 3h or 3pm")


@unittest.skipUnless(TMUX and shutil.which("fzf"), "tmux / fzf missing")
class BoxOnScreen(Popup):
    """The box as it is seen: the client runs in a pane of a second private
    server (capture-pane does not see a popup; the pane it is drawn in does)."""
    COLS, ROWS = 100, 30

    def outer(self, *args):
        r = subprocess.run([TMUX, "-S", self.outer_sock, "-f", "/dev/null", *args], env=self.env,
                           capture_output=True, text=True, timeout=10)
        return r.stdout

    def box(self, cols=None, rows=None, *args):
        d = tempfile.mkdtemp(prefix="sno-", dir="/private/tmp")     # short: sun_path
        self.addCleanup(shutil.rmtree, d, True)
        self.outer_sock = os.path.join(d, "s")
        self.addCleanup(lambda: self.outer("kill-server"))
        self.outer("new-session", "-d", "-x", str(cols or self.COLS), "-y", str(rows or self.ROWS),
                   f"exec env -u TMUX '{self.tmux}' attach -t main")
        end = time.time() + 10
        tty = []
        while not tty and time.time() < end:
            tty = [t for t in self.t("list-clients", "-F", "#{client_tty}").split() if t != self.tty]
            time.sleep(0.05)
        self.assertTrue(tty, "no client in the outer pane")
        self.custom = subprocess.Popen([BASH, str(SCRIPT), "custom", "-c", tty[0], *(args or ["p:A"])],
                                       env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: self.custom.poll() is None and self.custom.kill())
        self.shows("type a time, or pick one", "esc cancel")

    def lines(self):
        return [x.strip().strip("\u2502").strip() for x in self.outer("capture-pane", "-p").splitlines() if x.strip()]

    def live(self):
        """The prompt's text, and the line under it."""
        ls = self.lines()
        i = [k for k, x in enumerate(ls) if x.startswith("until:")]
        return (ls[i[0]][len("until:"):].strip(), ls[i[0] + 2]) if i and len(ls) > i[0] + 2 else ("", "")

    def shows(self, *want, timeout=10):
        end = time.time() + timeout
        while True:
            text = "\n".join(self.lines())
            if all(w in text for w in want):
                return text
            if time.time() > end:
                self.fail(f"{want} not on screen:\n{text}")
            time.sleep(0.03)

    def keys(self, text, *named):
        if text:
            self.outer("send-keys", "-l", "--", text)
        if named:
            self.outer("send-keys", *named)

    def says(self, text, want, timeout=10):
        """Replace what is typed by text: the line under it becomes want."""
        self.keys("", "C-u")
        self.keys(text)
        end = time.time() + timeout
        while self.live() != (text, want):
            if time.time() > end:
                self.assertEqual(self.live(), (text, want))
            time.sleep(0.03)

    def applied(self, epoch):
        self.assertEqual(self.popup_gone(), 0)
        end = time.time() + 10
        while not self.calls("closed-tabs.sh") and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(self.calls("closed-tabs.sh"), [["snooze", str(epoch), "A"]])

    def nothing_applied(self):
        self.assertEqual(self.popup_gone(), 0)
        time.sleep(0.5)
        self.assertEqual(self.calls("closed-tabs.sh"), [])
        self.assertEqual(self.calls("notify.sh"), [])

    def test_it_opens_with_every_suggestion(self):
        self.box()
        text = self.shows("next monday \u2014 Mon Nov 23 9:00", "1w \u2014 Mon Nov 23 10:15", "\u23ce snooze")
        for x in ("30m \u2014 10:45", "tonight \u2014 18:00", "tomorrow morning \u2014 Tue 9:00", "fri 9am \u2014 Fri 9:00"):
            self.assertIn(x, text)
        self.keys("", "Escape")
        self.nothing_applied()

    def test_next_monday(self):
        self.box()
        self.says("next monday", "\u2192 Mon Nov 23 9:00 \u00b7 in 7 days")
        self.shows("esc cancel")                # the keys stay
        self.keys("", "Enter")
        self.applied(local(2026, 11, 23, 9))

    def test_typed_time_wins_over_the_highlight(self):
        # "tom" reads as tomorrow 9:00 and arrows move over "tomorrow 2pm".
        self.box()
        self.says("tom", "\u2192 Tue Nov 17 9:00 \u00b7 in 22h 45m")
        self.keys("", "Down")
        self.keys("", "Down")
        self.keys("", "Enter")
        self.applied(local(2026, 11, 17, 9))

    def test_tomorrow_morning_at_once(self):
        # Enter straight after the last letter: no wait for the live line.
        self.box()
        self.keys("tomorrow morning", "Enter")
        self.applied(local(2026, 11, 17, 9))

    def test_a_time_no_suggestion_is_like(self):
        self.box()
        self.says("dec 3 at 2:30pm", "\u2192 Thu Dec 3 14:30 \u00b7 in 17 days")
        self.keys("", "Enter")
        self.applied(local(2026, 12, 3, 14, 30))

    def test_the_start_of_a_suggestion_completes_it(self):
        self.box()
        self.says("tomorrow morn", "\u2192 tomorrow morning \u00b7 Tue Nov 17 9:00 \u00b7 in 22h 45m")
        self.keys("", "Enter")
        self.applied(local(2026, 11, 17, 9))

    def test_a_suggestion_by_arrow(self):
        self.box()
        self.keys("", "Down", "Down")
        time.sleep(0.3)
        self.keys("", "Enter")
        self.applied(self.now + 2 * 3600)

    def test_unreadable_stays_and_can_be_mended(self):
        self.box()
        self.says("next mondayy", "can't read that")
        self.keys("", "Enter")
        self.shows("can't read that \u00b7 try 2h, fri 9am, next monday")
        self.assertIsNone(self.custom.poll())
        self.keys("", "BSpace")
        self.shows("\u2192 Mon Nov 23 9:00")
        self.keys("", "Enter")
        self.applied(local(2026, 11, 23, 9))

    def test_reasons(self):
        self.box()
        for text, want in (("3", "3 what? say 3m, 3h or 3pm"), ("45", "45 what? say 45m or 45h"),
                           ("yesterday", "already passed"), ("0m", "no time at all"), ("99999y", "too far off")):
            with self.subTest(text=text):
                self.says(text, want)
                self.keys("", "Enter")
                time.sleep(0.3)
                self.assertIsNone(self.custom.poll())
                self.assertEqual(self.live(), (text, want))
        self.keys("", "Escape")
        self.nothing_applied()

    def test_odd_text_is_only_text(self):
        self.box()
        touched = Path(self.outer_sock).with_name("t")          # short: the line must fit
        for text in (f"$(touch {touched})", f"`touch {touched}`", f"';touch {touched};'", f'";touch {touched};"',
                     "2h)+abort", "become(echo 1)", "-5m", "--", "{q} {1}", "\\", "#{pid}"):
            with self.subTest(text=text):
                self.says(text, "can't read that")
                self.keys("", "Enter")
                self.shows("can't read that \u00b7 try")
                self.assertIsNone(self.custom.poll())
        self.assertFalse(touched.exists())
        self.keys("", "Escape")
        self.nothing_applied()

    def test_cleared_and_entered_at_once_is_not_the_old_row(self):
        # ^U and Enter in one burst: fzf still shows the old text's rows.
        self.box()
        self.says("1w", "\u2192 Mon Nov 23 10:15 \u00b7 in 7 days")
        self.keys("", "C-u", "Enter")
        time.sleep(0.8)
        if self.custom.poll() is None:              # not settled: Enter waits to be asked again
            self.assertEqual(self.calls("closed-tabs.sh"), [])
            self.keys("", "Enter")
        self.applied(self.now + 1800)               # 30m, the first row

    def test_a_small_client_still_gets_the_box(self):
        self.box(40, 14)
        self.says("next monday", "\u2192 Mon Nov 23 9:00 \u00b7 in 7 days")
        self.shows("esc cancel")
        self.keys("", "Enter")
        self.applied(local(2026, 11, 23, 9))


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
