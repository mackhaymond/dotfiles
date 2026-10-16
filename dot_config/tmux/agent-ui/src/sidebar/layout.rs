//! The sidebar's pure half: a [`ViewModel`] and a size in, every row to draw
//! (each exactly `width` cells, with what a click on each cell does) out. No
//! I/O, so every density rule is unit-tested.
//!
//! ## Shape: a status card, NEEDS YOU, the agents (the body), the shells, the spaces
//!
//! ```text
//! ╭─ prefix ───────────────────────────╮  the card (a ratatui Block): the key
//! │ S ━━━━━━━━┃━━──────────  21% (-4%) │  table's mark in its top border, one
//! │ W ━━━━━━━━━━━━━┃━──────  60% (+17%)│  usage meter per window (fill = used,
//! ╰────────────────────────────────────╯  ┃ = where the pace expects it)
//!
//!  NEEDS YOU                          1
//! ▌ g  1Password Sign-ins       ✓ 4m      tinted cards, a state-coloured edge
//! ▌    Your four calls are done
//!
//!  AGENTS                    all spaces   (a click flips the scope)
//!  ● main                     ◐2 ○3       space header: dot in @space_color
//! ▎ d  Tmux Agent Sidebar   ⠹ working     the client's agent: highlighted row
//!      run cargo test             26s     every agent two lines: a dim second
//!   q  Pause Resume Agents      ✓ done     line (the message; for idle, its
//!      Merged the pause key       2m      last outcome; maybe blank), the
//!   h  Canvas Syllabus            idle    age at its end
//!      Synced 4 assignments       1h
//!
//!  SHELLS                                 the client's space's shells, one
//!   ⌥1  ⌂ ~                               row each (⌥N); the active one
//! ▎ ⌥2  nvim                              lifted; one line of pills when short
//!
//!  ──────────────────────────────────
//!   1 main   2 bai   3 cs35l  4 sched     spaces as pills (current filled),
//!  ⏸ 4 parked                             ⌃N order, wrapping; then the stash
//! ```
//!
//! Without a [`Bar`] (no client space, no usage, nothing selected) the top
//! is the classic ` agents` / ` space` title with the counts.
//!
//! ## Toasts: in the card's bottom border, so nothing moves
//!
//! notify.sh's messages (`@toast`, [`Toast`]) are drawn where the tmux
//! status line would have shown them, without bringing it back: in the
//! card's BOTTOM border, the way the key-table marks sit in its top one
//! ([`Frame::card_footer`]), glyph and colour by kind:
//!
//! ```text
//! ╰─ ✓ parked Canvas Syllabus ───────╯   ok ✓ green, info • blue,
//!                                        warn ! peach, error ✕ red
//! ```
//!
//! Clipped with `…`, the corner kept. A text that would lose more than a
//! few cells also shows whole, wrapped into up to 2 rows inside the card
//! just above the border (the one case that moves rows). No card (a short
//! frame, nothing in it): a tinted row of its own at the very top. A click
//! on it dismisses it in this sidebar ([`Target::Toast`]); it expires at
//! its deadline ([`Frame::toast_until`]) by the frame's clock.
//!
//! ## Density ladder: agents lose detail last; never scroll, never drop an agent silently
//!
//! The agents section is the body. A frame takes the first step at which
//! every agent still gets its full detail ([`STEPS`]):
//!
//! 1. the spaces section gets the rows left over: all its pill lines, else
//!    fewer (the client's space and those with an attention agent kept,
//!    `+K` for the rest). Its rule and one pill line are held back before
//!    anything else is laid out ([`spaces_min`]);
//! 2. the blank gaps go (between sections, between cards, between spaces);
//! 3. NEEDS YOU cards go from 2 lines to 1;
//! 4. only then do the agents compress (below), NEEDS YOU kept to the rows
//!    the agents' one-line shape leaves (at least 2, and at least a third of
//!    the body), cut with a `… N more need you` line.
//!
//! At full detail a hairline (`surface0`, [`sep_row`]) separates one agent
//! of a space from the next; the hairlines go after the gaps and card
//! lines, before any agent loses its second line.
//!
//! The agents' own ladder keeps every agent row the same height: the idle
//! agents of a space fold into one `○○○ 3 idle · Name, Name…` line; then
//! every agent goes to one line (age beside its state), unfolded, then
//! folded again; then whole spaces collapse to their header (with its
//! counts), fewest attention agents first; finally a `… N more` line.
//!
//! Every agents-section row records the agents it stands for
//! ([`Row::covers`]), so a test can prove each agent is shown, folded,
//! collapsed or counted.
//!
//! ## Narrow widths
//!
//! Under 30 columns the ages go, under 26 the second lines, then the state
//! word shrinks to its glyph; titles take what is left and are clipped. A row
//! is always exactly the frame's width: nothing wraps.
//!
//! ## Letters
//!
//! Every agent row and NEEDS YOU card starts with the agent's lifetime
//! letter (`@agent_key`, assigned by [`crate::menu::letters::of_view`], the
//! menu's own function) as a key-cap ([`Badge`]); peach while ⌥W's jump
//! table is armed. Agents without one leave it blank; with no letter
//! anywhere the column is not drawn.
//!
//! ## Scope
//!
//! [`crate::view::Scope`] (`@spaces` / `@strip_scope`): in SPACE scope the
//! agents section shows only the client's session (its header and ` no
//! agents in this space` when it has none). NEEDS YOU, the spaces and parked
//! stay global. The AGENTS header's scope word is the toggle once the scope
//! is a thing.
//!
//! ## Space order
//!
//! ONE order everywhere: `@space_index` (⌃N), spaces without one after, by
//! name ([`space_order`]). The agents section's groups and the spaces pills
//! both sort by it.
//!
//! ## Colour
//!
//! The canvas is the terminal's own background ([`Color::Reset`]), so the
//! split is WezTerm's colour. Surfaces are subtle and few: the client's agent
//! row (`surface0`), NEEDS YOU cards (their state colour mixed over the
//! canvas, [`Ink::Mix`]), the stash selection (mauve, mixed), and the solid
//! chips (key-caps, the key-table marks, pills).

use super::status::{ink_on, tmux_colour, Bar, Kind, Shell, Toast, ToastId, Usage};
use crate::actions::watcher_problem;
use crate::model::{Cat, Counts, Flag};
use crate::palette;
use crate::text::{clip, dwidth, fit_label, plain, sanitize};
use crate::view::{Agent, Need, Space, ViewModel};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::widgets::{Block, BorderType, Widget};
use std::cmp::Reverse;
use std::ops::Range;
use unicode_width::UnicodeWidthStr;

/// A palette colour name ([`palette::HEX`] or [`palette::UI`]).
pub type Hue = &'static str;

/// A colour a segment is drawn in: a palette name, one a tmux option
/// spelled (`@space_color`'s `#rrggbb`, the usage modules' `colour239`), or
/// a palette colour mixed over the canvas (a tinted surface).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Ink {
    Pal(Hue),
    Rgb(u8, u8, u8),
    Idx(u8),
    /// The colour at this percent over the canvas ([`palette::mix`]).
    Mix(Hue, u8),
}

impl Ink {
    pub fn color(self) -> Color {
        match self {
            Ink::Pal(p) => palette::color(p),
            Ink::Rgb(r, g, b) => Color::Rgb(r, g, b),
            Ink::Idx(n) => Color::Indexed(n),
            Ink::Mix(p, pct) => palette::mix(p, pct),
        }
    }
}

impl From<Hue> for Ink {
    fn from(h: Hue) -> Ink {
        Ink::Pal(h)
    }
}

/// A run of text in one style.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Seg {
    pub text: String,
    pub fg: Ink,
    /// None: the row's background.
    pub bg: Option<Ink>,
    pub bold: bool,
}

fn seg(text: impl Into<String>, fg: impl Into<Ink>) -> Seg {
    Seg { text: text.into(), fg: fg.into(), bg: None, bold: false }
}

fn bold(text: impl Into<String>, fg: impl Into<Ink>) -> Seg {
    Seg { bold: true, ..seg(text, fg) }
}

/// A solid block: `text` in `fg` on `bg`, bold.
fn block(text: impl Into<String>, fg: impl Into<Ink>, bg: impl Into<Ink>) -> Seg {
    Seg { bg: Some(bg.into()), ..bold(text, fg) }
}

/// A pill cell: `text` in `fg` on `bg`, not bold.
fn on(text: impl Into<String>, fg: impl Into<Ink>, bg: impl Into<Ink>) -> Seg {
    Seg { bg: Some(bg.into()), ..seg(text, fg) }
}

fn width(segs: &[Seg]) -> usize {
    segs.iter().map(|s| dwidth(&s.text)).sum()
}

/// At most `room` cells; the segment that does not fit is cut with `…`
/// ([`clip`]) and nothing after it is kept.
fn clip_segs(segs: Vec<Seg>, room: usize) -> Vec<Seg> {
    let mut out = Vec::new();
    let mut used = 0;
    for s in segs {
        let w = dwidth(&s.text);
        if used + w <= room {
            used += w;
            out.push(s);
            continue;
        }
        let left = room - used;
        if left > 0 {
            out.push(Seg { text: clip(&s.text, left), ..s });
        }
        break;
    }
    out
}

/// One line exactly `w` cells wide, `left` clipped to leave room for
/// `right`, the gap filled with `fill` (a space by default).
fn fit(w: usize, left: Vec<Seg>, right: Vec<Seg>, fill: Option<(char, Hue)>) -> Vec<Seg> {
    let right = clip_segs(right, w);
    let rw = width(&right);
    let mut out = clip_segs(left, w - rw);
    let pad = w - width(&out) - rw;
    if pad > 0 {
        let (c, hue) = fill.unwrap_or((' ', "text"));
        out.push(seg(c.to_string().repeat(pad), hue));
    }
    out.extend(right);
    out
}

