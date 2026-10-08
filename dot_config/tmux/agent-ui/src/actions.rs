//! Everything that changes something: moves, park/unpark, close, peek, the
//! watcher restart. Shells out exactly like agent-roster.py does, so the
//! scripts stay the one implementation of each move:
//!
//! | action            | runs                                                     | Python            |
//! |-------------------|----------------------------------------------------------|-------------------|
//! | go to a window    | `bash agent-jump.sh goto <tty> <win> <session>` (10 s)   | `Roster.go/jump`  |
//! | go to a parked one| `tmux run-shell -b "stash.sh unstash <win> <tty>"`       | `Roster.go`       |
//! | next / back       | `bash agent-jump.sh next|back <tty>`                     | `Roster.jump`     |
//! | go to a session   | goto on that session's ACTIVE window, in that session    | `Strip.click`     |
//! | park              | `run-shell -b "stash.sh stash <win>"`                    | `Roster.act` H    |
//! | close             | `run-shell -b "closed-tabs.sh close <pane>"` (agent pane)| `Roster.act` x    |
//! | discard (parked)  | `run-shell -b "stash.sh kill-many <win>"`                | `Roster.act` x    |
//! | restart watcher   | `run-shell -b "bash agent-tab-watcher.sh"`               | `Roster.act` r    |
//! | peek              | `capture-pane -p -e -J -S -N -t <agent pane>`            | `peek_lines`      |
//!
//! Every action that names a window re-checks it against the CURRENT
//! snapshot first (a row may come from a frame drawn before the latest
//! refresh): gone → [`PANE_GONE`]; no longer in the session the row showed
//! (parked or unparked meanwhile) → [`TAB_MOVED`], and nothing runs.
//!
//! "Never guess which pane": `x` closes the pane running the agent, matched
//! by TTY against `ps` like the watcher, never by pane_current_command (codex
//! via npm shows up as `node`). A split window where no pane, or several and
//! none of them active, runs an agent is refused with [`PANE_UNSURE`].

use crate::ansi::{self, StyledLine};
use crate::proc::{self, RunError};
use crate::tmux::{Snapshot, Tmux, Window, HOLD, US};
use std::path::PathBuf;
use std::process::Command;
use std::time::Duration;

pub const PANE_GONE: &str = "that window is gone";
pub const PANE_UNSURE: &str = "can't tell which pane is the agent · close it from the tab";
/// The Python adds the key to press again: "that tab moved · press x again"
/// (close/park) or "that tab moved · pick it again" (go).
pub const TAB_MOVED: &str = "that tab moved";
/// agent-roster.py `PEEK_LINES`.
pub const PEEK_LINES: usize = 15;
/// agent-roster.py `Roster.jump`'s timeout.
pub const JUMP_TIMEOUT: Duration = Duration::from_secs(10);
/// agent-roster.py `WATCHER_STALE`: same grace as ensure_watcher.
pub const WATCHER_STALE: f64 = 30.0;

/// The installed scripts directory: `$AGENT_UI_SCRIPTS`, else
/// `~/.config/tmux/scripts` (agent-roster.py `SCRIPTS`).
pub fn scripts_dir() -> PathBuf {
    if let Ok(d) = std::env::var("AGENT_UI_SCRIPTS") {
        if !d.is_empty() {
            return PathBuf::from(d);
        }
    }
    let home = std::env::var("HOME").unwrap_or_default();
    PathBuf::from(home).join(".config/tmux/scripts")
}

/// Single-quote for /bin/sh, and double every `#` because run-shell expands
/// its command as a tmux format first.
pub fn sh_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\\''").replace('#', "##"))
}

/// The watcher's pidfile (agent-roster.py `PIDFILE`).
pub fn watcher_pidfile() -> PathBuf {
    let base = std::env::var("TMPDIR").ok().filter(|s| !s.is_empty()).unwrap_or_else(|| "/tmp".into());
    PathBuf::from(base).join(format!("agent-tab-watcher.{}.pid", crate::tmux::uid()))
}

/// agent-roster.py `watcher_age`: seconds since the watcher touched its
/// pidfile, None when there is none.
pub fn watcher_age() -> Option<f64> {
    let m = std::fs::metadata(watcher_pidfile()).ok()?.modified().ok()?;
    let age = std::time::SystemTime::now().duration_since(m).map(|d| d.as_secs_f64()).unwrap_or(0.0);
    Some(age)
}

