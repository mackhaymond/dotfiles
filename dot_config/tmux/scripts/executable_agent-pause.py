#!/usr/bin/env python3
"""agent-pause.py: prefix P, the pause key (docs/agent-pause.md).

Before the laptop lid closes, one press tells every agent that is actually
WORKING to stop at a safe point, and remembers exactly which ones it told.
The next press tells exactly that saved set to resume. Not a fresh scan:
agents finish while the lid is shut and states move on, and a RESUME must
reach the agents that were paused, not whatever happens to be running now.

    agent-pause.py toggle <client_name>   the key: pause, or resume a saved set
    agent-pause.py status                 print the saved set, or "not paused"
    agent-pause.py dry-run                print who WOULD be paused/resumed; sends nothing
    agent-pause.py watch pause|resume [--demo]
                                          the popup's view (see FEEDBACK below);
                                          --demo plays it with fake agents

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
live claude-<pid>.json / codex-<pid>.json record in tmux-assistant-resurrect's
dir whose env.tmux_pane names it (the same lookup as tmux-mcp's readAgents,
same dir search order). message_agent refuses any pane without one, so picking
by anything else would only pick panes that cannot be reached. A window with
no record falls back to the pane whose command looks like an agent (claude,
codex, or Claude's version-named binary); those deliveries will most likely be
refused and are reported as failures, never dropped silently.

DELIVERY goes through tmux-mcp's message_agent (MCP over HTTP, bearer token
from its state dir), not a raw paste: its draft guard HOLDS a message while
the target's prompt box has a half-typed draft or a dialog, and delivers it
once the box is clear. "held" therefore counts as delivered.

This file is the keybind path: it never exits non-zero and never prints in
toggle mode (run-shell would open a view pane over Mack's window); every
outcome is a display-message on the pressing client and a line in the log.
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
    "[pause key] PAUSE: Mack is closing his laptop. Stop at the next safe point: "
    "finish or abandon the current edit so no file is half-written, stop any dev "
    "servers or long jobs you started (kill_pty), tell your running subagents/"
    "workflows to stop at a safe point, don't start new work, and reply in 2-3 "
    "lines with where you stopped and what's next. Don't commit or push unless "
    "that was already your next step. Wait for a RESUME message before continuing.")
RESUME_MSG = (
    "[pause key] RESUME: Mack is back. Pick up where you paused, using the next "
    "steps you noted, and restart any servers, jobs or subagents you stopped if "
    "you still need them.")

# Sessions that only park or mirror windows (agent-jump.sh's EXCLUDE): a
# window linked into one of these AND a real session is shown by the real one.
EXCLUDE = {"agents", "tasks", "stash", "scratch", "btop-popup"}
# Claude Code's kernel p_comm is its version ("2.1.170"); see the watcher's
# AGENT_PAT. Only the fallback for a pane with no session record.
AGENT_CMD = re.compile(r"^(claude|codex|[0-9]+\.[0-9]+\.[0-9]+)$")
US = "\x1f"
# @agent_summary is free text, so it goes LAST and the split is bounded.
PANE_FIELDS = ("pane_id", "window_id", "session_name", "window_index", "pane_index",
               "pane_pid", "pane_current_command", "pane_active", "@agent_state",
               "@agent_workflow", "@agent_cua", "@agent_kind", "@agent_summary")
PANE_FMT = US.join("#{%s}" % f for f in PANE_FIELDS)
DISPLAY_MS = "6000"


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


def display(client, msg):
    """One line on the pressing client. display-message expands formats, so
    every '#' in the text is doubled. A vanished client falls back to tmux's
    current one; nothing at all is fine (the log has it)."""
    msg = msg.replace("#", "##")
    if client:
        r = subprocess.run(["tmux", "display-message", "-c", client, "-d", DISPLAY_MS, msg],
                           capture_output=True, timeout=10)
        if r.returncode == 0:
            return
    subprocess.run(["tmux", "display-message", "-d", DISPLAY_MS, msg], capture_output=True, timeout=10)


def set_paused_option(n):
    """@agents_paused (global): the count while paused, unset otherwise. For a
    status indicator; nothing renders it yet."""
    if n:
        tmux("set-option", "-g", "@agents_paused", str(n), check=False)
    else:
        tmux("set-option", "-gu", "@agents_paused", check=False)


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
        m = re.match(r"^(claude|codex)-(\d+)\.json$", name)
        if not m:
            continue
        kind, pid = m.group(1), int(m.group(2))
        if not alive(pid):
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
            cmd = [r for r in rows if AGENT_CMD.match(r["pane_current_command"])]
            cmd.sort(key=lambda r: r["pane_active"] != "1")
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
            "kind": rec.get("kind") or head["agent_kind"] or (
                "codex" if pick["pane_current_command"] == "codex" else "claude"),
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


def deliver(mcp, jobs, msg):
    """jobs [(key, pane_id)] -> {key: (status, detail)}, concurrently: each
    delivery spends ~400 ms in tmux-mcp's draft guard. Never raises: every
    job gets a status - sent | held | failed | unknown (see McpUncertain;
    anything unexpected counts as unknown too, since it may have gone out)."""
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

def do_pause(client):
    panes = list_panes()
    recs = agent_records()
    targets, _idle, unidentified = scan(panes, recs)
    entry = {"mode": "pause", "client": client,
             "unidentified": unidentified}
    noid = (" · %d working window%s had no agent pane: %s" % (
        len(unidentified), "" if len(unidentified) == 1 else "s", names(unidentified))
        if unidentified else "")
    if not targets:
        msg = "⏸ no agents are working — nothing paused" + noid
        log(dict(entry, message=msg, targets=[]))
        return msg, None
    try:
        mcp = open_mcp()
    except McpError as e:
        msg = "⏸ pause failed, nothing sent: %s" % e
        log(dict(entry, message=msg, targets=targets))
        return msg, None
    try:
        res = deliver(mcp, [(i, t["pane_id"]) for i, t in enumerate(targets)], PAUSE_MSG)
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
        log(dict(entry, message=msg, targets=targets))
        return msg, None
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
    log(dict(entry, message=msg, targets=targets))
    return msg, "pause"


def do_resume(client, st):
    saved = st["targets"]
    panes = list_panes()
    recs = agent_records()
    live, gone = [], []
    for t in saved:
        pane, note = locate(t, panes, recs)
        t = dict(t, resume_pane=pane, resume_note=note)
        (live if pane else gone).append(t)
    entry = {"mode": "resume", "client": client, "paused_at": st.get("paused_at")}
    if live:
        try:
            mcp = open_mcp()
        except McpError as e:
            # Nothing was sent: keep the saved set so the next press retries.
            msg = "▶ resume failed, still paused (%d): %s" % (len(saved), e)
            log(dict(entry, message=msg, targets=live + gone))
            return msg, None
        try:
            res = deliver(mcp, [(i, t["resume_pane"]) for i, t in enumerate(live)], RESUME_MSG)
        finally:
            mcp.close()
        for i, t in enumerate(live):
            t["result"], t["detail"] = res.get(i, ("unknown", "no result"))
    for t in gone:
        t["result"], t["detail"] = "gone", t["resume_note"]
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
    log(dict(entry, message=msg, targets=live + gone))
    # Nobody reached: no RESUMED popup (it would claim a resume); the line
    # says what is still paused.
    return msg, ("resume" if ok else None)


def toggle(client):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lf = open(LOCK, "a")
    try:
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            display(client, "⏸ pause key busy — the last press is still sending")
            log({"mode": "busy", "client": client})
            return
        st = load_state()
        if st and st["targets"]:
            msg, popup = do_resume(client, st)
        else:
            msg, popup = do_pause(client)
    finally:
        lf.close()   # releases the flock
    # Feedback only AFTER the lock is free: the pause popup stays up until a
    # key, and a held lock would turn the next press into "busy".
    feedback(client, msg, popup)


# ------------------------------------------------------------- feedback
# A display-message line is too easy to miss with the lid half shut (Mack's
# call), so a press that sent anything opens a border-less FULL-SCREEN popup
# running `watch <mode>`: a block-letter banner (PAUSING → SAFE TO CLOSE, or
# RESUMED) over one live row per agent. display-message is the fallback for
# no client or a popup that will not open.

PEACH, GREEN, INK = "#fab387", "#a6e3a1", "#11111b"
OVERRIDES = ("AGENT_PAUSE_STATE_DIR", "TMUX_MCP_URL", "TMUX_MCP_PORT", "TMUX_MCP_TOKEN_FILE",
             "TMUX_ASSISTANT_RESURRECT_DIR")


def popup_cmd(mode):
    """The shell command the popup runs. The popup's environment is the tmux
    server's, not ours, so our overrides ride along explicitly (Enter in the
    popup resumes, which needs the same state dir, tmux-mcp and records).
    Paths and a URL only: the token itself never goes on a command line."""
    import shlex
    env = ["%s=%s" % (k, os.environ[k]) for k in OVERRIDES if os.environ.get(k)]
    return "exec %s%s %s watch %s" % (
        ("env %s " % " ".join(shlex.quote(e) for e in env)) if env else "",
        shlex.quote(sys.executable), shlex.quote(str(Path(__file__).resolve())), mode)


def popup_size(cw, ch):
    """~70% x 60% of the client, but never smaller than 60x16 (or the whole
    client when it is smaller than that). Pure."""
    return (min(cw, max(60, round(cw * 0.7))), min(ch, max(16, round(ch * 0.6))))


def open_popup(client, mode):
    """True if the popup opened. display-popup waits until it closes, which
    is fine: the lock is already released and run-shell -b backgrounds us.
    Centred and border-less on a solid tint, so it reads as a big card."""
    bg = PEACH if mode == "pause" else GREEN
    try:
        size = subprocess.run(["tmux", "display-message", "-p", "-c", client,
                               "#{client_width} #{client_height}"],
                              capture_output=True, text=True, timeout=10)
        cw, ch = (int(x) for x in size.stdout.split())
        w, h = popup_size(cw, ch)
        # tmux 3.7c draws almost nothing of a popup's content while a status
        # message is up on that client (measured: two rows, until the message
        # expires), so replace any message with an empty 1 ms one first.
        subprocess.run(["tmux", "display-message", "-c", client, "-d", "1", ""],
                       capture_output=True, timeout=10)
        time.sleep(0.1)
        r = subprocess.run(["tmux", "display-popup", "-c", client, "-E", "-B",
                            "-w", str(w), "-h", str(h), "-s", "bg=%s,fg=%s" % (bg, INK),
                            popup_cmd(mode)], capture_output=True, text=True)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def feedback(client, msg, popup):
    if popup and client and open_popup(client, popup):
        return
    display(client, msg)


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
SETTLED = {STOPPED, FAILED, GONE, NOT_SENT}   # a pause row that needs nothing more
STILL_AFTER = 180                             # s before "still working: …"
RESUME_CLOSE = 4                              # s the prefix-P RESUMED popup stays up
RESUMED_AFTER_PAUSE = 3                       # s the RESUMED view stays after Enter / a remote resume
PAUSE_FOOT = "⏎ Enter  resume all now   ·   Esc  close (resume later with prefix P)"


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


def build_frame(mode, items, cols, rows, elapsed, close_in=None, note=""):
    """-> (background hex, exactly `rows` lines of exactly `cols` chars).
    mode: pause | resume; items: [(label, location, status)]; close_in: the
    RESUMED view's countdown (s); note: one extra line (an Enter that failed).
    Pure."""
    cols, rows = max(cols, 10), max(rows, 3)
    statuses = [s for _, _, s in items]
    if mode == "pause":
        done = all(s in SETTLED for s in statuses)
        title, bg = ("SAFE TO CLOSE", GREEN) if done else ("PAUSING", PEACH)
        waiting = [lab for lab, _, s in items if s not in SETTLED]
        stopped = sum(s == STOPPED for s in statuses)
        sub = ("all %d stopped — safe to close the lid" % len(items) if done and stopped == len(items)
               else "nothing still working — safe to close the lid" if done
               else "%d of %d stopped — waiting for the rest" % (stopped, len(items)))
        sub += " · %d:%02d" % (elapsed // 60, elapsed % 60)
        foot = PAUSE_FOOT
    elif mode == "dropped":   # a resume that reached nobody: never green
        title, bg, waiting = "NOT RESUMED", PEACH, []
        sub = "no paused agent could be reached"
        left = 0 if close_in is None else int(close_in)
        foot = "closes in %ds · any key to close" % max(0, left)
    else:
        title, bg, waiting = "RESUMED", GREEN, []
        ok = sum(s in (RESUMED, WORKING, HELD, UNCONFIRMED) for s in statuses)
        sub = "%d of %d agents told to pick up where they left off" % (ok, len(items))
        left = RESUME_CLOSE - int(elapsed) if close_in is None else int(close_in)
        foot = "closes in %ds · any key to close" % max(0, left)
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
    if phase != "pause":   # resumed / dropped: brief views, any key closes
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


def watch(mode, demo=False):
    import select
    import shutil
    import signal
    import termios
    import tty
    t0 = time.time()
    phase = "pause" if mode == "pause" else "resumed"
    items_now = watch_items(mode, demo, t0)
    close_at = None if mode == "pause" else t0 + RESUME_CLOSE
    first = None if (demo or mode != "pause") else load_state()
    base = baseline(first) if first else None   # None: nothing to watch for
    note = "" if (demo or mode == "pause") else resume_note(last_resume())
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

    sys.stdout.write("\x1b[?1049h\x1b[?25l")
    try:
        if saved is not None:
            tty.setcbreak(fd)
        items, polled = None, 0.0
        while True:
            now = time.time()
            # One tick a second at most: this view sits open across a sleep.
            if items is None or now - polled >= 1:
                polled = now
                if phase == "pause" and base is not None and state_gone(base, load_state()):
                    changed(now, "a resume from another client reached nobody — still paused")
                else:
                    items = items_now()
                flag["dirty"] = True
            if flag["dirty"]:
                flag["dirty"] = False
                size = shutil.get_terminal_size((80, 24))
                paint(*build_frame({"resumed": "resume"}.get(phase, phase), items, size.columns,
                                   size.lines, int(now - t0),
                                   close_in=None if close_at is None else close_at - now + 0.999,
                                   note=note))
            if close_at is not None and now >= close_at:
                return
            wait = 1.0 if close_at is None else max(0.05, min(1.0, close_at - now))
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
    if cmd == "toggle":
        client = argv[2] if len(argv) > 2 else ""
        try:
            toggle(client)
        except Exception as e:   # the keybind path never fails silently
            msg = "⏸ pause key error: %s: %s" % (type(e).__name__, e)
            log({"mode": "error", "client": client, "message": msg})
            try:
                display(client, msg)
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
            watch(argv[2], demo="--demo" in argv[3:])
        except Exception as e:   # a crash would just blink the popup shut
            log({"mode": "error", "message": "watch: %s: %s" % (type(e).__name__, e)})
            print("pause key: the %s view failed (%s: %s); the agents were messaged anyway.\n"
                  "Details in %s. Any key to close." % (argv[2], type(e).__name__, e, LOG))
            sys.stdout.flush()
            import select
            select.select([sys.stdin], [], [], 30)
        return 0
    sys.stderr.write("usage: agent-pause.py toggle <client_name> | status | dry-run"
                     " | watch pause|resume [--demo]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
