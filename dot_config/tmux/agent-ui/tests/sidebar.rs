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
            // (Never the real transcripts: no activity blurbs here.)
            .env("AGENT_UI_CLAUDE_DIR", "/nonexistent")
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
        let tmux = agent_ui::Tmux::with_socket(Some(Socket::Name(self.name.clone())));
        let _ = std::fs::remove_file(agent_ui::sidebar::status::Registry::lock_path(&tmux));
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

/// The status line's arrow (U+E0B0) the client's space chip ends in.
const ARROW: char = '\u{e0b0}';

/// `l` as words: the arrow a space, runs of spaces one, padded (` 1 main `).
fn uncapped(l: &str) -> String {
    format!(" {} ", l.replace(ARROW, " ").split(' ').filter(|w| !w.is_empty()).collect::<Vec<_>>().join(" "))
}

/// Where the SPACES section starts (its header, else the gap above its
/// chips) → (that line, the PARKED header's line).
fn spaces_at(lines: &[String]) -> (usize, usize) {
    let p = at(lines, " PARKED");
    let s = lines[..p].iter().rposition(|l| l.starts_with(" SPACES"));
    let s = s.unwrap_or_else(|| lines[..p - 1].iter().rposition(|l| l.trim().is_empty()).unwrap_or_else(|| panic!("no spaces: {lines:#?}")));
    (s, p)
}

/// The SPACES section's text, as words.
fn spaces_text(lines: &[String]) -> String {
    let (s, p) = spaces_at(lines);
    uncapped(&lines[s..p].join(" "))
}

#[test]
fn once_renders_the_fixture() {
    let s = Server::start("once", 80, 24);
    s.fixture();
    let out = s.sidebar(&["--once", "34x30", "--client", CLIENT]);
    let raw: Vec<&str> = out.lines().collect();
    let lines = plain(&out);
    assert_eq!(lines.len(), 30);
    for l in &lines {
        assert_eq!(l.chars().count(), 34, "{l:?}");
    }
    // No client here: the classic title, no card.
    assert!(lines[0].starts_with(" agents") && lines[0].contains("✕1 ◉1 ◐1"), "{lines:#?}");
    assert!(lines[1..].iter().find(|l| !l.trim().is_empty()).unwrap().starts_with(" NEEDS YOU"));
    // Needs in agent-jump.sh order: failed first; each card starts with the
    // agent's letter key-cap (display order: work a s d, zeta f).
    let fail = at(&lines, " ▌ f  Broken thing");
    let ask = at(&lines, " ▌ a  proj/Which deck");
    assert!(fail < ask);
    // Cards are tinted surfaces; the canvas is the terminal's own background
    // (no background code at all) on the title and section headers.
    assert!(raw[fail].contains(";48;2;"), "{:?}", raw[fail]);
    // Then the agents, grouped by space under a dot and the name (no rule).
    let agents = at(&lines, " AGENTS");
    assert!(lines[agents].ends_with("all spaces "), "{lines:#?}");
    for y in [0, agents] {
        assert!(!raw[y].contains(";48;"), "{:?}", raw[y]);
    }
    // Each a tile: a canvas margin, its state stripe (none when idle), its key-cap.
    for t in [" ▎ s  Long running", "   d  Quiet one", " ▎ a  Which deck", " ▎ f  Broken thing"] {
        assert!(lines[agents..].iter().any(|l| l.starts_with(t)), "{t}: {lines:#?}");
    }
    let s_row = lines.iter().find(|l| l.starts_with(" ▎ s  Long running")).unwrap();
    assert!(s_row.contains(" ⚙ ") && s_row.ends_with("⠋ working  "), "{s_row:?}");
    for t in [" ● work", " ● zeta"] {
        assert!(lines[agents..].iter().any(|l| l.starts_with(t) && !l.contains('─')), "{t}: {lines:#?}");
    }
    // The spaces (each a row, or chips), then PARKED with nothing parked.
    let pills = spaces_text(&lines);
    let pos: Vec<usize> = [" main ", " work ", " zeta "].iter().map(|n| pills.find(n).unwrap_or_else(|| panic!("{n}: {lines:#?}"))).collect();
    assert!(pos.windows(2).all(|p| p[0] < p[1]), "{lines:#?}");
    assert!(lines[29].starts_with(" PARKED none"), "{lines:#?}");
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
        let start = lines.iter().position(|l| l.starts_with(" AGENTS"));
        let end = lines.iter().position(|l| l.starts_with(" SHELLS"));
        let end = end.or_else(|| Some(spaces_at(lines).0)).expect("the spaces");
        lines[start.expect("the agents section")..end].to_vec()
    };
    // Classic until an option says otherwise (no usage here, so no card and
    // no space chip; work has no shells, so no SHELLS section; the agents
    // section says which scope).
    let classic = once();
    assert!(classic[0].starts_with(" NEEDS YOU") && !classic.iter().any(|l| l.contains("SHELLS")), "{classic:#?}");
    assert!(agents_part(&classic)[0].ends_with("all spaces ") && agents_part(&classic).iter().any(|l| l.starts_with(" ● zeta")),
        "{classic:#?}");
    s.tmux(&["set-option", "-g", "@spaces", "on"]);
    let lines = once();
    let body = agents_part(&lines);
    assert!(body[0].ends_with(" ⌥V  this space  ") && body[1].starts_with(" ● work") && body[1].contains("◉1 ◐1 ○1"), "{lines:#?}");
    // (The client sits on work:1: its row is the highlighted one.)
    for t in [" ▌ a  Which deck", " ▎ s  Long running", "   d  Quiet one"] {
        assert!(body.iter().any(|l| l.starts_with(t)), "{t}: {lines:#?}");
    }
    assert!(!body.iter().any(|l| l.contains("zeta") || l.contains("Broken")), "{lines:#?}");
    // NEEDS YOU stays global, with the same letters.
    assert!(lines.iter().any(|l| l.starts_with(" ▌ f  Broken thing")), "{lines:#?}");
    s.tmux(&["set-option", "-g", "@strip_scope", "all"]);
    let lines = once();
    assert!(agents_part(&lines).iter().any(|l| l.starts_with(" ● zeta")), "{lines:#?}");
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
    assert!(!agents_part(&lines).iter().any(|l| l.contains("home")), "{lines:#?}");
    // ...it is the space's shell 1 in the SHELLS section (⌥1, ⌂), lifted:
    // the client is on it.
    let sh = lines.iter().position(|l| l.starts_with(" SHELLS")).unwrap_or_else(|| panic!("{lines:#?}"));
    assert!(lines[sh + 1].starts_with(" ▌ ⌥1  ⌂ home"), "{lines:#?}");
    assert!(lines.iter().any(|l| l.starts_with(" ● work")), "{lines:#?}");
}

