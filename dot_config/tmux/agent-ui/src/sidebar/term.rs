//! The few POSIX pieces the sidebar loop needs that std lacks: `poll(2)` on
//! stdin plus a self-pipe, with SIGHUP / SIGTERM / SIGWINCH caught into it.
//! This is the Python's `signal.set_wakeup_fd` + `select` (agent-roster.py
//! `main`): a signal wakes the wait at once, whatever SA_RESTART says.

use std::fs::File;
use std::io::{self, Read};
use std::mem::ManuallyDrop;
use std::os::fd::{AsRawFd, FromRawFd, RawFd};
use std::os::unix::net::UnixStream;
use std::sync::atomic::{AtomicI32, AtomicU32, Ordering};
use std::time::Duration;

/// Signal numbers (the same on macOS and Linux).
pub const SIGHUP: i32 = 1;
pub const SIGTERM: i32 = 15;
pub const SIGWINCH: i32 = 28;

#[repr(C)]
struct PollFd {
    fd: i32,
    events: i16,
    revents: i16,
}

const POLLIN: i16 = 0x1;
const POLLERR: i16 = 0x8;
const POLLHUP: i16 = 0x10;
const POLLNVAL: i16 = 0x20;

#[cfg(target_os = "macos")]
type NFds = std::os::raw::c_uint;
#[cfg(not(target_os = "macos"))]
type NFds = std::os::raw::c_ulong;

extern "C" {
    fn poll(fds: *mut PollFd, n: NFds, timeout: i32) -> i32;
    fn signal(sig: i32, handler: extern "C" fn(i32)) -> usize;
    fn write(fd: i32, buf: *const u8, n: usize) -> isize;
}

/// The self-pipe's write end, for the handler (-1: not installed).
static WAKE_FD: AtomicI32 = AtomicI32::new(-1);
/// Bit `1 << sig` per signal caught since the last [`Signals::take`].
static FIRED: AtomicU32 = AtomicU32::new(0);

extern "C" fn on_signal(sig: i32) {
    // Async-signal-safe: an atomic or and one write(2) to a non-blocking socket.
    FIRED.fetch_or(1 << sig, Ordering::SeqCst);
    let fd = WAKE_FD.load(Ordering::SeqCst);
    if fd >= 0 {
        // SAFETY: a 1-byte write from a static buffer; a full pipe just fails (EAGAIN).
        unsafe {
            write(fd, b"!".as_ptr(), 1);
        }
    }
}

/// What the caught signals ask for.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Fired {
    /// SIGHUP / SIGTERM: CMD+B's kill-pane. Leave quietly (Python `bye`).
    pub exit: bool,
    /// SIGWINCH: redraw in full.
    pub resize: bool,
}

/// The installed handlers and the self-pipe they write to.
pub struct Signals {
    rx: UnixStream,
    tx: UnixStream,
}

/// poll(2) over `fds` → the revents, or all zero when a signal interrupted it.
fn poll_fds(fds: &mut [PollFd], timeout: Duration) -> io::Result<()> {
    let ms = timeout.as_millis().min(i32::MAX as u128) as i32;
    // SAFETY: `fds` is a valid, exclusively borrowed array of pollfd structs.
    let n = unsafe { poll(fds.as_mut_ptr(), fds.len() as NFds, ms) };
    if n < 0 {
        let e = io::Error::last_os_error();
        if e.kind() != io::ErrorKind::Interrupted {
            return Err(e);
        }
        fds.iter_mut().for_each(|f| f.revents = 0);
    }
    Ok(())
}

fn ready(f: &PollFd) -> bool {
    f.revents & (POLLIN | POLLHUP | POLLERR | POLLNVAL) != 0
}

impl Signals {
    pub fn install() -> io::Result<Signals> {
        let (rx, tx) = UnixStream::pair()?;
        rx.set_nonblocking(true)?;
        tx.set_nonblocking(true)?;
        WAKE_FD.store(tx.as_raw_fd(), Ordering::SeqCst);
        for s in [SIGHUP, SIGTERM, SIGWINCH] {
            // SAFETY: on_signal only touches atomics and write(2).
            unsafe {
                signal(s, on_signal);
            }
        }
        Ok(Signals { rx, tx })
    }

