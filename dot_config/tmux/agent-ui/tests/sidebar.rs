//! The sidebar end to end, against a REAL private tmux server (`tmux -L
//! agentui-sb-<pid>-<tag>`, `-f /dev/null`): `--once` over a fixture, and the
//! live loop running in one of that server's panes (a real tty), driven with
//! raw SGR mouse bytes and Darwin notices under test-only names. The client
//! is the fake `/dev/ttys999` unless a test attaches a nested one, so no
//! action can ever reach a real tmux client. Every server is killed (and its
//! socket removed) by a Drop guard; its windows run `sleep 600`, so even a
//! SIGKILLed run leaves nothing behind for long.

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

    /// A nested `tmux attach -t <session>` in a pane of this same server: a
    /// REAL client (its tty is that pane's) → (its tty, the pane it runs in).
    fn nested_client(&self, session: &str, at: &str) -> String {
        let attach = format!("env -u TMUX -u TMUX_PANE tmux -L {} -f /dev/null attach -t {session}", self.name);
        self.tmux(&["new-window", "-d", "-t", at, &attach]);
        let mut tty = String::new();
        wait_for("the nested client", || {
            tty = self
                .tmux(&["list-clients", "-F", "#{client_tty} #{session_name}"])
                .lines()
                .find_map(|l| l.strip_suffix(&format!(" {session}")).map(String::from))
                .unwrap_or_default();
            !tty.is_empty()
        });
        tty
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
        std::thread::sleep(Duration::from_millis(10));
    }
}

/// The index of the first line starting with `p`.
fn at(lines: &[String], p: &str) -> usize {
    lines.iter().position(|l| l.starts_with(p)).unwrap_or_else(|| panic!("{p:?} not in {lines:#?}"))
}

#[test]
fn once_renders_the_fixture() {
    let s = Server::start("once", 80, 24);
    s.fixture();
    let out = s.sidebar(&["--once", "34x30", "--client", CLIENT]);
    // The terminal's own background: no background colour anywhere (no
    // client here, so no row is tinted).
    assert!(!out.contains(";48;"), "{out:?}");
    let lines = plain(&out);
    assert_eq!(lines.len(), 30);
    for l in &lines {
        assert_eq!(l.chars().count(), 34, "{l:?}");
    }
    assert!(lines[0].starts_with(" agents") && lines[0].contains("✕1 ◉1 ◐1"), "{lines:#?}");
    assert!(lines[2..].iter().find(|l| !l.trim().is_empty()).unwrap().starts_with(" needs you"));
    // Needs in agent-jump.sh order: failed first; each card starts with the
    // agent's number.
    let fail = at(&lines, "▌1 ✕ Broken thing");
    let ask = at(&lines, "▌2 ◉ proj/Which deck");
    assert!(fail < ask);
    // Then the agents, then the spaces list at the bottom, then parked.
    let agents = ask + at(&lines[ask..], " agents ");
    assert!(lines[agents].ends_with("by space "), "{lines:#?}");
    for t in [" 5 ◐ Long running build ⚙", "   ○ Quiet one", " 2 ◉ Which deck", " 1 ✕ Broken thing"] {
        assert!(lines[agents..].iter().any(|l| l.starts_with(t)), "{t}: {lines:#?}");
    }
    for t in [" work ", " zeta "] {
        assert!(lines[agents..].iter().any(|l| l.starts_with(t) && l.contains('─')), "{t}: {lines:#?}");
    }
    let spaces = at(&lines, " spaces");
    assert_eq!(&lines[spaces + 1..29].iter().map(|l| l.split_whitespace().nth(1).unwrap()).collect::<Vec<_>>(),
        &["main", "work", "zeta"], "{lines:#?}");
    assert!(lines[29].starts_with(" ▸ parked 0"));
    assert!(!lines.iter().any(|l| l.starts_with(" log") || l.contains('═')), "{lines:#?}");
    // `--split` (gone) is ignored, not an error.
    assert_eq!(plain(&s.sidebar(&["--once", "34x30", "--client", CLIENT, "--split", "0.7"])), lines);
}

