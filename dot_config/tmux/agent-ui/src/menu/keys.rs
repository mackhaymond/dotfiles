//! Raw input bytes → keys: a port of agent-roster.py `parse_keys` and
//! `KeyReader`, extended with the keys the new menu adds.
//!
//! Why not crossterm's event reader: inside a tmux popup every Option key
//! arrives as ESC + char, and crossterm decides "lone ESC or Alt+key" by
//! whether more bytes sat in the same read, with no wait. A read boundary
//! between ESC and `w` would then close the menu as Esc and type a `w`. The
//! Python waits [`ESC_WAIT`] for the rest instead; so does this.
//!
//! Rules (each one the Python's unless noted):
//! - A read can end mid-sequence: a trailing `ESC`, `ESC [` or `ESC [1;` is
//!   carried to the next read, never read as Esc. Only after the caller has
//!   waited [`ESC_WAIT`] with nothing more (`final`) is a lone ESC the Esc key,
//!   and a still-incomplete sequence dropped.
//! - Known CSI/SS3 sequences map to keys; unknown ones (Ctrl-arrows, paste
//!   brackets, Left/Right) are swallowed whole, or their ESC would read as
//!   "close". NEW: Home/End (`CSI H/F`, `SS3 H/F`, `CSI 1~/4~/7~/8~`) are keys
//!   now; the Python swallowed them.
//! - Alt+key (ESC + one char) is ignored, except Option-W ([`Key::AltW`], the
//!   key that opened the popup closes it) and NEW: Option-S / Option-X
//!   ([`Key::AltS`] / [`Key::AltX`]: next / back). ESC ESC is one Esc press
//!   followed by whatever the second ESC starts.
//! - SGR mouse reports (DECSET 1006) become [`Key::Mouse`].
//! - UTF-8 split across reads is reassembled (the Python's incremental
//!   decoder); invalid bytes become U+FFFD.

use std::time::Duration;

/// agent-roster.py `ESC_WAIT`: how long a trailing ESC / partial sequence
/// waits for the rest before it counts as typed.
pub const ESC_WAIT: Duration = Duration::from_millis(25);

/// One SGR mouse report: `ESC [ < button ; col ; row M|m`, 1-based col/row
/// converted to 0-based.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Mouse {
    /// The raw button code (0 left, 64/65 wheel; +4/8/16 modifiers, +32 motion).
    pub button: u16,
    pub col: u16,
    pub row: u16,
    /// `M` (press) rather than `m` (release).
    pub press: bool,
}

impl Mouse {
    /// A left-button press (modifiers allowed, motion not).
    pub fn is_click(&self) -> bool {
        self.press && self.button & !(4 | 8 | 16) == 0
    }

    /// Wheel up (-1) / down (+1), else None.
    pub fn wheel(&self) -> Option<i32> {
        if !self.press || self.button & 64 == 0 || self.button & 32 != 0 {
            return None;
        }
        match self.button & 3 {
            0 => Some(-1),
            1 => Some(1),
            _ => None,
        }
    }
}

/// A key, as the menu acts on it (the Python's key names).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Key {
    /// A printable character, space included (the Python's "space" is
    /// `Char(' ')`).
    Char(char),
    Up,
    Down,
    Home,
    End,
    PgUp,
    PgDn,
    Tab,
    /// Shift-Tab (`CSI Z`, "btab").
    BackTab,
    Enter,
    /// DEL or BS ("bs").
    Backspace,
    Esc,
    CtrlC,
    CtrlN,
    CtrlP,
    CtrlU,
    /// Any other C0 control: does nothing, but like any non-digit key it
    /// ends a half-typed or dead number (the Python passed these through raw).
    Other,
    AltW,
    AltS,
    AltX,
    Mouse(Mouse),
}

/// agent-roster.py `KEYSEQ` plus Home/End: complete CSI/SS3 sequences that
/// are keys. Anything else complete is swallowed.
fn keyseq(seq: &str) -> Option<Key> {
    Some(match seq {
        "\x1b[A" | "\x1bOA" => Key::Up,
        "\x1b[B" | "\x1bOB" => Key::Down,
        "\x1b[Z" => Key::BackTab,
        "\x1b[5~" => Key::PgUp,
        "\x1b[6~" => Key::PgDn,
        "\x1b[H" | "\x1bOH" | "\x1b[1~" | "\x1b[7~" => Key::Home,
        "\x1b[F" | "\x1bOF" | "\x1b[4~" | "\x1b[8~" => Key::End,
        _ => return None,
    })
}

