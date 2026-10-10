//! One frame of the picker into a ratatui [`Buffer`], in the menu's style
//! (its palette, rounded border, keycap footer):
//!
//! ```text
//! ╭─ sessions ─────────────────────────────────────╮
//! │ ❯ wo▏                                          │   the query
//! ├────────────────────────────────────────────────┤
//! │  work        ● 1 needs you · 1 working         │   matches (~30%)
//! │  workshop    ● 2 working                       │
//! ├─ work ─────────────────────────────────────────┤
//! │ (the session's active pane, bottom rows)       │   preview (~70%)
//! ├────────────────────────────────────────────────┤
//! │  ⏎  go   type to filter   esc  close           │   keys / confirm / message
//! ╰────────────────────────────────────────────────╯
//! ```
//!
//! The preview (fzf's `down:70%`) goes when the frame is too short for it.

use super::state::{Mode, Picker};
use crate::ansi::StyledLine;
use crate::palette::color;
use crate::text::{clip, dwidth, sanitize};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};

/// Smaller than this, one line saying so.
pub const MIN_W: u16 = 24;
pub const MIN_H: u16 = 7;
/// The preview needs at least this many frame rows.
pub const PREVIEW_MIN_H: u16 = 15;

fn fg(name: &str) -> Style {
    Style::new().fg(color(name))
}

fn on(f: &str, b: &str) -> Style {
    Style::new().fg(color(f)).bg(color(b))
}

fn bold(s: Style) -> Style {
    s.add_modifier(Modifier::BOLD)
}

/// Write `s` at (x, y), never past `end` → the x after it.
fn put(buf: &mut Buffer, x: u16, y: u16, end: u16, s: &str, st: Style) -> u16 {
    if x >= end {
        return x;
    }
    buf.set_stringn(x, y, s, (end - x) as usize, st).0
}

/// The frame's rows.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Geo {
    pub list: Rect,
    /// The preview's separator row and body.
    pub preview: Option<(u16, Rect)>,
    pub footer: u16,
}

impl Geo {
    pub fn new(w: u16, h: u16) -> Geo {
        let inner_w = w.saturating_sub(2);
        let footer = h.saturating_sub(2);
        if h < PREVIEW_MIN_H {
            return Geo { list: Rect::new(1, 3, inner_w, h.saturating_sub(6)), preview: None, footer };
        }
        // border, query, rule, list, rule, preview, rule, footer, border
        let avail = h - 7;
        let p = (u32::from(avail) * 7 / 10) as u16;
        let l = avail - p;
        let list = Rect::new(1, 3, inner_w, l);
        let sep = 3 + l;
        Geo { list, preview: Some((sep, Rect::new(1, sep + 1, inner_w, p))), footer }
    }

    /// The preview body's size, or None (hidden).
    pub fn preview_size(w: u16, h: u16) -> Option<(u16, u16)> {
        if w < MIN_W || h < MIN_H {
            return None; // "too small": nothing to capture for
        }
        Geo::new(w, h).preview.map(|(_, r)| (r.width.saturating_sub(2), r.height))
    }
}

fn rule(buf: &mut Buffer, y: u16, w: u16, label: Option<&str>) {
    let s1 = fg("surface1");
    let mut x = put(buf, 0, y, w, if label.is_some() { "├─ " } else { "├" }, s1);
    if let Some(l) = label {
        x = put(buf, x, y, w - 2, l, fg("sub"));
        x = put(buf, x, y, w - 1, " ", s1);
    }
    while x < w - 1 {
        x = put(buf, x, y, w - 1, "─", s1);
    }
    put(buf, w - 1, y, w, "┤", s1);
}

