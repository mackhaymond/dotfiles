//! `agent-ui diff WINDOW_ID --client <tty>`: one agent's uncommitted diff
//! in a popup. ⌥W then a CAPITAL letter (spaces.conf's `jump` table), the
//! menu's Space d and a click on a sidebar card's diff badge run it.
//!
//! Several agents often share one checkout, so this is NOT the folder's
//! `git diff`: only the files the agent's own transcript says it edited
//! that are still dirty ([`crate::insight`]: `Insight::dirty_files`, the
//! files behind its `+48 −12 · 3f` badge), grouped by repo.
//!
//! - **Which files** ([`files_of`]), first that applies:
//!   1. `AGENT_UI_DIFF_FILES=<file>` (the sidebar's click passes its own
//!      file; tests): the `agent-ui insight-files` format, a work tree on a
//!      line of its own, then its files one per line after a TAB.
//!   2. The publishing sidebar's ([`insight::files_path`], keyed by the
//!      server's `#{start_time}` and `#{socket_path}`,
//!      [`insight::board::server_key`]): while [`insight::board::verified`]
//!      (its lock held and its `.gen` written after a complete pass: ONE
//!      sidebar per server publishes,
//!      [`insight::Board`]), its file is exactly what the badge counted
//!      (attribution history and all), and no file means none dirty. The
//!      sidebar rewrites it only when the list changes, so its age says
//!      nothing; with no publisher alive a file left behind may be stale,
//!      and is not read.
//!   3. Else a one-off [`insight::Engine`] over that window: no attribution
//!      history, only the transcript's last 32 MB, so it may differ from a
//!      badge; the title says ` · unverified`.
//! - **The diff** ([`repo_diff`]), per repo: `git diff --no-renames <base>
//!   -- <files>` (base HEAD; the empty tree in a repo with no commit yet),
//!   then every untracked one as an addition (`git diff --no-index
//!   /dev/null <file>`, found by `git ls-files --others --exclude-standard`),
//!   at most [`UNTRACKED_MAX`] of them and none over [`UNTRACKED_BYTES`]
//!   (a `# untracked, not shown: …` line says which). Pathspecs go in
//!   batches of at most [`ARG_BYTES`] ([`batches`]), never one argv past
//!   ARG_MAX. `--no-optional-locks`, `--literal-pathspecs`, no colour, no
//!   external diff: plain text, written to a private temp file. With more
//!   than one repo each starts with a `# repo <work tree>` line. A git that
//!   fails says `diff: git failed in <root>: <its first error line>`.
//! - **Nothing dirty**: no popup; notify.sh says `d: no uncommitted changes`
//!   (the agent's letter, else its title) on the client. Exit 1.
//! - **The popup**: `display-popup -w 90% -h 85%`, centred on the whole
//!   WezTerm row (`-x` [`POPUP_X`], tmux.conf.tmpl's `@popup_x`), titled
//!   ` d · <title> · +A −R · Nf ` (the border's header line). Its command
//!   opens the temp file, unlinks it, and pages it ([`Pager`]): `delta`
//!   (git's own `core.pager` on this machine, so its `[delta]` config:
//!   side-by-side, line numbers, Catppuccin) through `less -R`, else `less
//!   -R` over the diff in git's default colours ([`colour`]).
//!   `AGENT_UI_DIFF_PAGER=less` forces the fallback. The command and `-d`
//!   are not formats (only `-T` is: its `#` doubled).
//! - **Closing**: no `-E`, so tmux itself closes the popup on Esc (and C-c)
//!   while the pager runs (with `-E` those keys would go to `less`, where a
//!   bare Esc cannot be told from an arrow key's); `q` quits the pager and
//!   the script's last step closes the popup (`display-popup -C -c <tty>`).
//! - **The client** is required: tmux would guess one otherwise (and
//!   notify.sh needs it). Without it: stderr, exit 2.
//! - **Blocking**: tmux's `display-popup` client returns when the popup
//!   closes, so this command does too (spawn it detached, or from
//!   `run-shell -b`). A popup already up on the client keeps tmux from
//!   opening another, silently: the temp file still there afterwards (the
//!   popup never ran to unlink it) says so, and `diff: popup could not
//!   open` is said, exit 1. `--after <pid>` (the menu passes its own pid)
//!   first waits for that process, the popup it runs in, to be gone, and
//!   tries once more [`RETRY_AFTER`] later.
//!
//! `--print` prints the popup's tmux argv (one argument per line) instead
//! of running it, and leaves the temp file.

