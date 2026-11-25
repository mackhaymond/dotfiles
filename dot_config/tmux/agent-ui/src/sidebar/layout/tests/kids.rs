//! The KIDS: delegated children (external subagents of any harness:
//! agent-spawn's, abby-delegate's) behave like native subagents of their
//! parent. The shell reconciler's derived options say who is whose
//! (`@agent_child` on a child, `@agent_kids` / `@agent_kids_ask` on its
//! root); a child gets no card, NEEDS YOU entry, pin, letter or count of its
//! own. FOLDED (the default) its whole family is one chip on the root's card
//! (`↳ 4 · 3 working`); a click on the chip OPENS it (a live row per child,
//! Design A; one strip row short of rows, Design B). An asking child
//! surfaces on its root's NEEDS YOU card, which acts on the child.

use super::*;

/// main: @1 the abby parent (`parent` state, 3 children, 2 running, `ask`
/// its `@agent_kids_ask` when given), its children @2 running, @3 done
/// (pinned, `pin_kid`), @4 asking a perm; @5 a plain working agent (the
/// client's), @6 a plain asking agent with no `@agent_child`.
fn family(parent: &'static str, ask: Option<&str>, pin_kid: bool) -> Vec<A<'static>> {
    let mut p = a("main", 1, parent, "Abby orchestrator").with("harness", "abby").with("kids", "3 2");
    if parent == "needs-input" {
        p = p.with("detail_kind", "ask").with("detail", "Which deck?");
    }
    if let Some(k) = ask {
        p = p.with("kids_ask", k);
    }
    let mut docs = a("main", 3, "done", "Docs kid").with("child", "@1").with("detail_kind", "done").with("detail", "wrote it");
    if pin_kid {
        docs = docs.with("pinned", "100");
    }
    vec![
        p,
        a("main", 2, "running", "Parser kid").with("child", "@1").with("detail_kind", "run").with("detail", "Bash cargo test"),
        docs,
        a("main", 4, "needs-input", "Tests kid").with("child", "@1").with("detail_kind", "perm").with("detail", "Bash cargo test"),
        a("main", 5, "running", "Plain agent"),
        a("main", 6, "needs-input", "Plain asker").with("detail_kind", "ask").with("detail", "Ship it?"),
    ]
}

fn fam(parent: &'static str, ask: Option<&str>) -> ViewModel {
    lettered(view(family(parent, ask, false), Some("@5")))
}

const ASK: &str = "@4\tneeds-input\tBash cargo test";

/// The plan's family (§5), agent-spawn's: @1 an abby parent HELD for its
/// children (its Stop held `running` with the workflow's gear, its own
/// reply a done detail); @2 a claude child running `Bash cargo test`, task
/// "Parser rewrite"; @3 a codex child done, its result `result` (`ready`,
/// `read`), task "Docs pass"; @4 a claude child asking a perm, task "Tests
/// for parser"; @5 a grandchild under @2, running, task "Fixture gen". With
/// `plain`, @6 a plain working agent (the client's) and @7 a plain asker;
/// else the client is on the parent.
fn spawned(result: &str, plain: bool) -> Vec<A<'static>> {
    let mut v = vec![
        a("main", 1, "running", "Abby orchestrator").with("harness", "abby").with("kids", "4 3").with("workflow", "1")
            .with("detail_kind", "done").with("detail", "Spawned three subagents"),
        a("main", 2, "running", "Parser kid").with("child", "@1").with("spawn_id", "s-a").with("spawn_task", "Parser rewrite")
            .with("detail_kind", "run").with("detail", "Bash cargo test"),
        a("main", 3, "idle", "Docs kid").with("child", "@1").with("spawn_id", "s-b").with("spawn_task", "Docs pass")
            .with("kind", "codex").with("detail_kind", "done").with("detail", "wrote it").with("spawn_result", result),
        a("main", 4, "needs-input", "Tests kid").with("child", "@1").with("spawn_id", "s-c").with("spawn_task", "Tests for parser")
            .with("detail_kind", "perm").with("detail", "Bash cargo test"),
        a("main", 5, "running", "Grand kid").with("child", "@2").with("spawn_id", "s-d").with("spawn_task", "Fixture gen"),
    ];
    if plain {
        v.push(a("main", 6, "running", "Plain agent"));
        v.push(a("main", 7, "needs-input", "Plain asker").with("detail_kind", "ask").with("detail", "Ship it?"));
    }
    v
}

fn spawned_view(result: &str, plain: bool) -> ViewModel {
    lettered(view(spawned(result, plain), Some(if plain { "@6" } else { "@1" })))
}

/// The family of @1 open (a click on its chip).
fn open(ids: &[&str]) -> Opts {
    Opts { kids_open: ids.iter().map(|s| s.to_string()).collect(), ..Opts::default() }
}

/// The kids' tasks in family order, and their windows.
const TASKS: [(&str, &str); 4] = [("Parser rewrite", "@2"), ("Fixture gen", "@5"), ("Docs pass", "@3"), ("Tests for parser", "@4")];

/// The windows of the NEEDS YOU entries of their OWN (no stand-ins: see
/// [`stand_ins`]), sorted (the order is agent-jump's, by stamp).
fn needs_ids(v: &ViewModel) -> Vec<&str> {
    let mut ids: Vec<&str> = v.needs.iter().filter(|n| n.via.is_none()).map(|n| n.agent.window_id.as_str()).collect();
    ids.sort();
    ids
}

/// The stand-in entry for child `kid`.
fn need<'a>(v: &'a ViewModel, kid: &str) -> &'a crate::view::Need {
    v.needs.iter().find(|n| n.via.as_ref().is_some_and(|c| c.window_id == kid)).unwrap_or_else(|| panic!("no stand-in for {kid}"))
}

/// The stand-in entries (`via` set) of `v`, by their child's window, sorted.
fn stand_ins(v: &ViewModel) -> Vec<&str> {
    let mut ids: Vec<&str> = v.needs.iter().filter_map(|n| n.via.as_ref()).map(|c| c.window_id.as_str()).collect();
    ids.sort();
    ids
}

