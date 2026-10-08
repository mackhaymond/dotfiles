//! The sidebar's pure half: a [`ViewModel`] and a size in, every row to draw
//! (each exactly `width` cells, with what a click on each cell does) out. No
//! I/O, so every density rule is unit-tested.
//!
//! ## Shape (the approved v4 mockup)
//!
//! ```text
//!  agents              ◉1 ◐2 ○8      header + per-state counts
//!  ⏵ next ⌥S  ⤺ back ⌥X  ≡ ⌥W       toolbar (Actions next/back, the menu)
//!
//!  needs you                    1    agent-jump.sh `list` order, cards
//! ▌◉ Kua Yu focus timing     44m
//! ▌  asks Which deck should…
//! ▌  main:3 · ✳ claude      ⏎ go
//!
//!  spaces                            one per session, fixed (name) order
//!  ● main            ◉1 ◐2 ○4
//!    ⌥ main ↑2  ~/.config/tmux
//!
//!  log                               event log, newest first; soaks up the rest
//!  11:10 ◉ Kua Yu: which deck?
//! ────────────────═─────────────────  the divider: a FIXED row (split ratio)
//!  agents                  by space
//!  main ⌥ main ──────── ◉1 ◐2 ○4     one rule per space with agents
//!  ◉ Kua Yu focus timing             each agent: title (full width) …
//!    asks Which deck should…  44m    … and what it does / waits on
//!
//!  ▸ parked 5                         the stash; a watcher problem in red
//! ```
//!
//! The divider sits at [`split_row`] and never moves on its own: needs and
//! agents coming and going only change what fills each half. That fixed
//! position is the point of the layout (agent-roster.py's strip grew and
//! shrank its boxes, so every row jumped).
//!
//! ## Density ladder: never scroll, never drop an agent silently
//!
//! Each half compresses on its own, one step at a time, until it fits:
//!
//! - top: the log shrinks to nothing first (it only ever gets the rows left
//!   over); then spaces go to one line each; then the blank gaps go; then
//!   needs cards go from 3 lines to 2 to 1; finally needs (and, past that,
//!   spaces) are cut with a `… N more need you` line.
//! - bottom: every agent to one line (age on the same line); then the idle
//!   agents of a space fold into one `○○○ 3 idle · Name, Name…` line; then
//!   whole spaces collapse to their rule line (with state-coloured counts),
//!   fewest attention agents first; finally a `… N more` line. Rows a step
//!   leaves over go back to second lines for the agents that matter most
//!   (attention, then in flight), so the half is never left half empty.
//!
//! Every bottom row records the agents it stands for ([`Row::covers`]), so a
//! test can prove each agent is shown, folded, collapsed or counted.

use crate::actions::watcher_problem;
use crate::model::{Cat, Counts, Flag, HIDDEN};
use crate::palette;
use crate::text::{clip, dwidth, fit_label, plain, sanitize};
use crate::view::{tilde, Agent, LogEntry, Need, Space, ViewModel};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use std::cmp::Reverse;
use unicode_width::UnicodeWidthStr;

/// Each half keeps at least this many rows, however the divider is dragged.
pub const MIN_HALF: u16 = 6;
/// Where the divider sits until the user drags it (a fraction of the height).
pub const DEFAULT_SPLIT: f64 = 0.5;

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
    /// An agent row, a needs card, a log line: agent-jump.sh goto
    /// (`Actions::go`, Strip.click "goto").
    Go { win: String, session: String },
    /// A log line: goto, but only while `win` is still in `session` at
    /// `index`. Window ids restart with the tmux server and the log file
    /// outlives it, so an id alone could name some other window.
    Log { win: String, session: String, index: u32 },
    /// A space row or rule: that session's active window (Strip.click "session").
    Session(String),
    /// The toolbar: agent-jump.sh next / back.
    Next,
    Back,
    /// The Option-W menu (Strip.open_menu), optionally on a tab ("parked").
    Menu(Option<&'static str>),
    /// The split divider: press and drag.
    Divider,
    /// The red watcher warning: restart it (the strip's ⟳, Strip.click "restart").
    Restart,
}

/// One screen line.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Row {
    /// Exactly the frame's width in cells.
    pub segs: Vec<Seg>,
    /// The row's background (None: `base`).
    pub bg: Option<Hue>,
    /// Click targets, `[x0, x1)` in 0-based cells (Strip.targets).
    pub targets: Vec<(u16, u16, Target)>,
    /// The agents, as (session, window id), this row stands for in the
    /// bottom half: shown, folded, collapsed into a rule, or counted.
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
    /// The divider's row, which is also the top half's height.
    pub split: u16,
    /// Exactly `height` rows.
    pub rows: Vec<Row>,
    /// The ladder step each half settled on (0 = roomiest).
    pub top_step: u8,
    pub bottom_step: u8,
    /// The spaces drawn with their git line (two-line rows with agents): the
    /// only ones whose `git status` is worth asking for.
    pub git_spaces: Vec<String>,
}

impl Frame {
    /// The target at 0-based cell (x, y), if any.
    pub fn target(&self, x: u16, y: u16) -> Option<&Target> {
        self.rows.get(y as usize).and_then(|r| r.target_at(x))
    }
}

/// What the layout needs besides the view.
#[derive(Debug, Clone, PartialEq)]
pub struct Opts {
    /// The divider's position as a fraction of the height (`@agent_sidebar_split`).
    pub split: f64,
    /// A transient message for the last row (Roster.say).
    pub msg: Option<String>,
}

