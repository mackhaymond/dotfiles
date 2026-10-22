//! What the insight ([`crate::insight`]) puts on a card: the doing line,
//! the diff badge (a click into the diff), never the context; and how they
//! give way at narrow widths.

use super::*;
use crate::insight::{DiffStat, Insight, Kind};
use std::collections::BTreeMap;

/// An insight: its todo and prompt, its diff (added, removed, files), the
/// tokens in a 1M context.
fn ins(todo: Option<&str>, prompt: Option<&str>, diff: Option<(u64, u64, usize)>, used: Option<u64>) -> Insight {
    Insight {
        kind: Kind::Claude,
        transcript: "/nonexistent/t.jsonl".into(),
        doing: todo.or(prompt).map(String::from),
        todo: todo.map(String::from),
        prompt: prompt.map(String::from),
        ctx_used: used,
        ctx_window: crate::insight::WINDOW_1M,
        diff: diff.map(|(added, removed, files)| DiffStat { added, removed, files }),
        dirty_files: Vec::new(),
        touched: diff.map_or(0, |d| d.2),
    }
}

/// `v` with the insights `map` (window id → insight) filled in.
fn with(mut v: ViewModel, map: Vec<(&str, Insight)>) -> ViewModel {
    let map: BTreeMap<String, Insight> = map.into_iter().map(|(w, i)| (w.to_string(), i)).collect();
    crate::insight::fill(&mut v, &map);
    v
}

/// One agent alone (@1), lettered, with `i` and a live `blurb`: the body of
/// its card's second line at 44 columns, and that row.
fn second(x: A, i: Option<Insight>, blurb: Option<&str>) -> (String, Row) {
    let mut v = lettered(view(vec![x], None));
    v.spaces.iter_mut().flat_map(|s| s.agents.iter_mut()).for_each(|a| a.blurb = blurb.map(String::from));
    let v = with(v, i.into_iter().map(|i| ("@1", i)).collect());
    let f = layout(&v, 44, 40, &Opts::default());
    let y = f.agents.clone().find(|&y| f.rows[y].covers.len() == 1).expect("a card") + 1;
    (words(&f.rows[y].text()), f.rows[y].clone())
}

/// A row's words, single-spaced, past its margin and rail.
fn words(l: &str) -> String {
    body(l).split_whitespace().collect::<Vec<_>>().join(" ")
}

const TODO: &str = "Wiring the badge";
const PROMPT: &str = "fix the parser";

/// Row 2's precedence: an asking agent's question; a working agent's
/// in-progress todo, then its live activity (both after a blue `›`), then
/// its run detail, then your last prompt (after a faint `> `); a done
/// agent's outcome; an idle agent's old outcome, then your last prompt;
/// the placeholders only when there is nothing at all.
#[test]
fn the_second_line_says_what_it_does() {
    let run = || a("main", 1, "running", "Sidebar").with("detail_kind", "run").with("detail", "Edit layout.rs");
    let all = Some(ins(Some(TODO), Some(PROMPT), None, None));
    let cases: Vec<(&str, A, Option<Insight>, Option<&str>, &str)> = vec![
        ("todo first", run(), all.clone(), Some("Running cargo test"), "› Wiring the badge"),
        ("then live", run(), Some(ins(None, Some(PROMPT), None, None)), Some("Running cargo test"), "› Running cargo test"),
        ("then the run detail", run(), Some(ins(None, Some(PROMPT), None, None)), None, "Edit layout.rs"),
        ("then the prompt", a("main", 1, "running", "Sidebar"), Some(ins(None, Some(PROMPT), None, None)), None, "> fix the parser"),
        ("nothing working", a("main", 1, "running", "Sidebar"), None, None, "no activity yet"),
        ("done", a("main", 1, "idle", "Jobs").with("detail_kind", "done").with("detail", "7 fixes applied"), all.clone(), None,
            "7 fixes applied"),
        ("old outcome", a("main", 12, "idle", "Jobs").with("detail_kind", "done").with("detail", "7 fixes applied"), all.clone(), None,
            "7 fixes applied"),
        ("idle prompt", a("main", 1, "idle", "Jacket"), all.clone(), None, "> fix the parser"),
        ("idle nothing", a("main", 1, "idle", "Jacket"), None, None, "no reply yet"),
        ("asks", a("main", 1, "needs-input", "Deck").with("detail_kind", "ask").with("detail", "Which deck?"), all.clone(), None,
            "Which deck?"),
    ];
    for (name, x, i, blurb, want) in cases {
        let (l2, r) = second(x, i, blurb);
        assert!(l2.starts_with(want), "{name}: {l2:?}");
        if want.starts_with('›') {
            assert!(r.segs.contains(&seg("› ", "blue")), "{name}: {:?}", r.segs);
        }
        if want.starts_with('>') {
            assert!(r.segs.contains(&seg(PROMPT_MARK, PLACEHOLDER)) && r.segs.contains(&seg(PROMPT, "overlay2")), "{name}: {:?}", r.segs);
        }
    }
}

