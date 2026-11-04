//! The key help's state ([`Help`]: the filter and the scroll), its layout
//! ([`columns`]) and one frame of it into a ratatui [`Buffer`], in the
//! menu's style (its palette, rounded border, key caps):
//!
//! ```text
//! ╭─ keys ─────────────────────────────────────────────────────────── ✕ ─╮
//! │ ❯ ▏type to filter: a key (opt w, cmd t) or words                     │   the filter
//! ├──────────────────────────────────────────────────────────────────────┤
//! │ SPACES ─────────────────────────   │ TABS ────────────────────────── │   section labels
//! │  ⌘1-9   Go to space N (lands …     │  ⌘T    New shell tab, this f…  │   key caps · description
//! │  ⌃S  1-9  Go to space N (any …     │  ⌘R    New claude tab, this …  │
//! │                                    │                                 │
//! ├──────────────────────────────────────────────────────────────────────┤
//! │  esc  close   ↑↓  scroll   type to filter                    90 keys │   footer
//! ╰──────────────────────────────────────────────────────────────────────╯
//! ```
//!
//! Two columns once the content is [`TWO_COLS`] wide, else one. Sections
//! are never split across columns: the split is the section boundary that
//! balances the two heights best. Both columns scroll together. No I/O.

use super::catalog::{self, Section};
use crate::menu::keys::{Key, Mouse};
use crate::palette::color;
use crate::text::{clip, dwidth, sanitize};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};

/// Smaller than this, one line saying so.
pub const MIN_W: u16 = 30;
pub const MIN_H: u16 = 8;
/// The content width (inside the border and its padding) from which the
/// keys are laid out in two columns.
pub const TWO_COLS: u16 = 90;
/// Between the two columns: a blank, a faint rule, a blank.
pub const GAP: u16 = 3;
/// Rows a wheel step scrolls.
pub const WHEEL: usize = 3;

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

/// One line of a column.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Line {
    /// A section's label: the section's index.
    Header(usize),
    /// (section, entry).
    Entry(usize, usize),
    Blank,
}

/// Each section as its lines: the label, then its keys.
fn blocks(sections: &[Section]) -> Vec<Vec<Line>> {
    sections
        .iter()
        .enumerate()
        .map(|(i, s)| {
            let mut v = vec![Line::Header(i)];
            v.extend((0..s.entries.len()).map(|j| Line::Entry(i, j)));
            v
        })
        .collect()
}

/// Blocks stacked with one blank line between them.
fn stack(blocks: &[Vec<Line>]) -> Vec<Line> {
    let mut out = Vec::new();
    for (k, b) in blocks.iter().enumerate() {
        if k > 0 {
            out.push(Line::Blank);
        }
        out.extend_from_slice(b);
    }
    out
}

/// The sections laid out in `ncols` (1 or 2) columns. With two, the split
/// is the section boundary that makes the taller column shortest (a tie
/// goes to the taller LEFT column).
pub fn columns(sections: &[Section], ncols: usize) -> Vec<Vec<Line>> {
    let b = blocks(sections);
    if ncols < 2 || b.len() < 2 {
        return vec![stack(&b)];
    }
    let height = |bs: &[Vec<Line>]| -> usize {
        if bs.is_empty() {
            0
        } else {
            bs.iter().map(Vec::len).sum::<usize>() + bs.len() - 1
        }
    };
    let mut best = (usize::MAX, 0usize);
    for k in 1..b.len() {
        let (l, r) = (height(&b[..k]), height(&b[k..]));
        let m = l.max(r);
        if m < best.0 || (m == best.0 && l >= r) {
            best = (m, k);
        }
    }
    let k = best.1;
    vec![stack(&b[..k]), stack(&b[k..])]
}

/// A key's width as drawn: each cap is its text plus a cell each side, a
/// joining word ([`catalog::parts`]) just its text, one cell between parts.
pub fn key_width(key: &str) -> u16 {
    let parts = catalog::parts(key);
    let w: u16 = parts.iter().map(|(p, cap)| if *cap { cells(p) + 2 } else { cells(p) }).sum();
    w + parts.len().saturating_sub(1) as u16
}

