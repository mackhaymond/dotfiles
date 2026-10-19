//! The lifetime LETTER keys (`@agent_key`), one pool for agents AND shells.
//!
//! Every LISTED window owns one home-row-first letter for its whole life,
//! stored on the window as the tmux user option `@agent_key`. ⌥W then the
//! letter (spaces.conf's `jump` table), the letter in the menu, or a click
//! on its key-cap in the sidebar goes straight to it. Listed ([`lettered`]):
//!
//! - every AGENT window (an agent state, in a shown session: not parked,
//!   not in a HIDDEN session), and
//! - every SHELL of every space (a window without an agent state in a
//!   session that is not `@space_hidden`), only while `@spaces` is on
//!   ([`crate::view::ViewModel::shells`]: the classic setup has no ⌥W
//!   table, so it letters agents only, as before),
//!
//! never a space's HOME window (`@space_home`): the space itself is the
//! home, reached by its ⌃N. Agents and shells share the one pool, so no
//! letter is ever both a shell's and an agent's. An agent that exits back
//! to its shell, or a shell where an agent starts, stays listed, so it
//! keeps its letter.
//!
//! On each refresh ([`assign`], pure):
//! 1. A window whose `@agent_key` is a valid letter keeps it, unless another
//!    listed window holds the same letter with priority: on a duplicate the
//!    lower NUMERIC window id keeps it, the other is reassigned.
//! 2. Every other window (no key, junk, a duplicate's loser) takes the
//!    lowest free letter in [`ALPHABET`] order: every AGENT first, in
//!    DISPLAY order ([`lettered`]), then every shell in display order, so
//!    past 26 a new agent still gets a letter before a shell does.
//! 3. Past 26 windows there is no letter (the row is still navigable).
//! 4. A letter whose holder is no longer listed (closed, parked, made a
//!    home) is free. An unlisted window that still carries a letter a listed
//!    window now holds has its stale `@agent_key` unset, so it cannot steal
//!    the letter back when it returns (it gets the lowest free one instead).
//!
//! A window linked into two sessions is one window (keyed by id). The
//! assignment is shown at once (in memory); what changed is persisted with
//! ONE batched, fire-and-forget tmux command ([`command`], every part `-q`
//! so one dead target cannot abort the rest), and [`KeyMemo`] makes sure the
//! same write is not re-sent every tick while tmux catches up, yet is sent
//! once more if a snapshot still hasn't shown it after [`RESEND_AFTER`].

use crate::tmux::Snapshot;
use crate::view::{Agent, Space, ViewModel};
use std::collections::{HashMap, HashSet};
use std::time::{Duration, Instant};

/// The letters, in the order free ones are handed out: home row first.
pub const ALPHABET: [char; 26] = [
    'a', 's', 'd', 'f', 'g', 'h', 'j', 'k', 'l', 'q', 'w', 'e', 'r', 't', 'y', 'u', 'i', 'o', 'p', 'z', 'x', 'c',
    'v', 'b', 'n', 'm',
];

/// The tmux user option holding a window's letter.
pub const OPTION: &str = "@agent_key";

/// An `@agent_key` value → its letter, when it is exactly one letter of
/// [`ALPHABET`] (lowercase; anything else is junk and gets reassigned).
pub fn parse_key(raw: &str) -> Option<char> {
    let mut it = raw.chars();
    let c = it.next()?;
    (it.next().is_none() && ALPHABET.contains(&c)).then_some(c)
}

/// The window id's number, `@12` → 12 (unparseable: last).
fn id_num(id: &str) -> u64 {
    id.strip_prefix('@').and_then(|n| n.parse().ok()).unwrap_or(u64::MAX)
}

/// One `@agent_key` write: set the letter (Some) or unset a stale one (None).
pub type Write = (String, Option<char>);

/// [`assign`]'s result.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Assigned {
    /// window id → its letter (agents past the 26th have none).
    pub letters: HashMap<String, char>,
    /// What must be stored for the frame to be true next time, in display
    /// order (then the unsets).
    pub writes: Vec<Write>,
}

