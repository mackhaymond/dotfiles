use super::*;
use crate::collate::Collator;
use crate::tmux::tests::row;
use crate::tmux::Snapshot;
use crate::view::BuildArgs;
use std::collections::HashMap;

const NOW: i64 = 1_000_000;
const CLIENT: &str = "/dev/ttys999";

/// One agent window: (session, index, state, title) plus options.
struct A<'a> {
    s: &'a str,
    i: u32,
    st: &'a str,
    title: &'a str,
    set: Vec<(&'a str, String)>,
}

/// Its `@agent_since` is `60 * i + 30` seconds ago (so `1m`, `2m`, ...).
fn a<'a>(s: &'a str, i: u32, st: &'a str, title: &'a str) -> A<'a> {
    let since = format!("{} {st}", NOW - 60 * i64::from(i) - 30);
    let mut set = vec![("since", since), ("kind", "claude".into()), ("path", format!("/nonexistent/{s}"))];
    if i == 1 {
        set.push(("active", "1".into()));
    }
    A { s, i, st, title, set }
}

impl<'a> A<'a> {
    fn with(mut self, k: &'a str, v: &str) -> Self {
        self.set.push((k, v.to_string()));
        self
    }
}

/// Window ids are assigned in order; the client sits on `here` (an id).
fn view(agents: Vec<A>, here: Option<&str>) -> ViewModel {
    let mut rows: Vec<String> = agents
        .iter()
        .enumerate()
        .map(|(n, x)| {
            let id = format!("@{}", n + 1);
            let mut set: Vec<(&str, &str)> = vec![("state", x.st), ("summary", x.title)];
            set.extend(x.set.iter().map(|(k, v)| (*k, v.as_str())));
            row(x.s, x.i, &id, &set)
        })
        .collect();
    // Noise the sidebar must ignore: a hidden session, a parked tab, a plain shell.
    rows.push(row("agents", 1, "@900", &[("state", "failed"), ("summary", "hidden")]));
    rows.push(row("stash", 1, "@901", &[("summary", "x"), ("stash_label", "Parked one"), ("stash_ts", "5")]));
    rows.push(row("main", 99, "@902", &[]));
    if let Some(h) = here {
        rows.push(format!("\x1fclient\x1f{CLIENT}\x1f{h}\x1fmain"));
    }
    let c = Collator::new("en_US.UTF-8");
    let args = BuildArgs { client: Some(CLIENT), now: NOW as f64, collator: &c, git: None, log: vec![], watcher_age: Some(1.0) };
    ViewModel::build(&Snapshot::parse(&rows.join("\n")), args)
}

/// Today: 11 agents, 1 need.
fn quiet() -> ViewModel {
    let agents = vec![
        a("main", 1, "running", "Handy.app Speech Commands").with("workflow", "1")
            .with("detail_kind", "run").with("detail", "Bash swift build -c release"),
        a("main", 2, "running", "Tmux Agent Sidebar").with("detail_kind", "run").with("detail", "Edit agent-roster.py"),
        a("main", 3, "needs-input", "Kua Yu focus timing").with("detail_kind", "ask")
            .with("detail", "Which deck should I pull the timing from?"),
        a("main", 4, "idle", "Commodities Job Tracker").with("detail_kind", "done").with("detail", "7 fixes applied"),
        a("main", 5, "idle", "Notch Tasks Orchestrator"),
        a("main", 6, "idle", "Lost suit jacket"),
        a("main", 7, "idle", "Backyard Halloween"),
        a("bai", 1, "idle", "Application link"),
        a("bai", 2, "idle", "BAI Info Session Slides"),
        a("schedule", 1, "idle", "P0a multi-user execution"),
        a("schedule", 2, "idle", "Prose extraction model"),
    ];
    view(agents, Some("@2"))
}

/// A busy day: 24 agents, 4 needs.
fn busy() -> ViewModel {
    view(busy_agents(), Some("@4"))
}

fn busy_agents() -> Vec<A<'static>> {
    let mut v = vec![
        a("main", 1, "failed", "Handy.app Speech Commands").with("workflow", "1"),
        a("main", 2, "needs-input", "Kua Yu focus timing").with("detail_kind", "ask").with("detail", "Which deck?"),
        a("main", 3, "done", "Commodities Job Tracker"),
        a("main", 4, "running", "Tmux Agent Sidebar"),
        a("main", 5, "running", "Notch Tasks Orchestrator"),
        a("main", 6, "idle", "Lost suit jacket"),
        a("cua-notch", 1, "needs-input", "Notch hover animation").with("detail_kind", "perm")
            .with("detail", "Bash git push origin main"),
        a("cua-notch", 2, "running", "Island resize spring"),
        a("cua-notch", 3, "running", "Census AX sweep").with("cua", "1"),
        a("cua-notch", 4, "running", "Invariants section 66"),
        a("cua-notch", 5, "idle", "Popup shadow"),
        a("schedule", 1, "running", "P0a multi-user execution"),
        a("schedule", 2, "running", "Prose extraction model"),
        a("schedule", 3, "idle", "Calendar sync"),
        a("schedule", 4, "idle", "ICS export"),
    ];
    for (s, n) in [("bai", 3), ("mackhaymond.co", 4), ("errands", 2)] {
        for i in 1..=n {
            v.push(a(s, i, "idle", "Some idle thing"));
        }
    }
    v
}

/// 90 agents, 12 needs, 9 spaces.
fn extreme() -> ViewModel {
    const NAMES: [&str; 9] = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india"];
    let mut v = Vec::new();
    let mut needs = 0;
    for s in NAMES {
        for i in 1..=10u32 {
            let st = if needs < 12 && i % 4 == 0 {
                needs += 1;
                "needs-input"
            } else if i % 3 == 0 {
                "running"
            } else {
                "idle"
            };
            v.push(a(s, i, st, "An agent with a fairly long title here"));
        }
    }
    view(v, Some("@5"))
}

fn empty() -> ViewModel {
    view(vec![], None)
}

fn all() -> Vec<(&'static str, ViewModel)> {
    vec![("quiet", quiet()), ("busy", busy()), ("extreme", extreme()), ("empty", empty())]
}

fn texts(f: &Frame) -> Vec<String> {
    f.rows.iter().map(Row::text).collect()
}

fn find(f: &Frame, needle: &str) -> usize {
    f.rows.iter().position(|r| r.text().contains(needle)).unwrap_or_else(|| panic!("{needle:?} not in {:#?}", texts(f)))
}

/// The agents section's text.
fn agents_part(f: &Frame) -> Vec<String> {
    texts(f)[f.agents.clone()].to_vec()
}

/// A tile's half-block pad row (`▀` below a list tile, `▄` / `▀` around
/// the usage panel).
fn is_pad(l: &str) -> bool {
    let t = l.trim();
    !t.is_empty() && t.chars().all(|c| c == '▀' || c == '▄')
}

/// A row's text as words: the arrow a space, runs of spaces one, padded
/// (` 1 main `, ` 2 cua-notch ` read as they would in prose).
fn uncapped(l: &str) -> String {
    format!(" {} ", l.replace(ARROW, " ").split(' ').filter(|w| !w.is_empty()).collect::<Vec<_>>().join(" "))
}

/// The cell column `needle` starts at in `l` (every glyph here is one cell).
fn col(l: &str, needle: &str) -> u16 {
    let i = l.find(needle).unwrap_or_else(|| panic!("{needle:?} not in {l:?}"));
    l[..i].chars().count() as u16
}

/// What an agent row says past its margin and state stripe.
fn body(l: &str) -> &str {
    l.trim_start_matches([' ', '▎', '▌']).trim_end()
}

/// The parked row as drawn: a rounded button.
fn parked(n: usize) -> String {
    if n == 0 { " PARKED none".to_string() } else { format!(" PARKED  {n} ") }
}

/// Every agent is covered exactly once, and `… N more` says N. (Rows a
/// toast card is drawn over still stand for their agents.)
fn check_accounting(name: &str, v: &ViewModel, f: &Frame) {
    let mut want: HashMap<(String, String), usize> = HashMap::new();
    for a in groups(v).iter().flat_map(|s| s.agents.iter()) {
        *want.entry((a.session.clone(), a.window_id.clone())).or_default() += 1;
    }
    let mut got: HashMap<(String, String), usize> = HashMap::new();
    for (y, r) in f.rows.iter().enumerate() {
        for c in &r.covers {
            *got.entry(c.clone()).or_default() += 1;
        }
        let t = r.text();
        let counted = t.contains(" more") || t.contains(" agents");
        if let Some(i) = t.find("… ").filter(|_| counted && !r.covers.is_empty() && !f.toast.contains(&y)) {
            let n: usize = t[i + "… ".len()..].split(' ').next().unwrap().parse().unwrap();
            assert_eq!(n, r.covers.len(), "{name} {}x{}: {t:?}", f.width, f.height);
        }
    }
    assert_eq!(got, want, "{name} {}x{}: {:#?}", f.width, f.height, texts(f));
}

/// The order every frame keeps: the panel (or title; nothing when the panel
/// would be empty), NEEDS YOU, agents, shells, spare rows, the spaces, the
/// parked row; sections never overlap; no line art anywhere but the toast
/// cards (drawn over the top, never over the last row).
fn check_order(name: &str, f: &Frame) {
    let h = usize::from(f.height);
    if h < 4 {
        return;
    }
    let t = texts(f);
    let shown = |y: usize| !f.toast.contains(&y);
    assert!(f.needs.end <= f.agents.start, "{name}: {:?} {:?} {t:#?}", f.needs, f.agents);
    assert!(f.agents.end <= f.spaces.start && f.spaces.end < h, "{name}: {:?} {:?} {t:#?}", f.agents, f.spaces);
    if !f.shells.is_empty() {
        assert!(f.agents.end <= f.shells.start && f.shells.end <= f.spaces.start, "{name}: shells {:?} {t:#?}", f.shells);
        assert!(!shown(f.shells.start) || t[f.shells.start].starts_with(" SHELLS"), "{name}: {t:#?}");
    }
    if !f.card.is_empty() {
        assert!(f.card.start == 0 && f.card.end <= f.needs.start, "{name}: card {:?} {t:#?}", f.card);
        assert!(f.status.start > f.card.start && f.status.end < f.card.end, "{name}");
        assert!(!shown(f.card.end - 1) || is_pad(&t[f.card.end - 1]), "{name}: the panel's bottom pad {t:#?}");
    }
    if !f.spaces.is_empty() {
        assert!(!shown(f.spaces.start) || t[f.spaces.start].trim().is_empty(), "{name}: the gap first {t:#?}");
        assert!(f.spaces.len() >= 2, "{name}: a gap and spaces {t:#?}");
        assert_eq!(f.spaces.end, f.parked.start, "{name}: PARKED right under the spaces {t:#?}");
        let vertical = t[f.spaces.start + 1].starts_with(" SPACES");
        assert!(!vertical || f.parked.len() >= 2, "{name}: a gap above PARKED under the SPACES list {t:#?}");
    }
    // PARKED last, its header always there (the stash, messages, the watcher).
    assert_eq!(f.parked.end, h, "{name}: {t:#?}");
    if f.toast.end <= f.parked.start {
        let header = f.parked.clone().find(|&y| t[y].starts_with(" PARKED")).unwrap_or_else(|| panic!("{name}: no PARKED {t:#?}"));
        assert!(f.parked.clone().take_while(|&y| y < header).all(|y| t[y].trim().is_empty()), "{name}: {t:#?}");
    }
    assert!(f.toast.end < h, "{name}: {t:#?}");
    assert!(!t.iter().any(|l| l.starts_with(" log") || l.contains('═')), "{name}: {t:#?}");
    for (y, l) in t.iter().enumerate().filter(|(y, _)| shown(*y)) {
        assert!(!l.contains(['─', '│', '╭', '╮', '╰', '╯']), "{name}: line art at {y}: {t:#?}");
    }
}

#[test]
fn every_size_fits_and_accounts_for_every_agent() {
    let mut sel = demo_bar();
    sel.selecting = Some(3);
    sel.prefix = true;
    // Toasts: a stack of three over the panel, a long one with the marks,
    // and a stack with no panel at all.
    let stack = vec![toast_n(1, Kind::Ok, "parked Canvas Syllabus"), toast_n(2, Kind::Error, LONG), toast_n(3, Kind::Warn, "hi")];
    let long = Bar { toasts: vec![toast(Kind::Error, LONG)], ..sel.clone() };
    let toasted = Bar { toasts: stack.clone(), ..demo_bar() };
    let bare = Bar { toasts: stack, ..Bar::default() };
    let bars = [Bar::default(), demo_bar(), sel, toasted, long, bare];
    let cases = |lettered: bool| -> Vec<(&Bar, u16)> {
        if lettered {
            // (The toasts at a card's narrowest and a wide width; the
            // stack at the usual one too.)
            let widths = |b: &Bar| match b.toasts.len() {
                1 => vec![16u16, 60],
                0 => vec![24u16, 30, 38, 60],
                _ => vec![16u16, 34, 60],
            };
            bars.iter().flat_map(|b| widths(b).into_iter().map(move |w| (b, w))).collect()
        } else {
            [24u16, 26, 30, 34, 45].map(|w| (&bars[0], w)).to_vec()
        }
    };
    for (name, v, l) in all().into_iter().flat_map(|(n, v)| [(n, v.clone(), false), (n, lettered(v), true)]) {
        for (bar, w) in cases(l) {
            for h in 2u16..=70 {
                let f = layout(&v, w, h, &with_bar(bar.clone()));
                assert_eq!(f.rows.len(), usize::from(h), "{name} {w}x{h}");
                for r in &f.rows {
                    assert_eq!(width(&r.segs), usize::from(w), "{name} {w}x{h}: {:?}", r.text());
                    assert!(r.targets.iter().all(|(a, b, _)| a < b && *b <= w), "{name} {w}x{h}");
                }
                check_accounting(name, &v, &f);
                check_order(&format!("{name} {w}x{h}"), &f);
                let mut buf = Buffer::empty(Rect::new(0, 0, w, h));
                paint(&f, &mut buf);
                // A toast drawn is a toast clickable, and every one that
                // fits is drawn: a card from 4 rows, its corners in place.
                let t = texts(&f);
                for y in f.toast.clone() {
                    assert!(f.rows[y].targets.iter().any(|t| matches!(t.2, Target::Toast(_))), "{name} {w}x{h}: {y}");
                }
                assert_eq!(f.toast.is_empty(), f.toast_until.is_none(), "{name} {w}x{h}");
                // (Under the marks and the hint, which stay on top.)
                let free = usize::from(h) - f.toast.start;
                assert_eq!(f.toast.is_empty(), bar.toasts.is_empty() || free < 2, "{name} {w}x{h}: {t:#?}");
                if f.toast.len() > 1 {
                    let (a, b) = (&t[f.toast.start], &t[f.toast.end - 1]);
                    assert!(a.starts_with(" ╭─") && a.ends_with("─╮ ") && b.starts_with(" ╰─"), "{name} {w}x{h}: {t:#?}");
                }
                if bar.prefix && !f.toast.is_empty() {
                    assert!(t[..f.toast.start].iter().any(|l| l.contains("prefix")), "{name} {w}x{h}: {t:#?}");
                }
            }
        }
    }
}