/// The help's state: the catalog, the filter and the scroll.
#[derive(Debug, Clone)]
pub struct Help {
    pub all: Vec<Section>,
    pub query: String,
    /// The first body line shown.
    pub top: usize,
    /// From the last frame: body rows and lines in the taller column, so
    /// the keys between frames scroll by what was on screen.
    pub page: usize,
    pub total: usize,
    /// The ✕ in the top border, from the last frame.
    pub close_at: Option<(u16, u16)>,
}

impl Help {
    pub fn new(all: Vec<Section>) -> Help {
        Help { all, query: String::new(), top: 0, page: 0, total: 0, close_at: None }
    }

    /// The sections the filter leaves.
    pub fn shown(&self) -> Vec<Section> {
        catalog::filter(&self.all, &self.query)
    }

    fn max_top(&self) -> usize {
        self.total.saturating_sub(self.page)
    }

    fn scroll(&mut self, d: i64) {
        let t = (self.top as i64 + d).max(0) as usize;
        self.top = t.min(self.max_top());
    }

    fn edited(&mut self) {
        self.top = 0;
    }

    /// Keys in order → true to close the help.
    pub fn handle(&mut self, keys: &[Key]) -> bool {
        keys.iter().any(|&k| self.key(k))
    }

    /// One key → true to close.
    pub fn key(&mut self, key: Key) -> bool {
        let page = self.page.saturating_sub(1).max(1) as i64;
        match key {
            Key::Esc if !self.query.is_empty() => {
                self.query.clear();
                self.edited();
            }
            Key::Esc | Key::CtrlC => return true,
            // `?` (the key that opened it) and `q` close, unless typing.
            Key::Char('?' | 'q') if self.query.is_empty() => return true,
            Key::Char(' ') if self.query.is_empty() => {}
            Key::Char(c) => {
                self.query.push(c);
                self.edited();
            }
            Key::Backspace => {
                self.query.pop();
                self.edited();
            }
            Key::CtrlU => {
                self.query.clear();
                self.edited();
            }
            Key::Up | Key::CtrlP => self.scroll(-1),
            Key::Down | Key::CtrlN => self.scroll(1),
            Key::PgUp => self.scroll(-page),
            Key::PgDn => self.scroll(page),
            Key::Home => self.top = 0,
            Key::End => self.top = self.max_top(),
            Key::Mouse(m) => return self.mouse(m),
            _ => {}
        }
        false
    }

    fn mouse(&mut self, m: Mouse) -> bool {
        if let Some(d) = m.wheel() {
            self.scroll(d as i64 * WHEEL as i64);
            return false;
        }
        m.is_click() && self.close_at.is_some_and(|(x, y)| m.row == y && (m.col == x || m.col + 1 == x || m.col == x + 1))
    }
}

/// Draw the whole frame (and keep its page size, line count and ✕ cell).
pub fn render(h: &mut Help, buf: &mut Buffer) {
    let area = buf.area;
    if area.width == 0 || area.height == 0 {
        return;
    }
    for c in buf.content.iter_mut() {
        c.reset();
    }
    buf.set_style(area, on("text", "base"));
    let (w, ht) = (area.width, area.height);
    if w < MIN_W || ht < MIN_H {
        put(buf, 0, 0, w, "keys · too small", fg("overlay"));
        h.close_at = None;
        return;
    }
    let s1 = fg("surface1");

    // The border, the ✕ at its top right.
    let mut x = put(buf, 0, 0, w, "╭─ ", s1);
    x = put(buf, x, 0, w, "keys", bold(fg("text")));
    x = put(buf, x, 0, w, " ", s1);
    while x < w - 1 {
        x = put(buf, x, 0, w - 1, "─", s1);
    }
    put(buf, w - 1, 0, w, "╮", s1);
    let cx = w - 4;
    put(buf, cx - 1, 0, w, " ✕ ", fg("overlay"));
    h.close_at = Some((cx, 0));
    for y in 1..ht - 1 {
        put(buf, 0, y, w, "│", s1);
        put(buf, w - 1, y, w, "│", s1);
    }
    put(buf, 0, ht - 1, w, "╰", s1);
    for x in 1..w - 1 {
        put(buf, x, ht - 1, w, "─", s1);
    }
    put(buf, w - 1, ht - 1, w, "╯", s1);
    rule(buf, 2, w);
    rule(buf, ht - 3, w);

    query_row(h, buf, w);
    let shown = h.shown();
    let body = Rect::new(2, 3, w - 4, ht - 6);
    body_lines(h, &shown, buf, body);
    footer(h, &shown, buf, w, ht - 2);
}

