//! Git facts for a space: the branch (pure file reads, agent-roster.py
//! `git_head`), and ahead/behind + dirty counts (a background `git status
//! --porcelain=v2 --branch`). Neither can stall a frame.
//!
//! - [`git_head`] mirrors the Python exactly: walk up to the first `.git`; a
//!   directory is the git dir, a FILE (worktree, submodule) says `gitdir:
//!   <path>`, relative to the directory holding it; HEAD and the `.git` file
//!   are read only if regular files ([`read_small`], so a FIFO can never block
//!   open()); nothing at or under [`REMOTE_PREFIXES`] is ever touched.
//! - [`GitCache::branch`] mirrors `Strip.branch` / `cache_branch`: cached
//!   BRANCH_TTL per cwd; a miss reads on a thread the caller waits on for at
//!   most BRANCH_WAIT; a slower read finishes alone and that cwd shows its last
//!   branch (or none) for BRANCH_SLOW_TTL.
//! - [`GitCache::status`] is new (the Python had no ahead/behind): it never
//!   waits. A miss returns the last value (or None) and starts one background
//!   job per repo; results are cached per repo root for STATUS_TTL. git runs
//!   with `--no-optional-locks` (it must never take index.lock under the
//!   user's feet) and is killed after STATUS_TIMEOUT; a repo that timed out is
//!   left alone for STATUS_SLOW_TTL.

use crate::proc;
use std::collections::{HashMap, HashSet};
use std::fs::OpenOptions;
use std::io::Read;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Component, Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

/// Where a stat can hang for a network timeout (SMB/AFP/NFS, autofs): no git
/// read at all under these (agent-roster.py `REMOTE_PREFIXES`).
pub const REMOTE_PREFIXES: [&str; 5] = ["/Volumes/", "/Network/", "/net/", "/home/", "/System/Volumes/Data/home/"];
/// agent-roster.py BRANCH_TTL / BRANCH_SLOW_TTL / BRANCH_WAIT.
pub const BRANCH_TTL: f64 = 30.0;
pub const BRANCH_SLOW_TTL: f64 = 600.0;
pub const BRANCH_WAIT: Duration = Duration::from_millis(50);
/// `git status` results are reused this long per repo.
pub const STATUS_TTL: f64 = 10.0;
/// A `git status` still running after this is killed...
pub const STATUS_TIMEOUT: Duration = Duration::from_secs(3);
/// ...and its repo is not asked again for this long.
pub const STATUS_SLOW_TTL: f64 = 120.0;
/// How long a cwd → repo-root lookup is trusted.
pub const ROOT_TTL: f64 = 30.0;

/// agent-roster.py `maybe_remote`: true at or under a REMOTE_PREFIXES path
/// (`/Volumes` itself too).
pub fn maybe_remote(path: &str) -> bool {
    let p = format!("{}/", path.trim_end_matches('/'));
    REMOTE_PREFIXES.iter().any(|r| p.starts_with(r))
}

#[cfg(target_os = "macos")]
const O_NONBLOCK: i32 = 0x0004;
#[cfg(not(target_os = "macos"))]
const O_NONBLOCK: i32 = 0o4000;

/// agent-roster.py `read_small`: the first line (trimmed) of a REGULAR file,
/// read without blocking; None for anything else. O_NONBLOCK plus the
/// regular-file check on the OPEN fd, so a FIFO named HEAD is refused and a
/// swap in between cannot slip past. Errors (missing, NUL in path) → Err.
pub fn read_small(path: &Path) -> std::io::Result<Option<String>> {
    let mut f = OpenOptions::new().read(true).custom_flags(O_NONBLOCK).open(path)?;
    if !f.metadata()?.is_file() {
        return Ok(None);
    }
    let mut buf = vec![0u8; 4096];
    let n = f.read(&mut buf)?;
    let text = String::from_utf8_lossy(&buf[..n]);
    Ok(Some(text.split('\n').next().unwrap_or("").trim().to_string()))
}

/// Python `os.path.normpath` for an absolute path: `.` and `..` resolved lexically.
fn normpath(p: &Path) -> PathBuf {
    let mut out = PathBuf::from("/");
    for c in p.components() {
        match c {
            Component::ParentDir => {
                out.pop();
            }
            Component::Normal(s) => out.push(s),
            _ => {}
        }
    }
    out
}

