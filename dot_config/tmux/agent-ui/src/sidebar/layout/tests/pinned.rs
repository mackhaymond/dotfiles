//! The PINNED section: pinned agents move out of their spaces into a block
//! of their own above AGENTS, in pin order, in every scope.

use super::*;

/// [`quiet`] with Lost suit jacket (@6, idle) pinned first and Kua Yu (@3,
/// asking) second; the client on Tmux Agent Sidebar (@2).
fn pinned_quiet() -> ViewModel {
    let mut v = quiet_agents();
    v[5] = a("main", 6, "idle", "Lost suit jacket").with("pinned", "100");
    v[2] = a("main", 3, "needs-input", "Kua Yu focus timing").with("detail_kind", "ask")
        .with("detail", "Which deck should I pull the timing from?").with("pinned", "200");
    view(v, Some("@2"))
}

fn quiet_agents() -> Vec<A<'static>> {
    vec![
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
    ]
}

/// The busy fixture with five pins across four spaces (one of them asking,
/// one the client's own, two idle).
fn pinned_busy() -> ViewModel {
    let mut v = busy_agents();
    for (k, t) in [(1usize, "50"), (3, "60"), (6, "70"), (13, "80"), (16, "90")] {
        let x = std::mem::replace(&mut v[k], a("x", 0, "", ""));
        v[k] = x.with("pinned", t);
    }
    view(v, Some("@4"))
}

/// Every agent covered once: the PINNED ones in their block, the rest in
/// their groups (the split view's).
fn check_pinned_accounting(name: &str, v: &ViewModel, f: &Frame) {
    let s = v.pins_split();
    let mut want: HashMap<(String, String), usize> = HashMap::new();
    for a in s.pinned.iter().chain(groups(&s).iter().flat_map(|g| g.agents.iter())) {
        *want.entry((a.session.clone(), a.window_id.clone())).or_default() += 1;
    }
    let mut got: HashMap<(String, String), usize> = HashMap::new();
    for r in &f.rows {
        for c in &r.covers {
            *got.entry(c.clone()).or_default() += 1;
        }
    }
    assert_eq!(got, want, "{name} {}x{}: {:#?}", f.width, f.height, texts(f));
}

/// A pinned agent MOVES: its card sits under ` PINNED` (in pin order, the
/// first pinned first), not in its space's group; NEEDS YOU still lists
/// the asking one; the AGENTS header follows the block after a gap; each
/// pinned card's second line names its space.
#[test]
fn pinned_agents_move_to_their_own_section() {
    let v = lettered(pinned_quiet());
    let f = layout(&v, 34, 70, &Opts::default());
    let t = texts(&f);
    let p = find(&f, " PINNED");
    assert_eq!(f.pinned.start, p, "{t:#?}");
    assert!(t[p].ends_with(&format!("{} 2 ", crate::pins::GLYPH)), "{t:#?}");
    assert!(f.agents.start <= p && f.needs.end <= p, "{t:#?}");
    // Pin order: @6 (pinned at 100) then @3 (200), two lines each.
    assert!(t[p + 1].contains("Lost suit jacket") && t[p + 3].contains("Kua Yu"), "{t:#?}");
    assert_eq!(f.pinned, p..p + 5, "{t:#?}");
    // The space on the second line, before the age.
    assert!(t[p + 2].contains("main · 6m"), "{t:#?}");
    // A gap, then AGENTS: the groups no longer hold them.
    assert!(t[p + 5].trim().is_empty() && t[p + 6].starts_with(" AGENTS"), "{t:#?}");
    let agents = &t[f.pinned.end..f.agents.end];
    assert!(!agents.iter().any(|l| l.contains("Kua Yu") || l.contains("Lost suit")), "{agents:#?}");
    assert!(agents.iter().any(|l| l.contains("Notch Tasks Orchestrator")), "{agents:#?}");
    // NEEDS YOU still asks for Kua Yu.
    assert!(t[f.needs.clone()].iter().any(|l| l.contains("Kua Yu")), "{t:#?}");
    // Clicks go there; a pinned card keeps its key-cap and rail.
    assert_eq!(f.rows[p + 3].target_at(5), Some(&Target::Go { win: "@3".into(), session: "main".into() }));
    assert!(f.rows[p + 3].segs.contains(&seg("▎", "yellow")), "{:?}", f.rows[p + 3].segs);
    check_pinned_accounting("pinned_quiet", &v, &f);
}