/// The assignment (see the module docs). `listed` is every listed agent
/// window in DISPLAY order as (window id, its `@agent_key`), a linked
/// window possibly twice (its first entry counts); `unlisted` is every
/// other window of the snapshot with its `@agent_key`.
pub fn assign(listed: &[(String, String)], unlisted: &[(String, String)]) -> Assigned {
    let mut seen: HashSet<&str> = HashSet::new();
    let agents: Vec<(&str, Option<char>)> = listed
        .iter()
        .filter(|(id, _)| seen.insert(id.as_str()))
        .map(|(id, raw)| (id.as_str(), parse_key(raw)))
        .collect();
    // 1. Valid keys: the lowest numeric window id holds a contested letter.
    let mut holder: HashMap<char, &str> = HashMap::new();
    for &(id, key) in &agents {
        if let Some(c) = key {
            match holder.get(&c) {
                Some(&h) if id_num(h) <= id_num(id) => {}
                _ => {
                    holder.insert(c, id);
                }
            }
        }
    }
    let mut letters: HashMap<String, char> = holder.iter().map(|(&c, &id)| (id.to_string(), c)).collect();
    // 2. The rest, in display order: the lowest free letter.
    let mut free = ALPHABET.iter().copied().filter(|c| !holder.contains_key(c));
    for &(id, _) in &agents {
        if letters.contains_key(id) {
            continue;
        }
        match free.next() {
            Some(c) => {
                letters.insert(id.to_string(), c);
            }
            None => break, // 3. past 26: no letter
        }
    }
    let mut writes: Vec<Write> = agents
        .iter()
        .filter_map(|&(id, key)| {
            let got = letters.get(id).copied()?;
            (key != Some(got)).then(|| (id.to_string(), Some(got)))
        })
        .collect();
    // 4. Stale claims on windows that are no longer listed.
    let held: HashSet<char> = letters.values().copied().collect();
    let mut gone: HashSet<&str> = HashSet::new();
    for (id, raw) in unlisted {
        if seen.contains(id.as_str()) || !gone.insert(id.as_str()) {
            continue;
        }
        if parse_key(raw).is_some_and(|c| held.contains(&c)) {
            writes.push((id.clone(), None));
        }
    }
    Assigned { letters, writes }
}

/// THE DISPLAY ORDER of the lettered windows, the one the sidebar draws
/// and the menu groups by: space by space in [`crate::view::space_order`]
/// (⌃N, then by name), and in each space its agents (agent state, by window
/// index), then its shells ([`Space::shells`]: no state, not the home, by
/// window index; none in a `@space_hidden` session). Homes are never in it.
/// A window linked into two spaces appears under both (its first entry
/// counts). Built from an UNSCOPED view (the scope never drops a window).
pub fn lettered(view: &ViewModel) -> Vec<&Agent> {
    view.ordered_spaces()
        .into_iter()
        .flat_map(|s: &Space| {
            let agents = s.agents.iter().filter(|a| !a.state.is_empty() && !a.home);
            let shells = if view.shells { s.shells() } else { Vec::new() };
            agents.chain(shells).collect::<Vec<&Agent>>()
        })
        .collect()
}

