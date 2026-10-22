// Fixtures in the shapes real transcripts have (~/.claude/projects and
// ~/.codex/sessions, read once to learn them), the words all made up.

use super::*;
use crate::json::Value;
use crate::obj;
use crate::tmux::tests::row;
use std::process::Command;

fn scratch(tag: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("agent-ui-insight-{tag}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&d);
    fs::create_dir_all(&d).unwrap();
    fs::canonicalize(d).unwrap()
}

fn arr(v: Vec<Value>) -> Value {
    Value::Arr(v)
}

/// A Claude user record (its `type` before its `message`, as written).
fn user(text: &str) -> String {
    obj![("isSidechain", false), ("type", "user"), ("message", obj![("role", "user"), ("content", text)]), ("uuid", "u1")].to_json()
}

/// A user record with extra top-level fields.
fn user_with(extra: &[(&str, Value)], content: Value) -> String {
    let mut kv: Vec<(String, Value)> = vec![("type".into(), "user".into())];
    kv.extend(extra.iter().map(|(k, v)| (k.to_string(), v.clone())));
    kv.push(("message".into(), obj![("role", "user"), ("content", content)]));
    Value::Obj(kv).to_json()
}

/// A Claude assistant record (its `message` before its `type`, as written).
fn asst(blocks: Vec<Value>, usage: [u64; 3], model: &str, requested: Option<&str>) -> String {
    let u = obj![("input_tokens", usage[0]), ("cache_creation_input_tokens", usage[1]), ("cache_read_input_tokens", usage[2]),
        ("output_tokens", 7)];
    let msg = obj![("model", model), ("id", "msg_1"), ("type", "message"), ("role", "assistant"), ("content", arr(blocks)), ("usage", u)];
    let mut kv: Vec<(String, Value)> = vec![("isSidechain".into(), false.into()), ("message".into(), msg), ("type".into(), "assistant".into())];
    if let Some(r) = requested {
        kv.push(("requestedModel".into(), r.into()));
    }
    Value::Obj(kv).to_json()
}

fn tool(id: &str, name: &str, input: Value) -> Value {
    obj![("type", "tool_use"), ("id", id), ("name", name), ("input", input)]
}

fn edit(id: &str, path: &str) -> String {
    asst(vec![tool(id, "Edit", obj![("file_path", path), ("old_string", "a"), ("new_string", "b"), ("replace_all", false)])], [1, 0, 0],
        "claude-opus-5-5", None)
}

fn result(id: &str, err: bool) -> String {
    let block = obj![("tool_use_id", id), ("type", "tool_result"), ("content", "done"), ("is_error", err)];
    obj![("type", "user"), ("message", obj![("role", "user"), ("content", arr(vec![block]))])].to_json()
}

fn todo(items: &[(&str, &str)]) -> String {
    let todos = items.iter().map(|(c, st)| obj![("content", *c), ("status", *st), ("activeForm", format!("{c}ing"))]).collect();
    asst(vec![tool("t", "TodoWrite", obj![("todos", arr(todos))])], [1, 0, 0], "claude-opus-5-5", None)
}

fn lines(ls: &[String]) -> String {
    ls.iter().map(|l| format!("{l}\n")).collect()
}

fn paths(v: &[&str]) -> Vec<PathBuf> {
    v.iter().map(PathBuf::from).collect()
}

fn claude(ls: &[String]) -> Facts {
    let mut f = Facts::default();
    feed_claude(&mut f, &lines(ls), true);
    f
}

fn append(p: &Path, text: &str) {
    use std::io::Write;
    let mut f = fs::OpenOptions::new().append(true).create(true).open(p).unwrap();
    f.write_all(text.as_bytes()).unwrap();
}

// ---------------------------------------------------------------- reading

/// Growth reads only the new bytes; a half-written last line waits for its
/// end and is then read whole; an unchanged file reads nothing.
#[test]
fn incremental_append_with_a_partial_line() {
    let d = scratch("append");
    let p = d.join("s.jsonl");
    let (a, b) = (edit("e1", "/r/a.rs"), edit("e2", "/r/b.rs"));
    let (half1, half2) = b.split_at(b.len() / 2);
    fs::write(&p, format!("{}\n{a}\n{half1}", user("first ask"))).unwrap();
    let mut t = Transcript::new(Kind::Claude, true);
    t.sync(&p, FIRST_MAX).unwrap();
    assert_eq!((t.facts.touched(), t.facts.prompt.as_deref()), (paths(&["/r/a.rs"]), Some("first ask")));
    let first = t.read;
    // Unchanged: nothing read.
    t.sync(&p, FIRST_MAX).unwrap();
    assert_eq!(t.read, first);
    // The line's end arrives, then another prompt.
    append(&p, &format!("{half2}\n{}\n", user("second ask")));
    t.sync(&p, FIRST_MAX).unwrap();
    assert_eq!(t.facts.touched(), paths(&["/r/a.rs", "/r/b.rs"]));
    assert_eq!(t.facts.prompt.as_deref(), Some("second ask"));
    // Read again: the half line (it was never consumed) and the new bytes, no more.
    assert_eq!(t.read - first, (half1.len() + half2.len() + 1 + user("second ask").len() + 1) as u64);
    assert_eq!(t.resets, 0);
    let _ = fs::remove_dir_all(&d);
}

/// Read in tiny chunks (lines cut anywhere, at every step) and from a cut
/// start: the same facts as one read.
#[test]
fn chunk_boundaries_change_nothing() {
    let d = scratch("chunks");
    let p = d.join("s.jsonl");
    let ls: Vec<String> = (0..12).map(|i| if i % 3 == 0 { user(&format!("ask {i}")) } else { edit(&format!("e{i}"), &format!("/r/{i}.rs")) }).collect();
    fs::write(&p, lines(&ls)).unwrap();
    let mut whole = Transcript::new(Kind::Claude, true);
    whole.sync(&p, FIRST_MAX).unwrap();
    for chunk in [1, 7, 64, 333] {
        let mut t = Transcript::new(Kind::Claude, true);
        t.chunk = chunk;
        t.sync(&p, FIRST_MAX).unwrap();
        assert_eq!(t.facts, whole.facts, "chunk {chunk}");
    }
    assert_eq!(whole.facts.touched().len(), 8);
    assert_eq!(whole.facts.prompt.as_deref(), Some("ask 9"));
    // A cut start in tiny chunks: the tail's whole lines only.
    let max = (ls[11].len() + ls[10].len() + 2 + 5) as u64;
    let mut t = Transcript::new(Kind::Claude, true);
    t.chunk = 5;
    t.sync(&p, max).unwrap();
    assert_eq!(t.facts.touched(), paths(&["/r/10.rs", "/r/11.rs"]));
    let _ = fs::remove_dir_all(&d);
}

/// Shrunk, replaced (a new inode) or rewritten in place at the same size:
/// parsed again from scratch, nothing of the old file kept.
#[test]
fn shrink_or_replace_reparses() {
    let d = scratch("shrink");
    let p = d.join("s.jsonl");
    fs::write(&p, lines(&[edit("e1", "/r/old.rs"), edit("e2", "/r/old2.rs")])).unwrap();
    let mut t = Transcript::new(Kind::Claude, true);
    t.sync(&p, FIRST_MAX).unwrap();
    assert_eq!(t.facts.touched().len(), 2);
    fs::write(&p, lines(&[edit("e3", "/r/new.rs")])).unwrap();
    t.sync(&p, FIRST_MAX).unwrap();
    assert_eq!((t.facts.touched(), t.resets), (paths(&["/r/new.rs"]), 1));
    // Replaced by rename, longer: a new file all the same.
    let q = d.join("next.jsonl");
    fs::write(&q, lines(&[edit("e4", "/r/x.rs"), edit("e5", "/r/y.rs"), edit("e6", "/r/z.rs")])).unwrap();
    fs::rename(&q, &p).unwrap();
    t.sync(&p, FIRST_MAX).unwrap();
    assert_eq!((t.facts.touched(), t.resets), (paths(&["/r/x.rs", "/r/y.rs", "/r/z.rs"]), 2));
    // Same size, rewritten in place (mtime moved).
    let same = lines(&[edit("e4", "/r/q.rs"), edit("e5", "/r/w.rs"), edit("e6", "/r/e.rs")]);
    std::thread::sleep(Duration::from_millis(20));
    fs::write(&p, &same).unwrap();
    let past = SystemTime::now() + Duration::from_secs(5);
    fs::File::options().write(true).open(&p).unwrap().set_modified(past).unwrap();
    t.sync(&p, FIRST_MAX).unwrap();
    assert_eq!((t.facts.touched(), t.resets), (paths(&["/r/q.rs", "/r/w.rs", "/r/e.rs"]), 3));
    let _ = fs::remove_dir_all(&d);
}

/// First sight of a big file reads its tail only, the cut first line
/// dropped; a burst past the bound skips ahead and keeps what it had.
#[test]
fn first_sight_reads_the_tail_only() {
    let d = scratch("tail");
    let p = d.join("s.jsonl");
    let ls: Vec<String> = (0..20).map(|i| edit(&format!("e{i}"), &format!("/r/f{i:02}.rs"))).collect();
    fs::write(&p, lines(&ls)).unwrap();
    let max = (ls[0].len() as u64 + 1) * 3 + 10; // three whole lines and a cut one
    let mut t = Transcript::new(Kind::Claude, true);
    t.sync(&p, max).unwrap();
    assert_eq!(t.facts.touched(), paths(&["/r/f17.rs", "/r/f18.rs", "/r/f19.rs"]));
    assert!(t.read <= max);
    // A burst bigger than the bound: the tail of it, the old facts kept.
    let more: Vec<String> = (20..30).map(|i| edit(&format!("e{i}"), &format!("/r/f{i:02}.rs"))).collect();
    append(&p, &lines(&more));
    t.sync(&p, max).unwrap();
    assert_eq!(t.facts.touched(), paths(&["/r/f17.rs", "/r/f18.rs", "/r/f19.rs", "/r/f27.rs", "/r/f28.rs", "/r/f29.rs"]));
    assert_eq!(t.resets, 0);
    let _ = fs::remove_dir_all(&d);
}

// ---------------------------------------------------------------- claude

/// The latest todo list's in-progress item, else the last prompt; the task
/// tools the same way.
#[test]
fn todo_in_progress_else_the_last_prompt() {
    let f = claude(&[user("Make the sidebar faster"), todo(&[("Profile", "completed"), ("Cache the snapshot", "in_progress")])]);
    assert_eq!(f.doing().as_deref(), Some("Cache the snapshoting"));
    // A newer list with nothing in progress: the prompt speaks.
    let f = claude(&[user("Make it faster"), todo(&[("Profile", "in_progress")]), todo(&[("Profile", "completed")])]);
    assert_eq!(f.doing().as_deref(), Some("Make it faster"));
    // A newer real prompt is a new job: the item left in progress before it
    // is stale, the prompt speaks; a list after the prompt speaks again.
    let f = claude(&[todo(&[("Port", "in_progress")]), user("now fix the tests")]);
    assert_eq!((f.todo(), f.doing().as_deref()), (None, Some("now fix the tests")));
    let f = claude(&[todo(&[("Port", "in_progress")]), user("now fix the tests"), todo(&[("Fix", "in_progress")])]);
    assert_eq!(f.doing().as_deref(), Some("Fixing"));
    // Across reads too (the prompt and the list in different syncs).
    let mut f = Facts::default();
    feed_claude(&mut f, &lines(&[user("first"), todo(&[("Port", "in_progress")])]), true);
    assert_eq!(f.doing().as_deref(), Some("Porting"));
    feed_claude(&mut f, &lines(&[edit("e1", "/r/a.rs"), result("e1", false)]), true);
    assert_eq!(f.doing().as_deref(), Some("Porting"), "a read with no prompt changes nothing");
    feed_claude(&mut f, &lines(&[user("second")]), true);
    assert_eq!(f.doing().as_deref(), Some("second"));
    // Task tools: in progress until closed.
    let upd = |id: &str, st: &str| {
        asst(vec![tool("x", "TaskUpdate", obj![("taskId", id), ("status", st), ("activeForm", format!("Doing {id}"))])], [1, 0, 0],
            "claude-opus-5-5", None)
    };
    let f = claude(&[user("go"), upd("1", "in_progress"), upd("2", "in_progress")]);
    assert_eq!(f.doing().as_deref(), Some("Doing 2"));
    let f = claude(&[user("go"), upd("1", "in_progress"), upd("2", "in_progress"), upd("2", "completed")]);
    assert_eq!(f.doing().as_deref(), Some("Doing 1"));
    let f = claude(&[user("go"), upd("1", "in_progress"), upd("1", "completed")]);
    assert_eq!(f.doing().as_deref(), Some("go"));
    // Nothing at all.
    assert_eq!(claude(&[edit("e", "/a")]).doing(), None);
}

/// What counts as a prompt, and how it is cleaned.
#[test]
fn prompts_are_real_and_clean() {
    let at = |ls: &[String]| claude(ls).prompt;
    // Tool results, meta, compact summaries, notifications, peers: never.
    let base = user("the real one");
    for other in [
        result("e1", false),
        user_with(&[("isMeta", true.into())], "Caveat: injected".into()),
        user_with(&[("isCompactSummary", true.into())], "This session is being continued".into()),
        user_with(&[("isVisibleInTranscriptOnly", true.into())], "summary".into()),
        user_with(&[("origin", obj![("kind", "task-notification")])], "<task-notification>x</task-notification>".into()),
        user_with(&[("origin", obj![("kind", "peer"), ("from", "a1")])], "subagent says hi".into()),
        user("<local-command-caveat>Caveat: the messages below</local-command-caveat>"),
        user("<bash-input>ls</bash-input>"),
        user("   "),
    ] {
        assert_eq!(at(&[base.clone(), other.clone()]).as_deref(), Some("the real one"), "{other}");
    }
    // A human-origin prompt counts; another agent's message (tmux
    // `message_agent`'s wrapper) never: the real prompt before it speaks.
    let h = user_with(&[("origin", obj![("kind", "human")])], "typed by hand".into());
    assert_eq!(at(&[h]).as_deref(), Some("typed by hand"));
    assert_eq!(at(&[base.clone(), user("[message from @cua-notch] rebase done")]).as_deref(), Some("the real one"));
    assert_eq!(at(&[user("  [message from @x] hi")]), None);
    assert_eq!(clean_prompt("[message to the team] ok").as_deref(), Some("[message to the team] ok"));
    // Pasted content: the tags go, the text stays; first line, spaces collapsed.
    assert_eq!(at(&[user("<pasted_content id=\"4a48\">\n  Fix   the\tparser\nsecond line")]).as_deref(), Some("Fix the parser"));
    assert_eq!(clean_prompt("look: <pasted_content id=\"1\">x</pasted_content> ok").as_deref(), Some("look: x ok"));
    // A slash command as typed.
    let cmd = "<command-name>/model</command-name>\n<command-message>model</command-message>\n<command-args>opus</command-args>";
    assert_eq!(at(&[user(cmd)]).as_deref(), Some("/model opus"));
    // Text blocks (an image beside them is no matter).
    let blocks = arr(vec![obj![("type", "image"), ("source", obj![])], obj![("type", "text"), ("text", "what is this")]]);
    assert_eq!(at(&[user_with(&[], blocks)]).as_deref(), Some("what is this"));
    // Long: clipped.
    assert_eq!(clean_prompt(&"word ".repeat(100)).unwrap().chars().count(), 200);
}

/// Context: the latest main-chain usage summed; the window from the model.
#[test]
fn context_sum_and_window() {
    let a = |u: [u64; 3], model: &str, req: Option<&str>| asst(vec![obj![("type", "text"), ("text", "ok")]], u, model, req);
    let f = claude(&[a([10, 20, 30], "claude-opus-5-5", None), a([1000, 2000, 3000], "claude-opus-5-5", Some("claude-opus-5-5[1m]"))]);
    assert_eq!((f.ctx_used, f.ctx_window(Kind::Claude)), (Some(6000), WINDOW_1M));
    let f = claude(&[a([1000, 2000, 3000], "claude-opus-5-5", Some("claude-opus-5-5"))]);
    assert_eq!(f.ctx_window(Kind::Claude), WINDOW_200K);
    // Past 200k without the suffix (an old record): 1M after all; Fable is 1M.
    let f = claude(&[a([5, 250_000, 0], "claude-opus-5", None)]);
    assert_eq!((f.ctx_used, f.ctx_window(Kind::Claude)), (Some(250_005), WINDOW_1M));
    assert_eq!(claude(&[a([5, 5, 5], "claude-fable-5-1", None)]).ctx_window(Kind::Claude), WINDOW_1M);
    // A synthetic message (no usage) and a sidechain record never speak.
    let synth = a([0, 0, 0], "<synthetic>", None);
    let side = a([9, 9, 9], "claude-opus-5-5", None).replace("\"isSidechain\":false", "\"isSidechain\":true");
    let f = claude(&[a([100, 0, 0], "claude-opus-5-5", None), synth, side]);
    assert_eq!(f.ctx_used, Some(100));
    assert_eq!(claude(&[user("hi")]).ctx_used, None);
}

/// A 1M window inferred from usage is not sticky: a model switch, or a
/// compaction, starts the peak afresh; a usage from before a compaction
/// never speaks after it.
#[test]
fn window_after_a_switch_or_a_compaction() {
    let a = |u: [u64; 3], model: &str, req: Option<&str>| asst(vec![obj![("type", "text"), ("text", "ok")]], u, model, req);
    let compact = obj![("type", "system"), ("subtype", "compact_boundary"), ("content", "Conversation compacted")].to_json();
    // An old suffix-less 1M session (seen past 200k), then a switch to a 200k model.
    let f = claude(&[a([5, 300_000, 0], "claude-opus-5", None), a([5, 50_000, 0], "claude-sonnet-5-5", Some("claude-sonnet-5-5"))]);
    assert_eq!((f.ctx_used, f.ctx_window(Kind::Claude)), (Some(50_005), WINDOW_200K));
    // Across reads the same.
    let mut f = Facts::default();
    feed_claude(&mut f, &lines(&[a([5, 300_000, 0], "claude-opus-5", None)]), true);
    assert_eq!(f.ctx_window(Kind::Claude), WINDOW_1M);
    feed_claude(&mut f, &lines(&[a([5, 9_000, 0], "claude-haiku-5-5", Some("claude-haiku-5-5"))]), true);
    assert_eq!(f.ctx_window(Kind::Claude), WINDOW_200K);
    // A compaction with no assistant record after it: no usage known, the peak gone.
    let f = claude(&[a([5, 300_000, 0], "claude-opus-5", None), compact.clone()]);
    assert_eq!((f.ctx_used, f.ctx_window(Kind::Claude)), (None, WINDOW_200K));
    let f = claude(&[a([5, 300_000, 0], "claude-opus-5", None), compact, a([5, 20_000, 0], "claude-opus-5", None)]);
    assert_eq!((f.ctx_used, f.ctx_window(Kind::Claude)), (Some(20_005), WINDOW_200K));
}

/// Each kind of candidate keeps its own newest: a long run of usage records
/// (more than the cap) never pushes the last prompt out.
#[test]
fn candidate_caps_are_per_kind() {
    let d = scratch("cands");
    let p = d.join("s.jsonl");
    let mut ls = vec![user("the one prompt")];
    ls.extend((0..2 * CANDS_MAX + 10).map(|i| asst(vec![], [i as u64 + 1, 0, 0], "claude-opus-5-5", None)));
    fs::write(&p, lines(&ls)).unwrap();
    let mut t = Transcript::new(Kind::Claude, true);
    t.sync(&p, FIRST_MAX).unwrap();
    assert_eq!((t.facts.prompt.as_deref(), t.facts.ctx_used), (Some("the one prompt"), Some(2 * CANDS_MAX as u64 + 10)));
    let _ = fs::remove_dir_all(&d);
}

/// Settled edits: a call awaiting its result (a permission prompt) counts
/// as touched but not yet as an edit that wrote.
#[test]
fn settled_edits() {
    let f = claude(&[edit("e1", "/r/a.rs"), result("e1", false), edit("e2", "/r/a.rs")]);
    assert_eq!(f.edits(), vec![(PathBuf::from("/r/a.rs"), 1)]);
    let f = claude(&[edit("e1", "/r/a.rs"), result("e1", false), edit("e2", "/r/a.rs"), result("e2", false)]);
    assert_eq!(f.edits(), vec![(PathBuf::from("/r/a.rs"), 2)]);
    let f = claude(&[edit("e1", "/r/a.rs")]);
    assert_eq!((f.touched().len(), f.edits()), (1, vec![(PathBuf::from("/r/a.rs"), 0)]));
}

/// An edit whose result is an error does not count; any that went through
/// does. Every edit tool, by its path field; a relative path is none.
#[test]
fn edits_that_went_through() {
    let f = claude(&[edit("e1", "/r/a.rs"), result("e1", true)]);
    assert!(f.touched().is_empty());
    let f = claude(&[edit("e1", "/r/a.rs"), result("e1", false), edit("e2", "/r/a.rs"), result("e2", true)]);
    assert_eq!(f.touched(), paths(&["/r/a.rs"]));
    let w = asst(vec![tool("w", "Write", obj![("file_path", "/r/new.md"), ("content", "x")]),
        tool("m", "MultiEdit", obj![("file_path", "/r/m.rs"), ("edits", arr(vec![]))]),
        tool("n", "NotebookEdit", obj![("notebook_path", "/r/n.ipynb"), ("new_source", "x")]),
        tool("r", "Write", obj![("file_path", "rel.txt"), ("content", "x")]),
        tool("q", "Read", obj![("file_path", "/r/read.rs")])], [1, 0, 0], "claude-opus-5-5", None);
    assert_eq!(claude(&[w]).touched(), paths(&["/r/new.md", "/r/m.rs", "/r/n.ipynb"]));
    // A Write whose content quotes a user record is still an assistant record.
    let quoting = asst(vec![tool("w", "Write", obj![("file_path", "/r/fixture.rs"), ("content", user("quoted"))])], [1, 0, 0],
        "claude-opus-5-5", None);
    assert_eq!(claude(&[quoting]).touched(), paths(&["/r/fixture.rs"]));
    // A subagent's transcript (every record a sidechain one): its edits count.
    let mut f = Facts::default();
    feed_claude(&mut f, &lines(&[edit("e9", "/r/sub.rs").replace("\"isSidechain\":false", "\"isSidechain\":true")]), false);
    assert_eq!(f.touched(), paths(&["/r/sub.rs"]));
}

// ---------------------------------------------------------------- codex

fn cx(top: &str, payload: Value) -> String {
    obj![("timestamp", "2026-10-21T07:20:50.000Z"), ("type", top), ("payload", payload)].to_json()
}

fn codex(ls: &[String]) -> Facts {
    let mut f = Facts::default();
    feed_codex(&mut f, &lines(ls));
    f
}

#[test]
fn codex_rollout() {
    let meta = cx("session_meta", obj![("id", "s1"), ("cwd", "/w"), ("context_window", 272_000)]);
    let ctx = cx("turn_context", obj![("cwd", "/w"), ("model", "gpt-6-codex")]);
    let env = cx("response_item", obj![("type", "message"), ("role", "user"),
        ("content", arr(vec![obj![("type", "input_text"), ("text", "<environment_context>\n<cwd>/w</cwd>")]]))]);
    let agents = cx("response_item", obj![("type", "message"), ("role", "user"),
        ("content", arr(vec![obj![("type", "input_text"), ("text", "# AGENTS.md instructions for /w")]]))]);
    let ask = cx("event_msg", obj![("type", "item_completed"), ("item", obj![("type", "UserMessage"), ("id", "u"),
        ("content", arr(vec![obj![("type", "text"), ("text", "Fix the   parser\nplease")]]))])]);
    let change = cx("event_msg", obj![("type", "item_completed"), ("item", obj![("type", "FileChange"), ("id", "i"), ("status", "completed"),
        ("changes", obj![
            ("/w/a.rs", obj![("type", "update"), ("unified_diff", "@@"), ("move_path", Value::Null)]),
            ("/w/old.rs", obj![("type", "update"), ("unified_diff", "@@"), ("move_path", "/w/moved.rs")]),
            ("/w/b.rs", obj![("type", "add"), ("content", "x")])
        ])])]);
    let failed = cx("event_msg", obj![("type", "item_completed"), ("item", obj![("type", "FileChange"), ("id", "j"), ("status", "failed"),
        ("changes", obj![("/w/nope.rs", obj![("type", "add"), ("content", "x")])])])]);
    let tokens = cx("event_msg", obj![("type", "token_count"), ("info", obj![
        ("total_token_usage", obj![("input_tokens", 1_239_852), ("output_tokens", 5078)]),
        ("last_token_usage", obj![("input_tokens", 99_577), ("cached_input_tokens", 97_920), ("output_tokens", 59)]),
        ("model_context_window", 828_400)])]);
    let no_info = cx("event_msg", obj![("type", "token_count"), ("info", Value::Null)]);
    let f = codex(&[meta.clone(), ctx.clone(), env.clone(), ask, agents, change, failed, tokens, no_info]);
    assert_eq!(f.touched(), paths(&["/w/a.rs", "/w/old.rs", "/w/moved.rs", "/w/b.rs"]));
    assert_eq!((f.ctx_used, f.ctx_window(Kind::Codex)), (Some(99_577), 828_400));
    assert_eq!(f.doing().as_deref(), Some("Fix the parser"));
    assert_eq!((f.cwd.as_str(), f.model.as_str()), ("/w", "gpt-6-codex"));
    // A plan step in progress speaks first; a finished plan gives it back.
    let plan = |st: &str| {
        let args = obj![("plan", arr(vec![obj![("step", "Write the tests"), ("status", st)]]))].to_json();
        cx("response_item", obj![("type", "function_call"), ("name", "update_plan"), ("arguments", args), ("call_id", "c")])
    };
    let f = codex(&[ctx.clone(), cx("event_msg", obj![("type", "user_message"), ("message", "ship it")]), plan("in_progress")]);
    assert_eq!(f.doing().as_deref(), Some("Write the tests"));
    let f = codex(&[ctx.clone(), cx("event_msg", obj![("type", "user_message"), ("message", "ship it")]), plan("in_progress"), plan("completed")]);
    assert_eq!(f.doing().as_deref(), Some("ship it"));
    // No token_count yet: the session's own window, nothing used.
    let f = codex(&[meta]);
    assert_eq!((f.ctx_used, f.ctx_window(Kind::Codex)), (None, 272_000));
}

/// An older rollout with no FileChange items: the patches' own paths (a
/// relative one under the turn's cwd; one inside code mode's escaped JS);
/// once a FileChange item shows up, they are no longer needed.
#[test]
fn codex_patches_without_file_changes() {
    let ctx = cx("turn_context", obj![("cwd", "/w"), ("model", "gpt-6")]);
    let custom = cx("response_item", obj![("type", "custom_tool_call"), ("name", "apply_patch"), ("call_id", "c1"),
        ("input", "*** Begin Patch\n*** Update File: src/x.rs\n@@\n-a\n+b\n*** End Patch\n")]);
    let js = "const r = await tools.apply_patch(\"*** Begin Patch\\n*** Add File: /w/new.rs\\n+x\\n*** Delete File: gone.rs\\n*** End Patch\");";
    let exec = cx("response_item", obj![("type", "custom_tool_call"), ("name", "exec"), ("call_id", "c2"), ("input", js)]);
    let f = codex(&[ctx.clone(), custom.clone(), exec]);
    let mut got = f.touched();
    got.sort();
    assert_eq!(got, paths(&["/w/gone.rs", "/w/new.rs", "/w/src/x.rs"]));
    let change = cx("event_msg", obj![("type", "item_completed"), ("item", obj![("type", "FileChange"), ("id", "i"), ("status", "completed"),
        ("changes", obj![("/w/src/x.rs", obj![("type", "update"), ("unified_diff", "@@")])])])]);
    assert_eq!(codex(&[ctx, custom, change]).touched(), paths(&["/w/src/x.rs"]));
    assert_eq!(patch_paths("*** Update File: a.rs\\n*** Move to: b.rs\\n", "/w"), paths(&["/w/a.rs", "/w/b.rs"]));
}

// ---------------------------------------------------------------- git

fn has_git() -> bool {
    Command::new("git").arg("--version").output().map(|o| o.status.success()).unwrap_or(false)
}

fn sh(dir: &Path, args: &[&str]) {
    let st = Command::new("git").args(args).current_dir(dir).env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_AUTHOR_NAME", "t").env("GIT_AUTHOR_EMAIL", "t@t").env("GIT_COMMITTER_NAME", "t").env("GIT_COMMITTER_EMAIL", "t@t")
        .output().unwrap();
    assert!(st.status.success(), "git {args:?}: {}", String::from_utf8_lossy(&st.stderr));
}

fn repo(tag: &str) -> PathBuf {
    let d = scratch(tag);
    sh(&d, &["init", "-q", "-b", "main"]);
    fs::write(d.join("a.txt"), "1\n2\n3\n").unwrap();
    fs::write(d.join("c.txt"), "same\n").unwrap();
    fs::write(d.join("gone.txt"), "x\ny\n").unwrap();
    fs::write(d.join(".gitignore"), "ignored.txt\n").unwrap();
    sh(&d, &["add", "-A"]);
    sh(&d, &["commit", "-q", "-m", "one"]);
    d
}

/// Modified, untracked (all its lines added), deleted (all removed),
/// unchanged and ignored files; after a commit, nothing.
#[test]
fn numstat_untracked_and_committed() {
    if !has_git() {
        return;
    }
    let d = repo("numstat");
    fs::write(d.join("a.txt"), "1\nTWO\n3\n4\n").unwrap();
    fs::write(d.join("b.txt"), "new\nfile").unwrap();
    fs::remove_file(d.join("gone.txt")).unwrap();
    fs::write(d.join("ignored.txt"), "x\n").unwrap();
    let rels: Vec<String> = ["a.txt", "b.txt", "c.txt", "gone.txt", "ignored.txt"].map(String::from).to_vec();
    let got = numstat(&d, &rels).unwrap();
    let want: HashMap<String, (u64, u64)> =
        [("a.txt", (2, 1)), ("b.txt", (2, 0)), ("gone.txt", (0, 2))].map(|(k, v)| (k.to_string(), v)).into_iter().collect();
    assert_eq!(got, (want, true));
    sh(&d, &["add", "-A"]);
    sh(&d, &["commit", "-q", "-m", "two"]);
    assert_eq!(numstat(&d, &rels), Some((HashMap::new(), true)));
    assert_eq!((count_lines(&d.join("b.txt")), count_lines(&d.join("nope"))), (2, 0));
    let _ = fs::remove_dir_all(&d);
}

/// A FIFO (or device) where a file is expected never blocks: not counted,
/// not read as a transcript.
#[test]
fn fifos_never_block() {
    let d = scratch("fifo");
    let fifo = d.join("pipe.jsonl");
    if !Command::new("mkfifo").arg(&fifo).status().is_ok_and(|s| s.success()) {
        return;
    }
    let t = Instant::now();
    assert_eq!(count_lines(&fifo), 0);
    assert!(Transcript::new(Kind::Claude, true).sync(&fifo, FIRST_MAX).is_err());
    assert!(t.elapsed() < Duration::from_secs(1));
    let _ = fs::remove_dir_all(&d);
}

/// A repo with no commit yet answers whole: diffed against the empty tree,
/// a staged file counts as added, an untracked one too.
#[test]
fn numstat_before_the_first_commit() {
    if !has_git() {
        return;
    }
    let d = scratch("nohead");
    sh(&d, &["init", "-q", "-b", "main"]);
    fs::write(d.join("new.txt"), "a\nb\n").unwrap();
    fs::write(d.join("staged.txt"), "x\n").unwrap();
    sh(&d, &["add", "staged.txt"]);
    let rels = vec!["new.txt".to_string(), "staged.txt".to_string()];
    let want: HashMap<String, (u64, u64)> =
        [("new.txt", (2, 0)), ("staged.txt", (1, 0))].map(|(k, v)| (k.to_string(), v)).into_iter().collect();
    assert_eq!(numstat(&d, &rels), Some((want, true)));
    let _ = fs::remove_dir_all(&d);
}

/// A half answer (here: HEAD names a missing commit, so the diff fails
/// while the untracked list works) is used, but never cached as whole:
/// asked again once allowed, though nothing changed.
#[test]
fn half_answers_are_asked_again() {
    if !has_git() {
        return;
    }
    let d = scratch("half");
    sh(&d, &["init", "-q", "-b", "main"]);
    fs::write(d.join("new.txt"), "a\nb\n").unwrap();
    let main_ref = d.join(".git/refs/heads/main");
    fs::write(&main_ref, "1234567890123456789012345678901234567890\n").unwrap();
    let rels = vec!["new.txt".to_string()];
    assert_eq!(numstat(&d, &rels), Some(([("new.txt".to_string(), (2, 0))].into_iter().collect(), false)));
    let mut e = Engine::new(d.join("c"), d.join("s"), d.join("x"), Box::new(Alive));
    e.ask_repo(&d, &rels, 100.0, 1);
    e.ask_repo(&d, &rels, 101.0, 2);
    assert_eq!(e.git_runs, 1, "throttled");
    e.ask_repo(&d, &rels, 106.0, 3);
    assert_eq!(e.git_runs, 2, "half answer: asked again");
    assert_eq!(e.repos[&d].valid, 0, "never current");
    // A whole one, once the bad ref is gone (no HEAD: the empty tree's
    // diff): then nothing changed means not asked.
    fs::remove_file(&main_ref).unwrap();
    e.ask_repo(&d, &rels, 112.0, 4);
    e.ask_repo(&d, &rels, 118.0, 5);
    assert_eq!((e.git_runs, e.repos[&d].valid), (3, 5));
    let _ = fs::remove_dir_all(&d);
}

/// Everything alive (no real processes asked).
struct Alive;

impl Procs for Alive {
    fn alive(&self, _: i32) -> bool {
        true
    }
    fn argv(&self, _: i32) -> Option<Vec<String>> {
        None
    }
    fn children(&self, _: i32) -> Vec<i32> {
        Vec::new()
    }
}

/// Through the engine: two Claude agents in ONE checkout (and one's
/// subagent) each get their own files' diff; a commit drops them out; a
/// Codex window reads its rollout; an OpenCode one has none.
#[test]
fn engine_attributes_per_agent() {
    if !has_git() {
        return;
    }
    let d = repo("engine");
    let root = scratch("engine-claude");
    let (sessions, projects) = (root.join("sessions"), root.join("projects"));
    let cwd = d.to_string_lossy().into_owned();
    let slug_dir = projects.join(crate::sidebar::activity::project_slug(&cwd));
    fs::create_dir_all(slug_dir.join("sid-a/subagents")).unwrap();
    fs::create_dir_all(&sessions).unwrap();
    for (pid, sid, win, pane) in [(11, "sid-a", "@1", "%1"), (12, "sid-b", "@2", "%2")] {
        let v = obj![("pid", pid), ("sessionId", sid), ("cwd", cwd.as_str()), ("tmux", format!("main:{win}.{pane}")), ("startedAt", 5)];
        fs::write(sessions.join(format!("{pid}.json")), v.to_json()).unwrap();
    }
    let p = |f: &str| d.join(f).to_string_lossy().into_owned();
    let usage = asst(vec![], [100, 200, 300], "claude-opus-5-5", Some("claude-opus-5-5[1m]"));
    fs::write(slug_dir.join("sid-a.jsonl"), lines(&[user("refactor a"), edit("e1", &p("a.txt")), usage])).unwrap();
    fs::write(slug_dir.join("sid-a/subagents/agent-1.jsonl"), lines(&[edit("s1", &p("b.txt"))])).unwrap();
    fs::write(slug_dir.join("sid-b.jsonl"), lines(&[user("tidy c"), edit("e2", &p("c.txt")), edit("e3", "/nowhere/x.txt")])).unwrap();
    let codex_root = scratch("engine-codex");
    let rollout = codex_root.join("rollout-1.jsonl");
    fs::write(&rollout, lines(&[cx("event_msg", obj![("type", "user_message"), ("message", "port it")])])).unwrap();
    fs::write(d.join("a.txt"), "1\n2\n3\n4\n").unwrap(); // a: +1
    fs::write(d.join("b.txt"), "x\ny\n").unwrap(); // b: untracked, +2
    fs::write(d.join("c.txt"), "different\n").unwrap(); // c: +1 −1
    let snap = Snapshot::parse(&[
        row("main", 1, "@1", &[("state", "running"), ("kind", "claude"), ("pane_flags", "A0%1 "), ("path", &cwd)]),
        row("main", 2, "@2", &[("state", "idle"), ("kind", "claude"), ("pane_flags", "A0%2 "), ("path", "/elsewhere")]),
        row("main", 3, "@3", &[("state", "idle"), ("kind", "codex"), ("rollout", &rollout.to_string_lossy())]),
        row("main", 4, "@4", &[("state", "idle"), ("kind", "opencode")]),
        row("main", 5, "@5", &[("state", ""), ("pane_flags", "A0%5 ")]),
    ].join("\n"));
    let mut e = Engine::new(root.clone(), sessions, codex_root.clone(), Box::new(Alive));
    let got = e.update(&snap, 1000.0);
    assert_eq!(got.keys().collect::<Vec<_>>(), ["@1", "@2", "@3"]);
    let a = &got["@1"];
    assert_eq!(a.diff, Some(DiffStat { added: 3, removed: 0, files: 2 }));
    assert_eq!(a.dirty_files, vec![d.join("a.txt"), d.join("b.txt")]);
    assert_eq!((a.doing.as_deref(), a.ctx_used, a.ctx_window), (Some("refactor a"), Some(600), WINDOW_1M));
    // @2's transcript found by the scan (its pane sits elsewhere); its file outside any repo: not dirty.
    let b = &got["@2"];
    assert_eq!((b.diff, b.touched), (Some(DiffStat { added: 1, removed: 1, files: 1 }), 2));
    assert_eq!(got["@3"].doing.as_deref(), Some("port it"));
    assert_eq!(got["@3"].diff, None);
    assert_eq!(e.git_runs, 1, "one repo, asked once for both agents");
    // Nothing changed: git is not asked again.
    e.update(&snap, 1002.0);
    assert_eq!(e.git_runs, 1);
    // Committed: they drop out (once the repo may be asked again).
    sh(&d, &["add", "-A"]);
    sh(&d, &["commit", "-q", "-m", "two"]);
    let got = e.update(&snap, 1004.0);
    assert_eq!((got["@1"].diff, e.git_runs), (Some(DiffStat { added: 3, removed: 0, files: 2 }), 1), "throttled");
    let got = e.update(&snap, 1006.0);
    assert_eq!((got["@1"].diff, got["@2"].diff, e.git_runs), (None, None, 2));
    assert!(got["@1"].dirty_files.is_empty());
    for x in [&d, &root, &codex_root] {
        let _ = fs::remove_dir_all(x);
    }
}

/// One file, two agents: A edits it and the change is committed; B edits
/// it later. The dirty change is B's alone, until A edits it once more.
#[test]
fn a_committed_edit_is_not_anothers_later_change() {
    if !has_git() {
        return;
    }
    let d = repo("leak");
    let root = scratch("leak-claude");
    let (sessions, projects) = (root.join("sessions"), root.join("projects"));
    let cwd = d.to_string_lossy().into_owned();
    let dir = projects.join(crate::sidebar::activity::project_slug(&cwd));
    fs::create_dir_all(&dir).unwrap();
    fs::create_dir_all(&sessions).unwrap();
    for (pid, sid, win, pane) in [(21, "sid-a", "@1", "%1"), (22, "sid-b", "@2", "%2")] {
        let v = obj![("pid", pid), ("sessionId", sid), ("tmux", format!("main:{win}.{pane}")), ("startedAt", 5)];
        fs::write(sessions.join(format!("{pid}.json")), v.to_json()).unwrap();
    }
    let a_txt = d.join("a.txt").to_string_lossy().into_owned();
    let (ta, tb) = (dir.join("sid-a.jsonl"), dir.join("sid-b.jsonl"));
    fs::write(&ta, lines(&[user("edit a"), edit("a1", &a_txt), result("a1", false)])).unwrap();
    fs::write(&tb, lines(&[user("wait")])).unwrap();
    fs::write(d.join("a.txt"), "1\n2\n3\nA\n").unwrap();
    let snap = Snapshot::parse(&[
        row("main", 1, "@1", &[("state", "idle"), ("kind", "claude"), ("pane_flags", "A0%1 "), ("path", &cwd)]),
        row("main", 2, "@2", &[("state", "idle"), ("kind", "claude"), ("pane_flags", "A0%2 "), ("path", &cwd)]),
    ].join("\n"));
    let mut e = Engine::new(root.clone(), sessions, root.join("codex"), Box::new(Alive));
    e.diff_every = 0.0;
    let got = e.update(&snap, 1000.0);
    assert_eq!((got["@1"].diff.map(|x| x.files), got["@2"].diff), (Some(1), None));
    // A's change committed: clean, and seen clean after A's edit.
    sh(&d, &["commit", "-qam", "a"]);
    let got = e.update(&snap, 1001.0);
    assert_eq!(got["@1"].diff, None);
    // B edits the same file later.
    append(&tb, &lines(&[edit("b1", &a_txt), result("b1", false)]));
    fs::write(d.join("a.txt"), "1\n2\n3\nA\nB\n").unwrap();
    let got = e.update(&snap, 1002.0);
    assert_eq!((got["@1"].diff, got["@2"].diff.map(|x| x.added)), (None, Some(1)), "B's change is not A's");
    assert_eq!(got["@1"].touched, 1, "still a file A touched");
    // A edits it again: A's too now.
    append(&ta, &lines(&[edit("a2", &a_txt), result("a2", false)]));
    let got = e.update(&snap, 1003.0);
    assert_eq!((got["@1"].diff.map(|x| x.files), got["@2"].diff.map(|x| x.files)), (Some(1), Some(1)));
    // A clean answer that is not current (git throttled) marks nothing: A
    // edits, git may not be asked yet, and A keeps its edit.
    sh(&d, &["commit", "-qam", "ab"]);
    e.diff_every = 1000.0;
    let got = e.update(&snap, 1004.0);
    assert!(got["@1"].diff.is_some(), "the old answer still shows it dirty");
    for x in [&d, &root] {
        let _ = fs::remove_dir_all(x);
    }
}

/// One pass that misses a window (its sessions file unreadable for a pass,
/// say) keeps its transcript's cache and its attribution: back the next
/// pass, a file it committed is still not its; gone past KEEP_FOR, both go.
#[test]
fn a_missed_pass_keeps_the_history() {
    if !has_git() {
        return;
    }
    let d = repo("missed");
    let root = scratch("missed-claude");
    let (sessions, projects) = (root.join("sessions"), root.join("projects"));
    let cwd = d.to_string_lossy().into_owned();
    let dir = projects.join(crate::sidebar::activity::project_slug(&cwd));
    fs::create_dir_all(&dir).unwrap();
    fs::create_dir_all(&sessions).unwrap();
    for (pid, sid, win, pane) in [(31, "sid-a", "@1", "%1"), (32, "sid-b", "@2", "%2")] {
        let v = obj![("pid", pid), ("sessionId", sid), ("tmux", format!("main:{win}.{pane}")), ("startedAt", 5)];
        fs::write(sessions.join(format!("{pid}.json")), v.to_json()).unwrap();
    }
    let a_txt = d.join("a.txt").to_string_lossy().into_owned();
    let (ta, tb) = (dir.join("sid-a.jsonl"), dir.join("sid-b.jsonl"));
    fs::write(&ta, lines(&[user("edit a"), edit("a1", &a_txt), result("a1", false)])).unwrap();
    fs::write(&tb, lines(&[user("wait")])).unwrap();
    fs::write(d.join("a.txt"), "1\n2\n3\nA\n").unwrap();
    let a_row = row("main", 1, "@1", &[("state", "idle"), ("kind", "claude"), ("pane_flags", "A0%1 "), ("path", &cwd)]);
    let b_row = row("main", 2, "@2", &[("state", "idle"), ("kind", "claude"), ("pane_flags", "A0%2 "), ("path", &cwd)]);
    let snap = Snapshot::parse(&[a_row, b_row.clone()].join("\n"));
    let only_b = Snapshot::parse(&b_row);
    let mut e = Engine::new(root.clone(), sessions, root.join("codex"), Box::new(Alive));
    e.diff_every = 0.0;
    assert_eq!(e.update(&snap, 1000.0)["@1"].diff.map(|x| x.files), Some(1));
    // A's change committed (seen clean after A's edit); B edits it later.
    sh(&d, &["commit", "-qam", "a"]);
    assert_eq!(e.update(&snap, 1001.0)["@1"].diff, None);
    append(&tb, &lines(&[edit("b1", &a_txt), result("b1", false)]));
    fs::write(d.join("a.txt"), "1\n2\n3\nA\nB\n").unwrap();
    // A pass without A: its cache and history stay.
    e.update(&only_b, 1002.0);
    assert_eq!((e.files.len(), e.seen.len(), e.found.len()), (2, 2, 2));
    let got = e.update(&snap, 1003.0);
    assert_eq!((got["@1"].diff, got["@2"].diff.map(|x| x.added)), (None, Some(1)), "B's change is still not A's");
    // Gone for longer than KEEP_FOR: forgotten, its session id's lookup too.
    e.update(&only_b, 1003.0 + KEEP_FOR + 1.0);
    assert_eq!((e.files.len(), e.seen.len()), (1, 1));
    assert_eq!(e.found.keys().collect::<Vec<_>>(), ["sid-b"]);
    // A session id looked for in vain is kept only while it would not be
    // looked for again (RETRY).
    fs::remove_file(&tb).unwrap();
    let t = 1003.0 + KEEP_FOR + 2.0;
    e.update(&only_b, t);
    assert!(matches!(e.found.get("sid-b"), Some(Err(_))), "{:?}", e.found);
    e.update(&Snapshot::parse(""), t + RETRY + 1.0);
    assert!(e.found.is_empty(), "{:?}", e.found);
    for x in [&d, &root] {
        let _ = fs::remove_dir_all(x);
    }
}

/// A panic in the engine never leaves the worker busy: the engine is made
/// afresh, and after PANICS_MAX in a row insights turn off for good.
#[test]
fn a_panicking_worker_turns_off() {
    struct Boom;
    impl Procs for Boom {
        fn alive(&self, _: i32) -> bool {
            panic!("boom (a test of the worker's panic handling)")
        }
        fn argv(&self, _: i32) -> Option<Vec<String>> {
            None
        }
        fn children(&self, _: i32) -> Vec<i32> {
            Vec::new()
        }
    }
    let d = scratch("panic");
    fs::create_dir_all(d.join("sessions")).unwrap();
    let v = obj![("pid", 7), ("sessionId", "s"), ("tmux", "main:@1.%1"), ("startedAt", 5)];
    fs::write(d.join("sessions/7.json"), v.to_json()).unwrap();
    let snap = Snapshot::parse(&row("main", 1, "@1", &[("state", "idle"), ("kind", "claude"), ("pane_flags", "A0%1 ")]));
    let made = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let (dd, m) = (d.clone(), made.clone());
    let mut live = Insights::with(move || {
        m.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        Engine::new(dd.join("claude"), dd.join("sessions"), dd.join("codex"), Box::new(Boom))
    });
    live.every = Duration::ZERO;
    let idle = |live: &Insights| {
        let t = Instant::now();
        while live.shared.lock().unwrap().busy && t.elapsed() < Duration::from_secs(10) {
            std::thread::sleep(Duration::from_millis(5));
        }
        !live.shared.lock().unwrap().busy
    };
    for n in 1..=PANICS_MAX as usize {
        live.refresh(&snap);
        assert!(idle(&live), "busy cleared after panic {n}");
        assert!(!live.pending());
        assert_eq!(made.load(std::sync::atomic::Ordering::SeqCst), n, "made afresh");
    }
    assert!(live.shared.lock().unwrap().dead);
    live.refresh(&snap);
    assert!(live.tx.is_none() && live.make.is_none() && !live.pending());
    let _ = fs::remove_dir_all(&d);
}

/// The live side: off reads nothing; on, results land from the worker
/// thread, and a refresh inside `every` hands it nothing.
#[test]
fn insights_from_the_worker() {
    let mut off = Insights::off();
    off.refresh(&Snapshot::default());
    assert_eq!((off.generation(), off.get("@1")), (0, None));
    assert!(off.tx.is_none());
    let d = scratch("worker");
    let roll = d.join("rollout-x.jsonl");
    fs::write(&roll, lines(&[cx("event_msg", obj![("type", "user_message"), ("message", "port the parser")])])).unwrap();
    let snap = Snapshot::parse(&row("main", 1, "@3", &[("state", "running"), ("kind", "codex"), ("rollout", &roll.to_string_lossy())]));
    let dd = d.clone();
    let mut live = Insights::with(move || Engine::new(dd.join("claude"), dd.join("sessions"), dd.join("codex"), Box::new(Alive)));
    live.every = Duration::from_secs(3600);
    live.refresh(&snap);
    let t = Instant::now();
    while live.generation() == 0 && t.elapsed() < Duration::from_secs(10) {
        std::thread::sleep(Duration::from_millis(5));
    }
    assert_eq!(live.get("@3").and_then(|i| i.doing).as_deref(), Some("port the parser"));
    // Inside `every`: nothing handed over, nothing new lands.
    live.refresh(&snap);
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(live.generation(), 1);
    let _ = fs::remove_dir_all(&d);
}

#[test]
fn rollout_lookup_and_formatting() {
    let d = scratch("rollouts");
    for (day, name) in [("2026/10/20", "rollout-2026-10-20T01-00-00-abc.jsonl"), ("2026/10/21", "rollout-2026-10-21T01-00-00-def.jsonl")] {
        fs::create_dir_all(d.join(day)).unwrap();
        fs::write(d.join(day).join(name), "").unwrap();
    }
    assert_eq!(find_rollout(&d, "abc"), Some(d.join("2026/10/20/rollout-2026-10-20T01-00-00-abc.jsonl")));
    assert_eq!(find_rollout(&d, "zzz"), None);
    assert_eq!((tokens(950), tokens(182_345), tokens(1_000_000), tokens(1_500_000)), ("950".into(), "182k".into(), "1M".into(), "1.5M".into()));
    assert_eq!(DiffStat { added: 48, removed: 12, files: 3 }.badge(), "+48 −12 · 3f");
    let _ = fs::remove_dir_all(&d);
}