#[test]
fn needs_come_first() {
    for (name, v) in all() {
        for h in [10u16, 15, 20, 58] {
            let f = layout(&v, 34, h, &Opts::default());
            let first = f.rows[1..].iter().map(Row::text).find(|t| !t.trim().is_empty()).unwrap();
            assert!(first.starts_with(" NEEDS YOU"), "{name} {h}: {first:?}");
        }
    }
}

/// Agents lose detail last: as the height shrinks, the spaces give up pill
/// lines first, then the gaps, the pads and the card lines; agents keep
/// their full detail until all of that is gone, and their step only grows.
#[test]
fn agents_lose_detail_last() {
    for (name, v) in all() {
        let mut last = (0u8, 0u8);
        // (Under 8 rows the spaces' reserve itself gives way: not a ladder.)
        for h in (8u16..=110).rev() {
            let f = layout(&v, 34, h, &Opts::default());
            let now = (f.step, f.agents_step);
            assert!(now >= last, "{name} {h}: {now:?} after {last:?}");
            if f.agents_step > 0 {
                // Compressed agents: no agent pads, one-line cards (spare
                // rows may still buy gaps and the cards' pads back).
                assert_eq!(f.step, STEPS.len() as u8, "{name} {h}");
                let cards = f.rows[f.needs.clone()].iter().filter(|r| r.bg.is_some()).count();
                assert!(cards <= v.needs.len(), "{name} {h}: {:#?}", texts(&f));
                assert!(!texts(&f)[f.agents.clone()].iter().any(|l| is_pad(l)), "{name} {h}: {:#?}", texts(&f));
            }
            if f.step == 0 && f.spaces.len() < 1 + pill_lines(&v, 34) {
                // Rows short at step 0: the spaces gave them up first.
                assert!(f.agents.end + 2 + f.spaces.len() >= usize::from(h) - 1, "{name} {h}: {:#?}", texts(&f));
            }
            last = now;
        }
    }
}

/// Rows a frame's step leaves over are spent on spacing, never on detail:
/// while blank rows sit unused above the spaces, no section gap is missing
/// (above AGENTS, SHELLS, NEEDS YOU), the NEEDS YOU cards have their pads
/// unless they cost more than is left, and so do the gaps between spaces.
/// The agents' pads never come back past the step that dropped them, and
/// every agent row keeps one shape.
#[test]
fn spare_rows_buy_spacing_back() {
    let bars = [Bar::default(), demo_bar(), Bar { prefix: true, ..demo_bar() }];
    let mut bought = 0;
    for (name, v) in all().into_iter().flat_map(|(n, v)| [(n, v.clone()), (n, lettered(v))]) {
        for bar in &bars {
            for w in [30u16, 34, 38] {
                for h in 8u16..=100 {
                    let f = layout(&v, w, h, &with_bar(bar.clone()));
                    let t = texts(&f);
                    let ctx = format!("{name} {w}x{h}");
                    let end = f.needs.end.max(f.agents.end).max(f.shells.end);
                    let stop = if f.spaces.is_empty() { usize::from(h) - 1 } else { f.spaces.start };
                    assert!(f.rows[end..stop].iter().all(is_blank), "{ctx}: {t:#?}");
                    let spare = stop - end;
                    let gap_above = |r: &std::ops::Range<usize>| r.is_empty() || r.start == 0 || is_blank(&f.rows[r.start - 1]);
                    let gaps = [gap_above(&f.needs), gap_above(&f.agents), gap_above(&f.shells)];
                    if spare > 0 {
                        assert_eq!(gaps, [true; 3], "{ctx}: a blank run at the bottom, a section gap missing: {t:#?}");
                    }
                    // The cards: all padded, or the pads cost more than is left.
                    let cards = f.rows[f.needs.clone()].iter().filter(|r| r.covers.is_empty() && r.target_at(0).is_some()).count();
                    let cut = t[f.needs.clone()].iter().any(|l| l.contains("need you"));
                    let pads = t[f.needs.clone()].iter().filter(|l| is_pad(l)).count();
                    let n = v.needs.len();
                    assert!(pads == 0 || pads == n, "{ctx}: {t:#?}");
                    if n > 0 && !cut && pads == 0 {
                        assert!(spare < n, "{ctx}: {cards} card rows unpadded with {spare} spare: {t:#?}");
                    }
                    // The spaces: a gap above every header but the first, or not enough left for all.
                    let missing = group_gaps(&f.rows[f.agents.clone()], usize::from(w));
                    if missing > 0 {
                        assert!(spare < missing, "{ctx}: {t:#?}");
                    }
                    // Never the agents' pads past the step that dropped them.
                    let agent_pads = t[f.agents.clone()].iter().any(|l| is_pad(l));
                    assert!(!agent_pads || STEPS.get(usize::from(f.step)).is_some_and(|s| s.pads), "{ctx}: {t:#?}");
                    bought += usize::from(f.step > 0 && gaps == [true; 3]);
                }
            }
        }
    }
    assert!(bought > 0, "spacing never bought back");
    // A frame that asked for it (34x40, the shells in one line, rows left
    // over): the gap above AGENTS is back and the one above PARKED, nothing
    // blank at the bottom; the agents keep their shape (no pads, zebra).
    let f = layout(&lettered(quiet()), 34, 40, &with_bar(demo_bar()));
    let t = texts(&f);
    assert_eq!((f.step, f.agents_step, f.shells.len()), (STEPS.len() as u8 - 1, 0, 1), "{t:#?}");
    assert!(is_blank(&f.rows[f.agents.start - 1]) && is_blank(&f.rows[f.parked.start]), "{t:#?}");
    assert!(f.shells.end == f.spaces.start && !t[f.agents.clone()].iter().any(|l| is_pad(l)), "{t:#?}");
}

/// The agents section's (first lines, second lines): a row that clicks
/// through to an agent and stands for it, and one that clicks through
/// without standing for anyone (its second line).
fn agent_lines(f: &Frame) -> (usize, usize) {
    let go = |y: usize| f.rows[y].target_at(0).is_some_and(|x| matches!(x, Target::Go { .. }));
    let firsts = f.agents.clone().filter(|&y| go(y) && f.rows[y].covers.len() == 1).count();
    let seconds = f.agents.clone().filter(|&y| go(y) && f.rows[y].covers.is_empty()).count();
    (firsts, seconds)
}

/// Every agent row in a frame is the same height: all two lines or all one.
#[test]
fn agent_rows_are_one_height() {
    for (name, v) in all() {
        for w in [24u16, 30, 34, 44] {
            for h in 2u16..=110 {
                let f = layout(&v, w, h, &Opts::default());
                let (firsts, seconds) = agent_lines(&f);
                assert!(seconds == 0 || seconds == firsts, "{name} {w}x{h}: {:#?}", texts(&f));
                if w >= 26 && f.agents_step <= 1 {
                    assert_eq!(seconds, firsts, "{name} {w}x{h}: {:#?}", texts(&f));
                }
            }
        }
    }
}

/// With a panel and shells too, a shrinking frame only ever gives detail
/// up: the step, the agents' step and the shells' form (section → one
/// line → none) never come back while the panel keeps its size (the panel
/// has its own ladder: the meters, then the panel, then usage go, and the
/// rows that frees may well buy the sections detail back).
#[test]
fn shrinking_never_brings_detail_back() {
    let bars = [demo_bar(), Bar { usage: vec![], stale: None, ..demo_bar() }, Bar { prefix: true, ..demo_bar() }];
    for (name, v) in all() {
        for bar in &bars {
            for w in [30u16, 34, 44] {
                let mut last = (0u8, 0u8, 0u8);
                let mut top = usize::MAX;
                for h in (12u16..=110).rev() {
                    let f = layout(&v, w, h, &with_bar(bar.clone()));
                    if f.status.end + f.card.len() != top {
                        (top, last) = (f.status.end + f.card.len(), (0, 0, 0));
                    }
                    let form = match f.shells.len() {
                        0 => 2,
                        1 => 1,
                        _ => 0,
                    };
                    let now = (f.step, f.agents_step, form);
                    assert!(now.0 >= last.0 && now.1 >= last.1 && now.2 >= last.2, "{name} {w}x{h}: {now:?} after {last:?} {:#?}", texts(&f));
                    last = now;
                }
            }
        }
    }
}

#[test]
fn quiet_is_roomy() {
    let v = quiet();
    let f = layout(&v, 34, 58, &Opts::default());
    let t = texts(&f);
    assert_eq!((f.step, f.agents_step), (0, 0), "{t:#?}");
    assert!(t[0].starts_with(" agents") && t[0].ends_with("◉1 ◐2 ○8 "), "{:?}", t[0]);
    // The count: a small yellow pill.
    assert!(t[1].trim().is_empty() && t[2].starts_with(" NEEDS YOU") && t[2].ends_with(" 1  "), "{t:#?}");
    // The card: a tile tinted in its state's colour with a yellow stripe;
    // title, glyph and age; then the question; then its pad.
    assert!(t[3].starts_with(" ▌ Kua Yu focus timing") && t[3].ends_with("◉ 3m  "), "{:?}", t[3]);
    assert!(t[4].starts_with(" ▌ Which deck should I pull"), "{:?}", t[4]);
    assert_eq!((f.rows[3].bg, f.rows[4].bg), (Some(Ink::Mix("yellow", 14)), Some(Ink::Mix("yellow", 14))));
    assert_eq!((f.rows[3].segs[0].clone(), f.rows[3].segs[1].clone()), (bare(1), seg("▌", "yellow")));
    assert_eq!(t[5], format!(" {} ", "▀".repeat(32)));
    assert!(f.rows[5].segs.contains(&seg("▀".repeat(32), Ink::Mix("yellow", 14))) && f.rows[5].bg.is_none());
    // Then a gap, and the agents right under it.
    assert!(t[6].trim().is_empty() && t[7].starts_with(" AGENTS") && t[7].ends_with("all spaces "), "{t:#?}");
    assert_eq!(f.agents.start, 7);
    // Space headers: on the canvas, a dot and the name, the counts on the right.
    let main = find(&f, " ● main");
    assert!(t[main].ends_with("◉1 ◐2 ○4 ") && f.rows[main].bg.is_none(), "{:?}", t[main]);
    // The client's agent: lifted (`tile_hi`), a wide stripe in its state's
    // colour, the spinner and its word; its message under it with the age;
    // its pad under that, in its colour. A canvas margin either side.
    let r = find(&f, "Tmux Agent Sidebar");
    assert!(t[r].starts_with(" ▌ Tmux Agent Sidebar") && t[r].ends_with("⠋ working  "), "{:?}", t[r]);
    assert!(t[r + 1].starts_with(" ▌   Edit agent-roster.py") && t[r + 1].ends_with(" 2m  "), "{:?}", t[r + 1]);
    assert_eq!((f.rows[r].bg, f.rows[r + 1].bg), (Some(Ink::Pal("tile_hi")), Some(Ink::Pal("tile_hi"))));
    assert_eq!(f.rows[r].segs[1], seg("▌", "blue"));
    assert_eq!((f.rows[r].segs[0].clone(), f.rows[r].segs.last().cloned()), (bare(1), Some(bare(1))));
    assert!(is_pad(&t[r + 2]) && f.rows[r + 2].segs.contains(&seg("▀".repeat(32), "tile_hi")), "{t:#?}");
    // A title gives way to the state word (and the flag stays); working:
    // a blue stripe on a plain tile.
    let h = find(&f, "Handy.app Speech… ⚙");
    assert!(t[h].ends_with("⠋ working  ") && t[h + 1].starts_with(" ▎   Bash swift build"), "{t:#?}");
    assert_eq!((f.rows[h].bg, f.rows[h].segs[1].clone()), (Some(Ink::Pal("tile")), seg("▎", "blue")));
    // Recently done: the word, its message, a green stripe.
    let c = find(&f, "Commodities Job Track");
    assert!(t[c].ends_with("✓ done  ") && t[c + 1].starts_with(" ▎   7 fixes applied"), "{t:#?}");
    assert_eq!(f.rows[c].segs[1], seg("▎", "green"));
    // Idle: two lines like every agent, no stripe, the word on the first,
    // the age under it; then its pad (no target, stands for no one).
    let n = find(&f, "Lost suit jacket");
    assert!(t[n].starts_with("   Lost suit") && t[n].ends_with(" idle  ") && t[n + 1].ends_with(" 6m  "), "{t:#?}");
    assert_eq!(t[n + 2], format!(" {} ", "▀".repeat(32)));
    assert!(f.rows[n + 2].targets.is_empty() && f.rows[n + 2].covers.is_empty() && f.rows[n + 3].covers.len() == 1, "{t:#?}");
    // Every tile has its pad: the section is headers, tiles and pads (and
    // the gaps between spaces), nothing else.
    for y in f.agents.clone().skip(1) {
        let r = &f.rows[y];
        assert!(r.text().trim().is_empty() || is_pad(&t[y]) || r.bg == Some(Ink::Pal("tile")) || r.bg == Some(Ink::Pal("tile_hi"))
            || t[y].starts_with(" ● "), "{y}: {t:#?}");
    }
    assert!(t[find(&f, "Notch Tasks Orchestrator")].ends_with(" idle  "), "{t:#?}");
    // A blank row between groups (after the last tile's pad), in ⌃N (here:
    // name) order.
    let bai = find(&f, " ● bai");
    assert!(t[main - 1].trim().is_empty() && is_pad(&t[main - 2]) && bai < main, "{t:#?}");
    // SPACES: a gap, its header and a row per space in ⌃N (here: name)
    // order, each with its counts; then a gap and PARKED with its count and
    // the parked tab. Spare rows above them.
    assert_eq!((f.spaces.clone(), f.parked.clone()), (50..55, 55..58), "{t:#?}");
    assert!(t[50].trim().is_empty() && t[51].trim_end() == " SPACES", "{t:#?}");
    assert!(t[52].contains(" bai ") && t[52].ends_with("○2  ") && t[54].contains(" schedule "), "{t:#?}");
    // The client's space: `@space_here`, one chip (peach: no colour of its
    // own) ending in the arrow.
    let peach = Ink::Pal("peach");
    assert!(f.rows[53].segs.contains(&block(" main ", "crust", peach)) && f.rows[53].segs.contains(&seg(ARROW, peach)), "{:?}", f.rows[53].segs);
    assert!(t[53].ends_with("◉1 ◐2 ○4  "), "{t:#?}");
    assert!(t[f.agents.end..50].iter().all(|l| l.trim().is_empty()), "{t:#?}");
    assert!(t[55].trim().is_empty() && t[56].starts_with(&parked(1)), "{t:#?}");
    assert!(t[57].contains("⏸ Parked one"), "{t:#?}");
    assert!(!f.spin || t.iter().any(|l| l.contains('⠋')));
}

