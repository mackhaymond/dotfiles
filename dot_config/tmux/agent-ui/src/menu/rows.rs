//! What the menu's list shows: the filters, the search, and the rows.
//!
//! The menu opens on **All**, in a FIXED order: one group per space in THE
//! space order (the sidebar's AGENTS order: ⌘N, then by name), each group's
//! agents by window index. Nothing about a row's state or age moves it: an
//! agent that goes from working to needs-input stays where it is and only
//! changes glyph and colour (its title takes the needs colour and its reason
//! shows inline). There is no NEEDS YOU section and no duplicate row (only a
//! window linked into two spaces is listed in both). Parked tabs collapse to
//! ONE row at the end, `▪ N parked ⇥`, that opens the Parked filter. Rows
//! appear or disappear only when windows do.
//!
//! The other filters (cycled with ⇥ / ⇧⇥) narrow it: Active (working or
//! needing you, the old default), Needs you (agent-jump.sh's queue, in its
//! order), Working, Idle (the same fixed groups; paused agents, stopped by
//! the pause key, are listed here too, never under Needs you or Active),
//! Parked (every parked tab).
//! Every row passes the search query.
//!
//! A PENDING space (`@spaces` on: a claude in the hidden `pending` session
//! building space N, [`crate::view::Space::pending`]) is a group of its own
//! at N, `N · creating…`, its claude under it; Enter goes there as anywhere
//! ([`crate::actions::Actions::go`]: its popup, `space.sh pending-open`).
//!
//! Selectable rows carry a [`RowKey`], unique within the list: a window
//! linked into two sessions is listed under both, so the window id alone
//! cannot be the selection.

use super::letters::{self, KeyMemo, Write};
use crate::collate::Collator;
use crate::git::GitCache;
use crate::model::{Cat, Counts};
use crate::tmux::{Snapshot, HOLD};
use crate::view::{Agent, BuildArgs, Need, Parked, SlotMemo, ViewModel};
use std::collections::{HashMap, HashSet};

/// The list's filters, in ⇥-cycle order (All first: the default).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Tab {
    /// Every listed agent in fixed space/index order, parked folded (the default).
    All,
    /// Working ∪ needs you, in the same fixed order.
    Active,
    Needs,
    Working,
    Idle,
    Parked,
}

impl Tab {
    pub const ALL: [Tab; 6] = [Tab::All, Tab::Active, Tab::Needs, Tab::Working, Tab::Idle, Tab::Parked];

    /// The filter's name in the header.
    pub fn title(self) -> &'static str {
        match self {
            Tab::All => "All",
            Tab::Active => "Active",
            Tab::Needs => "Needs you",
            Tab::Working => "Working",
            Tab::Idle => "Idle",
            Tab::Parked => "Parked",
        }
    }

    /// `--tab all|active|needs|working|idle|parked`.
    pub fn parse(s: &str) -> Option<Tab> {
        Some(match s {
            "all" => Tab::All,
            "active" => Tab::Active,
            "needs" | "needs-you" => Tab::Needs,
            "working" => Tab::Working,
            "idle" => Tab::Idle,
            "parked" => Tab::Parked,
            _ => return None,
        })
    }

    /// ⇥ / ⇧⇥: the next (`d` = 1) or previous (-1) filter, wrapping.
    pub fn step(self, d: i32) -> Tab {
        let i = Tab::ALL.iter().position(|&t| t == self).unwrap_or(0) as i32;
        Tab::ALL[(i + d).rem_euclid(Tab::ALL.len() as i32) as usize]
    }
}

/// The NEEDS YOU entry of `a`'s window (by window id, so a linked window's
/// row in any session finds it): its reason shows inline on the row.
pub fn need_of<'a>(v: &'a ViewModel, a: &Agent) -> Option<&'a Need> {
    if !a.attention {
        return None; // the queue holds attention states only: skip the scan
    }
    v.needs.iter().find(|n| n.agent.window_id == a.window_id)
}

/// The Active filter's set: in flight (the Working bucket), needs you
/// (exactly the Needs you queue), or a prompt just answered (idle with
/// @agent_pending, its turn not resumed: [`crate::view::holds_slot`]).
pub fn is_active(v: &ViewModel, a: &Agent) -> bool {
    a.cat == Some(Cat::Working) || need_of(v, a).is_some() || a.answered
}