impl Default for Opts {
    fn default() -> Opts {
        Opts { split: DEFAULT_SPLIT, msg: None }
    }
}

/// The divider row for a dragged-to row: each half keeps [`MIN_HALF`] rows
/// (both get half of a screen too short for that).
pub fn clamp_row(row: u16, height: u16) -> u16 {
    let lo = MIN_HALF.min(height / 2);
    let hi = height.saturating_sub(MIN_HALF).max(height / 2);
    row.clamp(lo, hi)
}

/// The divider row for a split ratio (non-finite: the default).
pub fn split_row(height: u16, ratio: f64) -> u16 {
    let r = if ratio.is_finite() { ratio.clamp(0.0, 1.0) } else { DEFAULT_SPLIT };
    clamp_row((f64::from(height) * r).round() as u16, height)
}

/// The ratio to remember after dragging the divider to `row`: rounds back to
/// the same (clamped) row at this height.
pub fn ratio_for_row(row: u16, height: u16) -> f64 {
    if height == 0 {
        return DEFAULT_SPLIT;
    }
    f64::from(clamp_row(row, height)) / f64::from(height)
}

/// A state's colour in the sidebar: CAT_HUE, except working is the steady
/// blue of the mockup (the live marks pulse; counts and log lines do not).
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

/// Row 0: ` agents` and the per-state counts over every shown space.
fn header_row(view: &ViewModel, w: usize) -> Row {
    let mut right = count_segs(&view.counts, true);
    right.push(seg(" ", "text"));
    Row::new(fit(w, vec![bold(" agents", "text")], right, None))
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
        segs.push(Seg { bg: Some("surface0"), ..seg(label, "text") });
        targets.push(((x + 1) as u16, (x + 1 + lw) as u16, t));
        x += 1 + lw;
    }
    Row { targets, ..Row::new(fit(w, segs, vec![], None)) }
}

