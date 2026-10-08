//! The agent event log: one line per state change, written by the tab watcher
//! (unit U2), read here. Its absence is normal (an older watcher, a fresh
//! boot): every reader returns an empty list then.
//!
//! Path: `${TMPDIR:-/tmp}/agent-events.<uid>.log` (macOS's `$TMPDIR` ends in
//! `/`; joining handles that). One line per event, fields separated by US:
//!
//! `epoch US window_id US session US window_index US state US prev_state US
//! detail_kind US title US detail`
//!
//! Newest last; the writer caps the file. Only the tail is read (the last
//! [`TAIL_BYTES`]); the first, probably partial, line of that tail is dropped
//! unless the read started at offset 0. Malformed lines (fewer than 9
//! fields, a non-numeric epoch) are skipped; extra trailing fields are
//! ignored, so the writer can grow the format without breaking readers.

use crate::model::Cat;
use crate::tmux::US;
use std::io::{Read, Seek, SeekFrom};
use std::path::PathBuf;
use std::time::SystemTime;

/// How much of the log's end is read.
pub const TAIL_BYTES: u64 = 64 * 1024;

/// One state change.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Event {
    pub epoch: i64,
    pub window_id: String,
    pub session: String,
    pub index: u32,
    pub state: String,
    pub prev_state: String,
    pub detail_kind: String,
    pub title: String,
    pub detail: String,
}

impl Event {
    /// One log line → an Event, or None when malformed.
    pub fn parse(line: &str) -> Option<Event> {
        let p: Vec<&str> = line.trim_end_matches('\r').split(US).collect();
        if p.len() < 9 || p[0].is_empty() || !p[0].bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        Some(Event {
            epoch: p[0].parse().ok()?,
            window_id: p[1].into(),
            session: p[2].into(),
            index: p[3].parse().unwrap_or(0),
            state: p[4].into(),
            prev_state: p[5].into(),
            detail_kind: p[6].into(),
            title: p[7].into(),
            detail: p[8].into(),
        })
    }

    /// The bucket of the new state (running → working; see [`Cat::of_state`]).
    pub fn cat(&self) -> Option<Cat> {
        Cat::of_state(&self.state)
    }
}

/// `${TMPDIR:-/tmp}/agent-events.<uid>.log`.
pub fn log_path() -> PathBuf {
    let base = std::env::var("TMPDIR").ok().filter(|s| !s.is_empty()).unwrap_or_else(|| "/tmp".into());
    PathBuf::from(base).join(format!("agent-events.{}.log", crate::tmux::uid()))
}

/// Parse a tail chunk: `from_start` says whether it begins at offset 0 (else
/// its first line is a fragment and is dropped). Oldest first.
pub fn parse_tail(bytes: &[u8], from_start: bool) -> Vec<Event> {
    let text = String::from_utf8_lossy(bytes);
    let mut lines = text.split('\n');
    if !from_start {
        lines.next();
    }
    lines.filter_map(Event::parse).collect()
}

#[cfg(target_os = "macos")]
const O_NONBLOCK: i32 = 0x0004;
#[cfg(target_os = "macos")]
const O_NOFOLLOW: i32 = 0x0100;
#[cfg(not(target_os = "macos"))]
const O_NONBLOCK: i32 = 0o4000;
#[cfg(not(target_os = "macos"))]
const O_NOFOLLOW: i32 = 0o400000;

/// Read the log's tail → events, oldest first (empty when there is no log).
///
/// Opened O_NONBLOCK | O_NOFOLLOW and type-checked on the OPEN fd (like
/// `git::read_small`): a FIFO at the path would block a plain open()
/// forever, and a symlink could point anywhere. Anything but a regular file
/// reads as no log.
pub fn read_tail(path: &std::path::Path) -> Vec<Event> {
    use std::os::unix::fs::OpenOptionsExt;
    let opened = std::fs::OpenOptions::new().read(true).custom_flags(O_NONBLOCK | O_NOFOLLOW).open(path);
    let Ok(mut f) = opened else { return Vec::new() };
    let Ok(meta) = f.metadata() else { return Vec::new() };
    if !meta.is_file() {
        return Vec::new();
    }
    let len = meta.len();
    let start = len.saturating_sub(TAIL_BYTES);
    if f.seek(SeekFrom::Start(start)).is_err() {
        return Vec::new();
    }
    let mut buf = Vec::with_capacity((len - start) as usize);
    if f.take(TAIL_BYTES).read_to_end(&mut buf).is_err() {
        return Vec::new();
    }
    parse_tail(&buf, start == 0)
}

/// A log reader that re-reads only when the file changed (size or mtime),
/// so a 1 s refresh costs one stat when nothing happened.
#[derive(Debug, Default)]
pub struct EventLog {
    pub path: PathBuf,
    seen: Option<(u64, Option<SystemTime>)>,
    events: Vec<Event>,
}

impl EventLog {
    pub fn new(path: PathBuf) -> EventLog {
        EventLog { path, seen: None, events: Vec::new() }
    }