/// The diff badge on row 2 before the age: `+48` dim green and `−12` dim
/// red (over the card, [`DIFF_TINT`]), ` · 3f` dim; nothing when clean.
/// Both rows one background: nothing in the badge has its own.
#[test]
fn the_badge_is_dim_green_and_red_on_the_card() {
    let x = a("main", 1, "running", "Sidebar").with("detail_kind", "run").with("detail", "Edit layout.rs");
    let (l2, r) = second(x, Some(ins(Some(TODO), None, Some((48, 12, 3)), Some(850_000))), None);
    assert_eq!(l2, "› Wiring the badge +48 −12 · 3f 1m");
    let bg = r.bg.expect("a card");
    assert!(r.segs.contains(&seg("+48", over(Ink::Pal("green"), DIFF_TINT, bg))), "{:?}", r.segs);
    assert!(r.segs.contains(&seg("−12", over(Ink::Pal("red"), DIFF_TINT, bg))), "{:?}", r.segs);
    assert!(r.segs.contains(&seg(" · 3f", "overlay2")), "{:?}", r.segs);
    assert!(r.segs.iter().all(|s| s.bg.is_none() || s.bg == Some(Ink::Canvas)), "{:?}", r.segs);
    // Clean: none.
    let (l2, _) = second(a("main", 1, "idle", "Jacket"), Some(ins(None, Some(PROMPT), None, Some(400_000))), None);
    assert_eq!(l2, "> fix the parser 1m");
    // Thousands, compact.
    let (l2, _) = second(a("main", 1, "idle", "Jacket"), Some(ins(None, Some("go"), Some((12_345, 1_234, 20)), None)), None);
    assert!(l2.ends_with("+12k −1.2k · 20f 1m"), "{l2:?}");
}

/// No context meter, however full (Mack: "no need for the context %"): a
/// nearly full agent's card, one line or two, at any width, never says
/// how full it is; its row 2 is the prompt and the age alone.
#[test]
fn the_context_is_never_drawn() {
    let v = with(
        lettered(view(vec![a("main", 1, "idle", "Jacket"), a("main", 2, "running", "Sidebar")], None)),
        vec![("@1", ins(None, Some("go"), None, Some(950_000))), ("@2", ins(Some(TODO), None, Some((48, 12, 3)), Some(1_000_000)))],
    );
    let f = layout(&v, 44, 30, &Opts::default());
    assert_eq!(words(&f.rows[find(&f, "Jacket") + 1].text()), "> go 1m", "{:#?}", texts(&f));
    for w in 20u16..=60 {
        for h in [8u16, 30] {
            let f = layout(&v, w, h, &Opts::default());
            assert!(texts(&f).iter().all(|l| !l.contains('%') && !l.contains('█')), "{w}x{h}: {:#?}", texts(&f));
        }
    }
}

/// Narrow: the file count goes first (34 columns keep the badge), then the
/// badge, before the doing text has under [`DOING_MIN`] cells past its mark
/// (or its whole). Every row exactly the frame's width.
#[test]
fn the_extras_give_way_before_the_doing_text() {
    let x = || a("main", 1, "running", "Sidebar").with("detail_kind", "run").with("detail", "Edit layout.rs");
    let i = ins(Some("Porting the whole parser to the new grammar"), None, Some((48, 12, 3)), Some(850_000));
    let v = with(lettered(view(vec![x()], None)), vec![("@1", i)]);
    for w in 26u16..=60 {
        let f = layout(&v, w, 40, &Opts::default());
        let t = texts(&f);
        assert!(t.iter().all(|l| dwidth(l) == usize::from(w)), "{w}: {t:#?}");
        let y = find(&f, "Sidebar") + 1;
        let r = &f.rows[y];
        let doing = r.segs.iter().find(|s| s.text.starts_with("Porting")).map_or(0, |s| dwidth(&s.text));
        let l = r.text();
        if l.contains('+') {
            assert!(doing >= DOING_MIN, "{w}: {l:?}");
        }
        // Dropped in order: a count only with a badge.
        assert!(!l.contains(" · 3f") || l.contains("+48"), "{w}: {l:?}");
        match w {
            44 => assert!(l.contains("+48 −12 · 3f 1m"), "{l:?}"),
            34 => assert!(l.contains("+48 −12 1m") && !l.contains("3f"), "{l:?}"),
            _ => {}
        }
    }
    // A short line keeps its whole: the badge stays.
    let v = with(lettered(view(vec![x()], None)), vec![("@1", ins(Some("Tests"), None, Some((48, 12, 3)), None))]);
    let f = layout(&v, 30, 40, &Opts::default());
    assert_eq!(words(&f.rows[find(&f, "Sidebar") + 1].text()), "› Tests +48 −12 1m", "{:#?}", texts(&f));
}