/// agent-roster.py `KEYCHR` (plus C-n / C-p / C-u): one non-ESC char.
fn keychr(c: char) -> Key {
    match c {
        '\t' => Key::Tab,
        '\r' | '\n' => Key::Enter,
        '\x7f' | '\x08' => Key::Backspace,
        '\x03' => Key::CtrlC,
        '\x0e' => Key::CtrlN,
        '\x10' => Key::CtrlP,
        '\x15' => Key::CtrlU,
        c if (c as u32) < 0x20 || (0x80..=0x9f).contains(&(c as u32)) => Key::Other,
        c => Key::Char(c),
    }
}

/// `CSI`: ESC [ params(0x30-0x3f)* intermediates(0x20-0x2f)* final(0x40-0x7e)
/// starting at `i` → (end, complete). `complete == false` with end == n
/// means the read ended inside it (the Python's `CSI_PART`).
fn scan_csi(b: &[char], i: usize) -> (usize, bool) {
    let mut j = i + 2;
    while j < b.len() && ('\x30'..='\x3f').contains(&b[j]) {
        j += 1;
    }
    while j < b.len() && ('\x20'..='\x2f').contains(&b[j]) {
        j += 1;
    }
    if j < b.len() && ('\x40'..='\x7e').contains(&b[j]) {
        (j + 1, true)
    } else {
        (j, false)
    }
}

/// agent-roster.py `MOUSE_SGR` on one complete CSI.
fn mouse_sgr(seq: &str) -> Option<Mouse> {
    let body = seq.strip_prefix("\x1b[<")?;
    let (nums, last) = body.split_at(body.len().checked_sub(1)?);
    let press = match last {
        "M" => true,
        "m" => false,
        _ => return None,
    };
    let mut it = nums.split(';').map(|p| if p.is_empty() { None } else { p.parse::<u16>().ok() });
    let (b, c, r) = (it.next()??, it.next()??, it.next()??);
    if it.next().is_some() {
        return None;
    }
    Some(Mouse { button: b, col: c.saturating_sub(1), row: r.saturating_sub(1), press })
}

/// agent-roster.py `parse_keys`: decoded input → (keys, leftover). See the
/// module docs for the rules; `final` means the caller already waited
/// [`ESC_WAIT`] and nothing followed.
pub fn parse_keys(buf: &str, fin: bool) -> (Vec<Key>, String) {
    let b: Vec<char> = buf.chars().collect();
    let n = b.len();
    let mut keys = Vec::new();
    let mut i = 0;
    let rest = |i: usize| b[i..].iter().collect::<String>();
    while i < n {
        let c = b[i];
        if c != '\x1b' {
            keys.push(keychr(c));
            i += 1;
            continue;
        }
        // A complete CSI or SS3 sequence: a key, a mouse report, or swallowed.
        if b.get(i + 1) == Some(&'[') {
            let (end, complete) = scan_csi(&b, i);
            if complete {
                let seq: String = b[i..end].iter().collect();
                if let Some(k) = keyseq(&seq) {
                    keys.push(k);
                } else if let Some(m) = mouse_sgr(&seq) {
                    keys.push(Key::Mouse(m));
                }
                i = end;
                continue;
            }
            if end == n {
                // The rest is still on its way (CSI_PART).
                return (keys, if fin { String::new() } else { rest(i) });
            }
            // A CSI torn by a char outside its grammar: Alt+[ then that char.
        } else if b.get(i + 1) == Some(&'O') && i + 2 < n && (' '..='~').contains(&b[i + 2]) {
            let seq: String = b[i..i + 3].iter().collect();
            if let Some(k) = keyseq(&seq) {
                keys.push(k);
            }
            i += 3;
            continue;
        }
        if i + 1 == n {
            // ESC is the last char: carried until the wait says it was a press.
            if fin {
                keys.push(Key::Esc);
                i += 1;
                continue;
            }
            return (keys, rest(i));
        }
        let nxt = b[i + 1];
        if nxt == 'O' && i + 2 == n {
            return (keys, if fin { String::new() } else { rest(i) });
        }
        if nxt == '\x1b' {
            // ESC ESC: the first one was a press.
            keys.push(Key::Esc);
            i += 1;
            continue;
        }
        match nxt {
            'w' | 'W' => keys.push(Key::AltW),
            's' | 'S' => keys.push(Key::AltS),
            'x' | 'X' => keys.push(Key::AltX),
            _ => {} // any other Alt+key: ignored
        }
        i += 2;
    }
    (keys, String::new())
}

/// agent-roster.py `KeyReader`: raw bytes → keys across reads. Holds the
/// UTF-8 carry (a multi-byte char split between reads) and the carry of an
/// incomplete escape sequence. No I/O: the caller does the waiting.
#[derive(Debug, Default)]
pub struct KeyReader {
    bytes: Vec<u8>,
    carry: String,
    /// The last flush dropped a CSI cut off by the read (`ESC [ < 0 ; 5`, a
    /// mouse report, or `ESC [ 1 ;`, a Ctrl-arrow): its tail (`;7M`, `5C`)
    /// at the start of the next read is not typing either, or its digits
    /// would jump. agent-roster.py `Strip.stray_bytes` (MOUSE_TAIL /
    /// MOUSE_HEAD), widened from mouse reports to any CSI.
    cut_csi: bool,
}

