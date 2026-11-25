"""snooze-when.py: reading a time written by hand.

"Now" is fixed and the zone is America/Denver (os.environ TZ + tzset), so every
named time is checked against zoneinfo. The module is loaded in-process (it is
called thousands of times here); the command line is run as a process, on the
system python 3.9 too.

LEGACY pins what the bash parser in snooze.sh answered before this one
existed, as seconds after each of NOWS, so it holds whichever way snooze.sh
parses today.
Run with unittest or pytest.
"""
import os
from datetime import datetime, timedelta
from pathlib import Path
import random
import re
import subprocess
import sys
import time
import types
import unittest
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get("SNOOZE_WHEN_UNDER_TEST", HERE / "scripts" / "executable_snooze-when.py"))
TZ = ZoneInfo("America/Denver")
# Executed, not imported: an import would leave a __pycache__ in scripts/.
W = types.ModuleType("snooze_when")
exec(compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec"), W.__dict__)


def local(y, mo, d, h=0, mi=0, s=0, fold=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=TZ, fold=fold).timestamp())


TUE = local(2026, 11, 24, 10, 15)       # a Tuesday morning
MON8 = local(2026, 11, 23, 8)           # Monday, before 9
FRI = local(2026, 11, 27, 19, 30)       # Friday evening
SAT = local(2026, 11, 28, 10, 15)
SUN = local(2026, 11, 29, 12)
NIGHT = local(2026, 11, 25, 1, 30)      # Wed 01:30, still Tuesday night
SPRING = local(2026, 3, 7, 20)          # Sat; 2:00 → 3:00 tomorrow
SPRING_NIGHT = local(2026, 3, 8, 1, 30)
AUTUMN = local(2026, 10, 31, 22)        # Sat; 2:00 → 1:00 tomorrow
AUTUMN_1 = local(2026, 11, 1, 1, 30)            # the first 1:30
AUTUMN_2 = local(2026, 11, 1, 1, 45, fold=1)    # the second 1:45
NYE = local(2026, 12, 31, 23, 30)
JAN31 = local(2027, 1, 31, 9)           # Sunday
NOWS = (TUE, MON8, FRI, SUN, NIGHT, SPRING, SPRING_NIGHT, AUTUMN, AUTUMN_1, AUTUMN_2, NYE, JAN31)

# What the bash parser said: seconds after each of NOWS (None: refused); one
# number where it is the same at all of them.
LEGACY = {
    "30m": 1800,
    "2h": 7200,
    "1d": 86400,
    "1w": 604800,
    "2h30m": 9000,
    "2h 30m": 9000,
    "45 min": 2700,
    "in 1 hour": 3600,
    "90min": 5400,
    "for 3 hours": 10800,
    "3 hours": 10800,
    "this 2h": 7200,
    "tonight": (27900, 36000, 81000, 21600, 59400, 75600, 55800, 75600, 63000, 58500, 66600, 32400),
    "evening": (27900, 36000, 81000, 21600, 59400, 75600, 55800, 75600, 63000, 58500, 66600, 32400),
    "this evening": (27900, 36000, 81000, 21600, 59400, 75600, 55800, 75600, 63000, 58500, 66600, 32400),
    "next evening": (27900, 36000, 81000, 21600, 59400, 75600, 55800, 75600, 63000, 58500, 66600, 32400),
    "today 5pm": (24300, 32400, None, 18000, 55800, None, 52200, None, 59400, 54900, None, 28800),
    "today tonight": (27900, 36000, None, 21600, 59400, None, 55800, None, 63000, 58500, None, 32400),
    "tomorrow": (81900, 90000, 48600, 75600, 27000, 43200, 23400, 43200, 30600, 26100, 34200, 86400),
    "tmrw": (81900, 90000, 48600, 75600, 27000, 43200, 23400, 43200, 30600, 26100, 34200, 86400),
    "tmr": (81900, 90000, 48600, 75600, 27000, 43200, 23400, 43200, 30600, 26100, 34200, 86400),
    "tom": (81900, 90000, 48600, 75600, 27000, 43200, 23400, 43200, 30600, 26100, 34200, 86400),
    "tomorrow 2pm": (99900, 108000, 66600, 93600, 45000, 61200, 41400, 61200, 48600, 44100, 52200, 104400),
    "tomorrow at 2pm": (99900, 108000, 66600, 93600, 45000, 61200, 41400, 61200, 48600, 44100, 52200, 104400),
    "tomorrow 2am": (56700, 64800, 23400, 50400, 1800, 21600, 1800, 18000, 5400, 900, 9000, 61200),
    "tomorrow 1am": (53100, 61200, 19800, 46800, 84600, 18000, 81000, 10800, 88200, 83700, 5400, 57600),
    "tomorrow evening": (114300, 122400, 81000, 108000, 59400, 75600, 55800, 75600, 63000, 58500, 66600, 118800),
    "tomorrow midnight": (49500, 57600, 16200, 43200, 81000, 14400, 77400, 7200, 84600, 80100, 1800, 54000),
    "mon": (513900, 3600, 221400, 75600, 459000, 129600, 109800, 129600, 117000, 112500, 293400, 86400),
    "tue": (600300, 90000, 307800, 162000, 545400, 216000, 196200, 216000, 203400, 198900, 379800, 172800),
    "weds": (81900, 176400, 394200, 248400, 27000, 302400, 282600, 302400, 289800, 285300, 466200, 259200),
    "thurs": (168300, 262800, 480600, 334800, 113400, 388800, 369000, 388800, 376200, 371700, 552600, 345600),
    "friday": (254700, 349200, 567000, 421200, 199800, 475200, 455400, 475200, 462600, 458100, 34200, 432000),
    "on saturday": (341100, 435600, 48600, 507600, 286200, 561600, 541800, 561600, 549000, 544500, 120600, 518400),
    "sun": (427500, 522000, 135000, 594000, 372600, 43200, 23400, 43200, 30600, 26100, 207000, 604800),
    "this fri": (254700, 349200, 567000, 421200, 199800, 475200, 455400, 475200, 462600, 458100, 34200, 432000),
    "fri 9am": (254700, 349200, 567000, 421200, 199800, 475200, 455400, 475200, 462600, 458100, 34200, 432000),
    "Fri 9 AM": (254700, 349200, 567000, 421200, 199800, 475200, 455400, 475200, 462600, 458100, 34200, 432000),
    "mon 11am": (521100, 10800, 228600, 82800, 466200, 136800, 117000, 136800, 124200, 119700, 300600, 93600),
    "mon 14:30": (533700, 23400, 241200, 95400, 478800, 149400, 129600, 149400, 136800, 132300, 313200, 106200),
    "sun 2:30am": (404100, 498600, 111600, 570600, 349200, 23400, 3600, 19800, 7200, 2700, 183600, 581400),
    "sun 1:30am": (400500, 495000, 108000, 567000, 345600, 19800, 601200, 12600, 608400, 603900, 180000, 577800),
    "friday evening": (287100, 381600, 599400, 453600, 232200, 507600, 487800, 507600, 495000, 490500, 66600, 464400),
    "evening 9am": (81900, 3600, 48600, 75600, 27000, 43200, 23400, 43200, 30600, 26100, 34200, 86400),
    "tonight 9pm": (38700, 46800, 5400, 32400, 70200, 3600, 66600, 86400, 73800, 69300, 77400, 43200),
    "9am": (81900, 3600, 48600, 75600, 27000, 43200, 23400, 43200, 30600, 26100, 34200, 86400),
    "9a": (81900, 3600, 48600, 75600, 27000, 43200, 23400, 43200, 30600, 26100, 34200, 86400),
    "2pm": (13500, 21600, 66600, 7200, 45000, 61200, 41400, 61200, 48600, 44100, 52200, 18000),
    "12am": (49500, 57600, 16200, 43200, 81000, 14400, 77400, 7200, 84600, 80100, 1800, 54000),
    "12pm": (6300, 14400, 59400, 86400, 37800, 54000, 34200, 54000, 41400, 36900, 45000, 10800),
    "2:15pm": (14400, 22500, 67500, 8100, 45900, 62100, 42300, 62100, 49500, 45000, 53100, 18900),
    "14:30": (15300, 23400, 68400, 9000, 46800, 63000, 43200, 63000, 50400, 45900, 54000, 19800),
    "08:05": (78600, 300, 45300, 72300, 23700, 39900, 20100, 39900, 27300, 22800, 30900, 83100),
    "2:30am": (58500, 66600, 25200, 52200, 3600, 23400, 3600, 19800, 7200, 2700, 10800, 63000),
    "1:50am": (56100, 64200, 22800, 49800, 1200, 21000, 1200, 13800, 1200, 86700, 8400, 60600),
    "noon": (6300, 14400, 59400, 86400, 37800, 54000, 34200, 54000, 41400, 36900, 45000, 10800),
    "midnight": (49500, 57600, 16200, 43200, 81000, 14400, 77400, 7200, 84600, 80100, 1800, 54000),
    "until noon": (6300, 14400, 59400, 86400, 37800, 54000, 34200, 54000, 41400, 36900, 45000, 10800),
    "at 9pm": (38700, 46800, 5400, 32400, 70200, 3600, 66600, 86400, 73800, 69300, 77400, 43200),
}


