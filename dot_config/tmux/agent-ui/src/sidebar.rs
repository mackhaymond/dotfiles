//! `agent-ui sidebar`: the herdr-style agent sidebar in a WezTerm split beside
//! the tmux client (CMD+B toggles it). It replaces agent-roster.py `--strip`:
//! the plumbing below is a port of its `Strip` class, while the layout
//! ([`layout`]) is its own: NEEDS YOU, then the agents (the body), then a
//! compact spaces list.
//!
//! ```text
//! agent-ui sidebar [--client <tty>] [--tmux-pane <id>] [--wezterm <path>]
//! agent-ui sidebar --once <W>x<H> [--client <tty>]
//! ```
//!
//! - **Client.** Found, not passed ([`WeztermLink`]): the other pane in this
//!   WezTerm tab whose tty is an attached tmux client; re-resolved while it is
//!   missing. `--client` pins it (tests, screenshots).
//! - **Never alone.** Once its client has been seen, the sidebar exits as
//!   soon as that client process exits (detach, exit, or its pane closed):
//!   a kqueue NOTE_EXIT on `#{client_pid}` ([`term::ProcWatch`]), polled
//!   with everything else, so it costs nothing until it fires; a snapshot
//!   that no longer lists the client says the same. While no client is
//!   resolved, a `wezterm cli list` that finds the CMD+B pane gone, or
//!   nothing else in the tab, ends it too ([`wezterm::alone_in`]).
//! - **Clicks only.** A left press acts on what was drawn at that cell
//!   ([`Target`]), resolved against the frame ON SCREEN; a plain RIGHT
//!   press on an agent row or NEEDS YOU card parks that window (`stash.sh
//!   stash <win>`), and `⏸ N parked` opens prefix h's stash picker
//!   ([`stash_command`]). Every click hands
//!   focus straight back to the tmux pane first (Strip.handle → focus_back),
//!   and any key that lands here is forwarded there. The wheel does nothing:
//!   nothing scrolls.
//! - **Scope.** `@spaces` / `@strip_scope` (globals, read in the same
//!   snapshot call): "space" shows only the client's session, numbered by
//!   agent ordinal (tmux's M-1..M-9); "all" every session
//!   ([`crate::view::Scope`]). Option-V (ESC v / V), the one key NOT
//!   forwarded, or a click on the header title (once the scope is a thing)
//!   flips and persists it, then posts [`notice::FOCUS_NOTICE`] so every
//!   other sidebar follows at once.
//! - **Cadence: told, not polling.** The loop sleeps in poll(2) on stdin,
//!   the signal pipe, the Darwin notice fd ([`notice`]: agent-state and focus
//!   notices) and the client's exit watch. A notice schedules one snapshot
//!   [`notice::DEBOUNCE`] later (a burst is one refresh); a click that moves
//!   refreshes at once. The timer is only a safety net: [`FALLBACK`] (5 s)
//!   normally, [`TICK`] (0.5 s) while a drawn agent pulses with
//!   `@agent_blink` (the watcher flips it each second) or when there is no
//!   notice fd. A redraw happens only when the rendered buffer changed.
//! - **Lifecycle.** The marker CMD+B finds the pane by is written once,
//!   before the alternate screen; SIGHUP / SIGTERM (CMD+B's kill-pane) or a
//!   closed tty end it quietly, with the terminal restored.
//! - **`--once WxH`** prints one frame in ANSI colour and exits: no
//!   alternate screen, no marker, no mouse (screenshots, tests).

mod input;
pub mod layout;
pub mod notice;
mod term;

pub use layout::{layout, Frame, Opts, Row, Seg, Target};

use crate::actions::{scripts_dir, sh_quote, watcher_age, Actions};
use crate::proc;
use crate::text;
use crate::tmux::{Snapshot, Socket, Tmux, HOLD};
use crate::view::BuildArgs;
use crate::view::{Core, ViewModel};
use crate::wezterm::{self, WeztermLink, MOUSE_OFF, MOUSE_ON};
use notice::Notices;
use ratatui::backend::CrosstermBackend;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::Terminal;
use std::collections::HashSet;
use std::io::{self, Stdout, Write};
use std::path::PathBuf;
use std::process::{Child, Command, ExitCode};
use std::time::{Duration, Instant};

