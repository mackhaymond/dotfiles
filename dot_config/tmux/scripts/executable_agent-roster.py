#!/usr/bin/env python3
"""agent-roster.py: prefix e, every agent across every tmux session in one popup.

A READER of the state the hook pipeline already keeps on each window
(@agent_state, @agent_summary, @agent_workflow, @agent_cua, @agent_since; see
docs/agent-tab-indicator.md). It holds no state of its own and runs only while
the popup is open: ONE tmux call a second (`list-windows -a \\; list-clients`).

    agent-roster.py --client <client_tty>

The client tty has to be passed in: display-popup does not expand formats in
its command, and `display -p` inside a popup resolves to tmux's "best" client,
not necessarily the one that pressed the key. tmux.conf therefore binds this
through `run-shell -C`, which does expand them.

LAUNCH SPEED (prefix e should paint in well under 100 ms; docs/agent-roster.md
has the per-stage numbers). The binding runs no shell and no tmux client: it
execs a real python3 (Homebrew's, never the /usr/bin xcrun stub or a pyenv
shim) with -I -S. Stays Python 3.9-compatible all the same, because 3.9 is the
fallback. The first frame needs exactly one tmux round trip: NEEDS YOU is
computed here, from the same list-windows rows (needs_order), instead of
forking `agent-jump.sh list` (bash + tmux + awk + sort + awk + cut) each tick.

Layout: NEEDS YOU on top, in agent-jump.sh's own order, so this list and
prefix g can never disagree: needs_order mirrors its `list` pipeline, and
tests/test_agent_roster.py pins the two against each other by running the real
script on the same fixtures. Moves still go through agent-jump.sh. Then one
group per session, most recently used
first (agents, tasks, scratch and btop-popup are never shown), then the parked tabs
collapsed onto one line. Windows with no agent are hidden until `a`, except the
one this popup is covering: a prompt that lands under the popup is discharged as
"seen" by the watcher, so that row has to stay visible.

Drawn with raw ANSI truecolour rather than curses. Inside tmux, curses only
gets the 256-colour palette, and these hues have to match the tab bar and
CuaNotch exactly (the palette note in tmux.conf.tmpl names this file as its
third surface). The pulse reads the watcher's @agent_blink, so it beats in
step with the tabs, and it freezes when they do.

Every move goes through agent-jump.sh (`goto`/`next`): select-window THEN
switch-client, so the visit discharges the tint the same way a tab click does.
"""
import codecs
import os
import re
import select
import signal
import subprocess
import sys
import termios
import time
import tty
import unicodedata

HOME = os.path.expanduser("~")
SCRIPTS = os.path.join(HOME, ".config/tmux/scripts")
JUMP = os.path.join(SCRIPTS, "agent-jump.sh")
STASH = os.path.join(SCRIPTS, "stash.sh")
CLOSED = os.path.join(SCRIPTS, "closed-tabs.sh")
WATCHER = os.path.join(SCRIPTS, "agent-tab-watcher.sh")
PIDFILE = os.path.join(os.environ.get("TMPDIR") or "/tmp", "agent-tab-watcher.%d.pid" % os.getuid())

US = "\x1f"
# Same set agent-jump.sh's EXCLUDE (minus the stash, which gets its own
# collapsed group here) and the session pickers skip. `tasks` is CuaNotch's
# broker session: never a place the user goes.
HIDDEN = {"agents", "tasks", "scratch", "btop-popup"}
HOLD = "stash"
WATCHER_STALE = 30          # same grace as ensure_watcher in agent-tab-indicator.sh
REFRESH = 1.0
ESC_WAIT = 0.025            # how long a trailing ESC / partial sequence waits for the rest
# is_agent_comm in agent-tab-watcher.sh: ps comm basename claude or codex, or
# Claude's version-named binary ("2.1.291"). Digits-only segments, anchored.
AGENT_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

# THIRD SURFACE of the agent colour language: same hex values as the
# @catppuccin_window_* formats in tmux.conf.tmpl and cua-notch Sources/Constants.swift;
# cua-notch dev/check-invariants section 65 fails if they drift.
HEX = {
    "red": "#f38ba8", "yellow": "#f9e2af", "green": "#a6e3a1", "pink": "#f5c2e7",
    "blue": "#89b4fa", "teal": "#94e2d5", "dimteal": "#659a91", "dimblue": "#5d7aaa",
    "peach": "#fab387", "text": "#cdd6f4", "sub": "#a6adc8", "overlay": "#6c7086",
    "surface0": "#313244", "surface1": "#45475a", "crust": "#11111b", "sky": "#89dceb",
}
GEAR = "\U000F0493"   # nf-md-cog: background workflow / subagent
MOUSE = "\U000F037D"  # nf-md-mouse: driving an app through cua-driver

