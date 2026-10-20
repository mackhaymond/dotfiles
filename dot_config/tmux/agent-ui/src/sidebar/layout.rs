//! The sidebar's pure half: a [`ViewModel`] and a size in, every row to draw
//! (each exactly `width` cells, with what a click on each cell does) out. No
//! I/O, so every density rule is unit-tested.
//!
//! ## Look: an app's panel, not text on a terminal
//!
//! OpenCode's and herdr's sidebars are the model. The sidebar paints its own
//! canvas, `mantle` ([`palette::CANVAS`], [`Ink::Canvas`]), one shade under
//! the terminals' `base`, so it reads as chrome beside them; no cell is ever
//! the terminal's default. On it sit raised surfaces, each leaving one
//! canvas column at each side of the sidebar ([`margin`], [`tile_row`]):
//!
//! - two PANELS in `tile_lo` behind half-block caps (`▄` above, `▀` below,
//!   [`pad_row`]): the usage panel at the top and the DOCK at the bottom;
//! - CARDS on one grid (c1 the state rail, c3-c5 the key-cap, c7 the
//!   title): agent cards are exactly two rows in ONE colour, stacked edge to
//!   edge, consecutive cards alternating the zebra's two shades ([`TONES`],
//!   [`shade`]: `tile`, `tile_lo`), afresh under each space's header; the
//!   client's own agent LIFTED ("you are here": `tile_me`, lavender 30%
//!   over the canvas, a full lavender cell down its right edge, its dim
//!   text `sub`; only the copy in the client's own space when a window is
//!   linked into two); NEEDS YOU cards their state's colour
//!   mixed over the canvas;
//! - SHELL rows: one row each, a TERMINAL LINE on a sunken strip (`term`,
//!   darker than the canvas where cards are raised), the same key-cap as
//!   an agent's, a green `❯` prompt; the client's own lifted;
//! - toasts: square `┃` bars on `surface0` (no corners).
//!
//! Hierarchy is tone and weight, never lines: section labels bold
//! `overlay2` ([`LABEL`]), titles `text` (bold when they have a state),
//! details `overlay2`, meta (ages, branches) dim. Blank canvas rows group
//! things: one above each section and each space's header, the first thing
//! to go when rows run short. Chips are square (the tmux status line's
//! catppuccin blocks): a [`module`] is ` key ` bold in dark ink on a colour,
//! then ` label ` on `surface0`.
//!
//! ## Shape: the usage panel, NEEDS YOU, the agents and shells (the body), the dock
//!
//! ```text
//!  ▄ prefix ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄   the usage panel; the key table's
//!   S ━━━━━━━━┃━━━━━━━━━━   21%   −4%    mark a chip on its top cap; one
//!   W ━━━━━━━━━━━━━┃━━━━━   60%  +17%    meter per window, in its pace's
//!  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀   colour (┃ = where the pace is)
//!
//!  NEEDS YOU                         1   (the count a square chip)
//!  ▌  g  1Password Sign-ins    ✓ done    a heavy rail in the state colour,
//!  ▌     Your four calls are done  4m    the key-cap solid
//!
//!  AGENTS                ⌥V all spaces   (the scope: a key hint)
//! ▌ 1  main   main            ◐2 ○3     the client's space: a BAND of its
//!  ▎  d  Tmux Agent Sidebar ⠹ working    colour, a ▌ at the edge; ` N `
//!  ▎     › Running cargo test     26s    solid, the name bold in it. A card:
//!  ▎  q  Pause Resume Agents  ✓ done     a rail in its state's colour, the
//!  ▎     Merged the pause key      2m    key-cap (quiet: the letter in its
//!     h  Canvas Syllabus                 SPACE's colour on a tint; solid for
//!        Synced 4 assignments      1h    the client's and asking / failed
//!     j  ❯ …/tmux/agent-ui               agents), the title, the state word
//!     k  ❯ nvim  agent-ui                right; idle: no rail, no word. The
//!                                        second line: the live blurb after
//!   2  cs35l   main              ○1     a `›`, else a message; the age.
//!  ▎  w  Eight Component Va… ✓ done      Then the space's SHELLS, one row
//!  ▎     I wrote tic-tac-toe.txt  9s     each a sunken strip: the key-cap,
//!                                        a green `❯`, the cwd, or the
//!                                        command and its folder.
//!                                        The header IS the space's home.
//!
//!  ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄   the dock: every space a row (⌃N
//!  ▌ 1  main               ◐2 ○3         order; the client's on its band),
//!    2  cs35l                 ○1         or chips; PARKED one line, its
//!   PARKED  4                            count, a message or the watcher
//!  ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀   on the right; never its tabs
//! ```
//!
//! ## Spaces, homes and shells
//!
//! A space's HOME window (`@space_home`) is never listed: the space's header
//! is the home. A click on it (or on the space in the dock) goes to the home
//! window ([`crate::actions::space_landing`]), and while the client is on
//! the home the header wears the client's own lift (`tile_me` and the
//! lavender edge) instead of its band. Every other window without an agent
//! state is a SHELL of its space: listed in its space's group after the
//! agents, one row each ([`shell_row`]), with its letter from the same pool
//! as the agents ([`crate::menu::letters`]); in the steps that fold the idle
//! agents, two or more shells fold into one line of pills
//! ([`shells_line`]). A space with only shells still gets its group; a
//! `@space_hidden` session lists no shells.
//!
//! Without a [`Bar`] (no client space, no usage, nothing selected) the top
//! is the classic ` agents` / ` space` title with the counts.
//!
//! ## The bottom: the dock
//!
//! Spaces are listed nowhere else, so the dock's gap and one line of chips
//! are held back before anything else is laid out ([`spaces_min`]), with
//! the PARKED line (the old last row). The first steps (with the gaps, and
//! the one right after) also leave the dock its rows ([`spaces_listed`],
//! [`Step::listed`]: every space a row and its caps); then the dock takes
//! the rows left over, the richest form that fits ([`bottom`]): every space
//! a row; else the client's space and those with an attention agent and
//! `+K more spaces`; else chips on as few as one line, each line kept over
//! the caps. Spare rows then buy spacing back (the section gaps, then the
//! gaps between spaces in AGENTS; never a gap between cards). PARKED is
//! always ONE line, its label and count (`none` dim with nothing parked),
//! at every height: the parked tabs themselves are never listed (prefix
//! h's picker, or the menu's Parked tab, shows them).
//!
//! ## Toasts: OpenCode's cards over the top, so nothing moves
//!
//! notify.sh's messages (`@toast`, [`Toast`]) float OVER the frame as cards
//! stacked from its top (the screen's top right), oldest on top, at most
//! [`TOASTS_SHOWN`], a blank canvas row between two and under the stack
//! ([`toast_stack`], [`toast_card`]):
//!
//! ```text
//!  ┃ ✓ Done                         10:02 ┃   info ● blue, ok ✓ green,
//!  ┃ parked Canvas Syllabus               ┃   warn ▲ peach, error ✕ red
//! ```
//!
//! Square `┃` bars in the kind's colour down a `surface0` fill, a canvas
//! margin each side; the glyph and title (notify.sh `-T`, else Info / Done
//! / Warning / Error) bold on the first row, the local time it was posted
//! right-aligned; the text wrapped into at most [`TOAST_LINES`] rows. The
//! rows beneath keep their place and what they stand for. A click anywhere
//! on a card dismisses that toast in this sidebar ([`Target::Toast`]); each
//! expires at its own deadline by the frame's clock, and
//! [`Frame::toast_until`] is the earliest one on screen. Short frames drop
//! the oldest card first, then body lines; the last row is never covered,
//! nor are the key-table marks and the selection hint (the cards start
//! under them). A sidebar too small for a card (under 16 columns or 3 free
//! rows) shows the newest toast as one compact row, down to 2 free rows: a
//! toast notify.sh posted is never lost to a size.
//!
//! ## Density ladder: agents lose detail last; never scroll, never drop an agent silently
//!
//! The agents section is the body. A frame takes the first step at which
//! every agent still gets its full detail ([`STEPS`]):
//!
//! 1. the dock gets the rows left over (above: "The bottom"); its minimum
//!    is held back before anything else is laid out;
//! 2. the blank rows go (the gaps between spaces in AGENTS first, then the
//!    section gaps: pure decoration goes before anything that says
//!    something);
//! 3. the dock's reserve goes (it may fall to chips);
//! 4. NEEDS YOU cards go from 2 lines to 1;
//! 5. only then do the agents compress (below), NEEDS YOU kept to the rows
//!    the agents' one-line shape leaves (at least 2, and at least a third of
//!    the body), cut with a `… N more need you` line.
//!
//! The agents' own ladder keeps every agent row the same height: the idle
//! agents of a space fold into one `○○○ 3 idle · Name, Name…` tile (and its
//! shells into one line); then every agent goes to one line (age beside its
//! state), unfolded, then folded again; then whole spaces collapse to their
//! header (with its counts, its shells gone with it), fewest attention
//! agents first; finally a `… N more` line.
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
//! Every agent card, shell row and NEEDS YOU card carries the window's
//! lifetime letter (`@agent_key`, one pool for agents and shells, assigned
//! by [`crate::menu::letters::of_view`], the menu's own function) as a
//! key-cap ([`Badge`]) in its SPACE's colour, so the window says where it
//! lives anywhere in the list: quiet by default, solid where you look next.
//! While ⌥W's jump table is armed every cap is inverted ([`ARMED`]: a peach
//! letter on `crust`), a look no calm cap has. Windows without one show the
//! cap blank; with no letter anywhere the column is not drawn.
//!
//! ## Scope
//!
//! [`crate::view::Scope`] (`@spaces` / `@strip_scope`): in SPACE scope the
//! agents section shows only the client's session, its agents and shells
//! (its header and ` no agents in this space` when it has neither); in ALL
//! scope every space with agents or shells. NEEDS YOU, the spaces and parked
//! stay global. The AGENTS header's scope word is a toggle once the scope is
//! a thing.
//!
//! ## Pinned
//!
//! A pinned agent (`@agent_pinned`, [`crate::pins`]) MOVES out of its
//! space's group into the PINNED block at the top of the agents section,
//! above the AGENTS header ([`pin_block`]): ` PINNED` like the other section
//! labels, the `⌥W 1-N` hint and the count on the right; its cards in PIN
//! order (first pinned first: pinning another never moves one already
//! there), each exactly as in its space (letter, state, rail, the client's
//! lift, clicks), the zebra afresh, its second line naming its space before
//! the age (`main · 4m`), and its ⌥W digit ([`pin_cap`]) under its letter (beside
//! it on a one-line card). Global in BOTH scopes (that is what a pin is
//! for). NEEDS YOU still lists a pinned agent that asks. On the ladder the
//! pins take the frame's card height but never fold, and the spaces
//! collapse before a pin loses its card; only a frame too short for every
//! pin on one line cuts them ([`pins_cut`]).
//!
//! ## Space order
//!
//! ONE order everywhere: `@space_index` (⌃N), spaces without one after, by
//! name ([`space_order`]). The agents section's groups and the dock's spaces
//! both sort by it.
//!
//! ## Colour
//!
//! Every tint is mixed over the canvas ([`palette::mix`], [`Ink::Mix`],
//! [`Ink::Tint`]), or over a card ([`over`]: a quiet key-cap). Surfaces
//! ([`palette::UI`]): `tile` / `tile_lo` (the zebra), `tile_me` (the
//! client's own agent, both rows, its own shell, and its space's header while
//! it is on the home: [`MINE_BG`]), the panels (`tile_lo`),
//! NEEDS YOU cards (their state colour, 14% / 9%), the client's space's band
//! ([`band`]: apart from every card shade), the stash selection (mauve 18%),
//! toasts (`surface0`), and the solid chips. Dim text is `overlay2`.

use super::pause::Phase;
use super::status::{ink_on, ink_rgb, tmux_colour, Bar, Kind, Toast, ToastId, Usage};
use crate::actions::watcher_problem;
use crate::model::{Cat, Counts, Flag, PAUSED_GLYPH, PAUSED_HUE};
use crate::palette;
use crate::text::{clip, dwidth, fit_label, plain, sanitize};
pub use crate::view::space_order;
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
    /// Any (r, g, b) at this percent over the canvas: a space's own colour
    /// as the tint of its header ([`tint`]).
    Tint(u8, u8, u8, u8),
    /// The canvas itself ([`palette::CANVAS`], the sidebar's own `mantle`),
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
            Ink::Tint(r, g, b, pct) => {
                let (r, g, b) = palette::over_canvas((r, g, b), pct);
                Color::Rgb(r, g, b)
            }
            Ink::Canvas => palette::color(palette::CANVAS),
        }
    }
}

/// `ink` at `pct`% over `under` (any two inks, as the terminal shows them):
/// a key-cap's tint over its card, the client's dock chip over the dock.
pub fn over(ink: Ink, pct: u8, under: Ink) -> Ink {
    let (a, b) = (ink_rgb(ink), ink_rgb(under));
    let p = f64::from(pct.min(100)) / 100.0;
    let m = |x: u8, y: u8| (f64::from(x) * p + f64::from(y) * (1.0 - p)).round() as u8;
    Ink::Rgb(m(a.0, b.0), m(a.1, b.1), m(a.2, b.2))
}

