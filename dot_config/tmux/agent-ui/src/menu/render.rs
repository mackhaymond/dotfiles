//! Drawing one frame of the menu into a ratatui [`Buffer`], plus the frame's
//! click map, the labels on screen, and the `--once` ANSI dump.
//!
//! The layout is the approved mockup's ("Option-W menu", agent-ui-v4):
//!
//! ```text
//! ╭─ agents ───────────────────────────────────────────── ⌥W ✕ ─╮   border, title, close
//! │  Active 3   All 11   Needs you 1   Working 2  … │ / search… │ │   chips + search
//! ├──────────────────────────┬───────────────────────────────────┤
//! │ main ⌥ main ───── ◉1 ◐2  │ Title               ◉ asks · 44m   │   list | preview
//! │  1 ◉ Kua Yu focus…   44m │ main:3 · ✳ claude · ⌥ main · ~/x   │
//! │      asks Which deck…    │ ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌  │
//! │  2 ◐ Handy.app ⚙     19m │ (the pane's last lines, coloured)  │
//! │ work ──────────── ✕1 ◐1  │                                    │
//! │ …                       ▕│  ⏎ go to it   p full peek  …       │   buttons
//! ├──────────────────────────┴───────────────────────────────────┤
//! │  ⏎  go   1–9  jump   ⇥  filter  …                             │   key bar / prompts
//! ╰──────────────────────────────────────────────────────────────╯
//! ```
//!
//! (The default Active tab, drawn above: space groups in fixed order, the
//! selected attention row's reason line under it. The All tab starts with a
//! NEEDS YOU section instead.)
//!
//! Under [`PREVIEW_MIN_W`] columns the preview goes and the list takes the
//! body. The full peek (`p`) takes the whole body (agent-roster.py
//! `peek_panel`). The footer shows, first match wins, the Python's order:
//! the peek hint, a y/n, the search hints, a half-typed number, a message,
//! the dead-number warning, else the key bar.

use super::rows::{Row, Tab, Target};
use super::state::{Button, Hit, Menu};
use crate::actions::watcher_problem;
use crate::hotkeys::hotkey_labels;
use crate::palette::color;
use crate::text::{clip, dwidth, fit_label, plain, sanitize};
use crate::view::{tilde, Agent};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use std::collections::HashMap;

/// Below this width the preview is hidden and the list takes the body.
pub const PREVIEW_MIN_W: u16 = 90;
/// The list's narrowest width beside a preview.
pub const LIST_MIN_W: u16 = 44;
/// Smaller than this, the frame is one line saying so.
pub const MIN_W: u16 = 30;
pub const MIN_H: u16 = 8;

fn fg(name: &str) -> Style {
    Style::new().fg(color(name))
}

fn on(f: &str, b: &str) -> Style {
    Style::new().fg(color(f)).bg(color(b))
}

fn bold(s: Style) -> Style {
    s.add_modifier(Modifier::BOLD)
}

/// Write `s` at (x, y), never past `end` (exclusive) → the x after it.
fn put(buf: &mut Buffer, x: u16, y: u16, end: u16, s: &str, st: Style) -> u16 {
    if x >= end {
        return x;
    }
    buf.set_stringn(x, y, s, (end - x) as usize, st).0
}

fn cells(s: &str) -> u16 {
    dwidth(s).min(u16::MAX as usize) as u16
}

/// A run of text in one style.
struct Seg(String, Style);

fn seg(t: impl Into<String>, st: Style) -> Seg {
    Seg(t.into(), st)
}

fn segs_w(v: &[Seg]) -> u16 {
    v.iter().map(|s| cells(&s.0)).sum()
}

/// One line across [x, x+w): `left` from the left, `right` flush right,
/// the gap between filled with `fill` (a one-cell string) when given.
fn line(buf: &mut Buffer, x: u16, y: u16, w: u16, left: &[Seg], right: &[Seg], fill: Option<(&str, Style)>) {
    let end = x + w;
    let rw = segs_w(right).min(w);
    let rstart = end - rw;
    let mut cx = x;
    for s in left {
        cx = put(buf, cx, y, rstart, &s.0, s.1);
    }
    if let Some((f, st)) = fill {
        while cx < rstart {
            cx = put(buf, cx, y, rstart, f, st);
        }
    }
    let mut rx = rstart;
    for s in right {
        rx = put(buf, rx, y, end, &s.0, s.1);
    }
}

/// The frame's geometry.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Geo {
    /// The body: between the two separators, inside the side borders.
    pub body: Rect,
    pub list: Rect,
    /// The preview, and the x of the │ before it (None: hidden).
    pub preview: Option<Rect>,
    pub split: Option<u16>,
}

impl Geo {
    pub fn new(w: u16, h: u16) -> Geo {
        let body = Rect::new(1, 3, w.saturating_sub(2), h.saturating_sub(6));
        if w < PREVIEW_MIN_W {
            return Geo { body, list: body, preview: None, split: None };
        }
        let lw = ((w - 3) * 47 / 100).max(LIST_MIN_W);
        let split = 1 + lw;
        let list = Rect::new(1, 3, lw, body.height);
        let preview = Rect::new(split + 1, 3, w - 2 - lw - 1, body.height);
        Geo { body, list, preview: Some(preview), split: Some(split) }
    }
}

/// A title as shown: a bare `~/` project prefix (an agent started in $HOME)
/// says nothing, so it goes; a real `project/` prefix stays.
pub fn shown_title(t: &str) -> &str {
    t.strip_prefix("~/").unwrap_or(t)
}

/// The colour of a NEEDS YOU reason word: the detail kind's, else the
/// agent's own (the state in words).
fn reason_color(a: &Agent, word_is_detail: bool) -> &'static str {
    if word_is_detail {
        match a.detail_kind.as_str() {
            "ask" | "perm" => "yellow",
            "fail" => "red",
            "done" => "green",
            _ => "blue",
        }
    } else {
        a.color.unwrap_or("overlay")
    }
}

/// The key-bar's jump keys: `1–7`, or `1–9 01–03` past nine rows.
pub fn jump_keys(n: usize) -> Option<String> {
    let labels = hotkey_labels(n);
    let last = labels.last()?;
    Some(match n {
        1 => "1".into(),
        2..=9 => format!("1–{n}"),
        _ => format!("1–9 {}–{last}", labels[9]),
    })
}

