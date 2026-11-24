//! The review popup's state ([`Review`]: the candidates, what was done to
//! each, the screen it is on), its keys ([`Review::key`] → [`Req`]) and one
//! frame of it into a ratatui [`Buffer`], in the sidebar's style (its
//! canvas and tiles). No I/O: the runner in `review.rs`
//! carries out each [`Req`] and reports back ([`Review::record`],
//! [`Review::refused`], [`Review::prompt_space`], [`Review::advance`]).
//!
//! ```text
//! ┌──────────────────────────────────────────────────────────┐
//! │ Review old agents  idle 8h+         ━━ ━━ ━━ ━━   2 of 4 │   a segment each, its outcome's colour
//! │                                                          │
//! │ 1 main              ▎  x  Fix the sidebar tests          │   the queue (when wide) · the card
//! │  d  Toasts    kept  ▎ ✻ claude  1 main  ✓ done 2d 3h ago │
//! │  x  Fix the s…  2d                                       │
//! │                     Last reply                           │   @agent_detail, wrapped
//! │ 2 cua-notch         The hardened build is installed and …│
//! │  g  Dock polish 9h                                       │
//! │  h  Counts      8h  On its screen                        │   the pane's last lines, tidied
//! │                      ❯ now make it clickable             │
//! │                     kept Toasts · asked again once …     │   the last message, its own row
//! │  c  close   p  park   s  snooze   k  keep   v   ←→   q   │   the dock: the keys, always there
//! └──────────────────────────────────────────────────────────┘
//! ```

use crate::launch::view::mark;
use crate::menu::keys::Key;
use crate::model::{Cat, Harness};
use crate::palette::{color, CANVAS};
use crate::text::{clip, dwidth, sanitize};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};

/// ← and →, as the runner hands them over. The shared key reader
/// (`menu::keys`) swallows Left/Right for every other popup, so the runner
/// rewrites their sequences to these private-use chars before it reads keys.
pub const LEFT: char = '\u{E0D0}';
pub const RIGHT: char = '\u{E0D1}';

/// Smaller than this, one line saying so.
pub const MIN_W: u16 = 36;
pub const MIN_H: u16 = 12;

/// One agent up for review, as the screen shows it.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Cand {
    /// `@N`.
    pub id: String,
    /// Its session: the space's name.
    pub session: String,
    /// The space's ⌘N.
    pub space_index: Option<u32>,
    /// `@space_color`, when it is a `#rrggbb`.
    pub space_color: Option<Color>,
    /// `@agent_key`, as stored: an opaque string (a letter, or a two-char
    /// overflow key such as `;a`); "" = none.
    pub key: String,
    /// `@agent_summary`, else the window name.
    pub title: String,
    pub harness: Harness,
    /// idle | done | failed | needs-input (paused folded in never reaches here).
    pub state: String,
    /// The `@agent_since` epoch: idle since then.
    pub since: i64,
    /// Its last reply (`@agent_detail` when it is a done / ask text), plain.
    pub reply: String,
    /// The pane's last lines; None until the runner captured them.
    pub preview: Option<Vec<String>>,
}

/// What was done to a candidate.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    Closed,
    Parked,
    Snoozed,
    Kept,
}

impl Outcome {
    pub fn word(self) -> &'static str {
        match self {
            Outcome::Closed => "closed",
            Outcome::Parked => "parked",
            Outcome::Snoozed => "snoozed",
            Outcome::Kept => "kept",
        }
    }

    /// Its colour, on its key, its badge and its segment of the header.
    pub fn hue(self) -> &'static str {
        match self {
            Outcome::Closed => "red",
            Outcome::Parked => "mauve",
            Outcome::Snoozed => "peach",
            Outcome::Kept => "teal",
        }
    }

    /// The agent left its space (closed, parked or snoozed).
    pub fn removes(self) -> bool {
        !matches!(self, Outcome::Kept)
    }
}

/// A space whose last agent this review just took away.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SpaceInfo {
    /// tmux's `$N`.
    pub sid: String,
    pub name: String,
    /// Its tabs without an agent (home included, as prefix Q counts them).
    pub shells: usize,
    /// The windows this review took out of it (the close waits for them to go).
    pub wins: Vec<String>,
}

/// One snooze choice: its key, its words, and the text `snooze.sh parse` reads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Preset {
    pub key: char,
    pub label: String,
    pub text: String,
}

/// snooze.sh's menu, in its order and with its keys: 1 hour, 3 hours, this
/// evening (tomorrow evening from 18:00 on, as the menu), tomorrow 9:00,
/// Monday 9:00. Custom… (`c`) follows them on screen.
pub fn presets(hour: u8) -> Vec<Preset> {
    let (el, et) = if hour < 18 { ("This evening 18:00", "tonight") } else { ("Tomorrow evening 18:00", "tomorrow 18:00") };
    [('1', "1 hour", "1h"), ('3', "3 hours", "3h"), ('e', el, et), ('t', "Tomorrow 9:00", "tomorrow"), ('m', "Monday 9:00", "mon")]
        .iter()
        .map(|&(key, label, text)| Preset { key, label: label.into(), text: text.into() })
        .collect()
}

/// The screen.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Mode {
    /// One agent: its card and the action keys.
    Agent,
    /// The snooze chooser for the current agent: `sel` over the presets and
    /// Custom… (the last row); `typing` is the Custom… box while it is open.
    Snooze { sel: usize, typing: Option<String>, err: Option<String> },
    /// "No agents left in X. Close the space? y/n".
    Space(SpaceInfo),
    /// The summary (or "Nothing to review"); any key closes.
    Done,
}

/// What a key asks of the runner.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Req {
    Close(usize),
    Park(usize),
    /// Snooze candidate `.0` until the time `.1` reads as (`snooze.sh parse`).
    Snooze(usize, String),
    Keep(usize),
    /// Close the popup and go to it.
    Jump(usize),
    CloseSpace(SpaceInfo),
    Quit,
}

#[derive(Debug, Clone)]
pub struct Review {
    pub cands: Vec<Cand>,
    pub outcomes: Vec<Option<Outcome>>,
    /// The candidate on screen.
    pub cur: usize,
    pub mode: Mode,
    /// One line over the key bar until the next key: (text, a warning).
    pub flash: Option<(String, bool)>,
    pub presets: Vec<Preset>,
    /// Epoch seconds, for "idle 2d 3h".
    pub now: i64,
    /// The threshold, for the empty screen's words.
    pub min_idle: i64,
    pub spaces_closed: usize,
    /// An action is out and not yet seen to land: every key but quit waits.
    pub busy: bool,
}

