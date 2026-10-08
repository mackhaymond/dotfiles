//! The menu's state machine: agent-roster.py `Roster` (`rebuild`, `move`,
//! `handle`, `act`, `digit`, `go`, the y/n confirms, the peek), driven by
//! [`Key`]s and clicks, with every side effect behind [`Effects`] so the
//! tests can record instead of act.
//!
//! What the Python did and this keeps:
//! - Numbers are resolved against the labels of the frame ON SCREEN when the
//!   first digit was pressed ([`Menu::drawn`], set by the renderer), never a
//!   list rebuilt since; a complete label acts at once, a prefix waits with
//!   no timeout, and a number that matches nothing swallows every further
//!   digit until another key (`Roster.digit`).
//! - A key that opens a y/n or the peek drops the rest of its read, so a
//!   paste or a fast "xy" never confirms itself (`Roster.handle`).
//! - Option-W closes the menu in every mode (`Roster.act`).
//! - A confirm acts on the window as it is NOW: gone, or moved to another
//!   session meanwhile, refuses with the reason (the core's re-check).
//! - Any key closes the peek, and only closes it.
//!
//! What the new layout changes is listed on [`Menu::act`].

use super::keys::{Key, Mouse};
use super::rows::{build_rows, number_rows, Data, Row, RowKey, Tab, Target};
use crate::actions::TAB_MOVED;
use crate::ansi::StyledLine;
use crate::hotkeys::{match_digits, DigitMatch};
use ratatui::layout::Rect;
use std::collections::HashMap;

/// Everything the menu can do to the outside world (the Python's `jump`,
/// `run_bg` and `agent_pane` calls). `Err` carries the footer text.
pub trait Effects {
    /// Go to `win` as listed under `session` (a parked row unstashes).
    fn go(&mut self, win: &str, session: &str) -> Result<(), String>;
    /// agent-jump.sh next / back.
    fn next(&mut self) -> Result<(), String>;
    fn back(&mut self) -> Result<(), String>;
    /// After the y: park / close (or discard a parked tab). Ok = footer text.
    fn park(&mut self, win: &str, session: &str) -> Result<String, String>;
    fn close(&mut self, win: &str, session: &str) -> Result<String, String>;
    fn restart_watcher(&mut self) -> String;
}

/// Effects that refuse everything: `--once` and tests that must not act.
pub struct NoEffects;

impl Effects for NoEffects {
    fn go(&mut self, _: &str, _: &str) -> Result<(), String> {
        Err("read-only".into())
    }
    fn next(&mut self) -> Result<(), String> {
        Err("read-only".into())
    }
    fn back(&mut self) -> Result<(), String> {
        Err("read-only".into())
    }
    fn park(&mut self, _: &str, _: &str) -> Result<String, String> {
        Err("read-only".into())
    }
    fn close(&mut self, _: &str, _: &str) -> Result<String, String> {
        Err("read-only".into())
    }
    fn restart_watcher(&mut self) -> String {
        "read-only".into()
    }
}

/// Which y/n is open.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Park,
    Close,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Confirm {
    pub action: Action,
    pub target: Target,
}

impl Confirm {
    /// The footer's verb: park, close, or discard (closing a parked tab).
    pub fn verb(&self) -> &'static str {
        match self.action {
            Action::Park => "park",
            Action::Close if self.target.parked() => "discard",
            Action::Close => "close",
        }
    }
}

/// The preview's buttons.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Button {
    Go,
    Peek,
    Park,
    Close,
}

/// What a click at a cell does (the click map of the frame on screen).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Hit {
    /// The ✕ in the top border.
    Close,
    Tab(Tab),
    Search,
    Row(Target),
    /// "+k more · ⇥ Parked".
    More,
    /// A preview button, with the row it was drawn for.
    Button(Button, Target),
}

/// One capture of a pane: None when tmux could not capture it.
#[derive(Debug, Clone, PartialEq)]
pub struct PeekData {
    pub lines: Option<Vec<StyledLine>>,
    /// How many lines were asked for.
    pub n: usize,
}

/// A capture the UI would like: `n` lines of `target`'s pane.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PeekWant {
    pub target: Target,
    pub n: usize,
}

/// How long a footer message stays (agent-roster.py `say`).
pub const MSG_SECS: f64 = 3.0;
/// Rows the wheel scrolls per notch.
pub const WHEEL_ROWS: usize = 3;

pub struct Menu {
    pub data: Data,
    pub tab: Tab,
    pub query: String,
    /// The search box has the keys (the Python's `filtering`).
    pub searching: bool,
    /// `a`: plain windows too (the Python's `show_all`).
    pub show_all: bool,
    pub rows: Vec<Row>,
    /// Row position → hotkey label, over the whole list.
    pub labels: HashMap<usize, String>,
    pub sel: Option<RowKey>,
    /// First list line on screen, and whether the next draw must scroll the
    /// selection into view (false after a wheel scroll).
    pub top: usize,
    pub follow: bool,
    /// Rows a PgUp/PgDn moves (the list's height, set by the renderer).
    pub page: usize,
    pub confirm: Option<Confirm>,
    /// A frame showing the open y/n has reached the terminal: until then a
    /// `y` (typed ahead, or queued during a refresh) answers nothing.
    pub prompt_shown: bool,
    /// `p`: the full-body peek of this row.
    pub peek_full: Option<Target>,
    pub msg: String,
    pub msg_until: f64,
    /// A 0-prefixed number being typed, and the frame it is resolved against.
    pub digits: String,
    digit_frame: (Vec<Target>, HashMap<usize, String>),
    /// A number matched nothing: swallow digits until another key.
    pub digits_dead: bool,
    /// The rows on screen in the last frame with their labels (set by the
    /// renderer): numbers resolve against this.
    pub drawn: (Vec<Target>, HashMap<usize, String>),
    /// The last frame's click map.
    pub clicks: Vec<(Rect, Hit)>,
    /// Captured panes by (window, session).
    pub peeks: HashMap<(String, String), PeekData>,
    /// Lines the preview / the full peek can show (set by the renderer; 0:
    /// no preview at this width).
    pub preview_rows: usize,
    pub full_rows: usize,
}

