//! ONE insight publisher per tmux server. Every WezTerm tab runs a sidebar
//! of its own; were each to run an [`Engine`](super::Engine) (each with its
//! own attribution history) and write the shared files, a fresh one would
//! overwrite what an older one counted, sweep what it wrote, and N tabs
//! would cost N engines. So the sidebars elect one ([`Board`]):
//!
//! - **The key.** A server is `<start_time>_<hash>` ([`server_key`]: its
//!   `#{start_time}` and an FNV-1a hash of its `#{socket_path}`, so two
//!   servers started in the same second still differ).
//! - **The lock.** `<dir>/<key>.lock` ([`lock_path`]; `<dir>`
//!   [`super::files_dir`]), held with `flock(LOCK_EX | LOCK_NB)` for the
//!   holder's life (the kernel lets it go when the process exits, however
//!   it exits). A sidebar that does not hold it tries again on every
//!   refresh, so one takes over when the holder is gone.
//! - **Taking over**: the new holder first removes whatever its key had
//!   published (`.gen` first: a dead holder's files are never trusted), and
//!   other servers' leftovers ([`sweep_others`]); it republishes on its
//!   first result.
//! - **The holder** runs the engine ([`Insights`]) and, after each result:
//!   the per-window `.files` ([`Published`]; its own stale-file sweep only
//!   once a COMPLETE pass landed, [`Engine::complete`](super::Engine)), then
//!   `<key>.json` ([`json_path`]: every window's insight, [`encode`]), then
//!   `<key>.gen` ([`gen_path`]: `<pid> <generation> <complete>`, written
//!   last, so a reader that sees it sees the rest; `<complete>` is 1 once a
//!   complete pass landed since it took the lock), touches its lock file,
//!   and posts the insight notice ([`Board::with_notice`]) so the mirrors
//!   read it at once. All atomic (temp + rename), 0600. A write that fails
//!   (a full or read-only disk) ends its publishing: the lock let go, its
//!   own engine kept ("solo"). A worker that gave up (its engine panicked
//!   [`super::PANICS_MAX`] times, or never started) removes `.gen`, then
//!   lets the lock go, for good; so does a holder that exits cleanly
//!   ([`Drop`]).
//! - **The others** run no engine at all: they mirror `<key>.json` whenever
//!   `.gen` changes (the notice wakes them; else one tiny read per wake),
//!   so every sidebar's cards agree.
//! - **Readers** (`agent-ui diff`) trust the `.files` only while
//!   [`verified`]: the lock held by someone and `.gen` there, complete.
//! - No server known yet, or a folder that cannot be used: the sidebar runs
//!   its own engine and publishes nothing ("solo"). A FAILED snapshot (no
//!   client and no window: tmux did not answer) changes nothing: the last
//!   key stands, and the engine is not handed it (an empty snapshot would
//!   clear its attribution history).

use super::{fill, write_atomic, DiffStat, Insight, Insights, Kind, Published};
use crate::json::{self, Value};
use crate::tmux::Snapshot;
use crate::view::ViewModel;
use std::collections::BTreeMap;
use std::fs::{self, File};
use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

extern "C" {
    fn flock(fd: i32, op: i32) -> i32;
}
const LOCK_EX: i32 = 2;
const LOCK_NB: i32 = 4;

/// Another server's lock file goes once it was not held, nor touched by a
/// holder's publish, for this long; its temp files after [`TMP_AGE`].
const LOCK_AGE: Duration = Duration::from_secs(86_400);
const TMP_AGE: Duration = Duration::from_secs(60);

/// A server's key: `<start_time>_<8 hex: FNV-1a of socket_path>`.
pub fn server_key(start: &str, socket_path: &str) -> String {
    let mut h: u32 = 0x811c_9dc5;
    for b in socket_path.bytes() {
        h = (h ^ u32::from(b)).wrapping_mul(0x0100_0193);
    }
    format!("{}_{h:08x}", start.trim())
}

