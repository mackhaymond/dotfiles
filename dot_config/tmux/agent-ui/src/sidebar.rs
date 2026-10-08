//! `agent-ui sidebar`: the herdr-style agent sidebar in a WezTerm split left
//! of the tmux client (CMD+B toggles it). It replaces agent-roster.py
//! `--strip`: the plumbing below is a port of its `Strip` class, while the
//! layout ([`layout`]) follows the v4 mockup.
//!
//! ```text
//! agent-ui sidebar [--client <tty>] [--tmux-pane <id>] [--wezterm <path>] [--split <0..1>]
//! agent-ui sidebar --once <W>x<H> [--client <tty>] [--split <0..1>]
//! ```
//!
//! - **Client.** Found, not passed ([`WeztermLink`]): the other pane in this
//!   WezTerm tab whose tty is an attached tmux client; re-resolved while it is
//!   missing. `--client` pins it (tests, screenshots).
//! - **Clicks only.** A left press acts on what was drawn at that cell
//!   ([`Target`]), resolved against the frame ON SCREEN. Every click hands
//!   focus straight back to the tmux pane first (Strip.handle → focus_back),
//!   and any key that lands here is forwarded there. The wheel does nothing:
//!   nothing scrolls. The divider is dragged (DECSET 1002 button-motion,
//!   never 1003) and its ratio is kept in tmux's global `@agent_sidebar_split`.
//! - **Cadence.** A snapshot every [`TICK`] (one tmux call), a redraw only
//!   when the rendered buffer changed, an immediate refresh after a move.
//!   Idle, it sleeps in poll(2).
//! - **Lifecycle.** The marker CMD+B finds the pane by is written once,
//!   before the alternate screen; SIGHUP / SIGTERM (CMD+B's kill-pane) or a
//!   closed tty end it quietly, with the terminal restored.
//! - **`--once WxH`** prints one frame in ANSI colour and exits: no
//!   alternate screen, no marker, no mouse (screenshots, tests).

mod input;
pub mod layout;
mod term;

pub use layout::{layout, Frame, Opts, Row, Seg, Target};

use crate::actions::{scripts_dir, sh_quote, watcher_age, Actions};
use crate::view::BuildArgs;
use crate::proc;
use crate::text;
use crate::tmux::{Snapshot, Socket, Tmux};
use crate::view::{Core, ViewModel};
use crate::wezterm::{self, WeztermLink, MOUSE_OFF, MOUSE_ON};
use ratatui::backend::CrosstermBackend;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::Terminal;
use std::io::{self, Stdout, Write};
use std::path::PathBuf;
use std::process::{Child, Command, ExitCode};
use std::time::{Duration, Instant};

/// One snapshot every half second: a steadier pulse than the Python's 1 s
/// (the watcher flips `@agent_blink` each second), still one cheap tmux call.
pub const TICK: Duration = Duration::from_millis(500);
/// The tmux global holding the divider's ratio.
pub const SPLIT_OPTION: &str = "@agent_sidebar_split";
/// agent-roster.py `ESC_WAIT`: how long a cut-off report waits for its rest.
const ESC_WAIT: Duration = Duration::from_millis(25);
/// How long a footer message stays.
const SAY_FOR: f64 = 4.0;
/// Button-motion reports, for dragging the divider (on top of MOUSE_ON's
/// 1000 + 1006; never 1003, which reports every motion).
const DRAG_ON: &str = "\x1b[?1002h";
const DRAG_OFF: &str = "\x1b[?1002l";

const USAGE: &str = "usage: agent-ui sidebar [--client <tty>] [--tmux-pane <id>] [--wezterm <path>] [--split <0..1>]
       agent-ui sidebar --once <W>x<H> [--client <tty>] [--split <0..1>]";

fn arg_after<'a>(args: &'a [String], flag: &str) -> Option<&'a str> {
    let i = args.iter().position(|a| a == flag)?;
    args.get(i + 1).map(String::as_str)
}

/// `34x58` → (34, 58).
fn parse_size(s: &str) -> Option<(u16, u16)> {
    let (w, h) = s.split_once('x')?;
    let (w, h) = (w.parse().ok()?, h.parse().ok()?);
    (w > 0 && h > 0).then_some((w, h))
}

fn parse_ratio(s: &str) -> Option<f64> {
    s.trim().parse::<f64>().ok().filter(|r| r.is_finite() && *r > 0.0 && *r < 1.0)
}