/// The scope end to end: a REAL client gives the sidebar its session and the
/// global @spaces / @strip_scope in the one snapshot call.
#[test]
fn once_follows_the_scope_options() {
    let s = Server::start("scope", 80, 30);
    s.fixture();
    let tty = s.nested_client("work", "main:8");
    let once = || plain(&s.sidebar(&["--once", "34x30", "--client", &tty]));
    let agents_part = |lines: &[String]| {
        let start = lines.iter().position(|l| l.starts_with(" agents ") && (l.ends_with("by space ") || l.ends_with("this space ")));
        lines[start.expect("the agents section")..at(lines, " spaces")].to_vec()
    };
    // Classic until an option says otherwise.
    let classic = once();
    assert!(classic[0].starts_with(" agents") && agents_part(&classic).iter().any(|l| l.starts_with(" zeta ")),
        "{classic:#?}");
    s.tmux(&["set-option", "-g", "@spaces", "on"]);
    let lines = once();
    assert!(lines[0].starts_with(" space") && lines[0].contains("◉1 ◐1 ○1") && !lines[0].contains('✕'), "{lines:#?}");
    let body = agents_part(&lines);
    for t in [" 1 ◉ Which deck", " 2 ◐ Long running build ⚙", " 3 ○ Quiet one"] {
        assert!(body.iter().any(|l| l.starts_with(t)), "{t}: {lines:#?}");
    }
    assert!(!body.iter().any(|l| l.contains("zeta") || l.contains("Broken")), "{lines:#?}");
    // NEEDS YOU stays global, unnumbered.
    assert!(lines.iter().any(|l| l.starts_with("▌✕ Broken thing")), "{lines:#?}");
    s.tmux(&["set-option", "-g", "@strip_scope", "all"]);
    let lines = once();
    assert!(lines[0].starts_with(" agents") && agents_part(&lines).iter().any(|l| l.starts_with(" zeta ")), "{lines:#?}");
    // Back to classic: byte for byte what it drew before any option was set.
    s.tmux(&["set-option", "-gu", "@strip_scope"]);
    s.tmux(&["set-option", "-gu", "@spaces"]);
    assert_eq!(once(), classic);
    // A space's home window (@space_home 1) is never a row, even the
    // client's own with @spaces off, and never counted.
    s.tmux(&["new-window", "-d", "-t", "work:0", "-n", "home", LIFE]);
    s.tmux(&["set-option", "-w", "-t", "work:0", "@space_home", "1"]);
    s.tmux(&["switch-client", "-c", &tty, "-t", "work:0"]);
    let lines = once();
    assert!(!lines.iter().any(|l| l.contains("home")), "{lines:#?}");
    assert!(lines.iter().any(|l| l.starts_with(" ● work")), "{lines:#?}");
}

/// Test-only notice names, so nothing real wakes (and real posts do not
/// wake the test's sidebar).
fn notice_names(tag: &str) -> (String, String) {
    let base = format!("com.mackhaymond.agent-ui.test.{}.{tag}", std::process::id());
    (format!("{base}.state"), format!("{base}.focus"))
}

fn post(name: &str) {
    assert!(Command::new("notifyutil").args(["-p", name]).status().unwrap().success());
}

#[test]
fn live_loop_draws_follows_notices_and_exits_quietly() {
    let s = Server::start("live", 34, 30);
    s.fixture();
    // Nothing pulsing (the running agent goes idle), so the timer is the 5 s
    // safety net and only a notice can make the sidebar look sooner.
    s.tmux(&["set-option", "-w", "-t", "work:2", "@agent_state", "idle"]);
    s.tmux(&["set-option", "-g", "remain-on-exit", "on"]);
    let (state, focus) = notice_names("live");
    let cmd = format!("env AGENT_STATE_NOTICE={state} AGENT_FOCUS_NOTICE={focus} {BIN} --socket {} sidebar --client {CLIENT}", s.name);
    s.tmux(&["new-window", "-d", "-t", "main:9", "-n", "sb", &cmd]);
    let screen = || s.tmux(&["capture-pane", "-p", "-t", "main:9"]);
    wait_for("the first frame", || screen().contains("needs you"));
    // The marker CMD+B finds the pane by: the title.
    assert_eq!(s.tmux(&["display-message", "-p", "-t", "main:9", "#{pane_title}"]).trim(), "agent-strip");
    std::thread::sleep(Duration::from_millis(300));

    // A state change, then its notice: on screen well before the 5 s poll.
    s.tmux(&["set-option", "-w", "-t", "work:3", "@agent_state", "needs-input"]);
    let t = Instant::now();
    post(&state);
    wait_for("the state notice", || screen().contains("▌3 ◉ Quiet one") || screen().contains("◉ Quiet one"));
    assert!(t.elapsed() < Duration::from_millis(1500), "{:?}", t.elapsed());
    // A rename with the focus notice, the same.
    s.tmux(&["rename-session", "-t", "zeta", "omega"]);
    let t = Instant::now();
    post(&focus);
    wait_for("the focus notice", || screen().contains(" omega"));
    assert!(t.elapsed() < Duration::from_millis(1500), "{:?}", t.elapsed());

    // The wheel scrolls nothing; a toolbar click runs `next` on the fake
    // client (refused by agent-jump.sh, never reaching a real one) and the
    // sidebar keeps running.
    let send = |bytes: &str| {
        let hex: Vec<String> = bytes.bytes().map(|b| format!("{b:02x}")).collect();
        let mut a = vec!["send-keys", "-t", "main:9", "-H"];
        a.extend(hex.iter().map(String::as_str));
        s.tmux(&a);
    };
    send("\x1b[<64;5;20M\x1b[<0;3;2M\x1b[<0;3;2m");
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(s.tmux(&["display-message", "-p", "-t", "main:9", "#{pane_dead}"]).trim(), "0");
    // Option-V: the scope toggle, persisted in the global @strip_scope
    // (nothing set yet: scope "all", so it flips to "space").
    send("\x1bv");
    wait_for("the scope option", || s.tmux(&["show-options", "-gqv", "@strip_scope"]).trim() == "space");

    // SIGTERM (CMD+B's kill-pane): a quiet exit, status 0, screen restored.
    let pid = s.tmux(&["display-message", "-p", "-t", "main:9", "#{pane_pid}"]);
    Command::new("kill").args(["-TERM", pid.trim()]).status().unwrap();
    wait_for("the exit", || s.tmux(&["display-message", "-p", "-t", "main:9", "#{pane_dead}"]).trim() == "1");
    assert_eq!(s.tmux(&["display-message", "-p", "-t", "main:9", "#{pane_dead_status}"]).trim(), "0");
    assert_eq!(s.tmux(&["display-message", "-p", "-t", "main:9", "#{alternate_on}"]).trim(), "0");
}

