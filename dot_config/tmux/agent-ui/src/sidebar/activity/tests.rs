use super::*;

/// A transcript line: the person's prompt.
fn prompt(text: &str) -> String {
    format!(r#"{{"type":"user","isSidechain":false,"message":{{"role":"user","content":"{text}"}},"uuid":"u"}}"#)
}

/// A tool result (a user record that is NOT a prompt).
fn result() -> String {
    r#"{"type":"user","isSidechain":false,"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t","content":[{"type":"text","text":"ok"}]}]}}"#.to_string()
}

/// An assistant record calling `name` with `input` (JSON).
fn call(name: &str, input: &str) -> String {
    format!(r#"{{"type":"assistant","isSidechain":false,"message":{{"role":"assistant","content":[{{"type":"text","text":"on it"}},{{"type":"tool_use","id":"t","name":"{name}","input":{input}}}]}}}}"#)
}

/// Assistant text alone.
fn say(text: &str) -> String {
    format!(r#"{{"type":"assistant","isSidechain":false,"message":{{"role":"assistant","content":[{{"type":"text","text":"{text}"}}]}}}}"#)
}

fn noise() -> String {
    r#"{"type":"system","subtype":"turn_duration","durationMs":5}"#.to_string()
}

fn of(lines: &[String]) -> Option<String> {
    blurb(&(lines.join("\n") + "\n"), true)
}

#[test]
fn the_latest_tool_call_phrased() {
    // Bash: its own description, else the command's head.
    assert_eq!(of(&[prompt("go"), call("Bash", r#"{"command":"cargo test","description":"Run the layout tests."}"#), result()]).as_deref(),
        Some("Run the layout tests"));
    assert_eq!(of(&[prompt("go"), call("Bash", r#"{"command":"cd /x && FOO=1 cargo test --release 2>&1 | tail"}"#)]).as_deref(),
        Some("Running cargo test"));
    // The pty server's run: the command's first words past cd / env.
    let pty = r#"{"command":"cd /Users/me/agent-ui && cargo build --release 2>&1 | grep warning","pty":"ui"}"#;
    assert_eq!(of(&[prompt("go"), call("mcp__pty__run", pty), result(), noise()]).as_deref(), Some("Running cargo build"));
    assert_eq!(of(&[prompt("go"), call("mcp__pty__run", r#"{"command":"( unset TMUX; export X=1; /tmp/s/fake.sh up )"}"#)]).as_deref(),
        Some("Running fake.sh up"));
    assert_eq!(of(&[prompt("go"), call("mcp__pty__spawn_pty", r#"{"name":"dev","command":"npm run dev"}"#)]).as_deref(), Some("Starting dev"));
    // Files, searches, the web, other agents.
    let cases = [
        ("Read", r#"{"file_path":"/a/b/layout.rs"}"#, "Reading layout.rs"),
        ("Edit", r#"{"file_path":"/a/b/status.rs","old_string":"x","new_string":"y"}"#, "Editing status.rs"),
        ("MultiEdit", r#"{"file_path":"/a/notify.sh"}"#, "Editing notify.sh"),
        ("Write", r#"{"file_path":"/a/new.md","content":"x"}"#, "Writing new.md"),
        ("Grep", r#"{"pattern":"toast_due"}"#, "Searching for toast_due"),
        ("Glob", r#"{"pattern":"**/*.rs"}"#, "Finding **/*.rs"),
        ("Agent", r#"{"description":"Review the layout diff","prompt":"long"}"#, "Review the layout diff"),
        ("Task", r#"{"prompt":"long"}"#, "Delegating"),
        ("Workflow", r#"{"script":"x"}"#, "Running workflow"),
        ("WebSearch", r#"{"query":"nvim-notify layout"}"#, "Searching the web for nvim-notify layout"),
        ("WebFetch", r#"{"url":"https://textual.textualize.io/widgets/button/"}"#, "Reading textual.textualize.io"),
        ("mcp__tmux__message_agent", r#"{"pane":"cuanotch-hover","message":"hi"}"#, "Messaging @cuanotch-hover"),
        ("mcp__tmux__read_pane", r#"{"pane":"@sidebar"}"#, "Checking @sidebar"),
        ("Skill", r#"{"skill":"cua:drive-app"}"#, "Using cua:drive-app"),
        ("mcp__codegraph__codegraph_explore", r#"{"query":"x"}"#, "Codegraph: explore"),
        ("mcp__plugin_cloudflare_cloudflare__docs", r#"{}"#, "Cloudflare: docs"),
        ("mcp__claude_ai_Gmail__search_threads", r#"{}"#, "Gmail: search threads"),
        ("SomethingNew", r#"{}"#, "Something new"),
        ("ScheduleWakeup", r#"{}"#, "Schedule wakeup"),
        ("mcp__claude_ai_Google_Calendar__list_events", r#"{}"#, "Google Calendar: list events"),
        ("SendMessage", r#"{"to":"a1b2c3d4e5f6a7b8","message":"x"}"#, "Messaging a subagent"),
        ("SendMessage", r#"{"to":"reviewer","message":"x"}"#, "Messaging reviewer"),
        ("mcp__pty__spawn_pty", r#"{"command":"API_KEY=sk-123 server --token abc"}"#, "Starting server"),
    ];
    for (name, input, want) in cases {
        assert_eq!(of(&[prompt("go"), call(name, input)]).as_deref(), Some(want), "{name}");
    }
}

#[test]
fn an_in_progress_todo_wins_across_tool_calls() {
    let todos = r#"{"todos":[{"content":"Fix the toast","status":"completed","activeForm":"Fixing the toast"},{"content":"Run tests","status":"in_progress","activeForm":"Running the test suite."}]}"#;
    let lines = [prompt("go"), call("TodoWrite", todos), result(), call("Read", r#"{"file_path":"/x/a.rs"}"#), result(),
        call("Bash", r#"{"command":"ls"}"#), result()];
    assert_eq!(of(&lines).as_deref(), Some("Running the test suite"));
    // The latest list decides: nothing in progress any more → the tool.
    let done = r#"{"todos":[{"content":"Run tests","status":"completed","activeForm":"Running the tests"}]}"#;
    let mut later = lines.to_vec();
    later.extend([call("TodoWrite", done), result(), call("Grep", r#"{"pattern":"x"}"#)]);
    assert_eq!(of(&later).as_deref(), Some("Searching for x"));
    // A task tool's in-progress activeForm counts too.
    let task = r#"{"taskId":"3","status":"in_progress","activeForm":"Wiring the blurb"}"#;
    assert_eq!(of(&[prompt("go"), call("TaskUpdate", task), result(), call("Read", r#"{"file_path":"/a"}"#)]).as_deref(),
        Some("Wiring the blurb"));
}

#[test]
fn only_the_current_turn_counts() {
    // The last turn's tool never speaks for this one: no tool yet → Thinking.
    let lines = [prompt("first"), call("Read", r#"{"file_path":"/old.rs"}"#), result(), say("done"), noise(), prompt("second"), say("hmm")];
    assert_eq!(of(&lines).as_deref(), Some(THINKING));
    // A tool result is no prompt: the walk goes on past it.
    let lines = [prompt("go"), call("Edit", r#"{"file_path":"/a/x.rs"}"#), result(), result(), noise()];
    assert_eq!(of(&lines).as_deref(), Some("Editing x.rs"));
    // A meta record (a hook's injected text) is no prompt either.
    let meta = r#"{"type":"user","isMeta":true,"message":{"role":"user","content":"<system-reminder>x</system-reminder>"}}"#.to_string();
    assert_eq!(of(&[prompt("go"), call("Grep", r#"{"pattern":"p"}"#), meta]).as_deref(), Some("Searching for p"));
    // A subagent's records (sidechain) say nothing about the main thread.
    let side = r#"{"type":"assistant","isSidechain":true,"message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"rm -rf x"}}]}}"#.to_string();
    assert_eq!(of(&[prompt("go"), call("Glob", r#"{"pattern":"*.md"}"#), side]).as_deref(), Some("Finding *.md"));
}

#[test]
fn a_cut_tail_reads_what_it_has() {
    // The 64 KB window starts mid-line: that line is dropped, the rest read.
    let dir = std::env::temp_dir().join(format!("agentui-activity-cut-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("t.jsonl");
    let big = format!(r#"{{"type":"user","message":{{"content":"{}"}}}}"#, "x".repeat(TAIL as usize * 2));
    let body = [prompt("old"), big, call("Bash", r#"{"command":"make all"}"#), result()].join("\n") + "\n";
    std::fs::write(&path, &body).unwrap();
    let (tail, whole) = read_tail(&path, TAIL).unwrap();
    assert!(!whole && tail.starts_with(r#"{"type":"assistant""#), "{:?}", &tail[..40]);
    let mut a = Activity::new(dir.clone());
    assert_eq!(a.blurb_of(&path).as_deref(), Some("Running make all"));
    // Nothing usable in the window and no prompt in sight: nothing said
    // (the caller keeps the run detail), never a made-up `Thinking`.
    assert_eq!(blurb(&noise(), false), None);
    assert_eq!(blurb(&(say("hi") + "\n"), false), None);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn tidy_makes_one_capitalized_line() {
    assert_eq!(tidy("running\tthe\ntests.  "), "Running the tests");
    assert_eq!(tidy("über…"), "Über…");
    assert_eq!(tidy(""), "");
    assert_eq!(command_head("FOO=1 BAR=2 env python3 -m unittest"), "python3");
    assert_eq!(command_head("cd x; git status --short"), "git status");
    assert_eq!(command_head("/usr/local/bin/tmux -L x ls"), "tmux");
    assert_eq!(command_head("  "), "");
    assert_eq!(project_slug("/Users/me/.config/tmux"), "-Users-me--config-tmux");
}

/// A fake `~/.claude`: a sessions file for window `@7` (session `work`),
/// its transcript where Claude keeps it.
fn fake_home(tag: &str) -> (PathBuf, PathBuf) {
    let root = std::env::temp_dir().join(format!("agentui-activity-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(root.join("sessions")).unwrap();
    let cwd = "/tmp/proj.x";
    let sid = "11111111-2222-3333-4444-555555555555";
    let pid = std::process::id();
    std::fs::write(root.join(format!("sessions/{pid}.json")),
        format!(r#"{{"pid":{pid},"sessionId":"{sid}","cwd":"{cwd}","tmux":"work:@7.%9","updatedAt":5,"status":"busy"}}"#)).unwrap();
    // A dead pid's stale file for the same window: never chosen over the live one.
    std::fs::write(root.join("sessions/999999.json"),
        r#"{"pid":999999,"sessionId":"dead","cwd":"/tmp","tmux":"work:@7.%1","updatedAt":9}"#).unwrap();
    let dir = root.join("projects").join(project_slug(cwd));
    std::fs::create_dir_all(&dir).unwrap();
    let t = dir.join(format!("{sid}.jsonl"));
    std::fs::write(&t, [prompt("go"), call("Read", r#"{"file_path":"/x/a.rs"}"#)].join("\n") + "\n").unwrap();
    (root, t)
}

#[test]
fn the_transcript_is_found_and_read_only_when_it_changes() {
    let (root, t) = fake_home("cache");
    let mut a = Activity::new(root.clone());
    a.scan_sessions();
    assert_eq!(a.blurb_for("work", "@7").as_deref(), Some("Reading a.rs"));
    assert_eq!(a.blurb_for("work", "@8"), None);
    // Unchanged: no read at all, however many refreshes.
    for _ in 0..5 {
        a.scan_sessions();
        assert_eq!(a.blurb_for("work", "@7").as_deref(), Some("Reading a.rs"));
    }
    assert_eq!(a.reads, 1);
    // It grows: read again, the newest call phrased.
    let mut f = std::fs::OpenOptions::new().append(true).open(&t).unwrap();
    std::io::Write::write_all(&mut f, (result() + "\n" + &call("Edit", r#"{"file_path":"/x/b.rs"}"#) + "\n").as_bytes()).unwrap();
    drop(f);
    assert_eq!(a.blurb_for("work", "@7").as_deref(), Some("Editing b.rs"));
    assert_eq!(a.reads, 2);
    // Not in the cwd's project directory: found by the scan, then cached.
    let moved = root.join("projects/elsewhere");
    std::fs::create_dir_all(&moved).unwrap();
    let to = moved.join(t.file_name().unwrap());
    std::fs::rename(&t, &to).unwrap();
    assert_eq!(a.blurb_for("work", "@7").as_deref(), Some("Editing b.rs"));
    let _ = std::fs::remove_dir_all(&root);
}

/// Through the view: working agents get a blurb, the rest none; codex
/// agents none (no transcript reader for them yet).
#[test]
fn apply_fills_only_working_agents() {
    use crate::collate::Collator;
    use crate::tmux::tests::row;
    use crate::tmux::Snapshot;
    use crate::view::BuildArgs;
    let (root, _t) = fake_home("apply");
    let rows = [
        row("work", 1, "@7", &[("state", "running"), ("summary", "Busy"), ("kind", "claude")]),
        row("work", 2, "@8", &[("state", "idle"), ("summary", "Calm"), ("kind", "claude")]),
    ];
    let c = Collator::new("C");
    let args = BuildArgs { client: None, now: 0.0, collator: &c, git: None, log: vec![], watcher_age: Some(1.0) };
    let mut v = ViewModel::build(&Snapshot::parse(&rows.join("\n")), args);
    let mut a = Activity::new(root.clone());
    a.apply(&mut v);
    let got: Vec<_> = v.agents().map(|x| (x.window_id.clone(), x.blurb.clone())).collect();
    assert_eq!(got, [("@7".to_string(), Some("Reading a.rs".to_string())), ("@8".to_string(), None)]);
    let _ = std::fs::remove_dir_all(&root);
}

/// A file whose name is a hash is named by where it lives instead.
#[test]
fn hashed_file_names_say_what_they_are() {
    let h = "/Users/me/.claude/projects/-Users-me--config-tmux/18fdc907-92a3-4339-8a1b-0123456789ab.jsonl";
    assert_eq!(file_label(h), "a transcript");
    assert_eq!(file_label("/tmp/claude-501/x/tool-results/0f3a9c2be41d7a55c0e1.txt"), "tool output");
    assert_eq!(file_label("/private/tmp/claude-501/s/scratchpad/63a5317f-6511-47e6-9c11-aa00bb11cc22.out"), "tool output");
    assert_eq!(file_label("/private/tmp/claude-501/s/tasks/63a5317f-6511-47e6-9c11-aa00bb11cc22.output"), "task output");
    assert_eq!(file_label("/Users/me/Desktop/screenshots/a1b2c3d4e5f6a7b8c9d0.png"), "screenshots/…");
    assert_eq!(file_label("/63a5317f-6511-47e6-9c11-aa00bb11cc22"), "a file");
    // Plain names (short hex too) stay as they are.
    for p in ["/x/layout.rs", "/x/deadbeef.txt", "/x/2026-10-16.md", "/x/README"] {
        assert_eq!(file_label(p), base(p), "{p}");
    }
    // Through the phrasing.
    let read = format!(r#"{{"file_path":"{h}"}}"#);
    assert_eq!(of(&[prompt("go"), call("Read", &read)]).as_deref(), Some("Reading a transcript"));
    assert_eq!(of(&[prompt("go"), call("Write", r#"{"file_path":"/x/tasks/0123456789abcdef0123.output","content":"x"}"#)]).as_deref(),
        Some("Writing task output"));
    assert_eq!(of(&[prompt("go"), call("Edit", r#"{"file_path":"/x/layout.rs"}"#)]).as_deref(), Some("Editing layout.rs"));
}

/// A task's in-progress `activeForm` stops counting once a later update
/// closes THAT task; another task's update leaves it be.
#[test]
fn a_closed_task_no_longer_speaks() {
    let start = r#"{"taskId":"1","status":"in_progress","activeForm":"Fixing A"}"#;
    let lines = [prompt("go"), call("TaskUpdate", start), result(), call("Edit", r#"{"file_path":"/x/a.rs"}"#), result(),
        call("TaskUpdate", r#"{"taskId":"1","status":"completed"}"#), result(), call("Bash", r#"{"command":"cargo test"}"#)];
    assert_eq!(of(&lines).as_deref(), Some("Running cargo test"));
    let other = [prompt("go"), call("TaskUpdate", start), result(), call("TaskUpdate", r#"{"taskId":"2","status":"completed"}"#),
        result(), call("Bash", r#"{"command":"cargo test"}"#)];
    assert_eq!(of(&other).as_deref(), Some("Fixing A"));
}

/// Never a word that may be a secret; shell control words say what they are.
#[test]
fn command_heads_hide_secrets() {
    assert_eq!(command_head("export TOKEN=x; mytool deploy ghp_AbCdEf0123456789XYZabc"), "mytool deploy");
    assert_eq!(command_head("ghp_AbCdEf0123456789XYZabc --x"), "a command");
    assert_eq!(command_head("psql password=hunter2"), "psql");
    assert_eq!(command_head("curl sk-live-abc https://x"), "curl");
    assert_eq!(command_head("deploy cua-notch-hover-popup-animation"), "deploy cua-notch-hover-popup-animation");
    assert_eq!(command_head("for f in *.rs; do wc -l $f; done"), "a loop");
    assert_eq!(command_head("while true; do sleep 1; done"), "a loop");
    assert_eq!(command_head("if [ -f x ]; then y; fi"), "a script");
}

/// A crashed process's stale sessions file never speaks for its window.
#[test]
fn only_a_live_session_speaks() {
    let (root, _t) = fake_home("dead");
    let mut a = Activity::new(root.clone());
    a.scan_sessions();
    assert_eq!(a.blurb_for("work", "@7").as_deref(), Some("Reading a.rs"));
    // The live one gone: the dead pid's file (newer `updatedAt`) is not used.
    std::fs::remove_file(root.join(format!("sessions/{}.json", std::process::id()))).unwrap();
    a.scan_sessions();
    assert_eq!(a.blurb_for("work", "@7"), None);
    let _ = std::fs::remove_dir_all(&root);
}