FIELDS = ["session", "index", "id", "name", "state", "summary", "workflow", "cua",
          "since", "last_attached", "blink", "stash_label", "stash_session", "stash_ts"]
FMT = US.join(["#{session_name}", "#{window_index}", "#{window_id}", "#{window_name}",
               "#{@agent_state}", "#{@agent_summary}", "#{@agent_workflow}", "#{@agent_cua}",
               "#{@agent_since}", "#{session_last_attached}", "#{@agent_blink}",
               "#{@stash_label}", "#{@stash_session}", "#{@stash_ts}"])
# Rides in the same tmux call as FMT, after a `;`. Four fields where a window
# row has fourteen, so parse_windows skips it and parse_clients takes only it.
CLIENT_TAG = "client"
CLIENT_FMT = US.join(["", CLIENT_TAG, "#{client_tty}", "#{window_id}"])

# agent-jump.sh's EXCLUDE, byte for byte (a test compares it with the
# script's), matched the way its awk does: index(ex, " " session " ").
JUMP_EXCLUDE = " agents tasks stash scratch btop-popup "
NEED_TIER = {"failed": 0, "needs-input": 1, "done": 2}
NO_STAMP = 9999999999       # agent-jump.sh: a window with no stamp sorts last in its tier
DIGITS = re.compile(r"[0-9]+\Z")
AWK_BLANKS = re.compile(r"[ \t\n]+")


def fg(name):
    h = HEX[name]
    return "\x1b[38;2;%d;%d;%dm" % (int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16))


def bg(name):
    h = HEX[name]
    return "\x1b[48;2;%d;%d;%dm" % (int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16))


RESET = "\x1b[0m"
BOLD = "\x1b[1m"


# ── pure model (unit-tested) ────────────────────────────────────────────────

def parse_windows(text):
    out = []
    for line in text.splitlines():
        parts = line.split(US)
        if len(parts) != len(FIELDS):
            continue
        w = dict(zip(FIELDS, parts))
        w["index"] = int(w["index"]) if w["index"].isdigit() else 0
        w["last_attached"] = int(w["last_attached"]) if w["last_attached"].isdigit() else 0
        s = w["since"].split(" ", 1)[0]
        w["since_t"] = int(s) if s.isdigit() else None
        # stash.sh's own label order. A parked agent is SIGTERMed (suspended)
        # and the watcher then GCs its @agent_* options, so @stash_label is
        # often the only title a parked tab still has.
        # Only for windows actually IN the holding session: stash.sh clears
        # @stash_label on unstash, but a window that left by any other route
        # would otherwise wear a frozen label over its live summary.
        parked = w["session"] == HOLD
        w["label"] = (parked and w["stash_label"]) or w["summary"] or w["name"]
        w["stash_t"] = int(w["stash_ts"]) if w["stash_ts"].isdigit() else None
        out.append(w)
    return out


def parse_clients(text):
    """The CLIENT_FMT rows of the same call → {client_tty: window_id}."""
    out = {}
    for line in text.splitlines():
        parts = line.split(US)
        if len(parts) == 4 and parts[0] == "" and parts[1] == CLIENT_TAG:
            out[parts[2]] = parts[3]
    return out


_XFRM = None


def collation(name=""):
    """The collation agent-jump.sh's `sort` uses: the libc collation of the
    environment's locale (LC_ALL > LC_COLLATE > LANG; "" = from the env), as
    strxfrm. prefix g runs under en_US.UTF-8, where `_x` sorts before `a`
    and `a` before `B`; a plain str sort would not. See needs_order for what
    happens when it calls two strings equal."""
    global _XFRM
    import locale                       # first NEEDS YOU sort only: ~1 ms
    try:
        locale.setlocale(locale.LC_COLLATE, name)
        _XFRM = locale.strxfrm
    except locale.Error:                # unknown locale: sort falls back to C too
        locale.setlocale(locale.LC_COLLATE, "C")
        _XFRM = str
    return _XFRM