#[test]
fn busy_squeezes_spaces_and_cards_before_agents() {
    let v = busy();
    let at = |h: u16| layout(&v, 34, h, &Opts::default());
    let first = |p: &dyn Fn(&Frame) -> bool| (20..120).rev().map(at).find(|f| p(f)).unwrap();
    // 24 agents, two lines each and a pad under each: full detail in 120
    // rows, gaps and all.
    let f = at(120);
    assert!(f.step == 0 && texts(&f).iter().any(|l| is_pad(l)), "{:#?}", texts(&f));
    // Shorter: the gaps go, then the cards' pads, then the agents' (the
    // cards still two lines), all before any agent loses detail.
    let f = first(&|f| f.step > 0);
    assert_eq!(f.agents_step, 0, "{:#?}", texts(&f));
    let f = first(&|f| !texts(f)[f.needs.clone()].iter().any(|l| is_pad(l)));
    assert!(texts(&f)[f.agents.clone()].iter().any(|l| is_pad(l)) && f.agents_step == 0, "{:#?}", texts(&f));
    let f = first(&|f| !texts(f)[f.agents.clone()].iter().any(|l| is_pad(l)));
    let card_lines = f.rows[f.needs.clone()].iter().filter(|r| r.bg.is_some()).count();
    assert_eq!((f.step, f.agents_step, card_lines), (3, 0, 2 * v.needs.len()), "{:#?}", texts(&f));
    // Without pads, adjacent tiles alternate the zebra's two shades.
    let bgs: Vec<Option<Ink>> = f.rows[f.agents.clone()].iter().filter(|r| r.covers.len() == 1).map(|r| r.bg).collect();
    assert!(bgs.contains(&Some(Ink::Pal("tile"))) && bgs.contains(&Some(Ink::Pal("tile_alt"))), "{bgs:?}");
    // Shorter still: the agents compress, every one of them the same height.
    for step in [1, 2, 3] {
        let f = (20..90).rev().map(at).find(|f| f.agents_step == step).unwrap();
        let t = texts(&f);
        assert_eq!(f.step, STEPS.len() as u8, "{t:#?}");
        // All four needs still on screen, in agent-jump.sh order (failed first).
        let fail = find(&f, "Handy.app");
        assert!(fail < find(&f, "Notch hover") && fail < f.agents.start);
        for title in ["Kua Yu focus timing", "Commodities Job Tracker"] {
            assert!(t[f.needs.clone()].iter().any(|l| l.contains(title)), "{title}");
        }
        let (firsts, seconds) = agent_lines(&f);
        assert_eq!(seconds, if step == 1 { firsts } else { 0 }, "{step}: {t:#?}");
    }
    // In space scope (main only) the same fleet keeps its full detail,
    // full cards and every pill.
    let mut sv = busy();
    sv.scope.session = Some("main".into());
    let f = layout(&sv, 34, 46, &Opts::default());
    assert_eq!((f.step, f.agents_step), (0, 0), "{:#?}", texts(&f));
    // Every space a row of SPACES (after its gap and header).
    assert_eq!(f.spaces.len(), 2 + space_list(&sv).len(), "{:#?}", texts(&f));
}

#[test]
fn extreme_cuts_and_counts() {
    let v = extreme();
    // 34x26: NEEDS YOU keeps a third (cards and a count); spaces collapse
    // but the first, which shows what it can and counts the rest. The pills
    // keep the client's space and the alert ones, `+K` for the rest.
    let f = layout(&v, 34, 26, &Opts::default());
    let t = texts(&f);
    assert_eq!((f.step, f.agents_step), (STEPS.len() as u8, 4), "{t:#?}");
    assert!(t[f.needs.clone()].last().unwrap().contains("more need you"), "{t:#?}");
    assert!(t[f.agents.clone()].iter().any(|l| l.starts_with(" … ") && l.contains(" more")), "{t:#?}");
    let pills: Vec<String> = t[f.spaces.start + 1..f.spaces.end].iter().map(|l| uncapped(l)).collect();
    assert!(pills.iter().any(|l| l.contains(" alpha ")) && pills.iter().any(|l| l.contains(" +")), "{t:#?}");
    check_accounting("extreme", &v, &f);
    // Shorter: more spaces than rows, the rest counted.
    let f = layout(&v, 34, 13, &Opts::default());
    let t = texts(&f);
    assert_eq!(f.agents_step, 5, "{t:#?}");
    assert!(t.iter().any(|l| l.contains("more need you")), "{t:#?}");
    assert!(t.iter().any(|l| l.contains("more in")), "{t:#?}");
    // `… N more` opens the menu on All; `… N more need you` the default tab.
    let more = find(&f, "more in") as u16;
    assert_eq!(f.target(3, more), Some(&Target::Menu(Some("all"))));
    let needs_more = find(&f, "more need you") as u16;
    assert_eq!(f.target(3, needs_more), Some(&Target::Menu(None)));
    check_accounting("extreme", &v, &f);
    // The spaces never go: the gap and a pill line with the client's space.
    assert!(f.spaces.len() >= 2 && uncapped(&t[f.spaces.start + 1]).contains(" alpha "), "{t:#?}");
    // With more room the spaces collapse, quietest first.
    let f = layout(&v, 34, 70, &Opts::default());
    assert!(f.agents_step >= 4, "{:#?}", texts(&f));
}

#[test]
fn empty_still_has_every_section() {
    let f = layout(&empty(), 34, 30, &Opts::default());
    let t = texts(&f);
    let n = find(&f, " NEEDS YOU");
    assert!(t[n].ends_with("✓ all clear "), "{:?}", t[n]);
    assert!(t.iter().any(|l| l.starts_with("   no agents running")));
    assert_eq!(t[0], " agents                           ");
    // The plain `main` session is still a space: its row.
    let s = &t[f.spaces.clone()];
    assert!(s[1].trim_end() == " SPACES" && s[2].contains(" main "), "{t:#?}");
}

#[test]
fn click_targets() {
    let v = quiet();
    let f = layout(&v, 34, 58, &Opts::default());
    let go = |win: &str| Some(Target::Go { win: win.into(), session: "main".into() });
    // Both lines of an agent tile, anywhere (its margins too), and both
    // lines of the card; never their pads.
    let r = find(&f, "Tmux Agent Sidebar") as u16;
    assert_eq!(f.target(5, r).cloned(), go("@2"));
    assert_eq!(f.target(0, r + 1).cloned(), go("@2"));
    assert_eq!(f.target(33, r + 1).cloned(), go("@2"));
    assert_eq!(f.target(5, r + 2), None);
    let card = find(&f, "▌ Kua Yu") as u16;
    for y in card..card + 2 {
        assert_eq!(f.target(0, y).cloned(), go("@3"));
        assert_eq!(f.target(33, y).cloned(), go("@3"));
    }
    assert_eq!(f.target(5, card + 2), None);
    // Space headers and SPACES rows (anywhere on them) go to the session.
    let hd = find(&f, " ● bai") as u16;
    assert_eq!(f.target(20, hd).cloned(), Some(Target::Session("bai".into())));
    let s = f.spaces.start as u16;
    for (y, name) in [(s + 2, "bai"), (s + 3, "main"), (s + 4, "schedule")] {
        assert_eq!((f.target(0, y), f.target(33, y)), (Some(&Target::Session(name.into())), Some(&Target::Session(name.into()))));
    }
    // The gaps, the section headers and blank rows do nothing.
    assert_eq!(f.target(5, s), None);
    assert_eq!(f.target(5, s + 1), None);
    assert_eq!(f.target(2, find(&f, " NEEDS YOU") as u16), None);
    assert_eq!(f.target(2, 1), None);
    // PARKED (its header and its tabs) opens the stash picker.
    assert_eq!((f.target(2, 56), f.target(20, 57)), (Some(&Target::Stash), Some(&Target::Stash)));
    // A watcher problem is a restart button, on the PARKED header.
    let mut v = quiet();
    v.watcher_age = Some(99.0);
    let f = layout(&v, 34, 58, &Opts::default());
    let p = find(&f, " PARKED") as u16;
    assert!(f.rows[p as usize].text().ends_with("watcher stalled 99s "));
    assert_eq!(f.target(30, p), Some(&Target::Restart));
    // A message wins the corner (and is not a button).
    let f = layout(&v, 34, 58, &Opts { msg: Some("no tmux client".into()), ..Opts::default() });
    assert!(f.rows[p as usize].text().ends_with("no tmux client "));
    assert_eq!(f.target(30, p), None);
    assert_eq!(f.target(2, p), Some(&Target::Stash));
}

/// The second line: the latest message only for working, asking and
/// (recently) done agents; markup is plain text; never `~/` or `project/`.
#[test]
fn titles_and_second_lines() {
    let old = (NOW - 3600).to_string();
    let v = view(
        vec![
            a("main", 1, "running", "~/Commodities Job Tracker").with("detail_kind", "done")
                .with("detail", "**Proof passed.** see [log](http://x)"),
            a("main", 2, "running", "tmux/Tmux Agent Sidebar").with("detail_kind", "run")
                .with("detail", "<agent-message from=a>`cargo test`</agent-message>"),
            a("main", 3, "idle", "~/Lost suit jacket").with("detail_kind", "done").with("detail", "## Found it"),
            a("main", 5, "running", "Markup only").with("detail_kind", "run").with("detail", "<agent-message from=x>"),
            a("main", 4, "needs-input", "~/Kua Yu focus timing").with("detail_kind", "ask")
                .with("detail", "Which **deck**?"),
            a("main", 6, "idle", "Long ago").with("detail_kind", "done").with("detail", "Old news")
                .with("since", &format!("{old} idle")),
        ],
        None,
    );
    let f = layout(&v, 60, 58, &Opts::default());
    let t = texts(&f);
    let at = |s: &str| f.agents.start + agents_part(&f).iter().position(|l| l.contains(s)).unwrap_or_else(|| panic!("{s}: {t:#?}"));
    // Working with last turn's outcome: a second line with just the age,
    // never the old outcome; then its pad, then the next agent.
    let c = at("Commodities Job Tracker");
    assert!(!t[c].contains("~/") && body(&t[c + 1]) == "1m" && is_pad(&t[c + 2]) && f.rows[c + 3].covers.len() == 1, "{t:#?}");
    // Working with what it runs: plain text under it; `project/` dropped.
    let r = at("Tmux Agent Sidebar");
    assert!(!t[r].contains("tmux/") && t[r + 1].starts_with(" ▎   cargo test "), "{:?}", t[r + 1]);
    let m = at("Markup only");
    assert!(body(&t[m + 1]) == "5m", "{t:#?}");
    // Recently done (idle, 4 min): its outcome as plain text.
    let l = at("Lost suit jacket");
    assert!(t[l].contains("✓ done") && t[l + 1].starts_with(" ▎   Found it "), "{:?}", t[l + 1]);
    // Long done: idle, no stripe, its last outcome under it with the age.
    let g = at("Long ago");
    assert!(t[g].ends_with(" idle  ") && t[g + 1].starts_with("     Old news") && t[g + 1].ends_with(" 1h  "), "{t:#?}");
    // Asking: the question.
    let k = at("Kua Yu");
    assert!(t[k].contains("◉ asks") && t[k + 1].starts_with(" ▎   Which deck?"), "{t:#?}");
    // Needs cards: no `~/`, plain reasons.
    assert!(t.iter().any(|l| l.starts_with(" ▌ Kua Yu focus timing")), "{t:#?}");
    assert!(t.iter().any(|l| l.starts_with(" ▌ Which deck?")), "{t:#?}");
}

#[test]
fn tiny_heights() {
    // No room for sections: one row still counts every agent.
    for h in 2u16..=3 {
        let f = layout(&quiet(), 34, h, &Opts::default());
        let y = f.rows.iter().position(|r| r.text().starts_with(" … 11 ")).unwrap_or_else(|| panic!("{h}: {:#?}", texts(&f)));
        assert_eq!(f.target(3, y as u16), Some(&Target::Menu(Some("all"))));
    }
    // Nothing pending in one row: the header saying so, never ` … 0 need you`.
    let cx = Cx { w: 34, b: Badge::OFF, now: NOW as f64, spin: 0 };
    assert_eq!(needs_cut(&empty(), cx, 1).iter().map(Row::text).collect::<Vec<_>>(), [needs_header(&empty(), 34).text()]);
}

