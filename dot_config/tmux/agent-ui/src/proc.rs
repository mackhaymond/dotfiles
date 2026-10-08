//! Subprocesses with a deadline. std has no `Command::output` timeout, and
//! every fork here (tmux, git, ps, wezterm, bash) must never hang a frame or an
//! action forever: the Python passes `timeout=` to every `subprocess.run`.

use std::io::{Read, Write};
use std::process::{Command, ExitStatus, Stdio};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

/// What a finished command left behind.
#[derive(Debug, Clone)]
pub struct Output {
    pub status: ExitStatus,
    pub stdout: String,
    pub stderr: String,
}

impl Output {
    pub fn ok(&self) -> bool {
        self.status.success()
    }
}

/// Why a command produced no [`Output`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RunError {
    /// Could not start (missing binary, fork failure); the OS error text.
    Spawn(String),
    /// Still running at the deadline; it was killed.
    Timeout,
}

impl std::fmt::Display for RunError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RunError::Spawn(e) => write!(f, "{e}"),
            RunError::Timeout => write!(f, "timed out"),
        }
    }
}

extern "C" {
    fn killpg(pgrp: i32, sig: i32) -> i32;
    fn setsid() -> i32;
}
const SIGKILL: i32 = 9;

/// A pipe drained on its own thread into a shared buffer, so whatever has
/// arrived can be taken at any moment (even while a grandchild still holds
/// the pipe open), and `done` says whether it reached EOF.
struct Drain {
    buf: Arc<Mutex<Vec<u8>>>,
    done: mpsc::Receiver<()>,
}

impl Drain {
    fn start<R: Read + Send + 'static>(r: Option<R>) -> Drain {
        let buf = Arc::new(Mutex::new(Vec::new()));
        let (tx, done) = mpsc::channel();
        let b = buf.clone();
        thread::spawn(move || {
            if let Some(mut r) = r {
                let mut chunk = [0u8; 8192];
                loop {
                    match r.read(&mut chunk) {
                        Ok(0) | Err(_) => break,
                        Ok(n) => b.lock().map(|mut v| v.extend_from_slice(&chunk[..n])).unwrap_or(()),
                    }
                }
            }
            let _ = tx.send(());
        });
        Drain { buf, done }
    }

    /// Wait for EOF until `deadline`; true if it came.
    fn wait(&self, deadline: Instant) -> bool {
        let left = deadline.saturating_duration_since(Instant::now());
        self.done.recv_timeout(left).is_ok()
    }

    fn take(&self) -> String {
        let v = self.buf.lock().map(|v| v.clone()).unwrap_or_default();
        String::from_utf8_lossy(&v).into_owned()
    }
}

/// Run `cmd` to completion or `timeout`, whichever comes first, feeding it
/// `input` on stdin (none: /dev/null). Output is decoded lossily.
///
/// The deadline holds for the whole call, not just the child's exit: the
/// child runs in its own process group, and when the deadline passes with
/// the child still running (→ [`RunError::Timeout`]) or with its pipes still
/// held open by something it left behind (`sh -c 'sleep 9 & echo hi'`), the
/// whole group is SIGKILLed. In the second case the child's own status and
/// whatever output arrived are returned. stdout/stderr are drained on their
/// own threads, so a chatty child can never block on a full pipe.
pub fn run(mut cmd: Command, input: Option<&[u8]>, timeout: Duration) -> Result<Output, RunError> {
    use std::os::unix::process::CommandExt;
    cmd.stdin(if input.is_some() { Stdio::piped() } else { Stdio::null() })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    let deadline = Instant::now() + timeout;
    let mut child = cmd.spawn().map_err(|e| RunError::Spawn(e.to_string()))?;
    let pgid = child.id() as i32;
    if let (Some(data), Some(mut stdin)) = (input, child.stdin.take()) {
        let data = data.to_vec();
        thread::spawn(move || {
            let _ = stdin.write_all(&data);
        });
    }
    let out = Drain::start(child.stdout.take());
    let err = Drain::start(child.stderr.take());
    let kill_group = || {
        // SAFETY: plain syscall; pgid is our child's own group (process_group(0)).
        unsafe { killpg(pgid, SIGKILL) };
    };
    let mut sleep = Duration::from_millis(1);
    let status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break st,
            Ok(None) if Instant::now() >= deadline => {
                kill_group();
                let _ = child.kill();
                let _ = child.wait();
                return Err(RunError::Timeout);
            }
            Ok(None) => {
                thread::sleep(sleep);
                sleep = (sleep * 2).min(Duration::from_millis(20));
            }
            Err(e) => return Err(RunError::Spawn(e.to_string())),
        }
    };
    let closed = out.wait(deadline) && err.wait(deadline);
    if !closed {
        // Something the child left in its group still holds a pipe.
        kill_group();
        let grace = Instant::now() + Duration::from_millis(100);
        out.wait(grace);
        err.wait(grace);
    }
    Ok(Output { status, stdout: out.take(), stderr: err.take() })
}

