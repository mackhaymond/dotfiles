//! PINNED agents: the window option `@agent_pinned`, kept across a restore.
//!
//! A pinned agent is listed in the sidebar's PINNED section (above AGENTS,
//! moved out of its space's group; [`crate::view::ViewModel::pins_split`]).
//! The pin itself is the WINDOW option `@agent_pinned`, its value the epoch
//! second it was pinned (the section's order: first pinned first). Every
//! toggle (the menu's Space p, prefix t's `agent-ui pin toggle`, a middle
//! click on a sidebar card) goes through [`toggle`].
//!
//! ## Kept across a restore: `pins.json`, by session id
//!
//! A window option dies with its server, and a resurrect restore (or
//! closed-tabs.sh's ⌘Z reopen) brings the agent back in a NEW window running
//! the SAME session id. So every pin is also an [`Entry`] in a small state
//! file ([`store_path`], `~/.local/state/agent-ui/pins.json`): the session id,
//! when it was pinned, the server it was last seen in (tmux's
//! `#{start_time}`), its window there, and when it was last seen.
//!
//! The session id of a window ([`sessions`]), no fork:
//! - claude: `~/.claude/sessions/<pid>.json`, whose `"tmux"` field names
//!   `<session>:<@window>.<%pane>`; the pid must be alive and the pane still
//!   in that window (the newest `startedAt` wins). Its argv
//!   (`sysctl(KERN_PROCARGS2)`) adds the ids it was started to RESUME
//!   (`--resume <id>`, `-r <id>`, `--resume=<id>`): every restore path
//!   (assistant-restore.sh, closed-tabs.sh, stash.sh's unpark) runs `claude
//!   --resume <sid>`, and a resumed claude may report a NEW id (after a
//!   compaction): an entry kept under the old id still finds its window and
//!   is re-keyed. (`--continue` names no id: such a resume is not matched.)
//! - codex: the tab hook's `@agent_session_id` (in the snapshot);
//! - opencode: the `-s <id>` / `--session <id>` of the opencode process
//!   under one of the window's panes (libproc's child list, its argv);
//!   one started without an id has none, and its pin lives as long as its
//!   window.
//!
//! The live sidebar syncs the file with the windows ([`reconcile`], at most
//! every few seconds, only while anything is pinned or stored):
//! 1. a pinned window whose session is known: its entry is set (server, window,
//!    time); a stale entry for that window under another id goes;
//! 2. an entry whose session now runs in an UNPINNED window: in the same
//!    server and the same window, the pin was taken off (dropped); anywhere
//!    else (a new server after a restore, a reopened tab) the pin is put
//!    back (`@agent_pinned` = its old time);
//! 3. an entry whose session runs nowhere: dropped if its window is still
//!    here (same server) without the pin, else kept for [`EXPIRE`] seconds
//!    (a parked or closed agent may come back).
//!
//! Several writers (sidebars, the pin command, the menu) can race: each
//! writes the whole file atomically (temp file, rename), and the next sync
//! repairs what a lost write dropped. A sync never re-pins a window it saw
//! unpinned in the same server.

use crate::json::{self, Value};
use crate::obj;
use crate::tmux::{Snapshot, Tmux, Window};
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::time::SystemTime;

/// The window option.
pub const OPTION: &str = "@agent_pinned";

/// The pin glyph (nf-oct-pin, U+F435: one cell, a Nerd Font glyph in the
/// BMP's private use area like the sidebar's branch glyph).
pub const GLYPH: &str = "\u{f435}";

/// How long an entry whose session runs nowhere is kept (seconds): a week.
pub const EXPIRE: i64 = 7 * 86_400;

