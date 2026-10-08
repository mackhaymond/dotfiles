//! Against a REAL private tmux server (`tmux -L agentui-test-<pid>-<tag>`,
//! started with `-f /dev/null` so no user config, plugin or watcher loads):
//!
//! - `agent-ui needs` == `agent-jump.sh list`, byte for byte, under en_US.UTF-8
//!   and C, over a fixture that exercises every rule of the queue (tiers,
//!   stamps, excluded sessions, linked windows, weightless names, plain
//!   windows, summary/name fallback);
//! - `dump --json` over the same server;
//! - the action plumbing reaches THAT server (scripts get `TMUX=<socket>`,
//!   run-shell runs there), peek captures, and close refuses to guess.
//!
//! Every server is killed by a Drop guard, so a panic anywhere still cleans
//! up; its windows run `sleep 600`, so even a SIGKILLed test run leaves a
//! server that exits on its own within ten minutes (exit-empty).

use agent_ui::actions::{Actions, PANE_UNSURE};
use agent_ui::json::{self, Value};
use agent_ui::{Socket, Tmux};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::{Duration, Instant};

const BIN: &str = env!("CARGO_BIN_EXE_agent-ui");
const LIFE: &str = "sleep 600";

fn jump_script() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../scripts/executable_agent-jump.sh")
}

/// A private tmux server, killed on drop.
struct Server {
    name: String,
    /// Set: the server is the "default" socket of a private TMUX_TMPDIR, so
    /// plain `tmux` (no -L, no $TMUX) reaches it when that env var is set.
    tmpdir: Option<PathBuf>,
}

impl Server {
    fn start(tag: &str) -> Server {
        let s = Server { name: format!("agentui-test-{}-{tag}", std::process::id()), tmpdir: None };
        // The guard exists before the server does: a panic below still kills it.
        s.boot();
        s
    }