/// `@agent_sidebar_split`, if set to a sane ratio.
fn read_split(tmux: &Tmux) -> Option<f64> {
    let o = tmux.run(&["show-options", "-gqv", SPLIT_OPTION]).ok()?;
    parse_ratio(&o.stdout)
}

/// The divider ratio: `--split`, else the tmux option, else the default.
fn initial_split(tmux: &Tmux, args: &[String]) -> f64 {
    arg_after(args, "--split").and_then(parse_ratio).or_else(|| read_split(tmux)).unwrap_or(layout::DEFAULT_SPLIT)
}

pub fn run(tmux: Tmux, args: &[String]) -> ExitCode {
    if args.iter().any(|a| a == "-h" || a == "--help") {
        println!("{USAGE}");
        return ExitCode::SUCCESS;
    }
    if args.iter().any(|a| a == "--once") {
        let Some(size) = arg_after(args, "--once").and_then(parse_size) else {
            eprintln!("{USAGE}");
            return ExitCode::from(2);
        };
        return once(tmux, args, size);
    }
    match live(tmux, args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("agent-ui sidebar: {e}");
            ExitCode::FAILURE
        }
    }
}

/// `--once WxH`: one frame, git given a moment to land (like `dump`).
fn once(tmux: Tmux, args: &[String], (w, h): (u16, u16)) -> ExitCode {
    let mut core = Core::new(tmux.clone());
    core.git.branch_wait = Duration::from_millis(500);
    core.snapshot = tmux.snapshot();
    let client = match arg_after(args, "--client") {
        Some(c) => Some(c.to_string()),
        None => {
            // Inside a WezTerm pane: the client next to it, as the live sidebar would.
            let own = std::env::var("WEZTERM_PANE").ok().filter(|s| !s.is_empty());
            let mut link = WeztermLink::new(wezterm::wezterm_exe(arg_after(args, "--wezterm")), None, None, own.clone());
            if own.is_some() {
                link.resolve(&core.snapshot, text::now());
            }
            link.client
        }
    };
    // Lay out once to learn which spaces show git status, ask for those,
    // let it land (bounded), then lay out for real.
    let opts = Opts { split: initial_split(&tmux, args), msg: None };
    let first = layout(&build_view(&mut core, client.as_deref(), &[]), w, h, &opts);
    let _ = build_view(&mut core, client.as_deref(), &first.git_spaces);
    core.git.settle(Duration::from_secs(2));
    let view = build_view(&mut core, client.as_deref(), &first.git_spaces);
    let frame = layout(&view, w, h, &opts);
    let mut buf = Buffer::empty(Rect::new(0, 0, w, h));
    layout::paint(&frame, &mut buf);
    let mut out = io::stdout().lock();
    let _ = out.write_all(layout::to_ansi(&buf).as_bytes());
    ExitCode::SUCCESS
}

/// How long a good `git status` is reused in the sidebar (it runs all day).
const STATUS_TTL: f64 = 30.0;

/// The view, asking git only for what is drawn. `Core::rebuild` reads every
/// space's branch and starts a `git status` per repo every TTL, forever;
/// here a branch is read only for spaces with agents (the rules and space
/// rows that show it), and status only for `git_spaces`, the spaces the
/// last frame drew with their git line ([`Frame::git_spaces`]).
fn build_view(core: &mut Core, client: Option<&str>, git_spaces: &[String]) -> ViewModel {
    let now = text::now();
    let log = core.events.newest(core.log_limit);
    let args = BuildArgs { client, now, collator: &core.collator, git: None, log, watcher_age: watcher_age() };
    let mut view = ViewModel::build(&core.snapshot, args);
    for sp in view.spaces.iter_mut().filter(|s| !s.agents.is_empty()) {
        sp.branch = core.git.branch(&sp.path, now);
        if git_spaces.contains(&sp.name) {
            sp.git = core.git.status(&sp.path, now);
        }
    }
    view
}

/// The window a log line names, if it is still THAT window: the id in the
/// logged session at the logged index. Ids restart with the tmux server and
/// the log outlives it.
pub fn log_target_live(snap: &Snapshot, win: &str, session: &str, index: u32) -> bool {
    snap.row_in(win, session).is_some_and(|w| w.index == index)
}

