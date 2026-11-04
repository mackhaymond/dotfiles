//! `keys.tsv`, the key catalog: parse it and filter it. No I/O here.
//!
//! The file's own header documents the columns; this reads columns 1-3 and
//! 6 (section, key, description, note). Columns 4-5 (tmux table and key
//! names) exist for `tests/test_keys_catalog.py`, which checks them against
//! a server running the real config.

/// One key.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    /// As Mack reads it (`⌥W a-z`): whitespace-separated parts, each drawn
    /// as its own key cap, except [`is_word`] parts.
    pub key: String,
    pub desc: String,
    /// A dim detail after the description; may be empty.
    pub note: String,
    /// Lowercase text the filter matches: the key (with its glyphs spelled
    /// out too: ⌘ "cmd", ⌥ "opt alt", ...), the description and the note.
    hay: String,
    /// The key's parts, lowercase, as written and spelled out, `/` splitting
    /// too (`⌥W a-z` → `⌥w`, `a-z`, `opt`, `alt`, `w`; `⌃H/J/K/L` → ...
    /// `h`, `j`): what a one-letter word must equal.
    toks: Vec<String>,
}

/// A titled group of keys, in file order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Section {
    pub name: String,
    pub entries: Vec<Entry>,
}

/// The catalog as built into the binary: the source tree's copy at build
/// time. `agent-ui keys` reads `~/.config/tmux/keys.tsv` first, so an edit
/// shows at the next open without a rebuild; this is the fallback.
pub const EMBEDDED: &str = include_str!("../../../keys.tsv");

/// The glyphs a key is written with, and what typing them out looks like.
const SPELLED: [(char, &str); 11] = [
    ('⌘', "cmd "), ('⌥', "opt alt "), ('⌃', "ctrl "), ('⇧', "shift "), ('⏎', "enter return "),
    ('⇥', "tab "), ('␣', "space "), ('↑', "up "), ('↓', "down "), ('↔', " "), ('…', " "),
];

/// The prefix, as keys.tsv writes it.
pub const PREFIX: &str = "⌃S";

/// A part of a key that is a joining word, not a key: drawn as plain text.
pub fn is_word(part: &str) -> bool {
    matches!(part, "/" | "then" | "or")
}

/// A key's parts, each with whether it is drawn as a key cap: all of them
/// but a joining word BETWEEN others (`⌃P / ⌃N`); a lone `/` is the key.
pub fn parts(key: &str) -> Vec<(&str, bool)> {
    let v: Vec<&str> = key.split_whitespace().collect();
    let n = v.len();
    v.into_iter().map(|p| (p, n < 2 || !is_word(p))).collect()
}

/// The key with its glyphs spelled out (`⌥W a-z` → `opt alt w a-z`).
pub fn spelled(key: &str) -> String {
    let mut out = String::new();
    for c in key.chars() {
        match SPELLED.iter().find(|(g, _)| *g == c) {
            Some((_, s)) => out.push_str(s),
            None => out.extend(c.to_lowercase()),
        }
    }
    out
}

impl Entry {
    pub fn new(key: &str, desc: &str, note: &str) -> Entry {
        let parts: Vec<&str> = key.split_whitespace().collect();
        // A leading ⌃S before more parts is THE PREFIX, not a key: it reads
        // as "prefix" / "ctrl", never as the letter s (or `s` would find
        // every prefix row and never ⌥S).
        let prefixed = parts.len() > 1 && parts[0] == PREFIX;
        let mut toks: Vec<String> = Vec::new();
        for (i, p) in parts.iter().enumerate() {
            if i == 0 && prefixed {
                toks.extend(["⌃s", "prefix", "ctrl"].map(String::from));
                continue;
            }
            let (low, sp) = (p.to_lowercase(), spelled(p));
            for t in low.split_whitespace().chain(sp.split_whitespace()) {
                toks.push(t.to_string());
                toks.extend(t.split('/').filter(|s| !s.is_empty() && *s != t).map(String::from));
            }
        }
        let hay = format!("{} {} {}{} {}", key.to_lowercase(), spelled(key), if prefixed { "prefix " } else { "" },
            desc.to_lowercase(), note.to_lowercase());
        Entry { key: key.to_string(), desc: desc.to_string(), note: note.to_string(), hay, toks }
    }

