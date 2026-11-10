//! The launcher's state ([`Launcher`]: the agents, where the tab goes, the
//! selection), its keys ([`Launcher::key`] → [`Act`]) and one frame of it
//! into a ratatui [`Buffer`], in the menu's style (its palette, rounded
//! border). No I/O.
//!
//! ```text
//! ╭─ launch ───────────────────────────── ✕ ─╮
//! │ here   2  notes                          │   where the tab goes (⌘⇧E: "pick an agent, then a space")
//! ├──────────────────────────────────────────┤
//! │ [C]  Claude  claude ················  c │   selected: lifted (tile_me)
//! │ [A]  Abby  abby ····················  a │   the zebra (tile / tile_lo)
//! │ [X]  Codex  codex ··················  x │   logo · name · command · leader · KEY
//! ├──────────────────────────────────────────┤
//! │  ⏎  launch   a-z  agent   esc  close     │
//! ╰──────────────────────────────────────────╯
//! ```
//!
//! Every row is ONE band: its tint (the zebra's two shades, the selected
//! row lifted) runs edge to edge, and a dotted leader carries the eye from
//! the name to the KEY, a solid key-cap at the row's right end: the
//! agent's letter, bold, dark on the agent's own colour. Each logo is the
//! agent's AgentMarks mark in its two-cell ligature form (the glyph, then
//! one CONTINUATION: [`mark`]) in its harness colour, every cell of it in
//! ONE style so WezTerm shapes it as one run.

use crate::menu::keys::{Key, Mouse};
use crate::model::Harness;
use crate::palette::color;
use crate::text::{clip, dwidth, sanitize};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};

/// Smaller than this, one line saying so.
pub const MIN_W: u16 = 24;
pub const MIN_H: u16 = 6;
/// The rows that are not agents: the border (2), the header line and its
/// rule (2), the footer's rule and the footer (2).
pub const CHROME_H: u16 = 6;
/// The zebra's two shades and the selected row's lift (the sidebar's).
pub const TONES: [&str; 2] = ["tile", "tile_lo"];
pub const LIFT: &str = "tile_me";

/// One installed agent, as `@launch_agents` records it (agent-launch.sh's
/// table): its ⌘E letter, its id (the harness: the logo) and the command
/// its tab runs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Agent {
    pub letter: char,
    pub id: String,
    pub command: String,
}

impl Agent {
    pub fn harness(&self) -> Harness {
        Harness::named(&self.id)
    }

    /// How the row names it: the brand's own spelling.
    pub fn name(&self) -> String {
        display_name(&self.id)
    }
}

/// An agent id as people write it (agy is Google's Antigravity CLI);
/// anything else capitalised.
pub fn display_name(id: &str) -> String {
    match id {
        "opencode" => "OpenCode".into(),
        "agy" => "Antigravity".into(),
        _ => {
            let mut c = id.chars();
            c.next().map(|f| f.to_uppercase().chain(c).collect()).unwrap_or_default()
        }
    }
}

/// The agent's logo, two cells: its glyph and one CONTINUATION, which the
/// AgentMarks font joins into one mark two cells wide.
pub fn mark(h: Harness) -> String {
    [h.glyph(), Harness::CONTINUATION].iter().collect()
}

/// The client's space, for the header.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Here {
    pub name: String,
    /// Its ⌘N (`@space_index`); None in a session that is not a space.
    pub index: Option<u32>,
    /// `@space_color`, when it is a `#rrggbb`.
    pub color: Option<Color>,
}

/// What a key asks of the I/O around the launcher.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Act {
    /// ⌘E: the agent on `letter` in a new tab beside this one.
    Launch(char),
    /// ⌘⇧E: the agent on `letter`, ⌘⇧R's space picker first.
    Pick(char),
    Close,
}

#[derive(Debug, Clone)]
pub struct Launcher {
    pub agents: Vec<Agent>,
    pub here: Option<Here>,
    /// ⌘⇧E: a chosen agent goes to the space picker, not straight here.
    pub pick_space: bool,
    /// The selected agent (an index into `agents`).
    pub sel: usize,
    /// The first row shown.
    pub top: usize,
    /// One line in place of the key bar until the next key ("no agent on b").
    pub flash: Option<String>,
    /// From the last frame: the ✕ and each row's (y, agent index), for clicks.
    pub close_at: Option<(u16, u16)>,
    pub rows_at: Vec<(u16, usize)>,
}

