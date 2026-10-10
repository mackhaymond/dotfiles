//! The picker's rows: sessions in MRU order with their agent rollup, and
//! the name matcher. A port of `scripts/mru-session-switch.sh`'s list
//! pipeline and `decorate()`.
//!
//! ## Rows
//! `list-sessions` sorted `-k1,1nr` on `session_last_attached`, minus the
//! client's current session and [`EXCLUDE`] (exact names, the script's awk).
//! `sort` has no `-s`, so equal stamps fall back to the whole line
//! (`<stamp>\t<name>`) in the locale's collation, ascending; a session never
//! attached prints an empty stamp, sorted as 0. Every session has a window,
//! so the core snapshot's `list-windows -a` rows carry all of this: no
//! second tmux call.
//!
//! ## Rollup
//! One per session with any agent window: a dot in the colour of its most
//! urgent window, then counts. The rank is [`crate::model::rank`], which is
//! the script's `rank()` exactly (failed 0 > needs-input 1 > done without a
//! workflow 2 > running / workflow / cua 3 > other agent state 4 > none 9).
//! The dot's colour is the script's static table: unlike the core's
//! `dot_color`, in flight is always pink (no blink, no blue for the window
//! you are on). Counts are per window row, like the script's awk.

use crate::collate::Collator;
use crate::model;
use crate::tmux::Snapshot;

/// Sessions the picker never lists (the script's awk filter, exact names).
pub const EXCLUDE: [&str; 4] = ["scratch", "agents", "tasks", "stash"];

/// A session's agent rollup.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Rollup {
    /// The most urgent rank (0..=4).
    pub best: u8,
    /// rank <= 2: failed, needs-input, done.
    pub need: usize,
    /// rank 3: in flight.
    pub work: usize,
    /// rank 4.
    pub idle: usize,
}

impl Rollup {
    /// The script's `col[best]`, by palette name.
    pub fn color(&self) -> &'static str {
        match self.best {
            0 => "red",
            1 => "yellow",
            2 => "green",
            3 => "pink",
            _ => "overlay",
        }
    }

    /// "N needs you · M working", or "N idle".
    pub fn text(&self) -> String {
        let mut out = String::new();
        if self.need > 0 {
            out = format!("{} needs you", self.need);
        }
        if self.work > 0 {
            if !out.is_empty() {
                out.push_str(" · ");
            }
            out.push_str(&format!("{} working", self.work));
        }
        if out.is_empty() {
            out = format!("{} idle", self.idle);
        }
        out
    }

    fn add(&mut self, r: u8) {
        if r <= 2 {
            self.need += 1;
        } else if r == 3 {
            self.work += 1;
        } else {
            self.idle += 1;
        }
    }
}

/// One listed session.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionRow {
    pub name: String,
    /// session_last_attached (0: never).
    pub last: i64,
    pub rollup: Option<Rollup>,
}

/// What one snapshot gives the picker.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Sessions {
    /// The listed rows, MRU first.
    pub rows: Vec<SessionRow>,
    /// Every session on the server (listed or not), for Enter on a typed name.
    pub all: Vec<String>,
    /// The client's session, when the client was found.
    pub current: Option<String>,
}

impl Sessions {
    pub fn exists(&self, name: &str) -> bool {
        self.all.iter().any(|s| s == name)
    }
}

/// The snapshot → the picker's sessions, for `client` (its own session is
/// left out).
pub fn build(snap: &Snapshot, client: Option<&str>, coll: &Collator) -> Sessions {
    let current = client.and_then(|c| snap.client(c)).map(|c| c.session.clone()).filter(|s| !s.is_empty());
    let mut rows: Vec<SessionRow> = Vec::new();
    let mut all: Vec<String> = Vec::new();
    for w in &snap.windows {
        let pos = match all.iter().position(|s| *s == w.session) {
            Some(p) => p,
            None => {
                all.push(w.session.clone());
                rows.push(SessionRow { name: w.session.clone(), last: w.last_attached, rollup: None });
                all.len() - 1
            }
        };
        let r = model::rank(w);
        if r != 9 {
            let ru = rows[pos].rollup.get_or_insert(Rollup { best: r, ..Rollup::default() });
            ru.best = ru.best.min(r);
            ru.add(r);
        }
    }
    rows.retain(|r| Some(&r.name) != current.as_ref() && !EXCLUDE.contains(&r.name.as_str()));
    // sort -t TAB -k1,1nr: stamps descending; ties by the whole line.
    let line = |r: &SessionRow| if r.last == 0 { format!("\t{}", r.name) } else { format!("{}\t{}", r.last, r.name) };
    rows.sort_by(|a, b| b.last.cmp(&a.last).then_with(|| coll.sort_cmp(&line(a), &line(b))));
    Sessions { rows, all, current }
}

