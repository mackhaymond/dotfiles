//! What a WORKING agent is doing right now, in a few words: the live
//! activity blurb its tile's second line shows (Claude's own subagent names
//! work the same way: short, model-written, changing as the turn goes on).
//!
//! No hook writes it and nothing forks: the sidebar reads the agent's own
//! session transcript.
//!
//! - **Which transcript.** Claude keeps `~/.claude/sessions/<pid>.json` per
//!   live process: its `sessionId`, `cwd` and `tmux` (`session:@win.%pane`).
//!   The directory is listed each refresh (a dozen tiny files; a file is
//!   re-read only when its (len, mtime) changed), and a window's session is
//!   a LIVE pid's (a crashed process's stale file never counts; the same
//!   tmux session first, then the newest `updatedAt`). The transcript is
//!   `~/.claude/projects/<cwd, every non-alphanumeric as ->/<sid>.jsonl`,
//!   else found once by a scan of `~/.claude/projects/*/<sid>.jsonl`; the
//!   path is cached per session id. Codex agents get no blurb (their rollout
//!   path is `@agent_rollout`, not in the snapshot yet): they keep the run
//!   detail.
//! - **How much.** Only for working agents; only when the transcript's (len,
//!   mtime) changed since the last read (the blurb is cached per file); at
//!   most its last [`TAIL`] bytes, parsed line by line from the end, the
//!   first (cut) line dropped. A window that holds nothing to go by (one
//!   huge tool result) says nothing: the row keeps the run detail.
//! - **What it says** ([`blurb`]): walking back through the CURRENT turn (up
//!   to the last real user prompt; a tool result is no prompt), the first of:
//!   the in-progress todo's `activeForm` (the latest TodoWrite, or a task
//!   tool's in-progress `activeForm` no later update of that task closed);
//!   the latest tool call, phrased ([`phrase`]:
//!   `Running cargo test`, `Editing layout.rs`, a Bash or Agent
//!   `description`); else `Thinking…`. One line, capitalized, no trailing
//!   period; the layout clips it to the tile.

use super::term;
use crate::json::{self, Value};
use crate::view::ViewModel;
use std::collections::HashMap;
use std::fs;
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime};

/// How much of a transcript's end is read for a blurb.
pub const TAIL: u64 = 64 * 1024;

/// How long a session whose transcript could not be found is left alone.
const RETRY: Duration = Duration::from_secs(10);

/// A file's identity for the caches: (len, mtime).
type Stamp = (u64, SystemTime);

fn stamp(p: &Path) -> Option<Stamp> {
    let m = fs::metadata(p).ok()?;
    Some((m.len(), m.modified().ok()?))
}

/// One `~/.claude/sessions/<pid>.json`.
#[derive(Debug, Clone)]
struct Session {
    stamp: Stamp,
    pid: i32,
    sid: String,
    cwd: String,
    /// The tmux session and window id from its `tmux` field
    /// (`main:@261.%270` → `main`, `@261`).
    session: String,
    window: String,
    updated: f64,
}

/// The blurbs of the working agents, and what finding them costs, cached
/// across refreshes (the live sidebar keeps one).
#[derive(Debug, Default)]
pub struct Activity {
    /// `~/.claude` (tests point it elsewhere).
    root: PathBuf,
    sessions: HashMap<PathBuf, Session>,
    /// Session id → its transcript (or when it was last looked for in vain).
    paths: HashMap<String, Result<PathBuf, Instant>>,
    /// Transcript → (its stamp when read, the blurb).
    blurbs: HashMap<PathBuf, (Stamp, Option<String>)>,
    /// Transcript reads so far (observable for the tests).
    pub reads: usize,
}

impl Activity {
    /// Reading `<root>/sessions` and `<root>/projects` (`root` = `~/.claude`).
    pub fn new(root: PathBuf) -> Activity {
        Activity { root, ..Activity::default() }
    }

    /// `~/.claude`, or `$AGENT_UI_CLAUDE_DIR` when set (tests).
    pub fn home() -> Activity {
        let root = match std::env::var("AGENT_UI_CLAUDE_DIR") {
            Ok(d) if !d.is_empty() => PathBuf::from(d),
            _ => PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(".claude"),
        };
        Activity::new(root)
    }