/// Draw the whole frame. `preview` is the capture of the selected session,
/// when there is one.
pub fn render(p: &mut Picker, buf: &mut Buffer, preview: Option<&[StyledLine]>) {
    let area = buf.area;
    if area.width == 0 || area.height == 0 {
        return;
    }
    for c in buf.content.iter_mut() {
        c.reset();
    }
    buf.set_style(area, on("text", "base"));
    let (w, h) = (area.width, area.height);
    if w < MIN_W || h < MIN_H {
        put(buf, 0, 0, w, "sessions · too small", fg("overlay"));
        return;
    }
    let geo = Geo::new(w, h);
    let s1 = fg("surface1");

    // The border.
    let mut x = put(buf, 0, 0, w, "╭─ ", s1);
    x = put(buf, x, 0, w, "sessions", bold(fg("text")));
    x = put(buf, x, 0, w, " ", s1);
    while x < w - 1 {
        x = put(buf, x, 0, w - 1, "─", s1);
    }
    put(buf, w - 1, 0, w, "╮", s1);
    for y in 1..h - 1 {
        put(buf, 0, y, w, "│", s1);
        put(buf, w - 1, y, w, "│", s1);
    }
    put(buf, 0, h - 1, w, "╰", s1);
    for x in 1..w - 1 {
        put(buf, x, h - 1, w, "─", s1);
    }
    put(buf, w - 1, h - 1, w, "╯", s1);
    rule(buf, 2, w, None);
    rule(buf, h - 3, w, None);

    query_row(p, buf, w);
    list(p, buf, geo.list);
    if let Some((sep, body)) = geo.preview {
        let name = p.selected().map(|r| clip(&sanitize(&r.name), (w as usize).saturating_sub(8)));
        rule(buf, sep, w, name.as_deref());
        if let Some(lines) = preview {
            preview_lines(buf, body, lines);
        }
    }
    footer(p, buf, w, geo.footer);
}

fn query_row(p: &Picker, buf: &mut Buffer, w: u16) {
    let y = 1;
    let end = w - 2;
    let mut x = put(buf, 2, y, end, "❯ ", bold(fg("blue")));
    if p.query.is_empty() {
        put(buf, x, y, end, "▏", fg("peach"));
        put(buf, x + 1, y, end, "type to filter, or a new name", fg("overlay"));
        return;
    }
    // The tail of a long query, so the cursor stays in sight.
    let room = end.saturating_sub(x + 1) as usize;
    let q = sanitize(&p.query);
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
    x = put(buf, x, y, end, &shown, fg("text"));
    put(buf, x, y, end, "▏", fg("peach"));
}

fn list(p: &mut Picker, buf: &mut Buffer, r: Rect) {
    let hgt = r.height as usize;
    let end = r.x + r.width;
    if p.matches.is_empty() {
        let t = p.target();
        let (msg, col) = if p.data.rows.is_empty() && t.is_empty() {
            ("No other sessions — type a name and press Enter to create one".to_string(), "overlay")
        } else if t == "scratch" || !super::rows::valid_name(t) {
            ("no match".to_string(), "overlay")
        } else if p.data.exists(t) {
            (format!("no match · ⏎ goes to {t}"), "overlay")
        } else {
            (format!("no match · ⏎ creates {t}"), "overlay")
        };
        if hgt > 0 {
            put(buf, r.x + 2, r.y, end, &msg, fg(col));
        }
        return;
    }
    // Scroll so the selection is in sight.
    if p.sel < p.top {
        p.top = p.sel;
    } else if hgt > 0 && p.sel >= p.top + hgt {
        p.top = p.sel + 1 - hgt;
    }
    p.top = p.top.min(p.matches.len().saturating_sub(hgt));
    // One column for the rollups, from the widest name of ALL rows (so it
    // does not move while filtering), at most half the width.
    let namew = p.data.rows.iter().map(|r| dwidth(&sanitize(&r.name))).max().unwrap_or(0).min(r.width as usize / 2);
    let bar = p.matches.len() > hgt && hgt > 0;
    let cend = if bar { end - 1 } else { end };
    for (k, (ri, score)) in p.matches.iter().enumerate().skip(p.top).take(hgt) {
        let y = r.y + (k - p.top) as u16;
        let row = &p.data.rows[*ri];
        let selected = k == p.sel;
        if selected {
            buf.set_style(Rect::new(r.x, y, cend - r.x, 1), Style::new().bg(color("surface1")));
        }
        let base = if selected { bold(fg("text")) } else { fg("text") };
        let hl = bold(fg("peach"));
        // The name, matched chars highlighted.
        let mut x = r.x + 2;
        let name: Vec<char> = sanitize(&row.name).chars().collect();
        let mut drawn = 0;
        // A name wider than the column ends in "…" (which takes its last cell).
        let limit = if dwidth(&sanitize(&row.name)) <= namew { namew } else { namew.saturating_sub(1) };
        for (i, c) in name.iter().enumerate() {
            let cw = dwidth(&c.to_string());
            if drawn + cw > limit {
                x = put(buf, x, y, cend, "…", base);
                break;
            }
            x = put(buf, x, y, cend, &c.to_string(), if score.pos.contains(&i) { hl } else { base });
            drawn += cw;
        }
        if let Some(ru) = row.rollup {
            let rx = r.x + 2 + namew as u16 + 3;
            let rx = rx.max(x + 1);
            let cx = put(buf, rx, y, cend, "●", fg(ru.color()));
            put(buf, cx + 1, y, cend, &ru.text(), fg("overlay"));
        }
    }
    if bar {
        let len = p.matches.len();
        let thumb = (hgt * hgt / len).max(1);
        let pos = p.top * (hgt - thumb) / (len - hgt).max(1);
        for k in 0..hgt {
            let st = if (pos..pos + thumb).contains(&k) { fg("surface1") } else { fg("surface0") };
            put(buf, end - 1, r.y + k as u16, end, "▕", st);
        }
    }
}

