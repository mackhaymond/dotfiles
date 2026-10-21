use super::*;
use std::fs;

fn s(v: &[&str]) -> Vec<String> {
    v.iter().map(|x| x.to_string()).collect()
}

#[test]
fn args() {
    let a = parse_args(&s(&["@12", "--client", "/dev/ttys004"])).unwrap();
    assert_eq!(a, Args { win: "@12".into(), client: "/dev/ttys004".into(), after: None, print: false });
    let a = parse_args(&s(&["--after", "4242", "--print", "@3", "--client", "/dev/ttys1"])).unwrap();
    assert_eq!((a.win.as_str(), a.client.as_str(), a.after, a.print), ("@3", "/dev/ttys1", Some(4242), true));
    // No client is refused: tmux would guess one.
    for bad in [&[][..], &["@3"], &["@3", "--client", ""], &["12", "--client", "c"], &["@1", "@2", "--client", "c"],
        &["@1", "--client"], &["@1", "--client", "c", "--after", "x"], &["@1", "--client", "c", "--after", "0"], &["@1", "--client", "c", "-x"]] {
        assert!(parse_args(&s(bad)).is_err(), "{bad:?}");
    }
}

#[test]
fn insight_files_format_back_into_repos() {
    let f = parse_files("/r/a\n\tsrc/x.rs\n\tREADME.md\n/r/empty\n/r/b\n\tdocs/y.md\n\n");
    assert_eq!(f, vec![
        (PathBuf::from("/r/a"), s(&["src/x.rs", "README.md"])),
        (PathBuf::from("/r/b"), s(&["docs/y.md"])),
    ]);
    assert!(parse_files("\tstray\n").is_empty());
}