/// None while the watcher is healthy; else "not running" / "stalled 42s"
/// (the popup's banner words; the strip says "off" / "stalled").
pub fn watcher_problem(age: Option<f64>) -> Option<String> {
    match age {
        None => Some("not running".into()),
        Some(a) if a > WATCHER_STALE => Some(format!("stalled {}s", a as i64)),
        _ => None,
    }
}

/// agent-roster.py `is_agent_comm`: a ps comm whose basename is claude or
/// codex, or Claude's version-named binary ("2.1.291").
pub fn is_agent_comm(comm: &str) -> bool {
    let base = comm.rsplit('/').next().unwrap_or(comm);
    if base == "claude" || base == "codex" {
        return true;
    }
    let parts: Vec<&str> = base.split('.').collect();
    parts.len() == 3 && parts.iter().all(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()))
}

/// agent-roster.py `pick_agent_pane`: the pane `x` closes → Ok(pane_id) or
/// Err(why). `panes_text`: `list-panes -F pane_id US pane_tty US pane_active`;
/// `ps_text`: `ps -ax -o tty=,comm=` (None when not needed or it failed).
pub fn pick_agent_pane(panes_text: &str, ps_text: Option<&str>) -> Result<String, &'static str> {
    let rows: Vec<Vec<&str>> =
        panes_text.lines().filter(|l| l.matches(US).count() == 2).map(|l| l.split(US).collect()).collect();
    if rows.is_empty() {
        return Err(PANE_GONE);
    }
    if rows.len() == 1 {
        return Ok(rows[0][0].to_string());
    }
    let mut agent_ttys = std::collections::HashSet::new();
    for l in ps_text.unwrap_or("").lines() {
        let l = l.trim();
        if let Some((tty, comm)) = l.split_once(char::is_whitespace) {
            if tty != "??" && is_agent_comm(comm.trim()) {
                agent_ttys.insert(tty.to_string());
            }
        }
    }
    let hits: Vec<(&str, &str)> = rows
        .iter()
        .filter(|r| !r[1].is_empty() && agent_ttys.contains(r[1].strip_prefix("/dev/").unwrap_or(r[1])))
        .map(|r| (r[0], r[2]))
        .collect();
    if hits.len() == 1 {
        return Ok(hits[0].0.to_string());
    }
    let active: Vec<&str> = hits.iter().filter(|(_, a)| *a == "1").map(|(p, _)| *p).collect();
    if active.len() == 1 {
        return Ok(active[0].to_string());
    }
    Err(PANE_UNSURE)
}

/// The action runner. Cheap to clone; holds no state.
#[derive(Debug, Clone)]
pub struct Actions {
    pub tmux: Tmux,
    pub scripts: PathBuf,
}

impl Actions {
    pub fn new(tmux: Tmux) -> Actions {
        Actions { tmux, scripts: scripts_dir() }
    }

    fn script(&self, name: &str) -> PathBuf {
        self.scripts.join(name)
    }

    fn run_bg_script(&self, name: &str, args: &[&str]) {
        let mut cmd = sh_quote(&self.script(name).to_string_lossy());
        for a in args {
            cmd.push(' ');
            cmd.push_str(&sh_quote(a));
        }
        self.tmux.run_bg(&cmd);
    }

    /// agent-roster.py `Roster.jump`: `bash agent-jump.sh <mode> <tty> [args]`.
    /// Ok once it ran (the popup then closes); Err with the footer text on a
    /// hang or a missing bash.
    pub fn jump(&self, mode: &str, client: &str, rest: &[&str]) -> Result<(), String> {
        let mut c = Command::new("bash");
        c.arg(self.script("agent-jump.sh")).arg(mode).arg(client).args(rest);
        self.tmux.script_env(&mut c);
        match proc::run(c, None, JUMP_TIMEOUT) {
            Ok(_) => Ok(()),
            Err(RunError::Timeout) => Err(format!("agent-jump.sh {mode} timed out")),
            Err(RunError::Spawn(e)) => Err(format!("agent-jump.sh {mode} failed: {e}")),
        }
    }

    /// agent-roster.py `Roster.go`: go to window `win` as listed under
    /// `session`. A parked row comes back through `stash.sh unstash`. The
    /// session is passed on, so a linked window lands in the session the row
    /// showed, not whichever agent-jump.sh would prefer.
    pub fn go(&self, snap: &Snapshot, client: &str, win: &str, session: &str) -> Result<(), String> {
        if snap.rows(win).next().is_none() {
            return Err(PANE_GONE.into());
        }
        if snap.row_in(win, session).is_none() {
            return Err(format!("{TAB_MOVED} · pick it again"));
        }
        if session == HOLD {
            self.unstash(client, win);
            return Ok(());
        }
        self.jump("goto", client, &[win, session])
    }