/// `<dir>/<key>.lock`: held by the publisher.
pub fn lock_path(dir: &Path, key: &str) -> PathBuf {
    dir.join(format!("{key}.lock"))
}

/// `<dir>/<key>.gen`: `<pid> <generation> <complete 0|1>`, the publisher's
/// last write.
pub fn gen_path(dir: &Path, key: &str) -> PathBuf {
    dir.join(format!("{key}.gen"))
}

/// `<dir>/<key>.json`: every window's insight ([`encode`]).
pub fn json_path(dir: &Path, key: &str) -> PathBuf {
    dir.join(format!("{key}.json"))
}

/// What a lock attempt found.
#[derive(Debug)]
pub enum Try {
    /// Ours now (held while the file stays open).
    Got(File),
    /// Another holds it.
    Busy,
    /// The folder or file cannot be used.
    Unavailable,
}

/// Try for `key`'s lock (the folder made 0700, the file 0600).
pub fn try_lock(dir: &Path, key: &str) -> Try {
    use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
    if fs::DirBuilder::new().recursive(true).mode(0o700).create(dir).is_err() {
        return Try::Unavailable;
    }
    let f = fs::OpenOptions::new().read(true).write(true).create(true).truncate(false).mode(0o600).open(lock_path(dir, key));
    let Ok(f) = f else { return Try::Unavailable };
    // SAFETY: a plain syscall on a descriptor this function owns.
    if unsafe { flock(f.as_raw_fd(), LOCK_EX | LOCK_NB) } == 0 {
        Try::Got(f)
    } else if std::io::Error::last_os_error().kind() == std::io::ErrorKind::WouldBlock {
        Try::Busy
    } else {
        Try::Unavailable
    }
}

/// Some process holds `key`'s lock now (a live publisher). Asks by trying
/// it (let go at once when it was free).
pub fn held(dir: &Path, key: &str) -> bool {
    let Ok(f) = File::open(lock_path(dir, key)) else { return false };
    // SAFETY: as in try_lock; closing `f` lets a lock taken here go.
    let got = unsafe { flock(f.as_raw_fd(), LOCK_EX | LOCK_NB) } == 0;
    !got && std::io::Error::last_os_error().kind() == std::io::ErrorKind::WouldBlock
}

/// The published files of `key` are a live publisher's and whole: its
/// `.gen` says a complete pass landed, and its lock is held.
pub fn verified(dir: &Path, key: &str) -> bool {
    let Ok(g) = fs::read_to_string(gen_path(dir, key)) else { return false };
    g.split_whitespace().nth(2) == Some("1") && held(dir, key)
}

/// The server key a file in the folder belongs to (`<key>.lock` / `.gen` /
/// `.json`, `<key>-<win>.files`).
fn key_of(name: &str) -> Option<&str> {
    if let Some(stem) = name.strip_suffix(".files") {
        return stem.split_once('-').map(|(k, _)| k);
    }
    [".lock", ".gen", ".json"].iter().find_map(|x| name.strip_suffix(x)).filter(|k| !k.starts_with('.'))
}

/// Remove what `key` published: `.gen` first (nothing verified from here
/// on), then `.json` and every `<key>-*.files`.
fn clear(dir: &Path, key: &str) {
    let _ = fs::remove_file(gen_path(dir, key));
    let _ = fs::remove_file(json_path(dir, key));
    let files = format!("{key}-");
    for e in fs::read_dir(dir).into_iter().flatten().flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        if name.starts_with(&files) && name.ends_with(".files") {
            let _ = fs::remove_file(e.path());
        }
    }
}

