//! The STASHED agents that are no window: the pinned entries of
//! closed-tabs.sh's history (docs: the stash-backlog plan, "the stash is the
//! kept shelf inside ⌘Z's backlog"). Parking a one-pane agent tab closes it
//! through `closed-tabs.sh close --pin`, so the agent lives on only as a
//! line of `$XDG_STATE_HOME/tmux-closed-tabs/history.jsonl` with
//! `"pinned": true` and `stashed_ts`; the Parked rows ([`crate::view::Parked`])
//! list those beside the live `stash` windows.
//!
//! The file is read directly (no `closed-tabs.sh pinned-json` per tick), and
//! only again once its mtime, size or inode changed ([`Stash::entries`]). A
//! missing file is no entry; a line that is not JSON, half written, not
//! pinned or without a usable id is skipped, never an error.
//!
//! An entry's row carries the window id [`ENTRY_PREFIX`]`<id>`, never a tmux
//! id (`@N`), so every place that acts on a parked row tells it apart
//! ([`entry_id`]): go reopens it (`closed-tabs.sh reopen`), discard unpins it
//! (`closed-tabs.sh unpin`: back into the backlog, undoable).

use crate::json::{self, Value};
use std::path::{Path, PathBuf};
use std::time::SystemTime;

/// Overrides the history file's path (tests; "" = the default).
pub const HISTORY_ENV: &str = "AGENT_UI_CLOSED_TABS_HISTORY";

/// The window id a stashed entry's row carries: this prefix and the entry id.
pub const ENTRY_PREFIX: &str = "entry:";

/// The entry id behind a row's window id, None for a real window.
pub fn entry_id(win: &str) -> Option<&str> {
    win.strip_prefix(ENTRY_PREFIX).filter(|id| !id.is_empty())
}

/// closed-tabs.sh's `HISTORY`: `$AGENT_UI_CLOSED_TABS_HISTORY`, else
/// `${XDG_STATE_HOME:-$HOME/.local/state}/tmux-closed-tabs/history.jsonl`.
pub fn history_path() -> PathBuf {
    if let Some(p) = std::env::var_os(HISTORY_ENV).filter(|p| !p.is_empty()) {
        return PathBuf::from(p);
    }
    let state = std::env::var_os("XDG_STATE_HOME")
        .filter(|p| !p.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".local/state"));
    state.join("tmux-closed-tabs/history.jsonl")
}

/// An id is used in a shell line (quoted), a file name (its preview) and a
/// row's window id: closed-tabs.sh writes `<epoch>-<pid>`; anything outside
/// `[A-Za-z0-9._-]`, or starting with a dot, is no entry.
fn usable_id(id: &str) -> bool {
    !id.is_empty()
        && !id.starts_with('.')
        && id.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
}

/// One pinned history entry, as closed-tabs.sh wrote it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StashEntry {
    pub id: String,
    /// When the tab was closed (epoch seconds; 0 when missing).
    pub ts: i64,
    /// When it was pinned (`stashed_ts`); None in an entry pinned without it.
    pub stashed_ts: Option<i64>,
    /// The space (session) it came from.
    pub session: String,
    /// Its window index there.
    pub index: u32,
    pub cwd: String,
    /// The tab's name at close.
    pub label: String,
    /// claude | codex | opencode | grok | copilot | agy | "" (closed-tabs.sh
    /// records abby as claude: [`StashEntry::harness`]).
    pub tool: String,
    pub session_id: String,
    /// The resume command.
    pub cmd: String,
    /// The model's one-liner (do_describe), "" while missing.
    pub desc: String,
    /// `wake_at`: a SNOOZED entry's wake time (epoch seconds); None when it
    /// is only stashed. snooze.sh reopens it then.
    pub wake_at: Option<i64>,
}

