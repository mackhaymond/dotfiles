//! The stale-agent sweep ([`Agent::stale`], docs/agent-roster.md "Stale
//! agents"): which agents it picks, and the quiet hint it puts on their
//! card's second line (`close?`, `unsaved work`), at every width.

use super::*;
use crate::insight::{DiffStat, Insight, Kind};
use crate::view::{stale_after, Stale, STALE_AFTER_DEFAULT};
use std::collections::BTreeMap;

const H: i64 = 3600;

/// An insight with a last prompt, `diff` (added, removed, files) and
/// whether git has answered for it (`known`).
fn ins(diff: Option<(u64, u64, usize)>, known: bool) -> Insight {
    Insight {
        kind: Kind::Claude,
        transcript: "/nonexistent/t.jsonl".into(),
        doing: Some("fix the parser".into()),
        todo: None,
        prompt: Some("fix the parser".into()),
        ctx_used: None,
        ctx_window: crate::insight::WINDOW_1M,
        diff: diff.map(|(added, removed, files)| DiffStat { added, removed, files }),
        diff_known: known,
        dirty_files: Vec::new(),
        touched: diff.map_or(0, |d| d.2),
    }
}

/// A row's words, single-spaced, past its margin and rail.
fn words(l: &str) -> String {
    body(l).split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Agent @1 in state `st` since `ago` seconds.
fn aged<'a>(st: &'a str, ago: i64, title: &'a str) -> A<'a> {
    a("main", 1, st, title).with("since", &format!("{} {st}", NOW - ago))
}

/// `x` alone (@1), lettered, its insight `i` filled in.
fn one(x: A, i: Option<Insight>) -> ViewModel {
    let mut v = lettered(view(vec![x], None));
    let map: BTreeMap<String, Insight> = i.into_iter().map(|i| ("@1".to_string(), i)).collect();
    crate::insight::fill(&mut v, &map);
    v
}

/// The sweep's verdict on `x` with insight `i`, threshold `after`.
fn verdict(x: A, i: Option<Insight>, after: i64) -> Option<Stale> {
    let v = one(x, i);
    let a = v.agents().chain(&v.pinned).find(|a| a.window_id == "@1").expect("agent @1");
    a.stale(NOW as f64, after)
}

/// Idle or done for the threshold (or more), on a KNOWN diff: clean says
/// close, dirty says unsaved; under the threshold, nothing.
#[test]
fn idle_long_enough_is_stale() {
    let d = STALE_AFTER_DEFAULT;
    let clean = || Some(ins(None, true));
    let dirty = || Some(ins(Some((48, 12, 3)), true));
    assert_eq!(d, 3 * H);
    assert_eq!(verdict(aged("idle", 4 * H, "Jacket"), clean(), d), Some(Stale::Close));
    assert_eq!(verdict(aged("idle", 4 * H, "Jacket"), dirty(), d), Some(Stale::Unsaved));
    assert_eq!(verdict(aged("done", 4 * H, "Jacket").with("detail_kind", "done").with("detail", "ok"), clean(), d), Some(Stale::Close));
    // The threshold itself counts; a second short of it does not.
    assert_eq!(verdict(aged("idle", 3 * H, "Jacket"), clean(), d), Some(Stale::Close));
    assert_eq!(verdict(aged("idle", 3 * H - 1, "Jacket"), dirty(), d), None);
    // No state stamp (or junk): no age, nothing.
    assert_eq!(verdict(a("main", 1, "idle", "Jacket").with("since", ""), clean(), d), None);
    assert_eq!(verdict(a("main", 1, "idle", "Jacket").with("since", "abc idle"), clean(), d), None);
}

/// Only idle and done: never working, asking, failed or paused, never with
/// a workflow or cua flag in flight, never a prompt just answered.
#[test]
fn only_idle_and_done_are_swept() {
    let d = STALE_AFTER_DEFAULT;
    let old = 10 * H;
    let cases = || -> Vec<(&str, A)> { vec![
        ("running", aged("running", old, "Busy")),
        ("needs-input", aged("needs-input", old, "Asks").with("detail_kind", "ask").with("detail", "Which?")),
        ("failed", aged("failed", old, "Broke").with("detail_kind", "fail").with("detail", "boom")),
        ("paused", aged("idle", old, "Paused").with("paused", "123")),
        ("workflow", aged("idle", old, "Fleet").with("workflow", "1")),
        ("cua", aged("idle", old, "Clicks").with("cua", "1")),
        ("done + workflow", aged("done", old, "Fleet").with("workflow", "1")),
        ("answered", aged("idle", old, "Answered").with("pending", &(NOW - 5).to_string())),
    ] };
    for i in [ins(None, true), ins(Some((1, 0, 1)), true)] {
        for (name, x) in cases() {
            assert_eq!(verdict(x, Some(i.clone()), d), None, "{name}");
        }
    }
    // (A stale answered stamp is no answer: swept as idle.)
    let x = aged("idle", old, "Answered").with("pending", &(NOW - 2 * crate::tmux::ANSWERED_TTL).to_string());
    assert_eq!(verdict(x, Some(ins(None, true)), d), Some(Stale::Close));
}

/// No insight, or one git has not answered for yet: nothing, never a guess.
#[test]
fn an_unknown_diff_says_nothing() {
    let d = STALE_AFTER_DEFAULT;
    assert_eq!(verdict(aged("idle", 5 * H, "Jacket"), None, d), None);
    assert_eq!(verdict(aged("idle", 5 * H, "Jacket"), Some(ins(None, false)), d), None);
    assert_eq!(verdict(aged("idle", 5 * H, "Jacket"), Some(ins(Some((3, 0, 1)), false)), d), None);
    // An OpenCode agent has no transcript, so no insight: nothing.
    assert_eq!(verdict(aged("idle", 5 * H, "Code").with("kind", "opencode"), None, d), None);
}

/// `@agent_stale_after` overrides the 3 hours (digits only; `0` turns the
/// sweep off; junk is the default).
#[test]
fn the_option_overrides_the_threshold() {
    let clean = || Some(ins(None, true));
    assert_eq!(verdict(aged("idle", 15 * 60, "Jacket"), clean(), STALE_AFTER_DEFAULT), None);
    assert_eq!(verdict(aged("idle", 15 * 60, "Jacket"), clean(), 600), Some(Stale::Close));
    assert_eq!(verdict(aged("idle", 100 * H, "Jacket"), clean(), 0), None);
    let cases = [("", STALE_AFTER_DEFAULT), ("600", 600), (" 7200\n", 7200), ("0", 0), ("3h", STALE_AFTER_DEFAULT),
        ("-5", STALE_AFTER_DEFAULT), ("1.5", STALE_AFTER_DEFAULT), ("99999999999999999999999", STALE_AFTER_DEFAULT)];
    for (raw, want) in cases {
        assert_eq!(stale_after(raw), want, "{raw:?}");
    }
    // Through the layout: the bar's value, else the default.
    let v = one(aged("idle", 15 * 60, "Jacket"), clean());
    let line = |bar: Bar| {
        let f = layout(&v, 44, 30, &Opts { bar, ..Opts::default() });
        words(&f.rows[find(&f, "Jacket") + 1].text())
    };
    assert_eq!(line(Bar::default()), "> fix the parser 15m");
    assert_eq!(line(Bar { stale_after: Some(600), ..Bar::default() }), "> fix the parser close? · 15m");
    let v = one(aged("idle", 5 * H, "Jacket"), clean());
    let f = layout(&v, 44, 30, &Opts { bar: Bar { stale_after: Some(0), ..Bar::default() }, ..Opts::default() });
    assert_eq!(words(&f.rows[find(&f, "Jacket") + 1].text()), "> fix the parser 5h");
}

/// Pinned means kept: never offered a close; its unsaved work still shows.
#[test]
fn pinned_agents_keep_only_the_unsaved_flag() {
    let d = STALE_AFTER_DEFAULT;
    let pinned = || aged("idle", 5 * H, "Jacket").with("pinned", "100");
    assert_eq!(verdict(pinned(), Some(ins(None, true)), d), None);
    assert_eq!(verdict(pinned(), Some(ins(Some((5, 0, 1)), true)), d), Some(Stale::Unsaved));
    // On its PINNED card: the hint, then the space tag, then the age.
    let v = one(pinned(), Some(ins(Some((5, 0, 1)), true)));
    let f = layout(&v, 60, 30, &Opts::default());
    let y = f.pinned.clone().find(|&y| f.rows[y].text().contains("Jacket")).expect("pinned card") + 1;
    assert_eq!(words(&f.rows[y].text()), "> fix the parser +5 · 1f unsaved work · main · 5h", "{:#?}", texts(&f));
}

/// The second line of agent @1's card at `w` columns: its words and row.
fn second_at(v: &ViewModel, w: u16) -> (String, Row) {
    let f = layout(v, w, 30, &Opts::default());
    let t = texts(&f);
    assert!(t.iter().all(|l| dwidth(l) == usize::from(w)), "{w}: {t:#?}");
    let y = find(&f, "Jacket") + 1;
    (words(&f.rows[y].text()), f.rows[y].clone())
}

/// The hint sits after the badge, before the age, dim: `close?` in the
/// placeholder's ink, `unsaved work` dim peach over the card; it shortens,
/// then goes, before the text keeps under [`DOING_MIN`] cells; a one-line
/// card never has it.
#[test]
fn the_hint_on_the_card_at_every_width() {
    let close = one(aged("idle", 4 * H, "Jacket"), Some(ins(None, true)));
    let unsaved = one(aged("idle", 4 * H, "Jacket"), Some(ins(Some((48, 12, 3)), true)));
    let (l, r) = second_at(&close, 44);
    assert_eq!(l, "> fix the parser close? · 4h");
    assert!(r.segs.contains(&seg("close?", PLACEHOLDER)), "{:?}", r.segs);
    let (l, r) = second_at(&unsaved, 60);
    assert_eq!(l, "> fix the parser +48 −12 · 3f unsaved work · 4h");
    let bg = r.bg.expect("a card");
    assert!(r.segs.contains(&seg("unsaved work", over(Ink::Pal("peach"), DIFF_TINT, bg))), "{:?}", r.segs);
    // Both rows one background: nothing in the hint has its own.
    assert!(r.segs.iter().all(|s| s.bg.is_none() || s.bg == Some(Ink::Canvas)), "{:?}", r.segs);
    // Narrower: the count goes, the hint shortens, the badge goes, the
    // hint's ` ·` goes (Mack's 34 columns still say it), then the hint.
    let want: [(u16, &str, &str); 8] = [
        (60, "> fix the parser close? · 4h", "> fix the parser +48 −12 · 3f unsaved work · 4h"),
        (51, "> fix the parser close? · 4h", "> fix the parser +48 −12 unsaved work · 4h"),
        (46, "> fix the parser close? · 4h", "> fix the parser +48 −12 unsaved · 4h"),
        (38, "> fix the parser close? · 4h", "> fix the parser unsaved · 4h"),
        (36, "> fix the pars… close? · 4h", "> fix the par… unsaved · 4h"),
        (34, "> fix the pars… close? 4h", "> fix the par… unsaved 4h"),
        (32, "> fix the parser 4h", "> fix the parser 4h"),
        // (Under 30 columns no age; under 26 no second line.)
        (28, "> fix the parser", "> fix the parser"),
    ];
    for (w, c, u) in want {
        assert_eq!(second_at(&close, w).0, c, "close at {w}");
        assert_eq!(second_at(&unsaved, w).0, u, "unsaved at {w}");
    }
    // Wherever the hint shows, the text keeps its DOING_MIN cells (or its whole).
    for v in [&close, &unsaved] {
        for w in 26u16..=70 {
            let (_, r) = second_at(v, w);
            if r.segs.iter().any(|s| s.text == "close?" || s.text.starts_with("unsaved")) {
                let text = r.segs.iter().find(|s| s.text.starts_with("fix")).map_or(0, |s| dwidth(&s.text));
                assert!(text >= DOING_MIN, "{w}: {:?}", r.text());
            }
        }
    }
    // A one-line card (no second line): no hint, at any width.
    let a1 = close.agents().find(|a| a.window_id == "@1").unwrap();
    for w in 8usize..=60 {
        let cx = Cx { w, b: Badge::of(&close, &Bar::default()), now: close.now, spin: 0, home: None, cur: None, shells: true, resumes: false, stale_after: STALE_AFTER_DEFAULT };
        let rows = agent_rows(a1, cx, false, 0, Ink::Pal("peach"), None);
        assert!(rows.len() == 1 && !rows[0].text().contains("close?"), "{w}: {:?}", rows[0].text());
        assert_eq!(dwidth(&rows[0].text()), w);
    }
}