fn rule(buf: &mut Buffer, y: u16, w: u16) {
    let s1 = fg("surface1");
    put(buf, 0, y, w, "├", s1);
    for x in 1..w - 1 {
        put(buf, x, y, w, "─", s1);
    }
    put(buf, w - 1, y, w, "┤", s1);
}

fn query_row(h: &Help, buf: &mut Buffer, w: u16) {
    let y = 1;
    let end = w - 2;
    let x = put(buf, 2, y, end, "❯ ", bold(fg("blue")));
    if h.query.is_empty() {
        put(buf, x, y, end, "▏", fg("peach"));
        put(buf, x + 1, y, end, "type to filter: a key (opt w, cmd t) or words", fg("overlay"));
        return;
    }
    // The tail of a long query, so the cursor stays in sight.
    let room = end.saturating_sub(x + 1) as usize;
    let q = sanitize(&h.query);
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
    let x = put(buf, x, y, end, &shown, fg("text"));
    put(buf, x, y, end, "▏", fg("peach"));
}

/// How many columns for a content width.
pub fn ncols(width: u16) -> usize {
    if width >= TWO_COLS {
        2
    } else {
        1
    }
}

fn body_lines(h: &mut Help, shown: &[Section], buf: &mut Buffer, r: Rect) {
    let rows = r.height as usize;
    if shown.is_empty() {
        h.page = rows;
        h.total = 0;
        h.top = 0;
        if rows > 0 {
            let msg = format!("no key matches “{}”", sanitize(h.query.trim()));
            put(buf, r.x, r.y, r.x + r.width, &msg, fg("overlay"));
        }
        return;
    }
    // One cell on the right is the scrollbar's when the lines overflow.
    let n = ncols(r.width);
    let cols = columns(shown, n);
    let total = cols.iter().map(Vec::len).max().unwrap_or(0);
    let bar = total > rows && rows > 0;
    let width = if bar { r.width - 1 } else { r.width };
    let colw = if n == 2 { (width.saturating_sub(GAP)) / 2 } else { width };
    h.page = rows;
    h.total = total;
    h.top = h.top.min(total.saturating_sub(rows));
    // Each section's key column: its widest key, at most half the column
    // (so a long key in one section does not push every description over).
    let kw: Vec<u16> = shown
        .iter()
        .map(|s| s.entries.iter().map(|e| key_width(&e.key)).max().unwrap_or(0).min(colw / 2))
        .collect();
    for (c, lines) in cols.iter().enumerate() {
        let x = r.x + c as u16 * (colw + GAP);
        for (k, line) in lines.iter().enumerate().skip(h.top).take(rows) {
            let y = r.y + (k - h.top) as u16;
            match *line {
                Line::Header(i) => header(buf, x, y, colw, &shown[i].name),
                Line::Entry(i, j) => entry(buf, x, y, colw, kw[i], &shown[i].entries[j]),
                Line::Blank => {}
            }
        }
        if c == 1 {
            // The faint divider between the columns.
            let dx = x - GAP + GAP / 2;
            for k in 0..rows.min(total.saturating_sub(h.top)) {
                put(buf, dx, r.y + k as u16, dx + 1, "│", fg("surface0"));
            }
        }
    }
    if bar {
        let len = total;
        let thumb = (rows * rows / len).max(1);
        let pos = h.top * (rows - thumb) / (len - rows).max(1);
        let bx = r.x + r.width - 1;
        for k in 0..rows {
            let st = if (pos..pos + thumb).contains(&k) { fg("surface1") } else { fg("surface0") };
            put(buf, bx, r.y + k as u16, bx + 1, "▕", st);
        }
    }
}

