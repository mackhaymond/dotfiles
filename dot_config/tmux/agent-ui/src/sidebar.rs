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
//!   stash <win>`), and `PARKED N` opens prefix h's stash picker
//!   ([`stash_command`]). Every click hands
//!   focus straight back to the tmux pane first (Strip.handle → focus_back),
//!   and any key that lands here is forwarded there. The wheel does nothing:
//!   nothing scrolls.
//! - **Scope.** `@spaces` / `@strip_scope` (globals, read in the same
//!   snapshot call): "space" shows only the client's session; "all" every
//!   session
//!   ([`crate::view::Scope`]). Option-V (ESC v / V), the one key NOT
//!   forwarded, or a click on the header title (once the scope is a thing)
//!   flips and persists it, then posts [`notice::FOCUS_NOTICE`] so every
//!   other sidebar follows at once.
//! - **Cadence: told, not polling.** The loop sleeps in poll(2) on stdin,
//!   the signal pipe, the Darwin notice fd ([`notice`]: agent-state and focus
//!   notices) and the client's exit watch. A notice schedules one snapshot
//!   [`notice::DEBOUNCE`] later (a burst is one refresh); a click that moves
//!   refreshes at once. The timer is only a safety net: [`FALLBACK`] (5 s),
//!   or [`TICK`] (0.5 s) when there is no notice fd. A redraw happens only
//!   when the rendered buffer changed.
//! - **The spinner.** While the frame on screen shows a working agent's
//!   spinner ([`Frame::spin`]) the loop also wakes every [`SPIN_EVERY`]
//!   (8 fps) to lay the SAME view out again with the next frame: no
//!   snapshot, no tmux call, and ratatui's diff writes only the spinner
//!   cells. Nothing working on screen: no such wakeup at all.
//! - **Key tables.** tmux posts the focus notice on ENTERING a key table
//!   (prefix, ⌥W's `jump`) but nothing on leaving it, so while a refresh
//!   shows the client outside `root` the sidebar re-reads just
//!   `#{client_key_table}` every 150 ms, for at most 10 s per table entered
//!   (later notices in the same table never extend it), and refreshes once
//!   when it changes ([`status::KeyPoll`]). In `root`: no polling.
//! - **Lifecycle.** The marker CMD+B finds the pane by is written once,
//!   before the alternate screen; SIGHUP / SIGTERM (CMD+B's kill-pane) or a
//!   closed tty end it quietly, with the terminal restored.
//! - **The whole UI.** While any sidebar is open the tmux status line is
//!   hidden and its content drawn here ([`status`]: the usage panel with
//!   the meters, the stale save, the prefix / ⌥W / selection marks; the
//!   spaces buttons); the
//!   registry behind `@sidebar_open` restores it when the last one leaves.
//!   Agent and shell rows carry their lifetime letters (`@agent_key`, one
//!   pool shared with the menu and ⌥W, persisted like it).
//! - **Toasts.** scripts/notify.sh, the replacement for `tmux
//!   display-message` while a sidebar is open, writes `@toast` and posts the
//!   focus notice; the refresh that follows reads it in the one snapshot and
//!   draws the live ones as cards stacked over the top of the frame
//!   ([`layout`]'s Toasts). Each stays until its deadline; the loop wakes
//!   at the earliest one to draw the frame without it (no polling); a click
//!   on a card dismisses that toast here only. The registry
//!   also keeps `@sidebar_clients` (which clients have a sidebar), so the
//!   script knows when to fall back to `display-message`.
//! - **`--once WxH`** prints one frame in ANSI colour and exits: no
//!   alternate screen, no marker, no mouse (screenshots, tests).

pub mod activity;
mod input;
pub mod layout;
pub mod notice;
pub mod status;
mod term;

pub use layout::{layout, Frame, Opts, Row, Seg, Target};