impl Menu {
    pub fn new(data: Data, tab: Tab) -> Menu {
        let mut m = Menu {
            data,
            tab,
            query: String::new(),
            searching: false,
            show_all: false,
            rows: Vec::new(),
            labels: HashMap::new(),
            sel: None,
            top: 0,
            follow: true,
            page: 10,
            confirm: None,
            prompt_shown: false,
            peek_full: None,
            msg: String::new(),
            msg_until: 0.0,
            digits: String::new(),
            digit_frame: (Vec::new(), HashMap::new()),
            digits_dead: false,
            drawn: (Vec::new(), HashMap::new()),
            clicks: Vec::new(),
            peeks: HashMap::new(),
            preview_rows: 0,
            full_rows: 0,
        };
        m.rebuild();
        m
    }

    /// A new refresh's data (agent-roster.py `load`).
    pub fn set_data(&mut self, data: Data) {
        if data != self.data {
            self.data = data;
            self.rebuild();
        }
    }

    /// agent-roster.py `rebuild`: the rows for the tab and query, and a
    /// selection that survives it. On open the first NEEDS YOU row is
    /// selected, else the window the client is on, else the first row. When
    /// the selected row goes away (filter typed, need discharged, tab
    /// parked, tab switched) the Python's fallback runs: another row of the
    /// same window (its group row first), else the client's window, else the
    /// first row.
    pub fn rebuild(&mut self) {
        self.rows = build_rows(&self.data, self.tab, &self.query, self.show_all);
        self.labels = number_rows(&self.rows);
        let keys: Vec<RowKey> = self.rows.iter().filter_map(Row::key).collect();
        // An empty list (a blank snapshot, a search typo) keeps the selection
        // for when the rows come back.
        if keys.is_empty() || self.sel.as_ref().is_some_and(|k| keys.contains(k)) {
            return;
        }
        let row_for = |wid: &str| -> Option<RowKey> {
            let hits: Vec<&RowKey> = keys.iter().filter(|k| k.window() == wid).collect();
            hits.iter().find(|k| matches!(k, RowKey::Agent(..))).or(hits.first()).map(|k| (*k).clone())
        };
        let cur = self.data.view.cur_win.clone();
        let cur_row = || cur.as_deref().and_then(row_for);
        self.sel = match self.sel.take() {
            None => keys.iter().find(|k| matches!(k, RowKey::Need(_))).cloned().or_else(cur_row),
            Some(old) => row_for(old.window()).or_else(cur_row),
        }
        .or_else(|| keys.first().cloned());
        self.follow = true;
    }

    /// The selected row.
    pub fn selected(&self) -> Option<&Row> {
        let k = self.sel.as_ref()?;
        self.rows.iter().find(|r| r.key().as_ref() == Some(k))
    }

    pub fn selected_target(&self) -> Option<Target> {
        self.selected().and_then(Row::target)
    }

    /// The row is in the frame on screen (its number is drawn).
    pub fn on_screen(&self, key: &RowKey) -> bool {
        self.drawn.0.iter().any(|t| t.key == *key)
    }

    /// The frame just written to the terminal is the one `render` last drew:
    /// an open y/n is now on screen.
    pub fn frame_shown(&mut self) {
        self.prompt_shown = self.confirm.is_some();
    }

    fn ask(&mut self, action: Action, target: Target) {
        self.confirm = Some(Confirm { action, target });
        self.prompt_shown = false;
    }

    /// ⏎ / p / s / x on a row (the selection, or the row a button was drawn
    /// for): go, peek, or open the park / close y/n.
    fn act_on(&mut self, b: Button, t: Target, fx: &mut dyn Effects) -> bool {
        match b {
            Button::Go => return self.go(&t, fx),
            Button::Peek => self.peek_full = Some(t),
            Button::Park if t.parked() => self.say("already parked"),
            Button::Park => self.ask(Action::Park, t),
            Button::Close => self.ask(Action::Close, t),
        }
        false
    }

    /// agent-roster.py `move`: by `d` selectable rows, clamped.
    pub fn move_by(&mut self, d: i64) {
        let keys: Vec<RowKey> = self.rows.iter().filter_map(Row::key).collect();
        if keys.is_empty() {
            return;
        }
        let i = self.sel.as_ref().and_then(|k| keys.iter().position(|x| x == k)).unwrap_or(0) as i64;
        self.sel = Some(keys[(i + d).clamp(0, keys.len() as i64 - 1) as usize].clone());
        self.follow = true;
    }

