//! "The sidebar is the whole UI": while any sidebar is open the tmux status
//! line is hidden (`set -g status off`), and what it showed lives in the
//! sidebar's status block instead. This module holds that block's data
//! ([`Bar`], built from the one per-tick snapshot, no extra tmux call and
//! no script run), the open-sidebar REGISTRY behind `@sidebar_open` (which
//! hides and restores the status line), and the [`Keeper`] that keeps the
//! status line's `#()` jobs (continuum's autosave, the usage tick) running
//! while the line itself is hidden.
//!
//! ## The registry (`@sidebar_open`, `@sidebar_pids`)
//!
//! `@sidebar_pids` lists the pids of the open sidebars and `@sidebar_open`
//! is its length (UB's prefix bindings read the count). Every change runs
//! under an exclusive `flock` (one lock file per tmux socket), reads the
//! list, drops every pid that is no longer a live `agent-ui` process (a
//! crashed or SIGKILLed sidebar), applies the change and writes the list,
//! the count and `status off` (count > 0) or `status on` (count 0) in ONE
//! tmux command. So the count is repaired by:
//! - the next sidebar to start ([`Registry::open`]),
//! - every exit path of a sidebar ([`Registry::close`]: normal, never-alone,
//!   SIGHUP / SIGTERM, a panic unwinding through the guard),
//! - a cheap check on each refresh of a live sidebar ([`needs_repair`]: its
//!   own pid missing, a dead pid listed, or a count that disagrees), and
//! - `agent-ui sidebar --repair` (one read, one write) for a hook to call.
//!
//! `@sidebar_clients` rides beside `@sidebar_pids` in the same writes, word
//! for word: the client each open sidebar sits beside (`-` until it has
//! one). A sidebar whose client resolves or changes re-registers on its
//! next refresh ([`needs_repair`]). notify.sh reads it to choose between a
//! toast and tmux's own `display-message`.
//!
//! ## Toasts (`@toast`)
//!
//! scripts/notify.sh posts a message as a toast instead of tmux's
//! `display-message` (which, with the status line off, would draw on the
//! client's bottom line and bring the line back) whenever a sidebar is open
//! for its client: it writes `@toast_seq` and `@toast` (up to 4 RS-separated
//! records, oldest first, [`Toast`]) in one tmux call and posts the focus
//! notice. The sidebar reads `@toast` in the same per-tick snapshot as
//! everything here; [`Bar::toasts`] keeps the records for this sidebar's
//! client (or every sidebar's).

use super::layout::Ink;
use super::term;
use crate::model::HIDDEN;
use crate::text::sanitize;
use crate::tmux::{Snapshot, Socket, Tmux, Window, HOLD};
use std::path::PathBuf;
use std::time::{Duration, Instant};

// ---------------------------------------------------------------- colours

/// The xterm-256 colour `n` as (r, g, b), as THIS terminal draws it: the
/// sixteen ANSI colours are WezTerm's `catppuccin-mocha` scheme's (wezterm.lua
/// `color_scheme`), not stock xterm's; the cube and the greys are xterm's.
fn xterm(n: u8) -> (u8, u8, u8) {
    const BASE: [(u8, u8, u8); 16] = [
        // black surface1, red, green, yellow, blue, magenta pink, cyan teal, white subtext1
        (0x45, 0x47, 0x5a), (0xf3, 0x8b, 0xa8), (0xa6, 0xe3, 0xa1), (0xf9, 0xe2, 0xaf),
        (0x89, 0xb4, 0xfa), (0xf5, 0xc2, 0xe7), (0x94, 0xe2, 0xd5), (0xba, 0xc2, 0xde),
        // bright: black surface2, the same accents, white subtext0
        (0x58, 0x5b, 0x70), (0xf3, 0x8b, 0xa8), (0xa6, 0xe3, 0xa1), (0xf9, 0xe2, 0xaf),
        (0x89, 0xb4, 0xfa), (0xf5, 0xc2, 0xe7), (0x94, 0xe2, 0xd5), (0xa6, 0xad, 0xc8),
    ];
    match n {
        0..=15 => BASE[n as usize],
        16..=231 => {
            let l = |v: u8| if v == 0 { 0 } else { 55 + 40 * v };
            let k = n - 16;
            (l(k / 36), l(k / 6 % 6), l(k % 6))
        }
        _ => {
            let g = 8 + 10 * (n - 232);
            (g, g, g)
        }
    }
}

/// A tmux colour as tmux options spell it → an [`Ink`]: `#rrggbb`,
/// `colourN` / `colorN`, or a name (the eight ANSI names, `bright…` too,
/// drawn in the sidebar's palette). `default`, `terminal`, "" and junk →
/// None (the caller's neutral style).
pub fn tmux_colour(raw: &str) -> Option<Ink> {
    let s = raw.trim().to_ascii_lowercase();
    if let Some(h) = s.strip_prefix('#') {
        if h.len() == 6 && h.bytes().all(|b| b.is_ascii_hexdigit()) {
            let p = |i: usize| u8::from_str_radix(&h[i..i + 2], 16).ok();
            return Some(Ink::Rgb(p(0)?, p(2)?, p(4)?));
        }
        return None;
    }
    if let Some(n) = s.strip_prefix("colour").or_else(|| s.strip_prefix("color")) {
        return n.parse::<u8>().ok().map(Ink::Idx);
    }
    let pal = match s.strip_prefix("bright").unwrap_or(&s) {
        "red" => "red",
        "green" => "green",
        "yellow" => "yellow",
        "blue" => "blue",
        "magenta" => "mauve",
        "cyan" => "sky",
        "white" => "text",
        "black" => "crust",
        _ => return None,
    };
    Some(Ink::Pal(pal))
}

/// An ink's (r, g, b), as the terminal will show it (the canvas: the
/// palette's `base`).
pub fn ink_rgb(ink: Ink) -> (u8, u8, u8) {
    match ink {
        Ink::Rgb(r, g, b) => (r, g, b),
        Ink::Idx(n) => xterm(n),
        Ink::Pal(p) => crate::palette::rgb(p),
        Ink::Mix(p, pct) => crate::palette::mix_rgb(p, pct),
        Ink::Tint(r, g, b, pct) => crate::palette::over_canvas((r, g, b), pct),
        Ink::Canvas => crate::palette::rgb(crate::palette::CANVAS),
    }
}

/// The ink that reads on `bg`: dark (`crust`) on a light colour, light
/// (`text`) on a dark one (relative luminance, 0.45 cut).
pub fn ink_on(bg: Ink) -> Ink {
    let (r, g, b) = ink_rgb(bg);
    let lum = (0.2126 * f64::from(r) + 0.7152 * f64::from(g) + 0.0722 * f64::from(b)) / 255.0;
    Ink::Pal(if lum > 0.45 { "crust" } else { "text" })
}

// ---------------------------------------------------------------- the bar

/// The client's space, as the status line's chip showed it.
#[derive(Debug, Clone, PartialEq)]
pub struct Chip {
    /// `@space_index` (⌃N), when set.
    pub index: Option<u32>,
    pub name: String,
    /// `@space_color`, when set and valid.
    pub color: Option<Ink>,
    /// Not a space (a hidden session or the stash): drawn `[name]`, dim.
    pub hidden: bool,
}

/// One of this space's shells (a window without `@agent_state`), as
/// space.sh numbers them: window-index order, the home window (always the
/// lowest) first, so ⌥N (M-N, `space.sh shell N`) is the Nth here.
#[derive(Debug, Clone, PartialEq)]
pub struct Shell {
    pub n: u32,
    pub label: String,
    pub home: bool,
    /// The session's active window.
    pub active: bool,
    /// In stash.sh's range selection (`@stash_sel`): the stash tint.
    pub selected: bool,
    pub win: String,
    pub session: String,
}

