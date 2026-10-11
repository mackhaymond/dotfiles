//! What the menu's list shows: the tabs, the search filter, and the rows
//! (agent-roster.py `build_items` / `selectable` / `number_items`, reshaped
//! for the tabbed layout).
//!
//! The Active tab (the default) is every agent that is working or needs you,
//! in a FIXED order: one group per space in the core's space order (the
//! sidebar's AGENTS order, by name), then by window index. Nothing about a
//! row's state or age moves it: an agent that goes from working to
//! needs-input stays where it is and only changes glyph and colour (no NEEDS
//! YOU section here; that section reorders). Agents leaving or joining the
//! set make rows disappear or appear in their slot, never swap.
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
use crate::view::{Agent, BuildArgs, Need, Parked, SlotMemo, ViewModel};
use std::collections::{BTreeMap, HashMap};

/// The tab row's filters, in display (and Tab-cycle) order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Tab {
    /// Working ∪ needs you, in fixed space/index order (the default).
    Active,
    All,
    Needs,
    Working,
    Idle,
    Parked,
}

impl Tab {
    pub const ALL: [Tab; 6] = [Tab::Active, Tab::All, Tab::Needs, Tab::Working, Tab::Idle, Tab::Parked];

    /// The chip's words.
    pub fn title(self) -> &'static str {
        match self {
            Tab::Active => "Active",
            Tab::All => "All",
            Tab::Needs => "Needs you",
            Tab::Working => "Working",
            Tab::Idle => "Idle",
            Tab::Parked => "Parked",
        }
    }

    /// The chip's words when the full row does not fit.
    pub fn short_title(self) -> &'static str {
        match self {
            Tab::Needs => "Needs",
            Tab::Working => "Work",
            t => t.title(),
        }
    }

    /// The colour of the chip's count when the chip is not active.
    pub fn hue(self) -> &'static str {
        match self {
            Tab::Active | Tab::All => "text",
            Tab::Needs => "yellow",
            Tab::Working => "blue",
            Tab::Idle | Tab::Parked => "overlay",
        }
    }

    /// `--tab active|all|needs|working|idle|parked`.
    pub fn parse(s: &str) -> Option<Tab> {
        Some(match s {
            "active" => Tab::Active,
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
            Tab::Active => {
                let ids: std::collections::HashSet<&str> =
                    v.agents().filter(|a| is_active(v, a)).map(|a| a.window_id.as_str()).collect();
                ids.len()
            }
            Tab::All => v.counts.total(),
            Tab::Needs => v.needs.len(),
            Tab::Working => v.counts.get(Cat::Working),
            Tab::Idle => v.counts.get(Cat::Idle),
            Tab::Parked => v.parked.len(),
        }
    }
}

/// The NEEDS YOU entry of `a`'s window: what the Needs you tab lists for it
/// (by window id, so a linked window's row in any session finds it).
pub fn need_of<'a>(v: &'a ViewModel, a: &Agent) -> Option<&'a Need> {
    if !a.attention {
        return None; // the queue holds attention states only: skip the scan
    }
    v.needs.iter().find(|n| n.agent.window_id == a.window_id)
}

/// The Active tab's set: in flight (the Working bucket: running, a workflow
/// or cua out), needs you (exactly what the Needs you tab counts), or a
/// prompt just answered (idle with @agent_pending, its turn not resumed).
/// The last keeps the agent you just went to answer in place, with its
/// number, instead of dropping it for the moment between your answer and
/// its next turn: this is exactly who holds a slot
/// ([`crate::view::holds_slot`]). It is NOT in the Needs you queue, which
/// stays byte-identical to `agent-jump.sh list`.
pub fn is_active(v: &ViewModel, a: &Agent) -> bool {
    a.cat == Some(Cat::Working) || need_of(v, a).is_some() || a.answered
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
        Some(Target::of(self.key()?, self.agent()?))
    }

    /// The row's slot: an agent row's or a NEEDS YOU row's (the same agent,
    /// the same number). Parked rows never show one.
    pub fn slot(&self) -> Option<u32> {
        match self {
            Row::Need(n) => n.agent.slot,
            Row::Agent(a) => a.slot,
            _ => None,
        }
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
    /// The agent's slot as the row was built (None for a parked row).
    pub slot: Option<u32>,
}

