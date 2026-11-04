//! The keybind help's hint on PARKED's right ([`keys_hint`]) and the
//! ` ? keys ` beside the armed prefix's chip ([`marks`]).

use super::*;

/// The cells of row `y` whose click is [`Target::Keys`].
fn keys_cells(f: &Frame, y: usize) -> Vec<u16> {
    (0..f.width).filter(|&x| f.target(x, y as u16) == Some(&Target::Keys)).collect()
}

/// The text drawn on cells `xs` of row `y` (WezTerm's cells).
fn text_at(f: &Frame, y: usize, xs: &[u16]) -> String {
    let mut at = 0usize;
    let mut out = String::new();
    for s in &f.rows[y].segs {
        for (t, cw) in clusters(&s.text) {
            if xs.contains(&(at as u16)) {
                out.push_str(&t);
            }
            at += cw;
        }
    }
    out
}

/// `quiet()` with `n` parked tabs.
fn with_parked(n: usize) -> ViewModel {
    let mut v = quiet();
    let p = v.parked[0].clone();
    v.parked = vec![p; n];
    v
}

/// At every height a frame has PARKED (small, medium, large; with and
/// without the usage panel; with and without the dock's caps) its right
/// says `⌃S ? keys`, the key bold blue and the word dim, and only those
/// cells open the help: the rest of the line (its ends too) is the
/// stash's. Every row exactly the frame's width.
#[test]
fn the_keys_hint_is_on_parked_at_every_height() {
    let mut seen_caps = (false, false);
    for (name, v) in [("quiet", quiet()), ("busy", busy()), ("empty", empty())] {
        for bar in [Bar::default(), demo_bar()] {
            for h in (3u16..=90).chain([120]) {
                let f = layout(&v, 34, h, &with_bar(bar.clone()));
                let t = texts(&f);
                let ctx = format!("{name} 34x{h}: {t:#?}");
                assert!(f.rows.iter().all(|r| dwidth(&r.text()) == 34), "{ctx}");
                if f.parked.is_empty() {
                    continue;
                }
                let y = f.parked.start;
                seen_caps.0 |= is_pad(&t[h as usize - 1]);
                seen_caps.1 |= !is_pad(&t[h as usize - 1]);
                assert!(t[y].ends_with("⌃S ? keys  "), "{ctx}");
                assert!(f.rows[y].segs.contains(&bold("⌃S ?", "blue")) && f.rows[y].segs.contains(&seg(" keys", LABEL)), "{ctx}");
                let cells = keys_cells(&f, y);
                assert_eq!(cells, (23..32).collect::<Vec<u16>>(), "{ctx}");
                assert_eq!(text_at(&f, y, &cells), "⌃S ? keys", "{ctx}");
                assert_eq!((f.target(0, y as u16), f.target(2, y as u16), f.target(22, y as u16), f.target(32, y as u16), f.target(33, y as u16)),
                    (Some(&Target::Stash), Some(&Target::Stash), Some(&Target::Stash), Some(&Target::Stash), Some(&Target::Stash)), "{ctx}");
            }
        }
    }
    // Both shapes of the bottom were seen: under the `▀` cap and the last row.
    assert_eq!(seen_caps, (true, true));
    // The hint never costs a row: the frame is row for row what it was
    // without it, but for PARKED's right.
    let v = busy();
    for h in [10u16, 24, 58] {
        let f = layout(&v, 34, h, &Opts::default());
        assert_eq!(f.rows.len(), usize::from(h));
        let p = f.parked.start;
        assert!(texts(&f)[p].starts_with("  PARKED  1 "), "{:#?}", texts(&f));
    }
}

/// As the line narrows the hint shortens (`? keys`, then `?`) and goes
/// before PARKED's count is ever clipped; never wider than the row, never
/// over the count (one, two digits, or none).
#[test]
fn the_keys_hint_narrows_then_goes() {
    for (n, count) in [(1usize, "  PARKED  1 "), (12, "  PARKED  12 "), (0, "  PARKED none")] {
        let v = with_parked(n);
        let mut forms = Vec::new();
        for w in (8u16..=60).rev() {
            let f = layout(&v, w, 40, &Opts::default());
            let t = texts(&f);
            let y = f.parked.start;
            let ctx = format!("{n} parked, {w} wide: {:?}", t[y]);
            assert!(f.rows.iter().all(|r| dwidth(&r.text()) == usize::from(w)), "{ctx}");
            let cells = keys_cells(&f, y);
            let hint = text_at(&f, y, &cells);
            // The count whole, and a gap of two before the hint.
            if !hint.is_empty() {
                assert!(t[y].starts_with(count), "{ctx}");
                assert!(usize::from(cells[0]) >= dwidth(count.trim_end()) + 2, "{ctx}");
                assert!(!t[y].contains('…'), "{ctx}");
                assert_eq!(usize::from(*cells.last().unwrap()), usize::from(w) - 3, "{ctx}: one cell then the margin after it");
            }
            let form = KEYS_HINTS.iter().position(|(k, l)| format!("{k}{l}") == hint).unwrap_or(KEYS_HINTS.len());
            assert!(forms.last().is_none_or(|&last| form >= last), "{ctx}: the hint grew as the line narrowed");
            forms.push(form);
        }
        // Every form was used, and each shows where it should.
        for k in 0..=KEYS_HINTS.len() {
            assert!(forms.contains(&k), "{n}: {forms:?}");
        }
    }
    // Mack's 34 columns: the whole hint; 24: `? keys`; 20: `?`; 16: none.
    let v = quiet();
    let hint = |w: u16| {
        let f = layout(&v, w, 40, &Opts::default());
        text_at(&f, f.parked.start, &keys_cells(&f, f.parked.start))
    };
    assert_eq!((hint(34), hint(24), hint(20), hint(16)), ("⌃S ? keys".into(), "? keys".into(), "?".into(), String::new()));
}

