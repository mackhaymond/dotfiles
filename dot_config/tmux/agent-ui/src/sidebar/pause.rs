//! The pause key's state in the sidebar (docs/agent-pause.md, "In the
//! sidebar"): prefix P pauses every working agent before the lid closes,
//! and Mack may close its popup (or the tab) and still want to see where
//! the pause stands. The sidebar reads the script's own files
//! (`progress.json`, `paused.json` in `$AGENT_PAUSE_STATE_DIR`, else
//! `~/.local/state/agent-pause`), the last wake and the boot
//! (`kern.waketime`, `kern.boottime`) and its own snapshot (window options,
//! the server's `#{start_time}`); the script publishes nothing extra.
//!
//! [`phase`] is the pure half (files parsed, a [`Clock`], the snapshot's
//! [`Windows`] in; a [`Phase`] out), first match wins:
//!
//! | phase | when |
//! |---|---|
//! | resuming | `progress.json`: mode resume, a press in flight |
//! | sending | `progress.json`: mode pause, a press in flight |
//! | ready | `paused.json` has targets and the machine woke (or booted) after `paused_at` |
//! | stopping | `paused.json` has targets and one's window is still WORKING |
//! | safe | `paused.json` has targets, none working, no wake since |
//! | (none) | anything else |
//!
//! A press is in flight while it is not done, its `<ms>-<pid>` process
//! lives, it started after the last boot and less than [`PRESS_MAX_AGE`]
//! ago: a toggle killed mid-delivery never holds the banner, even once its
//! pid is reused. A target whose window no longer exists counts as stopped,
//! and so does every target once the tmux server started after the pause
//! (a new server numbers its windows from @0 again: a saved id would name
//! some unrelated window). [`Reader`] is the cheap half: two `stat`s per
//! refresh, a file re-parsed only when its (inode, size, mtime) changed
//! (the script replaces both atomically), the wake sysctl only while
//! `paused.json` lists targets, the boot read once. Anything unreadable or
//! malformed is no file at all: the banner goes, the sidebar never fails.

use crate::json::{self, Value};
use crate::tmux::Snapshot;
use std::collections::HashSet;
use std::path::PathBuf;

/// Where the pause sits, as the banner says it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Phase {
    /// A resume press in flight: `n` agents being told to resume.
    Resuming { n: usize, labels: Vec<String> },
    /// A pause press in flight: `done` of `n` deliveries landed.
    Sending { done: usize, n: usize, labels: Vec<String> },
    /// Paused, and the machine woke (or booted) since: a click (or prefix
    /// P) resumes.
    Ready { labels: Vec<String> },
    /// Paused, `stopped` of `n` agents stopped; `working` still are.
    Stopping { stopped: usize, n: usize, working: Vec<String> },
    /// Paused, nothing working, no wake since: the lid can close.
    Safe { labels: Vec<String> },
}

impl Phase {
    /// A click on the banner resumes (`agent-pause.py resume`): only once
    /// the pause is settled (safe, or ready after a wake).
    pub fn clickable(&self) -> bool {
        matches!(self, Phase::Ready { .. } | Phase::Safe { .. })
    }

    /// Something is moving (a press in flight, agents still stopping): the
    /// banner shows a spinner.
    pub fn busy(&self) -> bool {
        matches!(self, Phase::Resuming { .. } | Phase::Sending { .. } | Phase::Stopping { .. })
    }

    /// A press is in flight: only the files can say when it lands.
    pub fn in_flight(&self) -> bool {
        matches!(self, Phase::Resuming { .. } | Phase::Sending { .. })
    }
}

/// How old a press not marked done may be and still count as in flight.
/// The script delivers to at most 8 agents at once, each call up to
/// `CALL_TIMEOUT` (60 s): 160 agents in the worst case fit in 20 minutes,
/// so no real press is ever cut off, while a dead one's banner (its pid
/// reused) goes within that.
pub const PRESS_MAX_AGE: f64 = 20.0 * 60.0;

/// The rows a press's progress lists only for the record, never as an
/// agent it messages: a pause's working windows with no agent pane
/// (`unidentified`), a resume's saved agents not found (`gone`).
fn extra(mode: &str, result: &str) -> bool {
    matches!((mode, result), ("pause", "unidentified") | ("resume", "gone"))
}

