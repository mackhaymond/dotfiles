//! `capture-pane -e` text → styled ratatui lines (for peeks).
//!
//! Only SGR (`ESC [ ... m`) is interpreted. Every other escape (OSC 8
//! hyperlinks, cursor moves) is dropped whole, and any other control
//! character becomes a space (agent-roster.py's `CONTROL` scrub), so nothing
//! from a pane can reach the terminal raw.

use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};

/// One captured line: its styled spans and its plain text.
#[derive(Debug, Clone, PartialEq)]
pub struct StyledLine {
    pub spans: Vec<(Style, String)>,
}

impl StyledLine {
    pub fn plain(&self) -> String {
        self.spans.iter().map(|(_, t)| t.as_str()).collect()
    }

    /// As a ratatui Line (owned).
    pub fn to_line(&self) -> Line<'static> {
        Line::from(self.spans.iter().map(|(s, t)| Span::styled(t.clone(), *s)).collect::<Vec<_>>())
    }

    /// Drop trailing whitespace (the Python `rstrip`), keeping styles.
    pub fn trim_end(&mut self) {
        while let Some((_, t)) = self.spans.last_mut() {
            let k = t.trim_end().len();
            t.truncate(k);
            if t.is_empty() {
                self.spans.pop();
            } else {
                break;
            }
        }
    }
}

fn color_256(n: u16) -> Color {
    Color::Indexed(n.min(255) as u8)
}

fn basic(n: u16) -> Color {
    [Color::Black, Color::Red, Color::Green, Color::Yellow, Color::Blue, Color::Magenta, Color::Cyan, Color::Gray]
        [n as usize % 8]
}

fn bright(n: u16) -> Color {
    [
        Color::DarkGray, Color::LightRed, Color::LightGreen, Color::LightYellow, Color::LightBlue,
        Color::LightMagenta, Color::LightCyan, Color::White,
    ][n as usize % 8]
}

/// Apply one SGR parameter list to `st`. Parameters are split on `;`; a
/// `:`-joined group is ONE parameter with its sub-parameters (ECMA-48 /
/// ITU T.416, which tmux's `capture-pane -e` passes through as written):
/// `4:n` an underline style (0: none), `38:5:n` / `38:2:[cs:]r:g:b` a
/// colour, the colour space slot skipped when present. (2026-10-22: the
/// colons were split like `;`, so nvim's undercurl `4:3` read as italic.)
fn apply_sgr(st: &mut Style, params: &str) {
    let ps: Vec<Vec<u16>> = if params.is_empty() {
        vec![vec![0]]
    } else {
        params.split(';').map(|g| g.split(':').map(|p| p.parse().unwrap_or(0)).collect()).collect()
    };
    let mut i = 0;
    while i < ps.len() {
        let g = &ps[i];
        let p = g[0];
        match p {
            0 => *st = Style::default(),
            1 => *st = st.add_modifier(Modifier::BOLD),
            2 => *st = st.add_modifier(Modifier::DIM),
            3 => *st = st.add_modifier(Modifier::ITALIC),
            4 if g.get(1) == Some(&0) => *st = st.remove_modifier(Modifier::UNDERLINED),
            4 => *st = st.add_modifier(Modifier::UNDERLINED),
            5 | 6 => *st = st.add_modifier(Modifier::SLOW_BLINK),
            7 => *st = st.add_modifier(Modifier::REVERSED),
            8 => *st = st.add_modifier(Modifier::HIDDEN),
            9 => *st = st.add_modifier(Modifier::CROSSED_OUT),
            21 | 22 => *st = st.remove_modifier(Modifier::BOLD | Modifier::DIM),
            23 => *st = st.remove_modifier(Modifier::ITALIC),
            24 => *st = st.remove_modifier(Modifier::UNDERLINED),
            25 => *st = st.remove_modifier(Modifier::SLOW_BLINK),
            27 => *st = st.remove_modifier(Modifier::REVERSED),
            28 => *st = st.remove_modifier(Modifier::HIDDEN),
            29 => *st = st.remove_modifier(Modifier::CROSSED_OUT),
            30..=37 => *st = st.fg(basic(p - 30)),
            39 => st.fg = None,
            40..=47 => *st = st.bg(basic(p - 40)),
            49 => st.bg = None,
            90..=97 => *st = st.fg(bright(p - 90)),
            100..=107 => *st = st.bg(bright(p - 100)),
            // The colon form: everything inside this one group.
            38 | 48 | 58 if g.len() > 1 => {
                let c = match g[1] {
                    5 => g.get(2).map(|&n| color_256(n)),
                    2 => {
                        let rgb = if g.len() >= 6 { &g[3..] } else { &g[2..] };
                        let at = |k: usize| rgb.get(k).copied().unwrap_or(0).min(255) as u8;
                        Some(Color::Rgb(at(0), at(1), at(2)))
                    }
                    _ => None,
                };
                if let Some(c) = c {
                    match p {
                        38 => *st = st.fg(c),
                        48 => *st = st.bg(c),
                        _ => {} // underline colour: ignored
                    }
                }
            }
            // The `;` form: the colour in the parameters that follow.
            38 | 48 | 58 => {
                let at = |k: usize| ps.get(i + k).map(|g| g[0]);
                let c = match at(1) {
                    Some(5) => {
                        let c = at(2).map(color_256);
                        i += 2;
                        c
                    }
                    Some(2) => {
                        let g = |k: usize| at(k).unwrap_or(0).min(255) as u8;
                        let c = Some(Color::Rgb(g(2), g(3), g(4)));
                        i += 4;
                        c
                    }
                    _ => None,
                };
                if let Some(c) = c {
                    match p {
                        38 => *st = st.fg(c),
                        48 => *st = st.bg(c),
                        _ => {} // underline colour: ignored
                    }
                }
            }
            _ => {}
        }
        i += 1;
    }
}