    pub fn set_tab(&mut self, tab: Tab) {
        if tab != self.tab {
            self.tab = tab;
            self.top = 0;
            self.rebuild();
        }
    }

    pub fn say(&mut self, text: impl Into<String>) {
        self.msg = text.into();
        self.msg_until = crate::text::now() + MSG_SECS;
    }

    /// agent-roster.py `go`: Ok closes the menu; a refusal stays, with why.
    fn go(&mut self, t: &Target, fx: &mut dyn Effects) -> bool {
        match fx.go(&t.win, &t.session) {
            Ok(()) => true,
            Err(e) => {
                self.say(e);
                false
            }
        }
    }

    /// next / back: close once it ran, else say why.
    fn jump(&mut self, r: Result<(), String>) -> bool {
        match r {
            Ok(()) => true,
            Err(e) => {
                self.say(e);
                false
            }
        }
    }

    /// agent-roster.py `handle`: one read's keys → true to close the menu.
    pub fn handle(&mut self, keys: &[Key], fx: &mut dyn Effects) -> bool {
        for &key in keys {
            let asking = self.confirm.is_none() && self.peek_full.is_none();
            if self.act(key, fx) {
                return true;
            }
            if asking && (self.confirm.is_some() || self.peek_full.is_some()) {
                return false;
            }
        }
        false
    }

    /// agent-roster.py `digit`.
    fn digit(&mut self, c: char, fx: &mut dyn Effects) -> bool {
        if self.digits_dead {
            return false;
        }
        if self.digits.is_empty() {
            self.digit_frame = self.drawn.clone();
        }
        self.digits.push(c);
        match match_digits(&self.digit_frame.1, &self.digits) {
            DigitMatch::Hit(i) => {
                self.digits.clear();
                let t = self.digit_frame.0[i].clone();
                self.go(&t, fx)
            }
            DigitMatch::Partial => false,
            DigitMatch::Dead => {
                self.say(format!("no row {} on screen · digits ignored until another key", self.digits));
                self.digits.clear();
                self.digits_dead = true;
                false
            }
        }
    }

    /// The y of a confirm: act on the window as it is now.
    fn run_confirm(&mut self, c: Confirm, fx: &mut dyn Effects) {
        let t = &c.target;
        let r = match c.action {
            Action::Park => fx.park(&t.win, &t.session),
            Action::Close => fx.close(&t.win, &t.session),
        };
        match r {
            Ok(m) => self.say(m),
            Err(e) if e == TAB_MOVED => {
                let k = if c.action == Action::Park { "s" } else { "x" };
                self.say(format!("{TAB_MOVED} · press {k} again"));
            }
            Err(e) => self.say(e),
        }
    }

    /// Keys while the search box has them: type, edit, move, switch tabs.
    /// Esc clears the search and leaves the box; ⏎ keeps it (the Python's
    /// filter: digits are hotkeys again).
    fn search_key(&mut self, key: Key, fx: &mut dyn Effects) -> bool {
        match key {
            Key::Esc | Key::CtrlC => {
                self.searching = false;
                self.query.clear();
            }
            Key::Enter => self.searching = false,
            Key::Backspace => {
                self.query.pop();
            }
            Key::CtrlU => self.query.clear(),
            Key::Char(c) => self.query.push(c),
            Key::Up | Key::CtrlP => self.move_by(-1),
            Key::Down | Key::CtrlN => self.move_by(1),
            Key::PgUp => self.move_by(-(self.page as i64)),
            Key::PgDn => self.move_by(self.page as i64),
            Key::Home => self.move_by(i64::MIN / 2),
            Key::End => self.move_by(i64::MAX / 2),
            Key::Tab => self.set_tab(self.tab.step(1)),
            Key::BackTab => self.set_tab(self.tab.step(-1)),
            Key::AltS => return self.jump(fx.next()),
            Key::AltX => return self.jump(fx.back()),
            _ => {}
        }
        self.rebuild();
        false
    }

