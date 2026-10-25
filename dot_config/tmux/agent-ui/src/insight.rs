//! What each agent has been up to, from its own transcript: the files it
//! edited (and how much of that is still uncommitted), what it is doing, and
//! how full its context is. The data behind an agent card's diff badge
//! (`+48 −12 · 3f`) and its "doing" line, behind the diff popup
//! (`agent-ui insight-files`), and `agent-ui insight`'s context column (no
//! card draws the context). No rendering here.
//!
//! Several agents often share ONE checkout (no worktrees), so `git diff` of
//! a folder is everyone's work: an agent's changes are the files ITS
//! transcript says it edited, still dirty.
//!
//! - **Which transcript** ([`Engine::update`]). Claude: the window's session
//!   id as the pins see it ([`crate::pins::sessions`]: the live
//!   `~/.claude/sessions/<pid>.json` naming the window), then
//!   `~/.claude/projects/<slug of the window's cwd>/<sid>.jsonl`, else one
//!   scan of `~/.claude/projects/*/<sid>.jsonl` (cached per id; a miss is
//!   retried after [`RETRY`] s). Its subagents' transcripts
//!   (`<sid>/subagents/*.jsonl`) add their edits (Mack's agents delegate
//!   most edits). Codex: `@agent_rollout`, else the rollout named
//!   `rollout-*-<@agent_session_id>.jsonl` under `~/.codex/sessions` (its
//!   spawned agents' rollouts are not followed). OpenCode: none.
//! - **Incremental** ([`Transcript::sync`]). Each file is read from where the
//!   last read stopped (just past its last complete line, so a half-written
//!   last line is read again whole next time); the file's (inode, len,
//!   mtime) says when: unchanged → nothing read, grown → the new bytes,
//!   shrunk / replaced / rewritten in place → parsed again from scratch.
//! - **Bounded.** On first sight (or after more than [`FIRST_MAX`] new bytes)
//!   only the last [`FIRST_MAX`] bytes are read: a 100 MB transcript costs
//!   a 32 MB read, once, [`CHUNK`] at a time (never the whole file in
//!   memory). The price: an edit older than that is not
//!   attributed (hours back in a session that long; usually committed). All
//!   of it runs on the live sidebar's own worker thread ([`Insights`]), never
//!   in a frame; a line is JSON-parsed only when a cheap substring test says
//!   it can matter (an edit or todo tool call, an edit's error result); the
//!   prompt and usage candidates are only remembered by place, and the end
//!   of the read parses the newest that says something ([`Cand`]).
//! - **What it extracts** ([`Facts`]). `touched`: the absolute paths of every
//!   Edit / Write / MultiEdit / NotebookEdit (Claude; a call whose result is
//!   an error does not count) or Codex `FileChange` item (else its
//!   `apply_patch` patches' `*** Update/Add/Delete File:` / `*** Move to:`
//!   lines), first edit first. `doing`: the in-progress item of the latest
//!   todo list (TodoWrite, TaskUpdate; Codex `update_plan`), else the last
//!   real prompt ([`clean_prompt`]: no tool results, meta / compact-summary
//!   records, task notifications or peer messages; `<pasted_content>` tags
//!   taken off; first line, whitespace collapsed). Context: Claude's latest
//!   main-chain assistant `usage` (input + cache read + cache creation) of a
//!   window ([`claude_window`]) of 1M for a `[1m]` model id, a Fable model, or
//!   once usage was seen past 200k, else 200k; Codex's latest `token_count`
//!   (`last_token_usage.input_tokens` of `model_context_window`).
//! - **Diff stat** ([`Engine::update`]). The touched files grouped by repo
//!   root ([`crate::git::git_dirs`], cached [`ROOT_TTL`] s); per repo ONE
//!   `git diff --numstat -z --no-renames HEAD -- <files>` plus `git ls-files
//!   --others --exclude-standard -- <files>` (an untracked file counts all
//!   its lines added), never with optional locks, killed after
//!   [`GIT_TIMEOUT`]. A repo is asked again only when something it could
//!   say changed (the files' len/mtime, its index, HEAD or HEAD reflog) and
//!   at most every [`DIFF_EVERY`] s; a half answer (one question failed) is
//!   used but asked again. A file outside any repo, ignored, or committed
//!   since (clean again) is simply not dirty; and once a CURRENT answer
//!   showed a file clean after an agent's last settled edit of it (its
//!   result read), its later changes are someone else's until that agent
//!   edits it again ([`Seen`]): A's committed edit never lends A the diff B
//!   makes in the same file afterwards.
//! - **Doing, context, more.** A real prompt newer than the todo list's
//!   last change hides that list's in-progress item (a new job); another
//!   agent's `[message from @…]` is no prompt. A model switch or a
//!   compaction forgets the usage peak (a 1M window inferred from it).
//! - **Panics.** The worker catches a panic in its engine, never stays
//!   busy, starts the engine afresh, and after [`PANICS_MAX`] in a row
//!   turns insights off.

use crate::json::{self, Value};
use crate::pins::{self, Procs};
use crate::tmux::{Snapshot, Tmux, Window};
use crate::view::ViewModel;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::hash::{Hash, Hasher};
use std::io::{Read, Seek, SeekFrom};
use std::os::unix::fs::{FileExt, MetadataExt};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{mpsc, Arc, Mutex};
use std::time::{Duration, Instant, SystemTime};

/// The most of a transcript read at once (first sight, or a burst).
pub const FIRST_MAX: u64 = 32 << 20;
/// A repo's diff is asked at most this often (seconds).
pub const DIFF_EVERY: f64 = 5.0;
/// The live sidebar hands its worker a snapshot at most this often.
pub const EVERY: Duration = Duration::from_secs(2);
/// How long a snapshot handed to the worker counts as [`Insights::pending`].
pub const PENDING_FOR: Duration = Duration::from_secs(10);
/// A transcript not found is looked for again after this (seconds).
pub const RETRY: f64 = 10.0;
/// How long a folder → repo root lookup is trusted (seconds).
pub const ROOT_TTL: f64 = 30.0;
/// A transcript's cache and its attribution history outlive the last
/// update that used them by this long (seconds). (2026-10-22: dropped after
/// ONE pass that missed the window, a sessions file read mid-rewrite say,
/// every transcript of it was read again from scratch, up to FIRST_MAX
/// each, and a committed file went back to the agent that had edited it.)
pub const KEEP_FOR: f64 = 60.0;
/// A git command still running after this is killed.
pub const GIT_TIMEOUT: Duration = Duration::from_secs(3);
/// Claude's context windows.
pub const WINDOW_200K: u64 = 200_000;
pub const WINDOW_1M: u64 = 1_000_000;
/// A prompt is kept to this many characters (the card clips it further).
const PROMPT_MAX: usize = 200;
/// Edit calls awaiting their result, at most.
const PENDING_MAX: usize = 64;
/// An untracked file's lines are counted in at most this many bytes.
const UNTRACKED_MAX: u64 = 8 << 20;

/// Whose transcript format.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Claude,
    Codex,
}

impl Kind {
    pub fn name(self) -> &'static str {
        match self {
            Kind::Claude => "claude",
            Kind::Codex => "codex",
        }
    }
}

/// Uncommitted lines in an agent's touched files.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct DiffStat {
    pub added: u64,
    pub removed: u64,
    /// Dirty files.
    pub files: usize,
}

impl DiffStat {
    /// `+48 −12 · 3f`.
    pub fn badge(&self) -> String {
        format!("+{} −{} · {}f", self.added, self.removed, self.files)
    }
}

/// One agent window's insight.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Insight {
    pub kind: Kind,
    pub transcript: PathBuf,
    /// What it is doing: the in-progress todo, else the last prompt.
    pub doing: Option<String>,
    /// `doing`'s two halves apart (a card says a prompt as yours).
    pub todo: Option<String>,
    pub prompt: Option<String>,
    /// Tokens in its context now (None: no usage seen yet).
    pub ctx_used: Option<u64>,
    pub ctx_window: u64,
    /// Its dirty touched files' lines (None: none dirty, or not known yet).
    pub diff: Option<DiffStat>,
    /// `diff` is an answer, not a guess: every touched file is outside any
    /// repo, dirty now, or shown clean by a whole git answer current since
    /// the agent's last edit of it was first seen. False while a repo has
    /// not answered (yet). (An agent that touched nothing: true, clean.)
    /// The stale-agent sweep ([`crate::view::stale`]) trusts only this.
    pub diff_known: bool,
    /// Its touched files still dirty, absolute, first edit first.
    pub dirty_files: Vec<PathBuf>,
    /// How many files it edited in all (dirty or not).
    pub touched: usize,
}

impl Insight {
    /// How full its context is, 0.0..=1.0 (None: no usage seen yet).
    pub fn ctx_fraction(&self) -> Option<f64> {
        let w = self.ctx_window.max(1) as f64;
        self.ctx_used.map(|u| (u as f64 / w).min(1.0))
    }
}

// ---------------------------------------------------------------- facts

/// What a transcript has said so far (kept across incremental reads).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Facts {
    /// Every path edited, first edit first...
    order: Vec<PathBuf>,
    /// ...and its edits that went through (a failed call takes its own back).
    count: HashMap<PathBuf, i32>,
    /// Claude edit calls whose result is not read yet: (tool_use id, path).
    pending: Vec<(String, PathBuf)>,
    /// The latest todo list's in-progress items: (id, words).
    tasks: Vec<(String, String)>,
    /// The last real prompt, cleaned.
    pub prompt: Option<String>,
    /// Lines taken so far (a line's place, to say what came after what).
    seq: u64,
    /// The places of the latest todo-list change, of the prompt, and of
    /// the last compaction (Claude's `compact_boundary`).
    todo_at: u64,
    prompt_at: u64,
    compact_at: u64,
    pub ctx_used: Option<u64>,
    /// The model id last seen (Claude: `requestedModel`, else the message's).
    pub model: String,
    /// The most context seen in use (Claude: tells a 1M window).
    pub peak: u64,
    /// Codex: `model_context_window`.
    pub window: Option<u64>,
    /// Codex: the turn's cwd (a relative patch path is under it).
    pub cwd: String,
    /// Codex: paths from `apply_patch` text, used only while no `FileChange`
    /// item was seen (newer Codex reports every applied patch as one).
    patched: Vec<PathBuf>,
    file_changes: bool,
}

