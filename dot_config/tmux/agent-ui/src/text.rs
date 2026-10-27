//! Cell widths, clipping and small formatters, mirroring agent-roster.py's
//! `char_width` / `clusters` / `dwidth` / `clip` / `fit_label` / `ago` and its
//! `CONTROL` scrub.
//!
//! Widths are measured the way tmux 3.7c draws them, per cluster (one drawn
//! cell group, [`clusters`]): a code point is as wide as `unicode-width`
//! says, but for the few tmux draws otherwise ([`TMUX_WIDTH`]); a VS16 makes
//! the cluster before it two cells ("❤️"); zero-width marks ride on the
//! character before them; and tmux's combining rules join a ZWJ sequence
//! ("👩‍💻"), a skin-tone sequence ("👍🏽") and a flag ("🇺🇸") into one
//! two-cell cluster. A text-presentation pictograph with no VS16 ("🛠",
//! "🖥", "🌡") is one cell. Over-counting a cell only costs padding;
//! under-counting overflows the row. The sidebar, which WezTerm draws, has
//! its own profile: [`wez`].
//!
//! (2026-10-22: these used to be per code point, every pictographic block
//! two cells (agent-roster.py's `EMOJI_WIDE`), which over-counted 556 code
//! points and every ZWJ / skin-tone sequence, and under-counted ☝ ⛹ ✌ ✍.
//! The test tables are tmux 3.7c's own: `#{w:}` per code point, and the
//! cursor after printing each sequence into a pane.)

use unicode_width::UnicodeWidthChar;

/// Variation selector 16 (emoji presentation).
pub const VS16: char = '\u{FE0F}';
/// Zero-width joiner.
pub const ZWJ: char = '\u{200D}';

/// Code points tmux 3.7c draws at another width than `unicode-width` 0.2
/// gives them (the ones that matter: an under-count overflows the row):
/// the emoji modifier bases with no emoji presentation (two cells), the
/// soft hyphen (one cell, though a format character), and the halfwidth
/// (semi-)voiced sound marks and Hangul filler (one cell each).
pub const TMUX_WIDTH: [(u32, usize); 13] = [
    (0x00AD, 1), (0x261D, 2), (0x26F9, 2), (0x270C, 2), (0x270D, 2),
    (0x1F3CB, 2), (0x1F3CC, 2), (0x1F574, 2), (0x1F575, 2), (0x1F590, 2),
    (0xFF9E, 1), (0xFF9F, 1), (0xFFA0, 1),
];

/// The emoji a skin-tone modifier (U+1F3FB..U+1F3FF) joins in tmux 3.7c
/// (measured: its list, not Unicode's Emoji_Modifier_Base; ✌🏽 is two
/// clusters there). A cluster STARTING with one of these takes the modifier.
pub const SKIN_BASES: [(u32, u32); 22] = [
    (0x1F44B, 0x1F450), (0x1F466, 0x1F469), (0x1F46E, 0x1F46E), (0x1F470, 0x1F478), (0x1F47C, 0x1F47C),
    (0x1F481, 0x1F483), (0x1F485, 0x1F487), (0x1F4AA, 0x1F4AA), (0x1F575, 0x1F575), (0x1F57A, 0x1F57A),
    (0x1F590, 0x1F590), (0x1F595, 0x1F596), (0x1F645, 0x1F647), (0x1F64B, 0x1F64F), (0x1F6B4, 0x1F6B6),
    (0x1F926, 0x1F926), (0x1F937, 0x1F939), (0x1F93D, 0x1F93E), (0x1F9B5, 0x1F9B6), (0x1F9B8, 0x1F9B9),
    (0x1F9CD, 0x1F9CF), (0x1F9D1, 0x1F9DF),
];

/// tmux keeps at most this many bytes in one cell: a skin-tone modifier
/// that would not fit starts a cluster of its own.
const CELL_BYTES: usize = 32;

fn is_skin(c: char) -> bool {
    ('\u{1F3FB}'..='\u{1F3FF}').contains(&c)
}

fn is_flag_letter(c: char) -> bool {
    ('\u{1F1E6}'..='\u{1F1FF}').contains(&c)
}

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

