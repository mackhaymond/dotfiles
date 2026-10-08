#!/usr/bin/env python3
"""agent-roster.py: prefix e, every agent across every tmux session in one popup.

A READER of the state the hook pipeline already keeps on each window
(@agent_state, @agent_summary, @agent_workflow, @agent_cua, @agent_since; see
docs/agent-tab-indicator.md). It holds no state of its own and runs only while
the popup is open: one `tmux list-windows` and one `agent-jump.sh list` a
second.

    agent-roster.py --client <client_tty>

The client tty has to be passed in: display-popup does not expand formats in
its command, and `display -p` inside a popup resolves to tmux's "best" client,
not necessarily the one that pressed the key. tmux.conf therefore binds this
through run-shell, which does expand them.

Layout: NEEDS YOU on top, in agent-jump.sh's own order, so this list and
prefix g can never disagree. Then one group per session, most recently used
first (agents, scratch and btop-popup are never shown), then the parked tabs
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
HIDDEN = {"agents", "scratch", "btop-popup"}
HOLD = "stash"
WATCHER_STALE = 30          # same grace as ensure_watcher in agent-tab-indicator.sh
REFRESH = 1.0
AGENT_CMD = re.compile(r"^(claude|codex|opencode|\d+\.\d+\.\d+)$")

# THIRD SURFACE of the agent colour language: same hex values as the
# @catppuccin_window_* formats in tmux.conf.tmpl and CuaNotch.swift.
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
        w["label"] = w["stash_label"] or w["summary"] or w["name"]
        w["stash_t"] = int(w["stash_ts"]) if w["stash_ts"].isdigit() else None
        out.append(w)
    return out


def parse_needs(text):
    """agent-jump.sh list → window ids, already in priority order."""
    return [l.split("\t", 1)[0] for l in text.splitlines() if l.strip()]


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
    label | sess | win | parked; only win and parked are selectable."""
    by_id = {w["id"]: w for w in windows}
    items = []
    q = query.lower()
    if q:
        hits = [w for w in windows if w["session"] not in HIDDEN
                and q in ("%s:%d %s" % (w["session"], w["index"], w["label"])).lower()]
        hits.sort(key=lambda w: (w["session"] == HOLD, -w["last_attached"], w["session"], w["index"]))
        return [{"kind": "win", "w": w, "long": True} for w in hits]

    need_rows = [by_id[i] for i in needs if i in by_id]
    if need_rows:
        items.append({"kind": "label", "text": "NEEDS YOU"})
        items.extend({"kind": "win", "w": w, "long": True} for w in need_rows)

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
        items.extend({"kind": "win", "w": w, "long": False} for w in shown)

    parked = sorted((w for w in windows if w["session"] == HOLD), key=lambda w: w["index"])
    if parked:
        items.append({"kind": "parked", "n": len(parked), "attn": sum(1 for w in parked if is_attn(w))})
        # ALL of them, regardless of `a`: every parked tab was put there on
        # purpose, and a suspended agent has no @agent_state left to pass the
        # "is an agent" filter (that filter hid 4 of 5 here, 2026-10-07).
        if parked_open:
            items.extend({"kind": "win", "w": w, "long": False, "parked": True} for w in parked)
    return items


def selectable(items):
    return [i for i, it in enumerate(items) if it["kind"] in ("win", "parked")]


def item_key(it):
    return it["w"]["id"] if it["kind"] == "win" else ("parked" if it["kind"] == "parked" else None)


def parse_keys(buf):
    """Split raw input into key names. A lone ESC is only returned when the
    caller has already waited for a following byte and none came."""
    keys, i = [], 0
    seqs = {"\x1b[A": "up", "\x1b[B": "down", "\x1bOA": "up", "\x1bOB": "down",
            "\x1b[Z": "btab", "\x1b[5~": "pgup", "\x1b[6~": "pgdn"}
    while i < len(buf):
        if buf[i] == "\x1b":
            for s, name in seqs.items():
                if buf.startswith(s, i):
                    keys.append(name); i += len(s); break
            else:
                # Unknown CSI (ESC [ … final) or SS3 (ESC O x, e.g. End/Home
                # in application mode): swallow it whole, or its ESC would read
                # as "close the popup".
                m = re.match(r"\x1b\[[0-9;?]*[A-Za-z~]|\x1bO[A-Za-z]", buf[i:])
                if m:
                    i += m.end()
                else:
                    keys.append("esc"); i += 1
            continue
        c = buf[i]; i += 1
        keys.append({"\t": "tab", "\r": "enter", "\n": "enter", " ": "space",
                     "\x7f": "bs", "\x08": "bs", "\x03": "ctrl-c"}.get(c, c))
    return keys


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


