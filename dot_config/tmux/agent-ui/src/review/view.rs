//! The review popup's state ([`Review`]: the candidates, what was done to
//! each, the screen it is on), its keys ([`Review::key`] → [`Req`]) and one
//! frame of it into a ratatui [`Buffer`], in the launcher's style (the
//! menu's palette, a rounded border). No I/O: the runner in `review.rs`
//! carries out each [`Req`] and reports back ([`Review::record`],
//! [`Review::refused`], [`Review::prompt_space`], [`Review::advance`]).
//!
//! ```text
//! ╭─ review old agents ───────────────── 3/9 ─╮
//! │  x  Fix the sidebar tests                   │   letter cap, title (an outcome badge once decided)
//! │  2  cua-notch  [C] claude  ✓ done  idle 2d 3h│   space, harness, state, how long idle
//! ├─────────────────────────────────────────────┤
//! │ last reply                                  │   @agent_detail, wrapped
//! │ The hardened build is installed and …       │
//! │ screen                                      │   the pane's last lines, dim
//! │ ❯                                            │
//! ├ closed … · ⌘Z brings it back ───────────────┤   the last message, on the rule
//! │ c close  p park  s snooze  k keep  v go  …  │   key bar, always there
//! ╰─────────────────────────────────────────────╯
//! ```

