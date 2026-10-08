#!/usr/bin/env python3
"""agent-roster.py: prefix q, every agent across every tmux session in one popup.

A READER of the state the hook pipeline already keeps on each window
(@agent_state, @agent_summary, @agent_workflow, @agent_cua, @agent_since,
@agent_kind, @agent_detail_kind, @agent_detail; see
docs/agent-tab-indicator.md; the last three may be unset, and everything
degrades to the old rows without them). It holds no state of its own and
runs only while the popup is open: ONE tmux call a second (`list-windows -a
\\; list-clients`). `p` peeks at the selected agent's pane (one capture-pane,
on demand; any key closes it).

    agent-roster.py --client <client_tty>
    agent-roster.py --strip [--tmux-pane <id>] [--wezterm <path>]   (see Strip)

NUMBER KEYS: every window row carries a hotkey in its index column, unique
across the list (a window shown twice gets two). Pressing it goes there, the
same path as Enter, and closes the popup: 1-9 always act on the first key;
rows 10.. are 0 + a fixed-width number (01.., never a bare 0), so no label is
a prefix of another and nothing ever waits on a timeout (hotkey_labels). A
number is resolved against the frame on screen at its first digit, never a
list rebuilt since, and one that matches nothing swallows further digits
until another key (Roster.digit). Inside the / filter, digits type.

The client tty has to be passed in: display-popup does not expand formats in
its command, and `display -p` inside a popup resolves to tmux's "best" client,
not necessarily the one that pressed the key. tmux.conf therefore binds this
through `run-shell -C`, which does expand them.

LAUNCH SPEED (prefix q should paint in well under 100 ms; docs/agent-roster.md
has the per-stage numbers). The binding runs no shell and no tmux client: it
execs a real python3 (Homebrew's, never the /usr/bin xcrun stub or a pyenv
shim) with -I -S. Stays Python 3.9-compatible all the same, because 3.9 is the
fallback. The first frame needs exactly one tmux round trip: NEEDS YOU is
computed here, from the same list-windows rows (needs_order), instead of
forking `agent-jump.sh list` (bash + tmux + awk + sort + awk + cut) each tick.

Layout: NEEDS YOU on top, in agent-jump.sh's own order, so this list and
prefix d can never disagree: needs_order mirrors its `list` pipeline, and
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
import functools
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
          "since", "last_attached", "blink", "stash_label", "stash_session", "stash_ts",
          "active", "panes", "path", "kind", "detail_kind", "detail"]
# The last six: window_active and pane_current_path (the window's active pane)
# give the strip each session's current window and its git branch;
# window_panes says whether a peek must look for the agent pane; @agent_kind
# (claude|codex), @agent_detail_kind (perm|ask|fail|done|run) and @agent_detail
# (one sanitized line, <= 80 chars) are set by agent-tab-indicator.sh and may
# be empty on any window.
FMT = US.join(["#{session_name}", "#{window_index}", "#{window_id}", "#{window_name}",
               "#{@agent_state}", "#{@agent_summary}", "#{@agent_workflow}", "#{@agent_cua}",
               "#{@agent_since}", "#{session_last_attached}", "#{@agent_blink}",
               "#{@stash_label}", "#{@stash_session}", "#{@stash_ts}",
               "#{window_active}", "#{window_panes}", "#{pane_current_path}",
               "#{@agent_kind}", "#{@agent_detail_kind}", "#{@agent_detail}"])
# Rides in the same tmux call as FMT, after a `;`. Four fields where a window
# row has twenty, so parse_windows skips it and parse_clients takes only it.
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
        w["panes"] = int(w["panes"]) if w["panes"].isdigit() else 1
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
    strxfrm. prefix d runs under en_US.UTF-8, where `_x` sorts before `a`
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


# The strip's state SHAPES (herdr's idea: shape and colour both carry the
# state). Colours are never chosen here: Roster.shape() takes dot()'s colour
# and swaps the glyph, so the palette check (cua-notch section 65) still reads
# the one mapping. CAT_HUE is only for the static count tokens; a test pins
# it to dot()'s colours.
CATS = ("failed", "needs-input", "done", "working", "idle")
CAT_GLYPH = {"failed": "✕", "needs-input": "◉", "done": "✓", "working": "◐", "idle": "○"}
CAT_HUE = {"failed": "red", "needs-input": "yellow", "done": "green", "working": "pink", "idle": "overlay"}
KIND_GLYPH = {"claude": "✳", "codex": "⬢"}
DETAIL_WORD = {"perm": "perm", "ask": "asks", "fail": "fail", "done": "done", "run": "run"}
STATE_WORDS = {"failed": "failed", "needs-input": "waiting on you", "done": "done"}
STRIP_GEAR, STRIP_MOUSE = "⚙", "◎"


def cat(w):
    """The count bucket a window falls in (CATS), or None for a plain shell."""
    if is_attn(w):
        return w["state"]
    if in_flight(w):
        return "working"
    return "idle" if w["state"] else None


def counts(ws):
    """[(cat, n)] in CATS order, zero counts dropped. Each window id once."""
    seen, n = set(), {}
    for w in ws:
        c = cat(w)
        if c and w["id"] not in seen:
            seen.add(w["id"])
            n[c] = n.get(c, 0) + 1
    return [(c, n[c]) for c in CATS if n.get(c)]


def count_tokens(cs):
    """counts() → (ansi, cells): `✕1 ◉2 ◐4`, coloured per state."""
    parts = ["%s%s%d" % (fg(CAT_HUE[c]), CAT_GLYPH[c], k) for c, k in cs]
    return " ".join(parts), sum(1 + len(str(k)) for _, k in cs) + max(0, len(cs) - 1)


# Where a stat can hang for a network timeout (SMB/AFP/NFS mounts, autofs):
# no branch is read under these at all. A local disk under /Volumes loses its
# branch too; that is the price of never asking a dead share.
REMOTE_PREFIXES = ("/Volumes/", "/Network/", "/net/", "/home/", "/System/Volumes/Data/home/")


def maybe_remote(path):
    """True for a path at or under REMOTE_PREFIXES (`/Volumes` itself too)."""
    return (path.rstrip("/") + "/").startswith(REMOTE_PREFIXES)


def read_small(path):
    """The first line of a REGULAR file, read without blocking, else None.
    A FIFO named HEAD would block open() forever; O_NONBLOCK plus the S_ISREG
    check (on the open fd, so a swap in between cannot slip past) refuse it."""
    import stat
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, 4096)
    finally:
        os.close(fd)
    return data.decode("utf-8", "replace").split("\n", 1)[0].strip()


def git_head(cwd):
    """The branch checked out at `cwd` (or the short sha when detached), or
    "". Pure file reads, no git fork: walk up to the first `.git`; a
    directory is the git dir, a FILE (worktree, submodule) says
    `gitdir: <path>`, relative to the directory holding it. Never under
    REMOTE_PREFIXES; HEAD and the .git file only if regular files (read_small).
    Can still be slow on a sick local disk: Strip.branch runs it off the UI
    thread."""
    if not cwd or maybe_remote(cwd):
        return ""
    d = cwd
    for _ in range(64):
        if not d or not os.path.isabs(d):
            return ""
        dot = os.path.join(d, ".git")
        try:
            if os.path.isdir(dot):
                gitdir = dot
            elif os.path.isfile(dot):
                first = read_small(dot) or ""
                if not first.startswith("gitdir:"):
                    return ""
                gitdir = first[len("gitdir:"):].strip()
                if not os.path.isabs(gitdir):
                    gitdir = os.path.normpath(os.path.join(d, gitdir))
                if maybe_remote(gitdir):
                    return ""
            else:
                up = os.path.dirname(d)
                if up == d:
                    return ""
                d = up
                continue
            head = read_small(os.path.join(gitdir, "HEAD"))
            if head is None:
                return ""
        except (OSError, ValueError):
            # ValueError: a NUL in a corrupt/hostile `.git` gitdir line makes
            # os.open raise "embedded null byte"; uncaught, the branch thread's
            # traceback would print into the strip's raw-mode pane.
            return ""
        if head.startswith("ref:"):
            ref = head[4:].strip()
            return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        return head[:7]
    return ""


def fit_label(label, width):
    """`proj/Title` into `width` cells → (proj, title), title clipped.
    The title wins: when both do not fit, the project (with its `/`) stays
    only if it fits WHOLE in a third of the width; a half project
    (`math_ec…`) says nothing the session box does not. No slash: all title."""
    label = CONTROL.sub(" ", label)
    proj, title = "", label
    if "/" in label and not label.startswith("/"):
        proj, title = label.split("/", 1)
        proj += "/"
    pw = dwidth(proj)
    if pw + dwidth(title) <= width:
        return proj, title
    if pw > width // 3:
        proj, pw = "", 0
    return proj, clip(title, width - pw)


def roster_popup_argv(client, prefix=None):
    """prefix q's display-popup, for the strip's ☰: the same /bin/dash line
    as tmux.conf.tmpl's `bind-key q` (a test compares the two), with the
    template's homebrew prefix resolved here."""
    if prefix is None:
        prefix = next((p for p in ("/opt/homebrew", "/usr/local") if os.access(p + "/bin/python3", os.X_OK)),
                      "/opt/homebrew")
    py = prefix + "/bin/python3"
    script = ('[ -x %s ] && exec %s -I -S "$0" --client "$1"; exec /usr/bin/python3 -S "$0" --client "$1"'
              % (py, py))
    return ["tmux", "display-popup", "-c", client, "-E", "-w", "75%", "-h", "75%", "-T", " agents ",
            "/bin/dash", "-c", script, os.path.join(SCRIPTS, "agent-roster.py"), client]