/// Where the repo holding `cwd` lives → (work tree dir, git dir), or None.
/// The same walk as `git_head` (and the same refusals).
pub fn git_dirs(cwd: &str) -> Option<(PathBuf, PathBuf)> {
    if cwd.is_empty() || maybe_remote(cwd) {
        return None;
    }
    let mut d = PathBuf::from(cwd);
    for _ in 0..64 {
        if !d.is_absolute() {
            return None;
        }
        let dot = d.join(".git");
        if dot.is_dir() {
            return Some((d, dot));
        } else if dot.is_file() {
            let first = read_small(&dot).ok()??;
            let rest = first.strip_prefix("gitdir:")?.trim();
            let mut gitdir = PathBuf::from(rest);
            if !gitdir.is_absolute() {
                gitdir = normpath(&d.join(gitdir));
            }
            if maybe_remote(&gitdir.to_string_lossy()) {
                return None;
            }
            return Some((d, gitdir));
        }
        let up = d.parent()?.to_path_buf();
        if up == d {
            return None;
        }
        d = up;
    }
    None
}

/// agent-roster.py `git_head`: the branch checked out at `cwd`, the short
/// (7-char) sha when detached, a non-`refs/heads/` ref as written, or "".
pub fn git_head(cwd: &str) -> String {
    let Some((_, gitdir)) = git_dirs(cwd) else { return String::new() };
    let head = match read_small(&gitdir.join("HEAD")) {
        Ok(Some(h)) => h,
        _ => return String::new(),
    };
    if let Some(r) = head.strip_prefix("ref:") {
        let r = r.trim();
        return r.strip_prefix("refs/heads/").unwrap_or(r).to_string();
    }
    head.chars().take(7).collect()
}

/// What `git status --porcelain=v2 --branch` says about a repo.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct GitStatus {
    /// `# branch.head`: the branch, or "(detached)".
    pub head: String,
    /// `# branch.upstream`, if any.
    pub upstream: Option<String>,
    /// `# branch.ab +A -B` (0 without an upstream).
    pub ahead: u32,
    pub behind: u32,
    /// Tracked entries with changes: `1`, `2` (renames) and `u` (unmerged) lines.
    pub changed: usize,
    /// `?` lines.
    pub untracked: usize,
}

impl GitStatus {
    /// The dirty count a space shows: changed + untracked.
    pub fn dirty(&self) -> usize {
        self.changed + self.untracked
    }

    /// Parse porcelain v2 output (NUL-free, `\n` separated).
    pub fn parse(text: &str) -> GitStatus {
        let mut s = GitStatus::default();
        for l in text.lines() {
            if let Some(h) = l.strip_prefix("# branch.head ") {
                s.head = h.to_string();
            } else if let Some(u) = l.strip_prefix("# branch.upstream ") {
                s.upstream = Some(u.to_string());
            } else if let Some(ab) = l.strip_prefix("# branch.ab ") {
                for part in ab.split(' ') {
                    if let Some(n) = part.strip_prefix('+') {
                        s.ahead = n.parse().unwrap_or(0);
                    } else if let Some(n) = part.strip_prefix('-') {
                        s.behind = n.parse().unwrap_or(0);
                    }
                }
            } else if l.starts_with("1 ") || l.starts_with("2 ") || l.starts_with("u ") {
                s.changed += 1;
            } else if l.starts_with("? ") {
                s.untracked += 1;
            }
        }
        s
    }
}

/// Run `git status` in `root` with the timeout → Some(status), or None on a
/// timeout / failure (not a repo any more, git missing).
pub fn run_git_status(root: &Path, timeout: Duration) -> Option<GitStatus> {
    let mut c = Command::new("git");
    c.arg("--no-optional-locks")
        .arg("-C")
        .arg(root)
        .args(["status", "--porcelain=v2", "--branch"])
        .env("GIT_OPTIONAL_LOCKS", "0")
        .env("GIT_TERMINAL_PROMPT", "0");
    match proc::run(c, None, timeout) {
        Ok(o) if o.ok() => Some(GitStatus::parse(&o.stdout)),
        _ => None,
    }
}

#[derive(Default)]
struct Shared {
    /// cwd → (looked up at, repo work tree)
    roots: HashMap<String, (f64, Option<PathBuf>)>,
    /// repo root → (fetched at, status, ttl)
    status: HashMap<PathBuf, (f64, Option<GitStatus>, f64)>,
    busy_cwds: HashSet<String>,
    busy_roots: HashSet<PathBuf>,
}

type BranchRead = (JoinHandle<()>, Arc<Mutex<Option<String>>>);