class Base(unittest.TestCase):
    def setUp(self):
        old = {k: os.environ.get(k) for k in ("TZ", "SNOOZE_NOW")}

        def back():
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            time.tzset()
        self.addCleanup(back)
        os.environ["TZ"] = "America/Denver"
        time.tzset()

    def when(self, text, now):
        """The epoch, or the kind of refusal. Anything else raised is a bug."""
        os.environ["SNOOZE_NOW"] = str(now)
        try:
            return W.safe_when(text, W.Now())
        except W.No as e:
            return e.kind

    def show(self, v):
        return v if isinstance(v, str) else datetime.fromtimestamp(v, TZ).strftime("%a %Y-%m-%d %H:%M:%S %Z")

    def check(self, now, table):
        """table: (text or texts, epoch or (y, mo, d, h, mi) or refusal kind)."""
        for texts, want in table:
            if isinstance(want, tuple):
                want = local(*want)
            for text in (texts if isinstance(texts, list) else [texts]):
                with self.subTest(now=self.show(now), text=text):
                    got = self.when(text, now)
                    self.assertEqual(self.show(got), self.show(want))
                    self.assertEqual(got, want)

    def refused(self, now, kind, *texts):
        for text in texts:
            with self.subTest(now=self.show(now), text=text):
                got = self.when(text, now)
                self.assertEqual(got, kind, self.show(got))


class Legacy(Base):
    def test_every_old_input_keeps_its_epoch(self):
        for text, wants in LEGACY.items():
            if not isinstance(wants, tuple):
                wants = (wants,) * len(NOWS)
            self.assertEqual(len(wants), len(NOWS))
            for now, want in zip(NOWS, wants):
                with self.subTest(text=text, now=self.show(now)):
                    got = self.when(text, now)
                    if want is None:
                        self.assertEqual(got, "passed")
                    else:
                        self.assertEqual(self.show(got), self.show(now + want))
                        self.assertEqual(got - now, want)

    def test_next_weekday_either_way_round(self):
        self.check(TUE, [("fri", (2026, 11, 27, 9)), (["next fri", "fri next"], (2026, 12, 4, 9)),
                         (["friday next 2pm", "2pm friday next", "on friday next at 2"], (2026, 12, 4, 14)),
                         ("friday next week", (2026, 12, 4, 9))])
        self.refused(TUE, "bad", "next friday next", "friday next month", "monday next friday")

    def test_a_clock_time_after_dark_is_pm(self):
        self.check(TUE, [
            (["tonight 9:30", "9:30 tonight", "evening 9:30", "tonight at 9:30", "tonight 0930", "tonight 09:30",
              "this evening at 9:30", "9:30 at night", "today 9:30 tonight"], (2026, 11, 24, 21, 30)),
            (["friday evening 9:30", "fri 9:30 evening", "friday night 9:30", "friday night at 9:30",
              "friday at 9:30 in the evening"], (2026, 11, 27, 21, 30)),
            ("dec 3 in the evening at 8:15", (2026, 12, 3, 20, 15)),
            ("tonight 11:59", (2026, 11, 24, 23, 59)),
            # am/pm, 12 and the 24-hour clock say it themselves
            ("tonight 9am", (2026, 11, 25, 9)), ("tonight 9:30am", (2026, 11, 25, 9, 30)),
            ("tonight 21:30", (2026, 11, 24, 21, 30)), ("tonight 0:30", (2026, 11, 25, 0, 30)),
            ("9:30", (2026, 11, 25, 9, 30)), ("friday 9:30", (2026, 11, 27, 9, 30)),
        ])

    def test_hours_after_dark(self):
        # One rule for "at 9" and "9:30": 4-11 is pm, 12 and 1-3 are past the
        # coming midnight (a named day's night runs on), and no evening.
        for now, day in ((TUE, 24), (NIGHT, 25)):
            for mins, form in ((0, "at %d"), (30, "%d:30"), (30, "at %d:30"), (30, "at half past %d")):
                for h in (1, 3, 4, 9, 11, 12):
                    said = form % h
                    pm = 4 <= h <= 11
                    for part in ("tonight", "night", "evening"):
                        for text in ((said + " " + part if part == "evening" else part + " " + said), part + " " + said):
                            if pm:
                                want = (2026, 11, day, h + 12, mins)
                            elif part == "evening":
                                want = "bad"
                            else:
                                want = local(2026, 11, day, h % 12, mins)
                                want = want if want > now else local(2026, 11, day + 1, h % 12, mins)
                            self.check(now, [(text, want)])
                    for part in ("night", "evening"):
                        want = ((2026, 11, 27, h + 12, mins) if pm else
                                "bad" if part == "evening" else (2026, 11, 28, h % 12, mins))
                        self.check(now, [("friday %s %s" % (part, said), want)])
        self.check(TUE, [("noon tonight", (2026, 11, 24, 12)), ("midnight tonight", (2026, 11, 25)),
                         ("friday night at midnight", (2026, 11, 28)), ("tonight 1230", (2026, 11, 25, 0, 30)),
                         ("eod tonight", (2026, 11, 24, 17)), ("today 1:30 tonight", (2026, 11, 25, 1, 30))])

    def test_the_error_fits_the_review_box(self):
        os.environ["SNOOZE_NOW"] = str(TUE)
        for text in ("x" * 40, "next mondayy at noon or so", "yesterday " * 4, "9" * 39 + "y", "0m " * 13):
            e = self.when(text, TUE)
            self.assertLessEqual(len("snooze: " + W.why(W.No(e), text)), 70, text)

    def test_a_number_joins_only_a_unit(self):
        self.check(TUE, [("45 min", TUE + 2700), ("2 h 30 m", TUE + 9000), ("9 am", (2026, 11, 25, 9)),
                         ("2 h30m", TUE + 9000), ("Fri 9 AM", (2026, 11, 27, 9))])
        self.refused(TUE, "bad", "3 2h", "1 2pm", "1 2:30", "2 30m", "1 2h30m", "1 2 h", "30 in m", "9 at am")

    def test_old_refusals_stay(self):
        self.refused(local(2026, 11, 16, 10, 15), "bad", "garbage", "9", "25:00", "13pm", "10:75",
                     "fri mon", "2pm 3pm", "*", "tomorrow 2pm extra", "3", "in 3", "2h 3", "-5m",
                     "12h pm", "$(date)", "`date`", "9; date", "2h && date")
        self.refused(TUE, "empty", "", "   ", "at", "in", "until", "snooze", "remind me", "this")
        self.refused(TUE, "zero", "0m", "0 days", "0h0m", "0 months", "zero hours")
        self.refused(TUE, "passed", "today 8am", "today")


