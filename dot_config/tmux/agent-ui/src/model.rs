//! The pure agent model: state predicates, count buckets, colours and glyphs,
//! and the NEEDS YOU order. Mirrors agent-roster.py `is_attn`, `in_flight`,
//! `rank`, `cat`, `counts`, `needs_order`, `Roster.dot` / `glyph` / `shape` /
//! `strip_glyph`, `CAT_GLYPH`, `CAT_HUE`, `KIND_GLYPH`, `DETAIL_WORD`.

use crate::collate::Collator;
use crate::tmux::Window;
use std::cmp::Ordering;
use std::collections::HashSet;

/// Sessions never shown as spaces or agents (agent-roster.py `HIDDEN`); the
/// stash (`HOLD`) is shown separately, as `parked`. Exact name match.
pub const HIDDEN: [&str; 4] = ["agents", "tasks", "scratch", "btop-popup"];
/// agent-jump.sh's `EXCLUDE`, byte for byte, matched the way its awk does:
/// `index(ex, " " session " ")`, a SUBSTRING test (so a session named
/// "tasks stash" is excluded too). A test compares it with the script.
pub const JUMP_EXCLUDE: &str = " agents tasks stash scratch btop-popup ";
/// agent-jump.sh: a window with no stamp sorts last in its tier.
pub const NO_STAMP: &str = "9999999999";

/// The count bucket of a window (agent-roster.py `CATS`), in display order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Cat {
    Failed,
    NeedsInput,
    Done,
    Working,
    Idle,
}

impl Cat {
    pub const ALL: [Cat; 5] = [Cat::Failed, Cat::NeedsInput, Cat::Done, Cat::Working, Cat::Idle];

    /// The CATS name ("failed", "needs-input", "done", "working", "idle").
    pub fn name(self) -> &'static str {
        match self {
            Cat::Failed => "failed",
            Cat::NeedsInput => "needs-input",
            Cat::Done => "done",
            Cat::Working => "working",
            Cat::Idle => "idle",
        }
    }

    /// agent-roster.py `CAT_GLYPH`: the state SHAPE (✕ ◉ ✓ ◐ ○).
    pub fn glyph(self) -> &'static str {
        match self {
            Cat::Failed => "✕",
            Cat::NeedsInput => "◉",
            Cat::Done => "✓",
            Cat::Working => "◐",
            Cat::Idle => "○",
        }
    }

    /// agent-roster.py `CAT_HUE`: the static colour of a count token / log
    /// line. (A live agent's mark uses [`dot_color`], which pulses.)
    pub fn hue(self) -> &'static str {
        match self {
            Cat::Failed => "red",
            Cat::NeedsInput => "yellow",
            Cat::Done => "green",
            Cat::Working => "pink",
            Cat::Idle => "overlay",
        }
    }

    /// The bucket of a bare state word (no workflow/cua flags), for event-log
    /// lines: failed/needs-input/done as themselves, running → working, any
    /// other non-empty state → idle.
    pub fn of_state(state: &str) -> Option<Cat> {
        match state {
            "failed" => Some(Cat::Failed),
            "needs-input" => Some(Cat::NeedsInput),
            "done" => Some(Cat::Done),
            "running" => Some(Cat::Working),
            "" => None,
            _ => Some(Cat::Idle),
        }
    }
}

/// agent-roster.py `is_attn`: failed, needs-input, or done with no fleet out.
pub fn is_attn(w: &Window) -> bool {
    matches!(w.state.as_str(), "failed" | "needs-input") || (w.state == "done" && w.workflow.is_empty())
}

/// agent-roster.py `in_flight`: running, or a workflow / cua flag set.
pub fn in_flight(w: &Window) -> bool {
    w.state == "running" || !w.workflow.is_empty() || !w.cua.is_empty()
}