impl StashEntry {
    /// One history line → the entry, when it is PINNED (`"pinned": true`)
    /// with a usable id; None for anything else (garbled, partial, unpinned).
    pub fn parse(line: &str) -> Option<StashEntry> {
        let v = json::parse(line.trim()).ok()?;
        if v.get("pinned").and_then(Value::as_bool) != Some(true) {
            return None;
        }
        let id = v.get("id").and_then(Value::scalar_string).filter(|id| usable_id(id))?;
        let s = |k: &str| v.get(k).and_then(Value::as_str).unwrap_or_default().to_string();
        let n = |k: &str| v.get(k).and_then(Value::as_f64).filter(|f| f.is_finite() && *f >= 0.0).map(|f| f as i64);
        Some(StashEntry {
            id,
            ts: n("ts").unwrap_or(0),
            stashed_ts: n("stashed_ts"),
            session: s("session"),
            index: n("index").and_then(|i| u32::try_from(i).ok()).unwrap_or(0),
            cwd: s("cwd"),
            label: s("label"),
            tool: s("tool"),
            session_id: s("session_id"),
            cmd: s("cmd"),
            desc: s("desc"),
            // A JSON number only, as snooze.sh reads it: anything else is an
            // alarm its timer never fires, so it is not shown as snoozed.
            wake_at: n("wake_at"),
        })
    }

    /// When it went on the shelf: `stashed_ts`, else its close time.
    pub fn when(&self) -> i64 {
        self.stashed_ts.unwrap_or(self.ts)
    }

    /// The row's window id ([`ENTRY_PREFIX`]).
    pub fn window_id(&self) -> String {
        format!("{ENTRY_PREFIX}{}", self.id)
    }

    /// The harness id its mark shows: `abby` for a claude entry whose resume
    /// command names abby (closed-tabs.sh's picker rule: `claude-abby` or
    /// `command abby `), else the tool.
    pub fn harness(&self) -> &str {
        if self.tool == "claude" && (self.cmd.contains("claude-abby") || self.cmd.starts_with("command abby ")) {
            "abby"
        } else {
            &self.tool
        }
    }

    /// closed-tabs.sh `pick_title`: (title, the rest). A generic label (a
    /// shell's name, `New Session`, a harness's name, the tool's own) gives
    /// way to the model's one-liner; with none yet, the row is named by where
    /// it lived. A specific label keeps the lead, the one-liner after it.
    pub fn title(&self) -> (String, String) {
        pick_title(&self.label, &self.desc, &self.tool, &self.session, self.index, &self.cwd)
    }
}

/// closed-tabs.sh `pick_title <label> <desc> <tool> <session> <index> <cwd>`,
/// ported word for word: (title, rest).
pub fn pick_title(label: &str, desc: &str, tool: &str, sess: &str, widx: u32, cwd: &str) -> (String, String) {
    // `${label##*/}`: after the last slash.
    let bare = label.rsplit('/').next().unwrap_or(label);
    const GENERIC: [&str; 19] = [
        "", "zsh", "bash", "sh", "fish", "-zsh", "home", "login", "New Session", "New session", "Claude Code", "claude",
        "codex", "grok", "copilot", "agy", "opencode", "abby", "gemini",
    ];
    if !(GENERIC.contains(&bare) || bare == tool) {
        return (label.to_string(), desc.to_string());
    }
    if !desc.is_empty() {
        return (desc.to_string(), String::new());
    }
    if bare == "home" {
        return (format!("home shell of {sess}"), String::new());
    }
    let place = crate::view::tilde(cwd);
    let base = place.rsplit('/').next().unwrap_or(&place);
    let tool = if tool.is_empty() { "shell" } else { tool };
    (format!("{tool} in {base} ({sess}:{widx})"), String::new())
}

/// Every PINNED entry of a history file's text, in file order; any other
/// line skipped ([`StashEntry::parse`]). An id seen twice keeps its last line
/// (closed-tabs.sh never writes two, but a half-finished rewrite might).
pub fn parse_pinned(text: &str) -> Vec<StashEntry> {
    let mut out: Vec<StashEntry> = Vec::new();
    // jq -c writes `"pinned":true`; a line without the key is never parsed.
    for e in text.lines().filter(|l| l.contains("\"pinned\"")).filter_map(StashEntry::parse) {
        out.retain(|o| o.id != e.id);
        out.push(e);
    }
    out
}

/// What tells a changed file: (mtime, size, inode).
type Stamp = (Option<SystemTime>, u64, u64);