    /// A server on `<private TMUX_TMPDIR>/tmux-<uid>/default`.
    fn start_default(tag: &str) -> Server {
        let dir = PathBuf::from(format!("/private/tmp/agentui-test-{}-{tag}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let s = Server { name: "default".into(), tmpdir: Some(dir) };
        s.boot();
        s
    }

    fn boot(&self) {
        // -f /dev/null: never the user's tmux.conf (its plugins restore and
        // resume real sessions, and its hooks start the real watcher).
        self.tmux(&["-f", "/dev/null", "new-session", "-d", "-s", "boot", "-x", "120", "-y", "40", LIFE]);
    }

    /// Plain `tmux` reaching this server: only meaningful with `tmpdir`.
    fn env_for_plain_tmux(&self, c: &mut Command) {
        c.env_remove("TMUX").env_remove("TMUX_PANE");
        if let Some(d) = &self.tmpdir {
            c.env("TMUX_TMPDIR", d);
        }
    }

    fn socket_file(&self) -> PathBuf {
        match &self.tmpdir {
            Some(d) => d.join(format!("tmux-{}", agent_ui::tmux::uid())).join(&self.name),
            None => Socket::Name(self.name.clone()).path(),
        }
    }

    fn cmd(&self) -> Command {
        let mut c = Command::new("tmux");
        c.args(["-L", &self.name]);
        self.env_for_plain_tmux(&mut c);
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

    fn socket_path(&self) -> String {
        self.tmux(&["display-message", "-p", "-t", "=boot:", "#{socket_path}"])
    }

    /// A window `session:index` (session created on first use) → its id.
    fn window(&self, session: &str, index: u32, name: &str, cmd: &str) -> String {
        let exists = self.try_tmux(&["has-session", "-t", &format!("={session}")]).status.success();
        if exists {
            return self.tmux(&["new-window", "-d", "-P", "-F", "#{window_id}", "-t", &format!("={session}:{index}"), "-n", name, cmd]);
        }
        let id = self.tmux(&["new-session", "-d", "-P", "-F", "#{window_id}", "-s", session, "-n", name, cmd]);
        if index != 0 {
            self.tmux(&["move-window", "-d", "-s", &id, "-t", &format!("={session}:{index}")]);
        }
        id
    }

    fn opt(&self, id: &str, k: &str, v: &str) {
        self.tmux(&["set-option", "-w", "-t", id, k, v]);
    }

    /// An agent window with @agent_state/@agent_since/@agent_workflow/@agent_summary.
    fn agent(&self, session: &str, index: u32, state: &str, since: Option<&str>, workflow: &str, summary: &str) -> String {
        let id = self.window(session, index, "claude", LIFE);
        if !state.is_empty() {
            self.opt(&id, "@agent_state", state);
        }
        if let Some(s) = since {
            self.opt(&id, "@agent_since", s);
        }
        if !workflow.is_empty() {
            self.opt(&id, "@agent_workflow", workflow);
        }
        if !summary.is_empty() {
            self.opt(&id, "@agent_summary", summary);
        }
        id
    }
}

impl Drop for Server {
    /// kill-server, then the socket file: tmux leaves it behind, and stray
    /// `-L` sockets are exactly what this suite must never leave.
    fn drop(&mut self) {
        let _ = self.cmd().arg("kill-server").output();
        let deadline = Instant::now() + Duration::from_secs(3);
        while self.cmd().arg("list-sessions").output().is_ok_and(|o| o.status.success()) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
        }
        let _ = std::fs::remove_file(self.socket_file());
        if let Some(d) = &self.tmpdir {
            let _ = std::fs::remove_dir_all(d);
        }
    }
}

/// The reviewer's probe: free text holding a newline or a US, in a summary,
/// a cwd and a @stash_label. → (window ids, the newline dir to remove).
fn free_text_fixture(s: &Server) -> (Vec<String>, PathBuf) {
    let mut ids = vec![
        s.agent("free", 1, "needs-input", Some("50 needs-input"), "", "multi\nline summary"),
        s.agent("free", 2, "failed", Some("60 failed"), "", "us\x1fsplit summary"),
    ];
    let dir = std::env::temp_dir().join(format!("{}-nl", s.name));
    let nl = dir.join("new\nline dir");
    std::fs::create_dir_all(&nl).unwrap();
    let id = s.tmux(&["new-window", "-d", "-P", "-F", "#{window_id}", "-t", "=free:3", "-c", nl.to_str().unwrap(), "-n", "claude", LIFE]);
    s.opt(&id, "@agent_state", "done");
    s.opt(&id, "@agent_since", "70 done");
    ids.push(id);
    let id = s.agent("free", 4, "idle", None, "", "");
    s.opt(&id, "@stash_label", "lab\nel");
    ids.push(id);
    let id = s.agent("stash", 60, "idle", None, "", "");
    s.opt(&id, "@stash_label", "parked\nlabel");
    ids.push(id);
    (ids, dir)
}

fn run(mut c: Command) -> String {
    let o = c.output().expect("spawn");
    assert!(o.status.success(), "{c:?}: {}", String::from_utf8_lossy(&o.stderr));
    String::from_utf8(o.stdout).expect("utf-8 output")
}

/// The NEEDS_FIXTURE of tests/test_agent_roster.py, on a real server.
fn needs_fixture(s: &Server) {
    s.agent("main", 1, "done", Some("300 done"), "", "proj/One");
    s.agent("main", 2, "needs-input", Some("200 needs-input"), "", "");
    s.agent("work", 1, "failed", Some("400 failed"), "", "a\ttabbed summary");
    s.agent("work", 2, "done", Some("50 done"), "1", ""); // fleet out: not queued
    s.agent("work", 3, "running", Some("10 running"), "", ""); // not queued
    s.window("work", 4, "zsh", LIFE); // plain window
    s.agent("main", 3, "needs-input", None, "", ""); // no stamp
    s.agent("main", 4, "needs-input", Some(""), "", "");
    s.agent("main", 5, "needs-input", Some("abc"), "", "");
    s.agent("main", 6, "needs-input", Some("12a needs-input"), "", "");
    s.agent("main", 7, "needs-input", Some("  150 needs-input"), "", "");
    s.agent("main", 8, "needs-input", Some("0150\tneeds-input"), "", "");
    s.agent("main", 9, "failed", Some("99999999999 failed"), "", "");
    s.agent("main", 10, "done", Some("300 done"), "0", "");
    for sess in ["agents", "tasks", "stash", "scratch", "btop-popup", "tasks stash", "stash2"] {
        s.agent(sess, 1, "failed", Some("1 failed"), "", "");
    }
    for (sess, idx) in [("B", 1), ("a", 1), ("_x", 1), ("Ä", 1), ("aa", 1), ("a-b", 1), ("Z", 1), ("10", 1), ("9", 1), ("a", 10), ("a", 2)] {
        s.agent(sess, idx, "done", Some("500 done"), "", "");
    }
    for sess in ["dev 🚀", "dev 🔥", "dev 🔥🔥"] {
        s.agent(sess, 1, "done", Some("700 done"), "", "");
    }
    s.agent("Ω", 9, "needs-input", None, "", "");
    s.agent("Ω", 7, "needs-input", None, "", "");
    s.agent("日本", 4, "needs-input", None, "", "");
    s.agent("ω", 1, "done", Some("800 done"), "", "");
    s.agent("Ω", 1, "done", Some("800 done"), "", "");
    s.agent("Ж本", 1, "done", Some("900 done"), "", "");
    s.agent("ωΩ🚀Ж", 1, "done", Some("900 done"), "", "");
    // Linked windows: queued once, from the first sorted row; excluded links never win.
    let w50 = s.agent("zz", 1, "failed", Some("5 failed"), "", "");
    s.tmux(&["link-window", "-d", "-s", &w50, "-t", "=main:50"]);
    s.tmux(&["link-window", "-d", "-s", &w50, "-t", "=stash:50"]);
    let w51 = s.agent("stash", 2, "failed", Some("6 failed"), "", "");
    s.tmux(&["link-window", "-d", "-s", &w51, "-t", "=yy:51"]);
}

#[test]
fn needs_matches_agent_jump_list() {
    let s = Server::start("needs");
    // yy must exist before link-window targets it.
    s.window("yy", 0, "plain", LIFE);
    needs_fixture(&s);
    let (_, nl_dir) = free_text_fixture(&s);
    let sock = s.socket_path();
    for loc in ["en_US.UTF-8", "C"] {
        let mut ours = Command::new(BIN);
        ours.args(["--socket", &s.name, "needs"]).env("LC_ALL", loc).env_remove("TMUX");
        let ours = run(ours);
        let mut theirs = Command::new("bash");
        theirs.arg(jump_script()).arg("list").env("LC_ALL", loc).env("TMUX", format!("{sock},0,0")).env_remove("TMUX_PANE");
        let theirs = run(theirs);
        assert!(theirs.lines().count() > 20, "fixture too small under {loc}:\n{theirs}");
        assert_eq!(ours, theirs, "agent-ui needs != agent-jump.sh list under {loc}");
        // And through the env var instead of --socket.
        let mut env = Command::new(BIN);
        env.arg("needs").env("LC_ALL", loc).env("AGENT_UI_TMUX_SOCKET", &s.name).env_remove("TMUX");
        assert_eq!(run(env), theirs);
        // The free-text windows are in the queue, labels cut where awk cuts them.
        assert!(theirs.contains("\tmulti\n") && theirs.contains("\tus\n"), "{theirs}");
    }
    let _ = std::fs::remove_dir_all(nl_dir);
}

/// Outside tmux under a non-UTF-8 locale, agent-jump.sh's plain `tmux`
/// prints US as `_` and lists nothing; utf8_ctype_env (what Tmux::script_env
/// applies) fixes that without changing its order.
#[test]
fn agent_jump_outside_tmux_under_a_c_locale() {
    let s = Server::start_default("ctype");
    s.window("yy", 0, "plain", LIFE);
    needs_fixture(&s);
    let ours = {
        let mut c = Command::new(BIN);
        c.arg("needs").env("LC_ALL", "C");
        s.env_for_plain_tmux(&mut c);
        run(c)
    };
    let jump = |fix: bool| {
        let mut c = Command::new("bash");
        c.arg(jump_script()).arg("list").env("LC_ALL", "C");
        s.env_for_plain_tmux(&mut c);
        if fix {
            let parent = |k: &str| if k == "LC_ALL" { Some("C".to_string()) } else { None };
            for (k, v) in agent_ui::tmux::utf8_ctype_env(parent) {
                match v {
                    Some(v) => c.env(k, v),
                    None => c.env_remove(k),
                };
            }
        }
        run(c)
    };
    assert!(ours.lines().count() > 20, "{ours}");
    assert_ne!(jump(false), ours, "the bug did not reproduce: is $TMUX leaking in?");
    assert_eq!(jump(true), ours);
}

#[test]
fn dump_json_over_a_real_server() {
    let s = Server::start("dump");
    s.window("yy", 0, "plain", LIFE);
    needs_fixture(&s);
    let (free_ids, nl_dir) = free_text_fixture(&s);
    let mut c = Command::new(BIN);
    c.args(["--socket", &s.name, "dump", "--json", "--client", "/dev/ttys999"]).env_remove("TMUX");
    let v = json::parse(&run(c)).expect("dump is JSON");
    assert_eq!(v.get("client_gone").and_then(Value::as_bool), Some(true));
    let names: Vec<&str> = v.get("spaces").unwrap().as_array().unwrap().iter().map(|x| x.get("name").unwrap().as_str().unwrap()).collect();
    for hidden in ["agents", "tasks", "stash", "scratch", "btop-popup"] {
        assert!(!names.contains(&hidden), "{hidden} shown: {names:?}");
    }
    assert!(names.contains(&"tasks stash") && names.contains(&"stash2") && names.contains(&"boot"));
    let mut sorted = names.clone();
    sorted.sort();
    assert_eq!(names, sorted, "spaces by name");
    let main = v.get("spaces").unwrap().as_array().unwrap().iter().find(|x| x.get("name").unwrap().as_str() == Some("main")).unwrap();
    let idx: Vec<f64> = main.get("agents").unwrap().as_array().unwrap().iter().map(|a| a.get("index").unwrap().as_f64().unwrap()).collect();
    let mut sorted_idx = idx.clone();
    sorted_idx.sort_by(f64::total_cmp);
    assert_eq!(idx, sorted_idx, "agents by window index");
    assert!(idx.contains(&50.0)); // the linked window shows in main too
    // stash:1 and stash:2 (agents), stash:50 (the link of zz:1), by index.
    let parked: Vec<f64> = v.get("parked").unwrap().as_array().unwrap().iter().map(|p| p.get("index").unwrap().as_f64().unwrap()).collect();
    assert_eq!(parked, [1.0, 2.0, 50.0, 60.0]);
    // Every free-text window is shown (none dropped by a split row), with
    // its display text scrubbed and its raw cwd kept.
    let mut shown: Vec<&Value> = v.get("parked").unwrap().as_array().unwrap().iter().collect();
    for sp in v.get("spaces").unwrap().as_array().unwrap() {
        shown.extend(sp.get("agents").unwrap().as_array().unwrap());
    }
    for id in &free_ids {
        let a = shown.iter().find(|a| a.get("window_id").unwrap().as_str() == Some(id)).unwrap_or_else(|| panic!("{id} missing"));
        let title = a.get("title").unwrap().as_str().unwrap();
        assert!(!title.contains(['\n', '\x1f']), "{title:?}");
    }
    let titles: Vec<&str> = shown.iter().map(|a| a.get("title").unwrap().as_str().unwrap()).collect();
    assert!(titles.contains(&"multi line summary") && titles.contains(&"us split summary") && titles.contains(&"parked label"), "{titles:?}");
    let free = v.get("spaces").unwrap().as_array().unwrap().iter().find(|x| x.get("name").unwrap().as_str() == Some("free")).unwrap();
    let paths: Vec<&str> = free.get("agents").unwrap().as_array().unwrap().iter().map(|a| a.get("path").unwrap().as_str().unwrap()).collect();
    assert!(paths.iter().any(|p| p.ends_with("new\nline dir")), "{paths:?}");
    let _ = std::fs::remove_dir_all(nl_dir);
    // The linked window queues once, from main (its first sorted row), never stash.
    let needs = v.get("needs").unwrap().as_array().unwrap();
    let zz: Vec<&str> = needs.iter().filter(|n| n.get("index").unwrap().as_f64() == Some(50.0) || n.get("session").unwrap().as_str() == Some("zz")).map(|n| n.get("session").unwrap().as_str().unwrap()).collect();
    assert_eq!(zz, ["main"]);
}

#[test]
fn a_panic_still_kills_the_server() {
    let name = std::sync::Mutex::new(String::new());
    let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let s = Server::start("panic");
        *name.lock().unwrap() = s.name.clone();
        assert!(s.try_tmux(&["list-sessions"]).status.success());
        panic!("deliberate panic with a live server");
    }));
    assert!(r.is_err());
    let name = name.into_inner().unwrap();
    let alive = Command::new("tmux").args(["-L", &name, "list-sessions"]).env_remove("TMUX").output().unwrap();
    assert!(!alive.status.success(), "server {name} survived the panic");
    assert!(!Socket::Name(name.clone()).path().exists(), "socket of {name} left behind");
}