/// The windows a row's click targets name ([`Target::Go`] / [`Target::Answer`]).
fn wins(r: &Row) -> Vec<&str> {
    r.targets
        .iter()
        .filter_map(|(_, _, t)| match t {
            Target::Go { win, .. } | Target::Answer { win, .. } => Some(win.as_str()),
            _ => None,
        })
        .collect()
}

/// The row of the parent's card (its first row) in AGENTS. (Its title may
/// be clipped for the family chip.)
fn parent_row(f: &Frame, t: &[String]) -> usize {
    f.agents.clone().find(|&y| t[y].contains("Abby")).unwrap_or_else(|| panic!("the parent's card: {t:#?}"))
}

/// A strip row ([`kid_strip`]): it folds a family (a [`Target::Kids`]) and
/// stands for no one (a chip's row covers its card).
fn is_strip(r: &Row) -> bool {
    r.covers.is_empty() && r.targets.iter().any(|(_, _, t)| matches!(t, Target::Kids(_)))
}

/// A child has no card, no NEEDS YOU entry and no letter, whatever its
/// state (@2 running, @3 done, @4 asking), and counts nowhere; folded (the
/// default) it has no row at all: its parent's first row stands for it.
#[test]
fn a_child_has_no_card_and_no_need() {
    let v = fam("idle", None);
    assert_eq!(needs_ids(&v), ["@6"], "only the plain asker");
    // (The asking child's entry is its parent's stand-in, `@agent_kids_ask` or not.)
    assert_eq!(stand_ins(&v), ["@4"]);
    assert!(v.needs.iter().all(|n| !["@2", "@3", "@4"].contains(&n.agent.window_id.as_str())));
    for kid in ["@2", "@3", "@4"] {
        assert_eq!(v.agents().find(|a| a.window_id == kid).and_then(|a| a.letter), None, "{kid} holds no letter");
    }
    // The counts: the parent (working: its children run), the two plain ones.
    let main = v.spaces.iter().find(|s| s.name == "main").unwrap();
    assert_eq!(main.counts.nonzero(), vec![(Cat::NeedsInput, 1), (Cat::Working, 2)]);
    assert_eq!(v.counts, main.counts);
    for (w, h) in [(42u16, 60u16), (34, 58), (24, 40)] {
        let f = layout(&v, w, h, &Opts::default());
        let t = texts(&f);
        for r in &f.rows {
            assert_eq!(dwidth(&r.text()), usize::from(w), "{t:#?}");
        }
        for title in ["Parser kid", "Docs kid", "Tests kid"] {
            assert!(!t[f.agents.clone()].iter().any(|l| l.contains(title)), "{title} drawn while folded {w}x{h}: {t:#?}");
        }
        // The parent's first row stands for it and its children.
        let p = parent_row(&f, &t);
        let covered: Vec<&str> = f.rows[p].covers.iter().map(|(_, w)| w.as_str()).collect();
        assert_eq!(covered, ["@1", "@2", "@3", "@4"], "{w}x{h}");
        // In NEEDS YOU only the asking one, as its parent's stand-in.
        for y in f.needs.clone().filter(|&y| wins(&f.rows[y]).iter().any(|x| ["@2", "@3", "@4"].contains(x))) {
            assert_eq!(wins(&f.rows[y]).first().copied(), Some("@4"), "{w}x{h}: {t:#?}");
            let card = if t[y].contains("Abby") { y } else { y - 1 };
            assert!(t[card].contains("Abby"), "{w}x{h}: {t:#?}");
        }
    }
}

/// FOLDED, at 44 columns (the plan's family: three children and a
/// grandchild): ONE card. Its first row carries the family chip ` ↳ 4 · 3
/// working` in teal (no gear: the busy children are what the watcher's gear
/// says) and the state's spinner; its second line what the busiest child
/// does (`› Bash cargo test`); no child row under it. A click on the chip
/// opens the family ([`Target::Kids`]); anywhere else on the card goes to
/// the parent.
#[test]
fn folded_family_is_one_card_with_a_chip() {
    let v = spawned_view("ready", true);
    let f = layout(&v, 44, 60, &Opts::default());
    let t = texts(&f);
    let p = parent_row(&f, &t);
    assert!(t[p].contains("Abby orchestrator ↳ 4 · 3 working"), "{:?}", t[p]);
    assert!(!t[p].contains('⚙'), "no gear while children are busy: {:?}", t[p]);
    assert!(f.rows[p].segs.iter().any(|s| s.text == " ↳ 4 · 3 working" && s.fg == Ink::Pal("teal") && !s.bold), "{:?}", f.rows[p].segs);
    assert!(t[p + 1].contains("› Bash cargo test"), "the busiest child's doing: {:?}", t[p + 1]);
    // Then the next card: no child rows, no strip.
    assert!(t[p + 2].contains("Plain agent"), "{t:#?}");
    for (task, _) in TASKS {
        assert!(!t.iter().any(|l| l.contains(task)), "{task}: {t:#?}");
    }
    assert!(!f.rows.iter().any(is_strip));
    let covered: Vec<&str> = f.rows[p].covers.iter().map(|(_, w)| w.as_str()).collect();
    assert_eq!(covered, ["@1", "@2", "@3", "@4", "@5"]);
    // The chip's cells open the family; the title's go to the parent.
    let x = col(&t[p], "↳");
    assert_eq!(f.target(x, p as u16), Some(&Target::Kids("@1".into())));
    assert_eq!(f.target(x + 8, p as u16), Some(&Target::Kids("@1".into())));
    assert_eq!(f.target(col(&t[p], "Abby"), p as u16), Some(&Target::Go { win: "@1".into(), session: "main".into() }));
    for r in &f.rows {
        assert_eq!(dwidth(&r.text()), 44, "{t:#?}");
    }
}