fn stamp(path: &Path) -> Option<Stamp> {
    use std::os::unix::fs::MetadataExt;
    let m = std::fs::metadata(path).ok()?;
    Some((m.modified().ok(), m.len(), m.ino()))
}

/// The pinned entries of one history file, read again only when it
/// changed. Cheap to keep in a UI's core; a tick costs one `stat`.
#[derive(Debug, Clone, Default)]
pub struct Stash {
    /// None: no file at all (unit tests; nothing is ever listed).
    path: Option<PathBuf>,
    seen: Option<Stamp>,
    entries: Vec<StashEntry>,
}

impl Stash {
    /// The entries of the history file at `path` (None: none, ever).
    pub fn at(path: Option<PathBuf>) -> Stash {
        Stash { path, seen: None, entries: Vec::new() }
    }

    /// The live history ([`history_path`]); a unit-test build reads none, so
    /// no test depends on the machine's own stash.
    pub fn from_env() -> Stash {
        Stash::at(if cfg!(test) { None } else { Some(history_path()) })
    }

    pub fn path(&self) -> Option<&Path> {
        self.path.as_deref()
    }

    /// The entries, re-read when the file's stamp changed since the last
    /// call. A missing (or unreadable) file is no entry.
    pub fn entries(&mut self) -> &[StashEntry] {
        let Some(p) = self.path.as_deref() else { return &self.entries };
        let now = stamp(p);
        if now.is_none() {
            self.seen = None;
            self.entries.clear();
        } else if now != self.seen {
            match std::fs::read(p) {
                Ok(bytes) => {
                    self.entries = parse_pinned(&String::from_utf8_lossy(&bytes));
                    self.seen = now;
                }
                Err(_) => {
                    self.seen = None;
                    self.entries.clear();
                }
            }
        }
        &self.entries
    }

    /// The entries as last read (no `stat`).
    pub fn cached(&self) -> &[StashEntry] {
        &self.entries
    }