use crate::actions::{scripts_dir, sh_quote, watcher_age, Actions};
use crate::menu::letters::{self, KeyMemo, Write as KeyWrite};
use crate::proc;
use crate::text;
use crate::tmux::{Snapshot, Socket, Tmux, HOLD};
use crate::view::BuildArgs;
use crate::view::{Core, ViewModel};
use crate::wezterm::{self, WeztermLink, MOUSE_OFF, MOUSE_ON};
use notice::Notices;
use status::{Bar, Keeper, KeyPoll, Registry};
use ratatui::backend::CrosstermBackend;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::Terminal;
use std::collections::HashSet;
use std::io::{self, Stdout, Write};
use std::path::PathBuf;
use std::process::{Child, Command, ExitCode};
use std::time::{Duration, Instant};

/// The fast safety-net tick, with no notice fd at all.
pub const TICK: Duration = Duration::from_millis(500);
/// The spinner's frame time (8 fps), only while one is on screen.
pub const SPIN_EVERY: Duration = Duration::from_millis(125);
/// The safety-net poll when notices tell us about every change.
pub const FALLBACK: Duration = Duration::from_secs(5);
/// The tmux global holding the scope (`space` | `all`; unset: by `@spaces`).
pub const SCOPE_OPTION: &str = "@strip_scope";
/// agent-roster.py `ESC_WAIT`: how long a cut-off report waits for its rest.
const ESC_WAIT: Duration = Duration::from_millis(25);
/// How long a footer message stays.
const SAY_FOR: f64 = 4.0;

const USAGE: &str = "usage: agent-ui sidebar [--client <tty>] [--tmux-pane <id>] [--wezterm <path>]
       agent-ui sidebar --once <W>x<H> [--client <tty>]
       agent-ui sidebar --repair [-v]";

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
    if args.iter().any(|a| a == "--repair") {
        // Dead sidebars out of @sidebar_pids / @sidebar_open; the status line
        // hidden while any is open (again, after a reload's `status on`),
        // back once none is. For tmux hooks (client-attached, a reload): two
        // tmux calls, no output, always status 0, since a run-shell hook shows
        // output or a failure in view mode. `-v` prints the count.
        let n = Registry::new(&tmux).repair();
        if args.iter().any(|a| a == "-v") {
            println!("{}", n.map_or("?".into(), |n| n.to_string()));
        }
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
    // The status block from the RAW snapshot (a space's home is its shell 1).
    let bar = Bar::build(&core.snapshot, client.as_deref());
    hide_homes(&mut core.snapshot);
    // Letters as the live sidebar would assign them; `--once` never writes.
    let (mut view, _) = build_view(&mut core, client.as_deref(), None);
    activity::Activity::home().apply(&mut view);
    let frame = layout(&view, w, h, &Opts { msg: None, bar, spin: 0, now: None });
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
///
/// Letters: [`letters::of_view`], THE assignment the agents menu runs too, on
/// the unscoped view; every agent row, shell row and NEEDS YOU card gets its
/// letter.
/// `keys`: the live sidebar's in-flight `@agent_key` writes; the writes
/// returned are the ones to send (None: `--once`, nothing to send).
fn build_view(core: &mut Core, client: Option<&str>, keys: Option<&mut KeyMemo>) -> (ViewModel, Vec<KeyWrite>) {
    let now = text::now();
    let args = BuildArgs { client, now, collator: &core.collator, git: None, log: Vec::new(), watcher_age: watcher_age() };
    let mut view = ViewModel::build_with(&core.snapshot, args, &mut core.slots);
    let persist = keys.is_some();
    let assigned = letters::of_view(&core.snapshot, &view, keys);
    let letter = |a: &mut crate::view::Agent| a.letter = assigned.letters.get(&a.window_id).copied();
    // Agents and shells alike (one pool): a shell row shows its letter too.
    view.spaces.iter_mut().flat_map(|s| s.agents.iter_mut().chain(s.plain.iter_mut())).for_each(letter);
    view.needs.iter_mut().for_each(|n| letter(&mut n.agent));
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
    (view, if persist { assigned.writes } else { Vec::new() })
}

/// The menu popup's `-w`: tmux.conf's menu cap (at most 100 columns, the
/// whole client when narrower). display-popup neither expands formats in
/// `-w` nor clamps a width wider than the client (it fails), so the client's
/// width is asked for first; unknown → the old 75%.
pub fn popup_width(client_cols: Option<u16>) -> String {
    match client_cols {
        Some(c) if c > 0 => c.min(100).to_string(),
        _ => "75%".into(),
    }
}

/// `… more` / `+K` / the idle fold: the agents menu (prefix q's) as a popup on the sidebar's
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

/// `PARKED N`: exactly what prefix h runs (tmux.conf.tmpl `bind-key h
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
    /// What the hidden status line showed, from the last snapshot.
    bar: Bar,
    /// The `@agent_key` writes in flight (the live sidebar persists the
    /// letters it assigns, exactly as the menu does).
    keys: KeyMemo,
    persist: bool,
    /// The open-sidebar registry (`@sidebar_open`), once this sidebar is in it.
    registry: Option<Registry>,
    /// When the registry was last repaired from a refresh (at most every 2 s).
    repaired_at: Option<Instant>,
    /// Runs the status line's `#()` jobs while it is hidden.
    keeper: Keeper,
    /// Re-reads the client's key table while it is not `root` ([`KeyPoll`]).
    keypoll: KeyPoll,
    /// How many cheap key-table reads ran (observable state for the tests).
    key_reads: u32,
    /// The spinner's clock: its frame is the [`SPIN_EVERY`]s since this.
    started: Instant,
    /// The toasts a click dismissed here ([`status::Toast::id`]): never
    /// drawn again. Only ids `@toast` still holds are kept (at most
    /// [`DISMISSED_MAX`]).
    dismissed: Vec<status::ToastId>,
    /// The working agents' activity blurbs ([`activity`]), cached.
    activity: activity::Activity,
}