/// The client's space's BAND (its header in AGENTS, its row in the dock):
/// its colour over the canvas, from [`BAND_MIN`]% up in steps of 2 until
/// the band sits plainly apart (at least [`BAND_APART`] in RGB distance)
/// from the canvas and every card shade that can sit next to it (`tile`,
/// `tile_lo`, `tile_hi`), and [`BAND_APART_MINE`] from the client's own
/// card (`tile_me`, lighter and edged, often right under it); at most
/// [`BAND_MAX`]%.
pub fn band(ink: Ink) -> Ink {
    let (r, g, b) = ink_rgb(ink);
    let apart = |pct: u8| {
        let t = palette::over_canvas((r, g, b), pct);
        let d = |c: (u8, u8, u8)| {
            let d = |x: u8, y: u8| (f64::from(x) - f64::from(y)).powi(2);
            (d(t.0, c.0) + d(t.1, c.1) + d(t.2, c.2)).sqrt()
        };
        [palette::CANVAS, "tile", "tile_lo", "tile_hi"].iter().all(|n| d(palette::rgb(n)) >= BAND_APART)
            && d(palette::rgb(MINE_BG)) >= BAND_APART_MINE
    };
    let pct = (BAND_MIN..=BAND_MAX).step_by(2).find(|&p| apart(p)).unwrap_or(BAND_MAX);
    Ink::Tint(r, g, b, pct)
}

/// [`band`]'s range and how far apart it must sit.
pub const BAND_MIN: u8 = 16;
pub const BAND_MAX: u8 = 34;
const BAND_APART: f64 = 20.0;
/// How far a band keeps from the client's own card (lighter than any band
/// and marked by its lavender edge, so a smaller margin suffices).
const BAND_APART_MINE: f64 = 12.0;

