//! ⌘E's ` ⌘E launch ` mark (key table `launch`, spaces.conf +
//! scripts/agent-launch.sh): the installed agents' glyphs and letters on the
//! panel's top pad, whole items only.

use super::*;
use crate::model::Harness;

/// Every agent agent-launch.sh knows, all installed, in its order.
fn all_agents() -> Vec<(char, Harness)> {
    crate::sidebar::status::launch_agents("c:claude a:abby x:codex o:opencode g:grok y:agy u:cursor p:copilot d:aider")
}

fn armed(agents: Vec<(char, Harness)>) -> Opts {
    with_bar(Bar { launch: Some(agents), ..demo_bar() })
}

/// The top pad's text with each harness glyph as `[I]` (its initial), so it
/// reads without the AgentMarks font.
fn plain(s: &str) -> String {
    s.chars()
        .map(|c| match Harness::all().find(|h| h.glyph() == c) {
            Some(h) => format!("[{}]", h.initial()),
            None => c.to_string(),
        })
        .collect()
}

/// (glyphs, letters) drawn after the mark: each glyph seg is followed by its
/// `k ` letter seg, never one without the other.
fn items(r: &Row) -> Vec<(char, char)> {
    let segs = &r.segs;
    let mut out = Vec::new();
    for (i, s) in segs.iter().enumerate() {
        let mut cs = s.text.chars();
        if let (Some(g), None) = (cs.next(), cs.next()) {
            if let Some(h) = Harness::all().find(|h| h.glyph() == g) {
                let next = segs.get(i + 1).expect("a glyph without its letter");
                let k = next.text.chars().next().unwrap();
                assert_eq!(next.text, format!("{k} "), "{:?}", segs);
                assert_eq!(s.fg, Ink::from(h.hue()), "{:?}", s);
                out.push((g, k));
            }
        }
    }
    out
}

#[test]
fn the_launch_mark_lists_the_installed_agents_whole() {
    let v = shelly();
    let all = all_agents();
    assert_eq!(all.len(), 9);
    // Wide: the mark, then every agent, claude first; the pad's ends kept.
    let f = layout(&v, 60, 58, &armed(all.clone()));
    let top = &f.rows[f.card.start];
    assert!(top.segs.contains(&block(" ⌘E launch ", "crust", "peach")), "{:?}", top.segs);
    let got = items(top);
    assert_eq!(got.iter().map(|(_, k)| *k).collect::<String>(), "caxogyupd");
    assert_eq!(got[0].0, Harness::named("claude").glyph());
    assert_eq!(got[5].0, Harness::named("agy").glyph()); // OTHER's until its row lands
    assert_eq!(got[8].0, Harness::OTHER.glyph()); // aider: not in the harness table
    let t = top.text();
    assert!(t.starts_with(" ▄ ⌘E launch  ") && t.ends_with("▄ ") && dwidth(&t) == 60, "{t:?}");
    // The panel is no taller for it.
    assert_eq!(f.card, layout(&v, 60, 58, &with_bar(demo_bar())).card);
    // Narrower: fewer agents, in order, each whole; the frame exactly as wide.
    let mut last = usize::MAX;
    for w in [44usize, 34, 28, 22] {
        let f = layout(&v, w as u16, 58, &armed(all.clone()));
        let top = &f.rows[f.card.start];
        let got = items(top);
        let keys: String = got.iter().map(|(_, k)| *k).collect();
        assert!("caxogyupd".starts_with(&keys), "{w}: {keys}");
        assert!(got.len() <= last, "{w}");
        last = got.len();
        let t = top.text();
        assert!(t.starts_with(" ▄ ⌘E launch ") && dwidth(&t) == w, "{w}: {t:?}");
    }
    // 34 columns (the sidebar's usual width) still shows a few.
    let f = layout(&v, 34, 58, &armed(all.clone()));
    assert!(items(&f.rows[f.card.start]).len() >= 3);
    // No list recorded yet: the mark and ⌥W's hint.
    let f = layout(&v, 34, 58, &armed(vec![]));
    let t = f.rows[f.card.start].text();
    assert!(t.starts_with(" ▄ ⌘E launch  type a letter"), "{t:?}");
    // Not armed: no mark.
    let f = layout(&v, 34, 58, &with_bar(demo_bar()));
    assert!(!f.rows[f.card.start].text().contains("launch"));
}

/// A text render of the armed top cap (run with `--nocapture`).
#[test]
fn render_the_launch_mark() {
    let v = shelly();
    for w in [60u16, 44, 34, 28, 22] {
        let f = layout(&v, w, 58, &armed(all_agents()));
        let t = f.rows[f.card.start].text();
        println!("{w:>2} |{t}|");
        println!("{w:>2} |{}|", plain(&t));
    }
}