/// What a click on a cell does.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Target {
    /// An agent row or a needs card: agent-jump.sh goto (`Actions::go`,
    /// Strip.click "goto").
    Go { win: String, session: String },
    /// A space pill or header: that session's active window (Strip.click "session").
    Session(String),
    /// One of this space's shells in the SHELLS section (⌥N): that window, like an
    /// agent row, but never parked by a right-click.
    Shell { win: String, session: String },
    /// agent-jump.sh next / back (no button draws them any more; kept for
    /// the click handler).
    Next,
    Back,
    /// The agents menu (Strip.open_menu), optionally on a tab ("all", "idle").
    Menu(Option<&'static str>),
    /// The `⏸ N parked` line: the stash picker, exactly prefix h's
    /// `stash.sh unstash '' <client>`.
    Stash,
    /// The red watcher warning: restart it (the strip's ⟳, Strip.click "restart").
    Restart,
    /// The scope word (or the classic title): flip the scope (space / all) and
    /// persist it in `@strip_scope` (Strip.toggle_scope).
    Scope,
    /// A toast ([`Toast::id`]): dismissed, in this sidebar only.
    Toast(ToastId),
}

/// One screen line.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Row {
    /// Exactly the frame's width in cells.
    pub segs: Vec<Seg>,
    /// The row's background (None: the terminal's own background).
    pub bg: Option<Ink>,
    /// Click targets, `[x0, x1)` in 0-based cells (Strip.targets).
    pub targets: Vec<(u16, u16, Target)>,
    /// The agents, as (session, window id), this row stands for in the
    /// agents section: shown, folded, collapsed into a header, or counted.
    pub covers: Vec<(String, String)>,
    /// The row draws a spinner frame (a working agent): the frame animates.
    pub anim: bool,
}

impl Row {
    fn new(segs: Vec<Seg>) -> Row {
        Row { segs, ..Row::default() }
    }

    fn blank(w: usize) -> Row {
        Row::new(vec![seg(" ".repeat(w), "text")])
    }

    fn bg(mut self, bg: Option<Ink>) -> Row {
        self.bg = bg;
        self
    }

    /// The whole row does `t`.
    fn click(mut self, w: usize, t: Target) -> Row {
        self.targets.push((0, w as u16, t));
        self
    }

    fn covering<'a>(mut self, agents: impl IntoIterator<Item = &'a Agent>) -> Row {
        self.covers.extend(agents.into_iter().map(|a| (a.session.clone(), a.window_id.clone())));
        self
    }

    /// The row's text, styles dropped.
    pub fn text(&self) -> String {
        self.segs.iter().map(|s| s.text.as_str()).collect()
    }

    /// The target drawn at cell `x` (Strip.target).
    pub fn target_at(&self, x: u16) -> Option<&Target> {
        self.targets.iter().find(|(x0, x1, _)| (*x0..*x1).contains(&x)).map(|(_, _, t)| t)
    }
}

/// One whole screen.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Frame {
    pub width: u16,
    pub height: u16,
    /// Exactly `height` rows.
    pub rows: Vec<Row>,
    /// The ladder step the frame settled on: 0..STEPS.len() an entry of
    /// [`STEPS`] (agents at full detail), STEPS.len() the agents compressed.
    pub step: u8,
    /// The agents section's own ladder step (0 = full detail).
    pub agents_step: u8,
    /// The card's content rows (the selection hint, usage, the stale save;
    /// the key-table marks without a border): empty without a [`Bar`].
    pub status: Range<usize>,
    /// The rounded card's rows, borders included ([`paint`] draws the
    /// Block over them); empty when the frame is too short for one.
    pub card: Range<usize>,
    /// What [`paint`] writes into the card's top border from its third
    /// cell: the key-table marks ([`marks`]).
    pub card_title: Vec<Seg>,
    /// What [`paint`] writes into the card's BOTTOM border from its third
    /// cell: a toast ([`toast_footer`]).
    pub card_footer: Vec<Seg>,
    /// The rows a toast is drawn on: the card's bottom border (and the rows
    /// a long one wraps into above it), or its own row at the top.
    pub toast: Range<usize>,
    /// When the toast on screen expires (epoch milliseconds, rounded up):
    /// the sidebar wakes then to draw the frame without it.
    pub toast_until: Option<i64>,
    /// The rows of each section, header included (empty: not drawn).
    pub needs: Range<usize>,
    pub agents: Range<usize>,
    /// This space's shells: the section, or one line of pills.
    pub shells: Range<usize>,
    /// The rule and the space pills.
    pub spaces: Range<usize>,
    /// A spinner is on screen: the sidebar redraws it every
    /// [`crate::sidebar::SPIN_EVERY`] (and only then).
    pub spin: bool,
}

impl Frame {
    /// The target at 0-based cell (x, y), if any.
    pub fn target(&self, x: u16, y: u16) -> Option<&Target> {
        self.rows.get(y as usize).and_then(|r| r.target_at(x))
    }
}

/// What the layout needs besides the view.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Opts {
    /// A transient message for the last row (Roster.say).
    pub msg: Option<String>,
    /// What the hidden tmux status line showed ([`Bar`]); the default (no
    /// client space) draws the classic header and no card.
    pub bar: Bar,
    /// The spinner frame ([`SPIN`] index; any number, taken modulo).
    pub spin: usize,
    /// The clock ages are shown against (epoch seconds); None: the view's
    /// snapshot time. The live sidebar passes the time of each frame.
    pub now: Option<f64>,
}

/// The working spinner: braille, one cell, ten frames.
pub const SPIN: [&str; 10] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

/// How long a finished turn counts as "recently done" (its message stays
/// on a second line, its state word says done) in seconds.
pub const RECENT: f64 = 600.0;

/// A state's colour in the sidebar: CAT_HUE, working the steady blue.
pub fn cat_hue(c: Cat) -> Hue {
    match c {
        Cat::Failed => "red",
        Cat::NeedsInput => "yellow",
        Cat::Done => "green",
        Cat::Working => "blue",
        Cat::Idle => "overlay",
    }
}

/// `✕1 ◉2 ◐4`: nonzero only, each in its state colour.
fn count_segs(c: &Counts) -> Vec<Seg> {
    let mut v = Vec::new();
    for (i, (k, n)) in c.nonzero().into_iter().enumerate() {
        if i > 0 {
            v.push(seg(" ", "text"));
        }
        v.push(seg(format!("{}{n}", k.glyph()), cat_hue(k)));
    }
    v
}

fn dim_line(w: usize, text: &str) -> Row {
    Row::new(fit(w, vec![seg(text, "overlay")], vec![], None))
}

/// Dim text: `overlay` on the canvas, `overlay2` on a surface (a pill, a
/// card, a lifted row), where `overlay` would not read.
fn dim(on_surface: bool) -> Hue {
    if on_surface {
        "overlay2"
    } else {
        "overlay"
    }
}

/// An agent's age at the frame's clock (`2m`), so a frame laid out between
/// snapshots (the spinner's) shows the true age.
fn age_of(a: &Agent, cx: Cx) -> String {
    crate::text::ago(a.since, cx.now)
}

/// Everything the row builders share: the width, the letter column, the
/// clock and the spinner frame.
#[derive(Debug, Clone, Copy)]
struct Cx {
    w: usize,
    b: Badge,
    now: f64,
    spin: usize,
}

impl Cx {
    /// Ages are drawn (30 columns and up).
    fn ages(self) -> bool {
        self.w >= 30
    }

    /// Second lines are drawn (26 columns and up).
    fn seconds(self) -> bool {
        self.w >= 26
    }
}

// ---------------------------------------------------------------- the top

/// Whether there is a card at all (a client space, usage, a stale save or a
/// selection to show). Without one the frame has the classic title.
fn bar_shown(bar: &Bar) -> bool {
    bar.space.is_some() || !bar.usage.is_empty() || bar.stale.is_some() || bar.selecting.is_some()
}

/// The classic title (no client space): ` agents` / ` space`, the scope's
/// counts on the right; the title toggles the scope once that is a thing.
fn title_row(view: &ViewModel, w: usize) -> Row {
    let (title, counts) = match view.scope.session {
        Some(_) => (" space", &view.scope.counts),
        None => (" agents", &view.counts),
    };
    let mut right = count_segs(counts);
    right.push(seg(" ", "text"));
    let mut row = Row::new(fit(w, vec![bold(title, "text")], right, None));
    let end = dwidth(title).min(w) as u16;
    if view.scope.toggles() && end > 1 {
        row.targets.push((1, end, Target::Scope));
    }
    row
}

/// Something a flowing line holds: a chip or pill, and what a click on it does.
#[derive(Debug, Clone)]
struct Item {
    segs: Vec<Seg>,
    target: Option<Target>,
}

fn item(segs: Vec<Seg>, target: Option<Target>) -> Item {
    Item { segs, target }
}

/// `items` left to right from column 1, one cell apart, over at most
/// `lines` rows of `w` cells (the last column kept free); an item wider
/// than a whole line is clipped. None when they do not fit.
fn place(items: &[Item], w: usize, lines: usize) -> Option<Vec<Row>> {
    let end = w.saturating_sub(1).max(1);
    type Line = (Vec<Seg>, Vec<(u16, u16, Target)>, usize);
    let fresh = || -> Line { (vec![seg(" ", "text")], Vec::new(), 1) };
    let mut out: Vec<Line> = vec![fresh()];
    for it in items {
        let cw = width(&it.segs);
        loop {
            let line = out.last_mut().expect("a line");
            let empty = line.2 == 1;
            let x0 = line.2 + usize::from(!empty);
            if x0 + cw <= end || empty {
                if !empty {
                    line.0.push(seg(" ", "text"));
                }
                let segs = clip_segs(it.segs.clone(), end.saturating_sub(x0));
                let x1 = x0 + width(&segs);
                if let Some(t) = it.target.clone().filter(|_| x1 > x0) {
                    line.1.push((x0 as u16, x1 as u16, t));
                }
                line.0.extend(segs);
                line.2 = x1;
                break;
            }
            if out.len() >= lines {
                return None;
            }
            out.push(fresh());
        }
    }
    let w = w.max(1);
    Some(out.into_iter().map(|(segs, targets, _)| Row { targets, ..Row::new(fit(w, clip_segs(segs, w), vec![], None)) }).collect())
}