/// OPEN, at 44 columns (Design A): under the full card one live row per
/// child in FAMILY order (the grandchild right after its parent, indented 2
/// more cells): the child's cap (no letter; solid yellow while it asks),
/// `↳`, its harness mark, its TASK, its state and age on the right (the
/// spinner for the working ones, `✓` green while its result is ready and
/// dim once read, `◉ perm` for the asker); the title bright while working
/// or asking, dim once done. A click on a row goes to that child's pane;
/// the chip, bold now, folds it again.
#[test]
fn open_family_rows() {
    let v = spawned_view("ready", true);
    let f = layout(&v, 44, 60, &open(&["@1"]));
    let t = texts(&f);
    let p = parent_row(&f, &t);
    assert!(t[p].contains("↳ 4 · 3 working"), "{:?}", t[p]);
    assert!(f.rows[p].segs.iter().any(|s| s.text == " ↳ 4 · 3 working" && s.bold), "bold while open: {:?}", f.rows[p].segs);
    let card = &f.rows[p];
    for (k, (task, win)) in TASKS.into_iter().enumerate() {
        let y = p + 2 + k;
        let r = &f.rows[y];
        assert!(t[y].contains(task) && t[y].contains(KID_MARK), "{task} at {y}: {t:#?}");
        assert_eq!(r.bg, card.bg, "one colour per card");
        assert_eq!(r.segs[1], card.segs[1], "the card's rail");
        assert!(r.covers.is_empty(), "{task}");
        assert_eq!(f.target(col(&t[y], task), y as u16), Some(&Target::Go { win: win.into(), session: "main".into() }), "{task}");
        assert_eq!(dwidth(&r.text()), 44);
        // The harness mark, one cell, right after the arrow.
        let raw = r.text();
        let mark = raw.chars().skip_while(|&c| c != '↳').nth(2).unwrap();
        let want = if win == "@3" { crate::model::Harness::named("codex") } else { crate::model::Harness::named("claude") };
        assert_eq!(mark, want.glyph(), "{task}");
    }
    let row = |task: &str| p + 2 + TASKS.iter().position(|(x, _)| *x == task).unwrap();
    let seg_of = |y: usize, text: &str| f.rows[y].segs.iter().find(|s| s.text.contains(text)).cloned().unwrap_or_else(|| panic!("{text:?} in {:?}", f.rows[y].segs));
    // The grandchild sits 2 cells further in.
    assert_eq!(col(&t[row("Fixture gen")], "↳"), col(&t[row("Parser rewrite")], "↳") + 2, "{t:#?}");
    assert_eq!(col(&t[row("Docs pass")], "↳"), col(&t[row("Parser rewrite")], "↳"));
    // Working: the spinner and the age; the title bright.
    let y = row("Parser rewrite");
    assert!(t[y].trim_end().ends_with("⠋ 2m"), "{:?}", t[y]);
    assert_eq!(seg_of(y, "Parser rewrite").fg, Ink::Pal("text"));
    assert!(f.rows[y].anim);
    // Done, result ready: a green check; the title dim.
    let y = row("Docs pass");
    assert!(t[y].trim_end().ends_with("✓ 3m"), "{:?}", t[y]);
    assert_eq!(seg_of(y, "✓").fg, Ink::Pal("green"));
    assert_eq!(seg_of(y, "Docs pass").fg, Ink::Pal(dim(true)));
    // Asking: `◉ perm` in yellow, its cap solid yellow, the title bright.
    let y = row("Tests for parser");
    assert!(t[y].trim_end().ends_with("◉ perm"), "{:?}", t[y]);
    assert_eq!(seg_of(y, "◉ perm").fg, Ink::Pal("yellow"));
    assert_eq!(f.rows[y].segs[3].bg, Some(Ink::Pal("yellow")), "the cap: {:?}", f.rows[y].segs);
    assert_eq!(seg_of(y, "Tests for parser").fg, Ink::Pal("text"));
    // Then the next card.
    assert!(t[p + 6].contains("Plain agent"), "{t:#?}");
    // Read: the check dim.
    let v = spawned_view("read", true);
    let f = layout(&v, 44, 60, &open(&["@1"]));
    let t = texts(&f);
    let y = parent_row(&f, &t) + 4;
    assert!(t[y].contains("Docs pass"), "{t:#?}");
    assert_eq!(f.rows[y].segs.iter().find(|s| s.text == "✓").map(|s| s.fg), Some(Ink::Pal(dim(true))));
    // Another family open (or none): this one stays folded.
    let f = layout(&v, 44, 60, &open(&["@6"]));
    assert!(!texts(&f).iter().any(|l| l.contains("Docs pass")));
    // Open, but too narrow for a second line (24 columns): nothing drawn
    // under the card, so the chip keeps its folded look.
    let f = layout(&v, 24, 60, &open(&["@1"]));
    let t = texts(&f);
    let p = parent_row(&f, &t);
    let chip: Vec<&Seg> = f.rows[p].segs.iter().filter(|s| s.text.starts_with(" ↳")).collect();
    assert!(chip.len() == 1 && !chip[0].bold, "{:?}", f.rows[p].segs);
    assert!(!t.iter().any(|l| l.contains("Docs pass")) && !f.rows.iter().any(is_strip), "{t:#?}");
}