/// A NEEDS YOU card: 3, 2 or 1 lines on `surface0`, a left bar in the state
/// colour (Strip.need_detail's reason on line 2).
fn need_rows(n: &Need, w: usize, lines: u8) -> Vec<Row> {
    let a = &n.agent;
    let hue = a.color.unwrap_or("overlay");
    let go = Target::Go { win: a.window_id.clone(), session: a.session.clone() };
    let card = |segs: Vec<Seg>| Row::new(segs).bg(Some("surface0")).click(w, go.clone());
    let bar = || seg("▌", hue);
    let age = if a.age.is_empty() { String::new() } else { format!("{} ", a.age) };
    let (proj, title) = fit_label(no_home(&a.title), w.saturating_sub(3 + dwidth(&age)));
    let mut rows = vec![card(fit(
        w,
        vec![bar(), seg(format!("{} ", a.glyph), hue), seg(proj, "overlay"), bold(title, "text")],
        vec![seg(age, "sub")],
        None,
    ))];
    if lines >= 2 {
        rows.push(card(fit(
            w,
            vec![
                bar(),
                seg(format!("  {} ", n.reason_word), word_hue(&n.reason_word, hue)),
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
            vec![bar(), seg(format!("  {}{kind}", sanitize(&a.place())), "overlay")],
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

/// The whole NEEDS YOU section. Never empty: with nothing pending it says so,
/// so nothing below it jumps when the first need arrives.
fn needs_block(view: &ViewModel, w: usize, lines: u8) -> Vec<Row> {
    let mut rows = vec![needs_header(view, w)];
    if view.needs.is_empty() {
        rows.push(dim_line(w, " ✓ nothing needs you"));
    }
    for n in &view.needs {
        rows.extend(need_rows(n, w, lines));
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

/// A space: `●`/`○`, name and counts; then branch, ahead/behind/dirty and
/// path (`two`). The current one on `surface0`; one with no agents dim.
fn space_rows(sp: &Space, w: usize, two: bool) -> Vec<Row> {
    let empty = sp.counts.total() == 0 && !sp.is_current;
    let bg = sp.is_current.then_some("surface0");
    let go = Target::Session(sp.name.clone());
    let name = sanitize(&sp.name);
    let (mark, name) = if sp.is_current {
        (seg(" ● ", "blue"), bold(name, "text"))
    } else if empty {
        (seg(" ○ ", "overlay"), seg(name, "overlay"))
    } else {
        (seg(" ○ ", "overlay"), seg(name, "sub"))
    };
    let mut right = count_segs(&sp.counts, true);
    right.push(seg(" ", "text"));
    let mut rows = vec![Row::new(fit(w, vec![mark, name], right, None)).bg(bg).click(w, go.clone())];
    if two && !empty && (!sp.branch.is_empty() || !sp.path_short.is_empty()) {
        let mut segs = Vec::new();
        if sp.branch.is_empty() {
            segs.push(seg("   ", "text"));
        } else {
            segs.push(seg(format!("   ⌥ {}", sanitize(&sp.branch)), if sp.is_current { "mauve" } else { "overlay" }));
            if let Some(g) = &sp.git {
                for (n, glyph, hue) in [(g.ahead as usize, "↑", "green"), (g.behind as usize, "↓", "red"), (g.dirty(), "✎", "peach")] {
                    if n > 0 {
                        segs.push(seg(format!(" {glyph}{n}"), hue));
                    }
                }
            }
            segs.push(seg("  ", "text"));
        }
        segs.push(seg(sanitize(&sp.path_short), "overlay"));
        rows.push(Row::new(fit(w, segs, vec![], None)).bg(bg).click(w, go));
    }
    rows
}

fn spaces_block(view: &ViewModel, w: usize, two: bool) -> Vec<Row> {
    let mut rows = vec![section(w, "spaces", "sub", vec![])];
    for sp in &view.spaces {
        rows.extend(space_rows(sp, w, two));
    }
    rows
}

/// SPACES in at most `room` rows (one line each), the rest counted. The
/// ones kept: the client's space, then those with an agent needing
/// attention, then name order; shown in name order.
fn spaces_cut(view: &ViewModel, w: usize, room: usize) -> Vec<Row> {
    let rows = spaces_block(view, w, false);
    if rows.len() <= room {
        return rows;
    }
    let n = view.spaces.len();
    let more = |k: usize, words: &str| dim_line(w, &format!(" … {k} {words}"));
    match room {
        0 => vec![],
        1 => vec![more(n, "spaces")],
        _ => {
            let keep = room - 2;
            let mut ranked: Vec<usize> = (0..n).collect();
            ranked.sort_by_key(|&i| {
                let sp = &view.spaces[i];
                (!sp.is_current, !sp.agents.iter().any(|a| a.attention))
            });
            let mut kept = ranked[..keep].to_vec();
            kept.sort_unstable();
            let mut rows = vec![section(w, "spaces", "sub", vec![])];
            rows.extend(kept.iter().flat_map(|&i| space_rows(&view.spaces[i], w, false)));
            rows.push(more(n - keep, "more spaces"));
            rows
        }
    }
}

/// The log lines worth showing. Lines whose NEW state is `idle` ("you
/// looked at it", "it went quiet") are noise, and so are HIDDEN sessions
/// (agents, tasks, ...).
fn log_entries(view: &ViewModel) -> Vec<&LogEntry> {
    view.log
        .iter()
        .filter(|e| e.event.state != "idle" && !HIDDEN.contains(&e.event.session.as_str()))
        .collect()
}

/// ` HH:MM ◉ Title: detail`, glyph and colour from the NEW state (never the
/// detail kind); a running line without a detail says `started`. A click
/// goes there only if that window is still the one logged ([`Target::Log`]).
fn log_row(e: &LogEntry, w: usize) -> Row {
    let ev = &e.event;
    let title = sanitize(no_home(&ev.title));
    let detail = plain(&ev.detail);
    let text = match (detail.is_empty(), ev.state.as_str()) {
        (false, _) => format!("{title}: {detail}"),
        (true, "running") => format!("{title} started"),
        (true, _) => title,
    };
    let hue = e.cat.map(cat_hue).unwrap_or("overlay");
    Row::new(fit(
        w,
        vec![seg(format!(" {} ", e.time), "overlay"), seg(format!("{} ", e.glyph), hue), seg(text, "sub")],
        vec![],
        None,
    ))
    .click(w, Target::Log { win: ev.window_id.clone(), session: ev.session.clone(), index: ev.index })
}

/// The top half's steps before cutting: (blank gaps, card lines, two-line spaces).
const TOP_STEPS: [(bool, u8, bool); 5] =
    [(true, 3, true), (true, 3, false), (false, 3, false), (false, 2, false), (false, 1, false)];

/// The top half, exactly `budget` rows → (rows, ladder step).
fn top_rows(view: &ViewModel, w: usize, budget: usize) -> (Vec<Row>, u8) {
    let mut rows = vec![header_row(view, w), toolbar_row(w)];
    let blank = || Row::blank(w);
    let mut step = TOP_STEPS.len() as u8;
    for (i, &(gaps, lines, two)) in TOP_STEPS.iter().enumerate() {
        let needs = needs_block(view, w, lines);
        let spaces = spaces_block(view, w, two);
        let gap = usize::from(gaps);
        if rows.len() + needs.len() + spaces.len() + 2 * gap > budget {
            continue;
        }
        if gaps {
            rows.push(blank());
        }
        rows.extend(needs);
        if gaps {
            rows.push(blank());
        }
        rows.extend(spaces);
        // The log only ever gets what is left over, so it shrinks first. An
        // empty one (a fresh log, a quiet day) says what will appear there.
        let log = log_entries(view);
        let rest = budget - rows.len();
        let head = gap + 1;
        if rest > head {
            if gaps {
                rows.push(blank());
            }
            rows.push(section(w, "log", "sub", vec![]));
            rows.extend(log.iter().take(rest - head).map(|e| log_row(e, w)));
            if log.is_empty() {
                let long = " events appear here as agents change state";
                rows.push(dim_line(w, if dwidth(long) <= w { long } else { " agent events appear here" }));
            }
        }
        step = i as u8;
        break;
    }
    if step as usize == TOP_STEPS.len() {
        // Even 1-line cards do not fit: needs get at least half the room
        // (they are why the sidebar exists), spaces what is left; both count
        // what they cut.
        let avail = budget.saturating_sub(rows.len());
        let needs_full = 1 + view.needs.len().max(1);
        let spaces_full = 1 + view.spaces.len();
        let needs_room = avail.saturating_sub(spaces_full).max(needs_full.min(avail.div_ceil(2))).min(avail);
        rows.extend(needs_cut(view, w, needs_room));
        rows.extend(spaces_cut(view, w, avail - needs_room));
    }
    rows.truncate(budget);
    while rows.len() < budget {
        rows.push(blank());
    }
    (rows, step)
}

/// The divider: a `─` rule with a `═` grip in the middle.
fn divider_row(w: usize) -> Row {
    let g = w.saturating_sub(1) / 2;
    Row::new(vec![seg("─".repeat(g), "surface1"), seg("═", "overlay"), seg("─".repeat(w - g - 1), "surface1")])
        .click(w, Target::Divider)
}

/// A title without a bare `~/` "project" (a summary made in $HOME).
fn no_home(title: &str) -> &str {
    title.strip_prefix("~/").unwrap_or(title)
}

/// The title under its space's rule: the `project/` prefix dropped (the
/// space already says where it is).
fn short_title(a: &Agent) -> String {
    sanitize(if a.short_title.is_empty() { no_home(&a.title) } else { &a.short_title })
}

/// One agent: 2 lines (title / what it does + age) or 1 (title + age). The
/// window the client is on gets the `sel` background and `here`. Line 2 of
/// a working agent is its run detail or `working` + path; of any other, its
/// last outcome (`done …`, `fail …`) or its path.
fn agent_rows(a: &Agent, w: usize, two: bool) -> Vec<Row> {
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
    let room = w.saturating_sub(3 + flag.as_ref().map_or(0, |f| dwidth(&f.text)) + width(&right) + gap);
    let title = clip(&short_title(a), room);
    let title = if quiet { seg(title, "sub") } else if two { bold(title, "text") } else { seg(title, "text") };
    let mut left = vec![seg(format!(" {} ", a.glyph), hue), title];
    left.extend(flag);
    let first = Row::new(fit(w, left, right, None)).bg(bg).click(w, go.clone()).covering([a]);
    if !two {
        return vec![first];
    }
    let path = || seg(sanitize(&tilde(&a.path)), "overlay");
    // A detail that is nothing but markup (a truncated `<agent-message …>`)
    // counts as none.
    let detail = plain(&a.detail);
    let mut l2 = vec![seg("   ", "text")];
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
    let row = Row::new(fit(w, left, right, Some(('─', "surface0")))).click(w, Target::Session(sp.name.clone()));
    if collapsed {
        row.covering(&sp.agents)
    } else {
        row
    }
}

/// `   ○○○ 3 idle · Name, Name…`: a space's idle agents in one line.
fn fold_row(idle: &[&Agent], w: usize) -> Row {
    let k = idle.len();
    let names: Vec<String> = idle.iter().map(|a| short_title(a)).collect();
    Row::new(fit(
        w,
        vec![seg(format!("   {} {k} idle · ", "○".repeat(k.min(4))), "overlay"), seg(names.join(", "), "overlay")],
        vec![],
        None,
    ))
    .click(w, Target::Menu(None))
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
    dim_line(w, &text).click(w, Target::Menu(None)).covering(agents.iter().copied())
}

/// An agent's identity across frames: (session, window id).
type Key<'a> = (&'a str, &'a str);

fn key(a: &Agent) -> Key<'_> {
    (&a.session, &a.window_id)
}

/// With `fold`, a space's idle agents (never the client's window) go into
/// one line, when there are 2+ of them → (shown in place, folded).
fn split_idle(sp: &Space, fold: bool) -> (Vec<&Agent>, Vec<&Agent>) {
    let foldable = |a: &Agent| fold && a.cat == Some(Cat::Idle) && !a.is_current;
    let (idle, shown): (Vec<&Agent>, Vec<&Agent>) = sp.agents.iter().partition(|a| foldable(a));
    if idle.len() >= 2 {
        (shown, idle)
    } else {
        (sp.agents.iter().collect(), vec![])
    }
}

/// A space's agent rows in window-index order (the folded line last); the
/// agents in `two` get their second line.
fn group_rows(sp: &Space, w: usize, two: &[Key], fold: bool) -> Vec<Row> {
    let (shown, idle) = split_idle(sp, fold);
    let mut rows: Vec<Row> = shown.iter().flat_map(|a| agent_rows(a, w, two.contains(&key(a)))).collect();
    if !idle.is_empty() {
        rows.push(fold_row(&idle, w));
    }
    rows
}

/// A space's agents in `room` rows: all of them one line each if they fit,
/// else the `room - 1` that matter most ([`promotion_rank`], kept in list
/// order) and a `… N more` line for the rest.
fn partial_rows(sp: &Space, w: usize, room: usize) -> Vec<Row> {
    if sp.agents.len() <= room {
        return sp.agents.iter().flat_map(|a| agent_rows(a, w, false)).collect();
    }
    let mut ranked: Vec<usize> = (0..sp.agents.len()).collect();
    ranked.sort_by_key(|&k| promotion_rank(&sp.agents[k]));
    let mut keep = ranked[..room - 1].to_vec();
    keep.sort_unstable();
    let mut rows: Vec<Row> = keep.iter().flat_map(|&k| agent_rows(&sp.agents[k], w, false)).collect();
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

/// The bottom half's content (between the divider and the last row), at
/// most `budget` rows → (rows, ladder step).
///
/// Steps 1..=3 are a SHAPE (one-line rows; idle folded; spaces collapsed);
/// rows a shape leaves over go back to second lines for the agents that
/// matter most ([`promotion_rank`]), so a half that just misses two lines
/// each is not left mostly empty.
fn bottom_rows(view: &ViewModel, w: usize, budget: usize) -> (Vec<Row>, u8) {
    if budget == 0 {
        return (vec![], 0);
    }
    let groups: Vec<&Space> = view.spaces.iter().filter(|s| !s.agents.is_empty()).collect();
    let header = section(w, "agents", "sub", vec![seg("by space ", "overlay")]);
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
                rows.extend(group_rows(sp, w, two, fold));
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
        if spare < 2 {
            return (promoted(true, &collapsed).unwrap_or(base), 3);
        }
        // The space whose collapse made it fit keeps the rows that frees:
        // its most important agents, one line each, and `… N more`.
        let at = 1 + (0..i)
            .map(|j| 1 + if collapsed[j] { 0 } else { group_rows(groups[j], w, &[], true).len() })
            .sum::<usize>();
        let mut rows = base;
        rows[at] = rule_row(groups[i], w, false);
        rows.splice(at + 1..at + 1, partial_rows(groups[i], w, spare));
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

/// The last row: ` ▸ parked N` (the menu's parked tab), and on the right a
/// message (Roster.say) or the watcher's problem in red (click: restart).
fn last_row(view: &ViewModel, w: usize, msg: Option<&str>) -> Row {
    let left = vec![seg(format!(" ▸ parked {}", view.parked.len()), "overlay")];
    let (right, restart) = match (msg, watcher_problem(view.watcher_age)) {
        (Some(m), _) => (vec![seg(format!("{} ", sanitize(m)), "sub")], false),
        (None, Some(p)) => (vec![seg(format!("watcher {p} "), "red")], true),
        (None, None) => (vec![], false),
    };
    let segs = fit(w, left, right.clone(), None);
    let rw = width(&clip_segs(right, w)) as u16;
    let w16 = w as u16;
    let mut row = Row::new(segs);
    row.targets.push((0, w16 - rw, Target::Menu(Some("parked"))));
    if restart && rw > 0 {
        row.targets.push((w16 - rw, w16, Target::Restart));
    }
    row
}

/// The whole screen: top half, divider at [`split_row`], bottom half, last
/// row. Always exactly `height` rows of exactly `width` cells.
pub fn layout(view: &ViewModel, width: u16, height: u16, opts: &Opts) -> Frame {
    let (w, h) = (usize::from(width), usize::from(height));
    let split = split_row(height, opts.split);
    let mut frame = Frame { width, height, split, ..Frame::default() };
    if w == 0 || h == 0 {
        return frame;
    }
    let split_n = usize::from(split);
    let (mut rows, top_step) = top_rows(view, w, split_n);
    let body = h.saturating_sub(split_n + 2);
    let all: Vec<&Agent> = view.agents().collect();
    if body == 0 && !all.is_empty() {
        // Too short for a bottom half: one row still counts every agent
        // (in place of the divider; the parked row follows if there is room).
        rows.push(more_row(w, &all, 0));
        if rows.len() < h {
            rows.push(last_row(view, w, opts.msg.as_deref()));
        }
    } else {
        rows.push(divider_row(w));
        let (bottom, bottom_step) = bottom_rows(view, w, body);
        rows.extend(bottom);
        while rows.len() < h.saturating_sub(1).max(split_n + 1) {
            rows.push(Row::blank(w));
        }
        rows.push(last_row(view, w, opts.msg.as_deref()));
        frame.bottom_step = bottom_step;
    }
    rows.truncate(h);
    if top_step == 0 {
        frame.git_spaces = view.spaces.iter().filter(|s| !s.agents.is_empty()).map(|s| s.name.clone()).collect();
    }
    frame.rows = rows;
    frame.top_step = top_step;
    frame
}

/// Draw a frame into a ratatui buffer: each row's background across the
/// whole line, then its segments at the cells the layout counted (a segment
/// never spills into the next, whatever width ratatui gives a grapheme).
pub fn paint(frame: &Frame, buf: &mut Buffer) {
    let area = buf.area;
    for (y, row) in frame.rows.iter().enumerate().take(usize::from(area.height)) {
        let y = area.y + y as u16;
        let row_bg = palette::color(row.bg.unwrap_or("base"));
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
/// change, the cells under a wide grapheme skipped as a terminal would.
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
    use crate::events::Event;
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
    fn view(agents: Vec<A>, here: Option<&str>, log: Vec<Event>) -> ViewModel {
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
        let args = BuildArgs { client: Some(CLIENT), now: NOW as f64, collator: &c, git: None, log, watcher_age: Some(1.0) };
        ViewModel::build(&Snapshot::parse(&rows.join("\n")), args)
    }

    fn ev(min_ago: i64, id: &str, state: &str, title: &str, detail: &str) -> Event {
        Event {
            epoch: NOW - 60 * min_ago,
            window_id: id.into(),
            session: "main".into(),
            index: 1,
            state: state.into(),
            prev_state: "running".into(),
            detail_kind: String::new(),
            title: title.into(),
            detail: detail.into(),
        }
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
        let log = (0..20)
            .map(|k| match ["needs-input", "done", "idle", "running", "failed"][k as usize % 5] {
                "idle" => ev(k * 7, "@3", "idle", "IDLE-NOISE", ""),
                st => ev(k * 7, "@3", st, "Kua Yu", "asked"),
            })
            .collect();
        view(agents, Some("@2"), log)
    }

    /// A busy day: 24 agents, 4 needs (the mockup's second frame).
    fn busy() -> ViewModel {
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
        view(v, Some("@4"), vec![ev(1, "@1", "failed", "Handy.app", "529 overloaded")])
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
        view(v, Some("@5"), vec![])
    }

    fn empty() -> ViewModel {
        view(vec![], None, vec![])
    }

    fn all() -> Vec<(&'static str, ViewModel)> {
        vec![("quiet", quiet()), ("busy", busy()), ("extreme", extreme()), ("empty", empty())]
    }

    fn texts(f: &Frame) -> Vec<String> {
        f.rows.iter().map(Row::text).collect()
    }

    fn find(f: &Frame, needle: &str) -> usize {
        f.rows.iter().position(|r| r.text().contains(needle)).unwrap_or_else(|| panic!("{needle:?} not in {:#?}", texts(f)))
    }

    /// Every agent is covered exactly once, and `… N more` says N.
    fn check_accounting(name: &str, v: &ViewModel, f: &Frame) {
        let mut want: HashMap<(String, String), usize> = HashMap::new();
        for a in v.agents() {
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
                    let mut buf = Buffer::empty(Rect::new(0, 0, w, h));
                    paint(&f, &mut buf);
                }
            }
        }
    }

    #[test]
    fn divider_never_moves_with_the_content() {
        for h in [10u16, 20, 40, 58, 70] {
            let frames: Vec<Frame> =
                [quiet(), busy(), empty(), extreme()].iter().map(|v| layout(v, 34, h, &Opts::default())).collect();
            for f in &frames {
                assert_eq!(f.split, frames[0].split, "h={h}");
                assert_eq!(f.rows[usize::from(f.split)], divider_row(34), "h={h}");
            }
        }
        assert_eq!(layout(&quiet(), 34, 58, &Opts::default()).split, 29);
    }

    #[test]
    fn needs_come_first() {
        for (name, v) in all() {
            for h in [10u16, 20, 58] {
                let f = layout(&v, 34, h, &Opts::default());
                let first = f.rows[2..].iter().map(Row::text).find(|t| !t.trim().is_empty()).unwrap();
                assert!(first.contains("need"), "{name} {h}: {first:?}");
            }
        }
    }

    #[test]
    fn quiet_is_roomy() {
        let v = quiet();
        let f = layout(&v, 34, 58, &Opts::default());
        let t = texts(&f);
        assert_eq!((f.top_step, f.bottom_step), (0, 0), "{t:#?}");
        assert!(t[0].starts_with(" agents") && t[0].ends_with("◉1 ◐2 ○8 "), "{:?}", t[0]);
        assert_eq!(t[1], "  ⏵ next ⌥S   ⤺ back ⌥X   ≡ ⌥W    ");
        assert!(t[2].trim().is_empty() && t[3].starts_with(" needs you"));
        // The card: title + age, reason, place + kind.
        assert!(t[4].starts_with("▌◉ Kua Yu focus timing") && t[4].ends_with("3m "), "{:?}", t[4]);
        assert!(t[5].starts_with("▌  asks Which deck"));
        assert!(t[6].starts_with("▌  main:3 · ✳ claude") && t[6].ends_with("⏎ go "));
        // The log soaks up the rest of the top half, right up to the divider.
        let log = find(&f, " log");
        assert!(log < 29 && t[28].contains("Kua Yu: asked"), "{t:#?}");
        // Idle lines are noise; glyphs follow the new state.
        assert!(!t.iter().any(|l| l.contains("IDLE-NOISE")), "{t:#?}");
        assert!(t[log + 1].contains(" ◉ Kua Yu: asked") && t[log + 2].contains(" ✓ Kua Yu"), "{t:#?}");
        let started = log_row(&LogEntry { event: ev(0, "@1", "running", "Handy", ""), time: "11:00".into(),
            cat: Some(Cat::Working), glyph: "◐", color: "pink", age: String::new() }, 34);
        assert!(started.text().starts_with(" 11:00 ◐ Handy started"));
        assert_eq!(started.segs[1].fg, "blue");
        // Agents get two lines and the whole width for their title.
        let r = find(&f, "Tmux Agent Sidebar");
        assert!(r > 29 && t[r].ends_with("here ") && f.rows[r].bg == Some("sel"));
        assert!(t[r + 1].starts_with("   run Edit agent-roster.py"));
        let h = find(&f, "Handy.app Speech Commands ⚙");
        assert!(t[h + 1].starts_with("   run Bash swift build"));
        assert_eq!(t[57], " ▸ parked 1                       ");
    }

    #[test]
    fn busy_compresses_each_half_alone() {
        let f = layout(&busy(), 34, 58, &Opts::default());
        let t = texts(&f);
        assert!(f.top_step >= 1 && f.bottom_step >= 1, "{t:#?}");
        // All four needs still on screen, in agent-jump.sh order (failed first).
        let fail = find(&f, "✕ Handy.app");
        assert!(fail < find(&f, "◉ Notch hover") && fail < 29);
        for title in ["Kua Yu focus timing", "Commodities Job Tracker"] {
            assert!(t[..29].iter().any(|l| l.contains(title)), "{title}");
        }
    }

    #[test]
    fn extreme_cuts_and_counts() {
        let v = extreme();
        let f = layout(&v, 34, 20, &Opts::default());
        let t = texts(&f);
        assert_eq!((f.top_step, f.bottom_step), (5, 4), "{t:#?}");
        assert!(t.iter().any(|l| l.contains("more need you")), "{t:#?}");
        assert!(t.iter().any(|l| l.contains("more in")), "{t:#?}");
        check_accounting("extreme", &v, &f);
        // With more room the spaces collapse, quietest first; the
        // attention-heavy ones keep their rows.
        let f = layout(&v, 34, 70, &Opts::default());
        assert!(f.bottom_step >= 3, "{:#?}", texts(&f));
    }

    #[test]
    fn empty_still_has_every_section() {
        let f = layout(&empty(), 34, 30, &Opts::default());
        let t = texts(&f);
        assert!(t.iter().any(|l| l.starts_with(" ✓ nothing needs you")));
        assert!(t.iter().any(|l| l.starts_with(" no agents running")));
        assert_eq!(t[0], " agents                           ");
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
        let card = find(&f, "▌◉ Kua Yu") as u16;
        for y in card..card + 3 {
            assert_eq!(f.target(0, y).cloned(), go("@3"));
        }
        // Spaces (both lines) and rules go to the session.
        let sp = find(&f, " ● main") as u16;
        assert_eq!(f.target(3, sp).cloned(), Some(Target::Session("main".into())));
        assert_eq!(f.target(3, sp + 1).cloned(), Some(Target::Session("main".into())));
        let bai = find(&f, " bai ─") as u16;
        assert_eq!(f.target(20, bai).cloned(), Some(Target::Session("bai".into())));
        // The toolbar's buttons, and the gaps between them.
        assert_eq!(f.target(0, 1), None);
        assert_eq!(f.target(2, 1), Some(&Target::Next));
        assert_eq!(f.target(13, 1), Some(&Target::Back));
        assert_eq!(f.target(26, 1), Some(&Target::Menu(None)));
        assert_eq!(f.target(12, 1), None);
        // A log line goes to its window; the divider drags; parked opens the menu.
        let log = find(&f, "Kua Yu: asked") as u16;
        assert_eq!(f.target(4, log).cloned(), Some(Target::Log { win: "@3".into(), session: "main".into(), index: 1 }));
        assert_eq!(f.target(16, 29), Some(&Target::Divider));
        assert_eq!(f.target(2, 57), Some(&Target::Menu(Some("parked"))));
        assert_eq!(f.target(2, 2), None);
        // A watcher problem is a restart button.
        let mut v = quiet();
        v.watcher_age = Some(99.0);
        let f = layout(&v, 34, 58, &Opts::default());
        assert!(f.rows[57].text().ends_with("watcher stalled 99s "));
        assert_eq!(f.target(30, 57), Some(&Target::Restart));
        // A message wins the corner (and is not a button).
        let f = layout(&v, 34, 58, &Opts { msg: Some("no tmux client".into()), ..Opts::default() });
        assert!(f.rows[57].text().ends_with("no tmux client "));
        assert_eq!(f.target(30, 57), None);
        assert_eq!(f.target(2, 57), Some(&Target::Menu(Some("parked"))));
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
            vec![ev(1, "@1", "done", "~/Commodities Job Tracker", "**7 fixes** applied")],
        );
        let f = layout(&v, 60, 58, &Opts::default());
        let t = texts(&f);
        let below =|s: &str| t[30..].iter().position(|l| l.contains(s)).map(|i| i + 30);
        // Under a space's rule: no `project/`, no `~/`.
        let c = below(" ◐ Commodities Job Tracker").unwrap_or_else(|| panic!("{t:#?}"));
        assert!(below("Tmux Agent Sidebar").is_some_and(|i| !t[i].contains("tmux/")));
        assert!(below(" ○ Lost suit jacket").is_some());
        // Working: never the last turn's outcome.
        assert!(t[c + 1].starts_with("   working /nonexistent/main"), "{:?}", t[c + 1]);
        let r = below("Tmux Agent Sidebar").unwrap();
        assert!(t[r + 1].starts_with("   run cargo test "), "{:?}", t[r + 1]);
        let m = below("Markup only").unwrap();
        assert!(t[m + 1].starts_with("   working /nonexistent/main"), "{:?}", t[m + 1]);
        // Idle keeps its outcome, as plain text.
        let l = below("Lost suit jacket").unwrap();
        assert!(t[l + 1].starts_with("   done Found it "), "{:?}", t[l + 1]);
        // Needs cards and log lines: no `~/`, plain reasons and details.
        assert!(t.iter().any(|l| l.starts_with("▌◉ Kua Yu focus timing")), "{t:#?}");
        assert!(t.iter().any(|l| l.starts_with("▌  asks Which deck?")), "{t:#?}");
        assert!(t.iter().any(|l| l.contains("✓ Commodities Job Tracker: 7 fixes app")), "{t:#?}");
    }

    #[test]
    fn tiny_heights_and_git_spaces() {
        // No room for a bottom half: one row counts every agent.
        for h in 2u16..=4 {
            let f = layout(&quiet(), 34, h, &Opts::default());
            assert!(f.rows.iter().any(|r| r.text().starts_with(" … 11 agents")), "{h}: {:#?}", texts(&f));
        }
        // Nothing pending in one row: never ` … 0 need you`.
        assert_eq!(needs_cut(&empty(), 34, 1).iter().map(Row::text).collect::<Vec<_>>(), [dim_line(34, " ✓ nothing needs you").text()]);
        // git status only where its line is drawn, never for agent-less spaces.
        let f = layout(&quiet(), 34, 58, &Opts::default());
        assert_eq!(f.git_spaces, ["bai", "main", "schedule"]);
        assert!(layout(&busy(), 34, 58, &Opts::default()).git_spaces.is_empty());
    }

    #[test]
    fn empty_log_says_what_goes_there() {
        let f = layout(&empty(), 34, 40, &Opts::default());
        let log = find(&f, " log");
        assert_eq!(f.rows[log + 1].text().trim_end(), " agent events appear here");
        let f = layout(&empty(), 50, 40, &Opts::default());
        assert!(texts(&f).iter().any(|l| l.starts_with(" events appear here as agents change state")));
        // Any line at all: no hint.
        let f = layout(&quiet(), 34, 58, &Opts::default());
        assert!(!texts(&f).iter().any(|l| l.contains("appear here")));
    }

    #[test]
    fn cut_spaces_keep_the_current_and_attention_ones() {
        // 12 spaces in a short top half; the client is in the last one by
        // name, and one other in the middle needs you.
        let mut v = Vec::new();
        for (k, s) in ["s01", "s02", "s03", "s04", "s05", "s06", "s07", "s08", "s09", "s10", "s11", "zz"].iter().enumerate() {
            v.push(a(s, 1, if k == 6 { "needs-input" } else { "idle" }, "t"));
        }
        let f = layout(&view(v, Some("@12"), vec![]), 34, 20, &Opts::default());
        let t = texts(&f);
        let top = &t[..usize::from(f.split)];
        let names: Vec<&str> = top.iter().filter_map(|l| l.strip_prefix(" ○ ").or(l.strip_prefix(" ● "))).collect();
        assert!(names.iter().any(|n| n.starts_with("zz")) && names.iter().any(|n| n.starts_with("s07")), "{t:#?}");
        assert!(top.iter().any(|l| l.contains("more spaces")), "{t:#?}");
    }

    #[test]
    fn folding_and_collapsing() {
        let v = busy();
        // One-line rows, then folds.
        let f = layout(&v, 34, 52, &Opts::default());
        let t = texts(&f);
        assert_eq!(f.bottom_step, 2, "{t:#?}");
        assert!(t.iter().any(|l| l.contains("○○○ 3 idle · ")), "{t:#?}");
        // The client's window is never folded away.
        assert!(t.iter().any(|l| l.contains("Tmux Agent Sidebar")));
        // Squeezed further: whole spaces collapse, quietest first; main
        // (the most attention) keeps its rows longest.
        let f = layout(&v, 34, 30, &Opts::default());
        let t = texts(&f);
        assert_eq!(f.bottom_step, 3, "{t:#?}");
        assert!(t.iter().any(|l| l.contains("Kua Yu focus timing")), "{t:#?}");
    }

    #[test]
    fn drag_clamps() {
        assert_eq!(split_row(58, 0.5), 29);
        assert_eq!(split_row(58, 0.0), MIN_HALF);
        assert_eq!(split_row(58, 1.0), 58 - MIN_HALF);
        assert_eq!(split_row(58, f64::NAN), 29);
        assert_eq!(split_row(10, 0.9), 5); // too short for two 6-row halves: half each
        assert_eq!(clamp_row(0, 40), 6);
        assert_eq!(clamp_row(100, 40), 34);
        for h in [10u16, 20, 58, 99] {
            for row in 0..=h {
                assert_eq!(split_row(h, ratio_for_row(row, h)), clamp_row(row, h), "{row}/{h}");
            }
        }
        let f = layout(&quiet(), 34, 40, &Opts { split: 0.99, msg: None });
        assert_eq!((f.split, f.rows[34].target_at(0)), (34, Some(&Target::Divider)));
    }

    #[test]
    fn glyph_widths_agree_with_ratatui() {
        // Every fixed glyph the sidebar draws: the layout's cell count
        // (text::dwidth) must be ratatui's (unicode-width over the grapheme).
        let bad: Vec<(char, usize, usize)> = "⏵⤺≡⌥▌◉✓✕◐○●═─↑↓✎⚙◎▸…✳⬢⏎"
            .chars()
            .map(|c| (c, dwidth(&c.to_string()), c.to_string().width()))
            .filter(|(_, a, b)| a != b || *a != 1)
            .collect();
        assert!(bad.is_empty(), "{bad:?}");
    }

    #[test]
    fn renders_through_ratatui() {
        use ratatui::backend::TestBackend;
        use ratatui::Terminal;
        let f = layout(&quiet(), 34, 58, &Opts::default());
        let mut term = Terminal::new(TestBackend::new(34, 58)).unwrap();
        term.draw(|fr| paint(&f, fr.buffer_mut())).unwrap();
        let buf = term.backend().buffer();
        assert_eq!(buf[(16, 29)].symbol(), "═");
        assert_eq!(buf[(0, 29)].fg, palette::color("surface1"));
        assert_eq!(buf[(0, 4)].bg, palette::color("surface0")); // the needs card
        assert_eq!(buf[(33, 57)].bg, palette::color("base"));
        let ansi = to_ansi(buf);
        assert_eq!(ansi.lines().count(), 58);
        assert!(ansi.starts_with("\x1b[0;38;2;205;214;244;48;2;30;30;46;1m agents"));
    }
}