/// ≡ / parked / `… more`: the Option-W menu as a popup on the sidebar's
/// client (Strip.open_menu → `roster_popup_argv`). This binary's `menu` when
/// its path is known (pointed at the same server; `-B`, since it draws its
/// own border and title), else the Python popup with tmux's ` agents ` title.
pub fn menu_command(tmux: &Tmux, client: &str, exe: Option<&std::path::Path>, tab: Option<&str>) -> Command {
    let mut c = tmux.command();
    c.args(["display-popup", "-c", client, "-E", "-w", "75%", "-h", "75%"]);
    match exe {
        Some(exe) => {
            // The Rust menu draws its own rounded border and title: no popup border.
            c.arg("-B").arg(exe);
            match &tmux.socket {
                Some(Socket::Name(s) | Socket::Path(s)) => {
                    c.args(["--socket", s]);
                }
                None => {}
            }
            c.args(["menu", "--client", client]);
            if let Some(t) = tab {
                c.args(["--tab", t]);
            }
        }
        None => {
            let py = ["/opt/homebrew", "/usr/local"]
                .iter()
                .map(|p| format!("{p}/bin/python3"))
                .find(|p| std::path::Path::new(p).exists())
                .unwrap_or_else(|| "/opt/homebrew/bin/python3".into());
            let script = format!(
                "[ -x {py} ] && exec {py} -I -S \"$0\" --client \"$1\"; exec /usr/bin/python3 -S \"$0\" --client \"$1\"",
                py = sh_quote(&py)
            );
            c.args(["-T", " agents ", "/bin/dash", "-c", &script]);
            c.arg(scripts_dir().join("agent-roster.py")).arg(client);
        }
    }
    c
}

/// The live sidebar: everything Strip holds between frames.
struct App {
    core: Core,
    actions: Actions,
    link: WeztermLink,
    view: ViewModel,
    /// What is on screen: clicks resolve against it (Strip.targets), and the
    /// buffer is compared to skip identical redraws.
    shown: Frame,
    buf: Option<Buffer>,
    /// Redraw in full next time (SIGWINCH).
    force: bool,
    split: f64,
    /// An open divider drag.
    drag: Option<Drag>,
    /// The last split written to tmux, and how often focus was handed back,
    /// and the last target acted on (observable state for the tests).
    saved_split: Option<f64>,
    focus_backs: u32,
    last_act: Option<Target>,
    /// Roster.say: the footer message and when it expires.
    msg: Option<(String, f64)>,
    /// display-popup clients, reaped on the tick (Strip.children).
    children: Vec<Child>,
    reader: input::Reader,
    exe: Option<PathBuf>,
    /// The spaces the last frame drew with their git line (see [`build_view`]).
    git_spaces: Vec<String>,
}

/// A divider drag: the split it started from (restored if it goes stale).
#[derive(Debug, Clone, Copy)]
struct Drag {
    from: f64,
}

impl App {
    fn new(tmux: Tmux, link: WeztermLink, split: f64) -> App {
        let mut core = Core::new(tmux.clone());
        core.git.status_ttl = STATUS_TTL;
        let view = build_view(&mut core, None, &[]);
        App {
            core,
            actions: Actions::new(tmux),
            link,
            view,
            shown: Frame::default(),
            buf: None,
            force: false,
            split,
            drag: None,
            saved_split: None,
            focus_backs: 0,
            last_act: None,
            msg: None,
            children: Vec::new(),
            reader: input::Reader::default(),
            exe: std::env::current_exe().ok(),
            git_spaces: Vec::new(),
        }
    }

    fn say(&mut self, text: impl Into<String>) {
        self.msg = Some((text.into(), text::now() + SAY_FOR));
    }

    /// Strip.refresh / load: reap popups, one snapshot, re-resolve the
    /// client if it went missing, then the view (git results land here too).
    fn refresh(&mut self) {
        self.children.retain_mut(|c| matches!(c.try_wait(), Ok(None)));
        self.core.snapshot = self.core.tmux.snapshot();
        self.link.on_snapshot(&self.core.snapshot, text::now());
        self.view = build_view(&mut self.core, self.link.client.as_deref(), &self.git_spaces);
    }

    fn focus_back(&mut self, data: &[u8]) {
        self.focus_backs += 1;
        if let Some(m) = self.link.focus_back(data, &self.core.snapshot, text::now()) {
            self.say(m);
        }
    }

