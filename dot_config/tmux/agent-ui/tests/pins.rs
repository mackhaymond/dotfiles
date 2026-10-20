//! `agent-ui pin` end to end against a REAL private tmux server (`tmux -L
//! agentui-pin-<pid>`, `-f /dev/null`, killed by a Drop guard): the window
//! option, the state file (a temp one: `$AGENT_UI_PINS`), the claude
//! session file that names the window, and the sidebar's PINNED section.

use agent_ui::Socket;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

const BIN: &str = env!("CARGO_BIN_EXE_agent-ui");
const LIFE: &str = "sleep 600";

struct Server {
    name: String,
    dir: PathBuf,
}

impl Server {
    fn start() -> Server {
        let name = format!("agentui-pin-{}", std::process::id());
        let dir = std::env::temp_dir().join(&name);
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("sessions")).unwrap();
        let s = Server { name, dir };
        s.tmux(&["new-session", "-d", "-s", "main", "-x", "120", "-y", "40", LIFE]);
        s.tmux(&["new-window", "-d", "-t", "main:1", LIFE]);
        s.tmux(&["new-window", "-d", "-t", "main:2", LIFE]);
        for (k, v) in [("state", "idle"), ("summary", "Pin me"), ("kind", "claude")] {
            s.tmux(&["set-option", "-w", "-t", "main:1", &format!("@agent_{k}"), v]);
        }
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

    fn store(&self) -> PathBuf {
        self.dir.join("pins.json")
    }

    fn agent_ui(&self, args: &[&str]) -> Output {
        Command::new(BIN)
            .args(["--socket", &self.name])
            .args(args)
            .env("AGENT_UI_PINS", self.store())
            .env("AGENT_UI_CLAUDE_SESSIONS", self.dir.join("sessions"))
            .env("AGENT_UI_SCRIPTS", "/nonexistent")
            .env("AGENT_UI_CLAUDE_DIR", "/nonexistent")
            .env("AGENT_PAUSE_STATE_DIR", "/nonexistent/agent-pause")
            // (Never wake the real sidebars.)
            .env("AGENT_STATE_NOTICE", format!("com.mackhaymond.test.{}", self.name))
            .env("AGENT_FOCUS_NOTICE", format!("com.mackhaymond.test.{}.focus", self.name))
            .env_remove("TMUX")
            .env_remove("WEZTERM_PANE")
            .output()
            .expect("run agent-ui")
    }

    fn pinned(&self, target: &str) -> String {
        self.opt(target, "@agent_pinned")
    }