/// The badge's cells open that agent's diff; the rest of the card still
/// goes there. On a two-line card, a one-line one, and a NEEDS YOU card.
#[test]
fn the_badge_clicks_into_the_diff() {
    let v = with(
        lettered(view(
            vec![
                a("main", 1, "running", "Sidebar").with("detail_kind", "run").with("detail", "Edit layout.rs"),
                a("main", 2, "needs-input", "Deck").with("detail_kind", "ask").with("detail", "Which?"),
            ],
            None,
        )),
        vec![("@1", ins(None, None, Some((48, 12, 3)), None)), ("@2", ins(None, None, Some((2, 1, 1)), None))],
    );
    let diff = |w: &str| Some(Target::Diff(w.into()));
    let go = |w: &str| Some(Target::Go { win: w.into(), session: "main".into() });
    for width in [44u16, 34] {
        let f = layout(&v, width, 40, &Opts::default());
        let y = f.agents.clone().find(|&y| f.rows[y].text().contains("Sidebar")).unwrap() + 1;
        let l = f.rows[y].text();
        let (x0, x1) = (col(&l, "+48"), col(&l, "−12") + 3);
        for x in x0..x1 {
            assert_eq!(f.target(x, y as u16).cloned(), diff("@1"), "{width} x={x} {l:?}");
        }
        assert_eq!(f.target(x0 - 1, y as u16).cloned(), go("@1"));
        assert_eq!(f.target(col(&l, "1m"), y as u16).cloned(), go("@1"));
        assert_eq!(f.target(x0, y as u16 - 1).cloned(), go("@1"));
        // NEEDS YOU: its reason, then the badge before the age.
        let n = f.needs.clone().find(|&y| f.rows[y].text().contains("Deck")).unwrap() + 1;
        let l = f.rows[n].text();
        assert!(body(&l).starts_with("Which?") && l.contains("+2 −1"), "{width}: {l:?}");
        assert_eq!(f.target(col(&l, "+2"), n as u16).cloned(), diff("@2"));
        assert_eq!(f.target(col(&l, "Which"), n as u16).cloned(), go("@2"));
    }
    // A one-line card: the badge before the word, where the title keeps room.
    let a1 = v.agents().find(|a| a.window_id == "@1").unwrap();
    let cx = Cx { w: 44, b: Badge::of(&v, &Bar::default()), now: v.now, spin: 0, home: None, cur: None, shells: true, resumes: false };
    let rows = agent_rows(a1, cx, false, 0, Ink::Pal("peach"), None);
    let l = rows[0].text();
    assert!(rows.len() == 1 && l.contains("Sidebar") && l.contains("+48 −12 ") && col(&l, "+48") < col(&l, "working"), "{l:?}");
    assert!(l.contains("f ⠋ working"), "{l:?}");
    assert_eq!(rows[0].target_at(col(&l, "+48")).cloned(), diff("@1"));
    assert_eq!(rows[0].target_at(col(&l, "Sidebar")).cloned(), go("@1"));
    // At 34 the title wins: no badge.
    let rows = agent_rows(a1, Cx { w: 34, ..cx }, false, 0, Ink::Pal("peach"), None);
    assert!(!rows[0].text().contains('+'), "{:?}", rows[0].text());
}