/// `2d 3h`, `9h 5m`, `42m` (never empty: under a minute is `0m`).
pub fn span(secs: i64) -> String {
    let s = secs.max(0);
    let (d, h, m) = (s / 86_400, s % 86_400 / 3600, s % 3600 / 60);
    if d > 0 {
        if h > 0 { format!("{d}d {h}h") } else { format!("{d}d") }
    } else if h > 0 {
        if m > 0 { format!("{h}h {m}m") } else { format!("{h}h") }
    } else {
        format!("{m}m")
    }
}

impl Review {
    pub fn new(cands: Vec<Cand>, now: i64, hour: u8, min_idle: i64) -> Review {
        let n = cands.len();
        Review {
            cands,
            outcomes: vec![None; n],
            cur: 0,
            mode: if n == 0 { Mode::Done } else { Mode::Agent },
            flash: None,
            presets: presets(hour),
            now,
            min_idle,
            spaces_closed: 0,
            busy: false,
        }
    }

    /// How many got this outcome.
    pub fn count(&self, o: Outcome) -> usize {
        self.outcomes.iter().filter(|x| **x == Some(o)).count()
    }

    /// The windows of `session` this review took out of it.
    pub fn removed_in(&self, session: &str) -> Vec<String> {
        self.cands
            .iter()
            .zip(&self.outcomes)
            .filter(|(c, o)| c.session == session && o.is_some_and(Outcome::removes))
            .map(|(c, _)| c.id.clone())
            .collect()
    }

    /// The runner did it: remember the outcome and say so (the screen does
    /// not move: [`advance`](Self::advance) or [`prompt_space`](Self::prompt_space) next).
    pub fn record(&mut self, i: usize, o: Outcome, msg: String) {
        if let Some(slot) = self.outcomes.get_mut(i) {
            *slot = Some(o);
        }
        self.mode = Mode::Agent;
        self.flash = Some((msg, false));
    }

    /// The runner would not (or could not): the reason, nothing changed.
    /// In the Custom… box the reason shows under it, and the box stays.
    pub fn refused(&mut self, msg: String) {
        if let Mode::Snooze { typing: Some(_), err, .. } = &mut self.mode {
            *err = Some(msg);
            return;
        }
        self.mode = Mode::Agent;
        self.flash = Some((msg, true));
    }

    /// The y/n question (its keys replace any message: the card's badge
    /// already says what was done).
    pub fn prompt_space(&mut self, info: SpaceInfo) {
        self.mode = Mode::Space(info);
        self.flash = None;
    }

    /// On to the next candidate (decided or not: the order never jumps), or
    /// the summary after the last.
    pub fn advance(&mut self) {
        if self.cur + 1 < self.cands.len() {
            self.cur += 1;
            self.mode = Mode::Agent;
        } else {
            self.mode = Mode::Done;
            self.flash = None;
        }
    }

    /// Keys in order: the first that asks something of the runner wins (the
    /// rest are dropped: the screen they were typed at is gone).
    pub fn handle(&mut self, keys: &[Key]) -> Option<Req> {
        keys.iter().find_map(|&k| self.key(k))
    }

    pub fn key(&mut self, k: Key) -> Option<Req> {
        if let Key::Mouse(m) = k {
            // Only a click is a key here (the end screen's "any key").
            if !(m.is_click() && self.mode == Mode::Done) {
                return None;
            }
        }
        if self.busy {
            return matches!(k, Key::Esc | Key::CtrlC | Key::AltW | Key::Char('q')).then_some(Req::Quit);
        }
        self.flash = None;
        match self.mode.clone() {
            Mode::Done => Some(Req::Quit),
            Mode::Space(info) => match k {
                Key::Char('y' | 'Y') => Some(Req::CloseSpace(info)),
                Key::Char('n' | 'N' | 'q') | Key::Esc | Key::CtrlC => {
                    self.advance();
                    None
                }
                _ => None,
            },
            Mode::Snooze { sel, typing, err } => self.snooze_key(k, sel, typing, err),
            Mode::Agent => self.agent_key(k),
        }
    }

    fn agent_key(&mut self, k: Key) -> Option<Req> {
        let i = self.cur;
        let done = self.outcomes.get(i).copied().flatten().filter(|o| o.removes());
        let final_word = |o: Outcome| format!("already {} · ←/→ to another", o.word());
        match k {
            Key::Esc | Key::CtrlC | Key::AltW | Key::Char('q') => return Some(Req::Quit),
            Key::Char('c' | 'p' | 's' | 'k' | ' ' | 'v') if done.is_some() => {
                self.flash = Some((final_word(done.unwrap_or(Outcome::Closed)), true));
            }
            Key::Char('c') => return Some(Req::Close(i)),
            Key::Char('p') => return Some(Req::Park(i)),
            Key::Char('s') => self.mode = Mode::Snooze { sel: 0, typing: None, err: None },
            Key::Char('k' | ' ') => return Some(Req::Keep(i)),
            Key::Char('v') => return Some(Req::Jump(i)),
            Key::Char('h' | LEFT) => {
                if self.cur == 0 {
                    self.flash = Some(("this is the first one".into(), false));
                } else {
                    self.cur -= 1;
                }
            }
            Key::Char('l' | RIGHT) | Key::Tab => self.advance(),
            _ => {}
        }
        None
    }

    fn snooze_key(&mut self, k: Key, sel: usize, typing: Option<String>, err: Option<String>) -> Option<Req> {
        let i = self.cur;
        let n = self.presets.len() + 1; // + Custom…
        if let Some(mut t) = typing {
            match k {
                Key::Esc | Key::CtrlC => self.mode = Mode::Snooze { sel, typing: None, err: None },
                Key::Enter => {
                    if t.trim().is_empty() {
                        self.mode = Mode::Snooze { sel, typing: Some(t), err: Some("type a time: 45m, 2h, fri 9am…".into()) };
                    } else {
                        let text = t.trim().to_string();
                        self.mode = Mode::Snooze { sel, typing: Some(t), err: None };
                        return Some(Req::Snooze(i, text));
                    }
                }
                Key::Backspace => {
                    t.pop();
                    self.mode = Mode::Snooze { sel, typing: Some(t), err: None };
                }
                Key::CtrlU => self.mode = Mode::Snooze { sel, typing: Some(String::new()), err: None },
                Key::Char(c) if !c.is_control() && c != LEFT && c != RIGHT && dwidth(&t) < 40 => {
                    t.push(c);
                    self.mode = Mode::Snooze { sel, typing: Some(t), err: None };
                }
                _ => self.mode = Mode::Snooze { sel, typing: Some(t), err },
            }
            return None;
        }
        let pick = |s: &mut Review, j: usize| -> Option<Req> {
            match s.presets.get(j) {
                Some(p) => Some(Req::Snooze(i, p.text.clone())),
                None => {
                    s.mode = Mode::Snooze { sel: j, typing: Some(String::new()), err: None };
                    None
                }
            }
        };
        match k {
            Key::Esc | Key::CtrlC | Key::Char('q') => self.mode = Mode::Agent,
            Key::Up | Key::CtrlP => self.mode = Mode::Snooze { sel: (sel + n - 1) % n, typing: None, err: None },
            Key::Down | Key::CtrlN | Key::Tab => self.mode = Mode::Snooze { sel: (sel + 1) % n, typing: None, err: None },
            Key::Enter => return pick(self, sel),
            Key::Char('c') => return pick(self, n - 1),
            Key::Char(c) => {
                if let Some(j) = self.presets.iter().position(|p| p.key == c) {
                    self.mode = Mode::Snooze { sel: j, typing: None, err: None };
                    return pick(self, j);
                }
            }
            _ => {}
        }
        None
    }
}