#[test]
fn cut_pills_keep_the_current_and_attention_spaces() {
    // 12 spaces in a short frame; the client is in the last one by name,
    // and one other in the middle needs you.
    let mut v = Vec::new();
    for (k, s) in ["s01", "s02", "s03", "s04", "s05", "s06", "s07", "s08", "s09", "s10", "s11", "zz"].iter().enumerate() {
        v.push(a(s, 1, if k == 6 { "needs-input" } else { "idle" }, "t"));
    }
    let mut v = view(v, Some("@12"));
    v.scope.session = Some("zz".into());
    for h in 7u16..=40 {
        let f = layout(&v, 34, h, &Opts::default());
        let t = texts(&f);
        let pills = uncapped(&t[f.spaces.start + 1..f.spaces.end].join("|"));
        assert!(pills.contains(" zz "), "{h}: {t:#?}");
        assert!(h < 8 || pills.contains(" s07 "), "{h}: {t:#?}");
        let shown = pills.matches(" s0").count() + pills.matches(" s1").count() + pills.matches(" zz ").count() + pills.matches(" main ").count();
        if shown < 13 {
            // The rest as `+K`, which opens the menu on All.
            let k: usize = pills.split(" +").nth(1).unwrap().split(' ').next().unwrap().parse().unwrap();
            assert_eq!(shown + k, 13, "{h}: {pills:?}");
            let y = (f.spaces.start + 1..f.spaces.end).find(|&y| t[y].contains('+')).unwrap();
            assert_eq!(f.target(col(&t[y], "+"), y as u16), Some(&Target::Menu(Some("all"))));
        }
        check_accounting(&format!("cut {h}"), &v, &f);
    }
}

/// ONE space order: the pills and the agents' groups both go by
/// `@space_index` (⌃N), unindexed spaces after by name; hidden spaces have
/// no pill. A click on a pill switches the client to that space.
#[test]
fn groups_and_pills_share_the_space_order() {
    let v = view(
        vec![
            a("main", 1, "running", "M").with("space_index", "1"),
            a("bai", 1, "idle", "B").with("space_index", "3"),
            a("cua-notch", 1, "needs-input", "C").with("space_index", "2"),
            a("zeta", 1, "idle", "Z").with("space_index", "12"),
            a("aaa", 1, "idle", "A"),
            a("hidden", 1, "idle", "H").with("space_hidden", "1"),
        ],
        Some("@1"),
    );
    let f = layout(&v, 60, 58, &Opts::default());
    let t = texts(&f);
    let group_order: Vec<String> = agents_part(&f)
        .iter()
        .filter_map(|l| l.strip_prefix(" ● ").map(|r| r.split_whitespace().next().unwrap().to_string()))
        .collect();
    let pills = uncapped(&t[f.spaces.start + 1..f.spaces.end].join(" "));
    let pill_order: Vec<&str> = ["main", "bai", "cua-notch", "zeta", "aaa", "hidden"]
        .into_iter()
        .filter(|n| pills.contains(&format!(" {n} ")))
        .collect::<Vec<_>>();
    let mut by_pos = pill_order.clone();
    by_pos.sort_by_key(|n| pills.find(&format!(" {n} ")).unwrap());
    assert_eq!(by_pos, ["main", "cua-notch", "bai", "zeta", "aaa"], "{pills:?}");
    // The groups: the same order (the hidden space still lists its agent, last-but-none: by name).
    let want: Vec<&str> = ["main", "cua-notch", "bai", "zeta", "aaa", "hidden"].to_vec();
    let shown: Vec<&str> = group_order.iter().map(String::as_str).filter(|g| *g != "hidden").collect();
    assert_eq!(shown, by_pos, "{t:#?}");
    assert!(group_order.iter().map(String::as_str).eq(want.iter().copied().filter(|g| group_order.iter().any(|x| x == g))));
    // The ⌃N digit leads each indexed pill; past 9 or unset, none.
    assert!(pills.contains(" 1 main ") && pills.contains(" 2 cua-notch ") && pills.contains(" 3 bai "), "{pills:?}");
    assert!(!pills.contains("12") && pills.contains(" zeta "), "{pills:?}");
    let y = f.spaces.clone().find(|&y| t[y].contains("cua-notch")).unwrap() as u16;
    assert_eq!(f.target(col(&t[y as usize], "cua-notch"), y), Some(&Target::Session("cua-notch".into())));
    // The same holds for the real fleet in its real (live) registry order.
    let order = |v: &ViewModel| groups(v).iter().filter(|s| !s.hidden).map(|s| s.name.clone()).collect::<Vec<_>>();
    let pills = |v: &ViewModel| space_list(v).iter().filter(|s| !s.agents.is_empty()).map(|s| s.name.clone()).collect::<Vec<_>>();
    assert_eq!(order(&v), pills(&v));
}

#[test]
fn spaces_wear_their_colour() {
    let v = view(
        vec![
            a("main", 1, "running", "M").with("space_index", "1").with("space_color", "#f9e2af"),
            a("bai", 1, "idle", "B").with("space_index", "2").with("space_color", "colour24"),
            a("zeta", 1, "idle", "Z").with("space_index", "3").with("space_color", "bogus"),
        ],
        Some("@1"),
    );
    let f = layout(&v, 34, 40, &Opts::default());
    let row = |f: &Frame, name: &str| f.rows[f.spaces.clone().find(|&y| f.rows[y].text().contains(&format!(" {name} "))).unwrap()].clone();
    // The client's space: `@space_here`, one chip in its colour, dark ink,
    // the arrow tail.
    let yellow = Ink::Rgb(0xf9, 0xe2, 0xaf);
    let p = row(&f, "main");
    assert!(p.segs.contains(&block(" 1 main ", "crust", yellow)) && p.segs.contains(&seg(ARROW, yellow)), "{:?}", p.segs);
    // Another: a status-line module, its digit on its colour (dark ink on a
    // light one, light on a dark one; peach for a colour tmux cannot read).
    assert!(row(&f, "bai").segs.contains(&block(" 2 ", "text", Ink::Idx(24))), "{:?}", row(&f, "bai").segs);
    assert!(row(&f, "zeta").segs.contains(&block(" 3 ", "crust", "peach")), "{:?}", row(&f, "zeta").segs);
    // Space headers: the dot in the space's colour.
    let hd = &f.rows[find(&f, " ● bai")];
    assert_eq!(hd.segs[1], seg("● ", Ink::Idx(24)));
    // An alert space's row counts it; a current space with no colour is
    // peach (the status line's), red while the prefix is armed.
    let v = view(vec![a("main", 1, "running", "M"), a("bai", 1, "needs-input", "B")], Some("@1"));
    let f = layout(&v, 34, 40, &Opts::default());
    assert!(row(&f, "bai").text().contains("◉1"), "{:?}", row(&f, "bai").text());
    assert!(row(&f, "main").segs.contains(&block(" main ", "crust", "peach")), "{:?}", row(&f, "main").segs);
    let f = layout(&v, 34, 40, &with_bar(Bar { prefix: true, ..Bar::default() }));
    assert!(row(&f, "main").segs.contains(&block(" main ", "crust", "red")), "{:?}", row(&f, "main").segs);
}

#[test]
fn folding_and_collapsing() {
    let v = busy();
    // Idle folded first, the rows still two lines each.
    let f = (20..90).rev().map(|h| layout(&v, 34, h, &Opts::default())).find(|f| f.agents_step == 1).unwrap();
    let t = texts(&f);
    assert!(t.iter().any(|l| l.contains("○○○ 3 idle · ")), "{t:#?}");
    // The fold: a one-row tile that opens the menu on its Idle tab.
    let fold = find(&f, "○○○ 3 idle · ");
    assert_eq!(f.target(5, fold as u16), Some(&Target::Menu(Some("idle"))));
    assert!(matches!(f.rows[fold].bg, Some(Ink::Pal("tile" | "tile_alt"))), "{:?}", f.rows[fold].bg);
    // The client's window is never folded away.
    assert!(t.iter().any(|l| l.contains("Tmux Agent Sidebar")));
    // Squeezed further: whole spaces collapse, quietest first; main keeps its rows longest.
    let f = (10..58).rev().map(|h| layout(&v, 34, h, &Opts::default())).find(|f| f.agents_step == 4).unwrap();
    let t = texts(&f);
    assert!(t[f.agents.clone()].iter().any(|l| l.contains("Kua Yu focus timing")), "{t:#?}");
}

/// A question just answered (idle + a fresh @agent_pending) is no longer
/// a NEEDS YOU card, and is never folded into the idle line.
#[test]
fn answered_is_never_folded() {
    let pending = (NOW - 600).to_string();
    let v = view(
        vec![
            a("main", 1, "running", "One"),
            a("main", 2, "idle", "Two").with("pending", &pending),
            a("main", 3, "idle", "Q3"),
            a("main", 4, "idle", "Q4"),
            a("main", 5, "idle", "Q5"),
        ],
        None,
    );
    assert!(v.needs.is_empty());
    let f = (8..58).rev().map(|h| layout(&v, 34, h, &Opts::default())).find(|f| f.agents_step == 1).unwrap();
    let t = texts(&f);
    assert!(t.iter().any(|l| l.contains("3 idle · Q3, Q4, Q5")), "{t:#?}");
    assert!(t.iter().any(|l| l.starts_with("   Two")), "{t:#?}");
}

/// `v` with letters as the sidebar gives them (`letters::of_view`'s
/// assignment, nothing stored yet): the listed agents in display order.
fn lettered(mut v: ViewModel) -> ViewModel {
    let listed: Vec<(String, String)> = crate::menu::rows::listed(&v).map(|a| (a.window_id.clone(), String::new())).collect();
    let got = crate::menu::letters::assign(&listed, &[]).letters;
    for x in v.spaces.iter_mut().flat_map(|s| s.agents.iter_mut()) {
        x.letter = got.get(&x.window_id).copied();
    }
    for n in v.needs.iter_mut() {
        n.agent.letter = got.get(&n.agent.window_id).copied();
    }
    v
}

/// A bar like the live one: main (⌃1, yellow), three shells (home first,
/// the second active), usage meters (the weekly one from its numbers, the
/// session one from its text) and a stale save.
fn demo_bar() -> Bar {
    use super::super::status::{Chip, Member};
    Bar {
        space: Some(Chip { index: Some(1), name: "main".into(), color: Some(Ink::Rgb(0xf9, 0xe2, 0xaf)), hidden: false }),
        shells: ["~", "nvim", "proj"]
            .iter()
            .enumerate()
            .map(|(k, l)| Shell {
                n: k as u32 + 1,
                label: l.to_string(),
                home: k == 0,
                active: k == 1,
                selected: false,
                win: format!("@{}", 901 + k),
                session: "main".into(),
            })
            .collect(),
        usage: vec![
            Usage { label: "S".into(), text: "13% (-38%)".into(), color: Some(Ink::Pal("green")), used: Some(13.0),
                expected: Some(51.0), delta: Some(-38), ..Usage::default() },
            Usage { label: "W".into(), text: "60% (+17%)".into(), color: Some(Ink::Pal("red")), used: Some(60.0),
                expected: Some(43.0), delta: Some(17), resets: Some(NOW + 3 * 3600), pooled: true,
                members: vec![Member { slot: Some(1), used: 90, active: false, cap: 90 }, Member { slot: Some(2), used: 30, active: true, cap: 100 }],
                ..Usage::default() },
            Usage { label: "F".into(), text: "--%".into(), color: Some(Ink::Idx(239)), ..Usage::default() },
        ],
        stale: Some("last save 2h ago".into()),
        ..Bar::default()
    }
}

fn with_bar(bar: Bar) -> Opts {
    Opts { bar, ..Opts::default() }
}

/// Every agent row and NEEDS YOU card shows its letter as a key-cap (a
/// raised chip on its tile), in both scopes; peach while ⌥W's jump table
/// is armed; blank past 26.
#[test]
fn letter_key_caps_lead_every_agent_row_and_card() {
    let v = lettered(quiet());
    let f = layout(&v, 34, 58, &Opts::default());
    let t = texts(&f);
    let r = find(&f, "Tmux Agent Side");
    assert!(t[r].starts_with(" ▌ f  Tmux Agent Side"), "{:?}", t[r]);
    assert!(t[r + 1].starts_with(" ▌    Edit agent-roster.py"), "{:?}", t[r + 1]);
    assert_eq!(f.rows[r].segs[2], on(" f ", "sub", "surface1"));
    let c = find(&f, "▌ g  Kua Yu focus timing");
    assert_eq!(f.rows[c].segs[2], on(" g ", "sub", "surface1"));
    assert!(t[c + 1].starts_with(" ▌    Which deck"), "{t:#?}");
    let i = find(&f, "▎ h  Commodities Job");
    assert_eq!(f.rows[i].segs[2], on(" h ", "sub", "surface1"));
    // Space scope: the same letters.
    let mut sv = lettered(quiet());
    sv.scope.session = Some("main".into());
    let f = layout(&sv, 34, 58, &Opts::default());
    find(&f, " f  Tmux Agent Side");
    find(&f, "▌ g  Kua Yu");
    // ⌥W armed (key table `jump`): every cap peach; the mark and the hint a
    // tab on the panel's top pad, the panel no taller for it.
    let f = layout(&v, 34, 58, &with_bar(Bar { jump: true, ..demo_bar() }));
    let top = &f.rows[f.card.start];
    assert!(top.segs.contains(&block(" ⌥W jump ", "crust", "peach")), "{:?}", top.segs);
    assert_eq!(top.text(), format!(" ▄ ⌥W jump  type a letter {} ", "▄".repeat(7)));
    assert_eq!(f.card, layout(&v, 34, 58, &with_bar(demo_bar())).card);
    let r = find(&f, "Tmux Agent Side");
    assert_eq!(f.rows[r].segs[2], block(" f ", "crust", "peach"));
    let c = find(&f, "▌ g  Kua Yu");
    assert_eq!(f.rows[c].segs[2], block(" g ", "crust", "peach"));
    // Narrow: the hint gives way, the mark stays whole, the pad's ends too.
    let f = layout(&v, 22, 58, &with_bar(Bar { jump: true, ..demo_bar() }));
    let top = f.rows[f.card.start].text();
    assert!(top.starts_with(" ▄ ⌥W jump  type") && top.ends_with("▄ ") && dwidth(&top) == 22, "{top:?}");
    // Past 26 agents: a blank cap, the title still aligned.
    let x = lettered(extreme());
    let none = x.agents().find(|a| a.letter.is_none()).expect("an agent past 26");
    let f = layout(&x, 60, 400, &Opts::default());
    let k = f.rows.iter().position(|r| r.covers.iter().any(|(s, id)| *s == none.session && *id == none.window_id)).unwrap();
    assert_eq!(f.rows[k].segs[2].text, "   ", "{:?}", f.rows[k].text());
    assert!(f.rows[k].text().chars().skip(2).collect::<String>().starts_with("    An agent"), "{:?}", f.rows[k].text());
}