/// Pin order is the pin's time, never the window's place: pinning another
/// agent later puts it last and moves no one already pinned.
#[test]
fn pin_order_is_first_pinned_first() {
    let mut ag = quiet_agents();
    ag[9] = a("schedule", 1, "idle", "P0a multi-user execution").with("pinned", "50");
    ag[0] = a("main", 1, "running", "Handy.app Speech Commands").with("pinned", "70");
    let before = view(ag, Some("@2"));
    let names = |v: &ViewModel| v.pinned_agents().iter().map(|a| a.window_id.clone()).collect::<Vec<_>>();
    assert_eq!(names(&before), ["@10", "@1"]);
    let mut ag = quiet_agents();
    ag[9] = a("schedule", 1, "idle", "P0a multi-user execution").with("pinned", "50");
    ag[0] = a("main", 1, "running", "Handy.app Speech Commands").with("pinned", "70");
    ag[7] = a("bai", 1, "idle", "Application link").with("pinned", "90");
    assert_eq!(names(&view(ag, Some("@2"))), ["@10", "@1", "@8"]);
    // A shell's stray pin, and a home's, are nothing.
    let mut ag = quiet_agents();
    ag.push(a("main", 20, "", "a shell").with("pinned", "10"));
    ag.push(a("main", 21, "idle", "home").with("pinned", "10").with("home", "1"));
    assert!(view(ag, Some("@2")).pinned_agents().is_empty());
}

/// PINNED is global: in THIS-SPACE scope a pinned agent of another space is
/// still listed (that is what pinning is for), and the space's own pinned
/// agent is not in its group; a space whose every agent is pinned says so.
#[test]
fn pins_show_in_every_scope() {
    let ag = vec![
        a("main", 1, "running", "Mine working"),
        a("main", 2, "idle", "Mine pinned").with("pinned", "20"),
        a("bai", 1, "idle", "Elsewhere pinned").with("pinned", "10"),
        a("bai", 2, "idle", "Elsewhere not"),
    ];
    let v = scoped(ag, "@1", "main", "on", "space", "");
    assert_eq!(v.scope.session.as_deref(), Some("main"));
    let f = layout(&v, 34, 50, &Opts::default());
    let t = texts(&f);
    let pins: Vec<&String> = t[f.pinned.clone()].iter().collect();
    assert!(pins.iter().any(|l| l.contains("Elsewhere pinned")) && pins.iter().any(|l| l.contains("Mine pinned")), "{t:#?}");
    assert!(pins.iter().any(|l| l.contains("bai · ")), "{t:#?}");
    let rest = &t[f.pinned.end..f.agents.end];
    assert!(rest.iter().any(|l| l.contains("Mine working")) && !rest.iter().any(|l| l.contains("pinned")), "{rest:#?}");
    assert!(!t.iter().any(|l| l.contains("Elsewhere not")), "{t:#?}");
    // Every agent of the space pinned: the header, and why it is empty.
    let ag = vec![a("main", 1, "idle", "Only one").with("pinned", "5"), a("bai", 1, "idle", "Other")];
    let v = scoped(ag, "@1", "main", "on", "space", "");
    let t = texts(&layout(&v, 34, 50, &Opts::default()));
    assert!(t.iter().any(|l| l.contains("all pinned above")) && !t.iter().any(|l| l.contains("no agents in this space")), "{t:#?}");
    // All spaces, every agent pinned: the same words under AGENTS.
    let ag = vec![a("main", 1, "idle", "One").with("pinned", "5"), a("bai", 1, "idle", "Two").with("pinned", "6")];
    let v = scoped(ag, "@1", "main", "on", "all", "");
    let t = texts(&layout(&v, 34, 50, &Opts::default()));
    assert!(t.iter().any(|l| l.contains("all pinned above")), "{t:#?}");
}