/// Other servers' leftovers (`mine` kept): every key whose lock nobody
/// holds loses its `.gen`, `.json` (prompt text) and `.files`; its lock
/// file too once [`LOCK_AGE`] old (a live holder touches it on every
/// publish, so a lock in use is never that old); temp files once
/// [`TMP_AGE`] old.
pub fn sweep_others(dir: &Path, mine: &str) {
    let old = |p: &Path, age: Duration| {
        fs::metadata(p).and_then(|m| m.modified()).ok().and_then(|t| SystemTime::now().duration_since(t).ok()).is_some_and(|d| d > age)
    };
    let mut keys: Vec<String> = Vec::new();
    for e in fs::read_dir(dir).into_iter().flatten().flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        if name.starts_with('.') && name.ends_with(".tmp") {
            if old(&e.path(), TMP_AGE) {
                let _ = fs::remove_file(e.path());
            }
        } else if let Some(k) = key_of(&name).filter(|k| *k != mine && !k.is_empty()) {
            if !keys.iter().any(|x| x == k) {
                keys.push(k.to_string());
            }
        }
    }
    for k in keys.iter().filter(|k| !held(dir, k)) {
        clear(dir, k);
        let lock = lock_path(dir, k);
        if old(&lock, LOCK_AGE) {
            let _ = fs::remove_file(lock);
        }
    }
}

fn insight_value(i: &Insight) -> Value {
    let diff = i.diff.map(|d| crate::obj![("added", d.added), ("removed", d.removed), ("files", d.files)]).unwrap_or(Value::Null);
    let dirty: Vec<String> = i.dirty_files.iter().map(|p| p.to_string_lossy().into_owned()).collect();
    crate::obj![
        ("kind", i.kind.name()),
        ("transcript", i.transcript.to_string_lossy().into_owned()),
        ("doing", i.doing.clone()),
        ("todo", i.todo.clone()),
        ("prompt", i.prompt.clone()),
        ("ctx_used", i.ctx_used),
        ("ctx_window", i.ctx_window),
        ("diff", diff),
        ("dirty_files", dirty),
        ("touched", i.touched),
    ]
}

fn insight_of(v: &Value) -> Option<Insight> {
    let s = |k: &str| v.get(k).and_then(Value::as_str).map(String::from);
    let n = |v: &Value, k: &str| v.get(k).and_then(Value::as_f64).map(|x| x.max(0.0) as u64);
    let kind = match v.get("kind")?.as_str()? {
        "codex" => Kind::Codex,
        _ => Kind::Claude,
    };
    let diff = v.get("diff").filter(|d| **d != Value::Null).map(|d| DiffStat {
        added: n(d, "added").unwrap_or(0),
        removed: n(d, "removed").unwrap_or(0),
        files: n(d, "files").unwrap_or(0) as usize,
    });
    let dirty = v.get("dirty_files").and_then(Value::as_array).unwrap_or(&[]).iter().filter_map(Value::as_str).map(PathBuf::from).collect();
    Some(Insight {
        kind,
        transcript: PathBuf::from(s("transcript").unwrap_or_default()),
        doing: s("doing"),
        todo: s("todo"),
        prompt: s("prompt"),
        ctx_used: n(v, "ctx_used"),
        ctx_window: n(v, "ctx_window").unwrap_or(super::WINDOW_200K),
        diff,
        dirty_files: dirty,
        touched: n(v, "touched").unwrap_or(0) as usize,
    })
}

/// `<server>.json`'s text: `{"generation": "<pid> <n>", "windows": {"@3":
/// {kind, transcript, doing, todo, prompt, ctx_used, ctx_window, diff:
/// {added, removed, files} | null, dirty_files: [...], touched}}}`.
pub fn encode(stamp: &str, map: &BTreeMap<String, Insight>) -> String {
    let wins = Value::Obj(map.iter().map(|(w, i)| (w.clone(), insight_value(i))).collect());
    crate::obj![("generation", stamp), ("windows", wins)].to_json()
}

/// [`encode`]'s text back (None: not that).
pub fn decode(text: &str) -> Option<BTreeMap<String, Insight>> {
    let v = json::parse(text).ok()?;
    let Some(Value::Obj(wins)) = v.get("windows") else { return None };
    Some(wins.iter().filter_map(|(w, i)| Some((w.clone(), insight_of(i)?))).collect())
}

