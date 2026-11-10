//! BARE spaces: in all scope every space gets its group, even one with
//! nothing but its home (its header alone); short of rows, those headers
//! are the first thing the agents section gives up.

use super::*;

/// main (⌘1): one working agent and two idle (they fold); work (⌘3): one
/// working; four bare spaces, nothing but their homes: b1 (⌘2), b2 (⌘4),
/// b3 (⌘5) and b4 (⌘6), the client on b4's home. `@spaces` on, `scope` the
/// client's `@strip_scope`.
fn bare_fleet(scope: &str) -> ViewModel {
    let ag = vec![
        a("main", 1, "running", "Main working").with("space_index", "1"),
        a("main", 2, "idle", "Main idle one").with("space_index", "1"),
        a("main", 3, "idle", "Main idle two").with("space_index", "1"),
        a("work", 1, "running", "Work working").with("space_index", "3"),
        a("b1", 0, "", "zsh").with("home", "1").with("space_index", "2"),
        a("b2", 0, "", "zsh").with("home", "1").with("space_index", "4"),
        a("b3", 0, "", "zsh").with("home", "1").with("space_index", "5"),
        a("b4", 0, "", "zsh").with("home", "1").with("space_index", "6"),
    ];
    scoped(ag, "@8", "b4", "on", scope, "")
}

const BARE: [&str; 4] = ["b1", "b2", "b3", "b4"];

fn cx_of(v: &ViewModel, w: usize) -> Cx<'_> {
    Cx { w, b: Badge::of(v, &Bar::default()), now: v.now, spin: 0, home: home_of(v), cur: v.cur_win.as_deref(), shells: v.shells, resumes: false, stale_after: crate::view::STALE_AFTER_DEFAULT, fold_after: crate::view::FOLD_AFTER_DEFAULT }
}

/// The space headers among `rows`, by name.
fn names(rows: &[Row], w: usize) -> Vec<String> {
    rows.iter()
        .filter(|r| is_group_header(r, w))
        .filter_map(|r| match &r.targets[0].2 {
            Target::Session(s) => Some(s.clone()),
            _ => None,
        })
        .collect()
}

/// All scope: every space a group in the ⌘N order, a bare one its header
/// with nothing under it (the next header, or a gap, right after), and no
/// words under AGENTS.
#[test]
fn all_scope_lists_bare_spaces() {
    let v = bare_fleet("all");
    assert_eq!(v.scope.session, None);
    let f = layout(&v, 34, 60, &Opts::default());
    let t = texts(&f);
    let order: Vec<String> = heads(&f).into_iter().map(|(_, s)| s).collect();
    assert_eq!(order, ["main", "b1", "work", "b2", "b3", "b4"], "{t:#?}");
    for name in BARE {
        let y = head(&f, name);
        let next = f.rows.get(y + 1);
        assert!(next.is_none_or(|r| is_blank(r) || is_group_header(r, 34) || y + 1 == f.agents.end), "{name}: {t:#?}");
    }
    assert!(!t.iter().any(|l| l.contains("no agents") || l.contains("all pinned") || l.contains("empty space")), "{t:#?}");
    check_accounting("bare", &v, &f);
    check_order("bare", &f);
}

/// A bare space's header is a header like any: the whole row goes to the
/// space (its home), and while the client is on that home it wears the
/// client's lift; the others their plain look.
#[test]
fn a_bare_header_goes_home() {
    let v = bare_fleet("all");
    let f = layout(&v, 34, 60, &Opts::default());
    let t = texts(&f);
    for name in BARE {
        let y = head(&f, name);
        assert!(is_group_header(&f.rows[y], 34), "{name}: {t:#?}");
        for x in [0u16, 5, 20, 33] {
            assert_eq!(f.target(x, y as u16), Some(&Target::Session(name.into())), "{name} {x}: {t:#?}");
        }
    }
    let mine = head(&f, "b4");
    assert_eq!(f.rows[mine].bg, Some(Ink::Pal(MINE_BG)), "{t:#?}");
    assert!(f.rows[mine].segs.iter().any(|s| s.text == MINE), "{:?}", f.rows[mine].segs);
    for name in ["b1", "b2", "b3"] {
        assert_ne!(f.rows[head(&f, name)].bg, Some(Ink::Pal(MINE_BG)), "{name}: {t:#?}");
    }
}