/// Draw the whole frame for `now`; records what is on screen in `m`.
pub fn render(m: &mut Menu, buf: &mut Buffer, now: f64) {
    let area = buf.area;
    m.clicks.clear();
    if area.width == 0 || area.height == 0 {
        m.drawn = Default::default();
        m.preview_rows = 0;
        m.full_rows = 0;
        return;
    }
    for c in buf.content.iter_mut() {
        c.reset();
    }
    buf.set_style(area, on("text", "base"));
    let (w, h) = (area.width, area.height);
    if w < MIN_W || h < MIN_H {
        put(buf, 0, 0, w, "agents · too small", fg("overlay"));
        m.drawn = Default::default();
        m.preview_rows = 0;
        m.full_rows = 0;
        return;
    }
    let geo = Geo::new(w, h);
    let s1 = fg("surface1");
    let split = if m.peek_full.is_some() { None } else { geo.split };

    // Borders and separators.
    top_border(m, buf, w);
    for y in 1..h - 1 {
        put(buf, 0, y, w, "│", s1);
        put(buf, w - 1, y, w, "│", s1);
    }
    for (y, mid) in [(2, "┬"), (h - 3, "┴")] {
        put(buf, 0, y, w, "├", s1);
        for x in 1..w - 1 {
            put(buf, x, y, w, "─", s1);
        }
        put(buf, w - 1, y, w, "┤", s1);
        if let Some(sx) = split {
            put(buf, sx, y, w, mid, s1);
        }
    }
    if let Some(sx) = split {
        for y in geo.body.y..geo.body.bottom() {
            put(buf, sx, y, w, "│", s1);
        }
    }
    put(buf, 0, h - 1, w, "╰", s1);
    for x in 1..w - 1 {
        put(buf, x, h - 1, w, "─", s1);
    }
    put(buf, w - 1, h - 1, w, "╯", s1);

    tab_row(m, buf, w);
    m.full_rows = geo.body.height.saturating_sub(2) as usize;
    if let Some(t) = m.peek_full.clone() {
        full_peek(m, buf, geo.body, &t);
        // The list (and its numbers) stays as last drawn under a peek.
        m.preview_rows = 0;
    } else {
        list(m, buf, geo.list);
        match geo.preview {
            Some(r) => preview(m, buf, r),
            None => m.preview_rows = 0,
        }
    }
    footer(m, buf, w, h - 2, now);
}

fn top_border(m: &mut Menu, buf: &mut Buffer, w: u16) {
    let s1 = fg("surface1");
    let mut x = put(buf, 0, 0, w, "╭─ ", s1);
    x = put(buf, x, 0, w, "agents", bold(fg("text")));
    x = put(buf, x, 0, w, " ", s1);
    let v = &m.data.view;
    let banner = match watcher_problem(v.watcher_age) {
        Some(why) => Some(format!(" watcher {why} · r restarts it ")),
        None if v.client_gone => Some(" this menu's client is gone · esc closes ".to_string()),
        None => None,
    };
    let right_w = 8; // " ⌥W ✕ ─╮"
    if let Some(b) = banner {
        x = put(buf, x, 0, w.saturating_sub(right_w + 1), &b, bold(on("crust", "red")));
        x = put(buf, x, 0, w, " ", s1);
    }
    while x < w - right_w {
        x = put(buf, x, 0, w - right_w, "─", s1);
    }
    let mut rx = put(buf, w - right_w, 0, w, " ⌥W ", fg("overlay"));
    m.clicks.push((Rect::new(rx.saturating_sub(1), 0, 3, 1), Hit::Close));
    rx = put(buf, rx, 0, w, "✕", fg("red"));
    put(buf, rx, 0, w, " ─╮", s1);
}

/// How the chips are written: full or short titles, with or without their
/// counts, with or without the padding inside each chip.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ChipStyle {
    pub short: bool,
    pub counts: bool,
    pub pad: bool,
}

impl ChipStyle {
    /// From the roomiest to the most compact.
    pub const LEVELS: [ChipStyle; 4] = [
        ChipStyle { short: false, counts: true, pad: true },
        ChipStyle { short: true, counts: true, pad: true },
        ChipStyle { short: true, counts: false, pad: true },
        ChipStyle { short: true, counts: false, pad: false },
    ];

    /// One chip's (title part, count part).
    fn parts(self, t: Tab, n: usize) -> (String, String) {
        let title = if self.short { t.short_title() } else { t.title() };
        let p = if self.pad { " " } else { "" };
        let count = if self.counts { format!("{n}{p}") } else { String::new() };
        (format!("{p}{title}{p}"), count)
    }

    /// The whole chip row's width, from its first chip to its last.
    fn width(self, counts: &[usize]) -> u16 {
        let chips: u16 = Tab::ALL
            .iter()
            .zip(counts)
            .map(|(&t, &n)| {
                let (a, b) = self.parts(t, n);
                cells(&a) + cells(&b)
            })
            .sum();
        chips + Tab::ALL.len() as u16 - 1
    }
}

/// The search field's narrowest useful width (else it is not drawn).
pub const SEARCH_MIN_W: u16 = 10;

/// The roomiest chip style that fits between x = 2 and `end`, and whether
/// the search field still fits beside it. The search field gives way first
/// (`/` opens it anyway), then the chips shorten; while a search is open
/// the field stays and the chips shorten first.
pub fn chip_style(counts: &[usize], end: u16, searching: bool) -> (ChipStyle, bool) {
    let fits = |s: ChipStyle, search: bool| 2 + s.width(counts) + if search { 2 + SEARCH_MIN_W } else { 0 } <= end;
    let mut order: Vec<(ChipStyle, bool)> = Vec::new();
    if searching {
        order.extend(ChipStyle::LEVELS.iter().map(|&s| (s, true)));
        order.extend(ChipStyle::LEVELS.iter().map(|&s| (s, false)));
    } else {
        for s in ChipStyle::LEVELS {
            order.extend([(s, true), (s, false)]);
        }
    }
    order.into_iter().find(|&(s, se)| fits(s, se)).unwrap_or((ChipStyle::LEVELS[3], false))
}

fn tab_row(m: &mut Menu, buf: &mut Buffer, w: u16) {
    let y = 1;
    let end = w - 2;
    let mut x = 2;
    let counts: Vec<usize> = Tab::ALL.iter().map(|t| t.count(&m.data.view)).collect();
    let (style, search) = chip_style(&counts, end, m.searching || !m.query.is_empty());
    for (t, &n) in Tab::ALL.into_iter().zip(&counts) {
        let x0 = x;
        let (title, count) = style.parts(t, n);
        if t == m.tab {
            x = put(buf, x, y, end, &format!("{title}{count}"), bold(on("crust", "blue")));
        } else {
            x = put(buf, x, y, end, &title, on("sub", "surface0"));
            x = put(buf, x, y, end, &count, on(if n > 0 { t.hue() } else { "overlay" }, "surface0"));
        }
        m.clicks.push((Rect::new(x0, y, x - x0, 1), Hit::Tab(t)));
        x += 1;
    }
    // The search field, flush right.
    let room = end.saturating_sub(x + 1);
    let sw = room.min(34);
    if !search || sw < SEARCH_MIN_W {
        return;
    }
    let sx = end - sw;
    let field = on("overlay", "surface0");
    buf.set_style(Rect::new(sx, y, sw, 1), field);
    let mut cx = put(buf, sx, y, end, " / ", field);
    if m.query.is_empty() && !m.searching {
        put(buf, cx, y, end, "search…", field);
    } else {
        let room = (end - cx).saturating_sub(2) as usize;
        let q = sanitize(&m.query);
        // The tail of a long query, so the cursor end stays visible.
        let mut shown: Vec<char> = Vec::new();
        for c in q.chars().rev() {
            if dwidth(&shown.iter().rev().collect::<String>()) + dwidth(&c.to_string()) > room {
                break;
            }
            shown.push(c);
        }
        let shown: String = shown.into_iter().rev().collect();
        cx = put(buf, cx, y, end, &shown, on("text", "surface0"));
        if m.searching {
            put(buf, cx, y, end, "▏", on("peach", "surface0"));
        }
    }
    m.clicks.push((Rect::new(sx, y, sw, 1), Hit::Search));
}