/// `progress.json`, as far as the banner needs it.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Progress {
    /// `pause` or `resume` (anything else is ignored).
    pub mode: String,
    pub done: bool,
    /// The rows the press messages, (label, result); `sending` until its
    /// delivery lands. The extras ([`extra`]) are left out.
    pub targets: Vec<(String, String)>,
    /// When the press started (its `press` is `<ms>-<pid>`), epoch seconds.
    pub started: Option<f64>,
    /// The press's process, when it says.
    pub pid: Option<i32>,
    /// That process still runs (set by the [`Reader`] on every read, never
    /// cached).
    pub alive: bool,
}

/// One saved agent of `paused.json`.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Saved {
    pub window_id: String,
    pub label: String,
}

/// `paused.json`, as far as the banner needs it.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Paused {
    /// `paused_at` as epoch seconds; None when missing or unreadable (then
    /// no wake can be compared with it, and the phase never says ready).
    pub paused_at: Option<f64>,
    pub targets: Vec<Saved>,
}

/// The clocks the phase compares with: now, the last wake from sleep and
/// the last boot (epoch seconds; None when unknown).
#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct Clock {
    pub now: f64,
    pub wake: Option<f64>,
    pub boot: Option<f64>,
}

/// What the snapshot says about the targets' windows ([`windows`]).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Windows {
    /// WORKING windows, by id.
    pub working: HashSet<String>,
    /// Every window, by id.
    pub exists: HashSet<String>,
    /// When the tmux server started (`#{start_time}`), if a client row said.
    pub server_start: Option<f64>,
}

/// A row's label: `label` (the agent's `@agent_summary`), else its
/// `location`, else `agent`; one line, control characters out.
fn label_of(t: &Value) -> String {
    let pick = |k: &str| t.get(k).and_then(Value::as_str).map(str::trim).filter(|s| !s.is_empty());
    let raw = pick("label").or_else(|| pick("location")).unwrap_or("agent");
    crate::text::sanitize(raw)
}

/// `progress.json`'s text → [`Progress`] (`alive` false: the reader sets
/// it). None unless it is an object with a `pause`/`resume` mode.
pub fn parse_progress(text: &str) -> Option<Progress> {
    let v = json::parse(text).ok()?;
    let mode = v.get("mode")?.as_str()?.to_string();
    if mode != "pause" && mode != "resume" {
        return None;
    }
    // No `done` at all reads as done: a half-understood record never
    // holds a banner up.
    let done = v.get("done").is_none_or(|d| d.as_bool().unwrap_or(true));
    let targets = v
        .get("targets")
        .and_then(Value::as_array)
        .unwrap_or(&[])
        .iter()
        .filter(|t| matches!(t, Value::Obj(_)))
        .map(|t| (label_of(t), t.get("result").and_then(Value::as_str).unwrap_or("").to_string()))
        .filter(|(_, r)| !extra(&mode, r))
        .collect();
    let press = v.get("press").and_then(Value::as_str).and_then(|p| p.split_once('-'));
    let started = press.and_then(|(ms, _)| ms.parse::<u64>().ok()).map(|ms| ms as f64 / 1000.0);
    let pid = press.and_then(|(_, pid)| pid.parse().ok());
    Some(Progress { mode, done, targets, started, pid, alive: false })
}

/// `paused.json`'s text → [`Paused`]. None unless it is an object whose
/// `targets` is a list (the script's own `load_state` rule); entries that
/// are not objects are skipped.
pub fn parse_paused(text: &str) -> Option<Paused> {
    let v = json::parse(text).ok()?;
    let targets = v.get("targets")?.as_array()?;
    let targets = targets
        .iter()
        .filter(|t| matches!(t, Value::Obj(_)))
        .map(|t| Saved { window_id: t.get("window_id").and_then(Value::as_str).unwrap_or("").to_string(), label: label_of(t) })
        .collect();
    let paused_at = v.get("paused_at").and_then(Value::as_str).and_then(parse_iso);
    Some(Paused { paused_at, targets })
}

