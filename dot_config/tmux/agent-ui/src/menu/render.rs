//! Drawing one frame of the menu into a ratatui [`Buffer`], plus the frame's
//! click map, what is on screen for the keys, and the `--once` ANSI dump.
//!
//! ```text
//! ╭─ agents · ◉ 1 need you · ◐ 4 working · ○ 6 idle · ▪ 6 parked ─── ⌥W ✕ ─╮   header
//! │ main ⌥ main ─────────────────────────────────────────────── ◉1 ◐2 ○1 │   a space
//! │  s  ◉ Kua Yu focus timing  asks Which deck?               kua   44m  │   letter · glyph · title · reason · tag · age
//! │  d  ◐ Tmux Agent Sidebar                         · here ·  tmux  19m  │
//! │  g  ○ Commodities Job Tracker                                   3h  │   idle: dimmer title
//! │ work ──────────────────────────────────────────────────────── ✕1 ○1 │
//! │     ▪ 6 parked ⇥                                                    │   the fold
//! ├─ Kua Yu focus timing · main:1 · ✳ claude · ⌥ main · ~/x ────────────┤   preview strip (Space v)
//! │ (the pane's last lines, coloured)                                   │
//! ├─────────────────────────────────────────────────────────────────────┤
//! │  a–j  go   ⌥W  last   ⏎  open   /  search   ␣  actions   ⇥  filter  │   key bar / search / strip / y/n
//! ╰─────────────────────────────────────────────────────────────────────╯
//! ```
//!
//! The header is ONE status line: the non-zero counts in the state hues,
//! and the filter's name when it is not All (`Working ⇥`, clickable). Each
//! agent row leads with its letter in reverse video, the background its
//! state's colour (needs yellow, failed red, done green, working blue, idle
//! overlay). A needs-you row's title takes the needs colour and its reason
//! shows inline; a pinned agent (the sidebar's PINNED section, Space p) wears
//! a dim pin glyph after its title, its row where it always is. The preview
//! is OFF by default; Space v turns on a strip of up to 10 lines under the
//! list, full width. The footer shows, first match
//! wins: a y/n, the search line, the actions strip, a message, else the key
//! bar.

use super::letters::ALPHABET;
use super::rows::{Row, Tab, Target};
use super::state::{strip, Drawn, Hit, Menu};
use crate::actions::watcher_problem;
use crate::model::Cat;
use crate::palette::color;
use crate::text::{clip, dwidth, plain, sanitize};
use crate::view::{tilde, Agent};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};

/// Smaller than this, the frame is one line saying so.
pub const MIN_W: u16 = 30;
pub const MIN_H: u16 = 8;
/// The preview strip's height: its rule line plus up to this many lines.
pub const PREVIEW_LINES: u16 = 10;

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
    /// Between the header and the footer's rule, inside the side borders.
    pub body: Rect,
    pub list: Rect,
    /// The preview strip (its rule line first), when it is on and fits.
    pub preview: Option<Rect>,
}

impl Geo {
    pub fn new(w: u16, h: u16, preview: bool) -> Geo {
        let body = Rect::new(1, 1, w.saturating_sub(2), h.saturating_sub(4));
        if !preview || body.height < 8 {
            return Geo { body, list: body, preview: None };
        }
        let ph = (PREVIEW_LINES + 1).min(body.height / 2);
        let list = Rect::new(body.x, body.y, body.width, body.height - ph);
        Geo { body, list, preview: Some(Rect::new(body.x, list.bottom(), body.width, ph)) }
    }
}

/// A title as shown: a bare `~/` project prefix says nothing, so it goes.
pub fn shown_title(t: &str) -> &str {
    t.strip_prefix("~/").unwrap_or(t)
}

/// A row's title and its project tag: `proj/Title` → ("Title", "proj"); a
/// bare `~/` (an agent started in $HOME) is no project.
pub fn title_and_tag(a: &Agent) -> (String, String) {
    let p = a.project.trim_end_matches('/');
    if p.is_empty() || p == "~" {
        (sanitize(shown_title(&a.title)), String::new())
    } else {
        (sanitize(&a.short_title), sanitize(p))
    }
}

/// The colour of a reason word: the detail kind's, else the agent's own.
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

/// The background of an agent's letter badge: its state's colour.
pub fn badge_color(a: &Agent) -> &'static str {
    match a.cat {
        Some(Cat::Failed) => "red",
        Some(Cat::NeedsInput) => "yellow",
        Some(Cat::Done) => "green",
        Some(Cat::Working) => "blue",
        Some(Cat::Paused) => crate::model::PAUSED_HUE,
        Some(Cat::Idle) | None => "overlay",
    }
}

/// Draw the whole frame for `now`; records what is on screen in `m`.
pub fn render(m: &mut Menu, buf: &mut Buffer, now: f64) {
    let area = buf.area;
    m.clicks.clear();
    if area.width == 0 || area.height == 0 {
        m.drawn = Default::default();
        m.preview_rows = 0;
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
        return;
    }
    let geo = Geo::new(w, h, m.preview);
    let s1 = fg("surface1");

    // Borders and the footer's rule.
    header(m, buf, w);
    for y in 1..h - 1 {
        put(buf, 0, y, w, "│", s1);
        put(buf, w - 1, y, w, "│", s1);
    }
    put(buf, 0, h - 3, w, "├", s1);
    for x in 1..w - 1 {
        put(buf, x, h - 3, w, "─", s1);
    }
    put(buf, w - 1, h - 3, w, "┤", s1);
    put(buf, 0, h - 1, w, "╰", s1);
    for x in 1..w - 1 {
        put(buf, x, h - 1, w, "─", s1);
    }
    put(buf, w - 1, h - 1, w, "╯", s1);

    list(m, buf, geo.list);
    match geo.preview {
        Some(r) => preview(m, buf, r, w),
        None => m.preview_rows = 0,
    }
    footer(m, buf, w, h - 2, now);
}