use crate::launch::view::mark;
use crate::menu::keys::Key;
use crate::model::{Cat, Harness};
use crate::palette::color;
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
    /// One line on the rule over the key bar until the next key: (text, a warning).
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

    /// `closed 3 · parked 2 · snoozed 1 · kept 2` (every count, zeros too;
    /// then the spaces closed, when any).
    pub fn summary(&self) -> String {
        let mut s = [Outcome::Closed, Outcome::Parked, Outcome::Snoozed, Outcome::Kept]
            .iter()
            .map(|&o| format!("{} {}", o.word(), self.count(o)))
            .collect::<Vec<_>>()
            .join(" · ");
        if self.spaces_closed > 0 {
            s.push_str(&format!(" · {} space{} closed", self.spaces_closed, if self.spaces_closed == 1 { "" } else { "s" }));
        }
        s
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

/// The state's glyph, word and colour (`✓ done`, `○ idle`, `✕ failed`, `◉ needs input`).
fn state_bits(state: &str) -> (&'static str, String, &'static str) {
    let cat = Cat::of_state(state).unwrap_or(Cat::Idle);
    (cat.glyph(), state.replace('-', " "), cat.hue())
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
    buf.set_style(area, on("text", "base"));
    let (w, h) = (area.width, area.height);
    if w < MIN_W || h < MIN_H {
        put(buf, 0, 0, w, "review · too small", fg("overlay"));
        return;
    }
    let s1 = fg("surface1");
    let mut x = put(buf, 0, 0, w, "╭─ ", s1);
    x = put(buf, x, 0, w, "review old agents", bold(fg("text")));
    x = put(buf, x, 0, w, " ", s1);
    while x < w - 1 {
        x = put(buf, x, 0, w - 1, "─", s1);
    }
    put(buf, w - 1, 0, w, "╮", s1);
    if !r.cands.is_empty() && r.mode != Mode::Done {
        let prog = format!(" {}/{} ", r.cur + 1, r.cands.len());
        put(buf, w - 2 - cells(&prog), 0, w - 1, &prog, fg("overlay2"));
    }
    for y in 1..h - 1 {
        put(buf, 0, y, w, "│", s1);
        put(buf, w - 1, y, w, "│", s1);
    }
    put(buf, 0, h - 1, w, "╰", s1);
    for x in 1..w - 1 {
        put(buf, x, h - 1, w, "─", s1);
    }
    put(buf, w - 1, h - 1, w, "╯", s1);
    let body = Rect::new(2, 1, w - 4, h - 4);
    match &r.mode {
        Mode::Done => done(r, buf, body),
        Mode::Space(info) => {
            card_head(r, buf, w);
            rule(buf, 3, w);
            space_prompt(info, buf, Rect::new(2, 4, w - 4, h - 7));
        }
        Mode::Snooze { sel, typing, err } => {
            card_head(r, buf, w);
            rule(buf, 3, w);
            snooze(r, *sel, typing.as_deref(), err.as_deref(), buf, Rect::new(2, 4, w - 4, h - 7));
        }
        Mode::Agent => {
            card_head(r, buf, w);
            rule(buf, 3, w);
            card_body(r, buf, Rect::new(2, 4, w - 4, h - 7));
        }
    }
    rule(buf, h - 3, w);
    message(r, buf, w, h - 3);
    footer(r, buf, w, h - 2);
}

fn rule(buf: &mut Buffer, y: u16, w: u16) {
    let s1 = fg("surface1");
    put(buf, 0, y, w, "├", s1);
    for x in 1..w - 1 {
        put(buf, x, y, w, "─", s1);
    }
    put(buf, w - 1, y, w, "┤", s1);
}

/// Lines 1-2: the letter cap and the title (an outcome badge on the
/// right once decided); the space, the harness, the state, how long idle.
fn card_head(r: &Review, buf: &mut Buffer, w: u16) {
    let Some(c) = r.cands.get(r.cur) else { return };
    let end = w - 2;
    let hue = c.harness.hue();
    let mut x = 2;
    if !c.key.is_empty() {
        x = put(buf, x, 1, end, &format!(" {} ", sanitize(&c.key)), bold(on("crust", hue)));
        x += 1;
    }
    let badge = r.outcomes.get(r.cur).copied().flatten().map(|o| format!(" {} ", o.word()));
    let title_end = badge.as_ref().map_or(end, |b| end.saturating_sub(cells(b) + 1));
    put(buf, x, 1, title_end, &clip(&sanitize(&c.title), title_end.saturating_sub(x) as usize), bold(fg("text")));
    if let Some(b) = badge {
        let bg = if b.trim() == "kept" { "surface1" } else { "green" };
        put(buf, end - cells(&b), 1, end, &b, bold(on(if bg == "green" { "crust" } else { "text" }, bg)));
    }
    // Line 2.
    let y = 2;
    let mut x = 2;
    if let Some(i) = c.space_index {
        x = put(buf, x, y, end, &format!(" {i} "), bold(on("text", "surface0")));
        x += 1;
    }
    let sst = bold(c.space_color.map_or(fg("text"), |col| Style::new().fg(col)));
    x = put(buf, x, y, end, &clip(&sanitize(&c.session), 24), sst);
    x = put(buf, x + 2, y, end, &mark(c.harness), fg(hue));
    x = put(buf, x + 1, y, end, c.harness.name(), fg("sub"));
    let (g, word, shue) = state_bits(&c.state);
    x = put(buf, x + 2, y, end, &format!("{g} {word}"), fg(shue));
    put(buf, x + 2, y, end, &format!("idle {}", span(r.now - c.since)), fg("overlay2"));
}

/// The last reply, wrapped, then the pane's last lines (dim), as many as fit.
fn card_body(r: &Review, buf: &mut Buffer, b: Rect) {
    let Some(c) = r.cands.get(r.cur) else { return };
    let end = b.x + b.width;
    let wid = b.width as usize;
    let mut y = b.y;
    let bottom = b.y + b.height;
    let reply = sanitize(&c.reply);
    let shot: Vec<String> = c.preview.clone().unwrap_or_default();
    // The reply gets up to half the body when there is a screen to show too.
    let room = b.height as usize;
    if !reply.trim().is_empty() && y < bottom {
        put(buf, b.x, y, end, "last reply", fg("overlay"));
        y += 1;
        let max = if shot.is_empty() { room.saturating_sub(1) } else { (room / 2).max(2).saturating_sub(1) };
        for line in wrap(&reply, wid, max) {
            if y >= bottom {
                break;
            }
            put(buf, b.x, y, end, &line, fg("text"));
            y += 1;
        }
    }
    if y + 1 < bottom {
        if y > b.y {
            y += 1; // a gap after the reply
        }
        if y + 1 < bottom {
            put(buf, b.x, y, end, "screen", fg("overlay"));
            y += 1;
            let left = (bottom - y) as usize;
            if shot.is_empty() {
                let what = if c.preview.is_none() { "…" } else { "(nothing on it)" };
                put(buf, b.x, y, end, what, fg("overlay"));
            }
            let k = shot.len().saturating_sub(left);
            for line in &shot[k..] {
                put(buf, b.x, y, end, &clip(&sanitize(line), wid), fg("overlay2"));
                y += 1;
            }
        }
    }
}

fn snooze(r: &Review, sel: usize, typing: Option<&str>, err: Option<&str>, buf: &mut Buffer, b: Rect) {
    let end = b.x + b.width;
    let bottom = b.y + b.height;
    put(buf, b.x, b.y, end, "snooze until…", bold(fg("text")));
    put(buf, b.x + 14, b.y, end, " parked now, back on its own then", fg("overlay"));
    let mut y = b.y + 2;
    let custom = (&'c', "Custom…");
    let rows: Vec<(&char, &str)> = r.presets.iter().map(|p| (&p.key, p.label.as_str())).chain(std::iter::once(custom)).collect();
    for (j, (k, label)) in rows.iter().enumerate() {
        if y >= bottom {
            break;
        }
        let me = j == sel;
        let bg = if me { "tile_me" } else { "base" };
        buf.set_style(Rect::new(b.x, y, b.width, 1), Style::new().bg(color(bg)));
        let x = put(buf, b.x + 1, y, end, &format!(" {k} "), bold(on("crust", "peach")));
        put(buf, x + 1, y, end, label, if me { bold(on("text", bg)) } else { on("text", bg) });
        y += 1;
    }
    if let Some(t) = typing {
        y += 1;
        if y < bottom {
            let x = put(buf, b.x, y, end, "when? ", fg("sub"));
            let bx = put(buf, x, y, end, &format!(" {t}"), on("text", "surface0"));
            put(buf, bx, y, end, "▏", on("peach", "surface0"));
            let fill_end = (x + 30).min(end);
            if bx + 1 < fill_end {
                buf.set_style(Rect::new(bx + 1, y, fill_end - bx - 1, 1), Style::new().bg(color("surface0")));
            }
            y += 1;
        }
        if y < bottom {
            let (s, st) = match err {
                Some(e) => (e.to_string(), fg("red")),
                None => ("45m · 2h · tonight · tomorrow 2pm · fri 9am · 14:30".to_string(), fg("overlay")),
            };
            put(buf, b.x, y, end, &clip(&sanitize(&s), b.width as usize), st);
        }
    }
}

fn space_prompt(info: &SpaceInfo, buf: &mut Buffer, b: Rect) {
    let end = b.x + b.width;
    let shells = format!("{} shell{}", info.shells, if info.shells == 1 { "" } else { "s" });
    let mut x = put(buf, b.x, b.y + 1, end, "No agents left in ", fg("text"));
    x = put(buf, x, b.y + 1, end, &clip(&sanitize(&info.name), 30), bold(fg("text")));
    put(buf, x, b.y + 1, end, &format!(" ({shells})."), fg("text"));
    put(buf, b.x, b.y + 3, end, "Close the space?", bold(fg("peach")));
    put(buf, b.x, b.y + 4, end, "every tab goes through ⌘W's close · ⌘Z reopens", fg("overlay"));
}

fn done(r: &Review, buf: &mut Buffer, b: Rect) {
    let end = b.x + b.width;
    let y = b.y + b.height / 2 - 1;
    if r.cands.is_empty() {
        put(buf, b.x, y, end, "Nothing to review", bold(fg("text")));
        let why = format!("no agent idle {}+ (pinned, kept and working ones skipped)", span(r.min_idle));
        put(buf, b.x, y + 1, end, &clip(&why, b.width as usize), fg("overlay"));
        return;
    }
    put(buf, b.x, y, end, "Review done", bold(fg("text")));
    put(buf, b.x, y + 1, end, &clip(&r.summary(), b.width as usize), fg("sub"));
    if let Some((m, warn)) = &r.flash {
        put(buf, b.x, y + 3, end, &clip(&sanitize(m), b.width as usize), fg(if *warn { "peach" } else { "green" }));
    }
}

/// The last message, until the next key: on the rule over the key bar, so it
/// never covers the keys and nothing moves. (The end screen's body shows it.)
fn message(r: &Review, buf: &mut Buffer, w: u16, y: u16) {
    let Some((m, warn)) = r.flash.as_ref().filter(|_| r.mode != Mode::Done) else { return };
    let end = w - 2;
    let text = clip(&sanitize(m), (end - 3) as usize);
    put(buf, 1, y, end, &format!(" {text} "), fg(if *warn { "peach" } else { "green" }));
}

/// The key bar, always there: tighter when the popup is narrow, then the
/// keys without their words.
fn footer(r: &Review, buf: &mut Buffer, w: u16, y: u16) {
    let end = w - 2;
    let items: &[(&str, &str)] = match &r.mode {
        Mode::Agent => &[("c", "close"), ("p", "park"), ("s", "snooze"), ("k", "keep"), ("v", "go"), ("←→", "move"),
            ("q", "quit")],
        Mode::Snooze { typing: Some(_), .. } => &[("⏎", "snooze"), ("esc", "back")],
        Mode::Snooze { .. } => &[("⏎", "snooze"), ("↑↓", "move"), ("esc", "back")],
        Mode::Space(_) => &[("y", "close the space"), ("n", "keep it")],
        Mode::Done => &[("any key", "close")],
    };
    let ov = fg("overlay");
    let cap = on("text", "surface0");
    let width = |gap: u16, words: bool| -> u16 {
        items.iter().map(|(k, wd)| cells(k) + 2 + gap + if words { 1 + cells(wd) } else { 0 }).sum::<u16>() - gap
    };
    let (gap, words) = [(2, true), (1, true)].into_iter().find(|&(g, wd)| width(g, wd) <= end - 2).unwrap_or((1, false));
    let mut x = 2;
    for (k, (key, word)) in items.iter().enumerate() {
        if k > 0 {
            x += gap;
        }
        x = put(buf, x, y, end, &format!(" {key} "), cap);
        if words {
            x = put(buf, x + 1, y, end, word, ov);
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
        assert_eq!(r.summary(), "closed 1 · parked 0 · snoozed 0 · kept 1");
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
        let f = frame(&r, 72, 20);
        assert!(f[0].starts_with("╭─ review old agents ─") && f[0].ends_with(" 1/3 ─╮"), "{}", f[0]);
        assert!(f[1].starts_with("│  a  First"), "{}", f[1]);
        assert!(f[2].starts_with("│  2  notes  [C] claude  ✓ done  idle 2d 3h"), "{}", f[2]);
        assert!(f[4].starts_with("│ last reply"), "{}", f[4]);
        assert!(f[5].starts_with("│ The hardened build"), "{}", f[5]);
        assert!(f.iter().any(|l| l.starts_with("│ screen")), "{f:#?}");
        assert!(f.iter().any(|l| l.starts_with("│ ❯")), "{f:#?}");
        assert!(f[18].starts_with("│  c  close  p  park  s  snooze  k  keep  v  go  ←→  move  q  quit "), "{}", f[18]);
        let wide = &frame(&r, 100, 20)[18];
        assert!(wide.starts_with("│  c  close   p  park   s  snooze   k  keep   v  go"), "{wide}");
        // A two-char key shows whole.
        r.advance();
        assert!(frame(&r, 72, 20)[1].starts_with("│  ;b  Second"));
        // Decided: a badge; the snooze chooser; the space prompt; the summary.
        r.record(1, Outcome::Closed, "closed Second · ⌘Z brings it back".into());
        let f = frame(&r, 72, 20);
        assert!(f[1].ends_with(" closed  │"), "{}", f[1]);
        // The message sits on the rule; every key stays, at any width.
        assert!(f[17].starts_with("├ closed Second") && f[17].ends_with("─┤"), "{}", f[17]);
        assert!(f[18].starts_with("│  c  close") && f[18].contains(" q  quit"), "{}", f[18]);
        let f = frame(&r, MIN_W, MIN_H);
        assert!(f[9].starts_with("├ closed Second") && f[9].ends_with('┤'), "{}", f[9]);
        assert!(f[10].starts_with("│  c   p   s   k   v   ←→   q "), "{}", f[10]);
        r.mode = Mode::Snooze { sel: 1, typing: Some("2h".into()), err: None };
        let f = frame(&r, 72, 20);
        assert!(f[4].starts_with("│ snooze until…"), "{}", f[4]);
        assert!(f[6].starts_with("│   1  1 hour") && f[11].starts_with("│   c  Custom…"), "{f:#?}");
        assert!(f[13].starts_with("│ when?  2h▏"), "{}", f[13]);
        r.prompt_space(SpaceInfo { sid: "$3".into(), name: "notes".into(), shells: 1, wins: vec![] });
        r.flash = None;
        let f = frame(&r, 72, 20);
        assert!(f[5].starts_with("│ No agents left in notes (1 shell)."), "{f:#?}");
        assert!(f[18].starts_with("│  y  close the space   n  keep it"), "{}", f[18]);
        r.mode = Mode::Done;
        r.flash = None;
        let f = frame(&r, 72, 20);
        assert!(f.iter().any(|l| l.contains("closed 1 · parked 0 · snoozed 0 · kept 0")), "{f:#?}");
        assert!(!f[0].contains("/3"));
        let empty = Review::new(vec![], NOW, 10, 8 * 3600);
        let f = frame(&empty, 72, 20);
        assert!(f.iter().any(|l| l.contains("Nothing to review")) && f.iter().any(|l| l.contains("idle 8h+")), "{f:#?}");
        assert_eq!(frame(&empty, 20, 5)[0], "review · too small");
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
        for line in frame(&r, 72, 20) {
            println!("{line}");
        }
    }
}
