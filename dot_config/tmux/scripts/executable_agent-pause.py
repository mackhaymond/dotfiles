#!/usr/bin/env python3
"""agent-pause.py: prefix P, the pause key (docs/agent-pause.md).

Before the laptop lid closes, one press tells every agent that is actually
WORKING to stop at a safe point, and remembers exactly which ones it told.
The next press tells exactly that saved set to resume. Not a fresh scan:
agents finish while the lid is shut and states move on, and a RESUME must
reach the agents that were paused, not whatever happens to be running now.

    agent-pause.py toggle <client_name>   the key: pause, or resume a saved set
    agent-pause.py resume <client_name>   resume a saved set, never pause (the sidebar)
    agent-pause.py dismiss [client_name]  turn off the notch's "all paused" glow; stays paused
    agent-pause.py void [client_name]     forget the pause, message nobody; they stay paused
    agent-pause.py status                 print the saved set, or "not paused"
    agent-pause.py dry-run                print who WOULD be paused/resumed; sends nothing
    agent-pause.py watch pause|resume [--demo] [--press <id>]
                                          the popup's view (see FEEDBACK below);
                                          --demo plays it with fake agents;
                                          --press: follow that press's delivery
                                          (progress.json) first

Rate-limited agents (RATE LIMITS below; docs "Rate-limited agents"):
    agent-pause.py limit-add <window>     remember a window the indicator marked @agent_limit
    agent-pause.py limit-tick             resume the limited agents whose limit has lifted
    agent-pause.py limit-auto on|off|toggle
                                          @agent_limit_autoresume (Mack's setting)
    agent-pause.py limit-resume [--force] <window>
                                          resume one limited agent now (the sidebar click)

WORKING (Mack's correction: idle agents are never messaged) is read from the
per-window options the tab indicator hooks and agent-tab-watcher.sh keep:
  @agent_state running          a turn is in progress
  @agent_workflow non-empty     a background workflow/subagent is out, even if
                                the main agent sits idle or done waiting on it
  @agent_cua non-empty          it is driving an app
Anything else (idle, done, needs-input, failed with neither flag) is not
working. needs-input is blocked on Mack already, and tmux-mcp would only hold
a message behind its dialog.

THE AGENT PANE in a window is the one tmux-mcp itself knows as an agent: a
live claude-<pid>.json / codex-<pid>.json / opencode-<pid>.json record in
tmux-assistant-resurrect's dir whose env.tmux_pane names it (the same lookup
as tmux-mcp's readAgents, same dir search order; an opencode record counts
only for the interactive TUI, never `opencode serve` or `attach`).
message_agent refuses any pane without one, so picking by anything else would
only pick panes that cannot be reached. A window with no record falls back to
the pane whose command looks like an agent (claude, codex, Claude's
version-named binary, or opencode in a window marked @agent_kind opencode,
which tmux-mcp also accepts without a record); other such deliveries will most
likely be refused and are reported as failures, never dropped silently.

Pausing an OpenCode agent is the same text message as for the others, typed
into its prompt by message_agent: no signal, nothing that could suspend or
kill the TUI. tmux-mcp holds it behind a draft like any other.

DELIVERY goes through tmux-mcp's message_agent (MCP over HTTP, bearer token
from its state dir), not a raw paste: its draft guard HOLDS a message while
the target's prompt box has a half-typed draft or a dialog, and delivers it
once the box is clear. "held" therefore counts as delivered.

This file is the keybind path: it never exits non-zero and never prints in
toggle mode (run-shell would open a view pane over Mack's window); every
outcome is a notify.sh message on the pressing client (a sidebar toast, else
a display-message) and a line in the log.
"""
import concurrent.futures
import contextlib
import fcntl
import itertools
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HOME = Path.home()
STATE_DIR = Path(os.environ.get("AGENT_PAUSE_STATE_DIR") or HOME / ".local/state/agent-pause")
STATE = STATE_DIR / "paused.json"
LAST = STATE_DIR / "paused.last.json"
LOG = STATE_DIR / "log"
LOCK = STATE_DIR / "lock"
PROGRESS = STATE_DIR / "progress.json"
VIEW = STATE_DIR / "view.pid"            # the open PAUSE popup's watch pid (pause_view_open)
MCP_URL = os.environ.get("TMUX_MCP_URL") or (
    "http://127.0.0.1:%s/mcp" % (os.environ.get("TMUX_MCP_PORT") or "7998"))
TOKEN_FILE = Path(os.environ.get("TMUX_MCP_TOKEN_FILE") or HOME / ".local/state/tmux-mcp/token")
KICK = "launchctl kickstart -k gui/%d/com.mackhaymond.tmux-mcp" % os.getuid()
# Per message_agent call. Generous: tmux-mcp may queue several deliveries to
# one pane behind each other, and a timeout only makes the result "unknown".
CALL_TIMEOUT = float(os.environ.get("AGENT_PAUSE_CALL_TIMEOUT") or 60)
# A saved agent a resume misses (gone, or refused) stays saved for the next
# press, until it has missed MISS_DROP times AND its first miss is MISS_AGE s
# old; then it is dropped, so one closed or always-refusing agent cannot hold
# the toggle in resume mode forever. Both, not either: two quick presses right
# after a resurrect restore must not drop an agent whose SessionStart hook
# simply has not rewritten its record yet.
MISS_DROP, MISS_AGE = 2, 60

PAUSE_MSG = (
    "[pause key] PAUSE NOW: Mack is shutting the laptop right now. Drop your work "
    "immediately: don't finish the task, the step or the current thought. Clean up "
    "only what would be left broken, and fast: if you are mid-edit, finish or undo "
    "that one edit so no file is half-written; stop dev servers and long jobs you "
    "started (kill_pty). Stop running subagents/workflows FIRST, outright "
    "(TaskStop), and only then kill_pty their shells: a live subagent recreates a "
    "killed shell and starts its next command in it, and cancelling its call does "
    "not stop a command already running there. Note where each was so a RESUME "
    "can restart it. Start "
    "nothing else: no new edits, tests, builds, commits or pushes. Then reply in "
    "2-3 lines with where you stopped and what's next, and stay stopped. Do not "
    "continue for any reason until a RESUME message arrives.")
RESUME_MSG = (
    "[pause key] RESUME: Mack is back. Pick up where you paused, using the next "
    "steps you noted, and restart any servers, jobs or subagents you stopped if "
    "you still need them.")

# Sessions that only park or mirror windows (agent-jump.sh's EXCLUDE): a
# window linked into one of these AND a real session is shown by the real one.
EXCLUDE = {"agents", "tasks", "stash", "scratch", "btop-popup"}
# Claude Code's kernel p_comm is its version ("2.1.170"); see the watcher's
# AGENT_PAT. Only the fallback for a pane with no session record. An
# `opencode` pane counts only in a window the watcher or plugin marked
# @agent_kind opencode (scan): pane_current_command can't tell the TUI from
# `opencode serve` / `attach`, and the kind is set for the TUI alone (same
# rule as tmux-mcp's isOpencodeTab).
AGENT_CMD = re.compile(r"^(claude|codex|opencode|[0-9]+\.[0-9]+\.[0-9]+)$")

# opencode_tui_args in agent-tab-watcher.sh, ported: is this opencode command
# line the interactive TUI? The first bare word after the program decides;
# options that take a value skip it. `attach` is a TUI on a remote server
# whose plugin cannot report this pane, so like serve/web/mcp/... it is not
# an agent here either.
OPENCODE_VALUE_OPTS = {"--log-level", "--port", "--hostname", "--mdns-domain", "--cors", "-m",
                       "--model", "-s", "--session", "--prompt", "--agent", "--replay-limit"}
OPENCODE_NOT_TUI = {"completion", "acp", "mcp", "attach", "run", "debug", "providers", "auth",
                    "agent", "upgrade", "uninstall", "serve", "web", "models", "stats", "export",
                    "import", "github", "session", "plugin", "plug", "db"}


def opencode_tui_args(args):
    a = args.split()
    i = 1
    while i < len(a):
        w = a[i]
        if w in OPENCODE_VALUE_OPTS:
            i += 2
        elif w == "--":
            return True
        elif w.startswith("-"):
            i += 1
        else:
            return w not in OPENCODE_NOT_TUI
    return True


def proc_args(pid):
    """`ps -o args=` for one pid, "" when it can't be read."""
    try:
        return subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(pid)], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def cmd_kind(cmd):
    """The agent kind a pane_current_command (an AGENT_CMD match) names."""
    return cmd if cmd in ("codex", "opencode") else "claude"
US = "\x1f"
# @agent_summary is free text, so it goes LAST and the split is bounded.
# @agent_limit is "<resets_at> <type> <since>" (RATE LIMITS): no separator in it.
PANE_FIELDS = ("pane_id", "window_id", "session_name", "window_index", "pane_index",
               "pane_pid", "pane_current_command", "pane_active", "@agent_state",
               "@agent_workflow", "@agent_cua", "@agent_kind", "@agent_paused",
               "@agent_limit", "@agent_limit_lifted", "@agent_pause_voided", "@agent_summary")
PANE_FMT = US.join("#{%s}" % f for f in PANE_FIELDS)
DISPLAY_MS = "6000"
# Messages go through notify.sh: a toast in the agent-ui sidebar when one
# shows the client (a display-message would bring the hidden status line
# back), tmux's own message otherwise.
NOTIFY = Path(os.environ.get("AGENT_PAUSE_NOTIFY") or HOME / ".config/tmux/scripts/notify.sh")


# ---------------------------------------------------------------- tmux

def tmux(*args, check=True):
    r = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=10)
    if check and r.returncode:
        raise RuntimeError("tmux %s: %s" % (args[0], r.stderr.strip() or r.returncode))
    return r.stdout


def list_panes():
    """{pane_id: row}, one row per pane. A window linked into several
    sessions lists its panes once per link; the location kept is the one in a
    real session (not EXCLUDE), else the first seen."""
    out = {}
    for line in tmux("list-panes", "-a", "-F", PANE_FMT).splitlines():
        parts = line.split(US, len(PANE_FIELDS) - 1)
        if len(parts) != len(PANE_FIELDS) or not parts[0].startswith("%"):
            continue
        row = dict(zip((f.lstrip("@") for f in PANE_FIELDS), parts))
        row["location"] = "%s:%s.%s" % (row["session_name"], row["window_index"], row["pane_index"])
        prev = out.get(row["pane_id"])
        if prev is None or (prev["session_name"] in EXCLUDE and row["session_name"] not in EXCLUDE):
            out[row["pane_id"]] = row
    return out


def display(client, msg, kind="info"):
    """One line on the pressing client: notify.sh (a sidebar toast, or the
    display-message line when no sidebar shows that client). Both expand
    formats, so every '#' in the text is doubled. A vanished client falls
    back to none (notify: every sidebar, else tmux's current client); no
    notify.sh, the plain display-message; nothing at all is fine (the log
    has it)."""
    msg = msg.replace("#", "##")
    if os.access(NOTIFY, os.X_OK):
        if client:
            names = subprocess.run(["tmux", "list-clients", "-F", "#{client_name}"],
                                   capture_output=True, text=True, timeout=10).stdout.split("\n")
            if client not in names:
                client = ""
        cmd = [str(NOTIFY)] + (["-c", client] if client else [])
        subprocess.run(cmd + ["-k", kind, "-t", str(int(DISPLAY_MS) / 1000), "--", msg],
                       capture_output=True, timeout=10)
        return
    if client:
        r = subprocess.run(["tmux", "display-message", "-c", client, "-d", DISPLAY_MS, msg],
                           capture_output=True, timeout=10)
        if r.returncode == 0:
            return
    subprocess.run(["tmux", "display-message", "-d", DISPLAY_MS, msg], capture_output=True, timeout=10)