/// The left list: rows, the selected need's reason line, the scrollbar.
fn list(m: &mut Menu, buf: &mut Buffer, r: Rect) {
    let lw = m.labels.values().map(|l| l.len()).max().unwrap_or(1);
    let mut sel_pos = m.sel.as_ref().and_then(|k| m.rows.iter().position(|r| r.key().as_ref() == Some(k)));
    // Each row's reason line, shown under it while selected: (word, its
    // colour, the reason text). NEEDS YOU rows, and the Active tab's
    // attention rows (which stay in their fixed slot instead).
    let reasons: Vec<Option<(String, &'static str, String)>> = m
        .rows
        .iter()
        .map(|row| {
            m.reason_of(row).map(|n| {
                (n.reason_word.clone(), reason_color(&n.agent, !n.reason.is_empty()), n.reason.clone())
            })
        })
        .collect();
    // Display lines: (row, is the reason line).
    let expand = |sel: Option<usize>| -> Vec<(usize, bool)> {
        let mut v = Vec::new();
        for (i, r) in reasons.iter().enumerate() {
            v.push((i, false));
            if Some(i) == sel && r.is_some() {
                v.push((i, true));
            }
        }
        v
    };
    let mut lines = expand(sel_pos);
    let hgt = r.height as usize;
    let len = lines.len();
    if m.follow {
        if let Some(sp) = sel_pos {
            let first = lines.iter().position(|l| l.0 == sp).unwrap_or(0);
            let last = lines.iter().rposition(|l| l.0 == sp).unwrap_or(first);
            if first < m.top {
                // Scrolling up onto a row: bring the headers right above it
                // too (a section, a group rule), while the row still fits.
                m.top = first;
                while m.top > 0 && m.rows[lines[m.top - 1].0].key().is_none() && last < m.top - 1 + hgt {
                    m.top -= 1;
                }
            } else if last >= m.top + hgt {
                m.top = last + 1 - hgt;
            }
        }
    }
    m.top = m.top.min(len.saturating_sub(hgt));
    if !m.follow {
        // After a wheel scroll: a selection scrolled off screen moves to the
        // nearest row still on it, so keys never act on a row out of sight.
        if let Some(sp) = sel_pos {
            let first = lines.iter().position(|l| l.0 == sp).unwrap_or(0);
            let visible: Vec<usize> = lines
                .iter()
                .skip(m.top)
                .take(hgt)
                .filter(|l| !l.1 && m.rows[l.0].key().is_some())
                .map(|l| l.0)
                .collect();
            let pick = if first < m.top { visible.first() } else { visible.last() };
            if !(m.top..m.top + hgt).contains(&first) {
                if let Some(&ri) = pick {
                    m.sel = m.rows[ri].key();
                    sel_pos = Some(ri);
                    lines = expand(sel_pos);
                    // A need's reason line shifts what follows it: keep the
                    // new selection (both its lines) inside the window.
                    let f = lines.iter().position(|l| l.0 == ri).unwrap_or(0);
                    let l = lines.iter().rposition(|l| l.0 == ri).unwrap_or(f);
                    if f < m.top {
                        m.top = f;
                    } else if l >= m.top + hgt {
                        m.top = l + 1 - hgt;
                    }
                }
            }
        }
    }
    let len = lines.len();
    m.page = hgt.saturating_sub(1).max(1);
    let cw = r.width.saturating_sub(1); // the last column is the scrollbar's
    let end = r.x + cw;

    let mut drawn: Vec<Target> = Vec::new();
    let mut labels: HashMap<usize, String> = HashMap::new();
    for (k, &(ri, detail)) in lines.iter().skip(m.top).take(hgt).enumerate() {
        let y = r.y + k as u16;
        let row = &m.rows[ri];
        let selected = Some(ri) == sel_pos;
        let rect = Rect::new(r.x, y, cw, 1);
        if selected {
            buf.set_style(rect, Style::new().bg(color("surface1")));
        }
        let label = m.labels.get(&ri).map(String::as_str);
        if detail {
            if let Some((word, wcol, reason)) = &reasons[ri] {
                let x = r.x + 1 + lw as u16 + 1 + 2;
                let cx = put(buf, x, y, end, word, fg(wcol));
                let rest = clip(&plain(reason), end.saturating_sub(cx + 1) as usize);
                put(buf, cx + 1, y, end, &rest, fg("sub"));
            }
        } else {
            draw_row(buf, r.x, y, cw, row, label, lw, selected, m.tab);
            if let (Some(t), Some(l)) = (row.target(), label) {
                labels.insert(drawn.len(), l.to_string());
                drawn.push(t);
            }
        }
        if let Some(t) = row.target() {
            m.clicks.push((rect, Hit::Row(t)));
        } else if matches!(row, Row::More(_)) {
            m.clicks.push((rect, Hit::More));
        }
    }
    if len > hgt && hgt > 0 {
        let x = r.x + cw;
        let thumb = (hgt * hgt / len).max(1);
        let pos = m.top * (hgt - thumb) / (len - hgt);
        for k in 0..hgt {
            let st = if (pos..pos + thumb).contains(&k) { fg("surface1") } else { fg("surface0") };
            put(buf, x, r.y + k as u16, x + 1, "▕", st);
        }
    }
    m.drawn = (drawn, labels);
    // What the selection is, as this frame shows it (keys act on it only
    // while it is still listed).
    m.drawn_sel = m.selected_target();
}

/// One list row (not the reason line).
#[allow(clippy::too_many_arguments)]
fn draw_row(buf: &mut Buffer, x0: u16, y: u16, cw: u16, row: &Row, label: Option<&str>, lw: usize, sel: bool,
    tab: Tab) {
    let ov = fg("overlay");
    match row {
        Row::Section { title, color: c, rule } => {
            let left = [seg(format!(" {title} "), bold(fg(c)))];
            line(buf, x0, y, cw, &left, &[], rule.then(|| ("─", fg("surface0"))));
        }
        Row::Group { name, branch, counts, .. } => {
            let mut left = vec![seg(format!(" {} ", sanitize(name)), bold(fg("text")))];
            if !branch.is_empty() {
                left.push(seg(format!("⌥ {} ", clip(branch, 24)), fg("mauve")));
            }
            let toks: Vec<String> = counts.nonzero().iter().map(|(k, n)| format!("{}{n}", k.glyph())).collect();
            let right = [seg(format!(" {} ", toks.join(" ")), ov)];
            line(buf, x0, y, cw, &left, &right, Some(("─", fg("surface0"))));
        }
        Row::Need(n) => {
            let a = &n.agent;
            let age = format!("{:>4}", a.age);
            let right = [seg(sanitize(&a.session), ov), seg(format!(" {age}"), ov)];
            window_row(buf, x0, y, cw, label, lw, a, a.glyph, a.color.unwrap_or("overlay"), &right, sel, false);
        }
        Row::Agent(a) => {
            let mut right = Vec::new();
            if a.is_current {
                right.push(seg("here", fg("blue")));
            }
            right.push(seg(format!(" {:>4}", a.age), ov));
            window_row(buf, x0, y, cw, label, lw, a, a.glyph, a.color.unwrap_or("overlay"), &right, sel, false);
        }
        Row::Parked(p) => {
            let mut words = String::new();
            if tab == Tab::Parked && cw >= 60 {
                words = format!("{} · ", p.state_words);
            }
            let right = [seg(format!("{words}{}", sanitize(&p.origin)), ov), seg(format!(" {:>4}", p.age), ov)];
            window_row(buf, x0, y, cw, label, lw, &p.agent, "▪", "overlay", &right, sel, true);
        }
        Row::More(k) => {
            let x = x0 + 1 + lw as u16 + 1 + 2;
            put(buf, x, y, x0 + cw, &format!("+{k} more · ⇥ Parked"), ov);
        }
        Row::Empty(why) => {
            put(buf, x0 + 1, y, x0 + cw, why, ov);
        }
    }
}

/// A window row: label, state glyph, title (+ flag), and `right` flush right.
#[allow(clippy::too_many_arguments)]
fn window_row(buf: &mut Buffer, x0: u16, y: u16, cw: u16, label: Option<&str>, lw: usize, a: &Agent, glyph: &str,
    gcol: &str, right: &[Seg], sel: bool, parked: bool) {
    let end = x0 + cw;
    let lab = format!("{:>lw$}", label.unwrap_or(""));
    let mut x = put(buf, x0 + 1, y, end, &lab, if sel { bold(fg("text")) } else { fg("overlay") });
    x = put(buf, x + 1, y, end, glyph, fg(gcol));
    x += 1;
    let flag = a.flag.map(|(f, c)| {
        let g = match f {
            crate::model::Flag::Workflow => crate::model::STRIP_GEAR,
            crate::model::Flag::Cua => crate::model::STRIP_MOUSE,
        };
        (format!(" {g}"), c)
    });
    let fw = flag.as_ref().map_or(0, |(s, _)| cells(s));
    let rw = segs_w(right);
    let room = end.saturating_sub(x + rw + fw + 1) as usize;
    // `proj/Title`: the project dim, and dropped first when room is short
    // (agent-roster.py `fit_label`, as the strip drew it).
    let (proj, title) = match a.title.strip_prefix("~/") {
        Some(rest) => (String::new(), clip(&sanitize(rest), room)),
        None => fit_label(&a.title, room),
    };
    x = put(buf, x, y, end, &proj, fg("overlay"));
    let tcol = if parked || (a.cat.is_none() && !a.is_current) {
        "overlay"
    } else if a.cat == Some(crate::model::Cat::Idle) {
        "sub"
    } else {
        "text"
    };
    let tst = if sel { bold(fg(if parked { "text" } else { tcol })) } else { fg(tcol) };
    x = put(buf, x, y, end, &title, tst);
    if let Some((s, c)) = flag {
        put(buf, x, y, end, &s, fg(c));
    }
    let mut rx = end.saturating_sub(rw);
    for s in right {
        rx = put(buf, rx, y, end, &s.0, s.1);
    }
}

/// The right column: title, meta, the live tail, the buttons.
fn preview(m: &mut Menu, buf: &mut Buffer, r: Rect) {
    m.preview_rows = r.height.saturating_sub(4) as usize;
    let ov = fg("overlay");
    let Some(row) = m.selected().cloned() else {
        put(buf, r.x + 1, r.y, r.right(), "nothing selected", ov);
        return;
    };
    let Some(a) = row.agent() else { return };
    let need = m.reason_of(&row).map(|n| (n.reason_word.clone(), !n.reason.is_empty()));
    let (word, wcol, age) = match (&row, need) {
        (_, Some((wd, is_detail))) => (wd, reason_color(a, is_detail), a.age.clone()),
        (Row::Parked(p), None) => (p.state_words.clone(), "overlay", p.age.clone()),
        _ => (
            if a.state_words.is_empty() { "shell".to_string() } else { a.state_words.to_string() },
            a.color.unwrap_or("overlay"),
            a.age.clone(),
        ),
    };
    let glyph = if matches!(row, Row::Parked(_)) { "▪" } else { a.glyph };
    let mut right = vec![seg(format!("{glyph} {} ", clip(&plain(&word), 24)), fg(wcol))];
    if !age.is_empty() {
        right.push(seg(format!("· {age} "), ov));
    }
    let title_room = r.width.saturating_sub(segs_w(&right) + 2) as usize;
    let title = clip(&sanitize(shown_title(&a.title)), title_room);
    line(buf, r.x, r.y, r.width, &[seg(format!(" {title}"), bold(fg("text")))], &right, None);

    // session:index · ✳ claude · ⌥ branch · path
    let mut meta = vec![seg(format!(" {}", a.place()), ov)];
    if let Row::Parked(p) = &row {
        if !p.origin.is_empty() {
            meta.push(seg(format!(" · from {}", p.origin), ov));
        }
    }
    if let Some(g) = a.kind_glyph {
        meta.push(seg(format!(" · {g} {}", a.kind), ov));
    }
    let branch = m.data.branches.get(&a.path).cloned().unwrap_or_default();
    if !branch.is_empty() {
        meta.push(seg(" · ", ov));
        meta.push(seg(format!("⌥ {branch}"), fg("mauve")));
    }
    if !a.path.is_empty() {
        meta.push(seg(format!(" · {}", sanitize(&tilde(&a.path))), ov));
    }
    line(buf, r.x, r.y + 1, r.width.saturating_sub(1), &meta, &[], None);
    let rule: String = "╌".repeat(r.width.saturating_sub(2) as usize);
    put(buf, r.x + 1, r.y + 2, r.right(), &rule, fg("surface0"));

    let key = (a.window_id.clone(), a.session.clone());
    let body = Rect::new(r.x + 1, r.y + 3, r.width.saturating_sub(2), m.preview_rows as u16);
    peek_lines(m, buf, body, &key, &a.place());

    // The buttons, each bound to this row.
    let Some(target) = row.target() else { return };
    let parked = matches!(row, Row::Parked(_));
    let mut btns = vec![(Button::Go, if parked { "⏎ unpark" } else { "⏎ go to it" }, "text"),
        (Button::Peek, "p full peek", "sub")];
    if !parked {
        btns.push((Button::Park, "s park", "sub"));
    }
    btns.push((Button::Close, if parked { "x discard" } else { "x close agent" }, "red"));
    let y = r.bottom() - 1;
    let mut x = r.x + 1;
    for (b, t, c) in btns {
        let s = format!(" {t} ");
        let bw = cells(&s);
        if x + bw > r.right() {
            break;
        }
        put(buf, x, y, r.right(), &s, on(c, "surface0"));
        m.clicks.push((Rect::new(x, y, bw, 1), Hit::Button(b, target.clone())));
        x += bw + 1;
    }
}

/// The captured tail of a pane inside `r` (top-aligned, its own colours).
fn peek_lines(m: &Menu, buf: &mut Buffer, r: Rect, key: &(String, String), place: &str) {
    let ov = fg("overlay");
    match m.peeks.get(key) {
        None => {
            put(buf, r.x, r.y, r.right(), "capturing…", ov);
        }
        Some(p) => match &p.lines {
            None => {
                put(buf, r.x, r.y, r.right(), &format!("can't capture {place}"), ov);
            }
            Some(ls) if ls.is_empty() => {
                put(buf, r.x, r.y, r.right(), "(empty)", ov);
            }
            Some(ls) => {
                let k = ls.len().saturating_sub(r.height as usize);
                for (i, l) in ls[k..].iter().enumerate() {
                    let y = r.y + i as u16;
                    let mut x = r.x;
                    for (st, t) in &l.spans {
                        x = put(buf, x, y, r.right(), t, *st);
                    }
                }
            }
        },
    }
}

/// `p`: the selected pane over the whole body (agent-roster.py `peek_panel`).
fn full_peek(m: &mut Menu, buf: &mut Buffer, b: Rect, t: &Target) {
    let ov = fg("overlay");
    let end = b.right();
    let shown = clip(&sanitize(shown_title(&t.title)), b.width.saturating_sub(20) as usize);
    let title = format!(" peek · {} {shown} ", t.place());
    let mut x = put(buf, b.x, b.y, end, "╭─", ov);
    x = put(buf, x, b.y, end - 1, &title, fg("sub"));
    while x < end - 1 {
        x = put(buf, x, b.y, end - 1, "─", ov);
    }
    put(buf, end - 1, b.y, end, "╮", ov);
    for y in b.y + 1..b.bottom() - 1 {
        put(buf, b.x, y, end, "│", ov);
        put(buf, end - 1, y, end, "│", ov);
    }
    put(buf, b.x, b.bottom() - 1, end, "╰", ov);
    for x in b.x + 1..end - 1 {
        put(buf, x, b.bottom() - 1, end, "─", ov);
    }
    put(buf, end - 1, b.bottom() - 1, end, "╯", ov);
    let inner = Rect::new(b.x + 2, b.y + 1, b.width.saturating_sub(4), b.height.saturating_sub(2));
    peek_lines(m, buf, inner, &(t.win.clone(), t.session.clone()), &t.place());
}

/// The key bar, or what replaces it.
fn footer(m: &Menu, buf: &mut Buffer, w: u16, y: u16, now: f64) {
    let x = 2;
    let end = w - 2;
    let ov = fg("overlay");
    let text = fg("text");
    let say = |buf: &mut Buffer, parts: &[Seg]| {
        let mut cx = x;
        for p in parts {
            cx = put(buf, cx, y, end, &p.0, p.1);
        }
    };
    if m.peek_full.is_some() {
        return say(buf, &[seg("any key closes the peek", ov)]);
    }
    if let Some(c) = &m.confirm {
        let room = (end - x).saturating_sub(cells(c.verb()) + 8) as usize;
        return say(buf, &[seg(format!("{} {}? ", c.verb(), clip(&sanitize(shown_title(&c.target.title)), room)), fg("yellow")),
            seg("y/n", text)]);
    }
    let keys: Vec<(String, &str)> = if m.searching {
        vec![("⏎".into(), "keep"), ("esc".into(), "clear"), ("↑↓".into(), "move"), ("⇥".into(), "filter")]
    } else if !m.digits.is_empty() {
        return say(buf, &[seg(format!("{}▏", m.digits), fg("peach")), seg("  next digit · esc cancel", ov)]);
    } else if !m.msg.is_empty() && now < m.msg_until {
        return say(buf, &[seg(sanitize(&m.msg), fg("sky"))]);
    } else if m.digits_dead {
        return say(buf, &[seg("digits ignored until another key", fg("yellow")), seg("  (esc clears)", ov)]);
    } else {
        let mut v: Vec<(String, &str)> = vec![("⏎".into(), "go")];
        if let Some(j) = jump_keys(m.labels.len()) {
            v.push((j, "jump"));
        }
        v.extend([("⇥".into(), "filter"), ("/".into(), "search"), ("p".into(), "peek"), ("s".into(), "park"),
            ("x".into(), "close"), ("⌥S".into(), "next"), ("⌥X".into(), "back"), ("⌥W".into(), "close")]);
        v
    };
    // Drop the least needed keys until the bar fits (go and ⌥W stay).
    let item_w = |k: &str, wd: &str| cells(k) + 2 + cells(wd) + 2;
    let mut shown = keys;
    let drop_order = ["back", "next", "filter", "park", "search", "peek", "close", "jump", "move"];
    let total = |v: &[(String, &str)]| v.iter().map(|(k, wd)| item_w(k, wd)).sum::<u16>();
    for d in drop_order {
        if total(&shown) <= end - x {
            break;
        }
        shown.retain(|(_, wd)| *wd != d);
    }
    let mut cx = x;
    for (k, wd) in shown {
        cx = put(buf, cx, y, end, &format!(" {k} "), on("text", "surface0"));
        cx = put(buf, cx, y, end, &format!(" {wd} "), ov);
    }
}

fn sgr_color(c: Color, bg: bool) -> Option<String> {
    let base = if bg { 40 } else { 30 };
    Some(match c {
        Color::Reset => return None,
        Color::Black => format!("{base}"),
        Color::Red => format!("{}", base + 1),
        Color::Green => format!("{}", base + 2),
        Color::Yellow => format!("{}", base + 3),
        Color::Blue => format!("{}", base + 4),
        Color::Magenta => format!("{}", base + 5),
        Color::Cyan => format!("{}", base + 6),
        Color::Gray => format!("{}", base + 7),
        Color::DarkGray => format!("{}", base + 60),
        Color::LightRed => format!("{}", base + 61),
        Color::LightGreen => format!("{}", base + 62),
        Color::LightYellow => format!("{}", base + 63),
        Color::LightBlue => format!("{}", base + 64),
        Color::LightMagenta => format!("{}", base + 65),
        Color::LightCyan => format!("{}", base + 66),
        Color::White => format!("{}", base + 67),
        Color::Indexed(n) => format!("{};5;{n}", base + 8),
        Color::Rgb(r, g, b) => format!("{};2;{r};{g};{b}", base + 8),
    })
}

/// A buffer as text lines: with truecolour SGR (`ansi`), or plain with
/// trailing blanks trimmed. Wide characters' hidden cells are skipped.
pub fn dump(buf: &Buffer, ansi: bool) -> Vec<String> {
    let mut out = Vec::new();
    for y in 0..buf.area.height {
        let mut s = String::new();
        let mut last: Option<(Color, Color, Modifier)> = None;
        let mut skip = 0usize;
        for x in 0..buf.area.width {
            let c = &buf[(buf.area.x + x, buf.area.y + y)];
            if skip > 0 {
                skip -= 1;
                continue;
            }
            let sym = c.symbol();
            skip = unicode_width::UnicodeWidthStr::width(sym).saturating_sub(1);
            if ansi && last != Some((c.fg, c.bg, c.modifier)) {
                let mut p = vec!["0".to_string()];
                for (m, code) in [(Modifier::BOLD, "1"), (Modifier::DIM, "2"), (Modifier::ITALIC, "3"),
                    (Modifier::UNDERLINED, "4"), (Modifier::REVERSED, "7"), (Modifier::CROSSED_OUT, "9")] {
                    if c.modifier.contains(m) {
                        p.push(code.into());
                    }
                }
                p.extend(sgr_color(c.fg, false));
                p.extend(sgr_color(c.bg, true));
                s.push_str(&format!("\x1b[{}m", p.join(";")));
                last = Some((c.fg, c.bg, c.modifier));
            }
            s.push_str(sym);
        }
        if ansi {
            s.push_str("\x1b[0m");
        } else {
            s.truncate(s.trim_end().len());
        }
        out.push(s);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::super::rows::tests::data;
    use super::super::rows::RowKey;
    use super::super::state::{Menu, NoEffects, PeekData};
    use super::*;
    use crate::ansi::parse_line;

    fn frame(m: &mut Menu, w: u16, h: u16) -> Vec<String> {
        let mut buf = Buffer::empty(Rect::new(0, 0, w, h));
        render(m, &mut buf, 1000.0);
        dump(&buf, false)
    }

    fn menu() -> Menu {
        Menu::new(data(), Tab::All)
    }

    fn cell_bg(m: &mut Menu, w: u16, h: u16, x: u16, y: u16) -> Color {
        let mut buf = Buffer::empty(Rect::new(0, 0, w, h));
        render(m, &mut buf, 1000.0);
        buf[(x, y)].bg
    }

    #[test]
    fn layout_110x34() {
        let mut m = menu();
        m.peeks.insert(("@6".into(), "work".into()), PeekData {
            lines: Some(vec![parse_line("\x1b[31m529 overloaded\x1b[0m"), parse_line("retry?")]),
            n: 10,
        });
        let f = frame(&mut m, 110, 34);
        assert_eq!(f.len(), 34);
        for l in &f {
            assert!(dwidth(l) <= 110, "{l}");
        }
        assert!(f[0].starts_with("╭─ agents ─") && f[0].ends_with(" ⌥W ✕ ─╮"), "{}", f[0]);
        assert!(f[1].contains(" All 7 ") && f[1].contains(" Needs you 2 ") && f[1].contains(" Parked 5 "));
        assert!(f[1].contains("/ search…"));
        assert!(f[2].starts_with("├") && f[2].contains("┬") && f[2].ends_with("┤"));
        assert!(f[3].starts_with("│ NEEDS YOU"));
        // The first need, selected: its reason line under it, and the preview.
        assert!(f[4].contains("1 ✕ Island resize") && f[4].contains("work"), "{}", f[4]);
        assert!(f[5].contains("failed"), "{}", f[5]); // no detail: the state in words
        assert!(f[3].contains("Island resize") && f[3].contains("✕ failed · 11m"), "{}", f[3]);
        assert!(f[4].contains("work:1"));
        assert!(f[6].contains("529 overloaded") && f[7].contains("retry?"), "{}", f[6]);
        assert!(f[6].contains("2 ◉ Kua Yu focus timing"), "{}", f[6]);
        assert!(f[7].contains(" main ") && f[7].contains("◉1 ◐2 ○1"), "{}", f[7]);
        assert!(f.iter().any(|l| l.contains("Tmux Agent Sidebar") && l.contains("here")));
        assert!(f.iter().any(|l| l.contains("Handy.app Speech ⚙")));
        assert!(f.iter().any(|l| l.contains("PARKED 5")));
        assert!(f.iter().any(|l| l.contains("+2 more · ⇥ Parked")));
        assert!(f[30].contains("⏎ go to it") && f[30].contains("x close agent"), "{}", f[30]);
        assert!(f[31].contains("┴"));
        assert!(f[32].contains(" ⏎  go ") && f[32].contains("1–9 01–04  jump") && f[32].contains("⌥S  next  ⌥X  back  ⌥W  close"),
            "{}", f[32]);
        assert!(f[33].starts_with("╰") && f[33].ends_with("╯"));
        // The selected row's background.
        assert_eq!(cell_bg(&mut m, 110, 34, 3, 4), color("surface1"));
        assert_eq!(cell_bg(&mut m, 110, 34, 3, 7), color("base"));
        assert_eq!(m.preview_rows, 24);
    }

    #[test]
    fn layout_80x24_hides_the_preview() {
        let mut m = menu();
        let f = frame(&mut m, 80, 24);
        assert_eq!(f.len(), 24);
        assert!(!f[2].contains("┬"));
        assert!(f.iter().all(|l| !l.contains("go to it")));
        assert_eq!(m.preview_rows, 0);
        assert_eq!(m.peek_want(), None);
        // The list takes the full width: ages flush right before the scrollbar.
        assert!(f[4].ends_with("work  11m▕│"), "{:?}", f[4]);
        // Key bar fits, dropping keys from the right of the drop order.
        assert!(f[22].contains("⏎  go") && f[22].contains("⌥W  close"), "{}", f[22]);
        assert!(dwidth(&f[22]) <= 80);
        // 18 body rows < 21 lines: it scrolls, with a scrollbar.
        assert!(f[3..21].iter().any(|l| l.contains('▕')));
    }

    /// The default tab: fixed groups, no NEEDS YOU section, the selected
    /// attention row's reason line under it in place.
    #[test]
    fn active_tab_110x34() {
        let mut m = Menu::new(data(), Tab::Active);
        let f = frame(&mut m, 110, 34);
        assert!(f[1].starts_with("│  Active 4   All 7 ") && f[1].contains(" Needs you 2 "), "{}", f[1]);
        assert!(!f.iter().any(|l| l.contains("NEEDS YOU") || l.contains("PARKED")));
        assert!(f[3].starts_with("│ main ") && f[3].contains("◉1 ◐2 ○1"), "{}", f[3]);
        assert!(f[4].contains("1 ◉ Kua Yu focus timing"), "{}", f[4]);
        assert!(f[5].contains("2 ◐ Tmux Agent Sidebar") && f[5].contains("here"), "{}", f[5]);
        assert!(f[6].contains("3 ◐ Handy.app Speech ⚙"), "{}", f[6]);
        assert!(f[7].starts_with("│ work "), "{}", f[7]);
        // Selected: the first need (work:1), its reason line right under it.
        assert!(f[8].contains("4 ✕ Island resize"), "{}", f[8]);
        assert!(f[9].contains("failed") && !f[9].contains("Island"), "{}", f[9]);
        assert!(f[10].contains("5 ◐ Handy.app Speech"), "{}", f[10]);
        assert_eq!(cell_bg(&mut m, 110, 34, 3, 8), color("surface1"));
        // The preview names the reason too.
        assert!(f[3].contains("Island resize") && f[3].contains("✕ failed · 11m"), "{}", f[3]);
        // Another attention row: its detail; a working row: no reason line.
        m.sel = Some(RowKey::Agent("main".into(), "@1".into()));
        let f = frame(&mut m, 110, 34);
        assert!(f[5].contains("asks Which deck?"), "{}", f[5]);
        assert!(f[3].contains("◉ asks"), "{}", f[3]);
        m.sel = Some(RowKey::Agent("main".into(), "@2".into()));
        let f = frame(&mut m, 110, 34);
        assert!(f[6].contains("3 ◐ Handy.app"), "{}", f[6]);
        // Labels on screen are the rows' labels.
        assert_eq!(m.drawn.0.len(), 5);
        assert_eq!(m.drawn.0[3].key, RowKey::Agent("work".into(), "@6".into()));
        assert_eq!(m.drawn.1[&3], "4");
        // The empty state.
        let mut d = data();
        d.view.needs.clear();
        for a in d.view.spaces.iter_mut().flat_map(|s| s.agents.iter_mut()) {
            a.cat = Some(crate::model::Cat::Idle);
        }
        m.set_data(d);
        let f = frame(&mut m, 110, 34);
        assert!(f[3].contains("│ nothing working or waiting · ⇥ for all"), "{}", f[3]);
    }

    /// Narrow popups: the search field gives way, then the chips shorten,
    /// then lose their counts, then their padding; every chip stays whole.
    #[test]
    fn chip_row_fits_narrow_widths() {
        for (w, want, search) in [
            (110, &[" Active 4 ", " All 7 ", " Needs you 2 ", " Working 2 ", " Idle 3 ", " Parked 5 "][..], true),
            (80, &[" Needs you 2 ", " Parked 5 "][..], true),
            (72, &[" Needs you 2 ", " Working 2 ", " Parked 5 "][..], false),
            (64, &[" Active 4 ", " Needs 2 ", " Work 2 ", " Idle 3 ", " Parked 5 "][..], false),
            (58, &[" Active ", " All ", " Needs ", " Work ", " Idle ", " Parked "][..], false),
            (40, &["Active", "All", "Needs", "Work", "Idle", "Parked"][..], false),
        ] {
            let mut m = Menu::new(data(), Tab::Active);
            let f = frame(&mut m, w, 24);
            for chip in want {
                assert!(f[1].contains(chip), "{w}: {chip:?} in {:?}", f[1]);
            }
            assert_eq!(f[1].contains("search…"), search, "{w}: {:?}", f[1]);
            if w <= 58 {
                assert!(!f[1].chars().any(|c| c.is_ascii_digit()), "{w}: counts dropped: {:?}", f[1]);
            }
            // Every chip is clickable, whole, inside the border.
            let chips: Vec<Rect> =
                m.clicks.iter().filter(|(_, h)| matches!(h, Hit::Tab(_))).map(|(r, _)| *r).collect();
            assert_eq!(chips.len(), 6);
            assert!(chips.iter().all(|r| r.width > 0 && r.right() <= w - 2), "{w}: {chips:?}");
        }
        // An open search keeps its field; the chips shorten first.
        let mut m = Menu::new(data(), Tab::Active);
        m.searching = true;
        let f = frame(&mut m, 64, 24);
        assert!(f[1].contains(" Parked ") && f[1].contains(" / ") && f[1].contains('▏'), "{:?}", f[1]);
    }

    #[test]
    fn needs_tab_80x24() {
        let mut m = Menu::new(data(), Tab::Needs);
        let f = frame(&mut m, 80, 24);
        assert!(f[1].contains(" Needs you 2 "));
        assert!(f[3].contains("NEEDS YOU"));
        assert!(f[4].contains("1 ✕ Island resize"));
        assert!(f[6].contains("2 ◉ Kua Yu"));
        assert!(!f.iter().any(|l| l.contains("Tmux Agent Sidebar")));
    }

    #[test]
    fn selection_stays_visible_when_scrolling() {
        let mut m = menu();
        for _ in 0..30 {
            m.act(crate::menu::keys::Key::Down, &mut NoEffects);
            let f = frame(&mut m, 80, 14); // 8 body rows
            let t = m.selected_target().unwrap();
            assert!(m.drawn.0.contains(&t), "{t:?} not drawn:\n{}", f.join("\n"));
        }
        assert_eq!(m.sel, Some(RowKey::Parked("@12".into())));
        assert!(m.top > 0);
        for _ in 0..30 {
            m.act(crate::menu::keys::Key::Up, &mut NoEffects);
            frame(&mut m, 80, 14);
            assert!(m.drawn.0.contains(&m.selected_target().unwrap()));
        }
        assert_eq!(m.top, 0);
        // The wheel moves the view; a selection scrolled off screen moves to
        // the nearest row still on it, so ⏎ always means a visible row.
        let wheel = |d: u16| crate::menu::keys::Key::Mouse(crate::menu::keys::Mouse { button: d, col: 5, row: 5, press: true });
        m.act(wheel(65), &mut NoEffects);
        frame(&mut m, 80, 14);
        assert!(m.top > 0);
        let t = m.selected_target().unwrap();
        assert!(m.drawn.0.contains(&t));
        for _ in 0..5 {
            m.act(wheel(65), &mut NoEffects);
            frame(&mut m, 80, 14);
            let t = m.selected_target().unwrap();
            assert!(m.drawn.0.contains(&t), "{t:?} off screen at top {}", m.top);
        }
        let mut fx = crate::menu::state::tests::Rec::default();
        let t = m.selected_target().unwrap();
        assert!(m.act(crate::menu::keys::Key::Enter, &mut fx));
        assert_eq!(fx.calls, [format!("go {} {}", t.win, t.session)]);
        // Scrolling back up pulls it down from the bottom edge the same way.
        for _ in 0..6 {
            m.act(wheel(64), &mut NoEffects);
            frame(&mut m, 80, 14);
            assert!(m.drawn.0.contains(&m.selected_target().unwrap()));
        }
        assert_eq!(m.top, 0);
    }

    /// A preview button acts on the row it was drawn for, not on whatever
    /// the selection became since; a row gone since says so.
    #[test]
    fn buttons_act_on_their_drawn_row() {
        let mut fx = crate::menu::state::tests::Rec::default();
        let mut m = menu();
        frame(&mut m, 110, 34); // buttons drawn for @6 (the first need)
        let (r, _) = m.clicks.iter().find(|(_, h)| matches!(h, Hit::Button(Button::Go, _))).cloned().unwrap();
        m.act(crate::menu::keys::Key::Char('j'), &mut fx); // selection moves; no redraw yet
        let click = crate::menu::keys::Key::Mouse(crate::menu::keys::Mouse { button: 0, col: r.x, row: r.y, press: true });
        assert!(m.act(click, &mut fx));
        assert_eq!(fx.calls, ["go @6 work"]);
        // The row went away before the click.
        let mut d = data();
        d.view.needs.clear();
        m.set_data(d);
        assert!(!m.act(click, &mut fx));
        assert_eq!(m.msg, "that tab moved · pick it again");
        assert_eq!(fx.calls.len(), 1);
    }

    #[test]
    fn zero_sized_frames() {
        let mut m = menu();
        assert!(frame(&mut m, 10, 0).is_empty());
        assert_eq!(frame(&mut m, 0, 10).len(), 10);
        assert!(m.drawn.0.is_empty() && m.clicks.is_empty());
    }

    #[test]
    fn click_map() {
        let mut fx = crate::menu::state::tests::Rec::default();
        let mut m = menu();
        frame(&mut m, 110, 34);
        let click = |col, row| crate::menu::keys::Key::Mouse(crate::menu::keys::Mouse { button: 0, col, row, press: true });
        // Chips.
        let (r, _) = m.clicks.iter().find(|(_, h)| *h == Hit::Tab(Tab::Parked)).cloned().unwrap();
        assert!(!m.act(click(r.x + 1, r.y), &mut fx));
        assert_eq!(m.tab, Tab::Parked);
        let (r, _) = m.clicks.iter().find(|(_, h)| *h == Hit::Tab(Tab::All)).cloned().unwrap();
        m.act(click(r.x, r.y), &mut fx);
        frame(&mut m, 110, 34);
        // The search field.
        let (r, _) = m.clicks.iter().find(|(_, h)| *h == Hit::Search).cloned().unwrap();
        m.act(click(r.x + 2, 1), &mut fx);
        assert!(m.searching);
        m.act(crate::menu::keys::Key::Esc, &mut fx);
        // A row: the first click selects, the second goes.
        let f = frame(&mut m, 110, 34);
        let y = f.iter().position(|l| l.contains("Kua Yu focus timing") && l.contains("main")).unwrap() as u16;
        assert!(!m.act(click(10, y), &mut fx));
        assert_eq!(m.sel, Some(RowKey::Need("@1".into())));
        assert!(fx.calls.is_empty());
        frame(&mut m, 110, 34);
        assert!(m.act(click(10, y), &mut fx));
        assert_eq!(fx.calls, ["go @1 main"]);
        // Buttons: park asks first; any click cancels the y/n.
        let (r, _) = m.clicks.iter().find(|(_, h)| matches!(h, Hit::Button(Button::Park, _))).cloned().unwrap();
        m.act(click(r.x, r.y), &mut fx);
        assert!(m.confirm.is_some());
        m.act(click(0, 0), &mut fx);
        assert!(m.confirm.is_none());
        let (r, _) = m.clicks.iter().find(|(_, h)| matches!(h, Hit::Button(Button::Peek, _))).cloned().unwrap();
        m.act(click(r.x, r.y), &mut fx);
        assert!(m.peek_full.is_some());
        let f = frame(&mut m, 110, 34);
        assert!(f[3].contains("╭─ peek · main:1 Kua Yu focus timing"), "{}", f[3]);
        assert!(f[32].contains("any key closes the peek"));
        m.act(click(50, 10), &mut fx); // a click closes it
        assert!(m.peek_full.is_none());
        frame(&mut m, 110, 34);
        // "+k more" opens the Parked tab; ✕ closes.
        let (r, _) = m.clicks.iter().find(|(_, h)| *h == Hit::More).cloned().unwrap();
        m.act(click(r.x + 3, r.y), &mut fx);
        assert_eq!(m.tab, Tab::Parked);
        frame(&mut m, 110, 34);
        assert!(m.act(click(110 - 4, 0), &mut fx));
        // A click on nothing does nothing.
        assert!(!m.act(click(1, 1), &mut fx));
    }

    #[test]
    fn footer_prompts() {
        let mut m = menu();
        frame(&mut m, 110, 34);
        m.act(crate::menu::keys::Key::Char('x'), &mut NoEffects);
        let f = frame(&mut m, 110, 34);
        assert!(f[32].contains("close Island resize? y/n"), "{}", f[32]);
        m.act(crate::menu::keys::Key::Char('n'), &mut NoEffects);
        m.act(crate::menu::keys::Key::Char('/'), &mut NoEffects);
        m.act(crate::menu::keys::Key::Char('k'), &mut NoEffects);
        let f = frame(&mut m, 110, 34);
        assert!(f[1].contains("/ k▏"), "{}", f[1]);
        assert!(f[32].contains("keep") && f[32].contains("clear"));
        m.act(crate::menu::keys::Key::Esc, &mut NoEffects);
        m.act(crate::menu::keys::Key::Char('0'), &mut NoEffects);
        let f = frame(&mut m, 110, 34);
        assert!(f[32].contains("0▏  next digit"), "{}", f[32]);
    }

    #[test]
    fn home_prefix_and_markdown() {
        let mut d = data();
        for a in d.view.spaces.iter_mut().flat_map(|s| s.agents.iter_mut()) {
            match a.window_id.as_str() {
                "@4" => a.title = "~/Commodities Job Tracker".into(),
                "@3" => a.title = "cua-notch/Notch Tasks".into(),
                _ => {}
            }
        }
        d.view.needs[1].reason = "**Which** `deck`?\n- <agent-message from=x>[the v3](http://x) one</agent-message>".into();
        let mut m = Menu::new(d, Tab::All);
        m.sel = Some(RowKey::Need("@1".into()));
        let all = frame(&mut m, 110, 34).join("\n");
        // A bare ~/ goes; a real project prefix stays.
        assert!(all.contains("○ Commodities Job Tracker") && !all.contains("~/Commodities"), "{all}");
        assert!(all.contains("◐ cua-notch/Notch Tasks"), "{all}");
        // The reason line is plain words.
        assert!(all.contains("asks Which deck? the v3 one"), "{all}");
        m.sel = Some(RowKey::Agent("main".into(), "@4".into()));
        let f = frame(&mut m, 110, 34);
        assert!(f[3].contains("│ Commodities Job Tracker"), "{}", f[3]);
        m.act(crate::menu::keys::Key::Char('x'), &mut NoEffects);
        assert!(frame(&mut m, 110, 34)[32].contains("close Commodities Job Tracker? y/n"));
    }

    #[test]
    fn jump_key_ranges() {
        assert_eq!(jump_keys(0), None);
        assert_eq!(jump_keys(1).as_deref(), Some("1"));
        assert_eq!(jump_keys(7).as_deref(), Some("1–7"));
        assert_eq!(jump_keys(12).as_deref(), Some("1–9 01–03"));
        assert_eq!(jump_keys(30).as_deref(), Some("1–9 001–021"));
    }

    #[test]
    fn tiny_and_test_backend() {
        let mut m = menu();
        let f = frame(&mut m, 20, 5);
        assert_eq!(f[0], "agents · too small");
        // Through ratatui's own TestBackend, as the live loop draws.
        let mut t = ratatui::Terminal::new(ratatui::backend::TestBackend::new(110, 34)).unwrap();
        t.draw(|fr| render(&mut m, fr.buffer_mut(), 1000.0)).unwrap();
        let b = t.backend().buffer().clone();
        assert!(dump(&b, false)[3].contains("NEEDS YOU"));
        assert!(dump(&b, true)[0].contains("\x1b[0;38;2;69;71;90;48;2;30;30;46m╭─ "));
    }
}