/// `fixed` (never dropped) then `items`, in at most `lines` rows. When they
/// do not fit, items go (the last of `rank`, most important first, before
/// the others) and `more(k)` stands for the k gone. → (rows, items shown).
fn pack(fixed: &[Item], items: &[Item], rank: &[usize], w: usize, lines: usize, more: &dyn Fn(usize) -> Item) -> (Vec<Row>, usize) {
    if lines == 0 {
        return (Vec::new(), 0);
    }
    let mut shown = vec![true; items.len()];
    let mut drop = rank.iter().rev();
    loop {
        let gone = shown.iter().filter(|s| !**s).count();
        let mut seq: Vec<Item> = fixed.to_vec();
        seq.extend(items.iter().zip(&shown).filter(|(_, s)| **s).map(|(i, _)| i.clone()));
        if gone > 0 {
            seq.push(more(gone));
        }
        if let Some(rows) = place(&seq, w, lines) {
            return (rows, items.len() - gone);
        }
        match drop.next() {
            Some(&i) => shown[i] = false,
            None => {
                let mut rows = place(&seq, w, usize::MAX).unwrap_or_default();
                rows.truncate(lines);
                return (rows, 0);
            }
        }
    }
}

/// THE shell label: `⌥N` (Option+N, space.sh's `shell N`; none past 9).
fn shell_key(s: &Shell) -> Option<String> {
    (s.n <= 9).then(|| format!("⌥{}", s.n))
}

/// A shell as a pill: ` ⌥2 nvim `. The active one lifted (`surface1`, a
/// peach key, bold), a selected one (`@stash_sel`) on the stash's mauve.
fn shell_pill(s: &Shell) -> Item {
    let label = format!("{}{}", if s.home { "⌂" } else { "" }, clip(&s.label, 12));
    let (bg, kf, lf) = if s.selected {
        ("mauve", "crust", "crust")
    } else if s.active {
        ("surface1", "peach", "text")
    } else {
        ("surface0", "overlay2", "sub")
    };
    let key = shell_key(s).map_or(" ".to_string(), |k| format!(" {k} "));
    let segs = vec![on(key, kf, bg), Seg { bold: s.active, ..on(format!("{label} "), lf, bg) }];
    item(segs, Some(Target::Shell { win: s.win.clone(), session: s.session.clone() }))
}

/// The key-table marks: a red ` prefix ` while the client's key table is
/// `prefix`; a peach ` ⌥W jump ` and `type a letter` while it is `jump`.
/// They sit in the card's top border ([`Frame::card_title`]), so arming a
/// table never changes the card's height; a frame too short for the border
/// gives them a row of their own. No space chip: the spaces' pills at the
/// bottom already mark the client's space.
fn marks(bar: &Bar) -> Vec<Seg> {
    if bar.prefix {
        vec![block(" prefix ", "crust", "red")]
    } else if bar.jump {
        vec![block(" ⌥W jump ", "crust", "peach"), bold(" type a letter ", "yellow")]
    } else {
        Vec::new()
    }
}

/// prefix v's range selection is live: ` N selected ` on mauve and stash.sh's
/// real keys (tmux.conf.tmpl's `stash` table), as many as fit.
fn hint_row(n: usize, w: usize) -> Row {
    let chip = block(format!(" {n} selected "), "crust", "mauve");
    let room = w.saturating_sub(2 + dwidth(&chip.text));
    let keys = ["h/l extend · ⏎ park · m move · esc cancel", "h/l · ⏎ park · m move · esc", "h/l ⏎ park esc", "h/l ⏎ esc"]
        .into_iter()
        .find(|k| dwidth(k) < room)
        .unwrap_or("");
    Row::new(fit(w, vec![seg(" ", "text"), chip, seg(format!(" {keys}"), "sub")], vec![], None))
}

/// A signed delta as the meter prints it, the way the status bar did:
/// `(+17%)`, `(-4%)`, `(±0%)`. A pooled pace reads the same.
fn delta_text(u: &Usage) -> String {
    let Some(d) = u.delta else { return String::new() };
    let sign = match d.signum() {
        1 => "+",
        -1 => "-",
        _ => "±",
    };
    format!("({sign}{}%)", d.unsigned_abs())
}

/// Time until `at` as the meter's reset hint says it: `45m`, `3h`, `2d`.
fn until(at: i64, now: f64) -> Option<String> {
    let s = (at as f64 - now).floor();
    if s <= 0.0 {
        return None;
    }
    let s = s as i64;
    Some(match s {
        0..=3599 => format!("{}m", (s / 60).max(1)),
        3600..=172_799 => format!("{}h", s / 3600),
        _ => format!("{}d", s / 86_400),
    })
}

/// One usage window as a meter: its label in its colour (`@codex_<w>_color`),
/// a thin bar filled to the used percent with a tick (`┃`) where the pace
/// expects it, the percent, the signed delta (peach when over pace, else
/// dim) and, when it fits, when the window resets (`↻3h`, dim). The reset
/// goes first, then the delta, when the bar would be too short.
fn meter_row(u: &Usage, w: usize, now: f64) -> Row {
    let hue = u.color.unwrap_or(Ink::Pal("overlay"));
    let label = clip(&sanitize(&u.label), 6);
    let pct = if u.idle {
        "idle".to_string()
    } else {
        u.used.map_or("--".to_string(), |v| format!("{v:.0}%"))
    };
    let delta = delta_text(u);
    let over = u.delta.is_some_and(|d| d > 0);
    let resets = u.resets.and_then(|r| until(r, now)).map(|r| format!("↻{r}"));
    let lw = dwidth(&label) + 2;
    let build = |with_delta: bool, with_reset: bool| -> Vec<Seg> {
        let mut r = vec![bold(format!(" {pct:>4}"), "text")];
        if with_delta && !delta.is_empty() {
            r.push(seg(format!(" {delta:>6}"), if over { "peach" } else { "overlay" }));
        }
        if let Some(rs) = resets.as_ref().filter(|_| with_reset) {
            r.push(seg(format!(" {rs:>4}"), "overlay"));
        }
        r.push(seg(" ", "text"));
        r
    };
    let mut right = build(true, true);
    if w.saturating_sub(lw + width(&right)) < 21 {
        right = build(true, false);
    }
    if w.saturating_sub(lw + width(&right)) < 8 {
        right = build(false, false);
    }
    let bw = w.saturating_sub(lw + width(&right) + 1);
    let mut left = vec![seg(" ", "text"), bold(label, hue), seg(" ", "text")];
    if bw >= 3 {
        // The fill and tick are clamped to the bar; the percent is not.
        let cells = |v: f64| ((v.clamp(0.0, 100.0) / 100.0) * bw as f64).round() as usize;
        let filled = u.used.map_or(0, cells).min(bw);
        let tick = u.expected.map(|e| cells(e).min(bw - 1));
        let mut runs: Vec<Seg> = Vec::new();
        for i in 0..bw {
            let (c, ink): (&str, Ink) = if Some(i) == tick {
                ("┃", Ink::Pal("text"))
            } else if i < filled {
                ("━", hue)
            } else {
                ("─", Ink::Pal("surface1"))
            };
            match runs.last_mut() {
                Some(r) if r.fg == ink => r.text.push_str(c),
                _ => runs.push(seg(c, ink)),
            }
        }
        left.extend(runs);
    }
    Row::new(fit(w, left, right, None))
}

/// Usage in one line for a shorter frame: ` S 21%  W 60%  F 2%`.
fn usage_row(bar: &Bar, w: usize) -> Option<Row> {
    if bar.usage.is_empty() {
        return None;
    }
    let mut v = vec![seg(" ", "text")];
    for (i, u) in bar.usage.iter().enumerate() {
        if i > 0 {
            v.push(seg("  ", "text"));
        }
        v.push(bold(format!("{} ", clip(&sanitize(&u.label), 6)), u.color.unwrap_or(Ink::Pal("overlay"))));
        let pct = if u.idle { "idle".into() } else { u.used.map_or("--".to_string(), |x| format!("{x:.0}%")) };
        v.push(seg(pct, "sub"));
    }
    Some(Row::new(fit(w, v, vec![], None)))
}

/// The red stale-save chip of status-right (`@resurrect_stale`).
fn stale_row(bar: &Bar, w: usize) -> Option<Row> {
    let s = bar.stale.as_ref()?;
    Some(Row::new(fit(w, vec![seg(" ", "text"), block(" ! ", "crust", "red"), seg(format!(" {s}"), "red")], vec![], None)))
}

/// `row` (built `w - 2` wide) moved one cell right inside the card's border.
fn inset(row: Row, w: usize) -> Row {
    let mut segs = vec![seg(" ", "text")];
    segs.extend(row.segs);
    let w16 = w as u16;
    let targets = row
        .targets
        .into_iter()
        .map(|(a, b, t)| (a + 1, (b + 1).min(w16.saturating_sub(1)), t))
        .filter(|(a, b, _)| a < b)
        .collect();
    Row { segs: fit(w, segs, vec![], None), targets, ..row }
}

// ---------------------------------------------------------------- toasts

/// A toast's glyph and colour: ok `✓` green, info `•` blue, warn `!`
/// peach, error `✕` red.
fn toast_look(k: Kind) -> (&'static str, Hue) {
    match k {
        Kind::Ok => ("✓", "green"),
        Kind::Info => ("•", "blue"),
        Kind::Warn => ("!", "peach"),
        Kind::Error => ("✕", "red"),
    }
}