/// The client's own pinned agent keeps its lift ("you are here") in PINNED;
/// a window linked into two spaces is pinned once.
#[test]
fn a_pinned_card_keeps_its_look() {
    let mut ag = quiet_agents();
    ag[1] = a("main", 2, "running", "Tmux Agent Sidebar").with("pinned", "1");
    let v = lettered(view(ag, Some("@2")));
    let f = layout(&v, 34, 70, &Opts::default());
    let y = f.pinned.start + 1;
    assert_eq!(f.rows[y].bg, Some(Ink::Pal(MINE_BG)), "{:#?}", texts(&f));
    assert!(f.rows[y].segs.iter().any(|s| s.text == MINE), "{:?}", f.rows[y].segs);
    // Linked: @2 in main and in work, pinned: one card.
    let mut rows: Vec<String> = vec![
        row("main", 1, "@2", &[("state", "idle"), ("summary", "Linked"), ("pinned", "3")]),
        row("work", 4, "@2", &[("state", "idle"), ("summary", "Linked"), ("pinned", "3")]),
        row("work", 5, "@5", &[("state", "idle"), ("summary", "Other")]),
    ];
    rows.push(format!("\x1fclient\x1f{CLIENT}\x1f@5\x1fwork"));
    let c = Collator::new("en_US.UTF-8");
    let args = BuildArgs { client: Some(CLIENT), now: NOW as f64, collator: &c, git: None, log: vec![], watcher_age: Some(1.0) };
    let v = ViewModel::build(&Snapshot::parse(&rows.join("\n")), args);
    let p = v.pinned_agents();
    assert_eq!(p.len(), 1);
    assert_eq!(p[0].session, "work"); // the client's session's copy
    let t = texts(&layout(&v, 34, 40, &Opts::default()));
    assert_eq!(t.iter().filter(|l| l.contains("Linked")).count(), 1, "{t:#?}");
}

/// Every size: exactly the frame, every agent accounted for once; pinned
/// cards are never folded into an idle line, every pin keeps its own card
/// until even the collapsed spaces are cut, and pins are worth more than
/// the idle agents in the groups.
#[test]
fn pinned_frames_fit_every_size() {
    let mut checked = 0;
    for (name, v) in [("quiet", pinned_quiet()), ("busy", keyed(pinned_busy()))].into_iter().flat_map(|(n, v)| [(n, v.clone()), (n, lettered(v))]) {
        let pins: Vec<(String, String)> = v.pins_split().pinned.iter().map(|a| (a.session.clone(), a.window_id.clone())).collect();
        for w in [24u16, 30, 34, 44] {
            for h in 2u16..=80 {
                let f = layout(&v, w, h, &Opts::default());
                assert_eq!(f.rows.len(), usize::from(h), "{name} {w}x{h}");
                for r in &f.rows {
                    assert_eq!(width(&r.segs), usize::from(w), "{name} {w}x{h}: {:?}", r.text());
                }
                check_pinned_accounting(name, &v, &f);
                check_order(&format!("pinned {name} {w}x{h}"), &f);
                let t = texts(&f);
                for r in &f.rows {
                    // A pin is never one of several agents a row stands for, but in the last resort's count.
                    if r.covers.len() > 1 && !r.text().contains("… ") {
                        assert!(!r.covers.iter().any(|c| pins.contains(c)), "{name} {w}x{h}: {:?} {t:#?}", r.text());
                    }
                }
                if f.agents_step <= 4 && !f.agents.is_empty() {
                    // Up to the collapsed spaces, every pin has its card.
                    for p in &pins {
                        assert!(f.rows[f.pinned.clone()].iter().any(|r| r.covers == [p.clone()]), "{name} {w}x{h}: {p:?} {t:#?}");
                    }
                    checked += 1;
                }
                if f.agents_step >= 1 && f.agents_step <= 3 {
                    // Pins keep the frame's one card height (two lines at step 1).
                    let lines = f.rows[f.pinned.clone()].len() - 1;
                    let want = if f.agents_step == 1 && w >= 26 { 2 } else { 1 } * pins.len();
                    assert_eq!(lines, want, "{name} {w}x{h}: {t:#?}");
                }
            }
        }
    }
    assert!(checked > 100, "{checked}");
}

