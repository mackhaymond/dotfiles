//! Raw input bytes → the picker's keys.
//!
//! The same rules as the menu's reader ([`crate::menu::keys`]): a read can
//! end inside an escape sequence, so a trailing `ESC`, `ESC [` or `ESC O` is
//! carried until the caller has waited [`ESC_WAIT`] with nothing more; only
//! then is a lone ESC the Esc key. Unknown sequences are swallowed whole and
//! Alt+key is ignored. UTF-8 split across reads is reassembled.
//!
//! The picker's own keys differ from the menu's in the control characters:
//! `C-j` (LF) and `C-k` move, `C-w` deletes a word, and `CR` is Enter. The
//! one exception is [`KeyReader::feed_cooked`]: bytes the tty took in while
//! it was still in cooked mode went through ICRNL, so an Enter typed before
//! raw mode arrives as LF and is read as Enter there (a C-j typed that early
//! is indistinguishable from it, and is Enter too).

pub use crate::menu::keys::ESC_WAIT;

/// A key, as the picker acts on it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Key {
    /// A printable character (space included).
    Char(char),
    /// ↑, C-p, C-k.
    Up,
    /// ↓, C-n, C-j.
    Down,
    /// CR.
    Enter,
    /// DEL or BS.
    Backspace,
    Esc,
    CtrlC,
    /// Clear the query.
    CtrlU,
    /// Delete the word before the cursor (to the previous blank).
    CtrlW,
    /// Any other control character or a swallowed sequence's stand-in: ignored.
    Other,
}

fn keychr(c: char) -> Key {
    match c {
        '\r' => Key::Enter,
        '\n' | '\x0e' => Key::Down, // C-j, C-n
        '\x0b' | '\x10' => Key::Up, // C-k, C-p
        '\x7f' | '\x08' => Key::Backspace,
        '\x03' => Key::CtrlC,
        '\x15' => Key::CtrlU,
        '\x17' => Key::CtrlW,
        c if (c as u32) < 0x20 || (0x80..=0x9f).contains(&(c as u32)) => Key::Other,
        c => Key::Char(c),
    }
}

/// Decoded input → (keys, the carried tail of an unfinished sequence).
/// `fin`: the caller already waited ESC_WAIT and nothing followed.
pub fn parse_keys(buf: &str, fin: bool) -> (Vec<Key>, String) {
    let b: Vec<char> = buf.chars().collect();
    let n = b.len();
    let mut keys = Vec::new();
    let mut i = 0;
    let rest = |i: usize| -> String { if fin { String::new() } else { b[i..].iter().collect() } };
    while i < n {
        let c = b[i];
        if c != '\x1b' {
            keys.push(keychr(c));
            i += 1;
            continue;
        }
        match b.get(i + 1) {
            None => {
                // A trailing ESC: a press only once the wait says so.
                if fin {
                    keys.push(Key::Esc);
                    i += 1;
                    continue;
                }
                return (keys, rest(i));
            }
            Some('[') => {
                // CSI: params 0x30-0x3f, intermediates 0x20-0x2f, final 0x40-0x7e.
                let mut j = i + 2;
                while j < n && ('\x30'..='\x3f').contains(&b[j]) {
                    j += 1;
                }
                while j < n && ('\x20'..='\x2f').contains(&b[j]) {
                    j += 1;
                }
                if j < n && ('\x40'..='\x7e').contains(&b[j]) {
                    if j == i + 2 {
                        match b[j] {
                            'A' => keys.push(Key::Up),
                            'B' => keys.push(Key::Down),
                            _ => {}
                        }
                    }
                    i = j + 1;
                } else if j == n {
                    return (keys, rest(i)); // still on its way
                } else {
                    i = j; // torn by a char outside the grammar: drop the head
                }
            }
            Some('O') => {
                if i + 2 == n {
                    return (keys, rest(i));
                }
                match b[i + 2] {
                    'A' => keys.push(Key::Up),
                    'B' => keys.push(Key::Down),
                    _ => {}
                }
                i += 3;
            }
            Some('\x1b') => {
                keys.push(Key::Esc); // ESC ESC: the first one was a press
                i += 1;
            }
            Some(_) => i += 2, // Alt+key: ignored
        }
    }
    (keys, String::new())
}

