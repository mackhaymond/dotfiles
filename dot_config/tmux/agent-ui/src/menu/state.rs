//! The menu's state machine, driven by [`Key`]s and clicks, with every side
//! effect behind [`Effects`] so the tests can record instead of act.
//!
//! The keys (one mode at a time):
//! - **List**: a letter (or its capital) goes to the agent holding it AT ONCE
//!   (no second key, no wait); ↑/↓, C-n/C-p, PgUp/PgDn, Home/End and the
//!   wheel move; ⏎ goes to the selected row (the `▪ N parked` row opens the
//!   Parked filter); ⇥/⇧⇥ cycle the filter; `/` searches; Space opens the
//!   actions strip; ⌥S / ⌥X go next / back; Esc / C-c close. (When CuaNotch's
//!   keyJumpToNeedy is opt+s it owns ⌥S globally, so the popup's ⌥S only
//!   fires while CuaNotch isn't running.)
//! - **Search** (`/`): typed characters (letters and digits included) filter
//!   the list; ⏎ goes to the selected match; Esc leaves the search and clears
//!   it (a second Esc closes); Backspace on an empty query leaves it.
//! - **Actions** (Space): the footer lists what the selected row offers
//!   (`p` preview, `s` park, `x` close or discard, `r` restart the watcher,
//!   ⏎ go); the next key runs one; Esc or any other key cancels.
//! - **y/n** after park / close: only `y` (on a prompt already on screen)
//!   confirms; any other key cancels.
//! - **⌥W**, in every mode: go to the LAST agent ([`Data::last`]); with none,
//!   close the menu.
//!
//! The safety rules:
//! - Letters resolve against the frame ON SCREEN ([`Menu::drawn`], set by
//!   the renderer), never a list rebuilt since: each letter maps to the
//!   window that held it when the frame was drawn. If that agent has gone
//!   and its letter went to a newcomer before the next frame, the letter
//!   still means the old window (the jump fails and says so); a letter
//!   nobody held in that frame does nothing.
//! - ⏎ and Space act only on a row the user can see and last saw selected:
//!   a refresh that removed it refuses the key ("that tab moved · pick it
//!   again"); a row scrolled off screen is scrolled back instead.
//! - A key that opens a y/n drops the rest of its read, so a paste or a fast
//!   " xy" never confirms itself; a `y` typed before the prompt was drawn
//!   neither confirms nor cancels.
//! - A confirm acts on the window as it is NOW: gone, or moved to another
//!   session meanwhile, refuses with the reason (the core's re-check).
//! - Moved out from under: when a refresh shows the client on another
//!   window than at open and the menu did not cause it (CuaNotch's ⌥S jump,
//!   anything external), [`Menu::moved_away`] is set and the loop closes the
//!   popup rather than leave it stale over the new window. A missing client
//!   row never counts; the menu's own go/next/back close it anyway, and its
//!   own park/close of the client's window is followed, not closed on.

use super::keys::{Key, Mouse};
use super::letters::ALPHABET;
use super::rows::{build_rows, need_of, Data, Row, RowKey, Tab, Target};
use crate::actions::TAB_MOVED;
use crate::ansi::StyledLine;
use crate::view::Need;
use ratatui::layout::Rect;
use std::collections::{BTreeMap, HashMap};

/// What the frame on screen offers the keys (set by the renderer).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Drawn {
    /// The window rows on screen, in display order.
    pub rows: Vec<Target>,
    /// Letter → the row it goes to, for every letter in the frame's data:
    /// its first row on screen, else its first row in the All order.
    pub letters: BTreeMap<char, Target>,
    /// Where ⌥W goes in this frame.
    pub last: Option<Target>,
}

impl Drawn {
    /// The frame of `d` with `rows` on screen.
    pub fn of(d: &Data, rows: Vec<Target>) -> Drawn {
        let row = |win: &str| rows.iter().find(|t| t.win == win && !t.parked()).cloned().or_else(|| d.first_row(win));
        let letters = d.letters.iter().filter_map(|(w, &c)| Some((c, row(w)?))).collect();
        let last = d.last.as_deref().and_then(row);
        Drawn { letters, last, rows }
    }
}

/// Everything the menu can do to the outside world. `Err` carries the
/// footer text.
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

/// What a row can have done to it (⏎, and the Space strip's keys).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Act {
    Go,
    /// Toggle the bottom preview strip.
    Peek,
    Park,
    Close,
    Restart,
}

/// The actions strip's entries for `t`: (key, act, words).
pub fn strip(t: &Target) -> Vec<(char, Act, &'static str)> {
    let mut v = vec![('p', Act::Peek, "preview")];
    if !t.parked() {
        v.push(('s', Act::Park, "park"));
    }
    v.push(('x', Act::Close, if t.parked() { "discard" } else { "close" }));
    v.push(('r', Act::Restart, "restart watcher"));
    v
}

/// What a click at a cell does (the click map of the frame on screen).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Hit {
    /// The ✕ in the top border.
    Close,
    /// The header's filter words: the next filter.
    Filter,
    Row(Target),
    /// The `▪ N parked ⇥` row.
    Fold,
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