/// `v` with its pins' ⌥W digit as the UIs give them (the first nine in pin order).
fn keyed(mut v: ViewModel) -> ViewModel {
    let keys: HashMap<String, u8> = v.pinned_agents().iter().take(9).zip(1u8..).map(|(a, n)| (a.window_id.clone(), n)).collect();
    crate::view::Agent::set_pin_keys(&mut v, &keys);
    v
}

/// The cell column a row's segment starts at, for the first segment that
/// says `text`.
fn seg_col(r: &Row, text: &str) -> Option<usize> {
    let mut x = 0;
    for s in &r.segs {
        if s.text == text {
            return Some(x);
        }
        x += dwidth(&s.text);
    }
    None
}

/// ⌥W then 1-9: a two-line pinned card wears its digit as a cap UNDER its
/// letter (no title room lost); a one-line card beside it; the PINNED
/// header hints `⌥W 1-2`; no digit anywhere else.
#[test]
fn pinned_cards_wear_their_key() {
    let v = lettered(keyed(pinned_quiet()));
    let f = layout(&v, 34, 70, &Opts::default());
    let t = texts(&f);
    let p = f.pinned.start;
    assert!(f.rows[p].segs.contains(&bold("⌥W", "blue")) && f.rows[p].segs.contains(&seg(" 1-2", LABEL)), "{:?}", f.rows[p].segs);
    for (y, n) in [(p + 1, 1), (p + 3, 2)] {
        let a = v.pins_split().pinned[n - 1].clone();
        let letter = format!(" {} ", a.letter.unwrap());
        let (lx, dx) = (seg_col(&f.rows[y], &letter), seg_col(&f.rows[y + 1], &format!(" {n} ")));
        assert!(lx.is_some() && lx == dx, "{y}: {lx:?} {dx:?} {t:#?}");
    }
    // Only PINNED cards wear one.
    // (Cards only: NEEDS YOU's count is a ` 1 ` chip of its own.)
    let digits = |r: &Row| {
        r.targets.iter().any(|t| matches!(t.2, Target::Go { .. })) && r.segs.iter().any(|s| s.bold && s.text.len() == 3 && s.text.trim().parse::<u8>().is_ok_and(|d| (1..=9).contains(&d)))
    };
    assert!(!f.rows[f.pinned.end..f.agents.end].iter().chain(&f.rows[f.needs.clone()]).any(digits), "{t:#?}");
    // One line each (a short frame): the digit beside the letter.
    let f = (10u16..60).map(|h| layout(&v, 34, h, &Opts::default())).find(|f| f.agents_step == 2).expect("a one-line step");
    assert!(seg_col(&f.rows[f.pinned.start + 1], " 1 ").is_some(), "{:#?}", texts(&f));
    // One pin: `⌥W 1`; at 14 columns the hint gives way to the count.
    let mut ag = quiet_agents();
    ag[0] = a("main", 1, "running", "Handy").with("pinned", "5");
    let v = keyed(view(ag, Some("@2")));
    let f = layout(&v, 34, 60, &Opts::default());
    assert!(f.rows[f.pinned.start].text().contains("⌥W 1 "), "{:?}", f.rows[f.pinned.start].text());
    let f = layout(&v, 14, 60, &Opts::default());
    assert!(!f.rows[f.pinned.start].text().contains('⌥'), "{:?}", f.rows[f.pinned.start].text());
}

/// ⌥W armed (the client's key table is `jump`): the digit caps invert with
/// every letter cap, the look no calm cap has.
#[test]
fn armed_jump_inverts_the_pin_digits() {
    let v = lettered(keyed(pinned_quiet()));
    let armed = Opts { bar: Bar { jump: true, ..Bar::default() }, ..Opts::default() };
    let f = layout(&v, 34, 70, &armed);
    let p = f.pinned.start;
    assert!(f.rows[p + 2].segs.contains(&block(" 1 ", ARMED.0, ARMED.1)), "{:?}", f.rows[p + 2].segs);
    assert!(f.rows[p + 4].segs.contains(&block(" 2 ", ARMED.0, ARMED.1)), "{:?}", f.rows[p + 4].segs);
    let calm = layout(&v, 34, 70, &Opts::default());
    assert!(!calm.rows[p + 2].segs.iter().any(|s| s.text == " 1 " && s.fg == Ink::Pal(ARMED.0)), "{:?}", calm.rows[p + 2].segs);
}

