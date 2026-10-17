// tmux-agent-state: drive this pane's tmux tab state from OpenCode's events.
//
// The OpenCode half of ~/.config/tmux/scripts/agent-tab-indicator.sh (see
// ~/.config/tmux/docs/agent-tab-indicator.md, "OpenCode"). Every lifecycle
// event becomes one `agent-tab-indicator.sh <mode> opencode` call with a
// Claude-shaped JSON payload on stdin (session_id, cwd, prompt, tool_name,
// tool_input, last_assistant_message, error, session_title, source), so the
// tab, the sidebar detail and the agent slot behave exactly as for Claude.
//
// It also keeps the GROUND-TRUTH STATUS FILE the watcher reads to clear a
// stuck `running` (OpenCode fires nothing the indicator could see on a crash
// mid-turn):
//   ${XDG_STATE_HOME:-~/.local/state}/tmux-agent/opencode/<pid>.status
// one line, `busy` or `idle`, written atomically (tmp + rename) on change and
// removed on exit. <pid> is process.pid, the opencode binary itself: in the
// full TUI this file runs in the server worker THREAD of that process.
//
// Where it runs (verified on 1.18.18): the full TUI loads server plugins in a
// worker thread of the same process, which inherits TMUX/TMUX_PANE, so the
// pane is known here; argv there is the worker's, not the user's. That worker
// is the ONLY place it acts (see ownsPane): main-thread loads (`run`, `serve`,
// `web`, ...) and any opencode started from another agent's tool shell stay
// inert.
//
// Rules:
// - Never block OpenCode: hooks only queue work and return. Each indicator
//   call costs ~300 ms of bash/jq/tmux, so calls run one at a time through a
//   single promise chain. Turn bookkeeping runs IN that chain too, in event
//   order, so a `running` can never land after the `done` that followed it
//   and a quick next prompt cannot be swallowed by the previous turn's end.
// - Child sessions (subagents: parentID set) never move the tab, except a
//   permission or question they raise, which blocks the user like the root's
//   own and is shown on the root's tab. Their tool calls count as heartbeats.
// - Heartbeats (tool.execute.after) are throttled to one per HEARTBEAT_MS; a
//   permission/question reply sends one at once (it re-arms `running`).
// - Esc (MessageAbortedError) ends a turn as `interrupt` (idle, no tint), not
//   `done`; any other session error is `failed` (red) and the idle after it
//   leaves the red alone.
//
// Overrides (tests): TMUX_AGENT_INDICATOR = the indicator script (run with
// bash), TMUX_AGENT_HEARTBEAT_MS = the heartbeat throttle.

import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

const AGENT = "opencode";
// OpenCode's default title until it generates one ("New session - <ISO date>";
// subagents get "Child session - ..."). Not a name: the indicator skips it too.
const PLACEHOLDER_TITLE = /^(New|Child) session - \d{4}-\d{2}-\d{2}T/;
const CALL_TIMEOUT_MS = 10_000;
const EXIT_WAIT_MS = 1_500;

// ------------------------------------------------------------ status file
// Process-wide: one file per opencode pid, whatever instances load us.
const statuses = new Map(); // sessionID -> "busy" | "retry", any session
let statusWritten; // the value on disk ("busy" | "idle"), undefined = none
let statusLive = false; // false after exit: never resurrect the file

function statusFile() {
  const base = process.env.XDG_STATE_HOME || join(homedir(), ".local/state");
  return join(base, "tmux-agent", "opencode", `${process.pid}.status`);
}

function writeStatus() {
  if (!statusLive) return;
  const value = statuses.size > 0 ? "busy" : "idle";
  if (value === statusWritten) return;
  const file = statusFile();
  const tmp = `${file}.tmp.${process.pid}`;
  try {
    mkdirSync(dirname(file), { recursive: true });
    writeFileSync(tmp, `${value}\n`);
    renameSync(tmp, file);
    statusWritten = value;
  } catch {
    try { unlinkSync(tmp); } catch {}
  }
}

function removeStatus() {
  statusLive = false;
  statusWritten = undefined;
  try { unlinkSync(statusFile()); } catch {}
}

let exitHooked = false;

// ------------------------------------------------------------ helpers
function indicatorPath() {
  return process.env.TMUX_AGENT_INDICATOR ||
    join(homedir(), ".config/tmux/scripts/agent-tab-indicator.sh");
}