/// `SPACES ────`: the label bold in overlay2, then a faint rule to the
/// column's end.
fn header(buf: &mut Buffer, x: u16, y: u16, w: u16, name: &str) {
    let end = x + w;
    let label = clip(&sanitize(name).to_uppercase(), w as usize);
    let mut cx = put(buf, x, y, end, &label, bold(fg("overlay2")));
    cx = put(buf, cx, y, end, " ", fg("surface0"));
    while cx < end {
        cx = put(buf, cx, y, end, "─", fg("surface0"));
    }
}

/// ` ⌥W   a-z   Jump to that agent…   dim note`: the key's caps, padded to
/// the column's key width, the description, then the note when it fits
/// whole.
fn entry(buf: &mut Buffer, x: u16, y: u16, w: u16, kw: u16, e: &catalog::Entry) {
    let end = x + w;
    let kend = (x + kw).min(end);
    let cap = bold(on("text", "surface0"));
    let mut cx = x;
    for (k, (part, is_cap)) in catalog::parts(&e.key).into_iter().enumerate() {
        if k > 0 {
            cx = put(buf, cx, y, kend, " ", Style::new());
        }
        let p = sanitize(part);
        if !is_cap {
            cx = put(buf, cx, y, kend, &p, fg("overlay"));
        } else {
            // A cap never spills past the key column: clipped inside it.
            let room = kend.saturating_sub(cx).saturating_sub(2) as usize;
            cx = put(buf, cx, y, kend, &format!(" {} ", clip(&p, room)), cap);
        }
    }
    let dx = kend + 2;
    if dx >= end {
        return;
    }
    let room = (end - dx) as usize;
    let desc = clip(&sanitize(&e.desc), room);
    let mut cx = put(buf, dx, y, end, &desc, fg("text"));
    if !e.note.is_empty() && cells(&desc) == cells(&sanitize(&e.desc)) {
        let note = sanitize(&e.note);
        if cx + 2 + cells(&note) <= end {
            cx = put(buf, cx + 2, y, end, &note, fg("overlay2"));
        }
    }
    let _ = cx;
}

fn footer(h: &Help, shown: &[Section], buf: &mut Buffer, w: u16, y: u16) {
    let end = w - 2;
    let ov = fg("overlay");
    let cap = on("text", "surface0");
    let mut cx = put(buf, 2, y, end, " esc ", cap);
    cx = put(buf, cx, y, end, if h.query.is_empty() { " close " } else { " clear " }, ov);
    if h.total > h.page {
        cx = put(buf, cx + 1, y, end, " ↑↓ ", cap);
        cx = put(buf, cx, y, end, " scroll ", ov);
    }
    let words = if h.query.is_empty() { "type to filter" } else { "⌫ edits" };
    cx = put(buf, cx + 1, y, end, words, ov);
    let all = catalog::count(&h.all);
    let n = catalog::count(shown);
    let count = if h.query.is_empty() { format!("{all} keys") } else { format!("{n} of {all}") };
    let cw = cells(&count);
    if end >= cw && end - cw > cx + 1 {
        put(buf, end - cw, y, end, &count, fg("overlay2"));
    }
}

#[cfg(test)]
mod tests {
    use super::super::catalog::{parse, tests::SAMPLE, EMBEDDED};
    use super::*;
    use crate::menu::render::dump;

    fn frame(h: &mut Help, w: u16, ht: u16) -> Vec<String> {
        let mut b = Buffer::empty(Rect::new(0, 0, w, ht));
        render(h, &mut b);
        dump(&b, false)
    }