    /// Strip.handle, events in order. Any press (modifiers too, not the
    /// wheel) hands focus back; only a plain left press acts, the last one in
    /// the read winning. A left press on the divider starts a drag instead:
    /// motion moves it, the release ends it (persisting it if it moved) and
    /// hands focus back. A press while a drag is open (its release was lost)
    /// drops that drag unsaved.
    fn handle(&mut self, data: &[u8]) {
        let inp = self.reader.feed(data);
        let mut focus = !inp.stray.is_empty();
        let mut act = None;
        for m in &inp.mice {
            if m.b & 64 != 0 {
                continue; // the wheel: nothing scrolls
            }
            if m.b & 32 != 0 {
                if self.drag.is_some() && m.b & 3 == 0 {
                    self.drag_to(m.y);
                }
                continue;
            }
            if !m.press {
                if let Some(d) = self.drag.take() {
                    self.drag_to(m.y);
                    if (self.split - d.from).abs() > f64::EPSILON {
                        self.save_split();
                    }
                    focus = true;
                }
                continue;
            }
            if let Some(d) = self.drag.take() {
                self.split = d.from; // a stale drag: never saved
            }
            focus = true;
            if m.b != 0 {
                continue; // right/middle, or a modifier held: focus only
            }
            match self.shown.target(m.x.saturating_sub(1), m.y.saturating_sub(1)).cloned() {
                Some(Target::Divider) => {
                    self.drag = Some(Drag { from: self.split });
                    act = None;
                }
                t => act = t,
            }
        }
        if self.drag.is_some() {
            // Mid-drag: focus goes back on the release, or the rest of the
            // drag would go to the tmux pane. Typed bytes still go now.
            if !inp.stray.is_empty() {
                self.focus_back(&inp.stray);
            }
            return;
        }
        if focus {
            self.focus_back(&inp.stray);
        }
        if let Some(t) = act {
            self.click(t);
        }
    }

    fn drag_to(&mut self, y1: u16) {
        self.split = layout::ratio_for_row(y1.saturating_sub(1), self.shown.height);
    }

    /// Remember the divider in `@agent_sidebar_split`.
    fn save_split(&mut self) {
        let v = format!("{:.4}", self.split);
        let _ = self.core.tmux.run(&["set-option", "-g", SPLIT_OPTION, &v]);
        self.saved_split = Some(self.split);
    }

    /// Strip.click: act on a target, then refresh at once after a move so
    /// the highlight follows it.
    fn click(&mut self, t: Target) {
        self.last_act = Some(t.clone());
        if t == Target::Restart {
            let m = self.actions.restart_watcher();
            self.say(m);
            return;
        }
        let Some(client) = self.link.client.clone() else {
            self.say("no tmux client");
            return;
        };
        let snap = &self.core.snapshot;
        let moved = match t {
            Target::Go { win, session } => self.actions.go(snap, &client, &win, &session).map(|_| true),
            Target::Log { win, session, index } => {
                if log_target_live(snap, &win, &session, index) {
                    self.actions.go(snap, &client, &win, &session).map(|_| true)
                } else {
                    Err("that tab is gone".into())
                }
            }
            Target::Session(s) => self.actions.goto_session(snap, &client, &s).map(|_| true),
            Target::Next => self.actions.next(&client).map(|_| true),
            Target::Back => self.actions.back(&client).map(|_| true),
            Target::Menu(tab) => {
                let cmd = menu_command(&self.core.tmux, &client, self.exe.as_deref(), tab);
                match proc::spawn_detached(cmd) {
                    Ok(child) => {
                        self.children.push(child);
                        Ok(false)
                    }
                    Err(e) => Err(format!("menu failed: {e}")),
                }
            }
            Target::Divider | Target::Restart => Ok(false),
        };
        match moved {
            Ok(true) => self.refresh(),
            Ok(false) => {}
            Err(m) => self.say(m),
        }
    }