use crate::insight::{self, DiffStat};
use crate::pins::Procs;
use crate::proc;
use crate::sidebar::status::POPUP_X;
use crate::tmux::{Socket, Tmux, Window};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};
use std::time::{Duration, Instant};

const USAGE: &str = "usage: agent-ui diff WINDOW_ID --client <tty> [--after <pid>] [--print]";

/// The most pathspec bytes handed to one git command.
pub const ARG_BYTES: usize = 128 << 10;
/// A git command still running after this is killed.
pub const GIT_TIMEOUT: Duration = Duration::from_secs(10);
/// git's empty tree: the base in a repo with no commit yet.
pub const EMPTY_TREE: &str = "4b825dc642cb6eb9a060e54bf8d69288fbee4904";
/// Untracked files shown per repo, at most...
pub const UNTRACKED_MAX: usize = 200;
/// ...and none bigger than this.
pub const UNTRACKED_BYTES: u64 = 1 << 20;
/// The line a repo's diff starts with when there are several.
pub const REPO_MARK: &str = "# repo ";
/// The line an untracked file left out gets.
pub const SKIP_MARK: &str = "# untracked, not shown: ";
/// How long `--after` waits for its process to go...
const AFTER_MAX: Duration = Duration::from_secs(3);
/// ...and, the popup refused once, before the one retry.
pub const RETRY_AFTER: Duration = Duration::from_millis(300);

/// The command line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Args {
    pub win: String,
    pub client: String,
    pub after: Option<i32>,
    pub print: bool,
}

pub fn parse_args(args: &[String]) -> Result<Args, String> {
    let (mut win, mut client, mut after, mut print) = (None, None, None, false);
    let mut it = args.iter();
    while let Some(a) = it.next() {
        let mut val = || it.next().cloned().ok_or_else(|| format!("{a} needs a value"));
        match a.as_str() {
            "--client" => client = Some(val()?),
            "--after" => {
                let v = val()?;
                after = Some(v.parse::<i32>().ok().filter(|p| *p > 0).ok_or_else(|| format!("bad pid {v:?}"))?);
            }
            "--print" => print = true,
            w if w.starts_with('@') && win.is_none() => win = Some(w.to_string()),
            _ => return Err(format!("unexpected argument {a:?}")),
        }
    }
    let win = win.ok_or("no window id")?;
    // (Never a guess: tmux would pick some client for the popup.)
    let client = client.filter(|c| !c.is_empty()).ok_or("no --client: whose popup?")?;
    Ok(Args { win, client, after, print })
}

/// One repo's part: its work tree and the files, repo-relative.
pub type Files = Vec<(PathBuf, Vec<String>)>;

/// The `agent-ui insight-files` format back into [`Files`]: a line without
/// a leading TAB starts a repo, a TAB line is one of its files.
pub fn parse_files(text: &str) -> Files {
    let mut out: Files = Vec::new();
    for l in text.lines() {
        match l.strip_prefix('\t') {
            Some(f) if !f.is_empty() => {
                if let Some((_, v)) = out.last_mut() {
                    v.push(f.to_string());
                }
            }
            Some(_) => {}
            None if !l.trim().is_empty() => out.push((PathBuf::from(l), Vec::new())),
            None => {}
        }
    }
    out.retain(|(_, v)| !v.is_empty());
    out
}

/// `rels` cut into runs whose bytes (each path plus its NUL) stay within
/// `max`; a single path longer than that still gets a run of its own.
pub fn batches(rels: &[String], max: usize) -> Vec<&[String]> {
    let mut out = Vec::new();
    let (mut start, mut size) = (0, 0);
    for (i, r) in rels.iter().enumerate() {
        let n = r.len() + 1;
        if i > start && size + n > max {
            out.push(&rels[start..i]);
            (start, size) = (i, 0);
        }
        size += n;
    }
    if start < rels.len() {
        out.push(&rels[start..]);
    }
    out
}

fn git(root: &Path) -> Command {
    let mut c = Command::new("git");
    c.args(["--no-optional-locks", "--literal-pathspecs", "-c", "core.quotepath=off", "-C"])
        .arg(root)
        .env("GIT_OPTIONAL_LOCKS", "0")
        .env("GIT_TERMINAL_PROMPT", "0");
    c
}