    /// Every whitespace-separated word of `query` (case-insensitive) occurs
    /// in the key, the description or the note. A one-letter (or digit)
    /// word is a KEY: it must be a whole part of the key (`opt w` is ⌥W,
    /// never "New space" for its w). An empty query matches.
    pub fn matches(&self, query: &str) -> bool {
        query.split_whitespace().all(|w| {
            let w = w.to_lowercase();
            let mut cs = w.chars();
            match (cs.next(), cs.next()) {
                (Some(c), None) if c.is_ascii_alphanumeric() => self.toks.contains(&w),
                _ => self.hay.contains(&w),
            }
        })
    }
}

/// The catalog text → its sections. Blank lines and `#` comments are
/// skipped, so is a row without a key or a description; a row joins the
/// section of that name wherever it appears (the first row places it).
pub fn parse(text: &str) -> Vec<Section> {
    let mut out: Vec<Section> = Vec::new();
    for line in text.lines() {
        let line = line.trim_end_matches('\r');
        if line.trim().is_empty() || line.trim_start().starts_with('#') {
            continue;
        }
        let cols: Vec<&str> = line.split('\t').map(str::trim).collect();
        let col = |i: usize| cols.get(i).copied().unwrap_or("");
        let (section, key, desc, note) = (col(0), col(1), col(2), col(5));
        if section.is_empty() || key.is_empty() || desc.is_empty() {
            continue;
        }
        let e = Entry::new(key, desc, note);
        match out.iter_mut().find(|s| s.name == section) {
            Some(s) => s.entries.push(e),
            None => out.push(Section { name: section.to_string(), entries: vec![e] }),
        }
    }
    out
}

/// The sections with only the entries `query` matches; sections left empty
/// go. A section whose NAME contains every word keeps all its entries, but
/// only for words of 2+ characters: a one-letter word is a key (`t` is
/// ⌘T and prefix t, not every row of TABS).
pub fn filter(sections: &[Section], query: &str) -> Vec<Section> {
    let q = query.trim();
    if q.is_empty() {
        return sections.to_vec();
    }
    sections
        .iter()
        .filter_map(|s| {
            let name = s.name.to_lowercase();
            let whole = q.split_whitespace().all(|w| w.chars().count() >= 2 && name.contains(&w.to_lowercase()));
            let entries: Vec<Entry> = s.entries.iter().filter(|e| whole || e.matches(q)).cloned().collect();
            (!entries.is_empty()).then(|| Section { name: s.name.clone(), entries })
        })
        .collect()
}