/// Days from 1970-01-01 to a civil date (Howard Hinnant's algorithm).
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// An ISO-8601 time WITH an offset (`2026-10-19T14:03:22-07:00`, `…Z`,
/// `…+0530`, fractional seconds allowed; Python's
/// `datetime.now().astimezone().isoformat()`) → epoch seconds. A time
/// without an offset is local and says nothing certain: None.
pub fn parse_iso(s: &str) -> Option<f64> {
    let s = s.trim();
    let b = s.as_bytes();
    let num = |r: std::ops::Range<usize>| -> Option<i64> {
        let t = s.get(r)?;
        (!t.is_empty() && t.bytes().all(|c| c.is_ascii_digit())).then(|| t.parse().ok()).flatten()
    };
    if b.len() < 16 || b[4] != b'-' || b[7] != b'-' || !matches!(b[10], b'T' | b't' | b' ') || b[13] != b':' {
        return None;
    }
    let (y, mo, d, h, mi) = (num(0..4)?, num(5..7)?, num(8..10)?, num(11..13)?, num(14..16)?);
    let mut i = 16;
    let mut sec = 0.0;
    if b.get(i) == Some(&b':') {
        let st = i + 1;
        let mut e = st;
        while e < b.len() && (b[e].is_ascii_digit() || b[e] == b'.') {
            e += 1;
        }
        sec = s.get(st..e)?.parse::<f64>().ok()?;
        i = e;
    }
    if !(1..=12).contains(&mo) || !(1..=31).contains(&d) || h > 23 || mi > 59 || !(0.0..61.0).contains(&sec) {
        return None;
    }
    let off = match s.get(i..)? {
        "Z" | "z" => 0,
        rest if rest.starts_with('+') || rest.starts_with('-') => {
            let sign = if rest.starts_with('-') { -1 } else { 1 };
            let digits: String = rest[1..].chars().filter(|c| *c != ':').collect();
            if digits.len() != 4 || !digits.bytes().all(|c| c.is_ascii_digit()) {
                return None;
            }
            let (oh, om): (i64, i64) = (digits[..2].parse().ok()?, digits[2..].parse().ok()?);
            if oh > 23 || om > 59 {
                return None;
            }
            sign * (oh * 3600 + om * 60)
        }
        _ => return None,
    };
    let days = days_from_civil(y, mo, d);
    Some((days * 86_400 + h * 3600 + mi * 60 - off) as f64 + sec)
}

/// The snapshot's [`Windows`]: the WORKING ones (the script's own rule:
/// `@agent_state` running, or `@agent_workflow` / `@agent_cua` set), every
/// window, by id, and the server's start (its first client row's
/// `#{start_time}`). From the RAW snapshot (before the sidebar blanks its
/// spaces' homes): the script reads the options as tmux holds them.
pub fn windows(snap: &Snapshot) -> Windows {
    let rows = &snap.windows;
    let working = rows.iter().filter(|w| w.state == "running" || !w.workflow.is_empty() || !w.cua.is_empty()).map(|w| w.id.clone()).collect();
    let exists = rows.iter().map(|w| w.id.clone()).collect();
    let server_start = snap.clients.first().and_then(|c| c.status.server_start.trim().parse::<f64>().ok()).filter(|&s| s > 0.0);
    Windows { working, exists, server_start }
}

/// The press in `p` is still in flight: not done, its process alive, begun
/// after the last boot and less than [`PRESS_MAX_AGE`] ago. A record with
/// no readable start is no press.
pub fn in_flight(p: &Progress, clock: Clock) -> bool {
    let Some(at) = p.started else { return false };
    !p.done && p.alive && clock.boot.is_none_or(|b| at >= b) && clock.now - at <= PRESS_MAX_AGE
}

/// THE phase (module docs), first match wins.
pub fn phase(progress: Option<&Progress>, paused: Option<&Paused>, clock: Clock, win: &Windows) -> Option<Phase> {
    if let Some(p) = progress.filter(|p| in_flight(p, clock)) {
        let labels: Vec<String> = p.targets.iter().map(|(l, _)| l.clone()).collect();
        let n = p.targets.len();
        if p.mode == "resume" {
            return Some(Phase::Resuming { n, labels });
        }
        if p.mode == "pause" {
            let done = p.targets.iter().filter(|(_, r)| r != "sending").count();
            return Some(Phase::Sending { done, n, labels });
        }
    }
    let st = paused.filter(|s| !s.targets.is_empty())?;
    let labels: Vec<String> = st.targets.iter().map(|t| t.label.clone()).collect();
    // A wake after the pause, or a boot (a reboot, or a shutdown with the
    // lid): the lid has been shut and opened again.
    let after = |t: Option<f64>| matches!((st.paused_at, t), (Some(at), Some(t)) if t > at);
    if after(clock.wake) || after(clock.boot) {
        return Some(Phase::Ready { labels });
    }
    // A server started since the pause numbers its windows afresh: no
    // saved id names the agent it did then.
    let fresh = after(win.server_start);
    // A target is still working while its window exists and works; a
    // window that is gone (a resurrect restore renumbers them) has stopped.
    let busy = |t: &&Saved| !fresh && !t.window_id.is_empty() && win.exists.contains(&t.window_id) && win.working.contains(&t.window_id);
    let working: Vec<String> = st.targets.iter().filter(busy).map(|t| t.label.clone()).collect();
    if !working.is_empty() {
        let n = st.targets.len();
        return Some(Phase::Stopping { stopped: n - working.len(), n, working });
    }
    Some(Phase::Safe { labels })
}