    /// Fill in [`crate::view::Agent::blurb`] for every working agent of
    /// `view` (spaces and NEEDS YOU alike); every other agent's stays None.
    pub fn apply(&mut self, view: &mut ViewModel) {
        // Working by its state alone: `in_flight` is also true of an asking
        // or failed agent with a workflow or cua flag, whose own line says
        // what it needs.
        let working = |a: &crate::view::Agent| a.cat == Some(crate::model::Cat::Working);
        let any = view.spaces.iter().flat_map(|s| s.agents.iter()).any(working);
        if !any {
            return;
        }
        self.scan_sessions();
        for a in view.spaces.iter_mut().flat_map(|s| s.agents.iter_mut()).chain(view.needs.iter_mut().map(|n| &mut n.agent)) {
            a.blurb = if working(a) && a.kind != "codex" { self.blurb_for(&a.session, &a.window_id) } else { None };
        }
    }

    /// The blurb for the agent in tmux window `win` of `session`, if its
    /// transcript says: the session file naming that window whose pid
    /// lives (a crashed one's stale file never speaks), one naming the same
    /// tmux session first, then the newest. (Its `procStart` is not checked
    /// against the process: a reused pid would also have to sit in that
    /// window.)
    pub fn blurb_for(&mut self, session: &str, win: &str) -> Option<String> {
        let key = |s: &Session| (s.session == session, s.updated);
        let s = self
            .sessions
            .values()
            .filter(|s| s.window == win && term::alive(s.pid))
            .max_by(|a, b| key(a).partial_cmp(&key(b)).unwrap_or(std::cmp::Ordering::Equal))?
            .clone();
        let path = self.transcript(&s.sid, &s.cwd)?;
        self.blurb_of(&path)
    }

    /// List `<root>/sessions`: (re)read the files that changed, forget the
    /// ones gone.
    fn scan_sessions(&mut self) {
        let Ok(dir) = fs::read_dir(self.root.join("sessions")) else {
            self.sessions.clear();
            return;
        };
        let mut seen = Vec::new();
        for e in dir.flatten() {
            let p = e.path();
            if p.extension().and_then(|x| x.to_str()) != Some("json") {
                continue;
            }
            let Some(st) = stamp(&p) else { continue };
            seen.push(p.clone());
            if self.sessions.get(&p).is_some_and(|s| s.stamp == st) {
                continue;
            }
            match fs::read_to_string(&p).ok().and_then(|t| session(&t, st)) {
                Some(s) => {
                    self.sessions.insert(p, s);
                }
                None => {
                    self.sessions.remove(&p);
                }
            }
        }
        self.sessions.retain(|p, _| seen.contains(p));
    }

    /// Session `sid`'s transcript: by its cwd's project directory, else one
    /// scan of every project directory; cached (a miss retried after
    /// [`RETRY`]).
    fn transcript(&mut self, sid: &str, cwd: &str) -> Option<PathBuf> {
        match self.paths.get(sid) {
            Some(Ok(p)) if p.exists() => return Some(p.clone()),
            Some(Err(at)) if at.elapsed() < RETRY => return None,
            _ => {}
        }
        let projects = self.root.join("projects");
        let file = format!("{sid}.jsonl");
        let direct = projects.join(project_slug(cwd)).join(&file);
        let found = if direct.exists() {
            Some(direct)
        } else {
            fs::read_dir(&projects).ok().and_then(|d| d.flatten().map(|e| e.path().join(&file)).find(|p| p.exists()))
        };
        self.paths.insert(sid.to_string(), found.clone().ok_or_else(Instant::now));
        found
    }

    /// The blurb from one transcript: read only when its (len, mtime)
    /// changed since the last read.
    pub fn blurb_of(&mut self, path: &Path) -> Option<String> {
        let st = stamp(path)?;
        if let Some((s, b)) = self.blurbs.get(path) {
            if *s == st {
                return b.clone();
            }
        }
        self.reads += 1;
        let b = read_tail(path, TAIL).and_then(|(t, whole)| blurb(&t, whole));
        self.blurbs.insert(path.to_path_buf(), (st, b.clone()));
        b
    }
}