/// Readable renders of the plan's family at 44 columns, folded and open
/// (the AgentMarks glyphs as their harness's initial, [`initials`]).
#[test]
fn the_plans_family_at_44_columns() {
    let v = spawned_view("ready", true);
    let block = |o: &Opts| {
        let f = layout(&v, 44, 60, o);
        let t: Vec<String> = f.rows.iter().map(|r| initials(&r.text())).collect();
        let p = t.iter().position(|l| l.contains("Abby orchestrator ↳")).unwrap();
        let n = t[p..].iter().position(|l| l.contains("Plain agent")).unwrap();
        t[p..p + n].iter().map(|l| l.trim_end().to_string()).collect::<Vec<_>>()
    };
    // (A tile row's text up to its last cell: 42 cells, its right side flush.)
    let line = |l: &str, r: &str| format!("{l}{}{r}", " ".repeat(42 - l.chars().count() - r.chars().count()));
    assert_eq!(block(&Opts::default()), [
        " ▎  a  Abby orchestrator ↳ 4 · 3 working ⠋".to_string(),
        line(" ▎ [A] › Bash cargo test", "1m"),
    ]);
    assert_eq!(block(&open(&["@1"])), [
        " ▎  a  Abby orchestrator ↳ 4 · 3 working ⠋".to_string(),
        line(" ▎ [A] › Bash cargo test", "1m"),
        line(" ▎     ↳ C Parser rewrite", "⠋ 2m"),
        line(" ▎       ↳ C Fixture gen", "⠋ 5m"),
        line(" ▎     ↳ X Docs pass", "✓ 3m"),
        line(" ▎     ↳ C Tests for parser", "◉ perm"),
    ]);
}

/// Short of rows (44x11), an open family goes to ONE strip row under its
/// card (Design B): a pill a child in family order, its harness mark and
/// state, no letters; a click on a pill goes to that child, on the `↳` folds
/// the family, anywhere else to the parent. It never vanishes while the
/// card is drawn.
#[test]
fn a_short_frame_draws_the_strip() {
    let v = spawned_view("ready", false);
    let f = layout(&v, 44, 11, &open(&["@1"]));
    let t = texts(&f);
    let ys: Vec<usize> = (0..f.rows.len()).filter(|&y| is_strip(&f.rows[y])).collect();
    assert_eq!(ys.len(), 1, "one strip at 44x11: {t:#?}");
    let y = ys[0];
    assert!(f.agents.contains(&y), "{t:#?}");
    assert_eq!(y, parent_row(&f, &t) + 2, "under its card: {t:#?}");
    let raw = initials(&f.rows[y].text());
    assert!(raw.contains("↳  C ⠋  C ⠋  X ✓  C ◉ perm"), "{raw:?}");
    for (task, _) in TASKS {
        assert!(!t.iter().any(|l| l.contains(task)), "no kid rows: {t:#?}");
    }
    // Each pill goes to its child, in family order.
    let gos: Vec<&str> = f.rows[y].targets.iter().filter_map(|(_, _, t)| match t {
        Target::Go { win, .. } => Some(win.as_str()),
        _ => None,
    }).collect();
    assert_eq!(gos, ["@2", "@5", "@3", "@4", "@1"], "the pills, then the row's own");
    let x = col(&t[y], "◉");
    assert_eq!(f.target(x, y as u16), Some(&Target::Go { win: "@4".into(), session: "main".into() }));
    assert_eq!(f.target(col(&t[y], "↳"), y as u16), Some(&Target::Kids("@1".into())));
    assert_eq!(f.target(42, y as u16), Some(&Target::Go { win: "@1".into(), session: "main".into() }));
    assert_eq!(dwidth(&f.rows[y].text()), 44);
    // Folded, the same frame has no strip.
    assert!(!layout(&v, 44, 11, &Opts::default()).rows.iter().any(is_strip));
}

/// A parent whose children run reads as WORKING with the chip saying how
/// many run, whatever its own state says (idle here; a done turn is no
/// NEEDS YOU entry while they run). None running: its own state, the chip's
/// count alone.
#[test]
fn a_parent_with_running_kids_reads_working() {
    for parent in ["idle", "done"] {
        let v = fam(parent, None);
        let f = layout(&v, 44, 60, &Opts::default());
        let t = texts(&f);
        let y = parent_row(&f, &t);
        assert!(t[y].contains("Abby orchestrator ↳ 3 · 2 working"), "{parent}: {:?}", t[y]);
        assert!(!t[y].contains('⚙'), "{parent}: {:?}", t[y]);
        assert!(!needs_ids(&v).contains(&"@1"), "{parent}: a done turn with its kids running is no NEEDS YOU");
        // Its second line: what the running child does.
        assert!(t[y + 1].contains("› Bash cargo test"), "{parent}: {:?}", t[y + 1]);
    }
    let mut agents = family("idle", None, false);
    agents[0] = a("main", 1, "idle", "Abby orchestrator").with("harness", "abby").with("kids", "3 0");
    let v = lettered(view(agents, Some("@5")));
    let f = layout(&v, 44, 60, &Opts::default());
    let t = texts(&f);
    let y = parent_row(&f, &t);
    assert!(t[y].contains("Abby orchestrator ↳ 3") && !t[y].contains("working") && !t[y].contains('⚙'), "{:?}", t[y]);
    // (Narrow: the chip shortens before the title goes.)
    let f = layout(&fam("idle", None), 34, 60, &Opts::default());
    let t = texts(&f);
    let y = parent_row(&f, &t);
    assert!(t[y].contains("↳ 2/3") || t[y].contains("↳ 3"), "{:?}", t[y]);
}

/// A HELD parent with no child saying anything: `waiting on N subagents`.
#[test]
fn a_held_parent_waits_on_its_children() {
    let mut agents = spawned("ready", true);
    agents[1] = a("main", 2, "running", "Parser kid").with("child", "@1").with("spawn_task", "Parser rewrite");
    agents[4] = a("main", 5, "running", "Grand kid").with("child", "@2");
    let v = lettered(view(agents, Some("@6")));
    let f = layout(&v, 44, 60, &Opts::default());
    let t = texts(&f);
    let y = parent_row(&f, &t);
    assert!(t[y + 1].contains("waiting on 3 subagents"), "{:?}", t[y + 1]);
    // A parent mid-turn of its own (running, no held reply) never speaks
    // for its children: its own placeholder.
    let mut agents = spawned("ready", true);
    agents[0] = a("main", 1, "running", "Abby orchestrator").with("harness", "abby").with("kids", "4 3");
    let v = lettered(view(agents, Some("@6")));
    let f = layout(&v, 44, 60, &Opts::default());
    let t = texts(&f);
    let y = parent_row(&f, &t);
    assert!(t[y + 1].contains("no activity yet"), "{:?}", t[y + 1]);
    // A workflow with no child busy keeps its gear.
    let mut agents = family("idle", None, false);
    agents[0] = a("main", 1, "running", "Abby orchestrator").with("kids", "3 0").with("workflow", "1");
    let v = lettered(view(agents, Some("@5")));
    let f = layout(&v, 44, 60, &Opts::default());
    let t = texts(&f);
    assert!(t[parent_row(&f, &t)].contains('⚙'), "{t:#?}");
}