/// One sidebar's place in the election (see the module docs).
pub struct Board {
    insights: Insights,
    dir: PathBuf,
    /// Insights at all (not [`Insights::off`] when made).
    enabled: bool,
    /// Posted after each publish ([`Board::with_notice`]).
    notice: Option<String>,
    /// This server's key, the last one known.
    key: Option<String>,
    /// The lock, while this sidebar publishes.
    lock: Option<File>,
    /// No election to be had: an engine of its own, nothing published.
    solo: bool,
    /// A publish failed to write: solo for this server's life.
    no_publish: bool,
    /// Its worker gave up: never the holder again (a mirror at most).
    gave_up: bool,
    published: Option<Published>,
    /// A complete pass landed since it holds the lock.
    complete_seen: bool,
    /// The engine's generation last taken.
    seen: u64,
    /// `.gen`'s text last mirrored.
    stamp: Option<String>,
    mirror: BTreeMap<String, Insight>,
    generation: u64,
}

impl Board {
    /// `insights` run while this sidebar publishes (or runs solo); the
    /// shared files under `dir`.
    pub fn new(insights: Insights, dir: PathBuf) -> Board {
        let enabled = insights.working();
        Board {
            insights,
            dir,
            enabled,
            notice: None,
            key: None,
            lock: None,
            solo: false,
            no_publish: false,
            gave_up: false,
            published: None,
            complete_seen: false,
            seen: 0,
            stamp: None,
            mirror: BTreeMap::new(),
            generation: 0,
        }
    }

    /// The Darwin notice (`notify(3)`) a publish posts, so the mirrors read
    /// it at once ([`crate::sidebar::notice::insight_name`]).
    pub fn with_notice(mut self, name: String) -> Board {
        self.notice = Some(name);
        self
    }

    /// It runs the engine now (the holder, or solo).
    pub fn runs(&self) -> bool {
        !self.gave_up && (self.lock.is_some() || self.solo)
    }

    /// It holds the lock (publishes).
    pub fn holder(&self) -> bool {
        self.lock.is_some()
    }

    /// The server key in use.
    pub fn key(&self) -> Option<&str> {
        self.key.as_deref()
    }

    /// A refresh with `snap` and the server's `key` (None: not known now,
    /// the last one stands): the election (the lock tried again while
    /// another holds it), the engine handed `snap` while this sidebar runs
    /// it and the snapshot did not fail, then [`Board::poll`].
    pub fn refresh(&mut self, snap: &Snapshot, key: Option<&str>) {
        if !self.enabled {
            return;
        }
        if let Some(k) = key.filter(|k| self.key.as_deref() != Some(*k)) {
            self.let_go();
            (self.key, self.solo, self.no_publish, self.stamp) = (Some(k.to_string()), false, false, None);
            self.mirror.clear();
            self.generation += 1;
        }
        if self.lock.is_none() && !self.gave_up {
            let was = self.runs();
            match self.key.clone().filter(|_| !self.no_publish) {
                Some(k) => match try_lock(&self.dir, &k) {
                    Try::Got(f) => self.take(f, &k),
                    Try::Busy => self.solo = false,
                    Try::Unavailable => self.solo = true,
                },
                None => self.solo = true,
            }
            if was != self.runs() {
                // (Its cards' source changed: the engine's, or the mirror.)
                self.generation += 1;
            }
        }
        // (A failed snapshot, tmux not answering, is never the engine's:
        // it would forget every window, and their history with them.)
        let failed = snap.clients.is_empty() && snap.windows.is_empty();
        if self.runs() && !failed {
            self.insights.refresh(snap);
        }
        self.poll();
    }

    /// The lock is ours: whatever the key had published goes first (a dead
    /// holder's files are never trusted), other servers' leftovers too; the
    /// first result republishes.
    fn take(&mut self, lock: File, key: &str) {
        clear(&self.dir, key);
        sweep_others(&self.dir, key);
        self.lock = Some(lock);
        self.published = Some(Published::new(self.dir.clone(), key.to_string()));
        (self.solo, self.complete_seen, self.stamp, self.seen) = (false, false, None, 0);
        self.mirror.clear();
    }

