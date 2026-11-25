#!/usr/bin/env python3
"""snooze-when — read a time written by hand ("next monday", "dec 3 at 2pm").

  snooze-when.py parse [--] <text…>    the epoch on stdout, or exit 1 with one
                                  line "snooze: …" on stderr
  snooze-when.py batch            one text per stdin line → text TAB epoch TAB
                                  label; unreadable lines are skipped
  snooze-when.py preview [--] <text…>  one line about it ("Mon Nov 30 9:00 · in
                                  6 days", or why it can't be read); always
                                  exit 0, nothing for empty text
  snooze-when.py box <query> <suggestion>
                                  the time box's live line: what Enter would
                                  take ("→ Mon Nov 30 9:00 · in 6 days")
  snooze-when.py enter <query> <suggestion>
                                  its Enter, as fzf actions: leave with the
                                  epoch, or stay open and say why not. Never
                                  any of the typed text: fzf reads this.

Text after -- is only text ("-5m", "parse"). Reading the box: the typed text
wins when it reads as a time; nothing typed, or the start of the highlighted
suggestion ("tomo" → tomorrow), is that suggestion; a bare number never is
("3": 3h? 3pm?).

Now is $SNOOZE_NOW (an epoch), else the clock. Local time (TZ / the system).
The result is always in the future, and nothing is guessed or dropped: a word
that is neither read nor on the filler list, two days, or two times refuse the
whole text.

  durations  30m 2h30m 1h30 45 min · an hour and a half · half an hour · a
             couple of hours · a few days (3) · 1 1/2 hours · 5 more minutes ·
             a fortnight · 2 months / a year (calendar, end of month clamped).
             Day or longer may take a time: in 2 days at 9am · and may count
             from a day: a week from monday · 2 weeks from friday 3pm. A range
             (1h-2h) or a rate (2 days a week) is refused
  days       today · tomorrow (before 05:00: the day just begun) · day after
             tomorrow · mon..sun, this / coming mon (the next such day) · next
             mon = mon next (the one in the NEXT Monday-based week) · monday of
             next week · monday after next · next week (Monday) / month (1st) /
             year · week after next · this / next weekend · end of (the / next)
             week / month / year (17:00) · later (today) = +3 h
  dates      dec 3 · december the 3rd 2027 · first of december · the 3rd ·
             30th of this month · 11/30 · 12/3/26 · 2026-12-03 · in january
             (1st) · end of november. No year: the next such date; today's,
             its time past, is past
  parts      morning 9 · noon, lunch 12 · after lunch 13 · afternoon, pm 14 ·
             late afternoon 16 · eod, cob, after work 17 · evening, tonight,
             dinner 18 · night, late tonight 21 · midnight · first thing 9 ·
             early (tomorrow) 8
  times      9am · 9:30 pm · 9.30am · 930am · 14:30 · 1430 · half past 3 ·
             quarter to 5 pm · 20 past 4 · five thirty · 5 o'clock. A bare hour
             (or 930) only beside a day, a part of day or "at"; spelled, only
             with "at". A lone number is refused
  am or pm   beside a part of day, the reading inside it (tonight: 4-11 pm, 12
             and 1-3 past midnight). Else 1-6, 1:00-6:59 are pm (03:30 is not),
             12 noon, 7-11 am; a spoken or bare 7-11 with no day, or today,
             the next one ("at 8" in the evening is 20:00)

Two passes. `legacy` is the parser snooze.sh had in bash: what it read keeps
its meaning, with these exceptions (Mack, 2026-11-25): "next friday" / "friday
next" was a bare Friday; "tonight 9:30" was 9:30 am, "mon 3:30" 3:30 am; "3
2h" and "1 2pm" were 32h and 12pm (a number joined any next word). Everything
else goes through Reader: PHRASE puts one way of saying it for another, then
each item is read in turn, in any order.

It runs on every keystroke of the time box, so no imports but these three (re
and datetime cost 15 ms each on the system python 3.9).
"""
import os
import sys
import time

MAX_TEXT = 200                  # characters
MAX_SECS = 36525 * 86400        # ~100 years: past this it is a typo
LATE_NIGHT_END = 5              # before 05:00 "tomorrow" is the day just begun
DAY_START = (9, 0)              # a day without a time
DAY_END = (17, 0)               # end of day / week / month
EARLY = (8, 0)
LATER_SECS = 3 * 3600
# Short: agent-ui's review box shows one line, 70 columns in its usual popup.
HINT = "try 2h, fri 9am, next monday"

DIGITS = frozenset("0123456789")
LETTERS = frozenset("abcdefghijklmnopqrstuvwxyz")
WDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def table(*rows):
    return {w: v for v, names in rows for w in names.split()}


WEEKDAY = table((0, "mon monday"), (1, "tue tues tuesday"), (2, "wed weds wednesday"),
                (3, "thu thur thurs thursday"), (4, "fri friday"), (5, "sat saturday"),
                (6, "sun sunday"))
MONTH = table((1, "jan january"), (2, "feb february"), (3, "mar march"), (4, "apr april"),
              (5, "may"), (6, "jun june"), (7, "jul july"), (8, "aug august"),
              (9, "sep sept september"), (10, "oct october"), (11, "nov november"),
              (12, "dec december"))
LEGACY_UNIT = table((60, "minutes minute mins min m"), (3600, "hours hour hrs hr h"),
                    (86400, "days day d"), (604800, "weeks week wks wk w"))
LEGACY_AMPM = frozenset("am pm a p".split())
# unit → (seconds, months)
UNIT = {w: (s, 0) for w, s in LEGACY_UNIT.items()}
UNIT.update(table(((14 * 86400, 0), "fortnight fortnights"), ((0, 1), "month months mo mos"),
                  ((0, 12), "year years yr yrs y")))
NUMBER = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty".split())}
NUMBER.update(thirty=30, forty=40, fifty=50)
ORDINAL = {w: i + 1 for i, w in enumerate(
    "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth "
    "fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth".split())}
