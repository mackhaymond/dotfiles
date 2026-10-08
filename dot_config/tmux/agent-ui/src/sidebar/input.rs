//! Raw terminal input → SGR mouse reports and typed bytes.
//!
//! The sidebar reads stdin itself, not through crossterm's event reader,
//! because a key typed into it must reach the tmux pane byte for byte
//! (agent-roster.py `Strip.handle` forwards it with `wezterm cli send-text
//! --no-paste`). So a read is split here into mouse reports (`ESC [ < b ; x ;
//! y M|m`, DECSET 1006) and everything else, the "stray" bytes.
//!
//! A report cut off at the end of a read (`ESC [ < 0 ; 5`) is dropped, and so
//! is its tail (`;7M`) at the start of the next read: neither is typing
//! (`Strip.stray_bytes` with `MOUSE_TAIL` / `MOUSE_HEAD`). The loop waits
//! ESC_WAIT for the rest of a cut report first ([`incomplete`]), so this is rare.

/// One SGR mouse report. `x`, `y` are 1-based, as the terminal sends them.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Mouse {
    /// The button byte: low 2 bits the button, 32 motion, 64 the wheel,
    /// 4/8/16 modifiers.
    pub b: u16,
    pub x: u16,
    pub y: u16,
    /// `M` (press, or motion) rather than `m` (release).
    pub press: bool,
}

/// What one read held.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Input {
    pub mice: Vec<Mouse>,
    /// Everything that was not a mouse report, in order.
    pub stray: Vec<u8>,
}

/// The splitter, with the one bit of state a cut report leaves behind.
#[derive(Debug, Default)]
pub struct Reader {
    /// The last read ended inside a report (Strip.cut_mouse).
    cut: bool,
}

/// `<`? then `[0-9;]*` from `i`: the end of that run.
fn params_end(raw: &[u8], i: usize) -> usize {
    let mut j = i;
    if raw.get(j) == Some(&b'<') {
        j += 1;
    }
    while raw.get(j).is_some_and(|c| c.is_ascii_digit() || *c == b';') {
        j += 1;
    }
    j
}

/// `b;x;y` → the three numbers, or None (the report is still dropped).
fn parse_params(p: &[u8]) -> Option<(u16, u16, u16)> {
    let s = std::str::from_utf8(p).ok()?;
    let mut it = s.split(';').map(|n| n.parse::<u16>().ok());
    let r = (it.next()??, it.next()??, it.next()??);
    it.next().is_none().then_some(r)
}

/// agent-roster.py `MOUSE_TAIL`: the read ends with a report (or any CSI)
/// still open, so the rest is worth waiting ESC_WAIT for.
pub fn incomplete(raw: &[u8]) -> bool {
    let Some(esc) = raw.iter().rposition(|&c| c == 0x1b) else { return false };
    let rest = &raw[esc + 1..];
    rest.first() == Some(&b'[') && params_end(rest, 1) == rest.len()
}

impl Reader {
    pub fn feed(&mut self, raw: &[u8]) -> Input {
        let mut out = Input::default();
        let mut i = 0;
        if std::mem::take(&mut self.cut) {
            // MOUSE_HEAD: the rest of the report the last read cut off.
            let j = params_end(raw, 0);
            if matches!(raw.get(j), Some(b'M' | b'm')) {
                i = j + 1;
            }
        }
        while i < raw.len() {
            if raw[i] == 0x1b && raw.get(i + 1) == Some(&b'[') {
                let open = raw.get(i + 2) == Some(&b'<');
                let j = params_end(raw, i + 2);
                if j == raw.len() {
                    // A report or CSI cut off at the end: drop it, and its tail next time.
                    self.cut = true;
                    break;
                }
                if open && matches!(raw[j], b'M' | b'm') {
                    if let Some((b, x, y)) = parse_params(&raw[i + 3..j]) {
                        out.mice.push(Mouse { b, x, y, press: raw[j] == b'M' });
                    }
                    i = j + 1;
                    continue;
                }
            }
            out.stray.push(raw[i]);
            i += 1;
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn m(b: u16, x: u16, y: u16, press: bool) -> Mouse {
        Mouse { b, x, y, press }
    }

    #[test]
    fn splits_reports_from_typing() {
        let mut r = Reader::default();
        let got = r.feed(b"a\x1b[<0;5;7Mb\x1b[<0;5;7m\x1b[A\x1b[<64;1;1M");
        assert_eq!(got.mice, vec![m(0, 5, 7, true), m(0, 5, 7, false), m(64, 1, 1, true)]);
        assert_eq!(got.stray, b"ab\x1b[A");
        // Malformed reports are not typing either.
        let got = r.feed(b"\x1b[<1;2Mz");
        assert!(got.mice.is_empty());
        assert_eq!(got.stray, b"z");
        // A lone ESC (the key) is typing.
        assert_eq!(r.feed(b"\x1b").stray, b"\x1b");
    }

    #[test]
    fn cut_reports_are_dropped_with_their_tail() {
        let mut r = Reader::default();
        let got = r.feed(b"x\x1b[<0;5");
        assert_eq!((got.mice.len(), got.stray.as_slice()), (0, b"x".as_slice()));
        let got = r.feed(b";7My");
        assert_eq!((got.mice.len(), got.stray.as_slice()), (0, b"y".as_slice()));
        // A fresh read that does not continue one is left alone.
        let mut r = Reader::default();
        assert_eq!(r.feed(b";7M").stray, b";7M");
    }

    #[test]
    fn incomplete_tails() {
        assert!(incomplete(b"\x1b[<0;5"));
        assert!(incomplete(b"ab\x1b["));
        assert!(incomplete(b"\x1b[<"));
        assert!(!incomplete(b"\x1b[<0;5;7M"));
        assert!(!incomplete(b"\x1b"));
        assert!(!incomplete(b"\x1b[A"));
        assert!(!incomplete(b"abc"));
    }
}
