//! PENDING spaces: a claude in the hidden `pending` session building space
//! N is a space of its own at N, in AGENTS and the dock, its header
//! `N  creating…`, its card the claude's, every click its popup.

use super::*;

/// `@pending_color` in the fixture.
const PINK: Ink = Ink::Rgb(0xf5, 0xc2, 0xe7);
const REQ: &str = "make a rust cli called fizz";

/// main (⌘1): home @1, @2 working (the client's), @3 idle; work (⌘2): home
/// @4, @5 asking; the claude @6 in the hidden `pending` session holding
/// space 3 (pink), in `state` with `extra` options; notes (⌘4): home @7,
/// shell @8. `spaces`: the client row's `@spaces`, `scope` its
/// `@strip_scope`; `strip`: the same windows without the pending options.
/// Letters as the sidebar assigns them (@2 a, @3 s, @5 d, @6 f, @8 g), the
/// scope applied.
fn pend_with(state: &str, extra: &[(&str, &str)], spaces: &str, scope: &str, strip: bool) -> ViewModel {
    let since = |st: &str| format!("{} {st}", NOW - 90);
    let mut six: Vec<(&str, String)> = vec![("state", state.into()), ("summary", "Rust CLI fizz".into()), ("since", since(state)),
        ("kind", "claude".into()), ("space_hidden", "1".into())];
    if !strip {
        six.extend([("pending_space", "3".into()), ("pending_request", REQ.into()), ("pending_color", "#f5c2e7".into())]);
    }
    six.extend(extra.iter().map(|(k, v)| (*k, v.to_string())));
    let six: Vec<(&str, &str)> = six.iter().map(|(k, v)| (*k, v.as_str())).collect();
    let rows = [
        row("main", 0, "@1", &[("home", "1"), ("space_index", "1"), ("space_color", "#fab387")]),
        row("main", 1, "@2", &[("state", "running"), ("summary", "Tmux Agent Sidebar"), ("since", &since("running")),
            ("space_index", "1"), ("space_color", "#fab387"), ("active", "1")]),
        row("main", 2, "@3", &[("state", "idle"), ("summary", "Lost suit jacket"), ("since", &since("idle")), ("space_index", "1"),
            ("space_color", "#fab387")]),
        row("work", 0, "@4", &[("home", "1"), ("space_index", "2"), ("space_color", "#89b4fa")]),
        row("work", 1, "@5", &[("state", "needs-input"), ("summary", "Kua Yu focus timing"), ("since", &since("needs-input")),
            ("detail_kind", "ask"), ("detail", "Which deck?"), ("space_index", "2"), ("space_color", "#89b4fa")]),
        row("pending", 1, "@6", &six),
        row("notes", 0, "@7", &[("home", "1"), ("space_index", "4")]),
        row("notes", 1, "@8", &[("path", "/x/notes"), ("space_index", "4")]),
        row("agents", 1, "@900", &[("state", "failed"), ("summary", "hidden")]),
        format!("\x1fclient\x1f{CLIENT}\x1f@2\x1fmain\x1f{spaces}\x1f{scope}\x1f"),
    ];
    let snap = Snapshot::parse(&rows.join("\n"));
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

/// [`pend_with`], `@spaces` on, every space shown.
fn pend(state: &str, extra: &[(&str, &str)]) -> ViewModel {
    pend_with(state, extra, "on", "all", false)
}

fn pending_target() -> Target {
    Target::Pending("@6".into())
}

fn go6() -> Target {
    Target::Go { win: "@6".into(), session: "pending".into() }
}

/// The agents section's space headers, by what a click on each does.
fn header_targets(f: &Frame) -> Vec<Target> {
    f.agents.clone().filter(|&y| is_group_header(&f.rows[y], usize::from(f.width))).map(|y| f.rows[y].targets[0].2.clone()).collect()
}

/// The pending space sits at its number among the spaces, in AGENTS and the
/// dock alike: ` 3 ` solid in its colour, `creating…` dim italic, the
/// spinner while its claude works (the row animates, no counts beside it);
/// under it the claude's ordinary card (its letter, the working word),
/// whose second line is the request typed, as a prompt. The header, the
/// dock row: its popup ([`Target::Pending`]); the card: [`Target::Go`],
/// which `Actions::go` routes to the popup.
#[test]
fn a_pending_space_is_a_group_at_its_number() {
    let v = pend("running", &[]);
    for w in [44u16, 34] {
        let f = layout(&v, w, 60, &Opts::default());
        let t = texts(&f);
        let s = |n: &str| Target::Session(n.into());
        assert_eq!(header_targets(&f), [s("main"), s("work"), pending_target(), s("notes")], "{w}: {t:#?}");
        let y = f.agents.clone().find(|&y| f.rows[y].targets.first().map(|x| &x.2) == Some(&pending_target())).unwrap();
        let h = &f.rows[y];
        assert!(h.text().starts_with("  3  creating… ⠋"), "{w}: {:?}", h.text());
        assert!(h.text().trim_end().ends_with('⠋'), "{w}: no counts beside the spinner: {:?}", h.text());
        assert!(h.segs.contains(&block(" 3 ", ink_on(PINK), PINK)), "{:?}", h.segs);
        assert!(h.segs.contains(&italic(CREATING, "overlay2")), "{:?}", h.segs);
        assert!(h.anim);
        // The card: the claude's, two rows, one colour, both clicking through.
        let (c1, c2) = (&f.rows[y + 1], &f.rows[y + 2]);
        assert_eq!(c1.covers, [("pending".to_string(), "@6".to_string())]);
        assert!(c1.text().contains(" f ") && c1.text().contains("Rust CLI fizz") && c1.text().contains("working"), "{:?}", c1.text());
        assert!(c2.text().contains(&format!("{PROMPT_MARK}make a rust cli")), "{w}: {:?}", c2.text());
        assert_eq!((c1.target_at(4), c2.target_at(4), c1.bg), (Some(&go6()), Some(&go6()), c2.bg));
        // The dock: every space a row in ⌘N order, the pending one at 3.
        let dock: Vec<&Row> = f.rows[f.spaces.clone()].iter().collect();
        assert_eq!(dock.len(), 4, "{t:#?}");
        let d3 = dock[2];
        assert!(d3.text().contains(" 3  creating…"), "{:?}", d3.text());
        assert_eq!(d3.target_at(2), Some(&pending_target()));
        assert!(d3.segs.iter().any(|s| s.italic && s.text == CREATING));
        assert_eq!(dock[3].target_at(2), Some(&s("notes")));
    }
    // Chips (the dock's last resort): the same click.
    let chips = spaces_rows(&v, 34, SpacesForm::Chips(2), false);
    assert!(chips.iter().any(|r| r.targets.iter().any(|t| t.2 == pending_target()) && r.text().contains("creating")));
    // Painted: `creating…` in italics (SGR 3).
    let f = layout(&v, 44, 60, &Opts::default());
    let mut buf = Buffer::empty(Rect::new(0, 0, 44, 60));
    paint(&f, &mut buf);
    assert!(to_ansi(&buf).contains(";3mcreating…"));
}

/// Its claude asks: the header says so with the counts (no spinner, no
/// animation), the card's second line is the question (not the request),
/// and it is a NEEDS YOU card like any, its key-cap solid in the pending
/// colour, clicking through to it (the popup); the header counts it.
#[test]
fn a_pending_claude_that_asks_needs_you() {
    let v = pend("needs-input", &[("detail_kind", "ask"), ("detail", "Which crate should it use?")]);
    assert_eq!(v.counts.get(Cat::NeedsInput), 2);
    let mut needs: Vec<&str> = v.needs.iter().map(|n| n.agent.window_id.as_str()).collect();
    needs.sort_unstable();
    assert_eq!(needs, ["@5", "@6"]);
    let f = layout(&v, 44, 60, &Opts::default());
    let y = f.agents.clone().find(|&y| f.rows[y].targets.first().map(|x| &x.2) == Some(&pending_target())).unwrap();
    let h = &f.rows[y];
    assert!(!h.anim && h.text().starts_with("  3  creating…") && h.text().trim_end().ends_with("◉1"), "{:?}", h.text());
    assert!(f.rows[y + 2].text().contains("Which crate should it use?"), "{:?}", f.rows[y + 2].text());
    assert!(!f.rows[y + 2].text().contains(REQ));
    let card = f.needs.clone().find(|&y| f.rows[y].target_at(4) == Some(&go6())).expect("a NEEDS YOU card");
    assert!(f.rows[card].segs.contains(&block(" f ", ink_on(PINK), PINK)), "{:?}", f.rows[card].segs);
    assert!(f.rows[card + 1].text().contains("Which crate should it use?"));
}

/// Space scope: AGENTS is the client's space alone; the dock still lists
/// the pending space at its number, its click the popup.
#[test]
fn space_scope_keeps_it_in_the_dock() {
    let v = pend_with("running", &[], "on", "", false);
    assert_eq!(v.scope.session.as_deref(), Some("main"));
    let f = layout(&v, 34, 60, &Opts::default());
    assert_eq!(header_targets(&f), [Target::Session("main".into())]);
    assert!(f.rows[f.spaces.clone()].iter().any(|r| r.target_at(2) == Some(&pending_target())));
}

/// Pinned, it moves into PINNED like any agent (its space's colour on its
/// key-cap, `creating` where a space's name would be); its space keeps its
/// dock row (and, with nothing left to list, no group in AGENTS).
#[test]
fn a_pending_claude_pins() {
    let v = pend("running", &[("pinned", "100")]);
    let f = layout(&v, 44, 60, &Opts::default());
    let p: Vec<&Row> = f.rows[f.pinned.clone()].iter().collect();
    assert!(p.len() >= 3 && p[1].covers == [("pending".to_string(), "@6".to_string())], "{:#?}", texts(&f));
    assert!(p[2].text().contains("creating"), "{:?}", p[2].text());
    assert_eq!(p[1].target_at(4), Some(&go6()));
    assert!(!header_targets(&f).contains(&pending_target()));
    assert!(f.rows[f.spaces.clone()].iter().any(|r| r.target_at(2) == Some(&pending_target())));
}

/// `@spaces` off: nothing changes. The frame is exactly the one the same
/// windows give without any pending option (the `pending` session the
/// hidden group it always was, by name, never in the dock).
#[test]
fn spaces_off_is_unchanged() {
    for st in ["running", "needs-input"] {
        let v = pend_with(st, &[], "", "", false);
        let bare = pend_with(st, &[], "", "", true);
        for (w, h) in [(44u16, 60u16), (34, 30)] {
            let (a, b) = (layout(&v, w, h, &Opts::default()), layout(&bare, w, h, &Opts::default()));
            assert_eq!(a, b, "{st} {w}x{h}");
            assert!(!texts(&a).iter().any(|l| l.contains("creating")), "{:#?}", texts(&a));
            assert!(a.rows.iter().all(|r| r.targets.iter().all(|t| !matches!(t.2, Target::Pending(_)))));
        }
    }
}