    /// `stash.sh unstash <win> <client>` on the tmux server (outlives us).
    pub fn unstash(&self, client: &str, win: &str) {
        self.run_bg_script("stash.sh", &["unstash", win, client]);
    }

    /// agent-jump.sh next: the next window that needs you.
    pub fn next(&self, client: &str) -> Result<(), String> {
        self.jump("next", client, &[])
    }

    /// agent-jump.sh back: undo a run of `next`s.
    pub fn back(&self, client: &str) -> Result<(), String> {
        self.jump("back", client, &[])
    }

    /// Strip.click on a session header: goto that session's ACTIVE window,
    /// in THAT session (even for a linked window). Select-then-switch, so the
    /// visit discharges the tint like a tab click.
    pub fn goto_session(&self, snap: &Snapshot, client: &str, session: &str) -> Result<(), String> {
        let w = snap
            .windows
            .iter()
            .find(|w| w.session == session && w.active)
            .ok_or_else(|| format!("session {session} is gone"))?;
        self.jump("goto", client, &[&w.id, session])
    }

    /// The current row of `win` in `session`, or the Python's refusal text.
    fn recheck<'a>(&self, snap: &'a Snapshot, win: &str, session: &str) -> Result<&'a Window, String> {
        if snap.rows(win).next().is_none() {
            return Err(PANE_GONE.into());
        }
        snap.row_in(win, session).ok_or_else(|| TAB_MOVED.to_string())
    }

    /// `H`/`s` after its y: park the tab (stash.sh SIGTERMs, i.e. suspends,
    /// an idle agent; it refuses a busy one on the status line). Ok carries
    /// the footer text ("parking <label>…": a request, not a fact).
    pub fn park(&self, snap: &Snapshot, win: &str, session: &str) -> Result<String, String> {
        let w = self.recheck(snap, win, session)?;
        if w.session == HOLD {
            return Err("already parked".into());
        }
        self.run_bg_script("stash.sh", &["stash", &w.id]);
        Ok(format!("parking {}…", w.label))
    }

    /// `x` after its y. A parked tab goes through stash.sh's own discard
    /// (`kill-many`, which logs a suspended conversation's id); anything
    /// else closes the agent's pane through closed-tabs.sh (⌘Z restores it),
    /// or is refused when the pane can't be told.
    pub fn close(&self, snap: &Snapshot, win: &str, session: &str) -> Result<String, String> {
        let w = self.recheck(snap, win, session)?;
        if w.session == HOLD {
            self.run_bg_script("stash.sh", &["kill-many", &w.id]);
            return Ok(format!("discarded {} · its session id is in the stash log", w.label));
        }
        let pane = self.agent_pane(&w.id)?;
        self.run_bg_script("closed-tabs.sh", &["close", &pane]);
        Ok(format!("closed {} · ⌘Z brings it back", w.label))
    }

    /// `r` / the strip's ⟳: restart the tab watcher on the server.
    pub fn restart_watcher(&self) -> String {
        let cmd = format!("bash {}", sh_quote(&self.script("agent-tab-watcher.sh").to_string_lossy()));
        self.tmux.run_bg(&cmd);
        "watcher restarted".into()
    }

    /// agent-roster.py `agent_pane`: pick_agent_pane against the live
    /// server; `ps` only runs for a split window.
    pub fn agent_pane(&self, win: &str) -> Result<String, String> {
        let f = ["#{pane_id}", "#{pane_tty}", "#{pane_active}"].join(&US.to_string());
        let out = match self.tmux.run(&["list-panes", "-t", win, "-F", &f]) {
            Ok(o) if o.ok() => o.stdout,
            _ => return Err(PANE_GONE.into()),
        };
        let n = out.lines().filter(|l| l.matches(US).count() == 2).count();
        let ps = if n > 1 {
            let mut c = Command::new("ps");
            c.args(["-ax", "-o", "tty=,comm="]);
            proc::run(c, None, Duration::from_secs(5)).ok().map(|o| o.stdout)
        } else {
            None
        };
        pick_agent_pane(&out, ps.as_deref()).map_err(String::from)
    }

    /// agent-roster.py `peek_lines`, with colours: the last `n` lines of the
    /// agent's pane, trailing blank lines dropped → None when tmux could not
    /// capture it. A split window asks [`agent_pane`](Self::agent_pane) first
    /// and falls back to the window's active pane when it can't tell.
    pub fn peek(&self, w: &Window, n: usize) -> Option<Vec<StyledLine>> {
        let mut target = w.id.clone();
        if w.panes > 1 {
            if let Ok(p) = self.agent_pane(&w.id) {
                target = p;
            }
        }
        let start = format!("-{n}");
        let out = match self.tmux.run(&["capture-pane", "-p", "-e", "-J", "-S", &start, "-t", &target]) {
            Ok(o) if o.ok() => o.stdout,
            _ => return None,
        };
        Some(peek_from_capture(&out, n))
    }
}

