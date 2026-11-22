//! The KIDS: abby-delegate children behave like native subagents of the
//! abby agent. The shell reconciler's derived options say who is whose
//! (`@agent_child` on a child, `@agent_kids` / `@agent_kids_ask` on its
//! parent); a child gets no card, NEEDS YOU entry, pin or count of its own,
//! only a dim `↳` row under its parent's full card, and an asking child
//! surfaces on its parent's NEEDS YOU card, which acts on the child.

use super::*;

/// Every glyph or word a state shows on a card (a nested row has none).
const STATE_MARKS: [&str; 7] = ["◐", "✓", "◉", "✕", "⏸", "working", "done"];

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

fn letter_of(v: &ViewModel, win: &str) -> crate::menu::letters::Letter {
    v.agents().find(|a| a.window_id == win).and_then(|a| a.letter).unwrap_or_else(|| panic!("no letter for {win}"))
}

/// A child gets no card and no NEEDS YOU entry, whatever its state (@2
/// running, @3 done, @4 asking), and counts nowhere; every row naming it is
/// a nested `↳` row that clicks to it.
#[test]
fn a_child_has_no_card_and_no_need() {
    let v = fam("idle", None);
    assert_eq!(needs_ids(&v), ["@6"], "only the plain asker");
    // (The asking child's entry is its parent's stand-in, `@agent_kids_ask` or not.)
    assert_eq!(stand_ins(&v), ["@4"]);
    assert!(v.needs.iter().all(|n| !["@2", "@3", "@4"].contains(&n.agent.window_id.as_str())));
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
        for (title, win) in [("Parser kid", "@2"), ("Docs kid", "@3"), ("Tests kid", "@4")] {
            for y in (0..t.len()).filter(|&y| t[y].contains(title)) {
                assert!(f.agents.contains(&y), "{title} outside AGENTS {w}x{h}: {t:#?}");
                assert!(t[y].contains(KID_MARK), "{title} has a row of its own {w}x{h}: {t:#?}");
                assert_eq!(wins(&f.rows[y]), [win], "{title}");
                assert!(f.rows[y].covers.is_empty(), "{title}");
            }
            // In NEEDS YOU only the asking one, as its parent's stand-in.
            for y in f.needs.clone().filter(|&y| wins(&f.rows[y]).contains(&win)) {
                assert_eq!(win, "@4", "{title} in NEEDS YOU {w}x{h}: {t:#?}");
                let card = if t[y].contains("Abby") { y } else { y - 1 };
                assert!(t[card].contains("Abby"), "{w}x{h}: {t:#?}");
            }
        }
    }
}

/// Under the parent's full card, one dim row per child in window order: the
/// child's quiet key-cap (its letter kept for ⌥W), `↳`, its title; the
/// card's colour and rail, no state glyph, colour or word.
#[test]
fn kids_nest_under_their_parent() {
    let v = fam("idle", None);
    let f = layout(&v, 42, 60, &Opts::default());
    let t = texts(&f);
    let p = f.agents.clone().find(|&y| t[y].contains("Abby orchestrator")).expect("the parent's card");
    let card = &f.rows[p];
    assert_eq!(wins(card), ["@1"]);
    // The parent's first row stands for it and its children.
    let covered: Vec<&str> = card.covers.iter().map(|(_, w)| w.as_str()).collect();
    assert_eq!(covered, ["@1", "@2", "@3", "@4"]);
    for (k, (title, win)) in [("Parser kid", "@2"), ("Docs kid", "@3"), ("Tests kid", "@4")].into_iter().enumerate() {
        let y = p + 2 + k;
        let r = &f.rows[y];
        let want = format!("{l}  {KID_MARK}{title}", l = letter_of(&v, win));
        assert!(t[y].contains(&want), "{want:?} in {:?}: {t:#?}", t[y]);
        assert_eq!(r.bg, card.bg, "one colour per card");
        assert_eq!(r.segs[1], card.segs[1], "the card's rail");
        for m in STATE_MARKS {
            assert!(!t[y].contains(m), "{m:?} on {:?}", t[y]);
        }
        // Dim: the title and arrow in the surface's dim, nothing bold but the cap.
        let title_seg = r.segs.iter().find(|s| s.text.contains(title)).unwrap();
        assert_eq!(title_seg.fg, Ink::Pal(dim(true)));
        assert!(!r.segs.iter().any(|s| s.bold && s.bg.is_none()), "{:?}", r.segs);
        assert_eq!(f.target(20, y as u16), Some(&Target::Go { win: win.into(), session: "main".into() }));
    }
    // Then the next card: no fourth kid row.
    assert!(!t[p + 5].contains(KID_MARK), "{t:#?}");
    // Letters: every child still has its own, none shared.
    let mut letters: Vec<_> = v.agents().filter_map(|a| a.letter).collect();
    letters.sort();
    letters.dedup();
    assert_eq!(letters.len(), 6);
}

