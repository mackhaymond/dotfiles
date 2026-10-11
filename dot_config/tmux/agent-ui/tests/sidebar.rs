//! The sidebar end to end, against a REAL private tmux server (`tmux -L
//! agentui-sb-<pid>-<tag>`, `-f /dev/null`): `--once` over a fixture, and the
//! live loop running in one of that server's panes (a real tty), driven with
//! raw SGR mouse bytes. The client is the fake `/dev/ttys999`, so no action
//! can ever reach a real tmux client. Every server is killed (and its socket
//! removed) by a Drop guard; its windows run `sleep 600`, so even a SIGKILLed
//! run leaves nothing behind for long.

use agent_ui::Socket;
use std::process::{Command, Output};
use std::time::{Duration, Instant};

const BIN: &str = env!("CARGO_BIN_EXE_agent-ui");
const LIFE: &str = "sleep 600";
const CLIENT: &str = "/dev/ttys999";

struct Server {
    name: String,
}

impl Server {
    fn start(tag: &str, w: u16, h: u16) -> Server {
        let s = Server { name: format!("agentui-sb-{}-{tag}", std::process::id()) };
        let (w, h) = (w.to_string(), h.to_string());
        s.tmux(&["new-session", "-d", "-s", "main", "-x", &w, "-y", &h, LIFE]);
        s
    }

    /// Every call carries `-f /dev/null`: any command that ends up starting
    /// a server (a `new-session` after the first died) must never load the
    /// user's tmux.conf, whose restore pipeline would resume real sessions.
    fn cmd(&self) -> Command {
        let mut c = Command::new("tmux");
        c.args(["-L", &self.name, "-f", "/dev/null"])
            .env_remove("TMUX")
            .env_remove("TMUX_PANE")
            .env_remove("WEZTERM_PANE");
        c
    }

    fn tmux(&self, args: &[&str]) -> String {
        let o: Output = self.cmd().args(args).output().expect("run tmux");
        assert!(o.status.success(), "tmux {args:?}: {}", String::from_utf8_lossy(&o.stderr));
        String::from_utf8_lossy(&o.stdout).into_owned()
    }

    /// A window running `sleep`, with agent options set.
    fn agent(&self, session: &str, index: u32, opts: &[(&str, &str)]) {
        let t = format!("{session}:{index}");
        if self.cmd().args(["has-session", "-t", session]).output().unwrap().status.success() {
            self.tmux(&["new-window", "-d", "-t", &t, LIFE]);
        } else {
            self.tmux(&["new-session", "-d", "-s", session, LIFE]);
            self.tmux(&["move-window", "-s", &format!("{session}:0"), "-t", &t]);
        }
        for (k, v) in opts {
            self.tmux(&["set-option", "-w", "-t", &t, &format!("@agent_{k}"), v]);
        }
    }

    /// The watcher never runs here, so the fixture stamps `@agent_slot`
    /// itself: 2 and 5 held, a stale 1 on the idle agent (ignored), and the
    /// failure not stamped yet (numbered at once: the lowest free, 1).
    fn fixture(&self) {
        self.agent("work", 1, &[("state", "needs-input"), ("summary", "proj/Which deck"), ("detail_kind", "ask"),
            ("detail", "pick one"), ("kind", "claude"), ("slot", "2")]);
        self.agent("work", 2, &[("state", "running"), ("summary", "Long running build"), ("workflow", "1"), ("slot", "5")]);
        self.agent("work", 3, &[("state", "idle"), ("summary", "Quiet one"), ("slot", "1")]);
        self.agent("zeta", 1, &[("state", "failed"), ("summary", "Broken thing")]);
    }

    fn sidebar(&self, args: &[&str]) -> String {
        let o = Command::new(BIN)
            .args(["--socket", &self.name, "sidebar"])
            .args(args)
            .env_remove("WEZTERM_PANE")
            .output()
            .expect("run agent-ui");
        assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
        String::from_utf8_lossy(&o.stdout).into_owned()
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.cmd().arg("kill-server").output();
        let _ = std::fs::remove_file(Socket::Name(self.name.clone()).path());
    }
}

fn plain(ansi: &str) -> Vec<String> {
    let mut out = Vec::new();
    for line in ansi.lines() {
        let mut s = String::new();
        let mut esc = false;
        for c in line.chars() {
            match (esc, c) {
                (false, '\x1b') => esc = true,
                (true, 'm') => esc = false,
                (true, _) => {}
                (false, c) => s.push(c),
            }
        }
        out.push(s);
    }
    out
}

fn wait_for(what: &str, mut f: impl FnMut() -> bool) {
    let t = Instant::now();
    while !f() {
        assert!(t.elapsed() < Duration::from_secs(10), "timed out waiting for {what}");
        std::thread::sleep(Duration::from_millis(50));
    }
}