impl Facts {
    /// The files edited, first edit first.
    pub fn touched(&self) -> Vec<PathBuf> {
        let mut out: Vec<PathBuf> = self.order.iter().filter(|p| self.count.get(*p).is_some_and(|n| *n > 0)).cloned().collect();
        if !self.file_changes {
            let have: HashSet<PathBuf> = out.iter().cloned().collect();
            out.extend(self.patched.iter().filter(|p| !have.contains(*p)).cloned());
        }
        out
    }

    /// The in-progress todo, if the latest list has one and no real prompt
    /// came after it (a new prompt is a new job: an item left in progress
    /// before it is stale).
    pub fn todo(&self) -> Option<&str> {
        self.tasks.last().filter(|_| self.prompt.is_none() || self.todo_at > self.prompt_at).map(|(_, t)| t.as_str())
    }

    /// Each touched file with its SETTLED edits: those whose result was read
    /// (a call still awaiting its result, a permission prompt say, may not
    /// have written yet). A Codex patch path known only from its text: 1.
    pub fn edits(&self) -> Vec<(PathBuf, u32)> {
        self.touched()
            .into_iter()
            .map(|p| {
                let open = self.pending.iter().filter(|(_, q)| *q == p).count() as i32;
                let n = self.count.get(&p).map_or(1, |n| (n - open).max(0));
                (p, n as u32)
            })
            .collect()
    }

    /// A model's usage seen: a different model (a switch) starts its peak
    /// afresh, so a 1M window inferred from the old one's usage goes.
    fn model_usage(&mut self, used: u64, model: String) {
        if model != self.model {
            self.peak = 0;
            self.model = model;
        }
        self.usage(used);
    }

    /// The todo list changed (at the line being taken).
    fn set_tasks(&mut self, tasks: Vec<(String, String)>) {
        self.tasks = tasks;
        self.todo_at = self.seq;
    }

    /// The in-progress todo, else the last prompt.
    pub fn doing(&self) -> Option<String> {
        self.todo().map(str::to_string).or_else(|| self.prompt.clone())
    }

    pub fn ctx_window(&self, kind: Kind) -> u64 {
        match kind {
            Kind::Claude => claude_window(&self.model, self.peak),
            Kind::Codex => self.window.unwrap_or(WINDOW_200K),
        }
    }

    fn edit(&mut self, path: PathBuf, delta: i32) {
        let n = self.count.entry(path.clone()).or_insert_with(|| {
            self.order.push(path);
            0
        });
        *n += delta;
    }

    fn usage(&mut self, used: u64) {
        self.ctx_used = Some(used);
        self.peak = self.peak.max(used);
    }
}

/// Claude's context window for `model` (`claude-opus-5-5[1m]`): 1M for a
/// `[1m]` id or a Fable model (1M natively), or once more than 200k was
/// seen in use (an id without its suffix, from before `requestedModel`);
/// else 200k.
pub fn claude_window(model: &str, peak: u64) -> u64 {
    if model.ends_with("[1m]") || model.contains("fable") || peak > WINDOW_200K {
        WINDOW_1M
    } else {
        WINDOW_200K
    }
}

fn s<'a>(v: &'a Value, k: &str) -> Option<&'a str> {
    v.get(k).and_then(Value::as_str)
}

fn n(v: &Value, k: &str) -> u64 {
    v.get(k).and_then(Value::as_f64).map_or(0, |x| x.max(0.0) as u64)
}

/// The tool names a Claude assistant line must name to be parsed.
const CLAUDE_TOOLS: [&str; 7] = [
    "\"name\":\"Edit\"", "\"name\":\"Write\"", "\"name\":\"MultiEdit\"", "\"name\":\"NotebookEdit\"", "\"name\":\"TodoWrite\"",
    "\"name\":\"TaskCreate\"", "\"name\":\"TaskUpdate\"",
];

/// The first `"type":"…"` value of a JSONL record: a Claude user record's
/// and every Codex record's own type (their `type` key comes before any
/// nested object); a Claude assistant record's message type (`message`).
fn first_type(line: &str) -> &str {
    const K: &str = "\"type\":\"";
    let Some(i) = line.find(K) else { return "" };
    let rest = &line[i + K.len()..];
    &rest[..rest.find('"').unwrap_or(0)]
}

/// A line whose worth only the newest of its kind decides: the end of a
/// read parses the newest of each, walking back past what says nothing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Cand {
    /// May hold the context in use (a Claude assistant record, a Codex
    /// `token_count`).
    Usage,
    /// May be a real prompt.
    Prompt,
}

/// At most this many candidates of EACH kind are kept per read (the
/// newest; a run of usage records never pushes the prompts out).
const CANDS_MAX: usize = 1024;

/// A read's candidates, by kind, oldest first: (the line's place, how to
/// fetch it).
#[derive(Debug)]
pub struct Cands<T> {
    kinds: [Vec<(u64, T)>; 2],
}

impl<T> Default for Cands<T> {
    fn default() -> Self {
        Cands { kinds: [Vec::new(), Vec::new()] }
    }
}

impl<T> Cands<T> {
    fn push(&mut self, c: Cand, seq: u64, at: T) {
        let v = &mut self.kinds[c as usize];
        v.push((seq, at));
        if v.len() >= 2 * CANDS_MAX {
            v.drain(..CANDS_MAX);
        }
    }
}

/// One whole Claude line: its edits, todos and edit results taken at once;
/// what it may say of the usage or prompt returned for the end of the read.
/// `main`: the session's own transcript (sidechain records skipped; todos,
/// prompts and usage read); else a subagent's (its edits only, every record
/// a sidechain one).
///
/// Cheap tests pick the lines worth parsing; whatever they let through is
/// checked again parsed (a record can quote another: a Write of this very
/// file holds `"type":"user"`).
pub fn claude_line(f: &mut Facts, line: &str, main: bool) -> Option<Cand> {
    f.seq += 1;
    match first_type(line) {
        // A compaction: the context starts over (and so does its peak).
        "system" if main && line.contains("\"subtype\":\"compact_boundary\"") => {
            (f.compact_at, f.ctx_used, f.peak) = (f.seq, None, 0);
            None
        }
        "user" if line.contains("\"type\":\"tool_result\"") => {
            if !f.pending.is_empty() {
                claude_results(f, line);
            }
            None
        }
        "user" => main.then_some(Cand::Prompt),
        "message" if line.contains("\"type\":\"assistant\"") => {
            if CLAUDE_TOOLS.iter().any(|t| line.contains(t)) {
                if let Ok(v) = json::parse(line) {
                    claude_calls(f, &v, main);
                }
            }
            main.then_some(Cand::Usage)
        }
        _ => None,
    }
}

/// A Claude candidate, parsed: whether it settled its kind.
fn claude_decide(f: &mut Facts, c: Cand, v: &Value, main: bool) -> bool {
    match c {
        Cand::Usage => claude_usage(v, main).map(|(u, m)| f.model_usage(u, m)),
        Cand::Prompt => claude_prompt(v).map(|p| f.prompt = Some(p)),
    }
    .is_some()
}

/// The end of a read: the newest usage and prompt among `cands`, each
/// parsed only as far back as it takes (a usage never from before the last
/// compaction); `text` fetches a candidate's line.
fn decide<T>(f: &mut Facts, kind: Kind, main: bool, cands: &Cands<T>, text: impl Fn(&T) -> Option<String>) {
    for c in [Cand::Usage, Cand::Prompt] {
        for (seq, at) in cands.kinds[c as usize].iter().rev() {
            if c == Cand::Usage && *seq < f.compact_at {
                break;
            }
            let Some(v) = text(at).and_then(|t| json::parse(&t).ok()) else { continue };
            let done = match kind {
                Kind::Claude => claude_decide(f, c, &v, main),
                Kind::Codex => codex_decide(f, c, &v),
            };
            if done {
                if c == Cand::Prompt {
                    f.prompt_at = *seq;
                }
                break;
            }
        }
    }
}

/// Feed whole lines of a Claude transcript ([`claude_line`], then the
/// newest usage and prompt).
pub fn feed_claude(f: &mut Facts, chunk: &str, main: bool) {
    let mut cands = Cands::default();
    for l in chunk.lines() {
        if let Some(c) = claude_line(f, l, main) {
            cands.push(c, f.seq, l);
        }
    }
    decide(f, Kind::Claude, main, &cands, |l| Some(l.to_string()));
}

/// A main-chain assistant record's context in use and model: None for a
/// sidechain record, or one with no usage (a synthetic message).
fn claude_usage(v: &Value, main: bool) -> Option<(u64, String)> {
    if s(v, "type") != Some("assistant") || main && v.get("isSidechain").and_then(Value::as_bool) == Some(true) {
        return None;
    }
    let m = v.get("message")?;
    let u = m.get("usage")?;
    let used = n(u, "input_tokens") + n(u, "cache_read_input_tokens") + n(u, "cache_creation_input_tokens");
    let model = s(v, "requestedModel").or_else(|| s(m, "model")).unwrap_or("");
    (used > 0 && model != "<synthetic>").then(|| (used, model.to_string()))
}