/// agent-roster.py `rank`: 0 failed, 1 needs-input, 2 done, 3 in flight,
/// 4 other agent states, 9 no agent.
pub fn rank(w: &Window) -> u8 {
    if is_attn(w) {
        return match w.state.as_str() {
            "failed" => 0,
            "needs-input" => 1,
            _ => 2,
        };
    }
    if in_flight(w) {
        3
    } else if !w.state.is_empty() {
        4
    } else {
        9
    }
}

/// agent-roster.py `cat`: the count bucket, or None for a plain shell.
pub fn cat(w: &Window) -> Option<Cat> {
    if is_attn(w) {
        return Cat::of_state(&w.state);
    }
    if in_flight(w) {
        Some(Cat::Working)
    } else if !w.state.is_empty() {
        Some(Cat::Idle)
    } else {
        None
    }
}

/// Per-bucket counts (agent-roster.py `counts`, which returns the non-zero
/// ones in CATS order: see [`Counts::nonzero`]).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Counts(pub [usize; 5]);

impl Counts {
    /// Each window id once (a linked window is one agent).
    pub fn of<'a>(ws: impl IntoIterator<Item = &'a Window>) -> Counts {
        let mut seen = HashSet::new();
        let mut n = [0usize; 5];
        for w in ws {
            if let Some(c) = cat(w) {
                if seen.insert(w.id.as_str()) {
                    n[c as usize] += 1;
                }
            }
        }
        Counts(n)
    }

    pub fn get(&self, c: Cat) -> usize {
        self.0[c as usize]
    }

    pub fn total(&self) -> usize {
        self.0.iter().sum()
    }

    /// `[(cat, n)]` in CATS order, zero counts dropped (`✕1 ◉2 ◐4`).
    pub fn nonzero(&self) -> Vec<(Cat, usize)> {
        Cat::ALL.iter().filter(|&&c| self.get(c) > 0).map(|&c| (c, self.get(c))).collect()
    }
}

/// agent-roster.py `Roster.dot` colour: the live mark of a window. Attention
/// states in their own colour; in flight pulses pink/blue with @agent_blink,
/// except the window the client is on, which never pulses (blue); other
/// agent states overlay; None for a plain shell.
pub fn dot_color(w: &Window, blink: bool, is_current: bool) -> Option<&'static str> {
    if is_attn(w) {
        return Some(match w.state.as_str() {
            "failed" => "red",
            "needs-input" => "yellow",
            _ => "green",
        });
    }
    if in_flight(w) {
        return Some(if is_current || !blink { "blue" } else { "pink" });
    }
    if w.state.is_empty() {
        None
    } else {
        Some("overlay")
    }
}

/// nf-md-cog / nf-md-mouse: the popup's workflow and cua icons (Nerd Font).
pub const GEAR: &str = "\u{F0493}";
pub const MOUSE: &str = "\u{F037D}";
/// The strip's text equivalents (no Nerd Font needed).
pub const STRIP_GEAR: &str = "⚙";
pub const STRIP_MOUSE: &str = "◎";

/// Which side flag a window wears, if any (workflow wins over cua).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Flag {
    Workflow,
    Cua,
}

/// agent-roster.py `Roster.glyph` / `strip_glyph`: the flag and its colour.
/// Workflow: teal/dimteal with the pulse. Cua: overlay while it needs you,
/// else blue/dimblue with the pulse.
pub fn flag(w: &Window, blink: bool) -> Option<(Flag, &'static str)> {
    if !w.workflow.is_empty() {
        Some((Flag::Workflow, if blink { "teal" } else { "dimteal" }))
    } else if !w.cua.is_empty() {
        Some((Flag::Cua, if is_attn(w) { "overlay" } else if blink { "blue" } else { "dimblue" }))
    } else {
        None
    }
}

/// agent-roster.py `KIND_GLYPH`.
pub fn kind_glyph(kind: &str) -> Option<&'static str> {
    match kind {
        "claude" => Some("✳"),
        "codex" => Some("⬢"),
        _ => None,
    }
}

