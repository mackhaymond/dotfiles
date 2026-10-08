//! What the menu's list shows: the tabs, the search filter, and the rows
//! (agent-roster.py `build_items` / `selectable` / `number_items`, reshaped
//! for the tabbed layout).
//!
//! The All tab is the Python popup's list: NEEDS YOU in agent-jump.sh order,
//! then one group per session, the client's session first and the rest most
//! recently attached first (the popup's order, not the sidebar's by-name
//! one), then the parked tabs. The other tabs narrow it: Needs you (the
//! queue alone), Working and Idle (the groups' agents in that bucket), and
//! Parked (every parked tab; the All tab shows only the first few).
//!
//! Selectable rows carry a positional [`RowKey`], unique within the list:
//! the same window can be listed twice (a NEEDS YOU row and its group row,
//! or a window linked into two sessions), so the window id alone cannot be
//! the selection (the Python's `item_key`).

use crate::collate::Collator;
use crate::git::GitCache;
use crate::model::{Cat, Counts};
use crate::tmux::{Snapshot, HOLD};
use crate::view::{Agent, BuildArgs, Need, Parked, ViewModel};
use std::collections::HashMap;

/// The tab row's filters, in display (and Tab-cycle) order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Tab {
    All,
    Needs,
    Working,
    Idle,
    Parked,
}

impl Tab {
    pub const ALL: [Tab; 5] = [Tab::All, Tab::Needs, Tab::Working, Tab::Idle, Tab::Parked];

    /// The chip's words.
    pub fn title(self) -> &'static str {
        match self {
            Tab::All => "All",
            Tab::Needs => "Needs you",
            Tab::Working => "Working",
            Tab::Idle => "Idle",
            Tab::Parked => "Parked",
        }
    }

    /// The colour of the chip's count when the chip is not active.
    pub fn hue(self) -> &'static str {
        match self {
            Tab::All => "text",
            Tab::Needs => "yellow",
            Tab::Working => "blue",
            Tab::Idle | Tab::Parked => "overlay",
        }
    }

    /// `--tab parked|needs|working|idle|all`.
    pub fn parse(s: &str) -> Option<Tab> {
        Some(match s {
            "all" => Tab::All,
            "needs" | "needs-you" => Tab::Needs,
            "working" => Tab::Working,
            "idle" => Tab::Idle,
            "parked" => Tab::Parked,
            _ => return None,
        })
    }

    /// Tab / Shift-Tab: the next (`d` = 1) or previous (-1) chip, wrapping.
    pub fn step(self, d: i32) -> Tab {
        let i = Tab::ALL.iter().position(|&t| t == self).unwrap_or(0) as i32;
        Tab::ALL[(i + d).rem_euclid(Tab::ALL.len() as i32) as usize]
    }

    /// The chip's count.
    pub fn count(self, v: &ViewModel) -> usize {
        match self {
            Tab::All => v.counts.total(),
            Tab::Needs => v.needs.len(),
            Tab::Working => v.counts.get(Cat::Working),
            Tab::Idle => v.counts.get(Cat::Idle),
            Tab::Parked => v.parked.len(),
        }
    }
}

/// A selectable row's identity (agent-roster.py `item_key`).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum RowKey {
    /// A NEEDS YOU row: the window.
    Need(String),
    /// A group row: (session, window).
    Agent(String, String),
    /// A parked row: the window.
    Parked(String),
}

impl RowKey {
    /// The window id the row points at (agent-roster.py `key_window`).
    pub fn window(&self) -> &str {
        match self {
            RowKey::Need(w) | RowKey::Parked(w) | RowKey::Agent(_, w) => w,
        }
    }
}