/// How long a footer message stays.
pub const MSG_SECS: f64 = 3.0;
/// Rows the wheel scrolls per notch.
pub const WHEEL_ROWS: usize = 3;

/// A letter key's letter (capitals too), if it is one of [`ALPHABET`].
pub fn letter_key(c: char) -> Option<char> {
    let l = c.to_ascii_lowercase();
    ALPHABET.contains(&l).then_some(l)
}

pub struct Menu {
    pub data: Data,
    pub tab: Tab,
    pub query: String,
    /// The search has the keys.
    pub searching: bool,
    pub rows: Vec<Row>,
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
    /// Space: the actions strip is open for this row.
    pub actions: Option<Target>,
    /// The bottom preview strip is on (Space p). Off by default.
    pub preview: bool,
    pub msg: String,
    pub msg_until: f64,
    /// The last frame's rows, letters and last agent (set by the renderer):
    /// keys resolve against this.
    pub drawn: Drawn,
    /// The last frame's click map.
    pub clicks: Vec<(Rect, Hit)>,
    /// Captured panes by (window, session).
    pub peeks: HashMap<(String, String), PeekData>,
    /// Lines the preview strip shows (set by the renderer; 0: none).
    pub preview_rows: usize,
    /// The selected row as the user last saw it drawn, or last chose it (a
    /// move, a click, a search, a filter): ⏎ / Space act only while that row
    /// is still listed.
    pub drawn_sel: Option<Target>,
    /// The filter `rows` was built for (the fallback walks the previous
    /// rows of the same filter).
    rows_tab: Tab,
    /// The client's window (its snapshot client row) when the menu opened,
    /// or the first frame that had the row: see [`Menu::moved_away`].
    home_win: Option<String>,
    /// Windows this menu parked or closed: the client leaving one of them
    /// is the menu's own doing, so the menu follows instead of closing.
    acted_on: Vec<String>,
    /// The client's window changed under the open menu and the menu did not
    /// cause it (CuaNotch's ⌥S jump, a click on the tab bar, any outside
    /// switch): the popup is stale over the new window, so the loop closes.
    pub moved_away: bool,
}

impl Menu {
    pub fn new(data: Data, tab: Tab) -> Menu {
        let mut m = Menu {
            data,
            tab,
            query: String::new(),
            searching: false,
            rows: Vec::new(),
            sel: None,
            top: 0,
            follow: true,
            page: 10,
            confirm: None,
            prompt_shown: false,
            actions: None,
            preview: false,
            msg: String::new(),
            msg_until: 0.0,
            drawn: Drawn::default(),
            clicks: Vec::new(),
            peeks: HashMap::new(),
            preview_rows: 0,
            drawn_sel: None,
            rows_tab: tab,
            home_win: None,
            acted_on: Vec::new(),
            moved_away: false,
        };
        m.track_client();
        m.rebuild();
        m
    }

    /// Follow the client's window. The first frame only records it; a
    /// missing client row (a snapshot that timed out, the client between
    /// sessions) changes nothing. Only a row that is present and names
    /// another window counts: after the menu's own park/close of the home
    /// window the new window becomes home, anything else sets
    /// [`moved_away`](Self::moved_away). The menu's go / next / back never
    /// get here: a move that ran closes the menu on the spot.
    fn track_client(&mut self) {
        let Some(cur) = self.data.view.cur_win.clone().filter(|w| !w.is_empty()) else { return };
        match &self.home_win {
            None => self.home_win = Some(cur),
            Some(h) if *h == cur => {}
            Some(h) if self.acted_on.contains(h) => self.home_win = Some(cur),
            Some(_) => self.moved_away = true,
        }
    }

    /// The user picked the current selection: keys may act on it before
    /// the next draw.
    fn chose(&mut self) {
        self.drawn_sel = self.selected_target();
    }

    /// A new refresh's data. Check [`moved_away`](Self::moved_away) after.
    pub fn set_data(&mut self, data: Data) {
        if data != self.data {
            self.data = data;
            self.track_client();
            self.rebuild();
        }
    }