/// The length of a CSI's tail at the start of `s`: parameters (0x30-0x3f,
/// `<` and digits included), intermediates, then a final byte. None when
/// `s` does not start with one.
fn csi_tail(s: &str) -> Option<usize> {
    let b = s.as_bytes();
    let mut n = b.iter().take_while(|c| (0x30..=0x3f).contains(*c)).count();
    n += b[n..].iter().take_while(|c| (0x20..=0x2f).contains(*c)).count();
    b.get(n).filter(|c| (0x40..=0x7e).contains(*c)).map(|_| n + 1)
}

impl KeyReader {
    pub fn new() -> KeyReader {
        KeyReader::default()
    }

    /// Decode what can be decoded; keep an incomplete trailing char.
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
                            rest = bad; // incomplete: wait for the rest
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

    pub fn feed(&mut self, data: &[u8]) -> Vec<Key> {
        let mut fresh = self.decode(data);
        if std::mem::take(&mut self.cut_csi) {
            if let Some(n) = csi_tail(&fresh) {
                fresh.drain(..n);
            }
        }
        let text = format!("{}{}", std::mem::take(&mut self.carry), fresh);
        let (keys, carry) = parse_keys(&text, false);
        self.carry = carry;
        keys
    }

    /// Nothing followed within ESC_WAIT: resolve the carry.
    pub fn flush(&mut self) -> Vec<Key> {
        let held = std::mem::take(&mut self.carry);
        // A CSI cut off by the read (the carry is only ever the cut tail).
        self.cut_csi = held.starts_with("\x1b[");
        let (keys, carry) = parse_keys(&held, true);
        self.carry = carry;
        keys
    }