const DIFF_FLAGS: [&str; 4] = ["--no-color", "--no-ext-diff", "--no-renames", "--no-textconv"];

/// Run one git command: its stdout when its exit status is one of `ok`,
/// else `diff: git failed in <root>: <first stderr line>`.
fn run_git(root: &Path, c: Command, ok: &[i32]) -> Result<String, String> {
    let failed = |why: &str| format!("diff: git failed in {}: {why}", root.display());
    match proc::run(c, None, GIT_TIMEOUT) {
        Ok(o) if o.status.code().is_some_and(|c| ok.contains(&c)) => Ok(o.stdout),
        Ok(o) => Err(failed(o.stderr.lines().find(|l| !l.trim().is_empty()).unwrap_or("no reason given").trim())),
        Err(e) => Err(failed(&e.to_string())),
    }
}

/// The untracked (and not ignored) files among `rels`, in `rels`' order.
pub fn untracked(root: &Path, rels: &[String]) -> Result<Vec<String>, String> {
    let mut found = std::collections::HashSet::new();
    for b in batches(rels, ARG_BYTES) {
        let mut c = git(root);
        c.args(["ls-files", "-z", "--others", "--exclude-standard", "--"]).args(b);
        found.extend(run_git(root, c, &[0])?.split('\0').filter(|p| !p.is_empty()).map(String::from));
    }
    Ok(rels.iter().filter(|r| found.contains(*r)).cloned().collect())
}

/// `1.3 MB`.
fn size(n: u64) -> String {
    format!("{:.1} MB", n as f64 / (1 << 20) as f64)
}

/// `root`'s uncommitted diff of `rels`: the tracked files against HEAD (the
/// empty tree with no commit yet), then each untracked one as all added
/// (capped: [`UNTRACKED_MAX`], [`UNTRACKED_BYTES`], a [`SKIP_MARK`] line
/// for each left out). Err: a git that failed.
pub fn repo_diff(root: &Path, rels: &[String]) -> Result<String, String> {
    let mut c = git(root);
    c.args(["rev-parse", "--verify", "-q", "HEAD"]);
    let has_head = proc::run(c, None, GIT_TIMEOUT).is_ok_and(|o| o.ok());
    let base = if has_head { "HEAD" } else { EMPTY_TREE };
    let mut out = String::new();
    for b in batches(rels, ARG_BYTES) {
        let mut c = git(root);
        c.arg("diff").args(DIFF_FLAGS).args([base, "--"]).args(b);
        out.push_str(&run_git(root, c, &[0])?);
    }
    let new = untracked(root, rels)?;
    for (i, f) in new.iter().enumerate() {
        if i == UNTRACKED_MAX {
            out.push_str(&format!("{SKIP_MARK}{} more untracked files\n", new.len() - i));
            break;
        }
        let len = std::fs::metadata(root.join(f)).map_or(0, |m| m.len());
        if len > UNTRACKED_BYTES {
            out.push_str(&format!("{SKIP_MARK}{f} ({})\n", size(len)));
            continue;
        }
        // (--no-index exits 1 when the two differ: that is the answer.)
        let mut c = git(root);
        c.args(["diff", "--no-index"]).args(DIFF_FLAGS).args(["--", "/dev/null", f]);
        out.push_str(&run_git(root, c, &[0, 1])?);
    }
    Ok(out)
}

/// Every repo's diff, one after the other; with more than one repo each
/// starts with a [`REPO_MARK`] line.
pub fn all_diffs(files: &Files) -> Result<String, String> {
    let mut out = String::new();
    for (root, rels) in files {
        let d = repo_diff(root, rels)?;
        if d.is_empty() {
            continue;
        }
        if files.len() > 1 {
            out.push_str(&format!("{REPO_MARK}{}\n", root.display()));
        }
        out.push_str(&d);
    }
    Ok(out)
}

/// A unified diff's lines added and removed (hunk bodies only) and its
/// files (`diff --git` headers, a binary file with no lines; an untracked
/// file left out counts as one, with none).
pub fn stat(diff: &str) -> DiffStat {
    let mut s = DiffStat::default();
    let mut hunk = false;
    for l in diff.lines() {
        if l.starts_with("diff --git ") {
            (s.files, hunk) = (s.files + 1, false);
        } else if let Some(rest) = l.strip_prefix(SKIP_MARK) {
            let more = rest.strip_suffix(" more untracked files").and_then(|n| n.parse::<usize>().ok());
            (s.files, hunk) = (s.files + more.unwrap_or(1), false);
        } else if l.starts_with("@@") {
            hunk = true;
        } else if hunk && l.starts_with('+') {
            s.added += 1;
        } else if hunk && l.starts_with('-') {
            s.removed += 1;
        }
    }
    s
}

