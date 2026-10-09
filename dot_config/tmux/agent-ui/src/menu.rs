//! `agent-ui menu`: the Option-W popup (and prefix q), replacing
//! `agent-roster.py --client <tty>`.
//!
//! ```text
//! agent-ui menu --client <tty> [--tab active|all|needs|working|idle|parked]
//! agent-ui menu --once WxH [--client <tty>] [--tab x] [--select <label>] [--plain]
//! ```
//!
//! It opens on the **Active** tab: every agent working or needing you, in a
//! fixed space/index order that a state change never reshuffles (see
//! [`rows`]). Tab / Shift-Tab cycle Active, All, Needs you, Working, Idle,
//! Parked.
//!
//! The pieces: [`keys`] (raw bytes → keys, the Python's `parse_keys`),
//! [`rows`] (tabs, search, the list), [`state`] (the `Roster` state machine
//! behind an [`Effects`] trait) and [`render`] (one frame into a ratatui
//! buffer, with its click map). This file is the I/O around them.
//!
//! ## The loop
//!
//! - **First frame.** One tmux call (`Core`'s snapshot), git branches only
//!   if already cached (`branch_wait` 0 for that frame), no event log, no
//!   `git status` ever (the menu does not show it), no peek: then draw.
//!   `AGENT_UI_MENU_TRACE=<file>` appends `first_frame_us N` to that file.
//! - **Cadence.** A snapshot every [`REFRESH`] (0.5 s, so the pulse steps in
//!   phase with `@agent_blink`); the terminal is written only when the frame
//!   differs from the one on screen.
//! - **Input** comes from a reader thread over a channel. A read that ends
//!   inside an escape sequence waits [`keys::ESC_WAIT`] for the rest (the
//!   Python's main loop). Input that arrived while refreshing is handled
//!   against the frame ON SCREEN before the next draw (labels, clicks).
//! - **Peeks** (the preview's live tail) run on a worker thread: requested
//!   [`PEEK_DEBOUNCE`] after the selection settles, then every
//!   [`PEEK_EVERY`]; the worker only ever runs the newest request, so the
//!   UI never waits on `capture-pane`.
//! - **Mouse:** SGR reports with DECSET 1000 + 1006 only (clicks and the
//!   wheel; never 1003 all-motion).
//!
//! `--once` renders one frame of the live server to stdout (truecolour SGR,
//! or `--plain`) and exits. It is read-only: one snapshot, the branch reads,
//! and one capture-pane of the selected row for the preview. No action runs.

pub mod keys;
pub mod render;
pub mod rows;
pub mod state;

use self::keys::{KeyReader, ESC_WAIT};
use self::render::{dump, render};
use self::rows::{Data, Tab};
use self::state::{Effects, Menu, PeekData, PeekWant};
use crate::actions::{self, Actions};
use crate::tmux::{Snapshot, Window};
use crate::{text, Core, Tmux};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use std::collections::VecDeque;
use std::io::{self, BufWriter, Read, Write};
use std::process::ExitCode;
use std::sync::mpsc::{self, RecvTimeoutError, Sender};
use std::time::{Duration, Instant};

const USAGE: &str = "usage: agent-ui menu --client <tty> [--tab active|all|needs|working|idle|parked]
       agent-ui menu --once WxH [--client <tty>] [--tab x] [--select <label>] [--plain]";

/// A snapshot every half second (lib.rs: the steadier pulse).
pub const REFRESH: Duration = Duration::from_millis(500);
/// The preview's capture is refreshed this often...
pub const PEEK_EVERY: Duration = Duration::from_secs(1);
/// ...and requested this long after the selection last moved.
pub const PEEK_DEBOUNCE: Duration = Duration::from_millis(60);
/// A split window's agent pane (one `list-panes` + `ps -ax`) is re-resolved
/// at most this often, or sooner when its pane count changes.
pub const PANE_TTL: Duration = Duration::from_secs(10);

/// The command line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Args {
    pub client: Option<String>,
    pub tab: Tab,
    pub once: Option<(u16, u16)>,
    pub select: Option<String>,
    pub plain: bool,
}