/// A parent whose children run reads as WORKING with the workflow's gear
/// and how many run (`⚙ 2`), whatever its own state says (idle here; a done
/// turn is no NEEDS YOU entry while they run). None running: its own state,
/// no gear.
#[test]
fn a_parent_with_running_kids_reads_working() {
    for parent in ["idle", "done"] {
        let v = fam(parent, None);
        let f = layout(&v, 42, 60, &Opts::default());
        let t = texts(&f);
        let y = f.agents.clone().find(|&y| t[y].contains("Abby orchestrator")).unwrap();
        assert!(t[y].contains("Abby orchestrator ⚙ 2"), "{parent}: {:?}", t[y]);
        assert!(t[y].contains("working"), "{parent}: {:?}", t[y]);
        assert!(f.rows[y].segs.contains(&seg(" ⚙ 2", "teal")), "{parent}: {:?}", f.rows[y].segs);
        assert!(!needs_ids(&v).contains(&"@1"), "{parent}: a done turn with its kids running is no NEEDS YOU");
        // One gear in AGENTS (the asking child's stand-in in NEEDS YOU says `· ⚙ 2` too).
        assert_eq!(t[f.agents.clone()].iter().filter(|l| l.contains("⚙")).count(), 1, "one gear: {t:#?}");
    }
    let mut agents = family("idle", None, false);
    agents[0] = a("main", 1, "idle", "Abby orchestrator").with("harness", "abby").with("kids", "3 0");
    let v = lettered(view(agents, Some("@5")));
    let f = layout(&v, 42, 60, &Opts::default());
    let t = texts(&f);
    let y = f.agents.clone().find(|&y| t[y].contains("Abby orchestrator")).unwrap();
    assert!(!t[y].contains('⚙') && !t[y].contains("working"), "{:?}", t[y]);
    // Its children still nest.
    assert!(t[y + 2].contains(KID_MARK), "{t:#?}");
}

/// An asking child surfaces as its PARENT's NEEDS YOU card, with the
/// child's state, colour and words after its letter (`↳ f: perm Bash…`);
/// the card, its answer chips and a jump from it all target the CHILD.
#[test]
fn an_asking_child_surfaces_on_its_parent() {
    let v = fam("idle", Some(ASK));
    assert_eq!((needs_ids(&v), stand_ins(&v)), (vec!["@6"], vec!["@4"]), "the parent, never the child");
    let n = need(&v, "@4");
    assert_eq!(n.agent.window_id, "@1");
    assert_eq!((n.agent.state.as_str(), n.agent.cat), ("needs-input", Some(Cat::NeedsInput)));
    assert_eq!(n.target().window_id, "@4");
    assert_eq!((n.reason_word.as_str(), n.reason.as_str()), ("perm", "Bash cargo test"));
    let k = letter_of(&v, "@4");
    assert_eq!(n.via.as_ref().and_then(|c| c.letter), Some(k));
    for (w, h) in [(42u16, 60u16), (60, 60), (34, 58)] {
        let f = layout(&v, w, h, &Opts::default());
        let t = texts(&f);
        let y = f.needs.clone().find(|&y| t[y].contains("Abby orchestrator")).expect("the parent's NEEDS YOU card");
        assert!(t[y + 1].contains(&format!("{KID_MARK}{k}: perm")), "{w}: {:?}", t[y + 1]);
        for r in &f.rows[y..y + 2] {
            assert!(!r.targets.is_empty());
            assert!(wins(r).iter().all(|&x| x == "@4"), "{w}: {:?}", r.targets);
        }
        // The answer chips answer the child's prompt.
        let child = v.agents().find(|a| a.window_id == "@4").unwrap();
        let answers: Vec<&Target> =
            f.rows[y + 1].targets.iter().map(|(_, _, t)| t).filter(|t| matches!(t, Target::Answer { .. })).collect();
        assert_eq!(answers.len(), 2, "{w}: {:?}", f.rows[y + 1].targets);
        for a in answers {
            let Target::Answer { win, prompt, .. } = a else { unreachable!() };
            assert_eq!((win.as_str(), prompt.as_str()), ("@4", crate::answer::prompt_key(child).as_str()));
        }
        // In AGENTS the child is still only its nested row.
        assert!(f.agents.clone().filter(|&y| t[y].contains("Tests kid")).all(|y| t[y].contains(KID_MARK)));
    }
}