/// An assistant record's edit and todo calls.
fn claude_calls(f: &mut Facts, v: &Value, main: bool) {
    if s(v, "type") != Some("assistant") || main && v.get("isSidechain").and_then(Value::as_bool) == Some(true) {
        return;
    }
    if let Some((used, model)) = claude_usage(v, main) {
        if main {
            f.model_usage(used, model);
        }
    }
    let blocks = v.get("message").and_then(|m| m.get("content")).and_then(Value::as_array).unwrap_or(&[]);
    for b in blocks.iter().filter(|b| s(b, "type") == Some("tool_use")) {
        let name = s(b, "name").unwrap_or("");
        let input = b.get("input").unwrap_or(&Value::Null);
        match name {
            "Edit" | "Write" | "MultiEdit" | "NotebookEdit" => {
                let Some(p) = s(input, "file_path").or_else(|| s(input, "notebook_path")).filter(|p| p.starts_with('/')) else { continue };
                let path = PathBuf::from(p);
                f.edit(path.clone(), 1);
                if let Some(id) = s(b, "id") {
                    f.pending.push((id.to_string(), path));
                    let excess = f.pending.len().saturating_sub(PENDING_MAX);
                    f.pending.drain(..excess);
                }
            }
            "TodoWrite" if main => {
                let todos = input.get("todos").and_then(Value::as_array).unwrap_or(&[]);
                let cur = todos.iter().find(|t| s(t, "status") == Some("in_progress"));
                let words = cur.and_then(|t| s(t, "activeForm").or_else(|| s(t, "content"))).map(str::trim).filter(|t| !t.is_empty());
                f.set_tasks(words.map(|w| vec![("todo".to_string(), w.to_string())]).unwrap_or_default());
            }
            "TaskCreate" | "TaskUpdate" if main => {
                let id = input.get("taskId").or_else(|| input.get("id")).and_then(Value::scalar_string).unwrap_or_default();
                let mut tasks = f.tasks.clone();
                match s(input, "status") {
                    Some("in_progress") => {
                        let words = s(input, "activeForm").or_else(|| s(input, "subject")).map(str::trim).filter(|w| !w.is_empty());
                        tasks.retain(|(i, _)| *i != id);
                        if let Some(w) = words {
                            tasks.push((id, w.to_string()));
                        }
                    }
                    Some(_) => tasks.retain(|(i, _)| *i != id),
                    None => continue,
                }
                f.set_tasks(tasks);
            }
            _ => {}
        }
    }
}

/// A tool-result record: an edit that failed takes its count back; every
/// result read settles its call.
fn claude_results(f: &mut Facts, line: &str) {
    if line.contains("\"is_error\":true") {
        if let Ok(v) = json::parse(line) {
            let blocks = v.get("message").and_then(|m| m.get("content")).and_then(Value::as_array).unwrap_or(&[]);
            for b in blocks {
                if s(b, "type") != Some("tool_result") || b.get("is_error").and_then(Value::as_bool) != Some(true) {
                    continue;
                }
                let id = s(b, "tool_use_id").unwrap_or("");
                if let Some(i) = f.pending.iter().position(|(p, _)| p == id) {
                    let (_, path) = f.pending.remove(i);
                    f.edit(path, -1);
                }
            }
        }
    }
    f.pending.retain(|(id, _)| !line.contains(id.as_str()));
}

/// A user record that is a real prompt → the prompt, cleaned: not meta, a
/// sidechain, a compact summary, transcript-only, a task notification or a
/// peer's message (`origin.kind` other than `human`), nor a tool result.
pub fn claude_prompt(v: &Value) -> Option<String> {
    let yes = |k: &str| v.get(k).and_then(Value::as_bool) == Some(true);
    if s(v, "type") != Some("user")
        || yes("isMeta")
        || yes("isSidechain")
        || yes("isCompactSummary")
        || yes("isVisibleInTranscriptOnly")
        || v.get("origin").and_then(|o| s(o, "kind")).is_some_and(|k| k != "human")
    {
        return None;
    }
    let text = match v.get("message").and_then(|m| m.get("content"))? {
        Value::Str(t) => t.clone(),
        Value::Arr(blocks) => {
            if blocks.iter().any(|b| s(b, "type") == Some("tool_result")) {
                return None;
            }
            blocks.iter().filter(|b| s(b, "type") == Some("text")).filter_map(|b| s(b, "text")).collect::<Vec<_>>().join("\n")
        }
        _ => return None,
    };
    clean_prompt(&text)
}

/// A prompt as one short line: `<pasted_content …>` tags taken off (the
/// pasted text stays); a slash command as `/name args`; any other text that
/// opens with a tag (`<task-notification>`, `<local-command-stdout>`,
/// `<bash-input>`, …: a wrapper, not words typed) is none. Then the first
/// non-blank line, whitespace collapsed, at most [`PROMPT_MAX`] characters.
pub fn clean_prompt(text: &str) -> Option<String> {
    let mut t = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(i) = [rest.find("<pasted_content"), rest.find("</pasted_content>")].into_iter().flatten().min() {
        t.push_str(&rest[..i]);
        let tail = &rest[i..];
        rest = tail.find('>').map_or("", |j| &tail[j + 1..]);
    }
    t.push_str(rest);
    let t = t.trim_start();
    let t = if let Some(name) = tag(t, "command-name") {
        format!("{name} {}", tag(t, "command-args").unwrap_or(""))
    } else if t.starts_with('<') && t[1..].starts_with(|c: char| c.is_ascii_alphabetic()) {
        return None;
    } else {
        t.to_string()
    };
    let line = t.lines().find(|l| !l.trim().is_empty())?;
    // Another agent's message (tmux `message_agent`'s wrapper): not Mack's.
    if line.trim_start().starts_with("[message from @") {
        return None;
    }
    let one = crate::text::sanitize(line).split_whitespace().collect::<Vec<_>>().join(" ");
    let one: String = one.chars().take(PROMPT_MAX).collect();
    (!one.is_empty()).then_some(one)
}

/// The text of `<name>…</name>` in `t`.
fn tag<'a>(t: &'a str, name: &str) -> Option<&'a str> {
    let open = format!("<{name}>");
    let i = t.find(&open)? + open.len();
    let j = t[i..].find(&format!("</{name}>"))?;
    Some(t[i..i + j].trim())
}

/// One whole Codex line, as [`claude_line`]: its context, file changes,
/// patches and plan taken at once; a `token_count` or user message
/// returned for the end of the read.
pub fn codex_line(f: &mut Facts, line: &str) -> Option<Cand> {
    f.seq += 1;
    let top = first_type(line);
    let event = top == "event_msg";
    if event && line.contains("\"type\":\"token_count\"") {
        return line.contains("\"last_token_usage\"").then_some(Cand::Usage);
    }
    if event && (line.contains("\"type\":\"UserMessage\"") || line.contains("\"type\":\"user_message\""))
        || top == "response_item" && line.contains("\"role\":\"user\"")
    {
        return Some(Cand::Prompt);
    }
    if top == "turn_context" || top == "session_meta" {
        if let Ok(v) = json::parse(line) {
            codex_context(f, &v);
        }
    } else if event && line.contains("\"type\":\"item_completed\"") && line.contains("\"type\":\"FileChange\"") {
        if let Ok(v) = json::parse(line) {
            codex_file_change(f, &v);
        }
    } else if top == "response_item" {
        if line.contains("*** Begin Patch") {
            if let Some(text) = json::parse(line).ok().and_then(|v| codex_call_text(&v)) {
                for p in patch_paths(&text, &f.cwd) {
                    if !f.patched.contains(&p) {
                        f.patched.push(p);
                    }
                }
            }
        }
        if line.contains("update_plan") {
            if let Some(p) = json::parse(line).ok().and_then(|v| v.get("payload").cloned()) {
                if let crate::sidebar::activity::Codex::Plan(step) = crate::sidebar::activity::codex_call(&p) {
                    f.set_tasks(step.map(|w| vec![("plan".to_string(), w)]).unwrap_or_default());
                }
            }
        }
    }
    None
}

/// A Codex candidate, parsed: whether it settled its kind.
fn codex_decide(f: &mut Facts, c: Cand, v: &Value) -> bool {
    match c {
        Cand::Usage => {
            let Some(p) = v.get("payload").filter(|p| s(p, "type") == Some("token_count")) else { return false };
            let Some(info) = p.get("info") else { return false };
            let used = info.get("last_token_usage").map_or(0, |l| n(l, "input_tokens"));
            if used == 0 {
                return false;
            }
            f.usage(used);
            if let Some(w) = info.get("model_context_window").and_then(Value::as_f64).filter(|w| *w > 0.0) {
                f.window = Some(w as u64);
            }
            true
        }
        Cand::Prompt => codex_prompt(v).map(|p| f.prompt = Some(p)).is_some(),
    }
}

/// Feed whole lines of a Codex rollout ([`codex_line`], then the newest
/// usage and prompt).
pub fn feed_codex(f: &mut Facts, chunk: &str) {
    let mut cands = Cands::default();
    for l in chunk.lines() {
        if let Some(c) = codex_line(f, l) {
            cands.push(c, f.seq, l);
        }
    }
    decide(f, Kind::Codex, true, &cands, |l| Some(l.to_string()));
}

/// `session_meta` / `turn_context`: the cwd, the model, a context window.
fn codex_context(f: &mut Facts, v: &Value) {
    if !matches!(s(v, "type"), Some("turn_context" | "session_meta")) {
        return;
    }
    let Some(p) = v.get("payload") else { return };
    if let Some(cwd) = s(p, "cwd").filter(|c| !c.is_empty()) {
        f.cwd = cwd.to_string();
    }
    if let Some(m) = s(p, "model").filter(|m| !m.is_empty()) {
        f.model = m.to_string();
    }
    if f.window.is_none() {
        f.window = p.get("context_window").and_then(Value::as_f64).filter(|w| *w > 0.0).map(|w| w as u64);
    }
}

