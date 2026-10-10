//! The picker's state machine: query, selection, the create confirm. No
//! I/O: [`Picker::handle`] returns what to do ([`Outcome`]) and the caller
//! does it.
//!
//! Keys handled before the rows are loaded are queued, in order, and
//! replayed by [`Picker::load`]: an Enter typed ahead acts on the filtered
//! list, never on an empty one.

use super::keys::Key;
use super::rows::{self, Score, Sessions};

/// What the picker decided.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    /// Close, doing nothing.
    Close,
    /// `switch-client -t =<name>`.
    Switch(String),
    /// `new-session -d -s <name> -c ~`, then switch to it.
    Create(String),
}

/// The footer's mode.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Mode {
    List,
    /// "Create and go to [name]? Y/n".
    Confirm(String),
}

#[derive(Debug, Clone)]
pub struct Picker {
    pub data: Sessions,
    pub query: String,
    /// The matching rows, best first: (index into data.rows, its score).
    pub matches: Vec<(usize, Score)>,
    /// Index into `matches`.
    pub sel: usize,
    /// The first match drawn (scroll).
    pub top: usize,
    pub mode: Mode,
    /// An inline message (the invalid-name one), until the next key.
    pub msg: Option<String>,
    pub loaded: bool,
    /// Keys that arrived before [`load`](Self::load).
    pub early: Vec<Key>,
}

impl Default for Picker {
    fn default() -> Self {
        Picker::new()
    }
}

impl Picker {
    /// A picker with no rows yet: keys are queued until [`load`](Self::load).
    pub fn new() -> Picker {
        Picker { data: Sessions::default(), query: String::new(), matches: Vec::new(), sel: 0, top: 0, mode: Mode::List,
            msg: None, loaded: false, early: Vec::new() }
    }

    /// A loaded picker.
    pub fn with(data: Sessions) -> Picker {
        let mut p = Picker::new();
        p.loaded = true;
        p.set_data(data);
        p
    }

    /// The first rows arrive: replay the keys typed meanwhile against them.
    pub fn load(&mut self, data: Sessions) -> Option<Outcome> {
        self.loaded = true;
        self.set_data(data);
        let early = std::mem::take(&mut self.early);
        self.handle(&early)
    }

    /// New rows (a refresh): the selection stays on its session by name.
    pub fn set_data(&mut self, data: Sessions) {
        let keep = self.selected().map(|r| r.name.clone());
        let top = self.top;
        self.data = data;
        self.refilter();
        if let Some(name) = keep {
            if let Some(i) = self.matches.iter().position(|(r, _)| self.data.rows[*r].name == name) {
                self.sel = i;
                // And the view stays where it was (render clamps it).
                self.top = top;
            }
        }
    }

    /// Set the query outright (`--once --query`): the first match is selected.
    pub fn set_query(&mut self, q: &str) {
        self.query = q.to_string();
        self.refilter();
    }

    fn refilter(&mut self) {
        self.matches = rows::filter(&self.data.rows, self.query.trim());
        self.sel = 0;
        self.top = 0;
    }

    /// The selected row.
    pub fn selected(&self) -> Option<&rows::SessionRow> {
        self.matches.get(self.sel).map(|(i, _)| &self.data.rows[*i])
    }

    /// The trimmed query (what Enter acts on), as the script trims it.
    pub fn target(&self) -> &str {
        self.query.trim()
    }

    /// Keys in order; the first that ends the picker wins (the rest are dropped).
    pub fn handle(&mut self, keys: &[Key]) -> Option<Outcome> {
        for &k in keys {
            if !self.loaded {
                self.early.push(k);
                continue;
            }
            if let Some(o) = self.key(k) {
                return Some(o);
            }
        }
        None
    }