/// An asking child surfaces as its PARENT's NEEDS YOU card, with the
/// child's state, colour and words after `↳` (it has no letter: `↳ perm
/// Bash…`); the card, its answer chips and a jump from it all target the
/// CHILD's pane.
#[test]
fn an_asking_child_surfaces_on_its_parent() {
    let v = fam("idle", Some(ASK));
    assert_eq!((needs_ids(&v), stand_ins(&v)), (vec!["@6"], vec!["@4"]), "the parent, never the child");
    let n = need(&v, "@4");
    assert_eq!(n.agent.window_id, "@1");
    assert_eq!((n.agent.state.as_str(), n.agent.cat), ("needs-input", Some(Cat::NeedsInput)));
    assert_eq!(n.target().window_id, "@4");
    assert_eq!((n.reason_word.as_str(), n.reason.as_str()), ("perm", "Bash cargo test"));
    assert_eq!(n.via.as_ref().and_then(|c| c.letter), None, "a child holds no letter");
    for (w, h) in [(42u16, 60u16), (60, 60), (34, 58)] {
        let f = layout(&v, w, h, &Opts::default());
        let t = texts(&f);
        let y = f.needs.clone().find(|&y| t[y].contains("Abby orchestrator")).expect("the parent's NEEDS YOU card");
        assert!(t[y + 1].contains(&format!("{KID_MARK}perm")), "{w}: {:?}", t[y + 1]);
        for r in &f.rows[y..y + 2] {
            assert!(!r.targets.is_empty());
            assert!(wins(r).iter().all(|&x| x == "@4"), "{w}: {:?}", r.targets);
        }
        // A click on the card jumps to the child.
        assert_eq!(f.target(col(&t[y], "Abby"), y as u16), Some(&Target::Go { win: "@4".into(), session: "main".into() }));
        // The answer chips answer the child's prompt.
        let child = v.agents().find(|a| a.window_id == "@4").unwrap();
        let answers: Vec<&Target> =
            f.rows[y + 1].targets.iter().map(|(_, _, t)| t).filter(|t| matches!(t, Target::Answer { .. })).collect();
        assert_eq!(answers.len(), 2, "{w}: {:?}", f.rows[y + 1].targets);
        for a in answers {
            let Target::Answer { win, prompt, .. } = a else { unreachable!() };
            assert_eq!((win.as_str(), prompt.as_str()), ("@4", crate::answer::prompt_key(child).as_str()));
        }
        // In AGENTS the child has no row (its family folded).
        assert!(!t[f.agents.clone()].iter().any(|l| l.contains("Tests kid")));
    }
}

/// The parent asking too: TWO cards, as agent-jump's queue lists them: its
/// own (acting on it) and the stand-in (its title, `↳`, acting on the
/// child).
#[test]
fn parent_and_child_asking_are_two_cards() {
    let v = fam("needs-input", Some(ASK));
    let mine: Vec<&crate::view::Need> = v.needs.iter().filter(|n| n.agent.window_id == "@1").collect();
    assert_eq!(mine.len(), 2, "{:?}", v.needs.iter().map(|n| (&n.agent.window_id, n.via.is_some())).collect::<Vec<_>>());
    let own = mine.iter().find(|n| n.via.is_none()).unwrap();
    assert_eq!((own.target().window_id.as_str(), own.reason.as_str()), ("@1", "Which deck?"));
    let si = mine.iter().find(|n| n.via.is_some()).unwrap();
    assert_eq!((si.target().window_id.as_str(), si.reason.as_str()), ("@4", "Bash cargo test"));
    let f = layout(&v, 42, 60, &Opts::default());
    let t = texts(&f);
    let cards: Vec<usize> = f.needs.clone().filter(|&y| t[y].contains("Abby orchestrator")).collect();
    assert_eq!(cards.len(), 2, "{t:#?}");
    let (with, without): (Vec<usize>, Vec<usize>) = cards.into_iter().partition(|&y| t[y + 1].contains(KID_MARK));
    assert_eq!((with.len(), without.len()), (1, 1), "{t:#?}");
    assert_eq!(wins(&f.rows[with[0]]), ["@4"]);
    assert_eq!(wins(&f.rows[without[0]]), ["@1"]);
}

/// The stand-in takes the child's CURRENT state, whatever `@agent_kids_ask`
/// says; a child no longer asking lists nothing, whatever the option says.
#[test]
fn the_childs_current_state_decides() {
    // The option says failed, the child asks: an ask, yellow.
    let v = fam("idle", Some("@4\tfailed\tboom"));
    assert_eq!(stand_ins(&v), ["@4"]);
    let n = v.needs.iter().find(|n| n.via.is_some()).unwrap();
    assert_eq!((n.agent.cat, n.agent.color), (Some(Cat::NeedsInput), Some("yellow")));
    // The child answered (running): nothing, and the parent is no entry.
    let mut agents = family("idle", Some(ASK), false);
    agents[3] = a("main", 4, "running", "Tests kid").with("child", "@1");
    let v = lettered(view(agents, Some("@5")));
    assert!(stand_ins(&v).is_empty());
    assert_eq!(needs_ids(&v), ["@6"]);
    // The child failed: failed, red, first.
    let mut agents = family("idle", Some(ASK), false);
    agents[3] = a("main", 4, "failed", "Tests kid").with("child", "@1").with("detail_kind", "fail").with("detail", "boom");
    let v = lettered(view(agents, Some("@5")));
    assert_eq!(stand_ins(&v), ["@4"]);
    // EVERY asking child, not just the one the option names: a second.
    let mut agents = family("idle", Some(ASK), false);
    agents[1] = a("main", 2, "failed", "Parser kid").with("child", "@1").with("detail_kind", "fail").with("detail", "oops");
    let v2 = lettered(view(agents, Some("@5")));
    assert_eq!(stand_ins(&v2), ["@2", "@4"]);
    assert!(v2.needs.iter().filter(|n| n.via.is_some()).all(|n| n.agent.title == "Abby orchestrator"));
    let f = layout(&v2, 42, 60, &Opts::default());
    let t = texts(&f);
    for kid in ["@2", "@4"] {
        let y = f.needs.clone().find(|&y| wins(&f.rows[y]) == [kid] && t[y].contains("Abby")).expect("a card per kid");
        assert!(t[y + 1].contains(KID_MARK), "{t:#?}");
    }
    let n = &v.needs[0];
    assert_eq!((n.agent.cat, n.agent.color, n.target().window_id.as_str()), (Some(Cat::Failed), Some("red"), "@4"));
    assert_eq!(n.reason, "boom");
}