function heartbeatMs() {
  const n = Number(process.env.TMUX_AGENT_HEARTBEAT_MS);
  return process.env.TMUX_AGENT_HEARTBEAT_MS && Number.isFinite(n) && n >= 0 ? n : 2_000;
}

async function isWorkerThread() {
  try {
    return !(await import("node:worker_threads")).isMainThread;
  } catch {
    return false;
  }
}

// Markers of a shell an agent runs its tools in (Claude Code, Codex, the pty
// MCP's agent shells). An opencode started there inherited the PARENT agent's
// TMUX_PANE: that tab belongs to the parent, never to this process.
const PARENT_AGENT_ENV = ["CLAUDECODE", "CODEX_THREAD_ID", "CODEX_SANDBOX", "AGENT_PTY", "AGENT_SHELL"];

// Does this process own the pane it inherited? Only the full TUI does: its
// server plugins run in the TUI's worker thread (verified on 1.18.18).
// Everything on the main thread stays inert: a shared server (`serve`, `web`,
// `acp`, ...) cannot know which pane it serves, and `opencode run "..."` is
// usually a tool call of another agent that passed down its own TMUX_PANE
// (it would repaint that agent's tab: title, kind, running, done, clear).
async function ownsPane() {
  if (!process.env.TMUX || !process.env.TMUX_PANE) return false;
  if (PARENT_AGENT_ENV.some((key) => process.env[key])) return false;
  return isWorkerThread();
}

// The indicator must act for THIS pane only: drop any Codex owner binding a
// launching shell might have leaked (the indicator would defer to it).
function indicatorEnv() {
  const env = { ...process.env };
  for (const key of Object.keys(env)) {
    if (key === "AGENT_TAB_SOCKET" || key.startsWith("AGENT_TAB_OWNER_")) delete env[key];
  }
  return env;
}

function runIndicator(mode, payload) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn("bash", [indicatorPath(), mode, AGENT], {
        env: indicatorEnv(),
        stdio: ["pipe", "ignore", "ignore"],
      });
    } catch {
      resolve();
      return;
    }
    const timer = setTimeout(() => {
      try { child.kill("SIGKILL"); } catch {}
      resolve();
    }, CALL_TIMEOUT_MS);
    timer.unref?.();
    const finish = () => {
      clearTimeout(timer);
      resolve();
    };
    child.on("error", finish);
    child.on("exit", finish);
    child.stdin?.on("error", () => {});
    try {
      child.stdin?.end(JSON.stringify(payload));
    } catch {}
  });
}

function textOf(parts) {
  return (Array.isArray(parts) ? parts : [])
    .filter((p) => p?.type === "text" && !p.synthetic && !p.ignored && typeof p.text === "string")
    .map((p) => p.text)
    .join("\n")
    .trim();
}

function errorText(error) {
  if (!error) return "";
  if (typeof error === "string") return error;
  return error.data?.message || error.message || error.name || "";
}

function capitalize(s) {
  return typeof s === "string" && s ? s[0].toUpperCase() + s.slice(1) : "";
}

// What a permission request is about, in the indicator's tool_input shape
// (DETAIL_JQ's toolarg: command, file_path, pattern, ...).
function permissionInput(p) {
  const meta = p?.metadata && typeof p.metadata === "object" ? p.metadata : {};
  const patterns = Array.isArray(p?.patterns) ? p.patterns.filter((x) => typeof x === "string") : [];
  if (p?.permission === "bash") {
    return { command: typeof meta.command === "string" ? meta.command : patterns.join(" ") };
  }
  const file = meta.filepath ?? meta.filePath ?? meta.file_path;
  if (typeof file === "string" && file) return { file_path: file };
  return { pattern: patterns.join(" ") };
}

