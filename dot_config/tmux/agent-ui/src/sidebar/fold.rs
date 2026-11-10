//! Spaces Mack keeps OPEN by hand: a dormant space ([`super::layout`],
//! "Dormant spaces") never folds while it is kept open.
//!
//! The gesture is the dormant header's fold chip (its counts and `▸`, or
//! `▾` once kept open; [`super::Target::Fold`]): a click keeps the space
//! open, a second one lets it fold again. The rest of the header still goes
//! to the space's home, as every header click does.
//!
//! ## Kept by NAME, in a file: `~/.local/state/agent-ui/space-open.json`
//!
//! Beside the pins' `pins.json`. Not a session option (`@space_open`): a
//! session option dies with its server, and a resurrect restore brings the
//! sessions back by NAME (space.sh's registry is keyed by name too), so the
//! name in a file is what survives a restore. The file is
//! `{"version": 1, "open": [{"name": "<space>", "at": <epoch>}]}`, written
//! whole (temp file, rename). Every sidebar re-reads it when its mtime or
//! size changes: one `stat` per refresh, no tmux call, no fork; the
//! sidebar that toggles posts the focus notice, so the others refresh (and
//! re-read) at once. A space closed for good leaves its name behind,
//! harmless (a new space by that name starts kept open); a click on its
//! chip clears it.
//!
//! Only the DEFAULT tmux server's sidebars use the real file ([`store_for`],
//! as the pins': a private `-L` test server never touches it) unless
//! `$AGENT_UI_SPACE_OPEN` names one; without a file the choice lives in the
//! sidebar's memory.

use crate::json::{self, Value};
use crate::obj;
use crate::tmux::Tmux;
use crate::view::ViewModel;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

/// The state file: `$AGENT_UI_SPACE_OPEN`, else
/// `$XDG_STATE_HOME/agent-ui/space-open.json`, else
/// `~/.local/state/agent-ui/space-open.json`.
pub fn store_path() -> PathBuf {
    let env = |k: &str| std::env::var(k).ok().filter(|v| !v.is_empty());
    if let Some(p) = env("AGENT_UI_SPACE_OPEN") {
        return PathBuf::from(p);
    }
    let base = env("XDG_STATE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(".local/state"));
    base.join("agent-ui/space-open.json")
}

/// The file a sidebar talking to `tmux` keeps: [`store_path`] for the
/// DEFAULT server ([`crate::pins::is_default_server`]), for any other only
/// an explicit `$AGENT_UI_SPACE_OPEN`, else none (memory only).
pub fn store_for(tmux: &Tmux) -> Option<PathBuf> {
    let env = |k: &str| std::env::var(k).ok().filter(|v| !v.is_empty());
    let explicit = env("AGENT_UI_SPACE_OPEN").is_some();
    let default = crate::pins::is_default_server(tmux.socket.as_ref(), env("TMUX").as_deref(), env("TMUX_TMPDIR").as_deref(),
        crate::tmux::uid());
    (default || explicit).then(store_path)
}

/// The spaces kept open (name → when), and where they are kept.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Store {
    path: Option<PathBuf>,
    /// The file's (mtime, size) as last read: a reload reads it again only
    /// when this changed.
    seen: Option<(SystemTime, u64)>,
    names: BTreeMap<String, i64>,
}

/// The file's text → the names kept open. Tolerant: junk, or an entry
/// without a name, is skipped.
pub fn parse(text: &str) -> BTreeMap<String, i64> {
    let Ok(v) = json::parse(text) else { return BTreeMap::new() };
    v.get("open")
        .and_then(Value::as_array)
        .unwrap_or(&[])
        .iter()
        .filter_map(|e| {
            let name = e.get("name").and_then(Value::as_str).filter(|n| !n.is_empty())?;
            Some((name.to_string(), e.get("at").and_then(Value::as_f64).map_or(0, |n| n as i64)))
        })
        .collect()
}

/// The names → the file's text.
pub fn to_json(names: &BTreeMap<String, i64>) -> String {
    let open: Vec<Value> = names.iter().map(|(n, at)| obj![("name", n.as_str()), ("at", *at)]).collect();
    obj![("version", 1), ("open", Value::Arr(open))].to_json_pretty() + "\n"
}

fn stamp(path: &Path) -> Option<(SystemTime, u64)> {
    let m = std::fs::metadata(path).ok()?;
    Some((m.modified().ok()?, m.len()))
}

impl Store {
    /// Kept in `path` (None: memory only), read at once.
    pub fn new(path: Option<PathBuf>) -> Store {
        let mut s = Store { path, ..Store::default() };
        s.reload();
        s
    }