class Durations(Base):
    def test_words(self):
        n = TUE
        self.check(n, [
            (["an hour", "in an hour", "a hour", "one hour", "for an hour", "an hour from now",
              "snooze for an hour", "Snooze until an hour from now", "1 hr.", "an hour later"], n + 3600),
            (["half an hour", "half hour", "a half hour", "in half an hour", "thirty minutes"], n + 1800),
            (["an hour and a half", "hour and a half", "one and a half hours", "1 and a half hours",
              "1.5h", "1.5 hours", "90 minutes"], n + 5400),
            (["a couple of hours", "a couple hours", "couple hours", "two hours", "2 hrs"], n + 7200),
            (["a few hours", "few hours", "three hours", "a few hours from now"], n + 3 * 3600),
            (["twelve hours", "half a day", "12 hrs"], n + 12 * 3600),
            (["2 hrs 30 min", "2 hours and 30 minutes", "two hours, thirty minutes",
              "two and a half hours", "2.5 hours", "2h30m from now"], n + 9000),
            (["forty five minutes", "forty-five minutes", "45 mins"], n + 2700),
            (["for the next two hours", "next 2 hours"], n + 7200),
            (["2h from now"], n + 7200),
            (["in a week", "a week", "1 week", "7 days", "a week from now"], n + 7 * 86400),
            (["a fortnight", "in a fortnight", "2 weeks"], n + 14 * 86400),
            (["a day and a half"], n + 36 * 3600),
            (["1.5d"], n + 36 * 3600),
        ])

    def test_calendar_months_and_years(self):
        self.check(TUE, [
            (["a month", "in 1 month", "in a month"], (2026, 12, 24, 10, 15)),
            ("3 months", (2027, 2, 24, 10, 15)),
            (["a year", "12 months"], (2027, 11, 24, 10, 15)),
            ("a year and a half", (2028, 5, 24, 10, 15)),
            ("1 month 2 days", (2026, 12, 26, 10, 15)),
            ("in 2 months at noon", (2027, 1, 24, 12)),
        ])
        self.check(JAN31, [("a month", (2027, 2, 28, 9)), ("13 months", (2028, 2, 29, 9)),
                           ("2 months", (2027, 3, 31, 9)), ("a month and a half", "bad")])
        self.check(NYE, [("2 months", (2027, 2, 28, 23, 30)), ("a month", (2027, 1, 31, 23, 30))])

    def test_a_day_or_longer_takes_a_time(self):
        self.check(TUE, [
            (["in 2 days at 9am", "2 days at 9", "9am in 2 days", "in two days 9am"],
             (2026, 11, 26, 9)),
            ("in a week at noon", (2026, 12, 1, 12)),
            ("in 3 days in the evening", (2026, 11, 27, 18)),
            ("a week from now at 3pm", (2026, 12, 1, 15)),
        ])
        self.refused(TUE, "bad", "2h at 9am", "in 48 hours at 9am", "in 30 min tonight at 9", "1.5 days at 9am", "2 days 9",
                     "in 2 days 3 hours at 9am", "2 days monday", "2 days and", "2 days and monday",
                     "2 hours monday 30m", "a", "an", "half", "couple", "a few", "a monday", "half a month")

    def test_from_a_day(self):
        self.check(TUE, [
            (["a week from monday", "week from monday", "1 week from mon", "a week after monday"],
             (2026, 12, 7, 9)),
            (["2 weeks from friday 3pm", "3pm 2 weeks from friday", "two weeks from friday at 3"],
             (2026, 12, 11, 15)),
            ("a week from tomorrow", (2026, 12, 2, 9)),
            ("a week from today", (2026, 12, 1, 9)),
            ("a week from tonight", (2026, 12, 1, 18)),
            ("a week from next monday", (2026, 12, 7, 9)),
            ("2 days after tomorrow", (2026, 11, 27, 9)),
            ("a month from dec 3", (2027, 1, 3, 9)),
            ("a week from monday morning at 8", (2026, 12, 7, 8)),
        ])
        self.refused(TUE, "bad", "2 hours from monday", "an hour after lunch", "a week from", "from monday",
                     "a week before monday", "a week from 3pm", "30 minutes before 5pm")


class Days(Base):
    def test_tuesday(self):
        self.check(TUE, [
            (["tomorrow", "Tomorrow!", "snooze until tomorrow", "remind me tomorrow", "by tomorrow",
              "first thing tomorrow", "first thing", "tomorrow first thing in the morning"], (2026, 11, 25, 9)),
            (["day after tomorrow", "the day after tomorrow", "a day after tomorrow"], (2026, 11, 26, 9)),
            ("day after tomorrow at noon", (2026, 11, 26, 12)),
            (["mon", "this monday", "on Monday", "next monday", "next mon.", "monday next week",
              "next week", "next week monday", "this coming monday", "until next monday"], (2026, 11, 30, 9)),
            (["fri", "this friday", "friday this week"], (2026, 11, 27, 9)),
            (["next friday", "friday next week", "next week on friday"], (2026, 12, 4, 9)),
            ("next tuesday", (2026, 12, 1, 9)), ("next wed at 3", (2026, 12, 2, 15)),
            ("tuesday", (2026, 12, 1, 9)), ("tuesday at 11", (2026, 11, 24, 11)),
            (["next week at 2pm", "2pm next week"], (2026, 11, 30, 14)),
            (["next month", "the 1st of next month"], (2026, 12, 1, 9)),
            ("the 15th of next month", (2026, 12, 15, 9)),
            (["next year", "jan 1"], (2027, 1, 1, 9)), ("dec 3 next year", (2027, 12, 3, 9)),
            (["this weekend", "weekend", "the weekend", "saturday"], (2026, 11, 28, 9)),
            ("next weekend", (2026, 12, 5, 9)), ("this weekend at noon", (2026, 11, 28, 12)),
            (["end of week", "end of the week", "eow", "end of this week"], (2026, 11, 27, 17)),
            ("end of the week at 3pm", (2026, 11, 27, 15)),
            ("end of next week", (2026, 12, 4, 17)),
            (["end of month", "end of the month", "eom"], (2026, 11, 30, 17)),
            ("end of next month", (2026, 12, 31, 17)), ("end of the year", (2026, 12, 31, 17)),
            (["end of day", "end of the day", "eod", "EOD today", "cob", "close of business",
              "end of today", "today eod"], (2026, 11, 24, 17)),
            (["end of day friday", "friday eod", "eod fri", "end of friday"], (2026, 11, 27, 17)),
            (["later", "later today"], TUE + 3 * 3600),
            (["first thing monday", "monday first thing"], (2026, 11, 30, 9)),
        ])

    def test_monday_before_nine(self):
        self.check(MON8, [
            (["monday", "this monday", "first thing"], (2026, 11, 23, 9)),
            (["next monday", "next week", "monday next week"], (2026, 11, 30, 9)),
            ("next friday", (2026, 12, 4, 9)), ("next sunday", (2026, 12, 6, 9)),
            ("a week from monday", (2026, 11, 30, 9)),
        ])

    def test_friday_evening(self):
        self.check(FRI, [
            (["end of week", "eow"], (2026, 12, 4, 17)), ("friday", (2026, 12, 4, 9)),
            ("friday at 9 pm", (2026, 11, 27, 21)), ("next friday", (2026, 12, 4, 9)),
            (["this weekend"], (2026, 11, 28, 9)), ("next weekend", (2026, 12, 5, 9)),
            (["eod", "cob"], (2026, 11, 28, 17)), ("later today", FRI + 3 * 3600),
            ("tonight at 9", (2026, 11, 27, 21)), ("tonight at 7", (2026, 11, 28, 19)),
            ("monday next week", (2026, 11, 30, 9)), ("this afternoon", "passed"),
            ("friday this week", "passed"), ("monday this week", "passed"),
            ("end of day today", "passed"),
        ])

    def test_in_the_weekend(self):
        self.check(SAT, [("this weekend", (2026, 11, 29, 9)), ("weekend evening", (2026, 11, 28, 18)),
                         ("next weekend", (2026, 12, 5, 9)), ("end of week", (2026, 12, 4, 17))])
        self.check(local(2026, 11, 28, 8), [("weekend", (2026, 11, 28, 9))])
        self.check(SUN, [("this weekend", (2026, 12, 5, 9)), ("this weekend at 3pm", (2026, 11, 29, 15)),
                         ("next weekend", (2026, 12, 5, 9)), ("next monday", (2026, 11, 30, 9)),
                         ("next week", (2026, 11, 30, 9)), ("next sunday", (2026, 12, 6, 9)),
                         ("tomorrow morning", (2026, 11, 30, 9))])

    def test_late_at_night(self):
        self.check(NIGHT, [
            (["tomorrow", "tomorrow morning", "in the morning", "this morning", "tomorrow at 9"],
             (2026, 11, 25, 9)),
            (["tomorrow night", "tonight at 9"], (2026, 11, 25, 21)),
            ("day after tomorrow", (2026, 11, 26, 9)), ("tomorrow at 2 am", (2026, 11, 25, 2)),
            ("tonight at 2", (2026, 11, 25, 2)), ("next wed", (2026, 12, 2, 9)),
            ("the 25th", (2026, 11, 25, 9)),
        ])

    def test_new_years_eve_and_jan_31(self):
        self.check(NYE, [
            (["tomorrow", "jan 1", "1/1", "next month", "next year", "the 1st"], (2027, 1, 1, 9)),
            ("dec 31", "passed"), ("dec 31 at 11:45pm", (2026, 12, 31, 23, 45)),
            ("midnight", (2027, 1, 1)),
            ("end of month", (2027, 1, 31, 17)), ("end of the year", (2027, 12, 31, 17)),
            ("next monday", (2027, 1, 4, 9)), ("later", NYE + 3 * 3600),
        ])
        self.check(JAN31, [
            ("next month", (2027, 2, 1, 9)), ("end of month", (2027, 1, 31, 17)),
            ("the 31st", "passed"), ("the 31st at 10", (2027, 1, 31, 10)),
            ("the 30th", (2027, 3, 30, 9)), ("the 31st of next month", "bad"),
            ("end of next month", (2027, 2, 28, 17)),
        ])

    def test_past(self):
        self.refused(TUE, "passed", "yesterday", "last monday", "last week", "last night", "yesterday at 9",
                     "2 hours ago", "today at 9am", "this morning", "nov 3 2026", "2025-12-03", "11/23/26")

    def test_conflicts(self):
        self.refused(TUE, "bad", "today yesterday", "today tomorrow", "monday tuesday", "dec 3 dec 4",
                     "9am 10am", "noon 3pm", "morning afternoon", "morning at 3pm", "afternoon 9am",
                     "dec 3 tonight", "tomorrow this afternoon", "next week tomorrow", "next month monday",
                     "next week next month", "later monday", "later 3pm", "fri dec 3", "friday the 3rd",
                     "monday or tuesday", "tomorrow please", "9-11am", "end of month next year",
                     "first thing tonight at 9", "first thing first thing", "evening at 2")

    def test_unread_words(self):
        self.refused(TUE, "bad", "end", "end of", "end of the", "next", "next next monday", "last", "day after",
                     "half past", "quarter to", "december", "dec", "this week", "weekend foo",
                     "close of", "remind", "o'clock", "am", "to", "next dec 3")


