//! The sidebar's WezTerm plumbing (agent-roster.py `Strip`): how it marks its
//! pane, finds the tmux client next to it, and hands focus back after a click.
//!
//! - Marking: [`strip_marker`] sets the user var `agent_strip=1` (OSC 1337,
//!   base64 "MQ==") that wezterm.lua's CMD+B looks for, and the pane title
//!   `agent-strip` as a second marker. Write it once at start, before the
//!   alternate screen.
//! - Mouse: the Python enables ONLY button tracking + SGR reports
//!   ([`MOUSE_ON`] = DECSET 1000 + 1006). crossterm's `EnableMouseCapture`
//!   also turns on 1002/1003 (motion), which floods a sidebar with events;
//!   write these sequences instead.
//! - Client: [`WeztermLink`] resolves `wezterm cli list` → the other pane in
//!   this tab whose tty is an attached tmux client (several: the CMD+B hint
//!   pane, then the active one, then the first). Re-resolved at most every
//!   [`RESOLVE_EVERY`] s while the client is missing; `--client` pins it.
//! - Focus: a click makes WezTerm focus the sidebar, and every CMD shortcut
//!   in wezterm.lua is a SendKey to the ACTIVE pane, so each click hands focus
//!   straight back ([`WeztermLink::focus_back`]: `wezterm cli activate-pane`),
//!   forwarding any keys that landed in the sidebar (`send-text --no-paste`).

use crate::json::{self, Value};
use crate::proc::{self, Output};
use crate::tmux::Snapshot;
use std::process::Command;
use std::time::Duration;

/// The user var wezterm.lua's CMD+B finds the sidebar pane by.
pub const STRIP_VAR: &str = "agent_strip";
pub const STRIP_TITLE: &str = "agent-strip";
/// While there is no tmux client: one `wezterm cli list` per 5 s.
pub const RESOLVE_EVERY: f64 = 5.0;
/// `wezterm cli` calls give up after 3 s.
pub const CLI_TIMEOUT: Duration = Duration::from_secs(3);
/// Button presses/releases as SGR reports, nothing else (Python's enter).
pub const MOUSE_ON: &str = "\x1b[?1000h\x1b[?1006h";
pub const MOUSE_OFF: &str = "\x1b[?1006l\x1b[?1000l";

/// OSC 1337 SetUserVar=agent_strip=MQ== (base64 "1") + OSC 2 title.
pub fn strip_marker() -> String {
    format!("\x1b]1337;SetUserVar={STRIP_VAR}=MQ==\x07\x1b]2;{STRIP_TITLE}\x07")
}

/// The wezterm binary: `--wezterm <path>`, else
/// `$WEZTERM_EXECUTABLE_DIR/wezterm`, used only if absolute and executable;
/// otherwise plain `wezterm` from PATH (agent-roster.py `main`).
pub fn wezterm_exe(arg: Option<&str>) -> String {
    use std::os::unix::fs::PermissionsExt;
    let cand = match arg {
        Some(a) => a.to_string(),
        None => std::path::Path::new(&std::env::var("WEZTERM_EXECUTABLE_DIR").unwrap_or_default())
            .join("wezterm")
            .to_string_lossy()
            .into_owned(),
    };
    let p = std::path::Path::new(&cand);
    let exec = std::fs::metadata(p).map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0).unwrap_or(false);
    if p.is_absolute() && exec {
        cand
    } else {
        "wezterm".into()
    }
}

/// agent-roster.py `wezterm_cli`: `wezterm cli --no-auto-start ...` → output
/// or None. `--no-auto-start`: with no GUI running, a plain `wezterm cli`
/// would start a mux server (which outlives us) instead of failing.
pub fn wezterm_cli(exe: &str, args: &[&str], input: Option<&[u8]>) -> Option<Output> {
    let mut c = Command::new(exe);
    c.args(["cli", "--no-auto-start"]).args(args);
    proc::run(c, input, CLI_TIMEOUT).ok()
}

/// agent-roster.py `wezterm_panes`: `wezterm cli list --format json`, parsed.
pub fn wezterm_panes(exe: &str) -> Option<Vec<Value>> {
    let o = wezterm_cli(exe, &["list", "--format", "json"], None)?;
    if !o.ok() {
        return None;
    }
    json::parse(&o.stdout).ok()?.as_array().map(|a| a.to_vec())
}

fn field(p: &Value, k: &str) -> Option<String> {
    p.get(k).and_then(Value::scalar_string)
}

/// agent-roster.py `pick_tmux_pane`: the strip's tmux client → (client tty,
/// wezterm pane id), or None. `panes`: wezterm_panes(); `own`: this pane's id
/// ($WEZTERM_PANE); `snap`: for the attached client ttys; `hint`: the pane
/// CMD+B was pressed in.
pub fn pick_tmux_pane(panes: &[Value], own: Option<&str>, snap: &Snapshot, hint: Option<&str>) -> Option<(String, String)> {
    let own = own?;
    let me = panes.iter().find(|p| field(p, "pane_id").as_deref() == Some(own))?;
    let tab = me.get("tab_id");
    let cands: Vec<&Value> = panes
        .iter()
        .filter(|p| {
            p.get("tab_id") == tab
                && field(p, "pane_id").as_deref() != Some(own)
                && p.get("tty_name").and_then(Value::as_str).is_some_and(|t| snap.client(t).is_some())
        })
        .collect();
    let best = hint
        .and_then(|h| cands.iter().find(|p| field(p, "pane_id").as_deref() == Some(h)))
        .or_else(|| cands.iter().find(|p| p.get("is_active").and_then(Value::as_bool) == Some(true)))
        .or(cands.first())?;
    Some((best.get("tty_name")?.as_str()?.to_string(), field(best, "pane_id")?))
}

