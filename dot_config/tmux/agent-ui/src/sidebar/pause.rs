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
//! A target whose PAUSE tmux-mcp HELD (`"result": "held"` in either file)
//! and whose window still works is [`Held`]: sending and stopping list it
//! apart ([`Phase::held`]), with tmux-mcp's reason (its `detail`, `held
//! for @h (…): <why>. It will be delivered…`: [`held_why`]) and where its
//! window lives in the snapshot, so the banner can say what to do
//! ([`ask`]) and jump there. The PAUSE goes out urgent, through a draft,
//! so it is held only while tmux-mcp cannot read the prompt box: copy
//! mode, a dialog or menu up, no box on screen. Once its window stops
//! working it is just stopped, and stays so for that pause: `paused.json`
//! keeps `held` until the resume, so the [`Reader`] remembers each held
//! window it has seen stopped (per `paused_at`, per press) and never holds
//! it again (an agent woken while paused is just still working).
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
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;

/// Where the pause sits, as the banner says it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Phase {
    /// A resume press in flight: `n` agents being told to resume.
    Resuming { n: usize, labels: Vec<String> },
    /// A pause press in flight: `done` of `n` deliveries landed (a held one
    /// counts: it is delivered once the box is clear); `held` still work.
    Sending { done: usize, n: usize, labels: Vec<String>, held: Vec<Held> },
    /// Paused, and the machine woke (or booted) since: a click (or prefix
    /// P) resumes.
    Ready { labels: Vec<String> },
    /// Paused, `stopped` of `n` agents stopped; `working` still are, and
    /// `held` still are because tmux-mcp holds their PAUSE (never in
    /// `working` too).
    Stopping { stopped: usize, n: usize, working: Vec<String>, held: Vec<Held> },
    /// Paused, nothing working, no wake since: the lid can close.
    Safe { labels: Vec<String> },
}

/// A target whose PAUSE tmux-mcp holds (module docs) while its window
/// still works.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Held {
    pub label: String,
    pub window_id: String,
    /// tmux-mcp's reason ([`held_why`]; "" when its detail did not say).
    pub why: String,
    /// The window's session in the snapshot ([`Windows::places`]): where a
    /// click jumps. Empty when the snapshot has no such window.
    pub session: String,
    /// Its lifetime letter (`@agent_key`), when it is listed (not parked,
    /// not in a hidden session).
    pub letter: Option<char>,
}

impl Phase {
    /// The held targets ([`Held`]): only a pause press in flight or agents
    /// still stopping have any.
    pub fn held(&self) -> &[Held] {
        match self {
            Phase::Sending { held, .. } | Phase::Stopping { held, .. } => held,
            _ => &[],
        }
    }

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
    /// The rows the press messages; each `sending` until its delivery
    /// lands. The extras ([`extra`]) are left out.
    pub targets: Vec<Sent>,
    /// `press` as written (`<ms>-<pid>`): which press this is.
    pub press: String,
    /// When the press started (its `press` is `<ms>-<pid>`), epoch seconds.
    pub started: Option<f64>,
    /// The press's process, when it says.
    pub pid: Option<i32>,
    /// That process still runs (set by the [`Reader`] on every read, never
    /// cached).
    pub alive: bool,
}

/// One row a press messages (`progress.json`'s `targets`).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Sent {
    pub label: String,
    /// `sending`, `sent`, `held`, `failed`, `unknown`, ...
    pub result: String,
    pub window_id: String,
    /// `result` is `held`, and the [`Reader`] has not seen its window stop.
    pub held: bool,
    /// tmux-mcp's reason ([`held_why`]).
    pub why: String,
}

/// One saved agent of `paused.json`.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Saved {
    pub window_id: String,
    pub label: String,
    /// Its PAUSE was held (`"result": "held"`), and the [`Reader`] has not
    /// seen its window stop since.
    pub held: bool,
    /// tmux-mcp's reason ([`held_why`]).
    pub why: String,
}