/// How many cells a toast's text may lose to the border's `…` before it
/// also gets rows of its own ([`toast_rows`]).
const TOAST_SLACK: usize = 4;

/// The toast in the bottom border of a card `w` wide: ` ✓ text ` from its
/// third cell (the glyph bold in the kind's colour, the text in `text`),
/// clipped with `…` so a rule cell and the corner stay: `╰─ ✓ parked Canvas
/// Syllabus ─────╯` → (segs, the text lost more than [`TOAST_SLACK`] cells).
fn toast_footer(t: &Toast, w: usize) -> (Vec<Seg>, bool) {
    let (glyph, hue) = toast_look(t.kind);
    let room = w.saturating_sub(5 + 4);
    let segs = vec![seg(" ", "text"), bold(glyph, hue), seg(" ", "text"), seg(clip(&t.text, room), "text"), seg(" ", "text")];
    (clip_segs(segs, w.saturating_sub(5)), dwidth(&t.text) > room + TOAST_SLACK)
}

/// `text` in lines of at most `width` cells, broken at spaces (a word
/// longer than a line is cut); at most `max` lines, the last ending in `…`
/// when text is left over.
fn wrap(text: &str, width: usize, max: usize) -> Vec<String> {
    let words: Vec<&str> = text.split(' ').filter(|w| !w.is_empty()).collect();
    let mut lines: Vec<String> = Vec::new();
    let mut cur = String::new();
    let mut i = 0;
    while i < words.len() {
        if lines.len() + 1 >= max.max(1) {
            // The last line: everything left, cut.
            let rest = words[i..].join(" ");
            lines.push(clip(&if cur.is_empty() { rest } else { format!("{cur} {rest}") }, width));
            return lines;
        }
        let next = if cur.is_empty() { words[i].to_string() } else { format!("{cur} {}", words[i]) };
        if dwidth(&next) <= width {
            cur = next;
            i += 1;
        } else if cur.is_empty() {
            lines.push(clip(words[i], width));
            i += 1;
        } else {
            lines.push(std::mem::take(&mut cur));
        }
    }
    if !cur.is_empty() {
        lines.push(cur);
    }
    lines
}

/// A long toast's whole text inside the card, just above its bottom border
/// (rows `w` wide, for [`inset`]): the glyph, then the text wrapped into at
/// most 2 rows. Each row dismisses it.
fn toast_rows(t: &Toast, w: usize) -> Vec<Row> {
    let (glyph, hue) = toast_look(t.kind);
    wrap(&t.text, w.saturating_sub(4), 2)
        .into_iter()
        .enumerate()
        .map(|(i, l)| {
            let mut left = if i == 0 { vec![seg(" ", "text"), bold(glyph, hue), seg(" ", "text")] } else { vec![seg("   ", "text")] };
            left.push(seg(l, "text"));
            Row::new(fit(w, left, vec![], None)).click(w, Target::Toast(t.id()))
        })
        .collect()
}

/// A toast on a row of its own, when there is no card to carry it: the
/// glyph and the text on the kind's colour mixed over the canvas (like a
/// NEEDS YOU card). The row dismisses it.
fn toast_line(t: &Toast, w: usize) -> Row {
    let (glyph, hue) = toast_look(t.kind);
    let left = vec![seg(" ", "text"), bold(glyph, hue), seg(" ", "text"), seg(t.text.clone(), "text")];
    Row::new(fit(w, left, vec![seg(" ", "text")], None)).bg(Some(Ink::Mix(hue, 14))).click(w, Target::Toast(t.id()))
}

/// The top of the frame ([`top_rows`]).
struct Top {
    rows: Vec<Row>,
    /// The content rows.
    status: Range<usize>,
    /// The card's rows (empty: no card).
    card: Range<usize>,
    /// The card's top and bottom border texts.
    title: Vec<Seg>,
    footer: Vec<Seg>,
    /// The toast's rows, and when it expires (None: none drawn).
    toast: Range<usize>,
    toast_until: Option<i64>,
}

/// A toast's deadline as [`Frame::toast_until`] holds it.
fn until_ms(t: &Toast) -> i64 {
    (t.until() * 1000.0).ceil() as i64
}

/// The top of the frame. With a [`Bar`]: the card. Its border needs 16
/// rows and 20 columns; one meter per window 30 rows (else usage in one
/// line, from 12; the stale save from 12 too); the selection hint 6.
/// Without one: the classic title.
///
/// A live toast ([`Toast::live`] at `now`) sits in the card's bottom border
/// (no row moves), plus up to 2 rows above it when its text is long; with
/// no card, on a row of its own at the very top (from 6 rows).
fn top_rows(view: &ViewModel, bar: &Bar, w: usize, h: usize, now: f64) -> Top {
    let toast = bar.toast.as_ref().filter(|t| t.live(now));
    let mut top = Top { rows: Vec::new(), status: 0..0, card: 0..0, title: Vec::new(), footer: Vec::new(), toast: 0..0, toast_until: None };
    // No card to carry it: the toast on a row of its own, first.
    let alone = |top: &mut Top| {
        if let Some(t) = toast.filter(|_| h >= 6) {
            top.rows.push(toast_line(t, w));
            top.toast = 0..1;
            top.toast_until = Some(until_ms(t));
        }
    };
    if !bar_shown(bar) {
        alone(&mut top);
        top.rows.push(title_row(view, w));
        let n = top.rows.len();
        top.status = n..n;
        return top;
    }
    let bordered = w >= 20 && h >= 16;
    let iw = if bordered { w - 2 } else { w };
    let mut content = Vec::new();
    let marks = marks(bar);
    if let Some(n) = bar.selecting.filter(|_| h >= 6) {
        content.push(hint_row(n, iw));
    }
    if h >= 30 {
        content.extend(bar.usage.iter().map(|u| meter_row(u, iw, now)));
    } else if h >= 12 {
        content.extend(usage_row(bar, iw));
    }
    if h >= 12 {
        content.extend(stale_row(bar, iw));
    }
    // Nothing to put in a card (or no room for its border): the marks on a
    // row of their own, first (after a toast).
    let marks_row = |w: usize| Row::new(fit(w, [vec![seg(" ", "text")], marks.clone()].concat(), vec![], None));
    if content.is_empty() || !bordered {
        alone(&mut top);
        if !marks.is_empty() {
            top.rows.push(marks_row(w));
        }
        top.rows.extend(content);
        top.status = 0..top.rows.len();
        return top;
    }
    top.rows.push(Row::blank(w));
    top.rows.extend(content.into_iter().map(|r| inset(r, w)));
    let mut bottom = Row::blank(w);
    if let Some(t) = toast {
        let (footer, long) = toast_footer(t, w);
        let at = top.rows.len();
        if long {
            top.rows.extend(toast_rows(t, iw).into_iter().map(|r| inset(r, w)));
        }
        bottom.targets.push((2, (2 + width(&footer)) as u16, Target::Toast(t.id())));
        top.footer = footer;
        top.toast = at..top.rows.len() + 1;
        top.toast_until = Some(until_ms(t));
    }
    top.rows.push(bottom);
    let n = top.rows.len();
    top.status = 1..n - 1;
    top.card = 0..n;
    // In the top border from its third cell, one corner and a rule cell left at the end.
    top.title = clip_segs(marks, w.saturating_sub(5));
    top
}

// ---------------------------------------------------------------- shells

/// ` SHELLS`, then this space's shells (the client's session, ⌥N order, the
/// home first) one row each: the ⌥N key-cap, `⌂` on the home, the label.
/// The session's active window is lifted like the client's agent; one in
/// prefix v's selection on mauve. A row clicks through to its shell.
fn shells_section(bar: &Bar, w: usize) -> Vec<Row> {
    if bar.shells.is_empty() {
        return Vec::new();
    }
    let mut rows = vec![Row::new(fit(w, vec![bold(" SHELLS", "overlay")], vec![], None))];
    for s in &bar.shells {
        let (bg, edge) = if s.selected {
            (Some(Ink::Mix("mauve", 18)), seg("▌", "mauve"))
        } else if s.active {
            (Some(Ink::Pal("surface0")), seg("▎", "blue"))
        } else {
            (None, seg(" ", "text"))
        };
        let raised = bg.is_some();
        let key = shell_key(s).map_or("    ".to_string(), |k| format!(" {k} "));
        let mut left = vec![edge, on(key, if s.active { "peach" } else { "sub" }, if raised { "surface1" } else { "surface0" })];
        left.push(seg(" ", "text"));
        if s.home {
            left.push(seg("⌂ ", dim(raised)));
        }
        left.push(if s.active { bold(s.label.clone(), "text") } else { seg(s.label.clone(), "sub") });
        let go = Target::Shell { win: s.win.clone(), session: s.session.clone() };
        rows.push(Row::new(fit(w, left, vec![], None)).bg(bg).click(w, go));
    }
    rows
}

/// The shells in one line, for a frame with no room for their section:
/// ` SHELLS` and the shells as pills, the active one kept, `+K` for those
/// that do not fit.
fn shells_line(bar: &Bar, w: usize) -> Vec<Row> {
    if bar.shells.is_empty() {
        return Vec::new();
    }
    let items: Vec<Item> = bar.shells.iter().map(shell_pill).collect();
    let mut rank: Vec<usize> = (0..items.len()).collect();
    rank.sort_by_key(|&i| !bar.shells[i].active);
    let more = |k: usize| item(vec![seg(format!("+{k}"), "overlay")], None);
    pack(&[item(vec![bold("SHELLS", "overlay")], None)], &items, &rank, w, 1, &more).0
}

// ---------------------------------------------------------------- agents

/// The agents' LETTER column: each agent row and NEEDS YOU card shows its
/// lifetime letter ([`crate::menu::letters`], the same `@agent_key` the
/// agents menu and UB's ⌥W table go by) as a key-cap: ` a ` on `surface1`,
/// bold; peach while the jump table is armed (`loud`). Agents without a
/// letter (past 26, the client's plain window) leave it blank so titles
/// stay aligned; with no letter anywhere the column is not drawn.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Badge {
    on: bool,
    loud: bool,
}

