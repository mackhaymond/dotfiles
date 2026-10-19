//! The Option-W menu against a REAL private tmux server
//! (`tmux -L agentui-menu-<pid>-<tag>`, `-f /dev/null`), never the user's:
//!
//! - `menu --once` renders the server's agents, runs nothing and writes no
//!   `@agent_key`;
//! - the interactive menu, run INSIDE a pane of that server and driven with
//!   `send-keys`: it draws, stores each agent's letter in `@agent_key` (one
//!   batched write), goes on a letter, searches, goes (⏎), goes to the last
//!   agent on Option-W (or closes with none), and parks after Space s y;
//!   every script it runs is a recording fake (`AGENT_UI_SCRIPTS`), so the
//!   only client it names is /dev/ttys999;
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
        // boot (and the menu's own panes in it) is scaffolding, no space: its
        // shells take no letter (shells share the agents' pool).
        s.tmux(&["set-option", "-t", "boot", "@space_hidden", "1"]);
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
    /// The watcher never runs on a private server, so the fixture stamps
    /// @agent_slot itself: 1, 2 and 4 (3 was freed), and a stale 3 on the
    /// parked tab (parked agents hold no number).
    fn fixture(&self) -> [String; 4] {
        let ask = self.agent("main", "needs-input", "Kua Yu focus timing", &[("@agent_since", "100 needs-input"),
            ("@agent_detail_kind", "ask"), ("@agent_detail", "Which deck?"), ("@agent_kind", "claude"),
            ("@agent_slot", "1")]);
        let run = self.agent("main", "running", "Tmux Agent Sidebar", &[("@agent_workflow", "1"), ("@agent_slot", "2")]);
        let fail = self.agent("work", "failed", "Island resize", &[("@agent_since", "50 failed"), ("@agent_slot", "4")]);
        let parked = self.agent("stash", "idle", "x", &[("@stash_label", "Pitch deck v2"), ("@stash_origin", "bai"),
            ("@agent_slot", "3")]);
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

impl Server {
    /// A window's @agent_key ("" when unset).
    fn key(&self, win: &str) -> String {
        self.tmux(&["show-options", "-wqv", "-t", win, "@agent_key"])
    }

    /// The windows' @agent_key, once all are set (or 10 s passed).
    fn wait_keys(&self, wins: &[&str]) -> Vec<String> {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            let ks: Vec<String> = wins.iter().map(|w| self.key(w)).collect();
            if ks.iter().all(|k| !k.is_empty()) || Instant::now() > deadline {
                return ks;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    fn wait_calls(&self, needle: &str) -> String {
        let deadline = Instant::now() + Duration::from_secs(10);
        while !self.calls().contains(needle) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
        }
        self.calls()
    }
}

#[test]
fn once_renders_the_server_and_runs_nothing() {
    let s = Server::start("once");
    let [ask, run, fail, parked] = s.fixture();
    // The default, All: spaces by name (main, work), letters in that order,
    // the parked tab folded, no NEEDS YOU section.
    let f = once(&s, &["--once", "110x34", "--client", FAKE_TTY]);
    let l: Vec<&str> = f.lines().collect();
    assert_eq!(l.len(), 34, "{f}");
    assert!(l[0].starts_with("╭─ agents") && l[0].contains(" · ◉ 2 need you · ◐ 1 working · ▪ 1 parked ─"), "{}", l[0]);
    assert!(!f.contains("NEEDS YOU") && !f.contains("PARKED"), "{f}");
    assert!(l[1].starts_with("│ main "), "{f}");
    assert!(l[2].starts_with("│  a  ◉ Kua Yu focus timing  asks Which deck?"), "{f}");
    assert!(l[3].starts_with("│  s  ◐ Tmux Agent Sidebar"), "{f}");
    assert!(l[4].starts_with("│ work "), "{f}");
    assert!(l[5].starts_with("│  d  ✕ Island resize  failed"), "{f}");
    assert!(l[6].starts_with("│     ▪ 1 parked ⇥"), "{f}");
    assert!(l[32].starts_with("│  a–d  go  ⌥W  close "), "{}", l[32]);
    // The same with --tab all; --select by letter with the preview strip;
    // other filters.
    assert_eq!(once(&s, &["--once", "110x34", "--client", FAKE_TTY, "--tab", "all"]), f);
    let f = once(&s, &["--once", "110x34", "--select", "a", "--preview"]);
    assert!(f.contains("├─ Kua Yu focus timing · main:0"), "{f}");
    let f = once(&s, &["--once", "80x24", "--tab", "needs"]);
    assert!(f.lines().next().unwrap().contains("Needs you ⇥") && !f.contains("Tmux Agent Sidebar"), "{f}");
    let f = once(&s, &["--once", "80x24", "--tab", "parked"]);
    assert!(f.contains("▪ Pitch deck v2"), "{f}");
    // A zero-height frame renders nothing, without a panic.
    assert_eq!(once(&s, &["--once", "10x0"]), "");
    // A stored letter wins over display order.
    s.tmux(&["set-option", "-w", "-t", &fail, "@agent_key", "a"]);
    let f = once(&s, &["--once", "110x34"]);
    assert!(f.contains("│  a  ✕ Island resize") && f.contains("│  s  ◉ Kua Yu"), "{f}");
    s.tmux(&["set-option", "-wu", "-t", &fail, "@agent_key"]);
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(s.calls(), "", "--once ran something");
    for w in [&ask, &run, &fail, &parked] {
        assert_eq!(s.key(w), "", "--once wrote @agent_key on {w}");
    }
}

#[test]
fn interactive_menu_in_a_pane() {
    let s = Server::start("live");
    let [ask, run, fail, parked] = s.fixture();

    // It draws, and stores every listed agent's letter (none on the parked tab).
    let p = s.menu("");
    let f = s.wait_screen(&p, "╰");
    assert!(f.contains("a  ◉ Kua Yu") && !f.contains("NEEDS YOU") && f.contains("▪ 1 parked ⇥"), "{f}");
    let trace = std::fs::read_to_string(s.dir.join("trace")).unwrap();
    let us: u64 = trace.split_whitespace().nth(1).unwrap().parse().unwrap();
    eprintln!("first frame in a tmux pane: {us} µs");
    assert_eq!(s.wait_keys(&[&ask, &run, &fail]), ["a", "s", "d"]);
    assert_eq!(s.key(&parked), "");
    // A letter goes at once.
    s.keys(&p, &["s"]);
    s.wait_dead(&p);
    assert_eq!(s.calls(), format!("agent-jump.sh goto {FAKE_TTY} {run} main\n"));

    // Search: letters type; ⏎ goes to the selected match.
    let p = s.menu("");
    s.wait_screen(&p, "╰");
    s.keys(&p, &["/"]);
    s.keys(&p, &["-l", "kua"]);
    let f = s.wait_screen(&p, "/ kua▏");
    assert!(!f.contains("Island resize"), "{f}");
    s.keys(&p, &["Enter"]);
    s.wait_dead(&p);
    assert!(s.calls().ends_with(&format!("agent-jump.sh goto {FAKE_TTY} {ask} main\n")), "{}", s.calls());

    // Option-W with no last agent closes, from the search too, running nothing.
    let n = s.calls().lines().count();
    let p = s.menu("--tab needs");
    s.wait_screen(&p, "╰");
    s.keys(&p, &["/", "M-w"]);
    s.wait_dead(&p);
    assert_eq!(s.calls().lines().count(), n);
    // With the global MRU pair set: the last agent.
    s.tmux(&["set-option", "-g", "@agent_mru_cur", &run]);
    s.tmux(&["set-option", "-g", "@agent_mru_prev", &ask]);
    let p = s.menu("");
    s.wait_screen(&p, "⌥W  last");
    s.keys(&p, &["M-w"]);
    s.wait_dead(&p);
    assert!(s.calls().ends_with(&format!("agent-jump.sh goto {FAKE_TTY} {run} main\n")), "{}", s.calls());

    // Space s asks first; n cancels; Space s y parks; Esc closes.
    let p = s.menu("");
    s.wait_screen(&p, "╰");
    s.keys(&p, &["Space", "s"]);
    s.wait_screen(&p, "park Island resize? y/n");
    s.keys(&p, &["n"]);
    s.wait_screen(&p, "⌥W  last");
    s.keys(&p, &["Space"]);
    s.wait_screen(&p, "esc  cancel");
    s.keys(&p, &["s"]);
    s.wait_screen(&p, "y/n");
    s.keys(&p, &["y"]);
    s.wait_screen(&p, "parking Island resize…");
    let calls = s.wait_calls("stash.sh");
    assert!(calls.ends_with(&format!("stash.sh stash {fail}\n")), "{calls}");
    assert!(!s.dead(&p));
    s.keys(&p, &["Escape"]);
    s.wait_dead(&p);

    // Alt-s runs next.
    let p = s.menu("");
    s.wait_screen(&p, "╰");
    s.keys(&p, &["M-s"]);
    s.wait_dead(&p);
    assert!(s.calls().ends_with(&format!("agent-jump.sh next {FAKE_TTY}\n")), "{}", s.calls());
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

    // Space x y on a one-pane agent: closed-tabs.sh closes its pane.
    assert_eq!(menu.sel, Some(RowKey::Agent("work".into(), fail.clone())));
    let pane = s.tmux(&["display-message", "-p", "-t", &fail, "#{pane_id}"]);
    menu.handle(&[Key::Char(' '), Key::Char('x')], &mut live);
    menu.frame_shown();
    menu.handle(&[Key::Char('y')], &mut live);
    assert_eq!(menu.msg, "closed Island resize · ⌘Z brings it back");
    // Stale confirm: x pressed, the window parked before the y.
    menu.sel = Some(RowKey::Agent("main".into(), ask.clone()));
    render(&mut menu, &mut buf, 0.0);
    menu.handle(&[Key::Char(' '), Key::Char('x')], &mut live);
    assert!(menu.confirm.is_some());
    s.tmux(&["move-window", "-s", &ask, "-t", "=stash:"]);
    menu.set_data(src.load());
    live.snap = src.core.snapshot.clone();
    menu.frame_shown();
    menu.handle(&[Key::Char('y')], &mut live);
    assert_eq!(menu.msg, "that tab moved · press ␣ x again");
    // A parked row: x is a discard through stash.sh.
    menu.set_tab(Tab::Parked);
    menu.sel = Some(RowKey::Parked(parked.clone()));
    render(&mut menu, &mut buf, 0.0);
    menu.handle(&[Key::Char(' '), Key::Char('x')], &mut live);
    assert_eq!(menu.confirm.as_ref().map(|c| c.verb()), Some("discard"));
    menu.frame_shown();
    menu.handle(&[Key::Char('y')], &mut live);
    s.wait_calls("kill-many");
    assert_eq!(s.calls(), format!("closed-tabs.sh close {pane}\nstash.sh kill-many {parked}\n"));
}
