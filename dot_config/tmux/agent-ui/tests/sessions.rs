//! `agent-ui sessions` against a REAL private tmux server
//! (`tmux -f /dev/null -L agentui-sess-<pid>-<tag>`), never the user's.
//!
//! A real client is attached to the server from inside one of its own panes
//! (`env -u TMUX tmux -L <name> attach -t home`, in session `boot`, which no
//! client shows), so `switch-client -c <tty>` has something to move. The
//! picker runs in another pane of `boot`, given that client's tty, and is
//! driven with `send-keys`, often before it has even started (the keys then
//! sit in the pane's cooked tty until it goes raw).
//!
//! Every server is killed (and its socket removed) by a Drop guard.

use agent_ui::Socket;
use std::path::PathBuf;
use std::process::{Command, Output};
use std::time::{Duration, Instant};

const BIN: &str = env!("CARGO_BIN_EXE_agent-ui");
const LIFE: &str = "sleep 600";

struct Server {
    name: String,
    dir: PathBuf,
}

impl Server {
    fn start(tag: &str) -> Server {
        let name = format!("agentui-sess-{}-{tag}", std::process::id());
        let dir = std::env::temp_dir().join(&name);
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let s = Server { name, dir };
        s.tmux(&["-f", "/dev/null", "new-session", "-d", "-s", "boot", "-x", "120", "-y", "40", LIFE]);
        s.tmux(&["set-option", "-g", "remain-on-exit", "on"]);
        // No zsh (and no .zshenv) between new-window and the binary.
        s.tmux(&["set-option", "-g", "default-shell", "/bin/sh"]);
        s
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

    /// A session with one window per agent state given ("" = a plain shell).
    fn session(&self, name: &str, states: &[&str]) {
        for (i, st) in states.iter().enumerate() {
            let id = if i == 0 {
                self.tmux(&["new-session", "-d", "-P", "-F", "#{window_id}", "-s", name, "-x", "120", "-y", "40", LIFE])
            } else {
                self.tmux(&["new-window", "-d", "-P", "-F", "#{window_id}", "-t", &format!("={name}:"), LIFE])
            };
            if !st.is_empty() {
                self.tmux(&["set-option", "-w", "-t", &id, "@agent_state", st]);
            }
        }
    }

    /// home (the client's), work, workshop, alpha, scratch, stash.
    fn fixture(&self) {
        self.session("home", &[""]);
        self.session("work", &["failed", "running"]);
        self.session("workshop", &["idle", "idle"]);
        self.session("alpha", &["needs-input"]);
        self.session("scratch", &[""]);
        self.session("stash", &["idle"]);
    }

    /// Attach a real client to `home` from a pane of `boot` → its tty.
    fn client(&self) -> String {
        let cmd = format!("env -u TMUX -u TMUX_PANE TERM=xterm-256color tmux -u -L '{}' attach -t home", self.name);
        self.tmux(&["new-window", "-d", "-t", "=boot:", "-n", "client", &cmd]);
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            let c = self.tmux(&["list-clients", "-F", "#{client_tty}"]);
            if let Some(t) = c.lines().find(|l| !l.is_empty()) {
                return t.to_string();
            }
            assert!(Instant::now() < deadline, "the client never attached");
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    fn at(&self, tty: &str) -> String {
        self.tmux(&["display-message", "-p", "-c", tty, "#{session_name}"])
    }

    fn wait_at(&self, tty: &str, want: &str) {
        let deadline = Instant::now() + Duration::from_secs(10);
        while self.at(tty) != want {
            assert!(Instant::now() < deadline, "client at {:?}, never {want:?}", self.at(tty));
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    /// Start the picker in a new pane of boot → its pane id. Keys typed by
    /// `ahead` (one send-keys each; `{P}` is the new pane) go in right after
    /// new-window, before the binary can be up.
    fn picker(&self, tty: &str, ahead: &[&[&str]]) -> String {
        let cmd = format!(
            "AGENT_UI_SESSIONS_TRACE='{}' '{}' --socket '{}' sessions --client '{tty}'",
            self.trace().display(), BIN, self.name
        );
        let p = self.tmux(&["new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "=boot:", "-n", "pick", &cmd]);
        for k in ahead {
            let k: Vec<String> = k.iter().map(|a| a.replace("{P}", &p)).collect();
            let k: Vec<&str> = k.iter().map(String::as_str).collect();
            self.keys(&p, &k);
        }
        p
    }

    fn trace(&self) -> PathBuf {
        self.dir.join("trace")
    }

    fn keys(&self, pane: &str, keys: &[&str]) {
        let mut a = vec!["send-keys", "-t", pane];
        a.extend_from_slice(keys);
        self.tmux(&a);
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
            assert!(Instant::now() < deadline, "picker did not exit:\n{}", self.screen(pane));
            std::thread::sleep(Duration::from_millis(20));
        }
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
    let o = Command::new(BIN).args(["--socket", &s.name, "sessions"]).args(args).env_remove("TMUX").output().unwrap();
    assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
    String::from_utf8(o.stdout).unwrap()
}

#[test]
fn once_renders_the_server() {
    let s = Server::start("once");
    s.fixture();
    let tty = s.client();
    let f = once(&s, &["--once", "100x30", "--client", &tty]);
    let l: Vec<&str> = f.lines().collect();
    assert_eq!(l.len(), 30, "{f}");
    assert!(l[0].starts_with("╭─ sessions"));
    // home is the client's; the rest were never attached: by name.
    let names: Vec<&str> = l[3..10].iter().filter_map(|r| r.split_whitespace().nth(1)).collect();
    assert_eq!(names, ["alpha", "boot", "work", "workshop", "│", "│", "│"], "{f}");
    assert!(l[3].contains("alpha      ● 1 needs you"), "{f}");
    assert!(!l[4].contains('●'), "{f}");
    assert!(l[5].contains("work       ● 1 needs you · 1 working"), "{f}");
    assert!(l[6].contains("workshop   ● 2 idle"), "{f}");
    assert!(!f.contains("scratch") && !f.contains("stash") && !f.contains("│  home"), "{f}");
    // --query: the name only.
    let f = once(&s, &["--once", "80x20", "--client", &tty, "--query", "shop"]);
    assert!(f.contains("workshop") && !f.contains("alpha") && f.contains("├─ workshop ─"), "{f}");
    let f = once(&s, &["--once", "80x20", "--client", &tty, "--query", "working"]);
    assert!(f.contains("no match · ⏎ creates working"), "{f}");
    // --ansi: truecolour.
    assert!(once(&s, &["--once", "60x10", "--ansi"]).contains("\x1b[0;1;38;2;205;214;244;48;2;30;30;46msessions"));
}

#[test]
fn keys_typed_before_the_picker_is_up() {
    let s = Server::start("ahead");
    s.fixture();
    let tty = s.client();
    assert_eq!(s.at(&tty), "home");

    // "shop" Enter, all typed the instant the pane exists.
    let p = s.picker(&tty, &[&["-l", "shop"], &["Enter"]]);
    s.wait_at(&tty, "workshop");
    s.wait_dead(&p);
    let trace = std::fs::read_to_string(s.trace()).unwrap_or_default();
    eprintln!("typed ahead:\n{trace}");
    assert!(trace.contains("done_us") && trace.contains("switch workshop"), "{trace}");

    // A burst in ONE tmux command: "alp", Enter.
    let p = s.picker(&tty, &[&["-l", "alp", ";", "send-keys", "-t", "{P}", "Enter"]]);
    s.wait_at(&tty, "alpha");
    s.wait_dead(&p);

    // A new name, Enter and y, all typed ahead: created, then switched to.
    let p = s.picker(&tty, &[&["-l", "fresh1"], &["Enter"], &["-l", "y"]]);
    s.wait_at(&tty, "fresh1");
    s.wait_dead(&p);
    let home = std::env::var("HOME").unwrap();
    assert_eq!(s.tmux(&["display-message", "-p", "-t", "=fresh1:", "#{session_path}"]), home);
}

#[test]
fn interactive_picker_in_a_pane() {
    let s = Server::start("live");
    s.fixture();
    let tty = s.client();

    // Draw; filter; move; go.
    let p = s.picker(&tty, &[]);
    let f = s.wait_screen(&p, "╰");
    assert!(f.contains("╭─ sessions") && f.contains("alpha") && f.contains("● 1 needs you · 1 working"), "{f}");
    assert!(!f.contains("scratch") && !f.contains("stash"), "{f}");
    let trace = std::fs::read_to_string(s.trace()).unwrap();
    let us: u64 = trace.lines().find_map(|l| l.strip_prefix("first_frame_us ")).unwrap().parse().unwrap();
    eprintln!("first frame in a tmux pane: {us} µs");
    s.keys(&p, &["-l", "wo"]);
    let f = s.wait_screen(&p, "❯ wo▏");
    assert!(!f.contains("alpha"), "{f}");
    s.keys(&p, &["Down"]);
    s.wait_screen(&p, "├─ workshop ─");
    s.keys(&p, &["C-k"]); // up again: work
    s.wait_screen(&p, "├─ work ─");
    s.keys(&p, &["Enter"]);
    s.wait_at(&tty, "work");
    s.wait_dead(&p);

    // Esc closes and goes nowhere.
    let p = s.picker(&tty, &[]);
    s.wait_screen(&p, "╰");
    s.keys(&p, &["-l", "al"]);
    s.wait_screen(&p, "❯ al▏");
    s.keys(&p, &["Escape"]);
    s.wait_dead(&p);
    assert_eq!(s.at(&tty), "work");

    // Create through the inline confirm: n cancels, then ⏎ ⏎ creates.
    let p = s.picker(&tty, &[]);
    s.wait_screen(&p, "╰");
    s.keys(&p, &["-l", "brandnew"]);
    s.wait_screen(&p, "no match · ⏎ creates brandnew");
    s.keys(&p, &["Enter"]);
    s.wait_screen(&p, "Create and go to [brandnew]? Y/n");
    s.keys(&p, &["n"]);
    s.wait_screen(&p, "type to filter");
    assert!(!s.try_tmux(&["has-session", "-t", "=brandnew"]).status.success());
    s.keys(&p, &["Enter"]);
    s.wait_screen(&p, "Y/n");
    s.keys(&p, &["Enter"]);
    s.wait_at(&tty, "brandnew");
    s.wait_dead(&p);

    // An invalid name says so inline and stays open; C-u clears; C-c closes.
    let p = s.picker(&tty, &[]);
    s.wait_screen(&p, "╰");
    s.keys(&p, &["-l", "a.b"]);
    s.keys(&p, &["Enter"]);
    s.wait_screen(&p, "Invalid session name (allowed: A-Z a-z 0-9 _ -): a.b");
    assert!(!s.dead(&p));
    s.keys(&p, &["C-u"]);
    s.wait_screen(&p, "❯ ▏type to filter");
    // scratch is never a target.
    s.keys(&p, &["-l", "scratch"]);
    s.keys(&p, &["Enter"]);
    s.wait_dead(&p);
    assert_eq!(s.at(&tty), "brandnew");
}
