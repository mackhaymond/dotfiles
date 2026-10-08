//! Cell widths, clipping and small formatters, mirroring agent-roster.py's
//! `char_width` / `clusters` / `dwidth` / `clip` / `fit_label` / `ago` and its
//! `CONTROL` scrub.
//!
//! Widths are measured the way tmux draws them: pictographic blocks count two
//! cells even where an older Unicode table says one (`EMOJI_WIDE`), a VS16
//! makes the character before it two cells ("❤️"), and zero-width marks ride
//! on the character before them. Over-counting a cell only costs padding;
//! under-counting overflows the row.

use unicode_width::UnicodeWidthChar;

/// agent-roster.py `EMOJI_WIDE`: pictographic blocks tmux draws two cells wide.
pub const EMOJI_WIDE: [(u32, u32); 4] =
    [(0x1F300, 0x1F64F), (0x1F680, 0x1F6FF), (0x1F900, 0x1F9FF), (0x1FA70, 0x1FAFF)];
/// Variation selector 16 (emoji presentation).
pub const VS16: char = '\u{FE0F}';

/// Format characters (Unicode category Cf) the Python counts as zero width
/// through `unicodedata.category(c) == "Cf"`; `unicode-width` gives some of
/// these one cell, so they are listed here.
fn is_format(o: u32) -> bool {
    matches!(o,
        0x00AD | 0x0600..=0x0605 | 0x061C | 0x06DD | 0x070F | 0x0890..=0x0891 | 0x08E2 | 0x180E
        | 0x200B..=0x200F | 0x202A..=0x202E | 0x2060..=0x2064 | 0x2066..=0x206F | 0xFEFF
        | 0xFFF9..=0xFFFB | 0x110BD | 0x110CD | 0x13430..=0x1343F | 0x1BCA0..=0x1BCA3
        | 0x1D173..=0x1D17A | 0xE0001 | 0xE0020..=0xE007F)
}

/// agent-roster.py `char_width`: 0, 1 or 2 cells for one code point.
pub fn char_width(c: char) -> usize {
    let o = c as u32;
    if o == 0x200D || (0xFE00..=0xFE0F).contains(&o) || is_format(o) {
        return 0;
    }
    match c.width() {
        Some(0) if !c.is_control() => return 0, // combining marks (Mn/Me)
        Some(2) => return 2,
        _ => {}
    }
    if EMOJI_WIDE.iter().any(|&(a, b)| (a..=b).contains(&o)) {
        return 2;
    }
    1
}

/// agent-roster.py `clusters`: `(text, cells)` per drawn cell group.
pub fn clusters(s: &str) -> Vec<(String, usize)> {
    if s.is_ascii() {
        return s.chars().map(|c| (c.to_string(), 1)).collect();
    }
    let mut out: Vec<(String, usize)> = Vec::new();
    for c in s.chars() {
        let w = char_width(c);
        match out.last_mut() {
            Some((t, cw)) if w == 0 => {
                t.push(c);
                if c == VS16 {
                    *cw = 2;
                }
            }
            _ => out.push((c.to_string(), w)),
        }
    }
    out
}

/// agent-roster.py `dwidth`: display width in cells.
pub fn dwidth(s: &str) -> usize {
    if s.is_ascii() {
        s.len()
    } else {
        clusters(s).iter().map(|(_, w)| w).sum()
    }
}

/// agent-roster.py `clip`: at most `width` cells; a clipped string ends in `…`
/// (which takes the last cell). Never splits a cluster (a VS16 stays on its char).
pub fn clip(s: &str, width: usize) -> String {
    if width == 0 {
        return String::new();
    }
    let cl = clusters(s);
    if cl.iter().map(|(_, w)| w).sum::<usize>() <= width {
        return s.to_string();
    }
    let mut out = String::new();
    let mut n = 0;
    for (t, cw) in cl {
        if n + cw > width - 1 {
            break;
        }
        out.push_str(&t);
        n += cw;
    }
    out.push('…');
    out
}