// ── drawing ────────────────────────────────────────────────────────────────

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

/// Greedy word wrap of plain text into at most `max` lines of `width`
/// cells; the last line ends in `…` when text was left over.
pub fn wrap(text: &str, width: usize, max: usize) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    if width == 0 || max == 0 {
        return out;
    }
    let mut line = String::new();
    let mut words = text.split_whitespace().peekable();
    while let Some(w) = words.next() {
        let cand = if line.is_empty() { w.to_string() } else { format!("{line} {w}") };
        if dwidth(&cand) <= width {
            line = cand;
            continue;
        }
        if !line.is_empty() {
            out.push(std::mem::take(&mut line));
            if out.len() == max {
                let last = out.pop().unwrap_or_default();
                out.push(clip(&format!("{last} …"), width));
                return out;
            }
        }
        line = if dwidth(w) > width { clip(w, width) } else { w.to_string() };
        if words.peek().is_none() {
            break;
        }
    }
    if !line.is_empty() {
        out.push(line);
    }
    out
}

/// The state's glyph, words and colour (`✓ done 9h ago`, `○ idle 2d 3h`).
fn state_bits(state: &str, idle: i64) -> (&'static str, String, &'static str) {
    let cat = Cat::of_state(state).unwrap_or(Cat::Idle);
    let words = match cat {
        Cat::Idle => format!("idle {}", span(idle)),
        _ => format!("{} {} ago", state.replace('-', " "), span(idle)),
    };
    (cat.glyph(), words, if cat == Cat::Idle { "overlay2" } else { cat.hue() })
}

/// Fill a row (or a block) with one background.
fn band(buf: &mut Buffer, r: Rect, bg: &str) {
    buf.set_style(r, Style::new().bg(color(bg)));
}

/// The frame's parts. Roomy (the popup's own size): blank rows around the
/// body and a three-row dock; wide: the queue on the left.
struct Parts {
    side: Option<Rect>,
    /// The card and what is under it.
    main: Rect,
    /// The message's row (always its own, so it never covers the keys).
    msg: u16,
    dock: Rect,
    keys: u16,
}

/// The queue's width: a quarter of the popup, within these.
const SIDE_W: (u16, u16) = (34, 50);
/// The longest line of prose that reads well (the reply; the screen gets it all).
const PROSE_W: u16 = 110;
const SIDE_MIN_W: u16 = 88;
const ROOMY_H: u16 = 20;

fn parts(w: u16, h: u16, queue: bool) -> Parts {
    let roomy = h >= ROOMY_H;
    let dock_h = if roomy { 3 } else { 1 };
    let dock = Rect::new(1, h - 1 - dock_h, w - 2, dock_h);
    let top = if roomy { 3 } else { 2 };
    let msg = dock.y - 1;
    let side = (queue && w >= SIDE_MIN_W).then(|| Rect::new(1, top, (w / 4).clamp(SIDE_W.0, SIDE_W.1), dock.y - top));
    let x = side.map_or(2, |s| s.x + s.width + 1);
    Parts { side, main: Rect::new(x, top, w - 2 - x, msg - top), msg, dock, keys: dock.y + dock_h / 2 }
}

/// Draw the whole frame.
pub fn render(r: &Review, buf: &mut Buffer) {
    let area = buf.area;
    if area.width == 0 || area.height == 0 {
        return;
    }
    for c in buf.content.iter_mut() {
        c.reset();
    }
    buf.set_style(area, on("text", CANVAS));
    let (w, h) = (area.width, area.height);
    if w < MIN_W || h < MIN_H {
        put(buf, 0, 0, w, "review · too small", fg("overlay"));
        return;
    }
    border(buf, w, h);
    header(r, buf, w);
    let p = parts(w, h, !r.cands.is_empty());
    if let Some(s) = p.side {
        queue(r, buf, s);
    }
    let m = p.main;
    if r.mode == Mode::Done {
        done(r, buf, m);
    } else {
        card(r, buf, m);
        let body = Rect::new(m.x, m.y + 3, m.width, m.height - 3);
        match &r.mode {
            Mode::Space(info) => space_prompt(info, buf, body),
            Mode::Snooze { sel, typing, err } => snooze(r, *sel, typing.as_deref(), err.as_deref(), buf, body),
            _ => card_body(r, buf, body),
        }
    }
    message(r, buf, m, p.msg);
    band(buf, p.dock, "tile_lo");
    footer(r, buf, w, p.keys);
}

fn border(buf: &mut Buffer, w: u16, h: u16) {
    let st = fg("surface1");
    for x in 1..w - 1 {
        put(buf, x, 0, w, "─", st);
        put(buf, x, h - 1, w, "─", st);
    }
    for y in 1..h - 1 {
        put(buf, 0, y, w, "│", st);
        put(buf, w - 1, y, w, "│", st);
    }
    for (x, y, s) in [(0, 0, "┌"), (w - 1, 0, "┐"), (0, h - 1, "└"), (w - 1, h - 1, "┘")] {
        put(buf, x, y, w, s, st);
    }
}

/// What this is, then a segment per agent (its outcome's colour once
/// decided, bright for the one on screen) and `3 of 9`.
fn header(r: &Review, buf: &mut Buffer, w: u16) {
    band(buf, Rect::new(1, 1, w - 2, 1), "tile_lo");
    let end = w - 2;
    let mut right = end;
    let n = r.cands.len();
    if n > 0 {
        let prog = if r.mode == Mode::Done { format!("{} of {n} decided", r.outcomes.iter().flatten().count()) } else { format!("{} of {n}", r.cur + 1) };
        right = end - cells(&prog);
        put(buf, right, 1, end, &prog, fg("sub"));
        let seg = 3 * n as u16;
        if right > seg + 46 {
            right -= seg + 1;
            for (i, o) in r.outcomes.iter().enumerate() {
                let hue = match o {
                    Some(o) => o.hue(),
                    None if i == r.cur && r.mode != Mode::Done => "lavender",
                    None => "surface2",
                };
                put(buf, right + 3 * i as u16, 1, end, "━━", fg(hue));
            }
        }
    }
    let x = put(buf, 2, 1, right, "Review old agents", bold(fg("text")));
    let sub = format!("idle {}+", span(r.min_idle));
    if x + 2 + cells(&sub) < right {
        put(buf, x + 2, 1, right, &sub, fg("overlay2"));
    }
}