    fn key(&mut self, k: Key) -> Option<Outcome> {
        if let Mode::Confirm(name) = &self.mode {
            let name = name.clone();
            self.mode = Mode::List;
            return match k {
                Key::Enter | Key::Char('y') | Key::Char('Y') => Some(Outcome::Create(name)),
                _ => None, // anything else: back to the list
            };
        }
        if k != Key::Other {
            self.msg = None;
        }
        match k {
            Key::Esc | Key::CtrlC => return Some(Outcome::Close),
            Key::Enter => return self.enter(),
            Key::Up => self.sel = self.sel.saturating_sub(1),
            Key::Down => {
                if self.sel + 1 < self.matches.len() {
                    self.sel += 1;
                }
            }
            Key::Char(c) => {
                self.query.push(c);
                self.refilter();
            }
            Key::Backspace => {
                if self.query.pop().is_some() {
                    self.refilter();
                }
            }
            Key::CtrlU => {
                if !self.query.is_empty() {
                    self.query.clear();
                    self.refilter();
                }
            }
            Key::CtrlW => {
                // unix-word-rubout: trailing blanks, then back to the previous blank.
                let t = self.query.trim_end_matches(char::is_whitespace);
                let cut = t.rfind(char::is_whitespace).map_or(0, |i| i + t[i..].chars().next().map_or(1, char::len_utf8));
                if cut != self.query.len() {
                    self.query.truncate(cut);
                    self.refilter();
                }
            }
            Key::Other => {}
        }
        None
    }

    /// The script's order: a match wins; else the typed name — empty
    /// closes, `scratch` closes, an invalid name says so, an existing
    /// session is switched to, a new one is offered.
    fn enter(&mut self) -> Option<Outcome> {
        if let Some(r) = self.selected() {
            return Some(Outcome::Switch(r.name.clone()));
        }
        let t = self.target().to_string();
        if t.is_empty() || t == "scratch" {
            return Some(Outcome::Close);
        }
        if !rows::valid_name(&t) {
            self.msg = Some(rows::invalid_message(&t));
            return None;
        }
        if self.data.exists(&t) {
            return Some(Outcome::Switch(t));
        }
        self.mode = Mode::Confirm(t);
        None
    }
}

#[cfg(test)]
mod tests {
    use super::super::rows::tests::fixture;
    use super::super::rows::build;
    use super::*;
    use crate::collate::Collator;
    use Key::*;

    fn data() -> Sessions {
        build(&fixture(), Some("/dev/ttys001"), &Collator::new("C"))
    }

    fn typed(s: &str) -> Vec<Key> {
        s.chars().map(Char).collect()
    }

    fn sel(p: &Picker) -> Option<&str> {
        p.selected().map(|r| r.name.as_str())
    }

    #[test]
    fn filter_and_go() {
        let mut p = Picker::with(data());
        assert_eq!(sel(&p), Some("alpha")); // MRU first
        assert_eq!(p.handle(&typed("wo")), None);
        assert_eq!(sel(&p), Some("work")); // prefix, MRU: work before workshop
        assert_eq!(p.handle(&[Down]), None);
        assert_eq!(sel(&p), Some("workshop"));
        assert_eq!(p.handle(&[Down, Down]), None);
        assert_eq!(sel(&p), Some("workshop")); // clamped (wiki is not "wo")
        assert_eq!(p.handle(&[Up, Up]), None);
        assert_eq!(sel(&p), Some("work"));
        assert_eq!(p.handle(&[Char('r'), Enter]), Some(Outcome::Switch("work".into())));
    }

    #[test]
    fn editing_keys() {
        let mut p = Picker::with(data());
        p.handle(&typed("foo bar  "));
        p.handle(&[CtrlW]);
        assert_eq!(p.query, "foo ");
        p.handle(&[CtrlW]);
        assert_eq!(p.query, "");
        p.handle(&typed("wsx"));
        assert!(p.matches.is_empty());
        p.handle(&[Backspace]);
        assert_eq!(sel(&p), Some("workshop")); // "ws": subsequence
        p.handle(&[CtrlU]);
        assert_eq!((p.query.as_str(), sel(&p)), ("", Some("alpha")));
        // Backspace on an empty query does nothing (it does not close).
        assert_eq!(p.handle(&[Backspace]), None);
        assert_eq!(p.handle(&[Esc]), Some(Outcome::Close));
        assert_eq!(Picker::with(data()).handle(&[CtrlC]), Some(Outcome::Close));
    }

