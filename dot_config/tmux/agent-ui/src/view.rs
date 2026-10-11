//! The view model both UIs draw from: one [`ViewModel`] per refresh.
//!
//! Built from one [`Snapshot`] (agent-roster.py `Roster.load` +
//! `Strip.strip_model`), plus the git cache and the event log:
//!
//! - `needs`: agent-jump.sh `list` order exactly ([`model::needs_order`]).
//!   Each entry is the window's row in the session `list` keeps (its first
//!   sorted row), which is also the session agent-jump.sh `next` goes to.
//! - `spaces`: one per tmux session, sorted by name (code point order, the
//!   Python `sorted()`), minus HIDDEN (agents, tasks, scratch, btop-popup;
//!   exact names) and the stash. Each carries its counts, its ACTIVE window's
//!   cwd and branch, and the repo's ahead/behind/dirty when known.
//! - `spaces[i].agents`: that session's windows that have an agent state, plus
//!   the window the client is on, in WINDOW INDEX order. Fixed: a state change
//!   recolours a row where it is and never moves it. (The Python strip sorted
//!   by rank first; the new layout deliberately does not.) `plain` holds the
//!   rest (plain shells), for a "show all" toggle.
//! - `parked`: the stash's windows by index.
//! - `log`: the newest events, newest first.
//! - `counts`: the header's per-state counts over every shown session
//!   (stash and HIDDEN excluded), each window id once.
//! - every agent's `slot`: its sticky number key ([`assign_slots`]), and
//!   `slot_width`, the digit count every label is padded to.

use crate::collate::Collator;
use crate::events::{Event, EventLog};
use crate::git::{GitCache, GitStatus};
use crate::hotkeys;
use crate::json::Value;
use crate::model::{self, Cat, Counts, Flag};
use crate::obj;
use crate::text;
use crate::tmux::{Snapshot, Tmux, Window, HOLD};
use std::collections::HashMap;

/// The agents that hold a number key: the ACTIVE set, the menu's Active tab.
/// In flight (running, a workflow or cua out), needing you (failed,
/// needs-input, done without a workflow: agent-jump.sh's queue), or a prompt
/// just answered at `now` ([`Window::answered`]: idle with a @agent_pending
/// epoch at most 600 s old, its turn not resumed yet; going to answer a
/// needs-input agent must not free its number). Other idle agents (a lapsed
/// or junk stamp among them), plain shells and stateless windows hold none.
pub fn holds_slot(w: &Window, now: f64) -> bool {
    model::in_flight(w) || model::is_attn(w) || w.answered(now)
}

/// The provisional numbers this process has shown, by window id: an
/// Active agent the watcher has not stamped yet keeps the number it was
/// first shown with until it is stamped (the stamp wins) or leaves the
/// Active set, so a later arrival that sorts before it can never take it.
/// The menu and the sidebar each hold one ([`Core::slots`]) across frames.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SlotMemo {
    held: HashMap<String, u32>,
}

impl SlotMemo {
    /// The provisional number remembered for `window_id`.
    pub fn get(&self, window_id: &str) -> Option<u32> {
        self.held.get(window_id).copied()
    }

    pub fn is_empty(&self) -> bool {
        self.held.is_empty()
    }
}

/// The window id's number, `@12` → 12 (unparseable: last).
fn id_num(id: &str) -> u64 {
    id.strip_prefix('@').and_then(|n| n.parse().ok()).unwrap_or(u64::MAX)
}

/// Every Active window's slot (window id → number), computed the way
/// agent-tab-watcher.sh `reconcile_slots` does, from the same snapshot, so a
/// UI never shows a new agent unnumbered for the watcher's tick (the watcher
/// stamps the same numbers within about a second):
///
/// 1. Eligible: [`holds_slot`], and linked into at least one session outside
///    agent-jump.sh's EXCLUDE ([`model::jump_excluded`]: agents, tasks,
///    stash, scratch, btop-popup). Its VISIBLE link is the one with the
///    byte-order SMALLEST session name (then the lower index). A parked or
///    hidden-only window, and every non-Active one, gets nothing.
/// 2. Keep stamps: an eligible window keeps its valid `@agent_slot`
///    ([`crate::tmux::parse_slot`]). On a duplicate the lower NUMERIC window
///    id keeps it. A stamp on a window that is no longer eligible is
///    ignored: its number is free at once.
/// 3. Remembered: every other eligible window (no stamp, junk, a
///    duplicate's loser) that `memo` holds a number for keeps it, unless a
///    stamp now holds that number (the stamp wins; rare once the hook stamps
///    synchronously).
/// 4. Fill: the rest take the LOWEST number free of both the stamps and the
///    remembered numbers, in (visible session name in plain BYTE order,
///    never the locale's collation, so "Zeta" < "alpha"; that link's window
///    index; numeric window id) order. The watcher sorts the same way.
///
/// `memo` is then exactly the provisional numbers given (steps 3 and 4): a
/// window stamped or no longer Active drops out of it. Nobody else's number
/// changes, so a freed slot leaves a gap and the next agent to become
/// active fills the lowest one. With an empty memo this is the watcher's
/// assignment exactly. `now` (epoch seconds) dates answered prompts
/// ([`holds_slot`]).
pub fn assign_slots(windows: &[Window], now: f64, memo: &mut SlotMemo) -> HashMap<String, u32> {
    /// A window's link: (session, window index), ordered as a tuple: the
    /// session name by bytes (`str`'s `Ord`), then the index.
    type Link<'a> = (&'a str, u32);
    // Each window id once: its stamp and its visible link.
    let mut seen: HashMap<&str, (&Window, Option<Link>)> = HashMap::new();
    for w in windows {
        let vis = (!model::jump_excluded(&w.session)).then_some((w.session.as_str(), w.index));
        let e = seen.entry(w.id.as_str()).or_insert((w, None));
        if let Some(v) = vis {
            if e.1.is_none_or(|cur| v < cur) {
                e.1 = Some(v);
            }
        }
    }
    let mut eligible: Vec<(&str, Link, Option<u32>)> = seen
        .into_iter()
        .filter_map(|(id, (w, vis))| Some((id, vis?, w.slot)).filter(|_| holds_slot(w, now)))
        .collect();
    // Deterministic: (visible link, window id), the fill order.
    eligible.sort_by(|a, b| a.1.cmp(&b.1).then_with(|| id_num(a.0).cmp(&id_num(b.0))).then_with(|| a.0.cmp(b.0)));
    let mut held: HashMap<u32, &str> = HashMap::new();
    let mut need: Vec<&str> = Vec::new();
    for &(id, _, stamp) in &eligible {
        match stamp {
            Some(s) => match held.get(&s) {
                None => {
                    held.insert(s, id);
                }
                Some(&keep) if id_num(id) < id_num(keep) => {
                    held.insert(s, id);
                    need.push(keep);
                }
                Some(_) => need.push(id),
            },
            None => need.push(id),
        }
    }
    // A duplicate's loser joins in fill order, not in discovery order.
    let rank: HashMap<&str, usize> = eligible.iter().enumerate().map(|(i, e)| (e.0, i)).collect();
    need.sort_by_key(|id| rank[id]);
    let mut out: HashMap<String, u32> = held.iter().map(|(&s, &id)| (id.to_string(), s)).collect();
    // Remembered provisional numbers first, so no newcomer can take one.
    let mut fresh: Vec<&str> = Vec::new();
    for id in need.iter().copied() {
        match memo.get(id).filter(|n| !held.contains_key(n)) {
            Some(n) => {
                held.insert(n, id);
                out.insert(id.to_string(), n);
            }
            None => fresh.push(id),
        }
    }
    let mut next = 1;
    for id in fresh {
        while held.contains_key(&next) {
            next += 1;
        }
        held.insert(next, id);
        out.insert(id.to_string(), next);
    }
    memo.held = need.iter().map(|&id| (id.to_string(), out[id])).collect();
    out
}