/// The spaces read each session's @space_index and @space_hidden off the
/// window rows (session options through the window format): ⌃N digits on
/// the pills, index order, hidden sessions left out; and the agents' groups
/// follow the same order.
#[test]
fn once_lists_spaces_by_space_index() {
    let s = Server::start("idx", 80, 24);
    s.fixture();
    for (sess, n) in [("main", "1"), ("zeta", "2"), ("work", "3")] {
        s.tmux(&["set-option", "-t", sess, "@space_index", n]);
    }
    s.tmux(&["new-session", "-d", "-s", "secret", LIFE]);
    s.tmux(&["set-option", "-t", "secret", "@space_hidden", "1"]);
    let lines = plain(&s.sidebar(&["--once", "34x30", "--client", CLIENT]));
    let pills = spaces_text(&lines);
    let pos: Vec<usize> = [" 1 main ", " 2 zeta ", " 3 work "].iter().map(|n| pills.find(n).unwrap_or_else(|| panic!("{n}: {lines:#?}"))).collect();
    assert!(pos.windows(2).all(|p| p[0] < p[1]), "{lines:#?}");
    assert!(!lines.iter().any(|l| l.contains("secret")), "{lines:#?}");
    // The groups: ⌃N order too (zeta before work, unlike by name).
    assert!(at(&lines, " ● zeta") < at(&lines, " ● work"), "{lines:#?}");
}