/// One list row.
#[derive(Debug, Clone, PartialEq)]
pub enum Row {
    /// "NEEDS YOU" / "PARKED 5": a bold section title; `rule` draws a ─ fill.
    Section { title: String, color: &'static str, rule: bool },
    /// A space's rule line: name, branch, counts.
    Group { name: String, branch: String, counts: Counts, current: bool },
    Need(Need),
    Agent(Agent),
    Parked(Parked),
    /// "+k more · ⇥ Parked" under the All tab's parked rows (clickable).
    More(usize),
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
            _ => None,
        }
    }

    /// The agent behind a selectable row.
    pub fn agent(&self) -> Option<&Agent> {
        match self {
            Row::Need(n) => Some(&n.agent),
            Row::Agent(a) => Some(a),
            Row::Parked(p) => Some(&p.agent),
            _ => None,
        }
    }

    /// What a number, a click or a confirm remembers of a row: enough to
    /// act on it after the list was rebuilt (the Python kept the item dict).
    pub fn target(&self) -> Option<Target> {
        let a = self.agent()?;
        Some(Target {
            key: self.key()?,
            win: a.window_id.clone(),
            session: a.session.clone(),
            index: a.index,
            title: a.title.clone(),
        })
    }
}

/// A row as it was on screen.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Target {
    pub key: RowKey,
    pub win: String,
    /// The session the row was listed under (`stash` for a parked row).
    pub session: String,
    pub index: u32,
    pub title: String,
}

impl Target {
    pub fn parked(&self) -> bool {
        self.session == HOLD
    }

    pub fn place(&self) -> String {
        format!("{}:{}", self.session, self.index)
    }
}

/// How many parked rows the All tab shows before "+k more" (all of them
/// when only one more would be hidden).
pub const PARKED_PREVIEW: usize = 3;

/// One refresh's data for the menu: the shared view model plus the two
/// things only the menu wants.
#[derive(Debug, Clone, PartialEq)]
pub struct Data {
    pub view: ViewModel,
    /// Indices into `view.spaces` in the menu's group order:
    /// agent-roster.py `build_items` (the client's session first, then the
    /// most recently attached, then by name).
    pub order: Vec<usize>,
    /// cwd → branch for every listed agent's path (the preview's meta line).
    pub branches: HashMap<String, String>,
}