/// Every agent up for review, under its space: the one on screen lifted,
/// the decided ones dim with their outcome.
fn queue(r: &Review, buf: &mut Buffer, s: Rect) {
    enum Line {
        Gap,
        Space(usize),
        Agent(usize),
    }
    let mut lines = Vec::new();
    for (i, c) in r.cands.iter().enumerate() {
        if i == 0 || r.cands[i - 1].session != c.session {
            if i > 0 {
                lines.push(Line::Gap);
            }
            lines.push(Line::Space(i));
        }
        lines.push(Line::Agent(i));
    }
    let cap_w = r.cands.iter().map(|c| dwidth(&sanitize(&c.key))).max().unwrap_or(1);
    let rows = s.height as usize;
    let at = lines.iter().position(|l| matches!(l, Line::Agent(i) if *i == r.cur)).unwrap_or(0);
    let top = at.saturating_sub(rows / 2).min(lines.len().saturating_sub(rows));
    let end = s.x + s.width;
    for (k, l) in lines.iter().skip(top).take(rows).enumerate() {
        let y = s.y + k as u16;
        match l {
            Line::Gap => {}
            Line::Space(i) => {
                let c = &r.cands[*i];
                let mut x = s.x + 1;
                if let Some(n) = c.space_index {
                    x = put(buf, x, y, end, &format!("{n} "), fg("overlay2"));
                }
                let st = bold(c.space_color.map_or(fg("sub"), |col| Style::new().fg(col)));
                put(buf, x, y, end - 1, &clip(&sanitize(&c.session), (end - 1 - x) as usize), st);
            }
            Line::Agent(i) => {
                let (c, o) = (&r.cands[*i], r.outcomes[*i]);
                let me = *i == r.cur && r.mode != Mode::Done;
                let bg = if me { "tile_me" } else { CANVAS };
                band(buf, Rect::new(s.x, y, s.width, 1), bg);
                let key = sanitize(&c.key);
                let cap = format!(" {key}{} ", " ".repeat(cap_w.saturating_sub(dwidth(&key))));
                let cap_st = match (me, o.is_some()) {
                    (true, _) => bold(on("crust", "lavender")),
                    (_, true) => on("overlay", "tile_lo"),
                    _ => bold(on("text", "surface0")),
                };
                let x = put(buf, s.x + 1, y, end, &cap, if key.is_empty() { on("text", bg) } else { cap_st });
                // The age in its largest unit: the card has the rest.
                let age = span(r.now - c.since);
                let word = o.map_or(age.split(' ').next().unwrap_or("").to_string(), |o| o.word().into());
                let word_x = end - 1 - cells(&word);
                put(buf, word_x, y, end, &word, on(o.map_or(if me { "sub" } else { "overlay" }, Outcome::hue), bg));
                let st = match (me, o.is_some()) {
                    (true, _) => bold(on("text", bg)),
                    (_, true) => on("overlay", bg),
                    _ => on("sub", bg),
                };
                let room = word_x.saturating_sub(x + 2) as usize;
                put(buf, x + 1, y, word_x, &clip(&sanitize(&c.title), room), st);
            }
        }
    }
}

/// The agent on screen, a two-row tile: its letter and title (its outcome on
/// the right once decided); its harness, its space, its state and since when.
fn card(r: &Review, buf: &mut Buffer, m: Rect) {
    let Some(c) = r.cands.get(r.cur) else { return };
    let bg = "tile";
    band(buf, Rect::new(m.x, m.y, m.width, 2), bg);
    let end = m.x + m.width - 1;
    let (g, words, shue) = state_bits(&c.state, r.now - c.since);
    for y in [m.y, m.y + 1] {
        put(buf, m.x, y, end, "▎", on(shue, bg));
    }
    let x0 = m.x + 2;
    let mut x = x0;
    if !c.key.is_empty() {
        x = put(buf, x, m.y, end, &format!(" {} ", sanitize(&c.key)), bold(on("crust", c.harness.hue()))) + 1;
    }
    let mut title_end = end;
    if let Some(o) = r.outcomes.get(r.cur).copied().flatten() {
        let b = format!(" {} ", o.word());
        title_end = end.saturating_sub(cells(&b) + 1);
        put(buf, title_end + 1, m.y, end, &b, bold(on("crust", o.hue())));
    }
    put(buf, x, m.y, title_end, &clip(&sanitize(&c.title), title_end.saturating_sub(x) as usize), bold(on("text", bg)));
    let y = m.y + 1;
    // What fits beside the state, which always shows.
    let tail = cells(g) + 1 + cells(&words);
    let mut x = put(buf, x0, y, end, &mark(c.harness), on(c.harness.hue(), bg));
    x = put(buf, x + 1, y, end, c.harness.name(), on("sub", bg)) + 2;
    let mut room = end.saturating_sub(x + tail + 2) as usize;
    if let Some(i) = c.space_index.filter(|_| room >= 8) {
        x = put(buf, x, y, end, &format!("{i} "), on("overlay2", bg));
        room -= format!("{i} ").len();
    }
    if room >= 4 {
        let st = c.space_color.map_or(on("sub", bg), |col| Style::new().fg(col).bg(color(bg)));
        x = put(buf, x, y, end, &clip(&sanitize(&c.session), room.min(28)), st) + 2;
    }
    put(buf, x, y, end, &format!("{g} {words}"), on(shue, bg));
}

/// A pane's last lines without the agent's own furniture: Claude's composer
/// (its two rules and the status line under them; a typed draft stays), rule
/// lines and runs of blanks.
pub fn tidy(lines: &[String]) -> Vec<String> {
    let rule = |l: &str| {
        let n = l.trim().chars().count();
        n >= 8 && l.trim().chars().filter(|c| "─━═▔▁".contains(*c)).count() * 10 >= n * 7
    };
    let rules: Vec<usize> = (0..lines.len()).filter(|&i| rule(&lines[i])).collect();
    let mut kept: Vec<String> = lines.to_vec();
    if let [.., a, b] = rules[..] {
        if b - a <= 10 && lines.len() - b <= 8 && lines[a + 1..b].iter().any(|l| l.trim_start().starts_with(['❯', '>'])) {
            let draft: Vec<&str> =
                lines[a + 1..b].iter().map(|l| l.trim().trim_start_matches(['❯', '>']).trim()).filter(|l| !l.is_empty()).collect();
            kept.truncate(a);
            if !draft.is_empty() {
                kept.extend([String::new(), format!("❯ {}", draft.join(" "))]);
            }
        }
    }
    let mut out: Vec<String> = Vec::new();
    for l in kept.iter().filter(|l| !rule(l)) {
        let l = l.trim_end();
        if l.is_empty() && out.last().is_none_or(|p| p.is_empty()) {
            continue;
        }
        out.push(l.to_string());
    }
    while out.last().is_some_and(|l| l.is_empty()) {
        out.pop();
    }
    out
}