/// agent-roster.py `DETAIL_WORD`: @agent_detail_kind as a word.
pub fn detail_word(kind: &str) -> Option<&'static str> {
    match kind {
        "perm" => Some("perm"),
        "ask" => Some("asks"),
        "fail" => Some("fail"),
        "done" => Some("done"),
        "run" => Some("run"),
        _ => None,
    }
}

/// agent-roster.py `STATE_WORDS` plus the popup's row words: the state in words.
pub fn state_words(w: &Window) -> &'static str {
    match w.state.as_str() {
        "failed" => "failed",
        "needs-input" => "waiting on you",
        "done" if !w.workflow.is_empty() => "done · fleet out",
        "done" => "done",
        "running" => "working",
        "idle" => "idle",
        _ => "",
    }
}

/// agent-jump.sh `EXCLUDE` test: `index(ex, " " s " ")`.
pub fn jump_excluded(session: &str) -> bool {
    JUMP_EXCLUDE.contains(&format!(" {session} "))
}

/// Numeric compare of two ASCII digit strings of any length (sort -n).
fn num_cmp(a: &str, b: &str) -> Ordering {
    let (a, b) = (a.trim_start_matches('0'), b.trim_start_matches('0'));
    a.len().cmp(&b.len()).then_with(|| a.cmp(b))
}

/// One window's queue entry, as agent-jump.sh `list` prints it (after `cut -f5-`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NeedRow {
    pub id: String,
    /// The session of the FIRST sorted row (the one `list` keeps).
    pub session: String,
    pub index: u32,
    pub state: String,
    /// The stamp exactly as awk printed it (digits as given, or NO_STAMP).
    pub stamp: String,
    /// summary-or-name with tabs as spaces, cut at its first newline or US
    /// (where awk's record / field ends).
    pub label: String,
    /// The whole output line, byte for byte.
    pub line: String,
}

impl NeedRow {
    /// `win\tsession\tindex\tstate\tsince\tlabel`, agent-jump.sh `list`'s line.
    pub fn list_line(&self) -> String {
        self.line.clone()
    }
}

/// awk's string → number for `%d` (strtod on the leading numeric prefix,
/// truncated): "12a" → 12, "" or "x" → 0, " -3.9" → -3.
fn awk_int(s: &str) -> i64 {
    let t = s.trim_start_matches([' ', '\t', '\n', '\r', '\x0b', '\x0c']);
    let b = t.as_bytes();
    let mut i = 0;
    if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
        i += 1;
    }
    while i < b.len() && b[i].is_ascii_digit() {
        i += 1;
    }
    if i < b.len() && b[i] == b'.' {
        i += 1;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
        }
    }
    if i < b.len() && (b[i] == b'e' || b[i] == b'E') {
        let mut j = i + 1;
        if j < b.len() && (b[j] == b'+' || b[j] == b'-') {
            j += 1;
        }
        if j < b.len() && b[j].is_ascii_digit() {
            while j < b.len() && b[j].is_ascii_digit() {
                j += 1;
            }
            i = j;
        }
    }
    t[..i].parse::<f64>().map(|v| v as i64).unwrap_or(0)
}