/// A key-cap's cells (` a `).
const BADGE_W: usize = 3;

impl Badge {
    #[cfg(test)]
    const OFF: Badge = Badge { on: false, loud: false };

    fn of(view: &ViewModel, bar: &Bar) -> Badge {
        let on = view.agents().chain(view.needs.iter().map(|n| &n.agent)).any(|a| a.letter.is_some());
        Badge { on, loud: bar.jump }
    }

    /// The cells before a title: the edge, the key-cap and a space; or the
    /// edge and a space.
    fn lead(self) -> usize {
        if self.on {
            BADGE_W + 2
        } else {
            2
        }
    }

    /// `a`'s key-cap (blank when it holds no letter): `surface0`, or
    /// `surface1` on a row that is itself lifted (`raised`).
    fn seg(self, a: &Agent, raised: bool) -> Seg {
        match a.letter {
            Some(c) if self.loud => block(format!(" {c} "), "crust", "peach"),
            Some(c) => on(format!(" {c} "), "sub", if raised { "surface1" } else { "surface0" }),
            None => seg(" ".repeat(BADGE_W), "text"),
        }
    }
}

/// An agent's state as the row's right side says it.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Word {
    /// The spinner frame, ✕ ◉ ✓, or "" (idle).
    glyph: &'static str,
    word: &'static str,
    hue: Hue,
}

impl Word {
    fn working(self: &Word) -> bool {
        self.word == "working"
    }
}

/// An idle agent whose last turn finished less than [`RECENT`] ago with a
/// message (`@agent_detail_kind` done).
fn recent_done(a: &Agent, now: f64) -> bool {
    a.cat == Some(Cat::Idle)
        && a.detail_word == Some("done")
        && !plain(&a.detail).is_empty()
        && a.since.is_some_and(|s| now - s as f64 <= RECENT)
}

/// The state word: `⠹ working` (blue, animated), `◉ asks` / `◉ perm`
/// (yellow), `✕ error` (red), `✓ done` (green: done, or idle with a recent
/// finished turn), `idle` (dim). None for a plain shell.
fn state_word(a: &Agent, cx: Cx) -> Option<Word> {
    if a.in_flight || a.cat == Some(Cat::Working) {
        return Some(Word { glyph: SPIN[cx.spin % SPIN.len()], word: "working", hue: "blue" });
    }
    Some(match a.cat? {
        Cat::Failed => Word { glyph: "✕", word: "error", hue: "red" },
        Cat::NeedsInput => Word { glyph: "◉", word: if a.detail_kind == "perm" { "perm" } else { "asks" }, hue: "yellow" },
        Cat::Done => Word { glyph: "✓", word: "done", hue: "green" },
        Cat::Idle if recent_done(a, cx.now) => Word { glyph: "✓", word: "done", hue: "green" },
        Cat::Idle | Cat::Working => Word { glyph: "", word: "idle", hue: "overlay" },
    })
}

/// The second line's message: the latest one, only for a working agent
/// (what it runs or says now, never the last turn's outcome), one asking
/// or failed, and one done (or recently finished). A detail that is nothing
/// but markup counts as none.
fn message(a: &Agent, now: f64) -> Option<String> {
    let m = plain(&a.detail);
    if m.is_empty() {
        return None;
    }
    let show = if a.in_flight || a.cat == Some(Cat::Working) {
        !matches!(a.detail_kind.as_str(), "done" | "fail")
    } else {
        matches!(a.cat, Some(Cat::NeedsInput | Cat::Failed | Cat::Done)) || recent_done(a, now)
    };
    show.then_some(m)
}

/// What a two-line agent row says under its title: the [`message`]; else,
/// for an idle agent, its last turn's outcome (what it last said is still
/// true of it); else nothing (a working agent's old outcome never reads as
/// what it does now). Every agent row is two lines either way.
fn second_line(a: &Agent, now: f64) -> String {
    message(a, now)
        .or_else(|| (!a.in_flight && a.cat != Some(Cat::Working)).then(|| plain(&a.detail)))
        .unwrap_or_default()
}

/// A title without a bare `~/` "project" (a summary made in $HOME).
fn no_home(title: &str) -> &str {
    title.strip_prefix("~/").unwrap_or(title)
}

/// The title under its space's header: the `project/` prefix dropped (the
/// space already says where it is).
fn short_title(a: &Agent) -> String {
    sanitize(if a.short_title.is_empty() { no_home(&a.title) } else { &a.short_title })
}

/// One agent: its key-cap, the title (bold unless idle), a flag (⚙ workflow,
/// ◎ cua), and the state word on the right, the age beside it; with `two`,
/// a dim second line ([`second_line`], maybe blank) with the age at its end,
/// so every agent row is the same height. The client's agent sits on
/// `surface0` with a blue edge; one in prefix v's selection on mauve with a
/// mauve edge. Both lines click through.
fn agent_rows(a: &Agent, cx: Cx, two: bool) -> Vec<Row> {
    let w = cx.w;
    let go = Target::Go { win: a.window_id.clone(), session: a.session.clone() };
    let (bg, edge) = if a.selected {
        (Some(Ink::Mix("mauve", 18)), seg("▌", "mauve"))
    } else if a.is_current {
        (Some(Ink::Pal("surface0")), seg("▎", "blue"))
    } else {
        (None, seg(" ", "text"))
    };
    let msg = (two && cx.seconds()).then(|| second_line(a, cx.now));
    let word = state_word(a, cx);
    let age = Some(age_of(a, cx)).filter(|g| cx.ages() && !g.is_empty() && word.is_some());
    // Dim text on a lifted row needs the surface's dim.
    let dimc = dim(bg.is_some());
    let flag = a.flag.map(|(f, _)| match f {
        Flag::Workflow => seg(" ⚙", "teal"),
        Flag::Cua => seg(" ◎", if a.attention { "overlay" } else { "blue" }),
    });
    let lead = cx.b.lead();
    let right_of = |full: bool, with_age: bool| -> Vec<Seg> {
        let mut r = Vec::new();
        if let Some(wd) = &word {
            let hue = if wd.hue == "overlay" { dimc } else { wd.hue };
            match (full, wd.glyph.is_empty()) {
                (true, false) => r.extend([seg(format!("{} ", wd.glyph), hue), seg(wd.word, hue)]),
                (true, true) => r.push(seg(wd.word, hue)),
                (false, false) => r.push(seg(wd.glyph, hue)),
                (false, true) => {}
            }
        }
        if with_age && msg.is_none() {
            if let Some(ag) = &age {
                r.push(seg(format!(" {ag}"), dimc));
            }
        }
        r.push(seg(" ", "text"));
        r
    };
    let fixed = |r: &[Seg]| lead + flag.as_ref().map_or(0, |f| dwidth(&f.text)) + width(r) + 1;
    let full = short_title(a);
    // The title before the age: an age that would clip it goes (a working
    // agent's age is the least of what the row says).
    let mut right = right_of(true, true);
    if fixed(&right) + dwidth(&full) > w {
        right = right_of(true, false);
    }
    if w < fixed(&right) + 6 {
        right = right_of(false, false);
    }
    let room = w.saturating_sub(fixed(&right));
    let quiet = word.as_ref().is_none_or(|x| x.word == "idle");
    let title = clip(&full, room);
    let title = if quiet { seg(title, "sub") } else { bold(title, "text") };
    let mut left = vec![edge.clone()];
    if cx.b.on {
        left.push(cx.b.seg(a, bg.is_some()));
    }
    left.push(seg(" ", "text"));
    left.push(title);
    left.extend(flag);
    let mut first = Row::new(fit(w, left, right, None)).bg(bg).click(w, go.clone()).covering([a]);
    first.anim = word.as_ref().is_some_and(Word::working);
    let Some(m) = msg else { return vec![first] };
    let r2 = match &age {
        Some(ag) => vec![seg(format!(" {ag} "), dimc)],
        None => vec![seg(" ", "text")],
    };
    // Under the title (past the key-cap); without letters, indented anyway,
    // so a message never reads as another agent.
    let l2 = vec![edge, seg(" ".repeat(lead.max(4) - 1), "text"), seg(m, dimc)];
    let second = Row::new(fit(w, l2, r2, None)).bg(bg).click(w, go);
    vec![first, second]
}

/// THE space order, everywhere (the agents' groups, the spaces pills):
/// `@space_index` (⌃N) first, spaces without one after (by name: the view's
/// order, kept by a stable sort).
pub fn space_order(sp: &Space) -> (bool, Option<u32>) {
    (sp.index.is_none(), sp.index)
}

/// A space's header in the agents section: a dot in its `@space_color`
/// (blue for the client's space without one, else dim), its name (bold),
/// its branch dim when there is room, and on the right its counts in their
/// colours. No rule. Collapsed, it stands for all its agents. A click
/// switches to the space.
fn group_header(sp: &Space, w: usize, collapsed: bool) -> Row {
    let dot = tmux_colour(&sp.color).unwrap_or(Ink::Pal(if sp.is_current { "blue" } else { "overlay" }));
    let mut left = vec![seg(" ", "text"), seg("● ", dot), bold(sanitize(&sp.name), if sp.is_current { "text" } else { "sub" })];
    let mut right = count_segs(&sp.counts);
    right.push(seg(" ", "text"));
    if !sp.branch.is_empty() {
        let br = format!("  \u{e0a0} {}", sanitize(&sp.branch));
        if width(&left) + dwidth(&br) + width(&right) < w {
            left.push(seg(br, "overlay"));
        }
    }
    let row = Row::new(fit(w, left, right, None)).click(w, Target::Session(sp.name.clone()));
    if collapsed {
        row.covering(&sp.agents)
    } else {
        row
    }
}