/// `^[A-Za-z0-9_-]+$`: no `.` (tmux turns it into `_`, and the switch to
/// the typed name would then fail) and nothing tmux reads as a target.
pub fn valid_name(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

/// The script's message for a name that can't be created.
pub fn invalid_message(name: &str) -> String {
    format!("Invalid session name (allowed: A-Z a-z 0-9 _ -): {name}")
}

/// How a name matches a query: lower sorts first.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Score {
    /// 0 exact, 1 prefix, 2 substring, 3 subsequence (all case-insensitive).
    pub tier: u8,
    /// Within a tier: the substring's offset, the subsequence's span.
    pub key: usize,
    /// Matched char positions in the name (for highlighting).
    pub pos: Vec<usize>,
}

/// Case-fold per char (a char whose lowercase is several chars, like "İ",
/// folds to the first of them), so name and query fold alike and positions
/// stay char positions.
fn fold(s: &str) -> Vec<char> {
    s.chars().map(|c| c.to_lowercase().next().unwrap_or(c)).collect()
}

/// A small fzf-like scorer over the NAME: None when `q` is not a
/// case-insensitive subsequence of it. An empty query matches everything.
pub fn score(name: &str, q: &str) -> Option<Score> {
    if q.is_empty() {
        return Some(Score { tier: 0, key: 0, pos: Vec::new() });
    }
    let n = fold(name);
    let q = fold(q);
    if q.len() > n.len() {
        return None;
    }
    let range = |a: usize| (a..a + q.len()).collect::<Vec<_>>();
    if n == q {
        return Some(Score { tier: 0, key: 0, pos: range(0) });
    }
    if n.starts_with(&q) {
        return Some(Score { tier: 1, key: 0, pos: range(0) });
    }
    if let Some(at) = n.windows(q.len()).position(|w| w == q.as_slice()) {
        return Some(Score { tier: 2, key: at, pos: range(at) });
    }
    // Subsequence: the tightest window (shortest span, then earliest).
    let mut best: Option<(usize, Vec<usize>)> = None;
    for start in 0..n.len() {
        if n[start] != q[0] {
            continue;
        }
        let mut pos = vec![start];
        let mut j = start + 1;
        for &qc in &q[1..] {
            while j < n.len() && n[j] != qc {
                j += 1;
            }
            if j == n.len() {
                break;
            }
            pos.push(j);
            j += 1;
        }
        if pos.len() < q.len() {
            break; // no later start can match either
        }
        let span = pos[pos.len() - 1] - start;
        if best.as_ref().is_none_or(|(s, _)| span < *s) {
            best = Some((span, pos));
        }
    }
    best.map(|(span, pos)| Score { tier: 3, key: span, pos })
}

/// The rows matching `q`, best first; equal scores keep MRU order.
pub fn filter(rows: &[SessionRow], q: &str) -> Vec<(usize, Score)> {
    let mut v: Vec<(usize, Score)> = rows.iter().enumerate().filter_map(|(i, r)| score(&r.name, q).map(|s| (i, s))).collect();
    v.sort_by(|a, b| a.1.tier.cmp(&b.1.tier).then(a.1.key.cmp(&b.1.key)).then(a.0.cmp(&b.0)));
    v
}

