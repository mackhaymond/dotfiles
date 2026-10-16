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

/// Every agent is covered exactly once, and `… N more` says N.
fn check_accounting(name: &str, v: &ViewModel, f: &Frame) {
    let mut want: HashMap<(String, String), usize> = HashMap::new();
    for a in groups(v).iter().flat_map(|s| s.agents.iter()) {
        *want.entry((a.session.clone(), a.window_id.clone())).or_default() += 1;
    }
    let mut got: HashMap<(String, String), usize> = HashMap::new();
    for r in &f.rows {
        for c in &r.covers {
            *got.entry(c.clone()).or_default() += 1;
        }
        let t = r.text();
        let counted = t.contains(" more") || t.contains(" agents");
        if let Some(i) = t.find("… ").filter(|_| counted && !r.covers.is_empty()) {
            let n: usize = t[i + "… ".len()..].split(' ').next().unwrap().parse().unwrap();
            assert_eq!(n, r.covers.len(), "{name} {}x{}: {t:?}", f.width, f.height);
        }
    }
    assert_eq!(got, want, "{name} {}x{}: {:#?}", f.width, f.height, texts(f));
}

/// The order every frame keeps: the card (or title; nothing when the card
/// would be empty), NEEDS YOU, agents, shells, spare rows, the spaces, the
/// parked row; sections never overlap.
fn check_order(name: &str, f: &Frame) {
    let h = usize::from(f.height);
    if h < 4 {
        return;
    }
    let t = texts(f);
    assert!(f.needs.end <= f.agents.start, "{name}: {:?} {:?} {t:#?}", f.needs, f.agents);
    assert!(f.agents.end <= f.spaces.start && f.spaces.end < h, "{name}: {:?} {:?} {t:#?}", f.agents, f.spaces);
    if !f.shells.is_empty() {
        assert!(f.agents.end <= f.shells.start && f.shells.end <= f.spaces.start, "{name}: shells {:?} {t:#?}", f.shells);
        assert!(t[f.shells.start].starts_with(" SHELLS"), "{name}: {t:#?}");
    }
    if !f.card.is_empty() {
        assert!(f.card.start == 0 && f.card.end <= f.needs.start, "{name}: card {:?} {t:#?}", f.card);
        assert!(f.status.start > f.card.start && f.status.end < f.card.end, "{name}");
    }
    if !f.spaces.is_empty() {
        assert!(t[f.spaces.start].contains('─'), "{name}: the rule first {t:#?}");
        assert!(f.spaces.len() >= 2, "{name}: a rule and pills {t:#?}");
        assert_eq!(f.spaces.end, h - 1, "{name}: spaces sit on the last row {t:#?}");
    }
    assert!(t[h - 1].starts_with(" ⏸ "), "{name}: {t:#?}");
    assert!(!t.iter().any(|l| l.starts_with(" log") || l.contains('═')), "{name}: {t:#?}");
}

