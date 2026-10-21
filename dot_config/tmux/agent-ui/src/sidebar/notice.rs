//! Darwin notifications (notify(3)) as a pollable fd: how the sidebar hears
//! "tmux changed" at once instead of polling for it.
//!
//! Two names, both posted with no payload (`notifyutil -p <name>`):
//!
//! - [`STATE_NOTICE`] (`com.mackhaymond.tmux.agent-state`): an agent's state
//!   moved. agent-tab-indicator.sh and agent-tab-watcher.sh post it on every
//!   write that changes `@agent_state` (docs/agent-tab-indicator.md "tmux is
//!   the truth"), as do tmux.conf's window-linked / window-unlinked hooks.
//! - [`FOCUS_NOTICE`] (`com.mackhaymond.tmux.focus`): where the user is
//!   changed: tmux hooks on a session / window switch, a session rename or
//!   close, and the M-v binding that flips `@strip_scope`. The sidebar posts
//!   it itself after its own scope toggle, so every other sidebar follows.
//!
//! `$AGENT_STATE_NOTICE` / `$AGENT_FOCUS_NOTICE` rename them (the scripts
//! honour the first one too), so tests never wake the real listeners.
//!
//! `notify_register_file_descriptor` hands back ONE fd for both names
//! (`NOTIFY_REUSE`); each post writes a 4-byte token to it. The fd is just
//! another entry in the loop's poll(2): no thread, no timer, nothing runs
//! while nothing is posted. Off macOS, or if registering fails, there is no
//! fd and the sidebar polls as before.

use std::os::fd::RawFd;
use std::time::Duration;

/// The agent-state notice (the scripts' `$AGENT_STATE_NOTICE` default).
pub const STATE_NOTICE: &str = "com.mackhaymond.tmux.agent-state";
/// The focus notice: session / window switches, renames, `@strip_scope`.
pub const FOCUS_NOTICE: &str = "com.mackhaymond.tmux.focus";

/// The two names in effect: the env overrides, else the defaults. An empty
/// override is ignored (`${AGENT_STATE_NOTICE-…}` in the scripts treats an
/// empty value as "post nothing"; the sidebar just keeps the default).
pub fn names() -> [String; 2] {
    let pick = |env: &str, default: &str| {
        std::env::var(env).ok().filter(|v| !v.is_empty()).unwrap_or_else(|| default.to_string())
    };
    [pick("AGENT_STATE_NOTICE", STATE_NOTICE), pick("AGENT_FOCUS_NOTICE", FOCUS_NOTICE)]
}

/// The insight notice: the server's insight publisher wrote a new result
/// ([`crate::insight::Board`]); the other sidebars read it at once, with no
/// snapshot. Registered on a fd of its own (a token says nothing of its
/// name). `$AGENT_INSIGHT_NOTICE` renames it (tests).
pub const INSIGHT_NOTICE: &str = "com.mackhaymond.agent-ui.insight";

/// [`INSIGHT_NOTICE`], or its env override.
pub fn insight_name() -> String {
    std::env::var("AGENT_INSIGHT_NOTICE").ok().filter(|v| !v.is_empty()).unwrap_or_else(|| INSIGHT_NOTICE.to_string())
}

/// How long a notice waits for the rest of its burst before the refresh:
/// a window switch fires session-window-changed, the indicator's
/// clear-current and maybe a state write within a few ms of each other.
pub const DEBOUNCE: Duration = Duration::from_millis(12);

#[cfg(target_os = "macos")]
mod sys {
    use std::os::raw::{c_char, c_int};
    pub const NOTIFY_STATUS_OK: u32 = 0;
    pub const NOTIFY_REUSE: c_int = 1;
    extern "C" {
        pub fn notify_register_file_descriptor(name: *const c_char, fd: *mut c_int, flags: c_int, token: *mut c_int) -> u32;
        pub fn notify_cancel(token: c_int) -> u32;
        pub fn notify_post(name: *const c_char) -> u32;
    }
}

/// The registered names and their shared fd.
#[derive(Debug, Default)]
pub struct Notices {
    fd: Option<RawFd>,
    tokens: Vec<i32>,
}