    /// The default path ([`log_path`]).
    pub fn default_path() -> EventLog {
        EventLog::new(log_path())
    }

    /// The newest `n` events, NEWEST FIRST.
    pub fn newest(&mut self, n: usize) -> Vec<Event> {
        let stamp = std::fs::metadata(&self.path).ok().map(|m| (m.len(), m.modified().ok()));
        if stamp != self.seen {
            self.events = if stamp.is_some() { read_tail(&self.path) } else { Vec::new() };
            self.seen = stamp;
        }
        self.events.iter().rev().take(n).cloned().collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn l(epoch: &str, state: &str, title: &str) -> String {
        [epoch, "@1", "main", "3", state, "running", "ask", title, "which deck?"].join("\x1f")
    }

    #[test]
    fn lines() {
        let e = Event::parse(&l("1700000000", "needs-input", "Kua Yu")).unwrap();
        assert_eq!((e.epoch, e.index, e.state.as_str(), e.title.as_str()), (1700000000, 3, "needs-input", "Kua Yu"));
        assert_eq!(e.cat(), Some(Cat::NeedsInput));
        assert!(Event::parse("").is_none());
        assert!(Event::parse("garbage").is_none());
        assert!(Event::parse(&l("12x", "done", "t")).is_none());
        assert!(Event::parse(&l("", "done", "t")).is_none());
        assert!(Event::parse("1\x1f@1\x1fmain").is_none()); // too few fields
        let extra = format!("{}\x1fextra", l("5", "done", "t"));
        assert_eq!(Event::parse(&extra).unwrap().detail, "which deck?");
        // An empty title/detail is fine; a bad index is 0.
        let e = Event::parse("5\x1f@2\x1fs\x1fx\x1fidle\x1f\x1f\x1f\x1f").unwrap();
        assert_eq!((e.index, e.title.as_str(), e.cat()), (0, "", Some(Cat::Idle)));
    }

    #[test]
    fn tails() {
        let body = [l("1", "running", "a"), l("2", "done", "b"), "junk".into(), l("3", "failed", "c")].join("\n") + "\n";
        let all = parse_tail(body.as_bytes(), true);
        assert_eq!(all.iter().map(|e| e.epoch).collect::<Vec<_>>(), [1, 2, 3]);
        // From mid-file: the first fragment is dropped even if it would parse.
        let mid = parse_tail(&body.as_bytes()[1..], false);
        assert_eq!(mid.iter().map(|e| e.epoch).collect::<Vec<_>>(), [2, 3]);
        // A torn last line (no newline yet) that is short is skipped.
        let torn = format!("{}\n5\x1f@1", l("4", "idle", "d"));
        assert_eq!(parse_tail(torn.as_bytes(), true).len(), 1);
    }

    #[test]
    fn reader_tail_and_absence() {
        let p = std::env::temp_dir().join(format!("agent-ui-events-{}.log", std::process::id()));
        let _ = std::fs::remove_file(&p);
        let mut log = EventLog::new(p.clone());
        assert!(log.newest(10).is_empty()); // no file
        // Bigger than the tail window: only the end is read, newest first.
        let mut body = String::new();
        let mut i = 0;
        while body.len() < (TAIL_BYTES as usize) * 2 {
            body.push_str(&l(&i.to_string(), "running", "padding padding padding"));
            body.push('\n');
            i += 1;
        }
        std::fs::write(&p, &body).unwrap();
        let got = log.newest(3);
        assert_eq!(got.iter().map(|e| e.epoch).collect::<Vec<_>>(), [i - 1, i - 2, i - 3]);
        let all = log.newest(usize::MAX);
        assert!(all.len() < i as usize && all.len() > 100);
        std::fs::remove_file(&p).unwrap();
        assert!(log.newest(3).is_empty());
    }

    #[test]
    fn fifo_and_symlink_are_no_log() {
        let d = std::env::temp_dir().join(format!("agent-ui-events-fifo-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        let fifo = d.join("events.log");
        assert!(std::process::Command::new("mkfifo").arg(&fifo).status().unwrap().success());
        // On a thread with a deadline: a regression must fail, not hang the suite.
        let (tx, rx) = std::sync::mpsc::channel();
        let f2 = fifo.clone();
        std::thread::spawn(move || {
            let mut log = EventLog::new(f2.clone());
            let _ = tx.send((read_tail(&f2).len(), log.newest(5).len()));
        });
        let got = rx.recv_timeout(std::time::Duration::from_secs(3)).expect("reading a FIFO blocked");
        assert_eq!(got, (0, 0));
        // A symlink to a real log is refused too (O_NOFOLLOW).
        let real = d.join("real.log");
        std::fs::write(&real, l("1", "done", "t") + "\n").unwrap();
        assert_eq!(read_tail(&real).len(), 1);
        let link = d.join("link.log");
        std::os::unix::fs::symlink(&real, &link).unwrap();
        assert!(read_tail(&link).is_empty());
        let _ = std::fs::remove_dir_all(&d);
    }
}