impl Launcher {
    /// Claude (agent-launch.sh's first) preselected.
    pub fn new(agents: Vec<Agent>, here: Option<Here>, pick_space: bool) -> Launcher {
        let sel = agents.iter().position(|a| a.id == "claude").unwrap_or(0);
        Launcher { agents, here, pick_space, sel, top: 0, flash: None, close_at: None, rows_at: Vec::new() }
    }

    fn step(&mut self, d: i64) {
        let n = self.agents.len() as i64;
        if n > 0 {
            self.sel = (self.sel as i64 + d).rem_euclid(n) as usize;
        }
    }

    fn choose(&self, i: usize) -> Option<Act> {
        let a = self.agents.get(i)?;
        Some(if self.pick_space { Act::Pick(a.letter) } else { Act::Launch(a.letter) })
    }

    /// Keys in order: the first that acts wins.
    pub fn handle(&mut self, keys: &[Key]) -> Option<Act> {
        keys.iter().find_map(|&k| self.key(k))
    }

    /// One key. A letter is that agent at once (either case: a ⇧ still held
    /// from ⌘⇧E does the same).
    pub fn key(&mut self, key: Key) -> Option<Act> {
        self.flash = None;
        match key {
            Key::Esc | Key::CtrlC | Key::AltW => return Some(Act::Close),
            Key::Enter => return self.choose(self.sel),
            Key::Up | Key::CtrlP => self.step(-1),
            Key::Down | Key::CtrlN => self.step(1),
            Key::Home => self.sel = 0,
            Key::End => self.sel = self.agents.len().saturating_sub(1),
            Key::Char(c) if c.is_ascii_alphabetic() => {
                let l = c.to_ascii_lowercase();
                if let Some(i) = self.agents.iter().position(|a| a.letter == l) {
                    return self.choose(i);
                }
                if l == 'q' {
                    return Some(Act::Close);
                }
                self.flash = Some(format!("no agent on {l}"));
            }
            Key::Mouse(m) => return self.mouse(m),
            _ => {}
        }
        None
    }

    fn mouse(&mut self, m: Mouse) -> Option<Act> {
        if let Some(d) = m.wheel() {
            self.step(d as i64);
            return None;
        }
        if !m.is_click() {
            return None;
        }
        if self.close_at.is_some_and(|(x, y)| m.row == y && m.col + 1 >= x && m.col <= x + 1) {
            return Some(Act::Close);
        }
        // A row: the first click selects it, a click on the selected one picks it.
        let (_, i) = *self.rows_at.iter().find(|(y, _)| *y == m.row)?;
        if i == self.sel {
            return self.choose(i);
        }
        self.sel = i;
        None
    }
}

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

/// The popup's height for `n` agents: every row plus the chrome.
pub fn height_for(n: usize) -> u16 {
    (n.max(1) as u16).saturating_add(CHROME_H)
}

/// Draw the whole frame (and keep its click map and the scroll).
pub fn render(l: &mut Launcher, buf: &mut Buffer) {
    let area = buf.area;
    l.close_at = None;
    l.rows_at.clear();
    if area.width == 0 || area.height == 0 {
        return;
    }
    for c in buf.content.iter_mut() {
        c.reset();
    }
    buf.set_style(area, on("text", "base"));
    let (w, h) = (area.width, area.height);
    if w < MIN_W || h < MIN_H {
        put(buf, 0, 0, w, "launch · too small", fg("overlay"));
        return;
    }
    let s1 = fg("surface1");
    let mut x = put(buf, 0, 0, w, "╭─ ", s1);
    x = put(buf, x, 0, w, "launch", bold(fg("text")));
    x = put(buf, x, 0, w, " ", s1);
    while x < w - 1 {
        x = put(buf, x, 0, w - 1, "─", s1);
    }
    put(buf, w - 1, 0, w, "╮", s1);
    let cx = w - 4;
    put(buf, cx - 1, 0, w, " ✕ ", fg("overlay"));
    l.close_at = Some((cx, 0));
    for y in 1..h - 1 {
        put(buf, 0, y, w, "│", s1);
        put(buf, w - 1, y, w, "│", s1);
    }
    put(buf, 0, h - 1, w, "╰", s1);
    for x in 1..w - 1 {
        put(buf, x, h - 1, w, "─", s1);
    }
    put(buf, w - 1, h - 1, w, "╯", s1);
    rule(buf, 2, w);
    rule(buf, h - 3, w);
    header(l, buf, w);
    rows(l, buf, Rect::new(1, 3, w - 2, h - CHROME_H));
    footer(l, buf, w, h - 2);
}