/// The status line's content, in the sidebar's usage panel: a tile with a
/// half-block pad above and below (no border), the red prefix mark a tab
/// on its top pad, one meter per usage window, the stale save; no space
/// chip and no shells (the pills at the bottom and the SHELLS section say
/// those). Shorter frames give up the meters for one line, then the panel.
#[test]
fn the_panel_replaces_the_status_line() {
    let v = lettered(quiet());
    let f = layout(&v, 34, 40, &with_bar(demo_bar()));
    let t = texts(&f);
    assert_eq!((f.card.clone(), f.status.clone()), (0..6, 1..5), "{t:#?}");
    assert!(!t[f.card.clone()].iter().any(|l| l.contains("main") || l.contains('⌥')), "{t:#?}");
    // The meters: label, bar, percent, delta.
    // The delta as the status bar wrote it, no Σ even for a pooled pace.
    assert!(t[1].starts_with("  S ━") && t[1].contains(" 13% (-38%) "), "{:?}", t[1]);
    assert!(t[2].starts_with("  W ━") && t[2].contains(" 60% (+17%) "), "{:?}", t[2]);
    assert!(!t.iter().any(|l| l.contains('Σ')), "{t:#?}");
    assert!(f.rows[2].segs.iter().any(|s| s.text.contains("(+17%)") && s.fg == Ink::Pal("peach")), "over pace: peach");
    assert!(t[3].starts_with("  F ━") && t[3].contains("   -- "), "{:?}", t[3]);
    assert!(t[4].contains(" ! ") && t[4].contains("last save 2h ago"), "{:?}", t[4]);
    // A tile: its pads, the content on `tile` between canvas margins, no line art.
    assert_eq!((t[0].clone(), t[5].clone()), (format!(" {} ", "▄".repeat(32)), format!(" {} ", "▀".repeat(32))));
    for y in 1..5 {
        let r = &f.rows[y];
        assert_eq!((r.bg, r.segs[0].clone(), r.segs.last().cloned()), (Some(Ink::Pal("tile")), bare(1), Some(bare(1))), "{y}");
    }
    assert!(f.rows[0].segs.contains(&seg("▄".repeat(32), "tile")) && f.rows[0].bg.is_none());
    let mut buf = Buffer::empty(Rect::new(0, 0, 34, 40));
    paint(&f, &mut buf);
    assert_eq!((buf[(0, 2)].bg, buf[(1, 2)].bg, buf[(32, 2)].bg, buf[(33, 2)].bg),
        (Color::Reset, palette::color("tile"), palette::color("tile"), Color::Reset));
    // The red prefix mark, keyed on the client's key table: a tab on the top pad.
    let p = layout(&v, 34, 40, &with_bar(Bar { prefix: true, ..demo_bar() }));
    assert_eq!(texts(&p)[0], format!(" ▄ prefix {} ", "▄".repeat(23)));
    assert!(p.rows[0].segs.contains(&block(" prefix ", "crust", "red")));
    assert_eq!((p.card.clone(), texts(&p)[1..].to_vec()), (f.card.clone(), t[1..].to_vec()), "the panel keeps its rows");
    // 34x20: still a panel, usage in one line.
    let f = layout(&v, 34, 20, &with_bar(demo_bar()));
    let t = texts(&f);
    assert!(!f.card.is_empty() && t.iter().any(|l| l.contains("S 13%  W 60%  F --")), "{t:#?}");
    // 34x14: no panel, the prefix mark on a row of its own; 34x11: no
    // usage either, nothing on top but the mark.
    let f = layout(&v, 34, 14, &with_bar(Bar { prefix: true, ..demo_bar() }));
    let t = texts(&f);
    assert!(f.card.is_empty() && t[0].starts_with("  prefix ") && t[1].starts_with(" S 13%"), "{t:#?}");
    let f = layout(&v, 34, 11, &with_bar(demo_bar()));
    let t = texts(&f);
    assert!(f.status.is_empty() && t[0].starts_with(" NEEDS YOU") && !t.iter().any(|l| l.contains("13%")), "{t:#?}");
    let f = layout(&v, 34, 11, &with_bar(Bar { prefix: true, ..demo_bar() }));
    assert!(texts(&f)[0].starts_with("  prefix "), "{:#?}", texts(&f));
    // No toolbar, ever.
    assert!(!t.iter().any(|l| l.contains("⏵ next") || l.contains("≡")), "{t:#?}");
}

/// The client's space's shells: a SHELLS section under the agents, one
/// button tile each (⌥N key-cap, ⌂ on the home, the label), the active one
/// lifted, each clicking through to its shell.
#[test]
fn shells_list_under_the_agents() {
    let v = lettered(quiet());
    let f = layout(&v, 34, 62, &with_bar(demo_bar()));
    let t = texts(&f);
    assert!(f.shells.start >= f.agents.end, "{t:#?}");
    let s = f.shells.start;
    // Each button with its pad.
    assert_eq!(f.shells.len(), 7, "{t:#?}");
    assert_eq!(t[s].trim_end(), " SHELLS");
    assert_eq!(t[s + 1].trim_end(), "   ⌥1  ⌂ ~");
    assert_eq!(t[s + 3].trim_end(), " ▌ ⌥2  nvim");
    assert_eq!(t[s + 5].trim_end(), "   ⌥3  proj");
    assert!([s + 2, s + 4, s + 6].iter().all(|&y| is_pad(&t[y]) && f.rows[y].targets.is_empty()), "{t:#?}");
    assert_eq!((f.rows[s + 1].bg, f.rows[s + 3].bg), (Some(Ink::Pal("tile")), Some(Ink::Pal("tile_hi"))));
    assert_eq!(f.rows[s + 3].segs[1], seg("▌", "blue"));
    assert!(f.rows[s + 3].segs.iter().any(|x| x.text == "nvim" && x.bold));
    let shell = |win: &str| Some(Target::Shell { win: win.into(), session: "main".into() });
    for (y, win) in [(s + 1, "@901"), (s + 3, "@902"), (s + 5, "@903")] {
        let y = y as u16;
        assert_eq!((f.target(0, y).cloned(), f.target(33, y).cloned()), (shell(win), shell(win)));
    }
    assert_eq!(f.target(3, s as u16), None);
    // Under the last agent's pad; a blank row before it too while the gaps last.
    assert!(is_pad(&t[s - 1]), "{t:#?}");
    let tall = layout(&v, 34, 80, &with_bar(demo_bar()));
    assert!(tall.step == 0 && texts(&tall)[tall.shells.start - 1].trim().is_empty(), "{:#?}", texts(&tall));
    // No shells, no section.
    let f = layout(&v, 34, 58, &with_bar(Bar { shells: vec![], ..demo_bar() }));
    assert!(f.shells.is_empty() && !texts(&f).iter().any(|l| l.contains("SHELLS")));
    // Short: the pads go, then one line of buttons before the agents lose
    // any detail, then kept in one line while they compress.
    let v = busy();
    for h in 12u16..=120 {
        let f = layout(&v, 34, h, &with_bar(demo_bar()));
        let t = texts(&f);
        assert!([7, 4, 1].contains(&f.shells.len()), "{h}: {t:#?}");
        if f.shells.len() == 1 {
            assert!(t[f.shells.start].starts_with(" SHELLS ") && t[f.shells.start].contains("nvim"), "{h}: {t:#?}");
        } else {
            assert_eq!(f.agents_step, 0, "{h}: {t:#?}");
        }
        assert!(f.shells.start >= f.agents.end && f.shells.end <= f.spaces.start.max(f.shells.end), "{h}: {t:#?}");
    }
}

#[test]
fn shells_line_counts_what_does_not_fit() {
    let shells: Vec<Shell> = (1..=12)
        .map(|n| Shell { n, label: format!("sh{n}"), home: n == 1, active: n == 7, selected: false, win: format!("@{n}"), session: "main".into() })
        .collect();
    let bar = Bar { shells, ..demo_bar() };
    for w in [34usize, 24, 60, 16] {
        let rows = shells_line(&bar, w);
        assert_eq!(rows.len(), 1, "{w}");
        let shown = rows[0].targets.len();
        let text = rows[0].text();
        let more = text.split(" +").nth(1).map_or(0, |k| k.trim().parse::<usize>().unwrap());
        assert_eq!(shown + more, 12, "{w}: {text:?}");
        assert_eq!(width(&rows[0].segs), w);
        // The active shell is kept first.
        assert!(w < 20 || text.contains("sh7"), "{w}: {text:?}");
    }
    let t = shells_line(&bar, 200)[0].text();
    assert!(t.starts_with(" SHELLS  ⌥1  ⌂sh1   ⌥2  sh2 "), "{t:?}");
    assert!(t.contains(" ⌥9  sh9   sh10   sh11 "), "{t:?}");
    assert!(shells_line(&Bar::default(), 34).is_empty() && shells_section(&Bar::default(), 34, true).is_empty());
}

/// A meter's bar as (glyph, ink) cells: from its fourth cell, the `━` and
/// `┃` run.
fn bar_cells(r: &Row) -> Vec<(char, Ink)> {
    r.segs.iter().flat_map(|s| s.text.chars().map(move |c| (c, s.fg))).skip(3).take_while(|(c, _)| "━┃".contains(*c)).collect()
}

/// A meter: Textual's progress bar, filled (the label's colour) to the
/// used percent over a `surface1` track, the tick where the pace expects
/// it, the reset hint only when the bar keeps its length.
#[test]
fn meters_fill_and_tick() {
    let u = Usage { label: "W".into(), color: Some(Ink::Pal("red")), used: Some(60.0), expected: Some(43.0), delta: Some(17),
        resets: Some(NOW + 3 * 3600), ..Usage::default() };
    let r = meter_row(&u, 40, NOW as f64);
    let t = r.text();
    assert_eq!(dwidth(&t), 40);
    let bar = bar_cells(&r);
    let bw = bar.len();
    let tick = bar.iter().position(|(c, _)| *c == '┃').unwrap();
    assert_eq!(tick, (0.43 * bw as f64).round() as usize, "{t:?}");
    let filled = bar.iter().filter(|(_, ink)| *ink == Ink::Pal("red")).count() + usize::from(tick < (0.6 * bw as f64).round() as usize);
    assert_eq!(filled, (0.6 * bw as f64).round() as usize, "{t:?}");
    assert!(bar.iter().all(|(c, ink)| *c == '┃' || *ink == Ink::Pal("red") || *ink == Ink::Pal("surface1")), "{bar:?}");
    assert!(t.ends_with(" 60% (+17%) ") || t.ends_with("↻3h "), "{t:?}");
    // Wide: the reset hint too.
    assert!(meter_row(&u, 60, NOW as f64).text().ends_with(" 60% (+17%)  ↻3h "), "{:?}", meter_row(&u, 60, NOW as f64).text());
    // Under pace: dim; on pace: `±0%`; idle: an empty bar and `idle`; no number: `--`.
    let under = Usage { delta: Some(-4), ..u.clone() };
    assert!(meter_row(&under, 40, 0.0).segs.iter().any(|s| s.text.contains(" (-4%)") && s.fg == Ink::Pal("overlay2")));
    let even = Usage { delta: Some(0), pooled: true, ..u.clone() };
    assert!(meter_row(&even, 40, 0.0).text().contains(" 60%  (±0%) "), "{:?}", meter_row(&even, 40, 0.0).text());
    let idle = Usage { label: "S".into(), idle: true, ..Usage::default() };
    let r = meter_row(&idle, 34, 0.0);
    let bar = bar_cells(&r);
    assert!(bar.len() > 5 && bar.iter().all(|x| *x == ('━', Ink::Pal("surface1"))) && r.text().ends_with("idle "), "{:?}", r.text());
    // Narrow: the delta goes before the bar does.
    let t = meter_row(&u, 16, NOW as f64).text();
    assert!(!t.contains("+17") && t.contains("60%") && dwidth(&t) == 16, "{t:?}");
    // Over the window: the real number, the bar full, the tick on its last cell.
    let over = Usage { used: Some(104.0), expected: Some(112.0), delta: Some(-8), ..u.clone() };
    let r = meter_row(&over, 40, NOW as f64);
    let bar = bar_cells(&r);
    assert!(r.text().contains("104%") && !bar.iter().any(|(_, ink)| *ink == Ink::Pal("surface1")) && bar.last().unwrap().0 == '┃', "{bar:?}");
    let under = Usage { used: Some(-3.0), expected: Some(-10.0), ..u.clone() };
    let bar = bar_cells(&meter_row(&under, 40, NOW as f64));
    assert!(bar[0].0 == '┃' && !bar.iter().any(|(_, ink)| *ink == Ink::Pal("red")), "{bar:?}");
}

/// Ages are formatted at the frame's clock, not frozen at the snapshot's:
/// a frame laid out a minute later (the spinner's) says a minute more.
#[test]
fn ages_follow_the_frame_clock() {
    let v = quiet();
    let at = |now: Option<f64>| {
        let f = layout(&v, 34, 58, &Opts { now, ..Opts::default() });
        let t = texts(&f);
        (t[find(&f, "Lost suit jacket") + 1].clone(), t[find(&f, "Edit agent-roster.py")].clone(), t[find(&f, "▌ Kua Yu")].clone())
    };
    let (idle, msg, card) = at(None);
    assert!(idle.ends_with(" 6m  ") && msg.ends_with(" 2m  ") && card.ends_with("◉ 3m  "), "{idle:?} {msg:?} {card:?}");
    let (idle, msg, card) = at(Some(NOW as f64 + 60.0));
    assert!(idle.ends_with(" 7m  ") && msg.ends_with(" 3m  ") && card.ends_with("◉ 4m  "), "{idle:?} {msg:?} {card:?}");
}

