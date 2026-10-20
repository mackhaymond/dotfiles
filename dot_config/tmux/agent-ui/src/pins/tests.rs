use super::*;
use crate::tmux::tests::row;
use crate::tmux::{CLIENT_TAG, STATUS_FORMATS};

/// A client row whose `#{start_time}` is `start` (the server's identity).
fn client(start: &str) -> String {
    let mut f: Vec<String> = ["", CLIENT_TAG, "/dev/ttys999", "@1", "main", "on", "", "", ""].map(String::from).to_vec();
    f.extend((0..STATUS_FORMATS.len()).map(|i| if i == 32 { start.to_string() } else { String::new() }));
    f.join("\x1f")
}

fn snap(rows: &[String], start: &str) -> Snapshot {
    let mut all = rows.to_vec();
    all.push(client(start));
    Snapshot::parse(&all.join("\n"))
}

fn map(pairs: &[(&str, &str)]) -> HashMap<String, String> {
    pairs.iter().map(|(w, s)| (w.to_string(), s.to_string())).collect()
}

/// Sessions as they run now (no resumes).
fn sids(pairs: &[(&str, &str)]) -> Sessions {
    Sessions { now: map(pairs), resumed: HashMap::new() }
}

/// A fake process table: dead pids, each pid's argv, its children.
#[derive(Default)]
struct Fake {
    dead: Vec<i32>,
    argv: HashMap<i32, Vec<String>>,
    kids: HashMap<i32, Vec<i32>>,
}

impl Fake {
    fn with_argv(mut self, pid: i32, argv: &[&str]) -> Fake {
        self.argv.insert(pid, argv.iter().map(|s| s.to_string()).collect());
        self
    }
}

impl Procs for Fake {
    fn alive(&self, pid: i32) -> bool {
        !self.dead.contains(&pid)
    }
    fn argv(&self, pid: i32) -> Option<Vec<String>> {
        self.argv.get(&pid).cloned()
    }
    fn children(&self, pid: i32) -> Vec<i32> {
        self.kids.get(&pid).cloned().unwrap_or_default()
    }
}

fn entry(sid: &str, at: i64, server: &str, win: &str, seen: i64) -> Entry {
    Entry { sid: sid.into(), at, server: server.into(), win: win.into(), seen }
}

fn agent(id: &str, index: u32, pin: &str) -> String {
    row("main", index, id, &[("state", "idle"), ("pinned", pin)])
}