class Dates(Base):
    def test_dates(self):
        self.check(TUE, [
            (["dec 3", "Dec. 3", "december 3rd", "3 dec", "3rd december", "the 3rd of december", "12/3",
              "12/03", "12/3/26", "12/3/2026", "2026-12-03", "on dec 3", "dec 3 2026", "december 3rd, 2026",
              "the 3rd", "3rd", "on the 3rd", "thu dec 3", "Thursday, December 3rd", "dec3",
              "thursday the 3rd"], (2026, 12, 3, 9)),
            (["dec 3 at 2pm", "dec 3 at 2", "dec 3 2pm", "2pm dec 3", "3 dec 14:00", "12/3 2pm",
              "2026-12-03 14:00", "dec 3 1400", "at 2 dec 3", "dec 3 afternoon", "the 3rd at 2",
              "dec 3 2026 2pm"], (2026, 12, 3, 14)),
            ("9 dec 3rd", (2026, 12, 3, 9)),
            ("dec 3 2027", (2027, 12, 3, 9)), ("dec 3 2030", (2030, 12, 3, 9)),
            ("dec 3 at 2030", (2026, 12, 3, 20, 30)),
            ("nov 24", "passed"), ("nov 23", (2027, 11, 23, 9)), ("nov 24 at noon", (2026, 11, 24, 12)),
            ("the 24th", "passed"), ("the 23rd", (2026, 12, 23, 9)), ("the 24th at 11", (2026, 11, 24, 11)),
            ("feb 29", (2028, 2, 29, 9)), ("may 5", (2027, 5, 5, 9)), ("march 1st", (2027, 3, 1, 9)),
            ("sept 22nd", (2027, 9, 22, 9)), ("11/30", (2026, 11, 30, 9)), ("the 31st", (2026, 12, 31, 9)),
            ("Mon Nov 30 9:00", (2026, 11, 30, 9)),
            ("dec 3 in the evening", (2026, 12, 3, 18)), ("dec 3 2:30", (2026, 12, 3, 14, 30)), ("dec 3 02:30", (2026, 12, 3, 2, 30)),
        ])

    def test_invalid(self):
        self.refused(TUE, "bad", "feb 30", "feb 29 2027", "nov 31", "13/1", "12/32", "0/5", "12/0", "dec 0",
                     "dec 32", "2026-13-01", "2026-02-30", "the 32nd", "3st", "the 0th", "9 dec 3", "dec 3 3 dec",
                     "12-3", "1/2/3/4", "2026/12", "12/3/026", "dec 9am", "the 3", "2026-12", "//", "12/", "1:2:3")
        self.refused(TUE, "far", "dec 3 2199", "9999-01-01", "12/3/9999", "0001-01-01")


class Times(Base):
    def test_parts(self):
        self.check(TUE, [
            (["morning", "in the morning", "tomorrow morning"], (2026, 11, 25, 9)),
            (["noon", "midday", "lunch", "lunchtime", "at lunch"], (2026, 11, 24, 12)),
            (["after lunch"], (2026, 11, 24, 13)), (["afternoon", "this afternoon"], (2026, 11, 24, 14)),
            (["evening", "tonight", "this evening"], (2026, 11, 24, 18)),
            (["night", "at night"], (2026, 11, 24, 21)), ("midnight", (2026, 11, 25)),
            ("tomorrow night", (2026, 11, 25, 21)), ("monday morning", (2026, 11, 30, 9)),
            ("monday after lunch", (2026, 11, 30, 13)), ("noon tomorrow", (2026, 11, 25, 12)),
            ("next week in the afternoon", (2026, 11, 30, 14)),
        ])

    def test_the_part_decides_the_hour(self):
        self.check(TUE, [
            (["tonight at 9", "9 tonight", "tonight 9", "9 at night"], (2026, 11, 24, 21)),
            (["morning at 8", "8 in the morning", "tomorrow morning at 8"], (2026, 11, 25, 8)),
            ("tomorrow morning at 4", (2026, 11, 25, 4)),
            (["tomorrow evening at 7", "tomorrow at 7 in the evening"], (2026, 11, 25, 19)),
            ("tomorrow night 9:30", (2026, 11, 25, 21, 30)), ("this afternoon at 2:30", (2026, 11, 24, 14, 30)),
            ("tonight at 12", (2026, 11, 25)), ("tonight at 1", (2026, 11, 25, 1)),
            ("friday night at 1", (2026, 11, 28, 1)), ("friday night at midnight", (2026, 11, 28)),
            ("lunch at 1", (2026, 11, 24, 13)), ("half past 3 tomorrow morning", (2026, 11, 25, 3, 30)),
            ("quarter to 12 tomorrow morning", (2026, 11, 25, 11, 45)),
            ("monday evening at 18:30", (2026, 11, 30, 18, 30)),
        ])

    def test_bare_hours(self):
        self.check(TUE, [
            ("monday 9", (2026, 11, 30, 9)), ("9 monday", (2026, 11, 30, 9)), ("monday at 3", (2026, 11, 30, 15)),
            ("tomorrow 7", (2026, 11, 25, 7)), ("tomorrow 6", (2026, 11, 25, 18)),
            ("tomorrow 12", (2026, 11, 25, 12)), ("tomorrow at 1", (2026, 11, 25, 13)),
            ("tomorrow at 15", (2026, 11, 25, 15)), ("tomorrow at 09", (2026, 11, 25, 9)),
            ("at 9", (2026, 11, 24, 21)), ("at 11", (2026, 11, 24, 11)), ("at 3", (2026, 11, 24, 15)),
            ("at nine tomorrow", (2026, 11, 25, 9)), ("monday @ 3", (2026, 11, 30, 15)),
        ])
        self.refused(TUE, "bad", "9", "3", "12", "in 9", "for 3", "by 5", "nine", "at 0", "at 24", "at 99",
                     "tomorrow 260", "tomorrow 24")

    def test_clock_forms(self):
        self.check(TUE, [
            (["9 am", "9am", "9 a.m.", "9 AM", "9.00am", "900 am", "0900"], (2026, 11, 25, 9)),
            (["9.30am", "9:30 am", "930am", "9:30"], (2026, 11, 25, 9, 30)),
            (["1430", "14:30", "2:30pm", "2.30 pm", "half past 2 pm", "half past two", "230pm"],
             (2026, 11, 24, 14, 30)),
            (["quarter to 5 pm", "quarter to 5", "quarter-to five"], (2026, 11, 24, 16, 45)),
            ("quarter past 4 pm", (2026, 11, 24, 16, 15)), ("half past 3", (2026, 11, 24, 15, 30)),
            (["5 o'clock", "five o'clock", "5 o’clock", "5 oclock", "5 o'clock pm"], (2026, 11, 24, 17)),
            (["20 to 5", "twenty to five", "20 minutes to 5"], (2026, 11, 24, 16, 40)),
            ("ten past 3", (2026, 11, 24, 15, 10)), ("quarter to 12 pm", (2026, 11, 24, 11, 45)),
            ("quarter to 1", (2026, 11, 24, 12, 45)), ("quarter to 12 am", (2026, 11, 24, 23, 45)),
            ("9am monday", (2026, 11, 30, 9)), ("half past 8 monday", (2026, 11, 30, 8, 30)),
            ("2 a.m. friday", (2026, 11, 27, 2)),
        ])
        self.refused(TUE, "bad", "9.30", "1.5", "2460", "1299", "930", "13 am", "0 am", "1330 pm", "960am",
                     "half to 3", "quarter to", "quarter past 99", "half past 3 4", "9:5am", "9::30", "9:30:00",
                     "5 o'clock 6", "9amm", "0am", "quarter to 13 pm")