/// A stand-in sorts as its CHILD (agent-jump's order): its stamp, then its
/// session and index.
#[test]
fn a_stand_in_sorts_as_its_child() {
    let mut agents = family("idle", Some(ASK), false);
    // The child asked long ago (older than the plain asker): first.
    agents[3] = a("main", 4, "needs-input", "Tests kid").with("child", "@1").with("since", &format!("{} needs-input", NOW - 5000));
    let v = lettered(view(agents, Some("@5")));
    assert_eq!(v.needs[0].via.as_ref().map(|c| c.window_id.as_str()), Some("@4"));
    assert_eq!(v.needs[0].stamp, (NOW - 5000).to_string());
    assert_eq!(v.needs[0].agent.age, crate::text::ago(Some(NOW - 5000), NOW as f64));
}

/// A GRANDCHILD (a child of a child) folds under the root's card, its ask
/// surfacing on the root; open, it sits right after its own parent, 2 cells
/// further in.
#[test]
fn a_grandchild_folds_under_the_root() {
    let mut agents = family("idle", None, false);
    agents[1] = a("main", 2, "running", "Parser kid").with("child", "@1").with("kids", "1 0")
        .with("kids_ask", "@7\tneeds-input\tBash ls");
    agents.push(a("main", 7, "needs-input", "Grand kid").with("child", "@2").with("detail_kind", "perm").with("detail", "Bash ls"));
    let v = lettered(view(agents, Some("@5")));
    assert_eq!(v.kids.get("@7").map(String::as_str), Some("@1"));
    assert_eq!(v.kids.get("@2").map(String::as_str), Some("@1"));
    let depth = |id: &str| v.agents().find(|a| a.window_id == id).map(|a| a.depth);
    assert_eq!((depth("@1"), depth("@2"), depth("@7")), (Some(0), Some(1), Some(2)));
    assert_eq!(stand_ins(&v), ["@4", "@7"]);
    let n = need(&v, "@7");
    assert_eq!((n.agent.window_id.as_str(), n.agent.title.as_str()), ("@1", "Abby orchestrator"));
    assert!(!needs_ids(&v).contains(&"@7") && !needs_ids(&v).contains(&"@2"));
    let f = layout(&v, 42, 60, &open(&["@1"]));
    let t = texts(&f);
    let p = parent_row(&f, &t);
    let nested: Vec<&str> = (p + 2..p + 6).map(|y| t[y].as_str()).collect();
    assert!(nested.iter().all(|l| l.contains(KID_MARK)), "{t:#?}");
    assert!(nested[0].contains("Parser kid") && nested[1].contains("Grand kid") && nested[2].contains("Docs kid"), "{t:#?}");
    assert_eq!(col(nested[1], "↳"), col(nested[0], "↳") + 2);
}

/// A child whose parent is no drawn card is a PLAIN agent: its own card,
/// counts and NEEDS YOU entry (abby in a home window, a parked parent).
#[test]
fn a_child_of_an_undrawn_parent_is_plain() {
    for how in ["home", "parked"] {
        let mut agents = family("idle", Some(ASK), false);
        let s = if how == "parked" { "stash" } else { "main" };
        let p = a(s, 1, "idle", "Abby orchestrator").with("kids", "3 2").with("kids_ask", ASK);
        agents[0] = if how == "home" { p.with("home", "1") } else { p };
        let v = lettered(view(agents, Some("@5")));
        assert!(v.kids.is_empty(), "{how}: {:?}", v.kids);
        assert!(stand_ins(&v).is_empty(), "{how}");
        // (@3's done turn and @4's ask are their own entries now.)
        assert_eq!(needs_ids(&v), ["@3", "@4", "@6"], "{how}");
        let main = v.spaces.iter().find(|s| s.name == "main").unwrap();
        assert_eq!(main.counts.get(Cat::NeedsInput), 2, "{how}");
        let f = layout(&v, 42, 60, &Opts::default());
        let t = texts(&f);
        assert!(!t.iter().any(|l| l.contains(KID_MARK)), "{how}: {t:#?}");
        assert!(f.agents.clone().any(|y| t[y].contains("Parser kid")), "{how}: {t:#?}");
    }
}

