//! Rate-limited agents (docs/agent-roster.md "Rate-limited agents"): a
//! PAUSED agent with `@agent_limit`. Its card wears the limit mark in peach
//! and says when the limit resets (or that it lifted, with a resume chip);
//! the rate-limit banner says how many, the earliest reset and the
//! auto-resume setting; it is never in NEEDS YOU.

use super::*;
use crate::text::when;

const H: i64 = 3600;

fn flag() -> String {
    (NOW - 300).to_string()
}

/// `@agent_limit` for a reset `r` (epoch; 0 unknown) of `kind`.
fn lim(r: i64, kind: &str) -> String {
    format!("{r} {kind} {}", NOW - 300)
}

/// Limited agents of every kind, a plain paused one, one Mack prompted
/// again (running, `@agent_limit` kept until the turn's outcome), and the
/// usual neighbours.
fn limited_view() -> ViewModel {
    let f = flag();
    view(
        vec![
            a("main", 1, "running", "Tmux Agent Sidebar"),
            // @2: resets in a minute (today).
            a("main", 2, "paused", "Soon limited").with("paused", &f).with("limit", &lim(NOW + 60, "five_hour"))
                .with("detail_kind", "limit").with("detail", "session limit · resets soon"),
            // @3: resets in two days (a weekday). (The indicator writes
            // `@agent_state paused` last, so the raw state is paused: no
            // slot. A done + flag window is the pause key's transient.)
            a("main", 3, "paused", "Weekly limited").with("paused", &f).with("limit", &lim(NOW + 2 * 24 * H, "seven_day")),
            // @4: a model limit (no reset time): the indicator's words.
            a("main", 4, "paused", "Fable limited").with("paused", &f).with("limit", &lim(0, "model"))
                .with("detail_kind", "limit").with("detail", "Opus limit · /model to switch"),
            // @5: lifted, waiting for Mack.
            a("main", 5, "paused", "Lifted one").with("paused", &f).with("limit", &lim(NOW - 600, "five_hour"))
                .with("limit_lifted", &(NOW - 60).to_string()),
            // @6: prompted again by Mack: running, the limit kept.
            a("main", 6, "running", "Prompted again").with("limit", &lim(NOW + 60, "five_hour")),
            // @7: paused by the pause key, no limit.
            a("main", 7, "paused", "Plain paused").with("paused", &f),
            a("main", 8, "needs-input", "Asks").with("detail_kind", "ask").with("detail", "which?"),
            a("main", 9, "idle", "Q9"),
        ],
        Some("@1"),
    )
}

/// The rows of the card titled `title` (its first line, its second).
fn card(f: &Frame, title: &str) -> (usize, usize) {
    let y = f.agents.clone().find(|&y| f.rows[y].text().contains(title)).unwrap_or_else(|| panic!("{title}: {:#?}", texts(f)));
    (y, y + 1)
}

fn peach_rail() -> Seg {
    seg("▎", "peach")
}

/// Limited agents are paused agents with a reason: in the paused bucket,
/// never attention, never NEEDS YOU nor agent-jump's queue, never a number
/// key (slot); their mark is the limit glyph in peach, their words `limit`.
/// A limited agent Mack prompted again is plainly working.
#[test]
fn limited_is_paused_never_needs_you_and_holds_no_slot() {
    let v = limited_view();
    let get = |t: &str| v.agents().find(|a| a.title == t).unwrap_or_else(|| panic!("{t}"));
    for t in ["Soon limited", "Weekly limited", "Fable limited", "Lifted one"] {
        let x = get(t);
        assert_eq!((x.state.as_str(), x.cat), ("paused", Some(Cat::Paused)), "{t}");
        assert!(x.limit.is_some() && !x.attention && !x.in_flight, "{t}");
        assert_eq!((x.glyph, x.color, x.state_words), (LIMIT_GLYPH, Some("peach"), "limit"), "{t}");
        assert_eq!(x.slot, None, "{t}: a limited agent holds no number");
    }
    assert_eq!(v.needs.iter().map(|n| n.agent.title.as_str()).collect::<Vec<_>>(), ["Asks"]);
    // Prompted again: running, no limit shown, its slot back.
    let p = get("Prompted again");
    assert_eq!((p.limit.as_ref(), p.cat, p.glyph), (None, Some(Cat::Working), "◐"));
    assert!(p.slot.is_some());
    // The plain paused one keeps ⏸ violet.
    let q = get("Plain paused");
    assert_eq!((q.limit.as_ref(), q.glyph, q.color, q.state_words), (None, "⏸", Some("violet"), "paused"));
    // Counted with the paused (the limited are paused); the view lists them.
    let main = v.spaces.iter().find(|s| s.name == "main").unwrap();
    assert_eq!(main.counts.get(Cat::Paused), 5);
    let ids: Vec<&str> = v.limited().iter().map(|a| a.title.as_str()).collect();
    assert_eq!(ids, ["Soon limited", "Weekly limited", "Fable limited", "Lifted one"]);
}