#[test]
fn every_size_fits_and_accounts_for_every_agent() {
    let mut sel = demo_bar();
    sel.selecting = Some(3);
    sel.prefix = true;
    // Toasts: a short one in the card, a long one (wrapped rows) with the
    // marks, and one with no card at all.
    let long = Bar { toast: Some(toast(Kind::Error, LONG)), ..sel.clone() };
    let toasted = Bar { toast: Some(toast(Kind::Ok, "parked Canvas Syllabus")), ..demo_bar() };
    let bare = Bar { toast: Some(toast(Kind::Warn, LONG)), ..Bar::default() };
    let bars = [Bar::default(), demo_bar(), sel, toasted, long, bare];
    let cases = |lettered: bool| -> Vec<(&Bar, u16)> {
        if lettered {
            // (The toasts at the border's narrowest and a wide width; the
            // short one at the usual one too.)
            let widths = |b: &Bar| match &b.toast {
                Some(t) if t.text == LONG => vec![20u16, 60],
                Some(_) => vec![20u16, 34, 60],
                None => vec![24u16, 30, 38, 60],
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
                // A toast drawn is a toast clickable, and the card keeps its corners.
                for y in f.toast.clone() {
                    assert!(f.rows[y].targets.iter().any(|t| matches!(t.2, Target::Toast(TOAST_ID))), "{name} {w}x{h}: {y}");
                }
                assert_eq!(f.toast.is_empty(), f.toast_until.is_none(), "{name} {w}x{h}");
                if !f.card.is_empty() {
                    let y = f.card.end as u16 - 1;
                    assert_eq!((buf[(0, y)].symbol(), buf[(w - 1, y)].symbol(), buf[(w - 2, y)].symbol()), ("╰", "╯", "─"),
                        "{name} {w}x{h}");
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
/// lines first, then the gaps, then card lines; agents keep their full
/// detail until all of that is gone, and their step only grows.
#[test]
fn agents_lose_detail_last() {
    for (name, v) in all() {
        let mut last = (0u8, 0u8);
        // (Under 8 rows the spaces' reserve itself gives way: not a ladder.)
        for h in (8u16..=90).rev() {
            let f = layout(&v, 34, h, &Opts::default());
            let now = (f.step, f.agents_step);
            assert!(now >= last, "{name} {h}: {now:?} after {last:?}");
            if f.agents_step > 0 {
                // Compressed agents: no gaps, one-line cards.
                assert_eq!(f.step, STEPS.len() as u8, "{name} {h}");
                let cards = f.rows[f.needs.clone()].iter().filter(|r| r.bg.is_some()).count();
                assert!(cards <= v.needs.len(), "{name} {h}: {:#?}", texts(&f));
            }
            if f.step == 0 && f.spaces.len() < 1 + pill_lines(&v, 34) {
                // Rows short at step 0: the spaces gave them up first.
                assert!(f.agents.end + 2 + f.spaces.len() >= usize::from(h) - 1, "{name} {h}: {:#?}", texts(&f));
            }
            last = now;
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
            for h in 2u16..=90 {
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

/// With a card and shells too, a shrinking frame only ever gives detail
/// up: the step, the agents' step and the shells' form (section → one
/// line → none) never come back while the card keeps its size (the card
/// has its own ladder: the meters, then the border, then usage go, and the
/// rows that frees may well buy the sections detail back).
#[test]
fn shrinking_never_brings_detail_back() {
    let bars = [demo_bar(), Bar { usage: vec![], stale: None, ..demo_bar() }, Bar { prefix: true, ..demo_bar() }];
    for (name, v) in all() {
        for bar in &bars {
            for w in [30u16, 34, 44] {
                let mut last = (0u8, 0u8, 0u8);
                let mut top = usize::MAX;
                for h in (12u16..=100).rev() {
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
    assert!(t[1].trim().is_empty() && t[2].starts_with(" NEEDS YOU") && t[2].ends_with("1 "), "{t:#?}");
    // The card: a tinted surface with a yellow edge; title, glyph and age;
    // then the question.
    assert!(t[3].starts_with("▌ Kua Yu focus timing") && t[3].ends_with("◉ 3m "), "{:?}", t[3]);
    assert!(t[4].starts_with("▌ Which deck should I pull"), "{:?}", t[4]);
    assert_eq!((f.rows[3].bg, f.rows[4].bg), (Some(Ink::Mix("yellow", 14)), Some(Ink::Mix("yellow", 14))));
    assert_eq!(f.rows[3].segs[0], seg("▌", "yellow"));
    // Then a gap, and the agents right under it.
    assert!(t[5].trim().is_empty() && t[6].starts_with(" AGENTS") && t[6].ends_with("all spaces "), "{t:#?}");
    assert_eq!(f.agents.start, 6);
    // Space headers: a dot and the name, the counts on the right, no rule.
    let main = find(&f, " ● main");
    assert!(t[main].ends_with("◉1 ◐2 ○4 ") && !t[main].contains('─'), "{:?}", t[main]);
    // The client's agent: highlighted, a blue edge, the spinner and its
    // word; its message under it with the age.
    let r = find(&f, "Tmux Agent Sidebar");
    assert!(t[r].starts_with("▎ Tmux Agent Sidebar") && t[r].ends_with("⠋ working "), "{:?}", t[r]);
    assert!(t[r + 1].starts_with("▎   Edit agent-roster.py") && t[r + 1].ends_with(" 2m "), "{:?}", t[r + 1]);
    assert_eq!((f.rows[r].bg, f.rows[r + 1].bg), (Some(Ink::Pal("surface0")), Some(Ink::Pal("surface0"))));
    // A title gives way to the state word (and the flag stays).
    let h = find(&f, "Handy.app Speech C… ⚙");
    assert!(t[h].ends_with("⠋ working ") && t[h + 1].starts_with("    Bash swift build"), "{t:#?}");
    assert!(f.rows[h].bg.is_none());
    // Recently done: the word, and its message.
    let c = find(&f, "Commodities Job Tracker");
    assert!(t[c].ends_with("✓ done ") && t[c + 1].starts_with("    7 fixes applied"), "{t:#?}");
    // Idle: two lines like every agent, the word on the first, the age
    // under it; then a hairline (no target) before the next agent.
    let n = find(&f, "Lost suit jacket");
    assert!(t[n].ends_with(" idle ") && t[n + 1].ends_with(" 6m ") && is_sep(&t[n + 2]), "{t:#?}");
    assert_eq!(t[n + 2], format!("  {}  ", "─".repeat(30)));
    assert!(f.rows[n + 2].targets.is_empty() && f.rows[n + 2].covers.is_empty() && f.rows[n + 3].covers.len() == 1, "{t:#?}");
    // No hairline after a space's last agent: its next row is a gap.
    let last = f.agents.end - 1;
    assert!(!t[f.agents.clone()].iter().zip(&t[f.agents.start + 1..=last]).any(|(a, b)| is_sep(a) && (b.trim().is_empty() || b.starts_with(" ●"))), "{t:#?}");
    assert!(t[find(&f, "Notch Tasks Orchestrator")].ends_with(" idle "), "{t:#?}");
    // A blank row between groups, in ⌃N (here: name) order.
    let bai = find(&f, " ● bai");
    assert!(t[main - 1].trim().is_empty() && bai < main, "{t:#?}");
    // The spaces: rule and pills right above the parked row; spare rows above them.
    assert_eq!(f.spaces, 55..57, "{t:#?}");
    assert!(t[55].starts_with(" ────") && t[56].starts_with("  bai   main ◉   schedule"), "{t:#?}");
    assert!(t[f.agents.end..55].iter().all(|l| l.trim().is_empty()), "{t:#?}");
    assert_eq!(t[57], " ⏸ 1 parked                       ");
    assert!(!f.spin || t.iter().any(|l| l.contains('⠋')));
}

#[test]
fn busy_squeezes_spaces_and_cards_before_agents() {
    let v = busy();
    // 24 agents, two lines each and hairlines between: full detail in 110
    // rows, gaps and all.
    let f = layout(&v, 34, 110, &Opts::default());
    assert!(f.step == 0 && texts(&f).iter().any(|l| is_sep(l)), "{:#?}", texts(&f));
    // Shorter: the cards lose their second line before any agent loses
    // detail; then the hairlines go, the agents still two lines.
    let f = (20..110).rev().map(|h| layout(&v, 34, h, &Opts::default())).find(|f| f.step > 0).unwrap();
    assert_eq!(f.agents_step, 0, "{:#?}", texts(&f));
    let f = (20..110).rev().map(|h| layout(&v, 34, h, &Opts::default())).find(|f| !texts(f).iter().any(|l| is_sep(l))).unwrap();
    assert_eq!((f.step, f.agents_step), (STEPS.len() as u8, 0), "{:#?}", texts(&f));
    // Shorter still: the agents compress, every one of them the same height.
    for step in [1, 2, 3] {
        let f = (20..90).rev().map(|h| layout(&v, 34, h, &Opts::default())).find(|f| f.agents_step == step).unwrap();
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
    let f = layout(&sv, 34, 40, &Opts::default());
    assert_eq!((f.step, f.agents_step), (0, 0), "{:#?}", texts(&f));
    assert_eq!(f.spaces.len(), 1 + pill_lines(&sv, 34));
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
    let pills = &t[f.spaces.start + 1..f.spaces.end];
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
    // The spaces never go: the rule and a pill line with the client's space.
    assert!(f.spaces.len() >= 2 && t[f.spaces.start + 1].contains(" alpha "), "{t:#?}");
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
    // The plain `main` session is still a space: its pill.
    assert_eq!(&t[f.spaces.clone()][1].trim_end(), &"  main");
}

#[test]
fn click_targets() {
    let v = quiet();
    let f = layout(&v, 34, 58, &Opts::default());
    let go = |win: &str| Some(Target::Go { win: win.into(), session: "main".into() });
    // Both lines of an agent row, anywhere, and both lines of the card.
    let r = find(&f, "Tmux Agent Sidebar") as u16;
    assert_eq!(f.target(5, r).cloned(), go("@2"));
    assert_eq!(f.target(0, r + 1).cloned(), go("@2"));
    assert_eq!(f.target(33, r + 1).cloned(), go("@2"));
    let card = find(&f, "▌ Kua Yu") as u16;
    for y in card..card + 2 {
        assert_eq!(f.target(0, y).cloned(), go("@3"));
        assert_eq!(f.target(33, y).cloned(), go("@3"));
    }
    // Space headers and pills go to the session; the gap between pills does nothing.
    let hd = find(&f, " ● bai") as u16;
    assert_eq!(f.target(20, hd).cloned(), Some(Target::Session("bai".into())));
    let pills = f.spaces.start as u16 + 1;
    let line = f.rows[pills as usize].text();
    let x = line.find(" main ").unwrap() as u16 + 2;
    assert_eq!(f.target(x, pills), Some(&Target::Session("main".into())));
    assert_eq!(f.target(1, pills), Some(&Target::Session("bai".into())));
    let gap = line.find(" main ").unwrap() as u16 - 1;
    assert_eq!(f.target(gap, pills), None, "{line:?}");
    assert_eq!(f.target(0, pills), None);
    // The rule, the section headers and blank rows do nothing.
    assert_eq!(f.target(5, f.spaces.start as u16), None);
    assert_eq!(f.target(2, find(&f, " NEEDS YOU") as u16), None);
    assert_eq!(f.target(2, 1), None);
    // Parked opens the stash picker.
    assert_eq!(f.target(2, 57), Some(&Target::Stash));
    // A watcher problem is a restart button.
    let mut v = quiet();
    v.watcher_age = Some(99.0);
    let f = layout(&v, 34, 58, &Opts::default());
    assert!(f.rows[57].text().ends_with("watcher stalled 99s "));
    assert_eq!(f.target(30, 57), Some(&Target::Restart));
    // A message wins the corner (and is not a button).
    let f = layout(&v, 34, 58, &Opts { msg: Some("no tmux client".into()), ..Opts::default() });
    assert!(f.rows[57].text().ends_with("no tmux client "));
    assert_eq!(f.target(30, 57), None);
    assert_eq!(f.target(2, 57), Some(&Target::Stash));
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
    // never the old outcome.
    let c = at("Commodities Job Tracker");
    assert!(!t[c].contains("~/") && t[c + 1].trim() == "1m" && is_sep(&t[c + 2]) && f.rows[c + 3].covers.len() == 1, "{t:#?}");
    // Working with what it runs: plain text under it; `project/` dropped.
    let r = at("Tmux Agent Sidebar");
    assert!(!t[r].contains("tmux/") && t[r + 1].starts_with("    cargo test "), "{:?}", t[r + 1]);
    let m = at("Markup only");
    assert!(t[m + 1].trim() == "5m", "{t:#?}");
    // Recently done (idle, 4 min): its outcome as plain text.
    let l = at("Lost suit jacket");
    assert!(t[l].contains("✓ done") && t[l + 1].starts_with("    Found it "), "{:?}", t[l + 1]);
    // Long done: idle, its last outcome under it with the age.
    let g = at("Long ago");
    assert!(t[g].ends_with(" idle ") && t[g + 1].starts_with("    Old news") && t[g + 1].ends_with(" 1h "), "{t:#?}");
    // Asking: the question.
    let k = at("Kua Yu");
    assert!(t[k].contains("◉ asks") && t[k + 1].starts_with("    Which deck?"), "{t:#?}");
    // Needs cards: no `~/`, plain reasons.
    assert!(t.iter().any(|l| l.starts_with("▌ Kua Yu focus timing")), "{t:#?}");
    assert!(t.iter().any(|l| l.starts_with("▌ Which deck?")), "{t:#?}");
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
        let pills = t[f.spaces.start + 1..f.spaces.end].join("|");
        assert!(pills.contains(" zz "), "{h}: {t:#?}");
        assert!(h < 8 || pills.contains(" s07 "), "{h}: {t:#?}");
        let shown = pills.matches(" s").count() + pills.matches(" zz ").count() + pills.matches(" main ").count();
        if shown < 13 {
            // The rest as `+K`, which opens the menu on All.
            let k: usize = pills.split(" +").nth(1).unwrap().split(' ').next().unwrap().parse().unwrap();
            assert_eq!(shown + k, 13, "{h}: {pills:?}");
            let y = (f.spaces.start + 1..f.spaces.end).find(|&y| t[y].contains(" +")).unwrap();
            let x = t[y].find(" +").unwrap() as u16 + 1;
            assert_eq!(f.target(x, y as u16), Some(&Target::Menu(Some("all"))));
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
    let pills = t[f.spaces.start + 1..f.spaces.end].join(" ");
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
    let y = (f.spaces.start + 1) as u16;
    let x = t[y as usize].find(" cua-notch").unwrap() as u16 + 2;
    assert_eq!(f.target(x, y), Some(&Target::Session("cua-notch".into())));
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
    let p = &f.rows[f.spaces.start + 1];
    // The client's space: filled with its colour, dark ink.
    assert!(p.segs.contains(&block(" 1 main ", "crust", Ink::Rgb(0xf9, 0xe2, 0xaf))), "{:?}", p.segs);
    // Another: its digit in its colour, on surface0.
    assert!(p.segs.contains(&Seg { bold: true, ..on(" 2", Ink::Idx(24), "surface0") }), "{:?}", p.segs);
    assert!(p.segs.contains(&Seg { bold: true, ..on(" 3", "overlay2", "surface0") }), "{:?}", p.segs);
    // Space headers: the dot in the space's colour.
    let hd = &f.rows[find(&f, " ● bai")];
    assert_eq!(hd.segs[1], seg("● ", Ink::Idx(24)));
    // An alert space's pill carries the state glyph.
    let v = view(vec![a("main", 1, "running", "M"), a("bai", 1, "needs-input", "B")], Some("@1"));
    let f = layout(&v, 34, 40, &Opts::default());
    assert!(f.rows[f.spaces.start + 1].text().contains(" bai ◉ "), "{:?}", f.rows[f.spaces.start + 1].text());
}

#[test]
fn folding_and_collapsing() {
    let v = busy();
    // Idle folded first, the rows still two lines each.
    let f = (20..90).rev().map(|h| layout(&v, 34, h, &Opts::default())).find(|f| f.agents_step == 1).unwrap();
    let t = texts(&f);
    assert!(t.iter().any(|l| l.contains("○○○ 3 idle · ")), "{t:#?}");
    // The fold opens the menu on its Idle tab.
    let fold = find(&f, "○○○ 3 idle · ") as u16;
    assert_eq!(f.target(5, fold), Some(&Target::Menu(Some("idle"))));
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
    assert!(t.iter().any(|l| l.starts_with("  Two")), "{t:#?}");
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

/// A card like the live one: main (⌃1, yellow), three shells (home first,
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
                members: vec![Member { slot: Some(1), used: 90, active: false }, Member { slot: Some(2), used: 30, active: true }],
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

/// Every agent row and NEEDS YOU card shows its letter as a key-cap, in
/// both scopes; peach while ⌥W's jump table is armed; blank past 26.
#[test]
fn letter_key_caps_lead_every_agent_row_and_card() {
    let v = lettered(quiet());
    let f = layout(&v, 34, 58, &Opts::default());
    let t = texts(&f);
    let r = find(&f, "Tmux Agent Sidebar");
    assert!(t[r].starts_with("▎ f  Tmux Agent Sidebar"), "{:?}", t[r]);
    assert!(t[r + 1].starts_with("▎    Edit agent-roster.py"), "{:?}", t[r + 1]);
    // On the lifted row the cap is lifted too.
    assert_eq!(f.rows[r].segs[1], on(" f ", "sub", "surface1"));
    let c = find(&f, "▌ g  Kua Yu focus timing");
    assert_eq!(f.rows[c].segs[1], on(" g ", "sub", "surface1"));
    assert!(t[c + 1].starts_with("▌    Which deck"), "{t:#?}");
    let i = find(&f, "  h  Commodities Job");
    assert_eq!(f.rows[i].segs[1], on(" h ", "sub", "surface0"));
    // Space scope: the same letters.
    let mut sv = lettered(quiet());
    sv.scope.session = Some("main".into());
    let f = layout(&sv, 34, 58, &Opts::default());
    find(&f, " f  Tmux Agent Sidebar");
    find(&f, "▌ g  Kua Yu");
    // ⌥W armed (key table `jump`): every cap peach; the mark and the hint
    // in the card's top border, the card no taller for it.
    let f = layout(&v, 34, 58, &with_bar(Bar { jump: true, ..demo_bar() }));
    assert!(f.card_title.contains(&block(" ⌥W jump ", "crust", "peach")), "{:?}", f.card_title);
    assert_eq!(top_border(&f), "╭─ ⌥W jump  type a letter ───────╮");
    assert_eq!(f.card, layout(&v, 34, 58, &with_bar(demo_bar())).card);
    let r = find(&f, "Tmux Agent Sidebar");
    assert_eq!(f.rows[r].segs[1], block(" f ", "crust", "peach"));
    let c = find(&f, "▌ g  Kua Yu");
    assert_eq!(f.rows[c].segs[1], block(" g ", "crust", "peach"));
    // Narrow: the hint gives way, the mark stays whole, the corner too.
    let f = layout(&v, 22, 58, &with_bar(Bar { jump: true, ..demo_bar() }));
    let top = top_border(&f);
    assert!(top.starts_with("╭─ ⌥W jump  type") && top.ends_with("─╮") && dwidth(&top) == 22, "{top:?}");
    // Past 26 agents: a blank cap, the title still aligned.
    let x = lettered(extreme());
    let none = x.agents().find(|a| a.letter.is_none()).expect("an agent past 26");
    let f = layout(&x, 60, 400, &Opts::default());
    let k = f.rows.iter().position(|r| r.covers.iter().any(|(s, id)| *s == none.session && *id == none.window_id)).unwrap();
    assert_eq!(f.rows[k].segs[1].text, "   ", "{:?}", f.rows[k].text());
    assert!(f.rows[k].text().starts_with("     An agent"), "{:?}", f.rows[k].text());
}

/// The card's top border as painted, the key-table marks in it.
fn top_border(f: &Frame) -> String {
    let mut buf = Buffer::empty(Rect::new(0, 0, f.width, f.height));
    paint(f, &mut buf);
    let y = f.card.start as u16;
    (0..f.width).map(|x| buf[(x, y)].symbol().to_string()).collect()
}

/// The status line's content, in the sidebar's card: a rounded Block, the
/// red prefix mark in its top border, one meter per usage window, the
/// stale save; no space chip and no shells (the pills at the bottom and the
/// SHELLS section say those). Shorter frames give up the meters for one
/// line, then the border.
#[test]
fn the_card_replaces_the_status_line() {
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
    assert!(t[3].starts_with("  F ─") && t[3].contains("   -- "), "{:?}", t[3]);
    assert!(t[4].contains(" ! ") && t[4].contains("last save 2h ago"), "{:?}", t[4]);
    // The card's border is ratatui's rounded Block, painted over the rows
    // (no row's own text carries it); nothing in it while no table is armed.
    let mut buf = Buffer::empty(Rect::new(0, 0, 34, 40));
    paint(&f, &mut buf);
    assert_eq!((buf[(0, 0)].symbol(), buf[(33, 0)].symbol(), buf[(0, 5)].symbol(), buf[(33, 5)].symbol()), ("╭", "╮", "╰", "╯"));
    assert_eq!((buf[(0, 2)].symbol(), buf[(33, 2)].symbol()), ("│", "│"));
    assert_eq!(buf[(0, 2)].fg, palette::color("surface1"));
    assert!(t[0].trim().is_empty() && t[5].trim().is_empty() && !t[2].contains('│'));
    assert_eq!(top_border(&f), format!("╭{}╮", "─".repeat(32)));
    // The red prefix mark, keyed on the client's key table: in the border.
    let p = layout(&v, 34, 40, &with_bar(Bar { prefix: true, ..demo_bar() }));
    assert_eq!(p.card_title, vec![block(" prefix ", "crust", "red")]);
    assert_eq!(top_border(&p), format!("╭─ prefix {}╮", "─".repeat(23)));
    assert_eq!((p.card.clone(), texts(&p)), (f.card.clone(), t.clone()), "the card keeps its rows");
    // 34x20: still a card, usage in one line.
    let f = layout(&v, 34, 20, &with_bar(demo_bar()));
    let t = texts(&f);
    assert!(!f.card.is_empty() && t.iter().any(|l| l.contains("S 13%  W 60%  F --")), "{t:#?}");
    // 34x14: no border, the prefix mark on a row of its own; 34x11: no
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

/// The client's space's shells: a SHELLS section under the agents, one row
/// each (⌥N key-cap, ⌂ on the home, the label), the active one lifted, each
/// clicking through to its shell.
#[test]
fn shells_list_under_the_agents() {
    let v = lettered(quiet());
    let f = layout(&v, 34, 58, &with_bar(demo_bar()));
    let t = texts(&f);
    assert!(f.shells.start > f.agents.end, "{t:#?}");
    let s = f.shells.start;
    assert_eq!(f.shells.len(), 4, "{t:#?}");
    assert_eq!(t[s].trim_end(), " SHELLS");
    assert_eq!(t[s + 1].trim_end(), "  ⌥1  ⌂ ~");
    assert_eq!(t[s + 2].trim_end(), "▎ ⌥2  nvim");
    assert_eq!(t[s + 3].trim_end(), "  ⌥3  proj");
    assert_eq!(f.rows[s + 2].bg, Some(Ink::Pal("surface0")));
    assert!(f.rows[s + 2].segs.iter().any(|x| x.text == "nvim" && x.bold));
    assert_eq!(f.rows[s + 1].bg, None);
    let shell = |win: &str| Some(Target::Shell { win: win.into(), session: "main".into() });
    for (k, win) in ["@901", "@902", "@903"].iter().enumerate() {
        let y = (s + 1 + k) as u16;
        assert_eq!((f.target(0, y).cloned(), f.target(33, y).cloned()), (shell(win), shell(win)));
    }
    assert_eq!(f.target(3, s as u16), None);
    // A blank row between the agents and the shells while the gaps last.
    assert!(t[s - 1].trim().is_empty(), "{t:#?}");
    // No shells, no section.
    let f = layout(&v, 34, 58, &with_bar(Bar { shells: vec![], ..demo_bar() }));
    assert!(f.shells.is_empty() && !texts(&f).iter().any(|l| l.contains("SHELLS")));
    // Short: one line of pills before the agents lose any detail, then
    // kept in one line while they compress.
    let v = busy();
    for h in 12u16..=90 {
        let f = layout(&v, 34, h, &with_bar(demo_bar()));
        let t = texts(&f);
        assert!(f.shells.len() == 4 || f.shells.len() == 1, "{h}: {t:#?}");
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
    assert!(t.starts_with(" SHELLS  ⌥1 ⌂sh1   ⌥2 sh2") && t.contains(" ⌥9 sh9   sh10   sh11 "), "{t:?}");
    assert!(shells_line(&Bar::default(), 34).is_empty() && shells_section(&Bar::default(), 34).is_empty());
}

/// A meter: filled to the used percent, the tick where the pace expects
/// it, the reset hint only when the bar keeps its length.
#[test]
fn meters_fill_and_tick() {
    let u = Usage { label: "W".into(), color: Some(Ink::Pal("red")), used: Some(60.0), expected: Some(43.0), delta: Some(17),
        resets: Some(NOW + 3 * 3600), ..Usage::default() };
    let r = meter_row(&u, 40, NOW as f64);
    let t = r.text();
    assert_eq!(dwidth(&t), 40);
    let bar: String = t.chars().skip(3).take_while(|c| "━─┃".contains(*c)).collect();
    let bw = bar.chars().count();
    let tick = bar.chars().position(|c| c == '┃').unwrap();
    assert_eq!(tick, (0.43 * bw as f64).round() as usize, "{t:?}");
    let filled = bar.chars().filter(|&c| c == '━').count() + usize::from(tick < (0.6 * bw as f64).round() as usize);
    assert_eq!(filled, (0.6 * bw as f64).round() as usize, "{t:?}");
    assert!(t.ends_with(" 60% (+17%) ") || t.ends_with("↻3h "), "{t:?}");
    // Wide: the reset hint too.
    assert!(meter_row(&u, 60, NOW as f64).text().ends_with(" 60% (+17%)  ↻3h "), "{:?}", meter_row(&u, 60, NOW as f64).text());
    // Under pace: dim; on pace: `±0%`; idle: an empty bar and `idle`; no number: `--`.
    let under = Usage { delta: Some(-4), ..u.clone() };
    assert!(meter_row(&under, 40, 0.0).segs.iter().any(|s| s.text.contains(" (-4%)") && s.fg == Ink::Pal("overlay")));
    let even = Usage { delta: Some(0), pooled: true, ..u.clone() };
    assert!(meter_row(&even, 40, 0.0).text().contains(" 60%  (±0%) "), "{:?}", meter_row(&even, 40, 0.0).text());
    let idle = Usage { label: "S".into(), idle: true, ..Usage::default() };
    let t = meter_row(&idle, 34, 0.0).text();
    assert!(t.contains("─────") && !t.contains('━') && !t.contains('┃') && t.ends_with("idle "), "{t:?}");
    // Narrow: the delta goes before the bar does.
    let t = meter_row(&u, 16, NOW as f64).text();
    assert!(!t.contains("+17") && t.contains("60%") && dwidth(&t) == 16, "{t:?}");
    // Over the window: the real number, the bar full, the tick on its last cell.
    let over = Usage { used: Some(104.0), expected: Some(112.0), delta: Some(-8), ..u.clone() };
    let t = meter_row(&over, 40, NOW as f64).text();
    let bar: String = t.chars().skip(3).take_while(|c| "━─┃".contains(*c)).collect();
    assert!(t.contains("104%") && !bar.contains('─') && bar.ends_with('┃'), "{t:?}");
    let under = Usage { used: Some(-3.0), expected: Some(-10.0), ..u.clone() };
    let bar: String = meter_row(&under, 40, NOW as f64).text().chars().skip(3).take_while(|c| "━─┃".contains(*c)).collect();
    assert!(bar.starts_with('┃') && !bar.contains('━'), "{bar:?}");
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
    assert!(idle.ends_with(" 6m ") && msg.ends_with(" 2m ") && card.ends_with("◉ 3m "), "{idle:?} {msg:?} {card:?}");
    let (idle, msg, card) = at(Some(NOW as f64 + 60.0));
    assert!(idle.ends_with(" 7m ") && msg.ends_with(" 3m ") && card.ends_with("◉ 4m "), "{idle:?} {msg:?} {card:?}");
}

/// Dim text on a surface (a pill, a card, a lifted row) is `overlay2`,
/// never `overlay` (under 3:1 there).
#[test]
fn dim_text_on_surfaces_is_overlay2() {
    let f = layout(&lettered(quiet()), 34, 58, &with_bar(demo_bar()));
    for r in &f.rows {
        for s in &r.segs {
            let surface = s.bg.is_some() || r.bg.is_some();
            assert!(!(surface && s.fg == Ink::Pal("overlay") && !s.text.trim().is_empty()), "{:?} in {:?}", s, r.text());
        }
    }
}

/// prefix v's selection: every selected agent row (both lines) and shell
/// row on a mauve tint with a mauve edge; the hint.
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
        assert_eq!(row.segs[0], seg("▌", "mauve"));
    }
    let p = find(&f, "Plain");
    assert_eq!(f.rows[p].bg, None);
    let sh = f.shells.start + 2;
    assert!(t[sh].starts_with("▌ ⌥2  nvim"), "{t:#?}");
    assert_eq!((f.rows[sh].bg, f.rows[sh].segs[0].clone()), (Some(Ink::Mix("mauve", 18)), seg("▌", "mauve")));
    assert_eq!(f.rows[sh + 1].bg, None);
    // The hint, first in the card: as many of the keys as fit.
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

/// An agent row's title, when `l` is one (no letters in these views).
fn row_title(l: &str) -> Option<&str> {
    let rest = l.strip_prefix("  ").or_else(|| l.strip_prefix("▎ "))?;
    (!rest.starts_with(' ') && !is_sep(l)).then(|| rest.split("  ").next().unwrap().trim())
}

/// A hairline between two agents (from the third cell; the spaces' rule
/// starts at the second).
fn is_sep(l: &str) -> bool {
    l.starts_with("  ─") && l.trim().chars().all(|c| c == '─')
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
    assert!(b[0].starts_with(" AGENTS") && b[0].ends_with("this space "), "{b:#?}");
    assert!(b[1].starts_with(" ● proj"), "{b:#?}");
    // Window-index order; no shell rows.
    let rows: Vec<&str> = b[2..].iter().filter_map(|l| row_title(l)).collect();
    assert_eq!(rows, ["two", "four", "five", "twelve"], "{b:#?}");
    assert!(!b.iter().any(|l| l.contains("other") || l.contains("o/one") || l.contains("zsh")), "{b:#?}");
    // NEEDS YOU stays global, as do the spaces.
    let needs = &t[f.needs.clone()];
    assert!(needs.iter().any(|l| l.starts_with("▌ p/five")) && needs.iter().any(|l| l.starts_with("▌ p/twelve")), "{needs:#?}");
    assert!(t[f.spaces.clone()].iter().any(|l| l.contains(" other ")), "{t:#?}");
    // The title toggles, and so does the scope word; the counts do not.
    assert_eq!((f.target(0, 0), f.target(1, 0), f.target(5, 0), f.target(6, 0), f.target(30, 0)),
        (None, Some(&Target::Scope), Some(&Target::Scope), None, None));
    let y = f.agents.start as u16;
    assert_eq!((f.target(28, y), f.target(3, y)), (Some(&Target::Scope), None));
}

#[test]
fn explicit_space_scope_without_spaces_keeps_the_current_shell() {
    let v = scoped(proj(), "@1", "proj", "", "space", "");
    assert_eq!(v.scope.session.as_deref(), Some("proj"));
    let f = layout(&v, 34, 58, &Opts::default());
    let b = agents_part(&f);
    // The client's shell is a row (the classic rule), no state word.
    let z = b.iter().find(|l| l.contains("zsh")).unwrap_or_else(|| panic!("{b:#?}"));
    assert!(z.starts_with("▎ zsh") && z.trim_end().ends_with("zsh"), "{z:?}");
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
    assert!(b[0].ends_with("all spaces "), "{b:#?}");
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
        assert!(b[0].ends_with("all spaces ") && b.iter().any(|l| l.starts_with(" ● other")), "{session}: {b:#?}");
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
    assert!(t[10].starts_with(" ⏸ "), "{t:#?}");
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
        let seconds = rows.iter().filter(|r| r.covers.is_empty() && r.bg != Some(Ink::Mix("yellow", 14))).count();
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
        assert!(t[r].starts_with("▎ f  Tmux"), "{w}: {:?}", t[r]);
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
}

#[test]
fn glyph_widths_agree_with_ratatui() {
    // Every fixed glyph the sidebar draws: the layout's cell count
    // (text::dwidth) must be ratatui's (unicode-width over the grapheme).
    let glyphs = "⏵⤺≡⌥▌▎◉✓✕•◐○●─━┃│╭╮╰╯↻Σ−±↑↓✎⚙◎▸…✳⬢⏎⏸⌃⌂\u{e0a0}".to_string() + &SPIN.concat();
    let bad: Vec<(char, usize, usize)> = glyphs
        .chars()
        .map(|c| (c, dwidth(&c.to_string()), c.to_string().width()))
        .filter(|(_, a, b)| a != b || *a != 1)
        .collect();
    assert!(bad.is_empty(), "{bad:?}");
}

/// A toast posted at the fixtures' clock (seq 7, 4 s), for every sidebar.
fn toast(kind: Kind, text: &str) -> Toast {
    Toast { seq: 7, at_ms: NOW * 1000, kind, ttl: 4.0, client: String::new(), text: text.into() }
}

/// Its id: what a click on it dismisses.
const TOAST_ID: ToastId = (7, NOW * 1000);

/// A toast too long for any border.
const LONG: &str = "couldn't park Canvas Syllabus: it is still working on the export";

/// The card's bottom border as painted, a toast in it.
fn bottom_border(f: &Frame) -> String {
    let mut buf = Buffer::empty(Rect::new(0, 0, f.width, f.height));
    paint(f, &mut buf);
    let y = f.card.end as u16 - 1;
    (0..f.width).map(|x| buf[(x, y)].symbol().to_string()).collect()
}

/// A toast sits in the card's bottom border, as the key-table marks sit in
/// its top one: no row moves, the glyph bold in the kind's colour, a click
/// on its cells dismisses it, and it says when it expires.
#[test]
fn toasts_sit_in_the_cards_bottom_border() {
    let v = lettered(quiet());
    let plain = layout(&v, 34, 40, &with_bar(demo_bar()));
    let f = layout(&v, 34, 40, &with_bar(Bar { toast: Some(toast(Kind::Ok, "parked Canvas Syllabus")), ..demo_bar() }));
    assert_eq!(bottom_border(&f), "╰─ ✓ parked Canvas Syllabus ─────╯");
    assert_eq!(bottom_border(&plain), format!("╰{}╯", "─".repeat(32)));
    assert_eq!((f.card.clone(), f.status.clone(), texts(&f)), (plain.card.clone(), plain.status.clone(), texts(&plain)));
    assert_eq!(top_border(&f), format!("╭{}╮", "─".repeat(32)));
    assert!(f.card_footer.contains(&bold("✓", "green")) && f.card_footer.contains(&seg("parked Canvas Syllabus", "text")));
    let mut buf = Buffer::empty(Rect::new(0, 0, 34, 40));
    paint(&f, &mut buf);
    let y = f.card.end as u16 - 1;
    assert_eq!((buf[(3, y)].fg, buf[(3, y)].modifier.contains(Modifier::BOLD)), (palette::color("green"), true));
    assert_eq!((buf[(5, y)].fg, buf[(5, y)].bg), (palette::color("text"), Color::Reset));
    // A click on its cells (not the rule after them) dismisses it.
    assert_eq!(f.toast, y as usize..y as usize + 1);
    assert_eq!((f.target(2, y), f.target(27, y), f.target(28, y), f.target(1, y)), (Some(&Target::Toast(TOAST_ID)), Some(&Target::Toast(TOAST_ID)), None, None));
    assert_eq!(f.toast_until, Some((NOW + 4) * 1000));
    assert_eq!(plain.toast_until, None);
    // Each kind: its glyph and colour.
    for (k, g, hue) in [(Kind::Info, "•", "blue"), (Kind::Ok, "✓", "green"), (Kind::Warn, "!", "peach"), (Kind::Error, "✕", "red")] {
        let f = layout(&v, 34, 40, &with_bar(Bar { toast: Some(toast(k, "hi")), ..demo_bar() }));
        assert_eq!(f.card_footer[1], bold(g, hue), "{k:?}");
        assert!(bottom_border(&f).starts_with(&format!("╰─ {g} hi ─")), "{k:?}");
    }
    // With the prefix mark up top: both borders carry theirs.
    let f = layout(&v, 34, 40, &with_bar(Bar { prefix: true, toast: Some(toast(Kind::Info, "hi")), ..demo_bar() }));
    assert!(top_border(&f).starts_with("╭─ prefix ") && bottom_border(&f).starts_with("╰─ • hi "));
}

/// Narrow cards clip the toast with `…` and keep a rule cell and the
/// corner; a text that loses only a little stays in the border alone.
#[test]
fn a_toast_clips_to_keep_the_corner() {
    let v = lettered(quiet());
    let text = "parked Canvas Syllabus";
    let plain = layout(&v, 30, 40, &with_bar(demo_bar()));
    let f = layout(&v, 30, 40, &with_bar(Bar { toast: Some(toast(Kind::Warn, text)), ..demo_bar() }));
    let b = bottom_border(&f);
    assert!(b.starts_with("╰─ ! parked Canvas Sy") && b.ends_with("… ──╯") && dwidth(&b) == 30, "{b:?}");
    assert_eq!((f.card.clone(), texts(&f)), (plain.card.clone(), texts(&plain)), "a little clipped: nothing moves");
    for w in [20u16, 22, 26] {
        let f = layout(&v, w, 40, &with_bar(Bar { toast: Some(toast(Kind::Warn, text)), ..demo_bar() }));
        let b = bottom_border(&f);
        assert!(b.starts_with("╰─ ! ") && b.contains('…') && b.ends_with("──╯") && dwidth(&b) == usize::from(w), "{w}: {b:?}");
    }
}

/// A long toast also shows whole (wrapped, at most 2 rows, the rest cut)
/// inside the card just above its bottom border; those rows dismiss it too.
#[test]
fn a_long_toast_wraps_inside_the_card() {
    let v = lettered(quiet());
    let plain = layout(&v, 34, 40, &with_bar(demo_bar()));
    let f = layout(&v, 34, 40, &with_bar(Bar { toast: Some(toast(Kind::Error, LONG)), ..demo_bar() }));
    let t = texts(&f);
    assert_eq!(f.card.len(), plain.card.len() + 2, "{t:#?}");
    let r = f.card.end - 3;
    assert_eq!(f.toast, r..f.card.end);
    assert_eq!(t[r].trim_end(), "  ✕ couldn't park Canvas", "{t:#?}");
    assert_eq!(t[r + 1].trim_end(), "    Syllabus: it is still worki…", "{t:#?}");
    assert!(f.rows[r].segs.contains(&bold("✕", "red")));
    for y in [r, r + 1] {
        assert_eq!((f.target(1, y as u16), f.target(0, y as u16), f.target(33, y as u16)), (Some(&Target::Toast(TOAST_ID)), None, None));
    }
    let b = bottom_border(&f);
    assert!(b.starts_with("╰─ ✕ couldn't park Canvas") && b.ends_with("… ──╯"), "{b:?}");
    // The rows sit inside the card's border.
    let mut buf = Buffer::empty(Rect::new(0, 0, 34, 40));
    paint(&f, &mut buf);
    assert_eq!((buf[(0, r as u16)].symbol(), buf[(33, r as u16)].symbol()), ("│", "│"));
    // Two short lines: all of it, nothing cut.
    let f = layout(&v, 34, 40, &with_bar(Bar { toast: Some(toast(Kind::Info, "a fairly long note that wraps once")), ..demo_bar() }));
    let t = texts(&f);
    let r = f.toast.start;
    assert_eq!((t[r].trim_end(), t[r + 1].trim_end()), ("  • a fairly long note that", "    wraps once"), "{t:#?}");
    assert_eq!(wrap("aaaa bbbb cccccccccccc dd", 6, 3), ["aaaa", "bbbb", "ccccc…"]);
    assert_eq!(wrap("xxxxxxxxxx yy", 4, 3), ["xxx…", "yy"]);
    assert_eq!(wrap("one", 6, 2), ["one"]);
}

/// No card to carry it (no Bar, a short frame, nothing in the card): a
/// tinted row of its own at the very top; none in a tiny frame.
#[test]
fn a_toast_without_a_card_takes_the_top_row() {
    let v = lettered(quiet());
    let classic = layout(&v, 34, 30, &with_bar(Bar { toast: Some(toast(Kind::Info, "hello there")), ..Bar::default() }));
    let t = texts(&classic);
    assert!(t[0].starts_with(" • hello there") && t[1].starts_with(" agents"), "{t:#?}");
    assert_eq!((classic.rows[0].bg, classic.toast.clone(), classic.card.clone()), (Some(Ink::Mix("blue", 14)), 0..1, 0..0));
    assert_eq!(classic.target(20, 0), Some(&Target::Toast(TOAST_ID)));
    // A frame too short for the border: first, then the prefix mark, then usage.
    let f = layout(&v, 34, 14, &with_bar(Bar { prefix: true, toast: Some(toast(Kind::Warn, "hello")), ..demo_bar() }));
    let t = texts(&f);
    assert!(f.card.is_empty() && t[0].starts_with(" ! hello") && t[1].starts_with("  prefix ") && t[2].starts_with(" S 13%"), "{t:#?}");
    assert_eq!(f.rows[0].bg, Some(Ink::Mix("peach", 14)));
    // A long one is clipped on its row.
    let f = layout(&v, 34, 30, &with_bar(Bar { toast: Some(toast(Kind::Error, LONG)), ..Bar::default() }));
    assert!(texts(&f)[0].ends_with("… "), "{:?}", texts(&f)[0]);
    // Under 6 rows: no room for it.
    let f = layout(&v, 34, 5, &with_bar(Bar { toast: Some(toast(Kind::Info, "hello")), ..Bar::default() }));
    assert!(f.toast.is_empty() && f.toast_until.is_none() && !texts(&f).iter().any(|l| l.contains("hello")));
}

/// A toast lives until its deadline by the frame's clock, then the frame is
/// the one without it.
#[test]
fn a_toast_expires_by_the_frame_clock() {
    let v = lettered(quiet());
    let bar = Bar { toast: Some(toast(Kind::Ok, "parked")), ..demo_bar() };
    let at = |bar: &Bar, now: f64| layout(&v, 34, 40, &Opts { now: Some(now), ..with_bar(bar.clone()) });
    let t0 = NOW as f64;
    assert!(bottom_border(&at(&bar, t0 + 3.99)).contains("parked"));
    for now in [t0 + 4.0, t0 + 60.0] {
        let f = at(&bar, now);
        assert_eq!((f.card_footer.is_empty(), f.toast.is_empty(), f.toast_until), (true, true, None));
        assert_eq!(f, at(&demo_bar(), now));
    }
    // The same for one on its own row (it gives the row back).
    let bare = Bar { toast: Some(toast(Kind::Ok, "parked")), ..Bar::default() };
    assert_eq!(at(&bare, t0 + 4.0), at(&Bar::default(), t0 + 4.0));
}

/// The terminal's own background everywhere but the surfaces: the
/// client's agent, the NEEDS YOU card, the pills.
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
    let pills = (f.spaces.start + 1) as u16..f.spaces.end as u16;
    for y in 0..58u16 {
        for x in 0..34u16 {
            let bg = buf[(x, y)].bg;
            if y == here || y == here + 1 {
                assert_eq!(bg, palette::color("surface0"), "({x},{y})");
            } else if y == card || y == card + 1 {
                assert_eq!(bg, palette::mix("yellow", 14), "({x},{y})");
            } else if !pills.contains(&y) {
                assert_eq!(bg, Color::Reset, "({x},{y}) {:?}", f.rows[y as usize].text());
            }
        }
    }
    let ansi = to_ansi(buf);
    assert_eq!(ansi.lines().count(), 58);
    // No background code at all on an untinted row.
    assert!(ansi.starts_with("\x1b[0;38;2;205;214;244;1m agents"), "{:?}", &ansi[..60]);
    assert!(!ansi.lines().next().unwrap().contains(";48;"));
}