/// The words under AGENTS: only where they say something the headers do
/// not. Bare headers alone (all scope): none. Not one space to list: `no
/// agents running`. Space scope on a bare space: its header and `no agents
/// in this space`, as ever.
#[test]
fn the_empty_words_only_without_headers() {
    let words = |t: &[String]| t.iter().any(|l| l.contains("no agents") || l.contains("all pinned"));
    // Nothing anywhere but two homes.
    let ag = vec![a("b1", 0, "", "zsh").with("home", "1"), a("b2", 0, "", "zsh").with("home", "1")];
    let v = scoped(ag, "@1", "b1", "on", "all", "");
    let f = layout(&v, 34, 40, &Opts::default());
    let t = texts(&f);
    assert_eq!(heads(&f).into_iter().map(|(_, s)| s).collect::<Vec<_>>(), ["b1", "b2"], "{t:#?}");
    assert!(!words(&t), "{t:#?}");
    // Not one space: the words.
    let v = scoped(vec![], "@900", "agents", "on", "all", "");
    assert!(v.spaces.is_empty(), "{:?}", v.spaces);
    let t = texts(&layout(&v, 34, 40, &Opts::default()));
    assert!(t.iter().any(|l| l.starts_with("   no agents running")), "{t:#?}");
    // Space scope on a bare space: its header and why it is empty.
    let v = bare_fleet("space");
    assert_eq!(v.scope.session.as_deref(), Some("b4"));
    let f = layout(&v, 34, 40, &Opts::default());
    let b = agents_part(&f);
    assert!(heads(&f) == [(f.agents.start + 1, "b4".to_string())] && b[2].starts_with("   no agents in this space"), "{b:#?}");
}

/// Short of rows the bare headers go first, before any agent loses a line:
/// from the end of the list, the client's own last, the ones gone counted
/// on one ` … N empty spaces` line at the section's end; then that line
/// too, still at full detail. Every later step (fold, one line, collapse,
/// cut) lists only the spaces with something in them.
#[test]
fn bare_headers_go_first() {
    let v = bare_fleet("all");
    let w = 34;
    let cx = cx_of(&v, w);
    let full = agents_section(&v, cx, usize::MAX, false, true).0;
    assert_eq!(names(&full, w), ["main", "b1", "work", "b2", "b3", "b4"]);
    // The order they go in: b3, b2, b1 (from the end), the client's b4 last.
    let gone_order = ["b3", "b2", "b1", "b4"];
    let mut seen = (false, false);
    let mut last = BARE.len();
    for budget in (1..=full.len()).rev() {
        let (rows, step) = agents_section(&v, cx, budget, false, true);
        let t: Vec<String> = rows.iter().map(Row::text).collect();
        assert!(rows.len() <= budget, "{budget}: {t:#?}");
        let shown: Vec<String> = names(&rows, w).into_iter().filter(|s| BARE.contains(&s.as_str())).collect();
        let line = t.iter().position(|l| l.contains("empty space"));
        // Never back once gone.
        assert!(shown.len() <= last, "{budget}: {t:#?}");
        last = shown.len();
        if step >= 1 {
            assert!(shown.is_empty() && line.is_none(), "{budget} step {step}: {t:#?}");
            continue;
        }
        // Full detail: every agent its card, two lines (a card's second row
        // stands for no one; nothing folded).
        let cards = rows.iter().filter(|r| r.covers.len() == 1).count();
        assert_eq!(cards, 4, "{budget}: {t:#?}");
        let k = BARE.len() - shown.len();
        let mut want: Vec<&str> = BARE.iter().copied().filter(|n| !gone_order[..k].contains(n)).collect();
        want.sort_by_key(|n| BARE.iter().position(|b| b == n));
        assert_eq!(shown, want, "{budget}: {t:#?}");
        match line {
            Some(y) => {
                seen.0 = true;
                assert!(k >= 2 && y == rows.len() - 1 && t[y].starts_with(&format!(" … {k} empty spaces")), "{budget}: {t:#?}");
                assert!(rows[y].targets.is_empty() && rows[y].covers.is_empty(), "{budget}: {t:#?}");
            }
            None if k == BARE.len() => seen.1 = true,
            None => assert_eq!(k, 0, "{budget}: {t:#?}"),
        }
    }
    assert!(seen.0 && seen.1, "{seen:?}");
    // The one-line shape NEEDS YOU is weighed against has none of them.
    let one = agents_section(&v, cx, agents_one_line(&v, cx), false, true);
    assert!(one.1 <= 2 && names(&one.0, w) == ["main", "work"], "{one:?}");
    assert_eq!(agents_one_line(&v, cx), 1 + (1 + 3) + (1 + 1));
    // Whole frames: the same, every agent accounted for at every height.
    let mut thinned = false;
    for h in 4u16..=50 {
        let f = layout(&v, 34, h, &Opts::default());
        let t = texts(&f);
        assert_eq!(f.rows.len(), usize::from(h));
        check_accounting(&format!("bare {h}"), &v, &f);
        check_order(&format!("bare {h}"), &f);
        let bare = heads(&f).iter().filter(|(_, s)| BARE.contains(&s.as_str())).count();
        if f.agents_step >= 1 {
            assert!(bare == 0 && !t.iter().any(|l| l.contains("empty space")), "{h}: {t:#?}");
        } else if bare < BARE.len() {
            thinned = true;
        }
    }
    assert!(thinned);
}