/// A limited card: a peach `▎` rail down both rows, the limit mark bold
/// peach on the right (its word below), the second line `limit · resets
/// <when>`, a model limit's `Fable limit · /model`, a lifted one's `limit
/// lifted` and its `⏎ resume` chip, whose cells (and only they) resume
/// that agent ([`Target::LimitResume`]). Never violet, never green, never
/// the agent's own last reply.
#[test]
fn limited_cards_say_when_and_lifted_ones_resume() {
    let v = lettered(limited_view());
    let now = NOW as f64;
    for w in [34u16, 44, 60] {
        let f = layout(&v, w, 80, &Opts::default());
        let t = texts(&f);
        let ctx = format!("{w}: {t:#?}");
        for title in ["Soon limited", "Weekly limited", "Fable limited", "Lifted one"] {
            let (y, y2) = card(&f, title);
            assert!(!f.needs.contains(&y), "{title} {ctx}");
            let (r, r2) = (&f.rows[y], &f.rows[y2]);
            assert_eq!((r.segs[1].clone(), r2.segs[1].clone()), (peach_rail(), peach_rail()), "{title} {ctx}");
            assert!(r.segs.contains(&bold(LIMIT_GLYPH, "peach")), "{title} {ctx}");
            assert!(!t[y].contains(&format!("{LIMIT_GLYPH} limit")), "{title}: the word once, below {ctx}");
            for s in r.segs.iter().chain(&r2.segs) {
                assert!(s.fg != Ink::Pal("green") && s.fg != Ink::Pal("violet"), "{title}: {s:?} {ctx}");
            }
            assert!(!t[y2].contains("session limit"), "{title}: the reply gives way {ctx}");
        }
        let (_, s) = card(&f, "Soon limited");
        assert!(t[s].contains(&format!("limit · resets {}", when(NOW + 60, now))), "{ctx}");
        assert!(f.rows[s].segs.contains(&bold("limit", "peach")), "{ctx}");
        let (_, s) = card(&f, "Weekly limited");
        let at = when(NOW + 2 * 24 * H, now);
        assert!(at.split(' ').count() == 3, "a weekday: {at}");
        // At 34 columns the age keeps its place: `resets` may go first.
        assert!(t[s].contains(&format!("limit · resets {at}")) || (w == 34 && t[s].contains(&format!("limit · {at}"))), "{ctx}");
        // No reset time: the indicator's own words (never a made-up model
        // name), and the chip (only Mack can judge it: always resumable).
        let (_, s) = card(&f, "Fable limited");
        assert!(t[s].contains("Opus limit") && !t[s].contains("Fable limit"), "{ctx}");
        assert!(w < 60 || t[s].contains("Opus limit · /model to switch"), "{ctx}");
        assert!(t[s].contains("⏎ resume"), "{ctx}");
        // Lifted: the chip, and its cells resume THAT agent; the rest of the
        // card still goes to the window.
        let (y, s) = card(&f, "Lifted one");
        assert!(t[s].contains("limit lifted ⏎ resume"), "{ctx}");
        assert!(f.rows[s].segs.contains(&block(LIMIT_RESUME, "crust", "peach")), "{:?}", f.rows[s].segs);
        let x0 = t[s].find("⏎").map(|b| dwidth(&t[s][..b])).unwrap() as u16 - 1;
        assert_eq!(&t[s][..t[s].find("⏎").unwrap()], format!(" ▎     limit lifted "), "{ctx}");
        let win = v.agents().find(|a| a.title == "Lifted one").unwrap().window_id.clone();
        for x in x0..x0 + dwidth(LIMIT_RESUME) as u16 {
            assert_eq!(f.target(x, s as u16), Some(&Target::LimitResume(win.clone())), "{x} {ctx}");
        }
        assert!(matches!(f.target(x0 - 2, s as u16), Some(Target::Go { .. })), "{ctx}");
        assert!(matches!(f.target(x0 + dwidth(LIMIT_RESUME) as u16, s as u16), Some(Target::Go { .. })), "{ctx}");
        assert!(matches!(f.target(4, y as u16), Some(Target::Go { .. })), "{ctx}");
        // Only the resumable cards (lifted, no reset time) have a chip.
        let chips: Vec<&Target> = f.rows.iter().flat_map(|r| &r.targets).map(|(_, _, t)| t)
            .filter(|t| matches!(t, Target::LimitResume(_))).collect();
        let model = v.agents().find(|a| a.title == "Fable limited").unwrap().window_id.clone();
        assert_eq!(chips.len(), 2, "{ctx}");
        assert!(chips.contains(&&Target::LimitResume(win.clone())) && chips.contains(&&Target::LimitResume(model)), "{ctx}");
        // Prompted again: working, blue; the plain paused one violet.
        let (y, _) = card(&f, "Prompted again");
        assert!(t[y].contains("working") || t[y].contains('⠋'), "{ctx}");
        let (_, s) = card(&f, "Plain paused");
        assert!(f.rows[s].segs.contains(&bold("paused", "violet")), "{ctx}");
    }
    // Narrow (no second lines under 26 columns): the mark and the word on
    // the one line, no chip.
    let f = layout(&v, 24, 80, &Opts::default());
    let y = find(&f, "Soon l");
    assert!(f.rows[y].segs.contains(&bold(format!("{LIMIT_GLYPH} limit"), "peach")) && f.rows[y].segs[1] == peach_rail(), "{:?}", f.rows[y].segs);
    assert!(!f.rows.iter().flat_map(|r| &r.targets).any(|(_, _, t)| matches!(t, Target::LimitResume(_))));
}