/// Start `cmd` in a new session (`setsid`, the Python's
/// `start_new_session=True`), no stdio, for a child that may outlive the
/// caller (display-popup -E holds its client until the popup closes).
/// The CALLER must reap it: `try_wait` it each tick (the sidebar keeps a
/// list), or a finished child stays a zombie. Prefer [`spawn_reaped`].
pub fn spawn_detached(mut cmd: Command) -> Result<std::process::Child, String> {
    use std::os::unix::process::CommandExt;
    cmd.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
    // SAFETY: setsid is async-signal-safe; nothing else runs between fork and exec.
    unsafe {
        cmd.pre_exec(|| {
            if setsid() < 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    cmd.spawn().map_err(|e| e.to_string())
}

/// [`spawn_detached`], reaped by a detached thread that waits on it: fire
/// and forget, no zombie. → the child's pid.
pub fn spawn_reaped(cmd: Command) -> Result<u32, String> {
    let mut child = spawn_detached(cmd)?;
    let pid = child.id();
    thread::spawn(move || {
        let _ = child.wait();
    });
    Ok(pid)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn output_and_timeout() {
        let mut c = Command::new("/bin/sh");
        c.args(["-c", "printf hi; printf err >&2; exit 3"]);
        let o = run(c, None, Duration::from_secs(5)).unwrap();
        assert_eq!((o.stdout.as_str(), o.stderr.as_str(), o.status.code()), ("hi", "err", Some(3)));
        let mut c = Command::new("/bin/sleep");
        c.arg("5");
        let t = Instant::now();
        assert_eq!(run(c, None, Duration::from_millis(100)).unwrap_err(), RunError::Timeout);
        assert!(t.elapsed() < Duration::from_secs(3));
        let mut c = Command::new("/bin/cat");
        let o = run(c, Some(b"abc"), Duration::from_secs(5)).unwrap();
        assert_eq!(o.stdout, "abc");
        c = Command::new("/nonexistent/binary");
        assert!(matches!(run(c, None, Duration::from_secs(1)), Err(RunError::Spawn(_))));
    }

    #[test]
    fn a_grandchild_holding_the_pipe_cannot_outlast_the_deadline() {
        let tag = format!("sleep 4.{}", std::process::id());
        let mut c = Command::new("/bin/sh");
        // The background sleep keeps stdout open after sh exits.
        c.args(["-c", &format!("{tag} & echo started")]);
        let t = Instant::now();
        let o = run(c, None, Duration::from_secs(1)).expect("sh itself exited");
        let took = t.elapsed();
        assert!(took >= Duration::from_millis(900) && took < Duration::from_millis(2500), "{took:?}");
        assert_eq!(o.stdout, "started\n");
        assert_eq!(o.status.code(), Some(0));
        // ...and the leftover was killed with its group.
        thread::sleep(Duration::from_millis(100));
        let ps = Command::new("pgrep").args(["-f", &tag]).output().unwrap();
        assert!(String::from_utf8_lossy(&ps.stdout).trim().is_empty(), "leftover sleep survived");
    }

    #[test]
    fn detached_children_get_their_own_session_and_are_reaped() {
        let mut child = spawn_detached({
            let mut c = Command::new("/bin/sh");
            c.args(["-c", "exit 0"]);
            c
        })
        .unwrap();
        assert!(child.wait().unwrap().success());
        let mut probe = Command::new("/bin/sh");
        let out = std::env::temp_dir().join(format!("agentui-sid-{}", std::process::id()));
        // After setsid the child leads its own session AND process group.
        probe.args(["-c", &format!("ps -o pgid= -p $$ > '{}'", out.display())]);
        let pid = spawn_reaped(probe).unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        while !std::fs::read_to_string(&out).is_ok_and(|s| !s.trim().is_empty()) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(20));
        }
        let got = std::fs::read_to_string(&out).unwrap_or_default();
        let _ = std::fs::remove_file(&out);
        let pgid: Vec<&str> = got.split_whitespace().collect();
        assert_eq!(pgid.last().copied(), Some(pid.to_string().as_str()), "not its own group/session leader: {got}");
        // Reaped: no zombie left with that pid once it exits.
        thread::sleep(Duration::from_millis(200));
        let st = Command::new("ps").args(["-o", "stat=", "-p", &pid.to_string()]).output().unwrap();
        assert!(!String::from_utf8_lossy(&st.stdout).contains('Z'), "zombie left");
    }
}