/// THE assignment for one snapshot, shared by the ⌥W menu and the CMD+B
/// sidebar so the two (and every open copy of either) always agree and
/// never fight over a letter: both call this with the snapshot and the
/// UNSCOPED view built from it ([`crate::view::ViewModel::build_with`]; the
/// sidebar's scope only renumbers, it never drops an agent from the view).
///
/// - Listed: [`lettered`] (every shown space's agents and shells, in display
///   order; never a home). Every other window of the snapshot is unlisted.
/// - `keys`: the caller's in-flight writes ([`KeyMemo`]): settled against
///   this snapshot, used as each window's key while unseen, and the writes
///   returned are only those not already in flight (plus any due a resend),
///   recorded as sent. None (`--once`, tests): every write the frame needs.
///
/// Idempotent: the same snapshot gives the same letters and the same writes
/// in any process, and a write that tmux already shows is never re-sent.
pub fn of_view(snap: &Snapshot, view: &ViewModel, keys: Option<&mut KeyMemo>) -> Assigned {
    let mut keys = keys;
    if let Some(k) = keys.as_deref_mut() {
        k.settle(snap.windows.iter().map(|w| (w.id.as_str(), w.key.as_str())));
    }
    let mut raw: HashMap<&str, &str> = HashMap::new();
    let mut homes: HashSet<&str> = HashSet::new();
    for w in &snap.windows {
        raw.entry(w.id.as_str()).or_insert(w.key.as_str());
        if w.home {
            homes.insert(w.id.as_str());
        }
    }
    let eff = |id: &str| {
        let r = raw.get(id).copied().unwrap_or("");
        keys.as_deref().map_or_else(|| r.to_string(), |k| k.effective(id, r))
    };
    // (A window that is a home in ANY of its links is a home: never listed.)
    // The FILL order: every agent first (display order), then every shell,
    // so past 26 a new agent still gets a letter before a shell does.
    let (agents, shells): (Vec<&Agent>, Vec<&Agent>) =
        lettered(view).into_iter().filter(|a| !homes.contains(a.window_id.as_str())).partition(|a| !a.state.is_empty());
    let shown: Vec<(String, String)> =
        agents.into_iter().chain(shells).map(|a| (a.window_id.clone(), eff(&a.window_id))).collect();
    let ids: HashSet<&str> = shown.iter().map(|(id, _)| id.as_str()).collect();
    let mut others: Vec<(String, String)> = Vec::new();
    let mut seen: HashSet<&str> = HashSet::new();
    for w in &snap.windows {
        if !ids.contains(w.id.as_str()) && seen.insert(w.id.as_str()) {
            others.push((w.id.clone(), eff(&w.id)));
        }
    }
    let mut a = assign(&shown, &others);
    if let Some(k) = keys {
        // New writes, then any still unseen past RESEND_AFTER (once).
        let now = Instant::now();
        let mut w = k.fresh(std::mem::take(&mut a.writes), now);
        w.extend(k.due(now));
        a.writes = w;
    }
    a
}

/// The ONE tmux command line (`;`-separated) that stores `writes`. Every
/// command is `-q`: tmux aborts a `;` list at its first error, so without
/// it a window closed since the snapshot (`-t @999`: no such window) would
/// drop every write after its own.
pub fn command(writes: &[Write]) -> Vec<String> {
    let mut a: Vec<String> = Vec::new();
    for (id, key) in writes {
        if !a.is_empty() {
            a.push(";".into());
        }
        match key {
            Some(c) => a.extend(["set-option", "-q", "-w", "-t", id, OPTION, &c.to_string()].map(String::from)),
            None => a.extend(["set-option", "-q", "-wu", "-t", id, OPTION].map(String::from)),
        }
    }
    a
}

/// How long a write may stay unseen in the snapshots before it is sent
/// once more (a lost write, or two writer threads landing out of order).
pub const RESEND_AFTER: Duration = Duration::from_millis(2500);

/// One write in flight: the value, when it was (last) sent, and whether it
/// has been re-sent already.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Sent {
    key: Option<char>,
    at: Instant,
    resent: bool,
}

/// What this menu has written and tmux has not shown back yet, by window
/// id. While a write is in flight its value counts as the window's key (so
/// a refresh before tmux applied it assigns the same letter), and it is not
/// sent again each tick; if a snapshot still does not show it after
/// [`RESEND_AFTER`] (and the window still exists), it is re-sent ONCE
/// ([`due`](Self::due)). A window that is gone is forgotten, never re-sent.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct KeyMemo {
    sent: HashMap<String, Sent>,
}