    fn sized(n: &[usize]) -> Vec<Section> {
        let text: String = n
            .iter()
            .enumerate()
            .flat_map(|(i, k)| (0..*k).map(move |j| format!("S{i}\tk{j}\tdesc {i}.{j}\n")))
            .collect();
        parse(&text)
    }

    #[test]
    fn one_column_stacks_with_blanks() {
        let s = sized(&[2, 1]);
        let c = columns(&s, 1);
        assert_eq!(c.len(), 1);
        assert_eq!(c[0], [Line::Header(0), Line::Entry(0, 0), Line::Entry(0, 1), Line::Blank, Line::Header(1),
            Line::Entry(1, 0)]);
    }

    #[test]
    fn two_columns_balance_on_section_boundaries() {
        // Heights 4, 3, 5, 2 (+1 header each): the best split is after the
        // second section: 4+3+1 = 8 (left) vs 5+2+1 = 8 (right).
        let s = sized(&[3, 2, 4, 1]);
        let c = columns(&s, 2);
        assert_eq!(c.len(), 2);
        assert_eq!((c[0].len(), c[1].len()), (8, 8));
        assert_eq!(c[1][0], Line::Header(2));
        // One section: it all goes left.
        let c = columns(&sized(&[5]), 2);
        assert_eq!(c.len(), 1);
        // Heights 3, 2, 3: after the first (3 | 6) and after the second
        // (6 | 3) tie on the taller column; the taller one goes LEFT.
        let c = columns(&sized(&[2, 1, 2]), 2);
        assert_eq!((c[0].len(), c[1].len()), (6, 3), "{c:?}");
        assert!(columns(&[], 2)[0].is_empty());
    }

    #[test]
    fn key_widths() {
        assert_eq!(key_width("⌘T"), 4);
        assert_eq!(key_width("⌥W a-z"), 4 + 1 + 5);
        assert_eq!(key_width("h / l"), 3 + 1 + 1 + 1 + 3);
        assert_eq!(key_width("x then ⇧Y"), 3 + 1 + 4 + 1 + 4);
        assert_eq!(ncols(TWO_COLS), 2);
        assert_eq!(ncols(TWO_COLS - 1), 1);
    }

    #[test]
    fn keys_filter_scroll_and_close() {
        let mut h = Help::new(parse(SAMPLE));
        h.page = 3;
        h.total = 10;
        assert!(!h.handle(&[Key::Down, Key::Down, Key::CtrlN]));
        assert_eq!(h.top, 3);
        h.key(Key::PgDn);
        assert_eq!(h.top, 5); // clamped to total - page... 7 is max; page-1 = 2 → 5
        h.key(Key::End);
        assert_eq!(h.top, 7);
        h.key(Key::Home);
        assert_eq!(h.top, 0);
        h.key(Key::Up);
        assert_eq!(h.top, 0);
        h.key(Key::Mouse(Mouse { button: 65, col: 5, row: 5, press: true }));
        assert_eq!(h.top, WHEEL);
        // Typing filters (and goes back to the top); a leading space is not typed.
        assert!(!h.handle(&[Key::Char(' '), Key::Char('o'), Key::Char('p'), Key::Char('t')]));
        assert_eq!((h.query.as_str(), h.top), ("opt", 0));
        // q and ? type while a filter is up...
        assert!(!h.handle(&[Key::Char('q'), Key::Char('?')]));
        assert_eq!(h.query, "optq?");
        h.handle(&[Key::Backspace, Key::Backspace]);
        assert_eq!(h.query, "opt");
        assert_eq!(h.shown().iter().map(|s| s.entries.len()).sum::<usize>(), 2);
        // ...Esc clears it first, then closes.
        assert!(!h.key(Key::Esc));
        assert!(h.query.is_empty());
        assert!(h.key(Key::Esc));
        // q, ? and C-c close with an empty filter.
        let mut h = Help::new(parse(SAMPLE));
        assert!(h.key(Key::Char('q')));
        assert!(h.key(Key::Char('?')));
        assert!(h.key(Key::CtrlC));
        h.query = "abc".into();
        h.key(Key::CtrlU);
        assert!(h.query.is_empty());
        assert!(!h.key(Key::Enter) && !h.key(Key::Tab));
    }