ORDINAL["thirtieth"] = 30
FRACTION = {"1/2": (1, 2), "1/4": (1, 4), "3/4": (3, 4)}
TOMORROW = frozenset("tomorrow tmrw tmr tom tomorow tommorow tommorrow".split())
LEGACY_FILL = frozenset("in at until this on next for".split())
FILL = frozenset("in until till til on for by the around about".split())
SOFT = frozenset("the around about".split())
NOON = {"noon": (12, 0), "midday": (12, 0), "midnight": (0, 0)}
EOD = frozenset("eod cob".split())
AMPM = {"am": 0, "pm": 12}
# "20 to 5", "quarter past 4": is it before the hour?
TO = {"to": True, "til": True, "till": True, "past": False, "after": False}
WEEKS = {"this": 0, "next": 7, "after next": 14}

# Parts of day: the time said by the part alone, and the minutes of the day
# (past 1440: of the next one) an hour beside it may mean. "tonight at 1" is
# 1:00 past midnight; "evening at 1" is nothing.
PARTS = {
    "morning": (9, 0, 60, 720),
    "lunch": (12, 0, 660, 960), "after-lunch": (13, 0, 660, 960),
    "afternoon": (14, 0, 720, 1140), "late-afternoon": (16, 0, 720, 1140),
    "after-work": (17, 0, 960, 1440), "dinner": (18, 0, 960, 1440), "evening": (18, 0, 960, 1440),
    "tonight": (18, 0, 960, 1680), "night": (21, 0, 960, 1680), "late-night": (21, 0, 960, 1680),
}
# The same for a time that says am/pm or is on the 24-hour clock.
SURE = {"morning": (0, 720), "tonight": (960, 1740), "night": (960, 1740), "late-night": (960, 1740)}
PREDAWN = ("04", "05")          # written so, "tonight 04:30" is the morning after
AFTER_DARK = frozenset("evening tonight night late-night".split())
# These roll to tomorrow when past ("this evening" always did); "this morning"
# is today's or nothing.
ROLLING = frozenset("evening tonight dinner".split())

# Said one way or another: put into one way before reading. (A name with "-"
# is ours alone; typed, the "-" parts the words.)
PHRASE = {
    "tonite": "tonight", "oclock": "o'clock", "o clock": "o'clock",
    "lunchtime": "lunch", "lunch time": "lunch", "dinnertime": "dinner", "dinner time": "dinner",
    "after lunch": "after-lunch", "after lunchtime": "after-lunch", "after work": "after-work",
    "late afternoon": "late-afternoon", "later tonight": "late-night", "late tonight": "late-night",
    "first thing": "first-thing", "later today": "later", "today later": "later",
    "end of day": "eod", "end of the day": "eod", "end of today": "eod today",
    "close of business": "eod", "end of business": "eod",
    "next day": "tomorrow", "next morning": "tomorrow morning", "week after next": "after-next",
    "quarter hour": "fifteen minutes", "quarter of an hour": "fifteen minutes",
    "a quarter of an hour": "fifteen minutes", "three quarters of an hour": "forty five minutes",
}
for _w in TOMORROW:
    PHRASE["day after " + _w] = PHRASE["a day after " + _w] = "overmorrow"
for _w in ("week", "month", "year"):
    for _p in ("early", "start of", "beginning of"):
        PHRASE["%s next %s" % (_p, _w)] = "next " + _w
for _w in TO:
    PHRASE["a quarter " + _w] = "quarter " + _w
PHRASE_WORDS = 5                # the longest


class No(Exception):
    """The text is no time. kind: bad, empty, zero, passed, far."""
    def __init__(self, kind="bad"):
        Exception.__init__(self, kind)
        self.kind = kind


# --- the calendar: days since 1970-01-01, and local wall-clock times --------

def to_days(y, m, d):
    y -= m <= 2
    era = y // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    return era * 146097 + yoe * 365 + yoe // 4 - yoe // 100 + doy - 719468


def to_date(days):
    z = days + 719468
    era = z // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    m = mp + (3 if mp < 10 else -9)
    return yoe + era * 400 + (m <= 2), m, doy - (153 * mp + 2) // 5 + 1


def month_len(y, m):
    return to_days(y + m // 12, m % 12 + 1, 1) - to_days(y, m, 1)


def add_months(days, n):
    y, m, d = to_date(days)
    m0 = y * 12 + m - 1 + n
    y, m = m0 // 12, m0 % 12 + 1
    return to_days(y, m, min(d, month_len(y, m)))


def weekday(days):
    return (days + 3) % 7


def local(days, h, m, s=0):
    """The epoch of that wall-clock time. One that happens twice: the first;
    one the spring change skips: as far past the gap (2:30 → 3:30)."""
    wall = days * 86400 + h * 3600 + m * 60 + s
    before = time.localtime(wall - 86400 - 3600).tm_gmtoff
    after = time.localtime(wall + 86400 + 3600).tm_gmtoff
    for off in sorted({before, after}, reverse=True):
        t = wall - off
        if time.localtime(t).tm_gmtoff == off:
            return t
    return wall - before


class Now(object):
    def __init__(self):
        self.t = 0
        v = os.environ.get("SNOOZE_NOW", "")
        if v and set(v) <= DIGITS and len(v) < 12:
            self.t = int(v)
        else:
            self.t = int(time.time())
        lt = time.localtime(self.t)
        self.days = to_days(lt.tm_year, lt.tm_mon, lt.tm_mday)
        self.wd = lt.tm_wday
        self.hour = lt.tm_hour
        self.clock = (lt.tm_hour, lt.tm_min, lt.tm_sec)


# --- hours ------------------------------------------------------------------

def fit(part, r, vague):
    """Minutes r, as the part of day it is said beside takes them: minutes
    from the named day's midnight. vague: a 12-hour clock without am/pm, so
    the one reading inside the part. "evening 3:30" has none and is refused."""
    lo, hi = PARTS[part][2:]
    if not vague:
        lo, hi = SURE.get(part, (lo, hi))
    for x in (range(r % 720, 2160, 720) if vague else (r, r + 1440)):
        if lo <= x < hi:
            return x
    raise No()


# --- pass 1: what snooze.sh always read -------------------------------------

def legacy_time(w):
    if w == "noon":
        return 12, 0
    if w == "midnight":
        return 0, 0
    ap = ""
    for s in ("am", "pm", "a", "p"):
        if w.endswith(s):
            ap, w = s[0], w[:-len(s)]
            break
    h, colon, m = w.partition(":")
    if not (1 <= len(h) <= 2 and set(h) <= DIGITS):
        return None
    if colon and not (len(m) == 2 and set(m) <= DIGITS):
        return None
    if not (colon or ap):
        return None
    h, m = int(h), int(m or 0)
    if ap:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ap == "p" else 0)
    return (h, m) if h <= 23 and m <= 59 else None


