//! Number-key labels: each agent's STICKY slot (`@agent_slot`).
//!
//! The watcher gives every agent window the lowest free number when it
//! appears and frees it only when the agent goes (exits, closes, is parked);
//! the others keep theirs. So `3` means the same agent for as long as it
//! lives, in the menu and in the sidebar, whatever moves around it.
//!
//! Labels are FIXED-width: zero-padded to the digit count of the highest slot
//! in use ([`slot_width`]). With slots up to 9 they are `1`..`9`; once any
//! slot is 10 or more, every label is two digits (`03`, `12`). Equal widths
//! make the set prefix-free, so a sequence is complete the moment its last
//! digit lands and never waits on a timeout. A bare `0` is NEVER a label
//! (slots start at 1): with two-digit labels it is a prefix that waits, and
//! a stale `0` typed as the width shrinks stays a dead sequence instead of
//! letting a following digit fall through to the agent pane just focused (a
//! permission menu, where 1 = Yes).
//!
//! How a UI must consume them (Roster.digit): resolve a number against the
//! labels of the frame ON SCREEN when its first digit was pressed, never a
//! list rebuilt since; a complete label acts at once; an incomplete one waits
//! (no timeout); one that matches nothing swallows further digits until a
//! non-digit key. Inside a text filter, digits type.

/// The label width for the highest slot in use: its digit count, 0 when no
/// agent has a slot.
pub fn slot_width(max_slot: Option<u32>) -> usize {
    max_slot.map_or(0, |n| n.to_string().len())
}

/// A slot as drawn and typed: zero-padded to `width` digits.
pub fn slot_label(slot: u32, width: usize) -> String {
    format!("{slot:0width$}")
}

/// What a typed digit sequence means against a frame's labels.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DigitMatch {
    /// A complete label: act on it.
    Hit,
    /// A prefix of some label: wait for the next digit.
    Partial,
    /// Matches nothing: swallow digits until a non-digit key.
    Dead,
}

/// Resolve `typed` against `labels` (as drawn).
pub fn match_digits<'a>(labels: impl IntoIterator<Item = &'a str>, typed: &str) -> DigitMatch {
    let mut partial = false;
    for l in labels {
        if l == typed {
            return DigitMatch::Hit;
        }
        partial |= l.starts_with(typed);
    }
    if partial {
        DigitMatch::Partial
    } else {
        DigitMatch::Dead
    }
}

/// The key bar's jump hint for a frame's labels: `3`, `1–7`, `01–12`.
pub fn jump_hint<'a>(labels: impl IntoIterator<Item = &'a str>) -> Option<String> {
    let mut v: Vec<&str> = labels.into_iter().collect();
    v.sort_unstable(); // fixed width: string order is numeric order
    v.dedup();
    match v.as_slice() {
        [] => None,
        [one] => Some(one.to_string()),
        [first, .., last] => Some(format!("{first}–{last}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn widths_and_labels() {
        assert_eq!(slot_width(None), 0);
        assert_eq!(slot_width(Some(1)), 1);
        assert_eq!(slot_width(Some(9)), 1);
        assert_eq!(slot_width(Some(10)), 2);
        assert_eq!(slot_width(Some(99)), 2);
        assert_eq!(slot_width(Some(100)), 3);
        assert_eq!(slot_label(3, 1), "3");
        assert_eq!(slot_label(3, 2), "03");
        assert_eq!(slot_label(12, 2), "12");
        assert_eq!(slot_label(7, 3), "007");
        // Any set of slots at their width is prefix-free and has no bare 0.
        for max in 1..=120u32 {
            let w = slot_width(Some(max));
            let ls: Vec<String> = (1..=max).map(|s| slot_label(s, w)).collect();
            for a in &ls {
                assert_ne!(a, "0");
                assert_eq!(a.len(), w);
                for b in &ls {
                    assert!(a == b || !b.starts_with(a.as_str()), "{a} prefixes {b} at max={max}");
                }
            }
        }
    }

    #[test]
    fn matching() {
        let one = ["1", "2", "4"];
        assert_eq!(match_digits(one, "2"), DigitMatch::Hit);
        assert_eq!(match_digits(one, "3"), DigitMatch::Dead); // a gap
        assert_eq!(match_digits(one, "0"), DigitMatch::Dead);
        let two = ["01", "04", "12"];
        assert_eq!(match_digits(two, "0"), DigitMatch::Partial);
        assert_eq!(match_digits(two, "1"), DigitMatch::Partial);
        assert_eq!(match_digits(two, "04"), DigitMatch::Hit);
        assert_eq!(match_digits(two, "12"), DigitMatch::Hit);
        assert_eq!(match_digits(two, "03"), DigitMatch::Dead);
        assert_eq!(match_digits(two, "4"), DigitMatch::Dead); // single digits are not labels at width 2
        assert_eq!(match_digits([], "1"), DigitMatch::Dead);
    }

    #[test]
    fn hints() {
        assert_eq!(jump_hint([]), None);
        assert_eq!(jump_hint(["3"]), Some("3".into()));
        assert_eq!(jump_hint(["4", "1", "2"]), Some("1–4".into()));
        assert_eq!(jump_hint(["12", "01", "04", "01"]), Some("01–12".into()));
    }
}