/// One window as a row: everything a UI needs to draw it, already decided.
#[derive(Debug, Clone, PartialEq)]
pub struct Agent {
    pub window_id: String,
    pub session: String,
    pub index: u32,
    /// window_name.
    pub name: String,
    /// Raw @agent_state ("" = no agent).
    pub state: String,
    /// Count bucket (None: plain shell).
    pub cat: Option<Cat>,
    /// agent-roster.py `rank` (0 failed .. 4 other, 9 none).
    pub rank: u8,
    /// The state shape (✕ ◉ ✓ ◐ ○), " " for a plain shell.
    pub glyph: &'static str,
    /// The palette colour of the shape (Roster.dot, pulse applied), None
    /// for a plain shell.
    pub color: Option<&'static str>,
    /// Summary, else window name (@stash_label first for parked rows).
    pub title: String,
    /// `title` split at its first `/` (agent-roster.py `fit_label`'s split):
    /// the project with its slash, and the rest. No slash: ("", title).
    pub project: String,
    pub short_title: String,
    /// @agent_detail_kind / its word (perm, asks, fail, done, run) / @agent_detail.
    pub detail_kind: String,
    pub detail_word: Option<&'static str>,
    pub detail: String,
    /// The epoch from @agent_since and its `ago()`.
    pub since: Option<i64>,
    pub age: String,
    /// The state in words ("waiting on you", "done · fleet out", ...).
    pub state_words: &'static str,
    /// @agent_kind (claude|codex) and its glyph (✳ ⬢).
    pub kind: String,
    pub kind_glyph: Option<&'static str>,
    pub workflow: bool,
    pub cua: bool,
    /// The side flag (workflow ⚙ / cua ◎) and its colour, pulse applied.
    pub flag: Option<(Flag, &'static str)>,
    pub attention: bool,
    pub in_flight: bool,
    /// A working agent reporting what it does (@agent_detail_kind run).
    pub run_detail: bool,
    /// The window the client is on (by window id, as the Python).
    pub is_current: bool,
    /// window_active in its session.
    pub active: bool,
    pub panes: u32,
    /// The active pane's cwd.
    pub path: String,
    /// The sticky number key ([`assign_slots`]; set by [`ViewModel::build`],
    /// None from [`Agent::from_window`] alone). None: not in the Active set
    /// (idle, a plain shell, parked).
    pub slot: Option<u32>,
    /// A prompt just answered ([`Window::answered`]): idle, but still in the
    /// Active set (the menu's Active tab, a slot) until its turn resumes.
    pub answered: bool,
}

impl Agent {
    pub fn from_window(w: &Window, blink: bool, cur_win: Option<&str>, now: f64) -> Agent {
        let is_current = cur_win == Some(w.id.as_str());
        let cat = model::cat(w);
        // Display text is scrubbed here (control chars, a newline or US in a
        // summary among them, become spaces: agent-roster.py's CONTROL), so
        // a UI can draw these fields as they are. `session`, `window_id` and
        // `path` stay raw: actions and git need the real values.
        let label = text::sanitize(&w.label);
        let (project, short_title) = match label.split_once('/') {
            Some((p, t)) if !label.starts_with('/') => (format!("{p}/"), t.to_string()),
            _ => (String::new(), label.clone()),
        };
        Agent {
            window_id: w.id.clone(),
            session: w.session.clone(),
            index: w.index,
            name: text::sanitize(&w.name),
            state: w.state.clone(),
            cat,
            rank: model::rank(w),
            glyph: cat.map(Cat::glyph).unwrap_or(" "),
            color: model::dot_color(w, blink, is_current),
            title: label,
            project,
            short_title,
            detail_kind: w.detail_kind.clone(),
            detail_word: model::detail_word(&w.detail_kind),
            detail: text::sanitize(&w.detail),
            since: w.since_t,
            age: text::ago(w.since_t, now),
            state_words: model::state_words(w),
            kind: w.kind.clone(),
            kind_glyph: model::kind_glyph(&w.kind),
            workflow: !w.workflow.is_empty(),
            cua: !w.cua.is_empty(),
            flag: model::flag(w, blink),
            attention: model::is_attn(w),
            in_flight: model::in_flight(w),
            run_detail: cat == Some(Cat::Working) && w.detail_kind == "run" && !w.detail.is_empty(),
            is_current,
            active: w.active,
            panes: w.panes,
            path: w.path.clone(),
            slot: None,
            answered: w.answered(now),
        }
    }

    /// `session:index`.
    pub fn place(&self) -> String {
        format!("{}:{}", self.session, self.index)
    }