def build_items(windows, needs, cur_win, show_all=False, parked_open=False, query="", stable=False):
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
    if stable:
        # The strip: a click moves the client, and "current session first"
        # would then reshuffle the list under the mouse. By name instead.
        order = sorted(sessions)
    else:
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


def hotkey_labels(n):
    """The digit labels for n numbered rows, in display order.

    Rows 1-9 are always the single keys 1-9, however long the list: the top
    of the list (NEEDS YOU, then the current session) is what gets pressed,
    and it must never wait on a timeout. Rows 10.. are `0` plus a FIXED-width
    number ("01".."09", or "001".."0NN" past 18 rows), so the label set is
    prefix-free: every key sequence is complete the moment its last digit
    lands, with no 350 ms wait and no Enter. The label shown on the row is
    exactly what to type.

    A bare `0` is NEVER a complete label (no "tenth row is 0" shortcut): when
    the list shrinks under a half-read number (11 → 10 rows), a stale `01`
    must stay inside the 0-namespace, not act on `0` and let the `1` fall
    through into the agent pane the jump just focused (a permission menu,
    where 1 = Yes)."""
    if n <= 9:
        return [str(i) for i in range(1, n + 1)]
    w = len(str(n - 9))
    return [str(i) for i in range(1, 10)] + ["0" + str(k).zfill(w) for k in range(1, n - 8)]


def number_items(items):
    """{item position: label} for every WINDOW row (NEEDS YOU, session-group,
    expanded parked rows, filter hits), in display order. A window listed
    twice gets two numbers; the parked header is not a window, no number."""
    pos = [i for i, it in enumerate(items) if it["kind"] == "win"]
    return dict(zip(pos, hotkey_labels(len(pos))))


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
# SGR mouse report (DECSET 1006): ESC [ < button ; col ; row M (press) / m (release).
MOUSE_SGR = re.compile(r"\x1b\[<([0-9]+);([0-9]+);([0-9]+)([Mm])\Z")


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
            mouse = None if name else MOUSE_SGR.match(m.group())
            if name:
                keys.append(name)
            elif mouse:                                  # "mouse:<button>:<col>:<row>:<M|m>"
                keys.append("mouse:%s:%s:%s:%s" % mouse.groups())
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


@functools.lru_cache(maxsize=4096)    # box drawing and state glyphs, every line of every frame
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
    if s.isascii():                     # every ASCII char is one cell (char_width), and most text is ASCII
        return [(c, 1) for c in s]
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
    return len(s) if s.isascii() else sum(w for _, w in clusters(s))


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


PEEK_LINES = 15


