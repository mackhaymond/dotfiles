//! The Option-W menu against a REAL private tmux server
//! (`tmux -L agentui-menu-<pid>-<tag>`, `-f /dev/null`), never the user's:
//!
//! - `menu --once` renders the server's agents and runs nothing;
//! - the interactive menu, run INSIDE a pane of that server and driven with
//!   `send-keys`: it draws, searches, goes (⏎), closes on Option-W and q, and
//!   parks after a y/n; every script it runs is a recording fake
//!   (`AGENT_UI_SCRIPTS`), so the only client it names is /dev/ttys999;
//! - the menu's state machine with the real [`Live`] effects: close, discard
//!   and the stale-confirm refusal.
//!
//! Every server is killed (and its socket removed) by a Drop guard.

use agent_ui::menu::keys::Key;
use agent_ui::menu::render::render;
use agent_ui::menu::rows::{RowKey, Tab};
use agent_ui::menu::state::Menu;
use agent_ui::menu::{Live, Source};
use agent_ui::{Actions, Socket, Tmux};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use std::path::PathBuf;
use std::process::{Command, Output};
use std::time::{Duration, Instant};

const BIN: &str = env!("CARGO_BIN_EXE_agent-ui");
const LIFE: &str = "sleep 600";
const FAKE_TTY: &str = "/dev/ttys999";

struct Server {
    name: String,
    dir: PathBuf,
}

