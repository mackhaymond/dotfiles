//! The HARNESS MARK: every full (two-line) agent card wears its harness's
//! logo ([`crate::model::Harness`]) in its brand colour on the second line,
//! in the key-cap's 3 cells under the letter (the glyph and two
//! continuations, one ligature), on the card's own colour; folded and
//! one-line forms and shell rows have none.

use super::*;
use crate::model::Harness;

/// One agent per harness, in states that land each kind of card: a
/// working claude (the client's), codex working, opencode just done, abby
/// (`@agent_harness` over `@agent_kind` claude) asking (NEEDS YOU), grok
/// working, one with no kind (other), idle ones to fold, and two pinned
/// (grok failed, codex idle) with their ⌥W digits.
fn fleet() -> ViewModel {
    let v = view(
        vec![
            a("main", 1, "running", "Claude card").with("detail_kind", "run").with("detail", "Bash cargo test"),
            a("main", 2, "running", "Codex card").with("kind", "codex"),
            a("main", 3, "idle", "OpenCode card").with("kind", "opencode").with("detail_kind", "done").with("detail", "Synced 4"),
            a("main", 4, "needs-input", "Abby card").with("harness", "abby").with("detail_kind", "ask").with("detail", "Which deck?"),
            a("main", 5, "running", "Grok card").with("kind", "grok"),
            a("main", 6, "idle", "Mystery card").with("kind", ""),
            a("main", 7, "idle", "Idle one"),
            a("main", 8, "idle", "Idle two").with("kind", "codex"),
            a("main", 9, "failed", "Pinned grok").with("kind", "grok").with("pinned", "100").with("detail", "boom"),
            a("main", 10, "idle", "Pinned codex").with("kind", "codex").with("pinned", "200"),
        ],
        Some("@1"),
    );
    let mut v = lettered(v);
    let keys: HashMap<String, u8> = v.pinned_agents().iter().zip(1u8..).map(|(a, n)| (a.window_id.clone(), n)).collect();
    crate::view::Agent::set_pin_keys(&mut v, &keys);
    v
}

/// The segment drawn at cell `x` of `r` and where it starts.
fn seg_at(r: &Row, x: usize) -> Option<(usize, &Seg)> {
    let mut at = 0;
    for s in &r.segs {
        let w = dwidth(&s.text);
        if (at..at + w).contains(&x) {
            return Some((at, s));
        }
        at += w;
    }
    None
}

fn has_mark(r: &Row) -> bool {
    r.text().chars().any(|c| crate::text::wez::AGENT_MARKS.contains(&(c as u32)))
}

/// A row's harness glyphs (continuations not counted) and continuations.
fn mark_chars(r: &Row) -> (usize, usize) {
    let t = r.text();
    let cont = t.chars().filter(|&c| c == Harness::CONTINUATION).count();
    (t.chars().filter(|c| crate::text::wez::AGENT_MARKS.contains(&(*c as u32))).count() - cont, cont)
}

/// Every agent, by window id (NEEDS YOU's copies and the pins included).
fn harness_of(v: &ViewModel, win: &str) -> Harness {
    let all = v.agents().chain(&v.pinned).chain(v.needs.iter().map(|n| &n.agent));
    all.into_iter().find(|a| a.window_id == win).map(|a| a.harness).unwrap_or_else(|| panic!("no agent {win}"))
}