/// Dim text on a surface (a pill, a card, a tile) is `overlay2`, never
/// `overlay` (under 3:1 there).
#[test]
fn dim_text_on_surfaces_is_overlay2() {
    let bars = [demo_bar(), Bar { toasts: vec![toast(Kind::Warn, "hi")], ..demo_bar() }];
    for (v, bar) in [lettered(quiet()), busy()].into_iter().flat_map(|v| bars.clone().map(move |b| (v.clone(), b))) {
        for h in [20u16, 40, 58] {
            let f = layout(&v, 34, h, &with_bar(bar.clone()));
            for r in &f.rows {
                for s in &r.segs {
                    let surface = s.bg.is_some_and(|b| b != Ink::Canvas) || r.bg.is_some();
                    assert!(!(surface && s.fg == Ink::Pal("overlay") && !s.text.trim().is_empty()), "{:?} in {:?}", s, r.text());
                }
            }
        }
    }
}

/// prefix v's selection: every selected agent tile (both lines) and shell
/// button on a mauve tint with a mauve stripe; the hint.
#[test]
fn the_stash_selection_tints_rows_and_shows_its_keys() {
    let v = lettered(view(
        vec![
            a("main", 1, "running", "Sel one").with("stash_sel", "1").with("detail_kind", "run").with("detail", "Bash ls"),
            a("main", 2, "idle", "Plain"),
        ],
        Some("@1"),
    ));
    let mut bar = demo_bar();
    bar.shells[1].selected = true;
    bar.selecting = Some(2);
    let f = layout(&v, 34, 40, &with_bar(bar.clone()));
    let t = texts(&f);
    let r = find(&f, "Sel one");
    for row in &f.rows[r..r + 2] {
        assert_eq!(row.bg, Some(Ink::Mix("mauve", 18)), "{:?}", row.text());
        assert_eq!(row.segs[1], seg("▌", "mauve"));
    }
    let p = find(&f, "Plain");
    assert_ne!(f.rows[p].bg, Some(Ink::Mix("mauve", 18)));
    let sh = find(&f, "⌥2  nvim");
    assert!(t[sh].starts_with(" ▌ ⌥2  nvim"), "{t:#?}");
    assert_eq!((f.rows[sh].bg, f.rows[sh].segs[1].clone()), (Some(Ink::Mix("mauve", 18)), seg("▌", "mauve")));
    assert_ne!(f.rows[find(&f, "⌥3  proj")].bg, Some(Ink::Mix("mauve", 18)));
    // The hint, first in the panel: as many of the keys as fit.
    assert_eq!(find(&f, " selected "), f.status.start);
    let hint = find(&f, " selected ");
    assert!(t[hint].contains(" 2 selected  h/l ⏎ park esc"), "{:?}", t[hint]);
    assert!(f.rows[hint].segs.iter().any(|s| *s == block(" 2 selected ", "crust", "mauve")));
    let f = layout(&v, 60, 40, &with_bar(bar));
    assert!(texts(&f).iter().any(|l| l.contains(" 2 selected  h/l extend · ⏎ park · m move · esc cancel")));
    // No selection: no hint.
    assert!(!texts(&layout(&v, 34, 40, &with_bar(demo_bar()))).iter().any(|l| l.contains("selected")));
}

/// A scoped view: `agents` plus the usual noise, the client on `here` in
/// `session`, with `@spaces` / `@strip_scope` / `@space_hidden` on its
/// client row, scope applied as the sidebar does.
fn scoped(agents: Vec<A>, here: &str, session: &str, spaces: &str, scope: &str, hidden: &str) -> ViewModel {
    let mut rows: Vec<String> = agents
        .iter()
        .enumerate()
        .map(|(n, x)| {
            let mut set: Vec<(&str, &str)> = vec![("state", x.st), ("summary", x.title)];
            set.extend(x.set.iter().map(|(k, v)| (*k, v.as_str())));
            row(x.s, x.i, &format!("@{}", n + 1), &set)
        })
        .collect();
    rows.push(row("agents", 1, "@900", &[("state", "failed"), ("summary", "hidden")]));
    rows.push(row("stash", 1, "@901", &[("summary", "x"), ("stash_label", "Parked one"), ("stash_ts", "5")]));
    rows.push(format!("\x1fclient\x1f{CLIENT}\x1f{here}\x1f{session}\x1f{spaces}\x1f{scope}\x1f{hidden}"));
    let c = Collator::new("en_US.UTF-8");
    let snap = Snapshot::parse(&rows.join("\n"));
    let args = BuildArgs { client: Some(CLIENT), now: NOW as f64, collator: &c, git: None, log: vec![], watcher_age: Some(1.0) };
    let mut v = ViewModel::build(&snap, args);
    v.apply_scope(&snap);
    v
}

/// Agents interleaved with shells, out of index order, and another
/// session. Ids follow the list: @1 shell (the client's), @2 idle, @3
/// shell, @4 failed (index 5), @5 running (index 4), @6 shell, @7
/// needs-input (index 12), @8 other.
fn proj() -> Vec<A<'static>> {
    vec![
        a("proj", 1, "", "zsh"),
        a("proj", 2, "idle", "p/two"),
        a("proj", 3, "", "zsh"),
        a("proj", 5, "failed", "p/five"),
        a("proj", 4, "running", "p/four"),
        a("proj", 9, "", "zsh"),
        a("proj", 12, "needs-input", "p/twelve"),
        a("other", 1, "running", "o/one"),
    ]
}

/// An agent row's title, when `l` is one (no letters in these views): past
/// the margin, the stripe and a space.
fn row_title(l: &str) -> Option<&str> {
    let rest = l.strip_prefix(' ')?;
    let mut cs = rest.chars();
    if !matches!(cs.next()?, ' ' | '▎' | '▌') {
        return None;
    }
    let rest = cs.as_str().strip_prefix(' ')?;
    (!rest.starts_with(' ') && !is_pad(l)).then(|| rest.split("  ").next().unwrap().trim())
}

#[test]
fn space_scope_shows_one_session() {
    let v = scoped(proj(), "@1", "proj", "on", "", "");
    assert_eq!(v.scope.session.as_deref(), Some("proj"));
    let f = layout(&v, 34, 58, &Opts::default());
    let t = texts(&f);
    // The title: this space's counts only (other's ◐ not counted).
    assert!(t[0].starts_with(" space ") && t[0].ends_with("✕1 ◉1 ◐1 ○1 "), "{:?}", t[0]);
    let b = agents_part(&f);
    // The scope: a rounded button.
    assert!(b[0].starts_with(" AGENTS") && b[0].ends_with(" ⌥V  this space  "), "{b:#?}");
    assert!(b[1].starts_with(" ● proj"), "{b:#?}");
    // Window-index order; no shell rows.
    let rows: Vec<&str> = b[2..].iter().filter_map(|l| row_title(l)).collect();
    assert_eq!(rows, ["two", "four", "five", "twelve"], "{b:#?}");
    assert!(!b.iter().any(|l| l.contains("other") || l.contains("o/one") || l.contains("zsh")), "{b:#?}");
    // NEEDS YOU stays global, as do the spaces.
    let needs = &t[f.needs.clone()];
    assert!(needs.iter().any(|l| l.starts_with(" ▌ p/five")) && needs.iter().any(|l| l.starts_with(" ▌ p/twelve")), "{needs:#?}");
    assert!(t[f.spaces.clone()].iter().any(|l| l.contains("other")), "{t:#?}");
    // The title toggles, and so does the scope button; the counts do not.
    assert_eq!((f.target(0, 0), f.target(1, 0), f.target(5, 0), f.target(6, 0), f.target(30, 0)),
        (None, Some(&Target::Scope), Some(&Target::Scope), None, None));
    let y = f.agents.start as u16;
    assert_eq!((f.target(21, y), f.target(32, y), f.target(33, y), f.target(3, y)), (Some(&Target::Scope), Some(&Target::Scope), None, None));
}

#[test]
fn explicit_space_scope_without_spaces_keeps_the_current_shell() {
    let v = scoped(proj(), "@1", "proj", "", "space", "");
    assert_eq!(v.scope.session.as_deref(), Some("proj"));
    let f = layout(&v, 34, 58, &Opts::default());
    let b = agents_part(&f);
    // The client's shell is a tile (the classic rule), no state word.
    let z = b.iter().find(|l| l.contains("zsh")).unwrap_or_else(|| panic!("{b:#?}"));
    assert!(z.starts_with(" ▌ zsh") && z.trim_end().ends_with("zsh"), "{z:?}");
    assert!(b.iter().any(|l| row_title(l) == Some("two")) && b.iter().any(|l| row_title(l) == Some("twelve")), "{b:#?}");
}

#[test]
fn all_scope_with_spaces_drops_shells_and_toggles() {
    let v = scoped(proj(), "@1", "proj", "on", "all", "");
    assert_eq!(v.scope.session, None);
    let f = layout(&v, 34, 58, &Opts::default());
    let t = texts(&f);
    assert!(t[0].starts_with(" agents ") && t[0].ends_with("✕1 ◉1 ◐2 ○1 "), "{:?}", t[0]);
    assert_eq!(f.target(3, 0), Some(&Target::Scope));
    let b = agents_part(&f);
    assert!(b[0].contains("all spaces"), "{b:#?}");
    assert!(b.iter().any(|l| l.starts_with(" ● other")) && b.iter().any(|l| l.starts_with(" ● proj")), "{b:#?}");
    assert!(!b.iter().any(|l| l.contains("zsh")), "{b:#?}");
    assert!(b.iter().any(|l| row_title(l) == Some("five")) && b.iter().any(|l| row_title(l) == Some("two")), "{b:#?}");
    // A space with nothing but shells is no group at all.
    let mut only_shell = proj();
    only_shell.push(a("plain", 1, "", "zsh"));
    let v = scoped(only_shell, "@9", "plain", "on", "all", "");
    assert!(!agents_part(&layout(&v, 34, 58, &Opts::default())).iter().any(|l| l.contains(" plain")));
}

#[test]
fn classic_when_spaces_off_and_scope_unset() {
    for (spaces, scope) in [("", ""), ("off", ""), ("1", ""), ("", "bogus")] {
        let v = scoped(proj(), "@1", "proj", spaces, scope, "");
        assert_eq!((v.scope.session.as_ref(), v.scope.toggles(), v.scope.name()), (None, false, "all"), "{spaces} {scope}");
        let f = layout(&v, 34, 58, &Opts::default());
        assert!(f.rows[0].targets.is_empty() && f.rows[f.agents.start].targets.is_empty());
        // Not a button: plain dim text.
        assert!(texts(&f)[f.agents.start].ends_with("all spaces "), "{spaces} {scope}");
        // The client's shell still shows: the classic rule.
        assert!(agents_part(&f).iter().any(|l| l.contains("zsh")), "{spaces} {scope}");
    }
    for (spaces, scope, want) in [("on", "", "space"), ("on", "all", "all"), ("", "space", "space"), ("on", "bogus", "space"), ("1", "", "all")] {
        assert_eq!(scoped(proj(), "@1", "proj", spaces, scope, "").scope.name(), want, "{spaces} {scope}");
    }
}

#[test]
fn hidden_sessions_fall_back_to_all() {
    let mut ws = proj();
    ws.push(a("scratch", 1, "running", "s/one"));
    for (session, here, hidden) in [("proj", "@2", "1"), ("scratch", "@9", ""), ("stash", "@901", "")] {
        let v = scoped(ws.iter().map(|x| A { set: x.set.clone(), ..*x }).collect(), here, session, "on", "", hidden);
        assert_eq!((v.scope.name(), v.scope.session.as_ref()), ("space", None), "{session}");
        let f = layout(&v, 34, 58, &Opts::default());
        assert!(texts(&f)[0].starts_with(" agents "), "{session}");
        let b = agents_part(&f);
        assert!(b[0].contains("all spaces") && b.iter().any(|l| l.starts_with(" ● other")), "{session}: {b:#?}");
    }
}

#[test]
fn empty_space_still_has_its_header() {
    let mut ws = proj();
    ws.push(a("empty", 1, "", "zsh"));
    let v = scoped(ws, "@9", "empty", "on", "", "");
    let f = layout(&v, 34, 30, &Opts::default());
    let t = texts(&f);
    assert_eq!(t[0].trim_end(), " space"); // no counts: nothing in it
    let b = agents_part(&f);
    assert!(b[1].starts_with(" ● empty") && b[2].starts_with("   no agents in this space"), "{b:#?}");
    assert!(t[f.needs.clone()][0].starts_with(" NEEDS YOU"));
    assert_eq!(f.target(4, (f.agents.start + 1) as u16), Some(&Target::Session("empty".into())));
}

#[test]
fn scoped_views_fit_every_size() {
    let mut fleet = busy_agents();
    for i in 1..=12 {
        fleet.push(a("main", 10 + i, if i % 3 == 0 { "" } else { "idle" }, "Filler"));
    }
    let views = [
        scoped(proj(), "@1", "proj", "on", "", ""),
        scoped(proj(), "@1", "proj", "", "space", ""),
        scoped(proj(), "@1", "proj", "on", "all", ""),
        scoped(fleet.iter().map(|x| A { set: x.set.clone(), ..*x }).collect(), "@4", "main", "on", "", ""),
    ];
    for v in &views {
        for w in [16u16, 24, 34, 60] {
            for h in 2u16..=70 {
                let f = layout(v, w, h, &Opts::default());
                assert_eq!(f.rows.len(), usize::from(h));
                for r in &f.rows {
                    assert_eq!(width(&r.segs), usize::from(w), "{w}x{h}: {:?}", r.text());
                    assert!(r.targets.iter().all(|(a, b, _)| a < b && *b <= w), "{w}x{h}");
                }
                check_accounting(&format!("scoped {w}x{h}"), v, &f);
                check_order(&format!("scoped {w}x{h}"), &f);
            }
        }
    }
}