    /// The signals caught since the last call (the pipe drained).
    pub fn take(&self) -> Fired {
        let mut buf = [0u8; 64];
        while matches!((&self.rx).read(&mut buf), Ok(n) if n > 0) {}
        let f = FIRED.swap(0, Ordering::SeqCst);
        Fired { exit: f & ((1 << SIGHUP) | (1 << SIGTERM)) != 0, resize: f & (1 << SIGWINCH) != 0 }
    }

    /// Wait up to `timeout` for `fd`, the `extra` fds (the notice fd, the
    /// client's exit watch) or a signal → (`fd` has something to read (data,
    /// EOF or an error: the read tells which), which `extra` fds are ready).
    pub fn wait(&self, fd: RawFd, extra: [Option<RawFd>; 2], timeout: Duration) -> io::Result<(bool, [bool; 2])> {
        let mut fds = [
            PollFd { fd, events: POLLIN, revents: 0 },
            PollFd { fd: self.rx.as_raw_fd(), events: POLLIN, revents: 0 },
            // poll(2) skips a negative fd.
            PollFd { fd: extra[0].unwrap_or(-1), events: POLLIN, revents: 0 },
            PollFd { fd: extra[1].unwrap_or(-1), events: POLLIN, revents: 0 },
        ];
        poll_fds(&mut fds, timeout)?;
        Ok((ready(&fds[0]), [extra[0].is_some() && ready(&fds[2]), extra[1].is_some() && ready(&fds[3])]))
    }
}

impl Drop for Signals {
    /// The pipe closes with us: never let the handler write to a reused fd.
    fn drop(&mut self) {
        let _ = WAKE_FD.compare_exchange(self.tx.as_raw_fd(), -1, Ordering::SeqCst, Ordering::SeqCst);
    }
}

/// `fd` readable within `timeout` (zero: right now)?
pub fn readable(fd: RawFd, timeout: Duration) -> io::Result<bool> {
    let mut fds = [PollFd { fd, events: POLLIN, revents: 0 }];
    poll_fds(&mut fds, timeout)?;
    Ok(ready(&fds[0]))
}

/// `pid` is still a live process (kill(pid, 0): an EPERM still means alive).
pub fn alive(pid: i32) -> bool {
    extern "C" {
        fn kill(pid: i32, sig: i32) -> i32;
    }
    if pid <= 0 {
        return false;
    }
    // SAFETY: signal 0 only checks that the process exists.
    let ok = unsafe { kill(pid, 0) == 0 };
    ok || io::Error::last_os_error().raw_os_error() == Some(1)
}

/// A pollable fd that turns readable when one process exits: a kqueue with
/// EVFILT_PROC / NOTE_EXIT on it (macOS; kqueue fds work in poll(2)). Costs
/// nothing until the exit. The sidebar watches its tmux client with it.
#[derive(Debug)]
pub struct ProcWatch {
    pub pid: i32,
    #[cfg_attr(not(target_os = "macos"), allow(dead_code))]
    kq: RawFd,
}

#[cfg(target_os = "macos")]
mod kq {
    #[repr(C)]
    pub struct KEvent {
        pub ident: usize,
        pub filter: i16,
        pub flags: u16,
        pub fflags: u32,
        pub data: isize,
        pub udata: *mut std::ffi::c_void,
    }
    #[repr(C)]
    pub struct Timespec {
        pub sec: i64,
        pub nsec: i64,
    }
    pub const EVFILT_PROC: i16 = -5;
    pub const EV_ADD: u16 = 0x1;
    pub const EV_ONESHOT: u16 = 0x10;
    pub const NOTE_EXIT: u32 = 0x8000_0000;
    extern "C" {
        pub fn kqueue() -> i32;
        pub fn kevent(kq: i32, ch: *const KEvent, nch: i32, ev: *mut KEvent, nev: i32, t: *const Timespec) -> i32;
        pub fn close(fd: i32) -> i32;
    }
}