/// `○○○ 3 idle · Name, Name…`: a space's idle agents in one line. No
/// letters: it stands for several agents.
fn fold_row(idle: &[&Agent], cx: Cx) -> Row {
    let k = idle.len();
    let names: Vec<String> = idle.iter().map(|a| short_title(a)).collect();
    let indent = " ".repeat(cx.b.lead());
    Row::new(fit(
        cx.w,
        vec![seg(format!("{indent}{} {k} idle · ", "○".repeat(k.min(4))), "overlay"), seg(names.join(", "), "overlay")],
        vec![],
        None,
    ))
    // The menu's Idle tab: just the idle agents.
    .click(cx.w, Target::Menu(Some("idle")))
    .covering(idle.iter().copied())
}

/// `… N more`: the agents nothing above has room for (`spaces`: how many
/// spaces they come from; 0: all of them, `… N agents`).
fn more_row(w: usize, agents: &[&Agent], spaces: usize) -> Row {
    let n = agents.len();
    let text = match spaces {
        0 => format!(" … {n} agents"),
        1 => format!(" … {n} more"),
        _ => format!(" … {n} more in {spaces} spaces"),
    };
    // The menu's All tab: the hidden agents may be idle, which Active omits.
    dim_line(w, &text).click(w, Target::Menu(Some("all"))).covering(agents.iter().copied())
}

/// An agent's identity across frames: (session, window id).
type Key<'a> = (&'a str, &'a str);

fn key(a: &Agent) -> Key<'_> {
    (&a.session, &a.window_id)
}

/// With `fold`, a space's idle agents (never the client's window, never one
/// whose prompt was just answered: it is still Active) go into one line,
/// when there are 2+ of them → (shown in place, folded).
fn split_idle(sp: &Space, fold: bool, now: f64) -> (Vec<&Agent>, Vec<&Agent>) {
    let foldable = |a: &Agent| fold && a.cat == Some(Cat::Idle) && !a.is_current && !a.answered && !recent_done(a, now);
    let (idle, shown): (Vec<&Agent>, Vec<&Agent>) = sp.agents.iter().partition(|a| foldable(a));
    if idle.len() >= 2 {
        (shown, idle)
    } else {
        (sp.agents.iter().collect(), vec![])
    }
}

/// A space's agent rows in window-index order (the folded line last); the
/// agents in `two` get their second line. With `sep`, a hairline between
/// one agent and the next ([`sep_row`]).
fn group_rows(sp: &Space, cx: Cx, two: &[Key], fold: bool, sep: bool) -> Vec<Row> {
    let (shown, idle) = split_idle(sp, fold, cx.now);
    let mut rows: Vec<Row> = Vec::new();
    for (k, a) in shown.iter().enumerate() {
        if sep && k > 0 {
            rows.push(sep_row(cx.w));
        }
        rows.extend(agent_rows(a, cx, two.contains(&key(a))));
    }
    if !idle.is_empty() {
        if sep && !shown.is_empty() {
            rows.push(sep_row(cx.w));
        }
        rows.push(fold_row(&idle, cx));
    }
    rows
}

/// The hairline between two agents of a space: a `surface0` rule from the
/// third cell to the second-last, so each agent reads as its own row. Not
/// a target, stands for no one.
fn sep_row(w: usize) -> Row {
    Row::new(fit(w, vec![seg("  ", "text"), seg("─".repeat(w.saturating_sub(4)), "surface0")], vec![], None))
}

/// A space's agents in `room` rows: all of them one line each if they fit,
/// else the `room - 1` that matter most ([`promotion_rank`], kept in list
/// order) and a `… N more` line for the rest.
fn partial_rows(sp: &Space, cx: Cx, room: usize) -> Vec<Row> {
    if sp.agents.len() <= room {
        return sp.agents.iter().flat_map(|a| agent_rows(a, cx, false)).collect();
    }
    let mut ranked: Vec<usize> = (0..sp.agents.len()).collect();
    ranked.sort_by_key(|&k| promotion_rank(&sp.agents[k], cx.now));
    let mut keep = ranked[..room - 1].to_vec();
    keep.sort_unstable();
    let mut rows: Vec<Row> = keep.iter().flat_map(|&k| agent_rows(&sp.agents[k], cx, false)).collect();
    let rest: Vec<&Agent> = (0..sp.agents.len()).filter(|k| !keep.contains(k)).map(|k| &sp.agents[k]).collect();
    rows.push(more_row(cx.w, &rest, 1));
    rows
}

/// Which agents a collapsed space keeps first, when its rows run short:
/// attention, then in flight, then the client's window, then any with a
/// message; ties in list order.
fn promotion_rank(a: &Agent, now: f64) -> u8 {
    if a.attention {
        0
    } else if a.in_flight {
        1
    } else if a.is_current {
        2
    } else if message(a, now).is_some() {
        3
    } else {
        4
    }
}

/// The spaces the agents section draws, in [`space_order`]: those with
/// agents; in space scope, just the client's session, agents or not.
fn groups(view: &ViewModel) -> Vec<&Space> {
    let mut v: Vec<&Space> = match &view.scope.session {
        Some(s) => view.spaces.iter().filter(|sp| &sp.name == s).collect(),
        None => view.spaces.iter().filter(|s| !s.agents.is_empty()).collect(),
    };
    v.sort_by_key(|s| space_order(s));
    v
}

/// The agents section's rows when every agent is on one line, nothing
/// folded or collapsed: what step 4 leaves the agents before NEEDS YOU
/// gets the rest.
fn agents_one_line(view: &ViewModel) -> usize {
    let groups = groups(view);
    if groups.iter().all(|g| g.agents.is_empty()) {
        return 2 + usize::from(view.scope.session.is_some());
    }
    1 + groups.iter().map(|g| 1 + g.agents.len()).sum::<usize>()
}

/// ` AGENTS` and the scope on the right (`this space` / `all spaces`), which
/// flips it once the scope is a thing.
fn agents_header(view: &ViewModel, w: usize) -> Row {
    let word = if view.scope.session.is_some() { "this space" } else { "all spaces" };
    let mut row = Row::new(fit(w, vec![bold(" AGENTS", "overlay")], vec![seg(format!("{word} "), "overlay")], None));
    let ww = dwidth(word) + 1;
    if view.scope.toggles() && w > ww + 8 {
        row.targets.push(((w - ww) as u16, (w - 1) as u16, Target::Scope));
    }
    row
}

/// The agents section (header, then each group's header and rows), at most
/// `budget` rows → (rows, its ladder step). `spaced`: a blank row between
/// groups when everything else fits.
///
/// Steps: 0 every agent two lines (with the hairlines between agents, then
/// without); 1 the same, idle folded; 2 every agent one line; 3 the same,
/// idle folded; 4 spaces collapsed; 5 `… N more`. Never a mix of heights in
/// one frame.
fn agents_section(view: &ViewModel, cx: Cx, budget: usize, spaced: bool) -> (Vec<Row>, u8) {
    if budget == 0 {
        return (vec![], 0);
    }
    let w = cx.w;
    let groups = groups(view);
    let space = view.scope.session.is_some();
    let header = agents_header(view, w);
    if space && groups.iter().all(|g| g.agents.is_empty()) {
        // The space's header even with no agent in it, so the section says whose it is.
        let mut rows = vec![header];
        rows.extend(groups.iter().map(|g| group_header(g, w, false)));
        rows.push(dim_line(w, "   no agents in this space"));
        rows.truncate(budget);
        return (rows, 0);
    }
    if groups.is_empty() {
        let mut rows = vec![header, dim_line(w, "   no agents running")];
        rows.truncate(budget);
        return (rows, 0);
    }
    let build_sep = |two: &[Key], fold: bool, collapsed: &[bool], spaced: bool, sep: bool| {
        let mut rows = vec![header.clone()];
        for (k, (sp, &c)) in groups.iter().zip(collapsed).enumerate() {
            if spaced && k > 0 {
                rows.push(Row::blank(w));
            }
            rows.push(group_header(sp, w, c));
            if !c {
                rows.extend(group_rows(sp, cx, two, fold, sep));
            }
        }
        rows
    };
    let build = |two: &[Key], fold: bool, collapsed: &[bool], spaced: bool| build_sep(two, fold, collapsed, spaced, false);
    let mut collapsed = vec![false; groups.len()];
    let all: Vec<Key> = groups.iter().flat_map(|g| g.agents.iter().map(key)).collect();
    // Full detail: the gaps between spaces go before the hairlines between agents.
    let full: &[(bool, bool)] = if spaced { &[(true, true), (false, true), (false, false)] } else { &[(false, true), (false, false)] };
    for &(sp, sep) in full {
        let rows = build_sep(&all, false, &collapsed, sp, sep);
        if rows.len() <= budget {
            return (rows, 0);
        }
    }
    // Every agent row the same height: all two lines (idle folded), else all one.
    for (step, two, fold) in [(1, &all[..], true), (2, &[][..], false), (3, &[][..], true)] {
        let rows = build(two, fold, &collapsed, false);
        if rows.len() <= budget {
            return (rows, step);
        }
    }
    // Collapse whole spaces: fewest attention agents first, then the client's
    // space last, then fewest in flight, then from the end of the list.
    let mut order: Vec<usize> = (0..groups.len()).collect();
    order.sort_by_key(|&i| {
        let g = groups[i];
        let attn = g.agents.iter().filter(|a| a.attention).count();
        let busy = g.agents.iter().filter(|a| a.in_flight).count();
        (attn, g.is_current, busy, Reverse(i))
    });
    for &i in &order {
        collapsed[i] = true;
        let base = build(&[], true, &collapsed, false);
        if base.len() > budget {
            continue;
        }
        let spare = budget - base.len();
        let at = 1 + (0..i)
            .map(|j| 1 + if collapsed[j] { 0 } else { group_rows(groups[j], cx, &[], true, false).len() })
            .sum::<usize>();
        if spare < 2 {
            if spare == 0 {
                return (base, 4);
            }
            // One row: the collapsed space's most important agent under its
            // header, which still counts the rest.
            let g = groups[i];
            let Some(top) = (0..g.agents.len()).min_by_key(|&k| (promotion_rank(&g.agents[k], cx.now), k)) else {
                return (base, 4);
            };
            let mut rows = base;
            let gone = key(&g.agents[top]);
            rows[at].covers.retain(|(s, id)| (s.as_str(), id.as_str()) != gone);
            rows.insert(at + 1, agent_rows(&g.agents[top], cx, false).remove(0));
            return (rows, 4);
        }
        // The space whose collapse made it fit keeps the rows that frees:
        // its most important agents, one line each, and `… N more`.
        let mut rows = base;
        rows[at] = group_header(groups[i], w, false);
        rows.splice(at + 1..at + 1, partial_rows(groups[i], cx, spare));
        return (rows, 4);
    }
    // More spaces than rows: the first ones as headers, the rest counted.
    if budget == 1 {
        let all: Vec<&Agent> = groups.iter().flat_map(|g| g.agents.iter()).collect();
        return (vec![more_row(w, &all, groups.len())], 5);
    }
    let keep = budget - 2;
    let mut rows = build(&[], true, &collapsed, false);
    rows.truncate(1 + keep);
    let rest: Vec<&Agent> = groups[keep..].iter().flat_map(|g| g.agents.iter()).collect();
    rows.push(more_row(w, &rest, groups.len() - keep));
    (rows, 5)
}

