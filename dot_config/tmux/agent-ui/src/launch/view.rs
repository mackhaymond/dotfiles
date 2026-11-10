//! The launcher's state ([`Launcher`]: the agents, the target spaces, the
//! selection), its keys ([`Launcher::key`] → [`Act`]) and one frame of it
//! into a ratatui [`Buffer`], in the menu's style (its palette, rounded
//! border, key caps). No I/O.
//!
//! ```text
//! ╭─ launch ───────────────────────────── ✕ ─╮
//! │ into   1  main  here           ⇥  space │   the target space (⇥ / ⇧⇥ cycle)
//! ├──────────────────────────────────────────┤
//! │ [C]  Claude       claude             c  │   selected: lifted, its cap peach
//! │ [A]  Abby         abby               a  │   logo · name · command · letter
//! │ [X]  Codex        codex              x  │
//! ├──────────────────────────────────────────┤
//! │  ⏎  launch   a-z  that one   ⇥  space   │   the keys (or a message)
//! ╰──────────────────────────────────────────╯
//! ```
//!
//! Each logo is the agent's AgentMarks mark in its two-cell ligature form
//! (the glyph, then one CONTINUATION: [`mark`]) in its harness colour,
//! every cell of it in ONE style so WezTerm shapes it as one run.

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
/// The rows that are not agents: the border (2), the target line and its
/// rule (2), the footer's rule and the footer (2).
pub const CHROME_H: u16 = 6;

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

/// A space the new tab can go to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Target {
    /// `$N`.
    pub sid: String,
    pub name: String,
    /// Its ⌘N (`@space_index`); None for the client's own session when it
    /// is not a space (a hidden one).
    pub index: Option<u32>,
    /// `@space_color`, when it is a `#rrggbb`.
    pub color: Option<Color>,
    /// The client's own space: the tab opens beside the current one, in
    /// its folder (as ⌘R); any other space gets it at its end, in its root.
    pub here: bool,
}

/// What a key asks of the I/O around the launcher.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Act {
    /// Launch the agent on `letter`; `space` None = beside this tab, else
    /// at the end of that space (`$N`).
    Launch { letter: char, space: Option<String> },
    Close,
}

#[derive(Debug, Clone)]
pub struct Launcher {
    pub agents: Vec<Agent>,
    pub targets: Vec<Target>,
    /// The selected agent (an index into `agents`).
    pub sel: usize,
    /// The target space (an index into `targets`).
    pub target: usize,
    /// The first agent row shown.
    pub top: usize,
    /// One line in place of the key bar until the next key ("no agent on b").
    pub flash: Option<String>,
    /// From the last frame: the ✕, the target line's row, and each agent
    /// row's (y, agent index), for clicks.
    pub close_at: Option<(u16, u16)>,
    pub header_y: Option<u16>,
    pub rows_at: Vec<(u16, usize)>,
}

impl Launcher {
    /// Claude (the first agent, agent-launch.sh's order) preselected; the
    /// target the client's own space (`here`), else the first.
    pub fn new(agents: Vec<Agent>, targets: Vec<Target>) -> Launcher {
        let target = targets.iter().position(|t| t.here).unwrap_or(0);
        let sel = agents.iter().position(|a| a.id == "claude").unwrap_or(0);
        Launcher { agents, targets, sel, target, top: 0, flash: None, close_at: None, header_y: None, rows_at: Vec::new() }
    }

    /// The space the selected target names; None = this one (or none known).
    fn space(&self) -> Option<String> {
        self.targets.get(self.target).filter(|t| !t.here).map(|t| t.sid.clone())
    }

    fn launch(&self, i: usize) -> Option<Act> {
        self.agents.get(i).map(|a| Act::Launch { letter: a.letter, space: self.space() })
    }

    fn step(&mut self, d: i64) {
        let n = self.agents.len() as i64;
        if n > 0 {
            self.sel = (self.sel as i64 + d).rem_euclid(n) as usize;
        }
    }