impl ProcWatch {
    /// Watch `pid`. None if it cannot be watched here, or is already gone
    /// (check [`alive`] to tell which).
    #[cfg(target_os = "macos")]
    pub fn new(pid: i32) -> Option<ProcWatch> {
        use kq::*;
        if pid <= 0 {
            return None;
        }
        // SAFETY: plain syscalls on a fresh kqueue and a stack-allocated event.
        unsafe {
            let fd = kqueue();
            if fd < 0 {
                return None;
            }
            let ev = KEvent {
                ident: pid as usize,
                filter: EVFILT_PROC,
                flags: EV_ADD | EV_ONESHOT,
                fflags: NOTE_EXIT,
                data: 0,
                udata: std::ptr::null_mut(),
            };
            let zero = Timespec { sec: 0, nsec: 0 };
            if kevent(fd, &ev, 1, std::ptr::null_mut(), 0, &zero) < 0 {
                close(fd);
                return None;
            }
            Some(ProcWatch { pid, kq: fd })
        }
    }

    #[cfg(not(target_os = "macos"))]
    pub fn new(_pid: i32) -> Option<ProcWatch> {
        None
    }

    /// The fd to poll.
    pub fn fd(&self) -> RawFd {
        self.kq
    }
}

impl Drop for ProcWatch {
    fn drop(&mut self) {
        #[cfg(target_os = "macos")]
        // SAFETY: our own kqueue fd, closed once.
        unsafe {
            kq::close(self.kq);
        }
    }
}

/// One raw read of stdin (unbuffered: poll must see everything not yet
/// read) → Some(bytes), or None when the terminal is gone (EOF).
pub fn read_stdin() -> io::Result<Option<Vec<u8>>> {
    // SAFETY: fd 0 stays open for the process's life; ManuallyDrop never closes it.
    let mut f = ManuallyDrop::new(unsafe { File::from_raw_fd(0) });
    let mut buf = [0u8; 1024];
    loop {
        match f.read(&mut buf) {
            Ok(0) => return Ok(None),
            Ok(n) => return Ok(Some(buf[..n].to_vec())),
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => return Ok(Some(Vec::new())),
            Err(e) => return Err(e),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn signals_wake_the_wait() {
        let s = Signals::install().unwrap();
        let (a, _b) = UnixStream::pair().unwrap();
        // Nothing ready: times out.
        assert_eq!(s.wait(a.as_raw_fd(), [None, None], Duration::from_millis(10)).unwrap(), (false, [false, false]));
        // An extra fd wakes it, and is told apart from the others.
        let (c, d) = UnixStream::pair().unwrap();
        let (e, _f) = UnixStream::pair().unwrap();
        (&d).write_all(b"x").unwrap();
        assert_eq!(s.wait(a.as_raw_fd(), [Some(e.as_raw_fd()), Some(c.as_raw_fd())], Duration::from_secs(2)).unwrap(),
            (false, [false, true]));
        // SIGWINCH to ourselves: the wait returns early and reports it.
        extern "C" {
            fn raise(sig: i32) -> i32;
        }
        // SAFETY: SIGWINCH has our handler installed above.
        unsafe { raise(SIGWINCH) };
        let t = std::time::Instant::now();
        let _ = s.wait(a.as_raw_fd(), [None, None], Duration::from_secs(5)).unwrap();
        assert!(t.elapsed() < Duration::from_secs(2));
        assert_eq!(s.take(), Fired { exit: false, resize: true });
        assert_eq!(s.take(), Fired::default());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn a_watched_process_exit_wakes_poll() {
        let mut child = std::process::Command::new("/bin/sleep").arg("0.3").spawn().unwrap();
        let pid = child.id() as i32;
        assert!(alive(pid));
        let w = ProcWatch::new(pid).expect("watch");
        assert!(!readable(w.fd(), Duration::from_millis(20)).unwrap());
        let t = std::time::Instant::now();
        assert!(readable(w.fd(), Duration::from_secs(5)).unwrap());
        assert!(t.elapsed() < Duration::from_secs(2));
        child.wait().unwrap();
        assert!(!alive(pid));
        // Gone already: no watch (the caller checks `alive`).
        assert!(ProcWatch::new(pid).is_none());
        assert!(!alive(0) && !alive(-1));
    }
}