def needs_order(windows):
    """agent-jump.sh `list`, in-process → window ids in priority order.

    The same pipeline over the same list-windows rows: drop EXCLUDE sessions;
    tier failed 0 > needs-input 1 > done-without-a-fleet 2 (anything else is
    not in the queue); sort; a linked window (one row per session) keeps its
    first sorted row. Pinned to the real script by tests/test_agent_roster.py's
    NeedsOrderTests.

    The sort is /usr/bin/sort's (2.3-Apple, FreeBSD's) exactly, for
    `-k1,1n -k2,2n -k3,3 -k4,4` with no -s, over the awk's full line:
      - tier and stamp numerically;
      - each text key by wcscoll, and when that says equal, the SHORTER key
        (in characters) first. Under en_US.UTF-8, emoji, Greek, Cyrillic and
        CJK carry no collation weight, so "Ω" vs "日本" or "dev 🚀" vs
        "dev 🔥" collate equal and length decides (not code points: "Ж本"
        sorts before "ωΩ🚀Ж");
      - all keys equal (e.g. "Ω":1 vs "ω":1): the WHOLE line by the same rule
        (wcscoll, then length), so the window id after the keys decides.
    Fuzzed against the real sort, 2400 random cases, under both locales."""
    xfrm = _XFRM or collation()
    keyed = []
    for w in windows:
        s = w["session"]
        if (" %s " % s) in JUMP_EXCLUDE:
            continue
        tier = NEED_TIER.get(w["state"])
        if tier is None or (tier == 2 and w["workflow"]):
            continue
        word = AWK_BLANKS.split(w["since"].strip(" \t\n"))[0]
        stamp = word if DIGITS.match(word) else str(NO_STAMP)   # awk prints the digits as given
        ix = "%09d" % w["index"]
        label = (w["summary"] or w["name"]).replace("\t", " ")
        line = "\t".join([str(tier), stamp, s, ix, w["id"], s, str(w["index"]), w["state"], stamp, label])
        key = (tier, int(stamp), xfrm(s), len(s), xfrm(ix), len(ix), xfrm(line), len(line))
        keyed.append((key, w["id"]))
    keyed.sort(key=lambda kv: kv[0])
    return list(dict.fromkeys(wid for _, wid in keyed))


def is_attn(w):
    return w["state"] in ("failed", "needs-input") or (w["state"] == "done" and not w["workflow"])


def in_flight(w):
    return w["state"] == "running" or bool(w["workflow"]) or bool(w["cua"])


RANK = {"failed": 0, "needs-input": 1, "done": 2}


def rank(w):
    if is_attn(w):
        return RANK[w["state"]]
    if in_flight(w):
        return 3
    return 4 if w["state"] else 9


def build_items(windows, needs, cur_win, show_all=False, parked_open=False, query=""):
    """The list as rows. Each item is a dict with kind in
    label | sess | win | parked; only win and parked are selectable.

    Selectable rows carry a POSITIONAL `key`, unique within the list. The same
    window can appear twice (a NEEDS YOU row and its session-group row, or a
    window linked into two sessions), so the window id alone cannot be the
    selection: move() would always find the first copy and loop between
    them. Actions still use it["w"]["id"]."""
    by_id = {w["id"]: w for w in windows}
    items = []
    q = query.lower()
    if q:
        hits = [w for w in windows if w["session"] not in HIDDEN
                and q in ("%s:%d %s" % (w["session"], w["index"], w["label"])).lower()]
        hits.sort(key=lambda w: (w["session"] == HOLD, -w["last_attached"], w["session"], w["index"]))
        return [{"kind": "win", "w": w, "long": True, "key": ("hit", w["session"], w["id"])} for w in hits]

    need_rows = [by_id[i] for i in dict.fromkeys(needs) if i in by_id]
    if need_rows:
        items.append({"kind": "label", "text": "NEEDS YOU"})
        items.extend({"kind": "win", "w": w, "long": True, "key": ("need", w["id"])} for w in need_rows)

    sessions = {}
    for w in windows:
        if w["session"] in HIDDEN or w["session"] == HOLD:
            continue
        sessions.setdefault(w["session"], []).append(w)
    cur_sess = by_id[cur_win]["session"] if cur_win in by_id else None
    order = sorted(sessions, key=lambda s: (s != cur_sess, -max(w["last_attached"] for w in sessions[s]), s))
    for s in order:
        ws = sorted(sessions[s], key=lambda w: w["index"])
        shown = [w for w in ws if show_all or w["state"] or w["id"] == cur_win]
        if not shown:
            continue
        best = min(ws, key=rank)
        items.append({"kind": "sess", "name": s, "best": best if rank(best) < 9 else None})
        items.extend({"kind": "win", "w": w, "long": False, "key": ("sess", s, w["id"])} for w in shown)

    parked = sorted((w for w in windows if w["session"] == HOLD), key=lambda w: w["index"])
    if parked:
        items.append({"kind": "parked", "n": len(parked), "attn": sum(1 for w in parked if is_attn(w)),
                      "key": "parked-hdr"})
        # ALL of them, regardless of `a`: every parked tab was put there on
        # purpose, and a suspended agent has no @agent_state left to pass the
        # "is an agent" filter (that filter hid 4 of 5 here, 2026-10-07).
        if parked_open:
            items.extend({"kind": "win", "w": w, "long": False, "parked": True, "key": ("parked", w["id"])}
                         for w in parked)
    return items


