//! `agent-ui sessions`: the prefix+a session picker, replacing
//! `scripts/mru-session-switch.sh` (bash + fzf) and its
//! `preview_session.sh`.
//!
//! ```text
//! agent-ui sessions --client <tty>
//! agent-ui sessions --once WxH [--client <tty>] [--query q] [--ansi]
//! ```
//!
//! Sessions in MRU order with their agent rollup; typing filters on the
//! NAME only; ⏎ switches the client, or offers to create a session named by
//! the query. The pieces: [`rows`] (the list and the matcher), [`state`]
//! (the [`Picker`]), [`render`], [`keys`]. This file is the I/O.
//!
//! ## Fast, and no key lost
//!
//! The binding opens it with no shell (`display-popup … agent-ui sessions`
//! in argv form). Then, in this order:
//!
//! 1. **Raw mode first**, before anything else, with `TCSANOW` (crossterm's
//!    `enable_raw_mode` uses `tcsetattr(…, TCSANOW, …)` on both of its
//!    backends; `TCSAFLUSH` would discard typed-ahead keys). Keys typed
//!    between the popup opening and this point sat in the tty's cooked
//!    line buffer; going raw keeps them (the kernel moves the partial line
//!    over). Their count is read right then (`FIONREAD`): that many bytes
//!    went through ICRNL, so their LF is an Enter ([`KeyReader::feed_cooked`]).
//! 2. **The reader thread** starts at once and queues every byte.
//! 3. **One tmux call**: the core snapshot (`list-windows -a` + `list-clients`
//!    in one round trip). Every session has a window, so its rows carry
//!    `session_last_attached` and the agent options; the client row names
//!    the client's session.
//! 4. Keys already queued are applied to the loaded list (an Enter typed
//!    ahead acts on the filtered rows); then the first frame is drawn.
//!    `AGENT_UI_SESSIONS_TRACE=<file>` appends `first_frame_us N`, and
//!    `done_us N <outcome>` when it ends (plus `cooked_bytes N` when keys
//!    were typed before raw mode).
//!
//! The preview (the selected session's active pane, its bottom rows, like
//! `preview_session.sh`) is captured on a worker thread, [`PREVIEW_DEBOUNCE`]
//! after the selection settles and every [`PREVIEW_EVERY`] after that; the
//! worker runs only the newest request, so input never waits on it. The
//! rows are refreshed every [`REFRESH`] (the selection stays on its name).
//!
//! `--once` renders one frame of the live server to stdout (plain text, or
//! `--ansi` for truecolour) with the selected row's preview, read-only.

pub mod keys;
pub mod render;
pub mod rows;
pub mod state;

use self::keys::{KeyReader, ESC_WAIT};
use self::render::{render, Geo};
use self::state::{Outcome, Picker};
use crate::ansi::{self, StyledLine};
use crate::collate::Collator;
use crate::tmux::Tmux;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use std::collections::HashMap;
use std::io::{self, BufWriter, Read, Write};
use std::process::ExitCode;
use std::sync::mpsc::{self, RecvTimeoutError, Sender, TryRecvError};
use std::time::{Duration, Instant};

const USAGE: &str = "usage: agent-ui sessions --client <tty>
       agent-ui sessions --once WxH [--client <tty>] [--query q] [--ansi]";

/// The rows are re-read this often while the picker is open.
pub const REFRESH: Duration = Duration::from_secs(1);
/// The preview is captured this long after the selection last moved...
pub const PREVIEW_DEBOUNCE: Duration = Duration::from_millis(40);
/// ...and again this often while it stays.
pub const PREVIEW_EVERY: Duration = Duration::from_secs(1);
/// The trace file's env var.
pub const TRACE_ENV: &str = "AGENT_UI_SESSIONS_TRACE";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Args {
    pub client: Option<String>,
    pub once: Option<(u16, u16)>,
    pub query: String,
    pub ansi: bool,
}

pub fn parse_args(args: &[String]) -> Result<Args, String> {
    let mut a = Args { client: None, once: None, query: String::new(), ansi: false };
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        let mut val = || it.next().cloned().ok_or_else(|| format!("{arg} needs a value"));
        match arg.as_str() {
            "--client" => a.client = Some(val()?),
            "--query" => a.query = val()?,
            "--once" => {
                let v = val()?;
                let (w, h) = v.split_once('x').ok_or_else(|| format!("--once wants WxH, not {v:?}"))?;
                let n = |s: &str| s.parse::<u16>().map_err(|_| format!("bad size {v:?}"));
                a.once = Some((n(w)?, n(h)?));
            }
            "--ansi" => a.ansi = true,
            _ => return Err(format!("unknown argument {arg:?}")),
        }
    }
    Ok(a)
}