    #[test]
    fn create_confirm() {
        let mut p = Picker::with(data());
        assert_eq!(p.handle(&typed("newproj")), None);
        assert!(p.matches.is_empty());
        assert_eq!(p.handle(&[Enter]), None);
        assert_eq!(p.mode, Mode::Confirm("newproj".into()));
        // Anything else cancels back to the list (and is not typed).
        assert_eq!(p.handle(&[Char('n')]), None);
        assert_eq!((p.mode.clone(), p.query.as_str()), (Mode::List, "newproj"));
        p.handle(&[Enter]);
        assert_eq!(p.handle(&[Esc]), None); // Esc cancels the confirm, not the picker
        assert_eq!(p.mode, Mode::List);
        for yes in [Enter, Char('y'), Char('Y')] {
            p.handle(&[Enter]);
            assert_eq!(p.handle(&[yes]), Some(Outcome::Create("newproj".into())));
        }
        // Surrounding blanks are trimmed, as the script trims the query.
        let mut p = Picker::with(data());
        p.handle(&typed(" zz "));
        p.handle(&[Enter]);
        assert_eq!(p.mode, Mode::Confirm("zz".into()));
    }

    #[test]
    fn invalid_name_and_scratch() {
        let mut p = Picker::with(data());
        p.handle(&typed("a.b"));
        assert_eq!(p.handle(&[Enter]), None);
        assert_eq!(p.msg.as_deref(), Some("Invalid session name (allowed: A-Z a-z 0-9 _ -): a.b"));
        assert_eq!(p.mode, Mode::List);
        p.handle(&[Backspace]);
        assert_eq!(p.msg, None); // the next key clears it
        // scratch is never a target, even typed in full.
        let mut p = Picker::with(data());
        p.handle(&typed("scratch"));
        assert!(p.matches.is_empty());
        assert_eq!(p.handle(&[Enter]), Some(Outcome::Close));
        // Another hidden session typed in full: it exists, so it is switched to.
        let mut p = Picker::with(data());
        p.handle(&typed("stash"));
        assert_eq!(p.handle(&[Enter]), Some(Outcome::Switch("stash".into())));
        // So is the client's own session.
        let mut p = Picker::with(data());
        p.handle(&typed("home"));
        assert_eq!(p.handle(&[Enter]), Some(Outcome::Switch("home".into())));
    }

    #[test]
    fn empty_enter() {
        // No rows, empty query: Enter closes.
        let mut p = Picker::with(Sessions::default());
        assert_eq!(p.handle(&[Enter]), Some(Outcome::Close));
        // Rows, empty query: the top one.
        let mut p = Picker::with(data());
        assert_eq!(p.handle(&[Enter]), Some(Outcome::Switch("alpha".into())));
    }

    #[test]
    fn typed_ahead_keys_wait_for_the_rows() {
        let mut p = Picker::new();
        // Typed before the snapshot landed, Enter included.
        assert_eq!(p.handle(&typed("shop")), None);
        assert_eq!(p.handle(&[Enter, Char('x')]), None);
        assert_eq!(p.early.len(), 6);
        assert_eq!(p.load(data()), Some(Outcome::Switch("workshop".into())));
        // An early Down then Enter.
        let mut p = Picker::new();
        p.handle(&typed("w"));
        p.handle(&[Down, Enter]);
        assert_eq!(p.load(data()), Some(Outcome::Switch("workshop".into())));
        // An early Enter on a name that matches nothing: the confirm, not a close.
        let mut p = Picker::new();
        p.handle(&typed("brandnew"));
        p.handle(&[Enter]);
        assert_eq!(p.load(data()), None);
        assert_eq!(p.mode, Mode::Confirm("brandnew".into()));
        assert_eq!(p.handle(&[Char('y')]), Some(Outcome::Create("brandnew".into())));
        // Keys with no Enter: applied, and the picker stays open.
        let mut p = Picker::new();
        p.handle(&typed("wi"));
        assert_eq!(p.load(data()), None);
        assert_eq!(sel(&p), Some("wiki"));
    }

    #[test]
    fn refresh_keeps_the_selection_by_name() {
        let mut p = Picker::with(data());
        p.handle(&typed("w"));
        p.handle(&[Down]);
        assert_eq!(sel(&p), Some("workshop"));
        let mut d = data();
        d.rows.insert(0, rows::SessionRow { name: "wow".into(), last: 999, rollup: None });
        p.set_data(d);
        assert_eq!(sel(&p), Some("workshop"));
        // The scroll position survives a refresh too (no jump every second).
        p.top = 1;
        p.set_data(p.data.clone());
        assert_eq!((sel(&p), p.top), (Some("workshop"), 1));
        // Gone: the first match.
        let mut d = data();
        d.rows.retain(|r| r.name != "workshop");
        p.set_data(d);
        assert_eq!(sel(&p), Some("work"));
    }
}