/// Squeezed hard, no row goes to waste: one row left over under a
/// collapsed space shows its most important agent (the header counts the
/// rest), never a blank row above the spaces.
#[test]
fn a_last_spare_row_shows_an_agent() {
    let mut fleet = busy_agents();
    for i in 1..=12 {
        fleet.push(a("main", 10 + i, "idle", "Filler"));
    }
    let v = scoped(fleet.iter().map(|x| A { set: x.set.clone(), ..*x }).collect(), "@4", "main", "on", "", "");
    for h in 6u16..=14 {
        let f = layout(&v, 34, h, &Opts::default());
        let t = texts(&f);
        if f.agents_step >= 3 {
            let end = if f.spaces.is_empty() { usize::from(h) - 1 } else { f.spaces.start };
            assert!(t[f.agents.end..end].iter().all(|l| !l.trim().is_empty()), "{h}: {t:#?}");
        }
        check_accounting(&format!("fleet {h}"), &v, &f);
    }
    let f = layout(&v, 34, 11, &Opts::default());
    let t = texts(&f);
    let hd = find(&f, " ● main");
    assert!(t[hd + 1].contains("Handy.app"), "{t:#?}");
    assert_eq!(f.spaces.len(), 2, "{t:#?}");
    assert!(t[10].starts_with(&parked(0)[..4]), "{t:#?}");
}

/// Narrow widths degrade in order: the age first (under 30), then the
/// second lines (under 26), then the state word to its glyph; titles clip;
/// nothing wraps.
#[test]
fn narrow_widths_degrade_gracefully() {
    let v = lettered(quiet());
    let age = |l: &str| {
        let last: Vec<char> = l.trim_end().rsplit(' ').next().unwrap_or("").chars().collect();
        last.len() >= 2 && last[..last.len() - 1].iter().all(|c| c.is_ascii_digit()) && "smhd".contains(last[last.len() - 1])
    };
    for w in 16u16..=40 {
        let f = layout(&v, w, 58, &with_bar(demo_bar()));
        let t = texts(&f);
        let rows: Vec<&Row> = f.rows[f.needs.start..f.agents.end].iter().filter(|r| r.target_at(0).is_some_and(|x| matches!(x, Target::Go { .. }))).collect();
        let seconds = rows.iter().filter(|r| r.covers.is_empty() && !matches!(r.bg, Some(Ink::Mix("yellow", _)))).count();
        if w < 30 {
            assert!(!rows.iter().any(|r| age(&r.text())), "{w}: {t:#?}");
        }
        if w < 26 {
            assert_eq!(seconds, 0, "{w}: {t:#?}");
        } else {
            assert!(seconds > 0, "{w}: {t:#?}");
        }
        if w >= 30 {
            assert!(rows.iter().any(|r| age(&r.text())), "{w}: {t:#?}");
        }
        for r in &f.rows {
            assert_eq!(width(&r.segs), usize::from(w));
        }
        // Every agent's title starts where it should (clipped, never shifted).
        let r = find(&f, "Tmux");
        assert!(t[r].starts_with(" ▌ f  Tmux"), "{w}: {:?}", t[r]);
    }
}

/// The spinner: only a working agent draws one (the frame says so), and
/// the frame only moves its glyph.
#[test]
fn the_spinner_turns_only_working_rows() {
    let v = quiet();
    let f0 = layout(&v, 34, 58, &Opts::default());
    let f1 = layout(&v, 34, 58, &Opts { spin: 1, ..Opts::default() });
    assert!(f0.spin && f1.spin);
    let changed: Vec<usize> = (0..58).filter(|&y| f0.rows[y] != f1.rows[y]).collect();
    let working: Vec<usize> = (0..58).filter(|&y| f0.rows[y].anim).collect();
    assert_eq!(changed, working);
    assert_eq!(working.len(), 2);
    assert!(f1.rows[working[0]].text().contains("⠙ working"));
    // SPIN wraps.
    let f10 = layout(&v, 34, 58, &Opts { spin: 10, ..Opts::default() });
    assert_eq!(f10.rows, f0.rows);
    // Nothing working: no spinner.
    let idle = view(vec![a("main", 1, "idle", "Calm"), a("main", 2, "needs-input", "Ask")], None);
    assert!(!layout(&idle, 34, 30, &Opts::default()).spin);
    // A toast card over a working row: that row's spinner is not drawn, so
    // it never asks for a redraw.
    let one = view(vec![a("main", 1, "running", "Busy")], None);
    let f = layout(&one, 34, 14, &Opts { bar: Bar { toasts: vec![toast(Kind::Info, "hi")], ..Bar::default() }, ..Opts::default() });
    let busy_row = f.rows.iter().position(|r| r.covers.len() == 1).unwrap();
    assert_eq!(f.spin, !f.toast.contains(&busy_row), "{:#?}", texts(&f));
}

#[test]
fn glyph_widths_agree_with_ratatui() {
    // Every fixed glyph the sidebar draws: the layout's cell count
    // (text::dwidth) must be ratatui's (unicode-width over the grapheme).
    let glyphs = "⏵⤺≡⌥▌▎▄▀◉✓✕•◐○●─━┃│╭╮╰╯↻Σ−±↑↓✎⚙◎▸…✳⬢⏎⏸⌃⌂\u{e0a0}\u{e0b0}›\u{f05a}\u{f058}\u{f071}\u{f057}".to_string() + &SPIN.concat();
    let bad: Vec<(char, usize, usize)> = glyphs
        .chars()
        .map(|c| (c, dwidth(&c.to_string()), c.to_string().width()))
        .filter(|(_, a, b)| a != b || *a != 1)
        .collect();
    assert!(bad.is_empty(), "{bad:?}");
}

/// A toast posted at the fixtures' clock (seq 7, 4 s), for every sidebar.
fn toast(kind: Kind, text: &str) -> Toast {
    Toast { seq: 7, at_ms: NOW * 1000, kind, ttl: 4.0, client: String::new(), text: text.into(), title: None }
}

/// Toast `seq`, posted `seq` ms after the fixtures' clock.
fn toast_n(seq: u64, kind: Kind, text: &str) -> Toast {
    Toast { seq, at_ms: NOW * 1000 + seq as i64, ..toast(kind, text) }
}

/// Its id: what a click on it dismisses.
const TOAST_ID: ToastId = (7, NOW * 1000);

/// A toast too long for one line.
const LONG: &str = "couldn't park Canvas Syllabus: it is still working on the export";

/// A toast is a card over the top of the frame (nvim-notify's): a rounded
/// border in its kind's colour over a fill of it, the icon and title and
/// the time it was posted in the top border, the text inside. Nothing
/// beneath moves, a click anywhere on it dismisses it, and it says when it
/// expires.
#[test]
fn a_toast_is_a_card_over_the_top() {
    let v = lettered(quiet());
    let plain = layout(&v, 34, 40, &with_bar(demo_bar()));
    let ok = toast(Kind::Ok, "parked Canvas Syllabus");
    let f = layout(&v, 34, 40, &with_bar(Bar { toasts: vec![ok.clone()], ..demo_bar() }));
    let t = texts(&f);
    let time = posted_at(&ok);
    assert_eq!(f.toast, 0..3, "{t:#?}");
    assert_eq!(t[0], format!(" ╭─ \u{f058} Done {} {time} ─╮ ", "─".repeat(13)));
    assert_eq!(t[1], format!(" │ parked Canvas Syllabus{} │ ", " ".repeat(6)));
    assert_eq!(t[2], format!(" ╰{}╯ ", "─".repeat(30)));
    // Nothing beneath moves; the rows under it still stand for theirs.
    assert_eq!((&f.rows[3..], f.card.clone(), f.status.clone(), f.needs.clone()), (&plain.rows[3..], plain.card.clone(), plain.status.clone(), plain.needs.clone()));
    assert!(f.rows[..3].iter().zip(&plain.rows[..3]).all(|(a, b)| a.covers == b.covers));
    // The look: the fill (green over the canvas), the border and the title
    // in green, the title bold, the time dim, the text plain; a canvas
    // margin either side.
    for y in 0..3 {
        let r = &f.rows[y];
        assert_eq!((r.bg, r.segs[0].clone(), r.segs.last().cloned()), (Some(Ink::Mix("green", 12)), bare(1), Some(bare(1))), "{y}");
    }
    assert!(f.rows[0].segs.contains(&bold("\u{f058} Done", "green")) && f.rows[0].segs.contains(&seg(time.clone(), "overlay2")));
    assert!(f.rows[1].segs.contains(&seg("parked Canvas Syllabus", "text")) && f.rows[1].segs.contains(&seg("│ ", "green")));
    let mut buf = Buffer::empty(Rect::new(0, 0, 34, 40));
    paint(&f, &mut buf);
    assert_eq!((buf[(0, 1)].bg, buf[(1, 1)].bg, buf[(1, 1)].fg), (Color::Reset, palette::mix("green", 12), palette::color("green")));
    // A click anywhere on it dismisses it.
    for y in 0..3u16 {
        assert_eq!((f.target(0, y), f.target(20, y), f.target(33, y)), (Some(&Target::Toast(TOAST_ID)), Some(&Target::Toast(TOAST_ID)), Some(&Target::Toast(TOAST_ID))));
    }
    assert_eq!(f.toast_until, Some((NOW + 4) * 1000));
    assert_eq!(plain.toast_until, None);
    // Each kind: its icon, title and colour.
    for (k, icon, title, hue) in [(Kind::Info, "\u{f05a}", "Info", "blue"), (Kind::Ok, "\u{f058}", "Done", "green"),
        (Kind::Warn, "\u{f071}", "Warning", "peach"), (Kind::Error, "\u{f057}", "Error", "red")] {
        let f = layout(&v, 34, 40, &with_bar(Bar { toasts: vec![toast(k, "hi")], ..demo_bar() }));
        assert!(texts(&f)[0].starts_with(&format!(" ╭─ {icon} {title} ─")), "{k:?}: {:?}", texts(&f)[0]);
        assert!(f.rows[0].segs.contains(&bold(format!("{icon} {title}"), hue)) && f.rows[1].bg == Some(Ink::Mix(hue, 12)), "{k:?}");
    }
    // notify.sh -T: the title instead of the kind's.
    let titled = Toast { title: Some("Stash".into()), ..ok.clone() };
    let f = layout(&v, 34, 40, &with_bar(Bar { toasts: vec![titled], ..demo_bar() }));
    assert!(texts(&f)[0].starts_with(" ╭─ \u{f058} Stash ─"), "{:?}", texts(&f)[0]);
    // Narrow: the time goes before the title is cut; the corners stay.
    let f = layout(&v, 18, 40, &with_bar(Bar { toasts: vec![ok], ..demo_bar() }));
    let top = texts(&f)[0].clone();
    assert!(top.starts_with(" ╭─ \u{f058} Done ─") && top.ends_with("─╮ ") && !top.contains(':'), "{top:?}");
}

/// Up to three cards at once, stacked from the top like nvim-notify's:
/// oldest on top, the newest below it; each its own to dismiss; the frame
/// wakes at the earliest deadline. Short frames drop the oldest first, then
/// body lines, and never cover the last row.
#[test]
fn toasts_stack_oldest_on_top() {
    let v = lettered(quiet());
    let stack = vec![toast_n(1, Kind::Ok, "one"), toast_n(2, Kind::Warn, "two"), toast_n(3, Kind::Error, "three"), toast_n(4, Kind::Info, "four")];
    let f = layout(&v, 34, 40, &with_bar(Bar { toasts: stack.clone(), ..demo_bar() }));
    let t = texts(&f);
    assert_eq!(f.toast, 0..9, "{t:#?}");
    assert!(t[1].contains("two") && t[4].contains("three") && t[7].contains("four") && !t.iter().any(|l| l.contains(" one ")), "{t:#?}");
    let id = |s: u64| Some(Target::Toast((s, NOW * 1000 + s as i64)));
    assert_eq!((f.target(5, 1).cloned(), f.target(5, 3).cloned(), f.target(5, 8).cloned()), (id(2), id(3), id(4)));
    // By posting time, whatever order `@toast` gave them in.
    let mut rev = stack.clone();
    rev.reverse();
    assert_eq!(layout(&v, 34, 40, &with_bar(Bar { toasts: rev, ..demo_bar() })).rows, f.rows);
    // The earliest deadline among those drawn; an expired one is not drawn.
    let short = Toast { ttl: 2.0, ..toast_n(5, Kind::Ok, "short") };
    let pair = vec![short.clone(), toast_n(6, Kind::Ok, "long")];
    let at = |now: f64| layout(&v, 34, 40, &Opts { now: Some(now), ..with_bar(Bar { toasts: pair.clone(), ..demo_bar() }) });
    assert_eq!((at(NOW as f64).toast.clone(), at(NOW as f64).toast_until), (0..6, Some(until_ms(&short))));
    let later = at(NOW as f64 + 3.0);
    assert_eq!((later.toast.clone(), later.toast_until), (0..3, Some(until_ms(&pair[1]))));
    assert!(texts(&later)[1].contains("long"));
    // Short frames: never the last row; the newest always there when any is.
    for h in 2u16..=12 {
        let f = layout(&v, 34, h, &with_bar(Bar { toasts: stack.clone(), ..demo_bar() }));
        let t = texts(&f);
        assert!(f.toast.len() < usize::from(h) && !f.toast.is_empty(), "{h}: {t:#?}");
        assert!(t.iter().any(|l| l.contains("four")), "{h}: {t:#?}");
        assert!(h >= 4 || f.toast.len() == 1, "{h}: a compact row {t:#?}");
    }
    // Too narrow for a card: the newest as a compact row.
    let f = layout(&v, 15, 40, &with_bar(Bar { toasts: stack, ..demo_bar() }));
    assert!(f.toast == (0..1) && texts(&f)[0].contains("four"), "{:#?}", texts(&f));
}