/// How many entries.
pub fn count(sections: &[Section]) -> usize {
    sections.iter().map(|s| s.entries.len()).sum()
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    pub const SAMPLE: &str = "# a comment\n\
        Spaces\t⌘1-9\tGo to space N\troot\tUser18..User26\t⌃S 1-9 too\n\
        \n\
        Jump\t⌥W a-z\tJump to that agent\tjump\ta..z\n\
        Spaces\t⌥N\tNew space\troot\tM-n\n\
        Sidebar\t⌘B\tSidebar on / off\n\
        Broken\t\tno key\n\
        Broken\t⌘X\n";

    #[test]
    fn parses_sections_in_first_appearance_order() {
        let s = parse(SAMPLE);
        let names: Vec<&str> = s.iter().map(|s| s.name.as_str()).collect();
        assert_eq!(names, ["Spaces", "Jump", "Sidebar"]);
        assert_eq!(s[0].entries.len(), 2);
        assert_eq!((s[0].entries[0].key.as_str(), s[0].entries[0].note.as_str()), ("⌘1-9", "⌃S 1-9 too"));
        assert_eq!(s[0].entries[1].key, "⌥N");
        // A row with only 3 columns has no note.
        assert_eq!(s[2].entries[0].note, "");
        assert_eq!(count(&s), 4);
    }

    #[test]
    fn crlf_and_trailing_tabs() {
        let s = parse("A\tk\tdesc\t\t\t\r\n");
        assert_eq!(s[0].entries[0].desc, "desc");
        assert_eq!(s[0].entries[0].note, "");
    }

    #[test]
    fn filter_matches_key_glyphs_words_and_case() {
        let s = parse(SAMPLE);
        let keys = |q: &str| -> Vec<String> {
            filter(&s, q).iter().flat_map(|s| s.entries.iter().map(|e| e.key.clone())).collect()
        };
        assert_eq!(keys(""), ["⌘1-9", "⌥N", "⌥W a-z", "⌘B"]);
        assert_eq!(keys("SIDEBAR"), ["⌘B"]); // the section name: all of it
        assert_eq!(keys("opt w a-z"), ["⌥W a-z"]); // ⌥ spelled out
        // A one-letter word is a key part, never a letter inside a word.
        assert_eq!(keys("opt w"), ["⌥W a-z"]);
        assert_eq!(keys("n"), ["⌥N"]);
        assert_eq!(keys("B"), ["⌘B"]);
        assert!(Entry::new("⌃H/J/K/L", "Move", "").matches("ctrl j"));
        assert_eq!(keys("alt"), ["⌥N", "⌥W a-z"]);
        assert_eq!(keys("cmd"), ["⌘1-9", "⌘B"]);
        assert_eq!(keys("⌘"), ["⌘1-9", "⌘B"]);
        assert_eq!(keys("space"), ["⌘1-9", "⌥N"]); // in the descriptions
        assert_eq!(keys("too"), ["⌘1-9"]); // the note
        assert!(keys("zzz").is_empty());
        assert!(filter(&s, "zzz").is_empty()); // empty sections go
    }

    /// One letter on the real catalog: the keys with that letter, never a
    /// whole section by its name, never every prefix row for the s of ⌃S.
    #[test]
    fn one_letter_queries_on_the_catalog() {
        let s = parse(EMBEDDED);
        let keys = |q: &str| -> Vec<String> {
            filter(&s, q).iter().flat_map(|s| s.entries.iter().map(|e| e.key.clone())).collect()
        };
        let t = keys("t");
        for k in ["⌘T", "⌘⇧T", "⌘⌥T", "⌃⌘T", "⌃S t"] {
            assert!(t.contains(&k.to_string()), "t: {t:?}");
        }
        assert!(!t.contains(&"⌘R".to_string()) && !t.contains(&"⌃S q".to_string()), "t: {t:?}");
        assert!(t.len() <= 8, "t: {t:?}");
        let d = keys("d");
        assert!(d.contains(&"⌃S d".to_string()) && d.contains(&"⌃S D".to_string()) && d.contains(&"␣ d".to_string()));
        assert!(!d.contains(&"⌥L".to_string()) && !d.contains(&"⌘B".to_string()), "d: {d:?}");
        assert!(d.len() <= 5, "d: {d:?}");
        let sq = keys("s");
        assert!(sq.contains(&"⌥S".to_string()) && sq.contains(&"␣ s".to_string()), "s: {sq:?}");
        assert!(!sq.contains(&"⌃S q".to_string()) && !sq.contains(&"⌃S d".to_string()), "s: {sq:?}");
        assert!(sq.len() <= 6, "s: {sq:?}");
        // "prefix" (and "ctrl") still find the prefix rows; a section name of
        // 2+ letters still brings its whole section.
        assert!(keys("prefix q").contains(&"⌃S q".to_string()));
        let tabs = keys("tabs");
        assert!(s.iter().find(|x| x.name == "Tabs").unwrap().entries.iter().all(|e| tabs.contains(&e.key)), "{tabs:?}");
    }

    #[test]
    fn words_and_spelling() {
        assert!(is_word("/") && is_word("then") && !is_word("⌘T"));
        assert_eq!(parts("⌃P / ⌃N"), [("⌃P", true), ("/", false), ("⌃N", true)]);
        assert_eq!(parts("/"), [("/", true)]); // the search key itself
        assert_eq!(parts("x then ⇧Y"), [("x", true), ("then", false), ("⇧Y", true)]);
        assert_eq!(spelled("⌘⇧T"), "cmd shift t");
        assert_eq!(spelled("␣ y"), "space  y");
    }

    #[test]
    fn the_embedded_catalog_parses_and_stays_short() {
        let s = parse(EMBEDDED);
        assert!(s.len() >= 6, "{} sections", s.len());
        assert!(count(&s) >= 50);
        for sec in &s {
            for e in &sec.entries {
                let w = crate::text::dwidth(&e.desc);
                assert!(w <= 50, "{}: {:?} is {w} cells", sec.name, e.desc);
                assert!(crate::text::dwidth(&e.key) <= 16, "{:?}", e.key);
            }
        }
        // prefix ? itself is listed.
        assert!(s.iter().flat_map(|s| &s.entries).any(|e| e.key == "⌃S ?"));
    }
}