/// agent-jump.sh `list`, in-process: the needs-you queue, in order.
///
/// The same pipeline over the same rows (agent-roster.py `needs_order`):
/// drop JUMP_EXCLUDE sessions (substring match); tier failed 0 > needs-input
/// 1 > done-without-a-fleet 2 (anything else is not queued); sort; a linked
/// window (one row per session) keeps its FIRST sorted row.
///
/// The sort is /usr/bin/sort's (2.3-Apple, FreeBSD's) exactly, for
/// `-k1,1n -k2,2n -k3,3 -k4,4` with no -s, over the awk's full line:
///   - tier and stamp numerically (any number of digits);
///   - each text key by wcscoll, and when that says equal, the SHORTER key (in
///     characters) first. Under en_US.UTF-8 emoji, Greek, Cyrillic and CJK
///     carry no weight, so "Ω" vs "日本" collate equal and length decides;
///   - all keys equal: the WHOLE line by the same rule, so the window id
///     after the keys decides.
///
/// The stamp is the first blank-separated word of @agent_since (awk `split`
/// on `[ \t\n]+`, leading blanks skipped) when it is all ASCII digits, else
/// NO_STAMP; the index key is `%09d`.
///
/// FREE TEXT. agent-jump.sh reads its own 7-field row (`id US session US
/// index US state US workflow US since US summary-or-name`) through awk, so a
/// newline in any value ends that record (the rest is a record of its own)
/// and a US starts a new field. This function rebuilds exactly that row from
/// the window's real values and runs the awk rules over every record of it,
/// and the sort keys and the dedup field are cut from the printed line by
/// tabs, as `sort -t TAB` and the second awk see them. So a summary holding
/// "a\nb" is listed as "a", one holding "a\x1fb" as "a", like the script.
/// (agent-roster.py did not model this; it dropped such windows.)
pub fn needs_order(windows: &[Window], coll: &Collator) -> Vec<NeedRow> {
    let us = crate::tmux::US.to_string();
    let mut lines: Vec<String> = Vec::new();
    for w in windows {
        let label = if w.summary.is_empty() { &w.name } else { &w.summary };
        let raw = [w.id.as_str(), &w.session, &w.index.to_string(), &w.state, &w.workflow, &w.since, label].join(&us);
        for rec in raw.split('\n') {
            let f: Vec<&str> = rec.split(&us).collect();
            let g = |i: usize| f.get(i - 1).copied().unwrap_or("");
            if jump_excluded(g(2)) {
                continue;
            }
            let tier = match g(4) {
                "failed" => 0,
                "needs-input" => 1,
                "done" if g(5).is_empty() => 2,
                _ => continue,
            };
            let word = g(6).split([' ', '\t', '\n']).find(|s| !s.is_empty()).unwrap_or("");
            let stamp = if !word.is_empty() && word.bytes().all(|b| b.is_ascii_digit()) { word } else { NO_STAMP };
            let label = g(7).replace('\t', " ");
            lines.push(format!(
                "{tier}\t{stamp}\t{s}\t{ix:09}\t{id}\t{s}\t{idx}\t{st}\t{stamp}\t{label}",
                s = g(2),
                ix = awk_int(g(3)),
                id = g(1),
                idx = g(3),
                st = g(4)
            ));
        }
    }
    let field = |l: &str, k: usize| l.split('\t').nth(k).unwrap_or("").to_string();
    lines.sort_by(|a, b| {
        num_cmp(&field(a, 0), &field(b, 0))
            .then_with(|| num_cmp(&field(a, 1), &field(b, 1)))
            .then_with(|| coll.sort_cmp(&field(a, 2), &field(b, 2)))
            .then_with(|| coll.sort_cmp(&field(a, 3), &field(b, 3)))
            .then_with(|| coll.sort_cmp(a, b))
    });
    let mut seen = HashSet::new();
    lines
        .into_iter()
        .filter(|l| seen.insert(field(l, 4)))
        .map(|l| {
            let out: Vec<&str> = l.splitn(5, '\t').collect();
            let line = out.get(4).copied().unwrap_or("").to_string();
            let o: Vec<&str> = line.splitn(6, '\t').collect();
            let at = |k: usize| o.get(k).copied().unwrap_or("").to_string();
            NeedRow {
                id: at(0),
                session: at(1),
                index: at(2).parse().unwrap_or(0),
                state: at(3),
                stamp: at(4),
                label: at(5),
                line,
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tmux::tests::row;
    use crate::tmux::Window;

    fn win(session: &str, index: u32, id: &str, state: &str, since: Option<&str>, workflow: &str) -> Window {
        let mut set = vec![("state", state), ("workflow", workflow)];
        if let Some(s) = since {
            set.push(("since", s));
        }
        Window::parse(&row(session, index, id, &set)).unwrap()
    }

    /// agent-roster tests NEEDS_FIXTURE: every rule of `list`, and the ties
    /// that need its exact sort. Linked windows are extra rows with the same id.
    pub fn needs_fixture() -> Vec<Window> {
        let mut v = vec![
            win("main", 1, "@1", "done", Some("300 done"), ""),
            win("main", 2, "@2", "needs-input", Some("200 needs-input"), ""),
            win("work", 1, "@3", "failed", Some("400 failed"), ""),
            win("work", 2, "@4", "done", Some("50 done"), "1"),
            win("work", 3, "@5", "running", Some("10 running"), ""),
            win("work", 4, "@6", "", None, ""),
            win("main", 3, "@7", "needs-input", None, ""),
            win("main", 4, "@8", "needs-input", Some(""), ""),
            win("main", 5, "@9", "needs-input", Some("abc"), ""),
            win("main", 6, "@10", "needs-input", Some("12a needs-input"), ""),
            win("main", 7, "@11", "needs-input", Some("  150 needs-input"), ""),
            win("main", 8, "@12", "needs-input", Some("0150\tneeds-input"), ""),
            win("main", 9, "@13", "failed", Some("99999999999 failed"), ""),
            win("main", 10, "@14", "done", Some("300 done"), "0"),
            win("agents", 1, "@20", "failed", Some("1 failed"), ""),
            win("tasks", 1, "@21", "failed", Some("1 failed"), ""),
            win("stash", 1, "@22", "failed", Some("1 failed"), ""),
            win("scratch", 1, "@23", "failed", Some("1 failed"), ""),
            win("btop-popup", 1, "@24", "failed", Some("1 failed"), ""),
            win("tasks stash", 1, "@25", "failed", Some("1 failed"), ""),
            win("stash2", 1, "@26", "failed", Some("1 failed"), ""),
            win("B", 1, "@30", "done", Some("500 done"), ""),
            win("a", 1, "@31", "done", Some("500 done"), ""),
            win("_x", 1, "@32", "done", Some("500 done"), ""),
            win("Ä", 1, "@33", "done", Some("500 done"), ""),
            win("aa", 1, "@34", "done", Some("500 done"), ""),
            win("a-b", 1, "@35", "done", Some("500 done"), ""),
            win("Z", 1, "@36", "done", Some("500 done"), ""),
            win("10", 1, "@37", "done", Some("500 done"), ""),
            win("9", 1, "@38", "done", Some("500 done"), ""),
            win("a", 10, "@39", "done", Some("500 done"), ""),
            win("a", 2, "@40", "done", Some("500 done"), ""),
            win("dev 🚀", 1, "@60", "done", Some("700 done"), ""),
            win("dev 🔥", 1, "@61", "done", Some("700 done"), ""),
            win("dev 🔥🔥", 1, "@62", "done", Some("700 done"), ""),
            win("Ω", 9, "@71", "needs-input", None, ""),
            win("Ω", 7, "@72", "needs-input", None, ""),
            win("日本", 4, "@73", "needs-input", None, ""),
            win("ω", 1, "@79", "done", Some("800 done"), ""),
            win("Ω", 1, "@80", "done", Some("800 done"), ""),
            win("Ж本", 1, "@81", "done", Some("900 done"), ""),
            win("ωΩ🚀Ж", 1, "@82", "done", Some("900 done"), ""),
            win("zz", 1, "@50", "failed", Some("5 failed"), ""),
            win("stash", 2, "@51", "failed", Some("6 failed"), ""),
        ];
        // links: @50 also in main and stash; @51 also in yy
        for s in ["main", "stash"] {
            v.push(win(s, 50, "@50", "failed", Some("5 failed"), ""));
        }
        v.push(win("yy", 51, "@51", "failed", Some("6 failed"), ""));
        v
    }

    fn ids(rows: &[NeedRow]) -> Vec<&str> {
        rows.iter().map(|r| r.id.as_str()).collect()
    }

    #[test]
    fn exclude_matches_the_script() {
        let src = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../scripts/executable_agent-jump.sh");
        let text = std::fs::read_to_string(src).expect("agent-jump.sh source");
        let line = text.lines().find(|l| l.starts_with("EXCLUDE=\"")).expect("EXCLUDE line");
        assert_eq!(line, format!("EXCLUDE=\"{JUMP_EXCLUDE}\""));
    }

    #[test]
    fn fixture_exercises_the_rules() {
        let got = needs_order(&needs_fixture(), &Collator::new("en_US.UTF-8"));
        let g = ids(&got);
        assert_eq!(&g[..3], ["@26", "@50", "@51"]); // failed, oldest first; linked rows once
        for wid in ["@4", "@14", "@5", "@6", "@20", "@21", "@22", "@23", "@24", "@25"] {
            assert!(!g.contains(&wid), "{wid} queued");
        }
        let pos = |id: &str| g.iter().position(|x| *x == id).unwrap();
        assert!(pos("@40") < pos("@39")); // a:2 before a:10
        assert!(pos("@11") < pos("@12")); // both 150: main:7 before main:8
        assert!(pos("@12") < pos("@2")); // "0150" is 150, before 200
        assert!(pos("@9") < pos("@1")); // no stamp: last of its tier, not of all
        assert!(pos("@13") > pos("@3")); // 99999999999 is a real stamp, and bigger than 400
        // The kept row of a linked window is its first sorted one, never an excluded link.
        let r50 = got.iter().find(|r| r.id == "@50").unwrap();
        assert_eq!(r50.session, "main");
        let r51 = got.iter().find(|r| r.id == "@51").unwrap();
        assert_eq!(r51.session, "yy");
        // The stamp is printed as given.
        assert_eq!(got.iter().find(|r| r.id == "@12").unwrap().stamp, "0150");
        assert_eq!(got.iter().find(|r| r.id == "@7").unwrap().stamp, NO_STAMP);
    }

    #[test]
    fn locale_changes_ties() {
        let en = needs_order(&needs_fixture(), &Collator::new("en_US.UTF-8"));
        let c = needs_order(&needs_fixture(), &Collator::new("C"));
        let (en, c) = (ids(&en), ids(&c));
        let p = |v: &Vec<&str>, id: &str| v.iter().position(|x| *x == id).unwrap();
        assert!(p(&en, "@31") < p(&en, "@30")); // en_US: a before B
        assert!(p(&c, "@30") < p(&c, "@31")); // C: B before a
    }

    #[test]
    fn weightless_names() {
        let got = needs_order(&needs_fixture(), &Collator::new("en_US.UTF-8"));
        let g = ids(&got);
        let before = |a: &str, b: &str| {
            let (pa, pb) = (g.iter().position(|x| *x == a).unwrap(), g.iter().position(|x| *x == b).unwrap());
            assert!(pa < pb, "{a} before {b}: {g:?}");
        };
        before("@72", "@71"); // Ω:7 before Ω:9
        before("@71", "@73"); // Ω (1 char) before 日本 (2), whatever the index
        before("@60", "@62");
        before("@61", "@62"); // "dev 🔥🔥" is longer
        before("@60", "@61"); // equal keys: the line, i.e. the id, decides
        before("@79", "@80"); // ω:1 before Ω:1 by id, though Ω < ω in code points
        before("@81", "@82"); // shorter, though ω < Ж in code points
    }

    #[test]
    fn predicates() {
        let w = |state: &str, wf: &str, cua: &str| {
            Window::parse(&row("s", 1, "@1", &[("state", state), ("workflow", wf), ("cua", cua)])).unwrap()
        };
        assert!(is_attn(&w("failed", "", "")) && is_attn(&w("needs-input", "1", "")));
        assert!(!is_attn(&w("done", "1", "")) && is_attn(&w("done", "", "")));
        assert_eq!(cat(&w("done", "1", "")), Some(Cat::Working)); // done with a fleet out: working
        assert_eq!(cat(&w("idle", "", "1")), Some(Cat::Working));
        assert_eq!(cat(&w("idle", "", "")), Some(Cat::Idle));
        assert_eq!(cat(&w("", "", "")), None);
        assert_eq!(cat(&w("", "1", "")), Some(Cat::Working)); // the Python's quirk: flag alone counts
        assert_eq!(rank(&w("", "", "")), 9);
        assert_eq!(rank(&w("weird", "", "")), 4);
        assert_eq!(dot_color(&w("running", "", ""), true, false), Some("pink"));
        assert_eq!(dot_color(&w("running", "", ""), false, false), Some("blue"));
        assert_eq!(dot_color(&w("running", "", ""), true, true), Some("blue")); // never pulses under you
        assert_eq!(dot_color(&w("idle", "", ""), true, false), Some("overlay"));
        assert_eq!(dot_color(&w("", "", ""), true, false), None);
        assert_eq!(flag(&w("running", "1", "1"), true), Some((Flag::Workflow, "teal")));
        assert_eq!(flag(&w("needs-input", "", "1"), true), Some((Flag::Cua, "overlay")));
        assert_eq!(flag(&w("running", "", "1"), false), Some((Flag::Cua, "dimblue")));
        assert_eq!(state_words(&w("done", "1", "")), "done · fleet out");
        // CAT_HUE is dot()'s colour for the attention states (the Python pins this too).
        for (s, c) in [("failed", Cat::Failed), ("needs-input", Cat::NeedsInput), ("done", Cat::Done)] {
            assert_eq!(dot_color(&w(s, "", ""), true, false), Some(c.hue()));
        }
    }

    #[test]
    fn counting() {
        let ws: Vec<Window> = [("@1", "failed"), ("@2", "idle"), ("@2", "idle"), ("@3", ""), ("@4", "running")]
            .iter()
            .map(|(id, st)| Window::parse(&row("s", 1, id, &[("state", st)])).unwrap())
            .collect();
        let c = Counts::of(&ws);
        assert_eq!(c.nonzero(), vec![(Cat::Failed, 1), (Cat::Working, 1), (Cat::Idle, 1)]);
        assert_eq!(c.total(), 3);
    }

    #[test]
    fn free_text_follows_awk_records() {
        let c = Collator::new("C");
        let mut ws = Vec::new();
        for (id, summary, since) in [
            ("@1", "a\nb", "5 x"),
            ("@2", "c\x1fd", "6 x"),
            ("@3", "x\n@9\x1fmain\x1f7\x1ffailed\x1f\x1f1\x1ffake", "7 x"),
            ("@4", "tab\there", "8\nx"),
        ] {
            let mut w = Window::parse(&row("main", 1, id, &[("state", "failed"), ("since", "0")])).unwrap();
            w.summary = summary.into();
            w.since = since.into();
            ws.push(w);
        }
        let got: Vec<String> = needs_order(&ws, &c).iter().map(|r| r.list_line()).collect();
        assert_eq!(got, [
            "@9\tmain\t7\tfailed\t1\tfake", // a record smuggled in after a newline: awk lists it too
            "@1\tmain\t1\tfailed\t5\ta",
            "@2\tmain\t1\tfailed\t6\tc",
            "@3\tmain\t1\tfailed\t7\tx",
            "@4\tmain\t1\tfailed\t8\t", // a newline in since ends the record before the label
        ]);
        assert_eq!((awk_int("12a"), awk_int(""), awk_int(" -3.9"), awk_int("1e3x"), awk_int("x1")), (12, 0, -3, 1000, 0));
    }

    #[test]
    fn big_stamps_compare_numerically() {
        assert_eq!(num_cmp("99999999999", "9999999999"), Ordering::Greater);
        assert_eq!(num_cmp("0150", "150"), Ordering::Equal);
        assert_eq!(num_cmp("200", "0150"), Ordering::Greater);
    }
}