def set_paused_option(n):
    """@agents_paused (global): the count while paused, unset otherwise. For a
    status indicator; nothing renders it yet. Cosmetic, so it never raises: a
    tmux timeout here must not cost the press its outcome."""
    try:
        if n:
            tmux("set-option", "-g", "@agents_paused", str(n), check=False)
        else:
            tmux("set-option", "-gu", "@agents_paused", check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def mark_paused(panes, on, repause=False, stamp=None, unless_limited=False):
    """The per-agent PAUSED flag (docs/agent-pause.md, "The paused state"):
    @agent_paused = <epoch> on each pane's window, which tells the tab hooks
    to end its turn as @agent_state `paused` instead of done or a quiet
    restore. Set BEFORE the PAUSE goes out (the reply's Stop can land within
    seconds) and cleared BEFORE the RESUME (that turn must end normally);
    the misses are put back afterwards. Clearing also turns a window still
    sitting in `paused` back to idle (the RESUME's prompt makes it running);
    repause (a RESUME that was refused) undoes that. One tmux call per pane:
    tmux abandons the rest of a command list at the first error, so one
    closed pane must not cost the others their flag. Cosmetic, so it never
    raises. stamp: the value to write instead of now. A rate-limited
    window's flag must be its @agent_limit's <since>: the indicator clears
    the flag on the turn's outcome only when the two match (RATE LIMITS).
    Either way a void's orphan mark (@agent_pause_voided, void_pause) goes:
    the flag is this press's, or nobody's, from now on. unless_limited
    (clearing only): leave a window that carries @agent_limit alone, tested
    by tmux in the same call - the indicator may have marked it limited
    since we looked, and its flag is then the limit's <since>."""
    panes = [p for p in dict.fromkeys(panes) if p]
    if not panes:
        return
    try:
        for p in panes:
            if on:
                cmds = ["set-option", "-w", "-t", p, "@agent_paused", str(int(stamp or time.time())), ";",
                        "set-option", "-wu", "-t", p, "@agent_pause_voided"]
                if repause:
                    cmds += [";", "if-shell", "-F", "-t", p, "#{==:#{@agent_state},idle}",
                             "set-option -w -t %s @agent_state paused" % p]
            elif unless_limited:
                cmds = ["if-shell", "-F", "-t", p,
                        "#{&&:#{==:#{@agent_state},paused},#{?#{@agent_limit},0,1}}",
                        "set-option -w -t %s @agent_state idle" % p, ";",
                        "if-shell", "-F", "-t", p, "#{?#{@agent_limit},0,1}",
                        "set-option -wu -t %s @agent_paused ; set-option -wu -t %s @agent_pause_voided"
                        % (p, p)]
            else:
                cmds = ["set-option", "-wu", "-t", p, "@agent_paused", ";",
                        "set-option", "-wu", "-t", p, "@agent_pause_voided", ";",
                        "if-shell", "-F", "-t", p, "#{==:#{@agent_state},paused}",
                        "set-option -w -t %s @agent_state idle" % p]
            tmux(*cmds, check=False)
        # The sidebar redraws on this notification instead of its 5 s poll.
        subprocess.run(["notifyutil", "-p", "com.mackhaymond.tmux.agent-state"],
                       capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


# ------------------------------------------------------ agent identity

def resurrect_dir():
    """tmux-assistant-resurrect's pid -> session record dir, searched in
    tmux-mcp's resurrectDir() order so both see the same records."""
    cands = []
    for var in ("TMUX_ASSISTANT_RESURRECT_DIR",):
        if os.environ.get(var):
            cands.append(Path(os.environ[var]))
    for var in ("XDG_RUNTIME_DIR", "TMPDIR"):
        if os.environ.get(var):
            cands.append(Path(os.environ[var]) / "tmux-assistant-resurrect")
    for c in cands:
        if c.is_dir():
            return c
    if sys.platform == "darwin":
        try:
            t = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True,
                               text=True, timeout=5).stdout.strip()
            if t and (Path(t) / "tmux-assistant-resurrect").is_dir():
                return Path(t) / "tmux-assistant-resurrect"
        except (OSError, subprocess.SubprocessError):
            pass
    p = Path("/tmp/tmux-assistant-resurrect")
    return p if p.is_dir() else None


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False


def stamp(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def claude_session_id(pid):
    """~/.claude/sessions/<pid>.json's sessionId (agent-session-lib.sh's
    convention), or "" when the file is missing or belongs to another pid."""
    try:
        d = json.loads((HOME / ".claude" / "sessions" / ("%d.json" % pid)).read_text())
    except (OSError, ValueError):
        return ""
    if not isinstance(d, dict) or d.get("pid") not in (None, pid):
        return ""
    sid = d.get("sessionId")
    return sid if isinstance(sid, str) else ""


def agent_records():
    """{pane_id: {kind, pid, session_id}} from live session records; newest
    record wins per pane, like tmux-mcp. A claude record's session id is
    replaced by the sessions file's when that is readable (it follows /clear
    and compaction; the record is only rewritten on SessionStart)."""
    d = resurrect_dir()
    out = {}
    if d is None:
        return out
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for name in names:
        m = re.match(r"^(claude|codex|opencode)-(\d+)\.json$", name)
        if not m:
            continue
        kind, pid = m.group(1), int(m.group(2))
        if not alive(pid):
            continue
        # The resurrect plugin's OpenCode plugin can run in `opencode serve`
        # (or an attach client) too: only the TUI is an agent. An argv we
        # cannot read keeps the record (a missed pause is the worse failure).
        if kind == "opencode":
            args = proc_args(pid)
            if args and not opencode_tui_args(args):
                continue
        try:
            j = json.loads((d / name).read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(j, dict):
            continue
        pane = (j.get("env") or {}).get("tmux_pane") if isinstance(j.get("env"), dict) else None
        sid = j.get("session_id")
        if not pane or not sid:
            continue
        rec = {"kind": kind, "pid": pid, "session_id": str(sid), "stamp": stamp(j.get("timestamp"))}
        if kind == "claude":
            rec["session_id"] = claude_session_id(pid) or rec["session_id"]
        prev = out.get(pane)
        if prev is None or rec["stamp"] >= prev["stamp"]:
            out[pane] = rec
    return out


# ------------------------------------------------------------- scanning

def working_reasons(row):
    # An agent waiting on Mack is never WORKING, whatever runs beside it: a
    # turn that ended asking while its background work is still out
    # (agent-tab-indicator.sh, ASKS WHILE WORK IS OUT) is needs-input with
    # @agent_workflow lit, and a pause message would land on its question.
    if row["agent_state"] == "needs-input":
        return []
    # A rate-limited agent is paused with a reason (RATE LIMITS), not
    # working, even with a workflow still out: that one is limited too. Only
    # its own RESUME's turn (running, @agent_limit kept until the outcome) is.
    if row.get("agent_limit") and row["agent_state"] != "running":
        return []
    why = []
    if row["agent_state"] == "running":
        why.append("running")
    if row["agent_workflow"]:
        why.append("workflow")
    if row["agent_cua"]:
        why.append("cua")
    return why


def label_of(row):
    return (row["agent_summary"].strip() or row["pane_current_command"] or row["pane_id"])


def window_rows(panes):
    """{window_id: [rows by pane_index]}; window options are the same on
    every pane of a window, so rows[0] answers for all of them."""
    windows = {}
    for row in panes.values():
        windows.setdefault(row["window_id"], []).append(row)
    for rows in windows.values():
        rows.sort(key=lambda r: int(r["pane_index"] or 0))
    return windows


def pick_agent_pane(rows, recs):
    """The agent pane among one window's rows -> (row, "record" | "command"),
    or (None, "") when there is none we could message (see THE AGENT PANE)."""
    kind = rows[0]["agent_kind"]
    with_rec = [r for r in rows if r["pane_id"] in recs]
    if with_rec:
        with_rec.sort(key=lambda r: (recs[r["pane_id"]]["kind"] != kind if kind else False,
                                     r["pane_active"] != "1"))
        return with_rec[0], "record"
    cmd = [r for r in rows if AGENT_CMD.match(r["pane_current_command"])
           and (r["pane_current_command"] != "opencode" or kind == "opencode")]
    cmd.sort(key=lambda r: (cmd_kind(r["pane_current_command"]) != kind if kind else False,
                            r["pane_active"] != "1"))
    return (cmd[0], "command") if cmd else (None, "")


def agent_target(pick, head, recs):
    """A saved target's identity (scan, limit-add): enough for locate()."""
    rec = recs.get(pick["pane_id"], {})
    return {
        "pane_id": pick["pane_id"],
        "window_id": head["window_id"],
        "location": pick["location"],
        "label": label_of(pick),
        "kind": rec.get("kind") or head["agent_kind"] or cmd_kind(pick["pane_current_command"]),
        "session_id": rec.get("session_id", ""),
        "pid": rec.get("pid"),
        "pane_pid": pick["pane_pid"],   # the pane's identity when there is no session id (locate)
        "pane_cmd": pick["pane_current_command"],   # ... with the agent it ran then
    }


def scan(panes, recs):
    """-> (targets, idle, unidentified). targets: one dict per working agent
    window; idle: agent windows that are not working (for dry-run); and
    unidentified: working windows with no pane we could message."""
    targets, idle, unidentified = [], [], []
    for wid, rows in window_rows(panes).items():
        head = rows[0]
        if not (head["agent_state"] or head["agent_kind"] or any(r["pane_id"] in recs for r in rows)):
            continue   # not an agent window at all
        why = working_reasons(head)
        if not why:
            idle.append({"window_id": wid, "location": head["location"],
                         "state": head["agent_state"] or "(no state)", "label": label_of(head)})
            continue
        pick, how = pick_agent_pane(rows, recs)
        if pick is None:
            unidentified.append({"window_id": wid, "location": head["location"],
                                 "why": "+".join(why), "label": label_of(head)})
            continue
        t = dict(agent_target(pick, head, recs), why="+".join(why), found_by=how)
        if head.get("agent_pause_voided") and head["agent_paused"].isdigit():
            # Still paused from a voided set (its workflow or cua is out):
            # a PAUSE this agent does not take must leave it so (finish_pause).
            t["orphan_flag"] = head["agent_paused"]
        targets.append(t)
    return targets, idle, unidentified


def locate(t, panes, recs):
    """Where a saved target lives now -> (pane_id | None, note). Its own pane
    if it still exists and (when a session id was saved) still runs that
    session; else a pane now running that session (a tmux-resurrect restore
    renumbers panes); else gone.

    With no session id (the command fallback: an opencode TUI with no
    record) the pane id alone is not identity: a server restored by
    tmux-resurrect numbers panes from %0 again, so the saved %7 can be
    another agent, which got the RESUME while the paused one was dropped
    (2026-10-22). Such a pane counts only while its pane_pid is the saved
    one. A target saved before pane_pid was recorded has none to compare
    and keeps the old trust.

    pane_pid is the SHELL's pid when the agent was started from one, so it
    survives Mack quitting that agent and starting another in the same
    pane (2026-10-22 review). So the pane must also still run the same kind
    of agent (pane_current_command, as saved, else the target's kind) and
    carry no session record: a record there means another, identified agent
    lives in it now."""
    sid = t.get("session_id") or ""
    pane = t.get("pane_id")
    note = "pane gone"
    if pane in panes:
        if not sid:
            row = panes[pane]
            saved = t.get("pane_pid")
            cmd = row.get("pane_current_command") or ""
            kind = cmd_kind(t["pane_cmd"]) if t.get("pane_cmd") else t.get("kind")
            same = (not saved or row.get("pane_pid") == saved) and pane not in recs \
                and bool(AGENT_CMD.match(cmd)) and (not kind or cmd_kind(cmd) == kind)
            if same:
                return pane, ""
            return None, "pane now runs something else"
        cur = recs.get(pane, {}).get("session_id")
        if cur == sid:
            return pane, ""
        note = "pane now runs another session" if cur else "pane no longer runs that agent"
    if sid:
        for p, rec in recs.items():
            if p in panes and rec.get("session_id") == sid:
                return p, "moved from %s" % pane
    return None, note


# ------------------------------------------------------------- MCP client

class McpError(Exception):
    """tmux-mcp said no (bad token, an HTTP error status, a refused tool)."""


class McpUnreachable(McpError):
    """Nothing could be sent: the connection itself was refused. The only case
    where kickstarting tmux-mcp is the right advice."""


class McpUncertain(McpError):
    """The request may have been delivered, but no usable reply came back (a
    timeout, a reset, a malformed response). tmux-mcp never aborts a
    message_agent when its client goes away, so for a tool call this means
    "probably reached": the target is saved, because a stray RESUME is
    harmless and a paused agent nobody resumes is not. Never answered with a
    kickstart: that would drop the messages tmux-mcp is holding."""


class Mcp:
    """The smallest Streamable-HTTP MCP client that can call one tool: one
    session for all calls, responses as JSON or SSE. Thread-safe for call()."""

    def __init__(self, url, token):
        self.url, self.token = url, token
        self.sid = None
        self.version = "2025-06-18"
        self.ids = itertools.count(1)
        self.id_lock = threading.Lock()

    def _headers(self):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "Authorization": "Bearer " + self.token}
        if self.sid:
            h["mcp-session-id"] = self.sid
            h["mcp-protocol-version"] = self.version
        return h

    def _send(self, payload, timeout, method="POST"):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.url, data=data, method=method, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.headers, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise McpError("tmux-mcp rejected the token (%s)" % TOKEN_FILE)
            raise McpError("tmux-mcp HTTP %d" % e.code)
        except urllib.error.URLError as e:
            # urllib wraps errors from connecting and sending; only a refused
            # or impossible connection proves nothing went out.
            if isinstance(e.reason, (ConnectionRefusedError, FileNotFoundError, socket.gaierror)):
                raise McpUnreachable("tmux-mcp unreachable (%s), fix: %s" % (e.reason, KICK))
            raise McpUncertain("no reply from tmux-mcp (%s)" % (e.reason,))
        except (OSError, http.client.HTTPException) as e:
            # Raised from reading the response (timeout, reset, IncompleteRead,
            # BadStatusLine, RemoteDisconnected...): the request was written.
            raise McpUncertain("no reply from tmux-mcp (%s: %s)" % (type(e).__name__, e))

    @staticmethod
    def _response(body, want):
        """The JSON-RPC response with id `want`, from a JSON or SSE body."""
        docs = []
        s = body.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                docs.append(json.loads(s))
            except ValueError:
                pass
        else:
            data = []
            # Not splitlines(): it also splits on U+2028/U+0085/\x1c-\x1e,
            # which JSON.stringify leaves raw inside strings.
            for line in body.replace("\r\n", "\n").split("\n") + [""]:
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line.rstrip("\r") and data:   # not strip(): it eats U+0085 etc.
                    try:
                        docs.append(json.loads("\n".join(data)))
                    except ValueError:
                        pass
                    data = []
        for d in docs:
            for m in (d if isinstance(d, list) else [d]):
                if isinstance(m, dict) and m.get("id") == want and ("result" in m or "error" in m):
                    return m
        raise McpUncertain("tmux-mcp sent no response to request %s" % want)

    def _next_id(self):
        with self.id_lock:
            return next(self.ids)

    def open(self):
        rid = self._next_id()
        headers, body = self._send({
            "jsonrpc": "2.0", "id": rid, "method": "initialize",
            "params": {"protocolVersion": self.version, "capabilities": {},
                       "clientInfo": {"name": "agent-pause", "version": "1"}}}, 10)
        m = self._response(body, rid)
        if "error" in m:
            raise McpError("tmux-mcp initialize failed: %s" % m["error"].get("message"))
        self.sid = headers.get("mcp-session-id")
        if not self.sid:
            raise McpError("tmux-mcp gave no session id")
        self.version = (m.get("result") or {}).get("protocolVersion") or self.version
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"}, 10)

    def call(self, name, args, timeout=None):
        timeout = timeout or CALL_TIMEOUT
        rid = self._next_id()
        _, body = self._send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                              "params": {"name": name, "arguments": args}}, timeout)
        return self._response(body, rid)

    def close(self):
        if not self.sid:
            return
        try:
            self._send(None, 5, method="DELETE")
        except McpError:
            pass
        self.sid = None


def classify(resp):
    """message_agent's reply -> (status, detail): sent | held | failed."""
    if "error" in resp:
        return "failed", str((resp["error"] or {}).get("message") or "error")
    res = resp.get("result") or {}
    text = " ".join(c.get("text", "") for c in res.get("content") or []
                    if isinstance(c, dict) and c.get("type") == "text").strip()
    first = text.split("\n")[0] if text else ""
    if res.get("isError"):
        return "failed", first or "error"
    if first.startswith("held for"):
        return "held", first
    if first.startswith("sent to"):
        return "sent", first
    return "failed", first or "empty reply"


REACHED = ("sent", "held", "unknown")   # "unknown" = no reply, probably delivered


def held_reason(detail):
    """tmux-mcp's why from a held reply ("held for @x (loc, pane %N): <why>.
    It will be delivered…") -> "<why>", or a generic one. The sidebar reads
    the same format (8a0b766), so it stays tmux-mcp's verbatim text."""
    m = re.match(r"held for \S+ \([^)]*\): (.*)$", detail or "", re.S)
    why = m.group(1).split(" It will be delivered")[0].strip().rstrip(".") if m else ""
    return why or "its prompt box is not free: look at its tab"


def deliver(mcp, jobs, msg, on_result=None, urgent=False):
    """jobs [(key, pane_id)] -> {key: (status, detail)}, concurrently: each
    delivery spends ~400 ms in tmux-mcp's draft guard. Never raises: every
    job gets a status - sent | held | failed | unknown (see McpUncertain;
    anything unexpected counts as unknown too, since it may have gone out).
    on_result(key, (status, detail)) is called as each one lands (progress).
    urgent (the PAUSE; Mack: "pause should override that hold"): tmux-mcp
    sends it along with a half-written draft, behind a note, instead of
    holding it until the box clears; only a dialog still holds it."""
    args = {"raw": True, "message": msg}
    if urgent:
        args["urgent"] = True

    def one(pane):
        try:
            return classify(mcp.call("message_agent", dict(args, pane=pane)))
        except McpUncertain as e:
            return "unknown", str(e)
        except McpError as e:
            return "failed", str(e)
        except Exception as e:
            return "unknown", "%s: %s" % (type(e).__name__, e)
    out = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(jobs)))) as ex:
            futs = {ex.submit(one, pane): key for key, pane in jobs}
            for f in concurrent.futures.as_completed(futs):
                out[futs[f]] = f.result()
                if on_result:
                    try:
                        on_result(futs[f], out[futs[f]])
                    except Exception:
                        pass   # progress is display only
    except Exception as e:   # e.g. no threads: whatever did not report may have gone out
        for key, _ in jobs:
            out.setdefault(key, ("unknown", "%s: %s" % (type(e).__name__, e)))
    return out


def open_mcp():
    try:
        token = TOKEN_FILE.read_text().strip()
    except OSError:
        raise McpError("tmux-mcp token missing (%s): is tmux-mcp installed and running?" % TOKEN_FILE)
    if not token:
        raise McpError("tmux-mcp token empty (%s)" % TOKEN_FILE)
    m = Mcp(MCP_URL, token)
    try:
        m.open()
    except BaseException:
        m.close()   # initialize may have left a session behind
        raise
    return m


# ---------------------------------------------------------------- state