/// An `item_completed` `FileChange`: `changes` maps each path to its change
/// (`update` maybe with a `move_path`, `add`, `delete`).
fn codex_file_change(f: &mut Facts, v: &Value) {
    let Some(item) = v.get("payload").and_then(|p| p.get("item")) else { return };
    if s(item, "type") != Some("FileChange") {
        return;
    }
    f.file_changes = true;
    if matches!(s(item, "status"), Some("failed" | "declined")) {
        return;
    }
    let Some(Value::Obj(changes)) = item.get("changes") else { return };
    let abs = |p: &str| if p.starts_with('/') { PathBuf::from(p) } else { Path::new(&f.cwd).join(p) };
    let mut got = Vec::new();
    for (path, ch) in changes {
        got.push(abs(path));
        if let Some(to) = s(ch, "move_path").filter(|t| !t.is_empty()) {
            got.push(abs(to));
        }
    }
    // (Every applied change is one more edit of its files.)
    got.dedup();
    for p in got {
        f.edit(p, 1);
    }
}

/// A call record's free text (a custom tool's `input`, a function's
/// `arguments`), where a patch would be.
fn codex_call_text(v: &Value) -> Option<String> {
    let p = v.get("payload")?;
    if !matches!(s(p, "type"), Some("custom_tool_call" | "function_call")) {
        return None;
    }
    s(p, "input").or_else(|| s(p, "arguments")).map(str::to_string)
}

/// The paths an `apply_patch` patch names, absolute (a relative one under
/// `cwd`). The patch may sit in a JSON or JavaScript string still escaped
/// (`\n` as two characters), so a path ends at a newline, a backslash or a
/// quote.
pub fn patch_paths(text: &str, cwd: &str) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = Vec::new();
    for head in ["*** Update File:", "*** Add File:", "*** Delete File:", "*** Move to:"] {
        for (i, _) in text.match_indices(head) {
            let rest = &text[i + head.len()..];
            let end = rest.find(['\n', '\r', '\\', '"', '\'', '`']).unwrap_or(rest.len());
            let p = rest[..end].trim();
            if p.is_empty() {
                continue;
            }
            let path = if p.starts_with('/') { PathBuf::from(p) } else if cwd.is_empty() { continue } else { Path::new(cwd).join(p) };
            if !out.contains(&path) {
                out.push(path);
            }
        }
    }
    out
}

/// A Codex user message → the prompt: `user_message` events, completed
/// `UserMessage` items, and `user` response messages (the injected
/// `<environment_context>` and `# AGENTS.md` ones are none).
fn codex_prompt(v: &Value) -> Option<String> {
    let p = v.get("payload")?;
    let texts = |c: Option<&Value>| -> Vec<String> {
        c.and_then(Value::as_array).unwrap_or(&[]).iter().filter_map(|b| s(b, "text")).map(str::to_string).collect()
    };
    let text = match (s(v, "type"), s(p, "type")) {
        (Some("event_msg"), Some("user_message")) => s(p, "message")?.to_string(),
        (Some("event_msg"), Some("item_completed")) => {
            let item = p.get("item")?;
            if s(item, "type") != Some("UserMessage") {
                return None;
            }
            texts(item.get("content")).join("\n")
        }
        (Some("response_item"), Some("message")) if s(p, "role") == Some("user") => {
            let t = texts(p.get("content")).join("\n");
            if t.trim_start().starts_with('#') {
                return None;
            }
            t
        }
        _ => return None,
    };
    clean_prompt(&text)
}

// ---------------------------------------------------------------- files

/// A file's identity: (inode, len, mtime).
fn identity(m: &fs::Metadata) -> (u64, u64, Option<SystemTime>) {
    (m.ino(), m.len(), m.modified().ok())
}

/// One transcript read so far.
#[derive(Debug, Clone)]
pub struct Transcript {
    pub kind: Kind,
    /// A Claude session's own transcript (false: a subagent's).
    pub main: bool,
    ino: u64,
    len: u64,
    mtime: Option<SystemTime>,
    /// Just past the last complete line read.
    offset: u64,
    /// `offset` is at a line start (false after starting mid-file, until
    /// the first newline is found).
    aligned: bool,
    pub facts: Facts,
    /// Bytes read so far (the tests watch it).
    pub read: u64,
    /// Full reparses so far.
    pub resets: u32,
    /// How much one read step holds ([`CHUNK`]; the tests make it tiny).
    pub chunk: u64,
}

impl Transcript {
    pub fn new(kind: Kind, main: bool) -> Transcript {
        Transcript {
            kind,
            main,
            ino: 0,
            len: 0,
            mtime: None,
            offset: 0,
            aligned: true,
            facts: Facts::default(),
            read: 0,
            resets: 0,
            chunk: CHUNK,
        }
    }

    /// Catch up with the file at `path` (see the module docs): nothing when
    /// unchanged, the new whole lines when grown, a fresh parse when shrunk,
    /// replaced or rewritten; at most the last `max` bytes at once.
    pub fn sync(&mut self, path: &Path, max: u64) -> std::io::Result<()> {
        let mut f = open_regular(path)?;
        let m = f.metadata()?;
        let (ino, len, mtime) = identity(&m);
        if (ino, len, mtime) == (self.ino, self.len, self.mtime) {
            return Ok(());
        }
        let fresh = self.ino == 0;
        if ino != self.ino || len < self.len || len < self.offset || len == self.len && mtime != self.mtime {
            if !fresh {
                self.resets += 1;
            }
            self.facts = Facts::default();
            (self.offset, self.aligned) = (0, true);
        }
        (self.ino, self.len, self.mtime) = (ino, len, mtime);
        if len - self.offset > max {
            // Too much at once: only the tail (what it skips is lost).
            (self.offset, self.aligned) = (len - max, false);
        }
        // A chunk at a time (what a read holds is a chunk and the line it
        // ends inside), each fed its whole lines.
        f.seek(SeekFrom::Start(self.offset))?;
        let mut left = len - self.offset;
        let mut buf: Vec<u8> = Vec::new();
        let mut cands: Cands<(u64, usize)> = Cands::default();
        while left > 0 {
            let have = buf.len();
            let want = self.chunk.min(left) as usize;
            buf.resize(have + want, 0);
            let got = read_full(&mut f, &mut buf[have..])?;
            buf.truncate(have + got);
            if got == 0 {
                break;
            }
            left -= got as u64;
            self.read += got as u64;
            let mut start = 0;
            if !self.aligned {
                // Mid-line (a cut start): skip to the next line.
                let Some(nl) = buf.iter().position(|&b| b == b'\n') else {
                    self.offset += buf.len() as u64;
                    buf.clear();
                    continue;
                };
                start = nl + 1;
                self.offset += start as u64;
                self.aligned = true;
            }
            let Some(last) = buf[start..].iter().rposition(|&b| b == b'\n') else {
                buf.drain(..start);
                continue;
            };
            let end = start + last + 1;
            let base = self.offset;
            self.offset += (end - start) as u64;
            self.lines(&buf[start..end], base, &mut cands);
            buf.drain(..end);
        }
        // The newest usage and prompt, read back from the file by place.
        let (kind, main) = (self.kind, self.main);
        decide(&mut self.facts, kind, main, &cands, |&(at, len)| {
            let mut b = vec![0u8; len];
            f.read_exact_at(&mut b, at).ok()?;
            Some(String::from_utf8_lossy(&b).into_owned())
        });
        Ok(())
    }

    /// Whole lines starting at file offset `base`: each taken
    /// ([`claude_line`] / [`codex_line`]), the candidates' places kept (the
    /// newest [`CANDS_MAX`] or so).
    fn lines(&mut self, whole: &[u8], base: u64, cands: &mut Cands<(u64, usize)>) {
        let mut pos = 0usize;
        for raw in whole.split(|&b| b == b'\n') {
            let at = base + pos as u64;
            pos += raw.len() + 1;
            if raw.is_empty() {
                continue;
            }
            let line = String::from_utf8_lossy(raw);
            let c = match self.kind {
                Kind::Claude => claude_line(&mut self.facts, &line, self.main),
                Kind::Codex => codex_line(&mut self.facts, &line),
            };
            if let Some(c) = c {
                cands.push(c, self.facts.seq, (at, raw.len()));
            }
        }
    }
}

/// How much of a transcript one read step holds.
const CHUNK: u64 = 1 << 20;

#[cfg(target_os = "macos")]
const O_NONBLOCK: i32 = 0x0004;
#[cfg(not(target_os = "macos"))]
const O_NONBLOCK: i32 = 0o4000;

/// `p` opened for reading if it is a REGULAR file: opened non-blocking and
/// checked on the open fd (git.rs `read_small`'s way), so a FIFO or a
/// device named like a transcript or an untracked file never blocks the
/// worker. (Non-blocking changes nothing for a regular file's reads.)
fn open_regular(p: &Path) -> std::io::Result<fs::File> {
    use std::os::unix::fs::OpenOptionsExt;
    let f = fs::OpenOptions::new().read(true).custom_flags(O_NONBLOCK).open(p)?;
    if !f.metadata()?.is_file() {
        return Err(std::io::Error::other("not a regular file"));
    }
    Ok(f)
}

/// Fill `buf` from `f` (short only at the end of the file).
fn read_full(f: &mut fs::File, buf: &mut [u8]) -> std::io::Result<usize> {
    let mut n = 0;
    while n < buf.len() {
        match f.read(&mut buf[n..]) {
            Ok(0) => break,
            Ok(k) => n += k,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
            Err(e) => return Err(e),
        }
    }
    Ok(n)
}