    fn cycle(&mut self, d: i64) {
        let n = self.targets.len() as i64;
        if n > 0 {
            self.target = (self.target as i64 + d).rem_euclid(n) as usize;
        }
    }

    /// Keys in order: the first that acts wins.
    pub fn handle(&mut self, keys: &[Key]) -> Option<Act> {
        keys.iter().find_map(|&k| self.key(k))
    }

    /// One key. A letter launches its agent at once (either case: a ⇧
    /// held from the old ⌘E ⇧letter does the same), into the target shown.
    pub fn key(&mut self, key: Key) -> Option<Act> {
        self.flash = None;
        match key {
            Key::Esc | Key::CtrlC | Key::AltW => return Some(Act::Close),
            Key::Enter => return self.launch(self.sel),
            Key::Up | Key::CtrlP => self.step(-1),
            Key::Down | Key::CtrlN => self.step(1),
            Key::Home => self.sel = 0,
            Key::End => self.sel = self.agents.len().saturating_sub(1),
            Key::Tab => self.cycle(1),
            Key::BackTab => self.cycle(-1),
            Key::Char(c) if c.is_ascii_alphabetic() => {
                let l = c.to_ascii_lowercase();
                if let Some(i) = self.agents.iter().position(|a| a.letter == l) {
                    return self.launch(i);
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
        if self.header_y == Some(m.row) {
            self.cycle(1);
            return None;
        }
        // A row: the first click selects it, a click on the selected one launches.
        let (_, i) = *self.rows_at.iter().find(|(y, _)| *y == m.row)?;
        if i == self.sel {
            return self.launch(i);
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
    l.header_y = None;
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
    target_line(l, buf, w);
    agent_rows(l, buf, Rect::new(1, 3, w - 2, h - CHROME_H));
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

/// `into  1  main  here        ⇥  space`: where the tab goes. The space's
/// ⌘N as a cap, its name in its colour; `⇥ space` on the right when there
/// is another space to go to.
fn target_line(l: &mut Launcher, buf: &mut Buffer, w: u16) {
    let y = 1;
    l.header_y = Some(y);
    let end = w - 2;
    let ov = fg("overlay");
    let cap = bold(on("text", "surface0"));
    let hint = l.targets.len() > 1;
    // The right side first, so the name gets what is left: `⇥ space`, or
    // only the cap when the frame is narrow.
    let word = hint && w >= 40;
    let right_w = match (hint, word) {
        (false, _) => 0,
        (true, false) => 3,
        (true, true) => 3 + 1 + 5,
    };
    let name_end = end.saturating_sub(if hint { right_w + 2 } else { 0 });
    let mut x = put(buf, 2, y, name_end, "into ", ov);
    match l.targets.get(l.target) {
        None => {
            put(buf, x, y, name_end, "this space", bold(fg("text")));
        }
        Some(t) => {
            if let Some(i) = t.index {
                x = put(buf, x, y, name_end, &format!(" {i} "), cap);
                x = put(buf, x, y, name_end, " ", Style::new());
            }
            let name = sanitize(&t.name);
            // `here` only beside the whole name; the name clipped last.
            let here = if t.here && x + cells(&name) + 6 <= name_end { "  here" } else { "" };
            let name = clip(&name, name_end.saturating_sub(x) as usize);
            let st = bold(t.color.map_or(fg("text"), |c| Style::new().fg(c)));
            x = put(buf, x, y, name_end, &name, st);
            put(buf, x, y, name_end, here, fg("overlay2"));
        }
    }
    if hint {
        let x = put(buf, end - right_w, y, end, " ⇥ ", on("text", "surface0"));
        if word {
            put(buf, x + 1, y, end, "space", ov);
        }
    }
}

fn agent_rows(l: &mut Launcher, buf: &mut Buffer, r: Rect) {
    let rows = r.height as usize;
    let n = l.agents.len();
    if n == 0 || rows == 0 {
        if rows > 0 {
            put(buf, r.x + 1, r.y, r.x + r.width, "no agent recorded (prefix r)", fg("overlay"));
        }
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
    // Columns: the logo at +1, the name at +4 padded to the widest, the
    // command two cells after, the letter's cap at the right (a pad, or the
    // scrollbar, after it).
    let end = r.x + r.width;
    let cap_x = end - 4;
    let name_w = l.agents.iter().map(|a| cells(&a.name())).max().unwrap_or(0);
    let name_x = r.x + 5;
    let cmd_x = name_x + name_w + 2;
    for (k, i) in (l.top..n).take(rows).enumerate() {
        let y = r.y + k as u16;
        let a = &l.agents[i];
        let sel = i == l.sel;
        let bg = if sel { "surface1" } else { "base" };
        buf.set_style(Rect::new(r.x, y, r.width, 1), Style::new().bg(color(bg)));
        let h = a.harness();
        put(buf, r.x + 1, y, cap_x, &mark(h), on(h.hue(), bg));
        let name = sanitize(&a.name());
        let nst = if sel { bold(on("text", bg)) } else { on("text", bg) };
        put(buf, name_x, y, cap_x.saturating_sub(1), &clip(&name, cap_x.saturating_sub(name_x + 1) as usize), nst);
        // The command only where it fits whole beside the name.
        let cmd = sanitize(&a.command);
        if cmd_x + cells(&cmd) + 1 < cap_x {
            put(buf, cmd_x, y, cap_x, &cmd, on(if sel { "overlay2" } else { "overlay" }, bg));
        }
        let cap = if sel { bold(on("crust", "peach")) } else { bold(on("text", "surface0")) };
        put(buf, cap_x, y, end, &format!(" {} ", a.letter), cap);
        l.rows_at.push((y, i));
    }
    if bar {
        let len = n;
        let thumb = (rows * rows / len).max(1);
        let pos = l.top * (rows - thumb) / (len - rows).max(1);
        let bx = end - 1;
        for k in 0..rows {
            let st = if (pos..pos + thumb).contains(&k) { fg("surface2") } else { fg("surface0") };
            let y = r.y + k as u16;
            let bg = if l.rows_at.get(k).is_some_and(|&(_, i)| i == l.sel) { "surface1" } else { "base" };
            put(buf, bx, y, end, "▕", st.bg(color(bg)));
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
    // In order of use: the letters are on every row's cap already.
    let mut items: Vec<(&str, &str)> = vec![("⏎", "launch")];
    if l.targets.len() > 1 {
        items.push(("⇥", "space"));
    }
    items.extend([("esc", "close"), ("a-z", "that one")]);
    let mut x = 2;
    for (k, (key, word)) in items.iter().enumerate() {
        let gap = if k > 0 { 2 } else { 0 };
        let need = gap + cells(key) + 2 + 1 + cells(word);
        if x + need > end {
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

    fn sp(sid: &str, name: &str, index: Option<u32>, here: bool) -> Target {
        Target { sid: sid.into(), name: name.into(), index, color: None, here }
    }

    pub fn spaces() -> Vec<Target> {
        vec![sp("$0", "main", Some(1), false), sp("$3", "notes", Some(2), true), sp("$5", "cua-notch", Some(3), false)]
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
        assert_eq!(height_for(9), 15);
        assert_eq!(height_for(0), 7);
    }

    #[test]
    fn claude_first_and_this_space() {
        let l = Launcher::new(all(), spaces());
        assert_eq!((l.sel, l.target), (0, 1));
        // Claude not first: still preselected. No space marked here: the first.
        let mut v = all();
        v.rotate_left(2);
        let l = Launcher::new(v, vec![sp("$0", "main", Some(1), false)]);
        assert_eq!((l.agents[l.sel].id.as_str(), l.target), ("claude", 0));
    }

    #[test]
    fn keys_launch_move_cycle_and_close() {
        let mut l = Launcher::new(all(), spaces());
        // ⏎: the selected agent (claude), beside this tab.
        assert_eq!(l.key(Key::Enter), Some(Act::Launch { letter: 'c', space: None }));
        // A letter: that agent at once, either case.
        assert_eq!(l.key(Key::Char('x')), Some(Act::Launch { letter: 'x', space: None }));
        assert_eq!(l.key(Key::Char('U')), Some(Act::Launch { letter: 'u', space: None }));
        // ↓ / C-n / ↑ / C-p move, wrapping; Home / End.
        assert_eq!(l.handle(&[Key::Down, Key::CtrlN]), None);
        assert_eq!(l.sel, 2);
        l.key(Key::Up);
        l.key(Key::CtrlP);
        l.key(Key::CtrlP);
        assert_eq!(l.sel, 8);
        l.key(Key::Home);
        assert_eq!(l.sel, 0);
        l.key(Key::End);
        assert_eq!(l.key(Key::Enter), Some(Act::Launch { letter: 'd', space: None }));
        // ⇥ / ⇧⇥: another space; a launch then names it.
        l.key(Key::Tab);
        assert_eq!(l.target, 2);
        assert_eq!(l.key(Key::Char('o')), Some(Act::Launch { letter: 'o', space: Some("$5".into()) }));
        l.key(Key::Tab);
        assert_eq!(l.target, 0);
        l.key(Key::BackTab);
        l.key(Key::BackTab);
        assert_eq!(l.target, 1);
        assert_eq!(l.key(Key::Char('c')), Some(Act::Launch { letter: 'c', space: None }));
        // A letter no agent has: a message, nothing launched; the next key clears it.
        assert_eq!(l.key(Key::Char('b')), None);
        assert_eq!(l.flash.as_deref(), Some("no agent on b"));
        l.key(Key::Down);
        assert_eq!(l.flash, None);
        // Esc, C-c, ⌥W and q close (q is no agent's letter).
        for k in [Key::Esc, Key::CtrlC, Key::AltW, Key::Char('q')] {
            assert_eq!(l.key(k), Some(Act::Close), "{k:?}");
        }
        // Space, digits, the rest: nothing.
        for k in [Key::Char(' '), Key::Char('1'), Key::Backspace, Key::Other] {
            assert_eq!(l.key(k), None, "{k:?}");
        }
        // No targets known: always beside this tab.
        let mut l = Launcher::new(all(), vec![]);
        l.key(Key::Tab);
        assert_eq!(l.key(Key::Enter), Some(Act::Launch { letter: 'c', space: None }));
        // No agents: ⏎ does nothing, letters say so.
        let mut l = Launcher::new(vec![], spaces());
        assert_eq!((l.key(Key::Enter), l.key(Key::Char('c'))), (None, None));
    }

    #[test]
    fn clicks_and_the_wheel() {
        let mut l = Launcher::new(all(), spaces());
        let _ = frame(&mut l, 50, 15);
        let click = |col, row| Key::Mouse(Mouse { button: 0, col, row, press: true });
        // The third row: selected, then launched.
        assert_eq!(l.key(click(10, 5)), None);
        assert_eq!(l.sel, 2);
        let _ = frame(&mut l, 50, 15);
        assert_eq!(l.key(click(10, 5)), Some(Act::Launch { letter: 'x', space: None }));
        // The target line cycles; the wheel moves.
        assert_eq!(l.key(click(10, 1)), None);
        assert_eq!(l.target, 2);
        l.key(Key::Mouse(Mouse { button: 65, col: 5, row: 5, press: true }));
        assert_eq!(l.sel, 3);
        // A release does nothing; the ✕ closes.
        let (x, y) = l.close_at.unwrap();
        assert_eq!(l.key(Key::Mouse(Mouse { button: 0, col: x, row: y, press: false })), None);
        assert_eq!(l.key(click(x, y)), Some(Act::Close));
    }

    #[test]
    fn frame_fits_its_content() {
        let mut l = Launcher::new(all(), spaces());
        let f = frame(&mut l, 46, height_for(9));
        let p = plain(&f);
        assert_eq!(f.len(), 15);
        assert!(f[0].starts_with("╭─ launch ─") && f[0].ends_with("─ ✕ ─╮"), "{}", f[0]);
        assert!(p[1].starts_with("│ into  2  notes  here") && p[1].contains(" ⇥  space │"), "{}", p[1]);
        assert!(f[2].starts_with("├─") && f[12].starts_with("├─"));
        // Rows: logo, name, command, the letter's cap at the right.
        assert!(p[3].starts_with("│ [C]  Claude       claude") && p[3].ends_with(" c  │"), "{}", p[3]);
        assert!(p[9].starts_with("│ [U]  Cursor       cursor-agent") && p[9].ends_with(" u  │"), "{}", p[9]);
        assert!(p[11].starts_with("│ [?]  Aider"), "{}", p[11]); // no AgentMarks logo: OTHER's
        assert!(p[13].starts_with("│  ⏎  launch   ⇥  space   esc  close"), "{}", p[13]);
        assert!(f[14].starts_with("╰") && f[14].ends_with("╯"));
    }

    #[test]
    fn styles_one_run_per_logo_and_the_lift() {
        let mut l = Launcher::new(all(), spaces());
        let mut b = Buffer::empty(Rect::new(0, 0, 46, 15));
        render(&mut l, &mut b);
        // The logo's two cells: one style, the harness colour on the row's bg.
        let (g, c) = (&b[(2, 3)], &b[(3, 3)]);
        assert_eq!(g.symbol(), "\u{10C100}");
        assert_eq!(c.symbol(), "\u{10C1FF}");
        assert_eq!((g.fg, g.bg, g.modifier), (c.fg, c.bg, c.modifier));
        assert_eq!((g.fg, g.bg), (color("h_claude"), color("surface1")));
        // Unselected rows sit on base; the selected cap is peach, the others surface0.
        assert_eq!(b[(3, 4)].bg, color("base"));
        assert_eq!(b[(43, 3)].bg, color("peach"));
        assert_eq!(b[(43, 4)].bg, color("surface0"));
        assert!(b[(6, 3)].modifier.contains(Modifier::BOLD));
    }

    #[test]
    fn short_frames_scroll_to_the_selection() {
        let mut l = Launcher::new(all(), spaces());
        let f = frame(&mut l, 40, 10); // 4 agent rows
        assert_eq!(l.rows_at.iter().map(|r| r.1).collect::<Vec<_>>(), [0, 1, 2, 3]);
        assert!(f[3].ends_with("▕│"), "{}", f[3]);
        l.key(Key::End);
        let f = plain(&frame(&mut l, 40, 10));
        assert_eq!(l.rows_at.iter().map(|r| r.1).collect::<Vec<_>>(), [5, 6, 7, 8]);
        assert!(f[6].starts_with("│ [?]  Aider"), "{f:#?}");
        // Narrow: the command goes before the name is cut; a long space name is clipped.
        let mut l = Launcher::new(all(), vec![sp("$1", "a-very-long-space-name-indeed", Some(12), true), sp("$2", "x", Some(1), false)]);
        let f = plain(&frame(&mut l, 28, 15));
        assert!(!f[9].contains("cursor-agent") && f[9].contains("Cursor"), "{}", f[9]);
        assert!(f[1].starts_with("│ into  12  a-very-") && f[1].contains("…") && f[1].ends_with(" ⇥  │"), "{}", f[1]);
        assert!(!f[1].contains("here"), "{}", f[1]);
        assert_eq!(frame(&mut l, 20, 5)[0], "launch · too small");
        assert!(frame(&mut l, 0, 0).is_empty());
        // The flash replaces the key bar.
        let mut l = Launcher::new(all(), spaces());
        l.key(Key::Char('b'));
        assert!(frame(&mut l, 46, 15)[13].starts_with("│ no agent on b"));
    }

    /// The text renders (run with `--nocapture`): 60x16 and 40x14.
    #[test]
    fn render_the_launcher() {
        for (w, h) in [(60u16, 16u16), (40, 14), (46, 15)] {
            let mut l = Launcher::new(all(), spaces());
            println!("{w}x{h}");
            for line in plain(&frame(&mut l, w, h)) {
                println!("{line}");
            }
        }
    }
}