/// The fast tick: while a drawn agent pulses (`@agent_blink` flips each
/// second, so half a second never aliases), or with no notice fd at all.
pub const TICK: Duration = Duration::from_millis(500);
/// The safety-net poll when notices tell us about every change.
pub const FALLBACK: Duration = Duration::from_secs(5);
/// The tmux global holding the scope (`space` | `all`; unset: by `@spaces`).
pub const SCOPE_OPTION: &str = "@strip_scope";
/// agent-roster.py `ESC_WAIT`: how long a cut-off report waits for its rest.
const ESC_WAIT: Duration = Duration::from_millis(25);
/// How long a footer message stays.
const SAY_FOR: f64 = 4.0;

const USAGE: &str = "usage: agent-ui sidebar [--client <tty>] [--tmux-pane <id>] [--wezterm <path>]
       agent-ui sidebar --once <W>x<H> [--client <tty>]";

fn arg_after<'a>(args: &'a [String], flag: &str) -> Option<&'a str> {
    let i = args.iter().position(|a| a == flag)?;
    args.get(i + 1).map(String::as_str)
}

/// agent-roster.py `SCOPE_KEY` (`\x1b[vV]`): typed bytes with every Option-V
/// (ESC v / ESC V) taken out → (the rest, how many). CSI `ESC [ v` is not
/// one, and neither is Option-S (tmux's M-s): those are forwarded.
fn take_scope_keys(stray: &[u8]) -> (Vec<u8>, usize) {
    let (mut out, mut n, mut i) = (Vec::with_capacity(stray.len()), 0, 0);
    while i < stray.len() {
        if stray[i] == 0x1b && matches!(stray.get(i + 1), Some(b'v' | b'V')) {
            n += 1;
            i += 2;
        } else {
            out.push(stray[i]);
            i += 1;
        }
    }
    (out, n)
}