class Round1(Base):
    """The review's inputs, as ruled. A wrong silent reading is worse than a
    refusal."""
    FOUR = (TUE, FRI, SUN, NIGHT)

    def refuse(self, *texts):
        for now in self.FOUR:
            self.refused(now, "bad", *texts)

    def test_lonely_numbers(self):
        self.refuse("in 20", "20", "for 20", "in twenty", "15", "05", "2 days 15", "15,5 days", "13", "23", "09",
                    "twenty", "by 20", "until 15", "after 15", "around 8", "about 8", "for 8", "by 8", "in 20 tomorrow",
                    "for 9 tomorrow", "in 2027", "1,5h", "15 5 days", "930", "in 930", "in 2 days 15")
        self.check(TUE, [("at 20", (2026, 11, 24, 20)), ("mon 15", (2026, 11, 30, 15)), ("15 mon", (2026, 11, 30, 15)),
                         ("tomorrow 08", (2026, 11, 25, 8)), ("at 05", (2026, 11, 25, 5)), ("tonight 22", (2026, 11, 24, 22)),
                         ("in 2 days at 15", (2026, 11, 26, 15)), ("by 9 tomorrow", (2026, 11, 25, 9))])
        self.check(NIGHT, [("at 05", (2026, 11, 25, 5)), ("at 20", (2026, 11, 25, 20))])

    def test_a_clock_time_before_seven_is_pm(self):
        for h in range(1, 7):
            for now, day in ((TUE, 24), (SUN, 29), (NIGHT, 25)):
                self.check(now, [(["%d:30" % h, "half past %d" % h], (2026, 11, day, h + 12, 30))])
            self.check(FRI, [("%d:30" % h, (2026, 11, 28, h + 12, 30))])
        self.check(TUE, [
            ("mon 3:30", (2026, 11, 30, 15, 30)), ("tomorrow 4:15", (2026, 11, 25, 16, 15)),
            ("today 3:30", (2026, 11, 24, 15, 30)), ("dec 3 4:15", (2026, 12, 3, 16, 15)),
            ("quarter past 4 tomorrow", (2026, 11, 25, 16, 15)), ("next friday 2:45", (2026, 12, 4, 14, 45)),
            (["03:30", "0330", "3:30am", "3:30 am"], (2026, 11, 25, 3, 30)), ("mon 03:30", (2026, 11, 30, 3, 30)),
            ("dec 3 0330", (2026, 12, 3, 3, 30)), ("7:30", (2026, 11, 25, 7, 30)), ("12:30", (2026, 11, 24, 12, 30)),
            ("0:30", (2026, 11, 25, 0, 30)), ("tomorrow morning 3:30", (2026, 11, 25, 3, 30)),
            ("tonight 3:30", (2026, 11, 25, 3, 30)), ("tonight 6:30", (2026, 11, 24, 18, 30)),
        ])
        self.refuse("evening 3:30", "friday evening at 1", "03:30 in the afternoon")

    def test_n_to_h(self):
        self.refuse("9 to 5", "2 to 4", "8 to 6", "3 past 4", "15 to 5", "30 past 3", "5 to 9 monday", "10 to 11 monday",
                    "monday 10 to 11", "tomorrow 10 to 11", "10 to 11 tonight", "10 to 11 in the morning",
                    "10 to 11 am", "5 to 9pm", "monday 9 till 5", "9 till 5", "10 to 15 minutes", "2 to 3 hours")
        self.check(TUE, [("10 to 11", (2026, 11, 24, 10, 50)), ("5 past 4", (2026, 11, 24, 16, 5)),
                         ("20 to 5", (2026, 11, 24, 16, 40)), ("20 to 5 tomorrow", (2026, 11, 25, 16, 40)),
                         ("25 past 3 pm", (2026, 11, 24, 15, 25)), ("10 past 4 monday", (2026, 11, 30, 16, 10)),
                         ("15 minutes to 5", (2026, 11, 24, 16, 45)), ("10 minutes to 11 tomorrow", (2026, 11, 25, 10, 50))])
        self.check(FRI, [("10 to 11", (2026, 11, 27, 22, 50))])
        self.check(NIGHT, [("10 to 11", (2026, 11, 25, 10, 50))])

    def test_ranges_and_rates(self):
        self.refuse("1h-2h", "30m-1h", "1h - 2h", "2h - 30m", "an hour a day", "2 days a week", "1 day a week",
                    "a week a month", "30 minutes 2 hours, x", "an hour and 2 hours", "2 days, 3 days x",
                    "an hour and an hour", "30 minutes and 2 hours", "dec 3 - 5", "dec 3 - 2027", "9am - 5pm",
                    "9am-5pm", "mon - fri", "2 - 3 days", "monday -", "9 - 5", "- 2h")
        self.check(TUE, [("2 hours and 30 minutes", TUE + 9000), ("a week and a day", TUE + 8 * 86400),
                         ("monday - 9am", (2026, 11, 30, 9)), ("dec 3 - 2pm", (2026, 12, 3, 14)),
                         ("30-min", TUE + 1800)])

    def test_ordinal_weekday(self):
        self.refuse("the 2nd wednesday", "2nd wednesday", "3rd thursday", "second wednesday", "the first monday",
                    "2nd wednesday of december", "thursday the 4th", "the 2nd friday, 3pm")
        self.check(TUE, [(["thursday the 3rd", "thursday 3rd", "thu, the 3rd"], (2026, 12, 3, 9))])

    def test_quarter_to_midnight(self):
        self.check(TUE, [("quarter to 12 am friday", (2026, 11, 26, 23, 45)),
                         ("quarter to 12 am", (2026, 11, 24, 23, 45)), ("quarter to 12 pm friday", (2026, 11, 27, 11, 45)),
                         ("quarter to 12 friday night", (2026, 11, 27, 23, 45)), ("quarter to 12 tonight", (2026, 11, 24, 23, 45)),
                         ("quarter to 1 tonight", (2026, 11, 25, 0, 45)), ("quarter to 1 friday", (2026, 11, 27, 12, 45))])

    def test_spelled_and_glued_hours(self):
        self.refuse("one monday", "2mon", "two friday", "mon2", "monday nine", "five tomorrow", "2monday", "tomorrow9")
        self.check(TUE, [("monday at nine", (2026, 11, 30, 9)), ("at two friday", (2026, 11, 27, 14)),
                         ("mon2pm", (2026, 11, 30, 14)), ("dec3", (2026, 12, 3, 9)), ("nine o'clock monday", (2026, 11, 30, 9))])

    def test_seven_to_eleven_is_the_next_one(self):
        self.check(TUE, [("at 7", (2026, 11, 24, 19)), ("today at 9", (2026, 11, 24, 21)), ("9 today", (2026, 11, 24, 21)),
                         ("at 11", (2026, 11, 24, 11)), ("8 o'clock", (2026, 11, 24, 20)), ("half past 9", (2026, 11, 24, 21, 30)),
                         ("tomorrow at 9", (2026, 11, 25, 9)), ("monday 9", (2026, 11, 30, 9)), ("in 2 days at 9", (2026, 11, 26, 9)),
                         ("quarter to 7", (2026, 11, 24, 18, 45)), ("quarter to 11", (2026, 11, 24, 10, 45)),
                         ("quarter to 7 tomorrow", (2026, 11, 25, 6, 45)), ("half past 6 tomorrow", (2026, 11, 25, 18, 30)),
                         ("tomorrow 6:30", (2026, 11, 25, 18, 30)), ("9:30", (2026, 11, 25, 9, 30)), ("9am", (2026, 11, 25, 9)),
                         ("tomorrow at 7", (2026, 11, 25, 7))])
        self.check(FRI, [("at 8", (2026, 11, 27, 20)), ("at 7", (2026, 11, 28, 7)), ("today at 9", (2026, 11, 27, 21)),
                         ("today at 7", "passed"), ("at 3", (2026, 11, 28, 15))])
        self.check(NIGHT, [("at 9", (2026, 11, 25, 9)), ("today at 9", (2026, 11, 25, 9)), ("at 3", (2026, 11, 25, 15))])
        self.check(local(2026, 11, 24, 23, 30), [("at 11", (2026, 11, 25, 11)), ("today at 11", "passed")])

    def test_today_s_date(self):
        self.check(TUE, [(["nov 24", "11/24", "24 nov", "the 24th", "tue nov 24"], "passed"),
                         ("nov 24 at 11", (2026, 11, 24, 11)), ("nov 24 2026", "passed"), ("nov 24 2027", (2027, 11, 24, 9)),
                         ("nov 23", (2027, 11, 23, 9)), ("the 23rd", (2026, 12, 23, 9))])
        self.check(NIGHT, [(["nov 25", "the 25th"], (2026, 11, 25, 9))])
        self.check(FRI, [("nov 27 at 9pm", (2026, 11, 27, 21)), ("nov 27", "passed")])

    def test_days_said_otherwise(self):
        self.check(TUE, [
            ("tomorrow am", (2026, 11, 25, 9)), ("tomorrow pm", (2026, 11, 25, 14)),
            ("monday am", (2026, 11, 30, 9)), ("monday pm", (2026, 11, 30, 14)), ("monday pm at 3", (2026, 11, 30, 15)),
            ("tonite", (2026, 11, 24, 18)), (["later tonight", "late tonight"], (2026, 11, 24, 21)),
            ("later tonight at 10", (2026, 11, 24, 22)),
            (["early tomorrow", "early tomorrow morning"], (2026, 11, 25, 8)), ("early monday", (2026, 11, 30, 8)),
            ("late afternoon", (2026, 11, 24, 16)), ("tomorrow late afternoon", (2026, 11, 25, 16)),
            ("monday of next week", (2026, 11, 30, 9)), ("friday of this week", (2026, 11, 27, 9)),
            (["week after next", "the week after next", "monday after next"], (2026, 12, 7, 9)),
            ("friday after next", (2026, 12, 11, 9)), ("friday of next week at 3", (2026, 12, 4, 15)),
            (["coming monday", "the coming monday", "this coming monday"], (2026, 11, 30, 9)),
            (["next day", "the next day", "next morning"], (2026, 11, 25, 9)),
            (["beginning of next week", "start of next week", "the start of next week", "early next week"], (2026, 11, 30, 9)),
            (["start of next month", "beginning of next month", "early next month"], (2026, 12, 1, 9)),
            (["in january", "start of january", "in jan"], (2027, 1, 1, 9)), ("in january at 3pm", (2027, 1, 1, 15)),
            ("in december", (2026, 12, 1, 9)), ("end of november", (2026, 11, 30, 17)), ("end of february", (2027, 2, 28, 17)),
            ("after work", (2026, 11, 24, 17)), ("monday after work", (2026, 11, 30, 17)), ("after work at 6", (2026, 11, 24, 18)),
            (["dinner", "at dinner", "dinner time", "dinnertime"], (2026, 11, 24, 18)), ("dinner tomorrow at 7", (2026, 11, 25, 19)),
            ("monday.", (2026, 11, 30, 9)), ("monday;", (2026, 11, 30, 9)), ("monday, 9am!", (2026, 11, 30, 9)),
            ("monday \u2013 9am", (2026, 11, 30, 9)),
        ])
        self.check(FRI, [("later tonight", (2026, 11, 27, 21)), ("friday of this week", "passed"),
                         ("monday after next", (2026, 12, 7, 9)), ("next morning", (2026, 11, 28, 9)),
                         ("early tomorrow", (2026, 11, 28, 8)), ("after work", (2026, 11, 28, 17)), ("in december", (2026, 12, 1, 9))])
        self.check(local(2026, 11, 27, 22), [("later tonight", "passed"), ("late tonight at 11", (2026, 11, 27, 23))])
        self.check(SUN, [("week after next", (2026, 12, 7, 9)), ("coming monday", (2026, 11, 30, 9)),
                         ("end of november", (2026, 11, 30, 17)), ("early next week", (2026, 11, 30, 9))])
        self.check(NIGHT, [("later tonight", (2026, 11, 25, 21)), ("early tomorrow", (2026, 11, 25, 8)),
                           ("next morning", (2026, 11, 25, 9)), ("tomorrow am", (2026, 11, 25, 9))])
        self.check(local(2026, 11, 30, 18), [("end of november", "passed"), ("end of october", (2027, 10, 31, 17))])
        self.refuse("in november", "january", "am", "pm", "early", "early afternoon", "late", "late monday", "monday after",
                    "beginning of the week", "start of the week", "after monday", "after dec 3", "after 12/3", "after 3 dec",
                    "after tomorrow", "after dinner", "friday late tonight", "in january 2027", "christmas",
                    "week after", "coming")

    def test_durations_said_otherwise(self):
        n = TUE
        self.check(n, [
            (["hour", "next hour", "another hour", "in another hour", "one more hour"], n + 3600),
            (["5 more minutes", "another 5 minutes", "five more minutes"], n + 300),
            (["1h30", "1h 30", "an hour and 30", "1 hour 30", "1 1/2 hours", "in 1 1/2 hours", "1h30 from now"], n + 5400),
            (["an hour and 15", "an hour and fifteen"], n + 4500), ("1h05", n + 3900),
            (["quarter of an hour", "a quarter of an hour", "quarter hour"], n + 900),
            ("three quarters of an hour", n + 2700), (["1/2 hour", "30 min"], n + 1800), ("2 1/4 hours", n + 8100),
            (["around an hour", "about an hour", "after an hour", "in about an hour"], n + 3600),
            (["after 2 hours", "about 2 hours", "around 2 hours"], n + 7200),
            ("another day", n + 86400), ("a few more days", n + 3 * 86400),
        ])
        self.refuse("1h 5", "1h 60", "1h 30 monday", "2 days 30", "hours", "another", "more", "5 more", "1 1/2",
                    "half hour hour", "2 days hour", "an hour 5")

    def test_times_said_otherwise(self):
        self.check(TUE, [
            (["around 3pm", "about 3pm", "after 3pm", "by 3pm", "until 3pm", "around 3 pm"], (2026, 11, 24, 15)),
            ("after 5 tomorrow", (2026, 11, 25, 17)), ("around 3 tomorrow", (2026, 11, 25, 15)),
            ("tomorrow around 3", (2026, 11, 25, 15)), ("after noon", (2026, 11, 24, 12)),
            (["tomorrow at 930", "tomorrow 930"], (2026, 11, 25, 9, 30)), ("monday 930", (2026, 11, 30, 9, 30)),
            ("monday 330", (2026, 11, 30, 15, 30)), ("at 930", (2026, 11, 25, 9, 30)), ("monday 930pm", (2026, 11, 30, 21, 30)),
            ("five thirty", (2026, 11, 24, 17, 30)), ("five thirty pm", (2026, 11, 24, 17, 30)),
            ("tomorrow at nine fifteen", (2026, 11, 25, 9, 15)), ("ten forty five tomorrow", (2026, 11, 25, 10, 45)),
            ("five thirty am", (2026, 11, 25, 5, 30)), ("tonight nine thirty", (2026, 11, 24, 21, 30)),
            (["quarter after 5", "a quarter after 5", "quarter past 5"], (2026, 11, 24, 17, 15)),
            (["quarter til 5", "quarter till 5", "a quarter to 5", "quarter to 5"], (2026, 11, 24, 16, 45)),
            ("10 after 4", (2026, 11, 24, 16, 10)), ("10 minutes after 3pm", (2026, 11, 24, 15, 10)),
        ])
        self.check(FRI, [("five thirty", (2026, 11, 28, 17, 30)), ("after 3pm", (2026, 11, 28, 15))])

    def test_spelled_ordinals(self):
        self.check(TUE, [
            ("the fifteenth", (2026, 12, 15, 9)), (["first of december", "december first", "the first of december"], (2026, 12, 1, 9)),
            (["december the 3rd", "december the third", "third of december"], (2026, 12, 3, 9)),
            ("30th of this month", (2026, 11, 30, 9)), ("the thirtieth of this month", (2026, 11, 30, 9)),
            ("the twenty fifth", (2026, 11, 25, 9)), ("the twenty-eighth at 3", (2026, 11, 28, 15)),
            ("december twenty first", (2026, 12, 21, 9)), ("the 30th of the month", (2026, 11, 30, 9)),
            ("3rd of this month", "passed"), ("24th of this month", "passed"), ("31st of this month", "bad"),
            ("fifteenth of next month", (2026, 12, 15, 9)), ("first thing tomorrow", (2026, 11, 25, 9)),
        ])
        self.refuse("one first", "december the 3", "the fifteenth monday", "fifteenth of", "thirty fifteenth", "a second")

    def test_a_25_hour_day_is_hours(self):
        os.environ["SNOOZE_NOW"] = str(local(2026, 11, 1))
        now = W.Now()
        self.assertEqual(W.preview(local(2026, 11, 1, 23, 45), now), "Sun Nov 1 23:45 \u00b7 in 24h 45m")
        self.assertEqual(W.preview(local(2026, 11, 2, 0, 45), now), "Mon Nov 2 0:45 \u00b7 in 1 day")