/// `paused.json`, as far as the banner needs it.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Paused {
    /// `paused_at` as epoch seconds; None when missing or unreadable (then
    /// no wake can be compared with it, and the phase never says ready).
    pub paused_at: Option<f64>,
    /// `paused_at` as written: which pause this is (the [`Reader`]'s memo).
    pub stamp: String,
    pub targets: Vec<Saved>,
    /// `glow_dismissed_at` is set (`agent-pause.py dismiss`: the notch's
    /// "all paused" ring turned off). Only the banner's dim `glow off`
    /// reads it: never the phase, so the banner stays until resume or void
    /// (docs/agent-pause.md "Dismiss and void").
    pub glow_off: bool,
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
    /// Every window by id → (its session, its letter when listed): where a
    /// [`Held`] row jumps. A window linked into two sessions: a shown
    /// session's row first.
    pub places: HashMap<String, (String, Option<char>)>,
}

/// A row's label: `label` (the agent's `@agent_summary`), else its
/// `location`, else `agent`; one line, control characters out.
fn label_of(t: &Value) -> String {
    let pick = |k: &str| t.get(k).and_then(Value::as_str).map(str::trim).filter(|s| !s.is_empty());
    let raw = pick("label").or_else(|| pick("location")).unwrap_or("agent");
    crate::text::sanitize(raw)
}

/// A row's string field `k` ("" when missing or not a string).
fn str_of(t: &Value, k: &str) -> String {
    t.get(k).and_then(Value::as_str).unwrap_or("").to_string()
}

/// A held row's reason, from its `detail` (message_agent's reply line,
/// tmux-mcp server.ts `held for @<handle> (<loc>, pane <id>): <why>. It
/// will be delivered once…`): `<why>`, one line; "" for any other detail.
pub fn held_why(detail: &str) -> String {
    let Some(rest) = detail.trim().strip_prefix("held for").and_then(|r| r.split_once("): ")).map(|(_, w)| w) else {
        return String::new();
    };
    let why = rest.split(". It will be delivered").next().unwrap_or("");
    crate::text::sanitize(why.trim().trim_end_matches('.').trim())
}

/// What Mack can do about a held PAUSE, from tmux-mcp's reason (its
/// `readBox` and `deliverGuarded` wording): leave copy mode, answer or
/// close what is over the box, bring the box back, clear a draft. None for
/// a reason it does not know (the banner shows the reason itself).
pub fn ask(why: &str) -> Option<&'static str> {
    let w = why.to_ascii_lowercase();
    Some(if w.contains("copy mode") {
        "leave copy mode"
    } else if w.contains("permission or question prompt") {
        "answer its prompt"
    } else if w.contains("does not have the cursor") {
        "close its dialog"
    } else if w.starts_with("no prompt box") {
        "show its prompt"
    } else if w.contains("draft") {
        "clear its prompt box"
    } else if w.contains("dialog") || w.contains("menu") {
        "close its dialog"
    } else {
        return None;
    })
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
        .map(|t| {
            let result = str_of(t, "result");
            Sent { label: label_of(t), held: result == "held", result, window_id: str_of(t, "window_id"), why: held_why(&str_of(t, "detail")) }
        })
        .filter(|s| !extra(&mode, &s.result))
        .collect();
    let stamp = str_of(&v, "press");
    let press = v.get("press").and_then(Value::as_str).and_then(|p| p.split_once('-'));
    let started = press.and_then(|(ms, _)| ms.parse::<u64>().ok()).map(|ms| ms as f64 / 1000.0);
    let pid = press.and_then(|(_, pid)| pid.parse().ok());
    Some(Progress { mode, done, targets, press: stamp, started, pid, alive: false })
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
        .map(|t| Saved { window_id: str_of(t, "window_id"), label: label_of(t), held: str_of(t, "result") == "held", why: held_why(&str_of(t, "detail")) })
        .collect();
    let paused_at = v.get("paused_at").and_then(Value::as_str).and_then(parse_iso);
    let glow_off = !str_of(&v, "glow_dismissed_at").trim().is_empty();
    Some(Paused { paused_at, stamp: str_of(&v, "paused_at"), targets, glow_off })
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