/// The capture in `r`: each line clipped to the width, the rest of the row
/// in the line's own last background (the script strips the closing
/// `ESC[0m` for that, as tmux's own preview paints to the edge).
fn preview_lines(buf: &mut Buffer, r: Rect, lines: &[StyledLine]) {
    // One blank column each side, like the list.
    let end = r.x + r.width.saturating_sub(1);
    let x0 = r.x + 1;
    if end <= x0 {
        return;
    }
    let k = lines.len().saturating_sub(r.height as usize);
    for (i, l) in lines[k..].iter().enumerate() {
        let y = r.y + i as u16;
        let mut x = x0;
        for (st, t) in &l.spans {
            x = put(buf, x, y, end, t, *st);
        }
        if let Some(bg) = l.spans.last().and_then(|(st, _)| st.bg) {
            if x < end {
                buf.set_style(Rect::new(x, y, end - x, 1), Style::new().bg(bg));
            }
        }
    }
}

fn footer(p: &Picker, buf: &mut Buffer, w: u16, y: u16) {
    let x = 2;
    let end = w - 2;
    if let Mode::Confirm(name) = &p.mode {
        let room = (end - x).saturating_sub(24) as usize;
        let cx = put(buf, x, y, end, &format!("Create and go to [{}]? ", clip(&sanitize(name), room)), fg("yellow"));
        put(buf, cx, y, end, "Y/n", bold(fg("text")));
        return;
    }
    if let Some(m) = &p.msg {
        put(buf, x, y, end, &sanitize(m), fg("red"));
        return;
    }
    let ov = fg("overlay");
    let cap = on("text", "surface0");
    let mut cx = put(buf, x, y, end, " ⏎ ", cap);
    cx = put(buf, cx, y, end, " go ", ov);
    cx = put(buf, cx + 1, y, end, "type to filter", ov);
    cx = put(buf, cx + 1, y, end, " esc ", cap);
    put(buf, cx, y, end, " close ", ov);
}

#[cfg(test)]
mod tests {
    use super::super::rows::tests::fixture;
    use super::super::rows::{build, Sessions};
    use super::super::state::Picker;
    use super::*;
    use crate::collate::Collator;
    use crate::menu::render::dump;

    fn frame(p: &mut Picker, w: u16, h: u16, prev: Option<&[StyledLine]>) -> Vec<String> {
        let mut b = Buffer::empty(Rect::new(0, 0, w, h));
        render(p, &mut b, prev);
        dump(&b, false)
    }

    fn picker() -> Picker {
        Picker::with(build(&fixture(), Some("/dev/ttys001"), &Collator::new("C")))
    }