/// The invariant on one frame: a card's rows are the consecutive rows that
/// click to its window ([`Target::Go`]); in a two-row card the second row
/// carries exactly one mark in its harness's colour: with letters the 3-cell
/// ligature (glyph + two continuations, ONE segment: one fg, bg and set of
/// attributes) on exactly the cells of the first row's ` k ` cap; without,
/// the bare glyph a pad after the rail; on no background of its own (the
/// card's colour: one bg per card), the first row on the same colour; no
/// other row has a mark. Every row exactly `w` cells. Returns (two-row
/// cards, one-row cards).
fn check(v: &ViewModel, f: &Frame, w: u16, letters: bool, ctx: &str) -> (usize, usize) {
    let t: Vec<String> = f.rows.iter().map(Row::text).collect();
    for r in &f.rows {
        assert_eq!((dwidth(&r.text()), width(&r.segs)), (usize::from(w), usize::from(w)), "{ctx}: {t:#?}");
    }
    let go = |y: usize| match f.rows.get(y).and_then(|r| r.target_at(1)) {
        Some(Target::Go { win, .. }) => Some(win.clone()),
        _ => None,
    };
    // (The cap's first cell: past the margin, the rail and a pad.)
    let col = margin(usize::from(w)) + 2;
    let (mut two, mut one) = (0, 0);
    let mut y = 0;
    while y < f.rows.len() {
        let Some(win) = go(y) else {
            assert!(!has_mark(&f.rows[y]), "{ctx} {y}: {t:#?}");
            y += 1;
            continue;
        };
        if go(y + 1).as_ref() == Some(&win) {
            two += 1;
            let (r1, r2) = (&f.rows[y], &f.rows[y + 1]);
            let h = harness_of(v, &win);
            assert!(!has_mark(r1), "{ctx} {y}: {t:#?}");
            let (at, s) = seg_at(r2, col).unwrap_or_else(|| panic!("{ctx} {y}: no cell {col}"));
            let text = if letters { h.mark() } else { h.glyph().to_string() };
            let want = Seg { bg: None, ..seg(text, h.hue()) };
            // (A selected card re-inks everything crust: none here.)
            assert_eq!((at, s), (col, &want), "{ctx} {y} {win}: {:?}", r2.segs);
            assert_eq!(mark_chars(r2), (1, if letters { 2 } else { 0 }), "{ctx}");
            if letters {
                // Exactly under the cap: the same cells, its width.
                let (cx, cap) = seg_at(r1, col).unwrap();
                assert_eq!((cx, dwidth(&cap.text)), (col, BADGE_W), "{ctx} {y}: {:?}", r1.segs);
            }
            assert!(r1.bg.is_some() && r1.bg == r2.bg, "{ctx} {y}: {:?} {:?}", r1.bg, r2.bg);
            y += 2;
        } else {
            one += 1;
            assert!(!has_mark(&f.rows[y]), "{ctx} {y}: one-line {t:#?}");
            y += 1;
        }
    }
    (two, one)
}

/// Every harness's mark on its full cards, in AGENTS, PINNED and NEEDS
/// YOU, at every width and height; none on the one-line and folded forms
/// the shorter frames fall to; widths exact throughout.
#[test]
fn every_full_card_wears_its_harness_mark() {
    let v = fleet();
    let (mut seen, mut folded, mut oneline) = (std::collections::BTreeSet::new(), 0, 0);
    for w in [24u16, 30, 34, 44, 60] {
        for h in (8u16..=60).step_by(2) {
            let f = layout(&v, w, h, &Opts::default());
            let ctx = format!("{w}x{h} step {} agents {}", f.step, f.agents_step);
            let (two, one) = check(&v, &f, w, true, &ctx);
            if two > 0 {
                for sect in [f.needs.clone(), f.pinned.clone(), f.agents.clone()] {
                    for y in sect.filter(|&y| has_mark(&f.rows[y])) {
                        let r = &f.rows[y];
                        let c = r.text().chars().find(|c| crate::text::wez::AGENT_MARKS.contains(&(*c as u32))).unwrap();
                        assert_ne!(c, Harness::CONTINUATION);
                        seen.insert(c as u32);
                    }
                }
            }
            oneline += one;
            folded += usize::from(f.rows[f.agents.clone()].iter().any(|r| r.covers.len() > 1));
        }
    }
    // Every harness of the fleet showed, and the ladder reached the shorter
    // forms.
    let ids = ["claude", "codex", "opencode", "grok", "abby", "other"];
    let all: std::collections::BTreeSet<u32> = ids.iter().map(|id| Harness::named(id).glyph() as u32).collect();
    assert_eq!(seen, all);
    assert!(folded > 5 && oneline > 5, "{folded} {oneline}");
    // The mark sits where the letter is, one row down; the abby card (NEEDS
    // YOU, and its AGENTS copy) in abby's red, not claude's orange.
    let f = layout(&v, 34, 60, &Opts::default());
    let y = f.needs.clone().find(|&y| f.rows[y].text().contains("Abby card")).expect("abby asks");
    let x = margin(34) + 2;
    assert_eq!(seg_at(&f.rows[y + 1], x).unwrap(), (x, &seg(Harness::named("abby").mark(), "h_abby")));
    let letter = v.needs.iter().find(|n| n.agent.title == "Abby card").and_then(|n| n.agent.letter).expect("a letter");
    assert_eq!(seg_at(&f.rows[y], x).unwrap().1.text, format!(" {letter} "), "{:?}", f.rows[y].segs);
}