#[test]
fn once_renders_the_fixture() {
    let s = Server::start("once", 80, 24);
    s.fixture();
    let out = s.sidebar(&["--once", "34x30", "--client", CLIENT]);
    let lines = plain(&out);
    assert_eq!(lines.len(), 30);
    for l in &lines {
        assert_eq!(l.chars().count(), 34, "{l:?}");
    }
    assert!(lines[0].starts_with(" agents") && lines[0].contains("✕1 ◉1 ◐1"), "{lines:#?}");
    assert!(lines[2..].iter().find(|l| !l.trim().is_empty()).unwrap().starts_with(" needs you"));
    // Needs in agent-jump.sh order: failed first; each card starts with the
    // agent's number.
    let fail = lines.iter().position(|l| l.starts_with("▌1 ✕ Broken thing")).expect("failed card");
    let ask = lines.iter().position(|l| l.starts_with("▌2 ◉ proj/Which deck")).expect("ask card");
    assert!(fail < ask);
    assert_eq!(lines[15], "────────────────═─────────────────");
    for t in [" 5 ◐ Long running build ⚙", "   ○ Quiet one", " work ", " zeta ", "     asks pick one", " 2 ◉ Which deck",
        " 1 ✕ Broken thing"] {
        assert!(lines[16..].iter().any(|l| l.starts_with(t) || (t.starts_with(" work") || t.starts_with(" zeta")) && l.contains(t)),
            "{t}: {lines:#?}");
    }
    // The split comes from the tmux option, or --split.
    s.tmux(&["set-option", "-g", "@agent_sidebar_split", "0.4"]);
    assert_eq!(plain(&s.sidebar(&["--once", "34x30"]))[12], "────────────────═─────────────────");
    assert_eq!(plain(&s.sidebar(&["--once", "34x30", "--split", "0.7"]))[21], "────────────────═─────────────────");
}

#[test]
fn live_loop_draws_drags_and_exits_quietly() {
    let s = Server::start("live", 34, 30);
    s.fixture();
    s.tmux(&["set-option", "-g", "remain-on-exit", "on"]);
    let cmd = format!("{BIN} --socket {} sidebar --client {CLIENT}", s.name);
    s.tmux(&["new-window", "-d", "-t", "main:9", "-n", "sb", &cmd]);
    let screen = || s.tmux(&["capture-pane", "-p", "-t", "main:9"]);
    wait_for("the first frame", || screen().contains("needs you"));
    let lines: Vec<String> = screen().lines().map(String::from).collect();
    assert!(lines[15].starts_with("────────────────═"), "{lines:#?}");
    // The marker CMD+B finds the pane by: the title.
    assert_eq!(s.tmux(&["display-message", "-p", "-t", "main:9", "#{pane_title}"]).trim(), "agent-strip");

    // Drag the divider from row 16 (1-based) up to row 11, then release.
    let send = |bytes: &str| {
        let hex: Vec<String> = bytes.bytes().map(|b| format!("{b:02x}")).collect();
        let mut a = vec!["send-keys", "-t", "main:9", "-H"];
        a.extend(hex.iter().map(String::as_str));
        s.tmux(&a);
    };
    send("\x1b[<0;17;16M");
    send("\x1b[<32;17;13M\x1b[<32;17;11M");
    send("\x1b[<0;17;11m");
    wait_for("the split option", || s.tmux(&["show-options", "-gqv", "@agent_sidebar_split"]).trim() == "0.3333");
    wait_for("the divider to move", || screen().lines().nth(10).is_some_and(|l| l.starts_with("────────────────═")));
    // The review's repro: press+release on the divider in ONE write, then a
    // click lower down. Neither may move (or save) the divider.
    send("\x1b[<0;17;11M\x1b[<0;17;11m");
    send("\x1b[<0;5;25M\x1b[<0;5;25m");
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(s.tmux(&["show-options", "-gqv", "@agent_sidebar_split"]).trim(), "0.3333");
    assert!(screen().lines().nth(10).is_some_and(|l| l.starts_with("────────────────═")));
    // The wheel scrolls nothing; a toolbar click runs `next` on the fake
    // client (refused by agent-jump.sh, never reaching a real one) and the
    // sidebar keeps running.
    send("\x1b[<64;5;20M\x1b[<0;3;2M\x1b[<0;3;2m");
    std::thread::sleep(Duration::from_millis(300));
    assert!(screen().lines().nth(10).is_some_and(|l| l.starts_with("────────────────═")));
    assert_eq!(s.tmux(&["display-message", "-p", "-t", "main:9", "#{pane_dead}"]).trim(), "0");

    // SIGTERM (CMD+B's kill-pane): a quiet exit, status 0, screen restored.
    let pid = s.tmux(&["display-message", "-p", "-t", "main:9", "#{pane_pid}"]);
    Command::new("kill").args(["-TERM", pid.trim()]).status().unwrap();
    wait_for("the exit", || s.tmux(&["display-message", "-p", "-t", "main:9", "#{pane_dead}"]).trim() == "1");
    assert_eq!(s.tmux(&["display-message", "-p", "-t", "main:9", "#{pane_dead_status}"]).trim(), "0");
    assert_eq!(s.tmux(&["display-message", "-p", "-t", "main:9", "#{alternate_on}"]).trim(), "0");
}