class Round2(Base):
    refuse = Round1.refuse
    FOUR = Round1.FOUR

    def test_second_is_no_unit(self):
        self.refuse("1 second", "5 second", "15 second", "twenty second", "in twenty second", "5 first", "one second",
                    "fifteen second", "in 5 second", "thirty second", "twenty second monday")
        self.check(TUE, [(["december twenty second", "twenty second of december", "december the twenty second",
                           "twenty-second december"], (2026, 12, 22, 9)),
                         ("the twenty second", (2026, 12, 22, 9)), ("the second", (2026, 12, 2, 9)),
                         ("december second", (2026, 12, 2, 9)), ("twenty first", (2026, 12, 21, 9))])

    def test_the_bare_number(self):
        self.refuse("friday the 4", "tue the 1", "friday on the 4", "monday on the 7", "the 4 friday", "at the 4", "the 930 friday")
        self.check(TUE, [("fri 4", (2026, 11, 27, 16)), (["at the 4th", "the 4th", "friday the 4th"], (2026, 12, 4, 9)),
                         ("in the next 2 days at 9", (2026, 11, 26, 9))])

    def test_n_to_m_is_a_span(self):
        self.refuse("in 10 to 20", "for 5 to 10", "10 to 20", "5 to 13", "ten to twenty", "in 5 past 3", "in about 10 to 11",
                    "20 to 25")
        self.check(TUE, [("10 to 5", (2026, 11, 24, 16, 50)), ("5 to 10", (2026, 11, 24, 21, 55)),
                         ("quarter to 14", (2026, 11, 24, 13, 45)), ("10 past 15", (2026, 11, 24, 15, 10))])

    def test_a_year_is_no_time(self):
        self.refuse("until 2027", "around 2028", "monday 2027", "in about 2027", "2026", "2039", "2031", "in about 20",
                    "in around 8", "in the 20", "for about 9 tomorrow")
        self.check(TUE, [("at 2027", (2026, 11, 24, 20, 27)), ("monday at 2027", (2026, 11, 30, 20, 27)),
                         ("2030", (2026, 11, 24, 20, 30)), ("2040", (2026, 11, 24, 20, 40)), ("2025", (2026, 11, 24, 20, 25)),
                         ("dec 3 2027", (2027, 12, 3, 9)), ("at about 9 tomorrow", (2026, 11, 25, 9)),
                         ("in about an hour", TUE + 3600)])

    def test_this_night_before_five(self):
        self.check(NIGHT, [
            (["tonight at 2", "later tonight at 2", "late tonight at 2", "this night at 2", "today 2:00 tonight",
              "late tonight 2am"], (2026, 11, 25, 2)),
            (["late tonight 3am", "tonight at 3", "this night at 3"], (2026, 11, 25, 3)),
            (["tonight at 1", "later tonight at 1", "this night at 1"], (2026, 11, 26, 1)),
            ("later tonight at 11", (2026, 11, 25, 23)), ("later tonight", (2026, 11, 25, 21)),
            ("friday night at 2", (2026, 11, 28, 2)),
        ])
        self.check(TUE, [(["later tonight at 2", "this night at 2", "tonight at 2"], (2026, 11, 25, 2))])

    def test_before_dawn(self):
        self.check(TUE, [(["tonight 04:30", "04:30 tonight", "tonight 0430", "tonight at 04:30"], (2026, 11, 25, 4, 30)),
                         ("friday night 04:30", (2026, 11, 28, 4, 30)), ("tonight 04", (2026, 11, 25, 4)),
                         ("tonight 4:30", (2026, 11, 24, 16, 30)), ("friday night 4:30", (2026, 11, 27, 16, 30)),
                         ("tonight 03:30", (2026, 11, 25, 3, 30)), ("tonight 09:30", (2026, 11, 24, 21, 30))])
        self.check(NIGHT, [("tonight 04:30", (2026, 11, 25, 4, 30))])
        self.refuse("tonight 05:30", "friday night 05:30", "evening 04:30", "friday evening 05:30", "tonight 05")

    def test_hour_alone(self):
        self.refuse("on the hour", "by the hour", "the hour", "this hour", "at hour", "until hour", "monday hour",
                    "in the hour", "on the next monday hour")
        self.check(TUE, [(["hour", "in hour", "next hour", "in the next hour", "for hour", "another hour", "about hour",
                           "hour from now"], TUE + 3600), ("hour and a half", TUE + 5400)])

    def test_to_twelve(self):
        for now, day, h in ((TUE, 24, 11), (FRI, 27, 23), (SUN, 29, 23), (NIGHT, 25, 11)):
            self.check(now, [("quarter to 12", (2026, 11, day, h, 45)), (["ten to 12", "10 to 12"], (2026, 11, day, h, 50)),
                             ("quarter to 12 today", (2026, 11, day, h, 45))])
        self.check(TUE, [("quarter to 12 tomorrow", (2026, 11, 25, 11, 45)), ("half past 12", (2026, 11, 24, 12, 30))])
        self.check(local(2026, 11, 24, 23, 55), [("quarter to 12", (2026, 11, 25, 11, 45))])

    def test_spoken_after_in(self):
        self.refuse("in one thirty", "for two thirty", "in five ten", "in about five ten")
        self.check(TUE, [("at one thirty", (2026, 11, 24, 13, 30)), ("by five ten", (2026, 11, 24, 17, 10))])

    def test_fzf_counts(self):
        os.environ["SNOOZE_NOW"] = str(TUE)
        now = W.Now()
        take, wait = "become(echo %d)" % (TUE + 1800), "bg-cancel+bell"
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FZF_MATCH_COUNT", "FZF_TOTAL_COUNT")])
        for shown, total, want in ((None, None, take), ("12", None, take), (None, "12", take), ("12", "12", take),
                                   ("1", "12", wait), ("0", "12", wait), ("", "12", take)):
            for k, v in (("FZF_MATCH_COUNT", shown), ("FZF_TOTAL_COUNT", total)):
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
            self.assertEqual(W.enter("", "30m", now), want, (shown, total))
            self.assertEqual(W.enter("2h", "30m", now), "become(echo %d)" % (TUE + 7200))