/// The top border, which is the header: `agents`, a watcher banner, the
/// filter when it is not All, the non-zero counts, then ⌥W ✕.
fn header(m: &mut Menu, buf: &mut Buffer, w: u16) {
    let s1 = fg("surface1");
    let ov = fg("overlay");
    let right_w = 8; // " ⌥W ✕ ─╮"
    let end = w - right_w;
    let mut x = put(buf, 0, 0, w, "╭─ ", s1);
    x = put(buf, x, 0, end, "agents", bold(fg("text")));
    let v = &m.data.view;
    let banner = match watcher_problem(v.watcher_age) {
        Some(why) => Some(format!(" watcher {why} · ␣ r restarts it ")),
        None if v.client_gone => Some(" this menu's client is gone · esc closes ".to_string()),
        None => None,
    };
    if let Some(b) = banner {
        x = put(buf, x, 0, end, " ", s1);
        x = put(buf, x, 0, end.saturating_sub(1), &b, bold(on("crust", "red")));
    }
    if m.tab != Tab::All {
        x = put(buf, x, 0, end, " · ", ov);
        let x0 = x;
        x = put(buf, x, 0, end, &format!("{} ⇥", m.tab.title()), bold(fg("blue")));
        m.clicks.push((Rect::new(x0, 0, x - x0, 1), Hit::Filter));
    }
    let counts: Vec<(String, &str)> = [
        (v.needs.len(), "◉", "need you", "yellow"),
        (v.counts.get(Cat::Working), "◐", "working", "blue"),
        (v.counts.get(Cat::Paused), crate::model::PAUSED_GLYPH, "paused", crate::model::PAUSED_HUE),
        (v.counts.get(Cat::Idle), "○", "idle", "overlay"),
        (v.parked.len(), "▪", "parked", "overlay"),
    ]
    .into_iter()
    .filter(|c| c.0 > 0)
    .map(|(n, g, words, c)| (format!("{g} {n} {words}"), c))
    .collect();
    for (t, c) in counts {
        // Each count whole or not at all (the last ones give way first).
        if x + 3 + cells(&t) + 1 > end {
            break;
        }
        x = put(buf, x, 0, end, " · ", ov);
        x = put(buf, x, 0, end, &t, fg(c));
    }
    if x < end {
        x = put(buf, x, 0, end, " ", s1);
    }
    while x < end {
        x = put(buf, x, 0, end, "─", s1);
    }
    let mut rx = put(buf, end, 0, w, " ⌥W ", ov);
    m.clicks.push((Rect::new(rx.saturating_sub(1), 0, 3, 1), Hit::Close));
    rx = put(buf, rx, 0, w, "✕", fg("red"));
    put(buf, rx, 0, w, " ─╮", s1);
}