// ---------------------------------------------------------------- git

/// What git said about one repo.
#[derive(Debug, Clone, Default)]
struct Repo {
    at: f64,
    /// What it was asked over (see [`Engine::repo_sig`]); 0 until a whole
    /// answer (diff AND untracked) came back.
    sig: u64,
    /// Dirty file (repo-relative) → (added, removed).
    stats: HashMap<String, (u64, u64)>,
    /// The last [`Engine::update`] (its tick) the answer was known to be
    /// current at: asked then, or nothing it depends on changed since a
    /// whole answer. 0: never.
    valid: u64,
}

/// One transcript's edits of one file, as the attribution follows them.
#[derive(Debug, Clone, Copy, Default)]
struct Seen {
    /// The transcript's reparse count and settled edits of the file...
    resets: u32,
    edits: u32,
    /// ...the update they were first seen in...
    since: u64,
    /// ...and its settled edits when a current answer showed the file
    /// clean (after them): it is that agent's again only once it edits it
    /// once more.
    clean_at: Option<u32>,
    /// When an update last used it (epoch seconds; [`KEEP_FOR`]).
    used: f64,
}

/// Dirty files (repo-relative) → (added, removed).
pub type Stats = HashMap<String, (u64, u64)>;

fn git(root: &Path) -> Command {
    let mut c = Command::new("git");
    c.args(["--no-optional-locks", "--literal-pathspecs", "-C"])
        .arg(root)
        .env("GIT_OPTIONAL_LOCKS", "0")
        .env("GIT_TERMINAL_PROMPT", "0");
    c
}

/// `git diff --numstat` and the untracked files among `rels` in `root`:
/// repo-relative path → (added, removed), and whether both questions were
/// answered (false: one failed, a timeout say, so the map may miss files).
/// None when git failed outright.
pub fn numstat(root: &Path, rels: &[String]) -> Option<(Stats, bool)> {
    let mut out = HashMap::new();
    let diff_from = |base: &str| {
        let mut c = git(root);
        c.args(["diff", "--numstat", "-z", "--no-renames", "--no-ext-diff", "--no-color", base, "--"]).args(rels);
        crate::proc::run(c, None, GIT_TIMEOUT).ok().filter(|o| o.ok())
    };
    // A repo with no commit yet has no HEAD: then the base is the empty tree,
    // as diff.rs `repo_diff` does, so a staged file counts as added and the
    // answer is whole. (2026-10-22: diffing HEAD there always failed, so a
    // staged file never showed, the repo was asked again every DIFF_EVERY and
    // no pass was ever complete.) Asked only when the HEAD diff failed, and
    // only a clean "no such ref" (rev-parse -q exits 1) switches the base.
    let diff = diff_from("HEAD").or_else(|| {
        let mut c = git(root);
        c.args(["rev-parse", "--verify", "-q", "HEAD"]);
        let no_head = crate::proc::run(c, None, GIT_TIMEOUT).is_ok_and(|o| o.status.code() == Some(1));
        no_head.then(|| diff_from(crate::diff::EMPTY_TREE)).flatten()
    });
    if let Some(o) = &diff {
        for rec in o.stdout.split('\0').filter(|r| !r.is_empty()) {
            let mut it = rec.splitn(3, '\t');
            let (Some(a), Some(r), Some(p)) = (it.next(), it.next(), it.next()) else { continue };
            // Binary: `-` / `-`, no lines, still dirty.
            out.insert(p.to_string(), (a.parse().unwrap_or(0), r.parse().unwrap_or(0)));
        }
    }
    let mut c = git(root);
    c.args(["ls-files", "-z", "--others", "--exclude-standard", "--"]).args(rels);
    let others = crate::proc::run(c, None, GIT_TIMEOUT).ok().filter(|o| o.ok());
    if let Some(o) = &others {
        for p in o.stdout.split('\0').filter(|r| !r.is_empty()) {
            out.insert(p.to_string(), (count_lines(&root.join(p)), 0));
        }
    }
    (diff.is_some() || others.is_some()).then_some((out, diff.is_some() && others.is_some()))
}

/// A text file's lines (a last line without its newline counts); 0 for a
/// binary file (a NUL in its first 8 KB) or what cannot be read; at most
/// [`UNTRACKED_MAX`] bytes are read.
pub fn count_lines(p: &Path) -> u64 {
    let Ok(f) = open_regular(p) else { return 0 };
    let mut buf = Vec::new();
    if f.take(UNTRACKED_MAX).read_to_end(&mut buf).is_err() || buf[..buf.len().min(8192)].contains(&0) {
        return 0;
    }
    let nl = buf.iter().filter(|&&b| b == b'\n').count() as u64;
    nl + u64::from(!buf.is_empty() && buf.last() != Some(&b'\n'))
}

// ---------------------------------------------------------------- engine

/// The synchronous core: transcripts, lookups and git, cached across calls.
/// The live sidebar runs one on its worker thread ([`Insights`]); the CLI
/// runs one once.
pub struct Engine {
    /// `~/.claude` (`projects` under it).
    pub claude_root: PathBuf,
    /// Claude's per-process session files ([`pins::claude_sessions_dir`]).
    pub sessions_dir: PathBuf,
    /// `$CODEX_HOME` or `~/.codex` (`sessions` under it).
    pub codex_root: PathBuf,
    pub first_max: u64,
    pub diff_every: f64,
    procs: Box<dyn Procs + Send>,
    files: HashMap<PathBuf, Transcript>,
    /// Transcript → when an update last used it (kept [`KEEP_FOR`]).
    used: HashMap<PathBuf, f64>,
    /// Session id → its transcript, or when it was last looked for in vain.
    found: HashMap<String, Result<PathBuf, f64>>,
    /// Folder → (looked up at, its real path and its repo's work tree).
    roots: HashMap<PathBuf, (f64, PathBuf, Option<PathBuf>)>,
    repos: HashMap<PathBuf, Repo>,
    /// (transcript, real file path) → how its edits stand against git.
    seen: HashMap<(PathBuf, PathBuf), Seen>,
    /// Updates so far.
    tick: u64,
    /// git asked so far (the tests watch it).
    pub git_runs: u32,
    /// The last update was a COMPLETE pass: every repo it needed answered
    /// whole and current (the shared files' sweep waits for one).
    pub complete: bool,
}

/// A transcript's edits of a file: (the file as written, the transcript,
/// its reparse count, the settled edits).
type Edits = (PathBuf, PathBuf, u32, u32);

impl Engine {
    pub fn new(claude_root: PathBuf, sessions_dir: PathBuf, codex_root: PathBuf, procs: Box<dyn Procs + Send>) -> Engine {
        Engine {
            claude_root,
            sessions_dir,
            codex_root,
            first_max: FIRST_MAX,
            diff_every: DIFF_EVERY,
            procs,
            files: HashMap::new(),
            used: HashMap::new(),
            found: HashMap::new(),
            roots: HashMap::new(),
            repos: HashMap::new(),
            seen: HashMap::new(),
            tick: 0,
            git_runs: 0,
            complete: false,
        }
    }

    /// The real places: `$AGENT_UI_CLAUDE_DIR` or `~/.claude`, the pins'
    /// sessions dir, `$CODEX_HOME` or `~/.codex`; the kernel's processes.
    pub fn home() -> Engine {
        let env = |k: &str| std::env::var(k).ok().filter(|v| !v.is_empty()).map(PathBuf::from);
        let home = PathBuf::from(std::env::var("HOME").unwrap_or_default());
        Engine::new(
            env("AGENT_UI_CLAUDE_DIR").unwrap_or_else(|| home.join(".claude")),
            pins::claude_sessions_dir(),
            env("CODEX_HOME").unwrap_or_else(|| home.join(".codex")),
            Box::new(pins::LiveProcs),
        )
    }