/// Screen lines at `width`, each with "he typed this": prose wraps (its
/// indent kept, past a bullet or prompt; three rows at most), a table or box
/// is clipped.
fn fit(lines: &[String], width: usize) -> Vec<(String, bool)> {
    let mut out = Vec::new();
    for l in lines {
        let l = sanitize(l);
        let lead = l.chars().take_while(|c| *c == ' ').count();
        let lead = if lead * 2 > width { 0 } else { lead };
        let text = l.trim_start();
        let typed = text.starts_with(['❯', '›']) || text.starts_with("> ");
        let boxed = l.chars().any(|c| ('\u{2500}'..='\u{257f}').contains(&c));
        if boxed || dwidth(&l) <= width {
            out.push((clip(&l, width), typed));
            continue;
        }
        let mut cs = text.chars();
        let (a, b) = (cs.next(), cs.next());
        let bullet = match (a, b) {
            (Some(a), Some(' ')) if !a.is_alphanumeric() => Some(a.to_string()),
            _ => None,
        };
        let hang = lead + bullet.as_ref().map_or(0, |m| dwidth(m) + 1);
        let rest = bullet.as_ref().map_or(text, |m| &text[m.len()..]);
        for (k, row) in wrap(rest, width.saturating_sub(hang), 3).into_iter().enumerate() {
            let pre = match &bullet {
                Some(m) if k == 0 => format!("{}{m} ", " ".repeat(lead)),
                _ => " ".repeat(hang),
            };
            out.push((format!("{pre}{row}"), typed));
        }
    }
    out
}

/// The last reply, wrapped, then the pane's last lines on a terminal strip.
fn card_body(r: &Review, buf: &mut Buffer, b: Rect) {
    let Some(c) = r.cands.get(r.cur) else { return };
    let (end, bottom) = (b.x + b.width, b.y + b.height);
    let label = bold(fg("overlay2"));
    let mut y = b.y;
    let shot = c.preview.as_deref().map(tidy);
    let reply = sanitize(&c.reply);
    if !reply.trim().is_empty() && y + 1 < bottom {
        put(buf, b.x, y, end, "Last reply", label);
        y += 1;
        // Up to half the body when there is a screen to show too.
        let room = b.height as usize;
        let max = if shot.as_ref().is_some_and(|s| s.is_empty()) { room - 1 } else { (room / 2).saturating_sub(1).max(1) };
        for line in wrap(&reply, b.width.min(PROSE_W) as usize, max) {
            if y >= bottom {
                break;
            }
            put(buf, b.x, y, end, &line, fg("text"));
            y += 1;
        }
        y += 1;
    }
    if y + 1 >= bottom {
        return;
    }
    put(buf, b.x, y, end, "On its screen", label);
    y += 1;
    let lines = match &shot {
        None => vec![("…".to_string(), false)],
        Some(s) if s.is_empty() => vec![("nothing on it".to_string(), false)],
        Some(s) => fit(s, (b.width - 2) as usize),
    };
    let left = (bottom - y) as usize;
    let start = lines.len().saturating_sub(left);
    let start = start + lines[start..].iter().take_while(|l| l.0.is_empty()).count();
    let empty = shot.is_none_or(|s| s.is_empty());
    // A panel down to the message row, however few lines there are.
    band(buf, Rect::new(b.x, y, b.width, bottom - y), "term");
    for (line, typed) in &lines[start..] {
        // What he typed stands out from what it answered.
        let st = match typed {
            _ if empty => on("overlay", "term"),
            true => bold(on("text", "term")),
            false => on("sub", "term"),
        };
        put(buf, b.x + 1, y, end, line, st);
        y += 1;
    }
}

fn snooze(r: &Review, sel: usize, typing: Option<&str>, err: Option<&str>, buf: &mut Buffer, b: Rect) {
    let (end, bottom) = (b.x + b.width, b.y + b.height);
    let x = put(buf, b.x, b.y, end, "Snooze until", bold(fg("text")));
    put(buf, x + 2, b.y, end, "parked now, back on its own then", fg("overlay2"));
    // The title's gap goes first where the rows would not fit.
    let mut y = b.y + if b.height > r.presets.len() as u16 + 4 { 2 } else { 1 };
    let custom = ('c', "Custom…");
    let rows: Vec<(char, &str)> = r.presets.iter().map(|p| (p.key, p.label.as_str())).chain(std::iter::once(custom)).collect();
    let rw = b.width.min(44);
    for (j, (k, label)) in rows.iter().enumerate() {
        if y >= bottom {
            break;
        }
        let me = j == sel;
        let bg = if me { "tile_me" } else { ["tile", "tile_lo"][j % 2] };
        band(buf, Rect::new(b.x, y, rw, 1), bg);
        let x = put(buf, b.x, y, end, &format!(" {k} "), if me { bold(on("crust", "peach")) } else { bold(on("peach", "surface0")) });
        put(buf, x + 1, y, end, label, if me { bold(on("text", bg)) } else { on("sub", bg) });
        y += 1;
    }
    if let Some(t) = typing {
        if y + 2 < bottom {
            y += 1;
        }
        if y < bottom {
            let x = put(buf, b.x, y, end, "when? ", fg("sub"));
            band(buf, Rect::new(x, y, (x + 30).min(end) - x, 1), "surface0");
            let bx = put(buf, x, y, end, &format!(" {t}"), on("text", "surface0"));
            put(buf, bx, y, end, "▏", on("peach", "surface0"));
            y += 1;
        }
        if y < bottom {
            let (s, st) = match err {
                Some(e) => (e.to_string(), fg("red")),
                None => ("45m · 2h · tonight · tomorrow 2pm · fri 9am · 14:30".to_string(), fg("overlay2")),
            };
            put(buf, b.x, y, end, &clip(&sanitize(&s), b.width as usize), st);
        }
    }
}