/// agent-roster.py `CONTROL.sub(" ", s)`: every C0/C1 control and DEL becomes a
/// space, so nothing typed into a window name or summary reaches the terminal raw.
pub fn sanitize(s: &str) -> String {
    s.chars()
        .map(|c| if (c as u32) < 0x20 || (0x7f..=0x9f).contains(&(c as u32)) { ' ' } else { c })
        .collect()
}

/// Agent detail / reason text as plain words for display. The hooks pass a
/// turn's text through raw, so it can carry markdown and tags, but it is
/// also full of code (`__init__.py`, `Vec<String>`, `sort <in >out`, `2**8`),
/// so only unmistakable markup goes:
///
/// - leading `#` / `>` / `-` / `*` / `+` line markers;
/// - markup tags ([`MARKUP_TAGS`], or any `<name-with-dash …>`), with
///   `name=value` attributes, their inner text kept; `<https://…>` → the URL;
/// - `[text](url)` → `text` when the target looks like a URL;
/// - `**` / `__` only as a matched pair at word boundaries; backticks;
///
/// then controls are [`sanitize`]d and whitespace runs (newlines too) become
/// one space.
pub fn plain(s: &str) -> String {
    let lines: Vec<&str> = s.split('\n').map(strip_markers).collect();
    let s = unlink(&strip_tags(&lines.join(" ")));
    let s = unemphasize(&unemphasize(&s, ['*', '*']), ['_', '_']).replace('`', "");
    sanitize(&s).split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Tag names [`plain`] strips besides any name with a `-` in it.
pub const MARKUP_TAGS: [&str; 17] = [
    "agent-message", "system-reminder", "task-notification", "command-name", "command-message",
    "command-args", "local-command-stdout", "b", "i", "em", "strong", "code", "br", "p", "pre", "kbd", "u",
];

/// The length of a markup tag starting right after its `<` (through its
/// `>`), or None: `/`?, a name of letters and `-` (listed, or with a `-`),
/// then ` key=value` attributes (value quoted or bare), then `/`? `>`.
fn tag_len(s: &str) -> Option<usize> {
    let b = s.as_bytes();
    let mut i = usize::from(b.first() == Some(&b'/'));
    let start = i;
    while i < b.len() && (b[i].is_ascii_alphabetic() || b[i] == b'-') {
        i += 1;
    }
    let name = s[start..i].to_ascii_lowercase();
    if !name.starts_with(|c: char| c.is_ascii_alphabetic()) || !(name.contains('-') || MARKUP_TAGS.contains(&name.as_str())) {
        return None;
    }
    loop {
        let gap = i;
        while b.get(i) == Some(&b' ') {
            i += 1;
        }
        match (b.get(i), b.get(i + 1)) {
            (Some(b'>'), _) => return Some(i + 1),
            (Some(b'/'), Some(b'>')) => return Some(i + 2),
            _ if i == gap => return None, // `<in.txt`, `<b and`: not a tag
            _ => {}
        }
        let key = i;
        while i < b.len() && (b[i].is_ascii_alphanumeric() || b[i] == b'-' || b[i] == b'_') {
            i += 1;
        }
        if i == key || b.get(i) != Some(&b'=') {
            return None;
        }
        i += 1;
        match b.get(i) {
            Some(&q @ (b'"' | b'\'')) => i += 2 + s[i + 1..].find(q as char)?,
            _ => {
                let v = i;
                while i < b.len() && !matches!(b[i], b' ' | b'>' | b'<') {
                    i += 1;
                }
                if i == v {
                    return None;
                }
            }
        }
    }
}

/// `<https://…>` starting right after its `<`: the length through `>`.
fn autolink_len(s: &str) -> Option<usize> {
    if !(s.starts_with("http://") || s.starts_with("https://")) {
        return None;
    }
    let end = s.find('>')?;
    (!s[..end].contains(|c: char| c.is_whitespace() || c == '<')).then_some(end + 1)
}

/// Is `[text](u)`'s `u` a link target (a scheme, mailto:, a path or anchor)?
fn urlish(u: &str) -> bool {
    if u.is_empty() || u.contains(char::is_whitespace) {
        return false;
    }
    let scheme = u.split_once("://").is_some_and(|(s, _)| !s.is_empty() && s.chars().all(|c| c.is_ascii_alphabetic()));
    scheme || u.starts_with("mailto:") || u.starts_with(['/', '#']) || u.starts_with("./") || u.starts_with("../")
}

/// Drop `d` (`**` or `__`) where it opens and closes emphasis: an opener
/// after the start, a space, or punctuation that does not follow a word
/// character, and before a non-space; a closer after a non-space and before
/// the end, a space, or punctuation not followed by a word character. Only
/// matched pairs go (`__init__.py`, `2**8` stay).
fn unemphasize(s: &str, d: [char; 2]) -> String {
    let c: Vec<char> = s.chars().collect();
    let n = c.len();
    let word = |k: Option<usize>| k.and_then(|k| c.get(k)).is_some_and(|ch| ch.is_alphanumeric());
    let at = |i: usize| i + 1 < n && c[i] == d[0] && c[i + 1] == d[1];
    let opens = |i: usize| {
        let before = match i.checked_sub(1).map(|k| c[k]) {
            None => true,
            Some(p) if p.is_whitespace() => true,
            Some(p) if p.is_ascii_punctuation() => !word(i.checked_sub(2)),
            _ => false,
        };
        before && c.get(i + 2).is_some_and(|x| !x.is_whitespace())
    };
    let closes = |i: usize| {
        let after = match c.get(i + 2) {
            None => true,
            Some(x) if x.is_whitespace() => true,
            Some(x) if x.is_ascii_punctuation() => !word(Some(i + 3)),
            _ => false,
        };
        after && i > 0 && !c[i - 1].is_whitespace()
    };
    let mut drop = vec![false; n];
    let mut i = 0;
    while i + 1 < n {
        if at(i) && opens(i) {
            if let Some(j) = (i + 3..n).find(|&j| at(j) && closes(j)) {
                for k in [i, i + 1, j, j + 1] {
                    drop[k] = true;
                }
                i = j + 2;
                continue;
            }
        }
        i += 1;
    }
    c.into_iter().zip(drop).filter(|(_, d)| !d).map(|(ch, _)| ch).collect()
}

/// A line without its leading markdown markers (`## `, `> `, `- `, ...).
fn strip_markers(line: &str) -> &str {
    let mut l = line.trim_start();
    loop {
        let hashes = l.trim_start_matches('#');
        let t = if hashes.len() != l.len() && (hashes.is_empty() || hashes.starts_with(' ')) { hashes } else { l };
        let t = t.strip_prefix('>').or_else(|| ["- ", "* ", "+ "].iter().find_map(|m| t.strip_prefix(m))).unwrap_or(t);
        let t = t.trim_start();
        if t.len() == l.len() {
            return l;
        }
        l = t;
    }
}

/// Markup tags ([`tag_len`]) → a space; autolinks → their URL; any other
/// `<` is text.
fn strip_tags(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(i) = rest.find('<') {
        out.push_str(&rest[..i]);
        let after = &rest[i + 1..];
        if let Some(n) = autolink_len(after) {
            out.push_str(&after[..n - 1]);
            rest = &after[n..];
        } else if let Some(n) = tag_len(after) {
            out.push(' ');
            rest = &after[n..];
        } else {
            out.push('<');
            rest = after;
        }
    }
    out.push_str(rest);
    out
}

/// `[text](url)` → `text`, for a non-empty text and a URL-ish target.
fn unlink(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(i) = rest.find('[') {
        out.push_str(&rest[..i]);
        let after = &rest[i + 1..];
        let link = after.find(']').and_then(|j| {
            let text = &after[..j];
            let tail = after[j + 1..].strip_prefix('(')?;
            let k = tail.find(')')?;
            (!text.is_empty() && !text.contains('[') && urlish(&tail[..k])).then(|| (text, &tail[k + 1..]))
        });
        match link {
            Some((text, tail)) => {
                out.push_str(text);
                rest = tail;
            }
            None => {
                out.push('[');
                rest = after;
            }
        }
    }
    out.push_str(rest);
    out
}

/// agent-roster.py `fit_label`: `proj/Title` into `width` cells → (proj, title).
/// The title wins: the project (with its `/`) stays only if both fit, or if it
/// fits WHOLE in a third of the width. No slash (or a leading one): all title.
pub fn fit_label(label: &str, width: usize) -> (String, String) {
    let label = sanitize(label);
    let (mut proj, title) = match label.split_once('/') {
        Some((p, t)) if !label.starts_with('/') => (format!("{p}/"), t.to_string()),
        _ => (String::new(), label.clone()),
    };
    let mut pw = dwidth(&proj);
    if pw + dwidth(&title) <= width {
        return (proj, title);
    }
    if pw > width / 3 {
        proj.clear();
        pw = 0;
    }
    let t = clip(&title, width.saturating_sub(pw));
    (proj, t)
}

/// agent-roster.py `ago`: `12s`, `5m`, `3h`, `2d`; "" for no stamp.
pub fn ago(t: Option<i64>, now: f64) -> String {
    let Some(t) = t else { return String::new() };
    let s = (now - t as f64).max(0.0) as i64;
    if s < 60 {
        format!("{s}s")
    } else if s < 3600 {
        format!("{}m", s / 60)
    } else if s < 86400 {
        format!("{}h", s / 3600)
    } else {
        format!("{}d", s / 86400)
    }
}

/// Seconds since the epoch, as a float (the Python `time.time()`).
pub fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

#[repr(C)]
struct Tm {
    tm_sec: i32,
    tm_min: i32,
    tm_hour: i32,
    tm_mday: i32,
    tm_mon: i32,
    tm_year: i32,
    tm_wday: i32,
    tm_yday: i32,
    tm_isdst: i32,
    tm_gmtoff: std::os::raw::c_long,
    tm_zone: *const std::os::raw::c_char,
}

extern "C" {
    fn localtime_r(t: *const i64, out: *mut Tm) -> *mut Tm;
}

/// `HH:MM` in local time for an epoch (the event log's time column).
pub fn clock_hm(epoch: i64) -> String {
    let mut tm = Tm {
        tm_sec: 0, tm_min: 0, tm_hour: 0, tm_mday: 0, tm_mon: 0, tm_year: 0, tm_wday: 0, tm_yday: 0,
        tm_isdst: 0, tm_gmtoff: 0, tm_zone: std::ptr::null(),
    };
    // SAFETY: localtime_r writes only into `tm`; time_t is i64 on 64-bit macOS/Linux.
    let ok = unsafe { !localtime_r(&epoch, &mut tm).is_null() };
    if ok {
        format!("{:02}:{:02}", tm.tm_hour, tm.tm_min)
    } else {
        String::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn widths() {
        assert_eq!(dwidth("abc"), 3);
        assert_eq!(dwidth("日本"), 4);
        assert_eq!(dwidth("❤️"), 2); // VS16: emoji presentation
        assert_eq!(dwidth("❤"), 1);
        assert_eq!(dwidth("🫠"), 2);
        assert_eq!(dwidth("e\u{301}"), 1); // combining mark
        assert_eq!(dwidth("a\u{200b}b"), 2); // zero-width space (Cf)
        assert_eq!(dwidth("👩\u{200d}💻"), 4); // ZWJ is zero, both emoji count
        assert_eq!(dwidth("✕◉✓◐○"), 5);
        assert_eq!(dwidth("\x1b"), 1);
    }

    #[test]
    fn clipping() {
        assert_eq!(clip("a❤️bcd", 3), "a…"); // never splits the VS16 off
        assert_eq!(clip("abc", 3), "abc");
        assert_eq!(clip("abcd", 3), "ab…");
        assert_eq!(clip("abcd", 0), "");
        assert_eq!(clip("abcd", 1), "…");
        assert_eq!(clip("日本語", 4), "日…");
        assert_eq!(clip("日本語", 5), "日本…");
        assert_eq!(clip("🫠🫠🫠", 4), "🫠…");
        for w in 0..12 {
            assert!(dwidth(&clip("🫠 日本 ❤️ x", w)) <= w);
        }
    }

    #[test]
    fn sanitizing() {
        assert_eq!(sanitize("a\tb\x1bc\u{9b}d"), "a b c d");
    }

    #[test]
    fn plain_text() {
        assert_eq!(plain("**Test 2 passed.**"), "Test 2 passed.");
        assert_eq!(plain("run `cargo test` and __then__ stop"), "run cargo test and then stop");
        assert_eq!(plain("## Summary\n> quoted\n- one\n* two\n+ three"), "Summary quoted one two three");
        assert_eq!(plain("see [the docs](https://x.y/z) now"), "see the docs now");
        assert_eq!(plain("<agent-message from=a>hello there</agent-message>"), "hello there");
        assert_eq!(plain("[message from @handy-a] ok"), "[message from @handy-a] ok"); // not a link
        assert_eq!(plain("a < b and c<5> d"), "a < b and c<5> d"); // not tags
        assert_eq!(plain("#1 issue, -5 degrees, x>y"), "#1 issue, -5 degrees, x>y");
        assert_eq!(plain("  lots \t of\n\n  space  "), "lots of space");
        assert_eq!(plain("a\x1bb"), "a b");
        assert_eq!(plain("<unclosed tag"), "<unclosed tag");
        assert_eq!(plain(""), "");
        // Code is not markup (the review's cases).
        assert_eq!(plain("Edit __init__.py"), "Edit __init__.py");
        assert_eq!(plain("Bash sort <in.txt >out.txt"), "Bash sort <in.txt >out.txt");
        assert_eq!(plain("fn f() -> Vec<String>"), "fn f() -> Vec<String>");
        assert_eq!(plain("a<b and c>d"), "a<b and c>d");
        assert_eq!(plain("2**8 = 256"), "2**8 = 256");
        assert_eq!(plain("<https://x.y/z>"), "https://x.y/z");
        assert_eq!(plain("arr[0](x)"), "arr[0](x)");
        assert_eq!(plain("see [a](b c)"), "see [a](b c)");
        // ...while real markup still goes.
        assert_eq!(plain("<b>bold</b> and <task-notification id='7' x=\"y z\">done</task-notification>"), "bold and done");
        assert_eq!(plain("<br/>x<my-tag/>y"), "x y");
        assert_eq!(plain("__really__ and **so**, (**this**)."), "really and so, (this).");
        assert_eq!(plain("[docs](./README.md) [top](#top) [m](mailto:a@b.c)"), "docs top m");
        assert_eq!(plain("snake_case and a_b_c"), "snake_case and a_b_c");
    }

    #[test]
    fn fit() {
        // agent-roster tests FitLabelTests
        assert_eq!(fit_label("proj/Title", 20), ("proj/".into(), "Title".into()));
        assert_eq!(fit_label("math_economics/A long title here", 20), ("".into(), "A long title here".into()));
        assert_eq!(fit_label("ab/A long title here", 12), ("ab/".into(), "A long t…".into()));
        assert_eq!(fit_label("/abs/path", 20), ("".into(), "/abs/path".into()));
        assert_eq!(fit_label("no slash", 4), ("".into(), "no …".into()));
    }

    #[test]
    fn agos() {
        assert_eq!(ago(None, 100.0), "");
        assert_eq!(ago(Some(100), 100.0), "0s");
        assert_eq!(ago(Some(200), 100.0), "0s"); // future stamp: clamped
        assert_eq!(ago(Some(41), 100.0), "59s");
        assert_eq!(ago(Some(40), 100.0), "1m");
        assert_eq!(ago(Some(0), 3599.0), "59m");
        assert_eq!(ago(Some(0), 3600.0), "1h");
        assert_eq!(ago(Some(0), 86399.0), "23h");
        assert_eq!(ago(Some(0), 86400.0 * 3.0), "3d");
    }

    #[test]
    fn clock() {
        let s = clock_hm(1_700_000_000);
        assert_eq!(s.len(), 5);
        assert_eq!(&s[2..3], ":");
    }
}