/// One usage window of status-right (`S` / `W` / the scoped one), drawn as
/// a meter: the numbers come from `@codex_<w>_used` / `_expected` /
/// `_delta` / `_resets` / `_members` when set, else from the text
/// (`60% (+17%)`, `22% (Σ-87%)`, `21%`, `idle`, `--%`).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Usage {
    pub label: String,
    /// `@codex_<w>_text` as the status line showed it (`--%` when unset).
    pub text: String,
    pub color: Option<Ink>,
    /// Percent used, as reported (it can pass 100); None: unknown (`--%`,
    /// `idle`).
    pub used: Option<f64>,
    /// Where the pace says it should be by now (percent, unclamped): the
    /// meter's tick.
    pub expected: Option<f64>,
    /// used − expected, signed percentage points (status-right's `(+17%)`).
    pub delta: Option<i32>,
    /// When the window resets (epoch seconds).
    pub resets: Option<i64>,
    /// The accounts pooled into this window (`@codex_<w>_members`,
    /// `slot:used:active[:cap],…`; empty for a single account).
    pub members: Vec<Member>,
    /// The pace is a pool's (`Σ` in the text, or 2+ members).
    pub pooled: bool,
    /// No window open (`idle`).
    pub idle: bool,
}

/// One account of a pooled window: its claude-swap slot, its own used
/// percent, whether it is the logged-in one, and its cap (100 − its
/// reserve: the percent it may be used to).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Member {
    /// None: the script knows the account but not its slot (`:27:1`).
    pub slot: Option<u32>,
    pub used: u32,
    pub active: bool,
    /// 100 when the entry has no 4th field (or a junk one).
    pub cap: u32,
}

/// A member's cap when the entry gives none.
pub const FULL_CAP: u32 = 100;

/// `1:90:0,2:30:1` or `1:90:0:90,2:32:1:100` → the members (an empty slot
/// allowed; the optional 4th field is the cap, [`FULL_CAP`] when absent or
/// junk; junk entries dropped).
pub fn parse_members(raw: &str) -> Vec<Member> {
    raw.split(',')
        .filter_map(|m| {
            let mut p = m.trim().split(':');
            let slot = match p.next()?.trim() {
                "" => None,
                s => Some(s.parse().ok()?),
            };
            let used = p.next()?.trim().parse().ok()?;
            let active = p.next()?.trim() == "1";
            let cap = p.next().and_then(|c| c.trim().parse().ok()).unwrap_or(FULL_CAP);
            Some(Member { slot, used, active, cap })
        })
        .collect()
}

impl Usage {
    /// Fill the numbers: the numeric options first, the text for the rest.
    pub fn read(label: String, text: String, color: Option<Ink>, m: &crate::tmux::Meter) -> Usage {
        let num = |s: &str| s.trim().parse::<f64>().ok().filter(|v| v.is_finite());
        let int = |s: &str| s.trim().trim_start_matches('+').parse::<i32>().ok();
        let t = text.trim();
        let idle = t == "idle";
        // `60% (+17%)`: the level before the first `%`, the delta in the brackets.
        let text_used = if t.contains('%') { t.split('%').next().and_then(num) } else { None };
        let inner = t.split_once('(').and_then(|(_, r)| r.split_once(')')).map(|(i, _)| i.trim());
        let sigma = inner.is_some_and(|i| i.starts_with('Σ'));
        let text_delta = inner.map(|i| i.trim_start_matches('Σ').trim_end_matches('%')).and_then(int);
        // The real numbers (a window can be over 100%): the meter clamps
        // only its fill and tick.
        let used = num(&m.used).or(text_used);
        let delta = int(&m.delta).or(text_delta);
        let expected = num(&m.expected).or_else(|| Some(used? - f64::from(delta?)));
        let members = parse_members(&m.members);
        let resets = m.resets.trim().parse::<i64>().ok().filter(|&r| r > 0);
        Usage {
            label,
            text,
            color,
            used,
            expected,
            delta,
            resets,
            pooled: sigma || members.len() >= 2,
            members,
            idle,
        }
    }
}

/// Everything the status block draws (besides the counts, which the view
/// has): built from the RAW snapshot, before home windows are hidden (a
/// space's home is its shell 1).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Bar {
    /// The client's space; None when the client is unknown (the classic
    /// header then).
    pub space: Option<Chip>,
    /// `#{client_key_table}` is `prefix`: the red mark.
    pub prefix: bool,
    /// It is `jump` (⌥W armed): the letter key-caps turn peach and the
    /// card says `⌥W jump · type a letter`.
    pub jump: bool,
    pub shells: Vec<Shell>,
    /// Empty when no usage option is set at all (no usage line).
    pub usage: Vec<Usage>,
    /// `@resurrect_stale` in words (`last save 2h ago`, `save refused`).
    pub stale: Option<String>,
    /// prefix v's range selection is live: how many windows it holds. Live
    /// while the client is in stash.sh's `stash` key table (armed by prefix
    /// v, left by ⏎ / H / m / a / esc / q / C-c / space) or while any window
    /// still carries `@stash_sel` (sel_clear's "never leak a tint" rule).
    pub selecting: Option<usize>,
    /// notify.sh's recent toasts (oldest first), those for this sidebar's
    /// client or for every sidebar. Expiry is the layout's business (its
    /// clock): it draws only the live ones.
    pub toasts: Vec<Toast>,
}

// ---------------------------------------------------------------- toasts

/// A toast's kind (`notify.sh -k`): its glyph and colour in the layout.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Info,
    Ok,
    Warn,
    Error,
}

impl Kind {
    /// `ok` / `warn` / `error`; anything else is info.
    pub fn parse(s: &str) -> Kind {
        match s.trim() {
            "ok" => Kind::Ok,
            "warn" => Kind::Warn,
            "error" => Kind::Error,
            _ => Kind::Info,
        }
    }
}

/// A toast's identity: (seq, epoch ms) ([`Toast::id`]).
pub type ToastId = (u64, i64);

/// The option notify.sh writes a toast to, and the counter beside it.
pub const TOAST: &str = "@toast";
pub const TOAST_SEQ: &str = "@toast_seq";

/// What separates the records in `@toast` (ASCII RS).
pub const TOAST_RS: char = '\u{1e}';

/// One toast from notify.sh, as `@toast` holds it (a GLOBAL option, read in
/// the per-tick snapshot): up to 4 records separated by RS (U+001E), oldest
/// first, each `seq TAB epoch_ms TAB kind TAB ttl_s TAB client TAB text
/// [TAB title]` (6-field records, from before titles, still parse).
#[derive(Debug, Clone, PartialEq)]
pub struct Toast {
    /// `@toast_seq` when it was posted.
    pub seq: u64,
    /// When it was posted (epoch milliseconds).
    pub at_ms: i64,
    pub kind: Kind,
    /// How long it shows (seconds).
    pub ttl: f64,
    /// The client it is for ("" = every sidebar).
    pub client: String,
    /// One line, controls made spaces, never empty.
    pub text: String,
    /// `notify.sh -T`: the card's title instead of the kind's own; None
    /// when not given (or blank).
    pub title: Option<String>,
}

/// One line: controls made spaces, runs of spaces one.
fn one_line(s: &str) -> String {
    sanitize(s).split_whitespace().collect::<Vec<_>>().join(" ")
}

impl Toast {
    /// One `@toast` record → the toast; None when malformed or empty.
    pub fn parse(raw: &str) -> Option<Toast> {
        // The 7th field is the title (notify.sh never writes a tab into
        // the text, so a 6-field record is one from before titles).
        let f: Vec<&str> = raw.trim_matches(|c| c == '\n' || c == TOAST_RS).splitn(7, '\t').collect();
        let [seq, at, kind, ttl, client, text] = f.get(..6).unwrap_or(&[])[..] else { return None };
        let title = f.get(6);
        let text = one_line(text);
        if text.is_empty() {
            return None;
        }
        let title = title.map(|t| one_line(t)).filter(|t| !t.is_empty());
        let ttl = ttl.trim().parse::<f64>().ok().filter(|t| t.is_finite() && *t > 0.0).unwrap_or(4.0).min(3600.0);
        Some(Toast {
            seq: seq.trim().parse().ok()?,
            at_ms: at.trim().parse().ok()?,
            kind: Kind::parse(kind),
            ttl,
            client: client.trim().to_string(),
            text,
            title,
        })
    }

    /// All of `@toast`: every well-formed record, oldest first.
    pub fn parse_all(raw: &str) -> Vec<Toast> {
        raw.split(TOAST_RS).filter_map(Toast::parse).collect()
    }

