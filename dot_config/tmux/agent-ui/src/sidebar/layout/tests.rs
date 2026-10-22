use super::*;
use crate::collate::Collator;
use crate::tmux::tests::row;
use crate::tmux::Snapshot;
use crate::view::BuildArgs;
use std::collections::HashMap;

const NOW: i64 = 1_000_000;
const CLIENT: &str = "/dev/ttys999";

/// Idle is the absence of a state: no rail, no word, a regular title. Every
/// other state (working, asking, failed, done) has the `▎` rail in its
/// colour down both rows of its card and its glyph and word bold in that
/// colour on the right (the glyph alone when narrow). The client's own idle
/// agent keeps a lavender rail; the stash selection a heavy mauve one.
#[test]
fn idle_says_nothing_and_every_state_has_its_rail() {
    let mut seen = (0, 0);
    for (name, v) in all().into_iter().chain([("coloured", coloured())]).flat_map(|(n, v)| [(n, v.clone()), (n, lettered(v))]) {
        for (w, h) in [(34u16, 58u16), (42, 90), (42, 30), (24, 40)] {
            let f = layout(&v, w, h, &Opts::default());
            let cx = Cx { w: usize::from(w), b: Badge::of(&v, &Bar::default()), now: v.now, spin: 0, home: None, cur: None, shells: true, resumes: false, ctx: CTX_STYLE };
            let t = texts(&f);
            for y in f.agents.clone().filter(|&y| f.rows[y].covers.len() == 1 && !is_group_header(&f.rows[y], usize::from(w))) {
                let r = &f.rows[y];
                let a = v.agents().find(|a| (a.session.clone(), a.window_id.clone()) == r.covers[0]).unwrap();
                let ctx = format!("{name} {w}x{h} {:?}", t[y]);
                let Some(word) = state_word(a, cx) else { continue };
                let two = f.rows.get(y + 1).is_some_and(|n| n.covers.is_empty() && n.bg == r.bg && n.target_at(1) == r.target_at(1));
                let rails: Vec<&Seg> = std::iter::once(&r.segs[1]).chain(two.then(|| &f.rows[y + 1].segs[1])).collect();
                assert!(!r.segs.iter().any(|s| s.text.trim() == "idle" || s.text.ends_with(" idle")), "{ctx}");
                if a.selected {
                    continue;
                }
                if word.idle() {
                    seen.0 += 1;
                    let want = if a.is_current { seg("▎", "lavender") } else { seg(" ", "text") };
                    assert!(rails.iter().all(|s| **s == want), "{ctx}: {rails:?}");
                    assert!(!r.segs.iter().any(|s| s.bold && s.fg != Ink::Pal("text") && s.bg.is_none()), "{ctx}: {:?}", r.segs);
                } else {
                    seen.1 += 1;
                    assert!(rails.iter().all(|s| **s == seg("▎", word.hue)), "{ctx}: {rails:?}");
                    let full = bold(format!("{} {}", word.glyph, word.word), word.hue);
                    assert!(r.segs.contains(&full) || r.segs.contains(&bold(word.glyph, word.hue)), "{ctx}: {:?}", r.segs);
                    assert!(r.segs.iter().any(|s| s.bold && s.fg == Ink::Pal("text")), "{ctx}: a bold title {:?}", r.segs);
                }
            }
        }
    }
    assert!(seen.0 > 50 && seen.1 > 50, "{seen:?}");
}

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
    // Noise the sidebar must ignore: a hidden session, a parked tab, a
    // space's home (the space's header is the home: never a row).
    rows.push(row("agents", 1, "@900", &[("state", "failed"), ("summary", "hidden")]));
    rows.push(row("stash", 1, "@901", &[("summary", "x"), ("stash_label", "Parked one"), ("stash_ts", "5")]));
    rows.push(row("main", 99, "@902", &[("home", "1")]));
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

/// A row's text as words: runs of spaces one, padded (` 1 main `,
/// ` 2 cua-notch ` read as they would in prose).
fn uncapped(l: &str) -> String {
    format!(" {} ", l.split(' ').filter(|w| !w.is_empty()).collect::<Vec<_>>().join(" "))
}

/// The end of the sections above the dock: NEEDS YOU and AGENTS (the
/// shells are rows of their spaces in it).
fn body_end(f: &Frame) -> usize {
    f.needs.end.max(f.agents.end)
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

/// The agents section's space headers, in order: (row, its space).
fn heads(f: &Frame) -> Vec<(usize, String)> {
    f.agents
        .clone()
        .filter_map(|y| match f.rows[y].targets.as_slice() {
            [(0, _, Target::Session(s))] => Some((y, s.clone())),
            _ => None,
        })
        .collect()
}

/// The row of `name`'s space header in the agents section.
fn head(f: &Frame, name: &str) -> usize {
    heads(f).into_iter().find(|(_, s)| s == name).map(|(y, _)| y).unwrap_or_else(|| panic!("no {name:?} header in {:#?}", texts(f)))
}

/// The parked row as drawn (in the dock: past its canvas margin).
fn parked(n: usize) -> String {
    if n == 0 { "  PARKED none".to_string() } else { format!("  PARKED  {n} ") }
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
/// would be empty), NEEDS YOU, agents (the shells in their spaces), spare
/// rows, then the dock to the last row (its gap, its `▄` cap, the spaces,
/// PARKED, its `▀` cap); sections never overlap; no SHELLS section or line
/// anywhere; no line art anywhere; the toast cards (drawn over the top)
/// never over the last row.
fn check_order(name: &str, f: &Frame) {
    let h = usize::from(f.height);
    if h < 4 {
        return;
    }
    let t = texts(f);
    let shown = |y: usize| !f.toast.contains(&y);
    assert!(f.needs.end <= f.agents.start, "{name}: {:?} {:?} {t:#?}", f.needs, f.agents);
    assert!(f.agents.end <= f.dock.start && f.dock.end == h, "{name}: {:?} {:?} {t:#?}", f.agents, f.dock);
    assert!(!t.iter().any(|l| l.contains("SHELLS")), "{name}: {t:#?}");
    if !f.card.is_empty() {
        assert!(f.card.start == 0 && f.card.end <= f.needs.start, "{name}: card {:?} {t:#?}", f.card);
        assert!(f.status.start > f.card.start && f.status.end < f.card.end, "{name}");
        assert!(!shown(f.card.end - 1) || is_pad(&t[f.card.end - 1]), "{name}: the panel's bottom pad {t:#?}");
    }
    if !f.spaces.is_empty() {
        // The dock: a gap above it, the spaces on its panel.
        assert!(f.dock.start < f.spaces.start && f.spaces.end <= f.parked.start, "{name}: {t:#?}");
        assert!(!shown(f.dock.start) || is_blank(&f.rows[f.dock.start]), "{name}: the gap first {t:#?}");
        for y in f.spaces.clone().filter(|&y| shown(y)) {
            assert!(f.rows[y].bg.is_some() && !f.rows[y].targets.is_empty(), "{name}: {y} {t:#?}");
        }
    }
    // The dock's rows: the gap, the caps (a pair or none) and panel rows.
    let caps = is_pad(&t[h - 1]);
    let pads = f.dock.clone().filter(|&y| shown(y) && is_pad(&t[y])).count();
    assert!(pads == 2 * usize::from(caps) || caps && pads == 1 && f.toast.end > f.dock.start, "{name}: {t:#?}");
    // PARKED: its one line always there (the stash, messages, the watcher),
    // last but for the dock's bottom cap: never the parked tabs.
    assert_eq!(f.parked.len(), 1, "{name}: PARKED is one line {:?} {t:#?}", f.parked);
    assert_eq!(f.parked.end + usize::from(caps), h, "{name}: {t:#?}");
    // Never under a toast: the stash, a message, the watcher's warning.
    assert!(f.toast.end <= f.parked.start, "{name}: a toast over PARKED {t:#?}");
    assert!(t[f.parked.start].trim_start().starts_with("PARKED"), "{name}: no PARKED {t:#?}");
    assert!(!t.iter().any(|l| l.starts_with(" log") || l.contains('═')), "{name}: {t:#?}");
    for (y, l) in t.iter().enumerate() {
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
                // A toast drawn is a toast clickable (but the blank canvas
                // rows between and under the cards), and every one that
                // fits is drawn: a card from 3 rows, its bars in place.
                let t = texts(&f);
                for y in f.toast.clone() {
                    let r = &f.rows[y];
                    let gap = r.bg.is_none() && r.targets.is_empty() && t[y].trim().is_empty();
                    assert!(gap || r.targets.iter().any(|t| matches!(t.2, Target::Toast(_))), "{name} {w}x{h}: {y}");
                }
                assert_eq!(f.toast.is_empty(), f.toast_until.is_none(), "{name} {w}x{h}");
                // (Under the marks and the hint, which stay on top.)
                let free = usize::from(h) - f.toast.start;
                assert_eq!(f.toast.is_empty(), bar.toasts.is_empty() || free < 2, "{name} {w}x{h}: {t:#?}");
                if f.toast.len() > 1 {
                    let a = &t[f.toast.start];
                    assert!(a.starts_with(" ┃ ") && a.ends_with(" ┃ "), "{name} {w}x{h}: {t:#?}");
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
            if f.step == 0 && f.spaces.len() < pill_lines(&v, 34) {
                // Rows short at step 0: the dock gave them up first (no
                // spare row left above it).
                assert_eq!(f.dock.start, body_end(&f), "{name} {h}: {:#?}", texts(&f));
            }
            last = now;
        }
    }
}

/// Rows a frame's step leaves over are spent on spacing, never on detail:
/// while blank rows sit unused above the spaces, no section gap is missing
/// (above AGENTS, SHELLS, NEEDS YOU), nor the gaps between spaces unless
/// they cost more than is left. Never a pad between list tiles (cards stack
/// in the zebra), and every agent row keeps one shape.
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
                    let end = body_end(&f);
                    let stop = f.dock.start;
                    assert!(f.rows[end..stop].iter().all(is_blank), "{ctx}: {t:#?}");
                    let spare = stop - end;
                    let gap_above = |r: &std::ops::Range<usize>| r.is_empty() || r.start == 0 || is_blank(&f.rows[r.start - 1]);
                    let gaps = [gap_above(&f.needs), gap_above(&f.agents)];
                    if spare > 0 {
                        assert_eq!(gaps, [true; 2], "{ctx}: a blank run at the bottom, a section gap missing: {t:#?}");
                    }
                    // The spaces: a gap above every header but the first, or not enough left for all.
                    let missing = group_gaps(&f.rows[f.agents.clone()], usize::from(w));
                    if missing > 0 {
                        assert!(spare < missing, "{ctx}: {t:#?}");
                    }
                    // Never a pad between list tiles, whatever is spare.
                    let lists = f.needs.start.min(f.agents.start)..body_end(&f);
                    assert!(!t[lists].iter().any(|l| is_pad(l)), "{ctx}: {t:#?}");
                    bought += usize::from(f.step > 0 && gaps == [true; 2]);
                }
            }
        }
    }
    assert!(bought > 0, "spacing never bought back");
    // A frame with just enough (34x40): the dock keeps its caps, nothing
    // blank at the bottom; the agents keep their shape (the zebra, no pads).
    let f = layout(&lettered(quiet()), 34, 40, &with_bar(demo_bar()));
    let t = texts(&f);
    assert_eq!(f.agents_step, 0, "{t:#?}");
    assert!(is_pad(&t[39]) && f.dock.start == f.agents.end, "{t:#?}");
    assert!(!t[f.agents.clone()].iter().any(|l| is_pad(l)), "{t:#?}");
}

