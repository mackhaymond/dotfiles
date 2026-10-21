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
//!   path is cached per session id. A Codex agent's is its rollout, whose
//!   path the window carries (`@agent_rollout`, which agent-tab-indicator.sh
//!   sets; a snapshot field, `Agent::rollout`).
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
//! - **Codex** ([`codex_blurb`]): the same walk back to the turn's
//!   `task_started`: the in-progress `update_plan` step, else the latest
//!   call ([`codex_call`]: a `function_call`'s JSON arguments; a
//!   `custom_tool_call` — `apply_patch`'s patch, or `exec`'s code-mode
//!   JavaScript, whose last `tools.<name>(…)` call that says something is
//!   read, each from its own argument span; a `local_shell_call`; a
//!   `web_search_call`): a shell → `Running <head>` (an argv's `-lc` script
//!   unwrapped), a patch → `Editing <first file>`, MCP (a name, or a
//!   `mcp__<server>__` namespace) → `<server>: <tool>`, the web, images,
//!   other agents; reasoning only → `Thinking…`; a turn already over
//!   (`task_complete`, `turn_aborted`) → nothing (the run detail). The same
//!   scrubbing of secrets and hashed names.
//! - **OpenCode**: no transcript reader, so no blurb (the row keeps the run
//!   detail). It never falls through to the Claude lookup, which would lend
//!   it the words of a live Claude session naming the same window (one in
//!   another pane of it).

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
            a.blurb = if !working(a) || a.kind == "opencode" {
                None
            } else if a.kind == "codex" {
                // Its rollout path rides on the window (`@agent_rollout`).
                (!a.rollout.is_empty()).then(|| self.read(Path::new(&a.rollout), codex_blurb)).flatten()
            } else {
                self.blurb_for(&a.session, &a.window_id)
            };
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
        self.read(path, blurb)
    }

    /// A transcript's blurb by `parse` (Claude's [`blurb`] or Codex's
    /// [`codex_blurb`]): read only when its (len, mtime) changed.
    pub fn read(&mut self, path: &Path, parse: fn(&str, bool) -> Option<String>) -> Option<String> {
        let st = stamp(path)?;
        if let Some((s, b)) = self.blurbs.get(path) {
            if *s == st {
                return b.clone();
            }
        }
        self.reads += 1;
        let b = read_tail(path, TAIL).and_then(|(t, whole)| parse(&t, whole));
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
    // (A dot suffix does not hide one: `AbCd…XYZ.sh`'s stem is checked.)
    let stem = w.split('.').next().unwrap_or(w);
    let long = stem.len() >= 20 && stem.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-');
    let kinds = [stem.chars().any(|c| c.is_ascii_uppercase()), stem.chars().any(|c| c.is_ascii_lowercase()), stem.chars().any(|c| c.is_ascii_digit())];
    long && kinds.iter().filter(|k| **k).count() >= 2
}