/// agent-roster.py `char_width`: 0, 1 or 2 cells for one code point on its
/// own (as tmux 3.7c draws it: [`TMUX_WIDTH`]).
pub fn char_width(c: char) -> usize {
    let o = c as u32;
    if let Some(&(_, w)) = TMUX_WIDTH.iter().find(|&&(p, _)| p == o) {
        return w;
    }
    if o == 0x200D || (0xFE00..=0xFE0F).contains(&o) || is_format(o) {
        return 0;
    }
    match c.width() {
        Some(0) if !c.is_control() => 0, // combining marks (Mn/Me)
        Some(2) => 2,
        _ => 1,
    }
}

/// agent-roster.py `clusters`: `(text, cells)` per drawn cell group, as
/// tmux 3.7c groups them: a zero-width character (a mark, a variation
/// selector, a ZWJ, a tag) joins the cluster before it, a VS16 making it two
/// cells; after a ZWJ any non-ASCII character joins too (the cluster keeps
/// its width: "👩‍💻" is two cells); a skin-tone modifier joins a cluster
/// that starts with one of [`SKIN_BASES`] (while it fits in a cell); and a
/// second flag letter joins the first ("🇺🇸", two cells).
pub fn clusters(s: &str) -> Vec<(String, usize)> {
    if s.is_ascii() {
        return s.chars().map(|c| (c.to_string(), 1)).collect();
    }
    let mut out: Vec<(String, usize)> = Vec::new();
    for c in s.chars() {
        let w = char_width(c);
        if let Some((t, cw)) = out.last_mut() {
            let first = t.chars().next().map_or(0, |f| f as u32);
            let flag = is_flag_letter(c) && t.chars().count() == 1 && t.chars().all(is_flag_letter);
            let joins = w == 0
                || flag
                || (t.ends_with(ZWJ) && !c.is_ascii())
                || (is_skin(c) && SKIN_BASES.iter().any(|&(a, b)| (a..=b).contains(&first))
                    && t.len() + c.len_utf8() <= CELL_BYTES);
            if joins {
                t.push(c);
                if c == VS16 || flag {
                    *cw = 2;
                } else if *cw == 0 {
                    *cw = w; // a leading joiner: the character it joined is the width
                }
                continue;
            }
        }
        out.push((c.to_string(), w));
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
    clip_in(s, width, clusters)
}

/// [`clip`] over a width profile's `clusters` ([`wez::clip`] too).
fn clip_in(s: &str, width: usize, clusters: fn(&str) -> Vec<(String, usize)>) -> String {
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
    fit_label_in(label, width, dwidth, clip)
}

/// [`fit_label`] over a width profile's `dwidth` and `clip` ([`wez::fit_label`] too).
fn fit_label_in(label: &str, width: usize, dwidth: fn(&str) -> usize, clip: fn(&str, usize) -> String) -> (String, String) {
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

/// Widths as WezTerm draws them, for the sidebar: it runs in a WezTerm split
/// beside the tmux pane (wezterm.lua's CMD+B), so WezTerm, not tmux, lays out
/// its cells, and the two disagree (`✌` is two cells in tmux, one here; the
/// soft hyphen one there, none here). The tmux-drawn popups (the menu, the
/// sessions list) keep the tmux profile above.
///
/// WezTerm (no `unicode_version` in wezterm.lua, so its default 9) splits
/// text into extended grapheme clusters (UAX #29: a ZWJ joins only two
/// pictographs, a skin-tone modifier is an extension of whatever precedes
/// it) and draws each `min(2, Σ code point widths)` cells, a variation
/// selector counting nothing: so "❤️" and "🛠️" are one cell, "👩‍💻", "✌🏽",
/// "a🏽" and "🇺🇸" two, and "x‍💻" three (`x‍` then `💻`).
///
/// (2026-10-22: the sidebar used tmux 3.7c's profile, so a `✌` in a title
/// was laid out two cells wide and left a stale cell after it on screen, and
/// "x‍💻" was one cell, the rest of the row drawn two cells right. The test
/// tables are WezTerm's own: the cursor after printing each into a private
/// mux server's pane.)
pub mod wez {
    use super::is_format;
    use unicode_width::UnicodeWidthChar;

    /// Code points WezTerm sums at another width than `unicode-width` 0.2
    /// gives them (the rest agree: every assigned code point through
    /// U+2FFFF and the tags / variation selectors at U+E0000, each printed
    /// on its own into a private mux server's pane, 2026-10-22): mostly the
    /// spacing marks (Mc: `ा` in "का", none; one cell to `unicode-width`),
    /// and the halfwidth (semi-)voiced sound marks, one cell, not none.
    /// Sorted `(first, last, cells)` ranges.
    pub const WEZ_WIDTH: [(u32, u32, usize); 163] = [
        (0x903, 0x903, 0), (0x93B, 0x93B, 0), (0x93E, 0x940, 0), (0x949, 0x94C, 0), (0x94E, 0x94F, 0), (0x982, 0x983, 0),
        (0x9BF, 0x9C0, 0), (0x9C7, 0x9CC, 0), (0xA03, 0xA03, 0), (0xA3E, 0xA40, 0), (0xA83, 0xA83, 0), (0xABE, 0xAC0, 0),
        (0xAC9, 0xACC, 0), (0xB02, 0xB03, 0), (0xB40, 0xB40, 0), (0xB47, 0xB4C, 0), (0xBBF, 0xBBF, 0), (0xBC1, 0xBCC, 0),
        (0xC01, 0xC03, 0), (0xC41, 0xC44, 0), (0xC82, 0xC83, 0), (0xCBE, 0xCBE, 0), (0xCC1, 0xCC1, 0), (0xCC3, 0xCC4, 0),
        (0xCF3, 0xCF3, 0), (0xD02, 0xD03, 0), (0xD3F, 0xD40, 0), (0xD46, 0xD4C, 0), (0xD4E, 0xD4E, 1), (0xD82, 0xD83, 0),
        (0xDD0, 0xDD1, 0), (0xDD8, 0xDDE, 0), (0xDF2, 0xDF3, 0), (0xF3E, 0xF3F, 0), (0xF7F, 0xF7F, 0),
        (0x102B, 0x102C, 0), (0x1031, 0x1031, 0), (0x1038, 0x1038, 0), (0x103B, 0x103C, 0), (0x1056, 0x1057, 0),
        (0x1062, 0x1064, 0), (0x1067, 0x106D, 0), (0x1083, 0x1084, 0), (0x1087, 0x108C, 0), (0x108F, 0x108F, 0),
        (0x109A, 0x109C, 0), (0x17A4, 0x17A4, 1), (0x17B6, 0x17B6, 0), (0x17BE, 0x17C5, 0), (0x17C7, 0x17C8, 0),
        (0x1923, 0x1926, 0), (0x1929, 0x1931, 0), (0x1933, 0x1938, 0), (0x1A19, 0x1A1A, 0), (0x1A55, 0x1A55, 0),
        (0x1A57, 0x1A57, 0), (0x1A61, 0x1A61, 0), (0x1A63, 0x1A64, 0), (0x1A6D, 0x1A72, 0), (0x1B04, 0x1B04, 0),
        (0x1B3E, 0x1B41, 0), (0x1B82, 0x1B82, 0), (0x1BA1, 0x1BA1, 0), (0x1BA6, 0x1BA7, 0), (0x1BE7, 0x1BE7, 0),
        (0x1BEA, 0x1BEC, 0), (0x1BEE, 0x1BEE, 0), (0x1C24, 0x1C2B, 0), (0x1C34, 0x1C35, 0), (0x1CE1, 0x1CE1, 0),
        (0x1CF7, 0x1CF7, 0), (0x2D7F, 0x2D7F, 0), (0x3164, 0x3164, 2), (0xA823, 0xA824, 0), (0xA827, 0xA827, 0),
        (0xA880, 0xA881, 0), (0xA8B4, 0xA8C3, 0), (0xA8FA, 0xA8FA, 1), (0xA952, 0xA952, 0), (0xA983, 0xA983, 0),
        (0xA9B4, 0xA9B5, 0), (0xA9BA, 0xA9BB, 0), (0xA9BE, 0xA9BF, 0), (0xAA2F, 0xAA30, 0), (0xAA33, 0xAA34, 0),
        (0xAA4D, 0xAA4D, 0), (0xAA7B, 0xAA7B, 0), (0xAA7D, 0xAA7D, 0), (0xAAEB, 0xAAEB, 0), (0xAAEE, 0xAAEF, 0),
        (0xAAF5, 0xAAF5, 0), (0xABE3, 0xABE4, 0), (0xABE6, 0xABE7, 0), (0xABE9, 0xABEA, 0), (0xABEC, 0xABEC, 0),
        (0xFF9E, 0xFFA0, 1), (0x11000, 0x11000, 0), (0x11002, 0x11002, 0), (0x11082, 0x11082, 0), (0x110B0, 0x110B2, 0),
        (0x110B7, 0x110B8, 0), (0x1112C, 0x1112C, 0), (0x11145, 0x11146, 0), (0x11182, 0x11182, 0), (0x111B3, 0x111B5, 0),
        (0x111BF, 0x111BF, 0), (0x111C2, 0x111C3, 1), (0x111CE, 0x111CE, 0), (0x1122C, 0x1122E, 0), (0x11232, 0x11233, 0),
        (0x112E0, 0x112E2, 0), (0x11302, 0x11303, 0), (0x1133F, 0x1133F, 0), (0x11341, 0x1134C, 0), (0x11362, 0x11363, 0),
        (0x11435, 0x11437, 0), (0x11440, 0x11441, 0), (0x11445, 0x11445, 0), (0x114B1, 0x114B2, 0), (0x114B9, 0x114B9, 0),
        (0x114BB, 0x114BC, 0), (0x114BE, 0x114BE, 0), (0x114C1, 0x114C1, 0), (0x115B0, 0x115B1, 0), (0x115B8, 0x115BB, 0),
        (0x115BE, 0x115BE, 0), (0x11630, 0x11632, 0), (0x1163B, 0x1163C, 0), (0x1163E, 0x1163E, 0), (0x116AC, 0x116AC, 0),
        (0x116AE, 0x116AF, 0), (0x1171E, 0x1171E, 0), (0x11720, 0x11721, 0), (0x11726, 0x11726, 0), (0x1182C, 0x1182E, 0),
        (0x11838, 0x11838, 0), (0x11931, 0x11938, 0), (0x1193F, 0x1193F, 1), (0x11940, 0x11940, 0), (0x11941, 0x11941, 1),
        (0x11942, 0x11942, 0), (0x119D1, 0x119D3, 0), (0x119DC, 0x119DF, 0), (0x119E4, 0x119E4, 0), (0x11A39, 0x11A39, 0),
        (0x11A57, 0x11A58, 0), (0x11A84, 0x11A89, 1), (0x11A97, 0x11A97, 0), (0x11C2F, 0x11C2F, 0), (0x11C3E, 0x11C3E, 0),
        (0x11CA9, 0x11CA9, 0), (0x11CB1, 0x11CB1, 0), (0x11CB4, 0x11CB4, 0), (0x11D46, 0x11D46, 1), (0x11D8A, 0x11D8E, 0),
        (0x11D93, 0x11D94, 0), (0x11D96, 0x11D96, 0), (0x11EF5, 0x11EF6, 0), (0x11F02, 0x11F02, 1), (0x11F03, 0x11F03, 0),
        (0x11F34, 0x11F35, 0), (0x11F3E, 0x11F3F, 0), (0x16F51, 0x16F87, 0),
    ];

    /// One code point inside a cluster, as WezTerm sums it ([`WEZ_WIDTH`]
    /// first): zero for a joiner, a variation selector, a format character
    /// (the soft hyphen too) or a mark.
    pub fn char_width(c: char) -> usize {
        let o = c as u32;
        if o >= 0x903 {
            let i = WEZ_WIDTH.partition_point(|&(_, last, _)| last < o);
            if let Some(&(_, _, w)) = WEZ_WIDTH.get(i).filter(|&&(first, _, _)| first <= o) {
                return w;
            }
        }
        if o == 0x200D || (0xFE00..=0xFE0F).contains(&o) || is_format(o) {
            return 0;
        }
        match c.width() {
            Some(0) if !c.is_control() => 0,
            Some(2) => 2,
            _ => 1,
        }
    }

    /// `(text, cells)` per extended grapheme cluster, as WezTerm draws it. A
    /// control character is a one-cell cluster of its own (the sidebar draws
    /// it as a space).
    pub fn clusters(s: &str) -> Vec<(String, usize)> {
        if s.is_ascii() {
            return s.chars().map(|c| (c.to_string(), 1)).collect();
        }
        let mut out = Vec::new();
        for run in s.split_inclusive(char::is_control) {
            let (text, ctl) = match run.char_indices().last() {
                Some((i, c)) if c.is_control() => (&run[..i], Some(c)),
                _ => (run, None),
            };
            // ratatui's segmentation (unicode-segmentation): no control in `text`.
            let span = ratatui::text::Span::raw(text);
            for g in span.styled_graphemes(ratatui::style::Style::default()) {
                out.push((g.symbol.to_string(), g.symbol.chars().map(char_width).sum::<usize>().min(2)));
            }
            if let Some(c) = ctl {
                out.push((c.to_string(), 1));
            }
        }
        out
    }

    /// Display width in WezTerm cells.
    pub fn dwidth(s: &str) -> usize {
        if s.is_ascii() {
            s.len()
        } else {
            clusters(s).iter().map(|(_, w)| w).sum()
        }
    }

    /// [`super::clip`] in WezTerm cells.
    pub fn clip(s: &str, width: usize) -> String {
        super::clip_in(s, width, clusters)
    }

    /// [`super::fit_label`] in WezTerm cells.
    pub fn fit_label(label: &str, width: usize) -> (String, String) {
        super::fit_label_in(label, width, dwidth, clip)
    }
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

/// An epoch broken down in local time, as much as a reset time needs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Local {
    /// Days since the epoch in LOCAL time (the UTC offset applied): two
    /// moments on the same local day have the same number.
    pub day: i64,
    /// 0 = Sunday.
    pub wday: u8,
    /// 0 = January.
    pub mon: u8,
    pub mday: u8,
    pub hour: u8,
    pub min: u8,
}

/// `epoch` in local time (libc `localtime_r`, so `TZ` applies); None if the
/// C library refused it.
pub fn local(epoch: i64) -> Option<Local> {
    let mut tm = Tm {
        tm_sec: 0, tm_min: 0, tm_hour: 0, tm_mday: 0, tm_mon: 0, tm_year: 0, tm_wday: 0, tm_yday: 0,
        tm_isdst: 0, tm_gmtoff: 0, tm_zone: std::ptr::null(),
    };
    // SAFETY: localtime_r writes only into `tm`; time_t is i64 on 64-bit macOS/Linux.
    let ok = unsafe { !localtime_r(&epoch, &mut tm).is_null() };
    ok.then(|| Local {
        day: (epoch + tm.tm_gmtoff as i64).div_euclid(86_400),
        wday: tm.tm_wday as u8,
        mon: tm.tm_mon as u8,
        mday: tm.tm_mday as u8,
        hour: tm.tm_hour as u8,
        min: tm.tm_min as u8,
    })
}

const WDAY: [&str; 7] = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MON: [&str; 12] = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/// A moment as a person says it, from `now`'s day ([`Local`] both): `1:00
/// AM` today, `Tue 1:00 AM` within the next six days, `Nov 2 1:00 AM`
/// further (or in the past: a stale reset still names its day).
pub fn when_words(at: Local, now: Local) -> String {
    let h12 = match at.hour % 12 {
        0 => 12,
        h => h,
    };
    let clock = format!("{h12}:{:02} {}", at.min, if at.hour < 12 { "AM" } else { "PM" });
    match at.day - now.day {
        0 => clock,
        1..=6 => format!("{} {clock}", WDAY[usize::from(at.wday % 7)]),
        _ => format!("{} {} {clock}", MON[usize::from(at.mon % 12)], at.mday),
    }
}

/// [`when_words`] for two epochs in this process's local time ("" if the
/// C library refused either).
pub fn when(at: i64, now: f64) -> String {
    match (local(at), local(now.floor() as i64)) {
        (Some(a), Some(n)) => when_words(a, n),
        _ => String::new(),
    }
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
        assert_eq!(dwidth("👩\u{200d}💻"), 2); // one ZWJ sequence: one wide cell
        assert_eq!(dwidth("✕◉✓◐○"), 5);
        assert_eq!(dwidth("\x1b"), 1);
    }

    /// One code point on its own, as tmux 3.7c measures it (`#{w:}` on a
    /// private server, 2026-10-22): the modifier bases it draws wide though
    /// `unicode-width` says one cell, the text-presentation pictographs one
    /// cell, the soft hyphen and halfwidth marks one cell.
    #[test]
    fn code_point_widths_are_tmuxs() {
        let tmux: [(u32, usize); 22] = [
            (0x261D, 2), (0x26F9, 2), (0x270C, 2), (0x270D, 2), (0x1F3CB, 2), (0x1F3CC, 2), (0x1F574, 2),
            (0x1F575, 2), (0x1F590, 2), (0x1F6E0, 1), (0x1F5A5, 1), (0x1F321, 1), (0x1F3F3, 1), (0x00AD, 1),
            (0xFF9E, 1), (0xFFA0, 1), (0x1F600, 2), (0x2764, 1), (0x2605, 1), (0x65E5, 2), (0x1FAE0, 2),
            (0x1F3FD, 2),
        ];
        for (cp, w) in tmux {
            let c = char::from_u32(cp).unwrap();
            assert_eq!((cp, char_width(c), dwidth(&c.to_string())), (cp, w, w));
        }
    }

    /// Sequences as tmux 3.7c draws them: the cursor column after printing
    /// each into a private pane (2026-10-22).
    #[test]
    fn sequence_widths_are_tmuxs() {
        let tmux: [(&str, usize); 46] = [
            ("\u{2764}\u{fe0f}", 2), ("\u{1f6e0}", 1), ("\u{1f6e0}\u{fe0f}", 2), ("\u{1f6e0}x", 2),
            ("\u{1f469}\u{200d}\u{1f4bb}", 2), ("\u{1f469}\u{200d}\u{1f4bb}x", 3), ("\u{1f9d1}\u{1f3fd}\u{200d}\u{1f4bb}", 2),
            ("\u{1f44d}\u{1f3fd}", 2), ("\u{1f3f3}\u{fe0f}\u{200d}\u{1f308}", 2), ("\u{1f3f3}\u{200d}\u{1f308}", 1),
            ("\u{1f468}\u{200d}\u{1f469}\u{200d}\u{1f467}\u{200d}\u{1f466}", 2), ("\u{1f9d1}\u{200d}\u{1f9d1}\u{200d}\u{1f9d2}", 2),
            ("\u{1f3cb}\u{fe0f}\u{200d}\u{2640}\u{fe0f}", 2), ("\u{1f1fa}\u{1f1f8}", 2), ("\u{1f1fa}", 1),
            ("\u{1f1fa}\u{1f1f8}\u{1f1ec}", 3), ("\u{1f1fa}\u{1f1f8}\u{1f1ec}\u{1f1e7}", 4), ("1\u{fe0f}\u{20e3}", 2),
            ("#\u{fe0f}\u{20e3}", 2), ("1\u{20e3}", 1), ("\u{1f600}\u{fe0e}", 2), ("a\u{fe0f}", 2), ("\u{2605}\u{fe0f}x", 3),
            ("\u{1f3f4}\u{e0067}\u{e0062}\u{e0065}\u{e006e}\u{e0067}\u{e007f}", 2), ("a\u{1f3fd}", 3),
            ("\u{270c}\u{1f3fd}", 4), ("\u{261d}\u{1f3fd}", 4), ("\u{1f3cb}\u{1f3fd}", 4), ("\u{1f590}\u{1f3fd}", 2),
            ("\u{1f600}\u{1f3fd}", 4), ("\u{1f4bb}\u{200d}\u{1f469}\u{1f3fd}", 4), ("\u{1f469}\u{200d}\u{1f600}\u{1f3fd}", 2),
            ("\u{1f44d}\u{1f3fd}\u{1f3fd}", 2), ("\u{1f44d}\u{1f3fd}\u{1f3fd}\u{1f3fd}\u{1f3fd}\u{1f3fd}\u{1f3fd}\u{1f3fd}\u{1f3fd}", 4),
            ("\u{1f469}\u{1f3fd}\u{200d}\u{2764}\u{fe0f}\u{200d}\u{1f48b}\u{200d}\u{1f468}", 2),
            ("\u{1f469}\u{1f3fd}\u{200d}\u{2764}\u{fe0f}\u{200d}\u{1f48b}\u{200d}\u{1f468}\u{1f3fb}", 4),
            ("a\u{200d}b", 2), ("a\u{200d}\u{2605}", 1), ("x\u{200d}\u{1f4bb}", 1), ("\u{2605}\u{200d}\u{1f469}", 1),
            ("\u{200d}\u{1f4bb}", 2), ("\u{1f4bb}\u{200d}", 2), ("x\u{200b}x", 2), ("e\u{301}\u{301}", 1),
            ("\u{1112}\u{1161}\u{11ab}\u{1100}\u{116e}\u{11a8}\u{110b}\u{1165}", 6), ("a\u{2764}\u{fe0f}c", 4),
        ];
        for (s, w) in tmux {
            assert_eq!((s, dwidth(s)), (s, w));
        }
    }

    /// Code points WezTerm draws otherwise than tmux (or `unicode-width`):
    /// the cursor after printing each into a private mux server's pane.
    #[test]
    fn code_point_widths_are_wezterms() {
        let wez: [(u32, usize); 16] = [
            (0x270C, 1), (0x261D, 1), (0x270D, 1), (0x26F9, 1), (0x1F3CB, 1), (0x1F575, 1), (0x1F590, 1),
            (0x00AD, 0), (0x093E, 0), (0xFF9E, 1), (0xFFA0, 1), (0x3164, 2), (0x1F600, 2), (0x2764, 1),
            (0x65E5, 2), (0x1F3FD, 2),
        ];
        for (cp, w) in wez {
            let c = char::from_u32(cp).unwrap();
            assert_eq!((cp, wez::dwidth(&c.to_string())), (cp, w));
        }
    }

    /// Sequences as WezTerm (unicode_version 9, the default) draws them: the
    /// cursor column after printing each into a private mux server's pane
    /// (2026-10-22). Where tmux differs, its count is in the comment.
    #[test]
    fn sequence_widths_are_wezterms() {
        let wez: [(&str, usize); 82] = [
            ("\u{2764}\u{fe0f}", 1), ("\u{2764}", 1), ("\u{1f6e0}", 1), ("\u{1f6e0}\u{fe0f}", 1), ("\u{1f6e0}x", 2),
            ("\u{270c}", 1), /* tmux 2 */ ("\u{270c}\u{1f3fd}", 2), /* tmux 4 */ ("\u{261d}\u{1f3fd}", 2),
            ("\u{1f600}\u{1f3fd}", 2), ("a\u{1f3fd}", 2), ("\u{ad}", 0), /* tmux 1 */ ("a\u{ad}b", 2),
            ("x\u{200d}\u{1f4bb}", 3), /* tmux 1 */ ("\u{1f3f3}\u{200d}\u{1f308}", 2),
            ("\u{e9}\u{200d}\u{1f4bb}", 3), ("a\u{200d}\u{65e5}", 3), ("\u{1f469}\u{200d}\u{1f4bb}", 2),
            ("\u{1f469}\u{200d}\u{1f4bb}x", 3), ("\u{1f9d1}\u{1f3fd}\u{200d}\u{1f4bb}", 2), ("\u{1f44d}\u{1f3fd}", 2),
            ("\u{1f3f3}\u{fe0f}\u{200d}\u{1f308}", 2), ("\u{1f468}\u{200d}\u{1f469}\u{200d}\u{1f467}\u{200d}\u{1f466}", 2),
            ("\u{1f3cb}\u{fe0f}\u{200d}\u{2640}\u{fe0f}", 2), ("\u{1f1fa}\u{1f1f8}", 2), ("\u{1f1fa}", 1),
            ("\u{1f1fa}\u{1f1f8}\u{1f1ec}", 3), ("\u{1f1fa}\u{1f1f8}\u{1f1ec}\u{1f1e7}", 4), ("1\u{fe0f}\u{20e3}", 1),
            ("#\u{fe0f}\u{20e3}", 1), ("1\u{20e3}", 1), ("\u{1f600}\u{fe0e}", 2), ("a\u{fe0f}", 1), ("\u{2605}\u{fe0f}x", 2),
            ("\u{1f3f4}\u{e0067}\u{e0062}\u{e0065}\u{e006e}\u{e0067}\u{e007f}", 2), ("\u{1f44d}\u{1f3fd}\u{1f3fd}", 2),
            ("a\u{200d}b", 2), ("a\u{200d}\u{2605}", 2), ("\u{200d}\u{1f4bb}", 2), ("\u{1f4bb}\u{200d}", 2),
            ("x\u{200b}x", 2), ("e\u{301}\u{301}", 1), ("\u{1112}\u{1161}\u{11ab}\u{1100}\u{116e}\u{11a8}\u{110b}\u{1165}", 6),
            ("a\u{2764}\u{fe0f}c", 3), ("\u{915}\u{93e}", 1), ("\u{915}\u{94d}\u{937}", 2), ("\u{e01}\u{e33}", 2),
            ("\u{ff76}\u{ff9e}", 2), ("\u{ff8a}\u{ff9f}", 2), ("\u{1100}\u{1100}", 2), ("\u{ac00}\u{11a8}", 2),
            ("\u{600}a", 1), ("\u{1f469}\u{200d}\u{2764}\u{fe0f}\u{200d}\u{1f468}", 2), ("\u{270c}\u{fe0f}", 1),
            ("\u{261d}\u{fe0f}", 1), ("\u{2601}\u{fe0f}", 1), ("\u{a9}\u{fe0f}", 1), ("\u{2122}\u{fe0f}", 1),
            ("\u{1f170}\u{fe0f}", 1), ("\u{1f5a5}\u{fe0f}", 1), ("\u{1f321}\u{fe0f}", 1), ("\u{65e5}\u{301}", 2),
            ("\u{65e5}\u{1f3fd}", 2), ("\u{1f3fd}", 2), ("\u{1f3fd}\u{1f3fd}", 2), ("\u{2b50}\u{1f3fd}", 2),
            ("\u{1f469}\u{1f3fd}\u{200d}\u{2764}\u{fe0f}\u{200d}\u{1f48b}\u{200d}\u{1f468}\u{1f3fb}", 2),
            ("\u{1f4bb}\u{200d}\u{1f469}\u{1f3fd}", 2), ("a\u{301}\u{200d}\u{1f4bb}", 3), ("\u{1f600}\u{301}", 2),
            ("\u{1f600}\u{200d}", 2), ("\u{200d}", 0), ("\u{fe0f}", 0), ("\u{301}", 0), ("\u{301}a", 1),
            ("\u{2028}x", 2), ("\u{ad}x", 1), ("a\u{20e3}", 1), ("\u{2764}\u{fe0f}\u{200d}\u{1f525}", 2),
            ("\u{1f9d1}\u{200d}\u{1f9d1}\u{200d}\u{1f9d2}", 2), ("\u{26f9}\u{1f3fd}", 2), ("\u{1f574}\u{1f3fd}", 2),
            ("\u{1f590}\u{1f3fd}", 2),
        ];
        let bad: Vec<_> = wez.iter().filter(|&&(s, w)| wez::dwidth(s) != w).map(|&(s, w)| (s, w, wez::dwidth(s))).collect();
        assert!(bad.is_empty(), "{bad:?}");
        // The one known miss, an over-count (padding, never an overflow):
        // WezTerm joins "★‍👩" (two cells), ratatui's unicode-segmentation
        // does not count ★ as a pictograph, so `★‍` then `👩`.
        assert_eq!(wez::dwidth("\u{2605}\u{200d}\u{1f469}"), 3);
        // Clusters keep their text: nothing dropped, nothing split mid-sequence.
        for (s, _) in wez {
            assert_eq!(wez::clusters(s).into_iter().map(|(t, _)| t).collect::<String>(), s);
        }
        assert_eq!(wez::clusters("x\u{200d}\u{1f4bb}"), [("x\u{200d}".into(), 1), ("\u{1f4bb}".into(), 2)]);
        assert_eq!(wez::clusters("a\tb\u{2764}\u{fe0f}"), [("a".into(), 1), ("\t".into(), 1), ("b".into(), 1), ("\u{2764}\u{fe0f}".into(), 1)]);
        assert_eq!(wez::clip("\u{270c} Peace", 4), "\u{270c} P…");
        assert_eq!(wez::fit_label("p/x\u{200d}\u{1f4bb}yz", 5), ("".into(), "x\u{200d}\u{1f4bb}yz".into()));
        assert_eq!(wez::fit_label("p/x\u{200d}\u{1f4bb}yz", 4), ("".into(), "x\u{200d}\u{1f4bb}…".into()));
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

    /// A reset time as the cards and the banner say it: 12-hour clock, the
    /// weekday when not today (up to six days ahead), the date further.
    #[test]
    fn when_words_say_the_day() {
        let at = |day: i64, wday: u8, hour: u8, min: u8| Local { day, wday, mon: 9, mday: 27, hour, min };
        let now = at(100, 1, 15, 0); // Mon
        assert_eq!(when_words(at(100, 1, 1, 0), now), "1:00 AM");
        assert_eq!(when_words(at(100, 1, 0, 5), now), "12:05 AM");
        assert_eq!(when_words(at(100, 1, 12, 0), now), "12:00 PM");
        assert_eq!(when_words(at(100, 1, 23, 59), now), "11:59 PM");
        assert_eq!(when_words(at(101, 2, 1, 0), now), "Tue 1:00 AM");
        assert_eq!(when_words(at(106, 0, 13, 30), now), "Sun 1:30 PM");
        assert_eq!(when_words(at(107, 1, 1, 0), now), "Oct 27 1:00 AM"); // a week out: the date
        assert_eq!(when_words(at(99, 0, 1, 0), now), "Oct 27 1:00 AM"); // past: the date too
        // The real clock: today's moment has no weekday, tomorrow's has one.
        let t = 1_800_000_000_i64;
        let n = local(t).unwrap();
        assert_eq!(n.day, local(t + 1).unwrap().day);
        assert!(!WDAY.iter().any(|d| when(t, t as f64).starts_with(d)) && when(t, t as f64).ends_with('M'));
        assert!(WDAY.iter().any(|d| when(t + 86_400, t as f64).starts_with(d)));
    }
}