    /// Every agent window's insight (window id → it): windows with an agent
    /// state, each id once; one without a transcript found (an OpenCode
    /// agent, a Claude session not yet written) has none.
    pub fn update(&mut self, snap: &Snapshot, now: f64) -> BTreeMap<String, Insight> {
        let mut ids = HashSet::new();
        let agents: Vec<&Window> =
            snap.windows.iter().filter(|w| !w.state.is_empty() && w.kind != "opencode" && ids.insert(w.id.as_str())).collect();
        let sids = if agents.iter().any(|w| w.kind != "codex") {
            pins::sessions(snap, &self.sessions_dir, &*self.procs).now
        } else {
            HashMap::new()
        };
        self.tick += 1;
        let tick = self.tick;
        let mut live: HashSet<PathBuf> = HashSet::new();
        let mut got: Vec<(String, Insight, Vec<Edits>)> = Vec::new();
        for w in agents {
            let (kind, path) = if w.kind == "codex" {
                (Kind::Codex, self.codex_transcript(w, now))
            } else {
                // (A non-Claude id the pins took from `@agent_session_id`: not ours.)
                match sids.get(&w.id) {
                    Some(sid) if w.kind == "claude" || *sid != w.session_id => (Kind::Claude, self.claude_transcript(sid, &w.path, now)),
                    _ => continue,
                }
            };
            let Some(path) = path else { continue };
            if self.sync(&path, kind, true).is_none() {
                continue;
            }
            live.insert(path.clone());
            let t = &self.files[&path];
            let f = &t.facts;
            let (doing, ctx_used, ctx_window) = (f.doing(), f.ctx_used, f.ctx_window(kind));
            let (todo, prompt) = (f.todo().map(str::to_string), f.prompt.clone());
            let edits_of = |t: &Transcript, tr: &Path| -> Vec<Edits> {
                t.facts.edits().into_iter().map(|(p, n)| (p, tr.to_path_buf(), t.resets, n)).collect()
            };
            let mut edits = edits_of(t, &path);
            if kind == Kind::Claude {
                for sub in subagent_files(&path) {
                    if let Some(t) = self.sync(&sub, Kind::Claude, false) {
                        edits.extend(edits_of(t, &sub));
                        live.insert(sub);
                    }
                }
            }
            let touched: HashSet<&PathBuf> = edits.iter().map(|e| &e.0).collect();
            let touched = touched.len();
            let ins = Insight {
                kind,
                transcript: path,
                doing,
                todo,
                prompt,
                ctx_used,
                ctx_window,
                diff: None,
                diff_known: false,
                dirty_files: Vec::new(),
                touched,
            };
            got.push((w.id.clone(), ins, edits));
        }
        // A transcript this pass did not reach (its window missed for a
        // pass: a sessions file read mid-rewrite) keeps its cache a while.
        for p in live {
            self.used.insert(p, now);
        }
        self.used.retain(|_, t| now - *t < KEEP_FOR);
        let used = &self.used;
        self.files.retain(|p, _| used.contains_key(p));
        // A session id's lookup goes with its transcript's cache, a miss once
        // it would be retried anyway (2026-10-22: every session id ever seen,
        // windows long gone among them, used to stay).
        self.found.retain(|_, f| match f {
            Ok(p) => used.contains_key(p),
            Err(at) => now - *at < RETRY,
        });
        // The diff: every touched file (its real path) by its repo, each
        // repo asked once.
        let mut want: BTreeMap<PathBuf, Vec<String>> = BTreeMap::new();
        let mut at: HashMap<PathBuf, Option<(PathBuf, PathBuf, String)>> = HashMap::new();
        for p in got.iter().flat_map(|(_, _, e)| e.iter().map(|e| &e.0)) {
            if at.contains_key(p) {
                continue;
            }
            let loc = self.locate(p, now);
            if let Some((_, root, rel)) = &loc {
                want.entry(root.clone()).or_default().push(rel.clone());
            }
            at.insert(p.clone(), loc);
        }
        for (root, rels) in &mut want {
            rels.sort();
            rels.dedup();
            self.ask_repo(root, rels, now, tick);
        }
        self.repos.retain(|r, _| want.contains_key(r));
        self.complete = want.keys().all(|r| self.repos.get(r).is_some_and(|x| x.valid == tick));
        // Each agent's dirty files: dirty, and edited by it since a current
        // answer last showed them clean (one agent's committed edit is not
        // another's later change of the same file).
        let mut kept: HashSet<(PathBuf, PathBuf)> = HashSet::new();
        let mut out = BTreeMap::new();
        for (id, mut ins, edits) in got {
            let mut d = DiffStat::default();
            let mut known = true;
            for (p, tr, resets, n) in edits {
                // (Outside any repo: never dirty, so known.)
                let Some(Some((real, root, rel))) = at.get(&p) else { continue };
                let key = (tr, real.clone());
                let s = self.seen.entry(key.clone()).or_default();
                if s.since == 0 || (s.resets, s.edits) != (resets, n) {
                    if s.resets != resets {
                        s.clean_at = None;
                    }
                    (s.resets, s.edits, s.since) = (resets, n, tick);
                }
                s.used = now;
                kept.insert(key);
                let repo = self.repos.get(root);
                match repo.and_then(|r| r.stats.get(rel)) {
                    None => {
                        if repo.is_some_and(|r| r.valid >= s.since) {
                            s.clean_at = Some(n);
                        } else {
                            // (Clean only by a stale or missing answer.)
                            known = false;
                        }
                    }
                    Some(&(a, r)) => {
                        if s.clean_at.is_none_or(|c| n > c) && !ins.dirty_files.contains(real) {
                            (d.added, d.removed, d.files) = (d.added + a, d.removed + r, d.files + 1);
                            ins.dirty_files.push(real.clone());
                        }
                    }
                }
            }
            ins.diff = (d.files > 0).then_some(d);
            ins.diff_known = known;
            out.insert(id, ins);
        }
        // (Kept a while unused too: a window back after a missed pass keeps
        // its `clean_at`, so a file it committed stays out of its badge.)
        self.seen.retain(|k, s| kept.contains(k) || now - s.used < KEEP_FOR);
        out
    }

    /// Bring the transcript at `path` up to date (a new one read once);
    /// None when it cannot be read.
    fn sync(&mut self, path: &Path, kind: Kind, main: bool) -> Option<&Transcript> {
        let max = self.first_max;
        let t = self.files.entry(path.to_path_buf()).or_insert_with(|| Transcript::new(kind, main));
        if t.sync(path, max).is_err() {
            self.files.remove(path);
            return None;
        }
        self.files.get(path)
    }

    /// Claude session `sid`'s transcript: under its window's cwd's project
    /// folder, else wherever one scan of the project folders finds it.
    fn claude_transcript(&mut self, sid: &str, cwd: &str, now: f64) -> Option<PathBuf> {
        match self.found.get(sid) {
            Some(Ok(p)) if p.exists() => return Some(p.clone()),
            Some(Err(at)) if now - at < RETRY => return None,
            _ => {}
        }
        let projects = self.claude_root.join("projects");
        let file = format!("{sid}.jsonl");
        let direct = projects.join(crate::sidebar::activity::project_slug(cwd)).join(&file);
        let hit = if !cwd.is_empty() && direct.is_file() {
            Some(direct)
        } else {
            fs::read_dir(&projects).ok().and_then(|d| d.flatten().map(|e| e.path().join(&file)).find(|p| p.is_file()))
        };
        self.found.insert(sid.to_string(), hit.clone().ok_or(now));
        hit
    }

    /// A Codex window's rollout: `@agent_rollout`, else the newest
    /// `rollout-*-<@agent_session_id>.jsonl` under `sessions/YYYY/MM/DD`.
    fn codex_transcript(&mut self, w: &Window, now: f64) -> Option<PathBuf> {
        if !w.rollout.is_empty() {
            return Some(PathBuf::from(&w.rollout));
        }
        let sid = w.session_id.as_str();
        if sid.is_empty() {
            return None;
        }
        match self.found.get(sid) {
            Some(Ok(p)) if p.exists() => return Some(p.clone()),
            Some(Err(at)) if now - at < RETRY => return None,
            _ => {}
        }
        let hit = find_rollout(&self.codex_root.join("sessions"), sid);
        self.found.insert(sid.to_string(), hit.clone().ok_or(now));
        hit
    }

    /// Where `p` really is: (its path with the folder's symlinks resolved,
    /// so `/tmp/x` and `/private/tmp/x` are one file; the work tree holding
    /// it; its path in there). None outside any repo. Cached per folder for
    /// [`ROOT_TTL`].
    fn locate(&mut self, p: &Path, now: f64) -> Option<(PathBuf, PathBuf, String)> {
        let (dir, name) = (p.parent()?.to_path_buf(), p.file_name()?);
        let hit = self.roots.get(&dir).filter(|(at, _, _)| now - at >= 0.0 && now - at < ROOT_TTL).cloned();
        let (real_dir, root) = match hit {
            Some((_, real, root)) => (real, root),
            None => {
                let real = fs::canonicalize(&dir).unwrap_or_else(|_| dir.clone());
                let root = crate::git::git_dirs(&real.to_string_lossy()).map(|(wt, _)| wt);
                self.roots.retain(|_, (at, _, _)| now - *at >= 0.0 && now - *at < ROOT_TTL);
                self.roots.insert(dir, (now, real.clone(), root.clone()));
                (real, root)
            }
        };
        let (real, root) = (real_dir.join(name), root?);
        let rel = real.strip_prefix(&root).ok()?.to_string_lossy().into_owned();
        (!rel.is_empty()).then_some((real, root, rel))
    }

    /// What `root`'s answer over `rels` depends on: the files' (len,
    /// mtime), and the repo's index, HEAD and HEAD reflog (a commit, a
    /// checkout, a stage).
    fn repo_sig(root: &Path, rels: &[String]) -> u64 {
        let mut h = std::collections::hash_map::DefaultHasher::new();
        let st = |p: &Path| fs::metadata(p).ok().map(|m| (m.len(), m.modified().ok()));
        for r in rels {
            (r, st(&root.join(r))).hash(&mut h);
        }
        if let Some((_, gd)) = crate::git::git_dirs(&root.to_string_lossy()) {
            for f in ["index", "HEAD", "logs/HEAD"] {
                st(&gd.join(f)).hash(&mut h);
            }
        }
        h.finish()
    }

    /// Ask git about `rels` in `root` (update `tick`) unless nothing it
    /// would say changed since a whole answer (which is then current
    /// still), or it was asked under [`Engine::diff_every`] s ago. A failure
    /// keeps the last answer; a half answer (one of the two questions
    /// failed) is used but never taken as current, and asked again.
    fn ask_repo(&mut self, root: &Path, rels: &[String], now: f64, tick: u64) {
        let sig = Engine::repo_sig(root, rels);
        if let Some(r) = self.repos.get_mut(root) {
            if r.sig != 0 && r.sig == sig {
                r.valid = tick;
                return;
            }
            if now - r.at >= 0.0 && now - r.at < self.diff_every {
                return;
            }
        }
        self.git_runs += 1;
        let got = numstat(root, rels);
        let r = self.repos.entry(root.to_path_buf()).or_default();
        r.at = now;
        if let Some((st, whole)) = got {
            r.stats = st;
            r.sig = if whole { sig } else { 0 };
            if whole {
                r.valid = tick;
            }
        }
    }
}