/// A selectable row's identity.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum RowKey {
    /// A Needs you filter row: the window.
    Need(String),
    /// A group row: (session, window).
    Agent(String, String),
    /// A parked row: the window.
    Parked(String),
    /// The All list's `▪ N parked ⇥` row.
    Fold,
}

impl RowKey {
    /// The window id the row points at ("" for the fold).
    pub fn window(&self) -> &str {
        match self {
            RowKey::Need(w) | RowKey::Parked(w) | RowKey::Agent(_, w) => w,
            RowKey::Fold => "",
        }
    }
}

/// One list row.
#[derive(Debug, Clone, PartialEq)]
pub enum Row {
    /// A space's rule line: name, branch, counts.
    Group { name: String, branch: String, counts: Counts, current: bool },
    /// A Needs you filter row (the queue's order).
    Need(Need),
    Agent(Agent),
    Parked(Parked),
    /// `▪ N parked ⇥` at the end of All: selectable, opens Parked.
    Fold(usize),
    /// "no agents running", "no matches", ...
    Empty(String),
}

impl Row {
    /// The selection key of a selectable row.
    pub fn key(&self) -> Option<RowKey> {
        match self {
            Row::Need(n) => Some(RowKey::Need(n.agent.window_id.clone())),
            Row::Agent(a) => Some(RowKey::Agent(a.session.clone(), a.window_id.clone())),
            Row::Parked(p) => Some(RowKey::Parked(p.agent.window_id.clone())),
            Row::Fold(_) => Some(RowKey::Fold),
            _ => None,
        }
    }

    /// The agent behind a window row.
    pub fn agent(&self) -> Option<&Agent> {
        match self {
            Row::Need(n) => Some(&n.agent),
            Row::Agent(a) => Some(a),
            Row::Parked(p) => Some(&p.agent),
            _ => None,
        }
    }

    /// What a key, a click or a confirm remembers of a window row: enough
    /// to act on it after the list was rebuilt. None for the fold.
    pub fn target(&self) -> Option<Target> {
        Some(Target::of(self.key()?, self.agent()?))
    }
}

/// A window row as it was on screen.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Target {
    pub key: RowKey,
    pub win: String,
    /// The session the row was listed under (`stash` for a parked row).
    pub session: String,
    pub index: u32,
    pub title: String,
    /// The window was pinned (`@agent_pinned`) as the row showed it: Space p
    /// unpins it then, else pins it.
    pub pinned: bool,
    /// A permission prompt was waiting on it as the row showed it
    /// ([`crate::answer::answerable`]): Space y / Space n answer it, and only
    /// that prompt ([`crate::answer::prompt_key`]).
    pub perm: Option<String>,
}

impl Target {
    /// `a` as listed under `key`.
    pub fn of(key: RowKey, a: &Agent) -> Target {
        Target {
            key,
            win: a.window_id.clone(),
            session: a.session.clone(),
            index: a.index,
            title: a.title.clone(),
            pinned: a.pinned.is_some(),
            perm: crate::answer::answerable(a).then(|| crate::answer::prompt_key(a)),
        }
    }

    pub fn parked(&self) -> bool {
        self.session == HOLD
    }

    pub fn place(&self) -> String {
        format!("{}:{}", self.session, self.index)
    }
}

/// Every LISTED agent row in display order (the All list's order): the
/// spaces' windows with an agent state, by space ([`crate::view::space_order`],
/// the sidebar's) then window index. A window linked into two spaces
/// appears under both.
pub fn listed(v: &ViewModel) -> impl Iterator<Item = &Agent> {
    v.ordered_spaces().into_iter().flat_map(|s| s.agents.iter()).filter(|a| !a.state.is_empty())
}