/// The state file: `$AGENT_UI_PINS`, else `$XDG_STATE_HOME/agent-ui/pins.json`,
/// else `~/.local/state/agent-ui/pins.json`.
pub fn store_path() -> PathBuf {
    let env = |k: &str| std::env::var(k).ok().filter(|v| !v.is_empty());
    if let Some(p) = env("AGENT_UI_PINS") {
        return PathBuf::from(p);
    }
    let base = env("XDG_STATE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(".local/state"));
    base.join("agent-ui/pins.json")
}

/// The state file a process talking to `tmux` keeps: [`store_path`] for the
/// DEFAULT server ([`is_default_server`]); for any other (`--socket`,
/// `$AGENT_UI_TMUX_SOCKET`, or an ambient `$TMUX` naming a private `-L`
/// server: tests, previews, a test server running the real config) only an
/// explicit `$AGENT_UI_PINS`, else none at all, so a throwaway server can
/// never rewrite the real pins.
pub fn store_for(tmux: &Tmux) -> Option<PathBuf> {
    let env = |k: &str| std::env::var(k).ok().filter(|v| !v.is_empty());
    let explicit = env("AGENT_UI_PINS").is_some();
    let default = is_default_server(tmux.socket.as_ref(), env("TMUX").as_deref(), env("TMUX_TMPDIR").as_deref(),
        crate::tmux::uid());
    (default || explicit).then(store_path)
}

/// `p` with symlinks resolved as far as it exists (`/tmp` → `/private/tmp`
/// on macOS); a path that does not exist keeps its unresolved tail.
fn resolved(p: &Path) -> PathBuf {
    if let Ok(c) = std::fs::canonicalize(p) {
        return c;
    }
    match (p.parent(), p.file_name()) {
        (Some(d), Some(f)) if !d.as_os_str().is_empty() => resolved(d).join(f),
        _ => p.to_path_buf(),
    }
}

/// Whether the server `socket` names (None: the ambient one, `tmux_env`'s
/// first field when set, else the default) is tmux's DEFAULT server:
/// `realpath(${TMUX_TMPDIR:-/tmp})/tmux-<uid>/default`, compared resolved.
pub fn is_default_server(socket: Option<&crate::tmux::Socket>, tmux_env: Option<&str>, tmpdir: Option<&str>, uid: u32) -> bool {
    let base = resolved(Path::new(tmpdir.filter(|t| !t.is_empty()).unwrap_or("/tmp")));
    let default = base.join(format!("tmux-{uid}")).join("default");
    let path = match socket {
        Some(crate::tmux::Socket::Path(p)) => PathBuf::from(p),
        Some(crate::tmux::Socket::Name(n)) => base.join(format!("tmux-{uid}")).join(n),
        None => match tmux_env.and_then(|t| t.split(',').next()).filter(|s| !s.is_empty()) {
            Some(s) => PathBuf::from(s),
            None => return true,
        },
    };
    resolved(&path) == resolved(&default)
}

/// Claude's per-process session files: `$AGENT_UI_CLAUDE_SESSIONS`, else
/// `~/.claude/sessions`.
pub fn claude_sessions_dir() -> PathBuf {
    match std::env::var("AGENT_UI_CLAUDE_SESSIONS") {
        Ok(d) if !d.is_empty() => PathBuf::from(d),
        _ => PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(".claude/sessions"),
    }
}

/// One kept pin.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    /// The agent's session id.
    pub sid: String,
    /// When it was pinned (epoch seconds): `@agent_pinned`'s value.
    pub at: i64,
    /// The server it was last seen in (`#{start_time}`).
    pub server: String,
    /// Its window there (`@N`).
    pub win: String,
    /// When it was last seen (epoch seconds).
    pub seen: i64,
}

/// The state file's contents.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Store {
    pub entries: Vec<Entry>,
}

impl Store {
    /// Tolerant: junk, or an entry missing its id, is skipped.
    pub fn parse(text: &str) -> Store {
        let Ok(v) = json::parse(text) else { return Store::default() };
        let num = |e: &Value, k: &str| e.get(k).and_then(Value::as_f64).map_or(0, |n| n as i64);
        let s = |e: &Value, k: &str| e.get(k).and_then(Value::scalar_string).unwrap_or_default();
        let entries = v
            .get("pins")
            .and_then(Value::as_array)
            .unwrap_or(&[])
            .iter()
            .filter(|e| !s(e, "sid").is_empty())
            .map(|e| Entry { sid: s(e, "sid"), at: num(e, "at"), server: s(e, "server"), win: s(e, "win"), seen: num(e, "seen") })
            .collect();
        Store { entries }
    }

    pub fn to_json(&self) -> String {
        let pins: Vec<Value> = self
            .entries
            .iter()
            .map(|e| obj![("sid", &e.sid), ("at", e.at), ("server", &e.server), ("win", &e.win), ("seen", e.seen)])
            .collect();
        obj![("version", 1), ("pins", Value::Arr(pins))].to_json_pretty() + "\n"
    }

    /// The file's store (empty when missing or unreadable).
    pub fn load(path: &Path) -> Store {
        std::fs::read_to_string(path).map(|t| Store::parse(&t)).unwrap_or_default()
    }

    /// Written whole: a temp file beside it, then a rename (a reader never
    /// sees half a file); its folder made when missing.
    pub fn save(&self, path: &Path) -> std::io::Result<()> {
        if let Some(d) = path.parent() {
            std::fs::create_dir_all(d)?;
        }
        let tmp = path.with_extension(format!("json.{}.tmp", std::process::id()));
        std::fs::write(&tmp, self.to_json())?;
        std::fs::rename(&tmp, path).inspect_err(|_| {
            let _ = std::fs::remove_file(&tmp);
        })
    }

    fn remove_sid(&mut self, sid: &str) {
        self.entries.retain(|e| e.sid != sid);
    }

    fn upsert(&mut self, e: Entry) {
        match self.entries.iter_mut().find(|x| x.sid == e.sid) {
            Some(x) => *x = e,
            None => self.entries.push(e),
        }
    }
}

/// `"<session>:<@window>.<%pane>"` → (window, pane).
fn tmux_field(s: &str) -> Option<(&str, &str)> {
    let (_, wp) = s.rsplit_once(':')?;
    let (w, p) = wp.split_once('.')?;
    (w.starts_with('@') && p.starts_with('%')).then_some((w, p))
}

/// What the pins ask of the processes: whether a pid lives, its argv, its
/// children. [`LiveProcs`] asks the kernel (no fork); tests fake it.
pub trait Procs {
    fn alive(&self, pid: i32) -> bool;
    /// The process's argv (`KERN_PROCARGS2`), None when it cannot be read.
    fn argv(&self, pid: i32) -> Option<Vec<String>>;
    /// Its direct children.
    fn children(&self, pid: i32) -> Vec<i32>;
}

