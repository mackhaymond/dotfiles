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

use agent_ui::answer::{answer_seen, plan, Asked, Dialog, PaneState, Seen, Verdict};
use agent_ui::{Actions, Socket, Tmux};
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
        self.answer_with(win, verdict, &[])
    }

    /// `answer` with more arguments after the verdict (`--since …`).
    fn answer_with(&self, win: &str, verdict: &str, more: &[&str]) -> Output {
        Command::new(BIN)
            .args(["--socket", &self.name, "answer", win, verdict])
            .args(more)
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

/// A detail with everything tmux's command and format parsers care about:
/// quotes, `#{…}`, `#(…)`, braces, `,`, `;`, `$`, backticks, a backslash.
const NASTY: &str = "Bash a,b {x} }y \"q\" 'sq' $(id) ${HOME} `id` #{pane_id} ##x #(echo pwned) ;x \\ end";

/// CuaNotch names the prompt it drew (`--since` / `--detail`, raw): another
/// one by now is refused (exit 1, said through notify.sh, nothing sent); a
/// flag without its value or an unknown flag is usage (exit 2, the old
/// builds' fallback); the same prompt, written differently (the since's
/// number alone, the detail with a trailing newline), is answered.
#[test]
fn cuanotch_names_the_prompt_it_drew() {
    let s = Server::start("s");
    let w = s.agent("stale", "perm_bash", "claude", "perm");
    s.tmux(&["set-option", "-w", "-t", &w, "@agent_detail", NASTY]);
    assert_eq!(s.opt(&w, "@agent_detail"), NASTY);
    let since = s.opt(&w, "@agent_since");
    let why = "stale: that prompt changed · look again";
    for more in [
        &["--since", "1699999999 needs-input"][..],
        &["--detail", "Bash ls"],
        &["--since", &since, "--detail", &NASTY.replace('#', "##")],
        &["--since", "", "--detail", NASTY],
        &["--detail", NASTY, "--since", "1700000000.5"],
    ] {
        let o = s.answer_with(&w, "allow", more);
        assert_eq!(o.status.code(), Some(1), "{more:?}: {o:?}");
        assert!(String::from_utf8_lossy(&o.stderr).contains(why), "{more:?}: {o:?}");
    }
    assert!(s.notified().contains(&format!("-k warn -- {why}")), "{}", s.notified());
    for more in [&["--since"][..], &["--detail"], &["--since", &since, "--detail"], &["--bogus", "1"], &["--since=1700000000"]] {
        let o = s.answer_with(&w, "allow", more);
        assert_eq!(o.status.code(), Some(2), "{more:?}: {o:?}");
        assert!(String::from_utf8_lossy(&o.stderr).contains("--since <raw @agent_since>"), "{o:?}");
    }
    assert_eq!(s.got("stale"), "");
    assert_eq!(s.opt(&w, "@agent_state"), "needs-input");

    let detail = format!("{NASTY}\n");
    let o = s.answer_with(&w, "allow", &["--since", "1700000000", "--detail", &detail, "--client", "/dev/null"]);
    assert!(o.status.success(), "{o:?}");
    assert_eq!(String::from_utf8_lossy(&o.stdout).trim(), "allowed stale");
    assert_eq!(s.got("stale"), "31");
    assert_eq!(s.opt(&w, "@agent_state"), "idle");
}

/// The race: CuaNotch's values match the snapshot `answer` read, then the
/// window is asked again before the send. The guard (the snapshot's raw
/// values, checked by tmux at the send) refuses; when the window no longer
/// matches what CuaNotch drew that is said as "that prompt changed", and a
/// re-ask that reads the same to CuaNotch is still refused (the screen).
#[test]
fn a_re_ask_after_the_read_is_refused_at_the_send() {
    let s = Server::start("r");
    let w = s.agent("race", "perm_bash", "claude", "perm");
    s.tmux(&["set-option", "-w", "-t", &w, "@agent_detail", NASTY]);
    let since = s.opt(&w, "@agent_since");
    let tmux = Tmux::with_socket(Some(Socket::Name(s.name.clone())));
    let actions = Actions::new(tmux.clone());
    let seen = Seen { since: Some(&since), detail: Some(NASTY) };
    let read = || {
        let snap = tmux.snapshot();
        assert!(seen.matches(snap.rows(&w).next().expect("the window")), "{snap:?}");
        snap
    };
    let set = |k: &str, v: &str| s.tmux(&["set-option", "-w", "-t", &w, k, v]);
    let changed = Err("race: that prompt changed · look again".to_string());

    let snap = read();
    set("@agent_since", "1700000001 needs-input");
    assert_eq!(answer_seen(&actions, &snap, &w, Verdict::Allow, None, seen), changed);

    set("@agent_since", &since);
    let snap = read();
    set("@agent_detail", "Bash rm -rf ~");
    assert_eq!(answer_seen(&actions, &snap, &w, Verdict::Deny, None, seen), changed);

    // Asked again with what normalises to the same prompt: still not the
    // one the snapshot (and so the guard) pinned.
    set("@agent_detail", NASTY);
    let snap = read();
    set("@agent_detail", &format!("{NASTY} "));
    assert_eq!(answer_seen(&actions, &snap, &w, Verdict::Allow, None, seen), Err("race: its screen changed · try again".into()));

    assert_eq!(s.got("race"), "");
    assert_eq!(s.opt(&w, "@agent_state"), "needs-input");
}

/// CuaNotch drew a perm prompt; by the click it is a question (same
/// `@agent_since`, `@agent_detail_kind` ask, a new detail): "that prompt
/// changed", not "no permission prompt pending", and nothing sent.
#[test]
fn a_perm_prompt_that_became_a_question() {
    let s = Server::start("q");
    let w = s.agent("asked", "perm_bash", "claude", "perm");
    s.tmux(&["set-option", "-w", "-t", &w, "@agent_detail", "Bash touch probe.txt"]);
    let since = s.opt(&w, "@agent_since");
    s.tmux(&["set-option", "-w", "-t", &w, "@agent_detail_kind", "ask"]);
    s.tmux(&["set-option", "-w", "-t", &w, "@agent_detail", "Which deck?"]);
    let o = s.answer_with(&w, "allow", &["--since", &since, "--detail", "Bash touch probe.txt"]);
    assert_eq!(o.status.code(), Some(1), "{o:?}");
    assert!(String::from_utf8_lossy(&o.stderr).contains("asked: that prompt changed · look again"), "{o:?}");
    assert_eq!(s.got("asked"), "");
    assert_eq!(s.opt(&w, "@agent_state"), "needs-input");
}

/// The global `--socket` / `-L` is read only before the subcommand: a raw
/// detail that reads `-L` (or `--socket`) is the detail, never the socket.
#[test]
fn a_detail_reading_like_the_socket_flag() {
    let s = Server::start("l");
    for (name, detail) in [("dashl", "-L"), ("dashsock", "--socket")] {
        let w = s.agent(name, "perm_bash", "claude", "perm");
        s.tmux(&["set-option", "-w", "-t", &w, "@agent_detail", detail]);
        let since = s.opt(&w, "@agent_since");
        let o = s.answer_with(&w, "allow", &["--detail", detail, "--since", &since]);
        assert!(o.status.success(), "{name}: {o:?}");
        assert_eq!(String::from_utf8_lossy(&o.stdout).trim(), format!("allowed {name}"));
        assert_eq!(s.got(name), "31", "{name}");
    }
    // After the subcommand, a stray `-L` is the subcommand's: usage.
    let o = s.answer_with("@1", "allow", &["-L", &s.name]);
    assert_eq!(o.status.code(), Some(2), "{o:?}");
}