pub fn run(tmux: Tmux, args: &[String]) -> ExitCode {
    let a = match parse_args(args) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("agent-ui sessions: {e}\n{USAGE}");
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
    match interactive(tmux, client) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("agent-ui sessions: {e}");
            ExitCode::from(1)
        }
    }
}

/// One snapshot → the picker's rows.
pub fn load(tmux: &Tmux, client: Option<&str>, coll: &Collator) -> rows::Sessions {
    rows::build(&tmux.snapshot(), client, coll)
}

/// `preview_session.sh`: the bottom `n` rows of the session's active pane
/// (`capture-pane -ep -t =<name>:` | `tail -n`), with their colours. None
/// when it can't be captured (the session went away).
pub fn capture(tmux: &Tmux, name: &str, n: usize) -> Option<Vec<StyledLine>> {
    let o = tmux.run(&["capture-pane", "-e", "-p", "-t", &format!("={name}:")]).ok()?;
    if !o.ok() {
        return None;
    }
    let lines: Vec<&str> = o.stdout.lines().collect();
    let k = lines.len().saturating_sub(n);
    Some(lines[k..].iter().map(|l| ansi::parse_line(l)).collect())
}

/// `--once`: one frame of the live server, read-only (one snapshot and one
/// capture-pane).
fn once(tmux: Tmux, a: &Args, w: u16, h: u16) -> ExitCode {
    let coll = Collator::from_env();
    let mut p = Picker::with(load(&tmux, a.client.as_deref(), &coll));
    p.set_query(&a.query);
    let prev = match (p.selected(), Geo::preview_size(w, h)) {
        (Some(r), Some((_, ph))) => capture(&tmux, &r.name, ph as usize),
        _ => None,
    };
    let mut buf = Buffer::empty(Rect::new(0, 0, w, h));
    render(&mut p, &mut buf, prev.as_deref());
    let mut out = io::stdout().lock();
    for l in crate::menu::render::dump(&buf, a.ansi) {
        let _ = writeln!(out, "{l}");
    }
    ExitCode::SUCCESS
}

/// Raw mode (entered first, see the module docs), then the alternate
/// screen once there is something to draw; all undone on drop.
struct TermGuard {
    screen: bool,
}

impl TermGuard {
    fn raw() -> io::Result<TermGuard> {
        crossterm::terminal::enable_raw_mode()?;
        Ok(TermGuard { screen: false })
    }

    fn screen(&mut self) -> io::Result<()> {
        if !self.screen {
            let mut o = io::stdout().lock();
            o.write_all(b"\x1b[?1049h\x1b[?25l\x1b[2J")?;
            o.flush()?;
            self.screen = true;
        }
        Ok(())
    }
}

impl Drop for TermGuard {
    fn drop(&mut self) {
        if self.screen {
            let mut o = io::stdout().lock();
            let _ = o.write_all(b"\x1b[0m\x1b[?25h\x1b[?1049l");
            let _ = o.flush();
        }
        let _ = crossterm::terminal::disable_raw_mode();
    }
}

/// Bytes waiting to be read on `fd` (`FIONREAD`); 0 if it can't be told.
fn pending_input(fd: i32) -> usize {
    extern "C" {
        fn ioctl(fd: i32, request: std::os::raw::c_ulong, ...) -> i32;
    }
    #[cfg(any(target_os = "macos", target_os = "ios", target_os = "freebsd"))]
    const FIONREAD: std::os::raw::c_ulong = 0x4004_667f;
    #[cfg(not(any(target_os = "macos", target_os = "ios", target_os = "freebsd")))]
    const FIONREAD: std::os::raw::c_ulong = 0x541b;
    let mut n: i32 = 0;
    // SAFETY: FIONREAD writes one int through the pointer.
    let r = unsafe { ioctl(fd, FIONREAD, &mut n as *mut i32) };
    if r == 0 && n > 0 {
        n as usize
    } else {
        0
    }
}

enum Msg {
    /// Bytes from stdin; `cooked`: taken in before raw mode.
    Input(Vec<u8>, bool),
    /// stdin closed: the popup went away.
    Eof,
    Preview(String, Option<Vec<StyledLine>>),
}