/// A Claude transcript's subagents' transcripts (`<sid>/subagents/*.jsonl`).
pub fn subagent_files(transcript: &Path) -> Vec<PathBuf> {
    let dir = transcript.with_extension("").join("subagents");
    let mut v: Vec<PathBuf> = fs::read_dir(dir)
        .map(|d| d.flatten().map(|e| e.path()).filter(|p| p.extension().and_then(|x| x.to_str()) == Some("jsonl")).collect())
        .unwrap_or_default();
    v.sort();
    v
}

/// The newest rollout of Codex session `sid` under `sessions`
/// (`YYYY/MM/DD/rollout-<time>-<sid>.jsonl`), newest days first, at most a
/// year of days looked at.
pub fn find_rollout(sessions: &Path, sid: &str) -> Option<PathBuf> {
    let suffix = format!("-{sid}.jsonl");
    let sorted = |p: &Path| -> Vec<PathBuf> {
        let mut v: Vec<PathBuf> = fs::read_dir(p).map(|d| d.flatten().map(|e| e.path()).collect()).unwrap_or_default();
        v.sort();
        v.reverse();
        v
    };
    let mut days = 0;
    for y in sorted(sessions).iter().filter(|p| p.is_dir()) {
        for m in sorted(y).iter().filter(|p| p.is_dir()) {
            for d in sorted(m).iter().filter(|p| p.is_dir()) {
                days += 1;
                if days > 366 {
                    return None;
                }
                let hit = sorted(d).into_iter().find(|f| f.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.starts_with("rollout-") && n.ends_with(&suffix)));
                if hit.is_some() {
                    return hit;
                }
            }
        }
    }
    None
}

/// Files grouped by the repo holding each (work tree → repo-relative
/// paths, in the order given); a file outside any repo is left out.
pub fn by_repo(files: &[PathBuf]) -> Vec<(PathBuf, Vec<String>)> {
    let mut out: Vec<(PathBuf, Vec<String>)> = Vec::new();
    for p in files {
        let Some(dir) = p.parent() else { continue };
        let Some((root, _)) = crate::git::git_dirs(&dir.to_string_lossy()) else { continue };
        let Ok(rel) = p.strip_prefix(&root) else { continue };
        let rel = rel.to_string_lossy().into_owned();
        match out.iter_mut().find(|(r, _)| *r == root) {
            Some((_, v)) => v.push(rel),
            None => out.push((root, vec![rel])),
        }
    }
    out
}

// ---------------------------------------------------------------- shared files