    /// Which toast this is, for a dismissal: notify.sh reads `@toast_seq`
    /// and writes it +1 in two calls, so two racing posts can share a seq;
    /// with the posting time they do not.
    pub fn id(&self) -> ToastId {
        (self.seq, self.at_ms)
    }

    /// When it stops showing (epoch seconds).
    pub fn until(&self) -> f64 {
        self.at_ms as f64 / 1000.0 + self.ttl
    }

    /// Showing at `now` (epoch seconds).
    pub fn live(&self, now: f64) -> bool {
        now < self.until()
    }

    /// For the sidebar beside `client`: for every sidebar, or for that one.
    /// A sidebar with no client yet shows only the former.
    pub fn for_client(&self, client: Option<&str>) -> bool {
        self.client.is_empty() || Some(self.client.as_str()) == client
    }
}

/// A shell's label, as spaces.conf's `@space_shell_label`: a shell process
/// shows where it is (`~`, or the cwd's last part), anything else its name
/// (tmux's automatic name: the command). The window name stands in for
/// `pane_current_command` (no extra field). While a space home's DASHBOARD
/// pane is the active one, the automatic name is the dashboard's command,
/// so the shell under it is labelled by its cwd ([`Window::path`] is the
/// shell's then).
pub fn shell_label(w: &Window) -> String {
    let name = w.name.trim();
    let dash_active = w.pane_list.iter().any(|p| p.dash && p.active);
    if dash_active || matches!(name.trim_start_matches('-'), "zsh" | "bash" | "sh" | "fish") {
        let home = std::env::var("HOME").unwrap_or_default();
        let path = w.path.trim_end_matches('/');
        if path.is_empty() {
            return sanitize(name);
        }
        if !home.is_empty() && path == home.trim_end_matches('/') {
            return "~".into();
        }
        return sanitize(path.rsplit('/').next().filter(|s| !s.is_empty()).unwrap_or("/"));
    }
    sanitize(name)
}

impl Bar {
    pub fn build(snap: &Snapshot, client: Option<&str>) -> Bar {
        let me = client.and_then(|c| snap.client(c));
        // Global options ride on every client row: ours, else any.
        let st = me.or(snap.clients.first()).map(|c| &c.status);
        let mut bar = Bar::default();
        if let Some(st) = st {
            bar.usage = usage(st);
            bar.stale = match st.resurrect_stale.trim() {
                "" => None,
                "refused" => Some("save refused".into()),
                v => Some(format!("last save {} ago", sanitize(v))),
            };
            bar.toasts = Toast::parse_all(&st.toast).into_iter().filter(|t| t.for_client(client)).collect();
        }
        let mut flagged: Vec<&str> = snap.windows.iter().filter(|w| w.stash_sel).map(|w| w.id.as_str()).collect();
        flagged.sort_unstable();
        flagged.dedup();
        let armed = me.is_some_and(|c| c.status.key_table == "stash");
        if armed || !flagged.is_empty() {
            bar.selecting = Some(flagged.len());
        }
        let Some(me) = me else { return bar };
        bar.prefix = me.status.key_table == "prefix";
        bar.jump = me.status.key_table == "jump";
        let session = if me.session.is_empty() {
            snap.rows(&me.window_id).next().map(|w| w.session.clone()).unwrap_or_default()
        } else {
            me.session.clone()
        };
        if session.is_empty() {
            return bar;
        }
        let mut ws: Vec<&Window> = snap.windows.iter().filter(|w| w.session == session).collect();
        ws.sort_by_key(|w| w.index);
        let hidden = me.space_hidden || HIDDEN.contains(&session.as_str()) || session == HOLD;
        bar.space = Some(Chip {
            index: ws.first().and_then(|w| w.space_index),
            color: ws.first().and_then(|w| tmux_colour(&w.space_color)),
            name: session.clone(),
            hidden,
        });
        if !hidden {
            bar.shells = ws
                .iter()
                .filter(|w| w.state.is_empty())
                .enumerate()
                .map(|(k, w)| Shell {
                    n: k as u32 + 1,
                    label: shell_label(w),
                    home: w.home,
                    active: w.active,
                    selected: w.stash_sel,
                    win: w.id.clone(),
                    session: session.clone(),
                })
                .collect();
        }
        bar
    }
}

/// status-right's three usage modules, from the options
/// codexbar-usage-status.sh keeps (`@codex_*`: the display provider's
/// numbers, Claude's here): `--%` while one is unset, as the bar does.
/// Nothing at all when none of them is set.
fn usage(st: &crate::tmux::Status) -> Vec<Usage> {
    if [&st.session_text, &st.weekly_text, &st.scoped_text].iter().all(|t| t.trim().is_empty()) {
        return Vec::new();
    }
    let text = |t: &str| if t.trim().is_empty() { "--%".to_string() } else { sanitize(t.trim()) };
    let account = sanitize(st.account_label.trim());
    let s_label = if account.is_empty() { "S".to_string() } else { format!("{account} S") };
    let icon = sanitize(st.scoped_icon.trim().trim_end_matches(':'));
    let [m0, m1, m2] = &st.meters;
    vec![
        Usage::read(s_label, text(&st.session_text), tmux_colour(&st.session_color), m0),
        Usage::read("W".into(), text(&st.weekly_text), tmux_colour(&st.weekly_color), m1),
        Usage::read(if icon.is_empty() { "F".into() } else { icon }, text(&st.scoped_text), tmux_colour(&st.scoped_color), m2),
    ]
}

// ---------------------------------------------------------------- the key table

/// How often [`KeyPoll`] re-reads the client's key table while it is not
/// `root`.
pub const KEY_POLL_EVERY: Duration = Duration::from_millis(150);
/// How long one poll lasts at most: still not `root` after this, it stops
/// until the next notice (a client left in a table, say copy-mode's).
pub const KEY_POLL_FOR: Duration = Duration::from_secs(10);

/// A key table that needs no watching: `root`, or unknown ("": no client
/// row, a fake client, an older row).
pub fn is_root(table: &str) -> bool {
    matches!(table.trim(), "" | "root")
}

/// What to do after one cheap read ([`KeyPoll::read`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeyStep {
    /// The same table: read again at [`KeyPoll::due`].
    Wait,
    /// The table changed (back to `root`, or another one): one full refresh.
    Refresh,
    /// Gave up (the bound): nothing more until a refresh sees another table.
    Stop,
}

/// The prefix / ⌥W mark must not stick: tmux posts the focus notice when a
/// key table is ENTERED (the root binding that switches to it does), but
/// nothing when the next key leaves it. So while a refresh shows the
/// client in any table but `root`, the sidebar re-reads ONLY
/// `#{client_key_table}` every [`KEY_POLL_EVERY`] (one `display-message`,
/// no snapshot) until it changes, then refreshes once. The [`KEY_POLL_FOR`]
/// bound starts only when the table CHANGES (root → prefix, prefix →
/// jump, ...): a long stay in one table (prefix v's `stash` while agents
/// post notices, copy-mode) stops polling after it however many notices
/// arrive, and each later notice is then just its one plain refresh. In
/// `root` nothing is scheduled at all: idle costs nothing.
#[derive(Debug, Clone, Default)]
pub struct KeyPoll {
    /// The table the last refresh saw ("" none yet).
    table: String,
    /// The next cheap read; None: not polling.
    next: Option<Instant>,
    /// When this poll gives up.
    until: Option<Instant>,
}

impl KeyPoll {
    /// A full refresh (whatever caused it: a notice, a click, the timer, the
    /// poll's own) saw the client in `table`: a new non-root table starts a
    /// poll with a fresh bound; the same one continues a running poll until
    /// its bound, and never restarts one.
    pub fn seen(&mut self, table: &str, now: Instant) {
        let table = table.trim();
        let changed = table != self.table;
        self.table = table.to_string();
        if is_root(table) {
            self.stop();
        } else if changed {
            self.until = Some(now + KEY_POLL_FOR);
            self.next = Some(now + KEY_POLL_EVERY);
        } else if self.until.is_some_and(|u| now < u) {
            self.next = Some(now + KEY_POLL_EVERY);
        } else {
            self.stop();
        }
    }