/// stdin → the channel: first the `cooked` bytes (as one message), then
/// everything else as it comes.
fn spawn_reader(tx: Sender<Msg>, mut cooked: usize) {
    std::thread::spawn(move || {
        let mut stdin = io::stdin().lock();
        let mut buf = [0u8; 4096];
        loop {
            let want = if cooked > 0 { cooked.min(buf.len()) } else { buf.len() };
            match stdin.read(&mut buf[..want]) {
                Ok(0) => break,
                Ok(n) => {
                    let was_cooked = cooked > 0;
                    cooked = cooked.saturating_sub(n);
                    if tx.send(Msg::Input(buf[..n].to_vec(), was_cooked)).is_err() {
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

/// The capture worker: runs only the newest request.
fn spawn_previewer(tmux: Tmux, tx: Sender<Msg>) -> Sender<(String, usize)> {
    let (ptx, prx) = mpsc::channel::<(String, usize)>();
    std::thread::spawn(move || {
        while let Ok(mut req) = prx.recv() {
            while let Ok(r) = prx.try_recv() {
                req = r;
            }
            let (name, n) = req;
            let lines = capture(&tmux, &name, n);
            if tx.send(Msg::Preview(name, lines)).is_err() {
                return;
            }
        }
    });
    ptx
}

fn trace(line: &str) {
    if let Ok(p) = std::env::var(TRACE_ENV) {
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(p) {
            let _ = writeln!(f, "{line}");
        }
    }
}

/// Run the outcome against the server.
fn act(tmux: &Tmux, client: &str, o: &Outcome) {
    match o {
        Outcome::Close => {}
        Outcome::Switch(name) => {
            let _ = tmux.run(&["switch-client", "-c", client, "-t", &format!("={name}")]);
        }
        Outcome::Create(name) => {
            let home = std::env::var("HOME").unwrap_or_else(|_| "/".into());
            let t = format!("={name}");
            let ok = tmux
                .run(&["new-session", "-d", "-s", name, "-c", &home, ";", "switch-client", "-c", client, "-t", &t])
                .is_ok_and(|o| o.ok());
            if !ok {
                // It exists after all (made meanwhile): just go there.
                let _ = tmux.run(&["switch-client", "-c", client, "-t", &t]);
            }
        }
    }
}

fn outcome_word(o: &Outcome) -> String {
    match o {
        Outcome::Close => "close".into(),
        Outcome::Switch(n) => format!("switch {n}"),
        Outcome::Create(n) => format!("create {n}"),
    }
}

type Term = ratatui::Terminal<ratatui::backend::CrosstermBackend<BufWriter<io::Stdout>>>;

/// Render; write only when the frame changed.
fn draw(term: &mut Term, p: &mut Picker, prev: Option<&[StyledLine]>, last: &mut Option<Buffer>) -> io::Result<()> {
    let (w, h) = crossterm::terminal::size()?;
    let mut buf = Buffer::empty(Rect::new(0, 0, w, h));
    render(p, &mut buf, prev);
    if last.as_ref() == Some(&buf) {
        return Ok(());
    }
    term.draw(|f| {
        let b = f.buffer_mut();
        if b.area == buf.area {
            b.content.clone_from(&buf.content);
        } else {
            render(p, b, prev);
        }
    })?;
    *last = Some(buf);
    Ok(())
}

fn interactive(tmux: Tmux, client: String) -> io::Result<()> {
    let t0 = Instant::now();
    // 1. Raw now: from here on nothing typed is cooked, echoed or lost.
    let mut guard = TermGuard::raw()?;
    let cooked = pending_input(0);
    // 2. The reader.
    let (tx, rx) = mpsc::channel::<Msg>();
    spawn_reader(tx.clone(), cooked);
    if cooked > 0 {
        trace(&format!("cooked_bytes {cooked}"));
    }
    // 3. One tmux call.
    let coll = Collator::from_env();
    let mut p = Picker::new();
    let data = load(&tmux, Some(&client), &coll);
    trace(&format!("loaded_us {}", t0.elapsed().as_micros()));
    let finish = |o: Outcome| {
        act(&tmux, &client, &o);
        trace(&format!("done_us {} {}", t0.elapsed().as_micros(), outcome_word(&o)));
    };
    if let Some(o) = p.load(data) {
        finish(o);
        return Ok(());
    }
    let previewer = spawn_previewer(tmux.clone(), tx);

    let mut reader = KeyReader::new();
    let mut term: Option<Term> = None;
    let mut last: Option<Buffer> = None;
    let mut previews: HashMap<String, Vec<StyledLine>> = HashMap::new();
    let mut next_refresh = Instant::now() + REFRESH;
    let mut want: Option<(String, usize)> = None;
    let mut preview_due: Option<Instant> = None;
    let mut first_preview = true;
    let mut drawn = false;
    // Something changed since the frame on screen.
    let mut dirty = true;

    loop {
        // Everything already queued is handled before drawing (typed-ahead
        // keys first of all); then wait for the next event.
        let msg = match rx.try_recv() {
            Ok(m) => Some(m),
            Err(TryRecvError::Disconnected) => return Ok(()),
            Err(TryRecvError::Empty) if dirty => None,
            Err(TryRecvError::Empty) => {
                let now = Instant::now();
                let mut deadline = next_refresh;
                if let Some(d) = preview_due {
                    deadline = deadline.min(d);
                }
                match rx.recv_timeout(deadline.saturating_duration_since(now)) {
                    Ok(m) => Some(m),
                    Err(RecvTimeoutError::Timeout) => None,
                    Err(RecvTimeoutError::Disconnected) => return Ok(()),
                }
            }
        };
        match msg {
            Some(Msg::Input(bytes, cooked)) => {
                let mut keys = if cooked { reader.feed_cooked(&bytes) } else { reader.feed(&bytes) };
                // A read that ended inside a sequence (or on a bare ESC) waits
                // ESC_WAIT for the rest; only silence makes a lone ESC "esc".
                let mut eof = false;
                while reader.pending() {
                    match rx.recv_timeout(ESC_WAIT) {
                        Ok(Msg::Input(b, c)) => {
                            keys.extend(if c { reader.feed_cooked(&b) } else { reader.feed(&b) });
                        }
                        Ok(Msg::Preview(name, lines)) => match lines {
                            Some(l) => {
                                previews.insert(name, l);
                            }
                            None => {
                                previews.remove(&name);
                            }
                        },
                        Ok(Msg::Eof) => {
                            keys.extend(reader.flush());
                            eof = true;
                        }
                        Err(_) => keys.extend(reader.flush()),
                    }
                }
                if let Some(o) = p.handle(&keys) {
                    finish(o);
                    return Ok(());
                }
                if eof {
                    trace(&format!("done_us {} eof", t0.elapsed().as_micros()));
                    return Ok(());
                }
                dirty = true;
                continue;
            }
            Some(Msg::Eof) => {
                trace(&format!("done_us {} eof", t0.elapsed().as_micros()));
                return Ok(());
            }
            Some(Msg::Preview(name, lines)) => {
                match lines {
                    Some(l) => {
                        previews.insert(name, l);
                    }
                    None => {
                        previews.remove(&name);
                    }
                }
                dirty = true;
                continue;
            }
            None => {}
        }
        dirty = false;
        if drawn && Instant::now() >= next_refresh {
            let d = load(&tmux, Some(&client), &coll);
            // A blank snapshot (a tmux timeout, a busy server) right after one
            // with sessions in it is not news: keep the rows on screen.
            if !d.all.is_empty() || p.data.all.is_empty() {
                p.set_data(d);
            }
            next_refresh = Instant::now() + REFRESH;
        }
        if term.is_none() {
            guard.screen()?;
            term = Some(ratatui::Terminal::new(ratatui::backend::CrosstermBackend::new(BufWriter::with_capacity(
                1 << 16,
                io::stdout(),
            )))?);
        }
        let sel = p.selected().map(|r| r.name.clone());
        let prev = sel.as_ref().and_then(|n| previews.get(n)).map(Vec::as_slice);
        if let Some(t) = term.as_mut() {
            draw(t, &mut p, prev, &mut last)?;
        }
        if !drawn {
            drawn = true;
            trace(&format!("first_frame_us {}", t0.elapsed().as_micros()));
        }

        // What the frame on screen wants previewed.
        let (w, h) = crossterm::terminal::size().unwrap_or((0, 0));
        let w2 = match (sel, Geo::preview_size(w, h)) {
            (Some(n), Some((_, ph))) => Some((n, ph as usize)),
            _ => None,
        };
        let now = Instant::now();
        if w2 != want {
            preview_due = w2.as_ref().map(|_| if first_preview { now } else { now + PREVIEW_DEBOUNCE });
            first_preview = false;
            want = w2;
        }
        if let (Some(due), Some(req)) = (preview_due, &want) {
            if now >= due {
                let _ = previewer.send(req.clone());
                preview_due = Some(now + PREVIEW_EVERY);
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
        let a = parse_args(&s(&["--client", "/dev/ttys004"])).unwrap();
        assert_eq!((a.client.as_deref(), a.once, a.ansi), (Some("/dev/ttys004"), None, false));
        let a = parse_args(&s(&["--once", "100x30", "--query", "wo", "--ansi"])).unwrap();
        assert_eq!((a.once, a.query.as_str(), a.ansi), (Some((100, 30)), "wo", true));
        assert!(parse_args(&s(&["--once", "100"])).is_err());
        assert!(parse_args(&s(&["--client"])).is_err());
        assert!(parse_args(&s(&["--bogus"])).is_err());
    }

    #[test]
    fn fionread_on_a_non_tty_is_zero() {
        // /dev/null: the ioctl fails or says 0; never a bogus count.
        let f = std::fs::File::open("/dev/null").unwrap();
        use std::os::fd::AsRawFd;
        assert_eq!(pending_input(f.as_raw_fd()), 0);
    }
}
