//! STATE and RECENCY fold the agents section (2026-11-10): a DORMANT space
//! (every agent idle, not prompted by Mack for `@space_fold_hours`) is its
//! header and counts alone even with rows to spare; under an open header an
//! idle agent out of mind folds into the `○ K idle` line; ACTIVE scope
//! leaves the dormant spaces out (one `… K quiet spaces` line); a space not
//! prompted for days dims and says how long. A window without `@thread_at`
//! counts from its `@agent_since` while idle; with neither it is fresh.

use super::*;

const H: i64 = 3600;

/// `@thread_at` `ago` seconds before NOW.
fn prompted(x: A<'static>, ago: i64) -> A<'static> {
    let at = (NOW - ago).to_string();
    x.with("thread_at", &at)
}

/// Idle since `ago` seconds before NOW (its state stamp).
fn idle_for(x: A<'static>, ago: i64) -> A<'static> {
    let since = format!("{} idle", NOW - ago);
    let mut x = x;
    x.set.retain(|(k, _)| *k != "since");
    x.with("since", &since)
}

/// An idle agent idle and unprompted for `ago`.
fn quiet(s: &'static str, i: u32, title: &'static str, ago: i64) -> A<'static> {
    prompted(idle_for(a(s, i, "idle", title), ago), ago)
}

fn home(s: &'static str, n: &str) -> A<'static> {
    a(s, 0, "", "zsh").with("home", "1").with("space_index", n)
}

/// THE fleet (9 spaces, 17 agents; the client on main's agent @2):
/// - main ⌘1: working (prompted a minute ago), two idle out of mind (5h, 10h);
/// - cua-notch ⌘2: asking, working, idle prompted 30 minutes ago;
/// - schedule ⌘3: three idle, prompted 6h ago: DORMANT;
/// - bai ⌘4: two idle, prompted 3 days ago: DORMANT and STALE (`3d`);
/// - errands ⌘5: one idle 30h, NO stamp (a tab Mack never typed into): its
///   `@agent_since` stands in: DORMANT;
/// - mackhaymond.co ⌘6: two idle, 5h: dormant, but kept open by hand;
/// - notes ⌘7: working on a peer's behalf, Mack's last prompt 2 days ago;
/// - canvas ⌘8: two idle, 20h, and a shell: DORMANT;
/// - photos ⌘9: its home alone (bare).
pub(super) fn fleet_agents() -> Vec<A<'static>> {
    let ix = |x: A<'static>, n: &str| x.with("space_index", n);
    vec![
        home("main", "1"),
        ix(prompted(a("main", 1, "running", "Tmux Agent Sidebar"), 60), "1"),
        ix(quiet("main", 2, "Notch Tasks Orchestrator", 5 * H), "1"),
        ix(quiet("main", 3, "Lost suit jacket", 10 * H), "1"),
        home("cua-notch", "2"),
        ix(prompted(a("cua-notch", 1, "needs-input", "Notch hover animation"), 2 * H)
            .with("detail_kind", "ask").with("detail", "Which spring?"), "2"),
        ix(prompted(a("cua-notch", 2, "running", "Island resize spring"), 20 * 60), "2"),
        ix(prompted(idle_for(a("cua-notch", 3, "idle", "Popup shadow"), 25 * 60), 30 * 60), "2"),
        home("schedule", "3"),
        ix(quiet("schedule", 1, "P0a multi-user execution", 6 * H), "3"),
        ix(quiet("schedule", 2, "Prose extraction model", 7 * H), "3"),
        ix(quiet("schedule", 3, "Calendar sync", 8 * H), "3"),
        home("bai", "4"),
        ix(quiet("bai", 1, "Application link", 3 * 86_400), "4"),
        ix(quiet("bai", 2, "BAI Info Session Slides", 3 * 86_400 + H), "4"),
        home("errands", "5"),
        ix(idle_for(a("errands", 1, "idle", "Dry cleaning"), 30 * H), "5"),
        home("mackhaymond.co", "6"),
        ix(quiet("mackhaymond.co", 1, "Launchpad catalog", 5 * H), "6"),
        ix(quiet("mackhaymond.co", 2, "Tunnel hostname", 6 * H), "6"),
        home("notes", "7"),
        ix(prompted(a("notes", 1, "running", "Weekly review"), 2 * 86_400), "7"),
        home("canvas", "8"),
        ix(quiet("canvas", 1, "Syllabus sync", 20 * H), "8"),
        ix(quiet("canvas", 2, "Grade export", 21 * H), "8"),
        ix(a("canvas", 3, "", "zsh"), "8"),
        home("photos", "9"),
    ]
}

/// The fleet in `scope` (`@spaces` on), mackhaymond.co kept open by hand.
pub(super) fn fleet(scope: &str) -> ViewModel {
    let mut v = scoped(fleet_agents(), "@2", "main", "on", scope, "");
    for sp in v.spaces.iter_mut().filter(|s| s.name == "mackhaymond.co") {
        sp.open = true;
    }
    v
}

fn header_row<'a>(f: &'a Frame, name: &str) -> &'a Row {
    &f.rows[head_any(f, name)]
}