/// Parse one line of `capture-pane -e` output.
pub fn parse_line(s: &str) -> StyledLine {
    let mut spans: Vec<(Style, String)> = Vec::new();
    let mut st = Style::default();
    let mut cur = String::new();
    let cs: Vec<char> = s.chars().collect();
    let mut i = 0;
    let flush = |spans: &mut Vec<(Style, String)>, cur: &mut String, st: Style| {
        if !cur.is_empty() {
            spans.push((st, std::mem::take(cur)));
        }
    };
    while i < cs.len() {
        let c = cs[i];
        if c == '\x1b' {
            match cs.get(i + 1) {
                Some('[') => {
                    // CSI: params 0x30-0x3f, intermediates 0x20-0x2f, final 0x40-0x7e.
                    let mut j = i + 2;
                    while j < cs.len() && ('\x30'..='\x3f').contains(&cs[j]) {
                        j += 1;
                    }
                    while j < cs.len() && ('\x20'..='\x2f').contains(&cs[j]) {
                        j += 1;
                    }
                    if j < cs.len() && ('\x40'..='\x7e').contains(&cs[j]) {
                        if cs[j] == 'm' {
                            flush(&mut spans, &mut cur, st);
                            let params: String = cs[i + 2..j].iter().collect();
                            apply_sgr(&mut st, &params);
                        }
                        i = j + 1;
                    } else {
                        i = j; // torn sequence: drop what there was
                    }
                    continue;
                }
                Some(']') | Some('P') | Some('_') | Some('^') => {
                    // OSC / DCS / APC / PM: up to BEL or ST (ESC \).
                    let mut j = i + 2;
                    while j < cs.len() && cs[j] != '\x07' && !(cs[j] == '\x1b' && cs.get(j + 1) == Some(&'\\')) {
                        j += 1;
                    }
                    i = if j < cs.len() && cs[j] == '\x07' { j + 1 } else { (j + 2).min(cs.len()) };
                    continue;
                }
                Some(_) => {
                    i += 2; // two-byte escape
                    continue;
                }
                None => {
                    i += 1;
                    continue;
                }
            }
        }
        cur.push(if (c as u32) < 0x20 || (0x7f..=0x9f).contains(&(c as u32)) { ' ' } else { c });
        i += 1;
    }
    flush(&mut spans, &mut cur, st);
    StyledLine { spans }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sgr_and_junk() {
        let l = parse_line("a\x1b[1;31mb\x1b[0m c\x1b[38;2;1;2;3md\x1b[38;5;200me\x1b]8;;http://x\x1b\\f\x1b]8;;\x07\tg\x1b[2Kh");
        assert_eq!(l.plain(), "ab cdef gh");
        assert_eq!(l.spans[1].0, Style::default().add_modifier(Modifier::BOLD).fg(Color::Red));
        assert_eq!(l.spans[3].0.fg, Some(Color::Rgb(1, 2, 3)));
        assert_eq!(l.spans[4].0.fg, Some(Color::Indexed(200)));
        let mut t = parse_line("x  \x1b[7m   ");
        t.trim_end();
        assert_eq!(t.plain(), "x");
        assert_eq!(parse_line("\x1b[").plain(), "");
        assert_eq!(parse_line("tail\x1b").plain(), "tail");
    }

    /// `:` sub-parameters belong to their parameter: an undercurl is an
    /// underline (not italic), `4:0` none, and colon colours read r:g:b
    /// past an empty or given colour space slot.
    #[test]
    fn sgr_sub_parameters() {
        let st = |s: &str| parse_line(&format!("\x1b[{s}mx")).spans[0].0;
        let ul = Style::default().add_modifier(Modifier::UNDERLINED);
        assert_eq!(st("4:3"), ul);
        assert_eq!(st("4:2"), ul);
        assert_eq!(st("4:5"), ul);
        assert_eq!(st("1;4:3"), ul.add_modifier(Modifier::BOLD));
        assert_eq!(st("4;4:0"), Style::default().remove_modifier(Modifier::UNDERLINED));
        assert_eq!(st("38:2::10:20:30").fg, Some(Color::Rgb(10, 20, 30)));
        assert_eq!(st("38:2:1:10:20:30").fg, Some(Color::Rgb(10, 20, 30)));
        assert_eq!(st("48:2:10:20:30").bg, Some(Color::Rgb(10, 20, 30)));
        assert_eq!(st("38:5:200;1"), Style::default().fg(Color::Indexed(200)).add_modifier(Modifier::BOLD));
        assert_eq!(st("58:2::1:2:3;3"), Style::default().add_modifier(Modifier::ITALIC)); // underline colour: ignored
        // The `;` form as before.
        assert_eq!(st("38;2;1;2;3;1").fg, Some(Color::Rgb(1, 2, 3)));
        assert_eq!(st("48;5;9"), Style::default().bg(Color::Indexed(9)));
    }
}