    pub fn to_json(&self) -> Value {
        obj![
            ("window_id", &self.window_id), ("session", &self.session), ("index", self.index),
            ("name", &self.name), ("state", &self.state), ("cat", self.cat.map(Cat::name)),
            ("rank", self.rank), ("glyph", self.glyph), ("color", self.color), ("title", &self.title),
            ("project", &self.project), ("short_title", &self.short_title),
            ("detail_kind", &self.detail_kind), ("detail_word", self.detail_word), ("detail", &self.detail),
            ("since", self.since), ("age", &self.age), ("state_words", self.state_words),
            ("kind", &self.kind), ("kind_glyph", self.kind_glyph), ("workflow", self.workflow),
            ("cua", self.cua),
            ("flag", self.flag.map(|(f, c)| obj![
                ("kind", match f { Flag::Workflow => "workflow", Flag::Cua => "cua" }),
                ("glyph", match f { Flag::Workflow => model::STRIP_GEAR, Flag::Cua => model::STRIP_MOUSE }),
                ("color", c)])),
            ("attention", self.attention), ("in_flight", self.in_flight), ("run_detail", self.run_detail),
            ("is_current", self.is_current), ("active", self.active), ("panes", self.panes),
            ("path", &self.path), ("slot", self.slot), ("answered", self.answered),
        ]
    }
}

/// A NEEDS YOU entry: the agent plus its reason line (Strip.need_detail).
#[derive(Debug, Clone, PartialEq)]
pub struct Need {
    pub agent: Agent,
    /// The detail word (perm, asks, fail, done) when there is a non-run
    /// detail, else the state in words ("waiting on you").
    pub reason_word: String,
    /// The detail text, or "" when `reason_word` is the state.
    pub reason: String,
    /// The @agent_since stamp exactly as agent-jump.sh printed it.
    pub stamp: String,
}

impl Need {
    fn new(agent: Agent, stamp: String) -> Need {
        let (word, text) = match agent.detail_word {
            Some(wd) if agent.detail_kind != "run" && !agent.detail.is_empty() => (wd.to_string(), agent.detail.clone()),
            _ => {
                let s = match agent.state.as_str() {
                    "failed" => "failed",
                    "needs-input" => "waiting on you",
                    "done" => "done",
                    other => other,
                };
                (s.to_string(), String::new())
            }
        };
        Need { agent, reason_word: word, reason: text, stamp }
    }

    pub fn to_json(&self) -> Value {
        let mut v = self.agent.to_json();
        if let Value::Obj(kv) = &mut v {
            kv.push(("reason_word".into(), self.reason_word.as_str().into()));
            kv.push(("reason".into(), self.reason.as_str().into()));
            kv.push(("stamp".into(), self.stamp.as_str().into()));
        }
        v
    }
}

/// One tmux session (a herdr "space").
#[derive(Debug, Clone, PartialEq)]
pub struct Space {
    pub name: String,
    /// The client is in this session (one of its windows is the client's).
    pub is_current: bool,
    pub counts: Counts,
    /// The session's active window.
    pub active_window: Option<String>,
    pub active_index: Option<u32>,
    /// The active window's active-pane cwd, and with `~` for $HOME.
    pub path: String,
    pub path_short: String,
    /// git_head of `path` ("" outside a repo, under a remote prefix, or not
    /// read yet).
    pub branch: String,
    /// The repo's last `git status` (None until the first background run
    /// lands, or outside a repo).
    pub git: Option<GitStatus>,
    /// Agent windows (+ the client's window), by window index.
    pub agents: Vec<Agent>,
    /// The other (plain) windows, by window index.
    pub plain: Vec<Agent>,
}

impl Space {
    pub fn to_json(&self) -> Value {
        obj![
            ("name", &self.name), ("is_current", self.is_current), ("counts", counts_json(&self.counts)),
            ("active_window", self.active_window.clone()), ("active_index", self.active_index),
            ("path", &self.path), ("path_short", &self.path_short), ("branch", &self.branch),
            ("git", self.git.as_ref().map(|g| obj![
                ("head", &g.head), ("upstream", g.upstream.clone()), ("ahead", g.ahead),
                ("behind", g.behind), ("changed", g.changed), ("untracked", g.untracked),
                ("dirty", g.dirty())])),
            ("agents", Value::Arr(self.agents.iter().map(Agent::to_json).collect())),
            ("plain", Value::Arr(self.plain.iter().map(Agent::to_json).collect())),
        ]
    }
}

/// A parked (stash) window.
#[derive(Debug, Clone, PartialEq)]
pub struct Parked {
    pub agent: Agent,
    /// @stash_origin: the session it came from.
    pub origin: String,
    /// @stash_session set: the agent was suspended (resumes on unpark).
    pub suspended: bool,
    /// The row's state words: the agent state, else "suspended" / "parked".
    pub state_words: String,
    /// @stash_ts, and the age shown (stash time, else @agent_since).
    pub parked_at: Option<i64>,
    pub age: String,
}

impl Parked {
    pub fn to_json(&self) -> Value {
        let mut v = self.agent.to_json();
        if let Value::Obj(kv) = &mut v {
            kv.push(("origin".into(), self.origin.as_str().into()));
            kv.push(("suspended".into(), self.suspended.into()));
            kv.push(("parked_state".into(), self.state_words.as_str().into()));
            kv.push(("parked_at".into(), self.parked_at.into()));
            kv.push(("parked_age".into(), self.age.as_str().into()));
        }
        v
    }
}

/// An event-log line, ready to draw.
#[derive(Debug, Clone, PartialEq)]
pub struct LogEntry {
    pub event: Event,
    /// Local `HH:MM`.
    pub time: String,
    pub cat: Option<Cat>,
    /// Shape and static hue of the new state (CAT_GLYPH / CAT_HUE).
    pub glyph: &'static str,
    pub color: &'static str,
    pub age: String,
}

impl LogEntry {
    fn new(event: Event, now: f64) -> LogEntry {
        let cat = event.cat();
        LogEntry {
            time: text::clock_hm(event.epoch),
            glyph: cat.map(Cat::glyph).unwrap_or("·"),
            color: cat.map(Cat::hue).unwrap_or("overlay"),
            age: text::ago(Some(event.epoch), now),
            cat,
            event,
        }
    }