/// The row of `name`'s header (a fold chip or not).
fn head_any(f: &Frame, name: &str) -> usize {
    f.agents
        .clone()
        .find(|&y| is_group_header(&f.rows[y], usize::from(f.width)) && f.rows[y].targets.iter().any(|t| t.2 == Target::Session(name.into())))
        .unwrap_or_else(|| panic!("no {name:?} header in {:#?}", texts(f)))
}

/// The windows a row stands for, by title.
fn covered(v: &ViewModel, r: &Row) -> Vec<String> {
    r.covers
        .iter()
        .filter_map(|(s, id)| v.agents().chain(v.pinned.iter()).find(|a| &a.session == s && &a.window_id == id).map(|a| a.title.clone()))
        .collect()
}

/// How many rows the agent titled `title` takes (its card: 2; one line: 1;
/// 0: no row of its own, folded or gone), checking each stands for it alone
/// and goes to it.
fn rows_of(v: &ViewModel, f: &Frame, title: &str) -> usize {
    let a = v.agents().find(|a| a.title == title).unwrap_or_else(|| panic!("no agent {title:?}"));
    let me = vec![(a.session.clone(), a.window_id.clone())];
    let go = Target::Go { win: a.window_id.clone(), session: a.session.clone() };
    let Some(y) = f.agents.clone().find(|&y| f.rows[y].covers == me) else { return 0 };
    assert!(f.rows[y].targets.iter().all(|t| t.2 == go || matches!(t.2, Target::Diff(_))), "{title}: {:?}", f.rows[y].targets);
    let second = f.rows.get(y + 1).is_some_and(|r| r.covers.is_empty() && r.target_at(4) == Some(&go));
    1 + usize::from(second)
}

/// The agents listed under `name`'s header: the rows after it to the next
/// header (or the section's end), as text.
fn under(f: &Frame, name: &str) -> Vec<String> {
    let y = head_any(f, name);
    let t = texts(f);
    (y + 1..f.agents.end).take_while(|&k| !is_group_header(&f.rows[k], usize::from(f.width)) && !is_blank(&f.rows[k])).map(|k| t[k].clone()).collect()
}

/// A dormant space is its header and counts alone at full height (rows to
/// spare), standing for all its agents, its shell gone with it; the
/// chevron `▸` ends its counts, and a click there is the fold chip
/// ([`Target::Fold`]) while the rest of the header goes home.
#[test]
fn a_dormant_space_folds_to_its_header() {
    let v = fleet("all");
    let f = layout(&v, 34, 90, &Opts::default());
    let t = texts(&f);
    assert_eq!(f.agents_step, 0, "{t:#?}");
    // (errands: no stamp at all, idle 30h by its state stamp: dormant too.)
    for name in ["schedule", "bai", "errands", "canvas"] {
        let r = header_row(&f, name);
        assert!(under(&f, name).is_empty(), "{name}: {t:#?}");
        let sp = v.spaces.iter().find(|s| s.name == name).unwrap();
        assert_eq!(r.covers.len(), sp.agents.len(), "{name}");
        let l = r.text();
        assert!(l.trim_end().ends_with(FOLD_SHUT), "{name}: {l:?}");
        let x = col(&l, FOLD_SHUT);
        assert_eq!(r.target_at(x), Some(&Target::Fold(name.into())), "{name}");
        assert_eq!(r.target_at(x - 2), Some(&Target::Fold(name.into())), "{name}: the counts too");
        assert_eq!(r.target_at(4), Some(&Target::Session(name.into())), "{name}: the number goes home");
        assert_eq!(r.target_at(x + 1), Some(&Target::Session(name.into())), "{name}: the pad goes home");
    }
    assert!(!t.iter().any(|l| l.contains("Syllabus") || l.contains("❯ ")), "canvas folded, its shell too: {t:#?}");
    check_accounting("dormant", &v, &f);
}