/// Where the publishing sidebar ([`Board`]) leaves each agent's dirty files
/// for `agent-ui diff` (so the popup shows exactly what the badge counted,
/// from its attribution history), and its `.json` / `.gen` / `.lock`:
/// `$AGENT_UI_INSIGHT_DIR`, else
/// `$XDG_STATE_HOME/agent-ui/insight`, else `~/.local/state/agent-ui/insight`.
pub fn files_dir() -> PathBuf {
    let env = |k: &str| std::env::var(k).ok().filter(|v| !v.is_empty());
    if let Some(d) = env("AGENT_UI_INSIGHT_DIR") {
        return PathBuf::from(d);
    }
    let base = env("XDG_STATE_HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(env("HOME").unwrap_or_default()).join(".local/state"));
    base.join("agent-ui/insight")
}

/// Window `win`'s file under `dir`: `<key>-<win without @>.files`; `key`
/// is the server's ([`board::server_key`]: its `#{start_time}`, stable for
/// its life and new after a restart, which renumbers windows, and a hash
/// of its `#{socket_path}`).
pub fn files_path_in(dir: &Path, key: &str, win: &str) -> PathBuf {
    dir.join(format!("{key}-{}.files", win.trim_start_matches('@')))
}

/// `$AGENT_UI_NO_INSIGHT` set (non-empty, not `0`): no insight read at all,
/// live or `sidebar --once` (hermetic tests).
pub fn disabled() -> bool {
    std::env::var("AGENT_UI_NO_INSIGHT").is_ok_and(|v| !v.is_empty() && v != "0")
}

/// [`files_path_in`] under [`files_dir`].
pub fn files_path(key: &str, win: &str) -> PathBuf {
    files_path_in(&files_dir(), key, win)
}

/// `files` as `agent-ui insight-files` prints them: each repo's work tree
/// on a line of its own, then its files repo-relative, one per line after a
/// tab ([`by_repo`]; a file outside any repo left out).
pub fn files_text(files: &[PathBuf]) -> String {
    let mut out = String::new();
    for (root, rels) in by_repo(files) {
        out.push_str(&format!("{}\n", root.display()));
        for r in rels {
            out.push_str(&format!("\t{r}\n"));
        }
    }
    out
}

/// Write `text` to `path` whole or not at all (a temp file beside it, then
/// a rename), mode 0600, its folder made 0700 when missing.
pub fn write_atomic(path: &Path, text: &str) -> std::io::Result<()> {
    use std::io::Write;
    use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
    let dir = path.parent().ok_or(std::io::ErrorKind::InvalidInput)?;
    fs::DirBuilder::new().recursive(true).mode(0o700).create(dir)?;
    let tmp = dir.join(format!(".{}.{}.tmp", path.file_name().and_then(|n| n.to_str()).unwrap_or("x"), std::process::id()));
    let mut f = fs::OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(&tmp)?;
    let done = f.write_all(text.as_bytes()).and_then(|_| f.sync_all()).and_then(|_| fs::rename(&tmp, path));
    if done.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    done
}

/// The publisher's side of the shared files ([`Board`]): each agent
/// window's dirty files kept at [`files_path_in`] (written only when they
/// changed; gone once the window has none, or is gone).
#[derive(Debug)]
pub struct Published {
    pub dir: PathBuf,
    pub server: String,
    /// Window id → the dirty files last written for it.
    last: HashMap<String, Vec<PathBuf>>,
    /// The folder was swept of this server's stale files.
    swept: bool,
}

impl Published {
    pub fn new(dir: PathBuf, server: String) -> Published {
        Published { dir, server, last: HashMap::new(), swept: false }
    }

    /// Bring the files in step with `map` (window id → insight). The first
    /// call with `sweep` (a complete pass: [`Engine::complete`]) also
    /// removes this server's files no window of `map` keeps.
    pub fn sync(&mut self, map: &BTreeMap<String, Insight>, sweep: bool) {
        let now: HashMap<&str, &Vec<PathBuf>> =
            map.iter().filter(|(_, i)| !i.dirty_files.is_empty()).map(|(w, i)| (w.as_str(), &i.dirty_files)).collect();
        if !self.swept && sweep {
            self.swept = true;
            let prefix = format!("{}-", self.server);
            for e in fs::read_dir(&self.dir).into_iter().flatten().flatten() {
                let name = e.file_name().to_string_lossy().into_owned();
                let Some(win) = name.strip_prefix(&prefix).and_then(|r| r.strip_suffix(".files")) else { continue };
                if !now.contains_key(format!("@{win}").as_str()) {
                    let _ = fs::remove_file(e.path());
                }
            }
        }
        let gone: Vec<String> = self.last.keys().filter(|w| !now.contains_key(w.as_str())).cloned().collect();
        for w in gone {
            let _ = fs::remove_file(files_path_in(&self.dir, &self.server, &w));
            self.last.remove(&w);
        }
        for (w, files) in now {
            if self.last.get(w) == Some(files) {
                continue;
            }
            let path = files_path_in(&self.dir, &self.server, w);
            let text = files_text(files);
            let ok = if text.is_empty() { fs::remove_file(&path).or_else(|e| if e.kind() == std::io::ErrorKind::NotFound { Ok(()) } else { Err(e) }) } else { write_atomic(&path, &text) };
            if ok.is_ok() {
                self.last.insert(w.to_string(), files.clone());
            }
        }
    }
}

// ---------------------------------------------------------------- live

#[derive(Default)]
struct Shared {
    map: BTreeMap<String, Insight>,
    /// Bumped on every result (a UI may redraw when it moved).
    generation: u64,
    busy: bool,
    /// The worker gave up (its engine kept panicking): insights are off.
    dead: bool,
    /// The last result was a complete pass ([`Engine::complete`]).
    complete: bool,
}

/// Panics in a row after which the worker gives up (each one restarts its
/// engine afresh first).
const PANICS_MAX: u32 = 3;

/// The worker's loop: the newest snapshot handed over, answered; a panic
/// in the engine (making it, or an update) never leaves `busy` set, and
/// the engine starts afresh; [`PANICS_MAX`] in a row turn insights off.
fn work(make: Box<dyn Fn() -> Engine + Send>, rx: mpsc::Receiver<Snapshot>, shared: Arc<Mutex<Shared>>) {
    use std::panic::{catch_unwind, AssertUnwindSafe};
    let lock = || shared.lock().unwrap_or_else(|e| e.into_inner());
    let mut engine: Option<Engine> = None;
    let mut panics = 0;
    while let Ok(mut snap) = rx.recv() {
        while let Ok(newer) = rx.try_recv() {
            snap = newer;
        }
        let got = catch_unwind(AssertUnwindSafe(|| {
            let e = engine.get_or_insert_with(&make);
            let map = e.update(&snap, crate::text::now());
            (map, e.complete)
        }));
        let mut sh = lock();
        sh.busy = false;
        match got {
            Ok((map, complete)) => {
                panics = 0;
                sh.map = map;
                sh.complete = complete;
                sh.generation += 1;
            }
            Err(_) => {
                engine = None;
                panics += 1;
                if panics >= PANICS_MAX {
                    sh.dead = true;
                    sh.map.clear();
                    sh.generation += 1;
                    return;
                }
            }
        }
    }
}

/// The live sidebar's side: an [`Engine`] on a worker thread of its own
/// (started on the first [`Insights::refresh`]), handed the latest snapshot
/// at most every [`EVERY`] and only while it is idle; the frame reads the
/// last results ([`Insights::get`], [`Insights::apply`]) and never waits.
pub struct Insights {
    /// Makes the worker's engine, again after a panic (None: never works,
    /// or the worker has it).
    make: Option<Box<dyn Fn() -> Engine + Send>>,
    shared: Arc<Mutex<Shared>>,
    tx: Option<mpsc::Sender<Snapshot>>,
    last: Option<Instant>,
    pub every: Duration,
}

impl Insights {
    /// Reading the real transcripts ([`Engine::home`]).
    pub fn home() -> Insights {
        Insights::with(Engine::home)
    }

    /// Its worker running the engine `make` makes (on that thread).
    pub fn with(make: impl Fn() -> Engine + Send + 'static) -> Insights {
        Insights { make: Some(Box::new(make)), shared: Arc::default(), tx: None, last: None, every: EVERY }
    }

    /// Never reading anything (unit tests).
    pub fn off() -> Insights {
        Insights { make: None, ..Insights::home() }
    }

    /// Hand the worker `snap` if it is idle and [`Insights::every`] has
    /// passed: a clone and a send, nothing more on this thread.
    pub fn refresh(&mut self, snap: &Snapshot) {
        let now = Instant::now();
        if self.tx.is_none() && self.make.is_none() || self.last.is_some_and(|t| now.duration_since(t) < self.every) {
            return;
        }
        let (busy, dead) = self.shared.lock().map(|s| (s.busy, s.dead)).unwrap_or((true, true));
        if dead {
            // (The worker gave up: off for good.)
            (self.tx, self.make) = (None, None);
            return;
        }
        if busy {
            return;
        }
        if let Some(make) = self.make.take() {
            let (tx, rx) = mpsc::channel::<Snapshot>();
            let shared = self.shared.clone();
            if std::thread::Builder::new().name("insight".into()).spawn(move || work(make, rx, shared)).is_err() {
                return;
            }
            self.tx = Some(tx);
        }
        let Some(tx) = &self.tx else { return };
        self.last = Some(now);
        if let Ok(mut sh) = self.shared.lock() {
            sh.busy = true;
        }
        if tx.send(snap.clone()).is_err() {
            // (The worker is gone: a panic. Nothing more is asked of it.)
            self.tx = None;
        }
    }

    /// Window `win`'s last insight.
    pub fn get(&self, win: &str) -> Option<Insight> {
        self.shared.lock().ok()?.map.get(win).cloned()
    }

    /// How many results have landed (a UI redraws when it moved).
    pub fn generation(&self) -> u64 {
        self.shared.lock().map(|s| s.generation).unwrap_or(0)
    }

    /// The last results whole (window id → insight).
    pub fn map(&self) -> BTreeMap<String, Insight> {
        self.shared.lock().map(|s| s.map.clone()).unwrap_or_default()
    }

    /// The last result, read at once: (the map, whether it was a complete
    /// pass, its generation).
    pub fn result(&self) -> (BTreeMap<String, Insight>, bool, u64) {
        self.shared.lock().map(|s| (s.map.clone(), s.complete, s.generation)).unwrap_or_default()
    }

    /// The last result was a complete pass ([`Engine::complete`]).
    pub fn complete(&self) -> bool {
        self.shared.lock().map(|s| s.complete).unwrap_or(false)
    }

    /// The worker gave up (its engine kept panicking).
    pub fn dead(&self) -> bool {
        self.shared.lock().map(|s| s.dead).unwrap_or(true)
    }

    /// It can work at all (not [`Insights::off`], not dead).
    pub fn working(&self) -> bool {
        (self.make.is_some() || self.tx.is_some()) && !self.dead()
    }

    /// The worker holds a snapshot it has not answered yet, handed it under
    /// [`PENDING_FOR`] ago (a UI waiting for its result checks
    /// [`Insights::generation`] meanwhile; a worker stuck or gone stops
    /// counting, so nothing waits on it forever).
    pub fn pending(&self) -> bool {
        self.tx.is_some()
            && self.last.is_some_and(|t| t.elapsed() < PENDING_FOR)
            && self.shared.lock().map(|s| s.busy).unwrap_or(false)
    }

    /// Fill in [`crate::view::Agent::insight`] for every agent of `view`.
    pub fn apply(&self, view: &mut ViewModel) {
        let Ok(sh) = self.shared.lock() else { return };
        fill(view, &sh.map);
    }
}

/// Fill in [`crate::view::Agent::insight`] for every agent of `view` from
/// `map` (window id → insight; `sidebar --once` reads one [`Engine`] pass).
pub fn fill(view: &mut ViewModel, map: &BTreeMap<String, Insight>) {
    let all = view.spaces.iter_mut().flat_map(|s| s.agents.iter_mut().chain(s.plain.iter_mut()));
    let all = all.chain(view.needs.iter_mut().map(|n| &mut n.agent)).chain(view.pinned.iter_mut());
    for a in all.chain(view.parked.iter_mut().map(|p| &mut p.agent)) {
        a.insight = map.get(&a.window_id).cloned();
    }
}

// ---------------------------------------------------------------- CLI

/// `182k`, `1M`, `950`.
pub fn tokens(n: u64) -> String {
    if n >= 1_000_000 && n.is_multiple_of(1_000_000) {
        format!("{}M", n / 1_000_000)
    } else if n >= 1_000_000 {
        format!("{:.1}M", n as f64 / 1e6)
    } else if n >= 1000 {
        format!("{}k", n / 1000)
    } else {
        n.to_string()
    }
}

/// One insight as JSON (the CLI's `--json`).
pub fn to_json(win: &str, i: &Insight) -> Value {
    let diff = i.diff.map(|d| crate::obj![("added", d.added), ("removed", d.removed), ("files", d.files)]).unwrap_or(Value::Null);
    let dirty: Vec<String> = i.dirty_files.iter().map(|p| p.to_string_lossy().into_owned()).collect();
    crate::obj![
        ("window", win),
        ("kind", i.kind.name()),
        ("transcript", i.transcript.to_string_lossy().into_owned()),
        ("doing", i.doing.clone()),
        ("ctx_used", i.ctx_used),
        ("ctx_window", i.ctx_window),
        ("diff", diff),
        ("diff_known", i.diff_known),
        ("dirty_files", dirty),
        ("touched", i.touched),
    ]
}

const USAGE: &str = "usage: agent-ui insight [WINDOW_ID] [--json]
       agent-ui insight-files WINDOW_ID";

/// `agent-ui insight [@id] [--json]`: every agent's insight (or one's), for
/// debugging. Exit 1 when the window asked for has none, 2 usage.
pub fn run(tmux: Tmux, args: &[String]) -> std::process::ExitCode {
    use std::io::Write;
    use std::process::ExitCode;
    let json = args.iter().any(|a| a == "--json");
    let rest: Vec<&String> = args.iter().filter(|a| *a != "--json").collect();
    if rest.len() > 1 || rest.first().is_some_and(|a| !a.starts_with('@')) {
        eprintln!("{USAGE}");
        return ExitCode::from(2);
    }
    let mut snap = tmux.snapshot();
    if let Some(w) = rest.first() {
        // (Only that window's transcript is read.)
        snap.windows.retain(|x| x.id == **w);
    }
    let mut all = Engine::home().update(&snap, crate::text::now());
    if let Some(w) = rest.first() {
        all.retain(|k, _| k == *w);
        if all.is_empty() {
            eprintln!("insight: no transcript for {w}");
            return ExitCode::from(1);
        }
    }
    let mut out = std::io::stdout().lock();
    if json {
        let v = Value::Arr(all.iter().map(|(w, i)| to_json(w, i)).collect());
        let _ = writeln!(out, "{}", v.to_json_pretty());
        return ExitCode::SUCCESS;
    }
    for (w, i) in &all {
        let ctx = match (i.ctx_used, i.ctx_fraction()) {
            (Some(u), Some(f)) => format!("{}/{} {:>3.0}%", tokens(u), tokens(i.ctx_window), f * 100.0),
            _ => format!("-/{}", tokens(i.ctx_window)),
        };
        let diff = i.diff.map(|d| d.badge()).unwrap_or_else(|| "clean".into());
        let doing = i.doing.as_deref().map(|d| crate::text::clip(d, 60)).unwrap_or_default();
        let _ = writeln!(out, "{w:<6} {:<6} {ctx:<16} {diff:<18} {:>3} touched  {doing}", i.kind.name(), i.touched);
    }
    ExitCode::SUCCESS
}

/// `agent-ui insight-files @id`: that agent's dirty touched files by repo:
/// each repo's work tree on a line of its own, then its files one per line,
/// repo-relative, after a tab. Exit 1 when the window has no insight, 2
/// usage.
pub fn run_files(tmux: Tmux, args: &[String]) -> std::process::ExitCode {
    use std::io::Write;
    use std::process::ExitCode;
    let [w] = args else {
        eprintln!("{USAGE}");
        return ExitCode::from(2);
    };
    let mut snap = tmux.snapshot();
    snap.windows.retain(|x| x.id == *w);
    let all = Engine::home().update(&snap, crate::text::now());
    let Some(i) = all.get(w.as_str()) else {
        eprintln!("insight-files: no transcript for {w}");
        return ExitCode::from(1);
    };
    let mut out = std::io::stdout().lock();
    for (root, rels) in by_repo(&i.dirty_files) {
        let _ = writeln!(out, "{}", root.display());
        for r in rels {
            let _ = writeln!(out, "\t{r}");
        }
    }
    ExitCode::SUCCESS
}

pub mod board;
pub use board::Board;

#[cfg(test)]
mod tests;