/// The edges of a limit card, at 60 columns:
/// - just past the reset, inside agent-pause.py's 120 s grace: `limit ·
///   resetting`, no chip (the script would refuse; native auto-continue
///   goes first), never a time in the past;
/// - past the grace but not lifted (the tick deferred it: a manual pause,
///   the wake grace): resumable, so the chip;
/// - no reset time and no words from the indicator: `model limit`, chip;
/// - a reset that overflows, will not parse, or is absurdly far: `limit ·
///   reset unknown` (never an empty time), chip.
#[test]
fn limit_cards_at_the_edges() {
    let f0 = flag();
    let v = lettered(view(
        vec![
            a("main", 1, "paused", "In grace").with("paused", &f0).with("limit", &lim(NOW - 60, "five_hour")),
            a("main", 2, "paused", "Deferred").with("paused", &f0).with("limit", &lim(NOW - 600, "five_hour")),
            a("main", 3, "paused", "No words").with("paused", &f0).with("limit", &lim(0, "model")),
            a("main", 4, "paused", "Overflow").with("paused", &f0).with("limit", "99999999999999999999 five_hour 1"),
            a("main", 5, "paused", "Far off").with("paused", &f0).with("limit", &lim(NOW + 500 * 24 * H, "seven_day")),
            a("main", 6, "paused", "Junk").with("paused", &f0).with("limit", "soon five_hour 1"),
        ],
        Some("@1"),
    ));
    let f = layout(&v, 60, 60, &Opts::default());
    let t = texts(&f);
    let chip = |s: usize| f.rows[s].targets.iter().any(|(_, _, x)| matches!(x, Target::LimitResume(_)));
    let (_, s) = card(&f, "In grace");
    assert!(t[s].contains("limit · resetting") && !t[s].contains("resets") && !chip(s), "{t:#?}");
    let (_, s) = card(&f, "Deferred");
    assert!(chip(s) && t[s].contains("⏎ resume") && !t[s].contains("resets"), "{t:#?}");
    let (_, s) = card(&f, "No words");
    assert!(t[s].contains("model limit") && chip(s), "{t:#?}");
    for title in ["Overflow", "Far off", "Junk"] {
        let (_, s) = card(&f, title);
        assert!(t[s].contains("limit · reset unknown") && chip(s), "{title}: {t:#?}");
        assert!(!t[s].contains("resets"), "{title}: {t:#?}");
    }
}