    /// agent-roster.py `act`: one key → true to close the menu.
    ///
    /// Changed from the Python, by the new layout: Tab / Shift-Tab switch
    /// tabs (they moved the selection); `s` parks (`H` still does); Alt-s /
    /// Alt-x go next / back and close (`d` still goes next); Home/End,
    /// C-n/C-p and the mouse are new; PgUp/PgDn move a list page (was 10).
    pub fn act(&mut self, key: Key, fx: &mut dyn Effects) -> bool {
        if key == Key::AltW {
            return true; // Option-W toggles the menu shut, whatever mode it is in
        }
        if let Key::Mouse(m) = key {
            return self.mouse(m, fx);
        }
        if self.peek_full.is_some() {
            self.peek_full = None; // any key closes the peek, and only that
            return false;
        }
        if self.confirm.is_some() {
            let yes = matches!(key, Key::Char('y' | 'Y'));
            if yes && !self.prompt_shown {
                return false; // the question was never on screen: neither yes nor no
            }
            let c = self.confirm.take().expect("checked");
            if yes {
                self.run_confirm(c, fx);
            }
            return false;
        }
        if self.searching {
            return self.search_key(key, fx);
        }
        if let Key::Char(c @ '0'..='9') = key {
            return self.digit(c, fx);
        }
        if self.digits_dead {
            // Any non-digit ends the swallowing; esc/⏎/⌫ do only that.
            self.digits_dead = false;
            if matches!(key, Key::Esc | Key::Enter | Key::Backspace) {
                return false;
            }
        }
        if !self.digits.is_empty() {
            // Any other key ends a half-typed number; ⌫ takes a digit back.
            if key == Key::Backspace {
                self.digits.pop();
                return false;
            }
            self.digits.clear();
            if matches!(key, Key::Esc | Key::Enter) {
                return false;
            }
        }
        // Keys that act on the selection act only on a row the user can see
        // (after a wheel scroll it may be off screen): else scroll back to it.
        let t = self.selected_target();
        let row_key = match key {
            Key::Enter | Key::Char(' ') => Some(Button::Go),
            Key::Char('p') => Some(Button::Peek),
            Key::Char('s' | 'H') => Some(Button::Park),
            Key::Char('x') => Some(Button::Close),
            _ => None,
        };
        if let (Some(b), Some(t)) = (row_key, t.clone()) {
            if !self.on_screen(&t.key) {
                self.follow = true;
                return false;
            }
            return self.act_on(b, t, fx);
        }
        match key {
            Key::Esc | Key::Char('q') | Key::CtrlC => {
                if self.query.is_empty() {
                    return true;
                }
                self.query.clear();
                self.rebuild();
            }
            Key::Down | Key::Char('j') | Key::CtrlN => self.move_by(1),
            Key::Up | Key::Char('k') | Key::CtrlP => self.move_by(-1),
            Key::PgDn => self.move_by(self.page as i64),
            Key::PgUp => self.move_by(-(self.page as i64)),
            Key::Home => self.move_by(i64::MIN / 2),
            Key::End => self.move_by(i64::MAX / 2),
            Key::Tab => self.set_tab(self.tab.step(1)),
            Key::BackTab => self.set_tab(self.tab.step(-1)),
            Key::AltS | Key::Char('d') => return self.jump(fx.next()),
            Key::AltX => return self.jump(fx.back()),
            Key::Char('a') => {
                self.show_all = !self.show_all;
                self.rebuild();
            }
            Key::Char('/') => self.searching = true,
            Key::Char('r') => {
                let m = fx.restart_watcher();
                self.say(m);
            }
            _ => {}
        }
        false
    }

    /// The click map of the frame on screen at (col, row).
    pub fn hit(&self, col: u16, row: u16) -> Option<&Hit> {
        self.clicks.iter().find(|(r, _)| col >= r.x && col < r.right() && row >= r.y && row < r.bottom()).map(|(_, h)| h)
    }

    /// A mouse report. A click is a key like any other: it closes the peek,
    /// cancels a y/n, and ends a half-typed number. The wheel only scrolls.
    fn mouse(&mut self, m: Mouse, fx: &mut dyn Effects) -> bool {
        if let Some(d) = m.wheel() {
            if self.peek_full.is_none() && self.confirm.is_none() {
                self.scroll(d);
            }
            return false;
        }
        if !m.is_click() {
            return false;
        }
        if self.peek_full.take().is_some() || self.confirm.take().is_some() {
            return false;
        }
        self.digits.clear();
        self.digits_dead = false;
        let Some(hit) = self.hit(m.col, m.row).cloned() else { return false };
        match hit {
            Hit::Close => return true,
            Hit::Tab(t) => self.set_tab(t),
            Hit::Search => self.searching = true,
            Hit::More => self.set_tab(Tab::Parked),
            Hit::Row(t) => {
                // Click the selected row (or double-click) to go.
                if self.sel.as_ref() == Some(&t.key) {
                    return self.go(&t, fx);
                }
                self.sel = Some(t.key);
                self.follow = true;
            }
            Hit::Button(b, t) => {
                // The row the button was drawn for, if it is still listed.
                if !self.rows.iter().any(|r| r.key().as_ref() == Some(&t.key)) {
                    self.say(format!("{TAB_MOVED} · pick it again"));
                    return false;
                }
                self.sel = Some(t.key.clone());
                return self.act_on(b, t, fx);
            }
        }
        false
    }

    /// The wheel: move the list; the renderer then pulls the selection
    /// into the rows left on screen.
    fn scroll(&mut self, d: i32) {
        self.follow = false;
        self.top = if d < 0 { self.top.saturating_sub(WHEEL_ROWS) } else { self.top + WHEEL_ROWS };
    }

    /// The capture the screen wants now: the full peek's, else the
    /// selected row's preview (none when the preview is hidden).
    pub fn peek_want(&self) -> Option<PeekWant> {
        if let Some(t) = &self.peek_full {
            return Some(PeekWant { target: t.clone(), n: self.full_rows.max(1) });
        }
        if self.preview_rows == 0 {
            return None;
        }
        self.selected_target().map(|t| PeekWant { target: t, n: self.preview_rows })
    }