/// An EX-child (its parent's agent gone: `@agent_kid_orphan`) is a plain
/// agent with its own card, its second line tagged `↳ (parent gone)`.
#[test]
fn an_orphan_says_its_parent_is_gone() {
    let agents = vec![
        a("main", 1, "running", "Plain agent"),
        a("main", 2, "idle", "Ex kid").with("spawn_id", "s-x").with("kid_orphan", "a").with("detail_kind", "done")
            .with("detail", "wrote the parser"),
    ];
    let v = lettered(view(agents, Some("@1")));
    let f = layout(&v, 44, 40, &Opts::default());
    let t = texts(&f);
    let y = find(&f, "Ex kid");
    assert!(f.agents.contains(&y) && !t[y].contains(KID_MARK), "{t:#?}");
    assert!(t[y + 1].contains(crate::view::ORPHAN_TAG), "{:?}", t[y + 1]);
    assert_eq!(f.rows[y].covers.len(), 1);
    // Mack's 34 columns: the short form, never dropped.
    let f = layout(&v, 34, 40, &Opts::default());
    let t = texts(&f);
    let y = find(&f, "Ex kid");
    assert!(t[y + 1].contains(&format!("{} · 2m", crate::view::ORPHAN_SHORT)), "{:?}", t[y + 1]);
}

/// A rate-limited child still shows in the rate-limit banner's list.
#[test]
fn a_limited_kid_is_in_the_limit_banner() {
    let mut agents = family("idle", None, false);
    agents[2] = a("main", 3, "paused", "Docs kid").with("child", "@1").with("limit", "1792420000 five_hour 1792410000");
    let v = lettered(view(agents, Some("@5")));
    let split = v.kids_split();
    let lim: Vec<&str> = split.limited().iter().map(|a| a.window_id.as_str()).collect();
    assert_eq!(lim, ["@3"]);
}

/// The menu's Needs tab: a stand-in row acts on the CHILD (Enter, y / n)
/// under its parent's title.
#[test]
fn the_menus_need_row_targets_the_child() {
    let v = fam("idle", Some(ASK));
    let row = crate::menu::rows::Row::Need(need(&v, "@4").clone());
    let t = row.target().unwrap();
    assert_eq!((t.win.as_str(), t.title.as_str()), ("@4", "Abby orchestrator"));
    assert_eq!(t.key, crate::menu::rows::RowKey::Need("@4".into()));
    let child = v.agents().find(|a| a.window_id == "@4").unwrap();
    assert_eq!(t.perm, Some(crate::answer::prompt_key(child)));
}

/// Pinning a child is refused (it would never show); unpinning one is not.
#[test]
fn a_child_never_pins() {
    let rows: Vec<String> = family("idle", None, false)
        .iter()
        .enumerate()
        .map(|(n, x)| {
            let mut set: Vec<(&str, &str)> = vec![("state", x.st)];
            set.extend(x.set.iter().map(|(k, v)| (*k, v.as_str())));
            row(x.s, x.i, &format!("@{}", n + 1), &set)
        })
        .collect();
    let snap = Snapshot::parse(&rows.join("\n"));
    // (Refused before any tmux call: the socket is never reached.)
    let tmux = crate::Tmux::with_socket(Some(crate::Socket::Name("agentui-kids-never".into())));
    let dir = std::path::Path::new("/nonexistent");
    let r = crate::pins::toggle(&tmux, &snap, "@2", Some(true), 1, None, dir, &crate::pins::LiveProcs);
    assert_eq!(r.err().as_deref(), Some("a delegated child folds under its parent"));
}

/// `@agent_child` unset is a plain agent as before (its own card, its own
/// NEEDS YOU entry, counted); so is one naming a window that is not there
/// (a parent gone before the reconciler caught up).
#[test]
fn no_child_option_is_a_plain_agent() {
    let mut agents = family("idle", None, false);
    agents.push(a("main", 7, "needs-input", "Stray kid").with("child", "@99").with("detail_kind", "ask").with("detail", "Hm?"));
    let v = lettered(view(agents, Some("@5")));
    assert_eq!(needs_ids(&v), ["@6", "@7"]);
    let f = layout(&v, 42, 60, &Opts::default());
    let t = texts(&f);
    for title in ["Plain asker", "Stray kid", "Plain agent"] {
        let y = f.agents.clone().find(|&y| t[y].contains(title)).unwrap_or_else(|| panic!("{title}: {t:#?}"));
        assert!(!t[y].contains(KID_MARK), "{title}: {:?}", t[y]);
        assert_eq!(f.rows[y].covers.len(), 1, "{title}");
        // A full card: a second row.
        assert_eq!(wins(&f.rows[y + 1]), wins(&f.rows[y]));
    }
    let main = v.spaces.iter().find(|s| s.name == "main").unwrap();
    assert_eq!(main.counts.get(Cat::NeedsInput), 2);
}

/// A pinned child folds like any other: no PINNED entry, no ⌥W digit; its
/// row under its parent once the family is open.
#[test]
fn a_pinned_child_folds() {
    let v = lettered(view(family("idle", None, true), Some("@5")));
    assert!(v.pinned_agents().is_empty());
    let f = layout(&v, 42, 60, &open(&["@1"]));
    let t = texts(&f);
    assert!(f.pinned.is_empty(), "{t:#?}");
    let y = find(&f, "Docs kid");
    assert!(t[y].contains(KID_MARK), "{t:#?}");
    let rows: Vec<String> = family("idle", None, true)
        .iter()
        .enumerate()
        .map(|(n, x)| {
            let mut set: Vec<(&str, &str)> = vec![("state", x.st)];
            set.extend(x.set.iter().map(|(k, v)| (*k, v.as_str())));
            row(x.s, x.i, &format!("@{}", n + 1), &set)
        })
        .collect();
    assert!(crate::pins::pin_order(&Snapshot::parse(&rows.join("\n"))).is_empty());
}