impl Notices {
    /// Register every name on one fd. Names that fail are skipped; with none
    /// registered there is no fd ([`fd`](Self::fd) None).
    #[cfg(target_os = "macos")]
    pub fn register(names: &[String]) -> Notices {
        let mut n = Notices::default();
        for name in names {
            let Ok(c) = std::ffi::CString::new(name.as_str()) else { continue };
            let mut fd = n.fd.unwrap_or(-1);
            let mut token = 0;
            let flags = if n.fd.is_some() { sys::NOTIFY_REUSE } else { 0 };
            // SAFETY: a NUL-terminated name and two valid out-pointers.
            let st = unsafe { sys::notify_register_file_descriptor(c.as_ptr(), &mut fd, flags, &mut token) };
            if st == sys::NOTIFY_STATUS_OK && fd >= 0 {
                n.fd = Some(fd);
                n.tokens.push(token);
            }
        }
        n
    }

    #[cfg(not(target_os = "macos"))]
    pub fn register(_names: &[String]) -> Notices {
        Notices::default()
    }

    /// The fd to poll (None: no notices; poll on a timer).
    pub fn fd(&self) -> Option<RawFd> {
        self.fd
    }

    /// Read every token waiting (call after poll says the fd is readable)
    /// → how many notices that was. Never blocks: it reads only while poll
    /// says there is more.
    pub fn drain(&self) -> usize {
        let Some(fd) = self.fd else { return 0 };
        let mut n = 0;
        let mut buf = [0u8; 256];
        while super::term::readable(fd, Duration::ZERO).unwrap_or(false) {
            match read_fd(fd, &mut buf) {
                Some(k) if k > 0 => n += k.div_ceil(4),
                _ => break,
            }
        }
        n
    }
}

fn read_fd(fd: RawFd, buf: &mut [u8]) -> Option<usize> {
    extern "C" {
        fn read(fd: i32, buf: *mut u8, n: usize) -> isize;
    }
    // SAFETY: `buf` is a valid, exclusively borrowed buffer of `buf.len()` bytes.
    let k = unsafe { read(fd, buf.as_mut_ptr(), buf.len()) };
    (k >= 0).then_some(k as usize)
}

impl Drop for Notices {
    /// Cancelling the last token closes the fd (notify(3)).
    fn drop(&mut self) {
        #[cfg(target_os = "macos")]
        for &t in &self.tokens {
            // SAFETY: a token notify_register_file_descriptor returned.
            unsafe {
                sys::notify_cancel(t);
            }
        }
    }
}

/// Post `name` (notifyutil -p, without the fork). False if it failed or
/// there is no notify(3) here.
pub fn post(name: &str) -> bool {
    #[cfg(target_os = "macos")]
    {
        let Ok(c) = std::ffi::CString::new(name) else { return false };
        // SAFETY: a NUL-terminated name.
        unsafe { sys::notify_post(c.as_ptr()) == sys::NOTIFY_STATUS_OK }
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = name;
        false
    }
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::*;
    use std::time::Instant;

    #[test]
    fn a_post_wakes_the_fd_and_drains() {
        let a = format!("com.mackhaymond.agent-ui.test.{}.a", std::process::id());
        let b = format!("com.mackhaymond.agent-ui.test.{}.b", std::process::id());
        let n = Notices::register(&[a.clone(), b.clone()]);
        let fd = n.fd().expect("notify fd");
        assert_eq!(n.tokens.len(), 2);
        assert!(!super::super::term::readable(fd, Duration::from_millis(20)).unwrap());
        // Either name wakes the one fd, within milliseconds.
        for name in [&a, &b] {
            let t = Instant::now();
            assert!(post(name));
            assert!(super::super::term::readable(fd, Duration::from_secs(2)).unwrap(), "{name}");
            assert!(t.elapsed() < Duration::from_millis(500));
            assert!(n.drain() >= 1);
            assert!(!super::super::term::readable(fd, Duration::ZERO).unwrap());
        }
        // A burst drains in one go.
        for _ in 0..5 {
            post(&a);
        }
        std::thread::sleep(Duration::from_millis(50));
        assert!(n.drain() >= 1);
        assert_eq!(n.drain(), 0);
    }
}