/// The kernel's answers: `kill(pid, 0)`, `sysctl(KERN_PROCARGS2)` and
/// libproc's `proc_listpids(PROC_PPID_ONLY)`; never a fork.
pub struct LiveProcs;

impl Procs for LiveProcs {
    fn alive(&self, pid: i32) -> bool {
        crate::sidebar::term::alive(pid)
    }

    fn argv(&self, pid: i32) -> Option<Vec<String>> {
        sys::argv(pid)
    }

    fn children(&self, pid: i32) -> Vec<i32> {
        sys::children(pid)
    }
}

#[cfg(target_os = "macos")]
mod sys {
    use std::os::raw::{c_int, c_uint, c_void};
    extern "C" {
        fn sysctl(name: *mut c_int, namelen: c_uint, oldp: *mut c_void, oldlenp: *mut usize, newp: *mut c_void, newlen: usize)
            -> c_int;
        fn proc_listpids(kind: u32, typeinfo: u32, buffer: *mut c_void, buffersize: c_int) -> c_int;
    }
    const CTL_KERN: c_int = 1;
    const KERN_PROCARGS2: c_int = 49;
    const PROC_PPID_ONLY: u32 = 6;

    /// `sysctl(KERN_PROCARGS2)`: an `int argc`, the exec path, NUL padding,
    /// then argc NUL-ended strings (the environment after them is not read).
    pub fn argv(pid: i32) -> Option<Vec<String>> {
        if pid <= 0 {
            return None;
        }
        let mut mib = [CTL_KERN, KERN_PROCARGS2, pid];
        let mut len: usize = 0;
        // SAFETY: a size query (null buffer) on a 3-int MIB.
        if unsafe { sysctl(mib.as_mut_ptr(), 3, std::ptr::null_mut(), &mut len, std::ptr::null_mut(), 0) } != 0 || len < 4 {
            return None;
        }
        let mut buf = vec![0u8; len];
        // SAFETY: `buf` holds `len` bytes, and sysctl writes at most `len`.
        if unsafe { sysctl(mib.as_mut_ptr(), 3, buf.as_mut_ptr().cast(), &mut len, std::ptr::null_mut(), 0) } != 0 {
            return None;
        }
        super::parse_procargs(&buf[..len.min(buf.len())])
    }

    pub fn children(pid: i32) -> Vec<i32> {
        if pid <= 0 {
            return Vec::new();
        }
        let mut buf = vec![0i32; 256];
        let bytes = (buf.len() * 4) as c_int;
        // SAFETY: `buf` holds `bytes` bytes; proc_listpids fills at most that.
        let n = unsafe { proc_listpids(PROC_PPID_ONLY, pid as u32, buf.as_mut_ptr().cast(), bytes) };
        if n <= 0 {
            return Vec::new();
        }
        buf.truncate((n as usize / 4).min(256));
        buf.retain(|&p| p > 0);
        buf
    }
}

#[cfg(not(target_os = "macos"))]
mod sys {
    pub fn argv(_: i32) -> Option<Vec<String>> {
        None
    }
    pub fn children(_: i32) -> Vec<i32> {
        Vec::new()
    }
}

/// A `KERN_PROCARGS2` buffer → argv: `argc` (native-endian int), the exec
/// path, NUL padding, then `argc` NUL-ended strings.
pub fn parse_procargs(buf: &[u8]) -> Option<Vec<String>> {
    let argc = i32::from_ne_bytes(buf.get(..4)?.try_into().ok()?);
    if argc <= 0 {
        return None;
    }
    let rest = &buf[4..];
    let path_end = rest.iter().position(|&b| b == 0)?;
    let mut i = path_end;
    while rest.get(i) == Some(&0) {
        i += 1;
    }
    let mut out = Vec::new();
    for part in rest[i..].split(|&b| b == 0).take(argc as usize) {
        out.push(String::from_utf8_lossy(part).into_owned());
    }
    (out.len() == argc as usize).then_some(out)
}

/// The session ids `argv` was started to resume: a value after any of
/// `flags` (`--resume X`, `-r X`), or glued (`--resume=X`); a value that is
/// itself a flag (`-r` with no id: the picker) is none. `--continue` names
/// no id, so it is never one.
pub fn resumed_ids(argv: &[String], flags: &[&str]) -> Vec<String> {
    let mut out = Vec::new();
    let ok = |v: &str| !v.is_empty() && !v.starts_with('-');
    for (i, a) in argv.iter().enumerate() {
        if flags.contains(&a.as_str()) {
            if let Some(v) = argv.get(i + 1).filter(|v| ok(v)) {
                out.push(v.clone());
            }
        } else if let Some((f, v)) = a.split_once('=') {
            if flags.contains(&f) && ok(v) {
                out.push(v.to_string());
            }
        }
    }
    out
}