/// Blank rows are what goes first as rows run short, and come back first
/// as they grow: the gaps between spaces in AGENTS, then the section gaps,
/// all before the dock's caps, the dock's spaces, a card's second line or
/// an agent; and spare rows buy the section gaps back before the gaps
/// between spaces.
#[test]
fn blank_rows_are_spent_first() {
    let section_gaps = |f: &Frame| {
        let gap = |r: &Range<usize>| !r.is_empty() && r.start > 0 && is_blank(&f.rows[r.start - 1]);
        (usize::from(gap(&f.needs)) + usize::from(gap(&f.agents)), 2)
    };
    let group_gaps_shown = |f: &Frame| heads(f).len().saturating_sub(1 + group_gaps(&f.rows[f.agents.clone()], f.width.into()));
    let caps = |f: &Frame| is_pad(&texts(f)[usize::from(f.height) - 1]);
    for (name, v) in [("busy", lettered(busy())), ("quiet", lettered(quiet())), ("coloured", coloured())] {
        for w in [34u16, 42] {
            let at = |h: u16| layout(&v, w, h, &with_bar(demo_bar()));
            let roomy = at(120);
            assert!(group_gaps_shown(&roomy) > 0 && section_gaps(&roomy) == (2, 2) && caps(&roomy), "{name}: {:#?}", texts(&roomy));
            for f in (16..=120).rev().map(at) {
                let t = texts(&f);
                let ctx = format!("{name} {w}x{}", f.height);
                let (gaps, can) = section_gaps(&f);
                // The gaps between spaces go first, and come back last.
                if gaps < can {
                    assert_eq!(group_gaps_shown(&f), 0, "{ctx}: {t:#?}");
                }
                // Every step that keeps the dock's reserve keeps its caps; a
                // dock without them leaves at most the one row they could
                // not use, which buys a gap.
                if f.step <= 1 {
                    assert!(caps(&f), "{ctx}: {t:#?}");
                }
                if !caps(&f) {
                    assert!(gaps + group_gaps_shown(&f) <= 1, "{ctx}: {t:#?}");
                }
            }
        }
    }
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
/// up: the step and the agents' step never come back while the panel keeps
/// its size (the panel has its own ladder: the meters, then the panel, then
/// usage go, and the rows that frees may well buy the sections detail back).
#[test]
fn shrinking_never_brings_detail_back() {
    let bars = [demo_bar(), Bar { usage: vec![], stale: None, ..demo_bar() }, Bar { prefix: true, ..demo_bar() }];
    for (name, v) in all().into_iter().chain([("shells", lettered(shelly()))]) {
        for bar in &bars {
            for w in [30u16, 34, 44] {
                let mut last = (0u8, 0u8);
                let mut top = usize::MAX;
                for h in (12u16..=110).rev() {
                    let f = layout(&v, w, h, &with_bar(bar.clone()));
                    if f.status.end + f.card.len() != top {
                        (top, last) = (f.status.end + f.card.len(), (0, 0));
                    }
                    let now = (f.step, f.agents_step);
                    assert!(now.0 >= last.0 && now.1 >= last.1, "{name} {w}x{h}: {now:?} after {last:?} {:#?}", texts(&f));
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
    // The section label bold in `overlay2`; the count a small yellow chip.
    assert!(t[1].trim().is_empty() && t[2].starts_with(" NEEDS YOU") && t[2].ends_with(" 1  "), "{t:#?}");
    assert!(f.rows[2].segs.contains(&bold(" NEEDS YOU", "overlay2")) && f.rows[2].segs.contains(&block(" 1 ", "crust", "yellow")), "{:?}", f.rows[2].segs);
    // The card: one tint of its state's colour, a heavy yellow rail down
    // both rows; the title bold, the state's glyph and word bold on the
    // right; then the question (`text`: here it is the content), the age
    // at its end. No pad.
    assert!(t[3].starts_with(" ▌ Kua Yu focus timing") && t[3].ends_with("◉ asks  "), "{:?}", t[3]);
    assert!(t[4].starts_with(" ▌   Which deck should I pul") && t[4].ends_with(" 3m  "), "{:?}", t[4]);
    assert_eq!((f.rows[3].bg, f.rows[4].bg), (Some(Ink::Mix("yellow", 14)), Some(Ink::Mix("yellow", 14))));
    assert_eq!((f.rows[3].segs[0].clone(), f.rows[3].segs[1].clone(), f.rows[4].segs[1].clone()), (bare(1), seg("▌", "yellow"), seg("▌", "yellow")));
    assert!(f.rows[3].segs.contains(&bold("◉ asks", "yellow")) && f.rows[4].segs.iter().any(|s| s.text.starts_with("Which") && s.fg == Ink::Pal("text")));
    // Then a gap, and the agents right under it.
    assert!(t[5].trim().is_empty() && t[6].starts_with(" AGENTS") && t[6].ends_with("all spaces "), "{t:#?}");
    assert_eq!(f.agents.start, 6);
    // Space headers on the canvas: ` N ` solid in the space's colour (peach:
    // no colour of its own; no ⌘N: blank), the name bold in it, the counts
    // on the right. The client's space a band of its colour with a `▌` at
    // the very edge.
    let main = head(&f, "main");
    let peach = Ink::Pal("peach");
    assert!(t[main].starts_with("▌    main") && t[main].ends_with("◉1 ◐2 ○4  ") && f.rows[main].bg == Some(band(peach)), "{:?}", t[main]);
    assert!(f.rows[main].segs.contains(&block("   ", "crust", peach)) && f.rows[main].segs.contains(&bold("main", peach)), "{:?}", f.rows[main].segs);
    let bai = head(&f, "bai");
    assert!(t[bai].starts_with("     bai") && f.rows[bai].bg.is_none(), "{:?}", t[bai]);
    // The client's agent, "you are here": lifted (`tile_me`, both rows), a
    // full lavender cell down its right edge, a rail in its state's colour,
    // the spinner and its word bold; its message under it with the age, in
    // `sub`; the next card right under that. A canvas margin either side.
    let r = find(&f, "Tmux Agent Sidebar");
    assert!(t[r].starts_with(" ▎ Tmux Agent Sidebar") && t[r].ends_with("⠋ working █ "), "{:?}", t[r]);
    assert!(t[r + 1].starts_with(" ▎   Edit agent-roster.py") && t[r + 1].ends_with(" 2m █ "), "{:?}", t[r + 1]);
    assert_eq!((f.rows[r].bg, f.rows[r + 1].bg), (Some(Ink::Pal(MINE_BG)), Some(Ink::Pal(MINE_BG))));
    for y in [r, r + 1] {
        let n = f.rows[y].segs.len();
        assert_eq!(f.rows[y].segs[n - 2], seg("█", "lavender"), "{y}");
    }
    assert!(f.rows[r + 1].segs.contains(&seg("Edit agent-roster.py", "sub")) && f.rows[r + 1].segs.contains(&seg(" 2m", "sub")), "{:?}", f.rows[r + 1].segs);
    assert_eq!((f.rows[r].segs[1].clone(), f.rows[r + 1].segs[1].clone()), (seg("▎", "blue"), seg("▎", "blue")));
    assert!(f.rows[r].segs.contains(&bold("Tmux Agent Sidebar", "text")) && f.rows[r].segs.contains(&bold("⠋ working", "blue")), "{:?}", f.rows[r].segs);
    assert_eq!((f.rows[r].segs[0].clone(), f.rows[r].segs.last().cloned()), (bare(1), Some(bare(1))));
    assert!(f.rows[r + 2].covers.len() == 1 && f.rows[r + 2].bg == Some(Ink::Pal("tile")), "{t:#?}");
    // A title gives way to the state word (and the flag stays); working:
    // a blue rail down a plain card, one colour (its group's first: `tile`).
    let h = find(&f, "Handy.app Speech… ⚙");
    assert!(t[h].ends_with("⠋ working  ") && t[h + 1].starts_with(" ▎   Bash swift build"), "{t:#?}");
    assert_eq!((f.rows[h].bg, f.rows[h].segs[1].clone()), (Some(Ink::Pal("tile")), seg("▎", "blue")));
    assert_eq!((f.rows[h + 1].bg, f.rows[h + 1].segs[1].clone()), (Some(Ink::Pal("tile")), seg("▎", "blue")));
    // Recently done: the word, its message, a green rail; the zebra's
    // other shade (the 4th card of main).
    let c = find(&f, "Commodities Job Track");
    assert!(t[c].ends_with("✓ done  ") && t[c + 1].starts_with(" ▎   7 fixes applied"), "{t:#?}");
    assert_eq!(f.rows[c].segs[1], seg("▎", "green"));
    assert_eq!((f.rows[c].bg, f.rows[c + 1].bg), (Some(Ink::Pal("tile_lo")), Some(Ink::Pal("tile_lo"))));
    // Idle: two lines like every agent, no rail, NO state word, a regular
    // `text` title; the age under it; the next card right under it.
    let n = find(&f, "Lost suit jacket");
    assert!(t[n].starts_with("   Lost suit jacket") && t[n].trim_end().ends_with("Lost suit jacket") && t[n + 1].ends_with(" 6m  "), "{t:#?}");
    assert!(f.rows[n].segs.contains(&seg("Lost suit jacket", "text")), "{:?}", f.rows[n].segs);
    assert!(f.rows[n + 2].covers.len() == 1 && t[n + 2].contains("Backyard"), "{t:#?}");
    // No pads: the section is headers, cards (each one colour of the zebra,
    // or the client's lift) and the gaps between spaces, nothing else.
    for y in f.agents.clone().skip(1) {
        let r = &f.rows[y];
        let tile = matches!(r.bg, Some(Ink::Pal("tile" | "tile_lo" | "tile_me")));
        assert!(!is_pad(&t[y]) && (is_blank(r) || tile || is_group_header(r, 34)), "{y}: {t:#?}");
        if r.covers.len() == 1 {
            assert!(f.rows[y + 1].covers.is_empty() && f.rows[y + 1].bg == r.bg, "{y}: a card is one colour {t:#?}");
        }
    }
    assert!(!t.iter().any(|l| l.contains(" idle ") || l.ends_with(" idle  ")), "{t:#?}");
    // A blank row between groups (after bai's second card, on the zebra's
    // other shade), in ⌘N (here: name) order.
    assert!(t[main - 1].trim().is_empty() && f.rows[main - 2].bg == Some(Ink::Pal("tile_lo")) && bai < main, "{t:#?}");
    // The dock: a gap, its `▄` cap, a row per space in ⌘N (here: name)
    // order, each with its counts, PARKED (one line, its count, never the
    // parked tab itself), its `▀` cap. Spare rows above it.
    assert_eq!((f.dock.clone(), f.spaces.clone(), f.parked.clone()), (51..58, 53..56, 56..57), "{t:#?}");
    assert!(t[51].trim().is_empty() && is_pad(&t[52]) && is_pad(&t[57]), "{t:#?}");
    assert!(t[53].contains(" bai ") && t[53].ends_with("○2  ") && t[55].contains(" schedule "), "{t:#?}");
    for y in [53, 55, 56] {
        assert_eq!(f.rows[y].bg, Some(Ink::Pal(PANEL)), "{y}");
    }
    // The client's space: its band, a `▌` at the panel's edge, its name
    // bold.
    assert!(f.rows[54].segs.contains(&block("   ", "crust", peach)) && f.rows[54].segs.contains(&bold("main", "text")), "{:?}", f.rows[54].segs);
    assert!(t[54].starts_with(" ▌    main") && t[54].ends_with("◉1 ◐2 ○4  ") && f.rows[54].bg == Some(band(peach)), "{t:#?}");
    assert!(t[f.agents.end..51].iter().all(|l| l.trim().is_empty()), "{t:#?}");
    assert!(t[56].starts_with(&parked(1)), "{t:#?}");
    assert!(!t.iter().any(|l| l.contains("Parked one") || l.contains('⏸')), "{t:#?}");
    assert!(!f.spin || t.iter().any(|l| l.contains('⠋')));
}

#[test]
fn busy_squeezes_spaces_and_cards_before_agents() {
    let v = busy();
    let at = |h: u16| layout(&v, 34, h, &Opts::default());
    let first = |p: &dyn Fn(&Frame) -> bool| (20..120).rev().map(at).find(|f| p(f)).unwrap();
    // 24 agents, two lines each, stacked in the zebra (no pads): full detail in
    // 90 rows, gaps and all (in 80 the section gaps still, bought back).
    let f = at(90);
    assert!(f.step == 0 && f.agents_step == 0 && !texts(&f)[..body_end(&f)].iter().any(|l| is_pad(l)), "{:#?}", texts(&f));
    // Shorter: the gaps go, then the SPACES list's reserve (the cards
    // still two lines), all before any agent loses detail.
    let f = first(&|f| f.step > 0);
    assert_eq!(f.agents_step, 0, "{:#?}", texts(&f));
    let f = first(&|f| f.step > 1);
    let card_lines = f.rows[f.needs.clone()].iter().filter(|r| r.bg.is_some()).count();
    assert_eq!((f.step, f.agents_step, card_lines), (2, 0, 2 * v.needs.len()), "{:#?}", texts(&f));
    // Every card one colour: the zebra's (the next card the other), or the lift.
    for y in f.agents.clone().filter(|&y| f.rows[y].covers.len() == 1) {
        let pair = (f.rows[y].bg, f.rows[y + 1].bg);
        assert!(pair.0 == pair.1 && matches!(pair.0, Some(Ink::Pal("tile" | "tile_lo" | "tile_me"))), "{y}: {pair:?}");
    }
    let f = first(&|f| f.step > 2);
    assert_eq!((f.step, f.agents_step), (3, 0), "{:#?}", texts(&f));
    // Shorter still: the agents compress, every one of them the same height.
    for step in [1, 2, 3] {
        let f = (20..90).rev().map(at).find(|f| f.agents_step == step).unwrap();
        let t = texts(&f);
        assert_eq!(f.step, STEPS.len() as u8, "{t:#?}");
        // All four needs still on screen, in agent-jump.sh order (failed first).
        let fail = find(&f, "Handy.app");
        assert!(fail < find(&f, "Notch hover") && fail < f.agents.start);
        for title in ["Kua Yu focus", "Commodities Job"] {
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
    // Every space a row of the dock.
    assert_eq!(f.spaces.len(), space_list(&sv).len(), "{:#?}", texts(&f));
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
    let pills: Vec<String> = t[f.spaces.clone()].iter().map(|l| uncapped(l)).collect();
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
    // The spaces never go: the dock's gap and a pill line with the client's
    // space.
    assert!(f.spaces.len() == 1 && is_blank(&f.rows[f.dock.start]) && uncapped(&t[f.spaces.start]).contains(" alpha "), "{t:#?}");
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
    // The plain `main` session is still a space: its row in the dock.
    let s = &t[f.spaces.clone()];
    assert!(s.len() == 1 && s[0].contains(" main "), "{t:#?}");
}

#[test]
fn click_targets() {
    let v = quiet();
    let f = layout(&v, 34, 58, &Opts::default());
    let go = |win: &str| Some(Target::Go { win: win.into(), session: "main".into() });
    // Both lines of an agent tile, anywhere (its margins too), and both
    // lines of the card; the next card right under it is the next agent's.
    let r = find(&f, "Tmux Agent Sidebar") as u16;
    assert_eq!(f.target(5, r).cloned(), go("@2"));
    assert_eq!(f.target(0, r + 1).cloned(), go("@2"));
    assert_eq!(f.target(33, r + 1).cloned(), go("@2"));
    assert_eq!(f.target(5, r + 2).cloned(), go("@3"));
    let card = find(&f, "▌ Kua Yu") as u16;
    for y in card..card + 2 {
        assert_eq!(f.target(0, y).cloned(), go("@3"));
        assert_eq!(f.target(33, y).cloned(), go("@3"));
    }
    assert_eq!(f.target(5, card + 2), None);
    // Space headers and the dock's space rows (anywhere on them) go to the
    // session.
    let hd = head(&f, "bai") as u16;
    assert_eq!((f.target(0, hd).cloned(), f.target(20, hd).cloned()), (Some(Target::Session("bai".into())), Some(Target::Session("bai".into()))));
    let s = f.spaces.start as u16;
    for (y, name) in [(s, "bai"), (s + 1, "main"), (s + 2, "schedule")] {
        assert_eq!((f.target(0, y), f.target(33, y)), (Some(&Target::Session(name.into())), Some(&Target::Session(name.into()))));
    }
    // The dock's gap and caps, the section headers and blank rows do nothing.
    let d = f.dock.start as u16;
    assert_eq!((f.target(5, d), f.target(5, d + 1), f.target(5, 57)), (None, None, None));
    assert_eq!(f.target(2, find(&f, " NEEDS YOU") as u16), None);
    assert_eq!(f.target(2, 1), None);
    // PARKED (its one line, across its width) opens the stash picker.
    let p = f.parked.start as u16;
    assert_eq!((f.target(0, p), f.target(2, p), f.target(33, p)), (Some(&Target::Stash), Some(&Target::Stash), Some(&Target::Stash)));
    // A watcher problem is a restart button, on the PARKED line's right.
    let mut v = quiet();
    v.watcher_age = Some(99.0);
    let f = layout(&v, 34, 58, &Opts::default());
    let p = find(&f, " PARKED") as u16;
    assert!(f.rows[p as usize].text().ends_with("watcher stalled 99s  "), "{:?}", f.rows[p as usize].text());
    assert_eq!((f.target(30, p), f.target(33, p), f.target(2, p)), (Some(&Target::Restart), Some(&Target::Restart), Some(&Target::Stash)));
    // A message wins the corner (and is not a button).
    let f = layout(&v, 34, 58, &Opts { msg: Some("no tmux client".into()), ..Opts::default() });
    assert!(f.rows[p as usize].text().ends_with("no tmux client  "));
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
    // never the old outcome; then, right under it, the next agent.
    let c = at("Commodities Job Tracker");
    assert!(!t[c].contains("~/") && body(&t[c + 1]).starts_with("no activity yet") && body(&t[c + 1]).ends_with(" 1m") && f.rows[c + 2].covers.len() == 1, "{t:#?}");
    // Working with what it runs: plain text under it; `project/` dropped.
    let r = at("Tmux Agent Sidebar");
    assert!(!t[r].contains("tmux/") && t[r + 1].starts_with(" ▎   cargo test "), "{:?}", t[r + 1]);
    let m = at("Markup only");
    assert!(body(&t[m + 1]).starts_with("no activity yet") && body(&t[m + 1]).ends_with(" 5m"), "{t:#?}");
    // Recently done (idle, 4 min): its outcome as plain text.
    let l = at("Lost suit jacket");
    assert!(t[l].contains("✓ done") && t[l + 1].starts_with(" ▎   Found it "), "{:?}", t[l + 1]);
    // Long done: idle, no rail and no word, its last outcome under it with
    // the age.
    let g = at("Long ago");
    assert!(t[g].trim_end().ends_with("Long ago") && t[g + 1].starts_with("     Old news") && t[g + 1].ends_with(" 1h  "), "{t:#?}");
    // Asking: the question, in `sub` (brighter than a dim detail).
    let k = at("Kua Yu");
    assert!(t[k].contains("◉ asks") && t[k + 1].starts_with(" ▎   Which deck?"), "{t:#?}");
    assert!(f.rows[k + 1].segs.contains(&seg("Which deck?", "sub")) && f.rows[g + 1].segs.contains(&seg("Old news", "overlay2")), "{t:#?}");
    // Needs cards: no `~/`, plain reasons.
    assert!(t.iter().any(|l| l.starts_with(" ▌ Kua Yu focus timing")), "{t:#?}");
    assert!(t.iter().any(|l| l.starts_with(" ▌   Which deck?")), "{t:#?}");
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
    let cx = Cx { w: 34, b: Badge::OFF, now: NOW as f64, spin: 0, home: None, cur: None, shells: true, resumes: false, ctx: CTX_STYLE };
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
        let pills = uncapped(&t[f.spaces.clone()].join("|"));
        assert!(pills.contains(" zz "), "{h}: {t:#?}");
        assert!(h < 8 || pills.contains(" s07 "), "{h}: {t:#?}");
        let shown = pills.matches(" s0").count() + pills.matches(" s1").count() + pills.matches(" zz ").count() + pills.matches(" main ").count();
        if shown < 13 {
            // The rest as `+K`, which opens the menu on All.
            let k: usize = pills.split(" +").nth(1).unwrap().split(' ').next().unwrap().parse().unwrap();
            assert_eq!(shown + k, 13, "{h}: {pills:?}");
            let y = f.spaces.clone().find(|&y| t[y].contains('+')).unwrap();
            assert_eq!(f.target(col(&t[y], "+"), y as u16), Some(&Target::Menu(Some("all"))));
        }
        check_accounting(&format!("cut {h}"), &v, &f);
    }
}

/// ONE space order: the pills and the agents' groups both go by
/// `@space_index` (⌘N), unindexed spaces after by name; hidden spaces have
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
    let group_order: Vec<String> = heads(&f).into_iter().map(|(_, s)| s).collect();
    let pills = uncapped(&t[f.spaces.clone()].join(" "));
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
    // The ⌘N digit leads each indexed pill; past 9 or unset, none.
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
    // The client's space in the dock: its ⌘N chip solid in its colour (dark
    // ink), its name bold on its band.
    let yellow = Ink::Rgb(0xf9, 0xe2, 0xaf);
    let p = row(&f, "main");
    assert!(p.segs.contains(&block(" 1 ", "crust", yellow)) && p.segs.contains(&bold("main", "text")) && p.bg == Some(band(yellow)), "{:?}", p);
    // Another: a status-line module, its digit on its colour (dark ink on a
    // light one, light on a dark one; peach for a colour tmux cannot read),
    // on the dock.
    assert!(row(&f, "bai").segs.contains(&block(" 2 ", "text", Ink::Idx(24))), "{:?}", row(&f, "bai").segs);
    assert!(row(&f, "zeta").segs.contains(&block(" 3 ", "crust", "peach")) && row(&f, "zeta").bg == Some(Ink::Pal(PANEL)), "{:?}", row(&f, "zeta"));
    // Space headers: the same chip, on the canvas; a dark colour's name in
    // plain text (it would not read in its own colour).
    let hd = &f.rows[head(&f, "bai")];
    assert!(hd.segs.contains(&block(" 2 ", "text", Ink::Idx(24))) && hd.segs.contains(&bold("bai", "text")), "{:?}", hd.segs);
    assert_eq!(hd.bg, None);
    // An alert space's row counts it; a current space with no colour is
    // peach (the status line's), red while the prefix is armed.
    let v = view(vec![a("main", 1, "running", "M"), a("bai", 1, "needs-input", "B")], Some("@1"));
    let f = layout(&v, 34, 40, &Opts::default());
    assert!(row(&f, "bai").text().contains("◉1"), "{:?}", row(&f, "bai").text());
    assert!(row(&f, "main").segs.contains(&block("   ", "crust", "peach")), "{:?}", row(&f, "main").segs);
    let f = layout(&v, 34, 40, &with_bar(Bar { prefix: true, ..Bar::default() }));
    assert!(row(&f, "main").segs.contains(&block("   ", "crust", "red")), "{:?}", row(&f, "main").segs);
    // (The header never goes red: it keeps the colour its agents' caps wear.)
    let hd = &f.rows[head(&f, "main")];
    assert!(hd.segs.contains(&block("   ", "crust", "peach")) && hd.bg == Some(band(Ink::Pal("peach"))), "{:?}", hd.segs);
}

/// Two coloured, indexed spaces, the client in cs35l; letters as the
/// sidebar gives them.
fn coloured() -> ViewModel {
    lettered(view(
        vec![
            a("main", 1, "running", "Canvas Syllabus").with("space_index", "1").with("space_color", "#b4befe"),
            a("main", 2, "idle", "Tmux sidebar hotkeys"),
            a("cs35l", 1, "needs-input", "Eight Component Values").with("space_index", "2").with("space_color", "#94e2d5")
                .with("detail_kind", "ask").with("detail", "Which one?"),
            a("cs35l", 2, "idle", "Lab 3"),
        ],
        Some("@3"),
    ))
}

/// A space's header in AGENTS (herdr's statusline module): on the canvas,
/// ` N ` solid in the space's colour (dark ink: the group's one fill), the
/// name bold in that colour, the branch dim, the counts right. The
/// client's space is findable at once: the only header on a band (its
/// colour washed over the cards' columns, apart from the card under it)
/// with a `▌` in its colour at the very edge. In both scopes, collapsed
/// too.
#[test]
fn space_headers_mark_the_current_space() {
    let lav = Ink::Rgb(0xb4, 0xbe, 0xfe);
    let teal = Ink::Rgb(0x94, 0xe2, 0xd5);
    let mut v = coloured();
    for sp in v.spaces.iter_mut().filter(|s| s.name == "main") {
        sp.branch = "trunk".into();
    }
    let f = layout(&v, 42, 62, &Opts::default());
    let t = texts(&f);
    let (m, c) = (head(&f, "main"), head(&f, "cs35l"));
    // Another space: on the canvas, its number chip, its name bold in its
    // colour, the branch dim.
    let main = &f.rows[m];
    assert!(main.bg.is_none() && t[m].starts_with("  1  main"), "{:?}", t[m]);
    assert!(main.segs.contains(&block(" 1 ", "crust", lav)) && main.segs.contains(&bold("main", lav)), "{:?}", main.segs);
    assert!(main.segs.contains(&seg("  \u{e0a0} trunk", "overlay2")) && t[m].ends_with("◐1 ○1  "), "{:?}", t[m]);
    // The client's: its band between canvas margins, the `▌` on the left
    // margin, its chip and name as any header's.
    let here = &f.rows[c];
    assert_eq!((here.bg, here.segs[0].clone(), here.segs.last().cloned()), (Some(band(teal)), Seg { bg: Some(Ink::Canvas), ..seg("▌", teal) }, Some(bare(1))));
    assert!(t[c].starts_with("▌ 2  cs35l") && here.segs.contains(&block(" 2 ", "crust", teal)) && here.segs.contains(&bold("cs35l", teal)), "{:?}", t[c]);
    assert_eq!(heads(&f).iter().filter(|(y, _)| f.rows[*y].bg.is_some() || t[*y].starts_with('▌')).count(), 1, "{t:#?}");
    // The band is a wash, and stands apart from the client's lifted card
    // right under it.
    let dist = |a: (u8, u8, u8), b: (u8, u8, u8)| ((f64::from(a.0) - f64::from(b.0)).powi(2) + (f64::from(a.1) - f64::from(b.1)).powi(2) + (f64::from(a.2) - f64::from(b.2)).powi(2)).sqrt();
    let (r, g, b) = ink_rgb(here.bg.unwrap());
    assert!(r < 0x48 && g < 0x58 && b < 0x58, "{:?}", (r, g, b));
    assert_eq!(f.rows[c + 1].bg, Some(Ink::Pal(MINE_BG)));
    assert!(dist((r, g, b), palette::rgb(MINE_BG)) >= 12.0);
    // Space scope: the same header.
    let mut sv = coloured();
    sv.scope.session = Some("cs35l".into());
    let f = layout(&sv, 42, 62, &Opts::default());
    let c = head(&f, "cs35l");
    assert!(f.rows[c].bg == Some(band(teal)) && texts(&f)[c].starts_with("▌ 2  cs35l"), "{:#?}", texts(&f));
    // Collapsed, it still reads so (and stands for its agents).
    let f = (8..40).rev().map(|h| layout(&v, 42, h, &Opts::default())).find(|f| f.agents_step >= 4).expect("a collapsed frame");
    let collapsed: Vec<(usize, String)> = heads(&f).into_iter().filter(|(y, _)| !f.rows[*y].covers.is_empty()).collect();
    assert!(!collapsed.is_empty(), "{:#?}", texts(&f));
    for (y, name) in collapsed {
        let sp = v.spaces.iter().find(|s| s.name == name).unwrap();
        let mine: Vec<(String, String)> = sp.agents.iter().map(|a| (a.session.clone(), a.window_id.clone())).collect();
        // It stands for its agents (all, or all but the one shown under it).
        assert!(f.rows[y].covers.iter().all(|c| mine.contains(c)) && f.rows[y].covers.len() + 1 >= mine.len(), "{name}: {:#?}", texts(&f));
        let ink = if name == "main" { lav } else { teal };
        assert!(f.rows[y].segs.iter().any(|s| s.bg == Some(ink)), "{name}: {:#?}", texts(&f));
        assert_eq!(f.rows[y].bg.is_some(), name == "cs35l", "{name}: {:#?}", texts(&f));
    }
}

/// The client's space's band is plainly not a card whatever its space's
/// hue: for every catppuccin accent it sits well away from the canvas and
/// every card shade (`tile`, `tile_lo`, the client's `tile_hi`), and its
/// quieter text (`sub`: the branch, the idle count) still reads.
#[test]
fn headers_stand_apart_from_cards_in_every_hue() {
    const ACCENTS: [(u8, u8, u8); 14] = [
        (0xf5, 0xe0, 0xdc), (0xf2, 0xcd, 0xcd), (0xf5, 0xc2, 0xe7), (0xcb, 0xa6, 0xf7), (0xf3, 0x8b, 0xa8), (0xeb, 0xa0, 0xac),
        (0xfa, 0xb3, 0x87), (0xf9, 0xe2, 0xaf), (0xa6, 0xe3, 0xa1), (0x94, 0xe2, 0xd5), (0x89, 0xdc, 0xeb), (0x74, 0xc7, 0xec),
        (0x89, 0xb4, 0xfa), (0xb4, 0xbe, 0xfe),
    ];
    let lin = |v: u8| {
        let s = f64::from(v) / 255.0;
        if s <= 0.03928 { s / 12.92 } else { ((s + 0.055) / 1.055).powf(2.4) }
    };
    let lum = |(r, g, b): (u8, u8, u8)| 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
    let contrast = |a, b| {
        let (x, y): (f64, f64) = (lum(a), lum(b));
        (x.max(y) + 0.05) / (x.min(y) + 0.05)
    };
    let dist = |a: (u8, u8, u8), b: (u8, u8, u8)| {
        let d = |x: u8, y: u8| (f64::from(x) - f64::from(y)).powi(2);
        (d(a.0, b.0) + d(a.1, b.1) + d(a.2, b.2)).sqrt()
    };
    for (r, g, b) in ACCENTS {
        let band = ink_rgb(band(Ink::Rgb(r, g, b)));
        for card in ["tile", "tile_lo", "tile_hi", palette::CANVAS] {
            let d = dist(band, palette::rgb(card));
            assert!(d >= 20.0, "#{r:02x}{g:02x}{b:02x} vs {card}: {d:.1}");
        }
        // The client's own card (lighter, its lavender edge): apart too.
        let d = dist(band, palette::rgb(MINE_BG));
        assert!(d >= 12.0, "#{r:02x}{g:02x}{b:02x} vs the own card: {d:.1}");
        // (Yellow's band is the lightest: about 3.9.)
        let c = contrast(palette::rgb("sub"), band);
        assert!(c >= 3.75, "#{r:02x}{g:02x}{b:02x}: sub at {c:.2}");
        // Its name, bold in the colour itself, reads on its own band.
        assert!(contrast((r, g, b), band) >= 4.5, "#{r:02x}{g:02x}{b:02x}");
    }
}

/// The key-cap of a card row: past the margin, the rail and a pad.
fn cap_of(r: &Row) -> Seg {
    r.segs[3].clone()
}

/// Every agent's letter is a key-cap in its SPACE's colour, in AGENTS and
/// in NEEDS YOU alike, so an agent says where it lives even mid-list. Quiet
/// by default (the letter bold in the colour, on the colour washed over its
/// card); SOLID (dark ink on the colour, the header chip's look) only where
/// you look next: the client's own agent, an asking or failed one, every
/// NEEDS YOU card. While ⌥W is armed every cap is inverted (a peach letter
/// on crust), a look no calm cap has, a peach space's solid one included.
#[test]
fn key_caps_are_quiet_but_where_you_look() {
    let lav = Ink::Rgb(0xb4, 0xbe, 0xfe);
    let teal = Ink::Rgb(0x94, 0xe2, 0xd5);
    let v = coloured();
    // (The blink's off second: a working cap its usual self.)
    let f = layout(&v, 42, 62, &blink_off());
    let t = texts(&f);
    let at = |title: &str| f.agents.start + agents_part(&f).iter().position(|l| l.contains(title)).unwrap();
    for (title, ink) in [("Canvas Syllabus", lav), ("Tmux sidebar hotkeys", lav), ("Lab 3", teal)] {
        let y = at(title);
        let c = cap_of(&f.rows[y]);
        let tint = over(ink, CAP_TINT, f.rows[y].bg.unwrap());
        assert_eq!((c.fg, c.bg, c.bold, c.text.trim().len()), (ink, Some(tint), true, 1), "{title}: {c:?}");
        // The same colour as its header's number chip.
        let hd = heads(&f).into_iter().rev().find(|(h, _)| *h < y).unwrap().0;
        assert!(f.rows[hd].segs.iter().any(|s| s.bg == Some(ink)), "{title}");
    }
    // The client's own (asking too): solid.
    let c = cap_of(&f.rows[at("Eight Component")]);
    assert_eq!((c.fg, c.bg, c.bold), (Ink::Pal("crust"), Some(teal), true), "{c:?}");
    // The NEEDS YOU card (cs35l's): solid teal too.
    let card = f.needs.clone().find(|&y| t[y].contains("Eight")).unwrap();
    assert_eq!((cap_of(&f.rows[card]).fg, cap_of(&f.rows[card]).bg), (Ink::Pal("crust"), Some(teal)));
    // Asking and failed: solid; working (its off second: [`a_working_cap_blinks`]),
    // done and idle: quiet (no client here).
    let v = lettered(view(
        vec![a("main", 1, "failed", "Broke"), a("main", 2, "needs-input", "Ask"), a("main", 3, "running", "Run"),
            a("main", 4, "done", "Done"), a("main", 5, "idle", "Calm")],
        None,
    ));
    let f = layout(&v, 42, 62, &blink_off());
    let peach = Ink::Pal("peach");
    for (title, solid) in [("Broke", true), ("Ask", true), ("Run", false), ("Done", false), ("Calm", false)] {
        let y = f.agents.start + agents_part(&f).iter().position(|l| l.contains(title)).unwrap();
        let c = cap_of(&f.rows[y]);
        let want = if solid { (Ink::Pal("crust"), Some(peach)) } else { (peach, Some(over(peach, CAP_TINT, f.rows[y].bg.unwrap()))) };
        assert_eq!((c.fg, c.bg), want, "{title}: {c:?}");
    }
    // A dark space colour: the quiet letter in `text` (it would not read).
    let v = lettered(view(vec![a("dark", 1, "idle", "Murky").with("space_color", "colour24")], None));
    let f = layout(&v, 42, 30, &Opts::default());
    assert_eq!(cap_of(&f.rows[find(&f, "Murky")]).fg, Ink::Pal("text"));
}

/// The frame's clock in the blink's off second (`NOW` is even: on).
fn blink_off() -> Opts {
    Opts { now: Some(NOW as f64 + 1.0), ..Opts::default() }
}

/// A working agent's key-cap BLINKS on the wall clock, so "working" reads
/// from the left edge: solid in the working blue in even seconds, its usual
/// look in odd ones (quiet, or the client's solid in its space's colour);
/// its rail steady blue down both rows in both. Asking, failed, done, idle
/// and paused caps never blink. The blink rides the spinner's redraws: the
/// working card's first row is already animated.
#[test]
fn a_working_cap_blinks() {
    assert!(blink_on(NOW as f64) && blink_on(NOW as f64 + 0.9) && !blink_on(NOW as f64 + 1.0) && blink_on(NOW as f64 + 2.0));
    assert!(blink_on(-2.0) && !blink_on(-0.5));
    let v = lettered(view(
        vec![a("main", 1, "failed", "Broke"), a("main", 2, "needs-input", "Ask"), a("main", 3, "running", "Run"),
            a("main", 4, "done", "Done"), a("main", 5, "idle", "Calm"), a("main", 6, "paused", "Rest"),
            a("main", 7, "running", "Mine")],
        Some("@7"),
    ));
    let peach = Ink::Pal("peach");
    let blue = Ink::Pal(cat_hue(Cat::Working));
    for (w, h) in [(44u16, 62u16), (34, 62), (34, 18)] {
        let on = layout(&v, w, h, &Opts { now: Some(NOW as f64 + 0.5), ..Opts::default() });
        let off = layout(&v, w, h, &Opts { now: Some(NOW as f64 + 1.5), ..Opts::default() });
        let row = |f: &Frame, title: &str| f.agents.start + agents_part(f).iter().position(|l| l.contains(title)).unwrap();
        for title in ["Broke", "Ask", "Run", "Done", "Calm", "Rest", "Mine"] {
            let ctx = format!("{w}x{h} {title}");
            let (y0, y1) = (row(&on, title), row(&off, title));
            let (c0, c1) = (cap_of(&on.rows[y0]), cap_of(&off.rows[y1]));
            match title {
                "Run" => {
                    assert_eq!((c0.fg, c0.bg, c0.bold), (Ink::Pal("crust"), Some(blue), true), "{ctx}");
                    assert_eq!((c1.fg, c1.bg), (peach, Some(over(peach, CAP_TINT, off.rows[y1].bg.unwrap()))), "{ctx}");
                }
                "Mine" => {
                    // Already solid: the ON second inverts ([`blink_cap`]).
                    assert_eq!((c0.fg, c0.bg, c0.bold), (blue, Some(Ink::Pal("crust")), true), "{ctx}");
                    assert_eq!((c1.fg, c1.bg), (Ink::Pal("crust"), Some(peach)), "{ctx}");
                }
                _ => assert_eq!(c0, c1, "{ctx}: never blinks"),
            }
            if matches!(title, "Run" | "Mine") {
                // The rail: steady working blue, both rows when there are
                // two, in either second; the row animates (the spinner's clock).
                for (f, y) in [(&on, y0), (&off, y1)] {
                    assert_eq!(f.rows[y].segs[1], seg("▎", "blue"), "{ctx}");
                    assert!(f.rows[y].anim && f.spin, "{ctx}");
                    let next = &f.rows[y + 1];
                    if next.covers.is_empty() && next.bg == f.rows[y].bg && next.target_at(1) == f.rows[y].target_at(1) {
                        assert_eq!(next.segs[1], seg("▎", "blue"), "{ctx}");
                    }
                }
            }
        }
        // Only the two working cards' caps differ between the seconds.
        let caps = |f: &Frame| f.agents.clone().filter(|&y| f.rows[y].covers.len() == 1).map(|y| cap_of(&f.rows[y])).collect::<Vec<_>>();
        let differ = caps(&on).iter().zip(caps(&off)).filter(|(a, b)| **a != *b).count();
        assert_eq!(differ, 2, "{w}x{h}");
    }
    // ⌥W armed: the armed look in both seconds (the jump table wins).
    let armed = |now: f64| layout(&v, 34, 62, &Opts { now: Some(now), bar: Bar { jump: true, ..Bar::default() }, ..Opts::default() });
    for f in [armed(NOW as f64), armed(NOW as f64 + 1.0)] {
        for title in ["Run", "Mine"] {
            let y = f.agents.start + agents_part(&f).iter().position(|l| l.contains(title)).unwrap();
            assert_eq!((cap_of(&f.rows[y]).fg, cap_of(&f.rows[y]).bg), (Ink::Pal("peach"), Some(Ink::Pal("crust"))), "{title}");
        }
    }
}

/// The blink shows whatever the space's colour: in a space as blue as the
/// working hue (blue itself, sapphire, sky) the client's own working card,
/// solid in that colour the off second, inverts the on second (a blue
/// letter on crust), never a solid blue that looks the same; a quiet
/// working cap there goes solid. PINNED cards blink exactly as in their
/// space (own or not).
#[test]
fn the_blink_shows_in_a_blue_space_and_in_pinned() {
    let blue = Ink::Pal(cat_hue(Cat::Working));
    for (hex, rgb) in [("#89b4fa", Ink::Rgb(0x89, 0xb4, 0xfa)), ("#74c7ec", Ink::Rgb(0x74, 0xc7, 0xec)), ("#89dceb", Ink::Rgb(0x89, 0xdc, 0xeb))] {
        for pin_mine in [false, true] {
            let mine = a("work", 1, "running", "Mine").with("space_color", hex);
            let v = lettered(view(
                vec![
                    if pin_mine { mine.with("pinned", "50") } else { mine },
                    a("work", 2, "running", "Pinned").with("space_color", hex).with("pinned", "100"),
                    a("work", 3, "idle", "Calm").with("space_color", hex),
                ],
                Some("@1"),
            ));
            for w in [44u16, 34] {
                let on = layout(&v, w, 40, &Opts { now: Some(NOW as f64), ..Opts::default() });
                let off = layout(&v, w, 40, &blink_off());
                let ctx = format!("{hex} {w} pin_mine={pin_mine}");
                let row = |f: &Frame, title: &str| f.agents.start + agents_part(f).iter().position(|l| l.contains(title)).unwrap();
                let cap = |f: &Frame, title: &str| cap_of(&f.rows[row(f, title)]);
                assert!(on.pinned.contains(&row(&on, "Pinned")), "{ctx}");
                assert_eq!(on.pinned.contains(&row(&on, "Mine")), pin_mine, "{ctx}");
                // The client's own: solid in the space's colour, then inverted.
                let (c0, c1) = (cap(&on, "Mine"), cap(&off, "Mine"));
                assert_eq!((c1.fg, c1.bg), (Ink::Pal("crust"), Some(rgb)), "{ctx}");
                assert_eq!((c0.fg, c0.bg, c0.bold), (blue, Some(Ink::Pal("crust")), true), "{ctx}");
                // The pinned one (not the client's): quiet, then solid blue.
                let (p0, p1) = (cap(&on, "Pinned"), cap(&off, "Pinned"));
                let bg = off.rows[row(&off, "Pinned")].bg.unwrap();
                assert_eq!((p1.fg, p1.bg), (rgb, Some(over(rgb, CAP_TINT, bg))), "{ctx}");
                assert_eq!((p0.fg, p0.bg), (Ink::Pal("crust"), Some(blue)), "{ctx}");
                // Idle: the same both seconds.
                assert_eq!(cap(&on, "Calm"), cap(&off, "Calm"), "{ctx}");
            }
        }
    }
}

/// ⌥W armed: EVERY cap with a letter, in AGENTS and NEEDS YOU, quiet or
/// solid, turns into the one armed look (a bold peach letter on crust),
/// and no calm cap anywhere looks like it, whatever its space's colour (a
/// colourless space's solid caps are peach themselves).
#[test]
fn armed_caps_are_unmistakable() {
    let armed_look = |s: &Seg| s.fg == Ink::Pal(ARMED.0) && s.bg == Some(Ink::Pal(ARMED.1)) && s.bold;
    let caps = |f: &Frame| -> Vec<(String, Seg)> {
        let t = texts(f);
        f.rows.iter().enumerate().filter(|(y, r)| {
            let card = f.needs.contains(y) && r.bg.is_some() && r.segs.len() > 3 && r.segs[1].text == "▌";
            let agent = r.covers.len() == 1 && f.agents.contains(y);
            (card || agent) && r.segs[3].text.trim().len() == 1
        }).map(|(y, r)| (t[y].clone(), r.segs[3].clone())).collect()
    };
    let mut fleets = vec![coloured(), lettered(quiet()), lettered(busy()), lettered(extreme())];
    fleets.push(lettered(view(
        vec![a("plain", 1, "needs-input", "Colourless"), a("warm", 1, "failed", "Peachy").with("space_color", "#fab387"),
            a("plain", 2, "idle", "Quiet one")],
        Some("@1"),
    )));
    for v in fleets {
        let calm = layout(&v, 42, 70, &with_bar(demo_bar()));
        let armed = layout(&v, 42, 70, &with_bar(Bar { jump: true, ..demo_bar() }));
        let (c, a) = (caps(&calm), caps(&armed));
        assert!(!c.is_empty() && c.len() == a.len(), "{:#?}", texts(&calm));
        // An agent with no letter (past 26) keeps the column even: a dim
        // dot in the armed look's crust.
        if let Some(r) = armed.rows.iter().find(|r| r.covers.len() == 1 && r.segs.len() > 3 && r.segs[3].text == " · ") {
            assert_eq!((r.segs[3].fg, r.segs[3].bg), (Ink::Pal("overlay2"), Some(Ink::Pal(ARMED.1))));
        }
        for ((line, cs), (_, as_)) in c.iter().zip(&a) {
            assert!(!armed_look(cs), "a calm cap looks armed: {line:?} {cs:?}");
            assert!(armed_look(as_) && as_.text == cs.text, "an armed cap does not: {line:?} {as_:?}");
        }
        // The mark says so too, a tab on the panel's top cap.
        assert!(armed.rows[armed.card.start].segs.contains(&block(" ⌥W jump ", "crust", "peach")));
    }
}

/// Cards stack edge to edge: no pad row between them (NEEDS YOU, agents,
/// shells), at any size; every card is ONE colour (both its rows), and the
/// card right under it another (the zebra, the client's lift, a tint), so
/// neighbours part with no gap.
#[test]
fn cards_are_one_colour_and_neighbours_differ() {
    for (name, v) in all().into_iter().chain([("coloured", coloured())]) {
        for (w, h) in [34u16, 24].into_iter().flat_map(|w| (8u16..=90).map(move |h| (w, h))) {
            let f = layout(&v, w, h, &with_bar(demo_bar()));
            let t = texts(&f);
            let name = format!("{name} {w}x{h}");
            let lists = f.needs.start..body_end(&f);
            assert!(!t[lists.clone()].iter().any(|l| is_pad(l)), "{name}: {t:#?}");
            // A card: a tile row clicking through to something (not a header).
            let card = |y: usize| {
                let r = &f.rows[y];
                (r.bg.is_some() && !is_group_header(r, usize::from(w)) && r.target_at(1).is_some()).then(|| r.target_at(1).cloned())
            };
            for y in lists.start..lists.end.saturating_sub(1) {
                let (Some(a), Some(b)) = (card(y), card(y + 1)) else { continue };
                let (x, z) = (f.rows[y].bg, f.rows[y + 1].bg);
                if a == b {
                    assert_eq!(x, z, "{name} {y}: one card, one colour {t:#?}");
                } else {
                    assert_ne!(x, z, "{name} {y}: neighbouring cards share a colour {t:#?}");
                }
            }
        }
    }
    // A NEEDS YOU card too: one tint, both rows.
    let f = layout(&quiet(), 34, 58, &Opts::default());
    let k = find(&f, "▌ Kua Yu");
    assert_eq!((f.rows[k].bg, f.rows[k + 1].bg), (Some(Ink::Mix("yellow", NEED_TINT[0])), Some(Ink::Mix("yellow", NEED_TINT[0]))));
    // The zebra starts afresh under each space's header.
    let f = layout(&busy(), 34, 90, &Opts::default());
    for (y, _) in heads(&f) {
        let next = &f.rows[y + 1];
        assert!(next.bg == Some(Ink::Pal("tile")) || next.bg == Some(Ink::Pal(MINE_BG)), "{y}: {:#?}", texts(&f));
    }
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
/// assignment, nothing stored yet): the listed agents and shells in display
/// order ([`crate::menu::letters::lettered`]).
fn lettered(mut v: ViewModel) -> ViewModel {
    let listed: Vec<(String, String)> =
        crate::menu::letters::lettered(&v).into_iter().map(|a| (a.window_id.clone(), String::new())).collect();
    let got = crate::menu::letters::assign(&listed, &[]).letters;
    for x in v.spaces.iter_mut().flat_map(|s| s.agents.iter_mut().chain(s.plain.iter_mut())) {
        x.letter = got.get(&x.window_id).copied();
    }
    for n in v.needs.iter_mut() {
        n.agent.letter = got.get(&n.agent.window_id).copied();
    }
    v
}

/// Spaces with HOMES and SHELLS, `@spaces` on, every space shown (scope
/// `scope`), the client on `here`, letters as the sidebar assigns them:
///
/// - main (⌘1): home @1; agents @2 working (main:1), @3 idle (main:2), @4
///   asking (main:3); shells @5 at a prompt in agent-ui (main:4), @6
///   running nvim (main:5);
/// - work (⌘2): home @7; agent @8 working; shell @9 in cua-notch;
/// - notes (⌘3): home @10; shell @11 in notes, no agent.
///
/// Letters (display order: each space's agents, then its shells): @2 a,
/// @3 s, @4 d, @5 f, @6 g, @8 h, @9 j, @11 k; homes none.
fn shells_at(here: &str, scope: &str, extra: &[(&str, &str, &str)]) -> ViewModel {
    let since = |st: &str| format!("{} {st}", NOW - 90);
    let mut rows = vec![
        row("main", 0, "@1", &[("home", "1"), ("space_index", "1"), ("path", "/x/main")]),
        row("main", 1, "@2", &[("state", "running"), ("summary", "Tmux Agent Sidebar"), ("since", &since("running")), ("space_index", "1"),
            ("detail_kind", "run"), ("detail", "Edit layout.rs")]),
        row("main", 2, "@3", &[("state", "idle"), ("summary", "Commodities Job Tracker"), ("since", &since("idle")), ("space_index", "1")]),
        row("main", 3, "@4", &[("state", "needs-input"), ("summary", "Kua Yu focus timing"), ("since", &since("needs-input")),
            ("detail_kind", "ask"), ("detail", "Which deck?"), ("space_index", "1")]),
        row("main", 4, "@5", &[("path", "/x/agent-ui"), ("space_index", "1")]),
        row("main", 5, "@6", &[("path", "/x/agent-ui"), ("space_index", "1")]).replacen("zsh", "nvim", 1),
        row("work", 0, "@7", &[("home", "1"), ("space_index", "2")]),
        row("work", 1, "@8", &[("state", "running"), ("summary", "Island resize spring"), ("since", &since("running")), ("space_index", "2")]),
        row("work", 2, "@9", &[("path", "/x/cua-notch"), ("space_index", "2")]),
        row("notes", 0, "@10", &[("home", "1"), ("space_index", "3")]),
        row("notes", 1, "@11", &[("path", "/x/notes"), ("space_index", "3")]),
        row("agents", 1, "@900", &[("state", "failed"), ("summary", "hidden")]),
    ];
    let session = rows.iter().find(|r| r.split('\x1f').nth(2) == Some(here)).and_then(|r| r.split('\x1f').next()).unwrap().to_string();
    // ("classic", ..) in `extra`: @spaces off.
    let spaces = if extra.iter().any(|e| e.0 == "classic") { "" } else { "on" };
    rows.push(format!("\x1fclient\x1f{CLIENT}\x1f{here}\x1f{session}\x1f{spaces}\x1f{scope}\x1f"));
    let mut snap = Snapshot::parse(&rows.join("\n"));
    for (id, k, v) in extra.iter().filter(|e| e.0 != "classic") {
        for w in snap.windows.iter_mut().filter(|w| w.id == *id) {
            match *k {
                "stash_sel" => w.stash_sel = *v == "1",
                "state" => w.state = v.to_string(),
                _ => panic!("{k}"),
            }
        }
    }
    let c = Collator::new("en_US.UTF-8");
    let args = BuildArgs { client: Some(CLIENT), now: NOW as f64, collator: &c, git: None, log: vec![], watcher_age: Some(1.0) };
    let mut v = ViewModel::build(&snap, args);
    let got = crate::menu::letters::of_view(&snap, &v, None).letters;
    for x in v.spaces.iter_mut().flat_map(|s| s.agents.iter_mut().chain(s.plain.iter_mut())) {
        x.letter = got.get(&x.window_id).copied();
    }
    for n in v.needs.iter_mut() {
        n.agent.letter = got.get(&n.agent.window_id).copied();
    }
    v.apply_scope(&snap);
    v
}

/// [`shells_at`] with the client on main's agent-ui shell, every space shown.
fn shelly() -> ViewModel {
    shells_at("@5", "all", &[])
}

/// A bar like the live one: main (⌘1, yellow), usage meters (the weekly
/// one from its numbers, the session one from its text) and a stale save.
fn demo_bar() -> Bar {
    use super::super::status::{Chip, Member};
    Bar {
        space: Some(Chip { index: Some(1), name: "main".into(), color: Some(Ink::Rgb(0xf9, 0xe2, 0xaf)), hidden: false }),
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

/// Every agent row and NEEDS YOU card shows its letter as a key-cap on the
/// card grid (rail, pad, cap, pad, title), in both scopes; inverted while
/// ⌥W's jump table is armed; blank past 26.
#[test]
fn letter_key_caps_lead_every_agent_row_and_card() {
    let mut v = lettered(quiet());
    let green = Ink::Rgb(0xa6, 0xe3, 0xa1);
    for sp in v.spaces.iter_mut().filter(|s| s.name == "main") {
        sp.color = "#a6e3a1".into();
    }
    let f = layout(&v, 34, 58, &blink_off());
    let t = texts(&f);
    // The client's own: solid (working too, in the blink's off second).
    let r = find(&f, "Tmux Agent S");
    assert!(t[r].starts_with(" ▎  f  Tmux Agent S"), "{:?}", t[r]);
    assert!(t[r + 1].starts_with(" ▎     Edit agent-roster.py"), "{:?}", t[r + 1]);
    assert_eq!(cap_of(&f.rows[r]), block(" f ", "crust", green));
    // A NEEDS YOU card: solid.
    let c = find(&f, "▌  g  Kua Yu focus");
    assert_eq!(cap_of(&f.rows[c]), block(" g ", "crust", green));
    assert!(t[c + 1].starts_with(" ▌     Which deck"), "{t:#?}");
    // Done (not the client's, not asking): quiet.
    let i = find(&f, "▎  h  Commodities Job");
    assert_eq!(cap_of(&f.rows[i]), block(" h ", green, over(green, CAP_TINT, Ink::Pal("tile_lo"))));
    // Another space's (no colour of its own): peach, the status line's.
    let b = find(&f, "Application link");
    assert_eq!(cap_of(&f.rows[b]).fg, Ink::Pal("peach"));
    // Space scope: the same letters.
    let mut sv = lettered(quiet());
    sv.scope.session = Some("main".into());
    let f = layout(&sv, 34, 58, &Opts::default());
    find(&f, " f  Tmux Agent S");
    find(&f, "▌  g  Kua Yu");
    // ⌥W armed (key table `jump`): every cap inverted (peach on crust); the
    // mark and the hint a tab on the panel's top pad, the panel no taller
    // for it.
    let f = layout(&v, 34, 58, &with_bar(Bar { jump: true, ..demo_bar() }));
    let top = &f.rows[f.card.start];
    assert!(top.segs.contains(&block(" ⌥W jump ", "crust", "peach")), "{:?}", top.segs);
    assert_eq!(top.text(), format!(" ▄ ⌥W jump  type a letter {} ", "▄".repeat(7)));
    assert_eq!(f.card, layout(&v, 34, 58, &with_bar(demo_bar())).card);
    for (title, k) in [("Tmux Agent S", 'f'), ("▌  g  Kua Yu", 'g'), ("Commodities Job", 'h'), ("Application link", 'a')] {
        assert_eq!(cap_of(&f.rows[find(&f, title)]), block(format!(" {k} "), "peach", "crust"), "{title}");
    }
    // Narrow: the hint gives way, the mark stays whole, the pad's ends too.
    let f = layout(&v, 22, 58, &with_bar(Bar { jump: true, ..demo_bar() }));
    let top = f.rows[f.card.start].text();
    assert!(top.starts_with(" ▄ ⌥W jump  type") && top.ends_with("▄ ") && dwidth(&top) == 22, "{top:?}");
    // Past 26 agents: a blank cap, the title still aligned.
    let x = lettered(extreme());
    let none = x.agents().find(|a| a.letter.is_none()).expect("an agent past 26");
    let f = layout(&x, 60, 400, &Opts::default());
    let k = f.rows.iter().position(|r| r.covers.iter().any(|(s, id)| *s == none.session && *id == none.window_id)).unwrap();
    assert_eq!(cap_of(&f.rows[k]).text, "   ", "{:?}", f.rows[k].text());
    assert!(f.rows[k].text().chars().skip(2).collect::<String>().starts_with("     An agent"), "{:?}", f.rows[k].text());
    // ⌥W armed: its cap joins the armed column (a dim dot on crust), so the
    // column reads as one.
    let f = layout(&x, 60, 400, &with_bar(Bar { jump: true, ..Bar::default() }));
    assert_eq!(cap_of(&f.rows[k]), on(" · ", "overlay2", "crust"), "{:?}", f.rows[k].text());
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
    // The meters: label, bar, percent, delta (a real minus, no brackets),
    // no Σ even for a pooled pace; the label, the fill and the delta in the
    // pace's colour: green under it, red well over it.
    assert!(t[1].starts_with("  S ━") && t[1].contains(" 13%  −38% "), "{:?}", t[1]);
    assert!(t[2].starts_with("  W ━") && t[2].contains(" 60%  +17% "), "{:?}", t[2]);
    assert!(!t.iter().any(|l| l.contains('Σ')), "{t:#?}");
    assert!(f.rows[1].segs.contains(&seg("  −38%", "green")) && f.rows[1].segs.contains(&bold("S", "green")), "under pace: green");
    assert!(f.rows[2].segs.contains(&seg("  +17%", "red")) && f.rows[2].segs.contains(&bold("W", "red")), "over pace: red");
    assert!(t[3].starts_with("  F ━") && t[3].contains("   -- "), "{:?}", t[3]);
    assert!(t[4].contains(" ! ") && t[4].contains("last save 2h ago"), "{:?}", t[4]);
    // A panel: its caps, the content on `tile_lo` between canvas margins,
    // no line art.
    assert_eq!((t[0].clone(), t[5].clone()), (format!(" {} ", "▄".repeat(32)), format!(" {} ", "▀".repeat(32))));
    for y in 1..5 {
        let r = &f.rows[y];
        assert_eq!((r.bg, r.segs[0].clone(), r.segs.last().cloned()), (Some(Ink::Pal(PANEL)), bare(1), Some(bare(1))), "{y}");
    }
    assert!(f.rows[0].segs.contains(&seg("▄".repeat(32), PANEL)) && f.rows[0].bg.is_none());
    let mut buf = Buffer::empty(Rect::new(0, 0, 34, 40));
    paint(&f, &mut buf);
    let canvas = palette::color("mantle");
    assert_eq!((buf[(0, 2)].bg, buf[(1, 2)].bg, buf[(32, 2)].bg, buf[(33, 2)].bg),
        (canvas, palette::color(PANEL), palette::color(PANEL), canvas));
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

/// Shells are rows of their SPACE, after its agents: one row each, a
/// TERMINAL LINE on its sunken strip (`term`, one colour for the row), the
/// key-cap exactly an agent's quiet cap (as over `tile`), a green bold `❯`,
/// then the cwd at a prompt (`sub`) or the command bold `text` with its
/// folder dim; the client's own lifted like its own agent card. No home row
/// anywhere (the header is the home), no SHELLS section, a space with
/// nothing but shells still a group; each row clicks through to its shell.
#[test]
fn shells_are_rows_of_their_space() {
    let v = shelly();
    let f = layout(&v, 44, 60, &with_bar(demo_bar()));
    let t = texts(&f);
    assert_eq!((f.step, f.agents_step), (0, 0), "{t:#?}");
    assert!(!t.iter().any(|l| l.contains("SHELLS")), "{t:#?}");
    // main: its header, its three agent cards (two rows each), then its two
    // shells. Letters: every agent first (a s d, work's f), then the shells.
    let main = head(&f, "main");
    let (own, nvim) = (find(&f, "/x/agent-ui"), find(&f, "nvim"));
    assert_eq!((own, nvim), (main + 7, main + 8), "{t:#?}");
    assert!(t[nvim].starts_with("    h  ❯ nvim  agent-ui ") && t[nvim].ends_with("  "), "{:?}", t[nvim]);
    let peach = Ink::Pal("peach");
    assert_eq!(cap_of(&f.rows[nvim]), block(" h ", peach, over(peach, CAP_TINT, Ink::Pal("tile"))));
    assert_eq!(f.rows[nvim].bg, Some(Ink::Pal(TERM_BG)));
    for s in [bold(PROMPT, "green"), bold("nvim", "text"), seg("  agent-ui", "overlay2")] {
        assert!(f.rows[nvim].segs.contains(&s), "{s:?} in {:?}", f.rows[nvim].segs);
    }
    // The client's own shell (at a prompt): lifted like its own card (the cap solid).
    assert!(t[own].starts_with(" ▎  g  ❯ /x/agent-ui") && t[own].ends_with(" █ "), "{:?}", t[own]);
    assert_eq!((f.rows[own].bg, f.rows[own].segs[1].clone(), cap_of(&f.rows[own])),
        (Some(Ink::Pal(MINE_BG)), seg("▎", "lavender"), block(" g ", "crust", peach)));
    assert!(f.rows[own].segs.contains(&seg("/x/agent-ui", "text")));
    let shell = |win: &str, s: &str| Some(Target::Shell { win: win.into(), session: s.into() });
    for (y, win) in [(own, "@5"), (nvim, "@6")] {
        assert_eq!((f.target(0, y as u16).cloned(), f.target(43, y as u16).cloned()), (shell(win, "main"), shell(win, "main")));
    }
    // work: its agent, then its shell (a prompt: its cwd, in `sub`); notes:
    // no agent, its shell alone.
    let work = head(&f, "work");
    assert!(t[work + 3].starts_with("    j  ❯ /x/cua-notch"), "{t:#?}");
    assert!(f.rows[work + 3].segs.contains(&seg("/x/cua-notch", "sub")), "{:?}", f.rows[work + 3].segs);
    let notes = head(&f, "notes");
    assert!(t[notes + 1].starts_with("    k  ❯ /x/notes"), "{t:#?}");
    assert_eq!(f.target(5, (notes + 1) as u16).cloned(), shell("@11", "notes"));
    assert!(main < work && work < notes, "⌘N order: {t:#?}");
    // No home anywhere: nothing goes to @1, @7 or @10.
    for r in &f.rows {
        for (_, _, tg) in &r.targets {
            if let Target::Go { win, .. } | Target::Shell { win, .. } = tg {
                assert!(!["@1", "@7", "@10"].contains(&win.as_str()), "{tg:?}: {t:#?}");
            }
        }
    }
    // Shell rows stand for no agent: the accounting is the agents'.
    check_accounting("shells", &v, &f);
    check_order("shells", &f);
    // @spaces off (the classic sidebar): no shell rows, no shell letters.
    let classic = shells_at("@2", "", &[("classic", "", "")]);
    assert!(!classic.shells && crate::menu::letters::lettered(&classic).iter().all(|a| !a.state.is_empty()));
    let f = layout(&classic, 44, 60, &Opts::default());
    assert!(!texts(&f).iter().any(|l| l.contains(PROMPT.trim())) && !heads(&f).iter().any(|(_, s)| s == "notes"), "{:#?}", texts(&f));
}

/// The cwd as a prompt shows it: `~` for $HOME, cut from the LEFT at a
/// folder boundary when it does not fit (`…/tmux/agent-ui`), a last folder
/// too long on its own cut mid-name.
#[test]
fn shell_paths_cut_from_the_left() {
    assert_eq!(clip_left("~/code/agent-ui", 20), "~/code/agent-ui");
    assert_eq!(clip_left("~/.local/share/chezmoi/dot_config/tmux/agent-ui", 26), "…/dot_config/tmux/agent-ui");
    assert_eq!(clip_left("~/.local/share/chezmoi/dot_config/tmux/agent-ui", 16), "…/tmux/agent-ui");
    assert_eq!(clip_left("~/x/averyveryverylongfolder", 10), "…ongfolder");
    assert_eq!(clip_left("/a/b", 0), "");
}

/// The space's header IS its home: a click goes there ([`Target::Session`],
/// which lands on the home window), and while the client is ON the home the
/// header wears the client's own lift (`tile_me`, the lavender edge)
/// instead of its band, in either scope.
#[test]
fn the_header_is_the_home() {
    let f = layout(&shelly(), 44, 60, &Opts::default());
    let main = head(&f, "main");
    assert_eq!(f.rows[main].bg, Some(band(Ink::Pal("peach"))), "{:#?}", texts(&f));
    assert!(!texts(&f)[main].contains('█'));
    for scope in ["all", "space"] {
        let f = layout(&shells_at("@1", scope, &[]), 44, 60, &Opts::default());
        let t = texts(&f);
        let main = head(&f, "main");
        assert_eq!(f.rows[main].bg, Some(Ink::Pal(MINE_BG)), "{scope}: {t:#?}");
        assert!(t[main].starts_with("▌ 1  main") && t[main].ends_with(" █ "), "{scope}: {:?}", t[main]);
        let n = f.rows[main].segs.len();
        assert_eq!(f.rows[main].segs[n - 2], seg("█", "lavender"), "{scope}");
        assert_eq!(f.target(20, main as u16), Some(&Target::Session("main".into())));
        // The only lifted row: no own card, no own shell.
        assert_eq!(f.rows.iter().filter(|r| r.bg == Some(Ink::Pal(MINE_BG))).count(), 1, "{scope}: {t:#?}");
        if scope == "all" {
            assert!(f.rows[head(&f, "work")].bg.is_none(), "{t:#?}");
        }
    }
    // Narrow (no second lines, no edges): the lift without the edge.
    let f = layout(&shells_at("@1", "all", &[]), 24, 60, &Opts::default());
    let main = head(&f, "main");
    assert!(f.rows[main].bg == Some(Ink::Pal(MINE_BG)) && !texts(&f)[main].contains('█'), "{:#?}", texts(&f));
}

/// ⌥W armed: a shell's cap is inverted like every agent's.
#[test]
fn jump_mode_inverts_shell_caps() {
    let f = layout(&shelly(), 44, 60, &with_bar(Bar { jump: true, ..demo_bar() }));
    for (needle, k) in [("nvim", 'h'), ("/x/agent-ui", 'g'), ("/x/cua-notch", 'j'), ("Tmux Agent", 'a')] {
        assert_eq!(cap_of(&f.rows[find(&f, needle)]), block(format!(" {k} "), "peach", "crust"), "{needle}");
    }
}

/// Squeezed to a step that folds the idle agents, a space's shells fold
/// too: two or more in ONE line on the shells' strip, each its key-cap and
/// label (its command, else its folder) two cells apart, each clicking
/// through to its shell; `+K` for those that do not fit, at any width.
#[test]
fn shells_fold_into_one_line() {
    let v = shelly();
    let f = (10..80).rev().map(|h| layout(&v, 44, h, &Opts::default())).find(|f| f.agents_step == 3).expect("a folding step");
    let t = texts(&f);
    let y = find(&f, "agent-ui");
    assert!(t[y].starts_with("    g  agent-ui   h  nvim"), "{t:#?}");
    assert_eq!(f.rows[y].bg, Some(Ink::Pal(TERM_BG)));
    let shell = |win: &str| Some(Target::Shell { win: win.into(), session: "main".into() });
    assert_eq!((f.target(4, y as u16).cloned(), f.target(col(&t[y], "nvim"), y as u16).cloned()), (shell("@5"), shell("@6")));
    assert_eq!(f.target(1, y as u16), None);
    // A lone shell never folds (work's).
    assert!(t.iter().any(|l| l.starts_with("    j  ❯ /x/cua-notch")), "{t:#?}");
    // Many: what does not fit is counted.
    let base = v.spaces.iter().flat_map(|s| s.plain.iter()).find(|a| a.window_id == "@6").unwrap().clone();
    let many: Vec<Agent> = (1..=12).map(|n| Agent { window_id: format!("@{}", 100 + n), shell: format!("sh{n}"), letter: None, ..base.clone() }).collect();
    let refs: Vec<&Agent> = many.iter().collect();
    for w in [16usize, 24, 34, 60, 200] {
        for on in [true, false] {
            let cx = Cx { w, b: Badge { on, loud: false }, now: NOW as f64, spin: 0, home: None, cur: None, shells: true, resumes: false, ctx: CTX_STYLE };
            let r = shells_line(&refs, cx, Ink::Pal("peach"));
            let text = r.text();
            assert_eq!(width(&r.segs), w, "{w}: {text:?}");
            let more = text.split('+').nth(1).map_or(0, |k| k.trim().parse::<usize>().unwrap());
            assert_eq!(r.targets.len() + more, 12, "{w}: {text:?}");
            assert!(r.targets.iter().all(|(a, b, _)| a < b && usize::from(*b) < w), "{w}: {:?}", r.targets);
            assert_eq!(more == 0, w == 200, "{w}: {text:?}");
        }
    }
}

/// A meter's bar as (glyph, ink) cells: from its fourth cell, the `━` and
/// `┃` run.
fn bar_cells(r: &Row) -> Vec<(char, Ink)> {
    r.segs.iter().flat_map(|s| s.text.chars().map(move |c| (c, s.fg))).skip(3).take_while(|(c, _)| "━┃".contains(*c)).collect()
}

/// A meter: Textual's progress bar, filled to the used percent over a
/// `surface1` track, the tick where the pace expects it, the reset hint
/// only when the bar keeps its length. The label, the fill and the delta
/// wear the pace's colour: green on or under it, peach up to 15 points
/// over, red beyond; without a pace, the status line's colour.
#[test]
fn meters_fill_and_tick() {
    let u = Usage { label: "W".into(), color: Some(Ink::Pal("blue")), used: Some(60.0), expected: Some(43.0), delta: Some(17),
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
    assert!(t.ends_with(" 60%  +17% ") || t.ends_with("↻3h "), "{t:?}");
    assert!(r.segs.contains(&bold("W", "red")) && r.segs.contains(&seg("  +17%", "red")), "{:?}", r.segs);
    // Wide: the reset hint too.
    assert!(meter_row(&u, 60, NOW as f64).text().ends_with(" 60%  +17%  ↻3h "), "{:?}", meter_row(&u, 60, NOW as f64).text());
    // A little over: peach; under pace: green, a real minus; on pace:
    // `±0%`, green; no pace: the status line's colour.
    let little = Usage { delta: Some(15), ..u.clone() };
    assert!(meter_row(&little, 40, 0.0).segs.contains(&seg("  +15%", "peach")));
    let under = Usage { delta: Some(-4), ..u.clone() };
    assert!(meter_row(&under, 40, 0.0).segs.contains(&seg("   −4%", "green")), "{:?}", meter_row(&under, 40, 0.0).segs);
    let even = Usage { delta: Some(0), pooled: true, ..u.clone() };
    assert!(meter_row(&even, 40, 0.0).text().contains(" 60%   ±0% "), "{:?}", meter_row(&even, 40, 0.0).text());
    assert!(meter_row(&even, 40, 0.0).segs.contains(&bold("W", "green")));
    let paceless = Usage { delta: None, expected: None, ..u.clone() };
    assert!(meter_row(&paceless, 40, 0.0).segs.contains(&bold("W", "blue")));
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
        (t[find(&f, "Lost suit jacket") + 1].clone(), t[find(&f, "Edit agent-roster.py")].clone(), t[find(&f, "▌ Kua Yu") + 1].clone())
    };
    let (idle, msg, card) = at(None);
    assert!(idle.ends_with(" 6m  ") && msg.ends_with(" 2m █ ") && card.ends_with(" 3m  "), "{idle:?} {msg:?} {card:?}");
    let (idle, msg, card) = at(Some(NOW as f64 + 60.0));
    assert!(idle.ends_with(" 7m  ") && msg.ends_with(" 3m █ ") && card.ends_with(" 4m  "), "{idle:?} {msg:?} {card:?}");
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
/// row on a mauve tint with a mauve stripe; the hint.
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
    bar.selecting = Some(2);
    let f = layout(&v, 34, 40, &with_bar(bar.clone()));
    let t = texts(&f);
    let r = find(&f, "Sel one");
    // (One colour, like every card.)
    for row in &f.rows[r..r + 2] {
        assert_eq!(row.bg, Some(Ink::Mix("mauve", 18)), "{:?}", row.text());
        assert_eq!(row.segs[1], seg("▌", "mauve"));
    }
    let p = find(&f, "Plain");
    assert_ne!(f.rows[p].bg, Some(Ink::Mix("mauve", 18)));
    // A selected shell: its row on the tint, the heavy mauve stripe; the
    // other shell on its strip.
    let sv = shells_at("@2", "all", &[("@6", "stash_sel", "1")]);
    let tall = layout(&sv, 44, 62, &with_bar(bar.clone()));
    let sh = find(&tall, "nvim");
    assert!(texts(&tall)[sh].starts_with(" ▌  h  ❯ nvim"), "{:#?}", texts(&tall));
    assert_eq!((tall.rows[sh].bg, tall.rows[sh].segs[1].clone()), (Some(Ink::Mix("mauve", 18)), seg("▌", "mauve")));
    assert_eq!(tall.rows[find(&tall, "/x/agent-ui")].bg, Some(Ink::Pal(TERM_BG)));
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
    // The scope: a key hint (the key bold blue, the word dim), no fill.
    assert!(b[0].starts_with(" AGENTS") && b[0].ends_with(" ⌥V this space "), "{b:#?}");
    assert!(f.rows[f.agents.start].segs.contains(&bold("⌥V", "blue")) && f.rows[f.agents.start].segs.iter().all(|s| s.bg.is_none()), "{b:#?}");
    assert_eq!(heads(&f), [(f.agents.start + 1, "proj".to_string())], "{b:#?}");
    // Window-index order: the agents, then the shells (each its prompt
    // line, the cwd; the client's own first, lifted).
    let rows: Vec<&str> = b[2..].iter().filter_map(|l| row_title(l)).collect();
    let g = format!("{PROMPT}/nonexistent/proj");
    assert_eq!(rows, ["two", "four", "five", "twelve", &g, &g, &g], "{b:#?}");
    let own = f.agents.start + b.iter().position(|l| l.contains(&g)).unwrap();
    assert_eq!(f.rows[own].bg, Some(Ink::Pal(MINE_BG)), "{b:#?}");
    assert_eq!(f.target(3, own as u16), Some(&Target::Shell { win: "@1".into(), session: "proj".into() }));
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
    // The client's shell is a tile (the classic rule), its lavender rail,
    // no state word.
    let z = b.iter().find(|l| l.contains("zsh")).unwrap_or_else(|| panic!("{b:#?}"));
    assert!(z.starts_with(" ▎ zsh") && z.trim_end().ends_with('█'), "{z:?}");
    assert!(b.iter().any(|l| row_title(l) == Some("two")) && b.iter().any(|l| row_title(l) == Some("twelve")), "{b:#?}");
}

#[test]
fn all_scope_lists_every_space_with_its_shells_and_toggles() {
    let v = scoped(proj(), "@1", "proj", "on", "all", "");
    assert_eq!(v.scope.session, None);
    let f = layout(&v, 34, 58, &Opts::default());
    let t = texts(&f);
    assert!(t[0].starts_with(" agents ") && t[0].ends_with("✕1 ◉1 ◐2 ○1 "), "{:?}", t[0]);
    assert_eq!(f.target(3, 0), Some(&Target::Scope));
    let b = agents_part(&f);
    assert!(b[0].contains("all spaces"), "{b:#?}");
    let names: Vec<String> = heads(&f).into_iter().map(|(_, s)| s).collect();
    assert!(names.contains(&"other".to_string()) && names.contains(&"proj".to_string()), "{b:#?}");
    // The shells are rows (by folder), never the window name.
    assert!(!b.iter().any(|l| l.contains("zsh")), "{b:#?}");
    assert_eq!(b.iter().filter(|l| l.contains(PROMPT)).count(), 3, "{b:#?}");
    assert!(b.iter().any(|l| row_title(l) == Some("five")) && b.iter().any(|l| row_title(l) == Some("two")), "{b:#?}");
    // A space with nothing but shells is a group: its header, its shell.
    let mut only_shell = proj();
    only_shell.push(a("plain", 1, "", "zsh"));
    let v = scoped(only_shell, "@9", "plain", "on", "all", "");
    let f = layout(&v, 34, 58, &Opts::default());
    let y = head(&f, "plain");
    assert!(texts(&f)[y + 1].contains(&format!("{PROMPT}/nonexistent/plain")), "{:#?}", texts(&f));
    // ...unless its session is hidden (`@space_hidden`): no space, no shells.
    let mut v = v;
    for sp in v.spaces.iter_mut().filter(|s| s.name == "plain") {
        sp.hidden = true;
    }
    assert!(!heads(&layout(&v, 34, 58, &Opts::default())).iter().any(|(_, s)| s == "plain"));
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
        assert!(b[0].contains("all spaces") && heads(&f).iter().any(|(_, s)| s == "other"), "{session}: {b:#?}");
    }
}

/// A space with nothing but its home (the client on it): its header (the
/// home, lifted: the client is on it) and `no agents in this space`.
#[test]
fn empty_space_still_has_its_header() {
    let mut ws = proj();
    ws.push(a("empty", 1, "", "zsh").with("home", "1"));
    let v = scoped(ws, "@9", "empty", "on", "", "");
    let f = layout(&v, 34, 30, &Opts::default());
    let t = texts(&f);
    assert_eq!(t[0].trim_end(), " space"); // no counts: nothing in it
    let b = agents_part(&f);
    assert!(heads(&f) == [(f.agents.start + 1, "empty".to_string())] && b[2].starts_with("   no agents in this space"), "{b:#?}");
    assert!(b[1].starts_with("▌") && b[1].contains(" empty ") && f.rows[f.agents.start + 1].bg == Some(Ink::Pal(MINE_BG)), "{b:#?}");
    assert!(t[f.needs.clone()][0].starts_with(" NEEDS YOU"));
    assert_eq!(f.target(4, (f.agents.start + 1) as u16), Some(&Target::Session("empty".into())));
    // A shell in it (the client on it): no `no agents` line, the shell is the row.
    let mut ws = proj();
    ws.push(a("empty", 1, "", "zsh"));
    let v = scoped(ws, "@9", "empty", "on", "", "");
    let b = agents_part(&layout(&v, 34, 30, &Opts::default()));
    assert!(b[2].contains(&format!("{PROMPT}/nonexistent/empty")) && !b.iter().any(|l| l.contains("no agents")), "{b:#?}");
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
            assert!(t[f.agents.end..f.dock.start].iter().all(|l| !l.trim().is_empty()), "{h}: {t:#?}");
        }
        check_accounting(&format!("fleet {h}"), &v, &f);
    }
    let f = layout(&v, 34, 11, &Opts::default());
    let t = texts(&f);
    let hd = head(&f, "main");
    assert!(t[hd + 1].contains("Handy.app"), "{t:#?}");
    assert_eq!((f.dock.len(), f.spaces.len()), (3, 1), "{t:#?}");
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
        assert!(t[r].starts_with(" ▎  f  Tmux"), "{w}: {:?}", t[r]);
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
    let glyphs = "⏵⤺≡⌥▌▎▄▀◉✓✕•◐○●▲─━┃│╭╮╰╯↻Σ−±↑↓✎⚙◎▸…✳⬢\u{f489}⏎⏸⌃⌂\u{e0a0}›".to_string() + &SPIN.concat();
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

/// A toast is a card over the top of the frame (OpenCode's): square `┃`
/// bars in its kind's colour down both sides of a `surface0` fill, no
/// corners; the glyph and title bold in the colour and the time it was
/// posted on its first row, the text under it; a blank canvas row under
/// the stack. Nothing beneath moves, a click anywhere on it dismisses it,
/// and it says when it expires.
#[test]
fn a_toast_is_a_card_over_the_top() {
    let v = lettered(quiet());
    let plain = layout(&v, 34, 40, &with_bar(demo_bar()));
    let ok = toast(Kind::Ok, "parked Canvas Syllabus");
    let f = layout(&v, 34, 40, &with_bar(Bar { toasts: vec![ok.clone()], ..demo_bar() }));
    let t = texts(&f);
    let time = posted_at(&ok);
    assert_eq!(f.toast, 0..3, "{t:#?}");
    assert_eq!(t[0], format!(" ┃ ✓ Done{}{time} ┃ ", " ".repeat(17)));
    assert_eq!(t[1], format!(" ┃ parked Canvas Syllabus{} ┃ ", " ".repeat(6)));
    assert!(t[2].trim().is_empty() && f.rows[2].bg.is_none() && f.rows[2].targets.is_empty(), "{t:#?}");
    // Nothing beneath moves; the rows under it still stand for theirs.
    assert_eq!((&f.rows[3..], f.card.clone(), f.status.clone(), f.needs.clone()), (&plain.rows[3..], plain.card.clone(), plain.status.clone(), plain.needs.clone()));
    assert!(f.rows[..3].iter().zip(&plain.rows[..3]).all(|(a, b)| a.covers == b.covers));
    // The look: the fill (`surface0`), the bars and the title in green, the
    // title bold, the time dim, the text plain; a canvas margin either side.
    for y in 0..2 {
        let r = &f.rows[y];
        assert_eq!((r.bg, r.segs[0].clone(), r.segs.last().cloned()), (Some(Ink::Pal("surface0")), bare(1), Some(bare(1))), "{y}");
        assert_eq!((r.segs[1].clone(), r.segs[r.segs.len() - 2].clone()), (seg("┃", "green"), seg("┃", "green")), "{y}");
    }
    assert!(f.rows[0].segs.contains(&bold("✓ Done", "green")) && f.rows[0].segs.contains(&seg(time.clone(), "overlay2")));
    assert!(f.rows[1].segs.contains(&seg("parked Canvas Syllabus", "text")));
    let mut buf = Buffer::empty(Rect::new(0, 0, 34, 40));
    paint(&f, &mut buf);
    assert_eq!((buf[(0, 1)].bg, buf[(1, 1)].bg, buf[(1, 1)].fg), (palette::color("mantle"), palette::color("surface0"), palette::color("green")));
    // A click anywhere on it dismisses it; the blank row under it does not.
    for y in 0..2u16 {
        assert_eq!((f.target(0, y), f.target(20, y), f.target(33, y)), (Some(&Target::Toast(TOAST_ID)), Some(&Target::Toast(TOAST_ID)), Some(&Target::Toast(TOAST_ID))));
    }
    assert_eq!(f.target(20, 2), None);
    assert_eq!(f.toast_until, Some((NOW + 4) * 1000));
    assert_eq!(plain.toast_until, None);
    // Each kind: its glyph, title and colour.
    for (k, glyph, title, hue) in [(Kind::Info, "●", "Info", "blue"), (Kind::Ok, "✓", "Done", "green"),
        (Kind::Warn, "▲", "Warning", "peach"), (Kind::Error, "✕", "Error", "red")] {
        let f = layout(&v, 34, 40, &with_bar(Bar { toasts: vec![toast(k, "hi")], ..demo_bar() }));
        assert!(texts(&f)[0].starts_with(&format!(" ┃ {glyph} {title} ")), "{k:?}: {:?}", texts(&f)[0]);
        assert!(f.rows[0].segs.contains(&bold(format!("{glyph} {title}"), hue)) && f.rows[1].segs[1] == seg("┃", hue), "{k:?}");
    }
    // notify.sh -T: the title instead of the kind's.
    let titled = Toast { title: Some("Stash".into()), ..ok.clone() };
    let f = layout(&v, 34, 40, &with_bar(Bar { toasts: vec![titled], ..demo_bar() }));
    assert!(texts(&f)[0].starts_with(" ┃ ✓ Stash "), "{:?}", texts(&f)[0]);
    // Narrow: the time goes before the title is cut; the bars stay.
    let f = layout(&v, 17, 40, &with_bar(Bar { toasts: vec![ok], ..demo_bar() }));
    let top = texts(&f)[0].clone();
    assert!(top.starts_with(" ┃ ✓ Done ") && top.ends_with(" ┃ ") && !top.contains(':'), "{top:?}");
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
    assert_eq!((f.target(5, 1).cloned(), f.target(5, 3).cloned(), f.target(5, 7).cloned()), (id(2), id(3), id(4)));
    // A blank canvas row between two cards and under the stack: no target.
    for y in [2u16, 5, 8] {
        assert!(t[usize::from(y)].trim().is_empty() && f.target(5, y).is_none(), "{y}: {t:#?}");
    }
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
        assert!(h >= 3 || f.toast.len() == 1, "{h}: a compact row {t:#?}");
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
    assert!(t[1].starts_with(" ┃ couldn't park Canvas ") && t[2].starts_with(" ┃ Syllabus: it is still ") && t[3].starts_with(" ┃ working on the export "), "{t:#?}");
    assert!((0..4).all(|y| t[y].ends_with(" ┃ ") && f.target(1, y as u16) == Some(&Target::Toast(TOAST_ID))), "{t:#?}");
    let huge = format!("{LONG} {LONG}");
    let f = layout(&v, 34, 40, &with_bar(Bar { toasts: vec![toast(Kind::Error, &huge)], ..demo_bar() }));
    let t = texts(&f);
    assert_eq!(f.toast, 0..5, "{t:#?}");
    assert!(t[3].trim_end_matches([' ', '┃']).ends_with('…'), "{t:#?}");
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
/// compact row (the card's bars and fill, the glyph and text; a click
/// dismisses it), down to 2 rows: notify.sh chose a toast, so it must be
/// seen.
#[test]
fn a_small_sidebar_still_shows_a_toast() {
    let v = lettered(quiet());
    let stack = vec![toast_n(1, Kind::Ok, "older"), toast_n(2, Kind::Warn, "newest")];
    for (w, h) in [(15u16, 40u16), (12, 10), (6, 8), (34, 2), (20, 2)] {
        let f = layout(&v, w, h, &with_bar(Bar { toasts: stack.clone(), ..demo_bar() }));
        let t = texts(&f);
        assert_eq!(f.toast.len(), 1, "{w}x{h}: {t:#?}");
        let y = f.toast.start;
        assert!(t[y].contains('▲') && t[y].contains('┃') && !t.iter().any(|l| l.contains("older")) && y + 1 < usize::from(h), "{w}x{h}: {t:#?}");
        assert!(w < 14 || t[y].contains("newest"), "{w}x{h}: {t:#?}");
        assert_eq!((f.rows[y].bg, f.target(0, y as u16)), (Some(Ink::Pal("surface0")), Some(&Target::Toast((2, NOW * 1000 + 2)))));
        assert_eq!(f.toast_until, Some(until_ms(&stack[1])));
    }
    // Three rows already hold a card (two rows), the frame's last row kept.
    let f = layout(&v, 34, 3, &with_bar(Bar { toasts: stack.clone(), ..demo_bar() }));
    assert!(f.toast == (0..2) && texts(&f)[0].contains("▲ Warning") && texts(&f)[1].contains("newest"), "{:#?}", texts(&f));
    // One row: nothing (the last row is the parked one).
    assert!(layout(&v, 34, 1, &with_bar(Bar { toasts: stack, ..demo_bar() })).toast.is_empty());
}

/// The sidebar paints its own canvas (mantle) under everything: no cell is
/// ever the terminal's default background. On it: the tiles (inside their
/// canvas margins), the NEEDS YOU card, the client's band, the dock.
#[test]
fn renders_on_its_own_canvas() {
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;
    let f = layout(&quiet(), 34, 58, &Opts::default());
    let mut term = Terminal::new(TestBackend::new(34, 58)).unwrap();
    term.draw(|fr| paint(&f, fr.buffer_mut())).unwrap();
    let buf = term.backend().buffer();
    let canvas = palette::color("mantle");
    let here = find(&f, "Tmux Agent Sidebar") as u16;
    let card = find(&f, "▌ Kua Yu") as u16;
    let main = head(&f, "main") as u16;
    for y in 0..58u16 {
        let row = &f.rows[y as usize];
        for x in 0..34u16 {
            let bg = buf[(x, y)].bg;
            assert_ne!(bg, Color::Reset, "({x},{y}) {:?}", row.text());
            // The margins are the canvas on every row.
            if x == 0 || x == 33 {
                assert_eq!(bg, canvas, "({x},{y}) {:?}", row.text());
                continue;
            }
            let seg_bg = {
                let mut at = 0;
                row.segs.iter().find(|s| {
                    at += dwidth(&s.text);
                    usize::from(x) < at
                }).and_then(|s| s.bg)
            };
            if y == here || y == here + 1 {
                assert_eq!(bg, seg_bg.map_or(palette::color(MINE_BG), Ink::color), "({x},{y})");
            } else if y == card || y == card + 1 {
                assert_eq!(bg, seg_bg.map_or(palette::mix("yellow", 14), Ink::color), "({x},{y})");
            } else if y == main {
                assert!(bg == band(Ink::Pal("peach")).color() || bg == palette::color("peach"), "({x},{y}) {bg:?}");
            } else if f.spaces.contains(&usize::from(y)) || f.parked.contains(&usize::from(y)) {
                assert!(bg != canvas, "({x},{y}) the dock");
            } else if row.bg.is_none() && seg_bg.is_none() {
                assert_eq!(bg, canvas, "({x},{y}) {:?}", row.text());
            }
        }
    }
    // No pad: the next card (the zebra's `tile`) right under the client's agent.
    let next = here + 2;
    assert_eq!((buf[(5, next)].bg, buf[(0, next)].bg), (palette::color("tile"), canvas));
    let ansi = to_ansi(buf);
    assert_eq!(ansi.lines().count(), 58);
    // Every line paints its background: the title on the canvas.
    assert!(ansi.starts_with("\x1b[0;38;2;205;214;244;48;2;24;24;37;1m agents"), "{:?}", &ansi[..60]);
    assert!(ansi.lines().all(|l| l.contains(";48;2;")));
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
    let r = find(&f, "Tmux Agent S");
    assert!(t[r + 1].starts_with(" ▎     › Running @2") && t[r + 1].ends_with(" 2m █ "), "{:?}", t[r + 1]);
    // (On the client's own card, its dim is `sub`.)
    assert!(f.rows[r + 1].segs.contains(&seg("› ", "blue")) && f.rows[r + 1].segs.contains(&seg("Running @2", "sub")));
    // Not working (idle, asking): their own second lines.
    let n = find(&f, "Lost suit jacket");
    let k = find(&f, "▎  g  Kua Yu");
    assert!(!t[n + 1].contains("Running") && t[k + 1].contains("Which deck"), "{t:#?}");
    let (firsts, seconds) = agent_lines(&f);
    assert_eq!(firsts, seconds);
    // No blurb: what it runs, as before.
    let f = layout(&lettered(quiet()), 34, 58, &Opts::default());
    let r = find(&f, "Tmux Agent S");
    assert!(texts(&f)[r + 1].starts_with(" ▎     Edit agent-roster.py"), "{:#?}", texts(&f));
}

/// The bottom gives way in order as rows run short: SPACES collapses to the
/// client's space and the attention ones (`+K more spaces`), then to chips;
/// PARKED is always its one line (label and count), never its tabs, at
/// every height.
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
        // One line with the count; no tab, no `+N more`, anywhere.
        assert!(t[f.parked.start].starts_with(&parked(5)), "{h}: {t:#?}");
        assert!(!t.iter().any(|l| l.contains("Parked ") || l.contains('⏸') || l.contains("+2 more")), "{h}: {t:#?}");
        assert_eq!(f.target(5, f.parked.start as u16), Some(&Target::Stash));
        let spaces = &t[f.spaces.clone()];
        let whole = |y: usize| matches!(f.rows[y].targets.as_slice(), [(0, 34, _)]);
        let form = if spaces.is_empty() {
            3
        } else if spaces.iter().any(|l| l.contains("more spaces")) {
            1
        } else if f.spaces.clone().all(whole) {
            0
        } else {
            2
        };
        if form == 1 {
            // The client's space (main, which needs you too) and `+2`.
            let more = f.spaces.clone().find(|&y| t[y].contains("+2  more spaces")).unwrap_or_else(|| panic!("{h}: {t:#?}"));
            assert!(spaces.iter().any(|l| l.contains(" main ")) && !spaces.iter().any(|l| l.contains(" bai ")), "{h}: {t:#?}");
            assert_eq!(f.target(5, more as u16), Some(&Target::Menu(Some("all"))));
        }
        forms.push(form);
    }
    for want in [0, 1, 2] {
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

/// The client's space keeps its chip and its counts when its name is long:
/// the name gives way, clipped with `…`, in the dock and in its header.
#[test]
fn a_long_space_name_gives_way() {
    let v = view(vec![a("a-very-long-space-name-indeed", 1, "running", "T").with("space_index", "1")], Some("@1"));
    for w in [24u16, 30, 38] {
        let f = layout(&v, w, 40, &Opts::default());
        for y in [f.spaces.start, head(&f, "a-very-long-space-name-indeed")] {
            let t = f.rows[y].text();
            assert!(t.contains('…') && t.contains(" 1  a-very") && t.trim_end().ends_with("◐1"), "{w}: {t:?}");
            assert_eq!(width(&f.rows[y].segs), usize::from(w));
        }
    }
}

/// A card with nothing to say on its second line (an idle agent with no
/// reply, a working one with no activity) shows a faint placeholder there,
/// the age where it always is: never a row that reads as broken. Cards stay
/// exactly two rows.
#[test]
fn an_empty_second_line_has_a_placeholder() {
    let v = lettered(view(vec![a("main", 1, "idle", "Sidebar Cmd+T R"), a("main", 2, "running", "Fresh"), a("main", 3, "idle", "Said")
        .with("detail_kind", "done").with("detail", "All done")], None));
    let f = layout(&v, 42, 40, &Opts::default());
    let t = texts(&f);
    for (title, want) in [("Sidebar Cmd+T R", "no reply yet"), ("Fresh", "no activity yet")] {
        let y = find(&f, title);
        assert!(f.rows[y + 1].segs.contains(&seg(want, PLACEHOLDER)) && f.rows[y + 1].covers.is_empty(), "{title}: {t:#?}");
        assert!(t[y + 1].trim_end().ends_with('m') && f.rows[y + 2].covers.len() == 1 || y + 2 >= f.agents.end, "{title}: {t:#?}");
    }
    let y = find(&f, "Said");
    assert!(t[y + 1].contains("All done") && !t[y + 1].contains(" yet"), "{t:#?}");
    let (firsts, seconds) = agent_lines(&f);
    assert_eq!(firsts, seconds);
}

fn phases() -> Vec<Phase> {
    let l = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();
    vec![
        Phase::Sending { done: 2, n: 5, labels: l(&["Tmux Agent Sidebar", "Island resize spring"]), held: vec![] },
        Phase::Stopping { stopped: 3, n: 5, working: l(&["Tmux Agent Sidebar", "Census AX sweep"]), held: vec![] },
        Phase::Safe { labels: l(&["Tmux Agent Sidebar", "Island resize spring"]) },
        Phase::Ready { labels: l(&["Tmux Agent Sidebar", "Island resize spring", "Census AX sweep"]) },
        Phase::Resuming { n: 3, labels: l(&["Tmux Agent Sidebar"]) },
    ]
}

/// The pause banner sits at the very top, ONE tint in its phase's colour,
/// a heavy rail, its word bold in the colour; two rows when the frame has
/// the room, the second the dim detail (who is still working); exactly the
/// frame's width. Only a settled pause (safe, ready) clicks through to
/// [`Target::Resume`]; only a moving one spins.
#[test]
fn the_pause_banner_says_where_the_pause_stands() {
    let v = lettered(quiet());
    let words = ["PAUSING  sending 2 of 5", "PAUSING  3 of 5 stopped", "⏸ PAUSED  safe to close", "↻ 3 ready  ", "RESUMING  3 agents…"];
    for (p, word) in phases().into_iter().zip(words) {
        for w in [24u16, 34, 44] {
            let f = layout(&v, w, 58, &Opts { pause: Some(p.clone()), ..with_bar(demo_bar()) });
            let t = texts(&f);
            let ctx = format!("{p:?} {w}: {t:#?}");
            assert_eq!(f.pause, 0..2, "{ctx}");
            let hue = pause_hue(&p);
            for y in f.pause.clone() {
                let r = &f.rows[y];
                assert_eq!((dwidth(&r.text()), r.bg), (usize::from(w), Some(pause_bg(&p))), "{ctx}");
                assert_eq!(r.segs[1], seg("▌", hue), "{ctx}");
                assert_eq!(r.anim, p.busy(), "{ctx}");
                let click = (0..w).map(|x| f.target(x, y as u16)).collect::<Vec<_>>();
                if p.clickable() {
                    // Everywhere but the first row's void chip, at its
                    // right end (a pad and the margin after it).
                    let void = click.iter().filter(|c| **c == Some(&Target::Void)).count();
                    assert!(click.iter().all(|c| matches!(c, Some(Target::Resume | Target::Void))), "{ctx}");
                    assert_eq!(void > 0, y == 0, "{ctx}");
                    let w = usize::from(w);
                    assert!(click[w - 2 - void..w - 2].iter().all(|c| *c == Some(&Target::Void)), "{ctx}");
                } else {
                    assert!(click.iter().all(Option::is_none), "{ctx}");
                }
            }
            // The word bold in the colour; ready's count on a solid chip of it.
            let chip = |s: &Seg| s.bold && s.bg == Some(Ink::Pal(hue)) && s.fg == Ink::Pal("crust");
            if matches!(p, Phase::Ready { .. }) {
                assert!(f.rows[0].segs.iter().any(chip), "{ctx}");
            } else {
                assert!(f.rows[0].segs.iter().any(|s| s.bold && s.fg == Ink::Pal(hue)), "{ctx}");
                assert!(!f.rows[0].segs.iter().any(chip), "{ctx}");
            }
            if w == 44 {
                assert!(t[0].contains(word), "{ctx}");
            }
            // The panel follows, then NEEDS YOU, as without a banner.
            assert!(f.card.start == 2 && f.needs.start > f.card.end, "{ctx}");
            assert!(f.spin || !p.busy(), "{ctx}");
        }
    }
    // Ready always says how to resume, at Mack's usual 34 columns too: who
    // on the first row, "prefix P or click to resume" on the second; one
    // row, the hint takes the place of who (` · click` going for the void
    // chip at 34).
    for w in [34u16, 44] {
        let f = layout(&v, w, 58, &Opts { pause: Some(phases()[3].clone()), ..Opts::default() });
        let t = texts(&f);
        assert!(t[0].contains("↻ 3 ready  Tmux Agent S"), "{t:#?}");
        assert!(t[1].contains("prefix P or click to resume"), "{t:#?}");
    }
    let f = layout(&v, 34, 10, &Opts { pause: Some(phases()[3].clone()), ..Opts::default() });
    let t = texts(&f);
    assert_eq!(f.pause, 0..1, "{t:#?}");
    assert!(t[0].contains("↻ 3 ready  prefix P ") && t[0].contains("✕"), "{t:#?}");
    let t = texts(&layout(&v, 44, 10, &Opts { pause: Some(phases()[3].clone()), ..Opts::default() }));
    assert!(t[0].contains("↻ 3 ready  prefix P · click") && t[0].contains("✕"), "{t:#?}");
    let f = layout(&v, 44, 58, &Opts { pause: Some(phases()[1].clone()), ..Opts::default() });
    assert!(texts(&f)[1].contains("still working  Tmux Agent Sidebar, C…"), "{:#?}", texts(&f));
    // No pause: nothing at all, the frame exactly as before.
    let none = layout(&v, 44, 58, &Opts::default());
    assert!(none.pause.is_empty());
    assert_eq!(none, layout(&v, 44, 58, &Opts { pause: None, ..Opts::default() }));
}

/// The banner survives the height ladder: two rows from 16, one from 4;
/// the sections under it take the squeeze (agents fold and compress, every
/// agent still accounted for); toasts float under it, never over it.
#[test]
fn the_pause_banner_outlasts_the_agents() {
    let stopping = phases()[1].clone();
    for (name, v) in all() {
        let v = lettered(v);
        for w in [30u16, 44] {
            for h in 2u16..=80 {
                let bar = Bar { toasts: vec![toast(Kind::Ok, "paused 5 agents")], ..demo_bar() };
                let f = layout(&v, w, h, &Opts { pause: Some(stopping.clone()), ..with_bar(bar) });
                let t = texts(&f);
                let ctx = format!("{name} {w}x{h}: {t:#?}");
                assert_eq!(f.rows.len(), usize::from(h), "{ctx}");
                assert!(f.rows.iter().all(|r| dwidth(&r.text()) == usize::from(w)), "{ctx}");
                let want = pause_lines(usize::from(h));
                // (A frame with no room for sections keeps its first row.)
                assert!(f.pause.len() == want || (f.pause.len() == 1 && f.step as usize == STEPS.len()), "{ctx}");
                if want > 0 {
                    assert!(t[0].contains("PAUSING"), "{ctx}");
                    assert!(f.toast.is_empty() || f.toast.start >= f.pause.end, "{ctx}");
                }
                check_accounting(name, &v, &f);
            }
        }
    }
    // Where the agents have had to give up detail, the banner kept both rows.
    let v = lettered(busy());
    let f = layout(&v, 44, 24, &Opts { pause: Some(stopping), ..with_bar(demo_bar()) });
    assert!(f.step as usize == STEPS.len() || f.agents_step > 0, "{:#?}", texts(&f));
    assert_eq!(f.pause, 0..2);
}

/// Toasts never cover PARKED, even when the dock's bottom cap is the last
/// row: a light fleet, a short frame, three long toasts, the watcher in
/// trouble; its warning stays readable (and clickable).
#[test]
fn toasts_never_cover_parked() {
    let mut v = lettered(view(vec![a("main", 1, "running", "One"), a("main", 2, "idle", "Two")], Some("@1")));
    v.watcher_age = Some(99.0);
    let stack = vec![toast_n(1, Kind::Ok, LONG), toast_n(2, Kind::Warn, LONG), toast_n(3, Kind::Error, LONG)];
    for h in 4u16..=30 {
        let f = layout(&v, 42, h, &with_bar(Bar { toasts: stack.clone(), ..demo_bar() }));
        let t = texts(&f);
        assert!(f.toast.end <= f.parked.start && t[f.parked.start].contains("watcher stalled"), "{h}: {t:#?}");
        let w = (t[f.parked.start].find("watcher").unwrap()) as u16;
        assert_eq!(f.target(w, f.parked.start as u16), Some(&Target::Restart), "{h}");
    }
}


/// The client's own card is "you are here" at a glance, and only ONE card
/// is: a window linked into two spaces is current in both (window ids), so
/// the copy in the client's own space carries the lift and the edge, the
/// other a plain card.
/// Its surface stays apart from every other on screen (the zebra, the
/// stash selection, the NEEDS YOU tints, the current space's band).
#[test]
fn only_the_clients_own_card_is_lifted() {
    let mine = |r: &Row| r.bg == Some(Ink::Pal(MINE_BG));
    let mut v = lettered(view(vec![a("aaa", 1, "running", "Linked"), a("main", 1, "running", "Linked"), a("main", 2, "idle", "Other")], Some("@2")));
    v.cur_session = Some("main".into());
    for x in v.spaces.iter_mut().flat_map(|s| s.agents.iter_mut()).filter(|x| x.title == "Linked") {
        x.window_id = "@2".into();
        x.is_current = true;
    }
    let f = layout(&v, 42, 40, &with_bar(demo_bar()));
    let t = texts(&f);
    let lifted: Vec<&(String, String)> = f.rows.iter().filter(|r| mine(r) && r.covers.len() == 1).map(|r| &r.covers[0]).collect();
    assert_eq!(lifted, [&("main".to_string(), "@2".to_string())], "{t:#?}");
    let edged = f.rows.iter().filter(|r| r.segs.iter().any(|s| s.text == "█")).count();
    assert_eq!(edged, 2, "{t:#?}");
    // Its surface, against everything else a frame paints, by a visible
    // margin.
    let dist = |a: (u8, u8, u8), b: (u8, u8, u8)| ((f64::from(a.0) - f64::from(b.0)).powi(2) + (f64::from(a.1) - f64::from(b.1)).powi(2) + (f64::from(a.2) - f64::from(b.2)).powi(2)).sqrt();
    let me = palette::rgb(MINE_BG);
    for (name, v) in all().into_iter().chain([("coloured", coloured())]) {
        let mut sel = demo_bar();
        sel.selecting = Some(1);
        let f = layout(&v, 42, 70, &with_bar(sel));
        for r in f.rows.iter().filter(|r| !mine(r)) {
            for ink in r.bg.iter().chain(r.segs.iter().filter_map(|s| s.bg.as_ref())) {
                let d = dist(ink_rgb(*ink), me);
                let band = is_group_header(r, 42) || f.spaces.contains(&f.rows.iter().position(|x| std::ptr::eq(x, r)).unwrap());
                assert!(d >= if band { 12.0 } else { 20.0 } || ink_rgb(*ink) != ink_rgb(r.bg.unwrap_or(Ink::Canvas)), "{name}: {ink:?} {d:.1} in {:?}", r.text());
            }
        }
    }
}

/// Agents paused by the pause key (docs/agent-pause.md "The paused state"):
/// `paused` as written, and done / idle with `@agent_paused` set (before the
/// tab hook writes `paused`). Plus one still running with the flag (it has
/// not stopped yet) and one asking with it (only Mack can answer).
fn paused_view() -> ViewModel {
    let flag = (NOW - 300).to_string();
    view(
        vec![
            a("main", 1, "running", "Tmux Agent Sidebar"),
            a("main", 2, "paused", "Island resize spring").with("detail_kind", "done").with("detail", "Paused. Waiting for RESUME."),
            a("main", 3, "done", "Census AX sweep").with("paused", &flag).with("detail_kind", "done").with("detail", "ok, pausing"),
            a("main", 4, "idle", "Kua Yu deck").with("paused", &flag).with("detail_kind", "done").with("detail", "7 fixes applied"),
            a("main", 5, "running", "Handy.app Speech").with("paused", &flag),
            a("main", 6, "needs-input", "Asks anyway").with("paused", &flag).with("detail_kind", "ask").with("detail", "which?"),
            a("main", 7, "idle", "Q7"),
            a("main", 8, "idle", "Q8"),
        ],
        Some("@1"),
    )
}

/// A paused card: a violet `▎` rail down both rows, the ⏸ bold violet
/// on the right (its word below: `paused`, never twice), the second line
/// `paused · prefix P resumes` while a pause is saved and it fits, else
/// `paused`; never green, never a NEEDS YOU card, never folded into the idle
/// line; the space's counts carry `⏸3` in violet.
#[test]
fn paused_cards_are_violet_and_never_need_you() {
    let v = lettered(paused_view());
    let ids = |want: &str| v.agents().filter(|a| a.state == want).map(|a| a.title.clone()).collect::<Vec<_>>();
    assert_eq!(ids("paused"), ["Island resize spring", "Census AX sweep", "Kua Yu deck"]);
    assert_eq!(v.needs.iter().map(|n| n.agent.title.as_str()).collect::<Vec<_>>(), ["Asks anyway"]);
    for (w, settled) in [(34u16, true), (44, true), (44, false)] {
        let pause = settled.then(|| Phase::Safe { labels: vec!["Island resize spring".into()] });
        let f = layout(&v, w, 70, &Opts { pause, ..Opts::default() });
        let t = texts(&f);
        let ctx = format!("{w} {settled}: {t:#?}");
        for title in ["Island resize spring", "Census AX sweep", "Kua Yu deck"] {
            let y = f.agents.clone().find(|&y| t[y].contains(title)).unwrap_or_else(|| panic!("{title}: {ctx}"));
            assert!(!f.needs.contains(&y), "{ctx}");
            let (r, r2) = (&f.rows[y], &f.rows[y + 1]);
            assert_eq!((r.segs[1].clone(), r2.segs[1].clone()), (seg("▎", "violet"), seg("▎", "violet")), "{title} {ctx}");
            assert!(r.segs.contains(&bold("⏸", "violet")) && !t[y].contains("⏸ paused"), "{title} {ctx}");
            assert!(r2.segs.contains(&bold("paused", "violet")), "{title} {ctx}");
            // The key that resumes, while a pause is saved: whole from 44
            // columns, `prefix P` at Mack's usual 34.
            let want = if w >= 44 { "paused · prefix P resumes" } else { "paused · prefix P" };
            assert_eq!(t[y + 1].contains(want), settled, "{title} {ctx}");
            assert_eq!(t[y + 1].contains("prefix P"), settled, "{title} {ctx}");
            assert!(!t[y + 1].contains("Paused. Waiting") && !t[y + 1].contains("7 fixes"), "{title}: the reply gives way {ctx}");
            for s in r.segs.iter().chain(&r2.segs) {
                assert!(s.fg != Ink::Pal("green"), "{title}: never green {ctx}");
            }
        }
        // Still running with the flag: working, as before.
        let h = find(&f, "Handy.app Spee");
        assert!(t[h].contains("working") && f.rows[h].segs[1] == seg("▎", "blue"), "{ctx}");
        // The space's header counts the paused bucket, after working.
        let m = head(&f, "main");
        assert!(f.rows[m].segs.contains(&seg("⏸3", "violet")), "{:?}", f.rows[m].segs);
        assert!(t[m].contains("◉1 ◐2 ⏸3 ○2"), "{ctx}");
    }
    // Squeezed: the idle agents fold, the paused ones never do.
    let f = (10..70).rev().map(|h| layout(&v, 34, h, &Opts::default())).find(|f| f.agents_step == 1).unwrap();
    let t = texts(&f);
    assert!(t.iter().any(|l| l.contains("2 idle · Q7, Q8")), "{t:#?}");
    for title in ["Island resize spring", "Census AX sweep", "Kua Yu deck"] {
        assert!(t[f.agents.clone()].iter().any(|l| l.contains(title)), "{title}: {t:#?}");
    }
    // Narrow (no second lines under 26 columns): the glyph and the word on
    // the one line.
    let f = layout(&v, 24, 70, &Opts::default());
    let y = find(&f, "Islan");
    assert!(f.rows[y].segs.contains(&bold("⏸ paused", "violet")) && f.rows[y].segs[1] == seg("▎", "violet"), "{:?}", f.rows[y].segs);
}

/// The banner matches CuaNotch: the moving phases (pausing, resuming: the
/// spinner) the quieter `dimviolet` on violet at 9%, as the notch pulses
/// violet; the settled ones ("all stopped") cyan on cyan at 14%, as the
/// notch glows, safe and ready told apart by emphasis (safe's `⏸ PAUSED`
/// bold in cyan, ready's count on a solid cyan chip). Never green (green
/// means done), never the old blue, never peach.
#[test]
fn pause_banner_is_violet_then_cyan_never_green() {
    let v = lettered(quiet());
    for p in phases() {
        let (hue, bg) = (pause_hue(&p), pause_bg(&p));
        match p {
            Phase::Safe { .. } | Phase::Ready { .. } => {
                assert_eq!((hue, bg), ("cyan", Ink::Mix("cyan", PAUSE_TINT)), "{p:?}");
            }
            _ => assert_eq!((hue, bg), ("dimviolet", Ink::Mix("violet", PAUSE_TINT_MOVING)), "{p:?}"),
        }
        for w in [34u16, 44] {
            let f = layout(&v, w, 58, &Opts { pause: Some(p.clone()), ..Opts::default() });
            for y in f.pause.clone() {
                assert_eq!(f.rows[y].bg, Some(bg));
                let banned = ["green", "blue", "peach"].map(Ink::Pal);
                assert!(f.rows[y].segs.iter().all(|s| !banned.contains(&s.fg)), "{p:?}");
            }
        }
    }
    let f = layout(&v, 34, 58, &Opts { pause: Some(phases()[2].clone()), ..Opts::default() });
    assert!(f.rows[0].segs.contains(&bold("⏸ PAUSED", "cyan")), "{:?}", f.rows[0].segs);
    let f = layout(&v, 34, 58, &Opts { pause: Some(phases()[3].clone()), ..Opts::default() });
    assert!(f.rows[0].segs.contains(&block(" ↻ 3 ready ", "crust", "cyan")), "{:?}", f.rows[0].segs);
    let f = layout(&v, 34, 58, &Opts { pause: Some(phases()[0].clone()), ..Opts::default() });
    assert!(f.rows[0].segs.iter().any(|s| s.text.contains("PAUSING") && s.fg == Ink::Pal("dimviolet")), "{:?}", f.rows[0].segs);
}

/// The settled banner's void chip (docs/agent-pause.md "Dismiss and void"),
/// safe and ready only, square at the first row's right end (a pad and the
/// margin after it): quiet, ` ✕ void ` dim on the cyan a step up (` ✕ ` at
/// Mack's usual 34 columns); armed by a first click, ` ✕ void? click
/// again ` bold dark ink on solid red (` ✕ void? ` at 34), and safe's
/// `safe to close` makes way for it. Its cells are [`Target::Void`]
/// ([`Target::VoidArmed`] armed); the rest of the banner still resumes.
/// A dismissed glow keeps the banner and says `glow off`, dim, at the
/// second row's right end (ready's hint shorter for it at 34).
#[test]
fn the_settled_banner_has_a_void_chip() {
    let v = lettered(quiet());
    let ph = phases();
    let (safe, ready) = (&ph[2], &ph[3]);
    let lay = |p: &Phase, w: u16, armed: bool, glow_off: bool| layout(&v, w, 58, &Opts { pause: Some(p.clone()), void_armed: armed, glow_off, ..Opts::default() });
    let chip_of = |f: &Frame| f.rows[0].segs.iter().find(|s| s.text.contains('✕') || s.text.contains("void?")).cloned();
    for (w, quiet, asks) in [(44u16, " ✕ void ", " ✕ void? click again "), (34, " ✕ ", " ✕ void? ")] {
        for p in [safe, ready] {
            let ctx = |f: &Frame| format!("{p:?} {w}: {:#?}", texts(f));
            let f = lay(p, w, false, false);
            let c = chip_of(&f).unwrap_or_else(|| panic!("{}", ctx(&f)));
            assert_eq!(c.text, quiet, "{}", ctx(&f));
            assert!(!c.bold && c.fg == Ink::Pal("overlay2") && c.bg.is_some_and(|b| b != pause_bg(p)), "{c:?}");
            assert!(texts(&f)[0].ends_with(&format!("{quiet}  ")), "{}", ctx(&f));
            let x1 = w - 2;
            let x0 = x1 - dwidth(quiet) as u16;
            assert!((x0..x1).all(|x| f.target(x, 0) == Some(&Target::Void)), "{}", ctx(&f));
            assert_eq!((f.target(x0 - 1, 0), f.target(x1, 0)), (Some(&Target::Resume), Some(&Target::Resume)), "{}", ctx(&f));
            assert!((0..w).all(|x| f.target(x, 1) == Some(&Target::Resume)), "{}", ctx(&f));
            // Armed: the question, in red, where the quiet chip was.
            let f = lay(p, w, true, false);
            assert_eq!(chip_of(&f), Some(block(asks, ink_on(Ink::Pal(VOID_HUE)), VOID_HUE)), "{}", ctx(&f));
            assert!(texts(&f)[0].ends_with(&format!("{asks}  ")), "{}", ctx(&f));
            let x0 = x1 - dwidth(asks) as u16;
            assert!((x0..x1).all(|x| f.target(x, 0) == Some(&Target::VoidArmed)), "{}", ctx(&f));
            assert!(!(0..w).any(|x| f.target(x, 0) == Some(&Target::Void)), "{}", ctx(&f));
            assert_eq!(f.target(2, 0), Some(&Target::Resume), "{}", ctx(&f));
            if matches!(p, Phase::Safe { .. }) {
                assert!(texts(&f)[0].contains("⏸ PAUSED") && !texts(&f)[0].contains("safe to close"), "{}", ctx(&f));
            }
        }
    }
    // A moving banner has none, armed or not, and never `glow off`.
    for p in [&ph[0], &ph[1], &ph[4]] {
        for armed in [false, true] {
            let f = lay(p, 44, armed, true);
            assert!(chip_of(&f).is_none() && !texts(&f).iter().any(|l| l.contains(GLOW_OFF)), "{p:?}: {:#?}", texts(&f));
            assert!(f.rows.iter().all(|r| r.targets.iter().all(|(_, _, t)| !matches!(t, Target::Void | Target::VoidArmed))));
        }
    }
    for w in [34u16, 44] {
        // Dismissed: the banner as ever, `glow off` on its second row.
        let f = lay(safe, w, false, true);
        let t = texts(&f);
        assert_eq!(f.pause, 0..2, "{t:#?}");
        assert!(t[0].contains("⏸ PAUSED") && t[1].contains("Tmux"), "{t:#?}");
        assert!(t[1].ends_with(&format!("{GLOW_OFF}  ")) && f.rows[1].segs.contains(&seg(GLOW_OFF, "overlay2")), "{t:#?}");
        let t = texts(&lay(ready, w, false, true));
        let hint = if w == 44 { "prefix P or click to resume" } else { "prefix P · click" };
        assert!(t[1].contains(hint) && t[1].ends_with(&format!("{GLOW_OFF}  ")), "{t:#?}");
        assert!(!texts(&lay(safe, w, false, false)).iter().any(|l| l.contains(GLOW_OFF)));
    }
    // Every banner row exactly the frame's width, at any width.
    for w in 4u16..=60 {
        for (p, armed, glow) in [safe, ready].into_iter().flat_map(|p| [(p, false, false), (p, true, false), (p, false, true), (p, true, true)]) {
            let f = lay(p, w, armed, glow);
            assert!(f.rows.iter().all(|r| dwidth(&r.text()) == usize::from(w)), "{p:?} {w} {armed} {glow}: {:#?}", texts(&f));
            let chip = if armed { Target::VoidArmed } else { Target::Void };
            assert!(w < 8 || (0..w).any(|x| f.target(x, 0) == Some(&chip)), "{p:?} {w}: {:#?}", texts(&f));
        }
    }
}

/// tmux-mcp's reason for holding a PAUSE (server.ts `readBox`): copy mode.
const COPY: &str = "the pane is in copy mode";

/// A held agent in the snapshot's `main`: `label`, window `win`, letter
/// `c`, held for copy mode.
fn held(label: &str, win: &str, c: Option<char>) -> Held {
    Held { label: label.into(), window_id: win.into(), why: COPY.into(), session: "main".into(), letter: c }
}

/// Stopping, `working` still working and `held` held by tmux-mcp.
fn holding(working: &[&str], held: Vec<Held>) -> Phase {
    let n = working.len() + held.len() + 1;
    Phase::Stopping { stopped: 1, n, working: working.iter().map(|s| s.to_string()).collect(), held }
}

/// An agent whose PAUSE tmux-mcp holds gets its own row under the banner:
/// on the banner's violet tint, a `yellow` rail and `✎` (the one thing to
/// do there), its title and `waiting: <what to do>` from tmux-mcp's reason
/// (copy mode: `leave copy mode`); at 34 columns `waiting:` goes, and a
/// title too long gives way to its letter's key-cap. A click jumps to its
/// tab ([`Target::Held`]). It is never also in `still working` (that row
/// goes when only held agents are left).
#[test]
fn a_held_pause_gets_its_own_row() {
    let v = lettered(quiet());
    let p0a = held("P0a", "@9", Some('k'));
    let p = holding(&[], vec![p0a.clone()]);
    for w in [34u16, 44] {
        let f = layout(&v, w, 58, &Opts { pause: Some(p.clone()), ..with_bar(demo_bar()) });
        let t = texts(&f);
        let ctx = format!("{w}: {t:#?}");
        // The banner's row, then the held row; no `still working` row.
        assert_eq!(f.pause, 0..2, "{ctx}");
        assert!(t[0].contains("PAUSING  1 of 2 stopped") && !t.iter().any(|l| l.contains("still working")), "{ctx}");
        let r = &f.rows[1];
        assert_eq!((dwidth(&r.text()), r.bg), (usize::from(w), Some(pause_bg(&p))), "{ctx}");
        assert_eq!(r.segs[1], seg("▌", HELD_HUE), "{ctx}");
        assert!(r.segs.contains(&bold(HELD_GLYPH, HELD_HUE)), "{ctx}");
        let want = if w == 44 { " ▌ ✎ P0a  waiting: leave copy mode" } else { " ▌ ✎ P0a  leave copy mode" };
        assert_eq!(t[1].trim_end(), want, "{ctx}");
        assert!(r.segs.contains(&seg("leave copy mode", "text")), "{ctx}");
        assert!(!r.anim, "{ctx}");
        // Anywhere on it: that agent's tab.
        let tab = Target::Held { win: "@9".into(), session: "main".into() };
        assert!((0..w).all(|x| f.target(x, 1) == Some(&tab)), "{ctx}");
        // Never peach (⌥W's armed caps), never green; the panel follows.
        assert!(r.segs.iter().all(|s| ![Ink::Pal("peach"), Ink::Pal("green")].contains(&s.fg)), "{ctx}");
        assert!(f.card.start == 2, "{ctx}");
    }
    // Each reason, what to do about it (34 columns).
    let row34 = |h: Held| texts(&layout(&v, 34, 58, &Opts { pause: Some(holding(&[], vec![h])), ..Opts::default() }))[1].trim_end().to_string();
    for (why, want) in [
        ("no prompt box on screen (a dialog or menu may be up)", " ▌ ✎ P0a  show its prompt"),
        ("the prompt box does not have the cursor (a dialog or menu may be up)", " ▌ ✎ P0a  close its dialog"),
        ("a permission or question prompt is up", " ▌ ✎ P0a  answer its prompt"),
        ("its prompt box has a half-written draft in it", " ▌ ✎ P0a  clear its prompt box"),
        // A reason it does not know: the reason itself (dim, cut).
        ("earlier messages to it are still held", " ▌ ✎ P0a  earlier messages to i…"),
        // None at all: look.
        ("", " ▌ ✎ P0a  look at its tab"),
    ] {
        assert_eq!(row34(Held { why: why.into(), ..p0a.clone() }), want, "{why}");
    }
    let f = layout(&v, 44, 58, &Opts { pause: Some(holding(&[], vec![Held { why: "could not read the cursor".into(), ..p0a.clone() }])), ..Opts::default() });
    assert!(f.rows[1].segs.contains(&seg("could not read the cursor", "sub")), "{:?}", f.rows[1].segs);
    // A long title: cut (`waiting:` goes first), at 34 its letter when what
    // to do is long; no letter, the title cut.
    let long = |c: Option<char>, why: &str| holding(&[], vec![Held { why: why.into(), ..held("Tmux Agent Sidebar redesign", "@9", c) }]);
    let t = texts(&layout(&v, 44, 58, &Opts { pause: Some(long(Some('k'), COPY)), ..Opts::default() }));
    assert_eq!(t[1].trim_end(), " ▌ ✎ Tmux Agent…  waiting: leave copy mode", "{t:#?}");
    let t = texts(&layout(&v, 34, 58, &Opts { pause: Some(long(Some('k'), COPY)), ..Opts::default() }));
    assert_eq!(t[1].trim_end(), " ▌ ✎ Tmux Agen…  leave copy mode", "{t:#?}");
    let draft = "its prompt box has a half-written draft in it";
    let f = layout(&v, 34, 58, &Opts { pause: Some(long(Some('k'), draft)), ..Opts::default() });
    assert_eq!(texts(&f)[1].trim_end(), " ▌ ✎  k  clear its prompt box", "{:#?}", texts(&f));
    assert!(f.rows[1].segs.iter().any(|s| s.text == " k " && s.bold && s.bg.is_some()), "{:?}", f.rows[1].segs);
    let t = texts(&layout(&v, 34, 58, &Opts { pause: Some(long(None, draft)), ..Opts::default() }));
    assert_eq!(t[1].trim_end(), " ▌ ✎ Tmux…  clear its prompt box", "{t:#?}");
    // Others still working keep their row, above the held one.
    let both = holding(&["Census AX sweep"], vec![p0a.clone()]);
    let t = texts(&layout(&v, 44, 58, &Opts { pause: Some(both), ..Opts::default() }));
    assert!(t[1].contains("still working  Census AX sweep") && !t[1].contains("P0a") && t[2].contains("✎ P0a"), "{t:#?}");
    // While the press is still in flight too.
    let sending = Phase::Sending { done: 2, n: 2, labels: vec!["P0a".into(), "Two".into()], held: vec![p0a.clone()] };
    let f = layout(&v, 34, 58, &Opts { pause: Some(sending), ..Opts::default() });
    assert_eq!(f.pause, 0..3, "{:#?}", texts(&f));
    assert!(texts(&f)[2].contains("✎ P0a  leave copy mode"), "{:#?}", texts(&f));
    // A window the snapshot could not place: the row says it, no jump.
    let lost = holding(&[], vec![Held { session: String::new(), ..p0a.clone() }]);
    let f = layout(&v, 44, 58, &Opts { pause: Some(lost), ..Opts::default() });
    assert!(texts(&f)[1].contains("✎ P0a") && (0..44).all(|x| f.target(x, 1).is_none()), "{:#?}", texts(&f));
    // Once it stops (safe, nothing held), the row is gone: the banner
    // settles like any other.
    let f = layout(&v, 44, 58, &Opts { pause: Some(Phase::Safe { labels: vec!["P0a".into()] }), ..Opts::default() });
    assert!(!texts(&f).iter().any(|l| l.contains(HELD_GLYPH)) && f.pause == (0..2), "{:#?}", texts(&f));
}

/// One row per held agent, at most [`HELD_MAX`]: past it the last says
/// how many more (and jumps to the first of them). Short frames keep one
/// (a count when several), very short ones none; every frame still exactly
/// its height.
#[test]
fn held_rows_are_capped() {
    let v = lettered(quiet());
    let many: Vec<Held> = (0..5).map(|i| held(&format!("Agent {i}"), &format!("@{}", 20 + i), None)).collect();
    let three = holding(&[], many[..3].to_vec());
    let t = texts(&layout(&v, 44, 58, &Opts { pause: Some(three), ..Opts::default() }));
    assert!((1..4).all(|y| t[y].contains(&format!("✎ Agent {}", y - 1))), "{t:#?}");
    let five = holding(&[], many.clone());
    let f = layout(&v, 44, 58, &Opts { pause: Some(five.clone()), ..Opts::default() });
    let t = texts(&f);
    assert_eq!(f.pause, 0..4, "{t:#?}");
    assert!(t[1].contains("✎ Agent 0") && t[2].contains("✎ Agent 1"), "{t:#?}");
    assert!(t[3].contains("✎ +3 more  look at their tabs"), "{t:#?}");
    assert_eq!(f.target(5, 3), Some(&Target::Held { win: "@22".into(), session: "main".into() }));
    // 10..16 rows: the banner's one row and one held row (a count of all).
    for h in [10u16, 15] {
        let f = layout(&v, 34, h, &Opts { pause: Some(five.clone()), ..Opts::default() });
        let t = texts(&f);
        assert_eq!((f.pause.clone(), f.rows.len()), (0..2, usize::from(h)), "{t:#?}");
        assert!(t[1].contains("✎ 5 agents waiting"), "{t:#?}");
        let one = holding(&[], many[..1].to_vec());
        assert!(texts(&layout(&v, 34, h, &Opts { pause: Some(one), ..Opts::default() }))[1].contains("✎ Agent 0  leave copy mode"));
    }
    for h in 2u16..10 {
        let f = layout(&v, 34, h, &Opts { pause: Some(five.clone()), ..Opts::default() });
        assert!(f.rows.len() == usize::from(h) && !texts(&f).iter().any(|l| l.contains(HELD_GLYPH)), "{h}: {:#?}", texts(&f));
    }
    assert_eq!((held_lines(58), held_lines(16), held_lines(12), held_lines(9)), (HELD_MAX, HELD_MAX, 1, 0));
}

mod answer;
mod insight;
mod pending;
mod pinned;