/// Raw bytes → keys across reads (UTF-8 carry + escape-sequence carry).
#[derive(Debug, Default)]
pub struct KeyReader {
    bytes: Vec<u8>,
    carry: String,
}

impl KeyReader {
    pub fn new() -> KeyReader {
        KeyReader::default()
    }

    fn decode(&mut self, data: &[u8]) -> String {
        self.bytes.extend_from_slice(data);
        let mut out = String::new();
        let mut rest: &[u8] = &self.bytes;
        loop {
            match std::str::from_utf8(rest) {
                Ok(s) => {
                    out.push_str(s);
                    rest = &[];
                    break;
                }
                Err(e) => {
                    let (ok, bad) = rest.split_at(e.valid_up_to());
                    out.push_str(std::str::from_utf8(ok).unwrap_or_default());
                    match e.error_len() {
                        None => {
                            rest = bad;
                            break;
                        }
                        Some(k) => {
                            out.push('\u{FFFD}');
                            rest = &bad[k..];
                        }
                    }
                }
            }
        }
        self.bytes = rest.to_vec();
        out
    }

    fn parse(&mut self, fresh: String) -> Vec<Key> {
        let text = format!("{}{}", std::mem::take(&mut self.carry), fresh);
        let (keys, carry) = parse_keys(&text, false);
        self.carry = carry;
        keys
    }

    /// Bytes read in raw mode.
    pub fn feed(&mut self, data: &[u8]) -> Vec<Key> {
        let fresh = self.decode(data);
        self.parse(fresh)
    }

    /// Bytes the tty line discipline took in before raw mode: ICRNL turned
    /// every Enter into LF, so LF is Enter here.
    pub fn feed_cooked(&mut self, data: &[u8]) -> Vec<Key> {
        let fresh = self.decode(data).replace('\n', "\r");
        self.parse(fresh)
    }

    /// Nothing followed within ESC_WAIT: resolve the carry.
    pub fn flush(&mut self) -> Vec<Key> {
        let held = std::mem::take(&mut self.carry);
        parse_keys(&held, true).0
    }

    /// An escape sequence is waiting for its rest.
    pub fn pending(&self) -> bool {
        !self.carry.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use Key::*;

    #[test]
    fn control_keys() {
        let (k, rest) = parse_keys("ab\r\n\x0e\x0b\x10\x7f\x08\x03\x15\x17\x01 ", false);
        assert_eq!(k, [Char('a'), Char('b'), Enter, Down, Down, Up, Up, Backspace, Backspace, CtrlC, CtrlU, CtrlW,
            Other, Char(' ')]);
        assert!(rest.is_empty());
    }

    #[test]
    fn sequences() {
        assert_eq!(parse_keys("\x1b[A\x1b[B\x1bOA\x1bOB", false).0, [Up, Down, Up, Down]);
        // Unknown CSI (Ctrl-arrows, paste brackets, Left) swallowed whole.
        assert_eq!(parse_keys("\x1b[1;5Ax\x1b[200~y\x1b[D", false).0, [Char('x'), Char('y')]);
        // Alt+key ignored; ESC ESC is one press then what follows.
        assert_eq!(parse_keys("\x1bwq\x1b\x1b[A", false).0, [Char('q'), Esc, Up]);
    }

    #[test]
    fn lone_esc_waits() {
        let mut r = KeyReader::new();
        assert_eq!(r.feed(b"ab\x1b"), [Char('a'), Char('b')]);
        assert!(r.pending());
        assert_eq!(r.flush(), [Esc]);
        // The rest of an arrow split across reads.
        assert_eq!(r.feed(b"\x1b["), []);
        assert_eq!(r.feed(b"B"), [Down]);
        assert!(!r.pending());
    }

    #[test]
    fn utf8_split() {
        let mut r = KeyReader::new();
        let s = "é".as_bytes();
        assert_eq!(r.feed(&s[..1]), []);
        assert_eq!(r.feed(&s[1..]), [Char('é')]);
    }

    #[test]
    fn cooked_lf_is_enter() {
        let mut r = KeyReader::new();
        // Typed before raw mode: "wo" Enter (ICRNL made it LF).
        assert_eq!(r.feed_cooked(b"wo\n"), [Char('w'), Char('o'), Enter]);
        // Raw from here on: LF is C-j.
        assert_eq!(r.feed(b"\n\r"), [Down, Enter]);
    }
}