def selectable(items):
    return [i for i, it in enumerate(items) if it["kind"] in ("win", "parked")]


def item_key(it):
    """The row's selection key (None for labels and session headers)."""
    return it.get("key") if it["kind"] in ("win", "parked") else None


def key_window(key):
    """The window id a row key points at (the last element), else None."""
    return key[-1] if isinstance(key, tuple) else None


KEYSEQ = {"\x1b[A": "up", "\x1b[B": "down", "\x1bOA": "up", "\x1bOB": "down",
          "\x1b[Z": "btab", "\x1b[5~": "pgup", "\x1b[6~": "pgdn"}
KEYCHR = {"\t": "tab", "\r": "enter", "\n": "enter", " ": "space",
          "\x7f": "bs", "\x08": "bs", "\x03": "ctrl-c"}
CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")       # ECMA-48: params, intermediates, final
CSI_PART = re.compile(r"\x1b\[[0-?]*[ -/]*\Z")     # a CSI cut off by the end of the read
SS3 = re.compile(r"\x1bO[ -~]")


def parse_keys(buf, final=False):
    """Split decoded input into key names → (keys, leftover).

    A read can end mid-sequence (key repeat, a paste, a slow redraw): the
    trailing "\\x1b", "\\x1b[" or "\\x1b[1;" comes back as `leftover` for the
    caller to prepend to the next read, never as esc. `final=True` means the
    caller already waited ESC_WAIT and nothing followed: a lone ESC is then
    the esc key, and a still-incomplete sequence is dropped.

    Known CSI/SS3 sequences map to names; unknown ones (End/Home, Ctrl-arrows,
    paste brackets) are swallowed whole, or their ESC would read as "close
    the popup". Alt+key (ESC + one char) is ignored for the same reason."""
    keys, i, n = [], 0, len(buf)
    while i < n:
        c = buf[i]
        if c != "\x1b":
            keys.append(KEYCHR.get(c, c)); i += 1
            continue
        m = CSI.match(buf, i) or SS3.match(buf, i)
        if m:
            name = KEYSEQ.get(m.group())
            if name:
                keys.append(name)
            i = m.end()
            continue
        if i + 1 == n:                                   # ESC is the last byte
            if final:
                keys.append("esc"); i += 1
                continue
            return keys, buf[i:]
        nxt = buf[i + 1]
        if (nxt == "[" and CSI_PART.match(buf, i)) or (nxt == "O" and i + 2 == n):
            return keys, ("" if final else buf[i:])     # the rest is still on its way
        if nxt == "\x1b":                                # ESC ESC: the first one was a press
            keys.append("esc"); i += 1
            continue
        i += 2                                           # Alt+key: ignored
    return keys, ""


class KeyReader:
    """Raw bytes → key names, across reads. Holds the UTF-8 decoder state (a
    multi-byte char split between reads must not become U+FFFD) and the carry
    of an incomplete escape sequence. No I/O: main() does the waiting."""

    def __init__(self):
        self.dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.carry = ""

    def feed(self, data):
        keys, self.carry = parse_keys(self.carry + self.dec.decode(data))
        return keys

    def flush(self):
        """Nothing followed within ESC_WAIT: resolve the carry."""
        keys, self.carry = parse_keys(self.carry, final=True)
        return keys

    @property
    def pending(self):
        return bool(self.carry)