def legacy_duration(w):
    secs, i = 0, 0
    if not w:
        return None
    while i < len(w):
        j = i
        while j < len(w) and w[j] in DIGITS:
            j += 1
        # The longest unit name that fits, as the regex alternation had it.
        for n in range(min(7, len(w) - j), 0, -1):
            if w[j:j + n] in LEGACY_UNIT:
                break
        else:
            return None
        if j == i:
            return None
        secs += int(w[i:j]) * LEGACY_UNIT[w[j:j + n]]
        i = j + n
    return secs


def legacy(text, now):
    """snooze.sh's parser: the epoch, or None when it could not read it (or
    read it as past)."""
    raw = [w for w in text.replace("\t", " ").replace("\n", " ").split(" ") if w]
    words, num = [], ""
    for i, w in enumerate(raw):
        near = raw[max(i - 1, 0):i + 2]
        if w == "next" and not WEEKDAY.keys().isdisjoint(near):
            return None
        if num:                                 # "45 min", "9 am"
            if w not in LEGACY_UNIT and w not in LEGACY_AMPM:
                return None
            words.append(num + w)
            num = ""
        elif set(w) <= DIGITS:
            num = w
        elif w not in LEGACY_FILL:
            words.append(w)
    if num or not words:
        return None
    durs = [legacy_duration(w) for w in words]
    if None not in durs:
        secs = sum(durs)
        if secs > MAX_SECS:
            raise No("far")
        return now.t + secs if secs > 0 else None
    day = tod = None
    dark, clock, plus = "", "", 0
    for w in words:
        if w in ("tonight", "evening"):
            tod = tod or "evening"
            dark = dark if dark == "tonight" else w
        elif w in ("today", "tomorrow", "tmrw", "tmr", "tom") or w in WEEKDAY:
            if day:
                return None
            day = w
        else:
            t = legacy_time(w)
            if t is None or tod not in (None, "evening"):
                return None
            tod = t
            clock = w if w[-1] in DIGITS and 1 <= t[0] <= 12 else ""
    h, m = (18, 0) if tod == "evening" else tod or DAY_START
    if clock and dark:                          # "tonight 9:30"
        plus, r = divmod(fit(dark, h * 60 + m, clock[:2] not in PREDAWN), 1440)
        h, m = r // 60, r % 60
    elif clock and h <= 6 and clock[0] != "0":  # "mon 3:30"
        h += 12
    step = 1
    if day is None:
        off = plus = 0
    elif day == "today" and plus:
        return None
    elif day == "today":
        off = step = 0
    elif day in WEEKDAY:
        off, step = (WEEKDAY[day] - now.wd) % 7, 7
    else:
        off = 0 if now.hour < LATE_NIGHT_END else 1
    t = local(now.days + off + plus, h, m)
    if t <= now.t:
        t = local(now.days + off + step + plus, h, m)
    return t if t > now.t else None


# --- pass 2: the rest -------------------------------------------------------

def suffix(n):
    return "th" if 10 < n % 100 < 14 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


class Tok(object):
    """A number ("9", "9:30", "11/30") or a word. glued: no space before it;
    spelled: a number that was a word."""
    __slots__ = ("num", "s", "glued", "spelled")

    def __init__(self, num, s, glued=False, spelled=False):
        self.num, self.s, self.glued, self.spelled = num, s, glued, spelled


def split(text):
    s = text.replace(u"’", "'").replace(",", " ").replace("@", " at ")
    toks = []
    for w in s.split():
        if w in ("-", u"–", u"—"):              # "monday - 9am"
            toks.append(Tok(False, "-"))
            continue
        w = w.rstrip(".!?;:")                   # "dec.", "a.m.", a full stop
        if w.endswith("a.m") or w.endswith("p.m"):
            w = w[:-3] + w[-3] + "m"
        i = 0
        while i < len(w):
            j = i
            if w[i] in DIGITS:
                while j < len(w) and (w[j] in DIGITS or w[j] in ":/.-"):
                    j += 1
                while w[j - 1] not in DIGITS:   # "9." / "9-": not part of it
                    j -= 1
                toks.append(Tok(True, w[i:j], i > 0))
            elif w[i] in LETTERS:
                while j < len(w) and (w[j] in LETTERS or w[j] == "'"):
                    j += 1
                toks.append(Tok(False, w[i:j], i > 0))
            elif w[i] == "-" and 0 < i < len(w) - 1 and w[i + 1] in LETTERS:
                j = i + 1                       # half-past, 30-min; "1h-2h" is a range
            else:
                raise No()
            i = j
    return toks


def phrases(toks):
    """One way of saying it for another, the longest first."""
    out, i = [], 0
    while i < len(toks):
        for n in range(min(PHRASE_WORDS, len(toks) - i), 0, -1):
            got = toks[i:i + n]
            new = None if any(t.num for t in got) else PHRASE.get(" ".join(t.s for t in got))
            # "1 day after tomorrow" counts from tomorrow
            if new and not (out and out[-1].num and got[0].s in UNIT):
                out.extend(Tok(False, w) for w in new.split())
                i += n
                break
        else:
            out.append(toks[i])
            i += 1
    return out


def tokens(text):
    out, tens = [], False
    toks = phrases(split(text))
    for i, t in enumerate(toks):
        n = -1 if t.num else NUMBER.get(t.s, ORDINAL.get(t.s, -1))
        if n < 0:
            out.append(t)
            tens = False
            continue
        if tens and 1 <= n <= 9:                # "forty five" as one
            n += int(out.pop().s)
            near = out[-1:] + toks[i + 1:i + 2]
            if t.s == "second" and not any(x.s in MONTH or x.s in ("the", "of") for x in near):
                raise No()                      # "twenty second": the 22nd, or 20 s?
        elif t.s in ORDINAL and out and out[-1].num:
            raise No()                          # "5 second"
        tens = n in (20, 30, 40, 50) and t.s in NUMBER
        out.append(Tok(True, str(n), False, True))
        if t.s in ORDINAL:
            out.append(Tok(False, suffix(n), True))
    return out