/// One refresh's data for the menu: the shared view model plus what only
/// the menu wants.
#[derive(Debug, Clone, PartialEq)]
pub struct Data {
    pub view: ViewModel,
    /// cwd → branch for every listed agent's path (the preview's meta line).
    pub branches: HashMap<String, String>,
    /// window id → its lifetime letter ([`letters::assign`]).
    pub letters: HashMap<String, char>,
    /// The window ⌥W goes to: the agent the client was on before this one
    /// (`@agent_mru_cur` / `@agent_mru_prev`; see [`last_agent`]).
    pub last: Option<String>,
    /// `@agent_key` writes this frame needs persisted. Taken (and sent) by
    /// the live [`super::Source`]; never sent by `--once` or tests.
    pub key_writes: Vec<Write>,
}

/// ⌥W's target: `@agent_mru_cur` when it is a listed agent other than the
/// client's current window, else `@agent_mru_prev` on the same terms, else
/// none. Ids that are not listed agents are ignored.
pub fn last_agent(listed: &HashSet<&str>, cur_win: Option<&str>, mru: (&str, &str)) -> Option<String> {
    let ok = |id: &str| !id.is_empty() && listed.contains(id) && Some(id) != cur_win;
    [mru.0, mru.1].into_iter().find(|id| ok(id)).map(String::from)
}

impl Data {
    /// A one-shot build (`--once`, tests): no slot memory, no key memory.
    pub fn build(snap: &Snapshot, client: Option<&str>, now: f64, coll: &Collator, git: Option<&mut GitCache>,
        watcher_age: Option<f64>) -> Data {
        Data::build_with(snap, client, now, coll, git, watcher_age, &mut SlotMemo::default(), None)
    }

    /// The view for `client`, WITHOUT git status (the menu never shows it)
    /// and without the event log. Branches come from `git` when given: pure
    /// file reads, cached, never waiting longer than its `branch_wait`.
    /// `keys`: the live menu's in-flight `@agent_key` writes ([`KeyMemo`]):
    /// with it, [`key_writes`](Self::key_writes) holds only what was not
    /// sent yet, and is recorded as sent.
    #[allow(clippy::too_many_arguments)]
    pub fn build_with(snap: &Snapshot, client: Option<&str>, now: f64, coll: &Collator, git: Option<&mut GitCache>,
        watcher_age: Option<f64>, memo: &mut SlotMemo, keys: Option<&mut KeyMemo>) -> Data {
        let mut view = ViewModel::build_with(
            snap,
            BuildArgs { client, now, collator: coll, git: None, log: Vec::new(), watcher_age },
            memo,
        );
        let mut branches = HashMap::new();
        if let Some(g) = git {
            // (A pending space has no folder yet: no branch.)
            for s in view.spaces.iter_mut().filter(|s| s.pending.is_none()) {
                s.branch = g.branch(&s.path, now);
            }
            let paths = view
                .needs
                .iter()
                .map(|n| &n.agent)
                .chain(view.spaces.iter().flat_map(|s| s.agents.iter()))
                .chain(view.parked.iter().map(|p| &p.agent))
                .map(|a| a.path.clone())
                .collect::<Vec<_>>();
            for p in paths {
                if let std::collections::hash_map::Entry::Vacant(e) = branches.entry(p) {
                    let b = g.branch(e.key(), now);
                    e.insert(b);
                }
            }
        }

        // Letters: the one shared assignment ([`letters::of_view`]), from each
        // window's @agent_key (or a write still in flight).
        // The pins' ⌥W digit (the row's pin mark says it).
        crate::view::Agent::set_pin_keys(&mut view, &crate::pins::pin_keys(snap));
        let a = letters::of_view(snap, &view, keys);
        let ids: HashSet<&str> = listed(&view).map(|a| a.window_id.as_str()).collect();
        let last = last_agent(&ids, view.cur_win.as_deref(), snap.mru());
        Data { view, branches, letters: a.letters, last, key_writes: a.writes }
    }

    /// The letter of `win`, if it holds one.
    pub fn letter(&self, win: &str) -> Option<char> {
        self.letters.get(win).copied()
    }