def dwidth(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def clip(s, width):
    if width <= 0:
        return ""
    if dwidth(s) <= width:
        return s
    out, n = "", 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in "WF" else 1
        if n + cw > width - 1:
            break
        out += c; n += cw
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


def client_window(client):
    r = tmux("list-clients", "-F", US.join(["#{client_tty}", "#{window_id}"]))
    if not r or r.returncode:
        return None
    for line in r.stdout.splitlines():
        t, _, win = line.partition(US)
        if t == client:
            return win
    return None


def agent_pane(win):
    """The pane to close: the agent's own if the window is split."""
    r = tmux("list-panes", "-t", win, "-F", US.join(["#{pane_id}", "#{pane_current_command}", "#{pane_active}"]))
    if not r or r.returncode:
        return None
    rows = [l.split(US) for l in r.stdout.splitlines() if l.count(US) == 2]
    for pid, cmd, _ in rows:
        if AGENT_CMD.match(cmd):
            return pid
    for pid, _, active in rows:
        if active == "1":
            return pid
    return rows[0][0] if rows else None


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
        self.confirm = None          # window dict pending close
        self.msg, self.msg_until = "", 0.0
        self.top = 0
        self.gone = False

    # data
    def refresh(self):
        r = tmux("list-windows", "-a", "-F", FMT)
        self.windows = parse_windows(r.stdout) if r and r.returncode == 0 else []
        try:
            n = subprocess.run(["bash", JUMP, "list"], capture_output=True, text=True, timeout=5)
            self.needs = parse_needs(n.stdout)
        except (OSError, subprocess.TimeoutExpired):
            self.needs = []
        self.cur_win = client_window(self.client)
        self.gone = self.cur_win is None
        self.rebuild()

    def rebuild(self):
        self.items = build_items(self.windows, self.needs, self.cur_win, self.show_all,
                                 self.parked_open, self.query)
        sel = selectable(self.items)
        keys = [item_key(self.items[i]) for i in sel]
        if self.sel_key not in keys:
            self.sel_key = self.cur_win if self.cur_win in keys else (keys[0] if keys else None)

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

    # actions; returning True closes the popup
    def act(self, key):
        if self.confirm is not None:
            w, self.confirm = self.confirm, None
            if key in ("y", "Y"):
                pane = agent_pane(w["id"])
                if pane:
                    run_bg("'%s' close '%s'" % (CLOSED, pane))
                    self.say("closed %s · ⌘Z brings it back" % w["label"])
                else:
                    self.say("that window is gone")
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
            else:
                subprocess.run(["bash", JUMP, "goto", self.client, w["id"]], timeout=10)
            return True
        elif key == "g":
            subprocess.run(["bash", JUMP, "next", self.client], timeout=10)
            return True
        elif key == "x" and it and it["kind"] == "win":
            self.confirm = it["w"]
        elif key == "H" and it and it["kind"] == "win":
            if it["w"]["session"] == HOLD:
                self.say("already parked")
            else:
                run_bg("'%s' stash '%s'" % (STASH, it["w"]["id"]))
                self.say("parked %s" % it["w"]["label"])
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
        now = time.time()
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
            foot = fg("yellow") + " close %s:%d %s? " % (self.confirm["session"], self.confirm["index"], self.confirm["label"]) + fg("text") + "y/n"
        elif self.filtering:
            foot = fg("peach") + " / " + fg("text") + self.query + "▏" + fg("overlay") + "   ⏎ keep · esc clear"
        elif self.msg and now < self.msg_until:
            foot = " " + fg("sky") + self.msg
        else:
            q = (fg("peach") + " /" + self.query + fg("overlay") + " · ") if self.query else " "
            foot = q + fg("overlay") + "tab/j/k move · space/⏎ go · g next · x close · H park · / filter · a all · esc"
        lines.append("")
        lines.append(foot)

        frame = "\x1b[H" + "".join(clip_ansi(l, cols) + RESET + "\x1b[K\r\n" for l in lines[:rows - 1])
        frame += clip_ansi(lines[rows - 1] if len(lines) >= rows else "", cols) + RESET + "\x1b[K"
        out.write(frame)
        out.flush()


def clip_ansi(s, width):
    """Truncate a string containing SGR escapes to `width` visible cells."""
    out, n, i = [], 0, 0
    while i < len(s):
        if s[i] == "\x1b":
            m = re.match(r"\x1b\[[0-9;]*m", s[i:])
            if m:
                out.append(m.group()); i += m.end(); continue
        c = s[i]
        cw = 2 if unicodedata.east_asian_width(c) in "WF" else 1
        if n + cw > width:
            break
        out.append(c); n += cw; i += 1
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
                buf = os.read(fd, 256).decode("utf-8", "replace")
                if buf == "\x1b":
                    more, _, _ = select.select([fd], [], [], 0.025)
                    if more:
                        buf += os.read(fd, 256).decode("utf-8", "replace")
                done = False
                for key in parse_keys(buf):
                    if roster.act(key):
                        done = True
                        break
                if done:
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