/// claude's resume flags (closed-tabs.sh / assistant-restore.sh / stash.sh
/// run `claude --resume <sid>`).
pub const CLAUDE_RESUME: [&str; 2] = ["--resume", "-r"];
/// opencode's session flags (`opencode -s <sid>` resumes that session, the
/// SAME id).
pub const OPENCODE_SESSION: [&str; 2] = ["-s", "--session"];

/// Every window's session as the pins see it.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Sessions {
    /// window id → the session it runs now.
    pub now: HashMap<String, String>,
    /// window id → the session ids its agent was started to resume (a
    /// resumed claude may report a NEW id, after a compaction, say: an
    /// entry kept under the old one still finds its window).
    pub resumed: HashMap<String, Vec<String>>,
}

/// Every window's agent session in `snap` (no fork):
/// - claude: its session file in `dir` (the newest live one naming a pane
///   still in the window), and the ids its argv resumed;
/// - codex: `@agent_session_id`, where no claude file spoke;
/// - opencode (`@agent_kind`): the `-s <id>` of the opencode process under
///   one of its panes (an opencode started without one has no id here).
pub fn sessions(snap: &Snapshot, dir: &Path, procs: &dyn Procs) -> Sessions {
    let mut out = Sessions::default();
    let wins: HashMap<&str, &Window> = snap.windows.iter().map(|w| (w.id.as_str(), w)).collect();
    // Claude: the newest live session file naming a pane of the window.
    let mut best: HashMap<String, (i64, String, i32)> = HashMap::new();
    if let Ok(rd) = std::fs::read_dir(dir) {
        for ent in rd.flatten() {
            let p = ent.path();
            if p.extension().and_then(|e| e.to_str()) != Some("json") {
                continue;
            }
            let Ok(text) = std::fs::read_to_string(&p) else { continue };
            let Ok(v) = json::parse(&text) else { continue };
            let sid = v.get("sessionId").and_then(Value::as_str).unwrap_or("");
            let pid = v.get("pid").and_then(Value::as_f64).map_or(0, |n| n as i32);
            let started = v.get("startedAt").and_then(Value::as_f64).map_or(0, |n| n as i64);
            let Some((win, pane)) = v.get("tmux").and_then(Value::as_str).and_then(tmux_field) else { continue };
            let Some(w) = wins.get(win) else { continue };
            if sid.is_empty() || !w.pane_list.is_empty() && !w.pane_list.iter().any(|q| q.id == pane) || !procs.alive(pid) {
                continue;
            }
            if best.get(win).is_none_or(|(t, _, _)| started > *t) {
                best.insert(win.to_string(), (started, sid.to_string(), pid));
            }
        }
    }
    for (w, (_, sid, pid)) in best {
        let olds: Vec<String> =
            procs.argv(pid).map(|a| resumed_ids(&a, &CLAUDE_RESUME)).unwrap_or_default().into_iter().filter(|o| *o != sid).collect();
        if !olds.is_empty() {
            out.resumed.insert(w.clone(), olds);
        }
        out.now.insert(w, sid);
    }
    // Codex (the tab hook sets it only for one): where no claude file spoke.
    for w in snap.windows.iter().filter(|w| !w.session_id.is_empty()) {
        out.now.entry(w.id.clone()).or_insert_with(|| w.session_id.clone());
    }
    // Opencode: its `-s <id>`, from the process under one of its panes.
    let mut seen = HashSet::new();
    let opencodes: Vec<&Window> =
        snap.windows.iter().filter(|w| w.kind == "opencode" && !out.now.contains_key(&w.id) && seen.insert(w.id.as_str())).collect();
    for w in opencodes {
        if let Some(sid) = w.pane_list.iter().filter(|p| !p.dash).filter_map(|p| p.pid).find_map(|pid| opencode_id(procs, pid)) {
            out.now.insert(w.id.clone(), sid);
        }
    }
    out
}

/// The `-s <id>` of the first opencode process at or under `pid` (a few
/// levels: the pane's shell, maybe a wrapper).
fn opencode_id(procs: &dyn Procs, pid: i32) -> Option<String> {
    let mut level = vec![pid];
    for _ in 0..4 {
        let mut next = Vec::new();
        for p in level {
            if let Some(a) = procs.argv(p) {
                let opencode = a.iter().take(2).any(|x| x.rsplit('/').next() == Some("opencode"));
                if opencode {
                    if let Some(id) = resumed_ids(&a, &OPENCODE_SESSION).into_iter().next() {
                        return Some(id);
                    }
                }
            }
            next.extend(procs.children(p));
        }
        if next.is_empty() {
            break;
        }
        level = next;
    }
    None
}

/// Every window's session id now (window id → id): [`sessions`]' `now`.
pub fn session_ids(snap: &Snapshot, dir: &Path, procs: &dyn Procs) -> HashMap<String, String> {
    sessions(snap, dir, procs).now
}

/// A change to a window's pin.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Plan {
    /// `@agent_pinned` = the time, on the window.
    Set(String, i64),
    Unset(String),
}