    pub fn to_json(&self) -> Value {
        let e = &self.event;
        obj![
            ("epoch", e.epoch), ("time", &self.time), ("age", &self.age), ("window_id", &e.window_id),
            ("session", &e.session), ("index", e.index), ("state", &e.state), ("prev_state", &e.prev_state),
            ("detail_kind", &e.detail_kind), ("title", &e.title), ("detail", &e.detail),
            ("cat", self.cat.map(Cat::name)), ("glyph", self.glyph), ("color", self.color),
        ]
    }
}

fn counts_json(c: &Counts) -> Value {
    Value::Obj(Cat::ALL.iter().map(|&k| (k.name().to_string(), Value::from(c.get(k)))).collect())
}

/// `~` for a $HOME prefix.
pub fn tilde(path: &str) -> String {
    let home = std::env::var("HOME").unwrap_or_default();
    if !home.is_empty() && home != "/" {
        if path == home {
            return "~".into();
        }
        if let Some(rest) = path.strip_prefix(&format!("{}/", home.trim_end_matches('/'))) {
            return format!("~/{rest}");
        }
    }
    path.to_string()
}

/// Everything one frame shows.
#[derive(Debug, Clone, PartialEq)]
pub struct ViewModel {
    pub now: f64,
    /// The client tty this view was built for.
    pub client: Option<String>,
    /// The client was given but is not attached (any more).
    pub client_gone: bool,
    /// The window and session the client is on.
    pub cur_win: Option<String>,
    pub cur_session: Option<String>,
    /// @agent_blink: the pulse phase every colour above was computed with.
    pub blink: bool,
    pub counts: Counts,
    pub needs: Vec<Need>,
    pub spaces: Vec<Space>,
    pub parked: Vec<Parked>,
    pub log: Vec<LogEntry>,
    /// Seconds since the watcher's heartbeat (None: no pidfile).
    pub watcher_age: Option<f64>,
    /// The collation `needs` was sorted with.
    pub locale: String,
    /// Every number label's width: the digit count of the highest slot held
    /// ([`hotkeys::slot_width`]); 0 when no agent holds one.
    pub slot_width: usize,
}

/// Inputs to [`ViewModel::build`] besides the snapshot.
pub struct BuildArgs<'a> {
    pub client: Option<&'a str>,
    pub now: f64,
    pub collator: &'a Collator,
    /// None: no branches/status (tests, or a UI that does not want them).
    pub git: Option<&'a mut GitCache>,
    /// Newest first (EventLog::newest).
    pub log: Vec<Event>,
    pub watcher_age: Option<f64>,
}

impl ViewModel {
    /// A one-shot view: slots with no memory of earlier frames (a `--once`
    /// render, a test). A UI that redraws uses [`build_with`](Self::build_with).
    pub fn build(snap: &Snapshot, args: BuildArgs<'_>) -> ViewModel {
        ViewModel::build_with(snap, args, &mut SlotMemo::default())
    }

    /// The view, with `memo` keeping each provisional slot this process has
    /// shown ([`SlotMemo`], [`assign_slots`]) and updated for the next frame.
    pub fn build_with(snap: &Snapshot, args: BuildArgs<'_>, memo: &mut SlotMemo) -> ViewModel {
        let BuildArgs { client, now, collator, mut git, log, watcher_age } = args;
        let blink = snap.blink();
        let cl = client.and_then(|c| snap.client(c));
        let cur_win = cl.map(|c| c.window_id.clone());
        let cur_session = cl.and_then(|c| {
            if !c.session.is_empty() {
                Some(c.session.clone())
            } else {
                snap.rows(&c.window_id).next().map(|w| w.session.clone())
            }
        });
        let cw = cur_win.as_deref();
        let slots = assign_slots(&snap.windows, now, memo);
        let agent = |w: &Window| Agent { slot: slots.get(&w.id).copied(), ..Agent::from_window(w, blink, cw, now) };

        let needs = model::needs_order(&snap.windows, collator)
            .into_iter()
            .filter_map(|r| snap.row_in(&r.id, &r.session).map(|w| Need::new(agent(w), r.stamp)))
            .collect();

        let shown = |w: &&Window| !model::HIDDEN.contains(&w.session.as_str()) && w.session != HOLD;
        let counts = Counts::of(snap.windows.iter().filter(shown));

        let mut names: Vec<&str> = snap.windows.iter().filter(shown).map(|w| w.session.as_str()).collect();
        names.sort();
        names.dedup();
        let mut spaces = Vec::new();
        for name in names {
            let mut ws: Vec<&Window> = snap.windows.iter().filter(|w| w.session == name).collect();
            ws.sort_by_key(|w| w.index);
            let active = ws.iter().find(|w| w.active).copied();
            let path = active.map(|w| w.path.clone()).unwrap_or_default();
            let (branch, status) = match git.as_deref_mut() {
                Some(g) => (g.branch(&path, now), g.status(&path, now)),
                None => (String::new(), None),
            };
            let (agents, plain): (Vec<&Window>, Vec<&Window>) =
                ws.iter().partition(|w| !w.state.is_empty() || Some(w.id.as_str()) == cw);
            spaces.push(Space {
                name: name.to_string(),
                is_current: ws.iter().any(|w| Some(w.id.as_str()) == cw),
                counts: Counts::of(ws.iter().copied()),
                active_window: active.map(|w| w.id.clone()),
                active_index: active.map(|w| w.index),
                path_short: text::sanitize(&tilde(&path)),
                path,
                branch,
                git: status,
                agents: agents.into_iter().map(agent).collect(),
                plain: plain.into_iter().map(agent).collect(),
            });
        }

        let mut parked_ws: Vec<&Window> = snap.windows.iter().filter(|w| w.session == HOLD).collect();
        parked_ws.sort_by_key(|w| w.index);
        let parked = parked_ws
            .into_iter()
            .map(|w| {
                let a = agent(w);
                let words = if w.state.is_empty() {
                    if w.stash_session.is_empty() { "parked" } else { "suspended" }.to_string()
                } else {
                    a.state_words.to_string()
                };
                let when = w.stash_t.or(w.since_t);
                Parked {
                    origin: text::sanitize(&w.stash_origin),
                    suspended: !w.stash_session.is_empty(),
                    state_words: words,
                    parked_at: w.stash_t,
                    age: text::ago(when, now),
                    agent: a,
                }
            })
            .collect();

        ViewModel {
            now,
            client: client.map(String::from),
            client_gone: client.is_some() && cl.is_none(),
            cur_win,
            cur_session,
            blink,
            counts,
            needs,
            spaces,
            parked,
            log: log.into_iter().map(|e| LogEntry::new(e, now)).collect(),
            watcher_age,
            locale: collator.name().to_string(),
            slot_width: hotkeys::slot_width(slots.values().copied().max()),
        }
    }

    /// `a`'s number label as drawn and typed (zero-padded to
    /// `slot_width`), None when it holds no slot.
    pub fn slot_label(&self, a: &Agent) -> Option<String> {
        a.slot.map(|s| hotkeys::slot_label(s, self.slot_width))
    }

    /// Every agent row in sidebar order (space by space), for hit-testing
    /// or numbering.
    pub fn agents(&self) -> impl Iterator<Item = &Agent> {
        self.spaces.iter().flat_map(|s| s.agents.iter())
    }