/// The snapshot's [`Windows`]: the WORKING ones ([`window_working`]), every
/// window, by id, the server's start (its first client row's
/// `#{start_time}`) and where each window lives. From the RAW snapshot
/// (before the sidebar blanks its spaces' homes): the script reads the
/// options as tmux holds them.
/// Is a window still working, for the pause? `@agent_state` running, or
/// `@agent_workflow` set, or `@agent_cua` set - but the drive only while the
/// turn is on. `@agent_cua` stays set for the watcher's CUA_LIVE (60s) after
/// the last drive, so an agent that closed its browsers and ended its turn
/// held the banner on "pausing" for up to a minute (agent l, 2026-10-26).
/// An ended turn drives nothing itself; a subagent still driving is out, and
/// `@agent_workflow` says so. CuaNotch has the same rule
/// (agentPauseRowWorking).
pub fn window_working(state: &str, workflow: &str, cua: &str) -> bool {
    let turn_ended = matches!(state, "idle" | "done" | "paused" | "failed");
    state == "running" || !workflow.is_empty() || (!cua.is_empty() && !turn_ended)
}

pub fn windows(snap: &Snapshot) -> Windows {
    let rows = &snap.windows;
    let working = rows.iter().filter(|w| window_working(&w.state, &w.workflow, &w.cua)).map(|w| w.id.clone()).collect();
    let exists = rows.iter().map(|w| w.id.clone()).collect();
    let server_start = snap.clients.first().and_then(|c| c.status.server_start.trim().parse::<f64>().ok()).filter(|&s| s > 0.0);
    // A hidden session (the stash, `agents`) letters nothing: a parked
    // window's stale `@agent_key` may name another window by now. (A
    // pending space's claude is the exception: listed, lettered.)
    let mut places: HashMap<String, (String, Option<char>)> = HashMap::new();
    let seen = |w: &&crate::tmux::Window| !w.space_hidden || w.is_pending();
    for w in rows.iter().filter(seen).chain(rows.iter().filter(|w| !seen(w))) {
        let letter = seen(&w).then(|| crate::menu::letters::parse_key(&w.key)).flatten();
        places.entry(w.id.clone()).or_insert((w.session.clone(), letter));
    }
    Windows { working, exists, server_start, places }
}

/// A held target as the banner lists it: its label, window and reason,
/// with where the window lives.
fn held_of(label: &str, window_id: &str, why: &str, win: &Windows) -> Held {
    let (session, letter) = win.places.get(window_id).cloned().unwrap_or_default();
    Held { label: label.to_string(), window_id: window_id.to_string(), why: why.to_string(), session, letter }
}

/// The held windows the [`Reader`] has seen stopped, for one pause or press
/// (`key`: `paused_at`, `press`): `paused.json` says `held` until the
/// resume, but once the agent stopped its PAUSE was delivered.
#[derive(Debug, Default)]
struct Delivered {
    key: String,
    windows: HashSet<String>,
}