    /// One cheap read's result (None: it failed, e.g. the client left).
    pub fn read(&mut self, got: Option<&str>, now: Instant) -> KeyStep {
        match got.map(str::trim) {
            None => {
                self.stop();
                KeyStep::Refresh
            }
            Some(t) if t != self.table => {
                // `seen` decides what comes next, from the refresh's snapshot.
                self.next = None;
                KeyStep::Refresh
            }
            Some(_) if self.until.is_none_or(|u| now >= u) => {
                self.stop();
                KeyStep::Stop
            }
            Some(_) => {
                self.next = Some(now + KEY_POLL_EVERY);
                KeyStep::Wait
            }
        }
    }

    /// When the next cheap read is due; None while not polling.
    pub fn due(&self) -> Option<Instant> {
        self.next
    }

    pub fn polling(&self) -> bool {
        self.next.is_some()
    }

    fn stop(&mut self) {
        self.next = None;
        self.until = None;
    }
}

/// The cheap read: the client's key table and nothing else.
pub fn key_table_command(client: &str) -> Vec<String> {
    ["display-message", "-p", "-c", client, "#{client_key_table}"].map(String::from).to_vec()
}

// ---------------------------------------------------------------- the registry

/// The option holding the open sidebars' pids, and the count UB reads.
pub const PIDS: &str = "@sidebar_pids";
pub const OPEN: &str = "@sidebar_open";

/// `@sidebar_pids` → pids (junk dropped).
pub fn parse_pids(raw: &str) -> Vec<i32> {
    raw.split_whitespace().filter_map(|p| p.parse::<i32>().ok()).filter(|&p| p > 0).collect()
}

/// The open sidebars' clients, word for word beside [`PIDS`] (word i is pid
/// i's client; [`NO_CLIENT`] while it has none): notify.sh reads it to know
/// whether a client has a sidebar to toast in.
pub const CLIENTS: &str = "@sidebar_clients";
/// The [`CLIENTS`] word of a sidebar whose client is not known (yet).
pub const NO_CLIENT: &str = "-";

/// One open sidebar: its pid and its client's word in [`CLIENTS`].
pub type Entry = (i32, String);

/// A client as its [`CLIENTS`] word: the name, or [`NO_CLIENT`] for none
/// (or one no word could hold).
pub fn client_word(client: Option<&str>) -> String {
    match client.map(str::trim) {
        Some(c) if !c.is_empty() && !c.contains(char::is_whitespace) => c.to_string(),
        _ => NO_CLIENT.into(),
    }
}

/// `@sidebar_pids` and `@sidebar_clients` → the entries (junk pids dropped,
/// a missing client word read as [`NO_CLIENT`]).
pub fn parse_entries(pids: &str, clients: &str) -> Vec<Entry> {
    let words: Vec<&str> = clients.split_whitespace().collect();
    pids.split_whitespace()
        .enumerate()
        .filter_map(|(i, p)| {
            let pid = p.parse::<i32>().ok().filter(|&p| p > 0)?;
            Some((pid, words.get(i).copied().unwrap_or(NO_CLIENT).to_string()))
        })
        .collect()
}

/// The list after a change: `prev` without dead pids (`live`) and without
/// `remove`, plus `add` (whose client replaces any listed for its pid);
/// each pid once, in order.
pub fn next_entries(prev: &[Entry], live: &dyn Fn(i32) -> bool, add: Option<&Entry>, remove: Option<i32>) -> Vec<Entry> {
    let mut out: Vec<Entry> = Vec::new();
    for e in prev.iter().chain(add) {
        let mine = add.filter(|a| a.0 == e.0);
        if Some(e.0) != remove && !out.iter().any(|o| o.0 == e.0) && (mine.is_some() || live(e.0)) {
            out.push(mine.unwrap_or(e).clone());
        }
    }
    out
}

/// The ONE tmux command line storing `entries`: the pids, their clients,
/// the count, and the status line (hidden while any sidebar is open, back
/// once none is).
pub fn write_command(entries: &[Entry]) -> Vec<String> {
    let pids = entries.iter().map(|e| e.0.to_string()).collect::<Vec<_>>().join(" ");
    let clients = entries.iter().map(|e| e.1.as_str()).collect::<Vec<_>>().join(" ");
    let mut a: Vec<String> = Vec::new();
    if entries.is_empty() {
        a.extend(["set-option", "-gqu", PIDS, ";", "set-option", "-gqu", CLIENTS].map(String::from));
    } else {
        a.extend(["set-option", "-gq", PIDS, &pids, ";", "set-option", "-gq", CLIENTS, &clients].map(String::from));
    }
    a.push(";".into());
    a.extend(["set-option", "-gq", OPEN, &entries.len().to_string()].map(String::from));
    a.push(";".into());
    a.extend(["set-option", "-gq", "status", if entries.is_empty() { "on" } else { "off" }].map(String::from));
    a
}

/// Whether a live sidebar's snapshot shows the registry out of step: its own
/// pid missing (a repair elsewhere, a restarted server) or listed with
/// another client than `client` (its [`client_word`]: resolved since it
/// opened, or moved), a listed pid that is dead, a count that is not the
/// list's length, or the status line shown (a config reload's `set -g
/// status on`) while this sidebar is open.
pub fn needs_repair(st: &crate::tmux::Status, me: i32, client: &str, live: &dyn Fn(i32) -> bool) -> bool {
    let entries = parse_entries(&st.sidebar_pids, &st.sidebar_clients);
    entries.iter().find(|e| e.0 == me).is_none_or(|e| e.1 != client)
        || entries.iter().any(|e| e.0 != me && !live(e.0))
        || st.sidebar_open.trim() != entries.len().to_string()
        || st.status_line.trim() != "off"
}

/// `pid` is a live agent-ui process (not a dead one's reused pid). When the
/// executable's path can't be read, alive is enough.
pub fn is_sidebar(pid: i32) -> bool {
    if !term::alive(pid) {
        return false;
    }
    match exe_name(pid) {
        Some(name) => name.starts_with("agent-ui") || name.starts_with("agent_ui"),
        None => true,
    }
}

#[cfg(target_os = "macos")]
fn exe_name(pid: i32) -> Option<String> {
    extern "C" {
        fn proc_pidpath(pid: i32, buf: *mut u8, size: u32) -> i32;
    }
    let mut buf = vec![0u8; 4096];
    // SAFETY: buf is valid for its length; the call writes at most `size` bytes.
    let n = unsafe { proc_pidpath(pid, buf.as_mut_ptr(), buf.len() as u32) };
    if n <= 0 {
        return None;
    }
    buf.truncate(n as usize);
    let path = String::from_utf8_lossy(&buf).into_owned();
    path.rsplit('/').next().map(String::from)
}

#[cfg(not(target_os = "macos"))]
fn exe_name(_pid: i32) -> Option<String> {
    None
}

/// The registry on one tmux server.
pub struct Registry {
    tmux: Tmux,
    lock: PathBuf,
}

impl Registry {
    /// The registry's lock file for `tmux`'s server (tests remove theirs).
    pub fn lock_path(tmux: &Tmux) -> PathBuf {
        Registry::new(tmux).lock
    }

    pub fn new(tmux: &Tmux) -> Registry {
        // One lock per socket (a test's private server never waits on the real one).
        let sock = match &tmux.socket {
            Some(s) => s.path(),
            // The ambient server: $TMUX's socket, else the default one.
            None => match std::env::var("TMUX").ok().and_then(|t| t.split(',').next().map(String::from)) {
                Some(p) if !p.is_empty() => PathBuf::from(p),
                _ => Socket::Name("default".into()).path(),
            },
        };
        let tag: String = sock.to_string_lossy().chars().map(|c| if c.is_ascii_alphanumeric() { c } else { '_' }).collect();
        let lock = std::env::temp_dir().join(format!("agent-ui-sidebar{tag}.lock"));
        Registry { tmux: tmux.clone(), lock }
    }