/// Every row of the table draws: a card per harness (by `@agent_kind`, and
/// one by `@agent_harness`), each with its own mark in its own colour; the
/// generic `agent` kind wears OTHER's.
#[test]
fn every_table_row_marks_its_card() {
    let ids: Vec<&str> = Harness::all().map(Harness::name).chain(["agent"]).collect();
    let mut ag: Vec<A> = ids.iter().enumerate().map(|(k, id)| a("main", k as u32 + 1, "running", id).with("kind", id)).collect();
    ag.push(a("main", 40, "running", "by-option").with("kind", "claude").with("harness", "deepseek"));
    let v = lettered(view(ag, None));
    let f = layout(&v, 44, 200, &Opts::default());
    let (two, _) = check(&v, &f, 44, true, "table");
    assert_eq!(two, ids.len() + 1, "{:#?}", texts(&f));
    for (title, h) in ids.iter().map(|id| (*id, Harness::named(id))).chain([("by-option", Harness::named("deepseek"))]) {
        let y = f.rows.iter().position(|r| r.text().contains(&format!("  {title} "))).unwrap_or_else(|| panic!("{title}"));
        assert_eq!(seg_at(&f.rows[y + 1], margin(44) + 2).unwrap().1, &seg(h.mark(), h.hue()), "{title}");
    }
    assert_eq!(Harness::named("agent"), Harness::OTHER);
}

/// No letter column (no letters anywhere): the card's 3-cell indent has no
/// room for the ligature and its pads, so the BARE glyph sits a pad after
/// the rail, the second line's text where it always was.
#[test]
fn without_letters_the_mark_follows_the_rail() {
    let mut v = fleet();
    for a in v.spaces.iter_mut().flat_map(|s| s.agents.iter_mut()).chain(v.pinned.iter_mut()).chain(v.needs.iter_mut().map(|n| &mut n.agent)) {
        a.letter = None;
    }
    for w in [24u16, 34, 44] {
        for h in [20u16, 40, 60] {
            let f = layout(&v, w, h, &Opts::default());
            let (two, _) = check(&v, &f, w, false, &format!("{w}x{h}"));
            // (Under 26 columns no card has a second line.)
            assert!(h < 40 || w < 26 || two > 3, "{w}x{h}");
        }
    }
}

/// Shells (their rows and their folded pills) never wear a mark; agents
/// with no `@agent_kind` wear the OTHER mark; a pending space's claude card
/// wears claude's.
#[test]
fn shells_and_pending_spaces() {
    let v = shelly();
    for w in [24u16, 34, 44] {
        for h in (10u16..=50).step_by(4) {
            let f = layout(&v, w, h, &Opts::default());
            check(&v, &f, w, true, &format!("shelly {w}x{h}"));
            for r in f.rows.iter().filter(|r| r.targets.iter().any(|t| matches!(t.2, Target::Shell { .. }))) {
                assert!(!has_mark(r), "{w}x{h}: {:?}", r.text());
            }
        }
    }
    let f = layout(&v, 34, 50, &Opts::default());
    let y = find(&f, "Tmux Agent");
    assert!(f.rows[y + 1].text().contains(Harness::OTHER.glyph()), "{:?}", f.rows[y + 1].text());
    let v = super::pending::pend("running", &[]);
    let f = layout(&v, 34, 50, &Opts::default());
    check(&v, &f, 34, true, "pending");
    let y = find(&f, "Rust CLI fizz");
    assert!(f.rows[y + 1].text().contains(Harness::named("claude").glyph()), "{:#?}", texts(&f));
}