/// Never alone: when the tmux client it sits beside detaches, the sidebar
/// exits (status 0) within moments, with no notice and no poll to help.
#[test]
fn the_sidebar_leaves_with_its_client() {
    let s = Server::start("alone", 34, 30);
    s.fixture();
    s.tmux(&["set-option", "-g", "remain-on-exit", "on"]);
    let tty = s.nested_client("work", "main:8");
    let (state, focus) = notice_names("alone");
    let cmd = format!("env AGENT_STATE_NOTICE={state} AGENT_FOCUS_NOTICE={focus} {BIN} --socket {} sidebar --client {tty}", s.name);
    s.tmux(&["new-window", "-d", "-t", "main:9", "-n", "sb", &cmd]);
    let screen = || s.tmux(&["capture-pane", "-p", "-t", "main:9"]);
    wait_for("the first frame", || screen().contains("needs you"));
    std::thread::sleep(Duration::from_millis(300));
    assert!(screen().starts_with(" agents"), "{}", screen());
    // @strip_scope set from outside (tmux's M-v) plus the focus notice: the
    // header follows at once, and back.
    for (scope, title) in [("space", " space"), ("all", " agents")] {
        s.tmux(&["set-option", "-g", "@strip_scope", scope]);
        let t = Instant::now();
        post(&focus);
        wait_for("the scope from outside", || screen().starts_with(title));
        assert!(t.elapsed() < Duration::from_millis(1500), "{:?}", t.elapsed());
    }
    // A window switch on the client, then the focus notice (tmux's hooks
    // post it): the highlight follows.
    s.tmux(&["select-window", "-t", "work:3"]);
    post(&focus);
    wait_for("the window switch", || screen().lines().any(|l| l.contains("Quiet one") && l.contains("here")));
    assert_eq!(s.tmux(&["display-message", "-p", "-t", "main:9", "#{pane_dead}"]).trim(), "0");
    // Suspended (prefix C-z): tmux 3.7 drops the client from list-clients,
    // but it is alive, so the sidebar stays (a notice forces a refresh).
    let cpid = s.tmux(&["list-clients", "-F", "#{client_pid}"]).trim().to_string();
    s.tmux(&["suspend-client", "-t", &tty]);
    wait_for("the client to drop out", || !s.tmux(&["list-clients", "-F", "#{client_tty}"]).contains(&tty));
    post(&focus);
    std::thread::sleep(Duration::from_millis(800));
    assert_eq!(s.tmux(&["display-message", "-p", "-t", "main:9", "#{pane_dead}"]).trim(), "0", "left a suspended client");
    Command::new("kill").args(["-CONT", &cpid]).status().unwrap();
    wait_for("the client back", || s.tmux(&["list-clients", "-F", "#{client_tty}"]).contains(&tty));
    post(&focus);
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(s.tmux(&["display-message", "-p", "-t", "main:9", "#{pane_dead}"]).trim(), "0");
    s.tmux(&["detach-client", "-t", &tty]);
    let t = Instant::now();
    wait_for("the sidebar to leave", || s.tmux(&["display-message", "-p", "-t", "main:9", "#{pane_dead}"]).trim() == "1");
    assert!(t.elapsed() < Duration::from_millis(1500), "{:?}", t.elapsed());
    assert_eq!(s.tmux(&["display-message", "-p", "-t", "main:9", "#{pane_dead_status}"]).trim(), "0");
}