/// An IDLE window that still carries `@agent_limit` (agent-pause.py's
/// limit_send flipped it paused → idle and unset `@agent_paused` just before
/// its RESUME, and the paste is held behind a draft or never lands) is still
/// a limited card: peach, the limit's words, the chip when resumable (lifted
/// here); never NEEDS YOU; never a slot, as the watcher's slot_active rules
/// for idle (only a fresh `@agent_pending` holds one, in both; agent-ui
/// mirrors the watcher so the numbers never part).
#[test]
fn an_idle_window_with_a_limit_is_still_limited() {
    let v = lettered(view(
        vec![
            a("main", 1, "running", "Here"),
            a("main", 2, "idle", "Held resume").with("limit", &lim(NOW - 600, "seven_day"))
                .with("limit_lifted", &(NOW - 300).to_string()).with("detail_kind", "limit").with("detail", "weekly limit · resets Mon"),
            a("main", 3, "idle", "Not lifted").with("limit", &lim(NOW + 3 * H, "five_hour")),
            a("main", 4, "idle", "Answered").with("limit", &lim(NOW + 3 * H, "five_hour")).with("pending", &NOW.to_string()),
        ],
        Some("@1"),
    ));
    let get = |t: &str| v.agents().find(|a| a.title == t).unwrap();
    for t in ["Held resume", "Not lifted"] {
        let x = get(t);
        assert!(x.limit.is_some() && x.cat == Some(Cat::Paused) && !x.attention, "{t}");
        assert_eq!((x.glyph, x.state_words, x.slot), (LIMIT_GLYPH, "limit", None), "{t}");
    }
    assert!(v.needs.is_empty());
    // A fresh answered stamp holds a slot for idle, limited or not: the
    // watcher's rule, mirrored.
    assert!(get("Answered").slot.is_some());
    let f = layout(&v, 44, 60, &Opts::default());
    let t = texts(&f);
    let (_, s) = card(&f, "Held resume");
    assert!(t[s].contains("limit lifted ⏎ resume"), "{t:#?}");
    assert!(f.rows[s].targets.iter().any(|(_, _, x)| *x == Target::LimitResume("@2".into())), "{t:#?}");
    let (_, s) = card(&f, "Not lifted");
    assert!(t[s].contains(&format!("limit · resets {}", when(NOW + 3 * H, NOW as f64))), "{t:#?}");
    assert!(texts(&f)[f.limit.clone()].iter().any(|l| l.contains("rate-limited · 3")), "{t:#?}");
}