/// Record one `git status` result for `root` (both job paths use this). A
/// failure (timeout, git error) keeps showing the last good answer and backs
/// off for STATUS_SLOW_TTL.
fn store_status(shared: &Mutex<Shared>, root: PathBuf, st: Option<GitStatus>, now: f64, ok_ttl: f64) {
    if let Ok(mut sh) = shared.lock() {
        let ttl = if st.is_some() { ok_ttl } else { STATUS_SLOW_TTL };
        let keep = st.or_else(|| sh.status.get(&root).and_then(|c| c.1.clone()));
        sh.busy_roots.remove(&root);
        sh.status.insert(root, (now, keep, ttl));
    }
}

/// Branch and status caches for the spaces list. One per UI process; call it
/// from the UI thread (it spawns its own workers).
pub struct GitCache {
    branches: HashMap<String, (f64, String, f64)>,
    branch_reads: HashMap<String, BranchRead>,
    shared: Arc<Mutex<Shared>>,
    /// Overridable for tests.
    pub status_timeout: Duration,
    pub branch_wait: Duration,
    /// How long a good `git status` is reused (default STATUS_TTL; the
    /// always-on sidebar uses longer).
    pub status_ttl: f64,
}

impl Default for GitCache {
    fn default() -> Self {
        GitCache {
            branches: HashMap::new(),
            branch_reads: HashMap::new(),
            shared: Arc::new(Mutex::new(Shared::default())),
            status_timeout: STATUS_TIMEOUT,
            branch_wait: BRANCH_WAIT,
            status_ttl: STATUS_TTL,
        }
    }
}

impl GitCache {
    pub fn new() -> GitCache {
        GitCache::default()
    }

    /// Strip.branch: `git_head(cwd)`, cached, never stalling the caller more
    /// than `branch_wait` (see module docs).
    pub fn branch(&mut self, cwd: &str, now: f64) -> String {
        if cwd.is_empty() || maybe_remote(cwd) {
            return String::new();
        }
        if let Some((h, _)) = self.branch_reads.get(cwd) {
            if h.is_finished() {
                let (_, boxed) = self.branch_reads.remove(cwd).unwrap();
                let got = boxed.lock().ok().and_then(|g| g.clone());
                if let Some(b) = got {
                    self.cache_branch(cwd, now, b, BRANCH_TTL);
                }
            }
        }
        let hit = self.branches.get(cwd).cloned();
        if let Some((at, b, ttl)) = &hit {
            if now - at >= 0.0 && now - at < *ttl {
                return b.clone();
            }
        }
        if self.branch_reads.contains_key(cwd) {
            return hit.map(|h| h.1).unwrap_or_default();
        }
        let boxed = Arc::new(Mutex::new(None));
        let (b2, c2) = (boxed.clone(), cwd.to_string());
        let h = thread::spawn(move || {
            let b = git_head(&c2);
            if let Ok(mut g) = b2.lock() {
                *g = Some(b);
            }
        });
        let deadline = Instant::now() + self.branch_wait;
        while !h.is_finished() && Instant::now() < deadline {
            thread::sleep(Duration::from_micros(200));
        }
        let got = if h.is_finished() { boxed.lock().ok().and_then(|g| g.clone()) } else { None };
        if let Some(b) = got {
            return self.cache_branch(cwd, now, b, BRANCH_TTL);
        }
        self.branch_reads.insert(cwd.to_string(), (h, boxed));
        let last = hit.map(|h| h.1).unwrap_or_default();
        self.cache_branch(cwd, now, last, BRANCH_SLOW_TTL)
    }

    /// cache_branch: store one entry, dropping every expired one.
    fn cache_branch(&mut self, cwd: &str, now: f64, branch: String, ttl: f64) -> String {
        self.branches.retain(|_, (at, _, t)| now - *at >= 0.0 && now - *at < *t);
        self.branches.insert(cwd.to_string(), (now, branch.clone(), ttl));
        branch
    }

    /// The last known `git status` of the repo holding `cwd`, never waiting.
    /// Starts (at most one per cwd/repo) background refresh when stale.
    pub fn status(&self, cwd: &str, now: f64) -> Option<GitStatus> {
        if cwd.is_empty() || maybe_remote(cwd) {
            return None;
        }
        let mut sh = self.shared.lock().ok()?;
        let root = sh.roots.get(cwd).cloned();
        let (root_fresh, root) = match root {
            Some((at, r)) => (now - at >= 0.0 && now - at < ROOT_TTL, r),
            None => (false, None),
        };
        let cached = root.as_ref().and_then(|r| sh.status.get(r).cloned());
        let status_fresh = cached.as_ref().is_some_and(|(at, _, ttl)| now - at >= 0.0 && now - at < *ttl);
        let last = cached.and_then(|c| c.1);
        if root_fresh && (root.is_none() || status_fresh) {
            return last;
        }
        if root_fresh {
            let r = root.unwrap();
            if !sh.busy_roots.contains(&r) {
                sh.busy_roots.insert(r.clone());
                drop(sh);
                self.spawn_status(r, now);
            }
        } else if !sh.busy_cwds.contains(cwd) {
            sh.busy_cwds.insert(cwd.to_string());
            drop(sh);
            self.spawn_lookup(cwd.to_string(), now);
        }
        last
    }