/// The pure half of [`Actions::peek`]: capture text → the last `n` lines,
/// each right-trimmed, trailing blank lines dropped.
pub fn peek_from_capture(out: &str, n: usize) -> Vec<StyledLine> {
    let mut lines: Vec<StyledLine> = out
        .lines()
        .map(|l| {
            let mut s = ansi::parse_line(l);
            s.trim_end();
            s
        })
        .collect();
    while lines.last().is_some_and(|l| l.plain().is_empty()) {
        lines.pop();
    }
    let k = lines.len().saturating_sub(n);
    lines.split_off(k)
}

#[cfg(test)]
mod tests {
    use super::*;

    const P: &str = "%1\x1f/dev/ttys001\x1f1\n%2\x1f/dev/ttys002\x1f0";

    #[test]
    fn single_pane_is_it() {
        assert_eq!(pick_agent_pane("%7\x1f/dev/ttys009\x1f1", None), Ok("%7".into()));
        assert_eq!(pick_agent_pane("", None), Err(PANE_GONE));
        assert_eq!(pick_agent_pane("garbage", None), Err(PANE_GONE));
    }

    #[test]
    fn matched_by_tty_not_active() {
        let ps = "ttys001 -zsh\nttys002 /opt/homebrew/bin/claude\n?? claude\n";
        assert_eq!(pick_agent_pane(P, Some(ps)), Ok("%2".into()));
        let ps = "ttys002 2.1.291\n";
        assert_eq!(pick_agent_pane(P, Some(ps)), Ok("%2".into()));
    }

    #[test]
    fn refuses_to_guess() {
        // No agent at all: never the active pane by default.
        assert_eq!(pick_agent_pane(P, Some("ttys001 zsh\nttys002 node\n")), Err(PANE_UNSURE));
        assert_eq!(pick_agent_pane(P, None), Err(PANE_UNSURE));
        // Two agents: the active one.
        assert_eq!(pick_agent_pane(P, Some("ttys001 claude\nttys002 codex\n")), Ok("%1".into()));
        // Two agents, neither active.
        let p = "%1\x1f/dev/ttys001\x1f0\n%2\x1f/dev/ttys002\x1f0\n%3\x1f/dev/ttys003\x1f1";
        assert_eq!(pick_agent_pane(p, Some("ttys001 claude\nttys002 codex\n")), Err(PANE_UNSURE));
    }

    #[test]
    fn agent_comms() {
        for c in ["claude", "/usr/local/bin/codex", "2.1.291", "/x/10.0.1"] {
            assert!(is_agent_comm(c), "{c}");
        }
        for c in ["node", "claude-code", "2.1", "2.1.x", "1..2", "zsh", "v2.1.3"] {
            assert!(!is_agent_comm(c), "{c}");
        }
    }

    #[test]
    fn quoting() {
        assert_eq!(sh_quote("/a b/c"), "'/a b/c'");
        assert_eq!(sh_quote("it's#1"), "'it'\\''s##1'");
    }

    #[test]
    fn peeks() {
        let out = "one\n\x1b[31mtwo\x1b[0m   \nthree\n\n   \n";
        let got = peek_from_capture(out, 2);
        assert_eq!(got.iter().map(|l| l.plain()).collect::<Vec<_>>(), ["two", "three"]);
        assert!(peek_from_capture("\n\n", 5).is_empty());
    }

    #[test]
    fn watcher_words() {
        assert_eq!(watcher_problem(None).as_deref(), Some("not running"));
        assert_eq!(watcher_problem(Some(42.7)).as_deref(), Some("stalled 42s"));
        assert_eq!(watcher_problem(Some(3.0)), None);
    }
}