pub fn parse_args(args: &[String]) -> Result<Args, String> {
    let mut a = Args { client: None, tab: Tab::Active, once: None, select: None, plain: false };
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        let mut val = || it.next().cloned().ok_or_else(|| format!("{arg} needs a value"));
        match arg.as_str() {
            "--client" => a.client = Some(val()?),
            "--tab" => {
                let v = val()?;
                a.tab = Tab::parse(&v).ok_or_else(|| format!("unknown tab {v:?}"))?;
            }
            "--once" => {
                let v = val()?;
                let (w, h) = v.split_once('x').ok_or_else(|| format!("--once wants WxH, not {v:?}"))?;
                let n = |s: &str| s.parse::<u16>().map_err(|_| format!("bad size {v:?}"));
                a.once = Some((n(w)?, n(h)?));
            }
            "--select" => a.select = Some(val()?),
            "--plain" => a.plain = true,
            _ => return Err(format!("unknown argument {arg:?}")),
        }
    }
    Ok(a)
}

pub fn run(tmux: Tmux, args: &[String]) -> ExitCode {
    let a = match parse_args(args) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("agent-ui menu: {e}\n{USAGE}");
            return ExitCode::from(2);
        }
    };
    if let Some((w, h)) = a.once {
        return once(tmux, &a, w, h);
    }
    let Some(client) = a.client.clone() else {
        eprintln!("{USAGE}");
        return ExitCode::from(2);
    };
    match interactive(tmux, client, a.tab) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("agent-ui menu: {e}");
            ExitCode::from(1)
        }
    }
}

/// The read side: one snapshot → the menu's [`Data`].
pub struct Source {
    pub core: Core,
    pub client: Option<String>,
}

impl Source {
    pub fn new(tmux: Tmux, client: Option<String>) -> Source {
        Source { core: Core::new(tmux), client }
    }

    pub fn load(&mut self) -> Data {
        self.core.snapshot = self.core.tmux.snapshot();
        Data::build(&self.core.snapshot, self.client.as_deref(), text::now(), &self.core.collator,
            Some(&mut self.core.git), actions::watcher_age())
    }
}

/// The real effects: the core's [`Actions`] against the latest snapshot
/// (each action re-checks its window there first).
pub struct Live {
    pub actions: Actions,
    pub client: String,
    pub snap: Snapshot,
}

impl Effects for Live {
    fn go(&mut self, win: &str, session: &str) -> Result<(), String> {
        self.actions.go(&self.snap, &self.client, win, session)
    }
    fn next(&mut self) -> Result<(), String> {
        self.actions.next(&self.client)
    }
    fn back(&mut self) -> Result<(), String> {
        self.actions.back(&self.client)
    }
    fn park(&mut self, win: &str, session: &str) -> Result<String, String> {
        self.actions.park(&self.snap, win, session)
    }
    fn close(&mut self, win: &str, session: &str) -> Result<String, String> {
        self.actions.close(&self.snap, win, session)
    }
    fn restart_watcher(&mut self) -> String {
        self.actions.restart_watcher()
    }
}

/// `--once`: one frame of the live server, read-only.
fn once(tmux: Tmux, a: &Args, w: u16, h: u16) -> ExitCode {
    let mut src = Source::new(tmux.clone(), a.client.clone());
    src.core.git.branch_wait = Duration::from_millis(500); // a one-shot may wait for its branches
    let mut menu = Menu::new(src.load(), a.tab);
    if let Some(label) = &a.select {
        let pos = menu.labels.iter().find(|(_, l)| *l == label).map(|(p, _)| *p);
        match pos.and_then(|p| menu.rows[p].key()) {
            Some(k) => menu.sel = Some(k),
            None => {
                eprintln!("agent-ui menu: no row {label}");
                return ExitCode::from(2);
            }
        }
    }
    let mut buf = Buffer::empty(Rect::new(0, 0, w, h));
    render(&mut menu, &mut buf, text::now());
    if let Some(PeekWant { target, n }) = menu.peek_want() {
        if let Some(win) = src.core.snapshot.row_in(&target.win, &target.session) {
            let lines = Actions::new(tmux).peek(win, n);
            menu.peek_result(target.win, target.session, PeekData { lines, n });
            render(&mut menu, &mut buf, text::now());
        }
    }
    let mut out = io::stdout().lock();
    for l in dump(&buf, !a.plain) {
        let _ = writeln!(out, "{l}");
    }
    ExitCode::SUCCESS
}