fn space_prompt(info: &SpaceInfo, buf: &mut Buffer, b: Rect) {
    let end = b.x + b.width;
    let shells = format!("{} shell{}", info.shells, if info.shells == 1 { "" } else { "s" });
    let x = put(buf, b.x, b.y, end, "No agents left in ", fg("sub"));
    let x = put(buf, x, b.y, end, &clip(&sanitize(&info.name), 30), bold(fg("text")));
    put(buf, x, b.y, end, &format!(" ({shells})."), fg("sub"));
    put(buf, b.x, b.y + 2, end, "Close the space?", bold(fg("peach")));
    put(buf, b.x, b.y + 3, end, "every tab goes through ⌘W's close · ⌘Z reopens", fg("overlay2"));
}

/// The end: a row per outcome, then what the last action said.
fn done(r: &Review, buf: &mut Buffer, b: Rect) {
    let end = b.x + b.width;
    if r.cands.is_empty() {
        let y = b.y + b.height / 2 - 1;
        put(buf, b.x, y, end, "Nothing to review", bold(fg("text")));
        let why = format!("no agent idle {}+ (pinned, kept and working ones skipped)", span(r.min_idle));
        put(buf, b.x, y + 1, end, &clip(&why, b.width as usize), fg("overlay2"));
        return;
    }
    put(buf, b.x, b.y, end, "Review done", bold(fg("text")));
    let undecided = r.outcomes.iter().filter(|o| o.is_none()).count();
    let mut rows: Vec<(usize, String, &str)> = [Outcome::Closed, Outcome::Parked, Outcome::Snoozed, Outcome::Kept]
        .iter()
        .map(|&o| (r.count(o), o.word().to_string(), o.hue()))
        .collect();
    if undecided > 0 {
        rows.push((undecided, "skipped".into(), "sub"));
    }
    if r.spaces_closed > 0 {
        rows.push((r.spaces_closed, format!("space{} closed", if r.spaces_closed == 1 { "" } else { "s" }), "red"));
    }
    for (y, (n, word, hue)) in (b.y + 1..b.y + b.height).zip(rows) {
        let (nst, wst) = if n > 0 { (bold(fg(hue)), fg("text")) } else { (fg("overlay"), fg("overlay")) };
        let x = put(buf, b.x, y, end, &format!("{n:>3}"), nst);
        put(buf, x + 2, y, end, &word, wst);
    }
}

/// The last message, until the next key, on its own row over the dock: it
/// never covers the keys and nothing moves.
fn message(r: &Review, buf: &mut Buffer, m: Rect, y: u16) {
    if let Some((s, warn)) = &r.flash {
        put(buf, m.x, y, m.x + m.width, &clip(&sanitize(s), m.width as usize), fg(if *warn { "peach" } else { "green" }));
    }
}