    /// The list as tmux holds it, both options in one call; None when tmux
    /// could not be asked.
    fn read(&self) -> Option<Vec<Entry>> {
        let fmt = format!("#{{{PIDS}}}\x1f#{{{CLIENTS}}}");
        let o = self.tmux.run(&["display-message", "-p", &fmt]).ok().filter(|o| o.ok())?;
        let (pids, clients) = o.stdout.trim_end_matches('\n').split_once('\x1f').unwrap_or((o.stdout.as_str(), ""));
        Some(parse_entries(pids, clients))
    }

    /// Read, change and write under the lock → the new count (None: tmux
    /// failed, nothing written).
    fn update(&self, add: Option<Entry>, remove: Option<i32>, force: bool) -> Option<usize> {
        // Only a start creates the lock file; a leaving or repairing one
        // whose server (and file) is gone runs unlocked.
        let _guard = FileLock::take(&self.lock, add.is_some());
        let prev = self.read()?;
        let next = next_entries(&prev, &is_sidebar, add.as_ref(), remove);
        if !force && next.is_empty() && prev.is_empty() {
            // Nothing open and nothing to repair: leave the status line
            // alone (off for another reason stays off), make the count agree.
            let _ = self.tmux.run(&["set-option", "-gq", OPEN, "0"]);
            return Some(0);
        }
        let o = self.tmux.run(&write_command(&next)).ok().filter(|o| o.ok())?;
        let _ = o;
        Some(next.len())
    }

    /// A sidebar starts (or re-registers, its client resolved or changed):
    /// listed with its client (dead ones dropped), the status line hidden.
    pub fn open(&self, me: i32, client: Option<&str>) -> Option<usize> {
        self.update(Some((me, client_word(client))), None, true)
    }

    /// A sidebar leaves: unlisted; the status line back once none is open.
    pub fn close(&self, me: i32) -> Option<usize> {
        self.update(None, Some(me), true)
    }

    /// Dead sidebars dropped; the status line back if that leaves none.
    pub fn repair(&self) -> Option<usize> {
        self.update(None, None, false)
    }
}

/// An exclusive flock(2) on a file, released on drop. Taking it never
/// fails the caller: without the file, the change runs unlocked.
struct FileLock(Option<std::fs::File>);

impl FileLock {
    fn take(path: &std::path::Path, create: bool) -> FileLock {
        use std::os::fd::AsRawFd;
        extern "C" {
            fn flock(fd: i32, op: i32) -> i32;
        }
        const LOCK_EX: i32 = 2;
        let f = std::fs::OpenOptions::new().create(create).truncate(false).write(true).open(path).ok();
        if let Some(f) = &f {
            // SAFETY: a valid open fd; flock only blocks until the holder lets go.
            unsafe { flock(f.as_raw_fd(), LOCK_EX) };
        }
        FileLock(f)
    }
}

impl Drop for FileLock {
    fn drop(&mut self) {
        use std::os::fd::AsRawFd;
        extern "C" {
            fn flock(fd: i32, op: i32) -> i32;
        }
        const LOCK_UN: i32 = 8;
        if let Some(f) = &self.0 {
            // SAFETY: as above.
            unsafe { flock(f.as_raw_fd(), LOCK_UN) };
        }
    }
}

// ---------------------------------------------------------------- the keeper

/// The `#(...)` shell commands in a status format, as tmux finds them
/// (nested parentheses counted), each once, in order.
pub fn status_jobs(fmt: &str) -> Vec<String> {
    let b = fmt.as_bytes();
    let mut out: Vec<String> = Vec::new();
    let mut i = 0;
    while i + 1 < b.len() {
        if b[i] == b'#' && b[i + 1] == b'(' {
            let start = i + 2;
            let mut depth = 1;
            let mut j = start;
            while j < b.len() {
                match b[j] {
                    b'(' => depth += 1,
                    b')' => {
                        depth -= 1;
                        if depth == 0 {
                            break;
                        }
                    }
                    _ => {}
                }
                j += 1;
            }
            if j >= b.len() {
                break; // unterminated: tmux runs nothing either
            }
            let cmd = fmt[start..j].trim().to_string();
            if !cmd.is_empty() && !out.contains(&cmd) {
                out.push(cmd);
            }
            i = j + 1;
        } else {
            i += 1;
        }
    }
    out
}

/// How often the status line's settings are read again (a config reload).
const KEEPER_REREAD: Duration = Duration::from_secs(60);

/// With the status line hidden tmux runs none of its `#()` jobs, and
/// status-right carries two that must keep running: continuum's autosave
/// (`continuum_save.sh`, the resurrect cadence) and the usage tick
/// (`codexbar-usage-status.sh --tick`, which keeps the `@codex_*` options
/// this sidebar shows fresh). So ONE open sidebar, the registry's lowest
/// live pid, runs each of them through `run-shell -b` every
/// `status-interval`, exactly as the line would have (formats expanded the
/// same way). Never per frame: at most one tmux call per interval.
#[derive(Debug, Default)]
pub struct Keeper {
    jobs: Vec<String>,
    interval: Duration,
    read_at: Option<Instant>,
    ran_at: Option<Instant>,
    /// How many times the jobs were started (tests).
    pub runs: u32,
}

impl Keeper {
    /// One tick: re-read the settings when due, run the jobs when due.
    /// `leader`: this sidebar is the one that runs them.
    pub fn tick(&mut self, tmux: &Tmux, leader: bool, now: Instant) {
        if !leader {
            return;
        }
        if self.read_at.is_none_or(|t| now.duration_since(t) >= KEEPER_REREAD) {
            self.read_at = Some(now);
            if let Ok(o) = tmux.run(&["show-options", "-gv", "status-interval", ";", "show-options", "-gv", "status-right"]) {
                if o.ok() {
                    let (iv, right) = o.stdout.split_once('\n').unwrap_or((o.stdout.as_str(), ""));
                    let secs = iv.trim().parse::<u64>().unwrap_or(15);
                    self.interval = Duration::from_secs(secs.max(1));
                    self.jobs = status_jobs(right.trim_end_matches('\n'));
                }
            }
        }
        if self.jobs.is_empty() || self.ran_at.is_some_and(|t| now.duration_since(t) < self.interval) {
            return;
        }
        self.ran_at = Some(now);
        self.runs += 1;
        let _ = tmux.run(&run_command(&self.jobs));
    }
}

/// `run-shell -b '{ <job> ; } >/dev/null 2>&1; true' ; …`: one tmux call.
/// Each job is silenced and always succeeds: a bare `run-shell -b` shows
/// a job's output, or its non-zero exit, in view mode on the active pane,
/// where the status line just dropped them.
pub fn run_command(jobs: &[String]) -> Vec<String> {
    let mut a: Vec<String> = Vec::new();
    for j in jobs {
        if !a.is_empty() {
            a.push(";".into());
        }
        a.extend(["run-shell".into(), "-b".into(), quiet(j)]);
    }
    a
}