/// A long toast wraps inside its card, at most three lines, the rest cut
/// with `…`; every line dismisses it.
#[test]
fn a_long_toast_wraps_inside_its_card() {
    let v = lettered(quiet());
    let f = layout(&v, 34, 40, &with_bar(Bar { toasts: vec![toast(Kind::Error, LONG)], ..demo_bar() }));
    let t = texts(&f);
    assert_eq!(f.toast, 0..5, "{t:#?}");
    assert!(t[1].starts_with(" │ couldn't park Canvas ") && t[2].starts_with(" │ Syllabus: it is still ") && t[3].starts_with(" │ working on the export "), "{t:#?}");
    assert!((1..4).all(|y| t[y].ends_with(" │ ") && f.target(1, y as u16) == Some(&Target::Toast(TOAST_ID))), "{t:#?}");
    let huge = format!("{LONG} {LONG}");
    let f = layout(&v, 34, 40, &with_bar(Bar { toasts: vec![toast(Kind::Error, &huge)], ..demo_bar() }));
    let t = texts(&f);
    assert_eq!(f.toast, 0..5, "{t:#?}");
    assert!(t[3].trim_end_matches([' ', '│']).ends_with('…'), "{t:#?}");
    assert_eq!(wrap("aaaa bbbb cccccccccccc dd", 6, 3), ["aaaa", "bbbb", "ccccc…"]);
    assert_eq!(wrap("xxxxxxxxxx yy", 4, 3), ["xxx…", "yy"]);
    assert_eq!(wrap("one", 6, 2), ["one"]);
}

/// A toast lives until its deadline by the frame's clock, then the frame is
/// the one without it.
#[test]
fn a_toast_expires_by_the_frame_clock() {
    let v = lettered(quiet());
    let bar = Bar { toasts: vec![toast(Kind::Ok, "stashed it")], ..demo_bar() };
    let at = |bar: &Bar, now: f64| layout(&v, 34, 40, &Opts { now: Some(now), ..with_bar(bar.clone()) });
    let t0 = NOW as f64;
    assert!(texts(&at(&bar, t0 + 3.99)).iter().any(|l| l.contains("stashed it")));
    for now in [t0 + 4.0, t0 + 60.0] {
        let f = at(&bar, now);
        assert_eq!((f.toast.is_empty(), f.toast_until), (true, None));
        assert_eq!(f, at(&demo_bar(), now));
    }
    // The same with no panel under it.
    let bare = Bar { toasts: vec![toast(Kind::Ok, "stashed it")], ..Bar::default() };
    assert_eq!(at(&bare, t0 + 4.0), at(&Bar::default(), t0 + 4.0));
}

/// The card's clock: local time, `HH:MM`, the event log's own
/// ([`crate::text::clock_hm`]), at the second the toast was posted.
#[test]
fn the_toast_clock_is_local_hh_mm() {
    let mins = |s: &str| -> u32 {
        assert!(s.len() == 5 && s.as_bytes()[2] == b':', "{s:?}");
        s[..2].parse::<u32>().unwrap() * 60 + s[3..].parse::<u32>().unwrap()
    };
    let at = posted_at(&toast(Kind::Ok, "x"));
    assert_eq!(at, crate::text::clock_hm(NOW));
    let later = posted_at(&Toast { at_ms: (NOW + 3660) * 1000 + 999, ..toast(Kind::Ok, "x") });
    assert_eq!((mins(&later) + 1440 - mins(&at)) % 1440, 61);
}

/// The key-table marks and the selection hint are what a key press is
/// waiting on: toast cards start under them, never over them.
#[test]
fn toasts_never_cover_the_marks_or_the_hint() {
    let v = lettered(busy());
    let up = vec![toast(Kind::Ok, "parked it")];
    let bar = Bar { prefix: true, selecting: Some(2), toasts: up.clone(), ..demo_bar() };
    for (w, h) in [(34u16, 40u16), (34, 14), (38, 50), (24, 30)] {
        let f = layout(&v, w, h, &with_bar(bar.clone()));
        let t = texts(&f);
        let above = t[..f.toast.start].join("\n");
        assert!(above.contains("prefix") && above.contains("2 selected"), "{w}x{h}: {t:#?}");
        assert!(t[f.toast.clone()].iter().any(|l| l.contains("parked it")), "{w}x{h}: {t:#?}");
    }
    // ⌥W armed: the tab stays, the card under it.
    let f = layout(&v, 34, 40, &with_bar(Bar { jump: true, toasts: up.clone(), ..demo_bar() }));
    assert_eq!(f.toast, 1..4, "{:#?}", texts(&f));
    assert!(texts(&f)[0].contains("⌥W jump"), "{:#?}", texts(&f));
    // Nothing waiting: from the very top.
    assert_eq!(layout(&v, 34, 40, &with_bar(Bar { toasts: up, ..demo_bar() })).toast, 0..3);
}

/// A sidebar too small for a card still shows the newest toast, as one
/// compact row (icon and text on the card's fill, a click dismisses it),
/// down to 2 rows: notify.sh chose a toast, so it must be seen.
#[test]
fn a_small_sidebar_still_shows_a_toast() {
    let v = lettered(quiet());
    let stack = vec![toast_n(1, Kind::Ok, "older"), toast_n(2, Kind::Warn, "newest")];
    for (w, h) in [(15u16, 40u16), (12, 10), (6, 8), (34, 3), (34, 2), (20, 2)] {
        let f = layout(&v, w, h, &with_bar(Bar { toasts: stack.clone(), ..demo_bar() }));
        let t = texts(&f);
        assert_eq!(f.toast.len(), 1, "{w}x{h}: {t:#?}");
        let y = f.toast.start;
        assert!(t[y].contains('\u{f071}') && !t.iter().any(|l| l.contains("older")) && y + 1 < usize::from(h), "{w}x{h}: {t:#?}");
        assert!(w < 10 || t[y].contains("newest"), "{w}x{h}: {t:#?}");
        assert_eq!((f.rows[y].bg, f.target(0, y as u16)), (Some(Ink::Mix("peach", TOAST_TINT)), Some(&Target::Toast((2, NOW * 1000 + 2)))));
        assert_eq!(f.toast_until, Some(until_ms(&stack[1])));
    }
    // One row: nothing (the last row is the parked one).
    assert!(layout(&v, 34, 1, &with_bar(Bar { toasts: stack, ..demo_bar() })).toast.is_empty());
}

/// The terminal's own background everywhere but the surfaces: the tiles
/// (inside their canvas margins), the NEEDS YOU card, the pills.
#[test]
fn renders_through_ratatui_on_the_terminal_background() {
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;
    let f = layout(&quiet(), 34, 58, &Opts::default());
    let mut term = Terminal::new(TestBackend::new(34, 58)).unwrap();
    term.draw(|fr| paint(&f, fr.buffer_mut())).unwrap();
    let buf = term.backend().buffer();
    let here = find(&f, "Tmux Agent Sidebar") as u16;
    let card = find(&f, "▌ Kua Yu") as u16;
    // (SPACES and PARKED carry the status line's coloured chips.)
    let pills = f.spaces.start as u16..f.parked.end as u16;
    for y in 0..58u16 {
        for x in 0..34u16 {
            let bg = buf[(x, y)].bg;
            let inside = (1..33).contains(&x);
            if pills.contains(&y) {
                continue;
            }
            if (y == here || y == here + 1) && inside {
                assert_eq!(bg, palette::color("tile_hi"), "({x},{y})");
            } else if (y == card || y == card + 1) && inside {
                assert_eq!(bg, palette::mix("yellow", 14), "({x},{y})");
            } else if f.rows[y as usize].bg.is_some() && inside {
                assert_eq!(bg, palette::color("tile"), "({x},{y}) {:?}", f.rows[y as usize].text());
            } else if !pills.contains(&y) && y != 57 && !(y == 2 && x > 28) {
                assert_eq!(bg, Color::Reset, "({x},{y}) {:?}", f.rows[y as usize].text());
            }
        }
    }
    // A pad: the tile's colour as the half block's ink, on the canvas.
    let pad = here + 2;
    assert_eq!((buf[(5, pad)].symbol(), buf[(5, pad)].fg, buf[(5, pad)].bg), ("▀", palette::color("tile_hi"), Color::Reset));
    let ansi = to_ansi(buf);
    assert_eq!(ansi.lines().count(), 58);
    // No background code at all on an untinted row.
    assert!(ansi.starts_with("\x1b[0;38;2;205;214;244;1m agents"), "{:?}", &ansi[..60]);
    assert!(!ansi.lines().next().unwrap().contains(";48;"));
}

/// A working agent's second line is its live activity blurb (from its
/// transcript, `activity`), a blue `›` in the key-cap's column; without
/// one, the run detail as before; an agent that is not working never shows
/// one. Every agent row stays two lines.
#[test]
fn a_working_agent_shows_its_activity() {
    let mut v = lettered(quiet());
    for a in v.spaces.iter_mut().flat_map(|s| s.agents.iter_mut()) {
        a.blurb = Some(format!("Running {}", a.window_id));
    }
    let f = layout(&v, 34, 58, &Opts::default());
    let t = texts(&f);
    let r = find(&f, "Tmux Agent Side");
    assert!(t[r + 1].starts_with(" ▌  › Running @2") && t[r + 1].ends_with(" 2m  "), "{:?}", t[r + 1]);
    assert!(f.rows[r + 1].segs.contains(&seg("› ", "blue")) && f.rows[r + 1].segs.contains(&seg("Running @2", "sub")));
    // Not working (idle, asking): their own second lines.
    let n = find(&f, "Lost suit jacket");
    let k = find(&f, "▎ g  Kua Yu");
    assert!(!t[n + 1].contains("Running") && t[k + 1].contains("Which deck"), "{t:#?}");
    let (firsts, seconds) = agent_lines(&f);
    assert_eq!(firsts, seconds);
    // No blurb: what it runs, as before.
    let f = layout(&lettered(quiet()), 34, 58, &Opts::default());
    let r = find(&f, "Tmux Agent Side");
    assert!(texts(&f)[r + 1].starts_with(" ▌    Edit agent-roster.py"), "{:#?}", texts(&f));
}

/// The bottom gives way in order as rows run short: PARKED's tabs first
/// (its header and count stay), then SPACES collapses to the client's
/// space and the attention ones (`+K more spaces`), then to chips; the
/// PARKED header is always there.
#[test]
fn the_bottom_gives_way_in_order() {
    let mut v = quiet();
    let p = v.parked[0].clone();
    v.parked = (1..=5)
        .map(|k| {
            let mut q = p.clone();
            q.agent.short_title = format!("Parked {k}");
            q.origin = "bai".into();
            q
        })
        .collect();
    let mut forms = Vec::new();
    for h in (8u16..=90).rev() {
        let f = layout(&v, 34, h, &Opts::default());
        let t = texts(&f);
        check_order(&format!("bottom {h}"), &f);
        let tabs = f.parked.clone().filter(|&y| t[y].contains("⏸ Parked")).count();
        let spaces = &t[f.spaces.clone()];
        let form = match spaces.get(1) {
            Some(l) if l.starts_with(" SPACES") => usize::from(spaces.iter().any(|l| l.contains("more spaces"))),
            Some(_) => 2,
            None => 3,
        };
        if tabs > 0 {
            // Three tabs and the count of the rest, beside the full list.
            assert_eq!((form, tabs), (0, 3), "{h}: {t:#?}");
            assert!(t[f.parked.end - 1].contains("+2 more") && t[f.parked.end - 2].ends_with("bai "), "{h}: {t:#?}");
            assert_eq!(f.target(5, (f.parked.end - 1) as u16), Some(&Target::Stash));
        }
        if form == 1 {
            // The client's space (main, which needs you too) and `+2`.
            let more = f.spaces.clone().find(|&y| t[y].contains("+2  more spaces")).unwrap_or_else(|| panic!("{h}: {t:#?}"));
            assert!(spaces.iter().any(|l| l.contains(" main ")) && !spaces.iter().any(|l| l.contains(" bai ")), "{h}: {t:#?}");
            assert_eq!(f.target(5, more as u16), Some(&Target::Menu(Some("all"))));
        }
        forms.push((form, tabs > 0));
    }
    for want in [(0, true), (0, false), (1, false), (2, false)] {
        assert!(forms.contains(&want), "{want:?} never seen: {forms:?}");
    }
}

/// An asking agent with a workflow or cua flag is "in flight", but not
/// working: its row shows its question, never a stale activity blurb.
#[test]
fn an_asking_agent_never_shows_a_blurb() {
    let mut v = view(vec![a("main", 1, "needs-input", "Ask").with("cua", "1").with("detail_kind", "ask")
        .with("detail", "Which deck should I use?")], None);
    for x in v.spaces.iter_mut().flat_map(|s| s.agents.iter_mut()) {
        assert!(x.in_flight, "the repro needs an in-flight agent");
        x.blurb = Some("Running stale tool".into());
    }
    let f = layout(&v, 34, 40, &Opts::default());
    let t = texts(&f);
    let r = f.agents.start + agents_part(&f).iter().position(|l| l.contains("Ask")).unwrap();
    assert!(t[r + 1].contains("Which deck should I use?") && !t.iter().any(|l| l.contains("stale tool")), "{t:#?}");
}

/// The client's space chip keeps its arrow when its name is long: the name
/// gives way, clipped with `…`, and the counts stay.
#[test]
fn a_long_space_name_keeps_the_arrow() {
    let v = view(vec![a("a-very-long-space-name-indeed", 1, "running", "T").with("space_index", "1")], Some("@1"));
    for w in [24u16, 30, 38] {
        let f = layout(&v, w, 40, &Opts::default());
        let y = f.spaces.clone().find(|&y| f.rows[y].text().contains(ARROW)).unwrap_or_else(|| panic!("{w}: {:#?}", texts(&f)));
        let t = f.rows[y].text();
        assert!(t.contains(&format!("… {ARROW}")) && t.starts_with("  1 a-very"), "{w}: {t:?}");
        assert!(t.trim_end().ends_with("◐1"), "{w}: {t:?}");
        assert_eq!(width(&f.rows[y].segs), usize::from(w));
    }
}