/// A shell running a hookless agent (⌘E's launcher sets `@agent_harness`)
/// shows that harness's one-cell glyph in its colour where the green `❯`
/// goes: cursor-agent (`cursor`, an AgentMarks logo) does. aider has no logo
/// (its `aider` id is not in HARNESSES, so it is OTHER) and keeps the plain
/// prompt, as a plain shell does.
#[test]
fn a_hookless_agents_shell_wears_its_glyph() {
    assert_eq!(Harness::named("aider"), Harness::OTHER);
    let v = scoped(
        vec![
            a("main", 1, "running", "Busy"),
            a("main", 2, "", "cursor-agent").with("harness", "cursor"),
            a("main", 3, "", "zsh"),
            a("main", 4, "", "aider").with("harness", "aider"),
        ],
        "@1", "main", "on", "space", "",
    );
    let f = layout(&v, 34, 30, &Opts::default());
    let shell = |win: &str| f.rows.iter().find(|r| r.targets.iter().any(|t| t.2 == Target::Shell { win: win.into(), session: "main".into() })).unwrap();
    let cursor = Harness::named("cursor");
    let g = shell("@2").segs.iter().find(|s| s.text.starts_with(cursor.glyph())).unwrap_or_else(|| panic!("{:?}", shell("@2").text()));
    assert_eq!((g.text.clone(), g.fg), (format!("{} ", cursor.glyph()), Ink::Pal(cursor.hue())));
    assert!(!shell("@2").text().contains(PROMPT));
    for win in ["@3", "@4"] {
        assert!(shell(win).text().contains(PROMPT), "{win}: {:?}", shell(win).text());
    }
}

/// A selected card (prefix v) is solid mauve with crust ink through: the
/// mark too, one ink on one colour (the other harness's faint mark in the
/// softer crust, as all dim text there).
#[test]
fn a_selected_card_inks_its_mark_crust() {
    let v = shells_at("@5", "all", &[("@2", "stash_sel", "1")]);
    let f = layout(&v, 34, 50, &Opts::default());
    let y = find(&f, "Tmux Agent");
    let r = &f.rows[y + 1];
    let (_, s) = seg_at(r, margin(34) + 3).unwrap();
    let soft = over(Ink::Pal(SEL_INK), SEL_SOFT, Ink::Pal(SEL_BG));
    assert_eq!((s.text.clone(), s.fg, s.bg, r.bg), (Harness::OTHER.mark(), soft, None, Some(Ink::Pal(SEL_BG))));
    // A brand mark goes full crust.
    let mut v = v;
    for a in v.spaces.iter_mut().flat_map(|s| s.agents.iter_mut()).filter(|a| a.window_id == "@2") {
        a.harness = Harness::named("codex");
    }
    let f = layout(&v, 34, 50, &Opts::default());
    let (_, s) = seg_at(&f.rows[y + 1], margin(34) + 3).unwrap();
    assert_eq!((s.text.clone(), s.fg), (Harness::named("codex").mark(), Ink::Pal(SEL_INK)));
}

/// A text render of the fleet (run with `--nocapture`): as drawn (the mark
/// the AgentMarks code points), then with each 3-cell mark as `[C]` (its
/// harness's initial in brackets), so it reads without the font.
#[test]
fn render_the_marks() {
    let v = fleet();
    // (34x24: the idle agents folded, no marks there.)
    for (w, h) in [(34u16, 44u16), (44, 44), (34, 24)] {
        let f = layout(&v, w, h, &Opts::default());
        println!("--- {w}x{h}, as drawn:");
        for r in &f.rows {
            println!("|{}|", r.text());
        }
        println!("--- {w}x{h}, marks as [initial] (C claude, X codex, O opencode, G grok, A abby, ? other):");
        for r in &f.rows {
            println!("|{}|", initials(&r.text()));
        }
    }
}