/// `~/.claude/projects`' directory name for a cwd: every character but an
/// ASCII letter or digit becomes `-` (`/Users/me/.config/tmux` →
/// `-Users-me--config-tmux`).
pub fn project_slug(cwd: &str) -> String {
    cwd.chars().map(|c| if c.is_ascii_alphanumeric() { c } else { '-' }).collect()
}

/// A sessions file → the [`Session`]; None without a session id or window.
fn session(text: &str, stamp: Stamp) -> Option<Session> {
    let v = json::parse(text).ok()?;
    let tmux = v.get("tmux")?.as_str()?;
    let (session, rest) = tmux.rsplit_once(':')?;
    let window = rest.split('.').next()?.to_string();
    if !window.starts_with('@') {
        return None;
    }
    Some(Session {
        stamp,
        session: session.to_string(),
        pid: v.get("pid").and_then(Value::as_f64).unwrap_or(0.0) as i32,
        sid: v.get("sessionId")?.as_str()?.to_string(),
        cwd: v.get("cwd").and_then(Value::as_str).unwrap_or("").to_string(),
        window,
        updated: v.get("updatedAt").and_then(Value::as_f64).unwrap_or(0.0),
    })
}

/// The last `max` bytes of a file as text → (it, whether that is the whole
/// file). A cut first line is dropped.
fn read_tail(path: &Path, max: u64) -> Option<(String, bool)> {
    let mut f = fs::File::open(path).ok()?;
    let len = f.metadata().ok()?.len();
    let from = len.saturating_sub(max);
    f.seek(SeekFrom::Start(from)).ok()?;
    let mut buf = Vec::with_capacity((len - from) as usize);
    f.take(max).read_to_end(&mut buf).ok()?;
    let mut text = String::from_utf8_lossy(&buf).into_owned();
    if from > 0 {
        let cut = text.find('\n').map_or(text.len(), |i| i + 1);
        text.drain(..cut);
    }
    Some((text, from == 0))
}

/// What a turn with no tool call yet says.
pub const THINKING: &str = "Thinking…";

/// The blurb from a transcript's tail (JSONL; `whole`: the tail is the
/// whole file): walking back from the end through the current turn, the
/// in-progress todo's `activeForm`, else the latest tool call phrased
/// ([`phrase`]), else [`THINKING`] when the turn's prompt is in the tail.
/// None when the tail holds nothing to go by.
pub fn blurb(tail: &str, whole: bool) -> Option<String> {
    let mut tool: Option<String> = None;
    // The latest todo list decides, in progress or not.
    let mut todo: Option<Option<String>> = None;
    // Tasks a later update took out of progress (completed, pending, …).
    let mut closed: Vec<String> = Vec::new();
    let mut prompt = false;
    for line in tail.lines().rev() {
        // Cheap tests first: most lines are neither (progress, system,
        // attachments), and a tool result is never a prompt.
        let user = line.contains("\"type\":\"user\"");
        if !user && !line.contains("\"type\":\"assistant\"") {
            continue;
        }
        if user && line.contains("\"tool_result\"") && !line.contains("\"type\":\"text\"") {
            continue;
        }
        let Ok(v) = json::parse(line) else { continue };
        if v.get("isSidechain").and_then(Value::as_bool) == Some(true) {
            continue;
        }
        let content = v.get("message").and_then(|m| m.get("content"));
        match v.get("type").and_then(Value::as_str) {
            Some("user") => {
                if is_prompt(&v) {
                    prompt = true;
                    break;
                }
            }
            Some("assistant") => {
                for block in content.and_then(Value::as_array).unwrap_or(&[]).iter().rev() {
                    if block.get("type").and_then(Value::as_str) != Some("tool_use") {
                        continue;
                    }
                    let name = block.get("name").and_then(Value::as_str).unwrap_or("");
                    let input = block.get("input").unwrap_or(&Value::Null);
                    if let Some(t) = todo_of(name, input) {
                        todo.get_or_insert(t);
                    } else if matches!(name, "TaskCreate" | "TaskUpdate") {
                        // A task's in-progress `activeForm`, unless a later
                        // update (seen first, walking back) closed it.
                        let id = input.get("taskId").or_else(|| input.get("id")).and_then(Value::scalar_string).unwrap_or_default();
                        match input.get("status").and_then(Value::as_str) {
                            Some("in_progress") if todo.is_none() && !closed.contains(&id) => {
                                if let Some(af) = input.get("activeForm").and_then(Value::as_str) {
                                    todo = Some(Some(af.to_string()));
                                }
                            }
                            Some("in_progress") | None => {}
                            Some(_) => closed.push(id),
                        }
                    } else if tool.is_none() {
                        tool = phrase(name, input);
                    }
                }
            }
            _ => {}
        }
        if todo.as_ref().is_some_and(Option::is_some) {
            break;
        }
    }
    let said = todo.flatten().or(tool).or_else(|| (prompt || whole).then(|| THINKING.to_string()));
    said.map(|s| tidy(&s)).filter(|s| !s.is_empty())
}