    pub fn to_json(&self) -> Value {
        obj![
            ("now", self.now), ("client", self.client.clone()), ("client_gone", self.client_gone),
            ("cur_win", self.cur_win.clone()), ("cur_session", self.cur_session.clone()),
            ("blink", self.blink), ("locale", &self.locale), ("watcher_age", self.watcher_age),
            ("slot_width", self.slot_width),
            ("counts", counts_json(&self.counts)),
            ("needs", Value::Arr(self.needs.iter().map(Need::to_json).collect())),
            ("spaces", Value::Arr(self.spaces.iter().map(Space::to_json).collect())),
            ("parked", Value::Arr(self.parked.iter().map(Parked::to_json).collect())),
            ("log", Value::Arr(self.log.iter().map(LogEntry::to_json).collect())),
        ]
    }
}

/// The whole read side in one place: a UI holds one Core and calls
/// [`refresh`](Core::refresh) on its tick.
pub struct Core {
    pub tmux: Tmux,
    pub git: GitCache,
    pub events: EventLog,
    pub collator: Collator,
    /// How many log events a view carries.
    pub log_limit: usize,
    /// The snapshot the last view was built from (actions re-check against it).
    pub snapshot: Snapshot,
    /// The provisional slots this UI has shown, across frames.
    pub slots: SlotMemo,
}

impl Core {
    /// tmux from the env (AGENT_UI_TMUX_SOCKET), the default event log, the
    /// environment's collation.
    pub fn new(tmux: Tmux) -> Core {
        Core {
            tmux,
            git: GitCache::new(),
            events: EventLog::default_path(),
            collator: Collator::from_env(),
            log_limit: 50,
            snapshot: Snapshot::default(),
            slots: SlotMemo::default(),
        }
    }

    /// One tmux call, then the view for `client`. Never blocks on git
    /// status; a branch read may take up to BRANCH_WAIT on a cache miss.
    pub fn refresh(&mut self, client: Option<&str>) -> ViewModel {
        self.snapshot = self.tmux.snapshot();
        self.rebuild(client)
    }

