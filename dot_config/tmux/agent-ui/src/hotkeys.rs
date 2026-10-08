//! Number-key labels (agent-roster.py `hotkey_labels` / `number_items`).
//!
//! Rows 1-9 are always the single keys 1-9, however long the list: the top
//! (NEEDS YOU, then the current session) is what gets pressed, and it must
//! never wait on a timeout. Rows 10.. are `0` plus a FIXED-width number
//! ("01".."09", or "001".."0NN" past 18 rows), so the set is prefix-free:
//! every sequence is complete the moment its last digit lands. A bare `0` is
//! NEVER a label: when the list shrinks under a half-typed number (11 → 10
//! rows) a stale `01` stays inside the 0-namespace instead of acting on `0`
//! and letting the `1` fall through into the agent pane just focused (a
//! permission menu, where 1 = Yes).
//!
//! How a UI must consume them (Roster.digit): resolve a number against the
//! labels of the frame ON SCREEN when its first digit was pressed, never a
//! list rebuilt since; a complete label acts at once; an incomplete one waits
//! (no timeout); one that matches nothing swallows further digits until a
//! non-digit key. Inside a text filter, digits type.

use std::collections::HashMap;

/// The labels for `n` numbered rows, in display order.
pub fn hotkey_labels(n: usize) -> Vec<String> {
    if n <= 9 {
        return (1..=n).map(|i| i.to_string()).collect();
    }
    let w = (n - 9).to_string().len();
    (1..=9).map(|i| i.to_string()).chain((1..=n - 9).map(|k| format!("0{k:0w$}"))).collect()
}

/// number_items: {position: label} for every row `is_window` accepts, in
/// order. A window listed twice gets two numbers; headers get none.
pub fn number_items<T>(items: &[T], is_window: impl Fn(&T) -> bool) -> HashMap<usize, String> {
    let pos: Vec<usize> = items.iter().enumerate().filter(|(_, it)| is_window(it)).map(|(i, _)| i).collect();
    pos.iter().copied().zip(hotkey_labels(pos.len())).collect()
}

/// What a typed digit sequence means against a frame's labels.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DigitMatch {
    /// A complete label: act on this row position.
    Hit(usize),
    /// A prefix of some label: wait for the next digit.
    Partial,
    /// Matches nothing: swallow digits until a non-digit key.
    Dead,
}

/// Resolve `typed` against `labels` ({position: label}, as drawn).
pub fn match_digits(labels: &HashMap<usize, String>, typed: &str) -> DigitMatch {
    if let Some((&p, _)) = labels.iter().find(|(_, l)| l.as_str() == typed) {
        return DigitMatch::Hit(p);
    }
    if labels.values().any(|l| l.starts_with(typed)) {
        DigitMatch::Partial
    } else {
        DigitMatch::Dead
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn labels_unique_and_prefix_free() {
        for n in 0..250 {
            let ls = hotkey_labels(n);
            assert_eq!(ls.len(), n);
            let set: std::collections::HashSet<_> = ls.iter().collect();
            assert_eq!(set.len(), n, "unique at {n}");
            for a in &ls {
                assert_ne!(a, "0");
                for b in &ls {
                    assert!(a == b || !b.starts_with(a.as_str()), "{a} prefixes {b} at n={n}");
                }
            }
            // Fixed width past 9.
            if n > 9 {
                let w = ls[9].len();
                assert!(ls[9..].iter().all(|l| l.len() == w && l.starts_with('0')));
            }
        }
        assert_eq!(hotkey_labels(3), ["1", "2", "3"]);
        assert_eq!(hotkey_labels(11)[9..], ["01", "02"]);
        assert_eq!(hotkey_labels(18)[17], "09");
        assert_eq!(hotkey_labels(19)[9..11], ["001", "002"]);
        assert_eq!(hotkey_labels(19)[18], "010");
    }

    #[test]
    fn numbering_and_matching() {
        let items = ["hdr", "w", "w", "hdr", "w"];
        let m = number_items(&items, |s| *s == "w");
        assert_eq!(m.len(), 3);
        assert_eq!((m[&1].as_str(), m[&2].as_str(), m[&4].as_str()), ("1", "2", "3"));
        assert_eq!(match_digits(&m, "2"), DigitMatch::Hit(2));
        assert_eq!(match_digits(&m, "7"), DigitMatch::Dead);
        let many: Vec<&str> = vec!["w"; 12];
        let m = number_items(&many, |_| true);
        assert_eq!(match_digits(&m, "0"), DigitMatch::Partial);
        assert_eq!(match_digits(&m, "03"), DigitMatch::Hit(11));
        assert_eq!(match_digits(&m, "05"), DigitMatch::Dead);
    }
}