/// Order helper for tests and callers that want the names only.
pub fn names(rows: &[SessionRow], q: &str) -> Vec<String> {
    filter(rows, q).into_iter().map(|(i, _)| rows[i].name.clone()).collect()
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::tmux::tests::row;

    /// A window's (state, workflow, cua).
    type Win<'a> = (&'a str, &'a str, &'a str);

    /// Window rows: (session, last_attached, [window]).
    pub fn snap(spec: &[(&str, &str, &[Win])], clients: &[(&str, &str)]) -> Snapshot {
        let mut lines = Vec::new();
        let mut id = 0;
        for (s, last, wins) in spec {
            let wins: Vec<Win> = if wins.is_empty() { vec![("", "", "")] } else { wins.to_vec() };
            for (i, (st, wf, cua)) in wins.iter().enumerate() {
                id += 1;
                lines.push(row(s, i as u32 + 1, &format!("@{id}"), &[("last_attached", last), ("state", st),
                    ("workflow", wf), ("cua", cua)]));
            }
        }
        for (tty, s) in clients {
            lines.push(format!("\x1fclient\x1f{tty}\x1f@1\x1f{s}"));
        }
        Snapshot::parse(&lines.join("\n"))
    }

    pub fn fixture() -> Snapshot {
        snap(&[
            ("home", "500", &[("idle", "", "")]),
            ("work", "400", &[("failed", "", ""), ("running", "", ""), ("idle", "", "")]),
            ("workshop", "300", &[("running", "", ""), ("", "1", "")]),
            ("scratch", "900", &[]),
            ("agents", "900", &[("failed", "", "")]),
            ("tasks", "900", &[]),
            ("stash", "900", &[("needs-input", "", "")]),
            ("wiki", "", &[]),
            ("beta", "", &[("done", "1", "")]),
            ("alpha", "450", &[("done", "", ""), ("needs-input", "", "")]),
        ], &[("/dev/ttys001", "home")])
    }

    #[test]
    fn mru_order_and_excludes() {
        let c = Collator::new("C");
        let s = build(&fixture(), Some("/dev/ttys001"), &c);
        let names: Vec<&str> = s.rows.iter().map(|r| r.name.as_str()).collect();
        // home is the client's own session; the four hidden ones never show;
        // never-attached ties (beta, wiki) by name.
        assert_eq!(names, ["alpha", "work", "workshop", "beta", "wiki"]);
        assert_eq!(s.current.as_deref(), Some("home"));
        assert!(s.exists("scratch") && s.exists("home") && !s.exists("nope"));
        // No client: nothing but the fixed list is left out.
        let s = build(&fixture(), None, &c);
        assert_eq!(s.rows[0].name, "home");
        assert_eq!(s.current, None);
        let s = build(&fixture(), Some("/dev/gone"), &c);
        assert_eq!(s.rows[0].name, "home");
    }

    #[test]
    fn rollup_rank_and_text() {
        let s = build(&fixture(), Some("/dev/ttys001"), &Collator::new("C"));
        let get = |n: &str| s.rows.iter().find(|r| r.name == n).unwrap().rollup;
        let alpha = get("alpha").unwrap();
        assert_eq!((alpha.color(), alpha.text().as_str()), ("yellow", "2 needs you")); // needs-input beats done
        let work = get("work").unwrap();
        assert_eq!((work.color(), work.text().as_str()), ("red", "1 needs you · 1 working")); // idle not shown
        let shop = get("workshop").unwrap();
        assert_eq!((shop.color(), shop.text().as_str()), ("pink", "2 working")); // a workflow alone is in flight
        // done while a workflow is out: not green, in flight.
        let beta = get("beta").unwrap();
        assert_eq!((beta.color(), beta.text().as_str()), ("pink", "1 working"));
        assert_eq!(get("wiki"), None); // no agent: no rollup
        let idle = Rollup { best: 4, need: 0, work: 0, idle: 3 };
        assert_eq!((idle.color(), idle.text().as_str()), ("overlay", "3 idle"));
    }

    #[test]
    fn names_are_validated_like_the_script() {
        for ok in ["work", "a-b_C9", "-x", "_"] {
            assert!(valid_name(ok), "{ok}");
        }
        for bad in ["", "a.b", "a b", "a:b", "é", "x/y", "=x"] {
            assert!(!valid_name(bad), "{bad}");
        }
        assert_eq!(invalid_message("a.b"), "Invalid session name (allowed: A-Z a-z 0-9 _ -): a.b");
    }

    fn rows(v: &[&str]) -> Vec<SessionRow> {
        v.iter().map(|n| SessionRow { name: n.to_string(), last: 0, rollup: None }).collect()
    }

    #[test]
    fn matcher_ranking() {
        let r = rows(&["dotfiles", "workshop", "network", "Work", "wok", "w-o-r-k-x"]);
        // exact (any case) > prefix > substring > subsequence; MRU within a tier.
        assert_eq!(names(&r, "work"), ["Work", "workshop", "network", "w-o-r-k-x"]);
        assert_eq!(names(&r, "WO"), ["workshop", "Work", "wok", "network", "w-o-r-k-x"]);
        assert_eq!(names(&r, "dtf"), ["dotfiles"]);
        assert_eq!(names(&r, "zz"), Vec::<String>::new());
        assert_eq!(names(&r, ""), ["dotfiles", "workshop", "network", "Work", "wok", "w-o-r-k-x"]);
        // The tightest subsequence wins and its positions are reported.
        let s = score("a-b-ab", "ab").unwrap();
        assert_eq!((s.tier, s.pos), (2, vec![4, 5]));
        let s = score("axxbab", "ab").unwrap();
        assert_eq!(s.tier, 2);
        let s = score("axbyyab", "aab").unwrap();
        assert_eq!((s.tier, s.key, s.pos), (3, 6, vec![0, 5, 6]));
        let s = score("x-a-b-ab", "a-b").unwrap();
        assert_eq!((s.tier, s.key), (2, 2));
        let s = score("ab-xa-yb", "a-b").unwrap(); // a(4)-(5)b(7) is tighter than a(0)-(2)b(7)
        assert_eq!((s.tier, s.key, s.pos), (3, 3, vec![4, 5, 7]));
        assert_eq!(score("ab", "abc"), None);
        assert_eq!(score("İstanbul", "İst").map(|s| s.tier), Some(1));
    }

    #[test]
    fn rollup_text_never_matches() {
        // "2 working" is a rollup, not a name: typing "work" can't hit it.
        let s = build(&fixture(), Some("/dev/ttys001"), &Collator::new("C"));
        assert_eq!(names(&s.rows, "working"), Vec::<String>::new());
        assert_eq!(names(&s.rows, "needs"), Vec::<String>::new());
    }

    /// The script's own decorate() over the same rows (tmux faked by a shell
    /// function): the colours and text must be ours.
    #[test]
    fn decorate_parity_with_the_script() {
        let src = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../scripts/executable_mru-session-switch.sh");
        let Ok(text) = std::fs::read_to_string(&src) else { return };
        let start = text.find("decorate() {").expect("decorate()");
        let end = start + text[start..].find("\n}\n").expect("end of decorate") + 3;
        let func = &text[start..end];
        let snap = fixture();
        let us = '\x1f';
        let fake: Vec<String> = snap.windows.iter().map(|w| format!("{}{us}{}{us}{}{us}{}", w.session, w.state, w.workflow, w.cua)).collect();
        let c = Collator::new("C");
        let s = build(&snap, None, &c);
        let list: Vec<&str> = s.rows.iter().map(|r| r.name.as_str()).collect();
        let dir = std::env::temp_dir().join(format!("agentui-decorate-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("fake.txt"), fake.join("\n") + "\n").unwrap();
        std::fs::write(dir.join("f.sh"), format!("tmux() {{ cat '{}'; }}\n{func}\ndecorate \"$1\"\n", dir.join("fake.txt").display())).unwrap();
        let out = std::process::Command::new("bash").arg(dir.join("f.sh")).arg(list.join("\n")).output().unwrap();
        let _ = std::fs::remove_dir_all(&dir);
        let out = String::from_utf8(out.stdout).unwrap();
        let lines: Vec<&str> = out.lines().collect();
        assert_eq!(lines.len(), s.rows.len(), "{out}");
        for (l, r) in lines.iter().zip(&s.rows) {
            let (name, deco) = l.split_once('\t').unwrap();
            assert_eq!(name, r.name);
            match r.rollup {
                None => assert_eq!(deco, "", "{name}"),
                Some(ru) => {
                    let (rr, gg, bb) = crate::palette::rgb(ru.color());
                    let want = format!("\x1b[38;2;{rr};{gg};{bb}m●\x1b[0m \x1b[38;2;108;112;134m{}\x1b[0m", ru.text());
                    assert_eq!(deco, want, "{name}");
                }
            }
        }
    }

    /// The script's list pipeline (list-sessions | awk | sort | cut) with tmux
    /// faked: same rows, same order, ties included.
    #[test]
    fn list_parity_with_the_script() {
        let src = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../scripts/executable_mru-session-switch.sh");
        let Ok(text) = std::fs::read_to_string(&src) else { return };
        let start = text.find("    tmux list-sessions").expect("list pipeline");
        let end = start + text[start..].find("  } || true").expect("end of pipeline");
        let pipe = &text[start..end];
        let snap = snap(&[
            ("zeta", "", &[]), ("Beta", "", &[]), ("alpha", "", &[]), ("one", "100", &[]), ("two", "100", &[]),
            ("three", "300", &[]), ("cur", "999", &[]), ("stash", "5", &[]), ("b", "", &[]),
        ], &[("/dev/x", "cur")]);
        let mut fake = String::new();
        let mut seen = Vec::new();
        for w in &snap.windows {
            if !seen.contains(&w.session) {
                seen.push(w.session.clone());
                let stamp = if w.last_attached == 0 { String::new() } else { w.last_attached.to_string() };
                fake.push_str(&format!("{stamp}\t{}\n", w.session));
            }
        }
        for loc in ["C", "en_US.UTF-8"] {
            let dir = std::env::temp_dir().join(format!("agentui-mru-{}-{loc}", std::process::id()));
            std::fs::create_dir_all(&dir).unwrap();
            std::fs::write(dir.join("fake.txt"), &fake).unwrap();
            std::fs::write(dir.join("f.sh"), format!("tmux() {{ cat '{}'; }}\ncurrent_session=cur\n{pipe}\n",
                dir.join("fake.txt").display())).unwrap();
            let out = std::process::Command::new("bash").arg(dir.join("f.sh")).env("LC_ALL", loc).output().unwrap();
            let _ = std::fs::remove_dir_all(&dir);
            let want: Vec<String> = String::from_utf8(out.stdout).unwrap().lines().map(String::from).collect();
            let got: Vec<String> = build(&snap, Some("/dev/x"), &Collator::new(loc)).rows.into_iter().map(|r| r.name).collect();
            assert_eq!(got, want, "{loc}");
        }
    }
}