/// The card's line under the title, form by form as the room shrinks.
#[test]
fn limit_line_forms() {
    let now = NOW as f64;
    let words = |l: &Limit, detail: &str, room: usize| {
        let (segs, span) = limit_line(l, detail, now, room, "overlay2");
        (segs.iter().map(|s| s.text.as_str()).collect::<String>(), span)
    };
    let text = |l: &Limit, room: usize| words(l, "", room);
    let chip = |n: usize| Some((n, n + dwidth(LIMIT_RESUME)));
    let soon = Limit::parse(&format!("{} five_hour 1", NOW + 60), "").unwrap();
    let at = when(NOW + 60, now);
    assert_eq!(text(&soon, 99), (format!("limit · resets {at}"), None));
    assert_eq!(text(&soon, dwidth(&format!("limit · {at}"))).0, format!("limit · {at}"));
    assert_eq!(text(&soon, 6).0, "limit");
    assert_eq!(text(&soon, 2).0, "limit"); // the last form, clipped by the row
    // No reset time: the indicator's words, else `model limit`; the chip.
    let model = Limit::parse("0 model 1", "").unwrap();
    let opus = "Opus limit · /model to switch";
    assert_eq!(words(&model, opus, 99), (format!("{opus}{LIMIT_RESUME}"), chip(dwidth(opus))));
    assert_eq!(words(&model, opus, 25), (format!("Opus limit{LIMIT_RESUME}"), chip(10)));
    assert_eq!(words(&model, "A very long model limit name", 20), (format!("A very lo…{LIMIT_RESUME}"), chip(10)));
    assert_eq!(words(&model, opus, 10).0, LIMIT_RESUME);
    assert_eq!(words(&model, opus, 9), ("Opus lim…".to_string(), None)); // no room for the chip: its words, cut
    assert_eq!(text(&model, 99), (format!("model limit{LIMIT_RESUME}"), chip(11)));
    assert_eq!(text(&Limit::parse("0 codex 1", "").unwrap(), 99).0, format!("model limit{LIMIT_RESUME}"));
    // Would not parse / absurdly far: unknown, resumable.
    for raw in ["soon five_hour 1".to_string(), "99999999999999999999 five_hour 1".into(), format!("{} seven_day 1", NOW + 500 * 24 * H)] {
        let l = Limit::parse(&raw, "").unwrap();
        assert_eq!(text(&l, 99), (format!("limit · reset unknown{LIMIT_RESUME}"), chip(21)), "{raw}");
    }
    // Inside the grace: resetting, no chip; past it: reset passed, the chip.
    let grace = Limit::parse(&format!("{} five_hour 1", NOW - 60), "").unwrap();
    assert_eq!(text(&grace, 99), ("limit · resetting".to_string(), None));
    let passed = Limit::parse(&format!("{} five_hour 1", NOW - 121), "").unwrap();
    assert_eq!(text(&passed, 99), (format!("limit · reset passed{LIMIT_RESUME}"), chip(20)));
    let lifted = Limit { lifted: Some(NOW), ..soon.clone() };
    assert_eq!(text(&lifted, 99), (format!("limit lifted{LIMIT_RESUME}"), Some((12, 12 + dwidth(LIMIT_RESUME)))));
    assert_eq!(text(&lifted, 22), (format!("limit lifted{LIMIT_RESUME}"), Some((12, 12 + dwidth(LIMIT_RESUME)))));
    assert_eq!(text(&lifted, 21), (format!("lifted{LIMIT_RESUME}"), Some((6, 6 + dwidth(LIMIT_RESUME)))));
    assert_eq!(text(&lifted, 10), (LIMIT_RESUME.to_string(), Some((0, dwidth(LIMIT_RESUME)))));
    assert_eq!(text(&lifted, 6), ("lifted".to_string(), None));
}

