//! The sidebar's pure half: a [`ViewModel`] and a size in, every row to draw
//! (each exactly `width` cells, with what a click on each cell does) out. No
//! I/O, so every density rule is unit-tested.
//!
//! ## Shape: NEEDS YOU, then agents (the body), then spaces
//!
//! ```text
//!  space               ◉1 ◐2 ○8      header + counts (the title flips the scope)
//!  ⏵ next ⌥S  ⤺ back ⌥X  ≡ ⌥W       toolbar (Actions next/back, the menu)
//!
//!  needs you                    1    agent-jump.sh `list` order, cards
//! ▌ ◉ Kua Yu focus timing     44m
//! ▌    asks Which deck should…
//! ▌    main:3 · ✳ claude    ⏎ go
//!
//!  agents                this space  the main body
//!  main ⌥ main ──────── ◉1 ◐2 ○4     one rule per space with agents
//!  1 ◉ Kua Yu focus timing           each agent: number, title …
//!      asks Which deck should… 44m   … and what it does / waits on
//!                                    (spare rows)
//!  spaces                            THE list of spaces (the status bar has
//!  ⌃1 ● main         ◉1 ◐2 ○4        none): ⌃N, name, counts, @space_index
//!  ⌃2 ○ bai                ○2        order; anchored above the last row
//!  ⏸ 5 parked                        the stash (click: prefix h's picker);
//! ```                                a watcher problem in red
//!
//! There is no event log and no divider any more: NEEDS YOU takes the rows
//! its cards need, the agents section starts right under it, and the spaces
//! list sits at the bottom, so spare rows collect between agents and spaces.
//!
//! ## Density ladder: agents lose detail last; never scroll, never drop an agent silently
//!
//! The agents section is the body. A frame takes the first step at which
//! every agent still gets its full two lines ([`STEPS`]):
//!
//! 1. the spaces list gets the rows left over: all of it, else FOLDED to
//!    the client's space and those with an attention agent, `+K more` on
//!    its header. The folded form ([`spaces_min`]) is held back before
//!    anything else is laid out, so the section never disappears (above 6
//!    rows) and agents lose detail before it does;
//! 2. the blank gaps between sections go;
//! 3. NEEDS YOU cards go from 3 lines to 2, then to 1;
//! 4. only then do the agents compress (below), NEEDS YOU kept to the rows
//!    the agents' one-line shape leaves (at least 2, and at least a third of
//!    the body), cut with a `… N more need you` line.
//!
//! The agents' own ladder: every agent to one line (age on the same line),
//! rows left over going back to second lines for the agents that matter most
//! (attention, then in flight); then the idle agents of a space fold into one
//! `○○○ 3 idle · Name, Name…` line; then whole spaces collapse to their rule
//! line (with state-coloured counts), fewest attention agents first; finally
//! a `… N more` line.
//!
//! Every agents-section row records the agents it stands for
//! ([`Row::covers`]), so a test can prove each agent is shown, folded,
//! collapsed or counted.
//!
//! ## Numbers
//!
//! Every agent row and NEEDS YOU card starts with the agent's sticky slot
//! ([`crate::view::assign_slots`]), zero-padded once any slot reaches 10.
//! Display only: the sidebar takes no number keys, and the Option-W menu
//! uses per-agent letters instead. Rows of agents without one (idle, the client's
//! plain window) leave the column blank, so titles stay aligned; with no
//! slot anywhere the column is not drawn at all. The folded idle line has
//! no numbers (idle agents hold none), and collapsed rules and `… N more`
//! stand for many agents, so neither does.
//!
//! ## Scope
//!
//! [`crate::view::Scope`] (`@spaces` / `@strip_scope`): in SPACE scope the
//! header reads ` space` with that session's counts, the agents section shows
//! only the client's session (its rule and ` no agents in this space` when
//! it has none), and its rows carry AGENT ORDINALS instead of slots: "1".."9"
//! by window index among the session's agents, blank past 9, dropped when
//! the title would keep fewer than 4 cells. NEEDS YOU (unnumbered there),
//! the spaces list and parked stay global. The header title is the toggle
//! once the scope is a thing.
//!
//! ## Colour
//!
//! No row has a background of its own: every cell is the terminal's default
//! background ([`Color::Reset`]), so the split is WezTerm's own colour. The
//! only tints left mark where the client is: the agent row of its window
//! (`sel`) and its space's row in the spaces list (`surface0`).

use crate::actions::watcher_problem;
use crate::hotkeys::slot_label;
use crate::model::{Cat, Counts, Flag};
use crate::palette;
use crate::text::{clip, dwidth, fit_label, plain, sanitize};
use crate::view::{tilde, Agent, Need, Space, ViewModel};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use std::cmp::Reverse;
use std::ops::Range;
use unicode_width::UnicodeWidthStr;

/// A palette colour name ([`palette::HEX`] or [`palette::UI`]).
pub type Hue = &'static str;

/// A run of text in one style.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Seg {
    pub text: String,
    pub fg: Hue,
    /// None: the row's background.
    pub bg: Option<Hue>,
    pub bold: bool,
}

fn seg(text: impl Into<String>, fg: Hue) -> Seg {
    Seg { text: text.into(), fg, bg: None, bold: false }
}

fn bold(text: impl Into<String>, fg: Hue) -> Seg {
    Seg { bold: true, ..seg(text, fg) }
}

fn width(segs: &[Seg]) -> usize {
    segs.iter().map(|s| dwidth(&s.text)).sum()
}

/// The mockup's `clip()`: at most `room` cells; the segment that does not fit
/// is cut with `…` ([`clip`]) and nothing after it is kept.
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

/// The mockup's `L()`: one line exactly `w` cells wide, `left` clipped to
/// leave room for `right`, the gap filled with `fill` (a space by default).
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
    /// A space row or rule: that session's active window (Strip.click "session").
    Session(String),
    /// The toolbar: agent-jump.sh next / back.
    Next,
    Back,
    /// The Option-W menu (Strip.open_menu), optionally on a tab ("all", "idle").
    Menu(Option<&'static str>),
    /// The `⏸ N parked` line: the stash picker, exactly prefix h's
    /// `stash.sh unstash '' <client>`.
    Stash,
    /// The red watcher warning: restart it (the strip's ⟳, Strip.click "restart").
    Restart,
    /// The header title: flip the scope (space / all) and persist it in
    /// `@strip_scope` (Strip.toggle_scope).
    Scope,
}

/// One screen line.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Row {
    /// Exactly the frame's width in cells.
    pub segs: Vec<Seg>,
    /// The row's background tint (None: the terminal's own background).
    pub bg: Option<Hue>,
    /// Click targets, `[x0, x1)` in 0-based cells (Strip.targets).
    pub targets: Vec<(u16, u16, Target)>,
    /// The agents, as (session, window id), this row stands for in the
    /// agents section: shown, folded, collapsed into a rule, or counted.
    pub covers: Vec<(String, String)>,
}

impl Row {
    fn new(segs: Vec<Seg>) -> Row {
        Row { segs, ..Row::default() }
    }

    fn blank(w: usize) -> Row {
        Row::new(vec![seg(" ".repeat(w), "text")])
    }

    fn bg(mut self, bg: Option<Hue>) -> Row {
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
    /// The ladder step the frame settled on: 0..=3 an entry of [`STEPS`]
    /// (agents at full detail), 4 the agents compressed.
    pub step: u8,
    /// The agents section's own ladder step (0 = two lines each).
    pub agents_step: u8,
    /// The rows of each section, header included (empty: not drawn).
    pub needs: Range<usize>,
    pub agents: Range<usize>,
    pub spaces: Range<usize>,
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
}

/// A state's colour in the sidebar: CAT_HUE, except working is the steady
/// blue of the mockup (the live marks pulse; counts do not).
pub fn cat_hue(c: Cat) -> Hue {
    match c {
        Cat::Failed => "red",
        Cat::NeedsInput => "yellow",
        Cat::Done => "green",
        Cat::Working => "blue",
        Cat::Idle => "overlay",
    }
}

/// agent-roster.py `count_tokens`: `✕1 ◉2 ◐4`, nonzero only, each in its
/// state colour (or all overlay).
fn count_segs(c: &Counts, coloured: bool) -> Vec<Seg> {
    let mut v = Vec::new();
    for (i, (k, n)) in c.nonzero().into_iter().enumerate() {
        if i > 0 {
            v.push(seg(" ", "text"));
        }
        v.push(seg(format!("{}{n}", k.glyph()), if coloured { cat_hue(k) } else { "overlay" }));
    }
    v
}

/// A detail / reason word's colour: asks and perm yellow, fail red, done
/// green, run blue.
fn word_hue(word: &str, fallback: Hue) -> Hue {
    match word {
        "perm" | "asks" | "ask" | "waiting on you" => "yellow",
        "fail" | "failed" => "red",
        "done" => "green",
        "run" => "blue",
        _ => fallback,
    }
}

fn section(w: usize, name: &str, hue: Hue, right: Vec<Seg>) -> Row {
    Row::new(fit(w, vec![bold(format!(" {name}"), hue)], right, None))
}

fn dim_line(w: usize, text: &str) -> Row {
    Row::new(fit(w, vec![seg(text, "overlay")], vec![], None))
}

/// Row 0: ` agents` and the per-state counts over every shown space; in
/// space scope ` space` and that session's counts. The title is the scope
/// toggle ([`Target::Scope`]) once the scope is a thing
/// ([`crate::view::Scope::toggles`]).
fn header_row(view: &ViewModel, w: usize) -> Row {
    let (title, counts) = match view.scope.session {
        Some(_) => (" space", &view.scope.counts),
        None => (" agents", &view.counts),
    };
    let mut right = count_segs(counts, true);
    right.push(seg(" ", "text"));
    let mut row = Row::new(fit(w, vec![bold(title, "text")], right, None));
    let end = dwidth(title).min(w) as u16;
    if view.scope.toggles() && end > 1 {
        row.targets.push((1, end, Target::Scope));
    }
    row
}

/// Row 1: the three buttons (⌥ hints dropped when they do not fit).
fn toolbar_row(w: usize) -> Row {
    let full = [" ⏵ next ⌥S ", " ⤺ back ⌥X ", " ≡ ⌥W "];
    let short = [" ⏵ next ", " ⤺ back ", " ≡ "];
    let need: usize = full.iter().map(|l| dwidth(l) + 1).sum();
    let labels = if need <= w { full } else { short };
    let acts = [Target::Next, Target::Back, Target::Menu(None)];
    let mut segs = Vec::new();
    let mut targets = Vec::new();
    let mut x = 0;
    for (label, t) in labels.into_iter().zip(acts) {
        let lw = dwidth(label);
        if x + 1 + lw > w {
            break;
        }
        segs.push(seg(" ", "text"));
        segs.push(seg(label, "sub"));
        targets.push(((x + 1) as u16, (x + 1 + lw) as u16, t));
        x += 1 + lw;
    }
    Row { targets, ..Row::new(fit(w, segs, vec![], None)) }
}

/// Which number a row's number column shows, and how wide it is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Num {
    /// The label's width in cells; 0: no column at all.
    w: usize,
    /// Space scope: the agent's [`Agent::ordinal`] ("1".."9", blank past 9),
    /// not its sticky slot.
    ordinal: bool,
}

impl Num {
    const NONE: Num = Num { w: 0, ordinal: false };
    /// Space scope's column: one digit.
    const ORDINAL: Num = Num { w: 1, ordinal: true };

    /// The classic column: slots, zero-padded to `sw` ([`ViewModel::slot_width`]).
    fn slots(sw: usize) -> Num {
        Num { w: sw, ordinal: false }
    }
}