/// The strip's link to its tmux client and WezTerm pane (Strip.__init__ /
/// load / resolve / focus_back).
#[derive(Debug, Clone)]
pub struct WeztermLink {
    pub exe: String,
    /// This pane ($WEZTERM_PANE).
    pub own: Option<String>,
    /// The pane CMD+B was pressed in (`--tmux-pane`).
    pub hint: Option<String>,
    /// `--client` was given: never resolve.
    pub fixed: bool,
    pub client: Option<String>,
    pub tmux_pane: Option<String>,
    next_resolve: f64,
}

impl WeztermLink {
    pub fn new(exe: String, client: Option<String>, hint: Option<String>, own: Option<String>) -> WeztermLink {
        let fixed = client.is_some();
        WeztermLink { exe, own, tmux_pane: if fixed { hint.clone() } else { None }, hint, fixed, client, next_resolve: 0.0 }
    }

    /// Strip.load: after each snapshot, re-resolve if our client is gone
    /// (rate-limited to RESOLVE_EVERY).
    pub fn on_snapshot(&mut self, snap: &Snapshot, now: f64) {
        let missing = self.client.as_deref().is_none_or(|c| snap.client(c).is_none());
        if !self.fixed && missing && now >= self.next_resolve {
            self.resolve(snap, now);
        }
    }

    /// Strip.resolve. A failed `wezterm cli list` keeps what we had.
    pub fn resolve(&mut self, snap: &Snapshot, now: f64) {
        self.next_resolve = now + RESOLVE_EVERY;
        if self.fixed {
            return;
        }
        let Some(panes) = wezterm_panes(&self.exe) else { return };
        match pick_tmux_pane(&panes, self.own.as_deref(), snap, self.hint.as_deref()) {
            Some((c, p)) => {
                self.client = Some(c);
                self.tmux_pane = Some(p);
            }
            None => {
                self.client = None;
                self.tmux_pane = None;
            }
        }
    }

    /// Strip.focus_back: give the keyboard back to the tmux pane, passing it
    /// `data` (keys that landed in the sidebar). One re-resolve if the pane
    /// id went stale. → None on success (or when pinned with no pane to hand
    /// to: silent, as in Python), else the message to show.
    pub fn focus_back(&mut self, data: &[u8], snap: &Snapshot, now: f64) -> Option<String> {
        for attempt in 0..2 {
            let Some(pane) = self.tmux_pane.clone() else {
                if attempt == 1 || self.fixed {
                    break;
                }
                self.resolve(snap, now);
                continue;
            };
            let mut ok = true;
            if !data.is_empty() {
                ok = wezterm_cli(&self.exe, &["send-text", "--pane-id", &pane, "--no-paste"], Some(data))
                    .is_some_and(|o| o.ok());
            }
            let r = wezterm_cli(&self.exe, &["activate-pane", "--pane-id", &pane], None);
            if ok && r.is_some_and(|o| o.ok()) {
                return None;
            }
            if attempt == 1 || self.fixed {
                break;
            }
            self.resolve(snap, now);
        }
        if self.fixed && self.tmux_pane.is_none() {
            None
        } else {
            Some("couldn't focus tmux · click it".into())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn marker() {
        assert_eq!(strip_marker(), "\x1b]1337;SetUserVar=agent_strip=MQ==\x07\x1b]2;agent-strip\x07");
    }

    #[test]
    fn picks_the_client_pane() {
        let snap = Snapshot::parse("\x1fclient\x1f/dev/ttys004\x1f@1\x1fmain\n\x1fclient\x1f/dev/ttys005\x1f@2\x1fmain");
        let panes = json::parse(
            r#"[{"tab_id":1,"pane_id":10,"tty_name":"/dev/ttys003","is_active":true},
                {"tab_id":1,"pane_id":11,"tty_name":"/dev/ttys004","is_active":false},
                {"tab_id":1,"pane_id":12,"tty_name":"/dev/ttys005","is_active":true},
                {"tab_id":2,"pane_id":13,"tty_name":"/dev/ttys006","is_active":true}]"#,
        )
        .unwrap();
        let panes = panes.as_array().unwrap();
        let got = |own, hint| pick_tmux_pane(panes, own, &snap, hint);
        assert_eq!(got(Some("10"), None), Some(("/dev/ttys005".into(), "12".into()))); // the active one
        assert_eq!(got(Some("10"), Some("11")), Some(("/dev/ttys004".into(), "11".into()))); // the hint
        assert_eq!(got(Some("13"), None), None); // nothing in that tab is a client
        assert_eq!(got(None, None), None);
        assert_eq!(got(Some("99"), None), None);
    }

    #[test]
    fn exe_resolution() {
        assert_eq!(wezterm_exe(Some("relative/wezterm")), "wezterm");
        assert_eq!(wezterm_exe(Some("/nonexistent/wezterm")), "wezterm");
        assert_eq!(wezterm_exe(Some("/bin/sh")), "/bin/sh");
    }
}