class Reader(object):
    def __init__(self, toks, now):
        self.t, self.now, self.i = toks, now, 0
        self.day = None         # ("today",) ("wd", n) ("date", y|None, m, d) …
        self.week = None        # a key of WEEKS: the week a weekday is in
        self.rel = None         # "month" | "year" (the next), "this month"
        self.time = None        # (kind, h, m, minutes to subtract)
        self.part = None
        self.today_part = False  # "this morning"
        self.lead = None        # "first thing", "early": the morning's time
        self.dur = None         # (seconds, months, whole days?)
        self.anchor = False     # "… from <day>"
        self.later = False
        self.end = False        # "end of the week"
        self.past = False
        self.lonely = False     # a bare hour without "at": it needs a day beside it
        self.range = False      # "10 to 11": a time, or from ten to eleven?
        self.prep = ""          # the filler just before

    # -- token access
    def word(self, k=0):
        j = self.i + k
        return self.t[j].s if 0 <= j < len(self.t) and not self.t[j].num else ""

    def num(self, k=0):
        j = self.i + k
        return self.t[j].s if j < len(self.t) and self.t[j].num else ""

    def int(self, k=0):
        s = self.num(k)
        return int(s) if s and set(s) <= DIGITS else -1

    def glued(self, k=0):
        j = self.i + k
        return j < len(self.t) and self.t[j].glued

    def spelled(self, k=0):
        j = self.i + k
        return j < len(self.t) and self.t[j].spelled

    def ampm(self, k=0):
        """"am" / "pm" at k ("a" / "p" glued to its number too), else ""."""
        w = self.word(k)
        return w if w in AMPM else w + "m" if w in ("a", "p") and self.glued(k) else ""

    # -- setters: anything said twice is a conflict
    def set_day(self, *day):
        if self.day is not None:
            # "fri dec 4": a weekday beside a date, checked when resolved
            a, b = sorted((self.day, day), key=lambda d: d[0])
            if b[0] == "wd" and len(a) == {"date": 4, "dom": 2}.get(a[0]):
                self.day = a + (b[1],)
                return
            raise No()
        self.day = day

    def set_time(self, kind, h, m=0, back=0):
        """kind: "bare" (9), "clock" (9:30), "h24", "ampm", "named" (noon)."""
        if self.time is not None or h > 23 or m > 59:
            raise No()
        self.time = (kind, h, m, back)

    def once(self, name, value):
        if getattr(self, name):
            raise No()
        setattr(self, name, value)

    # -- reading
    def read(self):
        if self.word() == "snooze":
            self.i += 1
        if self.word() == "remind" and self.word(1) == "me":
            self.i += 2
        seen = False
        while self.i < len(self.t):
            w = self.word()
            if w in FILL or w in ("at", "-", "after"):
                if w not in SOFT or not self.prep:      # "in about 20" is still "in"
                    self.prep = w
                self.i += 1
            elif w == "this":
                self.i += 1
                self.this()
                seen = True
            else:
                self.item(w)
                seen = True
        if self.prep in ("after", "-") and seen:
            raise No()
        if not seen:
            raise No("empty")

    def this(self):
        w = self.word()
        if w in PARTS and w not in ROLLING:
            self.today_part = True
        elif w == "week":
            self.i += 1
            self.once("week", "this")

    def item(self, w):
        """One day, time, part or duration, and what the filler before it asks."""
        prep, i0 = self.prep, self.i
        before = (self.day, self.week, self.rel, self.part)
        dur, plain = self.dur, self.int() >= 0
        had = self.time
        if self.num():
            self.number()
        else:
            self.name(w)
        if self.i <= i0:
            raise No()
        self.prep = ""
        if prep == "after" and before != (self.day, self.week, self.rel, self.part):
            raise No()                          # "after monday" is no day
        if prep == "-" and (self.dur is not dur or (plain and self.time is not had and self.time[0] != "ampm")):
            raise No()                          # "dec 3 - 5", "2h - 30m": ranges

    def name(self, w):
        one, two = self.word(1), self.word(2)
        if w in WEEKDAY:
            self.weekday(WEEKDAY[w])
        elif w == "coming" and one in WEEKDAY:
            self.i += 1
            self.weekday(WEEKDAY[one])
        elif w == "today":
            self.i += 1
            self.set_day("today")
        elif w in TOMORROW or w == "overmorrow":
            self.i += 1
            self.set_day("tomorrow", int(w == "overmorrow"))
        elif w == "yesterday" or (w == "last" and (
                one in WEEKDAY or one in ("week", "weekend", "month", "year", "night"))):
            self.i += 1 if w == "yesterday" else 2
            self.past = True
            self.set_day("past")
        elif w == "next":
            self.next(one)
        elif w == "after-next":
            self.i += 1
            self.once("week", "after next")
        elif w == "weekend":
            self.i += 1
            self.set_day("weekend")
        elif w in MONTH:
            self.month_day(MONTH[w])
        elif w in NOON or w in EOD:
            self.i += 1
            self.set_time("named", *NOON.get(w, DAY_END))
        elif w in PARTS:
            self.i += 1
            self.once("part", w)
            if w == "late-night":
                self.today_part = True
        elif w in AMPM and (self.day or self.week):     # "monday pm"
            self.i += 1
            self.once("part", "afternoon" if AMPM[w] else "morning")
        elif w == "first-thing" or (w == "early" and (one in TOMORROW or one in WEEKDAY or one == "morning")):
            self.i += 1
            self.once("lead", DAY_START if w == "first-thing" else EARLY)
        elif w == "later":
            self.i += 1
            self.once("later", True)
        elif w in ("eow", "eom", "eoy"):
            self.i += 1
            self.end_at({"w": "week", "m": "month", "y": "year"}[w[2]])
        elif w == "end" and one == "of":
            self.end_of()
        elif w in ("start", "beginning") and one == "of" and two in MONTH and not self.num(3):
            self.i += 3
            self.set_day("month", MONTH[two], False)
        elif (w == "quarter" and one in TO) or (w == "half" and one == "past"):
            self.i += 1
            self.past_to(30 if w == "half" else 15)
        elif not self.duration():
            raise No()

    def weekday(self, n):
        self.i += 1
        a, b, c = self.word(), self.word(1), self.word(2)
        if a == "next" and b not in ("week", "month", "year", "weekend"):
            self.i += 1                         # "friday next"
            self.once("week", "next")
        elif a == "after" and b == "next":
            self.i += 2
            self.once("week", "after next")
        elif a == "of" and b in ("this", "next") and c == "week":
            self.i += 3
            self.once("week", b)
        self.set_day("wd", n)

    def end_at(self, *what):
        self.end = True
        self.set_day("end", *what)

    def end_of(self):
        self.i += 2
        if self.word() == "the":
            self.i += 1
        nxt = self.word() == "next"
        if nxt or self.word() == "this":
            self.i += 1
        w = self.word()
        if w in ("week", "month", "year"):
            self.i += 1
            self.end_at(w)
            if nxt and w == "week":
                self.once("week", "next")
            elif nxt:
                self.once("rel", w)
        elif nxt:
            raise No()
        elif w in MONTH and not self.num(1):
            self.i += 1
            self.end = True
            self.set_day("month", MONTH[w], True)
        elif w in TOMORROW or w in WEEKDAY:
            self.set_time("named", *DAY_END)
            self.name(w)
        else:
            raise No()

    def next(self, w):
        if w in WEEKDAY:
            self.i += 2
            self.once("week", "next")
            self.set_day("wd", WEEKDAY[w])
        elif w == "week":
            self.i += 2
            self.once("week", "next")
        elif w in ("month", "year"):
            self.i += 2
            self.once("rel", w)
        elif w == "weekend":
            self.i += 2
            self.once("week", "next")
            self.set_day("weekend")
        else:
            self.i += 1                         # "for the next two hours"
            if not self.duration():
                raise No()

    # -- dates
    def ordinal(self, k):
        """Is token k the number of a day with its st/nd/rd/th?"""
        n = self.int(k)
        return 1 <= n <= 31 and self.word(k + 1) == suffix(n)

    def month_day(self, month):
        """At the month: "december (the) 3rd 2027"; "in january", its 1st."""
        k = 2 if self.word(1) == "the" and self.ordinal(2) else 1
        d = self.int(k)
        if self.prep == "in" and not self.num(1) and k == 1:
            self.i += 1
            self.set_day("month", month, False)
            return
        if not 1 <= d <= 31 or len(self.num(k)) > 2 or self.ampm(k + 1):
            raise No()
        self.i += k + 2 if self.ordinal(k) else k + 1
        self.date(None, month, d)

    def date(self, y, m, d):
        if y is None:
            n = self.int()
            if len(self.num()) == 4 and 2000 <= n <= 2199 and not self.ampm(1):
                y = n
                self.i += 1
        if not (1 <= m <= 12 and 1 <= d <= month_len(y if y is not None else 2000, m)):
            raise No()
        self.set_day("date", y, m, d)

    def day_of_month(self, n):
        """At "3rd": alone, "of december", "of next month"."""
        self.i += 2
        of = self.word() == "of"
        if of:
            self.i += 1
        a = self.word()
        if a in MONTH and not (self.ordinal(1) and not of):     # "9th dec 3rd": no
            self.i += 1
            self.date(None, MONTH[a], n)
        elif of and a in ("this", "the", "next") and self.word(1) == "month":
            self.i += 2
            self.once("rel", "month" if a == "next" else "this month")
            self.set_day("dom", n)
        elif of or a in WEEKDAY:                # "the 2nd wednesday": of what?
            raise No()
        else:
            self.set_day("dom", n)

    # -- numbers: a time, a date, or the start of a duration
    def number(self):
        s, n, at = self.num(), self.int(), self.prep == "at"
        w = self.word(1)
        # "20 to 5", "5 minutes past 3" — before the duration reads the 5 min
        if 0 < n < 60:
            k = 2 if w in UNIT and UNIT[w][0] == 60 else 1
            if self.word(k) in TO and self.num(k + 1):
                to = TO[self.word(k)]
                if k == 1 and (n not in (5, 10, 20, 25) or self.prep in ("in", "for")
                               or (to and self.int(2) > 12)):
                    raise No()                  # "9 to 5", "in 10 to 20"
                self.range = k == 1 and n <= 10 and to
                self.i += k
                self.past_to(n)
                return
        if self.duration():
            return
        if self.num(1) in FRACTION:
            raise No()                          # "1 1/2": of what?
        if n < 0:
            self.compound(s)
            return
        ap = self.ampm(1)
        if ap:
            h, m = (n, 0) if len(s) <= 2 else (n // 100, n % 100) if len(s) <= 4 else (0, 0)
            if not 1 <= h <= 12:
                raise No()
            self.i += 2
            self.set_time("ampm", h % 12 + AMPM[ap], m)
        elif w == "o'clock":
            self.i += 2
            self.hour(s, n)
        elif self.ordinal(0):
            self.day_of_month(n)
        elif w in MONTH and 1 <= n <= 31 and not at and self.day_before(s):
            self.i += 2
            self.date(None, MONTH[w], n)
        elif self.spelled() and self.spelled(1) and 1 <= n <= 12 and 10 <= self.int(1) <= 59 \
                and not self.ordinal(1) and self.prep not in ("in", "for"):
            m = self.int(1)                     # "five thirty"
            self.i += 2
            self.hour(s, n, m)
        else:
            self.bare(s, n, at)

    def day_before(self, s):
        """At "3 dec": is the 3 its day? "9 dec 3rd": no; "9 dec 3": who knows."""
        d, after = self.int(2), self.word(3)
        if not 1 <= d <= 31 or len(self.num(2)) > 2 or self.ampm(3) or after == "o'clock":
            return len(s) <= 2
        if not self.ordinal(2):
            raise No()
        return False

    def bare(self, s, n, at):
        """Digits alone: 1430 is a time; an hour, or 930, needs "at" or a day."""
        if not at and (self.spelled() or self.glued() or self.glued(1)):
            raise No()                          # "one monday", "2mon"
        if self.prep in ("in", "for") or self.word(-1) == "the":
            raise No()                          # "in 20 tomorrow", "friday the 4"
        if len(s) == 4 and 2026 <= n <= 2039 and n % 5 and not at:
            raise No()                          # "until 2027": a year
        self.i += 1
        if len(s) == 4:
            self.set_time("h24", n // 100, n % 100)
            return
        self.lonely = not at
        if len(s) == 3 and n >= 100:
            self.set_time("clock", n // 100, n % 100)
        elif len(s) <= 2 and 1 <= n <= 23:
            self.set_time("h24" if n > 12 or s[0] == "0" else "bare", n)
        else:
            raise No()

    def hour(self, s, n, m=0, back=0):
        """A spoken hour (o'clock, half past, five thirty), am/pm after it or not."""
        ap = self.ampm()
        if not 1 <= n <= (12 if ap else 23) or s[0] == "0":
            raise No()
        if ap:
            self.i += 1
            self.set_time("ampm", n % 12 + AMPM[ap], m, back)
        else:
            self.set_time("h24" if n > 12 else "bare", n, m, back)

    def past_to(self, mins):
        """At the "past" / "to" of "quarter to 5 pm"."""
        back = TO[self.word()]
        s, n = self.num(1), self.int(1)
        self.i += 3 if self.word(2) == "o'clock" else 2
        self.hour(s, n, 0 if back else mins, mins if back else 0)

    def compound(self, s):
        """9:30, 9.30am, 11/30, 12/3/26, 2026-12-03."""
        ap = self.ampm(1)
        self.i += 1
        for sep in ":./-":
            if sep in s:
                break
        f = s.split(sep)
        if not all(x and set(x) <= DIGITS for x in f):
            raise No()
        v = [int(x) for x in f]
        sizes = [len(x) for x in f]
        if (sep == ":" or (sep == "." and ap)) and sizes in ([1, 2], [2, 2]):
            if ap:
                self.i += 1
                if not 1 <= v[0] <= 12:
                    raise No()
                self.set_time("ampm", v[0] % 12 + AMPM[ap], v[1])
            else:
                self.set_time("h24" if v[0] > 12 or f[0][0] == "0" else "clock", v[0], v[1])
        elif sep == "/" and max(sizes[:2]) <= 2 and sizes[2:] in ([], [2], [4]):
            y = v[2] + (2000 if sizes[2] == 2 else 0) if v[2:] else None
            self.date(y, v[0], v[1])
        elif sep == "-" and sizes[0] == 4 and len(f) == 3 and max(sizes[1:]) <= 2:
            self.date(*v)
        else:
            raise No()

    # -- durations
    def half(self, k):
        return self.word(k) == "and" and self.word(k + 1) == "a" and self.word(k + 2) == "half"

    def quantity(self):
        """(numerator, denominator) at i, moving past it; None (and i where it
        was) when there is none."""
        i0 = self.i
        if self.word() == "another":
            self.i += 1
            if self.word() in UNIT:
                return 1, 1
        s, w = self.num(), self.word()
        if s:
            if s in FRACTION and self.word(1) in UNIT:
                self.i += 1
                return FRACTION[s]
            a, dot, b = s.partition(".")
            if set(a + b) <= DIGITS and len(b) <= 6:
                if len(a) > 12:
                    raise No("far")
                self.i += 1
                n, d = int(a + b), 10 ** len(b)
                if not dot and self.num() in FRACTION and self.word(1) in UNIT:    # "1 1/2 hours"
                    f = FRACTION[self.num()]
                    self.i += 1
                    return n * f[1] + f[0], f[1]
                if not dot and self.half(0) and self.word(3) in UNIT:
                    self.i += 3
                    return 2 * n + 1, 2
                return n, d
        if w in ("a", "an") and self.word(1) in ("couple", "few", "half"):
            self.i += 1
            w = self.word()
        if w in ("couple", "few"):
            self.i += 2 if self.word(1) == "of" else 1
            return (2, 1) if w == "couple" else (3, 1)
        if w == "half":
            self.i += 2 if self.word(1) in ("a", "an") else 1
            return 1, 2
        if w in ("a", "an") and self.word(1) in UNIT and len(self.word(1)) > 1:
            self.i += 1
            return 1, 1
        if w == "hour" and (self.i == 0 or self.word(-1) in ("in", "next", "for", "about", "around")):
            return 1, 1
        if w in UNIT and len(w) > 1 and (self.word(1) == "from" or self.half(1)):
            return 1, 1                         # "week from monday", "day and a half"
        self.i = i0
        return None

    def segment(self):
        """One "2 hours" / "an hour and a half": (secs, months, whole days?, unit)."""
        i0 = self.i
        q = self.quantity()
        if q and self.word() == "more":
            self.i += 1
        unit = self.word()
        if q is None or unit not in UNIT:
            self.i = i0
            return None
        self.i += 1
        n, d = q
        if self.half(0):
            self.i += 3
            n, d = 2 * n + d, 2 * d
        secs, months = UNIT[unit]
        if n // d > 10 ** 9 or n * months // d > 12 * 200:
            raise No("far")
        if months:
            if n * months % d:
                raise No()                      # half a month is how long?
            return 0, n * months // d, True, UNIT[unit]
        return n * secs // d, 0, n * secs % (d * 86400) == 0 and secs >= 86400, UNIT[unit]

    def duration(self):
        seg = self.segment()
        if seg is None:
            return False
        if self.dur is not None:
            raise No()
        secs, months, whole, unit = seg
        while True:
            i0 = self.i
            joined = self.word() == "and"
            if joined:
                self.i += 1
            loose = not self.num() and not joined       # "an hour a day"
            seg = self.segment()
            if seg is None:
                self.i = i0
                break
            size, last = seg[3][0] + seg[3][1] * 30 * 86400, unit[0] + unit[1] * 30 * 86400
            if loose or size >= last:
                raise No()                      # "30m-1h", "2 days a week"
            secs, months, whole, unit = secs + seg[0], months + seg[1], whole and seg[2], seg[3]
        if unit[0] == 3600:                     # "1h30", "an hour and 15"
            k = int(self.word() == "and")
            n = self.int(k)
            if len(self.num(k)) == 2 and 1 <= n <= 59 and (
                    self.i + k + 1 == len(self.t) or self.word(k + 1) in ("from", "later")):
                secs += n * 60
                self.i += k + 1
        self.dur = (secs, months, whole)
        w = self.word()
        if w == "from" and self.word(1) == "now":
            self.i += 2
        elif w == "later":
            self.i += 1
        elif w == "ago":
            self.i += 1
            self.past = True
        elif w == "from" or w == "after":
            if not whole:
                raise No()
            self.i += 1
            self.anchor = True
        return True

    # -- resolving
    def clock(self):
        """(minutes of the named day, of more than one reading?), or None when
        no time was said."""
        part = self.part
        if self.time is None:
            h, m = self.lead or (PARTS[part][:2] if part else (0, 0))
            return (h * 60 + m, False) if part else None
        kind, h, m, back = self.time
        dayish = self.day or self.week or self.rel
        if self.lonely and not (dayish or part):
            raise No()
        if self.range and (dayish or part or kind == "ampm"):
            raise No()
        vague = kind in ("bare", "clock") or (kind == "h24" and part in AFTER_DARK and 1 <= h <= 12
                                              and "%02d" % h not in PREDAWN)
        r = h * 60 + m - back
        if part:
            return fit(part, r, vague), False
        if vague and h <= 6:
            r += 720
        # 7-11 without am/pm: the next one, when no other day says morning
        either = kind == "bare" and (7 <= h <= 11 or (h == 12 and back)) and not self.dur and (not dayish or self.day == ("today",))
        return r, either

    def named_day(self, ahead):
        """The local day meant. ahead(day): is the time on that day to come?"""
        now, today = self.now, self.now.days
        monday = today - now.wd
        y, mo, _ = to_date(today)
        this_month = y * 12 + mo - 1
        day = self.day or ("",)
        kind = day[0]
        rel, week = self.rel, self.week
        # what may stand beside "next month" / "next week"
        if (rel and kind not in ("", "end", "date", "dom")) or (rel and week) \
                or (week and kind not in ("", "wd", "weekend", "end")) \
                or (kind == "end" and ((rel and rel != day[1]) or (week and (week != "next" or day[1] != "week")))):
            raise No()

        def first(days):
            """The first of them still ahead; today's, once past, is past."""
            for d in days:
                if ahead(d):
                    return d
                if d == today:
                    raise No("passed")
            raise No()

        def month_day(k, d):
            """Day d of the month k (years * 12 + month), if it has one."""
            length = month_len(k // 12, k % 12 + 1)
            return [to_days(k // 12, k % 12 + 1, d or length)] if d <= length else []

        if kind == "":
            if week == "this":
                raise No()
            if week:
                return monday + WEEKS[week]
            return month_day(this_month + 1, 1)[0] if rel == "month" else to_days(y + 1, 1, 1)
        if kind == "today":
            return today
        if kind == "tomorrow":
            d = today + (now.hour >= LATE_NIGHT_END)
            return (d if ahead(d) else d + 1) + day[1]
        if kind == "wd":
            if week:
                return monday + day[1] + WEEKS[week]
            d = today + (day[1] - now.wd) % 7
            return d if ahead(d) else d + 7
        if kind == "weekend":
            if week in ("next", "after next"):
                return monday + 5 + WEEKS[week]
            for d in (monday + 5, monday + 6):
                if d >= today and ahead(d):
                    return d
            return monday + 12
        if kind == "end" and day[1] == "week":
            d = monday + 4 + (7 if week else 0)
            return d if ahead(d) else d + 7
        if kind == "end":
            step = 12 if day[1] == "year" else 1
            k = this_month + (step if rel else 0) + (12 - mo if step == 12 else 0)
            d = month_day(k, 0)[0]
            return d if ahead(d) else month_day(k + step, 0)[0]
        if kind == "month":                     # its first day, or its last
            if not day[2] and day[1] == mo:
                raise No()                      # "in november", in November
            k = y * 12 + day[1] - 1
            last = day[2]
            return first([d for j in (k, k + 12) for d in month_day(j, 0 if last else 1) if d >= today])
        if kind == "dom":
            if rel == "year":
                raise No()
            k = this_month + (rel == "month")
            ks = [k] if rel else range(k, k + 4)
            days = [d for j in ks for d in month_day(j, day[1])]
            return self.agree(days[0] if rel and days else first([d for d in days if d >= today]), day[2:])
        # a date, perhaps with its weekday beside it
        y0, mo, dd = day[1:4]
        if rel:
            if rel != "year" or y0 is not None:
                raise No()
            y0 = y + 1
        if y0 is not None and abs(y0 - y) > 200:
            raise No("far")
        years = [y0] if y0 is not None else range(y, y + 10)    # feb 29: the next leap year
        days = [d for j in years for d in month_day(j * 12 + mo - 1, dd)]
        if y0 is not None and days:
            return self.agree(days[0], day[4:])
        return self.agree(first([d for d in days if d >= today]), day[4:])

    def agree(self, d, wd):
        """The day, unless a weekday said beside it is another one."""
        if wd and weekday(d) != wd[0]:
            raise No()                          # "fri dec 3" on a Thursday
        return d

    def resolve(self):
        now = self.now
        if self.lead:
            if self.part not in (None, "morning"):
                raise No()
            self.part = "morning"
        if self.today_part:
            if self.day is None and not (self.week or self.rel or self.dur):
                self.day = ("today",)
            elif self.day != ("today",):
                raise No()
        if self.anchor and self.part == "tonight" and not self.day:
            self.day = ("today",)               # "a week from tonight"
        elif self.part == "tonight" and (self.week or self.rel or self.dur
                                         or (self.day and self.day != ("today",))):
            raise No()
        if self.later:
            if self.day or self.week or self.rel or self.time or self.part or self.dur:
                raise No()
            return now.t + LATER_SECS
        clock = self.clock()
        dayish = self.day or self.week or self.rel
        secs = months = 0
        if self.dur:
            secs, months, whole = self.dur
            if months > 1200 or secs > MAX_SECS:
                raise No("far")
            if self.past:
                raise No("bad" if self.day or clock else "passed")
            if not secs and not months:
                raise No("zero")
            if clock is None and not self.anchor:
                if dayish:
                    raise No()
                t = now.t
                if months:
                    h, m, s = now.clock
                    t = local(add_months(now.days, months), h, m, s)
                return t + secs
            if not whole or bool(dayish) != self.anchor:
                raise No()
        if self.past:
            raise No("passed")
        if clock is None:
            if not dayish:
                raise No("empty")
            h, m = DAY_END if self.end else DAY_START
            clock = (h * 60 + m, False)
        r, either = clock

        def at(day, r):
            return local(day + r // 1440, r % 1440 // 60, r % 60)
        if self.day == ("today",) and r >= 1440 and now.hour < LATE_NIGHT_END and at(now.days, r - 1440) > now.t:
            r -= 1440                           # at 1:30, "later tonight at 2" is this 2:00
        if dayish:
            day = self.named_day(lambda d: at(d, r) > now.t)
        else:                                   # a time alone: its next occurrence
            day = now.days
            if not self.dur:
                r %= 1440
                if not either and at(day, r) <= now.t:
                    day += 1
        if either and at(day, r) <= now.t:      # 7-11: this evening's, or tomorrow's
            r += 720
            if not dayish and at(day, r) <= now.t:
                r += 720
        if self.dur:
            day = add_months(day, months) + secs // 86400
        t = at(day, r)
        if t <= now.t:
            raise No("passed")
        return t


def when(text, now):
    """The epoch `text` means, or No."""
    text = text.lower()
    if not text.strip():
        raise No("empty")
    if len(text) > MAX_TEXT:
        raise No()
    t = legacy(text, now)
    if t is None:
        r = Reader(tokens(text), now)
        r.read()
        t = r.resolve()
    if t <= now.t:
        raise No("passed")
    if t - now.t > MAX_SECS:
        raise No("far")
    return t


# --- words about it ---------------------------------------------------------

def one_line(text, most=36):
    s = " ".join("".join(c if c.isprintable() else " " for c in text.lower()).split())
    return s if len(s) <= most else s[:most - 1] + u"…"


def why(no, text):
    s = one_line(text)
    if no.kind == "empty" and not s:
        return "when? (%s)" % HINT
    return {"empty": "when? (%s)" % HINT,
            "zero": "'%s' is no time at all" % s,
            "passed": "'%s' has already passed" % s,
            "far": "'%s' is too far off" % s}.get(no.kind, "can't read '%s' (%s)" % (one_line(text, 18), HINT))


def short_why(no, text):
    """Why, without the text itself (a number of its own aside)."""
    s = one_line(text)
    if no.kind == "bad" and s and set(s) <= DIGITS and len(s) < 5:
        return "%s what? say %sm%s" % (s, s, ", %sh or %spm" % (s, s) if 1 <= int(s) <= 12 else " or %sh" % s)
    return {"empty": "", "zero": "no time at all", "passed": "already passed",
            "far": "too far off"}.get(no.kind, "can't read that")


def stamp(t, now, full):
    """"18:00" (today), "Fri 9:00" (this week), "Mon Nov 30 9:00"; `full` is
    always the last. Another year is named."""
    lt = time.localtime(t)
    hm = "%d:%02d" % (lt.tm_hour, lt.tm_min)
    days = to_days(lt.tm_year, lt.tm_mon, lt.tm_mday) - now.days
    if not full and days == 0:
        return hm
    if not full and 0 < days < 7:
        return "%s %s" % (WDAYS[lt.tm_wday], hm)
    y = "" if lt.tm_year == time.localtime(now.t).tm_year else " %d" % lt.tm_year
    return "%s %s %d%s %s" % (WDAYS[lt.tm_wday], MONS[lt.tm_mon - 1], lt.tm_mday, y, hm)


def distance(t, now):
    mins = (t - now.t + 59) // 60
    if mins < 60:
        return "in %dm" % mins
    lt = time.localtime(t)
    n, unit = to_days(lt.tm_year, lt.tm_mon, lt.tm_mday) - now.days, "day"
    if mins < 1440 or n < 1:                    # a 25-hour day
        return "in %dh" % (mins // 60) + (" %dm" % (mins % 60) if mins % 60 else "")
    if n >= 730:
        n, unit = n * 4 // 1461, "year"
    elif n >= 60:
        n, unit = n * 48 // 1461, "month"
    return "in %d %s%s" % (n, unit, "" if n == 1 else "s")


def out(s):
    sys.stdout.buffer.write(s if isinstance(s, bytes) else s.encode("utf-8", "replace"))


def safe_when(text, now):
    try:
        return when(text, now)
    except (OverflowError, ValueError, OSError):
        raise No("far")                         # past what the calendar holds


def preview(t, now):
    return u"%s \u00b7 %s" % (stamp(t, now, True), distance(t, now))


def choose(query, sel, now):
    """What Enter takes: (epoch, the suggestion it completes to or "")."""
    q = " ".join(query.lower().split())
    try:
        if not q:
            raise No("empty")
        return safe_when(query, now), ""
    except No as e:
        low = sel.lower()
        if not sel or not low.startswith(q) or (q and set(q) <= DIGITS):
            raise e
    return safe_when(sel, now), sel


def box(query, sel, now):
    if not query.strip():
        return u"type a time, or pick one"
    try:
        t, full = choose(query, sel, now)
    except No as e:
        return short_why(e, query) or u"\u2026"
    return u"\u2192 %s%s" % (full and full + u" \u00b7 ", preview(t, now))


def enter(query, sel, now):
    # fzf says how many rows it shows: an empty query not showing all has not
    # been searched yet (^U and Enter in one burst), and sel is the old one's.
    shown, total = os.environ.get("FZF_MATCH_COUNT"), os.environ.get("FZF_TOTAL_COUNT")
    if not query.strip() and shown and total and shown != total:
        return "bg-cancel+bell"
    try:
        return "become(echo %d)" % choose(query, sel, now)[0]
    except No as e:
        s = short_why(e, query) or "when?"
        if e.kind in ("bad", "empty") and "say" not in s:
            s += u" \u00b7 " + HINT
        return u"bg-cancel+bell+change-header(%s)" % s


def main(argv):
    verb, args = (argv[1] if len(argv) > 1 else ""), argv[2:]
    if args[:1] == ["--"]:
        args = args[1:]
    text = " ".join(args)
    now = Now()
    if verb == "parse":
        try:
            t = safe_when(text, now)
        except No as e:
            sys.stderr.buffer.write(("snooze: %s\n" % why(e, text)).encode("utf-8", "replace"))
            return 1
        out("%d\n" % t)
    elif verb == "preview":
        try:
            t = safe_when(text, now)
            s = preview(t, now)
        except No as e:
            s = short_why(e, text)
        out(s + "\n" if s else "")
    elif verb in ("box", "enter") and len(args) == 2:
        out((box if verb == "box" else enter)(args[0], args[1], now) + "\n")
    elif verb == "batch":
        for line in sys.stdin.buffer.read().split(b"\n"):
            line = line.rstrip(b"\r")
            text = line.decode("utf-8", "replace")
            try:
                t = safe_when(text, now)
            except No:
                continue
            out(line.replace(b"\t", b" ") + ("\t%d\t%s\n" % (t, stamp(t, now, False))).encode())
    else:
        sys.stderr.write("usage: snooze-when.py parse|preview [--] <text...> | box|enter [--] <query> <suggestion> | batch\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