    #[test]
    fn a_click_on_the_cross_closes() {
        let mut h = Help::new(parse(SAMPLE));
        let _ = frame(&mut h, 60, 20);
        let (x, y) = h.close_at.unwrap();
        let click = |col, row| Key::Mouse(Mouse { button: 0, col, row, press: true });
        assert!(!h.key(click(10, 10)));
        assert!(!h.key(Key::Mouse(Mouse { button: 0, col: x, row: y, press: false }))); // a release
        assert!(h.key(click(x, y)));
    }

    #[test]
    fn frame_one_column() {
        let mut h = Help::new(parse(SAMPLE));
        let f = frame(&mut h, 60, 16);
        assert_eq!(f.len(), 16);
        assert!(f[0].starts_with("╭─ keys ─") && f[0].ends_with("─ ✕ ─╮"), "{}", f[0]);
        assert!(f[1].starts_with("│ ❯ ▏type to filter"), "{}", f[1]);
        assert!(f[2].starts_with("├─") && f[13].starts_with("├─"));
        assert!(f[3].starts_with("│ SPACES ───"), "{}", f[3]);
        // Keys padded to their SECTION's widest: "⌘1-9" here (6 cells), and
        // "⌥W a-z" (10) under JUMP.
        assert!(f[4].starts_with("│  ⌘1-9   Go to space N  ⌃S 1-9 too"), "{}", f[4]);
        assert!(f[5].starts_with("│  ⌥N     New space"), "{}", f[5]);
        assert!(f[6].trim_end() == "│" || f[6].trim_end().ends_with("│"));
        assert!(f[7].starts_with("│ JUMP ───"), "{}", f[7]);
        // Two caps: each padded a cell either side, a blank between.
        assert!(f[8].starts_with("│  ⌥W   a-z   Jump to that agent"), "{}", f[8]);
        assert!(f[14].starts_with("│  esc  close  type to filter") && f[14].contains("4 keys"), "{}", f[14]);
        assert!(f[15].starts_with("╰") && f[15].ends_with("╯"));
        assert_eq!(frame(&mut h, 20, 5)[0], "keys · too small");
        assert!(frame(&mut h, 0, 0).is_empty());
    }

    #[test]
    fn frame_two_columns_and_no_match() {
        let mut h = Help::new(parse(EMBEDDED));
        let f = frame(&mut h, 120, 40);
        // Two section labels on the first body row, the divider between.
        assert!(f[3].starts_with("│ SPACES ─"), "{}", f[3]);
        assert_eq!(f[3].matches('│').count(), 3, "{}", f[3]);
        assert!(f[3].matches(" ─").count() >= 2);
        // Overflow: a scrollbar and the scroll keys in the footer.
        assert!(h.total > h.page);
        assert!(f[38].contains("↑↓  scroll"), "{}", f[38]);
        h.query = "zzzz".into();
        let f = frame(&mut h, 120, 40);
        assert!(f[3].starts_with("│ no key matches “zzzz”"), "{}", f[3]);
        assert!(f[38].contains("esc  clear") && f[38].contains("0 of"), "{}", f[38]);
    }

    #[test]
    fn scrolled_frames_keep_the_selection_of_lines() {
        let mut h = Help::new(sized(&[30]));
        let _ = frame(&mut h, 60, 16); // 10 body rows, 31 lines
        assert_eq!((h.page, h.total), (10, 31));
        h.key(Key::End);
        let f = frame(&mut h, 60, 16);
        assert!(f[12].contains("k29"), "{f:#?}");
        // The scrollbar's thumb sits at the bottom.
        assert!(f[12].trim_end().ends_with("▕ │"), "{}", f[12]);
        // A narrower frame reflows; the scroll is clamped, never past the end.
        h.top = 1000;
        let f = frame(&mut h, 60, 30);
        assert_eq!(h.top, 31 - 24);
        assert!(f[26].contains("k29"), "{}", f[26]);
    }
}