/// The parent asking too: TWO cards, as agent-jump's queue lists them: its
/// own (acting on it) and the stand-in (its title, `↳ k:`, acting on the
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
    let k = letter_of(&v, "@4");
    let (with, without): (Vec<usize>, Vec<usize>) = cards.into_iter().partition(|&y| t[y + 1].contains(&format!("{KID_MARK}{k}:")));
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
    for (kid, k) in [("@2", letter_of(&v2, "@2")), ("@4", letter_of(&v2, "@4"))] {
        let y = f.needs.clone().find(|&y| wins(&f.rows[y]) == [kid] && t[y].contains("Abby")).expect("a card per kid");
        assert!(t[y + 1].contains(&format!("{KID_MARK}{k}:")), "{t:#?}");
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

/// A GRANDCHILD (a child of a child) nests under the root's card and its
/// ask surfaces on the root.
#[test]
fn a_grandchild_folds_under_the_root() {
    let mut agents = family("idle", None, false);
    agents[1] = a("main", 2, "running", "Parser kid").with("child", "@1").with("kids", "1 0")
        .with("kids_ask", "@7\tneeds-input\tBash ls");
    agents.push(a("main", 7, "needs-input", "Grand kid").with("child", "@2").with("detail_kind", "perm").with("detail", "Bash ls"));
    let v = lettered(view(agents, Some("@5")));
    assert_eq!(v.kids.get("@7").map(String::as_str), Some("@1"));
    assert_eq!(v.kids.get("@2").map(String::as_str), Some("@1"));
    assert_eq!(stand_ins(&v), ["@4", "@7"]);
    let n = need(&v, "@7");
    assert_eq!((n.agent.window_id.as_str(), n.agent.title.as_str()), ("@1", "Abby orchestrator"));
    assert!(!needs_ids(&v).contains(&"@7") && !needs_ids(&v).contains(&"@2"));
    let f = layout(&v, 42, 60, &Opts::default());
    let t = texts(&f);
    let p = f.agents.clone().find(|&y| t[y].contains("Abby orchestrator")).unwrap();
    let nested: Vec<&str> = (p + 2..p + 6).map(|y| t[y].as_str()).collect();
    assert!(nested.iter().all(|l| l.contains(KID_MARK)) && nested[3].contains("Grand kid"), "{t:#?}");
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

/// A pinned child folds like any other: no PINNED entry, no ⌥W digit, its
/// row under its parent.
#[test]
fn a_pinned_child_folds() {
    let v = lettered(view(family("idle", None, true), Some("@5")));
    assert!(v.pinned_agents().is_empty());
    let f = layout(&v, 42, 60, &Opts::default());
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

/// Down the ladder: nested rows only under a full (two-line) card; a
/// one-line or folded parent folds its children with it (its row covers
/// them). Every window is covered exactly once at every height, and no row
/// is wider than the frame.
#[test]
fn kids_fold_with_their_parent() {
    let mut agents = family("idle", None, false);
    for i in 7..=16 {
        agents.push(a("work", i, if i % 2 == 0 { "running" } else { "idle" }, "Some other agent"));
    }
    let v = lettered(view(agents, Some("@5")));
    let ids: Vec<String> = (1..=16).map(|n| format!("@{n}")).collect();
    let mut steps = std::collections::BTreeSet::new();
    for h in 8..=70u16 {
        let f = layout(&v, 42, h, &Opts::default());
        let t = texts(&f);
        steps.insert(f.agents_step);
        let mut got: HashMap<&str, usize> = HashMap::new();
        for r in &f.rows {
            assert_eq!(dwidth(&r.text()), 42, "{h}: {t:#?}");
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
        let kids = f.agents.clone().filter(|&y| t[y].contains(KID_MARK)).count();
        match f.agents_step {
            0 | 1 => assert_eq!(kids, 3, "{h}: {t:#?}"),
            _ => assert_eq!(kids, 0, "{h}: {t:#?}"),
        }
    }
    assert!(steps.contains(&0) && steps.iter().any(|&s| s >= 2), "{steps:?}");
}

/// The verdict is every space's (NEEDS YOU and the counts never flip with
/// ⌥V); the scope only changes the drawing: in space scope a child in the
/// client's space whose parent lives in another space (not drawn) is drawn
/// as a card of its own in its space, counted nowhere; in all scope it is
/// nested under its parent there.
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
        let f = layout(&v, 42, 60, &Opts::default());
        let t = texts(&f);
        let kid: Vec<usize> = f.agents.clone().filter(|&y| t[y].contains("Kid in main")).collect();
        assert!(f.agents.clone().any(|y| t[y].contains("Plain agent")), "{scope}: {t:#?}");
        assert_eq!(kid.len(), 1, "{scope}: {t:#?}");
        if shown {
            assert!(t[kid[0]].contains(KID_MARK) && t[kid[0] - 2].contains("Abby orchestrator"), "{scope}: {t:#?}");
        } else {
            assert!(!t[kid[0]].contains(KID_MARK), "{scope}: {t:#?}");
            // (Only NEEDS YOU, which is global, names the parent.)
            assert!(!t[f.agents.clone()].iter().any(|l| l.contains("Abby orchestrator")), "{scope}: {t:#?}");
            assert_eq!(v.scope.counts.total(), 1, "counted nowhere");
        }
        // Either way a kid: the same verdict in both scopes, its ask its
        // parent's stand-in in NEEDS YOU.
        assert_eq!(v.kids.get("@2").map(String::as_str), Some("@1"), "{scope}");
        assert_eq!((stand_ins(&v), needs_ids(&v)), (vec!["@2"], vec![]), "{scope}");
        let main = v.spaces.iter().find(|s| s.name == "main").unwrap();
        assert_eq!(main.counts.total(), 1, "{scope}");
    }
}