def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_state():
    try:
        d = json.loads(STATE.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log({"mode": "warn", "message": "unreadable %s: %s" % (STATE, e)})
        return None
    return d if isinstance(d, dict) and isinstance(d.get("targets"), list) else None


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def log(entry):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > 1_000_000:
            os.replace(LOG, LOG.with_name("log.1"))
        with open(LOG, "a") as f:
            f.write(json.dumps(dict({"ts": now_iso()}, **entry)) + "\n")
    except OSError:
        pass


def short(label, n=18):
    return label if len(label) <= n else label[:n - 1] + "…"


def names(items, n=4):
    shown = ", ".join(short(t["label"]) for t in items[:n])
    return shown + (" +%d" % (len(items) - n) if len(items) > n else "")


# ---------------------------------------------------------------- modes

# PROGRESS (progress.json): what the press in flight is doing, for the popup
# that opens BEFORE delivery starts (Mack: it must appear at once, not after
# the seconds tmux-mcp takes). One record per press, rewritten as each result
# lands: {press, mode, done, outcome, message, targets[] with "result"}, the
# result "sending" until that target's delivery returns. It is display data
# only: paused.json alone still decides the toggle, and is written only once
# a pause has reached someone, so a pause in flight (lock held) or one that
# reached nobody never reads as "a resumable set exists". outcome, set with
# done: paused | failed (pause), resumed | not_resumed | failed (resume).

def new_press():
    return "%d-%d" % (int(time.time() * 1000), os.getpid())


class Progress:
    """Writes progress.json for one press; thread-safe (results land from the
    delivery threads)."""

    def __init__(self, press, mode, targets, extra=()):
        self.press, self.mode, self.targets, self.extra = press, mode, targets, list(extra)
        self.lock = threading.Lock()
        self.write()

    def write(self, done=False, outcome="", message=""):
        with self.lock:
            try:
                write_json(PROGRESS, {"press": self.press, "mode": self.mode, "done": done,
                                      "outcome": outcome, "message": message,
                                      "targets": self.targets + self.extra})
            except OSError:
                pass   # display data only: never let it cost a delivery

    def result(self, i, res):
        with self.lock:
            self.targets[i]["result"], self.targets[i]["detail"] = res
        self.write()


def read_progress():
    try:
        d = json.loads(PROGRESS.read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def noid_text(unidentified):
    return (" · %d working window%s had no agent pane: %s" % (
        len(unidentified), "" if len(unidentified) == 1 else "s", names(unidentified))
        if unidentified else "")


def plan_pause():
    """The cheap half of a pause (tmux + record reads): -> (targets, unidentified)."""
    targets, _idle, unidentified = scan(list_panes(), agent_records())
    for t in targets:
        t["result"], t["detail"] = "sending", ""
    return targets, unidentified


def finish_pause(client, prog, targets, unidentified):
    """The slow half: deliver PAUSE, write paused.json (reached targets only)
    and the final progress. -> (message line, paused?)."""
    entry = {"mode": "pause", "client": client, "unidentified": unidentified}
    noid = noid_text(unidentified)
    try:
        mcp = open_mcp()
    except McpError as e:
        for t in targets:
            t["result"], t["detail"] = "failed", str(e)
        msg = "⏸ pause failed, nothing sent: %s" % e
        prog.write(True, "failed", msg)
        log(dict(entry, message=msg, targets=targets))
        return msg, False
    mark_paused([t["pane_id"] for t in targets], True)
    try:
        res = deliver(mcp, [(i, t["pane_id"]) for i, t in enumerate(targets)], PAUSE_MSG,
                      on_result=prog.result, urgent=True)
    finally:
        mcp.close()
    for i, t in enumerate(targets):
        # A job with no result at all may still have gone out: unknown.
        t["result"], t["detail"] = res.get(i, ("unknown", "no result"))
    ok = [t for t in targets if t["result"] in REACHED]
    bad = [t for t in targets if t["result"] not in REACHED]
    # A miss marked rate-limited during the delivery keeps the limit's flag
    # (unless_limited: tmux tests it in the same call).
    mark_paused([t["pane_id"] for t in bad if not t.get("orphan_flag")], False, unless_limited=True)
    # A voided set's agent this PAUSE missed was paused before the press and
    # stays so: its own flag back, an orphan again (void_pause) - unless it
    # was marked limited meanwhile, whose <since> must stand (RATE LIMITS).
    # One call per pane, the test in it, as everywhere.
    for t in bad:
        if t.get("orphan_flag"):
            p, stamp = t["pane_id"], str(int(t["orphan_flag"]))
            try:
                tmux("if-shell", "-F", "-t", p, "#{?#{@agent_limit},0,1}",
                     "set-option -w -t %s @agent_paused %s ; set-option -w -t %s @agent_pause_voided %d"
                     % (p, stamp, p, int(time.time())), check=False)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
    for t in targets:
        t.pop("orphan_flag", None)
    held = [t for t in ok if t["result"] == "held"]
    unsure = [t for t in ok if t["result"] == "unknown"]
    if not ok:
        msg = "⏸ pause failed: no agent took the message (%s: %s)%s" % (
            short(bad[0]["label"]), bad[0]["detail"], noid)
        prog.write(True, "failed", msg)
        log(dict(entry, message=msg, targets=targets))
        return msg, False
    # paused.json BEFORE the final progress: the popup reads it on "done".
    write_json(STATE, {"version": 1, "paused_at": now_iso(), "targets": ok, "failed": bad,
                       "unidentified": unidentified})
    set_paused_option(len(ok))
    msg = "⏸ paused %d agent%s: %s" % (len(ok), "" if len(ok) == 1 else "s", names(ok))
    if held:
        # Said by name, with tmux-mcp's reason: the PAUSE is urgent, so a
        # draft no longer holds it - what does is a dialog or no prompt box,
        # and the agent keeps working until Mack deals with it (2026-10-21:
        # one waited 6 min while the banner said "still working").
        msg += " · WAITING on %s" % "; ".join(
            "%s (%s)" % (short(t["label"]), held_reason(t.get("detail", ""))) for t in held)
    if unsure:
        msg += " · %d unconfirmed (no reply; saved anyway)" % len(unsure)
    if bad:
        msg += " · FAILED %d: %s" % (len(bad), names(bad))
    msg += noid
    prog.write(True, "paused", msg)
    log(dict(entry, message=msg, targets=targets))
    return msg, True


def plan_resume(st):
    """The cheap half of a resume: where each saved agent is now.
    -> (live, gone), copies of the saved targets with resume_pane/note."""
    panes, recs = list_panes(), agent_records()
    live, gone = [], []
    for t in st["targets"]:
        pane, note = locate(t, panes, recs)
        t = dict(t, resume_pane=pane, resume_note=note)
        if pane and limit_handover(panes[pane]):
            # Rate-limited since the pause (its reply's turn hit the limit): a
            # RESUME would only spend a turn on the same error. limited.json
            # takes it over (finish_resume) and it stays paused.
            lim = parse_limit(panes[pane]["agent_limit"])
            t["limit_since"] = lim[2] if lim else 0
            t["result"] = "limited"
            t["detail"] = ("rate-limited until %s" % fmt_local(lim[0]) if lim and lim[0]
                           else "rate-limited (no reset time: only Mack can move it on)")
            live.append(t)
        elif pane:
            t["result"], t["detail"] = "sending", ""
            live.append(t)
        else:
            t["result"], t["detail"] = "gone", note
            gone.append(t)
    return live, gone


def do_resume(client, st):
    """A whole resume (Enter in the PAUSE popup). -> (message, "resume" | None)."""
    live, gone = plan_resume(st)
    msg, reached = finish_resume(client, Progress(new_press(), "resume", live, gone), st, live, gone)
    return msg, ("resume" if reached else None)


def finish_resume(client, prog, st, live, gone):
    """The slow half of a resume. -> (message line, reached anyone?)."""
    saved = st["targets"]
    entry = {"mode": "resume", "client": client, "paused_at": st.get("paused_at")}
    # A rate-limited target (plan_resume) gets no RESUME and keeps its flag.
    sendable = [(i, t) for i, t in enumerate(live) if t["result"] != "limited"]
    if sendable:
        try:
            mcp = open_mcp()
        except McpError as e:
            # Nothing was sent: keep the saved set so the next press retries.
            for t in live:
                t["result"], t["detail"] = "failed", str(e)
            msg = "▶ resume failed, still paused (%d): %s" % (len(saved), e)
            prog.write(True, "failed", msg)
            log(dict(entry, message=msg, targets=live + gone))
            return msg, False
        mark_paused([t["resume_pane"] for _, t in sendable], False)
    # A gone agent is no longer in its old pane (closed, or the pane now runs
    # another session: /clear, another /resume, a restart). If that pane is
    # still there its flag must go, or every later Stop in it reads paused.
    # The agent itself stays saved for a retry, wherever it turns up.
    if gone:
        try:
            alive = set(list_panes())
        except (OSError, RuntimeError, subprocess.SubprocessError):
            alive = set()
        mark_paused([t.get("pane_id") for t in gone if t.get("pane_id") in alive], False)
    if sendable:
        try:
            res = deliver(mcp, [(i, t["resume_pane"]) for i, t in sendable], RESUME_MSG,
                          on_result=prog.result)
        finally:
            mcp.close()
        for i, t in sendable:
            t["result"], t["detail"] = res.get(i, ("unknown", "no result"))
    ok = [t for t in live if t["result"] in REACHED]
    handed = [t for t in live if t["result"] == "limited"]
    bad = [t for t in live if t["result"] not in REACHED and t["result"] != "limited"]
    # The limited ones leave the saved set and keep their flag, rewritten to
    # their limit's <since> (the indicator clears only that value on the
    # turn's outcome). limited.json resumes them once their limit lifts: the
    # merge, and the LIMIT text for any that already has, run once the
    # pause lock is released (limit_after_press), so no press waits on them.
    for t in handed:
        if t.get("limit_since"):
            mark_paused([t["resume_pane"]], True, stamp=t["limit_since"])
    LIMIT_LATER.append({"handed": [t["resume_pane"] for t in handed], "resumed_any": bool(ok),
                        "paused_at": st.get("paused_at")})
    # Only the agents the RESUME reached leave the saved set, so a resume that
    # reached nobody can never turn into a fresh pause. A miss (failed or
    # gone) is kept until it has missed MISS_DROP times AND for MISS_AGE s
    # (see the constants), then dropped so it cannot wedge resume mode.
    now = time.time()
    for t in bad + gone:
        t["miss_count"] = int(t.get("miss_count") or 0) + 1
        t["first_miss_at"] = float(t.get("first_miss_at") or now)
    dropped = [t for t in bad + gone
               if t["miss_count"] >= MISS_DROP and now - t["first_miss_at"] >= MISS_AGE]
    for t in dropped:
        t["dropped"] = True
        t["detail"] += " (missed %d times: dropped)" % t["miss_count"]
    keep = [t for t in bad + gone if not t.get("dropped")]
    # A refused RESUME leaves that agent paused: its flag goes back on (a gone
    # one has no pane to flag; a dropped one is no longer paused at all).
    mark_paused([t["resume_pane"] for t in bad if not t.get("dropped")], True, repause=True)
    attempts = int(st.get("resume_attempts") or 0) + 1
    write_json(LAST, dict(st, resumed_at=now_iso(), resume=live + gone,
                          still_paused=len(keep), resume_attempts=attempts))
    if keep:
        strip = ("resume_pane", "resume_note", "result", "detail")
        write_json(STATE, dict(st, resume_attempts=attempts,
                               targets=[{k: v for k, v in t.items() if k not in strip} for t in keep]))
    else:
        try:
            STATE.unlink()
        except FileNotFoundError:
            pass
    set_paused_option(len(keep))
    if ok:
        msg = "▶ resumed %d agent%s: %s" % (len(ok), "" if len(ok) == 1 else "s", names(ok))
    elif handed:
        msg = "▶ nothing resumed"
    else:
        msg = "▶ resume reached no agents"
    if handed:
        msg += " · " + "; ".join("%s %s" % (short(t["label"]), t["detail"]) for t in handed[:4])
    unsure = [t for t in ok if t["result"] == "unknown"]
    if unsure:
        msg += " · %d unconfirmed (no reply)" % len(unsure)
    if gone:
        msg += " · gone %d: %s" % (len(gone), names(gone))
    if dropped:
        msg += " · %d dropped after %d misses" % (len(dropped), max(t["miss_count"] for t in dropped))
    if bad:
        msg += " · FAILED %d: %s" % (len(bad), names(bad))
    if keep:
        msg += " · %d still paused — press prefix P again to retry" % len(keep)
    # Nobody reached: the open popup shows NOT RESUMED, never a green claim.
    # A hand-over to limited.json is not a failure: they left the saved set.
    prog.write(True, "resumed" if ok or handed else "not_resumed", msg)
    log(dict(entry, message=msg, targets=live + gone))
    return msg, bool(ok or handed)


def toggle(client, resume_only=False):
    """The key. Under the lock: the cheap plan, progress.json, the popup
    (launched, not waited for), then delivery and the final state. A second
    press while this one delivers finds the lock held ("busy"), so it can
    never read a half-made pause as a resumable set. resume_only (the
    sidebar's click, `resume`): with no saved set it does nothing, so a click
    on a banner that just went stale can never turn into a fresh pause."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lf = open(LOCK, "a")
    popup = None
    try:
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            display(client, "⏸ pause key busy — the last press is still sending", "warn")
            log({"mode": "busy", "client": client})
            return
        st = load_state()
        press = new_press()
        if st and st["targets"]:
            live, gone = plan_resume(st)
            prog = Progress(press, "resume", live, gone)
            popup = open_popup(client, "resume", press)
            msg = finished(prog, lambda: finish_resume(client, prog, st, live, gone))
        elif resume_only:
            msg = "▶ nothing is paused — nothing to resume"
            log({"mode": "resume", "client": client, "message": msg, "targets": []})
        else:
            targets, unidentified = plan_pause()
            if not targets:
                msg = "⏸ no agents are working — nothing paused" + noid_text(unidentified)
                log({"mode": "pause", "client": client, "unidentified": unidentified,
                     "message": msg, "targets": []})
            else:
                prog = Progress(press, "pause", targets,
                                [dict(u, result="unidentified") for u in unidentified])
                popup = open_popup(client, "pause", press)
                msg = finished(prog, lambda: finish_pause(client, prog, targets, unidentified))
    finally:
        lf.close()   # releases the flock
    # The line too whenever the popup is NOT still up: it never opened, or
    # Mack pressed Esc while it was sending - then nothing else would tell him.
    if not popup_still_up(popup):
        display(client, msg, "warn" if "WAITING on " in msg else "info")
    # Then a resume's limit half (RATE LIMITS), outside the lock (a pause
    # press meanwhile is not turned away as busy) and after the resume's own
    # toast (its staggers must not hold that up): its outcome, a toast of its own.
    tail = limit_after_press()
    if tail:
        display(client, tail.lstrip(" ·"))


def finished(prog, finish):
    """Run a press's slow half. If it raises, mark the press done first, so
    the open popup stops spinning "sending" and shows the error (a pause
    whose paused.json did get written still counts as paused), then let the
    error reach main's handler (log + message line)."""
    try:
        return finish()[0]
    except BaseException as e:
        err = "pause key error: %s: %s (log: %s)" % (type(e).__name__, e, LOG)
        outcome = "paused" if prog.mode == "pause" and load_state() else "failed"
        prog.write(True, outcome, err)
        raise


# ------------------------------------------------- dismiss and void
# After the lid opens (Mack, 2026-10-21): the notch's strong cyan "all paused"
# ring can be DISMISSED without resuming anyone - the saved set, the sidebar's
# banner and every paused row stay - and the whole pause can be VOIDED: the
# set is forgotten and nobody is messaged, so the next press is a fresh PAUSE,
# never a RESUME of them. The agents themselves stay paused (Mack,
# 2026-10-28: a void cancels the resume, not the pause): @agent_paused and the
# `paused` state are left, and @agent_pause_voided marks the flag an orphan
# that only Mack's own prompt into that window ends (the tab hook's PAUSE
# VOIDED). Both run under the toggle's lock, so they never interleave with a
# press.

def under_lock(client, fn):
    """fn() under the press lock -> its message, or the "busy" line."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a") as lf:
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return "⏸ pause key busy — the last press is still sending"
        return fn()


def post_state_change():
    """The sidebar and CuaNotch redraw on this instead of their next poll."""
    try:
        subprocess.run(["notifyutil", "-p", "com.mackhaymond.tmux.agent-state"],
                       capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def dismiss_glow(client):
    """Turn off the notch's "all paused" ring: `glow_dismissed_at` in
    paused.json, until the set is resumed, voided or replaced. Nothing else
    changes. -> message."""
    def run():
        st = load_state()
        if not st or not st["targets"]:
            return "⏸ nothing is paused — no glow to dismiss"
        if not st.get("glow_dismissed_at"):
            write_json(STATE, dict(st, glow_dismissed_at=now_iso()))
            post_state_change()
        log({"mode": "dismiss", "client": client, "paused_at": st.get("paused_at")})
        return "⏸ glow dismissed — %d still paused (prefix P resumes)" % len(st["targets"])
    return under_lock(client, run)


def mark_voided(panes):
    """@agent_pause_voided = <epoch> on each pane's window that still carries
    the pause key's flag (docs/agent-pause.md, "Dismiss and void"): that flag
    is an orphan now, which no RESUME will ever clear, so the tab hook drops
    both on Mack's own next prompt there. Never on a rate-limited window: its
    flag is the limit's (RATE LIMITS), resumed by limited.json. The test is
    tmux's own, in the same call as the write. One call per pane, as
    mark_paused; cosmetic, so it never raises."""
    now = str(int(time.time()))
    try:
        for p in [p for p in dict.fromkeys(panes) if p]:
            tmux("if-shell", "-F", "-t", p, "#{?#{@agent_limit},0,#{?#{@agent_paused},1,0}}",
                 "set-option -w -t %s @agent_pause_voided %s" % (p, now), check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def void_pause(client):
    """Forget the saved set without messaging anyone: paused.json goes, the
    record is kept in paused.last.json as `voided_at`, and the next press is
    a fresh PAUSE. The agents stay PAUSED: no @agent_paused or @agent_state
    is touched, so every surface still shows them paused; each flagged window
    gets @agent_pause_voided (mark_voided) instead, which Mack's own prompt
    there ends. -> message, or None when an open PAUSE popup will say it:
    its VOIDED view. A line then would be a tmux message wherever no sidebar
    shows the client, and tmux 3.7c draws no popup frame under one (see
    open_popup), so VOIDED (3 s) would never show behind the 6 s line
    (2026-10-22 review)."""
    def run():
        st = load_state()
        if not st or not st["targets"]:
            return "⏸ nothing is paused — nothing to void"
        try:
            panes, recs = list_panes(), agent_records()
            found = [locate(t, panes, recs) for t in st["targets"]]
        except (OSError, RuntimeError, subprocess.SubprocessError):
            panes, found = None, []
        # Where each agent is now, else its old pane while that is still
        # there (a flagged window whose record is gone is still paused). An
        # old pane that now runs ANOTHER agent (locate's "another session" /
        # "something else") is not the paused one: its flag goes, as the
        # resume clears a gone target's pane, or that unrelated conversation
        # would show paused for good. Never a limited one's: that flag is
        # its own limit's.
        where, others = [], []
        if panes is not None:
            for t, (p, note) in zip(st["targets"], found):
                old = t.get("pane_id") if t.get("pane_id") in panes else None
                if p:
                    where.append(p)
                elif old and ("another session" in note or "something else" in note):
                    others.append(old)
                elif old:
                    where.append(old)
        mark_paused(others, False, unless_limited=True)
        mark_voided(where)
        # still_paused: the windows still flagged (a limited one too: it
        # stays paused, by its limit). Unknown (tmux failed): all of them.
        if panes is None:
            n = len(st["targets"])
        else:
            n = len({panes[p]["window_id"] for p in where if panes[p]["agent_paused"]})
        write_json(LAST, dict(st, voided_at=now_iso(), still_paused=n))
        try:
            STATE.unlink()
        except FileNotFoundError:
            pass
        set_paused_option(0)
        post_state_change()
        msg = "⏸ pause voided: %d agent%s paused, won't be resumed" % (
            n, " stays" if n == 1 else "s stay")
        log({"mode": "void", "client": client, "paused_at": st.get("paused_at"), "message": msg,
             "still_paused": n, "targets": st["targets"]})
        return None if pause_view_open() else msg
    return under_lock(client, run)


# --------------------------------------------------------- rate limits
# RATE LIMITS (docs/agent-pause.md, "Rate-limited agents"). An agent whose
# turn hit a usage limit is a PAUSED agent with a reason, not a second state
# machine: the tab indicator (the watcher, for codex) writes @agent_paused,
# @agent_limit "<resets_at> <type> <since>" and @agent_state paused, then
# runs `limit-add <window>` in the background. limited.json is that set,
# apart from the pause key's paused.json. `limit-tick`, which the watcher
# spawns when @agent_limit_due comes, cswap switches account or the machine
# wakes, sends each one LIMIT_RESUME_MSG once its limit has lifted.
#
# Its own lock (limit.lock), never the pause key's: a press must not wait
# behind a staggered delivery, and nothing here ever holds both. A toggle's
# resume only decides, under the pause lock, which saved targets are handed
# over; the merge and any LIMIT text run after it lets go (limit_after_press).
# Nor is the pause lock ever probed: the key takes it LOCK_NB, so even a brief
# hold here would turn Mack's press away as busy. A press in flight is read
# from progress.json instead (pause_in_flight).

LIMITED = STATE_DIR / "limited.json"
LIMIT_LOCK = STATE_DIR / "limit.lock"
# codexbar-usage-status.sh's published cache (its "The published cache"):
# the root is Claude's block, with `account`, `updated_at` (epoch s; moves
# with any reading), `fetched_at` (our own endpoint fetch only) and, per
# window family (session / weekly / scoped), `<family>_used` (percent, or
# null) and `<family>_locked`.
USAGE = HOME / ".cache/codexbar-tmux/usage.json"
SWAP_SEQ = HOME / ".claude-swap-backup/sequence.json"   # cswap rewrites it on a switch
LIMIT_RESUME_MSG = (
    "[limit] Your usage limit has reset. Continue the task you were working on when the "
    "limit was reached; do not repeat work that is already complete.")
# s after resets_at before the wall-clock rule fires: Claude's own
# auto-continue comes at reset + 30-90 s, and it must win (no double prompt).
LIMIT_GRACE = 120
WAKE_GRACE = 30          # s after a wake or boot: the network is still coming up
DNS_TIMEOUT = 2.0
LIMIT_MAX_ATTEMPTS = 3   # RESUMEs before a target waits for Mack (lifted, one notify)
LIMIT_BACKOFF = 120      # s after attempt n before the next: LIMIT_BACKOFF * 2**(n-1)
LIMIT_RETRY = 60         # s to the next tick when something due could not be acted on
LIMIT_STAGGER = float(os.environ.get("AGENT_PAUSE_LIMIT_STAGGER") or 5)
# A RESUME tmux-mcp HELD (a draft or dialog in the box) sits in its per-pane
# queue, undeduplicated, until the box clears or for up to 2 h (its
# HOLD_MAX_MS). It counts as reached and is never sent again while a copy may
# be waiting; one still not landed after LIMIT_HELD_LIFT s is lifted (one
# notify) for Mack instead.
LIMIT_HELD_MAX = 2 * 3600
LIMIT_HELD_LIFT = 600
# A re-mark more than this long after our last RESUME is a new limit episode,
# not that RESUME's false lift (which re-marks within minutes): its attempt
# count starts over.
LIMIT_EPISODE_GAP = 900
# A cswap switch lands before usage.json describes the new account (the
# publisher catches up on its status tick, every 15 s), so the watcher's one
# tick on the switch sees nothing yet. While a switch is newer than a
# target's since and usage.json is not, look again every LIMIT_SWITCH_POLL s,
# for at most LIMIT_SWITCH_WAIT s after the switch.
LIMIT_SWITCH_POLL = 20
LIMIT_SWITCH_WAIT = 300
SCOPED_TYPES = ("seven_day_opus", "seven_day_sonnet")
# A resume press's limit half, queued under the pause lock by finish_resume
# and run by limit_after_press once the lock is released.
LIMIT_LATER = []


def clock():
    """Now, for the limit engine. AGENT_PAUSE_NOW (epoch s) fixes it (tests)."""
    try:
        return float(os.environ.get("AGENT_PAUSE_NOW") or time.time())
    except ValueError:
        return time.time()


def last_wake():
    """The last wake or boot (epoch s): the later of kern.waketime and
    kern.boottime, the pair the sidebar reads for "ready to resume". 0 when
    neither can be read. AGENT_PAUSE_WAKETIME replaces both (tests)."""
    v = os.environ.get("AGENT_PAUSE_WAKETIME")
    if v:
        try:
            return float(v)
        except ValueError:
            return 0.0
    best = 0.0
    for key in ("kern.waketime", "kern.boottime"):
        try:   # "{ sec = 1761500000, usec = 0 } Sun Oct 26 ..."
            out = subprocess.run(["sysctl", "-n", key], capture_output=True, text=True,
                                 timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        m = re.search(r"\bsec\s*=\s*(\d+)", out)
        if m:
            best = max(best, float(m.group(1)))
    return best


def resolves(host):
    """Does `host` resolve within DNS_TIMEOUT? getaddrinfo has no timeout of
    its own, so it runs in a daemon thread that is abandoned if slow.
    AGENT_PAUSE_DNS: "ok", "fail", or a comma list of the hosts that resolve
    (tests)."""
    v = os.environ.get("AGENT_PAUSE_DNS")
    if v:
        return v == "ok" or host in v.split(",")
    box = []

    def look():
        try:
            socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
            box.append(True)
        except (OSError, UnicodeError):
            pass
    th = threading.Thread(target=look, daemon=True)
    th.start()
    th.join(DNS_TIMEOUT)
    return bool(box)


def limit_host(t):
    return "chatgpt.com" if "codex" in (t.get("type"), t.get("kind")) else "api.anthropic.com"


def parse_limit(v):
    """@agent_limit "<resets_at> <type> <since>" -> (resets_at, type, since), or
    None. resets_at 0: unknown (a model limit, never auto-resumed). Pure."""
    parts = (v or "").split()
    if len(parts) != 3:
        return None
    try:
        return int(float(parts[0])), parts[1], int(float(parts[2]))
    except ValueError:
        return None


def fmt_local(ts):
    """A time as Mack reads it, the one format on every surface (toasts,
    refusals, the log, status/dry-run), by calendar days in local time:
    today "5:31 PM", the next six days "Tue 5:31 PM", anything else (a week
    or more on, or past) "Nov 2 5:31 PM". A bare weekday for a reset 6 days
    and 20 hours out read as today. No leading zeros."""
    if not ts:
        return "unknown"
    dt = datetime.fromtimestamp(ts)
    hm = "%d:%02d %s" % (dt.hour % 12 or 12, dt.minute, "AM" if dt.hour < 12 else "PM")
    days = (dt.date() - datetime.fromtimestamp(clock()).date()).days
    if days == 0:
        return hm
    if 1 <= days <= 6:
        return "%s %s" % (dt.strftime("%a"), hm)
    return "%s %d %s" % (dt.strftime("%b"), dt.day, hm)


def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def read_json(path):
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def swap_facts():
    """-> (sequence.json's mtime or 0, usage.json's root or {})."""
    try:
        seq = SWAP_SEQ.stat().st_mtime
    except OSError:
        seq = 0.0
    return seq, read_json(USAGE) or {}


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def limit_family(typ, usage):
    """The usage.json window family a limit of this type counts against:
    "session" (five_hour), "scoped" (seven_day_opus/sonnet, while usage.json
    has a scoped reading), else "weekly". Pure."""
    if typ == "five_hour":
        return "session"
    if typ in SCOPED_TYPES and is_num(usage.get("scoped_used")):
        return "scoped"
    return "weekly"   # seven_day, overage, anything else, or no scoped reading


def window_free(fam, seq_mtime, usage):
    """Is that window free on the account usage.json now describes? Its
    `<fam>_locked` is false AND either its `<fam>_used` is a number under 100,
    or (no reading) our own fetch landed after the switch. The locked flags
    alone prove nothing right after a switch: adopting cswap's reading resets
    them to false and leaves fetched_at behind, so a switch to an exhausted
    account would pass. Pure."""
    if usage.get(fam + "_locked") is not False:
        return False
    used = usage.get(fam + "_used")
    if is_num(used):
        return used < 100
    return num(usage.get("fetched_at")) > seq_mtime


def swap_freed(t, seq_mtime, usage):
    """Did a cswap switch free this target? All four: a switch after it was
    marked (sequence.json newer than `since`), usage.json written after that
    switch, another account than the one it hit the limit on, and that
    account's matching window free (window_free). A false lift re-marks it
    with a new `since`, so only a NEWER switch can free it again. Pure."""
    acct = usage.get("account")
    return (t.get("type") != "codex" and seq_mtime > num(t.get("since"))
            and num(usage.get("updated_at")) > seq_mtime
            and bool(acct) and bool(t.get("account")) and acct != t.get("account")
            and window_free(limit_family(t.get("type"), usage), seq_mtime, usage))


def switch_settling(t, now, seq_mtime, usage):
    """Is a cswap switch that may free this target still waiting on
    usage.json? The switch is newer than its since, usage.json not newer
    than the switch, and the switch under LIMIT_SWITCH_WAIT s old (a
    usage.json that never settles stops the polling). Regardless of
    auto-resume: with it off, the tick must still see the lift to mark it.
    Not for a target already waiting for Mack, a codex one or a model
    limit (neither is freed by a switch). Pure."""
    return (not t.get("lifted_at") and t.get("type") != "codex"
            and int(num(t.get("resets_at"))) > 0
            and seq_mtime > num(t.get("since")) and num(usage.get("updated_at")) <= seq_mtime
            and now < seq_mtime + LIMIT_SWITCH_WAIT)


def limit_eligible(t, now, seq_mtime, usage):
    """Has this target's limit lifted? -> "lifted" (it was, and nothing has
    re-marked it since), "reset" (resets_at + LIMIT_GRACE has passed),
    "switch" (swap_freed), or "" (not yet). resets_at 0 is never eligible;
    codex only by the clock. Pure."""
    resets = int(num(t.get("resets_at")))
    if t.get("lifted_at"):
        return "lifted"
    if resets <= 0:
        return ""
    if now >= resets + LIMIT_GRACE:
        return "reset"
    return "switch" if swap_freed(t, seq_mtime, usage) else ""


def capped(t):
    return int(num(t.get("attempts"))) >= LIMIT_MAX_ATTEMPTS


def backoff_until(t):
    """The earliest next RESUME after `attempts` of them. Pure."""
    n = int(num(t.get("attempts")))
    return num(t.get("last_attempt")) + (LIMIT_BACKOFF * 2 ** (n - 1) if n > 0 else 0)


def held_pending(t, now):
    """May a RESUME tmux-mcp held for this target still be queued? Pure."""
    return bool(t.get("held_at")) and now - num(t["held_at"]) < LIMIT_HELD_MAX


def held_stuck(t, row, now):
    """A RESUME that went out held (or with no reply) and has not landed:
    LIMIT_HELD_LIFT s on, the window is neither running nor paused again.
    It is lifted for Mack, never resent. Pure."""
    return (t.get("last_result") in ("held", "unknown") and not t.get("lifted_at")
            and bool(row) and row["agent_state"] != "running" and not row["agent_paused"]
            and now - num(t.get("last_attempt")) >= LIMIT_HELD_LIFT)


def limit_due_at(targets, auto, now, actionable=None, eligible=()):
    """The earliest moment limit-tick could act on any target (epoch s), or
    None. Not counted: a target waiting for Mack (lifted with auto-resume off
    or past the cap), one with resets_at 0 and nothing lifted, and, when
    `actionable` (a set of window ids) is given, one not actionable now (its
    RESUME's turn running, Mack moved it on): only a re-mark changes that,
    and limit-add recomputes then. A target in `eligible` (window ids whose
    limit has lifted already, e.g. by a switch) waits only for its backoff,
    never for its reset; one whose held RESUME may still be queued waits for
    that to expire. Pure."""
    best = None
    for t in targets:
        wid = t.get("window_id")
        if actionable is not None and wid not in actionable:
            continue
        if t.get("lifted_at"):
            if not auto or capped(t):
                continue
            at = backoff_until(t)
        elif wid in eligible:
            at = backoff_until(t)
        elif int(num(t.get("resets_at"))) <= 0:
            continue
        else:
            at = max(int(num(t["resets_at"])) + LIMIT_GRACE, backoff_until(t))
        if held_pending(t, now):
            at = max(at, num(t["held_at"]) + LIMIT_HELD_MAX)
        best = at if best is None else min(best, at)
    return best


def limit_actionable(row, loose=False):
    """May the engine message this window now? Only while it is paused, in
    the paused set (@agent_paused) and still limited. loose (Mack's own
    limit-resume): limited and not running is enough, so an agent left idle
    by a RESUME that never landed can still be resumed by hand. Pure."""
    if loose:
        return bool(row and row["agent_limit"] and row["agent_state"] != "running")
    return bool(row and row["agent_state"] == "paused" and row["agent_paused"] and row["agent_limit"])


def limit_handover(row):
    """Does a toggle's resume hand this saved target to limited.json instead
    of sending it the pause RESUME? Only while it is limited and NOT running:
    one running a [limit] turn is resumed like any running target, or its
    Stop would clear the limit and leave it paused in neither set. Pure."""
    return bool(row["agent_limit"]) and row["agent_state"] != "running"


def pause_in_flight():
    """Is a pause press delivering right now? Its paused.json is written only
    after delivery, so progress.json says it: mode pause, not done, and its
    toggle still alive (a killed one never holds this)."""
    prog = read_progress()
    return bool(prog and prog.get("mode") == "pause" and not prog.get("done")
                and press_alive(prog.get("press")) and press_age(prog.get("press")) < PRESS_MAX_AGE)


PRESS_MAX_AGE = 600   # s: no press delivers this long; an older "in flight" is a reused pid


def press_age(press):
    """Seconds since a press began (real time, not clock()): its id's
    "<ms>-" stamp, else progress.json's write time; 0 when neither reads."""
    try:
        return time.time() - int(str(press).split("-", 1)[0]) / 1000.0
    except ValueError:
        try:
            return time.time() - PROGRESS.stat().st_mtime
        except OSError:
            return 0.0


def manual_pause_active(allow=None):
    """A manual pause the limit engine must not cut across: a pause press in
    flight, or a saved set in paused.json. allow: True ignores the saved set
    (Mack's own limit-resume); a paused_at string ignores only that set (a
    resume's leftover misses)."""
    if pause_in_flight():
        return True
    if allow is True:
        return False
    st = load_state()
    return bool(st and st["targets"]) and (allow is None or st.get("paused_at") != allow)


def limit_autoresume():
    """@agent_limit_autoresume (global; agent-pause.conf sets it on). On
    unless it says off."""
    try:
        v = tmux("show-options", "-gqv", "@agent_limit_autoresume", check=False)
    except (OSError, subprocess.SubprocessError):
        v = ""
    return v.strip().lower() not in ("off", "0", "false", "no")


def set_global(opt, val):
    """A global option, or unset with val None. Cosmetic: never raises."""
    try:
        if val is None:
            tmux("set-option", "-gu", opt, check=False)
        else:
            tmux("set-option", "-g", opt, str(val), check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def set_window_opt(target, opt, val):
    """A window option on the window of `target` (a pane id), or unset with
    val None. Cosmetic: never raises."""
    try:
        if val is None:
            tmux("set-option", "-wu", "-t", target, opt, check=False)
        else:
            tmux("set-option", "-w", "-t", target, opt, str(val), check=False)
    except (OSError, subprocess.SubprocessError):
        pass


@contextlib.contextmanager
def limit_lock(wait):
    """limit.lock, held for as long as limited.json is read, changed and
    written. Waits up to `wait` s for a holder; yields whether it got it."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lf = open(LIMIT_LOCK, "a")
    try:
        deadline = time.time() + wait
        while True:
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError:
                if time.time() >= deadline:
                    held = False
                    break
                time.sleep(0.1)
        yield held
    finally:
        lf.close()


def load_limited():
    try:
        d = json.loads(LIMITED.read_text())
    except FileNotFoundError:
        d = None
    except (OSError, ValueError) as e:
        log({"mode": "warn", "message": "unreadable %s: %s" % (LIMITED, e)})
        d = None
    if not (isinstance(d, dict) and isinstance(d.get("targets"), list)):
        d = {"version": 1, "targets": []}
    return d


def save_limited(d):
    write_json(LIMITED, dict(d, version=1))


def limit_where(t, panes, recs, wins):
    """Where a limited target is now -> (row, pane to message). The pane is
    located like a paused one (locate: the session id, after a restore too);
    when it cannot be (a record lagging), the saved window still answers for
    the options, with no pane to message: (row, None). (None, None): gone."""
    pane, _note = locate(t, panes, recs)
    if pane:
        row = panes[pane]
        t.update(pane_id=pane, window_id=row["window_id"], location=row["location"])
        return row, pane
    rows = wins.get(t.get("window_id"))
    return (rows[0] if rows else None), None


def limit_target(w, panes, recs):
    """A limited window `w` (window id, or one of its panes) as a target ->
    (target, row) or (None, why)."""
    wid = panes[w]["window_id"] if w in panes else w
    rows = window_rows(panes).get(wid)
    if not rows:
        return None, "no window %s" % w
    head = rows[0]
    lim = parse_limit(head["agent_limit"])
    if not lim:
        return None, "%s is not rate-limited (no @agent_limit)" % w
    pick, _how = pick_agent_pane(rows, recs)
    t = agent_target(pick or head, head, recs)
    t.update(resets_at=lim[0], type=lim[1], since=lim[2])
    return t, (pick or head)


def limit_renew(old, resets, typ, since, account):
    """A stored target's limit from a (re-)mark. A re-mark updates resets_at,
    type, since and account and keeps attempts (each RESUME counted itself
    when it went out). A re-mark with a new since is a new limit: no longer
    lifted, its RESUME (if any) spent. One more than LIMIT_EPISODE_GAP after
    our last RESUME is not that RESUME's false lift but a new episode, so the
    attempt count starts over: a cap reached hours ago must not cap it.
    Any re-mark also drops held_at: a turn has run since, so the held copy
    has landed (or is no reason to hold back a new episode's resume).
    account: the account at mark time; None keeps the stored one.
    -> renewed?"""
    renewed = old.get("since") != since or old.get("resets_at") != resets
    last = num(old.get("last_attempt")) or num(old.get("resumed_at"))
    if renewed and last and since > last + LIMIT_EPISODE_GAP:
        old["attempts"] = 0
        for k in ("last_result", "last_attempt"):
            old.pop(k, None)
    old.update(resets_at=resets, type=typ, since=since)
    if account:
        old["account"] = account
    if renewed:
        for k in ("lifted_at", "resumed_at", "held_at"):
            old.pop(k, None)
    return renewed


def limit_merge(d, t, account):
    """Merge one marked target into limited.json's set, by window or session,
    renewing its limit (limit_renew). Any other entry for the same window or
    session is pruned: its episode has ended, this mark replaced it. An entry
    whose window now runs ANOTHER session starts over as a new target.
    -> (the stored target, renewed?)."""
    sid = t["session_id"]
    matches = [o for o in d["targets"]
               if o.get("window_id") == t["window_id"] or (sid and o.get("session_id") == sid)]
    old = next((o for o in matches if sid and o.get("session_id") == sid), matches[0] if matches else None)
    gone = {id(o) for o in matches if o is not old}
    if old is not None and sid and old.get("session_id") and old["session_id"] != sid:
        gone.add(id(old))   # another agent in that window: a new target
        old = None
    d["targets"] = [o for o in d["targets"] if id(o) not in gone]
    if old is None:
        t = dict(t, account=account, attempts=0)
        d["targets"].append(t)
        return t, True
    lim = (t.pop("resets_at"), t.pop("type"), t.pop("since"))
    old.update(t)   # where it lives now
    return old, limit_renew(old, *lim, account)


def limit_sync(t, row, account):
    """The live @agent_limit on its window wins over the stored one, so a
    limit-add that never ran cannot leave a stale since (the cswap rule and
    the flag stamp both read it). The stored account stays: it is the one
    the limit was hit on, and `account` (now's) only fills a target that has
    none. -> renewed?"""
    lim = parse_limit(row["agent_limit"]) if row else None
    if not lim or (lim[0], lim[1], lim[2]) == (t.get("resets_at"), t.get("type"), t.get("since")):
        return False
    renewed = limit_renew(t, lim[0], lim[1], lim[2], None if t.get("account") else account)
    if renewed and row["agent_limit_lifted"]:
        set_window_opt(row["pane_id"], "@agent_limit_lifted", None)
    return renewed


def limit_add_locked(w, panes, recs, d):
    """limit-add with limit.lock held and limited.json loaded -> (message,
    target or None). Saves the file and recomputes @agent_limit_due."""
    t, row = limit_target(w, panes, recs)
    if t is None:
        return row, None
    t, renewed = limit_merge(d, t, swap_facts()[1].get("account"))
    if renewed and row["agent_limit_lifted"]:
        set_window_opt(t["pane_id"], "@agent_limit_lifted", None)
    save_limited(d)
    limit_recompute(d, floor=clock())   # already due: the next tick, at once
    return "limited: %s until %s (%s)" % (short(t["label"]), fmt_local(t["resets_at"]), t["type"]), t


def limit_add(w):
    """`limit-add W`: the indicator just marked W. Waits for a tick in flight
    (it is a background job; a lost add would leave the agent unresumed)."""
    with limit_lock(180) as held:
        if not held:
            msg = "limit-add %s: limit.lock busy for 180 s, not added" % w
        else:
            msg, _t = limit_add_locked(w, list_panes(), agent_records(), load_limited())
    log({"mode": "limit-add", "window": w, "message": msg})
    return msg


def limit_recompute(d, floor=None):
    """@agent_limit_due from limited.json as it stands: the earliest moment
    anything could be done, unset when nothing can. A moment that has passed
    (something due could not be acted on) becomes `floor` (default: now +
    LIMIT_RETRY), so the watcher does not respawn a tick every 20 s. Reads
    tmux afresh: a delivery just changed the windows it judges."""
    try:
        panes, recs = list_panes(), agent_records()
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None   # leave the option as it is
    wins = window_rows(panes)
    now = clock()
    seq_mtime, usage = swap_facts()
    # extra: moments limit_due_at does not know of (they need the window rows).
    actionable, eligible, extra = set(), set(), []
    for t in d["targets"]:
        row, _pane = limit_where(dict(t), panes, recs, wins)   # a copy: judged, not moved
        if limit_actionable(row):
            actionable.add(t.get("window_id"))
            if limit_eligible(t, now, seq_mtime, usage):
                eligible.add(t.get("window_id"))
            elif switch_settling(t, now, seq_mtime, usage):
                extra.append(now + LIMIT_SWITCH_POLL)   # usage.json has not caught up yet
        elif (t.get("last_result") in ("held", "unknown") and not t.get("lifted_at") and row
              and row["agent_state"] != "running" and not row["agent_paused"]):
            extra.append(num(t.get("last_attempt")) + LIMIT_HELD_LIFT)   # held_stuck's moment
    at = limit_due_at(d["targets"], limit_autoresume(), now, actionable, eligible)
    if extra:
        at = min(extra) if at is None else min(at, min(extra))
    if at is not None and at <= now:
        at = floor if floor is not None else now + LIMIT_RETRY
    set_global("@agent_limit_due", None if at is None else int(at))
    return at


LIMIT_REACHED = ("sent", "held", "unknown")   # as REACHED: a held RESUME will be delivered


def limit_send(mcp, t, pane):
    """LIMIT_RESUME_MSG to one limited target, recorded in t. @agent_paused
    goes first (as finish_resume does: the v1.1 hold rule then lets the paste
    make it running, @agent_limit kept until the turn's outcome). Never
    urgent: a half-typed draft must hold it.
      sent / unknown: reached.
      held: reached too, and the flag stays off. tmux-mcp delivers it once the
        box clears; it is never sent again while a copy may be queued
        (held_pending), and one that has not landed LIMIT_HELD_LIFT s on is
        lifted for Mack (held_stuck).
      refused / failed: the flag goes back on, stamped with the limit's own
        since (the indicator clears only that value), retried after the
        backoff.
    -> (status, detail)."""
    mark_paused([pane], False)
    res = deliver(mcp, [(0, pane)], LIMIT_RESUME_MSG).get(0, ("unknown", "no result"))
    at = clock()
    t["attempts"] = int(num(t.get("attempts"))) + 1
    t["last_attempt"] = at
    t["last_result"] = res[0]
    if res[0] in LIMIT_REACHED:
        t["resumed_at"] = at
        if res[0] == "held":
            t["held_at"] = at
        elif res[0] == "sent":
            t.pop("held_at", None)   # pasted: the box was clear, nothing older waits in it
        t.pop("lifted_at", None)
        set_window_opt(pane, "@agent_limit_lifted", None)
    else:
        mark_paused([pane], True, repause=True, stamp=t.get("since"))
    log({"mode": "limit-send", "window": t.get("window_id"), "label": t.get("label"),
         "attempt": t["attempts"], "result": res[0], "detail": res[1]})
    return res


def limit_lift(t, pane, notify=True, text=None):
    """Its limit has lifted but it waits for Mack (auto-resume off, past the
    cap, or a RESUME that never landed): @agent_limit_lifted on the window,
    lifted_at in the file, and one notify.sh per lift (lifted_at keeps it
    from repeating). text: the toast, when the usual one would mislead."""
    if t.get("lifted_at"):
        return
    at = clock()
    t["lifted_at"] = at
    set_window_opt(pane, "@agent_limit_lifted", int(at))
    if notify:
        display("", text or "limit lifted · resume? %s" % short(t.get("label", "?")))


def stuck_text(t):
    """held_stuck's toast: say why, so Mack clears the box instead of
    clicking resume (that would queue a second copy behind the first)."""
    label = short(t.get("label", "?"))
    if t.get("last_result") == "held":
        return "limit lifted · a resume is waiting behind a draft in %s's box" % label
    return "limit lifted · %s's resume got no reply: resume?" % label


def limit_recheck(pane, allow=None, loose=False):
    """Right before each delivery: "ok", "skip" (no longer actionable), or
    "stop" (a manual pause began, or a pause press is delivering: the rest
    waits for its resume). allow: see manual_pause_active; loose: see
    limit_actionable."""
    if manual_pause_active(allow):
        return "stop"
    try:
        row = list_panes().get(pane)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return "skip"
    return "ok" if limit_actionable(row, loose) else "skip"


def limit_deliver(d, sends, allow=None, loose=False, stagger_first=False):
    """sends [(target, pane)], LIMIT_RESUME_MSG to each, >= LIMIT_STAGGER s
    apart, each re-checked first; limited.json saved after every attempt.
    -> (reached [targets], failed [targets], stopped?, error or "")."""
    sent, failed = [], []
    if not sends:
        return sent, failed, False, ""
    try:
        mcp = open_mcp()
    except McpError as e:
        return sent, failed, False, str(e)   # nothing went out: no attempt counted
    try:
        for i, (t, pane) in enumerate(sends):
            if i or stagger_first:
                time.sleep(LIMIT_STAGGER)
            verdict = limit_recheck(pane, allow, loose)
            if verdict == "stop":
                return sent, failed, True, ""
            if verdict == "skip":
                continue
            res = limit_send(mcp, t, pane)
            (sent if res[0] in LIMIT_REACHED else failed).append(t)
            save_limited(d)
    finally:
        mcp.close()
    return sent, failed, False, ""


def limit_tick():
    """`limit-tick` (the watcher; flock-guarded here as well). -> its line."""
    with limit_lock(0) as held:
        if not held:
            msg = "limit-tick: another limit run holds limit.lock"
        else:
            msg = _limit_tick()
    log({"mode": "limit-tick", "message": msg})
    return msg


def _limit_tick():
    d = load_limited()
    if not d["targets"]:
        set_global("@agent_limit_due", None)
        return "nothing rate-limited"
    now = clock()
    panes, recs = list_panes(), agent_records()
    wins = window_rows(panes)
    # Drop what has moved on: a window gone, or @agent_limit cleared (a Stop
    # after its RESUME or the native auto-continue, or Mack's own prompt).
    keep, dropped, where = [], [], {}
    seq_mtime, usage = swap_facts()
    for t in d["targets"]:
        row, pane = limit_where(t, panes, recs, wins)
        if row is None or not row["agent_limit"]:
            dropped.append(t)
        else:
            limit_sync(t, row, usage.get("account"))   # the live mark wins
            keep.append(t)
            where[id(t)] = (row, pane)
    d["targets"] = keep
    note = " · dropped %d: %s" % (len(dropped), names(dropped)) if dropped else ""
    save_limited(d)

    def done(msg, floor=None):
        limit_recompute(d, floor)
        post_state_change()
        return msg + note

    if not keep:
        return done("nothing rate-limited")
    if manual_pause_active():
        # Deferred: the toggle's resume takes the lifted ones along.
        return done("deferred: a manual pause is active, or a pause press is sending")
    wake = last_wake()
    if now - wake < WAKE_GRACE:
        return done("waking: %ds since the last wake" % (now - wake), wake + WAKE_GRACE)
    due, offline, hosts, lifted = [], [], {}, []
    for t in keep:
        row, pane = where[id(t)]
        if held_stuck(t, row, now):
            # Its held (or unanswered) RESUME never landed: Mack, not a resend.
            limit_lift(t, pane or row["pane_id"], text=stuck_text(t))
            lifted.append(t)
            continue
        if pane is None or not limit_actionable(row) or not limit_eligible(t, now, seq_mtime, usage):
            continue
        h = limit_host(t)
        if h not in hosts:
            hosts[h] = resolves(h)
        (due if hosts[h] else offline).append((t, pane))
    auto = limit_autoresume()
    sends = []
    for t, pane in due:
        if not auto or capped(t):
            if not t.get("lifted_at"):
                limit_lift(t, pane)
                lifted.append(t)
        elif now >= backoff_until(t) and not held_pending(t, now):
            sends.append((t, pane))
    if lifted:
        save_limited(d)
    sent, failed, stopped, err = limit_deliver(d, sends)
    msg = "limit-tick: %d rate-limited" % len(keep)
    if sent:
        msg += " · resumed %d: %s" % (len(sent), names(sent))
    if failed:
        msg += " · not taken %d (retry): %s" % (len(failed), names(failed))
    if lifted:
        msg += " · lifted %d (waiting for Mack): %s" % (len(lifted), names(lifted))
    if offline:
        msg += " · offline: %s unresolved" % ", ".join(sorted(h for h, ok in hosts.items() if not ok))
    if stopped:
        msg += " · stopped: a manual pause began"
    if err:
        msg += " · tmux-mcp: %s" % err
    return done(msg)


def limit_auto(arg):
    """`limit-auto on|off|toggle` -> its line. The live global only;
    agent-pause.conf holds the default. Turned on with lifted targets, the
    watcher's next tick resumes them (@agent_limit_due = now)."""
    on = {"on": True, "off": False, "toggle": not limit_autoresume()}[arg]
    set_global("@agent_limit_autoresume", "on" if on else "off")
    if on and any(t.get("lifted_at") and not capped(t) for t in load_limited()["targets"]):
        set_global("@agent_limit_due", int(clock()))
    post_state_change()
    msg = "rate-limit auto-resume %s" % ("on" if on else "off")
    log({"mode": "limit-auto", "message": msg})
    return msg


def limit_resume(w, force=False):
    """`limit-resume [--force] W`: Mack's click on a limited card. Refused
    while the limit holds ("rate-limited until <t>") unless it is eligible
    (either rule, or lifted), resets_at is 0 (a model limit: only Mack can
    judge it) or --force. No wake or DNS grace, no manual-pause deferral:
    Mack asked. -> (ok, message)."""
    with limit_lock(10) as held:
        if not held:
            return False, "rate limit: busy, another limit run is sending; try again"
        panes, recs = list_panes(), agent_records()
        d = load_limited()
        t = next((x for x in d["targets"] if w in (x.get("window_id"), x.get("pane_id"))), None)
        if t is None:
            msg, t = limit_add_locked(w, panes, recs, d)   # marked, but its limit-add never ran
            if t is None:
                return False, msg
        row, pane = limit_where(t, panes, recs, window_rows(panes))
        if row is None or not row["agent_limit"]:
            return False, "%s is not rate-limited any more" % short(t.get("label", w))
        if row["agent_state"] == "running":
            return False, "%s is already running" % short(t.get("label", w))
        if pane is None:
            return False, "%s: no agent pane to message" % short(t.get("label", w))
        seq_mtime, usage = swap_facts()
        limit_sync(t, row, usage.get("account"))
        if held_pending(t, clock()) and not force:
            # tmux-mcp queues, undeduplicated: a click would add a second copy.
            return False, "a resume is already waiting behind the draft in %s's box" % short(
                t.get("label", w))
        resets = int(num(t.get("resets_at")))
        if not (force or resets <= 0 or limit_eligible(t, clock(), seq_mtime, usage)):
            if clock() >= resets:   # just reset: Claude's own auto-continue goes first
                return False, "limit just reset · auto-continue first, try after %s" % fmt_local(
                    resets + LIMIT_GRACE)
            return False, "rate-limited until %s" % fmt_local(resets)
        # Loose: limited and not running is enough (an agent a lost RESUME
        # left idle, flag off). Only a pause press in flight stops it.
        sent, failed, _stopped, err = limit_deliver(d, [(t, pane)], allow=True, loose=True)
        save_limited(d)
        limit_recompute(d)
        post_state_change()
    if err:
        return False, "resume failed: %s" % err
    if sent:
        return True, "▶ resumed %s (rate limit)" % short(t.get("label", w))
    if failed:
        return False, "%s did not take it (%s): still paused" % (short(t.get("label", w)),
                                                                t.get("last_result", "failed"))
    return False, "%s is no longer paused by the limit" % short(t.get("label", w))


def limit_after_press():
    """A resume press's limit half (LIMIT_LATER), run once the pause lock is
    released (toggle, locked_resume): a pause press meanwhile is not turned
    away as busy. -> a " · ..." tail for the toast."""
    jobs, LIMIT_LATER[:] = list(LIMIT_LATER), []
    tail = "".join(limit_after_resume(job) for job in jobs)
    if tail:
        log({"mode": "limit-after-resume", "message": tail.lstrip(" ·")})
    return tail


def limit_after_resume(job):
    """After a toggle's RESUMEs (job from finish_resume): first the saved
    targets it handed over are merged into limited.json (the indicator's own
    limit-add normally got there first; the merge is idempotent). Then the
    limited agents whose limit has lifted (lifted, or eligible by either
    rule) get LIMIT_RESUME_MSG, staggered after the RESUMEs (auto-resume on,
    not past the cap, no held copy queued), or are marked lifted with no
    message (off). No wake/DNS grace: Mack pressed resume. paused.json may
    still hold this resume's misses, which do not stop it; a new pause (in
    flight or saved) does. -> a " · ..." tail. Never raises: the press has
    already written its state."""
    try:
        if not job["handed"] and not load_limited()["targets"]:
            return ""
        with limit_lock(30) as held:
            if not held:   # a tick is sending: let the watcher's next one look
                set_global("@agent_limit_due", int(clock()))
                log({"mode": "warn", "message": "limit after resume: limit.lock busy"})
                return ""
            d = load_limited()
            panes, recs = list_panes(), agent_records()
            for p in job["handed"]:
                if p in panes:
                    limit_add_locked(p, panes, recs, d)
            wins = window_rows(panes)
            now, auto = clock(), limit_autoresume()
            seq_mtime, usage = swap_facts()
            sends, lifted = [], []
            for t in d["targets"]:
                row, pane = limit_where(t, panes, recs, wins)
                if row is not None:
                    limit_sync(t, row, usage.get("account"))
                if pane is None or not limit_actionable(row) or not limit_eligible(t, now, seq_mtime, usage):
                    continue
                if auto and not capped(t):
                    if not held_pending(t, now):
                        sends.append((t, pane))
                elif not t.get("lifted_at"):
                    limit_lift(t, pane, notify=False)   # the toast says it
                    lifted.append(t)
            save_limited(d)
            sent, failed, _stopped, err = limit_deliver(d, sends, allow=job["paused_at"],
                                                        stagger_first=job["resumed_any"])
            limit_recompute(d)
        tail = ""
        if sent:
            tail += " · limit lifted, resumed %d: %s" % (len(sent), names(sent))
        if failed or err:
            tail += " · limit-resume not taken %d%s" % (len(failed), ": " + err if err else "")
        if lifted:
            tail += " · limit lifted %d (auto-resume off): %s" % (len(lifted), names(lifted))
        return tail
    except Exception as e:
        log({"mode": "warn", "message": "limit after resume: %s: %s" % (type(e).__name__, e)})
        return ""


def limited_lines(eligibility=False):
    """status / dry-run: the limited set, one line per target."""
    targets = load_limited()["targets"]
    if not targets:
        return []
    out = ["rate-limited: %d agent%s (auto-resume %s)" % (
        len(targets), "" if len(targets) == 1 else "s", "on" if limit_autoresume() else "off")]
    seq_mtime, usage = swap_facts()
    for t in targets:
        resets = int(num(t.get("resets_at")))
        when = ("lifted" if t.get("lifted_at") else "resets %s" % fmt_local(resets) if resets
                else "resets unknown (only Mack)")
        why = limit_eligible(t, clock(), seq_mtime, usage) if eligibility else ""
        out.append("  %-6s %-6s %-18s %-24s attempts %d  %s%s" % (
            t.get("window_id", ""), t.get("kind", ""), t.get("type", ""), when,
            int(num(t.get("attempts"))), t.get("label", ""),
            "  [eligible: %s]" % why if why else ""))
    return out


# ------------------------------------------------------------- feedback
# A display-message line is too easy to miss with the lid half shut (Mack's
# call), so a press with anyone to message opens a big border-less popup
# running `watch <mode> --press <id>`, AT ONCE, before delivery: rows read
# "sending…" until progress.json fills them in, then a block-letter banner
# (PAUSING → SAFE TO CLOSE, RESUMED, or the peach NOT PAUSED / NOT RESUMED
# when nobody was reached) over one live row per agent. display-message is
# the fallback for no client or a popup that will not open.

PEACH, GREEN, INK = "#fab387", "#a6e3a1", "#11111b"
LAVENDER = "#b4befe"                          # neutral: VOIDED (not a resume, not a failure)
OVERRIDES = ("AGENT_PAUSE_STATE_DIR", "TMUX_MCP_URL", "TMUX_MCP_PORT", "TMUX_MCP_TOKEN_FILE",
             "TMUX_ASSISTANT_RESURRECT_DIR")


def popup_cmd(mode, press=None):
    """The shell command the popup runs. The popup's environment is the tmux
    server's, not ours, so our overrides ride along explicitly (Enter in the
    popup resumes, which needs the same state dir, tmux-mcp and records).
    Paths and a URL only: the token itself never goes on a command line."""
    import shlex
    env = ["%s=%s" % (k, os.environ[k]) for k in OVERRIDES if os.environ.get(k)]
    return "exec %s%s %s watch %s%s" % (
        ("env %s " % " ".join(shlex.quote(e) for e in env)) if env else "",
        shlex.quote(sys.executable), shlex.quote(str(Path(__file__).resolve())), mode,
        " --press %s" % shlex.quote(press) if press else "")


def popup_size(cw, ch):
    """~70% x 60% of the client, but never smaller than 60x16 (or the whole
    client when it is smaller than that). Pure."""
    return (min(cw, max(60, round(cw * 0.7))), min(ch, max(16, round(ch * 0.6))))


def _span(lo_hi_list):
    return min(a for a, _ in lo_hi_list), max(b for _, b in lo_hi_list)


def popup_geometry(cw, ch, client_tty, wez):
    """-> (w, h, x, y) for display-popup on a cw x ch client. Pure.

    WezTerm can hold Mack's sidebar as a native split BESIDE the tmux pane
    (wezterm.lua's strip), and a tmux popup centres on the tmux client only,
    so it sat off-centre in the window. WezTerm has no floating panes, so the
    popup is shifted instead: its centre goes to the centre of the WezTerm
    TAB (the bounding box of every pane in the tab holding this client's tty,
    from `wezterm cli list`), as far as it can while staying inside the
    client; the width shrinks to keep that centre where it can (to 70% of the
    tab, but no wider than twice the room on the nearer side), never below the
    usual minimum. x is the left column; y is "C", or, when panes are stacked
    vertically too, a number that tmux 3.7c reads as the popup's BOTTOM row
    (measured: -y N puts the top at N - h). Anything unexpected - no wezterm
    data, tty not found, odd JSON - gives the plain centred popup."""
    w, h = popup_size(cw, ch)
    try:
        me = next(p for p in wez if isinstance(p, dict) and p.get("tty_name") == client_tty)
        tab = [p for p in wez if isinstance(p, dict)
               and p.get("window_id") == me["window_id"] and p.get("tab_id") == me["tab_id"]]
        ml, mt = int(me["left_col"]), int(me["top_row"])
        left, right = _span([(int(p["left_col"]), int(p["left_col"]) + int(p["size"]["cols"])) for p in tab])
        top, bottom = _span([(int(p["top_row"]), int(p["top_row"]) + int(p["size"]["rows"])) for p in tab])
    except (StopIteration, KeyError, TypeError, ValueError):
        return w, h, "C", "C"

    def place(span_lo, span_hi, mine, size, pct, minimum):
        cx = (span_lo + span_hi) / 2.0 - mine        # tab centre, client coords
        want = min(round((span_hi - span_lo) * pct), int(2 * min(cx, size - cx)))
        n = min(size, max(want, min(minimum, size)))
        return n, int(max(0, min(size - n, round(cx - n / 2.0))))

    x, y = "C", "C"
    if (left, right) != (ml, ml + cw):           # panes beside the client
        w, x = place(left, right, ml, cw, 0.7, 60)
    if (top, bottom) != (mt, mt + ch):           # panes above/below it
        h, t = place(top, bottom, mt, ch, 0.6, 16)
        y = t + h                                # tmux's numeric -y is the bottom
    return w, h, x, y


WEZTERM_APP = "/Applications/WezTerm.app/Contents/MacOS/wezterm"


def wezterm_panes():
    """`wezterm cli list --format json`, or None. AGENT_PAUSE_WEZTERM=off
    skips it; a path there is read instead of running the CLI (tests: the real
    CLI is never run from them). Half a second at most: the popup is waiting."""
    import shutil
    src = os.environ.get("AGENT_PAUSE_WEZTERM", "")
    try:
        if src == "off":
            return None
        if src:
            return json.loads(Path(src).read_text())
        exe = shutil.which("wezterm") or (WEZTERM_APP if os.access(WEZTERM_APP, os.X_OK) else None)
        if not exe:
            return None
        # --no-auto-start: with no GUI to find (ssh, WezTerm quit, a stale
        # socket) plain `cli list` would spawn a wezterm-mux-server daemon.
        r = subprocess.run([exe, "cli", "--no-auto-start", "list", "--format", "json"],
                           capture_output=True, text=True, timeout=0.5)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def popup_enabled():
    """The popup is opt-in (Mack, 2026-10-19): the sidebar's pause banner
    and CuaNotch's glow are the status, and a press with the popup off says
    its outcome as a toast. On with the tmux option `@agent_pause_popup on`
    (agent-pause.conf); AGENT_PAUSE_POPUP overrides it (tests)."""
    v = os.environ.get("AGENT_PAUSE_POPUP")
    if v is None:
        try:
            v = tmux("show-options", "-gqv", "@agent_pause_popup", check=False)
        except (OSError, subprocess.SubprocessError):
            v = ""
    return v.strip().lower() in ("on", "1", "true", "yes")


def open_popup(client, mode, press):
    """Launch the popup for this press and return its process, without
    waiting: display-popup blocks until the popup closes, and the popup must
    be up while delivery runs. Its own session, so it outlives the toggle.
    Big, border-less, on a solid tint; positioned by popup_geometry."""
    if not client or not popup_enabled():
        return None
    bg = PEACH if mode == "pause" else GREEN
    try:
        q = subprocess.run(["tmux", "display-message", "-p", "-c", client,
                            "#{client_width} #{client_height} #{client_tty}"],
                           capture_output=True, text=True, timeout=10)
        cw, ch, tty = q.stdout.split()
        w, h, x, y = popup_geometry(int(cw), int(ch), tty, wezterm_panes() or [])
        log({"mode": "popup", "client": client, "press": press, "geometry": [w, h, x, y]})
        # tmux 3.7c draws almost nothing of a popup's content while a status
        # message is up on that client (measured: two rows, until the message
        # expires), so replace any message with an empty 1 ms one first.
        subprocess.run(["tmux", "display-message", "-c", client, "-d", "1", ""],
                       capture_output=True, timeout=10)
        time.sleep(0.1)
        return subprocess.Popen(["tmux", "display-popup", "-c", client, "-E", "-B",
                                 "-w", str(w), "-h", str(h), "-x", str(x), "-y", str(y),
                                 "-s", "bg=%s,fg=%s" % (bg, INK), popup_cmd(mode, press)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def popup_still_up(p):
    """Is that popup on screen now? A failed display-popup exits at once
    (given 0.3 s to do so); one Mack closed has exited too (0). Only a
    running one counts: anything else and the toggle shows the line."""
    if p is None:
        return False
    try:
        p.wait(timeout=0.3)
        return False
    except subprocess.TimeoutExpired:
        return True


def pause_view_open():
    """Is a PAUSE popup's view up now? watch keeps its pid in view.pid for
    as long as it runs; a stale file (a killed view) names a dead pid."""
    try:
        return alive(int(VIEW.read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        return False


def press_alive(press):
    """Is the toggle that owns this press id ("<ms>-<pid>") still running?
    An id it cannot parse counts as alive."""
    try:
        return alive(int(str(press).rsplit("-", 1)[1]))
    except (IndexError, ValueError):
        return True


# ---------------------------------------------------------------- watch
# The popup's view. build_frame is PURE (targets + statuses + size → lines)
# so it is unit-tested without a terminal, and so is the key handling
# (on_key, state_gone). watch() polls tmux once a second (the PAUSE popup is
# what Mack closes the lid on and it stays up across sleep, so it must never
# spin), paints with plain ANSI (alternate screen, truecolor fill of every
# cell, so the tint can change live without touching the popup's style) and
# restores the terminal on any exit.
#
# PAUSE popup keys: Enter resumes everyone right there (the toggle's own
# resume, under the same lock) and shows RESUMED; Esc closes it and leaves
# the saved set for prefix P; every other key is ignored, so a stray press
# on wake cannot dismiss it. A popup killed under us (SIGHUP) is an Esc. If
# the saved set changes while it is open (prefix P from another client), it
# shows that resume's RESUMED view and closes itself - unless the resume
# reached nobody, in which case it stays paused with a note. A resume only
# removes the agents it reached, so a retry set can remain (do_resume).

# A 5-row block font, just the letters the banners need.
FONT = {
    "P": ["████ ", "█   █", "████ ", "█    ", "█    "],
    "A": [" ███ ", "█   █", "█████", "█   █", "█   █"],
    "U": ["█   █", "█   █", "█   █", "█   █", " ███ "],
    "S": [" ████", "█    ", " ███ ", "    █", "████ "],
    "I": ["█████", "  █  ", "  █  ", "  █  ", "█████"],
    "N": ["█   █", "██  █", "█ █ █", "█  ██", "█   █"],
    "G": [" ████", "█    ", "█  ██", "█   █", " ████"],
    "F": ["█████", "█    ", "████ ", "█    ", "█    "],
    "E": ["█████", "█    ", "████ ", "█    ", "█████"],
    "T": ["█████", "  █  ", "  █  ", "  █  ", "  █  "],
    "O": [" ███ ", "█   █", "█   █", "█   █", " ███ "],
    "C": [" ████", "█    ", "█    ", "█    ", " ████"],
    "L": ["█    ", "█    ", "█    ", "█    ", "█████"],
    "R": ["████ ", "█   █", "████ ", "█  █ ", "█   █"],
    "M": ["█   █", "██ ██", "█ █ █", "█   █", "█   █"],
    "D": ["████ ", "█   █", "█   █", "█   █", "████ "],
    "V": ["█   █", "█   █", "█   █", " █ █ ", "  █  "],
    " ": ["   "] * 5,
}

PAUSING, HELD, STOPPED = "pausing…", "held (draft in box)", "stopped ✓"
RESUMED, WORKING, UNCONFIRMED = "resumed ✓", "working", "sent (no reply)"
FAILED, GONE, NOT_SENT = "failed", "gone", "not messaged (no agent pane)"
SENDING = "sending…"                          # delivery in flight (progress.json)
LIMITED_ROW = "rate-limited (left paused)"    # handed to limited.json, no RESUME (RATE LIMITS)
SENDING_FOOT = "sending… Enter works once every agent has it   ·   Esc  close"
SETTLED = {STOPPED, FAILED, GONE, NOT_SENT}   # a pause row that needs nothing more
STILL_AFTER = 180                             # s before "still working: …"
RESUME_CLOSE = 4                              # s the prefix-P RESUMED popup stays up
RESUMED_AFTER_PAUSE = 3                       # s the RESUMED view stays after Enter / a remote resume
PAUSE_FOOT = "⏎ Enter  resume all now   ·   Esc  close (resume later with prefix P)"
# A spinner while anything is still stopping, so the view reads as loading
# rather than frozen. It repaints at SPIN_HZ only until the banner flips (then
# back to one tick a second for the sleep); tmux is still polled once a second.
SPINNER, SPIN_HZ = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", 8


def block(text):
    rows = ["", "", "", "", ""]
    for i, ch in enumerate(text):
        glyph = FONT.get(ch, FONT[" "])
        for r in range(5):
            rows[r] += ("" if i == 0 else " ") + glyph[r]
    return rows


def banner_lines(text, cols):
    """The banner as block letters if it fits, one word per block row if
    that fits, else plain text."""
    whole = block(text)
    if len(whole[0]) <= cols - 2:
        return whole
    words = text.split()
    blocks = [block(w) for w in words]
    if all(len(b[0]) <= cols - 2 for b in blocks):
        out = []
        for i, b in enumerate(blocks):
            out += ([""] if i else []) + b
        return out
    return [text]


def pause_status(t, row):
    """One pause row's status from its saved result and its window now."""
    if t.get("result") == "failed":
        return FAILED
    if t.get("result") == "unidentified":
        return NOT_SENT
    if row is None:
        return GONE
    if working_reasons(row):
        return HELD if t.get("result") == "held" else PAUSING
    return STOPPED


def resume_status(t, row):
    if t.get("result") in ("gone", "failed"):
        return GONE if t["result"] == "gone" else FAILED
    if t.get("result") == "limited":
        return LIMITED_ROW
    if row is not None and row["agent_state"] == "running":
        return WORKING
    return {"held": HELD, "unknown": UNCONFIRMED}.get(t.get("result"), RESUMED)


def fit(s, n):
    return s if len(s) <= n else (s[:max(0, n - 1)] + "…" if n > 0 else "")


def build_frame(mode, items, cols, rows, elapsed, close_in=None, note="", spin=0, sending=False):
    """-> (background hex, exactly `rows` lines of exactly `cols` chars).
    mode: pause | resume | dropped (NOT RESUMED) | notpaused (NOT PAUSED) |
    voided (VOIDED: the saved set was voided under an open PAUSE view);
    items: [(label, location, status)]; close_in: a brief view's countdown
    (s); note: one extra line; spin: the spinner frame for rows still in
    flight; sending: delivery is still running (rows read SENDING until their
    result lands). Pure."""
    cols, rows = max(cols, 10), max(rows, 3)
    statuses = [s for _, _, s in items]
    sp = SPINNER[spin % len(SPINNER)]
    landed = sum(s != SENDING for s in statuses)
    plural = "" if len(items) == 1 else "s"
    if mode == "pause" and sending:
        title, bg, waiting = "PAUSING", PEACH, []
        sub = "%s sending PAUSE to %d agent%s… %d of %d done · %d:%02d" % (
            sp, len(items), plural, landed, len(items), elapsed // 60, elapsed % 60)
        items = [(l, loc, s if s in SETTLED else sp + " " + s) for l, loc, s in items]
        foot = SENDING_FOOT
    elif mode == "pause":
        done = all(s in SETTLED for s in statuses)
        title, bg = ("SAFE TO CLOSE", GREEN) if done else ("PAUSING", PEACH)
        waiting = [lab for lab, _, s in items if s not in SETTLED]
        stopped = sum(s == STOPPED for s in statuses)
        sub = ("✓ all %d stopped — safe to close the lid" % len(items) if done and stopped == len(items)
               else "✓ nothing still working — safe to close the lid" if done
               else "%s stopping all agents… %d of %d stopped" % (sp, stopped, len(items)))
        items = [(l, loc, s if s in SETTLED else sp + " " + s) for l, loc, s in items]
        sub += " · %d:%02d" % (elapsed // 60, elapsed % 60)
        foot = PAUSE_FOOT
    elif mode == "notpaused":   # a pause that reached nobody: nothing is saved
        title, bg, waiting = "NOT PAUSED", PEACH, []
        sub = "the PAUSE reached no agent — nothing is paused"
        foot = "any key to close"
    elif mode == "dropped":   # a resume that reached nobody: never green
        title, bg, waiting = "NOT RESUMED", PEACH, []
        sub = "no paused agent could be reached"
        left = 0 if close_in is None else int(close_in)
        foot = "closes in %ds · any key to close" % max(0, left)
    elif mode == "voided":    # the pause was voided: neither a resume nor a failure
        title, bg, waiting = "VOIDED", LAVENDER, []
        sub = "⏸ pause voided — agents stay paused, won't be resumed"
        left = 0 if close_in is None else int(close_in)
        foot = "closes in %ds · any key to close" % max(0, left)
    elif sending:             # resume, delivery still running
        title, bg, waiting = "RESUMED", GREEN, []
        sub = "%s sending RESUME to %d agent%s… %d of %d done" % (sp, len(items), plural, landed, len(items))
        foot = SENDING_FOOT
    else:
        title, bg, waiting = "RESUMED", GREEN, []
        ok = sum(s in (RESUMED, WORKING, HELD, UNCONFIRMED) for s in statuses)
        sub = "%d of %d agents told to pick up where they left off" % (ok, len(items))
        left = RESUME_CLOSE - int(elapsed) if close_in is None else int(close_in)
        foot = "closes in %ds · any key to close" % max(0, left)
    if mode != "pause":
        items = [(l, loc, sp + " " + s if s == SENDING else s) for l, loc, s in items]
    body = banner_lines(title, cols) + ["", fit(sub, cols - 2)]
    body += ([fit(note, cols - 2)] if note else []) + [""]
    # Item rows, left-aligned as one block that is centred as a whole.
    show_loc = cols >= 70
    lw = min(max([len(l) for l, _, _ in items] + [5]), 36 if show_loc else max(8, cols - 26))
    item_lines = []
    for lab, loc, st in items:
        line = fit(lab, lw).ljust(lw) + ("  " + fit(loc, 16).ljust(16) if show_loc else "") + "  " + st
        item_lines.append(fit(line, cols - 2))
    room = rows - 2 - len(body) - (2 if (mode == "pause" and waiting and elapsed >= STILL_AFTER) else 0)
    if len(item_lines) > max(room, 1):
        keep = max(room - 1, 0)
        item_lines = item_lines[:keep] + ["+%d more" % (len(items) - keep)]
    width = max([len(l) for l in item_lines] + [0])
    body += [l.ljust(width) for l in item_lines]
    if mode == "pause" and waiting and elapsed >= STILL_AFTER:
        body += ["", fit("still working: %s — close anyway or wait" % ", ".join(waiting), cols - 2)]
    body = body[:rows - 1]
    top = max(0, (rows - 1 - len(body)) // 2)
    lines = [""] * top + body
    lines += [""] * (rows - 1 - len(lines))
    lines.append(fit(foot, cols))
    return bg, [fit(l.center(cols), cols).ljust(cols) for l in lines]


def watch_items(mode, demo, t0):
    """-> a function returning the current [(label, location, status)]."""
    if demo:
        fake = [("tmux/Pause Resume Agents", "main:3.1"), ("cua-notch/1Password Sign-ins", "cua-notch:2.1"),
                ("math_econ_sched/Granola", "schedule:3.1"), ("tmux/Tmux Agent Sidebar", "main:2.1")]

        def demo_items():
            el = time.time() - t0
            if mode == "resume":
                return [(l, loc, RESUMED if i != 2 else WORKING) for i, (l, loc) in enumerate(fake)]
            return [(l, loc, STOPPED if el > 2 * (i + 1) else (HELD if i == 1 else PAUSING))
                    for i, (l, loc) in enumerate(fake)]
        return demo_items
    if mode == "pause":
        st = load_state() or {"targets": []}
        targets = list(st["targets"]) + list(st.get("failed") or []) + [
            dict(u, result="unidentified") for u in st.get("unidentified") or []]
        pane_of, status_of = (lambda t: t.get("pane_id")), pause_status
    else:
        targets = last_resume().get("resume") or []
        pane_of, status_of = (lambda t: t.get("resume_pane")), resume_status
    last = {"panes": {}}

    def items():
        try:
            last["panes"] = list_panes()
        except Exception:
            pass   # keep the previous snapshot; one failed read is not "gone"
        return [(t.get("label", "?"), t.get("location", ""), status_of(t, last["panes"].get(pane_of(t))))
                for t in targets]
    return items


def paint(bg, lines):
    r, g, b = (int(bg[i:i + 2], 16) for i in (1, 3, 5))
    ink = tuple(int(INK[i:i + 2], 16) for i in (1, 3, 5))
    out = ["\x1b[48;2;%d;%d;%dm\x1b[38;2;%d;%d;%dm" % (r, g, b, *ink)]
    for i, line in enumerate(lines):
        out.append("\x1b[%d;1H%s" % (i + 1, line))
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def on_key(phase, data):
    """A key read in the popup -> "resume" | "close" | None (ignored). phase:
    "pause" (the PAUSE view) or "resumed" (the RESUMED view). Pure.
    A lone ESC byte is Esc; anything longer that starts with ESC is an arrow,
    function key or focus report and is ignored like any other stray key."""
    if not data:
        return None
    if phase == "sending":   # delivery in flight: Enter would race it
        return "close" if data == b"\x1b" else None
    if phase != "pause":     # resumed / dropped / notpaused: any key closes
        return "close"
    if data in (b"\r", b"\n", b"\r\n"):
        return "resume"
    if data == b"\x1b":
        return "close"
    return None


def baseline(st):
    """What a PAUSE popup remembers about the saved set it shows: which pause
    it is and how many resume attempts it has seen. Pure."""
    st = st or {}
    return (st.get("paused_at"), int(st.get("resume_attempts") or 0))


def state_gone(base, st):
    """Has the saved set this PAUSE popup shows changed under it: resumed
    (wholly, or partly - a retry set can remain) from another client, or
    replaced by a newer pause? Pure."""
    return not st or not st.get("targets") or baseline(st) != base


def last_resume():
    """paused.last.json, or {}."""
    try:
        d = json.loads(LAST.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def resume_note(last):
    """The RESUMED view's extra line when a retry set remains. Pure."""
    n = int(last.get("still_paused") or 0)
    return "%d still paused — press prefix P again to retry" % n if n else ""


def resume_reached(last):
    """Did that resume reach anyone? A target handed to limited.json counts:
    it left the saved set, nothing failed. Pure."""
    return any(t.get("result") in REACHED + ("limited",) for t in last.get("resume") or [])


def voided(last, base):
    """Was the pause this popup shows (baseline `base`) voided, not resumed?
    void_pause writes paused.last.json from paused.json itself, so it
    carries that pause's paused_at and no `resume` at all. Pure."""
    return bool(last.get("voided_at")) and base is not None and last.get("paused_at") == base[0]


def dropped_note(last):
    """The NOT RESUMED view's line: a resume that reached nobody and dropped
    what was left. Pure."""
    n = sum(1 for t in last.get("resume") or [] if t.get("dropped"))
    return "dropped %d unreachable agent%s, nothing resumed" % (n, "" if n == 1 else "s")


def locked_resume(wait=3.0):
    """Enter in the PAUSE popup: the toggle's own resume, under the same lock
    (waiting up to `wait` s for a press in flight). -> (ok, message)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lf = open(LOCK, "a")
    try:
        deadline = time.time() + wait
        while True:
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    return False, "pause key busy — press Enter again"
                time.sleep(0.1)
        st = load_state()
        if not st or not st["targets"]:
            return True, "already resumed"
        msg, popup = do_resume("popup", st)
    finally:
        lf.close()
    return popup is not None, msg + limit_after_press()   # outside the lock (RATE LIMITS)


def progress_items(prog, panes):
    """progress.json's rows -> [(label, location, status)]: SENDING until a
    row's result lands, then the mode's own status. Pure."""
    out = []
    for t in prog.get("targets") or []:
        if t.get("result") == "sending":
            st = SENDING
        elif prog.get("mode") == "resume":
            st = resume_status(t, panes.get(t.get("resume_pane")))
        else:
            st = pause_status(t, panes.get(t.get("pane_id")))
        out.append((t.get("label", "?"), t.get("location", ""), st))
    return out


def after_sending(prog):
    """The view a finished press's popup moves to. Pure.
      pause:  paused -> "pause" (the live view)   failed -> "notpaused"
      resume: resumed -> "resumed"   not_resumed / failed -> "dropped" (NOT RESUMED)"""
    if prog.get("mode") == "resume":
        return "resumed" if prog.get("outcome") == "resumed" else "dropped"
    return "pause" if prog.get("outcome") == "paused" else "notpaused"


def watch(mode, demo=False, press=None):
    import select
    import shutil
    import signal
    import termios
    import tty
    t0 = time.time()
    # With a press id the popup opened before delivery: "sending" until that
    # press's progress.json says done (see after_sending).
    sending = bool(press) and not demo
    phase = "sending" if sending else ("pause" if mode == "pause" else "resumed")
    items_now = watch_items(mode, demo, t0)
    close_at = None if (mode == "pause" or sending) else t0 + RESUME_CLOSE
    first = None if (demo or mode != "pause" or sending) else load_state()
    base = baseline(first) if first else None   # None: nothing to watch for
    note = "" if (demo or mode == "pause" or sending) else resume_note(last_resume())
    panes_seen = {}
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd) if os.isatty(fd) else None
    flag = {"dirty": True}

    def bye(*_):   # the popup closed under us, ^C: the same as Esc
        raise SystemExit(0)
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, bye)
    signal.signal(signal.SIGWINCH, lambda *_: flag.update(dirty=True))

    def to_resumed(now):
        nonlocal phase, items_now, close_at, items, note
        phase, close_at = "resumed", now + RESUMED_AFTER_PAUSE
        items_now = watch_items("resume", demo, now)
        items = items_now()
        note = "" if demo else resume_note(last_resume())
        flag["dirty"] = True

    def changed(now, why):
        """The saved set changed under the PAUSE view. Decided by whether that
        resume REACHED anyone, never by whether paused.json is gone: it is
        also removed when a resume reached nobody and dropped the last misses.
          reached someone          -> RESUMED (with the retry count, if any)
          nobody, a set remains    -> stay paused, re-reading the set
          nobody, nothing remains  -> NOT RESUMED (peach), then close
          voided (CuaNotch, the sidebar: `agent-pause.py void`) -> VOIDED,
            then close. Its paused.last.json has no `resume`, so it used to
            read as a resume that reached nobody (2026-10-22)."""
        nonlocal base, items_now, items, note, phase, close_at
        st, last = load_state(), last_resume()
        if not (st and st.get("targets")) and voided(last, base):
            phase, close_at = "voided", now + RESUMED_AFTER_PAUSE
            note = ""
        elif resume_reached(last):
            to_resumed(now)
        elif st and st.get("targets"):
            base = baseline(st)
            items_now = watch_items("pause", demo, t0)
            items = items_now()
            note = why
        else:
            phase, close_at = "dropped", now + RESUMED_AFTER_PAUSE
            items_now = watch_items("resume", demo, now)
            items = items_now()
            note = dropped_note(last)
        flag["dirty"] = True

    def sent(now, prog=None):
        """This press's delivery is done (or its record was replaced by a
        later press, which only happens after ours finished): move on."""
        nonlocal phase, items_now, items, note, close_at, base
        prog = prog or read_progress()
        if not prog or prog.get("press") != press:
            prog = {"mode": mode, "outcome": "paused" if mode == "pause" else "resumed"}
            if mode == "pause" and not load_state():
                prog["outcome"] = "failed"
        nxt = after_sending(prog)
        msg = (prog.get("message") or "").lstrip("⏸▶ ")
        if nxt == "pause":
            phase = "pause"
            st = load_state()
            base = baseline(st) if st else None
            items_now = watch_items("pause", demo, t0)
            items = items_now()
        elif nxt == "resumed":
            to_resumed(now)
            close_at = now + RESUME_CLOSE
        else:   # notpaused (any key) / dropped (closes itself)
            phase, note = nxt, msg
            close_at = None if nxt == "notpaused" else now + RESUME_CLOSE
            items = progress_items(prog, panes_seen) if prog.get("targets") else items
        flag["dirty"] = True

    # A PAUSE view says so in view.pid, so a void elsewhere leaves the
    # telling to its VOIDED view (void_pause, pause_view_open).
    me = str(os.getpid())
    if mode == "pause" and not demo:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            VIEW.write_text(me + "\n")
        except OSError:
            pass
    sys.stdout.write("\x1b[?1049h\x1b[?25l")
    try:
        if saved is not None:
            tty.setcbreak(fd)
        items, polled = None, 0.0
        while True:
            now = time.time()
            if phase == "sending":
                # progress.json every frame (a small file); tmux, and whether
                # the toggle is still alive, once a second.
                dead = False
                if now - polled >= 1:
                    polled = now
                    try:
                        panes_seen = list_panes()
                    except Exception:
                        pass
                    dead = not press_alive(press)
                prog = read_progress()
                if prog and prog.get("press") == press and not prog.get("done"):
                    if dead and not (read_progress() or {}).get("done"):
                        # Killed without marking done (finished() covers any
                        # exception): never spin "sending" forever.
                        sent(now, dict(prog, done=True, message="the pause key crashed: see %s" % LOG,
                                       outcome="paused" if mode == "pause" and load_state() else "failed"))
                    else:
                        items = progress_items(prog, panes_seen)
                else:
                    sent(now)
                flag["dirty"] = True
            # One tick a second at most: this view sits open across a sleep.
            elif items is None or now - polled >= 1:
                polled = now
                if phase == "pause" and base is not None and state_gone(base, load_state()):
                    changed(now, "a resume from another client reached nobody — still paused")
                elif phase in ("pause", "resumed"):
                    items = items_now()
                flag["dirty"] = True
            if flag["dirty"]:
                flag["dirty"] = False
                size = shutil.get_terminal_size((80, 24))
                view = {"resumed": "resume", "sending": mode}.get(phase, phase)
                paint(*build_frame(view, items or [], size.columns,
                                   size.lines, int(now - t0),
                                   close_in=None if close_at is None else close_at - now + 0.999,
                                   note=note, spin=int((now - t0) * SPIN_HZ),
                                   sending=phase == "sending"))
            if close_at is not None and now >= close_at:
                return
            wait = 1.0 if close_at is None else max(0.05, min(1.0, close_at - now))
            if phase == "sending" or (phase == "pause" and items
                                      and any(s not in SETTLED for _, _, s in items)):
                wait, flag["dirty"] = min(wait, 1.0 / SPIN_HZ), True   # spinner frame
            ready, _, _ = select.select([fd], [], [], wait)
            if not ready:
                continue
            act = on_key(phase, os.read(fd, 64))
            if act == "close":
                return
            if act == "resume":
                if demo:
                    to_resumed(time.time())
                    continue
                note = "resuming…"
                size = shutil.get_terminal_size((80, 24))
                paint(*build_frame("pause", items, size.columns, size.lines, int(now - t0), note=note))
                ok, msg = locked_resume()
                if ok or (base is not None and state_gone(base, load_state())):
                    changed(time.time(), msg)   # same rule as a resume from elsewhere
                else:
                    note = msg                  # busy, or tmux-mcp down: nothing changed
                flag["dirty"] = True
    finally:
        try:
            if VIEW.read_text().strip() == me:   # never a later view's
                VIEW.unlink()
        except OSError:
            pass
        if saved is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()


def fmt_target(t, extra=""):
    sid = (t.get("session_id") or "")[:8]
    return "  %-6s %-22s %-6s %-16s %s%s%s" % (
        t["pane_id"], t["location"], t.get("kind", ""), t.get("why", ""), t["label"],
        "  sid " + sid if sid else "", extra)


def status():
    st = load_state()
    if not st or not st["targets"]:
        print("not paused")
    else:
        print("paused since %s: %d agent%s" % (st.get("paused_at"), len(st["targets"]),
                                              "" if len(st["targets"]) == 1 else "s"))
        for t in st["targets"]:
            print(fmt_target(t, "  [%s]" % t.get("result", "")))
        for t in st.get("failed") or []:
            print(fmt_target(t, "  [FAILED at pause: %s]" % t.get("detail", "")))
    for line in limited_lines():
        print(line)


def dry_run():
    """Reads tmux and the session records; writes nothing, sends nothing,
    takes no lock and opens no MCP connection."""
    panes = list_panes()
    recs = agent_records()
    st = load_state()
    if st and st["targets"]:
        print("mode: RESUME (a saved set from %s)" % st.get("paused_at"))
        for t in st["targets"]:
            pane, note = locate(t, panes, recs)
            if pane and limit_handover(panes[pane]):
                note = "rate-limited: no RESUME, handed to limited.json"
            print(fmt_target(t, "  -> %s%s" % (pane or "GONE", " (%s)" % note if note else "")))
        for line in limited_lines(eligibility=True):
            print(line)
        print("nothing sent (dry run)")
        return
    targets, idle, unidentified = scan(panes, recs)
    print("mode: PAUSE (no saved set)")
    print("would pause %d:" % len(targets))
    for t in targets:
        print(fmt_target(t, "" if t["found_by"] == "record" else "  (no session record: tmux-mcp will likely refuse)"))
    if unidentified:
        print("working but no agent pane found (%d):" % len(unidentified))
        for u in unidentified:
            print("  %-6s %-22s %-6s %-16s %s" % (u["window_id"], u["location"], "", u["why"], u["label"]))
    print("skipped, not working (%d):" % len(idle))
    for u in idle:
        print("  %-6s %-22s %-6s %-16s %s" % (u["window_id"], u["location"], "", u["state"], u["label"]))
    for line in limited_lines(eligibility=True):
        print(line)
    print("nothing sent (dry run)")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd in ("toggle", "resume"):
        client = argv[2] if len(argv) > 2 else ""
        try:
            toggle(client, resume_only=(cmd == "resume"))
        except Exception as e:   # the keybind path never fails silently
            msg = "⏸ pause key error: %s: %s" % (type(e).__name__, e)
            log({"mode": "error", "client": client, "message": msg})
            try:
                display(client, msg, "error")
            except Exception:
                pass
        return 0
    if cmd in ("dismiss", "void"):
        client = argv[2] if len(argv) > 2 else ""
        try:
            msg = (dismiss_glow if cmd == "dismiss" else void_pause)(client)
        except Exception as e:   # the click/key path never fails silently
            msg = "⏸ pause key error: %s: %s" % (type(e).__name__, e)
            log({"mode": "error", "client": client, "message": msg})
        try:
            if msg:   # None: an open PAUSE popup shows the void itself
                display(client, msg)
        except Exception:
            pass
        return 0
    # The rate-limit commands run from hooks, the watcher and sidebar clicks
    # (run-shell -b), so like the key they never print and never exit
    # non-zero: the log has every outcome, and a click's refusal is a toast.
    if cmd in ("limit-add", "limit-tick", "limit-auto", "limit-resume"):
        rest = argv[2:]
        try:
            if cmd == "limit-add" and len(rest) == 1:
                limit_add(rest[0])
            elif cmd == "limit-tick" and not rest:
                limit_tick()
            elif cmd == "limit-auto" and rest in (["on"], ["off"], ["toggle"]):
                display("", limit_auto(rest[0]))
            elif cmd == "limit-resume" and [a for a in rest if a != "--force"]:
                w = [a for a in rest if a != "--force"][0]
                ok, msg = limit_resume(w, force="--force" in rest)
                log({"mode": "limit-resume", "window": w, "force": "--force" in rest, "ok": ok,
                     "message": msg})
                display("", msg, "info" if ok else "warn")
            else:
                sys.stderr.write("usage: agent-pause.py limit-add <window> | limit-tick"
                                 " | limit-auto on|off|toggle | limit-resume [--force] <window>\n")
                return 2
        except Exception as e:
            msg = "rate limit error (%s): %s: %s" % (cmd, type(e).__name__, e)
            log({"mode": "error", "message": msg})
            if cmd in ("limit-auto", "limit-resume"):   # Mack's own action: say so
                try:
                    display("", msg, "error")
                except Exception:
                    pass
        return 0
    if cmd == "status":
        status()
        return 0
    if cmd == "dry-run":
        dry_run()
        return 0
    if cmd == "watch" and len(argv) > 2 and argv[2] in ("pause", "resume"):
        try:
            rest = argv[3:]
            press = rest[rest.index("--press") + 1] if "--press" in rest[:-1] else None
            watch(argv[2], demo="--demo" in rest, press=press)
        except Exception as e:   # a crash would just blink the popup shut
            log({"mode": "error", "message": "watch: %s: %s" % (type(e).__name__, e)})
            print("pause key: the %s view failed (%s: %s); the agents were messaged anyway.\n"
                  "Details in %s. Any key to close." % (argv[2], type(e).__name__, e, LOG))
            sys.stdout.flush()
            import select
            select.select([sys.stdin], [], [], 30)
        return 0
    sys.stderr.write("usage: agent-pause.py toggle|resume <client_name> | dismiss|void [client_name]"
                     " | status | dry-run"
                     " | watch pause|resume [--demo] [--press <id>]"
                     " | limit-add <window> | limit-tick | limit-auto on|off|toggle"
                     " | limit-resume [--force] <window>\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