/// The diff in git's default colours (bold headers, cyan hunk lines, green
/// additions, red removals) for the `less -R` fallback; a repo's line
/// becomes a magenta rule, a left-out file's a yellow note.
pub fn colour(diff: &str) -> String {
    let mut out = String::with_capacity(diff.len() + diff.len() / 4);
    let mut hunk = false;
    for l in diff.lines() {
        if let Some(root) = l.strip_prefix(REPO_MARK) {
            hunk = false;
            out.push_str(&format!("\x1b[1;35m━━ {root} {}\x1b[m\n", "━".repeat(60)));
            continue;
        }
        let sgr = if l.starts_with(SKIP_MARK) {
            hunk = false;
            "33"
        } else if l.starts_with("diff --git ") {
            hunk = false;
            "1"
        } else if l.starts_with("@@") {
            hunk = true;
            "36"
        } else if !hunk {
            "1"
        } else if l.starts_with('+') {
            "32"
        } else if l.starts_with('-') {
            "31"
        } else {
            ""
        };
        if sgr.is_empty() {
            out.push_str(l);
        } else {
            out.push_str(&format!("\x1b[{sgr}m{l}\x1b[m"));
        }
        out.push('\n');
    }
    out
}

/// Single-quote for /bin/sh. Unlike [`crate::actions::sh_quote`], `#` is
/// left alone: display-popup's command is not a format.
pub fn quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\\''"))
}

/// What pages the diff in the popup.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Pager {
    /// delta over the plain diff, paging through less.
    Delta { delta: PathBuf, less: PathBuf },
    /// less over the diff in git's colours.
    Less(PathBuf),
}

/// `name` on `$PATH`, else in Homebrew's or /usr/local's bin.
pub fn which(name: &str) -> Option<PathBuf> {
    let path = std::env::var("PATH").unwrap_or_default();
    path.split(':')
        .filter(|d| !d.is_empty())
        .map(PathBuf::from)
        .chain(["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"].map(PathBuf::from))
        .map(|d| d.join(name))
        .find(|p| p.is_file())
}

impl Pager {
    /// delta when there is one (unless `force_less`), else less.
    pub fn choose(delta: Option<PathBuf>, less: PathBuf, force_less: bool) -> Pager {
        match delta {
            Some(delta) if !force_less => Pager::Delta { delta, less },
            _ => Pager::Less(less),
        }
    }

    /// This machine's: `AGENT_UI_DIFF_PAGER=less` forces the fallback.
    pub fn find() -> Pager {
        let force = std::env::var("AGENT_UI_DIFF_PAGER").is_ok_and(|v| v == "less");
        Pager::choose(which("delta"), which("less").unwrap_or_else(|| "less".into()), force)
    }

    /// The temp file's contents for this pager: the plain diff for delta,
    /// coloured for less.
    pub fn content(&self, diff: &str) -> String {
        match self {
            Pager::Delta { .. } => diff.to_string(),
            Pager::Less(_) => colour(diff),
        }
    }

    /// The popup's shell command: page `file` (opened, then unlinked, so
    /// an Esc that kills the pager leaves nothing behind), then run `close`
    /// (the argv that closes the popup) once the pager quits.
    pub fn script(&self, file: &Path, close: &[String]) -> String {
        let f = quote(&file.to_string_lossy());
        // -c paints from the top: delta's `navigate` makes less search for
        // the first file at start, which on a diff shorter than the popup
        // otherwise leaves it at the bottom under a screenful of blanks.
        let page = match self {
            // (delta splits $DELTA_PAGER into words itself.)
            Pager::Delta { delta, less } => format!("DELTA_PAGER={} {} --paging=always",
                quote(&format!("{} -R -c --mouse", less.to_string_lossy())), quote(&delta.to_string_lossy())),
            Pager::Less(l) => format!("{} -R -c --mouse", quote(&l.to_string_lossy())),
        };
        let close: Vec<String> = close.iter().map(|a| quote(a)).collect();
        format!("exec <{f} && rm -f {f}; {page}; {}", close.join(" "))
    }
}