// ---------------------------------------------------------------- needs you

/// A NEEDS YOU card: 2 or 1 lines on its state colour mixed over the
/// canvas, a `▌` edge in that colour; the key-cap, the title (bold), and on
/// the right the state glyph and age. Line 2: the reason (a `perm` prompt
/// says so), or the state in words.
fn need_rows(n: &Need, cx: Cx, lines: u8) -> Vec<Row> {
    let w = cx.w;
    let a = &n.agent;
    let hue = a.color.unwrap_or("overlay");
    let bg = Some(Ink::Mix(hue, 14));
    let go = Target::Go { win: a.window_id.clone(), session: a.session.clone() };
    let card = |segs: Vec<Seg>| Row::new(segs).bg(bg).click(w, go.clone());
    let edge = || seg("▌", hue);
    let age = Some(age_of(a, cx)).filter(|g| cx.ages() && !g.is_empty()).map_or(String::new(), |g| format!(" {g}"));
    let right = vec![seg(a.glyph, hue), seg(format!("{age} "), dim(true))];
    let lead = cx.b.lead();
    let (proj, title) = fit_label(no_home(&a.title), w.saturating_sub(lead + width(&right) + 1));
    let mut first = vec![edge()];
    if cx.b.on {
        first.push(cx.b.seg(a, true));
    }
    first.extend([seg(" ", "text"), seg(proj, dim(true)), bold(title, "text")]);
    let mut rows = vec![card(fit(w, first, right, None))];
    if lines >= 2 {
        let mut l2 = vec![edge(), seg(" ".repeat(lead - 1), "text")];
        if n.reason.is_empty() {
            l2.push(seg(n.reason_word.clone(), hue));
        } else {
            if n.reason_word == "perm" {
                l2.push(seg("perm ", "yellow"));
            }
            l2.push(seg(plain(&n.reason), "sub"));
        }
        rows.push(card(fit(w, l2, vec![seg(" ", "text")], None)));
    }
    rows
}

/// ` NEEDS YOU` and the count, or `✓ all clear` with nothing pending.
fn needs_header(view: &ViewModel, w: usize) -> Row {
    let n = view.needs.len();
    let right = if n > 0 { vec![bold(format!("{n} "), "yellow")] } else { vec![seg("✓ all clear ", "overlay")] };
    Row::new(fit(w, vec![bold(" NEEDS YOU", if n > 0 { "yellow" } else { "overlay" })], right, None))
}

/// The whole NEEDS YOU section: its header and the cards (a blank row
/// between them with `gaps`). Never empty: with nothing pending it is the
/// header saying so.
fn needs_block(view: &ViewModel, cx: Cx, lines: u8, gaps: bool) -> Vec<Row> {
    let mut rows = vec![needs_header(view, cx.w)];
    for (k, n) in view.needs.iter().enumerate() {
        if gaps && k > 0 && lines > 1 {
            rows.push(Row::blank(cx.w));
        }
        rows.extend(need_rows(n, cx, lines));
    }
    rows
}

/// NEEDS YOU in at most `room` rows: 1-line cards, the rest counted.
fn needs_cut(view: &ViewModel, cx: Cx, room: usize) -> Vec<Row> {
    let w = cx.w;
    let n = view.needs.len();
    let more = |k: usize, words: &str| {
        Row::new(fit(w, vec![seg(format!(" … {k} {words}"), "yellow")], vec![], None)).click(w, Target::Menu(None))
    };
    let mut rows = needs_block(view, cx, 1, false);
    if rows.len() <= room {
        return rows;
    }
    match room {
        0 => vec![],
        1 => vec![more(n, "need you")],
        _ => {
            rows.truncate(room - 1);
            rows.push(more(n - (room - 2), "more need you"));
            rows
        }
    }
}

// ---------------------------------------------------------------- spaces

/// The spaces the pills show, in [`space_order`]: every session the view
/// holds except `@space_hidden` ones.
fn space_list(view: &ViewModel) -> Vec<&Space> {
    let mut v: Vec<&Space> = view.spaces.iter().filter(|s| !s.hidden).collect();
    v.sort_by_key(|s| space_order(s));
    v
}

/// A space has an agent needing attention (failed, needs you, done).
fn space_alert(sp: &Space) -> bool {
    sp.agents.iter().any(|a| a.attention)
}

/// A space as a pill: ` 2 bai `, its ⌃N digit (1-9) in its `@space_color`.
/// The client's space is FILLED with that colour (blue without one); the
/// others sit on `surface0`, dim when they hold no agent. A space with an
/// agent needing attention carries that state's glyph. A click switches
/// the client to it.
fn space_pill(sp: &Space) -> Item {
    let name = clip(&sanitize(&sp.name), 12);
    let col = tmux_colour(&sp.color);
    let num = sp.index.filter(|n| (1..=9).contains(n));
    let alert = sp.agents.iter().filter(|a| a.attention).min_by_key(|a| a.rank).and_then(|a| a.cat).map(|c| (c.glyph(), cat_hue(c)));
    let mut segs = Vec::new();
    if sp.is_current {
        let bg = col.unwrap_or(Ink::Pal("blue"));
        let ink = ink_on(bg);
        let label = match num {
            Some(n) => format!(" {n} {name} "),
            None => format!(" {name} "),
        };
        segs.push(block(label, ink, bg));
        if let Some((g, _)) = alert {
            segs.push(block(format!("{g} "), ink, bg));
        }
    } else {
        let bg = Ink::Pal("surface0");
        let quiet = sp.counts.total() == 0;
        if let Some(n) = num {
            segs.push(Seg { bold: true, ..on(format!(" {n}"), col.unwrap_or(Ink::Pal(dim(true))), bg) });
        }
        segs.push(on(format!(" {name} "), if quiet { dim(true) } else { "sub" }, bg));
        if let Some((g, hue)) = alert {
            segs.push(on(format!("{g} "), hue, bg));
        }
    }
    item(segs, Some(Target::Session(sp.name.clone())))
}

/// The thin rule above the pills.
fn rule_row(w: usize) -> Row {
    Row::new(fit(w, vec![seg(" ", "text"), seg("─".repeat(w.saturating_sub(2)), "surface0")], vec![], None))
}

/// The pills in at most `lines` rows: all of them, else the client's space,
/// those with an attention agent, then ⌃N order, kept (shown in list
/// order), and a `+K` pill (the menu's All tab) for the rest.
fn pill_rows(view: &ViewModel, w: usize, lines: usize) -> Vec<Row> {
    let list = space_list(view);
    let items: Vec<Item> = list.iter().map(|sp| space_pill(sp)).collect();
    let mut rank: Vec<usize> = (0..list.len()).collect();
    rank.sort_by_key(|&i| (!list[i].is_current, !space_alert(list[i])));
    let more = |k: usize| item(vec![on(format!(" +{k} "), "sub", "surface0")], Some(Target::Menu(Some("all"))));
    pack(&[], &items, &rank, w, lines, &more).0
}

/// How many pill lines every space needs (at most 3).
fn pill_lines(view: &ViewModel, w: usize) -> usize {
    let items: Vec<Item> = space_list(view).iter().map(|sp| space_pill(sp)).collect();
    place(&items, w, usize::MAX).map_or(1, |r| r.len()).clamp(1, 3)
}

/// The rows the spaces section keeps however short the frame (held back
/// before anything else is laid out, agents included): the rule and one
/// pill line. 0 with no spaces, or when `avail` is under 4.
fn spaces_min(view: &ViewModel, avail: usize) -> usize {
    if space_list(view).is_empty() || avail < 4 {
        0
    } else {
        2
    }
}

/// The spaces section in the `room` rows the sections above left: the rule
/// and as many pill lines as it needs and fit (at least one), else nothing.
fn spaces_fit(view: &ViewModel, w: usize, room: usize) -> Vec<Row> {
    if space_list(view).is_empty() || room < 2 {
        return Vec::new();
    }
    let lines = pill_lines(view, w).min(room - 1);
    let mut rows = vec![rule_row(w)];
    rows.extend(pill_rows(view, w, lines));
    rows
}

/// The steps at which agents keep full detail: (blank gaps, NEEDS YOU card
/// lines). The spaces section takes what each leaves over.
pub const STEPS: [(bool, u8); 3] = [(true, 2), (false, 2), (false, 1)];