/// A `struct timeval` sysctl (`kern.waketime`, `kern.boottime`) as epoch
/// seconds; None when unknown or zero (never slept since boot).
#[cfg(target_os = "macos")]
fn sysctl_time(name: &std::ffi::CStr) -> Option<f64> {
    #[repr(C)]
    #[derive(Default)]
    struct Timeval {
        sec: i64,
        usec: i32,
    }
    extern "C" {
        fn sysctlbyname(name: *const std::ffi::c_char, oldp: *mut std::ffi::c_void, oldlenp: *mut usize, newp: *mut std::ffi::c_void, newlen: usize) -> i32;
    }
    let mut tv = Timeval::default();
    let mut len = std::mem::size_of::<Timeval>();
    // SAFETY: a NUL-terminated name, a buffer of `len` bytes we own, no new value.
    let rc = unsafe { sysctlbyname(name.as_ptr(), (&mut tv as *mut Timeval).cast(), &mut len, std::ptr::null_mut(), 0) };
    (rc == 0 && tv.sec > 0).then(|| tv.sec as f64 + f64::from(tv.usec) / 1e6)
}

#[cfg(not(target_os = "macos"))]
fn sysctl_time(_: &std::ffi::CStr) -> Option<f64> {
    None
}

/// The last wake from sleep (`sysctl kern.waketime`).
pub fn waketime() -> Option<f64> {
    sysctl_time(c"kern.waketime")
}

/// The last boot (`sysctl kern.boottime`).
pub fn boottime() -> Option<f64> {
    sysctl_time(c"kern.boottime")
}

/// The state directory: `$AGENT_PAUSE_STATE_DIR` (when non-empty), else
/// `~/.local/state/agent-pause` (the script's `STATE_DIR`).
pub fn state_dir() -> PathBuf {
    match std::env::var_os("AGENT_PAUSE_STATE_DIR").filter(|s| !s.is_empty()) {
        Some(d) => PathBuf::from(d),
        None => PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".local/state/agent-pause"),
    }
}

/// What a file looked like when last parsed: (inode, size, mtime ns).
type Stamp = (u64, u64, i128);

/// One state file, re-parsed only when its [`Stamp`] changes.
#[derive(Debug)]
struct Cached<T> {
    path: PathBuf,
    stamp: Option<Stamp>,
    value: Option<T>,
    /// How many times it was parsed (observable for the tests).
    parses: u32,
}

impl<T: Clone> Cached<T> {
    fn new(path: PathBuf) -> Self {
        Cached { path, stamp: None, value: None, parses: 0 }
    }

    fn get(&mut self, parse: fn(&str) -> Option<T>) -> Option<T> {
        use std::os::unix::fs::MetadataExt;
        let Ok(m) = std::fs::metadata(&self.path) else {
            self.stamp = None;
            self.value = None;
            return None;
        };
        let stamp = (m.ino(), m.len(), i128::from(m.mtime()) * 1_000_000_000 + i128::from(m.mtime_nsec()));
        if self.stamp != Some(stamp) {
            self.stamp = Some(stamp);
            self.parses += 1;
            self.value = std::fs::read_to_string(&self.path).ok().and_then(|t| parse(&t));
        }
        self.value.clone()
    }
}

/// The cheap reader the sidebar keeps between refreshes ([`Reader::phase`]).
#[derive(Debug)]
pub struct Reader {
    progress: Cached<Progress>,
    paused: Cached<Paused>,
    /// The last wake as read (tests replace it).
    wake: fn() -> Option<f64>,
    /// The boot as read (tests replace it); read once, it never changes.
    boot_fn: fn() -> Option<f64>,
    boot: Option<Option<f64>>,
}

impl Reader {
    pub fn new(dir: PathBuf) -> Reader {
        Reader {
            progress: Cached::new(dir.join("progress.json")),
            paused: Cached::new(dir.join("paused.json")),
            wake: waketime,
            boot_fn: boottime,
            boot: None,
        }
    }

    /// [`state_dir`]'s reader.
    pub fn home() -> Reader {
        Reader::new(state_dir())
    }