impl Target {
    /// `a` as listed under `key`.
    pub fn of(key: RowKey, a: &Agent) -> Target {
        let slot = if matches!(key, RowKey::Parked(_)) { None } else { a.slot };
        Target { key, win: a.window_id.clone(), session: a.session.clone(), index: a.index, title: a.title.clone(), slot }
    }

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
    /// reads, cached, never waiting longer than its `branch_wait`. Slots
    /// with no memory of earlier frames (`--once`, tests): the live menu
    /// uses [`build_with`](Self::build_with).
    pub fn build(snap: &Snapshot, client: Option<&str>, now: f64, coll: &Collator, git: Option<&mut GitCache>,
        watcher_age: Option<f64>) -> Data {
        Data::build_with(snap, client, now, coll, git, watcher_age, &mut SlotMemo::default())
    }

    /// [`build`](Self::build), keeping the provisional slots this menu has
    /// shown in `memo` ([`SlotMemo`]).
    pub fn build_with(snap: &Snapshot, client: Option<&str>, now: f64, coll: &Collator, git: Option<&mut GitCache>,
        watcher_age: Option<f64>, memo: &mut SlotMemo) -> Data {
        let mut view = ViewModel::build_with(
            snap,
            BuildArgs { client, now, collator: coll, git: None, log: Vec::new(), watcher_age },
            memo,
        );
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

    if tab == Tab::Active {
        // The core's space order (by name, as the sidebar), never `d.order`
        // (most recently attached), so switching sessions moves nothing.
        for s in &v.spaces {
            let mut shown: Vec<&Agent> = s.agents.iter().filter(|a| is_active(v, a) && ok(a)).collect();
            shown.sort_by_key(|a| a.index);
            if shown.is_empty() {
                continue;
            }
            rows.push(Row::Group { name: s.name.clone(), branch: s.branch.clone(), counts: s.counts, current: s.is_current });
            rows.extend(shown.into_iter().cloned().map(Row::Agent));
        }
    }

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
                Tab::Active => "nothing working or waiting · ⇥ for all",
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

/// {row position: label} for every row whose agent holds a slot: its slot,
/// zero-padded to the view's width ([`crate::hotkeys`]). The same agent
/// listed twice (its NEEDS YOU row and its group row, or a window linked
/// into two sessions) shows the same number on both; a row without a slot
/// (an idle agent, a parked tab, a plain window) shows none.
pub fn number_rows(rows: &[Row], width: usize) -> HashMap<usize, String> {
    rows.iter()
        .enumerate()
        .filter_map(|(i, r)| r.slot().map(|s| (i, crate::hotkeys::slot_label(s, width))))
        .collect()
}

/// What each number key means in a frame drawn from `d`: label → the row it
/// goes to. Every slot in the frame's data is here, on screen or not (slots
/// are global and sticky, so `3` means agent 3 on every tab, through any
/// search). The target is the first row ON SCREEN showing that number
/// (`on_screen`, in display order), else its NEEDS YOU entry (the session
/// agent-jump.sh would go to), else its row in the menu's group order.
/// Slots are unique per window ([`crate::view::assign_slots`]).
pub fn slot_targets(d: &Data, on_screen: &[Target]) -> BTreeMap<String, Target> {
    let v = &d.view;
    let needs = v.needs.iter().map(|n| Target::of(RowKey::Need(n.agent.window_id.clone()), &n.agent));
    let groups = d
        .order
        .iter()
        .flat_map(|&i| v.spaces[i].agents.iter().chain(v.spaces[i].plain.iter()))
        .map(|a| Target::of(RowKey::Agent(a.session.clone(), a.window_id.clone()), a));
    let mut out = BTreeMap::new();
    for t in on_screen.iter().cloned().chain(needs).chain(groups) {
        if let Some(s) = t.slot {
            out.entry(crate::hotkeys::slot_label(s, v.slot_width)).or_insert(t);
        }
    }
    out
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
        // Labels: each Active agent's slot (no stamps here: the provisional
        // fill, main:1 main:2 main:3 work:1), the same number on its NEEDS YOU
        // row, its group row and a linked row; idle, parked and headers none.
        let labels = number_rows(&rows, d.view.slot_width);
        let mut got: Vec<(usize, &str)> = labels.iter().map(|(&i, l)| (i, l.as_str())).collect();
        got.sort_unstable();
        assert_eq!(got, [(1, "4"), (2, "1"), (4, "1"), (5, "2"), (6, "3"), (9, "4"), (11, "3")]);
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
        assert_eq!(c, [4, 7, 2, 2, 3, 5]); // @3 once though linked; agents session hidden
    }

    /// Active: working ∪ needs you, by space (the core's by-name order, not
    /// the All tab's most-recently-attached one), then window index; no
    /// NEEDS YOU section, no parked rows.
    #[test]
    fn active_tab_layout() {
        let d = data();
        let rows = build_rows(&d, Tab::Active, "", false);
        assert_eq!(shape(&rows), ["-- main", "main:@1", "main:@2", "main:@3", "-- work", "work:@6", "work:@3"]);
        // Exactly working ∪ the Needs you queue, each linked row in its space.
        let v = &d.view;
        let mut want: Vec<String> = Vec::new();
        for s in &v.spaces {
            let mut ids: Vec<&Agent> = s.agents.iter()
                .filter(|a| a.cat == Some(Cat::Working) || v.needs.iter().any(|n| n.agent.window_id == a.window_id))
                .collect();
            ids.sort_by_key(|a| a.index);
            want.extend(ids.iter().map(|a| format!("{}:{}", a.session, a.window_id)));
        }
        let got: Vec<String> = shape(&rows).into_iter().filter(|s| !s.starts_with("--")).collect();
        assert_eq!(got, want);
        // The order ignores the client's session and attach recency: same rows
        // with the client moved to Alpha and work attached last.
        let mut snap = fixture();
        for w in &mut snap.windows {
            w.last_attached = if w.session == "work" { 99 } else { 1 };
        }
        snap.clients[0].window_id = "@8".into();
        snap.clients[0].session = "Alpha".into();
        let d2 = Data::build(&snap, Some("/dev/ttys999"), 1000.0, &Collator::new("en_US.UTF-8"), None, Some(1.0));
        assert_eq!(shape(&build_rows(&d2, Tab::Active, "", false)), shape(&rows));
        // The search narrows it like any tab.
        assert_eq!(shape(&build_rows(&d, Tab::Active, "handy", false)), ["-- main", "main:@3", "-- work", "work:@3"]);
        assert_eq!(shape(&build_rows(&d, Tab::Active, "zzz", false)), ["(no matches)"]);
        // Labels are the agents' slots, not row positions: work:@3 is @3 = 3.
        let labels = number_rows(&rows, d.view.slot_width);
        assert_eq!((labels[&1].as_str(), labels[&5].as_str(), labels[&6].as_str(), labels.len()), ("1", "4", "3", 5));
    }

    /// The fixture with watcher stamps: `(window, slot)` on every row of it.
    pub fn stamped(slots: &[(&str, &str)]) -> Snapshot {
        let mut snap = fixture();
        for w in &mut snap.windows {
            w.slot = slots.iter().find(|(id, _)| w.id == *id).and_then(|(_, s)| crate::tmux::parse_slot(s));
        }
        snap
    }

    pub fn data_of(snap: &Snapshot) -> Data {
        Data::build(snap, Some("/dev/ttys999"), 1000.0, &Collator::new("en_US.UTF-8"), None, Some(1.0))
    }

    /// Gaps stay gaps; a missing stamp is filled with the lowest free number;
    /// a slot of 10+ makes every label two digits; slot targets cover the
    /// whole frame's data, on screen first.
    #[test]
    fn stamped_slots_label_rows() {
        // 1, 2, 4 held (3 was freed); @3 (linked) has no stamp yet: it takes 3.
        let d = data_of(&stamped(&[("@1", "1"), ("@2", "2"), ("@6", "4")]));
        let rows = build_rows(&d, Tab::Active, "", false);
        let labels = number_rows(&rows, d.view.slot_width);
        let shown: Vec<String> = rows.iter().enumerate()
            .filter_map(|(i, r)| r.key().map(|k| format!("{}={}", k.window(), labels.get(&i).map_or("-", String::as_str))))
            .collect();
        assert_eq!(shown, ["@1=1", "@2=2", "@3=3", "@6=4", "@3=3"]);
        // A gap with nobody to fill it: 1, 2, 4 and @3 idle.
        let mut snap = stamped(&[("@1", "1"), ("@2", "2"), ("@6", "4")]);
        for w in snap.windows.iter_mut().filter(|w| w.id == "@3") {
            w.state = "idle".into();
            w.workflow.clear();
        }
        let d = data_of(&snap);
        let rows = build_rows(&d, Tab::All, "", false);
        let mut labels: Vec<String> = number_rows(&rows, d.view.slot_width).into_values().collect();
        labels.sort();
        assert_eq!(labels, ["1", "1", "2", "4", "4"]); // @1 and @6 twice: NEEDS YOU and group
        // A slot of 12: every label is two digits, wherever it is drawn.
        let d = data_of(&stamped(&[("@1", "1"), ("@2", "12"), ("@3", "3"), ("@6", "4")]));
        assert_eq!(d.view.slot_width, 2);
        let rows = build_rows(&d, Tab::Needs, "", false); // @2 is not even listed here
        let labels = number_rows(&rows, d.view.slot_width);
        assert_eq!((labels[&1].as_str(), labels[&2].as_str()), ("04", "01"));
        // Slot targets: every slot of the data, the on-screen row first.
        let on_screen: Vec<Target> = rows.iter().filter_map(Row::target).collect();
        let st = slot_targets(&d, &on_screen);
        assert_eq!(st.keys().map(String::as_str).collect::<Vec<_>>(), ["01", "03", "04", "12"]);
        assert_eq!(st["01"].key, RowKey::Need("@1".into())); // its NEEDS YOU row, on screen
        assert_eq!(st["12"].key, RowKey::Agent("main".into(), "@2".into())); // off this tab: its group row
        assert_eq!(st["03"].key, RowKey::Agent("main".into(), "@3".into())); // linked: the menu's group order (main first)
        // Nothing on screen: needs first, then groups.
        let st = slot_targets(&d, &[]);
        assert_eq!(st["04"].key, RowKey::Need("@6".into()));
    }

    #[test]
    fn active_tab_empty() {
        let rows = [
            row("main", 1, "@1", &[("state", "idle")]),
            "\x1fclient\x1f/dev/ttys999\x1f@1\x1fmain".into(),
        ];
        let d = Data::build(&Snapshot::parse(&rows.join("\n")), Some("/dev/ttys999"), 1000.0, &Collator::new("C"), None,
            None);
        assert_eq!(shape(&build_rows(&d, Tab::Active, "", false)), ["(nothing working or waiting · ⇥ for all)"]);
        assert_eq!(Tab::Active.count(&d.view), 0);
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
        // Chip order: Active, All, Needs you, Working, Idle, Parked.
        assert_eq!(Tab::ALL.map(Tab::title), ["Active", "All", "Needs you", "Working", "Idle", "Parked"]);
        assert_eq!(Tab::Active.step(1), Tab::All);
        assert_eq!(Tab::All.step(1), Tab::Needs);
        assert_eq!(Tab::All.step(-1), Tab::Active);
        assert_eq!(Tab::Active.step(-1), Tab::Parked);
        assert_eq!(Tab::Parked.step(1), Tab::Active);
        assert_eq!(Tab::parse("active"), Some(Tab::Active));
        assert_eq!(Tab::parse("all"), Some(Tab::All));
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