    /// Lay out and draw, unless the rendered buffer is what is on screen.
    fn draw(&mut self, term: &mut Terminal<CrosstermBackend<Stdout>>) -> io::Result<()> {
        let size = term.size()?;
        let now = text::now();
        let msg = self.msg.as_ref().filter(|(_, until)| *until > now).map(|(m, _)| m.clone());
        let frame = layout(&self.view, size.width, size.height, &Opts { split: self.split, msg });
        let mut buf = Buffer::empty(Rect::new(0, 0, size.width, size.height));
        layout::paint(&frame, &mut buf);
        self.git_spaces.clone_from(&frame.git_spaces);
        if !self.force && self.buf.as_ref() == Some(&buf) {
            self.shown = frame;
            return Ok(());
        }
        if std::mem::take(&mut self.force) {
            term.clear()?;
        }
        term.draw(|f| {
            if f.area() == buf.area {
                f.buffer_mut().clone_from(&buf);
            } else {
                layout::paint(&frame, f.buffer_mut());
            }
        })?;
        self.buf = Some(buf);
        self.shown = frame;
        Ok(())
    }

    /// agent-roster.py `main`'s loop: wait for input, a signal or the tick;
    /// handle input against the frame on screen BEFORE drawing a new one.
    fn run(&mut self, term: &mut Terminal<CrosstermBackend<Stdout>>, sig: &term::Signals) -> io::Result<()> {
        self.refresh();
        let mut next = Instant::now() + TICK;
        self.draw(term)?;
        loop {
            let has_input = sig.wait(0, next.saturating_duration_since(Instant::now()))?;
            let fired = sig.take();
            if fired.exit {
                return Ok(());
            }
            self.force |= fired.resize;
            if has_input {
                let Some(mut data) = term::read_stdin()? else { return Ok(()) };
                // A read that ended inside a report waits ESC_WAIT for the rest.
                for _ in 0..8 {
                    if !input::incomplete(&data) || !term::readable(0, ESC_WAIT)? {
                        break;
                    }
                    match term::read_stdin()? {
                        Some(more) => data.extend(more),
                        None => return Ok(()),
                    }
                }
                self.handle(&data);
            }
            if Instant::now() >= next {
                self.refresh();
                next = Instant::now() + TICK;
            }
            // Input that arrived meanwhile was aimed at the frame on screen.
            if term::readable(0, Duration::ZERO)? {
                continue;
            }
            self.draw(term)?;
        }
    }
}