    fn opt(&self, target: &str, name: &str) -> String {
        let o = self.cmd().args(["show-options", "-wqv", "-t", target, name]).output().unwrap();
        String::from_utf8_lossy(&o.stdout).trim().to_string()
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.cmd().arg("kill-server").output();
        let _ = std::fs::remove_file(Socket::Name(self.name.clone()).path());
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn store_text(p: &Path) -> String {
    std::fs::read_to_string(p).unwrap_or_default()
}

/// Pin, see it in the sidebar's PINNED section and in the state file (by
/// the session id the claude session file gives that window), unpin; a
/// shell is refused; `on` / `off` are idempotent.
#[test]
fn pin_toggles_the_window_and_the_state_file() {
    let s = Server::start();
    let win = s.tmux(&["display-message", "-p", "-t", "main:1", "#{window_id}"]);
    let pane = s.tmux(&["display-message", "-p", "-t", "main:1", "#{pane_id}"]);
    let start = s.tmux(&["display-message", "-p", "#{start_time}"]);
    // This test process stands in for the claude in that pane (alive).
    let me = std::process::id();
    std::fs::write(s.dir.join(format!("sessions/{me}.json")),
        format!("{{\"pid\":{me},\"sessionId\":\"sid-1\",\"tmux\":\"main:{win}.{pane}\",\"startedAt\":5}}")).unwrap();

    let o = s.agent_ui(&["pin", "toggle", "--window", &win]);
    assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
    assert!(String::from_utf8_lossy(&o.stderr).contains("pinned Pin me · ⌥W 1"));
    assert_eq!(s.opt(&win, "@pin_key"), "1");
    let at = s.pinned(&win);
    assert!(!at.is_empty() && at.bytes().all(|b| b.is_ascii_digit()), "{at:?}");
    let st = store_text(&s.store());
    assert!(st.contains("\"sid\": \"sid-1\"") && st.contains(&format!("\"server\": \"{start}\"")) && st.contains(&format!("\"win\": \"{win}\"")),
        "{st}");

    // The sidebar lists it under PINNED, out of main's group.
    let o = s.agent_ui(&["sidebar", "--once", "34x40", "--client", "/dev/ttys999"]);
    let text = String::from_utf8_lossy(&o.stdout);
    assert!(text.contains("PINNED") && text.contains("Pin me"), "{text}");

    // `on` again: nothing changes.
    let o = s.agent_ui(&["pin", "on", "--window", &win]);
    assert!(o.status.success());
    assert_eq!(s.pinned(&win), at);

    // A second pin is ⌥2; unpinning the first renumbers it to ⌥1 at once.
    s.tmux(&["new-window", "-d", "-t", "main:3", LIFE]);
    s.tmux(&["set-option", "-w", "-t", "main:3", "@agent_state", "running"]);
    let two = s.tmux(&["display-message", "-p", "-t", "main:3", "#{window_id}"]);
    // (In the same second as the first, very likely: still after it.)
    assert!(s.agent_ui(&["pin", "on", "--window", &two]).status.success());
    assert_eq!((s.opt(&win, "@pin_key"), s.opt(&two, "@pin_key")), ("1".to_string(), "2".to_string()));
    let (a1, a2): (i64, i64) = (s.pinned(&win).parse().unwrap(), s.pinned(&two).parse().unwrap());
    assert!(a2 > a1, "{a1} {a2}");
    // A gap left by hand (no sidebar here): `pin renumber` closes it.
    s.tmux(&["set-option", "-w", "-t", &two, "@pin_key", "5"]);
    assert!(s.agent_ui(&["pin", "renumber"]).status.success());
    assert_eq!(s.opt(&two, "@pin_key"), "2");
    assert!(s.agent_ui(&["pin", "off", "--window", &win]).status.success());
    assert_eq!((s.opt(&win, "@pin_key"), s.opt(&two, "@pin_key")), (String::new(), "1".to_string()));
    assert!(s.agent_ui(&["pin", "off", "--window", &two]).status.success());
    assert_eq!(s.opt(&two, "@pin_key"), "");
    assert!(s.agent_ui(&["pin", "on", "--window", &win]).status.success());

    // Toggle again: unpinned, the entry gone.
    let o = s.agent_ui(&["pin", "--window", &win]);
    assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
    assert_eq!(s.pinned(&win), "");
    assert_eq!(s.opt(&win, "@pin_key"), "");
    assert!(!store_text(&s.store()).contains("sid-1"));
    let o = s.agent_ui(&["pin", "off", "--window", &win]);
    assert!(o.status.success());

    // A shell (no agent state) is refused, and nothing is set.
    let shell = s.tmux(&["display-message", "-p", "-t", "main:2", "#{window_id}"]);
    let o = s.agent_ui(&["pin", "--window", &shell]);
    assert_eq!(o.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&o.stderr).contains("only agents pin"));
    assert_eq!(s.pinned(&shell), "");
    // A parked tab, and an agent in a hidden session, are refused aloud.
    for (sess, why) in [("stash", "a parked tab is never pinned"), ("agents", "agents is not a space")] {
        s.tmux(&["new-session", "-d", "-s", sess, LIFE]);
        s.tmux(&["set-option", "-w", "-t", &format!("{sess}:0"), "@agent_state", "idle"]);
        let w = s.tmux(&["display-message", "-p", "-t", &format!("{sess}:0"), "#{window_id}"]);
        let o = s.agent_ui(&["pin", "--window", &w]);
        assert_eq!(o.status.code(), Some(1), "{sess}");
        assert!(String::from_utf8_lossy(&o.stderr).contains(why), "{}", String::from_utf8_lossy(&o.stderr));
        assert_eq!(s.pinned(&w), "");
    }
    // Bad usage.
    assert_eq!(s.agent_ui(&["pin", "sideways"]).status.code(), Some(2));
    assert_eq!(s.agent_ui(&["pin", "renumber", "x"]).status.code(), Some(2));
}