/// Right-click → park, end to end, with a FAKE stash.sh (`AGENT_UI_SCRIPTS`):
/// the sidebar hands `run-shell -b` the script with `stash <win>`, the fake
/// moves the window into `stash` like the real one, and the footer says
/// `parked …`; a click on `⏸ N parked` runs prefix h's `unstash '' <client>`.
#[test]
fn right_click_parks_through_stash_sh() {
    let s = Server::start("park", 34, 30);
    s.fixture();
    s.tmux(&["set-option", "-g", "remain-on-exit", "on"]);
    let dir = std::env::temp_dir().join(format!("agentui-park-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let log = dir.join("log");
    let script = format!(
        "#!/bin/sh\necho \"$*\" >> '{}'\n[ \"$1\" = stash ] || exit 0\n\
         tmux set-option -w -t \"$2\" @stash_origin \"$(tmux display-message -p -t \"$2\" '#{{session_name}}')\"\n\
         tmux new-session -d -s stash -n boot 'sleep 600'\ntmux move-window -s \"$2\" -t stash:\ntmux kill-window -t stash:boot\n",
        log.display()
    );
    let path = dir.join("stash.sh");
    std::fs::write(&path, script).unwrap();
    assert!(Command::new("chmod").args(["+x", path.to_str().unwrap()]).status().unwrap().success());
    let (state, focus) = notice_names("park");
    let cmd = format!("env AGENT_UI_CLAUDE_DIR=/nonexistent AGENT_UI_SCRIPTS={} AGENT_STATE_NOTICE={state} AGENT_FOCUS_NOTICE={focus} {BIN} --socket {} sidebar --client {CLIENT}",
        dir.display(), s.name);
    s.tmux(&["new-window", "-d", "-t", "main:9", "-n", "sb", &cmd]);
    let screen = || s.tmux(&["capture-pane", "-p", "-t", "main:9"]);
    wait_for("the first frame", || screen().contains("NEEDS YOU"));
    std::thread::sleep(Duration::from_millis(300));
    let send = |bytes: &str| {
        let hex: Vec<String> = bytes.bytes().map(|b| format!("{b:02x}")).collect();
        let mut a = vec!["send-keys", "-t", "main:9", "-H"];
        a.extend(hex.iter().map(String::as_str));
        s.tmux(&a);
    };
    let lines: Vec<String> = screen().lines().map(String::from).collect();
    let body = at(&lines, " AGENTS");
    let y = body + lines[body..].iter().position(|l| l.contains("Quiet one")).unwrap() + 1;
    let win = s.tmux(&["display-message", "-p", "-t", "work:3", "#{window_id}"]).trim().to_string();
    send(&format!("\x1b[<2;6;{y}M\x1b[<2;6;{y}m"));
    wait_for("the park", || s.tmux(&["list-windows", "-a", "-F", "#{session_name} #{window_id}"]).contains(&format!("stash {win}")));
    assert_eq!(s.tmux(&["show-options", "-wqv", "-t", &win, "@stash_origin"]).trim(), "work");
    post(&state); // tmux's window-unlinked hook does this for real
    wait_for("the footer", || screen().contains("parked Quiet one") && screen().contains("PARKED  1"));
    // The parked line: prefix h's command, the sidebar's client in it.
    let lines: Vec<String> = screen().lines().map(String::from).collect();
    let last = lines.iter().rposition(|l| l.starts_with(" PARKED")).unwrap() + 1;
    send(&format!("\x1b[<0;3;{last}M\x1b[<0;3;{last}m"));
    wait_for("the unstash", || std::fs::read_to_string(&log).unwrap_or_default().contains(&format!("unstash  {CLIENT}")));
    assert_eq!(std::fs::read_to_string(&log).unwrap().lines().next(), Some(format!("stash {win}").as_str()));
    let _ = std::fs::remove_dir_all(&dir);
}

/// A space home as space.sh builds it: a 9-row DASHBOARD pane on top
/// (`@space_dash_pane` 1, cwd `/`) over the shell (cwd /tmp), in `work:0`;
/// the dashboard left ACTIVE (the worst case) → (dashboard, shell) pane ids.
fn home_with_dashboard(s: &Server) -> (String, String) {
    s.tmux(&["new-window", "-d", "-t", "work:0", "-n", "home", "-c", "/tmp", LIFE]);
    s.tmux(&["set-option", "-w", "-t", "work:0", "@space_home", "1"]);
    let shell = s.tmux(&["display-message", "-p", "-t", "work:0", "#{pane_id}"]).trim().to_string();
    let dash = s.tmux(&["split-window", "-b", "-d", "-l", "9", "-t", "work:0", "-c", "/", "-P", "-F", "#{pane_id}", LIFE])
        .trim()
        .to_string();
    s.tmux(&["set-option", "-p", "-t", &dash, "@space_dash_pane", "1"]);
    s.tmux(&["select-pane", "-t", &dash]);
    (dash, shell)
}

/// A space home's dashboard pane, against the real format: never counted,
/// never the window's path or shell label, never the agent pane, and a
/// click on the home shell (⌥1) leaves focus on the shell under it.
#[test]
fn the_dashboard_pane_is_never_a_target() {
    let s = Server::start("dash", 34, 30);
    s.fixture();
    let (dash, shell) = home_with_dashboard(&s);
    let tmux = agent_ui::Tmux::with_socket(Some(Socket::Name(s.name.clone())));
    let snap = tmux.snapshot();
    let home = snap.windows.iter().find(|w| w.home).expect("the home row");
    assert_eq!((home.panes, home.has_dash(), home.focus_pane()), (1, true, Some(shell.as_str())), "{home:?}");
    assert!(home.path.ends_with("/tmp"), "{:?}", home.path);
    assert_eq!(home.capture_target(), shell);
    assert_eq!(agent_ui::Actions::new(tmux.clone()).agent_pane(&home.id, &home.kind), Ok(shell.clone()));
    // No dashboard elsewhere: the plain count and path, as before.
    let other = snap.windows.iter().find(|w| w.session == "work" && w.index == 1).unwrap();
    assert_eq!((other.panes, other.has_dash(), other.pane_list.len()), (1, false, 1));
    // The status block: shell 1 is the home, labelled by the SHELL's cwd.
    let tty = s.nested_client("work", "main:8");
    let lines = plain(&s.sidebar(&["--once", "34x30", "--client", &tty]));
    let home_row = |lines: &[String]| lines.iter().position(|l| l.contains("⌥1  ⌂ tmp"));
    assert!(home_row(&lines).is_some(), "{lines:#?}");
    // The live sidebar, with a fake agent-jump.sh that just switches the
    // client: a click on ⌥1 lands on the home with the shell focused.
    let dir = std::env::temp_dir().join(format!("agentui-dash-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let jump = dir.join("agent-jump.sh");
    std::fs::write(&jump, "#!/bin/sh\n[ \"$1\" = goto ] && tmux switch-client -c \"$2\" -t \"$3\"\nexit 0\n").unwrap();
    let (state, focus) = notice_names("dash");
    let cmd = format!("env AGENT_UI_CLAUDE_DIR=/nonexistent AGENT_UI_SCRIPTS={} AGENT_STATE_NOTICE={state} AGENT_FOCUS_NOTICE={focus} {BIN} --socket {} sidebar --client {tty}",
        dir.display(), s.name);
    s.tmux(&["set-option", "-g", "remain-on-exit", "on"]);
    s.tmux(&["new-window", "-d", "-t", "main:9", "-n", "sb", &cmd]);
    let screen = || s.tmux(&["capture-pane", "-p", "-t", "main:9"]);
    let lines = || screen().lines().map(String::from).collect::<Vec<_>>();
    wait_for("the first frame", || home_row(&lines()).is_some());
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(s.tmux(&["display-message", "-p", "-t", "work:0", "#{pane_id}"]).trim(), dash);
    // A click on the ⌥1 row, wherever the SHELLS section put it.
    let (x, y) = (8, home_row(&lines()).unwrap() + 1);
    let click = format!("\x1b[<0;{x};{y}M\x1b[<0;{x};{y}m");
    let hex: Vec<String> = click.bytes().map(|b| format!("{b:02x}")).collect();
    let mut a = vec!["send-keys", "-t", "main:9", "-H"];
    a.extend(hex.iter().map(String::as_str));
    s.tmux(&a);
    wait_for("the client on the home", || {
        s.tmux(&["list-clients", "-F", "#{client_tty} #{window_index}"]).lines().any(|l| l == format!("{tty} 0"))
    });
    wait_for("the shell focused", || s.tmux(&["display-message", "-p", "-t", "work:0", "#{pane_id}"]).trim() == shell);
    let _ = std::fs::remove_dir_all(&dir);
}

/// The prefix / ⌥W marks clear with NO notice: tmux posts one on entering
/// a key table but none on leaving it, so the sidebar re-reads the table
/// while it is not root (well inside the 5 s safety net, which is all
/// that runs here: nothing pulses).
#[test]
fn key_table_marks_clear_without_a_notice() {
    let s = Server::start("keys", 34, 30);
    s.fixture();
    s.tmux(&["set-option", "-w", "-t", "work:2", "@agent_state", "idle"]);
    s.tmux(&["set-option", "-g", "remain-on-exit", "on"]);
    s.tmux(&["bind-key", "-T", "jump", "a", "display-message", "a"]);
    let tty = s.nested_client("work", "main:8");
    let (state, focus) = notice_names("keys");
    let cmd = format!("env AGENT_UI_CLAUDE_DIR=/nonexistent AGENT_STATE_NOTICE={state} AGENT_FOCUS_NOTICE={focus} {BIN} --socket {} sidebar --client {tty}", s.name);
    s.tmux(&["new-window", "-d", "-t", "main:9", "-n", "sb", &cmd]);
    let screen = || s.tmux(&["capture-pane", "-p", "-t", "main:9"]);
    wait_for("the first frame", || screen().contains("NEEDS YOU"));
    std::thread::sleep(Duration::from_millis(300));
    let card = || screen().lines().take(4).collect::<Vec<_>>().join("\n");
    // (No usage here, so no card: the mark gets a row of its own, which
    // capture-pane trims.)
    for (table, want) in [("prefix", "  prefix"), ("jump", "⌥W jump")] {
        // Entering the table posts the notice (the root binding does)...
        s.tmux(&["switch-client", "-c", &tty, "-T", table]);
        post(&focus);
        wait_for(table, || card().contains(want));
        // ...leaving it posts nothing: the poll sees root and redraws.
        std::thread::sleep(Duration::from_millis(400));
        s.tmux(&["switch-client", "-c", &tty, "-T", "root"]);
        let t = Instant::now();
        wait_for("the mark to clear", || !card().contains(want));
        assert!(t.elapsed() < Duration::from_millis(1500), "{table}: {:?}", t.elapsed());
    }
    assert_eq!(s.tmux(&["display-message", "-p", "-t", "main:9", "#{pane_dead}"]).trim(), "0");
}

/// scripts/notify.sh end to end, pointed at this server (`NOTIFY_TMUX`), its
/// notice under this test's name and its log in a scratch dir → its status.
fn notify(s: &Server, focus: &str, log: &std::path::Path, args: &[&str]) -> std::process::ExitStatus {
    let script = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../scripts/executable_notify.sh");
    let o = Command::new("/bin/zsh")
        .arg("-f")
        .arg(script)
        .args(args)
        .env_remove("TMUX")
        .env_remove("TMUX_PANE")
        .env("NOTIFY_TMUX", format!("tmux -L {} -f /dev/null", s.name))
        .env("AGENT_FOCUS_NOTICE", focus)
        .env("NOTIFY_LOG", log)
        .output()
        .expect("run notify.sh");
    assert!(o.stdout.is_empty() && o.stderr.is_empty(), "{o:?}");
    o.status
}

/// notify.sh with a sidebar open for the client: a toast card over the top
/// of the sidebar within moments (the status line stays off and tmux shows
/// no message), gone at its deadline with no notice; a click dismisses
/// one; two at once stack, oldest on top. A client with no sidebar gets
/// tmux's own display-message, and so does every client once the sidebar
/// has gone.
#[test]
fn notify_toasts_in_the_sidebar_and_falls_back_without_one() {
    let s = Server::start("toast", 34, 40);
    s.fixture();
    s.tmux(&["set-option", "-g", "remain-on-exit", "on"]);
    s.tmux(&["set-option", "-g", "@codex_session_text", "21%"]); // usage: the card
    let tty = s.nested_client("work", "main:8");
    let dir = std::env::temp_dir().join(format!("agentui-toast-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let log = dir.join("log");
    let (state, focus) = notice_names("toast");
    let cmd = format!("env AGENT_UI_CLAUDE_DIR=/nonexistent AGENT_STATE_NOTICE={state} AGENT_FOCUS_NOTICE={focus} {BIN} --socket {} sidebar --client {tty}", s.name);
    s.tmux(&["new-window", "-d", "-t", "main:9", "-n", "sb", &cmd]);
    let screen = || s.tmux(&["capture-pane", "-p", "-t", "main:9"]);
    let opt = |o: &str| s.tmux(&["show-options", "-gqv", o]).trim().to_string();
    wait_for("the first frame", || screen().contains("NEEDS YOU") && screen().contains("21%"));
    // The registry says which client has a sidebar.
    assert_eq!((opt("@sidebar_clients"), opt("status")), (tty.clone(), "off".into()));
    // tmux's message log (commands are logged too): a display-message shows
    // up as `<client> message: <text>`.
    let said = |c: &str, text: &str| {
        s.tmux(&["show-messages"]).lines().any(|l| l.contains(&format!("{c} message: ")) && l.ends_with(text))
    };
    // A toast for this client.
    let t = Instant::now();
    assert!(notify(&s, &focus, &log, &["-c", &tty, "-k", "ok", "-t", "2", "--", "parked", "Canvas\tSyllabus"]).success());
    wait_for("the toast", || screen().contains("│ parked Canvas Syllabus"));
    assert!(t.elapsed() < Duration::from_millis(1500), "{:?}", t.elapsed());
    // A card: the kind's title in its top border.
    let top = screen().lines().next().unwrap_or_default().to_string();
    assert!(top.starts_with(" ╭─ \u{f058} Done ─"), "{top:?}");
    // (Untrimmed: an empty title is a trailing tab.)
    let raw = s.tmux(&["show-options", "-gqv", "@toast"]);
    let f: Vec<&str> = raw.trim_end_matches('\n').split('\t').collect();
    assert_eq!((f.len(), f[0], f[2], f[3], f[4], f[5], f[6]), (7, opt("@toast_seq").as_str(), "ok", "2", tty.as_str(), "parked Canvas Syllabus", ""));
    assert_eq!((opt("status"), said(&tty, "Syllabus")), ("off".into(), false), "no status line, no tmux message");
    // Gone at its deadline: no notice, the sidebar's own wakeup.
    wait_for("the toast to expire", || !screen().contains("parked Canvas"));
    let gone = t.elapsed();
    assert!(gone >= Duration::from_millis(1900) && gone < Duration::from_millis(3500), "{gone:?}");
    // A click on a long-lived one dismisses it (here).
    assert!(notify(&s, &focus, &log, &["-c", &tty, "-t", "60", "click me"]).success());
    wait_for("the second toast", || screen().contains("│ click me"));
    let y = screen().lines().position(|l| l.contains("│ click me")).unwrap() + 1;
    let click = format!("\x1b[<0;6;{y}M\x1b[<0;6;{y}m");
    let hex: Vec<String> = click.bytes().map(|b| format!("{b:02x}")).collect();
    let mut a = vec!["send-keys", "-t", "main:9", "-H"];
    a.extend(hex.iter().map(String::as_str));
    s.tmux(&a);
    wait_for("the dismissal", || !screen().contains("click me"));
    // No -c: the calling context's client (here the only one) → a toast too;
    // with -T, its own title. Two at once: stacked, the older on top.
    assert!(notify(&s, &focus, &log, &["-k", "warn", "-T", "Heads up", "no client given"]).success());
    assert!(notify(&s, &focus, &log, &["-k", "error", "-t", "60", "and another"]).success());
    wait_for("the context's toasts", || screen().contains("│ no client given") && screen().contains("│ and another"));
    let lines: Vec<String> = screen().lines().map(String::from).collect();
    assert!(lines[0].contains("\u{f071} Heads up") && lines[1].contains("no client given") && lines[3].contains("\u{f057} Error")
        && lines[4].contains("and another"), "{lines:#?}");
    // A second client, with no sidebar: tmux's own message, no toast.
    let other = s.nested_client("zeta", "main:7");
    let seq = opt("@toast_seq");
    assert!(notify(&s, &focus, &log, &["-c", &other, "-k", "error", "for the other one"]).success());
    wait_for("the other client's message", || said(&other, "for the other one"));
    assert_eq!(opt("@toast_seq"), seq);
    // The sidebar gone: the old display-message for its client too.
    let pid = s.tmux(&["display-message", "-p", "-t", "main:9", "#{pane_pid}"]);
    Command::new("kill").args(["-TERM", pid.trim()]).status().unwrap();
    wait_for("the exit", || opt("@sidebar_open") == "0");
    assert_eq!(opt("@sidebar_clients"), "");
    assert!(notify(&s, &focus, &log, &["-c", &tty, "back to tmux"]).success());
    wait_for("the client's message", || said(&tty, "back to tmux"));
    assert_eq!(opt("@toast_seq"), seq);
    assert!(!s.tmux(&["show-messages"]).lines().any(|l| l.contains(" message: ") && (l.contains("click me") || l.contains("no client"))));
    // A crashed sidebar (SIGKILL: nothing unlists it) stays in the registry,
    // but notify.sh checks its pid: tmux's own message, never a lost toast.
    s.tmux(&["new-window", "-d", "-t", "main:6", "-n", "sb2", &cmd]);
    wait_for("the second sidebar", || opt("@sidebar_clients") == tty);
    assert!(notify(&s, &focus, &log, &["-c", &tty, "-t", "60", "alive"]).success());
    wait_for("its toast", || s.tmux(&["capture-pane", "-p", "-t", "main:6"]).contains("│ alive"));
    let pid = s.tmux(&["display-message", "-p", "-t", "main:6", "#{pane_pid}"]);
    Command::new("kill").args(["-KILL", pid.trim()]).status().unwrap();
    wait_for("the crash", || s.tmux(&["display-message", "-p", "-t", "main:6", "#{pane_dead}"]).trim() == "1");
    assert_eq!((opt("@sidebar_clients"), opt("@sidebar_open")), (tty.clone(), "1".into()), "still listed");
    let seq = opt("@toast_seq");
    assert!(notify(&s, &focus, &log, &["-c", &tty, "after the crash"]).success());
    wait_for("the crash fallback", || said(&tty, "after the crash"));
    assert_eq!(opt("@toast_seq"), seq);
    // Every message logged: epoch, kind, client, text (and a -T title).
    let lines: Vec<String> = std::fs::read_to_string(&log).unwrap().lines().map(String::from).collect();
    let want = [("ok", &tty, "parked Canvas Syllabus"), ("info", &tty, "click me"), ("warn", &tty, "no client given\tHeads up"),
        ("error", &tty, "and another"), ("error", &other, "for the other one"), ("info", &tty, "back to tmux"),
        ("info", &tty, "alive"), ("info", &tty, "after the crash")];
    assert_eq!(lines.len(), want.len(), "{lines:#?}");
    for (l, (k, c, t)) in lines.iter().zip(want) {
        let f: Vec<&str> = l.splitn(4, '\t').collect();
        assert!(f.len() == 4 && f[0].parse::<u64>().is_ok() && (f[1], f[2], f[3]) == (k, c.as_str(), t), "{lines:#?}");
    }
    let _ = std::fs::remove_dir_all(&dir);
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
    let cmd = format!("env AGENT_UI_CLAUDE_DIR=/nonexistent AGENT_STATE_NOTICE={state} AGENT_FOCUS_NOTICE={focus} {BIN} --socket {} sidebar --client {CLIENT}", s.name);
    s.tmux(&["new-window", "-d", "-t", "main:9", "-n", "sb", &cmd]);
    let screen = || s.tmux(&["capture-pane", "-p", "-t", "main:9"]);
    wait_for("the first frame", || screen().contains("NEEDS YOU"));
    // The marker CMD+B finds the pane by: the title.
    assert_eq!(s.tmux(&["display-message", "-p", "-t", "main:9", "#{pane_title}"]).trim(), "agent-strip");
    std::thread::sleep(Duration::from_millis(300));

    // A state change, then its notice: on screen well before the 5 s poll.
    s.tmux(&["set-option", "-w", "-t", "work:3", "@agent_state", "needs-input"]);
    let t = Instant::now();
    post(&state);
    wait_for("the state notice", || screen().lines().any(|l| l.starts_with(" ▌") && l.contains("Quiet one")));
    assert!(t.elapsed() < Duration::from_millis(1500), "{:?}", t.elapsed());
    // A rename with the focus notice, the same.
    s.tmux(&["rename-session", "-t", "zeta", "omega"]);
    let t = Instant::now();
    post(&focus);
    wait_for("the focus notice", || screen().contains(" omega"));
    assert!(t.elapsed() < Duration::from_millis(1500), "{:?}", t.elapsed());

    // The wheel scrolls nothing; a click on a blank cell only hands focus
    // back, and the sidebar keeps running.
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
    // The status line's one job, which the sidebar must keep running once
    // it hides the line (as continuum's autosave in the real status-right).
    let dir = std::env::temp_dir().join(format!("agentui-keeper-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let job = dir.join("ran");
    s.tmux(&["set-option", "-g", "status-interval", "1"]);
    s.tmux(&["set-option", "-g", "status-right", &format!("#(touch '{}')x", job.display())]);
    let (state, focus) = notice_names("alone");
    let cmd = format!("env AGENT_UI_CLAUDE_DIR=/nonexistent AGENT_STATE_NOTICE={state} AGENT_FOCUS_NOTICE={focus} {BIN} --socket {} sidebar --client {tty}", s.name);
    s.tmux(&["new-window", "-d", "-t", "main:9", "-n", "sb", &cmd]);
    let screen = || s.tmux(&["capture-pane", "-p", "-t", "main:9"]);
    wait_for("the first frame", || screen().contains("NEEDS YOU"));
    std::thread::sleep(Duration::from_millis(300));
    assert!(screen().contains(" AGENTS"), "{}", screen());
    // Open: counted, the status line hidden; its job still runs.
    let opt = |o: &str| s.tmux(&["show-options", "-gqv", o]).trim().to_string();
    let pid = s.tmux(&["display-message", "-p", "-t", "main:9", "#{pane_pid}"]).trim().to_string();
    assert_eq!((opt("@sidebar_open"), opt("@sidebar_pids"), opt("status")), ("1".into(), pid.clone(), "off".into()));
    wait_for("the status job", || job.exists());
    // Letters persisted to @agent_key, as the menu would (work a s d, zeta f).
    wait_for("the letters", || s.tmux(&["show-options", "-wqv", "-t", "work:3", "@agent_key"]).trim() == "d");
    assert!(screen().lines().any(|l| l.starts_with("   d  Quiet one")), "{}", screen());
    // The client's key table, told by the focus notice: the prefix mark, the
    // ⌥W hint, prefix v's selection.
    // (UB's ⌥W table and stash.sh's, here with one key each so they exist.)
    s.tmux(&["bind-key", "-T", "jump", "a", "display-message", "a"]);
    s.tmux(&["bind-key", "-T", "stash", "Escape", "display-message", "esc"]);
    for (table, want) in [("prefix", "  prefix"), ("jump", "⌥W jump"), ("stash", "0 selected")] {
        s.tmux(&["switch-client", "-c", &tty, "-T", table]);
        post(&focus);
        wait_for(table, || screen().contains(want));
        s.tmux(&["switch-client", "-c", &tty, "-T", "root"]);
        post(&focus);
        wait_for("back to root", || !screen().contains(want));
    }
    s.tmux(&["set-option", "-w", "-t", "work:3", "@stash_sel", "1"]);
    post(&focus);
    wait_for("a leaked selection", || screen().contains("1 selected"));
    s.tmux(&["set-option", "-wu", "-t", "work:3", "@stash_sel"]);
    // The cheap check: a crashed sidebar's pid and a wrong count, repaired.
    s.tmux(&["set-option", "-g", "@sidebar_pids", &format!("999999 {pid}")]);
    s.tmux(&["set-option", "-g", "@sidebar_open", "7"]);
    post(&focus);
    wait_for("the repair", || opt("@sidebar_open") == "1" && opt("@sidebar_pids") == pid);
    // A config reload's `set -g status on` while it is open: hidden again.
    std::thread::sleep(Duration::from_millis(2100)); // repairs are at most every 2 s
    s.tmux(&["set-option", "-g", "status", "on"]);
    post(&focus);
    wait_for("the line hidden again", || opt("status") == "off");
    // @strip_scope set from outside (tmux's M-v) plus the focus notice: the
    // agents section follows at once, and back.
    for (scope, title) in [("space", "this space"), ("all", "all spaces")] {
        s.tmux(&["set-option", "-g", "@strip_scope", scope]);
        let t = Instant::now();
        post(&focus);
        wait_for("the scope from outside", || screen().lines().any(|l| l.starts_with(" AGENTS") && l.contains(title)));
        assert!(t.elapsed() < Duration::from_millis(1500), "{:?}", t.elapsed());
    }
    // A window switch on the client, then the focus notice (tmux's hooks
    // post it): the highlight (and its blue edge) follows.
    s.tmux(&["select-window", "-t", "work:3"]);
    post(&focus);
    wait_for("the window switch", || screen().lines().any(|l| l.starts_with(" ▌") && l.contains("Quiet one")));
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
    // The never-alone exit unlisted it: none open, the status line back.
    assert_eq!((opt("@sidebar_open"), opt("@sidebar_pids"), opt("status")), ("0".into(), String::new(), "on".into()));
    let _ = std::fs::remove_dir_all(&dir);
}

/// The keeper runs the hidden status line's `#()` jobs silenced: a job that
/// prints and one that fails leave every pane out of view mode (a bare
/// `run-shell -b` would show both on the client's active pane).
#[test]
fn status_jobs_run_quietly() {
    let s = Server::start("quiet", 34, 30);
    s.fixture();
    let tty = s.nested_client("work", "main:8");
    let dir = std::env::temp_dir().join(format!("agentui-quiet-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let job = dir.join("ran");
    s.tmux(&["set-option", "-g", "status-interval", "1"]);
    s.tmux(&["set-option", "-g", "status-right", &format!("#(date +%S)#(echo oops >&2; false)#(touch '{}')x", job.display())]);
    let (state, focus) = notice_names("quiet");
    let cmd = format!("env AGENT_UI_CLAUDE_DIR=/nonexistent AGENT_STATE_NOTICE={state} AGENT_FOCUS_NOTICE={focus} {BIN} --socket {} sidebar --client {tty}", s.name);
    s.tmux(&["new-window", "-d", "-t", "main:9", "-n", "sb", &cmd]);
    wait_for("the status job", || job.exists());
    std::fs::remove_file(&job).unwrap();
    wait_for("a second round", || job.exists());
    std::thread::sleep(Duration::from_millis(500));
    let modes = s.tmux(&["list-panes", "-a", "-F", "#{session_name}:#{window_index} #{pane_in_mode}"]);
    assert!(modes.lines().all(|l| l.ends_with(" 0")), "{modes}");
    let _ = std::fs::remove_dir_all(&dir);
}

/// `@sidebar_open` counts the open sidebars and hides the status line while
/// any is; every exit unlists, a crash (SIGKILL) is repaired by the next
/// sidebar to start or by `sidebar --repair`.
#[test]
fn the_registry_counts_sidebars_and_repairs_crashes() {
    let s = Server::start("reg", 34, 30);
    s.fixture();
    s.tmux(&["set-option", "-g", "remain-on-exit", "on"]);
    let opt = |o: &str| s.tmux(&["show-options", "-gqv", o]).trim().to_string();
    let (state, focus) = notice_names("reg");
    let start = |win: &str| -> String {
        let cmd = format!("env AGENT_UI_CLAUDE_DIR=/nonexistent AGENT_STATE_NOTICE={state} AGENT_FOCUS_NOTICE={focus} {BIN} --socket {} sidebar --client {CLIENT}", s.name);
        s.tmux(&["new-window", "-d", "-t", win, "-n", "sb", &cmd]);
        wait_for("a first frame", || s.tmux(&["capture-pane", "-p", "-t", win]).contains("NEEDS YOU"));
        s.tmux(&["display-message", "-p", "-t", win, "#{pane_pid}"]).trim().to_string()
    };
    let kill = |sig: &str, pid: &str| assert!(Command::new("kill").args([sig, pid]).status().unwrap().success());
    assert_eq!((opt("@sidebar_open"), opt("status")), (String::new(), "on".into()));
    let p7 = start("main:7");
    assert_eq!((opt("@sidebar_open"), opt("@sidebar_pids"), opt("status")), ("1".into(), p7.clone(), "off".into()));
    let p8 = start("main:8");
    assert_eq!((opt("@sidebar_open"), opt("@sidebar_pids")), ("2".into(), format!("{p7} {p8}")));
    // A normal exit (CMD+B's SIGTERM): one left, the line still hidden.
    kill("-TERM", &p7);
    wait_for("the exit", || opt("@sidebar_open") == "1");
    assert_eq!((opt("@sidebar_pids"), opt("status")), (p8.clone(), "off".into()));
    // A crash: nothing runs, the count is stale...
    kill("-KILL", &p8);
    wait_for("the crash", || s.tmux(&["display-message", "-p", "-t", "main:8", "#{pane_dead}"]).trim() == "1");
    assert_eq!(opt("@sidebar_open"), "1");
    // ...until the next sidebar starts: the dead one dropped.
    let p9 = start("main:9");
    assert_eq!((opt("@sidebar_open"), opt("@sidebar_pids"), opt("status")), ("1".into(), p9.clone(), "off".into()));
    // Or a cheap `--repair` (no sidebar left): the status line comes back.
    kill("-KILL", &p9);
    wait_for("the crash", || s.tmux(&["display-message", "-p", "-t", "main:9", "#{pane_dead}"]).trim() == "1");
    // Quiet (a run-shell hook would show output in view mode); -v counts.
    assert_eq!(s.sidebar(&["--repair"]), "");
    assert_eq!((opt("@sidebar_open"), opt("@sidebar_pids"), opt("status")), ("0".into(), String::new(), "on".into()));
    // Nothing to repair: the status line is left as it is.
    s.tmux(&["set-option", "-g", "status", "off"]);
    assert_eq!(s.sidebar(&["--repair", "-v"]).trim(), "0");
    assert_eq!(opt("status"), "off");
    s.tmux(&["set-option", "-g", "status", "on"]);
    // A reload's `status on` with a sidebar open: --repair hides it again.
    let p10 = start("main:10");
    s.tmux(&["set-option", "-g", "status", "on"]);
    assert_eq!(s.sidebar(&["--repair"]), "");
    assert_eq!((opt("@sidebar_open"), opt("@sidebar_pids"), opt("status")), ("1".into(), p10.clone(), "off".into()));
    kill("-TERM", &p10);
    wait_for("the exit", || opt("status") == "on");
    // Against no server at all: still status 0, still silent.
    let o = Command::new(BIN).args(["--socket", &format!("{}-none", s.name), "sidebar", "--repair"]).output().unwrap();
    assert!(o.status.success() && o.stdout.is_empty() && o.stderr.is_empty(), "{o:?}");
}