    /// The cheap check, every wake: the engine's new result (published
    /// when holding; a dead worker lets the lock go), or the holder's newer
    /// `.gen` mirrored. True when the cards' insight changed.
    pub fn poll(&mut self) -> bool {
        if !self.enabled {
            return false;
        }
        if self.runs() {
            if !self.insights.working() {
                // (Dead, or its thread never started.)
                self.give_up();
                return true;
            }
            let g = self.insights.generation();
            if g == self.seen {
                return false;
            }
            self.seen = g;
            self.generation += 1;
            if self.lock.is_some() {
                self.publish();
            }
            return true;
        }
        let Some(k) = self.key.as_deref() else { return false };
        let stamp = fs::read_to_string(gen_path(&self.dir, k)).ok();
        if stamp.is_none() || stamp == self.stamp {
            // (No publisher right now: the last mirror stands until one is.)
            return false;
        }
        let Some(map) = fs::read_to_string(json_path(&self.dir, k)).ok().and_then(|t| decode(&t)) else { return false };
        (self.stamp, self.mirror) = (stamp, map);
        self.generation += 1;
        true
    }

    /// The holder's writes after a result: the `.files`, `.json`, `.gen`
    /// (then the lock touched, the notice posted). A failed write ends its
    /// publishing.
    fn publish(&mut self) {
        let (Some(p), Some(k)) = (self.published.as_mut(), self.key.clone()) else { return };
        let (map, complete, g) = self.insights.result();
        self.complete_seen |= complete;
        p.sync(&map, self.complete_seen);
        let stamp = format!("{} {g} {}", std::process::id(), u8::from(self.complete_seen));
        let wrote = write_atomic(&json_path(&self.dir, &k), &encode(&stamp, &map)).and_then(|_| write_atomic(&gen_path(&self.dir, &k), &stamp));
        if wrote.is_err() {
            self.let_go();
            (self.no_publish, self.solo) = (true, true);
            return;
        }
        if let Some(l) = &self.lock {
            let _ = l.set_modified(SystemTime::now());
        }
        if let Some(n) = &self.notice {
            crate::sidebar::notice::post(n);
        }
    }

    /// The worker gave up: the lock let go, never taken again; the cards
    /// mirror whoever publishes next.
    fn give_up(&mut self) {
        self.gave_up = true;
        self.let_go();
        self.generation += 1;
    }

    /// Stop publishing: `.gen` removed FIRST (nothing verified from here
    /// on), then the lock let go.
    fn let_go(&mut self) {
        if self.lock.is_some() {
            if let Some(k) = self.key.as_deref() {
                let _ = fs::remove_file(gen_path(&self.dir, k));
            }
        }
        (self.lock, self.published) = (None, None);
    }

    /// Bumped whenever the cards' insight changed.
    pub fn generation(&self) -> u64 {
        self.generation
    }

    /// The engine holds a snapshot it has not answered (only while it runs).
    pub fn pending(&self) -> bool {
        self.runs() && self.insights.pending()
    }

    /// Every window's insight as this sidebar shows it.
    pub fn map(&self) -> BTreeMap<String, Insight> {
        if self.runs() {
            self.insights.map()
        } else {
            self.mirror.clone()
        }
    }

    /// Fill the cards' insight in.
    pub fn apply(&self, view: &mut ViewModel) {
        if self.enabled {
            fill(view, &self.map());
        }
    }

    /// Window `win`'s published file, when there is one now.
    pub fn files_of(&self, win: &str) -> Option<PathBuf> {
        let k = self.key.as_deref()?;
        Some(super::files_path_in(&self.dir, k, win)).filter(|p| p.is_file())
    }
}