impl Delivered {
    /// For the pause or press `key` (another one starts afresh): note every
    /// held target whose window exists and does not work (a window missing
    /// from the snapshot proves nothing: no snapshot yet, or it is gone and
    /// counts as stopped anyway), then clear `held` on every one noted.
    fn settle<'a>(&mut self, key: &str, rows: impl Iterator<Item = (&'a str, &'a mut bool)>, win: &Windows) {
        if self.key != key {
            self.key = key.to_string();
            self.windows.clear();
        }
        for (id, held) in rows {
            if *held && win.exists.contains(id) && !win.working.contains(id) {
                self.windows.insert(id.to_string());
            }
            *held &= !self.windows.contains(id);
        }
    }
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
    // A window still works: it exists and works by the script's rule.
    let works = |id: &str| !id.is_empty() && win.exists.contains(id) && win.working.contains(id);
    if let Some(p) = progress.filter(|p| in_flight(p, clock)) {
        let labels: Vec<String> = p.targets.iter().map(|t| t.label.clone()).collect();
        let n = p.targets.len();
        if p.mode == "resume" {
            return Some(Phase::Resuming { n, labels });
        }
        if p.mode == "pause" {
            let done = p.targets.iter().filter(|t| t.result != "sending").count();
            let held = p.targets.iter().filter(|t| t.held && works(&t.window_id)).map(|t| held_of(&t.label, &t.window_id, &t.why, win)).collect();
            return Some(Phase::Sending { done, n, labels, held });
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
    // One whose PAUSE is held works on until tmux-mcp can deliver it:
    // listed apart.
    let busy = |t: &&Saved| !fresh && works(&t.window_id);
    let working: Vec<String> = st.targets.iter().filter(busy).filter(|t| !t.held).map(|t| t.label.clone()).collect();
    let held: Vec<Held> = st.targets.iter().filter(busy).filter(|t| t.held).map(|t| held_of(&t.label, &t.window_id, &t.why, win)).collect();
    if !working.is_empty() || !held.is_empty() {
        let n = st.targets.len();
        return Some(Phase::Stopping { stopped: n - working.len() - held.len(), n, working, held });
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
    /// The held targets seen stopped, of the press and of the saved pause.
    delivered_press: Delivered,
    delivered: Delivered,
}

impl Reader {
    pub fn new(dir: PathBuf) -> Reader {
        Reader {
            progress: Cached::new(dir.join("progress.json")),
            paused: Cached::new(dir.join("paused.json")),
            wake: waketime,
            boot_fn: boottime,
            boot: None,
            delivered_press: Delivered::default(),
            delivered: Delivered::default(),
        }
    }

    /// [`state_dir`]'s reader.
    pub fn home() -> Reader {
        Reader::new(state_dir())
    }

    /// The phase now: the two files (re-parsed only when changed), the
    /// press's liveness, the held targets already seen stopped
    /// ([`Delivered`]), the clocks (the wake only while targets are saved
    /// and no press is in flight; the boot once, when there is anything to
    /// compare it with).
    pub fn phase(&mut self, win: &Windows) -> Option<Phase> {
        let progress = self.progress.get(parse_progress).map(|mut p| {
            // Only a press not done needs its process checked.
            p.alive = !p.done && p.pid.is_none_or(super::term::alive);
            let rows = p.targets.iter_mut().map(|t| (t.window_id.as_str(), &mut t.held));
            self.delivered_press.settle(&p.press, rows, win);
            p
        });
        let paused = self.paused.get(parse_paused).filter(|p| !p.targets.is_empty()).map(|mut p| {
            let rows = p.targets.iter_mut().map(|t| (t.window_id.as_str(), &mut t.held));
            self.delivered.settle(&p.stamp, rows, win);
            p
        });
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

    /// The saved set as last read ([`Reader::phase`]) has its glow
    /// dismissed ([`Paused::glow_off`]).
    pub fn glow_off(&self) -> bool {
        self.paused.value.as_ref().is_some_and(|p| p.glow_off)
    }

    /// Which pause the saved set as last read is: its `paused_at` as
    /// written ([`Paused::stamp`]); None with no saved set. (The banner's
    /// void chip is armed on one pause only.)
    pub fn stamp(&self) -> Option<String> {
        self.paused.value.as_ref().map(|p| p.stamp.clone())
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
        Windows { working: set(working), exists: set(exists), ..Windows::default() }
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

    /// A drive's flag outlives the turn by CUA_LIVE: once the turn ended it
    /// is not work (agent l, 2026-10-26). A subagent out or a turn on still is.
    #[test]
    fn a_drive_after_the_turn_is_not_work() {
        assert!(!window_working("paused", "", "1"));
        assert!(!window_working("done", "", "1"));
        assert!(!window_working("idle", "", "1"));
        assert!(window_working("paused", "1", "1"));
        assert!(window_working("running", "", ""));
        assert!(window_working("needs-input", "", "1"));
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
            Some(Phase::Sending { done: 2, n: 3, labels: vec!["A0".into(), "A1".into(), "A2".into()], held: vec![] })
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
        assert_eq!(phase(Some(&p), None, clock(None), &wins(&[], &[])), Some(Phase::Sending { done: 1, n: 2, labels: vec!["A0".into(), "A1".into()], held: vec![] }));
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
            Some(Phase::Stopping { stopped: 3, n: 4, working: vec!["Two".into()], held: vec![] })
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
        let stopping = Some(Phase::Stopping { stopped: 1, n: 2, working: vec!["Two".into()], held: vec![] });
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
        assert_eq!(phase(None, Some(&st), clock(None), &ex), Some(Phase::Stopping { stopped: 0, n: 1, working: vec!["One".into()], held: vec![] }));
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

    /// `agent-pause.py dismiss` only turns the notch's ring off
    /// (`glow_dismissed_at` in paused.json): the banner stays, the phase
    /// exactly as without it, until the resume or the void removes the
    /// targets (docs/agent-pause.md "Dismiss and void"). Only `glow_off`
    /// says so.
    #[test]
    fn a_dismissed_glow_keeps_the_banner() {
        let text = |extra: &str| format!(r#"{{"version":1,"paused_at":"{AT}"{extra},"targets":[{{"window_id":"@1","label":"One"}}],"failed":[]}}"#);
        let lit = parse_paused(&text("")).unwrap();
        let dark = parse_paused(&text(r#","glow_dismissed_at":"2026-10-19T15:00:00-07:00""#)).unwrap();
        assert!(!lit.glow_off && dark.glow_off);
        assert_eq!(Paused { glow_off: false, ..dark.clone() }, lit);
        let (idle, busy) = (wins(&[], &["@1"]), wins(&["@1"], &["@1"]));
        for (c, w) in [(clock(None), &idle), (clock(Some(AT_S + 9.0)), &idle), (clock(None), &busy)] {
            let got = phase(None, Some(&dark), c, w);
            assert!(got.is_some(), "{c:?}");
            assert_eq!(got, phase(None, Some(&lit), c, w), "{c:?}");
        }
        // Empty or not a string: not dismissed.
        assert!(!parse_paused(&text(r#","glow_dismissed_at":"""#)).unwrap().glow_off);
        assert!(!parse_paused(&text(r#","glow_dismissed_at":null"#)).unwrap().glow_off);
        // Through the reader: the banner, with `glow off`; voided (the file
        // gone), nothing.
        let dir = std::env::temp_dir().join(format!("agent-ui-glow-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let mut r = Reader::new(dir.clone());
        r.wake = || None;
        r.boot_fn = || Some(AT_S - 86_400.0);
        std::fs::write(dir.join("paused.json"), text(r#","glow_dismissed_at":"2026-10-19T15:00:00-07:00""#)).unwrap();
        assert_eq!(r.phase(&idle), Some(Phase::Safe { labels: vec!["One".into()] }));
        assert!(r.glow_off());
        // Which pause it is (the void chip's arm is tied to it).
        assert_eq!(r.stamp(), Some(AT.to_string()));
        std::fs::remove_file(dir.join("paused.json")).unwrap();
        assert_eq!(r.phase(&idle), None);
        assert!(!r.glow_off() && r.stamp().is_none());
        let _ = std::fs::remove_dir_all(&dir);
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
        assert_eq!(p.targets, vec![Saved { window_id: "@4".into(), label: "main:2.1".into(), ..Saved::default() }, Saved { window_id: "".into(), label: "agent".into(), ..Saved::default() }]);
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

    /// Windows `exists`, of which `working` work, each placed in `main`
    /// with the letter `places` gives it.
    fn placed(working: &[&str], places: &[(&str, Option<char>)]) -> Windows {
        let ids: Vec<&str> = places.iter().map(|(w, _)| *w).collect();
        let places = places.iter().map(|(w, c)| (w.to_string(), ("main".to_string(), *c))).collect();
        Windows { places, ..wins(working, &ids) }
    }

    /// message_agent's reply line for a PAUSE held because of copy mode
    /// (tmux-mcp server.ts), as the script saves it in `detail`.
    const COPY: &str = "held for @p0a (main:3.1, pane %9): the pane is in copy mode. It will be delivered once the box has been empty for 3s (1 message waiting).";

    /// A held PAUSE: `"result": "held"` in paused.json (as the script
    /// writes it once the press is done) and in progress.json (while it is
    /// in flight), its reason from `detail`. A held agent whose window still
    /// works is listed apart, with its session and letter, never also in
    /// `working`; once its window stops it is just stopped (then safe).
    #[test]
    fn a_held_pause_is_listed_apart_until_its_agent_stops() {
        // paused.json as finish_pause writes it (`ok` targets keep their result).
        let st = parse_paused(&format!(
            r#"{{"version":1,"paused_at":"{AT}","targets":[
              {{"pane_id":"%9","window_id":"@9","location":"main:3.1","label":"P0a","kind":"claude","result":"held","detail":"{COPY}"}},
              {{"pane_id":"%4","window_id":"@4","location":"main:2.1","label":"Done one","kind":"claude","result":"sent","detail":"sent to %4"}},
              {{"pane_id":"%5","window_id":"@5","location":"main:4.1","label":"Busy","kind":"codex","result":"unknown","detail":""}}],"failed":[]}}"#
        ))
        .unwrap();
        assert_eq!(st.targets.iter().map(|t| t.held).collect::<Vec<_>>(), [true, false, false]);
        let p0a = Held { label: "P0a".into(), window_id: "@9".into(), why: "the pane is in copy mode".into(), session: "main".into(), letter: Some('k') };
        let w = placed(&["@9", "@5"], &[("@9", Some('k')), ("@4", Some('a')), ("@5", None)]);
        let got = phase(None, Some(&st), clock(None), &w);
        assert_eq!(got, Some(Phase::Stopping { stopped: 1, n: 3, working: vec!["Busy".into()], held: vec![p0a.clone()] }));
        assert_eq!(got.unwrap().held(), std::slice::from_ref(&p0a));
        // Only the held one still works: no one in `working`.
        let w = placed(&["@9"], &[("@9", Some('k')), ("@4", None), ("@5", None)]);
        assert_eq!(phase(None, Some(&st), clock(None), &w), Some(Phase::Stopping { stopped: 2, n: 3, working: vec![], held: vec![p0a.clone()] }));
        // tmux-mcp delivered it, the agent stopped: no longer held, safe.
        let w = placed(&[], &[("@9", Some('k')), ("@4", None), ("@5", None)]);
        let safe = phase(None, Some(&st), clock(None), &w).unwrap();
        assert_eq!(safe, Phase::Safe { labels: vec!["P0a".into(), "Done one".into(), "Busy".into()] });
        assert!(safe.held().is_empty());
        // Its window gone: stopped like any other.
        assert!(matches!(phase(None, Some(&st), clock(None), &placed(&["@5"], &[("@5", None)])), Some(Phase::Stopping { ref held, .. }) if held.is_empty()));
        // A wake since: ready, nothing held.
        assert_eq!(phase(None, Some(&st), clock(Some(AT_S + 9.0)), &w).unwrap().held(), []);
        // A window the snapshot cannot place: listed, with nowhere to jump.
        let w = wins(&["@9"], &["@9"]);
        assert_eq!(phase(None, Some(&st), clock(None), &w).unwrap().held(), [Held { session: "".into(), letter: None, ..p0a.clone() }]);

        // progress.json while the press is in flight (Progress.write's shape).
        let ms = ((NOW - 30.0) * 1000.0) as u64;
        let mut pr = parse_progress(&format!(
            r#"{{"press":"{ms}-4242","mode":"pause","done":false,"outcome":"","message":"","targets":[
              {{"pane_id":"%9","window_id":"@9","location":"main:3.1","label":"P0a","result":"held","detail":"{COPY}"}},
              {{"pane_id":"%4","window_id":"@4","location":"main:2.1","label":"Done one","result":"sending","detail":""}},
              {{"window_id":"@7","location":"main:5.1","label":"No pane","result":"unidentified"}}]}}"#
        ))
        .unwrap();
        pr.alive = true;
        assert_eq!(pr.targets[0], Sent { label: "P0a".into(), result: "held".into(), window_id: "@9".into(), held: true, why: "the pane is in copy mode".into() });
        let w = placed(&["@9", "@4"], &[("@9", Some('k')), ("@4", Some('a'))]);
        assert_eq!(
            phase(Some(&pr), None, clock(None), &w),
            Some(Phase::Sending { done: 1, n: 2, labels: vec!["P0a".into(), "Done one".into()], held: vec![p0a.clone()] })
        );
        // Stopped meanwhile: not held. A resume's held rows are no pause's.
        assert_eq!(phase(Some(&pr), None, clock(None), &placed(&["@4"], &[("@9", Some('k'))])).unwrap().held(), []);
        pr.mode = "resume".into();
        assert_eq!(phase(Some(&pr), None, clock(None), &placed(&["@9"], &[("@9", Some('k'))])).unwrap().held(), []);
    }

    /// The reason is tmux-mcp's `<why>` (its `readBox` / `deliverGuarded`
    /// wording, server.ts), mapped to what to do; one it does not know has
    /// no action (the banner shows it as it is).
    #[test]
    fn a_held_reason_says_what_to_do() {
        let reply = |why: &str| format!("held for @h (main:3.1, pane %9): {why}. It will be delivered once the box has been empty for 3s (1 message waiting).");
        for (why, want) in [
            ("the pane is in copy mode", Some("leave copy mode")),
            ("no prompt box on screen (a dialog or menu may be up)", Some("show its prompt")),
            ("the prompt box does not have the cursor (a dialog or menu may be up)", Some("close its dialog")),
            ("a permission or question prompt is up", Some("answer its prompt")),
            ("its prompt box has a half-written draft in it", Some("clear its prompt box")),
            ("earlier messages to it are still held", None),
            ("could not read the cursor", None),
            ("capture failed: Error: can't find pane %9", None),
        ] {
            assert_eq!(held_why(&reply(why)), why);
            assert_eq!(ask(why), want, "{why}");
        }
        // Not a hold, or no reply line: no reason. Control characters out.
        for d in ["", "sent to @h (main:1.1, pane %1)", "held for nothing"] {
            assert_eq!(held_why(d), "", "{d}");
        }
        assert!(!held_why("held for @h (x, pane %1): a\u{1b}[31mb\nc").chars().any(char::is_control));
    }

    /// `paused.json` says `held` until the resume. Once the sidebar has seen
    /// the held agent's window stopped (its PAUSE delivered), that pause
    /// never holds it again: woken while paused, it is just still working.
    /// A new pause (another `paused_at`) starts afresh; a window missing
    /// from the snapshot (none yet) proves nothing.
    #[test]
    fn a_held_agent_seen_stopped_is_delivered() {
        let dir = std::env::temp_dir().join(format!("agent-ui-held-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let mut r = Reader::new(dir.clone());
        r.wake = || None;
        r.boot_fn = || Some(AT_S - 86_400.0);
        let write = |at: &str| {
            let tmp = dir.join(".paused.json.tmp");
            let t = format!(r#"{{"window_id":"@9","label":"P0a","result":"held","detail":"{COPY}"}}"#);
            std::fs::write(&tmp, format!(r#"{{"paused_at":"{at}","targets":[{t}]}}"#)).unwrap();
            std::fs::rename(&tmp, dir.join("paused.json")).unwrap();
        };
        write(AT);
        let p0a = |p: Option<Phase>| p.is_some_and(|p| p.held().len() == 1 && p.held()[0].why == "the pane is in copy mode");
        let (busy, idle) = (placed(&["@9"], &[("@9", None)]), placed(&[], &[("@9", None)]));
        // No snapshot yet: nothing proves it stopped.
        assert!(matches!(r.phase(&Windows::default()), Some(Phase::Safe { .. })));
        assert!(p0a(r.phase(&busy)));
        // Stopped (delivered): safe. Working again later: no longer held.
        assert!(matches!(r.phase(&idle), Some(Phase::Safe { .. })));
        assert_eq!(r.phase(&busy), Some(Phase::Stopping { stopped: 0, n: 1, working: vec!["P0a".into()], held: vec![] }));
        // A new pause: held again.
        write("2026-10-19T15:00:00-07:00");
        assert!(p0a(r.phase(&busy)));
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Where a held row jumps: each window's session (a shown one before a
    /// hidden one it is linked into) and its letter, never from a hidden
    /// session (a parked window's stale key).
    #[test]
    fn places_come_from_the_snapshot() {
        let snap = Snapshot::parse(
            &[
                crate::tmux::tests::row("main", 1, "@1", &[("state", "running"), ("key", "k")]),
                crate::tmux::tests::row("stash", 2, "@2", &[("state", "running"), ("key", "s"), ("space_hidden", "1")]),
                crate::tmux::tests::row("agents", 1, "@3", &[("state", "running"), ("key", "d"), ("space_hidden", "1")]),
                crate::tmux::tests::row("work", 3, "@3", &[("state", "running"), ("key", "d")]),
                crate::tmux::tests::row("main", 4, "@4", &[("state", "running"), ("key", "junk")]),
            ]
            .join("\n"),
        );
        let p = windows(&snap).places;
        assert_eq!(p["@1"], ("main".to_string(), Some('k')));
        assert_eq!(p["@2"], ("stash".to_string(), None));
        assert_eq!(p["@3"], ("work".to_string(), Some('d')));
        assert_eq!(p["@4"], ("main".to_string(), None));
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