fn rule(buf: &mut Buffer, y: u16, w: u16) {
    let s1 = fg("surface1");
    put(buf, 0, y, w, "├", s1);
    for x in 1..w - 1 {
        put(buf, x, y, w, "─", s1);
    }
    put(buf, w - 1, y, w, "┤", s1);
}

/// `here  2  notes`: where ⌘E's tab goes (the space's ⌘N as a cap, its
/// name in its colour, as the sidebar's dock). ⌘⇧E: what comes next.
fn header(l: &Launcher, buf: &mut Buffer, w: u16) {
    let (y, end) = (1, w - 2);
    let ov = fg("overlay");
    if l.pick_space {
        put(buf, 2, y, end, "pick an agent, then a space", ov);
        return;
    }
    let mut x = put(buf, 2, y, end, "here ", ov);
    match &l.here {
        None => {
            put(buf, x, y, end, "this space", bold(fg("text")));
        }
        Some(t) => {
            if let Some(i) = t.index {
                x = put(buf, x, y, end, &format!(" {i} "), bold(on("text", "surface0")));
                x = put(buf, x, y, end, " ", Style::new());
            }
            let st = bold(t.color.map_or(fg("text"), |c| Style::new().fg(c)));
            put(buf, x, y, end, &clip(&sanitize(&t.name), end.saturating_sub(x) as usize), st);
        }
    }
}

/// The agents: each row one band (the zebra's shade, the selected one
/// lifted) from the left edge to its key-cap at the right end, a dotted
/// leader between the row's words and its key.
fn rows(l: &mut Launcher, buf: &mut Buffer, r: Rect) {
    let rows = r.height as usize;
    let n = l.agents.len();
    if rows == 0 {
        return;
    }
    if n == 0 {
        put(buf, r.x + 1, r.y, r.x + r.width, "no agent recorded (prefix r)", fg("overlay"));
        return;
    }
    l.sel = l.sel.min(n - 1);
    // The selection always in sight.
    if l.sel < l.top {
        l.top = l.sel;
    } else if l.sel >= l.top + rows {
        l.top = l.sel + 1 - rows;
    }
    l.top = l.top.min(n.saturating_sub(rows));
    let bar = n > rows;
    let end = r.x + r.width - u16::from(bar);
    for (k, i) in (l.top..n).take(rows).enumerate() {
        let y = r.y + k as u16;
        let a = &l.agents[i];
        let sel = i == l.sel;
        let bg = if sel { LIFT } else { TONES[i % 2] };
        buf.set_style(Rect::new(r.x, y, end - r.x, 1), Style::new().bg(color(bg)));
        let h = a.harness();
        let cap = format!(" {} ", a.letter);
        let cap_x = end - cells(&cap);
        // The words end two cells before the cap (a dot and a gap kept).
        let words_end = cap_x.saturating_sub(2);
        let mut x = put(buf, r.x + 1, y, words_end, &mark(h), on(h.hue(), bg));
        let nst = if sel { bold(on("text", bg)) } else { on("text", bg) };
        let name = sanitize(&a.name());
        x = put(buf, x + 2, y, words_end, &clip(&name, words_end.saturating_sub(x + 2) as usize), nst);
        // The command, dim, only where it fits whole after the name.
        let cmd = sanitize(&a.command);
        if x + 2 + cells(&cmd) + 2 <= cap_x {
            x = put(buf, x + 2, y, cap_x, &cmd, on(if sel { "overlay2" } else { "overlay" }, bg));
        }
        // The leader: the band's dots from the words to the key.
        let lead = on(if sel { "overlay1" } else { "surface2" }, bg);
        let mut dx = x + 1;
        while dx + 1 < cap_x {
            put(buf, dx, y, cap_x, "·", lead);
            dx += 1;
        }
        put(buf, cap_x, y, end, &cap, bold(on("crust", h.hue())));
        l.rows_at.push((y, i));
    }
    if bar {
        let thumb = (rows * rows / n).max(1);
        let pos = l.top * (rows - thumb) / (n - rows).max(1);
        for k in 0..rows {
            let st = if (pos..pos + thumb).contains(&k) { fg("surface2") } else { fg("surface0") };
            put(buf, end, r.y + k as u16, end + 1, "▕", st);
        }
    }
}