class Dst(Base):
    def test_nine_is_nine(self):
        self.check(SPRING, [("tomorrow", (2026, 3, 8, 9)), ("in 2 days at 9am", (2026, 3, 9, 9)),
                            ("next monday", (2026, 3, 9, 9)), ("in a week at 9", (2026, 3, 14, 9)),
                            ("in a day", SPRING + 86400), ("a week", SPRING + 7 * 86400),
                            ("a month", (2026, 4, 7, 20)), ("half past 8 tomorrow", (2026, 3, 8, 8, 30))])
        self.check(AUTUMN, [("tomorrow", (2026, 11, 1, 9)), ("tomorrow night", (2026, 11, 1, 21)),
                            ("next monday", (2026, 11, 2, 9)), ("in a day", AUTUMN + 86400)])
        self.check(AUTUMN_2, [("in the morning", (2026, 11, 1, 9)), ("an hour", AUTUMN_2 + 3600)])

    def test_the_hour_that_is_not_there(self):
        # 2:30 does not exist on 2026-03-08: as far past the gap, 3:30 MDT.
        self.check(SPRING_NIGHT, [(["2:30am", "today at 2:30am", "half past 2 am", "3/8 2:30am"],
                                   (2026, 3, 8, 3, 30))])
        self.check(SPRING, [("half past 2 tomorrow morning", (2026, 3, 8, 3, 30))])

    def test_the_hour_that_is_there_twice(self):
        self.check(AUTUMN, [("half past 1 tomorrow morning", (2026, 11, 1, 1, 30))])
        # The first 1:50 is past; 1:50 rolls to the next day, as it always has.
        self.check(AUTUMN_2, [("1:50am", (2026, 11, 2, 1, 50))])

    def test_every_local_time_against_zoneinfo(self):
        for start in (local(2026, 3, 7), local(2026, 10, 31)):
            for k in range(0, 3 * 48):
                d = datetime.fromtimestamp(start, TZ).replace(tzinfo=None) + timedelta(minutes=30 * k)
                want = int(d.replace(tzinfo=TZ).timestamp())
                got = W.local(W.to_days(d.year, d.month, d.day), d.hour, d.minute)
                self.assertEqual(got, want, d)

    def test_calendar_arithmetic(self):
        d = datetime(2023, 12, 25)
        for k in range(0, 4000, 3):
            x = d + timedelta(days=k)
            n = W.to_days(x.year, x.month, x.day)
            self.assertEqual(n, (x - datetime(1970, 1, 1)).days)
            self.assertEqual(W.to_date(n), (x.year, x.month, x.day))
            self.assertEqual(W.weekday(n), x.weekday())