/// A shell command that runs `job` with no output and exit status 0.
pub fn quiet(job: &str) -> String {
    format!("{{ {job}\n}} </dev/null >/dev/null 2>&1; true")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tmux::tests::row;

    #[test]
    fn colours() {
        assert_eq!(tmux_colour("#F5C2E7"), Some(Ink::Rgb(0xf5, 0xc2, 0xe7)));
        assert_eq!(tmux_colour("colour239"), Some(Ink::Idx(239)));
        assert_eq!(tmux_colour("color2"), Some(Ink::Idx(2)));
        assert_eq!(tmux_colour("green"), Some(Ink::Pal("green")));
        assert_eq!(tmux_colour("brightmagenta"), Some(Ink::Pal("mauve")));
        for junk in ["", "default", "terminal", "#12345", "#zzzzzz", "colour300", "pink"] {
            assert_eq!(tmux_colour(junk), None, "{junk:?}");
        }
        assert_eq!(ink_on(Ink::Rgb(0xf9, 0xe2, 0xaf)), Ink::Pal("crust"));
        assert_eq!(ink_on(Ink::Idx(239)), Ink::Pal("text"));
        assert_eq!(ink_on(Ink::Rgb(0x1e, 0x1e, 0x2e)), Ink::Pal("text"));
        assert_eq!(xterm(196), (255, 0, 0));
        assert_eq!(xterm(232), (8, 8, 8));
        // The ANSI sixteen: catppuccin-mocha's, as WezTerm draws them.
        assert_eq!((xterm(0), xterm(4), xterm(12), xterm(15)), ((0x45, 0x47, 0x5a), (0x89, 0xb4, 0xfa), (0x89, 0xb4, 0xfa), (0xa6, 0xad, 0xc8)));
        assert_eq!((ink_on(Ink::Idx(1)), ink_on(Ink::Idx(0))), (Ink::Pal("crust"), Ink::Pal("text")));
        assert_eq!(ink_rgb(Ink::Tint(0x89, 0xb4, 0xfa, 20)), crate::palette::over_canvas(xterm(4), 20));
    }

    fn client(session: &str, win: &str, status: &[&str]) -> String {
        let mut f = vec!["".to_string(), "client".into(), "/dev/ttys999".into(), win.into(), session.into(), "on".into(),
            "".into(), "".into(), "4242".into()];
        let mut st: Vec<String> = status.iter().map(|s| s.to_string()).collect();
        st.resize(crate::tmux::STATUS_FORMATS.len(), String::new());
        f.extend(st);
        f.join("\x1f")
    }

    #[test]
    fn the_bar_reads_the_one_snapshot() {
        let home = std::env::var("HOME").unwrap();
        let snap = Snapshot::parse(&[
            row("main", 0, "@1", &[("home", "1"), ("path", &home), ("space_index", "1"), ("space_color", "#f9e2af")]),
            row("main", 1, "@2", &[("state", "running"), ("space_index", "1"), ("space_color", "#f9e2af")]),
            row("main", 2, "@3", &[("path", "/tmp/proj"), ("active", "1"), ("space_index", "1"), ("space_color", "#f9e2af")]),
            {
                let mut r = row("main", 3, "@4", &[("space_index", "1"), ("space_color", "#f9e2af")]);
                r = r.replacen("zsh", "nvim", 1);
                r
            },
            row("work", 1, "@5", &[]),
            client("main", "@3", &["prefix", "1", "77", "2h", "71% (-1%)", "green", "", "colour239", "F:", "0%", "#a6e3a1"]),
        ].join("\n"));
        let bar = Bar::build(&snap, Some("/dev/ttys999"));
        assert_eq!(bar.space, Some(Chip { index: Some(1), name: "main".into(), color: Some(Ink::Rgb(0xf9, 0xe2, 0xaf)), hidden: false }));
        assert!(bar.prefix && !bar.jump);
        let shells: Vec<_> = bar.shells.iter().map(|s| (s.n, s.label.as_str(), s.home, s.active, s.win.as_str())).collect();
        assert_eq!(shells, [(1, "~", true, false, "@1"), (2, "proj", false, true, "@3"), (3, "nvim", false, false, "@4")]);
        assert_eq!(bar.usage.iter().map(|u| (u.label.as_str(), u.text.as_str())).collect::<Vec<_>>(),
            [("S", "71% (-1%)"), ("W", "--%"), ("F", "0%")]);
        assert_eq!(bar.usage[1].color, Some(Ink::Idx(239)));
        assert_eq!(bar.stale.as_deref(), Some("last save 2h ago"));
        assert_eq!(bar.selecting, None);
        // A range selection: the stash table armed, two windows flagged
        // (an agent and a shell); the shell carries the tint.
        let snap = Snapshot::parse(&[
            row("main", 1, "@2", &[("state", "running"), ("stash_sel", "1")]),
            row("main", 2, "@3", &[("stash_sel", "1")]),
            row("main", 3, "@4", &[]),
            client("main", "@3", &["stash"]),
        ].join("\n"));
        let bar = Bar::build(&snap, Some("/dev/ttys999"));
        assert_eq!(bar.selecting, Some(2));
        assert_eq!(bar.shells.iter().map(|s| s.selected).collect::<Vec<_>>(), [true, false]);
        // Armed with nothing flagged yet: live, 0; a leaked flag without the table: live too.
        let armed = Snapshot::parse(&[row("main", 1, "@2", &[]), client("main", "@2", &["stash"])].join("\n"));
        assert_eq!(Bar::build(&armed, Some("/dev/ttys999")).selecting, Some(0));
        let leaked = Snapshot::parse(&[row("main", 1, "@2", &[("stash_sel", "1")]), client("main", "@2", &["root"])].join("\n"));
        assert_eq!(Bar::build(&leaked, Some("/dev/ttys999")).selecting, Some(1));
        // The jump table; a hidden session has no shells and a dim chip.
        let snap = Snapshot::parse(&[row("agents", 1, "@9", &[]), client("agents", "@9", &["jump", "", "", "refused"])].join("\n"));
        let bar = Bar::build(&snap, Some("/dev/ttys999"));
        assert!(bar.jump && !bar.prefix && bar.shells.is_empty() && bar.usage.is_empty());
        assert!(bar.space.as_ref().is_some_and(|c| c.hidden));
        assert_eq!(bar.stale.as_deref(), Some("save refused"));
        // No client: no chip (the classic header), the global options still read.
        let bar = Bar::build(&snap, Some("/dev/nope"));
        assert_eq!((bar.space.is_none(), bar.stale.as_deref()), (true, Some("save refused")));
    }

    /// The meters' numbers: the numeric options when set, else the text
    /// (with or without a delta, a pooled `Σ`, `idle`, `--%`); the pace
    /// tick is used − delta when `_expected` is unset.
    #[test]
    fn usage_meters_read_numbers_then_text() {
        let m = crate::tmux::Meter::default();
        let u = Usage::read("W".into(), "60% (+17%)".into(), None, &m);
        assert_eq!((u.used, u.delta, u.expected, u.pooled, u.idle), (Some(60.0), Some(17), Some(43.0), false, false));
        let u = Usage::read("S".into(), "22% (Σ-87%)".into(), None, &m);
        // Unclamped: the pace says 109% (the meter clamps its tick, not the number).
        assert_eq!((u.used, u.delta, u.expected, u.pooled), (Some(22.0), Some(-87), Some(109.0), true));
        // Over the window: the real level stays, from the text or the option.
        let u = Usage::read("W".into(), "104% (+30%)".into(), None, &m);
        assert_eq!((u.used, u.expected), (Some(104.0), Some(74.0)));
        let over = crate::tmux::Meter { used: "104".into(), ..Default::default() };
        assert_eq!(Usage::read("W".into(), "--%".into(), None, &over).used, Some(104.0));
        let u = Usage::read("S".into(), "21%".into(), None, &m);
        assert_eq!((u.used, u.delta, u.expected), (Some(21.0), None, None));
        for t in ["idle", "--%", "junk"] {
            let u = Usage::read("S".into(), t.into(), None, &m);
            assert_eq!((u.used, u.expected, u.idle), (None, None, t == "idle"), "{t}");
        }
        // The numeric options win over the text.
        let m = crate::tmux::Meter { used: "60".into(), expected: "43".into(), delta: "+17".into(), resets: "1792569600".into(),
            members: "1:90:0,2:30:1".into() };
        let u = Usage::read("W".into(), "60% (+17%)".into(), None, &m);
        assert_eq!((u.used, u.expected, u.delta, u.resets, u.pooled), (Some(60.0), Some(43.0), Some(17), Some(1_792_569_600), true));
        assert_eq!(u.members, [Member { slot: Some(1), used: 90, active: false, cap: 100 },
            Member { slot: Some(2), used: 30, active: true, cap: 100 }]);
        // An account the script has no slot for (`:27:1`) still counts: the pool stays a pool.
        assert_eq!(parse_members("1:90:0,:27:1"), [Member { slot: Some(1), used: 90, active: false, cap: 100 },
            Member { slot: None, used: 27, active: true, cap: 100 }]);
        // The optional 4th field: the cap (100 − the reserve); junk or blank is 100.
        assert_eq!(parse_members("1:90:0:90,2:32:1:100"), [Member { slot: Some(1), used: 90, active: false, cap: 90 },
            Member { slot: Some(2), used: 32, active: true, cap: 100 }]);
        assert_eq!(parse_members("1:90:0:x,2:32:1:").iter().map(|m| m.cap).collect::<Vec<_>>(), [100, 100]);
        let pool = crate::tmux::Meter { used: "58".into(), members: "1:90:0,:27:1".into(), ..Default::default() };
        assert!(Usage::read("W".into(), "58%".into(), None, &pool).pooled);
        let one = crate::tmux::Meter { used: "27".into(), members: ":27:1".into(), ..Default::default() };
        assert!(!Usage::read("W".into(), "27%".into(), None, &one).pooled);
        // The numbers beat the text; a single account (no members) is no pool.
        let m = crate::tmux::Meter { used: "13".into(), expected: "51".into(), delta: "-38".into(), ..Default::default() };
        let u = Usage::read("S".into(), "99% (+1%)".into(), None, &m);
        assert_eq!((u.used, u.expected, u.delta, u.pooled), (Some(13.0), Some(51.0), Some(-38), false));
        assert!(parse_members("junk,3:x:1,4:5").is_empty());
        // Through the snapshot: the weekly window's five fields; the
        // session's empty (no data yet), so its text.
        let mut st = vec!["root", "", "", "", "21%", "green", "60% (+17%)", "red", "", "", "", "", "", "off"];
        st.extend(["", "", "", "", "", "61", "44", "+17", "1792569600", "1:90:0,2:30:1"]);
        let snap = Snapshot::parse(&[row("main", 1, "@2", &[]), client("main", "@2", &st)].join("\n"));
        let bar = Bar::build(&snap, Some("/dev/ttys999"));
        assert_eq!((bar.usage[1].used, bar.usage[1].expected, bar.usage[1].members.len()), (Some(61.0), Some(44.0), 2));
        assert_eq!((bar.usage[0].used, bar.usage[0].expected), (Some(21.0), None));
    }

    /// The key-table poll: nothing at all in root; started when a refresh
    /// sees a new non-root table; one refresh when the table changes (then
    /// nothing, back in root); bounded per table, whatever notices arrive.
    #[test]
    fn the_key_table_poll_stops_when_root_returns() {
        let t0 = Instant::now();
        let at = |ms: u64| t0 + Duration::from_millis(ms);
        let mut k = KeyPoll::default();
        // Root (or unknown): never scheduled.
        for table in ["root", "", "root", " root "] {
            k.seen(table, t0);
            assert_eq!(k.due(), None, "{table:?}");
        }
        // Into prefix: cheap reads every 150 ms while it holds.
        k.seen("prefix", t0);
        assert_eq!(k.due(), Some(at(150)));
        assert_eq!(k.read(Some("prefix"), at(150)), KeyStep::Wait);
        assert_eq!(k.due(), Some(at(300)));
        // Back to root: one refresh, which sees root and stops the poll.
        assert_eq!(k.read(Some("root\n"), at(300)), KeyStep::Refresh);
        assert_eq!(k.due(), None);
        k.seen("root", at(310));
        assert!(!k.polling() && k.due().is_none());
        // prefix → jump: a refresh, and a new bound for the new table.
        k.seen("prefix", at(1000));
        assert_eq!(k.read(Some("jump"), at(1150)), KeyStep::Refresh);
        k.seen("jump", at(1160));
        assert_eq!(k.due(), Some(at(1310)));
        // Another refresh mid-poll (a notice, the timer) just continues it.
        k.seen("jump", at(5000));
        assert_eq!(k.due(), Some(at(5150)));
        // Bounded: 10 s after entering jump it gives up, still in jump...
        assert_eq!(k.read(Some("jump"), at(11_159)), KeyStep::Wait);
        assert_eq!(k.read(Some("jump"), at(11_160)), KeyStep::Stop);
        assert_eq!(k.due(), None);
        // ...and refreshes in the same table never restart it.
        k.seen("jump", at(12_000));
        assert_eq!(k.due(), None);
        // A failed read (the client left): one refresh, then nothing.
        k.seen("root", at(13_000));
        k.seen("jump", at(13_000));
        assert_eq!(k.due(), Some(at(13_150)));
        assert_eq!(k.read(None, at(13_150)), KeyStep::Refresh);
        k.seen("jump", at(13_160));
        assert_eq!(k.due(), None);
        assert_eq!(key_table_command("/dev/ttys9"), ["display-message", "-p", "-c", "/dev/ttys9", "#{client_key_table}"]);
    }

    /// A long stay in ONE non-root table (prefix v's `stash` while agents
    /// keep posting notices) polls for 10 s and then never again, however
    /// many refreshes follow; leaving and re-entering it starts one anew.
    #[test]
    fn notices_in_one_table_never_extend_the_poll() {
        let t0 = Instant::now();
        let at = |ms: u64| t0 + Duration::from_millis(ms);
        let mut k = KeyPoll::default();
        k.seen("stash", t0);
        let mut reads = 0;
        let mut ms = 0;
        while ms < 60_000 {
            // A cheap read when due, a notice-driven refresh every 300 ms.
            if k.due().is_some_and(|d| d <= at(ms)) {
                reads += 1;
                k.read(Some("stash"), at(ms));
            }
            if ms % 300 == 0 {
                k.seen("stash", at(ms));
            }
            ms += 150;
        }
        assert_eq!(k.due(), None, "still polling after a minute in one table");
        assert!(reads > 0 && reads <= 10_000 / 150 + 1, "{reads} reads");
        // Out (a refresh saw root) and back in: a new poll.
        k.seen("root", at(60_000));
        k.seen("stash", at(60_100));
        assert_eq!(k.due(), Some(at(60_250)));
    }

    /// The status block never takes a dashboard pane's word for a shell's:
    /// with it active, the home shell is labelled by the shell's cwd.
    #[test]
    fn a_dashboard_never_names_the_home_shell() {
        let sep = crate::tmux::PATH_SEP.to_string();
        let snap = Snapshot::parse(&[
            {
                let r = row("main", 0, "@1", &[("home", "1"), ("pane_flags", "D0%1 p1%2 "), ("path", &format!("1 /tmp/proj{sep}"))]);
                r.replacen("zsh", "space-dash", 1)
            },
            row("main", 1, "@2", &[("active", "1")]),
            client("main", "@2", &["root"]),
        ].join("\n"));
        let bar = Bar::build(&snap, Some("/dev/ttys999"));
        let shells: Vec<_> = bar.shells.iter().map(|s| (s.n, s.label.as_str(), s.home, s.win.as_str())).collect();
        assert_eq!(shells[0], (1, "proj", true, "@1"));
        assert_eq!(shells.len(), 2);
    }

    /// `@toast` as notify.sh writes it; junk is no toast.
    #[test]
    fn toasts_parse() {
        // A 6-field record (from before titles): no title.
        let t = Toast::parse("12\t1792000000250\tok\t4\t/dev/ttys001\tparked  Canvas \u{7}Syllabus\n").unwrap();
        assert_eq!(t, Toast { seq: 12, at_ms: 1_792_000_000_250, kind: Kind::Ok, ttl: 4.0, client: "/dev/ttys001".into(),
            text: "parked Canvas Syllabus".into(), title: None });
        // The 7th field: the title (blank is none).
        let titled = Toast::parse("12\t1792000000250\tok\t4\t\tparked\tStash  \tx").unwrap();
        assert_eq!((titled.text.as_str(), titled.title.as_deref()), ("parked", Some("Stash x")));
        assert_eq!(Toast::parse("12\t1\tok\t4\t\tparked\t  ").unwrap().title, None);
        assert_eq!(t.id(), (12, 1_792_000_000_250));
        assert_eq!((t.until(), t.live(1_792_000_004.0), t.live(1_792_000_004.25)), (1_792_000_004.25, true, false));
        assert!(t.for_client(Some("/dev/ttys001")) && !t.for_client(Some("/dev/ttys002")) && !t.for_client(None));
        let all = Toast::parse("3\t1000\twarn\t1.5\t\tx\u{1b}[31m").unwrap();
        assert_eq!((all.kind, all.ttl, all.client.as_str(), all.text.as_str()), (Kind::Warn, 1.5, "", "x [31m"));
        assert!(all.for_client(None) && all.for_client(Some("/dev/ttys001")));
        // Kinds: anything unknown is info; a junk ttl is the default, a huge one capped.
        assert_eq!([Kind::parse("error"), Kind::parse("info"), Kind::parse("nope")], [Kind::Error, Kind::Info, Kind::Info]);
        assert_eq!(Toast::parse("1\t0\tok\tjunk\t\tx").unwrap().ttl, 4.0);
        assert_eq!(Toast::parse("1\t0\tok\t-3\t\tx").unwrap().ttl, 4.0);
        assert_eq!(Toast::parse("1\t0\tok\t99999\t\tx").unwrap().ttl, 3600.0);
        for junk in ["", "1\t0\tok\t4\t", "1\t0\tok\t4\t\t  ", "x\t0\tok\t4\t\thi", "1\tnow\tok\t4\t\thi", "1\t0\tok\t4"] {
            assert_eq!(Toast::parse(junk), None, "{junk:?}");
        }
        // The stack: RS-separated records, oldest first, junk ones skipped.
        let all = Toast::parse_all("1\t10\tok\t4\t\tone\u{1e}junk\u{1e}2\t20\twarn\t4\t\ttwo\tTitled\u{1e}");
        assert_eq!(all.iter().map(|t| (t.seq, t.text.as_str(), t.title.as_deref())).collect::<Vec<_>>(),
            [(1, "one", None), (2, "two", Some("Titled"))]);
        assert!(Toast::parse_all("").is_empty());
    }

    /// The snapshot's toast reaches the bar only for this sidebar's client,
    /// or for every sidebar.
    #[test]
    fn the_bar_takes_only_its_clients_toast() {
        let with = |raw: &str| {
            let mut st = vec![""; crate::tmux::STATUS_FORMATS.len()];
            st[29] = raw;
            Snapshot::parse(&[row("main", 1, "@2", &[]), client("main", "@2", &st)].join("\n"))
        };
        let seqs = |b: Bar| b.toasts.iter().map(|t| t.seq).collect::<Vec<_>>();
        let mine = with("5\t1000\tok\t4\t/dev/ttys999\thi");
        assert_eq!(seqs(Bar::build(&mine, Some("/dev/ttys999"))), [5]);
        assert!(Bar::build(&mine, Some("/dev/ttys001")).toasts.is_empty());
        assert!(Bar::build(&mine, None).toasts.is_empty());
        let every = with("6\t1000\terror\t8\t\thi");
        for c in [Some("/dev/ttys999"), Some("/dev/ttys001"), None] {
            assert_eq!(Bar::build(&every, c).toasts.iter().map(|t| (t.seq, t.kind)).collect::<Vec<_>>(), [(6, Kind::Error)], "{c:?}");
        }
        assert!(Bar::build(&with(""), Some("/dev/ttys999")).toasts.is_empty());
        // A stack: each record kept or not by its own client.
        let stack = with("5\t1000\tok\t4\t/dev/ttys999\ta\u{1e}6\t1001\tok\t4\t/dev/ttys001\tb\u{1e}7\t1002\twarn\t4\t\tc\tT");
        assert_eq!(seqs(Bar::build(&stack, Some("/dev/ttys999"))), [5, 7]);
        assert_eq!(seqs(Bar::build(&stack, Some("/dev/ttys001"))), [6, 7]);
    }

    #[test]
    fn registry_math() {
        let live = |p: i32| p != 13;
        let e = |p: i32, c: &str| (p, c.to_string());
        assert_eq!(parse_pids(" 12 x 13  -4 0 99 "), [12, 13, 99]);
        // The clients ride word for word beside the pids (junk keeps its
        // place; a missing word is no client).
        assert_eq!(parse_entries(" 12 x 13 ", "/dev/a /dev/x"), [e(12, "/dev/a"), e(13, "-")]);
        assert_eq!(parse_entries("12 13", "/dev/a - /dev/c"), [e(12, "/dev/a"), e(13, "-")]);
        assert_eq!((client_word(Some(" /dev/a ")), client_word(None), client_word(Some("")), client_word(Some("a b"))),
            ("/dev/a".into(), "-".into(), "-".into(), "-".into()));
        // Start: dead ones dropped, me added once, with my client.
        let prev = [e(12, "/dev/a"), e(13, "/dev/b"), e(99, "-")];
        assert_eq!(next_entries(&prev, &live, Some(&e(50, "/dev/c")), None), [e(12, "/dev/a"), e(99, "-"), e(50, "/dev/c")]);
        assert_eq!(next_entries(&[e(50, "-"), e(50, "-")], &live, Some(&e(50, "/dev/c")), None), [e(50, "/dev/c")]);
        // Re-registering (the client resolved): same place, new client.
        assert_eq!(next_entries(&[e(50, "-"), e(12, "/dev/a")], &live, Some(&e(50, "/dev/c")), None),
            [e(50, "/dev/c"), e(12, "/dev/a")]);
        // Exit: me removed, dead ones dropped.
        assert_eq!(next_entries(&[e(12, "/dev/a"), e(13, "-"), e(50, "/dev/c")], &live, None, Some(50)), [e(12, "/dev/a")]);
        assert_eq!(next_entries(&[e(13, "-")], &live, None, None), Vec::<Entry>::new());
        assert_eq!(write_command(&[e(12, "/dev/a"), e(50, "-")]), ["set-option", "-gq", "@sidebar_pids", "12 50", ";",
            "set-option", "-gq", "@sidebar_clients", "/dev/a -", ";", "set-option", "-gq",
            "@sidebar_open", "2", ";", "set-option", "-gq", "status", "off"]);
        assert_eq!(write_command(&[]), ["set-option", "-gqu", "@sidebar_pids", ";", "set-option", "-gqu", "@sidebar_clients", ";",
            "set-option", "-gq", "@sidebar_open", "0", ";", "set-option", "-gq", "status", "on"]);
        // The cheap check.
        let st = |pids: &str, clients: &str, open: &str| crate::tmux::Status {
            sidebar_pids: pids.into(),
            sidebar_clients: clients.into(),
            sidebar_open: open.into(),
            status_line: "off".into(),
            ..Default::default()
        };
        assert!(!needs_repair(&st("12 50", "/dev/a /dev/c", "2"), 50, "/dev/c", &live));
        assert!(needs_repair(&st("12", "/dev/a", "1"), 50, "/dev/c", &live)); // me missing (server restarted, say)
        assert!(needs_repair(&st("12 50", "/dev/a -", "2"), 50, "/dev/c", &live)); // my client resolved since
        assert!(needs_repair(&st("12 50", "/dev/a", "2"), 50, "/dev/c", &live)); // the clients cut short
        assert!(!needs_repair(&st("12 50", "/dev/a", "2"), 50, "-", &live)); // ...but none to tell
        assert!(needs_repair(&st("13 50", "- /dev/c", "2"), 50, "/dev/c", &live)); // a crashed one
        assert!(needs_repair(&st("50", "/dev/c", "3"), 50, "/dev/c", &live)); // the count disagrees
        let shown = crate::tmux::Status { status_line: "on".into(), ..st("50", "/dev/c", "1") };
        assert!(needs_repair(&shown, 50, "/dev/c", &live)); // a config reload showed the line again
        // This test process is alive but not agent-ui.
        assert!(term::alive(std::process::id() as i32));
    }

    #[test]
    fn status_jobs_are_found_like_tmux() {
        let right = "#(/x/continuum_save.sh)#{?#{@resurrect_stale},a,b} S: #{?@t,#{@t},--%%}#(#{HOME}/s.sh --tick >/dev/null 2>&1 || true)x#(#{HOME}/s.sh --tick >/dev/null 2>&1 || true)#(echo (a) b)#(broken";
        assert_eq!(status_jobs(right), ["/x/continuum_save.sh", "#{HOME}/s.sh --tick >/dev/null 2>&1 || true", "echo (a) b"]);
        assert!(status_jobs("#{b:pane_current_path} %H:%M").is_empty());
        assert_eq!(run_command(&["a".into(), "b c # x".into()]), ["run-shell", "-b", "{ a\n} </dev/null >/dev/null 2>&1; true", ";",
            "run-shell", "-b", "{ b c # x\n} </dev/null >/dev/null 2>&1; true"]);
    }
}