/// `34x58` → (34, 58).
fn parse_size(s: &str) -> Option<(u16, u16)> {
    let (w, h) = s.split_once('x')?;
    let (w, h) = (w.parse().ok()?, h.parse().ok()?);
    (w > 0 && h > 0).then_some((w, h))
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

/// `--once WxH`: one frame, branches given a moment to land (like `dump`).
fn once(tmux: Tmux, args: &[String], (w, h): (u16, u16)) -> ExitCode {
    let mut core = Core::new(tmux.clone());
    core.git.branch_wait = Duration::from_millis(500);
    core.snapshot = tmux.snapshot();
    hide_homes(&mut core.snapshot);
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
    let view = build_view(&mut core, client.as_deref());
    let frame = layout(&view, w, h, &Opts::default());
    let mut buf = Buffer::empty(Rect::new(0, 0, w, h));
    layout::paint(&frame, &mut buf);
    let mut out = io::stdout().lock();
    let _ = out.write_all(layout::to_ansi(&buf).as_bytes());
    ExitCode::SUCCESS
}

/// Spaces' home windows (`@space_home` 1, a space's landing screen) are
/// never agents here, in any scope: whatever agent options one carries are
/// dropped, so nothing counts or lists it.
fn hide_homes(snap: &mut Snapshot) {
    for w in snap.windows.iter_mut().filter(|w| w.home) {
        for f in [&mut w.state, &mut w.workflow, &mut w.cua, &mut w.summary, &mut w.detail, &mut w.detail_kind] {
            f.clear();
        }
    }
}

/// The view. Git: a branch (cheap file reads, cached) for the spaces the
/// agents section draws a rule for; never `git status` (the compact spaces
/// list has no git line). No event log. A home window the client sits on is
/// never an agent row (with `@spaces` off it would be the classic one).
fn build_view(core: &mut Core, client: Option<&str>) -> ViewModel {
    let now = text::now();
    let args = BuildArgs { client, now, collator: &core.collator, git: None, log: Vec::new(), watcher_age: watcher_age() };
    let mut view = ViewModel::build_with(&core.snapshot, args, &mut core.slots);
    view.apply_scope(&core.snapshot);
    let homes: HashSet<&str> = core.snapshot.windows.iter().filter(|w| w.home).map(|w| w.id.as_str()).collect();
    let scoped = view.scope.session.clone();
    for sp in view.spaces.iter_mut() {
        if !homes.is_empty() {
            let (gone, kept): (Vec<_>, Vec<_>) = sp.agents.drain(..).partition(|a| homes.contains(a.window_id.as_str()));
            sp.agents = kept;
            if !gone.is_empty() {
                sp.plain.extend(gone);
                sp.plain.sort_by_key(|a| a.index);
            }
        }
        if !sp.agents.is_empty() || scoped.as_ref() == Some(&sp.name) {
            sp.branch = core.git.branch(&sp.path, now);
        }
    }
    view
}

/// The menu popup's `-w`: tmux.conf's Option-W cap (at most 100 columns, the
/// whole client when narrower). display-popup neither expands formats in
/// `-w` nor clamps a width wider than the client (it fails), so the client's
/// width is asked for first; unknown → the old 75%.
pub fn popup_width(client_cols: Option<u16>) -> String {
    match client_cols {
        Some(c) if c > 0 => c.min(100).to_string(),
        _ => "75%".into(),
    }
}

/// ≡ / `… more` / `+K more`: the Option-W menu as a popup on the sidebar's
/// client (Strip.open_menu → `roster_popup_argv`). This binary's `menu` when
/// its path is known (pointed at the same server; `-B`, since it draws its
/// own border and title), else the Python popup with tmux's ` agents ` title.
/// `width` is [`popup_width`]'s.
pub fn menu_command(tmux: &Tmux, client: &str, exe: Option<&std::path::Path>, tab: Option<&str>, width: &str) -> Command {
    let mut c = tmux.command();
    c.args(["display-popup", "-c", client, "-E", "-w", width, "-h", "75%"]);
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

/// `⏸ N parked`: exactly what prefix h runs (tmux.conf.tmpl `bind-key h
/// run-shell "…/stash.sh unstash '' '#{client_name}'"`), with the sidebar's
/// client in place of `#{client_name}`. A foreground run-shell, as there: the
/// script raises the picker with `display-popup -c <client>` and returns when
/// it closes, so the sidebar spawns it detached and reaps it on refresh.
pub fn stash_command(tmux: &Tmux, client: &str, scripts: &std::path::Path) -> Command {
    let stash = scripts.join("stash.sh");
    let mut c = tmux.command();
    c.args(["run-shell", &format!("{} unstash '' {}", sh_quote(&stash.to_string_lossy()), sh_quote(client))]);
    c
}

/// How long a right-click park waits to see its window reach the stash
/// before the footer stops tracking it.
const PARK_WATCH: f64 = 15.0;

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
    /// How often focus was handed back, and the last target acted on
    /// (observable state for the tests).
    focus_backs: u32,
    last_act: Option<Target>,
    /// The last right-click park asked for, (window id, session).
    last_park: Option<(String, String)>,
    /// A park in flight: (window id, label, give up at). Once a snapshot
    /// shows the window in the stash, the footer says `parked <label>`.
    parking: Option<(String, String, f64)>,
    /// Roster.say: the footer message and when it expires.
    msg: Option<(String, f64)>,
    /// display-popup clients, reaped on refresh (Strip.children).
    children: Vec<Child>,
    reader: input::Reader,
    exe: Option<PathBuf>,
    /// The Darwin notices this sidebar wakes on ([`notice`]).
    notices: Notices,
    /// The focus notice's name, posted after a scope toggle.
    focus_notice: String,
    /// The client process being watched for its exit.
    watch: Option<term::ProcWatch>,
    /// The client's pid once seen in a snapshot (never for a fake client).
    client_pid: Option<i32>,
    /// The client is gone (or the sidebar is alone in its tab): exit.
    gone: bool,
}

impl App {
    fn new(tmux: Tmux, link: WeztermLink, notices: Notices) -> App {
        let mut core = Core::new(tmux.clone());
        let view = build_view(&mut core, None);
        App {
            core,
            actions: Actions::new(tmux),
            link,
            view,
            shown: Frame::default(),
            buf: None,
            force: false,
            focus_backs: 0,
            last_act: None,
            last_park: None,
            parking: None,
            msg: None,
            children: Vec::new(),
            reader: input::Reader::default(),
            exe: std::env::current_exe().ok(),
            notices,
            focus_notice: notice::names()[1].clone(),
            watch: None,
            client_pid: None,
            gone: false,
        }
    }

    fn say(&mut self, text: impl Into<String>) {
        self.msg = Some((text.into(), text::now() + SAY_FOR));
    }

    /// Strip.refresh / load: reap popups, one snapshot, re-resolve the
    /// client if it went missing, then the view; then whether the client
    /// (or the tab) is gone.
    fn refresh(&mut self) {
        self.children.retain_mut(|c| matches!(c.try_wait(), Ok(None)));
        self.core.snapshot = self.core.tmux.snapshot();
        hide_homes(&mut self.core.snapshot);
        self.link.on_snapshot(&self.core.snapshot, text::now());
        self.view = build_view(&mut self.core, self.link.client.as_deref());
        self.check_client();
        self.check_park();
    }

    /// A park in flight that a snapshot now shows in the stash: say so
    /// (stash.sh refuses some, a working agent or a session's last tab, and
    /// says why on the status line; then the `parking …` note just lapses).
    fn check_park(&mut self) {
        let Some((win, label, until)) = self.parking.clone() else { return };
        if self.core.snapshot.row_in(&win, HOLD).is_some() {
            self.parking = None;
            self.say(format!("parked {label}"));
        } else if text::now() > until {
            self.parking = None;
        }
    }

    /// A right-click on an agent row or NEEDS YOU card: park that window
    /// (`stash.sh stash <win>`, which records @stash_origin, so unparking
    /// puts it back in its space). The move fires tmux's window-unlinked
    /// hook, whose agent-state notice redraws every sidebar at once.
    fn park(&mut self, t: &Target) {
        let Target::Go { win, session } = t else { return };
        self.last_park = Some((win.clone(), session.clone()));
        match self.actions.park(&self.core.snapshot, win, session) {
            Ok(m) => {
                let label = self.core.snapshot.row_in(win, session).map(|w| w.label.clone()).unwrap_or_default();
                self.parking = Some((win.clone(), label, text::now() + PARK_WATCH));
                self.say(m);
            }
            Err(m) => self.say(m),
        }
    }

    /// Track the client process: watch the pid a snapshot shows (its exit
    /// ends the loop, NOTE_EXIT); a pid already dead means it is gone. A
    /// client missing from the snapshot is NOT proof: tmux 3.7 leaves
    /// suspended (prefix C-z) and locked clients out of list-clients, so
    /// only a dead pid counts then. A client never seen (a fake `--client`,
    /// none yet) is never gone. Unresolved and alone in the tab: gone too.
    fn check_client(&mut self) {
        if self.link.alone && !self.link.fixed {
            self.gone = true;
            return;
        }
        let row = self.link.client.as_deref().and_then(|c| self.core.snapshot.client(c));
        match row.and_then(|c| c.pid) {
            Some(pid) if self.watch.as_ref().map(|w| w.pid) != Some(pid) => {
                self.client_pid = Some(pid);
                self.watch = term::ProcWatch::new(pid);
                if self.watch.is_none() && !term::alive(pid) {
                    self.gone = true;
                }
            }
            Some(_) => {}
            // Not listed (suspended, locked, or gone): gone only if dead.
            None => self.gone |= self.client_pid.is_some_and(|pid| !term::alive(pid)),
        }
    }

    fn focus_back(&mut self, data: &[u8]) {
        self.focus_backs += 1;
        if let Some(m) = self.link.focus_back(data, &self.core.snapshot, text::now()) {
            self.say(m);
        }
    }

    /// Strip.handle, events in order. Any press (modifiers too, not the
    /// wheel) hands focus back; only a plain left press acts, the last one
    /// in the read winning. Option-V (ESC v / ESC V) is the one key the
    /// sidebar keeps: the scope toggle. It is never forwarded, but focus
    /// still goes back; it acts after that, so its message is the one shown.
    fn handle(&mut self, data: &[u8]) {
        let mut inp = self.reader.feed(data);
        let (stray, toggles) = take_scope_keys(&inp.stray);
        inp.stray = stray;
        let mut focus = toggles > 0 || !inp.stray.is_empty();
        let mut act = None;
        let mut park = None;
        for m in &inp.mice {
            if m.b & 64 != 0 || m.b & 32 != 0 || !m.press {
                continue; // the wheel, motion, releases: nothing
            }
            focus = true;
            let t = self.shown.target(m.x.saturating_sub(1), m.y.saturating_sub(1)).cloned();
            match m.b {
                0 => act = t,
                // A plain right press (SGR button 2; WezTerm has no right-click
                // binding of its own, so it reaches a mouse-reporting pane):
                // park, on an agent row or card only.
                2 => park = t.filter(|t| matches!(t, Target::Go { .. })),
                _ => {} // middle, or a modifier held: focus only
            }
        }
        if focus {
            self.focus_back(&inp.stray);
        }
        if let Some(t) = act {
            self.click(t);
        }
        if let Some(t) = park {
            self.park(&t);
        }
        for _ in 0..toggles {
            self.toggle_scope();
        }
    }

    /// Strip.toggle_scope: flip the scope and persist it (`set-option -g
    /// @strip_scope`), so it survives CMD+B and is the same in every
    /// sidebar (the focus notice wakes the others); then redraw from a fresh
    /// snapshot (which reads it back). A failed set keeps the old scope and
    /// says so.
    fn toggle_scope(&mut self) {
        let new = self.view.scope.flipped();
        match self.core.tmux.run(&["set-option", "-g", SCOPE_OPTION, new]) {
            Ok(o) if o.ok() => {
                notice::post(&self.focus_notice);
                self.say(if new == "space" { "showing this space" } else { "showing every space" });
                self.refresh();
            }
            _ => self.say(format!("couldn't set {SCOPE_OPTION}")),
        }
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
        if t == Target::Scope {
            self.toggle_scope();
            return;
        }
        let Some(client) = self.link.client.clone() else {
            self.say("no tmux client");
            return;
        };
        let snap = &self.core.snapshot;
        let moved = match t {
            Target::Go { win, session } => self.actions.go(snap, &client, &win, &session).map(|_| true),
            Target::Session(s) => self.actions.goto_session(snap, &client, &s).map(|_| true),
            Target::Next => self.actions.next(&client).map(|_| true),
            Target::Back => self.actions.back(&client).map(|_| true),
            Target::Menu(tab) => {
                let cols = self
                    .core
                    .tmux
                    .run(&["display-message", "-p", "-c", &client, "#{client_width}"])
                    .ok()
                    .filter(|o| o.ok())
                    .and_then(|o| o.stdout.trim().parse::<u16>().ok());
                let cmd = menu_command(&self.core.tmux, &client, self.exe.as_deref(), tab, &popup_width(cols));
                match proc::spawn_detached(cmd) {
                    Ok(child) => {
                        self.children.push(child);
                        Ok(false)
                    }
                    Err(e) => Err(format!("menu failed: {e}")),
                }
            }
            Target::Stash => match proc::spawn_detached(stash_command(&self.core.tmux, &client, &self.actions.scripts)) {
                Ok(child) => {
                    self.children.push(child);
                    Ok(false)
                }
                Err(e) => Err(format!("stash failed: {e}")),
            },
            Target::Restart | Target::Scope => Ok(false),
        };
        match moved {
            Ok(true) => self.refresh(),
            Ok(false) => {}
            Err(m) => self.say(m),
        }
    }

    /// The safety-net poll interval ([`TICK`] / [`FALLBACK`]).
    fn cadence(&self) -> Duration {
        if self.notices.fd().is_none() || layout::pulses(&self.view) {
            TICK
        } else {
            FALLBACK
        }
    }

    /// When the footer message expires (a redraw then), if one is showing.
    fn msg_left(&self) -> Option<Duration> {
        let left = self.msg.as_ref()?.1 - text::now();
        (left > 0.0).then(|| Duration::from_secs_f64(left))
    }

    /// Lay out and draw, unless the rendered buffer is what is on screen.
    fn draw(&mut self, term: &mut Terminal<CrosstermBackend<Stdout>>) -> io::Result<()> {
        let size = term.size()?;
        let now = text::now();
        let msg = self.msg.as_ref().filter(|(_, until)| *until > now).map(|(m, _)| m.clone());
        let frame = layout(&self.view, size.width, size.height, &Opts { msg });
        let mut buf = Buffer::empty(Rect::new(0, 0, size.width, size.height));
        layout::paint(&frame, &mut buf);
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

    /// agent-roster.py `main`'s loop: wait for input, a notice, the client's
    /// exit, a signal or the timer; handle input against the frame on screen
    /// BEFORE drawing a new one.
    fn run(&mut self, term: &mut Terminal<CrosstermBackend<Stdout>>, sig: &term::Signals) -> io::Result<()> {
        self.refresh();
        let mut next = Instant::now() + self.cadence();
        // A notice-driven refresh, due DEBOUNCE after the first notice of a burst.
        let mut due: Option<Instant> = None;
        self.draw(term)?;
        loop {
            if self.gone {
                return Ok(());
            }
            let now = Instant::now();
            let mut wake = next.min(due.unwrap_or(next));
            if let Some(left) = self.msg_left() {
                wake = wake.min(now + left + Duration::from_millis(5));
            }
            let fds = [self.notices.fd(), self.watch.as_ref().map(term::ProcWatch::fd)];
            let (has_input, [noticed, exited]) = sig.wait(0, fds, wake.saturating_duration_since(now))?;
            let fired = sig.take();
            if fired.exit || exited {
                // CMD+B's kill-pane, or the tmux client next to us exited.
                return Ok(());
            }
            self.force |= fired.resize;
            if noticed && self.notices.drain() > 0 {
                due.get_or_insert(Instant::now() + notice::DEBOUNCE);
            }
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
            let now = Instant::now();
            if now >= next || due.is_some_and(|d| now >= d) {
                self.refresh();
                due = None;
                next = Instant::now() + self.cadence();
            }
            if self.gone {
                return Ok(());
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
    let mut app = App::new(tmux, link, Notices::register(&notice::names()));
    let sig = term::Signals::install()?;
    let mut out = io::stdout();
    out.write_all(wezterm::strip_marker().as_bytes())?;
    out.flush()?;
    crossterm::terminal::enable_raw_mode()?;
    let ran = (|| -> io::Result<()> {
        write!(out, "\x1b[?1049h\x1b[?25l\x1b[2J{MOUSE_ON}")?;
        out.flush()?;
        let mut term = Terminal::new(CrosstermBackend::new(io::stdout()))?;
        app.run(&mut term, &sig)
    })();
    let _ = write!(out, "{MOUSE_OFF}\x1b[0m\x1b[?25h\x1b[?1049l");
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
        assert_eq!((popup_width(Some(272)), popup_width(Some(80)), popup_width(None)), ("100".into(), "80".into(), "75%".into()));
        let c = menu_command(&t, "/dev/ttys999", Some(std::path::Path::new("/x/agent-ui")), Some("parked"), "100");
        assert_eq!(
            args(&c),
            ["-u", "-L", "priv", "display-popup", "-c", "/dev/ttys999", "-E", "-w", "100", "-h", "75%", "-B",
                "/x/agent-ui", "--socket", "priv", "menu", "--client", "/dev/ttys999", "--tab", "parked"]
        );
        // No binary path: the Python popup (prefix q's line, with its title).
        let c = menu_command(&Tmux::default(), "/dev/ttys999", None, None, "75%");
        let a = args(&c);
        assert_eq!(&a[a.len() - 7..a.len() - 3], ["-T", " agents ", "/bin/dash", "-c"]);
        assert!(!a.contains(&"-B".to_string()));
        assert!(a[a.len() - 2].ends_with("agent-roster.py") && a[a.len() - 1] == "/dev/ttys999");
    }

    /// An App over a tmux socket that does not exist (no command here can
    /// start a server) and no client (so no action runs: click() says "no
    /// tmux client"), showing an empty 34x30 frame.
    fn app() -> App {
        let tmux = Tmux::with_socket(Some(Socket::Name(format!("agentui-u3-none-{}", std::process::id()))));
        let link = WeztermLink::new("/nonexistent/wezterm".into(), None, None, None);
        let mut a = App::new(tmux, link, Notices::default());
        a.shown = layout(&a.view, 34, 30, &Opts::default());
        a
    }

    #[test]
    fn clicks_act_once_and_hand_focus_back() {
        let mut a = app();
        // A click on the parked row (the last one) acts on the release-less press.
        a.handle(b"\x1b[<0;3;30M\x1b[<0;3;30m");
        assert_eq!((a.focus_backs, a.last_act.clone()), (1, Some(Target::Stash)));
        // Motion and releases alone do nothing.
        a.last_act = None;
        a.handle(b"\x1b[<32;3;20M\x1b[<0;3;20m");
        assert_eq!((a.focus_backs, a.last_act.clone()), (1, None));
    }

    /// `⏸ N parked` runs exactly prefix h's line (tmux.conf.tmpl, read when
    /// the source tree is there), the client in place of `#{client_name}`.
    #[test]
    fn stash_click_is_prefix_h() {
        let t = Tmux::with_socket(Some(Socket::Name("priv".into())));
        let c = stash_command(&t, "/dev/ttys999", std::path::Path::new("/s"));
        assert_eq!(args(&c), ["-u", "-L", "priv", "run-shell", "'/s/stash.sh' unstash '' '/dev/ttys999'"]);
        let conf = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../tmux.conf.tmpl");
        if let Ok(conf) = std::fs::read_to_string(conf) {
            let bind = conf.lines().find(|l| l.starts_with("bind-key h ")).expect("prefix h");
            assert_eq!(bind, "bind-key h run-shell \"#{HOME}/.config/tmux/scripts/stash.sh unstash '' '#{client_name}'\"");
        }
    }

    /// A plain right press on an agent row parks that window: `stash.sh
    /// stash <win>` through run-shell -b (the line unit-tested here; the
    /// server is missing, so nothing runs), `parking <label>…` in the
    /// footer, then `parked <label>` once a snapshot shows it in the stash.
    /// Anywhere else, or with a modifier, a right press only hands focus back.
    #[test]
    fn right_click_parks_an_agent_row() {
        let mut a = app();
        a.core.snapshot = Snapshot::parse(&[
            row("main", 1, "@1", &[("state", "idle"), ("summary", "Quiet one")]),
            row("main", 2, "@2", &[]),
            "\x1fclient\x1f/dev/ttys999\x1f@2\x1fmain".into(),
        ].join("\n"));
        a.view = build_view(&mut a.core, Some("/dev/ttys999"));
        a.shown = layout(&a.view, 34, 30, &Opts::default());
        let y = a.shown.agents.start + a.shown.rows[a.shown.agents.clone()].iter().position(|r| r.text().contains("Quiet one")).unwrap();
        let (x, y1) = (5, y + 1);
        a.handle(format!("\x1b[<2;{x};{y1}M\x1b[<2;{x};{y1}m").as_bytes());
        assert_eq!(a.focus_backs, 1);
        assert_eq!((a.last_act.clone(), a.last_park.clone()), (None, Some(("@1".into(), "main".into()))));
        assert_eq!(a.msg.as_ref().map(|(m, _)| m.as_str()), Some("parking Quiet one…"));
        let stash = a.actions.scripts.join("stash.sh");
        assert_eq!(a.actions.park_line("@1"), format!("{} 'stash' '@1'", sh_quote(&stash.to_string_lossy())));
        // The window shows up in the stash: said so, and no longer tracked.
        a.core.snapshot = Snapshot::parse(&row("stash", 1, "@1", &[("stash_origin", "main")]));
        a.check_park();
        assert_eq!(a.msg.as_ref().map(|(m, _)| m.as_str()), Some("parked Quiet one"));
        assert!(a.parking.is_none());
        // Right on the parked line, ctrl-right on the row: focus only.
        a.last_park = None;
        a.handle(b"\x1b[<2;3;30M\x1b[<2;3;30m");
        a.handle(format!("\x1b[<18;{x};{y1}M").as_bytes());
        assert_eq!((a.focus_backs, a.last_park.clone(), a.last_act.clone()), (3, None, None));
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
    fn option_v_is_taken_out_of_typing() {
        assert_eq!(take_scope_keys(b"c\x1bvd\x1b[v\x1bs\x1bV"), (b"cd\x1b[v\x1bs".to_vec(), 2));
        assert_eq!(take_scope_keys(b"\x1b\x1bv"), (b"\x1b".to_vec(), 1));
        assert_eq!(take_scope_keys(b"v\x1b"), (b"v\x1b".to_vec(), 0));
    }

    /// Option-V and a title click toggle; the set fails here (no server),
    /// so the scope stays and the footer says why. Focus still goes back.
    #[test]
    fn scope_toggle_failure_keeps_the_scope() {
        let mut a = app();
        a.handle(b"\x1bv");
        assert_eq!(a.focus_backs, 1);
        assert_eq!(a.msg.as_ref().map(|(m, _)| m.as_str()), Some("couldn't set @strip_scope"));
        assert_eq!(a.view.scope.name(), "all");
        // CSI v is not the toggle: plain typing.
        a.msg = None;
        a.handle(b"\x1b[v");
        assert_eq!(a.focus_backs, 2);
        assert!(a.msg.as_ref().is_none_or(|(m, _)| !m.contains("strip_scope")), "{:?}", a.msg);
        // The title is a toggle only once the scope is a thing.
        assert_eq!(a.shown.target(2, 0), None);
        a.view.scope.spaces = true;
        a.shown = layout(&a.view, 34, 30, &Opts::default());
        a.handle(b"\x1b[<0;3;1M\x1b[<0;3;1m");
        assert_eq!((a.last_act.clone(), a.focus_backs), (Some(Target::Scope), 3));
        assert_eq!(a.msg.as_ref().map(|(m, _)| m.as_str()), Some("couldn't set @strip_scope"));
    }

    /// The cadence: the fast tick only while something drawn pulses (or
    /// with no notice fd); the safety net otherwise.
    #[test]
    fn cadence_follows_the_pulse() {
        let mut a = app();
        assert_eq!(a.cadence(), TICK); // no notice fd: poll
        a.notices = Notices::register(&[format!("com.mackhaymond.agent-ui.test.{}.cad", std::process::id())]);
        if a.notices.fd().is_none() {
            return; // no notify(3) here
        }
        assert_eq!(a.cadence(), FALLBACK);
        let snap = Snapshot::parse(&[row("main", 1, "@1", &[("state", "running")]),
            "\x1fclient\x1f/dev/ttys999\x1f@2\x1fmain".into(), row("main", 2, "@2", &[])].join("\n"));
        let c = crate::collate::Collator::new("C");
        let args = BuildArgs { client: Some("/dev/ttys999"), now: 0.0, collator: &c, git: None, log: vec![], watcher_age: None };
        a.view = ViewModel::build(&snap, args);
        assert!(layout::pulses(&a.view));
        assert_eq!(a.cadence(), TICK);
    }

    /// The client process: a pid seen, then unlisted, ends the sidebar only
    /// once the process is dead. tmux 3.7 drops suspended (prefix C-z) and
    /// locked clients from list-clients while they live: those stay. A
    /// failed (empty) snapshot proves nothing; a client never seen never
    /// ends the sidebar.
    #[test]
    fn a_vanished_client_ends_the_sidebar() {
        let listed = |pid: u32| {
            Snapshot::parse(&[row("main", 1, "@1", &[]), format!("\x1fclient\x1f/dev/ttys999\x1f@1\x1fmain\x1f\x1f\x1f\x1f{pid}")].join("\n"))
        };
        let unlisted = || Snapshot::parse(&row("main", 1, "@1", &[]));
        // A live client (this test process) that drops out of the listing:
        // suspended or locked. Never gone.
        let mut a = app();
        a.link.client = Some("/dev/ttys999".into());
        let me = std::process::id();
        a.core.snapshot = listed(me);
        a.check_client();
        assert_eq!((a.gone, a.client_pid), (false, Some(me as i32)));
        for snap in [Snapshot::default(), unlisted(), unlisted()] {
            a.core.snapshot = snap;
            a.check_client();
            assert!(!a.gone);
        }
        // Back in the listing (resumed / unlocked): still watched, still here.
        a.core.snapshot = listed(me);
        a.check_client();
        assert!(!a.gone && a.watch.is_some());
        // A client that really exited: unlisted and dead → gone.
        let mut child = Command::new("/bin/sleep").arg("5").spawn().unwrap();
        let mut a = app();
        a.link.client = Some("/dev/ttys999".into());
        a.core.snapshot = listed(child.id());
        a.check_client();
        assert!(!a.gone);
        child.kill().unwrap();
        child.wait().unwrap();
        a.core.snapshot = unlisted();
        a.check_client();
        assert!(a.gone);
        // Never seen: never gone.
        let mut b = app();
        b.link.client = Some("/dev/ttys999".into());
        b.core.snapshot = Snapshot::parse(&row("main", 1, "@1", &[]));
        b.check_client();
        assert!(!b.gone);
        // Alone in the tab (unpinned): gone.
        b.link.alone = true;
        b.check_client();
        assert!(b.gone);
    }

    /// `@space_home` windows: never an agent row (even the client's, with
    /// `@spaces` off, the classic rule) and never counted, whatever they carry.
    #[test]
    fn home_windows_never_show() {
        let mut a = app();
        a.core.snapshot = Snapshot::parse(&[
            row("main", 0, "@9", &[("home", "1"), ("state", "running"), ("summary", "Home")]),
            row("main", 1, "@1", &[("state", "idle"), ("summary", "Agent")]),
            "\x1fclient\x1f/dev/ttys999\x1f@9\x1fmain".into(),
        ].join("\n"));
        hide_homes(&mut a.core.snapshot);
        for scope in ["", "all", "space"] {
            a.core.snapshot.clients[0].strip_scope = scope.into();
            let v = build_view(&mut a.core, Some("/dev/ttys999"));
            assert_eq!(v.counts.total(), 1, "{scope}");
            assert!(v.agents().all(|x| x.window_id != "@9"), "{scope}");
            let f = layout(&v, 34, 30, &Opts::default());
            assert!(!f.rows.iter().any(|r| r.text().contains("Home") || r.text().contains("zsh")), "{scope}");
            // Its space is still the client's.
            assert!(v.spaces.iter().any(|s| s.name == "main" && s.is_current));
        }
    }

    #[test]
    fn args_parse() {
        assert_eq!(parse_size("34x58"), Some((34, 58)));
        for bad in ["34", "x58", "0x5", "34x-1", "axb"] {
            assert_eq!(parse_size(bad), None, "{bad}");
        }
    }
}