impl Server {
    fn start(tag: &str) -> Server {
        let name = format!("agentui-menu-{}-{tag}", std::process::id());
        let dir = std::env::temp_dir().join(&name);
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("scripts")).unwrap();
        let s = Server { name, dir };
        s.tmux(&["-f", "/dev/null", "new-session", "-d", "-s", "boot", "-x", "120", "-y", "40", LIFE]);
        s.tmux(&["set-option", "-g", "remain-on-exit", "on"]);
        // Recording fakes of every script an action runs.
        let body = format!("#!/bin/bash\nprintf '%s %s\\n' \"$(basename \"$0\")\" \"$*\" >> '{}'\n", s.log().display());
        for n in ["agent-jump.sh", "stash.sh", "closed-tabs.sh", "agent-tab-watcher.sh"] {
            let p = s.dir.join("scripts").join(n);
            std::fs::write(&p, &body).unwrap();
            Command::new("chmod").arg("+x").arg(&p).status().unwrap();
        }
        s
    }

    fn log(&self) -> PathBuf {
        self.dir.join("calls.log")
    }

    fn calls(&self) -> String {
        std::fs::read_to_string(self.log()).unwrap_or_default()
    }

    fn cmd(&self) -> Command {
        let mut c = Command::new("tmux");
        c.args(["-u", "-L", &self.name]).env_remove("TMUX").env_remove("TMUX_PANE");
        c
    }

    fn try_tmux(&self, args: &[&str]) -> Output {
        self.cmd().args(args).output().expect("run tmux")
    }

    fn tmux(&self, args: &[&str]) -> String {
        let o = self.try_tmux(args);
        assert!(o.status.success(), "tmux {args:?}: {}", String::from_utf8_lossy(&o.stderr));
        String::from_utf8_lossy(&o.stdout).trim_end().to_string()
    }

    /// A window in `session` (created on first use) → its id.
    fn window(&self, session: &str, name: &str, cmd: &str) -> String {
        if self.try_tmux(&["has-session", "-t", &format!("={session}")]).status.success() {
            return self.tmux(&["new-window", "-d", "-P", "-F", "#{window_id}", "-t", &format!("={session}:"), "-n", name, cmd]);
        }
        self.tmux(&["new-session", "-d", "-P", "-F", "#{window_id}", "-s", session, "-n", name, "-x", "120", "-y", "40", cmd])
    }

    fn agent(&self, session: &str, state: &str, summary: &str, extra: &[(&str, &str)]) -> String {
        let id = self.window(session, "claude", LIFE);
        for (k, v) in [("@agent_state", state), ("@agent_summary", summary)].iter().chain(extra) {
            self.tmux(&["set-option", "-w", "-t", &id, k, v]);
        }
        id
    }

    /// main: a question and a working agent; work: a failure; one parked tab.
    fn fixture(&self) -> [String; 4] {
        let ask = self.agent("main", "needs-input", "Kua Yu focus timing", &[("@agent_since", "100 needs-input"),
            ("@agent_detail_kind", "ask"), ("@agent_detail", "Which deck?"), ("@agent_kind", "claude")]);
        let run = self.agent("main", "running", "Tmux Agent Sidebar", &[("@agent_workflow", "1")]);
        let fail = self.agent("work", "failed", "Island resize", &[("@agent_since", "50 failed")]);
        let parked = self.agent("stash", "idle", "x", &[("@stash_label", "Pitch deck v2"), ("@stash_origin", "bai")]);
        [ask, run, fail, parked]
    }

    fn tmux_handle(&self) -> Tmux {
        Tmux::with_socket(Socket::parse(&self.name))
    }

    /// Run the interactive menu in a new window of this server → its pane.
    fn menu(&self, extra: &str) -> String {
        let trace = self.dir.join("trace");
        let cmd = format!(
            "AGENT_UI_SCRIPTS='{}' AGENT_UI_MENU_TRACE='{}' '{}' menu --client {FAKE_TTY} {extra}",
            self.dir.join("scripts").display(), trace.display(), BIN
        );
        self.tmux(&["new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "=boot:", "-n", "menu", &cmd])
    }

    fn screen(&self, pane: &str) -> String {
        self.tmux(&["capture-pane", "-p", "-t", pane])
    }

    fn wait_screen(&self, pane: &str, needle: &str) -> String {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            let s = self.screen(pane);
            if s.contains(needle) || Instant::now() > deadline {
                assert!(s.contains(needle), "{needle:?} never showed:\n{s}");
                return s;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    fn dead(&self, pane: &str) -> bool {
        self.tmux(&["display-message", "-p", "-t", pane, "#{pane_dead}"]) == "1"
    }

    fn wait_dead(&self, pane: &str) {
        let deadline = Instant::now() + Duration::from_secs(10);
        while !self.dead(pane) {
            assert!(Instant::now() < deadline, "menu did not exit:\n{}", self.screen(pane));
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    fn keys(&self, pane: &str, keys: &[&str]) {
        let mut a = vec!["send-keys", "-t", pane];
        a.extend_from_slice(keys);
        self.tmux(&a);
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.cmd().arg("kill-server").output();
        let deadline = Instant::now() + Duration::from_secs(3);
        while self.cmd().arg("list-sessions").output().is_ok_and(|o| o.status.success()) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
        }
        let _ = std::fs::remove_file(Socket::Name(self.name.clone()).path());
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn once(s: &Server, args: &[&str]) -> String {
    let mut c = Command::new(BIN);
    c.args(["--socket", &s.name, "menu", "--plain"]).args(args).env_remove("TMUX");
    c.env("AGENT_UI_SCRIPTS", s.dir.join("scripts"));
    let o = c.output().unwrap();
    assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
    String::from_utf8(o.stdout).unwrap()
}

#[test]
fn once_renders_the_server_and_runs_nothing() {
    let s = Server::start("once");
    s.fixture();
    let f = once(&s, &["--once", "110x34", "--client", FAKE_TTY]);
    let l: Vec<&str> = f.lines().collect();
    assert_eq!(l.len(), 34, "{f}");
    assert!(l[0].starts_with("╭─ agents"));
    assert!(l[1].contains(" All 3 ") && l[1].contains(" Needs you 2 ") && l[1].contains(" Parked 1 "), "{}", l[1]);
    assert!(l[3].contains("NEEDS YOU"));
    assert!(l[4].contains("1 ✕ Island resize") && l[4].contains("work"), "{f}");
    assert!(f.contains("2 ◉ Kua Yu focus timing"));
    assert!(f.contains("PARKED 1") && f.contains("Pitch deck v2"));
    assert!(f.contains("⏎ go to it"));
    // --select by label, another tab, the narrow layout.
    let f = once(&s, &["--once", "110x34", "--select", "2"]);
    assert!(f.lines().nth(3).unwrap().contains("Kua Yu focus timing") && f.contains("asks Which deck?"), "{f}");
    let f = once(&s, &["--once", "80x24", "--tab", "needs"]);
    assert!(!f.contains("Tmux Agent Sidebar") && f.contains("Kua Yu") && !f.contains("┬"), "{f}");
    // A zero-height frame renders nothing, without a panic.
    assert_eq!(once(&s, &["--once", "10x0"]), "");
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(s.calls(), "", "--once ran something");
}

#[test]
fn interactive_menu_in_a_pane() {
    let s = Server::start("live");
    let [ask, _, _, _] = s.fixture();

    // Draw, search, clear, go: ⏎ on the first need runs agent-jump.sh goto and exits.
    let p = s.menu("");
    let f = s.wait_screen(&p, "╰");
    assert!(f.contains("NEEDS YOU") && f.contains("Island resize"), "{f}");
    let trace = std::fs::read_to_string(s.dir.join("trace")).unwrap();
    let us: u64 = trace.split_whitespace().nth(1).unwrap().parse().unwrap();
    eprintln!("first frame in a tmux pane: {us} µs");
    s.keys(&p, &["/"]);
    s.keys(&p, &["-l", "kua"]);
    let f = s.wait_screen(&p, "/ kua▏");
    assert!(!f.contains("Island resize"), "{f}");
    s.keys(&p, &["Escape"]);
    s.wait_screen(&p, "Island resize");
    s.keys(&p, &["Enter"]);
    s.wait_dead(&p);
    // The search moved the selection to Kua Yu, and clearing it kept it there.
    assert_eq!(s.calls(), format!("agent-jump.sh goto {FAKE_TTY} {ask} main\n"));

    // Option-W closes, in the search box too, and runs nothing.
    let p = s.menu("--tab needs");
    s.wait_screen(&p, "╰");
    s.keys(&p, &["/", "M-w"]);
    s.wait_dead(&p);

    // s asks first; n cancels; s y parks; q closes.
    let p = s.menu("");
    s.wait_screen(&p, "╰");
    s.keys(&p, &["j", "s"]);
    s.wait_screen(&p, "park Kua Yu focus timing? y/n");
    s.keys(&p, &["n"]);
    s.wait_screen(&p, "⌥W  close");
    s.keys(&p, &["s"]);
    s.wait_screen(&p, "y/n");
    s.keys(&p, &["y"]);
    s.wait_screen(&p, "parking Kua Yu focus timing…");
    let deadline = Instant::now() + Duration::from_secs(10);
    while !s.calls().contains("stash.sh") && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(s.calls().ends_with(&format!("stash.sh stash {ask}\n")), "{}", s.calls());
    assert!(!s.dead(&p));
    s.keys(&p, &["q"]);
    s.wait_dead(&p);

    // A digit jumps at once; Alt-s runs next.
    let p = s.menu("");
    s.wait_screen(&p, "╰");
    s.keys(&p, &["2"]);
    s.wait_dead(&p);
    let p = s.menu("");
    s.wait_screen(&p, "╰");
    s.keys(&p, &["M-s"]);
    s.wait_dead(&p);
    let calls = s.calls();
    let tail: Vec<&str> = calls.lines().rev().take(2).collect();
    assert_eq!(tail, [format!("agent-jump.sh next {FAKE_TTY}"), format!("agent-jump.sh goto {FAKE_TTY} {ask} main")]);
}

#[test]
fn live_effects_through_the_state_machine() {
    let s = Server::start("fx");
    let [ask, _, fail, parked] = s.fixture();
    let tmux = s.tmux_handle();
    let mut src = Source::new(tmux.clone(), Some(FAKE_TTY.into()));
    let mut menu = Menu::new(src.load(), Tab::All);
    let actions = Actions { tmux: tmux.clone(), scripts: s.dir.join("scripts") };
    let mut live = Live { actions, client: FAKE_TTY.into(), snap: src.core.snapshot.clone() };
    let mut buf = Buffer::empty(Rect::new(0, 0, 110, 34));
    render(&mut menu, &mut buf, 0.0);

    // x y on a one-pane agent: closed-tabs.sh closes its pane.
    assert_eq!(menu.sel, Some(RowKey::Need(fail.clone())));
    let pane = s.tmux(&["display-message", "-p", "-t", &fail, "#{pane_id}"]);
    menu.handle(&[Key::Char('x')], &mut live);
    menu.frame_shown();
    menu.handle(&[Key::Char('y')], &mut live);
    assert_eq!(menu.msg, "closed Island resize · ⌘Z brings it back");
    // Stale confirm: x pressed, the window parked before the y.
    menu.sel = Some(RowKey::Need(ask.clone()));
    menu.handle(&[Key::Char('x')], &mut live);
    s.tmux(&["move-window", "-s", &ask, "-t", "=stash:"]);
    menu.set_data(src.load());
    live.snap = src.core.snapshot.clone();
    menu.frame_shown();
    menu.handle(&[Key::Char('y')], &mut live);
    assert_eq!(menu.msg, "that tab moved · press x again");
    // A parked row: x is a discard through stash.sh.
    menu.set_tab(Tab::Parked);
    menu.sel = Some(RowKey::Parked(parked.clone()));
    menu.handle(&[Key::Char('x')], &mut live);
    assert_eq!(menu.confirm.as_ref().map(|c| c.verb()), Some("discard"));
    menu.frame_shown();
    menu.handle(&[Key::Char('y')], &mut live);
    let deadline = Instant::now() + Duration::from_secs(10);
    while !s.calls().contains("kill-many") && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(20));
    }
    assert_eq!(s.calls(), format!("closed-tabs.sh close {pane}\nstash.sh kill-many {parked}\n"));
}