/// The last row: the stash in one line, ` ⏸ N parked` (a click opens the
/// same popup as prefix h, [`Target::Stash`]), and on the right a message
/// (Roster.say) or the watcher's problem in red (click: restart).
fn last_row(view: &ViewModel, w: usize, msg: Option<&str>) -> Row {
    let n = view.parked.len();
    let left = vec![seg(format!(" ⏸ {n} parked"), if n > 0 { "sub" } else { "overlay" })];
    let (right, restart) = match (msg, watcher_problem(view.watcher_age)) {
        (Some(m), _) => (vec![seg(format!("{} ", sanitize(m)), "sub")], false),
        (None, Some(p)) => (vec![seg(format!("watcher {p} "), "red")], true),
        (None, None) => (vec![], false),
    };
    let segs = fit(w, left, right.clone(), None);
    let rw = width(&clip_segs(right, w)) as u16;
    let w16 = w as u16;
    let mut row = Row::new(segs);
    row.targets.push((0, w16 - rw, Target::Stash));
    if restart && rw > 0 {
        row.targets.push((w16 - rw, w16, Target::Restart));
    }
    row
}

/// The whole screen: the card, NEEDS YOU, agents, spare rows, the spaces,
/// the last row. Always exactly `height` rows of exactly `width` cells.
pub fn layout(view: &ViewModel, width: u16, height: u16, opts: &Opts) -> Frame {
    let (w, h) = (usize::from(width), usize::from(height));
    let mut frame = Frame { width, height, ..Frame::default() };
    if w == 0 || h == 0 {
        return frame;
    }
    let bar = &opts.bar;
    let cx = Cx { w, b: Badge::of(view, bar), now: opts.now.unwrap_or(view.now), spin: opts.spin };
    let last = last_row(view, w, opts.msg.as_deref());
    let Top { rows: top, status, card, title: card_title, footer: card_footer, toast, toast_until } = top_rows(view, bar, w, h, cx.now);
    // The rows between the top and the last row.
    let avail = h.saturating_sub(top.len() + 1);
    if avail == 0 {
        // No room for sections: one row still counts every agent of the
        // scope, then the parked row.
        let all: Vec<&Agent> = groups(view).into_iter().flat_map(|s| s.agents.iter()).collect();
        if toast.start == 0 && !toast.is_empty() {
            // The first row is the toast's own.
            frame.toast = 0..1;
            frame.toast_until = toast_until;
        }
        let mut rows = vec![top.into_iter().next().unwrap_or_else(|| Row::blank(w))];
        rows.push(if all.is_empty() { Row::blank(w) } else { more_row(w, &all, 0) });
        rows.push(last);
        rows.truncate(h);
        frame.rows = rows;
        frame.step = STEPS.len() as u8;
        frame.status = 0..0;
        return frame;
    }
    // The spaces' rule and first pill line are held back first: spaces are
    // listed nowhere else, so the section never goes before the agents
    // lose detail.
    let avail = avail - spaces_min(view, avail);
    // Every step with the shells' whole section, then the last step with
    // their one line: one ladder, so as the frame shrinks neither the step
    // nor the shells' form ever comes back.
    let (full, line) = (shells_section(bar, w), shells_line(bar, w));
    let end = STEPS.len() - 1;
    let tries = STEPS.iter().enumerate().map(|(i, s)| (i, *s, &full)).chain([(end, STEPS[end], &line)]);
    let fits = tries.into_iter().find_map(|(i, (gaps, lines), shells)| {
        let needs = needs_block(view, cx, lines, gaps);
        let agents = agents_section(view, cx, usize::MAX, gaps).0;
        let gap_rows = usize::from(gaps) * (2 + usize::from(!shells.is_empty()));
        (needs.len() + agents.len() + shells.len() + gap_rows <= avail).then(|| (i as u8, gaps, needs, agents, shells.clone()))
    });
    let (step, gaps, needs, (agents, agents_step), shells) = match fits {
        Some((i, gaps, needs, agents, shells)) => (i, gaps, needs, (agents, 0), shells),
        None => {
            // The agents compress, the shells in one line (none in under 4
            // rows). NEEDS YOU gets what the agents' one-line shape leaves
            // (up to the whole, at least 2 rows and a third of the body),
            // and always leaves the agents a row.
            let shells = if avail >= 4 { line.clone() } else { Vec::new() };
            let avail = avail - shells.len();
            let n1 = needs_block(view, cx, 1, false).len();
            let floor = (avail / 3).max(2);
            let room = n1.min(avail.saturating_sub(agents_one_line(view)).max(floor)).min(avail - 1);
            let needs = needs_cut(view, cx, room);
            let agents = agents_section(view, cx, avail - needs.len(), false);
            (STEPS.len() as u8, false, needs, agents, shells)
        }
    };
    let mut rows = top;
    frame.status = status;
    frame.card = card;
    frame.card_title = card_title;
    frame.card_footer = card_footer;
    frame.toast = toast;
    frame.toast_until = toast_until;
    // (No gap above NEEDS YOU when nothing is on top.)
    if gaps && !rows.is_empty() {
        rows.push(Row::blank(w));
    }
    let at = rows.len();
    rows.extend(needs);
    frame.needs = at..rows.len();
    if gaps {
        rows.push(Row::blank(w));
    }
    let at = rows.len();
    rows.extend(agents);
    frame.agents = at..rows.len();
    if gaps && !shells.is_empty() {
        rows.push(Row::blank(w));
    }
    let at = rows.len();
    rows.extend(shells);
    frame.shells = at..rows.len();
    // Spare rows collect here; the spaces sit right above the last row.
    let spaces = spaces_fit(view, w, (h - 1).saturating_sub(rows.len()));
    while rows.len() + spaces.len() < h - 1 {
        rows.push(Row::blank(w));
    }
    let at = rows.len();
    rows.extend(spaces);
    frame.spaces = at.min(rows.len())..rows.len();
    rows.push(last);
    rows.truncate(h);
    frame.spin = rows.iter().any(|r| r.anim);
    frame.rows = rows;
    frame.step = step;
    frame.agents_step = agents_step;
    frame
}

/// Draw a frame into a ratatui buffer: each row's background (the
/// terminal's own, [`Color::Reset`], unless the row is tinted) across the
/// whole line, then its segments at the cells the layout counted (a segment
/// never spills into the next, whatever width ratatui gives a grapheme);
/// then the card's rounded [`Block`] over its rows.
pub fn paint(frame: &Frame, buf: &mut Buffer) {
    let area = buf.area;
    for (y, row) in frame.rows.iter().enumerate().take(usize::from(area.height)) {
        let y = area.y + y as u16;
        let row_bg = row.bg.map_or(Color::Reset, Ink::color);
        buf.set_style(Rect::new(area.x, y, area.width, 1), Style::default().fg(palette::color("text")).bg(row_bg));
        put(buf, 0, y, &row.segs, row_bg);
    }
    let card = frame.card.start.min(usize::from(area.height))..frame.card.end.min(usize::from(area.height));
    if card.len() >= 2 {
        let rect = Rect::new(area.x, area.y + card.start as u16, area.width, card.len() as u16);
        Block::bordered()
            .border_type(BorderType::Rounded)
            .border_style(Style::default().fg(palette::color("surface1")))
            .render(rect, buf);
        put(buf, 2, area.y + card.start as u16, &frame.card_title, Color::Reset);
        put(buf, 2, area.y + card.end as u16 - 1, &frame.card_footer, Color::Reset);
    }
}

/// `segs` drawn on row `y` from cell `x0` of the buffer's area, each at the
/// cells the layout counted (a segment never spills into the next, whatever
/// width ratatui gives a grapheme), clipped at the area's edge.
fn put(buf: &mut Buffer, x0: usize, y: u16, segs: &[Seg], row_bg: Color) {
    let area = buf.area;
    let mut x = x0;
    for s in segs {
        let sw = dwidth(&s.text);
        let room = usize::from(area.width).saturating_sub(x);
        if sw > 0 && room > 0 {
            let mut st = Style::default().fg(s.fg.color()).bg(s.bg.map_or(row_bg, Ink::color));
            if s.bold {
                st = st.add_modifier(Modifier::BOLD);
            }
            buf.set_style(Rect::new(area.x + x as u16, y, sw.min(room) as u16, 1), st);
            buf.set_stringn(area.x + x as u16, y, &s.text, sw.min(room), st);
        }
        x += sw;
    }
}

fn sgr(fg: Color, bg: Color, m: Modifier) -> String {
    let mut s = String::from("\x1b[0");
    match fg {
        Color::Rgb(r, g, b) => s.push_str(&format!(";38;2;{r};{g};{b}")),
        Color::Indexed(n) => s.push_str(&format!(";38;5;{n}")),
        _ => {}
    }
    match bg {
        Color::Rgb(r, g, b) => s.push_str(&format!(";48;2;{r};{g};{b}")),
        Color::Indexed(n) => s.push_str(&format!(";48;5;{n}")),
        _ => {}
    }
    if m.contains(Modifier::BOLD) {
        s.push_str(";1");
    }
    s.push('m');
    s
}

/// A buffer as truecolour ANSI lines (`sidebar --once`): one SGR per style
/// change, the cells under a wide grapheme skipped as a terminal would. A
/// [`Color::Reset`] background writes no background at all.
pub fn to_ansi(buf: &Buffer) -> String {
    let mut out = String::new();
    let area = buf.area;
    for y in area.top()..area.bottom() {
        let mut last = None;
        let mut skip = 0usize;
        for x in area.left()..area.right() {
            if skip > 0 {
                skip -= 1;
                continue;
            }
            let c = &buf[(x, y)];
            let key = (c.fg, c.bg, c.modifier);
            if last != Some(key) {
                out.push_str(&sgr(c.fg, c.bg, c.modifier));
                last = Some(key);
            }
            out.push_str(c.symbol());
            skip = c.symbol().width().saturating_sub(1);
        }
        out.push_str("\x1b[0m\n");
    }
    out
}

#[cfg(test)]
mod tests;