/// How many dismissed toast ids a sidebar remembers at most.
const DISMISSED_MAX: usize = 8;

impl App {
    fn new(tmux: Tmux, link: WeztermLink, notices: Notices) -> App {
        let mut core = Core::new(tmux.clone());
        let (view, _) = build_view(&mut core, None, None);
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
            bar: Bar::default(),
            keys: KeyMemo::default(),
            persist: false,
            registry: None,
            repaired_at: None,
            keeper: Keeper::default(),
            keypoll: KeyPoll::default(),
            key_reads: 0,
            started: Instant::now(),
            dismissed: Vec::new(),
            // (Unit tests never read the real transcripts.)
            activity: if cfg!(test) { activity::Activity::new("/nonexistent".into()) } else { activity::Activity::home() },
        }
    }

    /// The bar to draw: the snapshot's, less the toasts dismissed here.
    fn shown_bar(&self) -> Bar {
        let mut bar = self.bar.clone();
        bar.toasts.retain(|t| !self.dismissed.contains(&t.id()));
        bar
    }

    /// A toast dismissed here: never drawn again in this sidebar.
    fn dismiss(&mut self, id: status::ToastId) {
        if !self.dismissed.contains(&id) {
            self.dismissed.push(id);
        }
        // Bounded however the snapshot goes (notify.sh keeps 4 records).
        let excess = self.dismissed.len().saturating_sub(DISMISSED_MAX);
        self.dismissed.drain(..excess);
    }

    /// Drop the dismissals of toasts `@toast` no longer holds: only against
    /// what the snapshot actually read (its first client row carries the
    /// global option; a failed or timed-out snapshot has none, so it forgets
    /// nothing), every record unfiltered (a client briefly unresolved must
    /// not bring a dismissed toast back).
    fn forget_gone_toasts(&mut self) {
        let Some(st) = self.core.snapshot.clients.first().map(|c| &c.status) else { return };
        let held = status::Toast::parse_all(&st.toast);
        self.dismissed.retain(|id| held.iter().any(|t| t.id() == *id));
    }

    /// When the first toast on screen expires (a redraw then, without it;
    /// the next one's deadline is that frame's).
    fn toast_due(&self, now: Instant) -> Option<Instant> {
        let left = self.shown.toast_until? as f64 / 1000.0 - text::now();
        Some(now + Duration::from_secs_f64(left.max(0.0)) + Duration::from_millis(5))
    }

    /// The spinner frame at `now`.
    fn spin_at(&self, now: Instant) -> usize {
        (now.saturating_duration_since(self.started).as_millis() / SPIN_EVERY.as_millis()) as usize
    }

    /// When the spinner next turns, if the frame on screen shows one.
    fn spin_due(&self, now: Instant) -> Option<Instant> {
        if !self.shown.spin {
            return None;
        }
        let next = self.spin_at(now) as u32 + 1;
        Some(self.started + SPIN_EVERY * next)
    }

    fn say(&mut self, text: impl Into<String>) {
        self.msg = Some((text.into(), text::now() + SAY_FOR));
    }

    /// Strip.refresh / load: reap popups, one snapshot, re-resolve the
    /// client if it went missing, then the view; then whether the client
    /// (or the tab) is gone. The key table seen starts or stops the
    /// key-table poll ([`KeyPoll::seen`]).
    fn refresh(&mut self) {
        self.children.retain_mut(|c| matches!(c.try_wait(), Ok(None)));
        self.core.snapshot = self.core.tmux.snapshot();
        self.link.on_snapshot(&self.core.snapshot, text::now());
        self.bar = Bar::build(&self.core.snapshot, self.link.client.as_deref());
        self.forget_gone_toasts();
        let table = self.client_key_table();
        self.keypoll.seen(&table, Instant::now());
        hide_homes(&mut self.core.snapshot);
        let keys = if self.persist { Some(&mut self.keys) } else { None };
        let (mut view, writes) = build_view(&mut self.core, self.link.client.as_deref(), keys);
        // The working agents' live activity, from their transcripts (cached;
        // a file is read only when it changed).
        self.activity.apply(&mut view);
        self.view = view;
        if !writes.is_empty() {
            crate::menu::store_keys(&self.core.tmux, writes);
        }
        self.check_registry();
        self.check_client();
        self.check_park();
    }

    /// The client's key table as the last snapshot shows it ("" unknown).
    fn client_key_table(&self) -> String {
        let row = self.link.client.as_deref().and_then(|c| self.core.snapshot.client(c));
        row.map(|c| c.status.key_table.clone()).unwrap_or_default()
    }

    /// The key-table poll's read, when due: ONE `display-message` of
    /// `#{client_key_table}`; a change (back to `root`, say) refreshes once.
    fn poll_key_table(&mut self, now: Instant) {
        if self.keypoll.due().is_none_or(|d| now < d) {
            return;
        }
        self.key_reads += 1;
        let got = self.link.client.as_deref().and_then(|c| {
            let o = self.core.tmux.run(&status::key_table_command(c)).ok().filter(|o| o.ok())?;
            Some(o.stdout.trim().to_string())
        });
        if self.keypoll.read(got.as_deref(), now) == status::KeyStep::Refresh {
            self.refresh();
        }
    }

    /// The cheap check on every refresh: a registry out of step (this pid
    /// missing, a dead one listed, a count that disagrees) is repaired at
    /// once (re-registering this sidebar), at most every 2 s; and the
    /// status line's jobs run if this sidebar is the one that runs them
    /// ([`Keeper`]). Needs a client row to read the options from.
    fn check_registry(&mut self) {
        let Some(reg) = &self.registry else { return };
        let me = std::process::id() as i32;
        let Some(st) = self.core.snapshot.clients.first().map(|c| c.status.clone()) else { return };
        let now = Instant::now();
        let client = self.link.client.as_deref();
        if status::needs_repair(&st, me, &status::client_word(client), &status::is_sidebar)
            && self.repaired_at.is_none_or(|t| now.duration_since(t) >= Duration::from_secs(2))
        {
            self.repaired_at = Some(now);
            reg.open(me, client);
        }
        let live = status::parse_pids(&st.sidebar_pids).into_iter().filter(|&p| p == me || status::is_sidebar(p)).min();
        let leader = live.is_none_or(|p| p == me);
        self.keeper.tick(&self.core.tmux, leader, now);
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
        if let Target::Toast(id) = t {
            // Here only: the other sidebars keep showing it.
            self.dismiss(id);
            return;
        }
        let Some(client) = self.link.client.clone() else {
            self.say("no tmux client");
            return;
        };
        let snap = &self.core.snapshot;
        let moved = match t {
            Target::Go { win, session } | Target::Shell { win, session } => {
                self.actions.go(snap, &client, &win, &session).map(|_| true)
            }
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
            Target::Restart | Target::Scope | Target::Toast(_) => Ok(false),
        };
        match moved {
            Ok(true) => self.refresh(),
            Ok(false) => {}
            Err(m) => self.say(m),
        }
    }

    /// The safety-net poll interval ([`TICK`] / [`FALLBACK`]).
    fn cadence(&self) -> Duration {
        if self.notices.fd().is_none() {
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
        let spin = self.spin_at(Instant::now());
        // Ages at this frame's clock (no tmux call), so a spinner frame
        // between snapshots never shows a stale one.
        let opts = Opts { msg, bar: self.shown_bar(), spin, now: Some(now) };
        let frame = layout(&self.view, size.width, size.height, &opts);
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
            // Only while the client is in a key table other than root.
            if let Some(k) = self.keypoll.due() {
                wake = wake.min(k);
            }
            // Only while a spinner is on screen: its next frame.
            if let Some(s) = self.spin_due(now) {
                wake = wake.min(s);
            }
            // Only while a toast is on screen: its expiry.
            if let Some(t) = self.toast_due(now) {
                wake = wake.min(t);
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
            } else {
                self.poll_key_table(now);
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

/// This sidebar's place in the registry: listed (`@sidebar_open` up, the
/// status line hidden) on creation, unlisted (and the status line back once
/// no sidebar is open) on drop.
struct Open {
    reg: Registry,
    me: i32,
}

impl Open {
    /// `client`: the pinned one (`--client`), if any; a found one is
    /// recorded on the first refresh that sees it.
    fn new(reg: Registry, client: Option<&str>) -> Open {
        let me = std::process::id() as i32;
        reg.open(me, client);
        Open { reg, me }
    }
}

impl Drop for Open {
    fn drop(&mut self) {
        self.reg.close(self.me);
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
    let mut app = App::new(tmux.clone(), link, Notices::register(&notice::names()));
    app.persist = true;
    let sig = term::Signals::install()?;
    // In the registry (status line hidden) from here until every exit path
    // drops the guard: a normal return, the never-alone exit, SIGHUP/SIGTERM
    // (the loop returns), an error, or a panic unwinding.
    let _open = Open::new(Registry::new(&tmux), arg_after(args, "--client"));
    app.registry = Some(Registry::new(&tmux));
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
        // A click on the parked row (the dock's, over its bottom cap) acts on
        // the release-less press.
        let y = a.shown.parked.start + 1;
        a.handle(format!("\x1b[<0;3;{y}M\x1b[<0;3;{y}m").as_bytes());
        assert_eq!((a.focus_backs, a.last_act.clone()), (1, Some(Target::Stash)));
        // Motion and releases alone do nothing.
        a.last_act = None;
        a.handle(b"\x1b[<32;3;20M\x1b[<0;3;20m");
        assert_eq!((a.focus_backs, a.last_act.clone()), (1, None));
    }

    /// `PARKED N` runs exactly prefix h's line (tmux.conf.tmpl, read when
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
        a.view = build_view(&mut a.core, Some("/dev/ttys999"), None).0;
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

    /// The cadence: the fast tick only with no notice fd, the safety net
    /// otherwise, working agents or not. The spinner's own wakeup exists
    /// only while the frame on screen draws one, at the next 125 ms step,
    /// and each step is the next spinner frame.
    #[test]
    fn cadence_and_the_spinner() {
        let mut a = app();
        assert_eq!(a.cadence(), TICK); // no notice fd: poll
        assert_eq!(a.spin_due(Instant::now()), None); // nothing on screen spins
        let snap = Snapshot::parse(&[row("main", 1, "@1", &[("state", "running"), ("summary", "Busy")]),
            "\x1fclient\x1f/dev/ttys999\x1f@2\x1fmain".into(), row("main", 2, "@2", &[])].join("\n"));
        let c = crate::collate::Collator::new("C");
        let args = BuildArgs { client: Some("/dev/ttys999"), now: 0.0, collator: &c, git: None, log: vec![], watcher_age: None };
        a.view = ViewModel::build(&snap, args);
        a.shown = layout(&a.view, 34, 30, &Opts::default());
        assert!(a.shown.spin);
        let now = a.started + Duration::from_millis(300);
        assert_eq!(a.spin_at(now), 2);
        assert_eq!(a.spin_due(now), Some(a.started + Duration::from_millis(375)));
        let frame = |spin| layout(&a.view, 34, 30, &Opts { spin, ..Opts::default() });
        let (f0, f1) = (frame(0), frame(1));
        let y = f0.rows.iter().position(|r| r.text().contains("Busy")).unwrap();
        assert!(f0.rows[y].text().contains("⠋ working") && f1.rows[y].text().contains("⠙ working"));
        // Only the spinner changed.
        let diff: Vec<usize> = (0..f0.rows.len()).filter(|&k| f0.rows[k] != f1.rows[k]).collect();
        assert_eq!(diff, [y]);
        a.notices = Notices::register(&[format!("com.mackhaymond.agent-ui.test.{}.cad", std::process::id())]);
        if a.notices.fd().is_some() {
            assert_eq!(a.cadence(), FALLBACK);
        }
        // Idle again: no spinner, no wakeup.
        let snap = Snapshot::parse(&[row("main", 1, "@1", &[("state", "idle"), ("summary", "Busy")])].join("\n"));
        let args = BuildArgs { client: None, now: 0.0, collator: &c, git: None, log: vec![], watcher_age: None };
        a.view = ViewModel::build(&snap, args);
        a.shown = layout(&a.view, 34, 30, &Opts::default());
        assert_eq!((a.shown.spin, a.spin_due(now)), (false, None));
    }

    /// The loop's side of the key-table poll: nothing read while the poll is
    /// idle or not yet due; a due read that fails (no client here) refreshes
    /// once, which sees no table and stops it: no further reads.
    #[test]
    fn the_key_table_poll_reads_only_when_due() {
        let mut a = app();
        let t0 = Instant::now();
        a.poll_key_table(t0);
        assert_eq!((a.key_reads, a.keypoll.polling()), (0, false));
        a.keypoll.seen("prefix", t0);
        a.poll_key_table(t0);
        assert_eq!(a.key_reads, 0); // not due yet
        let due = a.keypoll.due().unwrap();
        a.poll_key_table(due);
        assert_eq!((a.key_reads, a.keypoll.polling()), (1, false));
        a.poll_key_table(due + Duration::from_secs(1));
        assert_eq!(a.key_reads, 1);
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
            let v = build_view(&mut a.core, Some("/dev/ttys999"), None).0;
            assert_eq!(v.counts.total(), 1, "{scope}");
            assert!(v.agents().all(|x| x.window_id != "@9"), "{scope}");
            let f = layout(&v, 34, 30, &Opts::default());
            assert!(!f.rows.iter().any(|r| r.text().contains("Home") || r.text().contains("zsh")), "{scope}");
            // Its space is still the client's.
            assert!(v.spaces.iter().any(|s| s.name == "main" && s.is_current));
        }
    }

    /// The sidebar and the agents menu assign the SAME letters from the same
    /// snapshot (one shared function, `letters::of_view`): unique across
    /// agents AND shells (one pool, each space's agents then its shells),
    /// home windows never lettered, a duplicate going to the lower window
    /// id, a parked window's stale claim unset. The writes are the menu's, sent
    /// once, and never again once tmux shows them.
    #[test]
    fn letters_are_shared_with_the_menu_and_persisted_once() {
        let cl = "/dev/ttys999";
        let raw = Snapshot::parse(&[
            row("main", 0, "@9", &[("home", "1"), ("state", "running")]),
            row("main", 1, "@1", &[("state", "idle"), ("key", "s")]),
            row("main", 2, "@2", &[("state", "running")]),
            row("main", 3, "@6", &[]),
            row("work", 1, "@3", &[("state", "needs-input")]),
            row("work", 2, "@4", &[("state", "idle"), ("key", "s")]),
            row("stash", 1, "@5", &[("state", "idle"), ("key", "a")]),
            // (@spaces on: shells are lettered too.)
            format!("\x1fclient\x1f{cl}\x1f@2\x1fmain\x1fon\x1f\x1f"),
        ].join("\n"));
        let menu = crate::menu::rows::Data::build(&raw, Some(cl), 0.0, &crate::collate::Collator::new("C"), None, None);
        let mut a = app();
        a.core.snapshot = raw.clone();
        hide_homes(&mut a.core.snapshot);
        let mut keys = KeyMemo::default();
        let (v, writes) = build_view(&mut a.core, Some(cl), Some(&mut keys));
        let got: Vec<(String, Option<char>)> = v.agents().map(|x| (x.window_id.clone(), x.letter)).collect();
        assert_eq!(got, [("@1".into(), Some('s')), ("@2".into(), Some('a')), ("@3".into(), Some('d')), ("@4".into(), Some('f'))]);
        for (id, l) in &got {
            assert_eq!(*l, menu.letter(id), "{id}: the menu agrees");
        }
        // main's shell: its letter from the same pool, after every agent.
        let shell = v.spaces.iter().flat_map(|s| s.plain.iter()).find(|x| x.window_id == "@6").map(|x| x.letter);
        assert_eq!((shell, menu.letter("@6")), (Some(Some('g')), Some('g')));
        assert_eq!((menu.letter("@9"), menu.letter("@5")), (None, None));
        // NEEDS YOU cards carry the same letter.
        assert_eq!(v.needs.iter().map(|n| n.agent.letter).collect::<Vec<_>>(), [Some('d')]);
        let want: Vec<KeyWrite> =
            vec![("@2".into(), Some('a')), ("@3".into(), Some('d')), ("@4".into(), Some('f')), ("@6".into(), Some('g')), ("@5".into(), None)];
        assert_eq!((&writes, &menu.key_writes), (&want, &want));
        // Idempotent: the same snapshot again sends nothing (in flight)...
        assert!(build_view(&mut a.core, Some(cl), Some(&mut keys)).1.is_empty());
        // ...and once tmux shows the writes, nothing either, the memo settled.
        for w in a.core.snapshot.windows.iter_mut() {
            if let Some((_, k)) = want.iter().find(|(id, _)| *id == w.id) {
                w.key = k.map(String::from).unwrap_or_default();
            }
        }
        let (v2, again) = build_view(&mut a.core, Some(cl), Some(&mut keys));
        assert!(again.is_empty() && keys.is_empty());
        assert_eq!(v2.agents().map(|x| x.letter).collect::<Vec<_>>(), [Some('s'), Some('a'), Some('d'), Some('f')]);
        // `--once` (no memo) computes them but returns nothing to send.
        assert!(build_view(&mut a.core, Some(cl), None).1.is_empty());
    }

    /// A left click on a toast card dismisses THAT toast in this sidebar
    /// only (no tmux call; focus still goes back); a newer toast shows
    /// again. While cards are on screen the loop wakes at the earliest
    /// deadline, and only then. The dismissals are bounded and forget the
    /// toasts `@toast` no longer holds.
    #[test]
    fn a_click_dismisses_the_toast_here() {
        use status::{Kind, Toast};
        let mut a = app();
        let t0 = (text::now() * 1000.0) as i64;
        let toast = |seq: u64, at_ms: i64, ttl: f64| Toast {
            seq,
            at_ms,
            kind: Kind::Ok,
            ttl,
            client: String::new(),
            text: format!("parked {seq}"),
            title: None,
        };
        let seqs = |a: &App| a.shown_bar().toasts.iter().map(|t| t.seq).collect::<Vec<_>>();
        let left = |a: &App| a.toast_due(Instant::now()).expect("a wakeup").saturating_duration_since(Instant::now());
        a.bar.toasts = vec![toast(3, t0, 60.0)];
        let frame = |a: &App| layout(&a.view, 34, 30, &Opts { bar: a.shown_bar(), now: Some(text::now()), ..Opts::default() });
        a.shown = frame(&a);
        // One card (its title row, one line of text) and the blank row under it.
        assert_eq!(a.shown.toast, 0..3);
        let l = left(&a);
        assert!(l > Duration::from_secs(55) && l <= Duration::from_secs(61), "{l:?}");
        a.handle(b"\x1b[<0;5;2M\x1b[<0;5;2m");
        assert_eq!((a.focus_backs, a.last_act.clone(), a.dismissed.clone()), (1, Some(Target::Toast((3, t0))), vec![(3, t0)]));
        a.shown = frame(&a);
        assert!(a.shown.toast.is_empty() && seqs(&a).is_empty() && a.toast_due(Instant::now()).is_none());
        assert_eq!(a.bar.toasts.len(), 1, "only the drawing forgets it");
        // A newer one: shown at once, the dismissed one still not.
        a.bar.toasts = vec![toast(3, t0, 60.0), toast(4, t0 + 1, 60.0)];
        a.shown = frame(&a);
        assert_eq!((a.shown.toast.clone(), seqs(&a)), (0..3, vec![4]));
        // Two racing posts that took the same seq: dismissing one never
        // hides the other.
        a.dismissed = vec![(4, t0 + 1)];
        a.bar.toasts = vec![toast(4, t0 + 3, 60.0)];
        a.shown = frame(&a);
        assert_eq!(a.shown.toast, 0..3);
        // Stacked, oldest on top: the wakeup is the earliest deadline; a
        // click on the second card dismisses that one alone.
        a.dismissed.clear();
        a.bar.toasts = vec![toast(5, t0, 60.0), toast(6, t0 + 5, 20.0)];
        a.shown = frame(&a);
        assert_eq!(a.shown.toast, 0..6);
        let l = left(&a);
        assert!(l > Duration::from_secs(20) && l <= Duration::from_secs(26), "{l:?}");
        a.handle(b"\x1b[<0;20;5M\x1b[<0;20;5m");
        assert_eq!((a.focus_backs, a.dismissed.clone()), (2, vec![(6, t0 + 5)]));
        a.shown = frame(&a);
        assert_eq!((a.shown.toast.clone(), seqs(&a)), (0..3, vec![5]));
        // A refresh forgets the dismissals of toasts gone from `@toast`, as
        // the snapshot read it: not on a failed snapshot (no client row),
        // not while the bar's filtered list is empty (a client briefly
        // unresolved); only when the option itself no longer holds it.
        let snap_with = |raw: &str| {
            let mut st = vec![String::new(); crate::tmux::STATUS_FORMATS.len()];
            st[29] = raw.to_string();
            let head = ["", "client", "/dev/ttys999", "@2", "main", "", "", "", "4242"].map(String::from);
            Snapshot::parse(&head.into_iter().chain(st).collect::<Vec<_>>().join("\x1f"))
        };
        a.bar.toasts = vec![];
        a.core.snapshot = Snapshot::parse("");
        a.forget_gone_toasts();
        assert_eq!(a.dismissed, vec![(6, t0 + 5)], "a failed snapshot forgets nothing");
        a.core.snapshot = snap_with(&format!("5\t{t0}\tok\t60\t\tparked 5\u{1e}6\t{}\tok\t20\t/dev/ttys001\tparked 6", t0 + 5));
        a.forget_gone_toasts();
        assert_eq!(a.dismissed, vec![(6, t0 + 5)], "still held (for another client): kept");
        a.core.snapshot = snap_with(&format!("5\t{t0}\tok\t60\t\tparked 5"));
        a.forget_gone_toasts();
        assert!(a.dismissed.is_empty());
        for i in 0..20 {
            a.dismiss((i, 0));
        }
        a.dismiss((19, 0));
        assert_eq!((a.dismissed.len(), a.dismissed.last()), (DISMISSED_MAX, Some(&(19, 0))));
        // Expired: the wakeup is now (to draw it away), then none.
        a.dismissed.clear();
        a.bar.toasts = vec![toast(7, t0 - 61_000, 60.0)];
        a.shown.toast_until = Some(((text::now() - 1.0) * 1000.0) as i64);
        let now = Instant::now();
        assert!(a.toast_due(now).unwrap() <= now + Duration::from_millis(5));
        a.shown = frame(&a);
        assert_eq!((a.shown.toast.clone(), a.toast_due(now)), (0..0, None));
    }

    #[test]
    fn args_parse() {
        assert_eq!(parse_size("34x58"), Some((34, 58)));
        for bad in ["34", "x58", "0x5", "34x-1", "axb"] {
            assert_eq!(parse_size(bad), None, "{bad}");
        }
    }
}