/// The key bar, as many of its keys as fit whole; a message in its place
/// until the next key.
fn footer(l: &Launcher, buf: &mut Buffer, w: u16, y: u16) {
    let end = w - 2;
    if let Some(m) = &l.flash {
        put(buf, 2, y, end, &clip(&sanitize(m), (end - 2) as usize), fg("peach"));
        return;
    }
    let ov = fg("overlay");
    let cap = on("text", "surface0");
    let items = [("⏎", if l.pick_space { "pick a space" } else { "launch" }), ("a-z", "agent"), ("esc", "close")];
    let mut x = 2;
    for (k, (key, word)) in items.iter().enumerate() {
        let gap = if k > 0 { 2 } else { 0 };
        if x + gap + cells(key) + 2 + 1 + cells(word) > end {
            break;
        }
        x += gap;
        x = put(buf, x, y, end, &format!(" {key} "), cap);
        x = put(buf, x + 1, y, end, word, ov);
    }
}

/// A frame's text with each AgentMarks glyph as `[I]` (its initial) and
/// the continuation cell dropped, so it reads without the font (`--once
/// --plain`, the tests).
pub fn initials(lines: &[String]) -> Vec<String> {
    lines
        .iter()
        .map(|s| {
            s.chars()
                .filter(|&c| c != Harness::CONTINUATION)
                .map(|c| match Harness::all().find(|h| h.glyph() == c) {
                    Some(h) => format!("[{}]", h.initial()),
                    None => c.to_string(),
                })
                .collect()
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::menu::render::dump;

    pub fn agents(raw: &[(char, &str, &str)]) -> Vec<Agent> {
        raw.iter().map(|&(letter, id, cmd)| Agent { letter, id: id.into(), command: cmd.into() }).collect()
    }

    /// Every agent agent-launch.sh knows, in its order.
    pub fn all() -> Vec<Agent> {
        agents(&[('c', "claude", "claude"), ('a', "abby", "abby"), ('x', "codex", "codex"), ('o', "opencode", "opencode"),
            ('g', "grok", "grok"), ('y', "agy", "agy"), ('u', "cursor", "cursor-agent"), ('p', "copilot", "copilot"),
            ('d', "aider", "aider")])
    }

    fn notes() -> Option<Here> {
        Some(Here { name: "notes".into(), index: Some(2), color: Some(Color::Rgb(0x94, 0xe2, 0xd5)) })
    }

    fn frame(l: &mut Launcher, w: u16, h: u16) -> Vec<String> {
        let mut b = Buffer::empty(Rect::new(0, 0, w, h));
        render(l, &mut b);
        dump(&b, false)
    }

    fn plain(lines: &[String]) -> Vec<String> {
        initials(lines)
    }

    #[test]
    fn names_and_marks() {
        assert_eq!(display_name("opencode"), "OpenCode");
        assert_eq!(display_name("agy"), "Antigravity");
        assert_eq!(display_name("cursor"), "Cursor");
        assert_eq!(display_name(""), "");
        let m = mark(Harness::named("codex"));
        assert_eq!(m, "\u{10C101}\u{10C1FF}");
        assert_eq!(dwidth(&m), 2);
        assert_eq!((height_for(9), height_for(0)), (15, 7));
    }

    #[test]
    fn keys_launch_move_and_close() {
        let mut l = Launcher::new(all(), notes(), false);
        assert_eq!(l.sel, 0);
        // ⏎: the selected agent (claude); a letter, at once, either case.
        assert_eq!(l.key(Key::Enter), Some(Act::Launch('c')));
        assert_eq!(l.key(Key::Char('x')), Some(Act::Launch('x')));
        assert_eq!(l.key(Key::Char('U')), Some(Act::Launch('u')));
        // ↓ / C-n / ↑ / C-p move, wrapping; Home / End.
        assert_eq!(l.handle(&[Key::Down, Key::CtrlN]), None);
        assert_eq!(l.sel, 2);
        l.handle(&[Key::Up, Key::CtrlP, Key::CtrlP]);
        assert_eq!(l.sel, 8);
        l.key(Key::Home);
        assert_eq!(l.sel, 0);
        l.key(Key::End);
        assert_eq!(l.key(Key::Enter), Some(Act::Launch('d')));
        // Nothing cycles: ⇥ / ⇧⇥ do nothing.
        assert_eq!((l.key(Key::Tab), l.key(Key::BackTab), l.sel), (None, None, 8));
        // A letter no agent has: a message; the next key clears it.
        assert_eq!(l.key(Key::Char('b')), None);
        assert_eq!(l.flash.as_deref(), Some("no agent on b"));
        l.key(Key::Down);
        assert_eq!(l.flash, None);
        // Esc, C-c, ⌥W and q close; space, digits, the rest: nothing.
        for k in [Key::Esc, Key::CtrlC, Key::AltW, Key::Char('q')] {
            assert_eq!(l.key(k), Some(Act::Close), "{k:?}");
        }
        for k in [Key::Char(' '), Key::Char('1'), Key::Backspace, Key::Other] {
            assert_eq!(l.key(k), None, "{k:?}");
        }
        // Claude not first: still preselected. No agents: nothing launches.
        let mut v = all();
        v.rotate_left(2);
        let l = Launcher::new(v, None, false);
        assert_eq!(l.agents[l.sel].id, "claude");
        let mut l = Launcher::new(vec![], notes(), false);
        assert_eq!((l.key(Key::Enter), l.key(Key::Char('c'))), (None, None));
    }

    #[test]
    fn cmd_shift_e_picks_then_hands_over_to_the_space_picker() {
        let mut l = Launcher::new(all(), notes(), true);
        assert_eq!(l.key(Key::Enter), Some(Act::Pick('c')));
        assert_eq!(l.key(Key::Char('X')), Some(Act::Pick('x')));
        l.key(Key::Down);
        assert_eq!(l.key(Key::Enter), Some(Act::Pick('a')));
        assert_eq!(l.key(Key::Esc), Some(Act::Close));
    }

    #[test]
    fn clicks_and_the_wheel() {
        let mut l = Launcher::new(all(), notes(), false);
        let _ = frame(&mut l, 50, 15);
        let click = |col, row| Key::Mouse(Mouse { button: 0, col, row, press: true });
        // The third row: selected, then launched.
        assert_eq!(l.key(click(10, 5)), None);
        assert_eq!(l.sel, 2);
        let _ = frame(&mut l, 50, 15);
        assert_eq!(l.key(click(10, 5)), Some(Act::Launch('x')));
        // The wheel moves; a release does nothing; the ✕ closes.
        l.key(Key::Mouse(Mouse { button: 65, col: 5, row: 5, press: true }));
        assert_eq!(l.sel, 3);
        let (x, y) = l.close_at.unwrap();
        assert_eq!(l.key(Key::Mouse(Mouse { button: 0, col: x, row: y, press: false })), None);
        assert_eq!(l.key(click(x, y)), Some(Act::Close));
    }

    #[test]
    fn frame_key_on_the_right_one_band_per_row() {
        let mut l = Launcher::new(all(), notes(), false);
        let f = frame(&mut l, 46, height_for(9));
        let p = plain(&f);
        assert_eq!(f.len(), 15);
        assert!(f[0].starts_with("╭─ launch ─") && f[0].ends_with("─ ✕ ─╮"), "{}", f[0]);
        assert!(p[1].starts_with("│ here  2  notes   ") && p[1].ends_with("   │"), "{}", p[1]);
        assert!(f[2].starts_with("├─") && f[12].starts_with("├─"));
        // Logo, name, command, the leader, the letter's cap at the right edge.
        assert!(p[3].starts_with("│ [C]  Claude  claude ···") && p[3].ends_with("·  c │"), "{}", p[3]);
        assert!(p[9].starts_with("│ [U]  Cursor  cursor-agent ···") && p[9].ends_with("·  u │"), "{}", p[9]);
        assert!(p[11].starts_with("│ [?]  Aider  aider ···"), "{}", p[11]); // no AgentMarks logo: OTHER's
        assert!(p[13].starts_with("│  ⏎  launch   a-z  agent   esc  close"), "{}", p[13]);
        assert!(f[14].starts_with("╰") && f[14].ends_with("╯"));
        // ⌘⇧E says what comes next; no space line of its own.
        let mut l = Launcher::new(all(), notes(), true);
        let p = plain(&frame(&mut l, 46, 15));
        assert!(p[1].starts_with("│ pick an agent, then a space"), "{}", p[1]);
        assert!(p[13].starts_with("│  ⏎  pick a space   a-z  agent"), "{}", p[13]);
        // Not a space: "this space".
        let mut l = Launcher::new(all(), None, false);
        assert!(frame(&mut l, 46, 15)[1].starts_with("│ here this space"));
    }

    #[test]
    fn styles_bands_caps_and_logos() {
        let mut l = Launcher::new(all(), notes(), false);
        let mut b = Buffer::empty(Rect::new(0, 0, 46, 15));
        render(&mut l, &mut b);
        // The logo's two cells: one style, the harness colour on the row's band.
        let (g, c) = (&b[(2, 3)], &b[(3, 3)]);
        assert_eq!((g.symbol(), c.symbol()), ("\u{10C100}", "\u{10C1FF}"));
        assert_eq!((g.fg, g.bg, g.modifier), (c.fg, c.bg, c.modifier));
        assert_eq!((g.fg, g.bg), (color("h_claude"), color(LIFT)));
        // Every cell from the left edge to the cap is the row's band: the
        // selected one lifted, the others the zebra, alternating.
        for x in 1..42 {
            assert_eq!(b[(x, 3)].bg, color(LIFT), "x {x}");
            assert_eq!(b[(x, 4)].bg, color(TONES[1]), "x {x}");
            assert_eq!(b[(x, 5)].bg, color(TONES[0]), "x {x}");
        }
        // The cap: solid, bold, dark on the agent's own colour, flush right.
        for (y, id, k) in [(3u16, "claude", "c"), (4, "abby", "a"), (5, "codex", "x")] {
            let (l, cap, r) = (&b[(42, y)], &b[(43, y)], &b[(44, y)]);
            for cell in [l, cap, r] {
                assert_eq!((cell.fg, cell.bg), (color("crust"), color(Harness::named(id).hue())), "{id}");
                assert!(cell.modifier.contains(Modifier::BOLD));
            }
            assert_eq!(cap.symbol(), k);
        }
        // The leader: dots on the band, brighter on the lifted row.
        assert_eq!((b[(40, 3)].symbol(), b[(40, 3)].fg), ("·", color("overlay1")));
        assert_eq!((b[(40, 4)].symbol(), b[(40, 4)].fg), ("·", color("surface2")));
        // The name bold only when selected; the header's space in its colour.
        assert!(b[(7, 3)].modifier.contains(Modifier::BOLD) && !b[(7, 4)].modifier.contains(Modifier::BOLD));
        assert_eq!(b[(12, 1)].fg, Color::Rgb(0x94, 0xe2, 0xd5));
    }

    #[test]
    fn short_and_narrow_frames() {
        let mut l = Launcher::new(all(), notes(), false);
        let f = frame(&mut l, 40, 10); // 4 rows
        assert_eq!(l.rows_at.iter().map(|r| r.1).collect::<Vec<_>>(), [0, 1, 2, 3]);
        assert!(f[3].ends_with(" c ▕│"), "{}", f[3]);
        l.key(Key::End);
        let f = plain(&frame(&mut l, 40, 10));
        assert_eq!(l.rows_at.iter().map(|r| r.1).collect::<Vec<_>>(), [5, 6, 7, 8]);
        assert!(f[6].starts_with("│ [?]  Aider"), "{f:#?}");
        // Narrow: the command goes before the name is cut; the cap always stays.
        let mut l = Launcher::new(all(), notes(), false);
        let f = plain(&frame(&mut l, 26, 15));
        assert!(!f[9].contains("cursor-agent") && f[9].contains("Cursor") && f[9].ends_with(" u │"), "{}", f[9]);
        assert_eq!(frame(&mut l, 20, 5)[0], "launch · too small");
        assert!(frame(&mut l, 0, 0).is_empty());
        // The flash replaces the key bar.
        l.key(Key::Char('b'));
        assert!(frame(&mut l, 46, 15)[13].starts_with("│ no agent on b"));
    }

    /// The text renders (run with `--nocapture`): ⌘E at 60x16 and 40x14,
    /// ⌘⇧E at 46x15.
    #[test]
    fn render_the_launcher() {
        for (w, h, pick) in [(60u16, 16u16, false), (40, 14, false), (46, 15, true)] {
            let mut l = Launcher::new(all(), notes(), pick);
            println!("{w}x{h}{}", if pick { " ⌘⇧E" } else { " ⌘E" });
            for line in plain(&frame(&mut l, w, h)) {
                println!("{line}");
            }
        }
    }
}