/// THE agent label: what an agent row's (and card's) label column shows,
/// at most `n.w` cells. Today the 1-9 ordinal (space scope) or the sticky
/// slot. One swappable field: to show the ⌥W menu's lifetime letter
/// instead, return the window's `@agent_key` stamp here (`Agent` would carry
/// it from `Window::key`, read through `menu::letters::parse_key`; the
/// sidebar only reads it, the menu owns the writes) and make `Num.w` 1.
fn agent_label(a: &Agent, n: Num) -> Option<String> {
    if n.ordinal {
        a.ordinal.filter(|&o| o <= 9).map(|o| o.to_string())
    } else {
        a.slot.map(|s| slot_label(s, n.w))
    }
}

/// The number column of an agent's first line: its slot label (zero-padded
/// to `n.w`) or, in space scope, its ordinal, and a space; blank when it has
/// none; None when there is no column (`n.w` 0). Display only: the sidebar
/// has no number keys (the ordinals are tmux's M-1..M-9).
fn num_seg(a: &Agent, n: Num) -> Option<Seg> {
    (n.w > 0).then(|| match agent_label(a, n) {
        Some(l) => seg(format!("{l} "), "sub"),
        None => seg(" ".repeat(n.w + 1), "text"),
    })
}

/// The cells [`num_seg`] takes: what the lines under it indent by.
fn num_w(n: Num) -> usize {
    if n.w > 0 {
        n.w + 1
    } else {
        0
    }
}

/// A NEEDS YOU card: 3, 2 or 1 lines, a left bar in the state colour
/// (Strip.need_detail's reason on line 2), the agent's number first.
fn need_rows(n: &Need, w: usize, lines: u8, sw: Num) -> Vec<Row> {
    let a = &n.agent;
    let hue = a.color.unwrap_or("overlay");
    let go = Target::Go { win: a.window_id.clone(), session: a.session.clone() };
    let card = |segs: Vec<Seg>| Row::new(segs).click(w, go.clone());
    let bar = || seg("▌", hue);
    let indent = " ".repeat(num_w(sw));
    let age = if a.age.is_empty() { String::new() } else { format!("{} ", a.age) };
    let (proj, title) = fit_label(no_home(&a.title), w.saturating_sub(3 + num_w(sw) + dwidth(&age)));
    let mut first = vec![bar()];
    first.extend(num_seg(a, sw));
    first.extend([seg(format!("{} ", a.glyph), hue), seg(proj, "overlay"), bold(title, "text")]);
    let mut rows = vec![card(fit(w, first, vec![seg(age, "sub")], None))];
    if lines >= 2 {
        rows.push(card(fit(
            w,
            vec![
                bar(),
                seg(format!("{indent}  {} ", n.reason_word), word_hue(&n.reason_word, hue)),
                seg(plain(&n.reason), "sub"),
            ],
            vec![],
            None,
        )));
    }
    if lines >= 3 {
        let kind = match a.kind_glyph {
            Some(g) => format!(" · {g} {}", sanitize(&a.kind)),
            None => String::new(),
        };
        rows.push(card(fit(
            w,
            vec![bar(), seg(format!("{indent}  {}{kind}", sanitize(&a.place())), "overlay")],
            vec![seg("⏎ go ", "blue")],
            None,
        )));
    }
    rows
}

fn needs_header(view: &ViewModel, w: usize) -> Row {
    let n = view.needs.len();
    let right = if n > 0 { vec![seg(format!("{n} "), "yellow")] } else { vec![] };
    section(w, "needs you", "yellow", right)
}

/// The whole NEEDS YOU section. Never empty: with nothing pending it says so.
/// Its cards carry slots, except in space scope, where numbers are ordinals
/// of one session and NEEDS YOU is global: no numbers there.
fn needs_block(view: &ViewModel, w: usize, lines: u8) -> Vec<Row> {
    let mut rows = vec![needs_header(view, w)];
    if view.needs.is_empty() {
        rows.push(dim_line(w, " ✓ nothing needs you"));
    }
    let num = if view.scope.session.is_some() { Num::NONE } else { Num::slots(view.slot_width) };
    for n in &view.needs {
        rows.extend(need_rows(n, w, lines, num));
    }
    rows
}