/// The popup's title: ` d · <title> · +A −R · Nf ` (` · unverified` when
/// the files are the fallback's; `#` doubled: it is a format).
pub fn title(letter: &str, label: &str, s: &DiffStat, verified: bool) -> String {
    let label = crate::text::clip(&crate::text::sanitize(label), 48);
    let head = if letter.is_empty() { label } else { format!("{letter} · {label}") };
    let tail = if verified { "" } else { " · unverified" };
    format!(" {head} · {}{tail} ", s.badge()).replace('#', "##")
}

/// `display-popup` (after `tmux -u [-L/-S]`) for `script`: on `client`,
/// 90% × 85%, centred on the whole row, `title` in its border, started in
/// `dir`; no -E (see the module docs).
pub fn popup_args(client: &str, title: &str, dir: &Path, script: &str) -> Vec<String> {
    let mut v: Vec<String> = ["display-popup", "-c", client, "-w", "90%", "-h", "85%", "-x", POPUP_X, "-T", title, "-d"]
        .iter()
        .map(|s| s.to_string())
        .collect();
    v.push(dir.to_string_lossy().into_owned());
    v.push(script.to_string());
    v
}

/// The argv that closes `client`'s popup from inside it, on this server.
pub fn close_argv(tmux: &Tmux, client: &str) -> Vec<String> {
    let mut v = vec![which("tmux").map_or_else(|| "tmux".into(), |p| p.to_string_lossy().into_owned())];
    match &tmux.socket {
        Some(Socket::Name(n)) => v.extend(["-L".into(), n.clone()]),
        Some(Socket::Path(p)) => v.extend(["-S".into(), p.clone()]),
        None => {}
    }
    v.extend(["display-popup".into(), "-C".into(), "-c".into(), client.to_string()]);
    v
}

/// What the messages call the window: its letter, else its title.
fn name_of(w: &Window) -> String {
    if w.key.trim().is_empty() { w.label.clone() } else { w.key.trim().to_string() }
}

/// Where the files come from (see the module docs).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    /// `AGENT_UI_DIFF_FILES`.
    Given,
    /// A live sidebar's file (or its absence: none dirty).
    Sidebar,
    /// A one-off engine: unverified.
    Engine,
}

/// Which [`Source`] to read, from `$AGENT_UI_DIFF_FILES` being set and a
/// live publisher's files being there ([`insight::board::verified`]).
pub fn source(given: bool, published: bool) -> Source {
    if given {
        Source::Given
    } else if published {
        Source::Sidebar
    } else {
        Source::Engine
    }
}

/// A file in the insight-files format; a missing one is no files.
pub fn read_files(p: &Path) -> Result<Files, String> {
    match std::fs::read_to_string(p) {
        Ok(t) => Ok(parse_files(&t)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Vec::new()),
        Err(e) => Err(format!("diff: {}: {e}", p.display())),
    }
}

/// The window's dirty touched files, and whether they are verified (the
/// badge's own: not the [`Source::Engine`] fallback). Err: why not.
fn files_of(tmux: &Tmux, w: &Window) -> Result<(Files, bool), String> {
    let given = std::env::var("AGENT_UI_DIFF_FILES").ok().filter(|p| !p.is_empty());
    if w.state.is_empty() && given.is_none() {
        return Err(format!("{}: not an agent", name_of(w)));
    }
    // (The server's insight key: its start time and socket path,
    // insight::board::server_key.)
    let info = tmux.run(&["display-message", "-p", "#{start_time}\t#{socket_path}"]).ok().filter(|o| o.ok());
    let info = info.map(|o| o.stdout.trim_end().to_string()).unwrap_or_default();
    let key = info.split_once('\t').filter(|(s, _)| !s.is_empty()).map(|(s, p)| insight::board::server_key(s, p));
    let published = key.as_deref().is_some_and(|k| insight::board::verified(&insight::files_dir(), k));
    match source(given.is_some(), published) {
        Source::Given => read_files(Path::new(given.as_deref().unwrap_or_default())).map(|f| (f, true)),
        Source::Sidebar => read_files(&insight::files_path(key.as_deref().unwrap_or_default(), &w.id)).map(|f| (f, true)),
        Source::Engine => {
            let mut snap = tmux.snapshot();
            snap.windows.retain(|r| r.id == w.id);
            let all = insight::Engine::home().update(&snap, crate::text::now());
            let i = all.get(&w.id).ok_or_else(|| format!("{}: no transcript to read", name_of(w)))?;
            Ok((insight::by_repo(&i.dirty_files), false))
        }
    }
}