    /// `win`'s first place in display order, as a target: its All-list row
    /// for an agent ([`listed`], a home running one included); else, for a
    /// SHELL (lettered, never listed as a row here: the menu is the
    /// agents'), the shell in its space, so its letter still goes there
    /// ([`letters::lettered`]).
    pub fn first_row(&self, win: &str) -> Option<Target> {
        let a = listed(&self.view)
            .find(|a| a.window_id == win)
            .or_else(|| letters::lettered(&self.view).into_iter().find(|a| a.window_id == win))?;
        Some(Target::of(RowKey::Agent(a.session.clone(), a.window_id.clone()), a))
    }
}

/// Case-insensitive substring match over what the search searches: title,
/// session (and `session:index`), detail and path.
pub fn matches(a: &Agent, q: &str) -> bool {
    if q.is_empty() {
        return true;
    }
    let q = q.to_lowercase();
    [a.title.as_str(), a.session.as_str(), a.detail.as_str(), a.path.as_str(), &a.place()]
        .iter()
        .any(|s| s.to_lowercase().contains(&q))
}

/// The list for one filter and search query (see the module docs).
pub fn build_rows(d: &Data, tab: Tab, query: &str) -> Vec<Row> {
    let v = &d.view;
    let mut rows = Vec::new();
    let ok = |a: &Agent| matches(a, query);
    let keep = |a: &Agent| -> bool {
        ok(a)
            && match tab {
                // The client's own window is listed even without an agent:
                // the "here" row (a prompt that lands under the popup).
                Tab::All => true,
                Tab::Active => is_active(v, a),
                Tab::Working => a.cat == Some(Cat::Working),
                // Paused agents are stopped too: listed with the idle ones.
                Tab::Idle => matches!(a.cat, Some(Cat::Idle | Cat::Paused)),
                Tab::Needs | Tab::Parked => false,
            }
    };
    if matches!(tab, Tab::All | Tab::Active | Tab::Working | Tab::Idle) {
        // THE space order (⌘N, then by name: the sidebar's and the
        // letters'), never the attach recency, so switching sessions moves
        // nothing.
        for s in v.ordered_spaces() {
            let shown: Vec<&Agent> = s.agents.iter().filter(|a| keep(a)).collect();
            if shown.is_empty() {
                continue;
            }
            // (A pending space is named by its number: `3 · creating…`.)
            rows.push(Row::Group { name: s.label(), branch: s.branch.clone(), counts: s.counts, current: s.is_current });
            rows.extend(shown.into_iter().cloned().map(Row::Agent));
        }
    }
    if tab == Tab::Needs {
        rows.extend(v.needs.iter().filter(|n| ok(&n.agent)).cloned().map(Row::Need));
    }
    let parked: Vec<&Parked> = v.parked.iter().filter(|p| ok(&p.agent)).collect();
    if tab == Tab::All && !parked.is_empty() {
        rows.push(Row::Fold(parked.len()));
    }
    if tab == Tab::Parked {
        rows.extend(parked.into_iter().cloned().map(Row::Parked));
    }
    if rows.is_empty() {
        let why = if !query.is_empty() {
            "no matches"
        } else {
            match tab {
                Tab::All => "no agents running",
                Tab::Active => "nothing working or waiting",
                Tab::Needs => "nothing needs you",
                Tab::Working => "nothing working",
                Tab::Idle => "nothing idle",
                Tab::Parked => "nothing parked",
            }
        };
        rows.push(Row::Empty(why.into()));
    }
    rows
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::tmux::tests::row;

    /// A popup-sized fixture: needs in two sessions, a linked window, a
    /// plain window, and five parked tabs. The client (/dev/ttys999) is on
    /// main:2 (@2); work was attached more recently than Alpha.
    pub fn fixture() -> Snapshot {
        let rows = [
            row("main", 1, "@1", &[("state", "needs-input"), ("summary", "Kua Yu focus timing"),
                ("since", "100 needs-input"), ("detail_kind", "ask"), ("detail", "Which deck?"), ("kind", "claude"),
                ("path", "/nonexistent/kua"), ("last_attached", "50")]),
            row("main", 2, "@2", &[("state", "running"), ("summary", "Tmux Agent Sidebar"), ("active", "1"),
                ("since", "900 running"), ("last_attached", "50")]),
            row("main", 3, "@3", &[("state", "running"), ("summary", "Handy.app Speech"), ("workflow", "1"),
                ("since", "800 running"), ("last_attached", "50")]),
            row("main", 4, "@4", &[("state", "idle"), ("summary", "Commodities Job Tracker"), ("since", "10 idle"),
                ("last_attached", "50")]),
            row("main", 5, "@5", &[("last_attached", "50")]),
            row("work", 1, "@6", &[("state", "failed"), ("summary", "Island resize"), ("since", "300 failed"),
                ("active", "1"), ("last_attached", "40")]),
            row("work", 2, "@7", &[("state", "idle"), ("summary", "Census AX sweep"), ("last_attached", "40")]),
            row("work", 3, "@3", &[("state", "running"), ("summary", "Handy.app Speech"), ("workflow", "1"),
                ("last_attached", "40")]),
            row("Alpha", 1, "@8", &[("state", "idle"), ("summary", "Application link"), ("active", "1"),
                ("last_attached", "10")]),
            row("agents", 1, "@9", &[("state", "failed")]),
            row("stash", 1, "@10", &[("stash_label", "Pitch deck v2"), ("stash_ts", "500"), ("stash_origin", "bai")]),
            row("stash", 2, "@11", &[("stash_label", "Resume tailoring"), ("stash_session", "x"), ("stash_origin", "main")]),
            row("stash", 3, "@12", &[("stash_label", "ICS export spike"), ("stash_origin", "schedule")]),
            row("stash", 4, "@13", &[("stash_label", "Calendar sync"), ("stash_origin", "schedule")]),
            row("stash", 5, "@14", &[("stash_label", "Catalog sync"), ("state", "idle"), ("stash_origin", "web")]),
            // (@spaces on: shells are lettered too.)
            "\x1fclient\x1f/dev/ttys999\x1f@2\x1fmain\x1fon\x1f\x1f".into(),
        ];
        Snapshot::parse(&rows.join("\n"))
    }

    pub fn data() -> Data {
        data_of(&fixture())
    }

    pub fn data_of(snap: &Snapshot) -> Data {
        Data::build(snap, Some("/dev/ttys999"), 1000.0, &Collator::new("en_US.UTF-8"), None, Some(1.0))
    }

    /// `snap` with every listed agent's letter stored in @agent_key, as the
    /// live menu would have written it.
    pub fn stored(mut snap: Snapshot) -> Snapshot {
        let d = data_of(&snap);
        for w in &mut snap.windows {
            w.key = d.letter(&w.id).map(String::from).unwrap_or_default();
        }
        snap
    }

    /// The fixture with each (window, state) applied (every linked row).
    pub fn with_states(set: &[(&str, &str)]) -> Snapshot {
        let mut snap = fixture();
        for w in &mut snap.windows {
            if let Some((_, st)) = set.iter().find(|(id, _)| w.id == *id) {
                w.state = st.to_string();
            }
        }
        snap
    }

    pub fn shape(rows: &[Row]) -> Vec<String> {
        rows.iter()
            .map(|r| match r {
                Row::Group { name, .. } => format!("-- {name}"),
                Row::Need(n) => format!("need {}", n.agent.window_id),
                Row::Agent(a) => format!("{}:{}", a.session, a.window_id),
                Row::Parked(p) => format!("parked {}", p.agent.window_id),
                Row::Fold(k) => format!("▪{k}"),
                Row::Empty(s) => format!("({s})"),
            })
            .collect()
    }

    /// All: spaces by name (Alpha, main, work: the core's order, not the
    /// client's session first), rows by index, no NEEDS YOU section, parked
    /// folded into one row at the end.
    #[test]
    fn all_layout() {
        let d = data();
        let rows = build_rows(&d, Tab::All, "");
        assert_eq!(shape(&rows), [
            "-- Alpha", "Alpha:@8",
            "-- main", "main:@1", "main:@2", "main:@3", "main:@4",
            "-- work", "work:@6", "work:@7", "work:@3",
            "▪5",
        ]);
        // No duplicate rows: every key once (a linked window has two keys, one per space).
        let keys: Vec<RowKey> = rows.iter().filter_map(Row::key).collect();
        assert_eq!(keys.iter().collect::<HashSet<_>>().len(), keys.len());
        // The client moved and work attached last: the same list.
        let mut snap = fixture();
        for w in &mut snap.windows {
            w.last_attached = if w.session == "work" { 99 } else { 1 };
        }
        snap.clients[0].window_id = "@8".into();
        snap.clients[0].session = "Alpha".into();
        assert_eq!(shape(&build_rows(&data_of(&snap), Tab::All, "")), shape(&rows));
    }

    /// The point of the fixed order: a state flip moves nothing.
    #[test]
    fn all_order_survives_state_flips() {
        let before = shape(&build_rows(&data(), Tab::All, ""));
        for flips in [&[("@2", "needs-input")][..], &[("@1", "running"), ("@6", "idle")], &[("@4", "failed"), ("@8", "done")],
            &[("@3", "needs-input")]] {
            let d = data_of(&with_states(flips));
            assert_eq!(shape(&build_rows(&d, Tab::All, "")), before, "{flips:?}");
        }
        // Only windows appearing / disappearing change it.
        let mut snap = fixture();
        snap.windows.retain(|w| w.id != "@4");
        let after = shape(&build_rows(&data_of(&snap), Tab::All, ""));
        let mut want = before.clone();
        want.retain(|s| s != "main:@4");
        assert_eq!(after, want);
    }

    #[test]
    fn filters() {
        let d = data();
        assert_eq!(shape(&build_rows(&d, Tab::Needs, "")), ["need @6", "need @1"]); // agent-jump.sh order
        assert_eq!(shape(&build_rows(&d, Tab::Working, "")), ["-- main", "main:@2", "main:@3", "-- work", "work:@3"]);
        assert_eq!(shape(&build_rows(&d, Tab::Idle, "")), ["-- Alpha", "Alpha:@8", "-- main", "main:@4", "-- work", "work:@7"]);
        assert_eq!(shape(&build_rows(&d, Tab::Active, "")),
            ["-- main", "main:@1", "main:@2", "main:@3", "-- work", "work:@6", "work:@3"]);
        let parked = shape(&build_rows(&d, Tab::Parked, ""));
        assert_eq!(parked, ["parked @10", "parked @11", "parked @12", "parked @13", "parked @14"]);
    }

    #[test]
    fn search_filters_every_row() {
        let d = data();
        assert_eq!(shape(&build_rows(&d, Tab::All, "handy")), ["-- main", "main:@3", "-- work", "work:@3"]);
        assert_eq!(shape(&build_rows(&d, Tab::All, "WHICH deck")), ["-- main", "main:@1"]);
        assert_eq!(shape(&build_rows(&d, Tab::All, "work:2")), ["-- work", "work:@7"]);
        // Parked tabs that match fold into the count.
        assert_eq!(shape(&build_rows(&d, Tab::All, "sync")), ["▪2"]);
        assert_eq!(shape(&build_rows(&d, Tab::Parked, "sync")), ["parked @13", "parked @14"]);
        assert_eq!(shape(&build_rows(&d, Tab::All, "zzz")), ["(no matches)"]);
        assert_eq!(shape(&build_rows(&d, Tab::Needs, "zzz")), ["(no matches)"]);
    }

    #[test]
    fn tabs_cycle_and_parse() {
        assert_eq!(Tab::ALL.map(Tab::title), ["All", "Active", "Needs you", "Working", "Idle", "Parked"]);
        assert_eq!(Tab::All.step(1), Tab::Active);
        assert_eq!(Tab::All.step(-1), Tab::Parked);
        assert_eq!(Tab::Parked.step(1), Tab::All);
        for (s, t) in [("all", Tab::All), ("active", Tab::Active), ("needs", Tab::Needs), ("parked", Tab::Parked)] {
            assert_eq!(Tab::parse(s), Some(t));
        }
        assert_eq!(Tab::parse("bogus"), None);
    }

    #[test]
    fn empty_server() {
        let d = Data::build(&Snapshot::default(), None, 0.0, &Collator::new("C"), None, None);
        assert_eq!(shape(&build_rows(&d, Tab::All, "")), ["(no agents running)"]);
        assert_eq!(shape(&build_rows(&d, Tab::Parked, "")), ["(nothing parked)"]);
        assert!(d.letters.is_empty() && d.last.is_none() && d.key_writes.is_empty());
    }

    /// Letters, the agents first in display order (Alpha:@8 a, main:@1 s,
    /// @2 d, @3 f, @4 g, work:@6 h, @7 j), then the shells (main's @5 k);
    /// the linked @3 once; the hidden @9 none; parked tabs none.
    #[test]
    fn letters_from_the_snapshot() {
        let d = data();
        let got: Vec<(&str, char)> = ["@8", "@1", "@2", "@3", "@4", "@6", "@7", "@5"].iter().map(|w| (*w, d.letter(w).unwrap())).collect();
        assert_eq!(got, [("@8", 'a'), ("@1", 's'), ("@2", 'd'), ("@3", 'f'), ("@4", 'g'), ("@6", 'h'), ("@7", 'j'), ("@5", 'k')]);
        assert_eq!(d.letters.len(), 8);
        assert_eq!(d.key_writes.len(), 8);
        // The shell is lettered, never listed as a row: its letter goes to
        // it in its space all the same.
        assert!(!shape(&build_rows(&d, Tab::All, "")).iter().any(|r| r.contains("@5")));
        assert_eq!(d.first_row("@5").map(|t| (t.win, t.session)), Some(("@5".into(), "main".into())));
        // Stored: the same letters, nothing to write; a stored letter wins
        // over display order.
        let mut snap = fixture();
        for w in &mut snap.windows {
            w.key = d.letter(&w.id).map(String::from).unwrap_or_default();
        }
        for w in snap.windows.iter_mut().filter(|w| w.id == "@8") {
            w.key.clear(); // Alpha:@8 lost its key: a is taken by nobody now...
        }
        for w in snap.windows.iter_mut().filter(|w| w.id == "@7") {
            w.key = "a".into(); // ...and @7 claims a
        }
        let d2 = data_of(&snap);
        assert_eq!((d2.letter("@7"), d2.letter("@8"), d2.letter("@1")), (Some('a'), Some('j'), Some('s')));
        assert_eq!(d2.key_writes, [("@8".to_string(), Some('j'))]);
        // @spaces off (classic: no ⌥W table): shells are never lettered.
        let mut classic = fixture();
        classic.clients[0].spaces = String::new();
        let d3 = data_of(&classic);
        assert_eq!((d3.letter("@5"), d3.letters.len()), (None, 7));
    }

    /// The live memo: a write in flight is not sent again, and its letter
    /// holds even if the next snapshot does not show it yet.
    #[test]
    fn key_memo_through_build() {
        let snap = fixture();
        let mut keys = KeyMemo::default();
        let coll = Collator::new("en_US.UTF-8");
        let b = |s: &Snapshot, k: &mut KeyMemo| {
            Data::build_with(s, Some("/dev/ttys999"), 1000.0, &coll, None, None, &mut SlotMemo::default(), Some(k))
        };
        let d1 = b(&snap, &mut keys);
        assert_eq!(d1.key_writes.len(), 8);
        // tmux has not applied them; @8 (a) closes meanwhile: nobody moves.
        let mut s2 = snap.clone();
        s2.windows.retain(|w| w.id != "@8");
        let d2 = b(&s2, &mut keys);
        assert!(d2.key_writes.is_empty(), "{:?}", d2.key_writes);
        assert_eq!((d2.letter("@1"), d2.letter("@7")), (Some('s'), Some('j')));
        // A newcomer takes the freed a.
        s2.windows.push(crate::tmux::Window::parse(&row("work", 9, "@30", &[("state", "running")])).unwrap());
        let d3 = b(&s2, &mut keys);
        assert_eq!(d3.letter("@30"), Some('a'));
        assert_eq!(d3.key_writes, [("@30".to_string(), Some('a'))]);
    }

    #[test]
    fn last_agent_rules() {
        let listed: HashSet<&str> = ["@1", "@2", "@3"].into_iter().collect();
        let cur = Some("@2");
        assert_eq!(last_agent(&listed, cur, ("@1", "@3")), Some("@1".into()));
        assert_eq!(last_agent(&listed, cur, ("@2", "@3")), Some("@3".into())); // cur is where we are: prev
        assert_eq!(last_agent(&listed, cur, ("@9", "@3")), Some("@3".into())); // not listed: ignored
        assert_eq!(last_agent(&listed, cur, ("@2", "@2")), None);
        assert_eq!(last_agent(&listed, cur, ("@2", "@10")), None); // parked / gone
        assert_eq!(last_agent(&listed, cur, ("", "")), None);
        assert_eq!(last_agent(&listed, None, ("@2", "")), Some("@2".into()));
        // From the snapshot's global options.
        let mut snap = fixture();
        for w in &mut snap.windows {
            w.mru_cur = "@2".into(); // the client is on @2
            w.mru_prev = "@6".into();
        }
        assert_eq!(data_of(&snap).last.as_deref(), Some("@6"));
        for w in &mut snap.windows {
            w.mru_prev = "@10".into(); // parked: not listed
        }
        assert_eq!(data_of(&snap).last, None);
    }

    /// A pending space's claude (`@spaces` on) is a group of its own at its
    /// number, `N · creating…`, the claude under it (lettered, in display
    /// order there); its row's target is its real window and session, which
    /// Enter hands to `Actions::go` (its popup). Spaces off: no such group.
    #[test]
    fn a_pending_space_is_a_group_at_its_number() {
        let mut snap = fixture();
        let set = [("state", "running"), ("summary", "Rust CLI fizz"), ("pending_space", "2"), ("pending_request", "make fizz"),
            ("pending_color", "#f5c2e7"), ("space_hidden", "1")];
        snap.windows.push(crate::tmux::Window::parse(&row("pending", 1, "@40", &set)).unwrap());
        for w in snap.windows.iter_mut().filter(|w| w.session == "main") {
            w.space_index = Some(1);
        }
        for w in snap.windows.iter_mut().filter(|w| w.session == "work") {
            w.space_index = Some(3);
        }
        let d = data_of(&snap);
        assert_eq!(shape(&build_rows(&d, Tab::All, "")), [
            "-- main", "main:@1", "main:@2", "main:@3", "main:@4",
            "-- 2 · creating…", "pending:@40",
            "-- work", "work:@6", "work:@7", "work:@3",
            "-- Alpha", "Alpha:@8",
            "▪5",
        ]);
        assert_eq!(shape(&build_rows(&d, Tab::Working, "fizz")), ["-- 2 · creating…", "pending:@40"]);
        // Its letter: after main's agents (a s d f), before work's.
        assert_eq!(d.letter("@40"), Some('g'));
        let t = build_rows(&d, Tab::All, "").iter().find(|r| r.agent().is_some_and(|a| a.window_id == "@40")).and_then(Row::target).unwrap();
        assert_eq!((t.win.as_str(), t.session.as_str()), ("@40", "pending"));
        // @spaces off: the hidden `pending` session as before, by name.
        snap.clients[0].spaces = String::new();
        let rows = shape(&build_rows(&data_of(&snap), Tab::All, ""));
        assert!(rows.contains(&"-- pending".to_string()) && !rows.iter().any(|r| r.contains("creating")), "{rows:?}");
    }

    /// A HOME window running an agent is listed (the menu never hides
    /// homes) but never lettered: its row off screen (another filter, a
    /// search), ⌥W "last" still finds it by its All-list row.
    #[test]
    fn first_row_finds_a_home_running_an_agent() {
        let mut snap = fixture();
        for w in snap.windows.iter_mut().filter(|w| w.id == "@6") {
            w.home = true; // work:1, failed
        }
        let d = data_of(&snap);
        assert_eq!(d.letter("@6"), None);
        let t = d.first_row("@6").expect("its All-list row");
        assert_eq!((t.key, t.session), (RowKey::Agent("work".into(), "@6".into()), "work".to_string()));
        // Off screen: the Parked tab draws no agent row, the target holds.
        let drawn = super::super::state::Drawn::of(&{ let mut d = d.clone(); d.last = Some("@6".into()); d }, Vec::new());
        assert_eq!(drawn.last.map(|t| t.win), Some("@6".into()));
    }
}