/// The list: one line per row, the scrollbar in its last column.
fn list(m: &mut Menu, buf: &mut Buffer, r: Rect) {
    let mut sel_pos = m.sel.as_ref().and_then(|k| m.rows.iter().position(|r| r.key().as_ref() == Some(k)));
    let hgt = r.height as usize;
    let len = m.rows.len();
    if m.follow {
        if let Some(sp) = sel_pos {
            if sp < m.top {
                // Scrolling up onto a row: bring its group rule into view too.
                m.top = sp;
                while m.top > 0 && m.rows[m.top - 1].key().is_none() && sp < m.top - 1 + hgt {
                    m.top -= 1;
                }
            } else if sp >= m.top + hgt {
                m.top = sp + 1 - hgt;
            }
        }
    }
    m.top = m.top.min(len.saturating_sub(hgt));
    if !m.follow {
        // After a wheel scroll: a selection scrolled off screen moves to the
        // nearest row still on it, so keys never act on a row out of sight.
        if let Some(sp) = sel_pos {
            if !(m.top..m.top + hgt).contains(&sp) {
                let visible: Vec<usize> =
                    (m.top..(m.top + hgt).min(len)).filter(|&i| m.rows[i].key().is_some()).collect();
                let pick = if sp < m.top { visible.first() } else { visible.last() };
                if let Some(&ri) = pick {
                    m.sel = m.rows[ri].key();
                    sel_pos = Some(ri);
                }
            }
        }
    }
    m.page = hgt.saturating_sub(1).max(1);
    let cw = r.width.saturating_sub(1); // the last column is the scrollbar's
    let mut drawn: Vec<Target> = Vec::new();
    for (k, ri) in (m.top..len).take(hgt).enumerate() {
        let y = r.y + k as u16;
        let row = m.rows[ri].clone();
        let selected = Some(ri) == sel_pos;
        let rect = Rect::new(r.x, y, cw, 1);
        if selected {
            buf.set_style(rect, Style::new().bg(color("surface1")));
        }
        draw_row(m, buf, r.x, y, cw, &row, selected);
        if let Some(t) = row.target() {
            drawn.push(t.clone());
            m.clicks.push((rect, Hit::Row(t)));
        } else if matches!(row, Row::Fold(_)) {
            m.clicks.push((rect, Hit::Fold));
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
    // What the letters and ⌥W mean until the next frame (state.rs has the rule).
    m.drawn = Drawn::of(&m.data, drawn);
    m.drawn_sel = m.selected_target();
}

/// The letter badge's cells: ` a ` (or blank, keeping titles aligned).
const BADGE_W: u16 = 3;

/// One list row.
fn draw_row(m: &Menu, buf: &mut Buffer, x0: u16, y: u16, cw: u16, row: &Row, sel: bool) {
    let ov = fg("overlay");
    let lead = x0 + 1 + BADGE_W + 1; // where the glyph goes
    match row {
        Row::Group { name, branch, counts, .. } => {
            let mut left = vec![seg(format!(" {} ", sanitize(name)), bold(fg("text")))];
            if !branch.is_empty() {
                left.push(seg(format!("⌥ {} ", clip(branch, 24)), fg("mauve")));
            }
            let toks: Vec<String> = counts.nonzero().iter().map(|(k, n)| format!("{}{n}", k.glyph())).collect();
            let right = [seg(format!(" {} ", toks.join(" ")), ov)];
            line(buf, x0, y, cw, &left, &right, Some(("─", fg("surface0"))));
        }
        Row::Agent(a) => agent_row(m, buf, x0, y, cw, a, None, sel),
        Row::Need(n) => agent_row(m, buf, x0, y, cw, &n.agent, Some(&n.agent.session), sel),
        Row::Parked(p) => {
            let a = &p.agent;
            let end = x0 + cw;
            let x = put(buf, lead, y, end, "▪ ", ov);
            let mut right = String::new();
            if m.tab == Tab::Parked && cw >= 60 {
                right = format!("{} · ", p.state_words);
            }
            right.push_str(&format!("{} {:>4}", sanitize(&p.origin), p.age));
            let rw = cells(&right);
            let room = end.saturating_sub(x + rw + 2) as usize;
            put(buf, x, y, end, &clip(&sanitize(shown_title(&a.title)), room), if sel { bold(fg("text")) } else { ov });
            put(buf, end.saturating_sub(rw), y, end, &right, ov);
        }
        Row::Fold(k) => {
            put(buf, lead, y, x0 + cw, &format!("▪ {k} parked ⇥"), if sel { bold(fg("text")) } else { ov });
        }
        Row::Empty(why) => {
            put(buf, x0 + 1, y, x0 + cw, why, ov);
        }
    }
}

/// An agent row: `[badge] glyph Title (flag)  reason …  · here ·  tag  age`.
/// `place`: a Needs filter row names its session (no group rule above it).
#[allow(clippy::too_many_arguments)]
fn agent_row(m: &Menu, buf: &mut Buffer, x0: u16, y: u16, cw: u16, a: &Agent, place: Option<&str>, sel: bool) {
    let ov = fg("overlay");
    let end = x0 + cw;
    let mut x = x0 + 1;
    if let Some(c) = m.data.letter(&a.window_id) {
        put(buf, x, y, end, &format!(" {c} "), bold(on("crust", badge_color(a))));
    }
    x += BADGE_W + 1;
    x = put(buf, x, y, end, a.glyph, fg(a.color.unwrap_or("overlay")));
    x = put(buf, x, y, end, " ", ov);

    let need = m.reason_of_agent(a);
    let (title, tag) = title_and_tag(a);
    let flag = a.flag.map(|(f, c)| {
        let g = match f {
            crate::model::Flag::Workflow => crate::model::STRIP_GEAR,
            crate::model::Flag::Cua => crate::model::STRIP_MOUSE,
        };
        (format!(" {g}"), c)
    });
    // A pinned agent (the sidebar's PINNED section) wears the pin glyph
    // after its title, dim, with its ⌥W digit: the menu's order never moves
    // for it.
    let pin = a.pinned.is_some().then(|| match a.pin_key {
        Some(n) => format!(" {} {n}", crate::pins::GLYPH),
        None => format!(" {}", crate::pins::GLYPH),
    });
    let fw = flag.as_ref().map_or(0, |(s, _)| cells(s)) + pin.as_deref().map_or(0, cells);
    // The right-hand tags, dropped (project, then here) when the title would
    // get too little room.
    let age = format!(" {:>4}", a.age);
    let mut tag = tag;
    if let Some(p) = place {
        tag = if tag.is_empty() { sanitize(p) } else { format!("{tag} · {}", sanitize(p)) };
    }
    let mut here = a.is_current;
    let right_w = |tag: &str, here: bool| -> u16 {
        cells(&age) + if tag.is_empty() { 0 } else { cells(tag) + 2 } + if here { cells("· here ·") + 2 } else { 0 }
    };
    let room = |rw: u16| end.saturating_sub(x + rw + fw + 1);
    let want = cells(&title).min(16);
    if room(right_w(&tag, here)) < want {
        tag.clear();
    }
    if room(right_w(&tag, here)) < want {
        here = false;
    }
    let avail = room(right_w(&tag, here)) as usize;
    let reason = need.map(|n| {
        let text = plain(&n.reason);
        (n.reason_word.clone(), reason_color(a, !n.reason.is_empty()), text)
    });
    let tw = dwidth(&title);
    let title_room = match &reason {
        Some((wd, _, text)) => {
            let rlen = dwidth(wd) + if text.is_empty() { 0 } else { 1 + dwidth(text) } + 2;
            tw.min((avail / 2).max(avail.saturating_sub(rlen)))
        }
        None => avail,
    };
    let tcol = if a.attention && need.is_some() {
        a.cat.map(Cat::hue).unwrap_or("yellow")
    } else if a.cat.is_none() && !a.is_current {
        "overlay"
    } else if a.cat == Some(Cat::Idle) {
        "sub"
    } else {
        "text"
    };
    let tst = if sel { bold(fg(tcol)) } else { fg(tcol) };
    let rx = end.saturating_sub(right_w(&tag, here));
    x = put(buf, x, y, rx, &clip(&title, title_room), tst);
    if let Some((s, c)) = &flag {
        x = put(buf, x, y, rx, s, fg(c));
    }
    if let Some(p) = &pin {
        x = put(buf, x, y, rx, p, fg("overlay2"));
    }
    if let Some((wd, wcol, text)) = reason {
        let stop = rx.saturating_sub(1);
        x = put(buf, x, y, stop, "  ", ov);
        x = put(buf, x, y, stop, &clip(&wd, stop.saturating_sub(x) as usize), fg(wcol));
        if !text.is_empty() && x + 2 < stop {
            x = put(buf, x, y, stop, " ", ov);
            put(buf, x, y, stop, &clip(&text, stop.saturating_sub(x) as usize), fg("sub"));
        }
    }
    let mut cx = rx;
    if here {
        cx = put(buf, cx, y, end, "· here ·", ov);
        cx = put(buf, cx, y, end, "  ", ov);
    }
    if !tag.is_empty() {
        put(buf, cx, y, end, &tag, ov);
    }
    put(buf, end.saturating_sub(cells(&age)), y, end, &age, ov);
}

/// The preview strip: a rule naming the selected row, then its pane's tail.
fn preview(m: &mut Menu, buf: &mut Buffer, r: Rect, w: u16) {
    let s1 = fg("surface1");
    let ov = fg("overlay");
    m.preview_rows = r.height.saturating_sub(1) as usize;
    put(buf, 0, r.y, w, "├", s1);
    put(buf, w - 1, r.y, w, "┤", s1);
    let sel = m.selected().cloned();
    let Some(a) = sel.as_ref().and_then(Row::agent).cloned() else {
        line(buf, r.x, r.y, r.width, &[seg("─ nothing selected ", ov)], &[], Some(("─", s1)));
        return;
    };
    // title · session:index · ✳ claude · ⌥ branch · ~/path
    let mut meta = vec![seg("─ ", s1), seg(clip(&sanitize(shown_title(&a.title)), 40), bold(fg("text"))),
        seg(format!(" · {}", a.place()), ov)];
    if let Some(Row::Parked(p)) = &sel {
        if !p.origin.is_empty() {
            meta.push(seg(format!(" · from {}", sanitize(&p.origin)), ov));
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
    meta.push(seg(" ", ov));
    line(buf, r.x, r.y, r.width, &meta, &[], Some(("─", s1)));
    let body = Rect::new(r.x + 1, r.y + 1, r.width.saturating_sub(2), m.preview_rows as u16);
    peek_lines(m, buf, body, &(a.window_id.clone(), a.session.clone()), &a.place());
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

/// The letters in play, as the key bar says them: `a`, `a–j` (alphabet
/// order: the range from the first held letter to the last).
pub fn letter_hint(drawn: &Drawn) -> Option<String> {
    let held: Vec<char> = ALPHABET.iter().copied().filter(|c| drawn.letters.contains_key(c)).collect();
    match held.as_slice() {
        [] => None,
        [one] => Some(one.to_string()),
        [first, .., last] => Some(format!("{first}–{last}")),
    }
}

/// Key items (`key`, `words`) drawn as ` key  words ` chips, dropping the
/// words in `drop_order` until they fit.
fn key_items(buf: &mut Buffer, x: u16, y: u16, end: u16, items: Vec<(String, String)>, drop_order: &[&str]) -> u16 {
    let item_w = |k: &str, wd: &str| cells(k) + 2 + cells(wd) + 2;
    let total = |v: &[(String, String)]| v.iter().map(|(k, wd)| item_w(k, wd)).sum::<u16>();
    let mut shown = items;
    for d in drop_order {
        if total(&shown) <= end.saturating_sub(x) {
            break;
        }
        shown.retain(|(_, wd)| wd != d);
    }
    let mut cx = x;
    for (k, wd) in shown {
        cx = put(buf, cx, y, end, &format!(" {k} "), on("text", "surface0"));
        cx = put(buf, cx, y, end, &format!(" {wd} "), fg("overlay"));
    }
    cx
}

/// The key bar, or what replaces it.
fn footer(m: &Menu, buf: &mut Buffer, w: u16, y: u16, now: f64) {
    let x = 2;
    let end = w - 2;
    if let Some(c) = &m.confirm {
        let room = (end - x).saturating_sub(cells(c.verb()) + 8) as usize;
        let mut cx = put(buf, x, y, end,
            &format!("{} {}? ", c.verb(), clip(&sanitize(shown_title(&c.target.title)), room)), fg("yellow"));
        cx = put(buf, cx, y, end, "y", bold(fg("text")));
        put(buf, cx, y, end, "/n", fg("text"));
        return;
    }
    if m.searching {
        let mut cx = put(buf, x, y, end, "/ ", bold(fg("peach")));
        let hints: Vec<(String, String)> =
            [("⏎", "go"), ("esc", "clear"), ("↑↓", "move")].map(|(k, v)| (k.to_string(), v.to_string())).to_vec();
        let hint_w: u16 = 4 + hints.iter().map(|(k, v)| cells(k) + cells(v) + 4).sum::<u16>();
        let room = end.saturating_sub(cx + 1 + hint_w.min(end.saturating_sub(cx) / 2)) as usize;
        let q = sanitize(&m.query);
        // The tail of a long query, so the cursor end stays visible.
        let mut shown: Vec<char> = Vec::new();
        let mut used = 0;
        for c in q.chars().rev() {
            let cw = dwidth(&c.to_string());
            if used + cw > room {
                break;
            }
            used += cw;
            shown.push(c);
        }
        let shown: String = shown.into_iter().rev().collect();
        cx = put(buf, cx, y, end, &shown, fg("text"));
        cx = put(buf, cx, y, end, "▏", fg("peach"));
        key_items(buf, cx + 3, y, end, hints, &["move", "clear"]);
        return;
    }
    if let Some(t) = &m.actions {
        let title = clip(&sanitize(shown_title(&t.title)), 24);
        let cx = put(buf, x, y, end, &format!("{title} "), bold(fg("text")));
        let mut items: Vec<(String, String)> =
            strip(t).into_iter().map(|(k, _, wd)| (k.to_string(), wd.to_string())).collect();
        items.push(("⏎".into(), "go".into()));
        items.push(("esc".into(), "cancel".into()));
        key_items(buf, cx, y, end, items, &["cancel", "go"]);
        return;
    }
    if !m.msg.is_empty() && now < m.msg_until {
        put(buf, x, y, end, &sanitize(&m.msg), fg("sky"));
        return;
    }
    let mut v: Vec<(String, String)> = Vec::new();
    if let Some(l) = letter_hint(&m.drawn) {
        v.push((l, "go".into()));
    }
    v.push(("⌥W".into(), if m.drawn.last.is_some() { "last" } else { "close" }.into()));
    for (k, wd) in [("⏎", "open"), ("/", "search"), ("␣", "actions"), ("⇥", "filter"), ("⌥S", "next"), ("⌥X", "back")] {
        v.push((k.into(), wd.into()));
    }
    key_items(buf, x, y, end, v, &["back", "next", "filter", "actions", "search", "open"]);
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
    use super::super::keys::{Key, Mouse};
    use super::super::rows::tests::{data, data_of, fixture};
    use super::super::rows::{Data, RowKey};
    use super::super::state::{tests::Rec, Menu, NoEffects, PeekData};
    use super::*;
    use crate::ansi::parse_line;

    fn frame(m: &mut Menu, w: u16, h: u16) -> Vec<String> {
        let mut buf = Buffer::empty(Rect::new(0, 0, w, h));
        render(m, &mut buf, 1000.0);
        dump(&buf, false)
    }

    fn buffer(m: &mut Menu, w: u16, h: u16) -> Buffer {
        let mut buf = Buffer::empty(Rect::new(0, 0, w, h));
        render(m, &mut buf, 1000.0);
        buf
    }

    fn menu() -> Menu {
        Menu::new(data(), Tab::All)
    }

    fn at(f: &[String], needle: &str) -> usize {
        f.iter().position(|l| l.contains(needle)).unwrap_or_else(|| panic!("{needle:?} not in\n{}", f.join("\n")))
    }

    fn click(col: u16, row: u16) -> Key {
        Key::Mouse(Mouse { button: 0, col, row, press: true })
    }

    #[test]
    fn layout_110x34() {
        let mut m = menu();
        let f = frame(&mut m, 110, 34);
        assert_eq!(f.len(), 34);
        for l in &f {
            assert!(dwidth(l) <= 110, "{l}");
        }
        // One header line: the non-zero counts; no chips, no NEEDS YOU, no PARKED section.
        assert!(f[0].starts_with("╭─ agents · ◉ 2 need you · ◐ 2 working · ○ 3 idle · ▪ 5 parked ─"), "{}", f[0]);
        assert!(f[0].ends_with(" ⌥W ✕ ─╮"), "{}", f[0]);
        let all = f.join("\n");
        assert!(!all.contains("NEEDS YOU") && !all.contains("PARKED") && !all.contains("Active"), "{all}");
        // Fixed groups by name; letter badge, glyph, title first.
        assert!(f[1].starts_with("│ Alpha ─") && f[1].contains("○1"), "{}", f[1]);
        assert_eq!(f[2], format!("│  a  ○ Application link{}│", " ".repeat(107 - 22)));
        assert!(f[3].starts_with("│ main ─") && f[3].contains("◉1 ◐2 ○1"), "{}", f[3]);
        // A need: its reason inline; the client's window: · here ·; ages flush right.
        assert!(f[4].starts_with("│  s  ◉ Kua Yu focus timing  asks Which deck?") && f[4].ends_with("15m │"), "{}", f[4]);
        assert!(f[5].starts_with("│  d  ◐ Tmux Agent Sidebar") && f[5].contains("· here ·"), "{}", f[5]);
        assert!(f[6].starts_with("│  f  ◐ Handy.app Speech ⚙"), "{}", f[6]);
        assert!(f[7].starts_with("│  g  ○ Commodities Job Tracker"), "{}", f[7]);
        assert!(f[8].starts_with("│ work ─"), "{}", f[8]);
        assert!(f[9].starts_with("│  h  ✕ Island resize  failed"), "{}", f[9]);
        assert!(f[10].starts_with("│  j  ○ Census AX sweep"), "{}", f[10]);
        assert!(f[11].starts_with("│  f  ◐ Handy.app Speech ⚙"), "{}", f[11]); // linked: the same letter
        assert!(f[12].starts_with("│     ▪ 5 parked ⇥"), "{}", f[12]);
        assert!(f[31].starts_with("├─") && f[31].ends_with("─┤"));
        assert!(f[32].starts_with("│  a–k  go  ⌥W  close  ⏎  open  /  search  ␣  actions  ⇥  filter"), "{}", f[32]);
        assert!(f[33].starts_with("╰") && f[33].ends_with("╯"));
        // Badges: the letter in reverse video on its state's colour.
        let b = buffer(&mut m, 110, 34);
        for (y, c) in [(2, "overlay"), (4, "yellow"), (5, "blue"), (9, "red"), (10, "overlay")] {
            assert_eq!((b[(3, y)].bg, b[(3, y)].fg), (color(c), color("crust")), "row {y}");
            assert!(b[(3, y)].modifier.contains(Modifier::BOLD));
        }
        // The needs title in the needs colour; idle dimmer but readable; selected bold on surface1.
        assert_eq!(b[(8, 4)].fg, color("yellow"));
        assert_eq!(b[(8, 7)].fg, color("sub"));
        assert_eq!(b[(8, 5)].fg, color("text"));
        assert_eq!((b[(8, 9)].bg, b[(8, 9)].fg), (color("surface1"), color("red")));
        assert_eq!(b[(30, 6)].bg, color("base"));
        // What the keys mean in this frame.
        // (k: main's shell, lettered from the one pool after the agents, though the menu lists no row for it.)
        assert_eq!(m.drawn.letters.keys().collect::<String>(), "adfghjks");
        assert_eq!(m.drawn.letters[&'k'].win, "@5");
        assert_eq!(m.drawn.letters[&'f'].key, RowKey::Agent("main".into(), "@3".into()));
        assert_eq!(m.drawn.rows.len(), 8);
        assert_eq!(m.preview_rows, 0);
        assert_eq!(m.peek_want(), None);
    }

    /// The filter's name shows (and cycles on a click) when it is not All.
    #[test]
    fn header_filter_and_narrow_counts() {
        let mut m = Menu::new(data(), Tab::Working);
        let f = frame(&mut m, 110, 34);
        assert!(f[0].starts_with("╭─ agents · Working ⇥ · ◉ 2 need you · ◐ 2 working"), "{}", f[0]);
        let (r, _) = m.clicks.iter().find(|(_, h)| *h == Hit::Filter).cloned().unwrap();
        m.act(click(r.x + 1, 0), &mut NoEffects);
        assert_eq!(m.tab, Tab::Idle);
        // Narrow: counts drop whole, from the end.
        let mut m = menu();
        let f = frame(&mut m, 60, 20);
        assert!(f[0].contains("○ 3 idle") && !f[0].contains("parked"), "{}", f[0]);
        let f = frame(&mut m, 40, 20);
        assert!(f[0].starts_with("╭─ agents · ◉ 2 need you ─"), "{}", f[0]);
        for l in &f {
            assert!(dwidth(l) <= 40, "{l}");
        }
        // Zero counts are dropped.
        let d = Data::build(&crate::tmux::Snapshot::parse(&crate::tmux::tests::row("main", 1, "@1",
            &[("state", "running")])), None, 1000.0, &crate::Collator::new("C"), None, Some(1.0));
        let f = frame(&mut Menu::new(d, Tab::All), 80, 20);
        assert!(f[0].starts_with("╭─ agents · ◐ 1 working ─"), "{}", f[0]);
    }

    /// `proj/Title`: the title first, the project a dim tag on the right; a
    /// bare `~/` is no project.
    #[test]
    fn project_tags() {
        let mut d = data();
        for a in d.view.spaces.iter_mut().flat_map(|s| s.agents.iter_mut()) {
            match a.window_id.as_str() {
                "@4" => (a.title, a.project, a.short_title) = ("~/Commodities".into(), "~/".into(), "Commodities".into()),
                "@2" => (a.title, a.project, a.short_title) = ("tmux/Agent Sidebar".into(), "tmux/".into(), "Agent Sidebar".into()),
                _ => {}
            }
        }
        let mut m = Menu::new(d, Tab::All);
        let f = frame(&mut m, 110, 34);
        let l = &f[at(&f, "Agent Sidebar")];
        assert!(l.starts_with("│  d  ◐ Agent Sidebar ") && l.contains("· here ·  tmux ") && l.ends_with(" 1m │"), "{l}");
        let l = &f[at(&f, "Commodities")];
        assert!(l.starts_with("│  g  ○ Commodities ") && !l.contains('~'), "{l}");
        // Short of room the tag goes first, then here; the title stays.
        let f = frame(&mut m, 44, 34);
        let l = &f[at(&f, "Agent Sidebar")];
        assert!(!l.contains("tmux") && l.contains("Agent Sidebar"), "{l}");
    }

    /// Space v: a strip under the list, full width, with the pane's tail.
    #[test]
    fn preview_strip() {
        let mut m = menu();
        frame(&mut m, 110, 34);
        m.peeks.insert(("@6".into(), "work".into()), PeekData {
            lines: Some(vec![parse_line("\x1b[31m529 overloaded\x1b[0m"), parse_line("retry?")]),
            n: 10,
        });
        m.handle(&[Key::Char(' '), Key::Char('v')], &mut NoEffects);
        assert!(m.preview);
        let f = frame(&mut m, 110, 34);
        // Body 30 rows: the list keeps 19, the strip 11 (its rule + 10 lines).
        assert!(f[20].starts_with("├─ Island resize · work:1 ─"), "{}", f[20]);
        assert!(f[21].starts_with("│ 529 overloaded") && f[22].starts_with("│ retry?"), "{}", f[21]);
        assert_eq!(m.preview_rows, 10);
        assert_eq!(m.peek_want().map(|w| (w.target.win, w.n)), Some(("@6".into(), 10)));
        assert!(f[12].contains("▪ 5 parked"), "{}", f[12]);
        // A short popup: no room, no strip.
        let f = frame(&mut m, 110, 11);
        assert!(!f.iter().any(|l| l.contains("Island resize ·")));
        assert_eq!(m.peek_want(), None);
        m.handle(&[Key::Char(' '), Key::Char('v')], &mut NoEffects);
        let f = frame(&mut m, 110, 34);
        assert!(!f.iter().any(|l| l.contains("529")));
    }

    #[test]
    fn footer_modes() {
        let mut m = menu();
        frame(&mut m, 110, 34);
        m.handle(&[Key::Char('/'), Key::Char('k'), Key::Char('u')], &mut NoEffects);
        let f = frame(&mut m, 110, 34);
        assert!(f[32].starts_with("│ / ku▏") && f[32].contains("⏎  go") && f[32].contains("esc  clear"), "{}", f[32]);
        m.act(Key::Esc, &mut NoEffects); // the search moved the selection to Kua; it stays
        m.act(Key::Char(' '), &mut NoEffects);
        let f = frame(&mut m, 110, 34);
        assert!(f[32].starts_with("│ Kua Yu focus timing  p  pin  s  park  x  close  v  preview  r  restart watcher  ⏎  go  esc  cancel"),
            "{}", f[32]);
        m.act(Key::Char('x'), &mut NoEffects);
        let f = frame(&mut m, 110, 34);
        assert!(f[32].contains("close Kua Yu focus timing? y/n"), "{}", f[32]);
        m.act(Key::Char('n'), &mut NoEffects);
        m.act(Key::Char('l'), &mut NoEffects); // nobody's letter
        let mut buf = Buffer::empty(Rect::new(0, 0, 110, 34));
        render(&mut m, &mut buf, crate::text::now());
        assert!(dump(&buf, false)[32].contains("nothing on l"));
        // ⌥W says where it goes.
        let mut snap = fixture();
        for w in &mut snap.windows {
            w.mru_cur = "@7".into();
        }
        let mut m = Menu::new(data_of(&snap), Tab::All);
        let f = frame(&mut m, 110, 34);
        assert!(f[32].contains("⌥W  last"), "{}", f[32]);
        // Narrow: the least needed keys go first; go and ⌥W stay.
        let f = frame(&mut m, 40, 20);
        assert!(f[18].contains("a–k  go") && f[18].contains("⌥W  last") && !f[18].contains("back"), "{}", f[18]);
    }

    /// Past 26 agents: no badge, the row still drawn and navigable.
    #[test]
    fn past_26_agents() {
        let rows: Vec<String> = (1..=28)
            .map(|i| crate::tmux::tests::row("main", i, &format!("@{i}"), &[("state", "running"), ("summary", &format!("w{i}"))]))
            .collect();
        let d = Data::build(&crate::tmux::Snapshot::parse(&rows.join("\n")), None, 1000.0, &crate::Collator::new("C"), None,
            Some(1.0));
        let mut m = Menu::new(d, Tab::All);
        m.act(Key::End, &mut NoEffects);
        let f = frame(&mut m, 80, 40);
        assert!(f.iter().any(|l| l.starts_with("│  m  ◐ w26 ")), "{f:#?}");
        assert!(f.iter().any(|l| l.starts_with("│     ◐ w28 ")), "{f:#?}");
        assert!(f[38].contains("a–m  go"), "{}", f[38]);
        assert_eq!(m.selected_target().map(|t| t.win), Some("@28".into()));
    }

    #[test]
    fn selection_stays_visible_when_scrolling() {
        let mut m = menu();
        m.act(Key::Home, &mut NoEffects);
        for _ in 0..12 {
            m.act(Key::Down, &mut NoEffects);
            frame(&mut m, 80, 10); // 6 body rows
            let k = m.sel.clone().unwrap();
            assert!(k == RowKey::Fold || m.drawn.rows.iter().any(|t| t.key == k), "{k:?} not drawn");
        }
        assert_eq!(m.sel, Some(RowKey::Fold));
        assert!(m.top > 0);
        for _ in 0..12 {
            m.act(Key::Up, &mut NoEffects);
            frame(&mut m, 80, 10);
        }
        assert_eq!(m.top, 0); // the group rule above the first row comes back too
        // The wheel moves the view; a selection scrolled off moves onto it.
        let wheel = |d: u16| Key::Mouse(Mouse { button: d, col: 5, row: 5, press: true });
        for _ in 0..3 {
            m.act(wheel(65), &mut NoEffects);
            frame(&mut m, 80, 10);
            let t = m.selected_target();
            assert!(t.is_none_or(|t| m.drawn.rows.contains(&t)), "off screen at top {}", m.top);
        }
        assert!(m.top > 0);
        let mut fx = Rec::default();
        if let Some(t) = m.selected_target() {
            assert!(m.act(Key::Enter, &mut fx));
            assert_eq!(fx.calls, [format!("go {} {}", t.win, t.session)]);
        }
    }

    #[test]
    fn click_map() {
        let mut fx = Rec::default();
        let mut m = menu();
        let f = frame(&mut m, 110, 34);
        // A row: the first click selects, the second goes.
        let y = at(&f, "Kua Yu focus timing") as u16;
        assert!(!m.act(click(10, y), &mut fx));
        assert_eq!(m.sel, Some(RowKey::Agent("main".into(), "@1".into())));
        frame(&mut m, 110, 34);
        assert!(m.act(click(10, y), &mut fx));
        assert_eq!(fx.calls, ["go @1 main"]);
        // A click cancels the actions strip (and only that).
        m.act(Key::Char(' '), &mut fx);
        assert!(!m.act(click(10, y), &mut fx) && m.actions.is_none());
        // The fold opens Parked; ✕ closes; nothing does nothing.
        let y = at(&f, "▪ 5 parked ⇥") as u16;
        m.act(click(10, y), &mut fx);
        assert_eq!(m.tab, Tab::Parked);
        let f = frame(&mut m, 110, 34);
        assert!(f[1].starts_with("│     ▪ Pitch deck v2") && f[1].contains("bai  ") && f[1].ends_with("8m │"), "{}", f[1]);
        assert!(m.act(click(110 - 4, 0), &mut fx));
        assert!(!m.act(click(1, 33), &mut fx));
    }

    /// Paused (docs/agent-pause.md "The paused state"): its own category,
    /// ⏸ in lavender (glyph, letter badge, header count), out of the needs
    /// tiers: `paused` as written and done / idle with `@agent_paused` set;
    /// listed under Idle, never under Needs you or Active.
    #[test]
    fn paused_is_a_lavender_category_out_of_the_needs() {
        let r = |i: u32, set: &[(&str, &str)]| crate::tmux::tests::row("main", i, &format!("@{i}"), set);
        let snap = crate::tmux::Snapshot::parse(&[
            r(1, &[("state", "paused"), ("summary", "Written paused")]),
            r(2, &[("state", "done"), ("paused", "1792418628"), ("summary", "Flagged done")]),
            r(3, &[("state", "idle"), ("paused", "1792418628"), ("summary", "Flagged idle")]),
            r(4, &[("state", "running"), ("paused", "1792418628"), ("summary", "Still working")]),
            r(5, &[("state", "done"), ("summary", "Really done")]),
            r(6, &[("state", "idle"), ("summary", "Just idle")]),
        ].join("\n"));
        let d = Data::build(&snap, None, 1000.0, &crate::Collator::new("C"), None, Some(1.0));
        assert_eq!(d.view.needs.iter().map(|n| n.agent.window_id.as_str()).collect::<Vec<_>>(), ["@5"]);
        let mut m = Menu::new(d.clone(), Tab::All);
        let f = frame(&mut m, 110, 20);
        assert!(f[0].contains("◉ 1 need you · ◐ 1 working · ⏸ 3 paused · ○ 1 idle"), "{}", f[0]);
        assert!(f[1].contains("✓1 ◐1 ⏸3 ○1"), "{}", f[1]);
        let b = buffer(&mut m, 110, 20);
        for name in ["Written paused", "Flagged done", "Flagged idle"] {
            let y = at(&f, name);
            assert!(f[y].contains(&format!("⏸ {name}")), "{}", f[y]);
            let gx = f[y].chars().position(|c| c == '⏸').unwrap() as u16;
            assert_eq!(b[(gx, y as u16)].fg, color("lavender"), "{name}");
            assert_eq!((b[(3, y as u16)].bg, b[(3, y as u16)].fg), (color("lavender"), color("crust")), "{name}");
        }
        let y = at(&f, "Really done");
        assert_eq!(b[(3, y as u16)].bg, color("green"));
        // The header's paused count in lavender.
        let hx = f[0].chars().position(|c| c == '⏸').unwrap() as u16;
        assert_eq!(b[(hx, 0)].fg, color("lavender"));
        // Filters: Idle lists the paused and the idle; Needs you and Active never a paused one.
        let titles = |tab: Tab| -> Vec<String> {
            super::super::rows::build_rows(&d, tab, "").iter().filter_map(|r| r.agent().map(|a| a.title.clone())).collect()
        };
        assert_eq!(titles(Tab::Idle), ["Written paused", "Flagged done", "Flagged idle", "Just idle"]);
        assert_eq!(titles(Tab::Needs), ["Really done"]);
        assert_eq!(titles(Tab::Active), ["Still working", "Really done"]);
        assert_eq!(titles(Tab::Working), ["Still working"]);
    }

    #[test]
    fn zero_and_tiny_frames() {
        let mut m = menu();
        assert!(frame(&mut m, 10, 0).is_empty());
        assert_eq!(frame(&mut m, 0, 10).len(), 10);
        assert!(m.drawn.rows.is_empty() && m.clicks.is_empty());
        assert_eq!(frame(&mut m, 20, 5)[0], "agents · too small");
        // Through ratatui's own TestBackend, as the live loop draws.
        let mut t = ratatui::Terminal::new(ratatui::backend::TestBackend::new(110, 34)).unwrap();
        t.draw(|fr| render(&mut m, fr.buffer_mut(), 1000.0)).unwrap();
        let b = t.backend().buffer().clone();
        assert!(dump(&b, false)[2].contains("Application link"));
        assert!(dump(&b, true)[0].contains("\x1b[0;38;2;69;71;90;48;2;30;30;46m╭─ "));
    }
}