/// The rate-limit banner: drawn only while an agent is rate-limited, under
/// the pause banner, in peach (never the pause's cyan or violet): the
/// count, the EARLIEST reset, the auto-resume setting; one row where it all
/// fits, two at Mack's 34 columns, one short row in a short frame. A click
/// goes to the agent with the earliest reset.
#[test]
fn the_rate_limit_banner() {
    let now = NOW as f64;
    let v = lettered(limited_view());
    // No limited agent: no banner.
    let f = layout(&lettered(quiet()), 34, 58, &Opts::default());
    assert!(f.limit.is_empty() && !texts(&f).iter().any(|l| l.contains("rate-limited")));
    let soon = when(NOW + 60, now);
    let first = v.agents().find(|a| a.title == "Soon limited").unwrap();
    let go = Target::Go { win: first.window_id.clone(), session: "main".into() };
    // Wide: one row, everything on it.
    let f = layout(&v, 70, 58, &Opts::default());
    let t = texts(&f);
    assert_eq!(f.limit, 0..1, "{t:#?}");
    assert!(t[0].contains(&format!("{LIMIT_GLYPH} rate-limited · 4 · resets {soon} · auto ↻")), "{t:#?}");
    assert_eq!(f.rows[0].bg, Some(Ink::Mix("peach", LIMIT_TINT)));
    assert_eq!(f.rows[0].segs[1], seg(RAIL_HEAVY, "peach"));
    assert_eq!(f.target(5, 0), Some(&go));
    // Mack's 34 columns: two rows, the setting on the first's right.
    let f = layout(&v, 34, 58, &Opts::default());
    let t = texts(&f);
    assert_eq!(f.limit, 0..2, "{t:#?}");
    assert!(t[0].contains("rate-limited · 4") && t[0].trim_end().ends_with("auto ↻"), "{t:#?}");
    assert!(t[1].contains(&format!("resets {soon}")), "{t:#?}");
    assert!(f.rows[f.limit.clone()].iter().all(|r| r.bg == Some(Ink::Mix("peach", LIMIT_TINT)) && r.segs[1] == seg(RAIL_HEAVY, "peach")));
    // Auto-resume off.
    let off = Opts { bar: Bar { limit_auto_off: true, ..Bar::default() }, ..Opts::default() };
    let f = layout(&v, 34, 58, &off);
    assert!(texts(&f)[0].trim_end().ends_with("auto off"), "{:#?}", texts(&f));
    // A short frame: one row, shedding `rate-limited`, then `resets`.
    let f = layout(&v, 34, 12, &Opts::default());
    let t = texts(&f);
    assert_eq!(f.limit, 0..1, "{t:#?}");
    assert!(t[0].contains(&format!("{LIMIT_GLYPH} 4 · resets {soon}")) || t[0].contains(&format!("{LIMIT_GLYPH} rate-limited · 4 · resets {soon}")), "{t:#?}");
    let f = layout(&v, 18, 12, &Opts::default());
    assert!(texts(&f)[0].contains(&format!("{LIMIT_GLYPH} 4 · ")), "{:#?}", texts(&f));
    // Too short: none.
    assert!(layout(&v, 34, 7, &Opts::default()).limit.is_empty());
    // Under a pause banner, never in it.
    let f = layout(&v, 34, 58, &Opts { pause: Some(Phase::Safe { labels: vec!["Plain paused".into()] }), ..Opts::default() });
    assert!(!f.pause.is_empty() && f.limit.start == f.pause.end && f.limit.len() == 2, "{:#?}", texts(&f));
    for y in f.pause.clone() {
        assert!(!texts(&f)[y].contains("rate-limited"));
    }
}