/// The tmux command for `plans`, in one call (None: nothing to do).
pub fn command(plans: &[Plan]) -> Option<Vec<String>> {
    let mut args: Vec<String> = Vec::new();
    for p in plans {
        if !args.is_empty() {
            args.push(";".into());
        }
        match p {
            Plan::Set(w, at) => args.extend(["set-option", "-w", "-t", w, OPTION, &at.to_string()].map(String::from)),
            Plan::Unset(w) => args.extend(["set-option", "-wu", "-t", w, OPTION].map(String::from)),
        }
    }
    (!args.is_empty()).then_some(args)
}

/// The server's identity: `#{start_time}` as a client row carries it.
pub fn server_of(snap: &Snapshot) -> Option<String> {
    snap.clients.iter().map(|c| c.status.server_start.trim()).find(|s| !s.is_empty()).map(String::from)
}

/// `#{start_time}`, from the snapshot or else asked of the server.
pub fn server_start(tmux: &Tmux, snap: &Snapshot) -> Option<String> {
    server_of(snap).or_else(|| {
        let o = tmux.run(&["display-message", "-p", "#{start_time}"]).ok().filter(|o| o.ok())?;
        Some(o.stdout.trim().to_string()).filter(|s| !s.is_empty())
    })
}

/// Sync `store` with the windows of `snap` (see the module docs): the pins
/// to put back, and `store` brought up to date. `s`: [`sessions`]; `server`:
/// this server's `#{start_time}`; `now`: epoch seconds.
///
/// An entry is found by the session its window runs NOW, else by one its
/// agent was started to RESUME ([`Sessions::resumed`]: a resumed claude may
/// report a new id); found that way it is re-keyed to the new id.
pub fn reconcile(store: &mut Store, snap: &Snapshot, s: &Sessions, server: &str, now: i64) -> Vec<Plan> {
    let sids = &s.now;
    let pinned: HashMap<&str, i64> = snap.windows.iter().filter_map(|w| Some((w.id.as_str(), w.pinned?))).collect();
    let exists: HashSet<&str> = snap.windows.iter().map(|w| w.id.as_str()).collect();
    let runs: HashMap<&str, &str> = sids.iter().map(|(w, s)| (s.as_str(), w.as_str())).collect();
    let resumed_in: HashMap<&str, &str> =
        s.resumed.iter().flat_map(|(w, olds)| olds.iter().map(move |o| (o.as_str(), w.as_str()))).collect();
    let mut touched: HashSet<String> = HashSet::new();
    // 1. Pinned windows.
    let mut wins: Vec<(&str, i64)> = pinned.iter().map(|(w, a)| (*w, *a)).collect();
    wins.sort();
    for (win, at) in wins {
        match sids.get(win) {
            Some(sid) => {
                // Its old ids (this window here, the ones it resumed) are this pin.
                let olds = s.resumed.get(win).cloned().unwrap_or_default();
                store.entries.retain(|e| !(olds.contains(&e.sid) || e.server == server && e.win == win && e.sid != *sid));
                store.upsert(Entry { sid: sid.clone(), at, server: server.into(), win: win.into(), seen: now });
                touched.insert(sid.clone());
            }
            None => {
                // Its session unknown (a parked, suspended agent; opencode):
                // whatever entry names this window here stays as it is.
                for e in store.entries.iter_mut().filter(|e| e.server == server && e.win == win) {
                    e.seen = now;
                    touched.insert(e.sid.clone());
                }
            }
        }
    }
    // 2 and 3. The other entries.
    let mut plans = Vec::new();
    let mut drop: Vec<String> = Vec::new();
    for e in store.entries.iter_mut().filter(|e| !touched.contains(&e.sid)) {
        let here = e.server == server;
        let found = runs.get(e.sid.as_str()).map(|&w| (w, false)).or_else(|| resumed_in.get(e.sid.as_str()).map(|&w| (w, true)));
        match found {
            Some((w, _)) if here && e.win == w => drop.push(e.sid.clone()),
            // (A window pinned already holds its own entry: nothing to put back.)
            Some((w, true)) if pinned.contains_key(w) => drop.push(e.sid.clone()),
            Some((w, by_resume)) => {
                plans.push(Plan::Set(w.to_string(), e.at));
                e.server = server.into();
                e.win = w.into();
                e.seen = now;
                if by_resume {
                    if let Some(new) = sids.get(w) {
                        e.sid = new.clone();
                    }
                }
            }
            None if here && exists.contains(e.win.as_str()) && !pinned.contains_key(e.win.as_str()) => drop.push(e.sid.clone()),
            None if now - e.seen > EXPIRE => drop.push(e.sid.clone()),
            None => {}
        }
    }
    for sid in drop {
        store.remove_sid(&sid);
    }
    // A re-keyed entry may meet one already under its new id: one each.
    let mut ids = HashSet::new();
    store.entries.retain(|e| ids.insert(e.sid.clone()));
    plans.dedup();
    plans
}

/// The sidebar's side of the sync: the store kept in memory (re-read only
/// when the file changed), [`reconcile`] run when the windows changed or
/// every [`Sync::EVERY`] seconds, and only while anything is pinned or
/// stored.
#[derive(Debug)]
pub struct Sync {
    path: PathBuf,
    dir: PathBuf,
    store: Store,
    mtime: Option<SystemTime>,
    last: f64,
    sig: u64,
    /// How many reconciles ran (observable for the tests).
    pub runs: u32,
}