/// The files: `AGENT_UI_DIFF_FILES`, else a live publisher's file (absent:
/// none dirty), else the unverified engine.
#[test]
fn which_files() {
    assert_eq!(source(true, true), Source::Given);
    assert_eq!(source(true, false), Source::Given);
    assert_eq!(source(false, true), Source::Sidebar);
    assert_eq!(source(false, false), Source::Engine);
    let dir = std::env::temp_dir().join(format!("agentui-diff-files-{}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    let p = insight::files_path_in(&dir, "1700000000", "@7");
    assert_eq!(read_files(&p), Ok(Vec::new())); // no file: nothing dirty
    fs::write(&p, "/r\n\ta.rs\n").unwrap();
    assert_eq!(read_files(&p), Ok(vec![(PathBuf::from("/r"), s(&["a.rs"]))]));
    let _ = fs::remove_dir_all(&dir);
}

/// Pathspecs are cut so no argv nears ARG_MAX; every path lands in exactly
/// one batch, in order, and an oversized one still gets its own.
#[test]
fn pathspec_batching() {
    let rels: Vec<String> = (0..1000).map(|i| format!("dir/file-{i:04}.rs")).collect(); // 16 bytes + NUL each
    let b = batches(&rels, 17 * 100);
    assert_eq!(b.len(), 10);
    assert!(b.iter().all(|x| x.len() == 100));
    assert_eq!(b.concat(), rels);
    assert!(batches(&[], 10).is_empty());
    let big = s(&["a", &"x".repeat(50), "b"]);
    assert_eq!(batches(&big, 10), vec![&big[0..1], &big[1..2], &big[2..3]]);
    let all = s(&["a", "b", "c"]);
    assert_eq!(batches(&all, ARG_BYTES), vec![&all[..]]);
}

#[test]
fn stat_counts_hunk_lines_only() {
    let d = "diff --git a/x b/x\nindex 1..2 100644\n--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n-old\n+new\n+++plus\n ctx\n\
             diff --git a/bin b/bin\nBinary files a/bin and b/bin differ\n";
    assert_eq!(stat(d), DiffStat { added: 2, removed: 1, files: 2 });
    assert_eq!(stat(""), DiffStat::default());
    // Left-out untracked files count as files, with no lines.
    let skipped = format!("{SKIP_MARK}big.bin (3.0 MB)\n{SKIP_MARK}12 more untracked files\n");
    assert_eq!(stat(&skipped), DiffStat { added: 0, removed: 0, files: 13 });
}

#[test]
fn colours_like_git() {
    let c = colour("# repo /r/b\ndiff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n c\n# untracked, not shown: big (2.0 MB)\n");
    let lines: Vec<&str> = c.lines().collect();
    assert!(lines[0].starts_with("\x1b[1;35m━━ /r/b ━━"), "{:?}", lines[0]); // a rule between repos
    assert_eq!(lines[1], "\x1b[1mdiff --git a/x b/x\x1b[m");
    assert_eq!(lines[3], "\x1b[1m+++ b/x\x1b[m"); // a header, not an addition
    assert_eq!(lines[4], "\x1b[36m@@ -1 +1 @@\x1b[m");
    assert_eq!((lines[5], lines[6], lines[7]), ("\x1b[31m-a\x1b[m", "\x1b[32m+b\x1b[m", " c"));
    assert_eq!(lines[8], "\x1b[33m# untracked, not shown: big (2.0 MB)\x1b[m");
}

#[test]
fn pager_choice_and_script() {
    let delta = Pager::choose(Some("/bin/delta".into()), "/usr/bin/less".into(), false);
    assert_eq!(delta, Pager::Delta { delta: "/bin/delta".into(), less: "/usr/bin/less".into() });
    assert_eq!(Pager::choose(Some("/bin/delta".into()), "/usr/bin/less".into(), true), Pager::Less("/usr/bin/less".into()));
    assert_eq!(Pager::choose(None, "/usr/bin/less".into(), false), Pager::Less("/usr/bin/less".into()));
    let close = s(&["/bin/tmux", "-L", "x", "display-popup", "-C", "-c", "/dev/ttys004"]);
    assert_eq!(delta.script(Path::new("/tmp/d.diff"), &close),
        "exec <'/tmp/d.diff' && rm -f '/tmp/d.diff'; DELTA_PAGER='/usr/bin/less -R -c --mouse' '/bin/delta' --paging=always; \
         '/bin/tmux' '-L' 'x' 'display-popup' '-C' '-c' '/dev/ttys004'");
    assert!(Pager::Less("/usr/bin/less".into()).script(Path::new("/t"), &close).contains("; '/usr/bin/less' -R -c --mouse; "));
    // The command is no format: a `#` (or a quote) in a path stays as is.
    let odd = Pager::Less("/l".into()).script(Path::new("/tmp/a#1'b.diff"), &close);
    assert!(odd.starts_with("exec <'/tmp/a#1'\\''b.diff' && rm -f '/tmp/a#1'\\''b.diff'; "), "{odd}");
    assert_eq!(quote("a#b"), "'a#b'");
    assert_eq!(delta.content("+a\n"), "+a\n");
    assert!(Pager::Less("l".into()).content("diff --git a/x b/x\n").starts_with("\x1b[1m"));
}

#[test]
fn popup_and_title() {
    let st = DiffStat { added: 48, removed: 12, files: 3 };
    assert_eq!(title("d", "fix #12 the cap", &st, true), " d · fix ##12 the cap · +48 −12 · 3f ");
    assert_eq!(title("d", "x", &st, false), " d · x · +48 −12 · 3f · unverified ");
    assert_eq!(title("", "x", &DiffStat { added: 1, removed: 0, files: 1 }, true), " x · +1 −0 · 1f ");
    let v = popup_args("/dev/ttys004", " d ", Path::new("/r#1"), "SCRIPT");
    assert_eq!(v, s(&["display-popup", "-c", "/dev/ttys004", "-w", "90%", "-h", "85%", "-x", POPUP_X, "-T", " d ", "-d", "/r#1", "SCRIPT"]));
    assert!(!v.iter().any(|a| a == "-E"), "Esc must reach tmux, not the pager");
    let tmux = Tmux::with_socket(Some(Socket::Name("priv".into())));
    assert_eq!(&close_argv(&tmux, "/dev/ttys9")[1..], &s(&["-L", "priv", "display-popup", "-C", "-c", "/dev/ttys9"])[..]);
    assert_eq!(&close_argv(&Tmux::with_socket(None), "/dev/ttys9")[1..], &s(&["display-popup", "-C", "-c", "/dev/ttys9"])[..]);
}

fn sh(dir: &Path, cmd: &str) {
    let st = Command::new("/bin/sh").arg("-c").arg(cmd).current_dir(dir).env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1").status().unwrap();
    assert!(st.success(), "{cmd}");
}

fn repo(tag: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("agentui-diff-{tag}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&d);
    fs::create_dir_all(&d).unwrap();
    sh(&d, "git init -q . && git config user.email t@t && git config user.name t && git config commit.gpgsign false");
    d
}

/// Only the files asked about: tracked changes against HEAD, untracked
/// ones as additions; another agent's dirty file and an ignored file never
/// show.
#[test]
fn untracked_files_are_additions_and_only_the_asked_files_show() {
    let d = repo("mine");
    sh(&d, "printf 'a\\nb\\n' > mine.txt && printf 'x\\n' > theirs.txt && echo ig.log > .gitignore && git add mine.txt theirs.txt .gitignore && git commit -qm init");
    sh(&d, "printf 'a\\nB\\nc\\n' > mine.txt && printf 'y\\n' > theirs.txt && printf 'n1\\nn2\\n' > 'new file.txt' && echo z > ig.log");
    let rels = s(&["mine.txt", "new file.txt", "ig.log", "gone.txt"]);
    assert_eq!(untracked(&d, &rels), Ok(s(&["new file.txt"])));
    let diff = repo_diff(&d, &rels).unwrap();
    assert!(diff.contains("diff --git a/mine.txt b/mine.txt"), "{diff}");
    // (git ends a ---/+++ path holding a space with a TAB.)
    assert!(diff.contains("--- /dev/null\n+++ b/new file.txt"), "{diff}");
    assert!(!diff.contains("theirs.txt") && !diff.contains("ig.log"), "{diff}");
    assert!(!diff.contains("\x1b["), "plain text for delta");
    assert_eq!(stat(&diff), DiffStat { added: 4, removed: 1, files: 2 });
    // Committed since: nothing left.
    sh(&d, "git add mine.txt 'new file.txt' && git commit -qm two");
    assert_eq!(repo_diff(&d, &rels), Ok(String::new()));
    let _ = fs::remove_dir_all(&d);
}

/// A big untracked file is named, not dumped; past [`UNTRACKED_MAX`] the
/// rest are counted.
#[test]
fn untracked_caps() {
    let d = repo("caps");
    sh(&d, "git commit -q --allow-empty -m init && head -c 1100000 /dev/zero | tr '\\0' 'x' > big.txt && echo s > small.txt");
    let diff = repo_diff(&d, &s(&["big.txt", "small.txt"])).unwrap();
    assert!(diff.contains(&format!("{SKIP_MARK}big.txt (1.0 MB)\n")), "{diff}");
    assert!(diff.contains("+++ b/small.txt") && !diff.contains("+++ b/big.txt"), "{diff}");
    assert_eq!(stat(&diff).files, 2);
    let many: Vec<String> = (0..UNTRACKED_MAX + 3).map(|i| format!("n{i:03}.txt")).collect();
    sh(&d, &format!("for i in $(seq 0 {}); do echo $i > $(printf 'n%03d.txt' $i); done", UNTRACKED_MAX + 2));
    let diff = repo_diff(&d, &many).unwrap();
    assert!(diff.ends_with(&format!("{SKIP_MARK}3 more untracked files\n")), "{}", &diff[diff.len().saturating_sub(200)..]);
    assert_eq!(stat(&diff).files, UNTRACKED_MAX + 3);
    let _ = fs::remove_dir_all(&d);
}

/// A git that fails is said, not taken for "no changes".
#[test]
fn git_failure_is_an_error() {
    let d = std::env::temp_dir().join(format!("agentui-diff-norepo-{}", std::process::id()));
    fs::create_dir_all(&d).unwrap();
    let e = repo_diff(&d, &s(&["a.txt"])).unwrap_err();
    assert!(e.starts_with(&format!("diff: git failed in {}: ", d.display())), "{e}");
    assert!(e.lines().count() == 1 && e.len() > format!("diff: git failed in {}: ", d.display()).len(), "{e}");
    let _ = fs::remove_dir_all(&d);
}

/// A repo with no commit yet diffs against the empty tree: a staged file
/// shows, as does an untracked one.
#[test]
fn a_repo_without_head() {
    let d = repo("nohead");
    sh(&d, "printf 'one\\n' > staged.txt && git add staged.txt && printf 'two\\n' > loose.txt");
    let diff = repo_diff(&d, &s(&["staged.txt", "loose.txt"])).unwrap();
    assert_eq!(stat(&diff), DiffStat { added: 2, removed: 0, files: 2 }, "{diff}");
    // Many repos: each starts with its work tree's line.
    let both = all_diffs(&vec![(d.clone(), s(&["staged.txt"])), (d.clone(), s(&["loose.txt"]))]).unwrap();
    assert_eq!(both.matches(&format!("{REPO_MARK}{}\n", d.display())).count(), 2);
    let _ = fs::remove_dir_all(&d);
}