/// Down the ladder with the family OPEN: its rows at full detail, its
/// strip on the rungs before (and with) one-line cards, folded from the
/// spaces' collapse on. Every window is covered exactly once at every
/// height, child rows and strips covering no one, and no row is wider than
/// the frame. Folded, no child row or strip at any height.
#[test]
fn kids_fold_with_their_parent() {
    let mut agents = spawned("ready", true);
    for i in 8..=17 {
        agents.push(a("work", i, if i % 2 == 0 { "running" } else { "idle" }, "Some other agent"));
    }
    let v = lettered(view(agents, Some("@6")));
    let ids: Vec<String> = (1..=17).map(|n| format!("@{n}")).collect();
    let kid = |r: &Row| r.covers.is_empty() && wins(r).iter().any(|w| ["@2", "@3", "@4", "@5"].contains(w)) && !is_strip(r);
    let mut steps = std::collections::BTreeSet::new();
    let mut strips = 0;
    for (opts, opened) in [(open(&["@1"]), true), (Opts::default(), false)] {
        for h in 8..=70u16 {
            let f = layout(&v, 44, h, &opts);
            let t = texts(&f);
            if opened {
                steps.insert(f.agents_step);
            }
            let mut got: HashMap<&str, usize> = HashMap::new();
            for r in &f.rows {
                assert_eq!(dwidth(&r.text()), 44, "{h}: {t:#?}");
                for (_, w) in &r.covers {
                    *got.entry(w.as_str()).or_default() += 1;
                }
            }
            if f.agents.is_empty() {
                continue;
            }
            for id in &ids {
                assert_eq!(got.get(id.as_str()).copied().unwrap_or(0), 1, "{id} at {h}: {t:#?}");
            }
            let rows = f.agents.clone().filter(|&y| kid(&f.rows[y])).count();
            let strip = f.agents.clone().filter(|&y| is_strip(&f.rows[y])).count();
            match (opened, f.agents_step) {
                (false, _) => assert_eq!((rows, strip), (0, 0), "{h}: {t:#?}"),
                (true, 0) => assert_eq!((rows, strip), (4, 0), "{h}: {t:#?}"),
                (true, 1) => assert!((rows, strip) == (4, 0) || (rows, strip) == (0, 1), "{h}: {t:#?}"),
                (true, 2 | 3) => assert_eq!((rows, strip), (0, 1), "{h}: {t:#?}"),
                (true, _) => assert_eq!((rows, strip), (0, 0), "{h}: {t:#?}"),
            }
            strips += strip;
        }
    }
    assert!(steps.contains(&0) && steps.iter().any(|&s| s >= 2), "{steps:?}");
    assert!(strips > 0, "the strip rung was never taken");
}

/// The verdict is every space's (NEEDS YOU and the counts never flip with
/// ⌥V); the scope only changes the drawing: in space scope a child in the
/// client's space whose parent lives in another space (not drawn) is drawn
/// as a card of its own in its space, tagged `↳ <parent's letter> ·
/// <space>`, counted nowhere; in all scope it folds under its parent there
/// (a row under it once the family is open).
#[test]
fn kids_go_where_their_parent_goes() {
    let rows = |scope: &str| {
        vec![
            row("work", 1, "@1", &[("state", "idle"), ("summary", "Abby orchestrator"), ("kids", "1 1"), ("space_index", "2")]),
            row("main", 1, "@2", &[("state", "needs-input"), ("summary", "Kid in main"), ("child", "@1"), ("space_index", "1")]),
            row("main", 2, "@3", &[("state", "running"), ("summary", "Plain agent"), ("space_index", "1")]),
            format!("\x1fclient\x1f{CLIENT}\x1f@3\x1fmain\x1fon\x1f{scope}"),
        ]
        .join("\n")
    };
    for (scope, shown) in [("space", false), ("all", true)] {
        let snap = Snapshot::parse(&rows(scope));
        let c = Collator::new("C");
        let args = BuildArgs { client: Some(CLIENT), now: NOW as f64, collator: &c, git: None, log: vec![], watcher_age: Some(1.0) };
        let mut v = lettered(ViewModel::build(&snap, args));
        v.apply_scope(&snap);
        let root = v.agents().find(|a| a.window_id == "@1").and_then(|a| a.letter).expect("the root's letter");
        for opts in [Opts::default(), open(&["@1"])] {
            let f = layout(&v, 44, 60, &opts);
            let t = texts(&f);
            let kid: Vec<usize> = f.agents.clone().filter(|&y| t[y].contains("Kid in main")).collect();
            assert!(f.agents.clone().any(|y| t[y].contains("Plain agent")), "{scope}: {t:#?}");
            if !shown {
                assert_eq!(kid.len(), 1, "{scope}: {t:#?}");
                assert!(!t[kid[0]].contains(KID_MARK), "{scope}: {t:#?}");
                assert!(t[kid[0] + 1].contains(&format!("{KID_MARK}{root} · work")), "{scope}: {t:#?}");
                // (Only NEEDS YOU, which is global, names the parent.)
                assert!(!t[f.agents.clone()].iter().any(|l| l.contains("Abby orchestrator")), "{scope}: {t:#?}");
                assert_eq!(v.scope.counts.total(), 1, "counted nowhere");
            } else if opts.kids_open.is_empty() {
                assert!(kid.is_empty(), "{scope}: folded: {t:#?}");
                let p = parent_row(&f, &t);
                assert!(f.rows[p].covers.iter().any(|(_, w)| w == "@2"), "{scope}");
            } else {
                assert_eq!(kid.len(), 1, "{scope}: {t:#?}");
                assert!(t[kid[0]].contains(KID_MARK) && t[kid[0] - 2].contains("Abby orchestrator"), "{scope}: {t:#?}");
            }
        }
        // Mack's 34 columns: the tag in the age's place, never dropped.
        if !shown {
            let f = layout(&v, 34, 60, &Opts::default());
            let t = texts(&f);
            let y = f.agents.clone().find(|&y| t[y].contains("Kid in main")).unwrap();
            assert!(t[y + 1].contains(&format!("{KID_MARK}{root} · work")), "{t:#?}");
        }
        // Either way a kid: the same verdict in both scopes, its ask its
        // parent's stand-in in NEEDS YOU.
        assert_eq!(v.kids.get("@2").map(String::as_str), Some("@1"), "{scope}");
        assert_eq!((stand_ins(&v), needs_ids(&v)), (vec!["@2"], vec![]), "{scope}");
        let main = v.spaces.iter().find(|s| s.name == "main").unwrap();
        assert_eq!(main.counts.total(), 1, "{scope}");
    }
}