impl Data {
    /// The view for `client`, WITHOUT git status (the menu never shows it,
    /// so it never forks `git status`) and without the event log (it never
    /// shows that either). Branches come from `git` when given: pure file
    /// reads, cached, never waiting longer than its `branch_wait`.
    pub fn build(snap: &Snapshot, client: Option<&str>, now: f64, coll: &Collator, git: Option<&mut GitCache>,
        watcher_age: Option<f64>) -> Data {
        let mut view =
            ViewModel::build(snap, BuildArgs { client, now, collator: coll, git: None, log: Vec::new(), watcher_age });
        let mut branches = HashMap::new();
        if let Some(g) = git {
            for s in &mut view.spaces {
                s.branch = g.branch(&s.path, now);
            }
            let paths = view
                .needs
                .iter()
                .map(|n| &n.agent)
                .chain(view.spaces.iter().flat_map(|s| s.agents.iter().chain(s.plain.iter())))
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
        let mut last: HashMap<&str, i64> = HashMap::new();
        for w in &snap.windows {
            let e = last.entry(w.session.as_str()).or_insert(0);
            *e = (*e).max(w.last_attached);
        }
        let cur = view.cur_session.clone();
        let mut order: Vec<usize> = (0..view.spaces.len()).collect();
        order.sort_by(|&a, &b| {
            let (sa, sb) = (&view.spaces[a], &view.spaces[b]);
            let key = |s: &str| (Some(s) != cur.as_deref(), -last.get(s).copied().unwrap_or(0));
            key(&sa.name).cmp(&key(&sb.name)).then_with(|| sa.name.cmp(&sb.name))
        });
        Data { view, order, branches }
    }
}

/// Case-insensitive substring match over what the search box searches:
/// title, session (and `session:index`), detail and path.
pub fn matches(a: &Agent, q: &str) -> bool {
    if q.is_empty() {
        return true;
    }
    let q = q.to_lowercase();
    [a.title.as_str(), a.session.as_str(), a.detail.as_str(), a.path.as_str(), &a.place()]
        .iter()
        .any(|s| s.to_lowercase().contains(&q))
}

/// The list for one tab: agent-roster.py `build_items`, by tab.
///
/// `show_all` (the Python's `a`) adds each space's plain windows to its
/// group on the All tab. Every row passes the search `query`.
pub fn build_rows(d: &Data, tab: Tab, query: &str, show_all: bool) -> Vec<Row> {
    let v = &d.view;
    let mut rows = Vec::new();
    let ok = |a: &Agent| matches(a, query);

    if matches!(tab, Tab::All | Tab::Needs) {
        let needs: Vec<&Need> = v.needs.iter().filter(|n| ok(&n.agent)).collect();
        if !needs.is_empty() {
            rows.push(Row::Section { title: "NEEDS YOU".into(), color: "yellow", rule: false });
            rows.extend(needs.into_iter().cloned().map(Row::Need));
        }
    }

    if matches!(tab, Tab::All | Tab::Working | Tab::Idle) {
        for &i in &d.order {
            let s = &v.spaces[i];
            let mut shown: Vec<&Agent> = match tab {
                Tab::Working => s.agents.iter().filter(|a| a.cat == Some(Cat::Working)).collect(),
                Tab::Idle => s.agents.iter().filter(|a| a.cat == Some(Cat::Idle)).collect(),
                _ if show_all => s.agents.iter().chain(s.plain.iter()).collect(),
                _ => s.agents.iter().collect(),
            };
            shown.retain(|a| ok(a));
            shown.sort_by_key(|a| a.index);
            if shown.is_empty() {
                continue;
            }
            rows.push(Row::Group { name: s.name.clone(), branch: s.branch.clone(), counts: s.counts, current: s.is_current });
            rows.extend(shown.into_iter().cloned().map(Row::Agent));
        }
    }

    if matches!(tab, Tab::All | Tab::Parked) {
        let parked: Vec<&Parked> = v.parked.iter().filter(|p| ok(&p.agent)).collect();
        if !parked.is_empty() {
            rows.push(Row::Section { title: format!("PARKED {}", parked.len()), color: "overlay", rule: true });
            let cap = if tab == Tab::All && parked.len() > PARKED_PREVIEW + 1 { PARKED_PREVIEW } else { parked.len() };
            rows.extend(parked[..cap].iter().map(|p| Row::Parked((*p).clone())));
            if cap < parked.len() {
                rows.push(Row::More(parked.len() - cap));
            }
        }
    }

    if rows.is_empty() {
        let why = if !query.is_empty() {
            "no matches"
        } else {
            match tab {
                Tab::All => "no agents running",
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

/// agent-roster.py `number_items`: {row position: label} for every window row.
pub fn number_rows(rows: &[Row]) -> HashMap<usize, String> {
    crate::hotkeys::number_items(rows, |r| r.key().is_some())
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
            "\x1fclient\x1f/dev/ttys999\x1f@2\x1fmain".into(),
        ];
        Snapshot::parse(&rows.join("\n"))
    }

    pub fn data() -> Data {
        Data::build(&fixture(), Some("/dev/ttys999"), 1000.0, &Collator::new("en_US.UTF-8"), None, Some(1.0))
    }

    fn shape(rows: &[Row]) -> Vec<String> {
        rows.iter()
            .map(|r| match r {
                Row::Section { title, .. } => format!("# {title}"),
                Row::Group { name, .. } => format!("-- {name}"),
                Row::Need(n) => format!("need {}", n.agent.window_id),
                Row::Agent(a) => format!("{}:{}", a.session, a.window_id),
                Row::Parked(p) => format!("parked {}", p.agent.window_id),
                Row::More(k) => format!("+{k}"),
                Row::Empty(s) => format!("({s})"),
            })
            .collect()
    }

    #[test]
    fn all_tab_layout() {
        let d = data();
        // The client's session first, then most recently attached.
        let names: Vec<&str> = d.order.iter().map(|&i| d.view.spaces[i].name.as_str()).collect();
        assert_eq!(names, ["main", "work", "Alpha"]);
        let rows = build_rows(&d, Tab::All, "", false);
        assert_eq!(shape(&rows), [
            "# NEEDS YOU", "need @6", "need @1",
            "-- main", "main:@1", "main:@2", "main:@3", "main:@4",
            "-- work", "work:@6", "work:@7", "work:@3",
            "-- Alpha", "Alpha:@8",
            "# PARKED 5", "parked @10", "parked @11", "parked @12", "+2",
        ]);
        // Labels: every window row, in order; headers get none.
        let labels = number_rows(&rows);
        assert_eq!(labels.len(), 13);
        assert_eq!(labels[&1], "1");
        assert_eq!(labels[&17], "04"); // the 13th window row
        assert!(!labels.contains_key(&0) && !labels.contains_key(&18));
        // `a`: the plain window joins its group, in index order.
        let rows = build_rows(&d, Tab::All, "", true);
        assert!(shape(&rows).contains(&"main:@5".to_string()));
    }

    #[test]
    fn tab_filters() {
        let d = data();
        assert_eq!(shape(&build_rows(&d, Tab::Needs, "", false)), ["# NEEDS YOU", "need @6", "need @1"]);
        assert_eq!(shape(&build_rows(&d, Tab::Working, "", false)),
            ["-- main", "main:@2", "main:@3", "-- work", "work:@3"]);
        assert_eq!(shape(&build_rows(&d, Tab::Idle, "", false)),
            ["-- main", "main:@4", "-- work", "work:@7", "-- Alpha", "Alpha:@8"]);
        let parked = shape(&build_rows(&d, Tab::Parked, "", false));
        assert_eq!(parked.len(), 6); // header + all five, no "+k more"
        // Counts on the chips.
        let c: Vec<usize> = Tab::ALL.iter().map(|t| t.count(&d.view)).collect();
        assert_eq!(c, [7, 2, 2, 3, 5]); // @3 once though linked; agents session hidden
    }

    #[test]
    fn search_filters_every_section() {
        let d = data();
        // Case-insensitive, over title...
        assert_eq!(shape(&build_rows(&d, Tab::All, "handy", false)),
            ["-- main", "main:@3", "-- work", "work:@3"]);
        // ...detail, path and session:index.
        assert_eq!(shape(&build_rows(&d, Tab::All, "WHICH deck", false)), ["# NEEDS YOU", "need @1", "-- main", "main:@1"]);
        assert_eq!(shape(&build_rows(&d, Tab::All, "/nonexistent/k", false)).len(), 4);
        assert_eq!(shape(&build_rows(&d, Tab::All, "work:2", false)), ["-- work", "work:@7"]);
        // Parked rows too, under the same cap.
        assert_eq!(shape(&build_rows(&d, Tab::All, "sync", false)), ["# PARKED 2", "parked @13", "parked @14"]);
        assert_eq!(shape(&build_rows(&d, Tab::All, "zzz", false)), ["(no matches)"]);
        assert_eq!(shape(&build_rows(&d, Tab::Needs, "zzz", false)), ["(no matches)"]);
    }

    #[test]
    fn tabs_cycle_and_parse() {
        assert_eq!(Tab::All.step(1), Tab::Needs);
        assert_eq!(Tab::All.step(-1), Tab::Parked);
        assert_eq!(Tab::Parked.step(1), Tab::All);
        assert_eq!(Tab::parse("parked"), Some(Tab::Parked));
        assert_eq!(Tab::parse("bogus"), None);
    }

    #[test]
    fn empty_server() {
        let d = Data::build(&Snapshot::default(), None, 0.0, &Collator::new("C"), None, None);
        assert_eq!(shape(&build_rows(&d, Tab::All, "", false)), ["(no agents running)"]);
        assert_eq!(shape(&build_rows(&d, Tab::Parked, "", false)), ["(nothing parked)"]);
    }
}
