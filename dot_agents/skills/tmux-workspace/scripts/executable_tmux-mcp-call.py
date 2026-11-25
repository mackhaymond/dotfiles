#!/usr/bin/env python3
"""Call one tool on the shared tmux MCP server from a shell.

For agents that do not have the `tmux` MCP server wired in (Grok, Copilot,
agy, or any shell): the same list_panes / read_pane / message_agent / whoami
and spawn_subagent / wait_subagents / subagent_result / list_subagents / send_subagent /
cancel_subagent tools Claude Code gets as mcp__tmux__*, over the server's
Streamable HTTP endpoint.

    tmux-mcp-call.py list_panes '{"agents_only": true}'
    tmux-mcp-call.py read_pane  '{"pane": "%5"}'
    tmux-mcp-call.py message_agent '{"pane": "%5", "message": "..."}'
    tmux-mcp-call.py --timeout 600 wait_subagents '{"timeout_s": 540}'

Options (before or after the tool name):
    --timeout S   seconds to wait for the tool's answer (default 60; for
                  wait_subagents, its timeout_s plus 30 unless given)
    --pane %N     the pane this call acts from (a tmux pane id), sent as the
                  X-Tmux-Pane header (default $TMUX_PANE). Only a hint: the server
                  accepts it when this process sits on that pane's terminal,
                  or for agent-spawn's wake messages, when the pane is a
                  subagent writing to its own parent. A call from a
                  tmux-pty-mcp shell resolves to the agent's own tab.

The bearer token is read from ~/.local/state/tmux-mcp/token, never taken on
the command line (pty and shell logs are plaintext). Prints the tool's text;
exits 1 on a tool error, 2 on usage, 3 when the server is unreachable.
Stdlib only.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

URL = os.environ.get("TMUX_MCP_URL", "http://127.0.0.1:7998/mcp")
TOKEN_FILE = os.path.expanduser("~/.local/state/tmux-mcp/token")
KICK = "launchctl kickstart -k gui/%d/com.mackhaymond.tmux-mcp" % os.getuid()
PANE_HINT = None  # set by main(): the X-Tmux-Pane header value


def send(token, sid, payload, method="POST", timeout=60):
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream",
         "Authorization": "Bearer " + token}
    if PANE_HINT:
        h["X-Tmux-Pane"] = PANE_HINT
    if sid:
        h["mcp-session-id"] = sid
        h["mcp-protocol-version"] = "2025-06-18"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(URL, data=data, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.headers, r.read().decode("utf-8", "replace")


def response(body, want):
    """The JSON-RPC message with id `want`, from a JSON or SSE body."""
    docs, s = [], body.strip()
    if s[:1] in "{[":
        docs.append(json.loads(s))
    else:
        data = []
        for line in body.replace("\r\n", "\n").split("\n") + [""]:
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line.strip() and data:
                docs.append(json.loads("\n".join(data)))
                data = []
    for d in docs:
        for m in d if isinstance(d, list) else [d]:
            if isinstance(m, dict) and m.get("id") == want:
                return m
    raise SystemExit("tmux-mcp sent no response to request %s" % want)


def parse_argv(argv):
    """(tool, args, timeout or None, pane or None); SystemExit(2) on usage."""
    rest, timeout, pane = [], None, None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            raise SystemExit(2)
        if a in ("--timeout", "--pane"):
            if i + 1 >= len(argv):
                raise SystemExit(2)
            v = argv[i + 1]
            if a == "--timeout":
                try:
                    timeout = float(v)
                except ValueError:
                    raise SystemExit(2)
                if timeout <= 0:
                    raise SystemExit(2)
            else:
                pane = v
            i += 2
            continue
        if a.startswith("--timeout=") or a.startswith("--pane="):
            k, v = a.split("=", 1)
            return parse_argv(argv[:i] + [k, v] + argv[i + 1:])
        rest.append(a)
        i += 1
    if len(rest) not in (1, 2):
        raise SystemExit(2)
    try:
        args = json.loads(rest[1]) if len(rest) == 2 else {}
    except ValueError:
        raise SystemExit(2)
    return rest[0], args, timeout, pane


def main():
    global PANE_HINT
    try:
        tool, args, timeout, pane = parse_argv(sys.argv[1:])
    except SystemExit:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    if timeout is None:
        timeout = 60.0
        if tool == "wait_subagents" and isinstance(args, dict):
            try:
                timeout = max(timeout, float(args.get("timeout_s") or 45) + 30)
            except (TypeError, ValueError):
                pass
    pane = pane if pane is not None else os.environ.get("TMUX_PANE", "")
    PANE_HINT = pane.strip() if re.match(r"^%\d+$", pane.strip()) else None
    try:
        with open(TOKEN_FILE) as f:
            token = f.read().strip()
    except OSError:
        print("no token at %s: is tmux-mcp installed? (%s)" % (TOKEN_FILE, KICK), file=sys.stderr)
        return 3
    sid = None
    try:
        hdrs, body = send(token, None, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "tmux-mcp-call", "version": "1"}}}, timeout=10)
        response(body, 1)
        sid = hdrs.get("mcp-session-id")
        send(token, sid, {"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=10)
        _, body = send(token, sid, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                    "params": {"name": tool, "arguments": args}}, timeout=timeout)
        m = response(body, 2)
    except urllib.error.HTTPError as e:
        print("tmux-mcp HTTP %d%s" % (e.code, " (token rejected)" if e.code == 401 else ""), file=sys.stderr)
        return 3
    except urllib.error.URLError as e:
        print("tmux-mcp unreachable (%s); fix: %s" % (e.reason, KICK), file=sys.stderr)
        return 3
    finally:
        if sid:
            try:
                send(token, sid, None, method="DELETE", timeout=5)
            except Exception:
                pass
    if "error" in m:
        print((m["error"] or {}).get("message", "error"), file=sys.stderr)
        return 1
    res = m.get("result") or {}
    print("\n".join(c.get("text", "") for c in res.get("content") or [] if c.get("type") == "text"))
    return 1 if res.get("isError") else 0


if __name__ == "__main__":
    sys.exit(main())
