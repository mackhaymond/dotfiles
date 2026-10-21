//! A permission prompt's NEEDS YOU card: the `y allow` / `n deny` chips on
//! its second line, their clicks, and the cards that get none.

use super::*;

/// A Claude perm prompt (@1), an asking agent (@2), a Codex perm prompt
/// (@3), lettered.
fn perms() -> ViewModel {
    lettered(view(
        vec![
            a("main", 1, "needs-input", "proj/Fix the parser").with("detail_kind", "perm")
                .with("detail", "Bash touch probe.txt"),
            a("main", 2, "needs-input", "Kua Yu focus timing").with("detail_kind", "ask").with("detail", "Which deck?"),
            a("main", 3, "needs-input", "Codex thing").with("detail_kind", "perm").with("detail", "Bash ls")
                .with("kind", "codex"),
        ],
        None,
    ))
}

/// The row of the NEEDS YOU card whose title holds `needle`.
fn need_row(f: &Frame, needle: &str) -> usize {
    f.needs.clone().find(|&y| f.rows[y].text().contains(needle)).unwrap_or_else(|| panic!("{needle:?} not in {:#?}", texts(f)))
}

/// @1's chip: it names the prompt the card shows (its `@agent_since`, 90 s
/// before NOW, and its detail), so a click answers that prompt only.
fn answer(win: &str, allow: bool) -> Option<Target> {
    let prompt = format!("{} Bash touch probe.txt", NOW - 90);
    Some(Target::Answer { win: win.into(), session: "main".into(), allow, prompt })
}

/// Wide: the caps and their words on line 2, right-aligned, the detail
/// clipped before them; the age moves up beside `◉ perm`. Every cell of a
/// chip (cap and word) answers; the rest of the card still goes there. Two
/// rows, one background: the words sit on the card, only the caps are solid.
#[test]
fn perm_cards_carry_answer_chips() {
    let v = perms();
    let go = Some(Target::Go { win: "@1".into(), session: "main".into() });
    for w in [44u16, 60] {
        let f = layout(&v, w, 40, &Opts::default());
        let t = texts(&f);
        let y = need_row(&f, "Fix the");
        assert!(t[y].trim_end().ends_with("◉ perm 1m"), "{w}: {t:#?}");
        let l2 = &t[y + 1];
        assert!(l2.starts_with(" ▌     perm Bash") && l2.ends_with(" y  allow   n  deny  "), "{w}: {l2:?}");
        assert!(t.iter().all(|l| dwidth(l) == usize::from(w)), "{w}: {t:#?}");
        let (yx, nx) = (col(l2, " y "), col(l2, " n "));
        for x in yx..yx + 9 {
            assert_eq!(f.target(x, y as u16 + 1).cloned(), answer("@1", true), "{w} x={x}");
        }
        for x in nx..nx + 8 {
            assert_eq!(f.target(x, y as u16 + 1).cloned(), answer("@1", false), "{w} x={x}");
        }
        assert_eq!(f.target(yx - 1, y as u16 + 1).cloned(), go);
        assert_eq!(f.target(nx - 1, y as u16 + 1).cloned(), go);
        assert_eq!(f.target(w - 1, y as u16 + 1).cloned(), go);
        assert_eq!(f.target(yx, y as u16).cloned(), go);
        // One card colour, both rows; the caps solid, their words on it.
        let r = &f.rows[y + 1];
        assert_eq!(r.bg, f.rows[y].bg);
        assert!(r.segs.contains(&block(" y ", ink_on(Ink::Pal("green")), "green")) && r.segs.contains(&seg(" allow", "text")));
        assert!(r.segs.contains(&block(" n ", ink_on(Ink::Pal("red")), "red")) && r.segs.contains(&seg(" deny", "text")));
        // Exactly two rows: the next card is the next agent's.
        assert_ne!(f.target(2, y as u16 + 2).cloned(), go);
    }
}

/// Mack's usual 34 columns: the caps alone (` y   n `), the detail keeps
/// its room; still clickable, still two rows.
#[test]
fn narrow_perm_cards_keep_the_caps() {
    let v = perms();
    let f = layout(&v, 34, 40, &Opts::default());
    let t = texts(&f);
    let y = need_row(&f, "Fix the");
    let l2 = &t[y + 1];
    assert!(l2.ends_with(" y   n   ") && !l2.contains("allow") && l2.contains("perm Bash touch"), "{l2:?}");
    assert_eq!(dwidth(l2), 34);
    let (yx, nx) = (col(l2, " y "), col(l2, " n "));
    assert_eq!((f.target(yx, y as u16 + 1).cloned(), f.target(yx + 2, y as u16 + 1).cloned()), (answer("@1", true), answer("@1", true)));
    assert_eq!((f.target(nx, y as u16 + 1).cloned(), f.target(nx + 2, y as u16 + 1).cloned()), (answer("@1", false), answer("@1", false)));
    assert_eq!(f.target(yx + 3, y as u16 + 1).cloned(), Some(Target::Go { win: "@1".into(), session: "main".into() }));
    // Under 26 columns there are no second lines, so no chips.
    let f = layout(&v, 24, 40, &Opts::default());
    assert!(!f.rows.iter().any(|r| r.targets.iter().any(|(_, _, t)| matches!(t, Target::Answer { .. }))));
}

/// An asking agent and a Codex perm prompt get no chips (their line 2 as
/// before, the age at its end); nor does a one-line card in a short frame.
#[test]
fn only_claude_perm_prompts_get_chips() {
    let v = perms();
    let f = layout(&v, 44, 40, &Opts::default());
    let t = texts(&f);
    for title in ["Kua Yu", "Codex thing"] {
        let y = need_row(&f, title);
        assert!(!t[y + 1].contains(" y ") && t[y + 1].trim_end().ends_with('m'), "{title}: {:?}", t[y + 1]);
    }
    let answers = |f: &Frame| f.rows.iter().flat_map(|r| &r.targets).filter(|(_, _, t)| matches!(t, Target::Answer { .. })).count();
    assert_eq!(answers(&f), 2);
    // Squeezed to one-line cards: none.
    let mut many = perms();
    many.needs = (0..20).map(|_| many.needs[0].clone()).collect();
    let f = layout(&many, 44, 30, &Opts::default());
    assert_eq!(answers(&f), 0, "{:#?}", texts(&f));
}