/// What the banner says about when: the earliest reset among those not
/// lifted; only model limits: the switch they need; every one lifted: so.
#[test]
fn the_banner_says_the_earliest_reset() {
    let now = NOW as f64;
    let f0 = flag();
    let mk = |agents: Vec<A>| lettered(view(agents, Some("@1")));
    let first_row = |v: &ViewModel| texts(&layout(v, 80, 58, &Opts::default()))[0].clone();
    // Two resets: the earlier one, wherever it sits.
    let v = mk(vec![
        a("main", 1, "paused", "Later").with("paused", &f0).with("limit", &lim(NOW + 5 * H, "five_hour")),
        a("main", 2, "paused", "Sooner").with("paused", &f0).with("limit", &lim(NOW + H, "five_hour")),
        a("main", 3, "paused", "Model").with("paused", &f0).with("limit", &lim(0, "model")),
    ]);
    assert!(first_row(&v).contains(&format!("rate-limited · 3 · resets {}", when(NOW + H, now))), "{}", first_row(&v));
    let f = layout(&v, 80, 58, &Opts::default());
    assert!(matches!(f.target(5, 0), Some(Target::Go { win, .. }) if win == "@2"));
    // Only limits with no reset time (written 0, garbled, absurdly far):
    // Mack resumes when ready.
    let v = mk(vec![a("main", 1, "paused", "Model").with("paused", &f0).with("limit", &lim(0, "model"))]);
    assert!(first_row(&v).contains("rate-limited · 1 · no reset time · resume when ready"), "{}", first_row(&v));
    let v = mk(vec![
        a("main", 1, "paused", "Model").with("paused", &f0).with("limit", &lim(0, "model")),
        a("main", 2, "paused", "Overflow").with("paused", &f0).with("limit", "99999999999999999999 five_hour 1"),
        a("main", 3, "paused", "Far").with("paused", &f0).with("limit", &lim(NOW + 500 * 24 * H, "seven_day")),
    ]);
    assert!(first_row(&v).contains("rate-limited · 3 · no reset time · resume when ready"), "{}", first_row(&v));
    // A believable reset wins over a far one; never an empty time.
    let v = mk(vec![
        a("main", 1, "paused", "Far").with("paused", &f0).with("limit", &lim(NOW + 500 * 24 * H, "seven_day")),
        a("main", 2, "paused", "Real").with("paused", &f0).with("limit", &lim(NOW + 2 * H, "five_hour")),
    ]);
    assert!(first_row(&v).contains(&format!("resets {}", when(NOW + 2 * H, now))), "{}", first_row(&v));
    // The earliest reset just passed (inside the grace): resetting.
    let v = mk(vec![a("main", 1, "paused", "Grace").with("paused", &f0).with("limit", &lim(NOW - 60, "five_hour"))]);
    assert!(first_row(&v).contains("rate-limited · 1 · resetting"), "{}", first_row(&v));
    // A lifted one does not count for the reset; all lifted: says so.
    let v = mk(vec![
        a("main", 1, "paused", "Lifted").with("paused", &f0).with("limit", &lim(NOW - H, "five_hour")).with("limit_lifted", "5"),
        a("main", 2, "paused", "Held").with("paused", &f0).with("limit", &lim(NOW + 3 * H, "seven_day")),
    ]);
    assert!(first_row(&v).contains(&format!("resets {}", when(NOW + 3 * H, now))), "{}", first_row(&v));
    let v = mk(vec![a("main", 1, "paused", "Lifted").with("paused", &f0).with("limit", &lim(NOW - H, "five_hour")).with("limit_lifted", "5")]);
    assert!(first_row(&v).contains("rate-limited · 1 · lifted · waiting for you"), "{}", first_row(&v));
    // A limit on an agent Mack prompted again (running): no banner.
    let v = mk(vec![a("main", 1, "running", "Again").with("limit", &lim(NOW + H, "five_hour"))]);
    assert!(layout(&v, 80, 58, &Opts::default()).limit.is_empty());
}

/// Every size: every row exactly the frame's width, every agent accounted
/// for once, the banner at the top under the pause banner, never more rows
/// than the frame; and the banner's COUNT always whole on its first row
/// (the words give way first, never the count).
#[test]
fn limited_frames_hold_at_every_size() {
    let v = lettered(limited_view());
    for w in [8u16, 12, 18, 20, 22, 24, 26, 28, 30, 34, 44, 60] {
        for h in 1u16..=60 {
            for opts in [Opts::default(), Opts { pause: Some(Phase::Safe { labels: vec!["Plain paused".into()] }), ..Opts::default() }] {
                let f = layout(&v, w, h, &opts);
                let ctx = format!("{w}x{h}: {:#?}", texts(&f));
                assert_eq!(f.rows.len(), usize::from(h), "{ctx}");
                for r in &f.rows {
                    assert_eq!(width(&r.segs), usize::from(w), "{ctx}");
                }
                assert!(f.limit.start >= f.pause.end || f.limit.is_empty(), "{ctx}");
                if let Some(y) = f.limit.clone().next() {
                    assert!(f.rows[y].segs.contains(&bold("4", "text")), "{ctx}: the count");
                    assert!(!f.rows[y].text().contains('…'), "{ctx}: nothing clipped on the count's row");
                }
                if f.agents_step == 0 && !f.agents.is_empty() {
                    check_accounting("limited", &v, &f);
                }
            }
        }
    }
}