impl KeyMemo {
    /// Forget writes the snapshot now shows (or whose window is gone).
    /// `windows`: every (window id, raw @agent_key) of the snapshot.
    pub fn settle<'a>(&mut self, windows: impl IntoIterator<Item = (&'a str, &'a str)>) {
        let now: HashMap<&str, &str> = windows.into_iter().collect();
        self.sent.retain(|id, s| match (now.get(id.as_str()), s.key) {
            (None, _) => false, // the window is gone
            (Some(raw), Some(c)) => parse_key(raw) != Some(c),
            (Some(raw), None) => !raw.is_empty(),
        });
    }

    /// The key to assign from: an in-flight write, else the snapshot's.
    pub fn effective(&self, id: &str, raw: &str) -> String {
        match self.sent.get(id).map(|s| s.key) {
            Some(Some(c)) => c.to_string(),
            Some(None) => String::new(),
            None => raw.to_string(),
        }
    }

    /// The writes not already in flight; they are recorded as sent at `now`.
    pub fn fresh(&mut self, writes: Vec<Write>, now: Instant) -> Vec<Write> {
        writes
            .into_iter()
            .filter(|(id, k)| {
                if self.sent.get(id).is_some_and(|s| s.key == *k) {
                    return false;
                }
                self.sent.insert(id.clone(), Sent { key: *k, at: now, resent: false });
                true
            })
            .collect()
    }

    /// Writes still unseen [`RESEND_AFTER`] after they were sent and not
    /// re-sent yet: send them once more (call after [`settle`](Self::settle),
    /// so a gone window is never among them). In window-id order.
    pub fn due(&mut self, now: Instant) -> Vec<Write> {
        let mut out: Vec<Write> = self
            .sent
            .iter_mut()
            .filter(|(_, s)| !s.resent && now.saturating_duration_since(s.at) >= RESEND_AFTER)
            .map(|(id, s)| {
                s.resent = true;
                s.at = now;
                (id.clone(), s.key)
            })
            .collect();
        out.sort_by(|a, b| a.0.cmp(&b.0));
        out
    }

    pub fn is_empty(&self) -> bool {
        self.sent.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn l(v: &[(&str, &str)]) -> Vec<(String, String)> {
        v.iter().map(|(a, b)| (a.to_string(), b.to_string())).collect()
    }

    fn got(a: &Assigned, id: &str) -> Option<char> {
        a.letters.get(id).copied()
    }

    #[test]
    fn keys_parse_strictly() {
        assert_eq!(ALPHABET.iter().collect::<HashSet<_>>().len(), 26);
        for (raw, want) in [("a", Some('a')), ("m", Some('m')), ("", None), ("A", None), ("ab", None), (" a", None),
            ("1", None), ("é", None)] {
            assert_eq!(parse_key(raw), want, "{raw:?}");
        }
    }

    #[test]
    fn fresh_agents_get_home_row_letters_in_display_order() {
        let a = assign(&l(&[("@5", ""), ("@2", ""), ("@9", "")]), &[]);
        assert_eq!((got(&a, "@5"), got(&a, "@2"), got(&a, "@9")), (Some('a'), Some('s'), Some('d')));
        assert_eq!(a.writes, [("@5".into(), Some('a')), ("@2".into(), Some('s')), ("@9".into(), Some('d'))]);
    }

    /// Stable across refreshes: once stored, nothing moves and nothing is written.
    #[test]
    fn stored_letters_stick() {
        let a = assign(&l(&[("@5", "a"), ("@2", "s"), ("@9", "d")]), &[]);
        assert_eq!((got(&a, "@5"), got(&a, "@2"), got(&a, "@9")), (Some('a'), Some('s'), Some('d')));
        assert!(a.writes.is_empty());
        // A new agent listed FIRST does not take anyone's letter: the lowest free.
        let a = assign(&l(&[("@20", ""), ("@5", "a"), ("@2", "s"), ("@9", "d")]), &[]);
        assert_eq!(got(&a, "@20"), Some('f'));
        assert_eq!(a.writes, [("@20".into(), Some('f'))]);
    }

    /// A close frees the letter; the next newcomer reuses the lowest free one.
    #[test]
    fn lowest_free_reuse_after_a_close() {
        let a = assign(&l(&[("@5", "a"), ("@9", "d")]), &[]); // @2 (s) closed
        assert_eq!((got(&a, "@5"), got(&a, "@9")), (Some('a'), Some('d')));
        let a = assign(&l(&[("@5", "a"), ("@9", "d"), ("@30", "")]), &[]);
        assert_eq!(got(&a, "@30"), Some('s'));
        // A parked agent is unlisted: its letter is free; it keeps the option,
        // which is unset once someone else holds that letter.
        let a = assign(&l(&[("@5", "a"), ("@9", "d"), ("@30", "")]), &l(&[("@2", "s"), ("@7", "k"), ("@8", "")]));
        assert_eq!(got(&a, "@30"), Some('s'));
        assert_eq!(a.writes, [("@30".into(), Some('s')), ("@2".into(), None)]); // @7's k is nobody's: left alone
        // Back from the stash with its option unset: the lowest free letter.
        let a = assign(&l(&[("@2", ""), ("@5", "a"), ("@9", "d"), ("@30", "s")]), &[]);
        assert_eq!(got(&a, "@2"), Some('f'));
        // ...or, when its letter is still free, its own letter back.
        let a = assign(&l(&[("@2", "s"), ("@5", "a"), ("@9", "d")]), &[]);
        assert_eq!(got(&a, "@2"), Some('s'));
        assert!(a.writes.is_empty());
    }

    #[test]
    fn duplicates_go_to_the_lower_window_id() {
        let a = assign(&l(&[("@12", "a"), ("@3", "a"), ("@40", "s")]), &[]);
        assert_eq!((got(&a, "@3"), got(&a, "@12"), got(&a, "@40")), (Some('a'), Some('d'), Some('s')));
        assert_eq!(a.writes, [("@12".into(), Some('d'))]);
        // Junk is reassigned like no key; a linked window (listed twice) is one agent.
        let a = assign(&l(&[("@1", "Z"), ("@2", "s"), ("@2", "s"), ("@1", "Z")]), &[]);
        assert_eq!((got(&a, "@1"), got(&a, "@2"), a.letters.len()), (Some('a'), Some('s'), 2));
        assert_eq!(a.writes, [("@1".into(), Some('a'))]);
        // A listed window is never "unlisted" too.
        let a = assign(&l(&[("@1", "a")]), &l(&[("@1", "a")]));
        assert!(a.writes.is_empty());
    }

    #[test]
    fn past_26_agents_have_no_letter() {
        let many: Vec<(String, String)> = (1..=30).map(|i| (format!("@{i}"), String::new())).collect();
        let a = assign(&many, &[]);
        assert_eq!(a.letters.len(), 26);
        assert_eq!((got(&a, "@1"), got(&a, "@26"), got(&a, "@27"), got(&a, "@30")), (Some('a'), Some('m'), None, None));
        assert_eq!(a.writes.len(), 26);
        // One of the 26 closes: the 27th takes its letter.
        let mut stored: Vec<(String, String)> =
            many.iter().map(|(id, _)| (id.clone(), got(&a, id).map(String::from).unwrap_or_default())).collect();
        stored.remove(4); // @5 (g) closed
        let b = assign(&stored, &[]);
        assert_eq!((got(&b, "@27"), got(&b, "@28")), (Some('g'), None));
        assert_eq!(b.writes, [("@27".into(), Some('g'))]);
    }

    /// [`of_view`] over a snapshot, as the menu and the sidebar call it
    /// (`--once`: no memo).
    fn of_rows(rows: &[String]) -> Assigned {
        let snap = Snapshot::parse(&rows.join("\n"));
        let c = crate::collate::Collator::new("C");
        let args = crate::view::BuildArgs { client: None, now: 0.0, collator: &c, git: None, log: vec![], watcher_age: None };
        of_view(&snap, &ViewModel::build(&snap, args), None)
    }

    /// Spaces: work (⌃1: first), then hid and main by name. work: its home
    /// @5, its shell @6. hid (`@space_hidden`): a shell @7, an agent @8.
    /// main: its home @1, agents @2 (main:1) and @4 (main:3), a shell @3
    /// (main:2).
    fn pool(set: &[(&str, &str, &str)]) -> Vec<String> {
        type Spec<'a> = (&'a str, u32, &'a str, Vec<(&'a str, &'a str)>);
        let mut rows: Vec<Spec> = vec![
            ("main", 0, "@1", vec![("home", "1")]),
            ("main", 1, "@2", vec![("state", "running")]),
            ("main", 2, "@3", vec![]),
            ("main", 3, "@4", vec![("state", "idle")]),
            ("work", 0, "@5", vec![("home", "1"), ("space_index", "1")]),
            ("work", 1, "@6", vec![("space_index", "1")]),
            ("hid", 1, "@7", vec![("space_hidden", "1")]),
            ("hid", 2, "@8", vec![("state", "running"), ("space_hidden", "1")]),
        ];
        for (id, k, v) in set {
            for r in rows.iter_mut().filter(|r| r.2 == *id) {
                r.3.retain(|(f, _)| f != k);
                r.3.push((k, v));
            }
        }
        let mut out: Vec<String> = rows.iter().map(|(s, i, id, f)| crate::tmux::tests::row(s, *i, id, f)).collect();
        // A client with @spaces on (shells are a thing), unless `set` says "classic".
        if !set.iter().any(|(id, _, _)| *id == "classic") {
            out.push("\x1fclient\x1f/dev/ttys999\x1f@2\x1fmain\x1fon\x1f\x1f".into());
        }
        out
    }

    /// ONE pool for agents and shells. Free letters go to every agent first
    /// (display order: spaces ⌃N first, then by name; each space's agents
    /// by index), then to the shells (display order); a home never; a
    /// hidden session's shell never (its agent, as ever, yes).
    #[test]
    fn shells_share_the_pool_and_homes_never_get_one() {
        let a = of_rows(&pool(&[]));
        let got = |id: &str| a.letters.get(id).copied();
        assert_eq!([got("@8"), got("@2"), got("@4"), got("@6"), got("@3")], [Some('a'), Some('s'), Some('d'), Some('f'), Some('g')]);
        assert_eq!((got("@1"), got("@5"), got("@7")), (None, None, None));
        assert_eq!(a.letters.len(), 5);
        // No letter is both a shell's and an agent's.
        assert_eq!(a.letters.values().collect::<HashSet<_>>().len(), a.letters.len());
        // A shell holding a letter an agent also claims: one pool, the lower
        // window id keeps it, the other moves.
        let a = of_rows(&pool(&[("@3", "key", "a"), ("@6", "key", "a")]));
        assert_eq!((a.letters["@3"], a.letters["@6"]), ('a', 'g'));
        // @spaces off (classic, no ⌥W table): agents only, as before.
        let a = of_rows(&pool(&[("classic", "", "")]));
        assert_eq!((a.letters.len(), a.letters.get("@3"), a.letters.get("@6")), (3, None, None));
    }

    /// Past 26: agents before shells. With every letter but one held by
    /// stored shells, a new agent takes the free one; a new shell gets none.
    #[test]
    fn agents_fill_before_shells_past_26() {
        let mut rows: Vec<String> = Vec::new();
        for (k, c) in ALPHABET.iter().take(25).enumerate() {
            rows.push(crate::tmux::tests::row("main", k as u32 + 1, &format!("@{}", k + 1), &[("key", &c.to_string())]));
        }
        rows.push(crate::tmux::tests::row("main", 26, "@26", &[])); // a new shell, first in display order
        rows.push(crate::tmux::tests::row("zz", 1, "@27", &[("state", "running")])); // a new agent, last
        rows.push("\x1fclient\x1f/dev/ttys999\x1f@1\x1fmain\x1fon\x1f\x1f".into());
        let a = of_rows(&rows);
        assert_eq!((a.letters.get("@27"), a.letters.get("@26")), (Some(&'m'), None));
        assert_eq!(a.writes, [("@27".to_string(), Some('m'))]);
        // Stored letters still stick: the shells keep all 25.
        assert_eq!(a.letters.len(), 26);
    }

    /// A window keeps its letter for life: a shell where an agent starts, and
    /// an agent that exits back to its shell, keep theirs, nothing written.
    /// A home carrying a contested letter has it unset like any unlisted
    /// window.
    #[test]
    fn shell_and_agent_keep_their_letter_across_the_switch() {
        let keys = [("@6", "key", "a"), ("@8", "key", "s"), ("@2", "key", "d"), ("@4", "key", "f"), ("@3", "key", "g")];
        let stored = of_rows(&pool(&keys));
        assert!(stored.writes.is_empty(), "{:?}", stored.writes);
        // claude starts in the shell @3; the agent @2 exits to its shell.
        let mut flip = keys.to_vec();
        flip.extend([("@3", "state", "running"), ("@2", "state", "")]);
        let a = of_rows(&pool(&flip));
        assert_eq!(a.letters, stored.letters);
        assert!(a.writes.is_empty(), "{:?}", a.writes);
        // A home with a stale contested letter: unset; with a free one: left.
        let mut stale = keys.to_vec();
        stale.extend([("@1", "key", "a"), ("@5", "key", "z")]);
        let a = of_rows(&pool(&stale));
        assert_eq!(a.writes, [("@1".to_string(), None)]);
        assert_eq!(a.letters.get("@1"), None);
    }

    #[test]
    fn one_batched_command() {
        assert!(command(&[]).is_empty());
        let c = command(&[("@3".into(), Some('a')), ("@9".into(), None)]);
        assert_eq!(c, ["set-option", "-q", "-w", "-t", "@3", "@agent_key", "a", ";", "set-option", "-q", "-wu", "-t",
            "@9", "@agent_key"]);
    }

    /// A target that is gone by the time tmux runs the list (`@999`) must
    /// not abort the writes after it: every command in the list is `-q`.
    #[test]
    fn dead_target_never_aborts_the_batch() {
        let c = command(&[("@999".into(), Some('a')), ("@4".into(), Some('s')), ("@998".into(), None), ("@5".into(), Some('d'))]);
        let cmds: Vec<&[String]> = c.split(|a| a == ";").collect();
        assert_eq!(cmds.len(), 4);
        for cmd in &cmds {
            assert_eq!(&cmd[..2], ["set-option", "-q"], "{cmd:?}");
        }
        assert_eq!(cmds[1], ["set-option", "-q", "-w", "-t", "@4", "@agent_key", "s"]);
    }

    /// A write the snapshots still don't show after RESEND_AFTER is sent ONE
    /// more time (lost, or overtaken by an older thread); never a loop.
    #[test]
    fn memo_resends_once_after_the_ttl() {
        let t0 = Instant::now();
        let mut m = KeyMemo::default();
        assert_eq!(m.fresh(vec![("@4".into(), Some('s'))], t0).len(), 1);
        m.settle([("@4", "")]); // not applied yet
        assert!(m.due(t0 + Duration::from_millis(500)).is_empty()); // too early
        assert!(m.due(t0 + RESEND_AFTER - Duration::from_millis(1)).is_empty());
        assert_eq!(m.due(t0 + RESEND_AFTER), [("@4".to_string(), Some('s'))]);
        // Once only, however long it stays unseen; the letter still holds.
        m.settle([("@4", "")]);
        assert!(m.due(t0 + RESEND_AFTER * 3).is_empty());
        assert!(m.fresh(vec![("@4".into(), Some('s'))], t0 + RESEND_AFTER * 3).is_empty());
        assert_eq!(m.effective("@4", ""), "s");
        // An unset write the same way.
        let mut m = KeyMemo::default();
        m.fresh(vec![("@6".into(), None)], t0);
        m.settle([("@6", "a")]);
        assert_eq!(m.due(t0 + RESEND_AFTER), [("@6".to_string(), None)]);
        // The window closed: forgotten by settle, never re-sent.
        let mut m = KeyMemo::default();
        m.fresh(vec![("@7".into(), Some('d')), ("@8".into(), Some('f'))], t0);
        m.settle([("@8", "")]); // @7 is gone
        assert_eq!(m.due(t0 + RESEND_AFTER * 2), [("@8".to_string(), Some('f'))]);
        m.settle([]);
        assert!(m.is_empty() && m.due(t0 + RESEND_AFTER * 9).is_empty());
        // Seen in time: nothing to re-send.
        let mut m = KeyMemo::default();
        m.fresh(vec![("@9".into(), Some('g'))], t0);
        m.settle([("@9", "g")]);
        assert!(m.due(t0 + RESEND_AFTER * 2).is_empty());
    }

    #[test]
    fn memo_never_resends_and_settles() {
        let t = Instant::now();
        let mut m = KeyMemo::default();
        let w = vec![("@3".to_string(), Some('a')), ("@9".to_string(), None)];
        assert_eq!(m.fresh(w.clone(), t), w);
        assert!(m.fresh(w.clone(), t).is_empty()); // tmux has not caught up: not again
        assert_eq!((m.effective("@3", ""), m.effective("@9", "s"), m.effective("@4", "k")), ("a".into(), "".into(), "k".into()));
        // The snapshot shows @3's letter; @9 still has its stale one.
        m.settle([("@3", "a"), ("@9", "s")]);
        assert_eq!(m.effective("@3", ""), ""); // forgotten: the snapshot is the truth again
        assert_eq!(m.effective("@9", "s"), "");
        m.settle([("@3", "a"), ("@9", "")]);
        assert!(m.is_empty());
        // A window that closed before its write landed is forgotten too.
        m.fresh(vec![("@5".into(), Some('d'))], t);
        m.settle([("@3", "a")]);
        assert!(m.is_empty());
        // A different value for the same window is a new write.
        m.fresh(vec![("@5".into(), Some('d'))], t);
        assert_eq!(m.fresh(vec![("@5".into(), Some('f'))], t).len(), 1);
    }
}