    fn spawn_status(&self, root: PathBuf, now: f64) {
        let shared = self.shared.clone();
        let (timeout, ttl) = (self.status_timeout, self.status_ttl);
        thread::spawn(move || {
            let st = run_git_status(&root, timeout);
            store_status(&shared, root, st, now, ttl);
        });
    }

    fn spawn_lookup(&self, cwd: String, now: f64) {
        let shared = self.shared.clone();
        let (timeout, ttl) = (self.status_timeout, self.status_ttl);
        thread::spawn(move || {
            let root = git_dirs(&cwd).map(|(wt, _)| wt);
            let mut run = None;
            if let Ok(mut sh) = shared.lock() {
                sh.roots.retain(|_, (at, _)| now - *at >= 0.0 && now - *at < ROOT_TTL);
                sh.roots.insert(cwd.clone(), (now, root.clone()));
                sh.busy_cwds.remove(&cwd);
                if let Some(r) = &root {
                    let fresh = sh.status.get(r).is_some_and(|(at, _, ttl)| now - at >= 0.0 && now - at < *ttl);
                    if !fresh && !sh.busy_roots.contains(r) {
                        sh.busy_roots.insert(r.clone());
                        run = Some(r.clone());
                    }
                }
            }
            if let Some(r) = run {
                let st = run_git_status(&r, timeout);
                store_status(&shared, r, st, now, ttl);
            }
        });
    }

    /// True while any status job runs.
    pub fn busy(&self) -> bool {
        self.shared.lock().map(|s| !s.busy_cwds.is_empty() || !s.busy_roots.is_empty()).unwrap_or(false)
    }