def ago(t, now):
    if t is None:
        return ""
    s = max(0, int(now - t))
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm" % (s // 60)
    if s < 86400:
        return "%dh" % (s // 3600)
    return "%dd" % (s // 86400)


# Pictographic blocks tmux draws two cells wide. east_asian_width already says
# W for most of them, but this python (3.9, Unicode 13) reports every emoji
# added since as N, so the blocks are listed outright. Over-counting a cell
# only costs padding; under-counting overflows the row.
EMOJI_WIDE = ((0x1F300, 0x1F64F), (0x1F680, 0x1F6FF), (0x1F900, 0x1F9FF), (0x1FA70, 0x1FAFF))
VS16 = "️"


def char_width(c):
    o = ord(c)
    if o == 0x200D or 0xFE00 <= o <= 0xFE0F or unicodedata.combining(c) \
            or unicodedata.category(c) in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(c) in "WF" or any(a <= o <= b for a, b in EMOJI_WIDE):
        return 2
    return 1


def clusters(s):
    """[(text, cells)]: zero-width marks ride on the char before them, and a
    VS16 (emoji presentation, as in "❤️") makes that char two cells."""
    out = []
    for c in s:
        w = char_width(c)
        if w == 0 and out:
            t, cw = out[-1]
            out[-1] = (t + c, 2 if c == VS16 else cw)
        else:
            out.append((c, w))
    return out


def dwidth(s):
    return sum(w for _, w in clusters(s))


def clip(s, width):
    if width <= 0:
        return ""
    cl = clusters(s)
    if sum(w for _, w in cl) <= width:
        return s
    out, n = "", 0
    for t, cw in cl:
        if n + cw > width - 1:
            break
        out += t; n += cw
    return out + "…"


# ── tmux I/O ────────────────────────────────────────────────────────────────

def tmux(*args):
    try:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None


def run_bg(cmd):
    """Hand a command to the tmux server, so it outlives this popup."""
    tmux("run-shell", "-b", cmd)


def snapshot():
    """Every window and every client in ONE tmux round trip → stdout or ""."""
    r = tmux("list-windows", "-a", "-F", FMT, ";", "list-clients", "-F", CLIENT_FMT)
    return r.stdout if r and r.returncode == 0 else ""


def is_agent_comm(comm):
    base = comm.rsplit("/", 1)[-1]
    return base in ("claude", "codex") or bool(AGENT_VERSION.match(base))


PANE_GONE = "that window is gone"
PANE_UNSURE = "can't tell which pane is the agent · close it from the tab"


def pick_agent_pane(panes_text, ps_text):
    """The pane `x` closes → (pane_id, None) or (None, why).

    panes_text: `list-panes -F pane_id US pane_tty US pane_active`.
    ps_text: `ps -ax -o tty=,comm=` (None when not needed or it failed).

    Matched by TTY, like the watcher, never by pane_current_command: codex
    launched through npm shows up there as `node`, and falling back to the
    active pane then closed the user's shell instead of the agent. A split
    window where no pane (or more than one, none of them active) runs an
    agent is refused rather than guessed at."""
    rows = [l.split(US) for l in panes_text.splitlines() if l.count(US) == 2]
    if not rows:
        return None, PANE_GONE
    if len(rows) == 1:
        return rows[0][0], None
    agent_ttys = set()
    for l in (ps_text or "").splitlines():
        parts = l.strip().split(None, 1)
        if len(parts) == 2 and parts[0] != "??" and is_agent_comm(parts[1].strip()):
            agent_ttys.add(parts[0])
    hits = [(pid, active) for pid, t, active in rows
            if t and (t[5:] if t.startswith("/dev/") else t) in agent_ttys]
    if len(hits) == 1:
        return hits[0][0], None
    active = [pid for pid, a in hits if a == "1"]
    if len(active) == 1:
        return active[0], None
    return None, PANE_UNSURE


def agent_pane(win):
    """pick_agent_pane against the live server. Runs only on `x`, so its ps
    costs nothing per tick, and only for a split window."""
    r = tmux("list-panes", "-t", win, "-F", US.join(["#{pane_id}", "#{pane_tty}", "#{pane_active}"]))
    if not r or r.returncode:
        return None, PANE_GONE
    ps_text = None
    if len([l for l in r.stdout.splitlines() if l.count(US) == 2]) > 1:
        try:
            ps_text = subprocess.run(["ps", "-ax", "-o", "tty=,comm="], capture_output=True,
                                     text=True, errors="replace", timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            ps_text = None
    return pick_agent_pane(r.stdout, ps_text)


def watcher_age():
    try:
        return time.time() - os.stat(PIDFILE).st_mtime
    except OSError:
        return None


# ── UI ─────────────────────────────────────────────────────────────────────

class Roster:
    def __init__(self, client):
        self.client = client
        self.windows, self.needs, self.cur_win = [], [], None
        self.items, self.sel_key = [], None
        self.show_all = self.parked_open = False
        self.query, self.filtering = "", False
        self.confirm = None          # {"action": close|park, "w": window} awaiting y/n
        self.msg, self.msg_until = "", 0.0
        self.top = 0
        self.gone = False

    # data
    def refresh(self):
        self.load(snapshot())

    def load(self, text):
        """One snapshot() → the model. Split from refresh() for the tests."""
        self.windows = parse_windows(text)
        self.needs = needs_order(self.windows)
        self.cur_win = parse_clients(text).get(self.client)
        self.gone = self.cur_win is None
        self.rebuild()

    def rebuild(self):
        self.items = build_items(self.windows, self.needs, self.cur_win, self.show_all,
                                 self.parked_open, self.query)
        sel = selectable(self.items)
        keys = [item_key(self.items[i]) for i in sel]
        if self.sel_key in keys:
            return
        # The row went away (filter typed/cleared, NEEDS YOU discharged, tab
        # parked): another row for the same window, else the current window's
        # session-group row, else the first row.
        def row_for(wid):
            if wid is None:
                return None
            rows = [k for k in keys if key_window(k) == wid]
            return next((k for k in rows if k[0] == "sess"), rows[0] if rows else None)
        self.sel_key = (row_for(key_window(self.sel_key)) or row_for(self.cur_win)
                        or (keys[0] if keys else None))

    def selected(self):
        for it in self.items:
            if item_key(it) == self.sel_key and it["kind"] in ("win", "parked"):
                return it
        return None

    def move(self, d):
        keys = [item_key(self.items[i]) for i in selectable(self.items)]
        if not keys:
            return
        i = keys.index(self.sel_key) if self.sel_key in keys else 0
        self.sel_key = keys[max(0, min(len(keys) - 1, i + d))]

    def say(self, text):
        self.msg, self.msg_until = text, time.time() + 3

    def jump(self, *args):
        """agent-jump.sh goto|next. True (close the popup) once it ran; a hang
        or a missing bash keeps the popup open with the reason in the footer."""
        try:
            subprocess.run(["bash", JUMP, args[0], self.client] + list(args[1:]), timeout=10)
        except subprocess.TimeoutExpired:
            self.say("agent-jump.sh %s timed out" % args[0])
            return False
        except OSError as e:
            self.say("agent-jump.sh %s failed: %s" % (args[0], e.strerror or e))
            return False
        return True

    def handle(self, keys):
        """One read's worth of keys → True to close the popup. A key that
        opens a y/n drops the rest of its batch, so the answer has to come in
        a later read: a paste or a fast "Hy" / "xy" must not confirm itself
        against the window the popup opened on."""
        for key in keys:
            asking = self.confirm is None
            if self.act(key):
                return True
            if asking and self.confirm is not None:
                return False
        return False

    # actions; returning True closes the popup
    def act(self, key):
        if self.confirm is not None:
            c, self.confirm = self.confirm, None
            if key not in ("y", "Y"):
                return False
            # The dict in c is from when x/H was pressed; the refresh tick may
            # have moved the window since. Act on the current one, and never
            # silently switch action (a tab parked in between must go through
            # kill-many, not closed-tabs).
            w = next((x for x in self.windows if x["id"] == c["w"]["id"]), None)
            if w is None:
                self.say(PANE_GONE)
                return False
            if w["session"] != c["w"]["session"]:
                self.say("that tab moved · press %s again" % ("H" if c["action"] == "park" else "x"))
                return False
            if c["action"] == "park":
                # SIGTERMs (suspends) the agent: behind y/n because the
                # default selection is the window you are sitting on.
                run_bg("'%s' stash '%s'" % (STASH, w["id"]))
                # A request, not a fact: stash.sh refuses a busy agent, and
                # says so on the status line, after this popup has moved on.
                self.say("parking %s…" % w["label"])
            elif w["session"] == HOLD:
                # A parked tab goes through stash.sh's own discard, which logs
                # a suspended conversation's id (with the command that resumes
                # it) and drops its sidecar row; closed-tabs knows neither.
                run_bg("'%s' kill-many '%s'" % (STASH, w["id"]))
                self.say("discarded %s · its session id is in the stash log" % w["label"])
            else:
                pane, why = agent_pane(w["id"])
                if pane:
                    run_bg("'%s' close '%s'" % (CLOSED, pane))
                    self.say("closed %s · ⌘Z brings it back" % w["label"])
                else:
                    self.say(why)
            return False

        if self.filtering:
            if key == "esc":
                self.filtering, self.query = False, ""
            elif key == "enter":
                self.filtering = False
            elif key == "bs":
                self.query = self.query[:-1]
            elif len(key) == 1 and key.isprintable():
                self.query += key
            elif key in ("up", "btab"):
                self.move(-1)
            elif key in ("down", "tab"):
                self.move(1)
            self.rebuild()
            return False

        it = self.selected()
        if key in ("esc", "q", "ctrl-c"):
            if self.query:
                self.query = ""; self.rebuild(); return False
            return True
        if key in ("down", "tab", "j"):
            self.move(1)
        elif key in ("up", "btab", "k"):
            self.move(-1)
        elif key == "pgdn":
            self.move(10)
        elif key == "pgup":
            self.move(-10)
        elif key in ("enter", "space"):
            if it is None:
                return False
            if it["kind"] == "parked":
                self.parked_open = not self.parked_open; self.rebuild(); return False
            w = it["w"]
            if w["session"] == HOLD:
                run_bg("'%s' unstash '%s' '%s'" % (STASH, w["id"], self.client))
                return True
            return self.jump("goto", w["id"])
        elif key == "g":
            return self.jump("next")
        elif key == "x" and it and it["kind"] == "win":
            self.confirm = {"action": "close", "w": it["w"]}
        elif key == "H" and it and it["kind"] == "win":
            if it["w"]["session"] == HOLD:
                self.say("already parked")
            else:
                self.confirm = {"action": "park", "w": it["w"]}
        elif key == "a":
            self.show_all = not self.show_all; self.rebuild()
        elif key == "/":
            self.filtering = True
        elif key == "r":
            run_bg("bash '%s'" % WATCHER)
            self.say("watcher restarted")
        return False

    # drawing
    def dot(self, w, blink):
        if is_attn(w):
            return fg({"failed": "red", "needs-input": "yellow", "done": "green"}[w["state"]]) + "●"
        if in_flight(w):
            if w["id"] == self.cur_win:
                return fg("blue") + "●"        # the window you are on never pulses
            return fg("pink" if blink else "blue") + "●"
        if w["state"]:
            return fg("overlay") + "·"
        return " "

    def glyph(self, w, blink):
        if w["workflow"]:
            return fg("teal" if blink else "dimteal") + GEAR + " "
        if w["cua"]:
            return fg("overlay" if is_attn(w) else ("blue" if blink else "dimblue")) + MOUSE + " "
        return ""

    def row(self, it, width, blink, now, selected):
        base = bg("surface0") if selected else ""
        if it["kind"] == "label":
            return " " + fg("overlay") + it["text"]
        if it["kind"] == "sess":
            b = it["best"]
            mark = (self.dot(b, blink) + " ") if b and rank(b) < 4 else "  "
            return " " + fg("sub") + BOLD + it["name"] + RESET + " " + mark
        if it["kind"] == "parked":
            extra = (fg("yellow") + "  %d needs you" % it["attn"]) if it["attn"] else ""
            arrow = "▾" if self.parked_open else "▸"
            return base + " " + fg("sub") + "%s parked (%d)" % (arrow, it["n"]) + extra
        w = it["w"]
        cur = w["id"] == self.cur_win
        bar = (fg("peach") + "▌") if cur else " "
        ix = ("%s:%d" % (w["session"], w["index"])) if it["long"] else ("  %d" % w["index"])
        state = {"failed": "failed", "needs-input": "waiting on you", "done": "done",
                 "running": "working", "idle": "idle"}.get(w["state"], "")
        if w["state"] == "done" and w["workflow"]:
            state = "done · fleet out"
        when = w["since_t"]
        if w["session"] == HOLD:
            if not w["state"]:
                state = "suspended" if w["stash_session"] else "parked"
            when = w["stash_t"] or when      # how long it has been parked
        right = "%-16s %4s " % (state, ago(when, now)) if state else ""
        g = self.glyph(w, blink)
        gw = 2 if g else 0
        title_w = width - 4 - dwidth(ix) - 2 - gw - len(right)
        title = clip(w["label"], max(4, title_w))
        pad = " " * max(0, title_w - dwidth(title))
        tcol = fg("text") if w["state"] or cur or w["session"] == HOLD else fg("overlay")
        return (base + bar + self.dot(w, blink) + base + " " + fg("overlay") + ix + "  " + tcol + title + pad
                + g + base + fg("overlay") + right)

    def draw(self, out):
        try:
            cols, rows = os.get_terminal_size(sys.stdout.fileno())
        except OSError:
            cols, rows = 100, 30
        lines = self.render(cols, rows, time.time())
        out.write("\x1b[H" + "\x1b[K\r\n".join(lines) + "\x1b[K")
        out.flush()

    def render(self, cols, rows, now):
        """The frame as exactly `rows` lines, each at most cols-1 cells. The
        last column stays empty: a line that fills it leaves the cursor in the
        pending-wrap state, where the \\x1b[K after it erases that last cell,
        and anything wider than the screen autowraps and shifts the frame."""
        rows = max(1, rows)
        blink = bool(self.windows) and self.windows[0]["blink"] == "1"   # a global option: same on every row
        ws = [w for w in self.windows if w["session"] not in HIDDEN]
        n_work = sum(1 for w in ws if w["state"] and in_flight(w) and not is_attn(w))
        n_need = len(self.needs)
        n_idle = sum(1 for w in ws if w["state"] == "idle" and not in_flight(w))
        lines = []
        counts = "%s%d working%s · %s%d need you%s · %d idle" % (
            fg("pink"), n_work, fg("overlay"), fg("yellow") if n_need else "", n_need, fg("overlay"), n_idle)
        lines.append(" " + fg("peach") + BOLD + "AGENTS" + RESET + "   " + fg("overlay") + counts)
        age = watcher_age()
        if age is None or age > WATCHER_STALE:
            why = "not running" if age is None else "stalled %ds" % age
            lines.append(" " + bg("red") + fg("crust") + BOLD + " watcher %s: tints and pulses may be stale · r restarts it " % why + RESET)
        elif self.gone:
            lines.append(" " + bg("red") + fg("crust") + BOLD + " this popup's client is gone · esc closes " + RESET)
        else:
            lines.append("")

        body_h = max(1, rows - len(lines) - 2)
        sel_pos = next((i for i, it in enumerate(self.items) if item_key(it) == self.sel_key
                        and it["kind"] in ("win", "parked")), 0)
        if sel_pos < self.top:
            self.top = sel_pos
        elif sel_pos >= self.top + body_h:
            self.top = sel_pos - body_h + 1
        self.top = max(0, min(self.top, max(0, len(self.items) - body_h)))
        view = self.items[self.top:self.top + body_h]
        for it in view:
            lines.append(self.row(it, cols, blink, now, item_key(it) == self.sel_key and it["kind"] in ("win", "parked")))
        if not self.items:
            lines.append(" " + fg("overlay") + ("no matches" if self.query else "no agents running"))
        while len(lines) < rows - 2:
            lines.append("")

        if self.confirm is not None:
            cw = self.confirm["w"]
            verb = "park" if self.confirm["action"] == "park" else ("discard" if cw["session"] == HOLD else "close")
            foot = fg("yellow") + " %s %s:%d %s? " % (verb, cw["session"], cw["index"], cw["label"]) + fg("text") + "y/n"
        elif self.filtering:
            foot = fg("peach") + " / " + fg("text") + self.query + "▏" + fg("overlay") + "   ⏎ keep · esc clear"
        elif self.msg and now < self.msg_until:
            foot = " " + fg("sky") + self.msg
        else:
            q = (fg("peach") + " /" + self.query + fg("overlay") + " · ") if self.query else " "
            foot = q + fg("overlay") + "tab/j/k move · space/⏎ go · g next · x close · H park · / filter · a all · esc"
        lines.append("")
        lines.append(foot)

        lines = (lines + [""] * rows)[:rows]
        return [clip_ansi(l, cols - 1) + RESET for l in lines]


SGR = re.compile(r"\x1b\[[0-9;]*m")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clip_ansi(s, width):
    """Truncate a string containing SGR escapes to `width` visible cells,
    measured the way dwidth measures (emoji, VS16). Any other control char (a
    stray ESC or tab in a window name) is drawn as a space, never sent raw."""
    out, n, pos = [], 0, 0
    width = max(0, width)
    for m in list(SGR.finditer(s)) + [None]:
        text = s[pos:m.start()] if m else s[pos:]
        for t, cw in clusters(CONTROL.sub(" ", text)):
            if n + cw > width:
                return "".join(out)
            out.append(t); n += cw
        if m:
            out.append(m.group()); pos = m.end()
    return "".join(out)


def main(argv):
    client = None
    if "--client" in argv:
        i = argv.index("--client")
        client = argv[i + 1] if i + 1 < len(argv) else None
    if not client:
        print("usage: agent-roster.py --client <client_tty>", file=sys.stderr)
        return 2
    roster = Roster(client)
    reader = KeyReader()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    rpipe, wpipe = os.pipe()
    os.set_blocking(wpipe, False)
    signal.set_wakeup_fd(wpipe)
    signal.signal(signal.SIGWINCH, lambda *_: None)
    out = sys.stdout
    try:
        tty.setraw(fd)
        out.write("\x1b[?1049h\x1b[?25l\x1b[2J")
        roster.refresh()
        next_refresh = time.time() + REFRESH
        roster.draw(out)
        while True:
            timeout = max(0.0, next_refresh - time.time())
            ready, _, _ = select.select([fd, rpipe], [], [], timeout)
            if rpipe in ready:
                os.read(rpipe, 64)               # SIGWINCH: just redraw
            if fd in ready:
                data = os.read(fd, 256)
                if not data:                     # the pty went away
                    return 0
                keys = reader.feed(data)
                # A read that ended mid-sequence (or on a bare ESC) waits
                # ESC_WAIT for the rest; only silence makes a lone ESC "esc".
                while reader.pending:
                    more, _, _ = select.select([fd], [], [], ESC_WAIT)
                    data = os.read(fd, 256) if more else b""
                    if not data:
                        keys += reader.flush()
                        break
                    keys += reader.feed(data)
                if roster.handle(keys):
                    return 0
            if time.time() >= next_refresh:
                roster.refresh()
                next_refresh = time.time() + REFRESH
            roster.draw(out)
    finally:
        out.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