impl Sync {
    pub const EVERY: f64 = 10.0;

    pub fn new(path: PathBuf, dir: PathBuf) -> Sync {
        Sync { path, dir, store: Store::default(), mtime: None, last: f64::NEG_INFINITY, sig: 0, runs: 0 }
    }

    /// The sync a sidebar on `tmux` runs: None off the default server
    /// without `$AGENT_UI_PINS` ([`store_for`]).
    pub fn for_tmux(tmux: &Tmux) -> Option<Sync> {
        store_for(tmux).map(|p| Sync::new(p, claude_sessions_dir()))
    }

    fn reload(&mut self) {
        let m = std::fs::metadata(&self.path).and_then(|m| m.modified()).ok();
        if m != self.mtime {
            self.mtime = m;
            self.store = Store::load(&self.path);
        }
    }

    /// What the windows look like to the sync: their ids, pins and kinds.
    fn signature(snap: &Snapshot) -> u64 {
        use std::hash::{Hash, Hasher};
        let mut h = std::collections::hash_map::DefaultHasher::new();
        for w in &snap.windows {
            (&w.id, w.pinned, w.state.is_empty(), &w.session_id).hash(&mut h);
        }
        h.finish()
    }

    /// One tick against `snap` at `now`: the tmux command that puts pins
    /// back (None: nothing to do). The store is saved when it changed.
    pub fn tick(&mut self, snap: &Snapshot, now: f64, procs: &dyn Procs) -> Option<Vec<String>> {
        let server = server_of(snap)?;
        if snap.windows.is_empty() {
            return None;
        }
        let sig = Self::signature(snap);
        if sig == self.sig && now - self.last < Self::EVERY {
            return None;
        }
        self.reload();
        if self.store.entries.is_empty() && snap.windows.iter().all(|w| w.pinned.is_none()) {
            (self.sig, self.last) = (sig, now);
            return None;
        }
        (self.sig, self.last) = (sig, now);
        self.runs += 1;
        let s = sessions(snap, &self.dir, procs);
        let before = self.store.clone();
        let plans = reconcile(&mut self.store, snap, &s, &server, now as i64);
        if self.store != before && self.store.save(&self.path).is_ok() {
            self.mtime = std::fs::metadata(&self.path).and_then(|m| m.modified()).ok();
        }
        command(&plans)
    }
}

// ---------------------------------------------------------------- ⌥W 1-9

/// The window option holding a pinned agent's ⌥W digit.
pub const KEY_OPTION: &str = "@pin_key";

/// How many pins get a key: ⌥W 1 to ⌥W 9.
pub const KEYS: usize = 9;

/// The pinned agents' windows in PIN order, as the sidebar's PINNED section
/// lists them ([`crate::view::ViewModel::pinned_agents`]): `@agent_pinned`
/// set, an agent (a state), never a home, in a shown session (not the
/// stash, not agents / tasks / scratch / btop-popup); first pinned first
/// (the pin's time, then the window id's number); each window once.
pub fn pin_order(snap: &Snapshot) -> Vec<&str> {
    let mut seen = HashSet::new();
    let mut v: Vec<&Window> = snap
        .windows
        .iter()
        .filter(|w| {
            w.pinned.is_some()
                && !w.state.is_empty()
                && !w.home
                && w.session != crate::tmux::HOLD
                && !crate::model::HIDDEN.contains(&w.session.as_str())
        })
        .filter(|w| seen.insert(w.id.as_str()))
        .collect();
    v.sort_by_key(|w| (w.pinned.unwrap_or(0), crate::view::id_num(&w.id)));
    v.into_iter().map(|w| w.id.as_str()).collect()
}

/// Every pinned window's ⌥W digit: the first [`KEYS`] in [`pin_order`] get 1..=9,
/// so the numbers are always 1..N with no gap; past nine, no key.
pub fn pin_keys(snap: &Snapshot) -> HashMap<String, u8> {
    pin_order(snap).into_iter().take(KEYS).zip(1u8..).map(|(w, n)| (w.to_string(), n)).collect()
}

/// A `@pin_key` write: (window id, the key; None unsets it).
pub type KeyWrite = (String, Option<u8>);

/// A stored `@pin_key` as a key (1-9), None for "" or junk.
pub fn parse_key(raw: &str) -> Option<u8> {
    match raw.trim().as_bytes() {
        [d @ b'1'..=b'9'] => Some(d - b'0'),
        _ => None,
    }
}

/// The writes that make every window's `@pin_key` what `keys` says: set
/// where it differs, unset on every other window that carries one (an
/// unpinned, closed-in-place, parked or tenth pin). Each window once.
pub fn key_writes(snap: &Snapshot, keys: &HashMap<String, u8>) -> Vec<KeyWrite> {
    let mut seen = HashSet::new();
    let mut out: Vec<KeyWrite> = snap
        .windows
        .iter()
        .filter(|w| seen.insert(w.id.as_str()))
        .filter_map(|w| {
            let want = keys.get(&w.id).copied();
            let ok = match want {
                Some(n) => parse_key(&w.pin_key) == Some(n),
                None => w.pin_key.is_empty(),
            };
            (!ok).then(|| (w.id.clone(), want))
        })
        .collect();
    out.sort_by_key(|(w, _)| crate::view::id_num(w));
    out
}

