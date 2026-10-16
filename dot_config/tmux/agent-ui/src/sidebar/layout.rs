//! The sidebar's pure half: a [`ViewModel`] and a size in, every row to draw
//! (each exactly `width` cells, with what a click on each cell does) out. No
//! I/O, so every density rule is unit-tested.
//!
//! ## Look: tiles on the canvas, not line art
//!
//! Textual's way: every surface is a filled block ("tile") on the terminal's
//! own background, never a box drawn in lines. A tile leaves one canvas
//! column at each side of the sidebar ([`margin`], [`tile_row`]) and keeps a
//! cell of padding inside; a half-block row (`▀` / `▄` in the tile's colour
//! on the canvas, [`pad_row`]) pads it, so it reads as a raised button. List
//! tiles (agents, NEEDS YOU cards, shell buttons) keep their pad BELOW them
//! ([`PAD_BELOW`]): a header sits right on its first tile, and half a row of
//! canvas parts one tile from the next. Without room for pads, tiles sit edge
//! to edge in two alternating shades ([`shade`]: `tile` / `tile_alt`).
//! Chips take a page out of the tmux status line (its catppuccin blocks,
//! square ends): a [`module`] is ` key ` bold in dark ink on a colour, then
//! ` label ` on `surface0` (the spaces, the shells in one line, the scope
//! toggle ` ⌥V `, the NEEDS YOU and PARKED counts, `+K`); the client's space
//! is `@space_here`, one chip in its colour ending in the powerline arrow
//! ([`ARROW`]). Section and space headers are text on the canvas. The only
//! line art left is a toast card's rounded border.
//!
//! ## Shape: the usage panel, NEEDS YOU, the agents (the body), the shells, SPACES, PARKED
//!
//! ```text
//!  ▄ prefix ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄   the usage panel: a tile; the key
//!   S ━━━━━━━━┃━━━━━━━━━━━  21% (-4%)    table's mark a chip on its top pad;
//!   W ━━━━━━━━━━━━━┃━━━━━━  60% (+17%)   one meter per window (fill = used,
//!  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀   ┃ = where the pace expects it)
//!
//!  NEEDS YOU                        1   (the count a square chip)
//!  ▌ g  1Password Sign-ins      ✓ 4m    cards: tiles tinted in the state's
//!  ▌    Your four calls are done        colour, a ▌ stripe in it
//!  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
//!
//!  AGENTS            ⌥V  all spaces     (the scope: a module once it toggles)
//!  ● main                     ◐2 ○3     space header: dot in @space_color
//!  ▌ d  Tmux Agent Sidebar ⠹ working    the client's agent: `tile_hi`, a wide
//!  ▌  › Running cargo test        26s   stripe; every agent two lines: working,
//!  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀   its live activity (`›`, from its
//!  ▎ q  Pause Resume Agents    ✓ done   transcript, see `activity`); else a
//!  ▎    Merged the pause key       2m   dim message (for idle, its last
//!  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀   outcome; maybe blank), the age at its
//!     h  Canvas Syllabus         idle   end; a `▎` stripe in the state's
//!        Synced 4 assignments      1h   colour (idle: none)
//!  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
//!
//!  SHELLS                               the client's space's shells, one-row
//!     ⌥1  ⌂ ~                           buttons (⌥N); the active one lifted;
//!  ▌ ⌥2  nvim                           one line of modules when short
//!
//!  SPACES                               one row per space, ⌃N order: ` N ` on
//!   1 main ▶               ◐2 ○3        its colour, the name, its counts; the
//!   2  bai                    ○2        client's `@space_here` (red while the
//!   3  cs35l                            prefix is armed)
//!
//!  PARKED  4                            the stash (a click: its picker); a
//!    ⏸ Canvas export          main      message or the watcher on the right;
//!     +1 more                           up to 3 parked tabs when rows allow
//! ```
//!
//! (`▶`: the arrow.) Without a [`Bar`] (no client space, no usage, nothing
//! selected) the top is the classic ` agents` / ` space` title with the
//! counts.
//!
//! ## The bottom: SPACES and PARKED
//!
//! Spaces are listed nowhere else, so their gap and one line of chips are
//! held back before anything else is laid out ([`spaces_min`]), with the
//! PARKED header (the old last row). The steps that keep decoration (gaps,
//! pads) must also leave the SPACES list its rows ([`spaces_listed`]); then
//! the bottom takes the rows left over, the richest form that fits
//! ([`bottom`]): every space a row; else the client's space and those with
//! an attention agent and `+K more spaces`; else chips on as few as one
//! line; the gap above PARKED last. Spare rows then buy spacing back (the
//! section gaps, the cards' pads, the gaps between spaces in AGENTS) and,
//! last, PARKED's tabs (beside the full SPACES list only).
//!
//! ## Toasts: nvim-notify cards over the top, so nothing moves
//!
//! notify.sh's messages (`@toast`, [`Toast`]) float OVER the frame as cards
//! stacked from its top (the screen's top right), oldest on top, at most
//! [`TOASTS_SHOWN`] ([`toast_stack`], [`toast_card`]):
//!
//! ```text
//!  ╭─  Done ──────────────── 10:02 ─╮   info  blue, ok  green,
//!  │ parked Canvas Syllabus           │   warn  peach, error  red
//!  ╰──────────────────────────────────╯
//! ```
//!
//! A rounded border in the kind's colour over a fill of it, a canvas margin
//! each side; the icon and title (notify.sh `-T`, else Info / Done /
//! Warning / Error) bold in the top border, the local time it was posted
//! right-aligned in it; the text wrapped into at most [`TOAST_LINES`] rows.
//! The rows beneath keep their place and what they stand for. A click
//! anywhere on a card dismisses that toast in this sidebar
//! ([`Target::Toast`]); each expires at its own deadline by the frame's
//! clock, and [`Frame::toast_until`] is the earliest one on screen. Short
//! frames drop the oldest card first, then body lines; the last row is
//! never covered, nor are the key-table marks and the selection hint (the
//! cards start under them). A sidebar too small for a card (under 16
//! columns or 4 free rows) shows the newest toast as one compact row, down
//! to 2 free rows: a toast notify.sh posted is never lost to a size.
//!
//! ## Density ladder: agents lose detail last; never scroll, never drop an agent silently
//!
//! The agents section is the body. A frame takes the first step at which
//! every agent still gets its full detail ([`STEPS`]):
//!
//! 1. SPACES and PARKED get the rows left over (above: "The bottom"); their
//!    minimum is held back before anything else is laid out;
//! 2. the blank gaps go (between sections, between spaces);
//! 3. the pads go: the NEEDS YOU cards' and shell buttons' first, then the
//!    agents' (pure decoration goes before anything that says something);
//! 4. NEEDS YOU cards go from 2 lines to 1; then the shells to one line;
//! 5. only then do the agents compress (below), NEEDS YOU kept to the rows
//!    the agents' one-line shape leaves (at least 2, and at least a third of
//!    the body), cut with a `… N more need you` line.
//!
//! The agents' own ladder keeps every agent row the same height: the idle
//! agents of a space fold into one `○○○ 3 idle · Name, Name…` tile; then
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
//! Every agent tile and NEEDS YOU card starts (after its stripe) with the
//! agent's lifetime letter (`@agent_key`, assigned by
//! [`crate::menu::letters::of_view`], the menu's own function) as a key-cap
//! ([`Badge`]): a raised `surface1` chip, peach while ⌥W's jump table is
//! armed. Agents without one leave it blank; with no letter anywhere the
//! column is not drawn.
//!
//! ## Scope
//!
//! [`crate::view::Scope`] (`@spaces` / `@strip_scope`): in SPACE scope the
//! agents section shows only the client's session (its header and ` no
//! agents in this space` when it has none). NEEDS YOU, the spaces and parked
//! stay global. The AGENTS header's scope word is a toggle button once the
//! scope is a thing.
//!
//! ## Space order
//!
//! ONE order everywhere: `@space_index` (⌃N), spaces without one after, by
//! name ([`space_order`]). The agents section's groups and the spaces
//! buttons both sort by it.
//!
//! ## Colour
//!
//! The canvas is the terminal's own background ([`Color::Reset`], and
//! [`Ink::Canvas`] for a tiled row's margins), so the split is WezTerm's
//! colour. Surfaces ([`palette::UI`]): `tile`, `tile_alt` (the zebra),
//! `tile_hi` (the client's agent, the active shell), NEEDS YOU cards and
//! toast cards (their colour mixed over the canvas, [`Ink::Mix`]), the stash
//! selection (mauve, mixed), and the solid chips (key-caps, the key-table
//! marks, the status-line modules). Dim text on a surface is `overlay2`.