/// The bare headers make way before the dock's list and before a NEEDS
/// YOU card's second line (a perm prompt's answer chips): main (⌘1) a
/// Claude perm prompt and a working agent, five or six bare spaces, all
/// scope (six: more than [`SPACES_LISTED`], whose dock reserve is its
/// collapsed form). While any bare header stands, the frame is at a step
/// that keeps the dock's list, every space a row, and two-line NEEDS YOU
/// cards, chips and all; and some height keeps the chips with the bare
/// headers thinned.
#[test]
fn bare_headers_go_before_the_chips_and_the_dock() {
    let chips = |f: &Frame| f.rows[f.needs.clone()].iter().any(|r| r.targets.iter().any(|t| matches!(t.2, Target::Answer { .. })));
    let names = ["c1", "c2", "c3", "c4", "c5", "c6"];
    for n in [5, 6] {
        let mut ag = vec![
            a("main", 1, "needs-input", "proj/Fix the parser").with("detail_kind", "perm")
                .with("detail", "Bash touch probe.txt").with("space_index", "1"),
            a("main", 2, "running", "Main working").with("space_index", "1"),
        ];
        let bares = &names[..n];
        ag.extend(bares.iter().enumerate().map(|(k, s)| a(s, 0, "", "zsh").with("home", "1").with("space_index", ["2", "3", "4", "5", "6", "7"][k])));
        let v = scoped(ag, "@2", "main", "on", "all", "");
        let mut thinned = false;
        for h in 6u16..=50 {
            let f = layout(&v, 34, h, &Opts::default());
            let t = texts(&f);
            let bare = heads(&f).iter().filter(|(_, s)| bares.contains(&s.as_str())).count();
            if bare > 0 {
                assert!(f.step <= 1 && chips(&f), "{n} {h}: step {} {t:#?}", f.step);
                assert_eq!(f.spaces.len(), space_list(&v).len(), "{n} {h}: {t:#?}");
                assert!(!t.iter().any(|l| l.contains("more spaces")), "{n} {h}: {t:#?}");
            }
            thinned |= chips(&f) && bare < n && f.agents_step == 0;
            check_accounting(&format!("chips {n} {h}"), &v, &f);
            check_order(&format!("chips {n} {h}"), &f);
        }
        assert!(thinned, "{n}");
        // The review's heights: the chips stand, the bare headers going.
        for h in [18u16, 22] {
            let f = layout(&v, 34, h, &Opts::default());
            let bare = heads(&f).iter().filter(|(_, s)| bares.contains(&s.as_str())).count();
            assert!(chips(&f) && bare < n, "{n} {h}: {:#?}", texts(&f));
        }
    }
}