class Absurd(Base):
    def test_far_and_long(self):
        self.refused(TUE, "far", "99999y", "9" * 60 + "d", "9" * 150 + " hours", "99999999d", "2000 months",
                     "999999 weeks from monday", "in 99999 days at 9am", "999999999999h")
        self.refused(TUE, "bad", "9" * 201, "monday " * 100, "x" * 100000, "/" * 150, "1:" * 99, "1h-" * 40, "1/" * 99)

    def test_fuzz(self):
        rnd = random.Random(20261124)
        words = (list(W.WEEKDAY) + list(W.MONTH) + list(W.UNIT) + list(W.NUMBER) + list(W.PARTS) * 2 + list(W.PHRASE)
                 + list(W.NOON) + list(W.FILL) * 2 + list(W.TOMORROW) + list(W.EOD) +
                 "this next next last today yesterday weekend week month year end of first thing later a an half "
                 "couple few and from now after ago past to quarter o'clock am pm at at 9 3 12 30 1430 2026 9:30 "
                 "9.30am 11/30 12/3/26 2026-12-03 3rd the 31st dec 0 99 1.5 99999999 snooze remind me".split())
        chars = "0123456789:/.-apm hdw,'@’é٣\x00\t\n \udc80monthu"
        ok = 0
        for i in range(12000):
            if i % 3:
                text = " ".join(rnd.choice(words) for _ in range(rnd.randint(1, 5)))
            elif i % 2:
                text = "".join(rnd.choice(chars) for _ in range(rnd.randint(1, 24)))
            else:
                text = rnd.randbytes(rnd.randint(1, 30)).decode("latin-1")
            now = rnd.choice(NOWS)
            try:
                got = self.when(text, now)
            except Exception as e:
                self.fail("%r at %s: %r" % (text, now, e))
            if not isinstance(got, str):
                ok += 1
                self.assertGreater(got, now, text)
        self.assertGreater(ok, 500)          # the salads do hit the grammar

    def test_no_bug_hides_behind_far(self):
        # safe_when turns a calendar overflow into "too far off"; `when` itself
        # must not need that for any text of a normal size.
        for text in list(LEGACY) + ["next monday", "dec 3 2199", "9999-01-01", "0001-01-01", "\udc80", "9" * 150]:
            for now in NOWS:
                os.environ["SNOOZE_NOW"] = str(now)
                try:
                    W.when(text, W.Now())
                except W.No:
                    pass


PYTHONS = [p for p in ("/usr/bin/python3", sys.executable) if os.path.exists(p)]


class CommandLine(Base):
    def run_it(self, *args, stdin=b"", now=TUE, python=PYTHONS[0], env=None):
        e = {"PATH": "/usr/bin:/bin", "TZ": "America/Denver", "SNOOZE_NOW": str(now), **(env or {})}
        return subprocess.run([python, "-I", "-S", str(SCRIPT), *args], input=stdin, env=e,
                              capture_output=True, timeout=30)

    def test_parse(self):
        for python in PYTHONS:
            with self.subTest(python=python):
                r = self.run_it("parse", "next", "monday", python=python)
                self.assertEqual((r.returncode, r.stdout, r.stderr),
                                 (0, b"%d\n" % local(2026, 11, 30, 9), b""))
        for args, why in ((["9"], "can't read '9' (try 2h,"), ([], "when? (try "), (["0m"], "'0m' is no time at all"),
                          (["Last", "MONDAY"], "'last monday' has already passed"), (["99999y"], "'99999y' is too far"),
                          (["a\nb\x1b[31m"], "can't read 'a b [31m' (try"), (["x" * 500], "can't read 'xxx"),
                          ([b"\xff\xfe"], "can't read ")):
            with self.subTest(args=args):
                r = self.run_it("parse", *args)
                self.assertEqual((r.returncode, r.stdout), (1, b""))
                err = r.stderr.decode()
                self.assertTrue(err.startswith("snooze: " + why), err)
                self.assertEqual(err.count("\n"), 1)
                self.assertLess(len(err), 200)

    def test_after_dashes_it_is_text(self):
        r = self.run_it("parse", "--", "next monday")
        self.assertEqual(r.stdout, b"%d\n" % local(2026, 11, 30, 9))
        for text in ("--", "-5m", "parse", "--help"):
            r = self.run_it("parse", "--", text)
            self.assertEqual((r.returncode, r.stdout), (1, b""))
            self.assertIn(("can't read '%s'" % text).encode(), r.stderr)

    def test_the_box(self):
        for q, sel, line, act in (
                ("next monday", "30m", "\u2192 Mon Nov 30 9:00 \u00b7 in 6 days", local(2026, 11, 30, 9)),
                ("tomo", "tomorrow", "\u2192 tomorrow \u00b7 Wed Nov 25 9:00 \u00b7 in 22h 45m", local(2026, 11, 25, 9)),
                ("  TOMO ", "tomorrow", "\u2192 tomorrow \u00b7 Wed Nov 25 9:00 \u00b7 in 22h 45m", local(2026, 11, 25, 9)),
                ("tom", "tomorrow 2pm", "\u2192 Wed Nov 25 9:00 \u00b7 in 22h 45m", local(2026, 11, 25, 9)),
                ("", "30m", "type a time, or pick one", TUE + 1800),
                ("", "", "type a time, or pick one", "when? \u00b7 try 2h, fri 9am, next monday"),
                ("3", "3h", "3 what? say 3m, 3h or 3pm", "3 what? say 3m, 3h or 3pm"),
                ("mrw", "tomorrow", "can't read that", "can't read that \u00b7 try 2h, fri 9am, next monday"),
                ("nope)+abort", "", "can't read that", "can't read that \u00b7 try 2h, fri 9am, next monday"),
                ("at", "", "\u2026", "when? \u00b7 try 2h, fri 9am, next monday"),
                ("yesterday", "", "already passed", "already passed")):
            with self.subTest(q=q, sel=sel):
                r = self.run_it("box", "--", q, sel)
                self.assertEqual((r.returncode, r.stdout.decode()), (0, line + "\n"))
                r = self.run_it("enter", "--", q, sel)
                want = "become(echo %d)" % act if isinstance(act, int) else "bg-cancel+bell+change-header(%s)" % act
                self.assertEqual((r.returncode, r.stdout.decode()), (0, want + "\n"))

    def test_enter_never_repeats_the_text(self):
        rnd = random.Random(5)
        os.environ["SNOOZE_NOW"] = str(TUE)
        now = W.Now()
        fixed = re.compile(r"become\(echo \d+\)|bg-cancel\+bell\+change-header\([a-z0-9 ,'?\u00b7]+\)")
        for _ in range(3000):
            q = "".join(rnd.choice("()+:;'\"$`{}abecomt123 \n") for _ in range(rnd.randint(0, 12)))
            self.assertRegex(W.enter(q, rnd.choice(["", "30m", "tomorrow"]), now), "^(?:%s)$" % fixed.pattern)
            self.assertNotIn("\n", W.box(q, "30m", now))

    def test_preview(self):
        for args, want, now in (
                (["next", "monday"], "Mon Nov 30 9:00 · in 6 days", TUE),
                (["45m"], "Tue Nov 24 11:00 · in 45m", TUE), (["2pm"], "Tue Nov 24 14:00 · in 3h 45m", TUE),
                (["2h"], "Tue Nov 24 12:15 · in 2h", TUE), (["1d"], "Wed Nov 25 10:15 · in 1 day", TUE),
                (["midnight"], "Wed Nov 25 0:00 · in 13h 45m", TUE),
                (["dec", "3", "2027"], "Fri Dec 3 2027 9:00 · in 12 months", TUE),
                (["dec", "3", "2029"], "Mon Dec 3 2029 9:00 · in 3 years", TUE),
                ([], "", TUE), (["at"], "", TUE), (["9"], "9 what? say 9m, 9h or 9pm", TUE), (["45"], "45 what? say 45m or 45h", TUE),
                (["nex"], "can't read that", TUE),
                (["yesterday"], "already passed", TUE), (["0m"], "no time at all", TUE), (["99999y"], "too far off", TUE)):
            with self.subTest(args=args):
                r = self.run_it("preview", *args, now=now)
                self.assertEqual((r.returncode, r.stderr), (0, b""))
                self.assertEqual(r.stdout.decode(), want + "\n" if want else "")

    def test_preview_in_the_c_locale(self):
        r = self.run_it("preview", "next monday", env={"LC_ALL": "C"})
        self.assertEqual(r.stdout.decode(), "Mon Nov 30 9:00 · in 6 days\n")

    def test_batch(self):
        r = self.run_it("batch", stdin="30m\nnope\n\ntonight\ntomorrow 2pm\nnext monday\ndec 3\njan 5\n".encode() + b"\xff\n")
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        want = [("30m", TUE + 1800, "10:45"), ("tonight", local(2026, 11, 24, 18), "18:00"),
                ("tomorrow 2pm", local(2026, 11, 25, 14), "Wed 14:00"),
                ("next monday", local(2026, 11, 30, 9), "Mon 9:00"),
                ("dec 3", local(2026, 12, 3, 9), "Thu Dec 3 9:00"),
                ("jan 5", local(2027, 1, 5, 9), "Tue Jan 5 2027 9:00")]
        self.assertEqual(r.stdout.decode(), "".join("%s\t%d\t%s\n" % w for w in want))

    def test_the_real_clock(self):
        r = self.run_it("parse", "1h", env={"SNOOZE_NOW": ""})
        self.assertAlmostEqual(int(r.stdout), time.time() + 3600, delta=20)

    def test_other_verbs(self):
        self.assertEqual(self.run_it().returncode, 2)
        self.assertEqual(self.run_it("eval", "1").returncode, 2)


if __name__ == "__main__":
    unittest.main()