/// A scripts dir whose agent-jump.sh / stash.sh record which server their
/// plain `tmux` reaches.
fn fake_scripts(dir: &Path, out: &Path) {
    std::fs::create_dir_all(dir).unwrap();
    let body = format!(
        "#!/bin/bash\nprintf '%s %s\\n' \"$(basename \"$0\")\" \"$*\" >> '{o}'\ntmux display-message -p '#{{socket_path}}' >> '{o}' 2>&1\n",
        o = out.display()
    );
    for n in ["agent-jump.sh", "stash.sh", "closed-tabs.sh"] {
        let p = dir.join(n);
        std::fs::write(&p, &body).unwrap();
        let _ = Command::new("chmod").arg("+x").arg(&p).status();
    }
}

fn wait_for(path: &Path, needle: &str) -> String {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let t = std::fs::read_to_string(path).unwrap_or_default();
        if t.contains(needle) || Instant::now() > deadline {
            return t;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[test]
fn actions_reach_the_test_server_only() {
    let s = Server::start("actions");
    let sock = s.socket_path();
    let a1 = s.agent("main", 1, "needs-input", Some("100 needs-input"), "", "proj/Asks");
    let peek = s.window("main", 2, "peek", "printf 'hello\\n\\033[31mworld\\033[0m\\n'; sleep 600");
    let split = s.window("main", 3, "split", LIFE);
    s.tmux(&["split-window", "-d", "-t", &split, LIFE]);
    let parked = s.agent("stash", 1, "idle", None, "", "");

    let tmp = std::env::temp_dir().join(format!("agentui-actions-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&tmp);
    let out = tmp.join("calls.log");
    fake_scripts(&tmp.join("scripts"), &out);
    let tmux = Tmux::with_socket(Socket::parse(&s.name));
    let actions = Actions { tmux: tmux.clone(), scripts: tmp.join("scripts") };
    let snap = tmux.snapshot();
    assert!(snap.windows.len() >= 5);

    // goto: the script runs with TMUX pointing at the test server.
    actions.go(&snap, "/dev/ttys999", &a1, "main").unwrap();
    let log = wait_for(&out, &sock);
    assert!(log.contains(&format!("agent-jump.sh goto /dev/ttys999 {a1} main")), "{log}");
    assert!(log.contains(&sock), "agent-jump.sh reached another server: {log}");
    // A row from an old frame: moved / gone are refused before anything runs.
    assert!(actions.go(&snap, "/dev/ttys999", &a1, "work").unwrap_err().starts_with("that tab moved"));
    assert_eq!(actions.go(&snap, "/dev/ttys999", "@9999", "main").unwrap_err(), "that window is gone");
    // A parked row unstashes through run-shell, on the test server.
    actions.go(&snap, "/dev/ttys999", &parked, "stash").unwrap();
    let log = wait_for(&out, "stash.sh unstash");
    assert!(log.contains(&format!("stash.sh unstash {parked} /dev/ttys999")), "{log}");
    // park refuses a parked tab; close on a split without an agent refuses to guess.
    assert_eq!(actions.park(&snap, &parked, "stash").unwrap_err(), "already parked");
    assert_eq!(actions.close(&snap, &split, "main").unwrap_err(), PANE_UNSURE);
    // Peek: colours kept, trailing blanks dropped.
    let w = snap.windows.iter().find(|w| w.id == peek).unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    let mut lines = actions.peek(w, 15).expect("capture");
    while !lines.iter().any(|l| l.plain() == "world") && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(20));
        lines = actions.peek(w, 15).expect("capture");
    }
    let plain: Vec<String> = lines.iter().map(|l| l.plain()).collect();
    assert_eq!(plain.iter().rev().take(2).rev().cloned().collect::<Vec<_>>(), ["hello", "world"], "{plain:?}");
    assert!(lines.last().unwrap().spans.iter().any(|(st, _)| st.fg.is_some()));
    let _ = std::fs::remove_dir_all(&tmp);
}