    /// An escape sequence is waiting for its rest (not a split UTF-8 char).
    pub fn pending(&self) -> bool {
        !self.carry.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use Key::*;

    fn p(s: &str) -> (Vec<Key>, String) {
        parse_keys(s, false)
    }

    /// agent-roster tests KeyTests.test_keys.
    #[test]
    fn keys() {
        assert_eq!(p("\x1b[A\x1b[B\x1b[Z\t\r j"), (vec![Up, Down, BackTab, Tab, Enter, Char(' '), Char('j')], "".into()));
        // Unknown CSI sequences are swallowed, never read as esc. NEW: SS3 F is End.
        assert_eq!(p("\x1bOF\x1b[1;5Cq\x1b[200~"), (vec![End, Char('q')], "".into()));
        assert_eq!(p("\x03"), (vec![CtrlC], "".into()));
        assert_eq!(p("\x0e\x10\x15\x01"), (vec![CtrlN, CtrlP, CtrlU, Other], "".into()));
        for (s, k) in [("\x1b[H", Home), ("\x1bOH", Home), ("\x1b[1~", Home), ("\x1b[7~", Home),
            ("\x1b[F", End), ("\x1b[4~", End), ("\x1b[8~", End), ("\x1b[5~", PgUp), ("\x1b[6~", PgDn),
            ("\x1bOA", Up), ("\x1bOB", Down)] {
            assert_eq!(p(s), (vec![k], "".into()), "{s:?}");
        }
        assert_eq!(p("\x1b[D\x1b[C"), (vec![], "".into())); // left/right: swallowed
    }

    /// test_lone_esc_waits.
    #[test]
    fn lone_esc_waits() {
        assert_eq!(p("j\x1b"), (vec![Char('j')], "\x1b".into()));
        assert_eq!(parse_keys("\x1b", true), (vec![Esc], "".into()));
        let mut r = KeyReader::new();
        assert_eq!(r.feed(b"\x1b"), vec![]);
        assert!(r.pending());
        assert_eq!(r.flush(), vec![Esc]);
        assert!(!r.pending());
    }

    /// test_partial_sequences_are_carried.
    #[test]
    fn partial_sequences_are_carried() {
        for (head, tail, want) in [("\x1b[", "A", vec![Up]), ("\x1b[1;", "5C", vec![]), ("\x1bO", "B", vec![Down]),
            ("\x1b[6", "~k", vec![PgDn, Char('k')]), ("\x1b[<0;5", ";7M", vec![Mouse(super::Mouse {
                button: 0, col: 4, row: 6, press: true })])] {
            assert_eq!(p(&format!("j{head}")), (vec![Char('j')], head.to_string()), "{head:?}");
            let mut r = KeyReader::new();
            assert_eq!(r.feed(format!("j{head}").as_bytes()), vec![Char('j')]);
            assert!(r.pending());
            assert_eq!(r.feed(tail.as_bytes()), want, "{head:?}");
            assert!(!r.pending());
        }
        // A sequence that never completes is dropped, never esc.
        assert_eq!(parse_keys("\x1b[1;", true), (vec![], "".into()));
        let mut r = KeyReader::new();
        r.feed(b"\x1b[");
        assert_eq!(r.flush(), vec![]);
    }

    /// test_alt_key_is_not_esc, with the menu's Alt keys.
    #[test]
    fn alt_keys() {
        assert_eq!(p("\x1bj"), (vec![], "".into()));
        assert_eq!(p("\x1bjk"), (vec![Char('k')], "".into()));
        assert_eq!(parse_keys("\x1b\x1b", true), (vec![Esc, Esc], "".into()));
        // OptionWTests.test_parse, plus Option-S / Option-X.
        assert_eq!(p("\x1bw").0, vec![AltW]);
        assert_eq!(p("\x1bW").0, vec![AltW]);
        assert_eq!(p("\x1bs\x1bX").0, vec![AltS, AltX]);
        assert_eq!(p("\x1bxk").0, vec![AltX, Char('k')]); // the Python ignored Alt-x
        // ESC ESC w: one Esc, then Option-W.
        assert_eq!(p("\x1b\x1bw").0, vec![Esc, AltW]);
        // ESC then w in a LATER read is still Option-W (the wait).
        let mut r = KeyReader::new();
        assert_eq!(r.feed(b"\x1b"), vec![]);
        assert_eq!(r.feed(b"w"), vec![AltW]);
    }

    /// test_split_utf8.
    #[test]
    fn split_utf8() {
        let mut r = KeyReader::new();
        let data = "é😀".as_bytes();
        assert_eq!(r.feed(&data[..1]), vec![]);
        assert_eq!(r.feed(&data[1..4]), vec![Char('é')]);
        assert!(!r.pending());
        assert_eq!(r.feed(&data[4..]), vec![Char('😀')]);
        assert_eq!(r.feed(b"\xffa"), vec![Char('\u{FFFD}'), Char('a')]);
    }

    /// StripTests.test_cut_mouse_report_is_not_typing: a report split across
    /// the ESC_WAIT flush never types its tail (a `3` would jump to row 3).
    #[test]
    fn cut_mouse_report_is_not_typing() {
        let mut r = KeyReader::new();
        assert_eq!(r.feed(b"\x1b[<0;5"), vec![]);
        assert_eq!(r.flush(), vec![]);
        assert_eq!(r.feed(b";3M"), vec![]);
        assert_eq!(r.feed(b"3"), vec![Char('3')]); // only the next read's head
        // Cut right after ESC [ <, and right after ESC [.
        for (head, tail) in [("\x1b[<", "0;12;3Mj"), ("\x1b[", "<0;1;1Mj")] {
            let mut r = KeyReader::new();
            r.feed(head.as_bytes());
            r.flush();
            assert_eq!(r.feed(tail.as_bytes()), vec![Char('j')], "{head:?}");
        }
        // A plain lone ESC is not a cut report: what follows is typing.
        let mut r = KeyReader::new();
        r.feed(b"\x1b");
        assert_eq!(r.flush(), vec![Esc]);
        assert_eq!(r.feed(b"3M"), vec![Char('3'), Char('M')]);
        // A cut Ctrl-arrow: its tail does not type a 5.
        let mut r = KeyReader::new();
        r.feed(b"\x1b[1;");
        r.flush();
        assert_eq!(r.feed(b"5Ck"), vec![Char('k')]);
        // A next read that is not a CSI tail is left alone.
        let mut r = KeyReader::new();
        r.feed(b"\x1b[<0;5");
        r.flush();
        assert_eq!(r.feed(b"12"), vec![Char('1'), Char('2')]);
    }

    #[test]
    fn mouse_reports() {
        let (k, _) = p("\x1b[<0;10;3M\x1b[<0;10;3m\x1b[<64;1;1M\x1b[<65;1;1M\x1b[<35;2;2M");
        let ms: Vec<super::Mouse> = k.iter().map(|k| match k { Mouse(m) => *m, _ => panic!("{k:?}") }).collect();
        assert_eq!((ms[0].col, ms[0].row, ms[0].is_click()), (9, 2, true));
        assert!(!ms[1].is_click()); // release
        assert_eq!((ms[2].wheel(), ms[3].wheel()), (Some(-1), Some(1)));
        assert!(!ms[4].is_click() && ms[4].wheel().is_none()); // motion
        assert_eq!(p("\x1b[<1;2M").0, vec![]); // malformed: swallowed
    }
}