/// While prefix v's selection is live, PINNED goes: every pinned agent sits
/// in its space's group in tab order (selected or not), so a selection
/// across a pinned tab reads as the tabs do; it comes back when the
/// selection ends.
#[test]
fn a_live_selection_puts_pins_back_in_their_space() {
    let mut ag = quiet_agents();
    ag[2] = a("main", 3, "needs-input", "Kua Yu focus timing").with("detail_kind", "ask")
        .with("detail", "Which deck should I pull the timing from?").with("pinned", "200");
    for x in &mut ag[4..7] {
        *x = std::mem::replace(x, a("x", 0, "", "")).with("stash_sel", "1");
    }
    let x = std::mem::replace(&mut ag[5], a("x", 0, "", ""));
    ag[5] = x.with("pinned", "100");
    let v = lettered(view(ag, Some("@2")));
    let mut bar = demo_bar();
    bar.selecting = Some(3);
    for (w, h) in [(34u16, 70u16), (44, 70)] {
        let f = layout(&v, w, h, &with_bar(bar.clone()));
        let t = texts(&f);
        assert!(f.pinned.is_empty() && !t.iter().any(|l| l.contains(" PINNED")), "{w}: {t:#?}");
        // Tab order in main: @3 (pinned, not selected) between @2 and @4;
        // the selection @5 @6 (pinned) @7 one run of mauve cards.
        let at = |s: &str| (f.agents.start..f.agents.end).find(|&y| t[y].contains(s)).unwrap_or_else(|| panic!("{s}: {t:#?}"));
        let order = ["Tmux Agent", "Kua Yu", "Commodities", "Notch Tasks", "Lost suit", "Backyard"].map(at);
        assert!(order.windows(2).all(|p| p[0] < p[1]), "{order:?} {t:#?}");
        // (Each card's both rows.)
        let run = order[3]..order[5] + 2;
        for (r, l) in f.rows[run.clone()].iter().zip(&t[run]) {
            assert_eq!(r.bg, Some(Ink::Pal(SEL_BG)), "{w} {l:?}");
        }
        assert_ne!(f.rows[order[1]].bg, Some(Ink::Pal(SEL_BG)));
        // Each agent drawn once (NEEDS YOU aside).
        let mut seen: HashMap<(String, String), usize> = HashMap::new();
        for r in &f.rows[f.agents.clone()] {
            for c in &r.covers {
                *seen.entry(c.clone()).or_default() += 1;
            }
        }
        assert!(seen.values().all(|&n| n == 1), "{seen:?}");
        assert_eq!(seen.get(&("main".into(), "@6".into())), Some(&1));
        // The selection ends: PINNED is back, @6 and @3 in it.
        let f = layout(&v, w, h, &with_bar(demo_bar()));
        let t = texts(&f);
        let p = find(&f, " PINNED");
        assert_eq!(f.pinned.start, p, "{t:#?}");
        assert!(t[f.pinned.clone()].iter().any(|l| l.contains("Lost suit jacket")) && t[f.pinned.clone()].iter().any(|l| l.contains("Kua Yu")), "{t:#?}");
        assert!(!t[f.pinned.end..f.agents.end].iter().any(|l| l.contains("Lost suit jacket")), "{t:#?}");
    }
}

/// Without a single pin, nothing changes: no PINNED header, an empty range.
#[test]
fn no_pins_no_section() {
    for (name, v) in all() {
        let f = layout(&v, 34, 58, &Opts::default());
        assert!(f.pinned.is_empty(), "{name}");
        assert!(!texts(&f).iter().any(|l| l.contains(" PINNED")), "{name}");
    }
}