/// notify.sh (`$NOTIFY_SH`, else the scripts directory's) on `client`;
/// `#` doubled, notify.sh expands formats.
fn notify(tmux: &Tmux, client: &str, kind: &str, text: &str) {
    let script = std::env::var("NOTIFY_SH").ok().filter(|s| !s.is_empty())
        .map_or_else(|| crate::actions::scripts_dir().join("notify.sh"), PathBuf::from);
    let mut c = Command::new(script);
    c.args(["-c", client, "-k", kind, "--", &text.replace('#', "##")]);
    tmux.script_env(&mut c);
    let _ = proc::run(c, None, Duration::from_secs(5));
}

/// Wait (at most [`AFTER_MAX`]) for `pid` to be gone, then a beat for tmux
/// to take its popup down.
fn wait_gone(pid: i32) {
    let end = Instant::now() + AFTER_MAX;
    while crate::pins::LiveProcs.alive(pid) && Instant::now() < end {
        std::thread::sleep(Duration::from_millis(20));
    }
    std::thread::sleep(Duration::from_millis(60));
}

/// A new private temp file (0600) holding `text`.
fn temp_file(text: &str) -> std::io::Result<PathBuf> {
    use std::os::unix::fs::OpenOptionsExt;
    let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.subsec_nanos());
    let p = std::env::temp_dir().join(format!("agent-ui-diff-{}-{nanos}.diff", std::process::id()));
    let mut f = std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(&p)?;
    f.write_all(text.as_bytes())?;
    Ok(p)
}

/// Raise the popup (`argv`) and wait for it to close: true if it ran (its
/// script unlinked `file`), false if tmux never opened it.
fn show(tmux: &Tmux, argv: &[String], file: &Path) -> bool {
    let mut c = tmux.command();
    c.args(argv);
    // (tmux's own status is not success when Esc closed the popup.)
    let _ = c.status();
    !file.exists()
}

/// The subcommand. Exit 0 shown (or printed), 1 nothing to show / refused
/// (said through notify.sh), 2 usage.
pub fn run(tmux: Tmux, args: &[String]) -> ExitCode {
    let a = match parse_args(args) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("agent-ui diff: {e}\n{USAGE}");
            return ExitCode::from(2);
        }
    };
    let client = a.client.as_str();
    let refuse = |m: String, kind: &str| {
        eprintln!("agent-ui diff: {m}");
        notify(&tmux, client, kind, &m);
        ExitCode::from(1)
    };
    let snap = tmux.snapshot();
    let Some(w) = snap.rows(&a.win).next().cloned() else {
        return refuse(format!("diff: {}", crate::actions::PANE_GONE), "warn");
    };
    let (files, verified) = match files_of(&tmux, &w) {
        Ok(f) => f,
        Err(m) => return refuse(m, "warn"),
    };
    let diff = match all_diffs(&files) {
        Ok(d) => d,
        Err(m) => return refuse(m, "warn"),
    };
    let s = stat(&diff);
    if s.files == 0 {
        return refuse(format!("{}: no uncommitted changes", name_of(&w)), "info");
    }
    let pager = Pager::find();
    let file = match temp_file(&pager.content(&diff)) {
        Ok(f) => f,
        Err(e) => return refuse(format!("diff: {e}"), "warn"),
    };
    let script = pager.script(&file, &close_argv(&tmux, client));
    let dir = files.first().map(|(r, _)| r.clone()).unwrap_or_else(|| "/".into());
    let argv = popup_args(client, &title(w.key.trim(), &w.label, &s, verified), &dir, &script);
    if a.print {
        let mut out = std::io::stdout().lock();
        for x in &argv {
            let _ = writeln!(out, "{x}");
        }
        return ExitCode::SUCCESS;
    }
    if let Some(pid) = a.after {
        wait_gone(pid);
    }
    let mut ran = show(&tmux, &argv, &file);
    if !ran && a.after.is_some() {
        std::thread::sleep(RETRY_AFTER);
        ran = show(&tmux, &argv, &file);
    }
    if ran {
        return ExitCode::SUCCESS;
    }
    let _ = std::fs::remove_file(&file);
    refuse("diff: popup could not open".into(), "warn")
}

#[cfg(test)]
mod tests;