/// The first words of a shell command line: past `cd`, `export`, env
/// assignments and the like, the command's name (a path's last part) and up
/// to two plain words after it (`cargo test`, `git status`); never a word
/// that may be a secret ([`secret`]: the head stops before it). A shell
/// loop is `a loop`, an `if` / `case` `a script`.
pub fn command_head(cmd: &str) -> String {
    // `zsh -fc '…'`, `bash -lc "…"`: the script's own head.
    if let Some(script) = shell_script(cmd) {
        return command_head(&script);
    }
    const SKIP: [&str; 12] = ["cd", "export", "unset", "set", "source", ".", "pushd", "popd", "true", ":", "local", "sleep"];
    const PREFIX: [&str; 6] = ["env", "sudo", "time", "exec", "nohup", "command"];
    for part in cmd.split(['\n', ';', '|', '&']) {
        // (`cd x && zsh -c '…'`: the script from here on, quotes and all.)
        let at = part.as_ptr() as usize - cmd.as_ptr() as usize;
        if let Some(script) = shell_script(&cmd[at..]) {
            return command_head(&script);
        }
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
        // An interpreter running a script file: the script (`zsh -f
        // /x/clean.zsh` → `clean.zsh`). Inline code or a module (`-c`, `-e`,
        // `-m`, `--eval`) is the interpreter alone; a flag's own value
        // (`-r x.js`) is never the script; a name that is not plain, or
        // whose stem may be a secret, is the interpreter alone too.
        const INTERP: [&str; 12] = ["sh", "bash", "zsh", "fish", "dash", "python", "python3", "node", "ruby", "perl", "bun", "deno"];
        if INTERP.contains(&name) {
            let mut look = words.clone();
            while let Some(w) = look.next() {
                let short = w.starts_with('-') && !w.starts_with("--");
                if matches!(w, "--eval" | "--print" | "--command") || (short && w.len() >= 2 && w.ends_with(['c', 'e', 'm', 'p'])) {
                    return name.to_string();
                }
                if matches!(w, "-r" | "--require" | "--import" | "--loader" | "-W" | "-X") {
                    look.next();
                    continue;
                }
                if w.starts_with('-') {
                    continue;
                }
                let base = w.trim_matches(['\'', '"']).trim_end_matches('/');
                let base = base.rsplit('/').next().unwrap_or(base);
                let stem = base.split('.').next().unwrap_or(base);
                let plain = !base.is_empty() && base.chars().all(|c| c.is_ascii_alphanumeric() || "._-".contains(c));
                if (w.contains('/') || w.contains('.')) && plain && !secret(base) && !secret(stem) {
                    return base.to_string();
                }
                break;
            }
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

/// The script of a shell run as `sh|bash|zsh|fish|dash -…c SCRIPT` (one
/// level of quotes taken off), when `cmd` is one.
fn shell_script(cmd: &str) -> Option<String> {
    let cmd = cmd.trim_start();
    let first = cmd.split_whitespace().next()?;
    if !matches!(first.rsplit('/').next(), Some("sh" | "bash" | "zsh" | "fish" | "dash")) {
        return None;
    }
    let rest = cmd[first.len()..].trim_start();
    let flag = rest.split_whitespace().next()?;
    if !(flag.starts_with('-') && flag.ends_with('c') && flag[1..].chars().all(|c| c.is_ascii_alphabetic())) {
        return None;
    }
    let script = rest[flag.len()..].trim_start();
    let script = match script.chars().next() {
        // Up to the matching quote (a double-quoted one's escapes read),
        // never what follows it (`| tail`, `&& echo ok`).
        Some(q @ ('\'' | '"')) => {
            let mut out = String::new();
            let mut esc = false;
            for c in script[1..].chars() {
                if esc {
                    if !matches!(c, '"' | '\\' | '$' | '`') {
                        out.push('\\');
                    }
                    out.push(c);
                    esc = false;
                } else if c == '\\' && q == '"' {
                    esc = true;
                } else if c == q {
                    break;
                } else {
                    out.push(c);
                }
            }
            out
        }
        _ => script.to_string(),
    };
    (!script.trim().is_empty()).then_some(script)
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

// ---------------------------------------------------------------- codex

/// The blurb from a Codex rollout's tail (JSONL, `@agent_rollout`):
/// walking back from the end to the current turn's `task_started`, the
/// in-progress plan step (`update_plan`), else the latest tool call phrased
/// ([`codex_call`]), else [`THINKING`] when the turn's start is in the tail
/// (reasoning and messages only). None when the tail holds nothing to go
/// by, or when its newest turn event says the turn is over
/// (`task_complete`, `turn_aborted`): the row keeps the run detail.
pub fn codex_blurb(tail: &str, whole: bool) -> Option<String> {
    const CALLS: [&str; 5] = ["function_call", "custom_tool_call", "local_shell_call", "web_search_call", "tool_search_call"];
    let event = |line: &str, t: &str| line.contains("\"type\":\"event_msg\"") && line.contains(&format!("\"payload\":{{\"type\":\"{t}\""));
    let mut tool: Option<String> = None;
    let mut plan: Option<Option<String>> = None;
    let mut start = false;
    for line in tail.lines().rev() {
        if event(line, "task_started") {
            start = true;
            break;
        }
        if event(line, "task_complete") || event(line, "turn_aborted") {
            return None;
        }
        // Cheap tests first: a call's own record, never its (big) output.
        if !line.contains("\"type\":\"response_item\"") || !CALLS.iter().any(|c| line.contains(&format!("\"payload\":{{\"type\":\"{c}\""))) {
            continue;
        }
        let Ok(v) = json::parse(line) else { continue };
        let Some(p) = v.get("payload") else { continue };
        match codex_call(p) {
            Codex::Plan(step) => {
                plan.get_or_insert(step);
            }
            Codex::Said(s) => {
                if tool.is_none() {
                    tool = Some(s);
                }
            }
            Codex::Nothing => {}
        }
        if plan.as_ref().is_some_and(Option::is_some) {
            break;
        }
    }
    let said = plan.flatten().or(tool).or_else(|| (start || whole).then(|| THINKING.to_string()));
    said.map(|s| tidy(&s)).filter(|s| !s.is_empty())
}

/// What one Codex call says.
#[derive(Debug, PartialEq)]
pub(crate) enum Codex {
    /// A plan (`update_plan`): its in-progress step, if any.
    Plan(Option<String>),
    Said(String),
    Nothing,
}

/// One rollout call record's payload → what it says. The shapes: a
/// `function_call` (`name`, maybe a `namespace` — `mcp__<server>__` makes
/// it an MCP call — JSON `arguments`), a `custom_tool_call` (`name`,
/// free-text `input`: `apply_patch`'s patch, or `exec`'s code-mode
/// JavaScript calling `tools.<name>(…)`), a `local_shell_call`
/// (`action.command` argv), a `web_search_call` (`action.query`, or the
/// first of `action.queries`).
pub(crate) fn codex_call(p: &Value) -> Codex {
    let s = |v: &Value, k: &str| v.get(k).and_then(Value::as_str).map(str::to_string);
    let name = s(p, "name").unwrap_or_default();
    match p.get("type").and_then(Value::as_str) {
        Some("function_call") => {
            let args = s(p, "arguments").and_then(|a| json::parse(&a).ok()).unwrap_or(Value::Null);
            let ns = s(p, "namespace").unwrap_or_default();
            let full = match ns.strip_prefix("mcp__") {
                Some(server) if !name.is_empty() => format!("mcp__{}__{name}", server.trim_end_matches('_')),
                _ => name,
            };
            codex_function(&full, &args)
        }
        Some("custom_tool_call") => {
            let input = s(p, "input").unwrap_or_default();
            match name.as_str() {
                "exec" => js_calls(&input),
                "apply_patch" => Codex::Said(patch_phrase(&input)),
                _ => Codex::Said(words_of(&name)),
            }
        }
        Some("local_shell_call") => {
            let cmd = p.get("action").and_then(|a| a.get("command")).map(argv_script).unwrap_or_default();
            if cmd.trim().is_empty() {
                return Codex::Nothing;
            }
            Codex::Said(format!("Running {}", command_head(&cmd)))
        }
        Some("web_search_call") => {
            let action = p.get("action");
            let q = action
                .and_then(|a| a.get("query"))
                .and_then(Value::as_str)
                .or_else(|| action.and_then(|a| a.get("queries")).and_then(Value::as_array).and_then(|qs| qs.iter().find_map(Value::as_str)))
                .map(str::trim)
                .filter(|q| !q.is_empty());
            Codex::Said(q.map_or_else(|| "Searching the web".to_string(), |q| format!("Searching the web for {q}")))
        }
        Some("tool_search_call") => Codex::Said("Loading tools".into()),
        _ => Codex::Nothing,
    }
}

/// A command as Codex passes it: an argv (`["bash", "-lc", "cargo test"]`:
/// the `-c` / `-lc` script unwrapped; else the words joined) or a string.
fn argv_script(v: &Value) -> String {
    match v {
        Value::Str(s) => s.clone(),
        Value::Arr(a) => {
            let words: Vec<&str> = a.iter().filter_map(Value::as_str).collect();
            match words.iter().position(|w| matches!(*w, "-c" | "-lc" | "-ic" | "-lic")) {
                Some(i) if i + 1 < words.len() => words[i + 1].to_string(),
                _ => words.join(" "),
            }
        }
        _ => String::new(),
    }
}

/// A `function_call` (or a code-mode `tools.<name>(…)` call, its arguments
/// read the same way) → what it says.
fn codex_function(name: &str, args: &Value) -> Codex {
    let s = |k: &str| args.get(k).and_then(Value::as_str).map(str::trim).filter(|v| !v.is_empty()).map(str::to_string);
    let said = |t: String| Codex::Said(t);
    match name {
        "shell" | "container.exec" | "exec_command" | "shell_command" | "local_shell" => {
            let cmd = args.get("command").or_else(|| args.get("cmd")).map(argv_script).unwrap_or_default();
            if cmd.trim().is_empty() {
                // (A command it does not spell out says nothing.)
                return Codex::Nothing;
            }
            said(format!("Running {}", command_head(&cmd)))
        }
        "apply_patch" => said(patch_phrase(&s("input").or_else(|| s("patch")).unwrap_or_default())),
        "update_plan" => {
            let steps = args.get("plan").and_then(Value::as_array).unwrap_or(&[]);
            let cur = steps.iter().find(|st| st.get("status").and_then(Value::as_str) == Some("in_progress"));
            Codex::Plan(cur.and_then(|st| st.get("step").and_then(Value::as_str)).map(str::to_string))
        }
        "view_image" => said("Viewing an image".into()),
        "write_stdin" | "wait" => said("Waiting on a command".into()),
        "spawn_agent" => said(s("task_name").map_or_else(|| "Starting an agent".into(), |t| format!("Starting agent {}", t.replace('_', " ")))),
        "send_message" => said(s("target").map_or_else(|| "Messaging an agent".into(), |t| format!("Messaging {t}"))),
        "followup_task" => said(s("target").map_or_else(|| "Following up".into(), |t| format!("Following up with {t}"))),
        "wait_agent" => said("Waiting on agents".into()),
        "list_agents" => said("Checking agents".into()),
        "interrupt_agent" => said(s("target").map_or_else(|| "Interrupting an agent".into(), |t| format!("Interrupting {t}"))),
        "sleep" => said("Waiting".into()),
        "request_user_input" | "request_user_input_async" => said("Asking you".into()),
        "clock__curr_time" | "curr_time" => Codex::Nothing,
        "web__run" | "web_search" => said(s("q").or_else(|| s("query")).map_or_else(|| "Searching the web".into(), |q| format!("Searching the web for {q}"))),
        _ if name.starts_with("mcp__") => phrase(name, args).map_or(Codex::Nothing, Codex::Said),
        "" => Codex::Nothing,
        _ => said(words_of(&name.replace("__", "_"))),
    }
}

/// Code mode: the JavaScript an `exec` call runs. Every `tools.<name>(…)`
/// call is read (a `tools.<word>` not called is skipped), each its own
/// argument span only ([`call_span`]) for the fields the phrasing needs
/// (`cmd`, a patch string, `step` + `status`, `q`); the LAST that says
/// something speaks (a cell runs its calls in order). A plan with nothing
/// in progress says nothing here.
fn js_calls(src: &str) -> Codex {
    let mut last = Codex::Nothing;
    let mut rest = src;
    while let Some(i) = rest.find("tools.") {
        let after = &rest[i + "tools.".len()..];
        let name: String = after.chars().take_while(|c| c.is_ascii_alphanumeric() || *c == '_').collect();
        let tail = &after[name.len()..];
        let Some(body) = tail.strip_prefix('(') else {
            rest = tail;
            continue;
        };
        let span = call_span(body);
        rest = &body[span.len()..];
        if name.is_empty() {
            continue;
        }
        let got = match name.as_str() {
            "apply_patch" => js_string(span.trim_start()).map_or(Codex::Nothing, |p| Codex::Said(patch_phrase(&p))),
            "update_plan" => js_plan_step(span).map_or(Codex::Nothing, |s| Codex::Plan(Some(s))),
            _ => {
                // The fields the phrasing reads, as a JSON object.
                let mut kv: Vec<(String, Value)> = Vec::new();
                for k in ["cmd", "command", "q", "query", "target", "task_name", "pattern", "file_path", "path"] {
                    if let Some(v) = js_field(span, k) {
                        kv.push((k.to_string(), Value::Str(v)));
                    }
                }
                codex_function(&name, &Value::Obj(kv))
            }
        };
        if got != Codex::Nothing {
            last = got;
        }
    }
    last
}

/// A call's argument source: `body` (just past its `(`) up to the matching
/// `)`, strings and nested brackets skipped; all of it when unclosed.
fn call_span(body: &str) -> &str {
    let mut depth = 1usize;
    let mut quote: Option<char> = None;
    let mut esc = false;
    for (i, c) in body.char_indices() {
        if let Some(q) = quote {
            if esc {
                esc = false;
            } else if c == '\\' {
                esc = true;
            } else if c == q {
                quote = None;
            }
            continue;
        }
        match c {
            '"' | '\'' | '`' => quote = Some(c),
            '(' | '[' | '{' => depth += 1,
            ')' | ']' | '}' => {
                depth -= 1;
                if depth == 0 {
                    return &body[..i];
                }
            }
            _ => {}
        }
    }
    body
}

/// The string value of `key` in JS / JSON object source (`key:"…"`,
/// `"key":"…"`, `'key':'…'`).
fn js_field(src: &str, key: &str) -> Option<String> {
    for pat in [format!("\"{key}\":"), format!("'{key}':"), format!("{key}:")] {
        let mut from = 0;
        while let Some(i) = src[from..].find(&pat) {
            let at = from + i;
            // A whole key, not the tail of a longer one (`xcmd:`).
            let ok = at == 0 || !src[..at].chars().next_back().is_some_and(|c| c.is_ascii_alphanumeric() || c == '_');
            if ok {
                if let Some(v) = js_string(src[at + pat.len()..].trim_start()) {
                    return Some(v);
                }
            }
            from = at + pat.len();
        }
    }
    None
}

/// A JS / JSON string literal at the start of `src` (`"…"`, `'…'`, `` `…` ``)
/// → its value (escapes read; an unterminated one: what there is).
fn js_string(src: &str) -> Option<String> {
    let mut cs = src.chars();
    let q = cs.next().filter(|c| "\"'`".contains(*c))?;
    let mut out = String::new();
    let mut esc = false;
    for c in cs {
        if esc {
            out.push(match c {
                'n' => '\n',
                't' => '\t',
                x => x,
            });
            esc = false;
        } else if c == '\\' {
            esc = true;
        } else if c == q {
            return Some(out);
        } else {
            out.push(c);
        }
    }
    Some(out)
}

/// The in-progress step of a code-mode `update_plan({plan:[…]})` (its
/// argument span).
fn js_plan_step(span: &str) -> Option<String> {
    span.split('{').filter(|item| item.contains("in_progress")).find_map(|item| js_field(item, "step"))
}

/// An `apply_patch` patch → `Editing <file>` (its first `*** Update
/// File:`, `Add File:` → `Writing`, `Delete File:` → `Deleting`), the file
/// named as [`file_label`] names it.
fn patch_phrase(patch: &str) -> String {
    for line in patch.lines() {
        for (head, verb) in [("*** Update File:", "Editing"), ("*** Add File:", "Writing"), ("*** Delete File:", "Deleting")] {
            if let Some(path) = line.trim().strip_prefix(head) {
                return format!("{verb} {}", file_label(path.trim()));
            }
        }
    }
    "Editing files".into()
}

#[cfg(test)]
mod tests;
