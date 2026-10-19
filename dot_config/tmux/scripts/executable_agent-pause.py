#!/usr/bin/env python3
"""agent-pause.py: prefix P, the pause key (docs/agent-pause.md).

Before the laptop lid closes, one press tells every agent that is actually
WORKING to stop at a safe point, and remembers exactly which ones it told.
The next press tells exactly that saved set to resume. Not a fresh scan:
agents finish while the lid is shut and states move on, and a RESUME must
reach the agents that were paused, not whatever happens to be running now.

    agent-pause.py toggle <client_name>   the key: pause, or resume a saved set
    agent-pause.py resume <client_name>   resume a saved set, never pause (the sidebar)
    agent-pause.py status                 print the saved set, or "not paused"
    agent-pause.py dry-run                print who WOULD be paused/resumed; sends nothing
    agent-pause.py watch pause|resume [--demo] [--press <id>]
                                          the popup's view (see FEEDBACK below);
                                          --demo plays it with fake agents;
                                          --press: follow that press's delivery
                                          (progress.json) first

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
    "started (kill_pty); tell running subagents/workflows to stop now. Start "
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
PANE_FIELDS = ("pane_id", "window_id", "session_name", "window_index", "pane_index",
               "pane_pid", "pane_current_command", "pane_active", "@agent_state",
               "@agent_workflow", "@agent_cua", "@agent_kind", "@agent_summary")
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


def scan(panes, recs):
    """-> (targets, idle, unidentified). targets: one dict per working agent
    window; idle: agent windows that are not working (for dry-run); and
    unidentified: working windows with no pane we could message."""
    windows = {}
    for row in panes.values():
        windows.setdefault(row["window_id"], []).append(row)
    targets, idle, unidentified = [], [], []
    for wid, rows in windows.items():
        rows.sort(key=lambda r: int(r["pane_index"] or 0))
        head = rows[0]   # window options are the same on every pane of it
        with_rec = [r for r in rows if r["pane_id"] in recs]
        if not (head["agent_state"] or head["agent_kind"] or with_rec):
            continue   # not an agent window at all
        why = working_reasons(head)
        if not why:
            idle.append({"window_id": wid, "location": head["location"],
                         "state": head["agent_state"] or "(no state)", "label": label_of(head)})
            continue
        pick, how = None, ""
        if with_rec:
            kind = head["agent_kind"]
            with_rec.sort(key=lambda r: (recs[r["pane_id"]]["kind"] != kind if kind else False,
                                         r["pane_active"] != "1"))
            pick, how = with_rec[0], "record"
        else:
            kind = head["agent_kind"]
            cmd = [r for r in rows if AGENT_CMD.match(r["pane_current_command"])
                   and (r["pane_current_command"] != "opencode" or kind == "opencode")]
            cmd.sort(key=lambda r: (cmd_kind(r["pane_current_command"]) != kind if kind else False,
                                    r["pane_active"] != "1"))
            if cmd:
                pick, how = cmd[0], "command"
        if pick is None:
            unidentified.append({"window_id": wid, "location": head["location"],
                                 "why": "+".join(why), "label": label_of(head)})
            continue
        rec = recs.get(pick["pane_id"], {})
        targets.append({
            "pane_id": pick["pane_id"],
            "window_id": wid,
            "location": pick["location"],
            "label": label_of(pick),
            "kind": rec.get("kind") or head["agent_kind"] or cmd_kind(pick["pane_current_command"]),
            "session_id": rec.get("session_id", ""),
            "pid": rec.get("pid"),
            "why": "+".join(why),
            "found_by": how,
        })
    return targets, idle, unidentified


def locate(t, panes, recs):
    """Where a saved target lives now -> (pane_id | None, note). Its own pane
    if it still exists and (when a session id was saved) still runs that
    session; else a pane now running that session (a tmux-resurrect restore
    renumbers panes); else gone."""
    sid = t.get("session_id") or ""
    pane = t.get("pane_id")
    note = "pane gone"
    if pane in panes:
        if not sid:
            return pane, ""
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


def deliver(mcp, jobs, msg, on_result=None):
    """jobs [(key, pane_id)] -> {key: (status, detail)}, concurrently: each
    delivery spends ~400 ms in tmux-mcp's draft guard. Never raises: every
    job gets a status - sent | held | failed | unknown (see McpUncertain;
    anything unexpected counts as unknown too, since it may have gone out).
    on_result(key, (status, detail)) is called as each one lands (progress)."""
    def one(pane):
        try:
            return classify(mcp.call("message_agent", {"pane": pane, "message": msg, "raw": True}))
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
    try:
        res = deliver(mcp, [(i, t["pane_id"]) for i, t in enumerate(targets)], PAUSE_MSG,
                      on_result=prog.result)
    finally:
        mcp.close()
    for i, t in enumerate(targets):
        # A job with no result at all may still have gone out: unknown.
        t["result"], t["detail"] = res.get(i, ("unknown", "no result"))
    ok = [t for t in targets if t["result"] in REACHED]
    bad = [t for t in targets if t["result"] not in REACHED]
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
        msg += " · %d held behind a draft" % len(held)
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
        if pane:
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
    if live:
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
        try:
            res = deliver(mcp, [(i, t["resume_pane"]) for i, t in enumerate(live)], RESUME_MSG,
                          on_result=prog.result)
        finally:
            mcp.close()
        for i, t in enumerate(live):
            t["result"], t["detail"] = res.get(i, ("unknown", "no result"))
    ok = [t for t in live if t["result"] in REACHED]
    bad = [t for t in live if t["result"] not in REACHED]
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
    else:
        msg = "▶ resume reached no agents"
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
    prog.write(True, "resumed" if ok else "not_resumed", msg)
    log(dict(entry, message=msg, targets=live + gone))
    return msg, bool(ok)


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
        display(client, msg)


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


# ------------------------------------------------------------- feedback
# A display-message line is too easy to miss with the lid half shut (Mack's
# call), so a press with anyone to message opens a big border-less popup
# running `watch <mode> --press <id>`, AT ONCE, before delivery: rows read
# "sending…" until progress.json fills them in, then a block-letter banner
# (PAUSING → SAFE TO CLOSE, RESUMED, or the peach NOT PAUSED / NOT RESUMED
# when nobody was reached) over one live row per agent. display-message is
# the fallback for no client or a popup that will not open.

PEACH, GREEN, INK = "#fab387", "#a6e3a1", "#11111b"
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


def open_popup(client, mode, press):
    """Launch the popup for this press and return its process, without
    waiting: display-popup blocks until the popup closes, and the popup must
    be up while delivery runs. Its own session, so it outlives the toggle.
    Big, border-less, on a solid tint; positioned by popup_geometry."""
    if not client:
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
    " ": ["   "] * 5,
}

PAUSING, HELD, STOPPED = "pausing…", "held (draft in box)", "stopped ✓"
RESUMED, WORKING, UNCONFIRMED = "resumed ✓", "working", "sent (no reply)"
FAILED, GONE, NOT_SENT = "failed", "gone", "not messaged (no agent pane)"
SENDING = "sending…"                          # delivery in flight (progress.json)
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
    if row is not None and row["agent_state"] == "running":
        return WORKING
    return {"held": HELD, "unknown": UNCONFIRMED}.get(t.get("result"), RESUMED)


def fit(s, n):
    return s if len(s) <= n else (s[:max(0, n - 1)] + "…" if n > 0 else "")


def build_frame(mode, items, cols, rows, elapsed, close_in=None, note="", spin=0, sending=False):
    """-> (background hex, exactly `rows` lines of exactly `cols` chars).
    mode: pause | resume | dropped (NOT RESUMED) | notpaused (NOT PAUSED);
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
    """Did that resume reach anyone? Pure."""
    return any(t.get("result") in REACHED for t in last.get("resume") or [])


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
        return popup is not None, msg
    finally:
        lf.close()


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
          nobody, nothing remains  -> NOT RESUMED (peach), then close"""
        nonlocal base, items_now, items, note, phase, close_at
        st, last = load_state(), last_resume()
        if resume_reached(last):
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
        return
    print("paused since %s: %d agent%s" % (st.get("paused_at"), len(st["targets"]),
                                          "" if len(st["targets"]) == 1 else "s"))
    for t in st["targets"]:
        print(fmt_target(t, "  [%s]" % t.get("result", "")))
    for t in st.get("failed") or []:
        print(fmt_target(t, "  [FAILED at pause: %s]" % t.get("detail", "")))


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
            print(fmt_target(t, "  -> %s%s" % (pane or "GONE", " (%s)" % note if note else "")))
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
    sys.stderr.write("usage: agent-pause.py toggle|resume <client_name> | status | dry-run"
                     " | watch pause|resume [--demo] [--press <id>]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