    /// The rows for the filter and query, and a selection that survives it.
    ///
    /// On open: the oldest agent that needs you (the first in
    /// `agent-jump.sh list` order: its row in the session that queue names,
    /// else any of its rows), else the last agent (⌥W's), else the window
    /// the client is on, else the first row. The selection is a row key,
    /// never a position, so a refresh that changes an agent's state keeps
    /// it. When the selected row goes away (filter typed, tab parked, filter
    /// switched) it goes to another row of the same window, else the nearest
    /// row that survived (the next one in the previous order, else the one
    /// before), else the client's window, else the first row.
    pub fn rebuild(&mut self) {
        let old_keys: Vec<RowKey> =
            if self.rows_tab == self.tab { self.rows.iter().filter_map(Row::key).collect() } else { Vec::new() };
        self.rows = build_rows(&self.data, self.tab, &self.query);
        self.rows_tab = self.tab;
        let keys: Vec<RowKey> = self.rows.iter().filter_map(Row::key).collect();
        // An empty list (a blank snapshot, a search typo) keeps the selection
        // for when the rows come back.
        if keys.is_empty() || self.sel.as_ref().is_some_and(|k| keys.contains(k)) {
            return;
        }
        let row_for = |wid: &str| -> Option<RowKey> {
            let hits: Vec<&RowKey> = keys.iter().filter(|k| k.window() == wid && !wid.is_empty()).collect();
            hits.iter().find(|k| matches!(k, RowKey::Agent(..))).or(hits.first()).map(|k| (*k).clone())
        };
        let cur = self.data.view.cur_win.clone();
        let cur_row = || cur.as_deref().and_then(row_for);
        let first_need = || {
            self.data.view.needs.iter().find_map(|n| {
                let a = &n.agent;
                [RowKey::Need(a.window_id.clone()), RowKey::Agent(a.session.clone(), a.window_id.clone())]
                    .into_iter()
                    .find(|k| keys.contains(k))
                    .or_else(|| row_for(&a.window_id))
            })
        };
        let last_row = || self.data.last.as_deref().and_then(row_for);
        let neighbour = |old: &RowKey| -> Option<RowKey> {
            let i = old_keys.iter().position(|k| k == old)?;
            old_keys[i + 1..].iter().chain(old_keys[..i].iter().rev()).find(|k| keys.contains(k)).cloned()
        };
        self.sel = match self.sel.take() {
            None => first_need().or_else(last_row).or_else(cur_row),
            Some(old) => row_for(old.window()).or_else(|| neighbour(&old)).or_else(cur_row),
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

    /// The need a row shows inline (its reason): a Needs row's own, or an
    /// attention agent's queue entry.
    pub fn reason_of<'a>(&'a self, row: &'a Row) -> Option<&'a Need> {
        match row {
            Row::Need(n) => Some(n),
            Row::Agent(a) => need_of(&self.data.view, a),
            _ => None,
        }
    }

    /// The need an agent's row shows inline, wherever it is listed.
    pub fn reason_of_agent(&self, a: &crate::view::Agent) -> Option<&Need> {
        need_of(&self.data.view, a)
    }

    /// The row is in the frame on screen.
    pub fn on_screen(&self, key: &RowKey) -> bool {
        self.drawn.rows.iter().any(|t| t.key == *key)
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

    /// Run `act` on the row `t`.
    fn act_on(&mut self, act: Act, t: Target, fx: &mut dyn Effects) -> bool {
        match act {
            Act::Go => return self.go(&t, fx),
            Act::Peek => {
                self.preview = !self.preview;
                self.follow = true;
            }
            Act::Park if t.parked() => self.say("already parked"),
            Act::Park => self.ask(Action::Park, t),
            Act::Close => self.ask(Action::Close, t),
            Act::Restart => {
                let m = fx.restart_watcher();
                self.say(m);
            }
        }
        false
    }

    /// By `d` selectable rows, clamped.
    pub fn move_by(&mut self, d: i64) {
        let keys: Vec<RowKey> = self.rows.iter().filter_map(Row::key).collect();
        if keys.is_empty() {
            return;
        }
        let i = self.sel.as_ref().and_then(|k| keys.iter().position(|x| x == k)).unwrap_or(0) as i64;
        self.sel = Some(keys[(i + d).clamp(0, keys.len() as i64 - 1) as usize].clone());
        self.follow = true;
        self.chose();
    }

    pub fn set_tab(&mut self, tab: Tab) {
        if tab != self.tab {
            self.tab = tab;
            self.top = 0;
            self.rebuild();
            self.chose();
        }
    }

    pub fn say(&mut self, text: impl Into<String>) {
        self.msg = text.into();
        self.msg_until = crate::text::now() + MSG_SECS;
    }

    /// Ok closes the menu; a refusal stays, with why.
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

    /// ⌥W: the last agent of the frame on screen, else close.
    fn last(&mut self, fx: &mut dyn Effects) -> bool {
        match self.drawn.last.clone() {
            Some(t) => self.go(&t, fx),
            None => true,
        }
    }

    /// A letter: the window holding it in the frame on screen.
    fn letter(&mut self, c: char, fx: &mut dyn Effects) -> bool {
        match self.drawn.letters.get(&c).cloned() {
            Some(t) => self.go(&t, fx),
            None => {
                self.say(format!("no agent on {c}"));
                false
            }
        }
    }

    /// One read's keys → true to close the menu.
    pub fn handle(&mut self, keys: &[Key], fx: &mut dyn Effects) -> bool {
        for &key in keys {
            let asking = self.confirm.is_none();
            if self.act(key, fx) {
                return true;
            }
            if asking && self.confirm.is_some() {
                return false; // a y/n opened: drop the rest of this read
            }
        }
        false
    }

    /// The y of a confirm: act on the window as it is now.
    fn run_confirm(&mut self, c: Confirm, fx: &mut dyn Effects) {
        let t = &c.target;
        let r = match c.action {
            Action::Park => fx.park(&t.win, &t.session),
            Action::Close => fx.close(&t.win, &t.session),
        };
        match r {
            Ok(m) => {
                // Parking or closing the client's own window moves the client.
                self.acted_on.push(t.win.clone());
                self.say(m)
            }
            Err(e) if e == TAB_MOVED => {
                let k = if c.action == Action::Park { "s" } else { "x" };
                self.say(format!("{TAB_MOVED} · press ␣ {k} again"));
            }
            Err(e) => self.say(e),
        }
    }

    /// The selection, if a key may act on it: a row the user can see and
    /// last saw selected. Else refuse (the row went away under the key) or
    /// scroll it back into view, and None.
    fn checked_selection(&mut self) -> Option<Target> {
        let t = self.selected_target()?;
        let gone = self.drawn_sel.as_ref().is_some_and(|d| !self.rows.iter().any(|r| r.key().as_ref() == Some(&d.key)));
        if gone {
            self.say(format!("{TAB_MOVED} · pick it again"));
            self.follow = true;
            return None;
        }
        if !self.on_screen(&t.key) {
            self.follow = true;
            return None;
        }
        Some(t)
    }

    /// ⏎: go to the selected row; the fold row opens the Parked filter.
    fn enter(&mut self, fx: &mut dyn Effects, checked: bool) -> bool {
        if self.sel == Some(RowKey::Fold) {
            self.set_tab(Tab::Parked);
            return false;
        }
        let t = if checked { self.checked_selection() } else { self.selected_target() };
        match t {
            Some(t) => self.go(&t, fx),
            None => false,
        }
    }

    /// Keys while the search has them.
    fn search_key(&mut self, key: Key, fx: &mut dyn Effects) -> bool {
        match key {
            Key::Esc => {
                self.searching = false;
                self.query.clear();
            }
            Key::CtrlC => return true,
            Key::Enter => return self.enter(fx, false),
            Key::Backspace if self.query.is_empty() => self.searching = false,
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
            _ => return false,
        }
        self.rebuild();
        self.chose();
        false
    }

    /// The key after Space: run that action on the strip's row, or cancel.
    fn strip_key(&mut self, key: Key, t: Target, fx: &mut dyn Effects) -> bool {
        let act = match key {
            Key::Enter => Some(Act::Go),
            Key::Char(c) => strip(&t).into_iter().find(|(k, _, _)| *k == c.to_ascii_lowercase()).map(|(_, a, _)| a),
            _ => None, // Esc, and anything else: cancelled
        };
        match act {
            Some(a) => self.act_on(a, t, fx),
            None => false,
        }
    }

    /// One key → true to close the menu.
    pub fn act(&mut self, key: Key, fx: &mut dyn Effects) -> bool {
        if let Key::Mouse(m) = key {
            return self.mouse(m, fx);
        }
        if key == Key::AltW {
            return self.last(fx); // the key that opened the menu: the last agent, or close
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
        if let Some(t) = self.actions.take() {
            return self.strip_key(key, t, fx);
        }
        if self.searching {
            return self.search_key(key, fx);
        }
        match key {
            // (On the fold row Space does nothing: it falls through below.)
            Key::Char(' ') if self.sel != Some(RowKey::Fold) => self.actions = self.checked_selection(),
            Key::Char('/') => {
                self.searching = true;
                self.query.clear();
            }
            Key::Char(c) => {
                if let Some(l) = letter_key(c) {
                    return self.letter(l, fx);
                }
            }
            Key::Enter => return self.enter(fx, true),
            Key::Esc | Key::CtrlC => return true,
            Key::Down | Key::CtrlN => self.move_by(1),
            Key::Up | Key::CtrlP => self.move_by(-1),
            Key::PgDn => self.move_by(self.page as i64),
            Key::PgUp => self.move_by(-(self.page as i64)),
            Key::Home => self.move_by(i64::MIN / 2),
            Key::End => self.move_by(i64::MAX / 2),
            Key::Tab => self.set_tab(self.tab.step(1)),
            Key::BackTab => self.set_tab(self.tab.step(-1)),
            Key::AltS => return self.jump(fx.next()),
            Key::AltX => return self.jump(fx.back()),
            _ => {}
        }
        false
    }

    /// The click map of the frame on screen at (col, row).
    pub fn hit(&self, col: u16, row: u16) -> Option<&Hit> {
        self.clicks.iter().find(|(r, _)| col >= r.x && col < r.right() && row >= r.y && row < r.bottom()).map(|(_, h)| h)
    }

    /// A mouse report. A click is a key like any other: it cancels a y/n
    /// or the actions strip (and only that). The wheel only scrolls.
    fn mouse(&mut self, m: Mouse, fx: &mut dyn Effects) -> bool {
        if let Some(d) = m.wheel() {
            if self.confirm.is_none() {
                self.scroll(d);
            }
            return false;
        }
        if !m.is_click() {
            return false;
        }
        if self.confirm.take().is_some() || self.actions.take().is_some() {
            return false;
        }
        let Some(hit) = self.hit(m.col, m.row).cloned() else { return false };
        match hit {
            Hit::Close => return true,
            Hit::Filter => self.set_tab(self.tab.step(1)),
            Hit::Fold => self.set_tab(Tab::Parked),
            Hit::Row(t) => {
                // Click the selected row (or double-click) to go.
                if self.sel.as_ref() == Some(&t.key) {
                    return self.go(&t, fx);
                }
                self.sel = Some(t.key);
                self.follow = true;
                self.chose();
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

    /// The capture the preview strip wants now (none while it is off).
    pub fn peek_want(&self) -> Option<PeekWant> {
        if !self.preview || self.preview_rows == 0 {
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
    use super::super::rows::tests::{data, data_of, fixture, stored, with_states};
    use super::super::rows::Tab;
    use super::*;
    use Key::*;

    /// Records every effect; `go_err` / `close_err` make them fail.
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
        let targets = m.rows.iter().filter_map(Row::target).collect();
        m.drawn = Drawn::of(&m.data, targets);
        m.drawn_sel = m.selected_target();
    }

    fn menu_of(d: Data) -> Menu {
        let mut m = Menu::new(d, Tab::All);
        draw_all(&mut m);
        m
    }

    fn menu() -> Menu {
        menu_of(data())
    }

    fn ak(s: &str, w: &str) -> RowKey {
        RowKey::Agent(s.into(), w.into())
    }

    fn row_keys(m: &Menu) -> Vec<RowKey> {
        m.rows.iter().filter_map(Row::key).collect()
    }

    /// The fixture with the global MRU pair set.
    fn with_mru(cur: &str, prev: &str) -> Data {
        let mut snap = fixture();
        for w in &mut snap.windows {
            w.mru_cur = cur.into();
            w.mru_prev = prev.into();
        }
        data_of(&snap)
    }

    #[test]
    fn opens_on_all() {
        assert_eq!(crate::menu::parse_args(&[]).unwrap().tab, Tab::All);
        let m = menu();
        assert_eq!(m.tab, Tab::All);
        assert!(!m.preview && !m.searching && m.actions.is_none());
    }

    /// The oldest need (the first in needs order: @6 failed), else the last
    /// agent, else the client's window, else the first row.
    #[test]
    fn initial_cursor() {
        assert_eq!(menu().sel, Some(ak("work", "@6")));
        let calm = |cur: &str, prev: &str| {
            let mut snap = with_states(&[("@6", "idle"), ("@1", "idle")]);
            for w in &mut snap.windows {
                w.mru_cur = cur.into();
                w.mru_prev = prev.into();
            }
            data_of(&snap)
        };
        assert!(calm("", "").view.needs.is_empty());
        // No needs: the last agent (prev, since cur is the client's @2).
        assert_eq!(Menu::new(calm("@2", "@7"), Tab::All).sel, Some(ak("work", "@7")));
        // A linked last agent: its first row in display order.
        assert_eq!(Menu::new(calm("@3", ""), Tab::All).sel, Some(ak("main", "@3")));
        // No last agent: the client's window.
        assert_eq!(Menu::new(calm("", ""), Tab::All).sel, Some(ak("main", "@2")));
        // Not even that: the first row.
        let mut d = calm("", "");
        d.view.cur_win = Some("@99".into());
        let m = Menu::new(d, Tab::All);
        assert_eq!(m.sel, Some(ak("Alpha", "@8")));
        // A need outranks the last agent.
        assert_eq!(Menu::new(with_mru("@2", "@7"), Tab::All).sel, Some(ak("work", "@6")));
        // On the Needs filter: its own row.
        assert_eq!(Menu::new(data(), Tab::Needs).sel, Some(RowKey::Need("@6".into())));
    }

    /// The point of the fixed order: a state change never moves a row or
    /// the selection.
    #[test]
    fn state_flip_keeps_rows_and_selection() {
        let mut m = menu();
        let before = row_keys(&m);
        m.sel = Some(ak("main", "@2"));
        m.set_data(data_of(&with_states(&[("@2", "needs-input")])));
        assert_eq!(row_keys(&m), before);
        assert_eq!(m.sel, Some(ak("main", "@2")));
        let a = m.selected().and_then(Row::agent).unwrap();
        assert_eq!((a.glyph, a.color), ("◉", Some("yellow")));
        assert_eq!(m.reason_of(m.selected().unwrap()).map(|n| n.reason_word.as_str()), Some("waiting on you"));
        // Letters don't move either.
        assert_eq!(m.data.letter("@2"), data().letter("@2"));
        m.set_data(data_of(&with_states(&[("@2", "idle"), ("@6", "running")])));
        assert_eq!(row_keys(&m), before);
        assert_eq!(m.sel, Some(ak("main", "@2")));
    }

    /// The selected agent goes away: the nearest surviving row; a key typed
    /// against the frame that still showed it is refused, not retargeted.
    #[test]
    fn selection_leaving_the_list() {
        let mut fx = Rec::default();
        let mut m = menu();
        assert_eq!(m.sel, Some(ak("work", "@6")));
        let mut snap = fixture();
        snap.windows.retain(|w| w.id != "@6");
        m.set_data(data_of(&snap));
        assert_eq!(m.sel, Some(ak("work", "@7"))); // the next row, not the client's @2
        for k in [Enter, Char(' ')] {
            assert!(!m.handle(&[k], &mut fx), "{k:?}");
            assert_eq!(m.msg, "that tab moved · pick it again", "{k:?}");
        }
        assert!(fx.calls.is_empty() && m.actions.is_none());
        draw_all(&mut m);
        assert!(m.handle(&[Enter], &mut fx));
        assert_eq!(fx.calls, ["go @7 work"]);
        // A move is a choice: ↑ then ⏎ before any redraw acts on the new row.
        let mut fx = Rec::default();
        let mut m = menu();
        m.set_data(data_of(&snap));
        assert!(m.handle(&[Up, Enter], &mut fx));
        assert_eq!(fx.calls, ["go @4 main"]);
    }

    /// A letter goes at once, to the window holding it in the frame on screen.
    #[test]
    fn letters_go_at_once() {
        let mut fx = Rec::default();
        // Alpha:@8 a, main:@1 s, @2 d, @3 f, @4 g, work:@6 h, @7 j.
        assert!(menu().handle(&[Char('d')], &mut fx));
        assert!(menu().handle(&[Char('J')], &mut fx)); // capitals too
        assert!(menu().handle(&[Char('f')], &mut fx)); // linked: its first row on screen (main)
        assert_eq!(fx.calls, ["go @2 main", "go @7 work", "go @3 main"]);
        // j and k are letters now, not movement.
        let mut m = menu();
        assert!(!m.handle(&[Char('k')], &mut fx)); // nobody holds k
        assert_eq!(m.msg, "no agent on k");
        assert_eq!(m.sel, Some(ak("work", "@6")));
        // Digits do nothing.
        assert!(!m.handle(&keys("123"), &mut fx));
        assert_eq!(fx.calls.len(), 3);
        // Off screen (another filter): still the agent's.
        let mut m = Menu::new(data(), Tab::Parked);
        draw_all(&mut m);
        assert!(m.handle(&[Char('a')], &mut fx));
        assert_eq!(fx.calls[3], "go @8 Alpha");
    }

    /// The drawn-frame rule: a letter freed and handed to a newcomer between
    /// frames still means the window that held it when the frame was drawn;
    /// a letter that appeared since the draw does nothing until drawn.
    #[test]
    fn letters_resolve_against_the_drawn_frame() {
        let mut fx = Rec::default();
        let base = stored(fixture());
        let mut m = menu_of(data_of(&base));
        assert_eq!(m.drawn.letters[&'a'].win, "@8");
        let mut snap = base.clone();
        snap.windows.retain(|w| w.id != "@8"); // @8 closes...
        snap.windows.push(crate::tmux::Window::parse(&crate::tmux::tests::row("work", 9, "@30",
            &[("state", "running")])).unwrap()); // ...and a newcomer takes a
        m.set_data(data_of(&snap));
        assert_eq!(m.data.letter("@30"), Some('a'));
        assert!(m.handle(&[Char('a')], &mut fx));
        assert_eq!(fx.calls, ["go @8 Alpha"]); // the old window (gone: the real go refuses), never @30
        draw_all(&mut m);
        assert!(m.handle(&[Char('a')], &mut fx));
        assert_eq!(fx.calls[1], "go @30 work");
        // A failed go stays open and says why.
        fx.go_err = Some("that window is gone".into());
        assert!(!m.handle(&[Char('a')], &mut fx));
        assert_eq!(m.msg, "that window is gone");
    }

    /// ⌥W goes to the last agent; with none it closes; in every mode.
    #[test]
    fn option_w_is_last_agent() {
        let mut fx = Rec::default();
        let mut m = menu_of(with_mru("@2", "@7"));
        assert!(m.act(AltW, &mut fx));
        assert_eq!(fx.calls, ["go @7 work"]);
        let mut m = menu_of(with_mru("@6", "@7")); // cur is another agent: cur
        assert!(m.handle(&[Char('/'), Char('x'), AltW], &mut fx)); // from the search too
        assert_eq!(fx.calls[1], "go @6 work");
        let mut m = menu_of(with_mru("@6", ""));
        m.handle(&[Char(' '), Char('x')], &mut fx); // from a y/n too
        assert!(m.confirm.is_some());
        assert!(m.act(AltW, &mut fx));
        assert_eq!(fx.calls[2], "go @6 work");
        // None: closes, runs nothing.
        let mut fx = Rec::default();
        assert!(menu().act(AltW, &mut fx));
        assert!(menu_of(with_mru("@2", "@10")).act(AltW, &mut fx)); // parked: not an agent here
        assert!(fx.calls.is_empty());
        // A refused go stays open.
        fx.go_err = Some("nope".into());
        assert!(!menu_of(with_mru("@7", "")).act(AltW, &mut fx));
    }

    #[test]
    fn search_mode() {
        let mut fx = Rec::default();
        let mut m = menu();
        // Letters type in the search; the selection follows the filter.
        assert!(!m.handle(&keys("/kua"), &mut fx));
        assert!(m.searching && m.query == "kua" && fx.calls.is_empty());
        assert_eq!(m.sel, Some(ak("main", "@1")));
        // ⏎ goes to the selected match.
        assert!(m.act(Enter, &mut fx));
        assert_eq!(fx.calls, ["go @1 main"]);
        // Esc leaves and clears; a second Esc closes.
        let mut m = menu();
        m.handle(&keys("/kua"), &mut fx);
        assert!(!m.act(Esc, &mut fx));
        assert!(!m.searching && m.query.is_empty());
        assert_eq!(row_keys(&m), row_keys(&menu()));
        assert!(m.act(Esc, &mut fx));
        // Backspace edits; on an empty query it leaves the search.
        let mut m = menu();
        m.handle(&keys("/ab"), &mut fx);
        m.handle(&[Backspace, Backspace], &mut fx);
        assert!(m.searching && m.query.is_empty());
        m.act(Backspace, &mut fx);
        assert!(!m.searching);
        // Every printable types (space, digits, x, s, p); C-u clears; ↑↓ move.
        let mut m = menu();
        m.handle(&keys("/x s p1"), &mut fx);
        assert_eq!(m.query, "x s p1");
        m.act(CtrlU, &mut fx);
        assert!(m.query.is_empty() && m.searching && m.confirm.is_none());
        let before = m.sel.clone();
        m.act(Down, &mut fx);
        assert_ne!(m.sel, before);
        // ⏎ with no match does nothing.
        m.handle(&keys("zzz"), &mut fx);
        assert!(!m.act(Enter, &mut fx));
        assert_eq!(fx.calls.len(), 1);
        // C-c closes from the search.
        assert!(m.act(CtrlC, &mut fx));
    }

    /// Space opens the actions strip for the selected row; the next key runs
    /// one; Esc (or anything else) cancels.
    #[test]
    fn actions_strip() {
        let mut fx = Rec::default();
        let mut m = menu();
        m.act(Char(' '), &mut fx);
        assert_eq!(m.actions.as_ref().map(|t| t.win.as_str()), Some("@6"));
        assert!(!m.act(Char('p'), &mut fx)); // p: the preview strip
        assert!(m.preview && m.actions.is_none());
        m.handle(&keys(" p"), &mut fx);
        assert!(!m.preview);
        // Esc cancels (and does not close); a letter after Space is not a jump.
        m.act(Char(' '), &mut fx);
        assert!(!m.act(Esc, &mut fx));
        assert!(m.actions.is_none());
        m.act(Char(' '), &mut fx);
        assert!(!m.act(Char('a'), &mut fx));
        assert!(m.actions.is_none() && fx.calls.is_empty());
        // s / x open their y/n; y (once drawn) runs it.
        m.handle(&keys(" s"), &mut fx);
        assert_eq!(m.confirm.as_ref().map(|c| c.verb()), Some("park"));
        m.frame_shown();
        m.act(Char('y'), &mut fx);
        assert_eq!(fx.calls, ["park @6 work"]);
        m.handle(&keys(" x"), &mut fx);
        assert_eq!(m.confirm.as_ref().map(|c| c.verb()), Some("close"));
        m.act(Char('n'), &mut fx); // anything but y cancels
        assert!(m.confirm.is_none() && fx.calls.len() == 1);
        // r restarts the watcher; ⏎ goes.
        m.handle(&keys(" r"), &mut fx);
        assert_eq!((fx.calls[1].as_str(), m.msg.as_str()), ("watcher", "watcher restarted"));
        m.act(Char(' '), &mut fx);
        assert!(m.act(Enter, &mut fx));
        assert_eq!(fx.calls[2], "go @6 work");
        // A parked row: x discards, s is not offered.
        let mut m = Menu::new(data(), Tab::Parked);
        draw_all(&mut m);
        m.handle(&keys(" s"), &mut fx);
        assert!(m.confirm.is_none());
        m.handle(&keys(" x"), &mut fx);
        assert_eq!(m.confirm.as_ref().map(|c| c.verb()), Some("discard"));
    }

    /// A batch never confirms itself; a y before the prompt was drawn does
    /// nothing.
    #[test]
    fn a_batch_never_confirms_itself() {
        let mut fx = Rec::default();
        let mut m = menu();
        for burst in [" sy", " xy", " xYy"] {
            m.confirm = None;
            assert!(!m.handle(&keys(burst), &mut fx));
            assert!(fx.calls.is_empty(), "{burst}");
            assert!(m.confirm.is_some(), "{burst}");
        }
        assert!(!m.handle(&[Char('y')], &mut fx)); // still not on screen
        assert!(fx.calls.is_empty() && m.confirm.is_some());
        m.frame_shown();
        m.handle(&[Char('y')], &mut fx);
        assert_eq!(fx.calls, ["close @6 work"]);
        // A refusal says why, and how to ask again.
        fx.close_err = Some(TAB_MOVED.into());
        m.handle(&keys(" x"), &mut fx);
        m.frame_shown();
        m.act(Char('y'), &mut fx);
        assert_eq!(m.msg, "that tab moved · press ␣ x again");
    }

    /// ⏎ and Space never act on a row that is not on screen: they scroll
    /// back to it instead.
    #[test]
    fn keys_refuse_a_row_off_screen() {
        let mut fx = Rec::default();
        let mut m = menu();
        m.drawn.rows.retain(|t| t.key != ak("work", "@6"));
        m.follow = false;
        for k in [Enter, Char(' ')] {
            assert!(!m.act(k, &mut fx));
            assert!(m.follow, "{k:?} re-follows");
            m.follow = false;
        }
        assert!(fx.calls.is_empty() && m.actions.is_none());
    }

    #[test]
    fn moving_and_the_fold() {
        let mut fx = Rec::default();
        let mut m = menu();
        m.act(Home, &mut fx);
        assert_eq!(m.sel, Some(ak("Alpha", "@8")));
        let mut seen = vec![m.sel.clone().unwrap()];
        for _ in 0..12 {
            m.act(Down, &mut fx);
            seen.push(m.sel.clone().unwrap());
        }
        // Every row once, top to bottom, the fold last, then it stays.
        assert_eq!(seen[1], ak("main", "@1"));
        assert_eq!(seen[8], RowKey::Fold);
        assert!(seen[8..].iter().all(|k| *k == RowKey::Fold));
        m.act(CtrlP, &mut fx);
        assert_eq!(m.sel, Some(ak("work", "@3")));
        m.act(CtrlN, &mut fx);
        m.act(End, &mut fx);
        assert_eq!(m.sel, Some(RowKey::Fold));
        // Space on the fold does nothing; ⏎ opens Parked.
        m.act(Char(' '), &mut fx);
        assert!(m.actions.is_none());
        assert!(!m.act(Enter, &mut fx));
        assert_eq!(m.tab, Tab::Parked);
        assert_eq!(m.sel, Some(RowKey::Parked("@10".into())));
        assert!(fx.calls.is_empty());
        // ⇥ cycles back round to All.
        m.act(Key::Tab, &mut fx);
        assert_eq!(m.tab, Tab::All);
        m.act(BackTab, &mut fx);
        assert_eq!(m.tab, Tab::Parked);
    }

    #[test]
    fn going_and_closing() {
        let mut fx = Rec::default();
        let mut m = menu();
        assert!(m.act(Enter, &mut fx));
        assert_eq!(fx.calls, ["go @6 work"]);
        fx.go_err = Some("agent-jump.sh goto timed out".into());
        assert!(!m.act(Enter, &mut fx));
        assert!(m.msg.contains("timed out"));
        let mut fx = Rec::default();
        assert!(m.act(AltS, &mut fx) && m.act(AltX, &mut fx));
        assert_eq!(fx.calls, ["next", "back"]);
        // A parked row goes through its own session (unstash).
        let mut m = Menu::new(data(), Tab::Parked);
        draw_all(&mut m);
        m.act(End, &mut fx);
        assert!(m.act(Enter, &mut fx));
        assert_eq!(fx.calls[2], "go @14 stash");
        // Esc and C-c close; a y/n's Esc only cancels.
        assert!(menu().act(Esc, &mut NoEffects));
        assert!(menu().act(CtrlC, &mut NoEffects));
        let mut m = menu();
        m.handle(&keys(" x"), &mut NoEffects);
        assert!(!m.act(Esc, &mut NoEffects));
        assert!(m.confirm.is_none());
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

    /// The client moved under the open menu (CuaNotch's ⌥S, a tab click):
    /// close. The first frame, a missing client row, and the menu's own
    /// park/close of the client's window never count.
    #[test]
    fn closes_when_moved_out_from_under() {
        let at = |w: Option<&str>| {
            let mut d = data();
            d.view.cur_win = w.map(String::from);
            d
        };
        let mut m = menu_of(at(Some("@2"))); // the first frame only records
        assert!(!m.moved_away);
        for w in [None, Some(""), Some("@2")] {
            m.set_data(at(w)); // a missing row is transient; @2 is home
            assert!(!m.moved_away, "{w:?}");
        }
        m.set_data(at(Some("@6")));
        assert!(m.moved_away);
        // Opened while the row was missing: the first present one is home.
        let mut m = menu_of(at(None));
        m.set_data(at(Some("@6")));
        assert!(!m.moved_away);
        m.set_data(at(Some("@7")));
        assert!(m.moved_away);
        // The menu parked the client's own window: follow, don't close...
        let mut fx = Rec::default();
        let mut m = menu_of(at(Some("@6")));
        m.handle(&keys(" s"), &mut fx);
        m.frame_shown();
        m.act(Char('y'), &mut fx);
        assert_eq!(fx.calls, ["park @6 work"]);
        m.set_data(at(Some("@7")));
        assert!(!m.moved_away);
        // ...but a move after that is someone else's.
        m.set_data(at(Some("@3")));
        assert!(m.moved_away);
        // Parking ANOTHER window doesn't excuse a move off home.
        let mut fx = Rec::default();
        let mut m = menu_of(at(Some("@2")));
        m.handle(&keys(" s"), &mut fx);
        m.frame_shown();
        m.act(Char('y'), &mut fx);
        assert_eq!(fx.calls, ["park @6 work"]);
        m.set_data(at(Some("@7")));
        assert!(m.moved_away);
    }

    #[test]
    fn preview_wants_captures_only_when_on() {
        let mut m = menu();
        m.preview_rows = 9;
        assert_eq!(m.peek_want(), None); // off by default
        m.handle(&keys(" p"), &mut NoEffects);
        assert_eq!(m.peek_want().map(|w| (w.target.win, w.n)), Some(("@6".into(), 9)));
        m.preview_rows = 0; // no room for it
        assert_eq!(m.peek_want(), None);
    }
}
