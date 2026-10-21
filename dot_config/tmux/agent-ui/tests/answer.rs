//! `agent-ui answer` end to end against a REAL private tmux server (`tmux -L
//! agentui-answer-<pid>`, `-f /dev/null`, killed by a Drop guard): panes
//! that draw a captured Claude Code permission dialog (or another screen)
//! and record the first key they get. allow types the Yes option's digit,
//! deny Escape; a pane without the dialog gets nothing, and the refusal
//! goes through notify.sh (`$NOTIFY_SH`, a stub here). A sent answer
//! discharges the window through the real agent-tab-indicator.sh
//! (`clear-current`, from the chezmoi source); its server runs with a
//! private HOME and TMPDIR, so the indicator's watcher watchdog finds no
//! pidfile and "respawns" a stub, never touching the real watcher.

use agent_ui::answer::{plan, Asked, Dialog, PaneState, Verdict};
use agent_ui::{Socket, Tmux};
use std::path::PathBuf;
use std::process::{Command, Output};
use std::time::{Duration, Instant};

const BIN: &str = env!("CARGO_BIN_EXE_agent-ui");
const FIXTURES: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/src/answer/fixtures");
const SCRIPTS: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../scripts");

struct Server {
    name: String,
    dir: PathBuf,
}

impl Server {
    /// `tag`: one server per test (they run in parallel).
    fn start(tag: &str) -> Server {
        let name = format!("agentui-answer-{tag}-{}", std::process::id());
        let dir = std::env::temp_dir().join(&name);
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("home/.config/tmux/scripts")).unwrap();
        let fake = "#!/bin/bash\nclear; cat \"$1\"\nstty -echo -icanon min 1\n\
                    k=$(dd bs=1 count=1 2>/dev/null | od -An -tx1 | tr -d ' \\n')\nprintf '%s' \"$k\" > \"$2\"\nexec sleep 600\n";
        let notify = format!("#!/bin/bash\nprintf '%s\\n' \"$*\" >> {}\n", dir.join("notify.log").display());
        let watcher = format!("#!/bin/bash\necho spawned >> {}\n", dir.join("watcher.log").display());
        for (f, body) in [("fake.sh", fake.to_string()), ("notify.sh", notify), ("home/.config/tmux/scripts/agent-tab-watcher.sh", watcher)] {
            std::fs::write(dir.join(f), body).unwrap();
            Command::new("chmod").arg("+x").arg(dir.join(f)).status().unwrap();
        }
        let s = Server { name, dir };
        // The server's environment is what run-shell children (the
        // indicator) get: a private HOME and TMPDIR, no state notices.
        let o = s.cmd()
            .args(["new-session", "-d", "-s", "main", "-x", "120", "-y", "40", "sleep 600"])
            .env("HOME", s.dir.join("home"))
            .env("TMPDIR", &s.dir)
            .env("AGENT_STATE_NOTICE", "")
            .output()
            .unwrap();
        assert!(o.status.success(), "{o:?}");
        s
    }

    fn cmd(&self) -> Command {
        let mut c = Command::new("tmux");
        c.args(["-L", &self.name, "-f", "/dev/null"]).env_remove("TMUX").env_remove("TMUX_PANE");
        c
    }

    fn tmux(&self, args: &[&str]) -> String {
        let o: Output = self.cmd().args(args).output().expect("run tmux");
        assert!(o.status.success(), "tmux {args:?}: {}", String::from_utf8_lossy(&o.stderr));
        String::from_utf8_lossy(&o.stdout).trim().to_string()
    }

    /// A window drawing `screen` (a fixture) as agent `kind` with detail
    /// kind `dk`, needs-input → its id. Waits until the screen is drawn.
    fn agent(&self, name: &str, screen: &str, kind: &str, dk: &str) -> String {
        let cmd = format!("{} {FIXTURES}/{screen}.txt {}", self.dir.join("fake.sh").display(), self.out(name).display());
        let id = self.tmux(&["new-window", "-d", "-P", "-F", "#{window_id}", "-t", "main:", &cmd]);
        for (k, v) in [("state", "needs-input"), ("kind", kind), ("detail_kind", dk), ("summary", name),
            ("since", "1700000000 needs-input")] {
            self.tmux(&["set-option", "-w", "-t", &id, &format!("@agent_{k}"), v]);
        }
        let t0 = Instant::now();
        while !self.tmux(&["capture-pane", "-p", "-t", &id]).contains(if screen == "answered" { "manual mode" } else { "to cancel" }) {
            assert!(t0.elapsed() < Duration::from_secs(5), "{name} never drew");
            std::thread::sleep(Duration::from_millis(50));
        }
        id
    }

    fn out(&self, name: &str) -> PathBuf {
        self.dir.join(format!("{name}.key"))
    }

    /// The key the pane got (hex), waiting a moment for it; "" for none.
    fn got(&self, name: &str) -> String {
        let t0 = Instant::now();
        while t0.elapsed() < Duration::from_millis(1500) {
            if let Ok(s) = std::fs::read_to_string(self.out(name)) {
                return s;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        String::new()
    }

    fn answer(&self, win: &str, verdict: &str) -> Output {
        Command::new(BIN)
            .args(["--socket", &self.name, "answer", win, verdict])
            .env("NOTIFY_SH", self.dir.join("notify.sh"))
            .env("AGENT_UI_SCRIPTS", SCRIPTS)
            .env_remove("TMUX")
            .env_remove("TMUX_PANE")
            .output()
            .expect("run agent-ui")
    }

    fn opt(&self, win: &str, name: &str) -> String {
        self.tmux(&["show-options", "-wqv", "-t", win, name])
    }

    fn notified(&self) -> String {
        std::fs::read_to_string(self.dir.join("notify.log")).unwrap_or_default()
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.cmd().arg("kill-server").output();
        let _ = std::fs::remove_file(Socket::Name(self.name.clone()).path());
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

#[test]
fn answers_only_a_dialog_on_screen() {
    let s = Server::start("a");
    let allow = s.agent("allow", "perm_bash", "claude", "perm");
    let deny = s.agent("deny", "perm_write", "claude", "perm");
    let plain = s.agent("plain", "answered", "claude", "perm");
    let ask = s.agent("ask", "ask", "claude", "perm");
    let codex = s.agent("codex", "perm_bash", "codex", "perm");

    let o = s.answer(&allow, "allow");
    assert!(o.status.success(), "{o:?}");
    assert_eq!(String::from_utf8_lossy(&o.stdout).trim(), "allowed allow");
    assert_eq!(s.got("allow"), "31"); // "1": the single-use Yes
    let o = s.answer(&deny, "deny");
    assert!(o.status.success(), "{o:?}");
    assert_eq!(s.got("deny"), "1b"); // Escape

    // Both discharged as a look at the tab would: idle, @agent_pending
    // stamped (the indicator's clear-current), so neither card stays.
    for w in [&allow, &deny] {
        assert_eq!(s.opt(w, "@agent_state"), "idle", "{w}");
        assert!(s.opt(w, "@agent_pending").parse::<i64>().is_ok_and(|t| t > 1_700_000_000), "{w}");
    }
    // A second click on the old card (this fake still draws its dialog):
    // the window is no longer waiting, nothing sent.
    let o = s.answer(&deny, "allow");
    assert_eq!(o.status.code(), Some(1), "{o:?}");
    assert!(String::from_utf8_lossy(&o.stderr).contains("deny: no permission prompt pending"), "{o:?}");

    for (win, name, why) in [
        (&plain, "plain", "plain: no permission prompt on screen"),
        (&ask, "ask", "ask: that's a question, not a permission prompt"),
        (&codex, "codex", "codex: answer Codex prompts in its tab"),
    ] {
        let o = s.answer(win, "allow");
        assert_eq!(o.status.code(), Some(1), "{name}: {o:?}");
        assert_eq!(s.got(name), "", "{name} got a key");
        assert_eq!(s.opt(win, "@agent_state"), "needs-input", "{name}");
        assert!(s.notified().contains(&format!("-k warn -- {why}")), "{name}: {}", s.notified());
    }
    // Usage.
    assert_eq!(s.answer(&allow, "maybe").status.code(), Some(2));
}

/// A pane left in copy mode captures its live screen but would take the
/// keys as copy-mode commands: the mode is cancelled in the same command,
/// then the key lands. Synchronized panes are refused (every pane would
/// get it).
#[test]
fn copy_mode_and_synchronized_panes() {
    let s = Server::start("c");
    let copy = s.agent("copy", "perm_bash", "claude", "perm");
    s.tmux(&["copy-mode", "-t", &copy]);
    assert_eq!(s.tmux(&["display-message", "-p", "-t", &copy, "#{pane_in_mode}"]), "1");
    let o = s.answer(&copy, "allow");
    assert!(o.status.success(), "{o:?}");
    assert_eq!(s.got("copy"), "31");
    assert_eq!(s.tmux(&["display-message", "-p", "-t", &copy, "#{pane_in_mode}"]), "0");

    let sync = s.agent("sync", "perm_bash", "claude", "perm");
    s.tmux(&["set-option", "-w", "-t", &sync, "synchronize-panes", "on"]);
    let o = s.answer(&sync, "deny");
    assert_eq!(o.status.code(), Some(1), "{o:?}");
    assert!(String::from_utf8_lossy(&o.stderr).contains("synchronize-panes on"), "{o:?}");
    assert_eq!(s.got("sync"), "");
}

/// The send is conditional on the state read with the capture: a pane that
/// moved in between (a stale cursor_y), or a window asked again in between
/// (another `@agent_since` or `@agent_detail`), gets nothing.
#[test]
fn a_pane_that_changed_gets_nothing() {
    let s = Server::start("m");
    let w = s.agent("moved", "perm_bash", "claude", "perm");
    let detail = "Bash a,b {x} }y \"q\" $(id)";
    s.tmux(&["set-option", "-w", "-t", &w, "@agent_detail", detail]);
    let since = s.opt(&w, "@agent_since");
    let pane = s.tmux(&["display-message", "-p", "-t", &w, "#{pane_id}"]);
    let line = s.tmux(&["display-message", "-p", "-t", &pane, agent_ui::answer::STATE_FMT]);
    let st = PaneState::parse(&line).unwrap();
    let tmux = Tmux::with_socket(Some(Socket::Name(s.name.clone())));
    let d = Dialog { yes: 1, no: 4 };
    let asked = Asked { since: &since, detail };
    let run = |st: &PaneState, asked: Asked| tmux.run(&plan(st, d, Verdict::Allow, &pane, asked).unwrap()).unwrap().stdout;
    assert_eq!(run(&PaneState { cursor_y: st.cursor_y + 1, ..st.clone() }, asked).trim(), "changed");
    // Re-asked: the window's options moved on from what the snapshot read.
    assert_eq!(run(&st, Asked { since: "1700000001 needs-input", ..asked }).trim(), "changed");
    assert_eq!(run(&st, Asked { detail: "Bash a,b {x} }y \"q\" $(id) ", ..asked }).trim(), "changed");
    assert_eq!(s.got("moved"), "");
    assert_eq!(run(&st, asked).trim(), "sent");
    assert_eq!(s.got("moved"), "31");
}