/// A fresh scratch directory under the system temp dir.
fn scratch(tag: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("agentui-pins-{}-{tag}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

#[test]
fn the_store_round_trips_and_shrugs_off_junk() {
    let s = Store { entries: vec![entry("a-1", 100, "1700", "@3", 150), entry("b-2", 0, "", "", 0)] };
    assert_eq!(Store::parse(&s.to_json()), s);
    assert_eq!(Store::parse("not json"), Store::default());
    assert_eq!(Store::parse("{\"pins\": [{\"at\": 1}, {\"sid\": \"x\"}]}").entries, vec![entry("x", 0, "", "", 0)]);
    let d = scratch("store");
    let p = d.join("deep/pins.json");
    s.save(&p).unwrap();
    assert_eq!(Store::load(&p), s);
    assert!(std::fs::read_dir(p.parent().unwrap()).unwrap().count() == 1, "no temp file left");
    assert_eq!(Store::load(&d.join("missing.json")), Store::default());
}

#[test]
fn the_tmux_field_names_a_window_and_a_pane() {
    assert_eq!(tmux_field("cs35l:@3.%4"), Some(("@3", "%4")));
    assert_eq!(tmux_field("a:b:@12.%40"), Some(("@12", "%40")));
    assert_eq!(tmux_field("main:3.4"), None);
    assert_eq!(tmux_field(""), None);
}

/// Claude: the newest LIVE session file naming a pane still in the window;
/// codex: `@agent_session_id`.
#[test]
fn session_ids_from_claude_files_and_the_codex_option() {
    let d = scratch("sids");
    let file = |name: &str, pid: i32, sid: &str, tmux: &str, started: i64| {
        let v = obj![("pid", pid), ("sessionId", sid), ("tmux", tmux), ("startedAt", started)];
        std::fs::write(d.join(name), v.to_json()).unwrap();
    };
    file("10.json", 10, "old", "main:@1.%1", 100);
    file("11.json", 11, "new", "main:@1.%1", 200); // newer: wins
    file("12.json", 12, "dead", "main:@2.%2", 300); // pid gone
    file("13.json", 13, "moved", "main:@3.%9", 300); // that pane is not in @3
    file("14.json", 14, "gone", "main:@77.%7", 300); // no such window
    std::fs::write(d.join("15.key"), "x").unwrap();
    std::fs::write(d.join("16.json"), "{broken").unwrap();
    let s = snap(&[
        row("main", 1, "@1", &[("state", "idle"), ("pane_flags", "A0%1 ")]),
        row("main", 2, "@2", &[("state", "idle"), ("pane_flags", "A0%2 ")]),
        row("main", 3, "@3", &[("state", "idle"), ("pane_flags", "A0%3 ")]),
        row("main", 4, "@4", &[("state", "idle"), ("kind", "codex"), ("session_id", "cdx-1")]),
    ], "1700");
    let procs = Fake { dead: vec![12], ..Fake::default() };
    assert_eq!(session_ids(&s, &d, &procs), map(&[("@1", "new"), ("@4", "cdx-1")]));
    assert!(session_ids(&s, &d.join("none"), &procs).len() == 1);
}

/// A resumed claude: its argv names the id it was started to resume
/// (`--resume X`, `-r X`, `--resume=X`); the id it runs NOW is its session
/// file's. `-r` alone (the picker) and `--continue` name none.
#[test]
fn a_resumed_claude_names_its_old_session() {
    let a = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();
    assert_eq!(resumed_ids(&a(&["claude", "--resume", "old-1"]), &CLAUDE_RESUME), ["old-1"]);
    assert_eq!(resumed_ids(&a(&["node", "/x/cli.js", "-r", "old-2", "--model", "x"]), &CLAUDE_RESUME), ["old-2"]);
    assert_eq!(resumed_ids(&a(&["claude", "--resume=old-3"]), &CLAUDE_RESUME), ["old-3"]);
    assert!(resumed_ids(&a(&["claude", "-r", "--model", "x"]), &CLAUDE_RESUME).is_empty());
    assert!(resumed_ids(&a(&["claude", "--continue"]), &CLAUDE_RESUME).is_empty());
    assert!(resumed_ids(&a(&["claude", "-r"]), &CLAUDE_RESUME).is_empty());
    assert_eq!(resumed_ids(&a(&["opencode", "-s", "ses_9"]), &OPENCODE_SESSION), ["ses_9"]);
    assert_eq!(resumed_ids(&a(&["opencode", "--session=ses_8"]), &OPENCODE_SESSION), ["ses_8"]);
    // Through sessions(): @1's claude (pid 21) resumed old-1 and now runs new-1.
    let d = scratch("resumed");
    let v = obj![("pid", 21), ("sessionId", "new-1"), ("tmux", "main:@1.%1"), ("startedAt", 5)];
    std::fs::write(d.join("21.json"), v.to_json()).unwrap();
    let s = snap(&[row("main", 1, "@1", &[("state", "idle"), ("pane_flags", "A0%1:900 ")])], "A");
    let procs = Fake::default().with_argv(21, &["claude", "--resume", "old-1", "--dangerously-skip-permissions"]);
    let got = sessions(&s, &d, &procs);
    assert_eq!(got.now, map(&[("@1", "new-1")]));
    assert_eq!(got.resumed, HashMap::from([("@1".to_string(), vec!["old-1".to_string()])]));
    // Resumed under the SAME id: nothing extra.
    let procs = Fake::default().with_argv(21, &["claude", "--resume", "new-1"]);
    assert!(sessions(&s, &d, &procs).resumed.is_empty());
}

/// Opencode: the `-s <id>` of the opencode process under one of its panes
/// (the pane's shell's child); none without one.
#[test]
fn an_opencode_window_is_known_by_its_session_flag() {
    let s = snap(&[
        row("main", 1, "@1", &[("state", "idle"), ("kind", "opencode"), ("pane_flags", "A0%1:100 ")]),
        row("main", 2, "@2", &[("state", "idle"), ("kind", "opencode"), ("pane_flags", "A0%2:200 ")]),
        row("main", 3, "@3", &[("state", "idle"), ("pane_flags", "A0%3:300 ")]), // not opencode
    ], "A");
    let mut procs = Fake::default()
        .with_argv(100, &["-zsh"])
        .with_argv(101, &["/opt/homebrew/bin/opencode", "-s", "ses_abc"])
        .with_argv(200, &["-zsh"])
        .with_argv(201, &["opencode"])
        .with_argv(300, &["-zsh"])
        .with_argv(301, &["opencode", "-s", "ses_zzz"]);
    procs.kids = HashMap::from([(100, vec![101]), (200, vec![201]), (300, vec![301])]);
    let got = sessions(&s, &scratch("oc"), &procs);
    assert_eq!(got.now, map(&[("@1", "ses_abc")]));
}

/// KERN_PROCARGS2's layout: argc, the exec path, NUL padding, the argv,
/// then the environment (never read).
#[test]
fn procargs_parse() {
    let mut b = 3i32.to_ne_bytes().to_vec();
    b.extend(b"/usr/local/bin/claude\0\0\0\0claude\0--resume\0abc\0HOME=/x\0");
    assert_eq!(parse_procargs(&b), Some(vec!["claude".into(), "--resume".into(), "abc".into()]));
    assert_eq!(parse_procargs(&b[..10]), None);
    assert_eq!(parse_procargs(&0i32.to_ne_bytes()), None);
    // This process, live (macOS): its own argv starts with the test binary.
    #[cfg(target_os = "macos")]
    {
        let me = LiveProcs.argv(std::process::id() as i32).expect("our own argv");
        assert!(me[0].contains("agent_ui"), "{me:?}");
        assert!(LiveProcs.alive(std::process::id() as i32));
    }
}

/// After a restore or ⌘Z the resumed claude reports a NEW id: the entry kept
/// under the old one finds the window through the resume, the pin goes back
/// and the entry is re-keyed to the new id. A window already pinned under
/// its new id absorbs the old entry.
#[test]
fn a_pin_survives_a_resume_that_changes_the_id() {
    let mut st = Store { entries: vec![entry("old-1", 100, "A", "@1", 50)] };
    let s = snap(&[agent("@3", 3, "")], "B");
    let ss = Sessions { now: map(&[("@3", "new-1")]), resumed: HashMap::from([("@3".to_string(), vec!["old-1".to_string()])]) };
    assert_eq!(reconcile(&mut st, &s, &ss, "B", 900), vec![Plan::Set("@3".into(), 100)]);
    assert_eq!(st.entries, vec![entry("new-1", 100, "B", "@3", 900)]);
    // Next sync, tmux shows the pin: one entry, under the new id.
    let s = snap(&[agent("@3", 3, "100")], "B");
    assert!(reconcile(&mut st, &s, &ss, "B", 950).is_empty());
    assert_eq!(st.entries, vec![entry("new-1", 100, "B", "@3", 950)]);
    // The pinned window absorbs a stale old-id entry.
    let mut st = Store { entries: vec![entry("old-1", 100, "A", "@1", 50), entry("new-1", 100, "B", "@3", 60)] };
    assert!(reconcile(&mut st, &s, &ss, "B", 990).is_empty());
    assert_eq!(st.entries, vec![entry("new-1", 100, "B", "@3", 990)]);
    // Same server, same window, resumed in place but unpinned: unpinned for good.
    let mut st = Store { entries: vec![entry("old-1", 100, "B", "@3", 50)] };
    let s = snap(&[agent("@3", 3, "")], "B");
    assert!(reconcile(&mut st, &s, &ss, "B", 990).is_empty());
    assert!(st.entries.is_empty());
}

/// The DEFAULT server is the one at `realpath(${TMUX_TMPDIR:-/tmp})/tmux-<uid>/default`,
/// however it is named: no socket and no $TMUX, `$TMUX` naming it (/tmp or
/// /private/tmp), `-L default`; never a private `-L` server, nor an ambient
/// $TMUX naming one (a test server running the real config).
#[test]
fn the_default_server_is_known_by_its_socket_path() {
    use crate::tmux::Socket;
    let d = |sock: Option<&Socket>, env: Option<&str>, tmpdir: Option<&str>| is_default_server(sock, env, tmpdir, 501);
    assert!(d(None, None, None));
    assert!(d(None, Some("/private/tmp/tmux-501/default,772,6"), None));
    assert!(d(None, Some("/tmp/tmux-501/default,1,0"), None));
    assert!(d(Some(&Socket::Name("default".into())), None, None));
    assert!(d(Some(&Socket::Path("/tmp/tmux-501/default".into())), None, None));
    assert!(!d(None, Some("/private/tmp/tmux-501/pn,1,0"), None));
    assert!(!d(None, Some("/private/tmp/claude-501/pn/tmux-501/default,1,0"), None)); // another TMPDIR's
    assert!(!d(Some(&Socket::Name("agentui-x".into())), None, None));
    assert!(!d(None, Some("/tmp/tmux-502/default,1,0"), None)); // another uid's
    // A TMUX_TMPDIR moves the default with it.
    let t = scratch("tmpdir");
    let td = t.to_string_lossy().to_string();
    assert!(d(None, Some(&format!("{td}/tmux-501/default,1,0")), Some(&td)));
    assert!(!d(None, Some("/private/tmp/tmux-501/default,1,0"), Some(&td)));
}

#[test]
fn commands_set_and_unset_in_one_call() {
    assert_eq!(command(&[]), None);
    let c = command(&[Plan::Set("@3".into(), 100), Plan::Unset("@4".into())]).unwrap();
    assert_eq!(c.join(" "), "set-option -w -t @3 @agent_pinned 100 ; set-option -wu -t @4 @agent_pinned");
}

/// A pinned window whose session is known is recorded with this server
/// and window; pinned again later, its time follows.
#[test]
fn a_pin_is_recorded() {
    let mut st = Store::default();
    let s = snap(&[agent("@1", 1, "100"), agent("@2", 2, "")], "A");
    let plans = reconcile(&mut st, &s, &sids(&[("@1", "s1"), ("@2", "s2")]), "A", 500);
    assert!(plans.is_empty());
    assert_eq!(st.entries, vec![entry("s1", 100, "A", "@1", 500)]);
    let s = snap(&[agent("@1", 1, "300")], "A");
    reconcile(&mut st, &s, &sids(&[("@1", "s1")]), "A", 600);
    assert_eq!(st.entries, vec![entry("s1", 300, "A", "@1", 600)]);
}

/// After a restore (a new server) the session runs in a new window: the pin
/// is put back with its old time. The same after ⌘Z reopened a closed tab
/// (same server, its old window gone).
#[test]
fn a_restore_or_a_reopen_puts_the_pin_back() {
    let mut st = Store { entries: vec![entry("s1", 100, "A", "@1", 50)] };
    let s = snap(&[agent("@1", 1, ""), agent("@3", 3, "")], "B");
    let plans = reconcile(&mut st, &s, &sids(&[("@3", "s1"), ("@1", "other")]), "B", 900);
    assert_eq!(plans, vec![Plan::Set("@3".into(), 100)]);
    assert_eq!(st.entries, vec![entry("s1", 100, "B", "@3", 900)]);
    // Reopened in the same server, a new window.
    let mut st = Store { entries: vec![entry("s1", 100, "A", "@1", 50)] };
    let s = snap(&[agent("@5", 5, "")], "A");
    assert_eq!(reconcile(&mut st, &s, &sids(&[("@5", "s1")]), "A", 900), vec![Plan::Set("@5".into(), 100)]);
}

/// Never re-pins a window it saw unpinned in the same server: the pin was
/// taken off (by a toggle whose file write was lost, or by hand).
#[test]
fn an_unpin_here_is_final() {
    let mut st = Store { entries: vec![entry("s1", 100, "A", "@1", 50)] };
    let s = snap(&[agent("@1", 1, "")], "A");
    assert!(reconcile(&mut st, &s, &sids(&[("@1", "s1")]), "A", 900).is_empty());
    assert!(st.entries.is_empty());
    // Its session unknown now (the agent exited), the window still here: gone too.
    let mut st = Store { entries: vec![entry("s1", 100, "A", "@1", 50)] };
    assert!(reconcile(&mut st, &s, &sids(&[]), "A", 900).is_empty());
    assert!(st.entries.is_empty());
}

/// A session that runs nowhere is kept (parked, closed, not restored yet)
/// for EXPIRE seconds after it was last seen.
#[test]
fn a_session_running_nowhere_is_kept_then_expires() {
    let mut st = Store { entries: vec![entry("s1", 100, "A", "@1", 1000)] };
    let s = snap(&[agent("@2", 2, "")], "B");
    assert!(reconcile(&mut st, &s, &sids(&[]), "B", 1000 + EXPIRE).is_empty());
    assert_eq!(st.entries.len(), 1);
    reconcile(&mut st, &s, &sids(&[]), "B", 1001 + EXPIRE);
    assert!(st.entries.is_empty());
    // Same server, its window gone (closed): kept.
    let mut st = Store { entries: vec![entry("s1", 100, "A", "@1", 1000)] };
    reconcile(&mut st, &s, &sids(&[]), "A", 2000);
    assert_eq!(st.entries.len(), 1);
}

/// A pinned window whose session changed (claude's /clear): the entry
/// follows the new id; one whose session is unknown (a parked, suspended
/// agent) keeps its entry, freshly seen.
#[test]
fn a_pinned_window_keeps_its_entry() {
    let mut st = Store { entries: vec![entry("s1", 100, "A", "@1", 50)] };
    let s = snap(&[agent("@1", 1, "100")], "A");
    reconcile(&mut st, &s, &sids(&[("@1", "s2")]), "A", 900);
    assert_eq!(st.entries, vec![entry("s2", 100, "A", "@1", 900)]);
    let mut st = Store { entries: vec![entry("s1", 100, "A", "@1", 50)] };
    reconcile(&mut st, &s, &sids(&[]), "A", 900);
    assert_eq!(st.entries, vec![entry("s1", 100, "A", "@1", 900)]);
}

/// The sidebar's sync: nothing at all to do (no pins, nothing stored) costs
/// no reconcile; a change of windows runs one, which puts a restored pin
/// back and saves the file; an unchanged snapshot waits EVERY seconds.
#[test]
fn the_sync_runs_when_the_windows_change() {
    let d = scratch("sync");
    let (path, dir) = (d.join("pins.json"), d.join("sessions"));
    std::fs::create_dir_all(&dir).unwrap();
    let mut sy = Sync::new(path.clone(), dir.clone());
    let alive = Fake::default();
    let s0 = snap(&[agent("@1", 1, "")], "B");
    assert_eq!(sy.tick(&s0, 1000.0, &alive), None);
    assert_eq!(sy.runs, 0);
    // A pin kept from an old server; its session now runs in @1.
    Store { entries: vec![entry("s1", 100, "A", "@7", 900)] }.save(&path).unwrap();
    let v = obj![("pid", 1), ("sessionId", "s1"), ("tmux", "main:@1.%1"), ("startedAt", 5)];
    std::fs::write(dir.join("1.json"), v.to_json()).unwrap();
    // (Same windows, inside EVERY: no run yet.)
    assert_eq!(sy.tick(&s0, 1001.0, &alive), None);
    let got = sy.tick(&s0, 1000.0 + Sync::EVERY, &alive).expect("the pin goes back");
    assert_eq!(got.join(" "), "set-option -w -t @1 @agent_pinned 100");
    assert_eq!(sy.runs, 1);
    assert_eq!(Store::load(&path).entries, vec![entry("s1", 100, "B", "@1", 1010)]);
    // tmux now shows it pinned: a new signature, a run, nothing to send.
    let s1 = snap(&[agent("@1", 1, "100")], "B");
    assert_eq!(sy.tick(&s1, 1011.0, &alive), None);
    assert_eq!(sy.runs, 2);
    // No server identity (no client row): never.
    let bare = Snapshot::parse(&agent("@1", 1, "100"));
    assert_eq!(sy.tick(&bare, 5000.0, &alive), None);
    assert_eq!(sy.runs, 2);
}

/// ⌥W 1-9: the pinned agents in pin order, numbered 1..N with no gap; a
/// shell's, a home's, a parked or hidden window's pin never counts; a
/// linked window once; past nine, no key.
#[test]
fn pin_keys_number_the_pins_in_order() {
    let s = snap(&[
        agent("@4", 4, "300"),
        agent("@2", 2, "100"),
        row("main", 3, "@3", &[("pinned", "50")]),                                  // a shell
        row("main", 5, "@5", &[("state", "idle"), ("pinned", "60"), ("home", "1")]), // a home
        row("stash", 1, "@6", &[("state", "idle"), ("pinned", "70")]),              // parked
        row("agents", 1, "@7", &[("state", "idle"), ("pinned", "80")]),             // hidden
        agent("@9", 9, "100"),                                                       // a tie: by id
        row("work", 1, "@4", &[("state", "idle"), ("pinned", "300")]),              // @4 linked
    ], "A");
    assert_eq!(pin_order(&s), ["@2", "@9", "@4"]);
    let k = pin_keys(&s);
    assert_eq!((k["@2"], k["@9"], k["@4"], k.len()), (1, 2, 3, 3));
    // Ten pins: the tenth has no key.
    let rows: Vec<String> = (1..=10).map(|i| agent(&format!("@{i}"), i, &(100 + i).to_string())).collect();
    let k = pin_keys(&snap(&rows, "A"));
    assert_eq!((k.len(), k["@1"], k["@9"], k.get("@10")), (9, 1, 9, None));
    assert_eq!((parse_key("3"), parse_key("0"), parse_key("10"), parse_key("x"), parse_key("")), (Some(3), None, None, None, None));
}

/// The writes: a key set where it differs, unset wherever no pin holds it
/// (unpinned, a tenth pin, a parked window); nothing when all agree.
#[test]
fn pin_key_writes_fill_and_clear() {
    let s = snap(&[
        row("main", 1, "@1", &[("state", "idle"), ("pinned", "10"), ("pin_key", "1")]),  // right already
        row("main", 2, "@2", &[("state", "idle"), ("pinned", "20"), ("pin_key", "3")]),  // a gap: 3 → 2
        row("main", 3, "@3", &[("state", "idle"), ("pin_key", "2")]),                    // unpinned
        row("stash", 1, "@4", &[("state", "idle"), ("pinned", "5"), ("pin_key", "4")]),  // parked
        row("main", 5, "@5", &[("state", "idle"), ("pin_key", "junk")]),
        row("main", 6, "@6", &[("state", "idle"), ("pinned", "30")]),                    // new: 3
    ], "A");
    let w = key_writes(&s, &pin_keys(&s));
    assert_eq!(w, vec![("@2".to_string(), Some(2)), ("@3".into(), None), ("@4".into(), None), ("@5".into(), None),
        ("@6".into(), Some(3))]);
    assert_eq!(key_command(&w[..2]).unwrap().join(" "), "set-option -q -w -t @2 @pin_key 2 ; set-option -q -wu -t @3 @pin_key");
    assert_eq!(key_command(&[]), None);
    // All agree: nothing.
    let s = snap(&[row("main", 1, "@1", &[("state", "idle"), ("pinned", "10"), ("pin_key", "1")])], "A");
    assert!(key_writes(&s, &pin_keys(&s)).is_empty());
}

/// In flight, a write is not sent again; seen, it settles; still unseen
/// after RESEND seconds, it goes once more.
#[test]
fn pin_key_writes_are_never_a_storm() {
    let s = snap(&[agent("@1", 1, "10")], "A");
    let mut m = KeyMemo::default();
    let w = || key_writes(&s, &pin_keys(&s));
    assert_eq!(m.fresh(&s, w(), 100.0), vec![("@1".to_string(), Some(1))]);
    assert!(m.fresh(&s, w(), 101.0).is_empty());
    assert!(m.fresh(&s, w(), 104.9).is_empty());
    assert_eq!(m.fresh(&s, w(), 105.0).len(), 1); // lost: once more
    // Seen: settled (and nothing left to write).
    let seen = snap(&[row("main", 1, "@1", &[("state", "idle"), ("pinned", "10"), ("pin_key", "1")])], "A");
    assert!(m.fresh(&seen, key_writes(&seen, &pin_keys(&seen)), 106.0).is_empty());
    assert!(m.sent.is_empty());
}

/// Off the default server nothing is kept unless `$AGENT_UI_PINS` says
/// where (tests and previews never touch the real file).
#[test]
fn only_the_default_server_keeps_pins_by_default() {
    let other = Tmux::with_socket(Some(crate::tmux::Socket::Name("agentui-pins-x".into())));
    if std::env::var("AGENT_UI_PINS").is_err() {
        assert_eq!(store_for(&other), None);
    }
    assert!(store_path().ends_with("agent-ui/pins.json") || std::env::var("AGENT_UI_PINS").is_ok());
}