    /// The view from the last snapshot (e.g. after a git result landed).
    pub fn rebuild(&mut self, client: Option<&str>) -> ViewModel {
        let now = text::now();
        let log = self.events.newest(self.log_limit);
        ViewModel::build_with(
            &self.snapshot,
            BuildArgs {
                client,
                now,
                collator: &self.collator,
                git: Some(&mut self.git),
                log,
                watcher_age: crate::actions::watcher_age(),
            },
            &mut self.slots,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tmux::tests::row;

    fn snap() -> Snapshot {
        let rows = [
            row("main", 3, "@3", &[("state", "running"), ("summary", "proj/Three"), ("workflow", "1"), ("since", "900 running")]),
            row("main", 1, "@1", &[("state", "needs-input"), ("summary", "proj/One"), ("since", "100 needs-input"),
                ("detail_kind", "ask"), ("detail", "which deck?"), ("active", "1"), ("path", "/nonexistent/main")]),
            row("main", 2, "@2", &[]),
            row("work", 1, "@4", &[("state", "failed"), ("summary", "Four"), ("since", "300 failed"), ("active", "1")]),
            row("work", 2, "@5", &[("state", "done"), ("summary", "Five"), ("workflow", "1")]),
            row("agents", 1, "@6", &[("state", "running")]),
            row("btop-popup", 1, "@7", &[("state", "failed")]),
            row("stash", 1, "@8", &[("summary", "x"), ("stash_label", "Parked one"), ("stash_session", "sid"),
                ("stash_ts", "500"), ("stash_origin", "main")]),
            row("stash", 0, "@9", &[("state", "needs-input")]),
            row("Alpha", 0, "@10", &[("state", "idle"), ("active", "1")]),
            "\x1fclient\x1f/dev/ttys999\x1f@2\x1fmain".into(),
        ];
        Snapshot::parse(&rows.join("\n"))
    }

    fn build(client: Option<&str>) -> ViewModel {
        let c = Collator::new("en_US.UTF-8");
        ViewModel::build(&snap(), BuildArgs { client, now: 1000.0, collator: &c, git: None, log: vec![], watcher_age: None })
    }

    #[test]
    fn spaces_fixed_order_and_exclusions() {
        let v = build(Some("/dev/ttys999"));
        let names: Vec<_> = v.spaces.iter().map(|s| s.name.as_str()).collect();
        assert_eq!(names, ["Alpha", "main", "work"]); // code point order; HIDDEN and stash gone
        let main = &v.spaces[1];
        assert!(main.is_current);
        // Agents by window index (not rank), the client's plain window kept.
        assert_eq!(main.agents.iter().map(|a| a.index).collect::<Vec<_>>(), [1, 2, 3]);
        assert!(main.plain.is_empty());
        assert_eq!(main.active_window.as_deref(), Some("@1"));
        assert_eq!(main.path, "/nonexistent/main");
        assert_eq!(main.counts.nonzero(), vec![(Cat::NeedsInput, 1), (Cat::Working, 1)]);
        assert!(main.agents[1].is_current && main.agents[1].cat.is_none());
        // Without a client the plain window is hidden.
        let v2 = build(None);
        assert_eq!(v2.spaces[1].agents.len(), 2);
        assert_eq!(v2.spaces[1].plain.len(), 1);
    }

    #[test]
    fn needs_and_counts() {
        let v = build(Some("/dev/ttys999"));
        let ids: Vec<_> = v.needs.iter().map(|n| n.agent.window_id.as_str()).collect();
        assert_eq!(ids, ["@4", "@1"]); // failed first; done-with-fleet, stash, HIDDEN excluded
        assert_eq!((v.needs[1].reason_word.as_str(), v.needs[1].reason.as_str()), ("asks", "which deck?"));
        assert_eq!(v.needs[0].reason_word, "failed");
        assert_eq!(v.needs[1].agent.age, "15m");
        // Header: failed 1, needs 1, working 2 (@3, @5), idle 1 (Alpha).
        assert_eq!(v.counts.0, [1, 1, 0, 2, 1]);
        assert_eq!((v.cur_win.as_deref(), v.cur_session.as_deref(), v.client_gone), (Some("@2"), Some("main"), false));
        assert!(build(Some("/dev/nope")).client_gone);
    }

    #[test]
    fn parked_rows() {
        let v = build(None);
        assert_eq!(v.parked.len(), 2);
        assert_eq!(v.parked[0].agent.window_id, "@9"); // by index
        assert_eq!(v.parked[1].agent.title, "Parked one");
        assert_eq!((v.parked[1].state_words.as_str(), v.parked[1].suspended), ("suspended", true));
        assert_eq!((v.parked[1].origin.as_str(), v.parked[1].age.as_str()), ("main", "8m"));
        assert_eq!(v.parked[0].state_words, "waiting on you");
    }

    #[test]
    fn colours_and_json() {
        let v = build(Some("/dev/ttys999"));
        let three = &v.spaces[1].agents[2];
        assert_eq!((three.glyph, three.color), ("◐", Some("pink"))); // blink "1": pulsing pink
        assert_eq!(three.flag, Some((Flag::Workflow, "teal")));
        assert_eq!((three.project.as_str(), three.short_title.as_str()), ("proj/", "Three"));
        let j = v.to_json().to_json();
        let back = crate::json::parse(&j).unwrap();
        assert_eq!(back.get("spaces").unwrap().as_array().unwrap().len(), 3);
        assert_eq!(back.get("counts").unwrap().get("working").unwrap().as_f64(), Some(2.0));
    }

    /// One frame with no memory (the watcher's own assignment).
    fn slots_of(rows: &[String]) -> Vec<(String, u32)> {
        slots_with(rows, &mut SlotMemo::default())
    }

    /// The slot tests' clock: @agent_pending "1700000000" is 0 s old.
    const T_NOW: f64 = 1_700_000_000.0;

    /// One frame of a UI that remembers what it showed in `memo`, at [`T_NOW`].
    fn slots_with(rows: &[String], memo: &mut SlotMemo) -> Vec<(String, u32)> {
        slots_at(rows, T_NOW, memo)
    }

    fn slots_at(rows: &[String], now: f64, memo: &mut SlotMemo) -> Vec<(String, u32)> {
        let s = Snapshot::parse(&rows.join("\n"));
        let mut v: Vec<(String, u32)> = assign_slots(&s.windows, now, memo).into_iter().collect();
        v.sort_by_key(|(id, _)| id_num(id));
        v
    }

    fn memo_of(m: &SlotMemo) -> Vec<(String, u32)> {
        let mut v: Vec<(String, u32)> = m.held.iter().map(|(k, &n)| (k.clone(), n)).collect();
        v.sort_by_key(|(id, _)| id_num(id));
        v
    }

    fn ids(v: &[(&str, u32)]) -> Vec<(String, u32)> {
        v.iter().map(|(i, s)| (i.to_string(), *s)).collect()
    }

    /// No stamps yet (the watcher has not ticked): every Active agent gets a
    /// provisional number, lowest first in (session, index) order; idle,
    /// plain, parked and hidden windows get none.
    #[test]
    fn slots_provisional_fill() {
        let got = slots_of(&[
            row("work", 2, "@1", &[("state", "running")]),
            row("main", 3, "@2", &[("state", "needs-input")]),
            row("main", 1, "@3", &[("state", "idle")]),
            row("main", 2, "@4", &[("state", "done")]),                     // needs you
            row("main", 4, "@5", &[("state", "done"), ("workflow", "1")]), // fleet out: working
            row("main", 5, "@6", &[("cua", "1")]),                          // cua out, no state
            row("main", 6, "@7", &[]),                                      // a plain shell
            row("stash", 1, "@8", &[("state", "running")]),                 // parked
            row("agents", 1, "@9", &[("state", "failed")]),                 // hidden
            row("Alpha", 9, "@10", &[("state", "failed")]),
        ]);
        // Alpha:9, main:2, main:3, main:4, main:5, work:2.
        assert_eq!(got, ids(&[("@1", 6), ("@2", 3), ("@4", 2), ("@5", 4), ("@6", 5), ("@10", 1)]));
        // Through the view: the agents carry them, and the label width follows the highest.
        let v = ViewModel::build(&snap(), BuildArgs { client: None, now: 1000.0, collator: &Collator::new("C"), git: None,
            log: vec![], watcher_age: None });
        let main = &v.spaces[1];
        assert_eq!(main.agents.iter().map(|a| a.slot).collect::<Vec<_>>(), [Some(1), Some(2)]); // main:1, main:3
        assert_eq!(v.needs.iter().map(|n| n.agent.slot).collect::<Vec<_>>(), [Some(3), Some(1)]); // work:1, main:1
        assert!(v.parked.iter().all(|p| p.agent.slot.is_none()));
        assert_eq!(v.spaces[0].agents[0].slot, None); // Alpha: idle
        assert_eq!(v.slot_width, 1);
        assert_eq!(v.slot_label(&main.agents[1]).as_deref(), Some("2"));
    }

    /// Stamps are kept; a stamp on an agent that left the Active set (idle,
    /// parked, a plain shell) is ignored and its number is free at once.
    #[test]
    fn slots_stale_stamp_ignored() {
        let got = slots_of(&[
            row("main", 1, "@1", &[("state", "running"), ("slot", "2")]),
            row("main", 2, "@2", &[("state", "idle"), ("slot", "1")]),   // went idle: 1 is free
            row("main", 3, "@3", &[("state", "running")]),               // new: takes 1
            row("stash", 1, "@4", &[("state", "running"), ("slot", "3")]), // parked: 3 is free
            row("main", 4, "@5", &[("slot", "4")]),                      // agent exited
            row("main", 5, "@6", &[("state", "failed")]),                // new: takes 3
        ]);
        assert_eq!(got, ids(&[("@1", 2), ("@3", 1), ("@6", 3)]));
    }

    /// 1–4 held; 3 goes idle: 1, 2, 4 keep theirs and the next agent to
    /// become active takes 3. A later arrival that sorts before it takes 5:
    /// the 3 already shown is never displaced (frames through one memo).
    #[test]
    fn slots_gap_filled_by_the_next_agent() {
        let held = |three: &str| {
            vec![
                row("main", 1, "@1", &[("state", "running"), ("slot", "1")]),
                row("main", 2, "@2", &[("state", "running"), ("slot", "2")]),
                row("main", 3, "@3", &[("state", three), ("slot", "3")]),
                row("main", 4, "@4", &[("state", "needs-input"), ("slot", "4")]),
            ]
        };
        assert_eq!(slots_of(&held("running")), ids(&[("@1", 1), ("@2", 2), ("@3", 3), ("@4", 4)]));
        assert_eq!(slots_of(&held("idle")), ids(&[("@1", 1), ("@2", 2), ("@4", 4)]));
        let mut memo = SlotMemo::default();
        let mut rows = held("idle");
        assert_eq!(slots_with(&rows, &mut memo), ids(&[("@1", 1), ("@2", 2), ("@4", 4)]));
        rows.push(row("zz", 9, "@7", &[("state", "running")])); // last in fill order, still the lowest gap
        assert_eq!(slots_with(&rows, &mut memo), ids(&[("@1", 1), ("@2", 2), ("@4", 4), ("@7", 3)]));
        rows.push(row("aa", 1, "@8", &[("state", "running")])); // sorts first, arrives later
        assert_eq!(slots_with(&rows, &mut memo), ids(&[("@1", 1), ("@2", 2), ("@4", 4), ("@7", 3), ("@8", 5)]));
        // Arriving in the same frame (nothing shown yet), fill order decides.
        assert_eq!(slots_of(&rows), ids(&[("@1", 1), ("@2", 2), ("@4", 4), ("@7", 5), ("@8", 3)]));
    }

    /// The memo, frame by frame: a provisional number is stable across
    /// frames and across earlier-sorting arrivals; a stamp always wins
    /// (adopted even when it differs, and the memo drops the window); leaving
    /// the Active set releases it; a newcomer takes the lowest number free of
    /// stamps AND remembered numbers.
    #[test]
    fn slot_memo_across_frames() {
        let mut memo = SlotMemo::default();
        let run = |s: &str, i: u32, id: &str, slot: &str| {
            let mut set = vec![("state", "running")];
            if !slot.is_empty() {
                set.push(("slot", slot));
            }
            row(s, i, id, &set)
        };
        // Frame 1: one stamped agent, one new one (provisional 2).
        let mut rows = vec![run("main", 1, "@1", "1"), run("work", 5, "@5", "")];
        assert_eq!(slots_with(&rows, &mut memo), ids(&[("@1", 1), ("@5", 2)]));
        assert_eq!(memo_of(&memo), ids(&[("@5", 2)]));
        // Frame 2: the same snapshot, the same numbers.
        assert_eq!(slots_with(&rows, &mut memo), ids(&[("@1", 1), ("@5", 2)]));
        // Frame 3: two unstamped arrivals that sort before @5 (aa < work):
        // they take 3 and 4, never @5's 2.
        rows.push(run("aa", 1, "@6", ""));
        rows.push(run("main", 2, "@7", ""));
        assert_eq!(slots_with(&rows, &mut memo), ids(&[("@1", 1), ("@5", 2), ("@6", 3), ("@7", 4)]));
        assert_eq!(memo_of(&memo), ids(&[("@5", 2), ("@6", 3), ("@7", 4)]));
        // Frame 4: the watcher stamps. @5 and @6 as shown; @7 differently
        // (a race): the stamp is adopted, and stamped windows leave the memo.
        rows = vec![run("main", 1, "@1", "1"), run("work", 5, "@5", "2"), run("aa", 1, "@6", "3"),
            run("main", 2, "@7", "9")];
        assert_eq!(slots_with(&rows, &mut memo), ids(&[("@1", 1), ("@5", 2), ("@6", 3), ("@7", 9)]));
        assert!(memo.is_empty());
        // A remembered number a stamp now claims: the stamp wins, the
        // remembered window moves to the lowest free number.
        let mut memo = SlotMemo::default();
        assert_eq!(slots_with(&[run("main", 1, "@1", "")], &mut memo), ids(&[("@1", 1)]));
        assert_eq!(slots_with(&[run("main", 1, "@1", ""), run("main", 2, "@2", "1")], &mut memo),
            ids(&[("@1", 2), ("@2", 1)]));
        // Leaving the set releases a remembered number at once: the next
        // newcomer may take it.
        let mut memo = SlotMemo::default();
        assert_eq!(slots_with(&[run("main", 1, "@1", ""), run("main", 2, "@2", "")], &mut memo),
            ids(&[("@1", 1), ("@2", 2)]));
        let idle = row("main", 1, "@1", &[("state", "idle")]);
        assert_eq!(slots_with(&[idle.clone(), run("main", 2, "@2", "")], &mut memo), ids(&[("@2", 2)]));
        assert_eq!(memo_of(&memo), ids(&[("@2", 2)]));
        assert_eq!(slots_with(&[idle, run("main", 2, "@2", ""), run("zz", 1, "@3", "")], &mut memo),
            ids(&[("@2", 2), ("@3", 1)]));
        // A window that closes is gone from the memo too.
        assert_eq!(slots_with(&[run("zz", 1, "@3", "")], &mut memo), ids(&[("@3", 1)]));
        assert_eq!(memo_of(&memo), ids(&[("@3", 1)]));
    }

    /// An answered prompt (idle + @agent_pending, its turn not resumed) is
    /// still Active: it keeps its stamp (and a provisional number), and it is
    /// in the Active set but NOT in the needs queue. Bare idle is not.
    #[test]
    fn slots_answered_prompt_keeps_its_number() {
        let rows = [
            row("main", 1, "@1", &[("state", "running"), ("slot", "1")]),
            row("main", 2, "@2", &[("state", "idle"), ("pending", "1700000000"), ("slot", "2")]),
            row("main", 3, "@3", &[("state", "idle"), ("slot", "3")]),
            row("main", 4, "@4", &[("state", "idle"), ("pending", "1700000000")]),
        ];
        assert_eq!(slots_of(&rows), ids(&[("@1", 1), ("@2", 2), ("@4", 3)]));
        let v = ViewModel::build(&Snapshot::parse(&rows.join("\n")), BuildArgs { client: None, now: T_NOW,
            collator: &Collator::new("C"), git: None, log: vec![], watcher_age: None });
        assert!(v.needs.is_empty()); // agent-jump.sh's queue: unchanged
        let a = &v.spaces[0].agents;
        assert_eq!(a.iter().map(|a| (a.answered, a.slot)).collect::<Vec<_>>(),
            [(false, Some(1)), (true, Some(2)), (false, None), (true, Some(3))]);
        assert_eq!(a[1].glyph, "○"); // drawn as what it is: idle
        // Through a needs-input → answered → running round trip the number never moves.
        let mut memo = SlotMemo::default();
        for st in [("needs-input", ""), ("idle", "1700000000"), ("running", "")] {
            let r = [row("main", 1, "@1", &[("state", "running")]), row("aa", 1, "@9", &[("state", st.0), ("pending", st.1)])];
            assert_eq!(slots_with(&r, &mut memo), ids(&[("@1", 2), ("@9", 1)]), "{st:?}");
        }
    }

    /// The pending stamp ages out: at 600 s the answered agent still holds
    /// its number; at 601 s (an answer-by-No that never resumed, a seen
    /// failure) it is plain idle and lets it go, stamped or remembered. A
    /// junk stamp never held it.
    #[test]
    fn slots_answered_prompt_lapses_after_600s() {
        let answered = |pending: &str, slot: &str| {
            let mut set = vec![("state", "idle"), ("pending", pending)];
            if !slot.is_empty() {
                set.push(("slot", slot));
            }
            vec![row("main", 1, "@1", &[("state", "running"), ("slot", "1")]), row("main", 2, "@2", &set)]
        };
        let fresh = || SlotMemo::default();
        assert_eq!(slots_at(&answered("1700000000", "2"), T_NOW + 600.0, &mut fresh()), ids(&[("@1", 1), ("@2", 2)]));
        assert_eq!(slots_at(&answered("1700000000", "2"), T_NOW + 601.0, &mut fresh()), ids(&[("@1", 1)]));
        assert_eq!(slots_at(&answered("1700000000", "2"), T_NOW + 600.9, &mut fresh()), ids(&[("@1", 1), ("@2", 2)]));
        for junk in ["x", "17e8", " 1700000000", "-1"] {
            assert_eq!(slots_at(&answered(junk, "2"), T_NOW, &mut fresh()), ids(&[("@1", 1)]), "{junk:?}");
        }
        // Remembered (never stamped): kept while fresh, released at 601 s, and
        // the number is free for the next agent.
        let mut memo = SlotMemo::default();
        let r = answered("1700000000", "");
        assert_eq!(slots_at(&r, T_NOW, &mut memo), ids(&[("@1", 1), ("@2", 2)]));
        assert_eq!(slots_at(&r, T_NOW + 600.0, &mut memo), ids(&[("@1", 1), ("@2", 2)]));
        assert_eq!(slots_at(&r, T_NOW + 601.0, &mut memo), ids(&[("@1", 1)]));
        assert!(memo.is_empty());
        let mut r2 = r.clone();
        r2.push(row("zz", 1, "@3", &[("state", "running")]));
        assert_eq!(slots_at(&r2, T_NOW + 602.0, &mut memo), ids(&[("@1", 1), ("@3", 2)]));
        // Through the view: the flag follows the clock.
        let snap = Snapshot::parse(&r.join("\n"));
        for (now, want) in [(T_NOW + 600.0, (true, Some(2))), (T_NOW + 601.0, (false, None))] {
            let v = ViewModel::build(&snap, BuildArgs { client: None, now, collator: &Collator::new("C"), git: None,
                log: vec![], watcher_age: None });
            let a = &v.spaces[0].agents[1];
            assert_eq!((a.answered, a.slot), want, "{now}");
        }
    }

    /// Duplicates: the lower window id keeps it, the loser fills like a new
    /// agent. A linked window is one window: one slot, its visible link
    /// deciding its place. Junk stamps are new agents.
    #[test]
    fn slots_duplicates_links_and_junk() {
        let got = slots_of(&[
            row("main", 1, "@9", &[("state", "running"), ("slot", "1")]),
            row("main", 2, "@3", &[("state", "running"), ("slot", "1")]),
            row("main", 3, "@5", &[("state", "running"), ("slot", "007")]),
            row("zz", 1, "@6", &[("state", "running")]),
            row("agents", 0, "@6", &[("state", "running")]), // its hidden link does not count
            row("aa", 5, "@7", &[("state", "running"), ("slot", "12")]),
        ]);
        // @3 keeps 1; then by visible link: main:1 @9 → 2, main:3 @5 → 3, zz:1 @6 → 4.
        assert_eq!(got, ids(&[("@3", 1), ("@5", 3), ("@6", 4), ("@7", 12), ("@9", 2)]));
        let s = Snapshot::parse(&[row("main", 1, "@1", &[("state", "running"), ("slot", "12")]),
            row("main", 2, "@2", &[("state", "running")])].join("\n"));
        let v = ViewModel::build(&s, BuildArgs { client: None, now: 0.0, collator: &Collator::new("C"), git: None,
            log: vec![], watcher_age: None });
        assert_eq!(v.slot_width, 2);
        let labels: Vec<_> = v.spaces[0].agents.iter().map(|a| v.slot_label(a)).collect();
        assert_eq!(labels, [Some("12".to_string()), Some("01".to_string())]);
    }

    /// The fill order is plain BYTE order (the watcher's), never the locale's
    /// collation: "Zeta" comes before "alpha" (en_US would say the opposite),
    /// and "_x" after "Zeta" but before "alpha". A linked window sorts by its
    /// byte-smallest non-hidden session and that link's index. The lower
    /// numeric id wins a duplicate (@9 < @10 though "@10" < "@9" as text).
    #[test]
    fn slots_fill_in_byte_order() {
        let got = slots_of(&[
            row("alpha", 1, "@1", &[("state", "running")]),
            row("Zeta", 1, "@2", &[("state", "running")]),
            row("_x", 1, "@3", &[("state", "running")]),
            row("beta", 9, "@4", &[("state", "running")]),
            row("Beta", 7, "@4", &[("state", "running")]),   // linked: "Beta" < "beta", index 7
            row("agents", 0, "@4", &[("state", "running")]), // hidden: never its sort key
            row("Beta", 8, "@5", &[("state", "running")]),
        ]);
        // Byte order: "Beta":7 @4, "Beta":8 @5, "Zeta" @2, "_x" @3, "alpha" @1.
        assert_eq!(got, ids(&[("@1", 5), ("@2", 3), ("@3", 4), ("@4", 1), ("@5", 2)]));
        let got = slots_of(&[
            row("main", 1, "@10", &[("state", "running"), ("slot", "1")]),
            row("main", 2, "@9", &[("state", "running"), ("slot", "1")]),
        ]);
        assert_eq!(got, ids(&[("@9", 1), ("@10", 2)]));
        // The collation the view is built with does not change any of it.
        let snap = Snapshot::parse(&[row("alpha", 1, "@1", &[("state", "running")]),
            row("Zeta", 1, "@2", &[("state", "running")])].join("\n"));
        for loc in ["en_US.UTF-8", "C"] {
            let v = ViewModel::build(&snap, BuildArgs { client: None, now: 0.0, collator: &Collator::new(loc), git: None,
                log: vec![], watcher_age: None });
            let slot = |id: &str| v.agents().find(|a| a.window_id == id).and_then(|a| a.slot);
            assert_eq!((slot("@2"), slot("@1")), (Some(1), Some(2)), "{loc}");
        }
    }

    #[test]
    fn tilde_paths() {
        let home = std::env::var("HOME").unwrap();
        assert_eq!(tilde(&format!("{home}/code/x")), "~/code/x");
        assert_eq!(tilde(&home), "~");
        assert_eq!(tilde("/etc"), "/etc");
        assert_eq!(tilde(&format!("{home}x/y")), format!("{home}x/y"));
    }
}