/// NEEDS YOU in at most `room` rows: 1-line cards, the rest counted.
fn needs_cut(view: &ViewModel, w: usize, room: usize) -> Vec<Row> {
    let n = view.needs.len();
    let more = |k: usize, words: &str| {
        Row::new(fit(w, vec![seg(format!(" … {k} {words}"), "yellow")], vec![], None)).click(w, Target::Menu(None))
    };
    let mut rows = needs_block(view, w, 1);
    if rows.len() <= room {
        return rows;
    }
    if n == 0 {
        // Nothing pending: the reassurance, never ` … 0 need you`.
        return rows.split_off(1).into_iter().take(room).collect();
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

/// THE space label: what a space row's label column shows, at most
/// [`SPACE_LABEL_W`] cells. Today `⌃N` for `@space_index` 1-9 (Ctrl+N
/// switches to it), None past 9 or without an index. One swappable field:
/// a Shift+letter scheme changes only this (and the width).
fn space_label(sp: &Space) -> Option<String> {
    sp.index.filter(|n| (1..=9).contains(n)).map(|n| format!("⌃{n}"))
}

/// The cells [`space_label`] takes (its column is drawn only when some
/// listed space has a label).
const SPACE_LABEL_W: usize = 2;

/// The spaces the list shows, in `@space_index` order (spaces without one
/// after, by name): every session the view holds except `@space_hidden`
/// ones. The status bar no longer lists spaces, so this is where they are.
fn space_list(view: &ViewModel) -> Vec<&Space> {
    let mut v: Vec<&Space> = view.spaces.iter().filter(|s| !s.hidden).collect();
    v.sort_by_key(|s| (s.index.is_none(), s.index)); // stable: name order within
    v
}

/// The label column's width for this list (0: no space has a label).
fn space_label_w(list: &[&Space]) -> usize {
    if list.iter().any(|s| space_label(s).is_some()) {
        SPACE_LABEL_W
    } else {
        0
    }
}

/// A space has an agent needing attention (failed, needs you, done).
fn space_alert(sp: &Space) -> bool {
    sp.agents.iter().any(|a| a.attention)
}

/// A space, one line: its label (`⌃N`), `●`/`○`, name and counts. The
/// client's one on `surface0` (where you are); one with no agents dim. A
/// click switches the client to it (its active window).
fn space_row(sp: &Space, w: usize, lw: usize) -> Row {
    let empty = sp.counts.total() == 0 && !sp.is_current;
    let name = sanitize(&sp.name);
    let mut left = vec![seg(" ", "text")];
    if lw > 0 {
        let label = space_label(sp).unwrap_or_default();
        let pad = " ".repeat(lw.saturating_sub(dwidth(&label)) + 1);
        left.push(seg(format!("{label}{pad}"), if sp.is_current { "blue" } else { "overlay" }));
    }
    let (mark, name) = if sp.is_current {
        (seg("● ", "blue"), bold(name, "text"))
    } else if empty {
        (seg("○ ", "overlay"), seg(name, "overlay"))
    } else {
        (seg("○ ", "overlay"), seg(name, "sub"))
    };
    left.extend([mark, name]);
    let mut right = count_segs(&sp.counts, true);
    right.push(seg(" ", "text"));
    Row::new(fit(w, left, right, None))
        .bg(sp.is_current.then_some("surface0"))
        .click(w, Target::Session(sp.name.clone()))
}

/// The section's header: ` spaces`, and `+K more` on the right when K
/// spaces are folded away (a click: the menu's All tab).
fn spaces_header(w: usize, more: usize) -> Row {
    if more == 0 {
        return section(w, "spaces", "sub", vec![]);
    }
    section(w, "spaces", "sub", vec![seg(format!("+{more} more "), "overlay")]).click(w, Target::Menu(Some("all")))
}

/// SPACES in exactly `room` rows (`room` >= 2) when it does not fit whole:
/// the header carries `+K more`, the rows kept are the client's space, then
/// those with an agent needing attention, then `@space_index` order; shown
/// in list order.
fn spaces_cut(view: &ViewModel, w: usize, room: usize) -> Vec<Row> {
    let list = space_list(view);
    let lw = space_label_w(&list);
    let n = list.len();
    if n < room {
        let mut rows = vec![spaces_header(w, 0)];
        rows.extend(list.iter().map(|sp| space_row(sp, w, lw)));
        return rows;
    }
    let keep = room.saturating_sub(1).min(n);
    let mut ranked: Vec<usize> = (0..n).collect();
    ranked.sort_by_key(|&i| (!list[i].is_current, !space_alert(list[i])));
    let mut kept = ranked[..keep].to_vec();
    kept.sort_unstable();
    let mut rows = vec![spaces_header(w, n - keep)];
    rows.extend(kept.iter().map(|&i| space_row(list[i], w, lw)));
    rows
}

fn spaces_block(view: &ViewModel, w: usize) -> Vec<Row> {
    spaces_cut(view, w, usize::MAX)
}

/// The rows the spaces list keeps however short the frame (reserved before
/// anything else gives up a row, agents included): its header (with
/// `+K more`) and the client's space plus every space with an attention
/// agent, at least one space, at most `max(2, avail / 4)`. 0 with no
/// spaces, or when `avail` leaves NEEDS YOU and the agents under 2 rows.
fn spaces_min(view: &ViewModel, avail: usize) -> usize {
    let list = space_list(view);
    if list.is_empty() {
        return 0;
    }
    let want = list.iter().filter(|s| s.is_current || space_alert(s)).count();
    let rows = 1 + want.max(1).min(list.len()).min((avail / 4).max(2));
    // Short of room: fewer kept spaces (the client's first), down to one.
    let rows = rows.min(avail.saturating_sub(2));
    if rows >= 2 {
        rows
    } else {
        0
    }
}

/// The spaces list in the `room` rows the sections above left: with a gap
/// before it if `gap` and it fits, whole if it fits, else folded (2+ rows),
/// else not at all → (a gap row leads, rows).
fn spaces_fit(view: &ViewModel, w: usize, room: usize, gap: bool) -> (bool, Vec<Row>) {
    if space_list(view).is_empty() {
        return (false, vec![]);
    }
    let full = spaces_block(view, w);
    let gaps: &[usize] = if gap { &[1, 0] } else { &[0] };
    for &g in gaps {
        if room >= g + full.len() {
            return (g == 1, with_gap(g, w, full));
        }
    }
    // Folded, every row is a space: no gap.
    if room >= 2 {
        return (false, spaces_cut(view, w, room));
    }
    (false, vec![])
}

fn with_gap(g: usize, w: usize, rows: Vec<Row>) -> Vec<Row> {
    let mut out: Vec<Row> = (0..g).map(|_| Row::blank(w)).collect();
    out.extend(rows);
    out
}

/// The steps at which agents keep full detail: (blank gaps, NEEDS YOU card
/// lines). The spaces list takes what each leaves over.
pub const STEPS: [(bool, u8); 4] = [(true, 3), (false, 3), (false, 2), (false, 1)];

/// A title without a bare `~/` "project" (a summary made in $HOME).
fn no_home(title: &str) -> &str {
    title.strip_prefix("~/").unwrap_or(title)
}

/// The title under its space's rule: the `project/` prefix dropped (the
/// space already says where it is).
fn short_title(a: &Agent) -> String {
    sanitize(if a.short_title.is_empty() { no_home(&a.title) } else { &a.short_title })
}

/// One agent: 2 lines (title / what it does + age) or 1 (title + age),
/// its number first ([`num_seg`]). The window the client is on gets the
/// `sel` background and `here`. Line 2 of a working agent is its run detail
/// or `working` + path; of any other, its last outcome (`done …`, `fail …`)
/// or its path.
fn agent_rows(a: &Agent, w: usize, two: bool, sw: Num) -> Vec<Row> {
    let hue = a.color.unwrap_or("overlay");
    let quiet = matches!(a.cat, None | Some(Cat::Idle));
    let go = Target::Go { win: a.window_id.clone(), session: a.session.clone() };
    let bg = a.is_current.then_some("sel");
    let flag = a.flag.map(|(f, c)| seg(if f == Flag::Workflow { " ⚙" } else { " ◎" }, c));
    let age = if a.age.is_empty() { " ".to_string() } else { format!(" {} ", a.age) };
    let right = if !two {
        vec![seg(age.clone(), "overlay")]
    } else if a.is_current {
        vec![seg("here ", "blue")]
    } else {
        vec![]
    };
    // The title gets every cell the glyph, the flag and the right side leave.
    let gap = usize::from(!right.is_empty() && !right[0].text.starts_with(' '));
    let fixed = 3 + flag.as_ref().map_or(0, |f| dwidth(&f.text)) + width(&right) + gap;
    // An ordinal only while the title keeps 4 cells (agent-roster.py
    // agent_content): on a very narrow sidebar the number goes, not the title.
    let sw = if sw.ordinal && w.saturating_sub(fixed + num_w(sw)) < 4 { Num::NONE } else { sw };
    let room = w.saturating_sub(fixed + num_w(sw));
    let title = clip(&short_title(a), room);
    let title = if quiet { seg(title, "sub") } else if two { bold(title, "text") } else { seg(title, "text") };
    // ` 3 ◉ Title`: the number in the space before the glyph's.
    let mut left = match num_seg(a, sw) {
        Some(n) => vec![seg(" ", "text"), n, seg(format!("{} ", a.glyph), hue), title],
        None => vec![seg(format!(" {} ", a.glyph), hue), title],
    };
    left.extend(flag);
    let first = Row::new(fit(w, left, right, None)).bg(bg).click(w, go.clone()).covering([a]);
    if !two {
        return vec![first];
    }
    let path = || seg(sanitize(&tilde(&a.path)), "overlay");
    // A detail that is nothing but markup (a truncated `<agent-message …>`)
    // counts as none.
    let detail = plain(&a.detail);
    let mut l2 = vec![seg(" ".repeat(3 + num_w(sw)), "text")];
    if a.in_flight {
        // Working: what it runs now, never the last turn's outcome.
        if a.detail_kind == "run" && !detail.is_empty() {
            l2.extend([seg("run ", "blue"), seg(detail, "sub")]);
        } else {
            l2.extend([seg("working ", "blue"), path()]);
        }
    } else if detail.is_empty() {
        l2.push(path());
    } else {
        if let Some(wd) = a.detail_word {
            l2.push(seg(format!("{wd} "), word_hue(wd, "sub")));
        }
        l2.push(seg(detail, "sub"));
    }
    let second = Row::new(fit(w, l2, vec![seg(age, "overlay")], None)).bg(bg).click(w, go);
    vec![first, second]
}

/// A space's rule: ` name ⌥ branch ───── counts `. Collapsed, it stands for
/// all its agents and its counts take their state colours.
fn rule_row(sp: &Space, w: usize, collapsed: bool) -> Row {
    let mut left = vec![bold(format!(" {} ", sanitize(&sp.name)), "text")];
    if !sp.branch.is_empty() {
        left.push(seg(format!("⌥ {} ", sanitize(&sp.branch)), if sp.is_current { "mauve" } else { "overlay" }));
    }
    let counts = count_segs(&sp.counts, collapsed);
    let right = if counts.is_empty() {
        vec![]
    } else {
        let mut r = vec![seg(" ", "text")];
        r.extend(counts);
        r.push(seg(" ", "text"));
        r
    };
    let row = Row::new(fit(w, left, right, Some(('─', "surface1")))).click(w, Target::Session(sp.name.clone()));
    if collapsed {
        row.covering(&sp.agents)
    } else {
        row
    }
}

/// `   ○○○ 3 idle · Name, Name…`: a space's idle agents in one line, its
/// circles under the titles above. No numbers: idle agents hold no slot.
fn fold_row(idle: &[&Agent], w: usize, sw: Num) -> Row {
    let k = idle.len();
    let names: Vec<String> = idle.iter().map(|a| short_title(a)).collect();
    let indent = " ".repeat(3 + num_w(sw));
    Row::new(fit(
        w,
        vec![seg(format!("{indent}{} {k} idle · ", "○".repeat(k.min(4))), "overlay"), seg(names.join(", "), "overlay")],
        vec![],
        None,
    ))
    // The menu's Idle tab: just the idle agents.
    .click(w, Target::Menu(Some("idle")))
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
/// whose prompt was just answered: it is still Active and shows its number)
/// go into one line, when there are 2+ of them → (shown in place, folded).
fn split_idle(sp: &Space, fold: bool) -> (Vec<&Agent>, Vec<&Agent>) {
    let foldable = |a: &Agent| fold && a.cat == Some(Cat::Idle) && !a.is_current && !a.answered;
    let (idle, shown): (Vec<&Agent>, Vec<&Agent>) = sp.agents.iter().partition(|a| foldable(a));
    if idle.len() >= 2 {
        (shown, idle)
    } else {
        (sp.agents.iter().collect(), vec![])
    }
}

/// A space's agent rows in window-index order (the folded line last); the
/// agents in `two` get their second line.
fn group_rows(sp: &Space, w: usize, two: &[Key], fold: bool, sw: Num) -> Vec<Row> {
    let (shown, idle) = split_idle(sp, fold);
    let mut rows: Vec<Row> = shown.iter().flat_map(|a| agent_rows(a, w, two.contains(&key(a)), sw)).collect();
    if !idle.is_empty() {
        rows.push(fold_row(&idle, w, sw));
    }
    rows
}

/// A space's agents in `room` rows: all of them one line each if they fit,
/// else the `room - 1` that matter most ([`promotion_rank`], kept in list
/// order) and a `… N more` line for the rest.
fn partial_rows(sp: &Space, w: usize, room: usize, sw: Num) -> Vec<Row> {
    if sp.agents.len() <= room {
        return sp.agents.iter().flat_map(|a| agent_rows(a, w, false, sw)).collect();
    }
    let mut ranked: Vec<usize> = (0..sp.agents.len()).collect();
    ranked.sort_by_key(|&k| promotion_rank(&sp.agents[k]));
    let mut keep = ranked[..room - 1].to_vec();
    keep.sort_unstable();
    let mut rows: Vec<Row> = keep.iter().flat_map(|&k| agent_rows(&sp.agents[k], w, false, sw)).collect();
    let rest: Vec<&Agent> = (0..sp.agents.len()).filter(|k| !keep.contains(k)).map(|k| &sp.agents[k]).collect();
    rows.push(more_row(w, &rest, 1));
    rows
}

/// Which one-line agents get their second line back first, when a
/// compressed step leaves rows over: attention, then in flight, then the
/// client's window, then any with something to say; ties in list order.
fn promotion_rank(a: &Agent) -> u8 {
    if a.attention {
        0
    } else if a.in_flight {
        1
    } else if a.is_current {
        2
    } else if !a.detail.is_empty() {
        3
    } else {
        4
    }
}

/// The spaces the agents section draws: those with agents; in space scope,
/// just the client's session, agents or not.
fn groups(view: &ViewModel) -> Vec<&Space> {
    match &view.scope.session {
        Some(s) => view.spaces.iter().filter(|sp| &sp.name == s).collect(),
        None => view.spaces.iter().filter(|s| !s.agents.is_empty()).collect(),
    }
}

/// Whether the frame shows a colour that pulses with `@agent_blink`: an
/// agent in flight (not the client's own window) or a workflow / cua flag
/// in the agents section. Only then does the sidebar need the fast tick.
pub fn pulses(view: &ViewModel) -> bool {
    groups(view).iter().flat_map(|s| s.agents.iter()).any(|a| (a.in_flight && !a.is_current) || a.flag.is_some())
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

/// The agents section (header, then each group's rule and rows), at most
/// `budget` rows → (rows, its ladder step).
///
/// Steps 1..=3 are a SHAPE (one-line rows; idle folded; spaces collapsed);
/// rows a shape leaves over go back to second lines for the agents that
/// matter most ([`promotion_rank`]), so a section that just misses two lines
/// each is not left mostly empty.
fn agents_section(view: &ViewModel, w: usize, budget: usize) -> (Vec<Row>, u8) {
    if budget == 0 {
        return (vec![], 0);
    }
    let groups = groups(view);
    let space = view.scope.session.is_some();
    let sw = if space { Num::ORDINAL } else { Num::slots(view.slot_width) };
    let header = section(w, "agents", "sub", vec![seg(if space { "this space " } else { "by space " }, "overlay")]);
    if space && groups.iter().all(|g| g.agents.is_empty()) {
        // The space's rule even with no agent in it, so the section says whose it is.
        let mut rows = vec![header];
        rows.extend(groups.iter().map(|g| rule_row(g, w, false)));
        rows.push(dim_line(w, " no agents in this space"));
        rows.truncate(budget);
        return (rows, 0);
    }
    if groups.is_empty() {
        let mut rows = vec![header, dim_line(w, " no agents running")];
        rows.truncate(budget);
        return (rows, 0);
    }
    let build = |two: &[Key], fold: bool, collapsed: &[bool]| {
        let mut rows = vec![header.clone()];
        for (sp, &c) in groups.iter().zip(collapsed) {
            rows.push(rule_row(sp, w, c));
            if !c {
                rows.extend(group_rows(sp, w, two, fold, sw));
            }
        }
        rows
    };
    // A shape that fits, with its spare rows handed out as second lines.
    let promoted = |fold: bool, collapsed: &[bool]| {
        let base = build(&[], fold, collapsed);
        if base.len() > budget {
            return None;
        }
        let mut cands: Vec<&Agent> = groups
            .iter()
            .zip(collapsed)
            .filter(|(_, &c)| !c)
            .flat_map(|(sp, _)| split_idle(sp, fold).0)
            .collect();
        cands.sort_by_key(|a| promotion_rank(a)); // stable: list order within a rank
        let two: Vec<Key> = cands.into_iter().take(budget - base.len()).map(key).collect();
        Some(build(&two, fold, collapsed))
    };
    let mut collapsed = vec![false; groups.len()];
    let all: Vec<Key> = groups.iter().flat_map(|g| g.agents.iter().map(key)).collect();
    let rows = build(&all, false, &collapsed);
    if rows.len() <= budget {
        return (rows, 0);
    }
    for (step, fold) in [(1, false), (2, true)] {
        if let Some(rows) = promoted(fold, &collapsed) {
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
        let base = build(&[], true, &collapsed);
        if base.len() > budget {
            continue;
        }
        let spare = budget - base.len();
        let at = 1 + (0..i)
            .map(|j| 1 + if collapsed[j] { 0 } else { group_rows(groups[j], w, &[], true, sw).len() })
            .sum::<usize>();
        if spare < 2 {
            let rows = promoted(true, &collapsed).unwrap_or_else(|| base.clone());
            if spare == 0 || rows.len() > base.len() {
                return (rows, 3);
            }
            // One row and nothing to promote: the collapsed space's most
            // important agent under its rule, which still counts the rest.
            let g = groups[i];
            let Some(top) = (0..g.agents.len()).min_by_key(|&k| (promotion_rank(&g.agents[k]), k)) else {
                return (rows, 3);
            };
            let mut rows = base;
            let gone = key(&g.agents[top]);
            rows[at].covers.retain(|(s, id)| (s.as_str(), id.as_str()) != gone);
            rows.insert(at + 1, agent_rows(&g.agents[top], w, false, sw).remove(0));
            return (rows, 3);
        }
        // The space whose collapse made it fit keeps the rows that frees:
        // its most important agents, one line each, and `… N more`.
        let mut rows = base;
        rows[at] = rule_row(groups[i], w, false);
        rows.splice(at + 1..at + 1, partial_rows(groups[i], w, spare, sw));
        return (rows, 3);
    }
    // More spaces than rows: the first ones as rules, the rest counted.
    if budget == 1 {
        let all: Vec<&Agent> = groups.iter().flat_map(|g| g.agents.iter()).collect();
        return (vec![more_row(w, &all, groups.len())], 4);
    }
    let keep = budget - 2;
    let mut rows = build(&[], true, &collapsed);
    rows.truncate(1 + keep);
    let rest: Vec<&Agent> = groups[keep..].iter().flat_map(|g| g.agents.iter()).collect();
    rows.push(more_row(w, &rest, groups.len() - keep));
    (rows, 4)
}

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

/// The whole screen: header, toolbar, NEEDS YOU, agents, spare rows, spaces,
/// last row. Always exactly `height` rows of exactly `width` cells.
pub fn layout(view: &ViewModel, width: u16, height: u16, opts: &Opts) -> Frame {
    let (w, h) = (usize::from(width), usize::from(height));
    let mut frame = Frame { width, height, ..Frame::default() };
    if w == 0 || h == 0 {
        return frame;
    }
    let last = last_row(view, w, opts.msg.as_deref());
    // The rows between the toolbar and the last row.
    let avail = h.saturating_sub(3);
    if avail == 0 {
        // No room for sections: one row still counts every agent of the
        // scope (in place of the toolbar), then the parked row.
        let all: Vec<&Agent> = groups(view).into_iter().flat_map(|s| s.agents.iter()).collect();
        let mut rows = vec![header_row(view, w)];
        rows.push(if all.is_empty() { toolbar_row(w) } else { more_row(w, &all, 0) });
        rows.push(last);
        rows.truncate(h);
        frame.rows = rows;
        frame.step = STEPS.len() as u8;
        return frame;
    }
    // The folded spaces list is held back first: spaces are listed nowhere
    // else, so the section never goes before the agents lose detail.
    let avail = avail - spaces_min(view, avail);
    let full = agents_section(view, w, usize::MAX).0;
    let fits = STEPS.iter().enumerate().find_map(|(i, &(gaps, lines))| {
        let needs = needs_block(view, w, lines);
        (needs.len() + full.len() + 2 * usize::from(gaps) <= avail).then_some((i as u8, gaps, needs))
    });
    let (step, gaps, needs, (agents, agents_step)) = match fits {
        Some((i, gaps, needs)) => (i, gaps, needs, (full, 0)),
        None => {
            // The agents compress. NEEDS YOU gets what their one-line shape
            // leaves (up to the whole, at least 2 rows and a third of the
            // body), and always leaves the agents a row.
            let n1 = needs_block(view, w, 1).len();
            let floor = (avail / 3).max(2);
            let room = n1.min(avail.saturating_sub(agents_one_line(view)).max(floor)).min(avail - 1);
            let needs = needs_cut(view, w, room);
            let agents = agents_section(view, w, avail - needs.len());
            (STEPS.len() as u8, false, needs, agents)
        }
    };
    let mut rows = vec![header_row(view, w), toolbar_row(w)];
    if gaps {
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
    // Spare rows collect here; the spaces list is anchored above the last row.
    let (gap, spaces) = spaces_fit(view, w, (h - 1).saturating_sub(rows.len()), gaps);
    while rows.len() + spaces.len() < h - 1 {
        rows.push(Row::blank(w));
    }
    let at = rows.len() + usize::from(gap);
    rows.extend(spaces);
    frame.spaces = at.min(rows.len())..rows.len();
    rows.push(last);
    rows.truncate(h);
    frame.rows = rows;
    frame.step = step;
    frame.agents_step = agents_step;
    frame
}

/// Draw a frame into a ratatui buffer: each row's background (the terminal's
/// own, [`Color::Reset`], unless the row is tinted) across the whole line,
/// then its segments at the cells the layout counted (a segment never spills
/// into the next, whatever width ratatui gives a grapheme).
pub fn paint(frame: &Frame, buf: &mut Buffer) {
    let area = buf.area;
    for (y, row) in frame.rows.iter().enumerate().take(usize::from(area.height)) {
        let y = area.y + y as u16;
        let row_bg = row.bg.map_or(Color::Reset, palette::color);
        buf.set_style(Rect::new(area.x, y, area.width, 1), Style::default().fg(palette::color("text")).bg(row_bg));
        let mut x = 0usize;
        for s in &row.segs {
            let sw = dwidth(&s.text);
            let room = usize::from(area.width).saturating_sub(x);
            if sw > 0 && room > 0 {
                let mut st = Style::default().fg(palette::color(s.fg)).bg(s.bg.map_or(row_bg, palette::color));
                if s.bold {
                    st = st.add_modifier(Modifier::BOLD);
                }
                buf.set_style(Rect::new(area.x + x as u16, y, sw.min(room) as u16, 1), st);
                buf.set_stringn(area.x + x as u16, y, &s.text, sw.min(room), st);
            }
            x += sw;
        }
    }
}

fn sgr(fg: Color, bg: Color, m: Modifier) -> String {
    let mut s = String::from("\x1b[0");
    if let Color::Rgb(r, g, b) = fg {
        s.push_str(&format!(";38;2;{r};{g};{b}"));
    }
    if let Color::Rgb(r, g, b) = bg {
        s.push_str(&format!(";48;2;{r};{g};{b}"));
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
mod tests {
    use super::*;
    use crate::collate::Collator;
    use crate::tmux::tests::row;
    use crate::tmux::Snapshot;
    use crate::view::BuildArgs;
    use std::collections::HashMap;

    const NOW: i64 = 1_000_000;
    const CLIENT: &str = "/dev/ttys999";

    /// One agent window: (session, index, state, title) plus options.
    struct A<'a> {
        s: &'a str,
        i: u32,
        st: &'a str,
        title: &'a str,
        set: Vec<(&'a str, String)>,
    }

    fn a<'a>(s: &'a str, i: u32, st: &'a str, title: &'a str) -> A<'a> {
        let since = format!("{} {st}", NOW - 60 * i64::from(i) - 30);
        let mut set = vec![("since", since), ("kind", "claude".into()), ("path", format!("/nonexistent/{s}"))];
        if i == 1 {
            set.push(("active", "1".into()));
        }
        A { s, i, st, title, set }
    }

    impl<'a> A<'a> {
        fn with(mut self, k: &'a str, v: &str) -> Self {
            self.set.push((k, v.to_string()));
            self
        }
    }

    /// Window ids are assigned in order; the client sits on `here` (an id).
    fn view(agents: Vec<A>, here: Option<&str>) -> ViewModel {
        view_with(agents, here, &mut crate::view::SlotMemo::default())
    }

    /// [`view`], one frame of a sidebar that remembers its slots in `memo`.
    fn view_with(agents: Vec<A>, here: Option<&str>, memo: &mut crate::view::SlotMemo) -> ViewModel {
        let mut rows: Vec<String> = agents
            .iter()
            .enumerate()
            .map(|(n, x)| {
                let id = format!("@{}", n + 1);
                let mut set: Vec<(&str, &str)> = vec![("state", x.st), ("summary", x.title)];
                set.extend(x.set.iter().map(|(k, v)| (*k, v.as_str())));
                row(x.s, x.i, &id, &set)
            })
            .collect();
        // Noise the sidebar must ignore: a hidden session, a parked tab, a plain shell.
        rows.push(row("agents", 1, "@900", &[("state", "failed"), ("summary", "hidden")]));
        rows.push(row("stash", 1, "@901", &[("summary", "x"), ("stash_label", "Parked one"), ("stash_ts", "5")]));
        rows.push(row("main", 99, "@902", &[]));
        if let Some(h) = here {
            rows.push(format!("\x1fclient\x1f{CLIENT}\x1f{h}\x1fmain"));
        }
        let c = Collator::new("en_US.UTF-8");
        let args = BuildArgs { client: Some(CLIENT), now: NOW as f64, collator: &c, git: None, log: vec![], watcher_age: Some(1.0) };
        ViewModel::build_with(&Snapshot::parse(&rows.join("\n")), args, memo)
    }

    /// Today: 11 agents, 1 need (the mockup's first frame).
    fn quiet() -> ViewModel {
        let agents = vec![
            a("main", 1, "running", "Handy.app Speech Commands").with("workflow", "1")
                .with("detail_kind", "run").with("detail", "Bash swift build -c release"),
            a("main", 2, "running", "Tmux Agent Sidebar").with("detail_kind", "run").with("detail", "Edit agent-roster.py"),
            a("main", 3, "needs-input", "Kua Yu focus timing").with("detail_kind", "ask")
                .with("detail", "Which deck should I pull the timing from?"),
            a("main", 4, "idle", "Commodities Job Tracker").with("detail_kind", "done").with("detail", "7 fixes applied"),
            a("main", 5, "idle", "Notch Tasks Orchestrator"),
            a("main", 6, "idle", "Lost suit jacket"),
            a("main", 7, "idle", "Backyard Halloween"),
            a("bai", 1, "idle", "Application link"),
            a("bai", 2, "idle", "BAI Info Session Slides"),
            a("schedule", 1, "idle", "P0a multi-user execution"),
            a("schedule", 2, "idle", "Prose extraction model"),
        ];
        view(agents, Some("@2"))
    }

    /// A busy day: 24 agents, 4 needs (the mockup's second frame).
    fn busy() -> ViewModel {
        view(busy_agents(), Some("@4"))
    }

    fn busy_agents() -> Vec<A<'static>> {
        let mut v = vec![
            a("main", 1, "failed", "Handy.app Speech Commands").with("workflow", "1"),
            a("main", 2, "needs-input", "Kua Yu focus timing").with("detail_kind", "ask").with("detail", "Which deck?"),
            a("main", 3, "done", "Commodities Job Tracker"),
            a("main", 4, "running", "Tmux Agent Sidebar"),
            a("main", 5, "running", "Notch Tasks Orchestrator"),
            a("main", 6, "idle", "Lost suit jacket"),
            a("cua-notch", 1, "needs-input", "Notch hover animation").with("detail_kind", "perm")
                .with("detail", "Bash git push origin main"),
            a("cua-notch", 2, "running", "Island resize spring"),
            a("cua-notch", 3, "running", "Census AX sweep").with("cua", "1"),
            a("cua-notch", 4, "running", "Invariants section 66"),
            a("cua-notch", 5, "idle", "Popup shadow"),
            a("schedule", 1, "running", "P0a multi-user execution"),
            a("schedule", 2, "running", "Prose extraction model"),
            a("schedule", 3, "idle", "Calendar sync"),
            a("schedule", 4, "idle", "ICS export"),
        ];
        for (s, n) in [("bai", 3), ("mackhaymond.co", 4), ("errands", 2)] {
            for i in 1..=n {
                v.push(a(s, i, "idle", "Some idle thing"));
            }
        }
        v
    }

    /// 90 agents, 12 needs, 9 spaces.
    fn extreme() -> ViewModel {
        const NAMES: [&str; 9] = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india"];
        let mut v = Vec::new();
        let mut needs = 0;
        for s in NAMES {
            for i in 1..=10u32 {
                let st = if needs < 12 && i % 4 == 0 {
                    needs += 1;
                    "needs-input"
                } else if i % 3 == 0 {
                    "running"
                } else {
                    "idle"
                };
                v.push(a(s, i, st, "An agent with a fairly long title here"));
            }
        }
        view(v, Some("@5"))
    }

    fn empty() -> ViewModel {
        view(vec![], None)
    }

    /// `busy()` with watcher stamps: a gap (3 free), one past nine (two-digit
    /// labels everywhere), and agents not stamped yet (filled into the gap
    /// and above).
    fn numbered() -> ViewModel {
        // Window ids follow list order: @1 Handy.app, @2 Kua Yu, @4 Tmux, @5 Notch Tasks.
        let stamps = [(0, "1"), (1, "2"), (3, "4"), (4, "12"), (5, "7")]; // @6 is idle: its 7 is stale
        let v = busy_agents()
            .into_iter()
            .enumerate()
            .map(|(k, x)| match stamps.iter().find(|(i, _)| *i == k) {
                Some((_, s)) => x.with("slot", s),
                None => x,
            })
            .collect();
        view(v, Some("@4"))
    }

    fn all() -> Vec<(&'static str, ViewModel)> {
        vec![("quiet", quiet()), ("busy", busy()), ("extreme", extreme()), ("empty", empty()), ("numbered", numbered())]
    }

    fn texts(f: &Frame) -> Vec<String> {
        f.rows.iter().map(Row::text).collect()
    }

    fn find(f: &Frame, needle: &str) -> usize {
        f.rows.iter().position(|r| r.text().contains(needle)).unwrap_or_else(|| panic!("{needle:?} not in {:#?}", texts(f)))
    }

    /// The agents section's text.
    fn agents_part(f: &Frame) -> Vec<String> {
        texts(f)[f.agents.clone()].to_vec()
    }

    /// Every agent is covered exactly once, and `… N more` says N.
    fn check_accounting(name: &str, v: &ViewModel, f: &Frame) {
        let mut want: HashMap<(String, String), usize> = HashMap::new();
        for a in groups(v).iter().flat_map(|s| s.agents.iter()) {
            *want.entry((a.session.clone(), a.window_id.clone())).or_default() += 1;
        }
        let mut got: HashMap<(String, String), usize> = HashMap::new();
        for r in &f.rows {
            for c in &r.covers {
                *got.entry(c.clone()).or_default() += 1;
            }
            let t = r.text();
            let counted = t.contains(" more") || t.contains(" agents");
            if let Some(i) = t.find("… ").filter(|_| counted && !r.covers.is_empty()) {
                let n: usize = t[i + "… ".len()..].split(' ').next().unwrap().parse().unwrap();
                assert_eq!(n, r.covers.len(), "{name} {}x{}: {t:?}", f.width, f.height);
            }
        }
        assert_eq!(got, want, "{name} {}x{}: {:#?}", f.width, f.height, texts(f));
    }

    /// The order every frame keeps: header, toolbar, NEEDS YOU, agents, spare
    /// rows, spaces, the parked row; sections never overlap.
    fn check_order(name: &str, f: &Frame) {
        let h = usize::from(f.height);
        if h < 4 {
            return;
        }
        let t = texts(f);
        assert!(f.needs.start >= 2 && f.needs.end <= f.agents.start, "{name}: {:?} {:?} {t:#?}", f.needs, f.agents);
        assert!(f.agents.end <= f.spaces.start && f.spaces.end < h, "{name}: {:?} {:?} {t:#?}", f.agents, f.spaces);
        if !f.spaces.is_empty() {
            assert!(t[f.spaces.start].starts_with(" spaces"), "{name}: {t:#?}");
            assert_eq!(f.spaces.end, h - 1, "{name}: spaces sit on the last row {t:#?}");
        }
        assert!(t[h - 1].starts_with(" ⏸ "), "{name}: {t:#?}");
        // No log, no divider, ever.
        assert!(!t.iter().any(|l| l.starts_with(" log") || l.contains('═')), "{name}: {t:#?}");
    }

    #[test]
    fn every_size_fits_and_accounts_for_every_agent() {
        for (name, v) in all() {
            for w in [24u16, 30, 34, 45, 60] {
                for h in 2u16..=70 {
                    let f = layout(&v, w, h, &Opts::default());
                    assert_eq!(f.rows.len(), usize::from(h), "{name} {w}x{h}");
                    for r in &f.rows {
                        assert_eq!(width(&r.segs), usize::from(w), "{name} {w}x{h}: {:?}", r.text());
                        assert!(r.targets.iter().all(|(a, b, _)| a < b && *b <= w), "{name} {w}x{h}");
                    }
                    check_accounting(name, &v, &f);
                    check_order(&format!("{name} {w}x{h}"), &f);
                    let mut buf = Buffer::empty(Rect::new(0, 0, w, h));
                    paint(&f, &mut buf);
                }
            }
        }
    }

    #[test]
    fn needs_come_first() {
        for (name, v) in all() {
            for h in [10u16, 15, 20, 58] {
                let f = layout(&v, 34, h, &Opts::default());
                let first = f.rows[2..].iter().map(Row::text).find(|t| !t.trim().is_empty()).unwrap();
                assert!(first.contains("need"), "{name} {h}: {first:?}");
            }
        }
    }

    /// Agents lose detail last: as the height shrinks, the spaces list goes
    /// first, then the gaps, then card lines; agents keep two lines each
    /// until all of that is gone, and their step only grows.
    #[test]
    fn agents_lose_detail_last() {
        for (name, v) in all() {
            let mut last = (0u8, 0u8);
            for h in (4u16..=90).rev() {
                let f = layout(&v, 34, h, &Opts::default());
                let now = (f.step, f.agents_step);
                assert!(now >= last, "{name} {h}: {now:?} after {last:?}");
                if f.agents_step > 0 {
                    // Compressed agents: no spare rows to give, no gaps, 1-line cards.
                    assert_eq!(f.step, STEPS.len() as u8, "{name} {h}");
                    assert!(!texts(&f).iter().any(|l| l.contains("⏎ go")), "{name} {h}");
                }
                if f.step == 0 && f.spaces.len() < 1 + v.spaces.len() {
                    // Rows short at step 0: the spaces list gave them up first.
                    assert!(f.agents.end + 2 + f.spaces.len() >= usize::from(h) - 1, "{name} {h}: {:#?}", texts(&f));
                }
                last = now;
            }
        }
    }

    #[test]
    fn quiet_is_roomy() {
        let v = quiet();
        let f = layout(&v, 34, 58, &Opts::default());
        let t = texts(&f);
        assert_eq!((f.step, f.agents_step), (0, 0), "{t:#?}");
        assert!(t[0].starts_with(" agents") && t[0].ends_with("◉1 ◐2 ○8 "), "{:?}", t[0]);
        assert_eq!(t[1], "  ⏵ next ⌥S   ⤺ back ⌥X   ≡ ⌥W    ");
        assert!(t[2].trim().is_empty() && t[3].starts_with(" needs you"));
        // The card: number + title + age, reason, place + kind. (Slots: the
        // provisional fill, main:1 main:2 main:3; idle agents hold none.)
        assert!(t[4].starts_with("▌3 ◉ Kua Yu focus timing") && t[4].ends_with("3m "), "{:?}", t[4]);
        assert!(t[5].starts_with("▌    asks Which deck"), "{:?}", t[5]);
        assert!(t[6].starts_with("▌    main:3 · ✳ claude") && t[6].ends_with("⏎ go "), "{:?}", t[6]);
        // Then a gap, and the agents right under it.
        assert!(t[7].trim().is_empty() && t[8].starts_with(" agents") && t[8].ends_with("by space "), "{t:#?}");
        assert_eq!(f.agents.start, 8);
        // Agents get two lines and the whole width for their title.
        let r = find(&f, "Tmux Agent Sidebar");
        assert!(t[r].ends_with("here ") && f.rows[r].bg == Some("sel"));
        assert!(t[r].starts_with(" 2 ◐ Tmux Agent Sidebar"), "{:?}", t[r]);
        assert!(t[r + 1].starts_with("     run Edit agent-roster.py"), "{:?}", t[r + 1]);
        let h = find(&f, "Handy.app Speech Commands ⚙");
        assert!(t[h].starts_with(" 1 ◐ Handy.app") && t[h + 1].starts_with("     run Bash swift build"), "{t:#?}");
        // An idle agent: a blank number column, its title still aligned.
        assert!(t[find(&f, "Commodities Job Tracker")].starts_with("   ○ Commodities Job Tracker"), "{t:#?}");
        // The spaces list: whole, one line each, right above the parked row;
        // spare rows between it and the agents.
        assert_eq!(f.spaces, 53..57, "{t:#?}");
        assert_eq!(&t[53..57], [" spaces                           ", " ○ bai                         ○2 ",
            " ● main                  ◉1 ◐2 ○4 ", " ○ schedule                    ○2 "]);
        assert!(t[f.agents.end..52].iter().all(|l| l.trim().is_empty()) && t[52].trim().is_empty(), "{t:#?}");
        assert_eq!(f.rows[55].bg, Some("surface0")); // where the client is
        assert_eq!(t[57], " ⏸ 1 parked                       ");
        // No event log, whatever happened.
        assert!(!t.iter().any(|l| l.contains(" log")), "{t:#?}");
    }

    #[test]
    fn busy_squeezes_spaces_and_cards_before_agents() {
        let v = busy();
        let f = layout(&v, 34, 58, &Opts::default());
        let t = texts(&f);
        // 24 agents never fit two lines each in 58 rows: compressed, so the
        // cards are one line and there are no gaps.
        assert_eq!(f.step, STEPS.len() as u8, "{t:#?}");
        assert!(f.agents_step >= 1, "{t:#?}");
        // All four needs still on screen, in agent-jump.sh order (failed first).
        let fail = find(&f, "✕ Handy.app");
        assert!(fail < find(&f, "◉ Notch hover") && fail < f.agents.start);
        for title in ["Kua Yu focus timing", "Commodities Job Tracker"] {
            assert!(t[f.needs.clone()].iter().any(|l| l.contains(title)), "{title}");
        }
        // The needing agents got their second line back in the agents section.
        let k = f.agents.start + agents_part(&f).iter().position(|l| l.contains("◉ Kua Yu")).unwrap();
        assert!(t[k + 1].contains("asks Which deck?"), "{t:#?}");
        // In space scope (main only) the same fleet keeps two lines each,
        // full cards, and the spaces list.
        let mut sv = busy();
        sv.scope.session = Some("main".into());
        let f = layout(&sv, 34, 58, &Opts::default());
        assert_eq!((f.step, f.agents_step), (0, 0), "{:#?}", texts(&f));
        assert_eq!(f.spaces.len(), 1 + sv.spaces.len());
    }

    #[test]
    fn extreme_cuts_and_counts() {
        let v = extreme();
        // 34x26: NEEDS YOU keeps a third (cards and a count); every space
        // collapses but the first, which shows what it can and counts the
        // rest. The spaces list is folded to the client's space and the
        // alert ones (capped), `+K more` on its header.
        let f = layout(&v, 34, 26, &Opts::default());
        let t = texts(&f);
        assert_eq!((f.step, f.agents_step), (STEPS.len() as u8, 3), "{t:#?}");
        assert!(t[f.needs.clone()].last().unwrap().contains("more need you"), "{t:#?}");
        assert!(t[f.agents.clone()].iter().any(|l| l.starts_with(" … ") && l.contains(" more ")), "{t:#?}");
        assert!(t[f.spaces.start].ends_with("+5 more ") && t[f.spaces.start + 1].starts_with(" ● alpha"), "{t:#?}");
        check_accounting("extreme", &v, &f);
        // 34x20: the folded spaces hold; the agents give up rows first.
        let f = layout(&v, 34, 20, &Opts::default());
        let t = texts(&f);
        assert_eq!((f.step, f.agents_step, f.spaces.len()), (STEPS.len() as u8, 4, 5), "{t:#?}");
        check_accounting("extreme", &v, &f);
        // Shorter: more spaces than rows, the rest counted.
        let f = layout(&v, 34, 13, &Opts::default());
        let t = texts(&f);
        assert_eq!(f.agents_step, 4, "{t:#?}");
        assert!(t.iter().any(|l| l.contains("more need you")), "{t:#?}");
        assert!(t.iter().any(|l| l.contains("more in")), "{t:#?}");
        // `… N more` opens the menu on All (what it hides may be idle);
        // `… N more need you` keeps the default tab.
        let more = find(&f, "more in") as u16;
        assert_eq!(f.target(3, more), Some(&Target::Menu(Some("all"))));
        let needs_more = find(&f, "more need you") as u16;
        assert_eq!(f.target(3, needs_more), Some(&Target::Menu(None)));
        check_accounting("extreme", &v, &f);
        // The spaces section never goes: header and the client's space at least.
        assert!(f.spaces.len() >= 2 && t[f.spaces.start + 1].starts_with(" ● alpha"), "{t:#?}");
        // With more room the spaces collapse, quietest first.
        let f = layout(&v, 34, 70, &Opts::default());
        assert!(f.agents_step >= 3, "{:#?}", texts(&f));
    }

    #[test]
    fn empty_still_has_every_section() {
        let f = layout(&empty(), 34, 30, &Opts::default());
        let t = texts(&f);
        assert!(t.iter().any(|l| l.starts_with(" ✓ nothing needs you")));
        assert!(t.iter().any(|l| l.starts_with(" no agents running")));
        assert_eq!(t[0], " agents                           ");
        // The plain `main` session is still a space.
        assert_eq!(&t[f.spaces.clone()], [" spaces                           ", " ○ main                           "]);
    }

    #[test]
    fn click_targets() {
        let v = quiet();
        let f = layout(&v, 34, 58, &Opts::default());
        let go = |win: &str| Some(Target::Go { win: win.into(), session: "main".into() });
        // Both lines of an agent row, and the needs card.
        let r = find(&f, "Tmux Agent Sidebar") as u16;
        assert_eq!(f.target(5, r).cloned(), go("@2"));
        assert_eq!(f.target(33, r + 1).cloned(), go("@2"));
        let card = find(&f, "▌3 ◉ Kua Yu") as u16;
        for y in card..card + 3 {
            assert_eq!(f.target(0, y).cloned(), go("@3"));
        }
        // Spaces and rules go to the session.
        let sp = find(&f, " ● main") as u16;
        assert_eq!(f.target(3, sp).cloned(), Some(Target::Session("main".into())));
        let bai = find(&f, " bai ─") as u16;
        assert_eq!(f.target(20, bai).cloned(), Some(Target::Session("bai".into())));
        // The toolbar's buttons, and the gaps between them.
        assert_eq!(f.target(0, 1), None);
        assert_eq!(f.target(2, 1), Some(&Target::Next));
        assert_eq!(f.target(13, 1), Some(&Target::Back));
        assert_eq!(f.target(26, 1), Some(&Target::Menu(None)));
        assert_eq!(f.target(12, 1), None);
        // Parked opens the menu; blank rows do nothing.
        assert_eq!(f.target(2, 57), Some(&Target::Stash));
        assert_eq!(f.target(2, 2), None);
        assert_eq!(f.target(2, 50), None);
        // A watcher problem is a restart button.
        let mut v = quiet();
        v.watcher_age = Some(99.0);
        let f = layout(&v, 34, 58, &Opts::default());
        assert!(f.rows[57].text().ends_with("watcher stalled 99s "));
        assert_eq!(f.target(30, 57), Some(&Target::Restart));
        // A message wins the corner (and is not a button).
        let f = layout(&v, 34, 58, &Opts { msg: Some("no tmux client".into()) });
        assert!(f.rows[57].text().ends_with("no tmux client "));
        assert_eq!(f.target(30, 57), None);
        assert_eq!(f.target(2, 57), Some(&Target::Stash));
    }

    #[test]
    fn titles_details_and_working_lines() {
        let v = view(
            vec![
                a("main", 1, "running", "~/Commodities Job Tracker").with("detail_kind", "done")
                    .with("detail", "**Proof passed.** see [log](http://x)"),
                a("main", 2, "running", "tmux/Tmux Agent Sidebar").with("detail_kind", "run")
                    .with("detail", "<agent-message from=a>`cargo test`</agent-message>"),
                a("main", 3, "idle", "~/Lost suit jacket").with("detail_kind", "done").with("detail", "## Found it"),
                a("main", 5, "running", "Markup only").with("detail_kind", "run").with("detail", "<agent-message from=x>"),
                a("main", 4, "needs-input", "~/Kua Yu focus timing").with("detail_kind", "ask")
                    .with("detail", "Which **deck**?"),
            ],
            None,
        );
        let f = layout(&v, 60, 58, &Opts::default());
        let t = texts(&f);
        let below = |s: &str| t[f.agents.clone()].iter().position(|l| l.contains(s)).map(|i| i + f.agents.start);
        // Under a space's rule: no `project/`, no `~/`.
        let c = below(" ◐ Commodities Job Tracker").unwrap_or_else(|| panic!("{t:#?}"));
        assert!(below("Tmux Agent Sidebar").is_some_and(|i| !t[i].contains("tmux/")));
        assert!(below(" ○ Lost suit jacket").is_some());
        // Working: never the last turn's outcome.
        assert!(t[c + 1].starts_with("     working /nonexistent/main"), "{:?}", t[c + 1]);
        let r = below("Tmux Agent Sidebar").unwrap();
        assert!(t[r + 1].starts_with("     run cargo test "), "{:?}", t[r + 1]);
        let m = below("Markup only").unwrap();
        assert!(t[m + 1].starts_with("     working /nonexistent/main"), "{:?}", t[m + 1]);
        // Idle keeps its outcome, as plain text.
        let l = below("Lost suit jacket").unwrap();
        assert!(t[l + 1].starts_with("     done Found it "), "{:?}", t[l + 1]);
        // Needs cards: no `~/`, plain reasons.
        assert!(t.iter().any(|l| l.starts_with("▌3 ◉ Kua Yu focus timing")), "{t:#?}");
        assert!(t.iter().any(|l| l.starts_with("▌    asks Which deck?")), "{t:#?}");
    }

    #[test]
    fn tiny_heights() {
        // No room for sections: one row still counts every agent.
        for h in 2u16..=3 {
            let f = layout(&quiet(), 34, h, &Opts::default());
            assert!(f.rows.iter().any(|r| r.text().starts_with(" … 11 agents")), "{h}: {:#?}", texts(&f));
            let y = find(&f, " … 11 agents") as u16;
            assert_eq!(f.target(3, y), Some(&Target::Menu(Some("all"))));
        }
        // Nothing pending in one row: never ` … 0 need you`.
        assert_eq!(needs_cut(&empty(), 34, 1).iter().map(Row::text).collect::<Vec<_>>(), [dim_line(34, " ✓ nothing needs you").text()]);
    }

    #[test]
    fn cut_spaces_keep_the_current_and_attention_ones() {
        // 12 spaces in a short frame; the client is in the last one by
        // name, and one other in the middle needs you.
        let mut v = Vec::new();
        for (k, s) in ["s01", "s02", "s03", "s04", "s05", "s06", "s07", "s08", "s09", "s10", "s11", "zz"].iter().enumerate() {
            v.push(a(s, 1, if k == 6 { "needs-input" } else { "idle" }, "t"));
        }
        let mut v = view(v, Some("@12"));
        v.scope.session = Some("zz".into());
        // Every height from 7 rows up keeps the section: folded, it is the
        // header with `+K more`, the client's space and the alert one, in
        // list order; it never vanishes before the agents lose detail.
        for h in 7u16..=40 {
            let f = layout(&v, 34, h, &Opts::default());
            let t = texts(&f);
            let part = &t[f.spaces.clone()];
            let names: Vec<&str> = part.iter().filter_map(|l| l.strip_prefix(" ○ ").or(l.strip_prefix(" ● "))).collect();
            let (s07, zz) = (names.iter().position(|n| n.starts_with("s07")), names.iter().position(|n| n.starts_with("zz")));
            // 6-7 rows: the client's space alone; from 8, the alert one too.
            assert!(zz.is_some(), "{h}: {t:#?}");
            assert!(h < 8 || (s07.is_some() && s07 < zz), "{h}: {t:#?}");
            // 13 spaces: the twelve and the fixture's plain `main`.
            if names.len() < 13 {
                assert!(part[0].starts_with(" spaces") && part[0].ends_with(&format!("+{} more ", 13 - names.len())), "{h}: {t:#?}");
                assert_eq!(f.target(30, f.spaces.start as u16), Some(&Target::Menu(Some("all"))));
            } else {
                assert_eq!(part[0].trim(), "spaces");
            }
            check_accounting(&format!("cut {h}"), &v, &f);
        }
    }

    /// The spaces section with `@space_index`: rows in index order (not name
    /// order), each starting with its `⌃N` (past 9 or unset: a blank label,
    /// unindexed spaces last), `@space_hidden` sessions left out; a click on
    /// a row switches the client to that space.
    #[test]
    fn spaces_carry_their_ctrl_number_in_index_order() {
        let v = view(
            vec![
                a("main", 1, "running", "M").with("space_index", "1"),
                a("bai", 1, "idle", "B").with("space_index", "3"),
                a("cua-notch", 1, "needs-input", "C").with("space_index", "2"),
                a("zeta", 1, "idle", "Z").with("space_index", "12"),
                a("aaa", 1, "idle", "A"),
                a("hidden", 1, "idle", "H").with("space_hidden", "1"),
            ],
            Some("@1"),
        );
        let f = layout(&v, 34, 58, &Opts::default());
        let t = texts(&f);
        let part = &t[f.spaces.clone()];
        assert_eq!(part, [
            " spaces                           ",
            " ⌃1 ● main                     ◐1 ",
            " ⌃2 ○ cua-notch                ◉1 ",
            " ⌃3 ○ bai                      ○1 ",
            "    ○ zeta                     ○1 ",
            "    ○ aaa                      ○1 ",
        ], "{t:#?}");
        // Click: that space (Actions::goto_session: agent-jump.sh goto on
        // its active window, in that session).
        let y = f.spaces.start as u16 + 2;
        assert_eq!(f.target(3, y), Some(&Target::Session("cua-notch".into())));
        assert_eq!(f.target(33, y), Some(&Target::Session("cua-notch".into())));
        // The current space is tinted, its label blue.
        assert_eq!(f.rows[f.spaces.start + 1].bg, Some("surface0"));
        assert_eq!(f.rows[f.spaces.start + 1].segs[1], seg("⌃1 ", "blue"));
        // No index anywhere: no label column at all.
        let f = layout(&quiet(), 34, 58, &Opts::default());
        assert!(texts(&f)[f.spaces.clone()].iter().skip(1).all(|l| l.starts_with(" ○ ") || l.starts_with(" ● ")));
    }

    #[test]
    fn folding_and_collapsing() {
        let v = busy();
        // One-line rows, then folds.
        let f = (20..58).rev().map(|h| layout(&v, 34, h, &Opts::default())).find(|f| f.agents_step == 2).unwrap();
        let t = texts(&f);
        assert!(t.iter().any(|l| l.contains("○○○ 3 idle · ")), "{t:#?}");
        // The fold opens the menu on its Idle tab (Active, the default,
        // does not list idle agents).
        let fold = find(&f, "○○○ 3 idle · ") as u16;
        assert_eq!(f.target(5, fold), Some(&Target::Menu(Some("idle"))));
        // The client's window is never folded away.
        assert!(t.iter().any(|l| l.contains("Tmux Agent Sidebar")));
        // Squeezed further: whole spaces collapse, quietest first; main
        // (the most attention) keeps its rows longest.
        let f = (10..58).rev().map(|h| layout(&v, 34, h, &Opts::default())).find(|f| f.agents_step == 3).unwrap();
        let t = texts(&f);
        assert!(t[f.agents.clone()].iter().any(|l| l.contains("Kua Yu focus timing")), "{t:#?}");
    }

    /// Two frames through the sidebar's memo: a newcomer that sorts first
    /// (session `aa`) takes the next free number and moves nobody's; the
    /// question just answered (idle + @agent_pending) keeps its number, is no
    /// longer a NEEDS YOU card, and is never folded into the idle line.
    #[test]
    fn memo_and_answered_render() {
        let base = || {
            vec![
                a("main", 1, "running", "One"),
                a("main", 2, "needs-input", "Two").with("detail_kind", "ask").with("detail", "which?"),
                a("main", 3, "idle", "Q3"),
                a("main", 4, "idle", "Q4"),
                a("main", 5, "idle", "Q5"),
                a("work", 2, "running", "Late sorter"),
            ]
        };
        let mut memo = crate::view::SlotMemo::default();
        let v = view_with(base(), None, &mut memo);
        let f = layout(&v, 34, 58, &Opts::default());
        for want in ["▌2 ◉ Two", " 1 ◐ One", " 2 ◉ Two", " 3 ◐ Late sorter"] {
            find(&f, want);
        }
        // Next frame: Two answered 600 s ago (still Active); `aa` starts (id
        // @7, sorts before main).
        let answered = |pending: &str| {
            let mut next = base();
            next[1] = a("main", 2, "idle", "Two").with("pending", pending);
            next.push(a("aa", 1, "running", "Early bird"));
            next
        };
        let v = view_with(answered(&(NOW - 600).to_string()), None, &mut memo);
        assert!(v.needs.is_empty());
        let f = layout(&v, 34, 58, &Opts::default());
        let t = texts(&f);
        assert!(t.iter().any(|l| l.starts_with(" ✓ nothing needs you")), "{t:#?}");
        for want in [" 4 ◐ Early bird", " 1 ◐ One", " 2 ○ Two", " 3 ◐ Late sorter", "   ○ Q3"] {
            assert!(t.iter().any(|l| l.starts_with(want)), "{want}: {t:#?}");
        }
        // Squeezed until idle agents fold: Two stays on its own numbered row.
        let f = (8..58).rev().map(|h| layout(&v, 34, h, &Opts::default())).find(|f| f.agents_step == 2).unwrap();
        let t = texts(&f);
        let fold = t.iter().find(|l| l.contains(" idle · ")).unwrap_or_else(|| panic!("{t:#?}"));
        assert!(fold.contains("3 idle · Q3, Q4, Q5"), "{fold:?}");
        assert!(t.iter().any(|l| l.starts_with(" 2 ○ Two")), "{t:#?}");
        // Without the memo the newcomer would have taken 1.
        let v = view(answered(&(NOW - 600).to_string()), None);
        assert_eq!(v.agents().find(|x| x.title == "Early bird").and_then(|x| x.slot), Some(1));
        // The stamp lapses (601 s): Two is plain idle, its number gone (a
        // blank column), folded with the others; nobody else's number moves.
        let v = view_with(answered(&(NOW - 601).to_string()), None, &mut memo);
        let f = layout(&v, 34, 58, &Opts::default());
        let t = texts(&f);
        for want in ["   ○ Two", " 4 ◐ Early bird", " 1 ◐ One", " 3 ◐ Late sorter"] {
            assert!(t.iter().any(|l| l.starts_with(want)), "{want}: {t:#?}");
        }
        assert!(!t.iter().any(|l| l.starts_with(" 2 ")), "{t:#?}");
        let f = (8..58).rev().map(|h| layout(&v, 34, h, &Opts::default())).find(|f| f.agents_step == 2).unwrap();
        let t = texts(&f);
        assert!(t.iter().any(|l| l.contains("4 idle · Two, Q3, Q4, Q5")), "{t:#?}");
        // A junk stamp never counted.
        let v = view(answered("soon"), None);
        assert_eq!(v.agents().find(|x| x.title == "Two").map(|x| (x.answered, x.slot)), Some((false, None)));
    }

    /// Slots on cards and rows: kept stamps (with a gap and one past nine),
    /// a stale stamp on an idle agent ignored, unstamped agents filled into
    /// the gap; two-digit labels everywhere; idle rows and the fold blank.
    #[test]
    fn numbers_on_cards_and_rows() {
        let v = numbered();
        assert_eq!(v.slot_width, 2);
        let slot = |title: &str| v.agents().find(|a| a.title == title).and_then(|a| a.slot);
        assert_eq!([slot("Handy.app Speech Commands"), slot("Kua Yu focus timing"), slot("Tmux Agent Sidebar"),
            slot("Notch Tasks Orchestrator"), slot("Lost suit jacket"), slot("Notch hover animation"),
            slot("Island resize spring"), slot("Commodities Job Tracker")],
            [Some(1), Some(2), Some(4), Some(12), None, Some(3), Some(5), Some(8)]);
        let f = layout(&v, 34, 58, &Opts::default());
        let t = texts(&f);
        // Cards: the bar, then the number.
        for card in ["▌01 ✕ Handy.app", "▌02 ◉ Kua Yu", "▌03 ◉ Notch hover", "▌08 ✓ Commodities"] {
            assert!(t[f.needs.clone()].iter().any(|l| l.starts_with(card)), "{card}: {t:#?}");
        }
        // Rows: the same numbers, the titles aligned behind a 3-cell column.
        let body = agents_part(&f);
        for row in [" 01 ✕ Handy.app", " 02 ◉ Kua Yu", " 04 ◐ Tmux Agent Sidebar", " 12 ◐ Notch Tasks"] {
            assert!(body.iter().any(|l| l.starts_with(row)), "{row}: {t:#?}");
        }
        assert!(body.iter().all(|l| !l.contains("○ Lost suit") || l.starts_with("    ○ Lost suit")), "{t:#?}");
        // Wider and taller: every agent on its own two lines, second lines indented past the number.
        let f = layout(&v, 60, 120, &Opts::default());
        let t = texts(&f);
        assert_eq!(f.agents_step, 0, "{t:#?}");
        let r = find(&f, " 04 ◐ Tmux Agent Sidebar");
        assert!(t[r + 1].starts_with("      working "), "{:?}", t[r + 1]);
        assert!(t.iter().any(|l| l.starts_with("    ○ Lost suit jacket")), "{t:#?}");
        let card = find(&f, "▌02 ◉ Kua Yu");
        assert!(t[card + 1].starts_with("▌     asks Which deck?") && t[card + 2].starts_with("▌     main:2 · ✳ claude"),
            "{t:#?}");
        // Squeezed until idle agents fold: the fold line has no numbers and
        // its circles sit under the titles.
        let f = (20..58).rev().map(|h| layout(&v, 34, h, &Opts::default())).find(|f| f.agents_step == 2).unwrap();
        let fold = texts(&f).into_iter().find(|l| l.contains(" idle · ")).unwrap();
        assert!(fold.starts_with("      ○○"), "{fold:?}");
        // One digit when no slot reaches 10.
        let f = layout(&quiet(), 34, 58, &Opts::default());
        assert!(texts(&f).iter().any(|l| l.starts_with(" 2 ◐ Tmux Agent Sidebar")));
    }

    /// A scoped view: `agents` plus the usual noise, the client on `here` in
    /// `session`, with `@spaces` / `@strip_scope` / `@space_hidden` on its
    /// client row, scope applied as the sidebar does.
    fn scoped(agents: Vec<A>, here: &str, session: &str, spaces: &str, scope: &str, hidden: &str) -> ViewModel {
        let mut rows: Vec<String> = agents
            .iter()
            .enumerate()
            .map(|(n, x)| {
                let mut set: Vec<(&str, &str)> = vec![("state", x.st), ("summary", x.title)];
                set.extend(x.set.iter().map(|(k, v)| (*k, v.as_str())));
                row(x.s, x.i, &format!("@{}", n + 1), &set)
            })
            .collect();
        rows.push(row("agents", 1, "@900", &[("state", "failed"), ("summary", "hidden")]));
        rows.push(row("stash", 1, "@901", &[("summary", "x"), ("stash_label", "Parked one"), ("stash_ts", "5")]));
        rows.push(format!("\x1fclient\x1f{CLIENT}\x1f{here}\x1f{session}\x1f{spaces}\x1f{scope}\x1f{hidden}"));
        let c = Collator::new("en_US.UTF-8");
        let snap = Snapshot::parse(&rows.join("\n"));
        let args = BuildArgs { client: Some(CLIENT), now: NOW as f64, collator: &c, git: None, log: vec![], watcher_age: Some(1.0) };
        let mut v = ViewModel::build(&snap, args);
        v.apply_scope(&snap);
        v
    }

    /// agent-roster tests' OrdinalTests fixture: agents interleaved with
    /// shells, out of index order, and another session. Ids follow the list:
    /// @1 shell (the client's), @2 idle, @3 shell, @4 failed (index 5),
    /// @5 running (index 4), @6 shell, @7 needs-input (index 12), @8 other.
    fn proj() -> Vec<A<'static>> {
        vec![
            a("proj", 1, "", "zsh"),
            a("proj", 2, "idle", "p/two"),
            a("proj", 3, "", "zsh"),
            a("proj", 5, "failed", "p/five"),
            a("proj", 4, "running", "p/four"),
            a("proj", 9, "", "zsh"),
            a("proj", 12, "needs-input", "p/twelve"),
            a("other", 1, "running", "o/one"),
        ]
    }

    #[test]
    fn space_scope_shows_one_session_numbered_by_ordinal() {
        let v = scoped(proj(), "@1", "proj", "on", "", "");
        assert_eq!(v.scope.session.as_deref(), Some("proj"));
        let ord: Vec<_> = v.spaces.iter().find(|s| s.name == "proj").unwrap().agents.iter()
            .map(|a| (a.window_id.as_str(), a.ordinal)).collect();
        // By window index, shells skipped (and, @spaces on, the client's shell is no row).
        assert_eq!(ord, [("@2", Some(1)), ("@5", Some(2)), ("@4", Some(3)), ("@7", Some(4))]);
        let f = layout(&v, 34, 58, &Opts::default());
        let t = texts(&f);
        // The header: this space's counts only (other's ◐ not counted).
        assert!(t[0].starts_with(" space ") && t[0].ends_with("✕1 ◉1 ◐1 ○1 "), "{:?}", t[0]);
        let b = agents_part(&f);
        assert!(b[0].starts_with(" agents") && b[0].ends_with("this space "), "{b:#?}");
        assert!(b[1].starts_with(" proj ─"), "{b:#?}");
        let numbered: Vec<&str> = b.iter().filter(|l| l.starts_with(" 1 ") || l.starts_with(" 2 ")
            || l.starts_with(" 3 ") || l.starts_with(" 4 ")).map(|l| l.trim_end()).collect();
        assert_eq!(numbered.len(), 4, "{b:#?}");
        for (k, want) in [" 1 ○ two", " 2 ◐ four", " 3 ✕ five", " 4 ◉ twelve"].iter().enumerate() {
            assert!(numbered[k].starts_with(want), "{want}: {b:#?}");
        }
        // Only this session in the agents section; no shell rows; NEEDS YOU
        // stays global (and unnumbered), as does the spaces list.
        assert!(!b.iter().any(|l| l.contains("other") || l.contains("o/one") || l.contains("zsh")), "{b:#?}");
        let needs = &t[f.needs.clone()];
        assert!(needs.iter().any(|l| l.starts_with("▌✕ p/five")) && needs.iter().any(|l| l.starts_with("▌◉ p/twelve")),
            "{needs:#?}");
        assert!(t[f.spaces.clone()].iter().any(|l| l.contains("○ other")), "{t:#?}");
        // The title toggles; the counts do not.
        assert_eq!((f.target(0, 0), f.target(1, 0), f.target(5, 0), f.target(6, 0), f.target(30, 0)),
            (None, Some(&Target::Scope), Some(&Target::Scope), None, None));
    }

    #[test]
    fn explicit_space_scope_without_spaces_keeps_the_current_shell() {
        let v = scoped(proj(), "@1", "proj", "", "space", "");
        assert_eq!(v.scope.session.as_deref(), Some("proj"));
        let f = layout(&v, 34, 58, &Opts::default());
        let b = agents_part(&f);
        // The client's shell is a row (the classic rule), blank in the number column.
        assert!(b.iter().any(|l| l.starts_with("     zsh")), "{b:#?}");
        assert!(b.iter().any(|l| l.starts_with(" 1 ○ two")) && b.iter().any(|l| l.starts_with(" 4 ◉ twelve")), "{b:#?}");
    }

    #[test]
    fn all_scope_with_spaces_drops_shells_and_toggles() {
        let v = scoped(proj(), "@1", "proj", "on", "all", "");
        assert_eq!(v.scope.session, None);
        let f = layout(&v, 34, 58, &Opts::default());
        let t = texts(&f);
        assert!(t[0].starts_with(" agents ") && t[0].ends_with("✕1 ◉1 ◐2 ○1 "), "{:?}", t[0]);
        assert_eq!(f.target(3, 0), Some(&Target::Scope));
        let b = agents_part(&f);
        assert!(b.iter().any(|l| l.starts_with(" other ─")) && b.iter().any(|l| l.starts_with(" proj ─")), "{b:#?}");
        assert!(!b.iter().any(|l| l.contains("zsh")), "{b:#?}");
        // Slots (other:1 took 1), not ordinals, in this scope; idle holds none.
        assert!(b.iter().any(|l| l.starts_with(" 3 ✕ five")) && b.iter().any(|l| l.starts_with("   ○ two")), "{b:#?}");
        // A space with nothing but shells is no group at all.
        let mut only_shell = proj();
        only_shell.push(a("plain", 1, "", "zsh"));
        let v = scoped(only_shell, "@9", "plain", "on", "all", "");
        assert!(!agents_part(&layout(&v, 34, 58, &Opts::default())).iter().any(|l| l.contains(" plain ")));
    }

    #[test]
    fn classic_when_spaces_off_and_scope_unset() {
        // "1", "off" or junk is not on; junk @strip_scope is unset.
        for (spaces, scope) in [("", ""), ("off", ""), ("1", ""), ("", "bogus")] {
            let v = scoped(proj(), "@1", "proj", spaces, scope, "");
            assert_eq!((v.scope.session.as_ref(), v.scope.toggles(), v.scope.name()), (None, false, "all"), "{spaces} {scope}");
            let f = layout(&v, 34, 58, &Opts::default());
            assert!(f.rows[0].targets.is_empty());
            // The client's shell still shows: the classic rule.
            assert!(agents_part(&f).iter().any(|l| l.contains("zsh")), "{spaces} {scope}");
        }
        for (spaces, scope, want) in [("on", "", "space"), ("on", "all", "all"), ("", "space", "space"),
            ("on", "bogus", "space"), ("1", "", "all")] {
            assert_eq!(scoped(proj(), "@1", "proj", spaces, scope, "").scope.name(), want, "{spaces} {scope}");
        }
    }

    #[test]
    fn hidden_sessions_fall_back_to_all() {
        // spaces.conf marks it, or the crate's own list, or the stash: header and body agree.
        let mut ws = proj();
        ws.push(a("scratch", 1, "running", "s/one"));
        for (session, here, hidden) in [("proj", "@2", "1"), ("scratch", "@9", ""), ("stash", "@901", "")] {
            let v = scoped(ws.iter().map(|x| A { set: x.set.clone(), ..*x }).collect(), here, session, "on", "", hidden);
            assert_eq!((v.scope.name(), v.scope.session.as_ref()), ("space", None), "{session}");
            let f = layout(&v, 34, 58, &Opts::default());
            assert!(texts(&f)[0].starts_with(" agents "), "{session}");
            let b = agents_part(&f);
            assert!(b[0].ends_with("by space ") && b.iter().any(|l| l.starts_with(" other ─")), "{session}: {b:#?}");
        }
    }

    #[test]
    fn empty_space_still_has_its_box() {
        let mut ws = proj();
        ws.push(a("empty", 1, "", "zsh"));
        let v = scoped(ws, "@9", "empty", "on", "", "");
        let f = layout(&v, 34, 30, &Opts::default());
        let t = texts(&f);
        assert_eq!(t[0].trim_end(), " space"); // no counts: nothing in it
        let b = agents_part(&f);
        assert!(b[1].starts_with(" empty ─") && b[2].starts_with(" no agents in this space"), "{b:#?}");
        assert!(t[f.needs.clone()][0].starts_with(" needs you"));
        assert_eq!(f.target(4, (f.agents.start + 1) as u16), Some(&Target::Session("empty".into())));
    }

    #[test]
    fn ordinals_stop_at_nine_and_go_when_narrow() {
        let ws: Vec<A> = (1..=11).map(|i| a("big", i, "running", ["b/n1", "b/n2", "b/n3", "b/n4", "b/n5", "b/n6",
            "b/n7", "b/n8", "b/n9", "b/n10", "b/n11"][i as usize - 1])).collect();
        let v = scoped(ws, "@1", "big", "on", "", "");
        let f = layout(&v, 34, 70, &Opts::default());
        let b = agents_part(&f);
        assert!(b.iter().any(|l| l.starts_with(" 9 ◐ n9 ")), "{b:#?}");
        assert!(b.iter().any(|l| l.starts_with("   ◐ n10 ")) && b.iter().any(|l| l.starts_with("   ◐ n11 ")), "{b:#?}");
        // Narrow: the number only while the title keeps 4 cells. A one-line
        // row has 3 cells of glyph and ` 1m ` (age) fixed, 2 for the number.
        let r = &v.spaces[0].agents[0];
        assert_eq!(r.age, "1m");
        for (w, shown) in [(10, false), (12, false), (13, true), (30, true)] {
            let row = &agent_rows(r, w, false, Num::ORDINAL)[0];
            assert_eq!(width(&row.segs), w, "{w}");
            assert_eq!(row.text().starts_with(" 1 ◐"), shown, "{w}: {:?}", row.text());
        }
    }

    #[test]
    fn scoped_views_fit_every_size() {
        let mut fleet = busy_agents();
        for i in 1..=12 {
            fleet.push(a("main", 10 + i, if i % 3 == 0 { "" } else { "idle" }, "Filler"));
        }
        let views = [
            scoped(proj(), "@1", "proj", "on", "", ""),
            scoped(proj(), "@1", "proj", "", "space", ""),
            scoped(proj(), "@1", "proj", "on", "all", ""),
            scoped(fleet.iter().map(|x| A { set: x.set.clone(), ..*x }).collect(), "@4", "main", "on", "", ""),
        ];
        for v in &views {
            for w in [16u16, 24, 34, 60] {
                for h in 2u16..=70 {
                    let f = layout(v, w, h, &Opts::default());
                    assert_eq!(f.rows.len(), usize::from(h));
                    for r in &f.rows {
                        assert_eq!(width(&r.segs), usize::from(w), "{w}x{h}: {:?}", r.text());
                        assert!(r.targets.iter().all(|(a, b, _)| a < b && *b <= w), "{w}x{h}");
                    }
                    check_accounting(&format!("scoped {w}x{h}"), v, &f);
                    check_order(&format!("scoped {w}x{h}"), &f);
                }
            }
        }
    }

    /// Squeezed hard, no row goes to waste: one row left over under a
    /// collapsed space shows its most important agent (the rule counts the
    /// rest), never a blank row above `⏸ N parked`.
    #[test]
    fn a_last_spare_row_shows_an_agent() {
        let mut fleet = busy_agents();
        for i in 1..=12 {
            fleet.push(a("main", 10 + i, "idle", "Filler"));
        }
        let v = scoped(fleet.iter().map(|x| A { set: x.set.clone(), ..*x }).collect(), "@4", "main", "on", "", "");
        for h in 6u16..=14 {
            let f = layout(&v, 34, h, &Opts::default());
            let t = texts(&f);
            if f.agents_step >= 3 {
                assert!(t[f.agents.end..usize::from(h) - 1].iter().all(|l| !l.trim().is_empty()), "{h}: {t:#?}");
            }
            check_accounting(&format!("fleet {h}"), &v, &f);
        }
        // 11 rows: 3 go to the folded spaces (main, and cua-notch needing
        // you); the one spare row under main's rule is its failed agent.
        let f = layout(&v, 34, 11, &Opts::default());
        let t = texts(&f);
        let rule = find(&f, " main ─");
        assert!(t[rule + 1].starts_with(" 1 ✕ Handy.app"), "{t:#?}");
        assert_eq!(f.spaces.len(), 3, "{t:#?}");
        assert!(t[10].starts_with(" ⏸ "), "{t:#?}");
    }

    #[test]
    fn glyph_widths_agree_with_ratatui() {
        // Every fixed glyph the sidebar draws: the layout's cell count
        // (text::dwidth) must be ratatui's (unicode-width over the grapheme).
        let bad: Vec<(char, usize, usize)> = "⏵⤺≡⌥▌◉✓✕◐○●─↑↓✎⚙◎▸…✳⬢⏎⏸⌃"
            .chars()
            .map(|c| (c, dwidth(&c.to_string()), c.to_string().width()))
            .filter(|(_, a, b)| a != b || *a != 1)
            .collect();
        assert!(bad.is_empty(), "{bad:?}");
    }

    /// The terminal's own background everywhere, except where the client is.
    #[test]
    fn renders_through_ratatui_on_the_terminal_background() {
        use ratatui::backend::TestBackend;
        use ratatui::Terminal;
        let f = layout(&quiet(), 34, 58, &Opts::default());
        let mut term = Terminal::new(TestBackend::new(34, 58)).unwrap();
        term.draw(|fr| paint(&f, fr.buffer_mut())).unwrap();
        let buf = term.backend().buffer();
        let here = find(&f, "Tmux Agent Sidebar") as u16;
        let cur_space = find(&f, " ● main") as u16;
        for y in 0..58u16 {
            for x in 0..34u16 {
                let want = if y == here || y == here + 1 {
                    palette::color("sel")
                } else if y == cur_space {
                    palette::color("surface0")
                } else {
                    Color::Reset
                };
                assert_eq!(buf[(x, y)].bg, want, "({x},{y}) {:?}", f.rows[y as usize].text());
            }
        }
        let ansi = to_ansi(buf);
        assert_eq!(ansi.lines().count(), 58);
        // No background code at all on an untinted row.
        assert!(ansi.starts_with("\x1b[0;38;2;205;214;244;1m agents"), "{:?}", &ansi[..60]);
        assert!(!ansi.lines().next().unwrap().contains(";48;"));
    }
}