/// A user record that starts a turn: the person's prompt (or a message
/// typed into the pane), not a tool result, not a meta record.
fn is_prompt(v: &Value) -> bool {
    if v.get("isMeta").and_then(Value::as_bool) == Some(true) {
        return false;
    }
    match v.get("message").and_then(|m| m.get("content")) {
        Some(Value::Str(s)) => !s.trim().is_empty(),
        Some(Value::Arr(blocks)) => blocks.iter().any(|b| b.get("type").and_then(Value::as_str) == Some("text")),
        _ => false,
    }
}

/// A TodoWrite call → Some(the in-progress item's `activeForm`, if any).
/// (The task tools are [`blurb`]'s own business: a task can be closed by a
/// later update.)
fn todo_of(name: &str, input: &Value) -> Option<Option<String>> {
    let s = |v: &Value, k: &str| v.get(k).and_then(Value::as_str).map(str::to_string);
    if name != "TodoWrite" {
        return None;
    }
    let todos = input.get("todos").and_then(Value::as_array).unwrap_or(&[]);
    let cur = todos.iter().find(|t| t.get("status").and_then(Value::as_str) == Some("in_progress"));
    Some(cur.and_then(|t| s(t, "activeForm").or_else(|| s(t, "content"))))
}

/// A word that may be a secret (never shown): it names one (`sk-`, `token`,
/// `key=`, `password`, `secret`), or it is 20+ characters of `[A-Za-z0-9_-]`
/// mixing at least two of upper case, lower case and digits.
fn secret(w: &str) -> bool {
    let l = w.to_ascii_lowercase();
    if ["sk-", "token", "key=", "password", "secret"].iter().any(|p| l.contains(p)) {
        return true;
    }
    let long = w.len() >= 20 && w.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-');
    let kinds = [w.chars().any(|c| c.is_ascii_uppercase()), w.chars().any(|c| c.is_ascii_lowercase()), w.chars().any(|c| c.is_ascii_digit())];
    long && kinds.iter().filter(|k| **k).count() >= 2
}