    /// Read the file again if it changed since the last read (one `stat`).
    /// A file gone is nothing kept open.
    pub fn reload(&mut self) {
        let Some(path) = self.path.as_deref() else { return };
        let now = stamp(path);
        if now == self.seen && now.is_some() {
            return;
        }
        self.names = std::fs::read_to_string(path).map(|t| parse(&t)).unwrap_or_default();
        self.seen = now;
    }

    pub fn is_open(&self, name: &str) -> bool {
        self.names.contains_key(name)
    }

    /// Keep `name` open (at `now`), or let it fold again → whether it is
    /// kept open now. The file is re-read first (another sidebar may have
    /// changed it), then written whole: a temp file beside it, then a
    /// rename. A failed write keeps the old state.
    pub fn toggle(&mut self, name: &str, now: i64) -> std::io::Result<bool> {
        self.seen = None;
        self.reload();
        let mut names = self.names.clone();
        let open = names.remove(name).is_none();
        if open {
            names.insert(name.to_string(), now);
        }
        if let Some(path) = self.path.as_deref() {
            if let Some(d) = path.parent() {
                std::fs::create_dir_all(d)?;
            }
            let tmp = path.with_extension(format!("json.{}.tmp", std::process::id()));
            std::fs::write(&tmp, to_json(&names))?;
            std::fs::rename(&tmp, path).inspect_err(|_| {
                let _ = std::fs::remove_file(&tmp);
            })?;
            self.seen = stamp(path);
        }
        self.names = names;
        Ok(open)
    }

    /// Mark the view's spaces kept open ([`crate::view::Space::open`]).
    pub fn apply(&self, view: &mut ViewModel) {
        for sp in view.spaces.iter_mut() {
            sp.open = self.is_open(&sp.name);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("agent-ui-fold-{}-{tag}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        d.join("state/space-open.json")
    }

    /// A toggle keeps the space open in the file, a second lets it fold;
    /// another store (another sidebar) sees each change on its next reload;
    /// a store made later (a restart, a restore) reads it back by name.
    #[test]
    fn kept_open_by_name_across_sidebars_and_restarts() {
        let path = scratch("toggle");
        let mut a = Store::new(Some(path.clone()));
        let mut b = Store::new(Some(path.clone()));
        assert!(!a.is_open("notes"));
        assert!(a.toggle("notes", 100).unwrap());
        assert!(a.is_open("notes") && path.exists());
        b.reload();
        assert!(b.is_open("notes"));
        assert_eq!(parse(&std::fs::read_to_string(&path).unwrap()), BTreeMap::from([("notes".to_string(), 100)]));
        // A brand-new store (the sidebar restarted, the server restored).
        assert!(Store::new(Some(path.clone())).is_open("notes"));
        // b toggles another; a's toggle of notes re-reads first, keeps it.
        assert!(b.toggle("errands", 200).unwrap());
        assert!(!a.toggle("notes", 300).unwrap());
        let c = Store::new(Some(path.clone()));
        assert!(!c.is_open("notes") && c.is_open("errands"));
        let _ = std::fs::remove_dir_all(path.parent().unwrap().parent().unwrap());
    }

    /// Without a file, memory only; junk in the file is nothing kept.
    #[test]
    fn memory_only_and_junk() {
        let mut m = Store::new(None);
        assert!(m.toggle("x", 1).unwrap() && m.is_open("x"));
        assert!(!m.toggle("x", 2).unwrap() && !m.is_open("x"));
        assert!(parse("not json").is_empty());
        assert_eq!(parse(r#"{"open":[{"name":""},{"at":3},{"name":"ok","at":4}]}"#), BTreeMap::from([("ok".to_string(), 4)]));
        let text = to_json(&BTreeMap::from([("a b".to_string(), 7)]));
        assert_eq!(parse(&text), BTreeMap::from([("a b".to_string(), 7)]));
    }

    /// `apply` marks exactly the kept spaces.
    #[test]
    fn apply_marks_the_view() {
        use crate::collate::Collator;
        use crate::tmux::tests::row;
        use crate::tmux::Snapshot;
        use crate::view::BuildArgs;
        let snap = Snapshot::parse(&[row("a", 1, "@1", &[("state", "idle")]), row("b", 1, "@2", &[("state", "idle")])].join("\n"));
        let mut v = ViewModel::build(&snap, BuildArgs { client: None, now: 0.0, collator: &Collator::new("C"), git: None, log: vec![], watcher_age: None });
        let mut s = Store::new(None);
        s.toggle("b", 1).unwrap();
        s.apply(&mut v);
        assert_eq!(v.spaces.iter().map(|sp| (sp.name.as_str(), sp.open)).collect::<Vec<_>>(), [("a", false), ("b", true)]);
    }
}