    #[test]
    fn layout_100x30() {
        let mut p = picker();
        let prev: Vec<StyledLine> = (0..40).map(|i| crate::ansi::parse_line(&format!("line {i}"))).collect();
        let f = frame(&mut p, 100, 30, Some(&prev));
        assert_eq!(f.len(), 30);
        assert!(f[0].starts_with("╭─ sessions ─") && f[0].ends_with("╮"), "{}", f[0]);
        assert!(f[1].starts_with("│ ❯ ▏type to filter"), "{}", f[1]);
        assert!(f[2].starts_with("├──"));
        assert!(f[3].starts_with("│  alpha      ● 2 needs you"), "{}", f[3]);
        assert!(f[4].starts_with("│  work       ● 1 needs you · 1 working"), "{}", f[4]);
        assert!(f[5].contains("workshop   ● 2 working"), "{}", f[5]);
        assert!(f[7].starts_with("│  wiki") && !f[7].contains('●'), "{}", f[7]);
        // 23 rows between the rules: 7 list, 16 preview (70%).
        assert!(f[10].starts_with("├─ alpha ─"), "{}", f[10]);
        assert!(f[11].starts_with("│ line 24") && f[26].starts_with("│ line 39"), "{}\n{}", f[11], f[26]);
        assert!(f[27].starts_with("├──"));
        assert!(f[28].starts_with("│  ⏎  go  type to filter  esc  close  ") && f[28].ends_with('│'), "{}", f[28]);
        assert!(f[29].starts_with("╰") && f[29].ends_with("╯"));
    }

    #[test]
    fn short_frames_drop_the_preview() {
        let mut p = picker();
        let f = frame(&mut p, 60, 12, None);
        assert_eq!(f.len(), 12);
        assert!(f[3].contains("alpha") && f[7].contains("wiki"), "{f:#?}");
        assert!(f[9].starts_with("├──") && f[10].contains("⏎"));
        assert_eq!(Geo::preview_size(60, 12), None);
        assert_eq!(Geo::preview_size(100, 30), Some((96, 16)));
        assert_eq!(frame(&mut p, 10, 3, None)[0], "sessions ·");
        assert!(frame(&mut p, 0, 0, None).is_empty());
    }

    #[test]
    fn filter_confirm_and_messages() {
        let mut p = picker();
        p.handle(&[super::super::keys::Key::Char('s'), super::super::keys::Key::Char('h')]);
        let f = frame(&mut p, 80, 20, None);
        assert!(f[1].contains("❯ sh▏"), "{}", f[1]);
        assert!(f[3].contains("workshop") && !f.iter().any(|l| l.contains("alpha")), "{f:#?}");
        p.query = "newproj".into();
        p.handle(&[super::super::keys::Key::Backspace, super::super::keys::Key::Char('j')]);
        let f = frame(&mut p, 80, 20, None);
        assert!(f[3].contains("no match · ⏎ creates newproj"), "{}", f[3]);
        p.handle(&[super::super::keys::Key::Enter]);
        let f = frame(&mut p, 80, 20, None);
        assert!(f[18].contains("Create and go to [newproj]? Y/n"), "{}", f[18]);
        p.handle(&[super::super::keys::Key::Char('n')]);
        p.query = "a.b".into();
        p.handle(&[super::super::keys::Key::Enter]);
        let f = frame(&mut p, 80, 20, None);
        assert!(f[18].contains("Invalid session name (allowed: A-Z a-z 0-9 _ -): a.b"), "{}", f[18]);
        let mut empty = Picker::with(Sessions::default());
        let f = frame(&mut empty, 80, 20, None);
        assert!(f[3].contains("No other sessions — type a name and press Enter to create one"), "{}", f[3]);
    }

    #[test]
    fn scrolls_to_the_selection() {
        let mut p = picker();
        for _ in 0..4 {
            p.handle(&[super::super::keys::Key::Down]);
        }
        // 9 rows → 3 list rows: wiki (the 5th) must be on screen.
        let f = frame(&mut p, 60, 9, None);
        assert!(f[3..6].iter().any(|l| l.contains("wiki")), "{f:#?}");
        assert!(!f[3..6].iter().any(|l| l.contains("alpha")));
        assert!(f[3..6].iter().all(|l| l.trim_end().ends_with('│')));
    }
}