/// One tmux command for `writes` (None: nothing to do).
pub fn key_command(writes: &[KeyWrite]) -> Option<Vec<String>> {
    let mut a: Vec<String> = Vec::new();
    for (id, key) in writes {
        if !a.is_empty() {
            a.push(";".into());
        }
        match key {
            Some(n) => a.extend(["set-option", "-q", "-w", "-t", id, KEY_OPTION, &n.to_string()].map(String::from)),
            None => a.extend(["set-option", "-q", "-wu", "-t", id, KEY_OPTION].map(String::from)),
        }
    }
    (!a.is_empty()).then_some(a)
}

/// The `@pin_key` writes a live sidebar has in flight (letters.rs's
/// `KeyMemo`, for pins): a write is not sent again while it is on its way;
/// a snapshot that shows it (or a gone window) settles it; one still unseen
/// after [`KeyMemo::RESEND`] seconds is forgotten, so the next refresh sends
/// it once more. Write-storm free: at most one write per window per RESEND.
#[derive(Debug, Default)]
pub struct KeyMemo {
    sent: HashMap<String, (Option<u8>, f64)>,
}

impl KeyMemo {
    pub const RESEND: f64 = 5.0;

    /// The writes of `writes` to send now (recorded as sent at `now`).
    pub fn fresh(&mut self, snap: &Snapshot, writes: Vec<KeyWrite>, now: f64) -> Vec<KeyWrite> {
        let have: HashMap<&str, Option<u8>> = snap.windows.iter().map(|w| (w.id.as_str(), parse_key(&w.pin_key))).collect();
        self.sent.retain(|id, (k, at)| have.get(id.as_str()).is_some_and(|h| h != k) && now - *at < Self::RESEND);
        writes
            .into_iter()
            .filter(|(id, k)| {
                if self.sent.get(id).is_some_and(|(s, _)| s == k) {
                    return false;
                }
                self.sent.insert(id.clone(), (*k, now));
                true
            })
            .collect()
    }
}

/// Renumber every pin now (after a toggle): ONE snapshot and, when anything
/// differs, ONE batched `set-option` command → the keys given.
pub fn renumber(tmux: &Tmux) -> HashMap<String, u8> {
    let snap = tmux.snapshot();
    let keys = pin_keys(&snap);
    if let Some(args) = key_command(&key_writes(&snap, &keys)) {
        let _ = tmux.run(&args);
    }
    keys
}

/// What a [`toggle`] did.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Toggled {
    /// Pinned now (false: unpinned).
    pub pinned: bool,
    /// The window's label (its summary, else its name).
    pub label: String,
    /// Whether the pin was kept in the state file too (a session id known).
    pub kept: bool,
    /// Its ⌥W digit now (None: unpinned, or past nine).
    pub key: Option<u8>,
}

/// What a toggle wants: `Some(true)` pin, `Some(false)` unpin, None flip.
pub type Want = Option<bool>;

/// Pin or unpin window `win` (`want`, else flip): `@agent_pinned` set to
/// `now` or unset, and the state file updated with it. Refused for a window
/// that is gone, not an agent, or a space's home. Only the window option
/// and the file are touched: posting the redraw notice is the caller's.
/// `path`: the state file ([`store_for`]); None keeps nothing.
#[allow(clippy::too_many_arguments)]
pub fn toggle(tmux: &Tmux, snap: &Snapshot, win: &str, want: Want, now: i64, path: Option<&Path>, dir: &Path,
    procs: &dyn Procs) -> Result<Toggled, String> {
    // (A pending space's claude is listed, in its own space: it pins.)
    let shown = |w: &&Window| {
        w.is_pending()
            || (w.session != crate::tmux::HOLD && !w.space_hidden && !crate::model::HIDDEN.contains(&w.session.as_str()))
    };
    let w = snap.rows(win).find(shown).or_else(|| snap.rows(win).next());
    let Some(w) = w else { return Err("that tab is gone".into()) };
    if w.home {
        return Err("a space's home is never pinned".into());
    }
    let pin = want.unwrap_or(w.pinned.is_none());
    if pin && w.state.is_empty() {
        return Err("only agents pin".into());
    }
    if pin && !shown(&w) {
        // (Nothing would list it, and ⌥W's digits never look there.)
        return Err(if w.session == crate::tmux::HOLD {
            "a parked tab is never pinned · unpark it first".into()
        } else {
            format!("{} is not a space · only a space's agents pin", w.session)
        });
    }
    let label = crate::text::sanitize(&w.label);
    if pin == w.pinned.is_some() && want.is_some() {
        return Ok(Toggled { pinned: pin, label, kept: false, key: parse_key(&w.pin_key) });
    }
    // A new pin always sorts last: past every pin there is, even one made
    // in the same second (whole seconds, as the restore keeps them).
    let last = snap.windows.iter().filter_map(|w| w.pinned).max().unwrap_or(0);
    let at = now.max(last + 1);
    let plan = if pin { Plan::Set(win.into(), at) } else { Plan::Unset(win.into()) };
    let args = command(std::slice::from_ref(&plan)).expect("one plan");
    match tmux.run(&args) {
        Ok(o) if o.ok() => {}
        _ => return Err(format!("couldn't set {OPTION}")),
    }
    // Every pin's ⌥W digit follows at once (1..N, no gap).
    let key = renumber(tmux).get(win).copied();
    let Some(path) = path else { return Ok(Toggled { pinned: pin, label, kept: false, key }) };
    let mut ss = sessions(snap, dir, procs);
    let sid = ss.now.remove(win);
    let olds = ss.resumed.remove(win).unwrap_or_default();
    let server = server_start(tmux, snap).unwrap_or_default();
    let mut store = Store::load(path);
    let before = store.clone();
    // The ids this window resumed are this pin too (superseded).
    store.entries.retain(|e| !olds.contains(&e.sid));
    match (&sid, pin) {
        (Some(s), true) if !server.is_empty() => {
            store.entries.retain(|e| !(e.server == server && e.win == win && e.sid != *s));
            store.upsert(Entry { sid: s.clone(), at, server: server.clone(), win: win.into(), seen: now });
        }
        (_, false) => {
            store.entries.retain(|e| sid.as_ref() != Some(&e.sid) && !(e.server == server && e.win == win));
        }
        _ => {}
    }
    if store != before {
        let _ = store.save(path);
    }
    Ok(Toggled { pinned: pin, label, kept: pin && sid.is_some() && !server.is_empty(), key })
}