/// Raw mode, the alternate screen, SGR mouse (1000 + 1006 only), no
/// cursor; all undone on drop, panics included.
struct TermGuard;

impl TermGuard {
    fn enter() -> io::Result<TermGuard> {
        crossterm::terminal::enable_raw_mode()?;
        let g = TermGuard;
        let mut o = io::stdout().lock();
        o.write_all(b"\x1b[?1049h\x1b[?25l\x1b[2J\x1b[?1000h\x1b[?1006h")?;
        o.flush()?;
        Ok(g)
    }
}

impl Drop for TermGuard {
    fn drop(&mut self) {
        let mut o = io::stdout().lock();
        let _ = o.write_all(b"\x1b[?1006l\x1b[?1000l\x1b[0m\x1b[?25h\x1b[?1049l");
        let _ = o.flush();
        let _ = crossterm::terminal::disable_raw_mode();
    }
}

enum Msg {
    Input(Vec<u8>),
    /// stdin closed: the popup went away.
    Eof,
    Peek(String, String, PeekData),
}

/// stdin → the channel, one read at a time.
fn spawn_reader(tx: Sender<Msg>) {
    std::thread::spawn(move || {
        let mut stdin = io::stdin().lock();
        let mut buf = [0u8; 4096];
        loop {
            match stdin.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    if tx.send(Msg::Input(buf[..n].to_vec())).is_err() {
                        return;
                    }
                }
                Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                Err(_) => break,
            }
        }
        let _ = tx.send(Msg::Eof);
    });
}

/// Which pane the preview captures, per window: [`Actions::peek`]'s choice
/// (the agent's pane of a split window, matched by tty through `ps`; the
/// window's active pane when that can't be told), remembered so the 1 s
/// refresh is one `capture-pane`, not `list-panes` + `ps -ax` each time.
#[derive(Default)]
pub struct PaneCache {
    /// window id → (its pane count then, the pane to capture, when).
    panes: std::collections::HashMap<String, (u32, String, Instant)>,
}

impl PaneCache {
    /// The capture target for `w`, resolving it (via `resolve`) only when
    /// the window's pane count changed or the entry is older than PANE_TTL.
    pub fn target(&mut self, w: &Window, resolve: impl FnOnce(&str) -> Option<String>) -> String {
        if w.panes <= 1 {
            self.panes.remove(&w.id);
            return w.id.clone();
        }
        if let Some((n, p, at)) = self.panes.get(&w.id) {
            if *n == w.panes && at.elapsed() < PANE_TTL {
                return p.clone();
            }
        }
        // Can't tell which pane is the agent: the window's active pane, as the core does.
        let p = resolve(&w.id).unwrap_or_else(|| w.id.clone());
        self.panes.insert(w.id.clone(), (w.panes, p.clone(), Instant::now()));
        p
    }

    /// A capture failed: resolve this window again next time.
    pub fn forget(&mut self, win: &str) {
        self.panes.remove(win);
    }
}

/// [`Actions::peek`] with the pane taken from `cache`.
fn capture(actions: &Actions, cache: &mut PaneCache, w: &Window, n: usize) -> Option<Vec<crate::ansi::StyledLine>> {
    let target = cache.target(w, |win| actions.agent_pane(win).ok());
    let start = format!("-{n}");
    match actions.tmux.run(&["capture-pane", "-p", "-e", "-J", "-S", &start, "-t", &target]) {
        Ok(o) if o.ok() => Some(actions::peek_from_capture(&o.stdout, n)),
        _ => {
            cache.forget(&w.id);
            None
        }
    }
}

/// The capture worker: runs only the newest request it has.
fn spawn_peeker(actions: Actions, tx: Sender<Msg>) -> Sender<(Window, String, usize)> {
    let (ptx, prx) = mpsc::channel::<(Window, String, usize)>();
    std::thread::spawn(move || {
        let mut cache = PaneCache::default();
        while let Ok(mut req) = prx.recv() {
            while let Ok(r) = prx.try_recv() {
                req = r;
            }
            let (w, session, n) = req;
            let lines = capture(&actions, &mut cache, &w, n);
            if tx.send(Msg::Peek(w.id.clone(), session, PeekData { lines, n })).is_err() {
                return;
            }
        }
    });
    ptx
}