use super::status::{ink_on, tmux_colour, Bar, Kind, Shell, Toast, ToastId, Usage};
use crate::actions::watcher_problem;
use crate::model::{Cat, Counts, Flag};
use crate::palette;
use crate::text::{clip, dwidth, fit_label, plain, sanitize};
use crate::view::{Agent, Need, Space, ViewModel};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
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
    /// The canvas itself: the terminal's own background ([`Color::Reset`]),
    /// for the margin cells of a row that is otherwise a tile.
    Canvas,
}

impl Ink {
    pub fn color(self) -> Color {
        match self {
            Ink::Pal(p) => palette::color(p),
            Ink::Rgb(r, g, b) => Color::Rgb(r, g, b),
            Ink::Idx(n) => Color::Indexed(n),
            Ink::Mix(p, pct) => palette::mix(p, pct),
            Ink::Canvas => Color::Reset,
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

// ---------------------------------------------------------------- surfaces

/// The canvas columns a tile leaves at each side of the sidebar (none in a
/// frame too narrow to spare them).
fn margin(w: usize) -> usize {
    usize::from(w >= 8)
}

/// A tile's width between the margins.
fn inner(w: usize) -> usize {
    w - 2 * margin(w)
}

/// `n` cells of the canvas, explicitly (a tiled row's margin).
fn bare(n: usize) -> Seg {
    Seg { bg: Some(Ink::Canvas), ..seg(" ".repeat(n), "text") }
}

/// One row of a TILE (a raised surface): `left` and `right` fitted into the
/// tile's width on `bg`, a canvas margin each side. The row's `bg` is the
/// tile's (segments without their own sit on it).
fn tile_row(w: usize, bg: Ink, left: Vec<Seg>, right: Vec<Seg>) -> Row {
    let m = margin(w);
    let mut segs = Vec::new();
    if m > 0 {
        segs.push(bare(m));
    }
    segs.extend(fit(inner(w), left, right, None));
    if m > 0 {
        segs.push(bare(m));
    }
    Row::new(segs).bg(Some(bg))
}

/// `row` (built [`inner`] wide) as a tile row on `bg`, its targets moved
/// past the margin.
fn into_tile(row: Row, w: usize, bg: Ink) -> Row {
    let m = margin(w) as u16;
    let mut t = tile_row(w, bg, row.segs, vec![]);
    t.targets = row.targets.into_iter().map(|(a, b, x)| (a + m, b + m, x)).collect();
    Row { covers: row.covers, anim: row.anim, ..t }
}

/// The half-block above a tile (`▄`, its top padding: half a row of canvas,
/// then half a row of the tile) or below one (`▀`): the tile's colour on the
/// canvas, between the margins. Not a target, stands for no one.
const PAD_TOP: char = '▄';
const PAD_BOTTOM: char = '▀';

fn pad_row(w: usize, bg: Ink, glyph: char) -> Row {
    pad_with(w, bg, glyph, Vec::new())
}

/// Where a list tile (an agent, a NEEDS YOU card, a shell button) keeps its
/// half-block pad: BELOW it, so a section or space header sits right on top
/// of its first tile (a label for them) and half a row of canvas parts one
/// tile from the next, and the last from whatever follows.
const PAD_BELOW: bool = true;

/// A list tile's rows (on `bg`) with its pad when `pads`.
fn padded(mut rows: Vec<Row>, w: usize, bg: Ink, pads: bool) -> Vec<Row> {
    if pads {
        if PAD_BELOW {
            rows.push(pad_row(w, bg, PAD_BOTTOM));
        } else {
            rows.insert(0, pad_row(w, bg, PAD_TOP));
        }
    }
    rows
}

/// A pad row carrying `chips` (solid blocks) from its third cell: a tab on
/// the tile's top edge.
fn pad_with(w: usize, bg: Ink, glyph: char, chips: Vec<Seg>) -> Row {
    let m = margin(w);
    let iw = inner(w);
    let lead = usize::from(!chips.is_empty() && iw > 2);
    let chips = clip_segs(chips, iw.saturating_sub(2));
    let mut segs = vec![seg(" ".repeat(m), "text"), seg(glyph.to_string().repeat(lead), bg)];
    let rest = iw - lead - width(&chips);
    segs.extend(chips);
    segs.push(seg(glyph.to_string().repeat(rest), bg));
    segs.push(seg(" ".repeat(m), "text"));
    Row::new(segs.into_iter().filter(|s| !s.text.is_empty()).collect())
}

/// The status line's arrow tail (Nerd Font powerline U+E0B0): the client's
/// space chip ends in it, as `@space_here` does.
const ARROW: &str = "\u{e0b0}";

/// A status-line module, the tmux bar's catppuccin blocks (square ends, no
/// caps): ` key ` bold in dark ink on its colour, then ` label ` on
/// `surface0` (no label: the key alone). Segments of the label without a
/// background of their own sit on `surface0`.
fn module(key: &str, key_bg: impl Into<Ink>, label: Vec<Seg>) -> Vec<Seg> {
    let bg = key_bg.into();
    let mut out = vec![block(format!(" {key} "), ink_on(bg), bg)];
    if !label.is_empty() {
        let s0 = Ink::Pal("surface0");
        out.push(on(" ", "text", s0));
        out.extend(label.into_iter().map(|s| Seg { bg: s.bg.or(Some(s0)), ..s }));
        out.push(on(" ", "text", s0));
    }
    out
}

/// A label alone as a module's `surface0` half (no key): ` label `.
fn label_module(label: Vec<Seg>) -> Vec<Seg> {
    let s0 = Ink::Pal("surface0");
    let mut out = vec![on(" ", "text", s0)];
    out.extend(label.into_iter().map(|s| Seg { bg: s.bg.or(Some(s0)), ..s }));
    out.push(on(" ", "text", s0));
    out
}

/// The zebra: adjacent tiles with no gap between them alternate two shades
/// so each still reads as its own; with gaps they are all `tile`.
fn shade(k: usize, gaps: bool) -> Ink {
    Ink::Pal(if gaps || k % 2 == 0 { "tile" } else { "tile_alt" })
}

/// What a click on a cell does.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Target {
    /// An agent row or a needs card: agent-jump.sh goto (`Actions::go`,
    /// Strip.click "goto").
    Go { win: String, session: String },
    /// A SPACES row, chip or space header: that session's active window (Strip.click "session").
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
    /// PARKED (its header or a parked tab): the stash picker, exactly prefix h's
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
    /// The panel's content rows (the selection hint, usage, the stale save;
    /// the key-table marks without a panel): empty without a [`Bar`].
    pub status: Range<usize>,
    /// The usage panel's rows, its two half-block pads included; empty when
    /// the frame is too short (or narrow) for one.
    pub card: Range<usize>,
    /// The rows the toast cards are drawn over (from the top, under the
    /// key-table marks and the selection hint; the rows beneath keep their
    /// place and what they stand for). Empty: none drawn.
    pub toast: Range<usize>,
    /// When the first toast on screen expires (epoch milliseconds, rounded
    /// up): the sidebar wakes then to draw the frame without it.
    pub toast_until: Option<i64>,
    /// The rows of each section, header included (empty: not drawn).
    pub needs: Range<usize>,
    pub agents: Range<usize>,
    /// This space's shells: the section, or one line of modules.
    pub shells: Range<usize>,
    /// The SPACES section: its gap, then its header and one row per space
    /// (or the client's and the attention ones and `+K more`), or, short
    /// of rows, the spaces as chips.
    pub spaces: Range<usize>,
    /// The PARKED section, at the very bottom: its gap (when room), its
    /// header (the stash's count, the message or watcher problem on the
    /// right) and, with rows to spare, up to 3 parked tabs.
    pub parked: Range<usize>,
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
/// Text on the canvas, like the section headers.
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

/// A shell as the status line's `@space_shell` module: ` ⌥2 ` on blue
/// (peach for the active one, the stash's mauve for a selected one), then
/// ` nvim ` on `surface0`, square ends.
fn shell_pill(s: &Shell) -> Item {
    let label = format!("{}{}", if s.home { "⌂" } else { "" }, clip(&s.label, 12));
    let key = if s.selected {
        "mauve"
    } else if s.active {
        "peach"
    } else {
        "blue"
    };
    let label = vec![Seg { bold: s.active, ..seg(label, if s.active { "text" } else { "sub" }) }];
    let segs = match shell_key(s) {
        Some(k) => module(&k, key, label),
        None => label_module(label),
    };
    item(segs, Some(Target::Shell { win: s.win.clone(), session: s.session.clone() }))
}

/// The key-table marks, solid chips: a red ` prefix ` while the client's key
/// table is `prefix`; a peach ` ⌥W jump ` and `type a letter` while it is
/// `jump`. They sit as a tab on the usage panel's top pad, so arming a
/// table never changes the panel's height; a frame too short for the panel
/// gives them a row of their own. No space chip: the SPACES section at the
/// bottom already mark the client's space.
fn marks(bar: &Bar) -> Vec<Seg> {
    if bar.prefix {
        vec![block(" prefix ", "crust", "red")]
    } else if bar.jump {
        vec![block(" ⌥W jump ", "crust", "peach"), on(" type a letter ", "peach", "surface1")]
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
/// a progress bar (Textual's: `━` filled in the label's colour over a
/// `surface1` track) with a tick (`┃`) where the pace expects it, the
/// percent, the signed delta (peach when over pace, else dim) and, when it
/// fits, when the window resets (`↻3h`, dim). The reset goes first, then
/// the delta, when the bar would be too short. Dim is `overlay2`: the meter
/// sits on the usage panel.
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
            r.push(seg(format!(" {delta:>6}"), if over { "peach" } else { "overlay2" }));
        }
        if let Some(rs) = resets.as_ref().filter(|_| with_reset) {
            r.push(seg(format!(" {rs:>4}"), "overlay2"));
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
                ("━", Ink::Pal("surface1"))
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

// ---------------------------------------------------------------- toasts

/// A toast's icon (Nerd Font), default title and colour: info  Info
/// blue, ok  Done green, warn  Warning peach, error  Error red.
fn toast_look(k: Kind) -> (&'static str, &'static str, Hue) {
    match k {
        Kind::Info => ("\u{f05a}", "Info", "blue"),
        Kind::Ok => ("\u{f058}", "Done", "green"),
        Kind::Warn => ("\u{f071}", "Warning", "peach"),
        Kind::Error => ("\u{f057}", "Error", "red"),
    }
}

/// How strongly a toast card's fill takes its kind's colour (percent over
/// the canvas).
const TOAST_TINT: u8 = 12;

/// At most this many toast cards at once (the newest), and body lines each.
const TOASTS_SHOWN: usize = 3;
const TOAST_LINES: usize = 3;

/// When a toast was posted, as its card says it (`10:02`, local time:
/// [`crate::text::clock_hm`], libc `localtime_r`); "" if the C library
/// says no.
fn posted_at(t: &Toast) -> String {
    crate::text::clock_hm(t.at_ms.div_euclid(1000))
}

/// One toast as a card (nvim-notify's): a rounded border in the kind's
/// colour over a fill of that colour mixed over the canvas, a canvas
/// margin each side; the icon and title (the `-T` one, else the kind's)
/// bold in the top border, the local time it was posted right-aligned in
/// it (dim; dropped when narrow); the text word-wrapped into at most
/// `lines` rows, `…` on the last when more is left. Every row of it
/// dismisses it here.
///
/// ```text
/// ╭─  Done ──────────────── 10:02 ─╮
/// │ parked Canvas Syllabus           │
/// ╰──────────────────────────────────╯
/// ```
fn toast_card(t: &Toast, w: usize, lines: usize) -> Vec<Row> {
    let (icon, default, hue) = toast_look(t.kind);
    let fill = Ink::Mix(hue, TOAST_TINT);
    let iw = inner(w);
    let line = |n: usize| seg("─".repeat(n), hue);
    let title = format!("{icon} {}", t.title.as_deref().unwrap_or(default));
    // The top border: `╭─ ` title ` ─…─ ` time ` ─╮`.
    let mut right = vec![seg(" ", "text"), seg(posted_at(t), "overlay2"), seg(" ", "text"), line(1), seg("╮", hue)];
    let left = vec![seg("╭", hue), line(1), seg(" ", "text"), bold(title, hue), seg(" ", "text")];
    if width(&left) + width(&right) + 1 > iw || right[1].text.is_empty() {
        right = vec![line(1), seg("╮", hue)];
    }
    let rw = width(&right);
    let mut top = clip_segs(left, iw.saturating_sub(rw + 1));
    top.push(line(iw.saturating_sub(width(&top) + rw)));
    top.extend(right);
    let id = Target::Toast(t.id());
    let mut rows = vec![tile_row(w, fill, top, vec![])];
    for l in wrap(&t.text, iw.saturating_sub(4), lines) {
        rows.push(tile_row(w, fill, vec![seg("│ ", hue), seg(l, "text")], vec![seg(" │", hue)]));
    }
    rows.push(tile_row(w, fill, vec![seg("╰", hue), line(iw.saturating_sub(2))], vec![seg("╯", hue)]));
    rows.into_iter().map(|r| r.click(w, id.clone())).collect()
}

/// A card needs this many columns; narrower, a toast is a compact row.
const TOAST_CARD_W: usize = 16;

/// One toast in a single row, for a sidebar too small for a card: the icon
/// bold in the kind's colour and the text (clipped) on the card's fill.
/// The row dismisses it.
fn toast_compact(t: &Toast, w: usize) -> Row {
    let (icon, _, hue) = toast_look(t.kind);
    let fill = Ink::Mix(hue, TOAST_TINT);
    tile_row(w, fill, vec![bold(format!("{icon} "), hue), seg(t.text.clone(), "text")], vec![]).click(w, Target::Toast(t.id()))
}

/// The live toasts (at `now`) as stacked cards from the top of `h` rows of
/// a frame `w` wide, oldest on top, the newest [`TOASTS_SHOWN`] at most, in
/// at most `h - 1` rows (the frame's last row stays): the oldest go first,
/// then body lines, when they do not fit. Too small for even one card
/// (under [`TOAST_CARD_W`] columns or 4 rows): the newest as a compact row
/// ([`toast_compact`]), down to 2 rows. → (rows, the earliest deadline
/// among them).
fn toast_stack(toasts: &[Toast], w: usize, h: usize, now: f64) -> (Vec<Row>, Option<i64>) {
    let mut live: Vec<&Toast> = toasts.iter().filter(|t| t.live(now)).collect();
    live.sort_by_key(|t| (t.at_ms, t.seq));
    let room = h.saturating_sub(1);
    let Some(&newest) = live.last() else { return (Vec::new(), None) };
    if room == 0 || w == 0 {
        return (Vec::new(), None);
    }
    let mut live = live.split_off(live.len().saturating_sub(TOASTS_SHOWN));
    if w >= TOAST_CARD_W {
        for lines in (1..=TOAST_LINES).rev() {
            while !live.is_empty() {
                let rows: Vec<Row> = live.iter().flat_map(|t| toast_card(t, w, lines)).collect();
                if rows.len() <= room {
                    return (rows, live.iter().map(|t| until_ms(t)).min());
                }
                if live.len() == 1 {
                    break;
                }
                live.remove(0);
            }
        }
    }
    (vec![toast_compact(newest, w)], Some(until_ms(newest)))
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

/// A toast's deadline as [`Frame::toast_until`] holds it.
fn until_ms(t: &Toast) -> i64 {
    (t.until() * 1000.0).ceil() as i64
}

/// The top of the frame ([`top_rows`]).
struct Top {
    rows: Vec<Row>,
    /// The content rows.
    status: Range<usize>,
    /// The panel's rows (empty: no panel).
    card: Range<usize>,
    /// How many of the top rows stay above any toast card: through the
    /// key-table marks and the selection hint, when shown (what a key press
    /// is waiting on is never covered).
    keep: usize,
}

/// The top of the frame. With a [`Bar`]: the usage panel, a tile with a
/// half-block pad above and below its content rows, the key-table marks a
/// tab on its top pad. It needs 16 rows and 20 columns; one meter per
/// window 30 rows (else usage in one line, from 12; the stale save from 12
/// too); the selection hint 6. Short of the panel, the marks get a row of
/// their own and the content sits on the canvas. Without a bar: the
/// classic title.
fn top_rows(view: &ViewModel, bar: &Bar, w: usize, h: usize, now: f64) -> Top {
    let mut top = Top { rows: Vec::new(), status: 0..0, card: 0..0, keep: 0 };
    if !bar_shown(bar) {
        top.rows.push(title_row(view, w));
        top.status = 1..1;
        return top;
    }
    let paneled = w >= 20 && h >= 16;
    let iw = if paneled { inner(w) } else { w };
    let mut content = Vec::new();
    let marks = marks(bar);
    let hint = bar.selecting.filter(|_| h >= 6);
    if let Some(n) = hint {
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
    if content.is_empty() || !paneled {
        // No panel: the marks on a row of their own, first.
        top.keep = usize::from(!marks.is_empty()) + usize::from(hint.is_some());
        if !marks.is_empty() {
            top.rows.push(Row::new(fit(w, [vec![seg(" ", "text")], marks.clone()].concat(), vec![], None)));
        }
        top.rows.extend(content);
        top.status = 0..top.rows.len();
        return top;
    }
    top.keep = if hint.is_some() { 2 } else { usize::from(!marks.is_empty()) };
    let bg = Ink::Pal("tile");
    top.rows.push(pad_with(w, bg, PAD_TOP, marks));
    top.rows.extend(content.into_iter().map(|r| into_tile(r, w, bg)));
    top.rows.push(pad_row(w, bg, PAD_BOTTOM));
    let n = top.rows.len();
    top.status = 1..n - 1;
    top.card = 0..n;
    top
}

// ---------------------------------------------------------------- shells

/// ` SHELLS`, then this space's shells (the client's session, ⌥N order, the
/// home first) as one-row button tiles: the ⌥N key-cap, `⌂` on the home,
/// the label. The session's active window is lifted like the client's
/// agent (`tile_hi`, a blue stripe); one in prefix v's selection on mauve.
/// With `pads` each button has its half-block pad below it; without, they
/// sit edge to edge in the zebra's two shades. A button clicks through to
/// its shell.
fn shells_section(bar: &Bar, w: usize, pads: bool) -> Vec<Row> {
    if bar.shells.is_empty() {
        return Vec::new();
    }
    let mut rows = vec![Row::new(fit(w, vec![bold(" SHELLS", "overlay")], vec![], None))];
    for (k, s) in bar.shells.iter().enumerate() {
        let (bg, edge) = if s.selected {
            (Ink::Mix("mauve", 18), seg("▌", "mauve"))
        } else if s.active {
            (Ink::Pal("tile_hi"), seg("▌", "blue"))
        } else {
            (shade(k, pads), seg(" ", "text"))
        };
        let key = shell_key(s).map_or("    ".to_string(), |k| format!(" {k} "));
        let mut left = vec![edge, on(key, if s.active { "peach" } else { "sub" }, "surface1")];
        left.push(seg(" ", "text"));
        if s.home {
            left.push(seg("⌂ ", dim(true)));
        }
        left.push(if s.active { bold(s.label.clone(), "text") } else { seg(s.label.clone(), "sub") });
        let go = Target::Shell { win: s.win.clone(), session: s.session.clone() };
        rows.extend(padded(vec![tile_row(w, bg, left, vec![]).click(w, go)], w, bg, pads));
    }
    rows
}

/// The shells in one line, for a frame with no room for their section:
/// ` SHELLS` and the shells as rounded buttons, the active one kept, `+K`
/// for those that do not fit.
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

    /// `a`'s key-cap (blank when it holds no letter): a raised chip
    /// (`surface1`) on its tile.
    fn seg(self, a: &Agent) -> Seg {
        match a.letter {
            Some(c) if self.loud => block(format!(" {c} "), "crust", "peach"),
            Some(c) => on(format!(" {c} "), "sub", "surface1"),
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

/// A working agent's live activity blurb ([`Agent::blurb`], from its
/// transcript), when it has one.
fn live_blurb(a: &Agent) -> Option<&str> {
    // Working by its state alone (`in_flight` is also an asking agent's with
    // a workflow or cua flag: its line says what it needs).
    (a.cat == Some(Cat::Working)).then_some(a.blurb.as_deref()).flatten().filter(|b| !b.trim().is_empty())
}

/// What a two-line agent row says under its title: a working agent's live
/// activity blurb ([`live_blurb`]); else the [`message`]; else, for an
/// idle agent, its last turn's outcome (what it last said is still true of
/// it); else nothing (a working agent's old outcome never reads as what it
/// does now). Every agent row is two lines either way.
fn second_line(a: &Agent, now: f64) -> String {
    if let Some(b) = live_blurb(a) {
        return sanitize(b);
    }
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

/// The tile an agent sits on: prefix v's selection mauve, the client's own
/// agent `tile_hi`, else `base` (the zebra's shade).
fn agent_bg(a: &Agent, base: Ink) -> Ink {
    if a.selected {
        Ink::Mix("mauve", 18)
    } else if a.is_current {
        Ink::Pal("tile_hi")
    } else {
        base
    }
}

/// One agent as a tile on `base` ([`agent_bg`]): a state stripe at its left
/// edge (`▎` in the state's colour; none when idle; the client's agent a
/// wider `▌`, blue when idle; the selection's mauve), its key-cap, the title
/// (bold unless idle), a flag (⚙ workflow, ◎ cua), and the state word on
/// the right, the age beside it; with `two`, a dim second line
/// ([`second_line`], maybe blank) with the age at its end, so every agent
/// row is the same height. Both lines click through.
fn agent_rows(a: &Agent, cx: Cx, two: bool, base: Ink) -> Vec<Row> {
    let w = inner(cx.w);
    let go = Target::Go { win: a.window_id.clone(), session: a.session.clone() };
    let msg = (two && cx.seconds()).then(|| second_line(a, cx.now));
    let word = state_word(a, cx);
    let bg = agent_bg(a, base);
    let busy = word.as_ref().filter(|x| x.word != "idle").map(|x| x.hue);
    let edge = if a.selected {
        seg("▌", "mauve")
    } else if a.is_current {
        seg("▌", busy.unwrap_or("blue"))
    } else {
        busy.map_or_else(|| seg(" ", "text"), |h| seg("▎", h))
    };
    let age = Some(age_of(a, cx)).filter(|g| cx.ages() && !g.is_empty() && word.is_some());
    // Dim text on a tile needs the surface's dim.
    let dimc = dim(true);
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
        left.push(cx.b.seg(a));
    }
    left.push(seg(" ", "text"));
    left.push(title);
    left.extend(flag);
    let mut first = tile_row(cx.w, bg, left, right).click(cx.w, go.clone()).covering([a]);
    first.anim = word.as_ref().is_some_and(Word::working);
    let Some(m) = msg else { return vec![first] };
    let r2 = match &age {
        Some(ag) => vec![seg(format!(" {ag} "), dimc)],
        None => vec![seg(" ", "text")],
    };
    // Under the title (past the key-cap); without letters, indented anyway,
    // so a message never reads as another agent.
    let indent = lead.max(4) - 1;
    let l2 = if live_blurb(a).is_some() {
        // Live: a `›` in the key-cap's column, the words under the title.
        vec![edge, seg(" ".repeat(indent - 2), "text"), seg("› ", "blue"), seg(m, "sub")]
    } else {
        vec![edge, seg(" ".repeat(indent), "text"), seg(m, dimc)]
    };
    let second = tile_row(cx.w, bg, l2, r2).click(cx.w, go);
    vec![first, second]
}

/// THE space order, everywhere (the agents' groups, SPACES):
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

/// `○○○ 3 idle · Name, Name…`: a space's idle agents in one line, a
/// one-row tile on `bg`. No letters: it stands for several agents.
fn fold_row(idle: &[&Agent], cx: Cx, bg: Ink) -> Row {
    let k = idle.len();
    let names: Vec<String> = idle.iter().map(|a| short_title(a)).collect();
    let indent = " ".repeat(cx.b.lead());
    tile_row(
        cx.w,
        bg,
        vec![seg(format!("{indent}{} {k} idle · ", "○".repeat(k.min(4))), dim(true)), seg(names.join(", "), dim(true))],
        vec![seg(" ", "text")],
    )
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

/// A space's agent tiles in window-index order (the folded line last); the
/// agents in `two` get their second line. With `pads`, each tile has its
/// half-block pad below it ([`pad_row`]: a half-row gap between tiles);
/// without, they sit edge to edge in the zebra's two shades ([`shade`]).
fn group_rows(sp: &Space, cx: Cx, two: &[Key], fold: bool, pads: bool) -> Vec<Row> {
    let (shown, idle) = split_idle(sp, fold, cx.now);
    let mut rows: Vec<Row> = Vec::new();
    for (k, a) in shown.iter().enumerate() {
        let base = shade(k, pads);
        rows.extend(padded(agent_rows(a, cx, two.contains(&key(a)), base), cx.w, agent_bg(a, base), pads));
    }
    if !idle.is_empty() {
        let base = shade(shown.len(), pads);
        rows.extend(padded(vec![fold_row(&idle, cx, base)], cx.w, base, pads));
    }
    rows
}

/// A space's agents in `room` rows: all of them one line each if they fit,
/// else the `room - 1` that matter most ([`promotion_rank`], kept in list
/// order) and a `… N more` line for the rest.
fn partial_rows(sp: &Space, cx: Cx, room: usize) -> Vec<Row> {
    if sp.agents.len() <= room {
        return sp.agents.iter().enumerate().flat_map(|(k, a)| agent_rows(a, cx, false, shade(k, false))).collect();
    }
    let mut ranked: Vec<usize> = (0..sp.agents.len()).collect();
    ranked.sort_by_key(|&k| promotion_rank(&sp.agents[k], cx.now));
    let mut keep = ranked[..room - 1].to_vec();
    keep.sort_unstable();
    let mut rows: Vec<Row> =
        keep.iter().enumerate().flat_map(|(n, &k)| agent_rows(&sp.agents[k], cx, false, shade(n, false))).collect();
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

/// A space header in the agents section ([`group_header`]: on the canvas,
/// the whole row switches to its space).
fn is_group_header(r: &Row, w: usize) -> bool {
    r.bg.is_none() && matches!(r.targets.as_slice(), [(0, x1, Target::Session(_))] if usize::from(*x1) == w)
}

/// A blank canvas row that does nothing and stands for no one (a gap).
fn is_blank(r: &Row) -> bool {
    r.bg.is_none() && r.targets.is_empty() && r.covers.is_empty() && r.text().trim().is_empty()
}

/// How many space headers in an agents section (its first aside) have no
/// gap above them.
fn group_gaps(rows: &[Row], w: usize) -> usize {
    let heads: Vec<usize> = (0..rows.len()).filter(|&y| is_group_header(&rows[y], w)).collect();
    heads.iter().skip(1).filter(|&&y| !is_blank(&rows[y - 1])).count()
}

/// The agents section with a gap above each space header but the first
/// (spare rows spent on spacing, whatever the agents' step).
fn spaced_groups(rows: Vec<Row>, w: usize) -> Vec<Row> {
    let mut out: Vec<Row> = Vec::with_capacity(rows.len() + 4);
    let mut seen = false;
    for r in rows {
        if is_group_header(&r, w) {
            if seen && !out.last().is_some_and(is_blank) {
                out.push(Row::blank(w));
            }
            seen = true;
        }
        out.push(r);
    }
    out
}

/// ` AGENTS` and the scope on the right (`this space` / `all spaces`): a
/// status-line module once the scope is a thing (` ⌥V ` on blue, its key;
/// a click flips it too), else dim text.
fn agents_header(view: &ViewModel, w: usize) -> Row {
    let word = if view.scope.session.is_some() { "this space" } else { "all spaces" };
    let toggles = view.scope.toggles() && w > dwidth(word) + 16;
    let mut right = if toggles { module("⌥V", "blue", vec![seg(word, "sub")]) } else { vec![seg(word, "overlay")] };
    right.push(seg(" ", "text"));
    let ww = width(&right);
    let mut row = Row::new(fit(w, vec![bold(" AGENTS", "overlay")], right, None));
    if toggles {
        row.targets.push(((w - ww) as u16, (w - 1) as u16, Target::Scope));
    }
    row
}

/// The agents section (header, then each group's header and rows), at most
/// `budget` rows → (rows, its ladder step). `spaced`: a blank row between
/// groups when everything else fits; `pads`: each tile with its half-block
/// pad (the frame's [`STEPS`] say when).
///
/// Steps: 0 every agent two lines (as asked, then without the gaps between
/// groups, then without pads, zebra-shaded); 1 the same, idle folded; 2
/// every agent one line; 3 the same, idle folded; 4 spaces collapsed; 5
/// `… N more`. Never a mix of heights in one frame.
fn agents_section(view: &ViewModel, cx: Cx, budget: usize, spaced: bool, pads: bool) -> (Vec<Row>, u8) {
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
    // Full detail: the gaps between spaces go before the tiles' pads.
    for (sp, sep) in [(spaced, pads), (false, pads), (false, false)] {
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
            rows.insert(at + 1, agent_rows(&g.agents[top], cx, false, shade(0, false)).remove(0));
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

/// How strongly a NEEDS YOU card takes its state's colour: the card's own
/// shade, and the zebra's other one (cards edge to edge alternate them).
const NEED_TINT: [u8; 2] = [14, 9];

/// A NEEDS YOU card, a tile of 2 or 1 lines on its state colour mixed over
/// the canvas (`tint` percent), a `▌` stripe in that colour; the key-cap,
/// the title (bold), and on the right the state glyph and age. Line 2: the
/// reason (a `perm` prompt says so), or the state in words. With `pad`,
/// its half-block pad below it.
fn need_rows(n: &Need, cx: Cx, lines: u8, tint: u8, pad: bool) -> Vec<Row> {
    let w = cx.w;
    let iw = inner(w);
    let a = &n.agent;
    let hue = a.color.unwrap_or("overlay");
    let bg = Ink::Mix(hue, tint);
    let go = Target::Go { win: a.window_id.clone(), session: a.session.clone() };
    let card = |l: Vec<Seg>, r: Vec<Seg>| tile_row(w, bg, l, r).click(w, go.clone());
    let edge = || seg("▌", hue);
    let age = Some(age_of(a, cx)).filter(|g| cx.ages() && !g.is_empty()).map_or(String::new(), |g| format!(" {g}"));
    let right = vec![seg(a.glyph, hue), seg(format!("{age} "), dim(true))];
    let lead = cx.b.lead();
    let (proj, title) = fit_label(no_home(&a.title), iw.saturating_sub(lead + width(&right) + 1));
    let mut first = vec![edge()];
    if cx.b.on {
        first.push(cx.b.seg(a));
    }
    first.extend([seg(" ", "text"), seg(proj, dim(true)), bold(title, "text")]);
    let mut rows = vec![card(first, right)];
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
        rows.push(card(l2, vec![seg(" ", "text")]));
    }
    padded(rows, w, bg, pad)
}

/// ` NEEDS YOU` and the count as a square yellow chip, or `✓ all clear`
/// with nothing pending.
fn needs_header(view: &ViewModel, w: usize) -> Row {
    let n = view.needs.len();
    let mut right = if n > 0 { module(&n.to_string(), "yellow", vec![]) } else { vec![seg("✓ all clear", "overlay")] };
    right.push(seg(" ", "text"));
    Row::new(fit(w, vec![bold(" NEEDS YOU", if n > 0 { "yellow" } else { "overlay" })], right, None))
}

/// The whole NEEDS YOU section: its header and the cards (each with its
/// pad with `pads`, else edge to edge, zebra-shaded). Never empty: with
/// nothing pending it is the header saying so.
fn needs_block(view: &ViewModel, cx: Cx, lines: u8, pads: bool) -> Vec<Row> {
    let mut rows = vec![needs_header(view, cx.w)];
    for (k, n) in view.needs.iter().enumerate() {
        let tint = NEED_TINT[usize::from(!pads && k % 2 == 1)];
        rows.extend(need_rows(n, cx, lines, tint, pads));
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

/// The spaces SPACES lists, in [`space_order`]: every session the view
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

/// A space's colour: its `@space_color`, else the status line's default
/// (`@space_col`: peach).
fn space_colour(sp: &Space) -> Ink {
    tmux_colour(&sp.color).unwrap_or(Ink::Pal("peach"))
}

/// The client's space's chip colour: red while the prefix is armed (the
/// status line's `@space_chip`), else the space's own.
fn chip_colour(sp: &Space, prefix: bool) -> Ink {
    if prefix {
        Ink::Pal("red")
    } else {
        space_colour(sp)
    }
}

/// The client's space as the status line's `@space_here`: one solid chip in
/// its colour, ` N name ` in bold dark ink, ending in the powerline arrow.
fn here_chip(sp: &Space, prefix: bool, room: usize) -> Vec<Seg> {
    // The name gives way (clipped with `…`) so the chip keeps its arrow in
    // `room` cells.
    let num = sp.index.filter(|n| (1..=9).contains(n)).map_or(0, |n| n.to_string().len() + 1);
    let name = clip(&sanitize(&sp.name), room.saturating_sub(num + 3).max(1));
    let chip = chip_colour(sp, prefix);
    let label = match sp.index.filter(|n| (1..=9).contains(n)) {
        Some(n) => format!(" {n} {name} "),
        None => format!(" {name} "),
    };
    vec![block(label, ink_on(chip), chip), seg(ARROW, chip)]
}

/// The state glyph of a space's most urgent attention agent, in its colour.
fn alert_of(sp: &Space) -> Option<(&'static str, Hue)> {
    sp.agents.iter().filter(|a| a.attention).min_by_key(|a| a.rank).and_then(|a| a.cat).map(|c| (c.glyph(), cat_hue(c)))
}

/// A space as a status-line chip, for the one-line fallback: the client's
/// space as [`here_chip`]; another as a module, ` N ` on its colour then its
/// name on `surface0` (dim when it holds no agent), an attention agent's
/// glyph after it. A click switches the client to it.
fn space_pill(sp: &Space, prefix: bool) -> Item {
    let mut segs = if sp.is_current {
        // (A 12-cell name at most, like the others.)
        here_chip(sp, prefix, 17)
    } else {
        let quiet = sp.counts.total() == 0;
        let mut label = vec![seg(clip(&sanitize(&sp.name), 12), if quiet { dim(true) } else { "sub" })];
        if let Some((g, hue)) = alert_of(sp) {
            label.push(seg(format!(" {g}"), hue));
        }
        match sp.index.filter(|n| (1..=9).contains(n)) {
            Some(n) => module(&n.to_string(), space_colour(sp), label),
            None => label_module(label),
        }
    };
    if sp.is_current {
        if let Some((g, hue)) = alert_of(sp) {
            segs.push(seg(format!(" {g}"), hue));
        }
    }
    item(segs, Some(Target::Session(sp.name.clone())))
}

/// The spaces as chips in at most `lines` rows (the bottom's last resort):
/// all of them, else the client's space, those with an attention agent,
/// then ⌃N order, kept (shown in list order), and a `+K` module (the menu's
/// All tab) for the rest.
fn pill_rows(view: &ViewModel, w: usize, lines: usize, prefix: bool) -> Vec<Row> {
    let list = space_list(view);
    let items: Vec<Item> = list.iter().map(|sp| space_pill(sp, prefix)).collect();
    let mut rank: Vec<usize> = (0..list.len()).collect();
    rank.sort_by_key(|&i| (!list[i].is_current, !space_alert(list[i])));
    let more = |k: usize| item(module(&format!("+{k}"), "overlay2", vec![]), Some(Target::Menu(Some("all"))));
    pack(&[], &items, &rank, w, lines, &more).0
}

/// How many chip lines every space needs (at most 3).
fn pill_lines(view: &ViewModel, w: usize) -> usize {
    let items: Vec<Item> = space_list(view).iter().map(|sp| space_pill(sp, false)).collect();
    place(&items, w, usize::MAX).map_or(1, |r| r.len()).clamp(1, 3)
}

/// The rows the spaces keep however short the frame (held back before
/// anything else is laid out, agents included): a gap and one line of
/// chips. 0 with no spaces, or when `avail` is under 4.
fn spaces_min(view: &ViewModel, avail: usize) -> usize {
    if space_list(view).is_empty() || avail < 4 {
        0
    } else {
        2
    }
}

/// How many spaces the SPACES section lists whole before it prefers its
/// collapsed form when reserving rows ([`spaces_listed`]).
const SPACES_LISTED: usize = 6;

/// The rows the SPACES section stood on end needs, with the gap above
/// PARKED: every space a row (up to [`SPACES_LISTED`] of them), else the
/// collapsed form. What the steps that keep decoration leave it.
fn spaces_listed(view: &ViewModel, w: usize) -> usize {
    let n = space_list(view).len();
    if n == 0 {
        return 0;
    }
    let form = if n <= SPACES_LISTED { SpacesForm::Full } else { SpacesForm::Collapsed };
    spaces_rows(view, w, form, false).len() + 1
}

/// One space as a row of the SPACES section, the status line's modules
/// stood on end: ` N ` on the space's colour (dark bold digit), then its
/// name on the row's tile, its branch dim when there is room, its agents'
/// counts on the right (the AGENTS headers' glyphs). The client's space is
/// `@space_here`: one chip in its colour (red while the prefix is armed)
/// across number and name, ending in the arrow. A click switches to it.
fn space_row(sp: &Space, w: usize, k: usize, prefix: bool) -> Row {
    let bg = shade(k, false);
    let name = sanitize(&sp.name);
    let mut right: Vec<Seg> =
        count_segs(&sp.counts).into_iter().map(|s| if s.fg == Ink::Pal("overlay") { Seg { fg: Ink::Pal(dim(true)), ..s } } else { s }).collect();
    right.push(seg(" ", "text"));
    let mut left = if sp.is_current {
        // The counts stay; the name gives way, the arrow kept.
        here_chip(sp, prefix, inner(w).saturating_sub(width(&right) + 1))
    } else {
        let quiet = sp.counts.total() == 0;
        let num = sp.index.filter(|n| (1..=9).contains(n)).map_or(" ".to_string(), |n| n.to_string());
        vec![block(format!(" {num} "), ink_on(space_colour(sp)), space_colour(sp)), seg(" ", "text"), seg(name, if quiet { dim(true) } else { "sub" })]
    };
    if !sp.branch.is_empty() {
        let br = format!("  \u{e0a0} {}", sanitize(&sp.branch));
        if width(&left) + dwidth(&br) + width(&right) < inner(w) {
            left.push(seg(br, dim(true)));
        }
    }
    tile_row(w, bg, left, right).click(w, Target::Session(sp.name.clone()))
}

/// ` SPACES`: the section's header (nothing on the right: the scope lives
/// in AGENTS).
fn spaces_header(w: usize) -> Row {
    Row::new(fit(w, vec![bold(" SPACES", "overlay")], vec![], None))
}

/// `+K more spaces`: the ones a collapsed SPACES section leaves out (the
/// menu's All tab).
fn spaces_more(k: usize, w: usize, shade_k: usize) -> Row {
    tile_row(w, shade(shade_k, false), [module(&format!("+{k}"), "overlay2", vec![]), vec![seg(" more spaces", dim(true))]].concat(), vec![])
        .click(w, Target::Menu(Some("all")))
}

/// The SPACES section's forms, richest first: every space a row (`full`);
/// the client's space and those with an attention agent, then `+K more`;
/// else (the last resort) the chips in `lines` lines. Each starts with its
/// gap.
fn spaces_rows(view: &ViewModel, w: usize, form: SpacesForm, prefix: bool) -> Vec<Row> {
    let list = space_list(view);
    if list.is_empty() {
        return Vec::new();
    }
    let mut rows = vec![Row::blank(w)];
    match form {
        SpacesForm::Full => {
            rows.push(spaces_header(w));
            rows.extend(list.iter().enumerate().map(|(k, sp)| space_row(sp, w, k, prefix)));
        }
        SpacesForm::Collapsed => {
            rows.push(spaces_header(w));
            let kept: Vec<&&Space> = list.iter().filter(|sp| sp.is_current || space_alert(sp)).collect();
            rows.extend(kept.iter().enumerate().map(|(k, sp)| space_row(sp, w, k, prefix)));
            rows.push(spaces_more(list.len() - kept.len(), w, kept.len()));
        }
        SpacesForm::Chips(lines) => rows.extend(pill_rows(view, w, lines.max(1), prefix)),
    }
    rows
}

/// How the SPACES section is drawn ([`spaces_rows`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SpacesForm {
    Full,
    Collapsed,
    Chips(usize),
}

/// ` PARKED` and its count as a square mauve chip (the stash's colour; dim
/// with none): a click opens the same popup as prefix h ([`Target::Stash`]).
/// On the right a message (Roster.say) or the watcher's problem in red
/// (click: restart).
fn parked_header(view: &ViewModel, w: usize, msg: Option<&str>) -> Row {
    let n = view.parked.len();
    let mut left = vec![bold(" PARKED", "overlay"), seg(" ", "text")];
    if n > 0 {
        left.extend(module(&n.to_string(), "mauve", vec![]));
    } else {
        left.push(seg("none", "overlay"));
    }
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

/// How many parked tabs the PARKED section lists at most.
const PARKED_SHOWN: usize = 3;

/// The parked tabs under ` PARKED`: up to [`PARKED_SHOWN`], dim, each its
/// title and, on the right, the space it came from; `+N more` for the
/// rest. Every row opens the unpark picker.
fn parked_rows(view: &ViewModel, w: usize) -> Vec<Row> {
    let mut rows: Vec<Row> = view
        .parked
        .iter()
        .take(PARKED_SHOWN)
        .map(|p| {
            let right = vec![seg(sanitize(&p.origin), "overlay"), seg(" ", "text")];
            Row::new(fit(w, vec![seg("   ⏸ ", "overlay"), seg(sanitize(&p.agent.short_title), "overlay")], right, None)).click(w, Target::Stash)
        })
        .collect();
    let rest = view.parked.len().saturating_sub(PARKED_SHOWN);
    if rest > 0 {
        rows.push(dim_line(w, &format!("     +{rest} more")).click(w, Target::Stash));
    }
    rows
}

/// The bottom of the frame in `room` rows (at least 1): the SPACES section
/// and the PARKED section's header, the richest form that fits. As rows run
/// short the spaces collapse to the client's space and the attention ones
/// (`+K more`), then to chips on as few as one line, the gap above PARKED
/// last. (The parked tabs under the header are spare rows' business:
/// [`parked_rows`], only beside the full SPACES section.) → (the spaces
/// rows, the parked rows: its gap and header, whether the spaces are full).
fn bottom(view: &ViewModel, w: usize, room: usize, prefix: bool, msg: Option<&str>) -> (Vec<Row>, Vec<Row>, bool) {
    let header = parked_header(view, w, msg);
    let parked = |gap: bool| -> Vec<Row> { if gap { vec![Row::blank(w), header.clone()] } else { vec![header.clone()] } };
    let list = space_list(view);
    if list.is_empty() {
        return (Vec::new(), parked(room >= 2), true);
    }
    if room < 3 {
        return (Vec::new(), parked(false), false);
    }
    let chips = pill_lines(view, w);
    let collapsible = list.iter().any(|sp| !sp.is_current && !space_alert(sp));
    let mut forms: Vec<(SpacesForm, bool)> = vec![(SpacesForm::Full, true)];
    if collapsible {
        forms.push((SpacesForm::Collapsed, true));
    }
    forms.extend((1..=chips).rev().map(|l| (SpacesForm::Chips(l), true)));
    forms.extend((1..=chips).rev().map(|l| (SpacesForm::Chips(l), false)));
    for (form, gap) in forms {
        let s = spaces_rows(view, w, form, prefix);
        let p = parked(gap);
        if s.len() + p.len() <= room {
            return (s, p, form == SpacesForm::Full);
        }
    }
    (Vec::new(), parked(false), false)
}

/// One step at which agents keep full detail ([`STEPS`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Step {
    /// Blank gaps between sections (and between spaces in AGENTS).
    pub gaps: bool,
    /// Every agent tile with its half-block pad; without, they sit edge to
    /// edge, zebra-shaded.
    pub pads: bool,
    /// The same for the NEEDS YOU cards and the shell buttons.
    pub card_pads: bool,
    /// NEEDS YOU card lines.
    pub lines: u8,
}

/// The steps at which agents keep full detail, roomiest first: the gaps
/// go, then the pads (pure decoration, before anything that says
/// something: the cards' and shells' first, the agents' last), then the
/// cards' second lines. The spaces section takes what each leaves over.
pub const STEPS: [Step; 5] = [
    Step { gaps: true, pads: true, card_pads: true, lines: 2 },
    Step { gaps: false, pads: true, card_pads: true, lines: 2 },
    Step { gaps: false, pads: true, card_pads: false, lines: 2 },
    Step { gaps: false, pads: false, card_pads: false, lines: 2 },
    Step { gaps: false, pads: false, card_pads: false, lines: 1 },
];

/// The whole screen: the panel, NEEDS YOU, agents, shells, spare rows, the
/// spaces, the last row; then the live toasts as cards over its top. Always
/// exactly `height` rows of exactly `width` cells.
pub fn layout(view: &ViewModel, width: u16, height: u16, opts: &Opts) -> Frame {
    let (w, h) = (usize::from(width), usize::from(height));
    let mut frame = Frame { width, height, ..Frame::default() };
    if w == 0 || h == 0 {
        return frame;
    }
    let cx = Cx { w, b: Badge::of(view, &opts.bar), now: opts.now.unwrap_or(view.now), spin: opts.spin };
    let keep = sections(view, &mut frame, opts, cx).min(frame.rows.len());
    // The toasts float over whatever is there: nothing beneath moves, and
    // the rows they hide still stand for their agents (back when they go).
    // They start under the key-table marks and the selection hint, which
    // stay readable while a key press waits on them.
    let (cards, until) = toast_stack(&opts.bar.toasts, w, frame.rows.len() - keep, cx.now);
    let n = cards.len();
    for (y, card) in (keep..).zip(cards) {
        let under = std::mem::take(&mut frame.rows[y].covers);
        frame.rows[y] = Row { covers: under, ..card };
    }
    frame.toast = keep..keep + n;
    frame.toast_until = until;
    frame.spin = frame.rows.iter().any(|r| r.anim);
    frame
}

/// [`layout`]'s sections, into `frame`.
fn sections(view: &ViewModel, frame: &mut Frame, opts: &Opts, cx: Cx) -> usize {
    let (w, h) = (cx.w, usize::from(frame.height));
    let bar = &opts.bar;
    let Top { rows: top, status, card, keep } = top_rows(view, bar, w, h, cx.now);
    // The rows between the top and the PARKED header (held back like the
    // old last row: it carries the stash, the messages and the watcher).
    let avail = h.saturating_sub(top.len() + 1);
    if avail == 0 {
        // No room for sections: one row still counts every agent of the
        // scope, then the parked row.
        let all: Vec<&Agent> = groups(view).into_iter().flat_map(|s| s.agents.iter()).collect();
        let mut rows = vec![top.into_iter().next().unwrap_or_else(|| Row::blank(w))];
        rows.push(if all.is_empty() { Row::blank(w) } else { more_row(w, &all, 0) });
        rows.push(parked_header(view, w, opts.msg.as_deref()));
        rows.truncate(h);
        // (Its header is the third row: empty when the frame cut it.)
        frame.parked = if rows.len() == 3 { 2..3 } else { rows.len()..rows.len() };
        frame.rows = rows;
        frame.step = STEPS.len() as u8;
        frame.status = 0..0;
        return keep.min(1);
    }
    // The spaces' gap and one line of chips are held back first: spaces are
    // listed nowhere else, so the section never goes before the agents
    // lose detail.
    let min = spaces_min(view, avail);
    let avail = avail - min;
    // Every step with the shells' whole section (each button with its pad
    // while the gaps last, then edge to edge), then the last step with their
    // one line: one ladder, so as the frame shrinks neither the step nor the
    // shells' form ever comes back.
    let (padded, full, line) = (shells_section(bar, w, true), shells_section(bar, w, false), shells_line(bar, w));
    let end = STEPS.len() - 1;
    let tries = STEPS.iter().enumerate().map(|(i, s)| (i, *s, if s.card_pads { &padded } else { &full })).chain([(end, STEPS[end], &line)]);
    // The SPACES list stood on end says something (each space's counts):
    // the steps that keep decoration (gaps, pads) must leave it its rows
    // too; the plain steps only its held-back minimum.
    let listed = if min == 0 { 0 } else { spaces_listed(view, w).saturating_sub(min) };
    let fits = tries.into_iter().find_map(|(i, Step { gaps, pads, card_pads, lines }, shells)| {
        let needs = needs_block(view, cx, lines, card_pads);
        let agents = agents_section(view, cx, usize::MAX, gaps, pads).0;
        let gap_rows = usize::from(gaps) * (2 + usize::from(!shells.is_empty()));
        let room = if gaps || pads || card_pads { avail.saturating_sub(listed) } else { avail };
        (needs.len() + agents.len() + shells.len() + gap_rows <= room).then(|| (i as u8, gaps, lines, needs, agents, shells.clone()))
    });
    let (step, gaps, lines, mut needs, (mut agents, agents_step), shells) = match fits {
        Some((i, gaps, lines, needs, agents, shells)) => (i, gaps, lines, needs, (agents, 0), shells),
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
            let agents = agents_section(view, cx, avail - needs.len(), false, false);
            (STEPS.len() as u8, false, 1, needs, agents, shells)
        }
    };
    // The section gaps the step keeps. (No gap above NEEDS YOU when nothing
    // is on top, none above an empty section.)
    let mut gap_needs = gaps && !top.is_empty();
    let mut gap_agents = gaps && !agents.is_empty();
    let mut gap_shells = gaps && !shells.is_empty();
    // The SPACES and PARKED sections take the rows left over (the richest
    // form that fits: they are content); what they leave buys spacing back,
    // never detail: the agents' shape (and so the ladder's steps) is
    // already settled, and their pads never come back.
    let used = |n: &[Row], a: &[Row], g: [bool; 3]| top.len() + n.len() + a.len() + shells.len() + g.iter().filter(|x| **x).count();
    let room = h.saturating_sub(used(&needs, &agents, [gap_needs, gap_agents, gap_shells]));
    let (spaces, mut parked, full) = bottom(view, w, room, bar.prefix, opts.msg.as_deref());
    let mut spare = room.saturating_sub(spaces.len() + parked.len());
    // 1. The gap above each section header: AGENTS, SHELLS, NEEDS YOU.
    for (gap, there) in [(&mut gap_agents, !agents.is_empty()), (&mut gap_shells, !shells.is_empty()), (&mut gap_needs, !top.is_empty())] {
        if there && !*gap && spare > 0 {
            *gap = true;
            spare -= 1;
        }
    }
    // 2. The NEEDS YOU cards' pads: every card or none (only when none were
    // cut: a `… N more need you` section stays as it is).
    if needs.len() == needs_block(view, cx, lines, false).len() {
        let padded = needs_block(view, cx, lines, true);
        let more = padded.len() - needs.len();
        if more > 0 && more <= spare {
            needs = padded;
            spare -= more;
        }
    }
    // 3. A gap above each space header in AGENTS (but the first): all or none.
    let missing = group_gaps(&agents, w);
    if missing > 0 && missing <= spare {
        agents = spaced_groups(agents, w);
        spare -= missing;
    }
    // 4. The parked tabs under their header (beside the full SPACES list).
    let tabs = parked_rows(view, w);
    if full && !tabs.is_empty() && tabs.len() <= spare {
        parked.extend(tabs);
    }
    let mut rows = top;
    frame.status = status;
    frame.card = card;
    if gap_needs {
        rows.push(Row::blank(w));
    }
    let at = rows.len();
    rows.extend(needs);
    frame.needs = at..rows.len();
    if gap_agents {
        rows.push(Row::blank(w));
    }
    let at = rows.len();
    rows.extend(agents);
    frame.agents = at..rows.len();
    if gap_shells {
        rows.push(Row::blank(w));
    }
    let at = rows.len();
    rows.extend(shells);
    frame.shells = at..rows.len();
    // Spare rows collect here (what spacing could not use); SPACES and
    // PARKED sit at the bottom.
    while rows.len() + spaces.len() + parked.len() < h {
        rows.push(Row::blank(w));
    }
    let at = rows.len();
    rows.extend(spaces);
    frame.spaces = at.min(rows.len())..rows.len();
    let at = rows.len();
    rows.extend(parked);
    rows.truncate(h);
    frame.parked = at.min(rows.len())..rows.len();
    frame.rows = rows;
    frame.step = step;
    frame.agents_step = agents_step;
    keep
}

/// Draw a frame into a ratatui buffer: each row's background (the
/// terminal's own, [`Color::Reset`], unless the row is a tile) across the
/// whole line, then its segments at the cells the layout counted (a segment
/// never spills into the next, whatever width ratatui gives a grapheme).
pub fn paint(frame: &Frame, buf: &mut Buffer) {
    let area = buf.area;
    for (y, row) in frame.rows.iter().enumerate().take(usize::from(area.height)) {
        let y = area.y + y as u16;
        let row_bg = row.bg.map_or(Color::Reset, Ink::color);
        buf.set_style(Rect::new(area.x, y, area.width, 1), Style::default().fg(palette::color("text")).bg(row_bg));
        put(buf, 0, y, &row.segs, row_bg);
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