/// The words a toggle's message says: `pinned Title · ⌥W 2` (no key past
/// nine) / `unpinned Title`.
pub fn said(t: &Toggled) -> String {
    match (t.pinned, t.key) {
        (true, Some(n)) => format!("pinned {} · ⌥W {n}", t.label),
        (true, None) => format!("pinned {}", t.label),
        (false, _) => format!("unpinned {}", t.label),
    }
}

const USAGE: &str = "usage: agent-ui pin [toggle|on|off] [--window <@id>] [--client <tty>]
       agent-ui pin renumber";

/// `agent-ui pin [toggle|on|off] [--window @id] [--client tty]`: pin or
/// unpin a window (default: the client's current one), post the agent-state
/// notice so every sidebar redraws at once, and with a client say so on it
/// (notify.sh: a toast in its sidebar, else tmux's message line). Exit 0
/// done, 1 refused (said on the client), 2 usage. `agent-ui pin renumber`:
/// just bring every `@pin_key` back to 1..N ([`renumber`]: spaces.conf's
/// ⌥W digit runs it on a miss, for a gap no sidebar was open to repair).
pub fn run(tmux: Tmux, args: &[String]) -> std::process::ExitCode {
    use std::process::ExitCode;
    if args.first().map(String::as_str) == Some("renumber") {
        if args.len() > 1 {
            eprintln!("{USAGE}");
            return ExitCode::from(2);
        }
        let before = tmux.snapshot();
        let keys = renumber(&tmux);
        if before.windows.iter().any(|w| parse_key(&w.pin_key) != keys.get(&w.id).copied()) {
            crate::sidebar::notice::post(&crate::sidebar::notice::names()[0]);
        }
        return ExitCode::SUCCESS;
    }
    let (mut want, mut win, mut client): (Want, Option<String>, Option<String>) = (None, None, None);
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "toggle" => want = None,
            "on" => want = Some(true),
            "off" => want = Some(false),
            "--window" => win = it.next().cloned(),
            "--client" => client = it.next().cloned(),
            _ => {
                eprintln!("{USAGE}");
                return ExitCode::from(2);
            }
        }
    }
    let snap = tmux.snapshot();
    let win = win.filter(|w| !w.is_empty()).or_else(|| client.as_deref().and_then(|c| snap.client_window(c)).map(String::from));
    let Some(win) = win else {
        eprintln!("{USAGE}");
        return ExitCode::from(2);
    };
    let store = store_for(&tmux);
    let r = toggle(&tmux, &snap, &win, want, crate::text::now() as i64, store.as_deref(), &claude_sessions_dir(), &LiveProcs);
    let (kind, msg, code) = match &r {
        Ok(t) => {
            crate::sidebar::notice::post(&crate::sidebar::notice::names()[0]);
            ("info", said(t), ExitCode::SUCCESS)
        }
        Err(e) => ("warn", format!("pin: {e}"), ExitCode::from(1)),
    };
    match client {
        Some(c) => notify(&tmux, &c, kind, &msg),
        None => eprintln!("{msg}"),
    }
    code
}

/// notify.sh on `client` (a toast in its sidebar, else tmux's message line).
fn notify(tmux: &Tmux, client: &str, kind: &str, text: &str) {
    let mut c = std::process::Command::new(crate::actions::scripts_dir().join("notify.sh"));
    c.args(["-k", kind, "-c", client, "--", text]);
    tmux.script_env(&mut c);
    let _ = crate::proc::run(c, None, std::time::Duration::from_secs(3));
}

#[cfg(test)]
mod tests;