    /// Wait (up to `timeout`) for running status jobs: for one-shot callers
    /// like `agent-ui dump`, never for a UI frame.
    pub fn settle(&self, timeout: Duration) {
        let deadline = Instant::now() + timeout;
        while self.busy() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn tmpdir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("agent-ui-git-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        fs::canonicalize(d).unwrap()
    }

    #[test]
    fn remote_prefixes() {
        assert!(maybe_remote("/Volumes"));
        assert!(maybe_remote("/Volumes/share/x"));
        assert!(maybe_remote("/net/host"));
        assert!(!maybe_remote("/Users/me"));
        assert!(!maybe_remote("/Volumesx"));
        assert!(!maybe_remote(""));
    }

    #[test]
    fn heads() {
        let d = tmpdir("heads");
        // A branch, read from a subdirectory.
        fs::create_dir_all(d.join("repo/.git")).unwrap();
        fs::create_dir_all(d.join("repo/src/deep")).unwrap();
        fs::write(d.join("repo/.git/HEAD"), "ref: refs/heads/feature/x\n").unwrap();
        assert_eq!(git_head(d.join("repo/src/deep").to_str().unwrap()), "feature/x");
        // Detached: the short sha.
        fs::write(d.join("repo/.git/HEAD"), "0123456789abcdef0123\n").unwrap();
        assert_eq!(git_head(d.join("repo").to_str().unwrap()), "0123456");
        // A ref outside refs/heads is shown as written.
        fs::write(d.join("repo/.git/HEAD"), "ref: refs/remotes/origin/main").unwrap();
        assert_eq!(git_head(d.join("repo").to_str().unwrap()), "refs/remotes/origin/main");
        // Worktree: a .git FILE with a relative gitdir.
        fs::create_dir_all(d.join("repo/.git/worktrees/wt")).unwrap();
        fs::write(d.join("repo/.git/worktrees/wt/HEAD"), "ref: refs/heads/wt-branch\n").unwrap();
        fs::create_dir_all(d.join("wt/sub")).unwrap();
        fs::write(d.join("wt/.git"), "gitdir: ../repo/.git/worktrees/wt\n").unwrap();
        assert_eq!(git_head(d.join("wt/sub").to_str().unwrap()), "wt-branch");
        assert_eq!(git_dirs(d.join("wt/sub").to_str().unwrap()).unwrap().0, d.join("wt"));
        // A .git file that says something else: nothing.
        fs::create_dir_all(d.join("bad")).unwrap();
        fs::write(d.join("bad/.git"), "nonsense\n").unwrap();
        assert_eq!(git_head(d.join("bad").to_str().unwrap()), "");
        // A gitdir pointing at nothing: nothing.
        fs::create_dir_all(d.join("gone")).unwrap();
        fs::write(d.join("gone/.git"), "gitdir: /nonexistent/x\n").unwrap();
        assert_eq!(git_head(d.join("gone").to_str().unwrap()), "");
        // Missing repo, relative and empty paths.
        assert_eq!(git_head("relative/path"), "");
        assert_eq!(git_head(""), "");
        assert_eq!(git_head("/Volumes/whatever"), "");
        let _ = fs::remove_dir_all(&d);
    }

    #[test]
    fn fifo_head_never_blocks() {
        let d = tmpdir("fifo");
        fs::create_dir_all(d.join("r/.git")).unwrap();
        let fifo = d.join("r/.git/HEAD");
        let st = Command::new("mkfifo").arg(&fifo).status().unwrap();
        assert!(st.success());
        let t = Instant::now();
        assert_eq!(git_head(d.join("r").to_str().unwrap()), "");
        assert!(t.elapsed() < Duration::from_secs(1));
        let _ = fs::remove_dir_all(&d);
    }

    #[test]
    fn porcelain() {
        let s = GitStatus::parse(
            "# branch.oid abc\n# branch.head main\n# branch.upstream origin/main\n# branch.ab +2 -1\n\
             1 .M N... 100644 100644 100644 a b f1\n2 R. N... 100644 100644 100644 a b R100 new\told\n\
             u UU N... 1 2 3 4 a b c f3\n? new.txt\n! ignored\n",
        );
        assert_eq!(s.head, "main");
        assert_eq!(s.upstream.as_deref(), Some("origin/main"));
        assert_eq!((s.ahead, s.behind, s.changed, s.untracked, s.dirty()), (2, 1, 3, 1, 4));
        let s = GitStatus::parse("# branch.oid (initial)\n# branch.head (detached)\n");
        assert_eq!((s.head.as_str(), s.upstream.clone(), s.ahead, s.dirty()), ("(detached)", None, 0, 0));
    }

    #[test]
    fn cache_branch_and_status() {
        let d = tmpdir("cache");
        let ok = Command::new("git").args(["init", "-q", "-b", "trunk"]).current_dir(&d).status();
        if !ok.map(|s| s.success()).unwrap_or(false) {
            return; // no git here
        }
        fs::write(d.join("a.txt"), "x").unwrap();
        let mut g = GitCache::new();
        let cwd = d.to_str().unwrap();
        g.branch_wait = Duration::from_secs(2);
        assert_eq!(g.branch(cwd, 1000.0), "trunk");
        // Cached: a HEAD change is not seen within the TTL.
        fs::write(d.join(".git/HEAD"), "ref: refs/heads/other\n").unwrap();
        assert_eq!(g.branch(cwd, 1010.0), "trunk");
        assert_eq!(g.branch(cwd, 1000.0 + BRANCH_TTL + 1.0), "other");
        fs::write(d.join(".git/HEAD"), "ref: refs/heads/trunk\n").unwrap();
        // Status: the first call never waits and has nothing yet.
        assert_eq!(g.status(cwd, 1000.0), None);
        g.settle(Duration::from_secs(10));
        let st = g.status(cwd, 1001.0).expect("status after settle");
        assert_eq!(st.head, "trunk");
        assert_eq!(st.untracked, 1);
        // The lookup path (root expired) with git now failing keeps the last answer.
        fs::write(d.join(".git/HEAD"), "garbage\n").unwrap();
        let later = 1001.0 + ROOT_TTL + 1.0;
        assert_eq!(g.status(cwd, later).map(|s| s.head), Some("trunk".into()));
        g.settle(Duration::from_secs(10));
        assert_eq!(g.status(cwd, later + 1.0).map(|s| s.head), Some("trunk".into()), "a failed run wiped the cache");
        fs::write(d.join(".git/HEAD"), "ref: refs/heads/trunk\n").unwrap();
        // Not a repo: None, and remembered.
        assert_eq!(g.status("/", 1000.0), None);
        g.settle(Duration::from_secs(10));
        assert_eq!(g.status("/", 1001.0), None);
        let _ = fs::remove_dir_all(&d);
    }
}