/// The first words of a shell command line: past `cd`, `export`, env
/// assignments and the like, the command's name (a path's last part) and up
/// to two plain words after it (`cargo test`, `git status`); never a word
/// that may be a secret ([`secret`]: the head stops before it). A shell
/// loop is `a loop`, an `if` / `case` `a script`.
pub fn command_head(cmd: &str) -> String {
    const SKIP: [&str; 12] = ["cd", "export", "unset", "set", "source", ".", "pushd", "popd", "true", ":", "local", "sleep"];
    const PREFIX: [&str; 6] = ["env", "sudo", "time", "exec", "nohup", "command"];
    for part in cmd.split(['\n', ';', '|', '&']) {
        let mut words = part.split_whitespace().map(|w| w.trim_start_matches(['(', '{'])).filter(|w| !w.is_empty()).peekable();
        while let Some(w) = words.peek() {
            let assign = w.split_once('=').is_some_and(|(k, _)| !k.is_empty() && k.chars().all(|c| c.is_ascii_alphanumeric() || c == '_'));
            if assign || PREFIX.contains(w) {
                words.next();
            } else {
                break;
            }
        }
        let Some(first) = words.next() else { continue };
        if SKIP.contains(&first) {
            continue;
        }
        match first {
            "for" | "while" | "until" | "select" => return "a loop".into(),
            "if" | "case" | "function" => return "a script".into(),
            _ => {}
        }
        let name = first.rsplit('/').next().unwrap_or(first);
        if secret(name) {
            return "a command".into();
        }
        let mut head = vec![name.to_string()];
        for w in words.take(2) {
            let plain = !w.starts_with('-') && w.chars().all(|c| c.is_ascii_alphanumeric() || "-_.:".contains(c));
            if !plain || secret(w) {
                break;
            }
            head.push(w.to_string());
        }
        return head.join(" ");
    }
    match cmd.split_whitespace().next() {
        Some(w) if !secret(w) => w.to_string(),
        Some(_) => "a command".into(),
        None => String::new(),
    }
}

/// A tool name as words: CamelCase split (`ScheduleWakeup` → `Schedule
/// wakeup`), `_` as spaces.
fn words_of(name: &str) -> String {
    let mut out = String::new();
    let mut prev_lower = false;
    for c in name.chars() {
        if c == '_' {
            out.push(' ');
            prev_lower = false;
            continue;
        }
        if c.is_ascii_uppercase() && prev_lower {
            out.push(' ');
            out.push(c.to_ascii_lowercase());
        } else {
            out.push(c);
        }
        prev_lower = c.is_ascii_lowercase() || c.is_ascii_digit();
    }
    out
}

/// An id a subagent goes by (hex, 12+ digits, maybe after `agent-`): no
/// name a person knows.
fn agent_id(to: &str) -> bool {
    let t = to.trim_start_matches("agent-").trim_start_matches("agent_");
    t.chars().filter(char::is_ascii_hexdigit).count() >= 12 && t.chars().all(|c| c.is_ascii_hexdigit() || c == '-')
}

/// A path's last part.
fn base(p: &str) -> &str {
    p.trim_end_matches('/').rsplit('/').next().unwrap_or(p)
}

/// A name that says nothing to a person: a UUID, or 16+ hex digits (with
/// at most `-` / `_` between them), before its extension.
fn hashy(name: &str) -> bool {
    let stem = name.split('.').next().unwrap_or(name);
    let hex = stem.chars().filter(char::is_ascii_hexdigit).count();
    hex >= 16 && stem.chars().all(|c| c.is_ascii_hexdigit() || c == '-' || c == '_')
}

/// A file as a phrase names it: its name; or, when that is a hash
/// ([`hashy`]), what it is by where it lives: `a transcript` (a `.jsonl`
/// under `.claude/projects`), `tool output` (under a tool-results or
/// scratchpad directory), `task output` (under `tasks`), else its nearest
/// meaningful directory (`screenshots/…`).
pub fn file_label(path: &str) -> String {
    let name = base(path);
    if !hashy(name) {
        return name.to_string();
    }
    let dirs: Vec<&str> = path.trim_end_matches('/').split('/').filter(|d| !d.is_empty()).collect();
    let dirs = &dirs[..dirs.len().saturating_sub(1)];
    let has = |d: &str| dirs.contains(&d);
    if path.contains("/.claude/projects/") && name.ends_with(".jsonl") {
        return "a transcript".into();
    }
    if has("tool-results") || has("tool_results") || has("scratchpad") {
        return "tool output".into();
    }
    if has("tasks") {
        return "task output".into();
    }
    match dirs.iter().rev().find(|d| !hashy(d) && !d.starts_with('.') && d.len() <= 24) {
        Some(d) => format!("{d}/…"),
        None => "a file".into(),
    }
}