/// Set up the terminal, run, and restore it whatever happened. Errors once
/// running (the pane is gone: EIO, EPIPE) end it quietly, like the Python.
fn live(tmux: Tmux, args: &[String]) -> io::Result<()> {
    let link = WeztermLink::new(
        wezterm::wezterm_exe(arg_after(args, "--wezterm")),
        arg_after(args, "--client").map(String::from),
        arg_after(args, "--tmux-pane").map(String::from),
        std::env::var("WEZTERM_PANE").ok().filter(|s| !s.is_empty()),
    );
    let split = initial_split(&tmux, args);
    let mut app = App::new(tmux, link, split);
    let sig = term::Signals::install()?;
    let mut out = io::stdout();
    out.write_all(wezterm::strip_marker().as_bytes())?;
    out.flush()?;
    crossterm::terminal::enable_raw_mode()?;
    let ran = (|| -> io::Result<()> {
        write!(out, "\x1b[?1049h\x1b[?25l\x1b[2J{MOUSE_ON}{DRAG_ON}")?;
        out.flush()?;
        let mut term = Terminal::new(CrosstermBackend::new(io::stdout()))?;
        app.run(&mut term, &sig)
    })();
    let _ = write!(out, "{DRAG_OFF}{MOUSE_OFF}\x1b[0m\x1b[?25h\x1b[?1049l");
    let _ = out.flush();
    let _ = crossterm::terminal::disable_raw_mode();
    let _ = ran; // the pane went away: nothing left to report to
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tmux::tests::row;

    fn args(c: &Command) -> Vec<String> {
        c.get_args().map(|a| a.to_string_lossy().into_owned()).collect()
    }

    #[test]
    fn menu_popup_argv() {
        let t = Tmux::with_socket(Some(Socket::Name("priv".into())));
        let c = menu_command(&t, "/dev/ttys999", Some(std::path::Path::new("/x/agent-ui")), Some("parked"));
        assert_eq!(
            args(&c),
            ["-u", "-L", "priv", "display-popup", "-c", "/dev/ttys999", "-E", "-w", "75%", "-h", "75%", "-B",
                "/x/agent-ui", "--socket", "priv", "menu", "--client", "/dev/ttys999", "--tab", "parked"]
        );
        // No binary path: the Python popup (prefix q's line, with its title).
        let c = menu_command(&Tmux::default(), "/dev/ttys999", None, None);
        let a = args(&c);
        assert_eq!(&a[a.len() - 7..a.len() - 3], ["-T", " agents ", "/bin/dash", "-c"]);
        assert!(!a.contains(&"-B".to_string()));
        assert!(a[a.len() - 2].ends_with("agent-roster.py") && a[a.len() - 1] == "/dev/ttys999");
    }

    /// An App over a tmux socket that does not exist (no command here can
    /// start a server) and no client (so no action runs: click() says "no
    /// tmux client"), showing an empty 34x30 frame: the divider is row 16 (1-based).
    fn app() -> App {
        let tmux = Tmux::with_socket(Some(Socket::Name(format!("agentui-u3-none-{}", std::process::id()))));
        let link = WeztermLink::new("/nonexistent/wezterm".into(), None, None, None);
        let mut a = App::new(tmux, link, 0.5);
        a.shown = layout(&a.view, 34, 30, &Opts::default());
        assert_eq!(a.shown.split, 15);
        a
    }

    #[test]
    fn divider_press_and_release_in_one_read_is_a_click() {
        let mut a = app();
        a.handle(b"\x1b[<0;17;16M\x1b[<0;17;16m");
        assert!(a.drag.is_none());
        assert_eq!((a.split, a.saved_split, a.focus_backs, a.last_act.clone()), (0.5, None, 1, None));
        // Press, drag and release in one read: moved and saved.
        a.handle(b"\x1b[<0;17;16M\x1b[<32;17;13M\x1b[<32;17;11M\x1b[<0;17;11m");
        assert!(a.drag.is_none());
        assert_eq!(a.split, layout::ratio_for_row(10, 30));
        assert_eq!((a.saved_split, a.focus_backs), (Some(a.split), 2));
    }

    #[test]
    fn a_lost_release_never_moves_the_divider_later() {
        let mut a = app();
        a.handle(b"\x1b[<0;17;16M\x1b[<32;17;20M");
        assert!(a.drag.is_some());
        assert_eq!(a.focus_backs, 0); // mid-drag: focus stays until the release
        // The release never comes; the next click (row 25) drops the drag
        // unsaved, restores the split, hands focus back and is a click.
        a.handle(b"\x1b[<0;5;25M\x1b[<32;5;27M\x1b[<0;5;25m");
        assert!(a.drag.is_none());
        assert_eq!((a.split, a.saved_split, a.focus_backs), (0.5, None, 1));
        // A click on the parked row acts.
        a.handle(b"\x1b[<0;3;30M\x1b[<0;3;30m");
        assert_eq!(a.last_act, Some(Target::Menu(Some("parked"))));
    }

    #[test]
    fn modified_clicks_hand_focus_back_without_acting() {
        let mut a = app();
        for b in [4u16, 8, 16, 2, 1, 18] {
            a.handle(format!("\x1b[<{b};3;30M\x1b[<{b};3;30m").as_bytes());
        }
        assert_eq!((a.focus_backs, a.last_act.clone()), (6, None));
        // The wheel neither acts nor takes focus; typing is forwarded.
        a.handle(b"\x1b[<64;3;30M\x1b[<65;3;30M");
        assert_eq!(a.focus_backs, 6);
        a.handle(b"x");
        assert_eq!(a.focus_backs, 7);
    }

    #[test]
    fn log_lines_only_reach_the_same_window() {
        let snap = Snapshot::parse(&[row("main", 3, "@7", &[]), row("work", 1, "@8", &[])].join("\n"));
        assert!(log_target_live(&snap, "@7", "main", 3));
        assert!(!log_target_live(&snap, "@7", "main", 4)); // renumbered, or a new server reusing @7
        assert!(!log_target_live(&snap, "@7", "work", 3)); // moved
        assert!(!log_target_live(&snap, "@9", "main", 3)); // gone
    }

    #[test]
    fn args_parse() {
        assert_eq!(parse_size("34x58"), Some((34, 58)));
        for bad in ["34", "x58", "0x5", "34x-1", "axb"] {
            assert_eq!(parse_size(bad), None, "{bad}");
        }
        assert_eq!(parse_ratio("0.4200\n"), Some(0.42));
        for bad in ["", "1", "0", "-0.2", "NaN", "inf", "x"] {
            assert_eq!(parse_ratio(bad), None, "{bad}");
        }
    }
}