/// A PINNED card keeps its space tag, the file count given up first, then
/// the badge, then the tag; every card stays two rows on one background,
/// the zebra intact.
#[test]
fn pinned_cards_and_the_card_invariants() {
    let v = with(
        lettered(view(
            vec![
                a("main", 1, "running", "Sidebar").with("pinned", "100").with("detail_kind", "run").with("detail", "Edit"),
                a("main", 2, "idle", "Jacket"),
                a("main", 3, "idle", "Jobs").with("detail_kind", "done").with("detail", "7 fixes applied"),
                a("main", 4, "idle", "Clean"),
            ],
            None,
        )),
        vec![
            ("@1", ins(Some(TODO), None, Some((48, 12, 3)), Some(850_000))),
            ("@2", ins(None, Some(PROMPT), Some((5, 0, 1)), Some(600_000))),
            ("@3", ins(None, Some(PROMPT), None, None)),
        ],
    );
    let f = layout(&v, 44, 40, &Opts::default());
    let y = f.pinned.clone().find(|&y| f.rows[y].text().contains("Sidebar")).unwrap() + 1;
    assert_eq!(words(&f.rows[y].text()), "› Wiring the bad… +48 −12 main · 1m", "{:#?}", texts(&f));
    // Wider, the file count too; narrower, the tag alone.
    let f = layout(&v, 50, 40, &Opts::default());
    let y = f.pinned.clone().find(|&y| f.rows[y].text().contains("Sidebar")).unwrap() + 1;
    assert_eq!(words(&f.rows[y].text()), "› Wiring the badge +48 −12 · 3f main · 1m", "{:#?}", texts(&f));
    let f = layout(&v, 38, 40, &Opts::default());
    let y = f.pinned.clone().find(|&y| f.rows[y].text().contains("Sidebar")).unwrap() + 1;
    assert_eq!(words(&f.rows[y].text()), "› Wiring the badge main · 1m", "{:#?}", texts(&f));
    for w in [34u16, 44, 60] {
        let f = layout(&v, w, 40, &Opts::default());
        let cards: Vec<usize> = f.agents.clone().filter(|&y| f.rows[y].covers.len() == 1 && !is_group_header(&f.rows[y], usize::from(w))).collect();
        assert_eq!(cards.len(), 4, "{w}: {:#?}", texts(&f));
        for y in cards {
            let (r1, r2) = (&f.rows[y], &f.rows[y + 1]);
            assert!(r2.covers.is_empty() && r1.bg == r2.bg, "{w}: {:?}", r2.text());
            assert!(r2.segs.iter().all(|s| s.bg.is_none() || s.bg == Some(Ink::Canvas)), "{w}: {:?}", r2.segs);
            // The next row is another card's (another shade) or not a card.
            if let Some(n) = f.rows.get(y + 2).filter(|n| n.covers.len() == 1) {
                assert_ne!(n.bg, r1.bg, "{w}");
            }
        }
    }
}

/// A one-line card whose title is empty (a bare `~/` summary) never takes a
/// right side wider than the tile, at any width; nor does any row.
#[test]
fn an_empty_title_never_overflows() {
    let v = with(lettered(view(vec![a("main", 1, "running", "~/")], None)), vec![("@1", ins(Some(TODO), None, Some((48, 12, 3)), Some(950_000)))]);
    let a1 = v.agents().find(|a| a.window_id == "@1").unwrap();
    for w in 8usize..=44 {
        let cx = Cx { w, b: Badge::of(&v, &Bar::default()), now: v.now, spin: 0, home: None, cur: None, shells: true, resumes: false };
        for two in [false, true] {
            for r in agent_rows(a1, cx, two, 0, Ink::Pal("peach"), None) {
                assert_eq!(dwidth(&r.text()), w, "{w} {two}: {:?}", r.text());
                assert!(r.targets.iter().all(|(x0, x1, _)| x0 < x1 && usize::from(*x1) <= w), "{w}: {:?}", r.targets);
            }
        }
        let f = layout(&v, w as u16, 30, &Opts::default());
        assert!(texts(&f).iter().all(|l| dwidth(l) == w), "{w}: {:#?}", texts(&f));
    }
}

/// Line counts compact without a `10.0k`.
#[test]
fn line_counts_are_compact() {
    let got: Vec<String> = [0, 999, 1000, 1234, 9949, 9950, 9999, 10_000, 12_345].into_iter().map(lines_n).collect();
    assert_eq!(got, ["0", "999", "1.0k", "1.2k", "9.9k", "10k", "10k", "10k", "12k"]);
}

/// A todo's whitespace runs read as one space.
#[test]
fn doing_text_is_one_line() {
    let (l2, _) = second(a("main", 1, "running", "Sidebar"), Some(ins(Some("Port\n  the\tparser"), None, None, None)), None);
    assert_eq!(l2, "› Port the parser 1m");
}