    /// The preview closed-tabs.sh captured for entry `id`
    /// (`previews/<id>.ansi` beside the history): its last `n` lines, None
    /// when there is none.
    pub fn preview(&self, id: &str, n: usize) -> Option<Vec<crate::ansi::StyledLine>> {
        if !usable_id(id) {
            return None;
        }
        let dir = self.path.as_deref()?.parent()?;
        let text = std::fs::read(dir.join("previews").join(format!("{id}.ansi"))).ok()?;
        Some(crate::actions::peek_from_capture(&String::from_utf8_lossy(&text), n))
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    /// A history: an unpinned entry, two pinned ones (one generic label with
    /// a desc, one abby), a garbled line, a half-written line, a pinned one
    /// with a bad id.
    pub const HISTORY: &str = concat!(
        r#"{"id":"1790000000-11","ts":1790000000,"session":"main","index":2,"cwd":"/tmp/a","label":"zsh","tool":"","session_id":"","cmd":""}"#, "\n",
        r#"{"id":"1790000100-12","ts":1790000100,"session":"work","index":3,"cwd":"/tmp/proj","label":"New Session","tool":"claude","session_id":"sid-1","cmd":"claude --resume sid-1","desc":"Fix the parser","pinned":true,"stashed_ts":1790000500}"#, "\n",
        "this is not json\n",
        r#"{"id":"1790000200-13","ts":1790000200,"session":"main","index":4,"cwd":"/tmp/x","label":"proj/Canvas sync","tool":"claude","session_id":"sid-2","cmd":"command abby --resume sid-2","pinned":true,"stashed_ts":1790000300}"#, "\n",
        r#"{"id":"1790000300-14","ts":1790000300,"pinned":true,"label":"half"#, "\n",
        r#"{"id":"../evil","ts":1,"pinned":true,"label":"x"}"#, "\n",
    );

    #[test]
    fn only_pinned_usable_lines() {
        let v = parse_pinned(HISTORY);
        assert_eq!(v.iter().map(|e| e.id.as_str()).collect::<Vec<_>>(), ["1790000100-12", "1790000200-13"]);
        let e = &v[0];
        assert_eq!((e.ts, e.stashed_ts, e.session.as_str(), e.index, e.tool.as_str()), (1790000100, Some(1790000500), "work", 3, "claude"));
        assert_eq!((e.when(), e.window_id().as_str(), e.harness()), (1790000500, "entry:1790000100-12", "claude"));
        assert_eq!(v[1].harness(), "abby");
        assert_eq!(entry_id(&e.window_id()), Some("1790000100-12"));
        assert_eq!((entry_id("@5"), entry_id("entry:")), (None, None));
        // No stashed_ts: its close time.
        let e = StashEntry::parse(r#"{"id":"9-9","ts":77,"pinned":true}"#).unwrap();
        assert_eq!((e.when(), e.label.as_str()), (77, ""));
        assert!(StashEntry::parse(r#"{"id":"9-9","ts":77,"pinned":false}"#).is_none());
        // Snoozed: pinned with wake_at, a JSON number only (snooze.sh's
        // rule): a string, even of digits, or a negative is none.
        assert_eq!(e.wake_at, None);
        let wake = |w: &str| StashEntry::parse(&format!(r#"{{"id":"9-9","ts":77,"pinned":true,"wake_at":{w}}}"#)).unwrap().wake_at;
        assert_eq!((wake("1790009000"), wake(r#""1790009000""#), wake(r#""soon""#), wake("-5")), (Some(1_790_009_000), None, None, None));
        assert!(StashEntry::parse(r#"{"id":"9-9","ts":77,"wake_at":1790009000}"#).is_none(), "unpinned is never snoozed");
        assert!(StashEntry::parse(r#"{"id":"","pinned":true}"#).is_none());
    }

    /// closed-tabs.sh `pick_title`, case by case.
    #[test]
    fn titles_mirror_pick_title() {
        let t = |label: &str, desc: &str, tool: &str, cwd: &str| pick_title(label, desc, tool, "work", 3, cwd);
        assert_eq!(t("proj/Canvas sync", "what it did", "claude", "/x"), ("proj/Canvas sync".into(), "what it did".into()));
        assert_eq!(t("New Session", "Fix the parser", "claude", "/x"), ("Fix the parser".into(), String::new()));
        assert_eq!(t("a/codex", "", "codex", "/tmp/proj"), ("codex in proj (work:3)".into(), String::new()));
        assert_eq!(t("home", "", "", "/tmp/proj"), ("home shell of work".into(), String::new()));
        assert_eq!(t("zsh", "", "", "/tmp/proj"), ("shell in proj (work:3)".into(), String::new()));
        assert_eq!(t("mytool", "", "mytool", "/tmp/q"), ("mytool in q (work:3)".into(), String::new()));
    }

    /// Read on first use, again only once the file changed; a missing file
    /// is nothing (and recovers when it comes back).
    #[test]
    fn rereads_on_change_only() {
        let dir = std::env::temp_dir().join(format!("agentui-stashed-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("previews")).unwrap();
        let p = dir.join("history.jsonl");
        let mut s = Stash::at(Some(p.clone()));
        assert!(s.entries().is_empty());
        std::fs::write(&p, HISTORY).unwrap();
        assert_eq!(s.entries().len(), 2);
        // The same stamp: the cached entries, not the file (proved by
        // poking the cache and seeing it kept).
        s.entries.pop();
        assert_eq!(s.entries().len(), 1);
        // Changed (longer): read again.
        std::fs::write(&p, format!("{HISTORY}{}\n", r#"{"id":"5-5","ts":5,"pinned":true}"#)).unwrap();
        assert_eq!(s.entries().len(), 3);
        std::fs::write(dir.join("previews/5-5.ansi"), "one\n\x1b[31mtwo\x1b[0m\n\n").unwrap();
        let lines = s.preview("5-5", 10).unwrap();
        assert_eq!(lines.iter().map(|l| l.plain()).collect::<Vec<_>>(), ["one", "two"]);
        assert!(s.preview("1790000100-12", 10).is_none() && s.preview("../x", 10).is_none());
        std::fs::remove_file(&p).unwrap();
        assert!(s.entries().is_empty());
        assert!(Stash::from_env().entries().is_empty(), "a test build never reads the live history");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