    /// A capture landed.
    pub fn peek_result(&mut self, win: String, session: String, data: PeekData) {
        self.peeks.insert((win, session), data);
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::super::rows::tests::data;
    use super::*;
    use crate::menu::rows::Tab;
    use Key::*;

    /// Records every effect; `go_err` / `pane_err` make them fail.
    #[derive(Default)]
    pub struct Rec {
        pub calls: Vec<String>,
        pub go_err: Option<String>,
        pub close_err: Option<String>,
    }

    impl Effects for Rec {
        fn go(&mut self, win: &str, session: &str) -> Result<(), String> {
            self.calls.push(format!("go {win} {session}"));
            self.go_err.clone().map_or(Ok(()), Err)
        }
        fn next(&mut self) -> Result<(), String> {
            self.calls.push("next".into());
            Ok(())
        }
        fn back(&mut self) -> Result<(), String> {
            self.calls.push("back".into());
            Ok(())
        }
        fn park(&mut self, win: &str, session: &str) -> Result<String, String> {
            self.calls.push(format!("park {win} {session}"));
            Ok(format!("parking {win}…"))
        }
        fn close(&mut self, win: &str, session: &str) -> Result<String, String> {
            self.calls.push(format!("close {win} {session}"));
            self.close_err.clone().map_or_else(|| Ok(format!("closed {win}")), Err)
        }
        fn restart_watcher(&mut self) -> String {
            self.calls.push("watcher".into());
            "watcher restarted".into()
        }
    }

    fn keys(s: &str) -> Vec<Key> {
        s.chars().map(Char).collect()
    }

    /// Mark every row as drawn (as a tall-enough frame would).
    pub fn draw_all(m: &mut Menu) {
        let mut targets = Vec::new();
        let mut labels = HashMap::new();
        for (i, r) in m.rows.iter().enumerate() {
            if let (Some(t), Some(l)) = (r.target(), m.labels.get(&i)) {
                labels.insert(targets.len(), l.clone());
                targets.push(t);
            }
        }
        m.drawn = (targets, labels);
    }

    fn menu() -> Menu {
        let mut m = Menu::new(data(), Tab::All);
        draw_all(&mut m);
        m
    }

    #[test]
    fn default_selection_is_the_first_need() {
        let m = menu();
        assert_eq!(m.sel, Some(RowKey::Need("@6".into())));
        // No needs: the client's window (its group row).
        let mut m = Menu::new(data(), Tab::Working);
        assert_eq!(m.sel, Some(RowKey::Agent("main".into(), "@2".into())));
        // Tab switch keeps the window when it is listed.
        m.set_tab(Tab::All);
        assert_eq!(m.sel, Some(RowKey::Agent("main".into(), "@2".into())));
        m.set_tab(Tab::Parked);
        assert_eq!(m.sel, Some(RowKey::Parked("@10".into())));
    }

    #[test]
    fn selection_falls_back_like_the_python() {
        let mut m = menu();
        m.sel = Some(RowKey::Need("@1".into()));
        m.query = "Kua".into(); // NEEDS YOU row still there
        m.rebuild();
        assert_eq!(m.sel, Some(RowKey::Need("@1".into())));
        m.set_tab(Tab::Idle); // "Kua" matches nothing idle: the empty list keeps the selection
        assert_eq!(m.sel, Some(RowKey::Need("@1".into())));
        m.query.clear();
        m.rebuild(); // @1 not listed: the client's window (@2, not idle), else the first row
        assert_eq!(m.sel, Some(RowKey::Agent("main".into(), "@4".into())));
        // A need discharged under the selection: the same window's group row.
        let mut m = menu();
        let mut d = data();
        d.view.needs.retain(|n| n.agent.window_id != "@6");
        m.set_data(d);
        assert_eq!(m.sel, Some(RowKey::Agent("work".into(), "@6".into())));
    }

    /// HotkeyTests: `n` idle windows main:1..n, the client on @1.
    fn many(n: u32) -> Menu {
        let mut rows: Vec<String> = (1..=n)
            .map(|i| crate::tmux::tests::row("main", i, &format!("@{i}"), &[("state", "idle"), ("summary", &format!("w{i}"))]))
            .collect();
        rows.push("\x1fclient\x1f/dev/ttys999\x1f@1\x1fmain".into());
        let snap = crate::tmux::Snapshot::parse(&rows.join("\n"));
        let d = Data::build(&snap, Some("/dev/ttys999"), 1000.0, &crate::Collator::new("C"), None, Some(1.0));
        let mut m = Menu::new(d, Tab::All);
        draw_all(&mut m);
        m
    }

    fn without(m: &mut Menu, win: &str) {
        let mut d = m.data.clone();
        for s in &mut d.view.spaces {
            s.agents.retain(|a| a.window_id != win);
        }
        m.set_data(d);
        draw_all(m);
    }

    #[test]
    fn single_digit_goes_at_once() {
        let mut fx = Rec::default();
        assert!(many(4).handle(&[Char('3')], &mut fx));
        assert_eq!(fx.calls, ["go @3 main"]);
    }

    /// test_past_ten_rows.
    #[test]
    fn past_ten_rows() {
        let mut fx = Rec::default();
        let mut m = many(12);
        assert!(m.handle(&[Char('1')], &mut fx)); // 1 still acts on the first key
        assert!(!m.handle(&[Char('0')], &mut fx)); // 0 waits, no timer
        assert_eq!((fx.calls.len(), m.digits.as_str()), (1, "0"));
        assert!(m.handle(&[Char('2')], &mut fx));
        assert_eq!(fx.calls[1], "go @11 main"); // "02": the 11th row
        assert!(many(12).handle(&keys("03"), &mut fx)); // both digits in one read
        assert_eq!(fx.calls[2], "go @12 main");
        let mut m = many(10); // exactly ten: the tenth is 01, a bare 0 never completes
        assert!(!m.handle(&[Char('0')], &mut fx));
        assert!(m.handle(&[Char('1')], &mut fx));
        assert_eq!(fx.calls[3], "go @10 main");
    }

    /// test_half_typed_number_cancels.
    #[test]
    fn half_typed_number_cancels() {
        let mut fx = Rec::default();
        let mut m = many(12);
        m.handle(&[Char('0')], &mut fx);
        assert!(!m.handle(&[Esc], &mut fx)); // esc drops the digit, the menu stays
        assert!(m.digits.is_empty());
        m.handle(&[Char('0')], &mut fx);
        m.handle(&[Backspace], &mut fx);
        assert!(m.digits.is_empty());
        m.handle(&keys("09"), &mut fx); // no row 09 (only 01-03)
        assert!(fx.calls.is_empty());
        assert!(m.msg.contains("no row 09"));
        let sel = m.sel.clone();
        m.handle(&[Char('j')], &mut fx); // another key ends the swallowing and still moves
        assert_ne!(m.sel, sel);
        m.handle(&[Char('0')], &mut fx);
        m.handle(&[Char('j')], &mut fx); // ...and ends a half-typed number the same way
        assert!(m.digits.is_empty());
        assert!(fx.calls.is_empty());
    }

    /// test_dead_number_swallows_its_tail.
    #[test]
    fn dead_number_swallows_its_tail() {
        let mut fx = Rec::default();
        let mut m = many(19);
        let i = m.drawn.1.iter().find(|(_, l)| l.as_str() == "005").map(|(i, _)| *i).unwrap();
        assert_eq!(m.drawn.0[i].win, "@14");
        without(&mut m, "@3"); // the new frame: 18 rows, 01-09
        assert!(!m.drawn.1.values().any(|l| l == "005"));
        assert!(!m.handle(&keys("005"), &mut fx));
        assert!(fx.calls.is_empty());
        assert!(m.msg.contains("no row 00"));
        assert!(!m.handle(&[Char('5')], &mut fx)); // still swallowed, in a later read too
        assert!(fx.calls.is_empty());
        m.handle(&[Esc], &mut fx); // esc ends it without closing
        assert!(m.handle(&[Char('5')], &mut fx)); // a fresh 5 is a jump again
        assert_eq!(fx.calls, ["go @6 main"]);
    }

    /// test_ten_rows_after_eleven.
    #[test]
    fn ten_rows_after_eleven() {
        let mut fx = Rec::default();
        let mut m = many(11);
        without(&mut m, "@2");
        assert!(!m.handle(&[Char('0')], &mut fx));
        assert!(fx.calls.is_empty());
        assert!(m.handle(&[Char('1')], &mut fx));
        assert_eq!(fx.calls, ["go @11 main"]);
    }

    /// test_sequence_resolves_against_the_first_keys_frame.
    #[test]
    fn sequence_resolves_against_the_first_keys_frame() {
        let mut fx = Rec::default();
        let mut m = many(19);
        m.handle(&[Char('0')], &mut fx);
        without(&mut m, "@3"); // redrawn mid-number
        assert!(m.handle(&keys("05"), &mut fx));
        assert_eq!(fx.calls, ["go @14 main"]); // what 005 said when the number was started
    }

    /// test_digits_type_in_the_filter.
    #[test]
    fn digits_type_in_the_search() {
        let mut fx = Rec::default();
        let mut m = many(12);
        m.handle(&[Char('/')], &mut fx);
        m.handle(&[Char('1')], &mut fx);
        m.handle(&[Char('1')], &mut fx);
        assert_eq!(m.query, "11");
        assert!(fx.calls.is_empty());
        m.handle(&[Enter], &mut fx);
        draw_all(&mut m);
        assert_eq!(m.drawn.0.len(), 1);
        assert!(m.handle(&[Char('1')], &mut fx));
        assert_eq!(fx.calls, ["go @11 main"]);
    }

    /// test_number_means_the_row_as_displayed: not-drawn rows are not acted on.
    #[test]
    fn number_means_the_row_as_displayed() {
        let mut fx = Rec::default();
        let mut m = many(30);
        m.drawn.0.truncate(5);
        m.drawn.1.retain(|i, _| *i < 5);
        assert!(!m.handle(&[Char('9')], &mut fx));
        assert!(fx.calls.is_empty());
    }

    #[test]
    fn moving_passes_duplicates_and_clamps() {
        let mut m = menu();
        m.move_by(-5);
        assert_eq!(m.sel, Some(RowKey::Need("@6".into())));
        let mut seen = vec![m.sel.clone().unwrap()];
        for _ in 0..20 {
            m.act(Down, &mut NoEffects);
            seen.push(m.sel.clone().unwrap());
        }
        // Every window row once, top to bottom, then it stays at the bottom.
        assert_eq!(seen[2], RowKey::Agent("main".into(), "@1".into()));
        assert_eq!(seen[12], RowKey::Parked("@12".into()));
        assert!(seen[13..].iter().all(|k| *k == seen[12]));
        m.act(Home, &mut NoEffects);
        assert_eq!(m.sel, Some(RowKey::Need("@6".into())));
        m.act(End, &mut NoEffects);
        assert_eq!(m.sel, Some(RowKey::Parked("@12".into())));
        m.act(Char('k'), &mut NoEffects);
        m.act(CtrlP, &mut NoEffects);
        assert_eq!(m.sel, Some(RowKey::Parked("@10".into())));
    }

    /// ActTests.test_park_needs_confirm / test_close_needs_confirm.
    #[test]
    fn confirm_flows() {
        let mut fx = Rec::default();
        let mut m = menu();
        assert!(!m.act(Char('s'), &mut fx));
        assert!(fx.calls.is_empty());
        assert_eq!(m.confirm.as_ref().map(|c| c.verb()), Some("park"));
        assert!(!m.act(Char('n'), &mut fx)); // anything but y cancels
        assert!(fx.calls.is_empty() && m.confirm.is_none());
        m.act(Char('H'), &mut fx);
        m.act(Esc, &mut fx); // even esc only cancels
        assert!(fx.calls.is_empty() && m.confirm.is_none());
        m.act(Char('s'), &mut fx);
        m.frame_shown();
        m.act(Char('y'), &mut fx);
        assert_eq!(fx.calls, ["park @6 work"]);
        assert!(m.msg.starts_with("parking "));
        m.act(Char('x'), &mut fx);
        assert_eq!(m.confirm.as_ref().map(|c| c.verb()), Some("close"));
        m.frame_shown();
        m.act(Char('Y'), &mut fx);
        assert_eq!(fx.calls[1], "close @6 work");
        // A refusal says why; TAB_MOVED says which key to press again.
        fx.close_err = Some(TAB_MOVED.into());
        m.act(Char('x'), &mut fx);
        m.frame_shown();
        m.act(Char('y'), &mut fx);
        assert_eq!(m.msg, "that tab moved · press x again");
        // A parked row: x discards, s refuses.
        m.act(End, &mut fx);
        m.act(Char('s'), &mut fx);
        assert!(m.confirm.is_none());
        assert_eq!(m.msg, "already parked");
        m.act(Char('x'), &mut fx);
        assert_eq!(m.confirm.as_ref().map(|c| c.verb()), Some("discard"));
    }

    /// test_typing_into_the_popup_parks_nothing / test_confirm_never_answered_by_its_own_batch.
    #[test]
    fn a_batch_never_confirms_itself() {
        let mut fx = Rec::default();
        let mut m = menu();
        for burst in ["sy", "xy", "Hyy", "xYy"] {
            m.confirm = None;
            assert!(!m.handle(&keys(burst), &mut fx));
            assert!(fx.calls.is_empty(), "{burst}");
            assert!(m.confirm.is_some(), "{burst}");
        }
        m.confirm = None;
        m.handle(&[Char('s')], &mut fx);
        m.frame_shown();
        m.handle(&[Char('y')], &mut fx);
        assert_eq!(fx.calls.len(), 1);
    }

    /// A y typed before any frame showed the question (queued behind a
    /// refresh) neither confirms nor cancels it.
    #[test]
    fn y_before_the_prompt_is_drawn_does_nothing() {
        let mut fx = Rec::default();
        let mut m = menu();
        m.handle(&[Char('x')], &mut fx);
        assert!(!m.handle(&[Char('y')], &mut fx));
        assert!(fx.calls.is_empty());
        assert!(m.confirm.is_some(), "the prompt stays");
        m.frame_shown(); // now it is on screen
        m.handle(&[Char('y')], &mut fx);
        assert_eq!(fx.calls, ["close @6 work"]);
        // A prompt reopened after one was shown starts unseen again.
        m.handle(&[Char('s')], &mut fx);
        assert!(!m.prompt_shown);
        m.handle(&[Char('y')], &mut fx);
        assert_eq!(fx.calls.len(), 1);
        // Any other key still cancels an unseen prompt.
        m.handle(&[Char('n')], &mut fx);
        assert!(m.confirm.is_none());
    }

    /// A row that is not on screen is never acted on by a key: it scrolls
    /// back into view instead.
    #[test]
    fn keys_refuse_a_row_off_screen() {
        let mut fx = Rec::default();
        let mut m = menu();
        m.drawn.0.retain(|t| t.key != RowKey::Need("@6".into()));
        m.follow = false;
        for k in [Enter, Char('x'), Char('s'), Char('p'), Char(' ')] {
            assert!(!m.act(k, &mut fx));
            assert!(m.follow, "{k:?} re-follows");
            m.follow = false;
        }
        assert!(fx.calls.is_empty() && m.confirm.is_none() && m.peek_full.is_none());
    }

    /// A blank snapshot (or a search that matches nothing) keeps the
    /// selection; when the rows come back it is still there.
    #[test]
    fn empty_list_keeps_the_selection() {
        let mut m = menu();
        m.move_by(3);
        let sel = m.sel.clone();
        let empty = Data::build(&crate::tmux::Snapshot::default(), Some("/dev/ttys999"), 1000.0,
            &crate::Collator::new("C"), None, Some(1.0));
        m.set_data(empty);
        assert_eq!(m.sel, sel);
        m.set_data(data());
        assert_eq!(m.sel, sel);
        m.handle(&keys("/qqq"), &mut NoEffects); // matches nothing
        assert_eq!(m.sel, sel);
        m.handle(&[Esc], &mut NoEffects);
        assert_eq!(m.sel, sel);
    }

    #[test]
    fn going() {
        let mut fx = Rec::default();
        let mut m = menu();
        assert!(m.act(Enter, &mut fx)); // closes
        assert_eq!(fx.calls, ["go @6 work"]);
        fx.go_err = Some("agent-jump.sh goto timed out".into());
        assert!(!m.act(Char(' '), &mut fx));
        assert!(m.msg.contains("timed out"));
        let mut fx = Rec::default();
        assert!(m.act(AltS, &mut fx) && m.act(Char('d'), &mut fx) && m.act(AltX, &mut fx));
        assert_eq!(fx.calls, ["next", "next", "back"]);
        // A parked row goes through its own session (unstash).
        m.act(End, &mut fx);
        assert!(m.act(Enter, &mut fx));
        assert_eq!(fx.calls[3], "go @12 stash");
    }

    /// OptionWTests.test_closes_in_every_mode, and the other closers.
    #[test]
    fn closing() {
        let mut m = menu();
        assert!(m.act(AltW, &mut NoEffects));
        m.searching = true;
        m.query = "ab".into();
        assert!(m.act(AltW, &mut NoEffects));
        m.searching = false;
        m.peek_full = m.selected_target();
        assert!(m.act(AltW, &mut NoEffects));
        m.peek_full = None;
        m.act(Char('s'), &mut NoEffects);
        assert!(m.act(AltW, &mut NoEffects));
        m.digits = "0".into();
        assert!(m.act(AltW, &mut NoEffects));
        let mut m = menu();
        assert!(m.act(Char('q'), &mut NoEffects));
        assert!(m.act(CtrlC, &mut NoEffects));
    }

    #[test]
    fn esc_clears_the_search_then_closes() {
        let mut m = menu();
        m.handle(&keys("/kua"), &mut NoEffects);
        assert!(m.searching);
        assert_eq!(m.query, "kua");
        assert_eq!(m.sel, Some(RowKey::Need("@1".into()))); // the selection followed the filter
        assert!(!m.act(Esc, &mut NoEffects)); // clears and leaves the box
        assert!(!m.searching && m.query.is_empty());
        assert!(m.act(Esc, &mut NoEffects)); // the second closes
        // ⏎ keeps the query: then Esc clears it, and the next Esc closes.
        let mut m = menu();
        m.handle(&keys("/kua"), &mut NoEffects);
        m.act(Enter, &mut NoEffects);
        assert!(!m.searching);
        assert_eq!(m.query, "kua");
        assert!(!m.act(Esc, &mut NoEffects));
        assert!(m.query.is_empty());
        assert!(m.act(Esc, &mut NoEffects));
        // In the box, q j k x s p and digits type.
        let mut m = menu();
        m.handle(&keys("/qjkxsp12 "), &mut NoEffects);
        assert_eq!(m.query, "qjkxsp12 ");
        m.act(Backspace, &mut NoEffects);
        m.act(CtrlU, &mut NoEffects);
        assert!(m.query.is_empty() && m.searching && m.confirm.is_none());
    }

    #[test]
    fn tab_keys_cycle() {
        let mut m = menu();
        m.act(Key::Tab, &mut NoEffects);
        assert_eq!(m.tab, Tab::Needs);
        m.act(BackTab, &mut NoEffects);
        m.act(BackTab, &mut NoEffects);
        assert_eq!(m.tab, Tab::Parked);
        m.act(Char('/'), &mut NoEffects);
        m.act(Key::Tab, &mut NoEffects); // also from the search box
        assert_eq!(m.tab, Tab::All);
    }

    #[test]
    fn peek_toggles_and_drops_its_batch() {
        let mut m = menu();
        // PeekTests.test_peek_drops_the_rest_of_its_batch: "pq" must not close.
        assert!(!m.handle(&keys("pq"), &mut NoEffects));
        assert!(m.peek_full.is_some());
        assert!(!m.handle(&keys("q"), &mut NoEffects)); // q closes the peek, not the menu
        assert!(m.peek_full.is_none());
        m.act(Char('p'), &mut NoEffects);
        m.act(Char('p'), &mut NoEffects); // p again: toggled off
        assert!(m.peek_full.is_none());
        assert!(m.handle(&keys("q"), &mut NoEffects));
        // What it asks for.
        m.full_rows = 20;
        m.preview_rows = 0;
        assert_eq!(m.peek_want(), None); // no preview at this width
        m.preview_rows = 9;
        assert_eq!(m.peek_want().map(|w| (w.target.win, w.n)), Some(("@6".into(), 9)));
        m.act(Char('p'), &mut NoEffects);
        assert_eq!(m.peek_want().map(|w| w.n), Some(20));
    }

    #[test]
    fn show_all_and_watcher_keys() {
        let mut fx = Rec::default();
        let mut m = menu();
        let n = m.rows.len();
        m.act(Char('a'), &mut fx);
        assert_eq!(m.rows.len(), n + 1);
        m.act(Char('r'), &mut fx);
        assert_eq!((fx.calls[0].as_str(), m.msg.as_str()), ("watcher", "watcher restarted"));
    }
}