impl Drop for Board {
    fn drop(&mut self) {
        self.let_go();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::insight::Engine;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use std::time::Instant;

    const K: &str = "4242_0badf00d";

    /// A snapshot with one client row (a server that answered).
    fn snap() -> Snapshot {
        let mut f: Vec<String> = ["", "client", "/dev/ttys999", "@9", "main", "on", "", "", "77"].map(String::from).to_vec();
        f.extend((0..crate::tmux::STATUS_FORMATS.len()).map(|_| String::new()));
        *f.last_mut().unwrap() = "4242".to_string();
        Snapshot::parse(&f.join("\x1f"))
    }

    /// Insights whose engines are counted in `made` (reading nothing; each
    /// made after `delay`).
    fn counted(made: &Arc<AtomicUsize>, delay: Duration) -> Insights {
        let made = made.clone();
        let mut i = Insights::with(move || {
            std::thread::sleep(delay);
            made.fetch_add(1, Ordering::SeqCst);
            let none = PathBuf::from("/nonexistent");
            Engine::new(none.clone(), none.clone(), none, Box::new(crate::pins::LiveProcs))
        });
        i.every = Duration::ZERO;
        i
    }

    /// `f` true within 2 s. (A lock let go can linger an instant: a child
    /// another test forks shares the descriptor until it execs.)
    fn eventually(mut f: impl FnMut() -> bool) -> bool {
        let t0 = Instant::now();
        while !f() {
            if t0.elapsed() > Duration::from_secs(2) {
                return false;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        true
    }

    fn settle(b: &mut Board) {
        let t0 = Instant::now();
        while b.pending() && t0.elapsed() < Duration::from_secs(5) {
            std::thread::sleep(Duration::from_millis(5));
        }
        b.poll();
    }

    fn dir(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("agentui-board-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }

    fn one() -> Insight {
        Insight {
            kind: Kind::Codex,
            transcript: "/t/r.jsonl".into(),
            doing: Some("Porting".into()),
            todo: Some("Porting".into()),
            prompt: Some("port \"it\"".into()),
            ctx_used: Some(850_000),
            ctx_window: 1_000_000,
            diff: Some(DiffStat { added: 48, removed: 12, files: 3 }),
            dirty_files: vec!["/r/a.rs".into()],
            touched: 4,
        }
    }

    #[test]
    fn keys_and_the_json() {
        let map: BTreeMap<String, Insight> = [("@3".to_string(), one())].into_iter().collect();
        assert_eq!(decode(&encode("1 2 1", &map)), Some(map));
        assert_eq!(decode("{}"), None);
        // Same second, another socket: another key.
        let (a, b) = (server_key("4242", "/private/tmp/tmux-501/default"), server_key("4242", "/private/tmp/tmux-501/insA2"));
        assert!(a.starts_with("4242_") && a.len() == 13 && a != b);
        assert_eq!(a, server_key("4242\n", "/private/tmp/tmux-501/default"));
        assert_eq!((key_of("4242_ab-12.files"), key_of("4242_ab.gen"), key_of(".x.gen.9.tmp"), key_of("notes")), (Some("4242_ab"), Some("4242_ab"), None, None));
    }

    /// Two sidebars of one server: ONE engine runs (the lock holder's), the
    /// other mirrors what it publishes; readers trust the files only while
    /// the holder lives and has had a complete pass; the holder gone, the
    /// other takes over. Sweeping is the holder's alone.
    #[test]
    fn one_publisher_per_server() {
        let d = dir("one");
        let (ma, mb) = (Arc::new(AtomicUsize::new(0)), Arc::new(AtomicUsize::new(0)));
        let mut a = Board::new(counted(&ma, Duration::ZERO), d.clone());
        let mut b = Board::new(counted(&mb, Duration::ZERO), d.clone());
        let s = snap();
        a.refresh(&s, Some(K));
        b.refresh(&s, Some(K));
        assert!(a.holder() && a.runs() && !b.holder() && !b.runs());
        settle(&mut a);
        assert_eq!((ma.load(Ordering::SeqCst), mb.load(Ordering::SeqCst)), (1, 0));
        assert!(json_path(&d, K).is_file() && verified(&d, K));
        assert!(fs::read_to_string(gen_path(&d, K)).unwrap().ends_with(" 1"));
        // The mirror follows every publish.
        let g = b.generation();
        assert!(b.poll() && b.generation() > g && !b.poll());
        assert_eq!(b.map(), a.map());
        let map: BTreeMap<String, Insight> = [("@3".to_string(), one())].into_iter().collect();
        write_atomic(&json_path(&d, K), &encode("x 9 1", &map)).unwrap();
        write_atomic(&gen_path(&d, K), "x 9 1").unwrap();
        assert!(b.poll());
        assert_eq!(b.map(), map);
        // A non-holder never sweeps.
        fs::write(d.join(format!("{K}-8.files")), "stale").unwrap();
        b.refresh(&s, Some(K));
        assert!(d.join(format!("{K}-8.files")).exists() && !b.runs());
        // The holder gone: its .gen with it, nothing verified; the other
        // takes over on its next refresh, clears what was published, and
        // runs an engine of its own.
        drop(a);
        assert!(!gen_path(&d, K).exists() && !verified(&d, K));
        assert!(eventually(|| {
            b.refresh(&s, Some(K));
            b.holder()
        }));
        assert!(!d.join(format!("{K}-8.files")).exists());
        settle(&mut b);
        assert_eq!(mb.load(Ordering::SeqCst), 1);
        assert!(verified(&d, K));
        // Another server elects apart.
        let mut c = Board::new(counted(&Arc::new(AtomicUsize::new(0)), Duration::ZERO), d.clone());
        c.refresh(&s, Some("5151_00000000"));
        assert!(c.holder());
        drop((b, c));
        let _ = fs::remove_dir_all(&d);
    }

    /// A crashed holder's leftovers (its `.gen`, its `.files`) are gone the
    /// moment another takes the lock, before it has published anything, so
    /// no reader trusts them meanwhile; other servers' leftovers go too
    /// (not a live one's).
    #[test]
    fn taking_over_clears_a_dead_holders_files() {
        let d = dir("take");
        // Another server, alive (its lock held): kept. (Its own takeover
        // swept the folder first: the leftovers come after it.)
        let live = "2222_bbbbbbbb";
        let mut l = Board::new(counted(&Arc::new(AtomicUsize::new(0)), Duration::ZERO), d.clone());
        l.refresh(&snap(), Some(live));
        fs::write(json_path(&d, live), "{}").unwrap();
        fs::write(gen_path(&d, K), "999 7 1").unwrap();
        fs::write(json_path(&d, K), "{}").unwrap();
        fs::write(d.join(format!("{K}-3.files")), "/r\n\ta.rs\n").unwrap();
        // Another server, dead: everything of it; a stale temp file.
        let dead = "1111_aaaaaaaa";
        for f in [gen_path(&d, dead), json_path(&d, dead), d.join(format!("{dead}-3.files")), lock_path(&d, dead), d.join(".x.json.99.tmp")] {
            fs::write(&f, "x").unwrap();
            File::options().write(true).open(&f).unwrap().set_modified(SystemTime::now() - Duration::from_secs(2 * 86_400)).unwrap();
        }
        // A fresh holder whose engine takes a while to start.
        let mut b = Board::new(counted(&Arc::new(AtomicUsize::new(0)), Duration::from_millis(300)), d.clone());
        assert!(!verified(&d, K)); // (no lock held: the dead one's .gen is not trusted)
        b.refresh(&snap(), Some(K));
        assert!(b.holder());
        assert!(!gen_path(&d, K).exists() && !json_path(&d, K).exists() && !d.join(format!("{K}-3.files")).exists());
        assert!(!verified(&d, K));
        let gone = |p: PathBuf| !p.exists();
        assert!(gone(gen_path(&d, dead)) && gone(json_path(&d, dead)) && gone(d.join(format!("{dead}-3.files"))) && gone(lock_path(&d, dead)));
        assert!(gone(d.join(".x.json.99.tmp")) && json_path(&d, live).exists() && lock_path(&d, live).exists());
        settle(&mut b);
        assert!(verified(&d, K));
        drop((b, l));
        let _ = fs::remove_dir_all(&d);
    }

    /// A failed snapshot (tmux did not answer: no client, no window) keeps
    /// the server and the lock, and never reaches the engine; no key known
    /// keeps the last one.
    #[test]
    fn a_failed_snapshot_changes_nothing() {
        let d = dir("failed");
        let mut a = Board::new(counted(&Arc::new(AtomicUsize::new(0)), Duration::ZERO), d.clone());
        a.refresh(&snap(), Some(K));
        settle(&mut a);
        let g = a.insights.generation();
        a.refresh(&Snapshot::default(), None);
        std::thread::sleep(Duration::from_millis(100));
        assert!(a.holder() && a.key() == Some(K) && !a.pending() && a.insights.generation() == g && !a.poll());
        // An answered one is handed over again.
        a.refresh(&snap(), None);
        settle(&mut a);
        assert!(a.holder() && a.insights.generation() > g);
        drop(a);
        let _ = fs::remove_dir_all(&d);
    }

    /// A worker that gives up (its engine panics every time) removes `.gen`
    /// and lets the lock go, and never holds it again.
    #[test]
    fn a_dead_worker_lets_go() {
        let d = dir("dead");
        let mut i = Insights::with(|| panic!("no engine"));
        i.every = Duration::ZERO;
        let mut a = Board::new(i, d.clone());
        a.refresh(&snap(), Some(K));
        assert!(a.holder());
        write_atomic(&gen_path(&d, K), "1 1 1").unwrap();
        let t0 = Instant::now();
        while a.holder() && t0.elapsed() < Duration::from_secs(5) {
            a.refresh(&snap(), Some(K));
            std::thread::sleep(Duration::from_millis(5));
        }
        assert!(!a.holder() && !a.runs() && !gen_path(&d, K).exists() && eventually(|| !held(&d, K)));
        a.refresh(&snap(), Some(K));
        assert!(!a.holder());
        let _ = fs::remove_dir_all(&d);
    }

    /// A publish that cannot write (a read-only folder) ends publishing:
    /// the lock let go, its own engine kept.
    #[test]
    fn a_failed_write_goes_solo() {
        use std::os::unix::fs::PermissionsExt;
        let d = dir("ro");
        let mut a = Board::new(counted(&Arc::new(AtomicUsize::new(0)), Duration::from_millis(100)), d.clone());
        a.refresh(&snap(), Some(K));
        assert!(a.holder());
        fs::set_permissions(&d, fs::Permissions::from_mode(0o500)).unwrap();
        settle(&mut a);
        fs::set_permissions(&d, fs::Permissions::from_mode(0o700)).unwrap();
        assert!(!a.holder() && a.runs() && eventually(|| !held(&d, K)));
        a.refresh(&snap(), Some(K));
        assert!(!a.holder() && a.runs());
        drop(a);
        let _ = fs::remove_dir_all(&d);
    }

    /// A publish posts the insight notice: a mirror's notice fd wakes.
    #[cfg(target_os = "macos")]
    #[test]
    fn a_publish_wakes_the_mirrors() {
        let d = dir("wake");
        let name = format!("com.mackhaymond.agent-ui.test.{}.insight", std::process::id());
        let n = crate::sidebar::notice::Notices::register(std::slice::from_ref(&name));
        let fd = n.fd().expect("notify fd");
        let mut a = Board::new(counted(&Arc::new(AtomicUsize::new(0)), Duration::ZERO), d.clone()).with_notice(name);
        a.refresh(&snap(), Some(K));
        settle(&mut a);
        assert!(crate::sidebar::term::readable(fd, Duration::from_secs(2)).unwrap());
        assert!(n.drain() >= 1);
        drop(a);
        let _ = fs::remove_dir_all(&d);
    }

    /// Insights off (`AGENT_UI_NO_INSIGHT`, the unit tests): no lock taken,
    /// nothing read.
    #[test]
    fn off_takes_no_lock() {
        let d = dir("off");
        let mut a = Board::new(Insights::off(), d.clone());
        a.refresh(&snap(), Some(K));
        assert!(!a.holder() && !lock_path(&d, K).exists() && a.map().is_empty());
        let _ = fs::remove_dir_all(&d);
    }
}