/// A tool call as a few words of what it does (`Running cargo test`,
/// `Reading layout.rs`, a Bash or Agent call's own `description`); None
/// for a call that says nothing (a todo list, an empty name).
pub fn phrase(name: &str, input: &Value) -> Option<String> {
    let s = |k: &str| input.get(k).and_then(Value::as_str).map(str::trim).filter(|v| !v.is_empty());
    let first = |ks: &[&str]| ks.iter().find_map(|k| s(k));
    let at = |p: &str| format!("@{}", p.trim_start_matches('@'));
    Some(match name {
        "" | "TodoWrite" | "TaskCreate" | "TaskUpdate" | "TaskList" | "TaskGet" => return None,
        "Bash" => s("description").map(str::to_string).unwrap_or_else(|| format!("Running {}", command_head(s("command").unwrap_or("")))),
        "mcp__pty__run" => format!("Running {}", command_head(s("command").unwrap_or(""))),
        "mcp__pty__spawn_pty" => match first(&["name", "pty"]) {
            Some(n) => format!("Starting {n}"),
            None => format!("Starting {}", s("command").map_or_else(|| "a shell".to_string(), command_head)),
        },
        "Read" => format!("Reading {}", first(&["file_path", "path"]).map_or("a file".into(), file_label)),
        "Edit" | "MultiEdit" | "NotebookEdit" => format!("Editing {}", first(&["file_path", "notebook_path"]).map_or("a file".into(), file_label)),
        "Write" => format!("Writing {}", s("file_path").map_or("a file".into(), file_label)),
        "Grep" => format!("Searching for {}", s("pattern").unwrap_or("text")),
        "Glob" => format!("Finding {}", s("pattern").unwrap_or("files")),
        "Agent" | "Task" => s("description").map_or_else(|| "Delegating".to_string(), str::to_string),
        "Workflow" => "Running workflow".into(),
        "WebSearch" => format!("Searching the web for {}", s("query").unwrap_or("it")),
        "WebFetch" => {
            let url = s("url").unwrap_or("");
            let host = url.split("://").nth(1).unwrap_or(url).split('/').next().unwrap_or("");
            if host.is_empty() { "Reading the web".into() } else { format!("Reading {host}") }
        }
        "mcp__tmux__message_agent" => format!("Messaging {}", at(first(&["pane", "target", "to", "agent", "handle"]).unwrap_or("an agent"))),
        "mcp__tmux__read_pane" => format!("Checking {}", at(first(&["pane", "target", "handle"]).unwrap_or("a pane"))),
        "Skill" => format!("Using {}", s("skill").unwrap_or("a skill")),
        "SendMessage" => match first(&["to", "recipient", "agent", "name", "agent_id"]) {
            Some(to) if agent_id(to) => "Messaging a subagent".into(),
            Some(to) => format!("Messaging {to}"),
            None => "Messaging an agent".into(),
        },
        "Monitor" => "Watching for an event".into(),
        "TaskStop" | "KillShell" | "KillBash" => "Stopping a task".into(),
        "TaskOutput" | "BashOutput" => "Checking a task's output".into(),
        "ToolSearch" => "Loading tools".into(),
        "AskUserQuestion" => "Asking you".into(),
        "ExitPlanMode" | "EnterPlanMode" => "Planning".into(),
        _ => match name.strip_prefix("mcp__").and_then(|r| r.split_once("__")) {
            Some((server, tool)) => {
                let server = server.strip_prefix("claude_ai_").unwrap_or(server);
                let server = if server.starts_with("plugin_") { server.rsplit('_').next().unwrap_or(server) } else { server };
                let tool = tool.strip_prefix(&format!("{server}_")).unwrap_or(tool);
                format!("{}: {}", server.replace('_', " "), tool.replace('_', " "))
            }
            None => words_of(name),
        },
    })
}

/// One line, no control characters, single spaces, capitalized, no
/// trailing period.
pub fn tidy(s: &str) -> String {
    let one = crate::text::sanitize(s).split_whitespace().collect::<Vec<_>>().join(" ");
    let one = one.trim_end_matches('.');
    let mut cs = one.chars();
    match cs.next() {
        Some(c) => c.to_uppercase().chain(cs).collect(),
        None => String::new(),
    }
}

#[cfg(test)]
mod tests;