/// The key bar, always there: the actions in their outcomes' colours, the
/// rest at the right end. Tighter when the popup is narrow, then the keys
/// without their words.
fn footer(r: &Review, buf: &mut Buffer, w: u16, y: u16) {
    let end = w - 2;
    // Key, word, and an action's colour.
    type Item = (&'static str, &'static str, Option<&'static str>);
    let items: &[Item] = match &r.mode {
        Mode::Agent => &[
            ("c", "close", Some(Outcome::Closed.hue())), ("p", "park", Some(Outcome::Parked.hue())),
            ("s", "snooze", Some(Outcome::Snoozed.hue())), ("k", "keep", Some(Outcome::Kept.hue())),
            ("v", "go", None), ("←→", "move", None), ("q", "quit", None),
        ],
        Mode::Snooze { typing: Some(_), .. } => &[("⏎", "snooze", Some("peach")), ("esc", "back", None)],
        Mode::Snooze { .. } => &[("⏎", "snooze", Some("peach")), ("↑↓", "move", None), ("esc", "back", None)],
        Mode::Space(_) => &[("y", "close the space", Some("red")), ("n", "keep it", Some("teal"))],
        Mode::Done => &[("any key", "close", None)],
    };
    // Words: 2 on every key, 1 on the actions only, 0 on none.
    let worded = |i: &Item, words: u8| words == 2 || (words == 1 && i.2.is_some());
    let width = |v: &[Item], gap: u16, words: u8| -> u16 {
        let one = |i: &Item| cells(i.0) + 2 + if worded(i, words) { 1 + cells(i.1) } else { 0 };
        v.iter().map(|i| one(i) + gap).sum::<u16>().saturating_sub(gap)
    };
    let (gap, words) = [(3, 2), (2, 2), (1, 2), (2, 1), (1, 1)]
        .into_iter()
        .find(|&(g, wd)| width(items, g, wd) <= end - 2)
        .unwrap_or((1, 0));
    let split = items.iter().position(|i| i.2.is_none()).filter(|&k| k > 0).unwrap_or(items.len());
    let mut x = 2;
    for (k, item) in items.iter().enumerate() {
        let (key, word, hue) = item;
        if k == split {
            x = end - width(&items[split..], gap, words);
        } else if k > 0 {
            x += gap;
        }
        x = put(buf, x, y, end, &format!(" {key} "), hue.map_or(on("text", "surface1"), |h| bold(on("crust", h))));
        if worded(item, words) {
            x = put(buf, x + 1, y, end, word, fg(if hue.is_some() { "text" } else { "overlay2" }));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::launch::view::initials;
    use crate::menu::render::dump;

    const NOW: i64 = 1_800_000_000;

    fn cand(id: &str, session: &str, key: &str, title: &str, idle: i64) -> Cand {
        Cand {
            id: id.into(),
            session: session.into(),
            space_index: Some(2),
            key: key.into(),
            title: title.into(),
            harness: Harness::named("claude"),
            state: "done".into(),
            since: NOW - idle,
            reply: "The hardened build is installed and committed, but the speed test still fails on the cold path.".into(),
            preview: Some(vec!["$ cargo test".into(), "ok".into(), "❯".into()]),
            ..Cand::default()
        }
    }

    fn three() -> Review {
        Review::new(vec![cand("@1", "notes", "a", "First", 2 * 86_400 + 3 * 3600), cand("@2", "notes", ";b", "Second", 9 * 3600),
            cand("@3", "work", "c", "Third", 30 * 3600)], NOW, 10, 8 * 3600)
    }

    fn frame(r: &Review, w: u16, h: u16) -> Vec<String> {
        let mut b = Buffer::empty(Rect::new(0, 0, w, h));
        render(r, &mut b);
        initials(&dump(&b, false))
    }

    #[test]
    fn spans() {
        assert_eq!(span(2 * 86_400 + 3 * 3600 + 59), "2d 3h");
        assert_eq!(span(86_400), "1d");
        assert_eq!(span(9 * 3600 + 5 * 60), "9h 5m");
        assert_eq!(span(8 * 3600), "8h");
        assert_eq!((span(42 * 60), span(-5)), ("42m".to_string(), "0m".to_string()));
    }

    #[test]
    fn the_presets_follow_snooze_sh() {
        let p = presets(10);
        let got: Vec<(char, &str, &str)> = p.iter().map(|p| (p.key, p.label.as_str(), p.text.as_str())).collect();
        assert_eq!(got, [('1', "1 hour", "1h"), ('3', "3 hours", "3h"), ('e', "This evening 18:00", "tonight"),
            ('t', "Tomorrow 9:00", "tomorrow"), ('m', "Monday 9:00", "mon")]);
        assert_eq!((presets(19)[2].label.as_str(), presets(19)[2].text.as_str()), ("Tomorrow evening 18:00", "tomorrow 18:00"));
    }

    #[test]
    fn action_keys_ask_the_runner() {
        let mut r = three();
        assert_eq!(r.key(Key::Char('c')), Some(Req::Close(0)));
        assert_eq!(r.key(Key::Char('p')), Some(Req::Park(0)));
        assert_eq!(r.key(Key::Char('k')), Some(Req::Keep(0)));
        assert_eq!(r.key(Key::Char(' ')), Some(Req::Keep(0)));
        assert_eq!(r.key(Key::Char('v')), Some(Req::Jump(0)));
        for k in [Key::Char('q'), Key::Esc, Key::CtrlC, Key::AltW] {
            assert_eq!(r.key(k), Some(Req::Quit), "{k:?}");
        }
        // Done: recorded, then on to the next; ← back; → skips.
        r.record(0, Outcome::Closed, "closed First".into());
        r.advance();
        assert_eq!((r.cur, &r.mode), (1, &Mode::Agent));
        r.key(Key::Char('h'));
        assert_eq!(r.cur, 0);
        // A closed one stays closed: its action keys only say so.
        assert_eq!(r.key(Key::Char('c')), None);
        assert!(r.flash.as_ref().is_some_and(|(m, warn)| m.starts_with("already closed") && *warn));
        // Nothing to go to either: said in the footer, the popup stays.
        assert_eq!(r.key(Key::Char('v')), None);
        assert!(r.flash.as_ref().is_some_and(|(m, _)| m.starts_with("already closed")));
        // While an action is out, only quit gets through.
        r.busy = true;
        assert_eq!((r.key(Key::Char('h')), r.key(Key::Char('c')), r.cur), (None, None, 0));
        assert_eq!(r.key(Key::Char('q')), Some(Req::Quit));
        r.busy = false;
        r.key(Key::Char('h'));
        assert_eq!(r.flash.as_ref().map(|f| f.0.as_str()), Some("this is the first one"));
        r.key(Key::Char('l'));
        r.key(Key::Tab);
        assert_eq!(r.cur, 2);
        // A kept one can still be decided again.
        r.record(2, Outcome::Kept, "kept".into());
        assert_eq!(r.key(Key::Char('c')), Some(Req::Close(2)));
        r.key(Key::Char('l'));
        assert_eq!(r.mode, Mode::Done);
        assert_eq!(r.key(Key::Char('x')), Some(Req::Quit));
        assert_eq!((r.count(Outcome::Closed), r.count(Outcome::Parked), r.count(Outcome::Kept)), (1, 0, 1));
    }

    #[test]
    fn snooze_chooser_presets_and_custom() {
        let mut r = three();
        assert_eq!(r.key(Key::Char('s')), None);
        assert_eq!(r.mode, Mode::Snooze { sel: 0, typing: None, err: None });
        assert_eq!(r.key(Key::Enter), Some(Req::Snooze(0, "1h".into())));
        assert_eq!(r.key(Key::Char('t')), Some(Req::Snooze(0, "tomorrow".into())));
        assert_eq!(r.key(Key::Char('m')), Some(Req::Snooze(0, "mon".into())));
        assert_eq!(r.mode, Mode::Snooze { sel: 4, typing: None, err: None });
        assert_eq!(r.key(Key::Char('1')), Some(Req::Snooze(0, "1h".into())));
        r.key(Key::Up); // wraps to Custom…
        assert_eq!(r.mode, Mode::Snooze { sel: 5, typing: None, err: None });
        r.key(Key::Down);
        r.key(Key::Down);
        assert_eq!(r.key(Key::Enter), Some(Req::Snooze(0, "3h".into())));
        // Custom…: a typed box; empty asks for a time; a failed parse shows under it.
        assert_eq!(r.key(Key::Char('c')), None);
        assert_eq!(r.key(Key::Enter), None);
        assert!(matches!(&r.mode, Mode::Snooze { typing: Some(t), err: Some(_), .. } if t.is_empty()));
        for c in "fri 9amx".chars() {
            r.key(Key::Char(c));
        }
        r.key(Key::Backspace);
        assert_eq!(r.key(Key::Enter), Some(Req::Snooze(0, "fri 9am".into())));
        r.refused("snooze: can't read 'fri 9am'".into());
        assert!(matches!(&r.mode, Mode::Snooze { typing: Some(t), err: Some(e), .. } if t == "fri 9am" && e.contains("can't read")));
        // Esc leaves the box, then the chooser.
        r.key(Key::Esc);
        assert_eq!(r.mode, Mode::Snooze { sel: 5, typing: None, err: None });
        r.key(Key::Esc);
        assert_eq!(r.mode, Mode::Agent);
        // A refusal outside the box: back on the card, the reason in the footer.
        r.key(Key::Char('s'));
        r.refused("Busy is working, can't snooze".into());
        assert_eq!(r.mode, Mode::Agent);
        assert_eq!(r.flash, Some(("Busy is working, can't snooze".into(), true)));
    }

    #[test]
    fn the_space_prompt() {
        let mut r = three();
        let info = SpaceInfo { sid: "$3".into(), name: "notes".into(), shells: 2, wins: vec!["@1".into()] };
        r.record(0, Outcome::Parked, "parked".into());
        r.prompt_space(info.clone());
        assert_eq!(r.key(Key::Char('x')), None);
        assert_eq!(r.key(Key::Char('y')), Some(Req::CloseSpace(info.clone())));
        r.key(Key::Char('n'));
        assert_eq!((r.cur, &r.mode), (1, &Mode::Agent));
        assert_eq!(r.removed_in("notes"), ["@1"]);
        assert!(r.removed_in("work").is_empty());
        r.record(1, Outcome::Kept, "kept".into());
        assert_eq!(r.removed_in("notes"), ["@1"]); // kept is not removed
    }

    #[test]
    fn frames() {
        let mut r = three();
        let has = |f: &[String], t: &str| f.iter().any(|l| l.contains(t));
        let f = frame(&r, 108, 32);
        assert!(f[1].starts_with("│ Review old agents  idle 8h+") && f[1].ends_with("━━ ━━ ━━  1 of 3 │"), "{}", f[1]);
        // The queue by space, caps lined up; the card beside it.
        assert!(f[3].starts_with("│ 2 notes") && f[3].contains("▎  a  First"), "{}", f[3]);
        assert!(f[4].starts_with("│  a   First") && f[4].contains(" 2d  ▎ [C] claude  2 notes  ✓ done 2d 3h ago"), "{}", f[4]);
        assert!(f[5].starts_with("│  ;b  Second"), "{}", f[5]);
        assert!(f[7].starts_with("│ 2 work"), "{}", f[7]);
        assert!(has(&f, "Last reply") && has(&f, "The hardened build") && has(&f, "On its screen") && has(&f, " $ cargo test"), "{f:#?}");
        assert!(f[29].starts_with("│  c  close    p  park    s  snooze    k  keep  ") && f[29].ends_with(" v  go    ←→  move    q  quit │"), "{}", f[29]);
        // Narrow: no queue, the card alone; a two-char key shows whole.
        r.advance();
        let f = frame(&r, 72, 20);
        assert!(f[3].starts_with("│ ▎  ;b  Second"), "{}", f[3]);
        // Decided: a badge, the queue's word; the message over the dock, the keys still there.
        r.record(1, Outcome::Closed, "closed Second · ⌘Z brings it back".into());
        let f = frame(&r, 108, 32);
        assert!(f[3].contains(" closed ") && f[5].contains("Second") && f[5].contains("closed"), "{} / {}", f[3], f[5]);
        assert!(f[27].contains("closed Second · ⌘Z"), "{}", f[27]);
        for (w, h, keys) in [(108, 32, 29), (72, 20, 17), (60, 18, 16), (MIN_W, MIN_H, 10)] {
            let f = frame(&r, w, h);
            let at = if h < ROOMY_H { keys - 1 } else { keys - 2 };
            assert!(f[at].contains("closed Second"), "{w}x{h} {}", f[at]);
            for k in ["c", "p", "s", "k", "v", "←→", "q"] {
                assert!(f[keys].contains(&format!(" {k} ")), "{w}x{h} {k}: {}", f[keys]);
            }
            assert!(w < 60 || f[keys].contains("snooze"), "{}", f[keys]);
            assert!(f.iter().all(|l| l.ends_with(['│', '┐', '┘'])), "{f:#?}");
        }
        r.mode = Mode::Snooze { sel: 1, typing: Some("2h".into()), err: None };
        let f = frame(&r, 72, 20);
        assert!(has(&f, "Snooze until") && has(&f, "│  1  1 hour") && has(&f, "│  c  Custom…") && has(&f, "│ when?  2h▏"), "{f:#?}");
        assert!(has(&frame(&r, 72, 16), "│ when?  2h▏"));
        r.prompt_space(SpaceInfo { sid: "$3".into(), name: "notes".into(), shells: 1, wins: vec![] });
        let f = frame(&r, 72, 20);
        assert!(has(&f, "│ No agents left in notes (1 shell).") && has(&f, "Close the space?"), "{f:#?}");
        assert!(f[17].starts_with("│  y  close the space    n  keep it"), "{}", f[17]);
        r.mode = Mode::Done;
        let f = frame(&r, 108, 32);
        assert!(f[1].ends_with("1 of 3 decided │") && has(&f, "  1  closed") && has(&f, "  0  parked") && has(&f, "  2  skipped"), "{f:#?}");
        let empty = Review::new(vec![], NOW, 10, 8 * 3600);
        let f = frame(&empty, 72, 20);
        assert!(has(&f, "Nothing to review") && has(&f, "idle 8h+"), "{f:#?}");
        assert_eq!(frame(&empty, 20, 5)[0], "review · too small");
        // Every size draws without a panic.
        for m in [Mode::Agent, Mode::Snooze { sel: 5, typing: Some("x".into()), err: Some("no".into()) }, Mode::Done] {
            r.mode = m;
            for (w, h) in [(MIN_W, MIN_H), (40, 60), (200, 12), (87, 19), (88, 20), (222, 73)] {
                frame(&r, w, h);
            }
        }
    }

    #[test]
    fn the_queue_scrolls_to_the_one_on_screen() {
        let cands = (0..60).map(|i| cand(&format!("@{i}"), "notes", "a", &format!("T{i}."), 9 * 3600)).collect();
        let mut r = Review::new(cands, NOW, 10, 8 * 3600);
        r.cur = 59;
        let f = frame(&r, 108, 32);
        assert!(f.iter().any(|l| l.starts_with("│  a  T59.")), "{f:#?}");
        assert!(f[1].ends_with(" 60 of 60 │") && !f[1].contains('━'), "{}", f[1]);
    }

    #[test]
    fn tidied_screens() {
        let v = |s: &[&str]| s.iter().map(|l| l.to_string()).collect::<Vec<_>>();
        let r = "─".repeat(40);
        // Claude's composer and status line go, the text stays.
        assert_eq!(tidy(&v(&["", "hi", "", "", "there", "", &r, "❯ ", &r, "  ~/x ❯ main", "  -- INSERT --"])), ["hi", "", "there"]);
        // A typed draft is kept.
        assert_eq!(tidy(&v(&["hi", &r, "❯ half a", "  thought", &r, "st"])), ["hi", "", "❯ half a thought"]);
        // Two rules around anything else (a table): only the rules go.
        assert_eq!(tidy(&v(&["a", &r, "b", &r, "c"])), ["a", "b", "c"]);
        assert!(tidy(&[]).is_empty() && tidy(&v(&["", &r])).is_empty());
        // Wrapped past its bullet; a prompt is "typed" on every row; a box is clipped.
        assert_eq!(fit(&v(&["❯ aa bb cc"]), 7), [("❯ aa bb".to_string(), true), ("  cc".to_string(), true)]);
        assert_eq!(fit(&v(&["│ aa bb cc"]), 7), [("│ aa b…".to_string(), false)]);
    }

    #[test]
    fn wrapping() {
        assert_eq!(wrap("a bb ccc dddd", 6, 5), ["a bb", "ccc", "dddd"]);
        assert_eq!(wrap("a bb ccc dddd", 6, 2), ["a bb", "ccc …"]);
        assert_eq!(wrap("abcdefghij", 4, 2), ["abc…"]);
        assert!(wrap("", 4, 2).is_empty() && wrap("x", 0, 2).is_empty());
    }

    /// The text renders (run with `--nocapture`).
    #[test]
    fn render_the_review() {
        let r = three();
        for line in frame(&r, 108, 32) {
            println!("{line}");
        }
    }
}