/// A message or the watcher's warning takes the corner while it shows (the
/// hint is back once it goes): never both, never one over the other.
#[test]
fn the_keys_hint_gives_way_to_a_message_or_the_watcher() {
    let mut v = quiet();
    for h in [12u16, 58] {
        let f = layout(&v, 34, h, &Opts { msg: Some("no tmux client".into()), ..Opts::default() });
        let p = f.parked.start;
        let t = f.rows[p].text();
        assert!(t.ends_with("no tmux client  ") && !t.contains("keys"), "{t:?}");
        assert!(keys_cells(&f, p).is_empty() && f.target(30, p as u16).is_none(), "{t:?}");
    }
    v.watcher_age = Some(99.0);
    for h in [12u16, 58] {
        let f = layout(&v, 34, h, &Opts::default());
        let p = f.parked.start;
        let t = f.rows[p].text();
        assert!(t.ends_with("watcher stalled 99s  ") && !t.contains("keys"), "{t:?}");
        assert!(keys_cells(&f, p).is_empty(), "{t:?}");
        assert_eq!((f.target(30, p as u16), f.target(2, p as u16)), (Some(&Target::Restart), Some(&Target::Stash)));
    }
    v.watcher_age = Some(1.0);
    let f = layout(&v, 34, 58, &Opts::default());
    assert!(f.rows[f.parked.start].text().ends_with("⌃S ? keys  "));
}

/// Toasts never cover the hint: it is on PARKED, which no toast covers.
#[test]
fn toasts_leave_the_keys_hint() {
    let v = quiet();
    let bar = Bar { toasts: (1..=3).map(|k| toast_n(k, Kind::Info, "a long toast that wraps and wraps over the frame")).collect(), ..demo_bar() };
    for h in [6u16, 9, 14, 30] {
        let f = layout(&v, 34, h, &with_bar(bar.clone()));
        let p = f.parked.start;
        assert!(!f.toast.contains(&p) && f.rows[p].text().ends_with("⌃S ? keys  "), "{h}: {:#?}", texts(&f));
    }
}

/// The armed prefix's chip says ` ? keys ` beside it, dim on `surface1`
/// (on the panel's top cap or, without a panel, its own row); never
/// clipped: too narrow for it whole, the chip alone.
#[test]
fn the_prefix_chip_says_question_mark_keys() {
    let v = quiet();
    let keys = on(" ? keys ", "sub", "surface1");
    let bar = Bar { prefix: true, ..demo_bar() };
    for (h, paneled) in [(40u16, true), (14, false)] {
        let f = layout(&v, 34, h, &with_bar(bar.clone()));
        assert_eq!(!f.card.is_empty(), paneled);
        let r = &f.rows[0];
        assert!(r.segs.contains(&block(" prefix ", "crust", "red")) && r.segs.contains(&keys), "{h}: {:?}", r.text());
        assert!(r.text().contains(" prefix  ? keys "), "{:?}", r.text());
    }
    for w in 8u16..=40 {
        let f = layout(&v, w, 40, &with_bar(bar.clone()));
        let r = &f.rows[0];
        assert_eq!(dwidth(&r.text()), usize::from(w));
        // (The prefix chip itself clips in the narrowest frames, as ever.)
        let t = r.text();
        assert!(!t.contains(" ?") || t.contains(" prefix  ? keys "), "{w}: {t:?}");
        if w >= 24 {
            assert!(t.contains(" prefix  ? keys "), "{w}: {t:?}");
        }
    }
    // Not armed: no word of it.
    let f = layout(&v, 34, 40, &with_bar(demo_bar()));
    assert!(!f.rows[0].text().contains("keys"));
}