// ------------------------------------------------------------ the plugin
export const TmuxAgentState = async ({ client, directory }) => {
  if (!(await ownsPane())) return {};

  const cwd = directory || process.cwd();
  const sessions = new Map(); // id -> { parentID, title } as last seen
  const lookups = new Map(); // id -> pending client.session.get
  const lastFetch = new Map(); // root id -> pending last-assistant fetch
  // Turn bookkeeping: touched only inside chain jobs (event order).
  const turns = new Set(); // roots with a turn in progress
  const aborted = new Set(); // roots whose turn was cancelled (Esc)
  const failedRoots = new Set(); // roots whose turn already went `failed`
  let chain = Promise.resolve();
  let lastHeartbeat = 0;
  let stopped = false;

  // ---------------------------------------------------------- sessions
  function remember(info) {
    if (!info?.id) return;
    sessions.set(info.id, { parentID: info.parentID || undefined, title: info.title });
  }

  function lookup(id) {
    if (sessions.has(id)) return Promise.resolve(sessions.get(id));
    if (!lookups.has(id)) {
      lookups.set(id, (async () => {
        try {
          const result = await client.session.get({ path: { id } });
          if (result?.data?.id === id) remember(result.data);
        } catch {}
        return sessions.get(id);
      })().finally(() => lookups.delete(id)));
    }
    return lookups.get(id);
  }

  // The root of a session's subagent tree. An unknown session is looked up
  // once; one that cannot be found is treated as a root.
  async function rootOf(id) {
    const seen = new Set();
    let current = id;
    while (typeof current === "string" && current && !seen.has(current)) {
      seen.add(current);
      const info = await lookup(current);
      if (!info?.parentID) return current;
      current = info.parentID;
    }
    return id;
  }

  function titleOf(id) {
    const title = sessions.get(id)?.title;
    return typeof title === "string" && !PLACEHOLDER_TITLE.test(title) ? title : "";
  }

  // ---------------------------------------------------------- transport
  function enqueue(job) {
    chain = chain.then(() => (stopped ? undefined : job())).catch(() => {});
  }

  // After dispose nothing but its own clear may reach the tab: a job already
  // running (awaiting a fetch, say) must not paint over that clear.
  function send(mode, fields) {
    if (stopped) return Promise.resolve();
    return runIndicator(mode, { cwd, ...fields });
  }

  function heartbeat(force) {
    const now = Date.now();
    if (!force && now - lastHeartbeat < heartbeatMs()) return;
    lastHeartbeat = now;
    enqueue(() => send("heartbeat", {}));
  }

  async function fetchLastAssistant(root) {
    try {
      const result = await client.session.messages({ path: { id: root }, query: { limit: 6 } });
      const messages = Array.isArray(result?.data) ? result.data : [];
      for (let i = messages.length - 1; i >= 0; i--) {
        if (messages[i]?.info?.role === "assistant") return messages[i];
      }
    } catch {}
    return undefined;
  }

  // Started when the idle EVENT arrives (not when its job runs), so the answer
  // is this turn's reply even if the user has already sent the next prompt.
  function prefetchLast(id) {
    if (sessions.get(id)?.parentID || lastFetch.has(id)) return;
    lastFetch.set(id, fetchLastAssistant(id));
  }

  // ---------------------------------------------------------- turn jobs
  async function startTurn(root, prompt) {
    if (prompt === undefined && turns.has(root)) return;
    turns.add(root);
    aborted.delete(root);
    failedRoots.delete(root);
    await send("running", { session_id: root, session_title: titleOf(root), prompt: prompt ?? "" });
  }

  // A root went idle. Exactly one of interrupt / done (or nothing, after a
  // `failed`) per turn: the idle arrives twice (session.status and
  // session.idle) and only the first finds the turn. The tab is the PANE's:
  // while another root's turn still runs here (/new, then a prompt, while
  // the first was working) the pane is still working, so only the last turn
  // to end paints the end state.
  async function finishTurn(root) {
    const fetched = lastFetch.get(root);
    lastFetch.delete(root);
    if (!turns.delete(root)) return;
    const wasAborted = aborted.delete(root);
    const wasFailed = failedRoots.delete(root);
    if (wasFailed || turns.size > 0) return;
    const last = await (fetched ?? fetchLastAssistant(root));
    if (wasAborted || last?.info?.error?.name === "MessageAbortedError") {
      await send("interrupt", { session_id: root, session_title: titleOf(root) });
      return;
    }
    await send("done", {
      session_id: root,
      session_title: titleOf(root),
      last_assistant_message: textOf(last?.parts),
    });
  }

  // ---------------------------------------------------------- start / exit
  statusLive = true;
  statusWritten = undefined;
  writeStatus();
  enqueue(() => send("idle", { session_id: "", source: "startup" }));

  async function shutdown() {
    if (stopped) return;
    // Let the in-flight call land first, so it cannot repaint after the
    // clear; nothing queued behind it runs any more.
    const pending = chain;
    stopped = true;
    removeStatus();
    await Promise.race([pending, new Promise((r) => setTimeout(r, EXIT_WAIT_MS))]);
    try {
      spawnSync("bash", [indicatorPath(), "clear", AGENT], {
        env: indicatorEnv(),
        input: JSON.stringify({ cwd, reason: "exit" }),
        stdio: ["pipe", "ignore", "ignore"],
        timeout: 3_000,
      });
    } catch {}
  }
  // The TUI worker gets no process 'exit' (verified: dispose runs on quit,
  // SIGHUP, SIGTERM and SIGINT); `opencode run` on the main thread may get
  // only this. The tab itself is the watcher's to garbage-collect then.
  if (!exitHooked) {
    exitHooked = true;
    process.once("exit", () => removeStatus());
  }

  return {
    dispose: shutdown,

    "chat.message": async (input, output) => {
      const id = input?.sessionID;
      if (!id) return;
      const prompt = textOf(output?.parts);
      if (!sessions.get(id)?.parentID) {
        statuses.set(id, "busy");
        writeStatus();
      }
      enqueue(async () => {
        if ((await rootOf(id)) === id) await startTurn(id, prompt);
      });
    },

    "tool.execute.after": async () => {
      heartbeat(false);
    },

    event: async ({ event }) => {
      const type = event?.type;
      const props = event?.properties ?? {};
      const info = props.info;
      if (info?.id && (type === "session.created" || type === "session.updated")) {
        const before = sessions.get(info.id)?.title ?? "";
        remember(info);
        if (info.parentID) return;
        if (type === "session.created") {
          // A new root conversation (/new, or the TUI's first prompt): fresh
          // tab title — unless a turn is running in this pane.
          enqueue(async () => {
            if (turns.size === 0) await send("idle", { session_id: info.id, source: "startup" });
          });
          return;
        }
        const title = titleOf(info.id);
        if (title && title !== before) {
          enqueue(() => send("reconcile", { session_id: info.id, session_title: title }));
        }
        return;
      }

      const id = typeof props.sessionID === "string" ? props.sessionID : undefined;
      switch (type) {
        case "session.status": {
          if (!id) return;
          const kind = props.status?.type;
          if (kind === "busy" || kind === "retry") {
            statuses.set(id, kind);
            writeStatus();
            enqueue(async () => {
              if ((await rootOf(id)) === id) await startTurn(id, undefined);
            });
          } else if (kind === "idle") {
            statuses.delete(id);
            writeStatus();
            prefetchLast(id);
            enqueue(async () => {
              if ((await rootOf(id)) === id) await finishTurn(id);
            });
          }
          return;
        }
        case "session.idle":
          if (!id) return;
          statuses.delete(id);
          writeStatus();
          prefetchLast(id);
          enqueue(async () => {
            if ((await rootOf(id)) === id) await finishTurn(id);
          });
          return;
        case "session.error": {
          if (!id) return;
          const error = props.error;
          enqueue(async () => {
            if ((await rootOf(id)) !== id || !turns.has(id)) return;
            if (error?.name === "MessageAbortedError") {
              aborted.add(id);
              return;
            }
            failedRoots.add(id);
            await send("failed", { session_id: id, session_title: titleOf(id), error: errorText(error) });
          });
          return;
        }
        case "session.deleted": {
          const gone = id ?? info?.id;
          if (!gone) return;
          statuses.delete(gone);
          writeStatus();
          enqueue(async () => {
            turns.delete(gone);
            aborted.delete(gone);
            failedRoots.delete(gone);
            lastFetch.delete(gone);
          });
          return;
        }
        case "permission.asked":
        case "question.asked": {
          if (!id) return;
          const question = type === "question.asked";
          enqueue(async () => {
            const root = await rootOf(id);
            await send("needs-approval", {
              session_id: root,
              session_title: titleOf(root),
              tool_name: question ? "AskUserQuestion" : capitalize(props.permission) || "Permission",
              tool_input: question
                ? { questions: Array.isArray(props.questions) ? props.questions : [] }
                : permissionInput(props),
            });
          });
          return;
        }
        case "permission.replied":
        case "question.replied":
        case "question.rejected":
          heartbeat(true);
          return;
        default:
          return;
      }
    },
  };
};