/// What never folds as dormant: the client's space, a space with an
/// attention agent (asking, failed, done), working or paused, a pending
/// space, one kept open by hand, one with an agent with neither a stamp
/// nor a state stamp, a prompt just answered; and nothing with
/// `@space_fold_hours` 0.
#[test]
fn what_never_folds() {
    let v = fleet("all");
    let f = layout(&v, 34, 90, &Opts::default());
    let t = texts(&f);
    for name in ["main", "cua-notch", "mackhaymond.co", "notes"] {
        assert!(!under(&f, name).is_empty(), "{name}: {t:#?}");
    }
    // Kept open: the chevron says so (▾), and its chip still toggles.
    let r = header_row(&f, "mackhaymond.co");
    assert!(r.text().trim_end().ends_with(FOLD_OPEN), "{:?}", r.text());
    assert_eq!(r.target_at(col(&r.text(), FOLD_OPEN)), Some(&Target::Fold("mackhaymond.co".into())));
    // No chip where folding is not the dormancy's.
    for name in ["main", "cua-notch", "notes"] {
        assert!(!header_row(&f, name).targets.iter().any(|t| matches!(t.2, Target::Fold(_))), "{name}");
    }
    // Each exception on its own: schedule (dormant) with one change.
    let one = |change: &dyn Fn(A<'static>) -> A<'static>| {
        let ag: Vec<A<'static>> = fleet_agents().into_iter().map(|x| if x.s == "schedule" && x.i == 2 { change(x) } else { x }).collect();
        scoped(ag, "@2", "main", "on", "all", "")
    };
    let open = |v: &ViewModel, why: &str| {
        let f = layout(v, 34, 90, &Opts::default());
        assert!(!under(&f, "schedule").is_empty(), "{why}: {:#?}", texts(&f));
    };
    for st in ["needs-input", "failed", "done", "running", "paused"] {
        open(&one(&|x: A<'static>| A { st, ..x }), st);
    }
    open(&one(&|x: A<'static>| { let mut x = x; x.set.retain(|(k, _)| *k != "thread_at" && *k != "since"); x }),
        "neither stamp: fresh");
    open(&one(&|x: A<'static>| { let mut x = x; x.set.retain(|(k, _)| *k != "thread_at"); idle_for(x, 30 * 60) }),
        "no stamp, idle half an hour: its state stamp is fresh");
    open(&one(&|x: A<'static>| prompted(x, H)), "prompted an hour ago");
    open(&one(&|x: A<'static>| x.with("pending", &(NOW - 30).to_string())), "a prompt just answered");
    open(&one(&|x: A<'static>| x.with("workflow", "1")), "work out");
    // The client on schedule's home: it is the client's space.
    let ag = fleet_agents();
    let home_id = format!("@{}", ag.iter().position(|x| x.s == "schedule" && x.i == 0).unwrap() + 1);
    let v = scoped(ag, &home_id, "schedule", "on", "all", "");
    open(&v, "the client's space");
    // Off.
    let v = fleet("all");
    let f = layout(&v, 34, 90, &Opts { bar: Bar { fold_after: Some(0), ..Bar::default() }, ..Opts::default() });
    assert!(!under(&f, "schedule").is_empty() && !under(&f, "bai").is_empty(), "{:#?}", texts(&f));
    // A longer N: schedule (6h) is not dormant at 7 hours, canvas (20h) is.
    let f = layout(&v, 34, 90, &Opts { bar: Bar { fold_after: Some(7 * H), ..Bar::default() }, ..Opts::default() });
    assert!(!under(&f, "schedule").is_empty() && under(&f, "canvas").is_empty(), "{:#?}", texts(&f));
}

/// A pending space never folds (its claude is its one card).
#[test]
fn a_pending_space_never_folds() {
    let mut v = fleet("all");
    for sp in v.spaces.iter_mut().filter(|s| s.name == "schedule") {
        sp.pending = Some("@99".into());
    }
    let f = layout(&v, 34, 90, &Opts::default());
    assert!(!f.rows.iter().any(|r| r.targets.iter().any(|t| t.2 == Target::Fold("schedule".into()))), "{:#?}", texts(&f));
}

/// Under an open header, idle agents out of mind (idle and unprompted for
/// N) fold into the space's one idle line even at full height, one alone
/// too; working, asking and recently prompted agents keep their cards. An
/// agent without a stamp counts from its state stamp while idle; with
/// neither it is fresh (its card stays).
#[test]
fn idle_agents_fold_within_an_open_space() {
    let v = lettered(fleet("all"));
    let f = layout(&v, 34, 90, &Opts::default());
    let t = texts(&f);
    // main: its working agent a card, each agent out of mind ONE row of its
    // own (letter, title in full, age), never merged into an idle line.
    assert_eq!(rows_of(&v, &f, "Tmux Agent Sidebar"), 2, "{t:#?}");
    for title in ["Notch Tasks Orchestrator", "Lost suit jacket"] {
        assert_eq!(rows_of(&v, &f, title), 1, "{title}: {t:#?}");
    }
    assert!(!t.iter().any(|l| l.contains(" idle ·")), "no merged line at full height: {t:#?}");
    let a = v.agents().find(|a| a.title == "Notch Tasks Orchestrator").unwrap();
    let y = f.agents.clone().find(|&y| f.rows[y].covers.len() == 1 && f.rows[y].covers[0].1 == a.window_id).unwrap();
    let l = &t[y];
    let letter = a.letter.expect("a letter");
    // (24 cells of title at 34 columns: the age makes way for it.)
    assert!(l.contains(&format!(" {letter}  Notch Tasks Orchestrator")), "letter and the whole title: {l:?}");
    assert_eq!(f.rows[y].target_at(20), Some(&Target::Go { win: a.window_id.clone(), session: a.session.clone() }));
    // A shorter title keeps its age.
    let lc = &t[find(&f, "Launchpad catalog")];
    assert!(lc.trim_end().ends_with("5h"), "its age: {lc:?}");
    // One background per row (no segment paints its own inside the tile
    // but the key-cap), the zebra going on: neighbours differ.
    assert!(f.rows[y].bg.is_some());
    assert_ne!(f.rows[y].bg, f.rows[y + 1].bg, "neighbours differ");
    assert_ne!(f.rows[y].bg, f.rows[y - 1].bg, "neighbours differ");
    // A title wider than the row is the row's first claim: the age goes first.
    let long = "An agent with a very long title that cannot fit at all";
    let ag: Vec<A<'static>> = fleet_agents().into_iter().map(|x| if x.title == "Lost suit jacket" { A { title: long, ..x } } else { x }).collect();
    let vl = lettered(scoped(ag, "@2", "main", "on", "all", ""));
    let fl = layout(&vl, 34, 90, &Opts::default());
    let yl = find(&fl, "An agent with");
    assert!(texts(&fl)[yl].contains("An agent with a very lo") && !texts(&fl)[yl].contains("10h"), "{:?}", texts(&fl)[yl]);
    // cua-notch: its idle agent prompted 30 minutes ago keeps its card.
    assert_eq!(rows_of(&v, &f, "Popup shadow"), 2, "{t:#?}");
    // Unstamped in main: idle 10h by its state stamp goes to one line; with
    // no state stamp either it is fresh and keeps its card.
    let strip = |x: A<'static>, keys: &[&str]| { let mut x = x; x.set.retain(|(k, _)| !keys.contains(k)); x };
    let ag: Vec<A<'static>> = fleet_agents().into_iter().map(|x| match x.title {
        "Lost suit jacket" => strip(x, &["thread_at"]),
        "Notch Tasks Orchestrator" => strip(x, &["thread_at", "since"]),
        _ => x,
    }).collect();
    let v2 = scoped(ag, "@2", "main", "on", "all", "");
    let f2 = layout(&v2, 34, 90, &Opts::default());
    assert_eq!((rows_of(&v2, &f2, "Lost suit jacket"), rows_of(&v2, &f2, "Notch Tasks Orchestrator")), (1, 2), "{:#?}", texts(&f2));
    // mackhaymond.co, kept open: its two idle agents one line each.
    assert_eq!((rows_of(&v, &f, "Launchpad catalog"), rows_of(&v, &f, "Tunnel hostname")), (1, 1), "{t:#?}");
    // A longer N (12h): main's 5h and 10h agents keep their cards.
    let f = layout(&v, 34, 90, &Opts { bar: Bar { fold_after: Some(12 * H), ..Bar::default() }, ..Opts::default() });
    assert_eq!((rows_of(&v, &f, "Notch Tasks Orchestrator"), rows_of(&v, &f, "Lost suit jacket")), (2, 2), "{:#?}", texts(&f));
}

/// Under ROW pressure the ladder still merges idle agents into one line,
/// and there each name follows its letter (bold, in its space's key-cap
/// colour), a click on a name going to that agent; the rest of the line is
/// the menu's Idle tab. The line stands for exactly them.
#[test]
fn the_pressure_line_names_each_agent_by_its_letter() {
    let ag: Vec<A<'static>> = fleet_agents().into_iter().filter(|x| x.s == "main" || x.s == "cua-notch").collect();
    let v = lettered(scoped(ag, "@2", "main", "on", "all", ""));
    let (h, f) = (8u16..40).map(|h| (h, layout(&v, 34, h, &Opts::default())))
        .find(|(_, f)| f.agents_step >= 1 && texts(f).iter().any(|l| l.contains("2 idle ·")))
        .expect("a height that folds the idle line");
    let t = texts(&f);
    let y = t.iter().position(|l| l.contains("2 idle ·")).unwrap();
    let notch = v.agents().find(|a| a.title == "Notch Tasks Orchestrator").unwrap();
    let suit = v.agents().find(|a| a.title == "Lost suit jacket").unwrap();
    let (kn, ks) = (notch.letter.unwrap(), suit.letter.unwrap());
    assert!(t[y].contains(&format!("2 idle · {kn} Notc")), "{h}: {:?}", t[y]);
    let mut got = covered(&v, &f.rows[y]);
    got.sort();
    assert_eq!(got, ["Lost suit jacket", "Notch Tasks Orchestrator"]);
    let r = &f.rows[y];
    let ink = space_colour(v.spaces.iter().find(|s| s.name == "main").unwrap());
    assert!(r.segs.iter().any(|s| s.text == format!("{kn} ") && s.bold && s.fg == name_ink(ink)), "{:?}", r.segs);
    let go = |a: &Agent| Target::Go { win: a.window_id.clone(), session: a.session.clone() };
    assert_eq!(r.target_at(col(&t[y], &format!("{kn} Notc"))), Some(&go(notch)));
    assert_eq!(r.target_at(col(&t[y], "Notc") + 2), Some(&go(notch)));
    assert_eq!(r.target_at(3), Some(&Target::Menu(Some("idle"))));
    // The second name, when the row shows it, goes to its agent.
    if let Some(i) = t[y].find(&format!("{ks} Lost")) {
        assert_eq!(r.target_at(t[y][..i].chars().count() as u16), Some(&go(suit)));
    }
    assert_eq!(width(&r.segs), 34);
    check_accounting("pressure", &v, &f);
}

/// UNSAVED WORK (the stale sweep's dirty verdict) keeps the agent's card and
/// its space open, however old: its hint is a risk worth seeing. A clean
/// one (`close?`) folds.
#[test]
fn unsaved_work_never_folds() {
    use std::collections::BTreeMap;
    let mut v = fleet("all");
    let id = |v: &ViewModel, t: &str| v.agents().find(|a| a.title == t).unwrap().window_id.clone();
    let dirty = super::stale::ins(Some((5, 0, 1)), true);
    let clean = super::stale::ins(None, true);
    let map: BTreeMap<String, crate::insight::Insight> = [
        (id(&v, "Lost suit jacket"), dirty.clone()),
        (id(&v, "Notch Tasks Orchestrator"), clean.clone()),
        (id(&v, "Calendar sync"), dirty),
        (id(&v, "Syllabus sync"), clean),
    ].into_iter().collect();
    crate::insight::fill(&mut v, &map);
    let f = layout(&v, 44, 90, &Opts::default());
    let t = texts(&f);
    let main = under(&f, "main");
    assert!(main.iter().any(|l| l.contains("Lost suit jacket")), "{t:#?}");
    assert!(main.iter().any(|l| l.contains("unsaved")), "{t:#?}");
    assert_eq!((rows_of(&v, &f, "Lost suit jacket"), rows_of(&v, &f, "Notch Tasks Orchestrator")), (2, 1), "{t:#?}");
    assert!(!under(&f, "schedule").is_empty(), "schedule holds unsaved work: {t:#?}");
    assert!(under(&f, "canvas").is_empty(), "canvas is clean: {t:#?}");
}

/// The client's own idle agent never folds, however old.
#[test]
fn the_clients_own_agent_never_folds() {
    // The client on main's Lost suit jacket (@4).
    let v = scoped(fleet_agents(), "@4", "main", "on", "all", "");
    let f = layout(&v, 34, 90, &Opts::default());
    assert_eq!((rows_of(&v, &f, "Lost suit jacket"), rows_of(&v, &f, "Notch Tasks Orchestrator")), (2, 1), "{:#?}", texts(&f));
}

/// A missing `@thread_at` falls back to the idle window's `@agent_since`
/// (never older than the real last prompt): a fleet with no stamp at all
/// (right after the deploy) still folds what has been idle for N. With
/// neither stamp a window is FRESH: such a fleet folds nothing and dims
/// nothing (the old sidebar, exactly).
#[test]
fn missing_stamps_fall_back_to_the_state_stamp_else_fresh() {
    let strip = |ag: Vec<A<'static>>, keys: &'static [&'static str]| -> Vec<A<'static>> {
        ag.into_iter().map(|mut x| { x.set.retain(|(k, _)| !keys.contains(k)); x }).collect()
    };
    let v = scoped(strip(fleet_agents(), &["thread_at"]), "@2", "main", "on", "all", "");
    let f = layout(&v, 34, 90, &Opts::default());
    let t = texts(&f);
    for name in ["schedule", "bai", "errands", "canvas"] {
        assert!(under(&f, name).is_empty(), "{name}: {t:#?}");
    }
    assert!(header_row(&f, "bai").text().contains("3d"), "{t:#?}");
    assert_eq!((rows_of(&v, &f, "Lost suit jacket"), rows_of(&v, &f, "Notch Tasks Orchestrator")), (1, 1), "{t:#?}");
    // Neither stamp: fresh.
    let v = scoped(strip(fleet_agents(), &["thread_at", "since"]), "@2", "main", "on", "all", "");
    let f = layout(&v, 34, 90, &Opts::default());
    let t = texts(&f);
    assert!(!t.iter().any(|l| l.contains(" idle ·") || l.contains(FOLD_SHUT) || l.contains(FOLD_OPEN)), "{t:#?}");
    for sp in v.spaces.iter().filter(|s| !s.agents.is_empty()) {
        assert!(!under(&f, &sp.name).is_empty(), "{}: {t:#?}", sp.name);
        assert!(!header_row(&f, &sp.name).text().contains("3d"));
    }
    // Same as with folding off.
    let off = layout(&v, 34, 90, &Opts { bar: Bar { fold_after: Some(0), ..Bar::default() }, ..Opts::default() });
    assert_eq!(texts(&off), t);
}

/// Active scope: every space but the dormant (and bare) ones, which one
/// ` … K quiet spaces` line counts and stands for (a click is the next
/// scope); the AGENTS hint says `⌥V active`.
#[test]
fn active_scope_leaves_the_quiet_spaces_out() {
    let v = fleet("active");
    assert!(v.scope.active() && v.scope.session.is_none());
    let f = layout(&v, 34, 60, &Opts::default());
    let t = texts(&f);
    let names: Vec<String> = f.agents.clone().filter(|&y| is_group_header(&f.rows[y], 34))
        .filter_map(|y| f.rows[y].targets.iter().find_map(|t| match &t.2 { Target::Session(s) => Some(s.clone()), _ => None }))
        .collect();
    assert_eq!(names, ["main", "cua-notch", "mackhaymond.co", "notes"], "{t:#?}");
    let y = find(&f, "quiet spaces");
    assert!(t[y].starts_with(" … 4 quiet spaces"), "{:?}", t[y]);
    assert_eq!(f.rows[y].target_at(3), Some(&Target::Scope));
    let mut q = covered(&v, &f.rows[y]);
    q.sort();
    assert_eq!(q.len(), 3 + 2 + 1 + 2, "{q:?}");
    let a = find(&f, " AGENTS");
    assert!(t[a].contains("⌥V active"), "{:?}", t[a]);
    // Every agent of the scope accounted for, at every height and width.
    for w in [24u16, 34, 42] {
        for h in 4u16..=60 {
            let f = layout(&v, w, h, &Opts::default());
            assert_eq!(f.rows.len(), usize::from(h));
            for r in &f.rows {
                assert_eq!(width(&r.segs), usize::from(w), "{w}x{h}: {:?}", r.text());
            }
            check_accounting(&format!("active {w}x{h}"), &v, &f);
        }
    }
    // Nothing active at all: the words say so, the line still counts.
    let ag: Vec<A<'static>> = fleet_agents().into_iter().filter(|x| ["schedule", "bai", "photos", "main"].contains(&x.s)).collect();
    let ag: Vec<A<'static>> = ag.into_iter().filter(|x| x.s != "main" || x.i == 0).collect();
    let v = scoped(ag, "@1", "main", "on", "active", "");
    let f = layout(&v, 34, 40, &Opts::default());
    let t = texts(&f);
    assert!(t.iter().any(|l| l.starts_with("   all quiet")) && t.iter().any(|l| l.starts_with(" … 2 quiet spaces")), "{t:#?}");
    check_accounting("all quiet", &v, &f);
}

/// A space Mack has not prompted for two days or more, nothing live in
/// it, dims: its number a quiet chip (its colour on a tint), its name in
/// the dim ink, and the age (`3d`) before its counts. Never the client's
/// space, never one with work or attention, never with `@space_fold_hours`
/// 0.
#[test]
fn a_stale_space_dims_and_says_how_long() {
    let v = fleet("all");
    let f = layout(&v, 34, 90, &Opts::default());
    let r = header_row(&f, "bai");
    let l = r.text();
    assert!(l.contains(" 3d ") || l.contains(" 3d"), "{l:?}");
    let name = r.segs.iter().find(|s| s.text == "bai").unwrap();
    assert_eq!(name.fg, Ink::Pal(LABEL));
    let chip = r.segs.iter().find(|s| s.text == " 4 ").unwrap();
    assert!(chip.bg.is_some() && chip.bg != Some(chip.fg), "a quiet chip: {chip:?}");
    let age = r.segs.iter().find(|s| s.text == "3d").unwrap();
    assert_eq!(age.fg, Ink::Pal("overlay"));
    // schedule (6h), canvas (20h): not stale; notes (2 days, working): live.
    for name in ["schedule", "canvas", "notes", "main", "errands"] {
        let r = header_row(&f, name);
        assert!(!r.segs.iter().any(|s| s.text.ends_with('d') && s.text.trim_end_matches('d').parse::<u32>().is_ok()), "{name}: {:?}", r.text());
    }
    // The age leaves the fold chip where it was: counts and chevron.
    let x = col(&l, FOLD_SHUT);
    assert_eq!(r.target_at(x), Some(&Target::Fold("bai".into())));
    assert_eq!(r.target_at(col(&l, "3d")), Some(&Target::Session("bai".into())));
    // `@space_fold_hours 0` turns the nudge off with the folding.
    let f = layout(&v, 34, 90, &Opts { bar: Bar { fold_after: Some(0), ..Bar::default() }, ..Opts::default() });
    let r = header_row(&f, "bai");
    assert!(!r.text().contains("3d"), "{:?}", r.text());
    assert_eq!(r.segs.iter().find(|s| s.text == "bai").unwrap().fg, name_ink(space_colour(v.spaces.iter().find(|s| s.name == "bai").unwrap())));
}

/// The one-line shape NEEDS YOU is weighed against ([`agents_one_line`])
/// counts the forms that fold at every step (a dormant space its header,
/// the agents out of mind their one idle line): exactly the rows the
/// agents section takes one line each. Overcounting it would cut NEEDS
/// YOU to `… N need you` while rows are free.
#[test]
fn the_one_line_shape_counts_what_folds() {
    for scope in ["all", "active"] {
        let v = fleet(scope);
        let cx = Cx { w: 34, b: Badge::of(&v, &Bar::default()), now: v.now, spin: 0, home: home_of(&v), cur: v.cur_win.as_deref(),
            shells: v.shells, resumes: false, stale_after: crate::view::STALE_AFTER_DEFAULT, fold_after: crate::view::FOLD_AFTER_DEFAULT };
        let n = agents_one_line(&v, cx);
        let (rows, step) = agents_section(&v, cx, n, false, false);
        assert!(step <= 2 && rows.len() == n, "{scope}: {n} vs {} at step {step}: {:#?}", rows.len(), rows.iter().map(Row::text).collect::<Vec<_>>());
        // Not one row to spare: one fewer and the one-line shape no longer fits.
        assert!(agents_section(&v, cx, n - 1, false, false).1 > 2, "{scope}");
    }
    // Whole frames: four asking agents and a space of twelve agents out of
    // mind (one idle line, never twelve rows): NEEDS YOU is never cut while
    // the frame still has blank rows above the dock.
    let mut ag: Vec<A<'static>> = vec![home("main", "1"), a("main", 1, "running", "Busy").with("space_index", "1")];
    for i in 1..=4 {
        ag.push(a("asks", i, "needs-input", "Which one?").with("detail_kind", "ask").with("detail", "pick").with("space_index", "2"));
    }
    for i in 1..=12 {
        ag.push(quiet("old", i, "Long ago", 10 * H).with("space_index", "3"));
    }
    ag.push(a("old", 13, "running", "Still going").with("space_index", "3"));
    let v = scoped(ag, "@2", "main", "on", "all", "");
    let mut cut = 0;
    for h in 12u16..=60 {
        let f = layout(&v, 34, h, &Opts::default());
        let t = texts(&f);
        if t.iter().any(|l| l.contains("need you")) {
            cut += 1;
            let blank = f.rows[f.agents.end..f.dock.start].iter().filter(|r| is_blank(r)).count();
            assert_eq!(blank, 0, "{h}: {t:#?}");
        }
        check_accounting(&format!("one line {h}"), &v, &f);
    }
    assert!(cut > 0, "some height cuts NEEDS YOU");
}

/// Every frame of the fleet, in every scope: exactly `width` cells a row,
/// two-row cards, every agent accounted for.
#[test]
fn fleet_frames_hold_at_every_size() {
    for (scope, v) in ["all", "active", "space"].into_iter().flat_map(|s| [(s, fleet(s)), (s, lettered(fleet(s)))]) {
        for w in [16u16, 24, 34, 44] {
            for h in 2u16..=70 {
                let f = layout(&v, w, h, &Opts::default());
                assert_eq!(f.rows.len(), usize::from(h));
                for r in &f.rows {
                    assert_eq!(width(&r.segs), usize::from(w), "{scope} {w}x{h}: {:?}", r.text());
                }
                check_accounting(&format!("{scope} {w}x{h}"), &v, &f);
            }
        }
    }
}

/// One space, one working agent and three out of mind, lettered, at 34
/// columns: roomy (each its own line) and under row pressure (the ladder's
/// lettered idle line) (`cargo test --lib fold::render_one_line --
/// --ignored --nocapture`).
#[test]
#[ignore]
fn render_one_line() {
    let ix = |x: A<'static>| x.with("space_index", "1");
    let ag = vec![
        home("main", "1"),
        ix(prompted(a("main", 1, "running", "Tmux Agent Sidebar"), 60)),
        ix(quiet("main", 2, "Notch Tasks Orchestrator", 5 * H)),
        ix(quiet("main", 3, "Lost suit jacket", 10 * H)),
        ix(quiet("main", 4, "Backyard Halloween", 26 * H)),
        home("bai", "2"),
        ix(prompted(a("bai", 1, "needs-input", "Application link"), 600).with("detail_kind", "ask").with("detail", "Which deadline?"))
            .with("space_index", "2"),
    ];
    let v = lettered(scoped(ag, "@2", "main", "on", "all", ""));
    for h in [30u16, 15] {
        let f = layout(&v, 34, h, &Opts::default());
        println!("--- 34x{h} (agents step {})", f.agents_step);
        for r in &f.rows {
            println!("|{}|", initials(&r.text()));
        }
    }
}

/// The before / after renders the review asks for (`cargo test --lib
/// fold::render -- --ignored --nocapture`): the fleet at 34x60, all scope
/// and active scope, folding off (as before) and on.
#[test]
#[ignore]
fn render() {
    let show = |title: &str, v: &ViewModel, opts: &Opts| {
        let f = layout(v, 34, 60, opts);
        println!("--- {title}");
        for r in &f.rows {
            println!("|{}|", initials(&r.text()));
        }
    };
    let off = Opts { bar: Bar { fold_after: Some(0), ..Bar::default() }, ..Opts::default() };
    show("all spaces, folding off", &fleet("all"), &off);
    show("all spaces, folding on (4h)", &fleet("all"), &Opts::default());
    show("active, folding on (4h)", &fleet("active"), &Opts::default());
}
