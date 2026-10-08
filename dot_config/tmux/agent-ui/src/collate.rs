//! The collation `/usr/bin/sort` uses inside agent-jump.sh `list`: the libc
//! collation of the environment's locale. Mirrors agent-roster.py `collation`.
//!
//! The Python calls `setlocale(LC_COLLATE, "")` and sorts by `strxfrm`, which
//! on macOS is `wcsxfrm`, i.e. the same order as `wcscoll`. Here the locale is
//! opened with `newlocale` and compared with `wcscoll_l`, which touches no
//! process-global state (so tests can compare locales side by side, and a
//! background thread is never surprised by a `setlocale`).
//!
//! The name is resolved like `setlocale(LC_COLLATE, "")` does: `LC_ALL`, then
//! `LC_COLLATE`, then `LANG` (first non-empty), else "C". An unknown locale
//! falls back to C, as the Python's `except locale.Error` does; C compares
//! code points (`wcscmp`, or the Python's plain `str`).

use std::cmp::Ordering;
use std::ffi::{c_void, CString};
use std::os::raw::{c_char, c_int};

type LocaleT = *mut c_void;
/// wchar_t is 32-bit on macOS and Linux.
type WChar = i32;

const LC_COLLATE_MASK: c_int = 1 << 0;
const LC_CTYPE_MASK: c_int = 1 << 1;

extern "C" {
    fn newlocale(mask: c_int, locale: *const c_char, base: LocaleT) -> LocaleT;
    fn freelocale(loc: LocaleT);
    fn wcscoll_l(a: *const WChar, b: *const WChar, loc: LocaleT) -> c_int;
}

/// A collation order. `Collator::from_env()` is what agent-jump.sh's sort sees.
pub struct Collator {
    loc: LocaleT,
    name: String,
}

// SAFETY: a locale_t is an immutable, reference-counted object once created;
// wcscoll_l only reads it.
unsafe impl Send for Collator {}
unsafe impl Sync for Collator {}

impl Drop for Collator {
    fn drop(&mut self) {
        if !self.loc.is_null() {
            // SAFETY: created by newlocale, freed once.
            unsafe { freelocale(self.loc) }
        }
    }
}

/// The LC_COLLATE name the environment selects (LC_ALL > LC_COLLATE > LANG).
pub fn env_locale_name() -> String {
    ["LC_ALL", "LC_COLLATE", "LANG"]
        .iter()
        .filter_map(|k| std::env::var(k).ok())
        .find(|v| !v.is_empty())
        .unwrap_or_else(|| "C".into())
}

impl Collator {
    /// The collation of `name` ("C"/"POSIX" or unknown: code points).
    pub fn new(name: &str) -> Collator {
        let mut loc: LocaleT = std::ptr::null_mut();
        if name != "C" && name != "POSIX" {
            if let Ok(c) = CString::new(name) {
                // SAFETY: valid C string; base NULL means "start from C".
                loc = unsafe { newlocale(LC_COLLATE_MASK | LC_CTYPE_MASK, c.as_ptr(), std::ptr::null_mut()) };
            }
        }
        let name = if loc.is_null() { "C".to_string() } else { name.to_string() };
        Collator { loc, name }
    }

    /// The environment's collation (what agent-jump.sh's `sort` uses).
    pub fn from_env() -> Collator {
        Collator::new(&env_locale_name())
    }

    /// The locale actually in use ("C" after a fallback).
    pub fn name(&self) -> &str {
        &self.name
    }

    /// wcscoll order of two strings (NULs, which tmux never prints, end a string).
    pub fn coll(&self, a: &str, b: &str) -> Ordering {
        if self.loc.is_null() {
            return a.cmp(b); // UTF-8 byte order == code point order
        }
        let wide = |s: &str| -> Vec<WChar> {
            s.chars().take_while(|&c| c != '\0').map(|c| c as WChar).chain(std::iter::once(0)).collect()
        };
        let (wa, wb) = (wide(a), wide(b));
        // SAFETY: both NUL-terminated; loc is a live locale.
        let r = unsafe { wcscoll_l(wa.as_ptr(), wb.as_ptr(), self.loc) };
        r.cmp(&0)
    }

    /// FreeBSD/Apple sort's text-key compare: wcscoll, and when that calls
    /// them equal, the SHORTER (in characters) first. See `needs_order`.
    pub fn sort_cmp(&self, a: &str, b: &str) -> Ordering {
        self.coll(a, b).then_with(|| a.chars().count().cmp(&b.chars().count()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn en_us_vs_c() {
        let en = Collator::new("en_US.UTF-8");
        let c = Collator::new("C");
        assert_eq!(en.name(), "en_US.UTF-8");
        assert_eq!(en.coll("a", "B"), Ordering::Less);
        assert_eq!(c.coll("a", "B"), Ordering::Greater);
        assert_eq!(en.coll("_x", "a"), Ordering::Less);
        // Weightless under en_US: emoji compare equal, length decides.
        assert_eq!(en.coll("dev 🚀", "dev 🔥"), Ordering::Equal);
        assert_eq!(en.sort_cmp("dev 🔥", "dev 🔥🔥"), Ordering::Less);
        assert_eq!(Collator::new("xx_NOPE.UTF-8").name(), "C");
    }
}