    /// The phase now: the two files (re-parsed only when changed), the
    /// press's liveness, the clocks (the wake only while targets are saved
    /// and no press is in flight; the boot once, when there is anything to
    /// compare it with).
    pub fn phase(&mut self, win: &Windows) -> Option<Phase> {
        let progress = self.progress.get(parse_progress).map(|mut p| {
            // Only a press not done needs its process checked.
            p.alive = !p.done && p.pid.is_none_or(super::term::alive);
            p
        });
        let paused = self.paused.get(parse_paused).filter(|p| !p.targets.is_empty());
        if progress.as_ref().is_none_or(|p| p.done) && paused.is_none() {
            return None;
        }
        let boot_fn = self.boot_fn;
        let boot = *self.boot.get_or_insert_with(boot_fn);
        let mut clock = Clock { now: crate::text::now(), wake: None, boot };
        if paused.is_some() && !progress.as_ref().is_some_and(|p| in_flight(p, clock)) {
            clock.wake = (self.wake)();
        }
        phase(progress.as_ref(), paused.as_ref(), clock, win)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn set(ids: &[&str]) -> HashSet<String> {
        ids.iter().map(|s| s.to_string()).collect()
    }

    /// Windows `exists`, of which `working` work, on a server of unknown start.
    fn wins(working: &[&str], exists: &[&str]) -> Windows {
        Windows { working: set(working), exists: set(exists), server_start: None }
    }

    fn paused(at: &str, targets: &[(&str, &str)]) -> Paused {
        let t: Vec<String> = targets.iter().map(|(w, l)| format!(r#"{{"window_id":"{w}","label":"{l}","pane_id":"%1","result":"sent"}}"#)).collect();
        parse_paused(&format!(r#"{{"version":1,"paused_at":"{at}","targets":[{}],"failed":[]}}"#, t.join(","))).unwrap()
    }

    /// A press begun 30 s before [`clock`]'s now, by a live process.
    fn progress(mode: &str, done: bool, results: &[&str]) -> Progress {
        let t: Vec<String> = results.iter().enumerate().map(|(i, r)| format!(r#"{{"label":"A{i}","result":"{r}"}}"#)).collect();
        let ms = ((NOW - 30.0) * 1000.0) as u64;
        let mut p = parse_progress(&format!(
            r#"{{"press":"{ms}-4242","mode":"{mode}","done":{done},"outcome":"","message":"","targets":[{}]}}"#,
            t.join(",")
        ))
        .unwrap();
        p.alive = true;
        p
    }

    const AT: &str = "2026-10-19T14:00:00-07:00";
    /// AT as epoch seconds (21:00 UTC).
    const AT_S: f64 = 1_792_443_600.0;
    /// The tests' now: an hour after AT.
    const NOW: f64 = AT_S + 3600.0;

    /// Now, a wake, a boot a day before the pause.
    fn clock(wake: Option<f64>) -> Clock {
        Clock { now: NOW, wake, boot: Some(AT_S - 86_400.0) }
    }

    #[test]
    fn iso_with_offsets() {
        assert_eq!(parse_iso(AT), Some(AT_S));
        assert_eq!(parse_iso("2026-10-19T21:00:00Z"), Some(AT_S));
        assert_eq!(parse_iso("2026-10-19T21:00:00+00:00"), Some(AT_S));
        assert_eq!(parse_iso("2026-10-20T02:30:00+0530"), Some(AT_S));
        assert_eq!(parse_iso("2026-10-19T21:00:00.250+00:00"), Some(AT_S + 0.25));
        assert_eq!(parse_iso("2026-10-19 21:00Z"), Some(AT_S));
        assert_eq!(parse_iso("1970-01-01T00:00:00Z"), Some(0.0));
        assert_eq!(parse_iso("2000-03-01T00:00:00Z"), Some(951_868_800.0));
        // No offset (local, unknowable here), junk: None.
        for bad in ["2026-10-19T21:00:00", "", "yesterday", "2026-13-01T00:00:00Z", "2026-10-19T21:00:00+7", "2026-10-19T25:00:00Z", "2026-10-19T21:00:00Zx"] {
            assert_eq!(parse_iso(bad), None, "{bad}");
        }
    }

    #[test]
    fn nothing_without_files() {
        assert_eq!(phase(None, None, clock(Some(AT_S + 9.0)), &wins(&[], &[])), None);
        // A saved set with no targets is no pause (the toggle would pause).
        assert_eq!(phase(None, Some(&paused(AT, &[])), clock(None), &wins(&[], &[])), None);
        // A finished press is history.
        let done = progress("pause", true, &["sent", "held"]);
        assert_eq!(phase(Some(&done), None, clock(None), &wins(&[], &[])), None);
    }

    #[test]
    fn a_press_in_flight_comes_first() {
        let st = paused(AT, &[("@1", "One")]);
        let ex = wins(&["@1"], &["@1"]);
        let p = progress("pause", false, &["sent", "sending", "held"]);
        assert_eq!(
            phase(Some(&p), Some(&st), clock(Some(AT_S + 60.0)), &ex),
            Some(Phase::Sending { done: 2, n: 3, labels: vec!["A0".into(), "A1".into(), "A2".into()] })
        );
        let r = progress("resume", false, &["sending", "sending"]);
        assert_eq!(phase(Some(&r), Some(&st), clock(Some(AT_S + 60.0)), &ex), Some(Phase::Resuming { n: 2, labels: vec!["A0".into(), "A1".into()] }));
        // A press whose toggle died is no press: the saved set decides.
        let dead = Progress { alive: false, ..r };
        assert_eq!(phase(Some(&dead), Some(&st), clock(None), &wins(&[], &["@1"])), Some(Phase::Safe { labels: vec!["One".into()] }));
    }

    /// The press's own record-only rows are no agents it messages: a
    /// pause's `unidentified` windows, a resume's `gone` agents.
    #[test]
    fn extras_are_not_counted() {
        let p = progress("pause", false, &["sent", "sending", "unidentified", "unidentified"]);
        assert_eq!(p.targets.len(), 2);
        assert_eq!(phase(Some(&p), None, clock(None), &wins(&[], &[])), Some(Phase::Sending { done: 1, n: 2, labels: vec!["A0".into(), "A1".into()] }));
        let r = progress("resume", false, &["sending", "gone", "sent"]);
        assert_eq!(phase(Some(&r), None, clock(None), &wins(&[], &[])), Some(Phase::Resuming { n: 2, labels: vec!["A0".into(), "A2".into()] }));
        // (Each mode drops only its own kind.)
        assert_eq!(progress("resume", false, &["unidentified"]).targets.len(), 1);
        assert_eq!(progress("pause", false, &["gone"]).targets.len(), 1);
    }

    /// A toggle killed mid-delivery leaves `done: false` behind. Once its
    /// pid is reused (a live, unrelated process), the press must still read
    /// as dead: begun before the last boot, or older than the cap.
    #[test]
    fn a_killed_press_never_holds_the_banner() {
        let st = paused(AT, &[("@1", "One")]);
        let none = wins(&[], &["@1"]);
        let safe = Some(Phase::Safe { labels: vec!["One".into()] });
        let mut p = progress("pause", false, &["sending"]);
        assert!(in_flight(&p, clock(None)));
        // Begun before the boot (a reboot since), its pid alive again.
        let rebooted = Clock { boot: Some(NOW - 10.0), ..clock(None) };
        assert!(!in_flight(&p, rebooted));
        assert_eq!(phase(Some(&p), Some(&st), rebooted, &none), Some(Phase::Ready { labels: vec!["One".into()] }));
        // Older than the cap; just under it is still a press.
        p.started = Some(NOW - PRESS_MAX_AGE - 1.0);
        assert!(!in_flight(&p, clock(None)));
        assert_eq!(phase(Some(&p), Some(&st), clock(None), &none), safe);
        p.started = Some(NOW - PRESS_MAX_AGE + 1.0);
        assert!(in_flight(&p, clock(None)));
        // No readable start: no press.
        p.started = None;
        assert!(!in_flight(&p, clock(None)));
    }

    // The cap outlasts the script's worst case: 160 agents, 8 at a time,
    // 60 s each.
    const _: () = assert!(PRESS_MAX_AGE >= (160.0 / 8.0) * 60.0);

    #[test]
    fn stopping_safe_and_gone_windows() {
        let st = paused(AT, &[("@1", "One"), ("@2", "Two"), ("@3", "Three"), ("", "NoWin")]);
        // @2 still works; @3 is gone (counts as stopped, even if some row
        // with its id were working elsewhere: it is not in `exists`).
        assert_eq!(
            phase(None, Some(&st), clock(None), &wins(&["@2", "@3"], &["@1", "@2"])),
            Some(Phase::Stopping { stopped: 3, n: 4, working: vec!["Two".into()] })
        );
        let all = vec!["One".to_string(), "Two".into(), "Three".into(), "NoWin".into()];
        assert_eq!(phase(None, Some(&st), clock(None), &wins(&[], &["@1", "@2"])), Some(Phase::Safe { labels: all.clone() }));
        // Every window gone (a resurrect restore): stopped, safe.
        assert_eq!(phase(None, Some(&st), clock(None), &wins(&["@9"], &["@9"])), Some(Phase::Safe { labels: all }));
    }

    /// A tmux server started after the pause numbers its windows from @0
    /// again: a saved `@2` is now some unrelated window, working or not.
    /// Every saved id counts as gone then; one started before is trusted.
    #[test]
    fn a_new_server_reuses_window_ids() {
        let st = paused(AT, &[("@1", "One"), ("@2", "Two")]);
        let mut w = wins(&["@2"], &["@0", "@1", "@2"]);
        let stopping = Some(Phase::Stopping { stopped: 1, n: 2, working: vec!["Two".into()] });
        assert_eq!(phase(None, Some(&st), clock(None), &w), stopping);
        w.server_start = Some(AT_S - 600.0);
        assert_eq!(phase(None, Some(&st), clock(None), &w), stopping);
        w.server_start = Some(AT_S + 600.0);
        assert_eq!(phase(None, Some(&st), clock(None), &w), Some(Phase::Safe { labels: vec!["One".into(), "Two".into()] }));
        // From the snapshot: the first client row's #{start_time}.
        let mut st_fields = vec![String::new(); crate::tmux::STATUS_FORMATS.len()];
        st_fields[32] = format!("{}", AT_S as i64 + 600);
        let head = ["", "client", "/dev/ttys999", "@2", "main", "", "", "", "4242"].map(String::from);
        let client = head.into_iter().chain(st_fields).collect::<Vec<_>>().join("\x1f");
        let snap = Snapshot::parse(&[crate::tmux::tests::row("main", 1, "@2", &[("state", "running")]), client].join("\n"));
        assert_eq!(windows(&snap).server_start, Some(AT_S + 600.0));
        assert_eq!(phase(None, Some(&st), clock(None), &windows(&snap)), Some(Phase::Safe { labels: vec!["One".into(), "Two".into()] }));
        assert_eq!(windows(&Snapshot::parse("")).server_start, None);
    }

    #[test]
    fn a_wake_or_a_boot_after_the_pause_means_ready() {
        let st = paused(AT, &[("@1", "One")]);
        let ex = wins(&["@1"], &["@1"]);
        let idle = wins(&[], &["@1"]);
        let ready = Some(Phase::Ready { labels: vec!["One".into()] });
        let safe = Some(Phase::Safe { labels: vec!["One".into()] });
        // Woke after: ready, even with the agent working again.
        assert_eq!(phase(None, Some(&st), clock(Some(AT_S + 1.0)), &ex), ready);
        // Woke before (or exactly then), or unknown: by the working check.
        assert_eq!(phase(None, Some(&st), clock(Some(AT_S - 1.0)), &idle), safe);
        assert_eq!(phase(None, Some(&st), clock(Some(AT_S)), &idle), safe);
        assert_eq!(phase(None, Some(&st), clock(None), &ex), Some(Phase::Stopping { stopped: 0, n: 1, working: vec!["One".into()] }));
        // Booted after the pause (a reboot; the wake is from before, or
        // none since boot): ready, never "safe to close".
        let rebooted = Clock { now: NOW, wake: None, boot: Some(AT_S + 120.0) };
        assert_eq!(phase(None, Some(&st), rebooted, &idle), ready);
        assert_eq!(phase(None, Some(&st), Clock { wake: Some(AT_S - 5.0), ..rebooted }, &ex), ready);
        // The same instant in another zone compares the same: 23:30 in +09:30
        // is 14:00 UTC, before a wake at 14:00:30 UTC.
        let east = paused("2026-10-19T23:30:00+09:30", &[("@1", "One")]);
        let wake = parse_iso("2026-10-19T14:00:30Z");
        assert_eq!(phase(None, Some(&east), clock(wake), &idle), ready);
        let west = paused("2026-10-19T07:00:31-07:00", &[("@1", "One")]);
        assert_eq!(phase(None, Some(&west), clock(wake), &idle), safe);
        // No usable paused_at: never ready (no wake can be compared).
        let naive = paused("2026-10-19T14:00:00", &[("@1", "One")]);
        assert_eq!(naive.paused_at, None);
        assert_eq!(phase(None, Some(&naive), Clock { now: NOW, wake: Some(AT_S + 1e6), boot: Some(AT_S + 1e6) }, &idle), safe);
    }

    #[test]
    fn malformed_files_are_no_files() {
        for bad in ["", "{", "[]", "null", "{\"targets\": 3}", "{\"targets\": {}}", "{\"paused_at\": \"x\"}", "\u{0}"] {
            assert_eq!(parse_paused(bad), None, "{bad:?}");
        }
        for bad in ["", "{", "[]", "{\"mode\": 1}", "{\"mode\": \"toggle\", \"done\": false}", "{\"done\": false}"] {
            assert_eq!(parse_progress(bad), None, "{bad:?}");
        }
        // Odd entries are skipped; labels fall back; a half record is done.
        let p = parse_paused(r#"{"targets":[1,"x",{"window_id":"@4","location":"main:2.1"},{"label":"  "}],"paused_at":7}"#).unwrap();
        assert_eq!(p.targets, vec![Saved { window_id: "@4".into(), label: "main:2.1".into() }, Saved { window_id: "".into(), label: "agent".into() }]);
        assert_eq!(p.paused_at, None);
        let q = parse_progress(r#"{"mode":"pause","targets":[{"result":"sending"}]}"#).unwrap();
        assert!(q.done && q.pid.is_none() && q.started.is_none());
        let r = parse_progress(r#"{"mode":"pause","done":false,"press":"1792418628123-34"}"#).unwrap();
        assert_eq!((r.started, r.pid), (Some(1_792_418_628.123), Some(34)));
        assert_eq!(parse_progress(r#"{"mode":"pause","done":false,"press":"x-34"}"#).unwrap().started, None);
        // Control characters never reach the screen.
        let r = parse_paused("{\"targets\":[{\"label\":\"a\\u001b[31mb\\nc\"}]}").unwrap();
        assert!(!r.targets[0].label.chars().any(char::is_control), "{:?}", r.targets[0].label);
    }

    #[test]
    fn the_reader_parses_only_what_changed() {
        let dir = std::env::temp_dir().join(format!("agent-ui-pause-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let mut r = Reader::new(dir.clone());
        let now = crate::text::now();
        r.wake = || Some(AT_S + 5.0);
        r.boot_fn = || Some(AT_S - 86_400.0);
        let none = wins(&[], &[]);
        assert_eq!(r.phase(&none), None);
        let write = |name: &str, text: &str| {
            let tmp = dir.join(format!(".{name}.tmp"));
            std::fs::write(&tmp, text).unwrap();
            std::fs::rename(&tmp, dir.join(name)).unwrap();
        };
        write("paused.json", &format!(r#"{{"paused_at":"{AT}","targets":[{{"window_id":"@1","label":"One"}}]}}"#));
        assert_eq!(r.phase(&none), Some(Phase::Ready { labels: vec!["One".into()] }));
        assert_eq!(r.phase(&none), Some(Phase::Ready { labels: vec!["One".into()] }));
        assert_eq!(r.paused.parses, 1);
        // A press in flight by a live process (this one), begun just now.
        let (me, ms) = (std::process::id(), (now * 1000.0) as u64);
        write("progress.json", &format!(r#"{{"press":"{ms}-{me}","mode":"resume","done":false,"targets":[{{"label":"One","result":"sending"}}]}}"#));
        assert_eq!(r.phase(&none), Some(Phase::Resuming { n: 1, labels: vec!["One".into()] }));
        // Its process dead (no such pid): not in flight.
        write("progress.json", &format!(r#"{{"press":"{ms}-2147483646","mode":"resume","done":false,"targets":[]}}"#));
        assert_eq!(r.phase(&none), Some(Phase::Ready { labels: vec!["One".into()] }));
        // A torn or junk file: nothing, never a crash; gone: nothing.
        write("paused.json", "{\"targets\": [");
        assert_eq!(r.phase(&none), None);
        std::fs::remove_file(dir.join("paused.json")).unwrap();
        assert_eq!(r.phase(&none), None);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn working_is_the_scripts_rule() {
        let snap = Snapshot::parse(
            &[
                crate::tmux::tests::row("main", 1, "@1", &[("state", "running")]),
                crate::tmux::tests::row("main", 2, "@2", &[("state", "idle"), ("workflow", "1")]),
                crate::tmux::tests::row("main", 3, "@3", &[("state", "needs-input"), ("cua", "x")]),
                crate::tmux::tests::row("main", 4, "@4", &[("state", "done")]),
                crate::tmux::tests::row("main", 5, "@5", &[("state", "failed")]),
            ]
            .join("\n"),
        );
        let w = windows(&snap);
        assert_eq!(w.working, set(&["@1", "@2", "@3"]));
        assert_eq!(w.exists, set(&["@1", "@2", "@3", "@4", "@5"]));
    }

    #[test]
    fn the_clocks_read() {
        // (This machine: booted once, before now.)
        if cfg!(target_os = "macos") {
            let b = boottime().expect("kern.boottime");
            assert!(b > 1e9 && b < crate::text::now());
        }
    }
}