/// `ink` at `pct`% over the canvas: a palette colour as [`Ink::Mix`], any
/// other (`@space_color`'s `#rrggbb`, `colourN`) as [`Ink::Tint`].
pub fn tint(ink: Ink, pct: u8) -> Ink {
    match ink {
        Ink::Pal(p) => Ink::Mix(p, pct),
        Ink::Canvas => Ink::Canvas,
        other => {
            let (r, g, b) = ink_rgb(other);
            Ink::Tint(r, g, b, pct)
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

/// The half-block above the usage panel (`▄`, its top padding: half a row
/// of canvas, then half a row of the tile) or below it (`▀`): the tile's
/// colour on the canvas, between the margins. Not a target, stands for no
/// one. List tiles (agents, NEEDS YOU cards) have no pads: they
/// stack edge to edge, parted by the zebra ([`TONES`]).
const PAD_TOP: char = '▄';
const PAD_BOTTOM: char = '▀';

fn pad_row(w: usize, bg: Ink, glyph: char) -> Row {
    pad_with(w, bg, glyph, Vec::new())
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


/// The zebra's two shades: list tiles stacked edge to edge (agent cards,
/// one row or two, the idle fold) alternate them, so every card is ONE colour
/// and its neighbours the other.
const TONES: (Hue, Hue) = ("tile", "tile_lo");

/// The two panels' colour (the usage panel at the top, the dock at the
/// bottom), behind half-block caps.
const PANEL: Hue = "tile_lo";

/// The `k`th tile of a run's shade ([`TONES`]); a run starts afresh under
/// each space's header.
fn shade(k: usize) -> Ink {
    Ink::Pal(if k.is_multiple_of(2) { TONES.0 } else { TONES.1 })
}

/// What a click on a cell does.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Target {
    /// An agent row or a needs card: agent-jump.sh goto (`Actions::go`,
    /// Strip.click "goto").
    Go { win: String, session: String },
    /// A dock space row or chip, or a space header: that space's HOME
    /// window, else the session's active one (Strip.click "session",
    /// [`crate::actions::space_landing`]).
    Session(String),
    /// A shell row (or its pill in a folded line): that window, like an
    /// agent row, but never parked by a right-click.
    Shell { win: String, session: String },
    /// agent-jump.sh next / back (no button draws them any more; kept for
    /// the click handler).
    Next,
    Back,
    /// The agents menu (Strip.open_menu), optionally on a tab ("all", "idle").
    Menu(Option<&'static str>),
    /// PARKED (its one line): the stash picker, exactly prefix h's
    /// `stash.sh unstash '' <client>`.
    Stash,
    /// The red watcher warning: restart it (the strip's ⟳, Strip.click "restart").
    Restart,
    /// The scope word (or the classic title): flip the scope (space / all) and
    /// persist it in `@strip_scope` (Strip.toggle_scope).
    Scope,
    /// A toast ([`Toast::id`]): dismissed, in this sidebar only.
    Toast(ToastId),
    /// The pause banner once the pause is settled (safe to close, or ready
    /// after a wake): `agent-pause.py resume <client>`, never `toggle`.
    Resume,
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
    /// The row is part of the PINNED block (its header, its cards, its
    /// `… more` line), at the top of the agents section.
    pub pinned: bool,
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
    /// The pause banner's rows ([`pause_rows`]): the very top. Empty: none.
    pub pause: Range<usize>,
    /// The rows of each section, header included (empty: not drawn).
    pub needs: Range<usize>,
    /// The agents section: the PINNED block (when anything is pinned), then
    /// every shown space's header (its home), its agents, then its shells.
    pub agents: Range<usize>,
    /// The PINNED block's rows (its header and cards), inside `agents`;
    /// empty when nothing is pinned.
    pub pinned: Range<usize>,
    /// The dock, through the last row: its gap (when room), its `▄` cap,
    /// the spaces, PARKED, its `▀` cap.
    pub dock: Range<usize>,
    /// The dock's spaces: one row per space (or the client's and the
    /// attention ones and `+K more`), or, short of rows, chips.
    pub spaces: Range<usize>,
    /// The dock's PARKED line (the stash's count, the message or watcher
    /// problem on the right): the last row, or the one above the dock's
    /// bottom cap. Never the parked tabs themselves.
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
    /// Where the pause key's pause stands ([`super::pause`]): a banner at
    /// the very top. None: nothing at all.
    pub pause: Option<Phase>,
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
        Cat::Paused => PAUSED_HUE,
        Cat::Idle => "overlay",
    }
}

/// `✕1 ◉2 ◐4 ⏸3`: nonzero only, each in its state colour.
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

/// A line of dim words on the canvas (`… 3 more`, `no agents running`).
fn dim_line(w: usize, text: &str) -> Row {
    Row::new(fit(w, vec![seg(text, LABEL)], vec![], None))
}

/// Dim text: `overlay2` everywhere text says something (on the mantle
/// canvas about 6:1, on any surface 4:1 or better); `overlay` only for the
/// faintest marks on the canvas, never on a surface, where it would not
/// read.
fn dim(on_surface: bool) -> Hue {
    if on_surface {
        "overlay2"
    } else {
        "overlay"
    }
}

/// The section labels' ink (NEEDS YOU, AGENTS, PARKED: bold,
/// uppercase) and the canvas's dim text.
const LABEL: Hue = "overlay2";

/// A section header: one blank row's worth of canvas around it is the
/// caller's (the gaps); ` LABEL` bold in [`LABEL`] at the first content
/// column, `right` ending at the last.
fn section_header(w: usize, label: &str, right: Vec<Seg>) -> Row {
    let mut right = right;
    if !right.is_empty() {
        right.push(seg(" ", "text"));
    }
    Row::new(fit(w, vec![bold(format!(" {label}"), LABEL)], right, None))
}

/// An agent's age at the frame's clock (`2m`), so a frame laid out between
/// snapshots (the spinner's) shows the true age.
fn age_of(a: &Agent, cx: Cx) -> String {
    crate::text::ago(a.since, cx.now)
}

/// Everything the row builders share: the width, the letter column, the
/// clock and the spinner frame.
#[derive(Debug, Clone, Copy)]
struct Cx<'a> {
    w: usize,
    b: Badge,
    now: f64,
    spin: usize,
    /// The space whose copy of the client's window is its own card (a
    /// window linked into two spaces is current in both: [`own`]); None:
    /// any copy.
    home: Option<&'a str>,
    /// The client's window: on a space's HOME, that space's header is "you".
    cur: Option<&'a str>,
    /// Shells are listed (`@spaces` on: [`ViewModel::shells`]).
    shells: bool,
    /// A pause is saved and settled or settling (the banner's stopping, safe
    /// or ready: `paused.json` lists targets), so prefix P resumes: a paused
    /// card says so.
    resumes: bool,
}

impl Cx<'_> {
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

/// The key-table marks, solid chips: a red ` prefix ` while the client's key
/// table is `prefix`; a peach ` ⌥W jump ` and `type a letter` while it is
/// `jump`. They sit as a tab on the usage panel's top pad, so arming a
/// table never changes the panel's height; a frame too short for the panel
/// gives them a row of their own. No space chip: the dock at the bottom
/// already marks the client's space.
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

/// A signed delta as the meter prints it: `+17%`, `−4%` (a real minus),
/// `±0%`. A pooled pace reads the same.
fn delta_text(u: &Usage) -> String {
    let Some(d) = u.delta else { return String::new() };
    let sign = match d.signum() {
        1 => "+",
        -1 => "−",
        _ => "±",
    };
    format!("{sign}{}%", d.unsigned_abs())
}

/// How far over its pace a window may run before the meter turns red
/// (percentage points); up to it, peach.
pub const PACE_SLACK: i32 = 15;

/// A usage window's colour by its pace: `green` on or under it, `peach`
/// up to [`PACE_SLACK`] points over, `red` beyond. Without a pace, the
/// status line's own colour (`@codex_<w>_color`), else dim.
fn pace_ink(u: &Usage) -> Ink {
    match u.delta {
        Some(d) if d <= 0 => Ink::Pal("green"),
        Some(d) if d <= PACE_SLACK => Ink::Pal("peach"),
        Some(_) => Ink::Pal("red"),
        None => u.color.unwrap_or(Ink::Pal("overlay2")),
    }
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

/// One usage window as a meter: its label bold in its pace's colour
/// ([`pace_ink`]), a progress bar (Textual's: `━` filled in that colour over
/// a `surface1` track) with a tick (`┃`, `text`) where the pace expects it,
/// the percent (bold), the signed delta in the pace's colour (so "fine" or
/// "over" reads before the number) and, when it fits, when the window
/// resets (`↻3h`, dim). The reset goes first, then the delta, when the bar
/// would be too short. Dim is `overlay2`: the meter sits on the usage panel.
fn meter_row(u: &Usage, w: usize, now: f64) -> Row {
    let hue = pace_ink(u);
    let label = clip(&sanitize(&u.label), 6);
    let pct = if u.idle {
        "idle".to_string()
    } else {
        u.used.map_or("--".to_string(), |v| format!("{v:.0}%"))
    };
    let delta = delta_text(u);
    let resets = u.resets.and_then(|r| until(r, now)).map(|r| format!("↻{r}"));
    let lw = dwidth(&label) + 2;
    let build = |with_delta: bool, with_reset: bool| -> Vec<Seg> {
        let mut r = vec![bold(format!(" {pct:>4}"), "text")];
        if with_delta && !delta.is_empty() {
            r.push(seg(format!(" {delta:>5}"), hue));
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
        v.push(bold(format!("{} ", clip(&sanitize(&u.label), 6)), pace_ink(u)));
        let pct = if u.idle { "idle".into() } else { u.used.map_or("--".to_string(), |x| format!("{x:.0}%")) };
        v.push(seg(pct, "text"));
    }
    Some(Row::new(fit(w, v, vec![], None)))
}

/// The red stale-save chip of status-right (`@resurrect_stale`).
fn stale_row(bar: &Bar, w: usize) -> Option<Row> {
    let s = bar.stale.as_ref()?;
    Some(Row::new(fit(w, vec![seg(" ", "text"), block(" ! ", "crust", "red"), seg(format!(" {s}"), "red")], vec![], None)))
}

// ---------------------------------------------------------------- toasts

/// A toast's glyph, default title and colour: info `●` Info blue, ok `✓`
/// Done green, warn `▲` Warning peach, error `✕` Error red.
fn toast_look(k: Kind) -> (&'static str, &'static str, Hue) {
    match k {
        Kind::Info => ("●", "Info", "blue"),
        Kind::Ok => ("✓", "Done", "green"),
        Kind::Warn => ("▲", "Warning", "peach"),
        Kind::Error => ("✕", "Error", "red"),
    }
}

/// A toast card's fill: a raised surface of its own (no tint: the bars
/// carry the kind's colour).
const TOAST_BG: Hue = "surface0";

/// The bar down each side of a toast (OpenCode's `SplitBorder`).
const TOAST_BAR: &str = "┃";

/// At most this many toast cards at once (the newest), and body lines each.
const TOASTS_SHOWN: usize = 3;
const TOAST_LINES: usize = 3;

/// When a toast was posted, as its card says it (`10:02`, local time:
/// [`crate::text::clock_hm`], libc `localtime_r`); "" if the C library
/// says no.
fn posted_at(t: &Toast) -> String {
    crate::text::clock_hm(t.at_ms.div_euclid(1000))
}

/// One row of a toast card: `┃` in the kind's colour down each side of a
/// `surface0` fill (square, no corners), a cell of padding inside each bar.
fn toast_row(w: usize, hue: Hue, left: Vec<Seg>, right: Vec<Seg>) -> Row {
    let mut l = vec![seg(TOAST_BAR, hue), seg(" ", "text")];
    l.extend(left);
    let mut r = right;
    r.extend([seg(" ", "text"), seg(TOAST_BAR, hue)]);
    tile_row(w, Ink::Pal(TOAST_BG), l, r)
}

/// One toast as a card (OpenCode's): square `┃` bars in the kind's colour
/// on `surface0`, a canvas margin each side; the glyph and title (the `-T`
/// one, else the kind's) bold in its colour on the first row, the local
/// time it was posted right-aligned (dim; dropped when narrow); the text
/// word-wrapped under it into at most `lines` rows, `…` on the last when
/// more is left. Every row of it dismisses it here.
///
/// ```text
///  ┃ ✓ Done                         10:42 ┃
///  ┃ parked Canvas Syllabus               ┃
/// ```
fn toast_card(t: &Toast, w: usize, lines: usize) -> Vec<Row> {
    let (glyph, default, hue) = toast_look(t.kind);
    let iw = inner(w);
    let room = iw.saturating_sub(4);
    let title = bold(format!("{glyph} {}", t.title.as_deref().unwrap_or(default)), hue);
    let at = posted_at(t);
    let right = if !at.is_empty() && dwidth(&title.text) + 1 + dwidth(&at) <= room { vec![seg(at, "overlay2")] } else { vec![] };
    let id = Target::Toast(t.id());
    let mut rows = vec![toast_row(w, hue, vec![title], right)];
    for l in wrap(&t.text, room, lines) {
        rows.push(toast_row(w, hue, vec![seg(l, "text")], vec![]));
    }
    rows.into_iter().map(|r| r.click(w, id.clone())).collect()
}

/// A card needs this many columns; narrower, a toast is a compact row.
const TOAST_CARD_W: usize = 16;

/// One toast in a single row, for a sidebar too small for a card: the
/// card's bars and fill, the glyph bold in the kind's colour and the text
/// (clipped). The row dismisses it.
fn toast_compact(t: &Toast, w: usize) -> Row {
    let (glyph, _, hue) = toast_look(t.kind);
    let left = vec![bold(format!("{glyph} "), hue), seg(t.text.clone(), "text")];
    let row = if inner(w) >= 6 { toast_row(w, hue, left, vec![]) } else { tile_row(w, Ink::Pal(TOAST_BG), left, vec![]) };
    row.click(w, Target::Toast(t.id()))
}

/// The live toasts (at `now`) as stacked cards from the top of `h` rows of
/// a frame `w` wide, oldest on top, the newest [`TOASTS_SHOWN`] at most, a
/// blank canvas row between two cards and (room permitting) one under the
/// stack, in at most `h - 1` rows (the frame's last row stays): the oldest
/// go first, then body lines, when they do not fit. Too small for even one
/// card (under [`TOAST_CARD_W`] columns or 3 rows): the newest as a
/// compact row ([`toast_compact`]), down to 2 rows. → (rows, the earliest
/// deadline among them). The blank rows are no target: they belong to no
/// toast.
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
                let mut rows: Vec<Row> = Vec::new();
                for (k, t) in live.iter().enumerate() {
                    if k > 0 {
                        rows.push(Row::blank(w));
                    }
                    rows.extend(toast_card(t, w, lines));
                }
                if rows.len() <= room {
                    if rows.len() < room {
                        rows.push(Row::blank(w));
                    }
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
    let bg = Ink::Pal(PANEL);
    top.rows.push(pad_with(w, bg, PAD_TOP, marks));
    top.rows.extend(content.into_iter().map(|r| into_tile(r, w, bg)));
    top.rows.push(pad_row(w, bg, PAD_BOTTOM));
    let n = top.rows.len();
    top.status = 1..n - 1;
    top.card = 0..n;
    top
}

// ---------------------------------------------------------------- the pause banner

/// How strongly a SETTLED pause banner (safe, ready) takes its cyan (one
/// tint for the whole banner, NEEDS YOU's first).
pub const PAUSE_TINT: u8 = 14;

/// The settled banner's accent (safe to close, ready to resume): electric
/// cyan, as CuaNotch glows once every paused agent has stopped. The paused
/// CARDS stay violet ([`PAUSED_HUE`]); only the "all stopped" signal is
/// cyan. (White before it; Mack: "solid white is no good".)
pub const SETTLED_HUE: &str = "cyan";

/// How strongly a MOVING pause banner (pausing, resuming) takes it: the
/// NEEDS YOU zebra's quieter tint, a step under [`PAUSE_TINT`].
pub const PAUSE_TINT_MOVING: u8 = 9;

/// A phase's colour, matching CuaNotch (Mack's ask): in progress (sending,
/// stopping, resuming: the spinner) the quieter `dimviolet`, as the notch
/// pulses violet while pausing; settled (safe to close, ready to resume)
/// [`SETTLED_HUE`] cyan, as the notch glows once every agent has stopped.
/// Never green: green means done. The two settled phases part by emphasis
/// ([`pause_rows`]): safe says `⏸ PAUSED` bold in cyan, ready puts its
/// count on a SOLID cyan chip, the one that asks for a press.
pub fn pause_hue(p: &Phase) -> Hue {
    if p.busy() { "dimviolet" } else { SETTLED_HUE }
}

/// A phase's banner background over the canvas: the paused violet at
/// [`PAUSE_TINT_MOVING`] while something moves, cyan at [`PAUSE_TINT`]
/// once settled.
pub fn pause_bg(p: &Phase) -> Ink {
    if p.busy() { Ink::Mix(PAUSED_HUE, PAUSE_TINT_MOVING) } else { Ink::Mix(SETTLED_HUE, PAUSE_TINT) }
}

/// How many rows the banner gets in a frame `h` tall: two (the second the
/// dim detail) from 16 rows, the usage panel's own threshold; one from 4;
/// none below. It is cut only after everything but the frame's last row:
/// the sections under it take the squeeze (the agents compress, NEEDS YOU
/// folds), never the banner.
pub fn pause_lines(h: usize) -> usize {
    match h {
        16.. => 2,
        4.. => 1,
        _ => 0,
    }
}

/// `n agent(s)`.
fn agents_n(n: usize) -> String {
    format!("{n} agent{}", if n == 1 { "" } else { "s" })
}

/// The pause banner (docs/agent-pause.md "In the sidebar"): ONE tile in
/// its phase's colour mixed over the canvas ([`pause_bg`], like a NEEDS YOU
/// card), a heavy `▌` rail in its phase's colour ([`pause_hue`]) down its
/// rows; the glyph (the
/// spinner while something moves) and the word bold in the colour, what it
/// means in `text` after it. The second row, when there is room and
/// something to say, is the detail under the word: who is still working
/// (`sub`, after a dim `still working`), else the agents' labels (dim),
/// clipped.
///
/// ```text
///  ▌ ⠹ PAUSING  sending 2 of 5          dim violet on 9% (a press in flight)
///  ▌ ⠹ PAUSING  3 of 5 stopped          dim violet on 9%
///  ▌   still working  Tmux Sidebar, …
///  ▌ ⏸ PAUSED  safe to close            cyan on 14%, bold word; a click resumes
///  ▌ [↻ 5 ready] Tmux Sidebar, Island…  cyan on 14%: a SOLID chip; a click resumes
///  ▌   prefix P or click to resume      (one row: ↻ 5 ready  prefix P · click)
///  ▌ ⠹ RESUMING  5 agents…              dim violet on 9%
/// ```
///
/// Safe and ready rows click through to [`Target::Resume`]; the others do
/// nothing (a press is in flight, or agents are still stopping). Ready
/// always says how to resume (Mack asked for the prefix P hint), so at the
/// usual 34 columns the hint takes the second row and the labels the rest
/// of the first.
fn pause_rows(p: &Phase, cx: Cx, lines: usize) -> Vec<Row> {
    let w = cx.w;
    if lines == 0 || w == 0 {
        return Vec::new();
    }
    let hue = pause_hue(p);
    let bg = pause_bg(p);
    let spin = SPIN[cx.spin % SPIN.len()];
    let dimc = dim(true);
    let names = |l: &[String]| seg(l.join(", "), dimc);
    let rail = || seg(RAIL_HEAVY, hue);
    if let Phase::Ready { labels } = p {
        // The chip: the count in dark ink on solid cyan, "press me".
        let mut l1 = vec![rail(), seg(" ", "text"), block(format!(" ↻ {} ready ", labels.len()), "crust", hue), seg(" ", "text")];
        let rows = if lines >= 2 {
            l1.push(names(labels));
            let l2 = vec![rail(), seg("   ", "text"), bold("prefix P", "text"), seg(" or click to resume", dimc)];
            vec![tile_row(w, bg, l1, vec![seg(" ", "text")]), tile_row(w, bg, l2, vec![seg(" ", "text")])]
        } else {
            l1.extend([bold("prefix P", "text"), seg(" · click", dimc)]);
            vec![tile_row(w, bg, l1, vec![seg(" ", "text")])]
        };
        return rows.into_iter().map(|r| r.click(w, Target::Resume)).collect();
    }
    let (word, rest, detail): (String, String, Vec<Seg>) = match p {
        Phase::Sending { done, n, labels } => (format!("{spin} PAUSING"), format!("sending {done} of {n}"), vec![names(labels)]),
        Phase::Resuming { n, labels } => (format!("{spin} RESUMING"), format!("{}…", agents_n(*n)), vec![names(labels)]),
        Phase::Stopping { stopped, n, working } => (
            format!("{spin} PAUSING"),
            format!("{stopped} of {n} stopped"),
            vec![seg("still working  ", dimc), seg(working.join(", "), "sub")],
        ),
        Phase::Safe { labels } => (format!("{PAUSED_GLYPH} PAUSED"), "safe to close".into(), vec![names(labels)]),
        Phase::Ready { .. } => unreachable!("handled above"),
    };
    let left = vec![rail(), seg(" ", "text"), bold(word, hue), seg(format!("  {rest}"), "text")];
    let mut rows = vec![tile_row(w, bg, left, vec![])];
    let says = detail.iter().any(|s| !s.text.trim().is_empty());
    if lines >= 2 && says {
        let mut l2 = vec![rail(), seg("   ", "text")];
        l2.extend(detail);
        rows.push(tile_row(w, bg, l2, vec![seg(" ", "text")]));
    }
    let busy = p.busy();
    rows.into_iter()
        .map(|r| {
            let r = Row { anim: busy, ..r };
            if p.clickable() {
                r.click(w, Target::Resume)
            } else {
                r
            }
        })
        .collect()
}

// ---------------------------------------------------------------- shells

/// A shell row's prompt (green, bold): what says "a terminal", not an
/// agent, at a glance.
pub const PROMPT: &str = "❯ ";

/// A space's SHELLS as its group lists them: [`Space::shells`] (no agent
/// state, never the home: the space's header IS the home), minus a window
/// the group already shows as a card (the client's own window in the
/// classic scope, `@spaces` off). `on`: shells are a thing at all
/// ([`ViewModel::shells`], `@spaces` on); off, none (the classic sidebar).
fn shells_of(sp: &Space, on: bool) -> Vec<&Agent> {
    if !on {
        return Vec::new();
    }
    sp.shells().into_iter().filter(|s| !sp.agents.iter().any(|a| a.window_id == s.window_id)).collect()
}

/// A shell row's sunken strip ([`palette::UI`]'s `term`, between the canvas
/// and `crust`).
const TERM_BG: Hue = "term";

/// A shell as its prompt line says it: (the running command, None at a
/// shell prompt; its cwd, `~` for $HOME).
fn shell_parts(s: &Agent) -> (Option<String>, String) {
    let name = s.name.trim().trim_start_matches('-');
    let prompt = name.is_empty() || matches!(name, "zsh" | "bash" | "sh" | "fish");
    let dir = sanitize(&crate::view::tilde(&s.path));
    let dir = if dir.is_empty() { sanitize(&s.shell) } else { dir };
    ((!prompt).then(|| sanitize(&s.shell)), dir)
}

/// A path in at most `room` cells, cut from the LEFT at a folder boundary
/// (`…/tmux/agent-ui`, as a prompt truncates it): a path's end is the part
/// that says where. A last folder too long on its own is cut mid-name.
fn clip_left(text: &str, room: usize) -> String {
    if dwidth(text) <= room {
        return text.to_string();
    }
    let parts: Vec<&str> = text.split('/').collect();
    for k in (1..parts.len()).rev() {
        let t = format!("…/{}", parts[parts.len() - k..].join("/"));
        if dwidth(&t) <= room {
            return t;
        }
    }
    if room == 0 {
        return String::new();
    }
    let mut tail: Vec<char> = Vec::new();
    let mut used = 1;
    for c in text.chars().rev() {
        let cw = dwidth(&c.to_string());
        if used + cw > room {
            break;
        }
        used += cw;
        tail.push(c);
    }
    format!("…{}", tail.into_iter().rev().collect::<String>())
}

/// One of a space's shells as ONE row in its group, after its agents, drawn
/// as a TERMINAL LINE: a sunken strip ([`TERM_BG`], darker than the canvas,
/// where the agent cards are raised tiles; one colour for the row), on the
/// card grid (c1 the rail, c3-c5 the key-cap, c7 the prompt). The key-cap
/// is EXACTLY an agent's (a quiet cap drawn as over the zebra's first
/// shade, `tile`; solid for the client's own; inverted while ⌥W is armed),
/// so its letter is as easy to spot. Then a green [`PROMPT`] and what the
/// prompt line would say: at a shell prompt its cwd (`~` for $HOME, cut
/// from the left at a folder: `…/tmux/agent-ui`) in `sub`; running a
/// command, the command bold `text` and the folder dim after it. The
/// client's own shell is lifted like its own agent card (`tile_me`, a
/// lavender rail, a full lavender cell down its right edge, the path
/// `text`); prefix v's selection tints it mauve with the heavy mauve rail.
/// A click goes to the shell.
fn shell_row(s: &Agent, cx: Cx, space: Ink) -> Row {
    let w = cx.w;
    let mine = own(s, cx);
    let bg = if s.selected {
        Ink::Mix("mauve", 18)
    } else if mine {
        Ink::Pal(MINE_BG)
    } else {
        Ink::Pal(TERM_BG)
    };
    let rail = if s.selected {
        seg(RAIL_HEAVY, "mauve")
    } else if mine {
        seg(RAIL, "lavender")
    } else {
        seg(" ", "text")
    };
    let mut left = vec![rail, seg(" ", "text")];
    if cx.b.on {
        left.extend([cx.b.seg(s, space, Ink::Pal(TONES.0), mine), seg(" ", "text")]);
    }
    left.push(bold(PROMPT, "green"));
    let mut right = vec![seg(" ", "text")];
    if mine && cx.seconds() {
        right.push(seg(MINE, MINE_HUE));
    }
    let dimc = if mine { MINE_DIM } else { dim(true) };
    let room = inner(w).saturating_sub(width(&left) + width(&right));
    let (cmd, dir) = shell_parts(s);
    match cmd {
        Some(c) => {
            let c = clip(&c, room);
            let rest = room.saturating_sub(dwidth(&c) + 2);
            let folder = dir.rsplit('/').next().unwrap_or(&dir).to_string();
            left.push(bold(c, "text"));
            if rest >= 4 {
                left.push(seg(format!("  {}", clip(&folder, rest)), dimc));
            }
        }
        None => left.push(seg(clip_left(&dir, room), if mine { "text" } else { "sub" })),
    }
    tile_row(w, bg, left, right).click(w, Target::Shell { win: s.window_id.clone(), session: s.session.clone() })
}

/// A space's shells in ONE row, for the steps that fold (the idle agents
/// fold into one line then too): the shell rows' strip, from the card
/// grid's cap column each shell its key-cap (as on its row) and its label
/// (the command it runs, else its folder), two cells apart, `+K` (dim) for
/// those that do not fit; without letters, the green prompt before each
/// label. Each shell's cells click through to it.
fn shells_line(shells: &[&Agent], cx: Cx, space: Ink) -> Row {
    let w = cx.w;
    let m = margin(w);
    let end = inner(w).saturating_sub(1);
    let mut segs: Vec<Seg> = vec![seg("  ", "text")];
    let mut targets = Vec::new();
    let mut x = 2;
    for (k, s) in shells.iter().enumerate() {
        let mut pill = Vec::new();
        if cx.b.on {
            pill.extend([cx.b.seg(s, space, Ink::Pal(TONES.0), own(s, cx)), seg(" ", "text")]);
        } else {
            pill.push(bold(PROMPT, "green"));
        }
        pill.push(seg(clip(&sanitize(&s.shell), 14), if own(s, cx) { "text" } else { "sub" }));
        let gap = usize::from(k > 0) * 2;
        let left = shells.len() - k - 1;
        let more = if left > 0 { dwidth(&format!("  +{left}")) } else { 0 };
        if x + gap + width(&pill) + more > end {
            let rest = format!("{}+{}", if k > 0 { "  " } else { "" }, shells.len() - k);
            segs.push(seg(rest, dim(true)));
            break;
        }
        if gap > 0 {
            segs.push(seg("  ", "text"));
        }
        let x0 = m + x + gap;
        x += gap + width(&pill);
        segs.extend(pill);
        targets.push((x0 as u16, (m + x) as u16, Target::Shell { win: s.window_id.clone(), session: s.session.clone() }));
    }
    Row { targets, ..tile_row(w, Ink::Pal(TERM_BG), segs, vec![]) }
}

// ---------------------------------------------------------------- agents

/// The agents' LETTER column: each agent row and NEEDS YOU card shows its
/// lifetime letter ([`crate::menu::letters`], the same `@agent_key` the
/// agents menu and UB's ⌥W table go by) as a key-cap ([`Badge::seg`]).
/// Agents without a letter (past 26, the client's plain window) leave it
/// blank so titles stay aligned; with no letter anywhere the column is not
/// drawn.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Badge {
    on: bool,
    loud: bool,
}

/// A key-cap's cells (` a `).
const BADGE_W: usize = 3;

/// How strongly a quiet key-cap takes its space's colour over its card.
pub const CAP_TINT: u8 = 22;

/// The state rail down a card's left edge (U+258E, a quarter block), and
/// the heavier one of a NEEDS YOU card and the stash selection (U+258C).
const RAIL: &str = "▎";
const RAIL_HEAVY: &str = "▌";

/// The client's own card ("you are here", at a glance): its surface,
/// lavender at 30% over the canvas (`tile_me`, well off both zebra shades,
/// the stash selection, the NEEDS YOU tints; every space's band kept apart
/// from it), its dim text (`sub`: `overlay2` is about 3:1 there), and a
/// full lavender cell down its right edge, both rows ([`MINE`]).
pub const MINE_BG: Hue = "tile_me";
const MINE_DIM: Hue = "sub";
const MINE: &str = "█";
const MINE_HUE: Hue = "lavender";

/// What the jump table (⌥W) turns every key-cap into: INVERTED, a bold
/// peach letter (its ` ⌥W jump ` mark's colour) on `crust`, darker than
/// any surface. No calm cap can look like it: a quiet cap's tint sits over
/// its card (never as dark as crust) and a solid one is its space's colour
/// with dark ink (never a peach letter), whatever the space's colour, the
/// colourless default peach included.
const ARMED: (Hue, Hue) = ("peach", "crust");

impl Badge {
    #[cfg(test)]
    const OFF: Badge = Badge { on: false, loud: false };

    fn of(view: &ViewModel, bar: &Bar) -> Badge {
        let shells = view.spaces.iter().flat_map(|s| s.plain.iter());
        let on = view.agents().chain(&view.pinned).chain(shells).chain(view.needs.iter().map(|n| &n.agent)).any(|a| a.letter.is_some());
        Badge { on, loud: bar.jump }
    }

    /// The cells before a title: the rail, a pad, the key-cap and a pad; or
    /// the rail and a pad.
    fn lead(self) -> usize {
        if self.on {
            BADGE_W + 3
        } else {
            2
        }
    }

    /// `a`'s key-cap on a card of colour `card`. Quiet: the letter bold in
    /// its SPACE's colour (`space`) on that colour at [`CAP_TINT`]% over the
    /// card, so an agent says where it lives without shouting. `filled`
    /// (the client's own agent, one asking or failed, every NEEDS YOU card):
    /// a solid chip in the space's colour (dark ink), "look here". While ⌥W
    /// is armed every cap with a letter is solid peach ([`ARMED`]), unlike
    /// any quiet cap. Without a letter, the cap blank.
    fn seg(self, a: &Agent, space: Ink, card: Ink, filled: bool) -> Seg {
        let tint = over(space, CAP_TINT, card);
        // A dark @space_color would not read as a letter on its own tint.
        let letter_ink = if ink_on(space) == Ink::Pal("crust") { space } else { Ink::Pal("text") };
        match a.letter {
            Some(c) if self.loud => block(format!(" {c} "), ARMED.0, ARMED.1),
            Some(c) if filled => block(format!(" {c} "), ink_on(space), space),
            Some(c) => block(format!(" {c} "), letter_ink, tint),
            // (Armed: no letter to type, but the column stays one look.)
            None if self.loud => on(" · ", "overlay2", ARMED.1),
            None => on(" ".repeat(BADGE_W), "text", if filled { space } else { tint }),
        }
    }
}

/// An agent's state as the row's right side says it.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Word {
    /// The spinner frame, ✕ ◉ ✓; "" when idle.
    glyph: &'static str,
    /// "" when idle: the default state says nothing.
    word: &'static str,
    hue: Hue,
}

impl Word {
    fn working(self: &Word) -> bool {
        self.word == "working"
    }

    /// Idle: no rail, no word, a regular title.
    fn idle(&self) -> bool {
        self.word.is_empty()
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
/// finished turn), `⏸ paused` (violet: never done-green, whatever its last
/// turn said); idle says nothing ([`Word::idle`]). None for a plain shell.
fn state_word(a: &Agent, cx: Cx) -> Option<Word> {
    if a.in_flight || a.cat == Some(Cat::Working) {
        return Some(Word { glyph: SPIN[cx.spin % SPIN.len()], word: "working", hue: "blue" });
    }
    Some(match a.cat? {
        Cat::Idle if !recent_done(a, cx.now) => Word { glyph: "", word: "", hue: "overlay2" },
        c => cat_word(a, c),
    })
}

/// A state's glyph and word by its category alone (a NEEDS YOU card's: an
/// asking agent with a workflow flag is in flight, but it still asks).
fn cat_word(a: &Agent, c: Cat) -> Word {
    match c {
        Cat::Failed => Word { glyph: "✕", word: "error", hue: "red" },
        Cat::NeedsInput => Word { glyph: "◉", word: if a.detail_kind == "perm" { "perm" } else { "asks" }, hue: "yellow" },
        Cat::Done | Cat::Idle => Word { glyph: "✓", word: "done", hue: "green" },
        Cat::Working => Word { glyph: "◐", word: "working", hue: "blue" },
        Cat::Paused => Word { glyph: PAUSED_GLYPH, word: "paused", hue: PAUSED_HUE },
    }
}

/// A paused card's second line (docs/agent-pause.md "The paused state"):
/// `paused` bold violet, then, while a pause is saved ([`Cx::resumes`]),
/// the key that resumes it, dim: ` · prefix P resumes` where it fits in
/// `room` cells, ` · prefix P` where that does (Mack's usual 34 columns),
/// else `paused` alone. It stands in for the agent's last reply (the
/// PAUSE's own acknowledgement says nothing), so a paused agent never reads
/// as done.
fn paused_line(room: usize, resumes: bool, dimc: Hue) -> Vec<Seg> {
    let mut v = vec![bold("paused", PAUSED_HUE)];
    let hint = [" · prefix P resumes", " · prefix P"].into_iter().find(|h| dwidth("paused") + dwidth(h) <= room);
    if let Some(h) = hint.filter(|_| resumes) {
        v.push(seg(h, dimc));
    }
    v
}

/// The cards that ask for a solid key-cap: the client's own agent, and one
/// asking or failed (what you will most likely type next).
fn loud_card(a: &Agent, cx: Cx) -> bool {
    own(a, cx) || matches!(a.cat, Some(Cat::NeedsInput | Cat::Failed))
}

/// The client's session when it holds a copy of the client's window (a
/// window linked into two spaces is `is_current` in both, by window id):
/// that copy, and only it, is the client's own card.
fn home_of(view: &ViewModel) -> Option<&str> {
    let s = view.cur_session.as_deref()?;
    let rows = view.spaces.iter().flat_map(|sp| sp.agents.iter().chain(&sp.plain)).chain(&view.pinned);
    rows.into_iter().any(|a| a.is_current && a.session == s).then_some(s)
}

/// `a` is the client's own card ([`home_of`]).
fn own(a: &Agent, cx: Cx) -> bool {
    a.is_current && cx.home.is_none_or(|s| a.session == s)
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

/// The faint ink of a second line with nothing to say ([`placeholder`]):
/// fainter than any real detail (`overlay2`), still 3:1 on every card.
pub const PLACEHOLDER: Hue = "overlay1";

/// What an agent's empty second line says: `no activity yet` while it
/// works (no blurb, no run detail), else `no reply yet`.
fn placeholder(a: &Agent) -> &'static str {
    if a.in_flight || a.cat == Some(Cat::Working) {
        "no activity yet"
    } else {
        "no reply yet"
    }
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

/// The ONE colour an agent's card sits on, both its rows: prefix v's
/// selection mauve, the client's own agent [`MINE_BG`] (lifted well off
/// the zebra: "you are here"), else the zebra's shade for the `k`th card of
/// its group ([`shade`]: consecutive cards alternate, so cards stacked with
/// no gap still part).
fn agent_bg(a: &Agent, k: usize, mine: bool) -> Ink {
    if a.selected {
        Ink::Mix("mauve", 18)
    } else if mine {
        Ink::Pal(MINE_BG)
    } else {
        shade(k)
    }
}

/// One agent as a card in one colour ([`agent_bg`]; `k`: its place in its
/// group, for the zebra), on the card grid: a state rail (`▎` in the
/// state's colour; the client's own idle agent lavender; none when idle;
/// the selection's heavier mauve `▌`), a pad, its key-cap ([`Badge::seg`]:
/// solid for the client's and an asking or failed agent, else a quiet
/// tint of its space's colour, `space`), a pad, the title (`text`, bold
/// for any state and for the client's), a flag (⚙ workflow, ◎ cua), and on
/// the right the state's glyph and word bold in its colour (nothing when
/// idle), the age beside it; with `two`, a second line under the title
/// ([`second_line`], maybe blank: `overlay2`, `sub` when it asks or failed,
/// a live blurb after a blue `›`) with the age at its end, so every agent
/// row is the same height. Both lines click through. `tag`: a dim word
/// before the second line's age (a PINNED card's space, `cs35l · 4m`),
/// dropped when it would leave the line under [`TAG_ROOM`] cells.
fn agent_rows(a: &Agent, cx: Cx, two: bool, k: usize, space: Ink, tag: Option<&str>) -> Vec<Row> {
    let w = inner(cx.w);
    let go = Target::Go { win: a.window_id.clone(), session: a.session.clone() };
    let msg = (two && cx.seconds()).then(|| second_line(a, cx.now));
    let word = state_word(a, cx);
    let mine = own(a, cx);
    // (Its edge from 26 columns, like second lines: narrower, the title.)
    let marked = mine && cx.seconds();
    let bg = agent_bg(a, k, mine);
    let busy = word.as_ref().filter(|x| !x.idle()).map(|x| x.hue);
    let rail = if a.selected {
        seg(RAIL_HEAVY, "mauve")
    } else if let Some(h) = busy {
        seg(RAIL, h)
    } else if mine {
        seg(RAIL, "lavender")
    } else {
        seg(" ", "text")
    };
    let age = Some(age_of(a, cx)).filter(|g| cx.ages() && !g.is_empty() && word.is_some());
    // Dim text on a tile needs the surface's dim; the own card's lift
    // needs `sub` (overlay2 is about 3:1 there).
    let dimc = if mine { MINE_DIM } else { dim(true) };
    let flag = a.flag.map(|(f, _)| match f {
        Flag::Workflow => seg(" ⚙", "teal"),
        Flag::Cua => seg(" ◎", if a.attention { "overlay2" } else { "blue" }),
    });
    // A PINNED card's ⌥W digit ([`pin_cap`]): under its letter on the
    // second line; on a one-line card, beside it.
    let pin = a.pin_key.filter(|_| tag.is_some() && cx.b.on).map(|n| pin_cap(n, bg, cx.b.loud));
    let inline = pin.is_some() && msg.is_none();
    let lead = cx.b.lead() + if inline { BADGE_W + 1 } else { 0 };
    // A paused card with its second line says `paused` there: the first
    // line keeps just the ⏸ (one word once, not twice).
    let paused = a.cat == Some(Cat::Paused) && !a.in_flight;
    let word_below = paused && msg.is_some();
    let right_of = |full: bool, with_age: bool| -> Vec<Seg> {
        let mut r = Vec::new();
        if let Some(wd) = word.as_ref().filter(|x| !x.idle()) {
            let full = full && !word_below;
            r.push(bold(if full { format!("{} {}", wd.glyph, wd.word) } else { wd.glyph.to_string() }, wd.hue));
        }
        if with_age && msg.is_none() {
            if let Some(ag) = &age {
                r.push(seg(format!(" {ag}"), dimc));
            }
        }
        r.push(seg(" ", "text"));
        r
    };
    let fixed = |r: &[Seg]| lead + flag.as_ref().map_or(0, |f| dwidth(&f.text)) + width(r) + 1 + usize::from(marked);
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
    let title = clip(&full, room);
    let title = if busy.is_some() || mine { bold(title, "text") } else { seg(title, "text") };
    let mut left = vec![rail.clone(), seg(" ", "text")];
    if cx.b.on {
        left.extend([cx.b.seg(a, space, bg, loud_card(a, cx)), seg(" ", "text")]);
    }
    if let Some(p) = pin.clone().filter(|_| inline) {
        left.extend([p, seg(" ", "text")]);
    }
    left.push(title);
    left.extend(flag);
    // The client's own card: a full lavender cell down its right edge
    // (both rows), where no state rail ever is.
    let edge = |mut r: Vec<Seg>| {
        if marked {
            // (After the right side's own pad: the word never touches it.)
            r.push(seg(MINE, MINE_HUE));
        }
        r
    };
    let mut first = tile_row(cx.w, bg, left, edge(right)).click(cx.w, go.clone()).covering([a]);
    first.anim = word.as_ref().is_some_and(Word::working);
    let Some(m) = msg else { return vec![first] };
    // Under the title (past the key-cap); without letters, indented anyway,
    // so a message never reads as another agent.
    let indent = if cx.b.on { lead - 1 } else { 3 };
    let right2 = |tag: Option<&str>| {
        let mut r: Vec<Seg> = Vec::new();
        if let Some(t) = tag {
            r.push(seg(format!(" {}", clip(&sanitize(t), TAG_W)), dimc));
            if age.is_some() {
                r.push(seg(" ·", dimc));
            }
        }
        r.extend(age.iter().map(|ag| seg(format!(" {ag}"), dimc)));
        r.push(seg(" ", "text"));
        edge(r)
    };
    let mut r2 = right2(tag);
    if tag.is_some() && inner(cx.w).saturating_sub(1 + indent + width(&r2)) < TAG_ROOM {
        r2 = right2(None);
    }
    let mut l2 = match pin {
        // The ⌥W digit cap under the letter's, on the card grid (c3-c5).
        Some(p) => vec![rail, seg(" ", "text"), p, seg(" ".repeat(indent - 1 - BADGE_W), "text")],
        None => vec![rail, seg(" ".repeat(indent), "text")],
    };
    if word_below {
        let room = inner(cx.w).saturating_sub(1 + indent + width(&r2));
        l2.extend(paused_line(room, cx.resumes, dimc));
    } else if live_blurb(a).is_some() {
        // Live: what it does right now, after a blue `›`.
        l2.extend([seg("› ", "blue"), seg(m, dimc)]);
    } else if m.trim().is_empty() && a.cat.is_some() {
        // Nothing to say yet: a faint placeholder, so the row never reads
        // as a broken card (the age stays where it is).
        l2.push(seg(placeholder(a), if mine { "overlay2" } else { PLACEHOLDER }));
    } else {
        let asks = matches!(a.cat, Some(Cat::NeedsInput | Cat::Failed));
        l2.push(seg(m, if asks { "sub" } else { dimc }));
    }
    let second = tile_row(cx.w, bg, l2, r2).click(cx.w, go);
    vec![first, second]
}

/// A pinned card's digit as a key-cap (` 1 `, [`BADGE_W`] wide): ⌥W then
/// that digit goes there (spaces.conf's `jump` table, like the letters).
/// The digit bold `text` on a neutral cap (overlay at 30% over the card),
/// the letter's quiet twin without a space colour: it is a key, not a place.
/// While ⌥W's table is armed (`loud`) it inverts with every letter cap
/// ([`ARMED`]). The PINNED header says `⌥W 1-3`.
fn pin_cap(n: u8, card: Ink, loud: bool) -> Seg {
    if loud {
        block(format!(" {n} "), ARMED.0, ARMED.1)
    } else {
        block(format!(" {n} "), "text", over(Ink::Pal("overlay"), 30, card))
    }
}

/// A second line's tag ([`agent_rows`]) is at most this many cells, and
/// goes when the line's text would keep fewer than [`TAG_ROOM`].
const TAG_W: usize = 10;
const TAG_ROOM: usize = 10;

/// A space's counts: the idle glyph's `overlay` lifted to `dimc` (the dim
/// of what they sit on).
fn surface_counts(c: &Counts, dimc: Hue) -> Vec<Seg> {
    count_segs(c).into_iter().map(|s| if s.fg == Ink::Pal("overlay") { Seg { fg: Ink::Pal(dimc), ..s } } else { s }).collect()
}

/// A space's ⌃N number as its chip says it (` 2 `; blank past 9 or unset).
fn space_num(sp: &Space) -> String {
    sp.index.filter(|n| (1..=9).contains(n)).map_or(" ".to_string(), |n| n.to_string())
}

/// The ink a space's name is bold in on a dark surface: its colour, or
/// `text` for a dark `@space_color` that would not read.
fn name_ink(ink: Ink) -> Ink {
    if ink_on(ink) == Ink::Pal("crust") {
        ink
    } else {
        Ink::Pal("text")
    }
}

/// A space's header in the agents section (herdr's statusline module): on
/// the canvas, ` N ` solid in the space's colour (dark ink, the one fill of
/// the group), the name bold in that colour, its branch dim when there is
/// room, its counts on the right, where a card's end. The client's space
/// is a BAND ([`band`]: its colour washed over the card's columns) with a
/// `▌` in its colour at the very edge, the branch and idle count lifted to
/// `sub`, so "where am I" reads at a glance and the band never melts into
/// the card under it. The header IS the space's HOME (no home row is
/// listed): a click goes to the home window, and while the client is ON
/// its home the header gets the client's own lift instead of the band
/// (`tile_me`, and a full lavender cell down its right edge, as the
/// client's own agent card). Collapsed, it stands for all its agents.
/// (Never red: the header keeps the colour its agents' key-caps wear.)
fn group_header(sp: &Space, cx: Cx, collapsed: bool) -> Row {
    let w = cx.w;
    let ink = space_colour(sp);
    let m = margin(w);
    let mine = sp.home.is_some() && sp.home.as_deref() == cx.cur;
    let here = sp.is_current || mine;
    let dimc = if here { "sub" } else { "overlay2" };
    let mut right = surface_counts(&sp.counts, dimc);
    if !right.is_empty() {
        // (A cell between a name cut short and its counts.)
        right.insert(0, seg(" ", "text"));
    }
    right.push(seg(" ", "text"));
    if mine && cx.seconds() {
        // (After the right side's own pad, where an own card's edge is.)
        right.push(seg(MINE, MINE_HUE));
    }
    let mut left = vec![block(format!(" {} ", space_num(sp)), ink_on(ink), ink), seg(" ", "text"), bold(sanitize(&sp.name), name_ink(ink))];
    if !sp.branch.is_empty() {
        let br = format!("  \u{e0a0} {}", sanitize(&sp.branch));
        if width(&left) + dwidth(&br) + width(&right) < inner(w) {
            left.push(seg(br, dimc));
        }
    }
    // The space's home: [`crate::actions::space_landing`].
    let target = Target::Session(sp.name.clone());
    let bg = if mine { Ink::Pal(MINE_BG) } else { band(ink) };
    let mut row = if here { tile_row(w, bg, left, right) } else { Row::new([vec![bare(m)], fit(inner(w), left, right, None), vec![bare(m)]].concat()) };
    if here && m > 0 {
        row.segs[0] = Seg { bg: Some(Ink::Canvas), ..seg(RAIL_HEAVY, ink) };
    }
    let row = row.click(w, target);
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
        vec![seg(format!("{indent}{} {k} idle · ", "○".repeat(k.min(4))), dim(true)), seg(names.join(", "), "sub")],
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

/// A space's agent cards in window-index order (the folded line last),
/// stacked edge to edge (no pads: consecutive cards alternate the zebra's
/// two shades, [`shade`], afresh under each space's header, so each card is
/// one colour and the next another); the agents in `two` get their second
/// line. Then its shells ([`shells_of`]), one row each ([`shell_row`]); with
/// `fold` (the steps that fold the idle agents), two or more of them in one
/// line ([`shells_line`]).
fn group_rows(sp: &Space, cx: Cx, two: &[Key], fold: bool) -> Vec<Row> {
    let (shown, idle) = split_idle(sp, fold, cx.now);
    let ink = space_colour(sp);
    let mut rows: Vec<Row> = Vec::new();
    for (k, a) in shown.iter().enumerate() {
        rows.extend(agent_rows(a, cx, two.contains(&key(a)), k, ink, None));
    }
    if !idle.is_empty() {
        // The zebra goes on: the fold is the next card.
        rows.push(fold_row(&idle, cx, shade(shown.len())));
    }
    let shells = shells_of(sp, cx.shells);
    if fold && shells.len() >= 2 {
        rows.push(shells_line(&shells, cx, ink));
    } else {
        rows.extend(shells.iter().map(|s| shell_row(s, cx, ink)));
    }
    rows
}

/// A space's agents in `room` rows: all of them one line each if they fit
/// (and as many of its shells as fit after them), else the `room - 1` that
/// matter most ([`promotion_rank`], kept in list order) and a `… N more`
/// line for the rest.
fn partial_rows(sp: &Space, cx: Cx, room: usize) -> Vec<Row> {
    let ink = space_colour(sp);
    if sp.agents.len() <= room {
        let mut rows: Vec<Row> = sp.agents.iter().enumerate().flat_map(|(k, a)| agent_rows(a, cx, false, k, ink, None)).collect();
        let left = room - rows.len();
        rows.extend(shells_of(sp, cx.shells).into_iter().take(left).map(|s| shell_row(s, cx, ink)));
        return rows;
    }
    let mut ranked: Vec<usize> = (0..sp.agents.len()).collect();
    ranked.sort_by_key(|&k| promotion_rank(&sp.agents[k], cx.now));
    let mut keep = ranked[..room - 1].to_vec();
    keep.sort_unstable();
    let mut rows: Vec<Row> = keep.iter().enumerate().flat_map(|(n, &k)| agent_rows(&sp.agents[k], cx, false, n, ink, None)).collect();
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
/// agents or shells; in space scope, just the client's session, whatever
/// it holds.
fn groups(view: &ViewModel) -> Vec<&Space> {
    let mut v: Vec<&Space> = match &view.scope.session {
        Some(s) => view.spaces.iter().filter(|sp| &sp.name == s).collect(),
        None => view.spaces.iter().filter(|s| !bare_group(view, s)).collect(),
    };
    v.sort_by_key(|s| space_order(s));
    v
}

/// Nothing to list under a space's header: no agent, no shell.
fn bare_group(view: &ViewModel, sp: &Space) -> bool {
    sp.agents.is_empty() && shells_of(sp, view.shells).is_empty()
}

/// The agents section's rows when every agent (and shell) is on one line,
/// nothing folded or collapsed: what step 4 leaves the agents before NEEDS
/// YOU gets the rest.
fn agents_one_line(view: &ViewModel) -> usize {
    let pins = if view.pinned.is_empty() { 0 } else { 1 + view.pinned.len() };
    let groups = groups(view);
    if groups.iter().all(|g| bare_group(view, g)) {
        return pins + 2 + usize::from(view.scope.session.is_some());
    }
    pins + 1 + groups.iter().map(|g| 1 + g.agents.len() + shells_of(g, view.shells).len()).sum::<usize>()
}

/// A space header in the agents section ([`group_header`]: the whole row
/// switches to its space; nothing else there does).
fn is_group_header(r: &Row, w: usize) -> bool {
    matches!(r.targets.as_slice(), [(0, x1, Target::Session(_))] if usize::from(*x1) == w)
}

/// A blank canvas row that does nothing and stands for no one (a gap).
fn is_blank(r: &Row) -> bool {
    r.bg.is_none() && r.targets.is_empty() && r.covers.is_empty() && r.text().trim().is_empty()
}

/// Where an agents section wants a gap that it does not have: above each
/// space header but the first, and above the AGENTS header under the
/// PINNED block.
fn gap_spots(rows: &[Row], w: usize) -> Vec<usize> {
    let mut seen = false;
    let mut v = Vec::new();
    for (y, r) in rows.iter().enumerate() {
        if is_group_header(r, w) {
            if seen {
                v.push(y);
            }
            seen = true;
        } else if y > 0 && rows[y - 1].pinned && !r.pinned && !is_blank(r) {
            v.push(y);
        }
    }
    v.retain(|&y| !is_blank(&rows[y - 1]));
    v
}

/// How many headers in an agents section lack their gap ([`gap_spots`]).
fn group_gaps(rows: &[Row], w: usize) -> usize {
    gap_spots(rows, w).len()
}

/// The agents section with a gap above each space header but the first,
/// and under the PINNED block (spare rows spent on spacing, whatever the
/// agents' step).
fn spaced_groups(rows: Vec<Row>, w: usize) -> Vec<Row> {
    let spots = gap_spots(&rows, w);
    let mut out: Vec<Row> = Vec::with_capacity(rows.len() + spots.len());
    for (y, r) in rows.into_iter().enumerate() {
        if spots.contains(&y) {
            out.push(Row::blank(w));
        }
        out.push(r);
    }
    out
}

/// The PINNED block's header: ` PINNED` like the other section labels; on
/// the right the keys that reach them as a herdr-style hint (`⌥W` bold
/// blue, as AGENTS' `⌥V`, then the digits dim: `⌥W 1-3`, `⌥W 1` for one;
/// none when no pin holds a key), then the pin glyph and how many, dim (a
/// pin asks for nothing). Too narrow for both, the count stays.
fn pinned_header(pins: &[Agent], w: usize) -> Row {
    let keys = pins.iter().filter_map(|a| a.pin_key).max();
    let mut right = Vec::new();
    match keys {
        Some(1) => right.extend([bold("⌥W", "blue"), seg(" 1", LABEL)]),
        Some(k) => right.extend([bold("⌥W", "blue"), seg(format!(" 1-{k}"), LABEL)]),
        None => {}
    }
    let count = format!("{} {}", crate::pins::GLYPH, pins.len());
    right.push(seg(if keys.is_some() { format!("  {count}") } else { count.clone() }, LABEL));
    if keys.is_some() && dwidth(" PINNED") + width(&right) + 2 > w {
        right = vec![seg(count, LABEL)];
    }
    Row { pinned: true, ..section_header(w, "PINNED", right) }
}

/// The PINNED block: its header, then each pinned agent ([`ViewModel::pinned`],
/// pin order) as its own card, exactly as in its space (letter, state, the
/// client's lift, clicks), the zebra afresh under the header, the agents in
/// `two` on two lines; out of its space's group, its second line names the
/// space (dim, before the age), and its key-cap wears the space's colour as
/// everywhere. Never folded: a pin says "keep this in view". Empty when
/// nothing is pinned.
fn pin_block(view: &ViewModel, cx: Cx, two: &[Key]) -> Vec<Row> {
    if view.pinned.is_empty() {
        return Vec::new();
    }
    let mut rows = vec![pinned_header(&view.pinned, cx.w)];
    for (k, a) in view.pinned.iter().enumerate() {
        let cards = agent_rows(a, cx, two.contains(&key(a)), k, space_ink(view, &a.session), Some(&a.session));
        rows.extend(cards.into_iter().map(|r| Row { pinned: true, ..r }));
    }
    rows
}

/// ` AGENTS` and the scope on the right (`this space` / `all spaces`): a
/// herdr-style key hint once the scope is a thing (`⌥V` bold blue, the
/// word dim; a click on it flips it too: a hint, not a selection, so no
/// fill), else the word dim.
fn agents_header(view: &ViewModel, w: usize) -> Row {
    let word = if view.scope.session.is_some() { "this space" } else { "all spaces" };
    let toggles = view.scope.toggles() && w > dwidth(word) + 14;
    let right = if toggles { vec![bold("⌥V", "blue"), seg(format!(" {word}"), LABEL)] } else { vec![seg(word, LABEL)] };
    let ww = width(&right) + 1;
    let mut row = section_header(w, "AGENTS", right);
    if toggles {
        row.targets.push(((w - ww) as u16, (w - 1) as u16, Target::Scope));
    }
    row
}

/// The agents section (header, then each group's header and rows), at most
/// `budget` rows → (rows, its ladder step). `spaced`: a blank row between
/// groups when everything else fits (the frame's [`STEPS`] say when).
///
/// Steps: 0 every agent two lines (as asked, then without the gaps between
/// groups); 1 the same, idle folded; 2 every agent one line; 3 the same,
/// idle folded; 4 spaces collapsed; 5 `… N more`. Never a mix of heights
/// in one frame.
///
/// The PINNED block ([`pin_block`]) comes first, above the AGENTS header
/// (a blank row between them when `spaced`): its cards take the same
/// height as every other agent row but never fold, and the spaces collapse
/// before it loses anything; only a frame too short for every pin on one
/// line cuts it (the first pins, then one `… N agents` line for the rest).
fn agents_section(view: &ViewModel, cx: Cx, budget: usize, spaced: bool) -> (Vec<Row>, u8) {
    if budget == 0 {
        return (vec![], 0);
    }
    let w = cx.w;
    let groups = groups(view);
    let space = view.scope.session.is_some();
    let header = agents_header(view, w);
    let pins = !view.pinned.is_empty();
    // Nothing (else) to list: the space's header even with nothing in it,
    // so the section says whose it is; the words say why.
    let bare = if space { groups.iter().all(|g| bare_group(view, g)) } else { groups.is_empty() };
    let mine_pinned = view.pinned.iter().any(|a| Some(&a.session) == view.scope.session.as_ref());
    let empty = match (space, pins) {
        (true, _) if mine_pinned => "   all pinned above",
        (true, _) => "   no agents in this space",
        (false, true) => "   all pinned above",
        (false, false) => "   no agents running",
    };
    let build = |two: &[Key], fold: bool, collapsed: &[bool], spaced: bool| {
        let mut rows = pin_block(view, cx, two);
        if spaced && pins {
            rows.push(Row::blank(w));
        }
        rows.push(header.clone());
        if bare {
            if space {
                rows.extend(groups.iter().map(|g| group_header(g, cx, false)));
            }
            rows.push(dim_line(w, empty));
            return rows;
        }
        for (k, (sp, &c)) in groups.iter().zip(collapsed).enumerate() {
            if spaced && k > 0 {
                rows.push(Row::blank(w));
            }
            rows.push(group_header(sp, cx, c));
            if !c {
                rows.extend(group_rows(sp, cx, two, fold));
            }
        }
        rows
    };
    let mut collapsed = vec![false; groups.len()];
    if bare && !pins {
        let mut rows = build(&[], false, &collapsed, false);
        rows.truncate(budget);
        return (rows, 0);
    }
    let all: Vec<Key> = view.pinned.iter().map(key).chain(groups.iter().flat_map(|g| g.agents.iter().map(key))).collect();
    // Full detail: the gaps between spaces go first.
    for sp in [spaced, false] {
        let rows = build(&all, false, &collapsed, sp);
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
    // The pinned block on one line each: what every later step keeps.
    let lead = pin_block(view, cx, &[]).len();
    if bare {
        return pins_cut(view, cx, &groups, &header, budget);
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
        let at = lead + 1 + (0..i)
            .map(|j| 1 + if collapsed[j] { 0 } else { group_rows(groups[j], cx, &[], true).len() })
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
            rows.insert(at + 1, agent_rows(&g.agents[top], cx, false, 0, space_colour(g), None).remove(0));
            return (rows, 4);
        }
        // The space whose collapse made it fit keeps the rows that frees:
        // its most important agents, one line each, and `… N more`.
        let mut rows = base;
        rows[at] = group_header(groups[i], cx, false);
        rows.splice(at + 1..at + 1, partial_rows(groups[i], cx, spare));
        return (rows, 4);
    }
    // More spaces than rows: the first ones as headers, the rest counted.
    if budget < lead + 2 {
        return pins_cut(view, cx, &groups, &header, budget);
    }
    let keep = budget - lead - 2;
    let mut rows = build(&[], true, &collapsed, false);
    rows.truncate(lead + 1 + keep);
    let rest: Vec<&Agent> = groups[keep..].iter().flat_map(|g| g.agents.iter()).collect();
    rows.push(more_row(w, &rest, groups.len() - keep));
    (rows, 5)
}

/// The agents section in fewer rows than the pins on one line each and a
/// line for the rest (step 5): the PINNED header and the first pins, then
/// one `… N more` line for every agent not shown (or the AGENTS header, when
/// every pin is shown and nothing else is listed). One row: that line alone.
/// Without pins it is the old last resort, unchanged.
fn pins_cut(view: &ViewModel, cx: Cx, groups: &[&Space], header: &Row, budget: usize) -> (Vec<Row>, u8) {
    let w = cx.w;
    let in_groups = || groups.iter().flat_map(|g| g.agents.iter());
    if budget == 1 {
        let all: Vec<&Agent> = view.pinned.iter().chain(in_groups()).collect();
        let spaces = if view.pinned.is_empty() { groups.len() } else { 0 };
        return (vec![more_row(w, &all, spaces)], 5);
    }
    let block = pin_block(view, cx, &[]);
    let shown = (budget - 1).min(block.len());
    let mut rows: Vec<Row> = block[..shown].to_vec();
    let rest: Vec<&Agent> = view.pinned.iter().skip(shown.saturating_sub(1)).chain(in_groups()).collect();
    rows.push(if rest.is_empty() { header.clone() } else { Row { pinned: true, ..more_row(w, &rest, 0) } });
    (rows, 5)
}

// ---------------------------------------------------------------- needs you

/// How strongly a NEEDS YOU card takes its state's colour (one tint for the
/// whole card): consecutive cards, edge to edge, alternate the two.
const NEED_TINT: [u8; 2] = [14, 9];

/// A NEEDS YOU card on the card grid, a tile of 2 or 1 lines all on its
/// state's colour mixed over the canvas (`tint`: [`NEED_TINT`]'s index,
/// the zebra's), a heavy `▌` rail in that colour down both rows; the
/// key-cap always solid (the letter you will press), the title bold, and
/// on the right the state's glyph and word bold in its colour (the age
/// beside it on a one-line card). Line 2: the reason in `text` (here the
/// detail IS the content; a `perm` prompt says so), or the state in words;
/// the age at its end.
fn need_rows(n: &Need, cx: Cx, lines: u8, tint: usize, space: Ink) -> Vec<Row> {
    let w = cx.w;
    let iw = inner(w);
    let a = &n.agent;
    let word = a.cat.map(|c| cat_word(a, c));
    let hue = word.as_ref().map_or(a.color.unwrap_or("overlay2"), |x| x.hue);
    let bg = Ink::Mix(hue, NEED_TINT[tint]);
    let go = Target::Go { win: a.window_id.clone(), session: a.session.clone() };
    let card = |bg: Ink, l: Vec<Seg>, r: Vec<Seg>| tile_row(w, bg, l, r).click(w, go.clone());
    let rail = || seg(RAIL_HEAVY, hue);
    let age = Some(age_of(a, cx)).filter(|g| cx.ages() && !g.is_empty()).map(|g| seg(format!(" {g}"), dim(true)));
    let lead = cx.b.lead();
    let state = |full: bool| match &word {
        Some(x) if full => bold(format!("{} {}", x.glyph, x.word), hue),
        _ => bold(a.glyph, hue),
    };
    // The richest right side that leaves the title room (its whole, or 10
    // cells: the word is worth a clipped title, as on an agent's card):
    // the word, then the glyph alone, then (a one-line card) no age.
    let one = lines < 2;
    let rights: Vec<Vec<Seg>> = [(true, one), (false, one), (false, false)]
        .into_iter()
        .map(|(full, aged)| {
            let mut r = vec![state(full)];
            r.extend(age.clone().filter(|_| aged));
            r.push(seg(" ", "text"));
            r
        })
        .collect();
    let want = dwidth(no_home(&a.title)).min(10);
    let right = rights.iter().find(|r| lead + width(r) + 1 + want <= iw).unwrap_or(&rights[2]).clone();
    let (proj, title) = fit_label(no_home(&a.title), iw.saturating_sub(lead + width(&right) + 1));
    let mut first = vec![rail(), seg(" ", "text")];
    if cx.b.on {
        first.extend([cx.b.seg(a, space, bg, true), seg(" ", "text")]);
    }
    first.extend([seg(proj, dim(true)), bold(title, "text")]);
    let mut rows = vec![card(bg, first, right)];
    if lines >= 2 {
        let indent = if cx.b.on { lead - 1 } else { 3 };
        let mut l2 = vec![rail(), seg(" ".repeat(indent), "text")];
        if n.reason.is_empty() {
            l2.push(seg(n.reason_word.clone(), hue));
        } else {
            if n.reason_word == "perm" {
                l2.push(seg("perm ", "yellow"));
            }
            l2.push(seg(plain(&n.reason), "text"));
        }
        let mut r2: Vec<Seg> = age.into_iter().collect();
        r2.push(seg(" ", "text"));
        rows.push(card(bg, l2, r2));
    }
    rows
}

/// The colour of the space `session` (its `@space_color`, else peach), for
/// a card outside its space's group.
fn space_ink(view: &ViewModel, session: &str) -> Ink {
    view.spaces.iter().find(|s| s.name == session).map_or(Ink::Pal("peach"), space_colour)
}

/// ` NEEDS YOU` and the count as a square chip (dark ink on yellow; on red
/// when a failure is waiting), or `✓ all clear` in green with nothing
/// pending.
fn needs_header(view: &ViewModel, w: usize) -> Row {
    let n = view.needs.len();
    let failed = view.needs.iter().any(|x| x.agent.cat == Some(Cat::Failed));
    let right = if n > 0 { module(&n.to_string(), if failed { "red" } else { "yellow" }, vec![]) } else { vec![seg("✓ all clear", "green")] };
    section_header(w, "NEEDS YOU", right)
}

/// The whole NEEDS YOU section: its header and the cards, edge to edge
/// (each one tint, consecutive cards alternating). Never empty: with
/// nothing pending it is the header saying so.
fn needs_block(view: &ViewModel, cx: Cx, lines: u8) -> Vec<Row> {
    let mut rows = vec![needs_header(view, cx.w)];
    for (k, n) in view.needs.iter().enumerate() {
        let tint = usize::from(!k.is_multiple_of(2));
        rows.extend(need_rows(n, cx, lines, tint, space_ink(view, &n.agent.session)));
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
    let mut rows = needs_block(view, cx, 1);
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

// ---------------------------------------------------------------- the dock

/// The spaces the dock lists, in [`space_order`]: every session the view
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

/// The state glyph of a space's most urgent attention agent, in its colour.
fn alert_of(sp: &Space) -> Option<(&'static str, Hue)> {
    sp.agents.iter().filter(|a| a.attention).min_by_key(|a| a.rank).and_then(|a| a.cat).map(|c| (c.glyph(), cat_hue(c)))
}

/// How strongly the client's space's chip takes its colour over the dock.
const HERE_CHIP: u8 = 30;

/// A space as a chip in the dock (the status line's modules, square
/// ends): ` N ` solid in its colour (dark ink; the client's red while the
/// prefix is armed), then ` name ` on `surface0` (`sub`; dim when it holds
/// no agent); the client's name bold `text` on its colour washed over the
/// dock ([`HERE_CHIP`]); an attention agent's glyph inside the name, bold
/// in its colour. A click switches the client to it.
fn space_pill(sp: &Space, prefix: bool) -> Item {
    let here = sp.is_current;
    let ink = if here { chip_colour(sp, prefix) } else { space_colour(sp) };
    let name_bg = if here { over(space_colour(sp), HERE_CHIP, Ink::Pal(PANEL)) } else { Ink::Pal("surface0") };
    let name = clip(&sanitize(&sp.name), 12);
    let quiet = sp.counts.total() == 0;
    let mut segs = match sp.index.filter(|n| (1..=9).contains(n)) {
        Some(n) => vec![block(format!(" {n} "), ink_on(ink), ink)],
        // The client's still says it is the client's (and armed) by a swatch.
        None if here => vec![block(" ", ink_on(ink), ink)],
        None => vec![],
    };
    segs.push(on(" ", "text", name_bg));
    segs.push(if here { block(name, "text", name_bg) } else { on(name, if quiet { dim(true) } else { "sub" }, name_bg) });
    if let Some((g, hue)) = alert_of(sp) {
        segs.push(block(format!(" {g}"), hue, name_bg));
    }
    segs.push(on(" ", "text", name_bg));
    item(segs, Some(Target::Session(sp.name.clone())))
}

/// The spaces as chips in at most `lines` dock rows (the bottom's last
/// resort): all of them, else the client's space, those with an attention
/// agent, then ⌃N order, kept (shown in list order), and a `+K` chip (the
/// menu's All tab) for the rest.
fn pill_rows(view: &ViewModel, w: usize, lines: usize, prefix: bool) -> Vec<Row> {
    let list = space_list(view);
    let items: Vec<Item> = list.iter().map(|sp| space_pill(sp, prefix)).collect();
    let mut rank: Vec<usize> = (0..list.len()).collect();
    rank.sort_by_key(|&i| (!list[i].is_current, !space_alert(list[i])));
    let more = |k: usize| item(module(&format!("+{k}"), "overlay2", vec![]), Some(Target::Menu(Some("all"))));
    let rows = pack(&[], &items, &rank, inner(w), lines, &more).0;
    rows.into_iter().map(|r| into_tile(r, w, Ink::Pal(PANEL))).collect()
}

/// How many chip lines every space needs (at most 3).
fn pill_lines(view: &ViewModel, w: usize) -> usize {
    let items: Vec<Item> = space_list(view).iter().map(|sp| space_pill(sp, false)).collect();
    place(&items, inner(w), usize::MAX).map_or(1, |r| r.len()).clamp(1, 3)
}

/// The rows the spaces keep however short the frame (held back before
/// anything else is laid out, agents included): the gap above the dock
/// and one line of chips. 0 with no spaces, or when `avail` is under 4.
fn spaces_min(view: &ViewModel, avail: usize) -> usize {
    if space_list(view).is_empty() || avail < 4 {
        0
    } else {
        2
    }
}

/// How many spaces the dock lists whole before it prefers its collapsed
/// form when reserving rows ([`spaces_listed`]).
const SPACES_LISTED: usize = 6;

/// The rows the dock with its spaces stood on end needs (its gap, its
/// caps, every space a row, up to [`SPACES_LISTED`] of them, else the
/// collapsed form; PARKED and the shells' line aside). What the steps
/// that keep decoration leave it.
fn spaces_listed(view: &ViewModel, w: usize) -> usize {
    let n = space_list(view).len();
    if n == 0 {
        return 0;
    }
    let form = if n <= SPACES_LISTED { SpacesForm::Full } else { SpacesForm::Collapsed };
    spaces_rows(view, w, form, false).len() + 3
}

/// One space as a row of the dock, the status line's modules stood on
/// end: ` N ` solid in the space's colour (dark bold digit), its name
/// (`sub`, dim with no agent), its branch dim when there is room, its
/// agents' counts on the right (the AGENTS headers' glyphs). The client's
/// space is its BAND ([`band`]), a `▌` in its colour at the panel's edge,
/// the name bold `text`, its chip red while the prefix is armed. A click
/// switches to it.
fn space_row(sp: &Space, w: usize, prefix: bool) -> Row {
    let here = sp.is_current;
    let ink = space_colour(sp);
    let chip = if here { chip_colour(sp, prefix) } else { ink };
    let dimc = if here { "sub" } else { dim(true) };
    let mut right = surface_counts(&sp.counts, dimc);
    if !right.is_empty() {
        // (A cell between a name cut short and its counts.)
        right.insert(0, seg(" ", "text"));
    }
    right.push(seg(" ", "text"));
    let name = sanitize(&sp.name);
    let quiet = sp.counts.total() == 0;
    let mut left = vec![
        if here { seg(RAIL_HEAVY, ink) } else { seg(" ", "text") },
        block(format!(" {} ", space_num(sp)), ink_on(chip), chip),
        seg(" ", "text"),
        if here { bold(name, "text") } else { seg(name, if quiet { dim(true) } else { "sub" }) },
    ];
    if !sp.branch.is_empty() {
        let br = format!("  \u{e0a0} {}", sanitize(&sp.branch));
        if width(&left) + dwidth(&br) + width(&right) < inner(w) {
            left.push(seg(br, dimc));
        }
    }
    let bg = if here { band(ink) } else { Ink::Pal(PANEL) };
    tile_row(w, bg, left, right).click(w, Target::Session(sp.name.clone()))
}

/// `+K more spaces`: the ones a collapsed dock leaves out (the menu's All
/// tab).
fn spaces_more(k: usize, w: usize) -> Row {
    tile_row(w, Ink::Pal(PANEL), [vec![seg(" ", "text")], module(&format!("+{k}"), "overlay2", vec![]), vec![seg(" more spaces", dim(true))]].concat(), vec![])
        .click(w, Target::Menu(Some("all")))
}

/// The dock's space rows in a form, richest first: every space a row
/// (`full`); the client's space and those with an attention agent, then
/// `+K more`; else (the last resort) the chips in `lines` lines.
fn spaces_rows(view: &ViewModel, w: usize, form: SpacesForm, prefix: bool) -> Vec<Row> {
    let list = space_list(view);
    if list.is_empty() {
        return Vec::new();
    }
    match form {
        SpacesForm::Full => list.iter().map(|sp| space_row(sp, w, prefix)).collect(),
        SpacesForm::Collapsed => {
            let kept: Vec<&&Space> = list.iter().filter(|sp| sp.is_current || space_alert(sp)).collect();
            let mut rows: Vec<Row> = kept.iter().map(|sp| space_row(sp, w, prefix)).collect();
            rows.push(spaces_more(list.len() - kept.len(), w));
            rows
        }
        SpacesForm::Chips(lines) => pill_rows(view, w, lines.max(1), prefix),
    }
}

/// How the dock's spaces are drawn ([`spaces_rows`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SpacesForm {
    Full,
    Collapsed,
    Chips(usize),
}

/// The dock's PARKED line: ` PARKED` and its count as a square mauve chip
/// (the stash's colour; `none` dim with nothing parked): a click opens the
/// same popup as prefix h ([`Target::Stash`]). On the right a message
/// (Roster.say) or the watcher's problem in red (click: restart).
fn parked_header(view: &ViewModel, w: usize, msg: Option<&str>) -> Row {
    let n = view.parked.len();
    let mut left = vec![bold(" PARKED", LABEL), seg(" ", "text")];
    if n > 0 {
        left.extend(module(&n.to_string(), "mauve", vec![]));
    } else {
        left.push(seg("none", dim(true)));
    }
    let (right, restart) = match (msg, watcher_problem(view.watcher_age)) {
        (Some(m), _) => (vec![seg(format!("{} ", sanitize(m)), "sub")], false),
        (None, Some(p)) => (vec![seg(format!("watcher {p} "), "red")], true),
        (None, None) => (vec![], false),
    };
    let rw = width(&clip_segs(right.clone(), inner(w)));
    let mut row = tile_row(w, Ink::Pal(PANEL), left, right);
    // The corner (and the margin beside it) is the message's or the
    // watcher's; the rest of the line is the stash's.
    let edge = if rw > 0 { (w - margin(w) - rw) as u16 } else { w as u16 };
    let w16 = w as u16;
    if edge > 0 {
        row.targets.push((0, edge, Target::Stash));
    }
    if restart && edge < w16 {
        row.targets.push((edge, w16, Target::Restart));
    }
    row
}

/// The dock: one raised panel anchoring the bottom (OpenCode's prompt
/// box), the gap above it, then its `▄` cap, the spaces, the shells' line
/// when they are folded into it (the caller's), PARKED, its `▀` cap.
#[derive(Debug, Default)]
struct Dock {
    /// The gap, the top cap and the space rows.
    head: Vec<Row>,
    /// How many of `head`'s last rows are spaces.
    spaces: usize,
    /// PARKED and the bottom cap.
    tail: Vec<Row>,
}

impl Dock {
    fn len(&self) -> usize {
        self.head.len() + self.tail.len()
    }
}

/// The dock in `room` rows (at least 1; the shells' line aside), the
/// richest form that fits. PARKED is always its one line
/// ([`parked_header`]: the label and the count), never the parked tabs
/// themselves, at any height. As rows run short the spaces collapse to
/// the client's space and the attention ones (`+K more`), then to chips on
/// as few as one line, each line of chips going only after the caps (a
/// space is content, the caps are not); the gap above the dock goes last
/// of all (with the spaces).
fn bottom(view: &ViewModel, w: usize, room: usize, prefix: bool, msg: Option<&str>) -> Dock {
    let header = parked_header(view, w, msg);
    let make = |gap: bool, caps: bool, spaces: Vec<Row>| -> Dock {
        let mut head = if gap { vec![Row::blank(w)] } else { Vec::new() };
        let mut tail = vec![header.clone()];
        if caps {
            head.push(pad_row(w, Ink::Pal(PANEL), PAD_TOP));
            tail.push(pad_row(w, Ink::Pal(PANEL), PAD_BOTTOM));
        }
        let n = spaces.len();
        head.extend(spaces);
        Dock { head, spaces: n, tail }
    };
    let list = space_list(view);
    if list.is_empty() {
        let forms = [(true, true), (true, false), (false, false)];
        return forms.into_iter().map(|(g, c)| make(g, c, Vec::new())).find(|d| d.len() <= room).unwrap_or_else(|| make(false, false, Vec::new()));
    }
    if room < 3 {
        return make(false, false, Vec::new());
    }
    let chips = pill_lines(view, w);
    let collapsible = list.iter().any(|sp| !sp.is_current && !space_alert(sp));
    let mut forms: Vec<(SpacesForm, bool)> = vec![(SpacesForm::Full, true)];
    if collapsible {
        forms.push((SpacesForm::Collapsed, true));
    }
    // Every space's chip before the caps.
    forms.extend((1..=chips).rev().flat_map(|l| [(SpacesForm::Chips(l), true), (SpacesForm::Chips(l), false)]));
    for (form, caps) in forms {
        let d = make(true, caps, spaces_rows(view, w, form, prefix));
        if d.len() <= room {
            return d;
        }
    }
    make(false, false, Vec::new())
}

/// One step at which agents keep full detail ([`STEPS`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Step {
    /// Blank gaps between sections (and between spaces in AGENTS).
    pub gaps: bool,
    /// The dock keeps the rows its spaces stood on end need (every space a
    /// row, [`spaces_listed`]); without, it has its held-back minimum and
    /// takes what is left.
    pub listed: bool,
    /// NEEDS YOU card lines.
    pub lines: u8,
}

/// The steps at which agents keep full detail, roomiest first: the gaps
/// go (pure decoration, before anything that says something; the gaps
/// between spaces in AGENTS before the section gaps), then the dock's
/// reserve, then the cards' second lines. The dock takes what each leaves
/// over. (No step has pads: list tiles stack edge to edge, in the zebra,
/// at every height.)
pub const STEPS: [Step; 4] = [
    Step { gaps: true, listed: true, lines: 2 },
    Step { gaps: false, listed: true, lines: 2 },
    Step { gaps: false, listed: false, lines: 2 },
    Step { gaps: false, listed: false, lines: 1 },
];

/// The whole screen: the panel, NEEDS YOU, agents (and shells), spare rows, the
/// spaces, the last row; then the live toasts as cards over its top. Always
/// exactly `height` rows of exactly `width` cells.
pub fn layout(view: &ViewModel, width: u16, height: u16, opts: &Opts) -> Frame {
    let (w, h) = (usize::from(width), usize::from(height));
    let mut frame = Frame { width, height, ..Frame::default() };
    if w == 0 || h == 0 {
        return frame;
    }
    let cx = Cx {
        w,
        b: Badge::of(view, &opts.bar),
        now: opts.now.unwrap_or(view.now),
        spin: opts.spin,
        home: home_of(view),
        cur: view.cur_win.as_deref(),
        shells: view.shells,
        resumes: matches!(opts.pause, Some(Phase::Stopping { .. } | Phase::Safe { .. } | Phase::Ready { .. })),
    };
    // Pinned agents move out of their spaces into PINNED (a view already
    // split is laid out as it is). The letters, the client's own card and
    // the key-caps above were read off the whole view.
    let split;
    let view = if view.pinned.is_empty() && view.agents().any(|a| a.is_pinned()) {
        split = view.pins_split();
        &split
    } else {
        view
    };
    let keep = sections(view, &mut frame, opts, cx).min(frame.rows.len());
    // The toasts float over whatever is there: nothing beneath moves, and
    // the rows they hide still stand for their agents (back when they go).
    // They start under the key-table marks and the selection hint, which
    // stay readable while a key press waits on them.
    // Never over PARKED either (the stash, a message, the watcher's
    // warning): the stack ends above it, whatever the dock's caps put
    // under it (toast_stack keeps the last row of the height it is given).
    let limit = if frame.parked.is_empty() { frame.rows.len() } else { frame.parked.start + 1 };
    let (cards, until) = toast_stack(&opts.bar.toasts, w, limit.saturating_sub(keep), cx.now);
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
    // The pause banner first, at the very top: the panel under it is laid
    // out in the rows it leaves, and the toasts float under it, never over.
    let banner = opts.pause.as_ref().map_or_else(Vec::new, |p| pause_rows(p, cx, pause_lines(h)));
    let nb = banner.len();
    let Top { rows: top, status, card, keep } = top_rows(view, bar, w, h - nb, cx.now);
    let shift = |r: Range<usize>| r.start + nb..r.end + nb;
    let (status, card, keep) = (shift(status), shift(card), keep + nb);
    frame.pause = 0..nb;
    let top = [banner, top].concat();
    // The rows between the top and the PARKED line (held back like the old
    // last row: it carries the stash, the messages and the watcher).
    let avail = h.saturating_sub(top.len() + 1);
    if avail == 0 {
        // No room for sections: one row still counts every agent of the
        // scope, then the parked row.
        let all: Vec<&Agent> = view.pinned.iter().chain(groups(view).into_iter().flat_map(|s| s.agents.iter())).collect();
        let mut rows = vec![top.into_iter().next().unwrap_or_else(|| Row::blank(w))];
        rows.push(if all.is_empty() { Row::blank(w) } else { more_row(w, &all, 0) });
        rows.push(parked_header(view, w, opts.msg.as_deref()));
        rows.truncate(h);
        // (Its line is the third row: empty when the frame cut it.)
        frame.parked = if rows.len() == 3 { 2..3 } else { rows.len()..rows.len() };
        frame.dock = frame.parked.clone();
        frame.rows = rows;
        frame.step = STEPS.len() as u8;
        frame.status = 0..0;
        // (The banner, when there is one, is that first row.)
        frame.pause = 0..nb.min(1);
        return keep.min(1);
    }
    // The dock's gap and one line of chips are held back first: spaces are
    // listed nowhere else, so they never go before the agents lose detail.
    let min = spaces_min(view, avail);
    let avail = avail - min;
    // The spaces stood on end say something (each space's counts): the
    // steps that keep them ([`Step::listed`]) leave the dock its rows; the
    // others only its held-back minimum.
    let reserve = if min == 0 { 0 } else { spaces_listed(view, w).saturating_sub(min) };
    let fits = STEPS.iter().enumerate().find_map(|(i, &Step { gaps, listed, lines })| {
        let needs = needs_block(view, cx, lines);
        let agents = agents_section(view, cx, usize::MAX, gaps).0;
        let gap_rows = usize::from(gaps) * 2;
        let room = if listed { avail.saturating_sub(reserve) } else { avail };
        (needs.len() + agents.len() + gap_rows <= room).then_some((i as u8, gaps, needs, agents))
    });
    let (step, gaps, needs, (mut agents, agents_step)) = match fits {
        Some((i, gaps, needs, agents)) => (i, gaps, needs, (agents, 0)),
        None => {
            // The agents compress. NEEDS YOU gets what the agents' one-line
            // shape leaves (up to the whole, at least 2 rows and a third of
            // the body), and always leaves the agents a row.
            let n1 = needs_block(view, cx, 1).len();
            let floor = (avail / 3).max(2);
            let room = n1.min(avail.saturating_sub(agents_one_line(view)).max(floor)).min(avail - 1);
            let needs = needs_cut(view, cx, room);
            let agents = agents_section(view, cx, avail - needs.len(), false);
            (STEPS.len() as u8, false, needs, agents)
        }
    };
    // The section gaps the step keeps. (No gap above NEEDS YOU when nothing
    // is on top, none above an empty section.)
    let mut gap_needs = gaps && !top.is_empty();
    let mut gap_agents = gaps && !agents.is_empty();
    // The dock takes the rows left over (the richest form that fits: its
    // spaces are content); what it leaves buys spacing back, never detail:
    // the agents' shape (and so the ladder's steps) is already settled.
    // Never a gap between cards: they stack, in the zebra.
    let used = |n: &[Row], a: &[Row], g: [bool; 2]| top.len() + n.len() + a.len() + g.iter().filter(|x| **x).count();
    let room = h.saturating_sub(used(&needs, &agents, [gap_needs, gap_agents]));
    let dock = bottom(view, w, room, bar.prefix, opts.msg.as_deref());
    let mut spare = room.saturating_sub(dock.len());
    // 1. The gap above each section header: AGENTS, NEEDS YOU.
    for (gap, there) in [(&mut gap_agents, !agents.is_empty()), (&mut gap_needs, !top.is_empty())] {
        if there && !*gap && spare > 0 {
            *gap = true;
            spare -= 1;
        }
    }
    // 2. A gap above each space header in AGENTS (but the first): all or none.
    // (The last use of spare rows: PARKED never takes any, it is always its
    // one line.)
    let missing = group_gaps(&agents, w);
    if missing > 0 && missing <= spare {
        agents = spaced_groups(agents, w);
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
    let pinned: Vec<usize> = frame.agents.clone().filter(|&y| rows[y].pinned).collect();
    frame.pinned = pinned.first().zip(pinned.last()).map_or(at..at, |(a, b)| *a..b + 1);
    // Spare rows collect here (what spacing could not use); the dock sits
    // at the bottom.
    while rows.len() + dock.len() < h {
        rows.push(Row::blank(w));
    }
    let Dock { head, spaces, tail } = dock;
    let at = rows.len();
    rows.extend(head);
    frame.dock = at.min(h)..h;
    frame.spaces = (rows.len() - spaces).min(h)..rows.len().min(h);
    let at = rows.len();
    rows.extend(tail);
    rows.truncate(h);
    frame.parked = at.min(rows.len())..(at + 1).min(rows.len());
    frame.rows = rows;
    frame.step = step;
    frame.agents_step = agents_step;
    keep
}

/// Draw a frame into a ratatui buffer: each row's background (the
/// sidebar's own canvas, [`palette::CANVAS`], unless the row is a tile)
/// across the whole line, then its segments at the cells the layout
/// counted (a segment never spills into the next, whatever width ratatui
/// gives a grapheme). No cell is ever left the terminal's default.
pub fn paint(frame: &Frame, buf: &mut Buffer) {
    let area = buf.area;
    let canvas = Ink::Canvas.color();
    buf.set_style(area, Style::default().fg(palette::color("text")).bg(canvas));
    for (y, row) in frame.rows.iter().enumerate().take(usize::from(area.height)) {
        let y = area.y + y as u16;
        let row_bg = row.bg.map_or(canvas, Ink::color);
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