type Term = ratatui::Terminal<ratatui::backend::CrosstermBackend<BufWriter<io::Stdout>>>;

/// Render; write to the terminal only when the frame changed.
fn draw(term: &mut Term, menu: &mut Menu, last: &mut Option<Buffer>) -> io::Result<()> {
    let (w, h) = crossterm::terminal::size()?;
    let mut buf = Buffer::empty(Rect::new(0, 0, w, h));
    render(menu, &mut buf, text::now());
    if last.as_ref() == Some(&buf) {
        menu.frame_shown(); // this very frame is already on screen
        return Ok(());
    }
    term.draw(|f| {
        let b = f.buffer_mut();
        if b.area == buf.area {
            b.content.clone_from(&buf.content);
        } else {
            render(menu, b, text::now()); // resized between the two size reads
        }
    })?;
    *last = Some(buf);
    menu.frame_shown();
    Ok(())
}

/// A refresh's snapshot is worth showing: not a blank one (a tmux timeout,
/// a busy server) right after one with windows in it.
pub fn usable(new: &Snapshot, last: &Snapshot) -> bool {
    !new.windows.is_empty() || last.windows.is_empty()
}

fn interactive(tmux: Tmux, client: String, tab: Tab) -> io::Result<()> {
    let t0 = Instant::now();
    let mut src = Source::new(tmux.clone(), Some(client.clone()));
    src.core.git.branch_wait = Duration::ZERO; // the first frame never waits on git
    let data = src.load();
    src.core.git.branch_wait = crate::git::BRANCH_WAIT;
    let actions = Actions::new(tmux);
    let mut live = Live { actions: actions.clone(), client, snap: src.core.snapshot.clone() };
    let mut menu = Menu::new(data, tab);

    let _guard = TermGuard::enter()?;
    let mut term = ratatui::Terminal::new(ratatui::backend::CrosstermBackend::new(BufWriter::with_capacity(
        1 << 16,
        io::stdout(),
    )))?;
    let mut last: Option<Buffer> = None;
    draw(&mut term, &mut menu, &mut last)?;
    if let Ok(p) = std::env::var("AGENT_UI_MENU_TRACE") {
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(p) {
            let _ = writeln!(f, "first_frame_us {}", t0.elapsed().as_micros());
        }
    }

    let (tx, rx) = mpsc::channel::<Msg>();
    spawn_reader(tx.clone());
    let peeker = spawn_peeker(actions, tx);
    let mut reader = KeyReader::new();
    let mut queue: VecDeque<Msg> = VecDeque::new();
    let mut next_refresh = Instant::now() + REFRESH;
    let mut want: Option<PeekWant> = None;
    let mut peek_due: Option<Instant> = None;
    let mut first_peek = true;

    loop {
        let now = Instant::now();
        let mut deadline = next_refresh;
        if let Some(d) = peek_due {
            deadline = deadline.min(d);
        }
        let left = menu.msg_until - text::now();
        if left > 0.0 {
            deadline = deadline.min(now + Duration::from_secs_f64(left));
        }
        let msg = match queue.pop_front() {
            Some(m) => Some(m),
            None => match rx.recv_timeout(deadline.saturating_duration_since(now)) {
                Ok(m) => Some(m),
                Err(RecvTimeoutError::Timeout) => None,
                Err(RecvTimeoutError::Disconnected) => return Ok(()),
            },
        };
        match msg {
            Some(Msg::Input(bytes)) => {
                let mut keys = reader.feed(&bytes);
                // A read that ended mid-sequence (or on a bare ESC) waits
                // ESC_WAIT for the rest; only silence makes a lone ESC "esc".
                while reader.pending() {
                    match rx.recv_timeout(ESC_WAIT) {
                        Ok(Msg::Input(b)) => keys.extend(reader.feed(&b)),
                        Ok(other) => {
                            let eof = matches!(other, Msg::Eof);
                            queue.push_back(other);
                            if eof {
                                keys.extend(reader.flush());
                            }
                        }
                        Err(_) => keys.extend(reader.flush()),
                    }
                }
                if menu.handle(&keys, &mut live) {
                    return Ok(());
                }
            }
            Some(Msg::Eof) => return Ok(()),
            Some(Msg::Peek(win, session, data)) => menu.peek_result(win, session, data),
            None => {}
        }
        if Instant::now() >= next_refresh {
            let d = src.load();
            if usable(&src.core.snapshot, &live.snap) {
                live.snap = src.core.snapshot.clone();
                menu.set_data(d);
            } else {
                src.core.snapshot = live.snap.clone(); // keep the last good frame
            }
            next_refresh = Instant::now() + REFRESH;
        }
        // Input that arrived meanwhile was typed or clicked against the frame
        // still on screen: handle it before drawing a new one.
        while let Ok(m) = rx.try_recv() {
            queue.push_back(m);
        }
        if queue.iter().any(|m| matches!(m, Msg::Input(_) | Msg::Eof)) {
            continue;
        }
        draw(&mut term, &mut menu, &mut last)?;

        // What the frame on screen wants captured.
        let w = menu.peek_want();
        let now = Instant::now();
        if w != want {
            peek_due = w.as_ref().map(|_| if first_peek { now } else { now + PEEK_DEBOUNCE });
            first_peek = false;
            want = w;
        }
        if let (Some(due), Some(PeekWant { target, n })) = (peek_due, &want) {
            if now >= due {
                if let Some(win) = live.snap.row_in(&target.win, &target.session) {
                    let _ = peeker.send((win.clone(), target.session.clone(), *n));
                }
                peek_due = Some(now + PEEK_EVERY);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(v: &[&str]) -> Vec<String> {
        v.iter().map(|x| x.to_string()).collect()
    }

    #[test]
    fn args() {
        let a = parse_args(&s(&["--client", "/dev/ttys004", "--tab", "parked"])).unwrap();
        assert_eq!((a.client.as_deref(), a.tab, a.once), (Some("/dev/ttys004"), Tab::Parked, None));
        let a = parse_args(&s(&["--once", "110x34", "--select", "02", "--plain"])).unwrap();
        assert_eq!((a.once, a.select.as_deref(), a.plain), (Some((110, 34)), Some("02"), true));
        assert_eq!(a.tab, Tab::Active); // the default
        assert_eq!(parse_args(&s(&["--tab", "active"])).unwrap().tab, Tab::Active);
        assert_eq!(parse_args(&s(&["--tab", "all"])).unwrap().tab, Tab::All);
        assert!(parse_args(&s(&["--tab", "nope"])).is_err());
        assert!(parse_args(&s(&["--once", "110"])).is_err());
        assert!(parse_args(&s(&["--client"])).is_err());
        assert!(parse_args(&s(&["--bogus"])).is_err());
    }

    #[test]
    fn blank_snapshots_after_good_ones_are_skipped() {
        let good = Snapshot::parse(&crate::tmux::tests::row("main", 1, "@1", &[("state", "idle")]));
        let blank = Snapshot::default();
        assert!(usable(&good, &blank) && usable(&good, &good) && usable(&blank, &blank));
        assert!(!usable(&blank, &good));
    }

    #[test]
    fn pane_cache_resolves_rarely() {
        let mut w = Window { id: "@5".into(), panes: 2, ..Window::default() };
        let mut c = PaneCache::default();
        let asked = std::cell::Cell::new(0);
        let get = |c: &mut PaneCache, w: &Window, ans: Option<&str>| {
            c.target(w, |_| {
                asked.set(asked.get() + 1);
                ans.map(String::from)
            })
        };
        assert_eq!(get(&mut c, &w, Some("%9")), "%9");
        assert_eq!(get(&mut c, &w, Some("%1")), "%9"); // cached: no second ps
        w.panes = 3; // a pane came or went: resolved again
        assert_eq!(get(&mut c, &w, None), "@5"); // can't tell: the window's active pane
        c.forget("@5");
        assert_eq!(get(&mut c, &w, Some("%2")), "%2");
        w.panes = 1; // one pane: the window itself, never asked
        assert_eq!(get(&mut c, &w, Some("%7")), "@5");
        assert_eq!(asked.get(), 3);
    }
}