def peek_lines(w):
    """`p`: the last PEEK_LINES non-blank-tailed lines of the agent's pane →
    [str], or None when tmux could not capture it. One capture-pane against
    the window (its active pane); only a split window first asks agent_pane
    which pane runs the agent (and falls back to the active one if it
    cannot tell)."""
    target = w["id"]
    if w.get("panes", 1) > 1:
        pane, _ = agent_pane(w["id"])
        target = pane or target
    r = tmux("capture-pane", "-p", "-J", "-S", "-%d" % PEEK_LINES, "-t", target)
    if not r or r.returncode:
        return None
    lines = [CONTROL.sub(" ", l).rstrip() for l in r.stdout.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return lines[-PEEK_LINES:]


def pick_tmux_pane(panes, own, clients, hint=None):
    """The strip's tmux client → (client_tty, wezterm pane_id) or (None, None).

    panes: `wezterm cli list --format json`, parsed. own: the strip's own
    pane id ($WEZTERM_PANE). clients: parse_clients() of the snapshot. The
    tmux client is a pane in the strip's OWN tab, not the strip, whose tty is
    an attached tmux client; with several, the pane CMD+B was pressed in
    (`hint`), then the active one, then the first."""
    own = str(own) if own is not None else None
    me = next((p for p in panes or () if str(p.get("pane_id")) == own), None)
    if me is None:
        return None, None
    cands = [p for p in panes if p.get("tab_id") == me.get("tab_id") and str(p.get("pane_id")) != own
             and p.get("tty_name") in clients]
    if not cands:
        return None, None
    best = (next((p for p in cands if str(p.get("pane_id")) == str(hint)), None)
            or next((p for p in cands if p.get("is_active")), None) or cands[0])
    return best["tty_name"], str(best["pane_id"])


def wezterm_cli(exe, *args):
    """`wezterm cli ...` → CompletedProcess or None. Only on a click, a stray
    key, or while the strip has no client: never on the refresh tick."""
    try:
        return subprocess.run([exe, "cli", *args], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None


def wezterm_panes(exe):
    r = wezterm_cli(exe, "list", "--format", "json")
    if not r or r.returncode:
        return None
    import json                          # strip only, and only when (re)resolving
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def watcher_age():
    try:
        return time.time() - os.stat(PIDFILE).st_mtime
    except OSError:
        return None


# ── UI ─────────────────────────────────────────────────────────────────────

class Roster:
    STABLE_ORDER = False             # sessions: current first, then most recently used

    def __init__(self, client):
        self.client = client
        self.windows, self.needs, self.cur_win = [], [], None
        self.items, self.sel_key = [], None
        self.show_all = self.parked_open = False
        self.query, self.filtering = "", False
        self.confirm = None          # {"action": close|park, "w": window} awaiting y/n
        self.peek = None             # {"w": window, "lines": [...]}: `p`, closed by any key
        self.msg, self.msg_until = "", 0.0
        self.top = 0
        self.gone = False
        self.labels = {}             # item position → hotkey label (number_items)
        self.drawn = {}              # label → item, as on screen in the LAST frame drawn
        self.digits, self.digit_map = "", {}   # a 0-prefixed number being typed, and its frame
        self.digits_dead = False     # a number matched nothing: swallow digits until another key
        self.last_frame = None       # what draw() last wrote: identical frames are not rewritten

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
                                 self.parked_open, self.query, stable=self.STABLE_ORDER)
        self.labels = number_items(self.items)
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

    def go(self, it):
        """Go to a window row → True once the move ran (Enter, a number, a
        click). `it` may come from a frame drawn before the latest refresh, so
        the window is re-checked first: gone, or no longer in the session the
        row showed (parked or unparked meanwhile) → nothing runs, the footer
        says why. A parked row comes back through stash.sh unstash.

        The row's session is passed on: a window linked into two sessions
        would otherwise land in the client's current one (agent-jump.sh
        goto prefers it), not the one the row was listed under."""
        w = it["w"]
        now = [x for x in self.windows if x["id"] == w["id"]]
        if not now:
            self.say(PANE_GONE)
            return False
        if not any(x["session"] == w["session"] for x in now):
            self.say("that tab moved · pick it again")
            return False
        if w["session"] == HOLD:
            run_bg("'%s' unstash '%s' '%s'" % (STASH, w["id"], self.client))
            return True
        return self.jump("goto", w["id"], w["session"])

    def digit(self, key):
        """A digit outside the filter → True to close the popup.

        Resolved against self.drawn, the labels of the frame on screen when
        the first digit was pressed, never against a list rebuilt since: a
        number always means the row the user read it on. Labels are
        prefix-free (hotkey_labels), so a complete label acts at once and an
        incomplete one ("0" with ten rows or more) waits for the next digit,
        with no timeout; esc drops it, backspace takes one digit back.

        A sequence that matches nothing (a stale "005" typed after the list
        shrank to "01".."09") swallows every further digit until a non-digit
        key: its tail must never restart as a fresh single-digit jump ("5")
        to some other window."""
        if self.digits_dead:
            return False
        if not self.digits:
            self.digit_map = dict(self.drawn)
        self.digits += key
        it = self.digit_map.get(self.digits)
        if it is not None:
            self.digits = ""
            return self.go(it)
        if any(l.startswith(self.digits) for l in self.digit_map):
            return False
        self.say("no row %s on screen · digits ignored until another key" % self.digits)
        self.digits, self.digits_dead = "", True
        return False

    def handle(self, keys, raw=None):
        """One read's worth of keys → True to close the popup. A key that
        opens a y/n drops the rest of its batch, so the answer has to come in
        a later read: a paste or a fast "Hy" / "xy" must not confirm itself
        against the window the popup opened on. Opening a peek drops the rest
        the same way, so the key that would close it has to be a new one."""
        for key in keys:
            asking = self.confirm is None and self.peek is None
            if self.act(key):
                return True
            if asking and (self.confirm is not None or self.peek is not None):
                return False
        return False

    def open_peek(self, w):
        lines = peek_lines(w)
        if lines is None:
            self.say("can't capture %s:%d" % (w["session"], w["index"]))
        else:
            self.peek = {"w": w, "lines": lines}

    # actions; returning True closes the popup
    def act(self, key):
        if self.peek is not None:        # any key closes the peek, and only that
            self.peek = None
            return False
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

        if len(key) == 1 and "0" <= key <= "9":
            return self.digit(key)
        if self.digits_dead:             # any non-digit ends the swallowing; esc/⏎/⌫ only that
            self.digits_dead = False
            if key in ("esc", "enter", "bs"):
                return False
        if self.digits:                  # any other key ends a half-typed number
            if key == "bs":
                self.digits = self.digits[:-1]
                return False
            self.digits = ""
            if key in ("esc", "enter"):  # esc/⏎ only drop it
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
            return self.go(it)
        elif key == "d":
            return self.jump("next")
        elif key == "x" and it and it["kind"] == "win":
            self.confirm = {"action": "close", "w": it["w"]}
        elif key == "H" and it and it["kind"] == "win":
            if it["w"]["session"] == HOLD:
                self.say("already parked")
            else:
                self.confirm = {"action": "park", "w": it["w"]}
        elif key == "p" and it and it["kind"] == "win":
            self.open_peek(it["w"])
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

    def shape(self, w, blink):
        """The strip's state mark: dot()'s colour (the checked mapping, pulse
        included) on a shape per state, ✕ ◉ ✓ ◐ ○ (CAT_GLYPH)."""
        c = cat(w)
        return self.dot(w, blink)[:-1] + CAT_GLYPH[c] if c else " "

    def strip_glyph(self, w, blink):
        """glyph()'s colour and pulse, with the strip's text glyphs ⚙ / ◎."""
        return self.glyph(w, blink).replace(GEAR, STRIP_GEAR).replace(MOUSE, STRIP_MOUSE)

    def row(self, it, width, blink, now, selected, label=None, label_w=0):
        """One line. `label` is the row's hotkey (number_items), drawn in the
        index column, right-aligned to label_w; NEEDS YOU and filter rows add
        a dim session:index after it, since no session header names theirs."""
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
        where = "%s:%d" % (w["session"], w["index"])
        if label is not None:
            num = label.rjust(label_w)
            ix = (num + " " + where) if it["long"] else num
            ixs = fg("sub") + BOLD + num + RESET + base + fg("overlay") + ix[len(num):]
        else:
            ix = where if it["long"] else ("  %d" % w["index"])
            ixs = fg("overlay") + ix
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
        # NEEDS YOU rows: what it is asking / why it stopped, dim, when it fits.
        detail = ""
        if it["long"] and is_attn(w) and w.get("detail") and w.get("detail_kind") in DETAIL_WORD:
            room = title_w - dwidth(title) - 2
            if room >= 8:
                detail = "  " + clip(CONTROL.sub(" ", "%s  %s" % (DETAIL_WORD[w["detail_kind"]], w["detail"])), room)
        pad = " " * max(0, title_w - dwidth(title) - dwidth(detail))
        tcol = fg("text") if w["state"] or cur or w["session"] == HOLD else fg("overlay")
        return (base + bar + self.dot(w, blink) + base + " " + ixs + "  " + tcol + title + fg("overlay") + detail
                + pad + g + base + fg("overlay") + right)

    DEFAULT_SIZE = (100, 30)

    def draw(self, out):
        """Render and write the frame, unless it is byte-identical to the last
        one written: an idle roster (the strip above all, open all day) then
        costs the terminal no repaint at all. main() clears last_frame on
        SIGWINCH, when the terminal may have reflowed what is on screen."""
        try:
            cols, rows = os.get_terminal_size(sys.stdout.fileno())
        except OSError:
            cols, rows = self.DEFAULT_SIZE
        lines = self.render(cols, rows, time.time())
        frame = "\x1b[H" + "\x1b[K\r\n".join(lines) + "\x1b[K"
        if frame == self.last_frame:
            return False
        out.write(frame)
        out.flush()
        self.last_frame = frame
        return True

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
        view = [] if self.peek else self.items[self.top:self.top + body_h]
        label_w = max([len(l) for l in self.labels.values()] or [0])
        if not self.peek:
            self.drawn = {}           # under a peek the list (and its numbers) stays as last drawn
        else:
            lines.extend(self.peek_panel(cols - 1, body_h))
        for pos, it in enumerate(view, self.top):
            label = self.labels.get(pos)
            if label is not None:
                self.drawn[label] = it
            lines.append(self.row(it, cols, blink, now, item_key(it) == self.sel_key and it["kind"] in ("win", "parked"),
                                  label, label_w))
        if not self.items and not self.peek:
            lines.append(" " + fg("overlay") + ("no matches" if self.query else "no agents running"))
        while len(lines) < rows - 2:
            lines.append("")

        if self.peek is not None:
            foot = " " + fg("overlay") + "any key closes the peek"
        elif self.confirm is not None:
            cw = self.confirm["w"]
            verb = "park" if self.confirm["action"] == "park" else ("discard" if cw["session"] == HOLD else "close")
            foot = fg("yellow") + " %s %s:%d %s? " % (verb, cw["session"], cw["index"], cw["label"]) + fg("text") + "y/n"
        elif self.filtering:
            foot = fg("peach") + " / " + fg("text") + self.query + "▏" + fg("overlay") + "   ⏎ keep · esc clear"
        elif self.digits:
            foot = fg("peach") + " " + self.digits + "▏" + fg("overlay") + "  next digit · esc cancel"
        elif self.msg and now < self.msg_until:
            foot = " " + fg("sky") + self.msg
        elif self.digits_dead:
            # Outlives the 3 s message on purpose: until another key ends it,
            # every digit is swallowed, and a silent swallow reads as broken.
            foot = fg("yellow") + " digits ignored until another key" + fg("overlay") + "  (esc clears)"
        else:
            q = (fg("peach") + " /" + self.query + fg("overlay") + " · ") if self.query else " "
            foot = q + fg("overlay") + ("1-9/0 go · tab/j/k move · space/⏎ go · p peek · d next · x close"
                                        " · H park · / filter · a all · esc")
        lines.append("")
        lines.append(foot)

        lines = (lines + [""] * rows)[:rows]
        return [clip_ansi(l, cols - 1) + RESET for l in lines]

    def peek_panel(self, width, height):
        """The peek as a rounded box of at most `height` lines and `width`
        cells: the agent's last lines, newest at the bottom."""
        w, got = self.peek["w"], self.peek["lines"]
        if height < 3 or width < 8:
            return []
        inner = width - 4
        title = " peek · %s:%d " % (w["session"], w["index"])
        title += clip(CONTROL.sub(" ", w["label"]), max(0, inner - dwidth(title) - 2)) + " "
        top = fg("overlay") + "╭─" + fg("sub") + title + fg("overlay") + "─" * max(0, width - 3 - dwidth(title)) + "╮"
        body = got[-(height - 2):] or ["(empty)"]
        out = [top]
        for l in body:
            t = clip(l, inner)
            out.append(fg("overlay") + "│ " + fg("text") + t + " " * (inner - dwidth(t)) + fg("overlay") + " │")
        out.append(fg("overlay") + "╰" + "─" * (width - 2) + "╯")
        return out


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


STRIP_VAR = "agent_strip"            # the user var wezterm.lua's CMD+B looks for
STRIP_TITLE = "agent-strip"
RESOLVE_EVERY = 5.0                   # while there is no tmux client: one `wezterm cli list` per 5 s
MOUSE_RAW = re.compile(rb"\x1b\[<[0-9;]*[Mm]")
MOUSE_TAIL = re.compile(rb"\x1b\[<?[0-9;]*\Z")    # a report (or CSI) cut off at the end of a read
MOUSE_HEAD = re.compile(rb"\A<?[0-9;]*[Mm]")       # ...and the rest of it, at the start of the next
BRANCH_TTL = 30.0                     # a cwd's .git/HEAD is re-read at most this often
BRANCH_SLOW_TTL = 600.0               # a cwd whose read outlived BRANCH_WAIT: left alone this long
BRANCH_WAIT = 0.05                    # the most one frame waits on a branch read (then it finishes alone)
ANY = 1 << 30                         # a click target that runs to the end of its line
UNBOLD = "\x1b[22m"
UNBG = "\x1b[49m"

# THE DENSITY LADDER (docs/agent-roster.md, "Strip layout"). The strip never
# scrolls: each frame takes the first step whose plan fits the pane height.
# Steps only ever REMOVE lines, so a taller pane never shows less.
#   rich     full layout + a dim second line under working agents that
#            report what they are doing (@agent_detail_kind run)
#   full     NEEDS YOU entries 2 lines (title / detail), one rounded box per
#            session, 1-line agent rows
#   joined   the boxes share their borders: one box, sessions split by ├─┤
#   needs1   NEEDS YOU entries 1 line each
#   fold     a session's idle agents (2 or more, not the current window)
#            fold into one `○○○ 3 idle` row
#   collapseK  the last K sessions (bottom up) shrink to their header line
#   nobar    the toolbar line goes (status/messages move onto line 1)
#   cap      NEEDS YOU keeps as many entries as fit (sessions keep at least
#            3 lines), the rest is one `… N more` row; then sessions past
#            what fits are one `… N more` divider. Nothing is dropped
#            silently, and every overflow row opens the popup.
LADDER = ("rich", "full", "joined", "needs1", "fold", "collapse", "nobar", "cap")


class Strip(Roster):
    """--strip: the always-visible, click-only agent list in a narrow WezTerm
    split left of the tmux pane (CMD+B in wezterm.lua toggles it).

    Same model and the same one tmux call a second as the popup, laid out for
    ~34-40 columns and ANY height without ever scrolling (LADDER): AGENTS and
    a count bar, a ⏵ next / ☰ menu toolbar, NEEDS YOU (in agent-jump.sh's
    order), then one box per session in name order (agents inside sorted
    attention > working > idle, then index), then `parked` at the bottom.

    No keyboard: a left click acts on what was drawn at that cell (targets):
    an agent row or NEEDS YOU entry goes there (agent-jump.sh goto), a
    session header goes to that session's current window, ⏵ next is
    agent-jump.sh next, ☰ menu / parked / any `… more` opens the prefix q
    popup on the strip's tmux client. A click makes WezTerm focus this pane,
    and every CMD shortcut in wezterm.lua is a SendKey to the ACTIVE pane, so
    each click hands focus straight back to the tmux pane (`wezterm cli
    activate-pane`) before acting; a key that lands here anyway is forwarded
    to the tmux pane (`wezterm cli send-text --no-paste`), so nothing is lost.

    The tmux client is found, not passed: `wezterm cli list` → the other
    pane in this tab → its tty → the tmux client with that client_tty. Done
    at start and again (at most every RESOLVE_EVERY s) while the client is
    missing; `--client` pins it instead (tests)."""
    STABLE_ORDER = True              # sessions by name: a click must not reshuffle the list

    def __init__(self, client=None, wezterm="wezterm", hint=None, own=None):
        Roster.__init__(self, client)
        self.fixed = client is not None
        self.wezterm, self.hint, self.own = wezterm, hint, own
        self.tmux_pane = hint if self.fixed else None
        self.clients = {}
        self.next_resolve = 0.0
        self.targets = []             # screen line (0-based) → [(x0, x1, action)], as last drawn
        self.level = ""               # the LADDER step the last frame used
        self.branches = {}            # cwd → (read at, branch, ttl)
        self.branch_reads = {}        # cwd → (thread, result box): reads that outlived BRANCH_WAIT
        self.children = []            # ☰ display-popup clients, reaped on the refresh tick
        self.cut_mouse = False        # the last read ended inside a mouse report

    def refresh(self):
        self.children = [p for p in self.children if p.poll() is None]
        Roster.refresh(self)

    def load(self, text):
        self.clients = parse_clients(text)
        if not self.fixed and self.client not in self.clients and time.time() >= self.next_resolve:
            self.resolve()
        Roster.load(self, text)

    def resolve(self):
        self.next_resolve = time.time() + RESOLVE_EVERY
        if self.fixed:
            return
        panes = wezterm_panes(self.wezterm)
        if panes is None:             # `wezterm cli list` itself failed: keep what we had
            return
        self.client, self.tmux_pane = pick_tmux_pane(panes, self.own, self.clients, self.hint)

    def focus_back(self, data=b""):
        """Give the keyboard back to the tmux pane (and pass it `data`, the
        keys that landed here). One re-resolve if the pane id went stale;
        if that fails too, the strip says so (focus is still here)."""
        for attempt in (0, 1):
            if self.tmux_pane is None:
                if attempt or self.fixed:
                    break
                self.resolve()
                continue
            ok = True
            if data:
                try:
                    r = subprocess.run([self.wezterm, "cli", "send-text", "--pane-id", self.tmux_pane, "--no-paste"],
                                       input=data, capture_output=True, timeout=3)
                    ok = r.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    ok = False
            r = wezterm_cli(self.wezterm, "activate-pane", "--pane-id", self.tmux_pane)
            if ok and r is not None and r.returncode == 0:
                return
            if attempt or self.fixed:
                break
            self.resolve()
        if not (self.fixed and self.tmux_pane is None):   # --client without --tmux-pane: nothing to hand to
            self.say("couldn't focus tmux · click it")

    def stray_bytes(self, raw):
        """The typed bytes of one read, minus mouse reports. A report cut off
        by the ESC_WAIT flush (`ESC [ < 0 ; 5`) is dropped, and so is its
        tail (`;7M`) at the start of the next read: neither is typing."""
        if not raw:
            return b""
        if self.cut_mouse:
            raw = MOUSE_HEAD.sub(b"", raw)
        self.cut_mouse = bool(MOUSE_TAIL.search(raw))
        return MOUSE_TAIL.sub(b"", MOUSE_RAW.sub(b"", raw))

    def handle(self, keys, raw=None):
        """Never closes (False). Left press: act on the target drawn at that
        cell. The wheel does nothing (nothing scrolls). Anything typed:
        forwarded to the tmux pane."""
        clicked, stray = None, self.stray_bytes(raw)
        for key in keys:
            if not key.startswith("mouse:"):
                continue
            _, b, x, y, kind = key.split(":")
            b, x, y = int(b), int(x), int(y)
            if kind != "M" or b & 64:          # releases, the wheel
                continue
            if (b & ~3) == 0 and b != 3:       # a plain press, no modifiers, not a drag
                clicked = (b, x, y)
        if clicked or stray:
            self.focus_back(stray)
        if clicked and clicked[0] == 0:
            self.click(clicked[1], clicked[2])
        return False

    def target(self, x, y):
        """The action drawn at column x, line y (both 1-based), or None."""
        row = self.targets[y - 1] if 0 < y <= len(self.targets) else ()
        return next((a for x0, x1, a in row if x0 <= x < x1), None)

    def click(self, x, y):
        a = self.target(x, y)
        if a is None:
            return
        if a[0] == "restart":
            run_bg("bash '%s'" % WATCHER)
            self.say("watcher restarted")
            return
        if self.client is None:
            self.say("no tmux client")
            return
        moved = False
        if a[0] == "goto":
            moved = self.go({"kind": "win", "w": a[1]})
        elif a[0] == "session":
            w = next((v for v in self.windows if v["session"] == a[1] and v["active"] == "1"), None)
            if w is None:
                self.say("session %s is gone" % a[1])
                return
            moved = self.jump("goto", w["id"], a[1])      # THIS session, even for a linked window
        elif a[0] == "next":
            moved = self.jump("next")
        elif a[0] == "menu":
            self.open_menu()
        if moved:
            self.refresh()            # the highlight follows the move now, not in a second

    def open_menu(self):
        """☰: prefix q's popup on the strip's tmux client. Popen, not run:
        display-popup -E may hold its client until the popup closes."""
        try:
            self.children.append(subprocess.Popen(
                roster_popup_argv(self.client), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True))
        except OSError as e:
            self.say("menu failed: %s" % (e.strerror or e))

    # model
    def branch(self, cwd, now):
        """git_head(cwd), cached BRANCH_TTL, and never able to stall the UI
        loop: the read runs on a daemon thread that the frame waits on for at
        most BRANCH_WAIT (a local repo answers in well under 1 ms). One that
        takes longer (a sick disk, a mount the prefix rule missed) finishes
        on its own; until it does, and for BRANCH_SLOW_TTL after the timeout,
        that cwd shows its last branch (or none) and starts no new read. A
        thread stuck for good costs one parked thread, never a frozen strip."""
        if not cwd or maybe_remote(cwd):
            return ""
        late = self.branch_reads.get(cwd)
        if late is not None and not late[0].is_alive():
            del self.branch_reads[cwd]
            if late[1]:
                self.cache_branch(cwd, now, late[1][0], BRANCH_TTL)
        hit = self.branches.get(cwd)
        if hit is not None and 0 <= now - hit[0] < hit[2]:
            return hit[1]
        if cwd in self.branch_reads:              # still reading: keep what we had
            return hit[1] if hit else ""
        import threading                          # the strip only, and only on a cache miss
        box = []
        t = threading.Thread(target=lambda: box.append(git_head(cwd)), daemon=True)
        t.start()
        t.join(BRANCH_WAIT)
        if box:
            return self.cache_branch(cwd, now, box[0], BRANCH_TTL)
        self.branch_reads[cwd] = (t, box)
        return self.cache_branch(cwd, now, hit[1] if hit else "", BRANCH_SLOW_TTL)

    def cache_branch(self, cwd, now, branch, ttl):
        """Store one entry, dropping every expired one (a day of cd's must not
        grow the cache without bound)."""
        self.branches = {k: v for k, v in self.branches.items() if 0 <= now - v[0] < v[2]}
        self.branches[cwd] = (now, branch, ttl)
        return branch

    def strip_model(self, now):
        """→ (needs, groups, parked). needs: windows in needs_order. groups:
        one per session with something to show, by name; each {name, rows
        (attention > working > idle, then index), active, branch, current}."""
        by_id = {w["id"]: w for w in self.windows}
        needs = [by_id[i] for i in dict.fromkeys(self.needs) if i in by_id]
        by = {}
        for w in self.windows:
            if w["session"] not in HIDDEN and w["session"] != HOLD:
                by.setdefault(w["session"], []).append(w)
        groups = []
        for s in sorted(by):
            ws = by[s]
            rows = sorted((w for w in ws if w["state"] or w["id"] == self.cur_win),
                          key=lambda w: (rank(w), w["index"]))
            if not rows:
                continue
            active = next((w for w in ws if w["active"] == "1"), None)
            groups.append({"name": s, "rows": rows, "active": active,
                           "branch": self.branch(active["path"] if active else "", now),
                           "current": any(w["id"] == self.cur_win for w in ws)})
        parked = [w for w in self.windows if w["session"] == HOLD]
        return needs, groups, parked

    def plan(self, needs, groups, parked, cfg):
        """The frame as line specs, for one ladder config."""
        P = [("head",)]
        if cfg["bar"]:
            P.append(("bar",))
        joined, opened = cfg["joined"], [False]

        def edge(what):
            P.append(("edge", "div" if joined and opened[0] else "top", what))
            opened[0] = True

        def close():
            if opened[0]:
                P.append(("bottom",))
                opened[0] = False
        if needs:
            nc = cfg["ncap"]
            shown = needs if nc is None or nc >= len(needs) else needs[:nc]
            edge(("needs",))
            for w in shown:
                P.append(("need", w))
                if cfg["nlines"] == 2:
                    P.append(("need2", w))
            if len(shown) < len(needs):
                P.append(("more", len(needs) - len(shown)))
            if not joined:
                close()
        sc = cfg["scap"]
        gs = groups if sc is None or sc >= len(groups) else groups[:sc]
        for i, g in enumerate(gs):
            boxed = i < cfg["boxed"]
            edge(("sess", g, boxed))
            if boxed:
                fold = [w for w in g["rows"] if cfg["fold"] and cat(w) == "idle" and w["id"] != self.cur_win]
                fold = fold if len(fold) >= 2 else []
                folded = {w["id"] for w in fold}
                for w in g["rows"]:
                    if w["id"] in folded:
                        continue
                    P.append(("row", w))
                    if cfg["rich"] and run_detail(w):
                        P.append(("run", w))
                if fold:
                    P.append(("fold", len(fold)))
            if not joined:
                close()
        if len(gs) < len(groups):
            edge(("smore", len(groups) - len(gs)))
        close()
        if not needs and not groups:
            P.append(("empty",))
        if parked:
            P.append(("parked",))
        return P

    def ladder(self, needs, groups, parked, rows):
        """→ (step name, plan): the first LADDER step whose plan fits `rows`."""
        S, N = len(groups), len(needs)
        cfg = {"bar": True, "rich": True, "nlines": 2, "joined": False, "fold": False,
               "boxed": S, "ncap": None, "scap": None}
        steps = [("rich", {}), ("full", {"rich": False}), ("joined", {"joined": True}),
                 ("needs1", {"nlines": 1}), ("fold", {"fold": True})]
        steps += [("collapse%d" % (S - k), {"boxed": k}) for k in range(S - 1, -1, -1)]
        steps.append(("nobar", {"bar": False}))
        for name, step in steps:
            cfg.update(step)
            p = self.plan(needs, groups, parked, cfg)
            if len(p) <= rows:
                return name, p
        # cap: NEEDS YOU first, sessions keep a floor of 3 lines (2 + `… more`)
        cfg["scap"] = S if S <= 3 else 2
        nc = N
        while nc > 0:
            cfg["ncap"] = nc
            if len(self.plan(needs, groups, parked, cfg)) <= rows:
                break
            nc -= 1
        cfg["ncap"] = nc
        for sc in range(S, -1, -1):
            cfg["scap"] = sc
            p = self.plan(needs, groups, parked, cfg)
            if len(p) <= rows:
                break
        return "cap", p

    # drawing
    DEFAULT_SIZE = (34, 30)

    def status(self, now):
        """What the toolbar line says instead of the toolbar, or None: a
        message (3 s), no client, or a dead watcher (clickable restart)."""
        if self.msg and now < self.msg_until:
            return fg("sky") + self.msg, None
        if self.client is None or self.gone:
            return fg("overlay") + "no tmux client", None
        age = watcher_age()
        if age is None or age > WATCHER_STALE:
            return (bg("red") + fg("crust") + BOLD + " watcher %s " % ("off" if age is None else "stalled")
                    + RESET + fg("sub") + " ⟳ restart", ("restart",))
        return None

    def render(self, cols, rows, now):
        """Exactly `rows` lines of at most cols-1 cells, at every height
        (LADDER; nothing scrolls). Records the click targets of each line."""
        rows = max(1, rows)
        W = max(1, cols - 1)
        blink = bool(self.windows) and self.windows[0]["blink"] == "1"
        needs, groups, parked = self.strip_model(now)
        self.level, plan = self.ladder(needs, groups, parked, rows)
        has_bar = ("bar",) in plan
        st = self.status(now)
        inner = W - 4
        lines, targets = [], []
        col = "surface1"
        needs_col = ("red" if any(w["state"] == "failed" for w in needs)
                     else "yellow" if any(w["state"] == "needs-input" for w in needs) else "green")

        def boxed(content, cw, hl=False):
            pad = " " * max(0, inner - cw)
            b = bg("surface0") if hl else ""
            return (fg(col) + "│" + b + " " + content + pad + " " + (UNBG if hl else "") + fg(col) + "│")

        two_line = any(s[0] == "need2" for s in plan)
        for spec in plan:
            k = spec[0]
            tg = []
            if k == "head":
                l = " " + fg("peach") + BOLD + "AGENTS" + UNBOLD
                if st and not has_bar:
                    l += "  " + st[0]
                    tg = [(1, ANY, st[1])] if st[1] else []
                else:
                    toks, _ = count_tokens(counts([w for w in self.windows
                                                   if w["session"] not in HIDDEN and w["session"] != HOLD]))
                    l += "  " + (toks or fg("overlay") + "no agents")
            elif k == "bar":
                if st:
                    l = " " + st[0]
                    tg = [(1, ANY, st[1])] if st[1] else []
                else:
                    l = " " + fg("text") + "⏵ next" + "  " + fg("sub") + "☰ menu"
                    tg = [(1, 9, ("next",)), (9, ANY, ("menu",))]
            elif k == "edge":
                what = spec[2]
                left, right = ("╭", "╮") if spec[1] == "top" else ("├", "┤")
                if what[0] == "needs":
                    col = needs_col
                    lab, lw = self.needs_label(needs, W - 5)
                    tg = [(1, ANY, ("next",))]
                elif what[0] == "sess":
                    g = what[1]
                    col = "overlay" if g["current"] else "surface1"
                    lab, lw = self.sess_label(g, what[2], W - 5)
                    tg = [(1, ANY, ("session", g["name"]))]
                else:
                    col = "surface1"
                    t = clip("… %d more sessions" % what[1], W - 5)
                    lab, lw = fg("overlay") + t, dwidth(t)
                    tg = [(1, ANY, ("menu",))]
                l = (fg(col) + left + " " + lab + " " + fg(col) + "─" * max(1, W - 4 - lw) + right)
            elif k == "bottom":
                l = fg(col) + "╰" + "─" * max(0, W - 2) + "╯"
            elif k in ("need", "row"):
                w = spec[1]
                extra = None
                if k == "need" and not two_line:
                    word = detail_word(w)
                    if word and inner >= 24:
                        extra = (fg(CAT_HUE[cat(w)]) + " " + word, len(word) + 1)
                c, cw = self.agent_content(w, inner, blink, now, extra)
                l = boxed(c, cw, w["id"] == self.cur_win)
                tg = [(1, ANY, ("goto", w))]
            elif k == "need2":
                w = spec[1]
                c, cw = self.need_detail(w, inner)
                l = boxed(c, cw, w["id"] == self.cur_win)
                tg = [(1, ANY, ("goto", w))]
            elif k == "run":
                w = spec[1]
                t = clip(CONTROL.sub(" ", w["detail"]), max(0, inner - 4))
                l = boxed("    " + fg("overlay") + t, 4 + dwidth(t), w["id"] == self.cur_win)
                tg = [(1, ANY, ("goto", w))]
            elif k == "fold":
                n = spec[1]
                t = clip("%s %d idle" % ("○" * min(n, 6), n), inner)
                l = boxed(fg("overlay") + t, dwidth(t))
                tg = [(1, ANY, ("menu",))]
            elif k == "more":
                t = clip("… %d more · ☰ menu" % spec[1], inner)
                l = boxed(fg("overlay") + t, dwidth(t))
                tg = [(1, ANY, ("menu",))]
            elif k == "empty":
                l = " " + fg("overlay") + "no agents running"
            elif k == "parked":
                attn = sum(1 for w in parked if is_attn(w))
                l = (" " + fg("sub") + "▸ parked %d" % len(parked)
                     + ((fg("overlay") + " · " + fg("yellow") + "%d need you" % attn) if attn else "")
                     + fg("overlay") + " · ☰")
                tg = [(1, ANY, ("menu",))]
            lines.append(l)
            targets.append(tg)
        if len(lines) < rows:                      # the gap sits above `parked`, which stays at the bottom
            gap = rows - len(lines)
            at = len(lines) - 1 if parked else len(lines)
            lines[at:at] = [""] * gap
            targets[at:at] = [[] for _ in range(gap)]
        self.targets = targets[:rows]
        return [clip_ansi(l, W) + RESET for l in lines[:rows]]

    def needs_label(self, needs, budget):
        t = "NEEDS YOU"
        toks, tw = count_tokens(counts(needs))
        if dwidth(t) + 1 + tw > budget:
            toks, tw = "", 0
        t = clip(t, budget)
        return fg("yellow") + BOLD + t + UNBOLD + ((" " + toks) if toks else ""), dwidth(t) + (1 + tw if tw else 0)

    def sess_label(self, g, boxed, budget):
        """`▸ name ⎇ branch ◐3 ✓1` in `budget` cells: the branch goes first
        (clipped to no less than 4 cells, else dropped), then the name is
        clipped (to no less than 4), then the rollup goes."""
        pre = "" if boxed else "▸ "
        avail = budget - len(pre)
        name, br = CONTROL.sub(" ", g["name"]), CONTROL.sub(" ", g["branch"])
        ro, rw = count_tokens(counts(g["rows"]))
        ro_w = rw + 1 if rw else 0
        nw = dwidth(name)
        if br and nw + 3 + dwidth(br) + ro_w > avail:
            left = avail - nw - ro_w - 3
            br = clip(br, left) if left >= 4 else ""
        if nw + ro_w > avail:
            if avail - ro_w >= 4:
                name = clip(name, avail - ro_w)
            else:
                ro, ro_w = "", 0
                name = clip(name, avail)
        lab = (fg("sub") + pre + fg("peach" if g["current"] else "text") + BOLD + name + UNBOLD
               + ((fg("overlay") + " ⎇ " + br) if br else "") + ((" " + ro) if ro else ""))
        return lab, len(pre) + dwidth(name) + ((3 + dwidth(br)) if br else 0) + ro_w

    def agent_content(self, w, width, blink, now, extra=None):
        """One agent line inside a box → (ansi, cells): shape, kind glyph,
        project/title, [extra], ⚙/◎, age right-aligned."""
        k = KIND_GLYPH.get(w.get("kind", ""))
        kind, kw = ((fg("overlay") + k + " "), 2) if k else ("", 0)
        g = self.strip_glyph(w, blink)
        if g:                             # its gap goes BEFORE it: a full title must not touch it
            g = " " + g[:-1]
        gw = 2 if g else 0
        age = ago(w["since_t"], now)
        right = " %3s" % age if age else ""
        ex, exw = extra or ("", 0)
        tw = max(0, width - 2 - kw - gw - len(right) - exw)
        proj, title = fit_label(w["label"], tw)
        used = dwidth(proj) + dwidth(title)
        s = (self.shape(w, blink) + " " + kind + fg("overlay") + proj + fg("text") + title
             + " " * max(0, tw - used) + ex + g + fg("overlay") + right)
        return s, 2 + kw + max(tw, used) + exw + gw + len(right)

    def need_detail(self, w, width):
        """A NEEDS YOU entry's second line → (ansi, cells): `  perm  Bash
        git push…   main:2`, or the state in words when there is no detail."""
        hue = fg(CAT_HUE[cat(w)])
        dk, d = w.get("detail_kind", ""), CONTROL.sub(" ", w.get("detail", ""))
        if dk in DETAIL_WORD and dk != "run" and d:
            word, text = DETAIL_WORD[dk], d
        else:
            word, text = STATE_WORDS.get(w["state"], w["state"]), ""
        where = clip("%s:%d" % (CONTROL.sub(" ", w["session"]), w["index"]), 12)
        avail = width - 2 - dwidth(where) - 1
        if avail < 8:
            where, avail = "", width - 2
        left = clip(word + ("  " + text if text else ""), avail)
        wl = min(len(word), dwidth(left))
        s = "  " + hue + left[:wl] + fg("overlay") + left[wl:]
        pad = width - 2 - dwidth(left) - dwidth(where)
        return s + " " * max(0, pad) + fg("overlay") + where, width if where else 2 + dwidth(left)


def detail_word(w):
    """The NEEDS YOU detail kind as a word (perm, asks, fail, done), or ""."""
    dk = w.get("detail_kind", "")
    return DETAIL_WORD[dk] if dk in DETAIL_WORD and dk != "run" and w.get("detail") else ""


def run_detail(w):
    """A working agent's own line of what it is doing (@agent_detail_kind run)."""
    return cat(w) == "working" and w.get("detail_kind") == "run" and bool(w.get("detail"))


def arg_after(argv, flag):
    if flag in argv:
        i = argv.index(flag)
        return argv[i + 1] if i + 1 < len(argv) else None
    return None


USAGE = ("usage: agent-roster.py --client <client_tty>\n"
         "       agent-roster.py --strip [--tmux-pane <wezterm pane id>] [--wezterm <path>] [--client <tty>]")


def main(argv):
    client = arg_after(argv, "--client")
    if "--strip" in argv:
        exe = arg_after(argv, "--wezterm") or os.path.join(os.environ.get("WEZTERM_EXECUTABLE_DIR", ""), "wezterm")
        if not os.path.isabs(exe) or not os.access(exe, os.X_OK):
            exe = "wezterm"
        roster = Strip(client, wezterm=exe, hint=arg_after(argv, "--tmux-pane"), own=os.environ.get("WEZTERM_PANE"))
        # SGR mouse (1000 + 1006), the user var CMD+B finds this pane by, and a title as a second marker.
        enter = ("\x1b]1337;SetUserVar=%s=MQ==\x07\x1b]2;%s\x07\x1b[?1049h\x1b[?25l\x1b[2J\x1b[?1000h\x1b[?1006h"
                 % (STRIP_VAR, STRIP_TITLE))
        leave = "\x1b[?1006l\x1b[?1000l"
    elif client:
        roster = Roster(client)
        enter, leave = "\x1b[?1049h\x1b[?25l\x1b[2J", ""
    else:
        print(USAGE, file=sys.stderr)
        return 2
    reader = KeyReader()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    rpipe, wpipe = os.pipe()
    os.set_blocking(wpipe, False)
    signal.set_wakeup_fd(wpipe)
    signal.signal(signal.SIGWINCH, lambda *_: None)
    if isinstance(roster, Strip):        # CMD+B's kill-pane: leave quietly, not with a traceback
        def bye(*_):
            raise SystemExit(0)
        signal.signal(signal.SIGHUP, bye)
        signal.signal(signal.SIGTERM, bye)
    out = sys.stdout
    try:
        tty.setraw(fd)
        out.write(enter)
        roster.refresh()
        next_refresh = time.time() + REFRESH
        roster.draw(out)
        while True:
            timeout = max(0.0, next_refresh - time.time())
            ready, _, _ = select.select([fd, rpipe], [], [], timeout)
            if rpipe in ready:
                os.read(rpipe, 64)               # SIGWINCH: redraw in full
                roster.last_frame = None
            if fd in ready:
                data = os.read(fd, 256)
                if not data:                     # the pty went away
                    return 0
                keys, raw = reader.feed(data), data
                # A read that ended mid-sequence (or on a bare ESC) waits
                # ESC_WAIT for the rest; only silence makes a lone ESC "esc".
                while reader.pending:
                    more, _, _ = select.select([fd], [], [], ESC_WAIT)
                    data = os.read(fd, 256) if more else b""
                    if not data:
                        keys += reader.flush()
                        break
                    keys += reader.feed(data)
                    raw += data
                if roster.handle(keys, raw):
                    return 0
            if time.time() >= next_refresh:
                roster.refresh()
                next_refresh = time.time() + REFRESH
            # Input that arrived while we refreshed (or ran a move) was typed
            # or clicked against the frame still on screen: handle it against
            # THAT frame (drawn / targets) before drawing a new one, or a
            # digit or click would land on whatever row now sits there.
            if select.select([fd], [], [], 0)[0]:
                continue
            roster.draw(out)
    finally:
        try:
            out.write(leave + "\x1b[0m\x1b[?25h\x1b[?1049l")
            out.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except (OSError, termios.error):     # the pane is already gone
            pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