/// Every bare header thinned away with nothing else to list: the words
/// are back under AGENTS (`no agents running`; `all pinned above` when
/// every agent is pinned), never a bare AGENTS header.
#[test]
fn the_words_come_back_when_every_bare_header_goes() {
    let homes = |names: &[&'static str]| names.iter().map(|s| a(s, 0, "", "zsh").with("home", "1")).collect::<Vec<_>>();
    let v = scoped(homes(&["b1", "b2", "b3", "b4"]), "@1", "b1", "on", "all", "");
    let cx = cx_of(&v, 34);
    assert_eq!(agents_one_line(&v, cx), 2);
    let t: Vec<String> = agents_section(&v, cx, 2, false, true).0.iter().map(Row::text).collect();
    assert!(t.len() == 2 && t[0].starts_with(" AGENTS") && t[1].starts_with("   no agents running"), "{t:#?}");
    // Whole frames: wherever no bare header is left, the words are there.
    let mut seen = false;
    for h in 4u16..=30 {
        let f = layout(&v, 34, h, &Opts::default());
        let b = agents_part(&f);
        assert!(!b.iter().any(|l| l.contains("… 0")), "{h}: {:#?}", texts(&f));
        if heads(&f).is_empty() && b.len() >= 2 && !b.iter().any(|l| l.contains("empty space")) {
            assert!(b.iter().any(|l| l.starts_with("   no agents running")), "{h}: {:#?}", texts(&f));
            seen = true;
        }
    }
    assert!(seen);
    // Every agent pinned: `all pinned above` once the headers have gone.
    let mut ag = vec![a("main", 1, "idle", "One").with("pinned", "5"), a("bai", 1, "idle", "Two").with("pinned", "6")];
    ag.extend(homes(&["b1", "b2"]));
    let v = scoped(ag, "@1", "main", "on", "all", "");
    let split = v.pins_split();
    let cx = cx_of(&split, 34);
    let full = agents_section(&split, cx, usize::MAX, false, true).0;
    assert_eq!(names(&full, 34), ["b1", "b2", "bai", "main"]);
    let mut seen = false;
    for budget in 1..full.len() {
        // (Step 5 is the pins' own cut: the header alone under every pin.)
        let (rows, step) = agents_section(&split, cx, budget, false, true);
        let t: Vec<String> = rows.iter().map(Row::text).collect();
        if step <= 3 && names(&rows, 34).is_empty() {
            assert!(t.last().is_some_and(|l| l.starts_with("   all pinned above")), "{budget}: {t:#?}");
            seen = true;
        }
    }
    assert!(seen);
}

/// Space scope is as it was: the client's space alone, at every height; no
/// bare header of another space, no `empty spaces` line.
#[test]
fn space_scope_lists_no_bare_spaces() {
    let mut ag = vec![
        a("main", 1, "running", "Main working").with("space_index", "1"),
        a("main", 2, "idle", "Main idle one").with("space_index", "1"),
    ];
    ag.extend(["b1", "b2"].map(|s| a(s, 0, "", "zsh").with("home", "1")));
    let v = scoped(ag, "@1", "main", "on", "space", "");
    assert_eq!(v.scope.session.as_deref(), Some("main"));
    for h in 4u16..=40 {
        let f = layout(&v, 34, h, &Opts::default());
        let t = texts(&f);
        assert!(heads(&f).iter().all(|(_, s)| s == "main"), "{h}: {t:#?}");
        assert!(!t.iter().any(|l| l.contains("empty space")), "{h}: {t:#?}");
    }
    let f = layout(&v, 34, 40, &Opts::default());
    assert_eq!(heads(&f).len(), 1, "{:#?}", texts(&f));
}
